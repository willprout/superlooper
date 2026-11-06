"""The consult (issue #538) — a blocked worker's question goes FIRST to a fork of the session that
approved the issue, which answers what it can and escalates what only the owner can decide.

``plugin/skills/superlooper/references/consult-standing-rule.md`` is the authority: the owner's
rulings of 2026-11-04 and what a consult may and may not do. This module is the mechanics that rule
needs. It must never weaken, widen or restate it.

Owner, on why: "ninety percent of the time that a session is frozen or parked or has a question for
me, I just tell my orchestrator for that project to unfuck the loop." The orchestrator that APPROVED
the issue holds the context of what the owner wanted when the work was shaped, so it is asked first.

================================ THE SHAPE ================================

The #163 question flow already did most of this: a worker writes its question with options and a
recommendation, pushes its WIP and exits; the runner posts the question and frees the lane; a fresh
session relaunches with the question and answer in its brief. Two things change — WHO answers, and
WHAT triggers the relaunch:

  1. The worker's blocked file arrives. Under the two-question cap, the runner reads the issue's
     approver stamp (#537, ``approval.read``) and, when the approving session is on THIS machine
     with its transcript on disk, launches a fork of it (``c<N>``, #536's mechanics) in the
     approver's own directory. The issue moves to ``consulting``, which holds NO lane.
  2. The fork reads the packet (``render_packet``), re-checks the current state, writes ONE outcome
     file and stops. It acts on nothing itself.
  3. The runner reads the outcome file (``parse_outcome``):
       * ANSWER   -> the runner posts the answer, MARKED as the approving session's and not the
                     owner's (``answer_comment``), and relaunches the worker with the Q&A in its
                     brief. Nobody applies ``agent-ready``: the relaunch rides the runner's own
                     ``in-progress`` stamp, the approval the loop already acted on.
       * ESCALATE -> the question goes to the owner exactly as before, carrying the fork's reasoning
                     and recommendation (``escalation_note``).
     No stamp, a stamp from another machine, no transcript, a launch that fails, a fork that dies or
     writes nothing in ``TIMEOUT_SECONDS``, or an outcome file nobody can read: the question goes to
     the owner exactly as it does today, and the REASON is journaled (``FALLBACK_REASONS``).

Pure: no gh, no git, no disk, no clock. The runner does the I/O; ``actions.decide`` does the routing.
"""
import collections
import hashlib
import re
import time

# --------------------------------------------------------------------------- the session's id

# The consult's id shape. ONE consult per issue at a time — a second question can only arrive from a
# worker that a settled consult relaunched — so the id is the issue's own number: the outcome file,
# the brief and every run-state marker of a consult name the issue they are about, and nothing has
# to be looked up to tie them back. `launch.CONSULT_RE` and the PreToolUse role table spell the same
# shape; `tests/test_consult.py` pins all three together.
ID_RE = re.compile(r"^c([0-9]+)$")
PREFIX = "c"


def consult_id(num):
    """The consult session id for issue `num`, or None for anything that is not a positive int."""
    if type(num) is not int or num <= 0:
        return None
    return "%s%d" % (PREFIX, num)


def is_consult_id(value):
    """Is `value` a consult session id? Total on any input, like `triage.is_flight_id`."""
    return isinstance(value, str) and ID_RE.match(value) is not None


def issue_id_of(cid):
    """The worker lane id (``i<N>``) a consult id is about, or None."""
    m = ID_RE.match(cid) if isinstance(cid, str) else None
    return "i%s" % m.group(1) if m else None


# --------------------------------------------------------------------------- the bounded wait

# How long a consult may run before the question goes to the owner anyway. The #536 spike measured
# a fork of a large compacted transcript at ~5 s to a ready prompt and ~25 s to write seven answers;
# a real consult also re-checks the branch, the PR and origin/main before deciding, which is minutes,
# not hours. Past this the runner stops waiting: a consult that has not decided by then is not going
# to be faster than the owner, and the bound is also what keeps a host-revived consult from hanging.
TIMEOUT_SECONDS = 20 * 60

# --------------------------------------------------------------------------- the fallbacks

NO_STAMP = "no_stamp"
STAMP_UNREAD = "stamp_unread"
OTHER_MACHINE = "other_machine"
NO_TRANSCRIPT = "no_transcript"
LAUNCH_FAILED = "launch_failed"
FORK_EXITED = "fork_exited"
TIMEOUT = "timeout"
MALFORMED = "malformed_outcome"
HELD = "consult_held"

