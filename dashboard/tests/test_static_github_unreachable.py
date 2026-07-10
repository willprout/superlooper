"""Guard (issue #38): GitHub-unreachable renders as its OWN distinct, honest state on the field and
boards — a dark tower / lost data link — never the cheerful all-clear it used to be confused with.

When ``gh`` is missing or unauthenticated every GitHub read fails closed to empty, and a quiet field
used to render "QUEUE EMPTY … all clear," indistinguishable from genuinely having no work. The
server now tells "gh answered: nothing there" from "gh unavailable/refused" (``repo.github`` +
``snapshot.github``, pinned in test_snapshot.py); the static bundle must render that distinction.

Like the other field guards (issues #22/#27/#30/#32/#35), these are STRING checks on the shipped
static bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit drops
the seam that keeps the unreachable state distinct from the genuine empty state. The rendered proof
that it LOOKS right — the dark tower sweeping for a signal, joy included — lives in the PR's
screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_BOARDS = (_STATIC / "boards.js").read_text(encoding="utf-8")
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_departures_board_binds_the_unreachable_state():
    # boards.js must read the server's unreachable flag and render a DISTINCT empty-board state, not
    # the queue-empty caption — "QUEUE EMPTY" over an unread queue is exactly the false claim (#38).
    assert "unreachable" in _BOARDS, "boards.js must bind the github-unreachable flag"
    assert "link-lost" in _BOARDS, (
        "boards.js must render a distinct .board-empty.link-lost state when GitHub is unreachable, "
        "never the queue-empty caption")


def test_field_legend_binds_the_unreachable_state():
    # The field-head legend must never show the empty-queue caption when GitHub is unreachable — the
    # queue is unread, not empty. shell.js binds repo.github.unreachable to a distinct legend.
    assert "github" in _SHELL, "shell.js must read repo.github"
    assert "unreachable" in _SHELL, (
        "shell.js must bind repo.github.unreachable so the field legend never shows a false all-clear")


def test_field_binder_forwards_the_link_state_and_mounts_the_card():
    # field.js maps repo.github.unreachable onto the engine model as a link state (the tower goes
    # dark) AND mounts the plain-words lost-data-link card. B.1: the binder only forwards; the server
    # decided the state.
    assert "github" in _FIELD, "field.js must read repo.github"
    assert re.search(r"link\s*:", _FIELD), "field.js must pass a `link` state to the engine model"
    assert "fld-link" in _FIELD, "field.js must mount the lost-data-link overlay card (fld-link)"


def test_airfield_engine_darkens_the_tower_when_the_link_is_lost():
    # The flagship delight (issue #38 / design §0.1): when the data link to GitHub is lost, the tower
    # beacon goes DARK and sweeps for a signal — a distinct beacon treatment, never the ok/attention/
    # alert light, and never a red alarm (this is "can't see," not "broken").
    assert "model.link" in _LIVE, "airfield_live.js towerFX must read model.link"
    assert re.search(r"link\s*===\s*['\"]lost['\"]", _LIVE), (
        "towerFX must special-case a lost data link with its own dark/searching beacon")


def test_css_styles_the_unreachable_field_card_and_board_state():
    assert ".fld-link" in _CSS, "shell.css must style .fld-link — the lost-data-link field card"
    assert ".board-empty.link-lost" in _CSS, (
        "shell.css must style .board-empty.link-lost — the departures board's dark data-link state")


def test_unreachable_state_names_itself_in_plain_words():
    # Costume rule 2 (design §3): the metaphor never hides the words you read. The dark tower must
    # still say, in plain words a screen-reader and a novice both parse, that GitHub can't be reached.
    combined = _FIELD + _BOARDS + _SHELL
    assert re.search(r"can.?t reach github|no data link|github unreachable", combined, re.IGNORECASE), (
        "the unreachable surfaces must name the state in plain words, never a bare icon")
