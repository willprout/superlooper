"""Guard (issue #32): queued flights must render as planes parked at the gates.

The departures queue (open ``agent-ready`` issues not yet flying) is the design's "at the stand
(approved, queued)" circuit stage (§3). The server now projects the launchable front of that queue
to ``repo.stand`` (a plane per gate, tested in ``test_snapshot.py``); the field binder must actually
draw them. The N-flight engine already anchors an ``at-stand`` plane at the west gates
(``BAYS_STAND`` in airfield_live.js) and draws it clean (no chocks, not dimmed) — visually distinct
from the parked "gave up" plane at the east gates with its chocks + "MX REQ" tag. The two remaining
seams are: field.js feeding ``repo.stand`` into the engine as ``at-stand`` flights, and a positive
"queued / at the stand" tag so a healthy waiting plane never reads like the stalled parked one.

Like the boards-paging (issue #30) and tower-scroll (issue #27) guards, these are STRING guards on
the shipped static bundle, not behavioural tests — the repo runs no JS engine (Python stdlib only).
They fail CI if a future edit drops the seam that puts queued planes on the field. The rendered proof
that it LOOKS right (queued planes at the gates, joy included) lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

import server   # the server owns STAND_BAYS; the engine's BAYS_STAND must agree with it

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# =============================== the server↔engine gate-count contract ===============================

def test_stand_bay_count_matches_the_server_cap():
    # The server caps repo.stand to STAND_BAYS; the engine parks each queued plane at a BAYS_STAND slot.
    # If the two drift, the field silently under-renders the queue or clamps overlapping planes onto the
    # last bay — with no CI noise. Pin them together (the same discipline that ties solari MAX_ROWS to
    # the server's cap_arrivals page_size in test_static_boards_paging.py).
    m = re.search(r"BAYS_STAND\s*=\s*\[([^\]]*)\]", _LIVE)
    assert m, "airfield_live.js must define BAYS_STAND (the west gate anchors)"
    bays = [x for x in m.group(1).split(",") if x.strip()]
    assert len(bays) == server.STAND_BAYS, (
        "airfield_live.js BAYS_STAND has %d gates but server.STAND_BAYS is %d — the field and the "
        "server cap disagree on how many queued planes fit at the gates" % (len(bays), server.STAND_BAYS))


# =============================== field.js binds the stand into the engine ===============================

def test_field_reads_the_stand_projection():
    # The queued planes come from the server's projection (design B.1: the field binds, never derives).
    assert re.search(r"\brepo\.stand\b", _FIELD), (
        "field.js must read repo.stand — the server's queued-flights-at-the-gates projection (issue #32)")


def test_field_maps_stand_rows_to_at_stand_flights():
    # Each stand row becomes an engine flight at the at-stand anchor. The stage string is the engine's
    # contract (airfield_live.js placementOf → anchorFor('at-stand') → BAYS_STAND).
    assert "at-stand" in _FIELD, "field.js must give queued planes the 'at-stand' stage"
    # A queued flight has no running session yet, so it never trails a contrail (honest liveness, §5).
    assert re.search(r"contrail[\"']?\s*[:=]\s*[\"']none[\"']", _FIELD), (
        "a queued plane at the stand has no session yet → contrail 'none'")


def test_field_adds_stand_flights_to_the_engine_flights():
    # The synthesized at-stand flights must reach engine.update alongside the real on-field flights —
    # merged into one flights array, not a separate render path the engine never sees.
    assert re.search(r"concat\(\s*stand", _FIELD), (
        "field.js must concat the stand-derived flights into the flights array handed to the engine")


# =============================== the engine tags a queued plane as healthy-and-waiting ===============================

def test_engine_marquees_the_stand_in_plain_words():
    # The west gates sit ~32px apart — too close for a text tag per plane — so the queued line gets ONE
    # calm marquee naming the §3 stage ("N AT THE STAND"), distinct from the parked plane's "MX REQ".
    assert re.search(r"stage\s*===\s*['\"]at-stand['\"]", _LIVE), (
        "airfield_live.js must special-case the at-stand stage to gather the queued planes")
    assert re.search(r"kind:\s*['\"]stand['\"]", _LIVE), (
        "the stand marquee must use the 'stand' tag kind (its own positive style, not 'mx')")
    assert "AT THE STAND" in _LIVE, (
        "the marquee must name the stage in plain words — the §3 'at the stand' (approved, queued)")


def test_at_stand_plane_is_not_drawn_as_parked():
    # Distinctness is load-bearing (DoD): a queued plane must never inherit the parked plane's chocks
    # or dimming. Those are keyed strictly to the 'parked'/'session-frozen' stages, never 'at-stand'.
    assert re.search(r"chocks:\s*f\.stage\s*===\s*['\"]parked['\"]", _LIVE), (
        "chocks belong to the parked plane only — a queued plane at the stand shows none")


# =============================== the queued tag has its own calm, positive style ===============================

def test_css_has_a_positive_stand_tag_style():
    assert ".fld-tag.stand" in _CSS, (
        "shell.css must style .fld-tag.stand — the queued plane's healthy 'waiting' label, distinct "
        "from the ochre 'MX REQ' (.fld-tag.mx) of the parked plane")
