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

The same script also carries the loop's only MID-SESSION phase signal (issue #443). A worker
builds, cross-reviews and files its report inside one session that emits no journal landmark, so a
lane read "building" for essentially its whole flight. This script is engine-owned code the worker
merely INVOKES, so it stamps `state/phase/<id>` when the review starts and again when it ends —
the signal can never depend on a worker remembering to announce anything, and by doctrine nothing
reads a screen. The tests below drive that with a stub codex that snapshots the breadcrumb from
INSIDE the review, which is the only way to prove the "during" half.
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
              # copy the phase breadcrumb as the REVIEW SEES IT — the only way to observe the
              # "during" half of a start/end pair from outside the process (issue #443).
              'if [ -n "${SL_TEST_PHASE_SNAPSHOT:-}" ]; then\n'
              '  cat "$SL_RUN_ROOT/state/phase/$SL_ISSUE_ID" > "$SL_TEST_PHASE_SNAPSHOT" 2>/dev/null'
              ' || : > "$SL_TEST_PHASE_SNAPSHOT"\n'
              'fi\n'
              'exit "${SL_TEST_CODEX_RC:-0}"\n')

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


# ===================== the mid-session phase breadcrumb (issue #443) =====================
# The engine advances a lane on journal LANDMARKS, and a whole session's build-plus-review stretch
# emits none — so the plane sat on the same leg for the entire flight. The cross-review is the one
# long sub-step the engine can honestly sense, because it runs through THIS script. Start and end
# are both stamped: a start-only breadcrumb would pin every reviewed lane at "cross-reviewing" for
# the rest of its life, which is the same lie in a different place.

def _phase_file(run_root, iid="i158"):
    return run_root / "state" / "phase" / iid


def _drive_with_snapshot(tmp_path, repo, run_root, **kw):
    """Run the helper inside a loop worker, capturing the breadcrumb AS THE REVIEW SAW IT."""
    snap = tmp_path / "phase_during"
    if snap.exists():
        snap.unlink()
    env = {"SL_TEST_PHASE_SNAPSHOT": str(snap)}
    env.update(kw.pop("extra_env", None) or {})
    proc, argv, _ = _run(repo, tmp_path, run_root=run_root, extra_env=env, **kw)
    during = snap.read_text() if snap.exists() else None
    after = _phase_file(run_root).read_text() if _phase_file(run_root).exists() else None
    return proc, argv, during, after


def _fields(line):
    """`<epoch> k=v k=v` -> (epoch, {k: v}); asserts the house format the runner parses."""
    assert line, "expected a breadcrumb line"
    parts = line.strip().split()
    return int(parts[0]), dict(p.split("=", 1) for p in parts[1:] if "=" in p)


def test_the_lane_reads_cross_reviewing_during_the_review_and_not_after(tmp_path):
    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    proc, argv, during, after = _drive_with_snapshot(tmp_path, repo, run_root)
    assert proc.returncode == 0, proc.stderr
    assert argv is not None, "the review must actually have been invoked"

    at, f = _fields(during)
    assert f["phase"] == "cross-reviewing" and f["event"] == "start"
    assert at > 0

    at2, f2 = _fields(after)
    assert f2["phase"] == "cross-reviewing" and f2["event"] == "end", \
        "a start with no end pins the lane at cross-reviewing for the rest of its flight"
    assert at2 >= at


def test_the_end_stamp_lands_even_when_the_review_fails(tmp_path):
    # The failure case is the one that matters: a review that errored, timed out or was interrupted
    # must not leave the lane claiming to be reviewing. The stamp rides an EXIT trap for this.
    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    proc, _, during, after = _drive_with_snapshot(tmp_path, repo, run_root,
                                                  extra_env={"SL_TEST_CODEX_RC": "7"})
    assert proc.returncode == 7, "the reviewer's exit code must still reach the caller"
    assert _fields(during)[1]["event"] == "start"
    _, f = _fields(after)
    assert f["event"] == "end"
    assert f.get("rc") == "7", "the end stamp records the review's outcome as diagnosable evidence"


def test_the_breadcrumb_is_one_whole_line_the_runner_can_always_parse(tmp_path):
    # The runner re-reads this file every tick while the review is running, so it must never be
    # caught half-written. Written to a temp file and renamed, hence: exactly one line, always.
    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    _, _, during, after = _drive_with_snapshot(tmp_path, repo, run_root)
    for text in (during, after):
        assert text.endswith("\n") and len(text.strip().splitlines()) == 1, repr(text)
    # No stray temp files left behind in the directory the runner scans.
    leftovers = [p.name for p in _phase_file(run_root).parent.iterdir() if p.name != "i158"]
    assert leftovers == [], leftovers


def test_the_stamped_phase_is_what_the_engines_reader_derives(tmp_path):
    # The writer and the reader are pinned to each other here, so the format can never drift into
    # a breadcrumb the runner silently ignores (which would fail soft — and silently — forever).
    import phase

    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    _, _, during, after = _drive_with_snapshot(tmp_path, repo, run_root)
    now = _fields(during)[0] + 1
    assert phase.derive(during, now=now) == phase.CROSS_REVIEWING
    assert phase.derive(after, now=now) == phase.BUILDING