# Every way a consult hands the question back to the owner WITHOUT a decision, and the sentence the
# journal and the morning report carry for it. A table, so a reason nobody wrote a sentence for
# cannot reach a reader as a bare token.
FALLBACK_REASONS = {
    NO_STAMP: "the issue carries no approver stamp, so there is no approving session to consult",
    # Never inferred as NO_STAMP (the #21 refused-is-not-empty discipline): GitHub refused the read,
    # so nobody knows whether a stamp exists, and the question goes to the owner as it always did.
    STAMP_UNREAD: "the approver stamp could not be read from GitHub just now",
    OTHER_MACHINE: "the approving session ran on another machine, and consults are same-machine "
                   "only",
    NO_TRANSCRIPT: "the approving session's transcript is not on this machine, so there is "
                   "nothing to fork",
    LAUNCH_FAILED: "the consult fork failed to launch",
    FORK_EXITED: "the consult fork exited without writing an outcome",
    TIMEOUT: "the consult fork wrote no outcome within %d minutes" % (TIMEOUT_SECONDS // 60),
    MALFORMED: "the consult fork's outcome file could not be read as ANSWER or ESCALATE",
    # A consult waits on the usage ceilings and machine-wide launch holds like any session, but
    # never for longer than it would have been given to decide: past the same bound the question
    # goes to the owner, so a hold can never keep a question from everyone (fresh review, P1).
    HELD: "the consult could not start within %d minutes" % (TIMEOUT_SECONDS // 60),
    # (#539) The handback consult's own three. A handback is already a SAFE state the owner can act
    # on, so a consult that cannot start NOW is not waited for: the handback reaches him as today.
    "consult_held_now": "a usage ceiling or a machine-wide launch hold kept the consult from "
                        "starting, so the handback went to the owner as it always did",
    "handback_cap": "the issue reached its handback cap, so this handback goes to the owner "
                    "regardless, with the consult history attached",
    "refused_move": "the consult fork asked for a move no consult may make",
}


def fallback_sentence(reason, detail=None):
    """The human line for one fallback reason (plus an optional detail clause)."""
    text = FALLBACK_REASONS.get(reason) if isinstance(reason, str) else None
    text = text or "the consult could not decide (%s)" % (reason if isinstance(reason, str)
                                                           else "unknown reason")
    if isinstance(detail, str) and detail.strip():
        text += " (%s)" % " ".join(detail.split())[:200]
    return text


def same_machine(stamp_host, hostname):
    """Is the stamped host THIS machine? Compared on the first label only, the way the stamp was
    written (`approval.make_stamp`): `Williams-Mini.lan` and `Williams-Mini.local` are one machine
    on two networks. Fails closed on anything unreadable."""
    if not (isinstance(stamp_host, str) and stamp_host.strip()):
        return False
    if not (isinstance(hostname, str) and hostname.strip()):
        return False
    here = hostname.strip().split(".", 1)[0]
    there = stamp_host.strip().split(".", 1)[0]
    return bool(here) and here.lower() == there.lower()


# --------------------------------------------------------------------------- the outcome file

ANSWER = "answer"
ESCALATE = "escalate"

Outcome = collections.namedtuple("Outcome", ["kind", "answer", "reasoning", "recommendation"])

# The four states an outcome file can be in. `incomplete` is NOT a failure: the fork may be writing
# it right now, so the runner waits (bounded by TIMEOUT_SECONDS) rather than settle on half a file.
ABSENT = "absent"
INCOMPLETE = "incomplete"
UNREADABLE = "malformed"
READ = "ok"

END_LINE = "END"
_OUTCOME_RE = re.compile(r"^OUTCOME:\s*(ANSWER|ESCALATE)\s*$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^(ANSWER|REASONING|RECOMMENDATION):\s*(.*)$")


def parse_outcome(text):
    """(state, Outcome-or-None) for the fork's outcome file.

    The format (``outcome_format``) is line-based and ends with a bare ``END`` line. Without that
    line the file is INCOMPLETE — a fork mid-write — never a decision; with it, the first non-blank
    line must be ``OUTCOME: ANSWER`` or ``OUTCOME: ESCALATE``. An ANSWER with no answer text is
    UNREADABLE, never an empty answer embedded as binding: that would relaunch the worker on a
    decision nobody made. An ESCALATE may lack its reasoning or recommendation — escalating is the
    safe direction, and the owner gets the question either way."""
    if not isinstance(text, str):
        return ABSENT, None
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1].strip() != END_LINE:
        return INCOMPLETE, None
    body = lines[:-1]
    while body and not body[0].strip():
        body.pop(0)
    head = _OUTCOME_RE.match(body[0].strip()) if body else None
    if not head:
        return UNREADABLE, None
    kind = ANSWER if head.group(1).upper() == "ANSWER" else ESCALATE
    sections = {}
    current = None
    for ln in body[1:]:
        m = _SECTION_RE.match(ln.strip())
        if m and m.group(1) not in sections:
            current = m.group(1)
            sections[current] = [m.group(2)] if m.group(2).strip() else []
            continue
        if current is not None:
            sections[current].append(ln)
    def _get(key):
        return "\n".join(sections.get(key, [])).strip()
    answer, reasoning, recommendation = _get("ANSWER"), _get("REASONING"), _get("RECOMMENDATION")
    if kind == ANSWER and not answer:
        return UNREADABLE, None
    return READ, Outcome(kind, answer, reasoning, recommendation)


def outcome_format():
    """The outcome-file contract, exactly as the brief hands it to the fork."""
    return ("Write EXACTLY one of these two shapes, ending with a line that says only `END`:\n\n"
            "```\n"
            "OUTCOME: ANSWER\n"
            "ANSWER:\n"
            "<the answer the worker should build on>\n"
            "REASONING:\n"
            "<why you know this is what the owner wanted: cite the conversation or the approved "
            "text>\n"
            "END\n"
            "```\n\n"
            "```\n"
            "OUTCOME: ESCALATE\n"
            "REASONING:\n"
            "<why this is a real fork only the owner can decide>\n"
            "RECOMMENDATION:\n"
            "<the option you would take, and why>\n"
            "END\n"
            "```\n\n"
            "Write it to a temporary file and move it into place, so the runner never reads half of "
            "it. Without the final `END` line the runner treats the file as still being written.")


# --------------------------------------------------------------------------- the handback history

# The journal acts that are an issue HANDING BACK, or being handed back into the loop. Owner ruling
# 2026-11-04: the consult must have context on the issue's previous handbacks. #536 measured why the
# packet must carry them rather than trusting the fork's memory: a fork remembers the approver's
# LATEST compaction summary and everything after it, and nothing older.
HISTORY_ACTS = ("post_question", "answer_relaunch", "consult_outcome", "park", "bounce",
                "reapprove", "resume_at_gate", "regenerate", "recover", "absorb_close")
_RECOVER_TIERS = ("frozen", "exited")
_DETAIL_MAX = 600


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "time unknown"


def _clip(text, limit=_DETAIL_MAX):
    text = " ".join(str(text).split()) if text is not None else ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _history_line(r):
    act = r.get("act")
    when = _iso(r.get("ts"))
    if act == "post_question":
        return "%s — asked the owner a question: %s" % (when, _clip(r.get("question")))
    if act == "answer_relaunch":
        return "%s — the owner answered and re-approved; a fresh session relaunched" % when
    if act == "consult_outcome" and r.get("handback") in HANDBACK_KINDS:
        return _handback_history_line(when, r)
    if act == "consult_outcome":
        kind = r.get("outcome_kind")
        if kind == "answered":
            return "%s — a consult of the approving session answered: %s" % (
                when, _clip(r.get("answer")))
        if kind == "escalated":
            return "%s — a consult escalated the question to the owner: %s" % (
                when, _clip(r.get("reasoning")))
        return "%s — the question fell back to the owner: %s" % (
            when, fallback_sentence(r.get("reason")))
    if act == "park":
        # `cause` is a short token on most parks and a whole sentence on some older ones (measured
        # on the live journal), where it only repeats the memo — so it is named only when short.
        cause = r.get("cause")
        tag = (" (%s)" % cause if isinstance(cause, str) and 0 < len(cause) <= 40
               and " " not in cause else "")
        return "%s — parked for the owner%s: %s" % (when, tag, _clip(r.get("memo")))
    if act == "bounce":
        return "%s — bounced by its worker (premise drift): %s" % (when, _clip(r.get("memo")))
    if act == "reapprove":
        return "%s — the owner re-approved it; the loop re-released it" % when
    if act == "resume_at_gate":
        return "%s — the owner re-approved a finished build; it resumed at the merge gate" % when
    if act == "regenerate":
        branch = r.get("new_branch")
        return "%s — regenerated from the issue after a merge conflict%s" % (
            when, " (rebuilt on `%s`)" % branch if isinstance(branch, str) and branch else "")
    if act == "recover":
        return "%s — relaunched after its session %s" % (
            when, "froze" if r.get("tier") == "frozen" else "exited")
    if act == "absorb_close":
        return "%s — the owner closed it on GitHub" % when
    return None


def history_lines(records, num):
    """The issue's handback history, oldest first, one line per event: every earlier question,
    answer, consult, park, freeze/crash recovery and bounce, and what resolved it.

    Only records that LANDED count (an outcome beginning `ok`): a question whose comment failed
    never reached the owner, and a retry storm must not read as twelve parks. Total on garbage."""
    out = []
    want_id = "i%d" % num if type(num) is int else None
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict) or r.get("act") not in HISTORY_ACTS:
            continue
        if r.get("num") != num and (want_id is None or r.get("id") != want_id):
            continue
        if not str(r.get("outcome", "")).startswith("ok"):
            continue
        if r.get("act") == "recover" and r.get("tier") not in _RECOVER_TIERS:
            continue
        line = _history_line(r)
        if line:
            out.append("- " + line)
    return out


