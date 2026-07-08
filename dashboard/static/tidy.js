/* The Tidy button's confirm dialog (issue #41) — the dashboard's FIRST ops-verb button and its
   SECOND button class: tapping Tidy runs the local `superlooper tidy` CLI (via the server) to close
   the cmux windows of FINISHED sessions. It is NOT a GitHub write — it executes a machine on this
   box — so the flow is deliberately two-step and confirm-gated (tap-where-you-read, design §0.3):

     open → POST /api/tidy/dry-run → dialog lists EXACTLY what dry-run returned →
     the owner taps "Close N windows" → POST /api/tidy → the honest result is shown.

   Nothing closes without that in-UI confirm: the execute POST lives only in runExecute(), reached
   solely from the data-tidy-confirm control. A command failure (nonzero exit, missing binary) comes
   back as ok:false with an error string and is shown plainly — never a silent success, never a
   clean "nothing to tidy" over a failed command.

   window.CCTidy is a persistent overlay OUTSIDE #root (like the drawer/digest) so the 2s poll never
   touches it. Design B.1: this file computes NO semantics — the server already turned the CLI's text
   into structured window rows (lib/tidy); this only binds those rows to pixels. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // `listedRepo` is the repo whose windows are CURRENTLY shown (set only when a dry-run for it
  // renders its list); the confirm executes against THAT, never the mutable `slug`, so a re-open
  // can't leave the dialog showing repo A while confirm closes repo B. `gen` supersedes an
  // in-flight dry-run when a newer open/retry starts, so an out-of-order response is dropped.
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
    node.id = "cc-tidy";
    node.className = "cc-tidy";
    node.innerHTML =
      '<div class="cc-tidy-card">' +
        '<div class="cc-tidy-head">' +
          '<span class="cc-tidy-title">\u{1F9F9} TIDY <b id="cc-tidy-target"></b></span>' +
          '<span class="cc-tidy-sub">closes the windows of FINISHED sessions — runs ' +
            '<code>superlooper tidy</code> on this machine. no GitHub, no AI.</span>' +
          '<button class="cc-tidy-x" data-tidy-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-tidy-body" id="cc-tidy-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-tidy-close]") || t.closest("[data-tidy-cancel]")) {
        close();
        return;
      }
      if (t.closest("[data-tidy-confirm]")) { runExecute(); return; }
      var retryEl = t.closest("[data-tidy-retry]");
      if (retryEl) { loadDryRun(++gen, retryEl.getAttribute("data-tidy-retry")); return; }
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
    listedRepo = "";                 // nothing is listed yet for this open
    busy = false;
    el("cc-tidy-target").textContent = "→ " + slug;
    node.classList.add("open");
    loadDryRun(++gen, repoSlug);     // ++gen supersedes any dry-run still in flight from a prior open
  }

  function close() { if (node) node.classList.remove("open"); }

  // Step 1 — the dry-run: list what WOULD close (closes nothing). Each dry-run carries the
  // generation of the open (or retry) that started it and the repo it is listing; a response from a
  // superseded open, or one that arrives after a close, is dropped — so the dialog can never render
  // one repo's windows under another's, even on an out-of-order response.
  function loadDryRun(myGen, repo) {
    setBody('<div class="cc-tidy-loading">checking for finished session windows…</div>');
    postJSON("/api/tidy/dry-run", { repo: repo })
      .then(function (res) {
        if (myGen !== gen || !isOpen()) return;    // superseded / closed → ignore this stale reply
        var b = res.body || {};
        if (res.status !== 200 || !b.ok) { renderError(b.error, false, repo); return; }
        listedRepo = repo;                         // THIS repo's windows are the ones now on screen
        renderList(b.windows || []);
      })
      .catch(function () {
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", false, repo);
      });
  }

  // Step 2 — the confirmed execute: CLOSE the windows. Reached ONLY from the data-tidy-confirm tap,
  // and it closes the EXACT repo whose windows are listed (listedRepo) — never a mutable "current"
  // a re-open could have moved — so confirm always closes precisely what the dialog showed.
  function runExecute() {
    if (busy || !listedRepo) return;
    var repo = listedRepo, myGen = gen;
    busy = true;
    setBody('<div class="cc-tidy-loading">closing the finished session windows…</div>');
    postJSON("/api/tidy", { repo: repo })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;    // a re-open superseded this / dialog closed
        var b = res.body || {};
        if (res.status !== 200 || !b.ok) { renderError(b.error, true, repo); return; }
        renderDone(b.closed || 0);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", true, repo);
      });
  }

  // The list of finished windows + the confirm gate. When none are finished, an honest all-clear —
  // and no confirm button, because there is nothing to execute.
  function renderList(windows) {
    if (!windows.length) {
      setBody(
        '<div class="cc-tidy-empty">No finished session windows to close — the apron is clear ✓</div>' +
        '<div class="cc-tidy-actions"><button class="btn ghost" data-tidy-close>Done</button></div>');
      return;
    }
    var rows = windows.map(function (w) {
      var fn = flightLabel(w.id);
      var surface = w.surface ? esc(w.surface) : "<span class=\"cc-tidy-nosurface\">(no surface)</span>";
      return '<div class="cc-tidy-row">' +
        '<span class="cc-tidy-flight">' + esc(fn) + '</span>' +
        '<span class="cc-tidy-status">' + esc(w.status) + '</span>' +
        '<span class="cc-tidy-surface">' + surface + '</span>' +
      '</div>';
    }).join("");
    var n = windows.length;
    var plural = n === 1 ? "window" : "windows";
    setBody(
      '<div class="cc-tidy-lead">tidy will close <b>' + n + '</b> finished session ' + plural +
        '. This closes only their terminal windows — it can’t touch a session still building.</div>' +
      '<div class="cc-tidy-list">' + rows + '</div>' +
      '<div class="cc-tidy-actions">' +
        '<button class="btn ghost" data-tidy-cancel>Cancel</button>' +
        '<button class="btn primary" data-tidy-confirm>Close ' + n + ' ' + plural + '</button>' +
      '</div>');
  }

  function renderDone(closed) {
    var plural = closed === 1 ? "window" : "windows";
    setBody(
      '<div class="cc-tidy-result ok">✓ Closed <b>' + closed + '</b> finished session ' + plural + '.</div>' +
      '<div class="cc-tidy-actions"><button class="btn ghost" data-tidy-close>Done</button></div>');
  }

  // A command failure is never a silent success: show the honest error. When it was the DRY-RUN
  // that failed (nothing ran), offer a Retry — a transient CLI hiccup shouldn't force reopening;
  // when the EXECUTE failed, just a dismiss (re-running could re-close, so the owner reopens
  // deliberately). The Retry re-lists the SAME repo that failed, under a fresh generation.
  function renderError(message, wasExecute, repo) {
    var msg = message || (wasExecute ? "tidy failed — nothing was closed" : "couldn’t list the windows");
    var retry = (!wasExecute && repo)
      ? '<button class="btn ghost" data-tidy-retry="' + esc(repo) + '">Retry</button>' : "";
    setBody(
      '<div class="cc-tidy-result err">⚠ ' + esc(msg) + '</div>' +
      '<div class="cc-tidy-actions">' + retry +
        '<button class="btn ghost" data-tidy-close>Close</button></div>');
  }

  // i23 -> SL-23 (the flight number everywhere else on the field); a non-iN id shows as-is.
  function flightLabel(id) {
    var m = /^i(\d+)$/.exec(String(id || ""));
    return m ? "SL-" + m[1] : String(id || "?");
  }

  function setBody(html) {
    var b = el("cc-tidy-body");
    if (b) b.innerHTML = html;
  }

  window.CCTidy = { open: open, isOpen: isOpen };
})();
