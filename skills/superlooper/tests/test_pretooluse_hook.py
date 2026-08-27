"""Issues #156 / #185 / #225 / #226 — the Claude PreToolUse deny hook for UNATTENDED loop sessions.

The costliest worker-instruction-drift incidents — and, since #226, the three paths to a bad merge
(see that section's own header at the foot of this file) — are made mechanically impossible here
rather than merely instructed-against:

  * AskUserQuestion in an unattended lane (i280): a human-facing dialog with no human at the pane,
    which stalled the lane all night. The deny points the session at the DURABLE protocol its OWN
    role uses — the worker's blocked-question file, the debugger's memo + notify.
  * a pattern-kill (`pkill -f`, `killall`) that matched and killed the owner's own live process
    (the dashboard). The deny restates the standing CLAUDE.md rule: kill exact PIDs only.
  * a mechanically-invalid `gh issue create` (#225): the 2026-07-16 audit found 25 of 35 open
    issues unlaunchable for want of a `type:` label and/or a parseable `## Loop metadata` section.
    The deny validates the issue BEFORE it exists and hands back exactly what is missing.

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
import shlex
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
# keyboard — the launcher carries SL_ATTENDED=1 into the session for exactly this distinction.
ATTENDED_DEBUGGER_ENV = {**DEBUGGER_ENV, "SL_ATTENDED": "1"}
# The triage flight (#448) — the third id shape a loop launcher can produce. Until it had a role
# here, `_role("t1")` answered None and a flight was allowed EVERY hazard this module denies.
TRIAGE_ENV = {"SL_ISSUE_ID": "t1", "SL_RUN_ROOT": "/runs/willprout"}


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
    depend on it: _exec_post_question tears down with remove_worktree=False and the launcher
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


def test_triage_ask_user_question_is_denied_with_the_flights_own_fallback():
    """#448: the triage flight (`t<N>`) has neither of the other two protocols. It holds no lane,
    so `state/blocked/<id>` is a dead drop (the runner reads one for an `i<N>` alone) and there is
    no "a fresh session resumes you with the answer" to promise — a flight is not resumed. Its
    unanswerable items are ESCALATIONS on the owner's sitting sheet, recorded in its run log."""
    reason = wp.run(_pre("AskUserQuestion", {"questions": []}), TRIAGE_ENV)
    assert reason, "AskUserQuestion must be denied in an unattended triage flight"
    assert "/runs/willprout/triage/runs" in reason, "the run log is where a flight records"
    assert "state/blocked" not in reason, "the worker blocked-file protocol is a dead drop for t<N>"
    assert "sl-debugger" not in reason, "nor is the debugger's memo contract the flight's"


def test_a_triage_flight_can_never_claim_attendance():
    """The owner tap (`d<N>`) is the ONLY attended session the loop can launch. A flight is
    launched on a schedule with nobody there, and its pane environment pins SL_ATTENDED empty — so
    an ambient `export SL_ATTENDED=1` anywhere upstream must not be able to disarm duty 1 for it."""
    assert wp.run(_pre("AskUserQuestion", {"questions": []}),
                  {**TRIAGE_ENV, "SL_ATTENDED": "1"}) is not None


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
    # lib/launch.py enforces) name a session whose escalation protocol we know. Anything else is
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


# =============================== duty 3: `gh issue create` (issue #225) ===============================
# The 2026-07-16 queue audit found 25 of 35 open issues mechanically unlaunchable — 16 of them
# `agent-ready` — because they lacked a `type:` label and/or a parseable `## Loop metadata` section.
# Workers file follow-ups all day and were never handed the mechanical format. The owner's redesign
# (2026-07-16): stop teaching it in the first prompt (his words — extra instructions in the very
# first prompt get ignored half the time) and BOUNCE the session at the moment it files wrong. The
# issue is validated BEFORE it exists; the deny reason names exactly what is missing; the session
# retries correctly on the freshest possible turn.
#
# FAIL OPEN PER DIMENSION is the load-bearing half. This fires on every Bash call, and a hook that
# denied a legitimate `gh issue create` it merely could not READ would cost more than the defect it
# prevents. So each dimension answers only from evidence it actually has: an unreadable body stands
# the body dimension down while the labels are still judged, a `--repo` pointing who-knows-where
# stands the whole duty down, and an unparseable command line is never judged at all.

VALID_BODY = "## Goal\nship it\n\n## Loop metadata\ntouches: engine\n"
REPO_CFG = {"repo": "willprout/superlooper", "touches_required": True,
            "areas": {"engine": ["skills/**"], "dashboard": ["dashboard/**"]}}


def _worktree(tmp_path, cfg=REPO_CFG, issue_id="i7"):
    """A worker's real on-disk shape: <run root>/worktrees/<id>/.superlooper/config.json — the
    path the launcher actually creates, so the hook can find the repo's contract from the
    environment alone."""
    wt = tmp_path / "worktrees" / issue_id
    (wt / ".superlooper").mkdir(parents=True)
    if cfg is not None:
        (wt / ".superlooper" / "config.json").write_text(json.dumps(cfg))
    return wt


def _create(command, tmp_path=None, env=None, cwd=None):
    payload = _pre("Bash", {"command": command})
    if cwd is not None:
        payload["cwd"] = str(cwd)
    e = dict(env or WORKER_ENV)
    if tmp_path is not None:
        e["SL_RUN_ROOT"] = str(tmp_path)
    return wp.run(payload, e)


# --- the deny, and what it must say ---

def test_an_issue_with_no_type_label_is_denied_before_it_exists(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title 'x' --label needs-owner --body %s" % shlex.quote(VALID_BODY),
                     tmp_path)
    assert reason, "an issue with no type: label must be refused at creation"
    assert "type:build" in reason and "type:investigate" in reason


