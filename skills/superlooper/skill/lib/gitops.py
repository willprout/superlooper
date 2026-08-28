"""Thin git shell for the conflict ladder (§C.4 6a) and lane worktrees. Everything runs
`git -C <worktree>` with a hard timeout and an rc check; nothing here ever raises into a tick.

Constitutional shape (§B.4, enforced by tests screening every argv AND this file's source):
branch updates are MERGE-BASED universally — there is no history-rewriting path and no forced
push of any kind in this module, so a diverged branch can only fail its push (git's own
fast-forward refusal) and re-enter the gate. The runner never resolves conflicts: a real
conflict is aborted and reported, and the regenerate/park decision belongs to gate.py.
"""
import os
import re
import shutil
import subprocess

GIT_TIMEOUT = 60   # seconds per git command — a hung network fetch must never wedge a tick

# An OBJECT ID as a citation may take: a 7-to-40 character hex abbreviation, either case.
# `commit_on_branch` explains why a ref name is not allowed to stand in for one.
_OID_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _git(cwd, *args, timeout=GIT_TIMEOUT):
    """Run `git -C <cwd> <args>`. Returns (rc, combined output). Never raises: timeout, missing
    binary, or any OSError becomes a nonzero rc with the message as output (fail closed)."""
    try:
        r = subprocess.run(["git", "-C", os.fspath(cwd), *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.returncode, (r.stdout or "") + (r.stderr or ""))
    except subprocess.TimeoutExpired:
        return (124, f"git {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        return (127, str(e))


def fetch(worktree):
    """`git fetch origin`. True on success."""
    rc, _ = _git(worktree, "fetch", "origin")
    return rc == 0


def commit_on_branch(worktree, commit, ref):
    """Is `commit` an ancestor of `ref` (i.e. does that history really contain it)?

    True / False / None, and the tri-state is the point (issue #449). This is what turns the
    standing rule's "citing commit-level evidence on `origin/main`" from a habit into a CHECK: a
    triage flight closing an issue as overtaken names a sha, and this is where the naming is
    verified before the issue is shut. Judging against `origin/<dev>` — never the working tree —
    is that rule's own discipline, and a ref this resolves is the only thing it will answer about.

    None means "git could not tell us", and a caller must treat it exactly as False for the
    purpose of ACTING: an unresolvable sha, a missing ref, a git that is not there. It is kept
    distinct so the refusal can say WHICH — "that commit is not on origin/main" and "git could not
    answer" send a flight to two different next steps.

    `commit` must be an OBJECT ID — a 7-to-40 character hex abbreviation — and never a ref name.
    That is not tidiness (fresh-agent review round 3, P1): `commit_on_branch(wt, "origin/main",
    "origin/main")` is trivially True, so a ref name would let an unattended flight satisfy the
    standing rule's "commit-level evidence" with a string that names no evidence at all. The shape
    gate also means an argument composed by that session — `--commit "$(rm -rf ~)"` is a string,
    not a commit — never reaches git in the first place (the argv form already keeps a shell out
    of it).

    The gate is TWO checks, because a hex shape alone is not an oid: a branch literally named
    `deadbeef` passes the regex and git resolves it happily. So the resolved oid must actually
    BEGIN with what was cited — which a branch's tip will not, and an abbreviation always does.
    """
    if not isinstance(commit, str) or not isinstance(ref, str):
        return None
    c, r = commit.strip(), ref.strip()
    # 7 is git's own floor for a meaningful abbreviation and the shortest thing a person cites; 40
    # is a full sha-1. Below 7 an abbreviation is a coincidence rather than a citation.
    if not _OID_RE.match(c):
        return None
    if not r or r.startswith("-"):
        return None
    rc, out = _git(worktree, "rev-parse", "--verify", "--quiet", c + "^{commit}")
    if rc != 0:
        return None                      # not a commit this repository has at all
    # The resolved oid must be the thing that was CITED, not something git found by that name.
    resolved = (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""
    if not resolved.lower().startswith(c.lower()):
        return None                      # a ref that merely LOOKS like an oid (branch `deadbeef`)
    rc, _ = _git(worktree, "rev-parse", "--verify", "--quiet", r + "^{commit}")
    if rc != 0:
        return None                      # the ref itself is unresolvable (never fetched?)
    rc, _ = _git(worktree, "merge-base", "--is-ancestor", c, r)
    if rc == 0:
        return True
    if rc == 1:
        return False                     # git's own "no" — the commit is real but not on that ref
    return None                          # any other rc is git failing, not git answering


def head_oid(worktree):
    """The worktree's current HEAD oid, or None if git could not answer (fail closed: the caller
    records no carry rather than a wrong one — issue #154). Used after a merge-update to name the
    head the reviewed diff was carried onto."""
    rc, out = _git(worktree, "rev-parse", "HEAD")
    oid = (out or "").strip()
    return oid if rc == 0 and len(oid) == 40 and all(c in "0123456789abcdef" for c in oid) else None


def merge_update(worktree, dev_branch):
    """Ladder step (a): fetch, then merge origin/<dev_branch> into the issue branch.

    Returns:
      "clean"    — merged (or already up to date); caller proceeds to recheck + plain push.
      "conflict" — a REAL merge conflict; the merge was aborted, the worktree left clean, and
                   the caller takes the regenerate path. Never leaves conflict markers behind.
      "error"    — infrastructure failure (fetch failed, git crashed/timed out, dirty tree,
                   detached HEAD, an abort that didn't take). Deliberately distinct from
                   "conflict": superseding a healthy PR over a network blip would be a false
                   regenerate; the gate simply retries the update on a later tick
                   (gate_decision routes any non-clean/non-conflict update_result back to
                   "update").

    Classification discipline (Task-9 cross-review): "conflict" is only reportable when the
    merge itself said so AND the abort is VERIFIED (MERGE_HEAD gone). A merge killed by
    timeout can leave MERGE_HEAD behind exactly like a real conflict — that is infra, not a
    conflict; and a worktree stuck mid-merge must never enter the regenerate bookkeeping as
    if it were cleanly classified.
    """
    # a detached-HEAD worktree would "merge" into no branch and report a clean update that
    # updated nothing — refuse up front (lane worktrees are always on their issue branch)
    on_branch, _ = _git(worktree, "symbolic-ref", "-q", "HEAD")
    if on_branch != 0:
        return "error"
    if not fetch(worktree):
        return "error"
    rc, _ = _git(worktree, "merge", "--no-edit", f"origin/{dev_branch}")
    if rc == 0:
        return "clean"
    if rc in (124, 127):                       # timeout / no git: infra, whatever disk says
        _git(worktree, "merge", "--abort")     # best-effort cleanup of a half-started merge
        return "error"
    # a real conflict leaves MERGE_HEAD; anything else (dirty tree, bad ref) never started
    merge_started, _ = _git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if merge_started != 0:
        return "error"
    _git(worktree, "merge", "--abort")
    aborted, _ = _git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return "conflict" if aborted != 0 else "error"


def plain_push(worktree, branch=None):
    """An ordinary `git push origin [<branch>]` — fast-forward only by construction, because
    no flag exists in this module to make it anything else. A diverged remote refuses the
    push (False) and the gate re-enters. True on success."""
    args = ["push", "origin", branch] if branch else ["push", "origin", "HEAD"]
    rc, _ = _git(worktree, *args)
    return rc == 0


def worktree_add(repo, path, branch, start_point=None):
    """Create a lane worktree. With `start_point` (e.g. 'origin/main'): a NEW branch `branch`
    at that point — the per-issue fresh start. Without: re-enter the EXISTING branch (the
    orphaned-in-progress relaunch case, plan Task 10). True on success; a bad ref or an
    already-checked-out branch fails closed to False."""
    path = os.fspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if start_point:
        rc, _ = _git(repo, "worktree", "add", "-b", branch, path, start_point)
    else:
        rc, _ = _git(repo, "worktree", "add", path, branch)
    return rc == 0


def worktree_remove(repo, path):
    """Remove a lane worktree, INCLUDING a dirty one — the M1 relaunch hygiene: a
    conflict-regenerated issue's stale worktree (usually dirty) must vanish so the rebuild
    starts fresh from current dev. Plain `git worktree remove` refuses dirty trees and the
    override flag is constitutionally unavailable here, so removal is rmtree + prune, which
    needs no flag at all. The branch itself is untouched (branches are preserved; only the
    checkout dies). True when the directory is gone and the registration pruned.

    This is the UNCONDITIONAL primitive — it destroys whatever is in the tree. Reclaim paths that
    must never destroy the only copy of a worker's output consult worktree_reclaim_block FIRST
    (issue #190); the deliberate throwaway/rebuild paths (regenerate, reapprove) and the merge
    paths (work already on the mainline) call this directly, as before."""
    p = os.fspath(path)
    shutil.rmtree(p, ignore_errors=True)
    rc, _ = _git(repo, "worktree", "prune")
    return not os.path.exists(p) and rc == 0


def checkout_age(worktree, now):
    """How many seconds this checkout has existed on disk — or 0 (BRAND NEW) when that cannot be
    read. No git, no network: one ``stat``. Lives beside ``worktree_reclaim_block`` because it is
    the other question a reclaim sweep asks of a directory, and because both of its callers — the
    runner's flight sweep and the `upkeep` census — already come here for the first one.

    THE ZERO IS THE CONTRACT (issue #463, fresh-agent review). Its callers read a YOUNG checkout as
    "a launch may still be reaching this": ``lib/launch.py`` creates a flight's checkout and only
    then opens the pane, so between the two there is an interval in which the directory exists and
    the session's singleton lock does not. A stat that could not be taken must therefore land on
    "brand new", never on "old enough to prune". A probe result, never a raise — including for a
    wrong-typed path, which ``os.stat`` answers with TypeError rather than OSError.

    ``max(mtime, ctime)``, taking the MORE RECENT of the two, for the same direction: either alone
    is a usable lower bound on age, and the newer one yields the SMALLER age. `git worktree add`
    stamps both at creation; anything that later writes into the directory moves mtime, which is
    what makes the re-point of a reused flight checkout read as recent activity rather than as an
    old, abandoned tree."""
    try:
        st = os.stat(os.fspath(worktree))
    except (OSError, ValueError, TypeError):
        return 0
    return max(0.0, now - max(st.st_mtime, st.st_ctime))


def worktree_reclaim_block(worktree):
    """Would pruning this worktree destroy the only copy of its work? Returns a short reason string
    when it WOULD (so the reclaim must refuse), or None when the checkout is safe to drop (issue
    #190). Reasons:
      "dirty"          — uncommitted changes live only here: tracked edits, staged, OR untracked
                         files (the i153 shape — the worker's output never `git add`ed).
      "unpushed"       — commits on the branch that no remote-tracking ref contains: the branch ref
                         alone would carry them, but the worker never pushed, so they exist nowhere
                         but this checkout.
      "dirty+unpushed" — both.
      "unreadable"     — this IS a git worktree but its state could not be read (a transient git
                         error, corruption). Fail CLOSED: never destroy what we could not verify is
                         saved. Leaks a checkout (disk) rather than risk the work.

    None (SAFE to prune) is returned for a missing directory (nothing to protect — the caller's
    prune just clears a stale registration) and for a directory that is not a git worktree at all
    (outside this git-state guard's mandate; the runner's reclaim targets are always real linked
    worktrees, so this only spares a bare dir from wedging the sweep — it never overrides the
    dirty/unpushed refusal on a genuine worktree).

    Read-only and network-free by construction (status + a local rev-list against the
    remote-tracking refs already on disk): the reclaim sweep runs it every tick, so it must never
    fetch or mutate."""
    p = os.fspath(worktree)
    if not os.path.isdir(p):
        return None                                    # already gone: nothing to protect
    # Is this a git worktree at all? Decide by a FILESYSTEM fact, never a git command's rc: a linked
    # worktree always carries a .git entry, a stray dir never does. Leaning on `git rev-parse` here
    # would be the bug — a transient git failure (timeout rc=124, missing binary rc=127, unreadable
    # index rc=128) and a genuine non-git dir both give rc!=0, and reading that as "not a worktree,
    # safe to prune" would let worktree_remove destroy a REAL checkout's unsaved work. So: no .git
    # entry -> outside the guard's mandate, return None (a stray dir never wedges the sweep); a .git
    # entry present -> it IS ours, and anything we then cannot read fails CLOSED (never destroy what
    # we could not verify is saved — issue #190).
    if not os.path.exists(os.path.join(p, ".git")):
        return None
    reasons = []
    rc, out = _git(p, "status", "--porcelain")         # includes untracked by default (the ?? lines)
    if rc != 0:
        return "unreadable"                            # a real worktree we can't read -> never prune
    if out.strip():
        reasons.append("dirty")
    # Commits reachable from HEAD but from no remote-tracking ref: `--not --remotes` subtracts every
    # refs/remotes/* (origin/main, origin/<branch> once pushed). 0 => every commit is already on a
    # remote; >0 => this checkout holds the only copy. A successful push updates the tracking ref, so
    # this clears without a fetch. `--count` prints a lone integer to stdout; take the first token so
    # a stray stderr warning folded in by _git can never turn a real count into a false "unreadable".
    rc, out = _git(p, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if rc != 0:
        return "unreadable"
    tokens = out.split()
    if not tokens:
        return "unreadable"
    try:
        unpushed = int(tokens[0])
    except ValueError:
        return "unreadable"
    if unpushed > 0:
        reasons.append("unpushed")
    return "+".join(reasons) if reasons else None
