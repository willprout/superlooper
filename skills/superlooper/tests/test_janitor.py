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


def test_park_labels_recognize_both_the_new_and_legacy_owner_decision_label():
    # issue #58 compat: the owner-decision label was renamed needs-william -> needs-owner. The
    # janitor's aged-park sweep queries EACH PARK_LABEL, so both must be recognized — a repo adopted
    # before the rename (or one mid-migration) still has its owner-decision issues found.
    assert "needs-owner" in janitor.PARK_LABELS      # current
    assert "needs-william" in janitor.PARK_LABELS    # legacy, still swept
    assert "parked" in janitor.PARK_LABELS


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


# --------------------------- accidental-close reopens (issue #229) ---------------------------
# The fourth debris class, and the only one whose action UNDOES a state change rather than
# tidying one: an issue closed as COMPLETED by a bare commit-message keyword reads as fixed while
# nothing shipped (the 2026-07-16 ledger commit that auto-closed the never-built #189). Detection
# lives in lib/closures.py and is tested there; these pin the janitor half — that it proposes,
# never acts, and that its own exclusions still hold.

def _closed(num, *, reason="COMPLETED", closer=None, title="Never built",
            closed="2026-07-16T15:32:28Z"):
    return {"number": num, "title": title, "stateReason": reason,
            "closedAt": closed, "closer": closer}


def _bare(oid="8b79d7acdeadbeef", headline="ledger: 07-16 overnight"):
    return {"type": "commit", "oid": oid, "headline": headline, "merged_prs": []}


def test_a_keyword_closed_issue_is_proposed_for_reopen_naming_the_commit():
    r = propose(closed_issues=[_closed(189, closer=_bare())])
    assert [p["key"] for p in r["proposals"]] == ["reopen:189"]
    p = r["proposals"][0]
    assert p["kind"] == "issue-reopen" and p["action"] == "reopen-issue" and p["target"] == 189
    assert p["title"] == "Never built"
    # the commit rides on the proposal AND in the why — the why becomes the audit comment
    assert p["commit"] == "8b79d7acdeadbeef"
    assert "8b79d7ac" in p["why"] and "ledger: 07-16 overnight" in p["why"]


def test_an_issue_closed_by_its_merged_pr_is_never_proposed_for_reopen():
    r = propose(closed_issues=[_closed(150, closer={"type": "pull_request", "number": 242,
                                                    "merged": True})])
    assert r["proposals"] == []


def test_an_owner_closed_issue_is_never_proposed_for_reopen():
    assert propose(closed_issues=[_closed(98, closer=None)])["proposals"] == []


def test_a_keyword_closed_issue_held_by_an_in_flight_lane_is_never_proposed():
    # a lane is mid-flight on that issue: it is already being dealt with — never touch it.
    r = propose(closed_issues=[_closed(189, closer=_bare())],
                ls_issues={"i189": {"status": "running", "branch": "sl/i189-x"}})
    assert r["proposals"] == []


def test_reopen_proposals_ride_the_refused_holdback_like_every_other_class():
    r = propose(closed_issues=[_closed(189, closer=_bare())], refused={"reopen:189"})
    assert r["proposals"] == [] and r["refused"] == ["reopen:189"]


def test_reopen_keys_never_collide_with_the_close_issue_keys():
    # both classes target issue numbers, so their KEYS must stay distinct — the command center
    # taps by key and the refused map is keyed the same way; one shared "issue:9" would let a
    # refused close hold back a reopen (and a tap on one run the other).
    close_key = propose(parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)])["proposals"][0]
    reopen_key = propose(closed_issues=[_closed(9, closer=_bare())])["proposals"][0]
    assert close_key["key"] == "issue:9" and reopen_key["key"] == "reopen:9"
    assert close_key["action"] == "close-issue" and reopen_key["action"] == "reopen-issue"


def test_reopen_proposals_are_deduped_sorted_and_emitted_last():
    r = propose(branches={"sl/i5-a": "tip0"},
                branch_prs={"sl/i5-a": (_pr(12, "MERGED"), True)},
                superseded_prs=[_pr(14, "OPEN", labels=("superseded",), head="sl/i7-a")],
                parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)],
                closed_issues=[_closed(70, closer=_bare()), _closed(12, closer=_bare()),
                               _closed(70, closer=_bare())])
    assert [p["key"] for p in r["proposals"]] == \
        ["branch:sl/i5-a", "pr:14", "issue:9", "reopen:12", "reopen:70"]


