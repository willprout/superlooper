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

   Held-back debris — an action a PREVIOUS sweep failed, which the CLI holds back — gets a second,
   deliberately narrower path (issue #131), because retrying is re-running a KNOWN-FAILING write:

     held row → "Retry" ARMS that one row → "Yes — <what it does>" → POST /api/janitor with that
     ONE key and retry:true → the server adds the CLI's --retry-refused → the honest result.

   It is never folded into the sweep confirm, never batched, and never automatic; the holdback itself
   is unchanged and still the CLI's (without that flag a held key comes back `held`, as before).

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
  // `heldArmed` is the ONE held-back key currently showing its confirm strip (issue #131). A retry
  // executes only the armed key, so a stale/forged confirm for another row can't run.
  var heldArmed = "";

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
      // the held-back row's own two-tap path — checked BEFORE the proposal rows, and entirely
      // separate from the sweep's confirm above.
      var heldGo = t.closest("[data-jan-held-go]");
      if (heldGo) { runRetry(heldGo.getAttribute("data-jan-held-go")); return; }
      var heldArm = t.closest("[data-jan-held-retry]");
      if (heldArm) { armRetry(heldArm.getAttribute("data-jan-held-retry")); return; }
      if (t.closest("[data-jan-held-cancel]")) { disarmRetry(); return; }
      var retryEl = t.closest("[data-jan-retry]");
      if (retryEl) { loadPropose(++gen, retryEl.getAttribute("data-jan-retry")); return; }
      var row = t.closest("[data-jan-key]");
      if (row) { toggle(row.getAttribute("data-jan-key")); return; }
    });
    document.addEventListener("keydown", function (e) {
      if (!isOpen()) return;
      if (e.key === "Escape" && !busy) { close(); return; }
      // A proposal row is role="checkbox" tabindex="0"; a <div> doesn't synthesize a click on
      // Space/Enter, so wire it explicitly — a keyboard-only owner must be able to arm debris too.
      if (e.key === " " || e.key === "Enter" || e.key === "Spacebar") {
        var t = e.target;
        var row = t && t.closest ? t.closest("[data-jan-key]") : null;
        if (row) { e.preventDefault(); toggle(row.getAttribute("data-jan-key")); }
      }
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
    heldArmed = "";                  // …and with no held-back retry armed
    el("cc-jan-target").textContent = "→ " + slug;
    node.classList.add("open");
    loadPropose(++gen, repoSlug);    // ++gen supersedes any propose still in flight from a prior open
  }

  function close() { heldArmed = ""; if (node) node.classList.remove("open"); }

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
        if (res.status !== 200 || !b.ok) { renderError(b.error, false, repo, b.skew); return; }
        listedRepo = repo;                         // THIS repo's proposals are the ones now on screen
        selected = {};                             // a fresh listing selects nothing
        heldArmed = "";                            // …and arms no held-back retry
        renderProposals(b.groups || [], b.held || [], b.held_items);
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
        if (res.status !== 200 || !b.ok) { renderError(b.error, true, repo, b.skew); return; }
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
      row.setAttribute("aria-checked", selected[key] ? "true" : "false");   // keep a11y state honest
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
  function renderProposals(groups, held, heldItems) {
    if (!groups.length) {
      setBody(
        '<div class="cc-jan-empty">Apron’s clear — no GitHub debris to sweep ✓</div>' +
        heldHTML(held, heldItems) +
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
      heldHTML(held, heldItems) +
      '<div class="cc-jan-actions">' +
        '<button class="btn ghost" data-jan-cancel>Cancel</button>' +
        '<button class="btn primary" id="cc-jan-confirm" data-jan-confirm disabled>' +
          'Sweep 0 selected</button>' +
      '</div>');
    updateFooter();
  }

  // Held-back actions: a prior sweep's failure the CLI is holding back (janitor_refused.json) — the
  // server reports the raw keys in `held` and, when it speaks the retry contract (issue #131), the
  // same keys WITH their consequence in `held_items`. They are never part of the sweep selection and
  // are never retried on their own: each row carries its own Retry, and that Retry only arms a
  // second, consequence-stating confirm (see armRetry/runRetry).
  //
  // No `held_items` means the RUNNING server is older than this bundle (the issue #136 skew shape)
  // and has no retry to give — so the rows fall back to key-only, with the terminal instruction, and
  // no button that would silently do nothing.
  function heldHTML(held, items) {
    var keys = (held && held.length) ? held : [];
    if (!keys.length) return "";
    // Array.isArray, not truthiness: a skewed/garbled body must fail closed to the terminal note
    // rather than into a half-built retry control.
    var canRetry = Array.isArray(items) && items.length > 0;
    var rows = canRetry
      ? items.map(function (it) {
          return '<div class="cc-jan-held-row" data-jan-held-row="' + esc(it.key) + '">' +
            '<span class="cc-jan-held-what">' + esc(it.what) + '</span>' +
            '<span class="cc-jan-held-act" data-jan-held-act="' + esc(it.key) + '">' +
              retryArmHTML(it.key) + '</span>' +
          '</div>';
        }).join("")
      : keys.map(function (k) {
          return '<div class="cc-jan-held-row">' + esc(k) + '</div>';
        }).join("");
    var note = canRetry
      ? 'Never retried on its own. <b>Retry</b> re-runs the one GitHub write that already failed — ' +
        'its own tap, its own confirm.'
      : 'Not retried automatically. Re-propose from the terminal with ' +
        '<code>superlooper janitor --retry-refused</code>.';
    return '<div class="cc-jan-held">' +
      '<div class="cc-jan-held-head">Held back — failed a previous sweep (' +
        (canRetry ? items.length : keys.length) + ')</div>' +
      rows +
      '<div class="cc-jan-held-note">' + note + '</div>' +
    '</div>';
  }

  // The two faces of one held row's action cell: the resting Retry, and — once armed — the explicit
  // confirm that states what re-running the failed write would do.
  function retryArmHTML(key) {
    return '<button class="btn ghost cc-jan-held-retry" data-jan-held-retry="' + esc(key) + '">' +
      'Retry</button>';
  }

  function retryConfirmHTML(key, what) {
    return '<span class="cc-jan-held-ask">re-run this failed write?</span>' +
      '<button class="btn cc-jan-held-go" data-jan-held-go="' + esc(key) + '" ' +
        'title="' + esc(what) + '">Yes — ' + esc(what) + '</button>' +
      '<button class="btn ghost cc-jan-held-cancel" data-jan-held-cancel>Cancel</button>';
  }

  // Tap 1 of the retry: arm THIS row (any other armed row goes back to rest — one at a time, so the
  // owner is always confirming exactly the write in front of him).
  function armRetry(key) {
    if (!key || busy) return;
    if (heldArmed && heldArmed !== key) disarmRetry();
    heldArmed = key;
    var cell = heldCell(key);
    if (cell) cell.innerHTML = retryConfirmHTML(key, heldWhat(key));
    // the armed row gives the question its own line — a confirm crammed beside a long branch name
    // is a confirm the owner skims past.
    var row = heldRow(key);
    if (row) row.classList.add("is-armed");
  }

  function disarmRetry() {
    var key = heldArmed;
    heldArmed = "";
    var cell = key ? heldCell(key) : null;
    if (cell) cell.innerHTML = retryArmHTML(key);
    var row = key ? heldRow(key) : null;
    if (row) row.classList.remove("is-armed");
  }

  function heldRow(key) {
    return node ? node.querySelector('[data-jan-held-row="' + cssEsc(key) + '"]') : null;
  }

  function heldCell(key) {
    return node ? node.querySelector('[data-jan-held-act="' + cssEsc(key) + '"]') : null;
  }

  // The server's own words for what this key does — read back off the row it rendered, never
  // re-derived here (design B.1: the JS computes no janitor semantics).
  function heldWhat(key) {
    var row = heldRow(key);
    var what = row ? row.querySelector(".cc-jan-held-what") : null;
    return (what && what.textContent) || key;
  }

  // Tap 2 of the retry — the ONLY place a held-back action is re-run (issue #131). It runs ALONE:
  // exactly the one armed key, with the explicit flag the server threads to the CLI's
  // --retry-refused. The sweep's confirm never reaches here, and this never sweeps anything else.
  function runRetry(key) {
    if (busy || !listedRepo || !key || key !== heldArmed) return;
    var repo = listedRepo, myGen = gen;
    busy = true;
    heldArmed = "";
    setBody('<div class="cc-jan-loading">retrying ' + esc(key) + '…</div>');
    postJSON("/api/janitor", { repo: repo, keys: [key], retry: true })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;   // a re-open superseded this / dialog closed
        var b = res.body || {};
        if (res.status !== 200 || !b.ok) { renderError(b.error, true, repo, b.skew); return; }
        renderResults(b);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center", true, repo);
      });
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
    // Counts come from the trusted local CLI as integers, but coerce anyway — the escaping
    // discipline says no field reaches innerHTML without being made safe first.
    var swept = Number(b.executed) || 0;
    var nFailed = Number(b.failed) || 0, nSkipped = Number(b.skipped) || 0, nHeld = Number(b.held) || 0;
    var parts = ['<b>' + swept + '</b> swept'];
    if (nFailed) parts.push('<b class="cc-jan-bad">' + nFailed + '</b> failed');
    if (nSkipped) parts.push(nSkipped + ' skipped');
    if (nHeld) parts.push(nHeld + ' held');
    var cls = nFailed ? "err" : "ok";
    var glyph = nFailed ? "⚠" : "✓";
    setBody(
      '<div class="cc-jan-result ' + cls + '">' + glyph + ' ' + parts.join(' · ') + '.</div>' +
      '<div class="cc-jan-list cc-jan-results">' + rows + '</div>' +
      '<div class="cc-jan-actions"><button class="btn ghost" data-jan-close>Done</button></div>');
  }

  // A command failure is never a silent success: show the honest error. A failed PROPOSE (nothing
  // ran) offers a Retry that re-lists the same repo under a fresh generation; a failed EXECUTE offers
  // only dismiss (re-running could re-sweep, so the owner reopens deliberately).
  //
  // `skew` (issue #136) is the server telling us it is running an older build than the code on disk
  // and has no route for this button — which is exactly how this dialog failed live on 2026-07-14,
  // the day the loop merged RAMP SWEEP under the owner's day-old dashboard. Then the Retry must go:
  // it would re-ask the SAME old server the SAME question it has no route for, forever. The server's
  // message already names the remedy (restart the dashboard), and that is a thing only the owner can
  // do — so the honest offer here is Close, not a button that cannot work.
  function renderError(message, wasExecute, repo, skew) {
    var msg = message || (wasExecute ? "the sweep failed — nothing was cleared"
                                     : "couldn’t read the apron");
    var retry = (!wasExecute && repo && !skew)
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
