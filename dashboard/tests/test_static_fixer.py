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


# =============================== the tap renders its outcome (issue #458) ===============================
#
# On 2026-08-26 the owner tapped Deploy Fixer during a Claude-auth outage. The request was consumed,
# the engine attempted the launch, the attempt failed, the failure was journaled — and the dashboard
# showed nothing. The mechanism was structural: the ONLY surface that ever named a result was the
# note box above, and a close, a page reload or a superseding open threw it away. "Seems to have
# done nothing" about a button that did something is the dishonest surface this project keeps
# killing, so the outcome now rides the SNAPSHOT and renders beside the button that was tapped.
#
# Every semantic is the server's (``lib/fixer.last_launch`` → ``trouble.fixer``): the sentence, the
# headline, whether an open-session affordance is honest. These guards pin that the shipped bundle
# BINDS them and derives nothing.

def _trouble_body():
    m = re.search(r"function troubleHTML\(s\)\s*\{(.+?)\n  \}", _SHELL_JS, re.S)
    assert m, "troubleHTML must still exist — it is where the button lives"
    return m.group(1)


def _fixer_outcome_body():
    """The renderer's CODE — whole-line comments stripped, because the header legitimately explains
    the outcome vocabulary it must not itself compose."""
    code = re.sub(r"(?m)^\s*//.*$", "", _SHELL_JS)
    m = re.search(r"function fixerOutcomeHTML\([^)]*\)\s*\{(.+?)\n  \}", code, re.S)
    assert m, ("shell.js must render the Deploy Fixer outcome (fixerOutcomeHTML) — a tap the "
               "machine consumed must never be followed by silence")
    return m.group(1)


def test_the_outcome_renders_in_the_banner_the_button_was_tapped_from():
    body = _trouble_body()
    assert "fixerOutcomeHTML" in body, (
        "the last tap's outcome must render INSIDE the trouble banner — the surface the Deploy "
        "Fixer button lives on (tap-where-you-read, §0.3), not in a dialog that a close throws away")
    assert re.search(r"fixerOutcomeHTML\([^)]*\.fixer", body), (
        "it must bind trouble.fixer — the SERVER's block for the repo the banner is naming")


def test_the_sentence_is_the_servers_never_composed_in_the_pixels():
    body = _fixer_outcome_body()
    assert re.search(r"\.text\b", body), "the banner must bind the server's finished sentence"
    for invented in ("did not launch", "deployed", "launched", "outcome not yet"):
        assert invented not in body.lower(), (
            "the JS must not compose its own verdict wording (%r) — lib/fixer owns every sentence, "
            "so the banner and the tower log can never disagree about one launch" % invented)


def test_all_three_outcomes_reach_the_owner_and_none_of_them_is_silence():
    body = _fixer_outcome_body()
    # The ONE early return allowed is "there has never been a tap" — the honest absence of a launch.
    early = re.findall(r"if\s*\(([^)]*)\)\s*return\s*\"\";", body)
    assert early, "the renderer must return nothing only when there is no tap to report"
    for guard in early:
        assert "present" in guard, (
            "an outcome may never be suppressed by the pixels — the only silence allowed is a repo "
            "that has never had a tap (%r)" % guard)
    assert "outcome" in body, "the outcome must reach the markup so each state can read differently"


def test_a_launched_fixer_gets_the_same_open_session_affordance_a_flight_card_has():
    body = _fixer_outcome_body()
    assert "session-window" in body, (
        "a launched fixer must offer the SAME verb the flight card already has — naming a live "
        "session and offering no way into it is half an answer")
    assert "data-fixer" in body, "the affordance must target the fixer's own d<N> seat"
    assert re.search(r"\.session\b", body), (
        "the affordance must be gated on the SERVER's `session` flag — the one state in which "
        "offering to open a window is honest (a confirmed launch with a named lane)")


def test_the_affordance_is_never_offered_for_a_launch_that_did_not_happen():
    body = _fixer_outcome_body()
    # The gate must be a CONDITION on the button, not a button with a condition inside it.
    m = re.search(r"\.session\b[\s\S]{0,120}session-window", body)
    assert m, "the open-session button must be produced only under the `session` gate"


def test_the_verb_the_affordance_fires_is_the_one_already_tested():
    assert 'act === "session-window"' in _SHELL_JS, (
        "the fixer's window must ride the existing session-window dispatch — one verb, one tested "
        "handler, not a second copy of the engine's four outcomes")
    m = re.search(r"function doSessionWindow\(([^)]*)\)\s*\{(.+?)\n  \}", _SHELL_JS, re.S)
    assert m, "doSessionWindow must still exist"
    assert "fixer" in m.group(2), (
        "the handler must be able to name a fixer seat, so the POST carries `fixer` instead of `num`")


def test_the_outcome_has_a_style_of_its_own_in_all_three_states():
    assert ".trouble-fixer" in _CSS, "the outcome strip needs its own style hook"
    for state in ("launched", "failed", "pending"):
        assert re.search(r"\.trouble-fixer\.%s\b" % state, _CSS), (
            "the %r outcome must be visually distinct — three states that look identical are one "
            "state wearing three names" % state)


def test_the_affordance_targets_the_servers_canonical_seat_not_the_journals_own_string():
    # Fresh-agent review (P1). `id` is whatever the journal line carried; `lane` is the seat the
    # server canonicalised through the SAME fence the endpoint validates against. Binding `id` here
    # would ship a button that 400s the moment a record carries an id the route refuses.
    body = _fixer_outcome_body()
    assert re.search(r"data-fixer=\"'\s*\+\s*esc\(f\.lane\)", body), (
        "the open-session button must carry f.lane — the server's canonical d<N> seat")
    assert not re.search(r"data-fixer=\"'\s*\+\s*esc\(f\.id\)", body), (
        "it must never put the journal's raw id on the wire")