def test_a_wrong_typed_closed_issue_list_proposes_no_reopens():
    for bad in (None, "nope", {}, 42, [None, "x", 7]):
        assert propose(closed_issues=bad)["proposals"] == []


def test_an_unreadable_loopstate_still_fails_the_whole_sweep_closed_including_reopens():
    r = janitor.propose(branches={}, branch_prs={}, superseded_prs=[], parked_issues=[],
                        closed_issues=[_closed(189, closer=_bare())], ls_issues="garbage",
                        now=NOW, aged_park_days=14)
    assert r == {"proposals": [], "refused": [], "reopen_withheld": 0,
                 "merged_open_withheld": 0}


def test_closed_issues_defaults_to_nothing_so_every_existing_caller_keeps_working():
    # the kwarg is optional: a caller that never fetched closed issues proposes no reopens
    # rather than raising (the CLI, upkeep and the dashboard all reach propose()).
    assert janitor.propose(branches={}, branch_prs={}, superseded_prs=[], parked_issues=[],
                           ls_issues={}, now=NOW, aged_park_days=14) == \
        {"proposals": [], "refused": [], "reopen_withheld": 0, "merged_open_withheld": 0}


def test_a_tuple_of_closed_issues_is_accepted_like_a_list():
    # the parameter's own default is a tuple, so a caller passing one must not silently
    # propose nothing (fail-closed is right for a WRONG type, not for a sequence).
    r = propose(closed_issues=(_closed(189, closer=_bare()),))
    assert [p["key"] for p in r["proposals"]] == ["reopen:189"]


def test_the_reopen_class_is_capped_per_sweep_and_the_remainder_is_reported():
    # UNLIKE every other class, "closed by a commit keyword" is ordinary practice in a repo that
    # does not run this loop — an adopted repo can legitimately have hundreds. `--yes` is a
    # blanket approval, so an uncapped class would fire hundreds of reopens (each a comment, each
    # a notification) on one word. The cap is stated, never silent.
    closed = [_closed(n, closer=_bare(), closed="2026-07-%02dT00:00:00Z" % n)
              for n in range(1, 26)]
    r = propose(closed_issues=closed)
    reopens = [p for p in r["proposals"] if p["kind"] == "issue-reopen"]
    assert len(reopens) == janitor.REOPEN_SWEEP_CAP
    assert r["reopen_withheld"] == 25 - janitor.REOPEN_SWEEP_CAP
    # the 10 most recently CLOSED survive the cap, emitted sorted by number
    assert [p["target"] for p in reopens] == list(range(16, 26))


def test_the_cap_keeps_the_most_recently_closed_not_the_highest_numbered():
    # an OLD, low-numbered issue keyword-closed yesterday is the urgent one; a high-numbered issue
    # keyword-closed a year ago is not. Number-as-recency is the same substitution the read one
    # layer up had to drop (gh.closed_issue_closers orders by UPDATED_AT, not CREATED_AT) — the
    # cap must not quietly reintroduce it.
    old_but_high = [_closed(n, closer=_bare(), closed="2025-01-01T00:00:00Z")
                    for n in range(900, 900 + janitor.REOPEN_SWEEP_CAP)]
    recent_but_low = _closed(3, closer=_bare(), closed="2026-07-27T00:00:00Z")
    r = propose(closed_issues=old_but_high + [recent_but_low])
    kept = [p["target"] for p in r["proposals"] if p["kind"] == "issue-reopen"]
    assert 3 in kept and len(kept) == janitor.REOPEN_SWEEP_CAP
    assert r["reopen_withheld"] == 1


def test_an_undated_close_loses_its_slot_rather_than_taking_someone_elses():
    # a missing closedAt is an unprovable date: it sorts oldest, so it is withheld before a
    # provably-recent one — the same "age is proven, never guessed" rule the park class follows.
    dated = [_closed(n, closer=_bare(), closed="2026-07-%02dT00:00:00Z" % n)
             for n in range(1, 1 + janitor.REOPEN_SWEEP_CAP)]
    undated = _closed(500, closer=_bare(), closed="")
    r = propose(closed_issues=dated + [undated])
    kept = [p["target"] for p in r["proposals"] if p["kind"] == "issue-reopen"]
    assert 500 not in kept and r["reopen_withheld"] == 1


