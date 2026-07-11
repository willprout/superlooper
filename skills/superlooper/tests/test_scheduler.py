"""Scheduler: which eligible issues launch NOW, respecting lanes, anti-affinity vs running
lanes, and the usage ceilings (fail closed). Usage cases ported from autocode's test_scheduler."""
import issues
import scheduler

OK = {"five_hour_pct": 10, "seven_day_pct": 10, "auth_status": "ok"}


def _issue(num, touches=None, labels=("type:build", "agent-ready"),
           created="2026-07-01T10:00:00Z", requeue_front=False, expedite=False, priority=None,
           blocked_by=None):
    labs = list(labels)
    if expedite:
        labs.append("expedite")
    if priority == "high":
        labs.append("priority:high")
    if priority == "low":
        labs.append("priority:low")
    meta = ""
    if touches is not None:
        meta += "touches: %s\n" % ", ".join(touches)
    if blocked_by:
        meta += "blocked-by: %s\n" % ", ".join("#%d" % b for b in blocked_by)
    body = ("## Loop metadata\n" + meta) if meta else ""
    gh = {"number": num, "title": "t", "labels": [{"name": n} for n in labs],
          "body": body, "createdAt": created}
    p = issues.parse_issue(gh)
    p["requeue_front"] = requeue_front
    return p


def _cfg(lanes=2, affinity="hard"):
    return {"lanes": lanes, "affinity": affinity}


def _nums(out):
    return [d["num"] for d in out]


# --------------------------- overlaps (the geometric predicate) ---------------------------

def test_overlaps_hard_shared_area_blocks():
    assert scheduler.overlaps(["frontend"], ["frontend", "api"], "hard") is True
    assert scheduler.overlaps(["frontend"], ["api"], "hard") is False


def test_overlaps_hard_wildcard_and_empty_block_everything():
    assert scheduler.overlaps(["*"], ["api"], "hard") is True
    assert scheduler.overlaps([], ["api"], "hard") is True          # empty declaration == unknown scope
    assert scheduler.overlaps([], [], "hard") is True


def test_overlaps_soft_never_blocks():
    assert scheduler.overlaps(["frontend"], ["frontend"], "soft") is False
    assert scheduler.overlaps(["*"], ["*"], "soft") is False


# --------------------------- usage fail-closed (ported) ---------------------------

def test_usage_fail_closed_launches_nothing():
    q = [_issue(1)]
    for bad in (
        {"five_hour_pct": None, "seven_day_pct": 10, "auth_status": "ok"},      # partial payload
        {"five_hour_pct": 10, "seven_day_pct": None, "auth_status": "ok"},
        {"five_hour_pct": 10, "seven_day_pct": 10, "auth_status": "api_error"},
        {"five_hour_pct": 10, "seven_day_pct": 10, "auth_status": "auth_expired"},
        {"five_hour_pct": 10, "seven_day_pct": 10, "auth_status": "ok", "stale": True},
        {},                                                                     # nothing at all
    ):
        assert scheduler.launchable(q, [], _cfg(), bad, closed_nums=set(), frozen=False) == [], bad


def test_usage_malformed_shapes_fail_closed():
    # RC-USAGEFAILOPEN, hardened: None / wrong-typed / NaN / inf / bool pcts must launch NOTHING.
    # (NaN is the sneaky one: NaN > 96 is False, so a bare comparison would fail OPEN.)
    q = [_issue(1)]
    for bad in (
        None, [], "nope", 42,
        {"five_hour_pct": "10", "seven_day_pct": 10, "auth_status": "ok"},       # string pct
        {"five_hour_pct": float("nan"), "seven_day_pct": 10, "auth_status": "ok"},
        {"five_hour_pct": float("inf"), "seven_day_pct": 10, "auth_status": "ok"},
        {"five_hour_pct": True, "seven_day_pct": 10, "auth_status": "ok"},        # bool
        {"five_hour_pct": 10, "seven_day_pct": [10], "auth_status": "ok"},        # list pct
    ):
        assert scheduler.launchable(q, [], _cfg(), bad, set(), False) == [], bad


def test_5h_over_90_blocks_launch():
    q = [_issue(1)]
    usage = {"five_hour_pct": 91, "seven_day_pct": 10, "auth_status": "ok"}
    assert scheduler.launchable(q, [], _cfg(), usage, set(), False) == []
    # 90 exactly is still allowed (the ceiling is >90)
    usage90 = {"five_hour_pct": 90, "seven_day_pct": 10, "auth_status": "ok"}
    assert _nums(scheduler.launchable(q, [], _cfg(), usage90, set(), False)) == [1]


