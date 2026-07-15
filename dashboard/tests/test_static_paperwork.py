"""Guard (issue #138): a flight the RUNNER would refuse must render as its OWN honest state.

The departures board promises the real launch order. An issue whose labels the runner refuses
(a missing/unknown/doubled ``type:``, a doubled or blank ``model:``/``effort:``) is one it will
silently never launch — so the board must never paint it launchable, never crown it NEXT OFF THE
STAND, and never park it at a gate. It gets its own signage instead: a PAPERWORK chip that names
the bad label in plain words, where the owner is already reading (design record §0.3,
tap-where-you-read).

It is signage, not an error slab (§0.1 — joy is a terminal requirement): the row keeps the board's
grid, flaps and split-flap voice, and reads in the airport's own amber — the colour this dashboard
already uses for "an owner decision waits" (§5). Ground ops really does hold a flight whose
dispatch paperwork is wrong; that is what this is.

Like the other field guards (issues #22/#27/#30/#32), these are STRING guards on the shipped static
bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit drops the
seam that keeps a refused flight distinct from a queued or an awaiting one. The rendered proof that
it LOOKS right (joy included) lives in the PR's screenshot evidence.
"""
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_BOARDS_JS = (_STATIC / "boards.js").read_text(encoding="utf-8")
_BOARDS_CSS = (_STATIC / "boards.css").read_text(encoding="utf-8")


# =============================== the JS binds the server's verdict, and computes none of it ===============================

def test_the_board_binds_the_servers_paperwork_status():
    # The verdict is the tested server's (design record B.1). The JS must READ `status`, never
    # re-derive "is this launchable" from labels of its own.
    assert '"paperwork"' in _BOARDS_JS
    assert "d.status" in _BOARDS_JS


def test_a_paperwork_row_shows_the_paperwork_chip():
    assert "PAPERWORK" in _BOARDS_JS


def test_a_paperwork_row_is_marked_as_its_own_row_state():
    # Its own class — never reusing `awaiting`, which means something else entirely (a connection
    # that hasn't landed yet). Two different reasons a flight can't leave, never collapsed (§5).
    assert "dep-row" in _BOARDS_JS and "paperwork" in _BOARDS_JS


def test_the_plain_words_reason_reaches_the_dom():
    # `refusal_text` is the server's plain sentence — it names the offending label and how to fix
    # it. It must reach the row (hover title + the screen-reader span), or the owner cannot act
    # from where they read it. The JS never composes the sentence; it binds it.
    assert "refusal_text" in _BOARDS_JS


def test_a_paperwork_flight_is_never_offered_the_expedite_bump():
    # ⚡ is a priority signal, not a paperwork fix: the runner refuses the issue either way, so a
    # "bump to the top of the launch order" button there would be a lie. The button is gated on
    # `launchable`, which the server sets false for every refused flight.
    assert "var expBtn = launchable" in _BOARDS_JS


# =============================== the pixels keep it airport signage, not an error slab ===============================

def test_the_paperwork_chip_has_its_own_colour():
    assert ".dep-status.paperwork" in _BOARDS_CSS


def test_the_paperwork_row_has_its_own_style():
    assert ".dep-row.paperwork" in _BOARDS_CSS


def test_paperwork_is_visually_distinct_from_next_and_queued_and_awaiting():
    # The whole point is legibility at a glance: a refused flight must not read as the gold flight
    # that is genuinely next, nor blend into the dim queued/awaiting greys.
    colours = {}
    for state in ("next", "queued", "await", "paperwork"):
        for line in _BOARDS_CSS.splitlines():
            if line.strip().startswith(".dep-status.%s " % state) or \
               line.strip().startswith(".dep-status.%s{" % state):
                colours[state] = line.split("color:")[1].split(";")[0].strip().lower()
                break
    assert set(colours) == {"next", "queued", "await", "paperwork"}, colours
    assert len(set(colours.values())) == 4, colours          # four states, four distinct colours
