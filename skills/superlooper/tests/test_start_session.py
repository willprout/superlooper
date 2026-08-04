"""bin/start-session.sh — the agent-specific launcher that turns SL_MODEL/SL_EFFORT into the
`claude` command line (the ONE place the Claude-specific flags live — agent-boundary rule). These
tests drive the script directly with an arg-recording stub `claude` on PATH (no real claude —
kickoff rule), pinning exactly which flags reach the CLI:

  * --model is passed iff SL_MODEL is non-empty (existing behavior, kept under test);
  * --effort is passed iff SL_EFFORT is non-empty — NEVER a default (owner ruling 2026-07-07);
  * a bracketed model (opus[1m]) survives verbatim through the launch stack.
"""
import os
import re
import shutil
import stat
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
START = os.path.join(REPO_ROOT, "skill", "bin", "start-session.sh")

# Records every argv element on its own line, then exits (a real worker would idle at the prompt).
# It ALSO dumps the environment it was actually handed when SL_TEST_ENV names a file (issue #301):
# what the launcher `unset` in its own shell is unobservable from outside, but what the spawned
# agent RECEIVES is exactly what would have been billed to an API key or stripped of its transcript.
# That file is the only honest oracle for a scrub.
# It also records its own $0 when SL_TEST_ARGV0 names a file (issue #303): WHICH claude binary the
# launcher resolved is invisible in argv, and it is the whole subject of the binary-pin ladder.
STUB_AGENT = ('#!/usr/bin/env bash\n'
              'printf "%s\\n" "$@" > "$SL_TEST_ARGS"\n'
              '[ -n "${SL_TEST_ARGV0:-}" ] && printf "%s\\n" "$0" > "$SL_TEST_ARGV0"\n'
              '[ -n "${SL_TEST_ENV:-}" ] && env > "$SL_TEST_ENV"\n'
              'exit 0\n')

# Stub `gh` for the positive auth assert (issue #299): answers `api user --jq .login` with
# $STUB_GH_LOGIN, or refuses like a logged-out CLI when that is "DEAD". No test may reach the real
# gh (CLAUDE.md ratchet), and start-session.sh runs the probe on EVERY launch, so every case here
# needs one.
#
# It also reproduces the 2026-07-29 spike's XDG_CONFIG_HOME landmine faithfully (issue #301): the
# real `gh` honours XDG_CONFIG_HOME for its config dir, so pointed anywhere else it finds no
# hosts.yml and answers, confidently, as nobody. That makes the scrub's ORDERING testable — the env
# floor must run BEFORE the auth probe, or the loop diagnoses a poisoned env as dead credentials.
STUB_GH = ('#!/usr/bin/env bash\n'
           'set -u\n'
           'login="${STUB_GH_LOGIN:-loopbot}"\n'
           'if [ -n "${XDG_CONFIG_HOME:-}" ]; then login="DEAD"; fi\n'
           'if [ "$login" = "DEAD" ]; then\n'
           '  echo "gh: To get started with GitHub CLI, please run:  gh auth login" >&2\n'
           '  exit 4\n'
           'fi\n'
           'printf "%s\\n" "$login"\n'
           'exit 0\n')

# Scrubbed from every child env below for a sharper reason than tidiness (#298): a worker pane
# running this suite has its OWN runner-minted session id live in the environment (start-session.sh
# reads it from exactly there), so without this the "no id was minted" cases would inherit the
# test-runner session's id and quietly assert nothing.
_SESSION_ENV = ("SL_SESSION_ID", "SL_RESUME")

