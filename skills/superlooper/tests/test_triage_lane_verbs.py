"""What every lane verb and every housekeeping reader ANSWERS for a `t<N>` triage flight (#463).

#448 made the flight a third session class the launcher spawns, beside the `i<N>` issue worker and
the `d<N>` debugger seat. The engine's operator verbs and its state readers decide what counts as a
lane from a shared id shape, and none of them was widened — so a running flight was a session the
operator had no verb to re-enter, bring forward or tidy, while the launcher recorded a pane handle
and a liveness stamp for it under the very directories those readers walk.

This file is the TABLE of the answers, one test per verb and per reader, so that "nobody revisited
the pattern" cannot happen again silently: a future edit that changes an answer has to change a
test that says what the answer was and why.

    focus-session   ACCEPTS   read-only, and the launcher records `state/panes/t<N>` for a flight
                              exactly as it does for a lane
    resume          REFUSES   with its own sentence — the one-a-day lease is consumed BEFORE the
                              session exists, so a dead flight waits for tomorrow by design
    tidy            ACCEPTS   a flight whose run is closed, or whose session is gone
    debug           n/a       it takes no id; it MINTS a `d<N>` from its own allocator
    janitor         SILENT    a flight opens no branch, no PR and no lane record
    report          SILENT    as a LANE — a flight is rendered by its triage acts (#451 owns that)
    published_view  SILENT    it is bounded by what loopstate tracks, and a flight is never in it
    actions         SPLIT     a flight IS a launch session (`_is_session_id`) and is NOT an issue

And in every case the widening adds a CLASS, it does not open the pattern: an id shape the engine
does not know is still refused.
"""
import re

import actions
import focus
import janitor
import panes
import published_view
import report
import tidy
import triage


# --------------------------- what a flight id IS ---------------------------

def test_triage_owns_the_one_spelling_of_the_flight_id_shape():
    assert triage.is_flight_id("t1") and triage.is_flight_id("t448")
    for junk in ("i1", "d1", "t", "T1", "t1x", "t-1", "t 1", "", None, 7, "t1/../etc"):
        assert not triage.is_flight_id(junk), junk


# --------------------------- focus-session: ACCEPTS ---------------------------

def test_focus_session_accepts_a_flight_id(tmp_path):
    """A flight with a recorded window resolves like any lane — the whole point: the launcher
    writes state/panes/t<N> for it."""
    state = tmp_path / "state"
    (state / "panes").mkdir(parents=True)
    (state / "panes" / "t7").write_text("ws1:pane1")
    (state / "panes" / "t7.ws").write_text("ws1")

    class Host:
        def focus(self, session):
            assert session.name == "t7"
            return type("R", (), {"outcome": focus.FOCUSED, "detail": "raised"})()

    got = focus.focus_lane(str(tmp_path), "t7", host=Host())
    assert got.outcome == focus.FOCUSED
    assert got.workspace == "ws1"


def test_focus_session_answers_no_window_for_a_flight_that_has_none(tmp_path):
    (tmp_path / "state" / "panes").mkdir(parents=True)
    got = focus.focus_lane(str(tmp_path), "t7")
    assert got.outcome == focus.NO_WINDOW


def test_focus_session_still_refuses_an_unknown_id_shape(tmp_path):
    for junk in ("t", "x7", "7", "t7.ws", "../t7", "t7/../../etc", ""):
        got = focus.focus_lane(str(tmp_path), junk)
        assert got.outcome == focus.UNKNOWN_LANE, junk


def test_focus_session_names_the_flight_in_its_refusal_sentence(tmp_path):
    got = focus.focus_lane(str(tmp_path), "nonsense")
    assert "t<N>" in got.detail and "triage flight" in got.detail


# --------------------------- tidy: ACCEPTS ---------------------------

def test_tidy_closes_a_flight_whose_run_is_closed():
    got = tidy.closable_flights({"t7"}, finished={"t7"}, live=set())
    assert got == [{"id": "t7", "status": tidy.FLIGHT_DONE}]


