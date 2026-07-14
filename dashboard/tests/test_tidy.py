"""Issue #41 — the Tidy verb: a LOCAL COMMAND execution, the dashboard's second button class.

Every other verb writes a GitHub label/comment/issue (``lib/actions.py``). Tidy is different: it
runs the local ``superlooper tidy`` CLI to close finished cmux session windows. So its adapter
(``lib/tidy.py``) mirrors ``lib/gh.py``'s discipline — a subprocess wrapper that NEVER raises into
the caller, a hard timeout, and fail-closed on any nonzero exit — but keeps the SEMANTICS (turning
the CLI's human list into structured window rows) as pure, unit-tested functions (design B.1).

Two properties are load-bearing bright lines:

* **No real binary in tests.** The CLI CLOSES windows; a stray real call would touch William's live
  cmux. The conftest points ``SL_SUPERLOOPER`` at an absent path by default; these tests override it
  in-body to ``tests/fakes/fake-superlooper``, which records every invocation.
* **Merged-only scope.** ``--all`` is NEVER passed (issue #41 — the dashboard does not expose the
  wider scope). The fake logs argv, so a test proves ``--all`` never appears.
"""
import json
from pathlib import Path

import pytest

import tidy as tidy_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


# =============================== the pure parser (against the REAL CLI format) ===============================
# These strings are the EXACT shape `superlooper tidy` prints (bin/superlooper cmd_tidy):
#   header:  "tidy will close N finished (merged) session window(s):"
#   row:     f"  {id:5} {status:13} {surface or '(no surface)'}"
#   footer:  "dry-run: nothing closed."  /  "closed N window(s)."
#   empty:   "tidy: no finished (merged) session windows to close."

_REAL_DRY_RUN = (
    "tidy will close 2 finished (merged) session window(s):\n"
    "  i23   merged        cmux:surface-23\n"
    "  i16   merged        (no surface)\n"
    "dry-run: nothing closed.\n"
)
_REAL_EMPTY = "tidy: no finished (merged) session windows to close.\n"


def test_parse_windows_reads_the_real_cli_row_format():
    windows = tidy_mod.parse_windows(_REAL_DRY_RUN)
    assert windows == [
        {"id": "i23", "status": "merged", "surface": "cmux:surface-23"},
        {"id": "i16", "status": "merged", "surface": ""},   # "(no surface)" → empty string
    ]


def test_parse_windows_empty_when_nothing_finished():
    assert tidy_mod.parse_windows(_REAL_EMPTY) == []
    assert tidy_mod.parse_windows("") == []


def test_parse_windows_ignores_header_and_footer_lines():
    # Only the indented ``  iN  status  surface`` rows are windows — never the header/footer prose.
    windows = tidy_mod.parse_windows(_REAL_DRY_RUN)
    assert [w["id"] for w in windows] == ["i23", "i16"]


def test_parse_windows_is_status_agnostic():
    # Defensive: even though the dashboard never requests --all, the parser must not choke on the
    # wider-scope statuses (parked/needs_william/bounced) if the CLI ever emits them.
    out = ("tidy will close 1 finished (terminal) session window(s):\n"
           "  i9    needs_william cmux:s-9\n")
    assert tidy_mod.parse_windows(out) == [
        {"id": "i9", "status": "needs_william", "surface": "cmux:s-9"}]


def test_parse_windows_reads_answerer_rows():
    # Issue #132: `superlooper tidy` now ALSO lists finished answerer session windows (a<N>) with a
    # synthetic "finished" status. The dashboard must bind those rows too, or the Tidy button would
    # silently under-count and never offer to close them — the exact linger the CLI now fixes.
    out = ("tidy will close 2 finished (merged) session window(s):\n"
           "  i23   merged        cmux:surface-23\n"
           "  a1    finished      cmux:answerer-1\n")
    assert tidy_mod.parse_windows(out) == [
        {"id": "i23", "status": "merged", "surface": "cmux:surface-23"},
        {"id": "a1", "status": "finished", "surface": "cmux:answerer-1"}]


def test_parse_windows_reads_an_answerer_with_no_surface():
    out = ("tidy will close 1 finished (merged) session window(s):\n"
           "  a7    finished      (no surface)\n")
    assert tidy_mod.parse_windows(out) == [
        {"id": "a7", "status": "finished", "surface": ""}]


def test_parse_closed_reads_the_count():
    assert tidy_mod.parse_closed("closed 3 window(s).\n") == 3
    assert tidy_mod.parse_closed("closed 1 window(s). 1 re-approvable ...") == 1
    assert tidy_mod.parse_closed(_REAL_EMPTY) == 0     # nothing closed → 0
    assert tidy_mod.parse_closed("") == 0


