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

import pytest

import actions
import brief
import evidence
import gate


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


def parsed(num, labels=("agent-ready", "type:build"), touches=("frontend",), title=None, **over):
    # touches defaults to a real declared area: the default cfg sets touches_required=True, so a
    # realistic approved issue DECLARES what it touches. Tests that exercise the no-touches path
    # pass touches=() explicitly (usually paired with config=cfg(touches_required=False)).
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
         "answers": {}, "exited": {}, "launch_stderr": {}, "frozen": None, "alert": None,
         "live_lock_ids": set(), "filed_fingerprints": {}, "local_date": "2026-07-02",
         "local_hhmm": "12:00", "last_report_date": "2026-07-02"}
    d.update(over)
    return d


GREEN = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]


def ghv(**over):
    g = {"stale": False, "consecutive_failures": 0, "closed_nums": set(),
         "prs": {}, "issue_comments": {}, "dev_checks": list(GREEN)}
    g.update(over)
    return g


# Well-formed 40-hex head oids. The gate parses the real shape (#154: the review verdict is pinned
# to the head it reviewed), so a fixture oid must BE an oid — but these stay readable rather than
# random so "the head moved" is legible in a test.
HEAD1 = "1" * 40
HEAD2 = "2" * 40


def pr_view(num=555, branch="sl/i5-issue-5", mergeable="MERGEABLE", state="OPEN",
            rollup=None, files=(), labels=(), comments=None, oid=HEAD1):
    # the default comments carry a verdict PINNED to this PR's head — the shape a worker posts,
    # and the only shape the gate accepts as evidence for the code being merged (#154).
    return {"number": num, "state": state, "mergeable": mergeable, "headRefName": branch,
            "headRefOid": oid, "labels": list(labels),
            "statusCheckRollup": list(GREEN) if rollup is None else rollup,
            "files": [{"path": p} for p in files],
            "comments": [{"body": f"{gate.pinned_review_marker(oid)} reviewed, no P0/P1"}]
                        if comments is None else comments}


GOOD_REPORT = "## Tests\n" + ("all green, 300 passed, evidence attached below. " * 3)


_DEFAULT = object()          # sentinel: None must be passable as a real (garbage) input


def decide(now=NOW, config=_DEFAULT, usage=_DEFAULT, parsed_issues=(), lane_state=(),
           events=(), dsk=_DEFAULT, gh_view=_DEFAULT, wake_grace_until=None):
    return actions.decide(now, cfg() if config is _DEFAULT else config,
                          usage_ok() if usage is _DEFAULT else usage,
                          list(parsed_issues), list(lane_state), list(events),
                          disk() if dsk is _DEFAULT else dsk,
                          ghv() if gh_view is _DEFAULT else gh_view,
                          wake_grace_until=wake_grace_until)


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
    assert "launch" in kinds and "post_question" in kinds
    assert kinds.index("post_question") < kinds.index("launch")


def test_usage_stale_within_grace_launches_nothing_but_everything_else_proceeds():
    # Stale but WITHIN the fail-open grace (issue #46): still fails closed on usage, so no launch,
    # while every non-launch flow proceeds. (Past the grace it would fail OPEN — covered separately.)
    stale_usage = {**usage_ok(), "last_ok_at": NOW - 600}       # 10 min: > stale (5m), < grace (30m)
    dsk = disk(blocked={"i7": "question?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(usage=stale_usage, parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    assert only(out, "fail_open") == []             # within the grace: not yet failing open
    assert len(only(out, "post_question")) == 1     # everything else proceeds


def test_usage_fail_closed_shapes_never_launch_and_never_raise():
    for bad in (None, {}, [], "x", {"auth_status": "ok"},
                {**usage_ok(), "five_hour_pct": float("nan")},
                {**usage_ok(), "auth_status": "auth_expired"}):
        out = decide(usage=bad, parsed_issues=[parsed(5)])
        assert only(out, "launch") == []


# --------------------------- fail OPEN on an UNREADABLE meter (issue #46) ---------------------------
# Split the two dark-meter cases: a meter that successfully READS exhausted keeps failing CLOSED (a
# true, cheap "don't launch"); a meter that is UNREADABLE past a bounded grace FAILS OPEN (launch
# normally, journal it once, alert once) — a full stop with real usage low is a worse failure than
# launching into maybe-exhausted quota. NB: `usage_ok()` uses fresh timestamps (last_ok_at == NOW),
# so a dark meter is modeled by an OLD last_ok_at — exactly what the runner leaves behind when a
# fetch fails (it freezes the last-good reading and its timestamp). The current auth_status the
# runner shows is the last-good "ok"; the injected api_error/no_keychain/auth_expired variants prove
# the decision keys on the DARK DURATION, not on any single reported status.

def dark_usage(status="ok", age=2400):
    # age seconds since the last good read; default 2400s (40 min) is past the 30-min grace.
    return {"auth_status": status, "five_hour_pct": 10.0, "seven_day_pct": 20.0,
            "last_ok_at": NOW - age, "first_attempt_at": NOW - age - 120}


def test_unreadable_meter_past_grace_fails_open_and_journals_once():
    for status in ("api_error", "no_keychain", "auth_expired", "ok"):   # 'ok' == the frozen last-good
        out = decide(usage=dark_usage(status=status), parsed_issues=[parsed(5)])
        assert len(only(out, "launch")) == 1, status                    # launches PROCEED (fail open)
        assert len(only(out, "fail_open")) == 1, status                 # one bounded fail-open record
    # dedup: with the dark episode already recorded on disk (usage_stale alerted), no second record
    d = disk(alert={"reasons": ["usage_stale"], "since": NOW - 100})
    out = decide(usage=dark_usage(), parsed_issues=[parsed(5)], dsk=d)
    assert only(out, "fail_open") == []                                 # already journaled this episode
    assert len(only(out, "launch")) == 1                               # ...but still launching


def test_unreadable_meter_within_grace_still_fails_closed():
    # A brief blip (dark but WITHIN the grace) still fails closed — the grace rides out transients
    # before launching blind.
    out = decide(usage=dark_usage(age=600), parsed_issues=[parsed(5)])  # 10 min < 30-min grace
    assert only(out, "launch") == []
    assert only(out, "fail_open") == []


def test_read_exhausted_meter_still_fails_closed():
    # A SUCCESSFUL read at/over either ceiling is a true, cheap "don't launch" — unchanged, and NEVER
    # a fail-open episode (a fresh reading is not a dark meter).
    for over in ({**usage_ok(), "five_hour_pct": 95.0},
                 {**usage_ok(), "seven_day_pct": 97.0}):
        out = decide(usage=over, parsed_issues=[parsed(5)])
        assert only(out, "launch") == []
        assert only(out, "fail_open") == []


def test_meter_recovery_resumes_gating_and_closes_the_episode():
    # Episode active on disk (usage_stale alerted). A fresh ok read closes it in the journal and
    # resumes normal gating.
    d = disk(alert={"reasons": ["usage_stale"], "since": NOW - 100})
    out = decide(usage=usage_ok(), parsed_issues=[parsed(5)], dsk=d)
    assert len(only(out, "usage_recovered")) == 1     # the fail-open episode closes in the journal
    assert len(only(out, "launch")) == 1              # normal gating resumes (under the ceiling)
    assert len(only(out, "clear_alert")) == 1         # the usage_stale alert clears


def test_usage_stale_alert_fires_once_per_dark_episode_while_failing_open():
    out = decide(usage=dark_usage(), parsed_issues=[parsed(5)])
    a = only(out, "alert")
    assert len(a) == 1 and "usage_stale" in a[0]["reasons"] and has_notify(out)
    assert len(only(out, "launch")) == 1              # ...and work continues while the alert stands
    # same dark episode already alerted on disk -> no repeat alert, no re-notify, no second record
    d = disk(alert={"reasons": a[0]["reasons"], "since": NOW - 100})
    out2 = decide(usage=dark_usage(), parsed_issues=[parsed(5)], dsk=d)
    assert only(out2, "alert") == [] and not has_notify(out2)
    assert only(out2, "fail_open") == []


def test_wake_grace_holds_the_usage_stale_alert_and_fail_open():
    # Issue #42: closing the laptop overnight makes the last good usage read look ancient on the
    # first post-sleep tick, which would fire a usage_stale ALERT text that self-clears a minute
    # later once the next fetch lands. WITHIN the wake grace the fresh dark crossing is held: no
    # usage_stale alert, no fail-open launch policy.
    out = decide(usage=dark_usage(age=2400), parsed_issues=[parsed(5)],
                 wake_grace_until=NOW + 300)
    assert only(out, "fail_open") == []
    assert not any("usage_stale" in a.get("reasons", []) for a in only(out, "alert"))


def test_usage_stale_rearms_after_the_wake_grace_if_still_dark():
    # A meter that is genuinely dark past the grace still alarms — the wake grace only delays the
    # re-arm, it never silences a real outage.
    out = decide(usage=dark_usage(age=2400), parsed_issues=[parsed(5)],
                 wake_grace_until=NOW - 1)             # the grace has already expired
    a = only(out, "alert")
    assert a and "usage_stale" in a[0]["reasons"] and has_notify(out)
    assert len(only(out, "fail_open")) == 1


def test_wake_grace_never_closes_a_genuine_ongoing_dark_episode():
    # A dark episode already established on disk (prev_dark). The wake grace gates only the FRESH
    # crossing, never an in-flight episode: it must NOT emit usage_recovered nor clear the alert
    # while the meter is still dark.
    d = disk(alert={"reasons": ["usage_stale"], "since": NOW - 9000})
    out = decide(usage=dark_usage(age=2400), parsed_issues=[parsed(5)], dsk=d,
                 wake_grace_until=NOW + 300)
    assert only(out, "usage_recovered") == []
    assert only(out, "clear_alert") == []


def test_wake_grace_holds_the_frozen_recovery_ladder():
    # A session already in `frozen` status before the sleep would otherwise be re-nudged on the first
    # post-gap tick. Within the wake grace the recovery ladder is held; past it, it resumes.
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("frozen", last_recover_at=NOW - 9000)}})
    held = decide(parsed_issues=[parsed(5)], dsk=d, wake_grace_until=NOW + 300)
    assert [a for a in held if a.get("act") == "recover" and a.get("tier") == "frozen"] == []
    resumed = decide(parsed_issues=[parsed(5)], dsk=d, wake_grace_until=NOW - 1)
    assert [a for a in resumed if a.get("act") == "recover" and a.get("tier") == "frozen"]


def test_usage_stale_alert_text_names_the_three_causes_and_the_remedy():
    # The usage_stale ALERT used to name no cause and no remedy, while the real fix lived only in a
    # code comment (issue #40). The alert text must now name the THREE proven causes and point at
    # where the remedy lives, so an operator can act off the push alone.
    msg = actions._alert_message("usage_stale")
    low = msg.lower()
    # cause 1 — expired Claude auth (re-login):
    assert "re-login" in low or "re-log in" in low
    assert "auth" in low or "keychain" in low
    # cause 2 — a stale pinned client version (bump USER_AGENT_VERSION in usage.py):
    assert "user_agent_version" in low
    assert "usage.py" in low
    # cause 3 — broken TLS trust in the invoking Python (post-approval amendment, 2026-07-10):
    assert "certificate_verify_failed" in low
    assert "install certificates.command" in low
    # ...and it names WHERE to diagnose/remedy, not just the symptom:
    assert "superlooper doctor" in low


def test_systemic_launch_failure_alert_names_app_nap_and_the_exact_remedy():
    # The systemic launch breaker trips exactly when the operator has walked away (issue #120) — the
    # moment they most need the alert to name the real cause. The message must name macOS App Nap and
    # carry the exact `defaults write` command + the cmux-relaunch step, so a "walked away" breaker
    # trip is never silent about what to run.
    msg = actions._alert_message("launch_systemic_failure")
    low = msg.lower()
    assert "app nap" in low or "app-nap" in low
    assert "defaults write com.cmuxterm.app nsappsleepdisabled -bool true" in low
    # must tell the operator to relaunch cmux (the flag is read only at app launch):
    assert any(w in low for w in ("relaunch", "restart", "quit"))
    # the breaker's hold-the-queue behavior is unchanged — the message still says the queue is held:
    assert "held" in low or "intact" in low


def test_restart_mid_outage_keeps_failing_open_and_never_false_recovers():
    # The dark-meter EPISODE marker (the usage_stale ALERT) is durable, but the runner's in-memory
    # grace clock resets on restart. On the first tick after a restart DURING an ongoing outage, the
    # meter is still dark (last_ok_at None, first_attempt_at just set) — byte-identical to a cold
    # start. prev_dark (the durable marker) must keep the episode FAILING OPEN, keep the alert, and
    # never falsely declare recovery or retract the alert. (Regression: keyed on the reset clock, the
    # first post-restart tick used to emit a false usage_recovered + clear the outage alert.)
    reset_clock = {"auth_status": "api_error", "five_hour_pct": None, "seven_day_pct": None,
                   "last_ok_at": None, "first_attempt_at": NOW - 5}     # clock just reset by restart
    d = disk(alert={"reasons": ["usage_stale"], "since": NOW - 9000})   # episode predates the restart
    out = decide(usage=reset_clock, parsed_issues=[parsed(5)], dsk=d)
    assert len(only(out, "launch")) == 1               # still FAILING OPEN across the restart
    assert only(out, "usage_recovered") == []          # NOT a false recovery
    assert only(out, "clear_alert") == []              # the dark-meter alert is NOT retracted
    assert only(out, "fail_open") == []                # already journaled: no duplicate open record


def test_malformed_usage_mid_episode_fails_closed_but_keeps_the_alert():
    # Defect-class-2 guard for the fail-open path: even with a dark episode ACTIVE on disk, a
    # malformed/absent usage view (no timeline) must NOT launch (never fail open on wrong-typed
    # input), must NOT clear the dark-meter alert, and must NOT false-recover. The episode simply
    # persists until a genuinely fresh read closes it.
    for bad in ({}, None, "x", {"auth_status": "api_error"}):
        d = disk(alert={"reasons": ["usage_stale"], "since": NOW - 9000})
        out = decide(usage=bad, parsed_issues=[parsed(5)], dsk=d)
        assert only(out, "launch") == [], bad             # fail CLOSED on wrong-typed input
        assert only(out, "clear_alert") == [], bad        # alert not retracted
        assert only(out, "usage_recovered") == [], bad    # no false recovery


def test_stale_over_ceiling_reading_fails_open_past_grace_by_design():
    # A last-known-OVER-ceiling read that then goes dark past the grace fails OPEN — once the meter is
    # dark we no longer trust the stale pct, and the owner's ruling is launch-beats-full-stop (#24
    # backstops a real collapse). Documents the deliberate behavior the scheduler docstring calls out.
    stale_over = {"auth_status": "ok", "five_hour_pct": 95.0, "seven_day_pct": 14.0,
                  "last_ok_at": NOW - 2400, "first_attempt_at": NOW - 7200}   # 95%, then dark 40 min
    out = decide(usage=stale_over, parsed_issues=[parsed(5)])
    assert len(only(out, "launch")) == 1
    assert len(only(out, "fail_open")) == 1


def test_failing_open_never_cascades_parks_under_a_systemic_launch_failure():
    # DoD: while failing open, a SYSTEMIC launch failure (#24/#153) holds the queue with ONE
    # runner-level alert — it never parks issues or strips agent-ready across the queue. The streak
    # is channel-only, so the issues in it carry NO per-issue charge; the hold is what keeps them
    # queued and intact for recovery.
    d = disk(launch_fail_ids=["i5", "i6"],
             issues_state={"version": 1, "issues": {
                 "i5": ist("ready"), "i6": ist("ready")}})
    out = decide(usage=dark_usage(), parsed_issues=[parsed(5), parsed(6)], dsk=d)
    assert only(out, "park") == []                    # no park cascade
    assert only(out, "launch") == []                  # launches HELD (queue intact for recovery)
    a = only(out, "alert")
    assert len(a) == 1 and "launch_systemic_failure" in a[0]["reasons"]


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


def test_base_missing_launch_failure_parks_naming_the_branch_not_the_shim():
    # issue #28: when the launch cap is hit because the worktree base branch is missing (the runner
    # stamped launch_error="base_missing"), the park memo must name the REAL cause — the missing
    # base branch — and must NOT send the newcomer chasing the launch shim.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", launch_failures=2, launch_error="base_missing")}})
    out = decide(config=cfg(dev_branch="develop"), parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5"
    memo = parks[0]["memo"]
    assert "develop" in memo                       # names the real base branch
    assert "shim" not in memo.lower()              # NOT the launch-shim wild-goose-chase
    assert has_notify(out)


def _launch_ev(rc, captured):
    """The evidence record _exec_launch stamps into loopstate at the moment a launch fails."""
    return evidence.build("launch", rc=rc, captured=captured)


def test_a_shim_not_fired_cap_names_the_shim_because_that_is_what_the_evidence_says():
    # The shim question is not banned — it is EARNED. rc=2 means a tab was created and no worker
    # ever started in it, which is precisely the shim's failure, so the memo still asks about it.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist(
        "ready", launch_failures=2,
        launch_evidence=_launch_ev(2, "[i5] LAUNCH NOT DELIVERED: no worker started in tab"))}})
    parks = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")
    assert len(parks) == 1
    assert "shim" in parks[0]["memo"].lower()
    assert "install-launch-shim.sh" in parks[0]["memo"]


def test_the_storm_park_memo_names_the_dead_anchor_and_never_the_shim():
    """THE case this issue exists for (2026-07-09). Ten issues parked asking "is the launch shim
    installed?" while cmux had told the launcher, in words, that the anchor's workspace was gone."""
    captured = ("[i5] could not parse a surface UUID from new-surface output: "
                "Error: not_found: Pane or workspace not found")
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist(
        "ready", launch_failures=2, launch_evidence=_launch_ev(1, captured))}})
    parks = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")
    assert len(parks) == 1
    memo = parks[0]["memo"]
    assert "workspace" in memo.lower()             # the REAL cause, named
    assert "not_found" in memo                     # the captured diagnostic itself, verbatim
    assert "is the shim installed" not in memo.lower()      # the wrong-component directive is gone
    assert "install-launch-shim" not in memo


def test_a_cap_with_no_evidence_admits_it_rather_than_guessing_a_component():
    # Fail-closed: a runner that captured nothing must SAY so. The 07-09 memo's sin was not that it
    # lacked evidence — it was that it sounded certain anyway and named an innocent component.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=2)}})
    parks = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")
    assert len(parks) == 1
    memo = parks[0]["memo"]
    assert evidence.CAPTURED_NONE in memo
    assert "install-launch-shim" not in memo        # no guessing at a component


@pytest.mark.parametrize("bad", [3, "boom", [], {"kind": "launch"}, {"detail": None}])
def test_a_corrupt_evidence_record_never_breaks_the_park(bad):
    # Wrong-typed state must cost wording, never the hand-back (the project's fail-open-on-type rule).
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", launch_failures=2, launch_evidence=bad)}})
    parks = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")
    assert len(parks) == 1 and parks[0]["memo"].strip()


def test_the_park_memo_is_bounded_against_a_runaway_stderr():
    # Cap sizes (the 2026-07-07 binary-in-reports incident): a memo is a GitHub comment, never a dump.
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist(
        "ready", launch_failures=2, launch_evidence=_launch_ev(1, "x" * 100_000))}})
    parks = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")
    assert len(parks[0]["memo"]) < 4000


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


# --------------------- D11: re-approval resumes at the gate, does not rebuild (issue #161) ---------------------
# The owner's loudest frustration: re-approving a FINISHED lane that already has an open PR wiped the
# worktree + report and rebuilt from scratch (the D11 defect). The split: a finished build resumes at
# the merge gate on its existing PR (building nothing new) by DEFAULT; the destructive rebuild is a
# separately-named action reached only via the explicit `rebuild` label.

def _finished(status="parked", **over):
    # a finished BUILD lane: report on disk, a recorded PR, a branch stamp — the shape a lane that
    # opened its PR and filed its report carries when it then parks for an owner decision.
    return disk(reports={"i5": GOOD_REPORT},
                issues_state={"version": 1, "issues": {
                    "i5": ist(status, pr=555, branch="sl/i5-issue-5", **over)}})


def test_reapproving_a_finished_build_resumes_at_the_gate_never_rebuilds():
    # THE D11 fix: a finished build (report on disk ⇒ a PR was opened) that parked and is re-approved
    # RE-ENTERS THE GATE on the existing PR — it emits `resume_at_gate`, never the destructive
    # `reapprove`, and never a fresh launch that would rebuild over the finished work.
    out = decide(parsed_issues=[parsed(5)], dsk=_finished("parked", merge_refusals=3))
    ra = only(out, "resume_at_gate")
    assert len(ra) == 1 and ra[0]["id"] == "i5" and ra[0]["num"] == 5
    assert only(out, "reapprove") == []                  # the work is NOT thrown away
    assert only(out, "launch") == []                     # and nothing is rebuilt from scratch
    assert only(out, "park") == []


def test_a_finished_needs_owner_build_also_resumes_at_the_gate():
    # needs_william is a park-family status too — a finished build handed back for an owner decision
    # (e.g. a merge GitHub refused) resumes at the gate on re-approval, keeping its PR.
    out = decide(parsed_issues=[parsed(5)], dsk=_finished("needs_william"))
    assert len(only(out, "resume_at_gate")) == 1 and only(out, "reapprove") == []


