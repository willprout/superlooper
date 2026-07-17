/* The night replay (Task 11 / design record §4) — a beloved TREAT, never load-bearing.
 *
 * window.CCReplay is a persistent overlay OUTSIDE #root (like the drawer/flag box) so the 2s poll
 * never touches it. It fetches server-derived FRAMES (/api/replay — every semantic pre-computed in
 * lib/replay: stage, contrail, lighting, the glossed sentence) and plays them back over the SAME
 * airfield engine the live field uses (window.AirfieldLive), scrubbable and steppable. A frame only
 * advances a plane when a real journal event changed its stage — the transit you watch IS the event.
 *
 * Design B.1: this file computes NO semantics. It maps the server's frame flights onto the engine's
 * model, drives scrub/step/play, and forwards a plane tap (or the caption) as `cc:drawer-open` so
 * every frame is clickable through to its event. It never derives a stage, a time, or a sentence. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  var node = null, canvas = null, engine = null;
  var frames = [], idx = 0, playing = false, timer = 0;
  var slug = "", fun = null;
  var STEP_MS = 900;                 // one frame per ~0.9s at play — a watchable time-lapse

  function el(id) { return document.getElementById(id); }

  function ensure() {
    if (node) return;
    node = document.createElement("div");
    node.id = "cc-replay";
    node.className = "cc-replay";
    node.innerHTML =
      '<div class="cc-replay-card">' +
        '<div class="cc-replay-head">' +
          '<span class="cc-replay-title">⏮ NIGHT REPLAY <b id="cc-replay-name"></b></span>' +
          '<span class="cc-replay-sub">a treat — not the answer to “what happened” (that’s the digest)</span>' +
          '<span class="cc-replay-trunc" id="cc-replay-trunc" hidden></span>' +
          '<select id="cc-replay-range" title="how far back to replay">' +
            '<option value="21600">last 6h</option>' +
            '<option value="43200">last 12h</option>' +
            '<option value="86400" selected>last 24h</option>' +
            '<option value="all">all history</option>' +
          '</select>' +
          '<button class="cc-replay-x" data-replay-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-replay-stage"><canvas class="cc-replay-canvas"></canvas>' +
          '<div class="cc-replay-empty" id="cc-replay-empty" hidden>no journal events in this window</div>' +
        '</div>' +
        '<div class="cc-replay-caption" id="cc-replay-caption"></div>' +
        '<div class="cc-replay-controls">' +
          '<button class="cc-replay-btn" data-replay-step="-1" title="step back">◀</button>' +
          '<button class="cc-replay-btn play" id="cc-replay-play" title="play / pause">▶</button>' +
          '<button class="cc-replay-btn" data-replay-step="1" title="step forward">▶</button>' +
          '<input type="range" class="cc-replay-scrub" id="cc-replay-scrub" min="0" max="0" value="0">' +
          '<span class="cc-replay-pos" id="cc-replay-pos">0 / 0</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(node);
    canvas = node.querySelector(".cc-replay-canvas");

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-replay-close]")) { close(); return; }
      var step = t.closest("[data-replay-step]");
      if (step) { stepBy(Number(step.getAttribute("data-replay-step"))); return; }
      if (t.closest("#cc-replay-play")) { togglePlay(); return; }
      var cap = t.closest("[data-replay-num]");
      if (cap) { openEvent(Number(cap.getAttribute("data-replay-num"))); return; }
    });
    el("cc-replay-scrub").addEventListener("input", function () {
      pause(); idx = Number(this.value) || 0; renderFrame();
    });
    el("cc-replay-range").addEventListener("change", function () { load(this.value); });
    document.addEventListener("keydown", function (e) {
      if (!isOpen()) return;
      if (e.key === "Escape") { close(); return; }
      if (e.key === "ArrowLeft") { stepBy(-1); e.preventDefault(); }
      else if (e.key === "ArrowRight") { stepBy(1); e.preventDefault(); }
      else if (e.key === " ") { togglePlay(); e.preventDefault(); }
    });

    // A tapped plane in the replay opens that flight's drawer — the same event the live field fires,
    // so replay is clickable through to ground truth exactly like the airfield (tap-where-you-read).
    canvas.addEventListener("click", function (e) {
      if (!engine) return;
      var p = logical(e), num = engine.hitTest(p.x, p.y);
      if (num != null) openEvent(num);
    });
    canvas.addEventListener("mousemove", function (e) {
      if (!engine) return;
      var p = logical(e);
      canvas.style.cursor = engine.hitTest(p.x, p.y) != null ? "pointer" : "default";
    });
  }

  function logical(e) {
    var r = canvas.getBoundingClientRect();
    return { x: (e.clientX - r.left) * (400 / r.width), y: (e.clientY - r.top) * (270 / r.height) };
  }

  function isOpen() { return !!(node && node.classList.contains("open")); }

  function open(repoSlug, funMap) {
    ensure();
    slug = repoSlug || "";
    fun = funMap || null;
    el("cc-replay-name").textContent = slug;
    node.classList.add("open");
    if (!engine && window.AirfieldLive) engine = window.AirfieldLive.mount(canvas);
    load(el("cc-replay-range").value);
  }

  function load(range) {
    pause();
    el("cc-replay-caption").innerHTML = '<span class="loading">reading the journal…</span>';
    var url = "/api/replay?repo=" + encodeURIComponent(slug) + "&range=" + encodeURIComponent(range);
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        // Read the body either way, but keep r.ok — a typed 500 (a bad journal line) must NOT render
        // as an empty replay; it shows the error honestly.
        return r.json().then(function (b) { return { ok: r.ok, body: b }; },
                             function () { return { ok: r.ok, body: null }; });
      })
      .then(function (res) {
        if (!res.ok || !res.body || res.body.error) {
          showError((res.body && res.body.error) || "replay unavailable");
          return;
        }
        setFrames(res.body);
      })
      .catch(function () { showError("couldn’t reach the command center"); });
  }

  function showError(msg) {
    frames = [];
    var empty = el("cc-replay-empty");
    empty.hidden = false;
    empty.textContent = msg;
    el("cc-replay-caption").innerHTML = '<span class="err">' + esc(msg) + '</span>';
    el("cc-replay-pos").textContent = "0 / 0";
    el("cc-replay-trunc").hidden = true;
    var scrub = el("cc-replay-scrub"); scrub.max = 0; scrub.value = 0;
    if (engine) engine.update({ resetKey: "replay-error", time: "day", status: "ok",
                                dim: false, banners: [], flights: [] });
  }

  function setFrames(rp) {
    frames = (rp && rp.frames) || [];
    var scrub = el("cc-replay-scrub"), empty = el("cc-replay-empty");
    empty.hidden = frames.length > 0;
    empty.textContent = "no journal events in this window";
    var trunc = el("cc-replay-trunc");
    if (rp.window && rp.window.truncated) {
      trunc.hidden = false;
      trunc.textContent = "· capped to the most recent " + frames.length + " frames";
    } else { trunc.hidden = true; }
    scrub.max = Math.max(0, frames.length - 1);
    idx = 0;
    if (!frames.length) {
      if (engine) engine.update({ resetKey: "replay-empty", time: "day", status: "ok", dim: false,
                                  banners: [], flights: [] });
      el("cc-replay-caption").innerHTML = "";
      el("cc-replay-pos").textContent = "0 / 0";
      return;
    }
    renderFrame();
  }

  // The engine model for a frame — the SAME shape field.js builds for the live field, mapping the
  // server's pre-derived flight fields onto the engine's keys (design B.1: no semantics here).
  function model(i) {
    var fr = frames[i];
    var airlines = !fun || fun.airlines !== false;
    var clock = !fun || fun.living_clock !== false;
    return {
      resetKey: "replay",
      time: clock ? (fr.daypart || "day") : "day",
      status: fr.status || "ok",
      dim: false,
      banners: [],       // replay scrubs recorded field states; it has never towed the name cloths

      flights: (fr.flights || []).map(function (f) {
        return { num: f.num, label: f.label, stage: f.stage, circuitStage: f.circuit_stage,
                 runway: f.runway || 0, contrail: f.contrail || "none",
                 spinning: !!f.spinning, trouble: !!f.trouble, tail: airlines ? f.tail : null };
      })
    };
  }

  function renderFrame() {
    if (!frames.length) return;
    idx = Math.max(0, Math.min(idx, frames.length - 1));
    if (engine) engine.update(model(idx));
    var fr = frames[idx];
    var radio = fr.radio ? '<span class="radio">' + esc(fr.radio) + '</span> ' : "";
    var chip = fr.num != null
      ? '<span class="fnum" data-replay-num="' + esc(fr.num) + '">SL-' + esc(fr.num) + '</span> ' : "";
    el("cc-replay-caption").innerHTML =
      '<span class="t">' + esc(fr.hhmm) + '</span>' + chip + radio +
      '<span class="msg">' + esc(fr.text) + '</span>' +
      (fr.num != null ? '<span class="hint">tap to open the flight →</span>' : "");
    el("cc-replay-scrub").value = idx;
    el("cc-replay-pos").textContent = (idx + 1) + " / " + frames.length;
  }

  function stepBy(d) { pause(); idx += d; renderFrame(); }

  function togglePlay() { if (playing) pause(); else play(); }

  function play() {
    if (!frames.length) return;
    if (idx >= frames.length - 1) idx = 0;         // replay from the top if parked at the end
    playing = true;
    el("cc-replay-play").textContent = "❚❚";
    window.clearInterval(timer);
    timer = window.setInterval(function () {
      if (idx >= frames.length - 1) { renderFrame(); pause(); return; }
      idx += 1; renderFrame();
    }, STEP_MS);
    renderFrame();
  }

  function pause() {
    playing = false;
    window.clearInterval(timer);
    var b = el("cc-replay-play");
    if (b) b.textContent = "▶";
  }

  function openEvent(num) {
    if (num == null) return;
    document.dispatchEvent(new CustomEvent("cc:drawer-open", { detail: { repo: slug, num: num } }));
  }

  function close() {
    pause();
    if (engine) { engine.destroy(); engine = null; }   // stop the rAF loop while the treat is put away
    if (node) node.classList.remove("open");
  }

  window.CCReplay = { open: open, isOpen: isOpen };
})();
