"""Which finished sessions may `superlooper tidy` close? PURE selection from state on disk —
no cmux, no gh, no subprocess, no clock — so the safety contract is a unit-test table
(tests/test_tidy.py). The CLI (skill/bin/superlooper `tidy`) does the best-effort close.

`tidy` is William's explicit word, never an automatic path: the V1 'nothing auto-closed'
posture stands (DRYRUN 2026-07-03). A real claude worker idles at its prompt forever after
finishing (D4) and never self-exits, so its cmux window lingers; this decides which lingering
windows are safe to close.

Safety, stated as code below and pinned by tests:
  * Only a status this module can positively NAME as terminal is ever selected (a positive
    allowlist). An in-flight lane ({running,blocked,frozen,exited} — a build in progress or an
    exited session mid-recovery), an in-between gate lane ({gating,holding} — build done, merge
    mechanics still running), a not-yet-started (ready/None) or unknown/typo'd status is NEVER
    selected. This is the fail-OPEN-on-wrong-typed defect class pointing the safe way: when in
    doubt, do NOT close.
  * The taxonomy is imported from actions.py, never re-invented, so tidy can never drift out of
    step with the runner's own notion of terminal vs in-flight.
"""
import actions

# Default scope: sessions of MERGED issues — truly done, safe to close at any time (a merged
# issue is never resurrected; a stray label on it does nothing — see actions.REAPPROVAL_STATUSES).
DEFAULT_STATUSES = frozenset({"merged"})
# `--all` extends to every terminal status. A parked / needs-william / bounced session is
# handed-back-and-idle; closing its window is safe because re-approval relaunches from the issue
# (the launch path frees any stale singleton lock itself — runner.py `_close_stale_session`).
ALL_STATUSES = frozenset(actions.TERMINAL_STATUSES)
# The terminal statuses a fresh `agent-ready` can RE-APPROVE and relaunch (merged is excluded —
# merged work is never rebuilt). tidy uses this to decide state-marker cleanup: a re-approvable
# session may be relaunched by a live runner AT ANY TIME, so tidy closes its window but never
# mutates its pane markers / singleton lock (that lifecycle stays the runner's — see the CLI's
# _close_window). Only a status that can NEVER relaunch (merged) has no concurrent writer, so only
# there is tidy's marker/lock cleanup provably race-free.
REAPPROVABLE = frozenset(actions.REAPPROVAL_STATUSES)


def _iid_num(iid):
    """i<N> -> N, else None (a loopstate key that isn't an issue id is skipped). Mirrors
    actions._iid_num — duplicated (not imported) to keep this pure selector self-contained and
    off a private name."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def closable(issues, windows, *, scope_all=False):
    """[{"id","status"}] for every issue whose status is closable in this scope AND has a
    recorded cmux window, sorted by issue number (deterministic). PURE — no input is mutated and
    a fresh list of fresh dicts is returned every call.

    issues   loopstate['issues']: {iid: {"status": ...}}. Wrong-typed -> nothing selected.
    windows  the iids that have a recorded window (a pane marker on disk). Not a collection ->
             treated as empty -> nothing selected (fail closed).
    scope_all  False = merged only (default); True = every terminal status.
    """
    # `scope_all is True`, not truthiness: a wrong-typed truthy value (e.g. "False") must NOT
    # silently widen to --all — the narrower merged-only default is the fail-closed landing.
    targets = ALL_STATUSES if scope_all is True else DEFAULT_STATUSES
    # Filter windows to str: a list/dict slips past the collection check yet an unhashable ELEMENT
    # would raise inside set() — the contract is "wrong-typed -> skipped, never a raise".
    have_window = ({w for w in windows if isinstance(w, str)}
                   if isinstance(windows, (set, frozenset, list, tuple)) else set())
    issues = issues if isinstance(issues, dict) else {}
    out = []
    for iid in sorted((k for k in issues if _iid_num(k) is not None), key=_iid_num):
        ist = issues.get(iid)
        if not isinstance(ist, dict):
            continue
        status = ist.get("status")
        # `isinstance(status, str)` FIRST: an unhashable wrong-typed status ([], {}) must be
        # skipped, never raise on the `in targets` membership test. Then the positive allowlist
        # AND an explicit in-flight veto — the veto is redundant while TERMINAL/INFLIGHT stay
        # disjoint, but it makes the never-close-a-live-lane property local and obvious, and
        # survives a future edit that mis-files a status into both sets.
        if (isinstance(status, str) and status in targets
                and status not in actions.INFLIGHT_STATUSES and iid in have_window):
            out.append({"id": iid, "status": status})
    return out


def reclaimable_worktrees(issues, worktree_ids):
    """[iid] for every PARK-FAMILY terminal issue (parked / needs-william / bounced) that still has a
    worktree dir on disk — the set the runner may safely `git worktree remove` to bound long-run disk
    growth (issue #41). PURE — no input mutated; a fresh sorted list every call.

    Same fail-closed safety as closable(): a positive REAPPROVABLE allowlist AND an explicit
    in-flight veto, so an in-flight lane ({running,blocked,frozen,exited}) or an in-between gate lane
    ({gating,holding}) or a not-yet-started/unknown status is NEVER reclaimed — its worktree is a
    LIVE lane still being written. Reclaiming a park-family worktree is safe: re-approval rebuilds
    from the issue on a fresh branch (runner _exec_reapprove/_exec_launch recreate it), and the
    committed work is preserved on the branch ref (worktree_remove drops only the checkout). merged is
    DELIBERATELY EXCLUDED — it stays on the existing merge-time removal path and its own
    cleanup_merged_worktrees gate, so this sweep never overrides that config.

    issues        loopstate['issues']: {iid: {"status": ...}}. Wrong-typed -> nothing selected.
    worktree_ids  iids that have a worktree dir on disk. Not a collection -> empty -> nothing selected.
    """
    have = ({w for w in worktree_ids if isinstance(w, str)}
            if isinstance(worktree_ids, (set, frozenset, list, tuple)) else set())
    issues = issues if isinstance(issues, dict) else {}
    out = []
    for iid in sorted((k for k in issues if _iid_num(k) is not None), key=_iid_num):
        ist = issues.get(iid)
        if not isinstance(ist, dict):
            continue
        status = ist.get("status")
        if (isinstance(status, str) and status in REAPPROVABLE
                and status not in actions.INFLIGHT_STATUSES and iid in have):
            out.append(iid)
    return out
