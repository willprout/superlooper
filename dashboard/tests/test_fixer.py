"""Issue #141 — the Deploy Fixer button: launch ONE interactive sl-debugger session at whatever the
dashboard is showing stuck.

Deploy Fixer is a LOCAL COMMAND execution — the same button class as Tidy (#41), Restart (#116) and
Janitor (#121), and the owner's 2026-07-07 verb amendment that created it. Its adapter
(``lib/fixer.py``) mirrors ``lib/restart.py``'s discipline: a subprocess wrapper that NEVER raises
into the caller, a hard timeout, an allow-list of watched repos, and fail-closed on every unknown.

The owner ruling that makes this legal (2026-07-15) is worth restating, because these tests are its
enforcement: **the no-AI-in-the-dashboard bright line is NOT crossed here.** The dashboard makes no
model call and holds no standing seat. It composes a prompt by string assembly (exactly as
``actions.compose_briefing`` already does for Discuss) and hands it to the engine's existing launch
shim — the same mechanism the runner and the watchdog already use. The AI runs in the launched
session, outside this process, because a human tapped a button. `test_no_model_call_or_network_in_
the_adapter` pins that.

Four properties are load-bearing bright lines:

* **No real shim in tests.** The real ``launch-session.sh`` OPENS A CMUX TAB and starts a live
  interactive Claude session — the most expensive stray call in this repo. The conftest points
  ``SL_LAUNCH_SESSION`` at an absent path by default; these tests override it in-body to
  ``tests/fakes/fake-launch-session``.
* **Fail closed when the launch can't be resolved.** No shim / no cmux pane / an unwatched repo ⇒ a
  plain honest error and NOTHING launches — never a half-launch, never a pretend success.
* **Single-flight.** A live debugger session (any ``worker.d*.lock`` naming a live pid — the
  engine's OWN convention, which its watchdog checks the same way) blocks a second launch. Never two
  debuggers on one patient.
* **The owner's note rides VERBATIM into the session's prompt.** Not summarized, not reworded — the
  brief is what ``start-session.sh`` cats into the agent as its opening message, so these tests read
  the brief back off disk through the fake and assert the exact text survives.
"""
import json
import os
from pathlib import Path

import pytest

import fixer as fixer_mod
import flights

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-launch-session")
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


# =============================== prompt composition (DoD: unit-tested) ===============================

def _ctx_with_trouble():
    snap = _snapshot([_flight(12, flights.PARKED, memo="gate refused: no review comment")],
                     runner_down=True, heartbeat_age=612.0,
                     state={"slug": SLUG, "level": "alert", "state": "runner-down", "rank": 100})
    return fixer_mod.trouble_context(snap, SLUG)


def test_prompt_invokes_the_sl_debugger_skill_and_says_a_human_is_present():
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), "", PATH, "/home/pat/.superlooper/x",
                                 operator="William")
    assert "sl-debugger" in p, "the prompt must invoke the skill by name — it IS the invocation"
    low = p.lower()
    assert "human-present" in low and "at the keyboard" in low, (
        "the launched session must know a person is present — the skill's authority contract "
        "branches on it (human-present vs the watchdog's unattended contract)")
    # It must never CLAIM to be the watchdog's unattended session — a different, stricter authority
    # tier. These are the unattended brief template's own load-bearing phrases; saying any of them
    # here would put the session in the wrong mode. (Naming `unattended` to DISCLAIM it is correct
    # and expected — so this pins the claims, not the word.)
    for claim in ("you are **unattended**", "nobody is watching", "nobody can answer",
                  "not a person"):
        assert claim not in low, "the prompt must not put the session in unattended mode: %r" % claim


def test_prompt_carries_the_owners_note_verbatim():
    note = "the queue looks frozen — i think the runner is wedged again.\n\nsecond paragraph."
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), note, PATH, "/home/pat/.superlooper/x",
                                operator="William")
    assert note in p, "the note must ride VERBATIM — never summarized, never reworded (no AI)"


