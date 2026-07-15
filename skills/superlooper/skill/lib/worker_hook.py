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
     text back as the continuation reason — verified delivery with zero keystrokes. The claim is
     an atomic rename to the .consumed.<ts> receipt the runner reads as delivery proof: one
     syscall makes it both gone from the inbox and provably delivered, so no rest can deliver the
     same mail twice.

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


def _is_tracked(cwd, rel):
    """True only if git positively says the file is tracked. A file the worker COMMITTED is repo
    content, not a stray report — moving it would leave a deletion in the branch under review.
    Unknown (no repo/no git) reads as untracked: rescuing the report is the likelier good."""
    try:
        r = subprocess.run(["git", "-C", cwd, "ls-files", "--error-unmatch", "--", rel],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return False
    return r.returncode == 0


def harvest_report(state_home, issue_id, cwd):
    """Move a stray report to the canonical path. Returns the source path moved, else None."""
    canonical = os.path.join(state_home, "reports", "%s.md" % issue_id)
    if os.path.exists(canonical):
        return None                      # the worker's real deliverable — never clobber it
    if not cwd:
        return None
    for rel in _candidates(issue_id):
        src = os.path.join(cwd, rel)
        if not os.path.isfile(src):
            continue
        if os.path.realpath(src) == os.path.realpath(canonical):
            continue                     # cwd IS the state home; there is nothing to move
        try:
            with open(src, "rb") as fh:
                if not fh.read(4096).strip():
                    continue             # a touched/half-written file is not a report
        except OSError:
            continue
        if _is_tracked(cwd, rel):
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
    porcelain = _git(cwd, "status", "--porcelain")
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

def consume_mail(state_home, issue_id, now):
    """Claim state/mail/<id> and return its text (bounded), or None if there's nothing to deliver.

    The claim IS the receipt: one atomic rename empties the inbox and writes the delivery proof,
    so there is no window where the mail is both queued and delivered. Blank mail is claimed too —
    it carries no instruction (nothing to say), but leaving it would retry it on every rest.
    """
    box = os.path.join(state_home, "state", "mail")
    mail = os.path.join(box, issue_id)
    if not os.path.isfile(mail):
        return None
    base = "%s.consumed.%d" % (mail, int(now))
    receipt, n = base, 0
    while os.path.exists(receipt):       # two mails inside one second must not overwrite a proof
        n += 1
        receipt = "%s.%d" % (base, n)
    try:
        os.replace(mail, receipt)
    except OSError:
        return None
    try:
        with open(receipt, "rb") as fh:
            raw = fh.read(MAIL_MAX_BYTES + 1)
    except OSError:
        return None
    truncated = len(raw) > MAIL_MAX_BYTES
    text = raw[:MAIL_MAX_BYTES].decode("utf-8", "replace")
    if not text.strip():
        return None
    return text + (TRUNCATED % MAIL_MAX_BYTES if truncated else "")


# --------------------------- the turn ---------------------------

def run(payload, env, now=None):
    """Do the turn's duties. Returns the decision JSON to print, or None to let the stop proceed."""
    now = time.time() if now is None else now
    issue_id = (env.get("SL_ISSUE_ID") or "").strip()
    state_home = (env.get("SL_RUN_ROOT") or "").strip()
    if not issue_id or not state_home:
        return None                      # not a worker session — stop-hook.sh already fenced this
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
        return None
    try:
        text = consume_mail(state_home, issue_id, now)
    except Exception:
        return None
    if not text:
        return None
    return {"decision": "block", "reason": text}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                         # malformed input fails closed and SILENT
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return 0
    try:
        decision = run(payload, os.environ)
    except Exception:
        return 0                         # never break a live session over a hook duty
    if decision:
        sys.stdout.write(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
