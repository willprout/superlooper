"""The approval ceremony and the approver stamp (issue #537) — pure logic, no I/O but `read`.

Owner ruling (2026-11-04): when the loop needs to consult someone about an issue, it consults the
session that APPROVED it, not the one that wrote it. A follow-up filed by a build session may not
carry the project-wide context of what the owner wants; the orchestration session he approved it in
does. So an approving session leaves a signature on the issue — its session id, its machine and its
working directory — and the engine reads that signature back later.

Two halves live here:

* **The ceremony** — what `superlooper approve` writes. Approval used to be three steps a session
  performed by hand from `references/approval-protocol.md` (apply `agent-ready`; clear
  `needs-owner`/`needs-william`/`parked`; post the audit comment), and a step taught in prose gets
  skipped (the point-of-error principle, #215/#225). The verb does all three; this module says
  exactly what they are, so the verb and its tests cannot drift apart.

* **The stamp and its reader.** The stamp rides at the END of the audit comment as a hidden HTML
  comment on its own line — after the audit sentence, never before it, because the brief drops a
  comment that BEGINS with a machine marker (`brief._MARKER_PREFIX`) and the approval would then
  vanish from the worker's view of its own thread. The reader returns an issue's NEWEST valid stamp
  among comments the repo owner's account authored, by the same owner rule the brief's amendments
  use (`brief.owner_login`), and nothing else is evidence: a stamp in another account's comment,
  quoted mid-sentence, or malformed is no stamp.

**An unstamped approval never clears a stamp.** The dashboard's Approve button posts none (owner
ruling: an unstamped approval falls back to the owner), and neither does any approval that predates
this module. Neither says anything about WHICH session approved, so neither can overturn a stamp
that does.

**What the owner check can and cannot do.** Every loop session posts as the owner's own login (no
bot account, owner ruling 2026-08-05), so "authored by the owner" excludes other accounts and
nothing more. What keeps a loop session from approving at all is the verb's refusal below
(`launched_by_runner`) together with the worker hook's deny on the raw `agent-ready` write.

**The agent boundary.** Which variable carries a session's id is the agent's spelling, so it lives
in `identity.py` (`identity.own_session_id`); nothing here names it. The stamp itself is agent-
agnostic: an id, a host, a directory.
"""
import collections
import json
import re

import brief
import gh

# The approval label, and the hand-back labels an approval ends (owner instruction 2026-08-05: the
# removal is not optional housekeeping — `needs-owner` says "waiting for the owner's decision", which
# the approval just ended). `needs-william` is the #58 rename's retired spelling, still cleared so a
# repo mid-migration comes out clean. ORDER is the order they are reported and removed in.
AGENT_READY = "agent-ready"
CLEARED_BY_APPROVAL = ("needs-owner", "needs-william", "parked")

# The variables the launcher names in EVERY session it spawns (`launch.pane_environment`) — a build
# worker, a repair session, a triage flight, and any kind added later, because every spawn path goes
# through that one function. Any one of them, non-blank, means the runner launched this process, and
# `agent-ready` is then not this session's to apply: approval is the owner's word, relayed by a
# SUPERVISED session. Keyed on what the launcher names rather than on the id's shape, so a session
# kind that does not exist yet (a consult, #536-#539) is refused the day it ships.
LAUNCH_MARKERS = ("SL_ISSUE_ID", "SL_RUN_ROOT", "SL_START_TOKEN", "SL_SESSION_ID")

# The marker's name. Shares the `<!-- superlooper-` family prefix with the review and investigation
# markers, but is never at the START of a comment (see the module docstring).
MARKER = "superlooper-approver"
_STAMP_LINE = re.compile(r"^<!-- " + re.escape(MARKER) + r" (\{.*\}) -->[ \t\r]*$", re.M)

# An id-shaped token: what names a transcript file. The same shape `delivery._UUID_ISH` accepts,
# for the same reason — a value that is not one cannot become a path, a broken marker, or prose.
_SESSION_SHAPE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")

Stamp = collections.namedtuple("Stamp", ["session", "host", "cwd"])

# The engine's read: `stamp` is the newest valid stamp or None; `ok` is False when GitHub REFUSED
# the comment read, so "nobody stamped this" is never inferred from a read that never happened (the
# #21 refused-vs-answered-empty discipline).
ApproverRead = collections.namedtuple("ApproverRead", ["stamp", "ok"])


def launched_by_runner(env):
    """The first launch marker set in `env`, or None for a plain terminal or a supervised session.

    Returns the variable's NAME, never its value: `SL_START_TOKEN` is a per-launch secret, and a
    refusal message must not print it."""
    env = env if isinstance(env, dict) or hasattr(env, "get") else {}
    for var in LAUNCH_MARKERS:
        value = env.get(var)
        if isinstance(value, str) and value.strip():
            return var
    return None


