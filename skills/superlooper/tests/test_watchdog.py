"""The unattended-debugger watchdog (issue #66): a MECHANICAL fallback — no LLM anywhere in
this module — that watches the engine's own health signals (stale heartbeat, present ALERT,
the no-progress shape) and, when one trips and the owner does not intervene within a grace
window, launches ONE fresh sl-debugger session through the same interactive launch shim
worker sessions use.

These tests pin the DoD properties before the module exists (TDD red):
  * trips on: stale heartbeat, present ALERT, no-progress (eligible work + empty lanes +
    nothing launched past the bound);
  * NEVER trips on designed-safe waits: gate-waiting on CI, blocked-by holds,
    parked / needs-william, frozen-but-building, a usage meter that READS exhausted;
  * the flow: one notify -> grace -> exactly one launch; a clear during grace stands down
    silently (no launch, no repeat notify);
  * once-per-incident: a continuing episode never launches twice; a genuinely new episode
    after recovery may;
  * kill-switch: observe + journal, launch nothing;
  * singleton: never two debugger sessions;
  * the authority setting rides into the launch request (default `full`).
"""
import issues
import watchdog as wd

T0 = 1_700_000_000
MIN = 60


def _cfg(**over):
    w = {"authority": "full", "allowlist": [], "grace_minutes": 30,
         "heartbeat_stale_minutes": 20, "no_progress_minutes": 30}
    w.update(over)
    return {"watchdog": w}


def _view(now=T0, **over):
    """A HEALTHY instance at `now`: fresh heartbeat, no ALERT, quiet queue."""
    v = {"heartbeat": now - 15, "alert": None, "lanes_busy": False, "gh_ok": True,
         "eligible_nums": [], "usage_exhausted": False, "kill_switch": False,
         "debugger_live": False}
    v.update(over)
    return v


def _run(now, view, state=None, cfg=None):
    return wd.evaluate(now, cfg or _cfg(), view, state if state is not None else wd.new_state())


def _outcomes(result):
    return [j.get("outcome") for j in result["journal"]]


# --------------------------- signals: healthy is silent ---------------------------

def test_healthy_instance_is_no_signal_no_action():
    r = _run(T0, _view())
    assert r["journal"] == []
    assert r["notify"] == []
    assert r["launch"] is None
    assert r["state"]["episode"] is None


def test_loop_that_never_ran_is_no_signal():
    # No heartbeat has EVER been written (fresh install / state home never used): there is no
    # instance to watch, so an absent heartbeat is NOT a stale one.
    r = _run(T0, _view(heartbeat=None))
    assert r["state"]["episode"] is None
    assert r["notify"] == []


# --------------------------- signals: the three trips ---------------------------

def test_stale_heartbeat_trips_and_notifies_once():
    r = _run(T0, _view(heartbeat=T0 - 21 * MIN))
    ep = r["state"]["episode"]
    assert ep is not None
    assert ep["signals"] == ["heartbeat_stale"]
    assert ep["opened_at"] == T0
    assert len(r["notify"]) == 1
    title, body = r["notify"][0]
    assert "watchdog" in title.lower()
    assert "30" in body                       # names the grace window
    assert "full" in body                     # names the authority tier
    assert _outcomes(r) == ["notified"]
    assert all(j["act"] == "watchdog" for j in r["journal"])
    assert r["launch"] is None                # never a launch before the grace elapses


def test_fresh_heartbeat_is_no_signal():
    r = _run(T0, _view(heartbeat=T0 - 19 * MIN))
    assert r["state"]["episode"] is None


def test_present_alert_trips():
    r = _run(T0, _view(alert={"reasons": ["gh_unreachable"], "since": T0 - 300}))
    ep = r["state"]["episode"]
    assert ep is not None and ep["signals"] == ["alert"]
    assert "gh_unreachable" in r["notify"][0][1]


def test_unreadable_alert_still_counts_as_present():
    # runner._read_json maps present-but-unreadable to {} — existence is the signal.
    r = _run(T0, _view(alert={}))
    assert r["state"]["episode"] is not None


