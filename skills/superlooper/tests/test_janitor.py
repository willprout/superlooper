"""The janitor's PURE proposal selector (issue #62): which GitHub-side debris may
`superlooper janitor` PROPOSE to the owner? Selection only — nothing here (or anywhere) closes
or deletes without William's explicit approval; the CLI executes approved items and is tested
in test_cli.py.

The safety contract, as a unit-test table:
  * A branch is proposed ONLY when its work provably landed or was provably replaced: its PR
    MERGED, or its PR CLOSED and labeled `superseded`. A branch with no PR, an OPEN PR (even a
    superseded one — closing that PR is its own proposal; the branch follows a later sweep), or
    a closed-unmerged PR without `superseded` is NEVER proposed: never delete an unmerged
    branch's work.
  * In-flight and mid-gate work (actions.TERRITORY_CLAIM_STATUSES) is mechanically excluded —
    by issue number parsed from the branch name AND by the loopstate-recorded branch — so it can
    never be proposed, whichever record survives.
  * Every wrong-typed or unreadable input fails CLOSED to "propose nothing" (this repo's
    fail-open-on-wrong-typed defect class pointing the safe way).
"""
import pytest

import actions
import janitor


def _pr(num, state, labels=(), head=None, oid="tip0"):
    """A raw gh PR dict, labels in gh's [{'name': ...}] shape. `oid` is headRefOid — the PR's
    last-known head, matched against the branch's current tip before a delete is proposed."""
    d = {"number": num, "state": state, "labels": [{"name": n} for n in labels],
         "headRefOid": oid}
    if head is not None:
        d["headRefName"] = head
    return d


NOW = 1_800_000_000.0
DAY = 86400


def _iso(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def propose(**kw):
    base = dict(branches={}, branch_prs={}, superseded_prs=[], parked_issues=[],
                ls_issues={}, now=NOW, aged_park_days=14, refused=frozenset(),
                dev_branch="main")
    base.update(kw)
    return janitor.propose(**base)


# --------------------------- branch proposals ---------------------------
# `branches` maps each remote branch to its CURRENT tip sha; a delete is proposed only when
# that tip IS the PR's last-known head (headRefOid) — commits pushed after the PR merged or
# closed would otherwise be lost (cross-review round 1, M3).

def test_merged_pr_branch_is_proposed_with_a_why_naming_the_pr():
    r = propose(branches={"sl/i5-fix-thing": "tip0"},
                branch_prs={"sl/i5-fix-thing": ( _pr(12, "MERGED"), True )})
    assert [p["key"] for p in r["proposals"]] == ["branch:sl/i5-fix-thing"]
    p = r["proposals"][0]
    assert p["action"] == "delete-branch" and p["target"] == "sl/i5-fix-thing"
    assert "#12" in p["why"] and "merged" in p["why"].lower()


def test_closed_superseded_pr_branch_is_proposed():
    r = propose(branches={"sl/i7-old": "tip0"},
                branch_prs={"sl/i7-old": (_pr(9, "CLOSED", labels=("superseded",)), True)})
    assert [p["target"] for p in r["proposals"]] == ["sl/i7-old"]
    assert "superseded" in r["proposals"][0]["why"]


def test_branch_tip_moved_since_the_pr_is_never_proposed():
    # commits pushed AFTER the PR merged/closed would be lost with the branch: deletion is
    # proposed ONLY when the branch's current tip is the PR's last-known head.
    r = propose(branches={"sl/i5-x": "a-new-tip"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED", oid="tip0"), True)})
    assert r["proposals"] == []


def test_unknown_tip_or_missing_head_oid_fails_closed():
    no_oid = _pr(12, "MERGED")
    del no_oid["headRefOid"]
    r = propose(branches={"sl/i5-x": None, "sl/i6-y": "tip0", "sl/i8-z": 42},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True),
                            "sl/i6-y": (no_oid, True),
                            "sl/i8-z": (_pr(13, "MERGED"), True)})
    assert r["proposals"] == []


def test_open_pr_branch_is_never_proposed_even_when_superseded():
    # closing the open superseded PR is its own proposal; the branch follows a LATER sweep —
    # deleting a branch under an open PR would force-close the PR server-side.
    r = propose(branches={"sl/i7-old": "tip0"},
                branch_prs={"sl/i7-old": (_pr(9, "OPEN", labels=("superseded",)), True)})
    assert [p for p in r["proposals"] if p["kind"] == "branch"] == []


def test_closed_unmerged_pr_without_superseded_is_never_proposed():
    r = propose(branches={"sl/i7-old": "tip0"},
                branch_prs={"sl/i7-old": (_pr(9, "CLOSED"), True)})
    assert r["proposals"] == []


def test_branch_with_no_pr_is_never_proposed():
    # no PR ever existed: the work can't be proven landed anywhere — never delete it.
    r = propose(branches={"sl/i7-old": "tip0"}, branch_prs={"sl/i7-old": ({}, True)})
    assert r["proposals"] == []


def test_refused_pr_lookup_fails_closed():
    # ok=False means GitHub REFUSED the lookup (gh.PrRead contract): emptiness is not an answer.
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), False)})
    assert r["proposals"] == []


