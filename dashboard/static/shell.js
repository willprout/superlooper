/* command-center front-end — binds /api/snapshot to the four-panel shell (screen 7a) and boring
   mode (screen 8c). No framework, no build step. Design record B.1: the server computes every
   SEMANTIC; this file only binds values to pixels and handles view-local interaction (sort, filter,
   expand, the B toggle). It must never derive a stage, a tier, or a numeral — those arrive ready.

   Truth first (Task 5): the airfield canvas is a placeholder; the tower gloss + drawer are minimal
   (the rich versions are Task 9).

   The verbs (Task 6): every action button POSTs one of the six mechanical verbs to the server —
   the ONLY writes in the product. The button carries data-act/data-repo/data-num; a single
   delegated handler on #root dispatches. Two pieces live OUTSIDE #root (the flag composer and the
   toast), appended to <body>, because #root is fully re-rendered every poll (~2s) and a textarea or
   a confirm inside it would lose focus/state on the next tick; the drop-confirm keeps its state in
   `state` so it survives a re-render. The server derives every semantic — this file never decides
   which labels move, only which verb a tap fires. */
(function () {
  "use strict";

  var root = document.getElementById("root");
  var app = document.getElementById("app");

  var state = {
    snapshot: null,
    boring: (location.hash === "#boring"),   // deep-link straight into boring mode

    builtView: null,
    connOk: true,
    sort: { key: "stage", dir: "asc" },
    fireRange: "86400",      // last 24h
    fireFilter: "",
    expandedTower: {},       // key -> true (survives polls)
    showRoutine: false,      // reveal routine bookkeeping (relabels) in the tower log (issue #36) —
                             // a pure view toggle, off by default; survives the 2s re-render
    repoIndex: 0,
    towerRepin: false,       // set on a repo switch so the NEW repo's tower pins to its newest line
                             // (never inherits the old repo's scroll offset — different feed) (issue #27)
    confirmingDrop: null,    // "repo#num" mid-confirm — kept in state so a 2s re-render can't reset it
    depPage: 0,              // departures board page — kept in state so the 2s re-render preserves it,
                             // reset to page 1 on a repo switch (the new repo has its own queue) (issue #30)
  };

  // ---- tiny helpers ----
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // The ONLY client-side map: stage -> color. A pure pixel binding (a value to a visual), never a
  // semantic — the stage itself, its sort rank, and the in-air flag all arrive from the server.
  var STAGE_COLOR = {
    "at-stand": "#5A6572", "taxi-out": "#2D6BC4", "takeoff": "#2D6BC4", "downwind": "#2D6BC4",
    "base-turn": "#2D6BC4", "final": "#2F8A4C", "touchdown": "#8A93A0", "taxi-in": "#8A93A0",
    "parked": "#C77E1F", "awaiting": "#C77E1F", "holding": "#7A5CBF",
    "session-frozen": "#8A93A0", "stranded": "#C79A2E", "merges-freeze": "#2D8FA0",
  };

  function repo() {
    var s = state.snapshot;
    if (!s || !s.repos || !s.repos.length) return null;
    return s.repos[Math.min(state.repoIndex, s.repos.length - 1)];
  }

  // ============================ polling ============================
  function poll() {
    fetch("/api/snapshot", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (snap) {
        state.snapshot = snap;
        state.connOk = true;
        render();
      })
      .catch(function () {
        state.connOk = false;
        // Two very different failures share this path. If we have NEVER painted (no snapshot yet),
        // there is no shell to hang the conn-warn banner on — render() early-returns on a null
        // snapshot — so a first-poll failure would sit on the seeded "connecting…" text forever.
        // Paint an honest first-paint surface straight into #root instead (issue #34). Once we DO
        // have a last-good snapshot, the conn-warn banner over it is the right, less-alarming surface.
        if (!state.snapshot) renderDisconnected();
        else updateChrome();
      })
      .then(function () {
        var ms = 2000;
        if (state.snapshot && state.snapshot.poll_seconds) ms = state.snapshot.poll_seconds * 1000;
        window.clearTimeout(poll._t);
        poll._t = window.setTimeout(poll, ms);
      });
  }

  // ============================ render dispatch ============================
  function render() {
    if (!state.snapshot) return;
    if (state.boring) {
      if (state.builtView !== "boring") {
        root.innerHTML = boringSkeleton();
        state.builtView = "boring";
        wireBoring();
      }
      updateBoringTable();
      updateFirehose();
    } else {
      var towerScroll = captureTowerScroll();   // read the outgoing feed BEFORE the rebuild (issue #27)
      root.innerHTML = shellHTML();
      state.builtView = "shell";
      // The airfield's canvas is a PERSISTENT node owned by field.js — re-parent it into the
      // fresh mount so sprite state and the animation loop survive every innerHTML rebuild.
      if (window.CCField) {
        window.CCField.attach(el("field-mount"), state.snapshot, state.repoIndex);
      }
      // The Solari arrivals board is a PERSISTENT node owned by boards.js — re-parented the same
      // way so the split-flap flutter is never clobbered mid-air by a 2s poll (design record §0.8).
      if (window.Boards) {
        var rb = repo();
        window.Boards.attach(el("cc-solari"), rb && rb.boards ? rb.boards.arrivals : [], state.snapshot.fun,
                             rb ? rb.slug : "");
      }
      drawCrest();
      // Re-pin the tower feed AFTER its siblings are mounted, so the feed's height is final when we
      // measure — the newest line stays visible, a scrolled-up reader keeps their place (issue #27).
      applyTowerScroll(towerScroll);
    }
    // The drawer lives OUTSIDE #root (survives the re-render); feed it the fresh flight each poll so
    // an open flight card tracks live state (design record §4 — always the same, always current).
    refreshDrawer();
    updateChrome();
  }

  // The honest FIRST-PAINT failure surface (issue #34). Before the first successful snapshot there is
  // no shell — so no conn-warn banner, and render() early-returns on a null snapshot. Without this,
  // a server that never answers the first poll (down, or the wrong port) leaves the page frozen on
  // the seeded "connecting to the field…" text with no error surface at all. This replaces that seed
  // with a plain, actionable "can't reach the tower" card the moment the first poll fails. It
  // self-heals for free: poll() keeps retrying, and the first success calls render(), which rebuilds
  // #root wholesale from the fresh snapshot and wipes this card. Only ever shown pre-first-snapshot
  // (the caller gates on !state.snapshot) — once a last-good snapshot exists, the conn-warn banner
  // over it is the calmer, correct surface.
  function renderDisconnected() {
    root.innerHTML =
      '<div class="cc-disconnected" role="alert">' +
        '<div class="cc-disc-title"><span class="dot"></span>can’t reach the tower</div>' +
        '<div class="cc-disc-body">No answer from the command center at this address yet. ' +
          'Is <code>bin/command-center</code> running, and on the port you opened? ' +
          'If that port was taken, change <code>port</code> in your <code>config.json</code>.</div>' +
        '<div class="cc-disc-foot">Retrying automatically… the field appears the moment it answers.</div>' +
      '</div>';
  }

  // The flight-card drawer: find a flight's server-composed drawer object by (repo, num) across the
  // whole field (Needs You is whole-field, so a click may name any watched repo's flight). The match
  // is STRICT on repo when a slug is given — flight numbers are only unique WITHIN a repo, so a
  // number-only match could return (and then let a verb write to) a different repo's same-numbered
  // flight. A blank slug (never expected from a real click) falls back to a number match.
  function findDrawer(repoSlug, num) {
    var s = state.snapshot;
    if (!s || !s.flights) return null;
    num = Number(num);
    for (var i = 0; i < s.flights.length; i++) {
      var f = s.flights[i];
      if (!f.drawer || f.num !== num) continue;
      if (!repoSlug || f.drawer.repo === repoSlug) return f.drawer;
    }
    return null;
  }

  function refreshDrawer() {
    if (!window.Drawer || !window.Drawer.isOpen()) return;
    var c = window.Drawer.current();
    var d = findDrawer(c.repo, c.num);
    if (d) window.Drawer.update(d);                    // absent (flight left the field) → keep last view
  }

  function openDrawer(repoSlug, num) {
    if (!window.Drawer) return;
    var d = findDrawer(repoSlug, num);
    // A clickable with no on-field flight (a still-queued departure) still opens a minimal card —
    // the issue link is the useful part; the rich drawer is for flights actually in the air.
    if (!d) {
      d = { num: Number(num), flight: "SL-" + num, repo: repoSlug, airline: "",
            title: "queued — not yet in the air", stage: null, circuit: [], off_path: null,
            clearance: [], memos: [], cargo: { present: false }, journal: [], go_arounds: 0,
            links: { issue: "https://github.com/" + repoSlug + "/issues/" + num, pr: null, branch: null } };
    }
    window.Drawer.open(d);
  }

  // The airline crest in the top bar (§7 identity) — a pure renderer call with the server's
  // deterministic tail color; the CSS placeholder square stays when the renderer isn't loaded.
  function drawCrest() {
    var c = root.querySelector("canvas.crest");
    var r = repo();
    if (c && r && window.Airfield3) {
      window.Airfield3.drawCrest(c, { bg: (r.colors && r.colors.tail) || undefined });
    }
  }

  function updateChrome() {
    var s = state.snapshot;
    app.classList.toggle("runner-down", !!(s && s.runner && s.runner.down));
    var banner = el("runner-banner");
    if (banner) banner.hidden = !(s && s.runner && s.runner.down);
    var conn = el("conn-warn");
    if (conn) conn.hidden = state.connOk;
  }

  // ============================ the shell (screen 7a) ============================
  function shellHTML() {
    var s = state.snapshot, r = repo();
    return (
      '<div class="conn-warn" id="conn-warn" hidden>lost the field — reconnecting… (showing the last good snapshot)</div>' +
      '<div class="runner-banner" id="runner-banner" hidden>' +
        '<span class="dot"></span><span class="t">RUNNER DOWN</span>' +
        '<span class="sub">' + esc((s.runner && s.runner.message) || "") + '</span></div>' +
      '<div class="shell">' +
        topbarHTML(s, r) +
        troubleHTML(s) +
        // Empty Needs You collapses to a slim rail and the airfield gains the reclaimed width (§4).
        // The class is bound to the server's all_clear flag (never computed here, B.1); the 2s poll
        // rebuilds this from the fresh snapshot, so a decision appearing restores the full column
        // with no reload.
        '<div class="main' + (s.all_clear ? ' needs-collapsed' : '') + '">' +
          needsYouHTML(s) +
          fieldHTML(s, r) +
          towerHTML(r) +
        '</div>' +
        boardsHTML(r) +
      '</div>'
    );
  }

  function topbarHTML(s, r) {
    var pill = s.pill || {}, usage = s.usage || {};
    var airline = r ? r.airline : "SUPERLOOPER";
    // pill message comes ready-made from the server (design B.1); the class is a pixel binding off level.
    var msg = pill.message || "all systems ok";
    var cls = "pill";
    if ((s.runner && s.runner.down) || pill.level === "alert") cls = "pill alert";
    else if (pill.level === "ok") cls = "pill ok";

    var usageHTML;
    if (usage.known) {
      var pct = Math.round(usage.five_hour_pct);
      usageHTML = '<span class="k">usage</span>' +
        '<span class="usage-bar"><span style="width:' + pct + '%"></span></span>' +
        '<span class="usage-pct">' + pct + '%</span>';
    } else {
      usageHTML = '<span class="k" title="' + esc(usage.status || "unknown") + '">usage ?</span>';
    }

    var wk = s.shipped_total || {};
    var cargo = s.live_cargo || {};
    var cargoHTML = (cargo.present && (cargo.added || cargo.removed)) ?
      ' · <span class="up">+' + cargo.added + '</span> <span class="down">−' + cargo.removed + '</span> IN FLIGHT' : "";
    var go = wk.go_arounds || 0;

    return '<div class="topbar">' +
      '<div class="brand"><canvas class="crest" width="44" height="44"></canvas>' +
        '<div class="brand-name">' + esc(airline.toUpperCase()) + ' <span class="airways">AIRWAYS</span></div></div>' +
      '<div class="' + cls + '">' +
        '<span class="dot"></span><span class="msg">' + esc(msg) + '</span>' +
        '<span class="sep">·</span><span class="k">tower: auto</span>' +
        '<span class="sep">·</span>' + usageHTML +
      '</div>' +
      '<div class="spacer"></div>' +
      '<div class="corner">THIS WEEK&nbsp;&nbsp;<strong>' + (wk.landings_window || 0) + ' LANDED</strong>' + cargoHTML +
        (go ? ' · ' + go + ' GO-AROUND' + (go === 1 ? '' : 'S') + ' SURVIVED' : '') + '</div>' +
      // The Tidy button (issue #41) — the one OPS-verb button: closes the windows of finished
      // sessions by running `superlooper tidy` locally (a confirm dialog lists exactly what will
      // close first). Carries the camera repo, like Flag, so a tap tidies the repo on screen.
      '<button class="tidy-btn" data-act="tidy-open" data-repo="' + esc(r ? r.slug : "") + '"' +
        (r ? "" : " disabled") +
        ' title="Tidy — close the terminal windows of finished sessions (runs superlooper tidy locally; no GitHub)">\u{1F9F9} Tidy</button>' +
      // The Restart button (issue #116) — an OPS-verb button: asks the LIVE runner to restart
      // itself in its own cmux tab (runs `superlooper request-restart` locally; a confirm dialog states
      // exactly what will happen first, and reports honestly when no loop is running). Never launches
      // or places a tab. Carries the camera repo, like Tidy/Flag, so a tap targets the repo on screen.
      '<button class="restart-btn" data-act="restart-open" data-repo="' + esc(r ? r.slug : "") + '"' +
        (r ? "" : " disabled") +
        ' title="Restart the loop — asks the running runner to restart itself in its own cmux tab (runs superlooper request-restart locally; no GitHub)">\u{1F504} Restart</button>' +
      // The Janitor button (issue #121) — an ops-verb button: clears GitHub-side debris
      // (stale merged/superseded loop branches, superseded PRs, aged parked issues) by running
      // `superlooper janitor` locally; a sweep dialog groups every proposal and executes only the
      // ones tapped. Carries the camera repo, like Flag/Tidy, so a tap sweeps the repo on screen.
      '<button class="janitor-btn" data-act="janitor-open" data-repo="' + esc(r ? r.slug : "") + '"' +
        (r ? "" : " disabled") +
        ' title="Sweep — clear GitHub-side debris (runs superlooper janitor locally; you tap exactly what to clear)">\u{1F5D1}️ Sweep</button>' +
      '<button class="flag-btn" data-act="flag-open" data-repo="' + esc(r ? r.slug : "") + '"' +
        (r ? "" : " disabled") +
        ' title="Flag something you see — files a GitHub issue labeled flag (no AI)">⚑ Flag</button>' +
    '</div>';
  }

  function troubleHTML(s) {
    var t = s.trouble || { present: false };
    if (!t.present) return '<div class="trouble" hidden></div>';
    var cls = t.level === "alert" ? "trouble alert" : "trouble";
    return '<div class="' + cls + '"><span class="dot"></span>' + esc(t.text) + '</div>';
  }

  function needsYouHTML(s) {
    // Every semantic (headline, gloss, kind, conflict-cap collision, Discuss-default) is the
    // server's; needsyou.js binds it. The drop-confirm state rides along so the 2s re-render can't
    // silently disarm a mid-confirm Drop (design record §4 — the panel never moves).
    return window.NeedsYou.panelHTML(s.needs_you, state.confirmingDrop);
  }

  function fieldHTML(s, r) {
    // The legend follows the camera, like the boards (§4): tally the SELECTED repo's flights.
    // Counting a server-supplied boolean is pure presentation — the classification is the server's.
    var flights = (r && r.flights) || [];
    var inAir = flights.filter(function (f) { return f.display && f.display.in_air; }).length;
    var deps = r && r.boards ? r.boards.departures.length : 0;
    // GitHub-unreachable (issue #38): the queue read failed closed to empty, so it is UNREAD, not
    // empty — the legend must not claim "QUEUE EMPTY". The server's github.unreachable flag drives a
    // distinct dark data-link legend instead (the field's dark-tower state carries the same truth).
    var ghDown = !!(r && r.github && r.github.unreachable);
    // Empty queue → the server's caption, which states the repo's REAL lane count (singular/plural +
    // honest no-number fallback all decided server-side, design record B.1 / issue #35). The JS only
    // binds it; the "QUEUE EMPTY" default guards the no-repo-on-camera case (r is null).
    var queue = deps ? deps + " QUEUED"
      : (ghDown ? "◈ NO DATA LINK" : ((r && r.queue_empty_caption) || "QUEUE EMPTY"));
    var clock = s.clock || "--:--";
    // The living clock label + the repo selector (single-repo VIEW — the grid is a later flight).
    var daypart = (s.daypart || "day").toUpperCase();
    var repos = s.repos || [];
    var selector = repos.length > 1 ? '<span class="repo-sel">' + repos.map(function (rp, i) {
      var on = i === Math.min(state.repoIndex, repos.length - 1);
      // The active tab is marked aria-current so "which repo is on camera" is exposed to a11y and is
      // semantically single (issue #44); the loud glance treatment — selection cursor, glow, camera
      // notch — is pure CSS on .repo-tab.on so this file decides no semantics.
      return '<button class="repo-tab' + (on ? " on" : "") + '" data-repoidx="' + i + '"' +
        (on ? ' aria-current="true" title="on camera — the field below shows this repo"' : '') + '>' +
        esc(rp.name) + '</button>';
    }).join("") + '</span>' : "";
    // Replay (a treat) + digest (the mechanical account) live behind buttons on the field head
    // (design record §4: [⏮ replay] rides the airfield). Both open on-demand overlays — no ritual,
    // no schedule (§0.2). Disabled with no repo on camera.
    var rslug = r ? r.slug : "";
    var tools = '<span class="field-tools">' +
      '<button class="field-tool" data-act="replay-open" data-repo="' + esc(rslug) + '"' +
        (r ? "" : " disabled") + ' title="Night replay — a scrubbable time-lapse of the journal (a treat)">⏮ REPLAY</button>' +
      '<button class="field-tool" data-act="digest-open" data-repo="' + esc(rslug) + '"' +
        (r ? "" : " disabled") + ' title="Morning digest — mechanical counts + one sentence per exception">▤ DIGEST</button>' +
      '</span>';
    return '<div class="panel">' +
      '<div class="panel-head"><span class="panel-title">' +
        esc((r ? r.airline : "FIELD")).toUpperCase() + ' FIELD</span>' +
        '<span class="sub">' + esc(r ? r.slug : "") + '</span>' + selector + tools +
        '<span class="right">' + esc(daypart) + ' · ' + clock + ' LOCAL</span></div>' +
      '<div class="field-frame"><div class="field-mount" id="field-mount"></div></div>' +
      '<div class="field-legend">' +
        '<span class="in-air">● ' + inAir + ' IN THE AIR</span>' +
        '<span>' + esc(queue) + '</span>' +
        '<span class="look">' + (s.needs_you ? s.needs_you.length : 0) + ' WAITING ON YOU</span>' +
      '</div></div>';
  }

  function towerHTML(r) {
    // The comms feed — radio flavor, glosses, tier, and the "since you last looked" divider all
    // arrive ready from the server (lib/tower); tower.js binds them. `expandedTower` (which rows show
    // their raw line) and `showRoutine` (whether routine bookkeeping is revealed, issue #36) are
    // view-local and survive polls.
    return window.Tower.panelHTML(r ? r.tower_log : [], state.expandedTower,
                                  r ? r.tower_new : 0, state.showRoutine);
  }

  // The tower feed scrolls INSIDE its panel (issue #27, shell.css). #root is rebuilt wholesale every
  // poll (~2s) and on every expand, which resets the fresh feed to the top (its OLDEST line). These
  // two seams re-pin the feed across that rebuild: capture the outgoing feed's scroll BEFORE the
  // rebuild, re-apply it AFTER. A reader parked at the bottom stays pinned to the newest line (the
  // reading order is newest-at-bottom); a reader who scrolled up into history keeps their place,
  // measured as distance-from-bottom so it stays put as new lines land below. No prior feed (first
  // render, or arriving from boring mode) → pin to the newest line so it is visible by default.
  function captureTowerScroll() {
    var f = root.querySelector(".tower-feed");
    // No feed to read (first render / from boring), or a repo switch just swapped in a DIFFERENT
    // feed — either way there is no meaningful prior offset, so pin the fresh feed to its newest line.
    if (!f || state.towerRepin) { state.towerRepin = false; return { pinned: true, fromBottom: 0 }; }
    var fromBottom = f.scrollHeight - f.scrollTop - f.clientHeight;
    return { pinned: fromBottom <= 4, fromBottom: Math.max(0, fromBottom) };
  }
  function applyTowerScroll(keep) {
    var f = root.querySelector(".tower-feed");
    if (!f || !keep) return;
    f.scrollTop = keep.pinned
      ? f.scrollHeight                                    // newest line visible at the bottom
      : Math.max(0, f.scrollHeight - f.clientHeight - keep.fromBottom);
  }

  // The boards (design record §3). Departures is the real launch order (window.Boards.departuresInner:
  // ⚡ expedite on top, priority band, number; a blocked-by connection reads "awaiting connection
  // SL-N", dimmed, never in the air). Arrivals is the Solari flagship: an EMPTY mount here (#cc-solari)
  // that render() fills via Boards.attach — a persistent node like the field canvas, so the split-flap
  // flutter is never wiped by a poll. Both read the SELECTED repo, so the boards follow the camera (§4).
  function boardsHTML(r) {
    var deps = (r && r.boards) ? r.boards.departures : [];
    // Normalize the held page against the CURRENT queue every render, not only on a click: if the
    // queue shrank between polls, state.depPage could sit past the last page and would resurface on a
    // regrow. Clamp it here so the stored page always matches what's shown (issue #30, Codex review).
    var depSize = (window.Boards && window.Boards.DEP_PAGE_SIZE) || 5;
    var depPageMax = Math.max(0, Math.ceil(deps.length / depSize) - 1);
    if (state.depPage > depPageMax) state.depPage = depPageMax;
    if (state.depPage < 0) state.depPage = 0;
    var ghDown = !!(r && r.github && r.github.unreachable);   // an unread queue → dark data-link (#38)
    return '<div class="boards">' +
      '<div class="board" id="cc-departures">' + window.Boards.departuresInner(deps, r ? r.slug : "", state.depPage, r ? r.queue_empty_caption : "", ghDown) + '</div>' +
      '<div class="board" id="cc-arrivals">' +
        '<div class="board-head"><span class="dot"></span><span class="name">ARRIVALS</span>' +
          '<span class="tag">LANDED · NEWEST FIRST</span></div>' +
        '<div class="solari-host" id="cc-solari"></div>' +
      '</div>' +
    '</div>';
  }

  // One delegated click listener on the persistent #root, installed ONCE at init (below). Because
  // the shell re-renders its innerHTML every poll, re-attaching per render would leak a listener
  // each time and make tower toggles fire N times. Boring mode wires its own element listeners on
  // its skeleton (rebuilt only on toggle), so this handler no-ops while boring is active. Clicks may
  // land on a child span, so each target is resolved with closest().
  function onShellClick(e) {
    if (state.boring) return;
    var node = e.target;
    if (!node || !node.closest) return;

    var actEl = node.closest("[data-act]");
    if (actEl) { handleAction(actEl); return; }

    var repoEl = node.closest("[data-repoidx]");
    if (repoEl) {                       // the repo selector — switch which field is on camera
      state.repoIndex = Number(repoEl.getAttribute("data-repoidx")) || 0;
      state.towerRepin = true;          // new repo → its tower shows the newest line, not repo A's offset
      state.depPage = 0;                // new repo → its departures queue starts on page 1 (issue #30)
      render();
      return;
    }

    // The departures page control (issue #30) — a view-state page turn, not a write. Clamp against the
    // camera repo's queue length (via Boards.DEP_PAGE_SIZE) so a turn never lands past the last page.
    var depPgEl = node.closest("[data-deppage]");
    if (depPgEl) {
      var dir = depPgEl.getAttribute("data-deppage");
      var rr = repo();
      var depsN = (rr && rr.boards) ? rr.boards.departures.length : 0;
      var size = (window.Boards && window.Boards.DEP_PAGE_SIZE) || 5;
      var pageMax = Math.max(0, Math.ceil(depsN / size) - 1);
      var np = state.depPage + (dir === "next" ? 1 : -1);
      state.depPage = Math.min(Math.max(0, np), pageMax);
      render();
      return;
    }

    var routineEl = node.closest("[data-tower-routine]");
    if (routineEl) {                     // reveal / hide routine bookkeeping in the tower log (#36) —
      state.showRoutine = !state.showRoutine;   // a pure view toggle, never a write
      render();
      return;
    }

    var togEl = node.closest("[data-toggle]");
    if (togEl) {
      var key = togEl.getAttribute("data-toggle");
      state.expandedTower[key] = !state.expandedTower[key];
      render();
      return;
    }
    var fEl = node.closest("[data-fnum]");
    if (fEl) {
      // Prefer the element's own data-repo (whole-field surfaces like Needs You set it); fall back
      // to the camera's repo for the per-repo surfaces (tower, boards) that don't carry one.
      var fr = fEl.getAttribute("data-repo") || (repo() ? repo().slug : "");
      openDrawer(fr, fEl.getAttribute("data-fnum"));
    }
  }

  // ============================ the verbs — the only writes (Task 6) ============================
  function handleAction(el) {
    var act = el.getAttribute("data-act");
    var repo = el.getAttribute("data-repo");
    var num = el.getAttribute("data-num");

    if (act === "flag-open") { openFlagBox(repo); return; }
    if (act === "tidy-open") { if (window.CCTidy) window.CCTidy.open(repo); return; }
    if (act === "restart-open") { if (window.CCRestart) window.CCRestart.open(repo); return; }
    if (act === "janitor-open") { if (window.CCJanitor) window.CCJanitor.open(repo); return; }
    if (act === "replay-open") { if (window.CCReplay) window.CCReplay.open(repo, state.snapshot && state.snapshot.fun); return; }
    if (act === "digest-open") { if (window.CCDigest) window.CCDigest.open(repo); return; }
    if (act === "discuss") { doDiscuss(repo, num); return; }
    if (act === "approve") {
      var owner = (state.snapshot && state.snapshot.operator) || "the owner";
      postVerb("/api/approve", repo, num, "Approved SL-" + num + " — " + owner + "'s word is recorded");
      return;
    }
    if (act === "bounce-yes") { postVerb("/api/bounce-yes", repo, num, "Bounce accepted — SL-" + num + " relaunching"); return; }
    if (act === "expedite") { postVerb("/api/expedite", repo, num, "⚡ Expedited SL-" + num); return; }
    if (act === "drop") { onDrop(repo, num); return; }
  }

  // Drop is the ONE destructive tap, so it takes a single inline confirm: first tap arms it (the
  // button flips to "Drop — tap again"), second tap within the window closes the issue. The armed
  // state lives in `state` so the 2s poll re-render can't silently disarm it; a timeout clears it.
  function onDrop(repo, num) {
    var keyd = repo + "#" + num;
    if (state.confirmingDrop !== keyd) {
      state.confirmingDrop = keyd;
      render();
      window.clearTimeout(onDrop._t);
      onDrop._t = window.setTimeout(function () {
        if (state.confirmingDrop === keyd) { state.confirmingDrop = null; render(); }
      }, 5000);
      return;
    }
    window.clearTimeout(onDrop._t);
    state.confirmingDrop = null;
    postVerb("/api/drop", repo, num, "Dropped SL-" + num + " — issue closed");
  }

  function postVerb(path, repo, num, okMsg) {
    postJSON(path, { repo: repo, num: Number(num) })
      .then(function (res) {
        if (res.status === 200 && res.body && res.body.ok) {
          toast(okMsg, "ok");
          refresh();     // re-poll so the field reflects the new GitHub state as the runner acts
        } else {
          toast((res.body && res.body.error) || "GitHub write failed — nothing changed", "err");
        }
      })
      .catch(function () { toast("couldn't reach the command center", "err"); });
  }

  function doDiscuss(repo, num) {
    postJSON("/api/discuss", { repo: repo, num: Number(num) })
      .then(function (res) {
        if (res.status === 200 && res.body && res.body.ok && res.body.text) {
          copyToClipboard(res.body.text);
          toast("Briefing for SL-" + num + " copied — paste into a fresh Claude", "ok");
        } else {
          toast("couldn't compose the briefing", "err");
        }
      })
      .catch(function () { toast("couldn't reach the command center", "err"); });
  }

  function postJSON(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        return { status: r.status, body: b };
      });
    });
  }

  function refresh() { window.clearTimeout(poll._t); poll(); }

  // ---- clipboard (Discuss copies the briefing) ----
  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  }
  function fallbackCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.focus(); ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (err) { /* clipboard may be blocked; the toast still says it's ready */ }
  }

  // ---- toast: transient feedback, lives OUTSIDE #root so a poll re-render never clobbers it ----
  function toast(msg, kind) {
    var t = el("cc-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "cc-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.className = "cc-toast show " + (kind === "err" ? "err" : "ok");
    window.clearTimeout(toast._t);
    toast._t = window.setTimeout(function () { t.className = "cc-toast"; }, 3400);
  }

  // ---- flag composer: an overlay OUTSIDE #root (a textarea inside #root would lose focus every
  //      poll). Built once, wired once; opened against the currently-viewed repo. ----
  function ensureFlagBox() {
    var box = el("cc-flagbox");
    if (box) return box;
    box = document.createElement("div");
    box.id = "cc-flagbox";
    box.className = "cc-flagbox";
    box.innerHTML =
      '<div class="cc-flag-card">' +
        '<div class="cc-flag-head">⚑ FLAG <span id="cc-flag-target"></span></div>' +
        '<div class="cc-flag-sub">Files your raw text as a GitHub issue labeled <b>flag</b>. No AI — a planning session sweeps flags later.</div>' +
        '<textarea id="cc-flag-text" rows="4" placeholder="what did you notice?" spellcheck="true"></textarea>' +
        '<div class="cc-flag-actions">' +
          '<button class="btn ghost" id="cc-flag-cancel">Cancel</button>' +
          '<button class="btn primary" id="cc-flag-send">File flag</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(box);
    el("cc-flag-cancel").addEventListener("click", closeFlagBox);
    el("cc-flag-send").addEventListener("click", submitFlag);
    box.addEventListener("click", function (e) { if (e.target === box) closeFlagBox(); });
    el("cc-flag-text").addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeFlagBox();
      // ⌘/Ctrl+Enter files it — a keyboard-first quick-capture
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitFlag();
    });
    return box;
  }

  function openFlagBox(repoSlug) {
    if (!repoSlug) return;
    var box = ensureFlagBox();
    box._repo = repoSlug;
    el("cc-flag-target").textContent = "→ " + repoSlug;
    el("cc-flag-text").value = "";
    box.classList.add("open");
    el("cc-flag-text").focus();
  }

  function closeFlagBox() {
    var box = el("cc-flagbox");
    if (box) box.classList.remove("open");
  }

  function submitFlag() {
    var box = el("cc-flagbox");
    if (!box) return;
    var text = el("cc-flag-text").value.trim();
    if (!text) { el("cc-flag-text").focus(); return; }
    var send = el("cc-flag-send");
    send.disabled = true;
    postJSON("/api/flag", { repo: box._repo, text: text })
      .then(function (res) {
        send.disabled = false;
        if (res.status === 200 && res.body && res.body.ok) {
          closeFlagBox();
          toast("Flag filed" + (res.body.num ? " — issue #" + res.body.num : ""), "ok");
          refresh();
        } else {
          toast((res.body && res.body.error) || "couldn't file the flag", "err");
        }
      })
      .catch(function () { send.disabled = false; toast("couldn't reach the command center", "err"); });
  }

  // ============================ boring mode (screen 8c — fully static) ============================
  var COLS = [
    { key: "flight", label: "FLIGHT" }, { key: "repo", label: "REPO" }, { key: "stage", label: "STAGE" },
    { key: "elapsed", label: "ELAPSED" }, { key: "idle", label: "IDLE" }, { key: "diff", label: "Δ DIFF" },
    { key: "files", label: "FILES" }, { key: "attempt", label: "ATTEMPT" }, { key: "note", label: "NOTE" },
  ];

  function boringSkeleton() {
    return (
      '<div class="conn-warn" id="conn-warn" hidden>lost the field — reconnecting…</div>' +
      '<div class="runner-banner" id="runner-banner" hidden><span class="dot"></span>' +
        '<span class="t">RUNNER DOWN</span><span class="sub"></span></div>' +
      '<div class="boring">' +
        '<div class="boring-bar"><span class="tag">BORING MODE</span>' +
          '<span class="hint">PRESS <kbd>B</kbd> TO TOGGLE</span>' +
          '<span class="hint" id="boring-slugs"></span>' +
          '<span class="right">EVERY VISUAL CHANNEL PAIRED WITH AN EXACT NUMERAL — SORT BY THE NUMBER, THE ART IS FLAVOR</span></div>' +
        '<div class="boring-body">' +
          '<div class="btable"><div class="thead" id="btable-head"></div><div id="btable-body"></div></div>' +
          '<div class="firehose">' +
            '<div class="firehose-head"><span class="tag">JOURNAL.JSONL — FIREHOSE</span>' +
              '<select id="fire-range">' +
                '<option value="3600">last 1h</option>' +
                '<option value="86400" selected>last 24h</option>' +
                '<option value="all">all</option>' +
              '</select>' +
              '<input id="fire-filter" type="text" placeholder="filter: act=park OR act=hold…" spellcheck="false">' +
              '<span class="right">FLIGHT NUMBERS CLICK THROUGH TO FILTER</span></div>' +
            '<div class="firehose-lines" id="fire-lines"></div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function wireBoring() {
    el("fire-range").value = state.fireRange;
    el("fire-filter").value = state.fireFilter;
    el("btable-head").addEventListener("click", function (e) {
      var key = e.target.getAttribute && e.target.getAttribute("data-key");
      if (!key) return;
      if (state.sort.key === key) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      else { state.sort.key = key; state.sort.dir = "asc"; }
      updateBoringTable();
    });
    el("btable-body").addEventListener("click", function (e) {
      var fnum = e.target.getAttribute && e.target.getAttribute("data-fnum");
      if (fnum) filterToFlight(fnum);
    });
    el("fire-lines").addEventListener("click", function (e) {
      var fnum = e.target.getAttribute && e.target.getAttribute("data-fnum");
      if (fnum) filterToFlight(fnum);
    });
    el("fire-range").addEventListener("change", function () {
      state.fireRange = el("fire-range").value; updateFirehose();
    });
    el("fire-filter").addEventListener("input", function () {
      state.fireFilter = el("fire-filter").value; updateFirehose();
    });
  }

  function updateBoringTable() {
    var head = el("btable-head"); if (!head) return;
    // The literal slug stays visible everywhere in boring mode (§7 — airline flavor never
    // replaces ground truth): NAME = owner/name for every repo on the field.
    var slugs = el("boring-slugs");
    if (slugs) {
      slugs.textContent = (state.snapshot.repos || []).map(function (r) {
        return r.name + " = " + r.slug;
      }).join(" · ");
    }
    head.innerHTML = COLS.map(function (c) {
      var sorted = state.sort.key === c.key;
      var arrow = sorted ? '<span class="arrow">' + (state.sort.dir === "asc" ? "▲" : "▼") + '</span>' : "";
      return '<span class="col' + (sorted ? " sorted" : "") + '" data-key="' + c.key + '">' + c.label + " " + arrow + '</span>';
    }).join("");

    var flights = (state.snapshot.flights || []).map(function (f) { return f.display || {}; });
    flights = sortFlights(flights, state.sort);
    var body = el("btable-body");
    if (!flights.length) { body.innerHTML = '<div class="btable-empty">no flights on the field</div>'; return; }
    body.innerHTML = flights.map(function (d) {
      var color = STAGE_COLOR[d.stage] || "#2A323C";
      return '<div class="brow">' +
        '<span class="fnum" data-fnum="' + esc(d.num) + '">' + esc(d.flight) + '</span>' +
        '<span class="repo">' + esc(d.repo) + '</span>' +
        '<span class="stage" style="color:' + color + '">' + esc(d.stage) + '</span>' +
        '<span>' + esc(d.elapsed) + '</span>' +
        '<span>' + esc(d.idle) + '</span>' +
        '<span>' + esc(d.diff) + '</span>' +
        '<span>' + esc(d.files == null ? "—" : d.files) + '</span>' +
        '<span>' + esc(d.attempt) + '</span>' +
        '<span class="note" title="' + esc(d.note) + '">' + esc(d.note || "—") + '</span>' +
      '</div>';
    }).join("");
  }

  function sortFlights(rows, sort) {
    var key = sort.key, dir = sort.dir === "asc" ? 1 : -1;
    function val(d) {
      if (key === "flight") return d.num;
      if (key === "repo") return d.repo;
      if (key === "stage") return d.stage_rank;         // circuit order comes from the server
      if (key === "elapsed") return d.elapsed_seconds;
      if (key === "idle") return d.staleness;
      if (key === "diff") return (d.diff_added || 0) + (d.diff_removed || 0);
      if (key === "files") return d.files;
      if (key === "attempt") return d.attempt;
      if (key === "note") return d.note || "";
      return 0;
    }
    return rows.slice().sort(function (a, b) {
      var va = val(a), vb = val(b);
      var na = va == null || va === -1, nb = vb == null || vb === -1;
      if (na && nb) return 0;
      if (na) return 1;        // unknown always sorts to the bottom, regardless of direction
      if (nb) return -1;
      if (typeof va === "string" || typeof vb === "string") {
        return String(va).localeCompare(String(vb)) * dir;
      }
      return (va - vb) * dir;
    });
  }

  function updateFirehose() {
    var lines = el("fire-lines"); if (!lines) return;
    var recs = (state.snapshot.journal_tail || []);
    var now = state.snapshot.generated_at;
    var range = state.fireRange;
    var terms = parseFilter(state.fireFilter);

    var shown = recs.filter(function (r) {
      if (range !== "all") {
        if (r.ts == null) return false;
        if (now - r.ts > Number(range)) return false;
      }
      return matchesFilter(r.raw, terms);
    });
    // newest first
    shown.sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });

    if (!shown.length) { lines.innerHTML = '<div class="firehose-empty">no journal lines match this window/filter</div>'; return; }
    lines.innerHTML = shown.slice(0, 200).map(function (r) {
      var chip = r.num != null
        ? '<span class="fnum" data-fnum="' + esc(r.num) + '">SL-' + esc(r.num) + '</span> ' : "";
      return '<div class="fline">' + chip + esc(r.raw) + '</div>';
    }).join("");
  }

  function parseFilter(f) {
    f = (f || "").trim();
    if (!f) return null;
    return f.split(/\s+OR\s+/i).map(function (t) {
      t = t.trim();
      var eq = t.indexOf("=");
      if (eq > 0) return { k: t.slice(0, eq).trim(), v: t.slice(eq + 1).trim() };
      return { text: t.toLowerCase() };
    });
  }
  function matchesFilter(raw, terms) {
    if (!terms) return true;
    var low = raw.toLowerCase();
    for (var i = 0; i < terms.length; i++) {
      var t = terms[i];
      if (t.text) { if (low.indexOf(t.text) !== -1) return true; }
      else if (low.indexOf('"' + t.k.toLowerCase() + '":"' + t.v.toLowerCase() + '"') !== -1
            || low.indexOf('"' + t.k.toLowerCase() + '":' + t.v.toLowerCase()) !== -1) return true;
    }
    return false;
  }

  function filterToFlight(num) {
    // Clicking a flight surfaces ITS journal slice — widen the time range to "all" so a flight whose
    // events are older than the current window (e.g. a 26h-old park) still shows, not an empty box.
    state.fireFilter = "id=i" + num;
    state.fireRange = "all";
    var input = el("fire-filter");
    if (input) input.value = state.fireFilter;
    var range = el("fire-range");
    if (range) range.value = "all";
    updateFirehose();
    var fh = el("fire-lines");
    if (fh && fh.scrollIntoView) fh.scrollIntoView({ block: "nearest" });
  }

  // ============================ init: one-time listeners ============================
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;   // never steal keystrokes from the filter box
    if (e.key === "b" || e.key === "B") {
      state.boring = !state.boring;
      try { location.hash = state.boring ? "boring" : ""; } catch (err) { /* ignore */ }
      render();
    }
  });

  // A tapped plane emits the drawer-open event (Task 7 contract) — now it opens the real flight card.
  document.addEventListener("cc:drawer-open", function (e) {
    var d = e.detail || {};
    openDrawer(d.repo, d.num);
  });

  // The drawer's own verb buttons dispatch the same Task-6 endpoints (tap-where-you-read, §0.3).
  if (window.Drawer) window.Drawer.init({ onAction: handleAction });

  // "Since you last looked": when the operator walks away (the tab hides or the page unloads), record the
  // newest comms ts as the watermark — a dashboard-local write (never GitHub). On return, everything
  // that arrived while he was gone renders under the divider (design record §4, §7 north star:
  // "walk away → come back → discover what your field did without you").
  function markTowerSeen() {
    var s = state.snapshot;
    if (!s || !s.repos) return;
    var newest = null;
    s.repos.forEach(function (r) {
      (r.tower_log || []).forEach(function (row) {
        // Only COMMS rows set the watermark (issue #36): routine bookkeeping is hidden and never
        // counts as "new traffic", so a hidden relabel must not advance the mark past the newest
        // visible comms row (which would hide a later-arriving, earlier-stamped comms entry).
        if (row.tier === "routine") return;
        if (row.ts != null && (newest == null || row.ts > newest)) newest = row.ts;
      });
    });
    if (newest == null) return;
    if (s.tower_last_seen != null && newest <= s.tower_last_seen) return;   // nothing newer to record
    postJSON("/api/tower-seen", { ts: newest }).catch(function () { /* best-effort; a nicety */ });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") markTowerSeen();
  });
  window.addEventListener("pagehide", markTowerSeen);

  root.addEventListener("click", onShellClick);   // installed once; survives every shell re-render
  poll();
})();
