"""The report harvest itself — worker_hook.harvest_report (issues #148, #189).

These cases were bought by i280 and i328: both wrote their report to a worktree-relative path on
the same day, the runner (which reads state_home/reports/<id>.md) saw nothing, and the queue
stalled two hours on i328. The harvest rescues exactly that.

It used to be a Stop-hook duty and fired on EVERY rest, which is how it promoted two live drafts to
"finished" on 2026-07-16 (i153/i163 — see test_stop_hook_harness.py's duty-1 block). Issue #189
kept the mover and moved the TRIGGER to the runner's #157 probe ladder, which fires it only once
the worker has acked DONE. So the fences below are tested against the function directly, and the
trigger is tested in test_actions.py / test_runner.py.

Every fence here gates a DESTRUCTIVE move, and the asymmetry is the whole design: a missed rescue
costs a stalled queue, a wrong move destroys the worker's only copy of its work.
"""

import shutil
import subprocess

import worker_hook

ISSUE = "i7"
REPORT_TEXT = "## Tests\nthe suite is green\n\n## Screenshot evidence\nn/a\n\n## Review\nclean\n"


# --------------------------- the rig ---------------------------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=30, check=True).stdout.strip()


def _worktree(path):
    """A real git repo standing in for the worker's worktree. The fences read real git answers."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30, capture_output=True)
    _git(path, "config", "user.email", "worker@example.invalid")
    _git(path, "config", "user.name", "Worker")
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-qm", "seed")
    return _git(path, "rev-parse", "HEAD")


def _state_home(tmp_path):
    root = tmp_path / "run"
    (root / "state").mkdir(parents=True)
    return root


def _harvest(run_root, cwd):
    return worker_hook.harvest_report(str(run_root), ISSUE, str(cwd))


def _canonical(run_root):
    return run_root / "reports" / f"{ISSUE}.md"


# --------------------------- the rescue (i280/i328) ---------------------------

def test_worktree_relative_report_is_harvested_to_the_canonical_path(tmp_path):
    # THE i280/i328 RESCUE — the regression #189 must never break: a truly-finished session whose
    # report is one directory off is still rescued, so the queue never stalls two hours again.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    moved = _harvest(run_root, wt)

    assert moved == str(stray), "the harvest reports the path it moved, for the journal"
    assert _canonical(run_root).read_text() == REPORT_TEXT
    assert not stray.exists(), "the harvest MOVES — a leftover would re-harvest forever"


def test_bare_report_at_the_cwd_root_is_harvested(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    (wt / f"{ISSUE}.md").write_text(REPORT_TEXT)

    assert _harvest(run_root, wt)
    assert _canonical(run_root).read_text() == REPORT_TEXT


def test_a_report_in_a_plain_non_git_cwd_is_still_harvested(tmp_path):
    # git ran and said "not a work tree". There is no branch to damage, so the rescue must still
    # happen — refusing everything would be over-correcting.
    run_root = _state_home(tmp_path)
    plain = tmp_path / "plain"
    (plain / "reports").mkdir(parents=True)
    (plain / "reports" / f"{ISSUE}.md").write_text(REPORT_TEXT)

    assert _harvest(run_root, plain)
    assert _canonical(run_root).read_text() == REPORT_TEXT


# --------------------------- the fences ---------------------------

def test_an_existing_canonical_report_is_never_clobbered(tmp_path):
    # The canonical report is the worker's real deliverable; a stale worktree copy must not win.
    run_root = _state_home(tmp_path)
    (run_root / "reports").mkdir()
    _canonical(run_root).write_text("## Tests\nthe real report\n")
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("## Tests\na stale draft\n")

    assert _harvest(run_root, wt) is None
    assert _canonical(run_root).read_text() == "## Tests\nthe real report\n"
    assert stray.exists(), "with the canonical report present the worktree is not ours to touch"


def test_an_empty_report_is_not_harvested(tmp_path):
    # A touched/half-written file is not a report; harvesting it would fire session_finished on
    # nothing and the gate would read an empty deliverable.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("   \n")

    assert _harvest(run_root, wt) is None
    assert not _canonical(run_root).exists()


def test_no_cwd_means_no_harvest(tmp_path):
    # A pruned/vanished worktree: there is nowhere to look, and inventing one is not an option.
    assert worker_hook.harvest_report(str(_state_home(tmp_path)), ISSUE, None) is None


def test_a_symlinked_report_is_never_harvested(tmp_path):
    # Harvesting MOVES the link itself, so the canonical report would BECOME a symlink to whatever
    # it points at — and the runner reads the canonical report and posts it.
    # The target sits INSIDE the worktree deliberately: a target outside would be refused by the
    # containment check, and this test would pass while proving nothing about the islink fence.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    inside = wt / "notes.md"
    inside.write_text("private working notes, not a report")
    (wt / "reports").mkdir()
    (wt / "reports" / f"{ISSUE}.md").symlink_to(inside)

    assert _harvest(run_root, wt) is None
    assert not _canonical(run_root).exists(), \
        "the canonical report must never become a symlink to some other file"
    assert inside.read_text() == "private working notes, not a report"


def test_a_symlinked_reports_dir_cannot_drag_a_file_out_of_another_directory(tmp_path):
    # The subtler one: the FILE is real, its PARENT is the link. Following it would move a file out
    # of a directory that has nothing to do with this worker.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    elsewhere = tmp_path / "someone-elses-dir"
    elsewhere.mkdir()
    victim = elsewhere / f"{ISSUE}.md"
    victim.write_text("## Tests\nnot this worker's file\n")
    (wt / "reports").symlink_to(elsewhere)

    assert _harvest(run_root, wt) is None
    assert victim.exists(), "never move a file that resolves outside the worker cwd"
    assert not _canonical(run_root).exists()


def test_a_git_tracked_file_is_never_harvested(tmp_path):
    # A report the worker COMMITTED is repo content — ripping it out of the worktree would leave a
    # deletion in the branch under review.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)
    _git(wt, "add", "reports")
    _git(wt, "commit", "-qm", "tracked report")

    assert _harvest(run_root, wt) is None
    assert stray.exists(), "a tracked file must stay in the worktree"
    assert not _canonical(run_root).exists()


def test_harvest_refuses_when_git_cannot_say_whether_the_file_is_tracked(tmp_path, monkeypatch):
    # Fail CLOSED in the destructive direction. With git unavailable we cannot tell a stray report
    # from one the worker COMMITTED. A missed rescue stalls a queue; a wrong move destroys work.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)
    # A PATH carrying everything EXCEPT git — the honest shape of "git is missing", rather than an
    # empty PATH (which would prove nothing about the fence).
    nogit = tmp_path / "nogit-bin"
    nogit.mkdir()
    for tool in ("bash", "python3", "date", "mkdir", "cat", "dirname"):
        real = shutil.which(tool)
        if real:
            (nogit / tool).symlink_to(real)
    assert shutil.which("git", path=str(nogit)) is None, "the rig must really hide git"
    monkeypatch.setenv("PATH", str(nogit))

    assert _harvest(run_root, wt) is None, "unknown tracked-state must refuse the move"
    assert stray.exists()
    assert not _canonical(run_root).exists()


def test_harvest_refuses_when_the_git_index_is_corrupt(tmp_path):
    # The subtle half of the fence. `git ls-files --error-unmatch` says "untracked" with rc=1, but
    # FAILS with rc=128 — and an unreadable index is rc=128. `rev-parse --is-inside-work-tree`
    # cannot rescue that: it never reads the index, so it still answers "true". Read the rc, not
    # just "non-zero", or a committed report gets ripped out of the branch under review.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)
    _git(wt, "add", "reports")
    _git(wt, "commit", "-qm", "tracked report")
    (wt / ".git" / "index").write_bytes(b"GARBAGE")   # git can no longer answer "is it tracked?"

    assert _harvest(run_root, wt) is None, "git could not answer — refuse, never guess"
    assert stray.exists()
    assert not _canonical(run_root).exists()
