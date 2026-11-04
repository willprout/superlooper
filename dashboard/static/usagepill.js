/* UsagePill — the usage figures in the top bar (issue #533).
 *
 * Binds the server's reading to pixels and decides nothing: whether the reading is usable arrives
 * ready as `usage.known`. A null five-hour on a known reading means the account reports no such
 * window right now, so its figure renders as an empty dash. It is checked by name because
 * Math.round(null) is 0, and a made-up 0% would read as headroom nobody measured. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function windowHTML(label, pct, emptyTitle) {
    var head = '<span class="k">' + label + '</span>';
    if (pct === null || pct === undefined) {
      return head + '<span class="usage-pct" title="' + esc(emptyTitle) + '">—</span>';
    }
    var shown = Math.round(pct);
    return head +
      '<span class="usage-bar"><span style="width:' + shown + '%"></span></span>' +
      '<span class="usage-pct">' + shown + '%</span>';
  }

  window.UsagePill = {
    html: function (usage) {
      var u = usage || {};
      if (!u.known) {
        return '<span class="k" title="' + esc(u.status || "unknown") + '">usage ?</span>';
      }
      return windowHTML("5h", u.five_hour_pct, "no five-hour window reported right now") +
        '<span class="sep">·</span>' +
        windowHTML("7d", u.seven_day_pct, "no seven-day figure");
    },
  };
})();
