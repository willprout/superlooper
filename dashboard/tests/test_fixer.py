"""Issue #141 / #144 — the Deploy Fixer button: launch ONE interactive sl-debugger session at
whatever the dashboard is showing stuck.

Deploy Fixer is a LOCAL COMMAND execution — the same button class as Tidy (#41), Restart (#116) and
Janitor (#121), and the owner's 2026-07-07 verb amendment that created it. Since #144 its adapter
(``lib/fixer.py``) is a THIN CLI SHELL, exactly like ``lib/restart.py``: it shells
``superlooper debug``, the engine's owner-tap launch verb.

That collapse is the whole point of #144, so these tests pin BOTH halves of the split:

* **What stays the dashboard's.** Reading the snapshot for what the board is currently rendering as
  unhealthy, and composing that into the context it hands over. Nobody else knows what the owner is
  looking at. Pure functions, unit-tested here.
* **What is now the ENGINE's.** The ``d<N>`` id allocation, the single-flight lock, the brief, the
  ``--cwd`` invocation, the ``SL_RUN_ROOT``/``SL_PANE`` handshake, the ``runner.anchor.json`` pane
  resolution, the ``worker.d<N>.lock`` liveness rule. Before #144 this file hand-copied all five,
  with no stability contract toward the engine — so a silent production break could not fail a
  green suite. ``test_no_engine_internals_remain_in_the_adapter`` is the ratchet that keeps them
  gone.

The owner ruling that makes this legal (2026-07-15) is worth restating, because these tests are its
enforcement: **the no-AI-in-the-dashboard bright line is NOT crossed here.** The dashboard makes no
model call and holds no standing seat. It composes a string (exactly as ``actions.compose_briefing``
already does for Discuss) and executes a local CLI. The AI runs in the launched session, outside
this process, because a human tapped a button. ``test_no_model_call_or_network_in_the_adapter``
pins that.

Two properties are load-bearing bright lines:

* **No real binary in tests.** ``superlooper debug`` OPENS A CMUX TAB and starts a live interactive
  Claude session — the most expensive stray call in this repo. The conftest points
  ``SL_SUPERLOOPER`` at an absent path by default; these tests override it in-body to
  ``tests/fakes/fake-superlooper``.
* **Fail closed, honestly.** An unwatched repo, an unreachable CLI, a live debugger, a launch that
  did not land — each is a plain ``ok: false`` with the reason in words. Never a half-launch, never
  a pretend success, never an exception into the caller.
"""
import json
import re
from pathlib import Path

import pytest

import fixer as fixer_mod
import flights

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


# =============================== the snapshot under the button ===============================

def _flight(num, stage, **extra):
    f = {"num": num, "stage": stage, "title": "flight %d" % num, "liveness": "fresh",
         "spinning": False, "memo": None, "pr": None, "attempt": 1}
    f.update(extra)
    return f


def _snapshot(flights_=None, **repo_extra):
    """A snapshot in the server's real shape (``server._repo_snapshot``): the fields the fixer's
    context assembly reads are exactly the ones the UI renders from."""
    repo = {"slug": SLUG, "name": "command-center", "flights": flights_ or [],
            "alert": None, "merges_frozen": None, "runner_down": False,
            "heartbeat_age": 12.0,
            "state": {"slug": SLUG, "level": "ok", "state": "ok", "rank": 0}}
    repo.update(repo_extra)
    return {"repos": [repo]}


# =============================== context assembly (DoD: unit-tested) ===============================

def test_context_names_the_repo_and_reads_healthy_when_nothing_is_wrong():
    ctx = fixer_mod.trouble_context(_snapshot(), SLUG)
    assert ctx["slug"] == SLUG
    assert ctx["name"] == "command-center"
    assert ctx["healthy"] is True
    assert ctx["items"] == []


def test_context_unknown_repo_is_none_not_a_guess():
    # A slug that isn't on the field yields None — the caller refuses; it never invents a context.
    assert fixer_mod.trouble_context(_snapshot(), "someone/else") is None
    assert fixer_mod.trouble_context({"repos": []}, SLUG) is None
    assert fixer_mod.trouble_context(None, SLUG) is None


