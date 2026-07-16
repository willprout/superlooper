"""runner.py — the account-auth probe + the ~30-min flight recorder (issue #159 / forensics U3).

decide's auth GATE is unit-tested in test_actions.py; these pin the SHELL half the runner owns:

  * the cached probe refreshes on a cadence (bounding `claude` spawns) and writes state/auth_probe.json;
  * the ~30-min FLIGHT RECORDER appends `claude auth status` + the credential keychain mtime to a
    durable, bounded state/auth_history.jsonl — the on-disk record the i336 auth-death class needed;
  * the whole path is Claude-only (agent boundary) — a Codex lane never probes;
  * end-to-end: with a queued issue and a DEAD auth reading, a tick spends NO launch and raises the
    auth_dead ALERT; with a healthy reading the same tick launches normally.

Same rig discipline as test_runner.py: fake-gh via SL_GH, injected run_script + stub usage, and the
auth probe injected per-test (the conftest autouse neutralizes the real one for every other test).
"""
import json
import shutil
from pathlib import Path

import pytest

import loopstate
import runner as runner_mod

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

NOW = 1_750_000_000


def make_config(**over):
    c = {
        "repo": "o/r", "dev_branch": "main", "prod_branch": None,
        "lanes": 2, "affinity": "hard", "areas": {},
        "touches_required": False, "required_checks": ["ci"], "merge_method": "squash",
        "ship_cmd": None, "ship_recheck_cmd": None,
        "report_required_sections": ["Tests"], "bright_lines": [],
        "cleanup_merged_worktrees": True, "report_time": "08:45",
        "models": {"worker": "opus", "answerer": "fable"},
        "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2},
        "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
               "quarantine": [], "nightly_time": "02:00"},
        "notify": {"imessage_to": None, "cmd": None},
        "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
    }
    c.update(over)
    return c


def _dead():
    return {"cli": "logged_out", "keychain_present": True, "keychain_mtime": NOW - 3600,
            "valid": False, "status_raw": '{"loggedIn": false}'}


def _ok():
    return {"cli": "logged_in", "keychain_present": True, "keychain_mtime": NOW - 60,
            "valid": True, "status_raw": '{"loggedIn": true}'}


