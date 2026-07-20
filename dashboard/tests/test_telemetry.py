"""Bounded, local GitHub API-burn telemetry (issue #15) — the pure module.

Everything here is exercised without touching gh: the classifiers are pure functions, and the
sink writes to a tmp dir. The two non-negotiables this defends:
  1. FAIL-SAFE. A record_* call NEVER raises — a telemetry fault must not break a gh call.
  2. BOUNDED. The file is byte-capped: once it crosses MAX_BYTES the oldest lines are dropped and
     the newest survive, so unbounded runtime data can never accumulate on disk.
"""
import json

import telemetry


# --------------------------- status classification (the four DoD statuses) ---------------------------

def test_classify_status_success_on_rc_zero():
    assert telemetry.classify_status(0, "") == "success"
    assert telemetry.classify_status(0, "some noise on stderr") == "success"


def test_classify_status_rate_limited_on_primary_and_secondary():
    assert telemetry.classify_status(1, "HTTP 403: API rate limit exceeded for user") == "rate_limited"
    assert telemetry.classify_status(1, "You have exceeded a secondary rate limit") == "rate_limited"
    # case-insensitive
    assert telemetry.classify_status(1, "API RATE LIMIT EXCEEDED") == "rate_limited"


def test_classify_status_failure_only_on_missing_binary():
    # rc 127 = gh not found / bad invocation — the call never left the machine, so it burned no
    # quota. Kept distinct from `refused` so burn accounting doesn't count it as an attempt.
    assert telemetry.classify_status(127, "gh not found / bad invocation") == "failure"


def test_classify_status_refused_on_timeout_and_generic_nonzero():
    assert telemetry.classify_status(124, "gh timed out") == "refused"      # timeout
    assert telemetry.classify_status(1, "HTTP 500: server error") == "refused"   # gh reached, refused
    assert telemetry.classify_status(1, "") == "refused"                   # nonzero, no signature


# --------------------------- api-surface classification (the quota bucket) ---------------------------

def test_classify_api_graphql_for_issue_pr_repo_search():
    assert telemetry.classify_api(["issue", "list", "--json", "number"]) == "graphql"
    assert telemetry.classify_api(["pr", "view", "20"]) == "graphql"
    assert telemetry.classify_api(["repo", "view"]) == "graphql"
    assert telemetry.classify_api(["search", "issues"]) == "graphql"


def test_classify_api_rest_for_api_paths_and_labels():
    assert telemetry.classify_api(["api", "rate_limit"]) == "rest"
    assert telemetry.classify_api(["api", "repos/{owner}/{repo}/branches"]) == "rest"
    assert telemetry.classify_api(["label", "list"]) == "rest"


def test_classify_api_graphql_for_api_graphql():
    assert telemetry.classify_api(["api", "graphql", "-f", "query=..."]) == "graphql"


def test_classify_api_unknown_for_empty_or_alien():
    assert telemetry.classify_api([]) == "unknown"
    assert telemetry.classify_api(["auth", "status"]) == "unknown"


# --------------------------- command-family (bounded cardinality) ---------------------------

def test_classify_family_subcommand_pair():
    assert telemetry.classify_family(["issue", "list", "--state", "open"]) == "issue list"
    assert telemetry.classify_family(["pr", "view", "20", "--json", "x"]) == "pr view"
    assert telemetry.classify_family(["label", "create", "flag"]) == "label create"


def test_classify_family_api_keeps_only_first_path_segment():
    # a per-branch path must NOT explode the family cardinality — only the first segment survives.
    assert telemetry.classify_family(["api", "rate_limit"]) == "api rate_limit"
    assert telemetry.classify_family(
        ["api", "repos/{owner}/{repo}/branches/sl-i1-x"]) == "api repos"
    assert telemetry.classify_family(["api", "graphql"]) == "api graphql"


# --------------------------- rate-limit snapshot parsing ---------------------------

def test_parse_rate_limit_extracts_core_and_graphql():
    body = json.dumps({"resources": {
        "core": {"limit": 5000, "used": 120, "remaining": 4880, "reset": 1700000000},
        "graphql": {"limit": 5000, "used": 4000, "remaining": 1000, "reset": 1700000123},
        "search": {"limit": 30, "used": 1, "remaining": 29, "reset": 1700000060},
        "ignored": {"limit": 1},
    }})
    res = telemetry.parse_rate_limit(body)
    assert res["core"] == {"limit": 5000, "used": 120, "remaining": 4880, "reset": 1700000000}
    assert res["graphql"]["used"] == 4000
    assert res["search"]["remaining"] == 29
    assert "ignored" not in res


