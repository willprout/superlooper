"""Which eligible issues to launch NOW — a pure function over the queue + lane state + usage.

Three gates, in order:
  1. USAGE, fail closed (ported from autocode, RC-USAGEFAILOPEN): unknown / stale / unhealthy /
     partial / malformed usage, or over the five-hour / seven-day ceilings, launches NOTHING.
     v1's `or 0` coercion once read a partial payload as "0% used" and launched over quota; a
     NaN pct would sneak past a bare `>` comparison (NaN > 96 is False), so pcts must be FINITE.
     A read with no five-hour window at all is not partial (issue #533): that ceiling is simply not
     in effect, and the seven-day one still gates.
  2. LANES: never exceed capacity; never launch the same id twice. `lanes` is EITHER a plain
     integer (one shared pool of N — today's behaviour) OR {"build": N, "investigate": M}: two
     STRICT pools, N for merge-producing work plus M RESERVED for investigations, with no
     cross-pool borrowing in either direction (issue #63). See _lane_free / _lane_pool.
  3. ANTI-AFFINITY: under hard affinity, a merge-producing issue whose declared touch-areas
     overlap a merge-producing running lane, held territory claim, or one already selected this
     tick is HELD.
     Investigations do not produce PRs or merges, so they are exempt in both directions. While
     merges are FROZEN, the standing-rule restore-green fix is exempt from all of it — finished-
     but-unmerged territory claims (issue #294: they cannot release while the mainline it exists to
     fix is frozen) AND running lanes (issue #519: a running lane stuck where nobody is watching
     need not end either). On a green mainline it is held like anything else. Under soft affinity
     the overlap is allowed and flagged (symmetrically) for the journal.

Eligibility itself (agent-ready, valid type, blocked-by closed) lives in issues.eligible.
`launch_ok` welds that to the usage rule as the ONE gate every path that starts or restarts a
session asks — fresh launch, crash recovery, restart orphan resume, conflict relaunch (issue #150).
"""
import math

import gate
import issues

FIVE_HOUR_LAUNCH_CEILING = 90    # spec: >90% five-hour -> no fresh launch
SEVEN_DAY_NEW_WORK_CEILING = 96  # spec: >96% seven-day -> no new work


