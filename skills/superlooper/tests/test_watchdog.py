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
import scheduler
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


def _open_no_progress_episode(now=T0):
    """A tripped, notified episode whose ONLY signal is no_progress at `now` (issue #42 has
    waited the full bound; heartbeat fresh, gh reachable)."""
    st = {"episode": None, "no_progress_since": {"42": now - 31 * MIN}, "next_debugger": 1}
    r = _run(now, _view(now, eligible_nums=[42]), st)
    assert r["state"]["episode"] is not None
    assert r["state"]["episode"]["signals"] == ["no_progress"]
    return r["state"]


def test_open_no_progress_episode_is_held_across_a_gh_outage():
    # Gap #2: an open no_progress episode must NOT stand down when the condition is merely
    # UNOBSERVABLE (gh dark), only when it is genuinely OBSERVED clear. Standing down on a blip
    # drops the episode and re-trips on recovery — a duplicate owner text and a restarted grace.
    st = _open_no_progress_episode(T0)
    opened_at = st["episode"]["opened_at"]
    r = _run(T0 + 5 * MIN, _view(T0 + 5 * MIN, gh_ok=False, eligible_nums=[]), st)
    assert r["state"]["episode"] is not None                 # HELD, not stood down
    assert r["state"]["episode"]["opened_at"] == opened_at   # SAME episode (grace clock intact)
    assert r["notify"] == []
    assert r["launch"] is None
    assert r["journal"] == []                                # a quiet hold, no stand_down record
    assert r["state"]["no_progress_since"] == st["no_progress_since"]   # clock frozen, preserved


def test_held_no_progress_episode_resumes_the_same_episode_on_recovery():
    st = _open_no_progress_episode(T0)                       # opened_at = T0, grace 30 min
    # gh goes dark right at what WOULD be launch time: held, no launch, grace clock NOT restarted
    held = _run(T0 + 35 * MIN, _view(T0 + 35 * MIN, gh_ok=False, eligible_nums=[]), st)
    assert held["launch"] is None
    assert held["state"]["episode"]["opened_at"] == T0
    # gh recovers with the queue still waiting: the SAME episode (grace already elapsed from T0)
    # launches immediately — the outage did not defer the launch.
    r = _run(T0 + 40 * MIN, _view(T0 + 40 * MIN, eligible_nums=[42]), held["state"])
    assert r["launch"] is not None
    assert r["launch"]["signals"] == ["no_progress"]


def test_observed_no_progress_clear_stands_down_silently():
    st = _open_no_progress_episode(T0)
    # gh is UP and reports the queue empty (work launched / merged): a genuine OBSERVED clear
    r = _run(T0 + 5 * MIN, _view(T0 + 5 * MIN, gh_ok=True, eligible_nums=[]), st)
    assert r["state"]["episode"] is None
    assert r["notify"] == []
    assert _outcomes(r) == ["stand_down"]


def test_busy_lane_stands_a_no_progress_episode_down_even_when_gh_is_dark():
    # A busy lane is DISK truth (a launch happened): a definite designed-hold clear, an
    # OBSERVATION in its own right, so the episode stands down even though gh is unreachable.
    st = _open_no_progress_episode(T0)
    r = _run(T0 + 5 * MIN, _view(T0 + 5 * MIN, lanes_busy=True, gh_ok=False), st)
    assert r["state"]["episode"] is None
    assert _outcomes(r) == ["stand_down"]


def test_stale_heartbeat_preempts_no_progress():
    # With the loop itself dead/wedged, the no-progress detector is moot: the stale-heartbeat
    # signal fires and the no-progress clock is left untouched (issues.json is not current).
    st = {"episode": None, "no_progress_since": {"42": T0 - 60 * MIN}, "next_debugger": 1}
    r = _run(T0, _view(heartbeat=T0 - 60 * MIN, eligible_nums=[42]), st)
    assert r["state"]["episode"]["signals"] == ["heartbeat_stale"]
    assert r["state"]["no_progress_since"] == {"42": T0 - 60 * MIN}


def _parsed(num, labels, touches=None, blocked_by=""):
    body = "## Loop metadata\n"
    if touches is not None:
        body += "touches: " + ", ".join(touches) + "\n"
    if blocked_by:
        body += "blocked-by: " + blocked_by + "\n"
    return issues.parse_issue({"number": num, "title": f"issue {num}",
                               "labels": [{"name": n} for n in labels], "body": body})


