"""The pure selection core behind `superlooper tidy` (skill/lib/tidy.py).

`tidy` is William's explicit word (V1 'nothing auto-closed' — DRYRUN 2026-07-03): a finished
claude worker idles at its prompt forever (D4) and never self-closes, so its cmux window piles
up. `closable()` picks which windows tidy may close, PURELY from state on disk — so the whole
safety contract is a unit-test table here, no cmux required.

The two safety properties that MUST hold (they are why the fail-OPEN-on-wrong-typed and
shared-mutable-default defect classes matter): tidy NEVER selects an in-flight lane
({running,blocked,frozen,exited}) or an in-between gate lane ({gating,holding}), and it only
ever selects a status it can positively NAME as closable — anything wrong-typed / unknown is
skipped (fail closed = never close something ambiguous).
"""
import copy

import tidy


def _issue(status):
    return {"status": status, "branch": None}


# ------------------------- default scope: merged only -------------------------

def test_default_scope_selects_only_merged_sessions_with_a_window():
    issues = {"i1": _issue("merged"), "i2": _issue("parked")}
    got = tidy.closable(issues, windows={"i1", "i2"})
    assert got == [{"id": "i1", "status": "merged"}]


def test_default_scope_excludes_the_other_terminal_statuses():
    # parked / needs_william / bounced are terminal but NOT merged: default scope leaves them.
    issues = {"i1": _issue("parked"), "i2": _issue("needs_william"), "i3": _issue("bounced")}
    assert tidy.closable(issues, windows={"i1", "i2", "i3"}) == []


# ------------------------- --all scope: every terminal status -------------------------

def test_all_scope_selects_every_terminal_status():
    issues = {"i1": _issue("merged"), "i2": _issue("parked"),
              "i3": _issue("needs_william"), "i4": _issue("bounced")}
    got = tidy.closable(issues, windows={"i1", "i2", "i3", "i4"}, scope_all=True)
    assert {g["id"] for g in got} == {"i1", "i2", "i3", "i4"}
    assert {g["id"]: g["status"] for g in got} == {
        "i1": "merged", "i2": "parked", "i3": "needs_william", "i4": "bounced"}


# ------------------------- safety: never an in-flight lane -------------------------

def test_never_selects_an_inflight_lane_even_under_all_with_a_window():
    # exited counts as IN-FLIGHT (its process may still be recovering) — leave it alone.
    for status in ("running", "blocked", "frozen", "exited"):
        issues = {"i1": _issue(status)}
        assert tidy.closable(issues, windows={"i1"}, scope_all=True) == [], status


def test_never_selects_an_in_between_gate_lane_even_under_all_with_a_window():
    # gating/holding = build done, merge mechanics still running; not terminal, never close.
    for status in ("gating", "holding"):
        issues = {"i1": _issue(status)}
        assert tidy.closable(issues, windows={"i1"}, scope_all=True) == [], status


def test_never_selects_a_not_yet_started_session():
    for status in ("ready", None):
        issues = {"i1": _issue(status)}
        assert tidy.closable(issues, windows={"i1"}, scope_all=True) == [], status


def test_never_selects_an_unknown_status_positive_allowlist():
    # a novel/typo'd status is not a known-terminal one: fail closed, do not close it.
    issues = {"i1": _issue("archived"), "i2": _issue("closed")}
    assert tidy.closable(issues, windows={"i1", "i2"}, scope_all=True) == []


# ------------------------- requires a recorded window -------------------------

def test_merged_without_a_recorded_window_is_not_selected():
    # no pane marker on disk -> nothing to close -> not listed (tidy closes WINDOWS).
    issues = {"i1": _issue("merged"), "i2": _issue("merged")}
    assert tidy.closable(issues, windows={"i2"}) == [{"id": "i2", "status": "merged"}]


# ------------------------- deterministic order -------------------------

def test_result_is_sorted_by_issue_number_not_lexically():
    issues = {"i10": _issue("merged"), "i2": _issue("merged"), "i1": _issue("merged")}
    got = tidy.closable(issues, windows={"i1", "i2", "i10"})
    assert [g["id"] for g in got] == ["i1", "i2", "i10"]


# ------------------------- fail closed on wrong-typed input -------------------------

def test_wrong_typed_issues_map_yields_nothing():
    for bad in (None, [], "merged", 5):
        assert tidy.closable(bad, windows={"i1"}) == [], bad


def test_wrong_typed_windows_selects_nothing():
    # windows we cannot read as a collection -> treat as empty -> close nothing (fail closed).
    issues = {"i1": _issue("merged")}
    for bad in (None, 5, "i1"):
        assert tidy.closable(issues, windows=bad) == [], bad


