"""Issue #365 — the Stop/Start verb: a LOCAL COMMAND execution over the engine's #239 off switch.

This is the dashboard's fourth button in the local-command class (after Tidy, Restart and the
Janitor), and its adapter (``lib/stopswitch.py``) inherits that class's whole discipline: a
subprocess wrapper that NEVER raises into the caller, a hard timeout, a watched-repo allow-list
checked BEFORE any subprocess runs, and an outcome that stays honest.

What makes THIS one different is the thing it is for. `superlooper stop` is the off switch: it
records the stop as deliberate, holds the supervisor, and takes the process down — and it has
failure modes that look nothing like a crash and must never be shown as a success:

* **A stop that did not take** (launchd still holds the job, or a pid the runner's record does not
  claim) is a well-formed JSON body at a NONZERO exit. Parsing the body first — as ``lib/restart``
  does — is what turns that into an honest "STOP INCOMPLETE" instead of a generic error, and the
  marker-still-recorded consequence has to survive with it.
* **A stop that HALF took** (the job is gone, the runner is finishing its tick) exits ZERO. It is
  the designed clean stop, and calling it "the runner is stopped" would be the false all-clear the
  DoD forbids. So the summary says what is actually true: the stop is recorded and nothing will
  start it again.
* **The pane home cannot be started at all** — automated tab placement is owner-ruled out — so
  ``start`` there reports ``started: false`` with a manual line and DELIBERATELY leaves the recorded
  stop standing. A button that showed that as "started" would strand the owner.

The semantics of all of that are derived server-side in pure Python (design record B.1) as a
``summary`` block, so the dialog binds sentences it never composed and every one of them is pinned
by a test here rather than discovered at 3am.

No real binary: the conftest points ``SL_SUPERLOOPER`` at an absent path by default and these tests
override it in-body to ``tests/fakes/fake-superlooper``. A stray real call would stop William's
live runner — the most consequential stray call this repo can make.
"""
import json
from pathlib import Path

import pytest

import stopswitch as stop_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """A StopSwitch bound to the fake CLI, with a fixtures dir the fake logs its calls into."""
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))     # the fake logs calls/mutations here
    # The configured binary is deliberately bogus so a passing test PROVES the SL_SUPERLOOPER env
    # override (the fail-closed fixture's only lever) actually wins over the configured path.
    verb = stop_mod.StopSwitch("/nonexistent/configured-superlooper", {SLUG: PATH},
                               operator="William")
    return verb, tmp_path


def _calls(fixtures):
    p = fixtures / "calls.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _mutations(fixtures):
    p = fixtures / "mutations.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


# --------------------------- the contract handed to the CLI ---------------------------

def test_stop_shells_the_engine_verb_for_the_watched_checkout(switch):
    verb, fixtures = switch
    res = verb.stop(SLUG)
    assert res["ok"] is True and res["verb"] == "stop"
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["stop", "--repo", PATH], "must target the watched checkout's path"
    assert "--json" in argv, "the adapter parses the machine-readable body, never the prose"


def test_stop_signs_the_request_with_the_operator_and_a_command_center_source(switch):
    verb, fixtures = switch
    verb.stop(SLUG)
    mut = _mutations(fixtures)[-1]
    assert mut["kind"] == "runner_stop"
    assert mut["operator"] == "William", "the marker records WHO stopped it (audit)"
    assert mut["source"] == "command-center", "…and WHAT asked, so a tap is distinguishable from a CLI run"


def test_start_shells_the_other_half_with_the_same_audit_fields(switch):
    verb, fixtures = switch
    res = verb.start(SLUG)
    assert res["ok"] is True and res["verb"] == "start"
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["start", "--repo", PATH]
    assert "--json" in argv
    mut = _mutations(fixtures)[-1]
    assert mut["kind"] == "runner_start" and mut["operator"] == "William"
    assert mut["source"] == "command-center"


def test_an_unwatched_repo_is_refused_before_any_subprocess_runs(switch):
    verb, fixtures = switch
    for res in (verb.stop("someone/else"), verb.start("someone/else")):
        assert res["ok"] is False and res["error"] == "unknown repo"
        assert res["summary"]["level"] == "err"
    assert _calls(fixtures) == [], "a stray/forged repo must never reach the CLI at all"


def test_an_operatorless_install_omits_the_flag_rather_than_inventing_a_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    verb = stop_mod.StopSwitch("/nonexistent/configured", {SLUG: PATH})
    verb.stop(SLUG)
    assert "--operator" not in _calls(tmp_path)[-1]["argv"]


# --------------------------- the outcomes, told honestly ---------------------------

