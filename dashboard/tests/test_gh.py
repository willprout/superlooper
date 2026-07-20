"""The GitHub adapter (``lib/gh.py``) — the dashboard's only egress to GitHub.

Two contracts every test here defends:
  1. **Fail closed, always.** A timeout, a missing binary, a nonzero rc, or unparseable JSON
     yields the EMPTY-but-typed result (``[]`` / ``{}`` / ``False`` / ``None``). Acting on nothing
     is safe; acting on a half-read GitHub state is not.
  2. **Right repo, every call.** The dashboard watches MANY repos, so every call is pinned to an
     explicit ``owner/name`` (injected as ``GH_REPO``); a mis-pinned call would talk to the wrong
     repo. The fake logs the pin so we assert it.

Tests run against ``tests/fakes/fake-gh`` (a fixture-backed stand-in). The autouse conftest fixture
points ``SL_GH`` at an absent path by default; each test that wants the fake overrides ``SL_GH``
in-body with ``monkeypatch`` (runs after the autouse fixture and wins — the sanctioned override).
No test here reaches the real ``gh`` or the network.
"""
import json
import os
from pathlib import Path

import pytest

import gh

FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"
REPO = "will-titan/command-center"


def _use_fake(monkeypatch, fixdir):
    """Point the adapter at the fake harness with ``fixdir`` as its fixture directory."""
    monkeypatch.setenv("SL_GH", str(FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))


def _write(fixdir, name, obj):
    (Path(fixdir) / name).write_text(json.dumps(obj))


def _mutations(fixdir):
    p = Path(fixdir) / "mutations.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _calls(fixdir):
    p = Path(fixdir) / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


# --------------------------- reads: happy path ---------------------------

def test_ready_issues_returns_open_agent_ready_issues(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list_agent-ready.json",
           [{"number": 4, "title": "T4", "labels": [{"name": "agent-ready"}], "body": "b"}])
    out = gh.ready_issues(REPO)
    assert out == [{"number": 4, "title": "T4",
                    "labels": [{"name": "agent-ready"}], "body": "b"}]


def test_open_issues_filters_by_label(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list_in-progress.json", [{"number": 7, "title": "running"}])
    out = gh.open_issues(REPO, label="in-progress")
    assert out == [{"number": 7, "title": "running"}]
    # the label rode through to gh as --label
    argv = _calls(tmp_path)[-1]["argv"]
    assert "--label" in argv and argv[argv.index("--label") + 1] == "in-progress"


def test_open_issues_without_label_lists_all_open(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [{"number": 1}, {"number": 2}])
    assert gh.open_issues(REPO) == [{"number": 1}, {"number": 2}]
    assert "--label" not in _calls(tmp_path)[-1]["argv"]


def test_issue_returns_single_issue_dict(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_view_5.json",
           {"number": 5, "title": "five", "labels": [{"name": "parked"}], "body": "x"})
    assert gh.issue(REPO, 5)["title"] == "five"


def test_pr_for_branch_returns_state_mergeable_checks(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "pr_list_sl-i4-x.json", [{
        "number": 20, "state": "OPEN", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        "headRefName": "sl-i4-x"}])
    pr = gh.pr_for_branch(REPO, "sl-i4-x")
    assert pr["state"] == "OPEN"
    assert pr["mergeable"] == "MERGEABLE"
    assert pr["statusCheckRollup"][0]["conclusion"] == "SUCCESS"


def test_pr_for_branch_none_when_no_pr(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "pr_list.json", [])
    assert gh.pr_for_branch(REPO, "no-such-branch") == {}


def test_pr_for_branch_reads_diff_size_fields(tmp_path, monkeypatch):
    # issue #48 (absorbs #47): the PR read carries its own diff size, so a landed flight's cargo
    # survives after its worktree is cleaned up. The size fields ride the SAME single read.
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "pr_list_sl-i4-x.json", [{
        "number": 20, "state": "MERGED", "mergeable": "MERGEABLE", "headRefName": "sl-i4-x",
        "statusCheckRollup": [], "additions": 340, "deletions": 12, "changedFiles": 7}])
    pr = gh.pr_for_branch(REPO, "sl-i4-x")
    assert (pr["additions"], pr["deletions"], pr["changedFiles"]) == (340, 12, 7)
    # the size fields were actually requested from gh (one read, not a second call)
    argv = _calls(tmp_path)[-1]["argv"]
    fields = argv[argv.index("--json") + 1].split(",")
    assert {"additions", "deletions", "changedFiles"} <= set(fields)