def qa_lines(qa_log, operator):
    """The answered questions on record (loopstate's `qa_log`), with WHO answered each."""
    out = []
    for i, e in enumerate(qa_log if isinstance(qa_log, list) else [], 1):
        if not isinstance(e, dict) or not isinstance(e.get("question"), str):
            continue
        who = ("the approving session (a consult)" if e.get("by") == "consult"
               else operator)
        answer = e.get("answer") if isinstance(e.get("answer"), str) else ""
        out.append("- **Q%d:** %s\n  **Answered by %s:** %s"
                   % (i, _clip(e["question"]), who,
                      _clip(answer) if answer.strip() else "(re-approved with no written answer)"))
    return out


# --------------------------------------------------------------------------- the packet

# Where the consult reads its standing rule in the approving checkout (this monorepo's own path).
# The template also names the copy the gated installer mirrors with the ops docs — spelled in the
# TEMPLATE, as the triage brief spells its own, because no engine script may name the installed
# skill home (`tests/test_one_publish_door.py`); `tests/test_consult.py` pins that copy against
# `ops_docs.OPS_DOCS`.
RULE_PATH = "plugin/skills/superlooper/references/consult-standing-rule.md"

def _sub(template, mapping):
    """ONE-pass ``{name}`` substitution. A value is placed verbatim and never re-scanned, so a
    ``{placeholder}`` inside the issue body or a quoted question stays literal — the discipline the
    worker brief keeps for the approved body. Unknown names are left as written."""
    return re.sub(r"\{([a-z_]+)\}",
                  lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0),
                  template)


def _comment_block(comments, owner, operator, ok=True, num=None):
    """The issue's comment thread, every author, VERBATIM — context for the consult, attributed so
    it can tell the owner's word from the loop's own machine comments. A thread GitHub REFUSED to
    serve is said to be unread, never rendered as "no comments": an owner amendment the consult
    never saw would otherwise read as one that does not exist."""
    if not ok:
        return ("_The comment thread could not be read from GitHub when this packet was built — "
                "read it yourself (`gh issue view %s --comments`) before you decide: it may carry "
                "the owner's amendments._" % (num if type(num) is int else "<N>"))
    out = []
    for c in comments if isinstance(comments, list) else []:
        if not isinstance(c, dict) or not isinstance(c.get("body"), str):
            continue
        author = c.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        when = c.get("createdAt") if isinstance(c.get("createdAt"), str) else ""
        body = c["body"]
        if body.lstrip().startswith("<!-- superlooper-"):
            who = "the loop (a machine comment the runner posted as @%s)" % (login or "unknown")
        elif isinstance(owner, str) and owner and login == owner:
            who = "@%s (%s, the owner — or a session posting as the owner)" % (login, operator)
        else:
            who = "@%s (not the owner)" % (login or "unknown")
        out.append("**%s%s:**\n%s\n" % (who, " — " + when if when else "", body.strip()))
    return "\n".join(out) if out else "_No comments._"