def test_context_collects_runner_down_alert_and_freeze():
    snap = _snapshot(runner_down=True, heartbeat_age=612.0, alert={}, merges_frozen={},
                     state={"slug": SLUG, "level": "alert", "state": "runner-down", "rank": 100})
    ctx = fixer_mod.trouble_context(snap, SLUG)
    kinds = [i["kind"] for i in ctx["items"]]
    assert "runner-down" in kinds and "alert" in kinds and "merges-freeze" in kinds
    assert ctx["healthy"] is False
    assert ctx["state"] == "runner-down"
    assert ctx["level"] == "alert"
    # The heartbeat age rides along as a NUMBER — the debugger gets the fact, not just the word.
    down = next(i for i in ctx["items"] if i["kind"] == "runner-down")
    assert down["heartbeat_age"] == 612.0


def test_context_collects_the_stuck_flights_the_ui_is_showing():
    snap = _snapshot([
        _flight(12, flights.PARKED, memo="gate refused: no review comment"),
        _flight(13, flights.SESSION_FROZEN, liveness="frozen"),
        _flight(14, flights.STRANDED, pr=99),
        _flight(15, flights.AWAITING),
        _flight(16, "downwind", spinning=True),
    ])
    ctx = fixer_mod.trouble_context(snap, SLUG)
    by_num = {i.get("num"): i for i in ctx["items"] if i.get("num")}
    assert set(by_num) == {12, 13, 14, 15, 16}
    assert by_num[12]["kind"] == flights.PARKED
    assert by_num[12]["memo"] == "gate refused: no review comment"
    assert by_num[13]["kind"] == flights.SESSION_FROZEN
    assert by_num[14]["kind"] == flights.STRANDED
    assert by_num[15]["kind"] == flights.AWAITING
    assert by_num[16]["kind"] == "spinning"


def test_context_ignores_healthy_and_designed_safe_flights():
    # A flight mid-build is not trouble; neither is one HOLDING (sequenced behind another lane —
    # the designed-safe wait). The fixer is pointed at what's WRONG, never at the whole field.
    snap = _snapshot([_flight(20, "downwind"), _flight(21, flights.HOLDING),
                      _flight(22, "final"), _flight(23, "touchdown")])
    ctx = fixer_mod.trouble_context(snap, SLUG)
    assert ctx["items"] == []
    assert ctx["healthy"] is True


def test_context_items_are_ranked_worst_first():
    # Same ranking the UI's own pill uses (flights._CONDITION_RANK) — the debugger reads the worst
    # thing first, exactly as the owner does.
    snap = _snapshot([_flight(12, flights.PARKED), _flight(13, flights.SESSION_FROZEN)],
                     runner_down=True, alert={})
    ctx = fixer_mod.trouble_context(snap, SLUG)
    kinds = [i["kind"] for i in ctx["items"]]
    assert kinds[0] == "runner-down", "runner-down outranks everything (rank 100)"
    assert kinds[1] == "alert"
    assert kinds.index(flights.PARKED) < kinds.index(flights.SESSION_FROZEN)


# =============================== context composition (DoD: unit-tested) ===============================

def _ctx_with_trouble():
    snap = _snapshot([_flight(12, flights.PARKED, memo="gate refused: no review comment")],
                     runner_down=True, heartbeat_age=612.0,
                     state={"slug": SLUG, "level": "alert", "state": "runner-down", "rank": 100})
    return fixer_mod.trouble_context(snap, SLUG)


def test_the_composed_context_is_the_board_in_words():
    c = fixer_mod.compose_context(_ctx_with_trouble())
    assert "RUNNER DOWN" in c.upper()
    assert "SL-12" in c, "the parked flight must be named by its number"
    assert "gate refused: no review comment" in c, "the flight's own memo is context, not noise"
    assert "last heartbeat 612s ago" in c, "the debugger gets the fact, not just the word"


def test_the_composed_context_is_ordered_worst_first():
    c = fixer_mod.compose_context(_ctx_with_trouble())
    assert c.index("RUNNER DOWN") < c.index("SL-12")


def test_a_healthy_field_composes_an_honest_nothing_not_a_fabricated_symptom():
    # The owner may tap Deploy Fixer on a field that reads clean (they saw something the UI didn't).
    # The context must say that honestly rather than invent trouble.
    c = fixer_mod.compose_context(fixer_mod.trouble_context(_snapshot(), SLUG))
    low = c.lower()
    assert "nothing" in low or "healthy" in low


def test_the_composed_context_does_not_instruct_the_debugger_how_to_repair():
    # Boundary: this is a READOUT of the board, never a work order — the debugger's authority and
    # its ladder are the sl-debugger skill's own contract. A context that started dictating repairs
    # would be this button quietly rewriting the skill.
    c = fixer_mod.compose_context(_ctx_with_trouble()).lower()
    for forbidden in ("force-push", "agent-ready", "pkill", "rung"):
        assert forbidden not in c, (
            "the context must not define debugger behavior (%r) — the skill owns that" % forbidden)