def test_two_type_labels_are_denied(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label type:build --label type:investigate "
                     "--body %s" % shlex.quote(VALID_BODY), tmp_path)
    assert reason and "type:build" in reason and "type:investigate" in reason


def test_a_missing_loop_metadata_section_is_denied_and_the_shape_is_handed_back(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label type:build --body '## Goal\nship it\n'",
                     tmp_path)
    assert reason
    assert "## Loop metadata" in reason and "touches:" in reason


def test_an_undeclared_area_is_denied_and_the_real_areas_are_named(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label type:build "
                     "--body '## Loop metadata\ntouches: plugin\n'", tmp_path)
    assert reason and "plugin" in reason
    assert "engine" in reason and "dashboard" in reason


def test_a_mechanically_valid_issue_is_allowed_through(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build,needs-owner --body %s"
                   % shlex.quote(VALID_BODY), tmp_path) is None


def test_the_wildcard_area_is_allowed(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build "
                   "--body '## Loop metadata\ntouches: *\n'", tmp_path) is None


def test_an_investigation_needs_no_touches(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:investigate --body '## Goal\nwhy?\n'",
                   tmp_path) is None


def test_the_deny_names_every_defect_at_once_not_one_per_retry(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label needs-owner --body '## Goal\nx\n'",
                     tmp_path)
    assert "type:" in reason and "## Loop metadata" in reason


# --- label argument shapes gh itself accepts ---

@pytest.mark.parametrize("labels", [
    "--label type:build --label needs-owner",
    "--label type:build,needs-owner",
    "--label=type:build --label=needs-owner",
    "-l type:build -l needs-owner",
    "--label 'type:build, needs-owner'",
])
def test_every_label_argument_shape_gh_accepts_is_read(tmp_path, labels):
    _worktree(tmp_path)
    assert _create("gh issue create --title x %s --body %s" % (labels, shlex.quote(VALID_BODY)),
                   tmp_path) is None


@pytest.mark.parametrize("body_flag", ["--body", "-b", "--body="])
def test_every_body_argument_shape_gh_accepts_is_read(tmp_path, body_flag):
    _worktree(tmp_path)
    sep = "" if body_flag.endswith("=") else " "
    reason = _create("gh issue create --title x --label type:build %s%s'## Goal\nx\n'"
                     % (body_flag, sep), tmp_path)
    assert reason and "## Loop metadata" in reason


def test_no_label_flag_at_all_is_confident_evidence_of_no_type_label(tmp_path):
    # Not ambiguity: the whole command line parsed, and it carries no labels. Two live issues in
    # this very repo (#284, #286) were filed exactly this way — with no labels at all.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --body %s" % shlex.quote(VALID_BODY), tmp_path)


# --- fail open, per dimension ---

def test_a_body_built_by_command_substitution_stands_the_body_dimension_down(tmp_path):
    # `--body "$(cat notes.md)"` — the text is not knowable from the command line, so it is not
    # judged. The LABELS still are.
    _worktree(tmp_path)
    assert _create('gh issue create --title x --label type:build --body "$(cat notes.md)"',
                   tmp_path) is None
    reason = _create('gh issue create --title x --label needs-owner --body "$(cat notes.md)"',
                     tmp_path)
    assert reason and "type:" in reason
    assert "## Loop metadata" not in reason      # never complain about a body we could not read


@pytest.mark.parametrize("flag", ["--body-file notes.md", "-F notes.md", "--editor",
                                  "--template bug.md"])
def test_a_body_from_somewhere_else_stands_the_body_dimension_down(tmp_path, flag):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build %s" % flag, tmp_path) is None


@pytest.mark.parametrize("flag", ["--web", "--recover draft.json",
                                  # gh's own documented shorthand, and the attached forms both
                                  # flags accept — the same gap `-Rowner/repo` was caught on.
                                  "-w", "--web=true", "--recover=draft.json"])
def test_a_form_filled_ELSEWHERE_stands_the_whole_duty_down(tmp_path, flag):
    # Fresh-agent review, 2026-07-30. `--web` hands the form to a browser for the person to fill
    # in; `--recover` restores a saved draft's fields, labels included. In both, no `--label` on
    # the command line means labels we did not READ — not the confident evidence of a missing
    # `type:` label that an ordinary line's absence is. The duty denied over exactly that absence,
    # complaining about a label the author was about to pick: a verdict on evidence it never had.
    _worktree(tmp_path)
    assert _create("gh issue create %s" % flag, tmp_path) is None
    assert _create("gh issue create %s --title x" % flag, tmp_path) is None


def test_no_body_flag_at_all_stands_the_body_dimension_down(tmp_path):
    # Without --body gh prompts or errors; either way there is no text to judge.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build", tmp_path) is None


def test_a_blank_area_name_never_reaches_the_sentence_that_teaches_the_format(tmp_path):
    # The deny reaches the model VERBATIM at the moment it errs, so its area list is the one place
    # a blank config key would read as an area you may declare ("areas: ,    , engine").
    _worktree(tmp_path, cfg={"areas": {"": ["a/**"], "   ": ["b/**"], "engine": ["skills/**"]},
                             "touches_required": True})
    reason = _create('gh issue create --title x --label type:build --body "## Goal\nx\n"', tmp_path)
    assert reason and "areas: engine (" in reason


@pytest.mark.parametrize("flag", ["--repo other/repo", "-R other/repo"])
def test_an_explicit_repo_stands_the_WHOLE_duty_down(tmp_path, flag):
    # A different repo has a different contract (its own areas, its own touches_required). We do
    # not hold this repo's rules over an issue filed somewhere else.
    _worktree(tmp_path)
    assert _create("gh issue create %s --title x --body '## Goal\nx\n'" % flag, tmp_path) is None


