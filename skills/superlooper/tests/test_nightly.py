"""nightly.py — the pure decision core of the 02:00 QA loop (plan Task 12).

The orchestration (fresh worktree, run qa.nightly_cmd, freeze, file issues, journal, notify) is
thin glue in `skill/bin/superlooper`; everything that DECIDES lives here so the whole flake-vs-
persistent-vs-accepted-vs-quarantined table is a unit test, no cmux/git/GitHub required.

Two invariants under test that the audit bought: a nightly that cannot parse its results is NOT
ok (so the caller reports "could not parse" + ALERT, never a silent green), and the auto-filed
fix issue carries EXACTLY the standing-rule label set (§4.4 audit trail) with the runner's
fingerprint dedup marker so a nightly-filed and a runner-filed issue for the same breakage are
one issue.
"""
import actions
import gate
import nightly

PASS_XML = """<?xml version="1.0"?>
<testsuites><testsuite name="s" tests="2" failures="0" errors="0">
  <testcase classname="pkg.mod" name="test_ok" time="0.1"/>
  <testcase classname="pkg.mod" name="test_ok2" time="0.1"/>
</testsuite></testsuites>"""

FAIL_XML = """<?xml version="1.0"?>
<testsuites><testsuite name="s" tests="3" failures="1" errors="1">
  <testcase classname="pkg.mod" name="test_ok" time="0.1"/>
  <testcase classname="pkg.mod" name="test_bad" time="0.2">
    <failure message="assert 1 == 2">Traceback ... at line 42</failure>
  </testcase>
  <testcase classname="pkg.mod" name="test_boom" time="0.0">
    <error message="ConnectionError">boom at /Users/ci/x/net.py:88</error>
  </testcase>
</testsuite></testsuites>"""


def _f(tid, text):
    return {"test_id": tid, "text": text}


# --------------------------- parse_junit ---------------------------

def test_parse_junit_extracts_failures_and_errors_not_passes():
    r = nightly.parse_junit([FAIL_XML])
    assert r["ok"] is True and r["tests"] == 3
    ids = {f["test_id"] for f in r["failures"]}
    assert ids == {"pkg.mod::test_bad", "pkg.mod::test_boom"}     # failure AND error count
    assert "pkg.mod::test_ok" not in ids


def test_parse_junit_all_green():
    r = nightly.parse_junit([PASS_XML])
    assert r["ok"] is True and r["failures"] == []


def test_parse_junit_could_not_parse_is_not_ok_never_a_silent_green():
    assert nightly.parse_junit([])["ok"] is False                # no results at all
    assert nightly.parse_junit(["<not valid xml"])["ok"] is False   # malformed
    assert nightly.parse_junit(["<testsuite tests='5'></testsuite>"])["ok"] is False  # 0 testcases
    # a mix: one malformed + one real -> ok True, and the real failures surface
    r = nightly.parse_junit(["<garbage", FAIL_XML])
    assert r["ok"] is True and len(r["failures"]) == 2


# --------------------------- classify: flake vs persistent ---------------------------

def test_persistent_failure_reproduces_on_retry():
    run1 = [_f("t::a", "boom"), _f("t::b", "once only")]
    run2 = [_f("t::a", "boom")]                                  # b passed on retry -> flake
    r = nightly.classify(run1, run2, quarantine=[], accepted_fps=set())
    assert {f["test_id"] for f in r["to_file"]} == {"t::a"}
    assert {f["test_id"] for f in r["flakes"]} == {"t::b"}


def test_no_retry_means_every_failure_is_persistent():
    r = nightly.classify([_f("t::a", "boom")], None, [], set())
    assert {f["test_id"] for f in r["to_file"]} == {"t::a"}
    assert r["flakes"] == []


def test_accepted_ledger_failures_fold_away_never_filed():
    run1 = [_f("t::a", "boom")]
    fp = nightly.fingerprint(_f("t::a", "boom"))
    r = nightly.classify(run1, run1, quarantine=[], accepted_fps={fp})
    assert r["to_file"] == []                                    # accepted -> never freezes/files
    assert {f["test_id"] for f in r["accepted"]} == {"t::a"}


