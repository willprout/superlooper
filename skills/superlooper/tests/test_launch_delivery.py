"""Delivery-verification tests for bin/launch-session.sh (RC-LAUNCHVERIFY).

Reproduces the run-20260625-1857 overnight killer deterministically: a cmux tab is created (an RPC
that works even when the Mac is locked), but the `cmux send` / `send-key Enter` keystrokes that
should start the worker are silently DROPPED (locked screen / cmux backgrounded), so start-session.sh
never runs. The launcher must DETECT non-delivery (no state/started/<id>.<token> sentinel) and FAIL
LOUDLY (exit 2) WITHOUT stamping any liveness — never fabricate "launched & alive". A normal
(delivered) launch must still succeed and record activity + pane (incl. workspace).

Ported from autocode's test_launch_delivery.py. Adaptations for superlooper:
  - identity/branch come from state/issues.json via loopstate (NOT plan.json);
  - env prefix SL_ (SL_RUN_ROOT/SL_REPO/SL_PANE/SL_CMUX/SL_LAUNCH_DIR/SL_DEV_BRANCH/…);
  - the brief lives at briefs/<id>.md; the worktree bases off origin/<dev>, so the throwaway repo is
    a CLONE (origin/main resolves); ids are i<N> (issues), not pr-NN.

The stub cmux has two modes via $STUB_MODE:
  deliver -> `new-surface` spawns the tab's shell, which sources the REAL launch shim and self-runs
             the dropped command (true integration test of the keystroke-free path).
  drop    -> nothing runs the dropped command (keystrokes enqueued but never delivered → the bug).
  orphan  -> a DIFFERENT tab's stale command stamps a WRONG-token started marker; our own per-token
             marker never appears, so this launch must still fail to verify.
new-surface ALWAYS succeeds in every mode (the tab existed; only the keystrokes were lost).
"""
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
LAUNCH = os.path.join(REPO_ROOT, "skill", "bin", "launch-session.sh")
SHIM_PATH = os.path.join(REPO_ROOT, "skill", "shell", "launch-shim.zsh")

STUB_CMUX = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    # A global flag (`--id-format uuids`) may precede the subcommand — strip it.
    while [ "${1:-}" = "--id-format" ]; do shift 2 || shift; done
    sub="${1:-}"; shift || true
    SURF="11111111-1111-1111-1111-111111111111"
    case "$sub" in
      new-surface)
        # Tab creation is an RPC that works even when the display sleeps -> always OK, real uuids shape.
        echo "OK $SURF 22222222-2222-2222-2222-222222222222 33333333-3333-3333-3333-333333333333"
        case "${STUB_MODE:-drop}" in
          deliver)
            # Mimic cmux spawning the new tab's shell, which sources the launch shim and self-runs the
            # dropped command. Run the REAL shim (shell/launch-shim.zsh) so this is a true integration
            # test of the keystroke-free path. The shim inherits SL_LAUNCH_DIR + PATH (stub claude).
            ( CMUX_SURFACE_ID="$SURF" zsh -c "source '$SHIM_PATH'" ) >/dev/null 2>&1 &
            ;;
          orphan)
            # A DIFFERENT tab's stale command flushes late and stamps a WRONG-token started marker;
            # our own per-token marker never appears, so this launch must still fail to verify.
            ( printf '%s' "ORPHAN-OTHER-TAB-UUID" > "$STUB_STARTED" ) &
            ;;
          drop) : ;;   # nothing runs the dropped command -> no worker -> verify times out (the bug)
        esac
        ;;
      rename-tab)
        # rename-tab runs AFTER launch-session.sh has dropped the worker command and BEFORE its
        # verify loop removes it (drop mode) — so this is a race-free point to CAPTURE the exact
        # command the new tab's fresh shell would run. Used to assert env vars (SL_MODEL/SL_EFFORT)
        # are NAMED in the command (a fresh tab shell inherits nothing, so anything not named is lost).
        [ -n "${STUB_CMD_CAPTURE:-}" ] && cp "$SL_LAUNCH_DIR/$SURF.cmd" "$STUB_CMD_CAPTURE" 2>/dev/null || true
        ;;
      close-surface) printf 'closed\\n' >> "$STUB_DIR/closed" ;;
      *) : ;;
    esac
    exit 0