def test_missing_lookup_entry_fails_closed():
    r = propose(branches={"sl/i5-x": "tip0"}, branch_prs={})
    assert r["proposals"] == []


def test_non_sl_branches_and_the_dev_branch_are_ignored():
    r = propose(branches={"main": "tip0", "feature/foo": "tip0", "sl/i5-x": "tip0"},
                branch_prs={b: (_pr(1, "MERGED"), True)
                            for b in ("main", "feature/foo", "sl/i5-x")})
    assert [p["target"] for p in r["proposals"]] == ["sl/i5-x"]
    # belt+braces: even a dev branch that matched the prefix is never proposed
    r2 = propose(branches={"sl/i5-x": "tip0"}, dev_branch="sl/i5-x",
                 branch_prs={"sl/i5-x": (_pr(1, "MERGED"), True)})
    assert r2["proposals"] == []


@pytest.mark.parametrize("status", sorted(actions.TERRITORY_CLAIM_STATUSES))
def test_inflight_issue_branch_is_excluded_by_number_parsed_from_the_name(status):
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                ls_issues={"i5": {"status": status}})
    assert r["proposals"] == []


@pytest.mark.parametrize("status", sorted(actions.TERRITORY_CLAIM_STATUSES))
def test_inflight_issue_branch_is_excluded_by_recorded_branch_name(status):
    # the loopstate branch record is the second, independent exclusion path: a branch whose
    # name doesn't parse (or parses to a different number) is still excluded when a live lane
    # RECORDS it as its own.
    r = propose(branches={"sl/weird-name": "tip0"},
                branch_prs={"sl/weird-name": (_pr(12, "MERGED"), True)},
                ls_issues={"i9": {"status": status, "branch": "sl/weird-name"}})
    assert r["proposals"] == []


def test_generation_suffixed_branch_of_an_inflight_issue_is_excluded():
    # sl/i5-x-r2 parses to issue 5 — a live rebuild excludes EVERY generation of its branches.
    r = propose(branches={"sl/i5-x-r2": "tip0"},
                branch_prs={"sl/i5-x-r2": (_pr(12, "MERGED"), True)},
                ls_issues={"i5": {"status": "running", "branch": "sl/i5-x-r3"}})
    assert r["proposals"] == []


@pytest.mark.parametrize("status", sorted(actions.TERMINAL_STATUSES))
def test_terminal_statuses_do_not_exclude(status):
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                ls_issues={"i5": {"status": status, "branch": "sl/i5-x"}})
    assert [p["target"] for p in r["proposals"]] == ["sl/i5-x"]


# --------------------------- superseded-PR proposals ---------------------------

def test_open_superseded_pr_is_proposed_to_close():
    r = propose(superseded_prs=[_pr(14, "OPEN", labels=("superseded",), head="sl/i7-a")])
    assert [p["key"] for p in r["proposals"]] == ["pr:14"]
    p = r["proposals"][0]
    assert p["action"] == "close-pr" and p["target"] == 14
    assert "superseded" in p["why"]


@pytest.mark.parametrize("status", sorted(actions.TERRITORY_CLAIM_STATUSES))
def test_superseded_pr_of_an_inflight_issue_is_excluded(status):
    r = propose(superseded_prs=[_pr(14, "OPEN", labels=("superseded",), head="sl/i7-a")],
                ls_issues={"i7": {"status": status}})
    assert r["proposals"] == []


def test_superseded_pr_without_the_label_in_the_data_is_skipped():
    # defense in depth: the query asked for the label, but the entry itself must prove it.
    r = propose(superseded_prs=[_pr(14, "OPEN", head="sl/i7-a")])
    assert r["proposals"] == []


def test_superseded_pr_with_wrong_typed_number_is_skipped():
    r = propose(superseded_prs=[_pr(True, "OPEN", labels=("superseded",), head="sl/i7-a"),
                                _pr("14", "OPEN", labels=("superseded",), head="sl/i7-b"),
                                "garbage", None])
    assert r["proposals"] == []


