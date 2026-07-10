"""Tests for the nested install.sh tombstone.

The monorepo root ``bin/install.sh`` is the gated, canonical publish path. The nested
``skills/superlooper/bin/install.sh`` remains only as an explicit redirect for old habits and
scripts; it must not publish, merge hooks, stamp VERSION, or install the launch shim.
"""
import os
import subprocess

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
INSTALL = os.path.join(REPO_ROOT, "bin", "install.sh")

SKILL_DIR = (".claude", "skills", "superlooper")
SETTINGS = (".claude", "settings.json")
CODEX_HOOKS = (".codex", "hooks.json")
SHIM_BEGIN = "# >>> superlooper launch shim >>>"


def _run(home, *args):
    poison = home / "poison-bin"
    poison.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    env["PATH"] = str(poison)
    env.pop("ZDOTDIR", None)
    return subprocess.run([INSTALL, *args], env=env,
                          capture_output=True, text=True, timeout=30)


def _skill(home, *parts):
    return home.joinpath(*SKILL_DIR, *parts)


def _combined_output(proc):
    return proc.stdout + proc.stderr


def _assert_redirect(proc):
    assert proc.returncode != 0, "nested installer must refuse to publish"
    out = _combined_output(proc)
    assert "canonical" in out.lower(), out
    assert "bin/install.sh" in out, out
    assert "gated" in out.lower(), out


def test_nested_installer_refuses_and_points_to_gated_root_installer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    proc = _run(home)

    _assert_redirect(proc)
    assert not _skill(home).exists(), "nested installer must not publish the payload"
    assert not home.joinpath(*SETTINGS).exists(), "nested installer must not write settings.json"
    assert not home.joinpath(*CODEX_HOOKS).exists(), "nested installer must not write Codex hooks.json"
    zshrc = home / ".zshrc"
    assert not zshrc.exists() or SHIM_BEGIN not in zshrc.read_text(), \
        "nested installer must not install the launch shim"


def test_nested_installer_refuses_even_dry_run_and_help(tmp_path):
    for arg in ("--dry-run", "--help"):
        home = tmp_path / arg.lstrip("-")
        home.mkdir()
        proc = _run(home, arg)

        _assert_redirect(proc)
        assert not _skill(home).exists(), f"{arg} must not publish the payload"


def test_nested_installer_preserves_existing_target_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    skill = _skill(home)
    skill.mkdir(parents=True)
    version = skill / "VERSION"
    version.write_text("before\n")
    settings = home.joinpath(*SETTINGS)
    settings.write_text('{"theme":"dark"}\n')
    codex_hooks = home.joinpath(*CODEX_HOOKS)
    codex_hooks.parent.mkdir(parents=True)
    codex_hooks.write_text('{"hooks":{}}\n')
    zshrc = home / ".zshrc"
    zshrc.write_text("export KEEP=1\n")

    proc = _run(home)

    _assert_redirect(proc)
    assert version.read_text() == "before\n"
    assert settings.read_text() == '{"theme":"dark"}\n'
    assert codex_hooks.read_text() == '{"hooks":{}}\n'
    assert zshrc.read_text() == "export KEEP=1\n"
