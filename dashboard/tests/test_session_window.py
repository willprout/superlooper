"""Issue #340 — the Open-session-window verb: the dashboard RIDES the engine's read-only door.

This button was built once before (on the merged #310 branch) and removed unshipped, because it
drove the session host itself. The owner's 2026-08-04 ruling settled the shape: the dashboard names
no host, and the capability lands in the ENGINE as ``superlooper focus-session`` (issue #339). So
``lib/session_window`` is a sibling of ``lib/tidy`` / ``lib/restart`` / ``lib/fixer`` — a thin shell
over the same CLI — and these tests pin the three properties that make it one:

* **No real binary, ever.** The conftest points ``SL_SUPERLOOPER`` at an absent path by default;
  these tests override it in-body to ``tests/fakes/fake-superlooper``, which records every argv.
  Nothing here can reach a real session host: the dashboard has no path to one at all, which is the
  point of the whole design.
* **The target is DERIVED from the flight**, never taken from a request body string —
  :func:`lane_id` turns a positive integer into ``i<N>`` and refuses everything else, so no string
  a client sends can become a subprocess argument.
* **All four of the engine's outcomes reach the owner as the engine reported them.** ``no_window``
  is the COMMON answer ("that session exited, or tidy closed its window") and it must never be
  dressed up as a success or flattened into a generic error.
"""
import ast
import json
from pathlib import Path

import pytest

import session_window as sw

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    """Point the adapter at the fake CLI and give it a fixture dir to log argv into."""
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_FOCUS_FIXTURES", str(tmp_path))
    return tmp_path


def calls(fixtures):
    p = fixtures / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def verb(binary="/nonexistent/superlooper", paths=None, timeout=None):
    return sw.SessionWindow(binary, paths if paths is not None else {SLUG: PATH}, timeout=timeout)


# =============================== the derived target (pure) ===============================

def test_lane_id_derives_the_engine_argument_from_a_flight_number():
    # The dashboard already knows this lane as ``i<N>`` (it is the key of the engine's own state
    # home). Deriving it here — rather than accepting one — is what keeps a request body out of argv.
    assert sw.lane_id(340) == "i340"
    assert sw.lane_id("340") == "i340"           # a JSON client may send either


@pytest.mark.parametrize("bad", [None, True, False, 0, -5, 1.5, "abc", "i340", "", "  ",
                                 "340; rm -rf /", "²", {"num": 1}])
def test_lane_id_refuses_anything_that_is_not_a_flight_number(bad):
    # The fence between a request body and a subprocess. ``True`` is screened explicitly (it is an
    # ``int`` in Python and would otherwise become the perfectly plausible ``i1``), and the string
    # test is ``isdecimal`` — ``"²".isdigit()`` is True but ``int("²")`` RAISES, which would turn
    # this "returns None" contract into an exception thrown out of a request handler.
    assert sw.lane_id(bad) is None


def test_a_body_string_can_never_become_the_engines_target(fixtures):
    # The DoD's bright line, asserted where it bites: a non-number is refused BEFORE any subprocess.
    res = verb().open(SLUG, "i340 --repo /somewhere/else")
    assert res["ok"] is False
    assert calls(fixtures) == [], "nothing may run for a target that is not a flight number"


# =============================== the invocation ===============================

def test_the_button_shells_the_engines_read_only_verb(fixtures):
    verb().open(SLUG, 340)
    argv = [c["argv"] for c in calls(fixtures)]
    assert argv == [["focus-session", "--repo", PATH, "--id", "i340", "--json"]], (
        "the dashboard reaches a session window through the ENGINE's focus-session verb and "
        "nothing else (owner ruling 2026-08-04 on #310)")


