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
           "SL_HERDR": str(host),
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
    assert "gui/%d" % os.getuid() in r.stdout


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
    assert "bootstrap gui/%d" % os.getuid() in r.stdout


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
    assert any(line.startswith("bootstrap gui/%d " % os.getuid()) for line in lines), lines
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


# --------------------------------------------- the fleet machine arms its own runner (issue #355)
# The fence pre-flight (#326) reads SL_FLEET_FENCE off the RUNNER's own process, and nothing in the
# engine used to set it — so the gate shipped correct and inert on the one machine it was built for.
# These drive the boot through the real CLI: the switch reaches the runner's environment from the
# file the build-up writes, and it reaches it in BOTH homes, because which home a runner lives in
# is a separate decision from whether its launches are gated.


def _arm(rig, body="SL_FLEET_FENCE=required\n"):
    """Write what `superlooper fleet --install` writes on a fleet machine."""
    prefix = Path(rig.env["SL_HOME"]) / "fleet"
    prefix.mkdir(parents=True, exist_ok=True)
    path = prefix / "environment"
    path.write_text("# superlooper fleet environment\n" + body)
    return path


def _in_a_pane(rig):
    """env for a runner booting in the PANE home — today's default and the one a plist misses."""
    cmux = _script(rig.tmp / "cmux-ok", 'echo "surface:1  a tab"\nexit 0\n')
    return {"SL_CMUX": str(cmux), "SL_PANE": "pane-1"}


def test_a_runner_on_a_built_fleet_machine_boots_with_the_launch_gate_armed(rig):
    path = _arm(rig)
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    # The line is read back OUT of the booted process's own environment through the pre-flight's
    # own parser — not printed from the file — so it is evidence about what a launch will see.
    assert "SL_FLEET_FENCE" in r.stdout and "required" in r.stdout
    assert str(path) in r.stdout


def test_the_login_item_home_arms_the_same_way(rig):
    # Whichever home the runner has: the machine's declaration is a machine fact, and a build-up
    # that only reached a LaunchAgent would leave today's default (the pane home) unarmed.
    write_config(rig.repo, runner_home="login-item")
    _arm(rig)
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SL_FLEET_FENCE" in r.stdout and "required" in r.stdout


def test_a_dev_workstation_boots_exactly_as_it_did(rig):
    # The DoD's fourth bullet. No build-up, no file, no switch, no gate — and nothing new printed
    # either: a machine that never opted in must not start explaining a fence it does not have.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SL_FLEET_FENCE" not in r.stdout + r.stderr


def test_the_machines_file_overrides_an_ambient_switch_at_boot(rig):
    # An `export SL_FLEET_FENCE=off` in a shell rc file or a LaunchAgent would otherwise silently
    # disarm the fleet machine — the same inheritance hazard the runner pins SL_ATTENDED empty for.
    _arm(rig)
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0",
            env_over={**_in_a_pane(rig), "SL_FLEET_FENCE": "off"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "required" in r.stdout
    # And it says so rather than quietly winning: an operator who set that variable on purpose has
    # to be able to see that the machine disagreed with them.
    assert "off" in r.stdout


def test_a_machine_may_disarm_itself_in_writing(rig):
    _arm(rig, "SL_FLEET_FENCE=off\n")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SL_FLEET_FENCE" in r.stdout and "off" in r.stdout
    assert "DISARMED" in r.stdout


def test_a_broken_env_file_arms_the_gate_and_says_what_is_wrong(rig):
    # Fail closed: the file exists only because the build-up ran here, so a hand-edit that lost the
    # line must not read as "this machine is unfenced" — and the runner must still start.
    _arm(rig, "")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "required" in out and "SL_FLEET_FENCE" in out
    assert "WARNING" in out


def test_a_file_the_runner_cannot_decode_never_stops_it_from_starting(rig):
    # The P0 of the fresh-agent review. The file this build-up writes is full of em dashes and the
    # docs invite a hand edit, so one round-trip through an editor that saves cp1252 is enough —
    # and `Probe.read_text` catches OSError only. A traceback here is a runner that does not start,
    # repeated on every resurrect, on the machine whose whole point is being unattended.
    prefix = Path(rig.env["SL_HOME"]) / "fleet"
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "environment").write_bytes(b"# superlooper fleet environment \x92\nSL_FLEET_FENCE=required\n")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "Traceback" not in out
    # ...and it fails CLOSED: an unreadable declaration is not "this machine is unfenced".
    assert "required" in out