def test_a_refused_reopen_never_occupies_a_cap_slot():
    # the cap bounds ACTIONS. A previously-refused reopen is a report, not an action — letting a
    # handful of stuck keys eat the whole cap would starve the class permanently: nothing new
    # proposed, and the backlog never advancing.
    closed = [_closed(n, closer=_bare(), closed="2026-07-%02dT00:00:00Z" % n)
              for n in range(1, 26)]
    stuck = {"reopen:%d" % n for n in range(20, 26)}          # 6 of the newest are held back
    r = propose(closed_issues=closed, refused=stuck)
    reopens = [p for p in r["proposals"] if p["kind"] == "issue-reopen"]
    assert len(reopens) == janitor.REOPEN_SWEEP_CAP           # still a full cap of fresh work
    assert not (set(p["key"] for p in reopens) & stuck)
    assert set(r["refused"]) == stuck                          # every stuck one is still reported


def test_nothing_is_withheld_when_the_sweep_fits_under_the_cap():
    r = propose(closed_issues=[_closed(189, closer=_bare())])
    assert r["reopen_withheld"] == 0


def test_an_issue_proposed_for_closing_is_never_also_proposed_for_reopening():
    # the two sources are disjoint in production (open parks vs closed issues), but a close
    # landing between the two fetches would pair them — and executing both under --yes would
    # close then reopen the same issue, leaving two contradictory audit comments.
    r = propose(parked_issues=[_issue(9, ("parked",), NOW - 30 * DAY)],
                closed_issues=[_closed(9, closer=_bare())])
    assert [p["key"] for p in r["proposals"]] == ["issue:9"]


def test_reopen_inputs_are_never_mutated():
    closed = [_closed(189, closer=_bare())]
    snap = repr(closed)
    propose(closed_issues=closed)
    assert repr(closed) == snap


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


# --------------------------- metadata repair (issue #225) ---------------------------
# The owner's amendment of 2026-07-17: remediation for a mechanically-invalid issue belongs to the
# JANITOR, because its existing propose/approve contract fits a metadata fix exactly — "so the
# manual batch-repair run on 2026-07-16 becomes a janitor tap, never a hand job again."
#
# The rule that keeps this honest: **the janitor never invents a value.** Which KIND an issue is,
# and which TERRITORY it touches, are judgment — so the sweep does not guess one. It offers the
# closed set of values the repo itself declares as mutually-exclusive ALTERNATIVES, and the owner's
# tap picks one. A bulk `--yes` can therefore never apply two of them (which would create the very
# `type_duplicate` defect it was fixing); only an explicit per-key tap executes an alternative.

_AREAS = {"engine": ["skills/**"], "dashboard": ["dashboard/**"]}
_META = "## Loop metadata\ntouches: engine\n"


def _open_issue(num, labels=(), body=_META, title="An issue"):
    return {"number": num, "title": title, "body": body,
            "labels": [{"name": n} for n in labels], "updatedAt": _iso(NOW)}


def _meta_propose(issues, **kw):
    kw.setdefault("areas", _AREAS)
    kw.setdefault("touches_required", True)
    return propose(lint_issues=issues, **kw)["proposals"]


def _of_kind(props, kind="metadata"):
    return [p for p in props if p["kind"] == kind]


def test_a_valid_issue_earns_no_metadata_proposal():
    assert _of_kind(_meta_propose([_open_issue(5, ["type:build"])])) == []


def test_a_missing_type_label_offers_one_alternative_per_KIND_and_never_picks_one():
    props = _of_kind(_meta_propose([_open_issue(5, ["needs-owner"])]))
    assert {p["label"] for p in props} == {"type:build", "type:investigate",
                                           "type:diagnose-and-fix"}
    assert {p["action"] for p in props} == {"add-label"}
    assert {p["choose_group"] for p in props} == {"issue:5:type"}
    assert all(p["target"] == 5 for p in props)
    assert all(p["key"].startswith("meta:5:type=") for p in props)


def test_a_missing_touches_offers_one_alternative_per_DECLARED_AREA_plus_the_wildcard():
    props = _of_kind(_meta_propose([_open_issue(5, ["type:build"], body="## Goal\nship it\n")]))
    assert {p["value"] for p in props} == {"engine", "dashboard", "*"}
    assert {p["action"] for p in props} == {"set-body"}
    assert {p["choose_group"] for p in props} == {"issue:5:touches"}
    # every alternative carries the EXACT body it would write — nothing is derived at execute time
    for p in props:
        assert "## Loop metadata" in p["body"] and p["value"] in p["body"]


