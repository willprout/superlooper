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
    allowlist). An in-flight lane ({running,frozen,exited} — a build in progress or an
    exited session mid-recovery), an in-between gate lane ({gating,holding} — build done, merge
    mechanics still running), a not-yet-started (ready/None) or unknown/typo'd status is NEVER
    selected. This is the fail-OPEN-on-wrong-typed defect class pointing the safe way: when in
    doubt, do NOT close.
  * The taxonomy is imported from actions.py, never re-invented, so tidy can never drift out of
    step with the runner's own notion of terminal vs in-flight.

**A `t<N>` TRIAGE FLIGHT IS ALSO TIDY'S SUBJECT (issue #463), and it is selected by a different
rule** — stated here rather than left to a pattern, because inheriting one is exactly what went
wrong. The launcher records `state/panes/t<N>` and `state/worker.t<N>.lock` for a flight (#448)
under the same directories this module's caller walks, but a flight deliberately has NO lane record
in `issues.json`: it belongs to no issue, opens no branch and merges nothing. So `closable()` — a
selector that reads a lane's STATUS — can say nothing about one, and a flight selected by it would
be a flight selected by accident.

The flight's own two readings are these, and both are positive facts on disk:

  * its RUN IS CLOSED — `superlooper triage-finish` journaled a finish for it (`triage.finished_
    flights`). That is the flight's equivalent of `merged`: the work completed, the sitting sheet
    is written, and the session sitting in front of its prompt is the D4 idler tidy exists for. It
    is in the DEFAULT scope.
  * its SESSION IS GONE — no live pid holds `worker.t<N>.lock`. It died mid-run, so there is no
    sitting sheet and the day's log stops wherever it stopped: the park-family shape, handed back
    rather than completed, and so `--all` scope exactly as a parked lane is.

A flight that is neither — working the queue right now — is never selected, by either scope.

What tidy still does NOT do for a flight is prune its checkout: this module closes windows, and the
`<run root>/worktrees/<t-id>` a flight gets in the non-default `worktree` home is reclaimed by the
runner once the session is gone (`reclaimable_flight_worktrees` below is that selector). Splitting
it that way keeps the owner verb free of any prune at all.
"""
import actions
import triage

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
    in-flight veto, so an in-flight lane ({running,frozen,exited}) or an in-between gate lane
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


# --------------------------------------------------------------------------- the triage flight

# What the listing PRINTS in the status column where a lane prints `merged` / `parked`. Prefixed,
# because they are not lane statuses and a column that mixed the two vocabularies would invite the
# next reader to look one of these up in `actions.TERMINAL_STATUSES` and find nothing.
FLIGHT_DONE = "flight:done"       # closed its own run — the default scope, as `merged` is
FLIGHT_ENDED = "flight:ended"     # its session is gone and it never closed — `--all`, as parked is


def _flight_ids(ids):
    """The `t<N>` ids in `ids` as a set, or **None** when `ids` is not a readable collection at all.

    THE narrowing, so every widening here adds a CLASS and does not open the pattern; the shape
    itself is `triage`'s to spell, not ours. A lane id, a `.ws` sidecar and a wrong-typed member are
    simply dropped.

    The None is the part that matters, and it is not the shape `closable` uses. There, an unreadable
    `windows` collapses to empty and empty means NOTHING IS SELECTED — fail-closed by arithmetic.
    Here one of the arguments is `live`, and for that one empty means MORE is selected, so folding
    an unreadable value into it would fail OPEN: a garbage liveness view would read as "no flight is
    running" and offer a working flight's window for closing. Callers below refuse outright instead.
    """
    if not isinstance(ids, (set, frozenset, list, tuple, dict)):
        return None
    return {i for i in ids if triage.is_flight_id(i)}


def _flight_num(tid):
    return int(tid[1:])


def closable_flights(flight_ids, *, finished=(), live=(), scope_all=False):
    """[{"id","status"}] for every TRIAGE FLIGHT whose session window tidy may close, sorted by
    flight number. PURE — nothing is mutated and a fresh list of fresh dicts comes back every call.

    flight_ids  the ids that have a recorded window (`state/panes/<id>`), unnarrowed — this
                narrows. Not a collection -> nothing selected.
    finished    the flights whose run is CLOSED (``triage.finished_flights``).
    live        the flights whose `worker.<id>.lock` names a LIVE pid.
    scope_all   False (default) = finished flights only; True also takes a flight whose session is
                gone and that never closed its run.

    The two scopes and their reasoning are in the module docstring. The safety property here is the
    same one `closable` has, arrived at from the other direction: a flight is selected only on a
    reading this function can positively NAME, so a flight still working the queue — live, and not
    finished — is selected by neither scope and by no fall-through.

    Note what is deliberately NOT a veto: a live pid does not protect a FINISHED flight, because
    that is the exact session tidy exists to close (D4 — it never self-exits). The veto that keeps
    a working flight safe is the absence of a positive reading, not its liveness.
    """
    ids, done, alive = _flight_ids(flight_ids), _flight_ids(finished), _flight_ids(live)
    if ids is None or done is None or alive is None:
        # ALL THREE, uniformly: none of the three readings has a safe default. A garbage `live`
        # would fail open (see `_flight_ids`); a garbage `finished` or `flight_ids` would fail
        # closed on its own, and refusing them here too costs nothing and spares the next reader
        # having to work out which is which.
        return []
    out = []
    for tid in sorted(ids, key=_flight_num):
        if tid in done:
            out.append({"id": tid, "status": FLIGHT_DONE})
        elif scope_all is True and tid not in alive:
            # `scope_all is True`, not truthiness — the same fail-closed landing `closable` takes:
            # a wrong-typed truthy value must not silently widen the scope.
            out.append({"id": tid, "status": FLIGHT_ENDED})
    return out


# How long a flight's checkout must have EXISTED before "no live lock" may be read as "no session"
# (fresh-agent review, P0). The launcher creates the checkout FIRST and only then opens the pane;
# `start-session.sh` takes `worker.<id>.lock` inside that new pane, a whole delivery-verify window
# later. Between the two there is a real interval in which a flight is being born and holds no lock
# at all, and a sweep that read that as "dead" would prune the checkout out from under a session it
# was still being launched into — the D14 shape, arriving through the one door the liveness veto
# does not cover.
#
# The interval to cover is bounded by the launch shim's own ceiling: `superlooper triage-flight`
# runs the launcher under a 180s subprocess timeout, after which either the lock exists or the
# launcher has torn the pane down. Half an hour is an order of magnitude past that, and costs
# nothing against the one-a-day cadence that produces these checkouts — a flight's is still
# reclaimed on the first tick after its session ends, or the next morning at the latest.
#
# The AGE itself is impure (a stat and a clock), so it is read by the callers — `gitops.checkout_age`
# is the one spelling, fail-closed to "brand new" on anything it cannot read — and arrives here as
# the `launching` set. This module stays a pure selector.
FLIGHT_LAUNCH_GRACE_SECONDS = 30 * 60


def reclaimable_flight_worktrees(worktree_ids, *, live=(), launching=()):
    """[t<N>] for every flight CHECKOUT on disk whose session is gone — the set the runner may
    `git worktree remove` to bound disk growth (issue #41). PURE; a fresh sorted list every call.

    A repo that sets `triage.home: worktree` gets its flight a detached checkout at
    `<run root>/worktrees/<t-id>` (#448). Nothing reclaimed it, and nothing could: the sibling
    selector above walks `issues.json`, and a flight has no record there by design — so every
    flight's checkout accumulated on disk forever, one per day, on exactly the repos that chose that
    home for privacy reasons. That is the growth #41 exists to bound, reproduced one session class
    over.

    Reclaiming one is safe in a way a lane's checkout never is. The #190 rule protects a worker's
    ONLY copy of its output; a flight commits nothing, pushes nothing and writes nothing there — its
    whole product is on GitHub and in its own state home (verdicts, run log, journal), none of which
    lives in this directory. The runner still prunes it through the guarded teardown, so a checkout
    that somehow holds work is refused and kept rather than dropped on that argument.

    TWO VETOES, and neither is the test the window selector uses. A finished flight's WINDOW may be
    closed while its CLI still idles; its checkout may not be removed then, because that directory is
    the live CLI's cwd and pruning it is the D14 shape — the next hook spawn dies in posix_spawn
    before it can run a line. So a flight's checkout waits for its session to actually end, however
    it ends: the owner's `tidy`, a restart, or the session's own exit.

    The second veto is the one the first cannot give, and it is why "no lock" alone is not evidence:
    the lock does not exist yet during a launch. See :data:`FLIGHT_LAUNCH_GRACE_SECONDS`.

    worktree_ids  the names on disk under `<run root>/worktrees`. Not a collection -> empty.
    live          the flights whose `worker.<id>.lock` names a LIVE pid.
    launching     the flights whose checkout is younger than the launch grace — including any whose
                  age could not be read at all, which must fail toward "a launch may be reaching it".
    """
    on_disk, alive = _flight_ids(worktree_ids), _flight_ids(live)
    young = _flight_ids(launching)
    if on_disk is None or alive is None or young is None:
        # An unreadable liveness or launch view proves nothing dead, so nothing is provably safe to
        # prune — the same refusal, for the same reason, as the window selector's.
        return []
    return sorted((t for t in on_disk if t not in alive and t not in young), key=_flight_num)

