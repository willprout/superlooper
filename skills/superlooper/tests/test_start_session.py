"""bin/start-session.sh — the agent-specific launcher that turns SL_MODEL/SL_EFFORT into the
`claude` command line (the ONE place the Claude-specific flags live — agent-boundary rule). These
tests drive the script directly with an arg-recording stub `claude` on PATH (no real claude —
kickoff rule), pinning exactly which flags reach the CLI:

  * --model is passed iff SL_MODEL is non-empty (existing behavior, kept under test);
  * --effort is passed iff SL_EFFORT is non-empty — NEVER a default (owner ruling 2026-07-07);
  * a bracketed model (opus[1m]) survives verbatim through the launch stack.
"""
import os
import shutil
import stat
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
START = os.path.join(REPO_ROOT, "skill", "bin", "start-session.sh")

# records every argv element on its own line, then exits (a real worker would idle at the prompt).
STUB_AGENT = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$SL_TEST_ARGS"\nexit 0\n'

# Scrubbed from every child env below for a sharper reason than tidiness (#298): a worker pane
# running this suite has its OWN runner-minted session id live in the environment (start-session.sh
# reads it from exactly there), so without this the "no id was minted" cases would inherit the
# test-runner session's id and quietly assert nothing.
_SESSION_ENV = ("SL_SESSION_ID", "SL_RESUME")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_start(tmp_path, *, agent="claude", model=None, effort=None, extra_env=None):
    """Run start-session.sh i1 with a stub agent; return its recorded argv (list of tokens).
    model/effort default to unset (env var absent); pass "" to exercise the empty-string path."""
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    _x(str(stubdir / "claude"), STUB_AGENT)
    _x(str(stubdir / "codex"), STUB_AGENT)
    args_file = tmp_path / f"{agent}_args"
    # start from a copy that never leaks the parent's SL_MODEL/SL_EFFORT into the child.
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_MODEL", "SL_EFFORT", "SL_CODEX_DANGEROUS_BYPASS",
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN",
                        _SESSION_ENV[0], _SESSION_ENV[1])}
    env.update({
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "SL_RUN_ROOT": str(run_root),
        "SL_TEST_ARGS": str(args_file),
        "SL_AGENT": agent,
    })
    if model is not None:
        env["SL_MODEL"] = model
    if effort is not None:
        env["SL_EFFORT"] = effort
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([START, "i1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"start-session.sh failed rc={r.returncode}\nSTDERR:\n{r.stderr}"
    return args_file.read_text().splitlines()


def _flag_value(argv, flag):
    """The token following `flag` in argv, or None if the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_effort_flag_passed_when_labeled(tmp_path):
    argv = _run_start(tmp_path, model="fable", effort="high")
    assert _flag_value(argv, "--effort") == "high"
    assert _flag_value(argv, "--model") == "fable"


def test_no_effort_flag_when_effort_unset(tmp_path):
    argv = _run_start(tmp_path, model="opus")
    assert "--effort" not in argv                       # never a default effort
    assert _flag_value(argv, "--model") == "opus"


def test_no_effort_flag_when_effort_empty(tmp_path):
    # the runner sends SL_EFFORT="" on the default path — that must NOT become `--effort ""`.
    argv = _run_start(tmp_path, model="opus", effort="")
    assert "--effort" not in argv


def test_no_model_flag_when_model_empty(tmp_path):
    # existing behavior kept under test: empty SL_MODEL omits --model (never `--model ""`),
    # and an effort label still applies on its own.
    argv = _run_start(tmp_path, model="", effort="max")
    assert "--model" not in argv
    assert _flag_value(argv, "--effort") == "max"


def test_bracketed_model_survives_verbatim(tmp_path):
    argv = _run_start(tmp_path, model="opus[1m]")
    assert _flag_value(argv, "--model") == "opus[1m]"


def test_sonnet_model_reaches_the_claude_cli(tmp_path):
    # issue #134: `sonnet` is a bare alias the claude CLI accepts on --model (its own --help lists
    # 'fable', 'opus', 'sonnet' as the latest-model aliases), so the seeded `model:sonnet` label
    # needs no mapping — the existing pass-through carries the value verbatim to the CLI.
    argv = _run_start(tmp_path, model="sonnet")
    assert _flag_value(argv, "--model") == "sonnet"


def test_codex_default_uses_interactive_tui_with_no_model_or_effort(tmp_path):
    argv = _run_start(tmp_path, agent="codex")
    assert argv[0] == "--no-alt-screen"
    assert "-C" in argv
    assert _flag_value(argv, "-C").endswith("/run")
    assert "-m" not in argv
    assert "-c" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--dangerously-bypass-hook-trust" in argv
    assert argv[-1] == "do the thing"


def test_codex_passes_explicit_model_and_reasoning_effort(tmp_path):
    argv = _run_start(tmp_path, agent="codex", model="gpt-5.5", effort="high")
    assert _flag_value(argv, "-m") == "gpt-5.5"
    assert _flag_value(argv, "-c") == 'model_reasoning_effort="high"'


def test_codex_dangerous_bypass_is_env_controlled(tmp_path):
    argv = _run_start(tmp_path, agent="codex",
                      extra_env={"SL_CODEX_DANGEROUS_BYPASS": "1",
                                 "SL_CODEX_BYPASS_HOOK_TRUST": "0",
                                 "SL_CODEX_NO_ALT_SCREEN": "0"})
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--dangerously-bypass-hook-trust" not in argv
    assert "--no-alt-screen" not in argv


# --------------------------- launch-stderr capture (issue #40) ---------------------------
# A launch that dies immediately (bad --model, a renamed/dropped CLI flag) writes its real reason
# to STDERR and vanishes with the doomed cmux tab; the runner then only sees "relaunched N times".
# start-session.sh (the agent-boundary launcher) must capture a BOUNDED tail of the agent's stderr
# to a well-known file the agent-agnostic park memo can read: state/launch_stderr/<id>.

def _run_start_capture(tmp_path, stub_body, *, agent="claude", model=None, extra_env=None):
    """Run start-session.sh i1 with a custom stub agent; return (tail_path, args_path). tail_path is
    the state/launch_stderr/i1 file (may not exist for a totally quiet launch)."""
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    _x(str(stubdir / agent), stub_body)
    args_file = tmp_path / f"{agent}_args"
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_MODEL", "SL_EFFORT", "SL_CODEX_DANGEROUS_BYPASS",
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN",
                        _SESSION_ENV[0], _SESSION_ENV[1])}
    env.update({
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "SL_RUN_ROOT": str(run_root),
        "SL_TEST_ARGS": str(args_file),
        "SL_AGENT": agent,
    })
    if model is not None:
        env["SL_MODEL"] = model
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([START, "i1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    # start-session.sh itself always exits 0 (it records the agent's rc into the exited marker and
    # returns to the shell) — a nonzero AGENT must not make the launcher fail.
    assert r.returncode == 0, f"start-session.sh failed rc={r.returncode}\nSTDERR:\n{r.stderr}"
    return run_root / "state" / "launch_stderr" / "i1", args_file


DYING_STUB = ('#!/usr/bin/env bash\n'
              'echo "error: unknown option \'--effort\'" >&2\n'
              'echo "run claude --help for usage" >&2\n'
              'exit 3\n')


def test_launch_stderr_tail_is_captured_when_the_agent_dies_at_launch(tmp_path):
    tail_path, _ = _run_start_capture(tmp_path, DYING_STUB)
    assert tail_path.exists(), "start-session.sh must capture the failed launch's stderr tail"
    body = tail_path.read_text()
    assert "unknown option '--effort'" in body
    assert "run claude --help for usage" in body


def test_launch_stderr_tail_is_bounded(tmp_path):
    # A chatty/looping launch must not grow the captured tail without bound; the MOST RECENT lines
    # (which carry the actual error) are what survive.
    noisy = ('#!/usr/bin/env bash\n'
             'for i in $(seq 1 5000); do echo "noise line $i" >&2; done\n'
             'echo "FINAL: the real error is here at the tail" >&2\n'
             'exit 3\n')
    tail_path, _ = _run_start_capture(tmp_path, noisy,
                                      extra_env={"SL_LAUNCH_STDERR_MAX_BYTES": "512"})
    assert tail_path.exists(), "start-session.sh must capture the failed launch's stderr tail"
    body = tail_path.read_text()
    assert len(body) <= 512
    assert "the real error is here at the tail" in body   # the tail, not the head, is kept


def test_healthy_launch_records_argv_and_captures_an_empty_tail(tmp_path):
    # Existing behavior unchanged: a healthy (exit 0, quiet) launch still records its argv through
    # the capture wrapper, and surfaces no error tail.
    tail_path, args_file = _run_start_capture(tmp_path, STUB_AGENT, model="opus")
    argv = args_file.read_text().splitlines()
    assert _flag_value(argv, "--model") == "opus"        # argv flows through the capture wrapper
    assert (not tail_path.exists()) or tail_path.read_text().strip() == ""


def test_a_stale_tail_is_cleared_on_a_brief_missing_relaunch(tmp_path):
    # Review P1-1: the per-launch clear must run BEFORE the brief-missing early-exit (which itself
    # writes an exited marker), so a prior FAILED launch's stderr can never mis-attribute to a later
    # "no brief" park of the same id.
    tail_path, _ = _run_start_capture(tmp_path, DYING_STUB)     # first launch dies, writes a tail
    assert "unknown option" in tail_path.read_text()
    run_root = tail_path.parent.parent.parent
    (run_root / "briefs" / "i1.md").unlink()                    # brief vanishes before the relaunch
    stubdir = run_root.parent / "stub"
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_MODEL", "SL_EFFORT", "SL_CODEX_DANGEROUS_BYPASS",
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN",
                        _SESSION_ENV[0], _SESSION_ENV[1])}
    env.update({"PATH": f"{stubdir}:{os.environ['PATH']}", "HOME": str(run_root.parent / "home"),
                "SL_RUN_ROOT": str(run_root), "SL_TEST_ARGS": str(run_root.parent / "unused"),
                "SL_AGENT": "claude"})
    r = subprocess.run([START, "i1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1                                    # the brief-missing early-exit fired
    assert (not tail_path.exists()) or tail_path.read_text().strip() == ""   # stale tail was cleared


def test_a_stale_tail_does_not_bleed_into_a_later_healthy_launch(tmp_path):
    # start-session.sh clears the tail at the start of every launch, so a prior FAILED launch's
    # error can never mis-attribute to a fresh (healthy) relaunch of the same id.
    tail_path, _ = _run_start_capture(tmp_path, DYING_STUB)
    assert "unknown option" in tail_path.read_text()
    # relaunch the SAME id in the SAME run root with a healthy agent:
    run_root = tail_path.parent.parent.parent
    stubdir = run_root.parent / "stub"
    _x(str(stubdir / "claude"), STUB_AGENT)
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_MODEL", "SL_EFFORT", "SL_CODEX_DANGEROUS_BYPASS",
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN",
                        _SESSION_ENV[0], _SESSION_ENV[1])}
    env.update({"PATH": f"{stubdir}:{os.environ['PATH']}", "HOME": str(run_root.parent / "home"),
                "SL_RUN_ROOT": str(run_root), "SL_TEST_ARGS": str(run_root.parent / "claude_args2"),
                "SL_AGENT": "claude"})
    r = subprocess.run([START, "i1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (not tail_path.exists()) or tail_path.read_text().strip() == ""


# --------------------------- the PreToolUse deny's env reaches the hook ---------------------------

# Issue #185's carve-out is only as real as the environment that carries it: the launcher names
# SL_ATTENDED in the command it drops into the tab, and the hook — a GRANDCHILD of that command
# (start-session.sh -> claude -> hook) — must actually see it. Everything else about #185 is unit
# tested; this is the one link that is pure process inheritance, so it is pinned end to end with a
# stub agent that asks the REAL hook script the same question Claude would.
_HOOK = os.path.join(REPO_ROOT, "skill", "bin", "pretooluse-hook.sh")

HOOK_PROBE_AGENT = """#!/usr/bin/env bash
# stands in for `claude`: runs the real PreToolUse hook and records what it decided.
payload='{"hook_event_name":"PreToolUse","tool_name":"AskUserQuestion","tool_input":{}}'
out="$(printf '%s' "$payload" | bash "$SL_TEST_HOOK")"
{ printf 'SL_ATTENDED=[%s]\\n' "${SL_ATTENDED-<unset>}"
  if [ -z "$out" ]; then printf 'DECISION=allow\\n'; else printf 'DECISION=deny\\n'; fi
} > "$SL_TEST_ARGS"
exit 0
"""


def _probe_hook(tmp_path, issue_id, attended=None):
    """Run start-session.sh for `issue_id` with a stub agent that consults the real hook; return
    its recorded lines."""
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "state").mkdir()
    (run_root / "briefs" / ("%s.md" % issue_id)).write_text("the brief")
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    _x(str(stubdir / "claude"), HOOK_PROBE_AGENT)
    args_file = tmp_path / "probe"
    env = {k: v for k, v in os.environ.items() if k != "SL_ATTENDED"}
    env.update({"PATH": f"{stubdir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
                "SL_RUN_ROOT": str(run_root), "SL_TEST_ARGS": str(args_file),
                "SL_TEST_HOOK": _HOOK, "SL_AGENT": "claude"})
    if attended is not None:
        env["SL_ATTENDED"] = attended
    r = subprocess.run([START, issue_id], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return args_file.read_text().splitlines()


# ---- session identity: --session-id at spawn, --resume on revive (issue #298) ----
#
# The launch stack used NEITHER flag before #298, so any interruption — a killed pane, a crashed
# host, a closed lid — ended the flight's conversation for good. These pin the two halves of the
# fix at the ONE place the Claude-specific flags may live (agent-boundary rule): a runner-minted id
# is ASSIGNED at spawn, and a revive re-enters that same id instead of minting a new conversation.


def test_session_id_is_passed_when_the_runner_minted_one(tmp_path):
    argv = _run_start(tmp_path, extra_env={"SL_SESSION_ID": "d1e1e1e1-0000-4000-8000-000000000001"})
    assert _flag_value(argv, "--session-id") == "d1e1e1e1-0000-4000-8000-000000000001"
    assert "--resume" not in argv, "a FRESH launch assigns an id; it does not resume one"


def test_no_session_flag_when_no_id_was_minted(tmp_path):
    # The pre-#298 shape must survive untouched: an unset id means the session launches exactly as
    # it always did, so this change can never break a caller that has not adopted it yet.
    assert "--session-id" not in _run_start(tmp_path)


def test_no_session_flag_when_the_minted_id_is_empty(tmp_path):
    # Empty must never become `--session-id ""` — claude rejects a non-UUID and the launch would
    # die with the tab, exactly like the `--model ""` case this file already pins.
    assert "--session-id" not in _run_start(tmp_path, extra_env={"SL_SESSION_ID": ""})


def test_resume_re_enters_the_recorded_session_instead_of_minting_one(tmp_path):
    argv = _run_start(tmp_path, extra_env={"SL_SESSION_ID": "d1e1e1e1-0000-4000-8000-000000000002",
                                           "SL_RESUME": "1"})
    assert _flag_value(argv, "--resume") == "d1e1e1e1-0000-4000-8000-000000000002"
    assert "--session-id" not in argv, "--session-id on an EXISTING id is an error, not a resume"
    # the brief is still the opening message — on a resume that brief IS the re-orientation
    # preamble, so it must reach the CLI as the first thing the revived session reads.
    assert argv[-1] == "do the thing"


def test_resume_without_an_id_cannot_open_the_interactive_picker(tmp_path):
    # `--resume` takes an OPTIONAL value: a bare `--resume` opens claude's interactive session
    # PICKER and the unattended launch would sit there forever. No id => no flag, ever.
    argv = _run_start(tmp_path, extra_env={"SL_RESUME": "1"})
    assert "--resume" not in argv and "--session-id" not in argv


def test_the_session_id_is_claude_specific_and_never_reaches_codex(tmp_path):
    # Agent boundary: `--session-id`/`--resume` are Claude Code's spelling. Codex has its own
    # `codex resume` and would abort on an unknown flag — the id must not leak into its argv.
    argv = _run_start(tmp_path, agent="codex",
                      extra_env={"SL_SESSION_ID": "d1e1e1e1-0000-4000-8000-000000000003"})
    assert "--session-id" not in argv and "--resume" not in argv


def test_the_exit_hint_names_the_actual_session_to_resume(tmp_path):
    # The pane's parting line is a human's recovery handle. A bare `claude --resume` drops the
    # operator into the picker; naming the id is the difference between a hint and an instruction.
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    _x(str(stubdir / "claude"), STUB_AGENT)
    env = {k: v for k, v in os.environ.items() if k not in ("SL_MODEL", "SL_EFFORT")}
    env.update({"PATH": f"{stubdir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
                "SL_RUN_ROOT": str(run_root), "SL_TEST_ARGS": str(tmp_path / "a"),
                "SL_AGENT": "claude",
                "SL_SESSION_ID": "d1e1e1e1-0000-4000-8000-000000000004"})
    r = subprocess.run([START, "i1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "claude --resume d1e1e1e1-0000-4000-8000-000000000004" in r.stdout


def test_the_attended_flag_survives_into_the_hook_the_agent_spawns(tmp_path):
    # The owner tap's launch: SL_ATTENDED=1 must arrive intact two processes down, or the deny
    # would tell a session with a person at the keyboard that nobody is there.
    assert _probe_hook(tmp_path, "d7", attended="1") == ["SL_ATTENDED=[1]", "DECISION=allow"]


def test_an_unattended_launch_reaches_the_hook_as_unattended(tmp_path):
    # The watchdog's launch names the flag EMPTY; the deny must fire for the debugger's own protocol.
    assert _probe_hook(tmp_path, "d7", attended="") == ["SL_ATTENDED=[]", "DECISION=deny"]


def test_a_worker_launch_is_denied_even_with_an_ambient_attended_flag(tmp_path):
    # Belt and suspenders: the runner pins SL_ATTENDED="" for workers, AND the hook ignores the flag
    # for a worker id. This drives the second half — an ambient export that slipped past the first.
    assert _probe_hook(tmp_path, "i9", attended="1") == ["SL_ATTENDED=[1]", "DECISION=deny"]
