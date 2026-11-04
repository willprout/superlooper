"""gitops: the thin git shell under the conflict ladder (§C.4 6a), tested against THROWAWAY
local repos built in tmp — a bare origin plus clones that diverge on demand. No network, no gh.

The constitutional assertion lives here too: NO gitops command line ever contains a force flag
(and no rebase — branch updates are merge-based universally, §B.4). Every subprocess argv the
module produces during this suite is recorded and screened; the module source is screened as
well, so the forbidden machinery cannot even exist in dead code.
"""
import os
import shutil
import subprocess

import pytest

import gitops


# --------------------------- the no-force / no-rebase screen ---------------------------

_SEEN_ARGV = []
_FORBIDDEN = {"--force", "--force-with-lease", "-f", "rebase"}


@pytest.fixture(autouse=True)
def _record_and_screen_argv(monkeypatch):
    """Wrap gitops' subprocess.run: record every argv it produces, then (after each test)
    assert none carried a force flag or rebase. This is the kickoff's fixed contract — the
    bright line is enforced on the REAL command lines, not just by code review."""
    real_run = subprocess.run

    def spy(argv, *a, **kw):
        _SEEN_ARGV.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(gitops.subprocess, "run", spy)
    yield
    for argv in _SEEN_ARGV:
        bad = _FORBIDDEN & set(argv)
        assert not bad, f"forbidden git flag {bad} in {argv}"


def test_module_source_contains_no_force_or_rebase():
    # enforcement by absence, at the source level: not even dead code may carry the machinery
    import inspect
    src = inspect.getsource(gitops)
    assert "--force" not in src
    assert "rebase" not in src.lower()


# --------------------------- throwaway repo fixtures ---------------------------

