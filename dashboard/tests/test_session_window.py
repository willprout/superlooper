"""Issue #310 — the Open-session-window verb (``lib/session_window.py``).

The dashboard's local-command class gains one more member: a tap on a flight card opens THAT
session's window on the session host (herdr ``agent focus``). Like Tidy/Restart/Janitor/Fixer it
shells a local binary, so it inherits their whole discipline and these tests pin it:

* never raises into a caller (missing binary / timeout / crash all become an honest ``ok: false``);
* a WATCHED repo only — an unwatched or forged slug is refused BEFORE any subprocess;
* the host name is DERIVED from the flight number here, never taken from the client, so no request
  body can steer the subprocess argument;
* a failure is surfaced plainly, never a silent success.

No test here reaches a real ``herdr``: ``tests/conftest.py`` points ``SL_HERDR`` at an absent path
by default and these tests inject ``tests/fakes/fake-herdr`` in-body (mirrors test_tidy.py).
"""
from pathlib import Path

import pytest

import session_window

_FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-herdr")
_PATHS = {"willprout/superlooper": "/tmp/superlooper"}


@pytest.fixture
def fake_herdr(monkeypatch):
    monkeypatch.setenv("SL_HERDR", _FAKE)
    return _FAKE


# =============================== the pure name derivation ===============================

def test_name_for_builds_the_hosts_agent_name_from_the_flight_number():
    # The engine addresses agents by NAME, derived from the lane id (session_host.name_for) — the
    # dashboard must spell it the same way or it addresses nothing.
    assert session_window.name_for(310) == "i310"
    assert session_window.name_for("310") == "i310"
    assert session_window.name_for(7) == "i7"


def test_name_for_refuses_anything_that_is_not_a_flight_number():
    # This is the fence that keeps a request body out of the subprocess argv: only a positive
    # integer ever becomes a name, so no string a client sends can reach the host as a target.
    for bad in (None, "", "  ", "i310", "310; rm -rf /", "-1", 0, -3, 1.5, True, [310], {"n": 1}):
        assert session_window.name_for(bad) is None, bad


# =============================== the verb ===============================

def test_open_focuses_the_flights_own_agent_on_the_host(fake_herdr):
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 310)
    assert res["ok"] is True
    assert res["verb"] == "session-window"
    assert res["name"] == "i310"
    # The fake echoes its argv — the verb spelling is pinned here, not discovered in production.
    assert res["raw"].split() == ["agent", "focus", "i310"]


def test_open_reports_the_manual_line_so_the_owner_can_always_do_it_by_hand(fake_herdr):
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 310)
    assert "agent focus i310" in res["manual"]


def test_an_unwatched_repo_is_refused_before_any_subprocess(monkeypatch):
    # SL_HERDR stays neutralized (the conftest default): if this ever shelled anything it would
    # fail on a missing binary, so a clean "unknown repo" proves nothing ran.
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("someone/else", 310)
    assert res == {"ok": False, "verb": "session-window", "error": "unknown repo"}


def test_a_bad_flight_number_is_refused_before_any_subprocess():
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", "not-a-number")
    assert res["ok"] is False
    assert "flight number" in res["error"]


def test_a_host_that_has_no_window_for_this_flight_is_an_honest_failure(fake_herdr):
    # The fake refuses i999 exactly as herdr does for an unknown agent (rc 1 + a named error).
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 999)
    assert res["ok"] is False
    assert res["name"] == "i999"
    # The host's own words are kept AND framed: this string lands alone in a toast seconds after a
    # tap, so it has to say which button produced it, not just echo a log line.
    assert "agent target i999 not found" in res["error"]        # herdr's own sentence, verbatim
    assert "session host" in res["error"] and "i999's window" in res["error"]


def test_a_missing_host_binary_fails_closed_and_names_what_to_fix(monkeypatch):
    monkeypatch.setenv("SL_HERDR", "/nonexistent/no-such-herdr")
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 310)
    assert res["ok"] is False
    assert "/nonexistent/no-such-herdr" in res["error"]
    assert "herdr_cli" in res["error"]


def test_a_hanging_host_times_out_instead_of_wedging_the_button(monkeypatch, fake_herdr):
    sw = session_window.SessionWindow("herdr", _PATHS, timeout=0.4)
    res = sw.open("willprout/superlooper", 1)          # the fake sleeps for i1
    assert res["ok"] is False
    assert "timed out" in res["error"]


def test_the_env_override_wins_over_the_configured_binary(fake_herdr):
    # Mirrors lib/tidy's SL_SUPERLOOPER precedence: the override is the one lever the fail-closed
    # fixture pulls, so it must beat whatever the config named.
    sw = session_window.SessionWindow("/nonexistent/configured-herdr", _PATHS)
    assert sw.open("willprout/superlooper", 310)["ok"] is True


def test_the_verb_never_raises_even_when_the_binary_is_a_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_HERDR", str(tmp_path))      # a directory is not executable
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 310)
    assert res["ok"] is False and res["error"]


def test_no_token_is_ever_placed_on_the_hosts_command_line(fake_herdr, monkeypatch):
    # The fence (#305) travels in the ENVIRONMENT, never in argv: on macOS a same-uid reader is
    # refused another process's environment but is served its argv, so a token on the command line
    # would publish it to exactly the worker panes the fence exists to keep out.
    monkeypatch.setenv("HERDR_API_TOKEN", "s3cret")
    sw = session_window.SessionWindow("herdr", _PATHS)
    res = sw.open("willprout/superlooper", 310)
    assert "s3cret" not in res["raw"]
    assert "s3cret" not in res["manual"]


def test_name_for_is_total_even_for_characters_python_calls_digits(monkeypatch):
    # `"²".isdigit()` is True but `int("²")` RAISES — Python's isdigit accepts superscripts and
    # other numeric characters that int() will not parse. This module bills name_for as pure and
    # total ("or None"), and it is the fence between a request body and a subprocess, so it must
    # answer None here rather than throw out of a request handler.
    for weird in ("²", "½", "¹²", " ³ "):
        assert session_window.name_for(weird) is None, weird
    # ...while a real non-ASCII DECIMAL digit is still just a number, and is accepted as one.
    assert session_window.name_for("٣١٠") == "i310"      # Arabic-Indic 310


def test_the_hosts_json_refusal_envelope_becomes_plain_words_not_a_wall_of_json(fake_herdr):
    # Measured against the installed herdr while building this, not guessed: a refusal is a JSON
    # ENVELOPE on stderr —
    #     {"error":{"code":"agent_not_found","message":"agent target i999 not found"},"id":"..."}
    # — and that whole blob landing in a toast would be the opposite of "surfaces plainly". The
    # host's own message is extracted; the machine-readable envelope is not the owner's problem.
    sw = session_window.SessionWindow("herdr", _PATHS)
    err = sw.open("willprout/superlooper", 999)["error"]
    assert "agent target i999 not found" in err
    assert "{" not in err and "cli:agent:focus" not in err, "the raw envelope must not reach the UI"


def test_a_refusal_this_build_cannot_parse_still_reaches_the_owner_verbatim(monkeypatch, tmp_path):
    # If a later herdr changes the envelope, the message must DEGRADE to the raw text — never to
    # silence. A failure the owner cannot see is worse than an ugly one.
    stub = tmp_path / "herdr"
    stub.write_text("#!/bin/sh\necho 'something entirely new went wrong' >&2\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setenv("SL_HERDR", str(stub))
    sw = session_window.SessionWindow("herdr", _PATHS)
    assert "something entirely new went wrong" in sw.open("willprout/superlooper", 310)["error"]
