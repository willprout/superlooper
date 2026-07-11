"""Guard (issue #45): a state-format mismatch renders as its OWN distinct, honest surface on the
field — a named "format mismatch" card — never a silently blank field.

The dashboard reads a state home field-by-field and every reader fails CLOSED to empty. So an engine
that changes the on-disk SHAPE would silently BLANK the field — the most likely future "why is my
dashboard empty" with no diagnostic. The engine now stamps the format version it wrote; the server
turns it into an honest verdict (``repo.state_format`` — compatible / a named mismatch, pinned in
test_snapshot.py + test_flights_state_format.py). The static bundle must RENDER that mismatch, so the
operator sees "the runner wrote a format I don't read" instead of an empty all-quiet field.

Like the other field guards (issues #22/#27/#30/#32/#35/#38), these are STRING checks on the shipped
static bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit drops
the seam. The rendered proof that it LOOKS right lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_field_binder_reads_the_state_format_verdict():
    # field.js binds the server's verdict — it never re-derives compatibility (design record B.1).
    assert "state_format" in _FIELD, "field.js must read repo.state_format"
    assert "compatible" in _FIELD, (
        "field.js must gate the mismatch card on repo.state_format.compatible")


def test_field_mounts_the_format_mismatch_card():
    assert "fld-fmt" in _FIELD, "field.js must mount the state-format-mismatch overlay card (fld-fmt)"


def test_field_binds_the_server_built_mismatch_message():
    # The message NAMES the versions and is built server-side (test_flights_state_format.py); the JS
    # only binds it. Assert the card carries a `.message` onto its own `.m` element rather than a
    # hardcoded string, so the version numbers a real mismatch names actually reach the screen.
    assert re.search(r"\.message\b", _FIELD), (
        "field.js must render the server-built state_format.message (the version-naming line)")
    assert re.search(r"querySelector\(\s*['\"]\.m['\"]\s*\)", _FIELD), (
        "field.js must set the mismatch card's .m element from the server message")


def test_css_styles_the_format_mismatch_card():
    assert ".fld-fmt" in _CSS, "shell.css must style .fld-fmt — the state-format-mismatch field card"


def test_mismatch_surface_names_itself_in_plain_words():
    # Costume rule 2 (design §3): the metaphor never hides the words you read. The card must say, in
    # plain words a screen-reader and a novice both parse, that this is a state-FORMAT mismatch.
    assert re.search(r"format", _FIELD, re.IGNORECASE), (
        "the mismatch card must name the state-format condition in plain words, never a bare icon")
