"""A lane's CURRENT PHASE (issue #443) — the missing mid-session sense, as a pure function.

THE PROBLEM. The engine advances a lane's position on journal LANDMARKS: it launched, its report
appeared, its PR opened, it merged. A worker builds, cross-reviews its diff, pushes, opens the PR
and files its report inside ONE session — and that whole stretch emits no landmark at all. So a
lane reads "building" for essentially its entire life and then flicks through every remaining
position in a tick or two (owner observation, 2026-08-20). That is a missing SENSE, not a missing
renderer.

THE SENSE. The one long sub-step the engine can honestly see is the cross-review, because it runs
through an ENGINE-OWNED script (``skill/bin/cross-review.sh``). The script stamps a breadcrumb into
the lane's state when the review starts and again when it ends, so the signal depends on engine code
and never on a worker remembering to announce anything. By doctrine the engine never reads screens;
a file is written and the supervisor reads the file, exactly as ``state/review_pin`` (written by
that same script) and ``state/exited`` already do. No screens, no new GitHub reads.

FAIL-SOFT IS THE WHOLE CONTRACT. A phase is a LABEL ON A BOARD. Nothing here holds a launch, raises
an alert, or reaches any decision path — ``actions.decide`` never sees it. Every unreadable input
(absent, half-written, hand-edited, wrong-typed, clock-skewed, expired) therefore resolves DOWN to
``building`` rather than raising or inventing: the worst a wrong answer can cost is a lane that
reads "building" while it is really reviewing, which is precisely the world this replaces. Nothing
in this module can raise, and nothing in it takes a clock of its own — the caller passes ``now``, so
the whole contract is testable without a state home (tests/test_phase.py).
"""

# The published vocabulary. CLOSED on purpose: the dashboard (a separate issue) renders this field,
# so a new value must move a test here before it can appear in the document.
BUILDING = "building"                  # the floor — working, or nothing legible enough to say more
CROSS_REVIEWING = "cross-reviewing"    # a review is running RIGHT NOW (the breadcrumb is open)
REPORT_POSTED = "report-posted"        # the report is filed — the worker's LAST contractual action
PR_OPEN = "pr-open"                    # a PR is open and the report is not filed yet
PHASES = (BUILDING, CROSS_REVIEWING, REPORT_POSTED, PR_OPEN)

# The state-home subdirectory the breadcrumb lives in: ``<home>/state/phase/<iid>``. One file per
# lane, holding the LATEST stamp only — the durable history of a review is ``state/review_pin`` plus
# the worker's transcript, and this file is re-read every tick by the runner, so it stays one line.
BREADCRUMB_DIR = "phase"

# The two events the script stamps. `start` is written immediately before the reviewer is invoked;
# `end` from an EXIT trap, so it lands whether the review succeeded, failed, or was interrupted.
START = "start"
END = "end"

# How long an OPEN stamp may stand before it stops being believed. A review is minutes of work (the
# cross-review skill budgets a 5-minute timeout and the config pins a bounded reasoning tier), so
# half an hour is far past any real one — while staying well under the freeze threshold
# (events.FREEZE_SECONDS = 2700), so a lane whose review died silently reads "building" again before
# the recovery ladder is even interested in it. This is the ONLY backstop a file can carry against
# a session killed hard enough to skip its own EXIT trap (SIGKILL, a power cut, a full disk).
STALE_SECONDS = 1800

# How far AHEAD of the reading clock a stamp may sit and still be believed. The worker that writes
# it and the runner that reads it share one machine's clock, so a stamp meaningfully in the future is
# a corrupt or hand-edited value rather than a review that started — and believing one would pin the
# lane at "cross-reviewing" until the clock caught up. A few minutes of slack absorbs the ordinary
# case (the stamp written a moment before a tick whose `now` was captured earlier).
FUTURE_SKEW_SECONDS = 300

# The per-issue loopstate statuses that mean "this lane is in the air", and therefore the only ones
# that get a phase at all. Mirrors loopstate.VALID: `ready` has never launched, and `merged`,
# `parked`, `bounced`, `needs_william` and `awaiting_answer` are all finished with the loop for now
# — publishing a phase for any of them would fabricate a live worker where there is none. `holding`
# stays IN: the work is done but the lane is still sequenced behind another, and its phase (the
# report it filed) is exactly what a reader wants to see while it waits.
IN_FLIGHT_STATUSES = frozenset({"running", "exited", "frozen", "gating", "holding"})


