"""Guards (issue #44): two late-night hazards on the shipped static bundle.

1. The ACTIVE repo tab must be *unmistakable at a glance* — not merely a subtle ``.on`` class.
   A verb (approve/drop) must never land on the wrong repo because the operator misread which
   terminal is on camera. So the lit tab stacks several glance signals in the 16-bit menu idiom:
   a bright filled chip, bolder than its recessive siblings, a wobbling **selection cursor** (▸),
   a soft glow, and a **camera notch** tying it to the field it is steering. Reduced motion stills
   the cursor blink.

2. The DROP confirm must *name its consequence in plain words*. "Drop — tap again" names the
   gesture but not the meaning; drop CLOSES the issue for good ("never-mind"), the far pole from
   approve's "release to build". The armed (second-tap) state now raises a plain-language
   consequence caption so a mid-confirm Drop can never be mistaken for an Approve — while keeping
   the existing two-tap, state-survives-the-poll behavior (design record §4; no browser confirm()).

Like the other ``test_static_*`` guards, these are string checks on the shipped bundle, not
behavioural tests (the repo runs no JS engine — Python stdlib only). The rendered proof that both
states look right lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_NEEDS_JS = (_STATIC / "needsyou.js").read_text(encoding="utf-8")


def _rule_body(css, selector):
    """Declaration block for the FIRST ``selector { ... }`` rule (these rules are flat — no nested
    braces). Returns "" when the selector is absent. ``.repo-tab`` matches only the bare rule, not
    ``.repo-tab.on`` / ``.repo-tab:hover`` (those have non-``{`` chars after the escaped selector)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def _strip_js_comments(js):
    """Drop block + whole-line ``//`` comments so a guard binds the CODE, not a comment that
    happens to mention the same word (Codex review pattern, issue #28)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


_SHELL_CODE = _strip_js_comments(_SHELL_JS)
_NEEDS_CODE = _strip_js_comments(_NEEDS_JS)


# =============================== the active repo tab (GLANCE test) ===============================

def test_active_repo_tab_is_unmistakable_beyond_a_subtle_class():
    # The lit tab must carry glance signals the base tab does NOT: a real fill, a glow the base
    # lacks, and a heavier weight. Three stacked cues so "which repo is on camera" reads across the
    # room, not just from a one-line class swap (issue #44).
    base = _rule_body(_CSS, ".repo-tab")
    on = _rule_body(_CSS, ".repo-tab.on")
    assert base, ".repo-tab base rule must exist"
    assert on, ".repo-tab.on must style the active tab"

    # A real filled background (not the base's transparent chip).
    assert re.search(r"background\s*:\s*(?!none)\S", on), (
        "the active tab must have a real filled background, not a transparent outline (issue #44)")
    # A glow the inactive tab does not have — a signal impossible to mistake for 'off'.
    assert "box-shadow" in on and "box-shadow" not in base, (
        "the active tab must add a glow (box-shadow) the inactive tab lacks (issue #44)")
    # Heavier weight than the recessive siblings.
    assert re.search(r"font-weight\s*:\s*(?:700|800|bold)", on), (
        "the active tab must be bolder than its inactive siblings (issue #44)")


def test_active_repo_tab_wears_a_16bit_selection_cursor():
    # The classic 16-bit menu pointer (▸): a ::before cursor present ONLY on the active tab. A gross
    # shape landmark readable at a distance where text is not.
    before = _rule_body(_CSS, ".repo-tab.on::before")
    assert before, ".repo-tab.on::before must render a selection cursor (issue #44)"
    m = re.search(r"content\s*:\s*(['\"])(.*?)\1", before)
    assert m and m.group(2).strip(), (
        "the selection cursor must have a non-empty content glyph (issue #44)")


def test_active_repo_tab_has_a_camera_notch_pointing_into_its_field():
    # A downward pixel triangle under the lit tab, tying it to the field below (this repo is the one
    # on camera). Built with the standard transparent-border triangle trick + a solid downward edge.
    after = _rule_body(_CSS, ".repo-tab.on::after")
    assert after, ".repo-tab.on::after must render the camera notch (issue #44)"
    assert re.search(r"position\s*:\s*absolute", after), "the camera notch must be absolutely placed"
    assert re.search(r"border-top\s*:\s*\d", after), (
        "the camera notch must be a downward triangle (a solid border-top edge) (issue #44)")


def test_inactive_repo_tabs_stay_recessive_so_the_contrast_is_stark():
    # The lit tab is loud; the inactive ones must read as 'not it'. The base tab keeps a recessive
    # (faint/muted) text colour so the on/off delta is unmistakable side by side.
    base = _rule_body(_CSS, ".repo-tab")
    on = _rule_body(_CSS, ".repo-tab.on")
    base_color = re.search(r"(?<!-)color\s*:\s*([^;]+)", base)
    on_color = re.search(r"(?<!-)color\s*:\s*([^;]+)", on)
    assert base_color and on_color, "both tab states must set a text colour"
    assert base_color.group(1).strip() != on_color.group(1).strip(), (
        "the active and inactive tabs must not share a text colour (issue #44)")
    assert re.search(r"var\(--(?:faint|muted)\)", base_color.group(1)), (
        "inactive tabs must use a recessive (faint/muted) colour so the lit tab stands out (issue #44)")


def test_active_tab_cursor_blink_respects_reduced_motion():
    # The 16-bit cursor blink is delight, never nagging: a prefers-reduced-motion block must still
    # the ::before animation (§0.2 / accessibility). Bind precisely — the reduced-motion OVERRIDE
    # (animation: none) exists AND sits under a prefers-reduced-motion media query.
    m = re.search(r"\.repo-tab\.on::before\s*\{[^}]*animation\s*:\s*none", _CSS)
    assert m, "reduced motion must set the active-tab cursor animation to none (issue #44)"
    preceding = _CSS[max(0, m.start() - 200):m.start()]
    assert "prefers-reduced-motion" in preceding, (
        "the stilled cursor override must live under a prefers-reduced-motion block (issue #44)")


def test_shell_marks_only_the_active_tab_as_aria_current():
    # shell.js decides NO semantics; the active index is presentation (i === selected). It must tag
    # the lit tab with aria-current so the 'on camera' state is exposed to a11y AND semantically
    # single. The attribute must sit in the TRUTHY branch of the `on` test, never unconditionally.
    assert "aria-current" in _SHELL_CODE, "shell.js must expose the active tab via aria-current (issue #44)"
    assert re.search(r"on\s*\?[^)]*aria-current", _SHELL_CODE), (
        "aria-current must be gated on the active-tab (`on ? ...`) branch, not applied to every tab "
        "(issue #44)")
    # The existing selection logic (which tab is `on`) is unchanged.
    assert re.search(r"i\s*===\s*Math\.min\(\s*state\.repoIndex", _SHELL_CODE), (
        "the active-tab selection (i === repoIndex) must remain intact (issue #44)")


# =============================== the drop confirm (states its consequence) ========================

def _drop_consequence_branch(code):
    """The STRING the confirming ternary builds for the .drop-consequence caption — the truthy arm of
    ``dropConsequence = confirming ? <this> : ""``. Binding guards to this captured arm (not to the
    whole file) means a phrase moved out of the rendered caption fails the guard (Codex review)."""
    m = re.search(r"dropConsequence\s*=\s*confirming\s*\?(.*?):\s*\"\"", code, re.S)
    return m.group(1) if m else ""


def test_drop_confirm_names_the_consequence_in_plain_language():
    # The armed state must raise a plain-language consequence caption (a .drop-consequence element)
    # built in the truthy branch of `confirming`, naming the meaning: drop CLOSES it for good —
    # never-mind, NOT release (the far pole from approve). "Drop — tap again" alone named the gesture
    # but not the meaning (issue #44). Bind to the CAPTURED branch, never to co-located prose.
    branch = _drop_consequence_branch(_NEEDS_CODE)
    assert branch, "needsyou.js must build a dropConsequence caption in the confirming branch (issue #44)"
    assert "drop-consequence" in branch, "the confirming branch must render a .drop-consequence caption"
    for phrase in ("for good", "never-mind", "not release"):
        assert phrase in branch, (
            "the drop consequence must say %r in plain words, in the rendered caption (issue #44)" % phrase)
    # It names the UNIQUE destructive target — repo AND number. Needs You is whole-field, so two repos
    # can each carry a #7; the number alone is ambiguous (Codex review, issue #44).
    assert "c.num" in branch, "the consequence caption must name the issue number (issue #44)"
    assert "c.repo" in branch, (
        "the consequence caption must name the repo so the destructive target is unique (issue #44)")


def test_drop_confirm_keeps_the_two_tap_gesture_and_survives_the_poll():
    # DoD: the existing two-tap, state-survives-re-render behaviour stays intact. The armed button
    # still carries the "tap again" gesture, still keys off `confirming`, still fires data-act=drop;
    # and `confirming` is still derived from the caller-threaded confirmingDrop (so the 2s re-render
    # can't silently disarm a mid-confirm Drop — design record §4).
    assert re.search(r"confirming\s*\?[^;]*tap again", _NEEDS_CODE), (
        "the armed drop button must still name the two-tap gesture ('tap again') (issue #44)")
    assert 'data-act="drop"' in _NEEDS_CODE, "the drop button must still fire data-act=drop"
    assert re.search(r"confirming\s*=\s*confirmingDrop\s*===\s*\(\s*c\.repo", _NEEDS_CODE), (
        "the confirming flag must still derive from the caller-threaded confirmingDrop === repo#num, "
        "so the poll re-render preserves a mid-confirm Drop (design record §4)")
    # panelHTML still threads confirmingDrop through to every card.
    assert re.search(r"panelHTML\(\s*needs\s*,\s*confirmingDrop\s*\)", _NEEDS_CODE), (
        "panelHTML must still accept and thread confirmingDrop (state survives the poll)")


def test_drop_consequence_reads_as_destructive_not_neutral():
    # The caption must read as a stop-and-think warning (red family), grouping visually with the
    # armed red Drop button — never a neutral note that a hurried operator skims past (§5: destructive
    # gets the red pole; amber is 'awaiting a decision').
    rule = _rule_body(_CSS, ".card .drop-consequence") or _rule_body(_CSS, ".drop-consequence")
    assert rule, "a .drop-consequence style must exist (issue #44)"
    assert re.search(r"var\(--red\)", rule) or re.search(r"#(?:C0392B|FBEAE7)", rule, re.I), (
        "the consequence caption must read in the destructive red family, not neutral (issue #44)")