def test_non_open_superseded_pr_is_skipped():
    # the query is open-only, but a stale/raced answer must not propose closing a closed PR.
    r = propose(superseded_prs=[_pr(14, "MERGED", labels=("superseded",), head="sl/i7-a")])
    assert r["proposals"] == []


# --------------------------- aged parked-issue proposals ---------------------------

def _issue(num, labels, updated_epoch, title="t"):
    return {"number": num, "title": title, "labels": [{"name": n} for n in labels],
            "updatedAt": _iso(updated_epoch)}


def test_aged_parked_issue_is_proposed_with_age_and_threshold_in_the_why():
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 21 * DAY)])
    assert [p["key"] for p in r["proposals"]] == ["issue:9"]
    p = r["proposals"][0]
    assert p["action"] == "close-issue" and p["target"] == 9
    assert "21d" in p["why"] and "14d" in p["why"]


def test_fresh_parked_issue_is_not_proposed():
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 13 * DAY)])
    assert r["proposals"] == []


def test_needs_william_label_counts_and_is_named_in_the_why():
    r = propose(parked_issues=[_issue(9, ("needs-william",), NOW - 15 * DAY)])
    assert len(r["proposals"]) == 1
    assert "needs-william" in r["proposals"][0]["why"]


def test_unparseable_or_missing_updated_at_fails_closed():
    bad = {"number": 9, "title": "t", "labels": [{"name": "parked"}],
           "updatedAt": "not-a-date"}
    missing = {"number": 10, "title": "t", "labels": [{"name": "parked"}]}
    r = propose(parked_issues=[bad, missing])
    assert r["proposals"] == []


def test_in_progress_labeled_issue_is_mechanically_excluded():
    r = propose(parked_issues=[_issue(9, ("parked", "in-progress"), NOW - 30 * DAY)])
    assert r["proposals"] == []


def test_agent_ready_labeled_issue_is_mechanically_excluded():
    # a re-approval whose label cleanup blipped can leave parked + agent-ready together; the
    # owner's approval word wins — never propose closing work he approved to run
    # (cross-review round 1, M2).
    r = propose(parked_issues=[_issue(9, ("parked", "agent-ready"), NOW - 30 * DAY)])
    assert r["proposals"] == []


@pytest.mark.parametrize("bad", ["14", True, -1, None, 1.5])
def test_wrong_typed_age_threshold_proposes_no_issues(bad):
    # a wrong-typed threshold must NOT coerce to the most aggressive setting (0d): the issue
    # class fails closed to nothing while branch/PR proposals stand on their own evidence
    # (cross-review round 1, M1).
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 365 * DAY)],
                branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                aged_park_days=bad)
    assert [p["kind"] for p in r["proposals"]] == ["branch"]


@pytest.mark.parametrize("status", sorted(actions.TERRITORY_CLAIM_STATUSES))
def test_inflight_loopstate_issue_is_excluded_whatever_its_labels_say(status):
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)],
                ls_issues={"i9": {"status": status}})
    assert r["proposals"] == []


def test_duplicate_issue_across_both_labels_is_proposed_once():
    a = _issue(9, ("parked", "needs-william"), NOW - 30 * DAY)
    r = propose(parked_issues=[a, dict(a)])
    assert [p["key"] for p in r["proposals"]] == ["issue:9"]


def test_zero_day_threshold_proposes_any_aged_park():
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 60)], aged_park_days=0)
    assert [p["target"] for p in r["proposals"]] == [9]


# --------------------------- refused-set handling ---------------------------

def test_refused_keys_are_held_back_and_reported_separately():
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)],
                refused={"branch:sl/i5-x"})
    assert [p["key"] for p in r["proposals"]] == ["issue:9"]
    assert r["refused"] == ["branch:sl/i5-x"]


def test_empty_refused_set_reproposes_everything():
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)}, refused=frozenset())
    assert [p["key"] for p in r["proposals"]] == ["branch:sl/i5-x"]
    assert r["refused"] == []


# --------------------------- reconcile (act-time re-verification) ---------------------------

def test_reconcile_executes_only_still_eligible_items():
    # the y/N wait can be minutes: an approved item executes ONLY if a FRESH re-derivation
    # still proposes it — a mid-wait re-approval can never get its branch deleted.
    approved = [{"key": "branch:sl/i5-x", "action": "delete-branch", "target": "sl/i5-x",
                 "kind": "branch", "why": "old why"},
                {"key": "issue:9", "action": "close-issue", "target": 9,
                 "kind": "issue", "why": "w"}]
    fresh = [{"key": "issue:9", "action": "close-issue", "target": 9,
              "kind": "issue", "why": "fresh why"}]
    to_run, skipped = janitor.reconcile(approved, fresh)
    assert [p["key"] for p in to_run] == ["issue:9"]
    assert to_run[0]["why"] == "fresh why"       # execute the FRESH item, not the stale one
    assert [p["key"] for p in skipped] == ["branch:sl/i5-x"]