""")

STUB_CLAUDE = "#!/usr/bin/env bash\nexit 0\n"
STUB_CODEX = "#!/usr/bin/env bash\nprintf \"%s\\n\" \"$@\" > \"$STUB_DIR/codex_args\"\nexit 0\n"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh required for the launch shim")

UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_issue(run_root, iid="i1", branch="sl/i1-thing"):
    """Write state/issues.json with one issue carrying its assigned branch (the runner's job)."""
    import loopstate
    st = loopstate.new_state()
    issue = loopstate.new_issue()
    issue["branch"] = branch
    st["issues"][iid] = issue
    (run_root / "state").mkdir(parents=True, exist_ok=True)
    loopstate.save(str(run_root / "state" / "issues.json"), st)


def _setup(tmp_path):
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "reports").mkdir()
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")
    _seed_issue(run_root)

    home = tmp_path / "home"
    home.mkdir()
    # origin (a real commit) + a CLONE used as SL_REPO, so `origin/main` resolves for the worktree base.
    origin = tmp_path / "origin"
    origin.mkdir()
    genv = {**os.environ, "HOME": str(home), "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-b", "main", str(origin)], check=True, capture_output=True, env=genv)
    subprocess.run(["git", "-C", str(origin), "config", "user.email", "t@example.com"], check=True, env=genv)
    subprocess.run(["git", "-C", str(origin), "config", "user.name", "t"], check=True, env=genv)
    (origin / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True, capture_output=True, env=genv)
    subprocess.run(["git", "-C", str(origin), "commit", "-m", "init"], check=True, capture_output=True, env=genv)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True, env=genv)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, env=genv)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, env=genv)

    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    cmux = stubdir / "cmux"
    _x(str(cmux), STUB_CMUX)
    _x(str(stubdir / "claude"), STUB_CLAUDE)
    _x(str(stubdir / "codex"), STUB_CODEX)
    return run_root, repo, home, stubdir, cmux