def test_the_explicit_rebuild_label_forces_a_finished_lane_to_rebuild():
    # The separately-named destructive action: the `rebuild` label is the owner's explicit choice to
    # DISCARD the finished PR/report and build anew. It routes a finished lane to `reapprove`, and
    # carries `had_rebuild` so the executor clears the one-shot label from its own isolated call.
    out = decide(parsed_issues=[parsed(5, labels=("agent-ready", "rebuild", "type:build"))],
                 dsk=_finished("parked"))
    ra = only(out, "reapprove")
    assert len(ra) == 1 and ra[0]["had_rebuild"] is True
    assert only(out, "resume_at_gate") == []             # the label overrides the resume default


def test_a_non_rebuild_reapprove_marks_had_rebuild_false():
    # An unfinished parked lane rebuilds with NO rebuild label present — the action says so, so the
    # executor never names a repo-absent `rebuild` in a remove (which would hard-fail the batch).
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("parked", launch_failures=2)}})
    ra = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "reapprove")
    assert len(ra) == 1 and ra[0]["had_rebuild"] is False


def test_a_parked_lane_with_no_finished_work_still_rebuilds():
    # Nothing to resume: a lane parked BEFORE it ever finished (no report) has no PR to re-enter the
    # gate on, so re-approval rebuilds from scratch exactly as before — resume is finished-only.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("parked", launches=3, launch_failures=2)}})   # no report
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "reapprove")) == 1 and only(out, "resume_at_gate") == []


def test_a_finished_investigation_rebuilds_never_resumes_at_a_merge_gate():
    # An investigation opens no PR — its completion is a marker comment, not a merge — so there is no
    # merge gate to resume at. Re-approving a finished investigation rebuilds it, never resumes.
    dsk = disk(reports={"i5": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i5": ist("parked", type="investigate")}})
    out = decide(parsed_issues=[parsed(5, labels=("agent-ready", "type:investigate"))], dsk=dsk)
    assert only(out, "resume_at_gate") == []
    assert len(only(out, "reapprove")) == 1


def test_a_resumed_lane_holds_the_launch_back_this_tick():
    # Like reapprove, resume must not also fire a same-tick launch off the pre-resume view: the lane
    # is being re-claimed for the gate, not relaunched. (Next tick the executor's in-progress label +
    # gating status keep it out of the launch phase entirely.)
    out = decide(parsed_issues=[parsed(5)], dsk=_finished("parked"))
    assert len(only(out, "resume_at_gate")) == 1
    assert only(out, "launch") == []


def test_regenerated_issue_relaunches_on_its_stamped_branch():
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", branch="sl/i5-issue-5-r1", conflicts=1, requeue_front=True)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    a = only(out, "launch")[0]
    assert a["branch"] == "sl/i5-issue-5-r1"


# ============ the teardown deferral is BOUNDED (issue #169) ============
# #149 made a rebuild refuse to prune a worktree while worker.<id>.lock names a live pid (the D14
# rule). A live pid does not prove the worker is OURS: a SIGKILLed start-session.sh never runs its
# EXIT trap, and pids recycle, so an unrelated process can inherit the number and hold the prune off
# forever. The rebuild then aborts every tick — and decide re-emits it every tick, with
# `reapproved_now` blocking the one launch whose _close_stale_session would drop the stale lock. The
# lane livelocks: parked forever, no cap, no notification, no ALERT. This ladder is the bound.

def _deferred(status="parked", deferrals=actions.TEARDOWN_DEFERRAL_CAP, live=True,
              reports=None, **over):
    ist_over = {"teardown_deferral_pid": 4242,
                "teardown_deferral_lock": "/run/state/worker.i5.lock"}
    ist_over.update(over)
    return disk(issues_state={"version": 1, "issues": {
                    "i5": ist(status, teardown_deferrals=deferrals, **ist_over)}},
                reports=reports or {},
                live_lock_ids={"i5"} if live else set())


def test_a_rebuild_that_cannot_clear_its_worktree_parks_at_the_cap_instead_of_retrying_forever():
    out = decide(parsed_issues=[parsed(5)], dsk=_deferred())
    assert only(out, "reapprove") == [], "the livelocked rebuild kept being re-emitted"
    p = only(out, "park")
    assert len(p) == 1 and p[0]["id"] == "i5" and p[0]["needs_william"] is True
    assert p[0]["cause"] == "teardown_deferral"
    assert has_notify(out), "the silent livelock must reach the owner"


def test_the_deferral_park_memo_names_the_pid_and_the_lock_path():
    # The memo is the whole point of counting it: the ONE thing that frees the lane is removing that
    # lock, and the owner cannot check the pid they are never told.
    memo = only(decide(parsed_issues=[parsed(5)], dsk=_deferred()), "park")[0]["memo"]
    assert "4242" in memo and "/run/state/worker.i5.lock" in memo


def test_under_the_cap_the_rebuild_still_retries():
    # The deferral is CORRECT under the cap — a worker really can still be unwinding. Only the
    # unbounded repetition is the bug.
    out = decide(parsed_issues=[parsed(5)],
                 dsk=_deferred(deferrals=actions.TEARDOWN_DEFERRAL_CAP - 1))
    assert len(only(out, "reapprove")) == 1 and only(out, "park") == []


def test_a_capped_lane_whose_lock_is_gone_rebuilds_instead_of_re_parking():
    # THE escape hatch. The park's memo tells the owner to remove the stale lock and re-approve;
    # if the at-cap counter alone re-parked, that instruction would be a lie and the lane would be
    # stuck FOREVER, one park louder. The counter only bites while the cause — a lock naming a live
    # pid — is still there; the successful rebuild then zeroes it (_exec_reapprove).
    out = decide(parsed_issues=[parsed(5)], dsk=_deferred(live=False))
    assert len(only(out, "reapprove")) == 1 and only(out, "park") == []


def test_a_corrupt_deferral_counter_fails_closed_to_the_park():
    # The fail-OPEN-on-wrong-TYPE defect class (_counter's own docstring): an unreadable counter
    # must land on the SAFE action (park), never be read as 0 and re-allow the capped retry forever.
    out = decide(parsed_issues=[parsed(5)], dsk=_deferred(deferrals="lots"))
    assert only(out, "reapprove") == [] and len(only(out, "park")) == 1


def test_a_corrupt_deferral_counter_with_no_live_lock_still_rebuilds():
    # The other half of that rule, and the reason the corrupt check is ANDed with live_locks: fail
    # closed must not mean strand-forever. With the cause gone there is nothing to protect the lane
    # from, and the rebuild it falls through to rewrites the unreadable counter honestly.
    out = decide(parsed_issues=[parsed(5)], dsk=_deferred(deferrals="lots", live=False))
    assert len(only(out, "reapprove")) == 1 and only(out, "park") == []


def test_re_approving_a_still_locked_lane_answers_the_owner_instead_of_silently_re_parking():
    # A re-approval this ladder REFUSES (the counter is already at cap, so nothing is retried) must
    # not just strip `agent-ready` and go quiet — an owner action silently undone is the same
    # silence #169 exists to end. The prior park LANDED (status needs_william, its cause stamped),
    # so this agent-ready is the owner's: distinct cause -> park() texts and memos again.
    dsk = _deferred(status="needs_william", park_notify_cause=actions.TEARDOWN_CAUSE)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["cause"] == actions.TEARDOWN_CAUSE_REAPPROVED
    assert "retry" not in p[0], "a new cause is a new episode, never a suppressed retry"
    assert has_notify(out)
    assert "NOTHING WAS RETRIED" in p[0]["memo"] and "4242" in p[0]["memo"]


def test_a_park_whose_label_move_failed_is_still_a_silent_retry_not_an_owner_re_approval():
    # The counter-case for the above, and why the cause reads BOTH the stamp and the status: when a
    # park's label move fails (the #61 dead zone), status never settles and the OLD agent-ready is
    # still standing. That is the first park re-deriving, not an owner act — it must stay the
    # deduped silent retry, or the dead zone re-opens the 41-text storm #61 closed.
    dsk = _deferred(status="parked", park_notify_cause=actions.TEARDOWN_CAUSE)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["cause"] == actions.TEARDOWN_CAUSE and p[0].get("retry") is True
    assert not has_notify(out)


def test_a_memo_with_no_recorded_pid_still_gives_a_runnable_check():
    # State that predates the stamp (or a lock removed between the refusal and the charge) must not
    # produce `ps -p (pid not recorded)`. Point at the lock to read the pid out of instead.
    dsk = _deferred(teardown_deferral_pid=None)
    memo = only(decide(parsed_issues=[parsed(5)], dsk=dsk), "park")[0]["memo"]
    assert "not recorded" not in memo and "cat /run/state/worker.i5.lock" in memo


def test_a_resume_at_the_gate_is_never_capped_by_the_deferral_ladder():
    # resume_at_gate (#161) tears NOTHING down — it re-enters the gate on the preserved worktree —
    # so the deferral ladder must not touch it. Capping it would strand a finished build whose only
    # sin is a stale lock.
    dsk = _deferred(reports={"i5": GOOD_REPORT})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "resume_at_gate")) == 1 and only(out, "park") == []


def test_a_conflict_regenerate_that_cannot_clear_its_worktree_parks_at_the_cap():
    # The regenerate face of the same livelock: _exec_regenerate aborts on a declined prune too, and
    # the gate re-emits it every tick with nothing counting.
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"), live_lock_ids={"i5"})
    d["issues_state"]["issues"]["i5"].update(
        update_result="conflict", update_head_oid=HEAD1, conflicts=0,
        teardown_deferrals=actions.TEARDOWN_DEFERRAL_CAP,
        teardown_deferral_pid=4242, teardown_deferral_lock="/run/state/worker.i5.lock")
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    assert only(out, "regenerate") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and "4242" in p[0]["memo"]
    assert has_notify(out)


def test_a_conflict_regenerate_under_the_cap_is_untouched():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"), live_lock_ids={"i5"})
    d["issues_state"]["issues"]["i5"].update(
        update_result="conflict", update_head_oid=HEAD1, conflicts=0,
        teardown_deferrals=actions.TEARDOWN_DEFERRAL_CAP - 1)
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    assert len(only(out, "regenerate")) == 1 and only(out, "park") == []


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
    dsk = disk(launch_anchor=_anchor_down(), launch_fail_ids=[],
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


def test_one_channel_failure_is_systemic_and_holds_the_queue():
    # Issue #153: the streak is CHANNEL-only (the runner records a launch failure here only when
    # evidence classifies it as a dead anchor / shim / launch machinery, never a per-issue fault),
    # so a SINGLE entry already means the channel is down. The FIRST such failure is systemic: every
    # launch held, one alert, nothing parked — no issue absorbs the blame, not even the first. (This
    # is the invariant the old distinct-issue-count heuristic could not express: it charged the first
    # one or two issues before inferring "channel" from the count.)
    dsk = disk(launch_fail_ids=["i5"])
    out = decide(parsed_issues=[parsed(5, touches=("frontend",)), parsed(6, touches=("api",))],
                 dsk=dsk)
    assert only(out, "launch") == [] and only(out, "park") == []
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["launch_systemic_failure"] and has_notify(out)


def test_single_issue_launch_cap_still_parks_when_anchor_healthy():
    # DoD #4 / boundary: a genuinely issue-SPECIFIC launch fault still parks that one issue. Its
    # counter is at the per-issue cap and the CHANNEL streak is empty (a per-issue fault never enters
    # it — issue #153), so nothing is systemic and the normal per-issue park fires.
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=[],
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


# =========================== account auth probe before a spend (#159) ===========================
# The i336 class (forensics U3): a session's auth died in-process and the runner burned 94 minutes
# nudging a dead-auth pane. The pane 'logged_out' state (#151) catches that AFTER a session is up;
# THIS gate catches it BEFORE — a fresh launch or a recovery relaunch into DEAD ACCOUNT AUTH would
# start logged out and waste the spend. The runner hands decide a `claude auth status` + keychain
# snapshot in disk["auth_probe"] only when a spend is pending; decide HOLDS the spend and ALERTS
# once on a DEFINITIVE dead reading, and FAILS OPEN on anything unreadable (never freeze the loop
# on a probe we merely could not run — the #46/#76 dark-meter asymmetry applied to auth).

def _auth_dead():
    return {"valid": False, "cli": "logged_out", "keychain_present": True,
            "keychain_mtime": NOW - 3600}


def _auth_unknown():
    return {"valid": None, "cli": "unknown", "keychain_present": True, "keychain_mtime": None}


def _auth_ok():
    return {"valid": True, "cli": "logged_in", "keychain_present": True, "keychain_mtime": NOW - 60}


def test_dead_auth_holds_fresh_launch_and_alerts_once():
    dsk = disk(auth_probe=_auth_dead())
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert only(out, "launch") == []                       # every fresh launch held
    assert only(out, "park") == []                         # nothing parked: the queue is intact
    assert only(out, "relabel") == []                      # agent-ready never stripped
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["auth_dead"]
    assert len(only(out, "notify")) == 1                   # exactly one notify (the alert)


def test_dead_auth_alert_dedupes_across_ticks():
    dsk = disk(auth_probe=_auth_dead(), alert={"reasons": ["auth_dead"]})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "alert") == [] and not has_notify(out)   # already alerted: no repeat
    assert only(out, "launch") == []


def test_unknown_auth_probe_fails_open_and_launches():
    dsk = disk(auth_probe=_auth_unknown())
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "launch")) == 1                   # dark probe -> fail open, launch normally
    assert only(out, "alert") == []


def test_valid_auth_probe_launches_normally():
    dsk = disk(auth_probe=_auth_ok())
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "launch")) == 1 and only(out, "alert") == []


def test_absent_auth_probe_launches_normally():
    # No auth_probe in the view (the runner saw no launch demand, or agent!=claude): fail open.
    out = decide(parsed_issues=[parsed(5)])
    assert len(only(out, "launch")) == 1 and only(out, "alert") == []


def test_wrong_typed_auth_probe_never_raises_or_falsely_degrades():
    for bad in (None, "dead", 0, [], {"valid": "no"}, {"valid": 0}, {"valid": None}, {}):
        dsk = disk(auth_probe=bad)
        out = decide(parsed_issues=[parsed(5)], dsk=dsk)
        assert len(only(out, "launch")) == 1, bad          # only a literal False blocks
        assert only(out, "alert") == [], bad


def test_dead_auth_with_nothing_to_spend_does_not_alert():
    # Idle: dead auth with NO approved queue, NO in-flight lane, and NO exited marker is not yet
    # harmful — no alert, no noise (mirrors the idle dead-anchor rule). The issue here is neither
    # agent-ready (no fresh launch) nor in-progress (no relaunch), so there is nothing to spend.
    dsk = disk(auth_probe=_auth_dead())
    out = decide(parsed_issues=[parsed(5, labels=("type:build",))], dsk=dsk)
    assert only(out, "alert") == [] and not has_notify(out)


def test_dead_auth_alerts_when_only_an_in_flight_lane_is_present():
    # A recovery relaunch is spend demand too (fresh-review P1 sub-note): a dead-auth reading with an
    # in-flight lane and no fresh queue must SURFACE, never hold silently.
    dsk = disk(auth_probe=_auth_dead())
    out = decide(parsed_issues=[parsed(8, labels=("in-progress", "type:build"))],
                 dsk=dsk, gh_view=ghv(prs={"i8": {}}))
    assert only(out, "alert")[0]["reasons"] == ["auth_dead"] and has_notify(out)


def test_dead_auth_holds_exited_relaunch_and_never_parks():
    p5 = parsed(5, labels=("in-progress", "type:build"))
    # Even at the retry cap, dead auth HOLDS rather than parks: auth is the owner's to fix and the
    # lane resumes the tick it reads healthy again (a park would need a re-approval).
    dsk = disk(auth_probe=_auth_dead(), exited={"i5": "x rc=1"},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(parsed_issues=[p5], dsk=dsk)
    assert only(out, "recover") == []                      # no relaunch into dead auth
    assert only(out, "park") == []                         # held, not parked
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i5" and "auth" in h[0]["reason"].lower()
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["auth_dead"]


def test_valid_auth_still_relaunches_an_exited_lane():
    p5 = parsed(5, labels=("in-progress", "type:build"))
    dsk = disk(auth_probe=_auth_ok(), exited={"i5": "x rc=1"},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=1)}})
    out = decide(parsed_issues=[p5], dsk=dsk)
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited" and only(out, "alert") == []


def test_dead_auth_holds_orphan_resume():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")})
    dsk = disk(auth_probe=_auth_dead())
    out = decide(parsed_issues=[p8], dsk=dsk, gh_view=g)
    assert only(out, "launch") == []                       # the orphan resume is held, not spent
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i8" and "auth" in h[0]["reason"].lower()
    assert only(out, "alert")[0]["reasons"] == ["auth_dead"]   # and it is NOT a silent hold


def test_dead_auth_does_not_block_gating_or_merging():
    # Auth gates SPENDS (launch/relaunch), never merge mechanics: a finished, clean PR still merges
    # while auth is dead — merging spends no session (mirrors the dead-anchor rule).
    d, g = _gating()                                       # i5 finished, clean PR
    d["auth_probe"] = _auth_dead()
    out = decide(parsed_issues=[parsed(9)], dsk=d, gh_view=g)   # i9 queued behind dead auth
    assert len(only(out, "merge")) == 1                    # the gate still merges the finished PR
    assert only(out, "launch") == []                       # but the queued launch is held
    assert only(out, "alert")[0]["reasons"] == ["auth_dead"]


def test_dead_auth_and_dead_anchor_both_named_sorted():
    dsk = disk(auth_probe=_auth_dead(), launch_anchor=_anchor_down())
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["auth_dead", "launch_anchor_down"]   # sorted
    assert only(out, "launch") == [] and only(out, "park") == []


# =========================== canary re-arm of the systemic hold (#115) ===========================
# The 2026-07-13 incident: three launch deliveries failed back-to-back, #24's breaker tripped
# correctly (one park raced the streak, one alert, launches HELD) — but the hold never released on
# its own. The systemic streak clears ONLY on a verified delivery, and with launches held no
# delivery is ever attempted, so the loop sat healthy-but-frozen until a manual restart. Re-arm the
# breaker: while the systemic hold stands, probe once per CANARY_RETRY_SECONDS with a SINGLE canary
# launch of the front-of-queue issue. A verified delivery clears the streak; a failed canary
# re-enters the hold, charging no per-issue cap and parking nothing.

def _held(**over):
    """A systemic-hold disk view (streak >= cap, probe ok, alert already on disk), plus a canary
    clock stamped `age` seconds in the past (default: exactly one retry interval)."""
    age = over.pop("age", actions.CANARY_RETRY_SECONDS)
    d = dict(launch_anchor=_anchor_ok(), launch_fail_ids=["i5", "i6"],
             launch_fail_at=NOW - age,
             alert={"reasons": ["launch_systemic_failure"]})
    d.update(over)
    return disk(**d)


def test_systemic_hold_fires_one_canary_of_the_front_of_queue_after_the_interval():
    # After a full retry interval since the last delivery failure, decide probes with ONE canary
    # launch — of the highest-priority queued issue, never the whole queue.
    out = decide(now=NOW, dsk=_held(),
                 parsed_issues=[parsed(5),
                                parsed(6, labels=("agent-ready", "type:build", "priority:high")),
                                parsed(7)])
    launches = only(out, "launch")
    assert len(launches) == 1                              # ONE probe, not a queue walk
    assert launches[0]["id"] == "i6"                       # front-of-queue in PRIORITY order
    assert launches[0].get("canary") is True
    assert only(out, "park") == []                         # a probe never parks


def test_systemic_hold_holds_and_does_not_canary_before_the_interval():
    # Inside the interval the hold still governs: no launch, and the already-on-disk alert never
    # repeats (no new notify per tick).
    out = decide(now=NOW, dsk=_held(age=actions.CANARY_RETRY_SECONDS - 1),
                 parsed_issues=[parsed(5), parsed(6)])
    assert only(out, "launch") == []                       # interval not elapsed: still held
    assert only(out, "park") == []
    assert only(out, "alert") == [] and not has_notify(out)   # already alerted: no repeat


def test_canary_probe_emits_no_new_alert_or_notify():
    # The canary rides UNDER the original systemic alert: it adds no second alert and no second text.
    out = decide(now=NOW, dsk=_held(), parsed_issues=[parsed(5), parsed(6), parsed(7)])
    assert len(only(out, "launch")) == 1
    assert only(out, "alert") == [] and not has_notify(out)


def test_no_canary_while_the_anchor_probe_itself_is_down():
    # anchor_down self-re-arms via its per-tick probe; a canary into a probe-dead pane is wasted, so
    # the streak-canary defers to the probe while the pane is reported dead.
    out = decide(now=NOW,
                 dsk=_held(launch_anchor=_anchor_down()),
                 parsed_issues=[parsed(5), parsed(6)])
    assert only(out, "launch") == []
    a = only(out, "alert")                                 # both detectors are named in the alert
    assert len(a) == 1
    assert set(a[0]["reasons"]) == {"launch_anchor_down", "launch_systemic_failure"}


def test_systemic_recovery_journals_a_record_and_clears_the_alert_and_resumes():
    # The canary's verified delivery cleared the streak (runner); THIS tick sees systemic fall while
    # the durable alert still names it — the exit edge: ONE recovery record, the alert cleared, and
    # the held queue resumes launching (agent-ready was never stripped, so no William re-touch).
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=[],
               alert={"reasons": ["launch_systemic_failure"]})
    out = decide(now=NOW, dsk=dsk,
                 parsed_issues=[parsed(5, touches=("frontend",)), parsed(6, touches=("api",))])
    rec = only(out, "launch_recovered")
    assert len(rec) == 1 and isinstance(rec[0].get("reason"), str) and rec[0]["reason"]
    assert len(only(out, "clear_alert")) == 1
    assert len(only(out, "launch")) == 2                   # queue resumes


