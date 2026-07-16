"""Issue #156 — the Claude worker PreToolUse deny hook.

Two of the costliest worker-instruction-drift incidents are made mechanically impossible here
rather than merely instructed-against:

  * AskUserQuestion in an unattended lane (i280): a human-facing dialog with no human at the pane,
    which stalled the lane all night. The deny points the worker at the DURABLE protocol — write
    the blocked-question file — that a fresh answerer actually reads.
  * a pattern-kill (`pkill -f`, `killall`) that matched and killed the owner's own live process
    (the dashboard). The deny restates the standing CLAUDE.md rule: kill exact PIDs only.

Both are Claude-only (Codex has no PreToolUse event — spike verdict), and both must be strict
no-ops outside a superlooper worker session so the hook is safe to register globally.

Two layers under test:
  * lib/worker_pretooluse.py — the pure decision core (`run`, `decide`), tested directly.
  * bin/pretooluse-hook.sh   — the entry-point script, driven end-to-end via subprocess with the
    exact stdin payload Claude Code sends, asserting the exact deny JSON it must print.
"""
import json
import os
import shutil
import subprocess

import pytest

import worker_pretooluse as wp

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
PRE_HOOK = os.path.join(REPO_ROOT, "skill", "bin", "pretooluse-hook.sh")

WORKER_ENV = {"SL_ISSUE_ID": "i7", "SL_RUN_ROOT": "/runs/willprout"}


# --------------------------- the pure decision core: run() ---------------------------

def _pre(tool_name, tool_input=None):
    p = {"hook_event_name": "PreToolUse", "tool_name": tool_name}
    if tool_input is not None:
        p["tool_input"] = tool_input
    return p


def test_ask_user_question_is_denied_with_the_blocked_file_fallback():
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    assert reason, "AskUserQuestion must be denied in a worker session"
    assert "AskUserQuestion" in reason
    # The deny must hand the worker the DURABLE protocol: its blocked-question file, at the exact
    # path the brief names (state/blocked/<id> under the run root).
    assert "/runs/willprout/state/blocked/i7" in reason


@pytest.mark.parametrize("command", [
    "pkill -f dashboard",
    "pkill dashboard",
    "killall node",
    "killall -9 Python",
    "sudo pkill -f server",
    "npm test && pkill -f leftover",
    "pgrep foo | xargs pkill",
    "PID=$(pgrep x); pkill x",
    "/usr/bin/pkill -f x",
    "ls\npkill x",
])
def test_pattern_kills_are_denied(command):
    reason = wp.run(_pre("Bash", {"command": command}), WORKER_ENV)
    assert reason, "pattern-kill must be denied: %r" % command
    assert "pkill" in reason and "PID" in reason


@pytest.mark.parametrize("command", [
    "kill 1234",
    "kill -9 $PID",
    "kill -TERM 42",
    "grep pkill /var/log/system.log",            # pkill as a search STRING, not a command
    'echo "remember to pkill later"',            # pkill inside a quoted literal
    "git commit -m 'stop using pkill/killall'",  # pkill in a commit message
    "cat notes-about-killall.txt",               # killall inside a filename
    "npm run test",
])
def test_benign_bash_is_allowed(command):
    assert wp.run(_pre("Bash", {"command": command}), WORKER_ENV) is None, \
        "must NOT deny a benign command: %r" % command


@pytest.mark.parametrize("command", [
    # ACCEPTED MISSES — unusual invocation forms the deliberately-narrow matcher does not catch (the
    # brief still instructs against them; a miss costs the safety net, not a killed process):
    "sh -c 'pkill x'",           # the name sits behind a quote, past any command-position anchor
    'bash -c "killall y"',
    "eval 'pkill z'",
    "xargs -r pkill",            # a flag breaks the xargs wrapper chain
    "if pkill foo; then echo x; fi",   # condition position
])
def test_known_pattern_kill_misses_are_intentional(command):
    # Pinned so a future regex tightening is a CONSCIOUS change, not an accident. If one of these
    # starts being denied, that is fine — but update this test on purpose.
    assert wp.run(_pre("Bash", {"command": command}), WORKER_ENV) is None