def in_flight(status):
    """Whether a loopstate ``status`` describes a lane that should carry a phase at all. Total: a
    wrong-typed or unknown status answers False, so a corrupt state file can never fabricate a lane
    in the air."""
    return isinstance(status, str) and status in IN_FLIGHT_STATUSES


def parse(text):
    """One breadcrumb line -> ``{"at": int, "phase": str, "event": str}``, or ``None``.

    The format is the state home's own house style — a leading epoch then ``key=value`` fields, the
    shape ``state/exited`` (``<epoch> rc=<n>``) and ``state/review_pin`` already use:

        1755712345 phase=cross-reviewing event=start
        1755712402 phase=cross-reviewing event=end rc=0

    Only the first line is read (a stamp is one line; anything after it is corruption) and unknown
    fields are ignored, so the format can grow without breaking an older reader. ANY input that is
    not exactly this shape — wrong type, no clock, a phase outside the published vocabulary, an
    event that is neither start nor end — answers None. None is the fail-soft signal the whole
    module rests on, never an error."""
    if not isinstance(text, str):
        return None                    # bytes, None, an int, a dict — all "no breadcrumb"
    line = text.strip().split("\n", 1)[0].strip()
    if not line:
        return None
    parts = line.split()
    try:
        at = int(parts[0])
    except ValueError:
        return None                    # no leading clock -> not our format, and never a raise
    fields = {}
    for p in parts[1:]:
        k, sep, v = p.partition("=")
        if sep and k and k not in fields:
            fields[k] = v              # FIRST wins, so a duplicated key can't be used to override
    name, event = fields.get("phase"), fields.get("event")
    if name not in PHASES or event not in (START, END):
        return None
    return {"at": at, "phase": name, "event": event}


def _open_review(text, now):
    """Whether ``text`` is an OPEN and BELIEVABLE cross-review stamp as of ``now`` — i.e. a review
    that started, has not stamped its end, and is neither expired nor from the future. Total: a
    wrong-typed clock answers False rather than raising, because this runs inside the publish step
    and a publish must never wedge a tick."""
    rec = parse(text)
    if rec is None or rec["event"] != START or rec["phase"] != CROSS_REVIEWING:
        return False
    try:
        age = float(now) - rec["at"]
    except (TypeError, ValueError):
        return False
    return -FUTURE_SKEW_SECONDS <= age <= STALE_SECONDS


def derive(breadcrumb, now, report_present=False, pr_open=False):
    """The lane's phase from its breadcrumb plus the landmarks the engine already holds.

    ``breadcrumb``     the raw text of ``state/phase/<iid>``, or None when there is no such file.
    ``now``            the reading clock (the tick's), passed in so this stays pure.
    ``report_present`` ``<home>/reports/<iid>.md`` exists — the worker's LAST contractual action.
    ``pr_open``        the lane's PR is open in the runner's own GitHub view (no new read).

    Precedence, and why:

      1. A LIVE review wins over everything. It is the lane's current ACTIVITY; the landmarks below
         are achievements it already banked, and "what is this lane doing right now" is the entire
         question this field exists to answer. A second review round after a PR is open therefore
         reads ``cross-reviewing`` rather than ``pr-open`` — non-monotonic on purpose, and bounded
         by the staleness rule so it can never latch.
      2. ``report-posted`` over ``pr-open``, because the loop contract makes the report the LAST
         action a worker takes, after the PR is opened. (An investigation issue opens no PR at all
         and still files a report, which this orders correctly too.)
      3. ``building`` is the floor and the fail-soft answer: no breadcrumb, an expired or corrupt
         one, and no landmark yet all mean the same honest thing — it is working and the engine has
         nothing more specific to say."""
    if _open_review(breadcrumb, now):
        return CROSS_REVIEWING
    if report_present:
        return REPORT_POSTED
    if pr_open:
        return PR_OPEN
    return BUILDING