def test_systemic_recovery_record_is_not_re_emitted_once_the_alert_is_cleared():
    # Deduped on the durable alert marker: once the alert is gone, no further recovery record.
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=[], alert=None)
    out = decide(now=NOW, dsk=dsk, parsed_issues=[parsed(5)])
    assert only(out, "launch_recovered") == []


def test_reapprove_during_the_systemic_hold_does_not_launch_into_the_dead_anchor():
    # DoD #3 / tonight's 20:21 shape: re-approving a parked issue while the hold stands resets its
    # counters (reapprove) but must NOT launch into the known-bad anchor — the hold governs.
    dsk = _held(age=1,                                     # interval NOT elapsed: no canary this tick
                issues_state={"version": 1, "issues": {
                    "i5": ist("ready", launch_failures=1),
                    "i6": ist("ready", launch_failures=1),
                    "i7": ist("parked", launch_failures=2)}})
    out = decide(now=NOW, dsk=dsk,
                 parsed_issues=[parsed(5), parsed(6), parsed(7)])
    ra = only(out, "reapprove")
    assert len(ra) == 1 and ra[0]["id"] == "i7"           # re-approval is honored (counters reset)
    assert only(out, "launch") == []                      # but nothing launches into the dead anchor
    assert only(out, "park") == []


def test_after_the_hold_clears_the_previously_held_queue_launches():
    # DoD #3 second half: once the streak clears (canary verified / restart), the queue frozen at
    # 20:21 launches with no William re-touch.
    dsk = disk(launch_anchor=_anchor_ok(), launch_fail_ids=[],
               issues_state={"version": 1, "issues": {
                   "i7": ist("ready", branch="sl/i7-x", launch_failures=0)}})
    out = decide(now=NOW, dsk=dsk, parsed_issues=[parsed(7)])
    launches = only(out, "launch")
    assert len(launches) == 1 and launches[0]["id"] == "i7"
    assert launches[0].get("canary") is not True          # a normal launch, not a probe


def test_wrong_typed_canary_clock_never_raises_or_probes_immediately():
    # A missing/garbage launch_fail_at must not make the canary fire the instant the hold engages:
    # the interval gate fails CLOSED (no real clock -> no probe this tick), never raising.
    for bad in (None, "soon", [], {"at": 1}, float("nan"), float("inf")):
        out = decide(now=NOW, dsk=_held(launch_fail_at=bad),
                     parsed_issues=[parsed(5), parsed(6)])
        assert only(out, "launch") == []                  # no probe on a garbage clock
        assert only(out, "park") == []


# =========================== display-sleep launch hold (#124) ===========================
# The live 2026-07-13 launch killer: macOS will not boot a fresh cmux tab's shell while the DISPLAY
# sleeps, so a launch attempted then is created and closed as an orphan — a burned attempt that feeds
# #24's systemic streak and churns an alert every sleeping episode (#115's canary makes THAT self-
# recover, but a cleaner fix is to not ATTEMPT delivery into a sleeping display at all). The runner
# hands decide a per-tick display-power read in disk["display_asleep"] (True=asleep, False=awake,
# None/absent=unreadable); decide HOLDS every fresh launch (and the #115 canary) QUIETLY while it
# reads confirmed asleep — no streak, no per-issue cap, no alert, since a sleeping display is normal,
# not a fault — and resumes automatically on wake. FAIL OPEN on anything but an explicit True.

def test_display_asleep_holds_every_fresh_launch_quietly():
    dsk = disk(display_asleep=True)
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert only(out, "launch") == []                       # every launch held: no tab attempted
    assert only(out, "park") == []                         # queue intact — nothing parked
    assert only(out, "relabel") == []                      # agent-ready never stripped
    assert only(out, "alert") == [] and not has_notify(out)   # a sleeping display is NOT a fault


def test_display_awake_launches_normally():
    dsk = disk(display_asleep=False)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "launch")) == 1 and only(out, "alert") == []


def test_display_probe_unreadable_fails_open_and_launches():
    # None (the runner could not read the display state — a non-macOS host, a pmset error/timeout)
    # must launch normally, EXACTLY today's behavior — never wedge the queue on a probe we could not
    # run (the #46/#76 dark-meter asymmetry, applied to display sleep).
    dsk = disk(display_asleep=None)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "launch")) == 1 and only(out, "alert") == []


def test_display_probe_absent_launches_normally():
    # No display_asleep key at all (the runner saw no launch demand and never probed): fail open.
    out = decide(parsed_issues=[parsed(5)])
    assert len(only(out, "launch")) == 1 and only(out, "alert") == []


def test_display_asleep_does_not_feed_the_streak_or_cap_or_park():
    # The whole point: while asleep NOTHING is attempted, so no issue's launch_failures grows and no
    # streak entry is made. An at-cap issue is held (its park defers to wake, like every other hold),
    # so a sleeping night never walks the queue into parks or alerts.
    dsk = disk(display_asleep=True, launch_fail_ids=[],
               issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures=2)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == [] and only(out, "park") == []
    assert only(out, "alert") == [] and not has_notify(out)


def test_display_asleep_wrong_typed_never_raises_or_falsely_degrades():
    # Fail-open-on-wrong-TYPED (the project's named defect class): ONLY an explicit True holds; any
    # other value launches normally and never raises.
    for bad in ("yes", 1, [], {"asleep": True}, "true", 0):
        dsk = disk(display_asleep=bad)
        out = decide(parsed_issues=[parsed(5)], dsk=dsk)
        assert len(only(out, "launch")) == 1, bad
        assert only(out, "alert") == [], bad


def test_display_asleep_does_not_block_gating_or_merging():
    # A sleeping display holds SPENDS (fresh launches), never merge mechanics: a finished, clean PR
    # still merges while asleep — merging spends no session (mirrors the dead-anchor / dead-auth rule).
    d, g = _gating()                                       # i5 finished, clean PR
    d["display_asleep"] = True
    out = decide(parsed_issues=[parsed(9)], dsk=d, gh_view=g)   # i9 queued behind the sleeping display
    assert len(only(out, "merge")) == 1                    # the gate still merges the finished PR
    assert only(out, "launch") == []                       # but the queued launch is held


def test_no_canary_while_the_display_is_asleep():
    # A systemic hold stands AND the display sleeps: the #115 canary must NOT fire into a sleeping
    # display (it would just create+close an orphan). The systemic streak is intact so the durable
    # alert still stands, but no probe is attempted until the display wakes.
    out = decide(now=NOW, dsk=_held(display_asleep=True),
                 parsed_issues=[parsed(5), parsed(6)])
    assert only(out, "launch") == []                       # no canary into the sleeping display
    assert only(out, "park") == []


def test_canary_fires_once_the_display_wakes():
    # The mirror: with the display awake (False) the held systemic streak still gets its one canary
    # probe after the interval — display sleep gates the canary, it does not disable it.
    out = decide(now=NOW, dsk=_held(display_asleep=False),
                 parsed_issues=[parsed(5), parsed(6)])
    launches = only(out, "launch")
    assert len(launches) == 1 and launches[0].get("canary") is True


# ---- recovery relaunches also boot a fresh tab, so a sleeping display holds them too (#124) ----
# A fresh-queue launch is not the only path that boots a tab macOS will not schedule while asleep:
# an exited-lane relaunch, an orphan resume, and a conflict resolver all do. The auth sibling (#159)
# holds all three; #124 must too, or the systemic streak still churns overnight via the relaunch
# path. Each holds QUIETLY (unlike dead auth, a sleeping display raises no alert) and resumes on wake.

def test_display_asleep_holds_the_exited_relaunch_and_never_parks():
    p5 = parsed(5, labels=("in-progress", "type:build"))
    # Even at the retry cap, a sleeping display HOLDS the recovery relaunch rather than parking or
    # burning a fresh tab: it resumes the tick the display wakes.
    dsk = disk(display_asleep=True, exited={"i5": "x rc=1"},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(parsed_issues=[p5], dsk=dsk)
    assert only(out, "recover") == []                      # no relaunch into the sleeping display
    assert only(out, "park") == []                         # held, not parked (even at cap)
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i5" and "display" in h[0]["reason"].lower()
    assert only(out, "alert") == [] and not has_notify(out)   # quiet: a sleeping display is no fault


def test_display_asleep_holds_the_orphan_resume():
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")})
    dsk = disk(display_asleep=True)
    out = decide(parsed_issues=[p8], dsk=dsk, gh_view=g)
    assert only(out, "launch") == []                       # the orphan resume is held, not spent
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i8" and "display" in h[0]["reason"].lower()
    assert only(out, "alert") == [] and not has_notify(out)   # quiet: no alert


def test_display_asleep_holds_the_conflict_resolver_quietly():
    # A conflict resolver is a session START: a fresh tab macOS will not boot while asleep, so it
    # holds (never a resolve_conflict, never a park) — and QUIETLY, unlike dead auth (#159).
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1)
    d["display_asleep"] = True
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    assert only(out, "resolve_conflict") == []             # not spent into the sleeping display
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i5" and "display" in h[0]["reason"].lower()
    assert only(out, "alert") == [] and not has_notify(out)   # quiet: no alert


def test_display_awake_still_relaunches_an_exited_lane():
    # The mirror: an awake display (or an absent read) never blocks a recovery relaunch — the exited
    # lane relaunches exactly as today.
    p5 = parsed(5, labels=("in-progress", "type:build"))
    dsk = disk(display_asleep=False, exited={"i5": "x rc=1"},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=1)}})
    out = decide(parsed_issues=[p5], dsk=dsk)
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited" and only(out, "alert") == []


def test_running_issue_still_labeled_agent_ready_gets_relabel_reconciliation():
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(parsed_issues=[parsed(5, labels=("agent-ready", "in-progress", "type:build"))],
                 dsk=dsk)
    assert only(out, "launch") == []


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


# =========================== touches_required (issue #36) ===========================

def test_touches_required_true_parks_an_approved_issue_missing_touches():
    # The knob now ACTS: an approved build issue that declares no `touches:` is refused at launch
    # and handed to William with a memo naming the missing block — never silently launched.
    out = decide(config=cfg(touches_required=True),
                 parsed_issues=[parsed(5, touches=())])
    assert only(out, "launch") == []
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5" and parks[0]["needs_william"] is True
    memo = parks[0]["memo"].lower()
    assert "touches" in memo and "touches_required" in memo
    assert has_notify(out)


def test_touches_required_true_launches_when_touches_declared():
    # The gate only bites the MISSING declaration; a well-declared issue launches unchanged.
    out = decide(config=cfg(touches_required=True),
                 parsed_issues=[parsed(5, touches=("frontend",))])
    assert len(only(out, "launch")) == 1 and only(out, "park") == []


def test_touches_required_false_relaxes_and_does_not_park_a_no_touches_issue():
    # false documents what is relaxed: a no-touches issue is allowed. It still launches (its
    # wildcard cost shows up only when it must co-schedule — see the serialization test below).
    out = decide(config=cfg(touches_required=False),
                 parsed_issues=[parsed(5, touches=())])
    assert only(out, "park") == []
    assert len(only(out, "launch")) == 1


def test_touches_required_true_never_parks_an_investigation_missing_touches():
    # Investigations produce no PR/merge, so touches are meaningless for them — the gate is about
    # merge-area affinity, not investigations. A no-touches investigate issue is NOT parked.
    out = decide(config=cfg(touches_required=True),
                 parsed_issues=[parsed(5, labels=("agent-ready", "type:investigate"), touches=())])
    assert only(out, "park") == []


def test_touches_required_missing_from_config_defaults_to_enforcing():
    # A config missing the key (or a corrupt non-bool) fails SAFE to enforcement (the loader
    # default is True), so a no-touches issue is refused rather than silently launched.
    c = cfg()
    del c["touches_required"]
    out = decide(config=c, parsed_issues=[parsed(5, touches=())])
    assert only(out, "launch") == [] and len(only(out, "park")) == 1


def test_touches_required_does_not_park_a_blocked_issue_early():
    # P2-1 (fresh review): a no-touches issue still BLOCKED by an open dependency must keep waiting,
    # not be parked early (which would strip agent-ready and force a needless re-approval). The park
    # is the true launch point — it fires only once the issue is otherwise eligible.
    p = parsed(5, touches=(), blocked_by=[3])
    out = decide(config=cfg(touches_required=True), parsed_issues=[p],
                 gh_view=ghv(closed_nums=set()))          # #3 still open
    assert only(out, "park") == [] and only(out, "launch") == []
    # once the dependency closes, the missing-touches refusal fires (the real launch point)
    out2 = decide(config=cfg(touches_required=True), parsed_issues=[p],
                  gh_view=ghv(closed_nums={3}))
    assert len(only(out2, "park")) == 1 and only(out2, "park")[0]["needs_william"] is True


def test_touches_required_does_not_mislabel_a_control_label_conflict_issue():
    # P2-1: a no-touches issue that is ALSO ineligible for a control-label conflict must not be
    # parked with a "missing touches" memo (a misdiagnosis). It is left for its own handling.
    p = parsed(5, touches=(), label_conflict=True)
    out = decide(config=cfg(touches_required=True), parsed_issues=[p])
    assert not [pk for pk in only(out, "park") if "touches_required" in pk.get("memo", "")]
    assert only(out, "launch") == []                      # still not launched (conflict unresolved)


def test_touches_required_true_does_not_park_the_auto_filed_restore_green_issue():
    # Regression (self-review of #36): the nightly-red auto-fix issue is diagnose-and-fix + auto-
    # approved. It MUST declare `touches: *` so touches_required does not park it — parking the very
    # issue meant to unfreeze a frozen mainline would deadlock auto-restore-green. Assert the filed
    # body carries a real touches declaration, and that such an issue launches (never parks).
    filed = actions._fix_issue("main", "ci", "FAILURE", "fp1")
    assert "touches: *" in filed["body"]
    fixp = parsed(5, labels=("agent-ready", "type:diagnose-and-fix", "auto-approved:nightly-red"),
                  touches=("*",))
    out = decide(config=cfg(touches_required=True), parsed_issues=[fixp])
    assert only(out, "park") == [] and len(only(out, "launch")) == 1


# =========================== wildcard launch-suppression journaling (issue #36) ===========

def test_wildcard_hold_journaled_when_a_no_touches_lane_serializes_the_queue():
    # touches_required:false + a running no-touches lane -> a well-declared candidate can't
    # co-schedule (the lane is a '*' that overlaps everything). The journal must SAY why.
    lane = [{"id": "i9", "touches": [], "type": "build"}]
    out = decide(config=cfg(touches_required=False, lanes=3),
                 parsed_issues=[parsed(5, touches=("api",))], lane_state=lane)
    assert only(out, "launch") == []
    wh = only(out, "wildcard_hold")
    assert len(wh) == 1 and wh[0]["id"] == "i5" and wh[0]["blocker"] == "i9"
    assert "one lane" in wh[0]["reason"].lower() or "serial" in wh[0]["reason"].lower()


def test_wildcard_hold_dedupes_once_per_episode():
    # Bounded: once the hold is journaled (loopstate flag set), a later tick in the same episode
    # does NOT re-journal it.
    lane = [{"id": "i9", "touches": [], "type": "build"}]
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("ready", wildcard_hold_journaled=True)}})
    out = decide(config=cfg(touches_required=False, lanes=3),
                 parsed_issues=[parsed(5, touches=("api",))], lane_state=lane, dsk=dsk)
    assert only(out, "wildcard_hold") == []
    assert only(out, "launch") == []


def test_named_overlap_hold_is_not_journaled_as_wildcard():
    # A genuine named-area overlap (both declare 'frontend') serializes by the operator's design —
    # no wildcard mystery, so no wildcard_hold record.
    lane = [{"id": "i9", "touches": ["frontend"], "type": "build"}]
    out = decide(parsed_issues=[parsed(5, touches=("frontend",))], lane_state=lane)
    assert only(out, "launch") == [] and only(out, "wildcard_hold") == []


def test_hold_action_carries_overlap_wildcard_for_the_merge_gate():
    # The merge-side mirror: a finished PR whose diff maps to '*' (no declared area) holds behind an
    # in-flight lane, and the hold action carries overlap_wildcard so the journal records why.
    d, g = _gating(pv=pr_view(files=("totally/undeclared.txt",)),
                   issues_extra={"i9": ist("running", declared_touches=["api"])})
    out = decide(dsk=d, gh_view=g)
    holds = only(out, "hold")
    assert len(holds) == 1 and holds[0].get("overlap_wildcard") is True


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
    assert only(out, "post_question") == []           # a bounce is not a question — never posts one


def test_bounced_already_processed_is_silent():
    dsk = disk(blocked={"i7": "BOUNCED: x"},
               issues_state={"version": 1, "issues": {"i7": ist("bounced")}})
    out = decide(dsk=dsk)
    assert only(out, "bounce") == [] and only(out, "post_question") == []


# =========================== durable-question lifecycle (#163) ===========================

def test_blocked_question_posts_durably_and_notifies():
    # A worker's owner-decision question (a plain blocked marker) becomes a DURABLE GitHub comment
    # and releases the lane — never a live-frozen session waiting on an answerer (#163).
    dsk = disk(blocked={"i7": "QUESTION: approach A or B?\nRECOMMENDATION: A"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    q = only(out, "post_question")
    assert len(q) == 1
    assert q[0] == {"act": "post_question", "id": "i7", "num": 7,
                    "question": "QUESTION: approach A or B?\nRECOMMENDATION: A"}
    assert has_notify(out)                             # the owner is told a question is waiting


def test_already_posted_question_does_not_renotify():
    # question_posted is the once-per-question stamp: the durable comment already went out, so a
    # re-derived tick (e.g. the label move is retrying) must not re-text the owner.
    dsk = disk(blocked={"i7": "q?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", question_posted=True)}})
    out = decide(dsk=dsk)
    assert len(only(out, "post_question")) == 1        # still re-derives the (idempotent) action
    assert not has_notify(out)                          # but never re-texts


def test_two_blocked_issues_each_post_their_question():
    dsk = disk(blocked={"i7": "q7?", "i8": "q8?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked"), "i8": ist("blocked")}})
    out = decide(dsk=dsk)
    ids = sorted(a["id"] for a in only(out, "post_question"))
    assert ids == ["i7", "i8"]


def test_third_question_parks_as_a_scoping_problem():
    # At most TWO questions per issue; a THIRD is a scoping problem only the owner can untangle (#163).
    dsk = disk(blocked={"i7": "a third question"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", questions_asked=2)}})
    out = decide(dsk=dsk)
    assert only(out, "post_question") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["id"] == "i7" and p[0]["needs_william"] is True
    assert "a third question" in p[0]["memo"] and has_notify(out)


def test_corrupt_questions_asked_parks_not_posts():
    # A wrong-typed counter is UNREADABLE — fail closed to a park, never re-allow an unbounded
    # round-trip (the _counter fail-closed contract).
    dsk = disk(blocked={"i7": "q?"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", questions_asked="lots")}})
    out = decide(dsk=dsk)
    assert only(out, "post_question") == []
    assert len(only(out, "park")) == 1


def test_second_question_still_posts():
    # One prior answered question (questions_asked=1) is under the cap — the second still posts.
    dsk = disk(blocked={"i7": "second question"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked", questions_asked=1)}})
    out = decide(dsk=dsk)
    assert len(only(out, "post_question")) == 1
    assert only(out, "park") == []


def test_awaiting_answer_with_agent_ready_relaunches():
    # The owner's answer = the approval verb (agent-ready re-applied) on an awaiting_answer issue.
    # It triggers a FRESH session (answer_relaunch), never a nudge into a frozen pane (#163).
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("awaiting_answer")}})
    out = decide(parsed_issues=[parsed(5, labels=("agent-ready", "type:build"))], dsk=dsk)
    r = only(out, "answer_relaunch")
    assert len(r) == 1 and r[0] == {"act": "answer_relaunch", "id": "i5", "num": 5}


def test_awaiting_answer_without_agent_ready_is_inert():
    # No answer yet: the issue sits idle, exempt from every liveness/recovery/launch path — no
    # relaunch, no nudge, no park (the whole point: a waiting question holds no live window).
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("awaiting_answer")}},
               exited={"i5": "1751000000 rc=0"})        # a stray turn-end exited stamp is ignored
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk)
    assert only(out, "answer_relaunch") == []
    assert only(out, "recover") == []
    assert only(out, "post_question") == []
    assert only(out, "park") == []


def test_awaiting_answer_occupies_no_lane():
    # awaiting_answer is NOT an in-flight status: the worker exited and the lane is free.
    st = {"version": 1, "issues": {"i5": ist("awaiting_answer")}}
    assert actions.lane_state_from(st) == []


