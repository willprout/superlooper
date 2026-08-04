"""The CLI half of the runner's process home (issue #306): `superlooper run`'s home-aware boot,
and the `runner-home` verb that installs and verifies the login-item home.

Driven as a real subprocess, like the rest of tests/test_cli.py — argparse, exit codes and the
operator-facing text ARE the contract here. Everything external is a stub: `launchctl` is a script
this file writes, `gh` is the committed fake, and the session host is neutralized to a path that
cannot exist (the conftest ratchet), so nothing here can touch a real job or a real fleet.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "userhome"
    (home / ".superlooper").mkdir(parents=True)
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A stub `git` on PATH so the required-command resolution has something to find; `gh` is
    # resolved through SL_GH like everywhere else in this engine.
    _script(bindir / "git", "exit 0\n")
    launchagents = tmp_path / "LaunchAgents"
    launchagents.mkdir()
    # launchctl stub: `managername` answers Aqua, everything else succeeds quietly. Tests that need
    # another answer rewrite it.
    launchctl = _script(tmp_path / "launchctl",
                        'case "$1" in managername) echo Aqua;; *) : ;; esac\nexit 0\n')
    # A stand-in for the session host binary. It answers the ONE thing the boot preflight asks
    # through the wrapper: a structured "no such agent" for the reserved probe name, which is the
    # host SPEAKING — i.e. reachable. Tests that want a silent host point SL_HERDR at nothing.
    host = _script(tmp_path / "sessionhost",
                   'echo \'{"id":"p","error":{"code":"agent_not_found","message":"no"}}\'\n'
                   'exit 1\n')
    env = {**os.environ,
           "HOME": str(home), "SL_HOME": str(tmp_path / "slhome"),
           "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
           "SL_LAUNCHCTL": str(launchctl), "SL_LAUNCHD_DIR": str(launchagents),
           "SL_UID": "501", "SL_HERDR": str(host),
           "SL_CMUX": "/bin/ls",
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    env.pop("GH_FAIL", None)
    write_config(repo)
    return type("Rig", (), {"env": env, "repo": repo, "home": home, "tmp": tmp_path,
                            "launchctl": launchctl, "launchagents": launchagents,
                            "fixdir": fixdir})


def write_config(repo, **over):
    cfg = {"version": 1, "repo": "o/r", "required_checks": ["ci"]}
    cfg.update(over)
    (repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))


def cli(rig, *args, env_over=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env, timeout=60)


# --------------------------------------------------------------- the verb reports the home

def test_runner_home_names_the_pane_home_and_offers_nothing_to_install(rig):
    r = cli(rig, "runner-home", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pane" in r.stdout
    # There is no job to install for a home that IS a visible tab, and saying so is the point:
    # silently rendering a LaunchAgent for the pane home would re-create exactly the impossible
    # mode issue #33 deleted.
    assert "runner_home" in r.stdout


def test_installing_a_job_for_the_pane_home_is_refused(rig):
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--install")
    assert r.returncode != 0
    assert "pane" in (r.stdout + r.stderr)
    assert not list(rig.launchagents.glob("*.plist"))


def test_runner_home_reports_the_login_item_job(rig):
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "com.superlooper.runner.o__r" in r.stdout
    assert "gui/501" in r.stdout


# --------------------------------------------------------------- --install

def test_install_writes_a_gui_launchagent_with_an_explicit_path(rig):
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--install")
    assert r.returncode == 0, r.stdout + r.stderr
    plist = rig.launchagents / "com.superlooper.runner.o__r.plist"
    assert plist.exists()
    d = plistlib.loads(plist.read_bytes())
    assert d["ProgramArguments"][1] == "run"
    assert str(rig.repo) in [str(a) for a in d["ProgramArguments"]]
    # The spike-proven gotcha: the dir the fake gh actually lives in must be ON the job's PATH,
    # because launchd's own four entries would not include it.
    assert str(_FAKE_GH.parent) in d["EnvironmentVariables"]["PATH"].split(":")
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True
    # It prints the command that activates it rather than activating it silently.
    assert "bootstrap gui/501" in r.stdout


def test_install_does_not_load_the_job_unless_asked(rig):
    write_config(rig.repo, runner_home="login-item")
    log = rig.tmp / "launchctl.log"
    _script(rig.launchctl,
            'echo "$@" >> %s\ncase "$1" in managername) echo Aqua;; esac\nexit 0\n' % log)
    cli(rig, "runner-home", "--repo", str(rig.repo), "--install")
    assert "bootstrap" not in (log.read_text() if log.exists() else "")


def test_install_load_bootstraps_into_the_gui_domain_and_nowhere_else(rig):
    write_config(rig.repo, runner_home="login-item")
    log = rig.tmp / "launchctl.log"
    _script(rig.launchctl,
            'echo "$@" >> %s\ncase "$1" in managername) echo Aqua;; esac\nexit 0\n' % log)
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--install", "--load")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = log.read_text().splitlines()
    assert any(line.startswith("bootstrap gui/501 ") for line in lines), lines
    assert not any("system/" in line for line in lines), lines


def test_install_refuses_when_a_required_command_is_not_on_path(rig):
    # If `gh` cannot be resolved at install time there is no honest PATH to bake into the plist —
    # and a job installed with launchd's four entries fails every GitHub read while looking alive.
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--install",
            env_over={"SL_GH": "/nonexistent/gh", "PATH": "/nonexistent"})
    assert r.returncode != 0
    assert "gh" in (r.stdout + r.stderr)
    assert not list(rig.launchagents.glob("*.plist"))


# --------------------------------------------------------------- --verify (the inside view)

def test_verify_passes_from_a_healthy_aqua_context(rig):
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Aqua" in r.stdout


def test_verify_refuses_a_non_aqua_session(rig):
    # The keychain rule, enforced where the mistake happens rather than taught in a document.
    write_config(rig.repo, runner_home="login-item")
    _script(rig.launchctl, 'case "$1" in managername) echo Background;; esac\nexit 0\n')
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--verify")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "Aqua" in out and "keychain" in out


def test_verify_refuses_when_the_keychain_backed_gh_login_is_dead(rig):
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--verify",
            env_over={"GH_FAIL": "1"})
    assert r.returncode != 0
    assert "gh" in (r.stdout + r.stderr).lower()


# --------------------------------------------------------------- `run`'s home-aware boot

def test_run_in_the_login_item_home_never_asks_for_a_pane(rig):
    # The pane home's boot FAILS HARD without a resolvable pane (D7). Under the login-item home
    # there is no pane to resolve, so that refusal must not fire — a runner that cannot start is
    # exactly as broken as one that starts wrong.
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "login-item" in r.stdout
    assert "pane" not in r.stdout.lower().split("agent=")[0] or "pane=(unset)" not in r.stdout


def test_run_in_the_login_item_home_refuses_when_the_session_host_is_silent(rig):
    # The port of D7's rule: fail HARD before the loop when the launch channel is unreachable,
    # rather than letting every issue burn its retry cap against a host that is not there.
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_HERDR": "/nonexistent/session-host"})
    assert r.returncode == 2
    assert "host" in (r.stdout + r.stderr).lower()


def test_run_in_the_login_item_home_refuses_outside_the_aqua_session(rig):
    write_config(rig.repo, runner_home="login-item")
    _script(rig.launchctl, 'case "$1" in managername) echo Background;; esac\nexit 0\n')
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1")
    assert r.returncode == 2
    assert "Aqua" in (r.stdout + r.stderr)


def test_run_in_the_pane_home_still_fails_hard_without_a_pane(rig):
    # Production regression guard: the pane home's D7 refusal is untouched.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_CMUX": "/nonexistent/cmux", "SL_PANE": ""})
    assert r.returncode == 2
    assert "FATAL" in (r.stdout + r.stderr)


# --------------------------------------------------------------- request-restart speaks the home

def test_request_restart_names_the_right_manual_start_for_the_login_item_home(rig):
    # With no live runner the verb refuses and names how to start one BY HAND. Under the login-item
    # home that instruction is a launchctl kickstart, not "open a tab" — a wrong remedy printed at
    # 3am is the D12 defect class this engine already paid for once.
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "request-restart", "--repo", str(rig.repo))
    out = r.stdout + r.stderr
    assert "kickstart" in out and "gui/501" in out
    assert "tab" not in out.lower()


def test_request_restart_still_names_the_tab_for_the_pane_home(rig):
    r = cli(rig, "request-restart", "--repo", str(rig.repo))
    assert "superlooper run" in r.stdout + r.stderr


def test_install_records_a_relocated_state_home_in_the_job(rig):
    # Found by driving the real installer (issue #306): the plist carried PATH but not SL_HOME, so
    # a job installed from a shell with a relocated state base would compute a DIFFERENT state home
    # once launchd ran it — a second, empty state home beside the real one, with the job's own log
    # still pointing at the first. The installer bakes the environment the job needs; SL_HOME is
    # part of that environment exactly as much as PATH is.
    write_config(rig.repo, runner_home="login-item")
    cli(rig, "runner-home", "--repo", str(rig.repo), "--install")
    d = plistlib.loads((rig.launchagents / "com.superlooper.runner.o__r.plist").read_bytes())
    assert d["EnvironmentVariables"]["SL_HOME"] == rig.env["SL_HOME"]
    # And the log path it writes must be inside that same state home, not another one.
    assert d["StandardOutPath"].startswith(rig.env["SL_HOME"])


def test_install_omits_the_state_home_variable_when_it_is_not_relocated(rig):
    # The default base needs no variable, and baking one in would freeze a path the operator never
    # chose — the job would keep using it after a future default changed.
    write_config(rig.repo, runner_home="login-item")
    env = dict(rig.env)
    env.pop("SL_HOME")
    r = subprocess.run([sys.executable, str(CLI), "runner-home", "--repo", str(rig.repo),
                        "--install"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    d = plistlib.loads((rig.launchagents / "com.superlooper.runner.o__r.plist").read_bytes())
    assert "SL_HOME" not in d["EnvironmentVariables"]