def test_a_nul_in_the_value_never_stops_the_runner_from_starting(rig):
    # os.environ refuses an embedded NUL with a ValueError; a crash-truncated file is that shape.
    _arm(rig, "SL_FLEET_FENCE=requ\0ired\n")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stdout + r.stderr
    assert "required" in r.stdout


def test_resume_arms_the_gate_too_and_keeps_its_json_parseable(rig):
    # `superlooper resume i<N>` is the FOURTH spawner and the only one besides the runner that can
    # put a WORKER on the host — so on a built fleet machine it must not fly the one launch class
    # the fence exists to contain past a gate the machine already armed. Its stdout is a
    # machine-readable answer the dashboard parses, so the arming says nothing there.
    _arm(rig)
    home = Path(rig.env["SL_HOME"]) / "o__r"
    (home / "state" / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "state" / "sessions" / "i101").write_text(
        "11111111-2222-3333-4444-555555555555\n")
    (home / "worktrees" / "i101").mkdir(parents=True, exist_ok=True)
    seen = rig.tmp / "launcher-env"
    launcher = _script(rig.tmp / "fake-launcher",
                       'printf %%s "$SL_FLEET_FENCE" > %s\nexit 0\n' % seen)
    r = cli(rig, "resume", "i101", "--repo", str(rig.repo), "--json",
            env_over={"SL_LAUNCH_SESSION": str(launcher)})
    assert seen.exists(), r.stdout + r.stderr
    assert seen.read_text() == "required", "a revived WORKER must meet the same gate"
    # stdout stays a single machine-readable object: the dashboard parses it.
    assert "fleet machine" not in r.stdout
    assert json.loads(r.stdout.strip())["id"] == "i101"


def test_the_file_may_not_set_the_rest_of_the_launchers_contract(rig):
    # An allow-list, not a general env file: SL_ATTENDED is pinned empty by the runner precisely
    # because an ambient value must never ride into a worker session, and a machine-level file that
    # could set it would put that whole contract on disk.
    _arm(rig, "SL_FLEET_FENCE=required\nSL_ATTENDED=1\n")
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0", env_over=_in_a_pane(rig))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SL_ATTENDED" in r.stdout and "never applies" in r.stdout


# --------------------------------------------------------------- request-restart speaks the home

def test_request_restart_names_the_right_manual_start_for_the_login_item_home(rig):
    # With no live runner the verb refuses and names how to start one BY HAND. Under the login-item
    # home that instruction is a launchctl kickstart, not "open a tab" — a wrong remedy printed at
    # 3am is the D12 defect class this engine already paid for once.
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "request-restart", "--repo", str(rig.repo))
    out = r.stdout + r.stderr
    assert "kickstart" in out and "gui/%d" % os.getuid() in out
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


def test_an_unreachable_session_host_is_reported_as_no_answer_not_as_a_refusal(rig):
    # Fresh-agent review round 2 (nit, but it points the operator at the wrong end of the wire):
    # everything the probe can observe — a timeout, a missing binary, the wrapper raising — is
    # "we got no answer", never "something answered and said no".
    write_config(rig.repo, runner_home="login-item")
    r = cli(rig, "runner-home", "--repo", str(rig.repo), "--verify",
            env_over={"SL_HERDR": "/nonexistent/session-host"})
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "no answer" in out
    assert "reachable but not answering" not in out


def test_the_status_line_never_reports_an_unreadable_job_as_simply_not_running(rig):
    # Confirmation review: the doctor learned to tell "the service says it is not running" from
    # "we could not tell", and this surface had the same conflation. A status line that says
    # "not running" about a job it could not read is the exact shape that makes a dark runner look
    # merely idle.
    write_config(rig.repo, runner_home="login-item")
    cli(rig, "runner-home", "--repo", str(rig.repo), "--install")

    _script(rig.launchctl, 'echo "com.x = {"\necho "\tstate = not running"\necho "}"\nexit 0\n')
    assert "not running" in cli(rig, "runner-home", "--repo", str(rig.repo)).stdout

    _script(rig.launchctl, 'exit 1\n')                       # the service manager refused
    out = cli(rig, "runner-home", "--repo", str(rig.repo)).stdout
    assert "not running" not in out and "not loaded" in out

    _script(rig.launchctl, 'echo "com.x = {"\necho "\tstate = some-future-word"\necho "}"\n')
    out = cli(rig, "runner-home", "--repo", str(rig.repo)).stdout
    assert "not running" not in out and "unknown" in out.lower()