def render_packet(template, *, cid, num, title, body, question, comments, owner, operator,
                  repo_slug, dev_branch, branch_state, history, qa, outcome_path, home,
                  approver_session, comments_ok=True):
    """The consult's whole brief. Everything the fork knows about now comes from here: its memory
    dates from approval time (and only from the approver's last compaction on), so the packet
    carries the issue as approved, its thread, the question, the branch/PR state and the history."""
    hist = list(history or []) + list(qa or [])
    return _sub(template, {
        "cid": cid, "issue_num": str(num), "title": title or "", "repo_slug": repo_slug or "",
        "dev_branch": dev_branch or "main", "operator": operator,
        "approver_session": approver_session, "home": home,
        "question": (question or "").strip() or "_(the worker's question was empty)_",
        "issue_body": (body or "").strip() or "_(empty body)_",
        "comments": _comment_block(comments, owner, operator, ok=comments_ok, num=num),
        "branch_state": "\n".join(branch_state or []) or "_(nothing recorded)_",
        "history": "\n".join(hist) if hist else "_None — this is the issue's first handback._",
        "outcome_path": outcome_path, "outcome_format": outcome_format(),
        "rule_path": RULE_PATH,
        "timeout_minutes": str(TIMEOUT_SECONDS // 60),
    })


# --------------------------------------------------------------------------- what the runner posts

# Every comment the runner posts FOR a consult begins with this. Two jobs, both load-bearing:
#   * the brief skips any comment beginning `<!-- superlooper-` when it renders binding amendments,
#     and every loop comment posts under the owner's own login — so without the marker the
#     consult's answer would reach the next worker as "the owner's binding amendment", the approving
#     session's words promoted to the owner's.
#   * it is not `<!-- superlooper-answer -->`, so the runner's answer ingestion (#163) can never
#     read a consult's comment as the owner answering.
MARKER = "<!-- superlooper-consult -->"


def answer_comment(question, outcome, approver_session, operator):
    """The comment the runner posts on an ANSWER: unmistakably the approving session's word."""
    reasoning = (outcome.reasoning or "").strip() or "_(no reasoning given)_"
    return ("%s\n"
            "**Answered by the approving session, not by %s.** A worker paused this issue to ask "
            "a question. Before it reached %s, the loop consulted a fork of the session that "
            "approved this issue (`%s`), and that session judged it already knew what %s would "
            "want. The worker is being relaunched with this answer; nobody applied `agent-ready`.\n\n"
            "**The question:**\n\n%s\n\n"
            "**The approving session's answer:**\n\n%s\n\n"
            "**Its reasoning:**\n\n%s\n\n"
            "_%s: if this answer is wrong, comment here or park the issue — a session launched "
            "after your comment reads it as a binding amendment._"
            % (MARKER, operator, operator, approver_session, operator, (question or "").strip(),
               outcome.answer.strip(), reasoning, operator))


def escalation_note(outcome, approver_session, operator):
    """What an ESCALATE adds to the question the owner gets: why it is his, and the recommendation."""
    reasoning = (outcome.reasoning or "").strip() or "_(no reasoning given)_"
    rec = (outcome.recommendation or "").strip() or "_(no recommendation given)_"
    return ("**The approving session was consulted first, and escalated this to %s.** A fork of "
            "the session that approved this issue (`%s`) judged this a decision only %s can "
            "make.\n\n"
            "_Why it is %s's call:_\n\n%s\n\n"
            "_Its recommendation:_\n\n%s"
            % (operator, approver_session, operator, operator, reasoning, rec))


# =========================================================================== the handbacks (#539)
#
# Owner ruling 2026-11-04: "parks and freezes too, anything in the dash. The freeze responder doesn't
# have context on what it's working on and why, so I would prefer the resume link." Everything the
# command center puts in front of the owner about ONE issue goes first to the approving session's
# fork — the same `c<N>` launch, packet discipline, outcome file and fallbacks as a question. What
# differs is what the fork may decide: one MOVE from a small set the runner can already carry out
# with the engine's existing verbs. Machine-level faults that belong to no single issue (a stalled
# or dead runner, a GitHub or usage outage) stay with the watchdog and its repair session: a consult
# is launched only where there is an issue and an approver.

PARK = "park"                    # any park, whatever its cause (incl. #513's progress-stall parks)
BOUNCE = "bounce"                # a worker's Step-0 BOUNCED: memo
FROZEN = "frozen"                # a lane latched frozen that its first context-free nudge did not wake
AT_DIALOG = "at_dialog"          # a lane sitting at its own in-window dialog past the bound (#151)
HOST_BLOCKED = "host_blocked"    # a lane the session host reports waiting on a person (#520)
HANDBACK_KINDS = (PARK, BOUNCE, FROZEN, AT_DIALOG, HOST_BLOCKED)

# The plain words a comment, a packet and the morning report use for each kind.
KIND_WORDS = {
    PARK: "a park",
    BOUNCE: "a bounce (its worker rejected the issue's premise at Step 0)",
    FROZEN: "a frozen lane",
    AT_DIALOG: "a lane sitting at its own dialog",
    HOST_BLOCKED: "a lane the session host reports blocked on a person",
}

# The move set. Each is something the runner ALREADY does with an existing verb:
#   RELAUNCH      a fresh session on the issue, through phase E like any launch (the launcher), with
#                 an optional clarification the brief embeds — the #538 answer-relaunch path;
#   RESUME        re-enter the interrupted session's conversation (`superlooper resume`), lane-bound;
#   END_RELAUNCH  end a stuck session (the one ordered teardown) and relaunch fresh;
#   WAIT          a bounded hold with its reason (the handback is held, then re-derived);
#   FOLLOW_UP     file a follow-up issue, unapproved and `needs-owner` (gh issue create); the handback
#                 itself then reaches the owner exactly as today, naming the follow-up;
#   ESCALATE      the handback reaches the owner exactly as today, with the fork's reasoning and
#                 recommendation.
RELAUNCH = "relaunch"
RESUME = "resume"
END_RELAUNCH = "end_and_relaunch"
WAIT = "wait"
FOLLOW_UP = "follow_up"
MOVES = (RELAUNCH, RESUME, END_RELAUNCH, WAIT, FOLLOW_UP, ESCALATE)
_MOVE_WORDS = {"RELAUNCH": RELAUNCH, "RESUME": RESUME, "END_AND_RELAUNCH": END_RELAUNCH,
               "WAIT": WAIT, "FOLLOW_UP": FOLLOW_UP, "ESCALATE": ESCALATE}

# What the set must NEVER include (#539 DoD). The parser needs no list to refuse these — anything
# that is not one of the six words is refused — so this tuple exists for the brief, the standing
# rule and the test that pins each one is refused by name. The D13 rule stands: only the owner's
# direct word in a supervised session may hand-merge.
NEVER_MOVES = ("a hand-merge", "a force-push", "applying `agent-ready`", "a `pre-authorized:` grant",
               "an engine publish", "a `.superlooper/**` edit", "keystrokes into a worker's pane")

# A WAIT is bounded: a hold the fork asks for longer than this is clamped to it, so no handback can
# be parked out of the owner's sight for more than two hours by one decision. A re-derived handback
# past the hold is a NEW handback, and counts toward the cap.
WAIT_MAX_MINUTES = 120

# A third handback on the same issue goes to the owner regardless (#539 DoD); config
# `session.handback_cap` changes it. It counts the handback consults since the owner's approval —
# a worker's questions keep their own two-question cap (#163). 0 or 1 sends every handback straight
# to the owner, exactly as today.
HANDBACK_CAP_DEFAULT = 3

HELD_NOW = "consult_held_now"
HANDBACK_CAP = "handback_cap"
REFUSED_MOVE = "refused_move"

# A fifth parse state, beside ABSENT/INCOMPLETE/UNREADABLE/READ: a complete file whose MOVE is not
# one of the six. It is never a decision; the runner hands the handback to the owner naming it.
REFUSED = "refused"

# The follow-up's provenance label. '(runner-managed)' in its labels.py description is what lets the
# #160 boot migration create it on a repo adopted before #539 — `gh issue create` refuses OUTRIGHT
# for a label the repo lacks (the #165/#337 class).
FOLLOW_UP_SOURCE = "source:consult"
FOLLOW_UP_TYPES = ("type:build", "type:investigate", "type:diagnose-and-fix")

HandbackOutcome = collections.namedtuple(
    "HandbackOutcome", ["move", "clarification", "reasoning", "recommendation", "minutes", "title",
                        "body", "issue_type"])

_HB_MOVE_RE = re.compile(r"^MOVE:\s*(.*?)\s*$", re.IGNORECASE)
_HB_SECTION_RE = re.compile(
    r"^(REASONING|RECOMMENDATION|CLARIFICATION|MINUTES|TITLE|TYPE|BODY):\s*(.*)$")
_SINGLE_LINE = ("MINUTES", "TITLE", "TYPE")


def _move_of(word):
    """The move a MOVE: value names, or None. Spelling-tolerant (`end-and-relaunch`, `End and
    relaunch`) but never fuzzy: the words are matched whole after normalizing separators."""
    key = re.sub(r"[\s\-]+", "_", word.strip()).upper()
    return _MOVE_WORDS.get(key)


def parse_handback_outcome(text):
    """(state, HandbackOutcome-or-None, refused-word-or-None) for a handback consult's outcome file.

    The same discipline as `parse_outcome`: line-based, ending in a bare ``END`` (without it the
    file is INCOMPLETE, a fork mid-write); the first non-blank line must be ``MOVE: <word>``. A word
    outside the six is REFUSED, carrying the word so the owner is told exactly what was asked for.
    A move the runner ACTS ON needs its REASONING (the accountability record) and its own fields —
    a WAIT its minutes, a FOLLOW_UP its title and body — or the file is UNREADABLE: the runner never
    carries out half a decision. ESCALATE needs nothing; escalating is the safe direction.

    Sections are recognised once each, at the start of a line; a later line that merely starts with
    a section word belongs to the section above it, so a follow-up BODY may quote anything."""
    if not isinstance(text, str):
        return ABSENT, None, None
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1].strip() != END_LINE:
        return INCOMPLETE, None, None
    body = lines[:-1]
    while body and not body[0].strip():
        body.pop(0)
    head = _HB_MOVE_RE.match(body[0].strip()) if body else None
    if not head:
        return UNREADABLE, None, None
    word = head.group(1)
    move = _move_of(word)
    if move is None:
        return REFUSED, None, " ".join(word.split())[:120] or "(blank)"
    sections, current = {}, None
    for ln in body[1:]:
        m = _HB_SECTION_RE.match(ln.strip())
        if m and m.group(1) not in sections:
            current = m.group(1)
            sections[current] = [m.group(2)] if m.group(2).strip() else []
            continue
        if current is not None:
            sections[current].append(ln)

    def _get(key):
        val = "\n".join(sections.get(key, [])).strip()
        if key in _SINGLE_LINE:
            val = val.splitlines()[0].strip() if val else ""
        return val

    reasoning = _get("REASONING")
    if move != ESCALATE and not reasoning:
        return UNREADABLE, None, None
    minutes = None
    if move == WAIT:
        raw = _get("MINUTES")
        if not re.fullmatch(r"[0-9]+", raw or "") or int(raw) <= 0:
            return UNREADABLE, None, None
        minutes = min(int(raw), WAIT_MAX_MINUTES)
    title = _get("TITLE")
    fu_body = _get("BODY")
    issue_type = ""
    if move == FOLLOW_UP:
        if not title or not fu_body:
            return UNREADABLE, None, None
        t = _get("TYPE").lower()
        t = t if t.startswith("type:") else "type:" + t if t else ""
        issue_type = t if t in FOLLOW_UP_TYPES else "type:build"
    return READ, HandbackOutcome(move, _get("CLARIFICATION"), reasoning, _get("RECOMMENDATION"),
                                 minutes, title, fu_body, issue_type), None