def test_launchable_nums_excludes_every_designed_safe_wait():
    # The no-progress eligibility view is what the SCHEDULER would launch now, run through the
    # same eligibility rule + belt-and-braces label exclusions as the runner's candidate filter.
    parsed = [
        _parsed(1, ["agent-ready", "type:build"], touches=["a"]),                 # genuinely waiting
        _parsed(2, ["agent-ready", "type:build"], touches=["b"], blocked_by="#99"),  # blocked-by hold
        _parsed(3, ["agent-ready", "type:build"], touches=["c"], blocked_by="#7"),   # dep closed -> in
        _parsed(4, ["in-progress", "type:build"], touches=["d"]),                 # building / gate-waiting on CI
        _parsed(5, ["agent-ready", "in-progress", "type:build"], touches=["e"]),  # launched already
        _parsed(6, ["parked", "type:build"], touches=["f"]),                      # parked
        _parsed(7, ["needs-owner", "type:build"], touches=["g"]),                 # owner's desk (current label)
        _parsed(8, ["agent-ready"], touches=["h"]),                               # no valid type
        _parsed(9, ["needs-william", "type:build"], touches=["i"]),               # owner's desk (legacy label)
    ]
    assert wd.launchable_nums(parsed, lane_state=[], config={"lanes": 10},
                              closed_nums={7}, territory_claims=[]) == [1, 3]


def test_launchable_nums_counts_genuinely_waiting_work():
    parsed = [_parsed(101, ["agent-ready", "type:build"], touches=["frontend"]),
              _parsed(102, ["agent-ready", "type:build"], touches=["api"])]
    assert wd.launchable_nums(parsed, lane_state=[], config={"lanes": 5},
                              closed_nums=set(), territory_claims=[]) == [101, 102]


def test_launchable_nums_excludes_candidates_held_by_a_territory_claim():
    # The #92 binding bug: a gating/holding issue's wildcard territory claim occupies NO lane
    # but blocks every overlapping candidate under hard affinity. The scheduler holds those
    # candidates, so the watchdog must NOT count them as launchable — a gate-waiting build with
    # eligible work behind it is a designed-safe wait, never a no-progress trip.
    parsed = [_parsed(101, ["agent-ready", "type:build"], touches=["frontend"]),
              _parsed(102, ["agent-ready", "type:build"], touches=["api"])]
    wildcard_claim = [{"id": "i106", "touches": [], "type": "build"}]
    assert wd.launchable_nums(parsed, lane_state=[], config={"lanes": 10},
                              closed_nums=set(), territory_claims=wildcard_claim) == []


def test_launchable_nums_respects_a_narrow_territory_claim_but_not_disjoint_work():
    # A narrow (non-wildcard) claim only holds candidates that actually overlap it: a claim on
    # `api` blocks #102 (api) but not #101 (frontend).
    parsed = [_parsed(101, ["agent-ready", "type:build"], touches=["frontend"]),
              _parsed(102, ["agent-ready", "type:build"], touches=["api"])]
    api_claim = [{"id": "i106", "touches": ["api"], "type": "build"}]
    assert wd.launchable_nums(parsed, lane_state=[], config={"lanes": 10},
                              closed_nums=set(), territory_claims=api_claim) == [101]


def test_launchable_nums_passes_usage_as_a_synthetic_pass():
    # The watchdog keeps its OWN reads-exhausted gate (usage_reads_exhausted) with the dark-meter
    # asymmetry, so launchable_nums must NOT re-judge the meter — it passes a synthetic usage pass
    # so a dark/absent meter never reads here as "nothing launchable" (which would neuter the
    # detector under launchd where the Keychain read fails).
    parsed = [_parsed(101, ["agent-ready", "type:build"], touches=["frontend"])]
    assert wd.launchable_nums(parsed, lane_state=[], config={"lanes": 5},
                              closed_nums=set(), territory_claims=[]) == [101]


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


def test_watchdog_usage_ceilings_match_the_schedulers():
    # The watchdog duplicates the scheduler's launch ceilings (to stay importable without it), so a
    # drift would make the detector's "designed hold" suppression disagree with what the scheduler
    # actually holds on. Pin them equal.
    assert wd._FIVE_HOUR_CEILING == scheduler.FIVE_HOUR_LAUNCH_CEILING
    assert wd._SEVEN_DAY_CEILING == scheduler.SEVEN_DAY_NEW_WORK_CEILING


def test_usage_exactly_at_the_ceiling_is_not_exhausted():
    # The comparison is strictly OVER (`>`), matching the scheduler: a read EXACTLY at the ceiling
    # still launches, so it must not read as a designed hold.
    assert wd.usage_reads_exhausted(
        {"auth_status": "ok", "five_hour_pct": float(wd._FIVE_HOUR_CEILING),
         "seven_day_pct": 1.0}) is False
    assert wd.usage_reads_exhausted(
        {"auth_status": "ok", "five_hour_pct": 1.0,
         "seven_day_pct": float(wd._SEVEN_DAY_CEILING)}) is False


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