def _sh(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def _init_identity(repo):
    _sh(repo, "config", "user.email", "test@example.invalid")
    _sh(repo, "config", "user.name", "gitops-test")


def _commit_file(repo, name, content, msg):
    (repo / name).write_text(content)
    _sh(repo, "add", str(name))
    _sh(repo, "commit", "-m", msg)


@pytest.fixture()
def repos(tmp_path):
    """A bare origin with one commit on main, plus two clones: `wt` (the issue worktree,
    on branch sl/i1-x) and `dev` (someone else landing work on main)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], capture_output=True, check=True)
    _init_identity(seed)
    _commit_file(seed, "shared.txt", "line1\nline2\nline3\n", "seed")
    _sh(seed, "push", "origin", "HEAD:main")

    wt = tmp_path / "wt"
    subprocess.run(["git", "clone", str(origin), str(wt)], capture_output=True, check=True)
    _init_identity(wt)
    _sh(wt, "checkout", "-b", "sl/i1-x")

    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", str(origin), str(dev)], capture_output=True, check=True)
    _init_identity(dev)
    return {"origin": origin, "wt": wt, "dev": dev}


# --------------------------- merge_update: the ladder's step (a) ---------------------------

def test_merge_update_clean(repos):
    # dev lands a DISJOINT change on main; the issue worktree merge-updates cleanly
    _commit_file(repos["dev"], "other.txt", "new file\n", "disjoint work")
    _sh(repos["dev"], "push", "origin", "HEAD:main")
    _commit_file(repos["wt"], "feature.txt", "issue work\n", "issue work")

    assert gitops.merge_update(repos["wt"], "main") == "clean"
    # the dev commit is now IN the issue branch (a real merge, not a rewrite)
    assert (repos["wt"] / "other.txt").exists()
    assert _sh(repos["wt"], "status", "--porcelain") == ""


def test_merge_update_conflict_aborts_and_leaves_worktree_clean(repos):
    # both sides edit the SAME line -> a real conflict: merge_update must ABORT (never leave
    # conflict markers for something else to trip over) and report "conflict" so the runner
    # takes the regenerate path. The runner never resolves conflicts (bright line).
    _commit_file(repos["dev"], "shared.txt", "line1 DEV\nline2\nline3\n", "dev edit")
    _sh(repos["dev"], "push", "origin", "HEAD:main")
    _commit_file(repos["wt"], "shared.txt", "line1 ISSUE\nline2\nline3\n", "issue edit")

    assert gitops.merge_update(repos["wt"], "main") == "conflict"
    # aborted: no merge in progress, no conflict markers, tree clean
    r = subprocess.run(["git", "-C", str(repos["wt"]), "rev-parse", "-q", "--verify",
                        "MERGE_HEAD"], capture_output=True, text=True)
    assert r.returncode != 0, "MERGE_HEAD still present — the merge was not aborted"
    assert _sh(repos["wt"], "status", "--porcelain") == ""
    assert "ISSUE" in (repos["wt"] / "shared.txt").read_text()   # issue work untouched


def test_merge_update_infra_failure_is_error_never_conflict(repos, tmp_path):
    # a dead remote (network down, wrong URL) is NEITHER clean NOR conflict — reporting
    # "conflict" would trigger a REGENERATE (superseding a healthy PR) off a network blip.
    # "error" makes the gate simply retry the update on a later tick.
    _sh(repos["wt"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert gitops.merge_update(repos["wt"], "main") == "error"


def test_fetch(repos, tmp_path):
    assert gitops.fetch(repos["wt"]) is True
    _sh(repos["wt"], "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert gitops.fetch(repos["wt"]) is False


def test_merge_update_timeout_is_error_not_conflict(monkeypatch):
    # Codex cross-review (Task 9): a merge command KILLED BY TIMEOUT can leave MERGE_HEAD
    # behind — that is an infra failure, not a real conflict; classifying it "conflict" would
    # supersede a healthy PR over a hang. Best-effort abort, then "error".
    calls = []

    def fake_git(cwd, *args, timeout=None):
        calls.append(args)
        if args[0] == "merge" and "--abort" not in args:
            return (124, "timed out")
        return (0, "")

    monkeypatch.setattr(gitops, "_git", fake_git)
    assert gitops.merge_update("/wt", "main") == "error"
    assert ("merge", "--abort") in calls          # cleanup was attempted


def test_merge_update_unverified_abort_is_error_not_conflict(monkeypatch):
    # Codex cross-review (Task 9): "conflict" is only reportable once the abort is VERIFIED
    # (MERGE_HEAD gone) — a worktree stuck mid-merge must read as "error", not enter the
    # regenerate bookkeeping as if it were cleanly classified.
    def fake_git(cwd, *args, timeout=None):
        if args == ("merge", "--no-edit", "origin/main"):
            return (1, "CONFLICT")
        if args == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return (0, "")                        # MERGE_HEAD present — before AND after abort
        if args == ("merge", "--abort"):
            return (1, "abort failed")
        return (0, "")

    monkeypatch.setattr(gitops, "_git", fake_git)
    assert gitops.merge_update("/wt", "main") == "error"


def test_merge_update_detached_head_is_error(repos):
    # Codex cross-review (Task 9): a detached-HEAD worktree would "merge" into no branch and
    # report a clean update that updated nothing — refuse up front.
    _sh(repos["wt"], "checkout", "--detach")
    assert gitops.merge_update(repos["wt"], "main") == "error"


# --------------------------- plain_push: fast-forward only, by construction ---------------------------

def test_plain_push_publishes_branch(repos):
    _commit_file(repos["wt"], "feature.txt", "issue work\n", "issue work")
    assert gitops.plain_push(repos["wt"], "sl/i1-x") is True
    assert "sl/i1-x" in _sh(repos["origin"], "branch", "--list", "sl/i1-x")


def test_plain_push_refuses_non_fast_forward(repos, tmp_path):
    # the remote branch moves ahead independently; a diverged local push must FAIL (git's own
    # non-ff refusal) — there is no force path to override it with, which is the point.
    _commit_file(repos["wt"], "feature.txt", "v1\n", "issue work")
    assert gitops.plain_push(repos["wt"], "sl/i1-x") is True
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--branch", "sl/i1-x", str(repos["origin"]), str(other)],
                   capture_output=True, check=True)
    _init_identity(other)
    _commit_file(other, "feature.txt", "v2 remote\n", "remote advance")
    _sh(other, "push", "origin", "HEAD:sl/i1-x")
    _sh(repos["wt"], "commit", "--allow-empty", "-m", "local divergence")
    assert gitops.plain_push(repos["wt"], "sl/i1-x") is False


# --------------------------- worktree add/remove ---------------------------

def test_worktree_add_new_branch_from_origin(repos, tmp_path):
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    assert (lane / "shared.txt").exists()
    assert _sh(lane, "branch", "--show-current") == "sl/i2-y"


def test_worktree_add_existing_branch(repos, tmp_path):
    # relaunch case (orphaned in-progress with an open PR): re-enter the SAME branch
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    gitops.worktree_remove(repos["wt"], lane)
    lane2 = tmp_path / "lanes" / "i2-again"
    assert gitops.worktree_add(repos["wt"], lane2, "sl/i2-y") is True
    assert _sh(lane2, "branch", "--show-current") == "sl/i2-y"


def test_worktree_remove_dirty_stale_worktree(repos, tmp_path):
    # the M1 relaunch hygiene: a conflict-regenerated issue's STALE worktree (often dirty)
    # must go away so the rebuild starts fresh from current dev. Plain `git worktree remove`
    # refuses a dirty tree and the force flag is constitutionally unavailable — so removal is
    # rmtree + prune, which needs no flag at all.
    lane = tmp_path / "lanes" / "i3"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i3-z", "origin/main") is True
    (lane / "junk.tmp").write_text("uncommitted debris")
    (lane / "shared.txt").write_text("dirty edit")
    assert gitops.worktree_remove(repos["wt"], lane) is True
    assert not lane.exists()
    assert str(lane) not in _sh(repos["wt"], "worktree", "list")


def test_worktree_add_fails_closed_on_bad_start_point(repos, tmp_path):
    assert gitops.worktree_add(repos["wt"], tmp_path / "lanes" / "bad",
                               "sl/i9-bad", "origin/does-not-exist") is False


# --------------------------- worktree_reclaim_block: the unpushed-work guard (issue #190) ---------------------------
# The detector the reclaim path consults BEFORE it prunes: pruning is rmtree, so a worktree whose
# work exists nowhere else (uncommitted, or committed-but-never-pushed) would be destroyed outright.
# These build REAL linked worktrees (exactly what the runner reclaims), not bare dirs.

def test_reclaim_block_none_when_clean_and_at_origin(repos, tmp_path):
    # a fresh lane at origin/main: nothing uncommitted, no commit missing from a remote -> safe.
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    assert gitops.worktree_reclaim_block(lane) is None


def test_reclaim_block_flags_a_dirty_tracked_edit(repos, tmp_path):
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    (lane / "shared.txt").write_text("uncommitted edit\n")
    assert gitops.worktree_reclaim_block(lane) == "dirty"


def test_reclaim_block_flags_untracked_files_as_work(repos, tmp_path):
    # the i153 shape: the worker created NEW files and never `git add`ed them. Untracked output
    # IS the only copy of the work — a bare `git worktree remove` would delete it. Must block.
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    (lane / "invariant_harness.py").write_text("the report says this exists; it lives only here\n")
    assert gitops.worktree_reclaim_block(lane) == "dirty"


def test_reclaim_block_flags_a_committed_but_unpushed_branch(repos, tmp_path):
    # committed, so the tree is clean — but the commit is on no remote ref. Pruning loses it.
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    _commit_file(lane, "feature.txt", "issue work\n", "issue work (never pushed)")
    assert _sh(lane, "status", "--porcelain") == ""          # clean tree...
    assert gitops.worktree_reclaim_block(lane) == "unpushed"  # ...but the work is unpushed


def test_reclaim_block_clears_once_the_branch_is_pushed(repos, tmp_path):
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    _commit_file(lane, "feature.txt", "issue work\n", "issue work")
    assert gitops.worktree_reclaim_block(lane) == "unpushed"
    assert gitops.plain_push(lane, "sl/i2-y") is True
    assert gitops.worktree_reclaim_block(lane) is None        # now on a remote ref -> safe


def test_reclaim_block_reports_both_causes_together(repos, tmp_path):
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    _commit_file(lane, "feature.txt", "committed but unpushed\n", "unpushed commit")
    (lane / "wip.txt").write_text("and uncommitted on top\n")
    assert gitops.worktree_reclaim_block(lane) == "dirty+unpushed"


def test_reclaim_block_none_on_a_missing_directory():
    # already gone: nothing to protect, and the caller's prune just clears a stale registration.
    assert gitops.worktree_reclaim_block("/no/such/worktree/anywhere") is None


def test_reclaim_block_none_on_a_non_git_directory(tmp_path):
    # not a git worktree at all -> outside this git-state guard's mandate; do not wedge the prune.
    d = tmp_path / "plain"
    d.mkdir()
    (d / "loose.txt").write_text("not under git")
    assert gitops.worktree_reclaim_block(d) is None


def test_reclaim_block_fails_closed_when_a_real_worktree_cannot_be_read(repos, tmp_path, monkeypatch):
    # a REAL worktree whose git state can't be read (a status error / corrupt index) must NOT be
    # pruned — we never destroy what we could not verify is saved. Distinct from a non-git dir.
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    real_git = gitops._git

    def flaky(cwd, *args, **kw):
        if args[:1] == ("status",):
            return (128, "fatal: could not read the index")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "_git", flaky)
    assert gitops.worktree_reclaim_block(lane) == "unreadable"


def test_reclaim_block_never_reads_a_broken_git_as_safe(repos, tmp_path, monkeypatch):
    # THE data-loss window the guard must close: on a REAL worktree (a .git entry is present) a
    # WHOLESALE git failure — a timeout, a missing binary, a PATH loss — must fail CLOSED, never
    # return None. None here would send worktree_remove's unconditional rmtree at a checkout still
    # holding the only copy of the work. The .git-entry test (a filesystem fact) is what keeps a
    # broken `git` from ever being mistaken for 'not a worktree, safe to prune'.
    lane = tmp_path / "lanes" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    (lane / "only_copy.txt").write_text("unsaved work")           # dirty + real
    monkeypatch.setattr(gitops, "_git", lambda *a, **k: (127, "git: command not found"))
    assert gitops.worktree_reclaim_block(lane) == "unreadable"    # NOT None


# --------------------------- lane_head: the progress clock's HEAD (issue #515) ---------------------------
# The runner reads every in-flight lane's worktree HEAD each tick, so a commit made mid-turn counts
# as progress without waiting for the worker to rest. The read runs beside a live worker's own git,
# so it must take no lock, and anything it cannot read is None — never another repo's HEAD.

def test_lane_head_reads_a_commit_made_in_the_worktree(repos, tmp_path):
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    before = gitops.lane_head(lane)
    assert before == _sh(lane, "rev-parse", "HEAD") and len(before) == 40
    _init_identity(lane)
    _commit_file(lane, "work.txt", "mid-turn\n", "a commit inside one long turn")
    after = gitops.lane_head(lane)
    assert after != before and after == _sh(lane, "rev-parse", "HEAD")


def test_lane_head_takes_no_lock_in_the_workers_worktree(repos, tmp_path):
    # A worker mid-`git add` holds its index.lock. A read that wanted that lock would fail (or, worse,
    # make the worker's own git fail); this one must read straight past it and leave it untouched.
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    gitdir = _sh(lane, "rev-parse", "--absolute-git-dir")
    lock = os.path.join(gitdir, "index.lock")
    with open(lock, "w") as f:
        f.write("held by the worker")
    st = os.stat(lock)
    expected = _sh(lane, "rev-parse", "HEAD")
    seen = len(_SEEN_ARGV)                         # the spy records every argv, this test's own too
    assert gitops.lane_head(lane) == expected
    argv = _SEEN_ARGV[seen:]
    with open(lock) as f:
        assert f.read() == "held by the worker"
    assert os.stat(lock).st_mtime_ns == st.st_mtime_ns
    assert argv and all("--no-optional-locks" in a for a in argv)
    assert all(a[a.index("--no-optional-locks") + 1] == "rev-parse" for a in argv)


def test_lane_head_is_none_for_a_missing_worktree(tmp_path):
    assert gitops.lane_head(tmp_path / "worktrees" / "gone") is None


def test_lane_head_is_none_for_a_pruned_worktree(repos, tmp_path):
    # The directory survives but its gitdir was pruned: git cannot read it, and that is None.
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    shutil.rmtree(_sh(lane, "rev-parse", "--absolute-git-dir"))
    assert gitops.lane_head(lane) is None


def test_lane_head_never_reads_an_enclosing_repo(repos):
    # A lane directory with no git of its own, sitting INSIDE some other repo (a state home that lives
    # in a checkout): plain `git -C` walks up and answers with THAT repo's HEAD. Never.
    stray = repos["wt"] / "worktrees" / "i9"
    stray.mkdir(parents=True)
    assert gitops.lane_head(stray) is None
    (stray / ".git").mkdir()                       # a .git that is not a repository: same answer
    assert gitops.lane_head(stray) is None


def test_lane_head_reads_past_stray_stderr(repos, tmp_path, monkeypatch):
    # The answer is git's stdout line. A runner environment that makes git chatter on stderr
    # (GIT_TRACE, a config warning) must not turn every read into None — that is a read that ships
    # inert while the suite stays green.
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    expected = _sh(lane, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_TRACE", "1")
    assert gitops.lane_head(lane) == expected


def test_lane_head_ignores_an_inherited_git_dir(repos, tmp_path, monkeypatch):
    # GIT_DIR / GIT_WORK_TREE in the runner's environment would skip discovery altogether and answer
    # with whatever repo they name. The read is about THIS directory, so they are dropped.
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    _init_identity(lane)
    _commit_file(lane, "lane.txt", "x\n", "the lane moves on")
    expected = _sh(lane, "rev-parse", "HEAD")
    other = repos["dev"]
    assert _sh(other, "rev-parse", "HEAD") != expected
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert gitops.lane_head(lane) == expected


def test_lane_head_is_none_when_git_times_out_or_is_missing(repos, tmp_path, monkeypatch):
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    for res in ((124, "git rev-parse timed out after 5s"), (127, "git: command not found"),
                (0, "not-an-oid"), (0, "")):
        monkeypatch.setattr(gitops, "_git", lambda *a, _r=res, **k: _r)
        assert gitops.lane_head(lane) is None, res


def test_lane_head_bounds_the_read_with_a_short_timeout(repos, tmp_path, monkeypatch):
    # Every in-flight lane is read every tick: a hung git must cost seconds, not the 60s fetch bound.
    lane = tmp_path / "worktrees" / "i2"
    assert gitops.worktree_add(repos["wt"], lane, "sl/i2-y", "origin/main") is True
    seen = {}

    def fake(cwd, *args, timeout=None, env=None):
        seen.update(timeout=timeout)
        return (124, "timed out")
    monkeypatch.setattr(gitops, "_git", fake)
    assert gitops.lane_head(lane) is None
    assert seen["timeout"] == gitops.LANE_HEAD_TIMEOUT <= 10


def test_lane_head_never_raises_on_a_wrong_typed_path():
    for bad in (None, 42, ["x"]):
        assert gitops.lane_head(bad) is None


# --------------------------- commit_on_branch (issue #449) ---------------------------
# The triage flight closes an issue as "overtaken" by CITING a commit, and the standing rule
# requires that evidence to be on `origin/<dev>`. This is where the citation is checked, so a
# close whose sha is wishful thinking is refused before the issue is shut.

def test_a_commit_that_really_landed_on_the_dev_branch_is_confirmed(repos):
    landed = _sh(repos["dev"], "rev-parse", "HEAD")
    assert gitops.commit_on_branch(repos["wt"], landed, "origin/main") is True
    assert gitops.commit_on_branch(repos["wt"], landed[:8], "origin/main") is True


def test_a_commit_that_is_only_local_is_not_evidence_on_the_dev_branch(repos):
    # The working tree is NOT evidence — the flight runs in the repo's real checkout, which may sit
    # on anything. A commit that exists here but was never pushed must read as "not on origin/main".
    _commit_file(repos["wt"], "local.txt", "not pushed\n", "local only")
    local = _sh(repos["wt"], "rev-parse", "HEAD")
    assert gitops.commit_on_branch(repos["wt"], local, "origin/main") is False


def test_an_unknown_sha_or_ref_answers_none_rather_than_false(repos):
    # None is "git could not tell us", which a caller treats as "do not act" — but it is kept
    # distinct so the refusal can say which of the two happened.
    assert gitops.commit_on_branch(repos["wt"], "0" * 40, "origin/main") is None
    landed = _sh(repos["dev"], "rev-parse", "HEAD")
    assert gitops.commit_on_branch(repos["wt"], landed, "origin/no-such-branch") is None


def test_a_rev_expression_never_reaches_git(repos):
    # The sha arrives from an UNATTENDED session's own words. argv form already keeps a shell out
    # of it; this keeps an expression from being resolved into an answer nobody cited.
    for bad in ("HEAD~1..HEAD", "-C", "", "   ", None, 7, "HEAD ; rm -rf /"):
        assert gitops.commit_on_branch(repos["wt"], bad, "origin/main") is None


def test_a_ref_NAME_is_not_commit_level_evidence(repos):
    """`origin/main` is trivially an ancestor of `origin/main`. A flight closing an issue as
    overtaken must cite a COMMIT — the standing rule's words are "commit-level evidence" — so a
    ref name has to be refused before git is asked a question whose answer is always yes
    (fresh-agent review round 3, P1)."""
    for name in ("origin/main", "main", "HEAD", "@", "v1.0", "origin/HEAD"):
        assert gitops.commit_on_branch(repos["wt"], name, "origin/main") is None, name


def test_a_branch_whose_NAME_looks_like_a_sha_is_still_not_a_sha(repos):
    """The hex-shape gate alone would let a branch called `deadbeef` through, and git would
    resolve it. The resolved oid must actually START with what was cited."""
    _sh(repos["dev"], "branch", "deadbeef")
    _sh(repos["dev"], "push", "origin", "deadbeef")
    _sh(repos["wt"], "fetch", "origin")
    assert gitops.commit_on_branch(repos["wt"], "deadbeef", "origin/main") is None
    # ...while a real abbreviated oid of the same length still works
    landed = _sh(repos["dev"], "rev-parse", "HEAD")
    assert gitops.commit_on_branch(repos["wt"], landed[:8], "origin/main") is True
    assert gitops.commit_on_branch(repos["wt"], landed.upper(), "origin/main") is True


def test_an_abbreviation_too_short_to_be_meant_is_refused(repos):
    landed = _sh(repos["dev"], "rev-parse", "HEAD")
    for short in (landed[:1], landed[:4], landed[:6]):
        assert gitops.commit_on_branch(repos["wt"], short, "origin/main") is None, short
    assert gitops.commit_on_branch(repos["wt"], landed[:7], "origin/main") is True