def test_blocked_question_posts_even_with_an_exited_marker():
    # #163: the worker EXITS right after writing its question, so start-session.sh stamps an `exited`
    # marker alongside the blocked marker. That is the normal hand-off — the question is posted, NOT
    # mistaken for a crash to recover-relaunch (which would loop forever re-asking).
    dsk = disk(blocked={"i7": "QUESTION: A or B?"}, exited={"i7": "1751000000 rc=0"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(parsed_issues=[parsed(7, labels=("in-progress", "type:build"))], dsk=dsk)
    assert only(out, "recover") == []
    assert len(only(out, "post_question")) == 1


def test_bounce_marker_wins_over_an_exited_marker():
    # A BOUNCED worker also exits — the bounce still fires, never a recover.
    dsk = disk(blocked={"i7": "BOUNCED: premise gone"}, exited={"i7": "1751000000 rc=0"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(parsed_issues=[parsed(7, labels=("in-progress", "type:build"))], dsk=dsk)
    assert only(out, "recover") == []
    assert len(only(out, "bounce")) == 1


def test_blocked_with_report_goes_to_gate_not_question():
    dsk = disk(blocked={"i7": "q?"}, reports={"i7": GOOD_REPORT},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert only(out, "post_question") == []
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


# =============== the frozen un-latch: resumed progress flips status back (issue #231) ===============
# A lane latched to `frozen` keeps drawing the 10-minute recovery nudge until its report lands — even
# after the session demonstrably resumed (took turns, committed, opened its PR). The un-latch keys on
# the #157 PROGRESS clock (HEAD / report / blocked marker), NEVER on activity: a nudge refreshes the
# pane's activity but can never move the signature, so the recovery ladder cannot answer itself (the
# i328 lesson #157 exists to encode). `clock`/`_sig` live in the probe-ladder section below.

def test_frozen_lane_unlatches_when_the_progress_clock_advances():
    # The core fix: a `frozen` lane whose baseline signature is OLD, whose clock now shows a NEW HEAD,
    # is written back to `running` (unfreeze) and the frozen recovery nudge STOPS for it.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(head="OLD"),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="NEW")}))
    uf = only(out, "unlatch_frozen")
    assert len(uf) == 1 and uf[0]["id"] == "i5" and uf[0]["sig"] == _sig(head="NEW")
    assert uf[0]["evidence_class"] == "HEAD"           # the journal names WHAT un-latched it
    assert only(out, "recover") == []                  # ...and the frozen ladder is done for it


def test_frozen_lane_unlatch_names_the_report_marker_as_evidence():
    # A resumed session that lands its report marker (HEAD unchanged) un-latches on the REPORT signal —
    # the journal must name the actual evidence class, not just "advanced".
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(report=False),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(report=True)}))
    uf = only(out, "unlatch_frozen")
    assert len(uf) == 1 and "report" in uf[0]["evidence_class"]


def test_i328_echo_alone_never_unlatches_a_frozen_lane():
    # THE regression guard (DoD): the clock is UNCHANGED from the baseline — a probe/nudge refreshed
    # the pane's activity but moved no HEAD/report/blocked. Activity-without-a-progress-change must NOT
    # un-latch; the lane stays frozen and the ladder keeps nudging on its cadence.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}))   # sig == baseline
    assert only(out, "unlatch_frozen") == []
    assert len(only(out, "recover")) == 1              # still nudged as frozen (genuinely stuck)


def test_frozen_lane_with_no_clock_keeps_nudging_and_never_unlatches():
    # A genuinely-stuck lane with no progress clock at all: no signature to measure, so the existing
    # frozen ladder is untouched (nudge on cadence). Never a false un-latch off a missing clock.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st))            # no status_clocks in the view
    assert only(out, "unlatch_frozen") == []
    assert len(only(out, "recover")) == 1


def test_frozen_lane_without_a_baseline_anchors_first_then_unlatches_on_a_real_change():
    # The awaiting/degraded edge: a lane that froze without a progress baseline (progress_sig None —
    # e.g. it was `awaiting` long-running work before the probe ladder ever anchored it). The FIRST
    # clock sighting is ANCHORED (so a later real advance can be measured), NOT mistaken for progress:
    # no unfreeze on first sight (mirrors #157's first-sight-anchors-without-acting).
    st = {"version": 1, "issues": {"i5": ist("frozen", last_recover_at=NOW - 601)}}  # no progress_sig
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="A")}))
    assert only(out, "unlatch_frozen") == []
    adv = only(out, "progress_advance")
    assert len(adv) == 1 and adv[0]["sig"] == _sig(head="A")
    assert only(out, "recover") == []                  # anchoring supersedes the nudge this one tick
    # Now the baseline is A; a genuine advance to B un-latches.
    st2 = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(head="A"),
                                           last_recover_at=NOW - 601)}}
    out2 = decide(dsk=disk(issues_state=st2, status_clocks={"i5": clock(head="B")}))
    assert len(only(out2, "unlatch_frozen")) == 1


def test_a_freshly_frozen_event_still_recovers_even_with_a_clock():
    # A lane crossing INTO frozen this tick (a `frozen` event, status still 'running') takes the normal
    # recover path — the un-latch is only for an already-LATCHED `frozen` status. Guards the un-latch
    # from swallowing the very first freeze recover (which is what stamps the baseline).
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(head="OLD"))}}
    out = decide(events=[{"type": "frozen", "id": "i5"}],
                 dsk=disk(issues_state=st, status_clocks={"i5": clock(head="NEW")}))
    assert only(out, "unlatch_frozen") == []
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "frozen"


def test_a_head_that_became_git_unreadable_never_unlatches_a_frozen_lane():
    # Fail-closed (fresh-review P1): a nudge wakes the worker, whose re-stamp can flap HEAD to
    # git-UNREADABLE when `git rev-parse` fails (a timeout / wedged worktree) — progress_signature
    # formats that as 'None|...'. That is NOT movement (the i328 self-answer shape); it must NOT
    # un-latch. The last-known-good baseline is preserved and the lane keeps being nudged.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(head="REALHEAD"),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head=None)}))  # head unreadable
    assert only(out, "unlatch_frozen") == []
    assert only(out, "progress_advance") == []         # never re-anchor a good baseline to a None head
    assert len(only(out, "recover")) == 1              # still nudged as frozen


def test_a_corrupt_non_str_baseline_fails_closed_and_re_anchors_instead_of_unlatching():
    # Fail-closed (fresh-review P1): a wrong-typed baseline (legacy/hand-corrupt state) is not None,
    # so a naive `cur_sig != baseline` would read ANY real signature as progress and falsely un-latch.
    # It must instead RE-ANCHOR (repair the baseline) and never un-latch — mirroring this codebase's
    # corrupt-input-fails-closed discipline.
    for bad in (42, ["x"], {"a": 1}, True):
        st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=bad,
                                              last_recover_at=NOW - 601)}}
        out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="H")}))
        assert only(out, "unlatch_frozen") == [], bad
        adv = only(out, "progress_advance")
        assert len(adv) == 1 and adv[0]["sig"] == _sig(head="H"), bad   # baseline repaired


def test_first_sight_with_an_unreadable_head_does_not_poison_the_baseline():
    # A lane with no baseline whose first clock sighting has an unreadable head must NOT anchor a
    # 'None' head (that baseline could never prove a future movement). Leave it unanchored and nudge;
    # anchor only once a readable head appears.
    st = {"version": 1, "issues": {"i5": ist("frozen", last_recover_at=NOW - 601)}}  # no progress_sig
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head=None)}))
    assert only(out, "unlatch_frozen") == [] and only(out, "progress_advance") == []
    assert len(only(out, "recover")) == 1


def test_a_report_marker_unlatches_even_when_the_head_is_unreadable():
    # A report/blocked marker change is a real milestone regardless of head readability: it un-latches
    # even if `git rev-parse` happens to be failing that rest.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(head="H", report=False),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head=None, report=True)}))
    uf = only(out, "unlatch_frozen")
    assert len(uf) == 1 and "report" in uf[0]["evidence_class"]


def test_frozen_lane_with_a_report_goes_to_the_gate_not_the_unlatch():
    # Boundary: once the report lands the ship gate owns the lane (it is checked above the frozen
    # tier). A frozen+report lane whose clock also advanced must go to `gate`, never `unlatch_frozen`
    # — the un-latch fills only the resumed-but-not-yet-reported window.
    st = {"version": 1, "issues": {"i5": ist("frozen", progress_sig=_sig(head="OLD"),
                                          last_recover_at=NOW - 601)}}
    out = decide(dsk=disk(issues_state=st, reports={"i5": GOOD_REPORT},
                          status_clocks={"i5": clock(head="NEW")}))
    assert only(out, "unlatch_frozen") == []
    assert len(only(out, "gate")) == 1


def test_unlatched_lane_that_freezes_again_re_enters_the_ladder_fresh():
    # DoD: the same lane freezing again later re-enters the ladder fresh. After an un-latch the lane is
    # `running` with a fresh baseline (progress_sig=NEW); a later `frozen` event recovers it normally
    # (no lingering un-latch state suppresses the ladder).
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(head="NEW"),
                                          progress_since=NOW - 60)}}
    out = decide(events=[{"type": "frozen", "id": "i5"}],
                 dsk=disk(issues_state=st, status_clocks={"i5": clock(head="NEW")}))
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "frozen"    # re-freeze -> the ladder fires again


def test_exited_marker_relaunches_under_cap_and_parks_at_cap():
    p5 = parsed(5, labels=("in-progress", "type:build"))   # in the view, as an in-flight issue is
    under = disk(exited={"i5": "x rc=1"},
                 issues_state={"version": 1, "issues": {"i5": ist("running", retries=1)}})
    out = decide(parsed_issues=[p5], dsk=under)
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited"
    at_cap = disk(exited={"i5": "x rc=1"},
                  issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(parsed_issues=[p5], dsk=at_cap)
    assert only(out, "recover") == []
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_exited_cap_park_memo_carries_the_launch_stderr_tail():
    # A launch that dies immediately (bad --model, a renamed/dropped CLI flag) writes its real
    # reason to stderr and vanishes with the doomed tab; start-session.sh captures a bounded tail
    # into disk["launch_stderr"][id]. When the relaunch cap parks, the memo must NAME that error,
    # not just "relaunched N times (cap N)" — the whole point of issue #40.
    tail = "error: unknown option '--effort'\nclaude: run `claude --help` for usage"
    dsk = disk(exited={"i5": "x rc=1"}, launch_stderr={"i5": tail},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    out = decide(dsk=dsk)
    parks = only(out, "park")
    assert len(parks) == 1 and has_notify(out)
    memo = parks[0]["memo"]
    assert "relaunched" in memo                         # the existing cap framing is preserved
    assert "unknown option '--effort'" in memo          # ...now WITH the real launch error


def test_exited_cap_park_memo_bounds_a_huge_stderr_tail():
    # A chatty or looping launch must never dump its whole stderr into a GitHub park comment.
    huge = "\n".join("noise line %d" % i for i in range(5000))
    dsk = disk(exited={"i5": "x rc=1"}, launch_stderr={"i5": huge},
               issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    memo = only(decide(dsk=dsk), "park")[0]["memo"]
    assert "relaunched" in memo
    assert "noise line 4999" in memo                    # the TAIL (real error) is kept...
    assert "noise line 0" not in memo                   # ...the head is dropped
    assert len(memo) < 3000                             # bounded — never the full spew


def test_exited_cap_park_memo_without_stderr_is_unchanged_and_wrong_typed_is_safe():
    base = disk(exited={"i5": "x rc=1"},
                issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
    memo = only(decide(dsk=base), "park")[0]["memo"]
    assert "relaunched" in memo                          # original memo, no tail appended, no crash
    # Wrong-typed launch_stderr (non-dict, or a non-string/blank entry) must never raise — it lands
    # on the safe plain memo (module contract: wrong-typed input never becomes an exception).
    for bad in ("not-a-dict", {"i5": 123}, {"i5": None}, {"i5": "   "}, None):
        d = disk(exited={"i5": "x rc=1"}, launch_stderr=bad,
                 issues_state={"version": 1, "issues": {"i5": ist("running", retries=2)}})
        m = only(decide(dsk=d), "park")[0]["memo"]
        assert "relaunched" in m, bad


def test_relaunch_tiers_respect_the_usage_gate_but_idle_peek_does_not():
    stale_usage = {**usage_ok(), "last_ok_at": NOW - 600}   # stale WITHIN the grace -> fail closed
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


# =========================== the progress-stall probe ladder (#157) ===========================
# The i328 infinite nudge loop: a lane TAKING TURNS (activity fresh) but making NO progress
# (HEAD/marker frozen) looped forever on the idle nudge, because each nudge refreshed the very
# activity stamp the ladder watched. The ladder now keys on the PROGRESS clock
# (state/status/<id>.json), demands a machine-readable ack, caps probes per episode, and escalates
# to a real park with a dossier — never an infinite loop, never a false park of a progressing lane.

def clock(head="H", report=False, blocked=False, dirty=False, iid="i5", ts=NOW):
    return {"id": iid, "ts": ts, "cwd": "/w", "head": head,
            "dirty": dirty, "report": report, "blocked": blocked}


def _sig(**kw):
    return actions.events_mod.progress_signature(clock(**kw))


def _apply_probe(ist_dict, act, now):
    """Mimic runner._exec_probe's durable bookkeeping so a decide loop can advance an episode:
    the attempt counter climbs, the nonce rotates, the send is stamped."""
    v = ist_dict.get("probe_attempts")
    ist_dict["probe_attempts"] = (v if type(v) is int else 0) + 1
    ist_dict["probe_nonce"] = "%d-%d" % (int(now), int(act.get("attempt") or 0))
    ist_dict["probe_sent_at"] = now


def test_progress_first_sight_anchors_the_clock_without_probing():
    # A running lane whose progress clock we have never recorded: anchor it (progress_advance), do
    # NOT probe — the stall clock only starts once we have a baseline to measure against.
    st = {"version": 1, "issues": {"i5": ist("running")}}          # no stored progress_sig
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}))
    adv = only(out, "progress_advance")
    assert len(adv) == 1 and adv[0]["id"] == "i5" and adv[0]["sig"] == _sig()
    assert only(out, "probe") == [] and only(out, "park") == []


def test_progress_fresh_lane_is_never_probed_and_idle_does_not_nudge():
    # sig matches, progress_since recent -> not stalled. Even a session_idle event (activity quiet)
    # must NOT nudge a lane the progress clock says is fine: the whole point of #157 is to stop
    # poking on activity staleness.
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(), progress_since=NOW - 60)}}
    out = decide(events=[{"type": "session_idle", "id": "i5"}],
                 dsk=disk(issues_state=st, status_clocks={"i5": clock()}))
    assert only(out, "probe") == [] and only(out, "park") == []
    assert only(out, "recover") == []          # the idle peek is gated off when a clock is present


def test_real_progress_resets_a_long_stalled_lane_and_never_parks():
    # The lane was stalled for ages, but the clock now shows a NEW HEAD: that is real progress.
    # progress_advance re-anchors and the episode resets — a progressing lane is NEVER parked.
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(head="OLD"),
                                          progress_since=NOW - 100000, probe_attempts=2)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="NEW")}))
    adv = only(out, "progress_advance")
    assert len(adv) == 1 and adv[0]["sig"] == _sig(head="NEW")
    assert only(out, "probe") == [] and only(out, "park") == []


def test_i328_probe_ladder_escalates_within_the_cap_instead_of_looping():
    # THE reproduction. Clock frozen every tick, activity fresh (frozen tier never fires). The old
    # idle nudge looped forever; the new ladder MUST reach a real park within a bounded probe count.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000)
    st = {"version": 1, "issues": {"i5": ist5}}
    probes, parked, now = 0, None, NOW
    for _ in range(50):                        # bounded: this MUST terminate in a park
        out = decide(now=now, dsk=disk(issues_state=st, status_clocks={"i5": clock()}),
                     parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
        if only(out, "park"):
            parked = only(out, "park")[0]
            break
        pr = only(out, "probe")
        if pr:
            probes += 1
            _apply_probe(ist5, pr[0], now)
        now += 300
    assert parked is not None, "the ladder never escalated — it looped (the i328 bug)"
    assert parked["cause"] == "progress_stall" and has_notify(out)
    assert probes <= 3                         # PROBE_CAP: bounded, never an infinite nudge loop


def test_working_ack_does_not_stop_escalation():
    # The i328 lie: the worker keeps answering (here, WORKING with the live nonce) but never
    # progresses. A probe answer does NOT reset the progress clock, so the cap still escalates.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000)
    st = {"version": 1, "issues": {"i5": ist5}}
    parked, now = None, NOW
    for _ in range(50):
        nonce = ist5.get("probe_nonce")
        acks = {"i5": "WORKING %s" % nonce} if nonce else {}
        out = decide(now=now, dsk=disk(issues_state=st, status_clocks={"i5": clock()}, acks=acks),
                     parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
        if only(out, "park"):
            parked = only(out, "park")[0]
            break
        pr = only(out, "probe")
        if pr:
            _apply_probe(ist5, pr[0], now)
        now += 300
    assert parked is not None and parked["cause"] == "progress_stall"
    assert "WORKING" in parked["memo"]         # the dossier names the worker's own self-report


# ---- the DONE ack is the harvest's trigger (issue #189) ----
# A DONE ack is the only mechanical "I am finished" the loop ever gets from a worker: nonce-fenced,
# machine-readable, and only ever asked of a lane the progress clock says is stalled and whose pane
# nudge-pane found idle. That is precisely the discriminator the Stop hook could not have — a rest
# is not an ending, and a live draft looks exactly like a finished session's misplaced report.
# Before #189, a DONE ack with no visible report walked the cap and parked on
# "acked DONE, but produced no report/PR the loop can see" — which IS the i280/i328 stall.

def test_done_ack_with_no_report_harvests_instead_of_walking_to_a_park():
    # THE i280/i328 RESCUE, relocated: the worker says it finished, the runner sees no report, so
    # LOOK for it before concluding there is none.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}, acks={"i5": "DONE n1"}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    h = only(out, "harvest_report")
    assert len(h) == 1 and h[0]["id"] == "i5"
    assert only(out, "park") == [], "look for the report before parking on 'there is no report'"


def test_a_working_ack_never_harvests():
    # The i153/i163 shape at the decision layer: a worker still building must never have its draft
    # promoted, no matter how long its clock has been frozen.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()},
                          acks={"i5": "WORKING n1"}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    assert only(out, "harvest_report") == []


def test_silence_is_not_a_done_ack_and_never_harvests():
    # No ack at all — the lane may be wedged mid-turn. Silence is not a declaration of finishedness.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    assert only(out, "harvest_report") == []


def test_a_stale_done_ack_from_a_previous_probe_never_harvests():
    # The nonce fence carries straight through to the harvest: an old episode's DONE must not move
    # a live session's files.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n2", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()},
                          acks={"i5": "DONE n1"}),          # answers the PREVIOUS probe
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    assert only(out, "harvest_report") == []


def test_a_done_ack_never_harvests_when_the_report_is_already_visible():
    # Nothing to rescue: the runner can already SEE the report, so the finish path owns this lane.
    # HONEST SCOPE (fresh review P2): a visible report is caught by the has_report branch far
    # earlier in decide(), which never reaches the stall ladder — so this pins the LAYERED outcome
    # ("a visible report is never harvested, whichever layer stops it"), not the ladder's own
    # `iid not in reports` clause. That clause is deliberate defence-in-depth for a DESTRUCTIVE
    # move: it must not depend on a distant `continue` staying where it is.
    ist5 = ist("running", progress_sig=_sig(report=True), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(report=True)},
                          reports={"i5": "## Tests\nreal"}, acks={"i5": "DONE n1"}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    assert only(out, "harvest_report") == []


def test_a_fruitless_harvest_is_tried_once_then_the_ladder_still_parks():
    # THE BOUNDING RULE. A DONE ack whose report simply does not exist anywhere must not re-harvest
    # every tick forever — that is the i328 pathology in a new costume. The attempt is spent once
    # per episode; the ladder then resumes and still reaches its classified park.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    harvests, parked, now = 0, None, NOW
    for _ in range(50):                        # bounded: this MUST terminate in a park
        out = decide(now=now, dsk=disk(issues_state=st, status_clocks={"i5": clock()},
                                       acks={"i5": "DONE %s" % ist5.get("probe_nonce")}),
                     parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
        if only(out, "park"):
            parked = only(out, "park")[0]
            break
        if only(out, "harvest_report"):
            harvests += 1
            ist5["harvest_tried"] = True       # mimic the executor's durable stamp
        pr = only(out, "probe")
        if pr:
            _apply_probe(ist5, pr[0], now)
        now += 300
    assert harvests == 1, "the harvest is spent once per episode, never a per-tick loop"
    assert parked is not None, "a fruitless harvest must still let the ladder escalate"
    assert parked["cause"] == "progress_stall" and "DONE" in parked["memo"]


def test_a_new_episode_re_arms_the_harvest():
    # Real progress resets the episode (the executor clears the stamp), so a worker that drafts,
    # keeps building, and only later genuinely finishes still gets its one rescue.
    ist5 = ist("running", progress_sig=_sig(head="OLD"), progress_since=NOW - 100000,
               probe_attempts=2, harvest_tried=True)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="NEW")}))
    adv = only(out, "progress_advance")
    assert len(adv) == 1, "a lane that moved HEAD re-anchors — and re-arms its rescue"


def test_stuck_ack_escalates_immediately_before_the_cap():
    # A STUCK ack (with the live nonce) is a definitive "I need help" — escalate now, don't keep
    # probing to the cap. It reaches the owner (needs-owner), not the plain parked queue.
    ist5 = ist("running", progress_sig=_sig(), progress_since=NOW - 100000,
               probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 1000)
    st = {"version": 1, "issues": {"i5": ist5}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()},
                          acks={"i5": "STUCK n1"}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["cause"] == "progress_stall"
    assert parks[0]["needs_william"] is True and "STUCK" in parks[0]["memo"]
    assert only(out, "probe") == []            # escalated, not re-probed


def test_corrupt_probe_attempts_fails_closed_to_a_park():
    # The fail-OPEN-on-wrong-typed defect class: a corrupt probe-attempt counter must NOT read as 0
    # and re-probe (an unbounded loop). It fails CLOSED to a classified park, like every other cap
    # counter in the module.
    for bad in ("3", None, True, [], 3.0):
        st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(),
                                              progress_since=NOW - 100000, probe_attempts=bad)}}
        out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}),
                     parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
        parks = only(out, "park")
        assert len(parks) == 1 and parks[0]["cause"] == "progress_stall", bad
        assert only(out, "probe") == [], bad