def test_prompt_works_with_an_empty_note_and_says_so_plainly():
    # DoD: launching with an empty note works. The prompt must be honest that none was typed,
    # rather than leaving a blank section the session reads as a lost instruction.
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), "", PATH, "/home/pat/.superlooper/x",
                                 operator="William")
    assert "no note" in p.lower()


@pytest.mark.parametrize("note", [None, "", "   \n  "])
def test_prompt_treats_blank_notes_as_no_note(note):
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), note, PATH, "/home/pat/.superlooper/x")
    assert "no note" in p.lower()


def test_prompt_carries_the_machine_context_repo_state_home_and_the_trouble():
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), "", PATH, "/home/pat/.superlooper/x",
                                 operator="William")
    assert PATH in p, "the checkout path is machine context the debugger needs"
    assert "/home/pat/.superlooper/x" in p, "the state home is where the patient's truth lives"
    assert SLUG in p
    # The specific items the UI is rendering as unhealthy, in words:
    assert "RUNNER DOWN" in p.upper()
    assert "SL-12" in p, "the parked flight must be named by its number"
    assert "gate refused: no review comment" in p, "the flight's own memo is context, not noise"


def test_prompt_on_a_healthy_field_says_the_dashboard_shows_nothing_wrong():
    # The owner may tap Deploy Fixer on a field that reads clean (he saw something the UI didn't).
    # The prompt must say that honestly rather than fabricate a symptom.
    ctx = fixer_mod.trouble_context(_snapshot(), SLUG)
    p = fixer_mod.compose_prompt(ctx, "everything looks fine but the board feels wrong", PATH,
                                 "/home/x", operator="William")
    assert "everything looks fine but the board feels wrong" in p
    low = p.lower()
    assert "nothing" in low or "no trouble" in low or "healthy" in low


def test_prompt_does_not_instruct_the_debugger_how_to_repair():
    # Boundary: this issue defines the LAUNCH, never the debugger's behavior — its authority and
    # its ladder are the skill's own contract. A prompt that started dictating repairs would be
    # this issue quietly rewriting the skill.
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), "fix it", PATH, "/home/x")
    low = p.lower()
    for forbidden in ("force-push", "agent-ready", "pkill", "rung"):
        assert forbidden not in low, (
            "the prompt must not define debugger behavior (%r) — the skill owns that" % forbidden)


def test_note_is_bounded_so_a_pasted_novel_cannot_become_the_prompt():
    huge = "x" * 50_000
    p = fixer_mod.compose_prompt(_ctx_with_trouble(), huge, PATH, "/home/x")
    assert len(p) < 20_000, "an unbounded note would drown the machine context it rides with"
    assert "truncated" in p.lower()


# =============================== launch-command construction (DoD: unit-tested) ===============================

def test_launch_script_is_the_shim_beside_the_configured_cli():
    # The engine ships `superlooper` and `launch-session.sh` as siblings in its bin/, so the ONE
    # configured path (config's superlooper_cli) locates both — no second config knob.
    got = fixer_mod.launch_script_for("~/.claude/skills/superlooper/bin/superlooper")
    assert got.endswith("/bin/launch-session.sh")
    assert "~" not in got, "the path must be expanded — a literal ~ never resolves in a subprocess"


def test_env_override_wins_over_the_configured_script(monkeypatch):
    monkeypatch.setenv("SL_LAUNCH_SESSION", "/tmp/injected-shim")
    assert fixer_mod.resolve_script("/configured/launch-session.sh") == "/tmp/injected-shim"


def test_resolve_script_falls_back_to_the_configured_path(monkeypatch):
    monkeypatch.delenv("SL_LAUNCH_SESSION", raising=False)
    assert fixer_mod.resolve_script("/configured/launch-session.sh") == "/configured/launch-session.sh"


def test_launch_argv_is_exactly_the_engines_cwd_form():
    # The `--cwd <dir> <d-id>` form is the engine's own contract for a debugger session: no
    # worktree, no branch, launched in an existing dir. `launch-session.sh` REFUSES an id that
    # isn't ^[ad][0-9]+$ through this path, so the id shape is load-bearing.
    argv = fixer_mod.launch_argv("/bin/shim", PATH, "d3")
    assert argv == ["/bin/shim", "--cwd", PATH, "d3"]