def _run_launch(run_root, repo, home, stubdir, cmux, mode, extra_env=None):
    launch_dir = os.path.join(os.path.dirname(str(run_root)), "launchdir")
    env = {
        **os.environ,
        "HOME": str(home),                         # isolate pretrust's ~/.claude.json write
        "PATH": f"{stubdir}:{os.environ['PATH']}",  # stub `claude` on PATH for start-session.sh
        "SL_RUN_ROOT": str(run_root),
        "SL_REPO": str(repo),
        "SL_DEV_BRANCH": "main",
        "SL_PANE": "pane:1",
        "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir),
        "SHIM_PATH": SHIM_PATH,                     # the real launch shim the stub runs in deliver mode
        "SL_LAUNCH_DIR": launch_dir,                # where launch-session drops the cmd file + the shim reads it
        # a wrong-TOKEN marker path (orphan mode) — different filename than the launch's own
        # per-token marker (state/started/i1.<SURF>), so it can never false-verify.
        "STUB_STARTED": str(run_root / "state" / "started" / "i1.ORPHAN-OTHER-TAB-UUID"),
        "STUB_MODE": mode,
        "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run([LAUNCH, "i1"], env=env, capture_output=True, text=True, timeout=60)


def test_delivered_launch_succeeds_and_records_liveness(tmp_path):
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver")
    assert r.returncode == 0, f"expected success, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    # the per-launch start marker is transient proof (observed, then cleaned up on success); success
    # is proven by the liveness + pane records, written ONLY after delivery is verified.
    assert (run_root / "state" / "activity" / "i1").exists(), "activity stamp must be written"
    panes = (run_root / "state" / "panes" / "i1")
    assert panes.exists(), "pane (surface id) must be recorded"
    assert UUID_RE.fullmatch(panes.read_text().strip()), "pane must be a UUID, not a short ref"
    ws = (run_root / "state" / "panes" / "i1.ws")
    assert ws.exists() and UUID_RE.fullmatch(ws.read_text().strip()), "workspace UUID must be recorded"
    assert "delivery verified" in r.stdout
    # the worktree was created off origin/main and checked out on the issue's branch.
    assert (run_root / "worktrees" / "i1").is_dir(), "the issue worktree must exist"


def test_relaunch_clears_the_dead_sessions_mailbox_and_clock_but_keeps_receipts(tmp_path):
    # Issue #148. The Stop hook gave each worker two new per-id markers, so they join the same
    # restart hygiene as report/blocked/exited/awaiting:
    #   * mail was addressed to the session that just DIED. A fresh session would consume a
    #     stranger's instruction as its own — and the hook's delivery is unconditional.
    #   * the progress clock is what a probe ladder reads to tell "made progress" from "took a
    #     turn"; a stale one lets a relaunched session that has never rested look like it stamped.
    # Delivery RECEIPTS are history, not run-state, and must survive the restart.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    mail_dir = run_root / "state" / "mail"
    mail_dir.mkdir(parents=True)
    (mail_dir / "i1").write_text("a dead session's instruction")
    receipt = mail_dir / "i1.consumed.1700000000"
    receipt.write_text("what was really delivered last run")
    status_dir = run_root / "state" / "status"
    status_dir.mkdir(parents=True)
    (status_dir / "i1.json").write_text('{"head": "deadbeef", "ts": 1}')

    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver")

    assert r.returncode == 0, f"expected success, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    assert not (mail_dir / "i1").exists(), "a dead session's mail must not be inherited"
    assert not (status_dir / "i1.json").exists(), "a stale progress clock must not survive relaunch"
    assert receipt.read_text() == "what was really delivered last run", \
        "delivery receipts are the record of what happened — a restart must not erase them"


def test_dropped_keystrokes_fail_loudly_without_fabricating_liveness(tmp_path):
    # THE overnight killer: tab created, keystrokes never delivered (locked Mac).
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop")
    assert r.returncode == 2, f"non-delivery must fail loudly (exit 2), got rc={r.returncode}"
    assert "LAUNCH NOT DELIVERED" in r.stderr
    # the runner must NEVER see this issue as launched-and-alive:
    assert not (run_root / "state" / "started" / "i1").exists()
    assert not (run_root / "state" / "activity" / "i1").exists(), "must NOT fabricate liveness"
    assert not (run_root / "state" / "panes" / "i1").exists(), "must NOT record a pane"


def test_dropped_launch_closes_orphan_tab(tmp_path):
    # On non-delivery the orphan tab must be CLOSED so a buffered command can't flush into a worker
    # on unlock (the locked-keystroke time-bomb). The stub records every close-surface call.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop")
    assert r.returncode == 2
    assert (stubdir / "closed").exists(), "the orphan tab must be closed on a failed launch"


def test_orphan_marker_with_wrong_token_does_not_verify(tmp_path):
    # codex P1-B: a started marker stamped by a DIFFERENT tab (wrong token) must NOT be accepted as
    # delivery of THIS launch — otherwise we'd record liveness/pane for a worker that never started
    # in our tab. The launch must still fail loudly (exit 2) and stamp no liveness.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="orphan")
    assert r.returncode == 2, f"a wrong-token marker must not verify; got rc={r.returncode}"
    assert "LAUNCH NOT DELIVERED" in r.stderr
    assert not (run_root / "state" / "activity" / "i1").exists()
    assert not (run_root / "state" / "panes" / "i1").exists()
    # the orphan marker exists under a DIFFERENT token filename (not this launch's per-token marker)
    assert (run_root / "state" / "started" / "i1.ORPHAN-OTHER-TAB-UUID").exists()


def test_missing_base_branch_fails_with_a_distinct_code_before_any_tab(tmp_path):
    # issue #28: the worktree bases off origin/<dev>. On a repo whose dev_branch is not on origin
    # (a master/develop repo left at "main", or a bad config), the launcher must FAIL FAST with a
    # DISTINCT exit code (3), NAME the missing base, and NOT create a tab or fabricate liveness — so
    # the runner's park memo blames the branch, not the launch shim. The setup's origin has only
    # 'main', so pointing the worker at 'develop' has no origin/develop base.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver",
                    extra_env={"SL_DEV_BRANCH": "develop"})
    assert r.returncode == 3, f"missing base must exit 3, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    assert "origin/develop" in r.stderr                       # names the exact missing base ref
    assert not (run_root / "worktrees" / "i1").exists()       # no worktree created
    assert not (run_root / "state" / "activity" / "i1").exists()  # no fabricated liveness
    assert not (run_root / "state" / "panes" / "i1").exists()     # no pane recorded
    assert not (stubdir / "closed").exists()                  # never even opened a tab to close


