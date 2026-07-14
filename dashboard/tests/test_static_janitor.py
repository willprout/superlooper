"""Issue #121 — the Janitor button + its sweep dialog (the shipped static bundle).

Janitor is the dashboard's SECOND ops-verb button (same LOCAL COMMAND class as Tidy): tapping it
runs ``superlooper janitor`` (via the server) to sweep GitHub-side debris — stale merged/superseded
``sl/*`` branches, open ``superseded`` PRs, aged parked/needs-owner issues. Owner ruling 2026-07-13:
full CLI parity without leaving the dashboard. The flow is a bright line of this issue:

    button → server runs `janitor --json` → dialog GROUPS the proposals by kind → the owner selects
    EXACTLY the subset he wants → confirm → server runs `janitor --json --execute-keys <subset>` →
    per-item result shown honestly (a failure is never a silent success).

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle
(the same discipline as ``test_static_tidy.py``). They exist so a future edit that drops the confirm
gate, the per-item consent, the grouping, or the failure surfacing fails CI instead of silently
letting the button sweep GitHub without asking. The rendered proof that it LOOKS right — and is
still delightful (§0.1) — lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_JAN_JS = (_STATIC / "janitor.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_index_loads_janitor_js_before_shell():
    assert "/janitor.js" in _INDEX, "index.html must load the Janitor overlay bundle"
    assert _INDEX.index("/janitor.js") < _INDEX.index("/shell.js"), (
        "janitor.js must load before shell.js so window.CCJanitor exists when the button binds it")


def test_janitor_flow_is_two_step_propose_then_execute():
    assert "/api/janitor/propose" in _JAN_JS, "janitor.js must fetch the proposal snapshot first"
    assert re.search(r"[\"']/api/janitor[\"']", _JAN_JS), (
        "janitor.js must POST to /api/janitor to execute the tapped subset")


def test_nothing_executes_without_an_in_ui_confirm():
    assert "data-jan-confirm" in _JAN_JS, (
        "janitor.js must render an explicit confirm control (data-jan-confirm) before executing")
    assert re.search(r"data-jan-confirm[\s\S]{0,120}runExecute", _JAN_JS), (
        "the data-jan-confirm control must trigger runExecute")
    assert re.search(r"function runExecute[\s\S]{0,800}?/api/janitor", _JAN_JS), (
        "the /api/janitor execute POST must live inside runExecute, reached only from the confirm")


def test_only_the_selected_subset_is_ever_swept():
    # The core of issue #121: the owner executes EXACTLY the subset he taps — there is no sweep-all
    # that skips per-kind consent. The execute POST sends a `keys` list built from the selection, and
    # the confirm is disabled/blocked when nothing is selected.
    assert "selected" in _JAN_JS, "janitor.js must track a per-item selection set"
    assert re.search(r"keys\s*:", _JAN_JS), (
        "the execute POST must carry a `keys` list — exactly the selected proposal keys")
    assert re.search(r"data-jan-key", _JAN_JS), (
        "each proposal row must carry its stable key (data-jan-key) so selection maps to keys")


def test_dialog_groups_the_proposals_the_server_returned():
    # Design B.1: the dialog binds the server's grouped proposals (kind/label/items) — it never
    # re-derives the janitor's selection rules (that is the CLI's lib/janitor, single source).
    assert re.search(r"\.groups", _JAN_JS), "janitor.js must render the server's grouped proposals"
    assert re.search(r"\.items", _JAN_JS) and re.search(r"\.label", _JAN_JS), (
        "each group shows its label and its items, as the server grouped them")
    assert re.search(r"\.why", _JAN_JS), (
        "each proposal shows the server's one-line why (the consequence the owner is approving)")


def test_command_failure_is_surfaced_not_a_silent_success():
    assert re.search(r"\.ok\b", _JAN_JS), "janitor.js must branch on the honest ok flag"
    assert re.search(r"\.error\b", _JAN_JS), "janitor.js must surface the server's error string"


def test_per_item_results_are_shown_honestly():
    # After execute the CLI returns per-key outcomes (ok/fail/skipped/held); a failed action must be
    # visible, never mistaken for a clean sweep.
    assert re.search(r"\.results", _JAN_JS), "janitor.js must render the per-key results"
    assert re.search(r"outcome", _JAN_JS), "each result shows its outcome (ok/fail/skipped/held)"


def test_execute_targets_the_listed_repo_not_a_mutable_current():
    assert "listedRepo" in _JAN_JS, "janitor.js must track the repo whose proposals are listed"
    assert re.search(r"var\s+repo\s*=\s*listedRepo", _JAN_JS), (
        "runExecute must execute against listedRepo, never a mutable current slug")
    assert re.search(r"myGen\s*!==\s*gen", _JAN_JS), (
        "a superseded / out-of-order propose response must be dropped (a generation guard)")


def test_held_back_actions_are_surfaced():
    # A refused/failed action from a prior sweep is held back by the CLI and reported in `held`; the
    # dialog must surface it (not silently drop it, not auto-retry it).
    assert re.search(r"\.held", _JAN_JS), "janitor.js must surface the server's held-back keys"


def test_shell_has_a_janitor_button_carrying_the_camera_repo():
    m = re.search(r'data-act="janitor-open"[\s\S]{0,140}?data-repo=', _SHELL_JS) or \
        re.search(r'data-repo=[\s\S]{0,140}?data-act="janitor-open"', _SHELL_JS)
    assert m, "shell.js topbar must render a janitor-open button carrying data-repo (the camera repo)"


def test_shell_dispatches_janitor_open_to_the_overlay():
    assert re.search(r'janitor-open', _SHELL_JS), "shell.js must handle the janitor-open action"
    assert re.search(r'CCJanitor', _SHELL_JS), "shell.js must open window.CCJanitor on the tap"


def test_janitor_surfaces_are_styled():
    assert ".cc-janitor" in _CSS, "shell.css must style the .cc-janitor dialog"
    assert ".janitor-btn" in _CSS, "shell.css must style the .janitor-btn top-bar button"
