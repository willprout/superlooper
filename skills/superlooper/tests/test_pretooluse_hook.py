"""Issues #156 / #185 — the Claude PreToolUse deny hook for UNATTENDED loop sessions.

Two of the costliest worker-instruction-drift incidents are made mechanically impossible here
rather than merely instructed-against:

  * AskUserQuestion in an unattended lane (i280): a human-facing dialog with no human at the pane,
    which stalled the lane all night. The deny points the session at the DURABLE protocol its OWN
    role uses — the worker's blocked-question file, the debugger's memo + notify.
  * a pattern-kill (`pkill -f`, `killall`) that matched and killed the owner's own live process
    (the dashboard). The deny restates the standing CLAUDE.md rule: kill exact PIDs only.

#185 (owner ruling 2026-07-16) widened the scope from workers alone to EVERY unattended session
the loop launches, with the AskUserQuestion reason adapted per role. The ruling named the answerer
`a<N>` conditionally ("while any remain pre-#194"); #194 has since merged and retired that seat, so
the live roles are the worker `i<N>` and the watchdog debugger `d<N>`. The one carve-out is ATTENDANCE, not role: `superlooper
debug`'s owner tap launches a `d<N>` session with a person at the keyboard (SL_ATTENDED=1), and
that duty's whole premise ("no human is here to answer") is false there, so the dialog is allowed.
The pattern-kill duty's premise (the pattern can match the OWNER's live processes) holds either
way, so it is NEVER carved out.

Both are Claude-only (Codex has no PreToolUse event — spike verdict), and both must be strict
no-ops outside a superlooper session so the hook is safe to register globally.

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
DEBUGGER_ENV = {"SL_ISSUE_ID": "d3", "SL_RUN_ROOT": "/runs/willprout"}
# The owner tap (`superlooper debug`, issue #144): the SAME d<N> shape, but a person is at the
# keyboard — launch-session.sh carries SL_ATTENDED=1 into the session for exactly this distinction.
ATTENDED_DEBUGGER_ENV = {**DEBUGGER_ENV, "SL_ATTENDED": "1"}


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


# --------------------------- #230: the worker deny must match the LIVE contract ---------------------------
# A deny reason is delivered to the model VERBATIM at the exact moment it errs, so a stale one is
# worse than none: the worker acts on it. These pin the four ways the #156 text had drifted off the
# contract by the 2026-07-16 first-prompt audit.

def test_worker_deny_does_not_teach_the_retired_answerer_flow():
    """The original text promised "a fresh answerer replies into this session". Nobody does. #163
    changed the shape entirely: the runner posts the question DURABLY as a GitHub comment, closes
    the window, and releases the lane; a FRESH session later resumes the issue with the owner's
    answer embedded in its brief. #194 then retired the answerer seat outright. A worker believing
    the old text waits in a closing window for a reply that is never coming."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    assert "answerer" not in reason.lower(), "the answerer seat is retired (#194) — never teach it"
    assert "replies into this session" not in reason
    assert "fresh session" in reason.lower(), "the deny must name what ACTUALLY resumes the issue"
    assert "end the session" in reason.lower(), "the deny must tell the worker to end, not to wait"


def test_worker_deny_requires_committing_and_pushing_the_wip():
    """The blocked protocol is write + COMMIT AND PUSH + end (brief-footer.md). The old text said
    only "write ... and end your turn", so a worker obeying it VERBATIM ended its session with this
    checkout holding the only copy of its work — the i153/i163 loss #190 now fences.

    The REASON given for the push must stay honest (fresh-agent review P0). The resume does not
    depend on it: _exec_post_question tears down with remove_worktree=False and launch-session.sh
    creates a worktree only when none exists, so the relaunch reuses the PRESERVED WIP. Claiming
    unpushed work is unrecoverable would be a new falsehood of the very class this issue removes,
    and a worker whose push failed would then refuse to end its session — the i280 stall, re-entered
    through the deny's own words. So: require the push, forbid the scare story."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    low = reason.lower()
    assert "commit" in low, "the deny must require committing the WIP"
    assert "git push -u origin head" in low, \
        "the deny must give the actual push command, not merely mention a pushed branch"
    # Pin the SUBSTANCE, not the phrasing: the resume must be described as reusing the preserved
    # worktree's WIP, and the scare story must not come back. An earlier version of this test also
    # pinned the noun phrase "only copy of your work", which pinned an over-claim of its own — the
    # push buys a copy off this MACHINE, not off this checkout (a committed branch ref survives the
    # checkout). Wording is free to move; these two facts are not.
    assert "work-in-progress" in low and "worktree" in low, \
        "the deny must say the resume reuses the PRESERVED worktree's WIP"
    assert "cannot pick up" not in low, \
        "never tell a worker the resume cannot see unpushed work — it reuses this very worktree"


def test_worker_deny_states_the_three_part_question_form():
    """A human reads the blocked file and the brief pins its three parts. Naming the path but not
    the form invites a bare sentence the owner cannot act on — and the runner quotes that file
    straight into a GitHub comment, so the shape IS the deliverable."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    for part in ("QUESTION:", "OPTIONS:", "RECOMMENDATION:"):
        assert part in reason, "the deny must name the %s part the brief requires" % part


def test_worker_deny_assumption_hint_is_not_pr_only():
    """An `investigate` worker opens ZERO pull requests — brief.py special-cases the assumption
    hint (_ASSUME_INVESTIGATE) for exactly this. The hook cannot see the issue type (no launcher
    exports one), so its hint must be type-NEUTRAL: it may offer the PR body, but never as the only
    place, or it hands every investigate worker an impossible instruction."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    low = reason.lower()
    # "root-cause report", not a bare "report": the loose token would pass on any incidental mention
    # (the debugger's reports/ path, "your report"), and the phrase must match brief.py's own
    # _ASSUME_INVESTIGATE so the deny and the brief name the SAME deliverable.
    assert "root-cause report" in low, \
        "the hint must also cover the no-PR (investigate) worker, whose deliverable is that report"
    assert "pr body" in low, "the hint must still name the PR body for the code types"


def test_worker_deny_states_the_two_question_cap():
    """The deny restates the whole protocol, so dropping the cap would leave a worker believing
    blocking is free. It is not: actions.QUESTION_CAP is 2 and a THIRD question PARKS the issue
    needs-owner instead of posting-and-resuming. The cap is what makes the assumption hint that
    follows it the cheaper move."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), WORKER_ENV)
    # Case-insensitive: the cap is the fact under test, not the SHOUTING that currently renders it.
    assert "two questions" in reason.lower(), \
        "the deny must state the 2-question cap the runner enforces (actions.QUESTION_CAP)"


def test_debugger_ask_user_question_is_denied_with_the_memo_fallback():
    """#185: the watchdog's unattended sl-debugger (d<N>) has neither protocol — it ends every run
    with a memo in the state home's reports/ plus a notify, so that is what the deny hands back."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), DEBUGGER_ENV)
    assert reason, "AskUserQuestion must be denied in an unattended debugger session"
    assert "/runs/willprout/reports" in reason, "the memo path is the debugger's escalation channel"
    assert "state/blocked" not in reason, "the worker blocked-file protocol is wrong for a debugger"


def test_attended_owner_tap_debugger_may_still_ask():
    """`superlooper debug` (issue #144) puts a PERSON at the keyboard and its brief says so. The
    AskUserQuestion duty exists only because nobody is there — with SL_ATTENDED=1 it must not fire,
    or the deny would tell the session a falsehood and push it into the unattended contract."""
    assert wp.run(_pre("AskUserQuestion", {"questions": []}), ATTENDED_DEBUGGER_ENV) is None


def test_attendance_cannot_be_claimed_by_a_worker():
    """The owner tap (`d<N>`) is the ONLY attended session the loop can launch, so the flag is
    honored for that role alone. A worker inherits its env from the runner's shell — an ambient
    `export SL_ATTENDED=1` there must not quietly disarm the deny that i280 bought."""
    assert wp.run(_pre("AskUserQuestion", {}), {**WORKER_ENV, "SL_ATTENDED": "1"}) is not None


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_attended_is_read_the_same_way_the_launch_stack_reads_booleans(truthy):
    env = {**DEBUGGER_ENV, "SL_ATTENDED": truthy}
    assert wp.run(_pre("AskUserQuestion", {}), env) is None


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "maybe"])
def test_a_non_truthy_attended_flag_is_still_unattended(falsy):
    env = {**DEBUGGER_ENV, "SL_ATTENDED": falsy}
    assert wp.run(_pre("AskUserQuestion", {}), env) is not None


def test_attendance_never_unlocks_the_pattern_kill_deny():
    """The kill duty's premise — the pattern can also match the OWNER's own live processes — does
    not depend on anyone watching, and no brief ever promises a debugger pattern-kills (the
    sl-debugger contract forbids them outright). Attendance carves out the dialog duty ONLY."""
    reason = wp.run(_pre("Bash", {"command": "pkill -f runner"}), ATTENDED_DEBUGGER_ENV)
    assert reason and "PID" in reason


@pytest.mark.parametrize("env", [DEBUGGER_ENV, ATTENDED_DEBUGGER_ENV])
def test_pattern_kills_are_denied_in_every_loop_session(env):
    reason = wp.run(_pre("Bash", {"command": "pkill -f dashboard"}), env)
    assert reason, "pattern-kill must be denied in %s" % env["SL_ISSUE_ID"]
    assert "pkill" in reason and "PID" in reason


@pytest.mark.parametrize("env", [WORKER_ENV, DEBUGGER_ENV, ATTENDED_DEBUGGER_ENV])
def test_benign_bash_stays_allowed_in_every_loop_session(env):
    assert wp.run(_pre("Bash", {"command": "kill 4242"}), env) is None


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


def test_noop_for_an_unrecognized_session_id():
    # Only the ids the loop's own launchers can produce (`i<N>` and `d<N>` — the exact shapes
    # launch-session.sh enforces) name a session whose escalation protocol we know. Anything else is
    # a session we cannot hand a correct fallback to, so we deny nothing. `a5` is in this list on
    # purpose: #194 retired the answerer seat, so an a<N> is now an unrecognized id like any other.
    for bad_id in ("", "i", "iabc", "7", "worker", "x9", "i7x", "d", "a5", "a-1", "I7"):
        env = {"SL_ISSUE_ID": bad_id, "SL_RUN_ROOT": "/runs"}
        assert wp.run(_pre("AskUserQuestion", {}), env) is None, "id %r must not be a session" % bad_id
        assert wp.run(_pre("Bash", {"command": "pkill -f x"}), env) is None


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

def _run_hook(run_root, payload, agent="claude", issue_id="i7", cwd=None, attended=None):
    env = {**os.environ, "SL_AGENT": agent, "SL_ISSUE_ID": issue_id, "SL_RUN_ROOT": str(run_root)}
    env.pop("SL_ATTENDED", None)
    if attended is not None:
        env["SL_ATTENDED"] = attended
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


def test_hook_denies_ask_user_question_for_an_unattended_debugger(tmp_path):
    run_root = tmp_path / "run"
    r = _run_hook(run_root, _pre("AskUserQuestion", {}), issue_id="d3")
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert str(run_root / "reports") in d["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_lets_the_attended_owner_tap_debugger_ask(tmp_path):
    # `superlooper debug` sets SL_ATTENDED=1 — a person IS at this pane, so the dialog stands.
    r = _run_hook(tmp_path / "run", _pre("AskUserQuestion", {}), issue_id="d3", attended="1")
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "an attended session must keep its dialog"


def test_hook_still_denies_a_pattern_kill_in_the_attended_debugger(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("Bash", {"command": "killall node"}),
                  issue_id="d3", attended="1")
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_is_a_noop_for_an_unrecognized_session_id(tmp_path):
    r = _run_hook(tmp_path / "run", _pre("AskUserQuestion", {}), issue_id="nonsense")
    assert r.returncode == 0, r.stderr
    assert _decision(r.stdout) is None, "an id whose protocol we don't know gets no deny"


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
