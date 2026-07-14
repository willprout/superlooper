"""File-signal sensing core: explicit on-disk signals -> edge-triggered events.

Ported from autocode's bin/watcher.py pure core (v2, the 2026-06-25 bombproofing pass — see
docs/founding/EVENT-MODEL.md, which IS the contract this module implements). The v1 `.done`
file (touched by the Stop hook on EVERY turn-yield) conflated "finished" with "yielded to
await my own background work" and produced ~50% false wakes. v2 reads explicit, file-backed
signals per issue id:

  reports/<id>.md           -> session_finished   (content-hash token; identical rewrite won't re-fire)
  state/blocked/<id>        -> session_blocked     (the session's plain-text question)
  state/exited/<id>         -> session_exited      (start-session.sh wrote it when Claude left the shell)
  activity stale, no marker -> session_idle (~8m, safe peek) then frozen (~45m, recovery)
  state/awaiting/<id>       -> suppresses the idle peek for known-long background work

What did NOT port (enforcement by absence — the standing orchestrator died, and sensor+actor
are now ONE deterministic process): all rotation_* machinery, the doorbell/ring_* layer, the
stall_*/ring-health alarms keyed on an LLM draining a queue, and the state/ship/*.json relay
(ship_status_token/normalize_ship/poll_ship) — the runner polls PR status directly via gh.py
inside its own tick, so GitHub state never needs a file relay between two processes.
"""
import hashlib
import os

# Staleness tiers (seconds). These module defaults mirror the §C.1 config defaults
# (session.idle_seconds / session.freeze_seconds); the runner passes the adopted repo's
# configured values — the env-var tunables died with the watcher daemon.
IDLE_SECONDS = 480        # rested-no-marker -> safe peek
FREEZE_SECONDS = 2700     # hard stall -> recovery ladder


def _hash_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


# Statuses for which the runner has already taken ownership — never fire idle/frozen for them
# (autocode review P0-1/F6/P2-3, mapped onto the loopstate lifecycle: a gating/holding/merged/
# parked/bounced/needs-william issue must not be nudged or restarted; its worker session is done
# or the decision now belongs to the runner/William, not the liveness tiers).
SETTLED_STATUSES = {"gating", "holding", "merged", "parked", "needs_william", "bounced"}