def test_awaiting_suppresses_the_probe_ladder():
    # A worker that flagged long background work (awaiting marker) is quiet by contract — never
    # probe it, never park it, exactly as the activity idle-peek already respects awaiting.
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(),
                                          progress_since=NOW - 100000)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}, awaiting={"i5": ""}))
    assert only(out, "probe") == [] and only(out, "park") == []


def test_probe_respects_the_retry_interval():
    # Within PROBE_RETRY_SECONDS of the last probe, no new probe fires (bounded cadence).
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(),
                                          progress_since=NOW - 100000,
                                          probe_attempts=1, probe_nonce="n1", probe_sent_at=NOW - 60)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock()}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    assert only(out, "probe") == [] and only(out, "park") == []


def test_no_progress_clock_falls_back_to_the_idle_peek():
    # Graceful degradation: an install/session with no status clock (the hook never stamped) keeps
    # the old activity idle-peek behavior rather than being silently un-probeable.
    st = {"version": 1, "issues": {"i5": ist("running")}}
    out = decide(events=[{"type": "session_idle", "id": "i5"}], dsk=disk(issues_state=st))
    r = only(out, "recover")
    assert len(r) == 1 and r[0] == {"act": "recover", "id": "i5", "tier": "idle"}


def test_progress_stall_park_dossier_names_the_evidence():
    # The park at the cap carries a real dossier: the stall duration, the frozen HEAD, the probe
    # count. This is what William reads instead of an unbounded nudge loop.
    st = {"version": 1, "issues": {"i5": ist("running", progress_sig=_sig(head="abc123def456"),
                                          progress_since=NOW - 1800, probe_attempts=3,
                                          probe_nonce="n3", probe_sent_at=NOW - 400)}}
    out = decide(dsk=disk(issues_state=st, status_clocks={"i5": clock(head="abc123def456")}),
                 parsed_issues=[parsed(5, labels=("in-progress", "type:build"))])
    memo = only(out, "park")[0]["memo"]
    assert "abc123def456"[:12] in memo and "3 probe" in memo
    assert "min" in memo and ("i328" in memo or "progress" in memo)


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


# --------------------------- merge refusals: bounded + surfaced (issue #27) ---------------------------
# A gate-green PR whose merge GitHub refuses (ordinary branch protection — required approvals /
# strict up-to-date — or a token without merge rights) used to retry every tick forever: no
# counter, no cap, no park, no notify. The executor bumps `merge_refusals` and records the gh
# stderr in `merge_refusal_reason` on each refusal; decide keeps retrying UNDER the bound, then
# parks needs-william ONCE with the reason and one notify. The bright line holds: surface branch
# protection to the owner, never bypass it.

def test_a_merge_refusal_under_the_bound_still_retries_with_no_noise():
    # DoD #2: a transient refusal that clears within the bound merges cleanly — decide keeps
    # emitting merge (the retry) and raises NO park and NO notify while under the cap.
    d, g = _gating()
    d["issues_state"]["issues"]["i5"].update(
        merge_refusals=actions.MERGE_REFUSAL_CAP - 1,
        merge_refusal_reason="failed to merge: the base branch has moved")
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "merge")) == 1                 # still retrying
    assert only(out, "park") == [] and not has_notify(out)


def test_merge_refused_to_the_cap_parks_needs_william_with_reason_and_one_notify():
    d, g = _gating()
    reason = "failed to merge: Protected branch update failed (2 approving reviews required)"
    d["issues_state"]["issues"]["i5"].update(
        merge_refusals=actions.MERGE_REFUSAL_CAP, merge_refusal_reason=reason)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == []                     # retries STOP at the cap
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True
    assert "2 approving reviews required" in p[0]["memo"]   # the refusal reason is surfaced
    n = only(out, "notify")
    assert len(n) == 1 and "2 approving reviews required" in n[0]["body"]


def test_corrupt_merge_refusal_counter_fails_closed_to_a_park():
    # The fail-OPEN-on-wrong-TYPED defect class: a corrupt counter must NOT read as 0 and re-merge
    # forever — it lands on the safe action (park to William), like every other capped counter.
    d, g = _gating()
    d["issues_state"]["issues"]["i5"]["merge_refusals"] = "lots"
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and has_notify(out)
    # the corrupt path does NOT claim a specific count it doesn't have
    assert "unreadable" in p[0]["memo"] and "consecutive times" not in p[0]["memo"]


def test_a_missing_refusal_reason_still_parks_with_a_sensible_memo():
    # The cap trips even if the reason was never captured (an odd crash window): the memo must not
    # be empty or malformed.
    d, g = _gating()
    d["issues_state"]["issues"]["i5"]["merge_refusals"] = actions.MERGE_REFUSAL_CAP
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and isinstance(p[0]["memo"], str) and len(p[0]["memo"]) > 20


def test_reset_merge_refusal_counter_merges_again_episode_scoped():
    # DoD #3: the guard is episode-scoped, never forever-latched. Once the counter is zeroed (the
    # reapprove executor's effect), the very PR that was capped merges again from scratch.
    d, g = _gating()
    d["issues_state"]["issues"]["i5"].update(merge_refusals=0, merge_refusal_reason=None)
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "merge")) == 1 and only(out, "park") == []


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


# ---------------- the nudge->park compliance window (issue #222) ----------------
# The pre-#222 grace was ONE tick: decide stamped the nudge and the very next tick (~18-25s) found the
# key present and parked, so no worker could post a review comment or fix report sections in time. The
# fix: decide stamps `nudged_at` on delivery and, until the configurable window elapses, the gate
# WAITS on that cause instead of parking. One nudge per cause is unchanged; only the WAIT is bounded.

def _nudged(iid="i5", key="review", at=NOW, **over):
    """A gating build lane that has already had `key` nudged, stamped at `at`."""
    base = {"nudged": [key], "nudged_at": {key: at}}
    base.update(over)
    return {iid: ist("gating", branch="sl/i5-issue-5", pr=555, **base)}


def test_a_freshly_nudged_review_waits_out_its_window_never_parks():
    # DoD: a lane whose review evidence is still missing 2 minutes after its nudge does NOT park —
    # the compliance window (default 480s) is still open, so decide neither parks nor re-nudges.
    d, g = _gating(pv=pr_view(comments=[]),                 # clean empty read: review genuinely absent
                   issues_extra=_nudged(key="review", at=NOW))
    out = decide(now=NOW + 120, dsk=d, gh_view=g)           # +2 min, well inside the 480s window
    assert only(out, "park") == [] and not has_notify(out)  # NOT the one-tick park
    assert only(out, "nudge") == []                         # and NOT a second nudge (one per cause)
    assert only(out, "merge") == []                         # nothing to merge yet — just waiting


def test_the_landing_review_gates_through_with_no_park_and_no_second_nudge():
    # the other half of the DoD: once the review comment LANDS (inside the window), the lane MERGES —
    # one nudge total, zero parks. The verdict is pinned to the PR head (the default pr_view shape).
    d, g = _gating(pv=pr_view(),                            # default comments carry the pinned marker
                   issues_extra=_nudged(key="review", at=NOW))
    out = decide(now=NOW + 120, dsk=d, gh_view=g)           # review arrived 2 min after the nudge
    assert len(only(out, "merge")) == 1
    assert only(out, "park") == [] and only(out, "nudge") == []


def test_the_window_parks_once_it_actually_elapses():
    # the ladder still terminates: past the window with the cause still unmet, decide parks — once.
    d, g = _gating(pv=pr_view(comments=[]), issues_extra=_nudged(key="review", at=NOW))
    out = decide(now=NOW + actions.NUDGE_GRACE_WINDOW_SECONDS + 1, dsk=d, gh_view=g)
    assert len(only(out, "park")) == 1 and has_notify(out)


def test_a_missing_or_corrupt_nudge_stamp_reads_as_expired_and_parks():
    # fail-closed: a nudged key with NO stamp (e.g. a lane nudged under pre-#222 state) — or a
    # future/negative one — reads as EXPIRED, so the ladder stays bounded (park, never a stuck wait).
    for bad in ({}, {"review": NOW + 10_000}, {"review": -5}, "not-a-dict"):
        d, g = _gating(pv=pr_view(comments=[]),
                       issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                                nudged=["review"], nudged_at=bad)})
        out = decide(now=NOW + 60, dsk=d, gh_view=g)        # only 60s in, yet the bad stamp expires it
        assert len(only(out, "park")) == 1, f"nudged_at={bad!r} must fail closed to park"


def test_the_window_is_config_overridable():
    # session.nudge_grace_window_seconds widens/narrows the grace. A 30s window parks at +60s where
    # the 480s default would still be waiting — proving the value flows from config, not a constant.
    short = cfg(session={"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                         "conflict_cap": 2, "nudge_grace_window_seconds": 30})
    d, g = _gating(pv=pr_view(comments=[]), issues_extra=_nudged(key="review", at=NOW))
    out = decide(now=NOW + 60, config=short, dsk=d, gh_view=g)
    assert len(only(out, "park")) == 1                      # 60s > the 30s override -> parked


def test_a_reapproved_lane_gets_a_fresh_nudge_before_any_park():
    # DoD defect (b): a re-approval (resume_at_gate/reapprove) clears nudged+nudged_at, so the very
    # next gate look on a still-unmet cause NUDGES fresh — it does NOT park instantly on a stale key.
    d, g = _gating(pv=pr_view(comments=[]),
                   issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           nudged=[], nudged_at={})})
    out = decide(now=NOW + 5, dsk=d, gh_view=g)
    assert len(only(out, "nudge")) == 1 and only(out, "nudge")[0]["nudge_key"] == "review"
    assert only(out, "park") == []                          # a fresh nudge FIRST, never an instant park


def test_a_corrupt_nudged_element_never_raises_into_the_tick():
    # decide-never-raises contract: a hand-corrupt non-str (here non-hashable) element in `nudged`
    # must not blow up the compliance-window computation; it reads as expired -> park, fail closed.
    d, g = _gating(pv=pr_view(comments=[]),
                   issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           nudged=["review", ["oops"]], nudged_at={"review": NOW})})
    out = decide(now=NOW + 5, dsk=d, gh_view=g)             # must not raise
    # 'review' is fresh (window open) -> the corrupt element does not force a park by itself, and the
    # tick completes; the point is simply that decide returned SOMETHING without crashing.
    assert isinstance(out, list)


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


def test_preauthorized_referee_issue_merges_through_decide():
    # issue #165 end-to-end through the runner: the owner's `pre-authorized:referee` label on the
    # LIVE issue flows (via gate.preauthorized_referee) into the gate, which merges the referee-
    # touching PR instead of re-parking. No park, no needs-owner notify.
    d, g = _gating(pv=pr_view(files=["src/f/Widget.tsx", ".superlooper/config.json"]))
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build",
                                                  gate.PREAUTHORIZED_REFEREE_LABEL),
                                       touches=("frontend",))],
                 dsk=d, gh_view=g)
    m = only(out, "merge")
    assert len(m) == 1 and m[0]["num"] == 5
    assert only(out, "park") == []
    assert not any("needs-owner" in n.get("title", "") for n in only(out, "notify"))


def test_preauthorized_merge_act_carries_the_referee_audit_trail():
    # issue #165: a pre-authorized referee merge must never be a SILENT auto-merge. The gate names
    # the paths; the ACT is what survives into the journal (_journal_outcome writes the act dict
    # verbatim) and into the merge comment. If the act drops them, the one unattended merge that
    # crosses a bright line becomes the least legible record in the loop — and the merge journal
    # naming every referee touch is the compensating control approval-protocol.md offers for the
    # coarse per-issue grant. So assert the keys ride onto the act itself.
    d, g = _gating(pv=pr_view(files=["src/f/Widget.tsx", ".superlooper/config.json"]))
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build",
                                                  gate.PREAUTHORIZED_REFEREE_LABEL),
                                       touches=("frontend",))],
                 dsk=d, gh_view=g)
    m = only(out, "merge")
    assert len(m) == 1
    assert m[0].get("referee_preauthorized") is True
    assert m[0].get("referee_paths") == [".superlooper/config.json"]


def test_ordinary_merge_act_carries_no_referee_noise():
    # the flag is inert on an ordinary merge — no referee keys on an act that crossed no bright line
    d, g = _gating(pv=pr_view(files=["src/f/Widget.tsx"]))
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build",
                                                  gate.PREAUTHORIZED_REFEREE_LABEL),
                                       touches=("frontend",))],
                 dsk=d, gh_view=g)
    m = only(out, "merge")
    assert len(m) == 1 and "referee_preauthorized" not in m[0] and "referee_paths" not in m[0]


def test_foreseeable_referee_issue_not_launched_without_preauth():
    # issue #165: an approved issue whose declared touches resolve to a referee path is NOT launched
    # unattended without the owner's pre-authorization — it waits, rather than burning a lane to a
    # certain park. Granting the label lets it launch.
    config = cfg(areas={"frontend": ["src/f/**"], "loop_rules": [".superlooper/**"]})
    held = decide(config=config,
                  parsed_issues=[parsed(9, touches=("loop_rules",))])   # agent-ready, no preauth
    assert only(held, "launch") == []
    allowed = decide(config=config,
                     parsed_issues=[parsed(9, touches=("loop_rules",),
                                           labels=("agent-ready", "type:build",
                                                   gate.PREAUTHORIZED_REFEREE_LABEL))])
    assert len(only(allowed, "launch")) == 1 and only(allowed, "launch")[0]["num"] == 9


def test_foreseeable_referee_hold_is_journaled_not_silent():
    # issue #165 + the #36/#150 "never a silent hold" discipline: a withheld referee issue is not
    # just skipped — the runner journals WHY (a launch_hold naming the pre-authorization label), so
    # "why isn't my approved issue running?" is answerable. Journal-only: no park, no notify.
    config = cfg(areas={"frontend": ["src/f/**"], "loop_rules": [".superlooper/**"]})
    out = decide(config=config, parsed_issues=[parsed(9, touches=("loop_rules",))])
    assert only(out, "launch") == [] and only(out, "park") == []
    holds = only(out, "launch_hold")
    assert len(holds) == 1 and holds[0]["id"] == "i9"
    assert gate.PREAUTHORIZED_REFEREE_LABEL in holds[0]["reason"]
    # pre-authorized -> it launches, and there is NO hold journal line
    out2 = decide(config=config,
                  parsed_issues=[parsed(9, touches=("loop_rules",),
                                        labels=("agent-ready", "type:build",
                                                gate.PREAUTHORIZED_REFEREE_LABEL))])
    assert len(only(out2, "launch")) == 1 and only(out2, "launch_hold") == []


def test_conflicting_pr_gets_a_mechanical_update():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    out = decide(dsk=d, gh_view=g)
    u = only(out, "update")
    assert len(u) == 1 and u[0]["pr"] == 555 and u[0]["head_oid"] == HEAD1


def test_clean_update_result_waits_for_github_to_recompute():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="clean", update_head_oid=HEAD1)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "update") == [] and only(out, "regenerate") == []


def test_head_change_invalidates_a_stale_update_result():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", oid=HEAD2))
    d["issues_state"]["issues"]["i5"].update(update_result="clean", update_head_oid=HEAD1)
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "update")) == 1              # stale verdict discarded, retry the update


def test_real_conflict_under_cap_regenerates_on_a_fresh_generation_branch():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1,
                                             conflicts=0)
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    r = only(out, "regenerate")
    assert len(r) == 1
    assert r[0]["new_branch"] == "sl/i5-issue-5-r1" and r[0]["conflicts"] == 1
    assert r[0]["pr"] == 555


def test_conflict_cap_parks_needs_william_with_notify():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1,
                                             conflicts=1)
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and has_notify(out)


def test_preserve_label_hires_a_conflict_resolution_session():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1)
    # Hiring the resolver is a session START, so since #150 it reads this issue's eligibility from
    # the view — where a real gating issue always is.
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    rc = only(out, "resolve_conflict")
    assert len(rc) == 1 and rc[0]["pr"] == 555
    assert only(out, "regenerate") == []


def test_dead_auth_holds_the_conflict_resolver_and_alerts():
    # A conflict resolver is a session START (#159): dead account auth would spawn it logged-out and
    # burn the spend, so it holds (never a resolve_conflict, never a park) and the auth_dead alert
    # surfaces (an in-flight lane is relaunch demand).
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1)
    d["auth_probe"] = _auth_dead()
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=d, gh_view=g)
    assert only(out, "resolve_conflict") == []             # not spent into dead auth
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i5" and "auth" in h[0]["reason"].lower()
    assert only(out, "alert")[0]["reasons"] == ["auth_dead"]


def test_update_error_retries_and_persistent_errors_alert():
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING"))
    d["issues_state"]["issues"]["i5"].update(update_result="error", update_head_oid=HEAD1,
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
    # an investigation close is not a merge: freeze never blocks it (the close still runs
    # through the exit interview — an accounted NO-FINDINGS reply, #215)
    d2 = disk(reports={"i7": GOOD_REPORT}, frozen={"reason": "dev red", "fingerprint": "f", "since": 1},
              issues_state={"version": 1, "issues": {"i7": ist("gating", type="investigate")}})
    g2 = ghv(prs={}, issue_comments={"i7": [
        {"body": "<!-- superlooper-investigation --> root cause: X"},
        {"body": "NO-FINDINGS"}]})
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
    # After holding on refused reads, a clean read carrying the marker AND an accounted exit
    # reply (#215) closes the parent cleanly.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {
                 "i7": ist("gating", type="investigate", read_waited=True)}})
    g = ghv(issue_comments={"i7": [
        {"body": "<!-- superlooper-investigation --> root cause: X"},
        {"body": "NO-FINDINGS"}]})
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
    # is NOT in the open-issue queue; the type comes from loopstate. Since #215 the reconciled
    # close ALSO runs through the exit interview: the reply must exist and be accounted.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("parked", type="investigate")}})
    g = ghv(issue_comments={"i7": [
        {"body": "<!-- superlooper-investigation --> root cause: X"},
        {"body": "NO-FINDINGS"}]})
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert [a["act"] for a in out] == [a["act"] for a in out if a["act"] == "close_investigate"]
    assert len(only(out, "close_investigate")) == 1 and only(out, "close_investigate")[0]["num"] == 7


def test_parked_investigation_with_marker_but_no_exit_reply_stays_parked():
    # #215: the marker alone no longer reconciles a parked investigation — closing unaccounted is
    # the exact defect this gate exists to end. A parked lane has no session to interview, so it
    # simply stays parked (the owner's call), with no ask, no park-churn, no notify.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("parked", type="investigate")}})
    g = ghv(issue_comments={"i7": [{"body": "<!-- superlooper-investigation --> root cause: X"}]})
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert only(out, "close_investigate") == [] and only(out, "exit_interview") == []
    assert not has_notify(out)


def test_parked_investigation_with_findings_reply_verifies_then_closes():
    # a truthful FINDINGS-FILED reply still reconciles a parked investigation: first the ONE
    # typed child-set read, then (verdict stamped, nothing missing) the close.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {"i7": ist("parked", type="investigate")}})
    g = ghv(issue_comments={"i7": [
        {"body": "<!-- superlooper-investigation --> root cause: X"},
        {"body": "FINDINGS-FILED: #41 #42", "createdAt": "2026-07-16T10:00:00Z"}]})
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    v = only(out, "verify_exit_refs")
    assert len(v) == 1 and v[0]["num"] == 7 and v[0]["refs"] == [41, 42]
    assert only(out, "close_investigate") == []
    d["issues_state"]["issues"]["i7"]["exit_verify"] = {
        "key": "2026-07-16T10:00:00Z", "missing": []}
    out2 = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert len(only(out2, "close_investigate")) == 1
    c = only(out2, "close_investigate")[0]
    assert c["exit"] == "FINDINGS-FILED: #41 #42"
    # ...but a verdict naming an unaccounted ref leaves it parked — never a close.
    d["issues_state"]["issues"]["i7"]["exit_verify"] = {
        "key": "2026-07-16T10:00:00Z", "missing": [42]}
    out3 = decide(parsed_issues=[], dsk=d, gh_view=g)
    assert only(out3, "close_investigate") == [] and only(out3, "verify_exit_refs") == []


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


# =========================== issue #215: the exit interview ===========================
# An investigation closes only through an end-of-run exit interview — findings filed as
# needs-owner children and verified against the parent's real child set, or an explicit
# NO-FINDINGS. Asked at the freshest turn (the finish), parsed as ONE strict line, bounded by
# the ask ladder: the interview + one re-ask, then park. Never close unaccounted.

_MARKER_COMMENT = {"body": "<!-- superlooper-investigation --> root cause: X"}


def _inv_finishing(comments=(_MARKER_COMMENT,), dsk_extra=None, **ist_over):
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {
                 "i7": ist("gating", type="investigate", **ist_over)}},
             **(dsk_extra or {}))
    g = ghv(issue_comments={"i7": list(comments)})
    p7 = parsed(7, labels=("in-progress", "type:investigate"))
    return d, g, p7


def test_finished_investigation_with_marker_gets_the_interview_not_a_close():
    d, g, p7 = _inv_finishing()
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out, "close_investigate") == []          # the motivating incident, closed
    a = only(out, "exit_interview")
    assert len(a) == 1 and a[0]["id"] == "i7" and a[0]["num"] == 7


def test_interview_asked_within_window_waits_quietly():
    d, g, p7 = _inv_finishing(exit_asks=1, exit_asked_at=NOW - 10)
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    for act in ("exit_interview", "close_investigate", "park", "nudge"):
        assert only(out, act) == [], act


