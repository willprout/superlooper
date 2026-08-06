"""Issue #340 — the Open-session-window button on the flight card (the shipped static bundle).

Owner ruling 2026-07-30 (docs/HERDR-ADOPTION-PLAN.md §9): the dashboard's live-view ambition is
REPLACED by a button on the card that opens that session's own window — attach, which is proven —
and **no observe-stream plumbing**. Owner ruling 2026-08-04 (issue #310): the dashboard never names
the session host; the button shells the engine's read-only verb. These are the guards on that shape.

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle —
the same discipline as ``test_static_tidy.py`` / ``test_static_restart.py``. They exist so a future
edit that offers the button on a flight with no session, opens a frame/stream, or swallows the
engine's answer fails CI instead of quietly shipping. The rendered proof that it LOOKS right lives
in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_DRAWER_JS = (_STATIC / "drawer.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def _handler_body(js, name):
    """The source of ``function <name>(...) { ... }``, brace-matched. The guards below assert on
    THIS rather than on a character window after a keyword: a distance-based regex passes or fails
    on how many unrelated lines happen to sit in between, so reordering two ``if`` statements
    elsewhere could flip it either way. Anchoring on the function's real extent makes the guard mean
    what it says."""
    i = js.index("function %s(" % name)
    depth, start = 0, js.index("{", i)
    for j in range(start, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError("unbalanced braces in %s" % name)


# =============================== the button ===============================

def test_the_card_renders_the_open_session_window_button():
    assert 'data-act="session-window"' in _DRAWER_JS, (
        "the flight card must carry the Open-session-window verb button")
    assert re.search(r"data-act=\"session-window\"[\s\S]{0,200}data-repo", _DRAWER_JS), (
        "the button must carry the repo + num the verb targets, like every other card verb")


def test_the_button_is_offered_only_when_the_flight_has_a_session():
    # The server decided this (cards._session); the pixels must READ it, never re-derive it from a
    # stage. A button that could only ever fail is a lie about what the surface can do.
    body = _handler_body(_DRAWER_JS, "sessionHTML")
    assert re.search(r"\bd\.session\b", body), "the card must read the server's session decision"
    assert ".present" in body, "the button must be gated on the server's session.present"
    assert re.search(r"present\)\s*return\s*\"\"", body), (
        "a flight with no recorded window must render NO button at all")


def test_the_button_is_drawn_into_the_card_action_row():
    # Offered on the card itself, not hidden behind another surface: the DoD is a button on the
    # flight card, and it leads the row because it is the only verb there that merely LOOKS at the
    # work rather than changing it.
    actions = _handler_body(_DRAWER_JS, "actionsHTML")
    assert "sessionHTML" in _DRAWER_JS and "session" in actions, (
        "the session-window button must be rendered into the card's action row")


def test_the_pixels_derive_no_target_of_their_own():
    # One derivation of the target (design record B.1): the button carries the flight NUMBER and the
    # server turns it into the engine's lane id. Two spellings is how a display and a verb drift.
    body = _handler_body(_DRAWER_JS, "sessionHTML")
    assert not re.search(r"[\"']i[\"']\s*\+\s*", body), (
        "drawer.js must not build a lane id itself — the server derives it (session_window.lane_id)")


# =============================== the tap ===============================

def test_the_button_posts_the_one_step_verb():
    assert re.search(r"[\"']/api/session-window[\"']", _SHELL_JS), (
        "shell.js must POST to /api/session-window")
    assert 'act === "session-window"' in _SHELL_JS, (
        "the shell's verb dispatcher must handle the session-window act")


def test_a_lane_with_no_window_reads_as_a_fact_not_a_failure():
    # The engine keeps `no_window` apart from its failures on purpose — a session that exited, or a
    # window `superlooper tidy` closed, is the COMMON answer. Painting it red would teach the owner
    # to distrust a truthful answer; hiding it would be a silent success. It gets its own tone.
    body = _handler_body(_SHELL_JS, "doSessionWindow")
    assert "no_window" in body, "the handler must recognise the engine's ordinary-answer outcome"
    assert re.search(r"no_window[\s\S]{0,80}\"note\"", body), (
        "`no_window` must toast in its own neutral tone — neither a success nor an error")
    assert ".cc-toast.note" in _CSS, "the neutral toast tone needs a style of its own"


def test_the_toast_actually_honours_the_neutral_tone():
    # Caught in a real browser, not by reading the code: ``toast()`` mapped every kind that was not
    # ``"err"`` to the SUCCESS class, so the neutral tone rendered green — "nothing was opened"
    # painted as "opened". The handler asking for a tone and the toast honouring it are two
    # separate facts, and this is the second one.
    body = _handler_body(_SHELL_JS, "toast")
    assert not re.search(r"kind\s*===\s*\"err\"\s*\?", body), (
        "a two-way err/ok mapping silently swallows every other tone — including `note`")
    assert re.search(r"TOAST_TONES\[\s*kind\s*\]", body), (
        "toast() must map the kind it is GIVEN, so a new tone cannot render as a success")
    assert re.search(r"TOAST_TONES\s*=\s*\{[^}]*note:\s*\"note\"", _SHELL_JS), (
        "`note` must be one of the tones toast() knows")


def test_the_answer_is_not_rendered_behind_the_card_it_was_tapped_on():
    # Also caught in a real browser: the drawer is a full-viewport overlay at z-index 80 and the
    # toast sat at 60, so the engine's sentence came back dimmed by the scrim with its first line
    # running under the panel. This verb's whole job is to deliver that sentence, so the toast has
    # to outrank every overlay a verb can be tapped from.
    toast_z = re.search(r"\.cc-toast\s*\{[\s\S]*?z-index:\s*(\d+)", _CSS)
    drawer_z = re.search(r"\.cc-drawer\s*\{[\s\S]*?z-index:\s*(\d+)", _CSS)
    assert toast_z and drawer_z
    assert int(toast_z.group(1)) > int(drawer_z.group(1)), (
        "the toast must render ABOVE the drawer — an answer the owner cannot read is not an answer")


def test_a_failed_open_is_shown_honestly_never_swallowed():
    body = _handler_body(_SHELL_JS, "doSessionWindow")
    assert "/api/session-window" in body, "the handler must POST the verb"
    assert "toast(" in body, "the handler must report its outcome to the owner"
    assert re.search(r"b\.message\b", body), (
        "the owner must read the SERVER's sentence — which carries the engine's own words — never "
        "a generic one composed here")
    assert re.search(r"b\.ok\b[\s\S]{0,60}\"ok\"", body), (
        "only an ok:true result may toast success")
    assert '"err"' in body, "anything the engine could not do must reach the owner as an error"


def test_the_button_never_writes_to_github():
    # It is a local-command verb like Tidy/Restart: it must not ride the GitHub verb helper, whose
    # toast claims a label landed and whose refresh re-polls for a GitHub state that never changed.
    body = _handler_body(_SHELL_JS, "doSessionWindow")
    assert "postVerb" not in body, (
        "session-window is a LOCAL command, not a GitHub write — it must not go through postVerb")
    assert "refresh(" not in body, "nothing about the loop changed, so nothing needs re-polling"


def test_the_button_has_a_style_of_its_own():
    assert "drawer-session" in _CSS, "the Open-session-window button needs its own style hook"


# =============================== the ruling's boundary ===============================

def test_no_observe_stream_plumbing_was_added():
    # The ruling's boundary, spelled as a test: attach only. Nothing in the shipped bundle may open
    # a frame stream, a socket, or a live pane read to show a session's screen — not on this button,
    # and not anywhere else in the product.
    for js in sorted(_STATIC.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        for banned in ("WebSocket", "EventSource", "MediaSource", "srcObject"):
            assert banned not in text, (
                "%s opens a live stream — the ruling is attach only, one window to the front"
                % js.name)
    assert not re.search(r"/api/(observe|frames|stream|pane)", _DRAWER_JS + _SHELL_JS), (
        "no observe/frame/pane stream endpoint may be added — the ruling is attach only")


def test_the_button_needs_no_second_call_to_do_its_job():
    # One tap, one verb. A poll, a follow-up read or a retry loop in this handler would be the first
    # inch of the live view the ruling replaced.
    body = _handler_body(_SHELL_JS, "doSessionWindow")
    assert body.count("postJSON(") == 1, "one tap is exactly one call"
    for banned in ("setInterval", "requestAnimationFrame", "setTimeout"):
        assert banned not in body, "%s in the tap handler is the beginning of a live view" % banned
