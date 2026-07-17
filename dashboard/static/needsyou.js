/* Needs You — the decision inbox (Task 9 / design record §2–§4).
 *
 * window.NeedsYou renders one card per decision waiting on the operator, from the server's already-glossed
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

  function cardHTML(c, confirming) {
    var da = ' data-repo="' + esc(c.repo) + '" data-num="' + esc(c.num) + '"';
    // The armed (second-tap) key is per (repo, num, ACT): a decision card can now carry TWO
    // destructive verbs — Rebuild and Drop (issue #161) — and arming one must never arm the other.
    // `confirming` is the caller-threaded "repo#num#act" mid-confirm, kept in state so the 2s poll
    // re-render can't silently disarm it (design record §4). `armKey` is that string for one action.
    var armKey = function (a) { return c.repo + "#" + c.num + "#" + a.act; };

    // The flight chip opens the drawer (tap-where-you-read); it carries data-repo because Needs You
    // is WHOLE-FIELD — a card may name any watched repo, not the one currently on camera, so the
    // drawer (and its verb buttons) must resolve by (repo, num), never by number alone. The issue
    // link (#162) is the escape hatch to ground truth: the owner reads the whole decision here, and
    // the issue itself is one click away — never a terminal.
    var chips = '<div class="chips">' +
      '<span class="chip" data-fnum="' + esc(c.num) + '" data-repo="' + esc(c.repo) +
        '" title="open the flight card">SL-' + esc(c.num) + '</span>' +
      '<span class="chip state" title="the literal loop state">' + esc(c.badge) + '</span>' +
      (c.issue_url
        ? '<a class="issue-link" href="' + esc(c.issue_url) + '" target="_blank" rel="noopener" ' +
          'title="open this issue on GitHub">issue ↗</a>'
        : "") +
      '</div>';

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

    // The WHOLE question, exactly as the worker wrote it (issue #162) — the server sends it untrimmed
    // and nothing here shortens it. The card grows; the panel scrolls. The operator must never have
    // to discover that the decision being answered continues below a fold.
    var memo = c.memo ? '<div class="memo">' + esc(c.memo) + '</div>' : "";

    // The dossier — the evidence behind the decision (issue #162; the capture from #152). Rows of
    // label → value, all server-derived real facts. When the runner captured no structured evidence
    // the server sends a `note` SAYING so, and the card prints that instead of implying the memo is
    // everything the machine saw (the honest-empty discipline, §5).
    var dossier = "";
    var d = c.dossier;
    if (d && ((d.items && d.items.length) || d.note)) {
      var rows = (d.items || []).map(function (it) {
        return '<div class="drow"><span class="k">' + esc(it.label) + '</span>' +
          '<span class="v">' + esc(it.value) + '</span></div>';
      }).join("");
      var note = d.note ? '<div class="dossier-note">' + esc(d.note) + '</div>' : "";
      dossier = '<div class="dossier"><div class="dossier-label">WHAT THE MACHINE SAW</div>' +
        rows + note + '</div>';
    }

    // The armed (second-tap) Drop names its CONSEQUENCE in plain words (issue #44): drop CLOSES the
    // issue for good — "never-mind", the far pole from approve's "release to build". The caption
    // rides ABOVE the actions so a mid-confirm Drop can never be mistaken for an Approve. Its WORDS
    // are the server's `armed_caption` (issue #162 / B.1) — a destructive consequence is a semantic,
    // and hard-coding it here let it drift from the label it warns about. It names the UNIQUE
    // destructive target — repo AND number: Needs You is WHOLE-FIELD, so two repos can each carry a
    // #7, and the number alone would not say which one closes (Codex review, issue #44).
    // Plain visible text, no aria-live role: #root is rebuilt whole every 2s poll while the confirm
    // stays armed, so a live region would re-announce every tick (Codex review). It is never a browser
    // confirm() — the state survives that re-render (§4).
    var armedAct = confirming && (c.actions || []).filter(function (a) {
      return a.destructive && a.armed_caption && confirming === armKey(a);
    })[0];
    var dropConsequence = armedAct
      ? '<div class="drop-consequence">' + esc(armedAct.armed_caption) + '</div>'
      : "";

    // Every button's LABEL, tone, order and consequence sentence come from the server (issue #162 /
    // design record B.1) — this file derives none of them, so the card, the drawer and the engine's
    // real behaviour cannot drift apart. `destructive` (not a hard-coded "drop") drives the two-tap
    // arm, so any future destructive verb inherits the confirm rather than being forgotten.
    var actions = '<div class="actions">' + (c.actions || []).map(function (a) {
      var armed = a.destructive && confirming === armKey(a);
      var cls = a.tone === "link" ? "btn-note link" : ("btn " + (a.tone || "ghost"));
      if (armed) cls += " danger";
      // A verb that takes typed input (the #163 Answer) renders a textarea the operator fills in;
      // the button's handler reads that field by (repo, num). This is still a mechanical verb — a
      // comment + a label — never AI; the field is just where the operator's own words go.
      var field = a.input
        ? '<textarea class="answer-field" data-input="' + esc(a.input) + '"' + da +
          ' rows="3" placeholder="Type your answer — it posts to the issue in your name and a fresh ' +
          'session resumes with it."></textarea>'
        : "";
      return '<div class="act">' + field +
        '<button class="' + cls + '" data-act="' + esc(a.act) + '"' + da +
          (a.input ? ' data-input="' + esc(a.input) + '"' : "") + '>' +
          esc(armed ? (a.armed_label || a.label) : a.label) + '</button>' +
        '<div class="act-why">' + esc(a.consequence || "") + '</div>' +
      '</div>';
    }).join("") + '</div>';

    return '<div class="card kind-' + esc(c.kind || "parked") + '">' +
      chips + headline + glossLine + collision + memo + dossier + dropConsequence + actions + '</div>';
  }

  // The full NEEDS YOU panel — every waiting decision, whole-field, never filtered by the camera (§4).
  // EMPTY collapses to a slim all-clear rail (§4) instead of a full empty column; shell.js narrows
  // the grid track to match, so the airfield gains the reclaimed width. The rail carries an explicit
  // "all clear" caption so the quiet state is never ambiguous (§5 — calm carries a caption).
  function panelHTML(needs, confirming) {
    needs = needs || [];
    if (!needs.length) {
      return '<div class="needs collapsed">' +
        '<div class="ribbon-allclear rail" role="status" ' +
          'aria-label="all clear — nothing needs you right now">' +
          '<span class="check">✓</span>' +
          '<span class="cap">ALL CLEAR</span></div></div>';
    }
    var body = '<div class="needs-list">' +
      needs.map(function (c) { return cardHTML(c, confirming); }).join("") + '</div>';
    return '<div class="panel needs">' +
      '<div class="panel-head"><span class="panel-title">NEEDS YOU</span>' +
        '<span class="badge">' + needs.length + '</span></div>' +
      body + '</div>';
  }

  window.NeedsYou = { panelHTML: panelHTML };
})();
