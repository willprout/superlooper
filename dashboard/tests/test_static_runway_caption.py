"""Guard (issue #35): the empty-queue caption reflects the repo's REAL lane count, never a hardcode.

The empty board used to read a literal "2 RUNWAYS OPEN" on every repo — a false factual claim for a
repo running one lane (or three). The lane count now travels server → snapshot → caption
(``flights.empty_queue_caption`` → ``repo["queue_empty_caption"]``); the JS only BINDS that finished
string (design record B.1 — semantics server-side, pixels client-side).

Like the tower-scroll and paging guards, these are STRING checks on the shipped static bundle (the
repo runs no JS engine — Python stdlib only). They fail CI if a future edit reintroduces a hardcoded
runway count or drops the binding seam. The rendered proof that the caption LOOKS right (16-bit
styling intact, joy included) lives in the PR's screenshot evidence.
"""
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_BOARDS = (_STATIC / "boards.js").read_text(encoding="utf-8")


def test_no_hardcoded_runway_count_survives_in_the_shipped_js():
    # "· 2 RUNWAYS OPEN" was the lie. Neither the plural nor the singular literal may reappear in the
    # two caption sites — the count is the server's, bound as a whole string.
    for name, src in (("shell.js", _SHELL), ("boards.js", _BOARDS)):
        assert "RUNWAYS OPEN" not in src, (
            "%s still hardcodes a plural runway count — the caption must bind the server's "
            "queue_empty_caption (issue #35), never a literal 'N RUNWAYS OPEN'" % name)
        assert "RUNWAY OPEN" not in src, (
            "%s still hardcodes a singular runway count — bind queue_empty_caption instead" % name)


def test_field_caption_binds_the_servers_queue_empty_caption():
    # shell.js's field head shows the empty-queue caption straight from the repo snapshot.
    assert "queue_empty_caption" in _SHELL, (
        "shell.js must bind r.queue_empty_caption for the empty-queue field caption (issue #35)")


def test_departures_board_binds_the_servers_queue_empty_caption():
    # boards.js's empty departures board renders the same server-supplied caption (threaded in from
    # the shell as the departuresInner emptyCaption argument), never a literal.
    assert "emptyCaption" in _BOARDS, (
        "boards.js must render the server's queue_empty_caption (via the emptyCaption argument) for "
        "the empty departures board (issue #35)")