def test_no_breadcrumb_is_written_outside_a_loop_worker(tmp_path):
    # Run standalone (no SL_RUN_ROOT / SL_ISSUE_ID) the helper is just a review command; it must not
    # invent a lane's state anywhere. Same guard the review_pin evidence file already uses.
    repo = _repo(tmp_path)
    proc, argv, _ = _run(repo, tmp_path)                     # no run_root
    assert proc.returncode == 0, proc.stderr
    assert argv is not None
    assert not (tmp_path / "state" / "phase").exists()


def test_a_refused_review_leaves_no_breadcrumb(tmp_path):
    # The pin could not be resolved, so no review runs — and a lane must never read "cross-reviewing"
    # for a review that never started.
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    proc, argv, _ = _run(bare, tmp_path, run_root=run_root)
    assert proc.returncode != 0 and argv is None
    assert not _phase_file(run_root).exists()


def test_an_unwritable_breadcrumb_never_costs_the_review(tmp_path):
    # Fail-soft in the direction that matters: the phase is a label on a board, the review is the
    # work. A state home that refuses the write must lose the label, never the review.
    import os as _os
    import stat as _stat

    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    _os.chmod(run_root / "state", _stat.S_IRUSR | _stat.S_IXUSR)     # read+exec only: no new dirs
    try:
        proc, argv, _ = _run(repo, tmp_path, run_root=run_root)
        assert proc.returncode == 0, proc.stderr
        assert argv is not None, "the review must run even when the breadcrumb cannot be written"
    finally:
        _os.chmod(run_root / "state", _stat.S_IRWXU)


def test_a_signalled_review_still_closes_its_breadcrumb_and_leaves_no_reviewer_behind(tmp_path):
    """The interrupted case, driven for real (fresh-agent review).

    Dropping `exec` means a bash wrapper now sits between the caller and codex, so "what happens
    when this is killed" is a genuinely new question. The realistic kill — a tool timeout, a Ctrl-C
    — signals the PROCESS GROUP, and that is what this drives: the reviewer must die with it (no
    orphan burning the owner's subscription) and the EXIT trap must still close the breadcrumb, or
    the lane would read "cross-reviewing" until the staleness rule expired it.

    Death is asserted on the reviewer's OWN RECORDED PIDS, not on a marker file it would also fail
    to write while merely still sleeping (round-2 review), and never by name or pattern — a pattern
    could match the owner's live processes. The group is one this test created
    (``start_new_session``) and its id is the recorded child pid."""
    import signal
    import time

    repo = _repo(tmp_path)
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    stubdir = tmp_path / "stub"
    stubdir.mkdir(exist_ok=True)
    pids_file = tmp_path / "reviewer_pids"
    # A reviewer that would outlive the signal if nothing killed it, and that writes down BOTH of
    # its pids first — its own and the long sleep it waits on — so death can be asserted directly.
    _x(str(stubdir / "codex"),
       '#!/usr/bin/env bash\n'
       'cat >/dev/null\n'
       'echo "$$" > "$SL_TEST_PIDS"\n'
       'sleep 30 &\n'
       'SP=$!\n'
       'echo "$SP" >> "$SL_TEST_PIDS"\n'
       'wait "$SP"\n')
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(POISON_TOML)
    env = {k: v for k, v in os.environ.items()
           if k not in ("SL_RUN_ROOT", "SL_ISSUE_ID", "SL_REVIEW_REPO_ROOT")}
    env.update({"PATH": f"{stubdir}:{os.environ['PATH']}", "HOME": str(home),
                "SL_TEST_ARGS": str(tmp_path / "args"), "SL_TEST_STDIN": str(tmp_path / "stdin"),
                "SL_TEST_PIDS": str(pids_file),
                "SL_RUN_ROOT": str(run_root), "SL_ISSUE_ID": "i158"})
    proc = subprocess.Popen([CROSS_REVIEW], cwd=str(repo), env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)

    def _pids():
        try:
            return [int(x) for x in pids_file.read_text().split()]
        except (OSError, ValueError):
            return []

    def _alive(pid):
        try:
            os.kill(pid, 0)                # signal 0 = an existence probe; it kills nothing
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                    # exists, owned by someone else — still "alive"
        return True

    try:
        proc.stdin.write(b"please review this artifact\n")
        proc.stdin.close()
        deadline = time.time() + 20
        while len(_pids()) < 2 and time.time() < deadline:
            time.sleep(0.05)               # the reviewer is up and sleeping once both pids land
        reviewer = _pids()
        assert len(reviewer) == 2, f"the stub reviewer never started (pids={reviewer})"
        assert _fields(_phase_file(run_root).read_text())[1]["event"] == "start"

        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:                      # never leave the probe's own process behind
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=20)

    _, f = _fields(_phase_file(run_root).read_text())
    assert f["event"] == "end", \
        "a killed review must still close its breadcrumb — else the lane claims to be reviewing"
    # Both reviewer pids must actually be GONE. Polled briefly: signal delivery and the reaping of
    # a reparented child are not instantaneous, and a zombie would still answer kill(pid, 0).
    deadline = time.time() + 10
    while any(_alive(pid) for pid in reviewer) and time.time() < deadline:
        time.sleep(0.05)
    still = [pid for pid in reviewer if _alive(pid)]
    assert not still, f"the reviewer must outlive nothing (still alive: {still})"