def test_issue_comments_returns_list(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_comments_4.json", {"comments": [{"body": "hello"}]})
    assert gh.issue_comments(REPO, 4) == [{"body": "hello"}]


def test_pr_comments_returns_list(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "pr_comments_20.json", {"comments": [{"body": "lgtm"}]})
    assert gh.pr_comments(REPO, 20) == [{"body": "lgtm"}]


# --------------------------- reads: reachability probe (issue #38) ---------------------------
# open_issues_probe is the ONE honest signal that separates "GitHub answered: no open issues" from
# "GitHub is unreachable / refused". Every other parser fails closed to the same empty value for
# both, which is safe for acting but LOSES the distinction the field needs to tell a genuine
# all-clear from a dead data link. The probe surfaces the fail-closed rc instead of swallowing it —
# and it IS the open-issue read the snapshot already makes every poll, so no extra gh call is added.

def test_open_issues_probe_reports_reachable_on_a_real_answer(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [{"number": 1}, {"number": 2}])
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == [{"number": 1}, {"number": 2}]
    assert reachable is True                      # gh answered


def test_open_issues_probe_reports_reachable_on_an_EMPTY_answer(tmp_path, monkeypatch):
    # The crux (issue #38): a real, successful, EMPTY answer is reachable=True — an honest all-clear,
    # NOT the unreachable state. Distinguishing this from the failures below is the whole point.
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [])
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is True


def test_open_issues_probe_unreachable_on_missing_binary(tmp_path, monkeypatch):
    # SL_GH stays at the conftest's neutralized absent path — the binary can't be found (rc 127).
    monkeypatch.setenv("GH_FIXTURES", str(tmp_path))
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is False                     # a missing gh IS the first-run unreachable gap


def test_open_issues_probe_unreachable_on_nonzero_rc(tmp_path, monkeypatch):
    # An unauthenticated / erroring gh exits nonzero — the second most likely first-run gap.
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    _write(tmp_path, "issue_list.json", [{"number": 1}])   # present but never reached
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is False


def test_open_issues_probe_unreachable_on_timeout(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [{"number": 1}])
    monkeypatch.setenv("GH_SLEEP", "2")
    monkeypatch.setattr(gh, "_DEFAULT_TIMEOUT", 0.3)
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is False                     # a hung gh is unreachable, never a false all-clear


def test_open_issues_probe_unreachable_on_unparseable_json(tmp_path, monkeypatch):
    # gh exited 0 but handed back junk — we have NO trustworthy queue read, so this is NOT a genuine
    # all-clear (Codex review): reachable means "gh gave us a usable open-issue list," and unparseable
    # output fails that just as a nonzero rc does. The list still fails closed to empty.
    _use_fake(monkeypatch, tmp_path)
    (tmp_path / "issue_list.json").write_text("not json {{{")
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is False


def test_open_issues_probe_unreachable_on_wrong_typed_json(tmp_path, monkeypatch):
    # Valid JSON but the wrong SHAPE (a dict where a list is required) is also not a usable answer —
    # reachable is True only for a real open-issue LIST (empty or not), never a false all-clear over
    # a read we couldn't use.
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", {"not": "a list"})
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is False


