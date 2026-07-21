"""Durable per-repo loop state. Atomic writes so a crash never leaves a half file.

issues.json is the runner's memory across restarts: launch/retry/conflict counters, lane,
branch, and per-issue status all live here, so a restarted runner reloads them intact and
never double-launches or drops a counter (the runner rebuilds from GitHub + this disk state).

Ported from autocode's state.py — the atomic-write + advisory-lock + S6 mutate-validation
guard survive UNCHANGED (they were each bought with a corrupted-state incident); the run/PR
schema is replaced by the per-issue schema and ALL rotation / run-completion / status-render
bookkeeping is dropped (the deterministic runner has no rotation, and status rendering moves
to report.py)."""
import copy
import json
import os
import tempfile
import time

# An issues.json read-modify-write takes milliseconds; a lock held longer than this means the
# holder crashed mid-update, so it is safe to steal. A real RMW is sub-millisecond; only a
# frozen/swapped/debugged holder lasts this long, so 120 s is conservative and a transiently-slow
# update is never falsely stolen (codex P2-E). Belt-and-suspenders to the runner pidfile
# singleton: even with one runner, a stray helper must not lost-update issues.json (the
# [null,null] clobber class the run-20260701-1750 tuple-lambda caused).
LOCK_STALE_SECONDS = 120

# The per-issue lifecycle statuses the runner tracks (replaces autocode's run/PR status set):
#   ready    - eligible, not yet launched
#   running  - a worker session is live in a lane
#   awaiting_answer - worker exited cleanly on an owner-decision question (durable GitHub comment);
#              the lane is RELEASED and the window CLOSED — nothing relaunches until the owner
#              answers (re-applies agent-ready). Exempt from every liveness/recovery path (#163).
#   frozen   - activity stale past FREEZE_SECONDS; in the recovery ladder
#   exited   - the Claude process returned to the shell; awaiting relaunch
#   gating   - finished (report exists); the mechanical ship gate is running
#   holding  - merge deferred (frozen mainline, or a touch overlap with an in-flight lane)
#   merged   - squash-merged to the dev mainline (terminal-good)
#   parked   - handed back to William with a memo (terminal-until-William)
#   needs_william - an owner decision is required (bounce / cap hit / recheck fail)
#   bounced  - the worker rejected the issue's premise (BOUNCED: marker); runner posted the memo
#
# There is deliberately NO `blocked` status (retired by #194). A worker with a question writes the
# blocked FILE while its status stays `running`; the runner posts the question durably and settles
# the issue to `awaiting_answer` in the same tick, so nothing ever occupies a "blocked" state. The
# member survived #163's wiring removal as a writerless enum value — an invitation for a future
# edit to re-introduce the live-frozen-session model this repo removed on purpose.
VALID = ["ready", "running", "awaiting_answer", "frozen", "exited",
         "gating", "holding", "merged", "parked", "needs_william", "bounced"]

# One issue's initial state TEMPLATE. `launches` is stamped mechanically by launch-session.sh at
# the moment a worker's delivery is VERIFIED (the only honest point); retries = launches - 1.
# `pr` caches the discovered PR number; `declared_touches` is the areas the issue claims (for
# anti-affinity vs in-flight lanes); `requeue_front` re-front-queues a conflict-regenerated issue.
# NEVER copy this with dict(DEFAULT_ISSUE): `declared_touches` AND `qa_log` are MUTABLE lists, so a
# shallow copy would alias one shared list across every issue and the template (the DEFAULT_PR
# aliasing class of bug — but DEFAULT_PR was all scalars, so it got away with a shallow copy; these
# can't). Construct fresh issues with new_issue(), which deep-copies (cross-review, Task 1).
# Durable-question fields (#163): `questions_asked` is the per-issue question count (capped at 2 — a
# third is a scoping park); `pending_question` holds the question the owner has not yet answered;
# `qa_log` is the answered [{question, answer}] history embedded into every relaunch brief so a fresh
# session inherits the full decision trail.
DEFAULT_ISSUE = {"status": "ready", "branch": None, "lane": None,
                 "launches": 0, "retries": 0, "conflicts": 0,
                 "requeue_front": False, "declared_touches": [], "pr": None,
                 "questions_asked": 0, "pending_question": None, "qa_log": []}


def new_issue():
    """A fresh per-issue state dict. Deep-copies DEFAULT_ISSUE so its mutable member
    (`declared_touches`) is never shared across issues or with the template."""
    return copy.deepcopy(DEFAULT_ISSUE)


def new_state():
    return {"version": 1, "issues": {}}


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, obj):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)   # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _acquire(lock_path, timeout=10.0, stale=LOCK_STALE_SECONDS):
    """Portable advisory mutex via O_EXCL create (flock is absent on macOS). Steals a lock whose
    holder is gone (lockfile older than `stale`). Returns a UNIQUE TOKEN string on acquire (used to
    release safely), or None on timeout. Uses the real wall clock directly — a fixed/injected clock
    would never reach the deadline and spin forever, so it is deliberately not injectable."""
    token = "%d.%d" % (os.getpid(), time.time_ns())
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, token.encode())
            os.close(fd)
            return token
        except FileExistsError:
            try:
                if (time.time() - os.stat(lock_path).st_mtime) > stale:
                    os.remove(lock_path)   # crashed holder -> steal; O_EXCL still arbitrates the retry
                    continue
            except OSError:
                continue                   # lock vanished between stat and remove -> retry the create
            if time.time() >= deadline:
                return None
            time.sleep(0.02)


def _release(lock_path, token):
    """Release ONLY if we still hold the lock (its content is still OUR token). If our lock was
    stolen after a stale-steal, the file now carries someone else's token and we must NOT remove it
    — removing a competitor's lock would break mutual exclusion (codex P2-E)."""
    try:
        with open(lock_path) as f:
            cur = f.read().strip()
    except OSError:
        return
    if cur == token:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def update(path, mutate, timeout=10.0):
    """Locked read-modify-write of issues.json. Acquires the mutex, loads, applies `mutate(obj)`
    (which may mutate in place and return None, or return a replacement), atomic-saves, releases
    ONLY our own token. Serializes concurrent writers so a transient overlap (a stray helper)
    cannot lost-update issues.json. Pairs with the runner pidfile singleton, which keeps overlaps
    rare in the first place."""
    lock_path = path + ".lock"
    token = _acquire(lock_path, timeout=timeout)
    if token is None:
        raise TimeoutError(f"could not acquire {lock_path} within {timeout}s")
    try:
        obj = load(path)
        res = mutate(obj)
        out = res if res is not None else obj
        # L1/S6: a mutate that returns a non-state value (the tuple lambda that wrote run.json =
        # [null,null] on run-20260701-1750) must be REJECTED, never persisted — and the good file
        # left intact. save() is only reached for a dict carrying 'issues'. mutate returning None
        # (in-place) yields out=obj, which always satisfies this.
        if not isinstance(out, dict) or "issues" not in out:
            raise ValueError(
                "loopstate.update mutate must return None (in-place) or a state dict containing "
                f"'issues'; got {type(out).__name__} — refusing to persist (issues.json "
                "corruption guard, S6)")
        save(path, out)
        return out
    finally:
        _release(lock_path, token)