def test_no_config_in_reach_still_judges_the_type_label_but_not_the_metadata(tmp_path):
    # The type: vocabulary is superlooper's own and repo-independent; whether touches are required,
    # and which areas exist, are NOT — so without a config those dimensions stand down.
    _worktree(tmp_path, cfg=None)
    assert _create("gh issue create --title x --label type:build --body '## Goal\nx\n'",
                   tmp_path) is None
    assert _create("gh issue create --title x --label needs-owner --body '## Goal\nx\n'",
                   tmp_path)


def test_an_unreadable_config_fails_open_the_same_way(tmp_path):
    _worktree(tmp_path, cfg=None)
    (tmp_path / "worktrees" / "i7" / ".superlooper" / "config.json").write_text("{not json")
    assert _create("gh issue create --title x --label type:build --body '## Goal\nx\n'",
                   tmp_path) is None


def test_a_repo_that_does_not_require_touches_is_not_asked_for_them(tmp_path):
    _worktree(tmp_path, cfg={**REPO_CFG, "touches_required": False})
    assert _create("gh issue create --title x --label type:build --body '## Goal\nx\n'",
                   tmp_path) is None


def test_the_config_is_also_found_from_the_sessions_own_cwd(tmp_path):
    # The `d<N>` debugger runs with --cwd against the repo itself and has no worktree under the
    # state home, so the payload's cwd is its route to the same contract. Walked UP, so a session
    # sitting in a subdirectory still finds it.
    repo = tmp_path / "checkout"
    (repo / ".superlooper").mkdir(parents=True)
    (repo / ".superlooper" / "config.json").write_text(json.dumps(REPO_CFG))
    deep = repo / "skills" / "superlooper"
    deep.mkdir(parents=True)
    reason = _create("gh issue create --title x --label type:build "
                     "--body '## Loop metadata\ntouches: plugin\n'",
                     env=DEBUGGER_ENV, cwd=deep)
    assert reason and "plugin" in reason


# --- the provenance advisory rides along, and never denies (issue #400) ---

def test_a_missing_source_label_never_denies_an_otherwise_valid_issue(tmp_path):
    # The whole family is display-only (owner ruling 2026-08-06): nothing may EVER block on it, and
    # this hook is the only surface that even asks. A valid issue with no `source:` label is created,
    # full stop — otherwise every worker filing a follow-up would be refused for provenance, which
    # is the definition of blocking on it.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build --body %s"
                   % shlex.quote(VALID_BODY), tmp_path) is None


def test_a_deny_the_issue_has_already_earned_also_teaches_the_source_label(tmp_path):
    # When the command is being refused anyway, the session is about to retype it — so this is the
    # cheapest possible moment to say the whole contract at once, including the part that is only
    # ever advice. It rides ALONG with a real defect; it never creates one.
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --body %s" % shlex.quote(VALID_BODY), tmp_path)
    assert reason and "type:" in reason
    assert "no `source:` label" in reason


def test_an_issue_that_names_its_source_is_told_nothing_about_provenance(tmp_path):
    # The type: complaint still stands (it is a real defect); the provenance line is gone, because
    # the issue answered it. Any value answers it — the family is open.
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label source:slackbot --body %s"
                     % shlex.quote(VALID_BODY), tmp_path)
    assert reason and "type:" in reason
    assert "no `source:` label" not in reason


@pytest.mark.parametrize("command", [
    "gh issue create --title 'unbalanced --body x",          # shlex cannot split it
    "echo gh issue create",                                  # not at a command position
    "gh issue list",                                         # a different gh verb
    "gh pr create --fill",                                   # a different noun
    "git commit -m 'gh issue create'",                       # the words, quoted, inside something else
    "gh issue create --title a --body x && gh issue create --title b --body y",  # two of them
])
def test_anything_we_cannot_confidently_read_as_one_gh_issue_create_is_allowed(tmp_path, command):
    _worktree(tmp_path)
    assert _create(command, tmp_path) is None


def test_a_gh_issue_create_after_a_separator_is_still_read(tmp_path):
    # `cd x && gh issue create ...` is the ordinary shape; a separator must not hide the call.
    _worktree(tmp_path)
    assert _create("cd /tmp && gh issue create --title x --label needs-owner --body %s"
                   % shlex.quote(VALID_BODY), tmp_path)


def test_an_absolute_path_to_gh_is_still_gh(tmp_path):
    _worktree(tmp_path)
    assert _create("/opt/homebrew/bin/gh issue create --title x --label needs-owner --body %s"
                   % shlex.quote(VALID_BODY), tmp_path)


# --- the duty holds for every session the loop launches, and nowhere else ---

@pytest.mark.parametrize("env", [WORKER_ENV, DEBUGGER_ENV, ATTENDED_DEBUGGER_ENV, TRIAGE_ENV])
def test_the_deny_holds_for_every_role_attended_or_not(tmp_path, env):
    # Attendance carves out duty 1 ONLY: a person at the pane does not make an unlaunchable issue
    # launchable, and the correction is just as cheap for them.
    _worktree(tmp_path, issue_id=env["SL_ISSUE_ID"])
    assert _create("gh issue create --title x --label needs-owner --body %s" % shlex.quote(VALID_BODY),
                   tmp_path, env=env)


def test_no_deny_outside_a_loop_session(tmp_path):
    _worktree(tmp_path)
    assert wp.run(_pre("Bash", {"command": "gh issue create --title x --body x"}),
                  {"SL_ISSUE_ID": "", "SL_RUN_ROOT": str(tmp_path)}) is None


def test_no_deny_for_codex(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --body '## Goal\nx\n'", tmp_path,
                   env={**WORKER_ENV, "SL_AGENT": "codex"}) is None


