"""GitHub adapter: parser correctness on captured real gh --json shapes (served by fake-gh),
mutation recording, and the two non-negotiables — timeout and nonzero-rc both fail CLOSED to
empty-but-typed results (act on nothing)."""
import json
import shutil
from pathlib import Path

import pytest

import gh
import issues

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"


@pytest.fixture
def ghenv(tmp_path, monkeypatch):
    """Copy the committed real-shape fixtures into a WRITABLE tmp dir (so mutations.jsonl writes
    don't touch the committed fixtures), and point gh.py at fake-gh over that dir."""
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))
    monkeypatch.delenv("GH_FAIL", raising=False)
    monkeypatch.delenv("GH_SLEEP", raising=False)
    return fixdir


def _mutations(fixdir):
    p = fixdir / "mutations.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _calls(fixdir):
    p = fixdir / "calls.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _after(argv, flag):
    """The value following --flag in an argv list (or None) — pins the exact gh invocation."""
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


# --------------------------- reads: parser correctness on real shapes ---------------------------

def test_ready_issues_parses_real_shape(ghenv):
    lst = gh.ready_issues()
    assert [i["number"] for i in lst] == [101, 102, 103]
    # the raw gh label shape [{ "name": ... }] is preserved for the caller's parse_issue
    assert lst[0]["labels"][0]["name"] == "type:build"
    # end-to-end: parse_issue consumes the raw gh dict directly
    p = issues.parse_issue(lst[1])
    assert p["type"] == "build" and p["priority"] == 1 and p["touches"] == ["api"]


def test_issue_view(ghenv):
    d = gh.issue(123)
    assert d["number"] == 123 and d["title"] == "Render the widget"


def test_issue_comments(ghenv):
    cr = gh.issue_comments(123)
    assert cr.ok is True                         # GitHub answered
    assert len(cr.comments) == 2
    assert cr.comments[1]["body"].startswith("<!-- superlooper-investigation -->")


def test_pr_for_branch_shape(ghenv):
    rd = gh.pr_for_branch("sl/i123-render-the-widget")
    assert rd.ok is True                         # GitHub answered (issue #61: refused != answered)
    pr = rd.pr
    assert pr["number"] == 555 and pr["state"] == "OPEN" and pr["mergeable"] == "MERGEABLE"
    assert pr["labels"] == []   # requested field: the gate reads the `preserve` label from here
    assert {f["path"] for f in pr["files"]} == {"src/components/Widget.tsx", "src/api/widget.py"}
    # the rollup carries BOTH gh shapes: CheckRun (name/conclusion) and StatusContext (context/state)
    names = {c.get("name") or c.get("context") for c in pr["statusCheckRollup"]}
    assert names == {"review/local-gate", "quality-gate"}


def test_pr_comments_has_review_marker(ghenv):
    cr = gh.pr_comments(555)
    assert cr.ok is True
    assert any(c["body"].startswith("<!-- superlooper-review -->") for c in cr.comments)


# --------------------------- comment reads: refused != answered-empty (issue #21) ----------
# The load-bearing distinction of #21: a REFUSED comment read (rate-limit/403/5xx/timeout, or a
# wrong-typed/unparseable body) must be distinguishable from GitHub ANSWERING "no comments". The
# old contract collapsed both to [], so a single stale/refused read false-parked a finished
# investigation. Now the adapter returns CommentRead(comments, ok): ok is True ONLY on a clean
# answer.

