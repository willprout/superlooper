"""Tests for bin/install.sh — the explicit publish step (plan Task 14).

Runs the REAL install.sh against a FAKE $HOME (via the HOME env override, exactly like
test_install_shim.py) so the real ~/.claude is never touched — the skill's source must NEVER
publish itself into the live ~/.claude during a test (project publishing rule).

What install.sh must do, and what these tests pin:
  * rsync the skill/ payload -> $HOME/.claude/skills/superlooper/ (SKILL.md, lib, bin, references)
  * write VERSION (git SHA + date)
  * idempotently merge TWO hook registrations into $HOME/.claude/settings.json via python stdlib
    (PostToolUse -> activity-hook.sh, Stop -> stop-hook.sh), WITHOUT clobbering existing hooks or
    other settings, and WITHOUT duplicating on re-run
  * install the launch shim (the ~/.zshrc block)
  * --dry-run mutates nothing
  * fail CLOSED on a wrong-typed existing settings.json rather than overwrite it
"""
import json
import os
import subprocess

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
INSTALL = os.path.join(REPO_ROOT, "bin", "install.sh")

SKILL_DIR = (".claude", "skills", "superlooper")
SETTINGS = (".claude", "settings.json")
ACT_SUFFIX = "skills/superlooper/bin/activity-hook.sh"
STOP_SUFFIX = "skills/superlooper/bin/stop-hook.sh"
SHIM_BEGIN = "# >>> superlooper launch shim >>>"


def _run(home, *args):
    env = {**os.environ, "HOME": str(home)}
    env.pop("ZDOTDIR", None)                       # so ~/.zshrc resolves under the fake HOME
    return subprocess.run([INSTALL, *args], env=env, capture_output=True, text=True, timeout=120)


def _skill(home, *parts):
    return home.joinpath(*SKILL_DIR, *parts)


def _settings(home):
    return json.loads(home.joinpath(*SETTINGS).read_text())


def _hook_commands(settings):
    """Flatten every hook command string in a settings dict, across all events/groups."""
    out = []
    for _event, groups in (settings.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


def _event_commands(settings, event):
    """Hook command strings registered under ONE specific event — so a test can pin that a hook
    landed under its REQUIRED event, not merely somewhere in the tree."""
    out = []
    for g in (settings.get("hooks") or {}).get(event, []):
        for h in g.get("hooks", []):
            out.append(h.get("command", ""))
    return out


# --------------------------------------------------------------------------------------------

def test_install_lays_down_payload_version_hooks_and_shim(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home)
    assert r.returncode == 0, r.stderr

    # payload: SKILL.md + a lib module + a bin script + a reference all rsynced in
    assert _skill(home, "SKILL.md").exists(), "SKILL.md must be published"
    assert _skill(home, "lib", "config.py").exists(), "lib payload must be published"
    assert _skill(home, "bin", "activity-hook.sh").exists(), "bin payload must be published"
    assert _skill(home, "references", "issue-writing.md").exists(), "references must be published"

    # VERSION = git SHA + date
    version = _skill(home, "VERSION").read_text().strip()
    assert version, "VERSION must be non-empty"
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", version), f"VERSION must carry a date, got {version!r}"

    # hooks merged into settings.json UNDER THEIR REQUIRED EVENTS (not merely somewhere in the tree):
    # activity-hook -> PostToolUse, stop-hook -> Stop. The event placement is load-bearing.
    s = _settings(home)
    assert any(c.endswith(ACT_SUFFIX) for c in _event_commands(s, "PostToolUse")), \
        "activity hook must be registered under PostToolUse"
    assert any(c.endswith(STOP_SUFFIX) for c in _event_commands(s, "Stop")), \
        "stop hook must be registered under Stop"

    # launch shim installed
    rc = (home / ".zshrc").read_text()
    assert rc.count(SHIM_BEGIN) == 1, "the launch shim block must be installed once"


def test_install_is_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _run(home)
    r2 = _run(home)                                # second run must add nothing new
    assert r2.returncode == 0, r2.stderr

    cmds = _hook_commands(_settings(home))
    assert sum(c.endswith(ACT_SUFFIX) for c in cmds) == 1, "activity hook must not be duplicated"
    assert sum(c.endswith(STOP_SUFFIX) for c in cmds) == 1, "stop hook must not be duplicated"
    assert (home / ".zshrc").read_text().count(SHIM_BEGIN) == 1, "shim block must not be duplicated"


def test_install_preserves_existing_hooks_and_settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    # Pre-existing settings: an unrelated top-level key AND a pre-existing PostToolUse hook that
    # MUST survive the merge (the realistic case: autocode's hooks + suggest-cross-review already
    # registered). A merge that clobbered these would be the fail-open defect this test guards.
    seed = {
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write", "hooks": [
                    {"type": "command", "command": "$HOME/.claude/hooks/suggest-cross-review.sh"}]},
            ],
        },
    }
    home.joinpath(*SETTINGS).write_text(json.dumps(seed))
    r = _run(home)
    assert r.returncode == 0, r.stderr

    s = _settings(home)
    assert s.get("theme") == "dark", "unrelated settings keys must survive the merge"
    cmds = _hook_commands(s)
    assert any(c.endswith("suggest-cross-review.sh") for c in cmds), "existing hook must survive"
    assert any(c.endswith(ACT_SUFFIX) for c in cmds), "superlooper activity hook must be added"
    assert any(c.endswith(STOP_SUFFIX) for c in cmds), "superlooper stop hook must be added"


def test_dry_run_mutates_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(home, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert not _skill(home).exists(), "--dry-run must not write the payload"
    assert not home.joinpath(*SETTINGS).exists(), "--dry-run must not write settings.json"
    zshrc = home / ".zshrc"
    assert not zshrc.exists() or SHIM_BEGIN not in zshrc.read_text(), "--dry-run must not touch ~/.zshrc"


def test_install_fails_closed_on_wrongtyped_settings(tmp_path):
    """A malformed settings.json — at the TOP LEVEL or NESTED — must make install FAIL rather than
    silently overwrite the user's file (the fail-OPEN-on-wrong-typed-input defect class). It must
    also not have published the payload (the merge runs before rsync, so a refusal aborts cleanly).
    Missing keys are tolerated elsewhere; only WRONG types fail closed."""
    malformed = [
        '{"hooks": ["this is not an object"]}',                          # hooks not an object
        '{"hooks": {"PostToolUse": "not a list"}}',                      # event value not a list
        '{"hooks": {"PostToolUse": ["not an object"]}}',                 # group not an object
        '{"hooks": {"PostToolUse": [{"hooks": "not a list"}]}}',         # group.hooks not a list
        '{"hooks": {"PostToolUse": [{"hooks": ["not an object"]}]}}',    # hook entry not an object
        '{"hooks": {"PostToolUse": [{"hooks": [{"command": 123}]}]}}',   # command not a string
    ]
    for i, blob in enumerate(malformed):
        home = tmp_path / f"home{i}"
        home.mkdir()
        (home / ".claude").mkdir()
        home.joinpath(*SETTINGS).write_text(blob)
        r = _run(home)
        assert r.returncode != 0, f"install must fail closed on malformed settings: {blob}"
        assert home.joinpath(*SETTINGS).read_text() == blob, \
            f"a fail-closed install must not modify the malformed settings.json: {blob}"
        assert not _skill(home).exists(), \
            f"a fail-closed install must not publish the payload: {blob}"
