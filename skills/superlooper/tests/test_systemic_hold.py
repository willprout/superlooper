"""The systemic-outage hold: a machine-wide failure PAUSES the queue (issue #320).

The 2026-07-09 shape, one root cause at a time: an environment fault that no queued issue caused
and none can fix walks every approved issue into its own park, and the owner pays N re-approvals
for something one command repaired. #24/#153 ended that for the DELIVERY CHANNEL. #159 ended it
for the runner's own Claude auth. #299 ended it for the runner's own `gh`. This suite pins the
LAYER that ends it for the rest, so the next class costs a table row instead of a redesign.

Two halves, both tested here:

  * ESCALATION. A reason that is honestly per-issue on ONE sample — one worktree with a broken env
    — is machine-wide across SYSTEMIC_ENV_FAILURE_CAP DISTINCT lanes. The discriminator is the
    owner's own (2026-08-03): "N consecutive refusals across DISTINCT issues is the same 'it is not
    the issues, it is the environment' inference the systemic-launch streak already makes for the
    delivery channel". One sample still parks exactly as it does today.
  * NAMING. Every held class says its OWN cause and its OWN remedy. A hold that arrives wearing the
    generic systemic body — macOS App Nap, restart cmux — is the exact mis-blame #299 was built to
    end, and a held queue writes no park memo, so that alert body is the WHOLE story the owner gets.

The arc test at the bottom is the acceptance fact the issue asks for: outage -> hold with exactly
one owner alert -> the owner fixes it -> the canary probe goes green -> the queue resumes on its
own, with nothing parked, nothing relabeled and no re-approval.
"""
import json

import pytest

import actions
import evidence
import loopstate
import report as report_lib
import runner as runner_mod

from test_actions import NOW, cfg, decide, disk, ist, only, parsed, has_notify
from test_runner import _launch_action, issue_state, mutations, rig  # noqa: F401  (rig is a fixture)


# --------------------------- the class registry (extensibility) ---------------------------

def test_the_three_named_classes_each_have_their_own_held_alert_reason():
    """The issue names three machine-wide classes. Each must reach the owner under a reason of its
    own — not another class's — because a held queue writes no park memo at all."""
    # worker-wide gh auth (rc=4 across distinct lanes: the 2026-07-29 inherited-XDG_CONFIG_HOME
    # spike, where the RUNNER's own gh stays healthy so the poll keeps working and the queue keeps
    # launching into refusals)
    assert "gh_auth_dead" in evidence.SYSTEMIC_ESCALATION_REASONS
    # Claude Code down / its auth dead in every worker env (rc=7 across distinct lanes)
    assert "claude_identity_wrong" in evidence.SYSTEMIC_ESCALATION_REASONS
    # the session host's control socket unreachable — already a CHANNEL fault (held on the first
    # failure), but it had no alert reason of its own and surfaced under the App Nap banner
    assert actions.LAUNCH_ALERT_REASONS["anchor_socket_lost"] != "launch_systemic_failure"
    for ev_reason in ("gh_auth_dead", "claude_identity_wrong", "anchor_socket_lost"):
        assert ev_reason in actions.LAUNCH_ALERT_REASONS, ev_reason


def test_a_fourth_class_is_a_table_row_not_a_re_plumbing():
    """The DoD's real ask: adding a class must not touch the detector, the hold, the canary or the
    recovery edge. `env_poisoned` IS that fourth class, shipped through the same two rows — and the
    proof it needed nothing else is that the arc below runs on it unchanged."""
    assert "env_poisoned" in evidence.SYSTEMIC_ESCALATION_REASONS
    assert actions.LAUNCH_ALERT_REASONS["env_poisoned"] == "env_poisoned_workers"
    # every escalatable reason is wired end to end: a class in the registry with no alert reason
    # would hold the queue and then say nothing at all about why.
    for r in evidence.SYSTEMIC_ESCALATION_REASONS:
        assert r in actions.LAUNCH_ALERT_REASONS, r


def test_every_held_class_names_its_own_cause_and_never_another_s_remedy():
    """The trap the issue calls out by name. The generic systemic body tells the owner to run
    `defaults write ... NSAppSleepDisabled` and relaunch cmux. For a dead credential or a poisoned
    env that is a confidently WRONG remedy the owner will spend a night on."""
    generic = actions.ALERT_MESSAGES["launch_systemic_failure"]
    for ev_reason, alert_reason in actions.LAUNCH_ALERT_REASONS.items():
        msg = actions._alert_message(alert_reason)
        assert msg != alert_reason, f"{alert_reason} falls back to its bare code"
        assert len(msg) > 80, alert_reason
        assert msg != generic, alert_reason
        assert "NSAppSleepDisabled" not in msg, alert_reason
        assert "defaults write" not in msg, alert_reason
        # a held class must say the queue is HELD, never imply the issues were parked
        assert "held" in msg.lower() or "hold" in msg.lower(), alert_reason


@pytest.mark.parametrize("ev_reason,needle", [
    ("gh_auth_dead", "gh auth login"),              # the worker envs' gh, not the runner's
    ("claude_identity_wrong", "claude"),            # the Anthropic account, not GitHub
    ("env_poisoned", "environment"),                # the variables, not a credential
    ("anchor_socket_lost", "socket"),               # the control socket, not App Nap
])
def test_each_class_carries_its_own_real_remedy(ev_reason, needle):
    msg = actions._alert_message(actions.LAUNCH_ALERT_REASONS[ev_reason]).lower()
    assert needle in msg, msg


def test_the_worker_gh_hold_is_not_confused_with_the_runner_gh_hold():
    """The asymmetric case is the motivating one: the runner's own gh is HEALTHY (the poll keeps
    working) while every worker's fresh env is de-authenticated. Telling the owner "the runner's
    own gh cannot say who it is" would send them to check the one thing that is fine."""
    workers = actions._alert_message("gh_auth_dead_workers")
    runner = actions._alert_message("gh_auth_dead_runner")
    assert workers != runner
    assert "worker" in workers.lower()


# --------------------------- the detector ---------------------------

# The launcher's OWN refusal text for each class, run through the real classifier — so a memo these
# tests read is the memo a park would really carry, not a fixture's idea of one.
_STDERR = {
    "gh_auth_dead": "[i5] GH AUTH DEAD: `gh api user` did not answer as 'loopbot'",
    "env_poisoned": "[i5] ENV POISONED: ANTHROPIC_API_KEY survived the scrub",
    "claude_identity_wrong": "[i5] CLAUDE IDENTITY REFUSED: not logged in",
}


def _ev(reason, rc=4):
    rec = evidence.build("launch", rc, _STDERR[reason])
    assert rec["reason"] == reason, rec       # the fixture must not drift from the classifier
    return rec


def _env_streak(reason, ids, rc=4, extra_ist=None, **over):
    """A disk view whose env-fault streak names `ids` for `reason` — what the runner publishes
    after each of those lanes refused its launch for that reason."""
    st = {i: ist("ready", launch_evidence=_ev(reason, rc)) for i in ids}
    st.update(extra_ist or {})
    return disk(launch_anchor={"ok": True},
                launch_env_fail_ids={reason: list(ids)},
                launch_fail_at=NOW - 10,
                issues_state={"version": 1, "issues": st}, **over)


def test_one_lane_refusing_is_not_systemic_and_still_parks_exactly_as_today():
    """The counter-argument the owner named: a genuinely one-off session fault (ONE worktree with a
    broken env) SHOULD park just that lane. Nothing about this path may change."""
    dsk = _env_streak("gh_auth_dead", ["i5"], extra_ist={
        "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP,
                  launch_evidence=_ev("gh_auth_dead"))})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    parks = only(out, "park")
    assert len(parks) == 1 and parks[0]["id"] == "i5"
    # ...with the memo naming AUTH, which is the whole reason #299 kept this per-issue
    assert "gh auth login" in parks[0]["memo"]
    assert only(out, "alert") == []


def test_two_distinct_lanes_refusing_the_same_way_holds_the_whole_queue():
    """The inference: it is not the issues, it is the environment."""
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)],
                 dsk=_env_streak("gh_auth_dead", ["i5", "i6"]))
    assert only(out, "park") == [], "a machine-wide fault must park nothing"
    assert only(out, "launch") == [], "and launch nothing new into the same wall"
    a = only(out, "alert")
    assert len(a) == 1 and a[0]["reasons"] == ["gh_auth_dead_workers"], a
    assert has_notify(out)


def test_the_hold_suppresses_the_per_issue_launch_cap_park():
    """A lane already AT its cap when the outage is recognised must be held, not parked: it is one
    of the N the owner would otherwise have to re-approve."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"], extra_ist={
        "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP,
                  launch_evidence=_ev("gh_auth_dead"))})
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert only(out, "park") == []


def test_two_lanes_refusing_for_DIFFERENT_reasons_are_two_faults_not_one_outage():
    """Distinctness is per REASON. Two different environment faults, one lane each, is exactly the
    'one broken worktree' case twice over — not evidence that the machine is down."""
    dsk = disk(launch_anchor={"ok": True},
               launch_env_fail_ids={"gh_auth_dead": ["i5"], "env_poisoned": ["i6"]},
               launch_fail_at=NOW - 10,
               issues_state={"version": 1, "issues": {
                   "i5": ist("ready", launch_evidence=_ev("gh_auth_dead")),
                   "i6": ist("ready", launch_evidence=_ev("env_poisoned", 6))}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert only(out, "alert") == []


def test_a_garbage_streak_view_never_holds_the_queue():
    """Fail OPEN on unreadable input: a wrongly-held queue is the bigger, quieter outage, and this
    view is data the runner writes — a bug there must cost nothing."""
    for bad in (None, [], "x", {"gh_auth_dead": "i5"}, {"gh_auth_dead": ["", None]},
                {5: ["i5", "i6"]}, {"not_a_reason": ["i5", "i6", "i7"]}):
        out = decide(parsed_issues=[parsed(5), parsed(6)],
                     dsk=disk(launch_anchor={"ok": True}, launch_env_fail_ids=bad))
        assert only(out, "alert") == [], bad


def test_a_repeat_of_the_same_lane_is_one_sample_not_two():
    """The streak counts DISTINCT lanes. A view that somehow lists the same id twice must not add
    up to an outage — that is the 'one broken worktree, retried' case."""
    out = decide(parsed_issues=[parsed(5), parsed(6)],
                 dsk=_env_streak("gh_auth_dead", ["i5", "i5"]))
    assert only(out, "alert") == []


# --------------------------- sample a second lane before parking the first ---------------------

def test_an_open_streak_prefers_a_lane_that_has_not_been_sampled_yet():
    """What makes 'nothing parked' structural rather than lucky. With one lane's refusal on the
    board the environment question is OPEN, and the cheapest way to settle it is to try a DIFFERENT
    lane — otherwise a serialized queue (one lane, or one territory) retries the same issue into
    its cap and parks it before a second sample ever exists."""
    dsk = _env_streak("gh_auth_dead", ["i5"])
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, config=cfg(lanes=1))
    launched = [a["id"] for a in only(out, "launch")]
    assert launched == ["i6"], launched


def test_with_nothing_else_to_sample_the_refused_lane_launches_exactly_as_today():
    """The bound that keeps the preference from starving the queue: when there is no unsampled
    peer, the sampled lane is launched normally — and so it still reaches its cap and still parks."""
    dsk = _env_streak("gh_auth_dead", ["i5"])
    out = decide(parsed_issues=[parsed(5)], dsk=dsk, config=cfg(lanes=1))
    assert [a["id"] for a in only(out, "launch")] == ["i5"]


def test_the_preference_expires_so_an_unschedulable_peer_can_never_starve_the_queue():
    """Second bound, on the clock: if the peer that would settle the question is never actually
    schedulable (its territory is claimed, usage says no), the preference must let go rather than
    hold the refused lane out of the queue forever."""
    dsk = _env_streak("gh_auth_dead", ["i5"])
    dsk["launch_fail_at"] = NOW - actions.ENV_SAMPLE_WINDOW_SECONDS - 1
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, config=cfg(lanes=1))
    assert [a["id"] for a in only(out, "launch")] == ["i5"]


def test_an_unreadable_clock_fails_open_to_no_preference():
    for bad in (None, "x", float("nan"), True):
        dsk = _env_streak("gh_auth_dead", ["i5"])
        dsk["launch_fail_at"] = bad
        out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, config=cfg(lanes=1))
        assert [a["id"] for a in only(out, "launch")] == ["i5"], bad


# --------------------------- recovery: the probe, and the resume ---------------------------

def test_the_held_queue_probes_itself_with_the_existing_canary():
    """The hold cannot clear itself (the streak clears only on a VERIFIED delivery, which the hold
    suppresses). #115's canary is the recovery probe the DoD asks for — READ-ONLY: it launches the
    front-of-queue issue, mutates no credential, logs nothing in, restarts nothing."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"])
    dsk["launch_fail_at"] = NOW - actions.CANARY_RETRY_SECONDS - 1
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    probes = only(out, "launch")
    assert len(probes) == 1 and probes[0]["canary"] is True
    assert only(out, "park") == []