def detect_events(snaps, emitted, now=None, idle_secs=IDLE_SECONDS, freeze_secs=FREEZE_SECONDS,
                  wake_grace_until=None):
    """Pure: per-issue snapshots -> (new_events, updated_emitted_set). See EVENT-MODEL.md.

    A snapshot: {id, status, launched, activity_mtime, report_hash, report_mtime, blocked_hash,
                 blocked_mtime, exited_token, exited_mtime, awaiting, now}.

    finished/blocked/exited are EDGE-triggered on a content/mtime token so an identical re-write
    never re-fires (A6) — but the dedup key is UN-LATCHED the moment the marker file is gone
    (review F1), so after the runner/launch-session.sh removes a marker a true re-create (even
    byte-identical) re-fires. 'resolved' (exempt from idle/frozen) is marker EXISTENCE, NOT an
    activity comparison (review P0-1: the Stop/PostToolUse hooks stamp activity AFTER the report,
    so an mtime compare made every finished session look unresolved and fire false idle+frozen).
    idle/frozen also never fire for a SETTLED status (the runner already owns it).

    wake_grace_until (issue #42): while now < this deadline, the idle/frozen staleness compare is
    SKIPPED entirely — a laptop that slept makes every in-flight worker's activity_mtime look hours
    old on the first post-sleep tick purely from the wall-clock jump, and a healthy session must not
    be poked for the resume artifact. Suppression un-latches the idle/frozen dedup keys exactly like
    a fresh session, so a session that is genuinely dead across the sleep re-fires once the grace
    expires (the runner opens the window; None/absent = no grace, the normal case)."""
    events = []
    em = set(emitted)

    def emit(ev, key):
        if key not in em:
            events.append(ev)
            em.add(key)            # within-call dedup; the runner discards the key if its durable write fails

    for s in snaps:
        sid = s["id"]
        n = s.get("now") if s.get("now") is not None else now

        if s.get("report_hash"):
            emit({"type": "session_finished", "id": sid, "report_token": s["report_hash"]},
                 (sid, "finished", s["report_hash"]))
        else:                       # report gone -> un-latch so a re-created report re-fires (F1)
            em = {k for k in em if not (k[0] == sid and k[1] == "finished")}

        if s.get("blocked_hash"):
            emit({"type": "session_blocked", "id": sid, "blocked_token": s["blocked_hash"]},
                 (sid, "blocked", s["blocked_hash"]))
        else:                       # marker removed (runner answered) -> un-latch (F1)
            em = {k for k in em if not (k[0] == sid and k[1] == "blocked")}

        if s.get("exited_token") is not None:
            emit({"type": "session_exited", "id": sid, "exited_token": s["exited_token"]},
                 (sid, "exited", s["exited_token"]))
        else:                       # marker cleared on relaunch -> un-latch (F1)
            em = {k for k in em if not (k[0] == sid and k[1] == "exited")}

        # resolved = a rest marker EXISTS (no activity comparison — review P0-1).
        resolved = (bool(s.get("report_mtime")) or bool(s.get("blocked_mtime"))
                    or s.get("exited_mtime") is not None)
        # A corrupt state/issues.json can carry a WRONG-TYPED status. An UNHASHABLE one ([]/{}) makes
        # `status in SETTLED_STATUSES` raise `unhashable type`, which — since the tick stamps its
        # heartbeat LAST (runner.tick) — wedges the whole tick before the heartbeat and the dashboard's
        # dead-man's switch then reads a live runner as dead (issue #95). Guard with isinstance(str)
        # exactly like tidy.closable / retry_runaway / snapshot(ist) do for this same
        # fail-open-on-wrong-typed defect class: a non-str status fails CLOSED to not-settled (the
        # idle/frozen tiers then still evaluate — their response is a safe peek, never a blind action),
        # and the skip is surfaced ONCE per corrupt id via the same emitted-dedup the tiers use (a
        # bounded record naming the id, never a silent swallow). A genuinely absent None is normal cold
        # state, not corruption, so it is not flagged; it stays hashable and reads as not-settled.
        status = s.get("status")
        if isinstance(status, str) or status is None:
            settled = status in SETTLED_STATUSES
            em.discard((sid, "corrupt_status"))    # well-typed now -> un-latch so a re-corruption re-fires
        else:
            emit({"type": "corrupt_status", "id": sid}, (sid, "corrupt_status"))
            settled = False
        act = s.get("activity_mtime")
        in_wake_grace = wake_grace_until is not None and n is not None and n < wake_grace_until
        frozen = idle = False
        if (s.get("launched") and not resolved and not settled and act is not None
                and n is not None and not in_wake_grace):
            stale = n - act
            if stale >= freeze_secs:
                frozen = True
            elif stale >= idle_secs and not s.get("awaiting"):
                idle = True
        if frozen:
            emit({"type": "frozen", "id": sid}, (sid, "frozen"))
            em.discard((sid, "idle"))        # frozen supersedes a pending idle
        elif idle:
            emit({"type": "session_idle", "id": sid}, (sid, "idle"))
        else:
            em.discard((sid, "idle"))
            em.discard((sid, "frozen"))
    return events, em


def _event_key(ev):
    """The dedup key for an event (so the runner can un-commit it if its durable write fails)."""
    t, i = ev.get("type"), ev.get("id")
    if t == "session_finished":
        return (i, "finished", ev.get("report_token"))
    if t == "session_blocked":
        return (i, "blocked", ev.get("blocked_token"))
    if t == "session_exited":
        return (i, "exited", ev.get("exited_token"))
    if t == "session_idle":
        return (i, "idle")
    if t == "frozen":
        return (i, "frozen")
    if t == "corrupt_status":
        return (i, "corrupt_status")
    return None