def test_launch_env_carries_the_shims_required_handshake():
    env = fixer_mod.launch_env({"PATH": "/usr/bin"}, "/home/state", "cmux:pane-1",
                               model="opus[1m]", agent="claude")
    assert env["SL_RUN_ROOT"] == "/home/state", "the shim aborts without it"
    assert env["SL_PANE"] == "cmux:pane-1", "the shim aborts without it"
    assert env["SL_AGENT"] == "claude"
    assert env["SL_MODEL"] == "opus[1m]"
    assert env["PATH"] == "/usr/bin", "the ambient env must survive — the shim needs git/cmux on PATH"


def test_launch_env_pins_the_verify_window():
    # The engine pins this for its own watchdog launch (review P1-1 there): an ambient large
    # SL_LAUNCH_VERIFY_SECONDS would let the launch outlive our timeout, so a late-delivering tab
    # could start a REAL session while we counted the attempt failed — the exact double-launch the
    # single-flight check exists to prevent.
    env = fixer_mod.launch_env({"SL_LAUNCH_VERIFY_SECONDS": "9999"}, "/h", "p")
    assert env["SL_LAUNCH_VERIFY_SECONDS"] == "30"


def test_next_fixer_id_shape_and_succession():
    assert fixer_mod.next_fixer_id([]) == "d1"
    assert fixer_mod.next_fixer_id(["d1", "d2"]) == "d3"
    # Non-debugger ids and garbage are ignored, never crash the allocator.
    assert fixer_mod.next_fixer_id(["i44", "a2", "junk", "d7"]) == "d8"
    # Gaps never re-use a number: a brief for d3 exists ⇒ the next is d4, so we can never clobber
    # a prior session's brief (which is its transcript's opening message).
    assert fixer_mod.next_fixer_id(["d3"]) == "d4"


# =============================== the adapter (subprocess, against the fake) ===============================

@pytest.fixture
def fx(tmp_path, monkeypatch):
    """A Fixer bound to the fake shim, a synthetic state home, and a fixtures dir the fake logs
    launches into."""
    monkeypatch.setenv("SL_LAUNCH_SESSION", FAKE)
    monkeypatch.setenv("SL_FIXER_FIXTURES", str(tmp_path / "fix"))
    (tmp_path / "fix").mkdir()
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    # A live runner anchor — the cmux pane the debugger tab is born in (the engine's own
    # resolution: state/runner.anchor.json, present while a runner is live OR crashed).
    (home / "state" / "runner.anchor.json").write_text(json.dumps({"pane": "cmux:pane-7"}))
    log = tmp_path / "cc" / "fixer-log.jsonl"
    # The configured script is deliberately bogus so a passing test PROVES the SL_LAUNCH_SESSION
    # override (the fail-closed fixture's only lever) actually wins over the configured path.
    verb = fixer_mod.Fixer("/nonexistent/configured-shim",
                           {SLUG: {"path": PATH, "state_home": str(home)}},
                           operator="William", log_path=str(log), now=lambda: 1_700_000_000.0)
    return verb, tmp_path, home, log


def _launches(tmp_path):
    p = tmp_path / "fix" / "launches.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _live_lock(home, sid, pid=None):
    """Plant a worker.d<N>.lock naming a LIVE pid — the engine's own singleton marker, which its
    watchdog reads the same way to refuse a second debugger."""
    (home / "state" / ("worker.%s.lock" % sid)).write_text(str(pid or os.getpid()))


# ---- the happy path ----

def test_execute_launches_one_session_through_the_shim(fx):
    verb, tmp_path, home, _log = fx
    res = verb.execute(SLUG, "the queue is frozen", _snapshot(runner_down=True))
    assert res["ok"] is True, res
    assert res["id"] == "d1"
    launches = _launches(tmp_path)
    assert len(launches) == 1, "exactly ONE session — never two"
    L = launches[0]
    assert L["argv"] == ["--cwd", PATH, "d1"], "the engine's own --cwd debugger form"
    assert L["env"]["SL_RUN_ROOT"] == str(home)
    assert L["env"]["SL_PANE"] == "cmux:pane-7", "resolved from the runner's recorded anchor"
    assert L["env"]["SL_AGENT"] == "claude"


