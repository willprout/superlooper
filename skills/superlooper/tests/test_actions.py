"""actions.decide — the runner's brain as data. The scenario table below IS the failure model
(spec §5, plan Task 10): three real autocode runs proved these exact scenarios are where nights
die, so each is a test, and each was written FAILING first.

Contract highlights under test:
  * decide is PURE and STATE-driven: same inputs -> same ordered action list; a cold restart
    (empty loopstate) reconstructs every in-flight decision from GitHub + disk alone.
  * Notify is a standing rule, asserted per scenario: EVERY transition to parked/needs-william,
    EVERY freeze, EVERY alert must carry an {"act": "notify"} in the same tick's actions. A
    scenario where one of these occurs without a notify FAILS.
  * The two proven defect classes are named and defended: shared mutable defaults (decide must
    never mutate its inputs or share state across calls) and fail-OPEN on wrong-TYPED input
    (every wrong-typed view field must land on the safe action, never an exception, never a
    trusting default).
  * Label mechanics are runner-side only: bounce/park/reclaim actions carry the label payloads;
    nothing here ever asks a worker to move a label.
"""
import copy

import actions
import brief


NOW = 1_750_000_000


# --------------------------- view builders ---------------------------

def cfg(**over):
    c = {
        "repo": "o/r", "dev_branch": "main", "prod_branch": None,
        "lanes": 2, "affinity": "hard", "areas": {"frontend": ["src/f/**"], "api": ["src/a/**"]},
        "touches_required": True, "required_checks": ["ci"], "merge_method": "squash",
        "ship_cmd": None, "ship_recheck_cmd": None,
        "report_required_sections": ["Tests"],
        "bright_lines": [], "cleanup_merged_worktrees": True, "report_time": "08:45",
        "models": {"worker": "opus", "answerer": "fable"},
        "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2},
        "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
               "quarantine": [], "nightly_time": "02:00"},
        "notify": {"imessage_to": None, "cmd": None},
    }
    c.update(over)
    return c


def usage_ok(now=NOW):
    return {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0,
            "last_ok_at": now, "first_attempt_at": now - 60}


def parsed(num, labels=("agent-ready", "type:build"), touches=(), title=None, **over):
    labels = list(labels)
    tvals = [x[len("type:"):] for x in labels if x.startswith("type:")]
    itype = tvals[0] if len(tvals) == 1 and tvals[0] in ("build", "investigate", "diagnose-and-fix") else "invalid"
    p = {"num": num, "id": f"i{num}", "title": title or f"Issue {num}", "type": itype,
         "labels": labels, "touches": list(touches), "blocked_by": [], "parent": None,
         "created_at": f"2026-07-01T00:00:{num % 60:02d}Z",
         "priority": 1 if "priority:high" in labels else 3 if "priority:low" in labels else 2,
         "expedite": "expedite" in labels}
    p.update(over)
    return p


def ist(status="running", **over):
    d = {"status": status, "branch": None, "lane": None, "launches": 1, "retries": 0,
         "conflicts": 0, "requeue_front": False, "declared_touches": [], "pr": None}
    d.update(over)
    return d


def disk(**over):
    d = {"issues_state": {"version": 1, "issues": {}}, "blocked": {}, "reports": {},
         "answers": {}, "exited": {}, "frozen": None, "alert": None, "live_lock_ids": set(),
         "filed_fingerprints": {}, "local_date": "2026-07-02", "local_hhmm": "12:00",
         "last_report_date": "2026-07-02"}
    d.update(over)
    return d


GREEN = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]


def ghv(**over):
    g = {"stale": False, "consecutive_failures": 0, "closed_nums": set(),
         "prs": {}, "issue_comments": {}, "dev_checks": list(GREEN)}
    g.update(over)
    return g


def pr_view(num=555, branch="sl/i5-issue-5", mergeable="MERGEABLE", state="OPEN",
            rollup=None, files=(), labels=(), comments=None, oid="head1"):
    return {"number": num, "state": state, "mergeable": mergeable, "headRefName": branch,
            "headRefOid": oid, "labels": list(labels),
            "statusCheckRollup": list(GREEN) if rollup is None else rollup,
            "files": [{"path": p} for p in files],
            "comments": [{"body": "<!-- superlooper-review --> reviewed, no P0/P1"}]
                        if comments is None else comments}


GOOD_REPORT = "## Tests\n" + ("all green, 300 passed, evidence attached below. " * 3)


_DEFAULT = object()          # sentinel: None must be passable as a real (garbage) input


def decide(now=NOW, config=_DEFAULT, usage=_DEFAULT, parsed_issues=(), lane_state=(),
           events=(), dsk=_DEFAULT, gh_view=_DEFAULT):
    return actions.decide(now, cfg() if config is _DEFAULT else config,
                          usage_ok() if usage is _DEFAULT else usage,
                          list(parsed_issues), list(lane_state), list(events),
                          disk() if dsk is _DEFAULT else dsk,
                          ghv() if gh_view is _DEFAULT else gh_view)


def only(result, act):
    return [a for a in result if a["act"] == act]


def has_notify(result):
    return bool(only(result, "notify"))


# =========================== launches / scheduling ===========================

def test_eligible_issue_launches_with_deterministic_branch():
    out = decide(parsed_issues=[parsed(5, title="Fix the widget")])
    launches = only(out, "launch")
    assert len(launches) == 1
    a = launches[0]
    assert a["id"] == "i5" and a["num"] == 5 and a["orphan"] is False
    assert a["branch"] == brief.branch_for(parsed(5, title="Fix the widget")) == "sl/i5-fix-the-widget"


