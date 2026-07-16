"""Guard (issue #146): the field always says WHICH source it is showing, and how old it is.

The dashboard renders the runner's own published view. When it can't — the runner went quiet — it
polls GitHub itself, and that must be impossible to mistake for the real thing. The owner spent
weeks reading this surface as a live mirror of the runner while it was quietly a second opinion;
the whole fix is worthless if the fallback looks identical to LIVE.

So two things must reach the screen:
  * ALWAYS, in both modes: the age of the data on screen and the tick timer (time since the
    runner's last completed tick). The stamp is the honesty.
  * In FALLBACK only: a prominent banner naming BOTH facts — since when the runner has been silent,
    and that this is GitHub directly rather than the runner's view.

Like the other field guards (issues #22/#27/#30/#32/#35/#38/#45), these are STRING checks on the
shipped static bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future
edit drops the seam. The rendered proof that it LOOKS right lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# --------------------------- the mode banner ---------------------------

def test_field_binder_reads_the_servers_source_verdict():
    # design record B.1: the JS binds the server's decision, never re-derives the mode.
    assert "source" in _FIELD, "field.js must read repo.source"
    assert re.search(r"\bfallback\b", _FIELD), (
        "field.js must gate the banner on the server's fallback mode")


def test_field_mounts_the_fallback_banner():
    assert "fld-src" in _FIELD, "field.js must mount the source-mode banner (fld-src)"


def test_the_banner_lines_come_from_the_server_never_hardcoded():
    # The two facts (silent-since HH:MM, showing GitHub directly) are composed server-side by
    # flights.source_mode so the words and the mode can never disagree. The JS binds `lines`.
    assert re.search(r"\.lines\b", _FIELD), (
        "field.js must render the server-built banner lines, not its own strings")


def test_the_banner_is_hidden_in_live_mode():
    # LIVE is the normal state and must be quiet — a banner that always shows teaches the owner to
    # ignore it, which is how the fallback would become invisible again.
    assert re.search(r"(hidden\s*=\s*!)", _FIELD), (
        "field.js must hide the source banner unless the server says fallback")


def test_css_styles_the_fallback_banner():
    assert ".fld-src" in _CSS, "shell.css must style .fld-src — the source-mode banner"


def test_the_fallback_banner_is_visually_unmistakable():
    # The DoD's word: "visually unmistakable from LIVE mode". A banner that whispers is the bug.
    # Assert the style block carries real emphasis rather than inheriting the field's quiet chrome.
    block = re.search(r"^\.fld-src\s*\{([^}]*)\}", _CSS, re.MULTILINE)
    assert block, ".fld-src must have its own style block"
    body = block.group(1)
    assert re.search(r"background|border", body), (
        ".fld-src must paint its own ground/border — it cannot read as ordinary field chrome")


# --------------------------- the always-on freshness surface ---------------------------
#
# #146 mounted a bare stamp (`fld-age`) that rendered the two ages and nothing else. Issue #166
# ABSORBED it into the standing truth strip (`fld-truth`): the same corner and the same always-on
# posture, but it now states the CONCLUSION ("loop may be down") and carries a third fact the two
# clocks cannot see — the engine's publish drift. Two surfaces both reporting freshness would have
# been a duplicate readout the owner had to reconcile by eye, so the stamp moved rather than
# multiplied.
#
# These guards follow that seam. #146's intent is unchanged and still pinned HERE: the surface is
# mounted, and it is never gated on the mode. The words themselves are now composed server-side, so
# the "both clocks, in plain words, never a fabricated zero" half of the intent is pinned where the
# words are — tests/test_truth.py (`..._is_stated_plainly_and_calmly`, `..._never_a_confident_zero`).
# The strip's own rendering guards live in tests/test_static_truth.py.

def test_field_mounts_the_always_on_freshness_surface():
    assert "fld-truth" in _FIELD, (
        "field.js must mount the always-on freshness surface — the truth strip (fld-truth)")


def test_the_freshness_surface_is_shown_in_both_modes():
    # It must NOT be gated on the mode — it is the always-on honesty, not a fallback extra. A strip
    # that appears only once you already suspect the dashboard is the bug, not the fix.
    for gate in re.findall(r"fixedEls\.truth\.hidden\s*=\s*([^;\n]+)", _FIELD):
        assert "fallback" not in gate, (
            "the truth strip must show in BOTH modes, never only in fallback")


def test_css_styles_the_always_on_freshness_surface():
    assert ".fld-truth" in _CSS, "shell.css must style .fld-truth — the standing truth strip"
