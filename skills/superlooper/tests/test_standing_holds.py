"""Standing holds re-announce and age out loud (issue #405) — the runner half.

Hold CONDITIONS are re-derived every tick, which is right; their JOURNALING is deduped on durable
stamps — the #150 launch-hold reason, the #225 queue-lint defect signature, the #36 wildcard-hold
boolean — that reset only when an issue launches, parks, or is re-approved. An issue that never does
any of those goes completely silent, forever, and across engine updates too: there was no state
migration and no stamp reaper. That is not a hypothetical; it was a three-day silent hold on the eApp
loop, 2026-07-31 -> 08-03, with no journal line, no alert and no morning-report mention.

Three properties are pinned here:

  * **Boot re-announces.** Every standing hold speaks once per ENGINE GENERATION, not once per
    lifetime: the boot clears the dedup stamps so the first tick re-derives and re-journals them.
  * **Holds carry a clock.** The re-announce must NOT reset the age — an alert on "held 3 days" is
    worthless if every restart zeroes it — so the age clock is a separate field, preserved across
    the re-announce and backfilled for a stamp written by an engine that had none.
  * **A held RELAUNCH is not a live flight.** A lane whose worker exited while usage is over ceiling
    keeps its `running` status; the durable state has to name the dead worker, or every surface
    reading it paints a live flight and then a frozen session while the runner waits for quota.

Same rig as test_runner.py: fake-gh via SL_GH, injected run_script, no real GitHub.
"""
import json
import shutil
from pathlib import Path

import pytest

import journal
import loopstate
import report
import runner as runner_mod

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

NOW = 1_750_000_000
DAY = 24 * 3600

OVER_CEILING = {"auth_status": "ok", "five_hour_pct": 95.0, "seven_day_pct": 20.0}
HEADROOM = {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0}


def make_config(**over):
    c = {
        "repo": "o/r", "dev_branch": "main", "prod_branch": None,
        "lanes": 2, "affinity": "hard", "areas": {"frontend": ["src/f/**"], "api": ["src/a/**"]},
        "touches_required": False, "required_checks": ["ci"], "merge_method": "squash",
        "ship_cmd": None, "ship_recheck_cmd": None,
        "report_required_sections": ["Tests"], "bright_lines": [],
        "cleanup_merged_worktrees": True, "report_time": "08:45",
        "models": {"worker": "opus", "debugger": "fable"},
        "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2},
        "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
               "quarantine": [], "nightly_time": "02:00"},
        "notify": {"imessage_to": None, "cmd": None},
        "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
    }
    c.update(over)
    return c


def _runner(tmp_path, monkeypatch, usage=HEADROOM):
    fixdir = tmp_path / "gh"
    if not fixdir.exists():
        shutil.copytree(_FIXTURES, fixdir)
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))
    monkeypatch.delenv("GH_FAIL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    r = runner_mod.Runner(repo=str(repo), config=make_config(), state_home=str(tmp_path / "home"),
                          pane="pane-1", run_script=lambda *a, **k: 0, fetch_usage=lambda: dict(usage))
    r._anchor_status = lambda: {"ok": True, "reason": ""}
    return r


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch)
    return type("Rig", (), {"r": r, "home": tmp_path / "home", "tmp": tmp_path,
                            "monkeypatch": monkeypatch})


def state_of(rig, iid="i101"):
    return json.loads((rig.home / "state" / "issues.json").read_text())["issues"][iid]


def write_state(rig, issues):
    loopstate.save(str(rig.home / "state" / "issues.json"), {"version": 1, "issues": issues})


def acts(rig, act):
    return [r for r in journal.read(str(rig.home)) if r.get("act") == act]


# =================== the re-announce (one hold, one engine generation) ===================

def _exited_over_ceiling(tmp_path, monkeypatch, stamp=None, **ist):
    """A lane whose worker DIED while usage sits over the ceiling — the realized #405 case. Its hold
    is journal-only, so nothing but the journal and the durable state can ever say it."""
    r = _runner(tmp_path, monkeypatch, usage=OVER_CEILING)
    home = tmp_path / "home"
    (home / "state" / "exited").mkdir(parents=True, exist_ok=True)
    (home / "state" / "exited" / "i101").write_text("100 rc=1 (died)\n")
    issue = {"status": "running", "retries": 0}
    if stamp is not None:
        issue["launch_hold_reason"] = stamp
    issue.update(ist)
    loopstate.save(str(home / "state" / "issues.json"), {"version": 1, "issues": {"i101": issue}})
    return r, home


HELD_FOR_QUOTA = ("no usage headroom (the meter is unreadable/unhealthy, or at-or-over a ceiling) — "
                  "the restart waits for quota, exactly as a fresh launch does")


