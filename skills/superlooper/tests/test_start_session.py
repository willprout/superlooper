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
                        "SL_CODEX_BYPASS_HOOK_TRUST", "SL_CODEX_NO_ALT_SCREEN")}
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
