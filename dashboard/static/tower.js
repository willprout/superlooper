/* The tower log — a comms feed (Task 9 / design record §4, §7).
 *
 * window.Tower renders the tower panel from the server's already-glossed rows. Every semantic is the
 * server's (lib/tower): each row arrives with a plain `text` sentence, an optional `radio` flavor
 * prefix beside it, a `kind` for styling, the exact `raw` journal line it expands to, and the
 * `fresh`/`divider` flags for the "since you last looked" line. This file only binds those to pixels
 * (design record B.1) and tracks which rows are expanded (view-local state passed in from shell.js).
 *
 * The costume discipline (§7): the radio prefix is FLAVOR, shown small beside the real sentence; the
 * sentence always stands on its own, so a reader who ignores the flavor loses nothing. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // A stable per-row key so an expanded raw line survives the 2s re-render (kept in shell.js state).
  function rowKey(row) {
    return String(row.ts) + ":" + String(row.num) + ":" + String(row.text).slice(0, 16);
  }

  function rowHTML(row, expanded) {
    var key = rowKey(row);
    var open = !!expanded[key];
    var fchip = row.num != null
      ? '<span class="fchip" data-fnum="' + esc(row.num) + '" title="open the flight card for SL-' +
        esc(row.num) + '">SL-' + esc(row.num) + '</span> '
      : "";
    // The radio prefix rides BESIDE the real sentence (§7) — small, amber, flavor only.
    var radio = row.radio ? '<span class="radio">' + esc(row.radio) + '</span> ' : "";
    return '<div class="tower-row kind-' + esc(row.kind || "event") + (row.fresh ? " fresh" : "") + '"' +
        ' data-key="' + esc(key) + '">' +
      '<span class="t">' + esc(row.hhmm) + '</span>' +
      '<span class="msg">' + radio + fchip + esc(row.text) + '</span>' +
      '<span class="caret" data-toggle="' + esc(key) + '" title="show the raw journal line">' +
        (open ? "▾" : "▸") + '</span>' +
      (open ? '<div class="tower-raw">' + esc(row.raw) + '</div>' : "") +
    '</div>';
  }

  // The full TOWER LOG panel. `rows` are chronological (oldest→newest); we show the most recent
  // window with the newest at the bottom, matching screen 7a's comms-feed reading order. The
  // "since you last looked" divider is drawn before the first row the server flagged.
  //
  // Routine bookkeeping (relabel repeats — the label-convergence flurry GitHub's read-lag produces,
  // issue #36) is classified server-side (row.tier === "routine") and HIDDEN from the comms feed by
  // default; a small in-panel affordance reveals it on demand (`showRoutine`, view-local state from
  // shell.js). The classification is the server's (design B.1) — this only binds tier to visibility.
  function panelHTML(rows, expanded, newCount, showRoutine) {
    // The server already windows to the display slice AND places the divider on a COMMS row within
    // it, so the client renders the (comms) rows as-given — never re-slicing (that would drop the
    // divider row). Routine rows are dropped from the default feed but their count feeds the reveal.
    rows = rows || [];
    expanded = expanded || {};
    var routineCount = 0;
    for (var i = 0; i < rows.length; i++) { if (rows[i].tier === "routine") routineCount++; }
    var shown = showRoutine ? rows : rows.filter(function (row) { return row.tier !== "routine"; });
    var body = shown.map(function (row) {
      var divider = row.divider
        ? '<div class="tower-divider"><span>SINCE YOU LAST LOOKED' +
          (newCount ? " · " + newCount + " NEW" : "") + '</span></div>'
        : "";
      return divider + rowHTML(row, expanded);
    }).join("");

    // The reveal affordance — only when there is routine bookkeeping in the window to reveal.
    var reveal = routineCount
      ? '<button type="button" class="tower-routine' + (showRoutine ? " on" : "") + '"' +
          ' data-tower-routine="' + (showRoutine ? "hide" : "show") + '"' +
          ' title="routine bookkeeping (relabels) is kept off the comms feed">' +
          (showRoutine
            ? "▾ hide " + routineCount + " routine bookkeeping"
            : "▸ " + routineCount + " routine bookkeeping hidden") +
        '</button>'
      : "";

    var newBadge = newCount
      ? '<span class="tower-new" title="new radio traffic since you last looked">' + newCount + ' NEW</span>'
      : "";
    return '<div class="panel tower">' +
      '<div class="panel-head"><span class="panel-title">TOWER LOG</span>' + newBadge +
        '<span class="right">WHOLE FIELD</span></div>' +
      '<div class="tower-feed">' +
        (body || '<div class="tower-foot">no radio traffic yet</div>') +
      '</div>' +
      reveal +
      '<div class="tower-foot">RADIO FLAVOR RIDES BESIDE THE REAL LINE · ▸ EXPANDS TO RAW JOURNAL</div>' +
    '</div>';
  }

  window.Tower = { panelHTML: panelHTML, rowKey: rowKey };
})();
