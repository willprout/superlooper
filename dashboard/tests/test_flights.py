"""Task 3 — the flight model: every display semantic derived in pure Python.

These tests are the squint test in code form (design record §3 mappings, §5 honest signals): the
raw facts dicts from Task 2 (state home) and Task 4 (gh + diff-stat) become honest "flight"
objects, and the tests assert the *state diagram* — delete the airplane art and this is still a
correct picture of what the loop is doing.

Discipline pinned by these tests, straight from the design record:
  * circuit position is DISCRETE — never derived from elapsed time (§3);
  * off-path states are visually distinct and never collapse into each other (§5);
  * a wandered merge earns no celebration (§7 honesty law);
  * a crisp contrail with flat progress is a "spinning?" warning, never the healthiest plane (§5);
  * the incident sign counts MACHINE stumbles only — William's gates never touch it (§7);
  * corner-counter stats are outcome-only — no human-latency stopwatch ever exists (§7 / kill list).

Fact shapes come from design/project/uploads/sample-data.txt and the committed fixtures — never
invented. All functions are pure: they take already-read facts and return values; no I/O here.
"""
import flights


# =============================== liveness tier (§5) ===============================
# Contrail liveness from activity-file age vs the repo's OWN thresholds (defaults 480/2700 s).

def test_liveness_fresh_below_idle_threshold():
    # age 100 s < idle 480 → the plane is being actively worked: a crisp contrail.
    assert flights.liveness_tier(mtime=900.0, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == flights.FRESH


def test_liveness_idle_between_thresholds():
    # age 600 s: past idle (480), short of frozen (2700) — the tower peeks.
    assert flights.liveness_tier(mtime=400.0, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == flights.IDLE


def test_liveness_frozen_at_or_past_freeze_threshold():
    # age exactly 2700 s (boundary inclusive) and beyond → frozen, no contrail.
    assert flights.liveness_tier(mtime=0.0, now=2700.0,
                                 idle_seconds=480, freeze_seconds=2700) == flights.FROZEN
    assert flights.liveness_tier(mtime=0.0, now=9999.0,
                                 idle_seconds=480, freeze_seconds=2700) == flights.FROZEN


def test_liveness_idle_boundary_is_inclusive():
    # age exactly the idle threshold counts as idle (matches the runner's `>=` tiering).
    assert flights.liveness_tier(mtime=0.0, now=480.0,
                                 idle_seconds=480, freeze_seconds=2700) == flights.IDLE


def test_liveness_none_when_no_activity_file():
    # No activity file ⇒ no liveness signal at all (an at-stand or arrived flight isn't "frozen").
    assert flights.liveness_tier(mtime=None, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) is None


def test_liveness_respects_per_repo_thresholds():
    # A repo that declares tighter thresholds tiers the SAME age more harshly — the flight model
    # reads each repo's own numbers, never a global constant.
    age_600 = dict(mtime=400.0, now=1000.0)
    assert flights.liveness_tier(idle_seconds=480, freeze_seconds=2700, **age_600) == flights.IDLE
    assert flights.liveness_tier(idle_seconds=120, freeze_seconds=300, **age_600) == flights.FROZEN


# =============================== attempt counter + wander (§3/§7) ===============================

def test_attempt_number_is_one_with_no_regenerate():
    journal = [{"act": "launch", "id": "i5"}, {"act": "merge", "id": "i5"}]
    assert flights.attempt_number(journal) == 1


def test_attempt_number_counts_regenerations():
    # i16 in the sample regenerated once → attempt 2 (design record §3: "SL-441·a2").
    journal = [
        {"act": "launch", "id": "i16"},
        {"act": "regenerate", "id": "i16", "conflicts": 1},
        {"act": "launch", "id": "i16"},
    ]
    assert flights.attempt_number(journal) == 2


def test_attempt_number_two_regenerations_is_attempt_three():
    journal = [{"act": "regenerate"}, {"act": "regenerate"}]
    assert flights.attempt_number(journal) == 3


def test_flight_label_hides_attempt_one():
    # Attempt 1 is the plain flight number; the ·A marker only appears once a go-around happened.
    assert flights.flight_label(23, 1) == "SL-23"


def test_flight_label_shows_attempt_after_regenerate():
    # Exact DoD format: SL-N·A2 (uppercase A, middle-dot separator).
    assert flights.flight_label(16, 2) == "SL-16·A2"
    assert flights.flight_label(441, 3) == "SL-441·A3"


def test_wander_flag_true_only_on_a_wandered_merge():
    # merge with wander:true (sample i23) → the flight is wandered and gets NO celebration.
    wandered = [{"act": "merge", "id": "i23", "wander": True, "outcome": "ok"}]
    clean = [{"act": "merge", "id": "i16", "wander": False, "outcome": "ok"}]
    assert flights.merged_wandered(wandered) is True
    assert flights.merged_wandered(clean) is False


def test_wander_flag_false_when_not_merged():
    assert flights.merged_wandered([{"act": "launch", "id": "i5"}]) is False


# =============================== circuit stage — DISCRETE (§3) ===============================
# The traffic pattern: at-stand → taxi-out → takeoff → downwind → base-turn → final → touchdown
# → taxi-in. Position comes from status + discrete markers, NEVER from elapsed time.

def test_stage_ready_is_at_stand():
    assert flights.circuit_stage("ready") == flights.AT_STAND


def test_stage_ready_with_launch_attempt_is_taxi_out():
    # A launch has been dispatched but the session isn't confirmed up — taxiing to the runway.
    assert flights.circuit_stage("ready", launched=True) == flights.TAXI_OUT


def test_stage_running_just_launched_is_takeoff():
    # Session launched, no build activity yet — rolling down the runway.
    assert flights.circuit_stage("running", launched=True, session_started=False) == flights.TAKEOFF


def test_stage_running_and_building_is_downwind():
    # Session is up and emitting activity — the long working leg.
    assert flights.circuit_stage("running", session_started=True) == flights.DOWNWIND


def test_stage_running_with_report_filed_is_base_turn():
    # The report landed while the session was still up — turning toward the gate.
    assert flights.circuit_stage("running", session_started=True, report_present=True) == flights.BASE_TURN


def test_stage_gating_is_final():
    assert flights.circuit_stage("gating") == flights.FINAL


def test_stage_cleared_gate_is_final_even_before_runner_flips():
    # All four checks green ⇒ at the gate ("cleared to land"), regardless of the raw status lag.
    assert flights.circuit_stage("running", session_started=True, cleared=True) == flights.FINAL


def test_stage_merged_is_touchdown():
    assert flights.circuit_stage("merged") == flights.TOUCHDOWN


def test_stage_merged_and_closed_is_taxi_in():
    assert flights.circuit_stage("merged", closed=True) == flights.TAXI_IN


def test_circuit_stage_takes_no_clock_argument():
    # Structural guarantee that position can't be time-derived: the function signature has no now/
    # elapsed/age parameter at all (design record §3, costume rule 3).
    import inspect
    params = set(inspect.signature(flights.circuit_stage).parameters)
    assert not (params & {"now", "elapsed", "age", "mtime", "seconds", "ts"})


def test_stage_position_never_advances_with_elapsed_time():
    # Two flights identical in every DISCRETE fact but flying for wildly different durations occupy
    # the SAME stage — elapsed time is not position (design record §3). Here liveness stays fresh in
    # both, so nothing about the passage of time may move the plane along the circuit.
    young = flights.flight_stage("running", session_started=True,
                                 liveness=flights.FRESH)
    old = flights.flight_stage("running", session_started=True,
                               liveness=flights.FRESH)
    assert young == old == flights.DOWNWIND


# =============================== off-path states — DISTINCT (§5) ===============================

def test_parked_dominates():
    assert flights.flight_stage("parked") == flights.PARKED


def test_needs_william_is_awaiting_amber():
    assert flights.flight_stage("needs_william") == flights.AWAITING


def test_bounced_status_is_awaiting_amber():
    assert flights.flight_stage("bounced") == flights.AWAITING


def test_bounced_marker_makes_a_blocked_flight_awaiting():
    # A blocked session whose marker is a BOUNCED: memo is an OWNER decision, not a live radio call.
    assert flights.flight_stage("blocked", bounced=True) == flights.AWAITING


def test_plain_blocked_keeps_flying_not_amber():
    # A worker blocked on a question the answerer is handling is still in the air (a radio call),
    # NOT an amber owner-decision — those demand opposite responses and must never share a look.
    assert flights.flight_stage("blocked", session_started=True) == flights.DOWNWIND


def test_holding_is_its_own_state():
    assert flights.flight_stage("holding") == flights.HOLDING


def test_status_frozen_is_session_frozen():
    assert flights.flight_stage("frozen") == flights.SESSION_FROZEN


def test_running_session_gone_frozen_is_session_frozen():
    # An in-air session whose activity aged past the frozen tier is a DEAD session (grey/no
    # contrail) — time degraded its liveness; it did not advance up the circuit.
    assert flights.flight_stage("running", session_started=True,
                                liveness=flights.FROZEN) == flights.SESSION_FROZEN


def test_at_stand_is_never_session_frozen_for_lack_of_a_contrail():
    # A queued flight has no session, so a None/absent liveness must NOT read as frozen.
    assert flights.flight_stage("ready", liveness=None) == flights.AT_STAND


def test_the_off_path_states_are_pairwise_distinct():
    # The core §5 honesty guarantee: every off-path state demands an opposite response, so no two
    # collapse. Six of them now, since a stranded gate (issue #22) joined the set.
    assert len(set(flights.OFF_PATH_STATES)) == len(flights.OFF_PATH_STATES) == 6


def test_session_frozen_and_merges_freeze_are_different_values():
    # The most dangerous collision: a dead SESSION vs the REPO's calm landing-pause. Same word
    # "frozen", opposite meanings, opposite responses — they must never be the same value.
    assert flights.SESSION_FROZEN != flights.MERGES_FREEZE


def test_awaiting_marker_is_not_amber_owner_decision():
    # state/awaiting/<id> is a worker's OWN long-background-wait touch (loop contract), not a
    # decision waiting on William — it must not render as the amber awaiting state.
    assert flights.flight_stage("running", session_started=True, long_wait=True) == flights.DOWNWIND


# =============================== stranded at the gate (issue #22 / §5) ===============================
# A finished session (report on disk, status `gating`) whose GATE stopped advancing is its OWN state.
# The session COMPLETED its work, so its naturally-stale activity file must never read as a dead
# session; "stranded at the gate" points the owner at the gate/runner, never at a healthy session.

def test_gating_with_report_and_stale_activity_is_stranded_not_frozen():
    # DoD #1: status `gating` + report present + stale (frozen-tier) session activity → STRANDED at
    # the gate, NOT a dead session. The session finished its work; the gate abandoned the issue.
    stage = flights.flight_stage("gating", liveness=flights.FROZEN, report_present=True)
    assert stage == flights.STRANDED
    assert stage != flights.SESSION_FROZEN


def test_dead_mid_flight_session_still_reads_session_frozen():
    # DoD #2: a genuinely dead mid-flight session — no report filed, activity aged past the frozen
    # tier — is UNCHANGED: still a dead SESSION (grey/no-contrail), never stranded-at-gate.
    stage = flights.flight_stage("running", session_started=True, liveness=flights.FROZEN,
                                 report_present=False)
    assert stage == flights.SESSION_FROZEN
    assert stage != flights.STRANDED


def test_stranded_is_distinct_from_session_frozen_and_holding():
    # The §5 honesty law extended: a stranded gate demands a different response from a dead session
    # (relaunch the worker) and from calm holding (just wait) — no two may share a value.
    assert flights.STRANDED not in (flights.SESSION_FROZEN, flights.HOLDING, flights.FINAL,
                                    flights.PARKED, flights.AWAITING, flights.MERGES_FREEZE)


def test_stranded_is_an_off_path_state():
    # It overrides the on-circuit position (rendered AT the gate) like the other off-path states, so
    # it joins OFF_PATH_STATES and is not itself an on-circuit stage.
    assert flights.STRANDED in flights.OFF_PATH_STATES
    assert flights.STRANDED not in flights.CIRCUIT_STAGES


def test_fresh_or_absent_gate_with_report_is_still_final_not_stranded():
    # A gate whose session JUST finished (fresh, or an absent activity file) is healthy at the gate —
    # the runner is landing it. Only a session that has gone STALE at the gate is stranded: we never
    # fabricate "stranded" without positive time evidence (a fresh/absent gate is cleared-to-land,
    # not stuck), and neither reading is ever the grey dead-session look.
    assert flights.flight_stage("gating", liveness=flights.FRESH, report_present=True) == flights.FINAL
    assert flights.flight_stage("gating", liveness=None, report_present=True) == flights.FINAL


def test_gating_without_a_report_is_not_stranded():
    # STRANDED is EARNED by the report on disk (the proof the session finished). A gate with no
    # report and stale activity can't be proven finished, so it keeps the conservative dead-session
    # read rather than claiming a clean hand-off to the gate.
    assert flights.flight_stage("gating", liveness=flights.FROZEN,
                                report_present=False) == flights.SESSION_FROZEN


# =============================== progress ≠ liveness → spinning (§5) ===============================
# A separate progress signal from existing data (diff-stat delta + journal event variety over a
# rolling window). Crisp contrail + flat progress = a "spinning?" warning, never the healthiest
# plane on the field.

def _repeat(act, n):
    # n identical journal events — the doom-loop signature (same operation over and over).
    return [{"act": act, "id": "i9", "outcome": "fail"} for _ in range(n)]


def test_progress_reports_event_variety():
    window = [{"act": "launch"}, {"act": "gate"}, {"act": "merge"}]
    assert flights.progress(window)["variety"] == 3


def test_progress_flat_when_one_event_repeats_and_diff_is_static():
    # Re-running the same failing test: many events, ONE kind, diff not growing → flat.
    prog = flights.progress(_repeat("gate", 6), diff_delta=0)
    assert prog["flat"] is True


def test_progress_not_flat_when_diff_is_growing():
    # Real code is being written — a growing diff rescues even a low-variety window.
    prog = flights.progress(_repeat("gate", 6), diff_delta=140)
    assert prog["flat"] is False


def test_progress_not_flat_when_events_are_varied():
    window = [{"act": "launch"}, {"act": "gate"}, {"act": "nudge"}, {"act": "gate"}]
    assert flights.progress(window, diff_delta=0)["flat"] is False


def test_progress_quiet_window_is_not_flat():
    # A calm, healthy session that simply hasn't journaled much is NOT spinning — flatness needs
    # real repetition, not merely a sparse window (guards against false doom-loop alarms).
    assert flights.progress([{"act": "gate"}], diff_delta=0)["flat"] is False
    assert flights.progress([], diff_delta=None)["flat"] is False


def test_spinning_true_only_when_liveness_crisp_and_progress_flat():
    flat = flights.progress(_repeat("gate", 6), diff_delta=0)
    assert flights.is_spinning(flights.FRESH, flat) is True


def test_spinning_false_when_progress_is_healthy():
    healthy = flights.progress(_repeat("gate", 6), diff_delta=200)
    assert flights.is_spinning(flights.FRESH, healthy) is False


def test_spinning_false_when_not_crisp():
    # An idle or frozen flight with flat progress is idle/frozen — a DIFFERENT signal, not spinning
    # (spinning is specifically "looks alive but isn't getting anywhere").
    flat = flights.progress(_repeat("gate", 6), diff_delta=0)
    assert flights.is_spinning(flights.IDLE, flat) is False
    assert flights.is_spinning(flights.FROZEN, flat) is False
    assert flights.is_spinning(None, flat) is False


# =============================== gate checklist (§3 — real check names) ===============================
# The "final" gate: report ✓ review ✓ CI ✓ mergeable ✓ → cleared to land. Names are REAL and fixed
# (design record §3, costume rule 2); CI reads the repo's configured required checks.

_GREEN_PR = {
    "number": 19, "state": "OPEN", "mergeable": "MERGEABLE",
    "statusCheckRollup": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    "headRefName": "sl/i16-x",
}


def test_gate_all_four_green_is_cleared():
    gate = flights.gate_checklist(_GREEN_PR, report_present=True, review_present=True,
                                  required_checks=["tests"])
    assert gate == {"report": True, "review": True, "ci": True, "mergeable": True, "cleared": True}


def test_gate_uses_the_four_real_check_names():
    gate = flights.gate_checklist(_GREEN_PR, report_present=True, review_present=True,
                                  required_checks=["tests"])
    assert set(gate) == {"report", "review", "ci", "mergeable", "cleared"}


def test_gate_missing_report_is_not_cleared():
    gate = flights.gate_checklist(_GREEN_PR, report_present=False, review_present=True,
                                  required_checks=["tests"])
    assert gate["report"] is False and gate["cleared"] is False


def test_gate_missing_review_is_not_cleared():
    gate = flights.gate_checklist(_GREEN_PR, report_present=True, review_present=False,
                                  required_checks=["tests"])
    assert gate["review"] is False and gate["cleared"] is False


def test_gate_ci_requires_every_required_check_to_succeed():
    pr = dict(_GREEN_PR, statusCheckRollup=[
        {"name": "tests", "conclusion": "SUCCESS"},
        {"name": "lint", "conclusion": "FAILURE"}])
    # tests passes, lint fails but ISN'T required → CI still green (the gate reads only real
    # required checks, decision B.11 / config required_checks=["tests"]).
    assert flights.gate_checklist(pr, True, True, ["tests"])["ci"] is True
    # but if lint WERE required, its failure blocks CI.
    assert flights.gate_checklist(pr, True, True, ["tests", "lint"])["ci"] is False


def test_gate_ci_false_when_a_required_check_is_missing():
    pr = dict(_GREEN_PR, statusCheckRollup=[])
    assert flights.gate_checklist(pr, True, True, ["tests"])["ci"] is False


def test_gate_ci_false_when_a_required_check_is_still_pending():
    # A check with no conclusion yet (in flight) is NOT success — fail closed.
    pr = dict(_GREEN_PR, statusCheckRollup=[{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}])
    assert flights.gate_checklist(pr, True, True, ["tests"])["ci"] is False


def test_gate_mergeable_only_on_literal_mergeable():
    conflicting = dict(_GREEN_PR, mergeable="CONFLICTING")
    unknown = dict(_GREEN_PR, mergeable="UNKNOWN")
    assert flights.gate_checklist(conflicting, True, True, ["tests"])["mergeable"] is False
    assert flights.gate_checklist(unknown, True, True, ["tests"])["mergeable"] is False


def test_gate_empty_pr_fails_closed():
    # No PR at all (gh returned {}) — nothing to clear; CI and mergeable are False, not crashy.
    gate = flights.gate_checklist({}, report_present=True, review_present=True, required_checks=["tests"])
    assert gate["ci"] is False and gate["mergeable"] is False and gate["cleared"] is False


# =============================== incident sign — machine stumbles ONLY (§7) ===============================
# "N landings since the last incident." An incident is a MACHINE-side failure (park / conflict-cap /
# failed auto-fix / runner death). William's gates are normal ops and NEVER repaint the sign.

def _merge(id_, ts, ok=True):
    return {"act": "merge", "id": id_, "ts": ts, "outcome": "ok" if ok else "merge failed (retry)"}


def test_landings_since_incident_equals_total_when_no_incident():
    journal = [_merge("i1", 10), _merge("i2", 20), _merge("i3", 30)]
    stats = flights.incident_stats(journal)
    assert stats["total_landings"] == 3
    assert stats["landings_since_incident"] == 3
    assert stats["last_incident_ts"] is None


def test_a_park_is_an_incident_and_resets_the_count():
    journal = [_merge("i1", 10), {"act": "park", "id": "i7", "ts": 15}, _merge("i2", 20), _merge("i3", 30)]
    stats = flights.incident_stats(journal)
    assert stats["total_landings"] == 3
    assert stats["landings_since_incident"] == 2       # only the two landings AFTER the park
    assert stats["last_incident_ts"] == 15


def test_owner_gates_never_count_as_incidents():
    # Approve/re-approve, answering a bounce, a gate check, a relabel — every one is William or the
    # machine HELPING, never a stumble. A field of nothing but owner/answerer actions + landings has
    # ZERO incidents (design record §7: "acting on the loop must never feel like breaking a streak").
    journal = [
        {"act": "relabel", "id": "i1", "add": ["in-progress"], "remove": ["agent-ready"], "ts": 5},
        {"act": "reapprove", "id": "i1", "ts": 6},
        {"act": "hire_answerer", "id": "i2", "ts": 7},
        {"act": "deliver_answer", "id": "i2", "ts": 8},
        {"act": "gate", "id": "i1", "ts": 9, "outcome": "ok"},
        _merge("i1", 10), _merge("i2", 20),
    ]
    stats = flights.incident_stats(journal)
    assert stats["last_incident_ts"] is None
    assert stats["landings_since_incident"] == stats["total_landings"] == 2


def test_a_successful_go_around_is_not_an_incident():
    # A regenerate that then lands is the machine RECOVERING, not giving up — only a conflict-cap
    # PARK is the stumble. A bare regenerate must not reset the sign.
    journal = [{"act": "regenerate", "id": "i16", "ts": 10}, _merge("i16", 20)]
    stats = flights.incident_stats(journal)
    assert stats["last_incident_ts"] is None
    assert stats["landings_since_incident"] == 1


def test_last_of_several_incidents_wins():
    journal = [{"act": "park", "ts": 10}, _merge("i1", 20), {"act": "park", "ts": 30}, _merge("i2", 40)]
    stats = flights.incident_stats(journal)
    assert stats["last_incident_ts"] == 30
    assert stats["landings_since_incident"] == 1       # only the landing after the SECOND park


def test_failed_merge_is_not_a_landing():
    journal = [_merge("i1", 10, ok=False), _merge("i2", 20)]
    assert flights.incident_stats(journal)["total_landings"] == 1


# =============================== corner counter — outcome-only (§7 / kill list #4) ===============================

def test_corner_stats_are_outcome_facts():
    journal = [_merge("i1", 100), _merge("i2", 200),
               {"act": "regenerate", "ts": 150}, {"act": "park", "ts": 160}]
    stats = flights.corner_stats(journal, now=1000, window_days=7)
    assert stats["landings_total"] == 2
    assert stats["go_arounds"] == 1
    assert stats["parks"] == 1


def test_corner_stats_window_counts_only_recent_landings():
    # now=1_000_000; a 7-day window is 604800 s. A landing 8 days ago is out; one an hour ago is in.
    now = 1_000_000
    week = 7 * 86400
    journal = [_merge("i_old", now - week - 3600), _merge("i_new", now - 3600)]
    stats = flights.corner_stats(journal, now=now, window_days=7)
    assert stats["landings_total"] == 2
    assert stats["landings_window"] == 1


def test_corner_stats_carry_no_human_latency_stopwatch():
    # The standing audit (design record §7, kill list #4): no time-to-approve, needs-you dwell, or
    # decisions/day may EVER appear here. Assert the key set is purely outcome and free of any
    # latency-shaped key.
    stats = flights.corner_stats([], now=1000)
    forbidden = ("approve", "latency", "dwell", "decision", "time_to", "wait", "response")
    assert all(not any(bad in k for bad in forbidden) for k in stats), stats
    assert set(stats) <= {"landings_total", "landings_window", "go_arounds", "parks"}


# =============================== global pill + tower status (§4) ===============================
# The pill aggregates the WORST state across all repos and names the offender.

def test_repo_state_ok_when_healthy():
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND, flights.FINAL], spinning=False,
                           merges_frozen=None, alert=None, heartbeat_age=10.0, heartbeat_down_seconds=300)
    assert r["level"] == "ok" and r["state"] == "ok"