def emitted_from_events(event_dicts):
    """Rebuild the dedup set from event payloads on disk (events/ + processed/) so a RESTARTED
    runner doesn't re-emit a token-keyed event it already recorded. Edge events (idle/frozen)
    are deliberately NOT rebuilt: a still-stuck session SHOULD re-alert after a restart, and
    the response is a safe peek, never a blind action."""
    out = set()
    for ev in event_dicts:
        t = ev.get("type")
        i = ev.get("id")
        if t == "session_finished" and ev.get("report_token") is not None:
            out.add((i, "finished", ev["report_token"]))
        elif t == "session_blocked" and ev.get("blocked_token") is not None:
            out.add((i, "blocked", ev["blocked_token"]))
        elif t == "session_exited" and ev.get("exited_token") is not None:
            out.add((i, "exited", ev["exited_token"]))
    return out


def reconcile_emitted(emitted, marker_hashes):
    """Make a restart-rebuilt dedup set reflect CURRENT disk (review D1). emitted_from_events()
    rebuilds finished/blocked token keys from the never-pruned processed/ events, which would
    re-latch a key the in-loop un-latch already dropped — so after a runner restart a re-created
    (even identical) report/blocked marker would never re-fire, and a re-blocked session would be
    silently stuck. Drop any finished/blocked key whose marker is now absent or whose content
    changed; keep one whose marker is still present unchanged (so we don't re-gate on restart).
    marker_hashes maps (id, 'finished'|'blocked') -> current sha1 (or None if the file is gone)."""
    out = set()
    for k in emitted:
        if len(k) == 3 and k[1] in ("finished", "blocked"):
            if marker_hashes.get((k[0], k[1])) == k[2]:
                out.add(k)
        else:
            out.add(k)        # exited keys: a new occurrence gets a new mtime token, so safe to keep
    return out


def next_seq(existing_names):
    """R7: next event sequence number, given every name ever seen (events/ + processed/)."""
    nums = []
    for n in existing_names:
        base = n.split(".")[0]
        if base.isdigit():
            nums.append(int(base))
    return (max(nums) + 1) if nums else 1


# The processed/ dir accumulates one file per event forever, and BOTH next_seq() and the restart
# rebuild scan it — so their cost grows with total history (issue #41). These bound the HOT dir: once
# it exceeds PROCESSED_CAP, the oldest are archived down to PROCESSED_KEEP newest. KEEP is a generous
# live-marker buffer — the restart rebuild only needs processed events whose report/blocked marker
# still exists (reconcile_emitted drops the rest), and an issue emits ~no new events once it settles,
# so KEEP events of other-issue churn cannot elapse within one issue's finish->merge window; even if
# it somehow did, the only cost is a safe re-emit (idempotent re-gate), never lost data.
PROCESSED_CAP = 1000
PROCESSED_KEEP = 500


def _seq_of(name):
    base = name.split(".")[0]
    return int(base) if isinstance(base, str) and base.isdigit() else None


def processed_overflow(names, cap=PROCESSED_CAP, keep=PROCESSED_KEEP):
    """Given every filename in events/processed/, the OLDEST names to move to the archive so the hot
    dir (next_seq + rebuild scan it) stays bounded — its listing cost, and thus next_seq's, becomes
    independent of total history. [] until the dir exceeds `cap` (batched: archives down to `keep`
    newest, not one-file-per-tick). Only the oldest (lowest seq) names are ever returned, so the
    newest file — the global-max seq — always stays hot and next_seq keeps returning max+1, monotonic
    and collision-free. PURE and fail-closed: a non-collection input archives nothing; non-str /
    non-numeric names never raise and sort first (treated as oldest, archived preferentially)."""
    if not isinstance(names, (list, tuple, set, frozenset)):
        return []
    strs = [n for n in names if isinstance(n, str)]
    if len(strs) <= cap:
        return []
    # ascending: non-numeric first (None -> sort-key False), then by numeric seq — oldest at the head
    strs.sort(key=lambda n: (_seq_of(n) is not None, _seq_of(n) or 0, n))
    drop = len(strs) - keep
    return strs[:drop] if drop > 0 else []


