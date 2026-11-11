"""Guard (issue #545): a ``consulting`` flight must render as its OWN state on the field and in the drawer.

The truth layer returns a seventh off-path state, ``consulting`` (flights.CONSULTING): a worker
exited cleanly on a question and a fork of the session that APPROVED the issue is considering it
before William is asked (#538). It needs nothing from him yet — so it must never borrow the amber
"awaiting your decision" look (that is the Questions card's state), never the grey dimmed hull of a
dead session, and never fly as an ordinary plane on the leg with a BUILDING cloth.

Like the other field guards (issues #22/#27/#30/#32) these are STRING guards on the shipped static
bundle — the repo runs no JS engine (Python stdlib only). The rendered proof that it LOOKS right, joy
included, lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_DRAWER = (_STATIC / "drawer.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")

_AMBER = "#C77E1F"
_GREY = "#8A93A0"


def test_a_consulting_plane_is_held_at_its_underlying_circuit_position():
    # Like awaiting/session-frozen/stranded: the plane sits where the worker left off (§5 — in place,
    # never teleported), not at the default mid-field anchor an unknown stage falls to.
    m = re.search(r"function placementOf[\s\S]*?\n\s*\}", _LIVE)
    assert m, "airfield_live.js must define placementOf"
    assert "'consulting'" in m.group(0), (
        "placementOf must route 'consulting' to f.circuitStage (the mirror of "
        "flights._PLACED_AT_CIRCUIT is pinned in test_static_field_landmarks)")


def test_a_consulting_plane_is_not_dimmed_like_a_dead_session():
    m = re.search(r"var dim = ([^;]*);", _LIVE)
    assert m, "airfield_live.js must compute a `dim` flag for hulls"
    assert "consulting" not in m.group(1), (
        "a question being considered is not a dead session — its hull must stay solid")


def test_a_consulting_plane_never_wears_the_amber_ring():
    m = re.search(r"if \(([^)]*)\) amberRing\(", _LIVE)
    assert m, "airfield_live.js must still draw the amber ring for an owner decision"
    assert "consulting" not in m.group(1), (
        "the amber ring means 'your decision' — a consult has not asked William anything")


def test_a_consulting_plane_gets_its_own_tag_in_plain_words():
    assert re.search(r"stage\s*===\s*'consulting'", _LIVE), (
        "airfield_live.js must special-case the 'consulting' stage to emit its own tag")
    assert re.search(r"kind:\s*'consult'", _LIVE), (
        "the consulting tag must use its own 'consult' kind, never 'amber' (your decision)")
    assert "CONSULTING APPROVER" in _LIVE, (
        "the tag must name what is happening in words the owner reads at a glance")


def test_a_consulting_plane_is_on_the_radio():
    # Joy (§0.1), honestly: the design record maps a blocked worker to a RADIO CALL (§3), and that is
    # exactly what this is — the plane calling the session that cleared it. Calm waves, not amber.
    assert re.search(r"function radioWaves\(", _LIVE), "airfield_live.js must define radioWaves"
    assert re.search(r"stage\s*===\s*'consulting'\)\s*radioWaves\(", _LIVE), (
        "a consulting plane must draw its radio waves")


def test_stage_color_has_a_calm_consulting_entry():
    m = re.search(r'["\']consulting["\']\s*:\s*["\'](#[0-9A-Fa-f]{6})["\']', _SHELL)
    assert m, "shell.js STAGE_COLOR must map 'consulting' to a hex color"
    assert m.group(1).upper() not in (_AMBER, _GREY), (
        "consulting must be neither the amber of an owner decision nor the grey of a dead session")


def test_css_styles_the_consulting_tag_and_drawer_note():
    assert ".fld-tag.consult" in _CSS, "shell.css must style .fld-tag.consult"
    # The drawer's off-path note is AMBER by default — unstyled, a consult would read as "your
    # decision". It must carry its own override.
    m = re.search(r"\.drawer-offpath\.state-consulting\s*\{([^}]*)\}", _CSS)
    assert m, "shell.css must style .drawer-offpath.state-consulting, overriding the amber default"
    assert "amber" not in m.group(1)


def test_the_drawer_quotes_the_question_being_considered():
    assert "off_path.question" in _DRAWER, (
        "drawer.js must render the question the approving session is considering — it is posted "
        "nowhere else until the consult decides")


def test_the_consulting_tag_hangs_above_the_hull_clear_of_the_towed_cloths():
    # Measured in the browser while building #545: hung BELOW the hull (where the amber tag goes),
    # the tag landed exactly on the towed-banner lane (#204's cloths fly just under the leg) and
    # garbled a neighbour's "SL-540 · BUILDING" cloth. Above the hull — where the session-frozen tag
    # already sits — the strip is clear.
    m = re.search(r"kind:\s*'consult',\s*x:\s*anchor\.x,\s*y:\s*anchor\.y\s*([-+])\s*box\.h", _LIVE)
    assert m, "the consult tag must be placed relative to its anchor and hull height"
    assert m.group(1) == "-", "the consult tag must hang ABOVE the hull, clear of the cloth lanes"
    css = re.search(r"\.fld-tag\.consult\s*\{([^}]*)\}", _CSS)
    assert css and "translate(-50%, 0)" not in css.group(1), (
        "an above-hull tag keeps the default upward transform — translate(-50%, 0) would drop it "
        "back down onto the hull")