def test_a_bare_touches_line_is_the_ONE_fix_with_no_choice_to_make():
    # The author already wrote the value; only the heading that makes it parseable is missing. So
    # this is a single determined proposal, not a menu — and it is ungrouped, which means a bulk
    # `--yes` may execute it.
    props = _of_kind(_meta_propose([_open_issue(5, ["type:build"],
                                           body="## Goal\nship it\n\ntouches: engine\n")]))
    assert len(props) == 1
    assert props[0]["action"] == "set-body" and props[0].get("choose_group") is None
    assert props[0]["body"] == "## Goal\nship it\n\n## Loop metadata\ntouches: engine\n"


def test_an_undeclared_area_is_named_by_doctor_but_never_repaired_here():
    # The correct fix may well be to DECLARE that area in `.superlooper/config.json` — a
    # bright-line file no automatic path may write. Offering to overwrite the author's declaration
    # instead would be the janitor picking the other answer silently, by deleting words it did not
    # write. So it proposes nothing, and the lint still names it everywhere else.
    assert _of_kind(_meta_propose([_open_issue(5, ["type:build"],
                                          body="## Loop metadata\ntouches: plugin\n")])) == []


def test_every_proposal_says_WHY_in_the_words_the_lint_used():
    props = _of_kind(_meta_propose([_open_issue(5, ["needs-owner"])]))
    assert all("type:" in p["why"] for p in props)


def test_a_type_defect_with_no_mechanical_answer_is_never_proposed():
    # Two `type:` labels: which one to remove is the owner's call and the janitor has no verb that
    # could be right, so it proposes nothing rather than guessing. (doctor still NAMES it.)
    assert _of_kind(_meta_propose([_open_issue(5, ["type:build", "type:investigate"])])) == []


def test_nothing_is_proposed_when_the_repo_declares_no_areas():
    # With no declared vocabulary there is no value the janitor may offer, so the touches menu is
    # empty rather than invented. The TYPE menu is superlooper's own closed set and still stands.
    props = _of_kind(_meta_propose([_open_issue(5, ["type:build"], body="## Goal\nx\n")], areas=None))
    assert props == []
    assert _of_kind(_meta_propose([_open_issue(5, ["needs-owner"])], areas=None))


def test_an_in_flight_issue_is_never_touched():
    # Same exclusion as every other proposal kind: a lane that is building or mid-gate is off
    # limits, whichever record survives.
    ls = {"i5": {"status": "running", "branch": "sl/i5-x"}}
    assert _of_kind(_meta_propose([_open_issue(5, ["needs-owner"])], ls_issues=ls)) == []


def test_a_closed_or_wrong_typed_issue_is_skipped_not_raised_on():
    props = _of_kind(_meta_propose([None, "nonsense", {"number": None}, 42]))
    assert props == []


def test_metadata_proposals_are_deterministic():
    a = _of_kind(_meta_propose([_open_issue(5, ["needs-owner"]), _open_issue(3, ["needs-owner"])]))
    b = _of_kind(_meta_propose([_open_issue(3, ["needs-owner"]), _open_issue(5, ["needs-owner"])]))
    assert [p["key"] for p in a] == [p["key"] for p in b]


def test_a_previously_refused_metadata_key_is_held_back_like_any_other():
    res = propose(lint_issues=[_open_issue(5, ["needs-owner"])], areas=_AREAS, touches_required=True,
                  refused={"meta:5:type=type:build"})
    assert "meta:5:type=type:build" in res["refused"]
    assert "meta:5:type=type:build" not in {p["key"] for p in res["proposals"]}


def test_the_sweep_without_lint_inputs_is_exactly_the_pre_225_sweep():
    # An older caller (or one that could not read the issue list) proposes no metadata fixes at all
    # rather than a menu built on nothing.
    assert _of_kind(propose()["proposals"]) == []


def test_a_repo_that_does_not_require_touches_gets_no_touches_menu():
    props = _of_kind(_meta_propose([_open_issue(5, ["type:build"], body="## Goal\nx\n")],
                                   touches_required=False))
    assert props == []