def test_non_dict_and_non_issue_entries_are_skipped():
    issues = {"i1": _issue("merged"), "i2": "not-a-dict", "next": _issue("merged"),
              "i3": None}
    assert tidy.closable(issues, windows={"i1", "i2", "i3", "next"}) == \
        [{"id": "i1", "status": "merged"}]


# ------------------------- no shared mutable state / no input mutation -------------------------

def test_does_not_mutate_its_inputs():
    issues = {"i1": _issue("merged")}
    before = copy.deepcopy(issues)
    windows = {"i1"}
    tidy.closable(issues, windows=windows)
    assert issues == before
    assert windows == {"i1"}


def test_repeated_calls_return_independent_lists():
    issues = {"i1": _issue("merged")}
    a = tidy.closable(issues, windows={"i1"})
    b = tidy.closable(issues, windows={"i1"})
    assert a == b
    assert a is not b and a[0] is not b[0]      # fresh objects, no shared-mutable default


# ------------------------- fail closed on UNHASHABLE wrong-typed input -------------------------
# (Codex cross-review round 1) the contract is "wrong-typed -> skipped, never a raise". A list /
# dict slips past an isinstance(...,(set,list,...)) collection check yet raises inside set()/`in`.

def test_unhashable_window_entries_are_skipped_not_raised():
    # a window list carrying an unhashable element must not raise; valid string ids still count.
    issues = {"i1": _issue("merged"), "i2": _issue("merged")}
    assert tidy.closable(issues, windows=[["i1"], "i2"]) == [{"id": "i2", "status": "merged"}]


def test_unhashable_status_is_skipped_not_raised():
    # a wrong-typed (unhashable) status must be skipped, never raise on the `in targets` check.
    issues = {"i1": {"status": []}, "i2": {"status": {}}, "i3": _issue("merged")}
    assert tidy.closable(issues, windows={"i1", "i2", "i3"}, scope_all=True) == \
        [{"id": "i3", "status": "merged"}]


def test_wrong_typed_scope_all_falls_to_the_default_merged_scope():
    # only the literal True widens scope; a truthy wrong-typed value (e.g. "False") must NOT
    # silently open --all — the safer, narrower default scope is the fail-closed landing.
    issues = {"i1": _issue("parked")}
    for bad in ("False", 1, "yes", [1]):
        assert tidy.closable(issues, windows={"i1"}, scope_all=bad) == [], bad


# ======================= worktree reclaim (issue #41) =======================
#
# Worktrees are auto-removed only for MERGED issues, so a parked / needs-william / bounced lane's
# worktree lingers forever. reclaimable_worktrees() is the SAME pure fail-closed selector shape as
# closable(): it names the park-family terminal statuses whose worktree still exists on disk, and it
# is safe because re-approval rebuilds from the issue on a fresh branch (nothing durable is lost —
# the branch is on the remote, the audit trail in the journal). merged is deliberately EXCLUDED: it
# stays on the existing merge-time removal path (its own cleanup_merged_worktrees gate). The runner
# sweeps this each tick to bound long-run disk growth; a live lane is NEVER touched.


def test_reclaims_park_family_worktrees_that_exist_on_disk():
    issues = {"i1": _issue("parked"), "i2": _issue("needs_william"), "i3": _issue("bounced")}
    assert set(tidy.reclaimable_worktrees(issues, {"i1", "i2", "i3"})) == {"i1", "i2", "i3"}


def test_reclaim_excludes_merged_left_to_the_merge_time_path():
    # merged worktrees are removed at merge time under cleanup_merged_worktrees — this sweep must not
    # double-claim them (or it would override that config when it is turned off).
    issues = {"i1": _issue("merged")}
    assert tidy.reclaimable_worktrees(issues, {"i1"}) == []


def test_reclaim_never_touches_a_live_or_in_between_gate_lane():
    # the core safety property: an in-flight build ({running,blocked,frozen,exited}) or an in-between
    # gate lane ({gating,holding}) is a LIVE lane — its worktree is being written; never reclaim it,
    # even with a worktree dir present on disk.
    for status in ("running", "blocked", "frozen", "exited", "gating", "holding"):
        issues = {"i1": _issue(status)}
        assert tidy.reclaimable_worktrees(issues, {"i1"}) == [], status


def test_reclaim_never_touches_not_started_or_unknown_status():
    for status in ("ready", None, "archived", "closed"):
        issues = {"i1": _issue(status)}
        assert tidy.reclaimable_worktrees(issues, {"i1"}) == [], status