def test_7d_over_96_blocks_new_work():
    q = [_issue(1)]
    usage = {"five_hour_pct": 10, "seven_day_pct": 97, "auth_status": "ok"}
    assert scheduler.launchable(q, [], _cfg(), usage, set(), False) == []
    usage96 = {"five_hour_pct": 10, "seven_day_pct": 96, "auth_status": "ok"}
    assert _nums(scheduler.launchable(q, [], _cfg(), usage96, set(), False)) == [1]


# ------------------- fail OPEN on an UNREADABLE meter (issue #46) -------------------
# The scheduler NEVER fails open on its own: it only honors the `fail_open` flag that decide (which
# owns the time-based grace clock) sets when the meter has been UNREADABLE past the bounded grace.
# This split keeps the exhausted-READ gate (fresh 'ok' at/over the ceiling -> no launch) untouched.

def test_fail_open_flag_launches_despite_an_unreadable_status():
    q = [_issue(1)]
    for status in ("api_error", "no_keychain", "auth_expired"):
        usage = {"auth_status": status, "five_hour_pct": None, "seven_day_pct": None,
                 "stale": True, "fail_open": True}
        assert _nums(scheduler.launchable(q, [], _cfg(), usage, set(), False)) == [1], status


def test_no_fail_open_flag_still_fails_closed_on_an_unreadable_status():
    # Without the flag (decide has NOT decided to fail open — e.g. still within the grace), an
    # unreadable meter launches nothing, exactly as before.
    q = [_issue(1)]
    usage = {"auth_status": "api_error", "five_hour_pct": None, "seven_day_pct": None, "stale": True}
    assert scheduler.launchable(q, [], _cfg(), usage, set(), False) == []


def test_usage_ok_honors_fail_open_for_the_relaunch_gate():
    # usage_ok is the SAME rule the runner's relaunch-tier recovery gates on, so fail_open must lift
    # it too — exited/frozen recovery resumes while the meter is dark (launch normally).
    assert scheduler.usage_ok({"auth_status": "api_error", "stale": True, "fail_open": True}) is True
    assert scheduler.usage_ok({"auth_status": "api_error", "stale": True}) is False


# --------------------------- lanes math ---------------------------

def test_fills_free_lanes():
    q = [_issue(1, touches=["a"]), _issue(2, touches=["b"]), _issue(3, touches=["c"])]
    out = scheduler.launchable(q, [], _cfg(lanes=2), OK, set(), False)
    assert _nums(out) == [1, 2]        # 2 lanes -> first two disjoint issues


def test_no_free_lanes_launches_nothing():
    q = [_issue(3, touches=["c"])]
    lanes = [{"id": "i1", "touches": ["a"]}, {"id": "i2", "touches": ["b"]}]
    assert scheduler.launchable(q, lanes, _cfg(lanes=2), OK, set(), False) == []


def test_one_free_lane_launches_one():
    q = [_issue(3, touches=["c"]), _issue(4, touches=["d"])]
    lanes = [{"id": "i1", "touches": ["a"]}]
    assert _nums(scheduler.launchable(q, lanes, _cfg(lanes=2), OK, set(), False)) == [3]


def test_running_issue_not_relaunched():
    # an issue already occupying a lane must never be launched again (worker-singleton at the
    # scheduler layer: dedupe by id against lane_state).
    q = [_issue(1, touches=["a"]), _issue(2, touches=["b"])]
    lanes = [{"id": "i1", "touches": ["a"]}]
    assert _nums(scheduler.launchable(q, lanes, _cfg(lanes=2), OK, set(), False)) == [2]


def test_duplicate_candidate_launches_once():
    # a duplicated candidate in the queue must launch ONCE, even with free lanes and soft affinity
    # (where nothing blocks the second copy) — dedupe by id within the tick.
    dup = _issue(1, touches=["a"])
    out = scheduler.launchable([dup, dup], [], _cfg(lanes=2, affinity="soft"), OK, set(), False)
    assert _nums(out) == [1]


# --------------------------- eligibility integration ---------------------------

def test_ineligible_issues_never_launch():
    q = [
        _issue(1, labels=["type:build"]),                    # no agent-ready
        _issue(2, labels=["agent-ready"]),                   # no valid type
        _issue(3, blocked_by=[99]),                          # blocked-by #99 not closed
        _issue(4, touches=["z"]),                            # fine
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=4), OK, closed_nums=set(), frozen=False)
    assert _nums(out) == [4]


