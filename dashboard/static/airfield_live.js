/* Live airfield — the N-flight animation engine over Airfield3 (Task 7).
   Lifted from design/project/airfield_live.js and extended from one demo plane on a fixed loop to
   one plane per REAL flight, each parked at the DISCRETE anchor of its circuit stage (design
   record §3: position never encodes time or fake progress — a plane only moves when its stage
   changes, and that brief transit IS the event you're seeing). Everything semantic arrives
   pre-derived in the model (design B.1): stage, underlying circuit stage, runway index, contrail
   kind, spinning/trouble flags. This file turns values into pixels and motion, nothing else.

   window.AirfieldLive.mount(canvas) -> {
     update(model)  — bind a fresh snapshot slice; returns the overlay layout (logical coords)
                      for the HTML tags field.js places (tags/banner/landmarks)
     hitTest(x, y)  — logical coords -> flight num (planes are tappable: drawer-open)
     destroy()
   }

   model = { time: 'day'|'dusk'|'night', status: 'ok'|'attention'|'alert', dim: bool,
             resetKey: string (repo switch clears sprite state),
             banner: {num, text}|null (towed behind that flight while on the leg),
             flights: [{num, label, stage, circuitStage, runway, contrail, spinning, trouble,
                        tail}] }

   The circuit (counterclockwise, from the prototype): leg E along the top (y 30) → descent arc
   at the east → landing roll W on the runway → climb arc at the west back to the leg. Runway 0
   is the y-82 strip, runway 1 the y-118 strip — a lane's flight owns its runway for takeoff and
   landing (§3). prefers-reduced-motion: no animation loop at all — one honest still per update. */
