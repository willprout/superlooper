/* airfield_motion.js — PURE geometry for issue #203 (holding-pattern phase separation + the
   airborne idle wander). No canvas, no DOM: numbers in, numbers out. This is deliberately its own
   file so the same math the eye sees in airfield_live.js is math that can be checked in isolation
   — it loads in the browser (window.AirfieldMotion) AND exports to node (module.exports), so a
   harness can prove the invariants the repo's JS-less pytest can't (no two holders coincide, the
   wander never leaves its bound, no two planes move in sync). airfield_live.js binds these to
   pixels and owns nothing semantic (design record B.1).

   Everything here is a pure function of its arguments — no time source of its own, no randomness,
   no state. The caller passes the clock (t seconds) and the seed (a flight number); the same
   inputs always give the same output, frame to frame and across reloads (a fresh random each frame
   would be jitter, not drift). */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;   // node
  root.AirfieldMotion = api;                                                // browser
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  var TWO_PI = Math.PI * 2;

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // Deterministic per-plane pseudo-random in [0,1). A flight number + a channel index → a stable,
  // well-spread unit value via integer bit-mixing (a small avalanche hash). This spreads ADJACENT
  // flight numbers cleanly — the sin-fract hash it replaces occasionally handed two neighbouring
  // flights near-equal periods, which reads as two planes drifting in sync. No Math.random: the
  // wander must reproduce frame to frame, or it is jitter rather than slow drift.
  function unit(seed, channel) {
    var h = (((seed | 0) + 1) * 374761393 + ((channel | 0) + 1) * 668265263) | 0;
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    h = (h ^ (h >>> 16)) >>> 0;             // unsigned 32-bit
    return h / 4294967296;
  }

  // ---- 1. holding separation (owner ruling #1) -------------------------------------------------
  // N holders distributed by phase around the pattern: two on opposite sides (0, π), three at
  // thirds, and so on. The slot is JUST an index — even spacing carries no landing order, which
  // stays in the tags and boards. count <= 1 → a single holder sits at phase 0.
  function holdPhase(slot, count) {
    if (count <= 1) return 0;
    return (slot % count) / count * TWO_PI;
  }

  // Shortest signed delta to steer `from` toward `to` (both radians). Used by the holding ease-in
  // so a joining/leaving holder slides the SHORT way to its evenly-spaced slot, never the long way.
  function angleDelta(from, to) {
    var d = (to - from) % TWO_PI;
    if (d > Math.PI) d -= TWO_PI;
    if (d < -Math.PI) d += TWO_PI;
    return d;
  }

  // ---- 2. the idle wander (owner ruling #2) ----------------------------------------------------
  // A slow, BOUNDED drift around the anchor: one sine per axis with a per-plane frequency AND
  // phase. |dx| ≤ boundX and |dy| ≤ boundY hold by construction (amplitude * sin), so a plane can
  // never leave its box (DoD). The seed spreads the frequencies and phases so no two planes share a
  // period or a starting phase — nothing moves in sync. t is seconds.
  function wanderOffset(seed, t, boundX, boundY) {
    var fx = 0.30 + unit(seed, 0) * 0.22;   // 0.30–0.52 rad/s → ~12–21 s axis periods, distinct per plane
    var fy = 0.35 + unit(seed, 1) * 0.22;   // 0.35–0.57 rad/s → ~11–18 s (a gentle, perceptible sway, not jitter)
    var px = unit(seed, 2) * TWO_PI;
    var py = unit(seed, 3) * TWO_PI;
    return { dx: boundX * Math.sin(t * fx + px), dy: boundY * Math.sin(t * fy + py) };
  }

  // The allowed span for one axis: the wander box [anchor - bound, anchor + bound] intersected with
  // the on-canvas safe zone [half, size - half]. If the anchor already sits past the safe zone (a
  // `final` plane rests a hair off the right edge by design), the span widens to include the anchor
  // on that side — so the wander may nudge such a plane inward but NEVER further off-canvas than it
  // already was. Returns [lo, hi] with lo <= hi.
  function axisSpan(anchor, bound, half, size) {
    var fieldLo = Math.min(anchor, half), fieldHi = Math.max(anchor, size - half);
    var lo = Math.max(anchor - bound, fieldLo);
    var hi = Math.min(anchor + bound, fieldHi);
    if (lo > hi) { lo = hi = anchor; }      // degenerate box → pin to anchor
    return [lo, hi];
  }

  // Simple separation (owner ruling #2). After everyone has wandered, push any two whose centres
  // fall within `minDist`, and nudge any mover off a banner rectangle — then re-clamp every mover
  // back inside its OWN span (a plane never leaves its bound to dodge a neighbour; DoD). One
  // relaxation pass is enough: the wandering set is pre-spaced (downwind anchors are 54px apart, so
  // two leg planes stay ≥46px apart even fully wandered) — this only resolves the rare near-touch
  // with the holding stack or the banner, a nudge within-bound, not a physics solver.
  //
  //   movers: [{x, y, xlo, xhi, ylo, yhi}]  — x/y current centre, [xlo,xhi]/[ylo,yhi] the span.
  //           Give an IMMOVABLE obstacle (e.g. a holder) an equal lo==hi==its position: it stays
  //           put and hands its full share of the push to the movable partner.
  //   banners: [{x, y, w, h}] | {x,y,w,h} | null  — towed cloths a wanderer must not drift onto (one
  //           per downwind leg plane, issue #204; a lone rect is still accepted). Best-effort within
  //           the mover's bound: a plane whose ANCHOR already sits under a cloth can't clear it in
  //           ±bound (that static overlap is banner-PLACEMENT's concern — bannerRects keeps every
  //           cloth below the hulls so it never arises) — the guarantee here is only that the WANDER
  //           never drives a plane further onto a cloth than its anchor already was.
  function separate(movers, minDist, banners) {
    for (var i = 0; i < movers.length; i++) {
      for (var j = i + 1; j < movers.length; j++) {
        var a = movers[i], b = movers[j];
        var ddx = b.x - a.x, ddy = b.y - a.y;
        var d = Math.sqrt(ddx * ddx + ddy * ddy);
        if (d >= minDist) continue;
        // Split the push by mobility: an immovable partner (lo==hi) takes none, so the movable one
        // absorbs the whole separation instead of only half (then the clamp keeps it in bound).
        var wa = movable(a) ? 1 : 0, wb = movable(b) ? 1 : 0;
        if (wa + wb === 0) continue;                 // two immovable obstacles: nothing to do
        var need = minDist - d;
        var ux, uy;
        if (d > 0) { ux = ddx / d; uy = ddy / d; } else { ux = 1; uy = 0; }   // exactly stacked → split along x
        a.x -= ux * need * (wa / (wa + wb)); a.y -= uy * need * (wa / (wa + wb));
        b.x += ux * need * (wb / (wa + wb)); b.y += uy * need * (wb / (wa + wb));
      }
    }
    if (banners) {
      var rects = banners.length === undefined ? [banners] : banners;   // accept one rect or a list
      for (var r = 0; r < rects.length; r++) {
        for (var k = 0; k < movers.length; k++) if (movable(movers[k])) pushOutOfRect(movers[k], rects[r]);
      }
    }
    for (var m = 0; m < movers.length; m++) {
      movers[m].x = clamp(movers[m].x, movers[m].xlo, movers[m].xhi);
      movers[m].y = clamp(movers[m].y, movers[m].ylo, movers[m].yhi);
    }
    return movers;
  }

  // ---- 3. towed-banner placement (issue #204, owner ruling #3) ---------------------------------
  // Every plane on the downwind leg tows a name cloth. A single westward tow AT leg altitude would
  // cover the western neighbour (the exact bug #204 fixes — the one cloth hid a neighbouring plane
  // that itself showed no name), so the cloths STAGGER into two horizontal lanes just BELOW the
  // leg. Occlusion-free BY CONSTRUCTION for any subset of the four downwind slots:
  //
  //   * Both lanes sit below every hull (laneY ≥ hull bottom + the #203 ±3 down-wander), so NO cloth
  //     ever overlaps a plane — at rest OR drifting.
  //   * The four downwind anchors are ≥54px apart. Sorting the towing planes by x and alternating
  //     lanes (index parity) puts every ADJACENT pair — the only pairs closer than 108px — in
  //     DIFFERENT lanes (different y ⇒ can't overlap), while any two planes SHARING a lane are ≥2
  //     apart in x-order, hence ≥108px apart — wider than the 74px cloth (the widest same-lane
  //     gap-closer: 108 − 74 = 34px clear). So no two cloths overlap either.
  //
  //   planes: [{num, x, halfW}]  x = STABLE anchor centre-x; halfW = hull half-width (tow clearance)
  //   cfg:    {bw, bh, laneY, laneH, tow}  cloth w/h · top-lane y · lane pitch · gap west of the hull
  // Returns [{num, x, y, w, h, lane}] — one cloth rect per input plane, in INPUT order (the caller
  // maps back by num). The rects are anchored to the STABLE x (never the wandering hull) so the
  // drawn cloth stays aligned with its pinned HTML text while the plane drifts (issue #203 wander).
  function bannerRects(planes, cfg) {
    var lane = {};
    planes.map(function (p, i) { return { x: p.x, num: p.num, i: i }; })
      .sort(function (a, b) { return a.x - b.x || a.num - b.num; })
      .forEach(function (o, k) { lane[o.i] = k % 2; });
    return planes.map(function (p, i) {
      var ln = lane[i];
      var right = p.x - p.halfW - cfg.tow;         // cloth's east edge, a hair west of the hull
      return { num: p.num, x: right - cfg.bw, y: cfg.laneY + ln * cfg.laneH,
               w: cfg.bw, h: cfg.bh, lane: ln };
    });
  }

  // Axis-aligned overlap test (a shared helper so the live engine and the node harness ask the same
  // question): do rects a and b share any area? Touching edges (== ) do NOT count as overlap.
  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  }

  function movable(p) { return p.xlo !== p.xhi || p.ylo !== p.yhi; }

  // Eject a mover from a rectangle along the SHALLOWER axis (least motion — a nudge, not a jump).
  // A small pad keeps the hull off the cloth, not merely centre-out.
  function pushOutOfRect(p, r) {
    var pad = 2;
    var cx = r.x + r.w / 2, cy = r.y + r.h / 2;
    var halfw = r.w / 2 + pad, halfh = r.h / 2 + pad;
    var ox = halfw - Math.abs(p.x - cx), oy = halfh - Math.abs(p.y - cy);
    if (ox <= 0 || oy <= 0) return;             // already clear on some axis
    if (ox < oy) p.x += (p.x < cx ? -ox : ox);
    else p.y += (p.y < cy ? -oy : oy);
  }

  return {
    TWO_PI: TWO_PI,
    clamp: clamp,
    unit: unit,
    holdPhase: holdPhase,
    angleDelta: angleDelta,
    wanderOffset: wanderOffset,
    axisSpan: axisSpan,
    separate: separate,
    bannerRects: bannerRects,
    rectsOverlap: rectsOverlap
  };
});