def test_a_completed_stop_says_the_runner_is_stopped(switch):
    verb, _ = switch
    res = verb.stop(SLUG)
    assert res["process_gone"] is True
    s = res["summary"]
    assert s["level"] == "ok" and s["stopped"] is True
    assert "stopped" in s["headline"].lower()
    # The consequence the owner came for: the guardians will not undo this.
    assert any("watchdog" in ln.lower() for ln in s["lines"])
    assert any("superlooper start" in ln for ln in s["lines"]), "the way back on is always shown"


def test_a_stop_the_runner_has_not_finished_yet_is_never_shown_as_stopped(switch, monkeypatch):
    # The designed clean stop on a long tick: the engine exits ZERO with process_gone false. A
    # summary that said "the runner is stopped" here would be a false all-clear over a runner that
    # is still merging.
    monkeypatch.setenv("SL_STOP_OUTCOME", "recorded")
    verb, _ = switch
    res = verb.stop(SLUG)
    assert res["ok"] is True and res["process_gone"] is False
    s = res["summary"]
    assert s["stopped"] is False, "the process is still up — this is not 'stopped' yet"
    assert s["level"] == "ok", "…but it IS the designed clean stop, not a failure"
    assert "not gone yet" in s["headline"].lower()
    assert any("nothing will start it again" in ln.lower() for ln in s["lines"])


def test_a_stop_with_no_live_runner_records_the_half_that_keeps_it_down(switch, monkeypatch):
    monkeypatch.setenv("SL_STOP_OUTCOME", "no_runner")
    verb, _ = switch
    s = verb.stop(SLUG)["summary"]
    assert s["level"] == "ok" and s["stopped"] is True
    assert any("no runner was live" in ln.lower() for ln in s["lines"])


def test_a_stop_launchd_did_not_hold_is_shown_as_incomplete_never_as_a_stop(switch, monkeypatch):
    monkeypatch.setenv("SL_STOP_OUTCOME", "not_held")
    verb, _ = switch
    res = verb.stop(SLUG)
    assert res["ok"] is False, "the CLI's nonzero body is parsed, not swallowed as a crash"
    s = res["summary"]
    assert s["level"] == "err" and s["stopped"] is False
    assert s["headline"].startswith("STOP INCOMPLETE")
    assert "still loaded" in s["headline"], "the engine's own diagnosis is relayed, not paraphrased"
    assert s["remedy"], "a failure always names what to do next"
    # The marker OUTLIVES the failed stop, and that is the fact the owner has to be told: the
    # runner is still up and still watched, but the record stands the moment it does go down.
    assert any("still recorded" in ln.lower() for ln in s["lines"])


def test_a_stop_that_could_not_record_itself_says_nothing_was_taken_down(switch, monkeypatch):
    monkeypatch.setenv("SL_STOP_OUTCOME", "marker_failed")
    verb, _ = switch
    s = verb.stop(SLUG)["summary"]
    assert s["level"] == "err" and s["stopped"] is False
    assert "could not be written" in s["headline"]
    # Nothing was recorded on this path, so the marker-outlives-the-failure line must NOT appear —
    # telling the owner a stop is on the books when none is would be its own fabrication.
    assert not any("still recorded" in ln.lower() for ln in s["lines"])


def test_a_started_runner_is_reported_with_its_pid_and_a_cleared_stop(switch):
    verb, _ = switch
    res = verb.start(SLUG)
    s = res["summary"]
    assert s["level"] == "ok" and s["started"] is True
    assert any("4242" in ln for ln in s["lines"])
    assert any("watchdog" in ln.lower() for ln in s["lines"]), "the stop is withdrawn — say so"


def test_a_pane_home_start_never_claims_to_have_started_anything(switch, monkeypatch):
    # `start` cannot open a session window (automated placement is owner-ruled out). It hands back
    # the one line to type and leaves the stop standing — and BOTH have to reach the owner or the
    # button strands them: they would read "started", see nothing running, and have no next step.
    monkeypatch.setenv("SL_START_OUTCOME", "manual")
    verb, _ = switch
    res = verb.start(SLUG)
    assert res["ok"] is True and res["started"] is False
    s = res["summary"]
    assert s["started"] is False and s["level"] == "warn"
    assert "nothing was started" in s["headline"].lower()
    assert any("superlooper run --repo" in ln for ln in s["lines"]), "the manual line is relayed verbatim"
    assert any("still stands" in ln.lower() for ln in s["lines"]), "the stop was NOT withdrawn"


def test_a_start_with_no_installed_job_fails_without_touching_the_stop(switch, monkeypatch):
    monkeypatch.setenv("SL_START_OUTCOME", "no_job")
    verb, _ = switch
    res = verb.start(SLUG)
    s = res["summary"]
    assert res["ok"] is False and s["level"] == "err" and s["started"] is False
    assert s["headline"].startswith("START FAILED")
    assert "no LaunchAgent is installed" in s["headline"]
    assert "runner-home" in (s["remedy"] or "")


