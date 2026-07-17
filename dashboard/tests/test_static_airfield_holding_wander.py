"""Guard (issue #203): the holding pattern separates by phase, and airborne circuit-leg planes
get a bounded idle wander — both PURELY VISUAL (owner rulings, design record B.1: pixels, no
semantics). Two changes:

  1. Holding separation. Two flights in the holding pattern used to circle visually stacked
     (same orbit angle on near-concentric rings — 10px apart, hulls ~30px, so indistinguishable).
     The fix distributes holders by PHASE around the pattern (two on opposite sides, three at
     thirds, ...) so no two ever coincide. Landing order stays in the tags/boards, never in the
     ring geometry (owner ruling #1).

  2. Idle wander. Airborne circuit-leg planes (takeoff/downwind/base-turn/final) hung perfectly
     motionless, reading as "stalled" while the work was in fact moving. Each now drifts slowly
     within a small BOUNDED box around its anchor, with per-plane randomized period/phase (no two
     in sync) and a simple separation pass so wanderers never drift onto each other or a banner
     (owner ruling #2). The GROUND stays still, and reduced-motion disables the wander entirely
     (owner ruling #3 / DoD).

The pure geometry lives in static/airfield_motion.js (no canvas, no DOM — numbers in, numbers
out) so it is verifiable in isolation; airfield_live.js only binds it to pixels (design B.1).

Like the other field guards (issues #22/#27/#30/#32), these are STRING guards on the shipped
static bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit
drops a seam. The rendered proof that it LOOKS right (a separated hold + drifting leg planes,
joy included) and the numeric proof of the invariants (a node harness over the pure module) live
in the PR's screenshot / review evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_MOTION = (_STATIC / "airfield_motion.js").read_text(encoding="utf-8")
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")


def _strip_js_comments(js):
    """Drop block + whole-line comments so a guard binds the CODE, not prose (the shared pattern of
    the other static guards — see test_static_first_paint.py)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


def _fn_body(code, name):
    """Body of ``function <name>(...) { ... }`` by brace-matching. "" when absent."""
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


# =============================== the pure motion module exists and is dual-loadable ===============================

def test_motion_module_is_loadable_in_browser_and_node():
    # The pure geometry must load in the browser (window.AirfieldMotion) AND export to node
    # (module.exports) so the SAME math the eye sees is the math a harness can check in isolation.
    assert "AirfieldMotion" in _MOTION, "airfield_motion.js must publish AirfieldMotion"
    assert "module.exports" in _MOTION, (
        "airfield_motion.js must export for node (module.exports) so the pure math is testable "
        "outside the browser — the repo runs no JS engine in CI")


def test_index_loads_motion_before_the_live_engine():
    # airfield_live.js reads window.AirfieldMotion at mount, so the module must be parsed first.
    mi = _INDEX.find("airfield_motion.js")
    li = _INDEX.find("airfield_live.js")
    assert mi != -1, "index.html must load /airfield_motion.js"
    assert li != -1 and mi < li, (
        "index.html must load airfield_motion.js BEFORE airfield_live.js (the engine binds it)")


def test_live_engine_binds_the_pure_module():
    assert "window.AirfieldMotion" in _LIVE, (
        "airfield_live.js must bind window.AirfieldMotion — the pure geometry (design B.1)")


# =============================== 1. holding: distribute by phase ===============================

def test_holdphase_distributes_evenly_by_count():
    body = _fn_body(_MOTION_CODE, "holdPhase")
    assert body, "airfield_motion.js must define holdPhase(slot, count) — the even phase spacing"
    # Even distribution is slot/count of a full turn: two on opposite sides, three at thirds.
    assert re.search(r"/\s*count", body) and "TWO_PI" in body, (
        "holdPhase must space slots evenly around the ring (slot/count * TWO_PI) — owner ruling #1")


def test_holding_anchor_carries_a_distributed_phase():
    # The holding orbit anchor must carry a per-plane phase from holdPhase, so holders sit at
    # distinct angles instead of stacking at one angle on near-concentric rings.
    assert re.search(r"holdPhase", _LIVE_CODE), (
        "airfield_live.js must call M.holdPhase to place each holder at its own angle (issue #203)")
    assert re.search(r"\.phase\b", _LIVE_CODE) and re.search(r"\bphase\b", _LIVE_CODE), (
        "the holding orbit anchor must carry a `phase` the step() loop renders at")


