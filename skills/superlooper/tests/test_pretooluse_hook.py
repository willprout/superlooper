"""Issues #156 / #185 / #225 — the Claude PreToolUse deny hook for UNATTENDED loop sessions.

Three of the costliest worker-instruction-drift incidents are made mechanically impossible here
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
    path launch-session.sh actually creates, so the hook can find the repo's contract from the
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


@pytest.mark.parametrize("flag", ["--body-file notes.md", "-F notes.md", "--editor", "--web",
                                  "--template bug.md"])
def test_a_body_from_somewhere_else_stands_the_body_dimension_down(tmp_path, flag):
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build %s" % flag, tmp_path) is None


def test_no_body_flag_at_all_stands_the_body_dimension_down(tmp_path):
    # Without --body gh prompts or errors; either way there is no text to judge.
    _worktree(tmp_path)
    assert _create("gh issue create --title x --label type:build", tmp_path) is None


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

@pytest.mark.parametrize("env", [WORKER_ENV, DEBUGGER_ENV, ATTENDED_DEBUGGER_ENV])
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