def test_blocked_by_unblocks_when_closed():
    q = [_issue(3, blocked_by=[99])]
    assert scheduler.launchable(q, [], _cfg(), OK, closed_nums=set(), frozen=False) == []
    assert _nums(scheduler.launchable(q, [], _cfg(), OK, closed_nums={99}, frozen=False)) == [3]


def test_freeze_does_not_stop_launches():
    # freeze stops MERGES, not builds — a frozen mainline still launches eligible work.
    q = [_issue(1, touches=["a"])]
    assert _nums(scheduler.launchable(q, [], _cfg(), OK, set(), frozen=True)) == [1]


# --------------------------- hard affinity ---------------------------

def test_hard_affinity_blocks_overlap_with_running_lane():
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["api"])]
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    out = scheduler.launchable(q, lanes, _cfg(lanes=3, affinity="hard"), OK, set(), False)
    assert _nums(out) == [2]           # #1 overlaps the running frontend lane -> held


def test_hard_affinity_investigation_not_held_by_running_build_overlap():
    q = [_issue(1, touches=["frontend"], labels=("type:investigate", "agent-ready"))]
    lanes = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    out = scheduler.launchable(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [1]


def test_hard_affinity_running_investigation_does_not_hold_build_overlap():
    q = [_issue(2, touches=["frontend"], labels=("type:build", "agent-ready"))]
    lanes = [{"id": "i9", "type": "investigate", "touches": ["frontend"]}]
    out = scheduler.launchable(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [2]


def test_hard_affinity_investigation_selected_this_tick_does_not_hold_build_overlap():
    q = [
        _issue(1, touches=["frontend"], labels=("type:investigate", "agent-ready")),
        _issue(2, touches=["frontend"], labels=("type:build", "agent-ready")),
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [1, 2]


def test_hard_affinity_build_selected_this_tick_does_not_hold_investigation_overlap():
    q = [
        _issue(1, touches=["frontend"], labels=("type:build", "agent-ready")),
        _issue(2, touches=["frontend"], labels=("type:investigate", "agent-ready")),
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [1, 2]


def test_hard_affinity_investigation_still_held_by_open_blocked_by():
    q = [_issue(1, touches=["frontend"], labels=("type:investigate", "agent-ready"),
                blocked_by=[99])]
    lanes = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    assert scheduler.launchable(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_hard_affinity_build_overlap_with_running_build_still_held():
    q = [_issue(1, touches=["frontend"], labels=("type:build", "agent-ready"))]
    lanes = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    assert scheduler.launchable(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_hard_affinity_blocks_overlap_with_held_claim_without_consuming_lane():
    q = [
        _issue(1, touches=["frontend"], labels=("type:build", "agent-ready")),
        _issue(2, touches=["api"], labels=("type:build", "agent-ready")),
    ]
    claims = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    out = scheduler.launchable(q, [], _cfg(lanes=1, affinity="hard"), OK, set(), False,
                               territory_claims=claims)
    assert _nums(out) == [2]           # #1 held by the claim; #2 uses the free lane


def test_hard_affinity_claim_release_allows_overlap_next_tick():
    q = [_issue(1, touches=["frontend"], labels=("type:build", "agent-ready"))]
    claims = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    assert scheduler.launchable(q, [], _cfg(lanes=1, affinity="hard"), OK, set(), False,
                                territory_claims=claims) == []
    out = scheduler.launchable(q, [], _cfg(lanes=1, affinity="hard"), OK, set(), False,
                               territory_claims=[])
    assert _nums(out) == [1]


def test_hard_affinity_investigations_are_exempt_from_held_claims_both_directions():
    build_claim = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    inv_claim = [{"id": "i8", "type": "investigate", "touches": ["frontend"]}]

    inv_q = [_issue(1, touches=["frontend"], labels=("type:investigate", "agent-ready"))]
    build_q = [_issue(2, touches=["frontend"], labels=("type:build", "agent-ready"))]

    inv_out = scheduler.launchable(inv_q, [], _cfg(lanes=1, affinity="hard"), OK, set(), False,
                                   territory_claims=build_claim)
    build_out = scheduler.launchable(build_q, [], _cfg(lanes=1, affinity="hard"), OK, set(), False,
                                     territory_claims=inv_claim)
    assert _nums(inv_out) == [1]
    assert _nums(build_out) == [2]


def test_hard_affinity_diagnose_and_fix_overlap_with_build_still_held():
    lanes = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    q = [_issue(1, touches=["frontend"], labels=("type:diagnose-and-fix", "agent-ready"))]
    assert scheduler.launchable(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_hard_affinity_wildcard_running_lane_blocks_all():
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["api"])]
    lanes = [{"id": "i9", "touches": ["*"]}]      # a running lane of unknown scope blocks everything
    assert scheduler.launchable(q, lanes, _cfg(lanes=3, affinity="hard"), OK, set(), False) == []


def test_hard_affinity_two_overlapping_candidates_serialize_in_one_tick():
    # two eligible issues that touch the same area cannot BOTH launch this tick under hard affinity.
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["frontend"])]
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [1]           # #2 conflicts with #1 (just selected) -> waits


def test_hard_affinity_undeclared_touches_conflicts_with_everything():
    q = [_issue(1, touches=None), _issue(2, touches=["api"])]   # #1 declares nothing -> "*"
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False)
    assert _nums(out) == [1]           # #1 launches into lane 1, then blocks #2 (unknown scope)


# --------------------------- launch_holds (why a launch was suppressed, issue #36) ---------------

def _holds(*a, **k):
    return scheduler.launch_holds(*a, **k)


def test_launch_holds_wildcard_candidate_behind_named_running_lane():
    # A no-touches (wildcard) candidate can't co-schedule with ANY running lane -> held. The hold
    # names WHY: the candidate itself is the wildcard.
    q = [_issue(1, touches=None)]                       # declares nothing -> "*"
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    holds = _holds(q, lanes, _cfg(lanes=3, affinity="hard"), OK, set(), False)
    assert [h["id"] for h in holds] == ["i1"]
    h = holds[0]
    assert h["num"] == 1 and h["blocker_id"] == "i9"
    assert h["self_wildcard"] is True and h["blocker_wildcard"] is False


def test_launch_holds_named_candidate_behind_wildcard_running_lane():
    # The mirror: a running no-touches lane blocks a well-declared candidate. The hold names the
    # LANE as the wildcard (this is the "lanes:5 serialize to one busy lane" trap).
    q = [_issue(2, touches=["api"])]
    lanes = [{"id": "i9", "touches": []}]               # running lane declares nothing -> "*"
    holds = _holds(q, lanes, _cfg(lanes=3, affinity="hard"), OK, set(), False)
    assert [h["id"] for h in holds] == ["i2"]
    assert holds[0]["blocker_id"] == "i9"
    assert holds[0]["self_wildcard"] is False and holds[0]["blocker_wildcard"] is True


def test_launch_holds_same_tick_wildcard_selection_blocks_the_rest():
    # #1 declares nothing, launches into lane 1, then blocks #2 and #3 (the headline serialization).
    q = [_issue(1, touches=None), _issue(2, touches=["api"]), _issue(3, touches=["db"])]
    cfg = _cfg(lanes=3, affinity="hard")
    assert _nums(scheduler.launchable(q, [], cfg, OK, set(), False)) == [1]
    holds = _holds(q, [], cfg, OK, set(), False)
    assert sorted(h["id"] for h in holds) == ["i2", "i3"]
    for h in holds:
        assert h["blocker_id"] == "i1" and h["blocker_wildcard"] is True


def test_launch_holds_named_vs_named_overlap_is_not_a_wildcard_hold():
    # Two issues that genuinely declare the SAME named area serialize by the operator's own
    # design — not a wildcard, so launch_holds stays silent (no mystery to explain).
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["frontend"])]
    assert _holds(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_launch_holds_disjoint_named_issues_have_no_holds():
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["api"])]
    assert _holds(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_launch_holds_empty_when_no_free_lanes():
    # All lanes busy -> everything is LANE-held, not affinity-held; that is not a wildcard mystery.
    q = [_issue(1, touches=None)]
    lanes = [{"id": "i8", "touches": ["a"]}, {"id": "i9", "touches": ["b"]}]
    assert _holds(q, lanes, _cfg(lanes=2, affinity="hard"), OK, set(), False) == []


def test_launch_holds_empty_under_soft_affinity():
    # soft affinity never blocks a co-schedule, so nothing is suppressed.
    q = [_issue(1, touches=None)]
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    assert _holds(q, lanes, _cfg(lanes=3, affinity="soft"), OK, set(), False) == []


def test_launch_holds_empty_when_usage_fails_closed():
    q = [_issue(1, touches=None)]
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    assert _holds(q, lanes, _cfg(lanes=3, affinity="hard"), {}, set(), False) == []


def test_launch_holds_wildcard_behind_held_claim_without_a_lane():
    # A territory claim (finished-but-unmerged lane) also blocks a wildcard candidate, and the
    # hold surfaces it — the claim consumes no lane slot but still serializes an unknown-scope issue.
    q = [_issue(1, touches=None), _issue(2, touches=["api"])]
    claims = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    holds = _holds(q, [], _cfg(lanes=2, affinity="hard"), OK, set(), False, territory_claims=claims)
    assert [h["id"] for h in holds] == ["i1"]
    assert holds[0]["blocker_id"] == "i9" and holds[0]["self_wildcard"] is True


def test_launch_holds_investigation_is_never_a_wildcard_hold():
    # Investigations produce no merge, so anti-affinity never holds them — even a no-touches one.
    q = [_issue(1, touches=None, labels=("type:investigate", "agent-ready"))]
    lanes = [{"id": "i9", "type": "build", "touches": ["frontend"]}]
    assert _holds(q, lanes, _cfg(lanes=3, affinity="hard"), OK, set(), False) == []


# --------------------------- soft affinity ---------------------------

def test_soft_affinity_allows_overlap_but_flags_it():
    q = [_issue(1, touches=["frontend"])]
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    out = scheduler.launchable(q, lanes, _cfg(lanes=3, affinity="soft"), OK, set(), False)
    assert _nums(out) == [1]           # soft: co-scheduled despite overlap
    assert out[0]["soft_overlap"] is True


def test_soft_affinity_disjoint_has_no_overlap_flag():
    q = [_issue(1, touches=["api"])]
    lanes = [{"id": "i9", "touches": ["frontend"]}]
    out = scheduler.launchable(q, lanes, _cfg(lanes=3, affinity="soft"), OK, set(), False)
    assert out[0]["soft_overlap"] is False


def test_soft_overlap_flag_is_symmetric_for_same_tick_pair():
    # two overlapping candidates launched together (no pre-existing lane) must BOTH be flagged —
    # the flag means "co-scheduled with an overlapping lane", which is true for both sides.
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["frontend"])]
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="soft"), OK, set(), False)
    assert _nums(out) == [1, 2]
    assert all(d["soft_overlap"] for d in out)