# ============================ runner resurrection (issue #208) ============================
# A runner that is PROVABLY GONE — heartbeat stale AND its recorded pid dead (a crash leaves the
# pidfile behind; a clean stop removes it) — is relaunched automatically, not merely debugged. The
# runner is a deterministic zero-token process, so it should restart as often as it needs to. A
# rolling-hour attempt cap turns a repeatedly-dying runner into a loud incident rather than a flap.
# `runner_dead` in the view is the CLI's "runner.lock names a dead pid" read; it defaults False, so
# the wedged-runner (alive, not ticking) debugger path above is untouched.

def _dead(now=T0, stale_min=21, **over):
    """A view where the runner is PROVABLY GONE: heartbeat stale AND the recorded pid dead."""
    return _view(now, heartbeat=now - stale_min * MIN, runner_dead=True, **over)


def test_provably_gone_runner_resurrects_instead_of_debugging():
    # heartbeat stale AND the recorded pid dead -> restart the runner, do NOT open a debugger
    # episode (there is nothing to diagnose in a dead process; it just needs restarting).
    r = _run(T0, _dead(T0))
    assert r["resurrect"] is not None
    assert r["resurrect"]["id"] == "r1"
    assert r["resurrect"]["signals"] == ["heartbeat_stale"]
    assert r["launch"] is None                             # no debugger for a dead runner
    assert r["state"]["episode"] is None                   # no heartbeat_stale debugger episode
    assert r["notify"] == []                               # the decision is quiet; after_resurrect texts
    assert r["state"]["resurrection"]["attempts"] == [T0]  # the attempt is recorded for the cap
    assert r["state"]["next_resurrection"] == 2            # the id counter advanced past r1


def test_after_resurrect_success_journals_a_distinct_act_and_texts_loudly():
    r = _run(T0, _dead(T0))
    done = wd.after_resurrect(T0, _cfg(), r["state"], r["resurrect"], rc=0)
    assert [j.get("act") for j in done["journal"]] == ["runner_resurrect"]
    assert done["journal"][0]["outcome"] == "resurrected"
    assert done["journal"][0]["id"] == "r1"
    assert len(done["notify"]) == 1                        # loud, not silent (the DoD)
    assert "r1" in done["notify"][0][1] or "runner" in done["notify"][0][1].lower()


def test_dead_runner_with_a_fresh_heartbeat_is_not_resurrected():
    # The DoD requires BOTH signals. A runner that crashed seconds ago still has a fresh heartbeat;
    # we wait for it to go stale (giving a human the chance to restart by hand first). The bounded
    # window is exactly the heartbeat-stale bound.
    r = _run(T0, _view(T0, heartbeat=T0 - 5 * MIN, runner_dead=True))
    assert r["resurrect"] is None
    assert r["state"]["episode"] is None
    assert r["notify"] == []


def test_wedged_runner_that_is_still_alive_gets_the_debugger_not_a_restart():
    # heartbeat stale but the pid is ALIVE = the loop is up but not completing ticks (wedged). That
    # is the debugger's job, exactly as before — resurrection must not fire.
    r = _run(T0, _view(T0, heartbeat=T0 - 21 * MIN, runner_dead=False))
    assert r["resurrect"] is None
    assert r["state"]["episode"] is not None               # the existing debugger episode opens
    assert r["state"]["episode"]["signals"] == ["heartbeat_stale"]


def test_resurrection_records_every_attempt_and_advances_the_id():
    st = wd.new_state()
    now = T0
    for i in range(3):
        r = _run(now, _dead(now), st)
        assert r["resurrect"]["id"] == f"r{i + 1}"
        # the runner restarts but re-dies before the next check (a crash loop)
        st = wd.after_resurrect(now, _cfg(), r["state"], r["resurrect"], rc=0)["state"]
        now += 6 * MIN
    assert len(st["resurrection"]["attempts"]) == 3


def test_rolling_hour_cap_stops_resurrecting_and_escalates_once():
    cfg = _cfg(resurrection_max_per_hour=3)
    st = wd.new_state()
    now = T0
    escalations = 0
    for _ in range(3):                                     # burn the cap
        r = _run(now, _dead(now), st, cfg=cfg)
        assert r["resurrect"] is not None
        st = wd.after_resurrect(now, cfg, r["state"], r["resurrect"], rc=0)["state"]
        now += 6 * MIN
    # cap reached: the next provably-gone check must NOT resurrect; it escalates loudly, once.
    capped = _run(now, _dead(now), st, cfg=cfg)
    assert capped["resurrect"] is None
    assert capped["state"]["episode"] is None              # no debugger flapping alongside the cap
    assert [j.get("outcome") for j in capped["journal"]] == ["resurrect_capped"]
    assert len(capped["notify"]) == 1
    escalations += len(capped["notify"])
    st = capped["state"]
    # a further check still capped: silent (the escalation already fired — no storm)
    now += 6 * MIN
    again = _run(now, _dead(now), st, cfg=cfg)
    assert again["resurrect"] is None
    assert again["notify"] == []
    assert again["journal"] == []
    assert escalations == 1


