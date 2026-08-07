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


# =================== the boot re-announce (one hold, one engine generation) ===================

def test_boot_clears_every_hold_dedup_stamp_so_the_next_tick_re_journals(rig):
    write_state(rig, {"i101": {"status": "ready", "launch_hold_reason": "waiting on #3"},
                      "i102": {"status": "ready", "wildcard_hold_journaled": True},
                      "i103": {"status": "ready", "queue_invalid_signature": "sig-1"}})
    assert rig.r._rearm_hold_journaling(now=NOW) == ["i101", "i102", "i103"]
    st = json.loads((rig.home / "state" / "issues.json").read_text())["issues"]
    assert st["i101"]["launch_hold_reason"] is None
    assert st["i102"]["wildcard_hold_journaled"] is False
    assert st["i103"]["queue_invalid_signature"] is None
    # ...and it is on the record that the loop re-armed, not that the holds simply vanished.
    assert acts(rig, "hold_rearm")[0]["issues"] == ["i101", "i102", "i103"]


def test_the_re_announce_preserves_the_age_of_a_hold_it_re_arms(rig):
    # THE trap: a hold that re-announces every boot but restarts its clock every boot can never be
    # older than one boot, so the age alert could never fire on the very stall it exists to catch.
    write_state(rig, {"i101": {"status": "ready", "launch_hold_reason": "waiting on #3",
                               "launch_hold_since": NOW - 3 * DAY}})
    rig.r._rearm_hold_journaling(now=NOW)
    assert state_of(rig)["launch_hold_since"] == NOW - 3 * DAY


def test_the_re_announce_backfills_a_clock_a_pre_405_stamp_never_had(rig):
    # A stamp written by an older engine has no clock at all. Backfill it from THIS boot: the age
    # then understates how long the hold has really stood, which is the honest direction — the loop
    # can prove "at least since this boot" and must never invent more than it can prove.
    write_state(rig, {"i101": {"status": "ready", "launch_hold_reason": "waiting on #3"}})
    rig.r._rearm_hold_journaling(now=NOW)
    assert state_of(rig)["launch_hold_since"] == NOW


def test_a_terminal_issue_is_not_re_announced(rig):
    # merged/parked/bounced/needs_william ended the episode: re-announcing a stamp left on one of
    # those would put history back on the board as a live hold.
    write_state(rig, {"i101": {"status": "parked", "launch_hold_reason": "waiting on #3"},
                      "i102": {"status": "merged", "queue_invalid_signature": "sig-1"}})
    assert rig.r._rearm_hold_journaling(now=NOW) == []
    assert state_of(rig)["launch_hold_reason"] == "waiting on #3"     # left exactly as it was


def test_a_loop_with_nothing_held_re_announces_nothing_and_journals_nothing(rig):
    write_state(rig, {"i101": {"status": "ready"}})
    assert rig.r._rearm_hold_journaling(now=NOW) == []
    assert acts(rig, "hold_rearm") == []


def test_the_re_announce_never_aborts_a_boot(rig):
    # It runs on the boot path, so it must fail like every other diagnostic there: report, never
    # raise. No issues.json at all (a first-ever boot) and a corrupt one both degrade to a no-op.
    assert rig.r._rearm_hold_journaling(now=NOW) == []                   # no state file yet
    (rig.home / "state").mkdir(parents=True, exist_ok=True)
    (rig.home / "state" / "issues.json").write_text("{{{not json")
    assert rig.r._rearm_hold_journaling(now=NOW) == []
    write_state(rig, {"i101": "nope", "i102": 7, "i103": None})          # wrong-typed entries
    assert rig.r._rearm_hold_journaling(now=NOW) == []
    loopstate.save(str(rig.home / "state" / "issues.json"), {"version": 1, "issues": "nope"})
    assert rig.r._rearm_hold_journaling(now=NOW) == []


def test_a_standing_hold_re_journals_on_the_first_tick_after_a_boot(tmp_path, monkeypatch):
    # The DoD, end to end: stamp present -> boot -> the first tick says it again. A worker died
    # while usage is over ceiling, so the relaunch is held and nothing will move this lane; the hold
    # was already journaled once in a previous engine generation and its stamp is still on disk.
    r = _runner(tmp_path, monkeypatch, usage=OVER_CEILING)
    home = tmp_path / "home"
    (home / "state" / "exited").mkdir(parents=True, exist_ok=True)
    (home / "state" / "exited" / "i101").write_text("100 rc=1 (died)\n")
    held = ("no usage headroom (the meter is unreadable/unhealthy, or at-or-over a ceiling) — "
            "the restart waits for quota, exactly as a fresh launch does")
    loopstate.save(str(home / "state" / "issues.json"),
                   {"version": 1, "issues": {"i101": {"status": "running", "retries": 0,
                                                      "launch_hold_reason": held}}})
    # Without the boot re-announce this tick is SILENT — the stamp already matches the cause.
    r.tick(now=NOW)
    assert [a for a in journal.read(str(home)) if a.get("act") == "launch_hold"] == []
    r._rearm_hold_journaling(now=NOW)
    r.tick(now=NOW + 15)
    said = [a for a in journal.read(str(home)) if a.get("act") == "launch_hold"]
    assert len(said) == 1 and said[0]["id"] == "i101" and "usage" in said[0]["reason"]


def test_run_re_announces_before_the_first_tick(rig, monkeypatch):
    # Wiring: the boot path itself calls it, not just a test. Assert on the ORDER — a re-announce
    # that ran after the first tick would let that tick pass silently, which is the whole defect.
    write_state(rig, {"i101": {"status": "ready", "launch_hold_reason": "waiting on #3"}})
    seen = []
    monkeypatch.setattr(rig.r, "tick", lambda *a, **k: seen.append("tick"))
    real = rig.r._rearm_hold_journaling
    monkeypatch.setattr(rig.r, "_rearm_hold_journaling",
                        lambda *a, **k: (seen.append("rearm"), real(*a, **k))[1])
    rig.r.run(max_ticks=1, sleep=lambda *_: None)
    assert seen[:2] == ["rearm", "tick"]


# =================== the age clock, stamped where the hold is stamped ===================

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
