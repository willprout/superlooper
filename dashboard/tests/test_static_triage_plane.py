"""Issue #451 — the triage flight's own airframe on the field, and its card under it.

The owner asked for "a special looking airplane", and the precedent to honour is that a debugger
launch already renders in its own words rather than as a build flight. A `t<N>` is the third session
class the launcher spawns and it is not a lane: it opens no branch, files no PR and owns no runway
(#463). So it must not be a differently-coloured build flight — it is a different aeroplane, flying
a track that is not the circuit.

What ships:

* **The airframe.** ``Airfield3.live.planeAwacs`` — the rotodome hull the sprite sheet has always
  carried and nothing has ever flown. A survey aircraft, in the field's own 16-bit vocabulary.
* **The track.** A flat racetrack across the runway corridor, outside every circuit anchor. Not a
  stage, not a runway, not the holding pattern — and deliberately unpainted, because the hold's
  drawn ring means "stacked, waiting to land" and the survey is not landing.
* **The card.** Under the field, bound wholly from ``lib/triage.run``.

Like the stand-planes (#32), boards-paging (#30) and tower-scroll (#27) guards, these are STRING
guards on the shipped static bundle, not behavioural tests — this repo runs no JS engine (Python
stdlib only). They fail CI if a future edit drops a seam. The rendered proof that it LOOKS right is
the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_A3 = (_STATIC / "airfield3.js").read_text(encoding="utf-8")
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# =============================== field.js binds the server's block ===============================

def test_field_reads_the_servers_triage_block():
    assert re.search(r"\brepo\.triage\b", _FIELD), (
        "field.js must read repo.triage — the server's derivation of the day's flight (issue #451)")


def test_a_plane_is_drawn_only_when_the_server_says_it_belongs_on_the_field():
    # `on_field` is the server's fail-closed verdict: a launch the ENGINE confirmed, on a flight it
    # NAMED, whose run has not closed. Deriving that here — from a launch record, from a contrail,
    # from anything — would put an aircraft on the field for a session that may not exist.
    assert re.search(r"tri\s*&&\s*tri\.on_field", _FIELD), (
        "the survey plane must be gated on the server's on_field flag, never on a local guess")


def test_the_survey_flight_carries_its_own_kind_and_stage():
    assert re.search(r"kind:\s*['\"]triage['\"]", _FIELD), (
        "field.js must mark the survey flight kind:'triage' — the ONLY thing that separates a "
        "flight's t7 from a lane's i7, which carry the same number")
    assert re.search(r"stage:\s*['\"]survey['\"]", _FIELD)


def test_the_survey_plane_reaches_the_engine_in_the_one_flights_array():
    assert re.search(r"concat\(\s*standFlights\s*,\s*surveyFlights\s*\)", _FIELD), (
        "the survey must be merged into the flights array handed to the engine, not drawn through "
        "a second render path the engine never sees")


def test_the_survey_never_wears_the_repos_airline_colours_from_the_binder():
    # It is the LOOP's own aircraft, not this repo's — so the binder must not hand it `tail`.
    survey = re.search(r"surveyFlights\s*=.*?\];", _FIELD, re.S)
    assert survey, "field.js must build surveyFlights"
    assert re.search(r"tail:\s*null", survey.group(0)), (
        "a flight belongs to no airline — its plane must not be handed the repo's tail colour")


# =============================== the engine flies it as its own airframe ===============================

def test_the_engine_has_a_survey_placement_outside_the_circuit():
    assert re.search(r"case\s*['\"]survey['\"]", _LIVE), (
        "airfield_live.js must anchor the 'survey' placement (issue #451)")
    assert re.search(r"stage\s*===\s*['\"]survey['\"]", _LIVE), (
        "placementOf must route a survey flight to its own track, never to a circuit stage")
    for stage in ("downwind", "final", "touchdown", "at-stand", "parked"):
        assert not re.search(r"case '%s':\s*//[^\n]*survey" % stage, _LIVE), (
            "the survey must never share a circuit stage's anchor — it flies no circuit")


def test_the_survey_track_is_its_own_ellipse_with_its_own_rate():
    assert re.search(r"var\s+SURVEY\s*=\s*\{[^}]*cx[^}]*cy[^}]*rx[^}]*ry[^}]*\}", _LIVE), (
        "the survey's track must be a named ellipse, not magic numbers in the anchor")
    assert "SURVEY_RATE" in _LIVE, (
        "the survey has its OWN angular rate — a holder's brisk circuit reads as impatience, and a "
        "flight is not queueing to land")
    assert re.search(r"solo:\s*true", _LIVE), (
        "the survey's orbit must be marked solo: it takes no holding slot phase and shares no base")


def test_the_survey_is_drawn_with_the_awacs_hull():
    assert re.search(r"awacs\s*\?\s*A\.live\.planeAwacs", _LIVE), (
        "a triage flight must be drawn with the rotodome airframe — a different aeroplane at a "
        "glance, not a recoloured build flight (the owner's 'special looking airplane')")
    assert "planeAwacs" in _A3 and re.search(r"planeAwacs:\s*planeAwacs", _A3), (
        "airfield3 must export planeAwacs into the live namespace the engine composes from")
    assert "SURVEY_TAIL" in _LIVE, (
        "the survey wears its own livery — neither the repo's airline colour nor the default one")


def test_the_holding_ring_is_never_painted_around_the_survey_track():
    # The drawn ring means "aircraft are stacked here waiting to land". A second one, five times the
    # size, over the whole field would say exactly that about the one aircraft that is not landing.
    assert re.search(r"s\.target\.orbit\.solo\s*\|\|\s*holdDrawn", _LIVE), (
        "the racetrack draw must skip a solo (survey) orbit")


def test_the_survey_carries_a_label_pinned_to_its_track():
    assert re.search(r"kind:\s*['\"]survey['\"]", _LIVE), "the survey tag needs its own tag kind"
    assert "TRIAGE SURVEY" in _LIVE, "the tag must name the job in plain words"
    assert "SURVEY_TAG" in _LIVE, (
        "the label is pinned to the TRACK, not the hull — a plane that never stops moving cannot "
        "carry a legible label on its back")
    assert re.search(r"\.fld-tag\.survey\b", _CSS), "the survey tag needs its own style"


# =============================== t7 and i7 are different aircraft ===============================

def test_sprites_are_keyed_by_a_key_that_separates_a_flight_from_a_lane():
    assert re.search(r"function keyOf\(", _LIVE), (
        "airfield_live.js must key sprites through keyOf — `t7` and `i7` carry the same number, and "
        "keying either by the bare number makes one silently replace the other on the field")
    assert re.search(r"sprites\[key\]", _LIVE)


def test_no_sprite_map_is_still_read_through_map_number():
    # The old `Object.keys(sprites).map(Number)` cannot survive a non-numeric key: it yields NaN,
    # NaN sorts nowhere, and `sprites[NaN]` is undefined — a crash inside the draw loop, every frame.
    assert "Object.keys(sprites).map(Number)" not in _LIVE, (
        "a sprite key is no longer always numeric — Object.keys(sprites).map(Number) is a NaN trap")


def test_a_survey_plane_opens_no_flight_card():
    # It has no lane, no branch, no PR and no drawer (#463). A tap that opened issue #7's card
    # because the flight is called `t7` is the same phantom the tower's own chips refuse.
    assert re.search(r"flight\.kind\s*===\s*['\"]triage['\"]\s*\)\s*continue", _LIVE), (
        "hitTest must SKIP a survey plane (not return null), so a lane plane under it stays tappable")


# =============================== the card under the field ===============================

def test_the_shell_renders_the_triage_card_only_when_a_flight_flew():
    assert "triageHTML" in _SHELL, "shell.js must render the triage card (issue #451)"
    assert re.search(r"if\s*\(!t\s*\|\|\s*!t\.present\)\s*return\s*\"\"", _SHELL), (
        "no flight ⇒ NO CARD — the honest silence the morning report keeps on a quiet day, and what "
        "makes 'the board is exactly as it was' true for every repo that has not opted in")
    assert re.search(r"triageHTML\(r\)", _SHELL), "the card must be placed in the field panel"


def test_the_card_binds_the_servers_words_and_derives_none_of_its_own():
    card = re.search(r"function triageHTML\(r\)\s*\{.*?\n  \}", _SHELL, re.S)
    assert card, "shell.js must define triageHTML"
    body = card.group(0)
    for field in ("t.headline", "t.text", "t.escalated", "t.counts_note", "t.state"):
        assert field in body, "the card must bind %s from the server (design B.1)" % field
    # No sentence-building, no arithmetic on the counts: those are lib/triage's, so the card, the
    # run log and the morning report can never disagree about one night's work.
    assert "judged" not in body and "escalated ·" not in body, (
        "the card must not compose its own tally — the flight's own words are what it shows")


def test_the_escalation_chip_is_never_shown_at_zero():
    card = re.search(r"function triageHTML\(r\)\s*\{.*?\n  \}", _SHELL, re.S).group(0)
    assert re.search(r"t\.escalated\s*>\s*0", card), (
        "a standing '0 FOR YOU' is furniture; the chip is a call on attention or it is absent")


def test_the_card_has_its_own_style_including_the_launch_that_never_flew():
    assert ".triage-card" in _CSS
    assert ".triage-card.is-no-flight" in _CSS, (
        "a launch that landed no session is a different state and must not read as a run that flew")
