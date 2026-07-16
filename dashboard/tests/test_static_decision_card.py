"""Guard (issue #162): every owner hand-back renders as a FULL-TEXT decision card.

The payload half has been honest since #5 — ``assemble_snapshot`` carries a long memo untrimmed
(pinned in ``tests/test_snapshot``). The PIXEL half was not: the memo well kept a fixed
``max-height`` and scrolled inside the card, so a long question was still something William had to
discover and scroll rather than read. #162 removes that clamp and adds the three things a decision
needs beside the text — a link to the issue, the dossier of evidence behind the decision (the
capture from #152), and verbs whose labels state their effect.

Like ``test_static_needs_collapse``, these are string guards on the shipped static bundle, not
behavioural tests (the repo runs no JS engine — Python stdlib only). They exist so a future edit
that re-clamps the memo, drops the dossier, or renames a verb back to a bare "Drop" fails CI
instead of silently regressing. The rendered proof that a long question really is shown in full
lives in the PR's screenshot evidence, driven in a real browser.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")
_NEEDS_JS = (_STATIC / "needsyou.js").read_text(encoding="utf-8")


def _rule_body(css, selector):
    """The declaration block for the FIRST ``selector { ... }`` rule (declarations only — these are
    flat rules with no nesting). Returns "" when the selector is absent."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def _strip_js_comments(js):
    """Drop block comments and whole-line ``//`` comments so a guard binds the CODE, not a comment
    that happens to mention the same word (the convention from issue #28's guards)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


_CODE = _strip_js_comments(_NEEDS_JS)


# =============================== the whole question, never truncated ===============================

def _all_rule_bodies(css, needle):
    """EVERY declaration block whose selector mentions ``needle`` — not just the first. A guard that
    reads only the first matching rule can be defeated by a later override that re-clamps the memo
    (Codex cross-review); the cascade is what the owner actually sees, so check all of it."""
    return [m.group(2) for m in re.finditer(r"([^{}]*)\{([^}]*)\}", css)
            if needle in m.group(1)]


def test_the_memo_well_has_no_height_clamp():
    # THE #162 pixel fix. A long question must GROW the card, never hide in a scroll box: the owner
    # reads the decision he is being asked to make without discovering there is more.
    bodies = _all_rule_bodies(_CSS, ".card .memo")
    assert bodies, ".card .memo must still be styled"
    for body in bodies:                       # every rule in the cascade, not merely the first
        assert "max-height" not in body, "a height clamp truncates the question — #162 forbids it"
        assert "-webkit-line-clamp" not in body and "line-clamp" not in body
        assert "overflow: hidden" not in body and "overflow:hidden" not in body
        assert "text-overflow" not in body, "an ellipsis is truncation by another name"
    # and the text must still wrap rather than run off the card
    assert any("pre-wrap" in b for b in bodies)


def test_the_needs_panel_does_not_re_clamp_the_card_from_outside():
    # A clamp on an ancestor would truncate just as effectively as one on .memo itself.
    for sel in (".needs-list", ".panel.needs"):
        body = _rule_body(_CSS, sel)
        if body:
            assert "max-height" not in body, "%s must not clamp the cards it holds" % sel


def test_the_card_renders_the_whole_memo_verbatim():
    # The JS binds the server's memo string whole — no slice/substr/truncate in the render path.
    assert "esc(c.memo)" in _CODE
    for chop in (".slice(", ".substr(", ".substring(", "…"):
        assert chop not in _CODE, "the card must never shorten the question client-side (%s)" % chop


# =============================== the issue link ===============================

def test_the_card_links_to_the_issue():
    # One click to the issue itself — the owner never opens a terminal to read what he is deciding.
    assert "c.issue_url" in _CODE
    assert 'target="_blank"' in _CODE


# =============================== the dossier ===============================

def test_the_card_renders_the_dossier_of_evidence():
    # The evidence behind the decision (#152's capture), shown so the owner can judge in place.
    assert "c.dossier" in _CODE
    assert "dossier" in _CSS, "the dossier needs a style, not just markup"


def test_the_card_shows_the_dossier_note_when_no_evidence_was_captured():
    # Honest empty: the card SAYS the runner captured nothing rather than implying the reason is all
    # the machine saw. The server owns the sentence; the JS only binds it.
    assert re.search(r"\bd\.note\b|dossier\.note", _CODE), "the honest-empty note must be rendered"


# =============================== consequence-named verbs ===============================

def test_the_buttons_are_labelled_from_the_server_not_derived_here():
    # Design record B.1: the server owns every semantic. The verb labels are #162's consequence
    # names, computed and tested in lib/cards — the JS must not re-derive them.
    assert "c.actions" in _CODE
    for derived in ('"Re-approve"', '"Accept & relaunch"', '"Drop"', 'approveLabel', 'approveAct'):
        assert derived not in _CODE, "the label/verb must come from the server, not %s" % derived


def test_the_destructive_button_is_marked_by_the_server():
    # The armed second tap is driven by the server's `destructive` flag, so a future verb that is
    # also destructive inherits the confirm automatically rather than being forgotten.
    assert "destructive" in _CODE
    assert "armed_label" in _CODE


def test_the_card_still_names_the_drop_target_uniquely_when_armed():
    # Kept from issue #44: Needs You is WHOLE-FIELD, so the confirm names repo AND number — two
    # repos can each carry a #7 and the number alone would not say which one closes.
    assert "drop-consequence" in _CODE
    assert "c.repo" in _CODE