def test_a_broken_config_read_never_raises_into_the_hook(tmp_path):
    # The whole duty runs behind main()'s fail-open catch, but a raise here would also blank the
    # OTHER two duties for that call. Prove it degrades in place instead.
    d = tmp_path / "worktrees" / "i7" / ".superlooper"
    d.mkdir(parents=True)
    (d / "config.json").mkdir()                  # a DIRECTORY where the config should be
    assert _create("gh issue create --title x --label type:build --body '## Goal\nx\n'",
                   tmp_path) is None


def test_the_hook_script_denies_an_invalid_issue_create_end_to_end(tmp_path):
    (tmp_path / "worktrees" / "i7" / ".superlooper").mkdir(parents=True)
    (tmp_path / "worktrees" / "i7" / ".superlooper" / "config.json").write_text(json.dumps(REPO_CFG))
    payload = _pre("Bash", {"command": "gh issue create --title x --label needs-owner "
                                       "--body '## Goal\nx\n'"})
    r = _run_hook(tmp_path, payload)
    d = _decision(r.stdout)
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "type:" in d["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_hook_script_allows_a_valid_issue_create_end_to_end(tmp_path):
    (tmp_path / "worktrees" / "i7" / ".superlooper").mkdir(parents=True)
    (tmp_path / "worktrees" / "i7" / ".superlooper" / "config.json").write_text(json.dumps(REPO_CFG))
    payload = _pre("Bash", {"command": "gh issue create --title x --label type:build --body %s"
                                       % shlex.quote(VALID_BODY)})
    r = _run_hook(tmp_path, payload)
    assert _decision(r.stdout) is None
    assert r.returncode == 0


# --- fresh-agent review (Codex, 2026-07-28), P0: a bare shell variable is not readable text ---
# `--body "$body"` and `--label "$labels"` are ordinary shapes, and shlex leaves them as the literal
# strings `$body` / `$labels`. Judging those AS the body or AS the label set denies a perfectly good
# command over content the hook never read — the exact fail-open-per-dimension violation this duty
# exists to avoid. An unreadable LABEL set stands the whole duty down, not just the label check:
# without the labels we cannot know the issue is an investigation, which needs no touches at all.

@pytest.mark.parametrize("body_arg", ['"$body"', '"$BODY_TEXT"', "$body", '"prefix $body"'])
def test_a_body_held_in_a_shell_variable_stands_the_body_dimension_down(tmp_path, body_arg):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build --body %s" % body_arg,
                   tmp_path) is None


@pytest.mark.parametrize("label_arg", ['"$labels"', "$LABELS", '"type:build,$extra"'])
def test_labels_held_in_a_shell_variable_stand_the_WHOLE_duty_down(tmp_path, label_arg):
    # Not merely the label check: an unreadable label set means we cannot tell an investigation
    # (which needs no `touches:`) from a build (which does), so demanding one would be a guess.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label %s --body '## Goal\nx\n'" % label_arg,
                   tmp_path) is None


def test_a_dollar_sign_that_is_merely_TEXT_still_costs_only_a_stood_down_check(tmp_path):
    # A body legitimately containing `$` reads as unexpanded and is not judged. That is a false
    # ALLOW — the safe direction — and it is the trade this rule makes deliberately.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build --body 'costs 5$ per run'",
                   tmp_path) is None


def test_a_literal_body_and_labels_are_still_judged_normally(tmp_path):
    # The rule must not gut the duty: nothing here holds a `$`.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label needs-owner --body '## Goal\nx\n'", tmp_path)


# --- fresh-agent review round 2 (Codex, 2026-07-28), P0: gh accepts ATTACHED short options ---
# `gh` is cobra/pflag-based, so `-ltype:build`, `-b'…'` and `-Rowner/repo` are all valid and all
# common. The parser read only the separated form, so it saw `-ltype:build` as an unrecognized
# token: the labels came back EMPTY and a valid command was denied for a `type:` label that was
# right there. The `-R` miss was the same bug pointed the other way — this repo's contract held
# over an issue being filed somewhere else entirely.

def test_an_attached_short_label_is_read(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x -ltype:build --body %s" % shlex.quote(VALID_BODY),
                   tmp_path) is None


def test_an_attached_short_body_is_read(tmp_path):
    _worktree(tmp_path)
    reason = _create("gh issue create --title x --label type:build -b'## Goal\nx\n'", tmp_path)
    assert reason and "## Loop metadata" in reason


def test_an_attached_short_repo_stands_the_whole_duty_down(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create -Rcli/cli --label type:build --body '## Goal\nx\n'",
                   tmp_path) is None


def test_attached_and_separated_short_options_mix(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create -t x -ltype:build -l needs-owner --body %s"
                   % shlex.quote(VALID_BODY), tmp_path) is None


def test_an_attached_short_label_holding_a_variable_still_stands_the_duty_down(tmp_path):
    _worktree(tmp_path)
    assert _create("gh issue create --title x -l$labels --body '## Goal\nx\n'", tmp_path) is None


# ======================= duties 4-6: the three bad-merge paths (issue #226) =======================
# Three routes to a bad merge were guarded only by prose. The owner ruled on 2026-08-05 (Q11) that
# there will be NO bot account — every loop action stays on the owner's own identity, so no
# vendor-side token scoping is coming and no after-the-fact provenance check is even possible (every
# session posts as the same login). The moment of the tool call is the only place identity exists,
# which makes these denies THE protection for all three paths rather than one layer of several.
#
#   4. FORGED COMMIT STATUS. The gate's required-checks rollup folds gh's two shapes — CheckRun
#      (name/conclusion) and StatusContext (context/state) — into one verdict, and GitHub's own
#      branch protection accepts a hand-posted context just as readily, so a failing diff can merge
#      green. #232 measured the account-credential-writable surface: exactly one endpoint,
#      POST /repos/{o}/{r}/statuses/{sha}.
#   5. APPROVAL LABELS. `agent-ready` IS the launch gate and `pre-authorized:*` is what the merge
#      gate consumes (#165) — a session that can apply either self-approves work the owner never
#      saw. Workers are INSTRUCTED to file child issues, so they hold the very tool that does it.
#   6. OUT-OF-BAND SHIPPING. `gh pr merge`, a direct push to the dev branch, and any force-push.
#      Precedent i328 (2026-07-15): a worker merged its own PR, defeating all three completion
#      signals at once and stalling the queue two hours.
#
# Same fail-open discipline as duties 2 and 3: deny only on confident matches, let ambiguous or
# read-shaped calls through, and PIN the accepted misses so a future tightening is deliberate.


