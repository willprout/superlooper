"""Guard (issue #22): a stranded gate must render as its OWN distinct plane at the gate.

The truth layer now returns a sixth off-path state — ``stranded`` (flights.STRANDED): a finished
session (report filed, status ``gating``) whose GATE stopped advancing. Its whole point is to be
LEGIBLE at a glance and unmistakably NOT the grey, dimmed "dead session" (session-frozen): the
work completed, so the plane sits solid at the gate while the tag/drawer point the owner at the
runner. The server already names it on the pill, banner, drawer, and boring table (tested in
test_snapshot.py / test_cards.py); the field binder (airfield_live.js) and the pixel maps
(shell.js / shell.css) must draw it distinctly.

Like the other field guards (issues #27/#30/#32), these are STRING guards on the shipped static
bundle — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit drops the
seam that keeps a stranded gate visually distinct from a dead session. The rendered proof that it
LOOKS right (a solid plane held at the gate, joy included) lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# =============================== the plane is held AT the gate (never teleported) ===============================

def test_stranded_plane_renders_at_its_underlying_final_position():
    # Like awaiting/session-frozen, a stranded flight renders AT its honest circuit position (final —
    # the gate), not at the default mid-field air anchor. placementOf must route 'stranded' to
    # circuitStage, so a stranded gate sits ON the gate threshold where its problem actually is (§5).
    m = re.search(r"placementOf[\s\S]*?\n\s*\}", _LIVE)
    assert m, "airfield_live.js must define placementOf"
    body = m.group(0)
    assert "stranded" in body, (
        "placementOf must special-case 'stranded' so it renders at its circuitStage (the gate), "
        "never the default mid-field anchor")
    assert re.search(r"stranded['\"]\s*\)\s*return\s+f\.circuitStage", body), (
        "a stranded flight must render at f.circuitStage (final = the gate), like awaiting/frozen")


# =============================== it is NOT the grey, dimmed dead session ===============================

def test_stranded_plane_is_not_dimmed_like_a_dead_session():
    # Distinctness is the whole DoD: a stranded gate's SESSION finished, so its hull must stay solid —
    # it must NEVER inherit the grey dimming reserved for the dead 'session-frozen'/'parked' planes.
    m = re.search(r"var dim = ([^;]*);", _LIVE)
    assert m, "airfield_live.js must compute a `dim` flag for hulls"
    assert "stranded" not in m.group(1), (
        "'stranded' must NOT be in the dim condition — a finished flight at the gate stays solid, "
        "never greyed out like a dead session (issue #22 distinctness)")


def test_stranded_plane_gets_its_own_field_tag():
    # A distinct tag names the state in plain words and points at the gate — its own tag kind, never
    # borrowing the 'frozen' (dead session) tag.
    assert re.search(r"stage\s*===\s*['\"]stranded['\"]", _LIVE), (
        "airfield_live.js must special-case the 'stranded' stage to emit its own tag")
    assert re.search(r"kind:\s*['\"]stranded['\"]", _LIVE), (
        "the stranded tag must use its own 'stranded' tag kind, distinct from 'frozen'")
    assert "STRANDED AT GATE" in _LIVE or "STRANDED AT THE GATE" in _LIVE, (
        "the tag must name the state in plain words the owner reads at a glance")


# =============================== the pixel maps carry a distinct 'stranded' entry ===============================

def test_stage_color_has_a_distinct_stranded_entry():
    # shell.js STAGE_COLOR is the one client-side stage->color binding. A stranded gate needs its OWN
    # color — never the grey (#8A93A0) it shares with a dead/landed plane, or the distinction is lost.
    m = re.search(r'["\']stranded["\']\s*:\s*["\'](#[0-9A-Fa-f]{6})["\']', _SHELL)
    assert m, "shell.js STAGE_COLOR must map 'stranded' to a hex color"
    assert m.group(1).upper() != "#8A93A0", (
        "stranded must not reuse the grey dead-session/landed color — it must read as its own state")


def test_css_styles_the_stranded_tag_and_drawer():
    assert ".fld-tag.stranded" in _CSS, (
        "shell.css must style .fld-tag.stranded — the stranded gate's own field tag")
    assert ".drawer-offpath.state-stranded" in _CSS, (
        "shell.css must style .drawer-offpath.state-stranded — the drawer's stranded note, distinct "
        "from the blue-grey session-frozen note")
