"""Unit tests for shell/launch-shim.zsh — the in-pane handoff that keeps the launch floor ours.

The shim is sourced by ~/.zshrc in every shell, including the login shell the session host spawns
in each new pane. Its whole job (issue #308) is to arm a one-shot function named after the agent
verb, so that when the host TYPES `claude` into the pane it runs start-session.sh — the floor, the
brief and the real agent — instead of a bare binary with none of that.

In every OTHER shell it must be an instant no-op: a hand-opened terminal, an unrelated pane, and
the operator's own herdr window are untouched. These run the real shim under zsh with a controlled
environment and then ask the shell what it is actually holding.

The shim has a SECOND, independent half, covered at the bottom of this file: the cmux command-file
mechanism, which now has exactly one writer left — bin/resurrect-runner.sh, the watchdog's restart
of a provably-gone runner. Sessions no longer need it (the host starts the agent itself, so there
is no command to drop and no boot race to lose), but the runner's own home is issue #306's and the
cmux retirement is #311's, so that half stays and is tested as what it now is.
"""
import os
import shutil
import subprocess
import time

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SHIM = os.path.join(REPO_ROOT, "skill", "shell", "launch-shim.zsh")

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="zsh required for the launch shim")


def _start_session(where, marker, executable=True):
    """A stand-in for start-session.sh that records the id it was handed."""
    path = where / "start-session.sh"
    path.write_text("#!/bin/sh\nprintf '%s' \"$1\" > '" + str(marker) + "'\n")
    path.chmod(0o755 if executable else 0o644)
    return path


def _run(script, env=None, timeout=20):
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    base.update(env or {})
    t0 = time.monotonic()
    r = subprocess.run(["zsh", "-c", script], env=base, capture_output=True, text=True,
                       timeout=timeout)
    return r, time.monotonic() - t0


def _pane_env(home, start_session, iid="i308", agent="claude"):
    return {"HERDR_PANE_ID": "w2:p1", "SL_ISSUE_ID": iid,
            "SL_RUN_ROOT": str(home), "SL_START_SESSION": str(start_session),
            "SL_AGENT": agent}


# --------------------------------------------------------------------------- the handoff

def test_the_typed_agent_verb_runs_start_session(tmp_path):
    """The whole point: the host types `claude`, and start-session.sh runs with the lane id."""
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    r, _ = _run(f"source '{SHIM}'; claude", env=_pane_env(tmp_path, start))
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "i308"


def test_the_codex_verb_is_armed_for_a_codex_repo(tmp_path):
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    r, _ = _run(f"source '{SHIM}'; codex",
                env=_pane_env(tmp_path, start, iid="i42", agent="codex"))
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "i42"


def test_no_agent_flag_is_composed_by_the_shim(tmp_path):
    """The agent boundary: start-session.sh owns every claude/codex flag. The shim hands it the id
    and nothing else, so the agent command line keeps exactly one author."""
    log = tmp_path / "argv"
    start = tmp_path / "start-session.sh"
    start.write_text("#!/bin/sh\nprintf '%s' \"$*\" > '" + str(log) + "'\n")
    start.chmod(0o755)
    r, _ = _run(f"source '{SHIM}'; claude --model opus 'a brief'",
                env=_pane_env(tmp_path, start))
    assert r.returncode == 0, r.stderr
    assert log.read_text() == "i308", "whatever the host typed, only the lane id is handed over"


def test_the_handoff_is_one_shot(tmp_path):
    """A second `claude` in that pane is the REAL binary, never a second worker for the lane."""
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    r, _ = _run(f"source '{SHIM}'; claude; whence -w claude || echo none",
                env=_pane_env(tmp_path, start))
    assert r.returncode == 0, r.stderr
    assert "claude: function" not in r.stdout


def test_a_state_home_with_spaces_survives_the_arming(tmp_path):
    """The body is ${(q)}-quoted, so a path with a space can neither break nor inject it."""
    odd = tmp_path / "a dir with spaces"
    odd.mkdir()
    marker = odd / "handed"
    start = _start_session(odd, marker)
    r, _ = _run(f"source '{SHIM}'; claude", env=_pane_env(odd, start))
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "i308"


# --------------------------------------------------------------------------- the no-op cases

def test_noop_in_an_ordinary_shell(tmp_path):
    """A terminal the owner opened — and the owner's own hosted window, which looks identical from
    in here — carries none of this loop's SL_*, so nothing is armed and nothing is delayed."""
    r, elapsed = _run(f"source '{SHIM}'; whence -w claude || echo none", env={})
    assert r.returncode == 0, r.stderr
    assert "claude: function" not in r.stdout
    assert elapsed < 3, "the shim must never delay an ordinary shell"


def test_noop_in_a_nested_shell_inside_a_live_session(tmp_path):
    """THE disarm (issue #308). A worker's own environment keeps SL_ISSUE_ID and SL_RUN_ROOT —
    hooks and the agent's children read them — so those two alone must never arm the handoff.
    start-session.sh unsets SL_START_SESSION for exactly this reason: a worker typing `zsh` and
    then `claude` must get the binary it asked for, not a second worker in its own lane."""
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    env = _pane_env(tmp_path, start)
    env.pop("SL_START_SESSION")
    r, _ = _run(f"source '{SHIM}'; whence -w claude || echo none", env=env)
    assert r.returncode == 0, r.stderr
    assert "claude: function" not in r.stdout
    assert not marker.exists()


