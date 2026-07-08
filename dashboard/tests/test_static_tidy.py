"""Issue #41 — the Tidy button + its confirm dialog (the shipped static bundle).

Tidy is the dashboard's FIRST ops-verb button and its second button CLASS: tapping it runs the
local ``superlooper tidy`` CLI (via the server) to close finished session windows — never a GitHub
write. The two-step, tap-where-you-read flow is a bright line of this issue:

    button → server runs `tidy --dry-run` → dialog lists EXACTLY that → confirm → server runs
    `tidy --yes` → result shown honestly (a failure is never a silent success).

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle
— the same discipline as ``test_static_tower_routine.py``. They exist so a future edit that drops
the confirm gate, the failure surfacing, or the two-step split fails CI instead of silently letting
the button close windows without asking. The rendered proof that it LOOKS right (the dialog listing
windows; the post-tidy result) lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_TIDY_JS = (_STATIC / "tidy.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_index_loads_tidy_js_before_shell():
    assert "/tidy.js" in _INDEX, "index.html must load the Tidy overlay bundle"
    assert _INDEX.index("/tidy.js") < _INDEX.index("/shell.js"), (
        "tidy.js must load before shell.js so window.CCTidy exists when the button binds it")


def test_tidy_flow_is_two_step_dry_run_then_execute():
    # Both endpoints appear: the dry-run list and the execute close are distinct server calls.
    assert "/api/tidy/dry-run" in _TIDY_JS, "tidy.js must fetch the dry-run list first"
    assert re.search(r"[\"']/api/tidy[\"']", _TIDY_JS), (
        "tidy.js must POST to /api/tidy to execute the close")


def test_nothing_executes_without_an_in_ui_confirm():
    # The execute POST must be gated behind a confirm control the user taps — never fired by the
    # same code path that merely listed the windows. The confirm control carries data-tidy-confirm.
    assert "data-tidy-confirm" in _TIDY_JS, (
        "tidy.js must render an explicit confirm control (data-tidy-confirm) before executing")
    # The confirm control routes to runExecute, and the /api/tidy execute POST lives INSIDE
    # runExecute — so nothing executes except via the confirm tap (never the dry-run/open path).
    assert re.search(r"data-tidy-confirm[\s\S]{0,80}runExecute", _TIDY_JS), (
        "the data-tidy-confirm control must trigger runExecute")
    assert re.search(r"function runExecute[\s\S]{0,600}?/api/tidy", _TIDY_JS), (
        "the /api/tidy execute POST must live inside runExecute, reached only from the confirm")


def test_dialog_lists_the_windows_the_server_returned():
    # Design B.1: the dialog binds the server's structured window rows (id/status/surface) — it
    # never re-parses CLI text (the server already did that in lib/tidy).
    assert re.search(r"\.windows", _TIDY_JS), "tidy.js must render the server's windows list"
    assert re.search(r"\.status", _TIDY_JS) and re.search(r"\.surface", _TIDY_JS), (
        "each listed window shows its status and surface, as the dry-run returned them")


def test_command_failure_is_surfaced_not_a_silent_success():
    # A nonzero exit / missing binary comes back as ok:false with an error string — the dialog must
    # show it, never render a clean 'nothing to tidy' or a success over a failed command.
    assert re.search(r"\.ok\b", _TIDY_JS), "tidy.js must branch on the honest ok flag"
    assert re.search(r"\.error\b", _TIDY_JS), "tidy.js must surface the server's error string on failure"


def test_execute_targets_the_listed_repo_not_a_mutable_current():
    # Confirm must close the EXACT repo whose windows were listed, and a stale/superseded dry-run
    # response must be dropped — so the dialog can never show repo A's windows while confirm closes
    # repo B's (a re-open racing an in-flight dry-run). Codex cross-review, issue #41.
    assert "listedRepo" in _TIDY_JS, "tidy.js must track the repo whose windows are listed"
    assert re.search(r"var\s+repo\s*=\s*listedRepo", _TIDY_JS), (
        "runExecute must execute against listedRepo, never a mutable current slug")
    assert re.search(r"myGen\s*!==\s*gen", _TIDY_JS), (
        "a superseded / out-of-order dry-run response must be dropped (a generation guard)")


def test_shell_has_a_tidy_button_carrying_the_camera_repo():
    # A Tidy button in the top bar, carrying the currently-viewed repo (like the Flag button), so a
    # tap tidies the repo on camera.
    m = re.search(r'data-act="tidy-open"[\s\S]{0,120}?data-repo=', _SHELL_JS) or \
        re.search(r'data-repo=[\s\S]{0,120}?data-act="tidy-open"', _SHELL_JS)
    assert m, "shell.js topbar must render a tidy-open button carrying data-repo (the camera repo)"


def test_shell_dispatches_tidy_open_to_the_overlay():
    assert re.search(r'tidy-open', _SHELL_JS), "shell.js must handle the tidy-open action"
    assert re.search(r'CCTidy', _SHELL_JS), "shell.js must open window.CCTidy on a tidy-open tap"


def test_tidy_surfaces_are_styled():
    assert ".cc-tidy" in _CSS, "shell.css must style the .cc-tidy dialog"
    assert ".tidy-btn" in _CSS, "shell.css must style the .tidy-btn top-bar button"
