"""The watchdog and the runner's process home (issue #306).

The watchdog's OWN home was decided the same way and needed no move: it already runs as a
`gui/$UID` LaunchAgent (`templates/launchd.watchdog.plist`), which is outside the session host by
construction and inside the Aqua session the keychain rule requires. What DID need deciding is the
one thing it does that assumes the runner's home — restarting a provably-gone runner (issue #208).

Under the pane home that means birthing a fresh tab in the dead runner's recorded anchor pane. Under
the login-item home there is no anchor and no tab: the restart is `launchctl kickstart -k` against
the job in the gui domain. Getting this wrong is the plan's own named migration landmine — the
watchdog left calling a launcher that cannot work means no unattended repair at exactly the moment
repair is needed.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

# Records every argv it is handed, so a test can assert which domain and which job were addressed.
_FAKE_LAUNCHCTL = '#!/bin/sh\necho "$@" >> "$LAUNCHCTL_LOG"\ncase "$1" in managername) echo Aqua;; esac\nexit ${LAUNCHCTL_RC:-0}\n'
_FAKE_RESURRECT = '#!/bin/sh\necho "$@" >> "$RESURRECT_LOG"\nexit 0\n'


@pytest.fixture
def rig(tmp_path):
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    home = tmp_path / "slhome" / "o__r"
    (home / "state").mkdir(parents=True)
    (tmp_path / "userhome").mkdir()

    def _exe(path, body):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    launchctl = _exe(tmp_path / "fake-launchctl", _FAKE_LAUNCHCTL)
    resurrect = _exe(tmp_path / "fake-resurrect-runner.sh", _FAKE_RESURRECT)
    env = {**os.environ,
           "HOME": str(tmp_path / "userhome"), "SL_HOME": str(tmp_path / "slhome"),
           "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
           "SL_CMUX": "/nonexistent/superlooper-test-cmux",
           "SL_LAUNCHCTL": str(launchctl), "SL_UID": "501",
           "SL_RESURRECT_RUNNER": str(resurrect),
           "LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
           "RESURRECT_LOG": str(tmp_path / "resurrect.log"),
           "SL_FAKE_USAGE": json.dumps({"auth_status": "ok", "five_hour_pct": 1.0,
                                        "seven_day_pct": 1.0})}
    env.pop("SL_PANE", None)
    env.pop("GH_FAIL", None)
    return type("Rig", (), {"tmp": tmp_path, "repo": repo, "home": home, "env": env,
                            "launchctl_log": tmp_path / "launchctl.log",
                            "resurrect_log": tmp_path / "resurrect.log"})


def write_config(rig, **over):
    cfg = {"version": 1, "repo": "o/r"}
    cfg.update(over)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))


def provably_gone(rig, pane=True):
    """The exact shape the watchdog resurrects on: heartbeat stale AND the recorded pid dead."""
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 3600))
    (rig.home / "state" / "runner.lock").write_text("999999")
    if pane:
        (rig.home / "state" / "runner.anchor.json").write_text(
            json.dumps({"pane": "ANCHOR-PANE-1", "workspace": "", "window": "", "pid": 1}))


def watchdog(rig):
    return subprocess.run([sys.executable, str(CLI), "watchdog", "--repo", str(rig.repo)],
                          capture_output=True, text=True, env=rig.env, timeout=120)


def _lines(path):
    return path.read_text().splitlines() if path.exists() else []


def test_the_pane_home_still_restarts_a_dead_runner_in_its_anchor_pane(rig):
    # Production regression guard: issue #208's path is untouched by the new home.
    write_config(rig)
    provably_gone(rig)
    r = watchdog(rig)
    assert r.returncode == 0, r.stderr
    assert _lines(rig.resurrect_log) == ["--cwd %s r1" % rig.repo]
    assert not _lines(rig.launchctl_log)


def test_the_login_item_home_restarts_a_dead_runner_by_kickstarting_its_job(rig):
    write_config(rig, runner_home="login-item")
    provably_gone(rig, pane=False)
    r = watchdog(rig)
    assert r.returncode == 0, r.stderr
    # The pane launcher — which needs an anchor that does not exist in this home — is never called.
    assert not _lines(rig.resurrect_log)
    calls = _lines(rig.launchctl_log)
    assert "kickstart -k gui/501/com.superlooper.runner.o__r" in calls, calls
    assert "resurrected the runner" in r.stdout


def test_a_login_item_restart_never_reaches_for_another_launchd_domain(rig):
    write_config(rig, runner_home="login-item")
    provably_gone(rig, pane=False)
    watchdog(rig)
    assert not any("system/" in line for line in _lines(rig.launchctl_log))


def test_a_failed_kickstart_is_reported_as_a_failed_restart_not_a_success(rig):
    # The whole point of issue #208's verify: a restart that did not happen must never be journaled
    # as one, or the morning report says the loop recovered from an outage it is still in.
    write_config(rig, runner_home="login-item")
    provably_gone(rig, pane=False)
    r = watchdog(rig)   # first, healthy — then repeat with the service manager refusing
    assert "resurrected the runner" in r.stdout
    rig.env["LAUNCHCTL_RC"] = "1"
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 3600))
    r2 = watchdog(rig)
    assert "restart FAILED" in r2.stdout or "DOWN" in r2.stdout, r2.stdout


def test_a_login_item_runner_that_is_merely_wedged_is_not_kickstarted(rig):
    # heartbeat stale but the recorded pid ALIVE is a wedged loop, which is the debugger's job.
    # Kickstarting it would kill a live runner mid-tick — the one thing this path must not do.
    write_config(rig, runner_home="login-item")
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 3600))
    (rig.home / "state" / "runner.lock").write_text(str(os.getpid()))
    watchdog(rig)
    assert not any("kickstart" in line for line in _lines(rig.launchctl_log))
