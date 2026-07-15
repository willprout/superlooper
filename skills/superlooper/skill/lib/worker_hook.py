"""The Claude worker Stop hook's core — the runner's in-process embassy (issue #148).

stop-hook.sh stamps liveness and then hands the hook payload here. Three duties, each one a
promise the runner can no longer get from "what a model remembered to do":

  1. REPORT HARVEST. Twice in one day (i280, i328) a worker wrote its report to a
     worktree-relative path; the runner reads state_home/reports/<id>.md, saw nothing, and the
     queue stalled two hours on i328. If the canonical report is absent and one is sitting at a
     conventional spot under the worker's cwd, move it where the runner looks.
  2. PROGRESS CLOCK. state/status/<id>.json each turn end: HEAD, dirty tree, report/blocked
     markers. "Took a turn" is not "made progress" — a session can rest forever changing nothing
     (i328). This is the signal a probe ladder reads to tell those apart; it is written on EVERY
     rest, so a missing stamp means the hook itself didn't run.
  3. MAILBOX. The runner drops state/mail/<id>; this consumes it, blocks the stop, and hands the
     text back as the continuation reason — verified delivery with zero keystrokes. Delivery is
     TWO-PHASE, because the receipt is the runner's proof and a proof that can be true when the
     delivery wasn't is worse than no proof at all:
        <id>            -> <id>.claimed.<ts>    atomic rename; wins the race, empties the inbox
        <id>.claimed.*  -> <id>.consumed.<ts>   ONLY after the block JSON is written AND flushed
        <id>.claimed.*  -> <id>.discarded.<ts>  nothing deliverable in it (blank mail)
     So .consumed means "Claude was handed this", full stop. If we die between the two, the
     leftover .claimed.<ts> is the honest "in flight, never proven" state — a runner may retry it;
     it must never read it as delivered.

CLAUDE ONLY. Codex's Stop is notify-only (it cannot block a stop), so Codex workers keep the
typed-probe + file-ack path; stop-hook.sh never routes them here.

FAIL SILENT, ALWAYS. This runs on every rest of a live session. Every duty is wrapped: a broken
duty degrades to today's behavior (an activity stamp) rather than surfacing a hook error in the
worker's TUI or, worse, wedging the session. The hook speaks to Claude ONLY by printing decision
JSON on stdout — printing nothing lets the stop proceed untouched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

GIT_TIMEOUT = 5           # a wedged git must never hold a worker's rest open
MAIL_MAX_BYTES = 16384    # an instruction, not a payload — an unbounded reason blows the turn
TRUNCATED = "\n\n[superlooper: mail truncated at %d bytes]"


def _git(cwd, *args):
    """A git fact, or None if git can't tell us (no repo, no git, a wedged index). None is a
    real answer here — 'we don't know' is honest, and never a reason to withhold the clock."""
    if not cwd:
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# --------------------------- duty 1: report harvest ---------------------------

# Where a worker that fumbled the absolute path actually puts it. Ordered by how it went wrong:
# a relative "reports/<id>.md" (the i280/i328 shape — the brief's path minus its root), then the
# bare file at the worktree root. Deliberately NOT a search: harvesting MOVES a file, so the hook
# only ever touches a spot that is unambiguously "the report, written one level off".
def _candidates(issue_id):
    return (os.path.join("reports", "%s.md" % issue_id), "%s.md" % issue_id)


TRACKED, UNTRACKED, NO_REPO, UNKNOWN = "tracked", "untracked", "no-repo", "unknown"


