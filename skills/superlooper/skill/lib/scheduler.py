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
    returns False (launch nothing)."""
    if not isinstance(usage, dict):
        return False
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
    if not _usage_ok(usage):
        return []

    lanes = config.get("lanes", 1)
    free = max(0, lanes - len(lane_state))
    if free == 0:
        return []

    affinity = config.get("affinity", "hard")
    lanes_in = _occupied_from(lane_state)
    claims_in = _occupied_from(territory_claims or [])
    running_ids = {lane.get("id") for lane in lanes_in}
    claimed_ids = {claim.get("id") for claim in claims_in}
    claimed_touches = [claim.get("touches", []) for claim in claims_in]
    lane_touches = [lane.get("touches", []) for lane in lanes_in]       # pre-existing occupied lanes
    lane_occupied = [{"touches": lane.get("touches", []), "type": lane.get("type")}
                     for lane in lanes_in]
    claim_occupied = [{"touches": claim.get("touches", []), "type": claim.get("type")}
                      for claim in claims_in]

    candidates = [p for p in parsed_issues
                  if p.get("id") not in running_ids
                  and p.get("id") not in claimed_ids
                  and issues.eligible(p, closed_nums, frozen)]
    candidates.sort(key=lambda p: issues.sort_key(p, p.get("requeue_front", False)))

    selected_ids = set(running_ids) | set(claimed_ids)
    selected = []
    occupied = list(lane_occupied) + list(claim_occupied)              # existing + chosen so far
    for p in candidates:
        if len(selected) >= free:
            break
        if p.get("id") in selected_ids:                               # never launch the same id twice
            continue
        touches = p["touches"]
        if any(_anti_affinity_blocks(p, ot, affinity) for ot in occupied):
            continue                                                  # hard conflict -> held this tick
        selected.append(p)
        selected_ids.add(p.get("id"))
        occupied.append({"touches": touches, "type": p.get("type")})

    # soft_overlap, computed after selection so it is SYMMETRIC: an issue is flagged if it shares
    # an area with any OTHER concurrent lane (a pre-existing lane, or another same-tick selection).
    out = []
    for i, p in enumerate(selected):
        others = lane_touches + claimed_touches \
            + [q["touches"] for j, q in enumerate(selected) if j != i]
        soft = affinity == "soft" and any(_geometric_overlap(p["touches"], ot) for ot in others)
        out.append({"id": p["id"], "num": p["num"], "touches": p["touches"], "soft_overlap": soft})
    return out
