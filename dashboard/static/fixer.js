/* The Deploy Fixer button's note box (issue #141) — the dashboard's FOURTH ops-verb button and the
   most consequential: tapping it starts ONE fresh interactive sl-debugger session in its own cmux
   tab, pointed at whatever the board is currently showing stuck.

   The button lives IN the trouble banner (tap-where-you-read, design §0.3): the one surface that
   appears for every condition it answers — runner down, ALERT, a park pile-up, a frozen session, a
   stranded flight, a spinning one, paused landings — and the one that follows you regardless of
   where the camera is (§4/§5). No trouble ⇒ no banner ⇒ no button.

     open → POST /api/fixer/check → the box shows what the board is reporting and offers an
     OPTIONAL note (skippable — an empty note deploys just fine) → the owner taps "Deploy fixer" →
     POST /api/fixer → the server composes the board readout and hands it, with the note, to
     `superlooper debug` → the honest result is shown.

   The bright line, and the owner's 2026-07-15 ruling in the UI: **no AI runs here.** This file makes
   no model call and the dashboard holds no seat. It collects a tap and (optionally) the owner's own
   words, and the server shells `superlooper debug` — the ENGINE's owner-tap launch verb (issue
   #144), which owns the id allocation, the single-flight lock, the brief and the launch handshake.
   The AI runs in the LAUNCHED session, in its own process, because a human tapped a button. His tap
   plus his note are his word, exactly as the Approve button records it.

   Two things this file deliberately does NOT do:
     * it never decides what is "unhealthy" — the server read the board and sent it (design B.1: the
       squint test — delete the art and the JSON is still a correct state diagram);
     * it never sends the context — a client that could name the trouble could lie about it, so the
       POST carries only the repo and the note, and the server reads the board fresh at tap time.

   window.CCFixer is a persistent overlay OUTSIDE #root (like the drawer/flag box/restart dialog) so
   the 2s poll never touches it — a textarea inside #root would lose focus, and the owner's
   half-typed note, on every tick. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // `listedRepo` is the repo the box is CURRENTLY showing (set only when its preflight renders); the
  // deploy launches against THAT, never the mutable `slug`, so a re-open can't leave the box showing
  // repo A while the tap deploys a fixer at repo B. `gen` supersedes an in-flight preflight when a
  // newer open starts, so an out-of-order response is dropped.
  var node = null, slug = "", listedRepo = "", busy = false, gen = 0;

  function postJSON(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        return { status: r.status, ok: r.ok, body: b || {} };
      });
    });
  }

  function ensure() {
    if (node) return;
    node = document.createElement("div");
    node.id = "cc-fixer";
    node.className = "cc-fixer";
    node.innerHTML =
      '<div class="cc-fixer-card">' +
        '<div class="cc-fixer-head">' +
          '<span class="cc-fixer-title">\u{1F527} DEPLOY FIXER <b id="cc-fixer-target"></b></span>' +
          '<span class="cc-fixer-sub">launches one interactive <code>sl-debugger</code> session in ' +
            'its own cmux tab, pointed at what the board is showing. the AI runs in THAT session — ' +
            'never in this dashboard.</span>' +
          '<button class="cc-fixer-x" data-fixer-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-fixer-body" id="cc-fixer-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-fixer-close]") || t.closest("[data-fixer-cancel]")) {
        close();
        return;
      }
      if (t.closest("[data-fixer-deploy]")) { runDeploy(); return; }
      var retryEl = t.closest("[data-fixer-retry]");
      if (retryEl) { loadPreflight(++gen, retryEl.getAttribute("data-fixer-retry")); return; }
    });
    document.addEventListener("keydown", function (e) {
      if (!isOpen() || busy) return;
      if (e.key === "Escape") { close(); return; }
      // ⌘/Ctrl+Enter deploys from inside the textarea — the same keyboard-first shape as the flag box.
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && el("cc-fixer-note")) runDeploy();
    });
  }

  function isOpen() { return !!(node && node.classList.contains("open")); }

  function open(repoSlug) {
    if (!repoSlug) return;
    ensure();
    slug = repoSlug;
    listedRepo = "";                 // nothing deploy-ready yet for this open
    busy = false;
    el("cc-fixer-target").textContent = "→ " + slug;
    node.classList.add("open");
    loadPreflight(++gen, repoSlug);  // ++gen supersedes any preflight still in flight from a prior open
  }

  function close() { if (node) node.classList.remove("open"); }

  // Step 1 — the preflight: is a fixer already on this patient, and what will ride into its prompt?
  // (writes nothing, launches nothing). Each preflight carries the generation of the open (or retry)
  // that started it; a response from a superseded open, or one arriving after a close, is dropped.
  function loadPreflight(myGen, repo) {
    setBody('<div class="cc-fixer-loading">reading the board…</div>');
    postJSON("/api/fixer/check", { repo: repo })
      .then(function (res) {
        if (myGen !== gen || !isOpen()) return;    // superseded / closed → ignore this stale reply
        var b = res.body || {};
        if (res.status !== 200 || b.ok !== true) { renderError(b.error, false, repo); return; }
        if (b.live === true) { renderLive(b.live_id); return; }
        listedRepo = repo;
        renderCompose(b.trouble);
      })
      .catch(function () {
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", false, repo);
      });
  }

  // Step 2 — the launch. Reached ONLY from the data-fixer-deploy tap, and it targets the EXACT repo
  // the box is showing (listedRepo). The note is read at tap time and sent verbatim; an empty one is
  // a first-class outcome, never a refusal.
  function runDeploy() {
    if (busy || !listedRepo) return;
    var noteEl = el("cc-fixer-note");
    var note = noteEl ? noteEl.value : "";
    var repo = listedRepo, myGen = gen;
    busy = true;
    setBody('<div class="cc-fixer-loading">deploying the fixer — opening its tab…</div>');
    postJSON("/api/fixer", { repo: repo, note: note })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;    // a re-open superseded this / box closed
        var b = res.body || {};
        if (res.status !== 200) { renderError(b.error, true, repo); return; }
        if (b.ok === true) { renderDone(b.id); return; }
        // A fixer started between the preflight and the tap → honest single-flight, never an error.
        if (b.live === true) { renderLive(b.live_id); return; }
        renderError(b.error, true, repo);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", true, repo);
      });
  }

  // The compose step: what the board is reporting (the server's own words — this file adds no
  // judgment), then the OPTIONAL note, then the deploy gate.
  function renderCompose(trouble) {
    var t = trouble || {};
    var items = t.items || [];
    var list;
    if (items.length) {
      list =
        '<div class="cc-fixer-lead">The board is reporting, worst first — this rides into the ' +
          'session’s prompt:</div>' +
        '<ul class="cc-fixer-trouble">' +
          items.map(function (i) {
            var num = i.num ? '<b>SL-' + esc(i.num) + '</b> — ' : "";
            return '<li class="kind-' + esc(i.kind) + '">' + num + esc(i.text || i.kind) + '</li>';
          }).join("") +
        '</ul>';
    } else {
      // An honest empty state: he may be tapping because he saw something the board didn't.
      list =
        '<div class="cc-fixer-lead">The board reads <b>clean</b> right now — no stale heartbeat, no ' +
          'ALERT, no freeze, nothing parked or frozen. Deploy anyway if you saw something it ' +
          'didn’t; your note below is what the session will go on.</div>';
    }
    setBody(
      list +
      '<label class="cc-fixer-label" for="cc-fixer-note">What needs fixing? ' +
        '<span class="cc-fixer-opt">optional — skip it and the board above is the whole brief</span>' +
      '</label>' +
      '<textarea id="cc-fixer-note" rows="3" spellcheck="true" ' +
        'placeholder="in your own words — what looks wrong, what you already tried…"></textarea>' +
      '<div class="cc-fixer-actions">' +
        '<button class="btn ghost" data-fixer-cancel>Cancel</button>' +
        '<button class="btn primary" data-fixer-deploy>\u{1F527} Deploy fixer</button>' +
      '</div>');
    var n = el("cc-fixer-note");
    if (n) n.focus();
  }

  // Single-flight, in words: a fixer is already on this patient. Never two debuggers — the second
  // would race the first's repairs. No deploy button, because there is nothing to ask.
  function renderLive(liveId) {
    setBody(
      '<div class="cc-fixer-notice">A fixer session' + (liveId ? ' (<b>' + esc(liveId) + '</b>)' : "") +
        ' is <b>already running</b> for ' + esc(slug) + ' — it has its own cmux tab. Two debuggers ' +
        'on one patient would race each other’s repairs, so this deploys nothing. Go talk to ' +
        'the one that’s already there.</div>' +
      '<div class="cc-fixer-actions"><button class="btn ghost" data-fixer-close>Done</button></div>');
  }

  function renderDone(id) {
    setBody(
      '<div class="cc-fixer-result ok">✓ Fixer ' + (id ? esc(id) + " " : "") + 'deployed — it’s ' +
        'in its own cmux tab with your note. Go say hello; it can answer you.</div>' +
      '<div class="cc-fixer-actions"><button class="btn ghost" data-fixer-close>Done</button></div>');
  }

  // A failed launch is never a silent success: show the honest error. When the PREFLIGHT failed
  // (nothing launched), offer a Retry; when the LAUNCH failed, just a dismiss.
  function renderError(message, wasLaunch, repo) {
    var msg = message || (wasLaunch ? "the launch failed — no session was started"
                                    : "couldn’t read the board");
    var retry = (!wasLaunch && repo)
      ? '<button class="btn ghost" data-fixer-retry="' + esc(repo) + '">Retry</button>' : "";
    setBody(
      '<div class="cc-fixer-result err">⚠ ' + esc(msg) + '</div>' +
      '<div class="cc-fixer-actions">' + retry +
        '<button class="btn ghost" data-fixer-close>Close</button></div>');
  }

  function setBody(html) {
    var b = el("cc-fixer-body");
    if (b) b.innerHTML = html;
  }

  window.CCFixer = { open: open, isOpen: isOpen };
})();
