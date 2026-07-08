/* Live airfield — circuit motion, contrail, banner, tower status effects.
   window.AirfieldLive.mount(canvas, bannerEl, clockEl, opts) */
(function () {
  'use strict';
  function mount(canvas, bannerEl, clockEl, opts) {
    const A = window.Airfield3;
    const W = 400, H = 270, S = 2;
    let time = (opts && opts.time) || 'day';
    let status = (opts && opts.status) || 'attention';
    let health = 'fresh';
    let mode = 'circuit';
    const HOLD = { cx: 300, cy: 34, rx: 26, ry: 16 };
    const SPIN = { cx: 200, cy: 44, rx: 18, ry: 12 };
    let th = -1.2;
    const tags = (opts && opts.tags) || null;
    const lmEls = (opts && opts.lmEls) || null;
    const LMS = [62, 150, 239, 330]; // Reconcile Pt, Build Island, Review Ridge, CI Shoals
    const base = document.createElement('canvas');
    function rebuild() { A.drawOverview(base, { time: time, status: status, noFlight: true, liveBeacon: true }); }
    rebuild();
    canvas.width = W * S; canvas.height = H * S;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = false;

    // circuit path: leg E → descent arc → landing roll W → climb arc, loop
    const segs = [
      { len: 272, air: true, pos: function (p) { return { x: 64 + 272 * p, y: 30, dir: 'E' }; } },
      { len: 82, air: true, pos: function (p) { const a = -Math.PI / 2 + p * Math.PI / 2; return { x: 336 + 52 * Math.cos(a), y: 82 + 52 * Math.sin(a), dir: p < 0.5 ? 'E' : 'S' }; } },
      { len: 368, air: false, roll: true, pos: function (p) { return { x: 388 - 368 * p, y: 82, dir: 'W' }; } },
      { len: 82, air: true, pos: function (p) { const a = Math.PI + p * Math.PI / 2; return { x: 64 + 52 * Math.cos(a), y: 82 + 52 * Math.sin(a), dir: p < 0.5 ? 'N' : 'E' }; } }
    ];
    const total = segs.reduce(function (s, x) { return s + x.len; }, 0);
    const ROLL_START = 272 + 82;

    function sample(u) {
      let d = u * total;
      for (let i = 0; i < segs.length; i++) {
        const s = segs[i];
        if (d <= s.len) { const r = s.pos(d / s.len); r.air = s.air; r.roll = !!s.roll; return r; }
        d -= s.len;
      }
      const r0 = segs[0].pos(0); r0.air = true; return r0;
    }

    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let u = 0.22, playing = !reduced, last = performance.now(), frame = 0, raf = 0;
    const trail = [];

    // tower beacon geometry (tower3 at 360,150 in drawOverview)
    const BX = 360, BY = 150;

    function towerFX(now) {
      if (status === 'ok') {
        const a = 0.12 + 0.06 * Math.sin(now / 900);
        ctx.fillStyle = 'rgba(110,224,138,' + a.toFixed(2) + ')';
        ctx.fillRect(BX - 7, BY - 7, 15, 15);
        ctx.fillStyle = '#6FE08A';
        ctx.fillRect(BX - 1, BY, 3, 2);
        return;
      }
      if (status === 'attention') {
        // clearly-alive breathing pulse
        const ph = 0.5 + 0.5 * Math.sin(now / 420);
        const a = 0.10 + 0.40 * ph;
        ctx.fillStyle = 'rgba(242,179,61,' + a.toFixed(2) + ')';
        ctx.fillRect(BX - 8, BY - 8, 17, 17);
        ctx.fillStyle = 'rgba(242,179,61,' + (a * 0.45).toFixed(2) + ')';
        const grow = Math.round(10 + 6 * ph);
        ctx.fillRect(BX - grow, BY - grow, grow * 2 + 1, grow * 2 + 1);
        ctx.fillStyle = ph > 0.7 ? '#FFE9B8' : '#F2B33D';
        ctx.fillRect(BX - 1, BY, 3, 2);
        return;
      }
      // alert: hard blink + expanding rings + red wash
      const on = (now % 700) < 380;
      const rp = ((now % 1400) / 1400); // ring phase
      // expanding double ring
      for (let k = 0; k < 2; k++) {
        const ph = (rp + k * 0.5) % 1;
        const rr = 6 + ph * 26;
        const al = 0.55 * (1 - ph);
        ctx.strokeStyle = 'rgba(255,90,72,' + al.toFixed(2) + ')';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(BX + 0.5, BY + 0.5, rr, 0, Math.PI * 2); ctx.stroke();
      }
      if (on) {
        ctx.fillStyle = 'rgba(255,90,72,0.34)';
        ctx.fillRect(BX - 10, BY - 10, 21, 21);
        ctx.fillStyle = '#FF5A48';
        ctx.fillRect(BX - 2, BY - 1, 5, 4);
        // light rays
        ctx.fillStyle = 'rgba(255,90,72,0.5)';
        ctx.fillRect(BX - 14, BY, 5, 1); ctx.fillRect(BX + 10, BY, 5, 1);
        ctx.fillRect(BX, BY - 14, 1, 5); ctx.fillRect(BX, BY + 10, 1, 5);
      } else {
        ctx.fillStyle = 'rgba(120,30,24,0.5)';
        ctx.fillRect(BX - 1, BY, 3, 2);
      }
      // soft red wash over the tower corner so it reads from across the room
      const washA = on ? 0.10 : 0.04;
      const grad = ctx.createRadialGradient(BX, BY, 4, BX, BY, 70);
      grad.addColorStop(0, 'rgba(255,80,60,' + washA + ')');
      grad.addColorStop(1, 'rgba(255,80,60,0)');
      ctx.fillStyle = grad;
      ctx.fillRect(BX - 70, BY - 70, 140, 140);
    }

    function draw(now) {
      const dt = Math.min(0.1, (now - last) / 1000); last = now; frame++;
      if (playing) {
        const cur = sample(u);
        const rollDist = u * total - ROLL_START;
        const speed = cur.roll ? Math.max(34, 115 - 0.24 * Math.max(0, rollDist)) : 46;
        u = (u + (speed * dt) / total) % 1;
      }
      let p;
      if (mode === 'circuit') {
        p = sample(u);
      } else {
        if (playing) th += dt * 1.15;
        const L = mode === 'spinning' ? SPIN : HOLD;
        const vx = -Math.sin(th) * L.rx, vy = Math.cos(th) * L.ry;
        p = {
          x: L.cx + Math.cos(th) * L.rx, y: L.cy + Math.sin(th) * L.ry,
          air: true, roll: false,
          dir: Math.abs(vx) > Math.abs(vy) ? (vx > 0 ? 'E' : 'W') : (vy > 0 ? 'S' : 'N')
        };
      }
      const horiz = p.dir === 'E' || p.dir === 'W';
      const sw = horiz ? 42 : 33, sh = horiz ? 33 : 42;
      const cx = Math.round(p.x - sw / 2), cy = Math.round(p.y - sh / 2);

      const healthEff = mode === 'spinning' ? 'fresh' : health;
      if (p.air && playing && healthEff !== 'frozen') {
        const gate = healthEff === 'fresh' ? true : (now % 1600) < 520;
        if (gate) {
          const lastT = trail[trail.length - 1];
          if (!lastT || Math.hypot(lastT.x - p.x, lastT.y - p.y) > 4) trail.push({ x: p.x, y: p.y, t: now });
        }
        while (trail.length > 90) trail.shift();
      }

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(base, 0, 0);
      ctx.setTransform(S, 0, 0, S, 0, 0);

      // holding / spinning loop drawn under the flight
      if (mode !== 'circuit') {
        const L = mode === 'spinning' ? SPIN : HOLD;
        ctx.fillStyle = 'rgba(250,246,228,0.95)';
        for (let a2 = 0; a2 < 40; a2 += 2) {
          ctx.fillRect(Math.round(L.cx + Math.cos(a2 / 6.37) * L.rx), Math.round(L.cy + Math.sin(a2 / 6.37) * L.ry), 2, 1);
        }
      }

      // contrail — fresh: bold & bright · idle: sputtering bursts that flicker · frozen: none
      const maxAge = healthEff === 'idle' ? 4.2 : 8;
      for (let i = 0; i < trail.length; i++) {
        const age = (now - trail[i].t) / 1000;
        if (age > maxAge) continue;
        let al = Math.max(0, 1.0 - age * (healthEff === 'idle' ? 0.22 : 0.12));
        if (healthEff === 'idle') al *= 0.75 + 0.25 * Math.sin(now / 90 + i * 1.7);
        ctx.fillStyle = 'rgba(255,255,255,' + Math.max(0, al).toFixed(2) + ')';
        const b = al > 0.62 ? 3 : al > 0.3 ? 2 : 1;
        ctx.fillRect(Math.round(trail[i].x) - 1, Math.round(trail[i].y) - 1, b, b);
      }
      // touchdown dust
      const rollDist2 = u * total - ROLL_START;
      if (p.roll && rollDist2 > 2 && rollDist2 < 30) {
        ctx.fillStyle = 'rgba(185,190,194,0.55)';
        ctx.fillRect(cx + 24, cy + 24, 2, 2); ctx.fillRect(cx + 28, cy + 21, 1, 1); ctx.fillRect(cx + 27, cy + 27, 1, 1);
      }
      // the flight
      A.live.plane(ctx, cx, cy, p.dir, healthEff === 'frozen' ? { air: p.air, dim: true } : { air: p.air, tint: time === 'day' ? null : time });
      if (time !== 'day' && healthEff !== 'frozen' && frame % 22 < 13) A.live.navLights(ctx, cx, cy, p.dir, true);

      // towed banner while on the leg
      const onLeg = mode === 'circuit' && p.air && p.dir === 'E' && p.y < 40 && p.x > 152 && p.x < 356;
      if (onLeg) {
        A.live.banner(ctx, cx, Math.round(p.y), cx - 84, Math.round(p.y) - 4, 74, 14);
        if (bannerEl) {
          bannerEl.style.opacity = '1';
          bannerEl.style.left = ((cx - 84) * S) + 'px';
          bannerEl.style.top = ((Math.round(p.y) - 4) * S) + 'px';
        }
      } else if (bannerEl) { bannerEl.style.opacity = '0'; }

      // landmark flag for whatever the flight is currently passing
      if (lmEls) {
        for (let i = 0; i < LMS.length; i++) {
          if (!lmEls[i]) continue;
          const on = mode === 'circuit' && p.air && p.dir === 'E' && p.y < 40 && Math.abs(p.x - LMS[i]) < 36;
          lmEls[i].style.opacity = on ? '1' : '0';
        }
      }

      towerFX(now);

      // flight-state FX
      if (mode === 'awaiting') {
        const pr = 15 + 3 * Math.sin(now / 260);
        const aA = 0.5 + 0.4 * (0.5 + 0.5 * Math.sin(now / 260));
        ctx.fillStyle = 'rgba(242,179,61,' + aA.toFixed(2) + ')';
        for (let a2 = 0; a2 < 26; a2++) {
          const ang = a2 / 4.14;
          ctx.fillRect(Math.round(p.x + Math.cos(ang) * pr), Math.round(p.y + Math.sin(ang) * pr), 2, 2);
        }
        ctx.fillStyle = 'rgba(242,179,61,' + (aA * 0.45).toFixed(2) + ')';
        for (let a2 = 0; a2 < 26; a2 += 2) {
          const ang = a2 / 4.14;
          ctx.fillRect(Math.round(p.x + Math.cos(ang) * (pr + 7)), Math.round(p.y + Math.sin(ang) * (pr + 7)), 1, 1);
        }
      }
      if (mode === 'spinning') {
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, W, H);
        ctx.arc(SPIN.cx, SPIN.cy, 52, 0, Math.PI * 2, true);
        ctx.fillStyle = 'rgba(18,24,40,0.40)';
        ctx.fill('evenodd');
        ctx.beginPath(); ctx.arc(SPIN.cx, SPIN.cy, 52, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(255,235,170,0.35)'; ctx.lineWidth = 1; ctx.stroke();
        ctx.restore();
      }
      if (tags) {
        if (tags.hold) tags.hold.style.opacity = mode === 'holding' ? '1' : '0';
        if (tags.awaitT) tags.awaitT.style.opacity = mode === 'awaiting' ? '1' : '0';
        if (tags.spin) tags.spin.style.opacity = mode === 'spinning' ? '1' : '0';
      }
      // runway lights twinkle after dark
      if (time !== 'day') {
        for (let k2 = 0; k2 < 3; k2++) {
          const s2 = Math.floor(now / 240) * 3 + k2;
          ctx.fillStyle = 'rgba(255,217,122,0.55)';
          ctx.fillRect(14 + ((s2 * 97) % 372), (s2 % 2) ? 80 : 116, 1, 1);
        }
      }

      if (clockEl) {
        const mins = 48 + Math.floor(u * 11);
        clockEl.textContent = '18:' + String(mins).padStart(2, '0');
      }
      raf = requestAnimationFrame(draw);
    }
    // paint the first frame synchronously (rAF may be throttled in hidden iframes)
    function kick() { cancelAnimationFrame(raf); draw(performance.now()); }
    kick();

    return {
      toggle: function () { playing = !playing; kick(); return playing; },
      setU: function (v) { playing = false; u = Math.min(0.999, Math.max(0, v)); trail.length = 0; kick(); },
      setTime: function (t2) { if (t2 !== time) { time = t2; rebuild(); kick(); } },
      setStatus: function (s2) { if (s2 !== status) { status = s2; rebuild(); kick(); } },
      setHealth: function (h2) { if (h2 !== health) { health = h2; kick(); } },
      setMode: function (m2) { if (m2 !== mode) { mode = m2; trail.length = 0; th = -1.2; kick(); } },
      destroy: function () { cancelAnimationFrame(raf); }
    };
  }
  window.AirfieldLive = { mount: mount };
})();