def test_an_issue_this_sweep_proposes_CLOSING_is_never_also_offered_a_metadata_repair():
    # The two classes met when #225 merged with #229's reopen class, and they can contradict: an
    # aged parked issue is proposed for CLOSING, and the same issue (parked precisely because
    # nobody could launch it) is exactly the shape the metadata lint flags. Offering both in one
    # menu invites the owner to approve a paperwork fix onto an issue he just closed — the same
    # contradictory pair #229's own close/reopen guard exists to prevent.
    aged = _issue(9, ("parked",), NOW - 21 * DAY)
    res = propose(parked_issues=[aged],
                  lint_issues=[_open_issue(9, ["parked"], body="## Goal\nno metadata\n")],
                  areas=_AREAS, touches_required=True)
    keys = [p["key"] for p in res["proposals"]]
    assert keys == ["issue:9"]                       # the close, and ONLY the close
    assert not [k for k in keys if k.startswith("meta:9:")]


def test_a_metadata_repair_still_stands_for_an_issue_the_sweep_leaves_alone():
    # The guard above must bound itself to issues actually proposed for closing — a FRESH parked
    # issue is not proposed for close, so its paperwork fix is still on offer. (Without this, the
    # exclusion could quietly swallow the whole class for parked issues, which is most of them.)
    fresh = _issue(9, ("parked",), NOW - 2 * DAY)
    res = propose(parked_issues=[fresh],
                  lint_issues=[_open_issue(9, ["parked"], body="## Goal\nno metadata\n")],
                  areas=_AREAS, touches_required=True)
    keys = [p["key"] for p in res["proposals"]]
    assert "issue:9" not in keys                     # too fresh to close
    assert [k for k in keys if k.startswith("meta:9:")]


# ------------- merged PR, issue still open (issue #404) -------------
# Layer 3 of "no merged PR may leave its issue open": the DETECTION for pairs that already exist.
# The gate now refuses to merge a keyword-less PR and the engine verifies the closure right after
# the merge — but neither reaches backwards. Every pair created before those layers shipped (and
# every one their read failures could not confirm) is debris only a sweep can find, and the harm is
# specific: an open issue behind merged work reads as UNSTARTED, gets re-approved, and the work runs
# a second time against a definition of done that may not be idempotent.
#
# Propose-only, like every other class here.

def _merged(num, head, state="MERGED"):
    """A gh.sl_head_prs entry: {number, state, headRefName}."""
    return {"number": num, "state": state, "headRefName": head}


def _pair_propose(**kw):
    kw.setdefault("sl_prs", [_merged(12, "sl/i5-fix-thing")])
    kw.setdefault("lint_issues", [_open_issue(5, ["type:build"])])
    kw.setdefault("areas", _AREAS)
    kw.setdefault("touches_required", True)
    return propose(**kw)["proposals"]


def test_a_merged_pr_whose_issue_is_still_open_is_proposed_for_closing():
    props = _of_kind(_pair_propose(), "issue-merged-open")
    assert [p["key"] for p in props] == ["closemerged:5"]
    p = props[0]
    assert p["action"] == "close-issue" and p["target"] == 5 and p["title"] == "An issue"
    assert p["pr"] == 12
    # the why becomes the audit comment on the closed issue: it must NAME the merged PR, or the
    # close cannot be traced back to the work that justified it
    assert "#12" in p["why"] and "merged" in p["why"].lower()


def test_the_pair_key_never_collides_with_the_aged_park_close():
    # `issue:<n>` is the aged-park close. A shared key would conflate two different justifications
    # in the refused map and in a per-key tap — the same reason #229's reopen got its own prefix.
    props = _pair_propose()
    assert "issue:5" not in [p["key"] for p in props]


def test_an_open_issue_with_no_merged_pr_is_never_proposed():
    for prs in ([], [_merged(12, "sl/i5-x", state="OPEN")], [_merged(12, "sl/i5-x", "CLOSED")],
                [_merged(12, "sl/i9-other")]):
        assert _of_kind(_pair_propose(sl_prs=prs), "issue-merged-open") == [], prs


def test_a_closed_issue_is_never_proposed_because_it_is_not_in_the_open_set():
    # `lint_issues` IS the open-issue universe (gh.open_issues_all) — the same read the metadata
    # sweep already makes, so this class adds no second issue read. An issue absent from it is
    # either closed (nothing to do) or unread (fail closed to proposing nothing).
    assert _of_kind(_pair_propose(lint_issues=[]), "issue-merged-open") == []
    assert _of_kind(_pair_propose(lint_issues=None), "issue-merged-open") == []


