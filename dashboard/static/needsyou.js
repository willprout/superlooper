/* Needs You — the decision inbox (Task 9 / design record §2–§4).
 *
 * window.NeedsYou renders one card per decision waiting on William, from the server's already-glossed
 * cards (lib/cards). Costume rule 2 (§3): the card LEADS with a plain headline + gloss; the literal
 * loop term is secondary (the badge + a hover). The conflict-cap card names the collision in one
 * plain sentence and highlights Discuss as the default (§8 — the guard against a blind Approve on a
 * collision). The panel never filters and never moves (§4); empty collapses to the all-clear ribbon.
 *
 * This file binds strings to pixels and wires the Task-6 verbs by data-act/data-repo/data-num — it
 * decides no labels; the server owns every semantic (design record B.1). */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function cardHTML(c, confirmingDrop) {
    var da = ' data-repo="' + esc(c.repo) + '" data-num="' + esc(c.num) + '"';
    var confirming = confirmingDrop === (c.repo + "#" + c.num);

    // The "yes" verb depends on the kind: a bounce accepts the amendment and relaunches; a parked or
    // needs-william flight re-approves. The server does the label math either way (Task 6).
    var isBounce = c.reason === "bounced" || c.kind === "bounced";
    var approveAct = isBounce ? "bounce-yes" : "approve";
    var approveLabel = isBounce ? "Accept & relaunch"
      : (c.state === "parked" ? "Re-approve & relaunch" : "Re-approve");

    // The flight chip opens the drawer (tap-where-you-read); it carries data-repo because Needs You
    // is WHOLE-FIELD — a card may name any watched repo, not the one currently on camera, so the
    // drawer (and its verb buttons) must resolve by (repo, num), never by number alone.
    var chips = '<div class="chips">' +
      '<span class="chip" data-fnum="' + esc(c.num) + '" data-repo="' + esc(c.repo) +
        '" title="open the flight card">SL-' + esc(c.num) + '</span>' +
      '<span class="chip state" title="the literal loop state">' + esc(c.badge) + '</span></div>';

    // The plain-language lead + gloss; the literal term rides on the gloss as a hover (costume rule 2).
    var gloss = c.gloss || {};
    var headline = '<div class="card-headline">' + esc(c.headline || "") + '</div>';
    var glossLine = gloss.plain
      ? '<div class="card-gloss" title="the literal term: ' + esc(gloss.term || "") + '">' +
        esc(gloss.plain) + ' <span class="term">(' + esc(gloss.term || "") + ')</span></div>'
      : "";

    // The conflict-cap collision, named in one plain sentence (never a bare badge, §3).
    var collision = c.collision
      ? '<div class="card-collision">✦ ' + esc(c.collision) + '</div>' : "";

    var memo = c.memo ? '<div class="memo">' + esc(c.memo) + '</div>' : "";

    // Discuss is the highlighted default ONLY on a conflict-cap card (§8). Everywhere else Approve
    // leads and Discuss is a quiet link.
    // The armed (second-tap) Drop names its CONSEQUENCE in plain words (issue #44): drop CLOSES the
    // issue for good — "never-mind", the far pole from approve's "release to build". The caption
    // rides ABOVE the actions so a mid-confirm Drop can never be mistaken for an Approve. It names
    // the UNIQUE destructive target — repo AND number: Needs You is WHOLE-FIELD, so two repos can
    // each carry a #7, and the number alone would not say which one closes (Codex review, issue #44).
    // Plain visible text, no aria-live role: #root is rebuilt whole every 2s poll while the confirm
    // stays armed, so a live region would re-announce every tick (Codex review). It is never a browser
    // confirm() — the state survives that re-render (§4). The button keeps the two-tap gesture ("tap
    // again"); the caption directly above it carries the repo, so the target is unambiguous.
    var dropConsequence = confirming
      ? '<div class="drop-consequence">✕ Closes ' + esc(c.repo) + ' #' + esc(c.num) +
        ' for good — never-mind, not release.</div>'
      : "";
    var dropBtn = '<button class="btn ghost' + (confirming ? " danger" : "") + '" data-act="drop"' + da + '>' +
      (confirming ? "Drop #" + esc(c.num) + " — tap again" : "Drop") + '</button>';
    var actions;
    if (c.discuss_default) {
      actions = '<div class="actions">' +
        '<button class="btn primary" data-act="discuss"' + da + '>Discuss →</button>' +
        '<button class="btn ghost" data-act="' + approveAct + '"' + da + '>' + esc(approveLabel) + '</button>' +
        dropBtn +
      '</div>';
    } else {
      actions = '<div class="actions">' +
        '<button class="btn primary" data-act="' + approveAct + '"' + da + '>' + esc(approveLabel) + '</button>' +
        dropBtn +
        '<button class="btn-note link" data-act="discuss"' + da + '>Discuss →</button>' +
      '</div>';
    }

    return '<div class="card kind-' + esc(c.kind || "parked") + '">' +
      chips + headline + glossLine + collision + memo + dropConsequence + actions + '</div>';
  }

  // The full NEEDS YOU panel — every waiting decision, whole-field, never filtered by the camera (§4).
  // EMPTY collapses to a slim all-clear rail (§4) instead of a full empty column; shell.js narrows
  // the grid track to match, so the airfield gains the reclaimed width. The rail carries an explicit
  // "all clear" caption so the quiet state is never ambiguous (§5 — calm carries a caption).
  function panelHTML(needs, confirmingDrop) {
    needs = needs || [];
    if (!needs.length) {
      return '<div class="needs collapsed">' +
        '<div class="ribbon-allclear rail" role="status" ' +
          'aria-label="all clear — nothing needs you right now">' +
          '<span class="check">✓</span>' +
          '<span class="cap">ALL CLEAR</span></div></div>';
    }
    var body = '<div class="needs-list">' +
      needs.map(function (c) { return cardHTML(c, confirmingDrop); }).join("") + '</div>';
    return '<div class="panel needs">' +
      '<div class="panel-head"><span class="panel-title">NEEDS YOU</span>' +
        '<span class="badge">' + needs.length + '</span></div>' +
      body + '</div>';
  }

  window.NeedsYou = { panelHTML: panelHTML };
})();