# =============================== the Tidy verb (against fake-superlooper) ===============================

@pytest.fixture
def tidy_fix(tmp_path, monkeypatch):
    """A Tidy bound to the fake CLI, with a fixtures dir the fake logs its calls/mutations into."""
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    # The configured binary is deliberately bogus so a passing test PROVES the SL_SUPERLOOPER env
    # override (the fail-closed fixture's only lever) actually wins over the configured path.
    verb = tidy_mod.Tidy("/nonexistent/configured-superlooper", {SLUG: PATH})
    return verb, tmp_path


def _calls(fixtures):
    p = fixtures / "calls.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _mutations(fixtures):
    p = fixtures / "mutations.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def test_dry_run_lists_the_windows_the_cli_names(tidy_fix):
    verb, _ = tidy_fix
    res = verb.dry_run(SLUG)
    assert res["ok"] is True
    assert res["count"] == 2
    assert [w["id"] for w in res["windows"]] == ["i23", "i16"]
    assert res["verb"] == "tidy-dry-run"


def test_dry_run_invokes_dry_run_against_the_right_repo(tidy_fix):
    verb, fixtures = tidy_fix
    verb.dry_run(SLUG)
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["tidy", "--repo", PATH]
    assert "--dry-run" in argv
    assert "--yes" not in argv          # dry-run closes nothing
    assert "--all" not in argv          # merged-only scope (issue #41)


def test_execute_closes_and_reports_the_count(tidy_fix):
    verb, fixtures = tidy_fix
    res = verb.execute(SLUG)
    assert res["ok"] is True
    assert res["closed"] == 2
    assert res["verb"] == "tidy"
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["tidy", "--repo", PATH]
    assert "--yes" in argv              # skips the CLI's y/N — the in-UI confirm already happened
    assert "--all" not in argv          # still merged-only


def test_execute_actually_runs_the_close_path(tidy_fix):
    verb, fixtures = tidy_fix
    verb.execute(SLUG)
    muts = _mutations(fixtures)
    assert any(m["kind"] == "tidy_execute" and m["yes"] and not m["all"] for m in muts)


def test_unknown_repo_is_refused_before_any_subprocess(tidy_fix):
    verb, fixtures = tidy_fix
    res = verb.dry_run("someone/else")
    assert res["ok"] is False
    assert res["error"] == "unknown repo"
    assert _calls(fixtures) == []        # refused BEFORE the CLI ever ran


def test_execute_unknown_repo_is_refused(tidy_fix):
    verb, fixtures = tidy_fix
    res = verb.execute("someone/else")
    assert res["ok"] is False and res["error"] == "unknown repo"
    assert _calls(fixtures) == []


def test_command_failure_surfaces_plainly_never_a_silent_success(tidy_fix, monkeypatch):
    verb, _ = tidy_fix
    monkeypatch.setenv("SL_TIDY_FAIL", "1")            # the CLI exits nonzero
    dry = verb.dry_run(SLUG)
    assert dry["ok"] is False and dry["windows"] == [] and dry["count"] == 0
    assert dry["error"]                                # a plain, non-empty message
    ex = verb.execute(SLUG)
    assert ex["ok"] is False and ex["closed"] == 0 and ex["error"]


def test_missing_binary_fails_closed(monkeypatch, tmp_path):
    # The conftest default already points SL_SUPERLOOPER at an absent path — assert that path is a
    # clean, honest failure (rc 127), not a crash, and names the CLI so the UI can say what's wrong.
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    verb = tidy_mod.Tidy("/nonexistent/configured", {SLUG: PATH})
    res = verb.dry_run(SLUG)
    assert res["ok"] is False
    assert "superlooper" in res["error"].lower() or "CLI" in res["error"]


def test_timeout_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    monkeypatch.setenv("SL_TIDY_SLEEP", "2")
    verb = tidy_mod.Tidy("/nonexistent", {SLUG: PATH}, timeout=0.2)
    res = verb.dry_run(SLUG)
    assert res["ok"] is False and res["windows"] == []


def test_env_override_wins_over_configured_binary(tidy_fix):
    # The configured binary in tidy_fix is bogus; the run still works — proving SL_SUPERLOOPER wins,
    # which is exactly what lets the conftest neutralize this globally by default.
    verb, _ = tidy_fix
    assert verb.dry_run(SLUG)["ok"] is True


def test_all_scope_is_never_passed_on_either_verb(tidy_fix):
    # A focused bright-line guard (issue #41): --all must never appear on ANY tidy invocation.
    verb, fixtures = tidy_fix
    verb.dry_run(SLUG)
    verb.execute(SLUG)
    for call in _calls(fixtures):
        assert "--all" not in call["argv"], "the dashboard must never widen tidy's scope to --all"