def test_launches_come_last_in_the_action_list():
    dsk = disk(blocked={"i7": "how should I configure X?"},
               issues_state={"version": 1, "issues": {
                   "i7": ist("blocked", type="investigate")}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    kinds = [a["act"] for a in out]
    assert "launch" in kinds and "hire_answerer" in kinds
    assert kinds.index("hire_answerer") < kinds.index("launch")


def test_usage_stale_launches_nothing_but_everything_else_proceeds():
    stale_usage = {**usage_ok(), "last_ok_at": NOW - 3599}
    dsk = disk(blocked={"i7": "question?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(usage=stale_usage, parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    assert len(only(out, "hire_answerer")) == 1     # everything else proceeds


def test_usage_fail_closed_shapes_never_launch_and_never_raise():
    for bad in (None, {}, [], "x", {"auth_status": "ok"},
                {**usage_ok(), "five_hour_pct": float("nan")},
                {**usage_ok(), "auth_status": "auth_expired"}):
        out = decide(usage=bad, parsed_issues=[parsed(5)])
        assert only(out, "launch") == []


def test_gh_stale_suppresses_launch_gate_and_orphans_but_disk_flows_proceed():
    dsk = disk(blocked={"i7": "BOUNCED: premise gone; amend goal to X"},
               reports={"i9": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i7": ist("blocked"), "i9": ist("gating")}})
    gv = ghv(stale=True, prs={"i9": pr_view()})
    out = decide(parsed_issues=[parsed(5), parsed(8, labels=("in-progress", "type:build"))],
                 dsk=dsk, gh_view=gv)
    assert only(out, "launch") == [] and only(out, "merge") == []
    assert only(out, "reclaim") == []
    assert len(only(out, "bounce")) == 1            # disk-driven flows continue


def test_hard_affinity_overlap_with_running_lane_holds_the_launch():
    lane = [{"id": "i1", "touches": ["frontend"]}]
    out = decide(parsed_issues=[parsed(5, touches=("frontend",))], lane_state=lane)
    assert only(out, "launch") == []


def test_launch_failures_cap_parks_with_notify():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5"
    assert has_notify(out)


def test_reapproving_a_parked_at_cap_issue_reapproves_and_does_not_relaunch_yet():
    # The live-dry-run bug: a parked-on-launch-cap issue stays filtered from launches forever
    # because launch_failures persists at the cap across a re-added agent-ready. Re-approval must
    # emit `reapprove` (the executor resets counters) and hold the launch back one tick so it
    # fires against the RESET counters, not the stale at-cap ones.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("parked", launches=3, retries=2, launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)      # agent-ready present again
    ra = only(out, "reapprove")
    assert len(ra) == 1 and ra[0]["id"] == "i5" and ra[0]["num"] == 5
    assert only(out, "launch") == []                     # launch waits for next tick
    assert only(out, "park") == []                       # never re-parks a fresh approval


def test_reapproving_a_parked_non_cap_issue_still_waits_one_tick():
    # Even when the counters are below cap (parked for some other reason), a re-approved issue is
    # reset-then-launched, never launched same-tick against its old counters.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("needs_william", launches=1, retries=0, conflicts=1)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "reapprove")) == 1
    assert only(out, "launch") == []


def test_reapproving_a_bounced_issue_reapproves():
    # bounced is a park-family terminal status too: a re-added agent-ready re-releases it.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("bounced", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "reapprove")) == 1 and only(out, "launch") == []


def test_a_parked_issue_without_a_fresh_agent_ready_is_left_alone():
    # No agent-ready = William has NOT re-approved: the issue stays parked, no reapprove, no launch.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("parked", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5, labels=("parked", "type:build"))], dsk=dsk)
    assert only(out, "reapprove") == [] and only(out, "launch") == []


def test_a_merged_issue_is_never_reapproved():
    # merged is truly done: even a stray agent-ready must not resurrect and rebuild it.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("merged")}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "reapprove") == [] and only(out, "launch") == []


def test_after_the_reapprove_reset_the_issue_launches_next_tick():
    # Proves the cap really reset: with status back to ready and launch_failures zeroed (the
    # executor's effect), the very issue that was filtered forever now launches.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", branch="sl/i5-x", launches=0, retries=0, launch_failures=0)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "reapprove") == []                  # already released
    assert len(only(out, "launch")) == 1                 # and now it launches


def test_regenerated_issue_relaunches_on_its_stamped_branch():
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", branch="sl/i5-issue-5-r1", conflicts=1, requeue_front=True)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    a = only(out, "launch")[0]
    assert a["branch"] == "sl/i5-issue-5-r1"


# =========================== launch anchor liveness (#24) ===========================
# The 2026-07-09 incident: the launch anchor — the cmux pane every worker tab is born in —
# died mid-run (the runner's tab dragged between cmux windows). Each fresh launch failed
# delivery, and the per-issue cap (2 attempts -> park -> notify) walked the WHOLE queue: 10
# approved issues became 10 parks + 10 notifications. A dead anchor is a SYSTEMIC, runner-level
# fault: ONE alert, launches HELD, the queue left intact — never N per-issue parks. Two
# independent detectors feed one degraded mode: the runner's per-tick pane probe (launch_anchor)
# and the runner's streak of distinct launch-delivery failures (launch_fail_ids).

def _anchor_down():
    return {"ok": False, "reason": "cmux cannot resolve pane 'p1' from this workspace"}


def _anchor_ok():
    return {"ok": True, "reason": ""}


def test_dead_launch_anchor_alerts_once_holds_launches_and_never_parks():
    dsk = disk(launch_anchor=_anchor_down())
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert only(out, "launch") == []                       # every launch held
    assert only(out, "park") == []                         # zero parks: the queue is intact
    assert only(out, "relabel") == []                      # agent-ready never stripped
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["launch_anchor_down"]
    assert len(only(out, "notify")) == 1                   # exactly ONE notify (the alert itself)


def test_dead_launch_anchor_alert_dedupes_across_ticks():
    dsk = disk(launch_anchor=_anchor_down(), alert={"reasons": ["launch_anchor_down"]})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert only(out, "alert") == [] and not has_notify(out)   # already alerted: no repeat
    assert only(out, "launch") == [] and only(out, "park") == []


def test_launch_anchor_down_but_nothing_queued_does_not_alert():
    # Idle: a dead anchor with no approved issue to launch is not yet harmful — no alert, no noise.
    dsk = disk(launch_anchor=_anchor_down())
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk)
    assert only(out, "alert") == [] and not has_notify(out)


def test_dead_anchor_with_a_sole_at_cap_issue_still_alerts_not_silent():
    # An approved issue held under a dead anchor must surface the fault even if it is already at its
    # own launch cap: while degraded its park is SUPPRESSED, so it is part of the held queue — the
    # runner must not sit on it silently (no launch, no park, no alert).
    dsk = disk(launch_anchor=_anchor_down(), launch_fail_ids=["i5"],
               issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == [] and only(out, "park") == []   # held, not parked
    a = only(out, "alert")
    assert len(a) == 1 and "launch_anchor_down" in a[0]["reasons"] and has_notify(out)


def test_launch_anchor_resolves_and_launches_resume_without_relabeling():
    # Once the pane resolves again the held queue launches with NO William touch (agent-ready was
    # never stripped), so recovery needs no re-approval. (Distinct areas so hard affinity lets both
    # go in one tick.)
    dsk = disk(launch_anchor=_anchor_ok())
    out = decide(parsed_issues=[parsed(5, touches=("frontend",)), parsed(6, touches=("api",))],
                 dsk=dsk)
    assert len(only(out, "launch")) == 2 and only(out, "alert") == []


def test_gating_and_merging_continue_while_the_launch_anchor_is_down():
    d, g = _gating()                                       # i5 finished, clean PR
    d["launch_anchor"] = _anchor_down()
    out = decide(parsed_issues=[parsed(9)], dsk=d, gh_view=g)   # i9 queued behind the dead anchor
    assert len(only(out, "merge")) == 1                    # the gate still merges the finished PR
    assert only(out, "launch") == []                       # but the queued launch is held
    assert len(only(out, "alert")) == 1


def test_systemic_launch_failures_across_distinct_issues_alert_and_hold():
    # DoD #3: K delivery failures across DIFFERENT issues while the anchor STILL RESOLVES (probe ok)
    # is the SAME runner-level degraded mode — one alert, queue preserved — reached via the runner's
    # failure streak, not the pane probe.
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=["i5", "i6"])   # probe ok, streak >= cap
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert only(out, "launch") == [] and only(out, "park") == []
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["launch_systemic_failure"]
    assert has_notify(out)


def test_one_distinct_failing_issue_is_not_systemic():
    # A single issue failing delivery (below the distinct-issue cap) is NOT a systemic fault: the
    # queue launches normally (that one issue parks per-issue once it hits its own launch cap — see
    # the next test). Distinct areas so hard affinity lets both go in one tick.
    dsk = disk(launch_fail_ids=["i5"])
    out = decide(parsed_issues=[parsed(5, touches=("frontend",)), parsed(6, touches=("api",))],
                 dsk=dsk)
    assert len(only(out, "launch")) == 2 and only(out, "alert") == []


def test_single_issue_launch_cap_still_parks_when_anchor_healthy():
    # DoD #4: a genuinely issue-SPECIFIC launch failure (one issue at its cap, anchor fine, no
    # other issue failing) still parks that one issue — unchanged.
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=["i5"],
               issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5" and has_notify(out)
    assert only(out, "alert") == []                        # one issue at cap is not systemic


def test_launch_anchor_and_fail_ids_wrong_typed_never_raise_or_falsely_degrade():
    for bad_anchor in (None, "down", 0, [], {"ok": "no"}, {"reason": "x"}):
        for bad_ids in (None, "i5", 3, {"i5": 1}):
            dsk = disk(launch_anchor=bad_anchor, launch_fail_ids=bad_ids)
            out = decide(parsed_issues=[parsed(5)], dsk=dsk)
            assert len(only(out, "launch")) == 1           # nothing degrades on garbage input
            assert only(out, "alert") == []


def test_running_issue_still_labeled_agent_ready_gets_relabel_reconciliation():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(parsed_issues=[parsed(5, labels=("agent-ready", "in-progress", "type:build"))],
                 dsk=dsk)
    assert only(out, "launch") == []
    rl = only(out, "relabel")
    assert len(rl) == 1
    assert rl[0]["add"] == ["in-progress"] and rl[0]["remove"] == ["agent-ready"]


def test_wrong_typed_issue_nums_are_never_scheduled():
    bad = [parsed(5), {**parsed(5), "num": None, "id": "iNone"},
           {**parsed(5), "num": "6", "id": "i6"}, {**parsed(5), "num": True, "id": "iTrue"}]
    out = decide(parsed_issues=bad)
    assert [a["id"] for a in only(out, "launch")] == ["i5"]


# =========================== bounce (runner-side label mechanics) ===========================

def test_bounced_marker_emits_bounce_with_verbatim_memo_and_notify():
    memo = "BOUNCED: the endpoint was removed in #44.\nProposed amendment: target /v2/widgets."
    dsk = disk(blocked={"i7": memo},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    b = only(out, "bounce")
    assert len(b) == 1 and b[0]["id"] == "i7" and b[0]["num"] == 7
    assert b[0]["memo"] == memo                       # quoted verbatim, never paraphrased
    assert has_notify(out)                            # needs-william transition
    assert only(out, "hire_answerer") == []           # a bounce skips the answerer entirely


def test_bounced_already_processed_is_silent():
    dsk = disk(blocked={"i7": "BOUNCED: x"},
               issues_state={"version": 1, "issues": {"i7": ist("bounced")}})
    out = decide(dsk=dsk)
    assert only(out, "bounce") == [] and only(out, "hire_answerer") == []


# =========================== answerer lifecycle ===========================

def test_blocked_question_hires_an_answerer():
    dsk = disk(blocked={"i7": "should I use approach A or B?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    h = only(out, "hire_answerer")
    assert len(h) == 1
    assert h[0] == {"act": "hire_answerer", "id": "i7", "num": 7,
                    "answerer_id": "a1", "question": "should I use approach A or B?"}


def test_active_answerer_record_prevents_a_duplicate_hire():
    dsk = disk(blocked={"i7": "q?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")},
                             "answerers": {"a1": {"for": "i7", "launched_at": NOW - 60}},
                             "next_answerer": 2})
    out = decide(dsk=dsk)
    assert only(out, "hire_answerer") == []


def test_two_blocked_issues_get_distinct_answerer_ids():
    dsk = disk(blocked={"i7": "q7?", "i8": "q8?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked"), "i8": ist("blocked")},
                             "next_answerer": 3})
    out = decide(dsk=dsk)
    ids = [a["answerer_id"] for a in only(out, "hire_answerer")]
    assert sorted(ids) == ["a3", "a4"]


def test_answer_file_with_active_record_delivers():
    dsk = disk(blocked={"i7": "q?"}, answers={"i7": "Use approach A; B breaks migrations."},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")},
                             "answerers": {"a1": {"for": "i7", "launched_at": NOW - 120}}})
    out = decide(dsk=dsk)
    d = only(out, "deliver_answer")
    assert len(d) == 1
    assert d[0]["id"] == "i7" and d[0]["answerer_id"] == "a1"
    assert d[0]["text"] == "Use approach A; B breaks migrations."


def test_park_prefixed_answer_parks_with_notify():
    dsk = disk(blocked={"i7": "q?"}, answers={"i7": "PARK: this needs William's pricing call"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")},
                             "answerers": {"a1": {"for": "i7", "launched_at": NOW - 120}}})
    out = decide(dsk=dsk)
    assert only(out, "deliver_answer") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True
    assert "PARK: this needs William's pricing call" in p[0]["memo"]
    assert "q?" in p[0]["memo"]                       # memo quotes the question
    assert has_notify(out)


def test_answerer_timeout_parks_with_notify():
    dsk = disk(blocked={"i7": "q?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")},
                             "answerers": {"a1": {"for": "i7", "launched_at": NOW - 901}}})
    out = decide(dsk=dsk)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["id"] == "i7" and has_notify(out)
    assert only(out, "hire_answerer") == []


def test_answerer_hire_failures_cap_parks_instead_of_rehiring():
    dsk = disk(blocked={"i7": "q?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", answerer_failures=2)}})
    out = decide(dsk=dsk)
    assert only(out, "hire_answerer") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_answer_delivery_failures_cap_parks_instead_of_redelivering():
    dsk = disk(blocked={"i7": "q?"}, answers={"i7": "answer"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", answer_delivery_failures=3)},
                             "answerers": {"a1": {"for": "i7", "launched_at": NOW - 60}}})
    out = decide(dsk=dsk)
    assert only(out, "deliver_answer") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_stale_answer_without_a_record_is_ignored():
    dsk = disk(blocked={"i7": "a NEW question"}, answers={"i7": "answer to an OLD question"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert only(out, "deliver_answer") == []
    assert len(only(out, "hire_answerer")) == 1       # the new question still gets an answerer


def test_blocked_with_exited_marker_recovers_instead_of_hiring():
    dsk = disk(blocked={"i7": "q?"}, exited={"i7": "1751000000 rc=1"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert only(out, "hire_answerer") == []
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited"


def test_blocked_with_report_goes_to_gate_not_answerer():
    dsk = disk(blocked={"i7": "q?"}, reports={"i7": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert only(out, "hire_answerer") == []
    assert len(only(out, "gate")) == 1


# =========================== liveness recovery ===========================

def test_idle_event_peeks():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(events=[{"type": "session_idle", "id": "i5"}], dsk=dsk)
    r = only(out, "recover")
    assert len(r) == 1 and r[0] == {"act": "recover", "id": "i5", "tier": "idle"}


def test_frozen_event_recovers_under_the_retry_cap():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running", retries=1)}})
    out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=dsk)
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "frozen"


def test_frozen_at_retry_cap_parks_with_notify():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=dsk)
    assert only(out, "recover") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_still_frozen_status_re_recovers_only_after_the_interval():
    recent = disk(issues_state={"version": 1, "issues": {
        "i5": ist("frozen", last_recover_at=NOW - 60)}})
    out = decide(dsk=recent)
    assert only(out, "recover") == []
    overdue = disk(issues_state={"version": 1, "issues": {
        "i5": ist("frozen", last_recover_at=NOW - 601)}})
    out = decide(dsk=overdue)
    assert len(only(out, "recover")) == 1


def test_exited_marker_relaunches_under_cap_and_parks_at_cap():
    under = disk(exited={"i5": "x rc=1"},
                 issues_state={"version": 1, "issues": {"i5": ist("running", retries=1)}})
    out = decide(dsk=under)
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited"
    at_cap = disk(exited={"i5": "x rc=1"},
                  issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(dsk=at_cap)
    assert only(out, "recover") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_relaunch_tiers_respect_the_usage_gate_but_idle_peek_does_not():
    stale_usage = {**usage_ok(), "last_ok_at": NOW - 3599}
    dsk = disk(exited={"i5": "x rc=1"},
               issues_state={"version": 1, "issues": {"i5": ist("running"), "i6": ist("running")}})
    out = decide(usage=stale_usage, events=[{"type": "session_idle", "id": "i6"}], dsk=dsk)
    tiers = {a["tier"] for a in only(out, "recover")}
    assert tiers == {"idle"}                          # exited relaunch waits for usage headroom


def test_exited_with_report_goes_to_gate_not_recovery():
    dsk = disk(exited={"i5": "x rc=0"}, reports={"i5": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(dsk=dsk)
    assert only(out, "recover") == []
    assert len(only(out, "gate")) == 1


# =========================== the gate, translated ===========================

def _gating(status="gating", report=GOOD_REPORT, pv=None, issues_extra=None, **dover):
    st = {"i5": ist(status, branch="sl/i5-issue-5", pr=555)}
    st.update(issues_extra or {})
    d = disk(reports={"i5": report},
             issues_state={"version": 1, "issues": st}, **dover)
    g = ghv(prs={"i5": pr_view() if pv is None else pv})
    return d, g


def test_finished_running_issue_transitions_to_gating():
    dsk = disk(reports={"i5": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(dsk=dsk)                             # no PR data fetched yet
    assert only(out, "gate") == [{"act": "gate", "id": "i5"}]
    assert only(out, "merge") == []                   # evaluation waits for the fetched view


def test_green_gate_merges_with_configured_method():
    d, g = _gating()
    out = decide(dsk=d, gh_view=g)
    m = only(out, "merge")
    assert len(m) == 1
    assert m[0]["num"] == 5 and m[0]["pr"] == 555 and m[0]["method"] == "squash"


def test_missing_sections_nudges_once_then_parks():
    d, g = _gating(report="## Wrong\nstuff")
    out = decide(dsk=d, gh_view=g)
    n = only(out, "nudge")
    assert len(n) == 1 and n[0]["nudge_key"] == "sections" and n[0]["message"]
    d, g = _gating(report="## Wrong\nstuff")
    d["issues_state"]["issues"]["i5"]["nudged"] = ["sections"]
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_gate_park_when_no_pr_exists():
    d, g = _gating(pv={})
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is False and has_notify(out)


# ---------------- bounded pending-checks escalation (issue #26) ----------------
# A required check that never reports reads as "pending" forever, and the pending wait had no
# timer: a finished issue sat in `gating` with no park, no memo, no notify. These pin the bound.

PENDING_ROLLUP = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]  # required "ci" running
UNREPORTED_ROLLUP = []                                                          # required "ci" absent


def _cap(secs):
    return cfg(session={"idle_seconds": 480, "freeze_seconds": 2700,
                        "retry_cap": 2, "conflict_cap": 2, "checks_pending_cap": secs})


def _gating_pending(since=_DEFAULT, rollup=PENDING_ROLLUP):
    over = {} if since is _DEFAULT else {"checks_pending_since": since}
    return _gating(pv=pr_view(rollup=rollup),
                   issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555, **over)})


def test_first_pending_tick_stamps_the_clock():
    d, g = _gating_pending()
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    assert only(out, "note_checks_pending") == [{"act": "note_checks_pending", "id": "i5"}]
    assert only(out, "park") == [] and only(out, "merge") == []


def test_pending_within_bound_waits_without_escalating():
    d, g = _gating_pending(since=NOW - 50)
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    assert only(out, "park") == [] and only(out, "note_checks_pending") == []


def test_pending_past_bound_escalates_once_naming_the_unreported_check():
    d, g = _gating_pending(since=NOW - 61, rollup=UNREPORTED_ROLLUP)
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True
    assert "ci" in p[0]["memo"] and has_notify(out)     # names the unreported check + notifies
    assert only(out, "merge") == []


def test_late_but_in_bound_checks_still_merge_cleanly():
    # the healthy repo: a check reports late (within the bound) and the PR merges — zero behavior
    # change. The stale pending clock is cleared, never an escalation.
    d, g = _gating(issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           checks_pending_since=NOW - 30)})   # default rollup GREEN
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    assert len(only(out, "merge")) == 1 and only(out, "park") == []
    assert only(out, "clear_checks_pending") == [{"act": "clear_checks_pending", "id": "i5"}]


def test_corrupt_pending_clock_restamps_never_escalates():
    for bad in (None, "x", True, float("nan")):
        d, g = _gating_pending(since=bad)
        out = decide(config=_cap(60), dsk=d, gh_view=g)
        assert only(out, "park") == []                  # never escalate off a garbage clock
        assert only(out, "note_checks_pending") == [{"act": "note_checks_pending", "id": "i5"}]


def test_future_pending_clock_cannot_defeat_the_cap():
    # a numeric-but-corrupt FUTURE timestamp makes now-since negative — the old bound would then
    # wait forever again. It must be treated as invalid and re-stamped, never trusted (Codex R1).
    d, g = _gating_pending(since=NOW + 10_000)
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    assert only(out, "park") == []
    assert only(out, "note_checks_pending") == [{"act": "note_checks_pending", "id": "i5"}]


def test_negative_pending_clock_never_escalates_spuriously():
    # a negative timestamp makes now-since huge — it must re-stamp, not escalate on garbage.
    d, g = _gating_pending(since=-5)
    out = decide(config=_cap(60), dsk=d, gh_view=g)
    assert only(out, "park") == []
    assert only(out, "note_checks_pending") == [{"act": "note_checks_pending", "id": "i5"}]


def test_referee_path_gate_parks_needs_william_with_file_memo_and_one_notify():
    d, g = _gating(pv=pr_view(files=["src/f/Widget.tsx", ".superlooper/config.json"]))
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"),
                                      touches=("frontend",))],
                 dsk=d, gh_view=g)
    assert only(out, "merge") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True
    assert ".superlooper/config.json" in p[0]["memo"]
    notices = only(out, "notify")
    assert len(notices) == 1 and ".superlooper/config.json" in notices[0]["body"]


def test_declared_referee_area_still_parks_needs_william():
    config = cfg(areas={"frontend": ["src/f/**"], "loop_rules": [".github/workflows/**"]})
    d, g = _gating(pv=pr_view(files=[".github/workflows/quality.yml"]))
    out = decide(config=config,
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"),
                                       touches=("loop_rules",))],
                 dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True
    assert ".github/workflows/quality.yml" in p[0]["memo"]
    assert len(only(out, "notify")) == 1


