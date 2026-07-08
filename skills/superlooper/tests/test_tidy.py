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