def test_tidy_closes_a_finished_flight_even_while_its_session_still_idles():
    # D4: a claude session never self-exits, so a FINISHED flight sits at its prompt with a live
    # lock. That is precisely the window tidy exists to close.
    got = tidy.closable_flights({"t7"}, finished={"t7"}, live={"t7"})
    assert [g["id"] for g in got] == ["t7"]


def test_tidy_leaves_a_flight_that_is_still_flying():
    assert tidy.closable_flights({"t7"}, finished=set(), live={"t7"}) == []


def test_tidy_default_scope_holds_back_a_flight_that_merely_ended():
    # Its session died mid-run and it never closed its run: the park-family shape, so it needs
    # --all exactly as a parked lane does.
    assert tidy.closable_flights({"t7"}, finished=set(), live=set()) == []
    got = tidy.closable_flights({"t7"}, finished=set(), live=set(), scope_all=True)
    assert got == [{"id": "t7", "status": tidy.FLIGHT_ENDED}]


def test_tidy_never_selects_a_non_flight_id():
    # The widening adds a CLASS, it does not open the pattern.
    for junk in ("i7", "d7", "t", "T7", "t7x", "t7.ws", "", 7, None):
        assert tidy.closable_flights({junk}, finished={junk}, live=set(), scope_all=True) == [], junk


def test_tidy_flight_selection_is_sorted_by_flight_number():
    got = tidy.closable_flights({"t2", "t10", "t1"}, finished={"t1", "t2", "t10"}, live=set())
    assert [g["id"] for g in got] == ["t1", "t2", "t10"]


def test_tidy_flight_selection_fails_closed_on_wrong_typed_input():
    assert tidy.closable_flights("t7", finished={"t7"}, live=set()) == []
    assert tidy.closable_flights({"t7"}, finished="t7", live=set()) == []
    # `live` is the one that would fail OPEN if an unreadable view collapsed to empty: it would
    # read as "no flight is running" and offer a working flight's window for closing.
    assert tidy.closable_flights({"t7"}, finished=set(), live=None, scope_all=True) == []
    assert tidy.closable_flights({"t7"}, finished=set(), live="t7", scope_all=True) == []


# --------------------------- the flight's checkout ---------------------------

def test_a_finished_flights_checkout_is_reclaimable_once_its_session_is_gone():
    assert tidy.reclaimable_flight_worktrees(["t7", "i3"], live=set()) == ["t7"]


def test_a_live_flights_checkout_is_never_reclaimed():
    # Pruning the cwd of a live CLI is the D14 shape, whatever the flight says about itself.
    assert tidy.reclaimable_flight_worktrees(["t7"], live={"t7"}) == []


def test_a_flight_still_being_launched_into_is_never_pruned():
    """The launcher CREATES the checkout and only then opens the pane; `start-session.sh` takes the
    singleton lock inside that new pane, a whole verify window later. So between the two there is a
    real interval in which a flight is being born and holds NO lock at all — and reading that as
    "dead" would prune the checkout out from under it (fresh-agent review, P0)."""
    assert tidy.reclaimable_flight_worktrees(["t7"], live=set(), launching={"t7"}) == []


def test_the_launch_grace_clears_the_launch_shims_own_ceiling():
    # The interval it must cover is bounded by the launch shim's own timeout, after which either
    # the lock exists or the launcher has torn the pane down.
    assert tidy.FLIGHT_LAUNCH_GRACE_SECONDS >= 180


def test_flight_checkout_reclaim_never_takes_a_lane_or_an_unknown_shape():
    assert tidy.reclaimable_flight_worktrees(["i7", "d7", "t", "tx", ""], live=set()) == []
    assert tidy.reclaimable_flight_worktrees("t7", live=set()) == []


def test_flight_checkout_reclaim_refuses_an_unreadable_liveness_view():
    # Nothing is provably dead, so nothing is provably safe to prune.
    assert tidy.reclaimable_flight_worktrees(["t7"], live="t7") == []
    assert tidy.reclaimable_flight_worktrees(["t7"], live=None) == []
    assert tidy.reclaimable_flight_worktrees(["t7"], launching="t7") == []
    assert tidy.reclaimable_flight_worktrees(["t7"], launching=None) == []


def test_flight_checkout_reclaim_is_sorted_by_flight_number():
    assert tidy.reclaimable_flight_worktrees(["t10", "t2"], live=set()) == ["t2", "t10"]


