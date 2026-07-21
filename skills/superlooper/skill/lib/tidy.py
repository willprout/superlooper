"""Which finished sessions may `superlooper tidy` close? PURE selection from state on disk —
no cmux, no gh, no subprocess, no clock — so the safety contract is a unit-test table
(tests/test_tidy.py). The CLI (skill/bin/superlooper `tidy`) does the best-effort close.

`tidy` is William's explicit word for closing FINISHED windows on demand (it closes the window
only — it never prunes a worktree). The runner also auto-closes some windows on its own. Owner
ruling 2026-07-16 (#168) governs the #149-family teardowns: a lane that SUCCESSFULLY MERGED and
landed auto-closes (gated by `auto_close_merged_windows`, default on), and the park-family reaper
is now strictly OPT-IN (`cleanup_parked_worktrees`, default off). By default the runner NEVER
auto-closes a parked / needs-william / bounced window while its session is live — the owner must
be able to open that stalled work and look at the session, so its window AND worktree persist
until an owner verb resolves the lane. (Separately, the #163 exit-clean question hand-back closes
an awaiting-answer window, but only AFTER the worker has already EXITED and pushed its WIP, and it
PRESERVES the worktree — there is no live session left to inspect.) This supersedes the V1
'nothing auto-closed' posture (DRYRUN 2026-07-03), written before the D14 forensics forced the
ordered teardown (#149). A real claude worker idles at its prompt forever after finishing (D4)
and never self-exits, so its cmux window lingers; this decides which lingering windows `tidy` is
safe to close on the owner's word (merged by default; --all extends to the park family, which the
runner never touches automatically by default).

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


def _aid_num(aid):
    """a<N> -> N (N >= 1), else None. The runner allocates aids from a1 upward (actions.decide /
    _exec_hire_answerer; next_answerer starts at 1), so `a0` is not a shape it ever produces — a
    stray `a0` marker is corruption and is skipped, matching the positive-allowlist doctrine (only
    act on a provably-real answerer id). Anything else (wrong-typed, `aX`, bare `a`) is skipped too."""
    if isinstance(aid, str) and aid.startswith("a") and aid[1:].isdigit():
        n = int(aid[1:])
        return n if n >= 1 else None
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


def closable_answerers(answerers, next_answerer, windows):
    """[{"id","status":"finished"}] for every answerer window (a<N> pane marker) whose answerer is
    PROVABLY FINISHED — its hire completed and its lifecycle has since ended. Sorted by answerer
    number (deterministic). PURE — no input is mutated and a fresh list of fresh dicts is returned
    every call. (Issue #132: an answerer hired by the Discuss/blocked flow is not a tracked issue,
    so closable() never sees it and its window lingers after its Q&A concludes.)

    An answerer's record lives in `answerers` for exactly the span it is ACTIVE: the runner writes
    it at hire (_exec_hire_answerer) and POPS it the instant the lifecycle ends — delivery, park
    (timeout / escalation / hire-cap), owner-absorb, re-approval (runner.py, four pop sites). So the
    map holds EXACTLY the currently-active answerers and "finished" is the ABSENCE of the record. The
    safety is a clock-free high-water mark: next_answerer is bumped to N+1 ATOMICALLY with writing
    a<N>'s record (one loopstate.update), and launch-session.sh writes a<N>'s pane marker only AFTER
    delivery verifies — strictly BEFORE that atomic write. So in the ONLY window where a<N> has a
    marker but no record (mid-hire, or a hire that failed and re-hires the SAME aid), next_answerer
    has NOT yet passed N and `N < next_answerer` is False: a mid-hire answerer is NEVER selected.
    Once the hire completes (counter past N) and the answerer later terminates (record popped), both
    conditions hold and the idle window is closable. aids are allocated monotonically and never
    reused, so a selected a<N> can never relaunch — closing its window AND clearing its marker are
    both permanently race-free (unlike a re-approvable issue, whose markers stay the runner's).

    Fail-closed, mirroring closable() — "wrong-typed / unprovable -> do not close":
      * `next_answerer` not a real positive int (bool excluded, float/str/None/missing) -> nothing is
        `< next_answerer` -> nothing selected.
      * `answerers` not a dict -> we CANNOT prove any a<N> is inactive -> nothing selected (a NON-dict
        fails closed; an empty {} is the normal all-delivered case and is fine). A record with any
        value type protects its aid (key-presence is the active test, not value shape).
      * `windows` not a collection, or a wrong-typed / unhashable element -> that element is skipped,
        never a raise.

    answerers      loopstate['answerers']: {aid: {"for": iid, ...}} — the active set.
    next_answerer  loopstate['next_answerer']: the monotonic high-water aid counter.
    windows        the a<N> ids that have a recorded pane marker on disk.
    """
    # `bool` is an int subclass but never a real counter — exclude it, or True would read as 1.
    hw = next_answerer if isinstance(next_answerer, int) and not isinstance(next_answerer, bool) else 0
    if not isinstance(answerers, dict):
        return []                                  # cannot read the active set -> close nothing
    active = {a for a in answerers if isinstance(a, str)}
    have_window = ({w for w in windows if isinstance(w, str)}
                   if isinstance(windows, (set, frozenset, list, tuple)) else set())
    out = []
    for aid in sorted((w for w in have_window if _aid_num(w) is not None), key=_aid_num):
        if aid not in active and _aid_num(aid) < hw:
            out.append({"id": aid, "status": "finished"})
    return out


def reclaimable_worktrees(issues, worktree_ids):
    """[iid] for every PARK-FAMILY terminal issue (parked / needs-william / bounced) that still has a
    worktree dir on disk — the set the runner may safely `git worktree remove` to bound long-run disk
    growth (issue #41). PURE — no input mutated; a fresh sorted list every call.

    Same fail-closed safety as closable(): a positive REAPPROVABLE allowlist AND an explicit
    in-flight veto, so an in-flight lane ({running,blocked,frozen,exited}) or an in-between gate lane
    ({gating,holding}) or a not-yet-started/unknown status is NEVER reclaimed — its worktree is a
    LIVE lane still being written. Reclaiming a park-family worktree is safe: re-approval rebuilds
    from the issue on a fresh branch — _exec_reapprove rotates the stamp to the next unburned
    generation and _exec_launch recreates the worktree off origin/<dev> (#177) — and the committed
    work is preserved on the RETIRED branch ref (worktree_remove drops only the checkout). merged is
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
