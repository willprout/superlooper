"""Issue #310 — the Open-session-window button on the flight card (the shipped static bundle).

Owner ruling 2026-07-30 (docs/HERDR-ADOPTION-PLAN.md §9): the dashboard's live-view ambition is
replaced by a BUTTON on the card that opens that session's herdr window — attach, which is proven —
and **no observe-stream plumbing**. These are the guards on that shape.

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle —
the same discipline as ``test_static_tidy.py`` / ``test_static_restart.py``. They exist so a future
edit that derives the host name in the pixels, offers the button on a flight with no session, opens
a frame/stream, or swallows a failure fails CI instead of quietly shipping. The rendered proof that
it LOOKS right lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_DRAWER_JS = (_STATIC / "drawer.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_the_card_renders_the_open_session_window_button():
    assert 'data-act="session-window"' in _DRAWER_JS, (
        "the flight card must carry the Open-session-window verb button")
    assert re.search(r"data-act=\"session-window\"[\s\S]{0,200}data-repo", _DRAWER_JS), (
        "the button must carry the repo + num the verb targets, like every other card verb")


def test_the_button_is_offered_only_when_the_flight_has_a_session():
    # The server decided this (cards._session); the pixels must READ it, never re-derive it from a
    # stage. A button that could only ever fail is a lie about what the surface can do.
    assert re.search(r"\bd\.session\b", _DRAWER_JS), (
        "the card must read the server's session decision")
    assert re.search(r"session[\s\S]{0,120}\.present", _DRAWER_JS), (
        "the button must be gated on the server's session.present")


def test_the_host_name_is_the_servers_never_derived_in_the_pixels():
    # One spelling of the host address (B.1): the card shows session.name, and nothing here builds
    # an "i" + num string of its own — two spellings is how a display and a verb drift apart.
    assert re.search(r"session[\s\S]{0,200}\.name", _DRAWER_JS), (
        "the card must show the server's session.name")
    assert not re.search(r"[\"']i[\"']\s*\+\s*", _DRAWER_JS), (
        "drawer.js must not build the host agent name itself — it is the server's (cards._session)")


def test_the_button_posts_the_one_step_verb():
    assert re.search(r"[\"']/api/session-window[\"']", _SHELL_JS), (
        "shell.js must POST to /api/session-window")
    assert 'act === "session-window"' in _SHELL_JS, (
        "the shell's verb dispatcher must handle the session-window act")


def test_no_observe_stream_plumbing_was_added():
    # The ruling's boundary, spelled as a test: attach only. Nothing in the shipped bundle may open
    # a frame stream, a websocket, or a live pane read to show a session's screen.
    for js in (_DRAWER_JS, _SHELL_JS):
        assert "WebSocket" not in js, "no live-view stream — the ruling is attach only"
        assert "EventSource" not in js, "no live-view stream — the ruling is attach only"
    assert not re.search(r"/api/(observe|frames|stream|pane)", _DRAWER_JS + _SHELL_JS), (
        "no observe/frame/pane stream endpoint may be added — the ruling is attach only")


def test_a_failed_open_is_shown_honestly_never_swallowed():
    # The host may have no window for this flight (a session that exited, or one already tidied).
    # That is the COMMON answer and it must reach the owner in the host's own words.
    assert re.search(r"session-window[\s\S]{0,900}?toast\(", _SHELL_JS), (
        "the session-window handler must report its outcome to the owner")
    assert re.search(r"doSessionWindow[\s\S]{0,900}?\berr\b", _SHELL_JS), (
        "a failure must be toasted as an error, never as a success")


def test_the_button_never_writes_to_github():
    # It is a local-command verb like Tidy/Restart: it must not ride the GitHub verb helper, whose
    # toast claims a label landed.
    assert not re.search(r"session-window[\s\S]{0,120}postVerb", _SHELL_JS), (
        "session-window is a LOCAL command, not a GitHub write — it must not go through postVerb")


def test_the_button_has_a_style_of_its_own():
    assert "drawer-session" in _CSS, "the Open-session-window button needs its own style hook"


def test_the_dashboard_names_no_host_of_its_own():
    # Issue #310's DoD: no dashboard code references cmux once the mini lane is the target. This is
    # the ratchet, and it covers the whole product — the shipped pixels, the pure cores, and the two
    # entry points. The dashboard has exactly ONE place that may name the session host at all
    # (lib/session_window.py, its single doorway, mirroring the engine's lib/session_host.py), and
    # even that one names herdr rather than cmux.
    #
    # Two exclusions, both deliberate. `tests/` may say cmux: the conftest neutralizes a binary
    # literally called cmux, and the fakes model the CLI's real output. `docs/` + `design/` may too:
    # they are the settled design record and its source uploads — history, not claims the product
    # makes today.
    #
    # Note the language the sweep left behind is HOST-NEUTRAL ("its own session window"), not
    # herdr-flavoured. The three-spawners migration (#308) has not landed, so a debugger session
    # still lands in a cmux tab today; copy that named herdr would be a different lie from the one
    # this test removes. "Session window" is true in both hosts, which is exactly the point of
    # putting the host behind one door.
    root = _STATIC.parent
    surfaces = sorted(root.glob("static/*.js")) + [_STATIC / "index.html"] + \
        sorted(root.glob("lib/*.py")) + [root / "bin" / "liftoff", root / "bin" / "command-center"]
    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        assert "cmux" not in text.lower(), (
            "%s names cmux — the dashboard must not claim a host (issue #310)" % path.name)