@pytest.mark.parametrize("command", [
    # ACCEPTED FALSE DENIES — a shell separator inside a quoted string reads as a command position;
    # the matcher errs toward denying (safe direction), which merely costs a rephrase.
    "git commit -m 'cleanup; pkill removed'",
    "echo 'do not | pkill things'",
])
def test_known_pattern_kill_false_denies_are_intentional(command):
    # Pinned for the same reason: this is the documented safe-direction tradeoff, not a bug. If a
    # future change stops denying these, update this test deliberately.
    assert wp.run(_pre("Bash", {"command": command}), WORKER_ENV) is not None


@pytest.mark.parametrize("tool", ["Edit", "Write", "Read", "Bash", "Grep", "Task"])
def test_other_tools_are_allowed(tool):
    # Bash here carries a harmless command; everything else is allowed outright. No broad allowlist.
    ti = {"command": "true"} if tool == "Bash" else {"file_path": "/tmp/x"}
    assert wp.run(_pre(tool, ti), WORKER_ENV) is None


def test_noop_outside_a_worker_session():
    # No SL_ISSUE_ID / SL_RUN_ROOT -> not a worker session; deny nothing, even AskUserQuestion.
    assert wp.run(_pre("AskUserQuestion", {}), {}) is None
    assert wp.run(_pre("AskUserQuestion", {}), {"SL_ISSUE_ID": "i7"}) is None
    assert wp.run(_pre("AskUserQuestion", {}), {"SL_RUN_ROOT": "/runs"}) is None


def test_noop_in_an_answerer_session():
    # An ANSWERER (id `a<N>`) is launched through the same start-session.sh, so it carries BOTH gate
    # vars — but it is not a worker: its blocked-file protocol doesn't exist (it writes one answer
    # file and escalates via `PARK:`). The deny must NOT fire for it, for either hazard.
    answerer = {"SL_ISSUE_ID": "a5", "SL_RUN_ROOT": "/runs/willprout"}
    assert wp.run(_pre("AskUserQuestion", {}), answerer) is None
    assert wp.run(_pre("Bash", {"command": "pkill -f x"}), answerer) is None
    # And a malformed / non-`i<N>` id is likewise never treated as a worker.
    for bad_id in ("", "i", "iabc", "7", "worker"):
        env = {"SL_ISSUE_ID": bad_id, "SL_RUN_ROOT": "/runs"}
        assert wp.run(_pre("AskUserQuestion", {}), env) is None, "id %r must not be a worker" % bad_id


def test_noop_for_codex_agent():
    # Codex has no PreToolUse event; the deny is Claude-only (spike verdict). Even a full worker env
    # denies nothing when SL_AGENT=codex.
    env = {**WORKER_ENV, "SL_AGENT": "codex"}
    assert wp.run(_pre("AskUserQuestion", {}), env) is None
    assert wp.run(_pre("Bash", {"command": "pkill -f x"}), env) is None


def test_noop_for_non_pretooluse_events():
    for ev in ("Stop", "PostToolUse", "SessionStart"):
        payload = {"hook_event_name": ev, "tool_name": "AskUserQuestion"}
        assert wp.run(payload, WORKER_ENV) is None