def test_interview_no_reply_past_window_reasks_then_parks_with_notify():
    late = NOW - actions.EXIT_REPLY_WINDOW_SECONDS - 1
    d, g, p7 = _inv_finishing(exit_asks=1, exit_asked_at=late)
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert len(only(out, "exit_interview")) == 1         # the one re-ask
    d2, g2, _ = _inv_finishing(exit_asks=2, exit_asked_at=late)
    out2 = decide(parsed_issues=[p7], dsk=d2, gh_view=g2)
    p = only(out2, "park")
    assert len(p) == 1 and has_notify(out2)
    assert "never verifiably delivered" in p[0]["memo"]  # no receipt: honest delivery state


def test_consumption_receipt_restarts_the_reply_window():
    # delivery is judged by the receipt, never the send rc — a mail consumed late (the worker
    # was mid-turn) gets its window from CONSUMPTION, so a slow-but-alive lane is not re-asked.
    late = NOW - actions.EXIT_REPLY_WINDOW_SECONDS - 100
    d, g, p7 = _inv_finishing(exit_asks=1, exit_asked_at=late,
                              dsk_extra={"exit_receipts": {"i7": NOW - 30}})
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out, "exit_interview") == [] and only(out, "park") == []
    # ...and once even the receipt-based window expires, the park memo says DELIVERED.
    d2, g2, _ = _inv_finishing(
        exit_asks=2, exit_asked_at=late,
        dsk_extra={"exit_receipts": {"i7": late + 50}})
    out2 = decide(parsed_issues=[p7], dsk=d2, gh_view=g2)
    p = only(out2, "park")
    assert len(p) == 1 and "delivered" in p[0]["memo"] \
        and "never verifiably delivered" not in p[0]["memo"]


def test_no_findings_reply_closes_with_the_accounted_claim():
    d, g, p7 = _inv_finishing(comments=[_MARKER_COMMENT, {"body": "NO-FINDINGS"}])
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    c = only(out, "close_investigate")
    assert len(c) == 1 and c[0]["num"] == 7 and c[0]["exit"] == "NO-FINDINGS"


def test_findings_reply_emits_exactly_one_typed_verification_read():
    d, g, p7 = _inv_finishing(comments=[
        _MARKER_COMMENT,
        {"body": "FINDINGS-FILED: #41 #42", "createdAt": "2026-07-16T09:00:00Z"}])
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    v = only(out, "verify_exit_refs")
    assert len(v) == 1 and v[0]["id"] == "i7" and v[0]["num"] == 7
    assert v[0]["refs"] == [41, 42] and v[0]["reply_key"] == "2026-07-16T09:00:00Z"
    assert only(out, "close_investigate") == []
    # with the verdict stamped clean, the SAME view closes — no second read is ever emitted.
    d["issues_state"]["issues"]["i7"]["exit_verify"] = {
        "key": "2026-07-16T09:00:00Z", "missing": []}
    out2 = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out2, "verify_exit_refs") == []
    assert len(only(out2, "close_investigate")) == 1


def test_newest_reply_wins_through_decide():
    d, g, p7 = _inv_finishing(comments=[
        _MARKER_COMMENT,
        {"body": "FINDINGS-FILED: garbage"},             # malformed, then corrected:
        {"body": "NO-FINDINGS"}])
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert len(only(out, "close_investigate")) == 1


def test_codex_ack_is_relayed_to_the_durable_comment_and_holds_the_ladder():
    # the degraded path: the ack file carries the one-line reply + nonce; the runner posts it
    # durably. While the relay is pending, the ladder must neither re-ask nor park — the runner
    # itself holds the answer.
    late = NOW - actions.EXIT_REPLY_WINDOW_SECONDS - 1
    d, g, p7 = _inv_finishing(exit_asks=2, exit_asked_at=late, exit_nonce="exit-77",
                              dsk_extra={"acks": {"i7": "NO-FINDINGS exit-77"}})
    out = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    r = only(out, "relay_exit_reply")
    assert len(r) == 1 and r[0]["num"] == 7 and r[0]["line"] == "NO-FINDINGS" \
        and r[0]["nonce"] == "exit-77"
    assert only(out, "park") == [] and only(out, "exit_interview") == []
    # relayed already: never posted twice, and the ladder resumes normally.
    d["issues_state"]["issues"]["i7"]["exit_ack_relayed"] = "exit-77"
    out2 = decide(parsed_issues=[p7], dsk=d, gh_view=g)
    assert only(out2, "relay_exit_reply") == []
    assert len(only(out2, "park")) == 1                  # cap spent, window expired, no reply yet
    # a stale ack (wrong nonce — e.g. the #157 progress ladder's) is never relayed.
    d2, g2, _ = _inv_finishing(exit_asks=1, exit_asked_at=NOW - 10, exit_nonce="exit-78",
                               dsk_extra={"acks": {"i7": "WORKING exit-12"}})
    out3 = decide(parsed_issues=[p7], dsk=d2, gh_view=g2)
    assert only(out3, "relay_exit_reply") == []


def test_parked_lane_with_a_stranded_ack_relays_it_then_reconciles():
    # fresh review P2-4: a lane parked OUT-OF-BAND (not by the gate's own relay-suppressed
    # paths) can still hold a valid nonce-fenced ack — the worker DID answer. The reconcile
    # relays it to the durable thread first (verdict deferred a tick), then closes off the
    # posted comment like any reconcile.
    d = disk(reports={"i7": GOOD_REPORT},
             issues_state={"version": 1, "issues": {
                 "i7": ist("parked", type="investigate", exit_nonce="exit-9")}},
             acks={"i7": "NO-FINDINGS exit-9"})
    g = ghv(issue_comments={"i7": [_MARKER_COMMENT]})
    out = decide(parsed_issues=[], dsk=d, gh_view=g)
    r = only(out, "relay_exit_reply")
    assert len(r) == 1 and r[0]["line"] == "NO-FINDINGS" and r[0]["num"] == 7
    assert only(out, "close_investigate") == []          # verdict deferred while the relay posts
    # relayed + the comment visible: the ordinary reconcile close fires.
    d["issues_state"]["issues"]["i7"]["exit_ack_relayed"] = "exit-9"
    g2 = ghv(issue_comments={"i7": [_MARKER_COMMENT, {"body": "NO-FINDINGS"}]})
    out2 = decide(parsed_issues=[], dsk=d, gh_view=g2)
    assert only(out2, "relay_exit_reply") == []
    assert len(only(out2, "close_investigate")) == 1


def test_exit_window_is_config_tunable():
    d, g, p7 = _inv_finishing(exit_asks=1, exit_asked_at=NOW - 100)
    c = cfg(session={"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                     "conflict_cap": 2, "exit_reply_window_seconds": 60})
    out = decide(config=c, dsk=d, gh_view=g, parsed_issues=[p7])
    assert len(only(out, "exit_interview")) == 1         # 100s > the 60s override: re-ask


# =========================== issue #61: refused PR lookup holds; notify-once per park cause ====
# The 2026-07-08 park-notify storm (41 texts, two ~6.5-min bursts): during hourly GraphQL dead
# zones the PR-for-branch lookup collapsed "GitHub refused" into "no PR exists", so finished
# builds were parked as PR-less — and the park path re-notified every tick while its own label
# write kept failing in the same dead zone. Guard (a): a refused lookup is OMITTED from the view
# (gh.PrRead), so the build gate HOLDs — journaled once, bounded — and only a bound expiry parks,
# once. Guard (b): the durable park_notify_cause marker makes a re-derived park a SILENT retry.


def _gating_no_pr_read(**ist_over):
    d = disk(reports={"i5": GOOD_REPORT},
             issues_state={"version": 1, "issues": {
                 "i5": ist("gating", branch="sl/i5-issue-5", **ist_over)}})
    return d, ghv(prs={})       # FRESH view, but i5's PR lookup did not land (refused / starved)


def test_finished_build_refused_pr_lookup_holds_never_parks():
    d, g = _gating_no_pr_read()
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == [] and not has_notify(out)
    w = only(out, "await_pr_read")
    assert len(w) == 1 and w[0]["id"] == "i5" and w[0]["num"] == 5 and w[0]["reason"]


def test_await_pr_read_stamps_once_per_episode():
    # bounded refusal journaling: once the wait clock is stamped, no further await_pr_read
    # records this episode — a long dead zone journals one record, not one per tick.
    d, g = _gating_no_pr_read(pr_read_pending_since=NOW - 10)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "await_pr_read") == [] and only(out, "park") == [] and not has_notify(out)


def test_corrupt_pr_read_clock_restamps_never_parks():
    # the _since_ok discipline (issue #26): a wrong-typed/future/negative clock is corrupt —
    # re-stamp it, never trust it to trip (or defeat) the bound.
    for bad in ("x", -5, NOW + 999, True):
        d, g = _gating_no_pr_read(pr_read_pending_since=bad)
        out = decide(dsk=d, gh_view=g)
        assert only(out, "park") == [], bad
        assert len(only(out, "await_pr_read")) == 1, bad


def test_refused_pr_lookup_past_bound_parks_once_with_notify():
    d, g = _gating_no_pr_read(pr_read_pending_since=NOW - actions.PR_READ_HOLD_CAP_SECONDS)
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["cause"] == "pr_read_refused" and has_notify(out)
    assert p[0]["needs_william"] is False
    # ...and while the park's own label move keeps failing (same dead zone), the re-derived park
    # next tick is a SILENT retry: the durable marker suppresses the repeat notify.
    d["issues_state"]["issues"]["i5"]["park_notify_cause"] = "pr_read_refused"
    out2 = decide(dsk=d, gh_view=g)
    p2 = only(out2, "park")
    assert len(p2) == 1 and p2[0].get("retry") is True and not has_notify(out2)


def test_pr_lookup_recovery_clears_the_wait_and_merges():
    d, g = _gating(issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           pr_read_pending_since=NOW - 300)})
    out = decide(dsk=d, gh_view=g)
    assert only(out, "clear_pr_read") == [{"act": "clear_pr_read", "id": "i5"}]
    assert len(only(out, "merge")) == 1 and only(out, "park") == [] and not has_notify(out)


def test_answered_empty_pr_lookup_still_parks_once():
    # distinguishability: {} IS a trustworthy answer ("GitHub says no PR on this head") — the
    # gate still parks, once, and the wait clock clears. Only a REFUSED read holds.
    d, g = _gating(pv={}, issues_extra={"i5": ist("gating", branch="sl/i5-issue-5",
                                                  pr_read_pending_since=NOW - 300)})
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "park")) == 1 and has_notify(out)
    assert only(out, "clear_pr_read") == [{"act": "clear_pr_read", "id": "i5"}]


def test_stale_view_never_stamps_the_pr_read_wait():
    # A wholly-stale gh view (a real outage) must not spawn per-issue await_pr_read noise — the
    # poll's consecutive_failures ALERT owns that (mirrors await_read's stale-view rule).
    d, g = _gating_no_pr_read()
    g["stale"] = True
    out = decide(dsk=d, gh_view=g)
    assert only(out, "await_pr_read") == [] and only(out, "park") == []


# =========================== issue #78: refused COMMENTS read holds the build gate ============
# The build-gate sibling of #61: the PR LOOKUP can succeed while the comments endpoint is refused
# (a partial dead zone). On a finished, reviewed PR whose review marker exists but has not yet
# been seen, the old attachment discarded CommentRead.ok — a refused comments read was
# indistinguishable from "GitHub answered: no comments", so tick N nudged "review" (key spent) and
# tick N+k parked a completed, properly-reviewed build. Guard: the runner attaches comments ONLY
# on a clean read, leaving the key ABSENT otherwise, and the gate WAITs on comments-absent
# (comments_unread) — journaled once per episode, bounded to a park past the same refused-read
# bound, cleared when a trustworthy read lands. Symmetric with await_pr_read (#61)/await_read(#21).


def _gating_comments_unread(**ist_over):
    # a FRESH view where i5's PR LOOKUP landed (number present) but its comments sub-read was
    # REFUSED/starved, so the runner OMITTED the 'comments' key (issue #78).
    pv = pr_view()
    del pv["comments"]
    return _gating(pv=pv, issues_extra={
        "i5": ist("gating", branch="sl/i5-issue-5", pr=555, **ist_over)})


def test_finished_build_refused_comments_read_holds_never_parks():
    d, g = _gating_comments_unread()
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == [] and not has_notify(out)
    w = only(out, "await_comments_read")
    assert len(w) == 1 and w[0]["id"] == "i5" and w[0]["num"] == 5 and w[0]["reason"]
    # DoD case 3: the refusal spends NO "review" nudge key — the ladder is untouched by the hold.
    assert only(out, "nudge") == []


def test_await_comments_read_stamps_once_per_episode():
    # bounded refusal journaling: once the wait clock is stamped, no further await_comments_read
    # records this episode — a long partial dead zone journals one record, not one per tick.
    d, g = _gating_comments_unread(comments_read_pending_since=NOW - 10)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "await_comments_read") == [] and only(out, "park") == [] and not has_notify(out)


def test_corrupt_comments_read_clock_restamps_never_parks():
    # the _since_ok discipline (issue #26): a wrong-typed/future/negative clock is corrupt —
    # re-stamp it, never trust it to trip (or defeat) the bound.
    for bad in ("x", -5, NOW + 999, True):
        d, g = _gating_comments_unread(comments_read_pending_since=bad)
        out = decide(dsk=d, gh_view=g)
        assert only(out, "park") == [], bad
        assert len(only(out, "await_comments_read")) == 1, bad


def test_refused_comments_read_past_bound_parks_once_with_notify():
    # a permanent partial dead zone (PR lookup up, comments endpoint down) must still hand to the
    # owner instead of holding silent-forever — park ONCE past the same bound, soft (re-approving
    # after reads recover picks it up), exactly like await_pr_read's bound expiry.
    d, g = _gating_comments_unread(
        comments_read_pending_since=NOW - actions.PR_READ_HOLD_CAP_SECONDS)
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0]["cause"] == "comments_read_refused" and has_notify(out)
    assert p[0]["needs_william"] is False


def test_comments_read_recovery_clears_the_wait_and_merges():
    # DoD case 1 recovery: the comments read lands (marker present) -> clear_comments_read + merge,
    # and the wait clock ends so a later refusal times fresh.
    d, g = _gating(issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           comments_read_pending_since=NOW - 300)})
    out = decide(dsk=d, gh_view=g)
    assert only(out, "clear_comments_read") == [{"act": "clear_comments_read", "id": "i5"}]
    assert len(only(out, "merge")) == 1 and only(out, "park") == [] and not has_notify(out)


def test_clean_empty_comments_still_nudges_then_parks_never_waits():
    # DoD case 2 unchanged: a CLEAN answered-empty comments read (a real [], review endpoint UP)
    # is NOT an unread-hold — the review nudge->park ladder proceeds and the comments clock is
    # never stamped (so no clear_comments_read either).
    d, g = _gating(pv=pr_view(comments=[]))
    out = decide(dsk=d, gh_view=g)
    assert only(out, "await_comments_read") == [] and only(out, "clear_comments_read") == []
    n = only(out, "nudge")
    assert len(n) == 1 and n[0]["nudge_key"] == "review"


def test_stale_view_never_stamps_the_comments_read_wait():
    # A wholly-stale gh view (a real outage) must not spawn per-issue await_comments_read noise —
    # the poll's consecutive_failures ALERT owns that (mirrors await_pr_read's stale-view rule).
    d, g = _gating_comments_unread()
    g["stale"] = True
    out = decide(dsk=d, gh_view=g)
    assert only(out, "await_comments_read") == [] and only(out, "park") == []


# ---------------- notify-once per (issue, park-cause) (issue #61 (b)) ----------------

def test_park_notify_precedes_the_park_action():
    # Crash-window ordering (Codex review C1): the notify must EXECUTE before _exec_park stamps
    # the suppression marker, so a runner crash mid-tick can only DUPLICATE a text, never lose
    # it. The executors run in list order, so this order pin is load-bearing.
    d, g = _gating(pv={})
    out = decide(dsk=d, gh_view=g)
    acts = [a["act"] for a in out]
    assert acts.index("notify") < acts.index("park")


def test_same_cause_repark_is_a_silent_retry():
    d, g = _gating(pv={})                       # answered-empty -> the "no PR exists" park verdict
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and has_notify(out) and p[0]["cause"]
    # the executor stamped the cause BEFORE its label move failed; the re-derived park is silent
    d["issues_state"]["issues"]["i5"]["park_notify_cause"] = p[0]["cause"]
    out2 = decide(dsk=d, gh_view=g)
    p2 = only(out2, "park")
    assert len(p2) == 1 and p2[0].get("retry") is True   # the park still retries (labels converge)
    assert not has_notify(out2)                          # ...but the texting happened once


def test_a_different_park_cause_notifies_again():
    # per-cause-episode, not forever: a park for a NEW cause is a new episode and texts again.
    d, g = _gating(pv={})
    d["issues_state"]["issues"]["i5"]["park_notify_cause"] = "some-earlier-cause"
    out = decide(dsk=d, gh_view=g)
    p = only(out, "park")
    assert len(p) == 1 and p[0].get("retry") is None and has_notify(out)


def test_recovery_clears_the_park_marker_so_a_later_park_notifies():
    # the gate reaches a NON-park verdict while a stale marker is set (the episode ended without
    # the label ever landing): clear it, so a LATER genuine park on this issue texts again.
    d, g = _gating(issues_extra={"i5": ist("gating", branch="sl/i5-issue-5", pr=555,
                                           park_notify_cause="pr_read_refused",
                                           park_notify_at=NOW - 60)})
    out = decide(dsk=d, gh_view=g)
    assert len(only(out, "merge")) == 1
    assert only(out, "clear_park_marker") == [{"act": "clear_park_marker", "id": "i5"}]


def test_park_label_stuck_past_bound_alerts_once():
    # incident §4: a park label move failing past the bound is ALERT-worthy — one more text,
    # not zero and not twenty. Rides the standard ALERT dedup (re-notify only on reason change).
    d = disk(issues_state={"version": 1, "issues": {
        "i5": ist("gating", park_notify_cause="checks",
                  park_notify_at=NOW - actions.PARK_LABEL_STUCK_ALERT_SECONDS - 10)}})
    out = decide(dsk=d)
    a = only(out, "alert")
    assert len(a) == 1 and "park_label_stuck:i5" in a[0]["reasons"] and has_notify(out)
    d2 = disk(issues_state=d["issues_state"],
              alert={"reasons": ["park_label_stuck:i5"], "since": NOW - 100})
    out2 = decide(dsk=d2)
    assert only(out2, "alert") == [] and not has_notify(out2)


def test_park_label_failing_under_bound_stays_quiet():
    d = disk(issues_state={"version": 1, "issues": {
        "i5": ist("gating", park_notify_cause="checks", park_notify_at=NOW - 60)}})
    out = decide(dsk=d)
    assert only(out, "alert") == [] and not has_notify(out)


def test_terminal_parked_issue_never_alerts_label_stuck():
    # the label landed (status settled terminal): the episode ended in success, no alarm.
    d = disk(issues_state={"version": 1, "issues": {
        "i5": ist("parked", park_notify_cause="checks", park_notify_at=NOW - 9999)}})
    out = decide(dsk=d)
    assert only(out, "alert") == [] and not has_notify(out)


# ---------------- bounce gets #61's park guards (issue #108) ----------------
# The bounce path (a worker's launch-time BOUNCED memo) never received #61's notify-once + stuck-
# alert guards, so a bounce whose label move keeps failing (the 2026-07-13 missing needs-owner
# label) re-emitted its notify EVERY tick — a text every ~18s, unbounded. These mirror the park
# notify-once tests above; bounce reuses the same durable handback marker (park_notify_*).

def test_bounce_notifies_once_then_silent_retries():
    dsk = disk(blocked={"i7": "BOUNCED: premise gone"},
               issues_state={"version": 1, "issues": {"i7": ist("running")}})
    out = decide(dsk=dsk)
    b = only(out, "bounce")
    assert len(b) == 1 and b[0].get("retry") is None and has_notify(out)
    # the executor stamped the cause BEFORE its label move failed; the re-derived bounce is silent
    dsk["issues_state"]["issues"]["i7"]["park_notify_cause"] = b[0]["cause"]
    out2 = decide(dsk=dsk)
    b2 = only(out2, "bounce")
    assert len(b2) == 1 and b2[0].get("retry") is True   # the bounce still retries (labels converge)
    assert not has_notify(out2)                          # ...but the texting happened once


def test_bounce_notify_precedes_the_bounce_action():
    # Crash-window ordering (mirrors park, Codex C1): notify BEFORE the bounce action, so a crash
    # mid-tick can only DUPLICATE a text, never lose it.
    dsk = disk(blocked={"i7": "BOUNCED: x"},
               issues_state={"version": 1, "issues": {"i7": ist("running")}})
    out = decide(dsk=dsk)
    acts = [a["act"] for a in out]
    assert acts.index("notify") < acts.index("bounce")


def test_bounce_label_stuck_past_bound_alerts_once():
    # a bounce label move failing past the bound is ALERT-worthy — one more text, not twenty. Rides
    # the SAME park_label_stuck machinery (reused handback marker) + the standard ALERT dedup.
    d = disk(blocked={"i7": "BOUNCED: x"},
             issues_state={"version": 1, "issues": {
                 "i7": ist("running", park_notify_cause="bounce",
                           park_notify_at=NOW - actions.PARK_LABEL_STUCK_ALERT_SECONDS - 10)}})
    out = decide(dsk=d)
    a = only(out, "alert")
    assert len(a) == 1 and "park_label_stuck:i7" in a[0]["reasons"] and has_notify(out)
    # the same-cause bounce retry underneath must stay silent (only the ALERT texts)
    assert only(out, "bounce") and only(out, "bounce")[0].get("retry") is True
    d2 = disk(blocked={"i7": "BOUNCED: x"}, issues_state=d["issues_state"],
              alert={"reasons": ["park_label_stuck:i7"], "since": NOW - 100})
    out2 = decide(dsk=d2)
    assert only(out2, "alert") == [] and not has_notify(out2)