def test_no_progress_trips_only_after_the_bound():
    st = wd.new_state()
    r1 = _run(T0, _view(eligible_nums=[42]), st)
    assert r1["state"]["episode"] is None               # one glimpse is not an episode
    assert r1["state"]["no_progress_since"] == {"42": T0}
    r2 = _run(T0 + 29 * MIN, _view(T0 + 29 * MIN, eligible_nums=[42]), r1["state"])
    assert r2["state"]["episode"] is None               # bound not yet reached
    r3 = _run(T0 + 30 * MIN, _view(T0 + 30 * MIN, eligible_nums=[42]), r2["state"])
    ep = r3["state"]["episode"]
    assert ep is not None and ep["signals"] == ["no_progress"]
    assert "#42" in r3["notify"][0][1]                  # names the waiting work


def test_no_progress_clock_survives_a_changing_queue_neighbour():
    # #42 waits the whole bound; #43 joins late. The trip keys on the issue that has waited
    # the full bound, not on the newest arrival.
    st = wd.new_state()
    r1 = _run(T0, _view(eligible_nums=[42]), st)
    r2 = _run(T0 + 20 * MIN, _view(T0 + 20 * MIN, eligible_nums=[42, 43]), r1["state"])
    assert r2["state"]["no_progress_since"] == {"42": T0, "43": T0 + 20 * MIN}
    r3 = _run(T0 + 30 * MIN, _view(T0 + 30 * MIN, eligible_nums=[42, 43]), r2["state"])
    assert r3["state"]["episode"] is not None


# --------------------------- signals: designed-safe waits never trip ---------------------------

def test_frozen_but_building_never_trips():
    # A freeze stops MERGES, not builds (the constitution): while a lane is BUSY building,
    # waiting eligible work is the sequential-build discipline working, not a fault — and the
    # no-progress clock RESETS (the wait so far was designed).
    st = wd.new_state()
    r1 = _run(T0, _view(eligible_nums=[42]), st)
    r2 = _run(T0 + 20 * MIN, _view(T0 + 20 * MIN, eligible_nums=[42], lanes_busy=True),
              r1["state"])
    assert r2["state"]["no_progress_since"] == {}
    assert r2["state"]["episode"] is None
    r3 = _run(T0 + 40 * MIN, _view(T0 + 40 * MIN, eligible_nums=[42]), r2["state"])
    assert r3["state"]["episode"] is None               # the bound restarted from scratch
    assert r3["state"]["no_progress_since"] == {"42": T0 + 40 * MIN}


def test_usage_reading_exhausted_is_a_designed_hold():
    # A meter that successfully READS exhausted means the runner is fail-closed holding on
    # purpose (spec ceilings). Never a trip, and the clock resets.
    st = {"episode": None, "no_progress_since": {"42": T0 - 60 * MIN}, "next_debugger": 1}
    r = _run(T0, _view(eligible_nums=[42], usage_exhausted=True), st)
    assert r["state"]["episode"] is None
    assert r["state"]["no_progress_since"] == {}


def test_gh_outage_neither_trips_nor_resets_the_clock():
    # An unreachable GitHub yields no trustworthy eligibility view: the clock FREEZES (kept,
    # not reset — the condition held at both endpoints) and nothing trips off it.
    st = {"episode": None, "no_progress_since": {"42": T0 - 60 * MIN}, "next_debugger": 1}
    r = _run(T0, _view(gh_ok=False, eligible_nums=[]), st)
    assert r["state"]["episode"] is None
    assert r["state"]["no_progress_since"] == {"42": T0 - 60 * MIN}


def test_busy_lanes_reset_the_clock_even_when_gh_is_dark():
    # Lane occupancy is DISK truth, independent of GitHub: a busy lane is progress and resets
    # the clocks even when the eligibility view is unavailable (the CLI deliberately skips the
    # gh query while lanes are busy).
    st = {"episode": None, "no_progress_since": {"42": T0 - 60 * MIN}, "next_debugger": 1}
    r = _run(T0, _view(lanes_busy=True, gh_ok=False), st)
    assert r["state"]["no_progress_since"] == {}


def test_stale_heartbeat_preempts_no_progress():
    # With the loop itself dead/wedged, the no-progress detector is moot: the stale-heartbeat
    # signal fires and the no-progress clock is left untouched (issues.json is not current).
    st = {"episode": None, "no_progress_since": {"42": T0 - 60 * MIN}, "next_debugger": 1}
    r = _run(T0, _view(heartbeat=T0 - 60 * MIN, eligible_nums=[42]), st)
    assert r["state"]["episode"]["signals"] == ["heartbeat_stale"]
    assert r["state"]["no_progress_since"] == {"42": T0 - 60 * MIN}