def test_worker_singleton_blocks_a_second_worker(tmp_path):
    # RC-WORKER-SINGLETON: if a LIVE worker already owns this id, a second launch's start-session.sh
    # must NOT start a second Claude (no split-brain on one worktree). It exits 0 without stamping the
    # token sentinel, so the second launch can't verify -> exits 2 and the lock is left untouched.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    (run_root / "state").mkdir(parents=True, exist_ok=True)
    wlock = run_root / "state" / "worker.i1.lock"      # a FILE containing the live holder's pid
    wlock.write_text(str(os.getpid()))                 # this test process is alive
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver")
    assert r.returncode == 2, f"second worker must not verify; got rc={r.returncode}"
    assert not (run_root / "state" / "activity" / "i1").exists()
    # the original holder's lock is intact (we never clobbered or stole a live worker's lock)
    assert wlock.read_text() == str(os.getpid())


def test_verified_launch_stamps_launch_counter(tmp_path):
    """Codex M2: the honest-point launch counter must ACTUALLY stamp — without a dedicated test the
    `|| echo WARN` guard in launch-session.sh could let the whole fact-4 feature silently no-op, which
    is exactly the dead-counter failure this task exists to kill."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver")
    assert r.returncode == 0, f"expected success, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    st = json.load(open(run_root / "state" / "issues.json"))
    assert st["issues"]["i1"]["launches"] == 1
    assert st["issues"]["i1"]["retries"] == 0


def test_second_verified_launch_increments_retries(tmp_path):
    """retries = launches - 1: the SECOND delivery-verified launch reads the prior count from
    issues.json and bumps to launches=2, retries=1 (a relaunch is the redo both audited runs logged
    as retries:0). Pre-seeded rather than a live double-launch to stay deterministic — the first
    launch's start-session.sh releases its worker lock asynchronously on an EXIT trap."""
    import loopstate
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    rj = run_root / "state" / "issues.json"
    st = loopstate.load(str(rj))
    st["issues"]["i1"]["launches"] = 1          # one prior delivery-verified launch already recorded
    loopstate.save(str(rj), st)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver")
    assert r.returncode == 0, f"expected success, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    st = json.load(open(rj))
    assert st["issues"]["i1"]["launches"] == 2
    assert st["issues"]["i1"]["retries"] == 1


def test_failed_delivery_does_not_bump_counter(tmp_path):
    """The exit-2 non-delivery path bails BEFORE the honest-point bump, so a launch that never
    became a worker must not inflate the retry telemetry."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop")
    assert r.returncode == 2, f"non-delivery must fail loudly (exit 2), got rc={r.returncode}"
    st = json.load(open(run_root / "state" / "issues.json"))
    assert st["issues"]["i1"].get("launches", 0) == 0   # exit-2 path bails before the bump


def test_worker_command_names_model_and_effort_env(tmp_path):
    # THE integration gap: launch-session.sh builds the worker command EXPLICITLY, because the new
    # tab is a FRESH shell that inherits NONE of the runner's env — so every var start-session.sh
    # needs must be NAMED in that command. If SL_EFFORT is omitted there, the per-issue effort label
    # silently never reaches `claude --effort`, even though start-session.sh handles it. Capture the
    # dropped command (drop mode: no shim runs, so the .cmd survives until rename-tab reads it) and
    # assert both knobs are present, %q-quoted verbatim.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    capture = tmp_path / "worker.cmd"
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop",
                    extra_env={"SL_MODEL": "fable", "SL_EFFORT": "high",
                               "STUB_CMD_CAPTURE": str(capture)})
    assert r.returncode == 2                            # drop mode never delivers -> fails loudly
    cmd = capture.read_text()
    assert "start-session.sh" in cmd, f"captured the wrong thing: {cmd!r}"
    assert "SL_MODEL=fable" in cmd
    assert "SL_EFFORT=high" in cmd                      # the bug lives here if this line ever fails
    assert "SL_AGENT=claude" in cmd                      # default agent reaches the session boundary


def test_launched_command_names_the_attended_flag(tmp_path):
    """#185: SL_ATTENDED is what tells the PreToolUse deny that a PERSON is at this pane (the
    `superlooper debug` owner tap sets it; every unattended launch leaves it empty). The fresh tab
    inherits nothing, so it must be NAMED in the dropped command or the carve-out never arrives —
    and an unattended launch must carry it EMPTY, never absent-and-then-inherited from a stray
    ambient export in whatever shell the runner was started from."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    capture = tmp_path / "attended.cmd"
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop",
                    extra_env={"SL_ATTENDED": "1", "STUB_CMD_CAPTURE": str(capture)})
    assert r.returncode == 2
    assert "SL_ATTENDED=1" in capture.read_text()

    capture2 = tmp_path / "unattended.cmd"
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop",
                    extra_env={"STUB_CMD_CAPTURE": str(capture2)})
    assert r.returncode == 2
    cmd = capture2.read_text()
    assert "SL_ATTENDED=''" in cmd, \
        "an unattended launch must name the flag EMPTY (%%q of ''), not omit it: %r" % cmd