def test_label_stuck_alert_is_suppressed_for_an_issue_being_absorbed():
    # issue #108 review P2: if a storm ran past the bound and the owner then closes the issue, the
    # same tick absorbs the close — so a "label stuck" alert text as they drop it is pure noise.
    # A positively-closed (fresh view) issue raises no stuck alert; it just absorbs.
    d = disk(blocked={"i7": "BOUNCED: x"},
             issues_state={"version": 1, "issues": {
                 "i7": ist("running", park_notify_cause="bounce",
                           park_notify_at=NOW - actions.PARK_LABEL_STUCK_ALERT_SECONDS - 10)}})
    out = decide(dsk=d, gh_view=ghv(closed_nums={7}))
    assert only(out, "absorb_close") == [{"act": "absorb_close", "id": "i7", "num": 7}]
    assert not any("park_label_stuck" in r for a in only(out, "alert") for r in a["reasons"])
    assert not has_notify(out)                          # no stuck-label text on the drop tick


def test_bounce_label_failing_under_bound_stays_quiet():
    d = disk(blocked={"i7": "BOUNCED: x"},
             issues_state={"version": 1, "issues": {
                 "i7": ist("running", park_notify_cause="bounce", park_notify_at=NOW - 60)}})
    out = decide(dsk=d)
    assert only(out, "alert") == [] and not has_notify(out)


# ---------------- absorb external closes for bounced/parked issues (issue #108) ----------------
# William closing the issue on GitHub (the dashboard's Drop) while the loop is bouncing/parking it
# is his answer: absorb the close, settle terminal, stand down. Positive-proof only (a fresh view
# whose closed set names the issue); scoped to bounce/park episodes so a normal merge-close or a
# plain running build is untouched.

def test_external_close_while_bouncing_absorbs_no_bounce_no_notify():
    dsk = disk(blocked={"i7": "BOUNCED: premise gone"},
               issues_state={"version": 1, "issues": {
                   "i7": ist("running", park_notify_cause="bounce", park_notify_at=NOW - 30)}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={7}))
    assert only(out, "bounce") == []
    assert only(out, "absorb_close") == [{"act": "absorb_close", "id": "i7", "num": 7}]
    assert not has_notify(out)


def test_external_close_while_parking_absorbs_no_park_no_notify():
    dsk = disk(reports={"i5": GOOD_REPORT},
               issues_state={"version": 1, "issues": {
                   "i5": ist("gating", park_notify_cause="checks", park_notify_at=NOW - 30)}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={5}))
    assert only(out, "park") == []
    assert only(out, "absorb_close") == [{"act": "absorb_close", "id": "i5", "num": 5}]
    assert not has_notify(out)


def test_external_close_while_terminally_bounced_absorbs_to_conclude():
    # post-storm: status already terminal 'bounced', issue closed on GitHub -> absorb so the flight
    # concludes (no lingering owner-decision presence). One absorb, no notify.
    dsk = disk(issues_state={"version": 1, "issues": {"i7": ist("bounced")}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={7}))
    assert only(out, "absorb_close") == [{"act": "absorb_close", "id": "i7", "num": 7}]
    assert not has_notify(out)


def test_external_close_while_terminally_parked_absorbs():
    for status in ("parked", "needs_william"):
        dsk = disk(issues_state={"version": 1, "issues": {"i5": ist(status)}})
        out = decide(dsk=dsk, gh_view=ghv(closed_nums={5}))
        assert only(out, "absorb_close") == [{"act": "absorb_close", "id": "i5", "num": 5}], status


def test_external_close_absorb_requires_a_fresh_view():
    # a STALE gh view never absorbs (positive-proof discipline, #48): a closed_nums from a stale
    # read is not acted on — the close must be freshly proven.
    dsk = disk(issues_state={"version": 1, "issues": {"i7": ist("bounced")}})
    out = decide(dsk=dsk, gh_view=ghv(stale=True, closed_nums={7}))
    assert only(out, "absorb_close") == []


def test_merged_issue_close_is_not_absorbed():
    # a normally-merged build issue is closed by Closes #N; it must NOT be re-absorbed as an
    # external close (its status is 'merged', not a bounce/park episode).
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("merged")}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={5}))
    assert only(out, "absorb_close") == []


def test_merged_issue_with_a_stale_park_marker_is_not_re_absorbed():
    # idempotency + crash-window safety: a 'merged' issue that still carries a stale park_notify_cause
    # (a crash between the merge and clear_park_marker executors) must NOT absorb — 'merged' is DONE.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i5": ist("merged", park_notify_cause="checks", park_notify_at=NOW - 9999)}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={5}))
    assert only(out, "absorb_close") == []


def test_running_build_close_is_out_of_scope_not_absorbed():
    # closing a plain running build (no bounce/park episode) is out of THIS issue's scope: never
    # absorbed here (the fail-to-owner posture on the build path is unchanged).
    dsk = disk(issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(dsk=dsk, gh_view=ghv(closed_nums={5}))
    assert only(out, "absorb_close") == []


def test_merged_pr_state_is_absorbed_not_wedged():
    # Crash window (Codex round-1 C2): gh merged the PR, the runner died before stamping
    # status=merged. Restart must ABSORB the fact — settle local state + labels — not sit in
    # gate-wait forever with a stuck in-progress label.
    d, g = _gating(pv=pr_view(state="MERGED"))
    out = decide(dsk=d, gh_view=g)
    assert only(out, "merge") == [] and only(out, "park") == []
    assert only(out, "absorb_merged") == [{"act": "absorb_merged", "id": "i5", "num": 5}]


# =============== in-flight branch->PR reconcile (issue #155) ===============
# Until #155 the ONLY path that consulted a PR's state was the finished/gating branch above, so a
# lane whose PR concluded WHILE IT WAS STILL BUILDING never learned of it. i328: the PR was merged
# out-of-band, which closed the issue ("Closes #328"), which dropped it from both open-issue lists,
# which kept the poll's want-set from ever looking it up — pr: null forever, and the lane held its
# slot for two hours. These pin the reconcile: act on a POSITIVE merged/closed answer, and on
# nothing else (a refused or answered-empty lookup means "keep building", never a park).

def _inflight(pv=None, status="running", **ist_over):
    """A lane IN FLIGHT — building, no report on disk. `pv=None` leaves the PR view with NO entry
    for it (exactly how the poll OMITS a refused lookup); `pv={}` is GitHub's answered 'no PR'."""
    d = disk(issues_state={"version": 1, "issues": {
        "i5": ist(status, branch="sl/i5-issue-5", **ist_over)}})
    return d, ghv(prs={} if pv is None else {"i5": pv})


def test_inflight_lane_absorbs_an_out_of_band_merge():
    # The i328 shape: no report, no parsed issue (the merge auto-closed it), status still 'running'.
    # A MERGED PR on this lane's active branch is the truth — settle it and free the lane.
    d, g = _inflight(pv=pr_view(state="MERGED"), pr=555)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == [{"act": "absorb_merged", "id": "i5", "num": 5}]
    assert only(out, "park") == []


def test_an_out_of_band_merge_absorbs_even_if_the_lane_never_saw_the_pr_open():
    # MERGED is deliberately NOT episode-scoped the way CLOSED is (below). A merge is a LANDED fact
    # about the branch — the work is in the mainline no matter which episode opened the PR — so it
    # is absorbed whether or not this lane ever recorded the number. Load-bearing for the wake-gap:
    # the runner can sleep through an entire open->merge (the laptop shuts) and must still settle
    # rather than stall, which is the whole point of i328.
    d, g = _inflight(pv=pr_view(state="MERGED"), pr=None)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == [{"act": "absorb_merged", "id": "i5", "num": 5}]
    assert only(out, "park") == []


def test_inflight_lane_records_its_pr_number_as_soon_as_one_exists():
    # DoD: "records the number as soon as a PR exists". i328's report was that the runner "still
    # carried pr: null" — this is that field. It also makes the CLOSED hand-back episode-scoped.
    d, g = _inflight(pv=pr_view(state="OPEN"), pr=None)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "record_pr") == [{"act": "record_pr", "id": "i5", "pr": 555}]


def test_inflight_lane_does_not_re_record_a_pr_it_already_knows():
    d, g = _inflight(pv=pr_view(state="OPEN"), pr=555)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "record_pr") == []


def test_inflight_lane_hands_back_the_close_of_the_pr_it_opened():
    # A CLOSED PR is out-of-band by construction: the runner never closes its own (regenerate
    # supersedes and leaves it OPEN on a preserved branch, stamping the NEW branch first; the
    # janitor's close path vetoes every claimed lane). Why it was closed is unknowable here and
    # both guesses are wrong — rebuilding loops against the call, merging is not ours — so hand it
    # back once. Matches the gate's own long-standing verdict for a closed PR (gate.py).
    d, g = _inflight(pv=pr_view(state="CLOSED"), pr=555)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == []
    p = only(out, "park")
    assert len(p) == 1 and p[0]["needs_william"] is True and p[0]["cause"] == "pr_closed"


def test_a_stale_closed_pr_from_a_previous_episode_never_parks_a_relaunched_lane():
    """THE regression the fresh-agent review caught, and the reason the CLOSED hand-back is scoped
    to the PR this episode actually opened.

    A park hands the issue to the owner; re-approving clears `pr` but KEEPS the branch stamp, and
    the relaunch builds on that same branch. A CLOSED PR does NOT stop a new PR on the same head
    (GitHub refuses only a second OPEN one) — so pre-#155 the worker simply opened a fresh PR and
    the newest-first lookup returned it. Recovery worked. An unscoped CLOSED park breaks that: it
    fires a tick after launch, BEFORE the worker can push, so the stale closed PR is the only
    answer and the lane re-parks forever — an inescapable trap, with the owner's only remedy being
    the one thing the memo told them to do. `pr: None` means this episode owns no PR yet: ignore
    the ghost and let the worker open its own."""
    d, g = _inflight(pv=pr_view(state="CLOSED"), pr=None)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == []                    # no trap: the lane keeps building...
    assert only(out, "record_pr") == []               # ...and a closed ghost is never recorded


def test_inflight_lane_with_an_open_pr_just_keeps_building():
    d, g = _inflight(pv=pr_view(state="OPEN"), pr=555)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == [] and only(out, "park") == []


def test_inflight_reconcile_never_parks_on_a_refused_lookup():
    # The refused!=empty discipline (#61) on the reconcile path: a lookup the poll OMITTED must
    # read as "unknown, keep building" — never as "no PR exists", and never as a park.
    d, g = _inflight(pv=None)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == [] and only(out, "absorb_merged") == []


def test_inflight_reconcile_never_parks_when_github_answers_no_pr():
    # answered-empty mid-build is the NORMAL case (the worker hasn't pushed yet) — not a fault.
    d, g = _inflight(pv={})
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == [] and only(out, "absorb_merged") == []


def test_inflight_reconcile_ignores_a_stale_view():
    # A stale/unreachable view degrades to the existing wait (the issue's boundary), never acts.
    d, g = _inflight(pv=pr_view(state="MERGED"), pr=555)
    g["stale"] = True
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == [] and only(out, "park") == []


def test_inflight_reconcile_ignores_a_superseded_pr():
    # Defense in depth: a `superseded` PR is dead history the runner itself retired (the orphan
    # sweep applies the same rule). It can only reach the ACTIVE branch through a half-executed
    # regenerate — act on it and we'd park the lane the runner just rebuilt.
    d, g = _inflight(pv=pr_view(state="CLOSED", labels=["superseded"]), pr=555)
    out = decide(dsk=d, gh_view=g)
    assert only(out, "park") == [] and only(out, "absorb_merged") == []


def test_absorbed_merge_wins_over_the_question_lifecycle():
    # The stall itself: i328's lane sat in the blocked/question machinery while its PR was ALREADY
    # merged. The merged fact must win over every lifecycle the lane would otherwise keep spinning.
    d, g = _inflight(pv=pr_view(state="MERGED"), status="blocked", pr=555)
    d["blocked"] = {"i5": "which approach should I take?"}
    out = decide(dsk=d, gh_view=g)
    assert only(out, "absorb_merged") == [{"act": "absorb_merged", "id": "i5", "num": 5}]
    assert only(out, "post_question") == []


def test_inflight_reconcile_leaves_a_not_yet_launched_issue_alone():
    # status None/'ready' is not in flight: no branch, nothing to reconcile.
    d = disk(issues_state={"version": 1, "issues": {"i5": ist(None)}})
    out = decide(dsk=d, gh_view=ghv(prs={"i5": pr_view(state="MERGED")}))
    assert only(out, "absorb_merged") == [] and only(out, "park") == []


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


# =============== the one launch-eligibility gate (issue #150 / D8) ===============
# The 07-15 marathon's D8: only fresh phase-E launches routed through issues.eligible. The
# liveness relaunch tier, the restart orphan resume and the conflict-resolution relaunch all
# started a session WITHOUT re-checking it, so a recovery could launch a worker straight past an
# open `blocked-by` — contained only by the worker's own step-0 reconcile bounce. These pin the
# rule from the other side: EVERY path that starts or restarts a session asks the SAME predicate
# first, and a refusal HOLDS legibly instead of launching silently.

def _blocked_exited(blocked_by=(41,), **ist_over):
    """An in-flight issue whose session died, and whose `blocked-by` is NOT closed."""
    p5 = parsed(5, labels=("in-progress", "type:build"), blocked_by=list(blocked_by))
    d = disk(exited={"i5": "x rc=1"},
             issues_state={"version": 1, "issues": {"i5": ist("running", **ist_over)}})
    return p5, d


def test_exited_recovery_never_relaunches_past_an_open_blocker():
    p5, d = _blocked_exited()
    out = decide(parsed_issues=[p5], dsk=d, gh_view=ghv(closed_nums=set()))
    assert only(out, "recover") == []                  # blocker 41 still open -> no session
    h = only(out, "launch_hold")
    assert len(h) == 1 and h[0]["id"] == "i5" and "41" in h[0]["reason"]   # held, and it says why
    assert only(out, "park") == [] and not has_notify(out)   # a hold is not a park (retry cap intact)


def test_exited_recovery_resumes_the_moment_the_blocker_closes():
    # The hold is a WAIT, not a verdict: the same inputs with the dependency closed relaunch.
    p5, d = _blocked_exited()
    out = decide(parsed_issues=[p5], dsk=d, gh_view=ghv(closed_nums={41}))
    r = only(out, "recover")
    assert len(r) == 1 and r[0]["tier"] == "exited"
    assert only(out, "launch_hold") == []


def test_exited_recovery_refuses_an_unapproved_or_mislabeled_issue():
    # The recovery path owes every condition the fresh path owes, not just blocked-by: an issue
    # William has since un-approved (no `agent-ready`, no `in-progress`), and one whose control
    # labels now conflict, both hold rather than relaunch.
    for labels in (("type:build",),                            # approval withdrawn
                   ("in-progress", "type:build", "type:investigate")):   # invalid type
        p5 = parsed(5, labels=labels)
        d = disk(exited={"i5": "x rc=1"},
                 issues_state={"version": 1, "issues": {"i5": ist("running")}})
        out = decide(parsed_issues=[p5], dsk=d)
        assert only(out, "recover") == [], labels
        assert len(only(out, "launch_hold")) == 1, labels


def test_exited_recovery_holds_when_the_issue_is_absent_from_the_github_view():
    # No parsed issue = eligibility is UNREADABLE, so none of the five conditions can be affirmed.
    # Fail closed exactly as _exec_launch already does for a fresh launch ("skipped: issue not in
    # the current GitHub view") rather than relaunching a session blind.
    d = disk(exited={"i5": "x rc=1"},
             issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(dsk=d)
    assert only(out, "recover") == []
    assert len(only(out, "launch_hold")) == 1


def test_orphan_resume_never_relaunches_past_an_open_blocker():
    # The restart rebuild resumes an in-progress issue's session on its open PR's branch. It went
    # straight to `launch` with no gate at all — not eligibility, and not even usage.
    p8 = parsed(8, labels=("in-progress", "type:build"), blocked_by=[41])
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")}, closed_nums=set())
    out = decide(parsed_issues=[p8], gh_view=g)
    assert only(out, "launch") == []
    assert only(out, "reclaim") == []                  # held in place: the orphan resume is what
                                                       # re-attaches the PR branch, so don't requeue
    h = only(out, "launch_hold")
    assert len(h) == 1 and "41" in h[0]["reason"]


def test_orphan_resume_proceeds_once_the_blocker_closes():
    p8 = parsed(8, labels=("in-progress", "type:build"), blocked_by=[41])
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")}, closed_nums={41})
    out = decide(parsed_issues=[p8], gh_view=g)
    launches = only(out, "launch")
    assert len(launches) == 1 and launches[0]["orphan"] is True
    assert only(out, "launch_hold") == []


def test_resolve_conflict_relaunch_never_starts_past_an_open_blocker():
    p5 = parsed(5, labels=("in-progress", "type:build"), blocked_by=[41])
    d, g = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1)
    out = decide(parsed_issues=[p5], dsk=d, gh_view=dict(g, closed_nums=set()))
    assert only(out, "resolve_conflict") == []
    assert len(only(out, "launch_hold")) == 1


def test_usage_fails_closed_identically_on_every_restart_path():
    # DoD: no drift between fresh and recovery. Fresh launches have always failed closed on a
    # missing/unhealthy meter; the orphan resume and the conflict relaunch never asked at all.
    dead_meter = {"auth_status": "expired", "five_hour_pct": None, "seven_day_pct": None,
                  "last_ok_at": NOW - 60, "first_attempt_at": NOW - 120}
    p8 = parsed(8, labels=("in-progress", "type:build"))
    g = ghv(prs={"i8": pr_view(num=88, branch="sl/i8-issue-8")})
    assert only(decide(parsed_issues=[p8], usage=dead_meter, gh_view=g), "launch") == []

    p5 = parsed(5, labels=("in-progress", "type:build"))
    d, gc = _gating(pv=pr_view(mergeable="CONFLICTING", labels=[{"name": "preserve"}]))
    d["issues_state"]["issues"]["i5"].update(update_result="conflict", update_head_oid=HEAD1)
    assert only(decide(parsed_issues=[p5], usage=dead_meter, dsk=d, gh_view=gc),
                "resolve_conflict") == []


def test_a_continuous_launch_hold_journals_once_but_re_journals_a_new_reason():
    # Bounded like the wildcard hold (#36): a 15s tick must not re-journal the same standing hold
    # forever — but the reason on the board must never go stale either, so a CHANGED cause speaks.
    p5, d = _blocked_exited()
    d["issues_state"]["issues"]["i5"]["launch_hold_reason"] = \
        only(decide(parsed_issues=[p5], dsk=d, gh_view=ghv(closed_nums=set())),
             "launch_hold")[0]["reason"]
    assert only(decide(parsed_issues=[p5], dsk=d, gh_view=ghv(closed_nums=set())),
                "launch_hold") == []                   # same standing hold: silent
    p5b = parsed(5, labels=("type:build",), blocked_by=[41])   # approval withdrawn: a NEW cause
    assert len(only(decide(parsed_issues=[p5b], dsk=d, gh_view=ghv(closed_nums=set())),
                    "launch_hold")) == 1


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
    assert len(only(out, "post_question")) == 1


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


# ------- issue #52: the dev freeze/unfreeze reads the DEV-required set, not the PR set -------
# A required check that gates PR merges but NEVER reports on the dev branch (e.g. a ship status
# stamped on PR head commits only, which the post-squash-merge dev HEAD never receives) must not
# strand a mainline freeze forever. Split config lets the dev set EXCLUDE it, so once the checks
# that DO report on dev green, the freeze lifts.
SPLIT_CFG = {"pr": ["ci", "ship"], "dev": ["ci"]}   # ship is PR-only, absent from the dev set


def test_pr_only_check_absent_from_dev_does_not_strand_freeze():
    # dev HEAD reports ci green; `ship` never reports on dev at all. The dev-required set is {ci},
    # which is green -> the freeze lifts. (Under the old flat list [ci, ship] this read pending
    # forever because ship is missing from dev — the exact strand-forever bug #52 fixes.)
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out = decide(config=cfg(required_checks=SPLIT_CFG), dsk=d,
                 gh_view=ghv(dev_checks=list(GREEN)))     # GREEN == [ci SUCCESS]; ship absent
    assert len(only(out, "unfreeze")) == 1


def test_flat_list_with_a_pr_only_check_still_strands_freeze():
    # Contrast (documents the bug the split fixes): the SAME dev view under a flat list that still
    # includes the PR-only `ship` reads pending forever -> stranded. The remedy is the config split.
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out = decide(config=cfg(required_checks=["ci", "ship"]), dsk=d,
                 gh_view=ghv(dev_checks=list(GREEN)))
    assert only(out, "unfreeze") == []


def test_dev_freeze_evaluates_only_the_dev_required_set():
    # A red `ship` on dev must NOT freeze (ship is not dev-required); a red `ci` MUST freeze.
    ship_red = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "ship", "status": "COMPLETED", "conclusion": "FAILURE"}]
    out = decide(config=cfg(required_checks=SPLIT_CFG), gh_view=ghv(dev_checks=ship_red))
    assert only(out, "freeze") == [] and only(out, "file_fix_issue") == []
    out2 = decide(config=cfg(required_checks=SPLIT_CFG), gh_view=ghv(dev_checks=list(RED)))
    assert len(only(out2, "freeze")) == 1 and has_notify(out2)