def test_a_latched_off_switch_is_an_error_even_though_the_runner_came_up(switch, monkeypatch):
    # The worst start outcome: the runner is UP but the marker will not come off, so the watchdog
    # stays stood down over a live runner, silently and forever. It must not read as a clean start.
    monkeypatch.setenv("SL_START_OUTCOME", "latched")
    verb, _ = switch
    s = verb.start(SLUG)["summary"]
    assert s["level"] == "err" and s["started"] is False
    assert "could not be removed" in s["headline"]
    assert s["remedy"]


# --------------------------- failing closed ---------------------------

def test_a_missing_cli_is_a_plain_error_naming_the_binary_never_a_silent_success(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", "/nonexistent/definitely-not-here")
    verb = stop_mod.StopSwitch("/nonexistent/configured", {SLUG: PATH})
    res = verb.stop(SLUG)
    assert res["ok"] is False and res["summary"]["stopped"] is False
    assert "/nonexistent/definitely-not-here" in res["error"]
    assert "superlooper_cli" in res["error"], "name the config key the operator has to fix"


def test_a_timeout_fails_closed_rather_than_hanging_the_dialog(switch, monkeypatch):
    monkeypatch.setenv("SL_TIDY_SLEEP", "1")
    verb, _ = switch
    verb._timeout = 0.05
    res = verb.stop(SLUG)
    assert res["ok"] is False and res["summary"]["level"] == "err"
    assert "timed out" in res["error"]


def test_unparseable_output_is_an_error_not_an_assumed_stop(switch, monkeypatch):
    monkeypatch.setenv("SL_STOP_GARBAGE", "1")
    verb, _ = switch
    res = verb.stop(SLUG)
    assert res["ok"] is False and res["summary"]["stopped"] is False


def test_the_adapter_never_raises_whatever_the_cli_does(switch, monkeypatch):
    # Every failure path is an honest result object. A raise here would 500 the endpoint and the
    # owner would learn nothing about whether their loop is running.
    verb, _ = switch
    for var in ("SL_STOP_GARBAGE", "SL_TIDY_FAIL"):
        monkeypatch.setenv(var, "1")
        assert verb.stop(SLUG)["ok"] is False
        monkeypatch.delenv(var)


# --------------------------- the pure summary, on its own ---------------------------

def test_summarize_is_pure_and_total_over_a_body_it_has_never_seen():
    # The CLI is a separate, gated codebase: an engine republished with a field this build has not
    # met must degrade to an honest, non-committal summary — never a KeyError inside a poll thread,
    # and never an invented success.
    s = stop_mod.summarize({"verb": "stop", "ok": True})
    assert s["level"] == "ok" and s["stopped"] is False
    s = stop_mod.summarize({})
    assert s["level"] == "err" and s["stopped"] is False and s["headline"]
    s = stop_mod.summarize({"verb": "start", "ok": False})
    assert s["level"] == "err" and s["started"] is False and s["headline"]


def test_the_summary_never_names_a_session_host():
    # Issue #310/#333: the dashboard names no host, anywhere. Every sentence this module composes
    # has to be host-neutral — the engine's own `manual` string is relayed as DATA, never authored
    # here, which is exactly what lets it stay true when the host changes.
    from pathlib import Path as _P
    src = (_P(stop_mod.__file__)).read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for host in ("cmux", "herdr", "tmux", "iterm", "wezterm", "kitty"):
        assert host not in body.lower(), "lib/stopswitch must never name a session host"


def test_a_start_that_found_a_live_runner_never_says_the_loop_is_off(switch, monkeypatch):
    # The engine's `loaded is None and pid is not None` outcome: a runner IS live, but launchd could
    # not be asked whether its job is loaded, so `start` deliberately acts on nothing — ok:true,
    # started:false, already_running:true, with a live pid. Reading `started` alone and concluding
    # "the loop is still off" would tell the owner their running loop is down, which is the exact
    # inversion of the fact they opened this dialog to learn.
    monkeypatch.setenv("SL_START_OUTCOME", "live_unconfirmed")
    verb, _ = switch
    res = verb.start(SLUG)
    assert res["ok"] is True and res["started"] is False and res["already_running"] is True
    s = res["summary"]
    assert "still off" not in s["headline"], "a live runner must never be reported as off"
    assert "already live" in s["headline"] and "4242" in s["headline"]
    assert s["started"] is False, "…and nothing was started, which is also true"
    assert any("launchd could not be asked" in ln for ln in s["lines"])
