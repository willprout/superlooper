"""bin/cross-review.sh — the agent-boundary helper that pins the CROSS-REVIEWER's model +
reasoning-effort from the repo's `.superlooper/config.json` and execs `codex exec` with those as
EXPLICIT flags (issue #158). It is the mechanical fix for the 2026-07-14→15 incident, whose root
cause was the plugin cross-review running `codex exec` BARE: the owner changed his machine-global
`~/.codex/config.toml` for unrelated work and every in-flight review silently ran at ultra effort,
timed out, and aged workers past the freeze threshold.

These tests drive the script directly with an arg+stdin-recording stub `codex` on PATH (no real
codex — kickoff rule) and a DELIBERATELY POISONED `$HOME/.codex/config.toml`, pinning that:

  * `-m <models.reviewer>` and `-c model_reasoning_effort="<models.reviewer_effort>"` ALWAYS reach
    the codex CLI — derived from the repo config, never from the ambient `~/.codex/config.toml`;
  * this holds even when the config OMITS the fields (the loader's concrete defaults apply — the
    review is never bare) and even when the ambient toml sets a different model/effort;
  * the prompt on the helper's stdin flows through to `codex exec -` unchanged;
  * the pinned values are surfaced as launch evidence (a stderr line + a durable state file when
    running inside a loop worker) so a review that ran at the wrong tier is diagnosable;
  * with NO resolvable `.superlooper/config.json`, the helper refuses to run codex at all rather
    than fall back to a bare (ambient-poisoned) invocation.
"""
import os
import shutil
import stat
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
CROSS_REVIEW = os.path.join(REPO_ROOT, "skill", "bin", "cross-review.sh")

# records every argv element on its own line AND captures stdin (the prompt), then exits 0.
STUB_CODEX = ('#!/usr/bin/env bash\n'
              'printf "%s\\n" "$@" > "$SL_TEST_ARGS"\n'
              'cat > "$SL_TEST_STDIN"\n'
              'exit 0\n')

# a hostile ambient config: if the helper ever ran `codex` bare, THIS is the model/effort it would
# silently inherit. Every assertion below proves the repo pin wins over these values.
POISON_TOML = 'model = "poison-global-model"\nmodel_reasoning_effort = "ultra"\n'

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _repo(tmp_path, models=None):
    """A minimal superlooper-configured repo under tmp_path. `models` overrides the models block."""
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    cfg = {"repo": "owner/name"}
    if models is not None:
        cfg["models"] = models
    import json
    (repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))
    return repo