def test_empty_dev_required_set_idles_the_freeze_mechanism():
    # A repo whose CI runs on PRs only sets an empty dev set. The freeze mechanism then IDLES: even a
    # red dev check never freezes / files a fix, and any existing freeze lifts (empty set == green).
    empty_dev = {"pr": ["ci"], "dev": []}
    out = decide(config=cfg(required_checks=empty_dev), gh_view=ghv(dev_checks=list(RED)))
    assert only(out, "freeze") == [] and only(out, "file_fix_issue") == []
    d = disk(frozen={"reason": "dev red", "fingerprint": "f", "since": NOW - 100})
    out2 = decide(config=cfg(required_checks=empty_dev), dsk=d, gh_view=ghv(dev_checks=list(RED)))
    assert len(only(out2, "unfreeze")) == 1


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


def test_dark_meter_within_grace_does_not_alert_yet():
    # The grace's silent window (issue #46): a dark meter still fails closed AND stays quiet until
    # the grace expires — the alert fires only when fail-open engages, so we neither cry wolf on a
    # blip nor launch-and-alert prematurely. (Past-grace alert + fail-open is covered above.)
    within = {**usage_ok(), "last_ok_at": NOW - 600}   # 10 min: stale, but < 30-min grace
    out = decide(usage=within)
    assert only(out, "alert") == [] and only(out, "fail_open") == []


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

    # A corrupt questions_asked (a bool is an int subclass but never a real count) is unreadable —
    # fail closed to a park, never re-open an unbounded question round-trip (#163).
    d = disk(blocked={"i7": "q?"},
             issues_state={"version": 1, "issues": {"i7": ist("blocked", questions_asked=True)}})
    out = decide(dsk=d)
    assert only(out, "post_question") == []
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


# ------------- wrong-typed / unhashable status: sibling audit of detect_events (issue #95) -------------
# Every `status in <SET>` reached in the tick must be hash-safe. A DICT-shaped issue whose STATUS
# VALUE is unhashable ([]/{}) slips past the existing `isinstance(ist, dict)` guards, so an unguarded
# membership test raises `unhashable type` and wedges the tick before its heartbeat stamp.

def test_lane_state_from_skips_an_unhashable_status():
    st = {"version": 1, "issues": {
        "i1": ist("running", declared_touches=["api"], type="build"),
        "i2": ist([]), "i3": ist({})}}               # dict ist, unhashable status value
    lanes = actions.lane_state_from(st)              # must NOT raise
    assert [x["id"] for x in lanes] == ["i1"]        # corrupt entries occupy no lane (fail closed)


def test_territory_claims_from_skips_an_unhashable_status():
    st = {"version": 1, "issues": {
        "i1": ist("gating", declared_touches=["api"], type="build"),
        "i2": ist([]), "i3": ist({})}}
    claims = actions.territory_claims_from(st)       # must NOT raise
    assert [x["id"] for x in claims] == ["i1"]       # corrupt entries make no territory claim (fail closed)


def test_decide_survives_an_unhashable_status_and_never_launches_it():
    # The whole tick brain must not raise on a corrupt issues.json, and a corrupt-status issue is
    # NEVER launched (fail closed for launches) even carrying a fresh agent-ready label — a corrupt
    # status is not a well-typed RELAUNCHABLE one.
    st = {"version": 1, "issues": {"i5": ist([]), "i7": ist({})}}
    out = decide(parsed_issues=[parsed(5), parsed(7)], dsk=disk(issues_state=st))   # must NOT raise
    assert only(out, "launch") == []


def test_corrupt_status_finished_issue_is_never_gated_or_merged():
    # Codex cross-review (round 1): a wrong-typed status is UNREADABLE lifecycle state, so hash-safety
    # is not enough — the loop must also take NO consequential action on it. A corrupt entry with a
    # finished report AND a clean mergeable PR otherwise falls through decide's non-membership branches
    # as if it were cold state and emits gate -> MERGE (a merge off corrupted state). Fail closed: skip.
    d, g = _gating(status=[])
    out = decide(dsk=d, gh_view=g)
    assert only(out, "gate") == [] and only(out, "merge") == []


def test_corrupt_status_in_progress_issue_is_never_orphan_launched():
    # Codex cross-review (round 1): the same fall-through emits an ORPHAN launch for a corrupt entry
    # carrying a GitHub in-progress label + an open PR — a launch off corrupted lifecycle state, which
    # the fail-closed contract forbids. Skipping the corrupt entry entirely blocks it.
    st = {"version": 1, "issues": {"i5": ist([], branch="sl/i5-issue-5")}}
    g = ghv(prs={"i5": pr_view(branch="sl/i5-issue-5", state="OPEN")})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))],
                 dsk=disk(issues_state=st), gh_view=g)
    assert only(out, "launch") == []


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
    # A no-touches repo is a touches_required:false repo (empty touches are allowed there); the
    # released parked claim must not freeze the sole lane.
    dsk = disk(issues_state={"version": 1, "issues": {
        "i9": ist("parked", declared_touches=[], type="build")}})
    out = decide(config=cfg(lanes=1, affinity="hard", touches_required=False),
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


# ============= reserved investigation lanes end-to-end through decide() (issue #63) =============
# The lane-state accounting truth site: decide passes the type-carrying lane_state straight into the
# reserved-pool scheduler, so the owner's case works with a real config object, not just a unit test.

def test_pooled_lanes_launch_investigation_while_build_waits():
    # THE OWNER'S CASE: the sole build lane is occupied by a running build; a second approved build
    # WAITS while an approved investigation launches immediately into the reserved lane.
    lane = [{"id": "i1", "type": "build", "touches": ["frontend"]}]
    out = decide(config=cfg(lanes={"build": 1, "investigate": 1}),
                 parsed_issues=[parsed(2, touches=("api",)),
                                parsed(3, labels=("agent-ready", "type:investigate"),
                                       touches=("frontend",))],
                 lane_state=lane)
    assert [a["id"] for a in only(out, "launch")] == ["i3"]


def test_pooled_lanes_reserved_investigation_lane_not_taken_by_build():
    # RESERVATION: the build pool is full and the investigation lane idle with nothing to run; the
    # queued build does NOT borrow the reserved lane.
    lane = [{"id": "i1", "type": "build", "touches": ["frontend"]}]
    out = decide(config=cfg(lanes={"build": 1, "investigate": 1}),
                 parsed_issues=[parsed(2, touches=("api",))],
                 lane_state=lane)
    assert only(out, "launch") == []


# =========================== issue #151: honest session-state sensing ===========================

def test_decide_alerts_on_a_logged_out_session():
    """i336: a session whose auth died in-process typed into for 94 minutes. Once the runner SENSES
    logged_out, decide must route it to the owner — this is the DoD's 'alerts / routes to an owner
    decision'. It lives here, not in the executor, because decide owns the ALERT file and rebuilds
    it from disk every tick: an executor-written alert would be cleared one tick later."""
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("running", sensed_state="logged_out")}})
    out = decide(dsk=d)
    a = only(out, "alert")
    assert len(a) == 1 and "session_logged_out:i5" in a[0]["reasons"]
    assert has_notify(out)


def test_logged_out_alert_says_what_the_owner_must_actually_do():
    """An alert whose body is a bare reason code costs the owner a diagnosis. The known cause here
    is specific and the fix is manual (the forensic note: /login inside the wedged window never
    stuck — only closing it worked), so the text must say so."""
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("running", sensed_state="logged_out")}})
    out = decide(dsk=d)
    body = [a for a in out if a["act"] == "notify"][0]["body"]
    assert "i5" in body and "login" in body.lower()
    assert "session_logged_out:i5" != body            # not just the raw code echoed back


def test_logged_out_alert_clears_once_the_session_is_sensed_healthy_again():
    """Same durable-marker discipline as every other reason: the alert stands while the condition
    is on disk and auto-clears when it is gone — never a sticky alarm needing a human to dismiss."""
    d = disk(alert={"reasons": ["session_logged_out:i5"]},
             issues_state={"version": 1, "issues": {"i5": ist("running", sensed_state=None)}})
    out = decide(dsk=d)
    assert len(only(out, "clear_alert")) == 1


def test_a_session_at_its_own_dialog_does_not_alert():
    """at_dialog is NOT an alarm: a session asking something in-window is live and working. Alerting
    on it would just re-teach the loop to cry wolf about healthy lanes (the i280 shape, one level up)."""
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("running", sensed_state="at_dialog")}})
    out = decide(dsk=d)
    assert only(out, "alert") == []


def test_a_logged_out_session_is_never_actually_typed_into():
    """The DoD's hard 'NEVER nudges it', located honestly.

    An earlier draft of this test asserted decide emits no `recover` for a logged-out lane. That
    read well and was wrong: `recover` is the only thing that re-reads the screen, so suppressing
    it stranded sensed_state forever (fresh-review P0, see the livelock test above). The recover is
    emitted — and delivers NOTHING, because nudge-pane classifies before it types and refuses with
    rc=5. That refusal is where 'never nudges' is truly enforced, at the only layer that can see
    the screen; asserting it here would only be asserting decide's good intentions.

    What decide owes this lane is the other half: alert the owner, and never park it."""
    d = disk(issues_state={"version": 1, "issues": {"i5": ist("running", sensed_state="logged_out")}})
    out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=d)
    assert any("session_logged_out:i5" in a["reasons"] for a in only(out, "alert"))
    assert [a for a in only(out, "park") if a["id"] == "i5"] == []


def test_a_session_at_a_dialog_is_not_walked_toward_a_park():
    """i280: the nudge ladder exhausted into a false park of a live, working lane. A lane sensed at
    its own dialog must not be PARKED — while still being re-sensed each cycle, which is what lets
    the reading expire once the dialog is answered (see the livelock test below)."""
    d = disk(issues_state={"version": 1,
                           "issues": {"i5": ist("running", sensed_state="at_dialog", retries=99)}})
    out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=d)
    assert [a for a in only(out, "park") if a["id"] == "i5"] == []
    # fence: retries=99 is far past the cap, so this exact lane WITHOUT the sensed state parks.
    # That is the false park i280 actually suffered — the assert above must be what prevents it.
    doomed = disk(issues_state={"version": 1, "issues": {"i5": ist("running", retries=99)}})
    assert len(only(decide(events=[{"type": "frozen", "id": "i5"}], dsk=doomed), "park")) == 1


def test_a_sensed_lane_is_still_re_sensed_so_the_state_cannot_livelock():
    """FRESH-REVIEW P0. sensed_state's ONLY writer is _record_sensed, which runs only from
    _exec_recover, which runs only when decide emits `recover`. So suppressing the recover emit
    made the field impossible to clear: the lane went silent FOREVER — no recover, no park, no
    alert — and `status` stays 'frozen' durably, which keeps the branch live to re-suppress every
    tick. A stale at_dialog would outlive the dialog, survive a relaunch, and mute a genuinely
    stuck lane for good.

    So the recover MUST keep firing: it is what re-reads the screen. It delivers nothing — nudge-
    pane refuses to type at logged_out/at_dialog (rc 5/6) — so this is a re-SENSE, not a nudge.
    What gets suppressed is the ESCALATION (see the park tests below)."""
    for sensed in ("at_dialog", "logged_out"):
        d = disk(issues_state={"version": 1,
                               "issues": {"i5": ist("frozen", sensed_state=sensed)}})
        out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=d)
        r = [a for a in only(out, "recover") if a["id"] == "i5"]
        assert len(r) == 1 and r[0]["tier"] == "frozen", f"{sensed} must still be re-sensed"


def test_a_sensed_lane_past_the_retry_cap_is_not_parked():
    """The i280 false park, precisely: retries far past the cap is what parks a frozen lane, and a
    lane at its own dialog is ALIVE. Suppress the park — not the sensing."""
    for sensed in ("at_dialog", "logged_out"):
        d = disk(issues_state={"version": 1,
                               "issues": {"i5": ist("frozen", sensed_state=sensed, retries=99)}})
        out = decide(events=[{"type": "frozen", "id": "i5"}], dsk=d)
        assert [a for a in only(out, "park") if a["id"] == "i5"] == [], f"{sensed} must not park"


def test_a_logged_out_alert_stops_once_the_lane_is_terminal():
    """FRESH-REVIEW P1. `session_logged_out` sat without the TERMINAL_STATUSES guard its immediate
    neighbour park_label_stuck carries, so a merged/parked lane wearing a stale sensed_state would
    alert forever and poison the alert dedup for every other reason."""
    for status in ("merged", "parked", "needs_william", "bounced"):
        d = disk(issues_state={"version": 1,
                               "issues": {"i5": ist(status, sensed_state="logged_out")}})
        out = decide(dsk=d)
        assert only(out, "alert") == [], f"a {status} lane must not alert"


def test_a_lane_stuck_at_its_own_dialog_eventually_reaches_the_owner():
    """FRESH-REVIEW P1 — the hole the first fix opened. 'It is waiting on a human' was wrong: there
    is NO human at that pane. The loop's channel for 'worker needs input' is state/blocked/<id> ->
    a durable GitHub question comment (the worker exits, the owner answers); an in-window
    AskUserQuestion is OFF that channel, so by construction nobody will ever answer it.

    So at_dialog-forever is not a live lane, it is a stalled one the loop cannot serve — and
    because 'frozen' is an INFLIGHT status, refusing to park it leaks the lane's slot silently and
    forever. Pre-#151 it parked: the memo named the wrong cause, but the owner learned and the slot
    came back. Trading a false park for a silent leak is not a fix.

    It gets logged_out's shape instead: an ALERT, bounded by persistence so a normal short dialog
    stays quiet. Parking is still refused — the lane IS alive — but it can no longer be silent."""
    ist_new = ist("frozen", sensed_state="at_dialog", sensed_since=NOW - 60)
    out = decide(events=[{"type": "frozen", "id": "i5"}],
                 dsk=disk(issues_state={"version": 1, "issues": {"i5": ist_new}}))
    assert only(out, "alert") == [], "a dialog that just opened must stay quiet"

    stuck = ist("frozen", sensed_state="at_dialog",
                sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)
    out = decide(events=[{"type": "frozen", "id": "i5"}],
                 dsk=disk(issues_state={"version": 1, "issues": {"i5": stuck}}))
    a = only(out, "alert")
    assert len(a) == 1 and "session_at_dialog:i5" in a[0]["reasons"]
    assert has_notify(out)
    assert [x for x in only(out, "park") if x["id"] == "i5"] == [], "alert, but still never park"


def test_the_stuck_dialog_alert_tells_the_owner_where_to_look():
    stuck = ist("frozen", sensed_state="at_dialog",
                sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)
    out = decide(events=[{"type": "frozen", "id": "i5"}],
                 dsk=disk(issues_state={"version": 1, "issues": {"i5": stuck}}))
    body = [x for x in out if x["act"] == "notify"][0]["body"]
    assert "i5" in body and "session_at_dialog:i5" != body


def test_a_dialog_alert_needs_a_real_stamp_and_a_live_lane():
    """A missing/corrupt stamp must not alert (it would fire on every dialog instantly), and a
    terminal lane's last reading is history — same guard as session_logged_out."""
    for since in (None, "garbage", NOW + 99999):
        d = disk(issues_state={"version": 1,
                               "issues": {"i5": ist("frozen", sensed_state="at_dialog",
                                                    sensed_since=since)}})
        assert only(decide(events=[{"type": "frozen", "id": "i5"}], dsk=d), "alert") == [], since
    for status in ("merged", "parked"):
        d = disk(issues_state={"version": 1,
                               "issues": {"i5": ist(status, sensed_state="at_dialog",
                                                    sensed_since=NOW - 99999)}})
        assert only(decide(dsk=d), "alert") == [], status


# ============ night-batching: routine owner decisions don't page at night (issue #164) ============
# The founding standard: nobody answers a 3am page and a park is a safe state. So a routine
# owner-DECISION hand-back (park / bounce / durable question) is BATCHED to the morning report
# during quiet hours instead of pushed; only SYSTEMIC-STOP alerts (runner/auth dead, whole queue
# stalled) keep paging. Quiet hours default on (21:00–08:00); an explicit `null` disables them.


def _recheck_park_dsk(local_hhmm):
    # recheck_failed is the cleanest routine needs-owner park: it fires before any gate re-run and
    # needs no GitHub/PR view — exactly the "I can't proceed without your decision" class this issue
    # is about. (needs_william=True, cause="recheck".)
    return disk(local_hhmm=local_hhmm,
                issues_state={"version": 1, "issues": {"i5": ist("running", recheck_failed=True)}})


def test_routine_needs_owner_park_does_not_push_at_night():
    out = decide(dsk=_recheck_park_dsk("23:30"))
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5" and parks[0]["needs_william"] is True
    assert not has_notify(out), "a routine owner-decision park must NOT page at night"


def test_the_same_park_during_the_day_still_pushes_immediately():
    out = decide(dsk=_recheck_park_dsk("12:00"))
    assert len(only(out, "park")) == 1
    assert has_notify(out), "during the day the owner is awake — the park pages promptly"


def test_park_early_morning_boundary_pushes_after_quiet_hours_end():
    # end is EXCLUSIVE: 08:00 is already daytime, so the park pages (and appears in the 08:45 report).
    assert has_notify(decide(dsk=_recheck_park_dsk("08:00")))
    assert not has_notify(decide(dsk=_recheck_park_dsk("07:59")))


def test_bounce_does_not_push_at_night_but_still_hands_back():
    dsk = disk(local_hhmm="02:00",
               blocked={"i7": "BOUNCED: already fixed on dev; propose closing"},
               issues_state={"version": 1, "issues": {"i7": ist("running")}})
    out = decide(dsk=dsk)
    assert len(only(out, "bounce")) == 1, "the bounce (hand-back) still fires — only the page is held"
    assert not has_notify(out)


def test_bounce_during_the_day_pushes():
    dsk = disk(local_hhmm="14:00",
               blocked={"i7": "BOUNCED: already fixed on dev; propose closing"},
               issues_state={"version": 1, "issues": {"i7": ist("running")}})
    out = decide(dsk=dsk)
    assert len(only(out, "bounce")) == 1 and has_notify(out)


def test_owner_question_does_not_push_at_night_but_is_posted_durably():
    dsk = disk(local_hhmm="03:15",
               blocked={"i7": "QUESTION: approach A or B?\nRECOMMENDATION: A"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert len(only(out, "post_question")) == 1, "the question is still posted durably to GitHub"
    assert not has_notify(out), "but the owner is not paged at 3am — it batches to the report"


def test_owner_question_during_the_day_pushes():
    dsk = disk(local_hhmm="10:00",
               blocked={"i7": "QUESTION: approach A or B?\nRECOMMENDATION: A"},
               issues_state={"version": 1, "issues": {"i7": ist("blocked")}})
    out = decide(dsk=dsk)
    assert len(only(out, "post_question")) == 1 and has_notify(out)


def test_systemic_stop_alert_still_pages_at_3am():
    # dead account auth with a spend pending is a SYSTEMIC stop — nothing can run — so it MUST keep
    # paging even in the dead of night. The safety layer is never quieted.
    dsk = disk(local_hhmm="03:00", auth_probe={"valid": False},
               issues_state={"version": 1, "issues": {"i5": ist(None)}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    alerts = only(out, "alert")
    assert alerts and "auth_dead" in alerts[0]["reasons"]
    assert has_notify(out), "a systemic stop pages regardless of the hour"


def test_quiet_hours_null_restores_the_old_always_push_behaviour():
    out = decide(config=cfg(notify={"imessage_to": None, "cmd": None, "quiet_hours": None}),
                 dsk=_recheck_park_dsk("23:30"))
    assert len(only(out, "park")) == 1 and has_notify(out), "explicit null disables night-batching"


def test_park_at_night_still_journals_and_lands_in_the_morning_report():
    # The end-to-end DoD assertion: a routine needs-owner park at night does NOT page AND does appear
    # in the morning report. We drive decide() for the (silent) park, journal exactly that action the
    # way the runner does (adds outcome + ts), then render the report from it.
    import report
    out = decide(dsk=_recheck_park_dsk("23:30"))
    park = only(out, "park")[0]
    assert not has_notify(out)                              # no 3am page
    record = dict(park, outcome="ok", ts=NOW)               # what runner._journal_outcome persists
    md = report.morning([record], {"date": "2026-07-02", "now": NOW + 10}, ledger={},
                        config={"repo": "o/r"})
    parked_section = md.split("## Parked / needs-owner")[1].split("\n## ")[0]
    assert "#5" in parked_section and "needs-owner" in parked_section.lower()


def test_quiet_hours_helper_windows_and_disable():
    q = {"start": "21:00", "end": "08:00"}                  # wraps midnight (the night window)
    assert actions._in_quiet_hours("23:30", q) and actions._in_quiet_hours("02:00", q)
    assert actions._in_quiet_hours("21:00", q)              # start inclusive
    assert not actions._in_quiet_hours("08:00", q)          # end exclusive
    assert not actions._in_quiet_hours("12:00", q)
    day = {"start": "09:00", "end": "17:00"}                # same-day window (no wrap)
    assert actions._in_quiet_hours("12:00", day) and not actions._in_quiet_hours("18:00", day)
    # disabled / malformed / missing clock all fail toward PUSHING (never quiet), never RAISE
    assert not actions._in_quiet_hours("03:00", None)
    assert not actions._in_quiet_hours("03:00", {"start": "oops", "end": "08:00"})
    assert not actions._in_quiet_hours(None, q)
    assert not actions._in_quiet_hours("03:00", {"start": "06:00", "end": "06:00"})
    assert not actions._in_quiet_hours("²³:00", q)          # unicode "digits": False, never int() raise
    assert not actions._in_quiet_hours("03:00", {"start": "²³:00", "end": "08:00"})