def test_the_composed_context_is_bounded_so_a_huge_board_cannot_drown_the_brief():
    # A repo with a hundred parked flights is a real state. The context rides into the session's
    # opening message, so it is bounded here as well as in the engine.
    snap = _snapshot([_flight(n, flights.PARKED, memo="m" * 400) for n in range(300)])
    c = fixer_mod.compose_context(fixer_mod.trouble_context(snap, SLUG))
    # The bound is on the CONTENT; the truncation marker is a fixed, visible addition on top.
    assert len(c) <= fixer_mod.CONTEXT_MAX + 200
    assert "truncated" in c.lower()


def test_compose_context_on_a_missing_context_is_a_string_not_a_crash():
    assert isinstance(fixer_mod.compose_context(None), str)


# =============================== the adapter (subprocess, against the fake CLI) ===============================

@pytest.fixture
def fx(tmp_path, monkeypatch):
    """A Fixer bound to the fake ``superlooper`` CLI, with a fixtures dir the fake logs its
    calls/mutations into."""
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_FIXER_FIXTURES", str(tmp_path))
    log = tmp_path / "cc" / "fixer-log.jsonl"
    # The configured binary is deliberately bogus so a passing test PROVES the SL_SUPERLOOPER env
    # override (the fail-closed fixture's only lever) actually wins over the configured path.
    verb = fixer_mod.Fixer("/nonexistent/configured-superlooper", {SLUG: PATH},
                           operator="William", log_path=str(log), now=lambda: 1_700_000_000.0)
    return verb, tmp_path, log


def _calls(fixtures):
    p = fixtures / "calls.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _launches(fixtures):
    p = fixtures / "mutations.jsonl"
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []
    return [r for r in rows if r["kind"] == "debug_launch"]


# ---- the happy path ----

def test_execute_shells_the_engines_debug_verb(fx):
    verb, fixtures, _log = fx
    res = verb.execute(SLUG, "the queue is frozen", _snapshot(runner_down=True))
    assert res["ok"] is True, res
    assert res["id"] == "d4", "the id is the ENGINE's — the dashboard no longer allocates one"
    assert res["verb"] == "fixer", "the UI's verb name, normalized from the CLI's own"

    argv = _calls(fixtures)[-1]["argv"]
    assert argv[0] == "debug"
    assert argv[argv.index("--repo") + 1] == PATH
    assert "--json" in argv
    assert argv[argv.index("--source") + 1] == "command-center"
    assert argv[argv.index("--operator") + 1] == "William"
    assert len(_launches(fixtures)) == 1, "exactly ONE session — never two"


def test_the_owners_note_reaches_the_engine_verbatim(fx):
    # The end-to-end proof of the whole feature: the words the owner typed arrive at the verb that
    # writes the brief, unsummarized and unreworded (there is no model in this path to reword them).
    verb, fixtures, _log = fx
    note = "the departures board is lying about SL-12"
    verb.execute(SLUG, note, _snapshot([_flight(12, flights.PARKED)]))
    assert _launches(fixtures)[0]["note"] == note


def test_the_board_context_is_piped_to_the_engine_on_stdin(fx):
    # Piped, not argv: a board with a hundred stuck flights must never hit an argv limit.
    verb, fixtures, _log = fx
    verb.execute(SLUG, "look", _snapshot([_flight(12, flights.PARKED)], runner_down=True))
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[argv.index("--context-file") + 1] == "-"
    context = _launches(fixtures)[0]["context"]
    assert "SL-12" in context and "RUNNER DOWN" in context.upper()


def test_execute_with_an_empty_note_still_launches(fx):
    verb, fixtures, _log = fx
    res = verb.execute(SLUG, "", _snapshot(alert={}))
    assert res["ok"] is True, res
    assert len(_launches(fixtures)) == 1


@pytest.mark.parametrize("note", [None, "", "   \n  "])
def test_a_blank_note_is_sent_as_no_note_at_all(fx, note):
    # An engine that receives `--note "   "` would write whitespace where the owner's words go.
    # Send nothing and let the engine say "no note" in its own words.
    verb, fixtures, _log = fx
    verb.execute(SLUG, note, _snapshot())
    argv = _calls(fixtures)[-1]["argv"]
    assert "--note" not in argv


