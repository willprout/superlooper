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


def test_open_issues_probe_reachable_but_empty_on_unparseable_json(tmp_path, monkeypatch):
    # gh RAN and exited 0 but handed back junk — it IS reachable (a real, if useless, answer); the
    # list still fails closed to empty. rc, not parseability, is the reachability signal.
    _use_fake(monkeypatch, tmp_path)
    (tmp_path / "issue_list.json").write_text("not json {{{")
    issues, reachable = gh.open_issues_probe(REPO)
    assert issues == []
    assert reachable is True


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
    _use_fake(monkeypatch, tmp_path)
    assert gh.set_labels(REPO, 4, add=["agent-ready"], remove=["parked", "needs-william"]) is True
    mut = _mutations(tmp_path)[-1]
    assert mut == {"kind": "set_labels", "num": "4",
                   "add": "agent-ready", "remove": "parked,needs-william"}


def test_set_labels_noop_when_nothing_to_change(tmp_path, monkeypatch):
    _use_fake(monkeypatch, tmp_path)
    assert gh.set_labels(REPO, 4) is True     # nothing to do -> True, and no gh call made
    assert _calls(tmp_path) == []


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