def test_conflicting_pr_gets_a_mechanical_update():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    out = decide(dsk=d, gh_view=g)
    u = only(out, "update")
    assert len(u) == 1 and u[0]["pr"] == 555 and u[0]["head_oid"] == "head1"


def test_clean_update_result_waits_for_github_to_recompute():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="clean", update_head_oid="head1")
    out = decide(dsk=d, gh_view=g)
    assert only(out, "update") == [] and only(out, "regenerate") == []


def test_head_change_invalidates_a_stale_update_result():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", oid="head2"))
    d["issues_state"]["issues"]["i5"].update(update_result="clean", update_head_oid="head1")
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "update")) == 1              # stale verdict discarded, retry the update


def test_real_conflict_under_cap_regenerates_on_a_fresh_generation_branch():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid="head1",
                                             conflicts=0)
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    r = only(out, "regenerate")
    assert len(r) == 1
    assert r[0]["new_branch"] == "sl/i5-issue-5-r1" and r[0]["conflicts"] == 1
    assert r[0]["pr"] == 555


def test_conflict_cap_parks_needs_william_with_notify():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid="head1",
                                             conflicts=1)
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and has_notify(out)


def test_preserve_label_hires_a_conflict_resolution_session():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid="head1")
    out = decide(dsk=d, gh_view=g)
    rc = only(out, "resolve_conflict")
    assert len(rc) == 1 and rc[0]["pr"] == 555
    assert only(out, "regenerate") == []


