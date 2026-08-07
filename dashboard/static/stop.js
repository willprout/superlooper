/* The Stop/Start button's confirm dialog (issue #365) — the owner's deliberate off switch, and the
   way back on. The dashboard's FOURTH ops-verb overlay and a sibling of Restart: tapping it runs the
   ENGINE's `superlooper stop` / `superlooper start` (issue #239) on this machine through the
   server. Not a GitHub write, not a model call — a local mechanical verb, like Tidy and Restart.

   It is also the only button here that takes production DOWN, so the flow is deliberately blunt
   (tap-where-you-read, design §0.3):

     open → the dialog states EXACTLY what stopping does → the owner taps "Stop the loop" →
     POST /api/stop → the SERVER's summary is shown, whatever it says.

   ONE STEP, NOT TWO, and that is not a shortcut. Restart opens with a preflight because
   `request-restart` has a `--check` that writes nothing; `stop` has no equivalent and cannot,
   because it records the stop BEFORE anything can die — there is no honest "would this work?" to
   ask that has not already changed the answer. So the confirm below is the ONLY gate between a tap
   and the loop going off, which is why it states consequences rather than asking "are you sure?".

   THE RESULT IS THE SERVER'S. This file renders `summary.headline` / `summary.lines` /
   `summary.remedy` and decides nothing (design record B.1). It deliberately does NOT read `ok` to
   say whether the loop stopped: `ok` is the verb's verdict and it is wrong in both directions —
   a stop can succeed with the runner still finishing its tick, and a start can succeed having
   started nothing at all (the pane home cannot open a session window; automated placement is
   owner-ruled out). Those two facts live in lib/stopswitch.summarize, in tested Python, once.

   THE CONTROL FLIPS, so both directions are confirmed. The topbar button is Stop when the loop is
   running and Start when a stop is recorded, and the board re-renders every 2 seconds — so the
   label under the cursor can change. The confirm is what makes that safe: whatever the button said,
   this dialog names the verb again, in its own words, before anything happens.

   window.CCStop is a persistent overlay OUTSIDE #root (like the drawer/tidy/restart) so the 2s poll
   never touches it. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(id) { return document.getElementById(id); }

  // `slug` is the repo the dialog is showing and `mode` the direction it was opened in; the confirm
  // executes against THOSE, never a later re-render's, so a poll that flips the button underneath
  // can't leave the dialog showing Stop while the confirm starts the loop. `gen` supersedes an
  // in-flight execute when a newer open starts, so an out-of-order response is dropped.
  var node = null, slug = "", mode = "stop", busy = false, gen = 0;

  var VERB = {
    stop: {
      icon: "⏹", title: "STOP", path: "/api/stop", confirm: "Stop the loop",
      pending: "stopping the loop…",
      lead: function (s) { return "A loop is configured for <b>" + esc(s) + "</b>. Stopping will:"; },
      consequences: [
        "<b>record the stop as deliberate</b> — the watchdog stands down instead of " +
          "restarting it, so this reads as your decision, not an outage;",
        "hold the supervisor and <b>stop the runner</b> — it finishes the current tick first, " +
          "and nothing will start it again;",
        "leave <b>in-flight worker sessions untouched</b> — nothing merges while it is off;",
        "stay off until you press <b>Start</b> — bounded: the job is bootstrapped again at " +
          "your <b>next login</b>. This is an off switch for the night, not an uninstall."
      ]
    },
    start: {
      icon: "▶", title: "START", path: "/api/start", confirm: "Start the loop",
      pending: "starting the loop…",
      lead: function (s) { return "A stop is recorded for <b>" + esc(s) + "</b>. Starting will:"; },
      consequences: [
        "<b>withdraw the recorded stop</b> — but only once something is provably running, so a " +
          "start that fails leaves the loop exactly as off as it was;",
        "<b>start the runner again where it lives</b> — and say plainly if it could not;",
        "put the <b>watchdog back on watch</b>, so a real crash is caught again."
      ]
    }
  };

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
    node.id = "cc-stop";
    node.className = "cc-stop";
    node.innerHTML =
      '<div class="cc-stop-card">' +
        '<div class="cc-stop-head">' +
          '<span class="cc-stop-title" id="cc-stop-verb"></span>' +
          '<span class="cc-stop-sub" id="cc-stop-sub"></span>' +
          '<button class="cc-stop-x" data-stop-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-stop-body" id="cc-stop-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-stop-close]") || t.closest("[data-stop-cancel]")) {
        close();
        return;
      }
      if (t.closest("[data-stop-confirm]")) { runExecute(); return; }
    });
    document.addEventListener("keydown", function (e) {
      if (isOpen() && e.key === "Escape" && !busy) close();
    });
  }

  function isOpen() { return !!(node && node.classList.contains("open")); }

  // Opening the dialog asks the server NOTHING. There is no preflight to run and — since a stop
  // records itself before anything can die — an open that called the server would BE the stop.
  function open(repoSlug, direction) {
    if (!repoSlug) return;
    ensure();
    slug = repoSlug;
    mode = direction === "start" ? "start" : "stop";
    busy = false;
    gen += 1;
    var v = VERB[mode];
    el("cc-stop-verb").innerHTML = esc(v.icon) + " " + v.title + " <b>→ " + esc(slug) + "</b>";
    el("cc-stop-sub").innerHTML =
      "runs <code>superlooper " + mode + "</code> on this machine. no GitHub, no AI.";
    node.classList.add("open");
    node.classList.toggle("is-start", mode === "start");
    renderConfirm();
  }

  function close() { if (node) node.classList.remove("open"); }

  // The one and only trigger. Reached ONLY from the data-stop-confirm tap, and it targets the EXACT
  // repo and direction the dialog is showing.
  function runExecute() {
    if (busy || !slug) return;
    var v = VERB[mode], repo = slug, myGen = gen;
    busy = true;
    setBody('<div class="cc-stop-loading">' + esc(v.pending) + '</div>');
    postJSON(v.path, { repo: repo })
      .then(function (res) {
        busy = false;
        if (myGen !== gen || !isOpen()) return;    // a re-open superseded this / dialog closed
        var b = res.body || {};
        if (res.status !== 200 || !b.summary) { renderError(b.error); return; }
        renderSummary(b.summary);
      })
      .catch(function () {
        busy = false;
        if (myGen === gen && isOpen()) renderError("couldn’t reach the command center");
      });
  }

  // The consequence, in plain words — what a tap actually does, before it does it.
  function renderConfirm() {
    var v = VERB[mode];
    setBody(
      '<div class="cc-stop-lead">' + v.lead(slug) + '</div>' +
      '<ul class="cc-stop-consequence"><li>' + v.consequences.join('</li><li>') + '</li></ul>' +
      '<div class="cc-stop-actions">' +
        '<button class="btn ghost" data-stop-cancel>Cancel</button>' +
        '<button class="btn primary" data-stop-confirm>' + esc(v.confirm) + '</button>' +
      '</div>');
  }

  // The outcome, exactly as the server told it. Three levels, three colours, and NOTHING derived
  // here: `ok`/`warn`/`err` and every sentence come from lib/stopswitch.summarize, which is where
  // "did the loop actually stop?" is decided once and tested.
  function renderSummary(s) {
    var level = s.level === "err" ? "err" : (s.level === "warn" ? "warn" : "ok");
    var mark = level === "ok" ? "✓" : (level === "warn" ? "!" : "⚠");
    var lines = (s.lines || []).map(function (ln) {
      return '<li>' + esc(ln) + '</li>';
    }).join("");
    setBody(
      '<div class="cc-stop-result ' + level + '">' + mark + ' ' + esc(s.headline || "") + '</div>' +
      (lines ? '<ul class="cc-stop-detail">' + lines + '</ul>' : "") +
      (s.remedy ? '<div class="cc-stop-remedy"><code>' + esc(s.remedy) + '</code></div>' : "") +
      '<div class="cc-stop-actions"><button class="btn ghost" data-stop-close>Done</button></div>');
  }

  // A transport failure is never a silent success and never a silent failure either: the loop's
  // state is exactly what the owner came here to find out, so say that we cannot tell.
  function renderError(message) {
    setBody(
      '<div class="cc-stop-result err">⚠ ' + esc(message || "the command center did not answer") +
        '</div>' +
      '<div class="cc-stop-detail-note">the loop’s state is unchanged as far as this button ' +
        'can tell — check the board, or run <code>superlooper status</code>.</div>' +
      '<div class="cc-stop-actions"><button class="btn ghost" data-stop-close>Close</button></div>');
  }

  function setBody(html) {
    var b = el("cc-stop-body");
    if (b) b.innerHTML = html;
  }

  window.CCStop = { open: open, isOpen: isOpen };
})();