def test_the_probe_tries_a_lane_that_has_NOT_already_refused():
    """A WRONGLY-held queue must self-heal, and this is the only thing that makes it (fresh-review
    P0). Two genuinely lane-local faults can reach the cap and escalate; if the probe then goes to
    the front of the queue it lands back on a lane that has already refused, re-fails, charges no
    cap by design, and the hold stands forever over a machine that was never broken. Probing the
    healthy lane behind them is what ends the episode."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"])
    dsk["launch_fail_at"] = NOW - actions.CANARY_RETRY_SECONDS - 1
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    probes = only(out, "launch")
    assert [p["id"] for p in probes] == ["i7"], probes
    assert probes[0]["canary"] is True


def test_an_unsampled_lane_the_SCHEDULER_refuses_does_not_cost_the_probe():
    """The other half of preferring an unsampled lane, and the reason the probe takes two passes
    rather than one filtered list: `_eligible_rows` is a pure eligibility filter, and the SCHEDULER
    is what actually refuses a blocked or territory-claimed issue. Handing it only the unsampled
    lane and taking its "no" for an answer would emit no probe at all — which, while the hold
    suppresses every park, is a tick the loop cannot leave."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"])
    dsk["launch_fail_at"] = NOW - actions.CANARY_RETRY_SECONDS - 1
    blocked = parsed(7, blocked_by=[5])          # unsampled, but the scheduler will refuse it
    out = decide(parsed_issues=[parsed(5), parsed(6), blocked], dsk=dsk)
    probes = only(out, "launch")
    assert len(probes) == 1 and probes[0]["canary"] is True, out
    assert probes[0]["id"] in ("i5", "i6"), probes


def test_the_probe_still_fires_when_every_candidate_is_at_its_launch_cap():
    """The dead end the hold could otherwise create (fresh-review P0). While a hold stands it
    suppresses every cap-park, so a queue whose candidates had all reached the cap would emit no
    probe (the cap filters them out of the candidate set) AND no park — a state nothing in the loop
    can leave except a restart. The probe asks about the MACHINE, which an at-cap lane answers as
    well as any other, and it charges no cap either way."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"], extra_ist={
        "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP,
                  launch_evidence=_ev("gh_auth_dead")),
        "i6": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP,
                  launch_evidence=_ev("gh_auth_dead"))})
    dsk["launch_fail_at"] = NOW - actions.CANARY_RETRY_SECONDS - 1
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    probes = only(out, "launch")
    assert len(probes) == 1 and probes[0]["canary"] is True, out
    assert only(out, "park") == [], "and it still parks nothing"


def test_the_normal_launch_path_never_relaxes_the_per_issue_cap():
    """The other half of that fix: only the PROBE may ignore the cap. If the normal path did too, an
    at-cap issue would launch forever instead of parking — the cap would stop meaning anything."""
    dsk = disk(launch_anchor={"ok": True},
               issues_state={"version": 1, "issues": {
                   "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP,
                             launch_evidence=_ev("gh_auth_dead"))}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    assert [a["id"] for a in only(out, "park")] == ["i5"]


def test_a_green_probe_lifts_the_hold_and_journals_the_recovery():
    """The streak is cleared by the runner on a verified delivery; THIS tick sees it fall while the
    durable ALERT still names the class — the exit edge. One journal record, and the alert retracts
    itself through the reasons diff. No relabel, no re-approval: the queue simply resumes."""
    dsk = disk(launch_anchor={"ok": True}, launch_env_fail_ids={},
               alert={"reasons": ["gh_auth_dead_workers"], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert len(only(out, "launch_recovered")) == 1
    assert only(out, "clear_alert"), "the hold's alert must retract itself"
    assert [a["id"] for a in only(out, "launch")], "and launching must resume on its own"
    assert only(out, "park") == [] and only(out, "relabel") == []


@pytest.mark.parametrize("alert_reason", sorted(actions.LAUNCH_HOLD_ALERT_REASONS))
def test_every_name_a_launch_hold_can_wear_journals_its_own_recovery(alert_reason):
    """Fresh-review P2. The recovery edge used to key on the GENERIC name alone, which was complete
    only while that was the only thing a launch streak could be called. #299 broke it and this issue
    widened it to five more classes — so a hold under any of them cleared SILENTLY: the alert
    retracted, launching resumed, and the journal recorded that the outage simply stopped existing."""
    dsk = disk(launch_anchor={"ok": True}, launch_env_fail_ids={},
               alert={"reasons": [alert_reason], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert len(only(out, "launch_recovered")) == 1, alert_reason


def test_a_reason_the_POLL_also_raises_is_not_read_as_a_launch_recovery():
    """`gh_unreachable` is the one launch-alert reason a second detector raises (the poll's
    consecutive-failure count). Its falling edge says GitHub answered, not that launch delivery came
    back, so reading it as a launch recovery would journal one that never happened."""
    assert "gh_unreachable" not in actions.LAUNCH_HOLD_ALERT_REASONS
    dsk = disk(launch_anchor={"ok": True}, launch_env_fail_ids={},
               alert={"reasons": ["gh_unreachable"], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    assert only(decide(parsed_issues=[parsed(5)], dsk=dsk), "launch_recovered") == []


def test_the_recovery_record_is_emitted_once_not_every_tick():
    dsk = disk(launch_anchor={"ok": True}, launch_env_fail_ids={},
               issues_state={"version": 1, "issues": {}})
    assert only(decide(parsed_issues=[parsed(5)], dsk=dsk), "launch_recovered") == []


def test_a_still_held_class_does_not_journal_a_recovery():
    """Two classes at once: one clearing must not announce that launching resumed while the other
    still holds the queue."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"])
    dsk["alert"] = {"reasons": ["env_poisoned_workers", "gh_auth_dead_workers"], "since": NOW - 60}
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert only(out, "launch_recovered") == []


# --------------------------- the runner half: the streak itself ---------------------------

def test_an_environment_refusal_feeds_the_streak_and_still_charges_its_own_lane(rig):
    """Both halves at once, and they are not in tension: the lane's own cap keeps ticking (so a
    genuine one-off still parks), AND the sample is recorded so a second distinct lane can prove
    the fault is the machine's."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(
        4, "[i101] GH AUTH DEAD: `gh api user` did not answer as 'loopbot'"))
    rig.r._execute(_launch_action(), NOW)
    assert issue_state(rig, "i101")["launch_failures"] == 1
    assert "i101" not in rig.r._launch_fail_ids, "an env fault is not a delivery-channel fault"
    assert rig.r._launch_env_fail_ids.get("gh_auth_dead") == {"i101"}


def test_the_streak_is_published_for_decide_to_read(rig, monkeypatch):
    """decide is PURE — it can only see what the tick hands it, so the streak must ride the view
    beside the channel streak it sits above."""
    seen = {}
    real = actions.decide

    def spy(now, config, usage, parsed_issues, lane_state, events, dsk, gh_view, **kw):
        seen.update(dsk)
        return real(now, config, usage, parsed_issues, lane_state, events, dsk, gh_view, **kw)

    rig.r.tick(now=NOW)
    rig.r._launch_env_fail_ids["gh_auth_dead"] = {"i102", "i101"}
    monkeypatch.setattr(actions, "decide", spy)
    rig.r.tick(now=NOW + 20)
    assert seen["launch_env_fail_ids"] == {"gh_auth_dead": ["i101", "i102"]}


def test_a_verified_delivery_clears_the_streak(rig):
    """One green launch proves the environment is fine — the same evidence that clears the channel
    streak, and the only thing that ever clears either."""
    rig.r.tick(now=NOW)
    rig.r._launch_env_fail_ids["gh_auth_dead"] = {"i101"}
    rig.r._delivery_cleared()
    assert rig.r._launch_env_fail_ids == {}


def test_a_probes_own_per_issue_misfortune_never_renames_the_standing_hold(rig):
    """Fresh-review P1. A canary only ever runs while a hold ALREADY stands, and the lane it probes
    can fail for a reason of its own — a worktree that would not create, a brief that could not be
    written. Recording that in the CHANNEL streak makes decide read it back as the streak's cause,
    find no mapping for it, and append the generic `launch_systemic_failure` — so a queue held for
    dead worker auth pages the owner a SECOND time, mid-episode, telling them to go reconfigure
    macOS App Nap. That is the exact mis-blame this layer exists to end."""
    rig.r.tick(now=NOW)
    rig.r._launch_env_fail_ids["gh_auth_dead"] = {"i101", "i102"}
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(1, "[i103] could not create the worktree"))
    rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert rig.r._launch_fail_ids == set(), "a probe's own git problem is not a channel fault"
    assert rig.r._launch_fail_at == NOW, "but the probe still re-spaces its own retry clock"
    assert issue_state(rig, "i101").get("launch_failures", 0) == 0, "and charges no cap either"


def test_not_even_a_probes_CHANNEL_failure_renames_the_episode(rig):
    """Round 3 of the same finding, one rung up: a GENUINE channel reason the alert table has no
    mapping for — a `launch_timeout` probed during a dead-socket episode — takes the identical route
    to the identical wrong banner. #299's mixed-streak rule is right for what it was written about
    (two different lanes failing differently is two causes, and both must be named); a probe
    re-reading the same outage is not a second cause. So a canary contributes to the retry clock and
    nothing else, and the episode keeps the name the lanes that TRIPPED it gave it."""
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i102"}
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(124, "[i101] launcher hung"))
    rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert rig.r._launch_fail_ids == {"i102"}, "the streak keeps the lanes that tripped the hold"
    assert rig.r._launch_fail_at == NOW


def test_a_real_launchs_channel_failure_does_still_feed_the_streak(rig):
    """The line that keeps the rule above from disarming #24: a REAL launch is a sample, and its
    channel fault is exactly what establishes and sustains the hold."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(2, "[i101] shim never fired"))
    rig.r._execute(_launch_action(), NOW)
    assert "i101" in rig.r._launch_fail_ids


def test_the_probe_still_fires_when_a_candidates_launch_counter_is_CORRUPT():
    """Round-3 P0, the same no-probe/no-park shape through a different door: a wrong-typed
    `launch_failures` excludes a lane from the candidate set, while the hold suppresses the
    corrupt-counter park that would otherwise resolve it. The probe sets the whole launch-failure
    accounting aside — cap and readability alike — because it is bookkeeping about an ISSUE and the
    probe's question is about the MACHINE."""
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"], extra_ist={
        "i5": ist("ready", launch_failures="oops", launch_evidence=_ev("gh_auth_dead")),
        "i6": ist("ready", launch_failures=None, launch_evidence=_ev("gh_auth_dead"))})
    dsk["launch_fail_at"] = NOW - actions.CANARY_RETRY_SECONDS - 1
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    probes = only(out, "launch")
    assert len(probes) == 1 and probes[0]["canary"] is True, out
    assert only(out, "park") == []