def test_repo_state_flags_a_parked_flight_as_attention():
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND, flights.PARKED], spinning=False,
                           merges_frozen=None, alert=None, heartbeat_age=10.0)
    assert r["level"] == "attention" and r["state"] == flights.PARKED


def test_repo_state_awaiting_outranks_parked():
    # Two attention conditions at once — an owner decision is the more actionable, so it's named.
    r = flights.repo_state(slug="a/b", states=[flights.PARKED, flights.AWAITING], spinning=False,
                           merges_frozen=None, alert=None, heartbeat_age=10.0)
    assert r["state"] == flights.AWAITING


def test_repo_state_merges_freeze_is_calm_attention():
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND], spinning=False,
                           merges_frozen={"reason": "dev red"}, alert=None, heartbeat_age=10.0)
    assert r["level"] == "attention" and r["state"] == flights.MERGES_FREEZE


def test_repo_state_flags_a_stranded_gate_as_attention():
    # A stranded gate is real trouble the owner should see off-screen (§4/§5): finished work that the
    # gate isn't landing. It raises the pill to attention and names itself, so the field agrees.
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND, flights.STRANDED], spinning=False,
                           merges_frozen=None, alert=None, heartbeat_age=10.0)
    assert r["level"] == "attention" and r["state"] == flights.STRANDED