def test_codex_agent_selection_launches_and_pretrusts_project(tmp_path):
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="deliver",
                    extra_env={"SL_AGENT": "codex",
                               "SL_MODEL": "gpt-5.5",
                               "SL_EFFORT": "high",
                               "SL_CODEX_DANGEROUS_BYPASS": "1"})
    assert r.returncode == 0, f"expected Codex launch success, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    wt = run_root / "worktrees" / "i1"
    assert wt.is_dir(), "Codex worker must get the same issue worktree"
    args = (stubdir / "codex_args").read_text().splitlines()
    assert args[0] == "--no-alt-screen"
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--dangerously-bypass-hook-trust" in args
    assert args[args.index("-C") + 1] == str(wt)
    assert args[args.index("-m") + 1] == "gpt-5.5"
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert args[-1] == "do the thing"
    codex_config = home / ".codex" / "config.toml"
    text = codex_config.read_text()
    assert f'[projects."{wt}"]' in text
    assert 'trust_level = "trusted"' in text


def test_codex_worker_command_names_agent_specific_env(tmp_path):
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    capture = tmp_path / "codex-worker.cmd"
    r = _run_launch(run_root, repo, home, stubdir, cmux, mode="drop",
                    extra_env={"SL_AGENT": "codex",
                               "SL_CODEX_DANGEROUS_BYPASS": "0",
                               "SL_CODEX_BYPASS_HOOK_TRUST": "0",
                               "SL_CODEX_NO_ALT_SCREEN": "1",
                               "STUB_CMD_CAPTURE": str(capture)})
    assert r.returncode == 2
    cmd = capture.read_text()
    assert "SL_AGENT=codex" in cmd
    assert "SL_CODEX_DANGEROUS_BYPASS=0" in cmd
    assert "SL_CODEX_BYPASS_HOOK_TRUST=0" in cmd
    assert "SL_CODEX_NO_ALT_SCREEN=1" in cmd


def test_answerer_cwd_mode_launches_in_place_without_worktree(tmp_path):
    """The answerer second mode (`--cwd <dir> a<N>`): a fresh session launched IN an existing dir —
    no worktree, no branch, no git, no issues.json counter (an answerer is not a tracked issue) —
    but the SAME delivery verification + pane/activity recording (Task 10 hires answerers this way)."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    answers = run_root / "answers"; answers.mkdir()
    (run_root / "briefs" / "a1.md").write_text("answer the question")
    launch_dir = os.path.join(os.path.dirname(str(run_root)), "launchdir_a")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "SL_RUN_ROOT": str(run_root),
        "SL_PANE": "pane:1",
        "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir),
        "SHIM_PATH": SHIM_PATH,
        "SL_LAUNCH_DIR": launch_dir,
        "STUB_MODE": "deliver",
        "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    r = subprocess.run([LAUNCH, "--cwd", str(answers), "a1"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"answerer launch must succeed, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    assert (run_root / "state" / "activity" / "a1").exists(), "answerer activity must be recorded"
    assert (run_root / "state" / "panes" / "a1").exists(), "answerer pane must be recorded"
    assert not (run_root / "worktrees" / "a1").exists(), "answerer must NOT create a worktree"
    # a1 is not a tracked issue -> no counter entry was fabricated in issues.json
    st = json.load(open(run_root / "state" / "issues.json"))
    assert "a1" not in st["issues"], "an answerer must not be stamped as an issue"


def test_debugger_cwd_mode_launches_like_an_answerer(tmp_path):
    """The watchdog's unattended sl-debugger session (issue #66) rides the SAME --cwd mode as an
    answerer, with a d<N> id: launched in an existing dir (the target repo checkout), no worktree,
    no issues.json entry, and the identical shim delivery verification — never a headless
    `claude -p` (owner billing rule)."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    (run_root / "briefs" / "d1.md").write_text("diagnose the instance")
    launch_dir = os.path.join(os.path.dirname(str(run_root)), "launchdir_d")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stubdir}:{os.environ['PATH']}",
        "SL_RUN_ROOT": str(run_root),
        "SL_PANE": "pane:1",
        "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir),
        "SHIM_PATH": SHIM_PATH,
        "SL_LAUNCH_DIR": launch_dir,
        "STUB_MODE": "deliver",
        "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    r = subprocess.run([LAUNCH, "--cwd", str(repo), "d1"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"debugger launch must succeed, got rc={r.returncode}\nSTDERR:\n{r.stderr}"
    assert (run_root / "state" / "activity" / "d1").exists(), "debugger activity must be recorded"
    assert (run_root / "state" / "panes" / "d1").exists(), "debugger pane must be recorded"
    assert not (run_root / "worktrees" / "d1").exists(), "a debugger session must NOT create a worktree"
    st = json.load(open(run_root / "state" / "issues.json"))
    assert "d1" not in st["issues"], "a debugger session must not be stamped as an issue"


