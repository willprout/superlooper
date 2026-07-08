/* The boards — departures (the launch queue, real order) + arrivals (the Solari flagship).
 *
 * window.Boards has two jobs, both pixels-only (design record B.1 — the ordering, the awaiting-
 * connection state, the newest-first arrivals all arrive already decided by the tested server):
 *
 *   • departuresInner(deps, slug) → the departures board's inner HTML. `deps` is the ordered queue
 *     from flights.queue_rows: ⚡ expedite on top, priority band, number; a blocked flight reads
 *     "awaiting connection SL-N", dimmed, and never offers a launch bump.
 *   • attach(mount, arrivals, fun) → drives the persistent Solari board. Exactly the field's model
 *     (window.CCField.attach): the Solari node is created ONCE and re-parented into the fresh mount
 *     every poll, so shell.js's innerHTML rebuild never clobbers a split-flap flutter mid-air. Each
 *     poll only feeds it the arrivals and it flutters what actually changed. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ---- departures: the launch queue in real order (design record §3) ----
  // The full queue paginates DEP_PAGE_SIZE rows at a time (issue #30) so a long backlog never grows
  // the board without bound; the current `page` is held by the shell (state.depPage) so it survives
  // the 2s poll re-render. Departures is a static split-flap-styled list (only the Solari flagship
  // flutters), so a page turn here is a clean re-render — the joy investment stays on the arrivals.
  var DEP_PAGE_SIZE = 5;

  function departuresInner(deps, slug, page) {
    deps = deps || [];
    var pages = Math.max(1, Math.ceil(deps.length / DEP_PAGE_SIZE));
    page = Math.min(Math.max(0, page || 0), pages - 1);
    var start = page * DEP_PAGE_SIZE;
    var pageDeps = deps.slice(start, start + DEP_PAGE_SIZE);
    var rows = pageDeps.map(function (d) {
      var launchable = d.launchable !== false;                 // server truth; default launchable
      var expedited = !!d.expedited;
      var next = launchable && d.pos === 1;

      var flightCls = "flap link" + (expedited ? " exped" : "");
      var flight = (expedited ? "⚡ " : "") + (d.flight || ("SL-" + d.num));

      // STATUS is a SHORT, colour-coded label that mirrors the arrivals remark chip (issue #31) — the
      // thing that makes the two boards read as one airport's signage. The costume stays honest
      // (design record §3, costume rule 2): the label is short, but the server's full plain-words
      // status_text ("NEXT OFF THE STAND", "AWAITING CONNECTION SL-9") reaches a SCREEN READER as real
      // DOM text — a visually-hidden (.cc-sr-only) span carries the full phrase while the short visible
      // chip is aria-hidden, so the SR reads the whole phrase and not both (mouse users get it on the
      // hover title too). The SL-N a blocked flight waits on comes straight from the server's discrete
      // `blocked_by` — the JS composes, never parses.
      var statusFull = String(d.status_text || (launchable ? "QUEUED" : "AWAITING")).toUpperCase();
      var statusLabel, statusState;
      if (!launchable) {
        statusLabel = d.blocked_by != null ? ("AWAITING SL-" + d.blocked_by) : "AWAITING";
        statusState = "await";
      } else if (next) {
        statusLabel = "NEXT"; statusState = "next";
      } else {
        statusLabel = "QUEUED"; statusState = "queued";
      }

      // Only a launchable flight offers the ⚡ bump — an awaiting flight can't leave the stand until
      // its connection lands, so a "bump to the top of the launch order" button there would be a lie.
      var expBtn = launchable
        ? '<button class="dep-expedite" data-act="expedite" data-repo="' + esc(slug) + '" data-num="' + esc(d.num) + '"' +
            ' title="⚡ Expedite SL-' + esc(d.num) + ' — bump to the top of the launch order">⚡</button>'
        : '<span class="dep-expedite-blank" aria-hidden="true"></span>';

      return '<div class="board-row dep-row' + (launchable ? "" : " awaiting") + '">' +
        '<span class="' + flightCls + '" data-fnum="' + esc(d.num) + '">' + esc(flight) + '</span>' +
        '<span class="flap">' + esc(d.destination || "—") + '</span>' +
        '<span class="dep-status ' + statusState + '" title="' + esc(statusFull) + '">' +
          '<span aria-hidden="true">' + esc(statusLabel) + '</span>' +
          '<span class="cc-sr-only">' + esc(statusFull) + '</span>' +
        '</span>' +
        expBtn +
      '</div>';
    }).join("");

    // The page control — prev · a split-flap page indicator · next — shown only when the queue spills
    // past one page. The buttons carry data-deppage so the shell's one delegated listener turns the
    // page (and reset it on a repo switch); the indicator sits in the board's corner (right-aligned).
    var pager = pages > 1
      ? '<div class="dep-pager">' +
          '<button type="button" class="dep-page-btn dep-page-prev" data-deppage="prev"' +
            (page <= 0 ? " disabled" : "") + ' aria-label="earlier in the launch order">◀</button>' +
          '<span class="dep-page-num">' + (page + 1) + " / " + pages + '</span>' +
          '<button type="button" class="dep-page-btn dep-page-next" data-deppage="next"' +
            (page >= pages - 1 ? " disabled" : "") + ' aria-label="later in the launch order">▶</button>' +
        '</div>'
      : "";

    // The queue-position column is gone (issue #31): row order IS the launch order, so the position
    // number was redundant with the rows themselves. Three labelled columns remain — FLIGHT ·
    // DESTINATION · STATUS — the last right-aligned above its colour-coded label like a real board's
    // remarks column.
    // A one-line legend explains the ⚡ marker/button in place (DoD), shown only when a queue exists.
    var legend = deps.length
      ? '<div class="dep-legend"><span class="bolt" aria-hidden="true">⚡</span>' +
          '<span>EXPEDITE · TAP TO BUMP A FLIGHT TO THE FRONT OF THE LINE</span></div>'
      : "";

    return '<div class="board-head"><span class="dot"></span><span class="name">DEPARTURES</span>' +
        '<span class="tag">LAUNCH ORDER</span></div>' +
      '<div class="board-cols dep-cols"><span>FLIGHT</span><span>DESTINATION</span>' +
        '<span class="dep-col-status">STATUS</span></div>' +
      rows +
      (deps.length ? "" : '<div class="board-empty">— QUEUE EMPTY · 2 RUNWAYS OPEN —</div>') +
      pager +
      legend;
  }

  // ---- arrivals: the persistent Solari board (mirrors window.CCField.attach) ----
  var _node = null;     // the persistent .solari container (survives every innerHTML rebuild)
  var _ctrl = null;     // its Solari controller (mounted once)

  function arrivalLines(arrivals) {
    // The server hands arrivals newest-first already; map each landed flight to a Solari row. `id`
    // (the issue number) is the stable identity the board diffs on to flutter only genuine landings.
    return (arrivals || []).map(function (a) {
      return { id: a.num, time: a.hhmm || "", flight: a.flight || ("SL-" + a.num),
               title: a.landed || "", remark: a.remark || "" };
    });
  }

  function attach(mount, arrivals, fun, slug) {
    if (!mount || !window.Solari) return;
    if (!_node) {
      _node = document.createElement("div");
      _node.className = "solari";
      mount.appendChild(_node);              // in-DOM before mount → real width for the tile count
      _ctrl = window.Solari.mount(_node);
    } else if (_node.parentNode !== mount) {
      mount.appendChild(_node);              // re-parent into the fresh mount (the poll rebuilt it)
    }
    // Fun toggles arrive folded (master AND mechanic) from the server; absent ⇒ on (§0.1 default).
    var animate = fun ? fun.solari !== false : true;
    var clack = fun ? !!fun.solari_clack : true;
    // `slug` is the repo the camera is on; the controller resets to page 1 when it changes so the
    // board never shows repo B's arrivals on repo A's page (§4 — the boards follow the camera).
    _ctrl.update(arrivalLines(arrivals), { animate: animate, clack: clack, repo: slug });
  }

  window.Boards = {
    departuresInner: departuresInner,
    attach: attach,
    DEP_PAGE_SIZE: DEP_PAGE_SIZE,   // the shell reads this to clamp state.depPage on a page turn
  };
})();