def _raw_issue(num, labels, blocked_by=""):
    body = f"## Loop metadata\nblocked-by: {blocked_by}\n" if blocked_by else ""
    return {"number": num, "title": f"issue {num}",
            "labels": [{"name": n} for n in labels], "body": body}


def test_eligible_nums_excludes_every_designed_safe_wait():
    parsed = [issues.parse_issue(r) for r in (
        _raw_issue(1, ["agent-ready", "type:build"]),                       # genuinely waiting
        _raw_issue(2, ["agent-ready", "type:build"], blocked_by="#99"),     # blocked-by hold
        _raw_issue(3, ["agent-ready", "type:build"], blocked_by="#7"),      # dep closed -> in
        _raw_issue(4, ["in-progress", "type:build"]),                       # building / gate-waiting on CI
        _raw_issue(5, ["agent-ready", "in-progress", "type:build"]),        # launched already
        _raw_issue(6, ["parked", "type:build"]),                            # parked
        _raw_issue(7, ["needs-owner", "type:build"]),                       # owner's desk (current label)
        _raw_issue(8, ["agent-ready"]),                                     # no valid type
        _raw_issue(9, ["needs-william", "type:build"]),                     # owner's desk (legacy label, #58 compat)
    )]
    assert wd.eligible_nums(parsed, closed_nums={7}) == [1, 3]


# --------------------------- the flow: notify -> grace -> one launch ---------------------------

def _open_episode(now=T0, cfg=None):
    """A tripped, notified episode on a stale heartbeat at `now`."""
    r = _run(now, _view(now, heartbeat=now - 21 * MIN), cfg=cfg)
    assert r["state"]["episode"] is not None
    return r["state"]


def test_within_grace_is_silent_waiting():
    st = _open_episode(T0)
    r = _run(T0 + 29 * MIN, _view(T0 + 29 * MIN, heartbeat=T0 - 21 * MIN), st)
    assert r["notify"] == []
    assert r["journal"] == []
    assert r["launch"] is None


def test_grace_elapsed_emits_exactly_one_launch_request():
    st = _open_episode(T0)
    now = T0 + 30 * MIN
    r = _run(now, _view(now, heartbeat=T0 - 21 * MIN), st)
    assert r["launch"] is not None
    assert r["launch"]["id"] == "d1"
    assert r["launch"]["authority"] == "full"
    assert r["launch"]["signals"] == ["heartbeat_stale"]
    # recording the outcome (rc=0) marks the episode launched, journals + notifies once
    done = wd.after_launch(now, _cfg(), r["state"], r["launch"], rc=0)
    ep = done["state"]["episode"]
    assert ep["launched_at"] == now and ep["launch_id"] == "d1"
    assert _outcomes(done) == ["launched"]
    assert done["journal"][0]["id"] == "d1"
    assert len(done["notify"]) == 1 and "d1" in done["notify"][0][1]
    # ...and the SAME standing episode never launches again
    later = _run(now + 60 * MIN, _view(now + 60 * MIN, heartbeat=T0 - 21 * MIN), done["state"])
    assert later["launch"] is None
    assert later["notify"] == []


def test_signal_clearing_during_grace_stands_down_silently():
    st = _open_episode(T0)
    r = _run(T0 + 10 * MIN, _view(T0 + 10 * MIN), st)     # healthy again mid-grace
    assert r["state"]["episode"] is None
    assert r["launch"] is None
    assert r["notify"] == []                              # silent stand-down: journal only
    assert _outcomes(r) == ["stand_down"]
    # a LATER re-trip is a genuinely new episode: it notifies again
    r2 = _run(T0 + 60 * MIN, _view(T0 + 60 * MIN, heartbeat=T0 + 30 * MIN), r["state"])
    assert r2["state"]["episode"] is not None
    assert len(r2["notify"]) == 1


