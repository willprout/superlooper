"""`superlooper watchdog` (issue #66): the CLI one-shot around lib/watchdog.py — invoked as a
real subprocess, with everything external injected: fake-gh via SL_GH, the state base via
SL_HOME, a FAKE launch script via SL_LAUNCH_SESSION (the DoD's fake shim — no test reaches a
real cmux/claude), and the usage meter via SL_FAKE_USAGE (no test reaches the Keychain).

The launchd job runs this exact command every few minutes; each invocation reads the health
signals, advances the episode state in state/watchdog.json, and exits.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import journal

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"


@pytest.fixture
def cli():
    """Load the `superlooper` entry-point script as a module (it guards main() under
    __name__ == '__main__', so importing runs no command) to unit-test its file-lock helpers."""
    loader = importlib.machinery.SourceFileLoader("superlooper_cli", str(CLI))
    spec = importlib.util.spec_from_loader("superlooper_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
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
  printf 'VERIFY %s\\n' "${SL_LAUNCH_VERIFY_SECONDS:-unset}"
} >> "$STUB_LOG"
exit "${STUB_RC:-0}"
"""

_FAKE_RESURRECT = """#!/bin/bash
{ printf 'ARGS %s\\n' "$*"
  printf 'PANE %s\\n' "${SL_PANE:-}"
  printf 'ROOT %s\\n' "${SL_RUN_ROOT:-}"
  printf 'BIN %s\\n' "${SL_SUPERLOOPER_BIN:-}"
  printf 'VERIFY %s\\n' "${SL_LAUNCH_VERIFY_SECONDS:-unset}"
  # Snapshot the watchdog state AS THE LAUNCHER SEES IT — proof of what was already durable on
  # disk at launch time, not merely what got written once the launcher returned.
  printf 'STATE_AT_LAUNCH %s\\n' "$(cat "${SL_RUN_ROOT:-}/state/watchdog.json" 2>/dev/null | tr -d '\\n ')"
} >> "$RESURRECT_LOG"
exit "${STUB_RESURRECT_RC:-0}"
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
        # A fake runner-resurrection launcher (issue #208): logs its call + honors STUB_RESURRECT_RC,
        # exactly like the fake launch-session above — no test reaches real cmux / a real runner.
        self.resurrect_log = tmp_path / "resurrect-calls.log"
        resurrect = tmp_path / "fake-resurrect-runner.sh"
        resurrect.write_text(_FAKE_RESURRECT)
        resurrect.chmod(resurrect.stat().st_mode | stat.S_IXUSR)
        self.env = {**os.environ,
                    "HOME": str(tmp_path / "userhome"),
                    "SL_HOME": str(tmp_path / "slhome"),
                    "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
                    "SL_CMUX": "/nonexistent/superlooper-test-cmux",
                    "SL_LAUNCH_SESSION": str(launch),
                    "SL_RESURRECT_RUNNER": str(resurrect),
                    "STUB_LOG": str(self.stub_log),
                    "RESURRECT_LOG": str(self.resurrect_log),
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

    def runner_lock(self, pid):
        # A pidfile the dead runner left behind (a crash leaves it; a clean stop removes it).
        (self.home / "state" / "runner.lock").write_text(str(pid))

    def wjournal(self):
        return [r for r in journal.read(str(self.home)) if r.get("act") == "watchdog"]

    def rjournal(self):
        return [r for r in journal.read(str(self.home)) if r.get("act") == "runner_resurrect"]

    def launch_calls(self):
        if not self.stub_log.exists():
            return []
        return [l.split(" ", 1)[1] for l in self.stub_log.read_text().splitlines()
                if l.startswith("ARGS ")]

    def resurrect_calls(self):
        if not self.resurrect_log.exists():
            return []
        return [l.split(" ", 1)[1] for l in self.resurrect_log.read_text().splitlines()
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
    # the gh fixtures hold eligible agent-ready work (#101/#102) with empty lanes. The eligibility
    # view is now what the SCHEDULER would launch NOW (scheduler.launchable): with the default
    # `lanes: 2` and disjoint touch-areas (frontend / api), BOTH are launchable this tick, so both
    # no-progress clocks start. One glimpse is not an episode.
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


def test_launch_env_pins_the_verify_window(tmp_path):
    # Fresh review P1-1: launch-session.sh inherits the caller's SL_LAUNCH_VERIFY_SECONDS. An
    # ambient large value (a debugging export, a LaunchAgent env) would let the launch
    # subprocess outlive the watchdog's own timeout — rc=124 counts a FAILED attempt while the
    # tab delivers late and a real session starts, so a later check could launch a SECOND
    # session for the same episode. The watchdog must pin the verify window itself.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.episode(age_seconds=3600)
    rig.anchor()
    r = rig.run(SL_LAUNCH_VERIFY_SECONDS="600")
    assert r.returncode == 0, r.stderr
    assert "VERIFY 30" in rig.stub_log.read_text()


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


# --------------------------- the atomic singleton lock (issue #92) ---------------------------
# The per-state-home lock is acquired atomically (create-with-content via `ln`/O_EXCL, NOT
# check-then-write) so a launchd firing overlapping a hand-run check cannot interleave their
# read-evaluate-write of watchdog.json. Ownership-checked release + content-guarded dead-holder
# reclaim, ported from start-session.sh's proven worker-singleton.

def test_acquire_lock_creates_with_our_pid(cli, tmp_path):
    lock = tmp_path / "watchdog.lock"
    assert cli._watchdog_acquire_lock(str(lock)) is True
    assert lock.read_text().strip() == str(os.getpid())


def test_acquire_lock_yields_to_a_live_holder(cli, tmp_path):
    lock = tmp_path / "watchdog.lock"
    lock.write_text(str(os.getpid()))                     # our own pid is live by construction
    assert cli._watchdog_acquire_lock(str(lock)) is False
    assert lock.read_text().strip() == str(os.getpid())   # a live holder's lock is NEVER clobbered


def test_acquire_lock_reclaims_a_dead_holder(cli, tmp_path):
    lock = tmp_path / "watchdog.lock"
    lock.write_text("999999")                             # a dead pid (test_runner's convention)
    assert cli._watchdog_acquire_lock(str(lock)) is True
    assert lock.read_text().strip() == str(os.getpid())


def test_acquire_lock_reclaims_garbage_content(cli, tmp_path):
    lock = tmp_path / "watchdog.lock"
    lock.write_text("not-a-pid\n")                        # a truncated/corrupt write is not live
    assert cli._watchdog_acquire_lock(str(lock)) is True
    assert lock.read_text().strip() == str(os.getpid())


def test_release_lock_is_ownership_checked(cli, tmp_path):
    lock = tmp_path / "watchdog.lock"
    lock.write_text("999999")                             # a DIFFERENT holder (not us)
    cli._watchdog_release_lock(str(lock))
    assert lock.exists()                                  # never remove a lock that isn't ours
    lock.write_text(str(os.getpid()))
    cli._watchdog_release_lock(str(lock))
    assert not lock.exists()


def test_acquire_lock_exactly_one_of_many_concurrent_checks_wins_a_free_slot(cli, tmp_path):
    # The core atomicity the old check-then-write LACKED: many checks firing at once against a FREE
    # (absent) lock — a launchd firing overlapping a hand-run check — must resolve to EXACTLY ONE
    # winner, never two proceeding into a duplicate-episode / burned-launch race. The old
    # `if _live_lock: return; open(lock,"w")` let every racer past (absent -> not live) and all
    # write; the `ln`/O_EXCL create arbitrates to one. fork (not spawn) so children share this
    # loaded CLI module; each exits 0 iff it acquired.
    lock = tmp_path / "watchdog.lock"                     # absent: a free slot
    n = 16
    pids = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:                                      # child
            got = False
            try:
                got = cli._watchdog_acquire_lock(str(lock))
            except BaseException:
                got = False
            if got:
                time.sleep(1.0)      # HOLD the slot (a real check does its work while holding) so
                                     # every racer that fails the link sees us LIVE and yields
            os._exit(0 if got else 1)
        pids.append(pid)
    wins = 0
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
            wins += 1
    assert wins == 1, f"expected exactly one winner, got {wins}"
    assert lock.exists()                                  # the winner's lock stands


def test_watchdog_reclaims_a_dead_holders_lock_and_runs(tmp_path):
    # End to end: a lock left by a DEAD holder (a prior check that crashed mid-run) is reclaimed,
    # and the check proceeds — the lock never wedges the detector forever.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)                                   # a stale heartbeat -> an episode opens
    (rig.home / "state" / "watchdog.lock").write_text("999999")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert [x["outcome"] for x in rig.wjournal()] == ["notified"]
    assert (rig.home / "state" / "watchdog.json").exists()


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


def _seed_territory_claim(rig, status, iid="i106", touches=None):
    """Seed issues.json with a merge-producing issue holding a territory claim (`gating` /
    `holding`) — a finished build gate-waiting on CI / holding through a merge freeze. It occupies
    NO lane, but its declared territory is still protected."""
    import loopstate
    st = loopstate.new_state()
    claim = loopstate.new_issue()
    claim["status"] = status
    claim["type"] = "build"
    claim["declared_touches"] = touches if touches is not None else []   # [] == wildcard '*'
    st["issues"][iid] = claim
    loopstate.save(str(rig.home / "state" / "issues.json"), st)


@pytest.mark.parametrize("status", ["gating", "holding"])
def test_territory_claim_suppresses_no_progress(tmp_path, status):
    # The #92 binding bug: a finished merge-producing build gate-waiting on CI (or holding through
    # a merge freeze) with a wildcard claim, plus eligible approved work behind it and empty lanes,
    # is a DESIGNED-SAFE wait — the scheduler holds the eligible work behind the territory claim.
    # The no-progress clock must not run and nothing must notify.
    rig = _Rig(tmp_path)
    rig.heartbeat(10)                                # the loop looks healthy...
    _seed_territory_claim(rig, status)               # ...but a gating/holding claim holds the queue
    old = time.time() - 3600
    (rig.home / "state" / "watchdog.json").write_text(json.dumps(
        {"episode": None, "no_progress_since": {"101": old, "102": old}, "next_debugger": 1}))
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []                       # no episode opens
    assert rig.wstate()["no_progress_since"] == {}    # the clock does not run
    assert rig.wstate()["episode"] is None
    assert rig.launch_calls() == []


def test_refused_ready_read_freezes_clocks_and_holds_the_episode(tmp_path):
    # DoD gap #2 + #4, end to end: probe SUCCEEDS but the agent-ready list read is REFUSED (an
    # hourly GraphQL dead zone). The no-progress condition is UNOBSERVABLE, not cleared, so an open
    # no_progress episode is HELD (not stood down) and the clocks FREEZE — no duplicate owner text,
    # no restarted grace.
    rig = _Rig(tmp_path)
    rig.heartbeat(10)                                # heartbeat fresh: only gh is dark
    old = time.time() - 3600
    ep = {"signals": ["no_progress"], "opened_at": time.time() - 600, "detail": "seeded",
          "launched_at": None, "launch_id": None, "launch_attempts": 0,
          "launch_failure_notified": False}
    (rig.home / "state" / "watchdog.json").write_text(json.dumps(
        {"episode": ep, "no_progress_since": {"101": old}, "next_debugger": 1}))
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "--label agent-ready", "times": 5}]))
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.wjournal() == []                       # a quiet hold: no stand_down, no notify
    st = rig.wstate()
    assert st["episode"] is not None                  # the episode is HELD across the outage
    assert st["episode"]["signals"] == ["no_progress"]
    assert st["episode"]["opened_at"] == ep["opened_at"]      # SAME episode, grace clock intact
    assert st["no_progress_since"] == {"101": old}    # clocks FROZEN, not reset
    assert rig.launch_calls() == []


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


# ============================ runner resurrection (issue #208) ============================
# The watchdog RESTARTS a provably-gone runner (heartbeat stale AND its recorded pid dead) instead
# of only hiring a debugger. `runner.lock` naming a DEAD pid is the crash signal; a clean stop
# removes the pidfile, so it is not resurrected. The launcher (resurrect-runner.sh) is faked via
# SL_RESURRECT_RUNNER, exactly like the debugger launcher.

def test_provably_gone_runner_is_resurrected_not_debugged(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)                              # heartbeat stale...
    rig.runner_lock(999999)                          # ...AND the recorded pid is dead (a crash)
    rig.anchor(pane="ANCHOR-PANE-1")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    # the runner was RESTARTED (r1), NOT debugged
    calls = rig.resurrect_calls()
    assert len(calls) == 1
    assert calls[0] == f"--cwd {rig.repo} r1"
    assert rig.launch_calls() == []                  # no debugger for a dead runner
    log = rig.resurrect_log.read_text()
    assert "PANE ANCHOR-PANE-1" in log               # into the recorded anchor pane
    assert f"ROOT {rig.home}" in log
    assert "VERIFY 30" in log                         # verify window pinned under the timeout
    # a distinct journal act + a loud restart notification
    recs = rig.rjournal()
    assert [x["outcome"] for x in recs] == ["resurrected"]
    assert recs[0]["id"] == "r1"
    st = rig.wstate()
    assert st["episode"] is None                     # no debugger episode opened
    assert len(st["resurrection"]["attempts"]) == 1
    assert st["next_resurrection"] == 2
    assert "resurrected the runner" in r.stdout


def test_clean_stop_no_pidfile_is_not_resurrected(tmp_path):
    # A deliberately-stopped runner removes its pidfile. Heartbeat stale + NO runner.lock is NOT
    # 'provably gone' — it must not be auto-restarted (that would fight the owner). It falls to the
    # existing debugger episode instead.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)                              # no runner.lock written
    rig.anchor()
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.resurrect_calls() == []               # never resurrected
    assert [x["outcome"] for x in rig.wjournal()] == ["notified"]   # the debugger episode opens
    assert rig.rjournal() == []


def test_live_runner_pidfile_is_wedged_not_dead(tmp_path):
    # heartbeat stale but the recorded pid is ALIVE = a wedged loop (up, not ticking). That is the
    # debugger's job, not a restart.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(os.getpid())                     # a real, alive pid
    rig.anchor()
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.resurrect_calls() == []
    assert [x["outcome"] for x in rig.wjournal()] == ["notified"]   # debugger episode


def test_failed_resurrection_journals_and_notifies_the_failure(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(999999)
    rig.anchor()
    r = rig.run(STUB_RESURRECT_RC=2)                 # the launcher could not bring the runner up
    assert r.returncode == 0, r.stderr
    assert len(rig.resurrect_calls()) == 1
    recs = rig.rjournal()
    assert [x["outcome"] for x in recs] == ["resurrect_failed"]
    assert recs[0]["rc"] == 2
    # the attempt still counts toward the crash-loop cap even though delivery failed
    assert len(rig.wstate()["resurrection"]["attempts"]) == 1
    # the hand-run summary must NOT claim "resurrected" when the restart FAILED
    assert "restart FAILED" in r.stdout
    assert "resurrected the runner" not in r.stdout


def test_resurrect_pins_the_verify_window(tmp_path):
    # Mirror of test_launch_env_pins_the_verify_window for the resurrect path: an ambient
    # SL_LAUNCH_VERIFY_SECONDS must NOT leak through, or resurrect-runner.sh's poll could outlive the
    # subprocess timeout and get killed mid-cleanup. The watchdog HARDCODES 30.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(999999)
    rig.anchor()
    r = rig.run(SL_LAUNCH_VERIFY_SECONDS="600")
    assert r.returncode == 0, r.stderr
    assert "VERIFY 30" in rig.resurrect_log.read_text()


def test_cap_of_zero_escalates_without_resurrecting_and_summary_is_honest(tmp_path):
    # resurrection_max_per_hour=0 disables auto-restart: a provably-gone runner escalates LOUDLY
    # (once) and is NOT restarted; the hand-run summary must say the runner is DOWN, never "healthy".
    rig = _Rig(tmp_path, watchdog_cfg={"resurrection_max_per_hour": 0})
    rig.heartbeat(3600)
    rig.runner_lock(999999)
    rig.anchor()
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.resurrect_calls() == []               # disabled: nothing restarted
    assert [x["outcome"] for x in rig.rjournal()] == ["resurrect_capped"]
    assert "DISABLED" in r.stdout or "PAUSED" in r.stdout
    assert "healthy" not in r.stdout


def test_the_attempt_is_durable_before_the_launcher_runs(tmp_path):
    # Fresh-review P2-6: the attempt was persisted only AFTER the resurrect subprocess, which can run
    # up to WATCHDOG_LAUNCH_TIMEOUT (180s). A watchdog killed inside that window lost the attempt, so
    # the next check restarted again with the slot un-burned — and the rolling-hour cap is the ONLY
    # anti-storm guard the DoD has ("no relaunch storms"). The attempt must be on disk BEFORE the
    # launcher is invoked, so a crash mid-restart can only ever OVER-count, never under-count.
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(999999)
    rig.anchor()
    r = rig.run()
    assert r.returncode == 0, r.stderr
    snap = [l.split(" ", 1)[1] for l in rig.resurrect_log.read_text().splitlines()
            if l.startswith("STATE_AT_LAUNCH ")]
    assert len(snap) == 1, rig.resurrect_log.read_text()
    at_launch = json.loads(snap[0])
    assert len(at_launch["resurrection"]["attempts"]) == 1, at_launch


def test_every_capped_check_stays_honest_not_just_the_first(tmp_path):
    # Fresh-review P1-1: the escalation JOURNAL/notify is deduped to once per capped streak (no
    # storm), but the hand-run summary must be honest on EVERY check. Deriving "capped" from the
    # (deduped) journal made the 3rd check print "healthy — no signal" while the runner was still
    # provably gone — the machine telling the owner the loop is fine during an outage.
    rig = _Rig(tmp_path, watchdog_cfg={"resurrection_max_per_hour": 1})
    rig.anchor()
    rig.runner_lock(999999)                          # dead pid: the runner stays provably gone
    rig.heartbeat(3600)
    first = rig.run()                                # burns the single cap slot
    assert "resurrected the runner" in first.stdout, first.stdout
    second = rig.run()                               # cap hit: escalates loudly, once
    assert "PAUSED" in second.stdout and "healthy" not in second.stdout
    third = rig.run()                                # still capped: silent, but MUST stay honest
    assert "healthy" not in third.stdout, third.stdout
    assert "DOWN" in third.stdout, third.stdout
    # the escalation itself stays deduped — one notify across the capped streak, never per check
    assert [x["outcome"] for x in rig.rjournal()] == ["resurrected", "resurrect_capped"]


def test_no_resolvable_pane_is_a_loud_resurrect_failure_not_a_crash(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(999999)                          # dead runner, but no anchor + no SL_PANE
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.resurrect_calls() == []               # nothing to launch INTO — script never ran
    recs = rig.rjournal()
    assert [x["outcome"] for x in recs] == ["resurrect_failed"]
    assert recs[0]["rc"] == "no_pane"


def test_kill_switch_suppresses_resurrection(tmp_path):
    rig = _Rig(tmp_path)
    rig.heartbeat(3600)
    rig.runner_lock(999999)
    rig.anchor()
    (rig.home / "state" / "WATCHDOG_OFF").write_text("")
    r = rig.run()
    assert r.returncode == 0, r.stderr
    assert rig.resurrect_calls() == []               # WATCHDOG_OFF disables the whole path
    assert rig.rjournal() == []
    assert [x["outcome"] for x in rig.wjournal()] == ["disabled"]