def _unknown():
    return {"cli": "unknown", "keychain_present": None, "keychain_mtime": None,
            "valid": None, "status_raw": ""}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))
    monkeypatch.delenv("GH_FAIL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"

    calls = []

    def run_script(args, env=None, timeout=None):
        calls.append({"args": [str(x) for x in args], "env": dict(env or {})})
        return 0

    r = runner_mod.Runner(
        repo=str(repo), config=make_config(), state_home=str(home), pane="pane-1",
        run_script=run_script,
        fetch_usage=lambda: {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0})
    r._anchor_status = lambda: {"ok": True, "reason": ""}
    return type("Rig", (), {"r": r, "calls": calls, "home": home, "repo": repo})


def _state(rig, *parts):
    return rig.home / "state" / Path(*parts)


def _history_lines(rig):
    p = _state(rig, "auth_history.jsonl")
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _launch_calls(rig):
    return [c for c in rig.calls if c["args"] and c["args"][0].endswith("launch-session.sh")]


# --------------------------- the cached probe ---------------------------

def test_refresh_auth_caches_the_snapshot_and_writes_the_probe_file(rig):
    rig.r._probe_auth = lambda: _dead()
    rig.r._refresh_auth(NOW)
    snap = rig.r.auth_view()
    assert snap["valid"] is False and snap["cli"] == "logged_out"
    assert snap["checked_at"] == NOW
    on_disk = loopstate.load(str(_state(rig, "auth_probe.json")))
    assert on_disk["valid"] is False and on_disk["keychain_mtime"] == NOW - 3600


def test_refresh_auth_respects_the_cadence(rig):
    n = {"c": 0}

    def probe():
        n["c"] += 1
        return _ok()

    rig.r._probe_auth = probe
    rig.r._refresh_auth(NOW)
    rig.r._refresh_auth(NOW + 5)            # within AUTH_REFRESH_SECONDS: no re-probe
    assert n["c"] == 1
    rig.r._refresh_auth(NOW + runner_mod.AUTH_REFRESH_SECONDS + 1)
    assert n["c"] == 2
    rig.r._refresh_auth(NOW + 9999, force=True)   # force ignores the cadence
    assert n["c"] == 3


def test_refresh_auth_survives_a_throwing_probe(rig):
    def boom():
        raise RuntimeError("claude not on PATH")

    rig.r._probe_auth = boom
    rig.r._refresh_auth(NOW)                 # must not raise
    assert rig.r.auth_view() is None         # last-good (none) stands -> fail open downstream


# --------------------------- the flight recorder ---------------------------

def test_capture_writes_the_flight_recorder(rig):
    rig.r._probe_auth = lambda: _dead()
    rig.r._capture_auth(NOW)
    lines = _history_lines(rig)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["at"] == NOW and rec["valid"] is False and rec["cli"] == "logged_out"
    assert rec["keychain_mtime"] == NOW - 3600


def test_capture_respects_the_30_min_cadence(rig):
    rig.r._probe_auth = lambda: _ok()
    rig.r._capture_auth(NOW)                                  # first sample (boot)
    rig.r._capture_auth(NOW + 60)                             # within 30 min: no new sample
    assert len(_history_lines(rig)) == 1
    rig.r._capture_auth(NOW + runner_mod.AUTH_CAPTURE_SECONDS + 1)
    assert len(_history_lines(rig)) == 2


def test_capture_is_bounded(rig, monkeypatch):
    monkeypatch.setattr(runner_mod, "AUTH_HISTORY_MAX_LINES", 6)
    rig.r._probe_auth = lambda: _ok()
    for i in range(10):
        rig.r._capture_auth(NOW + i * (runner_mod.AUTH_CAPTURE_SECONDS + 1))
    lines = _history_lines(rig)
    assert len(lines) <= 6                                    # trimmed to the recent tail
    assert lines[-1]["at"] == NOW + 9 * (runner_mod.AUTH_CAPTURE_SECONDS + 1)   # newest kept


def test_capture_survives_a_throwing_probe(rig):
    rig.r._probe_auth = lambda: (_ for _ in ()).throw(OSError("boom"))
    rig.r._capture_auth(NOW)                                  # must not raise
    assert _history_lines(rig) == []                          # no snapshot -> nothing recorded


# --------------------------- Claude-only (agent boundary) ---------------------------

def test_codex_agent_never_probes_or_captures(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    repo = tmp_path / "repo"; repo.mkdir()
    home = tmp_path / "home"
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return _dead()

    r = runner_mod.Runner(repo=str(repo), config=make_config(), state_home=str(home),
                          pane="p", agent="codex", run_script=lambda *a, **k: 0,
                          fetch_usage=lambda: {"auth_status": "ok"})
    r._probe_auth = probe
    r._refresh_auth(NOW)
    r._capture_auth(NOW)
    assert calls["n"] == 0                                    # never shelled out
    assert r.auth_view() is None
    assert not (home / "state" / "auth_history.jsonl").exists()


# --------------------------- demand gating ---------------------------

def test_wants_session_start_true_on_queue_or_exited(rig):
    rig.r.tick(now=NOW)                                       # polls: i101 (agent-ready) lands queued
    assert rig.r._wants_session_start() is True              # fresh-queue demand
    # ...and an exited marker alone (no fresh queue) is relaunch demand too.
    exdir = _state(rig, "exited"); exdir.mkdir(parents=True, exist_ok=True)
    (exdir / "i7").write_text("x rc=1")
    rig.r._parsed_by_id = {}                                  # clear the queue
    assert rig.r._wants_session_start() is True


# --------------------------- end-to-end through a real tick ---------------------------

def test_dead_auth_holds_the_queued_launch_and_alerts(rig):
    rig.r._probe_auth = lambda: _dead()
    rig.r.tick(now=NOW)
    assert _launch_calls(rig) == []                          # the queued launch was NOT spent
    alert = loopstate.load(str(_state(rig, "ALERT")))
    assert "auth_dead" in alert["reasons"]
    # the pre-spend gate stamped the snapshot for the dashboard/forensics too
    assert loopstate.load(str(_state(rig, "auth_probe.json")))["valid"] is False


def test_healthy_auth_launches_normally_through_a_tick(rig):
    rig.r._probe_auth = lambda: _ok()
    rig.r.tick(now=NOW)
    assert len(_launch_calls(rig)) >= 1                       # feeding a healthy probe never blocks launches
    assert not (_state(rig, "ALERT")).exists()


def test_unknown_auth_probe_does_not_block_a_tick(rig):
    # A dark probe (couldn't run) must fail OPEN — launches proceed, no alert.
    rig.r._probe_auth = lambda: _unknown()
    rig.r.tick(now=NOW)
    assert len(_launch_calls(rig)) >= 1
    assert not (_state(rig, "ALERT")).exists()
