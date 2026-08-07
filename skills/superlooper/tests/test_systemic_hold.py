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
import shutil
from pathlib import Path

import pytest

import actions
import evidence
import loopstate
import report as report_lib
import runner as runner_mod

from test_actions import NOW, cfg, decide, disk, ist, only, parsed, has_notify
from test_runner import (_launch_action, issue_state, make_config, mutations,  # noqa: F401
                         rig, seed_issue)


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


def test_the_hold_line_says_nothing_is_parked():
    """The whole point of surfacing it: a paused queue must never be read as an idle one, and the
    owner must not go looking for issues to re-approve."""
    line = actions.queue_hold_line({"reasons": ["gh_auth_dead_workers"]})
    assert "parked" in line.lower() or "re-approv" in line.lower()


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


def test_the_morning_report_names_the_cause_and_says_nothing_was_parked():
    view = {"date": "2026-08-07", "now": NOW, "frozen": None,
            "queue": [{"num": 5, "title": "a"}, {"num": 6, "title": "b"}], "usage": None,
            "queue_hold": {"reasons": ["gh_auth_dead_workers"], "since": NOW - 3600}}
    text = report_lib.morning([], view, {}, cfg())
    assert "gh_auth_dead_workers" in text
    assert "re-approve" in text
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