def test_open_issues_probe_carries_the_label_through(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list_agent-ready.json", [{"number": 4}])
    issues, reachable = gh.open_issues_probe(REPO, label="agent-ready")
    assert issues == [{"number": 4}] and reachable is True
    argv = _calls(tmp_path)[-1]["argv"]
    assert argv[argv.index("--label") + 1] == "agent-ready"


def test_open_issues_delegates_to_the_probe_and_drops_reachability(tmp_path, monkeypatch):
    # open_issues stays the list-only surface every existing caller uses — it is now the probe with
    # the reachability dropped, so the two can never diverge on the query they send gh.
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [{"number": 9}])
    assert gh.open_issues(REPO) == [{"number": 9}]


# --------------------------- reads: repo pinning ---------------------------

def test_every_read_is_pinned_to_the_given_repo(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [])
    other = "will-titan/superlooper"
    gh.open_issues(REPO)
    gh.open_issues(other)
    repos = [c["repo"] for c in _calls(tmp_path)]
    assert repos == [REPO, other]      # each call talked to the repo it was asked about


# --------------------------- reads: fail closed ---------------------------

def test_reads_fail_closed_on_missing_binary(tmp_path, monkeypatch):
    # SL_GH stays at the conftest's neutralized absent path — the binary can't be found.
    monkeypatch.setenv("GH_FIXTURES", str(tmp_path))
    assert gh.ready_issues(REPO) == []
    assert gh.open_issues(REPO) == []
    assert gh.issue(REPO, 1) == {}
    assert gh.pr_for_branch(REPO, "b") == {}
    assert gh.issue_comments(REPO, 1) == []
    assert gh.pr_comments(REPO, 1) == []