def test_a_corrupt_counter_still_parks_its_lane_when_nothing_is_held():
    """...and the deferral is only a deferral: with no hold standing, a corrupt counter parks
    exactly as it does today, and the probe's relaxation never reaches the normal launch path."""
    dsk = disk(launch_anchor={"ok": True},
               issues_state={"version": 1, "issues": {"i5": ist("ready", launch_failures="oops")}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    assert only(out, "launch") == []
    assert [a["id"] for a in only(out, "park")] == ["i5"]


def test_a_per_issue_fault_outside_the_registry_never_enters_the_streak(rig):
    """A missing brief, a git-level worktree failure: this issue's own state, no matter how many
    lanes hit it. Only the registry's environment classes escalate."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(1, "[i101] could not create the worktree"))
    rig.r._execute(_launch_action(), NOW)
    assert rig.r._launch_env_fail_ids == {}
    assert issue_state(rig, "i101")["launch_failures"] == 1


# --------------------------- visibility: a paused queue is not an idle one -------------------

def test_status_says_the_queue_is_held_and_why():
    alert = {"reasons": ["gh_auth_dead_workers"], "since": NOW}
    line = actions.queue_hold_line(alert)
    assert line.startswith("queue: HELD")
    assert "gh_auth_dead_workers" in line


def test_status_says_flowing_when_nothing_holds():
    for a in (None, {"reasons": []}, {"reasons": ["usage_stale"]}):
        assert actions.queue_hold_line(a) == "queue: flowing", a


def test_an_unreadable_alert_marker_is_never_reported_as_a_flowing_queue():
    """Existence IS the signal (the merges_frozen.json posture). A damaged marker may hide a held
    queue, and "flowing" printed under a visibly broken ALERT line is the one answer that is
    certainly wrong. `{}` is what both readers produce for present-but-unparseable."""
    for a in ({}, "garbage", 5, []):
        line = actions.queue_hold_line(a)
        assert "flowing" not in line, a
        assert "UNKNOWN" in line, a
    assert actions.queue_hold_reasons({}) == [actions.ALERT_UNREADABLE]


def test_report_and_actions_agree_on_the_unreadable_sentinel():
    """report.py mirrors the sentinel rather than importing the decide graph (its own discipline for
    _TERMINAL_STATUSES). Pinned so the two cannot drift into a report that renders a bare code."""
    assert report_lib._ALERT_UNREADABLE == actions.ALERT_UNREADABLE


def test_the_report_hedges_rather_than_naming_a_cause_it_could_not_read():
    view = {"date": "2026-08-07", "now": NOW, "frozen": None, "queue": [], "usage": None,
            "queue_hold": {"reasons": [actions.ALERT_UNREADABLE], "since": NOW - 600}}
    text = report_lib.morning([], view, {}, cfg())
    assert "Nothing happened overnight" not in text
    assert "UNREADABLE" in text


def test_the_hold_line_names_every_held_class_at_once():
    line = actions.queue_hold_line({"reasons": ["env_poisoned_workers", "gh_auth_dead_workers"]})
    assert "env_poisoned_workers" in line and "gh_auth_dead_workers" in line


def test_the_hold_line_says_the_hold_itself_takes_no_action():
    """The whole point of surfacing it: a paused queue must never be read as an idle one, and the
    owner must not go looking for issues to re-approve."""
    line = actions.queue_hold_line({"reasons": ["gh_auth_dead_workers"]})
    assert "parks nothing" in line and "moves no label" in line


def test_no_held_class_claims_an_accounting_it_did_not_keep():
    """Fresh-review P2. "No issue charged" is true of the CHANNEL classes and FALSE of the escalated
    environment ones: those bump the refusing lane's own launch cap on the way in, which is exactly
    what keeps a one-off broken worktree parking normally. A status line and an alert body that
    overclaim are how an owner learns to stop believing them."""
    shared = actions.queue_hold_line({"reasons": ["gh_auth_dead_workers"]}).lower()
    assert "no issue charged" not in shared
    # ...and it may not flatly claim NOTHING is parked either (round-4 P2): a lane that reached its
    # own launch cap BEFORE a second distinct refusal proved the outage parked on its own account,
    # and an owner told otherwise walks past a real re-approval. What is true of every class is that
    # the hold ITSELF takes no action, so that is what the shared surfaces claim.
    assert "nothing is parked" not in shared and "nothing parked" not in shared
    assert "parks nothing" in shared
    for ev_reason in evidence.SYSTEMIC_ESCALATION_REASONS:
        msg = actions._alert_message(actions.LAUNCH_ALERT_REASONS[ev_reason]).lower()
        assert "no issue charged" not in msg, ev_reason
        assert "nothing parked" not in msg, ev_reason
        assert "parks nothing" in msg, ev_reason
        # and each says out loud what the late-escalation case costs
        assert "may already have parked" in msg, ev_reason


def test_the_morning_report_never_reads_a_held_queue_as_a_quiet_night():
    """The exact confusion the DoD names. An overnight outage with a full queue and nothing moving
    is the ONE night that reads 'nothing happened' precisely BECAUSE nothing happened."""
    view = {"date": "2026-08-07", "now": NOW, "frozen": None, "queue": [], "usage": None,
            "queue_hold": {"reasons": ["gh_auth_dead_workers"], "since": NOW - 3600}}
    text = report_lib.morning([], view, {}, cfg())
    assert "Nothing happened overnight" not in text
    # the push body (the first non-title line) must carry it: an owner who never opens the file
    # still learns the loop stopped
    summary = next(ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#"))
    assert "HELD" in summary, summary


def test_the_morning_report_names_the_cause_and_says_the_hold_took_no_action():
    view = {"date": "2026-08-07", "now": NOW, "frozen": None,
            "queue": [{"num": 5, "title": "a"}, {"num": 6, "title": "b"}], "usage": None,
            "queue_hold": {"reasons": ["gh_auth_dead_workers"], "since": NOW - 3600}}
    text = report_lib.morning([], view, {}, cfg())
    assert "gh_auth_dead_workers" in text
    assert "parks nothing" in text and "moves no label" in text
    assert "1h 0m" in text, "how long the loop has been down is the first thing to know"


def test_the_runner_hands_the_hold_to_the_morning_report(rig):
    """report.py is pure and never imports the runner's brain, so the hold reaches it only if the
    tick puts it in the view. Pinned end to end: the ALERT on disk becomes the line in the file."""
    loopstate.save(str(rig.home / "state" / "ALERT"),
                   {"reasons": ["gh_auth_dead_workers"], "since": NOW - 7200})
    rig.r._morning_report_hook("2026-08-07", NOW)
    text = (rig.home / "reports" / "morning-2026-08-07.md").read_text()
    assert "THE LAUNCH QUEUE IS HELD" in text
    assert "gh_auth_dead_workers" in text


def test_the_runners_report_is_unchanged_when_nothing_is_held(rig):
    rig.r._morning_report_hook("2026-08-07", NOW)
    assert "HELD" not in (rig.home / "reports" / "morning-2026-08-07.md").read_text()


def test_a_malformed_queue_hold_renders_no_hold_rather_than_a_broken_line():
    for bad in (None, {}, "x", {"reasons": None}, {"reasons": []}, {"reasons": [None, ""]}):
        view = {"date": "2026-08-07", "now": NOW, "frozen": None, "queue": [], "usage": None,
                "queue_hold": bad}
        assert "HELD" not in report_lib.morning([], view, {}, cfg()), bad


def test_a_report_with_no_alert_is_unchanged():
    view = {"date": "2026-08-07", "now": NOW, "frozen": None, "queue": [], "usage": None}
    text = report_lib.morning([], view, {}, cfg())
    assert "Nothing happened overnight" in text
    assert "HELD" not in text


# --------------------------- the arc (the DoD's acceptance fact) ---------------------------

GH_AUTH_DEAD_STDERR = "[%s] GH AUTH DEAD: `gh api user` did not answer as 'loopbot'"


@pytest.fixture
def outage(rig):
    """Real runner ticks over a LAUNCHER that refuses every flight the way a machine-wide fault
    does — the 2026-07-29 shape: an inherited XDG_CONFIG_HOME de-authenticates `gh` in every
    WORKER's fresh env while the runner's own stays healthy, so the poll keeps working and the queue
    keeps launching into refusals.

    Serialized to ONE lane on purpose. That is the harder shape and the realistic one for a repo
    whose issues share a territory: with a single lane the queue retries the SAME issue into its
    per-issue cap, so a design that only holds after the second distinct sample would have parked
    the first issue before the second ever existed."""
    rig.r.config["lanes"] = 1
    fault = {"rc": 0}
    launched = []
    inner = rig.r._run_script

    def run_script(args, env=None, timeout=None):
        a = [str(x) for x in args]
        if a and a[0].endswith("launch-session.py"):
            launched.append(a[1])
            if fault["rc"]:
                inner(args, env=env, timeout=timeout)       # still recorded in rig.calls
                return runner_mod.ScriptRC(fault["rc"], GH_AUTH_DEAD_STDERR % a[1])
        return inner(args, env=env, timeout=timeout)

    rig.r._run_script = run_script
    rig.fault = fault
    rig.launched = launched
    return rig


def _journal(rig, act):
    import journal as journal_mod
    return [r for r in journal_mod.read(str(rig.home)) if r.get("act") == act]


def _alert_file(rig):
    p = rig.home / "state" / "ALERT"
    return json.loads(p.read_text()) if p.exists() else None


def test_a_machine_wide_outage_holds_the_queue_and_resumes_on_its_own(outage):
    """THE arc, end to end: outage -> the queue holds with exactly ONE owner alert -> the owner
    fixes it in one command -> the recovery probe goes green -> the queue resumes by itself, with
    nothing parked, nothing relabeled and no re-approval asked for."""
    rig = outage
    rig.fault["rc"] = 4
    t = NOW
    for _ in range(6):                                  # the outage: every launch refuses
        rig.r.tick(now=t)
        t += 20
    # ...and it lasts long enough for the recovery probe to fire and REFUSE, twice. This is where a
    # second owner page would leak: a failed canary is charged to the CHANNEL streak (#115), so from
    # the next tick the SAME outage is visible through two detectors at once. They must agree on the
    # name — both read the failing lane's own stamped evidence through LAUNCH_ALERT_REASONS — or the
    # reasons list changes, the dedup breaks, and the owner is paged again every five minutes.
    for _ in range(2):
        t += actions.CANARY_RETRY_SECONDS + 30
        rig.r.tick(now=t)
        t += 20
        rig.r.tick(now=t)

    # --- the queue is HELD, not walked into parks ---
    st = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    assert [i for i, d in st.items() if d.get("status") == "parked"] == []
    assert _journal(rig, "park") == []
    assert not [m for m in mutations(rig) if "parked" in json.dumps(m)]
    # ...and every approved issue still wears agent-ready: nothing to re-approve
    assert not [m for m in mutations(rig)
                if m.get("kind") == "set_labels" and "agent-ready" in (m.get("remove") or [])]

    # --- exactly ONE owner alert, naming THIS class ---
    alerts = _journal(rig, "alert")
    assert len(alerts) == 1, alerts
    assert _alert_file(rig)["reasons"] == ["gh_auth_dead_workers"]
    notifies = [n for n in _journal(rig, "notify")
                if "gh_auth_dead_workers" in json.dumps(n) or "worker" in json.dumps(n).lower()]
    assert len(notifies) == 1, notifies

    # --- two distinct lanes were sampled, and then the queue stopped launching ---
    assert len(set(rig.launched)) >= 2, rig.launched
    sampled = len(rig.launched)

    # --- the owner fixes it in one command; the recovery probe finds out on its own ---
    rig.fault["rc"] = 0
    t += actions.CANARY_RETRY_SECONDS + 30
    rig.r.tick(now=t)                                   # the canary probe
    assert len(rig.launched) == sampled + 1, "exactly one probe, not a re-storm"
    t += 20
    rig.r.tick(now=t)                                   # the tick that sees the streak cleared

    assert len(_journal(rig, "launch_recovered")) == 1
    assert _alert_file(rig) is None, "the hold's alert retracts itself"
    assert _journal(rig, "park") == []
    running = {i for i, d in
               loopstate.load(str(rig.home / "state" / "issues.json"))["issues"].items()
               if d.get("status") == "running"}
    assert running, "the queue resumed on its own"


def test_a_single_broken_lane_under_the_same_fault_still_parks(outage):
    """The other half of the DoD, on the same rig: when the fault is NOT machine-wide — one lane
    refuses, every other lane flies — the queue must behave exactly as it does today."""
    rig = outage
    rig.r.config["lanes"] = 2          # the healthy lane flies and keeps flying; only i102 refuses
    bad = {"i102"}                                      # one issue's worktree, nobody else's

    inner = rig.r._run_script

    def run_script(args, env=None, timeout=None):
        a = [str(x) for x in args]
        if a and a[0].endswith("launch-session.py") and a[1] in bad:
            rig.launched.append(a[1])
            return runner_mod.ScriptRC(4, GH_AUTH_DEAD_STDERR % a[1])
        return inner(args, env=env, timeout=timeout)

    rig.r._run_script = run_script
    t = NOW
    for _ in range(3):                                  # fail, fail, park — the unchanged schedule
        rig.r.tick(now=t)
        t += 20

    st = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    assert st["i102"]["status"] == "parked", st["i102"]
    assert st["i101"]["status"] == "running", "the healthy lane was never touched"
    assert _alert_file(rig) is None, "one broken lane is not a machine-wide outage"
    parks = _journal(rig, "park")
    assert [p.get("id") for p in parks] == ["i102"], parks
    # the memo names AUTH — the reason #299 kept this fault per-issue in the first place
    assert "gh auth login" in json.dumps(parks[0])




# ============ the machine-wide credential/environment hold (issue #457) ======================
#
# 2026-08-26 was exactly the class this layer exists for, and it stayed silent for a full day.
# The realized shape, from the repo's own journal:
#
#   18:30  the usage meter goes unreadable past the grace -> usage_stale ALERT + FAIL OPEN
#          (a deliberate, recorded decision — this issue does not touch it)
#   19:03  the watchdog's unattended sl-debugger launch REFUSES (d26, rc=5: the runner's own gh)
#   19:08  ...again (d27)
#   19:13  ...again (d28)
#   15:19  the owner taps Debug from the command center; it refuses too (d29, rc=7)
#          "[d29] CLAUDE IDENTITY REFUSED ... this environment is not logged in to Claude"
#
# ...while the auth probe answered `unknown` throughout, and the queue's own lanes were all in
# flight, so the runner attempted almost no launch of its own for hours.
#
# NOTHING in #320 could see that, for two reasons that compound. Its streaks count the RUNNER's own
# launches, and every launch on that machine was made by another PROCESS. And they key on ONE
# evidence reason reaching a cap: these refusals wore several, and not one of them repeated across
# two distinct lanes. So there was no hold, no single alert, no pause: the loop sat "idle" over a
# machine that could not start a session at all.
#
# The class this section pins: N distinct SESSION ids whose launches refused for a
# CREDENTIAL/ENVIRONMENT reason, consecutively, with nothing starting between them — AT LEAST ONE OF
# THEM A FLIGHT THE QUEUE DOES NOT OWN — combined with a meter we cannot read or an auth probe that
# will not confirm the account. Three disciplines bound it:
#
#   * REFUSED IS NOT DEAD. An `unknown` probe is a question the machine declined to answer (the
#     live probe reads it right now, on a healthy machine), so it may never hold anything by
#     itself. The OBSERVED launch failures are the evidence that tips the verdict.
#   * A BROKEN WORKTREE IS NOT A BROKEN MACHINE. The owner's rule (2026-08-03) is that two
#     different environment faults with one lane each is the one-off case twice over. A d<N> flight
#     runs in the repo's own checkout, owns no worktree, and is what makes the difference.
#   * THE LOOP MUST ALWAYS BE ABLE TO LEAVE. A hold that suppressed every flight would be a state
#     nothing but a restart could end, and this streak clears on exactly one thing: a launch.

AUTH_DEATH = "claude_auth_dead_machine"
_AUTH_UNKNOWN = {"cli": "unknown", "keychain_present": True, "keychain_mtime": NOW - 3600,
                 "valid": None, "status_raw": ""}
_AUTH_HEALTHY = {"cli": "logged_in", "keychain_present": True, "keychain_mtime": NOW - 3600,
                 "valid": True, "status_raw": ""}
_AUTH_DEAD = dict(_AUTH_UNKNOWN, cli="logged_out", valid=False)


def _dark_meter(now=NOW):
    """A usage view whose meter has been unreadable PAST the fail-open grace — the first half of the
    2026-08-26 shape, and the posture #46 deliberately FAILS OPEN on (untouched here)."""
    return {"auth_status": "api_error", "five_hour_pct": None, "seven_day_pct": None,
            "last_ok_at": now - actions.USAGE_FAIL_OPEN_GRACE_SECONDS - 1,
            "first_attempt_at": now - 7200}


def _attempts(ids, fail_at=NOW - 10, spawners=("runner", "watchdog"), **over):
    """A disk view whose ATTEMPT streak names `ids` — the distinct session ids whose launch refused
    for a credential/environment reason with no verified delivery since — and the SPAWNERS those
    refusals came from, which is what the hold actually counts."""
    over.setdefault("launch_anchor", {"ok": True})
    if isinstance(ids, (list, tuple)):
        # each id gets a spawner, cycling through the ones named — the runner publishes the MAP, so
        # a test may not name a spawner without a sample that produced it
        streak = {"samples": {x: [list(spawners)[i % len(spawners)]] for i, x in enumerate(ids)}}
    else:
        streak = ids
    return disk(launch_attempt_streak=streak, launch_fail_at=fail_at, **over)


def _reasons(out):
    return sorted({r for a in only(out, "alert") for r in a["reasons"]})


def test_the_auth_death_class_is_held_named_and_carries_its_own_remedy():
    """Every held class says its OWN cause and its OWN remedy (#320's rule, and a held queue writes
    no park memo, so this body is the whole story the owner gets). This streak by construction has
    NOT settled on one of the three credential faults, so the body names all three rather than
    guessing — and says out loud what it would mean if the escalation were wrong."""
    assert AUTH_DEATH in actions.QUEUE_HELD_ALERT_REASONS
    assert AUTH_DEATH not in actions.LAUNCH_HOLD_ALERT_REASONS, \
        "it owns an exit edge of its own; sharing the generic marker cost #320 its restart record"
    msg = actions._alert_message(AUTH_DEATH)
    assert msg != AUTH_DEATH, "it falls back to its bare code"
    assert len(msg) > 80
    assert msg != actions.ALERT_MESSAGES["launch_systemic_failure"]
    assert "NSAppSleepDisabled" not in msg and "defaults write" not in msg
    assert "held" in msg.lower() or "hold" in msg.lower()
    assert "parks nothing" in msg.lower()
    for remedy in ("claude auth status", "gh auth login", "ANTHROPIC_API_KEY"):
        assert remedy in msg, remedy


def test_the_realized_shape_holds_the_queue_dark_meter_then_distinct_session_failures():
    """THE 2026-08-26 detector fact: an unreadable meter, then consecutive refusals across distinct
    session ids with no success between. One alert, nothing parked, nothing launched."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH in _reasons(out), _reasons(out)
    assert len(only(out, "alert")) == 1, "one alert, not one per class and not one per tick"
    assert only(out, "park") == [], "a machine-wide fault parks nothing"
    assert only(out, "launch") == [], "and launches nothing new into the same wall"
    assert only(out, "relabel") == []
    assert has_notify(out)


def test_two_QUEUE_LANES_alone_are_never_this_class_however_many_they_are():
    """The owner's own rule, and the one this class could most easily break: "two different
    environment faults with one lane each is the one-off case twice over, not an outage"
    (2026-08-03, the rule SYSTEMIC_ENV_FAILURE_CAP is per-reason for). Two lanes with two separately
    broken worktrees must park with their own memos, not freeze the healthy queue behind them under
    a banner saying the machine's login expired."""
    for ids in (["i5", "i6"], ["i5", "i6", "i7"]):
        dsk = _attempts(ids, spawners=["runner"], auth_probe=_AUTH_UNKNOWN)
        out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk, usage=_dark_meter())
        assert AUTH_DEATH not in _reasons(out), ids


def test_ONE_SPAWNERS_OWN_FLIGHTS_are_never_read_as_the_machine():
    """A refusal only proves something about the environment it was read in, and one spawner has
    one environment however many flights it makes. The watchdog mints a FRESH d<N> for every retry
    of ONE episode (three per episode), so a watchdog started by launchd with a bare PATH refuses
    every flight identically while the runner's own launches are demonstrably fine; reading that as
    the machine would freeze a healthy queue behind a page saying nothing can start at all — and
    this hold's own ALERT is a watchdog signal, so it would re-arm from the episode it opened. The
    same is true one spawner over: `debug` and `resume` are shelled from the same shell or the same
    dashboard, and `resume` carries a LANE's own i<N>, so ids cannot be counted for independence."""
    for ids, spawners in ((["d26", "d27"], ["watchdog"]),
                          (["d26", "d27", "d28"], ["watchdog"]),
                          (["d29", "i104"], ["operator"]),
                          (["i5", "i6"], ["runner"])):
        dsk = _attempts(ids, spawners=spawners, auth_probe=_AUTH_UNKNOWN)
        out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
        assert AUTH_DEATH not in _reasons(out), (ids, spawners)
        assert [a["id"] for a in only(out, "launch")], "the healthy queue keeps flying"


def test_TWO_INDEPENDENT_SPAWNERS_agreeing_is_what_neither_can_explain_away():
    """The discriminator, and the shape the real 2026-08-26 journal produced: the watchdog's launchd
    job refused at 19:03 and the owner's Debug tap refused the next afternoon. Neither environment
    can explain the other's refusal away, and no #320 streak can count either — the queue was
    serialized behind an in-flight wildcard lane and the runner attempted no launch for over a day.
    """
    for spawners in (["watchdog", "operator"], ["runner", "watchdog"], ["runner", "operator"]):
        dsk = _attempts(["d26", "d29"], spawners=spawners, auth_probe=_AUTH_UNKNOWN)
        out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
        assert AUTH_DEATH in _reasons(out), spawners


def test_a_hold_a_narrower_class_mutes_is_never_a_SILENT_hold():
    """The muting rule reads the reason a specific class actually RAISES, not its detector flag.
    The anchor alert needs a FRESH launch pending; this class also counts an in-flight lane. On a
    machine with lanes in flight and nothing approved — the realized shape — muting on the flag left
    the queue held with nothing said at all, which is the one outcome worse than a vague name."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, launch_anchor={"ok": False},
                    issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk,
                 usage=_dark_meter())
    assert _reasons(out) != [], "a held queue must never be a silent one"
    assert AUTH_DEATH in _reasons(out), _reasons(out)


def test_an_unknown_probe_with_no_launch_failures_never_holds_anything():
    """THE refused-not-answered rule, in the owner's own words: `unknown` is a question the machine
    declined to answer, not a dead credential. It must never trip the hold by ITSELF — the loop
    reads `unknown` on healthy machines every day (the live probe reads it right now)."""
    dsk = disk(launch_anchor={"ok": True}, auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out), _reasons(out)
    assert [a["id"] for a in only(out, "launch")] == ["i5"], "and the queue keeps flying"


def test_an_unknown_probe_with_consecutive_failures_IS_the_verdict():
    """The other half of the same rule: N OBSERVED launch failures is EVIDENCE, and evidence is what
    tips a probe that would not answer. Here the meter is perfectly readable — the auth half of the
    disjunction carries it alone."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert AUTH_DEATH in _reasons(out), _reasons(out)
    assert only(out, "launch") == [] and only(out, "park") == []


def test_a_single_failed_flight_is_not_a_streak():
    """One sample cannot tell a broken lane from a broken machine — the same discriminator #320
    settled for the environment classes, applied to session ids."""
    dsk = _attempts(["d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out)


def test_a_repeat_of_the_SAME_session_id_is_one_sample_not_two():
    dsk = _attempts(["d26", "d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out)


def test_a_confirmed_HEALTHY_account_and_a_readable_meter_never_hold():
    """The conjunction is load-bearing in both directions. Flights that failed while the meter reads
    fine AND `claude auth status` positively reports the assigned account are not an auth death."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_HEALTHY)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


def test_a_more_specific_class_keeps_its_own_name_and_this_one_stays_quiet():
    """The backstop rule. A hold that arrives wearing another class's banner is the mis-blame #320
    exists to end; stapling this cruder name onto an episode a specific detector has ALREADY named
    precisely is the same harm from the other side — two names at once for one outage, and an owner
    sent to check two different things. Every specific hold is pinned here."""
    # a DEFINITIVE dead reading: #159's own auth_dead already holds and names it
    out = decide(parsed_issues=[parsed(5), parsed(6)],
                 dsk=_attempts(["i5", "d26"], auth_probe=_AUTH_DEAD))
    assert _reasons(out) == ["auth_dead"], _reasons(out)
    # #320's environment escalation, with an attempt streak standing beside it
    dsk = _env_streak("gh_auth_dead", ["i5", "i6"])
    dsk["launch_attempt_streak"] = {"samples": {"i5": ["runner"], "d26": ["watchdog"]}}
    dsk["auth_probe"] = _AUTH_UNKNOWN
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)], dsk=dsk)
    assert _reasons(out) == ["gh_auth_dead_workers"], _reasons(out)
    # a dead launch ANCHOR (#24), whose remedy is a cmux tab and nothing to do with an account
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, launch_anchor={"ok": False})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert AUTH_DEATH not in _reasons(out), _reasons(out)
    # ...and the CHANNEL streak (#153), which holds on its very first entry
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, launch_fail_ids=["i5"])
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


@pytest.mark.parametrize("rising", ["auth_dead", "anchor_down"])
def test_a_WORSENING_outage_never_journals_a_recovery(rising):
    """The edge that must key on EVIDENCE, not on the flag. When a more specific class rises — the
    auth probe finally answers "dead", or the anchor drops — this class stops naming the episode,
    and a recovery edge keyed on that flag would journal "launch delivery verified again" on the
    exact tick the outage was PROVEN, retract the alert, and page again when it re-armed. The streak
    itself clears on one thing, a verified delivery, which is what the record claims."""
    over = {"auth_probe": _AUTH_DEAD} if rising == "auth_dead" \
        else {"auth_probe": _AUTH_UNKNOWN, "launch_anchor": {"ok": False}}
    dsk = _attempts(["i5", "d26"], alert={"reasons": [AUTH_DEATH], "since": NOW - 600}, **over)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert only(out, "launch_recovered") == [], out


def test_a_probe_that_reads_healthy_does_NOT_lift_a_standing_hold():
    """The conjunct LATCHES once the episode is on disk. It is a 5-second `claude auth status`
    subprocess refreshed every 60 s that answers `unknown` on any timeout — and three of the faults
    this streak admits (`gh_auth_dead`, its runner sibling, `env_poisoned`) are things that probe
    and the usage meter cannot see at all, so their reading healthy is not evidence about them.
    Re-asked fresh every tick in the shape this class is FOR — the runner launching nothing, so no
    flight ever settles it — a flip raised, retracted and re-raised the page once a minute,
    journalling a lift each time and landing the launch-cap parks it had been suppressing."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_HEALTHY,
                    alert={"reasons": [AUTH_DEATH], "since": NOW - 600},
                    issues_state={"version": 1, "issues": {
                        "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP)}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert only(out, "launch_recovered") == [], out
    assert only(out, "clear_alert") == [] and only(out, "launch") == []
    assert only(out, "park") == [], "and the parks it suppresses stay suppressed"


def test_a_FLAPPING_probe_pages_once_and_stays_paged():
    """The same rule as a sequence, which is how it was found: twelve ticks of an alternating probe
    used to be six pages, five retractions and five journalled lifts on a machine where nothing had
    flown. Once the ALERT names the class, only the streak ends it."""
    seen = []
    alert = None
    for tick in range(6):
        dsk = _attempts(["i5", "d26"], alert=alert,
                        auth_probe=_AUTH_HEALTHY if tick % 2 else _AUTH_UNKNOWN)
        out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
        for a in only(out, "alert"):
            alert = {"reasons": a["reasons"], "since": NOW}
            seen.append(a["reasons"])
        assert only(out, "clear_alert") == [], tick
        assert only(out, "launch_recovered") == [], tick
    assert seen == [[AUTH_DEATH]], seen


def test_a_narrower_class_TAKING_the_naming_is_never_read_as_a_lift():
    """...and the guard on that record. A more specific class rising is a worsening outage, not an
    ending one — the queue stays held under its name, and journalling a lift there would be the
    fabricated-recovery defect wearing new words."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_DEAD,
                    alert={"reasons": [AUTH_DEATH], "since": NOW - 600})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert only(out, "launch_recovered") == [], out


def test_a_garbage_attempt_streak_never_holds_the_queue():
    """Fail OPEN on unreadable input — a wrongly-held queue is the bigger, quieter outage, and this
    view is data the runner writes."""
    for bad in (None, "x", 5, {}, {"samples": None}, {"samples": "i5"},
                {"ids": ["i5", "d26"], "spawners": ["runner", "watchdog"]},   # the old shape
                {"samples": {"i5": ["runner"], "d26": ["runner"]}},        # one spawner
                {"samples": {"i5": ["runner"], "i6": ["operator"]}},       # no worktree-free flight
                {"samples": {"i5": ["nope"], "d26": ["alsonope"]}},        # unknown spawners
                {"samples": {"i5": 1, "d26": 2}},
                {"samples": {"": ["runner"], "d26": ["watchdog"]}},
                {"samples": {"nope": ["runner"], "alsonope": ["watchdog"]}},
                {"samples": {"i5": ["runner"]}},
                # a REJECTED id may not contribute the second spawner
                {"samples": {"i5": ["runner"], "i6": ["runner"], "zzz": ["watchdog"]}},
                {"samples": {"i5": [["runner"]], "d26": ["watchdog"]}}):  # unhashable: must not raise
        dsk = _attempts(bad, auth_probe=_AUTH_UNKNOWN)
        out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
        assert AUTH_DEATH not in _reasons(out), bad


def test_the_hold_suppresses_the_per_issue_launch_cap_park():
    """A lane already AT its cap when the outage is recognised is held, not parked: it is one of the
    N re-approvals this whole layer exists to stop charging the owner."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, issues_state={
        "version": 1, "issues": {"i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP)}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert only(out, "park") == []


def test_the_loop_can_always_LEAVE_this_hold_even_with_nothing_to_probe():
    """The state nothing could end. On the realized machine there is no approved queue at all (it
    was serialized behind an in-flight wildcard lane), so the #115 probe has no candidate — its
    candidate set is issue lanes, and this streak can be made entirely of flights that are not. If
    the hold ALSO suppressed the recovery relaunch of an exited lane at its retry cap, nothing on
    the machine could fly and nothing could ever clear the streak. So the relaunch ladder is left
    exactly as it is: the lane below its cap relaunches (and a green one lifts the hold), and the
    lane at its cap still parks — one visible park the owner can re-approve beats a frozen loop."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN,
                    exited={"i5": 1}, issues_state={"version": 1, "issues": {
                        "i5": ist("running", retries=0, branch="sl/i5-x", num=5)}})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk,
                 usage=_dark_meter())
    assert only(out, "recover") != [], "a lane with retries left still flies — that is the exit"
    # ...and at the cap it parks rather than vanishing into the hold
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN,
                    exited={"i5": 1}, issues_state={"version": 1, "issues": {
                        "i5": ist("running", retries=99, branch="sl/i5-x", num=5)}})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk,
                 usage=_dark_meter())
    assert [a["id"] for a in only(out, "park")] == ["i5"], out


def test_the_auth_death_hold_probes_itself_with_the_existing_canary():
    """The hold cannot clear itself — nothing launches while it stands, and only a launch can prove
    the machine came back. #115's canary is the recovery probe, reused unchanged."""
    dsk = _attempts(["i5", "d26"], fail_at=NOW - actions.CANARY_RETRY_SECONDS - 1,
                    auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    probes = only(out, "launch")
    assert len(probes) == 1 and probes[0]["canary"] is True, out
    assert only(out, "park") == []


def test_a_recovery_lifts_the_hold_and_relabels_nothing():
    """The exit edge: the streak cleared on a verified delivery (a green probe, or a launch that
    simply worked) while the durable ALERT still names the class. One journal record, the alert
    retracts itself, the queue resumes — no issue relabeled, no re-approval asked for.

    NOTE the probe is still `unknown` here. The streak clearing is what lifts this hold; an auth
    reading that never answers must not be able to keep it standing."""
    dsk = disk(launch_anchor={"ok": True}, launch_attempt_streak={"samples": {}, "delivered": True},
               auth_probe=_AUTH_UNKNOWN,
               alert={"reasons": [AUTH_DEATH], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    assert len(only(out, "launch_recovered")) == 1
    assert only(out, "clear_alert"), "the hold's alert must retract itself"
    assert [a["id"] for a in only(out, "launch")], "and launching resumes on its own"
    assert only(out, "park") == [] and only(out, "relabel") == []


def test_a_still_held_auth_death_does_not_journal_a_recovery():
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN,
                    alert={"reasons": [AUTH_DEATH], "since": NOW - 600})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert only(out, "launch_recovered") == []


def test_an_idle_machine_is_never_paged_for_a_hold_it_is_being_denied_nothing_by():
    """Gated on real demand, exactly as the anchor and auth_dead alerts are. With no approved queue
    and no lane in flight, nothing is waiting on this hold — and a page the owner cannot act on
    teaches them to ignore the next one. The moment work is approved, the page (and the recovery
    probe that lifts it) arrive together."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


def test_an_in_flight_lane_alone_is_demand_enough_to_be_told():
    """...and the 2026-08-26 shape is exactly that case: no approved queue (it was serialized behind
    a wildcard lane), but lanes in flight whose dead sessions would be relaunched into the wall."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN,
                    issues_state={"version": 1, "issues": {"i5": ist("running")}})
    out = decide(parsed_issues=[parsed(5, labels=("in-progress", "type:build"))], dsk=dsk,
                 usage=_dark_meter())
    assert AUTH_DEATH in _reasons(out), _reasons(out)


def test_the_usage_fail_open_decision_is_untouched_and_the_detector_composes_with_it():
    """The DoD's boundary, driven together. The dark meter still FAILS OPEN (its own alert stands,
    its own episode record is journaled, the scheduler is still told to launch normally) — this
    detector sits ABOVE that decision and holds for a reason of its own, naming BOTH."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert "usage_stale" in _reasons(out), "the fail-open alert is not suppressed"
    assert AUTH_DEATH in _reasons(out), "and the hold names itself beside it"
    assert len(only(out, "fail_open")) == 1, "the fail-open episode is still journaled"
    # ...and the composition is one page, not two: both reasons ride ONE alert record.
    assert len(only(out, "alert")) == 1


def test_the_existing_env_escalation_is_untouched_by_the_new_class():
    """#320's gh-auth class still holds under ITS name on two distinct lanes, with no attempt
    streak published at all — the new detector adds a class, it does not re-plumb the old ones."""
    out = decide(parsed_issues=[parsed(5), parsed(6), parsed(7)],
                 dsk=_env_streak("gh_auth_dead", ["i5", "i6"]))
    assert _reasons(out) == ["gh_auth_dead_workers"], _reasons(out)


def test_the_status_line_and_the_morning_report_name_the_auth_death_hold():
    """A paused queue is not an idle one — and this is the class that spent a whole day looking
    exactly like one."""
    line = actions.queue_hold_line({"reasons": [AUTH_DEATH], "since": NOW})
    assert line.startswith("queue: HELD") and AUTH_DEATH in line
    view = {"date": "2026-08-26", "now": NOW, "frozen": None, "queue": [], "usage": None,
            "queue_hold": {"reasons": [AUTH_DEATH], "since": NOW - 3600}}
    text = report_lib.morning([], view, {}, cfg())
    assert "Nothing happened overnight" not in text
    assert AUTH_DEATH in text


# --------------- the runner half: what the runner could not see before ----------------------
#
# The streak is DERIVED from the journal once per tick, not accumulated in memory. That is the
# choice these tests exist to protect: an in-memory streak has to be reconciled with the journal
# for the launches this process did not run, and every incremental reconciliation this issue tried
# — a byte offset, a timestamp watermark, a lagged one — had a boundary at which a VERIFIED
# DELIVERY fell outside the window while the refusals it answered fell inside, which re-holds a
# machine that just launched a session. A pure function of a bounded window has no such boundary.


def _oop(rig, act, outcome, sid, ts=NOW, **extra):
    """Journal the record a FOREIGN spawner writes — the watchdog's own check process, the owner's
    `superlooper debug` tap, `superlooper resume`. None of them runs inside the runner, so the
    journal is the only place it can ever learn a launch was attempted at all."""
    import journal as journal_mod
    journal_mod.append(str(rig.home), dict({"act": act, "outcome": outcome, "id": sid}, **extra), ts)


def _mine(rig, act, outcome, sid, ts=NOW, **extra):
    """Journal one of the RUNNER's own launch outcomes, in the shape `_journal_outcome` writes."""
    import journal as journal_mod
    journal_mod.append(str(rig.home), dict({"act": act, "outcome": outcome, "id": sid}, **extra), ts)


def _fly(rig, action, now):
    """Execute one launch action AND journal its outcome, the way the tick does — the record's
    stamped `evidence` (#152) is what the derivation classifies the runner's own refusals from."""
    rig.r._journal_outcome(action, rig.r._execute(action, now), now)


def _streak(rig, now=NOW + 20):
    return rig.r._launch_attempt_streak(now)


def test_a_watchdog_debugger_launch_failure_feeds_the_attempt_streak(rig):
    """d26/d27/d28 on 2026-08-25: three unattended repair flights refused in ten minutes, and the
    systemic layer never heard about one of them. rc=5 is the runner's own gh — a credential."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5, signals=["alert"])
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=5, signals=["alert"])
    assert _streak(rig) == {"samples": {"d26": ["watchdog"], "d27": ["watchdog"]},
                            "delivered": False}


def test_an_owner_tapped_debug_launch_failure_feeds_the_attempt_streak(rig):
    """d29 on 2026-08-26: the owner tapped Debug in the command center and the launcher refused with
    CLAUDE IDENTITY REFUSED. A person watching it fail is the loudest sample there is."""
    rig.r.tick(now=NOW)
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 1, rc=7, operator="willprout",
         source="command-center", error="[d29] CLAUDE IDENTITY REFUSED in the session's own env")
    assert _streak(rig) == {"samples": {"d29": ["operator"]}, "delivered": False}


def test_a_RESUME_is_a_foreign_spawner_however_its_id_is_shaped(rig):
    """THE reason the two sides are split by spawner and never by id shape. `superlooper resume`
    carries a lane's own `i<N>`, but it is shelled from an operator's shell or the dashboard — so
    its refusal reads THAT environment, the very one the flight side exists to be independent of.
    Counted as a lane, one poisoned shell would produce both halves on its own and freeze a healthy
    queue under a page saying nothing on the machine can start a session."""
    rig.r.tick(now=NOW)
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 1, rc=6,
         error="[d29] ENV POISONED: ANTHROPIC_API_KEY survived the scrub")
    _oop(rig, "resume", "resume_failed", "i104", ts=NOW + 2, rc=6, session_id="abc",
         error="[i104] ENV POISONED: ANTHROPIC_API_KEY survived the scrub")
    assert _streak(rig)["samples"] == {"d29": ["operator"], "i104": ["operator"]}, "one environment"
    out = decide(parsed_issues=[parsed(5), parsed(6)], usage=_dark_meter(),
                 dsk=disk(launch_anchor={"ok": True}, auth_probe=_AUTH_UNKNOWN,
                          launch_attempt_streak=_streak(rig)))
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


def test_a_successful_resume_still_clears_the_streak(rig):
    """Its success is proof the machine can start a session, and a hold left standing over a machine
    the owner has just visibly resumed work on is the failure this whole class is about."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "resume", "resumed", "i104", ts=NOW + 2, session_id="abc")
    assert _streak(rig) == {"samples": {}, "delivered": True}


def test_a_DELIVERY_CHANNEL_refusal_never_enters_this_streak(rig):
    """The mis-blame this filter exists to prevent, and it is not hypothetical: the watchdog mints a
    FRESH d<N> for every retry of one episode, so an ordinary dead-cmux-pane outage produces two
    distinct session ids five minutes apart BY DESIGN. Admitted here it would hold the queue under a
    credential's banner and send the owner to go log back into Claude over a missing tab."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=2, signals=["alert"])
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=124, signals=["alert"])
    assert _streak(rig)["samples"] == {}


def test_a_PER_ISSUE_refusal_never_enters_this_streak(rig):
    """The same guard on the runner's own launches. A worktree that would not create and a brief
    that could not be written are two lanes with problems of their own — paging the owner "your
    Claude login has expired" over a stale git index.lock is the exact mis-blame."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(1, "[i101] could not create the worktree"))
    _fly(rig, _launch_action(), NOW + 1)
    assert _streak(rig)["samples"] == {}
    assert issue_state(rig, "i101")["launch_failures"] == 1, "it still parks on its own schedule"


def test_a_CANARY_probes_refusal_is_never_a_sample(rig):
    """#320's rule, rounds 2 and 3, and it applies here for the same reason: a probe only ever runs
    while a hold already stands, and it can refuse for a fault of the LANE it happened to probe —
    which would enter this streak as though the MACHINE had produced it, and keep a wrongly-held
    queue held by its own probing. A probe is not a sample."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(6, "[i101] ENV POISONED: ANTHROPIC_API_KEY survived"))
    _fly(rig, dict(_launch_action(), canary=True), NOW + 1)
    assert _streak(rig)["samples"] == {}


def test_a_record_with_no_readable_rc_is_dropped_rather_than_guessed_at(rig):
    """A refusal we cannot NAME is not evidence about credentials, and admitting it would let an
    unnamed fault wear a credential's remedy."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, signals=["alert"])
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc="five", signals=["alert"])
    assert _streak(rig)["samples"] == {}


def test_the_runners_OWN_launch_failure_feeds_the_lane_side(rig):
    """One streak, whoever attempted the launch — otherwise the same outage would need two. The
    runner's own records already carry the classified evidence (#152), so nothing is re-derived."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(7, "[i101] CLAUDE IDENTITY REFUSED: not logged in"))
    _fly(rig, _launch_action(), NOW + 1)
    assert _streak(rig) == {"samples": {"i101": ["runner"]}, "delivered": False}


@pytest.mark.parametrize("rec", [
    {"act": "launch", "id": "i101", "outcome": "ok"},
    {"act": "recover", "id": "i101", "tier": "exited", "outcome": "ok"},
    {"act": "resolve_conflict", "id": "i101", "outcome": "ok"},
])
def test_every_verified_delivery_this_runner_makes_clears_the_streak(rec, rig):
    """All three launch paths, read back from the journal so a RESTART sees them too. The recovery
    relaunch is the load-bearing one: the machine-wide hold deliberately does not suppress it — on
    an all-in-flight machine it is the only flight left, and the #115 probe's candidates are issue
    lanes this streak may contain none of."""
    import journal as journal_mod
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=8)
    assert sorted(_streak(rig)["samples"]) == ["d26", "d27"]
    journal_mod.append(str(rig.home), rec, NOW + 30)
    assert _streak(rig, NOW + 40) == {"samples": {}, "delivered": True}


@pytest.mark.parametrize("tier", ["idle", "frozen", None])
def test_a_recover_that_only_NUDGED_clears_nothing(tier, rig):
    """...and the tier is what tells them apart. `recover` reads outcome "ok" for a delivered NUDGE
    as well as for a relaunch, and an i336 lane whose auth died in-process answers nudges all day
    while starting nothing. Clearing on that would disarm the detector during exactly the outage it
    exists for."""
    import journal as journal_mod
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=8)
    journal_mod.append(str(rig.home), {"act": "recover", "id": "i101", "tier": tier,
                                       "outcome": "ok"}, NOW + 30)
    assert sorted(_streak(rig, NOW + 40)["samples"]) == ["d26", "d27"]


def test_a_verified_delivery_BETWEEN_two_refusals_breaks_the_streak(rig):
    """"Consecutive, with no success between them" is the whole claim, and the derivation reads the
    journal in FILE order to settle it — the order things actually finished, one O_APPEND write per
    record from every writer."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "debug_launch", "launched", "d27", ts=NOW + 2, operator="willprout")
    _oop(rig, "watchdog", "launch_failed", "d28", ts=NOW + 3, rc=5)
    assert sorted(_streak(rig)["samples"]) == ["d28"], "the success clears d26 and only d26"


def test_a_delivery_WRITTEN_LATE_BUT_STAMPED_EARLY_still_clears_the_streak(rig):
    """The defect that killed every incremental design this issue tried. This runner stamps its
    journal records with the clock the TICK started on, and a tick may spend minutes in launches and
    nudges before writing them — so its verified delivery can be written after, and carry a stamp
    well before, refusals another process recorded meanwhile. Any reader that ORDERS by `ts`, or
    that skips records at a `ts` threshold, eventually cuts between a delivery and the refusals it
    answered: the refusals come back, the streak re-forms, and a machine that verifiably launched a
    session is re-held and paged over. Reading the whole window in file order cannot do that,
    however far the stamps are apart."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 3000, rc=5)
    _oop(rig, "resume", "resume_failed", "i104", ts=NOW + 3001, rc=7, session_id="abc")
    assert sorted(_streak(rig, NOW + 3100)["samples"]) == ["d26", "i104"]
    _mine(rig, "launch", "ok", "i101", ts=NOW)         # stamped 3000s earlier, written last
    for at in (NOW + 3100, NOW + 3300, NOW + 9000):
        assert _streak(rig, at) == {"samples": {}, "delivered": True}, at


def test_the_streak_NEVER_expires_on_a_clock_however_long_the_outage_runs(rig):
    """The fabricated-recovery trap, and the reason this streak has no clock at all. While a hold
    stands the ONLY thing that flies is the #115 canary, whose refusals are deliberately not samples
    — so nothing refreshes the evidence. Under any time window the streak therefore ages below the
    cap MID-OUTAGE, and decide reads that fall as recovery: it journals "launch delivery verified
    again" over a machine that verified nothing, retracts the alert, resumes, and walks the at-cap
    lanes into the parks the hold had been suppressing. The real 2026-08-26 journal settles it too —
    its two independent samples were TWENTY HOURS apart."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 2, rc=7)
    for at in (NOW + 3600, NOW + 6 * 3600 + 60, NOW + 20 * 3600, NOW + 7 * 24 * 3600):
        assert _streak(rig, at)["samples"] == {"d26": ["watchdog"], "d29": ["operator"]}, at


def test_only_a_VERIFIED_DELIVERY_ever_clears_it(rig):
    """...which is the other half of having no clock: the one thing that ends the streak is the one
    thing the alert claims when it retracts."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 2, rc=7)
    _oop(rig, "watchdog", "launched", "d30", ts=NOW + 8 * 24 * 3600)
    assert _streak(rig, NOW + 8 * 24 * 3600 + 1) == {"samples": {}, "delivered": True}


def test_a_RESTART_reconstructs_the_whole_streak_including_its_lane_side(rig):
    """A fresh process holds no streak in memory and rebuilds it from the journal — BOTH sides. An
    earlier design read only the foreign half back, so a restart mid-outage dropped the lane side,
    the two-sided test failed, and decide read that fall as a recovery: the alert retracted and the
    queue relaunched into the wall."""
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(runner_mod.ScriptRC(7, "[i101] CLAUDE IDENTITY REFUSED: not logged in"))
    _fly(rig, _launch_action(), NOW + 1)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 2, rc=5)
    reborn = type(rig.r)(repo=str(rig.repo), config=rig.r.config, state_home=str(rig.home),
                         pane="pane-1", run_script=lambda *a, **k: 0,
                         fetch_usage=lambda: {"auth_status": "ok"})
    assert reborn._launch_attempt_streak(NOW + 20)["samples"] == {
        "d26": ["watchdog"], "i101": ["runner"]}


def test_an_unrelated_journal_record_is_never_read_as_a_verified_delivery(rig):
    """The derivation walks EVERY record in the window, and most of them are not launches at all.
    One with no `outcome` field must not match a missing table row and read as a session that
    started — it did, once, and silently emptied the streak on the tick the hold should have
    tripped."""
    import journal as journal_mod
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=5)
    for rec in ({"act": "brief_comments", "id": "i101", "num": 101, "fetched": 1},
                {"act": "alert", "reasons": ["usage_stale"], "outcome": "ok"},
                {"act": "watchdog", "id": "d99"},                  # a launch act, no outcome
                {"act": "resume", "id": "i5", "outcome": None}):
        journal_mod.append(str(rig.home), rec, NOW + 3)
    assert sorted(_streak(rig)["samples"]) == ["d26", "d27"]


def test_a_record_with_an_unusable_TIMESTAMP_costs_a_sample_and_nothing_else(rig):
    """json.loads accepts Infinity, clocks step, state homes get restored. An unusable stamp cannot
    be placed in the window, so the record is skipped — never a hold, never a silent detector."""
    rig.r.tick(now=NOW)
    with open(rig.home / "journal.jsonl", "a") as f:
        f.write('{"ts": Infinity, "act": "event", "event": {"type": "x"}}\n')
    import journal as journal_mod
    journal_mod.append(str(rig.home), {"act": "event", "event": {"type": "y"}}, NOW + 86400)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 30, rc=5)
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 31, rc=8)
    assert sorted(_streak(rig, NOW + 40)["samples"]) == ["d26", "d27"]


def test_the_attempt_streak_is_published_for_decide_to_read(rig, monkeypatch):
    """decide is PURE — it can only see what the tick hands it, and it must be handed BOTH sides:
    the split cannot be read off the id (see the resume test above)."""
    seen = {}
    real = actions.decide

    def spy(now, config, usage, parsed_issues, lane_state, events, dsk, gh_view, **kw):
        seen.update(dsk)
        return real(now, config, usage, parsed_issues, lane_state, events, dsk, gh_view, **kw)

    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d27", ts=NOW + 2, rc=5)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 3, rc=5)
    monkeypatch.setattr(actions, "decide", spy)
    rig.r.tick(now=NOW + 20)
    assert seen["launch_attempt_streak"] == {
        "samples": {"d26": ["watchdog"], "d27": ["watchdog"]}, "delivered": False}


# ------------------------- the arc: the 2026-08-26 outage, replayed -------------------------

@pytest.fixture
def auth_outage(rig):
    """The realized 2026-08-26 machine, in the shape that made it invisible.

    ONE lane is approved — the queue that day was serialized behind a wildcard in-flight lane, so
    only one issue could ever be sampled. The usage meter is dark (every fetch refuses) and the auth
    probe answers `unknown`: refused, not dead, exactly as it did. Every launch refuses the way the
    owner's own Debug tap did that afternoon: CLAUDE IDENTITY REFUSED, this environment is not
    logged in to Claude.

    That is the whole trap. #320's environment class needs TWO distinct lanes refusing the same way
    and there is only one to give it; the channel class never sees an environment fault at all; and
    the lane's own launch cap is two attempts, so the single approved issue simply parks and the
    loop goes quiet over a machine that cannot start a session at all.
    """
    rows = json.loads((rig.fixdir / "issue_list.json").read_text())
    for row in rows:
        if row["number"] != 101:
            row["labels"] = [{"name": "type:build"}, {"name": "needs-owner"}]
    (rig.fixdir / "issue_list.json").write_text(json.dumps(rows))

    fault = {"rc": 7}
    launched = []
    inner = rig.r._run_script

    def run_script(args, env=None, timeout=None):
        a = [str(x) for x in args]
        if a and a[0].endswith("launch-session.py"):
            launched.append(a[1])
            if fault["rc"]:
                inner(args, env=env, timeout=timeout)       # still recorded in rig.calls
                return runner_mod.ScriptRC(
                    fault["rc"],
                    "[%s] CLAUDE IDENTITY REFUSED in the session's own environment — this "
                    "environment is not logged in to Claude" % a[1])
        return inner(args, env=env, timeout=timeout)

    rig.r._run_script = run_script
    rig.r._fetch_usage = lambda: {"auth_status": "api_error", "five_hour_pct": None,
                                  "seven_day_pct": None}
    rig.r._probe_auth = lambda: dict(_AUTH_UNKNOWN)
    rig.fault = fault
    rig.launched = launched
    return rig


def _agent_ready_removed(rig):
    return [m for m in mutations(rig)
            if m.get("kind") == "set_labels" and "agent-ready" in (m.get("remove") or [])]


def test_the_2026_08_26_outage_holds_the_queue_and_resumes_on_its_own(auth_outage):
    """THE acceptance fact, replaying the day the hold was supposed to trip and did not.

    A dark meter (failing open — its own recorded decision, untouched here), an auth probe that will
    not answer, one lane refusing for the SESSION's Claude account and one unattended repair flight
    refusing for the RUNNER's gh: two distinct session ids that could not start, one of them a
    flight the queue does not own, neither anywhere near a cap of its own. ONE alert, zero parks,
    nothing relabeled, and no launch but the recovery probe while it stands — and when the owner
    logs back in, the probe finds out by itself.
    """
    rig = auth_outage
    t = NOW
    rig.r.tick(now=t)                                   # the meter's first (failed) read
    while t < NOW + actions.USAGE_FAIL_OPEN_GRACE_SECONDS + 60:
        t += 900                                        # ...and it stays dark past the grace, in
        rig.r.tick(now=t)                               # steps under the wake-gap threshold
    assert _journal(rig, "fail_open"), "the usage fail-open decision is untouched"

    # the meter is failing open now, so the one approved lane flies — and refuses
    assert rig.launched == ["i101"], rig.launched
    assert issue_state(rig, "i101")["launch_failures"] == 1, "the lane's own cap still ticks"
    assert AUTH_DEATH not in (_alert_file(rig) or {}).get("reasons", []), \
        "one sample cannot tell a broken lane from a broken machine"

    # ...and the watchdog's unattended repair flight refuses too, from its own process and for a
    # DIFFERENT credential. That is the second distinct session — and the one the queue does not
    # own, which is what makes this the machine rather than a worktree.
    _oop(rig, "watchdog", "launch_failed", "d26", ts=t + 1, rc=5, signals=["alert"])
    t += 20
    rig.r.tick(now=t)

    # --- HELD ---
    alert = _alert_file(rig)
    assert AUTH_DEATH in alert["reasons"], alert
    assert len([a for a in _journal(rig, "alert")
                if AUTH_DEATH in (a.get("reasons") or [])]) == 1, _journal(rig, "alert")
    assert actions.queue_hold_line(alert).startswith("queue: HELD")

    # --- nothing parked, nothing relabeled, and the ONLY launch is the recovery probe ---
    flights = list(rig.launched)
    t += actions.CANARY_RETRY_SECONDS + 30              # long enough for the canary to be DUE...
    rig.r.tick(now=t)
    assert rig.launched == flights + ["i101"], "one probe, which refuses and re-enters the hold"
    probes = list(rig.launched)
    for _ in range(3):                                  # ...and then nothing again until the next
        t += 20
        rig.r.tick(now=t)
    st = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    assert [i for i, d in st.items() if d.get("status") == "parked"] == []
    assert _journal(rig, "park") == []
    assert _agent_ready_removed(rig) == [], "no re-approval is owed"
    assert rig.launched == probes, "a held queue attempts no launch but its probe"
    assert issue_state(rig, "i101")["launch_failures"] == 1, "and the probe charged the lane nothing"
    assert len([a for a in _journal(rig, "alert")
                if AUTH_DEATH in (a.get("reasons") or [])]) == 1, "it pages exactly once"

    # --- the owner logs back in. The auth probe still says `unknown` — the STREAK clearing is what
    #     lifts this hold, and a probe that never answers must not be able to keep it standing.
    rig.fault["rc"] = 0
    t += actions.CANARY_RETRY_SECONDS + 30
    rig.r.tick(now=t)                                   # the canary probe, which now flies
    assert len(rig.launched) == len(probes) + 1, "exactly one probe, not a re-storm"
    t += 20
    rig.r.tick(now=t)                                   # the tick that sees the streak cleared

    assert len(_journal(rig, "launch_recovered")) == 1
    assert AUTH_DEATH not in ((_alert_file(rig) or {}).get("reasons") or [])
    assert _journal(rig, "park") == [], "and it parked nothing on the way out either"
    assert loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]["i101"]["status"] \
        == "running", "the queue resumed on its own"


@pytest.fixture
def serialized_outage(rig):
    """The 2026-08-26 machine as the journal actually records it: NO approved queue at all (it was
    serialized behind a wildcard in-flight lane that never exited), one lane in flight, a dark meter
    and an auth probe answering `unknown`. The runner attempts no launch of its own for the whole
    outage — every flight on the machine is somebody else's process."""
    rows = json.loads((rig.fixdir / "issue_list.json").read_text())
    for row in rows:
        row["labels"] = [{"name": "type:build"},
                         {"name": "in-progress" if row["number"] == 101 else "needs-owner"}]
    (rig.fixdir / "issue_list.json").write_text(json.dumps(rows))
    launched = []
    inner = rig.r._run_script

    def run_script(args, env=None, timeout=None):
        a = [str(x) for x in args]
        if a and a[0].endswith("launch-session.py"):
            launched.append(a[1])
        return inner(args, env=env, timeout=timeout)

    rig.r._run_script = run_script
    rig.r._probe_auth = lambda: dict(_AUTH_UNKNOWN)
    rig.launched = launched
    return rig


def test_the_DARK_METER_half_carries_it_end_to_end_on_its_own(serialized_outage):
    """The disjunction's other half, driven rather than asserted at the boundary. Here the auth
    probe answers cleanly — the account is fine as far as anything can tell — and it is the meter's
    silence past the fail-open grace that makes "the machine cannot start a session" the reading.
    Three of the faults this streak admits are things `claude auth status` cannot see at all."""
    rig = serialized_outage
    rig.r._probe_auth = lambda: dict(_AUTH_HEALTHY)
    rig.r._fetch_usage = lambda: {"auth_status": "api_error", "five_hour_pct": None,
                                  "seven_day_pct": None}
    rig.r._usage["first_attempt_at"] = NOW - 2 * actions.USAGE_FAIL_OPEN_GRACE_SECONDS
    rig.r.tick(now=NOW)
    assert _journal(rig, "fail_open"), "the meter is dark past its grace, failing open as designed"
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 2, rc=7)
    rig.r.tick(now=NOW + 20)
    assert AUTH_DEATH in (_alert_file(rig) or {}).get("reasons", []), _alert_file(rig)
    assert _journal(rig, "park") == []


def test_the_realized_shape_end_to_end_no_launch_of_the_runners_own(serialized_outage):
    """THE incident, driven through real ticks in the shape that made it invisible: the runner
    launches nothing, and the only evidence is a watchdog repair flight and an owner's Debug tap
    refusing from their own processes. #320's streaks stay empty throughout — they count launches
    this process ran. The demand that makes it worth saying is an in-flight lane, not a queue."""
    rig = serialized_outage
    rig.r.tick(now=NOW)                                 # the in-flight lane resumes; nothing queued
    assert _alert_file(rig) is None
    flown = list(rig.launched)
    relabels = len(_agent_ready_removed(rig))           # the resume's own, before the hold

    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5, signals=["alert"])
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 2, operator="willprout",
         source="command-center",
         error="[d29] CLAUDE IDENTITY REFUSED in the session's own environment")
    rig.r.tick(now=NOW + 20)

    alert = _alert_file(rig)
    assert AUTH_DEATH in (alert or {}).get("reasons", []), alert
    assert len([a for a in _journal(rig, "alert")
                if AUTH_DEATH in (a.get("reasons") or [])]) == 1
    assert _journal(rig, "park") == []
    assert len(_agent_ready_removed(rig)) == relabels, "the hold itself moves no label"
    assert rig.launched == flown, "and the runner attempts nothing of its own while it stands"
    assert rig.r._launch_fail_ids == set() and rig.r._launch_env_fail_ids == {}, \
        "#320's own streaks stay empty — this is the evidence they cannot count"

    # ...and it lifts the moment any flight flies, whoever flew it
    _oop(rig, "debug_launch", "launched", "d30", ts=NOW + 30, operator="willprout")
    rig.r.tick(now=NOW + 40)
    assert len(_journal(rig, "launch_recovered")) == 1
    assert AUTH_DEATH not in ((_alert_file(rig) or {}).get("reasons") or [])
    assert _journal(rig, "park") == []


def test_the_2026_08_26_journal_shape_is_what_this_detector_actually_reads(rig):
    """The realized incident, record for record, in the shapes the real journal holds — including
    the owner-tapped one, which carries NO rc at all and is classifiable only from the launcher's
    own line. Requiring an rc made the entire existing journal invisible to this detector, and
    every record written by a CLI not yet republished would stay invisible after."""
    rig.r.tick(now=NOW)
    for n, sid in enumerate(("d26", "d27", "d28")):
        _oop(rig, "watchdog", "launch_failed", sid, ts=NOW + n + 1, rc=5, signals=["alert"])
    _oop(rig, "debug_launch", "launch_failed", "d29", ts=NOW + 10, operator="willprout",
         source="command-center",
         error="[d29] CLAUDE IDENTITY REFUSED in the session's own environment — the flight was "
               "refused before it started.\n[d29] this environment is not logged in to Claude "
               "(`loggedIn` is False, authMethod 'none')")
    streak = _streak(rig, NOW + 20)
    assert streak["samples"] == {"d26": ["watchdog"], "d27": ["watchdog"],
                                 "d28": ["watchdog"], "d29": ["operator"]}, streak
    out = decide(parsed_issues=[parsed(5)], usage=_dark_meter(),
                 dsk=disk(launch_anchor={"ok": True}, auth_probe=_AUTH_UNKNOWN,
                          launch_attempt_streak=streak))
    assert AUTH_DEATH in _reasons(out), "the day this layer was written for must trip it"


def test_the_streak_reason_registry_matches_the_classifier_it_mirrors():
    """A hand-copied set in a different module from the table it mirrors. Pinned the way #320 pins
    its own registry: every reason here must be one `lib/evidence` can actually emit, or the filter
    silently admits nothing and the class quietly stops working."""
    import evidence as evidence_mod
    emitted = {r for r, _ in evidence_mod._LAUNCH_RC.values()}
    assert runner_mod.AUTH_DEATH_STREAK_REASONS <= emitted, \
        runner_mod.AUTH_DEATH_STREAK_REASONS - emitted
    # ...and the other direction, which is #320's own promise: "adding a class is this frozenset
    # plus one row in LAUNCH_ALERT_REASONS. Nothing about the detector, the hold, the recovery probe
    # or the resume edge changes." A fourth escalatable reason added on those instructions must feed
    # this streak too, or it silently would not.
    assert evidence_mod.SYSTEMIC_ESCALATION_REASONS <= runner_mod.AUTH_DEATH_STREAK_REASONS, \
        evidence_mod.SYSTEMIC_ESCALATION_REASONS - runner_mod.AUTH_DEATH_STREAK_REASONS
    # ...and none of them is a CHANNEL fault: those hold on their first entry under their own name.
    assert not (runner_mod.AUTH_DEATH_STREAK_REASONS & evidence_mod.CHANNEL_FAULT_REASONS
                - {"gh_auth_dead_runner", "claude_identity_wrong_runner"})


def test_every_spawner_the_runner_can_name_is_one_decide_will_accept():
    """The two halves of the spawner vocabulary live in different modules — the runner names them,
    decide vets them — so a name added on one side and not the other would count as no spawner at
    all and silently disarm the two-spawner rule."""
    named = {runner_mod.SPAWNER_RUNNER} | {row[2] for row
                                           in runner_mod.FOREIGN_LAUNCH_OUTCOMES.values()}
    assert named <= actions.AUTH_DEATH_SPAWNERS, named - actions.AUTH_DEATH_SPAWNERS
    assert len(named) >= actions.AUTH_DEATH_SPAWNER_CAP, "the rule must be satisfiable at all"


def test_the_QUEUE_EMPTYING_is_never_read_as_the_machine_coming_back():
    """The third way a conjunct can fall, and the one that is not evidence at all. The runner stops
    feeding an auth probe the moment no spend is pending, so a class that kept evaluating its
    conjunct through that gap would read the probe's DISAPPEARANCE as the account confirming
    itself — ending the hold and journaling a lift because the queue emptied, on a machine still
    refusing every flight. The hold retracts its page then (as the anchor and auth_dead alerts
    beside it do), and says nothing it cannot stand behind."""
    dsk = _attempts(["i5", "d26"], alert={"reasons": [AUTH_DEATH], "since": NOW - 600})
    dsk.pop("auth_probe", None)                        # no demand -> the runner feeds no probe
    out = decide(parsed_issues=[], dsk=dsk)
    assert only(out, "launch_recovered") == [], out
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


def test_and_it_re_raises_when_work_is_approved_again_and_nothing_has_flown():
    """...and the price of that gate, stated: one extra page across an outage that spans an empty
    queue. The streak is unchanged — only a delivery clears it — so the moment there is something to
    hold, the hold is said again."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, alert=None)
    out = decide(parsed_issues=[parsed(5)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH in _reasons(out), _reasons(out)


def test_a_320_hold_lifting_via_RESTART_still_journals_its_own_recovery():
    """The edges must not be shared, and a restart is what proves it. #320's streaks live in memory
    and reset with the process; this class's is derived from the journal and does not — and the two
    co-occur by design, because #320's escalatable reasons are members of this streak's family and
    its own standing ALERT is a watchdog signal that opens the episode supplying the second spawner.
    Sharing one exit edge meant a #320 hold lifting via the documented #24 restart fallback
    journalled nothing at all, while its record's own text names that very case."""
    dsk = disk(launch_anchor={"ok": True}, launch_env_fail_ids={},   # the restart cleared #320's
               launch_attempt_streak={"samples": {"i5": ["runner"], "d26": ["watchdog"]}},
               auth_probe=_AUTH_UNKNOWN,
               alert={"reasons": ["gh_auth_dead_workers"], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk)
    recs = only(out, "launch_recovered")
    assert len(recs) == 1, out
    assert "launch delivery verified again" in recs[0]["reason"], recs
    # ...and the tick is honest about what it hands over: the attempt streak the restart could not
    # clear still stands, so the queue is held again immediately — under THIS class's name.
    assert _reasons(out) == [AUTH_DEATH], _reasons(out)


def test_this_classs_own_streak_clearing_journals_a_recovery_of_its_own():
    """...and its own edge says what actually happened: a session started. It may make that claim —
    a verified delivery is the only thing that clears this streak."""
    dsk = disk(launch_anchor={"ok": True}, launch_attempt_streak={"samples": {}, "delivered": True},
               auth_probe=_AUTH_UNKNOWN,
               alert={"reasons": [AUTH_DEATH], "since": NOW - 600},
               issues_state={"version": 1, "issues": {}})
    out = decide(parsed_issues=[parsed(5)], dsk=dsk)
    recs = only(out, "launch_recovered")
    assert len(recs) == 1 and "a session started again" in recs[0]["reason"], out
    assert only(out, "clear_alert") and only(out, "park") == [] and only(out, "relabel") == []




def test_a_SECOND_spawner_refusing_the_same_session_is_more_evidence_not_less(rig):
    """The map keeps every spawner that read a refusal of a session, never just the last. The
    natural sequence makes the difference: the runner cannot relaunch a lane, so the owner hand-
    `resume`s it — two environments refusing the same id. Overwritten, that collapses two spawners
    to one, the streak drops below its own threshold, and a SECOND refusal quietly ends the hold."""
    import journal as journal_mod
    rig.r.tick(now=NOW)
    _oop(rig, "debug_launch", "launch_failed", "d26", ts=NOW + 1, rc=7)
    journal_mod.append(str(rig.home), {
        "act": "recover", "id": "i5", "tier": "exited", "outcome": "relaunch rc=7",
        "evidence": {"kind": "launch", "rc": 7, "reason": "claude_identity_wrong",
                     "captured": "x"}}, NOW + 2)
    assert _streak(rig)["samples"] == {"d26": ["operator"], "i5": ["runner"]}
    _oop(rig, "resume", "resume_failed", "i5", ts=NOW + 3, rc=7, session_id="abc")
    assert _streak(rig)["samples"] == {"d26": ["operator"], "i5": ["operator", "runner"]}
    out = decide(parsed_issues=[parsed(5)], usage=_dark_meter(),
                 dsk=disk(launch_anchor={"ok": True}, auth_probe=_AUTH_UNKNOWN,
                          alert={"reasons": [AUTH_DEATH], "since": NOW},
                          launch_attempt_streak=_streak(rig)))
    assert only(out, "launch_recovered") == [], out
    assert AUTH_DEATH in _reasons(out), _reasons(out)


@pytest.mark.parametrize("streak,why", [
    ({"samples": {"i5": ["runner"]}}, "a sample lost to a truncated read"),
    ({"samples": {}}, "the whole window lost, or a journal that could not be read"),
    ({"samples": {}, "delivered": False}, "a journal read that saw no delivery at all"),
])
def test_a_streak_that_merely_WENT_AWAY_is_not_a_delivery(streak, why):
    """The exit edge keys on the runner having SEEN a session start, never on the streak merely
    being gone. Every weaker reading has teeth: a lost sample drops the threshold, and an unreadable
    or scrolled-past journal empties the map — read as a delivery, any of them retracts a standing
    page mid-outage, lands the launch-cap parks the hold was suppressing and launches back into the
    wall. Those parks and that relaunch still happen (the hold is gone either way); what must not
    happen is the loop recording that a launch was verified when none was."""
    dsk = disk(launch_anchor={"ok": True}, auth_probe=_AUTH_UNKNOWN,
               launch_attempt_streak=streak,
               alert={"reasons": [AUTH_DEATH, "usage_stale"], "since": NOW - 600},
               issues_state={"version": 1, "issues": {
                   "i5": ist("ready", launch_failures=actions.LAUNCH_FAILURE_CAP)}})
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert only(out, "launch_recovered") == [], why
    # ...and the tick is not pretending otherwise: the hold really is gone, so the at-cap lane parks
    # and the queue flies again. That is the honest consequence; the false RECORD is what is barred.
    assert [a["id"] for a in only(out, "park")] == ["i5"], why


def test_a_journal_the_runner_could_not_read_publishes_no_delivery(rig):
    """...and the runner says so at the source. Its self-guard returns an empty map, which without
    the flag beside it is indistinguishable from a session having started."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    assert _streak(rig)["delivered"] is False
    (rig.home / "journal.jsonl").unlink()
    assert _streak(rig) == {"samples": {}, "delivered": False}, "an empty read is not a delivery"


def test_a_STALE_exited_marker_is_not_demand_this_class_can_page_on():
    """#159's relaunch-demand reading counts a bare `state/exited/<id>` marker, and start-session.sh
    writes one on EVERY session exit while only the relaunch paths remove it — never a park, never a
    teardown. The owner's live machine has carried one for a lane that merged on 2026-07-11. #159
    can afford that looseness (its reading auto-clears on the next healthy probe); this class holds
    a queue and pages until a flight flies, and on an idle machine there is no flight to make — so
    the page would stand forever on a marker nobody will ever act on."""
    dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN,
                    exited={"i40": "1783801109 rc=137"},
                    issues_state={"version": 1, "issues": {"i40": ist("merged")}})
    out = decide(parsed_issues=[], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out), _reasons(out)


def test_an_exited_marker_for_a_LIVE_lane_is_demand_and_still_pages():
    """...and the clause is kept, not dropped: a lane the recovery ladder may yet relaunch is a
    flight about to be attempted, which is exactly what this hold denies. A lane the state file has
    forgotten entirely counts too — fail-safe, the way #159 is."""
    for ist_map in ({"i41": ist("exited")}, {}):
        dsk = _attempts(["i5", "d26"], auth_probe=_AUTH_UNKNOWN, exited={"i41": "x"},
                        issues_state={"version": 1, "issues": ist_map})
        out = decide(parsed_issues=[], dsk=dsk, usage=_dark_meter())
        assert AUTH_DEATH in _reasons(out), (ist_map, _reasons(out))


def test_a_delivery_FOLLOWED_by_a_fresh_refusal_reports_no_delivery(rig):
    """`delivered` is "the walk ended on a session having started", not "it saw one somewhere". A
    refusal after the delivery re-seeds the streak, and reporting the delivery beside it would let
    the exit edge fire on a machine that has started refusing again."""
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    _oop(rig, "debug_launch", "launched", "d27", ts=NOW + 2)
    assert _streak(rig) == {"samples": {}, "delivered": True}
    _oop(rig, "watchdog", "launch_failed", "d28", ts=NOW + 3, rc=5)
    streak = _streak(rig)
    assert streak["samples"] == {"d28": ["watchdog"]} and streak["delivered"] is False


def test_a_resolve_conflict_refusal_is_a_sample_like_any_other(rig):
    """The one runner launch path the hold does not suppress besides the recovery relaunch, so it is
    the one most likely to feed this streak while a hold already stands."""
    import journal as journal_mod
    rig.r.tick(now=NOW)
    _oop(rig, "watchdog", "launch_failed", "d26", ts=NOW + 1, rc=5)
    journal_mod.append(str(rig.home), {
        "act": "resolve_conflict", "id": "i5", "outcome": "conflict-session launch rc=6",
        "evidence": {"kind": "launch", "rc": 6, "reason": "env_poisoned", "captured": "x"}},
        NOW + 2)
    assert _streak(rig)["samples"] == {"d26": ["watchdog"], "i5": ["runner"]}


def test_every_act_that_runs_the_launcher_is_in_one_of_the_streak_tables():
    """The vocabulary guard the reason and spawner registries already have. The dangerous direction
    is a missed DELIVERY: a new spawner whose success this reader cannot recognise leaves the streak
    standing over a machine that has demonstrably started a session. Pinned against the launcher's
    actual call sites so a fourth one cannot be added without a row here."""
    import re as _re
    known = set(runner_mod.RUNNER_LAUNCH_ACTS) | {runner_mod.RUNNER_RELAUNCH_ACT} \
        | set(runner_mod.FOREIGN_LAUNCH_OUTCOMES)
    # the runner's own launcher call sites, read off the source rather than listed by hand
    src = open(_re.sub(r"\.pyc$", ".py", runner_mod.__file__)).read()
    assert src.count("self._script(LAUNCHER)") == len(runner_mod.RUNNER_LAUNCH_ACTS) + 1, \
        "a launcher call site was added or removed — does it journal an act this streak reads?"
    assert {"launch", "recover", "resolve_conflict"} <= known
    assert {"watchdog", "debug_launch", "resume"} <= known


@pytest.mark.parametrize("flap", ["auth_dead", "anchor_down"])
def test_a_STANDING_hold_keeps_its_name_through_a_flapping_probe(flap):
    """The latch keeps its memory in the durable ALERT — and the mute is what rewrites that field.
    Both probe-driven muters read a five-second subprocess every tick (`claude auth status`, the
    pane probe), so one flapping renamed the episode back and forth and paged the owner once a
    minute for a whole outage, twice as often as before this class existed.

    The mute exists so an episode is never named twice AT ONCE; it is not a reason to rename one.
    An episode keeps the name it opened under — #320's own rule, one class up."""
    alert, pages = None, []
    for tick in range(6):
        # tick 0 is the healthy reading, so THIS class opens the episode and the flap starts after
        over = {"auth_probe": _AUTH_DEAD if tick % 2 else _AUTH_UNKNOWN} if flap == "auth_dead" \
            else {"auth_probe": _AUTH_UNKNOWN, "launch_anchor": {"ok": not tick % 2}}
        out = decide(parsed_issues=[parsed(5), parsed(6)], usage=_dark_meter(),
                     dsk=_attempts(["i5", "d26"], alert=alert, **over))
        for a in only(out, "alert"):
            alert = {"reasons": a["reasons"], "since": NOW}
            pages.append(a["reasons"])
        assert only(out, "launch_recovered") == [], tick
    assert pages, "the episode must be said at least once"
    # THE CLAIM: once this class has named the episode, it never stops naming it. What still moves
    # is the flapping detector's OWN reason joining and leaving the list beside it — that is
    # `auth_dead`'s and `launch_anchor_down`'s pre-existing behaviour (on a queue with no hold at
    # all those two raise and retract with their probe), and latching THEIR readings is their own
    # issue, not this one's. What this class must never do is let the episode be renamed away from
    # it and re-opened, which is what turned one outage into a page a minute.
    assert all(AUTH_DEATH in p for p in pages), pages
    other = "auth_dead" if flap == "auth_dead" else "launch_anchor_down"
    assert {frozenset(p) for p in pages} <= {
        frozenset([AUTH_DEATH, "usage_stale"]),
        frozenset([AUTH_DEATH, other, "usage_stale"])}, pages


def test_a_narrower_class_arriving_FIRST_still_takes_the_naming():
    """...and the mute is not disarmed, only bounded to what it is for. With nothing standing yet, a
    definitive dead reading names the episode and this class stays quiet — the whole point of the
    backstop."""
    out = decide(parsed_issues=[parsed(5), parsed(6)], usage=_dark_meter(),
                 dsk=_attempts(["i5", "d26"], auth_probe=_AUTH_DEAD, alert=None))
    assert _reasons(out) == ["auth_dead", "usage_stale"], _reasons(out)


def test_an_over_long_session_id_costs_a_sample_not_the_tick():
    """`_iid_num` does a bare `int(iid[1:])`, and Python refuses an integer literal past 4300 digits
    — so an id this admits could raise out of the whole tick, before the heartbeat is stamped, which
    is how a live runner reads as dead (#95). Every other garbage shape in this path costs a sample."""
    dsk = _attempts({"samples": {"i" + "1" * 5000: ["runner"], "d26": ["watchdog"]}},
                    auth_probe=_AUTH_UNKNOWN)
    out = decide(parsed_issues=[parsed(5), parsed(6)], dsk=dsk, usage=_dark_meter())
    assert AUTH_DEATH not in _reasons(out), "one usable sample is not a streak"
