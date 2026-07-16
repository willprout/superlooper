"""Issue #156 — the gated root installer registers the PreToolUse deny hook.

The hook is inert until it is BOTH published (bin/pretooluse-hook.sh copied into the installed
skill) AND registered under the PreToolUse event in ~/.claude/settings.json. It is CLAUDE ONLY:
Codex has no PreToolUse event (spike verdict), so it must NOT appear in the Codex hooks.json.

Runs the gated root installer against a FAKE $HOME so the real machine is never touched, with
SL_DEFAULTS neutralized (the launch-shim step would otherwise write real macOS user defaults).
"""
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
ROOT_INSTALL = os.path.abspath(os.path.join(REPO_ROOT, "..", "..", "bin", "install.sh"))

PRE_HOOK_CMD = "$HOME/.claude/skills/superlooper/bin/pretooluse-hook.sh"


def _commands(hooks, event):
    return [h.get("command") for g in hooks.get(event, []) for h in g.get("hooks", [])]


def _install(home, *args):
    env = {**os.environ, "HOME": str(home), "SL_DEFAULTS": "/usr/bin/true"}
    env.pop("ZDOTDIR", None)
    return subprocess.run(["bash", ROOT_INSTALL, *args], env=env,
                          capture_output=True, text=True, timeout=120)


def test_installer_registers_pretooluse_deny_for_claude_only(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    r = _install(home, "--yes")
    assert r.returncode == 0, r.stderr or r.stdout

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    hooks = settings.get("hooks", {})
    assert _commands(hooks, "PreToolUse") == [PRE_HOOK_CMD], \
        "PreToolUse deny must be registered in Claude settings.json"
    # The existing two hooks must still be there — the new target is additive.
    assert any("activity-hook.sh" in c for c in _commands(hooks, "PostToolUse"))
    assert any("stop-hook.sh" in c for c in _commands(hooks, "Stop"))

    # Claude-only: the PreToolUse deny must NOT be in the Codex hooks.json.
    codex = json.loads((home / ".codex" / "hooks.json").read_text())
    assert "PreToolUse" not in codex.get("hooks", {}), \
        "Codex has no PreToolUse event — the deny must not be registered there"

    # And the hook script itself must be published into the installed skill, executable.
    published = home / ".claude" / "skills" / "superlooper" / "bin" / "pretooluse-hook.sh"
    assert published.exists() and os.access(published, os.X_OK), "hook script must be published +x"


def test_installer_registration_is_idempotent(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    assert _install(home, "--yes").returncode == 0
    assert _install(home, "--yes").returncode == 0            # a second publish must not duplicate
    hooks = json.loads((home / ".claude" / "settings.json").read_text()).get("hooks", {})
    assert _commands(hooks, "PreToolUse") == [PRE_HOOK_CMD], \
        "re-running the installer must not duplicate the PreToolUse hook"


def test_dry_run_reports_pretooluse_for_claude_but_not_codex(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    r = _install(home, "--dry-run")
    assert r.returncode == 0, r.stderr
    # The Claude settings block lists PreToolUse; the Codex block (which follows) does not.
    claude_block, _, codex_block = r.stdout.partition("codex hooks")
    assert "PreToolUse" in claude_block, "dry-run must show the PreToolUse registration for Claude"
    assert "PreToolUse" not in codex_block, "dry-run must NOT register PreToolUse for Codex"
    assert not (home / ".claude" / "settings.json").exists(), "dry-run must write nothing"
