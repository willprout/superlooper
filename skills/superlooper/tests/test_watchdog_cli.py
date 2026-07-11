"""`superlooper watchdog` (issue #66): the CLI one-shot around lib/watchdog.py — invoked as a
real subprocess, with everything external injected: fake-gh via SL_GH, the state base via
SL_HOME, a FAKE launch script via SL_LAUNCH_SESSION (the DoD's fake shim — no test reaches a
real cmux/claude), and the usage meter via SL_FAKE_USAGE (no test reaches the Keychain).

The launchd job runs this exact command every few minutes; each invocation reads the health
signals, advances the episode state in state/watchdog.json, and exits.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import journal

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

_LOW_USAGE = json.dumps({"auth_status": "ok", "five_hour_pct": 5.0, "seven_day_pct": 5.0})
_EXHAUSTED_USAGE = json.dumps({"auth_status": "ok", "five_hour_pct": 99.0, "seven_day_pct": 5.0})

_FAKE_LAUNCH = """#!/bin/bash
{ printf 'ARGS %s\\n' "$*"
  printf 'PANE %s\\n' "${SL_PANE:-}"
  printf 'ROOT %s\\n' "${SL_RUN_ROOT:-}"
  printf 'MODEL %s\\n' "${SL_MODEL:-}"
  printf 'AGENT %s\\n' "${SL_AGENT:-}"
} >> "$STUB_LOG"
exit "${STUB_RC:-0}"
"""


class _Rig:
    def __init__(self, tmp_path, watchdog_cfg=None):
        self.tmp = tmp_path
        fixdir = tmp_path / "gh"
        shutil.copytree(_FIXTURES, fixdir)
        self.fixdir = fixdir
        self.repo = tmp_path / "repo"
        (self.repo / ".superlooper").mkdir(parents=True)
        cfg = {"version": 1, "repo": "o/r"}
        if watchdog_cfg is not None:
            cfg["watchdog"] = watchdog_cfg
        (self.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))
        self.home = tmp_path / "slhome" / "o__r"
        (self.home / "state").mkdir(parents=True)
        (tmp_path / "userhome").mkdir()
        self.stub_log = tmp_path / "launch-calls.log"
        launch = tmp_path / "fake-launch-session.sh"
        launch.write_text(_FAKE_LAUNCH)
        launch.chmod(launch.stat().st_mode | stat.S_IXUSR)
        self.env = {**os.environ,
                    "HOME": str(tmp_path / "userhome"),
                    "SL_HOME": str(tmp_path / "slhome"),
                    "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
                    "SL_CMUX": "/nonexistent/superlooper-test-cmux",
                    "SL_LAUNCH_SESSION": str(launch),
                    "STUB_LOG": str(self.stub_log),
                    "SL_FAKE_USAGE": _LOW_USAGE}
        # this test process may itself run inside a superlooper worker: its ambient pane must
        # never leak into the subject's pane resolution.
        self.env.pop("SL_PANE", None)
        self.env.pop("GH_FAIL", None)

    # --- state-home seeding helpers ---
    def heartbeat(self, age_seconds):
        (self.home / "state" / "runner.heartbeat").write_text(
            str(int(time.time()) - age_seconds))

    def anchor(self, pane="PANE-UUID-1"):
        (self.home / "state" / "runner.anchor.json").write_text(
            json.dumps({"pane": pane, "workspace": "", "window": "", "pid": 1}))

    def episode(self, age_seconds=3600, signals=("heartbeat_stale",), **fields):
        ep = {"signals": sorted(signals), "opened_at": time.time() - age_seconds,
              "detail": "seeded episode", "launched_at": None, "launch_id": None,
              "launch_attempts": 0, "launch_failure_notified": False}
        ep.update(fields)
        (self.home / "state" / "watchdog.json").write_text(json.dumps(
            {"episode": ep, "no_progress_since": {}, "next_debugger": 1}))

    def wstate(self):
        return json.loads((self.home / "state" / "watchdog.json").read_text())

    def wjournal(self):
        return [r for r in journal.read(str(self.home)) if r.get("act") == "watchdog"]

    def launch_calls(self):
        if not self.stub_log.exists():
            return []
        return [l.split(" ", 1)[1] for l in self.stub_log.read_text().splitlines()
                if l.startswith("ARGS ")]

    def run(self, **extra_env):
        env = {**self.env, **{k: str(v) for k, v in extra_env.items()}}
        return subprocess.run([sys.executable, str(CLI), "watchdog", "--repo", str(self.repo)],
                              env=env, capture_output=True, text=True, timeout=120)


def test_healthy_check_is_quiet_and_persists_state(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(10)
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []                      # no transition, no journal noise
    st = rig.wstate()
    assert st["episode"] is None
    # the gh fixtures hold eligible agent-ready work (#101/#102) with empty lanes: the
    # no-progress clocks started, but one glimpse is not an episode.
    assert set(st["no_progress_since"]) == {"101", "102"}
    assert rig.launch_calls() == []


def test_stale_heartbeat_opens_an_episode_and_journals_notified(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    r = rig.run()
    assert r.returncode == 0, r.stderr
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["notified"]
    assert recs[0]["signals"] == ["heartbeat_stale"]
    assert rig.wstate()["episode"]["signals"] == ["heartbeat_stale"]


def test_grace_elapsed_launches_the_debugger_exactly_once(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor(pane="PANE-UUID-1")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    calls = rig.launch_calls()
    assert len(calls) == 1
    assert calls[0] == f"--cwd {rig.repo} d1"
    log = rig.stub_log.read_text()
    assert "PANE PANE-UUID-1" in log                 # the runner's recorded anchor pane
    assert f"ROOT {rig.home}" in log
    brief = (rig.home / "briefs" / "d1.md").read_text()
    assert "sl-debugger" in brief and "heartbeat_stale" in brief and "full" in brief
    assert "{" not in brief.replace("{}", ""), "unsubstituted placeholder left in the brief"
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["launched"]
    assert recs[0]["id"] == "d1"
    st = rig.wstate()
    assert st["episode"]["launched_at"] is not None
    assert st["next_debugger"] == 2
    # the SAME continuing episode never launches a second session
    r2 = rig.run()
    assert r2.returncode == 0, r2.stderr
    assert len(rig.launch_calls()) == 1


def test_env_pane_overrides_the_anchor(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor(pane="ANCHOR-PANE")
    r = rig.run(SL_PANE="ENV-PANE")
    assert r.returncode == 0, r.stderr
    assert "PANE ENV-PANE" in rig.stub_log.read_text()


def test_failed_launch_journals_and_does_not_mark_launched(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor()
    r = rig.run(STUB_RC=2)
    assert r.returncode == 0, r.stderr
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["launch_failed"]
    assert recs[0]["rc"] == 2
    st = rig.wstate()
    assert st["episode"]["launched_at"] is None
    assert st["episode"]["launch_attempts"] == 1


def test_no_resolvable_pane_is_a_loud_launch_failure_not_a_crash(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)                    # no anchor file, no SL_PANE
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.launch_calls() == []                  # nothing to launch INTO — script never ran
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["launch_failed"]
    assert recs[0]["rc"] == "no_pane"


def test_kill_switch_observes_journals_and_launches_nothing(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor()
    (rig.home / "state" / "WATCHDOG_OFF").write_text("")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.launch_calls() == []
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["disabled"]
    assert recs[0]["signals"] == ["heartbeat_stale"]


def test_live_debugger_lock_blocks_a_second_session(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor()
    # a LIVE debugger session holds worker.d<N>.lock (start-session.sh's singleton); this
    # test process's own pid is live by construction.
    (rig.home / "state" / "worker.d1.lock").write_text(str(os.getpid()))
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.launch_calls() == []
    assert [x["outcome"] for x in rig.wjournal()] == ["skipped_live_session"]


def test_watchdog_singleton_yields_to_a_live_check(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    (rig.home / "state" / "watchdog.lock").write_text(str(os.getpid()))
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []
    assert not (rig.home / "state" / "watchdog.json").exists()


def test_no_progress_trips_from_the_gh_view(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(10)                                # the loop looks healthy...
    old = time.time() - 3600
    (rig.home / "state" / "watchdog.json").write_text(json.dumps(
        {"episode": None, "no_progress_since": {"101": old, "102": old},
         "next_debugger": 1}))                       # ...but the queue has waited an hour
    r = rig.run()
    assert r.returncode == 0, r.stderr
    recs = rig.wjournal()
    assert [x["outcome"] for x in recs] == ["notified"]
    assert recs[0]["signals"] == ["no_progress"]


def test_no_progress_stands_down_when_a_lane_is_busy(tmp_path):
    # frozen-but-building / sequential-build discipline: an occupied lane is progress, and it
    # RESETS the clocks — never a trip.
    import loopstate
    rig = _Rig(tmp_path)
    rig.heartbeat(10)
    st = loopstate.new_state()
    issue = loopstate.new_issue()
    issue["status"] = "running"
    st["issues"]["i7"] = issue
    loopstate.save(str(rig.home / "state" / "issues.json"), st)
    old = time.time() - 3600
    (rig.home / "state" / "watchdog.json").write_text(json.dumps(
        {"episode": None, "no_progress_since": {"101": old, "102": old}, "next_debugger": 1}))
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []
    assert rig.wstate()["no_progress_since"] == {}


def test_usage_reading_exhausted_suppresses_no_progress(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(10)
    old = time.time() - 3600
    (rig.home / "state" / "watchdog.json").write_text(json.dumps(
        {"episode": None, "no_progress_since": {"101": old, "102": old}, "next_debugger": 1}))
    r = rig.run(SL_FAKE_USAGE=_EXHAUSTED_USAGE)
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []
    assert rig.wstate()["no_progress_since"] == {}


def test_recovery_stands_down_silently(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(10)                                # healthy again
    rig.episode(age_seconds=600)                     # an open episode from earlier
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert [x["outcome"] for x in rig.wjournal()] == ["stand_down"]
    assert rig.wstate()["episode"] is None
    assert rig.launch_calls() == []
