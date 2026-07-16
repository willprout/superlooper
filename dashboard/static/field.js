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
      '<span class="s">showing local flights only · the queue is dark until the link returns</span>';
    overlays.appendChild(link);
    fixedEls.link = link;

    // STATE-FORMAT MISMATCH (issue #45): the runner stamped a state-home format this build of the
    // dashboard doesn't read, so the fields below may be misread or blank. Without this card the
    // fail-closed readers would render an all-quiet field and hide WHY — the silent "why is my
    // dashboard empty." A compact card NAMES the mismatch (the version line is the server's, bound
    // into `.m`; the title/remedy are static). Distinct from the dark data-link: this is "I can't
    // read the shape on disk," not "I can't reach GitHub." It self-heals when the dashboard is
    // updated (or an old-format home reappears): the next poll rebuilds without it.
    var fmt = document.createElement('div');
    fmt.className = 'fld-fmt';
    fmt.hidden = true;
    fmt.innerHTML = '<span class="t">◇ STATE FORMAT MISMATCH</span>' +
      '<span class="m"></span>' +
      '<span class="s">some readings may be blank until command-center is updated</span>';
    overlays.appendChild(fmt);
    fixedEls.fmt = fmt;

    // SOURCE MODE (issue #146). The field normally renders the RUNNER's own published view. When the
    // runner goes quiet the dashboard polls GitHub itself — a second opinion, on a stale premise —
    // and that must never look like the real thing: the owner read this surface as a live mirror of
    // the runner for weeks while it quietly wasn't one. So fallback SHOUTS, in a band the eye can't
    // file as chrome, naming both facts (since when the runner went silent; that this is GitHub
    // directly). The server composes the words (design B.1); this only binds them. Self-clearing:
    // the poll after the runner returns rebuilds without it, no restart, nothing to dismiss.
    var srcBanner = document.createElement('div');
    srcBanner.className = 'fld-src';
    srcBanner.hidden = true;
    overlays.appendChild(srcBanner);
    fixedEls.src = srcBanner;

    // THE STANDING TRUTH STRIP (issue #166). It absorbs #146's always-on freshness stamp — same
    // corner, same quiet register when all is well — but it now states the CONCLUSION rather than
    // only the numbers ("loop may be down", not merely "last tick 15m ago", which makes the owner
    // know the threshold to read it), and it carries a third fact those clocks can't see: the
    // engine's publish drift, the merged fixes the runner is not running yet.
    //
    // Always mounted, never gated on trouble. It is the honesty that makes the whole surface
    // readable, not a warning — the owner read this field for weeks as a live mirror of the runner
    // while it quietly wasn't one, and a strip that only appears when someone already knows to look
    // for it would rebuild that exact bug.
    //
    // Design B.1: every word here is the server's (lib/truth.py, unit-tested). This file picks no
    // threshold, formats no age, and decides no state — it binds three strings and a level class.
    var strip = document.createElement('div');
    strip.className = 'fld-truth';
    overlays.appendChild(strip);
    fixedEls.truth = strip;

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

    // State-format-mismatch card (issue #45) — shown when the runner stamped a format this dashboard
    // doesn't read (repo.state_format.compatible === false; a pre-handshake home is compatible, so it
    // stays hidden). The server built the version-naming line; we only bind it. Like the link card,
    // runner-down's CSS takeover wins when both are true — but in the real mismatch case the runner
    // is UP (a new engine stamping a new shape), so this card shows exactly when it matters.
    var sf = c.repo.state_format;
    var mismatch = !!(sf && sf.compatible === false);
    fixedEls.fmt.hidden = !mismatch;
    if (mismatch) fixedEls.fmt.querySelector('.m').textContent = sf.message || '';

    bindSource(c.repo.source);
    bindTruth(c.repo.truth);
  }

  /* Bind the fallback banner (issue #146). It reads the server's verdict (repo.source) and derives
     nothing — which mode we're in, and the words for it, are decided once in lib/flights.source_mode
     so the banner and the board can never tell two different stories.

     The always-on freshness stamp this used to bind moved into bindTruth (issue #166), which states
     the same two clocks plus the conclusion; what stays here is the loud fallback band alone. */
  function bindSource(src) {
    src = src || {};
    var fallback = src.mode === 'fallback';

    // The banner: fallback only. LIVE stays quiet, or the shout becomes wallpaper and the fallback
    // is invisible again — the very failure this issue closes.
    fixedEls.src.hidden = !fallback;
    if (fallback) {
      var lines = (src.banner && src.banner.lines) || [];
      fixedEls.src.innerHTML = '<span class="t">◆ FALLBACK — GITHUB DIRECT</span>' +
        lines.map(function (l) { return '<span class="m">' + esc(l) + '</span>'; }).join('');
    }
  }

  /* Bind the standing truth strip (issue #166): how long since the runner ticked, whose truth is on
     screen, and whether the engine running the loop is the one that was merged.

     ALWAYS rendered — in every mode, healthy or not. That is deliberate and is the whole point: a
     surface that only tells you it might be lying once you suspect it isn't a surface you can trust
     the rest of the time.

     This derives NOTHING. `level`, each `state`, and every `text` are composed server-side in
     lib/truth.py (which in turn reads flights.source_mode's and engine.drift's verdicts, never a
     second opinion of its own). A missing strip falls back to the DOWN state rather than a blank:
     an absent verdict is not an all-clear, and rendering nothing is how this surface lied before. */
  function bindTruth(t) {
    t = t || {};
    var tick = t.tick || {}, data = t.data || {}, eng = t.engine;
    // The level class colours the whole strip, so one glance at it summarises everything under it.
    fixedEls.truth.className = 'fld-truth lvl-' + esc(t.level || 'down');
    var rows = '<span class="r ' + esc(tick.state || 'down') + '">' +
      esc(tick.text || 'no tick seen — loop may be down') + '</span>' +
      '<span class="r ' + esc(data.state || 'blind') + '">' + esc(data.text || 'data ?') + '</span>';
    // The engine line appears ONLY when there is something to say — a live engine is silent (§0.2:
    // a strip that congratulates itself every two seconds is one the owner stops reading, and then
    // the one time it matters he won't see it).
    if (eng && eng.text) {
      rows += '<span class="r eng ' + esc(eng.state || '') + '">' + esc(eng.text) + '</span>';
    }
    fixedEls.truth.innerHTML = rows;
  }

  window.CCField = { attach: attach };
})();
