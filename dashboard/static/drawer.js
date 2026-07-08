/* The flight-card drawer — ground truth one click from anywhere (Task 9 / design record §4).
 *
 * window.Drawer is a persistent overlay OUTSIDE #root (like the flag box + toast) so it survives the
 * 2s poll re-render while open; shell.js re-feeds it the fresh drawer object each poll via update().
 * It opens from any plane, row, or board line (tap-where-you-read, §0.3) and always shows the same
 * thing: title, circuit rail, the clearance checklist under its REAL check names with plain glosses,
 * issue/PR/branch links, memo history, the size-not-risk cargo chip, that flight's glossed journal
 * slice (each row expands to its raw line), and the go-around counter.
 *
 * Every semantic is the server's (lib/cards); this binds strings to pixels (design record B.1). The
 * verb buttons dispatch the same Task-6 endpoints through shell.js's handler (tap-where-you-read). */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  var node = null;            // the persistent overlay (appended to <body> once)
  var onAction = null;        // shell.js's verb dispatcher (tap-where-you-read)
  var cur = null;             // { repo, num } of the open drawer, or null
  var expanded = {};          // journal-row key -> true (drawer-local expand state)

  function ensure() {
    if (node) return;
    node = document.createElement("div");
    node.id = "cc-drawer";
    node.className = "cc-drawer";
    document.body.appendChild(node);
    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-drawer-close]")) { close(); return; }
      var tog = t.closest("[data-drawer-toggle]");
      if (tog) {
        var k = tog.getAttribute("data-drawer-toggle");
        expanded[k] = !expanded[k];
        if (cur && cur._last) render(cur._last);   // re-render in place, preserving state
        return;
      }
      var act = t.closest("[data-act]");
      if (act && onAction) { onAction(act); return; }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && cur) close();
    });
  }

  function railHTML(d) {
    // Costume rule 2 / joy-pass owner ruling: this ground-truth rail LEADS with the real developer
    // term (s.label) and demotes the airport metaphor (s.flavor) to small secondary text. The hover
    // carries the fuller plain detail (s.desc — for the gate step, the real check names).
    var steps = (d.circuit || []).map(function (s) {
      var cls = "rail-step" + (s.current ? " current" : "") + (s.done ? " done" : "");
      var flavor = s.flavor ? '<span class="rail-flavor">' + esc(s.flavor) + '</span>' : "";
      return '<span class="' + cls + '" title="' + esc(s.desc) + '">' +
        '<span class="rail-dev">' + esc(s.label) + '</span>' + flavor + '</span>';
    }).join('<span class="rail-arm"></span>');
    var off = d.off_path
      ? '<div class="drawer-offpath state-' + esc(d.off_path.state) + '">' + esc(d.off_path.plain) + '</div>'
      : "";
    return '<div class="drawer-section"><div class="drawer-label">CIRCUIT</div>' +
      '<div class="drawer-rail">' + steps + '</div>' + off + '</div>';
  }

  function clearanceHTML(d) {
    var rows = (d.clearance || []).map(function (c) {
      var mark = c.ok ? '<span class="ck ok">✓</span>' : '<span class="ck no">•</span>';
      return '<div class="clr-row' + (c.ok ? " ok" : "") + '" title="check name: ' + esc(c.key) + '">' +
        mark + '<span class="clr-label">' + esc(c.label) + '</span>' +
        '<span class="clr-gloss">' + esc(c.gloss) + '</span></div>';
    }).join("");
    return '<div class="drawer-section"><div class="drawer-label">CLEARANCE CHECKLIST</div>' +
      '<div class="drawer-clearance">' + rows + '</div></div>';
  }

  function linksHTML(d) {
    var l = d.links || {};
    var bits = [];
    if (l.issue) bits.push('<a class="drawer-link" href="' + esc(l.issue) + '" target="_blank" rel="noopener">issue ↗</a>');
    if (l.pr) bits.push('<a class="drawer-link" href="' + esc(l.pr) + '" target="_blank" rel="noopener">PR ↗</a>');
    if (l.branch) bits.push('<span class="drawer-branch" title="branch">' + esc(l.branch) + '</span>');
    return '<div class="drawer-links">' + bits.join("") + '</div>';
  }

  function factsHTML(d) {
    var cargo = d.cargo || {};
    var cargoChip = cargo.present
      ? '<span class="drawer-chip cargo" title="diff size — a neutral fact, never risk">' +
        esc(cargo.chip) + (cargo.files ? ' · ' + esc(cargo.files) + ' file' + (cargo.files === 1 ? "" : "s") : "") +
        '</span>'
      : '<span class="drawer-chip cargo empty">no cargo yet</span>';
    var go = d.go_arounds
      ? '<span class="drawer-chip go" title="conflict rebuilds survived">↻ ' + esc(d.go_arounds) +
        ' go-around' + (d.go_arounds === 1 ? "" : "s") + '</span>'
      : "";
    return '<div class="drawer-facts">' + cargoChip + go + '</div>';
  }

  function memosHTML(d) {
    if (!d.memos || !d.memos.length) return "";
    var items = d.memos.map(function (m) { return '<div class="drawer-memo">' + esc(m) + '</div>'; }).join("");
    return '<div class="drawer-section"><div class="drawer-label">MEMO HISTORY</div>' + items + '</div>';
  }

  function journalHTML(d) {
    var rows = (d.journal || []).slice().reverse().map(function (r, i) {   // newest first in the drawer
      var key = "j" + i + ":" + String(r.ts);
      var open = !!expanded[key];
      var radio = r.radio ? '<span class="radio">' + esc(r.radio) + '</span> ' : "";
      return '<div class="drawer-jrow kind-' + esc(r.kind || "event") + '">' +
        '<span class="t">' + esc(r.hhmm) + '</span>' +
        '<span class="msg">' + radio + esc(r.text) + '</span>' +
        '<span class="caret" data-drawer-toggle="' + esc(key) + '">' + (open ? "▾" : "▸") + '</span>' +
        (open ? '<div class="tower-raw">' + esc(r.raw) + '</div>' : "") +
      '</div>';
    }).join("");
    return '<div class="drawer-section"><div class="drawer-label">FLIGHT JOURNAL</div>' +
      '<div class="drawer-journal">' + (rows || '<div class="drawer-empty">no journal lines yet</div>') +
      '</div></div>';
  }

  function actionsHTML(d) {
    var da = ' data-repo="' + esc(d.repo) + '" data-num="' + esc(d.num) + '"';
    // The verb + label + whether Discuss is the default are the SERVER's (d.decision) — so a bounced
    // flight fires bounce-yes (its audit trail), and the conflict-cap Discuss-default (§8) holds here
    // exactly as on the card. Drop — the one destructive verb — stays on the card (its confirm lives
    // there). Discuss is always available.
    var dec = d.decision;
    var discuss = '<button class="btn-note link" data-act="discuss"' + da + '>Discuss →</button>';
    if (!dec) return '<div class="drawer-actions">' + discuss + '</div>';
    var approve = '<button class="btn ' + (dec.discuss_default ? "ghost" : "primary") +
      '" data-act="' + esc(dec.approve_act) + '"' + da + '>' + esc(dec.approve_label) + '</button>';
    var bits = dec.discuss_default
      ? ['<button class="btn primary" data-act="discuss"' + da + '>Discuss →</button>', approve]
      : [approve, discuss];
    return '<div class="drawer-actions">' + bits.join("") + '</div>';
  }

  function render(d) {
    cur._last = d;
    node.innerHTML =
      '<div class="drawer-panel">' +
        '<div class="drawer-head">' +
          '<div class="drawer-flight">' + esc(d.flight || ("SL-" + d.num)) + '</div>' +
          '<button class="drawer-x" data-drawer-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="drawer-title">' + esc(d.title || "") + '</div>' +
        '<div class="drawer-sub">' + esc(d.airline || "") + ' · ' + esc(d.repo || "") + '</div>' +
        factsHTML(d) +
        linksHTML(d) +
        railHTML(d) +
        clearanceHTML(d) +
        memosHTML(d) +
        journalHTML(d) +
        actionsHTML(d) +
      '</div>';
    node.classList.add("open");
  }

  function init(handlers) {
    ensure();
    onAction = handlers && handlers.onAction;
  }

  function open(d) {
    if (!d) return;
    ensure();
    expanded = {};                       // a fresh open starts with the journal collapsed
    cur = { repo: d.repo, num: d.num };
    render(d);
  }

  // shell.js calls this each poll so an open drawer tracks the live flight; a missing object leaves
  // the last view up (the flight may have just left the field — never blank out mid-read).
  function update(d) {
    if (!cur || !d) return;
    render(d);
  }

  function close() {
    cur = null;
    if (node) { node.classList.remove("open"); node.innerHTML = ""; }
  }

  window.Drawer = {
    init: init, open: open, update: update, close: close,
    isOpen: function () { return !!cur; },
    current: function () { return cur ? { repo: cur.repo, num: cur.num } : null; },
  };
})();