def test_update_error_retries_and_persistent_errors_alert():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="error", update_head_oid="head1",
                                             update_errors=1)
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "update")) == 1              # an infra blip is retried, never a conflict
    assert only(out, "regenerate") == [] and only(out, "alert") == []
    d["issues_state"]["issues"]["i5"]["update_errors"] = 4
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "update")) == 1
    assert len(only(out, "alert")) == 1 and has_notify(out)


def test_overlap_with_inflight_lane_holds_once():
    d, g = _gating(pv=pr_view(files=("src/f/App.tsx",)),
                   issues_extra={"i9": ist("running", declared_touches=["frontend"])})
    out = decide(dsk=d, gh_view=g)
    h = only(out, "hold")
    assert len(h) == 1 and h[0]["overlap_lane"] == "i9"
    d["issues_state"]["issues"]["i5"]["status"] = "holding"
    out = decide(dsk=d, gh_view=g)
    assert only(out, "hold") == []                    # journal-once: already holding


def test_frozen_mainline_holds_merges_but_not_investigation_closes():
    d, g = _gating()
    d["frozen"] = {"reason": "dev red", "fingerprint": "f", "since": NOW - 100}
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == []
    assert len(only(out, "hold")) == 1
    # an investigation close is not a merge: freeze never blocks it
    d2 = disk(reports={"i7": GOOD_REPORT}, frozen={"reason": "dev red", "fingerprint": "f", "since": 1},
              issues_state={"version": 1, "issues": {"i7": ist("gating", type="investigate")}})
    g2 = ghv(prs={}, issue_comments={"i7": [{"body": "<!-- superlooper-investigation --> root cause: X"}]})
    out2 = decide(parsed_issues=[parsed(7, labels=("in-progress", "type:investigate"))],
                  dsk=d2, gh_view=g2)
    assert [a["act"] for a in out2 if a["act"] in ("close_investigate", "hold")] == ["close_investigate"]