def test_holders_share_a_base_angle_and_ease_to_their_slot():
    # All holders advance a SHARED base angle so `base + phase` stays evenly spaced regardless of
    # entry time; each eases the short way to its slot (angleDelta) so a joining/leaving holder
    # re-spaces smoothly instead of teleporting.
    assert "holdBase" in _LIVE_CODE, (
        "airfield_live.js must keep a shared holdBase so holders stay evenly distributed")
    assert "angleDelta" in _LIVE_CODE, (
        "a holder must ease the short way toward base+phase (M.angleDelta) — no long-way spin")


def test_reduced_motion_still_separates_the_hold():
    # Under prefers-reduced-motion there is no orbit loop, but the ONE honest still must still place
    # holders at their distinct phases — otherwise they stack again at the ellipse's east point.
    assert re.search(r"reduced", _LIVE_CODE), "airfield_live.js must branch on reduced motion"
    # the orbit branch must run under reduced (to place statically at phase), not be gated out of it
    assert not re.search(r"target\.orbit\s*&&\s*!reduced", _LIVE_CODE), (
        "the holding placement must run under reduced motion too (static, but phase-distributed) — "
        "gating it out (`orbit && !reduced`) re-stacks holders at one point")


# =============================== 2. wander: bounded, per-plane, ground-still ===============================

def test_wander_offset_is_bounded_and_per_plane():
    body = _fn_body(_MOTION_CODE, "wanderOffset")
    assert body, "airfield_motion.js must define wanderOffset(seed, t, boundX, boundY)"
    # Bounded BY CONSTRUCTION: amplitude * sin(...) can never exceed the bound (a plane never leaves
    # its box). Per-plane: frequency AND phase derive from the seed, so no two share a period/phase.
    assert "Math.sin" in body, "the drift must be a sine (bounded by its amplitude) — DoD 'no plane leaves its bound'"
    assert re.search(r"unit\(\s*seed", body), (
        "frequency/phase must derive from the per-plane seed (unit(seed, ...)) — DoD 'no two in sync'")


def test_wander_is_deterministic_not_random():
    # A fresh Math.random each frame would be jitter, not drift, and would jitter differently every
    # reload. The wander must be a pure function of (seed, t): reproducible frame to frame. (Check the
    # CODE, not the prose — the module's comments legitimately mention "No Math.random".)
    assert "Math.random" not in _MOTION_CODE, (
        "the wander must be deterministic (a function of seed+time), never Math.random — jitter, "
        "and it would move the overlay-anchored perception around")


def test_live_engine_wanders_only_the_downwind_leg():
    # The idle wander applies to the downwind LEG only — the long straight leg over Build Island
    # where a motionless plane read as stalled, and the only airborne stage whose anchors (54px
    # apart) are separable by a bounded ±4 wander. The runway-owned stages (takeoff/base-turn/final)
    # fan their slots out 12–16px apart, closer than a hull, so wandering them could not honour "no
    # plane overlaps another"; they are deliberately excluded (a follow-up would re-space them).
    assert "wanderOffset" in _LIVE_CODE, "airfield_live.js must call M.wanderOffset for the leg"
    m = re.search(r"WANDER_STAGES\s*=\s*\{([^}]*)\}", _LIVE_CODE)
    assert m, "airfield_live.js must declare an explicit WANDER_STAGES allow-set"
    wset = m.group(1)
    assert "downwind" in wset, "the downwind leg must wander (issue #203)"
    for tight in ("takeoff", "base-turn", "final"):
        assert tight not in wset, (
            "the runway-owned airborne stage %r has sub-hull-spaced fan-out anchors and must NOT "
            "wander until they are re-spaced (overlap invariant) — deferred from #203" % tight)
    for ground in ("at-stand", "parked", "taxi-out", "taxi-in", "touchdown"):
        assert ground not in wset, (
            "a ground/taxi stage (%r) must never wander — stillness on the ground is the point "
            "(owner ruling #3)" % ground)
    for offpath in ("awaiting", "session-frozen", "stranded"):
        assert offpath not in wset, (
            "an off-path state (%r) must render still in place, not hover (design §5)" % offpath)