def test_parse_rate_limit_fails_closed_on_junk():
    assert telemetry.parse_rate_limit("not json {{{") == {}
    assert telemetry.parse_rate_limit(json.dumps({"no": "resources"})) == {}
    assert telemetry.parse_rate_limit(json.dumps({"resources": "wrong type"})) == {}


# --------------------------- recording: the row shapes ---------------------------

def test_record_call_writes_the_dod_row_shape(tmp_path):
    telemetry.record_call(tmp_path, "runner", "will-titan/superlooper", "ready_issues",
                          ["issue", "list", "--label", "agent-ready"], 0, "", now=111.0)
    rows = telemetry.read(tmp_path, "runner")
    assert len(rows) == 1
    r = rows[0]
    assert r == {"ts": 111.0, "kind": "call", "client": "runner",
                 "repo": "will-titan/superlooper", "op": "ready_issues",
                 "family": "issue list", "api": "graphql", "status": "success"}


def test_record_call_marks_rate_limited_status(tmp_path):
    telemetry.record_call(tmp_path, "runner", "o/r", "open_issues",
                          ["issue", "list"], 1, "API rate limit exceeded", now=1.0)
    assert telemetry.read(tmp_path, "runner")[0]["status"] == "rate_limited"


def test_record_rate_limit_writes_snapshot_row(tmp_path):
    telemetry.record_rate_limit(tmp_path, "runner", "o/r",
                                {"graphql": {"used": 4000, "remaining": 1000}}, True, now=222.0)
    r = telemetry.read(tmp_path, "runner")[0]
    assert r["kind"] == "rate_limit" and r["ok"] is True
    assert r["resources"]["graphql"]["used"] == 4000
    assert r["ts"] == 222.0 and r["client"] == "runner"


def test_runner_and_dashboard_write_separate_files(tmp_path):
    # Client-suffixed files: the two daemons never share (never race) a file, and an incident tool
    # globs gh-telemetry-*.jsonl to read both.
    telemetry.record_call(tmp_path, "runner", "o/r", "x", ["issue", "list"], 0, "", now=1.0)
    telemetry.record_call(tmp_path, "dashboard", "o/r", "y", ["pr", "list"], 0, "", now=2.0)
    assert (tmp_path / "gh-telemetry-runner.jsonl").exists()
    assert (tmp_path / "gh-telemetry-dashboard.jsonl").exists()
    assert [r["op"] for r in telemetry.read(tmp_path, "runner")] == ["x"]
    assert [r["op"] for r in telemetry.read(tmp_path, "dashboard")] == ["y"]


# --------------------------- fail-safe: observability must never break a gh call ---------------------------

def test_record_never_raises_on_an_unwritable_home(tmp_path):
    # home is a FILE, so os.makedirs/open under it must fail — and record must swallow it.
    bad = tmp_path / "not-a-dir"
    bad.write_text("x")
    telemetry.record_call(bad, "runner", "o/r", "x", ["issue", "list"], 0, "", now=1.0)     # no raise
    telemetry.record_rate_limit(bad, "runner", "o/r", {}, False, now=1.0)                   # no raise


def test_read_is_tolerant_of_corrupt_lines(tmp_path):
    p = tmp_path / "gh-telemetry-runner.jsonl"
    p.write_text('{"kind":"call","op":"a"}\nnot json {{{\n\n{"kind":"call","op":"b"}\n')
    assert [r["op"] for r in telemetry.read(tmp_path, "runner")] == ["a", "b"]


# --------------------------- bounded: the ring drops oldest, keeps newest ---------------------------

def test_file_is_byte_bounded_and_keeps_the_newest_rows(tmp_path, monkeypatch):
    # Shrink the caps so a handful of rows trips the trim, then assert the file stays under the cap
    # AND the survivors are the most-recent writes (a ring, not a truncate-to-nothing).
    monkeypatch.setattr(telemetry, "MAX_BYTES", 2000)
    monkeypatch.setattr(telemetry, "TRIM_TO_BYTES", 1000)
    for i in range(400):
        telemetry.record_call(tmp_path, "runner", "o/r", "op%d" % i,
                              ["issue", "list"], 0, "", now=float(i))
    p = tmp_path / "gh-telemetry-runner.jsonl"
    assert p.stat().st_size <= telemetry.MAX_BYTES
    rows = telemetry.read(tmp_path, "runner")
    assert rows, "the ring must keep the newest rows, not empty the file"
    # the very last write survived; an early one was dropped
    ops = [r["op"] for r in rows]
    assert "op399" in ops
    assert "op0" not in ops
    # monotonic newest tail: every surviving ts is greater than the dropped ones
    assert rows == sorted(rows, key=lambda r: r["ts"])