def test_finished_investigation_without_marker_nudges_then_parks():
    # ANSWERED-EMPTY (a clean read, marker genuinely absent): nudge once, then park — unchanged.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("gating", type="investigate")}})
    g = ghv(issue_comments={"i7": [{"body": "just chatter"}]})
    p7 = parsed(7, labels=("in-progress", "type:investigate"))
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert len(only(out, "nudge")) == 1
    d["issues_state"]["issues"]["i7"]["nudged"] = ["investigation"]
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_finished_investigation_refused_read_holds_never_parks():
    # REFUSED / STARVED read (iid absent from issue_comments even though the view is FRESH): the
    # gate must HOLD, never nudge, never park, never notify (issue #21 (a): #8's false-park). It
    # journals ONE await_read record (never silent) and dedups on read_waited so the hold is bounded.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("gating", type="investigate")}})
    g = ghv(issue_comments={})            # fresh view, but i7's comment read did not land (refused)
    p7 = parsed(7, labels=("in-progress", "type:investigate"))
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out, "park") == [] and only(out, "nudge") == [] and not has_notify(out)
    assert len(only(out, "await_read")) == 1 and only(out, "await_read")[0]["num"] == 7

    # even with the nudge ledger already spent, a refused read must STILL hold — the old bug parked
    # here because the missing marker looked authoritative.
    d["issues_state"]["issues"]["i7"]["nudged"] = ["investigation"]
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out, "park") == [] and not has_notify(out)

    # bounded: once read_waited is stamped, no further await_read records this episode.
    d["issues_state"]["issues"]["i7"]["read_waited"] = True
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out, "await_read") == [] and only(out, "park") == []


def test_refused_read_recovers_and_the_investigation_closes():
    # After holding on refused reads, a clean read carrying the marker closes the parent cleanly.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {
                 "i7": ist("gating", type="investigate", read_waited=True)}})
    g = ghv(issue_comments={"i7": [{"body": "<!-- superlooper-investigation --> root cause: X"}]})
    p7 = parsed(7, labels=("in-progress", "type:investigate"))
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert [a["act"] for a in out if a["act"] in ("close_investigate", "park", "nudge")] \
        == ["close_investigate"]


def test_stale_view_never_emits_await_read_for_an_investigation():
    # A wholly-stale gh view (a real outage) must not spawn per-issue await_read noise — the poll's
    # consecutive_failures ALERT owns that. await_read is only for a FRESH view missing ONE read.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("gating", type="investigate")}})
    g = ghv(stale=True, issue_comments={})
    out = decide(parsed_issues=[parsed(7, labels=("in-progress", "type:investigate"))], dsk=d, gh_view=g)
    assert only(out, "await_read") == [] and only(out, "park") == []


def test_parked_investigation_reconciles_when_marker_appears_on_a_clean_read():
    # RECONCILIATION (issue #21): a PARKED investigation whose marker comment shows up on a later
    # SUCCESSFUL read is closed — never left parked forever. The issue is terminal (parked), so it
    # is NOT in the open-issue queue; the type comes from loopstate.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("parked", type="investigate")}})
    g = ghv(issue_comments={"i7": [{"body": "<!-- superlooper-investigation --> root cause: X"}]})
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert [a["act"] for a in out] == [a["act"] for a in out if a["act"] == "close_investigate"]
    assert len(only(out, "close_investigate")) == 1 and only(out, "close_investigate")[0]["num"] == 7