def test_malformed_tool_input_never_raises():
    # A wrong-typed / missing tool_input must fail closed to "allow", never raise.
    assert wp.run(_pre("Bash", "not-a-dict"), WORKER_ENV) is None
    assert wp.run({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, WORKER_ENV) is None
    assert wp.run("not-a-dict", WORKER_ENV) is None


# --------------------------- the entry-point script: pretooluse-hook.sh ---------------------------

def _run_hook(run_root, payload, agent="claude", issue_id="i7", cwd=None):
    env = {**os.environ, "SL_AGENT": agent, "SL_ISSUE_ID": issue_id, "SL_RUN_ROOT": str(run_root)}
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(["bash", PRE_HOOK], input=stdin, env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=10)


def _decision(stdout):
    """Parse the hook's stdout as a PreToolUse decision, or None when it printed nothing (allow)."""
    if not stdout.strip():
        return None
    return json.loads(stdout)


def test_hook_denies_ask_user_question_with_the_exact_claude_contract(tmp_path):
    run_root = tmp_path / "run"
    r = _run_hook(run_root, _pre("AskUserQuestion", {"questions": []}))
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    # The EXACT shape Claude Code requires to block a tool (even under --dangerously-skip-permissions).
    assert d["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = d["hookSpecificOutput"]["permissionDecisionReason"]
    assert "AskUserQuestion" in reason
    assert str(run_root / "state" / "blocked" / "i7") in reason


def test_hook_denies_a_pattern_kill(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("Bash", {"command": "pkill -f dashboard-server"}))
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "PID" in d["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_a_benign_call(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("Bash", {"command": "kill 4242"}))
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "a benign call must proceed (no decision printed)"


def test_hook_allows_a_non_hazard_tool(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}))
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None


def test_hook_is_a_noop_for_codex(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("AskUserQuestion", {}), agent="codex")
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "the PreToolUse deny is Claude-only"


def test_hook_is_a_noop_when_not_a_worker_session(tmp_path):
    # No SL_ISSUE_ID / SL_RUN_ROOT — the ad-hoc / William's-own / any-non-loop session case. The
    # shell exits before reading a byte.
    env = {k: v for k, v in os.environ.items() if k not in ("SL_ISSUE_ID", "SL_RUN_ROOT")}
    r = subprocess.run(["bash", PRE_HOOK], input=json.dumps(_pre("AskUserQuestion", {})),
                       env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None


def test_hook_is_a_noop_for_an_answerer_session(tmp_path):
    # An answerer (id `a<N>`) carries both gate vars but is not a worker — the deny must not fire.
    r = _run_hook(tmp_path / "run", _pre("AskUserQuestion", {}), issue_id="a5")
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "the deny is worker-scoped; an answerer must be untouched"


def test_hook_fails_open_when_the_lib_is_missing(tmp_path):
    """The central promise: a broken/absent decision core must degrade to ALLOW, never block every
    tool. Copy ONLY the hook script into a bin dir with no sibling ../lib — the shell's file guard
    misses, it drains stdin, and the call proceeds."""
    fake_skill = tmp_path / "skill"
    (fake_skill / "bin").mkdir(parents=True)
    (fake_skill / "lib").mkdir()                   # exists but EMPTY — no worker_pretooluse.py
    hook_copy = fake_skill / "bin" / "pretooluse-hook.sh"
    shutil.copy(PRE_HOOK, hook_copy)
    env = {**os.environ, "SL_AGENT": "claude", "SL_ISSUE_ID": "i7", "SL_RUN_ROOT": str(tmp_path / "run")}
    r = subprocess.run(["bash", str(hook_copy)], input=json.dumps(_pre("AskUserQuestion", {})),
                       env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "a missing decision core must ALLOW (fail open), never block"


def test_hook_fails_open_on_malformed_input(tmp_path):
    for blob in ("", "{", json.dumps({"hook_event_name": "PostToolUse"})):
        r = _run_hook(tmp_path / "run", blob)
        assert r.returncode == 0, "malformed input must fail closed (rc 0) and silent: %r" % blob
        assert _decision(r.stdout) is None, "malformed input must ALLOW (never block): %r" % blob


def test_hook_still_denies_from_a_safe_cwd_with_the_worktree_gone(tmp_path):
    """cwd-safety, the same guard the other worker hooks carry: a worker's worktree can be pruned
    out from under a live session. The deny must still fire — every path it needs is in the payload
    and the env, never the cwd."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    run_root = tmp_path / "run"
    shutil.rmtree(worktree)                       # pruned under the live CLI
    # Spawn from a SAFE cwd (Claude spawns hooks; an explicit-cwd spawn into a pruned dir dies in
    # posix_spawn before any script runs — that is the runner's teardown-ordering problem, not this
    # hook's, and is pinned in test_hooks.py). From safe ground the deny must still land.
    r = _run_hook(run_root, _pre("AskUserQuestion", {}), cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
