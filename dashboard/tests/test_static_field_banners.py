"""Guard (issue #204): every airborne circuit-leg (downwind) plane tows its OWN name cloth, chosen
server-side as a LIST and placed occlusion-free by the client — no cloth ever covers a plane or
another cloth. William watched the single featured cloth hide a neighbouring in-flight plane that
itself showed no name; "which flight is that?" wasn't on the screen.

Two seams, kept honest here:

  1. The server list. lib/server.py derives ``field_banners`` (a LIST, one {num,label,text} per
     on-field downwind leg flight), generalising the former single ``field_banner`` pick — its own
     selection logic (holding/final/takeoff/base-turn excluded, ordered by number) is unit-tested
     in tests/test_snapshot.py. Here we only guard that the SHIPPED client binds the plural key.

  2. The occlusion-free placement. The pure geometry lives in static/airfield_motion.js
     (bannerRects: stagger the cloths into two horizontal lanes below the leg — no canvas, no DOM,
     numbers in/out) so the SAME math the eye sees is checkable in isolation; airfield_live.js binds
     it to pixels and field.js lays out one .fld-banner span per cloth (design B.1).

Like the other field guards (issues #22/#27/#30/#32/#203), these are STRING guards on the shipped
static bundle — the repo runs no JS engine in CI (Python stdlib only). They fail CI if a future
edit drops a seam. The NUMERIC proof that the placement is occlusion-free across every subset of the
four downwind slots (a node harness over bannerRects) and the RENDERED proof that it looks right
(two readable cloths, joy included) live in the PR's review / screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_MOTION = (_STATIC / "airfield_motion.js").read_text(encoding="utf-8")
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")


def _strip_js_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


def _fn_body(code, name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", code)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i + 1:j]
    return ""


_MOTION_CODE = _strip_js_comments(_MOTION)
_LIVE_CODE = _strip_js_comments(_LIVE)
_FIELD_CODE = _strip_js_comments(_FIELD)


# =============================== the pure placement module ===============================

def test_motion_module_defines_and_exports_bannerRects():
    body = _fn_body(_MOTION_CODE, "bannerRects")
    assert body, "airfield_motion.js must define bannerRects(planes, cfg) — the occlusion-free placer"
    assert re.search(r"bannerRects\s*:\s*bannerRects", _MOTION_CODE), (
        "bannerRects must be exported on the module api so the node harness can check the invariant")


def test_bannerRects_staggers_into_lanes_by_x_order_parity():
    # Occlusion-free BY CONSTRUCTION: sort the towing planes by x and alternate lanes (index % 2), so
    # every adjacent pair (the only <108px-apart pairs) lands in different lanes while same-lane pairs
    # sit ≥108px apart — wider than the 74px cloth. The proof over all slot subsets is the harness's.
    body = _fn_body(_MOTION_CODE, "bannerRects")
    assert re.search(r"sort\s*\(", body), "bannerRects must sort the planes (by x) to assign lanes"
    assert "% 2" in body or "%2" in body, (
        "bannerRects must alternate two lanes by x-order parity (k % 2) — the occlusion-free stagger")
    assert re.search(r"laneY", body) and re.search(r"laneH", body), (
        "bannerRects must place each cloth at cfg.laneY + lane*cfg.laneH (the two horizontal lanes)")


def test_lanes_sit_below_the_downwind_hulls_even_under_the_wander():
    # Both lanes must sit BELOW every hull so no cloth overlaps a plane. The downwind anchor is y=30
    # with a 33px hull (bottom ≈ 46.5), and #203 lets it wander ±3 in y (bottom → 49.5), so the top
    # lane must clear the WANDERED bottom, not just the resting one (review fix, issue #204). The
    # numeric proof over every slot subset, wander applied, lives in the PR's node-harness evidence.
    m = re.search(r"BANNER\s*=\s*\{([^}]*)\}", _LIVE_CODE)
    assert m, "airfield_live.js must declare a BANNER geometry config"
    laney = re.search(r"laneY\s*:\s*(\d+)", m.group(1))
    assert laney and int(laney.group(1)) >= 50, (
        "the top banner lane must sit at/below y=50 — clear of the downwind hull bottom under full "
        "down-wander (46.5 + 3 = 49.5) so no cloth overlaps a plane, at rest OR drifting (issue #204)")


# =============================== the live engine binds the placer ===============================

def test_live_engine_binds_bannerRects_and_carries_a_banner_list():
    assert "M.bannerRects" in _LIVE_CODE, (
        "airfield_live.js must place the cloths via the pure module (M.bannerRects) — design B.1")
    # the model carries a LIST now, never the old single pick
    assert re.search(r"model\s*=\s*\{[^}]*banners\s*:", _LIVE_CODE), (
        "the engine model must carry `banners` (a list), replacing the single `banner`")
    assert not re.search(r"\bm\.banner\b(?!s)", _LIVE_CODE), (
        "no reference to the old single m.banner may remain — it is a list now (m.banners)")


def test_cloth_is_suppressed_mid_transit_and_only_on_the_downwind_leg():
    # The rendered cloth must never float clothless: a plane mid-transit (s.path) or not actually on
    # the downwind leg tows NO cloth. bannerLayout gates on exactly that before handing a plane to the
    # placer — so the HTML text, laid out from the same list, disappears in lockstep.
    body = _fn_body(_LIVE_CODE, "bannerLayout")
    assert body, "airfield_live.js must define bannerLayout()"
    assert "s.path" in body, (
        "bannerLayout must skip a plane mid-transit (s.path) — no clothless text overlay (issue #204)")
    assert "'downwind'" in body or '"downwind"' in body, (
        "bannerLayout must only tow a cloth for a plane on the downwind leg (placementOf === downwind)")


def test_leader_uses_the_wandering_hull_but_the_cloth_stays_pinned():
    # The drawn cloth is pinned (bannerLayout → s.target), but the LEADER connects the wandering hull
    # (s.cur) to it, so the rope flexes with the drift while the cloth and text hold — the visible
    # sign that the plane is alive without the text ever sliding off.
    assert "drawLeader" in _LIVE_CODE, "airfield_live.js must draw a tow-leader (drawLeader)"


# =============================== field.js lays out one span per cloth ===============================

def test_field_binds_the_plural_key_and_renders_one_span_per_cloth():
    assert "repo.field_banners" in _FIELD_CODE, (
        "field.js must read repo.field_banners (the server's list) — squint test, design B.1")
    assert not re.search(r"repo\.field_banner\b(?!s)", _FIELD_CODE), (
        "field.js must not read the old singular repo.field_banner — it is a list now")
    # one .fld-banner span per layout entry: a map/join over layout.banners, not a single element
    assert re.search(r"layout\.banners", _FIELD_CODE), (
        "field.js must lay out one text cloth per engine-returned banner rect (layout.banners)")
    assert re.search(r'class="fld-banner"', _FIELD_CODE), (
        "field.js must render .fld-banner spans for the towed name text")


def test_field_matches_cloth_text_to_rect_by_flight_number():
    # The engine returns rects keyed by num; the server text is keyed by num; field.js joins them so a
    # staggered rect always carries ITS OWN plane's name (never a mismatch after reordering).
    body = _fn_body(_FIELD_CODE, "placeOverlays")
    assert body, "field.js must define placeOverlays()"
    assert re.search(r"textByNum|\.num\b", body), (
        "field.js must match each banner rect to its server text by flight number")