def test_start_session_disarms_the_handoff_for_its_own_children(tmp_path):
    """The other half of the pair, asserted against the real start-session.sh: whatever the pane
    handed it, its children must not carry the arming variable."""
    engine = os.path.join(REPO_ROOT, "skill", "bin", "start-session.sh")
    with open(engine) as f:
        source = f.read()
    assert "unset SL_START_SESSION" in source, \
        "start-session.sh must disarm the handoff before it launches the agent"
    assert source.index("unset SL_START_SESSION") < source.index("acquire_worker"), \
        "the disarm runs before anything that can exit early"


def test_noop_for_an_agent_this_stack_cannot_launch(tmp_path):
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    r, _ = _run(f"source '{SHIM}'; whence -w claude || echo none",
                env=_pane_env(tmp_path, start, agent="parrot"))
    assert r.returncode == 0, r.stderr
    assert "claude: function" not in r.stdout


def test_an_unrunnable_handoff_says_so_and_arms_nothing(tmp_path):
    """LOUD, and deliberately unarmed. A bare agent started with no floor and no brief would look
    alive without being this loop's session; leaving the verb unarmed means no start sentinel is
    stamped, so the launcher refuses the delivery instead of recording a live lane."""
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker, executable=False)
    r, _ = _run(f"source '{SHIM}'; whence -w claude || echo none",
                env=_pane_env(tmp_path, start))
    assert r.returncode == 0, r.stderr
    assert "claude: function" not in r.stdout
    assert "launch handoff unavailable" in r.stderr


def test_re_sourcing_is_idempotent(tmp_path):
    """~/.zshrc can be sourced more than once; arming twice must still hand off exactly once."""
    count = tmp_path / "count"
    start = tmp_path / "start-session.sh"
    start.write_text("#!/bin/sh\nprintf x >> '" + str(count) + "'\n")
    start.chmod(0o755)
    r, _ = _run(f"source '{SHIM}'; source '{SHIM}'; claude; claude 2>/dev/null || true",
                env=_pane_env(tmp_path, start))
    assert r.returncode == 0, r.stderr
    assert count.read_text() == "x", "the handoff runs exactly once, however often it is armed"


# --------------------------------------------------------------------------- the runner's own half

def test_the_command_file_half_still_serves_the_runner_resurrection(tmp_path):
    """bin/resurrect-runner.sh is the ONE remaining writer of a dropped command file (issue #208):
    it restarts a provably-gone runner in its recorded cmux pane, which is not a session launch and
    not this issue's to retire. It goes when cmux does."""
    d = tmp_path / "launch"
    d.mkdir()
    marker = tmp_path / "ran.S1"
    (d / "S1.cmd").write_text(f"touch '{marker}'")
    r, _ = _run(f"source '{SHIM}'",
                env={"SL_LAUNCH_DIR": str(d), "CMUX_SURFACE_ID": "S1"})
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "the shim must run the command file for its own surface"
    assert not (d / "S1.cmd").exists(), "the command file is claimed, not left behind"


def test_the_command_half_ignores_another_surfaces_command(tmp_path):
    d = tmp_path / "launch"
    d.mkdir()
    marker = tmp_path / "ran.other"
    (d / "OTHER.cmd").write_text(f"touch '{marker}'")
    r, elapsed = _run(f"source '{SHIM}'",
                      env={"SL_LAUNCH_DIR": str(d), "CMUX_SURFACE_ID": "S6"})
    assert r.returncode == 0
    assert not marker.exists()
    assert (d / "OTHER.cmd").exists(), "another surface's command is left untouched"
    assert elapsed < 3


def test_the_command_half_claims_exactly_once_across_a_double_source(tmp_path):
    d = tmp_path / "launch"
    d.mkdir()
    count = tmp_path / "count"
    (d / "S7.cmd").write_text(f"printf x >> '{count}'")
    r, _ = _run(f"source '{SHIM}'; source '{SHIM}'",
                env={"SL_LAUNCH_DIR": str(d), "CMUX_SURFACE_ID": "S7"})
    assert r.returncode == 0, r.stderr
    assert count.read_text() == "x"


def test_a_stale_active_marker_never_delays_a_hand_opened_terminal(tmp_path):
    d = tmp_path / "launch"
    d.mkdir()
    active = d / ".active"
    active.write_text("")
    old = time.time() - 600
    os.utime(active, (old, old))
    r, elapsed = _run(f"source '{SHIM}'",
                      env={"SL_LAUNCH_DIR": str(d), "CMUX_SURFACE_ID": "S3",
                           "SL_SHIM_WAIT_TICKS": "50"})
    assert r.returncode == 0
    assert elapsed < 3, "a stale active marker must be ignored"


def test_the_two_halves_are_independent(tmp_path):
    """A session pane carries no CMUX_SURFACE_ID and a resurrection tab carries none of the SL_*
    triple, so neither half can ever fire on the other's case."""
    marker = tmp_path / "handed"
    start = _start_session(tmp_path, marker)
    d = tmp_path / "launch"
    d.mkdir()
    ran = tmp_path / "ran"
    (d / "S1.cmd").write_text(f"touch '{ran}'")
    env = _pane_env(tmp_path, start)
    env["SL_LAUNCH_DIR"] = str(d)
    r, _ = _run(f"source '{SHIM}'; claude", env=env)          # a session pane: no surface id
    assert r.returncode == 0, r.stderr
    assert marker.read_text() == "i308"
    assert not ran.exists(), "the session pane must not claim a resurrection command"