# ---- the launch-floor env denylist (issue #301) -------------------------------------------------
# Every name here is a REALIZED silent-lie vector, not a hypothetical:
#   * ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL flip a session from Max-subscription to API billing
#     with no error and no signal (tool-dive claim c1, two codebases) — and a LIVE key was found in
#     the owner's own ~/.zshrc:5 on 2026-07-30 (docs/SPIKES-2026-07-30-supervised.md §0.3);
#   * inherited CLAUDE_CODE_* / CLAUDECODE turn transcript saving OFF, which silently breaks
#     `--resume` and so silently voids the whole resurrection floor (#298 depends on this);
#   * XDG_CONFIG_HOME de-authenticates `gh` while it keeps answering confidently.
# (All from docs/SPIKES-2026-07-29-results.md.) Values are recognisable so a leak is unmistakable
# in a failure message.
POISON_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-MUST-NEVER-REACH-A-SESSION",
    "ANTHROPIC_BASE_URL": "https://not-anthropic.invalid/v1",
    "ANTHROPIC_AUTH_TOKEN": "tok-MUST-NEVER-REACH-A-SESSION",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "00000000-0000-4000-8000-000000000000",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_PID": "424242",
    "CLAUDE_EFFORT": "xhigh",
    "XDG_CONFIG_HOME": "/nowhere/xdg",
}

_POISON_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_POISON_EXACT = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "XDG_CONFIG_HOME")


def _is_poison(name):
    return name.startswith(_POISON_PREFIXES) or name in _POISON_EXACT


_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def _recorded_env_names(path):
    """The variable NAMES the stub agent was actually handed. Names only: a value can contain
    newlines and `=`, so only a line that OPENS with a valid identifier followed by `=` names a
    variable."""
    return [m.group(1) for m in (_NAME_RE.match(ln) for ln in path.read_text().splitlines()) if m]


def _recorded_env(path):
    out = {}
    for line in path.read_text().splitlines():
        m = _NAME_RE.match(line)
        if m:
            out[m.group(1)] = line[len(m.group(1)) + 1:]
    return out


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(autouse=True)
def _no_ambient_claude_pin(monkeypatch, _never_reach_real_claude):
    """This file's subject is the binary LADDER (issue #303), so it must run with no pin unless a
    case sets one — including conftest's guaranteed-absent `_never_reach_real_claude` default, which
    would otherwise make every launch here refuse a pin it never asked for (rung 1 fails closed by
    design). Declared AFTER that fixture so it undoes it rather than racing it.

    Safe to drop here, unlike anywhere else in the suite: every helper below launches with
    HOME under a tmp dir and a stub `claude` first on PATH, so both remaining rungs land on a stub
    and no case can reach the machine's real Claude Code."""
    monkeypatch.delenv("SL_CLAUDE", raising=False)


@pytest.fixture(autouse=True)
def _stub_gh(tmp_path_factory, monkeypatch):
    """start-session.sh now gates EVERY launch on the positive gh-auth assert (#299), so every case
    in this file needs a `gh` that answers and a login to answer as — and none may reach the real
    gh (CLAUDE.md ratchet; conftest points SL_GH at an absent path by default). Set the healthy
    defaults here rather than in each helper's env dict: they are all built from os.environ, so
    they inherit it. The auth cases below override STUB_GH_LOGIN / SL_EXPECT_GH_LOGIN in-body."""
    gh = tmp_path_factory.mktemp("ghstub") / "gh"
    _x(str(gh), STUB_GH)
    monkeypatch.setenv("SL_GH", str(gh))
    monkeypatch.setenv("STUB_GH_LOGIN", "loopbot")
    monkeypatch.setenv("SL_EXPECT_GH_LOGIN", "loopbot")
    # An ambient XDG_CONFIG_HOME would de-auth the stub gh in EVERY case above (it reproduces the
    # spike's landmine), so pin it absent here and let the one ordering case set it in-body.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return gh


