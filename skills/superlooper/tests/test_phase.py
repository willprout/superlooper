"""The lane's CURRENT PHASE (issue #443) — the pure sense, with no disk and no clock of its own.

A worker builds, cross-reviews, and files its report inside ONE session. The engine advances a
lane's position on journal LANDMARKS only (launched -> report filed -> PR -> merged), and the whole
build-plus-review stretch emits none of them — so a lane reads "building" for essentially its entire
life and then flicks through every remaining position in a tick or two. The missing signal is the
CROSS-REVIEW, which is the one long sub-step the engine could always have seen: it runs through an
ENGINE-OWNED script (``skill/bin/cross-review.sh``), so the script stamps a breadcrumb into the
lane's state and nothing depends on a worker remembering to announce anything. By doctrine the
engine never reads screens; a file is written and the supervisor reads the file, exactly as
``state/review_pin`` and ``state/exited`` already do.

This module is that sense, and it is deliberately TOTAL and FAIL-SOFT. Every unreadable input —
absent, half-written, hand-edited, clock-skewed, wrong-typed — resolves to a phase rather than an
error, because a phase is a LABEL ON A BOARD and never an input to a decision. Nothing here holds a
launch, raises an alert, or changes what the gate does; the worst a wrong answer can cost is a lane
that reads "building" while it is actually reviewing, which is exactly what the loop showed before
this existed.
"""
import phase


# ------------------------------------- the vocabulary -------------------------------------

def test_the_four_phase_values_are_the_published_vocabulary():
    # The dashboard (a separate issue) renders this field; the set it binds to must be closed and
    # named here, so a new value can never appear in the document without this test moving.
    assert phase.PHASES == ("building", "cross-reviewing", "report-posted", "pr-open")
    assert phase.BUILDING == "building"
    assert phase.CROSS_REVIEWING == "cross-reviewing"
    assert phase.REPORT_POSTED == "report-posted"
    assert phase.PR_OPEN == "pr-open"


def test_only_lanes_actually_in_the_air_have_a_phase():
    # A phase answers "what is this lane DOING", so it exists only while a lane is doing something.
    # `ready` has never launched and every terminal status is finished — both must publish NOTHING
    # rather than a fabricated "building" a reader would take for a live worker.
    for status in ("running", "exited", "frozen", "gating", "holding"):
        assert phase.in_flight(status) is True, status
    for status in ("ready", "merged", "parked", "bounced", "needs_william", "awaiting_answer"):
        assert phase.in_flight(status) is False, status
    # A wrong-typed or unknown status is not in flight either — never invent a lane in the air.
    for junk in (None, 17, True, ["running"], "", "nonsense"):
        assert phase.in_flight(junk) is False, junk


# ------------------------------- the breadcrumb the script writes -------------------------------

def _crumb(at, event="start", name="cross-reviewing"):
    return "%s phase=%s event=%s\n" % (at, name, event)


def test_parses_the_breadcrumb_the_cross_review_script_writes():
    rec = phase.parse(_crumb(1000))
    assert rec == {"at": 1000, "phase": "cross-reviewing", "event": "start"}


def test_parses_an_end_stamp_carrying_the_reviews_exit_code():
    # The end stamp carries `rc=` as diagnosable evidence (the review_pin line's habit). Extra
    # fields must never break the parse — this format has to be able to grow.
    rec = phase.parse("1000 phase=cross-reviewing event=end rc=1\n")
    assert rec["event"] == "end" and rec["at"] == 1000


def test_a_malformed_breadcrumb_parses_as_nothing_rather_than_raising():
    for junk in (None, "", "   ", "not-a-clock phase=cross-reviewing event=start",
                 "1000", "1000 phase=cross-reviewing", "1000 event=start",
                 "1000 phase=inventing event=start", "1000 phase=cross-reviewing event=sideways",
                 b"1000 phase=cross-reviewing event=start", 1000, {"at": 1000},
                 "\x00\x01 garbage"):
        assert phase.parse(junk) is None, junk


# ------------------------------------ the derived phase ------------------------------------

def test_an_open_fresh_breadcrumb_reads_cross_reviewing():
    assert phase.derive(_crumb(1000), now=1030) == "cross-reviewing"


def test_a_closed_breadcrumb_stops_reading_cross_reviewing():
    # The whole point of stamping the END: the review is over, so the lane is back to building
    # (or further along, if a landmark has since appeared). A start-only stamp would pin every
    # reviewed lane at "cross-reviewing" for the rest of its flight.
    assert phase.derive(_crumb(1000, event="end"), now=1030) == "building"


def test_an_absent_breadcrumb_reads_building():
    assert phase.derive(None, now=1000) == "building"


def test_a_stale_open_breadcrumb_degrades_to_building():
    # A review that never wrote its end stamp (the session was SIGKILLed, the disk refused the
    # write) must not pin the lane at "cross-reviewing" forever. Age is the only backstop the file
    # itself can carry, so an open stamp expires.
    assert phase.derive(_crumb(1000), now=1000 + phase.STALE_SECONDS - 1) == "cross-reviewing"
    assert phase.derive(_crumb(1000), now=1000 + phase.STALE_SECONDS + 1) == "building"


def test_a_breadcrumb_stamped_in_the_future_degrades_to_building():
    # The worker and the runner share one machine's clock, so a stamp meaningfully AHEAD of the
    # tick is a corrupt or hand-edited value, not a review that started. Trusting it would pin the
    # lane at "cross-reviewing" until the clock caught up.
    assert phase.derive(_crumb(1000), now=1000 - 5) == "cross-reviewing"     # trivial skew is fine
    assert phase.derive(_crumb(1000), now=1000 - phase.FUTURE_SKEW_SECONDS - 1) == "building"


def test_a_malformed_breadcrumb_degrades_to_building_and_never_raises():
    for junk in ("garbage", "", None, b"bytes", 17, {"phase": "cross-reviewing"}):
        assert phase.derive(junk, now=1000) == "building", junk


def test_a_filed_report_reads_report_posted():
    assert phase.derive(None, now=1000, report_present=True) == "report-posted"


def test_an_open_pr_reads_pr_open():
    assert phase.derive(None, now=1000, pr_open=True) == "pr-open"


def test_the_report_is_the_later_landmark_so_it_wins_over_an_open_pr():
    # The loop contract makes the report the worker's LAST action, after the PR is opened — so a
    # lane holding both is further along than one holding only a PR.
    assert phase.derive(None, now=1000, report_present=True, pr_open=True) == "report-posted"


def test_a_live_review_wins_over_the_landmarks_it_already_passed():
    # A second review round after a PR is open is the lane's CURRENT activity; the landmarks are
    # achievements it already banked. Answering "what is it doing now" is this field's whole job,
    # and the answer is bounded by the staleness rule above.
    assert phase.derive(_crumb(1000), now=1010, pr_open=True) == "cross-reviewing"
    assert phase.derive(_crumb(1000), now=1010, report_present=True) == "cross-reviewing"


def test_a_wrong_typed_clock_never_raises():
    for now in (None, "soon", object()):
        assert phase.derive(_crumb(1000), now=now) == "building", now


def test_every_derived_answer_is_in_the_published_vocabulary():
    for crumb in (None, "garbage", _crumb(1000), _crumb(1000, event="end")):
        for rp in (True, False):
            for po in (True, False):
                assert phase.derive(crumb, now=1010, report_present=rp, pr_open=po) in phase.PHASES