def audit_sentence(operator, date):
    """The audit line every conversational approval has always carried."""
    return "Approved by %s in conversation, %s." % (operator, date)


def make_stamp(session, host, cwd):
    """A Stamp, or None when any part could not be read back by `parse_stamps`.

    The host keeps only its first label: `socket.gethostname()` answers `Williams-Mini.lan` on one
    network and `Williams-Mini.local` on another, and a consult routes on the machine, not on the
    network it happened to be on."""
    if not isinstance(session, str) or not _SESSION_SHAPE.match(session.strip()):
        return None
    if not isinstance(host, str) or not isinstance(cwd, str):
        return None
    host = host.strip().split(".", 1)[0]
    if not host or not cwd:
        return None
    return Stamp(session=session.strip(), host=host, cwd=cwd)


def render_stamp(stamp):
    """The hidden marker line for a stamp.

    JSON, with `<`, `>` and `&` escaped as `\\u003c`/`\\u003e`/`\\u0026` — still valid JSON that decodes
    to the same string, but a directory named `a-->b` can no longer end the HTML comment early and
    spill the rest of the marker onto the rendered page. `ensure_ascii` keeps it to one line."""
    payload = json.dumps({"session": stamp.session, "host": stamp.host, "cwd": stamp.cwd},
                         sort_keys=True, ensure_ascii=True)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "<!-- %s %s -->" % (MARKER, payload)


def audit_comment(operator, date, stamp=None):
    """The whole audit comment: the sentence, then — when there is a stamp — one line a human can
    read on the issue and the hidden marker the engine reads. With no stamp it is the bare sentence,
    exactly what an unstamped approval has always looked like."""
    text = audit_sentence(operator, date)
    if stamp is None:
        return text
    return "%s\n\nApproving session: `%s` on %s, cwd `%s`.\n\n%s" % (
        text, stamp.session, stamp.host, stamp.cwd, render_stamp(stamp))


def _from_payload(raw):
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    session, host, cwd = d.get("session"), d.get("host"), d.get("cwd")
    if not (isinstance(session, str) and _SESSION_SHAPE.match(session)):
        return None
    if not (isinstance(host, str) and host and isinstance(cwd, str) and cwd):
        return None
    return Stamp(session=session, host=host, cwd=cwd)


def parse_stamps(body):
    """Every valid stamp in one comment body, in order. A marker must stand on its own line — one
    quoted in backticks or mid-sentence is a comment ABOUT the format, not an approval."""
    if not isinstance(body, str):
        return []
    return [s for s in (_from_payload(m.group(1)) for m in _STAMP_LINE.finditer(body)) if s]


def latest_stamp(comments, owner):
    """The newest valid stamp in comments the repo owner's account authored, or None.

    `comments` is the thread as `gh issue view --json comments` serves it, oldest first (GitHub's
    order, and the order the brief renders). Fails closed throughout: a wrong-typed thread, entry,
    author or body is skipped; a login is compared EXACTLY against `owner`, as the brief compares it;
    with no owner, nothing is trusted. An unstamped or malformed comment is skipped rather than read
    as "no approver", so it can never clear an earlier stamp."""
    if not isinstance(comments, list) or not isinstance(owner, str) or not owner:
        return None
    found = None
    for c in comments:
        if not isinstance(c, dict):
            continue
        author = c.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not (isinstance(login, str) and login == owner):
            continue
        stamps = parse_stamps(c.get("body"))
        if stamps:
            found = stamps[-1]
    return found


def read(num, config, comments_reader=None):
    """An issue's current approver, as the engine asks for it: ApproverRead(stamp, ok).

    The owner is `brief.owner_login(config)` — ONE owner definition shared with the brief's
    amendments and the runner's answer ingestion. `comments_reader` defaults to
    `gh.issue_comments` and is injectable for tests; whatever it returns must carry the CommentRead
    shape (`comments`, `ok`)."""
    reader = comments_reader if comments_reader is not None else gh.issue_comments
    thread = reader(num)
    comments = getattr(thread, "comments", None)
    if not getattr(thread, "ok", False) or not isinstance(comments, list):
        return ApproverRead(stamp=None, ok=False)
    return ApproverRead(stamp=latest_stamp(comments, brief.owner_login(config)), ok=True)


def labels_to_clear(present):
    """The hand-back labels an approval removes: those of CLEARED_BY_APPROVAL the issue CARRIES.

    Not the whole tuple, deliberately. `gh issue edit` is all-or-nothing, and it hard-fails — the
    `agent-ready` add included — when a `--remove-label` names a label the REPO does not define, even
    one the issue never carried. A repo that finished the #58 rename no longer defines
    `needs-william`, so naming it unconditionally would kill every approval there (the dashboard paid
    for exactly this in #114). A label the issue carries is one the repo has."""
    have = set(present) if isinstance(present, (list, tuple, set, frozenset)) else set()
    return [name for name in CLEARED_BY_APPROVAL if name in have]
