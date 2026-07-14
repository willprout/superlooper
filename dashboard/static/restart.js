/* The Restart button's confirm dialog (issue #116) — the dashboard's SECOND ops-verb button and a
   sibling of Tidy: tapping Restart asks the LIVE runner (via the server → `superlooper
   request-restart`) to restart ITSELF in its own cmux tab. It is NOT a GitHub write — it drops a
   request the runner honors between ticks by re-exec'ing in place (fresh engine, cleared in-memory
   state). So the flow is deliberately two-step and confirm-gated (tap-where-you-read, design §0.3):

     open → POST /api/restart/check → dialog states EXACTLY what will happen (or, if NO runner is
     live, says so and shows the one-line manual start) → the owner taps "Restart the loop" →
     POST /api/restart → the honest result is shown.

   The bright line the whole design rides on: this NEVER spawns or places a cmux tab (owner ruling,
   2026-07-09). With no live runner the dialog cannot and must not resurrect one — it reports that
   plainly and points at the manual procedure; there is no confirm button, because there is nothing
   to ask. A command failure (missing CLI, crash) comes back as ok:false with an error string and is
   shown plainly — never a silent success.

   window.CCRestart is a persistent overlay OUTSIDE #root (like the drawer/tidy) so the 2s poll never
   touches it. Design B.1: this file computes NO semantics — the server already turned the CLI's JSON
   into a structured result; this only binds it to pixels. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // `listedRepo` is the repo the dialog is CURRENTLY showing (set only when its preflight renders);
  // the confirm executes against THAT, never the mutable `slug`, so a re-open can't leave the dialog
  // showing repo A while confirm restarts repo B. `gen` supersedes an in-flight preflight when a
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
    node.id = "cc-restart";
    node.className = "cc-restart";
    node.innerHTML =
      '<div class="cc-restart-card">' +
        '<div class="cc-restart-head">' +
          '<span class="cc-restart-title">\u{1F504} RESTART <b id="cc-restart-target"></b></span>' +
          '<span class="cc-restart-sub">asks the running loop to restart itself in its own tab — ' +
            'runs <code>superlooper request-restart</code> on this machine. no GitHub, no AI.</span>' +
          '<button class="cc-restart-x" data-restart-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-restart-body" id="cc-restart-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-restart-close]") || t.closest("[data-restart-cancel]")) {
        close();
        return;
      }
      if (t.closest("[data-restart-confirm]")) { runExecute(); return; }
      var retryEl = t.closest("[data-restart-retry]");
      if (retryEl) { loadPreflight(++gen, retryEl.getAttribute("data-restart-retry")); return; }
    });
    document.addEventListener("keydown", function (e) {
      if (isOpen() && e.key === "Escape" && !busy) close();
    });
  }

  function isOpen() { return !!(node && node.classList.contains("open")); }

  function open(repoSlug) {
    if (!repoSlug) return;
    ensure();
    slug = repoSlug;
    listedRepo = "";                 // nothing confirmed-ready yet for this open
    busy = false;
    el("cc-restart-target").textContent = "→ " + slug;
    node.classList.add("open");
    loadPreflight(++gen, repoSlug);  // ++gen supersedes any preflight still in flight from a prior open
  }

  function close() { if (node) node.classList.remove("open"); }

  // Step 1 — the preflight: is a live runner there to ask? (writes nothing). Each preflight carries
  // the generation of the open (or retry) that started it and the repo it is checking; a response
  // from a superseded open, or one that arrives after a close, is dropped.
  function loadPreflight(myGen, repo) {
    setBody('<div class="cc-restart-loading">checking for a running loop…</div>');
    postJSON("/api/restart/check", { repo: repo })
      .then(function (res) {
        if (myGen !== gen || !isOpen()) return;    // superseded / closed → ignore this stale reply
        var b = res.body || {};
        if (res.status !== 200) { renderError(b.error, false, repo); return; }
        if (b.running === true) { listedRepo = repo; renderConfirm(); return; }
        if (b.running === false) { renderNoRunner(b.manual); return; }
        renderError(b.error, false, repo);         // running unknown → the CLI couldn't answer
      })
      .catch(function () {
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", false, repo);
      });
  }

  // Step 2 — the confirmed request: ask the runner to restart. Reached ONLY from the
  // data-restart-confirm tap, and it targets the EXACT repo the dialog is showing (listedRepo).
  function runExecute() {
    if (busy || !listedRepo) return;
    var repo = listedRepo, myGen = gen;
    busy = true;
    setBody('<div class="cc-restart-loading">asking the loop to restart…</div>');
    postJSON("/api/restart", { repo: repo })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;    // a re-open superseded this / dialog closed
        var b = res.body || {};
        if (res.status !== 200) { renderError(b.error, true, repo); return; }
        if (b.requested === true) { renderDone(); return; }
        // The runner died between the preflight and the confirm → honest no-runner, never an error.
        if (b.running === false) { renderNoRunner(b.manual); return; }
        renderError(b.error, true, repo);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", true, repo);
      });
  }

  // A live runner is there: state EXACTLY what will happen, in plain words, then the confirm gate.
  function renderConfirm() {
    setBody(
      '<div class="cc-restart-lead">A loop is running for <b>' + esc(slug) + '</b>. Restarting will:</div>' +
      '<ul class="cc-restart-consequence">' +
        '<li>let it <b>finish the current tick</b>, then restart the loop in its own cmux tab;</li>' +
        '<li>reload the currently-installed engine and <b>clear in-memory state</b> ' +
          '(e.g. a stuck launch hold);</li>' +
        '<li>leave <b>in-flight worker sessions untouched</b> — nothing merges while it restarts.</li>' +
      '</ul>' +
      '<div class="cc-restart-actions">' +
        '<button class="btn ghost" data-restart-cancel>Cancel</button>' +
        '<button class="btn primary" data-restart-confirm>Restart the loop</button>' +
      '</div>');
  }

  // No live runner: the button cannot (and must not) resurrect one. Say so plainly and show the
  // one-line manual start — never a confirm button, because there is nothing to ask.
  function renderNoRunner(manual) {
    var line = manual || "open a cmux tab and run: superlooper run --repo <path>";
    setBody(
      '<div class="cc-restart-notice">No loop is running for <b>' + esc(slug) + '</b> — ' +
        'there is nothing to restart. Start it by hand in a visible cmux tab:</div>' +
      '<div class="cc-restart-manual"><code>' + esc(line) + '</code></div>' +
      '<div class="cc-restart-actions"><button class="btn ghost" data-restart-close>Done</button></div>');
  }

  function renderDone() {
    setBody(
      '<div class="cc-restart-result ok">✓ Restart requested — the loop will finish its ' +
        'current tick and restart itself in its own tab.</div>' +
      '<div class="cc-restart-actions"><button class="btn ghost" data-restart-close>Done</button></div>');
  }

  // A command failure is never a silent success: show the honest error. When the PREFLIGHT failed
  // (nothing was asked), offer a Retry; when the EXECUTE failed, just a dismiss.
  function renderError(message, wasExecute, repo) {
    var msg = message || (wasExecute ? "restart failed — nothing was asked" : "couldn’t check the loop");
    var retry = (!wasExecute && repo)
      ? '<button class="btn ghost" data-restart-retry="' + esc(repo) + '">Retry</button>' : "";
    setBody(
      '<div class="cc-restart-result err">⚠ ' + esc(msg) + '</div>' +
      '<div class="cc-restart-actions">' + retry +
        '<button class="btn ghost" data-restart-close>Close</button></div>');
  }

  function setBody(html) {
    var b = el("cc-restart-body");
    if (b) b.innerHTML = html;
  }

  window.CCRestart = { open: open, isOpen: isOpen };
})();
