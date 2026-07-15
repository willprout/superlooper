/* The stale-tower NOTAM (issue #136) — the dashboard's one honest word about ITSELF.

   Every other surface here reports the FIELD. This one reports the tower: the server has noticed
   that the code on disk has moved on since it booted, so controls the browser has already been
   handed may hit a router that never learned them. That is the live 2026-07-14 failure — the loop
   merged the janitor UI while the owner's day-old dashboard was up, his page rendered the new RAMP
   SWEEP button (static assets are re-read from disk every request), and the tap came back `no such
   action` beside a Retry that could never succeed.

   A NOTAM is the airport's own word for a posted advisory about a facility condition, and that is
   exactly the register this needs. It is deliberately NOT the red of RUNNER DOWN or a lost link:
   nothing is broken, no flight is affected, the field is flying fine — the tower is simply reading
   from yesterday's book. Amber, one line, posted on the wall. Dismissible, because §0.2 is no
   nagging and a strip you cannot dismiss is a nag; it stays gone for the session once waved off.

   Lives OUTSIDE #root (appended to <body>, like the toast and the ops dialogs) because #root is
   rebuilt wholesale every ~2s — a notice hung inside it would resurrect itself, and its dismissal
   would be undone, 30 times a minute. Living out here also means both views get it for free: the
   shell and boring mode both run updateChrome.

   Design B.1: this file computes NO skew semantics. `version.skew` is the server's decision (pure
   Python in lib/version.py, unit-tested there), `version.message` is its ready-made words, and
   `version.remedy` is the command it names — read from the snapshot, never hardcoded here, so the
   day the command is renamed the pixels cannot drift away from the truth. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // `dismissed` is per page-load, on purpose: a reload is exactly when a stale server hands you a
  // fresh page against an old router, so a reload is exactly when the notice has earned another say.
  var node = null, dismissed = false, shownFor = null;

  function ensure() {
    if (node) return;
    node = document.createElement("div");
    node.id = "cc-notam";
    node.className = "cc-notam";
    node.hidden = true;
    // One listener, attached once, on a node no re-render touches — no leak, no re-attach.
    node.addEventListener("click", function (e) {
      var t = e.target;
      if (t && t.closest && t.closest("[data-notam-dismiss]")) {
        dismissed = true;
        node.hidden = true;
      }
    });
    document.body.appendChild(node);
  }

  /* Bind the snapshot's `version` block. Called from shell.js's updateChrome on every poll, in BOTH
     the shell and boring mode. A snapshot with no version block (an older embedder, or a stamp that
     couldn't be taken) simply says nothing — silence is the honest answer to "I don't know". */
  function update(version) {
    var on = !!(version && version.skew) && !dismissed;
    if (!on) {
      if (node) node.hidden = true;
      return;
    }
    ensure();
    // Rebuild only when the words actually change — the poll runs every 2s and this strip is static
    // text; rewriting it 30 times a minute would be churn for nothing.
    var key = (version.message || "") + "|" + (version.remedy || "");
    if (key !== shownFor) {
      node.innerHTML =
        '<span class="cc-notam-tag">NOTAM</span>' +
        '<span class="cc-notam-body">' +
          '<b>STALE TOWER</b> — ' + esc(version.message || "") +
          (version.remedy ? ' restart it: <code>' + esc(version.remedy) + '</code>' : "") +
        '</span>' +
        '<button class="cc-notam-x" data-notam-dismiss title="dismiss">✕</button>';
      shownFor = key;
    }
    node.hidden = false;
  }

  window.CCNotam = { update: update };
})();