def test_repo_state_alert_is_the_alert_level():
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND], spinning=False,
                           merges_frozen=None, alert={"reasons": ["x"]}, heartbeat_age=10.0)
    assert r["level"] == "alert" and r["state"] == "alert"


def test_repo_state_runner_down_when_heartbeat_stale_or_absent():
    stale = flights.repo_state(slug="a/b", states=[], spinning=False, merges_frozen=None,
                               alert=None, heartbeat_age=999.0, heartbeat_down_seconds=300)
    absent = flights.repo_state(slug="a/b", states=[], spinning=False, merges_frozen=None,
                                alert=None, heartbeat_age=None, heartbeat_down_seconds=300)
    assert stale["state"] == "runner-down" and stale["level"] == "alert"
    assert absent["state"] == "runner-down"


def test_repo_state_runner_down_outranks_a_park():
    # A factory-stop (alert) always beats an attention-level park when both are true.
    r = flights.repo_state(slug="a/b", states=[flights.PARKED], spinning=False, merges_frozen=None,
                           alert=None, heartbeat_age=None, heartbeat_down_seconds=300)
    assert r["state"] == "runner-down"


def test_spinning_flight_is_an_attention_condition():
    r = flights.repo_state(slug="a/b", states=[flights.DOWNWIND], spinning=True, merges_frozen=None,
                           alert=None, heartbeat_age=10.0)
    assert r["level"] == "attention" and r["state"] == "spinning"


