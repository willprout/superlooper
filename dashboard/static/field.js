/* The airfield binder (Task 7) — connects /api/snapshot to the N-flight engine.
   Design B.1 discipline: every semantic (stage, contrail kind, runway, trouble, captions, the
   incident number) arrives pre-derived from the server; this file only maps values onto the
   engine's model, positions the HTML overlay tags the engine lays out, and forwards plane taps
   as a `cc:drawer-open` event (the drawer itself is a later flight).

   The root node (canvas + overlays) is built ONCE and re-parented into the shell's mount on
   every poll re-render — the canvas, its rAF loop, and the sprite state all survive the shell's
   innerHTML rebuilds. */
(function () {
  'use strict';

  var root = null, canvas = null, overlays = null, tagBox = null, engine = null;
  var lmEls = [], fixedEls = {};
  var lastLayout = null, lastCtx = null;
  var slug = "";

  var LM_LABELS = ['▸ RECONCILE PT', '▸ BUILD ISLAND', '▸ REVIEW RIDGE', '▸ CI SHOALS'];
  var LM_POS = [{ x: 62, y: 64 }, { x: 150, y: 64 }, { x: 239, y: 64 }, { x: 330, y: 64 }];
  var SIGN = { x: 334, y: 247, w: 56, h: 15 };   // the painted incident sign (airfield3 geometry)

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function ensure() {
    if (root) return;
    root = document.createElement('div');
    root.className = 'fld-root';
    canvas = document.createElement('canvas');
    canvas.className = 'fld-canvas';
    root.appendChild(canvas);

    overlays = document.createElement('div');
    overlays.className = 'fld-overlays';
    root.appendChild(overlays);

    LM_LABELS.forEach(function (t) {
      var el = document.createElement('span');
      el.className = 'fld-lm';
      el.textContent = t;
      overlays.appendChild(el);
      lmEls.push(el);
    });
    tagBox = document.createElement('div');
    tagBox.className = 'fld-tags';
    overlays.appendChild(tagBox);

    ['banner', 'caption', 'sign', 'freeze'].forEach(function (k) {
      var el = document.createElement('span');
      el.className = 'fld-' + k;
      el.hidden = true;
      overlays.appendChild(el);
      fixedEls[k] = el;
    });
    fixedEls.freeze.textContent = 'LANDINGS PAUSED — REPAIR FLIGHT DISPATCHED';
    // RUNNER DOWN (screen 8d): the full-surface grey lives on the root class; this is the card.
    var rd = document.createElement('div');
    rd.className = 'fld-rd';
    rd.hidden = true;
    rd.innerHTML = '<div class="fld-rd-card"><span class="t">RUNNER DOWN</span>' +
      '<span class="m" id="fld-rd-msg"></span>' +
      '<span class="s">the dashboard backend watches the runner, not the other way around</span></div>';
    overlays.appendChild(rd);
    fixedEls.rd = rd;

    // GITHUB UNREACHABLE (issue #38): the tower has lost its data link to GitHub, so the queue /
    // arrivals / titles are blind. Deliberately NOT the full-surface red runner-down takeover — the
    // LOCAL flights (from the state home) are still real and stay visible; only the GitHub-derived
    // layer is dark. A compact, plain-words card names it while the canvas tower beacon goes dark and
    // sweeps for a signal. It self-heals: the next reachable poll rebuilds without it.
    var link = document.createElement('div');
    link.className = 'fld-link';
    link.hidden = true;
    link.innerHTML = '<span class="t">◈ NO DATA LINK</span>' +
      '<span class="m">can’t reach GitHub — the tower is searching</span>' +
      '<span class="s">showing local state only · the boards are dark until the link returns</span>';
    overlays.appendChild(link);
    fixedEls.link = link;

    engine = window.AirfieldLive.mount(canvas);
    canvas.addEventListener('click', function (e) {
      var p = logical(e), num = engine.hitTest(p.x, p.y);
      if (num == null) return;
      document.dispatchEvent(new CustomEvent('cc:drawer-open', { detail: { repo: slug, num: num } }));
    });
    canvas.addEventListener('mousemove', function (e) {
      var p = logical(e);
      canvas.style.cursor = engine.hitTest(p.x, p.y) != null ? 'pointer' : 'default';
    });
    window.addEventListener('resize', function () {
      if (lastLayout && lastCtx) placeOverlays(lastLayout, lastCtx);
    });
  }

  function logical(e) {
    var r = canvas.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (400 / r.width), y: (e.clientY - r.top) * (270 / r.height) };
  }

  function attach(mountEl, snapshot, repoIndex) {
    if (!mountEl || !snapshot || !window.AirfieldLive || !window.Airfield3) return;
    ensure();
    if (root.parentNode !== mountEl) mountEl.appendChild(root);
    update(snapshot, repoIndex);
  }

  function update(snapshot, repoIndex) {
    var repos = snapshot.repos || [];
    if (!repos.length) return;
    var repo = repos[Math.min(repoIndex || 0, repos.length - 1)];
    slug = repo.slug;
    var fun = snapshot.fun || {};
    var tail = (fun.airlines !== false && repo.colors) ? repo.colors.tail : null;

    var onField = (repo.flights || []).filter(function (f) {
      return f.display && f.display.on_field;
    });
    var flights = onField.map(function (f) {
      return { num: f.num, label: f.label, stage: f.stage, circuitStage: f.circuit_stage,
               runway: f.display.runway || 0, contrail: f.contrail || 'none',
               spinning: !!f.spinning, trouble: !!f.display.trouble, tail: tail };
    });

    // Approved-but-not-launched flights (the departures queue) wait as planes at the west gates —
    // the design's "at the stand (approved, queued)" stage (§3, issue #32). They arrive pre-derived
    // in repo.stand (the launchable front of the queue, one per gate); this file only carries each to
    // a plane. A queued flight has no session yet, so it never trails a contrail and is never lit —
    // a healthy plane WAITING, visually apart from the parked "gave up" plane (chocks, dimmed). The
    // drawer already opens a minimal card for a tap on a still-queued flight (shell.js).
    var flying = {};
    flights.forEach(function (f) { flying[f.num] = true; });
    var standFlights = (repo.stand || []).filter(function (s) {
      return !flying[s.num];                 // never double-draw a flight already in the air
    }).map(function (s) {
      return { num: s.num, label: s.flight, stage: 'at-stand', circuitStage: 'at-stand',
               runway: 0, contrail: 'none', spinning: false, trouble: false, tail: tail };
    });

    // The towed banner's flight and text are CHOSEN server-side (repo.field_banner, squint
    // test) — this file only carries them to the cloth.
    var banner = repo.field_banner || null;

    // GitHub-unreachable (issue #38): the server's honest flag → the engine's `link` state. When the
    // link is lost the tower beacon goes dark and sweeps for a signal (a dark tower, never a red
    // alarm — this is "can't see the boards," not "the runner died"). Local flights stay lit; the
    // classification is the server's, this only forwards the flag (design record B.1).
    var linkLost = !!(repo.github && repo.github.unreachable);

    var anyLit = flights.some(function (f) { return f.trouble; });
    var layout = engine.update({
      resetKey: repo.slug,
      time: fun.living_clock === false ? 'day' : (snapshot.daypart || 'day'),
      status: snapshot.tower_status || 'attention',
      dim: anyLit || (repo.state && repo.state.state === 'alert'),
      link: linkLost ? 'lost' : 'ok',
      banner: banner,
      flights: flights.concat(standFlights)
    });

    var ctxState = { snapshot: snapshot, repo: repo, banner: banner, fun: fun };
    lastLayout = layout; lastCtx = ctxState;
    placeOverlays(layout, ctxState);
  }

  function placeOverlays(layout, c) {
    var f = canvas.clientWidth / 400;   // logical px -> CSS px (aspect fixed, one factor)
    function pos(el, x, y) { el.style.left = (x * f) + 'px'; el.style.top = (y * f) + 'px'; }

    lmEls.forEach(function (el, i) {
      pos(el, LM_POS[i].x, LM_POS[i].y);
      el.style.opacity = layout.landmarks[i] ? '1' : '0';
    });

    tagBox.innerHTML = layout.tags.map(function (t) {
      // clamp toward the field so a tag on an edge anchor stays readable, never clipped
      var tx = Math.max(78, Math.min(322, t.x));
      var ty = Math.max(16, Math.min(254, t.y));
      return '<span class="fld-tag ' + esc(t.kind) + '" style="left:' + (tx * f) +
             'px;top:' + (ty * f) + 'px">' + esc(t.text) + '</span>';
    }).join('');

    var b = fixedEls.banner;
    b.hidden = !(layout.banner && c.banner);
    if (!b.hidden) {
      pos(b, layout.banner.x, layout.banner.y);
      b.style.width = (layout.banner.w * f) + 'px';
      b.style.height = (layout.banner.h * f) + 'px';
      b.textContent = c.banner.text;
    }

    var cap = fixedEls.caption;
    cap.hidden = !c.repo.field_caption;
    if (!cap.hidden) cap.textContent = c.repo.field_caption;

    var sign = fixedEls.sign;
    sign.hidden = c.fun.incident_sign === false;
    if (!sign.hidden) {
      pos(sign, SIGN.x, SIGN.y);
      sign.style.width = (SIGN.w * f) + 'px';
      sign.style.height = (SIGN.h * f) + 'px';
      var n = (c.repo.incident && c.repo.incident.landings_since_incident) || 0;
      sign.textContent = n + ' LANDING' + (n === 1 ? '' : 'S') + ' SINCE THE LAST INCIDENT';
    }

    // Freeze reads CALM by design (§3): landings paused, flying continues — a tag, never a crash.
    var fz = fixedEls.freeze;
    fz.hidden = !c.repo.merges_frozen;
    if (!fz.hidden) pos(fz, 200, 70);

    var down = !!(c.snapshot.runner && c.snapshot.runner.down);
    root.classList.toggle('down', down);
    fixedEls.rd.hidden = !down;
    if (down) {
      var msg = document.getElementById('fld-rd-msg');
      if (msg) msg.textContent = (c.snapshot.runner && c.snapshot.runner.message) || '';
    }

    // GitHub-unreachable card (issue #38) — shown when the data link is lost. Runner-down is more
    // severe (it grays the whole surface and hides every other overlay via CSS), so it wins when both
    // are true; here we just bind the flag. The tower beacon's dark-sweep is drawn on the canvas.
    fixedEls.link.hidden = !(c.repo.github && c.repo.github.unreachable);
  }

  window.CCField = { attach: attach };
})();
