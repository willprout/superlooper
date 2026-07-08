"""Guard (issue #29): on narrower DESKTOP windows the airfield never gets tiny — the flanking panels
reflow before the field is ever starved.

The airfield is the centerpiece (design record §0.1 "joy is terminal", §4). In the default three-panel
shell the field is the middle ``1fr`` track flanked by two FIXED side columns (Needs You ~320px, Tower
Log ~340px). With no breakpoints those fixed flanks stay put as the window narrows, so every lost pixel
comes straight out of the field's ``1fr`` — measured, it collapses from ~750px at 1600 down to ~174px at
1024 (a postage stamp). The fix is CSS-only and pixels-only (design record B.1): the field itself needs
no canvas rescaling — ``.field-mount`` is ``width:100%`` capped at 800px, so the rendered airfield simply
follows whatever width the grid hands it. Desktop breakpoints make the FLANKS give way first: a wider tier
trims both side tracks, and a narrower tier drops the Tower Log to a full-width row BELOW so the field
springs back to full size beside Needs You alone. Mobile stays deferred (§4).

Like ``test_static_tower_scroll`` / ``test_static_needs_collapse``, these are string guards on the shipped
static bundle, not behavioural tests (the repo runs no JS engine — Python stdlib only). They exist so a
future edit that drops a breakpoint, fixes the field track, or re-starves the airfield fails CI instead of
silently regressing. The rendered proof that the field stays legible at each width lives in the PR's
screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def _strip_comments(css):
    """Drop /* ... */ comments so a guard binds the CSS, not prose that happens to mention a term."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


_CODE = _strip_comments(_CSS)


def _media_blocks(css):
    """(condition, body) for each top-level ``@media ( ... ) { ... }`` block, brace-balanced (so the
    nested ``selector { ... }`` rules inside are captured whole, unlike a flat ``[^}]*`` match)."""
    blocks = []
    i = 0
    while True:
        m = re.search(r"@media([^{]*)\{", css[i:])
        if not m:
            break
        cond = m.group(1).strip()
        start = i + m.end()
        depth, j = 1, start
        while j < len(css) and depth:
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        blocks.append((cond, css[start:j - 1]))
        i = j
    return blocks


def _top_level_css(css):
    """The stylesheet with every ``@media { ... }`` block removed — the default (widest) rules only."""
    out, i = [], 0
    # rebuild by walking and skipping balanced @media blocks
    while i < len(css):
        m = re.search(r"@media[^{]*\{", css[i:])
        if not m:
            out.append(css[i:])
            break
        out.append(css[i:i + m.start()])
        j = i + m.end()
        depth = 1
        while j < len(css) and depth:
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        i = j
    return "".join(out)