def handback_outcome_format():
    """The handback outcome-file contract, exactly as the brief hands it to the fork."""
    return ("Write EXACTLY one of these six shapes, ending with a line that says only `END`. The "
            "first line names your ONE move; nothing else is a move.\n\n"
            "```\nMOVE: RELAUNCH\nREASONING:\n<why a fresh session is what the owner would want>\n"
            "CLARIFICATION:\n<optional: what the fresh session's brief should add, binding for "
            "that build>\nEND\n```\n\n"
            "```\nMOVE: RESUME\nREASONING:\n<why re-entering the interrupted session's own "
            "conversation is right>\nCLARIFICATION:\n<optional: an instruction placed after its "
            "re-orientation>\nEND\n```\n\n"
            "```\nMOVE: END_AND_RELAUNCH\nREASONING:\n<why the stuck session should be ended and a "
            "fresh one started>\nCLARIFICATION:\n<optional: what the fresh session's brief should "
            "add>\nEND\n```\n\n"
            "```\nMOVE: WAIT\nREASONING:\n<what is worth waiting for>\nMINUTES: <a whole number, "
            "at most %d>\nEND\n```\n\n"
            "```\nMOVE: FOLLOW_UP\nREASONING:\n<why this belongs in a separate issue>\nTITLE: <one "
            "line>\nTYPE: <build | investigate | diagnose-and-fix>\nBODY:\n<the follow-up issue's "
            "Goal, Definition of done and Boundaries>\nEND\n```\n\n"
            "```\nMOVE: ESCALATE\nREASONING:\n<why this is the owner's call>\nRECOMMENDATION:\n<the "
            "option you would take, and why>\nEND\n```\n\n"
            "A FOLLOW_UP is filed UNAPPROVED (`needs-owner`), and the handback itself still reaches "
            "the owner, naming it. Never a move, whatever you write: %s. A MOVE the runner does not "
            "recognise is refused and handed to the owner, naming what you asked for.\n\n"
            "Write it to a temporary file and move it into place, so the runner never reads half of "
            "it. Without the final `END` line the runner treats the file as still being written."
            % (WAIT_MAX_MINUTES, "; ".join(NEVER_MOVES)))


