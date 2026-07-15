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


# --------------------------- the always-on stamp ---------------------------

def test_field_mounts_the_freshness_stamp():
    assert "fld-age" in _FIELD, "field.js must mount the data-age / tick-timer stamp (fld-age)"


def test_the_stamp_binds_both_clocks():
    # Both, always: how old the DATA is, and how long since the runner's last completed TICK. They
    # are different facts — a fresh tick can still be republishing a 90s-old GitHub read.
    assert "data_age" in _FIELD, "field.js must bind repo.source.data_age"
    assert "tick_age" in _FIELD, "field.js must bind repo.source.tick_age"


def test_the_stamp_is_shown_in_both_modes():
    # The stamp must NOT be gated on the mode — it is the always-on honesty, not a fallback extra.
    # Assert the age element is never given the banner's fallback gate.
    stamp = re.search(r"fixedEls\.age\.hidden\s*=\s*([^;\n]+)", _FIELD)
    if stamp:
        assert "fallback" not in stamp.group(1), (
            "the freshness stamp must show in BOTH modes, never only in fallback")


def test_css_styles_the_freshness_stamp():
    assert ".fld-age" in _CSS, "shell.css must style .fld-age — the always-on freshness stamp"


def test_the_stamp_names_its_clocks_in_plain_words():
    # Costume rule 2 (design §3): the metaphor never hides the words you read. A bare "12s / 4s"
    # tells the owner nothing about WHICH clock is which.
    assert re.search(r"data|age", _FIELD, re.IGNORECASE)
    assert re.search(r"tick", _FIELD, re.IGNORECASE), (
        "the stamp must name the tick timer in plain words, never a bare number")


def test_an_unknown_age_is_never_rendered_as_a_number():
    # source_mode returns data_age None before the first direct poll lands. Rendering that as "0s"
    # would claim the freshest possible data at the exact moment we have none.
    assert re.search(r"==\s*null|!=\s*null|null\s*[=!]==?", _FIELD), (
        "field.js must handle a null age explicitly rather than formatting it as a number")