def _bash(command, tmp_path=None, env=None, cwd=None):
    """The hook's verdict for one Bash command — the deny reason, or None to allow."""
    return _create(command, tmp_path=tmp_path, env=env, cwd=cwd)


# --------------------------- matcher A: a forged commit status ---------------------------

@pytest.mark.parametrize("command", [
    "gh api repos/willprout/superlooper/statuses/abc123 -f state=success -f context=tests",
    "gh api /repos/willprout/superlooper/statuses/abc123 --method POST -f state=success",
    "gh api -X POST repos/willprout/superlooper/statuses/$SHA -f state=success",
    "gh api --method POST 'repos/willprout/superlooper/statuses/abc' -f context=tests",
    "gh api https://api.github.com/repos/o/r/statuses/deadbeef -f state=success",
    "cd /tmp && gh api repos/o/r/statuses/abc -f state=success",
    "SHA=$(git rev-parse HEAD)\ngh api repos/o/r/statuses/$SHA -f state=success",
    "/opt/homebrew/bin/gh api repos/o/r/statuses/abc -f state=success",
])
def test_a_hand_posted_commit_status_is_denied(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "writing a commit status must be denied: %r" % command


def test_the_status_deny_names_the_contract_it_protects(tmp_path):
    """The reason reaches the model VERBATIM at the moment it errs, so it must teach the contract,
    not merely forbid: CI goes green because the tests RAN, the gate reads real checks, and writing
    one is never a worker's job."""
    _worktree(tmp_path)
    reason = _bash("gh api repos/o/r/statuses/abc -f state=success", tmp_path)
    low = reason.lower()
    assert "commit status" in low
    assert "gate" in low, "the deny must name what a forged status would fool"
    assert "tests ran" in low or "tests actually ran" in low, \
        "the deny must state WHY CI goes green — because the tests ran, not because someone said so"


@pytest.mark.parametrize("command", [
    # READS of the same data are a different endpoint shape entirely (`/commits/<sha>/status`,
    # `/commits/<sha>/statuses`) and are exactly how a worker checks its own CI. Never denied.
    "gh api repos/willprout/superlooper/commits/abc123/status",
    "gh api repos/willprout/superlooper/commits/abc123/statuses",
    "gh api repos/o/r/commits/abc/check-runs --jq '.check_runs[].conclusion'",
    "gh pr checks 42",
    "gh api repos/o/r/pulls/42 --jq .mergeable",
    "gh pr comment 42 --body 'the statuses/ endpoint is not for us'",
    "gh issue comment 7 --body 'see repos/o/r/statuses/abc in the docs'",
])
def test_reading_status_and_ordinary_gh_calls_are_untouched(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "must NOT deny: %r" % command


# --------------------------- matcher B: approval labels ---------------------------

@pytest.mark.parametrize("command", [
    "gh issue edit 42 --add-label agent-ready",
    "gh issue edit 42 --add-label=agent-ready",
    "gh issue edit 42 --add-label 'needs-owner,agent-ready'",
    "gh pr edit 42 --add-label agent-ready",
    "gh issue edit 42 --add-label pre-authorized:referee",
    "gh pr edit 42 --add-label pre-authorized:anything",
    "gh issue create --title x --label type:build,agent-ready --body 'b'",
    "gh issue create --title x -lagent-ready --body 'b'",
    "gh api repos/o/r/issues/42/labels -f labels[]=agent-ready",
    "gh api --method POST repos/o/r/issues/42/labels -f 'labels[]=pre-authorized:referee'",
    "gh api -X PUT repos/o/r/issues/42/labels -f labels[]=agent-ready",
    "cd /tmp && gh issue edit 42 --add-label agent-ready",
])
def test_applying_an_approval_label_is_denied(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "applying an approval label must be denied: %r" % command


def test_the_approval_deny_hands_back_the_sanctioned_path(tmp_path):
    """Forbidding alone would leave the session stuck holding work it believes is ready. The deny
    must name the label that actually puts the issue in front of the owner."""
    _worktree(tmp_path)
    reason = _bash("gh issue edit 42 --add-label agent-ready", tmp_path)
    low = reason.lower()
    assert "needs-owner" in low, "the deny must hand back the sanctioned label"
    assert "agent-ready" in low
    assert "owner" in low, "the deny must say whose verb approval is"


@pytest.mark.parametrize("command", [
    # REMOVAL is a safety act, not approval — never denied.
    "gh issue edit 42 --remove-label agent-ready",
    "gh pr edit 42 --remove-label pre-authorized:referee",
    "gh api -X DELETE repos/o/r/issues/42/labels/agent-ready",
    "gh api --method DELETE repos/o/r/issues/42/labels/pre-authorized:referee",
    # the METHOD belt on its own, both spellings: a DELETE naming the label in a FIELD rather than
    # in the path is still a removal. `-X` must be reached even though `--method` is tried first.
    "gh api -X DELETE repos/o/r/issues/42/labels -f labels[]=agent-ready",
    "gh api -XDELETE repos/o/r/issues/42/labels -f labels[]=agent-ready",
    # ...and ordinary labels of every shape.
    "gh issue edit 42 --add-label needs-owner",
    "gh issue edit 42 --add-label type:build,parked",
    "gh issue create --title x --label type:build,needs-owner "
    "--body '## Goal\nx\n\n## Loop metadata\ntouches: engine\n'",
    # reading labels, and creating a label in the repo's vocabulary, are not applying one
    "gh api repos/o/r/issues/42/labels",
    "gh label list",
    # the words as TEXT, inside something else
    "gh issue comment 7 --body 'this needs agent-ready from the owner'",
    "git commit -m 'never apply agent-ready'",
])
def test_ordinary_label_work_and_removal_stay_allowed(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "must NOT deny: %r" % command


def test_an_approval_label_held_in_a_variable_stands_the_matcher_down(tmp_path):
    # `--add-label "$LABEL"` — shlex leaves the literal `$LABEL`, which is a recipe, not the value.
    # Judging it would be a verdict on evidence we never read (the duty-3 rule, applied here).
    _worktree(tmp_path)
    assert _bash('gh issue edit 42 --add-label "$LABEL"', tmp_path) is None


def test_the_approval_deny_beats_the_issue_create_format_deny(tmp_path):
    """`gh issue create --label agent-ready` with a malformed body trips duty 3 as well. The
    approval line is the harder one — the session must be told it may not self-approve, not handed
    a format lecture that implies the command is fine once the body is fixed."""
    _worktree(tmp_path)
    reason = _bash("gh issue create --title x --label agent-ready --body '## Goal\nx\n'", tmp_path)
    assert reason and "needs-owner" in reason.lower()
    assert "MECHANICALLY INVALID" not in reason


# --------------------------- matcher C: out-of-band shipping ---------------------------

@pytest.mark.parametrize("command", [
    "gh pr merge 42",
    "gh pr merge --squash --admin 42",
    "gh pr merge --auto --squash",
    "cd /tmp && gh pr merge 42 --squash",
    "gh pr create --fill\ngh pr merge --squash",
    "/opt/homebrew/bin/gh pr merge 42",
    "sudo gh pr merge 42",
])
def test_gh_pr_merge_is_denied(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "self-merging must be denied: %r" % command


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin main:main",
    "git push origin HEAD:refs/heads/main",
    "git push -u origin main",
    "git push --delete origin main",
    "git push origin :main",
    "git -C /some/checkout push origin main",
    "git -c user.name=x push origin main",
    "cd /tmp && git push origin main",
    "git fetch origin\ngit push origin main",
])
def test_a_direct_push_to_the_dev_branch_is_denied(tmp_path, command):
    _worktree(tmp_path)                              # REPO_CFG carries no dev_branch -> "main"
    assert _bash(command, tmp_path), "a direct dev-branch push must be denied: %r" % command


def test_the_dev_branch_comes_from_config_not_from_a_hardcoded_name(tmp_path):
    _worktree(tmp_path, cfg={**REPO_CFG, "dev_branch": "develop"})
    assert _bash("git push origin develop", tmp_path), "the CONFIGURED dev branch must be denied"
    assert _bash("git push origin main", tmp_path) is None, \
        "a branch that is not this repo's mainline is an ordinary branch"


def test_without_a_config_the_dev_branch_dimension_stands_down(tmp_path):
    # We do not know this repo's mainline, so we do not guess one. The repo-INDEPENDENT halves of
    # the matcher (force-push, `gh pr merge`) still fire.
    _worktree(tmp_path, cfg=None)
    assert _bash("git push origin main", tmp_path) is None
    assert _bash("git push --force origin sl/i226-x", tmp_path)
    assert _bash("gh pr merge 42", tmp_path)


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git push --force origin sl/i226-pretooluse",
    "git push --force-with-lease origin sl/i226-pretooluse",
    "git push --force-with-lease=sl/i226-x origin sl/i226-x",
    "git push -f origin sl/i226-x",
    "git push origin +sl/i226-x",
    "git push origin +HEAD:sl/i226-x",
    "cd /tmp && git push --force origin sl/i226-x",
])
def test_any_force_push_is_denied_whatever_the_branch(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "a force-push must be denied: %r" % command


def test_the_shipping_deny_teaches_the_sanctioned_path(tmp_path):
    _worktree(tmp_path)
    for command in ("gh pr merge 42", "git push origin main", "git push --force origin sl/x"):
        low = _bash(command, tmp_path).lower()
        assert "gh pr create" in low, "the deny must name the PR the worker DOES open: %r" % command
        assert "gate" in low, "the deny must say who merges: %r" % command


def test_the_shipping_deny_teaches_a_pr_body_that_closes_the_issue(tmp_path):
    """(#404) A denied worker follows the next instruction literally, so the PR line it is handed
    must be one the gate will actually merge. A bare `gh pr create --fill` takes its body from the
    commit message, which need carry no closing keyword — and the gate now refuses a PR whose body
    would leave its issue open. The deny is the second place PR creation is taught (brief.py is the
    first); this is the drift #238 exists to catch, one site over."""
    _worktree(tmp_path)
    for command in ("gh pr merge 42", "git push origin main", "git push --force origin sl/x"):
        low = _bash(command, tmp_path).lower()
        assert "--body" in low, "the deny must teach a PR BODY, not a bare --fill: %r" % command
        assert "closes #" in low, "the deny must name the closing keyword: %r" % command


def test_the_force_push_deny_names_the_stale_review_pin_cost(tmp_path):
    """Bright line 6 is server-enforced on the dev branch only; on an `sl/*` branch the real cost is
    a PR stranded on a review verdict pinned to a commit that no longer exists (post-#154), which is
    stall-shaped waste. Say so, or the deny reads as arbitrary on a feature branch."""
    _worktree(tmp_path)
    low = _bash("git push --force origin sl/i226-x", tmp_path).lower()
    assert "review" in low and "pin" in low


@pytest.mark.parametrize("command", [
    "git push -u origin HEAD",
    "git push origin HEAD",
    "git push origin sl/i226-pretooluse-deny-wave",
    "git push origin HEAD:sl/i226-pretooluse-deny-wave",
    "git push",
    "git push origin",
    "git fetch origin main",
    "git rebase origin/main",
    "git merge origin/main",
    "git log origin/main..HEAD",
    "gh pr create --fill --body 'Closes #226'",
    "gh pr view 42 --json mergeable",
    "gh pr comment 42 --body 'ready to merge'",
    "gh pr list --state merged",
    # the words as TEXT, inside something else
    "git commit -m 'never gh pr merge, never --force'",
    "echo 'git push --force origin main'",
])
def test_ordinary_shipping_work_stays_allowed(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "must NOT deny: %r" % command


@pytest.mark.parametrize("command", [
    # ACCEPTED MISSES — the same deliberately-narrow posture duty 2 documents. The brief still
    # instructs against all three, and the runner's per-tick branch->PR reconcile still settles the
    # fact after; a miss costs the safety net, not the guard rail.
    "sh -c 'git push --force origin main'",          # the call sits behind a quote
    "bash -c \"gh pr merge 42\"",
    "eval 'git push --force'",
])
def test_known_bad_merge_misses_are_intentional(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "pinned accepted miss: %r" % command


@pytest.mark.parametrize("command", [
    # ACCEPTED FALSE DENIES, the same safe-direction trade duty 2 already makes. An unquoted newline
    # is read as the separator it is, so a heredoc BODY line reads as a command position. Erring
    # toward denying costs the session a rephrase; erring the other way costs a rewritten branch.
    "cat <<EOF\ngit push --force\nEOF",
    "cat <<EOF\ngh pr merge is what the gate does, not you\nEOF",
])
def test_known_bad_merge_false_denies_are_intentional(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is not None, "pinned accepted false deny: %r" % command


def test_a_quoted_refspec_is_still_an_ordinary_refspec(tmp_path):
    # Quoting is not hiding: shlex unquotes, so `'HEAD:main'` is read exactly like `HEAD:main`.
    _worktree(tmp_path)
    assert _bash("git push origin 'HEAD:main'", tmp_path)


# --- fresh-agent review (Codex, 2026-08-06): four reachable holes in the first cut ---

@pytest.mark.parametrize("command", [
    # P1-1. `gh`'s OWN global options sit BEFORE the subcommand, so `gh --repo x pr merge 42` hid
    # the verb from every matcher. Unlike duty 3 — where `--repo` stands the duty DOWN because
    # another repo has another CONTRACT — these three hazards are about this SESSION's conduct, not
    # the target repo's rules, so a `--repo` never buys permission.
    "gh --repo willprout/superlooper pr merge 42",
    "gh -R willprout/superlooper pr merge 42",
    "gh --repo willprout/superlooper issue edit 42 --add-label agent-ready",
    "gh -Rwillprout/superlooper pr edit 42 --add-label pre-authorized:referee",
    "gh --hostname github.com api repos/o/r/statuses/abc -f state=success",
])
def test_gh_global_options_do_not_hide_the_subcommand(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "a gh global flag must not hide the verb: %r" % command


@pytest.mark.parametrize("command", [
    # P1-2. `shlex.split` leaves UNSPACED punctuation glued to its neighbour (`/tmp;` is one token),
    # so the trailing-`; echo ok` and leading-`cd /tmp;` shapes — the most ordinary compound lines
    # a worker writes — walked straight past the command-position check.
    "cd /tmp; gh pr merge 42",
    "gh pr merge 42; echo ok",
    "true; git push origin main",
    "git push origin main; echo done",
    "cd /tmp&&gh pr merge 42",
    "git fetch||git push --force origin sl/x",
    "(gh pr merge 42)",
    "gh issue edit 42 --add-label agent-ready; echo ok",
])
def test_unspaced_shell_separators_do_not_hide_the_call(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "an unspaced separator must not hide the call: %r" % command


@pytest.mark.parametrize("command", [
    # P1-3. A dry run SHIPS NOTHING — it is a read-shaped call, and the module's whole posture is
    # that read-shaped calls pass. Denying one costs a worker the only safe way to check a refspec.
    "git push --dry-run origin main",
    "git push -n origin main",
    "git push origin main --dry-run",
    "git push --dry-run --force origin sl/i226-x",
])
def test_a_dry_run_push_ships_nothing_and_is_allowed(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "a dry run must not be denied: %r" % command


@pytest.mark.parametrize("command", [
    # Review round 2 (Codex, 2026-08-06). An ENV-ASSIGNMENT PREFIX is the ordinary way to run one
    # command with one variable set, and it left `gh`/`git` looking like an argument to it. Same for
    # a CONDITION position — duty 2 documents `if pkill …; then` as an accepted miss, but here the
    # deny is the cheap direction, so it is closed rather than accepted.
    "FOO=bar gh pr merge 42",
    "GIT_SSH_COMMAND='ssh -i k' git push origin main",
    "env FOO=bar gh issue edit 42 --add-label agent-ready",
    "if git push origin main; then echo ok; fi",
    "while ! git push --force origin sl/x; do sleep 1; done",
    "until gh pr merge 42; do sleep 1; done",
])
def test_assignment_prefixes_and_condition_positions_do_not_hide_the_call(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path), "must not be hidden by its position: %r" % command


def test_a_word_before_the_binary_is_still_not_a_command_position(tmp_path):
    # The walk-back must still END at something that is not a separator: these are TEXT, not calls.
    _worktree(tmp_path)
    for command in ("echo if git push --force origin main",
                    "echo env gh pr merge 42",
                    "grep -r 'FOO=bar gh pr merge' docs/"):
        assert _bash(command, tmp_path) is None, "must NOT deny: %r" % command


@pytest.mark.parametrize("command", [
    # Review round 2, P2. Both of these are READS, and reads pass. `--help` prints text; an explicit
    # `-X GET` is unambiguous evidence that nothing is being written (gh sends exactly the method it
    # is told to), so honoring it keeps the matcher strictly evidence-based.
    "gh pr merge --help",
    "gh issue edit --help",
    "gh api -X GET repos/o/r/statuses/abc",
    "gh api --method GET repos/o/r/statuses/abc",
])
def test_help_and_explicit_reads_are_not_writes(tmp_path, command):
    _worktree(tmp_path)
    assert _bash(command, tmp_path) is None, "a read must not be denied: %r" % command


def test_a_mirror_push_is_a_force_push(tmp_path):
    # P1-4. `--mirror` force-updates every ref and deletes the ones you no longer have — a force
    # push with no `--force` on the line and no refspec to inspect.
    _worktree(tmp_path)
    assert _bash("git push --mirror origin", tmp_path)


def test_an_exotic_refspec_is_an_accepted_miss(tmp_path):
    # `git push origin HEAD:refs/for/main` (gerrit-style) and a remote spelled as a URL are not
    # resolved. Pinned so a future tightening is a conscious change.
    _worktree(tmp_path)
    assert _bash("git push origin HEAD:refs/for/main", tmp_path) is None
    assert _bash("git push git@github.com:willprout/superlooper.git HEAD", tmp_path) is None


# --------------------------- the three duties hold everywhere the others do ---------------------

@pytest.mark.parametrize("env", [WORKER_ENV, DEBUGGER_ENV, ATTENDED_DEBUGGER_ENV, TRIAGE_ENV])
@pytest.mark.parametrize("command", ["gh api repos/o/r/statuses/abc -f state=success",
                                     "gh issue edit 4 --add-label agent-ready",
                                     "gh issue edit 4 --add-label pre-authorized:referee",
                                     "gh pr merge 42",
                                     "git push --force origin sl/x"])
def test_the_bad_merge_denies_hold_for_every_role_attended_or_not(tmp_path, env, command):
    # Attendance carves out duty 1 ONLY. The sl-debugger's own unattended contract already forbids
    # merging and force-pushing at EVERY authority tier, `full` included, so there is nothing to
    # carve out here either.
    _worktree(tmp_path, issue_id=env["SL_ISSUE_ID"])
    assert _bash(command, tmp_path, env=env)


@pytest.mark.parametrize("command", ["gh api repos/o/r/statuses/abc -f state=success",
                                     "gh issue edit 4 --add-label agent-ready",
                                     "gh pr merge 42",
                                     "git push --force origin sl/x"])
def test_no_bad_merge_deny_outside_a_loop_session(tmp_path, command):
    _worktree(tmp_path)
    assert wp.run(_pre("Bash", {"command": command}),
                  {"SL_ISSUE_ID": "", "SL_RUN_ROOT": str(tmp_path)}) is None
    assert _bash(command, tmp_path, env={**WORKER_ENV, "SL_AGENT": "codex"}) is None


def test_a_malformed_command_line_is_never_judged(tmp_path):
    _worktree(tmp_path)
    assert _bash("git push --force origin 'unbalanced", tmp_path) is None
    assert wp.run(_pre("Bash", {"command": None}), WORKER_ENV) is None


def test_a_broken_config_read_never_takes_the_shipping_matcher_down(tmp_path):
    d = tmp_path / "worktrees" / "i7" / ".superlooper"
    d.mkdir(parents=True)
    (d / "config.json").mkdir()                      # a DIRECTORY where the config should be
    assert _bash("git push origin main", tmp_path) is None      # dev branch unknown -> stands down
    assert _bash("gh pr merge 42", tmp_path)                    # repo-independent half still fires


# --------------------------- end to end, through the hook script ---------------------------

@pytest.mark.parametrize("command,needle", [
    ("gh api repos/o/r/statuses/abc -f state=success", "commit status"),
    ("gh issue edit 42 --add-label agent-ready", "needs-owner"),
    ("gh pr merge 42", "gh pr create"),
    ("git push --force origin sl/i226-x", "force"),
    ("git push origin main", "main"),
])
def test_the_hook_script_denies_each_bad_merge_path_end_to_end(tmp_path, command, needle):
    (tmp_path / "worktrees" / "i7" / ".superlooper").mkdir(parents=True)
    (tmp_path / "worktrees" / "i7" / ".superlooper" / "config.json").write_text(json.dumps(REPO_CFG))
    r = _run_hook(tmp_path, _pre("Bash", {"command": command}))
    assert r.returncode == 0, r.stderr
    d = _decision(r.stdout)
    assert d and d["hookSpecificOutput"]["permissionDecision"] == "deny", command
    assert needle in d["hookSpecificOutput"]["permissionDecisionReason"], command


def test_the_hook_script_still_lets_the_worker_ship_the_sanctioned_way(tmp_path):
    (tmp_path / "worktrees" / "i7" / ".superlooper").mkdir(parents=True)
    (tmp_path / "worktrees" / "i7" / ".superlooper" / "config.json").write_text(json.dumps(REPO_CFG))
    for command in ("git push -u origin HEAD", "gh pr create --fill --body 'Closes #226'",
                    "gh pr comment 42 --body '<!-- superlooper-review sha=abc --> LGTM'"):
        r = _run_hook(tmp_path, _pre("Bash", {"command": command}))
        assert r.returncode == 0, r.stderr
        assert _decision(r.stdout) is None, "the sanctioned path must be untouched: %r" % command