def test_issue_comments_refused_is_not_answered_empty(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    cr = gh.issue_comments(1)
    assert cr.ok is False and cr.comments == []   # refused: caller must NOT read this as "no marker"


def test_issue_comments_answered_empty_is_ok(ghenv):
    (ghenv / "issue_comments_1.json").write_text('{"comments": []}')
    cr = gh.issue_comments(1)
    assert cr.ok is True and cr.comments == []    # genuine empty thread: an authoritative answer


def test_issue_comments_timeout_is_refused(ghenv, monkeypatch):
    monkeypatch.setenv("GH_SLEEP", "2")
    # gh.py's 30s hard timeout is too slow to exercise here; drive the low-level read directly.
    cr = gh._comment_read(["issue", "view", "1", "--json", "comments"], timeout=0.4)
    assert cr.ok is False and cr.comments == []


def test_comment_reads_wrong_typed_body_is_refused(ghenv):
    # A 200 whose body is valid JSON but the WRONG shape ("comments" missing / not a list) is
    # NOT a clean answer — it must fail closed to ok=False, never to an authoritative empty.
    for fixture, call in (("issue_comments_1.json", lambda: gh.issue_comments(1)),
                          ("pr_comments_1.json", lambda: gh.pr_comments(1))):
        (ghenv / fixture).write_text('"a bare string, wrong type"')
        cr = call()
        assert cr.ok is False and cr.comments == []
        (ghenv / fixture).write_text('{"comments": "not a list"}')
        cr = call()
        assert cr.ok is False and cr.comments == []


# ------------- watchdog reads: refused != answered-empty (issue #92, the #21/#61 contract) ------
# The no-progress detector must distinguish a REFUSED list read from a genuinely empty one: a
# probe-ok-but-refused read reset its clocks and stood episodes down (the fail-closed-empty-read-
# as-truth trap gh.probe's own docstring warns about). These read-health variants carry `ok`.

def test_ready_issues_health_answered(ghenv):
    rd = gh.ready_issues_health()
    assert rd.ok is True
    assert [i["number"] for i in rd.value] == [101, 102, 103]


def test_ready_issues_health_refused_is_not_answered_empty(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    rd = gh.ready_issues_health()
    assert rd.ok is False and rd.value == []   # refused: caller must NOT read this as "no work"


def test_ready_issues_health_wrong_typed_body_is_refused(ghenv):
    (ghenv / "issue_list.json").write_text('{"not": "a list"}')
    rd = gh.ready_issues_health()
    assert rd.ok is False and rd.value == []


def test_closed_issue_nums_health_answered(ghenv):
    rd = gh.closed_issue_nums_health()
    assert rd.ok is True and rd.value == {41, 52}


def test_closed_issue_nums_health_answered_empty_is_ok(ghenv):
    (ghenv / "issue_list_closed.json").write_text("[]")
    rd = gh.closed_issue_nums_health()
    assert rd.ok is True and rd.value == set()   # a genuine empty closed list: authoritative


def test_closed_issue_nums_health_refused_is_not_answered_empty(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    rd = gh.closed_issue_nums_health()
    assert rd.ok is False and rd.value == set()


# --------------------------- PR lookups: refused != answered-empty (issue #61) ----------
# The build-gate half of the #21 contract, extended per the 2026-07-08 park-notify storm: during
# hourly GraphQL dead zones the PR-for-branch lookup collapsed "GitHub refused" into "no PR
# exists", so finished builds were parked as PR-less. pr_for_branch now returns PrRead(pr, ok):
# ok is True ONLY on a clean answer ({} pr = GitHub genuinely answered "no PR on this head").

def test_pr_for_branch_refused_is_not_answered_empty(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    rd = gh.pr_for_branch("sl/i5-x")
    assert rd.ok is False and rd.pr == {}   # refused: caller must NOT read this as "no PR exists"


def test_pr_for_branch_answered_empty_is_ok(ghenv):
    (ghenv / "pr_list.json").write_text("[]")
    rd = gh.pr_for_branch("sl/i5-x")
    assert rd.ok is True and rd.pr == {}    # a clean "nothing there": authoritative, may park


def test_pr_for_branch_timeout_is_refused(ghenv, monkeypatch):
    # a timeout surfaces as _run's conventional rc 124 — the refused path, never answered-empty
    monkeypatch.setattr(gh, "_run", lambda args, timeout=30: (124, ""))
    rd = gh.pr_for_branch("sl/i5-x")
    assert rd.ok is False and rd.pr == {}


def test_pr_for_branch_wrong_typed_body_is_refused(ghenv):
    # A 200 whose body is valid JSON but the WRONG shape is NOT a clean answer — it must fail
    # closed to ok=False, never to an authoritative "no PR". Includes an entry WITHOUT a real
    # positive-int `number` (Codex review C2): a numberless "PR" would read as trustworthy and
    # then park at the gate as "no PR exists" — the fail-OPEN-on-wrong-typed defect class.
    for body in ('"a bare string, wrong type"', '{"not": "a list"}', '["not a dict"]',
                 "this is not json {{{",
                 '[{}]', '[{"number": null}]', '[{"number": "555"}]', '[{"number": false}]',
                 '[{"number": 0}]', '[{"number": -3}]'):
        (ghenv / "pr_list.json").write_text(body)
        rd = gh.pr_for_branch("sl/i5-x")
        assert rd.ok is False and rd.pr == {}, body


def test_branch_checks_normalized(ghenv):
    # with no commit-status fixture, the /status endpoint fails closed to nothing, so the dev
    # view is exactly the check-runs (the pre-#23 shape stays a subset — a missing status
    # endpoint never breaks the dev poll).
    assert gh.branch_checks("main") == [
        {"name": "review/local-gate", "status": "completed", "conclusion": "success"},
        {"name": "quality-gate", "status": "completed", "conclusion": "success"},
    ]


def test_branch_checks_merges_check_runs_and_commit_statuses(ghenv):
    # issue #23: the dev view must carry the SAME check universe the PR rollup carries —
    # check-runs AND commit statuses — so a required check that reports on the dev branch only
    # as a commit status is visible to freeze/unfreeze. A check-runs-only view was blind to it
    # (its dev view read pending forever, so a mainline freeze could never auto-lift).
    (ghenv / "commit_status.json").write_text(json.dumps({
        "state": "success",
        "statuses": [{"context": "ship/status", "state": "success"}],
    }))
    got = gh.branch_checks("main")
    # the check-runs (CheckRun shape) survive unchanged...
    assert {"name": "review/local-gate", "status": "completed", "conclusion": "success"} in got
    assert {"name": "quality-gate", "status": "completed", "conclusion": "success"} in got
    # ...and the commit status rides along in the StatusContext shape gate.required_checks_state
    # already folds ({context, state}), so no downstream special-casing is needed.
    assert {"context": "ship/status", "state": "success"} in got
    assert len(got) == 3


def test_branch_checks_partial_when_status_endpoint_is_wrong_typed(ghenv):
    # fail closed INDEPENDENTLY: a wrong-typed /status body drops only the status contribution;
    # the check-runs still form the dev view (a required status then reads missing -> pending,
    # never a false green).
    (ghenv / "commit_status.json").write_text('"a bare string, wrong type"')
    got = gh.branch_checks("main")
    assert got == [
        {"name": "review/local-gate", "status": "completed", "conclusion": "success"},
        {"name": "quality-gate", "status": "completed", "conclusion": "success"},
    ]


def test_recent_pr_check_entries_flattens_recent_pr_rollups(ghenv):
    # issue #26: the doctor cross-checks required_checks names against what recent PRs actually
    # reported. This returns the raw rollup entries across recent PRs, flattened.
    entries = gh.recent_pr_check_entries()
    names = {e.get("name") or e.get("context") for e in entries}
    assert names == {"review/local-gate", "quality-gate"}


def test_recent_pr_check_entries_fail_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.recent_pr_check_entries() == []       # unreadable PR list -> no evidence, not a crash


# --------------------------- default branch + branch existence (issue #28) ----------
# adopt writes the repo's REAL default branch as dev_branch (a master/develop repo would
# otherwise fail every worktree creation off origin/main), and doctor validates it exists.

def test_default_branch_parses_the_ref(ghenv):
    assert gh.default_branch() == "main"             # repo_view.json: defaultBranchRef.name


def test_default_branch_reads_a_non_main_default(ghenv):
    (ghenv / "repo_view.json").write_text(json.dumps({"defaultBranchRef": {"name": "trunk"}}))
    assert gh.default_branch() == "trunk"


def test_default_branch_fails_closed_to_none(ghenv, monkeypatch):
    # gh unreachable / unauthenticated: adopt must fall back to the template default, never crash.
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.default_branch() is None


def test_default_branch_none_on_wrong_typed_ref(ghenv):
    (ghenv / "repo_view.json").write_text(json.dumps({"defaultBranchRef": None}))
    assert gh.default_branch() is None               # wrong-typed shape fails closed


def test_branch_exists_true_when_present(ghenv):
    assert gh.branch_exists("main") is True          # default: fake reports the branch present


def test_branch_exists_false_when_missing(ghenv, monkeypatch):
    monkeypatch.setenv("GH_MISSING_BRANCHES", "develop")
    assert gh.branch_exists("develop") is False      # 404 -> False (doctor FAILs on this)


def test_branch_exists_argv_encodes_slashed_ref(ghenv):
    gh.branch_exists("release/2.0")
    argv = _calls(ghenv)[-1]
    # the ref is URL-encoded so a slashed branch doesn't split into extra api path segments
    assert any("release%2F2.0" in a for a in argv), argv


def test_compare(ghenv):
    c = gh.compare("main", "sl/i123-x")
    assert c["status"] == "ahead" and c["ahead_by"] == 3
    assert c["files"][0]["filename"] == "src/api/widget.py"


def test_child_issues_precise_filter(ghenv):
    # #41 and #42 declare parent #40; #43 declares #400 — the fuzzy search would catch #43 on the
    # "#40" substring, so gh.child_issues must filter precisely via parse_loop_metadata.
    kids = gh.child_issues(40)
    assert sorted(k["number"] for k in kids) == [41, 42]


# --------------------------- writes: mutation recording ---------------------------

def test_set_labels_records_mutation(ghenv):
    assert gh.set_labels(5, add=["in-progress"], remove=["agent-ready"]) is True
    assert _mutations(ghenv)[-1] == {
        "kind": "set_labels", "num": "5", "add": "in-progress", "remove": "agent-ready"}


def test_set_labels_noop_when_nothing_to_change(ghenv):
    assert gh.set_labels(5) is True
    assert _mutations(ghenv) == []       # no gh call made when there's nothing to add/remove


def test_comment_records(ghenv):
    assert gh.comment(5, "hello") is True
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "comment" and m["num"] == "5" and m["body"] == "hello"


def test_pr_comment_records(ghenv):
    assert gh.pr_comment(555, "cross-linked to #5") is True
    assert _mutations(ghenv)[-1]["kind"] == "pr_comment"


def test_merge_pr_records_method(ghenv):
    ok, reason = gh.merge_pr(555, "squash")
    assert ok is True and reason == ""          # a clean merge carries no refusal reason
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "merge_pr" and m["method"] == "squash" and m["num"] == "555"


def test_merge_pr_refusal_returns_false_and_a_bounded_stderr_reason(ghenv, monkeypatch):
    # Issue #27: a refused merge (branch protection, or a token without merge rights) must surface
    # WHY, not just fail silently. gh's stderr is the honest reason; merge_pr returns it, bounded.
    monkeypatch.setenv("GH_FAIL", "1")
    ok, reason = gh.merge_pr(555, "squash")
    assert ok is False
    assert isinstance(reason, str) and "forced failure" in reason   # fake-gh's stderr surfaced
    assert 0 < len(reason) <= gh.MERGE_REFUSAL_REASON_CHARS


def test_merge_refusal_reason_is_collapsed_to_one_line_and_bounded():
    # The tail helper is pure: a pathological multi-line/huge stderr collapses to a single bounded
    # line (a memo/notify/comment can't be blown up by a chatty gh), and empty/None -> "".
    big = "failed to merge:\nProtected branch update failed\n" + "x" * 5000
    r = gh._merge_refusal_reason(big)
    assert "\n" not in r and len(r) <= gh.MERGE_REFUSAL_REASON_CHARS
    assert "Protected branch update failed" in gh._merge_refusal_reason(
        "failed to merge: Protected branch update failed")
    assert gh._merge_refusal_reason("") == "" and gh._merge_refusal_reason(None) == ""


def test_create_issue_returns_number_and_records(ghenv, monkeypatch):
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "777")
    num = gh.create_issue("Restore green", "scoped to green", labels=["type:diagnose-and-fix"])
    assert num == 777
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "create_issue" and m["title"] == "Restore green"
    assert m["labels"] == "type:diagnose-and-fix"


# --------------------------- fail-closed: timeout + nonzero rc + bad json + no binary ---------

def test_nonzero_rc_fails_closed_everywhere(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    # reads -> empty-but-typed
    assert gh.ready_issues() == []
    assert gh.issue(1) == {}
    assert gh.pr_for_branch("x").ok is False and gh.pr_for_branch("x").pr == {}
    assert gh.issue_comments(1).ok is False and gh.issue_comments(1).comments == []
    assert gh.pr_comments(1).ok is False and gh.pr_comments(1).comments == []
    assert gh.branch_checks("m") == []
    assert gh.compare("a", "b") == {}
    assert gh.child_issues(1) == []
    # writes -> False/None (act as if it didn't happen)
    assert gh.set_labels(1, add=["x"]) is False
    assert gh.comment(1, "y") is False
    assert gh.pr_comment(1, "y") is False
    assert gh.merge_pr(1, "squash")[0] is False    # (ok, reason): ok fails closed, reason carries why
    assert gh.create_issue("t", "b") is None


def test_timeout_fails_closed(ghenv, monkeypatch):
    # fake-gh sleeps 2s; a 0.4s hard timeout must return a nonzero rc + empty stdout.
    monkeypatch.setenv("GH_SLEEP", "2")
    rc, out = gh._run(["issue", "list"], timeout=0.4)
    assert rc != 0 and out == ""


def test_bad_json_fails_closed(ghenv):
    (ghenv / "issue_list.json").write_text("this is not json {{{")
    assert gh.ready_issues() == []


def test_missing_binary_fails_closed(ghenv, monkeypatch):
    monkeypatch.setenv("SL_GH", "/nonexistent/definitely-not-gh")
    rc, out = gh._run(["issue", "list"], timeout=5)
    assert rc == 127 and out == ""
    assert gh.ready_issues() == []


@pytest.mark.parametrize("fixture,call,empty", [
    ("issue_list.json", lambda: gh.ready_issues(), []),
    ("issue_view.json", lambda: gh.issue(1), {}),
    # issue_comments / pr_comments / pr_for_branch have their OWN wrong-typed tests above (they
    # return a CommentRead / PrRead, not a bare list — refused != answered-empty, issues #21/#61).
    ("check_runs.json", lambda: gh.branch_checks("m"), []),
    ("compare.json", lambda: gh.compare("a", "b"), {}),
    ("issue_search.json", lambda: gh.child_issues(1), []),
])
def test_wrong_typed_json_fails_closed_everywhere(ghenv, fixture, call, empty):
    # a 200 whose body is valid JSON but the WRONG type (a bare string where a list/dict is
    # expected) must fail closed for EVERY parser — never hand the wrong shape downstream.
    (ghenv / fixture).write_text('"a bare string, wrong type"')
    assert call() == empty


# --------------------------- argv contract (pin the exact gh invocation) ---------------------------

def test_ready_issues_argv_pins_agent_ready_and_open(ghenv):
    gh.ready_issues()
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["issue", "list"]
    assert _after(argv, "--label") == "agent-ready"
    assert _after(argv, "--state") == "open"


def test_pr_for_branch_argv_pins_head_and_state_all(ghenv):
    gh.pr_for_branch("sl/i123-x")
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["pr", "list"]
    assert _after(argv, "--head") == "sl/i123-x"
    assert _after(argv, "--state") == "all"


def test_branch_checks_argv_encodes_slashed_ref(ghenv):
    # the real-gh bug the fake would mask: a slashed branch must be URL-encoded in BOTH the
    # check-runs AND the commit-status api paths (issue #23 widened the dev view to read both).
    gh.branch_checks("sl/i1-x")
    api_paths = [c[1] for c in _calls(ghenv) if c and c[0] == "api" and len(c) > 1]
    assert any("commits/sl%2Fi1-x/check-runs" in p for p in api_paths)
    assert any("commits/sl%2Fi1-x/status" in p for p in api_paths)
    assert not any("sl/i1-x/" in p for p in api_paths)   # the raw (broken) form must NOT appear


def test_compare_argv_encodes_refs(ghenv):
    gh.compare("main", "sl/i1-x")
    argv = _calls(ghenv)[-1]
    assert "compare/main...sl%2Fi1-x" in argv[1]


def test_child_issues_argv_searches_all_state(ghenv):
    gh.child_issues(40)
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["issue", "list"]
    assert _after(argv, "--state") == "all"
    assert "--search" in argv and "parent: #40" in _after(argv, "--search")


def test_create_issue_argv_pins_title_body_labels(ghenv):
    gh.create_issue("Restore green", "scoped body", labels=["type:diagnose-and-fix", "expedite"])
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["issue", "create"]
    assert _after(argv, "--title") == "Restore green"
    assert _after(argv, "--body") == "scoped body"
    assert _after(argv, "--label") == "type:diagnose-and-fix,expedite"


def test_merge_pr_argv_pins_squash_flag(ghenv):
    gh.merge_pr(9, "squash")
    assert "--squash" in _calls(ghenv)[-1]


# --------------------------- Task-10 additions (the runner's poll surface) ---------------------------
# open_issues (in-progress orphan sweep), closed_issue_nums (blocked-by eligibility), probe (gh
# health for the persistent-failure ALERT), pr_add_labels (supersede a PR), close_issue
# (investigation close), and headRefOid in the PR view (the runner clears update_result when the
# PR head changes — without the oid that contract is unimplementable).

def test_open_issues_parses_and_pins_label(ghenv):
    lst = gh.open_issues("in-progress")
    assert [i["number"] for i in lst] == [101, 102, 103]
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["issue", "list"]
    assert _after(argv, "--state") == "open"
    assert _after(argv, "--label") == "in-progress"


def test_closed_issue_nums(ghenv):
    assert gh.closed_issue_nums() == {41, 52}
    argv = _calls(ghenv)[-1]
    assert argv[:2] == ["issue", "list"]
    assert _after(argv, "--state") == "closed"


def test_closed_issue_nums_skips_wrong_typed_entries(ghenv):
    (ghenv / "issue_list_closed.json").write_text('[{"number": 7}, {"number": "8"}, "x", {}]')
    assert gh.closed_issue_nums() == {7}


def test_probe_ok_and_failing(ghenv, monkeypatch):
    assert gh.probe() is True
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.probe() is False


def test_pr_add_labels_records(ghenv):
    assert gh.pr_add_labels(555, ["superseded"]) is True
    m = _mutations(ghenv)[-1]
    assert m == {"kind": "pr_add_labels", "num": "555", "add": "superseded"}


def test_close_issue_records(ghenv):
    assert gh.close_issue(123, comment="done — investigation report posted") is True
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "close_issue" and m["num"] == "123"
    assert m["comment"] == "done — investigation report posted"


def test_close_issue_without_comment(ghenv):
    assert gh.close_issue(123) is True
    assert _mutations(ghenv)[-1]["comment"] is None


def test_pr_view_carries_head_oid(ghenv):
    pr = gh.pr_for_branch("sl/i123-render-the-widget").pr
    assert pr["headRefOid"] == "abc123def456"
    argv = _calls(ghenv)[-1]
    assert "headRefOid" in _after(argv, "--json")


def test_new_helpers_fail_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.open_issues("in-progress") == []
    assert gh.closed_issue_nums() == set()
    assert gh.pr_add_labels(1, ["superseded"]) is False
    assert gh.close_issue(1) is False


def test_labels_and_create_label(ghenv):
    assert "preserve" in gh.labels() and "agent-ready" in gh.labels()
    assert gh.create_label("parked", "c2e0c6", "handed back with a memo") is True
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "create_label" and m["name"] == "parked" and m["force"] is True


def test_labels_fail_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.labels() == set()
    assert gh.create_label("x", "ffffff", "d") is False


# --------------------------- D1: explicit repo targeting (GH_REPO) ---------------------------
# gh resolves its target repo from the process CWD's git remotes, so a runner started outside
# the adopted repo silently talked to the wrong repo — or none (live dry-run 2026-07-03, D1).
# set_repo(config.repo) pins every gh subprocess via GH_REPO — gh's own override, honored by
# the issue/pr/label commands and `gh api` {owner}/{repo} placeholders.

@pytest.fixture
def envprobe(tmp_path, monkeypatch):
    """SL_GH -> a stub printing $GH_REPO: the assertion surface is the EXACT env the gh
    subprocess receives, not gh-module internals."""
    probe = tmp_path / "gh-envprobe"
    probe.write_text('#!/bin/sh\nprintf \'%s\' "${GH_REPO:-}"\n')
    probe.chmod(0o755)
    monkeypatch.setenv("SL_GH", str(probe))
    monkeypatch.setattr(gh, "_repo", None, raising=False)   # isolate + auto-restore the pin
    return probe


def test_set_repo_pins_every_gh_subprocess(envprobe, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    gh.set_repo("owner/name")
    assert gh._run(["api", "rate_limit"]) == (0, "owner/name")


def test_set_repo_beats_an_ambient_GH_REPO(envprobe, monkeypatch):
    # the live-run workaround was `export GH_REPO=...` — config.repo must win over that too,
    # or a stale export from operating repo A would silently redirect repo B's runner
    monkeypatch.setenv("GH_REPO", "somewhere/else")
    gh.set_repo("owner/name")
    assert gh._run(["api", "rate_limit"])[1] == "owner/name"


def test_unpinned_run_leaves_the_ambient_env_alone(envprobe, monkeypatch):
    gh.set_repo(None)
    monkeypatch.setenv("GH_REPO", "ambient/repo")
    assert gh._run(["api", "rate_limit"])[1] == "ambient/repo"
    monkeypatch.delenv("GH_REPO", raising=False)
    assert gh._run(["api", "rate_limit"])[1] == ""


def test_set_repo_blank_clears_to_unpinned(envprobe, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    gh.set_repo("owner/name")
    gh.set_repo("   ")
    assert gh._run(["api", "rate_limit"])[1] == ""


# --------------------------- janitor reads/writes (issue #62) ---------------------------

def test_remote_branches_parses_names_and_tips_and_pins_the_api_path(ghenv):
    branches = gh.remote_branches()
    assert branches == {"main": "aaa111", "sl/i5-fix-thing": "bbb222",
                        "sl/i7-old-thing": "ccc333"}
    argv = _calls(ghenv)[-1]
    assert argv[0] == "api" and "branches?per_page=100" in argv[1]


def test_remote_branches_fails_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.remote_branches() == {}


def test_remote_branches_wrong_typed_entries_skip_or_carry_no_tip(ghenv):
    # a nameless/garbage entry is skipped; a named entry with an unreadable sha is kept with
    # tip None — the janitor then never proposes it (tip unprovable, fail closed).
    (ghenv / "branches.json").write_text(json.dumps(
        [{"name": "main"}, {"name": "x", "commit": {"sha": 42}}, {"name": 42},
         "garbage", {"nom": "x"}]))
    assert gh.remote_branches() == {"main": None, "x": None}


def test_open_prs_labeled_parses_and_pins_label(ghenv):
    lst = gh.open_prs_labeled("superseded")
    assert [p["number"] for p in lst] == [14]
    assert lst[0]["headRefName"] == "sl/i7-old-thing"
    argv = _calls(ghenv)[-1]
    assert _after(argv, "--label") == "superseded" and _after(argv, "--state") == "open"
    assert "labels" in _after(argv, "--json") and "headRefName" in _after(argv, "--json")


def test_open_prs_labeled_fails_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.open_prs_labeled("superseded") == []


def test_open_issues_activity_parses_and_asks_for_updated_at(ghenv):
    lst = gh.open_issues_activity("parked")
    assert [i["number"] for i in lst] == [9]
    assert lst[0]["updatedAt"] == "2026-06-01T12:00:00Z"
    argv = _calls(ghenv)[-1]
    assert _after(argv, "--label") == "parked" and "updatedAt" in _after(argv, "--json")


def test_open_issues_activity_fails_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.open_issues_activity("parked") == []


def test_delete_branch_records_and_pins_the_ref_path(ghenv):
    assert gh.delete_branch("sl/i5-fix-thing") is True
    m = _mutations(ghenv)[-1]
    assert m == {"kind": "delete_ref", "ref": "heads/sl/i5-fix-thing"}
    argv = _calls(ghenv)[-1]
    # slashes in the branch stay raw path segments; the method is an explicit DELETE
    assert argv[1].endswith("/git/refs/heads/sl/i5-fix-thing")
    assert _after(argv, "-X") == "DELETE"


def test_delete_branch_fails_closed(ghenv, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.delete_branch("sl/i5-fix-thing") is False


def test_close_pr_records_with_comment(ghenv):
    assert gh.close_pr(14, comment="superlooper janitor: superseded") is True
    m = _mutations(ghenv)[-1]
    assert m["kind"] == "close_pr" and m["num"] == "14"
    assert m["comment"] == "superlooper janitor: superseded"


def test_close_pr_without_comment_and_fail_closed(ghenv, monkeypatch):
    assert gh.close_pr(14) is True
    assert _mutations(ghenv)[-1]["comment"] is None
    monkeypatch.setenv("GH_FAIL", "1")
    assert gh.close_pr(14) is False
