import json
import os
import subprocess

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
