"""Guard (issue #36): the TOWER LOG hides routine bookkeeping by default, revealed on demand.

The tower log is the curated comms channel (design record §4) — machine bookkeeping (label
convergence: `relabel`, fired several times per launch as GitHub's read lags the write) does not
belong on the radio. The classification is server-side (`lib/tower.tier`, unit-tested): each row
arrives tagged `tier: "comms" | "routine"`. This file guards the CLIENT half — the shipped static
bundle must hide routine rows by default and offer a small in-panel affordance to reveal them, and
shell.js must own that reveal as view-local state (a pure view toggle, never a write).

These are string guards on the shipped bundle, not behavioural tests (the repo runs no JS engine —
Python stdlib only), the same discipline as test_static_tower_scroll.py. They exist so a future edit
that drops the default-hide or the reveal affordance fails CI instead of silently re-announcing the
noise. The rendered proof that it LOOKS right (relabel repeats gone by default) lives in the PR's
screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_TOWER_JS = (_STATIC / "tower.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_tower_js_reads_the_server_side_tier():
    # The client binds the server's classification (design B.1) — it never re-derives which acts are
    # routine; it reads row.tier and treats "routine" specially.
    assert re.search(r"\.tier", _TOWER_JS), "tower.js must read each row's server-side tier"
    assert '"routine"' in _TOWER_JS or "'routine'" in _TOWER_JS, (
        "tower.js must key off the 'routine' tier to hide those rows")


def test_tower_js_hides_routine_rows_unless_revealed():
    # Default view: routine rows are filtered OUT of the feed unless the reveal flag is set.
    assert re.search(r"tier\s*!==\s*['\"]routine['\"]", _TOWER_JS), (
        "tower.js must filter out routine rows (tier !== 'routine') from the default feed")
    assert re.search(r"showRoutine", _TOWER_JS), (
        "tower.js must accept a showRoutine flag that keeps routine rows when the reader reveals them")


def test_tower_js_offers_an_in_panel_reveal_affordance():
    # A small in-panel affordance reveals the hidden routine rows on demand — its click hook is
    # data-tower-routine, the seam shell.js listens on.
    assert "data-tower-routine" in _TOWER_JS, (
        "tower.js must render a reveal affordance carrying data-tower-routine")


def test_shell_js_owns_reveal_as_view_local_state_and_a_pure_toggle():
    # The reveal is a view toggle, not a write: shell.js keeps showRoutine in view state, flips it on
    # the affordance click, and re-renders — the same shape as the raw-line caret toggle.
    assert "showRoutine" in _SHELL_JS, "shell.js must track showRoutine in view-local state"
    assert re.search(r"data-tower-routine", _SHELL_JS), (
        "shell.js must handle the data-tower-routine affordance click")
    # the handler flips the flag and re-renders (a pure view change — no postJSON write)
    m = re.search(r"data-tower-routine.*?\n(?:.*\n){0,8}?.*render\(\)", _SHELL_JS)
    assert m, "the reveal click must toggle state and call render(), like the caret toggle"


def test_shell_js_passes_reveal_state_into_the_tower_panel():
    # towerHTML must thread state.showRoutine into Tower.panelHTML, or the toggle can never take effect.
    assert re.search(r"panelHTML\([^)]*showRoutine", _SHELL_JS) or re.search(
        r"state\.showRoutine", _SHELL_JS[_SHELL_JS.index("function towerHTML"):
                                         _SHELL_JS.index("function towerHTML") + 400]), (
        "towerHTML must pass state.showRoutine into Tower.panelHTML")


def test_watermark_computation_ignores_routine_rows():
    # The "since you last looked" watermark is a comms-traffic signal — a hidden relabel must not
    # advance it past the newest VISIBLE comms row (#36), which would hide a later-arriving,
    # earlier-stamped comms entry. markTowerSeen skips routine rows when choosing the newest ts.
    m = re.search(r"function markTowerSeen[\s\S]{0,500}?tier\s*===\s*['\"]routine['\"]", _SHELL_JS)
    assert m, "markTowerSeen must skip routine rows when computing the newest-seen ts"


def test_routine_affordance_is_styled():
    # The affordance is a real, glanceable control, not raw text — it has its own CSS rule.
    assert ".tower-routine" in _CSS, "shell.css must style the .tower-routine reveal affordance"