def test_reclaim_requires_the_worktree_to_exist_on_disk():
    # nothing on disk -> nothing to reclaim (idempotent: once removed, the dir is gone and the next
    # sweep skips it, so steady-state cost is a single listing, no git call).
    issues = {"i1": _issue("parked"), "i2": _issue("parked")}
    assert tidy.reclaimable_worktrees(issues, {"i2"}) == ["i2"]


def test_reclaim_result_is_sorted_by_issue_number():
    issues = {"i10": _issue("parked"), "i2": _issue("parked"), "i1": _issue("parked")}
    assert tidy.reclaimable_worktrees(issues, {"i1", "i2", "i10"}) == ["i1", "i2", "i10"]


def test_reclaim_fail_closed_on_wrong_typed_inputs():
    for bad in (None, [], "parked", 5):
        assert tidy.reclaimable_worktrees(bad, {"i1"}) == [], bad
    issues = {"i1": _issue("parked")}
    for bad in (None, 5, "i1"):
        assert tidy.reclaimable_worktrees(issues, bad) == [], bad


def test_reclaim_skips_unhashable_worktree_entries_and_status_without_raising():
    issues = {"i1": {"status": []}, "i2": _issue("parked")}
    assert tidy.reclaimable_worktrees(issues, [["i1"], "i2"]) == ["i2"]


def test_reclaim_does_not_mutate_its_inputs():
    issues = {"i1": _issue("parked")}
    before = copy.deepcopy(issues)
    ids = {"i1"}
    tidy.reclaimable_worktrees(issues, ids)
    assert issues == before and ids == {"i1"}


# ======================= answerer windows (issue #132) =======================
#
# An answerer session (a<N>) hired by the Discuss/blocked flow is NOT a tracked issue, so
# closable() never sees it and its cmux window lingers after its Q&A concludes (live evidence:
# state/panes/a1 left behind after delivery). closable_answerers() is the SAME pure fail-closed
# selector shape, adapted to a lifecycle where "finished" is the ABSENCE of a record:
#
#   * The runner writes answerers[a<N>] at hire and POPS it the moment the answerer's lifecycle
#     ends — delivery, park (timeout / escalation / hire-cap), owner-absorb, re-approval (runner.py,
#     four pop sites). So the `answerers` map holds EXACTLY the currently-active answerers; a finished
#     one shows up as a pane marker with no record.
#   * The clock-free race guard is a high-water mark: next_answerer is bumped to N+1 ATOMICALLY with
#     writing a<N>'s record (one loopstate.update), and launch-session.sh writes a<N>'s pane marker
#     only AFTER delivery verifies — strictly BEFORE that atomic write. So in the ONLY window where
#     a<N> has a marker but no record (mid-hire), next_answerer has NOT yet passed N, and
#     `N < next_answerer` is False: a mid-hire answerer is NEVER selected. aids are allocated
#     monotonically and never reused, so a selected a<N> can never relaunch — closing its window and
#     clearing its marker are both permanently race-free.
#
# Selection = a<N> has a pane marker AND a<N> is not an active record AND N < next_answerer.


def test_delivered_answerer_window_is_selected():
    # The primary case: answer delivered -> record popped, marker lingers, counter already past N.
    got = tidy.closable_answerers({}, next_answerer=2, windows={"a1"})
    assert got == [{"id": "a1", "status": "finished"}]


def test_active_answerer_is_never_selected_even_with_a_window_and_high_water():
    # a1 is mid-answer (a live record exists) -> NEVER close, even though its marker is on disk and
    # the counter has moved past it.
    answerers = {"a1": {"for": "i5", "launched_at": 100}}
    assert tidy.closable_answerers(answerers, next_answerer=2, windows={"a1"}) == []


def test_mid_hire_race_is_not_selected_counter_not_yet_past_n():
    # The airtight guard: launch-session.sh wrote the a1 marker, but the runner has NOT yet written
    # the record + bumped next_answerer (that atomic write lands together). During this window
    # next_answerer is still 1, so `1 < 1` is False and a1 is left alone — never kill a mid-hire.
    assert tidy.closable_answerers({}, next_answerer=1, windows={"a1"}) == []


def test_failed_hire_marker_is_not_selected_until_a_later_hire_completes():
    # A hire that failed (rc != 0) never bumped the counter, and a re-hire REUSES the same aid. So
    # while next_answerer has not passed a1, a1 must not be closed — a re-hire could relight it.
    assert tidy.closable_answerers({}, next_answerer=1, windows={"a1"}) == []


