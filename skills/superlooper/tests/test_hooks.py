import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
ACTIVITY_HOOK = os.path.join(REPO_ROOT, "skill", "bin", "activity-hook.sh")
STOP_HOOK = os.path.join(REPO_ROOT, "skill", "bin", "stop-hook.sh")


def _run_hook(script, run_root, stdin):
    env = {
        **os.environ,
        "SL_AGENT": "codex",
        "SL_ISSUE_ID": "i7",
        "SL_RUN_ROOT": str(run_root),
    }
    return subprocess.run(["bash", script], input=stdin, env=env,
                          capture_output=True, text=True, timeout=10)


def _activity_file(run_root):
    return run_root / "state" / "activity" / "i7"


def test_codex_post_tool_use_fixture_updates_activity(tmp_path):
    run_root = tmp_path / "run"
    fixture = {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/worktree",
        "hook_event_name": "PostToolUse",
        "model": "gpt-5.5",
        "permission_mode": "bypassPermissions",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "tool_response": {"exit_code": 0, "output": ""},
    }
    r = _run_hook(ACTIVITY_HOOK, run_root, json.dumps(fixture))
    assert r.returncode == 0, r.stderr
    assert _activity_file(run_root).exists(), "PostToolUse must stamp the liveness file"


def test_codex_large_post_tool_use_fixture_updates_activity(tmp_path):
    run_root = tmp_path / "run"
    fixture = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "yes | head -c 2000000"},
        "tool_response": {"exit_code": 0, "output": "x" * 2_000_000},
    }
    r = _run_hook(ACTIVITY_HOOK, run_root, json.dumps(fixture))
    assert r.returncode == 0, r.stderr
    assert _activity_file(run_root).exists(), "large PostToolUse payloads must still stamp liveness"


def test_codex_stop_fixture_updates_activity_rest_state(tmp_path):
    run_root = tmp_path / "run"
    fixture = {
        "hook_event_name": "Stop",
        "last_assistant_message": "Done.",
        "stop_hook_active": False,
    }
    r = _run_hook(STOP_HOOK, run_root, json.dumps(fixture))
    assert r.returncode == 0, r.stderr
    assert _activity_file(run_root).exists(), "Stop must stamp the same rest/liveness file"


def test_codex_hook_malformed_or_empty_json_noops(tmp_path):
    cases = [
        (ACTIVITY_HOOK, ""),
        (ACTIVITY_HOOK, "{"),
        (ACTIVITY_HOOK, json.dumps({"hook_event_name": "Stop"})),
        (STOP_HOOK, ""),
        (STOP_HOOK, "{"),
        (STOP_HOOK, json.dumps({"hook_event_name": "PostToolUse"})),
    ]
    for i, (script, blob) in enumerate(cases):
        run_root = tmp_path / f"run{i}"
        r = _run_hook(script, run_root, blob)
        assert r.returncode == 0, "malformed Codex hook input must fail closed without surfacing"
        assert not _activity_file(run_root).exists(), "bad Codex input must not stamp activity"


# ------------------- issue #149: the D14 pruned-cwd mechanism -------------------

def _run_hook_from(cwd, script, run_root, stdin, agent="claude"):
    """Run a hook with an EXPLICIT cwd — the shape the agent CLI itself uses to spawn hooks."""
    env = {**os.environ, "SL_AGENT": agent, "SL_ISSUE_ID": "i7", "SL_RUN_ROOT": str(run_root)}
    return subprocess.run(["bash", script], input=stdin, env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=10)


def test_an_explicit_cwd_spawn_into_a_pruned_worktree_dies_before_the_hook_runs(tmp_path):
    """The D14 root cause, pinned. The agent CLI spawns its hooks with an EXPLICIT cwd (the
    worker's worktree). Prune that worktree under a live CLI and the spawn ITSELF fails with
    ENOENT — `posix_spawn '/bin/sh'` in the forensics — so the hook never runs and the
    liveness/exit stamp never lands, exactly as the lane finishes.

    This is why the hook scripts CANNOT defend themselves here, and why the real fix is ordering
    in the runner (never prune under a live CLI — see test_runner.py's teardown tests). This test
    exists to keep that reasoning honest: if it ever stops raising, the ordering rule could be
    revisited."""
    worktree = tmp_path / "worktrees" / "i7"
    worktree.mkdir(parents=True)
    run_root = tmp_path / "run"
    shutil.rmtree(worktree)                       # the runner prunes it under the live CLI
    with pytest.raises(FileNotFoundError):
        _run_hook_from(worktree, ACTIVITY_HOOK, run_root, "")
    assert not (run_root / "state" / "activity" / "i7").exists()


def test_hooks_stamp_liveness_from_a_safe_cwd_even_with_the_worktree_gone(tmp_path):
    """The other half of the spike: the same prune, but the hook spawned from a safe,
    always-present cwd runs fine and still stamps. Nothing in a hook may depend on the worker's
    worktree being on disk — every path it touches is absolute."""
    worktree = tmp_path / "worktrees" / "i7"
    worktree.mkdir(parents=True)
    run_root = tmp_path / "run"
    shutil.rmtree(worktree)
    for script in (ACTIVITY_HOOK, STOP_HOOK):
        r = _run_hook_from(tmp_path, script, run_root, "")
        assert r.returncode == 0, r.stderr
        assert (run_root / "state" / "activity" / "i7").exists()


def test_hooks_survive_a_cwd_that_was_unlinked_under_them(tmp_path):
    """A hook whose process already stands in the pruned worktree (it inherited the cwd, or was
    spawned before the prune landed) must still stamp. Both hooks step onto solid ground
    (SL_RUN_ROOT, else /) before doing any work, and every path they touch is absolute.

    Note what is deliberately NOT asserted: bash prints `shell-init: ... getcwd` when it STARTS in
    an unlinked directory, before the script's first line runs, so no in-script `cd` can suppress
    it. That noise is harmless; the stamp landing is the promise."""
    run_root = tmp_path / "run"
    for script in (ACTIVITY_HOOK, STOP_HOOK):
        doomed = tmp_path / "doomed"
        doomed.mkdir()
        env = {**os.environ, "SL_AGENT": "claude", "SL_ISSUE_ID": "i7",
               "SL_RUN_ROOT": str(run_root)}
        # stand in the directory, unlink it, THEN exec the hook — the pruned-cwd shape
        r = subprocess.run(
            ["bash", "-c", f'cd "{doomed}" && rm -rf "{doomed}" && exec bash "$0"', script],
            input="", env=env, capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
        assert (run_root / "state" / "activity" / "i7").exists()