def _start(tmp_path, *, agent="claude", model=None, effort=None, extra_env=None,
           resume_brief=None, standalone=False):
    """Run start-session.sh i1 with a stub agent; return (CompletedProcess, run_root, args_file).
    model/effort default to unset (env var absent); pass "" to exercise the empty-string path.
    `resume_brief` seeds briefs/i1.resume.md — a resume launch REQUIRES it (the selection fails
    closed rather than substituting the lane's own brief).
    `standalone` puts a stub at $HOME/.local/bin/claude — the native install the binary ladder pins
    ahead of PATH (issue #303); without it the ladder falls through to the PATH stub, which is what
    every pre-#303 case in this file relies on."""
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")
    if resume_brief is not None:
        (run_root / "briefs" / "i1.resume.md").write_text(resume_brief)
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    _x(str(stubdir / "claude"), STUB_AGENT)
    _x(str(stubdir / "codex"), STUB_AGENT)
    if standalone:
        native = tmp_path / "home" / ".local" / "bin"
        native.mkdir(parents=True, exist_ok=True)
        _x(str(native / "claude"), STUB_AGENT)
    args_file = tmp_path / f"{agent}_args"
    # start from a copy that never leaks the parent's SL_MODEL/SL_EFFORT into the child, and never
    # its POISON either (#301): this suite very often runs inside a real Claude Code session, whose
    # CLAUDE_CODE_*/CLAUDECODE/CLAUDE_PID/CLAUDE_EFFORT are ambient in os.environ. Left in, the
    # baseline "a clean env launches normally" case would silently be a scrub case instead, and the
    # scrub cases could not tell their own injected poison from the harness's.
    # (SL_CLAUDE, the #303 binary pin, is cleared for the whole file by _no_ambient_claude_pin
    # above — every helper here builds from os.environ, so one fixture covers them all. The pin
    # cases below set it in extra_env.)
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_MODEL", "SL_EFFORT", "SL_CODEX_DANGEROUS_BYPASS",
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN",
                        _SESSION_ENV[0], _SESSION_ENV[1])
           and not _is_poison(k)}
    env.update({
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "SL_RUN_ROOT": str(run_root),
        "SL_TEST_ARGS": str(args_file),
        "SL_TEST_ARGV0": str(tmp_path / f"{agent}_argv0"),
        "SL_TEST_ENV": str(tmp_path / f"{agent}_env"),
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
    return r, run_root, args_file


def _run_start(tmp_path, **kw):
    """The flag-shape helper: a SUCCESSFUL start, returning the agent's recorded argv."""
    r, _run_root, args_file = _start(tmp_path, **kw)
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
    argv = _run_start(tmp_path, resume_brief="RE-ORIENTATION FIRST",
                      extra_env={"SL_SESSION_ID": "d1e1e1e1-0000-4000-8000-000000000002",
                                 "SL_RESUME": "1"})
    assert _flag_value(argv, "--resume") == "d1e1e1e1-0000-4000-8000-000000000002"
    assert "--session-id" not in argv, "--session-id on an EXISTING id is an error, not a resume"
    # A resume opens on the PREAMBLE, never on the lane's own brief — the ordering the DoD names.
    assert argv[-1] == "RE-ORIENTATION FIRST"


def test_resume_without_an_id_cannot_open_the_interactive_picker(tmp_path):
    # `--resume` takes an OPTIONAL value: a bare `--resume` opens claude's interactive session
    # PICKER and the unattended launch would sit there forever. No id => no flag, ever.
    argv = _run_start(tmp_path, resume_brief="preamble", extra_env={"SL_RESUME": "1"})
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


# ---- the positive gh-auth assert, in the worker's OWN env (issue #299) --------------------------
# start-session.sh is the ONE place that runs inside the spawned session's environment and worktree
# — the fresh tab inherits nothing from the runner, which is exactly why an env-level auth death
# (the 2026-07-29 XDG_CONFIG_HOME spike) is invisible to any check the launcher makes about itself.
AUTH_RC = 4


def _authfail_markers(run_root):
    d = run_root / "state" / "authfail"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_dead_gh_auth_refuses_before_the_agent_and_leaves_a_named_marker(tmp_path):
    r, run_root, args_file = _start(tmp_path, extra_env={"STUB_GH_LOGIN": "DEAD",
                                                         "SL_START_TOKEN": "TOK"})
    assert r.returncode == AUTH_RC, f"expected rc={AUTH_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists(), "the agent must never be started with dead gh auth"
    assert "gh auth" in r.stderr.lower(), f"the refusal must NAME auth death: {r.stderr!r}"
    # The launcher is waiting on the delivery sentinel; the auth marker is what tells it the tab is
    # dead on arrival AND why, so it can tear down at once instead of blaming the shim 30s later.
    assert _authfail_markers(run_root) == ["i1.TOK"], "a per-launch auth marker must be left"
    assert "gh" in (run_root / "state" / "authfail" / "i1.TOK").read_text().lower()
    assert not (run_root / "state" / "started" / "i1.TOK").exists(), \
        "a refused session must NOT stamp the delivery sentinel — it never became a worker"


def test_a_stranger_login_is_refused_even_though_gh_answers_happily(tmp_path):
    # POSITIVE: absence of an error is not auth. gh exits 0 and answers a real login here — just
    # not the one this loop runs as.
    r, run_root, args_file = _start(tmp_path, extra_env={"STUB_GH_LOGIN": "stranger",
                                                         "SL_START_TOKEN": "TOK"})
    assert r.returncode == AUTH_RC, f"expected rc={AUTH_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists(), "the agent must never run as an unexpected account"
    assert "stranger" in r.stderr and "loopbot" in r.stderr, \
        f"name the login found AND the one expected: {r.stderr!r}"


def test_a_missing_expectation_fails_closed(tmp_path):
    # The launcher ALWAYS hands the expectation down. If it ever stops, there is nothing to assert
    # against — and a floor that silently disables itself when its input goes missing is not a
    # floor. Refuse rather than degrade to "some gh answered".
    r, run_root, args_file = _start(tmp_path, extra_env={"SL_EXPECT_GH_LOGIN": ""})
    assert r.returncode == AUTH_RC, f"expected rc={AUTH_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists(), "no expectation must never mean 'start anyway'"


def test_a_refused_launch_writes_no_exited_marker(tmp_path):
    # The exited marker means "a worker WAS running and its process is gone" — the runner recovers
    # from it by RELAUNCHING. No worker ever ran here, and a relaunch into the same dead auth would
    # just burn the lane again; the launcher's rc is the whole signal.
    r, run_root, _args = _start(tmp_path, extra_env={"STUB_GH_LOGIN": "DEAD"})
    assert r.returncode == AUTH_RC
    assert not (run_root / "state" / "exited" / "i1").exists()


def test_healthy_gh_auth_proceeds_to_the_agent(tmp_path):
    argv = _run_start(tmp_path, model="fable")
    assert argv[-1] == "do the thing", "an authed env must reach the agent with its brief"


def test_the_assert_also_gates_the_codex_agent(tmp_path):
    # gh auth is agent-independent: the assert sits ABOVE the agent branch, so a codex repo is
    # covered by construction rather than by a second copy of the check.
    r, _run_root, args_file = _start(tmp_path, agent="codex",
                                     extra_env={"STUB_GH_LOGIN": "DEAD"})
    assert r.returncode == AUTH_RC, f"expected rc={AUTH_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists()


# ---- the env scrub + post-scrub assert, in the worker's OWN env (issue #301) --------------------
# Sessions inherit the full parent env. One exported ANTHROPIC_API_KEY silently flips every worker
# from Max-subscription to API billing — no error, no signal, just a bill — and a LIVE key sat in
# the owner's ~/.zshrc:5 until 2026-07-30. The same inheritance turns transcript saving off
# (CLAUDE_CODE_*, which silently voids `--resume` and so #298's whole resurrection floor) and
# de-authenticates `gh` (XDG_CONFIG_HOME).
#
# The tab's shell SOURCES ~/.zshrc, so the poison is injected AFTER the launcher has run and before
# start-session.sh starts: this file is the only place the fix can live, and the only place it can
# be tested honestly. SCRUB AND ASSERT, per docs/HERDR-ADOPTION-PLAN.md §6 — the scrub removes the
# poison, the assert catches a scrub that rotted.
ENV_RC = 6

# A poison the scrub CANNOT remove — how a rotted scrub is driven without a test-only backdoor in
# the floor itself. BASH_ENV is sourced by every non-interactive bash (a real injection vector), and
# `declare -rx` makes the variable exported AND readonly, so `unset` fails and the poison survives
# into the spawned agent's environment. Nothing here is stubbed: the scrub really runs, really
# fails, and the assert is the only thing standing between that and an API-billed session.
UNSCRUBBABLE = 'declare -rx ANTHROPIC_API_KEY=sk-ant-api03-UNSCRUBBABLE\n'


def _envfail_markers(run_root):
    d = run_root / "state" / "envfail"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _bash_env(tmp_path, body):
    p = tmp_path / "bashenv.sh"
    p.write_text(body)
    return {"BASH_ENV": str(p)}


def test_a_poisoned_parent_env_never_reaches_the_agent(tmp_path):
    """THE claim (c1). The oracle is the environment the AGENT was handed, not the launcher's own
    bookkeeping — a session is billed by what its process received."""
    r, _run_root, args_file = _start(tmp_path, extra_env=dict(POISON_ENV))
    assert r.returncode == 0, f"a scrubbable env must launch normally: rc={r.returncode}\n{r.stderr}"
    assert args_file.exists(), "the agent must have started"
    handed = _recorded_env_names(tmp_path / "claude_env")
    leaked = sorted(n for n in handed if _is_poison(n))
    assert leaked == [], f"these reached the spawned agent: {leaked}"


def test_the_scrub_keeps_what_a_session_legitimately_needs(tmp_path):
    """The boundary, and it is not a nicety: c25's documented landmine is that overriding HOME
    breaks macOS keychain OAuth. A denylist scrub, never an aggressive allowlist — PATH, HOME and
    the loop's own SL_* handoff must survive it intact."""
    r, run_root, _args = _start(tmp_path, extra_env=dict(POISON_ENV))
    assert r.returncode == 0, r.stderr
    handed = _recorded_env(tmp_path / "claude_env")
    assert handed.get("HOME") == str(tmp_path / "home"), "HOME must survive the scrub untouched"
    assert str(tmp_path / "stub") in handed.get("PATH", ""), "PATH must survive the scrub"
    assert handed.get("SL_RUN_ROOT") == str(run_root), "the loop's own handoff must survive"
    assert handed.get("SL_ISSUE_ID") == "i1"


def test_an_unscrubbable_poison_refuses_the_flight_and_leaves_a_named_marker(tmp_path):
    """A scrub that rotted must not fail OPEN. The refusal is loud, per-launch, and NAMES the
    variable — the launcher is meanwhile counting down its delivery-verify window, and without this
    marker a refused flight is indistinguishable from a launch shim that never fired."""
    extra = _bash_env(tmp_path, UNSCRUBBABLE)
    extra["SL_START_TOKEN"] = "TOK"
    r, run_root, args_file = _start(tmp_path, extra_env=extra)
    assert r.returncode == ENV_RC, f"expected rc={ENV_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists(), "the agent must NEVER start in an env that could be API-billed"
    assert "ANTHROPIC_API_KEY" in r.stderr, f"the refusal must NAME the variable: {r.stderr!r}"
    assert _envfail_markers(run_root) == ["i1.TOK"], "a per-launch env marker must be left"
    assert "ANTHROPIC_API_KEY" in (run_root / "state" / "envfail" / "i1.TOK").read_text()
    assert not (run_root / "state" / "started" / "i1.TOK").exists(), \
        "a refused session must NOT stamp the delivery sentinel — it never became a worker"


def test_a_refused_env_writes_no_exited_marker(tmp_path):
    # Same discipline as the auth refusal: the exited marker means "a worker WAS running and its
    # process is gone", and the runner recovers from it by RELAUNCHING — straight back into the same
    # poisoned environment. No worker ever ran here; the launcher's rc is the whole signal.
    r, run_root, _args = _start(tmp_path, extra_env=_bash_env(tmp_path, UNSCRUBBABLE))
    assert r.returncode == ENV_RC, r.stderr
    assert not (run_root / "state" / "exited" / "i1").exists()


def test_the_env_floor_also_gates_the_codex_agent(tmp_path):
    # The floor sits ABOVE the agent branch, so both spawn agents are covered by construction rather
    # than by a second copy that could drift. (A codex session inheriting XDG_CONFIG_HOME loses its
    # gh just the same, and an inherited base-url redirect is nobody's friend.)
    r, _run_root, args_file = _start(tmp_path, agent="codex",
                                     extra_env=_bash_env(tmp_path, UNSCRUBBABLE))
    assert r.returncode == ENV_RC, f"expected rc={ENV_RC}, got {r.returncode}\n{r.stderr}"
    assert not args_file.exists()


def test_a_clean_env_launches_normally(tmp_path):
    """The other half of the contract: the floor must be invisible when there is nothing to scrub.
    A floor that refused a healthy env would be worse than none at all."""
    r, _run_root, args_file = _start(tmp_path, model="fable")
    assert r.returncode == 0, f"a clean env must launch: rc={r.returncode}\n{r.stderr}"
    assert args_file.read_text().splitlines()[-1] == "do the thing"
    assert [n for n in _recorded_env_names(tmp_path / "claude_env") if _is_poison(n)] == []


def test_the_scrub_runs_before_the_gh_auth_assert(tmp_path):
    """ORDERING, and it is load-bearing. The stub gh reproduces the 2026-07-29 landmine: it honours
    XDG_CONFIG_HOME, so pointed anywhere else it answers as nobody. Scrub first and the probe sees a
    healthy config and the launch proceeds; probe first and the loop parks the issue under a memo
    telling the owner to re-login — a confidently WRONG remedy for a poisoned environment."""
    r, _run_root, args_file = _start(tmp_path, extra_env={"XDG_CONFIG_HOME": "/nowhere/xdg"})
    assert r.returncode == 0, \
        f"the env floor must repair gh's config dir before the auth probe: {r.stderr!r}"
    assert args_file.exists()


def test_the_assert_reads_the_env_the_agent_will_inherit_not_the_shells_own_bookkeeping(tmp_path):
    """Why the assert is not a tautology of the scrub. It re-reads the environment from a CHILD
    process — exactly what the agent gets — so a variable the shell believes it removed but did not
    is still caught. Drive it with the readonly poison alongside a scrubbable one: the scrubbable
    one must be gone from the memo, the unscrubbable one named."""
    poisoned = dict(POISON_ENV)
    poisoned.update(_bash_env(tmp_path, UNSCRUBBABLE))
    r, _run_root, _args = _start(tmp_path, extra_env=poisoned)
    assert r.returncode == ENV_RC, r.stderr
    assert "ANTHROPIC_API_KEY" in r.stderr
    assert "ANTHROPIC_BASE_URL" not in r.stderr, \
        f"a variable the scrub DID remove must not be reported as still present: {r.stderr!r}"
    assert "XDG_CONFIG_HOME" not in r.stderr


# ------------------- the claude binary, resolved explicitly (issue #303) -------------------------
# Before this, the launcher ran a BARE `claude` and PATH order decided which binary a worker got.
# On the tool-dive machine the only `claude` was cmux's bundled wrapper — a script that contains no
# Claude Code, walks PATH for another claude and execs it — so retiring cmux would have taken the
# launcher's binary with it. The ladder below replaces that luck: SL_CLAUDE, else the standalone
# native install at ~/.local/bin/claude, else PATH. These cases drive start-session.sh for real and
# read back the $0 the stub was invoked as, which is the only honest oracle for "which binary ran".

def _argv0(tmp_path, agent="claude"):
    return (tmp_path / f"{agent}_argv0").read_text().strip()


def test_launch_runs_the_standalone_install_ahead_of_whatever_path_offers(tmp_path):
    # The core guarantee: with the standalone build present, no PATH entry participates in the
    # decision — so cmux's shim (or its removal) cannot change which binary a worker runs.
    r, _run_root, _args = _start(tmp_path, standalone=True)

    assert r.returncode == 0, r.stderr
    assert _argv0(tmp_path) == str(tmp_path / "home" / ".local" / "bin" / "claude")


def test_launch_honours_an_explicit_sl_claude_pin_over_the_standalone_install(tmp_path):
    pinned = tmp_path / "pinned-claude"
    _x(str(pinned), STUB_AGENT)

    r, _run_root, _args = _start(tmp_path, standalone=True,
                                 extra_env={"SL_CLAUDE": str(pinned)})

    assert r.returncode == 0, r.stderr
    assert _argv0(tmp_path) == str(pinned)


def test_launch_falls_back_to_path_when_no_standalone_install_exists(tmp_path):
    # A machine that installed Claude Code some other way still launches — the ladder's last rung.
    r, _run_root, _args = _start(tmp_path)

    assert r.returncode == 0, r.stderr
    assert _argv0(tmp_path) == str(tmp_path / "stub" / "claude")


def test_launch_refuses_a_pin_that_names_no_executable_instead_of_falling_back(tmp_path):
    # FAIL CLOSED. Falling back to PATH here would restore the exact luck the pin removes, at the
    # moment the operator most believes the binary is pinned — and it would do it silently.
    r, run_root, _args = _start(tmp_path, standalone=True,
                                extra_env={"SL_CLAUDE": str(tmp_path / "nowhere" / "claude")})

    assert r.returncode != 0
    assert "SL_CLAUDE" in r.stderr
    assert not (tmp_path / "claude_argv0").exists()          # no binary ran at all
    # the runner's relaunch-cap park memo reads this file (issue #40), so the reason must be there
    assert "SL_CLAUDE" in (run_root / "state" / "launch_stderr" / "i1").read_text()
    assert (run_root / "state" / "exited" / "i1").exists()


def test_launch_refuses_when_no_claude_exists_anywhere(tmp_path):
    # A PATH with the ordinary system utilities the script itself needs (mktemp, env, sed, date) but
    # no claude anywhere on it — the machine-has-no-Claude-Code state, not a broken shell.
    r, run_root, _args = _start(tmp_path, extra_env={"PATH": "/usr/bin:/bin"})

    assert r.returncode != 0
    assert "claude" in r.stderr.lower()
    assert (run_root / "state" / "exited" / "i1").exists()


def test_launch_records_the_binary_it_actually_ran(tmp_path):
    # A worker tab resolves in ITS OWN environment, so the doctor re-resolving in the operator's
    # shell proves nothing about the worker's (the #299/#301 lesson). This stamp is what does.
    _r, _run_root, _args = _start(tmp_path, standalone=True)

    record = tmp_path / "home" / ".superlooper" / "claude-bin.last"
    assert record.read_text().strip() == str(tmp_path / "home" / ".local" / "bin" / "claude")


def test_a_codex_launch_leaves_the_claude_binary_record_alone(tmp_path):
    # The record answers "which claude did a worker run"; a codex flight must not overwrite it with
    # something that is not a claude at all.
    _r, _run_root, _args = _start(tmp_path, agent="codex", standalone=True)

    assert not (tmp_path / "home" / ".superlooper" / "claude-bin.last").exists()


def test_the_shell_ladder_and_the_doctors_ladder_resolve_the_same_binary(tmp_path, monkeypatch):
    """start-session.sh and stack_doctor.resolve_claude are TWINS, duplicated on purpose (the fresh
    tab shell inherits nothing and knows no engine paths, so the launcher cannot import the doctor).
    Duplication only stays honest if something drives BOTH and compares — otherwise the doctor
    eventually reports on a binary the launcher stopped running, which is precisely the confident
    lie this whole block exists to prevent. Each rung of the ladder is checked, not just one."""
    import stack_doctor

    home = tmp_path / "home"
    stub = tmp_path / "stub"
    pinned = tmp_path / "pinned-claude"
    _x(str(pinned), STUB_AGENT)

    for label, kw, pin in (
        ("PATH fallback", {}, None),
        ("standalone install", {"standalone": True}, None),
        ("explicit pin", {"standalone": True}, str(pinned)),
    ):
        case = tmp_path / label.replace(" ", "_")
        case.mkdir()
        env = {"SL_CLAUDE": pin} if pin else None
        r, _run_root, _args = _start(case, extra_env=env, **kw)
        assert r.returncode == 0, f"{label}: {r.stderr}"
        shell_said = _argv0(case)

        monkeypatch.setenv("HOME", str(case / "home"))
        monkeypatch.setenv("PATH", f"{case / 'stub'}:{os.environ['PATH']}")
        if pin:
            monkeypatch.setenv("SL_CLAUDE", pin)
        else:
            monkeypatch.delenv("SL_CLAUDE", raising=False)

        doctor_said = stack_doctor.resolve_claude(stack_doctor.Probe())["path"]
        assert doctor_said == shell_said, (
            f"{label}: the doctor would report on {doctor_said} while a launch runs {shell_said}")
