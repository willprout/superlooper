"""Unit tests for shell/launch-shim.zsh — the keystroke-free launch mechanism (RC6 fix).

The shim is sourced by ~/.zshrc in every cmux tab's shell. In the ONE tab superlooper just created
it must self-run the dropped worker command (no `cmux send` keystrokes, so it survives display sleep
/ lock / app-backgrounding). In EVERY other shell it must be a fast no-op so manually-opened
terminals are never delayed. These run the real shim under zsh with planted files + a controlled
environment.

Ported verbatim from autocode's test_launch_shim.py; only the env prefix changed
(AUTOCODE_LAUNCH_DIR -> SL_LAUNCH_DIR, AUTOCODE_SHIM_WAIT_TICKS -> SL_SHIM_WAIT_TICKS) so the
superlooper shim coexists with autocode's under a different marker dir (plan §B.5).

Note: the display-sleep SURVIVAL itself can only be proven by a live test with the screen off (the
shell runs as a normal child process of cmux, independent of the display) — that is the manual
acceptance test. These cover everything else: the file/claim/wait/gate logic the shim must get right.
"""
import os
import shutil
import subprocess
import threading
import time

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SHIM = os.path.join(REPO_ROOT, "skill", "shell", "launch-shim.zsh")

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh required for the launch shim")


def _run(launch_dir, surface_id=None, extra_env=None, source_times=1, timeout=20):
    """Source the shim in a fresh zsh with a controlled env; return (CompletedProcess, elapsed_s)."""
    env = {"PATH": os.environ["PATH"], "SL_LAUNCH_DIR": str(launch_dir)}
    if surface_id is not None:
        env["CMUX_SURFACE_ID"] = surface_id
    if extra_env:
        env.update(extra_env)
    script = "; ".join([f"source '{SHIM}'"] * source_times)
    t0 = time.monotonic()
    r = subprocess.run(["zsh", "-c", script], env=env, capture_output=True, text=True, timeout=timeout)
    return r, time.monotonic() - t0


def _cmd_file(launch_dir, surface_id, marker):
    """Plant a per-surface command file whose command touches `marker` (proves the shim ran it)."""
    (launch_dir / f"{surface_id}.cmd").write_text(f"touch '{marker}'")


def test_runs_command_for_this_surface(tmp_path):
    d = tmp_path / "launch"; d.mkdir()
    marker = tmp_path / "ran.S1"
    _cmd_file(d, "S1", marker)
    r, _ = _run(d, surface_id="S1")
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "shim must run the command file for its own surface"
    assert not (d / "S1.cmd").exists(), "the command file must be claimed (consumed), not left behind"


def test_noop_outside_cmux(tmp_path):
    # No CMUX_SURFACE_ID => not a cmux tab => must do nothing (and not hang).
    d = tmp_path / "launch"; d.mkdir()
    marker = tmp_path / "ran"
    (d / ".cmd").write_text(f"touch '{marker}'")   # a stray file; must be ignored
    r, elapsed = _run(d, surface_id=None)
    assert r.returncode == 0
    assert not marker.exists()
    assert elapsed < 3, "a non-cmux shell must return immediately"


def test_noop_when_idle_no_active(tmp_path):
    # A normal cmux terminal opened when NO launch is in flight: no cmd file, no .active marker.
    # Must return instantly (never delay a hand-opened terminal).
    d = tmp_path / "launch"; d.mkdir()
    r, elapsed = _run(d, surface_id="S2")
    assert r.returncode == 0
    assert elapsed < 3, "no command + no active marker must NOT wait"


def test_stale_active_does_not_wait(tmp_path):
    # A stale .active (an old launch long finished) must not make a fresh terminal wait.
    d = tmp_path / "launch"; d.mkdir()
    active = d / ".active"; active.write_text("")
    old = time.time() - 600
    os.utime(active, (old, old))
    r, elapsed = _run(d, surface_id="S3", extra_env={"SL_SHIM_WAIT_TICKS": "50"})
    assert r.returncode == 0
    assert elapsed < 3, "a stale active marker must be ignored (no wait)"


def test_fresh_active_waits_then_gives_up(tmp_path):
    # A launch IS in flight (fresh .active) but no command for THIS surface ever arrives: the shim
    # waits the bounded window, then returns. Proves the wait gate engages (and is bounded).
    d = tmp_path / "launch"; d.mkdir()
    (d / ".active").write_text("")
    r, elapsed = _run(d, surface_id="S4", extra_env={"SL_SHIM_WAIT_TICKS": "5"})  # 5*0.2s = 1s
    assert r.returncode == 0
    assert elapsed >= 0.7, "with a fresh active marker the shim must wait for its command file"
    assert elapsed < 6, "the wait must be bounded, not indefinite"


def test_waits_for_late_command_then_runs_it(tmp_path):
    # THE boot-race fix: the shell reaches the shim BEFORE launch-session writes the command file.
    # With a fresh .active marker the shim waits, and when the file appears it runs it.
    d = tmp_path / "launch"; d.mkdir()
    (d / ".active").write_text("")
    marker = tmp_path / "ran.S5"

    def _late_write():
        time.sleep(0.6)
        _cmd_file(d, "S5", marker)

    t = threading.Thread(target=_late_write); t.start()
    try:
        r, elapsed = _run(d, surface_id="S5", extra_env={"SL_SHIM_WAIT_TICKS": "50"})
    finally:
        t.join()
    assert r.returncode == 0, r.stderr
    assert marker.exists(), "shim must wait for a late-arriving command file and run it"


def test_ignores_other_surfaces_command(tmp_path):
    # The shim must run ONLY its own surface's command, never another tab's.
    d = tmp_path / "launch"; d.mkdir()
    marker = tmp_path / "ran.other"
    _cmd_file(d, "OTHER", marker)
    r, elapsed = _run(d, surface_id="S6")
    assert r.returncode == 0
    assert not marker.exists(), "must not run a different surface's command"
    assert (d / "OTHER.cmd").exists(), "another surface's command file must be left untouched"
    assert elapsed < 3


def test_claim_is_idempotent_across_double_source(tmp_path):
    # Sourcing ~/.zshrc twice (or a re-source) must not double-run the command.
    d = tmp_path / "launch"; d.mkdir()
    count = tmp_path / "count"
    (d / "S7.cmd").write_text(f"printf x >> '{count}'")
    r, _ = _run(d, surface_id="S7", source_times=2)
    assert r.returncode == 0, r.stderr
    assert count.read_text() == "x", "the command must run exactly once even if the shim is sourced twice"
