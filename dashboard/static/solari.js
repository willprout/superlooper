/* Solari split-flap arrivals board — the flagship delight moment (design record §0.8, §7).
 *
 * Lifted from design/project/solari.js (the vetted prototype flip mechanics: WAAPI flap-fall over
 * a short runway of REAL glyphs, never blur) and extended from a hardcoded 2-row demo into a live
 * component the snapshot feeds: window.Solari.mount(container) returns a controller whose update()
 * takes the real arrivals (newest first) and flutters ONLY the rows that changed.
 *
 * The disciplines this file owes the design record:
 *   • Genuinely satisfying (§0.8) — a left→right cascade of mechanical flips; the newest arrival
 *     announces itself with a soft clack run. This is where the animation-quality investment goes.
 *   • Settle < 1s — every row finishes its flutter under a second (the timing budget is asserted by
 *     construction below: (cols-1)*STAGGER + MAX_FLIPS*STEP, plus a small per-row lead, stays < 1000ms).
 *   • Readable mid-flutter — every intermediate frame is a legible character (pathTo walks real
 *     glyphs), never a blur.
 *   • prefers-reduced-motion honored — the row lands instantly, same information, no flutter/clack.
 *   • Clack ships low + toggleable — a short, quiet mechanical tick, gated by the fun toggle map;
 *     it is the ONLY sound anywhere in the product.
 *
 * This file is pixels only (design record B.1): every value it paints — the time, the flight
 * number, the title, the remark, the ordering (newest first) — arrives already decided by the
 * tested server. It computes no semantics; it only makes them flutter. */
