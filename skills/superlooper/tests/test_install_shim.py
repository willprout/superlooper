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


def _run(home, *args):
    env = {**os.environ, "HOME": str(home)}
    env.pop("ZDOTDIR", None)               # so ZSHRC resolves to $HOME/.zshrc
    return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=30)


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
