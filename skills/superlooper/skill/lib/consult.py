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
        return "%s — parked for the owner (%s): %s" % (when, r.get("cause") or "no cause",
                                                        _clip(r.get("memo")))
    if act == "bounce":
        return "%s — bounced by its worker (premise drift): %s" % (when, _clip(r.get("memo")))
    if act == "reapprove":
        return "%s — the owner re-approved it; the loop re-released it" % when
    if act == "resume_at_gate":
        return "%s — the owner re-approved a finished build; it resumed at the merge gate" % when
    if act == "regenerate":
        return "%s — regenerated from the issue (merge conflict): %s" % (when,
                                                                        _clip(r.get("reason")))
    if act == "recover":
        return "%s — recovered a %s session" % (when, r.get("tier"))
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

# Where the consult reads its standing rule: in the approving checkout (this monorepo's own path),
# else the copy the gated installer mirrors with the ops docs (`ops_docs.OPS_DOCS`, which
# `tests/test_ops_docs.py` holds to the whole references directory).
RULE_PATH = "plugin/skills/superlooper/references/consult-standing-rule.md"
RULE_MIRROR = "~/.claude/skills/superlooper/docs/ops/superlooper/references/consult-standing-rule.md"

def _sub(template, mapping):
    """ONE-pass ``{name}`` substitution. A value is placed verbatim and never re-scanned, so a
    ``{placeholder}`` inside the issue body or a quoted question stays literal — the discipline the
    worker brief keeps for the approved body. Unknown names are left as written."""
    return re.sub(r"\{([a-z_]+)\}",
                  lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0),
                  template)


def _comment_block(comments, owner, operator):
    """The issue's comment thread, every author, VERBATIM — context for the consult, attributed so
    it can tell the owner's word from the loop's own machine comments."""
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
                  approver_session, rule_paths=(RULE_PATH, RULE_MIRROR)):
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
        "comments": _comment_block(comments, owner, operator),
        "branch_state": "\n".join(branch_state or []) or "_(nothing recorded)_",
        "history": "\n".join(hist) if hist else "_None — this is the issue's first handback._",
        "outcome_path": outcome_path, "outcome_format": outcome_format(),
        "rule_path": rule_paths[0], "rule_mirror": rule_paths[1],
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


__all__ = ["ID_RE", "PREFIX", "consult_id", "is_consult_id", "issue_id_of", "TIMEOUT_SECONDS",
           "NO_STAMP", "STAMP_UNREAD", "OTHER_MACHINE", "NO_TRANSCRIPT", "LAUNCH_FAILED", "FORK_EXITED", "TIMEOUT",
           "MALFORMED", "FALLBACK_REASONS", "fallback_sentence", "same_machine",
           "ANSWER", "ESCALATE", "Outcome", "ABSENT", "INCOMPLETE", "UNREADABLE", "READ",
           "END_LINE", "parse_outcome", "outcome_format", "HISTORY_ACTS", "history_lines",
           "qa_lines", "RULE_PATH", "RULE_MIRROR", "render_packet", "MARKER", "answer_comment", "escalation_note"]
