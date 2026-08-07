"""Issue #365 — the Stop/Start button + its confirm dialog (the shipped static bundle).

Stop is the dashboard's off switch, and the only button here that takes production DOWN. So the
guards below are stricter than Restart's, and each one is a property that would be a real incident
if a future edit dropped it:

    button → dialog states EXACTLY what stopping does → the owner taps "Stop the loop" →
    POST /api/stop → the SERVER's summary is rendered verbatim (never re-derived, never assumed).

* **Nothing stops without an in-UI confirm.** There is no preflight step — the engine has none, and
  a stop records itself before anything can die — so the confirm is the ONLY gate between a tap and
  the loop going off. It cannot be fired by the same code path that opened the dialog.
* **A way back exists.** The same control becomes Start when a stop is recorded. A stop button with
  no visible resume strands a non-terminal owner, which is the DoD's own line.
* **The outcome is the server's.** The dialog binds ``summary.headline`` / ``summary.lines`` /
  ``summary.remedy``; it does not decide from ``ok`` whether the loop stopped. That decision is
  wrong in both directions (a stop can succeed with the runner still finishing a tick, a start can
  succeed having started nothing) and it is made once, in tested Python.
* **Stopped is not crashed, on the board.** The stopped banner is its own element with its own
  words, never the red RUNNER DOWN one, and the whole-surface RUNNER DOWN grey does not fire.
* **No session host, anywhere.** #310/#333: the dashboard names none. Any host-specific remedy is
  the ENGINE's string, relayed as data.

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped bundle —
the same discipline as ``test_static_restart.py``. The rendered proof that it LOOKS right lives in
the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_STOP_JS = (_STATIC / "stop.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# --------------------------- the bundle is wired ---------------------------

def test_index_loads_the_stop_bundle_before_shell():
    assert "/stop.js" in _INDEX, "index.html must load the Stop overlay bundle"
    assert _INDEX.index("/stop.js") < _INDEX.index("/shell.js"), (
        "stop.js must load before shell.js so window.CCStop exists when the button binds it")


def test_the_topbar_carries_the_control_and_routes_it_to_the_overlay():
    assert 'data-act="stop-open"' in _SHELL_JS, "the topbar must carry the Stop/Start control"
    assert re.search(r'act === "stop-open"[\s\S]{0,120}CCStop', _SHELL_JS), (
        "the stop-open action must open window.CCStop")


def test_the_control_carries_the_camera_repo_and_disables_without_one():
    # Like Tidy/Restart/Sweep/Flag: a tap targets the repo on screen, and with no repo there is
    # nothing to stop — an enabled button pointing at "" would POST a missing repo.
    m = re.search(r'data-act="stop-open"[\s\S]{0,400}?</button>', _SHELL_JS)
    assert m, "the stop control must be a single rendered button"
    assert "data-repo=" in m.group(0)
    assert "disabled" in m.group(0)


# --------------------------- the confirm gate ---------------------------

def test_nothing_stops_without_an_in_ui_confirm():
    assert "data-stop-confirm" in _STOP_JS, (
        "stop.js must render an explicit confirm control before executing")
    assert re.search(r"data-stop-confirm[\s\S]{0,80}runExecute", _STOP_JS), (
        "the data-stop-confirm control must trigger runExecute")
    # The strongest form of the gate: there is exactly ONE call site that posts to the server in
    # this whole file, and it is inside runExecute — which only the confirm tap reaches. A second
    # call site anywhere else would be a second way to stop the loop.
    assert _STOP_JS.count("postJSON(v.path") == 1, "one POST call site, not two"
    assert re.search(r"function runExecute[\s\S]{0,900}?postJSON\(v\.path", _STOP_JS), (
        "the execute POST must live inside runExecute, reached only from the confirm")


def test_opening_the_dialog_posts_nothing():
    # The whole gate rests on this: `open` must not fetch. The engine has no preflight for a stop,
    # so an open that called the server would BE the stop.
    opener = re.search(r"function open\([\s\S]{0,900}?\n  \}", _STOP_JS)
    assert opener, "stop.js must define open()"
    assert "fetch(" not in opener.group(0) and "postJSON(" not in opener.group(0), (
        "opening the Stop dialog must not call the server — the confirm is the only trigger")


def test_both_directions_have_their_own_endpoint():
    assert re.search(r"[\"']/api/stop[\"']", _STOP_JS), "stop.js must POST to /api/stop"
    assert re.search(r"[\"']/api/start[\"']", _STOP_JS), "stop.js must POST to /api/start"


def test_the_confirm_states_the_consequence_in_plain_words():
    # The DoD: a confirmation step, because it stops production. A confirm that says only "are you
    # sure?" is a speed bump; this one has to say what actually happens.
    assert re.search(r"finish(es)? (the |its )?current tick", _STOP_JS, re.I), (
        "the confirm must say the runner finishes its current tick")
    assert re.search(r"in-flight worker sessions untouched", _STOP_JS, re.I), (
        "the confirm must say in-flight worker sessions are untouched")
    assert re.search(r"watchdog", _STOP_JS, re.I), (
        "the confirm must say the guardians stand down — that is what makes it a STOP")
    assert re.search(r"next login", _STOP_JS, re.I), (
        "the confirm must state the engine's own bound: this is an off switch for the night, "
        "not an uninstall")


def test_the_start_direction_is_confirmed_too_so_a_mislabelled_tap_cannot_fire():
    # The control's identity flips under a 2s poll. The confirm is what makes that safe: whatever
    # the button said, the dialog names the verb again before anything happens.
    assert re.search(r"Start the loop", _STOP_JS), "the start confirm must name its own verb"
    assert re.search(r"Stop the loop", _STOP_JS), "the stop confirm must name its own verb"


# --------------------------- the outcome is the server's ---------------------------

def test_the_result_is_rendered_from_the_servers_summary():
    assert "summary" in _STOP_JS, "the dialog must bind the server-computed summary"
    assert re.search(r"summary[\s\S]{0,400}headline", _STOP_JS), "…its headline"
    assert re.search(r"\.lines\b", _STOP_JS), "…its supporting lines"
    assert re.search(r"\.remedy\b", _STOP_JS), "…and the remedy on a failure"


def test_the_dialog_never_decides_for_itself_that_the_loop_stopped():
    # Design B.1 and the DoD's "never a false all-clear" in one: `ok` is the verb's verdict, not the
    # loop's state. If this file branched on b.ok to say "stopped", a stop that merely recorded
    # itself over a still-merging runner would be shown as a completed one.
    assert not re.search(r"\bb\.ok\b\s*\?", _STOP_JS), (
        "the dialog must not derive its headline from ok — the server's summary decides")
    assert re.search(r"level", _STOP_JS), "the ok/warn/err level comes from the summary too"


def test_a_transport_failure_is_shown_rather_than_assumed_either_way():
    assert re.search(r"catch\(", _STOP_JS), "a failed fetch must be caught"
    assert re.search(r"could|couldn", _STOP_JS), "…and reported, never swallowed into silence"


# --------------------------- the board: stopped is not crashed ---------------------------

def test_the_stopped_banner_is_its_own_element_with_its_own_words():
    assert "stopped-banner" in _SHELL_JS, "a deliberate stop gets its own banner"
    assert re.search(r"runner\.stopped\b", _SHELL_JS), "…bound to the snapshot's stopped fact"
    assert re.search(r"stopped_message|stopped_headline", _SHELL_JS), (
        "…and to the server-composed sentence, never one derived in JS")


def test_the_red_runner_down_takeover_does_not_fire_for_a_deliberate_stop():
    # `runner.down` is already False server-side for a stopped repo, so the existing binding is
    # correct — this pins that the stopped banner is NOT wired to the same flag, which would put
    # both banners on screen at once saying opposite things.
    m = re.search(r'"stopped-banner"[\s\S]{0,600}', _SHELL_JS)
    assert m and "RUNNER DOWN" not in m.group(0)[:400], (
        "the stopped banner must not reuse the RUNNER DOWN wording")
    assert re.search(r"STOPPED BY OWNER|stopped_headline", _SHELL_JS)


def test_the_stopped_surface_is_visibly_calmer_than_the_crash_surface():
    # §0.2 and honesty at once: a deliberate stop is quiet, not alarming, and must never look like
    # the RUNNER DOWN emergency. Distinct CSS, distinctly gentler.
    assert ".stopped-banner" in _CSS, "the stopped banner needs its own styling"
    down = re.search(r"#app\.runner-down \.shell \{[^}]*grayscale\(([\d.]+)\)", _CSS)
    stop = re.search(r"#app\.stopped \.shell \{[^}]*grayscale\(([\d.]+)\)", _CSS)
    assert down and stop, "both surfaces declare their own grey"
    assert float(stop.group(1)) < float(down.group(1)), (
        "a stop the owner chose must be gentler than a crash")


# --------------------------- the bright lines ---------------------------

def test_the_stop_bundle_names_no_session_host():
    # #310/#333. The engine's `manual` line may name one at runtime; this file may not author one.
    for host in ("cmux", "herdr", "tmux", "iterm", "wezterm", "kitty"):
        assert host not in _STOP_JS.lower(), "stop.js must never name a session host"


def test_the_stop_bundle_makes_no_model_call():
    # The constitutional inheritance: no AI/LLM anywhere in the dashboard. Every button is a
    # mechanical verb.
    for tell in ("anthropic", "openai", "claude", "llm", "completion"):
        assert tell not in _STOP_JS.lower(), "the dashboard makes no model calls"
    assert re.search(r"no GitHub, no AI", _STOP_JS), (
        "the dialog says out loud what it is: a local command, no GitHub, no AI")


def test_the_trouble_banner_offers_no_fixer_for_a_state_the_server_calls_unfixable():
    # Deploy Fixer launches a real agent session. The decision about whether a condition HAS a
    # patient is the server's (design B.1) — this pins that the JS honours it rather than offering
    # the button for anything with an offender.
    assert re.search(r"t\.fixable !== false", _SHELL_JS), (
        "the fixer button must respect the server's `fixable` flag")