def test_high_water_boundary_selects_below_but_not_at_next_answerer():
    # N < next_answerer is strict: with next_answerer=3, a1/a2 are past-and-finished, a3 is the aid
    # currently being (or about to be) hired and must be left alone.
    got = tidy.closable_answerers({}, next_answerer=3, windows={"a1", "a2", "a3"})
    assert [g["id"] for g in got] == ["a1", "a2"]


def test_answerer_without_a_recorded_window_is_not_selected():
    # closable_answerers closes WINDOWS: no pane marker on disk -> nothing to close.
    assert tidy.closable_answerers({}, next_answerer=5, windows=set()) == []
    assert tidy.closable_answerers({}, next_answerer=5, windows={"a2"}) == \
        [{"id": "a2", "status": "finished"}]


def test_answerer_result_is_sorted_by_number_not_lexically():
    got = tidy.closable_answerers({}, next_answerer=99, windows={"a1", "a2", "a10"})
    assert [g["id"] for g in got] == ["a1", "a2", "a10"]


def test_next_answerer_wrong_typed_or_missing_selects_nothing():
    # Not a real positive int -> nothing is `< next_answerer` -> close nothing (fail closed). bool is
    # excluded (True is an int subclass but never a real counter); a float/str/None cannot gate.
    for bad in (None, "2", 2.0, True, False, [1], {}):
        assert tidy.closable_answerers({}, next_answerer=bad, windows={"a1"}) == [], bad


def test_wrong_typed_answerers_map_fails_closed():
    # A corrupt answerers map means we CANNOT prove a<N> is inactive -> never close it (when in
    # doubt, do not close). Unlike an empty {} (the normal all-delivered case), a NON-dict fails closed.
    for bad in (None, [], "a1", 5):
        assert tidy.closable_answerers(bad, next_answerer=5, windows={"a1"}) == [], bad


def test_empty_answerers_dict_is_the_normal_delivered_case_not_a_failure():
    # {} is a VALID active set (everything delivered) -> the window IS closable.
    assert tidy.closable_answerers({}, next_answerer=5, windows={"a1"}) == \
        [{"id": "a1", "status": "finished"}]


def test_a_record_with_any_value_type_protects_that_aid():
    # The active check is key-presence, not value shape: a wrong-typed record value still means the
    # runner tracked a1 -> protect it (fail closed), never close.
    for rec in ("garbage", 5, None, {}, {"for": "i9"}):
        answerers = {"a1": rec}
        assert tidy.closable_answerers(answerers, next_answerer=5, windows={"a1"}) == [], rec


def test_wrong_typed_windows_selects_nothing():
    for bad in (None, 5, "a1"):
        assert tidy.closable_answerers({}, next_answerer=5, windows=bad) == [], bad


def test_unhashable_window_entries_are_skipped_not_raised():
    # The contract is "wrong-typed -> skipped, never a raise" (a list slips past the collection
    # check yet raises inside set()); valid a<N> ids still count.
    got = tidy.closable_answerers({}, next_answerer=5, windows=[["a1"], "a2"])
    assert got == [{"id": "a2", "status": "finished"}]


def test_non_answerer_window_names_are_skipped():
    # Only a<N> ids are answerer windows; an i<N> issue id, a bare "a", or a typo'd "aX" is ignored.
    windows = {"a2", "a", "aX", "i3", "a2.ws"}
    assert tidy.closable_answerers({}, next_answerer=5, windows=windows) == \
        [{"id": "a2", "status": "finished"}]


def test_a0_is_not_a_real_answerer_and_is_skipped():
    # The runner allocates aids from a1 (never a0), so a stray `a0` pane marker is corruption, not a
    # finished answerer — skip it (positive allowlist: only act on a provably-real answerer id).
    assert tidy.closable_answerers({}, next_answerer=5, windows={"a0", "a1"}) == \
        [{"id": "a1", "status": "finished"}]


def test_answerer_does_not_mutate_its_inputs():
    answerers = {"a1": {"for": "i5"}}
    windows = {"a1", "a2"}
    before_a = copy.deepcopy(answerers)
    tidy.closable_answerers(answerers, next_answerer=3, windows=windows)
    assert answerers == before_a and windows == {"a1", "a2"}


def test_repeated_answerer_calls_return_independent_lists():
    a = tidy.closable_answerers({}, next_answerer=2, windows={"a1"})
    b = tidy.closable_answerers({}, next_answerer=2, windows={"a1"})
    assert a == b and a is not b and a[0] is not b[0]