def test_soft_disjoint_pair_not_flagged():
    q = [_issue(1, touches=["frontend"]), _issue(2, touches=["api"])]
    out = scheduler.launchable(q, [], _cfg(lanes=2, affinity="soft"), OK, set(), False)
    assert not any(d["soft_overlap"] for d in out)


# --------------------------- ordering (expedite, priority, requeue_front) ---------------------------

def test_expedite_jumps_the_queue():
    q = [
        _issue(1, touches=["a"], created="2026-07-01T00:00:00Z"),               # oldest normal
        _issue(2, touches=["b"], created="2026-07-05T00:00:00Z", expedite=True),  # expedite, newer
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=1), OK, set(), False)          # only ONE lane
    assert _nums(out) == [2]           # expedite takes the single lane


def test_priority_band_orders_before_normal():
    q = [
        _issue(1, touches=["a"], created="2026-07-01T00:00:00Z"),               # normal
        _issue(2, touches=["b"], created="2026-07-05T00:00:00Z", priority="high"),
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=1), OK, set(), False)
    assert _nums(out) == [2]           # high priority wins the lane


def test_requeue_front_orders_ahead_of_same_band():
    q = [
        _issue(1, touches=["a"], created="2026-07-01T00:00:00Z"),               # older normal
        _issue(2, touches=["b"], created="2026-07-05T00:00:00Z", requeue_front=True),
    ]
    out = scheduler.launchable(q, [], _cfg(lanes=1), OK, set(), False)
    assert _nums(out) == [2]           # requeue_front jumps ahead of its band


def test_usage_ok_is_public_for_the_runner_relaunch_gate():
    # Task 10: decide() gates relaunch-tier recovery on the SAME fail-closed usage rule the
    # launch gate uses — one rule, one name, never a re-implementation that could drift.
    assert scheduler.usage_ok({"auth_status": "ok", "five_hour_pct": 10.0,
                               "seven_day_pct": 20.0}) is True
    assert scheduler.usage_ok(None) is False
    assert scheduler.usage_ok({"auth_status": "ok", "five_hour_pct": float("nan"),
                               "seven_day_pct": 1.0}) is False