def test_a_generation_branch_still_names_its_issue():
    # sl/i5-x-r2 is issue 5's rebuild — the same convention brief.branch_for/closures use.
    props = _of_kind(_pair_propose(sl_prs=[_merged(30, "sl/i5-fix-thing-r2")]),
                     "issue-merged-open")
    assert [p["target"] for p in props] == [5]


def test_an_issue_with_several_sl_prs_is_proposed_once_naming_a_merged_one():
    props = _of_kind(_pair_propose(sl_prs=[_merged(12, "sl/i5-x", state="CLOSED"),
                                           _merged(30, "sl/i5-x-r2")]), "issue-merged-open")
    assert [p["target"] for p in props] == [5]
    assert props[0]["pr"] == 30                      # the MERGED one, never the closed rebuild


def test_an_in_flight_lane_is_never_proposed():
    # a live lane on issue 5 (a rebuild after the first PR merged, say) is already being dealt with
    props = _of_kind(_pair_propose(ls_issues={"i5": {"status": "running", "branch": "sl/i5-x"}}),
                     "issue-merged-open")
    assert props == []


def test_a_reapproved_or_claimed_issue_is_never_proposed():
    # the owner's own word is on it. `agent-ready` on a merged-PR issue means he deliberately
    # re-opened and re-approved the work; `in-progress` means a lane holds it. Neither is debris.
    # (Both labels are REMOVED by the normal launch/merge path — an issue left open by a merged PR
    # carries neither — so this guard cannot silently swallow the class it is guarding.)
    for label in ("agent-ready", "in-progress"):
        props = _of_kind(_pair_propose(lint_issues=[_open_issue(5, ["type:build", label])]),
                         "issue-merged-open")
        assert props == [], label


def test_an_issue_this_sweep_proposes_closing_as_aged_debris_is_not_proposed_twice():
    # two justifications, one action: emitting both would put the same close in the menu twice.
    aged = _issue(5, ("parked",), NOW - 21 * DAY)
    keys = [p["key"] for p in propose(parked_issues=[aged], sl_prs=[_merged(12, "sl/i5-x")],
                                      lint_issues=[_open_issue(5, ["parked"])],
                                      areas=_AREAS, touches_required=True)["proposals"]]
    assert "issue:5" in keys and "closemerged:5" not in keys


def test_pairs_are_deterministic_and_sorted_by_issue():
    props = _of_kind(_pair_propose(
        sl_prs=[_merged(30, "sl/i9-b"), _merged(12, "sl/i5-a"), _merged(20, "sl/i7-c")],
        lint_issues=[_open_issue(9), _open_issue(5), _open_issue(7)]), "issue-merged-open")
    assert [p["target"] for p in props] == [5, 7, 9]


def test_a_refused_pair_is_held_back_not_silently_retried():
    r = propose(sl_prs=[_merged(12, "sl/i5-x")], lint_issues=[_open_issue(5, ["type:build"])],
                areas=_AREAS, touches_required=True, refused=frozenset({"closemerged:5"}))
    assert _of_kind(r["proposals"], "issue-merged-open") == []
    assert r["refused"] == ["closemerged:5"]


@pytest.mark.parametrize("bad", [None, "not a list", 42, [None], ["x"], [{}],
                                 [{"number": 12}], [{"headRefName": "sl/i5-x"}],
                                 [{"number": "12", "state": "MERGED", "headRefName": "sl/i5-x"}],
                                 [{"number": 0, "state": "MERGED", "headRefName": "sl/i5-x"}],
                                 [{"number": 12, "state": "MERGED", "headRefName": 42}]])
def test_wrong_typed_sl_prs_propose_nothing(bad):
    assert _of_kind(_pair_propose(sl_prs=bad), "issue-merged-open") == []


def test_a_wrong_typed_open_issue_entry_is_skipped_not_proposed():
    for bad in ([None], [42], [{}], [{"number": None}], [{"number": "5"}], [{"number": 0}]):
        assert _of_kind(_pair_propose(lint_issues=bad), "issue-merged-open") == [], bad


