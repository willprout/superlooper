"""gitops: the thin git shell under the conflict ladder (§C.4 6a), tested against THROWAWAY
local repos built in tmp — a bare origin plus clones that diverge on demand. No network, no gh.

The constitutional assertion lives here too: NO gitops command line ever contains a force flag
(and no rebase — branch updates are merge-based universally, §B.4). Every subprocess argv the
module produces during this suite is recorded and screened; the module source is screened as
well, so the forbidden machinery cannot even exist in dead code.
"""
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
