/* Multi-repo airport — terminals on a skybridge ring, camera swings between them.
   window.MultiView.mount(canvas, chipsEl) */
(function () {
  'use strict';
  function mount(canvas, chipsEl) {
    const world = document.createElement('canvas');
    window.Airfield3.drawMultiWorld(world); // 1200x680 device
    canvas.width = 1200; canvas.height = 680;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = false;

    const CX = 300, CY = 160, RX = 135, RY = 95;
    // polar views around the ring: overview → superlooper (bottom) → dashboard (NE) → reserved (NW)
    const VIEWS = [
      { a: Math.PI / 2, k: 0, s: 1 },
      { a: Math.PI / 2, k: 0.95, s: 1.9 },
      { a: -0.85, k: 1.12, s: 1.9 },
      { a: Math.PI + 0.78, k: 0.96, s: 1.9 }
    ];
    let cur = { a: VIEWS[0].a, k: 0, s: 1 };
    let from = null, to = null, t0 = 0, idx = 0, raf = 0;

    function ease(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }
    function render() {
      const s = cur.s, w = 600 / s, h = 340 / s;
      let cx = CX + Math.cos(cur.a) * RX * cur.k;
      let cy = CY + Math.sin(cur.a) * RY * cur.k;
      cx = Math.max(w / 2, Math.min(600 - w / 2, cx));
      cy = Math.max(h / 2, Math.min(340 - h / 2, cy));
      ctx.drawImage(world, (cx - w / 2) * 2, (cy - h / 2) * 2, w * 2, h * 2, 0, 0, 1200, 680);
    }
    function settle() {
      cur = { a: to.a, k: to.k, s: to.s };
      to = null;
      render();
      if (chipsEl) chipsEl.style.opacity = idx === 0 ? '1' : '0';
    }
    function tick(now) {
      if (!to) return;
      const t = Math.min(1, (now - t0) / 700);
      const e = ease(t);
      // rotate around the ring: shortest angular path
      let da = to.a - from.a;
      da = ((da + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI;
      cur = { a: from.a + da * e, k: from.k + (to.k - from.k) * e, s: from.s + (to.s - from.s) * e };
      render();
      if (t >= 1) settle();
      else raf = requestAnimationFrame(tick);
    }
    function go(i) {
      idx = ((i % VIEWS.length) + VIEWS.length) % VIEWS.length;
      from = { a: cur.a, k: cur.k, s: cur.s };
      to = VIEWS[idx];
      t0 = performance.now();
      if (chipsEl) chipsEl.style.opacity = '0';
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
      setTimeout(function () { if (to === VIEWS[idx]) settle(); }, 850);
    }
    render();
    if (chipsEl) chipsEl.style.opacity = '1';
    return {
      next: function () { go(idx + 1); },
      prev: function () { go(idx - 1); },
      overview: function () { go(0); }
    };
  }
  window.MultiView = { mount: mount };
})();
