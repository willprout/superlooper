"""Guard (issue #28): an EMPTY Needs You collapses to a slim all-clear ribbon and the airfield
gains the reclaimed width (design record §4: "Empty Needs You collapses to an 'all clear' ribbon").

The server already tells the front-end when nothing waits: ``assemble_snapshot`` sets
``all_clear`` (``lib/server``, pinned in ``tests/test_snapshot``). What was missing was the PIXEL
half — the panel kept occupying its full ~320px column even when empty, so the field never widened.
The fix is presentation-only (design record B.1 — semantics server-side, pixels client-side):
shell.js toggles a ``needs-collapsed`` class on ``.main`` off ``all_clear``, needsyou.js renders a
slim rail instead of the full panel, and shell.css narrows the Needs You track while the field's
``1fr`` track soaks up the reclaimed space.

Like ``test_static_tower_scroll``, these are string guards on the shipped static bundle, not
behavioural tests (the repo runs no JS engine — Python stdlib only). They exist so a future edit
that drops the collapse or re-widens the empty column fails CI instead of silently regressing. The
rendered proof that BOTH states (empty ribbon, full panel with a card) look right lives in the PR's
screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_NEEDS_JS = (_STATIC / "needsyou.js").read_text(encoding="utf-8")


def _rule_body(css, selector):
    """The declaration block for the FIRST ``selector { ... }`` rule (declarations only, no nested
    braces in these flat rules). Returns "" when the selector is absent."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def _strip_js_comments(js):
    """Drop block comments and whole-line ``//`` comments so a guard binds the CODE, not a comment
    that happens to mention the same word (Codex review, issue #28). Whole-line only, so ``//`` inside
    a string literal (never at line-start in this bundle) is left untouched."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


_SHELL_CODE = _strip_js_comments(_SHELL_JS)
_NEEDS_CODE = _strip_js_comments(_NEEDS_JS)


def _first_track_px(columns):
    """The pixel width of the FIRST track in a grid-template-columns value, or None if it is not a
    plain px length (e.g. ``1fr``/``auto``)."""
    first = columns.strip().split()[0]
    m = re.match(r"^(\d+(?:\.\d+)?)px$", first)
    return float(m.group(1)) if m else None


def _grid_columns(body):
    m = re.search(r"grid-template-columns\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else ""


def test_collapsed_main_narrows_needs_and_keeps_the_field_flexible():
    # Baseline: the default three-column main gives Needs You a wide fixed track.
    base_cols = _grid_columns(_rule_body(_CSS, ".main"))
    base_first = _first_track_px(base_cols)
    assert base_first is not None, ".main must keep a fixed first (Needs You) track"

    # The collapsed variant is the whole point of issue #28: a MUCH narrower first track so the
    # empty panel stops occupying a full column.
    collapsed_body = _rule_body(_CSS, ".main.needs-collapsed")
    assert collapsed_body, (
        ".main.needs-collapsed must exist so an empty Needs You collapses its column (issue #28)")
    collapsed_cols = _grid_columns(collapsed_body)
    collapsed_first = _first_track_px(collapsed_cols)
    assert collapsed_first is not None, "the collapsed first track must be a fixed slim px width"
    assert collapsed_first < base_first, (
        "the collapsed Needs You track (%s) must be narrower than the default (%s) (issue #28)"
        % (collapsed_first, base_first))
    assert collapsed_first <= 120, (
        "a 'slim ribbon' must be genuinely slim, not a merely-smaller column (issue #28)")
    # The reclaimed width must go to the airfield: the middle (field) track stays flexible (1fr),
    # so narrowing column 1 widens the field rather than the tower.
    assert "1fr" in collapsed_cols, (
        "the field track must stay 1fr in the collapsed grid so the airfield gains the width (issue #28)")


def test_collapsed_needs_rail_is_styled_as_a_ribbon():
    # The slim state is a styled RAIL, not a bare unstyled div — the all-clear ribbon fills the
    # narrowed column. Reuse of the existing green ribbon look keeps calm reading as calm (§5).
    assert _rule_body(_CSS, ".needs.collapsed") or _rule_body(_CSS, ".ribbon-allclear.rail"), (
        "a collapsed rail style (.needs.collapsed / .ribbon-allclear.rail) must exist (issue #28)")


def test_shell_toggles_the_collapse_class_off_all_clear():
    # shell.js decides NO semantics: it binds the server's all_clear flag to the collapse class on
    # .main. The 2s poll rebuilds #root from the fresh snapshot, so a decision appearing flips
    # all_clear false and restores the full panel with no reload (design record §4).
    #
    # Bind the actual CONDITIONAL, not just co-located strings: needs-collapsed must sit in the
    # truthy branch of an all_clear test on one line, so the guard fails if a future edit applies the
    # class unconditionally or drops the gate (Codex review, issue #28). Comments are stripped first
    # so the word "all_clear" in the explanatory comment can never satisfy the guard on its own.
    assert re.search(r"all_clear\s*\?[^;\n]*needs-collapsed", _SHELL_CODE), (
        "shell.js must add 'needs-collapsed' to .main ONLY in the truthy branch of an all_clear test "
        "— gated on the server flag, never applied unconditionally (issue #28)")


def test_needsyou_renders_a_slim_ribbon_when_empty_and_the_full_panel_otherwise():
    # The collapse must be EXCLUSIVE: the empty case early-returns the slim rail, and the full panel
    # is the fall-through after it — never both. Assert the branch STRUCTURE (order + early return),
    # not just that the strings exist somewhere, so a future edit that renders both, or hoists the
    # rail out of the empty branch, fails the guard (Codex review, issue #28). Comments stripped so
    # the ordering reflects code, not prose.
    empty = _NEEDS_CODE.find("!needs.length")
    assert empty != -1, "needsyou.js must special-case the empty (!needs.length) branch (issue #28)"
    rail = _NEEDS_CODE.find("needs collapsed", empty)
    panel = _NEEDS_CODE.find("panel needs", empty)
    assert rail != -1 and panel != -1, "both the collapsed rail and the full panel must exist (issue #28)"
    assert rail < panel, (
        "the empty case must render the collapsed rail BEFORE (and instead of) the full panel (issue #28)")
    # The empty branch must EARLY-RETURN the rail — otherwise the full panel below still renders too.
    assert "return" in _NEEDS_CODE[empty:rail], (
        "the empty (!needs.length) branch must early-return the collapsed rail, never fall through "
        "into the full panel (issue #28)")
    # Content checks: the rail carries the all-clear ribbon + an explicit caption (§5 — calm captioned),
    # and the full panel still leads with its title + count badge (card content unchanged, in scope).
    assert "ribbon-allclear" in _NEEDS_CODE[empty:panel], (
        "the collapsed rail must render the all-clear ribbon (issue #28)")
    assert re.search(r"all clear", _NEEDS_CODE[empty:panel], re.IGNORECASE), (
        "the collapsed ribbon must carry an explicit 'all clear' caption (§5, issue #28)")
    assert "NEEDS YOU" in _NEEDS_CODE[panel:] and "panel-title" in _NEEDS_CODE[panel:], (
        "the full Needs You panel (title) must still render when a decision is waiting (issue #28)")
    assert "badge" in _NEEDS_CODE[panel:], (
        "the waiting-decision panel must still carry its count badge (issue #28)")
