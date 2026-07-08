"""Task 12 — ``bin/install-launchd.sh`` end-to-end (the install tool a stranger runs).

The pure render is pinned in ``test_launchd.py``; this drives the actual shell installer once,
proving the part the shell owns: it resolves the ABSOLUTE ``bin/command-center`` + config paths a
LaunchAgent needs (a relative path would break under launchd), writes a parseable plist to the
LaunchAgents dir, and prints the ``launchctl load`` next step. The install DIR is redirected with
``CC_LAUNCHD_DIR`` and ``HOME`` into a tmp sandbox, and ``--load`` is never passed — so the test
places a file and touches ``launchctl`` never, leaving the real machine's LaunchAgents untouched
(this suite shares William's Mac — no test may reach a real system binary).
"""
import os
import plistlib
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_INSTALLER = _ROOT / "bin" / "install-launchd.sh"
_REAL_BIN = _ROOT / "bin" / "command-center"


def _run(args, *, home, launchd_dir):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CC_LAUNCHD_DIR"] = str(launchd_dir)
    return subprocess.run(["bash", str(_INSTALLER), *args],
                          capture_output=True, text=True, env=env)


def test_installer_writes_a_parseable_keepalive_plist_with_absolute_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    launchd_dir = tmp_path / "LaunchAgents"
    config = tmp_path / "config.json"
    config.write_text('{"repos": [{"path": "~/code/x"}]}')   # existence is all the shell checks

    proc = _run([str(config)], home=home, launchd_dir=launchd_dir)
    assert proc.returncode == 0, proc.stderr

    plist = launchd_dir / "com.command-center.plist"
    assert plist.exists(), "installer must write the LaunchAgent plist\n" + proc.stdout + proc.stderr
    doc = plistlib.loads(plist.read_bytes())

    # Absolute paths, or launchd cannot find them: the real bin, and the config resolved absolute.
    assert doc["ProgramArguments"][0] == str(_REAL_BIN.resolve())
    assert doc["ProgramArguments"][1] == str(config.resolve())
    assert doc["KeepAlive"] is True
    # The log lands under the (sandboxed) HOME, and its dir was created so launchd can write it.
    assert doc["StandardOutPath"].startswith(str(home))
    assert Path(doc["StandardOutPath"]).parent.is_dir()


def test_installer_prints_the_launchctl_load_next_step(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    launchd_dir = tmp_path / "LaunchAgents"
    config = tmp_path / "config.json"; config.write_text("{}")
    proc = _run([str(config)], home=home, launchd_dir=launchd_dir)
    assert proc.returncode == 0, proc.stderr
    assert "launchctl load" in proc.stdout      # the stranger gets the one command to activate it


def test_installer_fails_loud_when_the_config_is_missing(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    launchd_dir = tmp_path / "LaunchAgents"
    proc = _run([str(tmp_path / "does-not-exist.json")], home=home, launchd_dir=launchd_dir)
    assert proc.returncode != 0
    assert "config" in (proc.stderr + proc.stdout).lower()
    assert not (launchd_dir / "com.command-center.plist").exists()   # nothing written on error