def _finite_pct(x):
    """A usable utilization percentage: a finite real number, not a bool (True == 1 would slip
    past the ceiling), not NaN/inf (NaN > ceiling is False and would fail OPEN)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _usage_ok(usage):
    """FAIL CLOSED: only a dict with 'ok' auth, non-stale, a FINITE seven-day pct under its ceiling
    and a five-hour pct that is either FINITE and under its ceiling or None passes. Any other missing
    / partial / unhealthy / malformed usage (None, wrong type, NaN, ...) returns False (launch
    nothing).

    Why five-hour may be None (issue #533): the usage endpoint can answer 200 with no five-hour
    window. usage.py reports that as `ok` with five_hour_pct None, meaning that ceiling is not in
    effect, not that the figure is unknown. A five-hour value that is present but unusable ("10",
    NaN, True) is still malformed and still launches nothing.

    ONE exception, FAIL OPEN (issue #46): when the caller sets `fail_open`, launch. The scheduler
    never decides this itself — it is a typed, understood flag that decide() sets ONLY after the
    meter has been UNREADABLE past a bounded grace (a dark meter: TLS/Keychain/auth outage, not a
    fresh read). Launching into maybe-exhausted quota, where the sessions hit the wall themselves
    (and #24's systemic breaker catches a real collapse), is a better failure than a full stop with
    real usage low. This is DELIBERATELY not the refused≠empty / fail-open-on-wrong-typed defect
    class: decide sets fail_open on a typed, understood status past a measured grace — it is never
    set for a meter that is FRESHLY reading, so the exhausted-read gate below is untouched (a FRESH
    'ok' at/over the ceiling still launches nothing). NB: a LAST-known-over-ceiling read that then
    goes dark past the grace DOES fail open — by design; once the meter is dark we no longer trust
    that stale pct, and the owner's ruling is that launching beats a full stop (#24 backstops a real
    collapse)."""
    if not isinstance(usage, dict):
        return False
    if usage.get("fail_open"):
        return True
    if usage.get("auth_status") != "ok" or usage.get("stale"):
        return False
    fh = usage.get("five_hour_pct")
    sd = usage.get("seven_day_pct")
    if not _finite_pct(sd):
        return False
    if fh is not None and not _finite_pct(fh):
        return False
    if sd > SEVEN_DAY_NEW_WORK_CEILING:
        return False
    if fh is not None and fh > FIVE_HOUR_LAUNCH_CEILING:
        return False
    return True


def usage_ok(usage):
    """Public face of the fail-closed usage rule (Task 10): the runner's relaunch-tier recovery
    gates on the SAME rule as fresh launches — one rule, one name, so the two paths can never
    drift apart."""
    return _usage_ok(usage)


def launch_ok(parsed, closed_nums, frozen, usage, resume=False, config=None):
    """THE launch-eligibility gate (issue #150 / D8) — the ONE predicate every path that starts or
    restarts a session must pass: usage headroom (fail closed) AND issues.eligible (approved, valid
    type, no control-label conflict, every `blocked-by` closed) AND, when `config` is supplied, the
    foreseeable-referee gate (issue #165).

    It exists because those two halves used to be asked separately, by different callers, in
    different combinations: fresh phase-E launches asked both, crash recovery asked only usage, and
    the restart orphan resume plus the conflict relaunch asked NEITHER — so a recovery could start a
    worker straight past an open blocker, contained only by the worker's own step-0 reconcile
    bounce. Every caller now asks THIS, so the paths cannot drift: a condition added here binds
    recovery the same tick it binds a fresh launch.

    Foreseeable-referee gate (issue #165): an issue whose DECLARED touches resolve to a referee path
    (.superlooper/**, .github/workflows/**) can ONLY ever end in a needs-owner park at the gate, so
    launching it unattended just burns a lane to reach a certain stop. It is refused UNLESS the owner
    pre-authorized it up front (the `pre-authorized:referee` label). This is a LANE-economy gate, not
    the bright line: it only fires when `config` (needed to resolve touch areas -> globs) is passed,
    and when it is skipped the diff-time referee park in gate_decision still refuses the merge — so a
    caller without config degrades to the old burn-a-lane-then-park behaviour, never to auto-merge.

    Fails CLOSED on a missing or wrong-typed `parsed` (the recovery paths can reach this for an
    issue absent from the GitHub view): with no issue to read, not one of the conditions can be
    affirmed, and an unverifiable start is exactly what this gate exists to refuse. `_exec_launch`
    already applies this same rule to fresh launches ("skipped: issue not in the current GitHub
    view") — recovery now matches it rather than relaunching blind.

    See issues.eligible for `resume`, which changes only WHICH label counts as the approval."""
    if not _usage_ok(usage):
        return False
    if not isinstance(parsed, dict):
        return False
    try:
        if not issues.eligible(parsed, closed_nums, frozen, resume=resume):
            return False
    except (KeyError, TypeError):        # a malformed parse (missing/wrong-typed labels, type, ...)
        return False                     # -> unreadable eligibility -> fail closed, never launch
    if config is not None and gate.foreseeable_referee_stop(parsed.get("touches"), config) \
            and not gate.preauthorized_referee(parsed.get("labels")):
        return False                     # a certain, un-authorized referee park -> wait for the owner
    return True


def _is_wildcard(touches):
    """Does this declaration behave as the wildcard '*' — i.e. overlap EVERY lane? An empty
    declaration (an issue of unknown scope) is treated as '*', and a literal '*' is one too. This
    is the single predicate behind both the safe-default serialization AND the issue #36 journaling
    that explains it: a wildcard is exactly why 'only one lane is busy'."""
    a = {t for t in touches if isinstance(t, str)} if isinstance(touches, (list, set, tuple)) else set()
    return (not a) or ("*" in a)


def _geometric_overlap(touches_a, touches_b):
    """Do two sets of declared areas share ground? The wildcard '*' (a path in no declared area)
    overlaps everything; an EMPTY declaration is treated as '*' too — an issue of unknown scope
    conflicts with any lane, the safe conservative default (a no-touches issue under hard affinity
    runs alone rather than risk a same-area collision)."""
    a = set(touches_a) or {"*"}
    b = set(touches_b) or {"*"}
    if "*" in a or "*" in b:
        return True
    return bool(a & b)


def overlaps(touches_a, touches_b, affinity):
    """Do two issues' declared touch-areas conflict for CO-SCHEDULING under this affinity?
    'hard' -> a geometric overlap BLOCKS (returns True). 'soft' -> overlaps never block (returns
    False); the overlap is journaled by the scheduler, not prevented."""
    if affinity != "hard":
        return False
    return _geometric_overlap(touches_a, touches_b)


def _merge_affinity_subject(itype):
    return itype != "investigate"


def _restore_green(parsed):
    """Is this the standing-rule restore-green fix (spec §4.4)? Delegates to gate.restore_green so
    the LAUNCH exemption here (#294/#519) and the gate's FREEZE exemption (#295) read the same fact the
    same way — two exemptions that disagreed about which issue is the standing-rule fix would put
    the loop right back in a half-escaped deadlock."""
    return gate.restore_green(parsed.get("labels"))


def _freeze_exempt_launch(candidate, frozen):
    """Issues #294 + #519 — while merges are FROZEN, the standing-rule restore-green fix ignores
    every occupied territory: finished-but-unmerged claims, running lanes, and lanes launching this
    same tick. It still needs a free lane (the caller checks capacity first), and it still occupies
    `*` once selected, so everything after it is held exactly as before.

    #294 — the red-mainline deadlock. A territory claim outlives its lane through gating/holding and
    releases only on MERGE; a red mainline FREEZES merges, so those claims accumulate and never
    release. The fix declares `touches: *` (its scope is genuinely unknown — whatever broke the
    check), which overlaps every one of them, so it was blocked by the very PRs that were blocked by
    it. That exemption buys the fix a LAUNCH, the first half of the escape; its PR then crosses the
    freeze at gate_decision step 4 (issue #295), so a dev-check freeze lifts itself: filed ->
    launched (here) -> built -> merged -> dev greens -> unfreeze.

    #519 — RUNNING lanes too. #294 left them blocking on the premise "a running lane always ENDS —
    sessions have caps". #518 refuted it: eApp lane i1031 sat on a startup dialog the recovery
    ladder could not clear, ended only when a human pressed Escape, and the fix waited 32 minutes
    behind it with a lane free. Owner ruling (#518 option a): ALL running lanes, not a sensed-stuck
    subset — that subset would lean on the same session-state sensing that misread i1031 (and the
    workers in #513). A lane launching this same tick is a running lane by the time the tick's
    actions land, so it stands aside the same way; holding the fix one tick behind it would only
    delay the identical launch.

    Why overlapping a live lane is safe here: lanes build in separate worktrees, so there are never
    two writers in one tree; and the gate's overlap step (touch_verdict) holds the OTHER lane's merge
    while this fix is in flight — #518 shows it: i1031's PR held behind in-flight i1044 and merged
    after the fix. The cost is a rebase, and a rebase beats a mainline held red.

    What this does NOT bound: the fix's OWN merge. That same overlap step holds the fix's PR behind
    any lane still in flight when it finishes, if the fix's diff overlaps that lane's declared
    touches or maps to `*`, and it runs before the freeze step the fix crosses. A lane that stays
    stuck can therefore still hold the mainline red one stage later — owner decision filed as #522.

    Why `frozen` gates it: the whole justification is a mainline that cannot release territory. On a
    green mainline claims release on their own within a tick or two, and a fix issue that outlived
    its breakage has no reason to jump an overlapping lane."""
    return bool(frozen) and _restore_green(candidate)


def _anti_affinity_blocks(candidate, occupied, affinity):
    if not _merge_affinity_subject(candidate.get("type")):
        return False
    if not _merge_affinity_subject(occupied.get("type")):
        return False
    return overlaps(candidate.get("touches", []), occupied.get("touches", []), affinity)


def _clean_touches(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _occupied_from(records):
    out = []
    seen = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "touches": _clean_touches(rec.get("touches")),
                    "type": rec.get("type")})
    return out


def _lane_pool(entry):
    """Which capacity pool a running lane or candidate draws on (issue #63). An investigation goes
    to the reserved 'investigate' pool; everything else — build, diagnose-and-fix, or an
    unknown/missing type — draws on the 'build' pool. Mirrors `_merge_affinity_subject`: the
    build pool is exactly the set anti-affinity governs, the strict sequential merge-producing side.
    A merge-producing issue therefore never lands in the investigation pool — the reservation."""
    return "investigate" if entry.get("type") == "investigate" else "build"


def _lane_free(config, lane_state):
    """Per-pool free lane counts. Returns (free, pooled):
      free   — {pool_name: available_slots}
      pooled — True when `lanes` is the reserved-pool object, False for the plain-integer form.
    Integer form (back-compat): ONE shared pool 'all' of N slots — every running lane consumes from
    it and every candidate draws on it, byte-for-byte today's arithmetic (max(0, N - len(state))).
    Object form (issue #63): two STRICT pools — 'build' (all merge-producing work) and 'investigate'
    (reserved) — with NO cross-pool borrowing in either direction. Everything degrades safely rather
    than raising (the scheduler is total on garbage): a MISSING `lanes` uses the default 1 (via
    config.get); a PRESENT-but-garbage value (wrong type, negative) fails CLOSED to zero capacity —
    launch nothing — dominating the old `max(0, N - len)` for every value validation would reject."""
    raw = config.get("lanes", 1)
    if isinstance(raw, dict):
        caps = {p: _nonneg_int(raw.get(p)) for p in ("build", "investigate")}
        used = {"build": 0, "investigate": 0}
        for lane in _occupied_from(lane_state):
            used[_lane_pool(lane)] += 1
        return {p: max(0, caps[p] - used[p]) for p in caps}, True
    n = _nonneg_int(raw)                    # present-but-garbage -> 0 -> launch nothing (fail closed)
    return {"all": max(0, n - len(lane_state))}, False


def _nonneg_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _plan(parsed_issues, lane_state, config, usage, closed_nums, frozen, territory_claims=None):
    """Shared core of the launch decision. Returns (selected, holds):
      selected — the parsed issues chosen to launch this tick, in priority order.
      holds    — the eligible candidates a hard-affinity overlap SUPPRESSED *while a lane was still
                 free* (affinity was the binding constraint, not the lane cap). Each is
                 {"p": parsed_issue, "blocker": occupied_entry}. A candidate the lane cap alone
                 stopped (encountered after `free` were already chosen) is NOT a hold — it is
                 lane-bound, not affinity-bound, so it carries no "why is only one lane busy"
                 mystery.
    Both public entry points build on this so the "what launched" and "why the rest didn't" views
    can never drift from one another (they are computed from the SAME selection walk)."""
    if not _usage_ok(usage):
        return [], []

    # LANES: never exceed capacity. A plain-integer `lanes` is a single shared pool; the reserved
    # {"build","investigate"} object is two strict pools with no borrowing (issue #63). `pooled`
    # tells the selection walk which of a candidate's pools to spend from.
    free, pooled = _lane_free(config, lane_state)
    if all(v <= 0 for v in free.values()):
        return [], []

    affinity = config.get("affinity", "hard")
    lanes_in = _occupied_from(lane_state)
    claims_in = _occupied_from(territory_claims or [])
    running_ids = {lane.get("id") for lane in lanes_in}
    claimed_ids = {claim.get("id") for claim in claims_in}
    # occupied entries keep their id so a hold can NAME the lane/claim that blocked it, and their
    # KIND so the journal can say when it releases (issue #519): "lane" = a running lane (or one
    # selected earlier this tick — running once the tick lands); "claim" = FINISHED-but-unmerged
    # territory. territory_claims_from() covers in-flight statuses too, so a running issue appears in
    # BOTH lists under one id; it is a lane, never a finished claim.
    lane_occupied = [{"id": lane.get("id"), "touches": lane.get("touches", []), "type": lane.get("type"),
                      "kind": "lane"} for lane in lanes_in]
    claim_occupied = [{"id": claim.get("id"), "touches": claim.get("touches", []),
                       "type": claim.get("type"),
                       "kind": "lane" if claim.get("id") in running_ids else "claim"}
                      for claim in claims_in]

    # The fresh path asks launch_ok — the SAME function every restart path asks (issue #150) — and
    # not a private re-implementation of half of it. The usage half is already settled by the
    # early-out above (which must stay: it also means "no holds"), so this re-asks it per candidate
    # for free; the point is that ONE predicate answers for both kinds of launch.
    candidates = [p for p in parsed_issues
                  if p.get("id") not in running_ids
                  and p.get("id") not in claimed_ids
                  and launch_ok(p, closed_nums, frozen, usage, config=config)]
    candidates.sort(key=lambda p: issues.sort_key(p, p.get("requeue_front", False)))

    selected_ids = set(running_ids) | set(claimed_ids)
    selected, holds = [], []
    occupied = list(lane_occupied) + list(claim_occupied)              # existing + chosen so far
    for p in candidates:
        if p.get("id") in selected_ids:                               # never launch the same id twice
            continue
        pool = _lane_pool(p) if pooled else "all"
        if free.get(pool, 0) <= 0:
            # this candidate's pool cap is binding (a build pool full does NOT block investigations,
            # and vice versa) -> lane-bound, NOT an affinity hold, so it carries no wildcard mystery.
            continue
        blocker = None if _freeze_exempt_launch(p, frozen) else \
            next((ot for ot in occupied if _anti_affinity_blocks(p, ot, affinity)), None)
        if blocker is not None:
            holds.append({"p": p, "blocker": blocker})                # affinity conflict -> held this tick
            continue
        selected.append(p)
        selected_ids.add(p.get("id"))
        free[pool] -= 1
        occupied.append({"id": p.get("id"), "touches": p["touches"], "type": p.get("type"),
                         "kind": "lane"})
    return selected, holds


def launchable(parsed_issues, lane_state, config, usage, closed_nums, frozen,
               territory_claims=None):
    """Return the issues to launch NOW, in priority order, as a list of dicts:
        {"id", "num", "touches", "soft_overlap": bool}
    `lane_state` is the list of currently OCCUPIED lanes, each {"id", "touches", "type"?}. Each
    `territory_claims` entry has the same shape, but consumes no lane slot; it only participates
    in anti-affinity. Both MUST be derived from the SAME issues_state snapshot (lane_state_from /
    territory_claims_from of one load, which is what every caller does): a hold's blocker KIND
    (running lane vs finished claim, issue #519) is told apart by subtracting the lane ids, so
    claims from a newer snapshot than `lane_state` could name a live lane as finished territory.
    parsed issue may carry a "requeue_front" flag (merged in by the runner from loopstate; default
    False).
    `soft_overlap` (soft affinity only) is True when this launch shares an area with ANY OTHER
    concurrent lane/claim — pre-existing or same-tick — flagged symmetrically so the journal sees
    the pair.
    """
    selected, _ = _plan(parsed_issues, lane_state, config, usage, closed_nums, frozen,
                        territory_claims)
    affinity = config.get("affinity", "hard")
    lanes_in = _occupied_from(lane_state)
    claims_in = _occupied_from(territory_claims or [])
    lane_touches = [lane.get("touches", []) for lane in lanes_in]       # pre-existing occupied lanes
    claimed_touches = [claim.get("touches", []) for claim in claims_in]

    # soft_overlap, computed after selection so it is SYMMETRIC: an issue is flagged if it shares
    # an area with any OTHER concurrent lane (a pre-existing lane, or another same-tick selection).
    out = []
    for i, p in enumerate(selected):
        others = lane_touches + claimed_touches \
            + [q["touches"] for j, q in enumerate(selected) if j != i]
        soft = affinity == "soft" and any(_geometric_overlap(p["touches"], ot) for ot in others)
        out.append({"id": p["id"], "num": p["num"], "touches": p["touches"], "soft_overlap": soft})
    return out


def launch_holds(parsed_issues, lane_state, config, usage, closed_nums, frozen,
                 territory_claims=None):
    """Why a launch was SUPPRESSED by the WILDCARD (issue #36): the subset of affinity holds whose
    overlap was caused by a wildcard '*' on either side — the candidate declares no touches (unknown
    scope), or the lane/claim blocking it does. This is the silent trap the DoD names: a no-touches
    issue overlaps every lane under hard affinity, so `lanes: N` serializes to one busy lane with
    nothing said. A named-vs-named overlap is the operator's OWN declared affinity working as
    designed and carries no mystery, so it is deliberately excluded. Each entry:
        {"id", "num", "blocker_id", "self_wildcard": bool, "blocker_wildcard": bool}
    Empty when usage fails closed, no lane is free (lane-bound, not affinity-bound), or under soft
    affinity (nothing blocks). The runner journals these once per episode so 'why is only one lane
    busy' is answerable from the journal.

    Issue #519 adds two fields so a restore-green fix's hold can be told truthfully: "restore_green"
    (the candidate is the standing-rule fix — whose `touches: *` the engine wrote on purpose, so the
    prose must never advise narrowing it) and "blocker_kind" ("lane" = a running lane, or one
    launching ahead of it this tick; "claim" = finished-but-unmerged territory). Such a hold only
    ever arises on an UNFROZEN mainline — while merges are frozen the fix is exempt (see
    _freeze_exempt_launch)."""
    _, holds = _plan(parsed_issues, lane_state, config, usage, closed_nums, frozen, territory_claims)
    out = []
    for h in holds:
        p, blocker = h["p"], h["blocker"]
        self_wc = _is_wildcard(p.get("touches"))
        blocker_wc = _is_wildcard(blocker.get("touches"))
        if not (self_wc or blocker_wc):
            continue                                                  # named-vs-named: not a wildcard
        out.append({"id": p.get("id"), "num": p.get("num"), "blocker_id": blocker.get("id"),
                    "self_wildcard": self_wc, "blocker_wildcard": blocker_wc,
                    "restore_green": _restore_green(p),
                    "blocker_kind": blocker.get("kind", "lane")})
    return out