def test_the_owners_note_reaches_the_sessions_actual_prompt(fx):
    # The end-to-end proof of the whole feature: the brief the shim reads (which start-session.sh
    # cats into the interactive agent as its opening message) contains the owner's words verbatim.
    verb, tmp_path, _home, _log = fx
    note = "the departures board is lying about SL-12"
    verb.execute(SLUG, note, _snapshot([_flight(12, flights.PARKED)]))
    brief = _launches(tmp_path)[0]["brief"]
    assert brief is not None, "the brief must exist BEFORE the shim runs — it aborts without one"
    assert note in brief
    assert "sl-debugger" in brief
    assert "SL-12" in brief, "the trouble the UI was showing rides along with the note"


def test_execute_with_an_empty_note_still_launches(fx):
    verb, tmp_path, _home, _log = fx
    res = verb.execute(SLUG, "", _snapshot(alert={}))
    assert res["ok"] is True, res
    assert "no note" in _launches(tmp_path)[0]["brief"].lower()


def test_the_brief_lands_where_the_engine_looks_for_it(fx):
    verb, tmp_path, home, _log = fx
    verb.execute(SLUG, "x", _snapshot())
    assert _launches(tmp_path)[0]["brief_path"] == str(home / "briefs" / "d1.md")
    assert (home / "briefs" / "d1.md").exists(), "the brief persists — it is the session's record"


# ---- single-flight (DoD) ----

def test_preflight_reports_a_live_fixer_and_the_button_refuses(fx):
    verb, tmp_path, home, _log = fx
    _live_lock(home, "d1")
    pre = verb.preflight(SLUG, _snapshot(alert={}))
    assert pre["live"] is True, "a live worker.d*.lock IS a live debugger — the engine's convention"
    res = verb.execute(SLUG, "again", _snapshot(alert={}))
    assert res["ok"] is False
    assert res["live"] is True
    assert "already" in (res.get("error") or "").lower()
    assert _launches(tmp_path) == [], "NOTHING may launch while a fixer is live"


def test_a_dead_lock_does_not_block_a_launch(fx):
    # A crashed session leaves its lock behind. A stale lock must not wedge the button forever —
    # liveness is the PID's, not the file's.
    verb, tmp_path, home, _log = fx
    _live_lock(home, "d1", pid=999_999)          # a pid that cannot be alive
    pre = verb.preflight(SLUG, _snapshot())
    assert pre["live"] is False
    res = verb.execute(SLUG, "", _snapshot())
    assert res["ok"] is True, res
    assert res["id"] == "d2", "the dead d1 is never re-used — its brief stays its own record"


def test_preflight_is_read_only(fx):
    verb, tmp_path, home, log = fx
    verb.preflight(SLUG, _snapshot(alert={}))
    assert _launches(tmp_path) == [], "the preflight launches nothing"
    assert not (home / "briefs").exists(), "the preflight composes no brief"
    assert not log.exists(), "the preflight records no launch"


def test_preflight_shows_the_trouble_the_dialog_will_send(fx):
    verb, _tmp, _home, _log = fx
    pre = verb.preflight(SLUG, _snapshot([_flight(12, flights.PARKED)], alert={}))
    kinds = [i["kind"] for i in pre["trouble"]["items"]]
    assert "alert" in kinds and flights.PARKED in kinds


# ---- fail closed (DoD) ----

def test_unwatched_repo_is_refused_before_any_subprocess(fx):
    verb, tmp_path, _home, _log = fx
    res = verb.execute("someone/else", "hi", _snapshot())
    assert res["ok"] is False
    assert res["error"] == "unknown repo"
    assert _launches(tmp_path) == []


def test_unresolvable_shim_says_so_plainly_and_launches_nothing(fx, monkeypatch):
    # The conftest's DEFAULT posture: SL_LAUNCH_SESSION points at an absent path. The UI must say
    # so in words the owner can act on, and nothing may launch.
    verb, tmp_path, home, _log = fx
    monkeypatch.setenv("SL_LAUNCH_SESSION", "/nonexistent/not-a-shim")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "launch-session.sh" in res["error"] or "could not" in res["error"].lower()
    assert _launches(tmp_path) == []
    assert not (home / "briefs" / "d1.md").exists(), (
        "an unresolvable shim must not leave a brief behind — nothing launched, no trace")


