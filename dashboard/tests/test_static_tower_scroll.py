"""Guard (issue #27): the TOWER LOG must scroll inside its panel, never stretch the page.

Screen 7a's assembled shell gives every panel its own region: the tower log is one column of the
four-panel grid, and the boards sit BELOW that grid. If the tower panel grows with its content it
stretches the grid row and shoves the boards far down the page. The tower row COUNT is already
capped server-side (``lib/server._tower_window``, limit 14), but a count cap is not a HEIGHT cap:
long sentences wrap and any row can expand to its raw journal line, so the feed's height is still
unbounded. The fix is the same one ``.needs-list`` already uses in this grid — an internally
scrolling, height-bounded feed — plus a tiny bit of shell.js so the newest line stays visible
across the 2s poll re-render (which rebuilds ``#root`` wholesale and would otherwise reset the
feed to the oldest line at the top).

These are string guards on the shipped static bundle, not behavioural tests (the repo runs no JS
engine — Python stdlib only). They exist so a future edit that drops the bound or the scroll-pin
fails CI instead of silently reintroducing the page-stretch. The rendered proof that it LOOKS right
lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")


def _rule_body(css, selector):
    """The declaration block for the FIRST ``selector { ... }`` rule (declarations only, no nested
    braces in these flat rules). Returns "" when the selector is absent."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def test_tower_feed_is_height_bounded_with_internal_scroll():
    body = _rule_body(_CSS, ".tower-feed")
    assert body, ".tower-feed rule must exist in shell.css"
    # Internal scroll: the feed owns a vertical scrollbar rather than growing the panel.
    assert re.search(r"overflow-y\s*:\s*auto", body), (
        ".tower-feed must set overflow-y: auto so the log scrolls INSIDE the panel (issue #27)")
    # Bounded height inside the flex panel: flex-grow to fill + min-height:0 so it can actually
    # shrink below its content (without min-height:0 a flex item refuses to scroll and stretches).
    assert re.search(r"min-height\s*:\s*0", body), (
        ".tower-feed must set min-height: 0 or the flex item won't shrink to scroll (issue #27)")
    assert re.search(r"(^|;|\s)flex\s*:", body), (
        ".tower-feed must flex to fill the panel's remaining height (issue #27)")


def test_tower_panel_is_allowed_to_shrink_in_the_grid():
    # The feed's internal scroll only bounds the page if the .tower grid item itself may shrink below
    # its content — otherwise the grid track grows to the feed's full height and the boards get shoved
    # down anyway. min-height:0 on the panel is the load-bearing sibling of the feed's own bound.
    body = _rule_body(_CSS, ".tower")
    assert re.search(r"min-height\s*:\s*0", body), (
        ".tower must keep min-height: 0 so the grid item can shrink and its feed can scroll (issue #27)")


def test_stale_never_scrolls_comment_is_gone():
    # The old comment asserted the row cap meant the feed "never needs an internal scroll" — the
    # exact wrong assumption issue #27 corrects. It must not linger and mislead the next reader.
    assert "never needs an internal scroll" not in _CSS, (
        "the stale 'never needs an internal scroll' comment contradicts the issue #27 fix")


def test_shell_restores_tower_scroll_to_newest_across_rerenders():
    # #root is rebuilt every poll (root.innerHTML = shellHTML()), which resets the feed to the top
    # (oldest). shell.js must re-pin the feed so the NEWEST line stays visible, while preserving a
    # reader who scrolled up into history. The capture/apply pair is the seam that does this.
    assert "captureTowerScroll" in _SHELL_JS and "applyTowerScroll" in _SHELL_JS, (
        "shell.js must capture the tower feed's scroll before the re-render and re-apply it after, "
        "so the newest line stays visible across the 2s poll (issue #27)")
    # The capture must run BEFORE the innerHTML rebuild and the apply AFTER it, or scroll is lost.
    cap = _SHELL_JS.index("captureTowerScroll(")
    rebuild = _SHELL_JS.index("root.innerHTML = shellHTML()")
    apply = _SHELL_JS.index("applyTowerScroll(")
    assert cap < rebuild < apply, (
        "captureTowerScroll must run before the shell rebuild and applyTowerScroll after it")
    # Guard the distance-from-bottom math itself (not just the function names): the pin decision and
    # the restore target both read scrollHeight/scrollTop/clientHeight. Asserting the terms keeps a
    # no-op refactor from passing the guard while quietly dropping the "keep the reader's place" math.
    for term in ("scrollHeight", "scrollTop", "clientHeight"):
        assert term in _SHELL_JS, (
            "the tower scroll math must read " + term + " to keep newest visible / preserve place")


def test_repo_switch_pins_new_repo_tower_to_newest():
    # A repo switch swaps in a DIFFERENT feed; reusing the previous repo's scroll offset could land
    # the new repo's tower on an old line instead of its newest. shell.js flags the switch so the
    # fresh feed pins to the newest line (Codex review, issue #27).
    assert "towerRepin" in _SHELL_JS, (
        "shell.js must flag a repo switch (towerRepin) so the new repo's tower pins to its newest line")
    # The flag is raised on the repo-selector click and consumed by the scroll capture.
    assert re.search(r"state\.towerRepin\s*=\s*true", _SHELL_JS), (
        "the repo selector must set state.towerRepin = true before re-rendering")
    assert re.search(r"state\.towerRepin\b", _SHELL_JS[_SHELL_JS.index("function captureTowerScroll"):]), (
        "captureTowerScroll must honor the towerRepin flag and pin the fresh feed to newest")
