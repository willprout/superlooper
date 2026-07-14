"""Issue #116 — the Restart verb: a LOCAL COMMAND execution, like Tidy (the dashboard's second
button class). Every GitHub-write verb lives in ``lib/actions.py``; Restart, like Tidy, is
different — it shells the local ``superlooper`` CLI, here ``superlooper request-restart``, to ask
the LIVE runner to restart ITSELF in its own cmux tab. So its adapter (``lib/restart.py``) mirrors
``lib/tidy.py``'s discipline: a subprocess wrapper that NEVER raises into the caller, a hard
timeout, and fail-closed on any nonzero exit — but keeps the outcome HONEST.

Two properties are load-bearing bright lines:

* **No real binary in tests.** The CLI writes into a live loop's state home; a stray real call
  would poke William's running runner. The conftest points ``SL_SUPERLOOPER`` at an absent path by
  default; these tests override it in-body to ``tests/fakes/fake-superlooper``.
* **The dead-runner case is an honest outcome, not a crash.** No live runner ⇒ the verb reports
  ``running: false`` with the one-line manual start — it never launches or places anything. The
  adapter parses the CLI's JSON body EVEN on a nonzero exit (a refusal is rc 1 with a real body), so
  the button shows the honest "no loop running", never a generic error.
"""
import json
from pathlib import Path

import pytest

import restart as restart_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


@pytest.fixture
def restart_fix(tmp_path, monkeypatch):
    """A Restart bound to the fake CLI, with a fixtures dir the fake logs its calls/mutations into."""
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))     # the fake logs calls/mutations here
    # The configured binary is deliberately bogus so a passing test PROVES the SL_SUPERLOOPER env
    # override (the fail-closed fixture's only lever) actually wins over the configured path.
    verb = restart_mod.Restart("/nonexistent/configured-superlooper", {SLUG: PATH},
                               operator="William")
    return verb, tmp_path


def _calls(fixtures):
    p = fixtures / "calls.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _mutations(fixtures):
    p = fixtures / "mutations.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


# --------------------------- preflight (the button's step 1) ---------------------------

def test_preflight_reports_a_live_runner_and_writes_nothing(restart_fix):
    verb, fixtures = restart_fix
    res = verb.preflight(SLUG)
    assert res["ok"] is True and res["running"] is True
    assert res["verb"] == "restart-check"
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["request-restart", "--repo", PATH]
    assert "--check" in argv                              # preflight only — writes nothing
    assert _mutations(fixtures) == []                    # --check records no marker write


def test_preflight_reports_no_live_runner(restart_fix, monkeypatch):
    verb, _ = restart_fix
    monkeypatch.setenv("SL_RESTART_RUNNING", "0")
    res = verb.preflight(SLUG)
    assert res["running"] is False
    assert "superlooper run" in res["manual"]            # the one-line manual start


# --------------------------- execute (the confirmed request) ---------------------------

def test_execute_requests_the_restart_of_a_live_runner(restart_fix):
    verb, fixtures = restart_fix
    res = verb.execute(SLUG)
    assert res["ok"] is True and res["running"] is True and res["requested"] is True
    assert res["verb"] == "restart"
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["request-restart", "--repo", PATH]
    assert "--check" not in argv                          # the execute path writes the marker
    assert "--source" in argv and "command-center" in argv


def test_execute_signs_the_request_with_the_operator(restart_fix):
    verb, fixtures = restart_fix
    verb.execute(SLUG)
    mut = [m for m in _mutations(fixtures) if m["kind"] == "restart_request"]
    assert mut and mut[-1]["operator"] == "William" and mut[-1]["source"] == "command-center"
    assert mut[-1]["repo"] == PATH


def test_execute_on_a_dead_runner_is_an_honest_refusal_not_an_error(restart_fix, monkeypatch):
    # The dead-runner case (rc 1 from the CLI, but a well-formed JSON body): the adapter surfaces the
    # honest outcome — running:false + the manual start — never a generic command error, and it makes
    # NO attempt to launch or place anything.
    verb, fixtures = restart_fix
    monkeypatch.setenv("SL_RESTART_RUNNING", "0")
    res = verb.execute(SLUG)
    assert res["ok"] is False and res["running"] is False and res["requested"] is False
    assert "superlooper run" in res["manual"]
    assert not any(m["kind"] == "restart_request" for m in _mutations(fixtures))  # nothing written


# --------------------------- the allow-list + fail-closed bright lines ---------------------------

def test_unknown_repo_is_refused_before_any_subprocess(restart_fix):
    verb, fixtures = restart_fix
    assert verb.preflight("someone/else")["error"] == "unknown repo"
    assert verb.execute("someone/else")["error"] == "unknown repo"
    assert _calls(fixtures) == []                         # refused BEFORE the CLI ever ran


def test_missing_binary_fails_closed(monkeypatch, tmp_path):
    # The conftest default points SL_SUPERLOOPER at an absent path — a clean, honest failure (never a
    # crash), naming the CLI so the UI can say what to fix.
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    verb = restart_mod.Restart("/nonexistent/configured", {SLUG: PATH})
    res = verb.execute(SLUG)
    assert res["ok"] is False and res["error"]           # a plain, non-empty message
    assert res["running"] is None                        # liveness genuinely unknown, never a false "up"


def test_garbage_output_is_a_plain_failure_never_a_false_success(restart_fix, monkeypatch):
    verb, _ = restart_fix
    monkeypatch.setenv("SL_RESTART_GARBAGE", "1")         # CLI ran but printed no parseable JSON
    res = verb.execute(SLUG)
    assert res["ok"] is False and res["error"]