def test_parked_investigation_does_not_reconcile_on_a_refused_read():
    # A refused read (absent from issue_comments) must NEVER move a parked issue — reconciliation
    # acts only on a fresh, trustworthy read carrying the marker.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("parked", type="investigate")}})
    g = ghv(issue_comments={})                              # fresh view, i7 read refused/absent
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert only(out, "close_investigate") == []
    # and answered-empty (marker genuinely still absent) also leaves the park untouched.
    g2 = ghv(issue_comments={"i7": [{"body": "still just chatter"}]})
    out2 = decide(parsed_issues=[], dsk=d, gh_view=g2)
    assert only(out2, "close_investigate") == []


def test_merged_pr_state_is_absorbed_not_wedged():
    # Crash window (Codex round-1 C2): gh merged the PR, the runner died before stamping
    # status=merged. Restart must ABSORB the fact — settle local state + labels — not sit in
    # gate-wait forever with a stuck in-progress label.
    d, g = _gating(pv=pr_view(state="MERGED"))
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == [] and only(out, "park") == []
    assert only(out, "absorb_merged") == [{"act": "absorb_merged", "id": "i5", "num": 5}]


def test_recheck_failure_parks_needs_william():
    d, g = _gating()
    d["issues_state"]["issues"]["i5"]["recheck_failed"] = True
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and has_notify(out)


def test_gate_waits_when_pr_data_is_not_yet_fetched():
    d = disk(reports={"i5": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i5": ist("gating")}})
    out = decide(dsk=d, gh_view=ghv(prs={}))          # no entry for i5 at all
    assert only(out, "merge") == [] and only(out, "park") == []


# =========================== orphaned in-progress (restart rebuild) ===========================

def test_orphan_with_open_pr_relaunches_on_the_pr_branch():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")})
    out = decide(parsed_issues=[p8], gh_view=g)
    launches = only(out, "launch")
    assert len(launches) == 1
    assert launches[0]["orphan"] is True and launches[0]["branch"] == "sl/i8-issue-8"


def test_orphan_without_pr_reclaims_to_the_queue():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": {}})
    out = decide(parsed_issues=[p8], gh_view=g)
    r = only(out, "reclaim")
    assert r == [{"act": "reclaim", "id": "i8", "num": 8}]
    assert only(out, "launch") == []


def test_in_progress_with_a_live_lock_is_left_alone():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    d = disk(live_lock_ids={"i8"})
    out = decide(parsed_issues=[p8], dsk=d, gh_view=ghv(prs={"i8": {}}))
    assert only(out, "reclaim") == [] and only(out, "launch") == []


def test_orphan_sweep_waits_for_pr_data():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    out = decide(parsed_issues=[p8], gh_view=ghv(prs={}))
    assert only(out, "reclaim") == [] and only(out, "launch") == []


def test_orphan_with_a_superseded_pr_reclaims_instead_of_resurrecting_it():
    # A partially-executed regenerate (labels moved on the PR but not yet on the issue) must
    # never be "recovered" by relaunching the OLD branch: the superseded PR is dead history.
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8",
                               labels=[{"name": "superseded"}])})
    out = decide(parsed_issues=[p8], gh_view=g)
    assert only(out, "launch") == []
    assert only(out, "reclaim") == [{"act": "reclaim", "id": "i8", "num": 8}]


def test_orphan_with_a_branch_mismatch_reclaims_on_the_stamped_branch_instead():
    # loopstate already stamped a NEWER branch (a regenerate got as far as the state update):
    # the open PR on the old branch is not this issue's active branch — requeue, don't resurrect.
    p8 = parsed(8, labels=("in-progress", "type:build"))
    d = disk(issues_state={"version": 1, "issues": {
        "i8": ist("ready", branch="sl/i8-issue-8-r1", conflicts=1)}})
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")})
    out = decide(parsed_issues=[p8], dsk=d, gh_view=g)
    assert only(out, "launch") == []
    assert only(out, "reclaim") == [{"act": "reclaim", "id": "i8", "num": 8}]


def test_cold_restart_reconstructs_a_finished_issue_straight_to_merge():
    # Empty loopstate (runner died and lost nothing that matters): GitHub + disk rebuild the world.
    d = disk(reports={"i5": GOOD_REPORT})
    g = ghv(prs={"i5": pr_view()})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    assert len(only(out, "gate")) == 1                # status transition rebuilt
    assert len(only(out, "merge")) == 1               # and the decision itself


def test_cold_restart_reconstructs_a_blocked_issue():
    d = disk(blocked={"i7": "q?"})
    out = decide(parsed_issues=[parsed(7, labels=("in-progress", "type:build"))], dsk=d)
    assert len(only(out, "hire_answerer")) == 1


# =========================== red dev: freeze + fix-forward ===========================

def test_red_dev_freezes_files_once_and_notifies():
    out = decide(gh_view=ghv(dev_checks=list(RED)))
    f = only(out, "freeze")
    assert len(f) == 1 and f[0]["fingerprint"]
    fix = only(out, "file_fix_issue")
    assert len(fix) == 1
    assert fix[0]["labels"] == ["type:diagnose-and-fix", "agent-ready",
                                "auto-approved:nightly-red", "expedite"]   # EXACTLY these
    assert "green" in fix[0]["title"].lower() or "green" in fix[0]["body"].lower()
    assert "ci" in fix[0]["title"] or "ci" in fix[0]["body"]
    assert has_notify(out)


def test_red_dev_already_frozen_and_filed_is_silent():
    fp = actions.dev_fingerprint(list(RED), ["ci"])
    d = disk(frozen={"reason": "dev red", "fingerprint": fp, "since": NOW - 100},
             filed_fingerprints={fp: 9001})
    out = decide(dsk=d, gh_view=ghv(dev_checks=list(RED)))
    assert only(out, "freeze") == [] and only(out, "file_fix_issue") == []
    assert not has_notify(out)


def test_filed_fingerprint_freezes_but_does_not_refile():
    fp = actions.dev_fingerprint(list(RED), ["ci"])
    d = disk(filed_fingerprints={fp: 9001})
    out = decide(dsk=d, gh_view=ghv(dev_checks=list(RED)))
    assert len(only(out, "freeze")) == 1 and has_notify(out)
    assert only(out, "file_fix_issue") == []


def test_green_dev_unfreezes():
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out = decide(dsk=d)
    assert len(only(out, "unfreeze")) == 1


def test_no_dev_data_never_unfreezes():
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    g = ghv()
    del g["dev_checks"]
    out = decide(dsk=d, gh_view=g)
    assert only(out, "unfreeze") == []
    g2 = ghv(stale=True, dev_checks=list(GREEN))
    out = decide(dsk=d, gh_view=g2)
    assert only(out, "unfreeze") == []


def test_pending_dev_checks_do_nothing():
    pending = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
    out = decide(gh_view=ghv(dev_checks=pending))
    assert only(out, "freeze") == [] and only(out, "unfreeze") == []