def test_the_configured_cli_is_overridable_exactly_like_every_other_local_command(monkeypatch,
                                                                                  tmp_path):
    # ``SL_SUPERLOOPER`` wins over the configured path — the ONE lever the fail-closed fixture pulls.
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_FOCUS_FIXTURES", str(tmp_path))
    res = sw.SessionWindow("/nonexistent/configured-superlooper", {SLUG: PATH}).open(SLUG, 340)
    assert res["ok"] is True, "the env override must resolve the binary, not the configured path"


def test_an_unwatched_repo_is_refused_before_any_subprocess(fixtures):
    res = verb().open("someone/else", 340)
    assert res == {"ok": False, "verb": "session-window", "repo": "someone/else", "num": 340,
                   "id": None, "outcome": None, "message": "unknown repo",
                   "error": "unknown repo"}
    assert calls(fixtures) == [], "an unwatched repo must never reach a subprocess"


# =============================== the four outcomes ===============================

def test_a_focused_window_is_an_honest_success(fixtures):
    res = verb().open(SLUG, 340)
    assert res["ok"] is True
    assert res["outcome"] == "focused"
    assert res["id"] == "i340"
    assert res["error"] is None
    # The claim is the ENGINE's, not a bigger one: the HOST moved its focus.
    assert "focus" in res["message"].lower()


def test_no_window_is_reported_as_the_engine_reported_it(fixtures, monkeypatch):
    # THE COMMON ANSWER, and the one this verb must never dress up. A lane whose session exited (or
    # whose window `superlooper tidy` closed) has no window to raise — an ordinary fact, never a
    # silent success and never a generic error.
    monkeypatch.setenv("SL_FOCUS_OUTCOME", "no_window")
    res = verb().open(SLUG, 340)
    assert res["ok"] is False, "nothing was opened — this may never read as a success"
    assert res["outcome"] == "no_window"
    # The engine's own sentence reaches the owner verbatim, not a shrug of our own.
    assert res["error"] == res["message"]
    assert "no session window is recorded for i340" in res["message"]


def test_an_unreachable_host_is_never_read_as_the_session_being_gone(fixtures, monkeypatch):
    monkeypatch.setenv("SL_FOCUS_OUTCOME", "host_unreachable")
    res = verb().open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] == "host_unreachable"
    assert "could not ask the host" in res["message"]
    assert "no window" not in res["message"], (
        "absence of signal must never be rendered as 'your session is gone'")


def test_an_unknown_lane_surfaces_the_engines_words(fixtures, monkeypatch):
    monkeypatch.setenv("SL_FOCUS_OUTCOME", "unknown_lane")
    res = verb().open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] == "unknown_lane"
    assert res["message"].strip()


def test_every_outcome_carries_the_same_keys(fixtures, monkeypatch):
    # One shape for every answer, including the ones a given path has nothing to put in them — a UI
    # that has to test for a missing key writes a different branch per answer, which is how one of
    # the four ends up unhandled (the engine's own rule, kept on this side of the wire too).
    seen = []
    for outcome in ("focused", "no_window", "host_unreachable", "unknown_lane"):
        monkeypatch.setenv("SL_FOCUS_OUTCOME", outcome)
        seen.append(set(verb().open(SLUG, 340)))
    assert all(keys == seen[0] for keys in seen)
    assert seen[0] == {"ok", "verb", "repo", "num", "id", "outcome", "message", "error"}


# =============================== the CLI that could not answer at all ===============================

def test_a_missing_cli_names_what_to_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", str(tmp_path / "not-installed"))
    res = verb().open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] is None, "the engine never answered — there is no outcome to claim"
    assert "superlooper" in res["error"] and "config.json" in res["error"]


def test_a_timeout_fails_closed(fixtures, monkeypatch):
    monkeypatch.setenv("SL_FOCUS_SLEEP", "2")
    res = verb(timeout=0.2).open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] is None
    assert "timed out" in res["error"]