def test_no_cmux_pane_fails_closed(fx):
    # No recorded runner anchor (a cleanly-stopped loop) ⇒ no pane ⇒ the shim would abort. Refuse
    # BEFORE launching, with the reason in plain words.
    verb, tmp_path, home, _log = fx
    (home / "state" / "runner.anchor.json").unlink()
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "pane" in res["error"].lower() or "cmux" in res["error"].lower()
    assert _launches(tmp_path) == []


def test_a_corrupt_anchor_fails_closed_not_crash(fx):
    verb, _tmp, home, _log = fx
    (home / "state" / "runner.anchor.json").write_text("{not json")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "pane" in res["error"].lower() or "cmux" in res["error"].lower()


def test_a_failed_launch_is_never_a_silent_success(fx, monkeypatch):
    verb, _tmp, _home, _log = fx
    monkeypatch.setenv("SL_FIXER_RC", "1")
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False, "rc != 0 means the shim did NOT verify delivery"
    assert res["error"]


def test_a_hung_launch_trips_the_timeout_and_reports_it(fx, monkeypatch):
    verb, _tmp, _home, _log = fx
    monkeypatch.setenv("SL_FIXER_SLEEP", "3")
    verb._timeout = 0.4                       # a module-constant-free shrink, mirroring tidy/restart
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False
    assert "timed out" in res["error"].lower()


def test_the_adapter_never_raises_into_the_caller(fx, monkeypatch):
    # A button that 500s is worse than one that says "no". Every failure mode is a dict.
    verb, _tmp, _home, _log = fx
    monkeypatch.setenv("SL_LAUNCH_SESSION", str(Path(__file__)))   # not executable → OSError
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is False and res["error"]


# ---- the launch is recorded (DoD) ----

def test_every_launch_is_recorded_with_a_timestamp_and_the_note(fx):
    verb, _tmp, _home, log = fx
    verb.execute(SLUG, "the queue is frozen", _snapshot(runner_down=True))
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == 1_700_000_000.0
    assert r["note"] == "the queue is frozen", "a later reader must see WHY the fixer ran"
    assert r["id"] == "d1"
    assert r["repo"] == SLUG
    assert r["ok"] is True
    assert r["operator"] == "William"
    assert "runner-down" in r["trouble"], "what the UI was showing at tap time"


def test_a_failed_launch_is_recorded_too(fx, monkeypatch):
    # The record is the honest history: an attempt that failed is exactly what a later reader
    # needs to see, so it can never be quietly absent.
    verb, _tmp, _home, log = fx
    monkeypatch.setenv("SL_FIXER_RC", "1")
    verb.execute(SLUG, "nope", _snapshot())
    rows = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1 and rows[0]["ok"] is False


def test_the_log_lives_in_the_dashboards_own_dir_not_a_repos_loop_state():
    # decision B.4 / lib/desk: the center's own facts live under $SL_HOME/command-center/, BESIDE
    # the loop state homes it reads — never inside one.
    p = str(fixer_mod.default_log_path())
    assert p.endswith("/command-center/fixer-log.jsonl")


def test_a_log_write_failure_never_breaks_the_launch(fx):
    verb, tmp_path, _home, _log = fx
    verb._log_path = "/nonexistent/dir/deep/fixer-log.jsonl"
    res = verb.execute(SLUG, "x", _snapshot())
    assert res["ok"] is True, "the record is bookkeeping — it must never fail the owner's tap"
    assert len(_launches(tmp_path)) == 1


# ---- the bright line ----

def test_no_model_call_or_network_in_the_adapter():
    # The owner's 2026-07-15 ruling in code: the dashboard composes a prompt by string assembly and
    # hands it to the engine's shim. No model call, no standing seat, no network — the AI runs in
    # the launched session, outside this process, because a human tapped a button.
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