# --- issue #23: the dev view now carries commit STATUSES ({context,state}), not just check-runs.
# A required check that reports on dev only as a commit status must drive freeze/unfreeze exactly
# like a check-run. (The widening lives in gh.branch_checks; decide already folds both shapes —
# these lock that the freeze/unfreeze rule reads a StatusContext dev view correctly.)
STATUS_GREEN = [{"context": "ship", "state": "success"}]
STATUS_RED = [{"context": "ship", "state": "failure"}]


def test_commit_status_only_dev_view_unfreezes_when_green():
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out = decide(config=cfg(required_checks=["ship"]),
                 dsk=d, gh_view=ghv(dev_checks=list(STATUS_GREEN)))
    assert len(only(out, "unfreeze")) == 1


def test_commit_status_only_dev_view_red_freezes_and_stays_frozen():
    # fail-closed: a genuinely red required status freezes...
    out = decide(config=cfg(required_checks=["ship"]),
                 gh_view=ghv(dev_checks=list(STATUS_RED)))
    assert len(only(out, "freeze")) == 1
    # ...and stays frozen on the same breakage (no spurious unfreeze while red).
    fp = actions.dev_fingerprint(list(STATUS_RED), ["ship"])
    d = disk(frozen={"reason": "dev red", "fingerprint": fp, "since": NOW - 100},
             filed_fingerprints={fp: 9001})
    out2 = decide(config=cfg(required_checks=["ship"]),
                  dsk=d, gh_view=ghv(dev_checks=list(STATUS_RED)))
    assert only(out2, "unfreeze") == [] and only(out2, "freeze") == []


def test_check_runs_only_view_missing_the_required_status_stays_pending():
    # THE old permanent-pending, at the view level: when the required status is ABSENT from the
    # dev view (what a check-runs-only poll produced), a frozen mainline never lifts — the exact
    # outage issue #23 fixes by widening gh.branch_checks to also read commit statuses.
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out = decide(config=cfg(required_checks=["ship"]),
                 dsk=d, gh_view=ghv(dev_checks=[]))   # check-runs only; the ship status is invisible
    assert only(out, "unfreeze") == []                # pending forever -> never auto-lifts
    assert only(out, "freeze") == []                  # pending, not red -> no freeze either


# =========================== alerts ===========================

def test_persistent_gh_failure_alerts_once_with_notify():
    g = ghv(stale=True, consecutive_failures=10)
    out = decide(gh_view=g)
    a = only(out, "alert")
    assert len(a) == 1 and any("gh" in r for r in a[0]["reasons"]) and has_notify(out)
    # same alert already on disk -> no repeat, no re-notify
    d = disk(alert={"reasons": a[0]["reasons"]})
    out = decide(dsk=d, gh_view=g)
    assert only(out, "alert") == [] and not has_notify(out)


def test_alert_clears_when_conditions_pass():
    d = disk(alert={"reasons": ["gh_unreachable"]})
    out = decide(dsk=d)
    assert len(only(out, "clear_alert")) == 1


def test_retry_runaway_alerts():
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("running", retries=4)}})
    out = decide(dsk=d)
    a = only(out, "alert")
    assert len(a) == 1 and any("i5" in r for r in a[0]["reasons"]) and has_notify(out)


def test_usage_stale_over_an_hour_alerts():
    stale = {**usage_ok(), "last_ok_at": NOW - 3601}
    out = decide(usage=stale)
    a = only(out, "alert")
    assert len(a) == 1 and any("usage" in r for r in a[0]["reasons"]) and has_notify(out)


# =========================== morning report ===========================

def test_morning_report_fires_once_per_day_after_report_time():
    d = disk(local_hhmm="08:45", local_date="2026-07-03", last_report_date="2026-07-02")
    out = decide(dsk=d)
    assert only(out, "morning_report") == [{"act": "morning_report", "date": "2026-07-03"}]
    d2 = disk(local_hhmm="09:00", local_date="2026-07-03", last_report_date="2026-07-03")
    assert only(decide(dsk=d2), "morning_report") == []
    d3 = disk(local_hhmm="08:00", local_date="2026-07-03", last_report_date="2026-07-02")
    assert only(decide(dsk=d3), "morning_report") == []


# =========================== defense: the two proven defect classes ===========================

def test_decide_never_mutates_its_inputs():
    config = cfg()
    usage = usage_ok()
    plist = [parsed(5), parsed(8, labels=("in-progress", "type:build"))]
    lanes = [{"id": "i9", "touches": ["api"]}]
    events = [{"type": "session_idle", "id": "i9"}]
    d = disk(blocked={"i7": "q?"}, reports={"i5": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("blocked"), "i9": ist("running")}})
    g = ghv(prs={"i5": pr_view(), "i8": {}})
    frozen_args = [copy.deepcopy(x) for x in (config, usage, plist, lanes, events, d, g)]
    actions.decide(NOW, config, usage, plist, lanes, events, d, g)
    actions.decide(NOW, config, usage, plist, lanes, events, d, g)   # twice: no cross-call state
    assert [config, usage, plist, lanes, events, d, g] == frozen_args


def test_decide_is_deterministic():
    d = disk(blocked={"i7": "q?"},
             issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    assert decide(dsk=d) == decide(dsk=d)


def test_wrong_typed_views_fail_closed_not_open():
    # Every wrong-typed input lands on "do nothing dangerous", never an exception into the tick.
    garbage = [None, 42, "x", [], {"issues_state": "nope"}, {"blocked": [1, 2]}]
    for g in garbage:
        out = actions.decide(NOW, cfg(), usage_ok(), [parsed(5)], [], [], g, ghv())
        assert isinstance(out, list)
    for g in (None, 42, "x", [], {"prs": "nope"}, {"dev_checks": "red"}):
        out = actions.decide(NOW, cfg(), usage_ok(), [parsed(5)], [], [], disk(), g)
        assert isinstance(out, list)
        assert only(out, "merge") == [] and only(out, "freeze") == []
    out = actions.decide(NOW, None, None, None, None, None, None, None)
    assert isinstance(out, list) and only(out, "launch") == []


def test_corrupt_cap_counters_park_instead_of_proceeding():
    # Codex round-1 C1 — the fail-OPEN-on-wrong-TYPED defect class, on the cap counters:
    # a corrupt counter must land on the SAFE action (park to William), never re-allow the
    # capped action by reading as 0. Missing (None) still legitimately means 0.
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures="4")}})
    out = decide(parsed_issues=[parsed(5)], dsk=d)
    assert only(out, "launch") == []
    assert len(only(out, "park")) == 1 and has_notify(out)

    d = disk(blocked={"i7": "q?"},
             issues_state={"version": 1, "issues": {"i7": ist("blocked", answerer_failures=True)}})
    out = decide(dsk=d)
    assert only(out, "hire_answerer") == []
    assert len(only(out, "park")) == 1 and has_notify(out)

    d = disk(blocked={"i7": "q?"}, answers={"i7": "answer"},
             issues_state={"version": 1,
                           "issues": {"i7": ist("blocked", answer_delivery_failures="9")},
                           "answerers": {"a1": {"for": "i7", "launched_at": NOW - 60}}})
    out = decide(dsk=d)
    assert only(out, "deliver_answer") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_explicit_null_counter_is_corruption_not_zero():
    # Codex round-2: nothing in this system ever WRITES null into a counter, so a present
    # null is corruption — it must park, not read as a clean 0 (fail closed).
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=None)}})
    out = decide(parsed_issues=[parsed(5)], dsk=d)
    assert only(out, "launch") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_corrupt_update_errors_counter_alerts():
    d = disk(issues_state={"version": 1, "issues": {
        "i5": ist("gating", update_errors="99")}})
    out = decide(dsk=d)
    a = only(out, "alert")
    assert len(a) == 1 and any("update_errors:i5" in r for r in a[0]["reasons"])
    assert has_notify(out)