def handback_cap(config):
    """`session.handback_cap` from the repo's config, or the default (3). Fails to the default on
    anything but a non-negative int — config.load validates it, and this stays total anyway."""
    sess = config.get("session") if isinstance(config, dict) else None
    v = sess.get("handback_cap") if isinstance(sess, dict) else None
    return v if type(v) is int and v >= 0 else HANDBACK_CAP_DEFAULT


def handback_key(kind, episode):
    """The identity of ONE handback episode: its kind plus what marks the episode — a park's cause,
    a bounce's memo, the clock a freeze or a dialog started on. A park's cause can be a whole memo
    (a gate park), so text is digested to keep the key short and the state file readable."""
    if isinstance(episode, str):
        episode = hashlib.sha1(episode.encode("utf-8", "replace")).hexdigest()[:12]
    elif isinstance(episode, float) and episode == int(episode):
        episode = int(episode)
    return "%s:%s" % (kind, episode)


# --------------------------------------------------------------------------- the handback packet

def render_handback_packet(template, *, cid, num, title, body, kind, what, comments, owner,
                           operator, repo_slug, dev_branch, branch_state, history, qa,
                           outcome_path, home, approver_session, bright_line=False,
                           comments_ok=True, handback_number=1, cap=HANDBACK_CAP_DEFAULT):
    """The handback consult's whole brief: the handback itself, then everything a question's packet
    carries (the issue as approved, its thread, the branch/PR now, the whole handback history)."""
    hist = list(history or []) + list(qa or [])
    if bright_line:
        bright = ("**This park is on a bright line** — the diff reaches a referee path "
                  "(`.superlooper/**` or `.github/workflows/**`). It reaches %s whatever you "
                  "decide: write the move you would make anyway, and your reasoning and "
                  "recommendation go to %s with it." % (operator, operator))
    else:
        bright = "_Not a bright line: the move you choose is the one the runner carries out._"
    if type(cap) is int and handback_number + 1 >= cap:
        cap_line = ("This is handback %d on this issue. The next one goes to %s regardless, with the "
                    "consult history attached (cap %d)." % (handback_number, operator, cap))
    else:
        cap_line = ("This is handback %d on this issue; the %s goes to %s regardless, with the "
                    "consult history attached." % (handback_number, _ordinal(cap), operator))
    return _sub(template, {
        "cid": cid, "issue_num": str(num), "title": title or "", "repo_slug": repo_slug or "",
        "dev_branch": dev_branch or "main", "operator": operator,
        "approver_session": approver_session, "home": home,
        "kind_words": KIND_WORDS.get(kind, "a handback"),
        "what_happened": (what or "").strip() or "_(no memo recorded)_",
        "bright_line": bright, "cap_line": cap_line,
        "issue_body": (body or "").strip() or "_(empty body)_",
        "comments": _comment_block(comments, owner, operator, ok=comments_ok, num=num),
        "branch_state": "\n".join(branch_state or []) or "_(nothing recorded)_",
        "history": "\n".join(hist) if hist else "_None — this is the issue's first handback._",
        "outcome_path": outcome_path, "outcome_format": handback_outcome_format(),
        "rule_path": RULE_PATH,
        "timeout_minutes": str(TIMEOUT_SECONDS // 60),
    })


def _ordinal(n):
    if type(n) is not int or n <= 0:
        return "next"
    return {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(n, "%dth" % n)


# --------------------------------------------------------------------------- what the runner posts

MOVE_SENTENCES = {
    RELAUNCH: "relaunch the issue in a fresh session",
    RESUME: "resume the interrupted session's own conversation (`superlooper resume`)",
    END_RELAUNCH: "end the stuck session and relaunch the issue in a fresh one",
    WAIT: "wait",
    FOLLOW_UP: "file a follow-up issue",
}


def move_comment(kind, what, outcome, approver_session, operator):
    """The comment the runner posts when it carries out a move: unmistakably the approving
    session's decision, never the owner's word, marked so no later brief promotes it to an
    amendment and no answer ingestion reads it as the owner answering."""
    move = MOVE_SENTENCES.get(outcome.move, outcome.move)
    if outcome.move == WAIT and outcome.minutes:
        move = "wait %d minutes before this handback reaches %s" % (outcome.minutes, operator)
    clar = (outcome.clarification or "").strip()
    reasoning = (outcome.reasoning or "").strip() or "_(no reasoning given)_"
    return ("%s\n"
            "**Handled by the approving session, not by %s.** The loop was about to put %s on this "
            "issue in front of %s:\n\n> %s\n\n"
            "Before it reached %s, the loop consulted a fork of the session that approved this "
            "issue (`%s`). That session judged it already knew what %s would want, and chose to "
            "**%s** — nobody applied `agent-ready`.\n\n"
            "**Its reasoning:**\n\n%s\n\n%s"
            "_%s: if this was wrong, comment here or park the issue — a session launched after "
            "your comment reads it as a binding amendment._"
            % (MARKER, operator, KIND_WORDS.get(kind, "a handback"), operator,
               _clip(what, 500) or "(no memo recorded)", operator, approver_session, operator,
               move, reasoning,
               ("**Its clarification for the next session:**\n\n%s\n\n" % clar) if clar else "",
               operator))


# Fallback reasons whose handback reaches the owner WITH a note: the consult decided something he
# must see (a refused move), or the cap sent it with the history. Every other fallback — no stamp,
# a fork that never decided — reaches him EXACTLY as today, the reason journaled and nothing new
# posted, which is what "behaves exactly as it does today" means for them.
_NOTE_REASONS = (REFUSED_MOVE, HANDBACK_CAP)


def note_posts(via):
    """Does this delivery post a consult note on the issue? The cap posts one only with consults
    on record to attach — a cap of 0 or 1 reached with none is today's handback, nothing added."""
    if not isinstance(via, dict):
        return False
    if via.get("reason") == HANDBACK_CAP and via.get("consults") == 0:
        return False
    return via.get("outcome") == "escalated" or via.get("reason") in _NOTE_REASONS


def handback_note(via, operator, history=None):
    """The marked comment that rides a handback to the owner when the consult escalated it, asked
    for a refused move, filed a follow-up, or the cap sent it regardless. Posted as its OWN comment
    (never appended to the park memo, which carries no machine marker): the approving session's
    words must never reach a later brief as the owner's binding amendment."""
    via = via if isinstance(via, dict) else {}
    kind = KIND_WORDS.get(via.get("kind"), "a handback")
    session = via.get("session") or "unknown"
    reasoning = (via.get("reasoning") or "").strip() or "_(no reasoning given)_"
    rec = (via.get("recommendation") or "").strip() or "_(no recommendation given)_"
    if via.get("reason") == HANDBACK_CAP:
        hist = "\n".join(history or []) or "_(no earlier consult on record)_"
        return ("%s\n**This handback goes to %s regardless: the issue reached its handback cap "
                "(%s).** The approving session was consulted on the earlier ones; its history on "
                "this issue:\n\n%s"
                % (MARKER, operator, via.get("cap", HANDBACK_CAP_DEFAULT), hist))
    if via.get("reason") == REFUSED_MOVE:
        return ("%s\n**The approving session was consulted first, and asked for a move no consult "
                "may make:** `%s`. The runner refused it, and %s on this issue goes to %s as it "
                "always did. A consult picks one of: relaunch, resume, end and relaunch, wait, file "
                "a follow-up, escalate — never %s."
                % (MARKER, _clip(via.get("detail"), 120) or "(blank)", kind, operator,
                   "; ".join(NEVER_MOVES)))
    if via.get("forced") == "bright_line":
        return ("%s\n**A park on a bright line always reaches %s.** The diff reaches a referee path, so "
                "whatever the approving session (`%s`) decided, the runner escalates. It proposed "
                "to **%s**.\n\n_Its reasoning:_\n\n%s\n\n_Its proposal, as a recommendation:_\n\n%s"
                % (MARKER, operator, session,
                   MOVE_SENTENCES.get(via.get("proposed"), via.get("proposed") or "escalate"),
                   reasoning, rec))
    if via.get("move") == FOLLOW_UP:
        fu = via.get("follow_up")
        filed = ("filed follow-up **#%s** (unapproved, `needs-owner`)" % fu if fu
                 else "tried to file a follow-up, but GitHub refused the create — its proposal is "
                      "below")
        extra = "" if fu else "\n\n_The proposed follow-up:_ **%s**\n\n%s" % (
            _clip(via.get("title"), 200), (via.get("body") or "").strip())
        return ("%s\n**The approving session was consulted first, and %s.** %s on this issue "
                "still reaches %s.\n\n_Its reasoning:_\n\n%s%s"
                % (MARKER, filed, kind[0].upper() + kind[1:], operator, reasoning, extra))
    return ("%s\n**The approving session was consulted first, and escalated this to %s.** A fork "
            "of the session that approved this issue (`%s`) judged %s a decision only %s can "
            "make.\n\n_Why it is %s's call:_\n\n%s\n\n_Its recommendation:_\n\n%s"
            % (MARKER, operator, session, kind, operator, operator, reasoning, rec))


def follow_up_body(body, num, approver_session, touches):
    """The follow-up issue's body: the fork's text, its provenance, and the `## Loop metadata`
    section every loop issue carries — the fork's own if it wrote one, else the original issue's
    declared areas (the repo's issue-create rule needs one)."""
    text = (body or "").rstrip()
    prov = ("\n\n_Filed by a consult of the session that approved #%s (`%s`), while handling a "
            "handback on that issue. Unapproved: it needs %s._"
            % (num, approver_session, "the owner's word before the loop builds it"))
    if re.search(r"^## Loop metadata\s*$", text, re.M):
        return text + prov + "\n"
    areas = [t for t in (touches or []) if isinstance(t, str) and t.strip()]
    meta = "\n\n## Loop metadata\ntouches: %s\n" % (", ".join(areas) if areas else "*")
    return text + prov + meta


def follow_up_labels(issue_type):
    """The labels a consult-filed follow-up carries: unapproved (`needs-owner`), one type, its
    provenance. Never `agent-ready` or a `pre-authorized:` grant, whatever the fork wrote."""
    t = issue_type if issue_type in FOLLOW_UP_TYPES else "type:build"
    return ["needs-owner", t, FOLLOW_UP_SOURCE]


def _handback_history_line(when, r):
    kind = KIND_WORDS.get(r.get("handback"), "a handback")
    oc = r.get("outcome_kind")
    if oc == "moved":
        move = r.get("move")
        word = MOVE_SENTENCES.get(move, move or "act")
        if move == WAIT and r.get("minutes"):
            word = "wait %s minutes" % r.get("minutes")
        return "%s — %s went to a consult of the approving session, which chose to %s: %s" % (
            when, kind, word, _clip(r.get("reasoning")))
    if oc == "escalated":
        tail = " (filed follow-up #%s)" % r["follow_up"] if r.get("follow_up") else ""
        return "%s — %s went to a consult, which escalated it to the owner%s: %s" % (
            when, kind, tail, _clip(r.get("reasoning")))
    return "%s — %s went to the owner: %s" % (when, kind, fallback_sentence(r.get("reason")))


__all__ = ["ID_RE", "PREFIX", "consult_id", "is_consult_id", "issue_id_of", "TIMEOUT_SECONDS",
           "NO_STAMP", "STAMP_UNREAD", "OTHER_MACHINE", "NO_TRANSCRIPT", "LAUNCH_FAILED", "FORK_EXITED", "TIMEOUT",
           "MALFORMED", "HELD", "FALLBACK_REASONS", "fallback_sentence", "same_machine",
           "ANSWER", "ESCALATE", "Outcome", "ABSENT", "INCOMPLETE", "UNREADABLE", "READ",
           "END_LINE", "parse_outcome", "outcome_format", "HISTORY_ACTS", "history_lines",
           "qa_lines", "RULE_PATH", "render_packet", "MARKER", "answer_comment", "escalation_note",
           # the handbacks (#539)
           "PARK", "BOUNCE", "FROZEN", "AT_DIALOG", "HOST_BLOCKED", "HANDBACK_KINDS", "KIND_WORDS",
           "RELAUNCH", "RESUME", "END_RELAUNCH", "WAIT", "FOLLOW_UP", "MOVES", "NEVER_MOVES",
           "WAIT_MAX_MINUTES", "HANDBACK_CAP_DEFAULT", "HELD_NOW", "HANDBACK_CAP", "REFUSED_MOVE",
           "REFUSED", "FOLLOW_UP_SOURCE", "FOLLOW_UP_TYPES", "HandbackOutcome",
           "parse_handback_outcome", "handback_outcome_format", "handback_cap", "handback_key",
           "render_handback_packet", "MOVE_SENTENCES", "move_comment", "note_posts",
           "handback_note", "follow_up_body", "follow_up_labels"]