(function () {
  "use strict";

  // The split-flap alphabet. A displayed glyph MUST live here (the flip runway indexes into it), so
  // the composer sanitizes any other character to a space — keeping every frame legible.
  var CH = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:·-#./&+";
  var CH_SET = {};
  for (var _i = 0; _i < CH.length; _i++) CH_SET[CH[_i]] = true;

  // Tile geometry — the shared split-flap scale (issue #31, boards visual pass). The original 21x32 /
  // 17px read as flaps beautifully but so LARGE that only ~13 characters of a title fit before it
  // clipped; brought down to 18x27 / 14px a full row reads as flaps AND ~32 columns fit at the default
  // desktop width (more on a wider shell, capped at 36), so titles stop clipping so hard. The line-up
  // matches the departures ``.flap`` height so the two boards' rows sit on one baseline. The mechanical
  // proportions (tall tile, centered glyph, mid seam) and — critically —
  // every MOTION constant below are unchanged, so the flutter behaviour (settle < 1s, readable
  // mid-flutter, reduced-motion) is exactly as vetted; only the tile pixel size changed. The departures
  // ``.flap`` (boards.css) adopts this same scale + tile face so the two boards read as one airport.
  var TILE_W = 18, TILE_H = 27, GAP = 3;
  var GLYPH_FONT = "600 14px/" + TILE_H + "px 'IBM Plex Sans Condensed', 'IBM Plex Sans', sans-serif";
  var MAX_ROWS = 5;                 // how many recent arrivals the board shows at once

  // Motion budget (DoD / §7: settle < 1s). The WHOLE board — a new arrival that shuffles every row
  // down — must settle under a second: (rows-1)*ROW_LEAD + (cols-1)*STAGGER + (MAX_FLIPS+1)*(STEP+8)
  // (pathTo walks MAX_FLIPS+1 real glyphs; the last flap runs STEP+8). At the 36-column cap that is
  // ~0.93s — comfortably inside the second, still an unhurried mechanical cascade (§0.8), not a snap.
  // Rescaling the tiles (issue #31) changed no motion constant, so this budget is untouched; the guard
  // test_static_boards_siblings.py pins it mechanically.
  var STAGGER = 12;                 // ms between adjacent tiles starting (the left→right cascade)
  var STEP = 54;                    // ms per single flap fall
  var MIN_FLIPS = 3, MAX_FLIPS = 5; // flaps a changed tile runs through before it settles
  var ROW_LEAD = 34;               // ms each lower row lags the one above (the board "shuffles down")
  var IDLE_RESET_MS = 300000;      // after 5 min with no page interaction, flap back to page 1 (§30 owner amendment)

  function styl(el, s) { for (var k in s) el.style[k] = s[k]; }
  function repeat(ch, n) { var s = ""; while (n-- > 0) s += ch; return s; }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function sanitize(s) {
    var up = String(s == null ? "" : s).toUpperCase();
    var out = "";
    for (var i = 0; i < up.length; i++) out += CH_SET[up[i]] ? up[i] : " ";
    return out;
  }

  // The tile-row string for one arrival: "HH:MM  SL-N  TITLE", uppercased, sanitized, clipped to the
  // board width. The remark (LANDED ✓ / SEE REPORT) is a coloured chip beside the tiles, not a flap.
  function composeLine(line, cols) {
    var parts = [];
    if (line.time) parts.push(line.time);
    if (line.flight) parts.push(line.flight);
    if (line.title) parts.push(line.title);
    return sanitize(parts.join("  ")).slice(0, cols);
  }

  // ---- one tile (top half / bottom half / seam), lifted from the prototype ----
  function makeTile() {
    var t = document.createElement("span");
    styl(t, {
      position: "relative", display: "inline-block", width: TILE_W + "px", height: TILE_H + "px",
      background: "#14181E", borderRadius: "3px", overflow: "hidden",
      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.09), inset 0 -6px 9px rgba(0,0,0,0.3)"
    });
    function half(top) {
      var h = document.createElement("span");
      styl(h, {
        position: "absolute", left: "0", right: "0", height: (TILE_H / 2) + "px", overflow: "hidden",
        top: top ? "0" : (TILE_H / 2) + "px", background: top ? "#2A313B" : "#1B2129"
      });
      var g = document.createElement("span");
      styl(g, {
        display: "block", width: TILE_W + "px", height: TILE_H + "px", textAlign: "center",
        font: GLYPH_FONT, color: "#F2F4EF", transform: top ? "none" : "translateY(-" + (TILE_H / 2) + "px)"
      });
      h.appendChild(g);
      t.appendChild(h);
      return g;
    }
    t._top = half(true);
    t._bot = half(false);
    var seam = document.createElement("span");
    styl(seam, { position: "absolute", left: "0", right: "0", top: "50%", height: "1px",
      background: "rgba(0,0,0,0.65)", zIndex: "3" });
    t.appendChild(seam);
    t._ch = " ";
    return t;
  }
  function setTile(t, ch) { t._top.textContent = ch; t._bot.textContent = ch; t._ch = ch; }

  // One mechanical flip: a flap carrying the OLD glyph falls over the new one (WAAPI, with a
  // setTimeout fallback so a throttled tab still settles).
  function flipStep(t, next, dur) {
    var flap = document.createElement("span");
    styl(flap, {
      position: "absolute", left: "0", right: "0", top: "0", height: (TILE_H / 2) + "px",
      overflow: "hidden", background: "#2A313B", zIndex: "2", transformOrigin: "bottom center",
      borderRadius: "3px 3px 0 0", boxShadow: "0 1px 2px rgba(0,0,0,0.4)"
    });
    var g = document.createElement("span");
    styl(g, { display: "block", width: TILE_W + "px", height: TILE_H + "px", textAlign: "center",
      font: GLYPH_FONT, color: "#F2F4EF" });
    g.textContent = t._ch;
    flap.appendChild(g);
    t.appendChild(flap);
    t._top.textContent = next;
    var done = function () {
      if (flap.parentNode) { flap.remove(); t._bot.textContent = next; t._ch = next; }
    };
    if (flap.animate) {
      var a = flap.animate([{ transform: "rotateX(0deg)" }, { transform: "rotateX(-88deg)" }],
        { duration: dur, easing: "ease-in" });
      a.onfinish = done;
      setTimeout(done, dur + 40);
    } else {
      done();
    }
  }

  // A short runway of real glyphs ending exactly on the target — never a random blur.
  function pathTo(target, k) {
    var ti = Math.max(0, CH.indexOf(target));
    var seq = [];
    for (var j = k; j >= 0; j--) seq.push(CH[(ti - j + CH.length * 3) % CH.length]);
    return seq;
  }

  // ---- the soft mechanical clack (the only sound in the product; ships low, §7) ----
  var _ctx = null;
  function clack() {
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!_ctx) _ctx = new AC();
      if (_ctx.state === "suspended") _ctx.resume();      // browsers gate audio on a user gesture
      var now = _ctx.currentTime;
      var o = _ctx.createOscillator();
      var g = _ctx.createGain();
      o.type = "square";
      o.frequency.setValueAtTime(1300 + Math.random() * 320, now);   // a tiny pitch scatter per tile
      g.gain.setValueAtTime(0.0001, now);
      g.gain.exponentialRampToValueAtTime(0.05, now + 0.004);        // fast attack, LOW peak
      g.gain.exponentialRampToValueAtTime(0.0001, now + 0.045);      // quick decay → a mechanical "tick"
      o.connect(g); g.connect(_ctx.destination);
      o.start(now); o.stop(now + 0.05);
    } catch (e) { /* audio unavailable — the board stays silent, never broken */ }
  }

  // Flutter one row of tiles to `str` (already composed + clipped). `announce` clacks each changed
  // tile as it starts (used for the newest arrival's row only — a satisfying left→right clatter that
  // isn't a wall of noise). Reduced/!animate lands instantly. `guard()` is a staleness check: a
  // newer update (a repo switch, or a second arrival within the settle window) supersedes this
  // flutter, so every scheduled flip drops if the render generation moved on — no stale glyph ever
  // overwrites the newer board.
  function land(tiles, str, reduced, announce, guard) {
    for (var i = 0; i < tiles.length; i++) {
      var target = i < str.length ? str[i] : " ";
      var t = tiles[i];
      if (t._ch === target) continue;
      if (reduced) { setTile(t, target); continue; }
      var k = MIN_FLIPS + (i % (MAX_FLIPS - MIN_FLIPS + 1));
      var seq = pathTo(target, k);
      (function (tile, sequence, col) {
        var start = col * STAGGER;
        if (announce) setTimeout(function () { if (!guard || guard()) clack(); }, start);
        sequence.forEach(function (ch2, j) {
          setTimeout(function () {
            if (guard && !guard()) return;   // superseded by a newer update — drop this stale flip
            flipStep(tile, ch2, j === sequence.length - 1 ? STEP + 8 : STEP);
          }, start + j * (STEP + 8));
        });
      })(t, seq, i);
    }
  }

  window.Solari = {
    mount: function (container) {
      container.innerHTML = "";
      container.className = (container.className || "") + " solari-mounted";

      var empty = document.createElement("div");
      empty.className = "solari-empty";
      empty.textContent = "— NOTHING HAS LANDED YET —";
      container.appendChild(empty);

      // Choose a column count from the live board width (fixed after mount for the mechanical look);
      // fall back to a sensible default when the panel hasn't laid out yet.
      var cols = Math.floor((container.clientWidth || 0) / (TILE_W + GAP));
      if (!cols || cols < 18) cols = 30;
      if (cols > 36) cols = 36;      // bound the widest row so the whole-board settle stays < 1s

      var rows = [];        // each: {wrap, tiles:[...], remark:<span>}
      for (var r = 0; r < MAX_ROWS; r++) {
        var wrap = document.createElement("div");
        wrap.className = "solari-row";
        var tilesBox = document.createElement("div");
        tilesBox.className = "solari-tiles";
        tilesBox.setAttribute("aria-hidden", "true");   // the flaps are decorative; the row carries a label
        var tiles = [];
        for (var c = 0; c < cols; c++) { var tl = makeTile(); tilesBox.appendChild(tl); tiles.push(tl); }
        var remark = document.createElement("span");
        remark.className = "solari-remark";
        wrap.appendChild(tilesBox);
        wrap.appendChild(remark);
        wrap.style.display = "none";       // rows reveal as arrivals fill them
        container.appendChild(wrap);
        rows.push({ wrap: wrap, tiles: tiles, remark: remark });
      }

      // The page control (issue #30) — prev · a split-flap page indicator · next. It lives INSIDE the
      // persistent .solari node, so the current page and these buttons survive every 2s poll re-render
      // exactly like the tiles do. The indicator is styled as a flap tile (boards.css) to keep the
      // board's mechanical character; it sits in the board's corner (right-aligned).
      var pager = document.createElement("div");
      pager.className = "solari-pager";
      pager.style.display = "none";        // revealed only when there is more than one page
      var prevBtn = document.createElement("button");
      prevBtn.type = "button"; prevBtn.className = "solari-page-prev"; prevBtn.textContent = "◀";
      prevBtn.setAttribute("aria-label", "newer arrivals");
      var pageNum = document.createElement("span");
      pageNum.className = "solari-page-num";
      pageNum.setAttribute("aria-live", "polite");
      var nextBtn = document.createElement("button");
      nextBtn.type = "button"; nextBtn.className = "solari-page-next"; nextBtn.textContent = "▶";
      nextBtn.setAttribute("aria-label", "older arrivals");
      pager.appendChild(prevBtn); pager.appendChild(pageNum); pager.appendChild(nextBtn);
      container.appendChild(pager);

      var ctrl = {
        _cols: cols,
        _allLines: [],       // the whole capped backlog the server handed us (newest first)
        _prevAllIds: null,   // ids across the WHOLE backlog last update (null = never rendered)
        _page: 0,            // which page is showing; survives polls so history stays readable
        _pages: 1,
        _gen: 0,             // render generation — bumped each paint so stale flip timers self-cancel
        _idleTimer: null,    // the 5-min "flap back to page 1" timer (armed only while off page 1)
        _repo: null,         // which repo's arrivals are showing — a change resets the page (follows camera, §4)
        _opts: { animate: true, clack: true },
        el: container,

        _pageCount: function () { return Math.max(1, Math.ceil(this._allLines.length / MAX_ROWS)); },

        _syncPager: function () {
          this._pages = this._pageCount();
          pager.style.display = this._pages > 1 ? "" : "none";
          pageNum.textContent = (this._page + 1) + " / " + this._pages;   // e.g. "2 / 3"
          prevBtn.disabled = this._page <= 0;
          nextBtn.disabled = this._page >= this._pages - 1;
        },

        // Restart the inactivity timer that flaps the board back to page 1 (owner amendment). It only
        // runs while OFF page 1 — page 1 is already the front, nothing to return from.
        _armIdle: function () {
          var self = this;
          if (self._idleTimer) { clearTimeout(self._idleTimer); self._idleTimer = null; }
          if (self._page > 0) self._idleTimer = setTimeout(function () { self.goToPage(0); }, IDLE_RESET_MS);
        },

        // Paint the current page. `flutter` runs the flap cascade (a real board change — a page turn,
        // or a new arrival on page 1); otherwise tiles are set instantly (a quiet poll on an unchanged
        // page). prefers-reduced-motion / animate:false always land instantly, same information (DoD).
        _paint: function (flutter) {
          var self = this, cols = this._cols;
          var gen = ++this._gen;                              // supersede any in-flight flutter
          var guard = function () { return gen === self._gen; };
          var reduced = reducedMotion() || self._opts.animate === false;
          var clackOn = !!self._opts.clack && !reduced;
          var doFlutter = flutter && !reduced;

          var start = this._page * MAX_ROWS;
          var lines = this._allLines.slice(start, start + MAX_ROWS);
          empty.style.display = this._allLines.length ? "none" : "";

          function pad(s) { return s.length < cols ? s + repeat(" ", cols - s.length) : s.slice(0, cols); }

          for (var r = 0; r < MAX_ROWS; r++) {
            var line = lines[r], row = rows[r];
            if (!line) { row.wrap.style.display = "none"; row.wrap.removeAttribute("data-fnum"); continue; }
            row.wrap.style.display = "";
            if (line.id != null) row.wrap.setAttribute("data-fnum", line.id);   // click-through to the flight
            // A screen reader reads this plain sentence, never the decorative per-tile glyphs.
            row.wrap.setAttribute("aria-label",
              [line.time, line.flight, line.title, line.remark].filter(Boolean).join(" ").replace(/▪/g, "").trim());
            setRemark(row.remark, line.remark);

            var want = pad(composeLine(line, cols));
            var current = row.tiles.map(function (t) { return t._ch; }).join("");
            if (current === want) continue;                  // already correct — no flutter, no work

            if (!doFlutter) { land(row.tiles, want, true, false, null); continue; }  // instant set (no timers)

            // A real board change: flutter. Row 0 clacks its cascade; lower rows lag a touch so the
            // board shuffles like a real one. Every scheduled flip is guard()ed, so a newer paint
            // mid-flutter (a page turn, another arrival) drops the stale work instead of overwriting.
            var announce = clackOn && r === 0;
            (function (tiles, str, delay, ann) {
              if (delay) setTimeout(function () { if (guard()) land(tiles, str, false, ann, guard); }, delay);
              else land(tiles, str, false, ann, guard);
            })(row.tiles, want, r * ROW_LEAD, announce);
          }
          this._syncPager();
        },

        // A poll feeds the WHOLE capped backlog; the board keeps its current page (so a reader browsing
        // history is not yanked back every 2s) and flutters ONLY when a genuinely new arrival appears
        // while page 1 (the newest) is showing — the flagship "it landed while I was gone" moment. A
        // steady poll with the same arrivals must NOT re-flutter (motion without meaning is a lie, §7).
        update: function (lines, opts) {
          this._opts = opts || { animate: true, clack: true };
          // The camera moved to another repo (§4 — the boards follow the camera): this persistent
          // board is now showing a DIFFERENT repo's arrivals, so it must start at page 1 (its newest),
          // not inherit repo A's page. Clear the old repo's idle timer and paint the new board fresh.
          var repo = this._opts.repo;
          if (repo !== undefined && repo !== this._repo) {
            this._repo = repo;
            this._page = 0;
            this._prevAllIds = null;
            if (this._idleTimer) { clearTimeout(this._idleTimer); this._idleTimer = null; }
          }
          this._allLines = (lines || []).slice();
          var pages = this._pageCount();
          if (this._page > pages - 1) this._page = pages - 1;   // backlog shrank → clamp into range
          if (this._page < 0) this._page = 0;

          var allIds = this._allLines.map(function (l) { return l.id; });
          var prevAll = this._prevAllIds;
          var hasNewArrival = prevAll === null
            || allIds.some(function (id) { return prevAll.indexOf(id) === -1; });
          this._prevAllIds = allIds;

          this._paint(this._page === 0 && hasNewArrival);
        },

        // Turn to a page — a prev/next click, or the inactivity timer returning to page 1. Always
        // flutters (the DoD: page transitions use the existing flap animation), reduced-motion honored
        // inside _paint. Any turn re-arms the 5-min idle timer.
        goToPage: function (p) {
          var pages = this._pageCount();
          this._page = Math.min(Math.max(0, p), pages - 1);
          this._paint(true);
          this._armIdle();
        }
      };

      prevBtn.addEventListener("click", function (e) { e.stopPropagation(); ctrl.goToPage(ctrl._page - 1); });
      nextBtn.addEventListener("click", function (e) { e.stopPropagation(); ctrl.goToPage(ctrl._page + 1); });

      return ctrl;
    }
  };

  function setRemark(span, remark) {
    var text = String(remark == null ? "" : remark).toUpperCase();
    span.textContent = text;
    var kind = "landed";
    if (/see report/i.test(text)) kind = "seereport";
    span.className = "solari-remark " + kind;
  }
})();