def test_cap_of_zero_disables_resurrection_and_escalates_immediately():
    cfg = _cfg(resurrection_max_per_hour=0)
    r = _run(T0, _dead(T0), cfg=cfg)
    assert r["resurrect"] is None
    assert [j.get("outcome") for j in r["journal"]] == ["resurrect_capped"]
    assert r["journal"][0]["max_per_hour"] == 0
    assert len(r["notify"]) == 1
    # the message must reflect DISABLED, never "restarted 0 time(s)" (misleading when never enabled)
    body = r["notify"][0][1]
    assert "disabled" in body.lower()
    assert "0 time" not in body


def test_attempts_age_out_of_the_rolling_window():
    cfg = _cfg(resurrection_max_per_hour=2)
    st = wd.new_state()
    # two attempts an hour+ apart never trip the cap (each ages out before the next)
    r1 = _run(T0, _dead(T0), st, cfg=cfg)
    st = wd.after_resurrect(T0, cfg, r1["state"], r1["resurrect"], rc=0)["state"]
    later = T0 + 61 * MIN
    r2 = _run(later, _dead(later), st, cfg=cfg)
    assert r2["resurrect"] is not None                     # the first attempt aged out of the window
    assert r2["state"]["resurrection"]["attempts"] == [later]


def test_settle_window_suppresses_the_wedged_debugger_right_after_a_resurrection():
    # Just after a resurrection the NEW runner is booting and has not stamped a fresh heartbeat yet,
    # so the OLD heartbeat still reads stale WHILE the pid is now alive. That must NOT be mistaken
    # for a wedged runner and open a debugger episode — the runner is simply catching up.
    r = _run(T0, _dead(T0))
    st = wd.after_resurrect(T0, _cfg(), r["state"], r["resurrect"], rc=0)["state"]
    booting = _run(T0 + 2 * MIN, _view(T0 + 2 * MIN, heartbeat=T0 - 21 * MIN, runner_dead=False), st)
    assert booting["state"]["episode"] is None             # settle window: no debugger, no notify
    assert booting["notify"] == []
    assert booting["launch"] is None
    # ...but if it is STILL stale after the settle window, it is genuinely wedged -> debugger opens
    wedged = _run(T0 + 10 * MIN, _view(T0 + 10 * MIN, heartbeat=T0 - 21 * MIN, runner_dead=False), st)
    assert wedged["state"]["episode"] is not None


def test_after_resurrect_failure_texts_once_per_down_streak():
    r = _run(T0, _dead(T0))
    d1 = wd.after_resurrect(T0, _cfg(), r["state"], r["resurrect"], rc=2)
    assert [j.get("outcome") for j in d1["journal"]] == ["resurrect_failed"]
    assert len(d1["notify"]) == 1                           # the failure texts once...
    # a second failed attempt in the SAME down-streak does not re-text
    r2 = _run(T0 + 6 * MIN, _dead(T0 + 6 * MIN), d1["state"])
    d2 = wd.after_resurrect(T0 + 6 * MIN, _cfg(), r2["state"], r2["resurrect"], rc=2)
    assert d2["notify"] == []
    # recovery (runner healthy again) re-arms the failure text for a future streak
    healthy = _run(T0 + 12 * MIN, _view(T0 + 12 * MIN), d2["state"])
    assert healthy["state"]["resurrection"]["failure_notified"] is False


def test_kill_switch_suppresses_resurrection_entirely():
    r = _run(T0, _dead(T0, kill_switch=True))
    assert r["resurrect"] is None
    assert _outcomes(r) == ["disabled"]
    assert r["notify"] == []
    assert r["state"]["episode"] is None


def test_resurrection_coexists_with_a_separate_alert_debugger_episode():
    # A dead runner that ALSO left an ALERT on disk: resurrection restarts the runner AND the alert
    # (a separate concern) still drives the debugger episode. Only heartbeat_stale is rerouted.
    r = _run(T0, _dead(T0, alert={"reasons": ["issues_json_corrupt"]}))
    assert r["resurrect"] is not None
    ep = r["state"]["episode"]
    assert ep is not None and ep["signals"] == ["alert"]   # heartbeat_stale rerouted; alert remains


def test_coerce_state_handles_a_wrong_typed_resurrection_slice():
    for garbage in (None, [], "x", {"resurrection": "weird", "next_resurrection": "9"},
                    {"resurrection": {"attempts": "nope"}}):
        st = wd.coerce_state(garbage)
        assert isinstance(st["resurrection"], dict)
        assert isinstance(st["resurrection"]["attempts"], list)
        assert st["next_resurrection"] >= 1
        r = wd.evaluate(T0, _cfg(), _view(), st)           # never crashes
        assert r["resurrect"] is None