def test_new_episode_after_recovery_may_launch_again():
    st = _open_episode(T0)
    launched = wd.after_launch(
        T0 + 30 * MIN, _cfg(),
        _run(T0 + 30 * MIN, _view(T0 + 30 * MIN, heartbeat=T0 - 21 * MIN), st)["state"],
        {"id": "d1", "signals": ["heartbeat_stale"], "authority": "full", "allowlist": []},
        rc=0)["state"]
    # recovery closes the episode...
    cleared = _run(T0 + 40 * MIN, _view(T0 + 40 * MIN), launched)["state"]
    assert cleared["episode"] is None
    # ...and a fresh trip + grace launches a NEW session with a NEW id
    st2 = _run(T0 + 90 * MIN, _view(T0 + 90 * MIN, heartbeat=T0 + 60 * MIN,
                                    alert={"reasons": ["launch_runaway:i9"]}), cleared)["state"]
    r = _run(T0 + 120 * MIN, _view(T0 + 120 * MIN, heartbeat=T0 + 60 * MIN,
                                   alert={"reasons": ["launch_runaway:i9"]}), st2)
    assert r["launch"] is not None and r["launch"]["id"] == "d2"


def test_episode_unions_a_new_signal_without_renotifying():
    st = _open_episode(T0)
    now = T0 + 5 * MIN
    r = _run(now, _view(now, heartbeat=T0 - 21 * MIN, alert={"reasons": ["x"]}), st)
    assert r["state"]["episode"]["signals"] == ["alert", "heartbeat_stale"]
    assert r["notify"] == []


def test_zero_grace_launches_on_the_tripping_check():
    cfg = _cfg(grace_minutes=0)
    r = _run(T0, _view(heartbeat=T0 - 21 * MIN), cfg=cfg)
    assert len(r["notify"]) == 1
    assert r["launch"] is not None and r["launch"]["id"] == "d1"


# --------------------------- rails: singleton, retry cap, kill-switch ---------------------------

def test_a_live_debugger_session_blocks_a_new_launch():
    st = _open_episode(T0)
    now = T0 + 30 * MIN
    r = _run(now, _view(now, heartbeat=T0 - 21 * MIN, debugger_live=True), st)
    assert r["launch"] is None
    assert _outcomes(r) == ["skipped_live_session"]
    # the moment the old session ends, the (already past-grace) launch fires
    r2 = _run(now + 5 * MIN, _view(now + 5 * MIN, heartbeat=T0 - 21 * MIN), r["state"])
    assert r2["launch"] is not None


def test_failed_launches_notify_once_and_stop_at_the_cap():
    st = _open_episode(T0)
    notified_failures = 0
    now = T0 + 30 * MIN
    for attempt in range(wd.LAUNCH_ATTEMPT_CAP):
        r = _run(now, _view(now, heartbeat=T0 - 21 * MIN), st)
        assert r["launch"] is not None, f"attempt {attempt} should retry"
        done = wd.after_launch(now, _cfg(), r["state"], r["launch"], rc=2)
        assert _outcomes(done) == ["launch_failed"]
        assert done["state"]["episode"]["launched_at"] is None
        notified_failures += len(done["notify"])
        st = done["state"]
        now += 5 * MIN
    assert notified_failures == 1                        # the failure texts ONCE per episode
    r = _run(now, _view(now, heartbeat=T0 - 21 * MIN), st)
    assert r["launch"] is None                           # cap reached: hold, don't storm


def test_kill_switch_observes_journals_and_launches_nothing():
    r = _run(T0, _view(heartbeat=T0 - 21 * MIN, kill_switch=True))
    assert _outcomes(r) == ["disabled"]
    assert r["journal"][0]["signals"] == ["heartbeat_stale"]
    assert r["notify"] == []
    assert r["launch"] is None
    assert r["state"]["episode"] is None                 # fully inert: no episode opens


def test_kill_switch_journals_once_per_distinct_observation():
    # Fresh review P1-2 (the 2026-07-08 unbounded-repetition class): an overnight kill-switch
    # at a 5-min interval must not write ~96 identical journal lines. One record per DISTINCT
    # observed signal set; a change journals again; re-enabling re-arms the dedup.
    v = _view(heartbeat=T0 - 21 * MIN, kill_switch=True)
    r1 = _run(T0, v)
    assert _outcomes(r1) == ["disabled"]
    r2 = _run(T0 + 5 * MIN, _view(T0 + 5 * MIN, heartbeat=T0 - 21 * MIN, kill_switch=True),
              r1["state"])
    assert r2["journal"] == []                           # same observation: silent
    r3 = _run(T0 + 10 * MIN, _view(T0 + 10 * MIN, heartbeat=T0 - 21 * MIN,
                                   alert={"reasons": ["x"]}, kill_switch=True), r2["state"])
    assert _outcomes(r3) == ["disabled"]                 # the observation CHANGED: journal it
    assert r3["journal"][0]["signals"] == ["alert", "heartbeat_stale"]
    # switch removed and later re-applied: the dedup marker cleared, so it journals afresh
    r4 = _run(T0 + 15 * MIN, _view(T0 + 15 * MIN), r3["state"])
    r5 = _run(T0 + 20 * MIN, _view(T0 + 20 * MIN, heartbeat=T0 - 21 * MIN, kill_switch=True),
              r4["state"])
    assert _outcomes(r5) == ["disabled"]


