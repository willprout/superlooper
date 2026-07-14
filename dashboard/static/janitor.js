/* The Janitor button's sweep dialog (issue #121) — the dashboard's SECOND ops-verb button, same
   LOCAL COMMAND class as Tidy: tapping it runs `superlooper janitor` (via the server) to clear
   GitHub-side debris off the apron — stale merged/superseded sl/* branches, open `superseded` PRs,
   and aged parked/needs-owner issues. Owner ruling 2026-07-13: full CLI parity without leaving the
   dashboard. It writes GitHub, so the flow is deliberately two-step and consent-gated:

     open → POST /api/janitor/propose → dialog GROUPS the proposals by kind → the owner taps EXACTLY
     the ones he wants → "Sweep N" confirm → POST /api/janitor with that subset of keys → the
     honest per-item result is shown.

   Nothing sweeps that the owner did not tap: the execute POST lives only in runExecute(), reached
   solely from the data-jan-confirm control, and it sends EXACTLY the selected keys. There is no
   sweep-all — every item is an individual tap (per-kind consent). A command failure (nonzero exit,
   missing binary) comes back as ok:false with an error string and is shown plainly.

   window.CCJanitor is a persistent overlay OUTSIDE #root (like Tidy/the drawer) so the 2s poll never
   touches it. Design B.1: this file computes NO janitor semantics — the server (driving the CLI's
   pure lib/janitor) already selected and grouped the proposals; this only binds them to pixels and
   tracks which the owner tapped. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // `listedRepo` is the repo whose proposals are CURRENTLY shown (set only when a propose for it
  // renders); execute runs against THAT, never the mutable `slug`, so a re-open can't leave the
  // dialog showing repo A while confirm sweeps repo B. `gen` supersedes an in-flight propose when a
  // newer open/retry starts. `selected` is the set of proposal keys the owner has tapped.
  var node = null, slug = "", listedRepo = "", busy = false, gen = 0;
  var selected = null;

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
    node.id = "cc-janitor";
    node.className = "cc-janitor";
    node.innerHTML =
      '<div class="cc-jan-card">' +
        '<div class="cc-jan-head">' +
          '<span class="cc-jan-title"><span class="cc-jan-sprite" aria-hidden="true"></span> RAMP SWEEP <b id="cc-jan-target"></b></span>' +
          '<span class="cc-jan-sub">clears GitHub-side debris — stale branches, superseded PRs, ' +
            'aged parked issues. runs <code>superlooper janitor</code> on this machine. no AI. ' +
            'nothing sweeps until you tap it.</span>' +
          '<button class="cc-jan-x" data-jan-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-jan-body" id="cc-jan-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-jan-close]") || t.closest("[data-jan-cancel]")) {
        close();
        return;
      }
      if (t.closest("[data-jan-confirm]")) { runExecute(); return; }
      var retryEl = t.closest("[data-jan-retry]");
      if (retryEl) { loadPropose(++gen, retryEl.getAttribute("data-jan-retry")); return; }
      var row = t.closest("[data-jan-key]");
      if (row) { toggle(row.getAttribute("data-jan-key")); return; }
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
    selected = {};                   // a fresh open starts with NOTHING selected — no sweep-all
    el("cc-jan-target").textContent = "→ " + slug;
    node.classList.add("open");
    loadPropose(++gen, repoSlug);    // ++gen supersedes any propose still in flight from a prior open
  }

  function close() { if (node) node.classList.remove("open"); }

  // Step 1 — propose: list what the sweep WOULD do (changes NOTHING). Each propose carries the
  // generation of the open (or retry) that started it and the repo it is listing; a response from a
  // superseded open, or one that arrives after a close, is dropped — so the dialog can never render
  // one repo's proposals under another's.
  function loadPropose(myGen, repo) {
    setBody('<div class="cc-jan-loading">walking the apron for debris…</div>');
    postJSON("/api/janitor/propose", { repo: repo })
      .then(function (res) {
        if (myGen !== gen || !isOpen()) return;    // superseded / closed → ignore this stale reply
        var b = res.body || {};
        if (res.status !== 200 || !b.ok) { renderError(b.error, false, repo); return; }
        listedRepo = repo;                         // THIS repo's proposals are the ones now on screen
        selected = {};                             // a fresh listing selects nothing
        renderProposals(b.groups || [], b.held || []);
      })
      .catch(function () {
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", false, repo);
      });
  }

  // Step 2 — the confirmed execute: sweep EXACTLY the tapped subset. Reached ONLY from the
  // data-jan-confirm tap, against the EXACT repo whose proposals are listed (listedRepo), and it
  // sends only the selected keys — never a mutable "current", never an item the owner didn't tap.
  function runExecute() {
    var keys = selectedKeys();
    if (busy || !listedRepo || !keys.length) return;
    var repo = listedRepo, myGen = gen;
    busy = true;
    setBody('<div class="cc-jan-loading">sweeping ' + keys.length + ' off the apron…</div>');
    postJSON("/api/janitor", { repo: repo, keys: keys })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;    // a re-open superseded this / dialog closed
        var b = res.body || {};
        if (res.status !== 200 || !b.ok) { renderError(b.error, true, repo); return; }
        renderResults(b);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", true, repo);
      });
  }

  function selectedKeys() {
    return Object.keys(selected).filter(function (k) { return selected[k]; });
  }

  // Toggle one proposal's selection and reflect it live — the row's armed state and the footer's
  // count/consequence — without a full re-render (so a tap feels instant).
  function toggle(key) {
    if (!key) return;
    selected[key] = !selected[key];
    var row = node.querySelector('[data-jan-key="' + cssEsc(key) + '"]');
    if (row) {
      row.classList.toggle("is-selected", !!selected[key]);
      var box = row.querySelector(".cc-jan-check");
      if (box) box.textContent = selected[key] ? "✓" : "";
    }
    updateFooter();
  }

  function cssEsc(s) {
    return String(s).replace(/["\\]/g, "\\$&");
  }

  // The proposal list, grouped by kind, each item a tap target. Nothing is pre-selected. When there
  // is no debris, an honest all-clear — and no confirm button, because there is nothing to sweep.
  function renderProposals(groups, held) {
    if (!groups.length) {
      setBody(
        '<div class="cc-jan-empty">Apron’s clear — no GitHub debris to sweep ✓</div>' +
        heldHTML(held) +
        '<div class="cc-jan-actions"><button class="btn ghost" data-jan-close>Done</button></div>');
      return;
    }
    var body = groups.map(function (g) {
      var items = (g.items || []).map(function (it) {
        return '<div class="cc-jan-row" data-jan-key="' + esc(it.key) + '" role="checkbox" ' +
                 'aria-checked="false" tabindex="0">' +
          '<span class="cc-jan-check" aria-hidden="true"></span>' +
          '<span class="cc-jan-what">' + esc(it.what) + '</span>' +
          '<span class="cc-jan-why">' + esc(it.why) + '</span>' +
        '</div>';
      }).join("");
      return '<div class="cc-jan-group cc-jan-' + esc(g.kind) + '">' +
        '<div class="cc-jan-group-head"><span class="cc-jan-kind" aria-hidden="true"></span>' +
          esc(g.label) + ' <span class="cc-jan-count">' + (g.items || []).length + '</span></div>' +
        items +
      '</div>';
    }).join("");
    setBody(
      '<div class="cc-jan-lead">Tap the debris to clear, then sweep. Deleting a branch or closing ' +
        'a PR/issue here does the same GitHub write the terminal would — it can’t be undone from the ' +
        'dashboard.</div>' +
      '<div class="cc-jan-list">' + body + '</div>' +
      heldHTML(held) +
      '<div class="cc-jan-actions">' +
        '<button class="btn ghost" data-jan-cancel>Cancel</button>' +
        '<button class="btn primary" id="cc-jan-confirm" data-jan-confirm disabled>' +
          'Sweep 0 selected</button>' +
      '</div>');
    updateFooter();
  }

  // Held-back actions: a prior sweep's failure the CLI is holding back (janitor_refused.json) — the
  // server reports them in `held`. Surface them once, plainly, and DON'T offer them as tap targets:
  // they are not silently retried (the terminal's --retry-refused re-proposes them deliberately).
  function heldHTML(held) {
    if (!held || !held.length) return "";
    var rows = held.map(function (k) {
      return '<div class="cc-jan-held-row">' + esc(k) + '</div>';
    }).join("");
    return '<div class="cc-jan-held">' +
      '<div class="cc-jan-held-head">Held back — failed a previous sweep (' + held.length + ')</div>' +
      rows +
      '<div class="cc-jan-held-note">Not retried automatically. Re-propose from the terminal with ' +
        '<code>superlooper janitor --retry-refused</code>.</div>' +
    '</div>';
  }

  // Update the confirm button's enabled state and its consequence-stating label from the current
  // selection. The count and per-kind breakdown are read from the selected KEYS (branch:/pr:/issue:
  // prefixes the server minted) — no janitor logic, just reading the identities it returned.
  function updateFooter() {
    var btn = el("cc-jan-confirm");
    if (!btn) return;
    var keys = selectedKeys();
    btn.disabled = keys.length === 0;
    btn.textContent = keys.length === 0 ? "Sweep 0 selected"
      : "Sweep " + keys.length + " — " + breakdown(keys);
  }

  function breakdown(keys) {
    var n = { branch: 0, pr: 0, issue: 0 };
    keys.forEach(function (k) {
      var kind = String(k).split(":", 1)[0];
      if (n[kind] != null) n[kind] += 1;
    });
    var parts = [];
    if (n.branch) parts.push(n.branch + (n.branch === 1 ? " branch" : " branches"));
    if (n.pr) parts.push(n.pr + (n.pr === 1 ? " PR" : " PRs"));
    if (n.issue) parts.push(n.issue + (n.issue === 1 ? " issue" : " issues"));
    return parts.join(", ");
  }

  // The honest per-item outcome after a sweep: ok / fail / skipped / held. A failed action is never
  // hidden — it shows with its reason, so a partial sweep is never mistaken for a clean one.
  function renderResults(b) {
    var results = b.results || [];
    var rows = results.map(function (r) {
      var oc = String(r.outcome || "");
      var glyph = oc === "ok" ? "✓" : oc === "fail" ? "✗"
        : oc === "held" ? "⏸" : "–";
      var reason = r.reason ? '<span class="cc-jan-res-reason">' + esc(r.reason) + '</span>' : "";
      return '<div class="cc-jan-res-row cc-jan-res-' + esc(oc) + '">' +
        '<span class="cc-jan-res-glyph" aria-hidden="true">' + glyph + '</span>' +
        '<span class="cc-jan-res-key">' + esc(r.key) + '</span>' + reason +
      '</div>';
    }).join("");
    var swept = b.executed || 0;
    var parts = ['<b>' + swept + '</b> swept'];
    if (b.failed) parts.push('<b class="cc-jan-bad">' + b.failed + '</b> failed');
    if (b.skipped) parts.push(b.skipped + ' skipped');
    if (b.held) parts.push(b.held + ' held');
    var cls = b.failed ? "err" : "ok";
    var glyph = b.failed ? "⚠" : "✓";
    setBody(
      '<div class="cc-jan-result ' + cls + '">' + glyph + ' ' + parts.join(' · ') + '.</div>' +
      '<div class="cc-jan-list cc-jan-results">' + rows + '</div>' +
      '<div class="cc-jan-actions"><button class="btn ghost" data-jan-close>Done</button></div>');
  }

  // A command failure is never a silent success: show the honest error. A failed PROPOSE (nothing
  // ran) offers a Retry that re-lists the same repo under a fresh generation; a failed EXECUTE offers
  // only dismiss (re-running could re-sweep, so the owner reopens deliberately).
  function renderError(message, wasExecute, repo) {
    var msg = message || (wasExecute ? "the sweep failed — nothing was cleared"
                                     : "couldn’t read the apron");
    var retry = (!wasExecute && repo)
      ? '<button class="btn ghost" data-jan-retry="' + esc(repo) + '">Retry</button>' : "";
    setBody(
      '<div class="cc-jan-result err">⚠ ' + esc(msg) + '</div>' +
      '<div class="cc-jan-actions">' + retry +
        '<button class="btn ghost" data-jan-close>Close</button></div>');
  }

  function setBody(html) {
    var b = el("cc-jan-body");
    if (b) b.innerHTML = html;
  }

  window.CCJanitor = { open: open, isOpen: isOpen };
})();
