/* The mechanical morning digest (Task 11 / design record §4) — the load-bearing "what happened".
 *
 * window.CCDigest is a persistent overlay OUTSIDE #root (like the drawer) so the 2s poll never
 * touches it. It fetches the server-derived digest (/api/digest — every count and every sentence
 * pre-computed in lib/digest; NO AI, NO composed prose, §9) and renders three plain sections:
 * mechanical counts, one honest sentence per exception, and the full timestamped event table. Every
 * exception and every row is clickable through to its flight (drawer) or its raw journal line.
 *
 * Design B.1: this file computes NO semantics — it binds the server's strings to pixels and forwards
 * a flight tap as `cc:drawer-open`. It never counts, glosses, or decides what an exception is. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  var node = null, slug = "", expanded = {};
  function el(id) { return document.getElementById(id); }

  // The counts shown as chips, in reading order. Landings/departures/go-arounds/parks always show
  // (the plain top-line); the rest appear only when they happened, so a calm night stays uncluttered.
  var COUNTS = [
    { key: "departures", label: "DEPARTURES", always: true },
    { key: "landings", label: "LANDINGS", always: true },
    { key: "go_arounds", label: "GO-AROUNDS", always: true },
    { key: "parks", label: "PARKS", always: true },
    { key: "wandered", label: "WANDERED" },
    { key: "holds", label: "HOLDS" },
    { key: "missed_approaches", label: "MISSED APPROACHES" },
    { key: "freezes", label: "FREEZES" },
    { key: "alerts", label: "ALERTS" }
  ];

  function ensure() {
    if (node) return;
    node = document.createElement("div");
    node.id = "cc-digest";
    node.className = "cc-digest";
    node.innerHTML =
      '<div class="cc-digest-card">' +
        '<div class="cc-digest-head">' +
          '<span class="cc-digest-title">▤ MORNING DIGEST <b id="cc-digest-name"></b></span>' +
          '<span class="cc-digest-sub">mechanical — counts + one plain sentence per exception. no AI.</span>' +
          '<select id="cc-digest-range" title="window">' +
            '<option value="43200">last 12h</option>' +
            '<option value="86400" selected>last 24h</option>' +
            '<option value="604800">last 7d</option>' +
            '<option value="all">all history</option>' +
          '</select>' +
          '<button class="cc-digest-x" data-digest-close title="close (Esc)">✕</button>' +
        '</div>' +
        '<div class="cc-digest-body" id="cc-digest-body"></div>' +
      '</div>';
    document.body.appendChild(node);

    node.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t === node || t.closest("[data-digest-close]")) { close(); return; }
      var tog = t.closest("[data-digest-toggle]");
      if (tog) {
        var k = tog.getAttribute("data-digest-toggle");
        expanded[k] = !expanded[k];
        renderFrom(node._last);
        return;
      }
      var fn = t.closest("[data-digest-num]");
      if (fn) {
        var num = Number(fn.getAttribute("data-digest-num"));
        document.dispatchEvent(new CustomEvent("cc:drawer-open", { detail: { repo: slug, num: num } }));
      }
    });
    el("cc-digest-range").addEventListener("change", function () { load(this.value); });
    document.addEventListener("keydown", function (e) {
      if (isOpen() && e.key === "Escape") close();
    });
  }

  function isOpen() { return !!(node && node.classList.contains("open")); }

  function open(repoSlug) {
    ensure();
    slug = repoSlug || "";
    expanded = {};
    el("cc-digest-name").textContent = slug;
    node.classList.add("open");
    load(el("cc-digest-range").value);
  }

  function load(range) {
    el("cc-digest-body").innerHTML = '<div class="cc-digest-loading">reading the journal…</div>';
    var url = "/api/digest?repo=" + encodeURIComponent(slug) + "&range=" + encodeURIComponent(range);
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (b) { return { ok: r.ok, body: b }; },
                             function () { return { ok: r.ok, body: null }; });
      })
      .then(function (res) {
        // A typed 500 (or a "no repo configured" error) must NOT render as a clean/empty digest —
        // the load-bearing account may never falsely say "a clean run" when it actually failed.
        if (!res.ok || !res.body || res.body.error) {
          el("cc-digest-body").innerHTML = '<div class="cc-digest-err">' +
            esc((res.body && res.body.error) || "digest unavailable — the journal couldn’t be read") + '</div>';
          return;
        }
        expanded = {}; renderFrom(res.body);
      })
      .catch(function () {
        el("cc-digest-body").innerHTML = '<div class="cc-digest-err">couldn’t reach the command center</div>';
      });
  }

  function countsHTML(d) {
    var c = d.counts || {};
    var chips = COUNTS.filter(function (m) {
      return m.always || (c[m.key] || 0) > 0;
    }).map(function (m) {
      var n = c[m.key] || 0;
      var cls = "cc-digest-count" + (n > 0 && !m.always ? " hot" : "") + (n === 0 ? " zero" : "");
      return '<span class="' + cls + '"><b>' + esc(n) + '</b>' + esc(m.label) + '</span>';
    }).join("");
    return '<div class="cc-digest-counts">' + chips + '</div>';
  }

  function exceptionsHTML(d) {
    var exc = d.exceptions || [];
    if (!exc.length) {
      return '<div class="cc-digest-section">' +
        '<div class="cc-digest-label">EXCEPTIONS</div>' +
        '<div class="cc-digest-clean">no exceptions in this window — a clean run ✓</div></div>';
    }
    var rows = exc.map(function (e, i) {
      var key = "x" + i;
      var open = !!expanded[key];
      var chip = e.num != null
        ? '<span class="fnum" data-digest-num="' + esc(e.num) + '">SL-' + esc(e.num) + '</span>' : "";
      return '<div class="cc-digest-exc kind-' + esc(e.kind) + '">' +
        '<span class="t">' + esc(e.hhmm) + '</span>' +
        '<span class="kind">' + esc(e.kind.replace(/_/g, " ")) + '</span>' +
        chip +
        '<span class="s">' + esc(e.sentence) + '</span>' +
        '<span class="caret" data-digest-toggle="' + key + '">' + (open ? "▾" : "▸") + '</span>' +
        (open ? '<div class="cc-digest-raw">' + esc(e.raw) + '</div>' : "") +
      '</div>';
    }).join("");
    return '<div class="cc-digest-section">' +
      '<div class="cc-digest-label">EXCEPTIONS <span class="n">' + exc.length + '</span></div>' +
      '<div class="cc-digest-exceptions">' + rows + '</div></div>';
  }

  function eventsHTML(d) {
    var ev = d.events || [];
    if (!ev.length) {
      return '<div class="cc-digest-section"><div class="cc-digest-label">EVENT LOG</div>' +
        '<div class="cc-digest-clean">no journal events in this window</div></div>';
    }
    // Newest first — the same reading order as the tower log + firehose.
    var rows = ev.slice().reverse().map(function (r, i) {
      var key = "e" + i;
      var open = !!expanded[key];
      var radio = r.radio ? '<span class="radio">' + esc(r.radio) + '</span> ' : "";
      var chip = r.num != null
        ? '<span class="fnum" data-digest-num="' + esc(r.num) + '">SL-' + esc(r.num) + '</span>' : "";
      return '<div class="cc-digest-row kind-' + esc(r.kind || "event") + '">' +
        '<span class="t">' + esc(r.hhmm) + '</span>' + chip +
        '<span class="msg">' + radio + esc(r.text) + '</span>' +
        '<span class="caret" data-digest-toggle="' + key + '">' + (open ? "▾" : "▸") + '</span>' +
        (open ? '<div class="cc-digest-raw">' + esc(r.raw) + '</div>' : "") +
      '</div>';
    }).join("");
    return '<div class="cc-digest-section">' +
      '<div class="cc-digest-label">EVENT LOG <span class="n">' + ev.length + '</span> · ' +
        'TIMESTAMPED · TAP A FLIGHT TO OPEN IT</div>' +
      '<div class="cc-digest-events">' + rows + '</div></div>';
  }

  function renderFrom(d) {
    node._last = d;
    if (!d) return;
    el("cc-digest-body").innerHTML = countsHTML(d) + exceptionsHTML(d) + eventsHTML(d);
  }

  function close() { if (node) node.classList.remove("open"); }

  window.CCDigest = { open: open, isOpen: isOpen };
})();
