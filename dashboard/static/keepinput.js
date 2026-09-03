/* KeepInput — the operator's half-typed words survive the poll redraw (issue #475).
 *
 * The whole surface is rebuilt from /api/snapshot every ~2s with an `innerHTML` assignment: #root
 * for the shell, the drawer node for the flight card. A rebuilt `<textarea>` is a NEW, empty
 * element, so anything the operator had typed and not yet submitted vanished about a second after
 * they started — which is exactly how the dashboard's own Answer verb (#163) became unusable.
 *
 * The codebase already carries the antidote for MOVING state: the airfield canvas and the Solari
 * board are persistent nodes re-parented into each fresh mount (window.CCField.attach /
 * window.Boards.attach), so a rebuild never clobbers them. A typed answer cannot use that pattern —
 * the field is composed per (repo, num) deep inside server-driven markup — so it gets the other
 * honest answer to the same hazard: carry the operator's state ACROSS the rebuild.
 *
 *     KeepInput.preserve(container, function () { container.innerHTML = fresh(); });
 *
 * What survives is only what the operator put there. The rule is the browser's own notion of a
 * DIRTY field — `value !== defaultValue`, i.e. the field holds something other than what the HTML
 * rendered. So a field nobody has touched keeps refreshing from the server like every other pixel
 * (the drawer must never freeze on stale data), and one that has been typed into keeps its words,
 * its focus and its caret. It is deliberately general rather than keyed to `.answer-field`: any
 * free-text field a future render puts on this path inherits the survival instead of quietly
 * re-earning the bug. */
(function () {
  "use strict";

  // Which controls hold typed text. An EXCLUDE list, not an include list, so a text-ish input type
  // added later is preserved by default and only a genuinely non-text control has to be named.
  var NOT_TEXT = {
    button: 1, checkbox: 1, color: 1, date: 1, "datetime-local": 1, file: 1, hidden: 1, image: 1,
    month: 1, radio: 1, range: 1, reset: 1, submit: 1, time: 1, week: 1,
  };

  function isTexty(el) {
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName !== "INPUT") return false;
    return !NOT_TEXT[(el.getAttribute("type") || "text").toLowerCase()];
  }

  function fields(container) {
    if (!container || !container.querySelectorAll) return [];
    var out = [], all = container.querySelectorAll("textarea, input");
    for (var i = 0; i < all.length; i++) if (isTexty(all[i])) out.push(all[i]);
    return out;
  }

  // The identity that has to survive the rebuild. Never DOM position: a card can appear or leave
  // between two polls, and matching by position would pour one flight's half-written answer into
  // another flight's field. It is the identifying attributes the renderers already emit — the
  // verb's `data-input` and the (repo, num) the answer would be POSTed to — plus a per-key
  // occurrence number so two identical fields in one container still can't swap contents.
  function keyOf(el) {
    return [el.tagName, el.id || "", el.getAttribute("name") || "", el.className || "",
            el.getAttribute("data-input") || "", el.getAttribute("data-repo") || "",
            el.getAttribute("data-num") || ""].join("|");
  }

  function keyed(container, fn) {
    var seen = {};
    fields(container).forEach(function (el) {
      var k = keyOf(el);
      seen[k] = (seen[k] || 0) + 1;
      fn(el, k + "|#" + seen[k]);
    });
  }

  function capture(container) {
    var doc = (container && container.ownerDocument) || document;
    var saved = {};
    keyed(container, function (el, key) {
      var focused = doc.activeElement === el;
      var dirty = el.value !== el.defaultValue;
      if (!dirty && !focused) return;          // untouched and unfocused ⇒ nothing of the operator's
      var start = null, end = null;
      try { start = el.selectionStart; end = el.selectionEnd; } catch (e) { /* some input types */ }
      saved[key] = { value: el.value, dirty: dirty, focused: focused, start: start, end: end };
    });
    return saved;
  }

  function restore(container, saved) {
    if (!saved) return;
    keyed(container, function (el, key) {
      var s = saved[key];
      if (!s) return;
      if (s.dirty) el.value = s.value;
      if (!s.focused) return;
      try { el.focus(); } catch (e) { return; }
      if (s.start === null || !el.setSelectionRange) return;
      try { el.setSelectionRange(s.start, s.end); } catch (e) { /* not selectable */ }
    });
  }

  // Run `rebuild` — whatever wholesale re-render the caller does — with the operator's typed state
  // carried across it. Fail-soft on purpose: if `rebuild` throws, the surface has bigger problems
  // than a lost draft, and swallowing it here would hide the real error.
  function preserve(container, rebuild) {
    var saved = capture(container);
    rebuild();
    restore(container, saved);
  }

  window.KeepInput = { preserve: preserve, capture: capture, restore: restore };
})();