def test_reads_fail_closed_on_nonzero_rc(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    _write(tmp_path, "issue_list.json", [{"number": 1}])   # present but never reached
    assert gh.open_issues(REPO) == []
    assert gh.pr_for_branch(REPO, "b") == {}


def test_reads_fail_closed_on_unparseable_json(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    (tmp_path / "issue_list.json").write_text("not json {{{")
    assert gh.open_issues(REPO) == []


def test_reads_fail_closed_on_wrong_typed_json(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", {"not": "a list"})   # a dict where a list is required
    assert gh.open_issues(REPO) == []


def test_reads_fail_closed_on_timeout(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    _write(tmp_path, "issue_list.json", [{"number": 1}])
    monkeypatch.setenv("GH_SLEEP", "2")                # fake sleeps 2s
    monkeypatch.setattr(gh, "_DEFAULT_TIMEOUT", 0.3)   # adapter gives up at 0.3s
    assert gh.open_issues(REPO) == []                  # timed out -> empty, not a hang


# --------------------------- writes: happy path + recording ---------------------------

def test_set_labels_records_add_and_remove(tmp_path, monkeypatch):
    # Each label operation is its OWN gh edit (issue #114: a single all-or-nothing edit lets one
    # repo-absent remove sink the add). The add is one edit; each remove is its own edit; every one
    # is pinned to the right issue.
    _use_fake(monkeypatch, tmp_path)
    assert gh.set_labels(REPO, 4, add=["agent-ready"], remove=["parked", "needs-owner"]) is True
    muts = [m for m in _mutations(tmp_path) if m["kind"] == "set_labels"]
    assert all(m["num"] == "4" for m in muts)
    # The ADD lands FIRST (the load-bearing order: agent-ready is applied before any blocker is
    # cleared, and a failed add short-circuits before firing doomed removes).
    assert muts[0]["add"] == "agent-ready" and muts[0]["remove"] is None
    adds = [m for m in muts if m["add"]]
    assert [m["add"] for m in adds] == ["agent-ready"]           # exactly one add edit
    assert all(m["remove"] is None for m in adds)                # the add edit carries no remove
    removes = [m["remove"] for m in muts if m["remove"]]
    assert removes == ["parked", "needs-owner"]                  # one edit per removed label, in order
    assert all(m["add"] is None for m in muts if m["remove"])    # a remove edit carries no add


def test_set_labels_noop_when_nothing_to_change(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    assert gh.set_labels(REPO, 4) is True     # nothing to do -> True, and no gh call made
    assert _calls(tmp_path) == []


# --------------------------- writes: a repo-nonexistent remove must not sink the write (issue #114) ---------------------------
# gh's ``issue edit --remove-label X`` HARD-FAILS when X is not defined in the REPO's label set —
# even when the issue never carried it. A completed #58 rename removes the legacy id from the repo,
# so a batched remove that still names it errored out and the agent-ready add never landed (every
# Approve tap died with "nothing changed"). Removing a label the repo no longer defines is vacuously
# done; it must not be treated as a failure, and it must not take the add / the other removes with it.

def _removed_labels(muts):
    """Every label the adapter actually removed, flattened across however many edit calls it made
    (one batched edit, or one-per-label — either shape answers 'did label X get removed?')."""
    out = set()
    for m in muts:
        if m["kind"] == "set_labels" and m.get("remove"):
            out.update(m["remove"].split(","))
    return out


def _added_labels(muts):
    out = set()
    for m in muts:
        if m["kind"] == "set_labels" and m.get("add"):
            out.update(m["add"].split(","))
    return out


def test_set_labels_tolerates_a_repo_absent_remove_and_still_lands_add_and_others(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_LABEL_NOT_IN_REPO", "needs-william")   # a repo that FINISHED the #58 rename
    ok = gh.set_labels(REPO, 4, add=["agent-ready"],
                       remove=["parked", "needs-owner", "needs-william"])
    assert ok is True                                    # a vacuous remove is NOT a failure
    muts = _mutations(tmp_path)
    assert "agent-ready" in _added_labels(muts)          # the add LANDED (the whole point of Approve)
    removed = _removed_labels(muts)
    assert {"parked", "needs-owner"} <= removed          # the real removes LANDED too
    assert "needs-william" not in removed                # the repo-absent one was never recorded


def test_set_labels_still_removes_the_legacy_label_when_the_repo_defines_it(tmp_path, monkeypatch):
    # Mid-migration: the repo STILL carries needs-william. The fix must not stop clearing it (the
    # legacy id was included on purpose so a mid-migration repo clears cleanly).
    _use_fake(monkeypatch, tmp_path)
    ok = gh.set_labels(REPO, 4, add=["agent-ready"], remove=["parked", "needs-william"])
    assert ok is True
    removed = _removed_labels(_mutations(tmp_path))
    assert "needs-william" in removed and "parked" in removed


def test_set_labels_surfaces_a_genuine_remove_failure_no_false_ok(tmp_path, monkeypatch):
    # A NON "not found" failure on a remove (auth/network/500) is a real failure — it must be
    # surfaced, never swallowed as if it were a vacuous repo-absent remove.
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL_REMOVE", "1")
    assert gh.set_labels(REPO, 4, add=["agent-ready"], remove=["parked"]) is False


def test_label_absent_classifier_only_tolerates_the_benign_gh_not_found_shape():
    # The tolerance must match gh's EXACT benign shape (``'<label>' not found``) and nothing looser —
    # a genuine 404/auth/repo error that merely ECHOES the label name must NOT be swallowed (that
    # would be a false-ok, the one thing this fix must never introduce). Fail closed on ambiguity.
    assert gh._label_absent_from_repo("failed to update issue #4: 'needs-william' not found",
                                      "needs-william") is True
    assert gh._label_absent_from_repo("HTTP 404: Not Found while removing needs-owner",
                                      "needs-owner") is False          # 404 echoing the label ≠ benign
    assert gh._label_absent_from_repo("gh: could not resolve to a Repository 'needs-owner' not-found",
                                      "needs-owner") is False          # not the exact benign shape
    assert gh._label_absent_from_repo("", "parked") is False           # no stderr ≠ benign


def test_set_labels_fails_closed_when_the_add_fails(tmp_path, monkeypatch):
    # Auth/network fails everything, including the add — the write did not land, so ok is False and
    # the failure toast fires. (The add is authoritative; a failed add is never a success.)
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.set_labels(REPO, 4, add=["agent-ready"], remove=["parked", "needs-william"]) is False


def test_comment_records_body(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    assert gh.comment(REPO, 4, "Approved by William via command-center, 2026-07-07.") is True
    mut = _mutations(tmp_path)[-1]
    assert mut["kind"] == "comment"
    assert mut["num"] == "4"
    assert "Approved by William" in mut["body"]


def test_create_issue_returns_new_number(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "123")
    num = gh.create_issue(REPO, "flag: something odd", "body text", labels=["flag"])
    assert num == 123
    mut = _mutations(tmp_path)[-1]
    assert mut["kind"] == "create_issue"
    assert mut["title"] == "flag: something odd"
    assert mut["labels"] == "flag"


def test_close_issue_records_close_and_comment(tmp_path, monkeypatch):
    # drop = close + audit comment in one atomic gh call (the only destructive verb).
    _use_fake(monkeypatch, tmp_path)
    assert gh.close_issue(REPO, 5, comment="Dropped by William via command-center, 2026-07-07.") is True
    mut = _mutations(tmp_path)[-1]
    assert mut["kind"] == "close_issue"
    assert mut["num"] == "5"
    assert "Dropped by William" in mut["comment"]


def test_close_issue_without_comment_still_closes(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    assert gh.close_issue(REPO, 5) is True
    mut = _mutations(tmp_path)[-1]
    assert mut["kind"] == "close_issue"
    assert mut["comment"] is None
    # no --comment flag rode through when none was given
    assert "--comment" not in _calls(tmp_path)[-1]["argv"]


def test_close_issue_is_pinned_to_repo(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    gh.close_issue(REPO, 5)
    assert _calls(tmp_path)[-1]["repo"] == REPO


def test_close_issue_fails_closed_to_false_on_error(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.close_issue(REPO, 5, comment="x") is False


def test_create_label_records_and_forces(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    assert gh.create_label(REPO, "flag", "d73a4a", "flagged by William") is True
    mut = _mutations(tmp_path)[-1]
    assert mut["kind"] == "create_label"
    assert mut["name"] == "flag"
    assert mut["force"] is True      # create-or-update, so first use is idempotent


def test_writes_are_pinned_to_repo(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    gh.comment(REPO, 4, "x")
    assert _calls(tmp_path)[-1]["repo"] == REPO


# --------------------------- writes: fail closed ---------------------------

def test_writes_fail_closed_to_false_on_error(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.set_labels(REPO, 4, add=["x"]) is False
    assert gh.comment(REPO, 4, "x") is False
    assert gh.create_label(REPO, "flag", "abc", "d") is False


def test_create_issue_returns_none_on_error(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.create_issue(REPO, "t", "b") is None


def test_create_issue_returns_none_when_number_unparseable(tmp_path, monkeypatch):
    # A zero-rc create whose stdout carries no issue number must not fabricate one.
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "")   # fake prints ".../issues/\n" -> no digits
    assert gh.create_issue(REPO, "t", "b") is None


# --------------------------- per-client GitHub API-burn telemetry (issue #15) ---------------------------
# The dashboard adapter records one bounded local telemetry row per gh subprocess (client="dashboard")
# — the same shape the runner records — plus a FREE rate-limit snapshot via rate_limit_snapshot().
# Enabled explicitly via gh.set_telemetry_enabled() — OFF by default, so ordinary gh tests record
# nothing. Rows land in each repo's own state home (the dashboard watches many repos), never the repo.

import config
import telemetry


def _tele(home):
    return telemetry.read(home, "dashboard")


def test_telemetry_off_by_default_writes_nothing(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    _write(tmp_path, "issue_list.json", [])
    gh.open_issues(REPO)
    assert list((tmp_path / "sl").glob("**/gh-telemetry-*.jsonl")) == []


def test_telemetry_records_one_dashboard_row_per_gh_call(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    monkeypatch.setattr(gh, "_telemetry_enabled", False, raising=False)   # isolate + auto-restore
    _write(tmp_path, "issue_list_agent-ready.json", [{"number": 4}])
    gh.set_telemetry_enabled()
    gh.ready_issues(REPO)
    home = config.state_home(REPO)
    rows = [r for r in _tele(home) if r["kind"] == "call"]
    assert len(rows) == 1
    r = rows[0]
    assert r["client"] == "dashboard"
    assert r["repo"] == REPO                          # the pinned repo rode into the row
    assert r["op"] == "ready_issues"
    assert r["family"] == "issue list"
    assert r["api"] == "graphql"
    assert r["status"] == "success"


def test_telemetry_writes_under_each_repos_own_state_home(tmp_path, monkeypatch):
    # The dashboard watches many repos; each call's row lands under ITS repo's state home, not a
    # single dashboard-wide file.
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    monkeypatch.setattr(gh, "_telemetry_enabled", False, raising=False)
    other = "will-titan/superlooper"
    _write(tmp_path, "issue_list.json", [])
    gh.set_telemetry_enabled()
    gh.open_issues(REPO)
    gh.open_issues(other)
    assert [r["repo"] for r in _tele(config.state_home(REPO))] == [REPO]
    assert [r["repo"] for r in _tele(config.state_home(other))] == [other]


def test_telemetry_distinguishes_rate_limited_from_refused_and_success(tmp_path, monkeypatch):
    # The #8 distinction the burn record must keep: a rate-limit refusal is a DIFFERENT status than a
    # generic refusal, and both are visibly NOT the "success" a genuine empty answer carries.
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    monkeypatch.setattr(gh, "_telemetry_enabled", False, raising=False)
    _write(tmp_path, "issue_list.json", [])
    gh.set_telemetry_enabled()
    # a rate-limit 403 (fake-gh sets the exact stderr)
    monkeypatch.setenv("GH_FAIL", "1")
    monkeypatch.setenv("GH_FAIL_STDERR", "HTTP 403: API rate limit exceeded for user")
    gh.open_issues(REPO)
    monkeypatch.delenv("GH_FAIL", raising=False)
    monkeypatch.delenv("GH_FAIL_STDERR", raising=False)
    gh.open_issues(REPO)                              # a clean, empty answer
    statuses = [r["status"] for r in _tele(config.state_home(REPO)) if r["kind"] == "call"]
    assert statuses == ["rate_limited", "success"]


def test_rate_limit_snapshot_records_a_free_snapshot(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    monkeypatch.setattr(gh, "_telemetry_enabled", False, raising=False)
    _write(tmp_path, "api_rate_limit.json", {"resources": {
        "core": {"limit": 5000, "used": 10, "remaining": 4990, "reset": 1700000000},
        "graphql": {"limit": 5000, "used": 3000, "remaining": 2000, "reset": 1700000123}}})
    gh.set_telemetry_enabled()
    assert gh.rate_limit_snapshot(REPO) is True
    snaps = [r for r in _tele(config.state_home(REPO)) if r["kind"] == "rate_limit"]
    assert len(snaps) == 1
    assert snaps[0]["client"] == "dashboard" and snaps[0]["ok"] is True
    assert snaps[0]["resources"]["graphql"]["remaining"] == 2000
    # the snapshot read hit the FREE rate_limit endpoint, not a quota-bearing surface
    assert _calls(tmp_path)[-1]["argv"][:2] == ["api", "rate_limit"]


def test_rate_limit_snapshot_marks_unreachable(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl"))
    monkeypatch.setattr(gh, "_telemetry_enabled", False, raising=False)
    gh.set_telemetry_enabled()
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.rate_limit_snapshot(REPO) is False
    snap = [r for r in _tele(config.state_home(REPO)) if r["kind"] == "rate_limit"][0]
    assert snap["ok"] is False and snap["resources"] == {}


def test_config_import_is_available_for_home_resolution(tmp_path, monkeypatch):
    # gh resolves each row's home via config.state_home — pin that the module is wired.
    import config as _config
    assert gh.config is _config