def _run(cwd, tmp_path, *, prompt="please review this artifact", extra_env=None,
         poison=True, run_root=None):
    """Run cross-review.sh from `cwd` with a stub codex + a poisoned ~/.codex/config.toml on a
    throwaway HOME. Returns (proc, argv_or_None, stdin_or_None)."""
    stubdir = tmp_path / "stub"
    stubdir.mkdir(exist_ok=True)
    _x(str(stubdir / "codex"), STUB_CODEX)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    if poison:
        (home / ".codex" / "config.toml").write_text(POISON_TOML)
    args_file = tmp_path / "codex_args"
    stdin_file = tmp_path / "codex_stdin"
    for f in (args_file, stdin_file):
        if f.exists():
            f.unlink()
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_RUN_ROOT", "SL_ISSUE_ID", "SL_REVIEW_REPO_ROOT")}
    env.update({
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "HOME": str(home),
        "SL_TEST_ARGS": str(args_file),
        "SL_TEST_STDIN": str(stdin_file),
    })
    if run_root is not None:
        env["SL_RUN_ROOT"] = str(run_root)
        env["SL_ISSUE_ID"] = "i158"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run([CROSS_REVIEW], input=prompt, env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)
    argv = args_file.read_text().splitlines() if args_file.exists() else None
    stdin = stdin_file.read_text() if stdin_file.exists() else None
    return proc, argv, stdin


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_passes_explicit_model_and_reasoning_effort_from_config(tmp_path):
    repo = _repo(tmp_path, models={"reviewer": "gpt-5.5", "reviewer_effort": "high"})
    proc, argv, _ = _run(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _flag_value(argv, "-m") == "gpt-5.5"
    assert _flag_value(argv, "-c") == 'model_reasoning_effort="high"'
    assert argv[0] == "exec"                 # `codex exec`, not bare `codex`
    assert argv[-1] == "-"                    # reads the prompt from stdin


def test_pin_wins_over_poisoned_ambient_config(tmp_path):
    # THE incident test: the repo pin must win even though ~/.codex/config.toml names a different
    # model and 'ultra' effort. The helper never reads the toml, so the poison can never leak.
    repo = _repo(tmp_path, models={"reviewer": "gpt-5.5", "reviewer_effort": "medium"})
    proc, argv, _ = _run(repo, tmp_path, poison=True)
    assert proc.returncode == 0, proc.stderr
    assert _flag_value(argv, "-m") == "gpt-5.5"
    assert "poison-global-model" not in argv
    assert _flag_value(argv, "-c") == 'model_reasoning_effort="medium"'
    assert 'model_reasoning_effort="ultra"' not in argv


def test_defaults_apply_when_config_omits_reviewer_fields(tmp_path):
    # A repo that never set models.reviewer/reviewer_effort still gets EXPLICIT flags — the loader's
    # concrete defaults fill them, so the review is never bare (never inherits the ambient config).
    repo = _repo(tmp_path)                                   # no `models` block at all
    proc, argv, _ = _run(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _flag_value(argv, "-m") == "gpt-5.5"
    assert _flag_value(argv, "-c") == 'model_reasoning_effort="medium"'


def test_prompt_flows_through_stdin_to_codex(tmp_path):
    repo = _repo(tmp_path)
    proc, _, stdin = _run(repo, tmp_path, prompt="REVIEW THIS SPECIFIC ARTIFACT")
    assert proc.returncode == 0, proc.stderr
    assert stdin.strip() == "REVIEW THIS SPECIFIC ARTIFACT"


def test_effort_with_shell_metachars_is_toml_quoted(tmp_path):
    # the effort is TOML-quoted exactly like start-session.sh's codex branch, so a value carrying a
    # quote can't break out of the `-c model_reasoning_effort="..."` assignment.
    repo = _repo(tmp_path, models={"reviewer_effort": 'hi"gh'})
    proc, argv, _ = _run(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _flag_value(argv, "-c") == 'model_reasoning_effort="hi\\"gh"'


def test_pinned_values_are_surfaced_as_launch_evidence(tmp_path):
    repo = _repo(tmp_path, models={"reviewer": "gpt-5.5", "reviewer_effort": "high"})
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    proc, _, _ = _run(repo, tmp_path, run_root=run_root)
    assert proc.returncode == 0, proc.stderr
    # (1) a stderr evidence line naming the pinned values (lands in the worker transcript).
    assert "gpt-5.5" in proc.stderr and "high" in proc.stderr
    assert "cross-review" in proc.stderr.lower()
    # (2) a durable state file the runner/owner can read off-session to diagnose a wrong-tier review.
    pin_file = run_root / "state" / "review_pin" / "i158"
    assert pin_file.exists(), "the pinned reviewer values must be recorded as durable launch evidence"
    body = pin_file.read_text()
    assert "gpt-5.5" in body and "high" in body


def test_refuses_to_run_bare_when_no_config_is_resolvable(tmp_path):
    # No .superlooper/config.json anywhere up the tree: the helper must FAIL LOUD and NOT invoke
    # codex — a bare invocation would inherit the machine-global config, the very thing #158 ends.
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    proc, argv, _ = _run(bare, tmp_path)
    assert proc.returncode != 0
    assert argv is None, "codex must NOT be invoked when the reviewer pin cannot be resolved"
    assert "config" in proc.stderr.lower()


def test_bad_relative_repo_root_refuses_without_hanging(tmp_path):
    # A relative SL_REVIEW_REPO_ROOT that cannot be entered must canonicalize to an absolute start
    # (fallback $PWD) and terminate — a naive `dirname` walk on a relative "." would loop forever.
    # The subprocess timeout in _run would surface a hang as a failure; the assertion pins the clean
    # refuse. (No config exists above the tmp cwd, so it refuses rather than run bare.)
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    proc, argv, _ = _run(bare, tmp_path,
                         extra_env={"SL_REVIEW_REPO_ROOT": "does-not-exist-relative"})
    assert proc.returncode != 0
    assert argv is None, "codex must not run when the pin cannot be resolved"