def test_global_pill_picks_worst_across_repos_and_names_offender():
    calm = flights.repo_state(slug="titan/calm", states=[flights.DOWNWIND], spinning=False,
                              merges_frozen=None, alert=None, heartbeat_age=10.0)
    troubled = flights.repo_state(slug="titan/troubled", states=[flights.PARKED], spinning=False,
                                  merges_frozen=None, alert=None, heartbeat_age=10.0)
    pill = flights.global_pill([calm, troubled])
    assert pill["level"] == "attention"
    assert pill["state"] == flights.PARKED
    assert pill["offender"] == "titan/troubled"


def test_global_pill_all_clear_names_no_offender():
    calm = flights.repo_state(slug="a/b", states=[flights.DOWNWIND], spinning=False,
                              merges_frozen=None, alert=None, heartbeat_age=10.0)
    pill = flights.global_pill([calm])
    assert pill["level"] == "ok" and pill["offender"] is None


def test_global_pill_empty_field_is_ok():
    assert flights.global_pill([])["level"] == "ok"


def test_tower_status_is_the_pill_level():
    assert flights.tower_status({"level": "ok"}) == "ok"
    assert flights.tower_status({"level": "attention"}) == "attention"
    assert flights.tower_status({"level": "alert"}) == "alert"


# =============================== cargo precedence (issue #48, absorbs #47) ===============================
# A flight's cargo (size, never risk) has a fixed precedence: the PR's own diff size when a PR exists
# (open or merged) — so cargo survives after a landed flight's worktree is cleaned up — else the live
# worktree diff-stat, else honest absence. NEVER a fake zero: an unknown size degrades to the next
# source, not to "+0/−0".

def test_cargo_from_pr_reads_the_prs_diff_size():
    pr = {"state": "MERGED", "additions": 340, "deletions": 12, "changedFiles": 7}
    assert flights.cargo_from_pr(pr) == {"present": True, "files": 7, "added": 340, "removed": 12}


def test_cargo_from_pr_is_none_when_no_pr_or_failed_read():
    # {} is both "no PR for this branch" and "fail-closed gh read" — either way, no PR cargo (fall
    # through to the worktree, never a fabricated zero).
    assert flights.cargo_from_pr({}) is None
    assert flights.cargo_from_pr(None) is None


def test_cargo_from_pr_is_none_when_size_fields_absent():
    # A PR dict from an older/partial read with no size fields must not read as +0/−0 cargo.
    assert flights.cargo_from_pr({"state": "OPEN", "mergeable": "MERGEABLE"}) is None


def test_cargo_from_pr_is_none_when_changed_files_zero_or_nonnumeric():
    # changedFiles < 1 (or non-numeric) can't be a real PR — treat as no honest size, not a zero.
    assert flights.cargo_from_pr({"additions": 0, "deletions": 0, "changedFiles": 0}) is None
    assert flights.cargo_from_pr({"additions": 5, "deletions": 1, "changedFiles": None}) is None


def test_cargo_from_pr_allows_zero_line_change_when_a_file_changed():
    # A pure rename / binary-only PR: files touched, zero line delta — a real (if quiet) size.
    assert flights.cargo_from_pr({"additions": 0, "deletions": 0, "changedFiles": 2}) == {
        "present": True, "files": 2, "added": 0, "removed": 0}


def test_build_flight_cargo_prefers_pr_size_over_worktree():
    # A landed flight: the worktree diff is gone (present False), but the merged PR remembers +340/−12.
    issue = {
        "id": "i16", "num": 16, "status": "merged", "branch": "sl/i16-x", "pr": 19,
        "activity_mtime": None, "journal": [], "report_present": True, "review_present": True,
        "pr_facts": {"state": "MERGED", "additions": 340, "deletions": 12, "changedFiles": 7},
        "cargo": {"present": False, "files": 0, "added": 0, "removed": 0},
    }
    c = flights.build_flight(issue, _REPO)["cargo"]
    assert (c["present"], c["added"], c["removed"], c["files"]) == (True, 340, 12, 7)


def test_build_flight_cargo_falls_back_to_worktree_when_pr_has_no_size():
    # An early in-flight flight: no PR yet, but the live worktree already carries +18/−2.
    issue = {
        "id": "i9", "num": 9, "status": "running", "branch": "sl/i9-x", "pr": None,
        "activity_mtime": _REPO["now"] - 30, "journal": [], "pr_facts": {},
        "cargo": {"present": True, "files": 3, "added": 18, "removed": 2},
    }
    c = flights.build_flight(issue, _REPO)["cargo"]
    assert (c["present"], c["added"], c["removed"], c["files"]) == (True, 18, 2, 3)


def test_build_flight_cargo_is_honest_absence_when_neither_pr_nor_worktree():
    # A landed flight whose PR read failed AND whose worktree is gone: honest absence, never a zero
    # dressed up as delivered cargo.
    issue = {
        "id": "i23", "num": 23, "status": "merged", "branch": "sl/i23-x", "pr": 25,
        "activity_mtime": None, "journal": [], "pr_facts": {},
        "cargo": {"present": False, "files": 0, "added": 0, "removed": 0},
    }
    assert flights.build_flight(issue, _REPO)["cargo"]["present"] is False


# =============================== build_flight — facts → an honest flight (§3/§5/§7) ===============================
# The capstone: every fact for one issue composed into one flight object. Inputs are the shapes the
# server assembles from Task-2 readers + Task-4 gh/diff; cases mirror sample-data.txt flights.

_REPO = {"idle_seconds": 480, "freeze_seconds": 2700, "required_checks": ["tests"],
         "now": 1783190000, "merges_frozen": None, "progress_window_seconds": 900}