def test_a_standing_hold_re_journals_once_on_the_first_tick_of_a_new_generation(tmp_path, monkeypatch):
    # THE DoD, end to end: a stamp written by a PREVIOUS engine generation is still on disk, and the
    # cause has not changed — so under the old reason-only dedup this loop was silent for life.
    r, home = _exited_over_ceiling(tmp_path, monkeypatch, stamp=HELD_FOR_QUOTA,
                                   launch_hold_generation="1700000000.999")
    r.tick(now=NOW)
    said = [a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]
    assert len(said) == 1 and said[0]["id"] == "i101" and "usage" in said[0]["reason"]
    # ...and then quiet: ONCE per generation, never per tick. A 15s tick must not be spammed.
    for i in range(1, 4):
        r.tick(now=NOW + 15 * i)
    assert len([a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]) == 1


def test_a_stamp_from_THIS_generation_stays_silent(tmp_path, monkeypatch):
    # The mirror, and the reason the generation is a KEY rather than a reaper: a hold this process
    # already announced must not announce again just because the process ticked.
    r, home = _exited_over_ceiling(tmp_path, monkeypatch)
    r.tick(now=NOW)                                    # first tick of this generation: says it
    assert len([a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]) == 1
    r.tick(now=NOW + 15)
    assert len([a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]) == 1


def test_a_RESTART_is_what_re_arms_the_hold(tmp_path, monkeypatch):
    # A new process = a new generation = one more announcement. This is the whole mechanism: the
    # restart is exactly when an operator's model of the loop is rebuilt, and when a republished
    # engine takes effect.
    r, home = _exited_over_ceiling(tmp_path, monkeypatch)
    r.tick(now=NOW)
    r2 = _runner(tmp_path, monkeypatch, usage=OVER_CEILING)
    r2._hold_generation = "9999999999.1"               # a genuinely different process
    r2.tick(now=NOW + 60)
    assert len([a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]) == 2


def test_the_re_announce_never_resets_the_age_of_the_hold_it_re_states(tmp_path, monkeypatch):
    # THE trap: a hold that re-announces every restart but restarts its clock every restart could
    # never be older than one restart, so the age alert could never fire on the very stall it exists
    # to catch. The clock is a separate field precisely so it survives the re-key.
    r, home = _exited_over_ceiling(tmp_path, monkeypatch, stamp=HELD_FOR_QUOTA,
                                   launch_hold_generation="old", launch_hold_since=NOW - 3 * DAY)
    r.tick(now=NOW)
    ist = json.loads((home / "state" / "issues.json").read_text())["issues"]["i101"]
    assert ist["launch_hold_since"] == NOW - 3 * DAY
    assert ist["launch_hold_generation"] == r._hold_generation      # re-keyed to THIS generation


def test_a_pre_405_stamp_gains_a_clock_on_its_first_re_announce(tmp_path, monkeypatch):
    # A stamp written by an engine older than #405 has no clock at all, and no generation either —
    # which is exactly what makes the generation half of the key re-journal it. The clock then starts
    # from that tick: the age understates how long the hold really stood, the honest direction.
    r, home = _exited_over_ceiling(tmp_path, monkeypatch, stamp=HELD_FOR_QUOTA)
    r.tick(now=NOW)
    assert json.loads((home / "state" / "issues.json").read_text())["issues"]["i101"][
        "launch_hold_since"] == NOW


def test_a_boot_that_cannot_re_derive_its_holds_never_ERASES_them(tmp_path, monkeypatch):
    # The reason this re-keys instead of clearing the stamps at boot. Every launch-phase hold lives
    # behind `not launches_held` — and a SLEEPING DISPLAY is normal overnight — so a 3am resurrection
    # restart that cleared the stamps would leave the 08:45 report saying "nothing is held" with the
    # whole queue held: the exact silence this issue exists to end, reintroduced by its own fix.
    r = _runner(tmp_path, monkeypatch)
    home = tmp_path / "home"
    r._display_asleep = lambda: True                   # the overnight condition, held all night
    loopstate.save(str(home / "state" / "issues.json"),
                   {"version": 1, "issues": {"i101": {
                       "status": "ready", "queue_invalid_signature": "sig-1",
                       "queue_invalid_since": NOW - 3 * DAY, "queue_invalid_generation": "old"}}})
    r.tick(now=NOW)
    assert [a for a in journal.read(str(home)) if a.get("act") == "queue_invalid"] == []  # held
    ist = json.loads((home / "state" / "issues.json").read_text())["issues"]["i101"]
    assert ist["queue_invalid_signature"] == "sig-1"           # the stamp SURVIVES
    assert ist["queue_invalid_since"] == NOW - 3 * DAY         # ...and so does its age
    held = report.standing_holds({"version": 1, "issues": {"i101": ist}})
    assert len(held) == 1 and held[0]["kind"] == "queue_invalid"