def _tracked_state(cwd, rel):
    """Is `rel` git-tracked? Answers TRACKED / UNTRACKED / NO_REPO / UNKNOWN — a real tri-state,
    because this gates a DESTRUCTIVE move and the three "git said no" reasons are not the same:

      * TRACKED   — the worker committed it. It is repo content, not a stray report; moving it
                    would leave a deletion in the branch under review.
      * UNTRACKED — git is healthy and positively says no. Safe to rescue.
      * NO_REPO   — git ran and says this isn't a work tree at all. No branch to damage: rescue.
      * UNKNOWN   — git is missing/wedged/timed out. We CANNOT tell tracked from untracked, so we
                    refuse: a missed rescue costs a stalled queue, a wrong move destroys work.
    """
    try:
        r = subprocess.run(["git", "-C", cwd, "ls-files", "--error-unmatch", "--", rel],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return UNKNOWN
    if r.returncode == 0:
        return TRACKED
    # Non-zero is ambiguous — "untracked", "not a repo", and "broken index" all land here. Ask a
    # second, narrower question to tell them apart rather than guessing in the destructive direction.
    try:
        w = subprocess.run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return UNKNOWN
    if w.returncode != 0:
        return NO_REPO
    return UNTRACKED if w.stdout.strip() == "true" else UNKNOWN


def harvest_report(state_home, issue_id, cwd):
    """Move a stray report to the canonical path. Returns the source path moved, else None."""
    canonical = os.path.join(state_home, "reports", "%s.md" % issue_id)
    if os.path.exists(canonical):
        return None                      # the worker's real deliverable — never clobber it
    if not cwd:
        return None
    real_cwd = os.path.realpath(cwd)
    for rel in _candidates(issue_id):
        src = os.path.join(cwd, rel)
        if not os.path.isfile(src):
            continue
        # SYMLINK FENCE. This duty MOVES a file, so it must be certain the file is really the
        # worker's own, sitting where it looks like it sits. os.path.isfile() follows links and
        # os.replace() moves the LINK — so a `reports` symlink would drag a file out of whatever
        # directory it points at, and a report symlinked to a secret would become the report the
        # runner reads and posts. Refuse a symlink outright, and require the resolved path to land
        # inside the resolved cwd (that second check is what catches a symlinked PARENT).
        if os.path.islink(src):
            continue
        real_src = os.path.realpath(src)
        if not real_src.startswith(real_cwd + os.sep):
            continue
        if real_src == os.path.realpath(canonical):
            continue                     # cwd IS the state home; there is nothing to move
        try:
            with open(src, "rb") as fh:
                if not fh.read(4096).strip():
                    continue             # a touched/half-written file is not a report
        except OSError:
            continue
        if _tracked_state(cwd, rel) not in (UNTRACKED, NO_REPO):
            continue
        try:
            os.makedirs(os.path.dirname(canonical), exist_ok=True)
            try:
                os.replace(src, canonical)
            except OSError:
                shutil.move(src, canonical)   # different filesystem (EXDEV)
        except (OSError, shutil.Error):
            return None
        return src
    return None


# --------------------------- duty 2: the progress clock ---------------------------

def status_snapshot(state_home, issue_id, cwd, now):
    # --no-optional-locks: plain `git status` takes index.lock to refresh the index, and this fires
    # on every rest of a session whose own git may be mid-command. Reading the clock must never
    # contend with the work it is measuring.
    porcelain = _git(cwd, "--no-optional-locks", "status", "--porcelain")
    return {
        "id": issue_id,
        "ts": int(now),
        "cwd": cwd,
        "head": _git(cwd, "rev-parse", "HEAD"),
        # None (not False) when git couldn't tell us — a reader must not mistake "unknown" for clean.
        "dirty": None if porcelain is None else bool(porcelain),
        "report": os.path.exists(os.path.join(state_home, "reports", "%s.md" % issue_id)),
        "blocked": os.path.exists(os.path.join(state_home, "state", "blocked", issue_id)),
    }


def stamp_status(state_home, issue_id, cwd, now):
    """Write state/status/<id>.json atomically — a reader must never catch a half-written clock."""
    d = os.path.join(state_home, "state", "status")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % issue_id)
    blob = json.dumps(status_snapshot(state_home, issue_id, cwd, now), sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(blob + "\n")
        os.replace(tmp, path)            # atomic on POSIX
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return path


# --------------------------- duty 3: the mailbox ---------------------------

def _free_name(base):
    """`base`, or base.1/base.2/… — two mails inside one second must not overwrite each other's
    receipt (a receipt is evidence; silently replacing one loses the record of a real delivery)."""
    name, n = base, 0
    while os.path.exists(name):
        n += 1
        name = "%s.%d" % (base, n)
    return name


def claim_mail(state_home, issue_id, now):
    """Phase 1: atomically take the inbox. Returns (claimed_path, mail_base) or None.

    The rename is the exclusive primitive — whoever wins it owns the mail, so concurrent rests
    cannot both deliver it. It is NOT proof of anything yet; only settle() can say what happened.
    """
    mail = os.path.join(state_home, "state", "mail", issue_id)
    if not os.path.isfile(mail):
        return None
    claimed = _free_name("%s.claimed.%d" % (mail, int(now)))
    try:
        os.replace(mail, claimed)
    except OSError:
        return None
    return claimed, mail


def read_claim(claimed):
    """The claimed mail's text, bounded, or None if there is nothing deliverable in it."""
    try:
        with open(claimed, "rb") as fh:
            raw = fh.read(MAIL_MAX_BYTES + 1)
    except OSError:
        return None
    truncated = len(raw) > MAIL_MAX_BYTES
    text = raw[:MAIL_MAX_BYTES].decode("utf-8", "replace")   # invalid bytes must not kill delivery
    if not text.strip():
        return None
    return text + (TRUNCATED % MAIL_MAX_BYTES if truncated else "")


def settle(claimed, mail_base, verb, now):
    """Phase 2: name what actually happened. `verb` is 'consumed' (Claude was handed it) or
    'discarded' (there was nothing in it to hand over). Called only once the outcome is FACT."""
    dst = _free_name("%s.%s.%d" % (mail_base, verb, int(now)))
    try:
        os.replace(claimed, dst)
    except OSError:
        return None                      # the .claimed marker stays: in flight, never proven
    return dst


# --------------------------- the turn ---------------------------

def run(payload, env, now=None):
    """Do the turn's file-side duties and decide the stop.

    Returns (decision, confirm) where `decision` is the JSON to print (or None to let the stop
    proceed) and `confirm` is a zero-arg callable the CALLER invokes once the decision has
    provably reached Claude. Delivery proof is the caller's to give, not ours: we cannot see
    whether the write succeeded, so we must not be the one to claim it did.
    """
    now = time.time() if now is None else now
    issue_id = (env.get("SL_ISSUE_ID") or "").strip()
    state_home = (env.get("SL_RUN_ROOT") or "").strip()
    if not issue_id or not state_home:
        return None, None                # not a worker session — stop-hook.sh already fenced this
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        cwd = None                       # a pruned/vanished worktree: no git facts, no harvest
    # Harvest BEFORE the stamp so the clock never reports report=false about a report this very
    # turn just rescued. Each duty is independently wrapped — one failing must not sink the others.
    try:
        harvest_report(state_home, issue_id, cwd)
    except Exception:
        pass
    try:
        stamp_status(state_home, issue_id, cwd, now)
    except Exception:
        pass
    # THE BOUNDED RE-INJECTION GUARD. stop_hook_active means Claude is already continuing BECAUSE a
    # Stop hook blocked — blocking again from inside that continuation is how a Stop hook spins
    # forever. Don't even claim the mail: a receipt without a delivery is a lie the runner would
    # read as proof. The mail keeps until the next natural rest. (Claude caps consecutive blocks at
    # 8 as its own backstop; this guard means we never approach it.)
    if payload.get("stop_hook_active"):
        return None, None
    try:
        claim = claim_mail(state_home, issue_id, now)
        if not claim:
            return None, None
        claimed, mail_base = claim
        text = read_claim(claimed)
        if not text:
            # Nothing to hand over. It still leaves the inbox (else it retries on every rest), but
            # it is named for what it was: discarded, never "consumed".
            settle(claimed, mail_base, "discarded", now)
            return None, None
    except Exception:
        return None, None
    return ({"decision": "block", "reason": text},
            lambda: settle(claimed, mail_base, "consumed", now))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                         # malformed input fails closed and SILENT
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return 0
    try:
        decision, confirm = run(payload, os.environ)
    except Exception:
        return 0                         # never break a live session over a hook duty
    if not decision:
        return 0
    try:
        sys.stdout.write(json.dumps(decision))
        sys.stdout.flush()               # a buffered write that never lands is not a delivery
    except Exception:
        return 0                         # the .claimed marker stands: in flight, never proven
    try:
        confirm()                        # NOW the receipt is true
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
