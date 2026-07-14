"""Tests for bin/install-launch-shim.sh — idempotent, reversible install of the launch shim.

Runs against a FAKE $HOME so the real ~/.zshrc is never touched.

Ported from autocode's test_install_shim.py; the marker dir moved ~/.autocode -> ~/.superlooper and
the ~/.zshrc guard block markers changed to "superlooper" so both shims coexist (plan §B.5).
"""
import os
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
INSTALL = os.path.join(REPO_ROOT, "skill", "bin", "install-launch-shim.sh")
BEGIN = "# >>> superlooper launch shim >>>"


def _run(home, *args, env_extra=None):
    env = {**os.environ, "HOME": str(home)}
    env.pop("ZDOTDIR", None)               # so ZSHRC resolves to $HOME/.zshrc
    # Neutralize the real `defaults` binary UNCONDITIONALLY so no test ever writes real macOS user
    # defaults (com.cmuxterm.app) — even if SL_DEFAULTS is exported ambiently. Individual tests that
    # need to observe the call inject a recording/failing stub via env_extra (which wins, below).
    env["SL_DEFAULTS"] = "/usr/bin/true"
    if env_extra:
        env.update(env_extra)
    return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=30)


def _defaults_stub(tmp_path, log, exit_code=0):
    """A stand-in `defaults` binary the installer will invoke instead of the real one: it appends
    its space-joined args to `log` and exits `exit_code`, so a test can prove what was requested
    without touching real macOS user defaults."""
    stub = tmp_path / "defaults-stub.sh"
    stub.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit %d\n' % (log, exit_code))
    stub.chmod(0o755)
    return stub


def test_install_creates_shim_launch_dir_and_sources_it(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (home / ".zshrc").write_text("export FOO=1\n")          # pre-existing content must survive
    r = _run(home)
    assert r.returncode == 0, r.stderr
    assert (home / ".superlooper" / "launch-shim.zsh").exists(), "shim must be installed"
    ld = home / ".superlooper" / "launch"
    assert ld.is_dir(), "launch dir must exist"
    assert oct(ld.stat().st_mode & 0o777) == "0o700", "launch dir must be mode 700"
    rc = (home / ".zshrc").read_text()
    assert "export FOO=1" in rc, "pre-existing ~/.zshrc content must be preserved"
    assert rc.count(BEGIN) == 1, "exactly one shim block must be added"
    assert "source \"$HOME/.superlooper/launch-shim.zsh\"" in rc


def test_install_is_idempotent(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    _run(home)
    _run(home)                                              # second run must not duplicate the block
    rc = (home / ".zshrc").read_text()
    assert rc.count(BEGIN) == 1, "re-running the installer must not duplicate the block"


def test_install_disables_app_nap_for_cmux(tmp_path):
    # Nap-proofing (issue #120): the installer also sets NSAppSleepDisabled on the cmux bundle so
    # an idle/occluded cmux is never App-Napped into deferring worker-tab shell spawns.
    home = tmp_path / "home"; home.mkdir()
    log = tmp_path / "defaults.log"
    stub = _defaults_stub(tmp_path, log, exit_code=0)
    r = _run(home, env_extra={"SL_DEFAULTS": str(stub)})
    assert r.returncode == 0, r.stderr
    assert log.exists(), "the installer must call `defaults` to disable App Nap"
    calls = log.read_text()
    assert "write com.cmuxterm.app NSAppSleepDisabled -bool true" in calls
    # It must tell the operator the setting only takes effect after cmux is relaunched.
    out = r.stdout + r.stderr
    assert "NSAppSleepDisabled" in out
    assert any(w in out.lower() for w in ("relaunch", "restart", "quit"))


def test_install_survives_a_failing_defaults_write(tmp_path):
    # A defaults-write failure must NEVER abort the shim install (the critical part); it degrades to
    # a WARN naming the exact manual command.
    home = tmp_path / "home"; home.mkdir()
    log = tmp_path / "defaults.log"
    stub = _defaults_stub(tmp_path, log, exit_code=1)
    r = _run(home, env_extra={"SL_DEFAULTS": str(stub)})
    assert r.returncode == 0, r.stderr
    assert (home / ".superlooper" / "launch-shim.zsh").exists(), "shim install must still succeed"
    out = r.stdout + r.stderr
    assert "defaults write com.cmuxterm.app NSAppSleepDisabled -bool true" in out


def test_uninstall_removes_block_and_shim_but_keeps_user_content(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    (home / ".zshrc").write_text("alias ll='ls -la'\n")
    _run(home)
    r = _run(home, "--uninstall")
    assert r.returncode == 0, r.stderr
    rc = (home / ".zshrc").read_text()
    assert BEGIN not in rc, "uninstall must remove the shim block"
    assert "alias ll='ls -la'" in rc, "uninstall must keep the user's own ~/.zshrc content"
    assert not (home / ".superlooper" / "launch-shim.zsh").exists(), "uninstall must remove the shim file"