def test_a_giant_note_is_bounded_before_it_is_handed_over(fx):
    verb, fixtures, _log = fx
    verb.execute(SLUG, "x" * 50_000, _snapshot())
    sent = _launches(fixtures)[0]["note"]
    assert len(sent) <= fixer_mod.NOTE_MAX + 200
    assert "truncated" in sent.lower()


# ---- the preflight ----

def test_preflight_asks_the_engine_and_writes_nothing(fx):
    verb, fixtures, log = fx
    pre = verb.preflight(SLUG, _snapshot(alert={}))
    assert pre["ok"] is True and pre["verb"] == "fixer-check"
    assert pre["live"] is False and pre["live_id"] is None
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[0] == "debug" and "--check" in argv
    assert _launches(fixtures) == [], "the preflight launches nothing"
    assert not log.exists(), "the preflight records no launch"


def test_preflight_shows_the_trouble_the_dialog_will_send(fx):
    verb, _fixtures, _log = fx
    pre = verb.preflight(SLUG, _snapshot([_flight(12, flights.PARKED)], alert={}))
    kinds = [i["kind"] for i in pre["trouble"]["items"]]
    assert "alert" in kinds and flights.PARKED in kinds


def test_preflight_surfaces_a_live_debugger_the_engine_reports(fx, monkeypatch):
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_DEBUG_LIVE", "d3")
    pre = verb.preflight(SLUG, _snapshot(alert={}))
    assert pre["live"] is True and pre["live_id"] == "d3"


# ---- single-flight: now the ENGINE's refusal, surfaced as-is ----

def test_a_live_debugger_refusal_is_surfaced_not_reinvented(fx, monkeypatch):
    # The engine holds the watchdog's own lock across its whole check-allocate-launch, so IT is the
    # authority on "a debugger is already on this patient". The dashboard shows its words.
    verb, fixtures, _log = fx
    monkeypatch.setenv("SL_DEBUG_LIVE", "d3")
    res = verb.execute(SLUG, "again", _snapshot(alert={}))
    assert res["ok"] is False
    assert res["live"] is True and res["live_id"] == "d3"
    assert "already" in (res.get("error") or "").lower()
    assert _launches(fixtures) == [], "NOTHING may launch while a fixer is live"


# ---- fail closed ----

def test_unwatched_repo_is_refused_before_any_subprocess(fx):
    verb, fixtures, _log = fx
    res = verb.execute("someone/else", "hi", _snapshot())
    assert res["ok"] is False
    assert res["error"] == "unknown repo"
    assert _calls(fixtures) == []


def test_a_repo_not_on_the_field_is_refused_before_any_subprocess(fx):
    # Configured but absent from the snapshot: the dashboard cannot describe what it cannot see, so
    # it refuses rather than sending a context-free launch.
    verb, fixtures, _log = fx
    res = verb.execute(SLUG, "hi", {"repos": []})
    assert res["ok"] is False
    assert _calls(fixtures) == []


def test_a_failed_launch_is_never_a_silent_success(fx, monkeypatch):
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_DEBUG_FAIL", "1")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False, "the CLI's ok:false is the honest verdict on delivery"
    assert "timed out" in res["error"]
    assert res["verb"] == "fixer"


def test_an_unreachable_cli_says_so_plainly(fx, monkeypatch):
    # The conftest's DEFAULT posture: SL_SUPERLOOPER points at an absent path. The UI must say so
    # in words the owner can act on, never a bare exit code.
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_SUPERLOOPER", "/nonexistent/not-a-cli")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "superlooper" in res["error"].lower()


def test_unparseable_cli_output_is_an_honest_failure_not_a_pretend_success(fx, monkeypatch):
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_DEBUG_GARBAGE", "1")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False and res["error"]


def test_a_hung_launch_trips_the_timeout_and_reports_it(fx, monkeypatch):
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_TIDY_SLEEP", "3")           # the fake's shared sleep injector
    verb._timeout = 0.4                                # mirrors tidy/restart's shrinkable timeout
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "timed out" in res["error"].lower()


def test_the_adapter_never_raises_into_the_caller(fx, monkeypatch):
    # A button that 500s is worse than one that says "no". Every failure mode is a dict.
    verb, _fixtures, _log = fx
    monkeypatch.setenv("SL_SUPERLOOPER", str(Path(__file__)))   # not executable → OSError
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False and res["error"]


# ---- the launch is recorded ----