def test_answerer_missing_cwd_dir_fails(tmp_path):
    """A --cwd dir that does not exist is a runner bug — fail before creating a tab, not silently."""
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    (run_root / "briefs" / "a1.md").write_text("answer")
    env = {
        **os.environ, "HOME": str(home), "PATH": f"{stubdir}:{os.environ['PATH']}",
        "SL_RUN_ROOT": str(run_root), "SL_PANE": "pane:1", "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir), "SHIM_PATH": SHIM_PATH,
        "SL_LAUNCH_DIR": os.path.join(os.path.dirname(str(run_root)), "launchdir_a2"),
        "STUB_MODE": "deliver", "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    r = subprocess.run([LAUNCH, "--cwd", str(run_root / "nope"), "a1"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, f"a missing --cwd dir must fail (exit 1), got rc={r.returncode}"


def test_cwd_mode_rejects_non_answerer_id(tmp_path):
    # cross-review Task 6: --cwd mode is answerer-only. A caller bug that routes an ISSUE id (i<N>)
    # through it would silently skip worktree creation + the issue counter — fail closed instead.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    answers = run_root / "answers"; answers.mkdir()
    (run_root / "briefs" / "i9.md").write_text("x")
    env = {
        **os.environ, "HOME": str(home), "PATH": f"{stubdir}:{os.environ['PATH']}",
        "SL_RUN_ROOT": str(run_root), "SL_PANE": "pane:1", "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir), "SHIM_PATH": SHIM_PATH,
        "SL_LAUNCH_DIR": os.path.join(os.path.dirname(str(run_root)), "launchdir_i9"),
        "STUB_MODE": "deliver", "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    r = subprocess.run([LAUNCH, "--cwd", str(answers), "i9"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, f"--cwd must reject a non-answerer id, got rc={r.returncode}"
    assert not (run_root / "state" / "panes" / "i9").exists(), "must refuse before creating a tab"


def test_answerer_cwd_resolved_to_absolute_path(tmp_path):
    # cross-review Task 6: a RELATIVE --cwd must be resolved to an absolute path before it reaches the
    # dropped `cd %q` (which runs in the new tab's fresh shell, not the launcher's cwd). Passing the
    # answers dir as a path relative to SL_RUN_ROOT must still launch + verify.
    run_root, repo, home, stubdir, cmux = _setup(tmp_path)
    (run_root / "answers").mkdir()
    (run_root / "briefs" / "a1.md").write_text("answer")
    env = {
        **os.environ, "HOME": str(home), "PATH": f"{stubdir}:{os.environ['PATH']}",
        "SL_RUN_ROOT": str(run_root), "SL_PANE": "pane:1", "SL_CMUX": str(cmux),
        "STUB_DIR": str(stubdir), "SHIM_PATH": SHIM_PATH,
        "SL_LAUNCH_DIR": os.path.join(os.path.dirname(str(run_root)), "launchdir_rel"),
        "STUB_MODE": "deliver", "SL_LAUNCH_VERIFY_SECONDS": "5",
    }
    # cwd = run_root so "answers" is relative; the launcher must resolve it to an absolute path.
    r = subprocess.run([LAUNCH, "--cwd", "answers", "a1"], env=env, cwd=str(run_root),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"relative --cwd must resolve + launch, got rc={r.returncode}\n{r.stderr}"
    assert (run_root / "state" / "panes" / "a1").exists(), "answerer pane must be recorded"