def test_reconcile_never_executes_unapproved_fresh_items():
    fresh = [{"key": "pr:14", "action": "close-pr", "target": 14, "kind": "pr", "why": "w"}]
    to_run, skipped = janitor.reconcile([], fresh)
    assert to_run == [] and skipped == []


# --------------------------- wrong-typed inputs fail closed ---------------------------

def test_wrong_typed_inputs_propose_nothing_and_never_raise():
    r = propose(branches="sl/i5-x",            # not a mapping
                branch_prs=[("sl/i5-x", {})],  # not a dict
                superseded_prs={"14": {}},     # not a list
                parked_issues="garbage",       # not a list
                ls_issues=["i5"])              # not a dict
    assert r["proposals"] == [] and r["refused"] == []


def test_the_old_list_shape_for_branches_fails_closed():
    # `branches` is a {name: current tip} mapping; a bare list carries no tips, so no branch
    # can be proven un-moved — no branch proposals, never a raise.
    r = propose(branches=["sl/i5-x"],
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)})
    assert r["proposals"] == []


def test_wrong_typed_loopstate_entries_are_skipped_not_raised():
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                ls_issues={"i5": "running", "i6": None, 7: {"status": "running"},
                           "not-an-iid": {"status": "running"}})
    # none of the garbage entries could be POSITIVELY read as in-flight for i5;
    # but a wrong-typed record for THE SAME issue must fail closed (excluded).
    assert r["proposals"] == []


def test_wrong_typed_loopstate_as_a_whole_proposes_nothing():
    # the exclusion SOURCE being unreadable means nothing is provably idle: the whole sweep
    # fails closed to no proposals, even for otherwise-perfect candidates.
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                superseded_prs=[_pr(14, "OPEN", labels=("superseded",), head="sl/i7-a")],
                parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)],
                ls_issues=["i5"])
    assert r["proposals"] == [] and r["refused"] == []


def test_wrong_typed_loopstate_entry_for_another_issue_does_not_exclude():
    r = propose(branches={"sl/i5-x": "tip0"},
                branch_prs={"sl/i5-x": (_pr(12, "MERGED"), True)},
                ls_issues={"i6": "garbage"})
    assert [p["target"] for p in r["proposals"]] == ["sl/i5-x"]


def test_inputs_are_never_mutated_and_output_is_deterministic():
    branches = {"sl/i9-b": "tip0", "sl/i5-a": "tip0"}
    prs = [_pr(14, "OPEN", labels=("superseded",), head="sl/i7-a")]
    issues = [_issue(9, ("parked",), NOW - 30 * DAY)]
    ls = {"i1": {"status": "running"}}
    snap = (dict(branches), [dict(p) for p in prs], [dict(i) for i in issues],
            {k: dict(v) for k, v in ls.items()})
    kw = dict(branches=branches,
              branch_prs={b: (_pr(1, "MERGED"), True) for b in branches},
              superseded_prs=prs, parked_issues=issues, ls_issues=ls)
    r1, r2 = propose(**kw), propose(**kw)
    assert r1 == r2
    # grouped deterministically: branches (sorted), then PRs, then issues
    assert [p["key"] for p in r1["proposals"]] == \
        ["branch:sl/i5-a", "branch:sl/i9-b", "pr:14", "issue:9"]
    assert (branches, prs, issues, ls) == (snap[0], snap[1], snap[2], snap[3])


# --------------------------- little parsers ---------------------------

@pytest.mark.parametrize("branch,num", [
    ("sl/i62-the-janitor", 62), ("sl/i5-x-r2", 5), ("sl/i7", 7),
    ("sl/x", None), ("sl/i-x", None), ("main", None), ("sl/i12x", None),
    (None, None), (42, None),
])
def test_branch_issue_num(branch, num):
    assert janitor.branch_issue_num(branch) == num


def test_parse_epoch_roundtrips_github_timestamps():
    assert janitor.parse_epoch("2027-01-15T00:00:00Z") == 1799971200.0
    assert janitor.parse_epoch(_iso(NOW)) == NOW


@pytest.mark.parametrize("bad", ["", "not-a-date", "2027-01-15", None, 42,
                                 "2027-01-15T00:00:00+02:00"])
def test_parse_epoch_fails_closed_to_none(bad):
    assert janitor.parse_epoch(bad) is None