def retry_runaway(issues_state, threshold=4):
    """Pure (fact-4): issue ids whose mechanically-stamped retries have blown far past the
    retry cap (2) — a doom-looping relaunch cycle must be loud (state/ALERT, Task 10).
    threshold = cap + slack (a William-released re-run is legit). Tolerates a wrong-typed
    issues.json (fail closed = report nothing) — a corrupt state file must never kill a tick;
    that includes a wrong-typed COUNTER ("4"/None/bool), not just a wrong-typed container
    (Codex cross-review, Task 8 — the fail-open-on-wrong-typed-input defect class again).
    `type(...) is int` deliberately excludes bool (True is an int subclass, not a count)."""
    issues = (issues_state or {}).get("issues") if isinstance(issues_state, dict) else {}
    if not isinstance(issues, dict):
        return []
    return [iid for iid, ist in issues.items()
            if isinstance(ist, dict)
            and type(ist.get("retries", 0)) is int
            and ist.get("retries", 0) >= threshold]


# --------------------------- the §C.3 snapshot reader ---------------------------

def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def snapshot(state_home, issue_ids, issues_state, now):
    """Read the §C.3 markers for each issue id into the snapshot dicts detect_events consumes.
    (autocode's _snapshot, public here: runner.py calls it cross-module each tick.)

    Layout under state_home: reports/<id>.md + state/{activity,blocked,exited,awaiting}/<id>.
    `issues_state` is the loopstate issues.json dict; a wrong-typed/corrupt one yields
    status=None snapshots rather than raising into the tick (fail closed — the settled-status
    idle/frozen suppression then simply doesn't apply, and the response to any resulting event
    is a safe peek, never a blind action).

    exited_token is the marker's mtime_ns (sticky until relaunch clears the file); `launched`
    means the session left SOME trace (activity, report, or exited) — delivery proof lives in
    state/started/ and is launch-session.sh's business, not an event source (EVENT-MODEL)."""
    root = os.fspath(state_home)
    state = os.path.join(root, "state")
    issues = (issues_state or {}).get("issues") if isinstance(issues_state, dict) else {}
    if not isinstance(issues, dict):
        issues = {}
    out = []
    for iid in issue_ids:
        rep = os.path.join(root, "reports", f"{iid}.md")
        blk = os.path.join(state, "blocked", iid)
        ext = os.path.join(state, "exited", iid)
        act = os.path.join(state, "activity", iid)
        awa = os.path.join(state, "awaiting", iid)

        report_mtime = _mtime(rep)
        blocked_mtime = _mtime(blk)
        exited_ns = _mtime_ns(ext)
        activity_mtime = _mtime(act)

        ist = issues.get(iid)
        status = ist.get("status") if isinstance(ist, dict) else None

        launched = activity_mtime is not None or report_mtime is not None or exited_ns is not None
        out.append({
            "id": iid,
            "status": status,
            "launched": launched,
            "activity_mtime": activity_mtime,
            "report_hash": _hash_file(rep) if report_mtime is not None else None,
            "report_mtime": report_mtime,
            "blocked_hash": _hash_file(blk) if blocked_mtime is not None else None,
            "blocked_mtime": blocked_mtime,
            "exited_token": exited_ns,
            "exited_mtime": (exited_ns / 1e9) if exited_ns is not None else None,
            "awaiting": os.path.exists(awa),
            "now": now,
        })
    return out
