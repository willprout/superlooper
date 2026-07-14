"""Issue #116 — the Restart button + its confirm dialog (the shipped static bundle).

Restart is the dashboard's SECOND ops-verb button: tapping it asks the LIVE runner (via the server →
`superlooper request-restart`) to restart ITSELF in its own cmux tab — never a GitHub write, and
never a tab launch or placement (owner bright line, 2026-07-09). The two-step, tap-where-you-read
flow is a bright line of this issue:

    button → server runs `request-restart --check` → dialog states EXACTLY what will happen (or, if
    NO runner is live, says so and shows the manual start) → confirm → server runs `request-restart`
    → result shown honestly (a failure is never a silent success).

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle —
the same discipline as ``test_static_tidy.py``. They exist so a future edit that drops the confirm
gate, the plain-words consequence, the dead-runner honesty, or the two-step split fails CI instead
of silently letting the button restart the loop without asking. The rendered proof that it LOOKS
right lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_RESTART_JS = (_STATIC / "restart.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_index_loads_the_restart_bundle_before_shell():
    assert "/restart.js" in _INDEX, "index.html must load the Restart overlay bundle"
    assert _INDEX.index("/restart.js") < _INDEX.index("/shell.js"), (
        "restart.js must load before shell.js so window.CCRestart exists when the button binds it")


def test_restart_flow_is_two_step_check_then_execute():
    # Both endpoints appear: the preflight (is a runner live?) and the request are distinct calls.
    assert "/api/restart/check" in _RESTART_JS, "restart.js must fetch the preflight first"
    assert re.search(r"[\"']/api/restart[\"']", _RESTART_JS), (
        "restart.js must POST to /api/restart to request the restart")


def test_nothing_executes_without_an_in_ui_confirm():
    # The execute POST must be gated behind a confirm control the user taps — never fired by the same
    # code path that merely ran the preflight. The confirm control carries data-restart-confirm.
    assert "data-restart-confirm" in _RESTART_JS, (
        "restart.js must render an explicit confirm control (data-restart-confirm) before executing")
    assert re.search(r"data-restart-confirm[\s\S]{0,80}runExecute", _RESTART_JS), (
        "the data-restart-confirm control must trigger runExecute")
    assert re.search(r"function runExecute[\s\S]{0,700}?/api/restart", _RESTART_JS), (
        "the /api/restart request POST must live inside runExecute, reached only from the confirm")


def test_confirm_states_the_consequence_in_plain_words():
    # The DoD: the dialog states the consequence plainly — finishes the current tick, restarts the
    # loop in its own tab, in-flight worker sessions untouched.
    assert re.search(r"finish the current tick", _RESTART_JS, re.I), (
        "the confirm must say it finishes the current tick")
    assert re.search(r"own (cmux )?tab", _RESTART_JS, re.I), (
        "the confirm must say it restarts the loop in its own tab")
    assert re.search(r"in-flight worker sessions untouched", _RESTART_JS, re.I), (
        "the confirm must say in-flight worker sessions are untouched")


def test_dead_runner_is_honest_and_offers_no_restart():
    # No live runner ⇒ the dialog says so, shows the one-line manual start, and offers NO confirm
    # (nothing to ask) — the button never resurrects or places a loop.
    assert "renderNoRunner" in _RESTART_JS, "restart.js must have a dead-runner branch"
    assert re.search(r"running\s*===\s*false", _RESTART_JS), (
        "restart.js must branch on the honest running:false liveness")
    assert re.search(r"\.manual\b", _RESTART_JS), (
        "the dead-runner branch must show the server's manual one-liner")
    # The no-runner render must NOT contain a confirm control — there is nothing to execute.
    m = re.search(r"function renderNoRunner[\s\S]{0,600}?\n  }", _RESTART_JS)
    assert m and "data-restart-confirm" not in m.group(0), (
        "the no-runner dialog must offer no restart confirm (nothing to launch)")


def test_command_failure_is_surfaced_not_a_silent_success():
    assert re.search(r"\.error\b", _RESTART_JS), "restart.js must surface the server's error string"
    assert "cc-restart-result err" in _RESTART_JS, "restart.js must render an honest failure line"


def test_execute_targets_the_shown_repo_not_a_mutable_current():
    # Confirm must request the EXACT repo the dialog is showing, and a stale/superseded preflight
    # response must be dropped — so it can never show repo A while confirm restarts repo B.
    assert "listedRepo" in _RESTART_JS, "restart.js must track the repo the dialog is showing"
    assert re.search(r"var\s+repo\s*=\s*listedRepo", _RESTART_JS), (
        "runExecute must execute against listedRepo, never a mutable current slug")
    assert re.search(r"myGen\s*!==\s*gen", _RESTART_JS), (
        "a superseded / out-of-order preflight response must be dropped (a generation guard)")


def test_shell_has_a_restart_button_carrying_the_camera_repo():
    m = re.search(r'data-act="restart-open"[\s\S]{0,160}?data-repo=', _SHELL_JS) or \
        re.search(r'data-repo=[\s\S]{0,160}?data-act="restart-open"', _SHELL_JS)
    assert m, "shell.js topbar must render a restart-open button carrying data-repo (the camera repo)"


def test_shell_dispatches_restart_open_to_the_overlay():
    assert re.search(r'restart-open', _SHELL_JS), "shell.js must handle the restart-open action"
    assert re.search(r'CCRestart', _SHELL_JS), "shell.js must open window.CCRestart on a restart-open tap"


def test_restart_surfaces_are_styled():
    assert ".cc-restart" in _CSS, "shell.css must style the .cc-restart dialog"
    assert ".restart-btn" in _CSS, "shell.css must style the .restart-btn top-bar button"