def test_banner_cloth_is_pinned_to_the_stable_anchor_so_the_towing_plane_may_wander():
    # Issue #204 supersedes #203's "hold the banner-towing plane still". Now EVERY downwind plane
    # tows its own name cloth, so holding them all still would cost the leg the very life #203 gave
    # it. Instead the CLOTH is pinned to the plane's STABLE anchor (bannerLayout reads s.target,
    # never the wandering s.cur) — so the drawn cloth and its anchor-pinned HTML text stay aligned
    # while the hull keeps drifting; only a short leader flexes between them. Two guarantees:
    #   (a) the cloth-placement helper anchors to s.target, not the wandering hull, and
    #   (b) the wander flag no longer carves the banner plane out (downwind still wanders).
    body = _fn_body(_LIVE_CODE, "bannerLayout")
    assert body, "airfield_live.js must define bannerLayout() — the pinned cloth rects (issue #204)"
    assert "s.target" in body and "s.cur" not in body, (
        "the towed cloth must be pinned to the STABLE anchor (s.target), never the wandering hull "
        "(s.cur) — otherwise the cloth slides out from under its pinned text (issue #204)")
    assert not re.search(r"wanders\s*=[^;]*banner", _LIVE_CODE), (
        "the wander flag must NOT exclude the banner-towing flight any more — pinning the cloth "
        "(not the plane) is what keeps the text aligned, so downwind planes keep their wander (#204)")


def test_a_holder_that_stays_a_holder_never_transits():
    # A holder re-slotted by a membership change must re-space via the phase ease, NOT the
    # stage-transit path — routing it through the transit would fly every remaining holder to the
    # ellipse's east point at once and momentarily re-stack them (the very bug #203 fixes).
    assert re.search(r"wasOrbit\s*&&\s*isOrbit", _LIVE_CODE), (
        "the moved/transit check must treat orbit→orbit as NOT a transit (holders re-space by ease)")


def test_wander_requires_air_and_is_disabled_under_reduced_motion():
    # The wander guard must require the plane be airborne AND animation be allowed. reduced-motion
    # disables the idle wander entirely (DoD), and a ground plane never lifts.
    assert re.search(r"wanders\b", _LIVE_CODE), (
        "airfield_live.js must gate the wander on a per-sprite `wanders` flag")
    # the flag must require an airborne anchor
    assert re.search(r"wanders\s*=\s*[^;]*\.air", _LIVE_CODE), (
        "a plane only wanders when its anchor is airborne (anchor.air) — the ground stays still")
    # and the per-frame application must be gated by !reduced
    assert re.search(r"wanders\s*&&\s*!reduced|!reduced\s*&&[^\n]*wanders", _LIVE_CODE), (
        "reduced-motion must disable the idle wander entirely (DoD)")


def test_separation_pass_keeps_wanderers_apart():
    # A simple separation pass must run so wandering planes never drift onto each other or a banner
    # (owner ruling #2). The pure resolver lives in the motion module.
    assert _fn_body(_MOTION_CODE, "separate"), (
        "airfield_motion.js must define separate(...) — the push-apart resolver")
    assert re.search(r"\bseparate\s*\(", _LIVE_CODE), (
        "airfield_live.js must run the separation pass over the wandering planes")


# =============================== overlay tags stay pinned to stable anchors ===============================

def test_overlay_tags_pin_to_the_stable_anchor_not_the_wandering_hull():
    # The wander moves the HULL (s.cur), never the overlay tag: tags read the stable target anchor
    # so a drifting plane's label never jitters (DoD). Guard that no layoutTags.push reads s.cur.
    body = _fn_body(_LIVE_CODE, "update")
    assert body, "airfield_live.js must define update()"
    # isolate the tag-building pushes and assert none of them position off s.cur
    for m in re.finditer(r"layoutTags\.push\(\s*\{(.*?)\}\s*\)", body, flags=re.S):
        chunk = m.group(1)
        assert "s.cur" not in chunk, (
            "overlay tags must anchor to the stable target (anchor.x / HOLD.cx), never the wandering "
            "hull s.cur — otherwise the wander jitters the label (DoD)")