# --------------------------- the finished signal ---------------------------

def test_finished_flights_reads_the_flights_own_closing_act():
    records = [{"act": "triage_launch", "id": "t7", "date": "2026-08-25"},
               {"act": "triage_finish", "date": "2026-08-25", "flight": "t7"}]
    assert triage.finished_flights(records) == {"t7"}


def test_a_hand_run_finish_outside_a_flight_closes_no_flight():
    # `_triage_flight_id` records "" when SL_ISSUE_ID names no flight — an honest "belongs to none".
    assert triage.finished_flights([{"act": "triage_finish", "flight": ""}]) == set()
    assert triage.finished_flights([{"act": "triage_finish"}]) == set()


def test_finished_flights_fails_closed_on_every_wrong_typed_shape():
    assert triage.finished_flights(None) == set()
    assert triage.finished_flights("nope") == set()
    assert triage.finished_flights([None, 7, [], {"act": 3}]) == set()
    assert triage.finished_flights([{"act": "triage_finish", "flight": ["t7"]}]) == set()
    assert triage.finished_flights([{"act": "triage_finish", "flight": "i7"}]) == set()


def test_only_the_closing_act_counts_as_finished():
    for act in ("triage_launch", "triage_keep", "triage_close", "triage_escalate"):
        assert triage.finished_flights([{"act": act, "flight": "t7"}]) == set(), act


# --------------------------- panes: already admits a flight ---------------------------

def test_panes_lists_a_flights_recorded_window(tmp_path):
    state = tmp_path / "state"
    (state / "panes").mkdir(parents=True)
    (state / "panes" / "t7").write_text("ws1:pane1")
    (state / "panes" / "t7.ws").write_text("ws1")
    assert panes.recorded_ids(str(state)) == {"t7"}


# --------------------------- janitor: SILENT ---------------------------

def test_janitor_never_proposes_anything_about_a_flight():
    # A flight opens no branch and no PR, so there is nothing of its shape for the sweep to see.
    out = janitor.propose(branches=["sl/t7", "sl/i7"], branch_prs={}, superseded_prs=[],
                          parked_issues=[], ls_issues={}, now=0, aged_park_days=7,
                          dev_branch="main")
    assert all("t7" not in str(p.get("target")) for p in out["proposals"]), out["proposals"]


def test_janitors_branch_reader_refuses_a_flight_shaped_branch():
    assert janitor.branch_issue_num("sl/t7") is None
    assert janitor.branch_issue_num("sl/i7") == 7


# --------------------------- report: SILENT as a lane ---------------------------

def test_the_morning_reports_lane_reader_never_renders_a_flight_as_a_lane():
    # A flight has no loopstate record by design; if one were forged there, it is still not an
    # issue and must not be numbered as one.
    assert report._iid_num("t7") is None
    holds = report.standing_holds({"issues": {"t7": {"status": "running"}}}, [])
    assert holds == []


# --------------------------- published_view: SILENT ---------------------------

def test_the_published_view_never_carries_a_flight():
    """It is bounded by what LOOPSTATE tracks, and a flight is never in loopstate — so a flight
    reaches no carry, no PR row and no phase. #451 owns how a flight renders; this pins that it is
    not smuggled in here as a lane in the meantime."""
    view = published_view.build({}, {}, tracked_ids=set(), now=0,
                                carry_titles={"t7": "a flight"},
                                carry_prs={"t7": {"number": 7, "state": "OPEN"}},
                                in_flight_ids=set(), report_ids=set())
    assert "t7" not in view["issues"] and "t7" not in view["titles"]
    assert "t7" not in view["prs"]
    assert published_view._iid_num("t7") is None


# --------------------------- actions: a session, never an issue ---------------------------

def test_actions_counts_a_flight_as_a_launch_session():
    assert actions._is_session_id("t7")


def test_actions_never_counts_a_flight_as_an_issue():
    assert actions._iid_num("t7") is None


def test_actions_still_refuses_an_unknown_session_id_shape():
    for junk in ("x7", "t", "t7x", "tt7", "", None, "t" + "9" * 40):
        assert not actions._is_session_id(junk), junk