def test_an_engine_too_old_for_the_verb_says_exactly_that(fixtures, monkeypatch):
    # The failure mode this dashboard is built to name (lib/engine's whole subject): the runner
    # executes the INSTALLED engine copy, so a merged focus-session is inert until someone
    # republishes. argparse exits 2 on `invalid choice` with no JSON — which must not be flattened
    # into "that lane is unknown", the one diagnosis that would stop this being reported.
    monkeypatch.setenv("SL_FOCUS_UNKNOWN_VERB", "1")
    res = verb().open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] is None
    assert "install" in res["error"], "the remedy — republishing the engine — must be named"
    assert "focus-session" in res["error"]


def test_output_the_dashboard_cannot_read_is_an_honest_failure(fixtures, monkeypatch):
    monkeypatch.setenv("SL_FOCUS_GARBAGE", "1")
    res = verb().open(SLUG, 340)
    assert res["ok"] is False
    assert res["outcome"] is None
    assert res["error"].strip()


def test_the_adapter_never_raises_into_a_caller(fixtures, monkeypatch):
    # Every failure shape at once — a tap can only ever fail closed (mirrors lib/tidy._run).
    for env in ({"SL_FOCUS_FAIL": "1"}, {"SL_FOCUS_GARBAGE": "1"}, {"SL_FOCUS_OUTCOME": "nonsense"}):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        res = verb().open(SLUG, 340)
        assert res["ok"] is False and res["error"]
        for k in env:
            monkeypatch.delenv(k)


# =============================== the ruling's boundary ===============================