def test_an_engine_generation_is_unique_per_process(rig):
    # Two live runners cannot share a pid, and a restart cannot reuse a second — so the token is
    # distinct wherever it matters. (A re-exec preserves the pid by design; a re-exec inside the same
    # second reuses the token and skips one re-announce of a hold journaled seconds earlier.)
    assert isinstance(rig.r._hold_generation, str) and rig.r._hold_generation
    assert rig.r._mint_hold_generation(now=NOW) != rig.r._mint_hold_generation(now=NOW + 1)


# =================== the age clock, stamped where the hold is stamped ===================

def test_the_all_clear_ENDS_the_age_clock_rather_than_preserving_it(rig):
    # #172 has no clear-the-stamp verb: it retires a stale hold by OVERWRITING it with prose that
    # says the gate now passes. That is an episode BOUNDARY, and the clock has to observe it —
    # otherwise the retired hold's clock survives the all-clear and is inherited by the next,
    # unrelated hold, which the report would then call days old and alert as a stall on its first
    # minute. (The report already refuses to LIST an all-clear; this is the other half.)
    import actions
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "waiting on #3"}, NOW - 3 * DAY)
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "all_clear": True,
                             "reason": actions._LANE_BOUND_AFTER_UNLANDED_READ}, NOW - 3 * DAY + 300)
    assert state_of(rig)["launch_hold_since"] is None
    # ...so a genuinely NEW hold three days later is timed from ITSELF, not from the retired one.
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "no usage headroom"}, NOW)
    ist = state_of(rig)
    assert ist["launch_hold_since"] == NOW
    assert report.standing_holds({"version": 1, "issues": {"i101": ist}})[0]["since"] == NOW


def test_decide_flags_the_all_clear_and_nothing_else(rig):
    # The flag must ride the ONE reason that is an all-clear. Asked of decide itself, so a future
    # edit that re-words the retirement without tagging it is caught here rather than by an owner
    # reading "held 3d" on a minute-old hold.
    import actions
    said = [a for a in actions.decide(
        NOW, make_config(), {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0,
                             "last_ok_at": NOW, "first_attempt_at": NOW - 60},
        [{"num": 5, "id": "i5", "title": "t", "type": "build",
          "labels": ["agent-ready", "type:build"], "touches": ["frontend"], "blocked_by": [3],
          "parent": None, "created_at": "2026-07-01T00:00:05Z", "priority": 2, "expedite": False}],
        [], [],
        {"issues_state": {"version": 1, "issues": {}}, "blocked": {}, "reports": {}, "exited": {},
         "frozen": None, "alert": None, "live_lock_ids": set(), "filed_fingerprints": {},
         "settled_fix_issues": {}, "local_date": "2026-07-02", "local_hhmm": "12:00",
         "last_report_date": "2026-07-02"},
        {"stale": False, "consecutive_failures": 0, "closed_nums": set(), "closed_read_ok": False,
         "prs": {}, "issue_comments": {}, "dev_checks": []})
        if a.get("act") == "launch_hold"]
    # A REAL hold (the unlanded closed read) is never flagged as an all-clear.
    assert len(said) == 1 and said[0]["all_clear"] is False
    assert said[0]["reason"].startswith(actions.UNLANDED_CLOSED_READ_PREFIX)


def test_a_launch_hold_stamps_its_reason_its_clock_and_whether_a_worker_died(rig):
    rig.r._exec_launch_hold({"act": "launch_hold", "id": "i101", "num": 101,
                             "reason": "no usage headroom", "relaunch": True}, NOW)
    ist = state_of(rig)
    assert ist["launch_hold_reason"] == "no usage headroom"
    assert ist["launch_hold_since"] == NOW and ist["relaunch_held"] is True


def test_a_re_stamped_launch_hold_never_restarts_its_own_clock(rig):
    # The re-announce clears the reason and the first tick re-stamps it. The clock must survive that
    # round trip, or the age would reset to zero on every boot.
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "held", "relaunch": False}, NOW)
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "held", "relaunch": False},
                            NOW + 5 * DAY)
    assert state_of(rig)["launch_hold_since"] == NOW


def test_the_wildcard_and_queue_contract_holds_stamp_their_own_clocks(rig):
    rig.r._exec_wildcard_hold({"id": "i101", "num": 101, "reason": "the lane serializes"}, NOW)
    rig.r._exec_queue_invalid({"id": "i102", "num": 102, "signature": "sig-1",
                               "reason": "missing `## Loop metadata`"}, NOW)
    assert state_of(rig, "i101")["wildcard_hold_since"] == NOW
    assert state_of(rig, "i102")["queue_invalid_since"] == NOW
    # ...and neither restarts its clock while the same episode stands.
    rig.r._exec_wildcard_hold({"id": "i101", "num": 101, "reason": "the lane serializes"}, NOW + DAY)
    assert state_of(rig, "i101")["wildcard_hold_since"] == NOW


