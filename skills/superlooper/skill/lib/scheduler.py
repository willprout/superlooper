"""Which eligible issues to launch NOW — a pure function over the queue + lane state + usage.

Three gates, in order:
  1. USAGE, fail closed (ported from autocode, RC-USAGEFAILOPEN): unknown / stale / unhealthy /
     partial / malformed usage, or over the five-hour / seven-day ceilings, launches NOTHING.
     v1's `or 0` coercion once read a partial payload as "0% used" and launched over quota; a
     NaN pct would sneak past a bare `>` comparison (NaN > 96 is False), so pcts must be FINITE.
  2. LANES: never exceed config.lanes concurrent sessions; never launch the same id twice.
  3. ANTI-AFFINITY: under hard affinity, a merge-producing issue whose declared touch-areas
     overlap a merge-producing running lane, held territory claim, or one already selected this
     tick is HELD.
     Investigations do not produce PRs or merges, so they are exempt in both directions. Under
     soft affinity the overlap is allowed and flagged (symmetrically) for the journal.

Eligibility itself (agent-ready, valid type, blocked-by closed) lives in issues.eligible.
"""
import math

import issues

FIVE_HOUR_LAUNCH_CEILING = 90    # spec: >90% five-hour -> no fresh launch
SEVEN_DAY_NEW_WORK_CEILING = 96  # spec: >96% seven-day -> no new work


def _finite_pct(x):
    """A usable utilization percentage: a finite real number, not a bool (True == 1 would slip
    past the ceiling), not NaN/inf (NaN > ceiling is False and would fail OPEN)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _usage_ok(usage):
    """FAIL CLOSED: only a dict with 'ok' auth, non-stale, both pcts FINITE and under the ceilings
    passes. Any missing / partial / unhealthy / malformed usage (None, wrong type, NaN, ...)
    returns False (launch nothing).

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
    if not _finite_pct(fh) or not _finite_pct(sd):
        return False
    if sd > SEVEN_DAY_NEW_WORK_CEILING:
        return False
    if fh > FIVE_HOUR_LAUNCH_CEILING:
        return False
    return True


def usage_ok(usage):
    """Public face of the fail-closed usage rule (Task 10): the runner's relaunch-tier recovery
    gates on the SAME rule as fresh launches — one rule, one name, so the two paths can never
    drift apart."""
    return _usage_ok(usage)


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

    lanes = config.get("lanes", 1)
    free = max(0, lanes - len(lane_state))
    if free == 0:
        return [], []

    affinity = config.get("affinity", "hard")
    lanes_in = _occupied_from(lane_state)
    claims_in = _occupied_from(territory_claims or [])
    running_ids = {lane.get("id") for lane in lanes_in}
    claimed_ids = {claim.get("id") for claim in claims_in}
    # occupied entries keep their id so a hold can NAME the lane/claim that blocked it.
    lane_occupied = [{"id": lane.get("id"), "touches": lane.get("touches", []), "type": lane.get("type")}
                     for lane in lanes_in]
    claim_occupied = [{"id": claim.get("id"), "touches": claim.get("touches", []),
                       "type": claim.get("type")} for claim in claims_in]

    candidates = [p for p in parsed_issues
                  if p.get("id") not in running_ids
                  and p.get("id") not in claimed_ids
                  and issues.eligible(p, closed_nums, frozen)]
    candidates.sort(key=lambda p: issues.sort_key(p, p.get("requeue_front", False)))

    selected_ids = set(running_ids) | set(claimed_ids)
    selected, holds = [], []
    occupied = list(lane_occupied) + list(claim_occupied)              # existing + chosen so far
    for p in candidates:
        if len(selected) >= free:
            break                                                     # lane cap now binding, not affinity
        if p.get("id") in selected_ids:                               # never launch the same id twice
            continue
        blocker = next((ot for ot in occupied if _anti_affinity_blocks(p, ot, affinity)), None)
        if blocker is not None:
            holds.append({"p": p, "blocker": blocker})                # affinity conflict -> held this tick
            continue
        selected.append(p)
        selected_ids.add(p.get("id"))
        occupied.append({"id": p.get("id"), "touches": p["touches"], "type": p.get("type")})
    return selected, holds


def launchable(parsed_issues, lane_state, config, usage, closed_nums, frozen,
               territory_claims=None):
    """Return the issues to launch NOW, in priority order, as a list of dicts:
        {"id", "num", "touches", "soft_overlap": bool}
    `lane_state` is the list of currently OCCUPIED lanes, each {"id", "touches", "type"?}. Each
    `territory_claims` entry has the same shape, but consumes no lane slot; it only participates
    in anti-affinity.
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
    busy' is answerable from the journal."""
    _, holds = _plan(parsed_issues, lane_state, config, usage, closed_nums, frozen, territory_claims)
    out = []
    for h in holds:
        p, blocker = h["p"], h["blocker"]
        self_wc = _is_wildcard(p.get("touches"))
        blocker_wc = _is_wildcard(blocker.get("touches"))
        if not (self_wc or blocker_wc):
            continue                                                  # named-vs-named: not a wildcard
        out.append({"id": p.get("id"), "num": p.get("num"), "blocker_id": blocker.get("id"),
                    "self_wildcard": self_wc, "blocker_wildcard": blocker_wc})
    return out