def test_the_merged_open_class_is_capped_per_sweep_and_the_remainder_is_reported():
    """UNLIKE a branch delete or an aged park, this class's population can be large ALL AT ONCE
    through no fault of the owner — and the headline case is exactly that. GitHub honors closing
    keywords only for merges into the DEFAULT branch, so a repo whose `dev_branch` is not the
    default has never had one honored: every merged issue is a pair, from adoption. `--yes` is a
    blanket approval, and one that fires hundreds of closes (each a comment, each a notification,
    with no single undo) is the harm REOPEN_SWEEP_CAP exists for, pointed the other way."""
    prs = [_merged(100 + n, "sl/i%d-x" % n) for n in range(1, 26)]
    issues = [_open_issue(n) for n in range(1, 26)]
    r = propose(sl_prs=prs, lint_issues=issues, areas=_AREAS, touches_required=True)
    pairs = _of_kind(r["proposals"], "issue-merged-open")
    assert len(pairs) == janitor.MERGED_OPEN_SWEEP_CAP
    assert r["merged_open_withheld"] == 25 - janitor.MERGED_OPEN_SWEEP_CAP
    # the OLDEST debris goes first, and the order is stable — nothing in this class's inputs
    # carries when the merge happened, so a recency order would be invented, not read
    assert [p["target"] for p in pairs] == list(range(1, 11))


def test_a_refused_pair_keeps_its_report_without_occupying_a_cap_slot():
    # a handful of stuck keys must never starve the class: the cap bounds ACTIONS, and a
    # previously-refused key is a report (the same split the reopen class makes).
    prs = [_merged(100 + n, "sl/i%d-x" % n) for n in range(1, 26)]
    issues = [_open_issue(n) for n in range(1, 26)]
    r = propose(sl_prs=prs, lint_issues=issues, areas=_AREAS, touches_required=True,
                refused=frozenset({"closemerged:3"}))
    pairs = _of_kind(r["proposals"], "issue-merged-open")
    assert "closemerged:3" not in [p["key"] for p in pairs]
    assert r["refused"] == ["closemerged:3"]
    assert len(pairs) == janitor.MERGED_OPEN_SWEEP_CAP   # the refusal cost the class no slot


def test_a_sweep_below_the_cap_reports_nothing_withheld():
    r = propose(sl_prs=[_merged(12, "sl/i5-x")], lint_issues=[_open_issue(5)],
                areas=_AREAS, touches_required=True)
    assert r["merged_open_withheld"] == 0
    # ...and a sweep that fails closed on an unreadable exclusion source still answers the key
    assert propose(ls_issues="not a dict")["merged_open_withheld"] == 0


def test_an_issue_in_the_owners_attention_queue_is_never_proposed():
    """A park label means he is holding the issue open on purpose — closing it would answer a
    question he has not answered. Combined with the cap above, this is the one way the class could
    otherwise undo an owner decision under `--yes`. It cannot swallow the class it guards: park and
    merge are different terminal paths, so an issue a merged PR left open carries no park label."""
    for label in janitor.PARK_LABELS:
        props = _of_kind(_pair_propose(lint_issues=[_open_issue(5, ["type:build", label])]),
                         "issue-merged-open")
        assert props == [], label


def test_a_reopen_and_a_close_of_the_same_issue_never_share_one_menu():
    """Opposite actions on one issue, both bulk-approvable under `--yes`, is exactly the
    contradiction #229's own close/reopen guard exists to refuse. It takes a race to reach — the
    closed set and the open set are separate reads, so the issue must be closed for one and open for
    the other — but every other close class is guarded against it and this one must be too."""
    r = propose(closed_issues=[_closed(5, closer=_bare())],
                sl_prs=[_merged(12, "sl/i5-x")], lint_issues=[_open_issue(5, ["type:build"])],
                areas=_AREAS, touches_required=True)
    keys = [p["key"] for p in r["proposals"]]
    assert "reopen:5" in keys                        # the reopen is proposed, as before
    assert "closemerged:5" not in keys               # ...and never its opposite in the same sweep


def test_a_duplicated_open_issue_entry_is_proposed_once():
    """`lint_issues` is a raw gh list, and a duplicated entry would otherwise emit `closemerged:5`
    twice. A duplicated close is not harmless: under `--yes` the second `gh issue close` returns
    nonzero, which lands the key in the refused map and blocks the class for that issue until
    `--retry-refused`. Every sibling class dedups inside its own emit loop; so does this one."""
    iss = _open_issue(5, ["type:build"])
    props = _of_kind(_pair_propose(lint_issues=[iss, dict(iss), dict(iss)]), "issue-merged-open")
    assert [p["key"] for p in props] == ["closemerged:5"]