def test_wrong_typed_issue_records_fail_closed():
    d = disk(blocked={"i7": "q?"},
             issues_state={"version": 1, "issues": {"i7": "corrupt", "i8": None}})
    out = decide(dsk=d)
    assert isinstance(out, list)                      # no raise into the tick


def test_a_tick_where_every_helper_returned_empty_is_a_quiet_tick():
    out = actions.decide(NOW, cfg(), {}, [], [], [], disk(), ghv(dev_checks=[]))
    acts = {a["act"] for a in out}
    assert "merge" not in acts and "launch" not in acts and "park" not in acts


# =========================== ordering ===========================

def test_safety_actions_precede_work_actions():
    d = disk(frozen=None)
    g = ghv(dev_checks=list(RED))
    out = decide(parsed_issues=[parsed(5)], dsk=d, gh_view=g)
    kinds = [a["act"] for a in out]
    assert kinds.index("freeze") < len(kinds)         # freeze present...
    launch_idx = [i for i, k in enumerate(kinds) if k == "launch"]
    if launch_idx:                                    # ...and any launch comes after it
        assert kinds.index("freeze") < launch_idx[0]


# =========================== lane_state helper ===========================

def test_lane_state_from_counts_only_inflight_statuses():
    st = {"version": 1, "issues": {
        "i1": ist("running", declared_touches=["api"], type="build"),
        "i2": ist("blocked", type="investigate"), "i3": ist("frozen"), "i4": ist("exited"),
        "i5": ist("gating"), "i6": ist("merged"), "i7": ist("parked"),
        "i8": "corrupt"}}
    lanes = actions.lane_state_from(st)
    assert [x["id"] for x in lanes] == ["i1", "i2", "i3", "i4"]
    assert lanes[0]["touches"] == ["api"]
    assert lanes[0]["type"] == "build"
    assert lanes[1]["type"] == "investigate"
    assert "type" not in lanes[2]
    assert actions.lane_state_from(None) == []
    assert actions.lane_state_from({"issues": "corrupt"}) == []


def test_territory_claims_from_holds_inflight_and_finished_builds_only():
    st = {"version": 1, "issues": {
        "i1": ist("running", declared_touches=["api"], type="build"),
        "i2": ist("blocked", declared_touches=["ops"], type="investigate"),
        "i3": ist("gating", declared_touches=["frontend"], type="build"),
        "i4": ist("holding", declared_touches=["docs"], type="diagnose-and-fix"),
        "i5": ist("gating", declared_touches=["frontend"], type="investigate"),
        "i6": ist("ready", declared_touches=["frontend"], branch="sl/i6-x-r1",
                  conflicts=1, requeue_front=True),
        "i7": ist("merged", declared_touches=["frontend"]),
        "i8": ist("parked", declared_touches=["frontend"]),
        "i9": ist("needs_william", declared_touches=["frontend"]),
        "i10": ist("bounced", declared_touches=["frontend"]),
        "i11": "corrupt"}}
    claims = actions.territory_claims_from(st)
    assert [x["id"] for x in claims] == ["i1", "i3", "i4"]
    assert claims[0]["touches"] == ["api"]
    assert claims[0]["type"] == "build"
    assert claims[2]["type"] == "diagnose-and-fix"
    assert actions.territory_claims_from(None) == []
    assert actions.territory_claims_from({"issues": "corrupt"}) == []


def test_finished_claim_holds_overlapping_launch_but_does_not_consume_capacity():
    dsk = disk(issues_state={"version": 1, "issues": {
        "i9": ist("gating", declared_touches=["frontend"], type="build")}})
    out = decide(config=cfg(lanes=1, affinity="hard"),
                 parsed_issues=[parsed(1, touches=["frontend"]), parsed(2, touches=["api"])],
                 dsk=dsk)
    launches = only(out, "launch")
    assert [a["id"] for a in launches] == ["i2"]


def test_finished_claim_release_on_merge_regenerate_and_park_allows_overlap():
    for status, extra in (
        ("merged", {}),
        ("ready", {"branch": "sl/i9-old-r1", "conflicts": 1, "requeue_front": True}),
        ("parked", {}),
        ("needs_william", {}),
    ):
        dsk = disk(issues_state={"version": 1, "issues": {
            "i9": ist(status, declared_touches=["frontend"], type="build", **extra)}})
        out = decide(config=cfg(lanes=1, affinity="hard"),
                     parsed_issues=[parsed(1, touches=["frontend"])],
                     dsk=dsk)
        assert [a["id"] for a in only(out, "launch")] == ["i1"], status


def test_parked_wildcard_claim_releases_so_no_touches_repo_does_not_freeze():
    dsk = disk(issues_state={"version": 1, "issues": {
        "i9": ist("parked", declared_touches=[], type="build")}})
    out = decide(config=cfg(lanes=1, affinity="hard"),
                 parsed_issues=[parsed(1, touches=[])],
                 dsk=dsk)
    assert [a["id"] for a in only(out, "launch")] == ["i1"]


def test_finished_investigations_neither_hold_nor_are_held_by_claims():
    build_claim = disk(issues_state={"version": 1, "issues": {
        "i9": ist("gating", declared_touches=["frontend"], type="build")}})
    inv_out = decide(config=cfg(lanes=1, affinity="hard"),
                     parsed_issues=[parsed(1, labels=("agent-ready", "type:investigate"),
                                           touches=["frontend"])],
                     dsk=build_claim)
    assert [a["id"] for a in only(inv_out, "launch")] == ["i1"]

    inv_claim = disk(issues_state={"version": 1, "issues": {
        "i9": ist("gating", declared_touches=["frontend"], type="investigate")}})
    build_out = decide(config=cfg(lanes=1, affinity="hard"),
                       parsed_issues=[parsed(1, touches=["frontend"])],
                       dsk=inv_claim)
    assert [a["id"] for a in only(build_out, "launch")] == ["i1"]


def test_corrupt_issue_state_stops_fresh_launches_fail_closed():
    for bad_state in (
        {"version": 1},
        {"version": 1, "issues": "corrupt"},
        {"version": 1, "issues": {"i9": "corrupt"}},
        ["not", "state"],
    ):
        out = decide(parsed_issues=[parsed(1, touches=["frontend"])],
                     dsk=disk(issues_state=bad_state))
        assert only(out, "launch") == [], bad_state