def test_every_launch_is_recorded_with_a_timestamp_and_the_note(fx):
    verb, _fixtures, log = fx
    verb.execute(SLUG, "the queue is frozen", _snapshot(runner_down=True))
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == 1_700_000_000.0
    assert r["note"] == "the queue is frozen", "a later reader must see WHY the fixer ran"
    assert r["id"] == "d4"
    assert r["repo"] == SLUG
    assert r["ok"] is True
    assert r["operator"] == "William"
    assert "runner-down" in r["trouble"], "what the UI was showing at tap time"


def test_a_failed_launch_is_recorded_too(fx, monkeypatch):
    # The record is the honest history: an attempt that failed is exactly what a later reader
    # needs to see, so it can never be quietly absent.
    verb, _fixtures, log = fx
    monkeypatch.setenv("SL_DEBUG_FAIL", "1")
    verb.execute(SLUG, "nope", _snapshot())
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1 and rows[0]["ok"] is False


def test_the_log_lives_in_the_dashboards_own_dir_not_a_repos_loop_state():
    # decision B.4 / lib/desk: the center's own facts live under $SL_HOME/command-center/, BESIDE
    # the loop state homes it reads — never inside one.
    p = str(fixer_mod.default_log_path())
    assert p.endswith("/command-center/fixer-log.jsonl")


def test_a_log_write_failure_never_breaks_the_launch(fx):
    verb, fixtures, _log = fx
    verb._log_path = "/nonexistent/dir/deep/fixer-log.jsonl"
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is True, "the record is bookkeeping — it must never fail the owner's tap"
    assert len(_launches(fixtures)) == 1


# ---- the bright lines ----

def test_no_model_call_or_network_in_the_adapter():
    # The owner's 2026-07-15 ruling in code: the dashboard composes a string and hands it to a local
    # CLI. No model call, no standing seat, no network — the AI runs in the launched session,
    # outside this process, because a human tapped a button.
    low = (Path(__file__).resolve().parent.parent / "lib" / "fixer.py").read_text().lower()
    # No model vendor or inference surface anywhere in the adapter.
    for forbidden in ("anthropic", "openai", "api_key", "claude -p", "messages.create",
                      "chat.completions", "completion("):
        assert forbidden not in low, (
            "lib/fixer.py must contain no model call (%r) — the bright line" % forbidden)
    # No network-capable import or call. (Matched as imports/calls, not bare substrings: prose may
    # legitimately name e.g. ThreadingHTTPServer, which is the server this runs inside, not egress.)
    for forbidden in ("import urllib", "import socket", "import requests", "import httpx",
                      "urlopen(", "http.client", "socket."):
        assert forbidden not in low, (
            "lib/fixer.py must make no network call (%r) — the bright line" % forbidden)


def test_no_engine_internals_remain_in_the_adapter():
    """Issue #144's DoD as a RATCHET. Each pattern below is an engine internal this adapter used to
    hard-code with no stability contract toward the engine — and because no test may reach a real
    shim, the suite would have stayed green while production silently broke. They now live behind
    ``superlooper debug``; a future edit that reaches back for one fails here.

    Matched as CODE, not prose: the module header legitimately explains what moved and why, so the
    scan reads the file with its docstrings and comments stripped."""
    src = (Path(__file__).resolve().parent.parent / "lib" / "fixer.py").read_text()
    code = _strip_comments_and_docstrings(src).lower()
    forbidden = {
        "launch-session": "the launch shim path — the engine resolves its own",
        "sl_run_root": "the shim's env handshake — the engine owns it",
        "sl_pane": "the shim's env handshake — the engine owns it",
        "sl_launch_verify_seconds": "the shim's pinned verify window — the engine owns it",
        "sl_launch_session": "the shim override — the engine resolves it",
        "runner.anchor.json": "the pane anchor's on-disk shape — the engine reads it",
        "watchdog.json": "the watchdog's own document — only the engine may read or write it",
        "next_debugger": "the id allocator — the engine allocates under its own lock",
        "worker.d": "the worker-lock naming convention — the engine's single-flight rule",
        "briefs": "the brief path convention — the engine composes and places the brief",
        "os.kill": "pid liveness probing — the engine decides what 'already running' means",
    }
    for needle, why in forbidden.items():
        assert needle not in code, (
            "lib/fixer.py must not hard-code the engine internal %r (%s) — issue #144" % (needle, why))


def _strip_comments_and_docstrings(src):
    """The source with ``#`` comments and triple-quoted strings removed — crude but sufficient for
    a needle scan over a stdlib-only module whose only triple-quoted strings ARE docstrings."""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())