def test_kill_switch_mid_episode_holds_the_launch():
    st = _open_episode(T0)
    now = T0 + 30 * MIN
    r = _run(now, _view(now, heartbeat=T0 - 21 * MIN, kill_switch=True), st)
    assert r["launch"] is None
    assert _outcomes(r) == ["disabled"]
    # the watch state itself is untouched while disabled: the episode neither advances nor
    # closes, and the no-progress clocks stay exactly as they were.
    assert r["state"]["episode"] == st["episode"]
    assert r["state"]["no_progress_since"] == st["no_progress_since"]
    assert r["state"]["next_debugger"] == st["next_debugger"]


# --------------------------- authority delivery ---------------------------

def test_launch_request_carries_the_configured_authority_and_allowlist():
    cfg = _cfg(authority="allowlist", allowlist=["superlooper doctor", "relabel"])
    st = _open_episode(T0, cfg=cfg)
    r = _run(T0 + 30 * MIN, _view(T0 + 30 * MIN, heartbeat=T0 - 21 * MIN), st, cfg=cfg)
    assert r["launch"]["authority"] == "allowlist"
    assert r["launch"]["allowlist"] == ["superlooper doctor", "relabel"]


def test_authority_defaults_to_full_when_the_config_omits_the_block():
    st = _run(T0, _view(heartbeat=T0 - 21 * MIN), cfg={})["state"]
    r = wd.evaluate(T0 + 30 * MIN, {}, _view(T0 + 30 * MIN, heartbeat=T0 - 21 * MIN), st)
    assert r["launch"] is not None
    assert r["launch"]["authority"] == "full"


# --------------------------- usage helper ---------------------------

def test_usage_reads_exhausted_only_on_a_successful_over_ceiling_read():
    ok_low = {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0}
    ok_high = {"auth_status": "ok", "five_hour_pct": 95.0, "seven_day_pct": 20.0}
    ok_weekly = {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 97.0}
    dark = {"auth_status": "no_keychain", "five_hour_pct": None, "seven_day_pct": None}
    assert wd.usage_reads_exhausted(ok_low) is False
    assert wd.usage_reads_exhausted(ok_high) is True
    assert wd.usage_reads_exhausted(ok_weekly) is True
    # a DARK meter is unreadable, not exhausted (the #46/#76 asymmetry): it must NOT read as a
    # designed hold, or a launchd context with no Keychain would neuter the detector forever.
    assert wd.usage_reads_exhausted(dark) is False
    assert wd.usage_reads_exhausted(None) is False
    assert wd.usage_reads_exhausted({"auth_status": "ok", "five_hour_pct": float("nan"),
                                     "seven_day_pct": 1.0}) is False


# --------------------------- brief rendering ---------------------------

def test_render_brief_substitutes_the_invocation_context():
    t = "signals: {signals}\nauthority: {authority}\nhome: {state_home}\nallow: {allowlist}"
    out = wd.render_brief(t, {"signals": "heartbeat_stale", "authority": "full",
                              "state_home": "/x/home", "allowlist": "(none)"})
    assert out == "signals: heartbeat_stale\nauthority: full\nhome: /x/home\nallow: (none)"


# --------------------------- state hygiene ---------------------------

def test_wrong_typed_persisted_state_degrades_to_a_fresh_one():
    # watchdog.json may be hand-edited/corrupt: a wrong-typed shape must never crash the check
    # or fabricate an episode (the fail-open-on-wrong-typed-input defect class).
    for garbage in (None, [], "x", {"episode": "weird", "no_progress_since": [1],
                                    "next_debugger": "9"}):
        r = wd.evaluate(T0, _cfg(), _view(), wd.coerce_state(garbage))
        assert r["state"]["episode"] is None
        assert r["launch"] is None
