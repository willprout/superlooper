"""Issue #141 — the Deploy Fixer button + its note box (the shipped static bundle).

Deploy Fixer is the dashboard's fourth ops-verb button and the most consequential: tapping it starts
a live interactive sl-debugger session on William's machine, pointed at whatever the board is showing
stuck. The flow is a bright line of this issue:

    button (IN the trouble banner — tap-where-you-read, §0.3) → server preflight → a note box that
    is SKIPPABLE → Deploy → server composes the prompt + launches → the honest result.

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle —
the same discipline as ``test_static_restart.py`` / ``test_static_tidy.py``. They exist so a future
edit that moves the button away from the trouble it responds to, makes the note mandatory, drops the
single-flight honesty, or lets the JS invent a semantic fails CI instead of silently shipping. The
rendered proof that it LOOKS right (joy included, §0.1) lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIXER_JS = (_STATIC / "fixer.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def test_index_loads_the_fixer_bundle_before_shell():
    assert "/fixer.js" in _INDEX, "index.html must load the Deploy Fixer overlay bundle"
    assert _INDEX.index("/fixer.js") < _INDEX.index("/shell.js"), (
        "fixer.js must load before shell.js so window.CCFixer exists when the button binds it")


# =============================== placement: tap-where-you-read (§0.3) ===============================

def test_the_button_lives_in_the_trouble_banner():
    # §0.3 is a fixed point: wherever a decision is shown, its action is right there. The trouble
    # banner is the ONE surface that renders on every unhealthy condition this button answers
    # (runner-down, alert, parked, session-frozen, stranded, spinning, freeze) and it is
    # camera-independent (§4/§5) — so an off-screen problem still offers its fix.
    m = re.search(r"function troubleHTML\(s\)\s*\{(.+?)\n  \}", _SHELL_JS, re.S)
    assert m, "troubleHTML must still exist — it is where the button lives"
    body = m.group(1)
    assert "fixer-open" in body, (
        "the Deploy Fixer button must render INSIDE the trouble banner (tap-where-you-read, §0.3) — "
        "not parked in the topbar away from the trouble it answers")


def test_the_button_targets_the_offending_repo():
    m = re.search(r"function troubleHTML\(s\)\s*\{(.+?)\n  \}", _SHELL_JS, re.S)
    body = m.group(1)
    assert "offender" in body, (
        "the button must carry trouble.offender (the server's slug for the repo in trouble) — the "
        "fixer is pointed at the patient the banner is naming, never at the viewed repo by accident")


def test_no_button_when_nothing_is_wrong():
    # The banner is hidden when all is clear, so the button cannot appear on a healthy board: the
    # early return must come BEFORE any button markup.
    m = re.search(r"function troubleHTML\(s\)\s*\{(.+?)\n  \}", _SHELL_JS, re.S)
    body = m.group(1)
    early = body.index("return")
    assert early < body.index("fixer-open"), (
        "the not-present early return must precede the button — no fixer button on a clean board")


def test_shell_dispatches_the_open():
    assert re.search(r'act === "fixer-open"', _SHELL_JS), "shell.js must route the fixer-open tap"
    assert "CCFixer" in _SHELL_JS, "shell.js must open the overlay via window.CCFixer"


# =============================== the flow ===============================

def test_flow_is_two_step_check_then_launch():
    assert "/api/fixer/check" in _FIXER_JS, "fixer.js must fetch the preflight first"
    assert re.search(r"[\"']/api/fixer[\"']", _FIXER_JS), (
        "fixer.js must POST to /api/fixer to launch the session")


def test_the_note_box_is_skippable():
    # DoD: "Tap → optional text box (skippable); launching with an empty note works." The deploy
    # button must never be gated on the textarea having content — a fixer with no note is a
    # first-class outcome, not a degraded one.
    assert not re.search(r"if\s*\(\s*!\s*note\s*\)\s*\{?\s*return", _FIXER_JS), (
        "the note is OPTIONAL — fixer.js must not refuse to deploy on an empty note "
        "(contrast the Flag composer, where empty text IS refused)")
    assert "optional" in _FIXER_JS.lower(), (
        "the box must SAY the note is optional — a blank field with no cue reads as required")


def test_the_textarea_lives_outside_root():
    # The NOTAM/flag precedent: #root is rebuilt wholesale every ~2s poll, so a textarea inside it
    # loses focus (and the owner's half-typed note) on every tick.
    assert "document.body.appendChild" in _FIXER_JS, (
        "the note box must be appended to <body>, outside #root — the 2s poll would otherwise eat "
        "focus and the owner's typing mid-sentence")


def test_the_owners_note_is_sent_verbatim():
    # No client-side summarizing, trimming to a sentence, or templating — the note is his word.
    assert re.search(r"note:\s*\w+", _FIXER_JS), "the note must be POSTed as-is"


def test_the_dialog_shows_when_a_fixer_is_already_running():
    # DoD: single-flight — "the UI shows that a fixer is already running."
    assert re.search(r"\blive\b", _FIXER_JS), "fixer.js must read the preflight's `live` flag"
    assert "already" in _FIXER_JS.lower(), (
        "the dialog must SAY a fixer is already running — a silently-disabled button is not an "
        "explanation")


def test_a_failed_launch_is_shown_honestly():
    assert re.search(r"\berror\b", _FIXER_JS), "fixer.js must surface the server's error string"
    assert re.search(r"renderError|result err", _FIXER_JS), (
        "a failed launch must render an honest failure — never a silent success")


# =============================== the bright lines ===============================

def test_the_js_computes_no_semantics():
    # Design B.1 (the squint test): the server already decided what is unhealthy and composed the
    # prompt. This file binds strings to pixels. If the JS started deciding what counts as trouble,
    # the banner and the fixer's prompt could disagree about what is wrong.
    for forbidden in ("heartbeat_age >", "runner_down &&", "rank >", "_CONDITION_RANK"):
        assert forbidden not in _FIXER_JS, (
            "fixer.js must not derive trouble (%r) — the server owns every semantic" % forbidden)


def test_the_client_never_supplies_the_trouble_context():
    # The prompt's honesty rests on the SERVER reading the board at tap time. A client that could
    # name the trouble could lie about it — so the POST body carries only the repo and the note.
    m = re.search(r"postJSON\(\s*[\"']/api/fixer[\"']\s*,\s*\{(.*?)\}", _FIXER_JS, re.S)
    assert m, "the launch POST must be findable"
    body = m.group(1)
    assert "trouble" not in body and "snapshot" not in body, (
        "the client must not send the context — the server reads it fresh from its own snapshot")


def test_no_model_call_in_the_client():
    low = _FIXER_JS.lower()
    for forbidden in ("anthropic", "openai", "api_key", "api.", "completions"):
        assert forbidden not in low, (
            "fixer.js must contain no model call (%r) — the dashboard never holds a seat; the AI "
            "runs in the LAUNCHED session" % forbidden)


# =============================== the 16-bit design language (§0.8) ===============================

def test_the_button_and_box_are_styled_in_the_16bit_language():
    # DoD: "in the dashboard's 16-bit design language — this is part of the delight surface, not a
    # gray admin widget." The button lives on the alarm banner, so it must have a treatment for BOTH
    # banner tiers (amber attention and the dark ALERT) — a single flat gray would read as chrome.
    assert ".trouble-fix" in _CSS, "the banner button needs its own treatment"
    assert ".trouble.alert .trouble-fix" in _CSS, (
        "the button must restyle on the ALERT banner — its background changes underneath it")
    assert ".cc-fixer" in _CSS, "the note box needs the ops-dialog chrome"
    # It must use the established design tokens, not invented one-off colors.
    m = re.search(r"\.trouble-fix\s*\{(.+?)\}", _CSS, re.S)
    assert m and "var(--" in m.group(1), (
        "the button must use the shared design tokens (var(--…)) — never a one-off hex")


def test_the_box_names_the_verb_and_what_it_will_do():
    low = _FIXER_JS.lower()
    assert "sl-debugger" in low, "the box must name what it launches, in the owner's own vocabulary"
    assert "deploy fixer" in low or "deploy" in low