def test_quarantined_failures_never_freeze_or_file_or_flake():
    run1 = [_f("tests/flaky.py::test_x", "boom"), _f("t::real", "boom")]
    r = nightly.classify(run1, run1, quarantine=["tests/flaky.py::test_x"], accepted_fps=set())
    assert {f["test_id"] for f in r["to_file"]} == {"t::real"}   # only the non-quarantined one
    assert {f["test_id"] for f in r["quarantined"]} == {"tests/flaky.py::test_x"}


def test_quarantine_supports_glob_patterns():
    run1 = [_f("tests/flaky/test_a.py::test_x", "boom")]
    r = nightly.classify(run1, run1, quarantine=["tests/flaky/*"], accepted_fps=set())
    assert r["to_file"] == []
    assert {f["test_id"] for f in r["quarantined"]} == {"tests/flaky/test_a.py::test_x"}


def test_classify_wrong_typed_inputs_never_raise():
    r = nightly.classify(None, None, None, None)
    assert r["to_file"] == [] and r["flakes"] == [] and r["quarantined"] == []


def test_quarantine_matches_a_parametrized_id_verbatim_never_crashes():
    # a real pytest id has brackets: `[gpu-0]` is an INVALID fnmatch range (u..0 descends) and
    # would raise re.error. Quarantining exactly that id must still work (exact-match fallback),
    # never crash the whole nightly (the never-raise contract).
    tid = "tests/test_gpu.py::test_render[gpu-0]"
    run1 = [_f(tid, "boom"), _f("t::real", "boom")]
    r = nightly.classify(run1, run1, quarantine=[tid], accepted_fps=set())
    assert {f["test_id"] for f in r["quarantined"]} == {tid}     # quarantined by exact match
    assert {f["test_id"] for f in r["to_file"]} == {"t::real"}   # the real failure still files


# --------------------------- fingerprint + fix issue ---------------------------

def test_fingerprint_is_the_gate_scheme():
    f = _f("t::x", "boom 42")
    assert nightly.fingerprint(f) == gate.fix_issue_fingerprint("t::x", "boom 42")


def test_fix_issue_carries_standing_rule_labels_and_the_dedup_marker():
    f = _f("tests/test_login.py::test_redirect", "AssertionError: redirect loop at line 88")
    issue = nightly.fix_issue(f, dev_branch="main")
    assert issue["labels"] == ["type:diagnose-and-fix", "agent-ready",
                               "auto-approved:nightly-red", "expedite"]
    fp = nightly.fingerprint(f)
    assert issue["fingerprint"] == fp
    assert f"Failure fingerprint: `{fp}`" in issue["body"]       # runner's _exec_file_fix_issue marker
    assert "test_redirect" in issue["title"] or "test_redirect" in issue["body"]
    assert "restoring green" in issue["body"].lower() or "restore green" in issue["title"].lower()


def test_fix_issue_fences_backticks_and_normalizes_id_newlines():
    # Codex R2 M2: worker-controlled failure text with a ``` run must NOT escape the code fence
    # (which would let it inject issue-body instructions), and a newline in the test id must not
    # break the title / DoD line.
    f = _f("tests/x.py::test_a\nmalicious injected line", "trace ``` closes early ``` end")
    issue = nightly.fix_issue(f, dev_branch="main")
    assert "\n" not in issue["title"]                        # id newline normalized in the title
    assert "````" in issue["body"]                           # fence longer than the 3-backtick run
    assert "trace ``` closes early" in issue["body"]         # excerpt preserved inside the longer fence


def test_nightly_fix_labels_match_the_runner_standing_rule():
    # §4.4 audit-trail regression: nightly-filed and runner-filed fix issues carry identical labels
    assert nightly.NIGHTLY_FIX_LABELS == actions.FIX_ISSUE_LABELS
    assert "auto-approved:nightly-red" in nightly.NIGHTLY_FIX_LABELS