def test_build_flight_regenerated_merge_lands_and_celebrates():
    # i16: regenerated once, then a CLEAN merge → attempt 2, touchdown, and a celebration earned.
    issue = {
        "id": "i16", "num": 16, "status": "merged", "branch": "sl/i16-...-r1", "pr": 19,
        "activity_mtime": None, "blocked": None, "awaiting_marker": False,
        "report_present": True, "review_present": True, "closed": False,
        "journal": [{"act": "regenerate", "id": "i16", "ts": 1783188550},
                    {"act": "merge", "id": "i16", "wander": False, "outcome": "ok", "ts": 1783188879}],
        "pr_facts": {"state": "MERGED", "mergeable": "MERGEABLE",
                     "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}]},
        "cargo": {"present": False, "files": 0, "added": 0, "removed": 0}, "diff_delta": None,
    }
    f = flights.build_flight(issue, _REPO)
    assert f["label"] == "SL-16·A2"
    assert f["attempt"] == 2
    assert f["stage"] == flights.TOUCHDOWN
    assert f["on_circuit"] is True
    assert f["wander"] is False
    assert f["celebrate"] is True
    assert f["liveness"] is None          # merged: no live session, so no contrail (not "frozen")


def test_build_flight_wandered_merge_gets_no_celebration():
    # i23: the merge wandered outside declared areas — a real landing, but NO flourish (§7 honesty).
    issue = {
        "id": "i23", "num": 23, "status": "merged", "branch": "sl/i23-x", "pr": 25,
        "activity_mtime": None, "report_present": True, "review_present": True,
        "journal": [{"act": "merge", "id": "i23", "wander": True, "outcome": "ok", "ts": 1783364266}],
        "pr_facts": {}, "cargo": {"present": False, "files": 0, "added": 0, "removed": 0},
    }
    f = flights.build_flight(issue, _REPO)
    assert f["wander"] is True
    assert f["celebrate"] is False


def test_build_flight_parked_carries_its_memo():
    # i7: the answerer timed out and the machine parked — chocks, and the plain memo rides along.
    issue = {
        "id": "i7", "num": 7, "status": "parked", "branch": "sl/i7-x", "pr": None,
        "activity_mtime": None,
        "journal": [{"act": "park", "id": "i7", "num": 7, "needs_william": False,
                     "memo": "answerer a1 timed out after 15 min.", "ts": 1783189995}],
    }
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.PARKED
    assert "answerer" in f["memo"]


def test_build_flight_bounced_is_awaiting_with_reason_and_memo():
    # A BOUNCED: marker → amber awaiting, reason bounced, the memo carried for the Needs You card.
    issue = {
        "id": "i8", "num": 8, "status": "bounced", "branch": None,
        "blocked": "BOUNCED: the footer already ships as of PR #12, so the premise is gone.",
        "activity_mtime": None, "journal": [],
    }
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.AWAITING
    assert f["awaiting_reason"] == "bounced"
    assert "premise is gone" in f["memo"]


def test_build_flight_holding_flight():
    issue = {"id": "i15", "num": 15, "status": "holding", "branch": "sl/i15-x", "pr": 17,
             "activity_mtime": 1783189990, "journal": []}
    assert flights.build_flight(issue, _REPO)["stage"] == flights.HOLDING


def test_build_flight_spinning_worker_is_flagged_not_healthy():
    # A fresh contrail but the same gate re-running with no diff growth → downwind AND spinning.
    now = _REPO["now"]
    journal = [{"act": "gate", "id": "i9", "outcome": "fail", "ts": now - 60 * i} for i in range(6)]
    issue = {"id": "i9", "num": 9, "status": "running", "branch": "sl/i9-x", "pr": None,
             "activity_mtime": now - 30, "journal": journal, "diff_delta": 0,
             "cargo": {"present": True, "files": 1, "added": 0, "removed": 0}}
    f = flights.build_flight(issue, _REPO)
    assert f["liveness"] == flights.FRESH
    assert f["stage"] == flights.DOWNWIND
    assert f["spinning"] is True


def test_build_flight_running_session_carries_a_gate_checklist():
    now = _REPO["now"]
    issue = {"id": "i4", "num": 4, "status": "running", "branch": "sl/i4-x", "pr": 20,
             "activity_mtime": now - 30, "report_present": False, "review_present": False,
             "journal": [{"act": "launch", "id": "i4", "ts": now - 200}],
             "pr_facts": {"mergeable": "MERGEABLE",
                          "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}]}}
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.DOWNWIND
    assert f["gate"]["ci"] is True and f["gate"]["mergeable"] is True
    assert f["gate"]["report"] is False and f["gate"]["cleared"] is False


def test_build_flight_landings_paused_when_repo_frozen():
    # A repo-wide merge freeze doesn't ground the plane — it keeps flying, but its landing clearance
    # is flagged paused (design record §3: freeze is calm; planes keep taking off and flying).
    now = _REPO["now"]
    repo = dict(_REPO, merges_frozen={"reason": "dev red"})
    issue = {"id": "i4", "num": 4, "status": "running", "branch": "sl/i4-x",
             "activity_mtime": now - 30, "journal": []}
    f = flights.build_flight(issue, repo)
    assert f["stage"] == flights.DOWNWIND        # still flying
    assert f["landings_paused"] is True


def test_build_flight_awaiting_marker_does_not_make_it_amber():
    # state/awaiting/<id> long-wait touch present, but the flight is a healthy running session — it
    # stays downwind (a worker's own wait is not an owner decision).
    now = _REPO["now"]
    issue = {"id": "i4", "num": 4, "status": "running", "activity_mtime": now - 30,
             "awaiting_marker": True, "journal": []}
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.DOWNWIND
    assert f["long_wait"] is True
    assert f["awaiting_reason"] is None


# =============================== round-1 review fixes (Codex cross-review) ===============================

def test_landed_clean_requires_a_successful_unwandered_merge():
    # Celebration requires POSITIVE proof (§7), never the mere absence of a wander flag.
    assert flights.landed_clean([{"act": "merge", "outcome": "ok", "wander": False}]) is True
    assert flights.landed_clean([{"act": "merge", "outcome": "ok", "wander": True}]) is False
    assert flights.landed_clean([{"act": "merge", "outcome": "merge failed (retry)"}]) is False
    assert flights.landed_clean([]) is False


def test_celebrate_needs_positive_evidence_not_mere_absence_of_wander():
    # status:merged but the merge event is absent from the slice (dropped/corrupt) → we CANNOT
    # confirm a clean landing, so NO flourish. This closes the §7 hole where a wandered merge whose
    # event went missing would have been celebrated.
    issue = {"id": "i16", "num": 16, "status": "merged", "branch": "x", "pr": 9,
             "activity_mtime": None, "journal": []}
    f = flights.build_flight(issue, _REPO)
    assert f["merged"] is True          # still shown as a landing (status says so)
    assert f["celebrate"] is False      # but no flourish without proof it was clean


def test_celebrate_false_on_a_failed_merge_record():
    issue = {"id": "i16", "num": 16, "status": "merged", "activity_mtime": None,
             "journal": [{"act": "merge", "outcome": "merge failed (will retry)", "ts": 1}]}
    assert flights.build_flight(issue, _REPO)["celebrate"] is False


def test_progress_variety_distinguishes_event_types():
    # 'event' envelopes carry the real fact in event.type — three distinct types are three kinds,
    # not one, so a lively mixed window never false-flags as flat/spinning.
    window = [{"act": "event", "event": {"type": "session_blocked"}},
              {"act": "event", "event": {"type": "idle"}},
              {"act": "event", "event": {"type": "session_finished"}}]
    prog = flights.progress(window, diff_delta=0)
    assert prog["variety"] == 3
    assert prog["flat"] is False


def test_progress_repeated_same_event_type_is_flat():
    window = [{"act": "event", "event": {"type": "idle"}} for _ in range(5)]
    assert flights.progress(window, diff_delta=0)["flat"] is True


def test_corner_stats_outcome_totals_are_invariant_to_now():
    # The outcome TOTALS depend only on the journal, never the clock; only the explicit weekly
    # window tracks the injected now (as "this week" must). Same journal, two clocks → same totals.
    journal = [_merge("i1", 100), {"act": "regenerate", "ts": 50}, {"act": "park", "ts": 60}]
    a = flights.corner_stats(journal, now=1000)
    b = flights.corner_stats(journal, now=10_000_000)
    assert a["landings_total"] == b["landings_total"] == 1
    assert a["go_arounds"] == b["go_arounds"] == 1
    assert a["parks"] == b["parks"] == 1


# =============================== the field bindings (Task 7 — §3/§5/§7) ===============================
# The animated field draws NOTHING it derives itself (design B.1): the contrail kind, the plane's
# underlying circuit position, the runway a lane owns, the living-clock daypart, and the airline
# tail color are all settled HERE, so the squint test still holds with the art deleted.

def test_contrail_crisp_while_young():
    # age 100 s, idle 480 — well inside the fresh tier: bold, bright contrail.
    assert flights.contrail_kind(mtime=900.0, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == "crisp"


def test_contrail_thins_as_the_fresh_tier_ages():
    # §5: "thins as it ages" — age 300 s is past half the idle threshold (240) but not yet idle.
    assert flights.contrail_kind(mtime=700.0, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == "thin"


def test_contrail_sputters_at_the_idle_tier():
    # age 600 s ≥ idle 480 — the sputtering trail is the same 8-min tier where the tower peeks.
    assert flights.contrail_kind(mtime=400.0, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == "sputter"


def test_contrail_gone_at_the_frozen_tier():
    assert flights.contrail_kind(mtime=0.0, now=2700.0,
                                 idle_seconds=480, freeze_seconds=2700) == "none"


def test_contrail_none_without_a_session():
    # No activity file ⇒ no session ⇒ no contrail — never painted "frozen" for lack of a signal.
    assert flights.contrail_kind(mtime=None, now=1000.0,
                                 idle_seconds=480, freeze_seconds=2700) == "none"


def test_contrail_thresholds_are_the_repos_own():
    # A tighter repo thins/sputters the SAME age sooner — per-repo thresholds, never a constant.
    age_300 = dict(mtime=700.0, now=1000.0)
    assert flights.contrail_kind(idle_seconds=480, freeze_seconds=2700, **age_300) == "thin"
    assert flights.contrail_kind(idle_seconds=240, freeze_seconds=600, **age_300) == "sputter"


# --- the living clock (§7 — wall-clock drives lighting; no weather, ever) ---

def _local_epoch(hour):
    import time
    return time.mktime((2026, 7, 7, hour, 30, 0, 0, 0, -1))


def test_daypart_buckets_day_dusk_night():
    assert flights.daypart(_local_epoch(12)) == "day"
    assert flights.daypart(_local_epoch(19)) == "dusk"
    assert flights.daypart(_local_epoch(23)) == "night"
    assert flights.daypart(_local_epoch(3)) == "night"


def test_daypart_dawn_reads_as_the_dusk_wash():
    # 07:30 local: the dawn wash reuses the dusk palette — the prototype has exactly three tints.
    assert flights.daypart(_local_epoch(7)) == "dusk"


# --- runways = the repo's real lanes (§3: "2 runways = 2 concurrent builds") ---

def test_assign_runways_gives_each_lane_its_own_runway():
    m = flights.assign_runways(["i15", None, "i9", "i15"])
    assert set(m) == {"i15", "i9"}          # None is not a lane; duplicates collapse
    assert sorted(m.values()) == [0, 1]     # two lanes → the two runways


def test_assign_runways_is_deterministic_across_input_order():
    assert flights.assign_runways(["i9", "i15"]) == flights.assign_runways(["i15", "i9"])


def test_assign_runways_empty_is_empty():
    assert flights.assign_runways([]) == {}


# --- the empty-queue caption reflects the repo's configured lanes (issue #35) ---
# The empty board used to hardcode "2 RUNWAYS OPEN" for every repo — a false factual claim on a
# truth-first surface for a repo running one lane (or three). The count now comes from the repo's
# configured `lanes`; this pure formatter owns the singular/plural and the honest no-number fallback
# so the JS only binds the finished string (design record B.1).

def test_empty_queue_caption_reflects_the_configured_lane_count():
    assert flights.empty_queue_caption(2) == "QUEUE EMPTY · 2 RUNWAYS OPEN"
    assert flights.empty_queue_caption(3) == "QUEUE EMPTY · 3 RUNWAYS OPEN"


def test_empty_queue_caption_is_singular_for_a_single_lane():
    assert flights.empty_queue_caption(1) == "QUEUE EMPTY · 1 RUNWAY OPEN"


def test_empty_queue_caption_omits_the_count_when_lanes_unknown():
    # No readable lane count → fall back honestly: no invented number (issue #35 DoD).
    assert flights.empty_queue_caption(None) == "QUEUE EMPTY"


def test_empty_queue_caption_never_invents_a_number_for_a_bad_count():
    # A non-positive or non-int input is not a real lane count; the caption claims no number rather
    # than printing e.g. "0 RUNWAYS OPEN" — defence in depth against a false factual claim.
    for bad in (0, -1, "two", True, 2.0):
        assert flights.empty_queue_caption(bad) == "QUEUE EMPTY"


# --- airline identity (§7 — auto-generated default, deterministic colors) ---

def test_airline_color_is_deterministic_and_palette_bound():
    c1 = flights.airline_color("will-titan/command-center")
    assert c1 == flights.airline_color("will-titan/command-center")
    assert c1 in flights.AIRLINE_COLORS
    assert c1.startswith("#") and len(c1) == 7


# --- build_flight carries the field's bindings ---

def test_build_flight_carries_circuit_stage_and_contrail():
    issue = {"id": "i5", "num": 5, "status": "running", "activity_mtime": 1783189900.0,
             "journal": []}
    f = flights.build_flight(issue, _REPO)
    assert f["circuit_stage"] == flights.DOWNWIND
    assert f["contrail"] == "crisp"


def test_frozen_session_never_shows_a_contrail():
    # Status says the SESSION froze; even a paradoxically fresh activity file must not paint a
    # crisp trail on a grey plane — grey/no-contrail is reserved for liveness failure (§5).
    issue = {"id": "i5", "num": 5, "status": "frozen", "activity_mtime": 1783189990.0,
             "journal": []}
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.SESSION_FROZEN
    assert f["contrail"] == "none"


def test_awaiting_flight_keeps_its_underlying_circuit_position():
    # The amber ring renders AT the plane's honest position — an awaiting flight still knows where
    # on the circuit it stopped (report filed → base turn), it never teleports to a magic fix.
    issue = {"id": "i5", "num": 5, "status": "needs_william", "activity_mtime": 1783189900.0,
             "journal": [], "report_present": True}
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.AWAITING
    assert f["circuit_stage"] == flights.BASE_TURN


def test_build_flight_stranded_gate_is_its_own_state_at_the_gate():
    # The whole pipeline (issue #22): a finished investigation — report on disk, status `gating` —
    # whose activity file has aged past the frozen tier is STRANDED at the gate, held AT its final
    # position (never teleported), and trails no contrail (its session is done, not spinning).
    issue = {"id": "i22", "num": 22, "status": "gating",
             "activity_mtime": _REPO["now"] - _REPO["freeze_seconds"] - 60,   # well past frozen
             "journal": [], "report_present": True}
    f = flights.build_flight(issue, _REPO)
    assert f["stage"] == flights.STRANDED
    assert f["stage"] != flights.SESSION_FROZEN
    assert f["circuit_stage"] == flights.FINAL
    assert f["on_circuit"] is False
    assert f["contrail"] == "none"


# =============================== queue order — the departures board (Task 8 / §3) ===============================
# The launch queue is a state diagram too: which flight leaves the stand next is a PURE function of
# eligibility (open + agent-ready + not already flying), the ⚡ expedite label, the priority band,
# and the blocked-by connections declared in each issue's body. A flight whose connection has NOT
# arrived is "awaiting connection SL-N" — shown on the departures board, NEVER in the air (§3). All
# of it derived here so the JS only paints split-flaps; the squint test holds on the queue.


def _cand(num, title="Do the thing", labels=None, body="", created_at="", requeue_front=False,
          typed=True):
    """A departures candidate in the gh issue shape the assembler passes.

    A real approved issue ALWAYS carries exactly one valid ``type:`` label — the runner refuses to
    launch one that doesn't (``issues.eligible``), so the board shows such an issue as paperwork,
    never as launchable. ``typed`` therefore adds the default ``type:build`` whenever the fixture
    doesn't name a ``type:`` label of its own, keeping every ordering fixture a realistic flight; a
    test that is ABOUT the paperwork rules passes ``typed=False`` or its own ``type:`` labels."""
    labels = list(labels or [])
    if typed and not any(n.startswith("type:") for n in flights.label_names(labels)):
        labels = _lbl("type:build") + labels
    return {"num": num, "title": title, "labels": labels, "body": body,
            "created_at": created_at, "requeue_front": requeue_front}


def _lbl(*names):
    """gh's label shape: a list of {"name": ...} dicts."""
    return [{"name": n} for n in names]


def _sat_none(_n):
    """No connection is ever satisfied (used when candidates have no blocked-by)."""
    return False


# ---- blocked-by parsing (issue-body Loop metadata, NOT a label) ----

def test_parse_blocked_by_reads_the_loop_metadata_line():
    body = "## Goal\nx\n\n## Loop metadata\nblocked-by: #5\ntouches: content\n"
    assert flights.parse_blocked_by(body) == [5]


def test_parse_blocked_by_absent_is_empty():
    assert flights.parse_blocked_by("## Goal\nno metadata here\n") == []
    assert flights.parse_blocked_by("") == []
    assert flights.parse_blocked_by(None) == []


def test_parse_blocked_by_multiple_connections_deduped_and_sorted():
    assert flights.parse_blocked_by("blocked-by: #7, #5 and #5") == [5, 7]


def test_parse_blocked_by_is_case_insensitive_and_ignores_other_hashes():
    # Only the blocked-by line's issue refs count — a stray "#9" elsewhere is not a connection.
    body = "see #9 for context\n\nBlocked-By: #12\n"
    assert flights.parse_blocked_by(body) == [12]


# ---- priority band (priority:* label) ----

def test_priority_rank_named_bands_order_high_before_low():
    assert flights.priority_rank(_lbl("priority:high")) < flights.priority_rank(_lbl("priority:low"))
    assert flights.priority_rank(_lbl("priority:normal")) == flights.priority_rank([])  # absent == normal


def test_priority_rank_absent_or_unknown_is_the_middle_band():
    assert flights.priority_rank([]) == flights.priority_rank(_lbl("priority:weird"))
    assert flights.priority_band([]) == "normal"
    assert flights.priority_band(_lbl("priority:high")) == "high"


def test_priority_rank_accepts_string_labels_too():
    # Some callers hand bare label names, not gh dicts; both must work.
    assert flights.priority_rank(["priority:high"]) == flights.priority_rank(_lbl("priority:high"))


def test_priority_is_deterministic_with_multiple_labels():
    # Two priority:* labels: the MOST urgent (min rank) wins, every time — never a set-order flake
    # (Codex cross-review, Task 8). high(0) beats low(2) regardless of label order.
    assert flights.priority_rank(_lbl("priority:low", "priority:high")) == flights.priority_rank(_lbl("priority:high"))
    assert flights.priority_band(_lbl("priority:low", "priority:high")) == "high"
    assert flights.priority_band(_lbl("priority:high", "priority:low")) == "high"


# ---- queue_rows: eligibility + expedite-on-top + priority + number tiebreak ----

def test_queue_orders_by_priority_then_number_when_no_expedite():
    rows = flights.queue_rows(
        [_cand(30, labels=_lbl("priority:low")),
         _cand(12, labels=_lbl("priority:high")),
         _cand(20)],  # normal
        satisfied=_sat_none)
    assert [r["num"] for r in rows] == [12, 20, 30]     # high, then normal, then low
    assert [r["pos"] for r in rows] == [1, 2, 3]         # 1-based launch position
    assert all(r["launchable"] for r in rows)


def test_queue_creation_time_is_the_tiebreak_within_a_band():
    # FIFO within a band means OLDEST-CREATED first — the runner's own tiebreak (issues.sort_key).
    # The board used to tie by issue NUMBER, which is a different order whenever an issue is created
    # out of number order (a reopened/imported issue, or one filed while another sat unapproved).
    rows = flights.queue_rows(
        [_cand(3, created_at="2026-06-01T00:00:00Z"),    # lowest number, but the NEWEST
         _cand(9, created_at="2026-01-01T00:00:00Z"),    # highest number, but the OLDEST
         _cand(7, created_at="2026-03-01T00:00:00Z")],
        satisfied=_sat_none)
    assert [r["num"] for r in rows] == [9, 7, 3]


def test_queue_number_is_only_the_last_resort_tiebreak():
    # Same instant (or no createdAt at all): the runner pre-sorts its candidates by issue number and
    # sorts stably, so number IS its final tiebreak — the board must not flap between two flights.
    rows = flights.queue_rows([_cand(9), _cand(3), _cand(7)], satisfied=_sat_none)
    assert [r["num"] for r in rows] == [3, 7, 9]


def test_expedite_jumps_to_the_top_over_any_priority():
    rows = flights.queue_rows(
        [_cand(12, labels=_lbl("priority:high")),
         _cand(30, labels=_lbl("expedite", "priority:low"))],
        satisfied=_sat_none)
    assert rows[0]["num"] == 30                          # ⚡ beats a higher priority band
    assert rows[0]["expedited"] is True
    assert rows[0]["pos"] == 1
    assert "NEXT OFF THE STAND" in rows[0]["status_text"].upper()


# ---- blocked-by: awaiting connection, never in the air ----

def test_unmet_connection_is_awaiting_not_launchable_and_sinks_to_the_bottom():
    rows = flights.queue_rows(
        [_cand(26, body="blocked-by: #9"), _cand(20)],
        satisfied=lambda n: False)                       # #9 has NOT landed
    by_num = {r["num"]: r for r in rows}
    assert by_num[26]["launchable"] is False
    assert by_num[26]["blocked_by"] == 9
    assert by_num[26]["pos"] is None                     # no launch position — it can't leave the stand
    assert "AWAITING CONNECTION SL-9" in by_num[26]["status_text"].upper()
    # a launchable flight always ranks ahead of a blocked one
    assert rows.index(by_num[20]) < rows.index(by_num[26])


def test_met_connection_makes_the_flight_launchable():
    rows = flights.queue_rows([_cand(26, body="blocked-by: #9")],
                              satisfied=lambda n: n == 9)  # #9 landed → connection arrived
    assert rows[0]["launchable"] is True
    assert rows[0]["blocked_by"] is None
    assert rows[0]["pos"] == 1


def test_expedite_never_launches_a_blocked_flight():
    # ⚡ cannot override an unmet connection — a blocked flight is never in the air (§3).
    rows = flights.queue_rows([_cand(26, labels=_lbl("expedite"), body="blocked-by: #9")],
                              satisfied=lambda n: False)
    assert rows[0]["launchable"] is False
    assert rows[0]["pos"] is None
    assert "AWAITING CONNECTION" in rows[0]["status_text"].upper()


# ---- paperwork: a flight the RUNNER would refuse over its labels (issue #138) ----
# The board's old lie: it applied no eligibility check at all, so a mislabeled issue rendered as
# launchable — even NEXT OFF THE STAND — indefinitely, while the runner silently never took it.

def test_a_missing_type_label_is_paperwork_never_launchable():
    rows = flights.queue_rows([_cand(5, typed=False)], satisfied=_sat_none)
    assert rows[0]["launchable"] is False
    assert rows[0]["status"] == "paperwork"
    assert rows[0]["pos"] is None                        # never a launch position
    assert "PAPERWORK" in rows[0]["status_text"].upper()
    assert "NO TYPE LABEL" in rows[0]["status_text"].upper()


def test_an_unknown_type_label_is_paperwork_and_names_the_label():
    rows = flights.queue_rows([_cand(5, labels=_lbl("type:frobnicate"))], satisfied=_sat_none)
    assert rows[0]["status"] == "paperwork"
    assert rows[0]["refusal"] == "type_unknown"
    assert "type:frobnicate" in rows[0]["refusal_text"]   # the plain words name the bad label


def test_duplicate_model_labels_are_paperwork():
    rows = flights.queue_rows([_cand(5, labels=_lbl("model:opus", "model:sonnet"))],
                              satisfied=_sat_none)
    assert rows[0]["launchable"] is False
    assert rows[0]["refusal"] == "model_duplicate"


def test_duplicate_effort_labels_are_paperwork():
    rows = flights.queue_rows([_cand(5, labels=_lbl("effort:high", "effort:low"))],
                              satisfied=_sat_none)
    assert rows[0]["launchable"] is False
    assert rows[0]["refusal"] == "effort_duplicate"


def test_a_paperwork_flight_is_never_next_off_the_stand():
    # The headline defect: the mislabeled issue is the LOWEST-numbered, highest-priority candidate,
    # so every old rule would have crowned it NEXT OFF THE STAND. The runner would never take it.
    rows = flights.queue_rows(
        [_cand(1, labels=_lbl("priority:high"), typed=False),
         _cand(50, labels=_lbl("priority:low"))],
        satisfied=_sat_none)
    assert rows[0]["num"] == 50                          # the flight the runner would really launch
    assert "NEXT OFF THE STAND" in rows[0]["status_text"].upper()
    by_num = {r["num"]: r for r in rows}
    assert by_num[1]["status"] == "paperwork"
    assert "NEXT OFF THE STAND" not in by_num[1]["status_text"].upper()


def test_expedite_never_lifts_a_paperwork_flight():
    # ⚡ is a priority signal, not a paperwork fix — the runner refuses the issue either way.
    rows = flights.queue_rows([_cand(5, labels=_lbl("expedite"), typed=False)], satisfied=_sat_none)
    assert rows[0]["launchable"] is False
    assert rows[0]["expedited"] is False
    assert rows[0]["pos"] is None


def test_paperwork_and_awaiting_are_distinct_honest_states():
    # Two different reasons a flight can't leave — never collapsed into one another (design §5).
    rows = flights.queue_rows(
        [_cand(5, typed=False), _cand(6, body="blocked-by: #9"), _cand(7)],
        satisfied=_sat_none)
    by_num = {r["num"]: r for r in rows}
    assert by_num[5]["status"] == "paperwork"
    assert by_num[6]["status"] == "awaiting"
    assert by_num[7]["status"] == "queued"
    assert rows[0]["num"] == 7                           # the only launchable one leads the board


# ---- requeue-front: the conflict-rebuilt flight jumps its band (issue #138) ----

def test_a_requeued_flight_goes_to_the_front_of_its_own_band():
    # loopstate's requeue_front: the runner puts a conflict-rebuilt issue ahead of its band, even
    # though it is the NEWEST of them. The board had no concept of this at all — so its order was
    # wrong exactly when a rebuild happened, the moment the owner most needs the truth.
    rows = flights.queue_rows(
        [_cand(10, created_at="2026-01-01T00:00:00Z"),
         _cand(11, created_at="2026-02-01T00:00:00Z"),
         _cand(12, created_at="2026-03-01T00:00:00Z", requeue_front=True)],
        satisfied=_sat_none)
    assert [r["num"] for r in rows] == [12, 10, 11]
    assert rows[0]["pos"] == 1
    assert "NEXT OFF THE STAND" in rows[0]["status_text"].upper()


def test_a_requeue_never_leaves_its_band():
    # It jumps to the front of its OWN band — not over a higher band, and never over ⚡.
    rows = flights.queue_rows(
        [_cand(10, labels=_lbl("priority:high"), created_at="2026-09-01T00:00:00Z"),
         _cand(11, created_at="2026-01-01T00:00:00Z", requeue_front=True),
         _cand(12, labels=_lbl("expedite", "priority:low"), created_at="2026-09-01T00:00:00Z")],
        satisfied=_sat_none)
    assert [r["num"] for r in rows] == [12, 10, 11]      # ⚡, then high band, then the requeued normal


def test_queue_row_carries_flight_and_destination_for_the_board():
    rows = flights.queue_rows([_cand(42, title="Add a splash screen")], satisfied=_sat_none)
    assert rows[0]["flight"] == "SL-42"
    assert rows[0]["destination"] == "Add a splash screen"


def test_empty_queue_is_empty():
    assert flights.queue_rows([], satisfied=_sat_none) == []


# =============================== arrivals backlog cap (issue #30, owner amendment) ===============================
# The split-flap arrivals board pages through a bounded backlog: the smaller of 5 pages or 3 days of
# landings; older entries drop off. The cap is pure semantics (design record B.1) so it is tested
# here — delete the flaps and the retained backlog is still exactly right.
_DAY = 86400
_ARR_NOW = 1_800_000_000


def _arr(num, ts):
    return {"num": num, "ts": ts}


def test_cap_arrivals_keeps_a_small_recent_backlog_untouched():
    rows = [_arr(3, _ARR_NOW - 10), _arr(2, _ARR_NOW - 3600), _arr(1, _ARR_NOW - 7200)]
    out = flights.cap_arrivals(rows, _ARR_NOW)
    assert [r["num"] for r in out] == [3, 2, 1]     # already newest-first, all within 3 days, under cap


def test_cap_arrivals_drops_landings_older_than_three_days():
    rows = [_arr(2, _ARR_NOW - 3600),               # ~1h ago — kept
            _arr(1, _ARR_NOW - 4 * _DAY)]           # 4 days ago — drops off the board
    out = flights.cap_arrivals(rows, _ARR_NOW)
    assert [r["num"] for r in out] == [2]


def test_cap_arrivals_keeps_the_three_day_boundary_inclusive():
    # A landing exactly at the 3-day edge is still within the window (>= cutoff), never dropped.
    rows = [_arr(1, _ARR_NOW - 3 * _DAY)]
    assert [r["num"] for r in flights.cap_arrivals(rows, _ARR_NOW)] == [1]


def test_cap_arrivals_caps_at_five_pages_of_five_newest_first():
    # 30 recent landings → only the newest 25 (5 pages × 5 rows) survive the cap.
    rows = [_arr(i, _ARR_NOW - i) for i in range(30)]   # num i landed i seconds ago → 0 is newest
    out = flights.cap_arrivals(rows, _ARR_NOW)
    assert len(out) == 25
    assert [r["num"] for r in out] == list(range(25))   # newest 25, newest first


def test_cap_arrivals_keeps_unprovable_recency_landings():
    # A status-merged flight with no journal merge proof has ts None (recency unprovable). The field
    # can't draw it, but the board still CARRIES it (§7 honesty) — the age filter must not drop it.
    rows = [_arr(2, _ARR_NOW - 60), _arr(1, None)]
    out = flights.cap_arrivals(rows, _ARR_NOW)
    nums = [r["num"] for r in out]
    assert 1 in nums and 2 in nums
    assert nums[0] == 2 and nums[-1] == 1               # unknown-ts sorts oldest, never above a real one


def test_cap_arrivals_sorts_newest_first_regardless_of_input_order():
    rows = [_arr(1, _ARR_NOW - 100), _arr(3, _ARR_NOW - 10), _arr(2, _ARR_NOW - 50)]
    assert [r["num"] for r in flights.cap_arrivals(rows, _ARR_NOW)] == [3, 2, 1]


def test_cap_arrivals_treats_non_finite_ts_as_unknown_recency():
    # json.loads accepts NaN/Infinity, so a corrupt journal ts can be a non-finite float; a bool is
    # never a real ts. None of these prove age, so the age filter must KEEP them (never crash, never
    # drop) and they sort oldest — pinning the docstring's intentional handling (Codex review nit).
    for bad in (float("nan"), float("inf"), float("-inf"), True):
        rows = [_arr(2, _ARR_NOW - 60), _arr(1, bad)]
        out = flights.cap_arrivals(rows, _ARR_NOW)
        nums = [r["num"] for r in out]
        assert nums == [2, 1], "non-finite/bool ts %r must be kept and sort oldest, not dropped" % (bad,)


def test_cap_arrivals_page_size_default_matches_the_solari_board():
    # The semantic rows-per-page MUST equal the Solari board's MAX_ROWS, or "5 pages" server-side and
    # the client's page count would disagree. The cross-file guard lives in the static-board test; here
    # we pin the server default so a silent change to it fails a unit test too.
    import inspect
    assert inspect.signature(flights.cap_arrivals).parameters["page_size"].default == 5
    assert inspect.signature(flights.cap_arrivals).parameters["max_pages"].default == 5
    assert inspect.signature(flights.cap_arrivals).parameters["max_age_days"].default == 3


def test_a_settled_bounce_still_shows_its_own_memo_not_a_stale_park_memo():
    # Codex cross-review P0 (issue #162). `_exec_bounce` REMOVES `state/blocked/<id>` and settles
    # status to `bounced`, so once the bounce settles the marker holding the amendment is gone and
    # the text survives only in the journal's `bounce` record. Reading only `park` records made a
    # bounced card show an OLDER park's memo — the owner would read the wrong question entirely and
    # accept an amendment he never saw. The most recent hand-back (park OR bounce) wins.
    journal = [
        {"ts": 100, "act": "park", "id": "i8", "num": 8, "memo": "old launch failed"},
        {"ts": 200, "act": "reapprove", "id": "i8"},
        {"ts": 300, "act": "bounce", "id": "i8", "num": 8,
         "memo": "BOUNCED: the premise is gone. Proposed amendment: restyle."},
    ]
    f = flights.build_flight(
        {"id": "i8", "num": 8, "status": "bounced", "branch": "sl/i8", "blocked": None,
         "activity_mtime": None, "journal": journal},
        {"now": 1000, "idle_seconds": 480, "freeze_seconds": 2700, "required_checks": []})
    assert f["stage"] == flights.AWAITING
    assert f["memo"] == "BOUNCED: the premise is gone. Proposed amendment: restyle."


def test_a_live_bounce_marker_still_wins_over_the_journal():
    # Before the bounce settles, the marker IS the freshest truth — unchanged behaviour.
    journal = [{"ts": 300, "act": "bounce", "id": "i8", "num": 8, "memo": "the journal copy"}]
    f = flights.build_flight(
        {"id": "i8", "num": 8, "status": "blocked", "branch": "sl/i8",
         "blocked": "BOUNCED: the marker copy", "activity_mtime": None, "journal": journal},
        {"now": 1000, "idle_seconds": 480, "freeze_seconds": 2700, "required_checks": []})
    assert f["memo"] == "BOUNCED: the marker copy"


def test_a_park_after_a_bounce_still_wins():
    # Ordering, not act-preference: whichever hand-back happened LAST is the one being answered.
    journal = [
        {"ts": 100, "act": "bounce", "id": "i8", "num": 8, "memo": "an old bounce"},
        {"ts": 300, "act": "park", "id": "i8", "num": 8, "memo": "the current park reason"},
    ]
    f = flights.build_flight(
        {"id": "i8", "num": 8, "status": "parked", "branch": "sl/i8", "blocked": None,
         "activity_mtime": None, "journal": journal},
        {"now": 1000, "idle_seconds": 480, "freeze_seconds": 2700, "required_checks": []})
    assert f["memo"] == "the current park reason"