def test_a_launch_clears_every_hold_clock_it_ends(rig):
    # The stamps clear on launch today; their clocks and the dead-worker flag must clear WITH them,
    # or a launched lane would keep reporting a hold that ended when it started.
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "held", "relaunch": True}, NOW)
    rig.r._exec_wildcard_hold({"id": "i101", "num": 101, "reason": "serialized"}, NOW)
    rig.r._exec_queue_invalid({"id": "i101", "num": 101, "signature": "s", "reason": "bad"}, NOW)
    rig.r._parsed_by_id = {"i101": {"num": 101, "type": "build", "touches": ["frontend"],
                                    "labels": ["agent-ready", "type:build"], "title": "t"}}
    rig.r._raw_by_id = {"i101": {"body": ""}}
    rig.r._exec_launch({"act": "launch", "id": "i101", "num": 101, "branch": "sl/i101-t",
                        "touches": ["frontend"], "soft_overlap": False, "orphan": False}, NOW)
    ist = state_of(rig)
    for k in ("launch_hold_since", "wildcard_hold_since", "queue_invalid_since"):
        assert ist[k] is None, k
    assert ist["relaunch_held"] is False


def test_a_recovery_relaunch_clears_the_relaunch_hold_it_ends(rig):
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "held", "relaunch": True}, NOW)
    rig.r._exec_recover({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    ist = state_of(rig)
    assert ist["launch_hold_reason"] is None and ist["launch_hold_since"] is None
    assert ist["relaunch_held"] is False


def test_a_park_clears_the_launch_hold_clock_with_its_reason(rig):
    rig.r._exec_launch_hold({"id": "i101", "num": 101, "reason": "held", "relaunch": True}, NOW)
    rig.r._parsed_by_id = {"i101": {"num": 101, "labels": ["agent-ready"]}}
    rig.r._exec_park({"act": "park", "id": "i101", "num": 101, "memo": "m",
                      "needs_william": False}, NOW)
    ist = state_of(rig)
    assert ist["launch_hold_reason"] is None and ist["launch_hold_since"] is None
    assert ist["relaunch_held"] is False


# =================== a held relaunch is not a live flight (DoD 4) ===================

def test_an_exited_worker_over_ceiling_leaves_state_that_names_the_usage_hold(tmp_path, monkeypatch):
    # The realized case: the worker died, usage is over the ceiling, so the runner is deliberately
    # waiting for quota. The lane's status stays `running` (a hold is a WAIT, not a status change —
    # that is #150's boundary and this issue does not move it), so the durable record is the ONLY
    # thing that can tell a reader the flight is not live. It must name both facts.
    r = _runner(tmp_path, monkeypatch, usage=OVER_CEILING)
    home = tmp_path / "home"
    (home / "state" / "exited").mkdir(parents=True, exist_ok=True)
    (home / "state" / "exited" / "i101").write_text("100 rc=1 (died)\n")
    loopstate.save(str(home / "state" / "issues.json"),
                   {"version": 1, "issues": {"i101": {"status": "running", "retries": 0}}})
    r.tick(now=NOW)
    ist = json.loads((home / "state" / "issues.json").read_text())["issues"]["i101"]
    assert ist["status"] == "running"                      # unchanged: no hold/park semantics moved
    assert ist["relaunch_held"] is True                    # ...but no longer indistinguishable
    assert "usage" in ist["launch_hold_reason"] or "quota" in ist["launch_hold_reason"]
    assert ist["launch_hold_since"] == NOW                 # and it is aged from here
    # ...and the standing-holds view a report (or any later surface) reads names the real cause.
    held = report.standing_holds({"version": 1, "issues": {"i101": ist}})
    assert len(held) == 1 and held[0]["relaunch"] is True
    assert "quota" in held[0]["reason"] or "usage" in held[0]["reason"]


# =================== the morning report gets the holds ===================

def test_the_morning_report_view_carries_the_loopstate_the_holds_come_from(rig):
    write_state(rig, {"i101": {"status": "ready", "launch_hold_reason": "waiting on #3",
                               "launch_hold_since": NOW - 3 * DAY}})
    rig.r._morning_report_hook("2026-08-07", NOW)
    text = (rig.home / "reports" / "morning-2026-08-07.md").read_text()
    assert "## Standing holds" in text
    assert "#101" in text.split("## Standing holds")[1] and "3d 0h" in text
    assert "STALL" in text                                  # past the threshold -> alert tier