(function () {
  'use strict';

  var W = 400, H = 270, S = 2;
  var BX = 360, BY = 150;                    // tower beacon (tower3 at 360,150 in drawOverview)
  var HOLD = { cx: 300, cy: 38, rx: 26, ry: 14 };
  var BAYS_STAND = [54, 86, 118];            // at-stand fills the jet-bridged west gates (server caps to this count)
  var BAYS_PARKED = [298, 266, 234, 182];    // parked fills the east gates inward — never mixed
  var STAND_Y = 182;
  var TRANSIT_MS = 1400;                     // one stage-change flight
  var WIND = 9;                              // px/s exhaust drift behind an anchored plane

  // Discrete anchors: placement stage -> slot list (per runway where the stage is runway-owned).
  // Each entry: [x, y, dir, air, small]. Slots beyond the first fan out deterministically.
  function anchorFor(place, runway, slot) {
    var ry = runway === 1 ? 118 : 82;
    switch (place) {
      case 'at-stand':
        return ground(BAYS_STAND[Math.min(slot, BAYS_STAND.length - 1)], STAND_Y, 'S', true);
      case 'parked':
        return ground(BAYS_PARKED[Math.min(slot, BAYS_PARKED.length - 1)], STAND_Y, 'S', true);
      case 'taxi-out':                       // the connector taxiway nearest its runway
        return ground(runway === 1 ? 304 : 44, 140 + slot * 16, 'N', false);
      case 'takeoff':                        // just lifted off the west end, climbing
        return air(runway === 1 ? 24 + slot * 12 : 27 + slot * 12, runway === 1 ? 62 : 45, 'N');
      case 'downwind':                       // the working leg, over Build Island
        return air([170, 224, 116, 278][Math.min(slot, 3)], 30, 'E');
      case 'base-turn':                      // report filed — turning toward the gate
        return air(360 - slot * 16, 35, 'E');
      case 'final':                          // the gate — lined up on its own runway's threshold
        return air(386, (runway === 1 ? 104 : 72) - slot * 14, 'S');
      case 'touchdown':                      // merged — rolling out on its own runway
        return { x: 230 - slot * 56, y: ry, dir: 'W', air: false, small: false, roll: true };
      case 'taxi-in':                        // closed — trundling home along the parallel taxiway
        return ground(150 + slot * 60, 155, 'W', false);
      case 'holding':                        // the drawn holding pattern ("number 2 for landing")
        return { x: HOLD.cx + HOLD.rx + slot * 10, y: HOLD.cy, dir: 'S', air: true, small: false,
                 orbit: { cx: HOLD.cx, cy: HOLD.cy, rx: HOLD.rx + slot * 10, ry: HOLD.ry + slot * 5 } };
      default:
        return air(170, 30, 'E');
    }
    function ground(x, y, dir, small) { return { x: x, y: y, dir: dir, air: false, small: small }; }
    function air(x, y, dir) { return { x: x, y: y, dir: dir, air: true, small: false }; }
  }

  // Where a flight PHYSICALLY sits: on-circuit stages sit at their own anchor; holding sits in
  // the pattern; parked sits at the stalled gates; awaiting/session-frozen/stranded sit at the
  // flight's UNDERLYING circuit position (§5 — the amber ring / grey hull / stranded plane render
  // in place, no magic fix). A stranded gate's circuitStage is 'final', so it sits ON the gate.
  function placementOf(f) {
    if (f.stage === 'holding') return 'holding';
    if (f.stage === 'parked') return 'parked';
    if (f.stage === 'awaiting' || f.stage === 'session-frozen' ||
        f.stage === 'stranded') return f.circuitStage || 'downwind';
    return f.stage;
  }

  function spriteBox(dir, small) {
    var w = small ? 25 : 33, h = small ? 32 : 42;
    var horiz = dir === 'E' || dir === 'W';
    return { w: horiz ? h : w, h: horiz ? w : h };
  }

  function tailPoint(cx, cy, dir, box) {
    if (dir === 'E') return { x: cx - box.w / 2 - 2, y: cy, dx: -1, dy: 0 };
    if (dir === 'W') return { x: cx + box.w / 2 + 2, y: cy, dx: 1, dy: 0 };
    if (dir === 'N') return { x: cx, y: cy + box.h / 2 + 2, dx: 0, dy: 1 };
    return { x: cx, y: cy - box.h / 2 - 2, dx: 0, dy: -1 };
  }

  function ease(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }

  function mount(canvas) {
    var A = window.Airfield3;
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    canvas.width = W * S; canvas.height = H * S;
    var ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = false;

    var base = document.createElement('canvas');
    var baseKey = '';
    var model = { time: 'day', status: 'ok', dim: false, flights: [], banner: null, resetKey: '' };
    var sprites = {};          // num -> persistent sprite state (cur pos, trail, transit)
    var raf = 0, last = 0, frame = 0;

    function rebuildBase() {
      var key = model.time + '|' + model.status;
      if (key === baseKey) return;
      baseKey = key;
      A.drawOverview(base, { time: model.time, status: model.status,
                             noFlight: true, noDemoAircraft: true, liveBeacon: true });
    }

    // ---------- binding a fresh snapshot ----------
    function update(m) {
      if (m.resetKey !== model.resetKey) sprites = {};        // repo switch: no cross-field ghosts
      model = m;
      rebuildBase();

      // Deterministic slot allocation: same flights → same slots, sorted by flight number.
      var byPlace = {};
      var ordered = m.flights.slice().sort(function (a, b) { return a.num - b.num; });
      var layoutTags = [];
      var seen = {};
      var standXs = [];          // x of each plane at the stand — one marquee is placed over them
      ordered.forEach(function (f) {
        var place = placementOf(f);
        var groupKey = place + (place === 'taxi-out' || place === 'takeoff' ||
                                place === 'final' || place === 'touchdown' ? ':' + f.runway : '');
        byPlace[groupKey] = (byPlace[groupKey] || 0);
        var anchor = anchorFor(place, f.runway, byPlace[groupKey]);
        byPlace[groupKey]++;
        seen[f.num] = true;

        var s = sprites[f.num];
        if (!s) {
          s = sprites[f.num] = { cur: { x: anchor.x, y: anchor.y }, dir: anchor.dir,
                                 trail: [], th: -1.2, dustUntil: 0, path: null };
        }
        var moved = !s.target || s.target.x !== anchor.x || s.target.y !== anchor.y ||
                    (!!s.target.orbit) !== (!!anchor.orbit);
        var wasAir = s.target ? s.target.air : anchor.air;
        s.flight = f;
        if (moved && s.target && !reduced) {
          // The transit IS the event: fly from the old anchor to the new one. A landing routes
          // through the runway threshold so touchdown reads as touchdown (plus dust).
          var pts = [{ x: s.cur.x, y: s.cur.y }];
          if (place === 'touchdown' && wasAir) {
            var thr = { x: 391, y: anchor.y };
            pts.push(thr);
            s.dustAt = thr;
          } else { s.dustAt = null; }
          pts.push({ x: anchor.x, y: anchor.y });
          s.path = { pts: pts, t0: performance.now(), dust: place === 'touchdown' && wasAir };
        } else if (moved) {
          s.cur = { x: anchor.x, y: anchor.y };                // reduced motion: honest jump
          s.path = null;
        }
        s.target = anchor;

        // Overlay tags at the STABLE target anchor (never chasing the animation).
        var box = spriteBox(anchor.dir, anchor.small);
        if (f.stage === 'holding') {
          layoutTags.push({ kind: 'hold', x: HOLD.cx, y: HOLD.cy - HOLD.ry - 8,
                            text: 'SL-' + f.num + ' HOLDING — Nº2 FOR LANDING' });
        }
        if (f.stage === 'awaiting') {
          // hangs BELOW the amber ring: the top strip belongs to the holding-pattern tag
          layoutTags.push({ kind: 'amber', x: anchor.x, y: anchor.y + box.h / 2 + 16,
                            text: 'SL-' + f.num + ' · AWAITING YOUR DECISION' });
        }
        if (f.spinning) {
          layoutTags.push({ kind: 'spin', x: anchor.x, y: anchor.y + box.h / 2 + 16,
                            text: 'SL-' + f.num + ' SPINNING? · ALIVE · PROGRESS FLAT' });
        }
        if (f.stage === 'parked') {
          layoutTags.push({ kind: 'mx', x: anchor.x, y: anchor.y + box.h / 2 + 3,
                            text: f.label + ' · MX REQ' });
        }
        if (f.stage === 'at-stand') standXs.push(anchor.x);   // one marquee over them all (below)
        if (f.stage === 'session-frozen') {
          layoutTags.push({ kind: 'frozen', x: anchor.x, y: anchor.y - box.h / 2 - 6,
                            text: f.label + ' · SESSION FROZEN' });
        }
        // A stranded gate is a FINISHED flight the gate never landed — a solid plane held on the
        // threshold, its own gold tag pointing at the runner (issue #22). Never the grey frozen tag.
        if (f.stage === 'stranded') {
          layoutTags.push({ kind: 'stranded', x: anchor.x, y: anchor.y - box.h / 2 - 6,
                            text: f.label + ' · STRANDED AT GATE' });
        }
      });
      Object.keys(sprites).forEach(function (k) { if (!seen[k]) delete sprites[k]; });

      // The queued planes get ONE calm marquee over the whole stand, not a colliding tag per plane
      // — the west gates sit ~32px apart, far too close for per-plane text (issue #32). It names the
      // §3 stage in plain words ("N AT THE STAND"); a healthy, waiting line, never the parked "MX REQ".
      if (standXs.length) {
        var sx = standXs.reduce(function (a, b) { return a + b; }, 0) / standXs.length;
        layoutTags.push({ kind: 'stand', x: Math.round(sx), y: STAND_Y - 24,
                          text: standXs.length + ' AT THE STAND' });
      }

      var banner = null;
      if (m.banner && sprites[m.banner.num]) {
        var bs = sprites[m.banner.num];
        // no banner while the plane is mid-transit — the text overlay must never float clothless
        if (!bs.path && placementOf(bs.flight) === 'downwind') {
          var bw = spriteBox(bs.target.dir, bs.target.small).w;
          banner = { x: bs.target.x - bw / 2 - 84, y: bs.target.y - 4, w: 74, h: 14 };
        }
      }

      if (reduced) draw(performance.now());                    // one honest still per poll
      return { tags: layoutTags, banner: banner, landmarks: landmarkFlags() };
    }

    function landmarkFlags() {
      // Only TRUE claims light up (costume rule 1): a downwind flight IS building, so Build
      // Island lights. Reconcile/Review/CI landmarks stay scenery — the runner journals no
      // per-phase fact that could honestly place a plane over them (known MVP data gap, §9).
      var buildIsland = Object.keys(sprites).some(function (k) {
        var f = sprites[k].flight;
        return f && placementOf(f) === 'downwind';
      });
      return [false, buildIsland, false, false];
    }

    // ---------- per-frame motion ----------
    function step(s, now, dt) {
      if (s.path) {
        var pts = s.path.pts, total = 0, lens = [];
        for (var i = 1; i < pts.length; i++) {
          var L = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
          lens.push(L); total += L;
        }
        var t = Math.min(1, (now - s.path.t0) / (TRANSIT_MS + total * 2));
        var d = ease(t) * total, x = pts[0].x, y = pts[0].y, dirx = 0, diry = 0;
        for (var j = 0; j < lens.length; j++) {
          if (d <= lens[j] || j === lens.length - 1) {
            var p = lens[j] ? d / lens[j] : 1;
            x = pts[j].x + (pts[j + 1].x - pts[j].x) * Math.min(1, p);
            y = pts[j].y + (pts[j + 1].y - pts[j].y) * Math.min(1, p);
            dirx = pts[j + 1].x - pts[j].x; diry = pts[j + 1].y - pts[j].y;
            break;
          }
          d -= lens[j];
        }
        s.cur.x = x; s.cur.y = y;
        s.dir = Math.abs(dirx) > Math.abs(diry) ? (dirx > 0 ? 'E' : 'W') : (diry > 0 ? 'S' : 'N');
        if (s.path.dust && s.dustAt && Math.hypot(x - s.dustAt.x, y - s.dustAt.y) < 6) {
          s.dustUntil = now + 900;
          s.path.dust = false;
        }
        if (t >= 1) {
          s.path = null;
          s.dir = s.target.dir;
          s.th = 0;   // a holding entry lands exactly on the ellipse's east point — no pop
        }
        return;
      }
      if (s.target.orbit && !reduced) {                        // holding: the one true loop
        s.th += dt * 1.15;
        var o = s.target.orbit;
        s.cur.x = o.cx + Math.cos(s.th) * o.rx;
        s.cur.y = o.cy + Math.sin(s.th) * o.ry;
        var vx = -Math.sin(s.th) * o.rx, vy = Math.cos(s.th) * o.ry;
        s.dir = Math.abs(vx) > Math.abs(vy) ? (vx > 0 ? 'E' : 'W') : (vy > 0 ? 'S' : 'N');
        return;
      }
      s.cur.x = s.target.x; s.cur.y = s.target.y; s.dir = s.target.dir;
    }

    // Exhaust: spawn puffs at the tail on a per-kind cadence; puffs drift downwind and fade.
    var SPAWN_MS = { crisp: 200, thin: 430, sputter: 210 };
    function spawnTrail(s, now) {
      var f = s.flight, kind = f.contrail;
      if (!s.target.air || kind === 'none' || !SPAWN_MS[kind]) return;
      if (kind === 'sputter' && (now % 1600) >= 520) return;   // bursty, like the prototype idle gate
      if (now - (s.lastPuff || 0) < SPAWN_MS[kind]) return;
      s.lastPuff = now;
      var box = spriteBox(s.dir, s.target.small);
      var tp = tailPoint(s.cur.x, s.cur.y, s.dir, box);
      s.trail.push({ x: tp.x, y: tp.y, dx: tp.dx, dy: tp.dy, t: now });
      while (s.trail.length > 40) s.trail.shift();
    }

    function drawTrail(s, now) {
      var kind = s.flight.contrail;
      if (kind === 'none') return;
      var maxAge = kind === 'sputter' ? 4.2 : 8;
      var fade = kind === 'sputter' ? 0.22 : 0.12;
      for (var i = 0; i < s.trail.length; i++) {
        var p = s.trail[i], age = (now - p.t) / 1000;
        if (age > maxAge) continue;
        var al = Math.max(0, 1 - age * fade);
        if (kind === 'sputter') al *= 0.75 + 0.25 * Math.sin(now / 90 + i * 1.7);
        if (kind === 'thin') al *= 0.7;
        ctx.fillStyle = 'rgba(255,255,255,' + Math.max(0, al).toFixed(2) + ')';
        var b = al > 0.62 && kind === 'crisp' ? 3 : al > 0.3 ? 2 : 1;
        ctx.fillRect(Math.round(p.x + p.dx * age * WIND) - 1,
                     Math.round(p.y + p.dy * age * WIND) - 1, b, b);
      }
    }

    // Tower status FX — lifted verbatim from the prototype engine (ok breath / attention pulse /
    // alert blink + rings + wash). Reduced motion gets the steady mid-state frame.
    function towerFX(now) {
      if (reduced) now = 0;
      if (model.status === 'ok') {
        var a = 0.12 + 0.06 * Math.sin(now / 900);
        ctx.fillStyle = 'rgba(110,224,138,' + a.toFixed(2) + ')';
        ctx.fillRect(BX - 7, BY - 7, 15, 15);
        ctx.fillStyle = '#6FE08A';
        ctx.fillRect(BX - 1, BY, 3, 2);
        return;
      }
      if (model.status === 'attention') {
        var ph = 0.5 + 0.5 * Math.sin(now / 420);
        var aa = 0.10 + 0.40 * ph;
        ctx.fillStyle = 'rgba(242,179,61,' + aa.toFixed(2) + ')';
        ctx.fillRect(BX - 8, BY - 8, 17, 17);
        ctx.fillStyle = 'rgba(242,179,61,' + (aa * 0.45).toFixed(2) + ')';
        var grow = Math.round(10 + 6 * ph);
        ctx.fillRect(BX - grow, BY - grow, grow * 2 + 1, grow * 2 + 1);
        ctx.fillStyle = ph > 0.7 ? '#FFE9B8' : '#F2B33D';
        ctx.fillRect(BX - 1, BY, 3, 2);
        return;
      }
      var on = reduced ? true : (now % 700) < 380;
      var rp = ((now % 1400) / 1400);
      for (var k = 0; k < 2; k++) {
        var rph = (rp + k * 0.5) % 1;
        var rr = 6 + rph * 26;
        var ral = 0.55 * (1 - rph);
        ctx.strokeStyle = 'rgba(255,90,72,' + ral.toFixed(2) + ')';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(BX + 0.5, BY + 0.5, rr, 0, Math.PI * 2); ctx.stroke();
      }
      if (on) {
        ctx.fillStyle = 'rgba(255,90,72,0.34)';
        ctx.fillRect(BX - 10, BY - 10, 21, 21);
        ctx.fillStyle = '#FF5A48';
        ctx.fillRect(BX - 2, BY - 1, 5, 4);
        ctx.fillStyle = 'rgba(255,90,72,0.5)';
        ctx.fillRect(BX - 14, BY, 5, 1); ctx.fillRect(BX + 10, BY, 5, 1);
        ctx.fillRect(BX, BY - 14, 1, 5); ctx.fillRect(BX, BY + 10, 1, 5);
      } else {
        ctx.fillStyle = 'rgba(120,30,24,0.5)';
        ctx.fillRect(BX - 1, BY, 3, 2);
      }
      var washA = on ? 0.10 : 0.04;
      var grad = ctx.createRadialGradient(BX, BY, 4, BX, BY, 70);
      grad.addColorStop(0, 'rgba(255,80,60,' + washA + ')');
      grad.addColorStop(1, 'rgba(255,80,60,0)');
      ctx.fillStyle = grad;
      ctx.fillRect(BX - 70, BY - 70, 140, 140);
    }

    function amberRing(x, y, now) {                            // awaiting: the §5 amber state
      var pr = 15 + (reduced ? 0 : 3 * Math.sin(now / 260));
      var aA = reduced ? 0.75 : 0.5 + 0.4 * (0.5 + 0.5 * Math.sin(now / 260));
      ctx.fillStyle = 'rgba(242,179,61,' + aA.toFixed(2) + ')';
      for (var a2 = 0; a2 < 26; a2++) {
        var ang = a2 / 4.14;
        ctx.fillRect(Math.round(x + Math.cos(ang) * pr), Math.round(y + Math.sin(ang) * pr), 2, 2);
      }
      ctx.fillStyle = 'rgba(242,179,61,' + (aA * 0.45).toFixed(2) + ')';
      for (var a3 = 0; a3 < 26; a3 += 2) {
        var ang2 = a3 / 4.14;
        ctx.fillRect(Math.round(x + Math.cos(ang2) * (pr + 7)),
                     Math.round(y + Math.sin(ang2) * (pr + 7)), 1, 1);
      }
    }

    // Trouble treatment (§5 alarm salience): the field dims, the problem stays lit.
    function dimExcept(targets) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, W, H);
      targets.forEach(function (t) { ctx.moveTo(t.x + 52, t.y); ctx.arc(t.x, t.y, 52, 0, Math.PI * 2, true); });
      ctx.fillStyle = 'rgba(18,24,40,0.40)';
      ctx.fill('evenodd');
      targets.forEach(function (t) {
        ctx.beginPath(); ctx.arc(t.x, t.y, 52, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,235,170,0.35)'; ctx.lineWidth = 1; ctx.stroke();
      });
      ctx.restore();
    }

    function draw(now) {
      if (!reduced && !canvas.isConnected) {   // boring mode detached us — idle, don't paint
        raf = requestAnimationFrame(draw);
        return;
      }
      var dt = Math.min(0.1, (now - last) / 1000); last = now; frame++;

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(base, 0, 0);
      ctx.setTransform(S, 0, 0, S, 0, 0);

      var nums = Object.keys(sprites).map(Number).sort(function (a, b) { return a - b; });
      var spotlit = [];

      // motion + trails first (trails render under every hull)
      nums.forEach(function (n) {
        var s = sprites[n];
        step(s, now, dt);
        if (!reduced) spawnTrail(s, now);
      });

      // the holding pattern racetrack, under its plane
      var holdDrawn = false;
      nums.forEach(function (n) {
        var s = sprites[n];
        if (!s.target.orbit || holdDrawn) return;
        holdDrawn = true;
        var o = s.target.orbit;
        ctx.fillStyle = 'rgba(250,246,228,0.95)';
        for (var a2 = 0; a2 < 40; a2 += 2) {
          ctx.fillRect(Math.round(o.cx + Math.cos(a2 / 6.37) * o.rx),
                       Math.round(o.cy + Math.sin(a2 / 6.37) * o.ry), 2, 1);
        }
      });

      nums.forEach(function (n) { drawTrail(sprites[n], now); });

      // hulls — ground planes first (an air hull always overlaps a ground hull, never under it)
      var order = nums.slice().sort(function (a, b) {
        var A2 = sprites[a].target.air ? 1 : 0, B2 = sprites[b].target.air ? 1 : 0;
        return A2 - B2 || a - b;
      });
      order.forEach(function (n) {
        var s = sprites[n], f = s.flight;
        var inAir = s.target.air || (s.path && s.path.pts[0].y < 80);
        var box = spriteBox(s.dir, s.target.small);
        var px = Math.round(s.cur.x - box.w / 2), py = Math.round(s.cur.y - box.h / 2);
        var dim = f.stage === 'session-frozen' || f.stage === 'parked';
        var o = { air: inAir, dim: dim, chocks: f.stage === 'parked',
                  tint: (!dim && model.time !== 'day') ? model.time : null,
                  tail: f.tail };
        var drawFn = s.target.small ? A.live.planeSmall : A.live.plane;
        drawFn(ctx, px, py, s.dir, o);
        if (model.time !== 'day' && !dim && (reduced || frame % 22 < 13)) {
          A.live.navLights(ctx, px, py, s.dir, !s.target.small);
        }
        // touchdown dust — the landing you can see
        if (s.dustUntil > now) {
          var dal = (s.dustUntil - now) / 900;
          ctx.fillStyle = 'rgba(185,190,194,' + (0.55 * dal).toFixed(2) + ')';
          ctx.fillRect(px + box.w - 6, py + box.h - 6, 2, 2);
          ctx.fillRect(px + box.w - 2, py + box.h - 9, 1, 1);
          ctx.fillRect(px + box.w - 3, py + box.h - 3, 1, 1);
        }
        if (f.stage === 'awaiting') amberRing(s.cur.x, s.cur.y, now);
        // The spotlight list is the SERVER's (display.trouble = the repo's single worst
        // condition, §5) — a spinning flight that is not the worst keeps its warning tag but
        // never hijacks the wash (review fix 2026-07-07).
        if (model.dim && f.trouble) spotlit.push({ x: s.cur.x, y: s.cur.y });
      });

      // towed banner behind the featured leg flight (the drawn cloth; text is an HTML overlay)
      if (model.banner && sprites[model.banner.num]) {
        var bs = sprites[model.banner.num];
        if (!bs.path && placementOf(bs.flight) === 'downwind') {
          var bbox = spriteBox(bs.dir, bs.target.small);
          var bx = Math.round(bs.cur.x - bbox.w / 2);
          A.live.banner(ctx, bx, Math.round(bs.cur.y), bx - 84, Math.round(bs.cur.y) - 4, 74, 14);
        }
      }

      towerFX(now);

      if (model.dim || spotlit.length) {
        if (spotlit.length) dimExcept(spotlit);
        else { ctx.fillStyle = 'rgba(18,24,40,0.28)'; ctx.fillRect(0, 0, W, H); }
      }

      // runway lights twinkle after dark — both strips
      if (model.time !== 'day' && !reduced) {
        for (var k2 = 0; k2 < 3; k2++) {
          var s2 = Math.floor(now / 240) * 3 + k2;
          ctx.fillStyle = 'rgba(255,217,122,0.55)';
          ctx.fillRect(14 + ((s2 * 97) % 372), (s2 % 2) ? 80 : 116, 1, 1);
        }
      }

      if (!reduced) raf = requestAnimationFrame(draw);
    }

    function hitTest(lx, ly) {
      // Mirror the hull draw order (ground first, then air, ascending num) REVERSED, so the tap
      // always lands on the visually topmost plane (review fix 2026-07-07).
      var order = Object.keys(sprites).map(Number).sort(function (a, b) {
        var A2 = sprites[a].target.air ? 1 : 0, B2 = sprites[b].target.air ? 1 : 0;
        return A2 - B2 || a - b;
      });
      for (var i = order.length - 1; i >= 0; i--) {
        var s = sprites[order[i]];
        var box = spriteBox(s.dir, s.target.small);
        if (Math.abs(lx - s.cur.x) <= box.w / 2 + 4 && Math.abs(ly - s.cur.y) <= box.h / 2 + 4) {
          return order[i];
        }
      }
      return null;
    }

    rebuildBase();
    if (!reduced) raf = requestAnimationFrame(draw);

    return {
      update: update,
      hitTest: hitTest,
      destroy: function () { cancelAnimationFrame(raf); }
    };
  }

  window.AirfieldLive = { mount: mount };
})();