def _rule(body, selector):
    """Declaration block for the FIRST exact ``selector { ... }`` in ``body`` ("" if absent)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", body)
    return m.group(1) if m else ""


def _grid_cols(rule_body):
    m = re.search(r"grid-template-columns\s*:\s*([^;]+)", rule_body)
    return m.group(1).strip() if m else ""


def _tracks(cols):
    return cols.split() if cols else []


def _max_width(cond):
    m = re.search(r"max-width\s*:\s*(\d+)px", cond)
    return int(m.group(1)) if m else None


def _fixed_px(track):
    m = re.match(r"^(\d+(?:\.\d+)?)px$", track)
    return float(m.group(1)) if m else None


_TOP = _top_level_css(_CODE)
_MEDIA = _media_blocks(_CODE)
# The desktop reflow tiers: max-width media blocks (in a desktop range) that override .main's columns.
_MAIN_TIERS = [
    (_max_width(cond), body)
    for cond, body in _MEDIA
    if _max_width(cond) and _grid_cols(_rule(body, ".main"))
]


def test_default_main_is_the_field_flanked_by_two_fixed_columns():
    # Baseline (widest): the field is the 1fr middle track flanked by two fixed side columns.
    cols = _grid_cols(_rule(_TOP, ".main"))
    tracks = _tracks(cols)
    assert len(tracks) == 3, ".main must default to a three-track grid (Needs | field | Tower)"
    assert tracks[1] == "1fr", "the field (middle track) must be 1fr so it is the flexible centerpiece"
    assert _fixed_px(tracks[0]) is not None and _fixed_px(tracks[2]) is not None, (
        "the two FLANKING tracks are the fixed ones — the field flexes between them")


def test_desktop_breakpoints_exist_to_reflow_the_main_grid():
    # Without breakpoints the fixed flanks stay put and the field's 1fr collapses on narrow windows.
    # There must be at least two desktop max-width tiers that re-lay .main's columns (issue #29).
    assert len(_MAIN_TIERS) >= 2, (
        "shell.css must add desktop breakpoints (@media max-width) that reflow .main so the airfield "
        "keeps a legible size — none found (issue #29)")
    for mw, _body in _MAIN_TIERS:
        assert 960 <= mw <= 1500, (
            "breakpoints must target DESKTOP window sizes (~1024–1440), not mobile (§4); got %spx" % mw)


def test_the_field_is_the_second_1fr_track_in_every_tier():
    # The whole point: the field NEVER becomes a small fixed track, and it stays the SECOND track —
    # Needs You is track 1, the field is track 2 = 1fr — matching the DOM order Needs → Field. Checking
    # POSITION (tracks[1]), not just "1fr" presence, means a slip like `1fr 264px` (field in the fixed
    # second track) fails instead of passing (Codex review nit, issue #29).
    for label, body in [("default", _TOP)] + [("<=%dpx" % mw, b) for mw, b in _MAIN_TIERS]:
        tracks = _tracks(_grid_cols(_rule(body, ".main")))
        assert len(tracks) >= 2 and tracks[1] == "1fr", (
            "the field must be the SECOND track and 1fr in the %s .main grid, so narrowing the window "
            "narrows the FLANKS and the field gains the reclaimed width, never a fixed small track "
            "(issue #29)" % label)


def test_a_wider_tier_trims_both_flanks_below_the_default():
    # The first line of defence: before anything stacks, trim BOTH fixed flanks so the field keeps its
    # size while all three panels stay in view (great at ~1280/1366).
    default_tracks = _tracks(_grid_cols(_rule(_TOP, ".main")))
    d_left, d_right = _fixed_px(default_tracks[0]), _fixed_px(default_tracks[2])
    trimmed = None
    for mw, body in _MAIN_TIERS:
        tracks = _tracks(_grid_cols(_rule(body, ".main")))
        if len(tracks) == 3:
            left, right = _fixed_px(tracks[0]), _fixed_px(tracks[2])
            if left is not None and right is not None and left < d_left and right < d_right:
                trimmed = (mw, left, right)
                break
    assert trimmed, (
        "a three-column tier must trim BOTH flanking tracks below the default (%s/%s) so the field "
        "keeps its width before any panel has to stack (issue #29)" % (d_left, d_right))


def test_the_narrowest_tier_stacks_the_tower_full_width_and_bounds_it():
    # When trimming is exhausted, the Tower Log drops to a full-width row BELOW: .main becomes a
    # two-track grid (the field flanked by Needs You alone) and the field springs back to full size.
    assert _MAIN_TIERS, "no .main reflow tiers found (issue #29)"
    narrowest_mw, body = min(_MAIN_TIERS, key=lambda t: t[0])
    tracks = _tracks(_grid_cols(_rule(body, ".main")))
    assert len(tracks) == 2 and _fixed_px(tracks[0]) is not None and tracks[1] == "1fr", (
        "the narrowest tier (<=%dpx) must drop .main to a TWO-track grid — Needs You (fixed) then the "
        "field (1fr, second) — so the field is no longer squeezed between two fixed columns (issue #29)"
        % narrowest_mw)
    tower = _rule(body, ".tower")
    assert re.search(r"grid-column\s*:\s*1\s*/\s*-1", tower), (
        "the Tower Log must span the full width (grid-column: 1 / -1) as its own row below the field "
        "in the narrowest tier (issue #29)")
    # The dropped tower's feed must stay a bounded, INTERNALLY-SCROLLING strip (issue #27) — never
    # collapse to empty nor stretch the page. Two things make that true and BOTH must be guarded:
    #   1. a max-height cap (so it scrolls rather than growing its now-auto-height row), and
    #   2. a NON-GROWING flex (flex: 0 …). The base feed is `flex: 1 1 0; min-height: 0`, which with no
    #      tall field sibling to fill would collapse to zero height (the empty-log bug). A guard that
    #      only checked max-height would pass even if the collapse-prone base flex survived, so pin the
    #      override's non-growing flex too (Codex review, issue #29).
    feed = re.search(r"\.tower-feed[^{}]*\{([^}]*)\}", body)
    assert feed, (
        "the narrowest tier must override .tower-feed to bound the dropped Tower Log's feed (issue #29)")
    feed_body = feed.group(1)
    assert re.search(r"max-height\s*:", feed_body), (
        "the dropped feed must keep a max-height so it scrolls internally, never stretching the page "
        "(issue #27 + #29)")
    assert re.search(r"flex\s*:\s*0\b", feed_body) or re.search(r"flex-grow\s*:\s*0\b", feed_body), (
        "the dropped feed must be NON-GROWING (flex: 0 …) — the base flex:1 1 0 would collapse it to "
        "zero height with no tall field sibling to fill (the empty-log bug) (issue #29)")


def test_the_collapsed_all_clear_variant_reflows_in_lockstep():
    # An empty Needs You collapses to a slim rail (#28: .main.needs-collapsed — HIGHER specificity than
    # .main). EVERY tier that reflows .main must ALSO reflow .main.needs-collapsed with the SAME track
    # count — not just "at least one" tier. Otherwise a tier could drop .main to two tracks while the
    # higher-specificity collapsed rule from a WIDER tier stays three tracks, leaving the all_clear grid
    # inconsistent (a 2-track field but a 3-track collapsed rail) below that breakpoint (Codex review,
    # issue #28 + #29).
    assert _MAIN_TIERS, "no .main reflow tiers found (issue #29)"
    for mw, body in _MAIN_TIERS:
        main_tracks = _tracks(_grid_cols(_rule(body, ".main")))
        coll_cols = _grid_cols(_rule(body, ".main.needs-collapsed"))
        assert coll_cols, (
            "the <=%dpx tier reflows .main but not .main.needs-collapsed — the collapsed all_clear "
            "layout would keep a stale wider-tier grid below this breakpoint (issue #28 + #29)" % mw)
        coll_tracks = _tracks(coll_cols)
        assert len(coll_tracks) == len(main_tracks), (
            "the collapsed variant must match .main's track COUNT at <=%dpx (%d vs %d) so the two grids "
            "stay in lockstep (issue #29)" % (mw, len(coll_tracks), len(main_tracks)))
        assert len(coll_tracks) >= 2 and coll_tracks[1] == "1fr", (
            "the collapsed variant must keep the field as its SECOND 1fr track at <=%dpx too (#29)" % mw)


def test_stacking_tier_comes_after_the_trim_tier_in_width_and_in_source_order():
    # Two guards, because the cascade needs BOTH:
    #   (1) breakpoint NUMBERS — the two-track (stacking) tier triggers at a narrower max-width than the
    #       three-track (trimming) tier: you trim first, then stack, never the reverse.
    #   (2) SOURCE ORDER — below the stacking breakpoint BOTH media blocks match, and both set .main at
    #       EQUAL specificity, so the LATER block wins. The stacking block must therefore appear AFTER
    #       the trimming block in the file. Guarding only the numbers (as this test first did) would let
    #       a future reorder pass while the trim tier's 3-track grid silently un-stacked the tower below
    #       1300px (Codex review, issue #29). `_MAIN_TIERS` is built from `_MEDIA` in source order, so a
    #       tier's index here IS its position in the stylesheet.
    two = [(i, mw) for i, (mw, body) in enumerate(_MAIN_TIERS)
           if len(_tracks(_grid_cols(_rule(body, ".main")))) == 2]
    three = [(i, mw) for i, (mw, body) in enumerate(_MAIN_TIERS)
             if len(_tracks(_grid_cols(_rule(body, ".main")))) == 3]
    assert two and three, "need both a trimming (3-track) and a stacking (2-track) tier"
    assert max(mw for _i, mw in two) < max(mw for _i, mw in three), (
        "the stacking tier must trigger at a narrower width than the trimming tier (trim, then stack)")
    assert min(i for i, _mw in two) > max(i for i, _mw in three), (
        "the two-track stacking block must appear AFTER the three-track trimming block in source order, "
        "so it wins the equal-specificity cascade where both media queries match — otherwise the tower "
        "silently un-stacks below the stacking breakpoint (issue #29)")


def test_field_mount_stays_fluid_and_capped_so_no_canvas_logic_is_needed():
    # The invariant that makes a pixels-only reflow work (boundary: no canvas rescaling beyond what the
    # renderer already supports): the field mount is fluid (width:100%) with a max-width cap, and the
    # canvas fills it. The grid hands the mount a width; the airfield follows. Guard it stays.
    mount = _rule(_TOP, ".field-mount")
    assert re.search(r"width\s*:\s*100%", mount), ".field-mount must stay width:100% (fluid) (issue #29)"
    assert re.search(r"max-width\s*:\s*\d+px", mount), (
        ".field-mount must keep a max-width cap so the field is fluid up to a ceiling (issue #29)")
    canvas = _rule(_CODE, ".fld-root canvas.fld-canvas")
    assert re.search(r"width\s*:\s*100%", canvas), (
        "the field canvas must stay width:100% so it follows the mount width (renderer-supported, #29)")