def _code_without_prose(path):
    """The module as ``ast.unparse`` renders it with every DOCSTRING blanked: comments are gone
    (unparse drops them), docstrings are gone, and every other string literal is kept.

    The same tone as the engine's one-doorway fence, and for the same reason: a docstring saying
    "no observe stream, no frame rendering" is documentation of the boundary, while a string literal
    or an identifier carrying one of those words is machinery. A guard that could not tell the two
    apart would forbid this module from explaining itself."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            first.value.value = ""
    return ast.unparse(tree).lower()


def test_no_observe_stream_or_pane_read_plumbing_exists_here():
    # Owner ruling 2026-07-30: this REPLACES the live-view ambition. Attach, which is proven — no
    # observe-stream plumbing, no frame rendering, no pane reads. A regression test rather than a
    # comment, because "we could just also show the last few lines" is exactly the kind of thing a
    # later change reaches for.
    code = _code_without_prose(sw.__file__)
    # ``capture`` is deliberately absent: ``subprocess.run(capture_output=True)`` is how every
    # adapter here reads a CLI's own stdout, and a guard that fired on it would be muted within a
    # week. ``screenshot`` covers the thing that word would have been listed for.
    for banned in ("observe", "stream", "frame", "screenshot", "send-keys",
                   "send_keys", "pane", "websocket", "eventsource"):
        assert banned not in code, (
            "lib/session_window names %r in code — this verb opens a window and nothing else, and "
            "the ruling that gave it to the engine excluded every form of live view" % banned)


def test_the_no_live_view_guard_would_notice_the_plumbing_coming_back(tmp_path):
    # The meta-test: prove the sweep above can actually fail, so it cannot rot into a green that
    # means nothing (the failure mode structural guards die of).
    plumbing = tmp_path / "regressed.py"
    plumbing.write_text('"""No observe stream lives here."""\n'
                        'def frames(pane):\n'
                        '    return _run(["session", "observe", pane])\n')
    code = _code_without_prose(plumbing)
    assert "observe" in code and "pane" in code, (
        "the guard must read code, and it must still catch a real live-view call")
    prose_only = tmp_path / "clean.py"
    prose_only.write_text('"""No observe stream, no frame rendering, no pane reads."""\n'
                          'def open_it(iid):\n'
                          '    return _run(["focus-session", "--id", iid])\n')
    assert "observe" not in _code_without_prose(prose_only), (
        "a module explaining the boundary in prose is not a module that crossed it")


def test_the_verb_makes_exactly_one_call_and_it_changes_nothing(fixtures):
    # One tap, one read-only engine call. A second call — a status poll, a follow-up read — would be
    # the beginning of the plumbing the ruling excluded.
    verb().open(SLUG, 340)
    ran = calls(fixtures)
    assert len(ran) == 1
    assert ran[0]["argv"][0] == "focus-session", (
        "focus-session is the engine's read-only door; no other verb belongs on this path")


# =============================== the fixer's own seat (issue #458) ===============================
#
# A Deploy Fixer tap that LANDS puts an interactive sl-debugger session on the field, and the
# dashboard now names it at the button (``lib/fixer.last_launch``). Naming it and then offering no
# way in would be half an answer, so the launched fixer gets the same open-session affordance the
# flight card already has — pointed at its own lane.
#
# A debugger seat is a ``d<N>`` lane, not a flight number. The engine has always resolved both
# (``skill/lib/focus.LANE_ID_RE`` is ``^[id][0-9]+$``, and focusing is read-only, so unlike ``tidy``
# — which CLOSES, and deliberately leaves a debugger seat alone — there is nothing here a d-lane
# needs protecting from). What is new is only that the dashboard can now ASK for one, behind the
# same fence: an id is REBUILT from an integer, so no string from a request body reaches argv.

def test_debugger_lane_id_derives_the_engine_argument_from_a_fixer_id():
    assert sw.debugger_lane_id("d4") == "d4"
    assert sw.debugger_lane_id(" d12 ") == "d12"          # a trimmed body string is still a d-lane


@pytest.mark.parametrize("bad", [None, True, False, 4, 1.5, "", "  ", "i4", "d", "dd4", "d4x",
                                 "d-1", "d 4", "d4; rm -rf /", "d²", {"id": "d4"}, "d" + "9" * 40])
def test_debugger_lane_id_refuses_anything_that_is_not_a_fixer_seat(bad):
    # The same fence ``lane_id`` draws, for the other lane shape: only a d followed by digits — of a
    # sane length — becomes an id, and the returned string is REBUILT from the parsed integer, so
    # the caller's own bytes never reach the subprocess.
    assert sw.debugger_lane_id(bad) is None


def test_a_body_string_can_never_become_the_fixers_target(fixtures):
    res = verb().open_debugger(SLUG, "d4 --repo /somewhere/else")
    assert res["ok"] is False
    assert calls(fixtures) == [], "nothing may run for a target that is not a fixer seat"


def test_opening_a_fixers_window_shells_the_same_read_only_verb(fixtures):
    res = verb(binary=FAKE).open_debugger(SLUG, "d4")
    argv = calls(fixtures)[0]["argv"]
    assert argv[:2] == ["focus-session", "--repo"]
    assert argv[2] == PATH
    assert "--id" in argv and argv[argv.index("--id") + 1] == "d4"
    assert res["ok"] is True and res["id"] == "d4"
    assert "d4" in res["message"], "the sentence must name the fixer the owner tapped"


def test_a_fixer_whose_window_is_gone_reads_as_a_fact_not_a_failure(fixtures, monkeypatch):
    monkeypatch.setenv("SL_FOCUS_OUTCOME", "no_window")
    res = verb(binary=FAKE).open_debugger(SLUG, "d4")
    assert res["ok"] is False and res["outcome"] == "no_window"
    assert res["message"] and "no session window is recorded" in res["message"]


def test_the_fixer_answer_carries_the_same_keys_as_a_flights(fixtures):
    flight = verb(binary=FAKE).open(SLUG, 340)
    fixer = verb(binary=FAKE).open_debugger(SLUG, "d4")
    assert set(flight) == set(fixer)
    assert fixer["num"] is None, "a debugger seat is not a flight — it must not borrow a number"


def test_an_unwatched_repo_is_refused_before_any_subprocess_for_a_fixer_too(fixtures):
    res = sw.SessionWindow(FAKE, {}).open_debugger(SLUG, "d4")
    assert res["ok"] is False and res["error"] == "unknown repo"
    assert calls(fixtures) == []
