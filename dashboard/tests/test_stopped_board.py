"""Issue #365 — the board tells a deliberate stop apart from a crash.

Half of this issue is the button; this is the other half, and it is the half the owner lives with.
`superlooper stop` (issue #239) leaves a marker at ``state/runner.stopped`` precisely because a
deliberate stop is otherwise *the exact shape of a crash* — a stale heartbeat and no live runner —
and the engine's guardians are built to fix that shape at 3am. The dashboard is a third guardian:
it greys the whole surface RUNNER DOWN and fires the dead-man's-switch push. Reading the marker is
what stops it from texting the owner about an outage they created on purpose.

Three states, and the distinctions are the whole point:

* **off** — the marker is down and there is a positive "no live runner" read (the heartbeat is
  stale past the down threshold). The loop is off because the owner said so. Shown as its own
  state, never RUNNER DOWN, and it does NOT fire the push.
* **stopping** — the marker is down and the runner is still inside its tick. Transient and
  truthful: it is on its way out, and calling it a crash would be wrong in the other direction.
* **not taken** — the marker is down and the runner has completed a tick SINCE it landed. That is
  the engine's own STOP NOT TAKEN, and it is the dangerous one: the owner believes the loop is off,
  it is not, and the moment it does die the guardians will stand down over it.

The marker's ``stopped_at`` is what separates the last two, and it has to be. The down threshold is
five minutes, so a purely heartbeat-based read would call every SUCCESSFUL stop "not taken" for the
first five minutes — a five-minute lie after every single tap. A tick that landed after the marker
is proof the stop did not hold; nothing weaker is.

And the direction of every failure is deliberate: an unreadable or unparseable marker still counts
as present (existence is the signal, exactly as the engine reads it), because every reader of that
file treats absent as permission to restart the loop.
"""
import json
import os
import shutil

import pytest

import flights
import readers
import server
import watchdog as watchdog_mod


def _home(tmp_path, *, stopped=None, heartbeat=None, now=1_000_000):
    """A minimal state home: an optional stop marker, an optional heartbeat epoch."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    if stopped is not None:
        (state / "runner.stopped").write_text(
            stopped if isinstance(stopped, str) else json.dumps(stopped))
    if heartbeat is not None:
        (state / "runner.heartbeat").write_text(str(heartbeat))
    return readers.read_state_home(tmp_path, now=now)


# --------------------------- the reader ---------------------------

def test_the_marker_is_a_contract_key_of_every_state_home_read(tmp_path):
    assert "stopped" in _home(tmp_path)


def test_an_absent_marker_reads_as_none_never_as_an_empty_stop(tmp_path):
    # None is "no stop recorded". Anything else here would make every healthy runner look stopped.
    assert _home(tmp_path)["stopped"] is None


def test_a_recorded_stop_carries_who_and_when(tmp_path):
    facts = _home(tmp_path, stopped={"stopped_at": 999_000, "operator": "William",
                                     "source": "command-center", "home": "login-item"})
    assert facts["stopped"]["operator"] == "William"
    assert facts["stopped"]["stopped_at"] == 999_000
    assert facts["stopped"]["source"] == "command-center"


def test_an_unparseable_marker_still_counts_as_a_stop(tmp_path):
    # Existence is the signal (the engine reads it the same way). A marker lost to a truncated
    # write would hand the loop back to the guardians the owner just overruled.
    assert _home(tmp_path, stopped="{half-writ")["stopped"] == {}


# --------------------------- the three states ---------------------------

NOW = 1_000_000


def test_no_marker_is_no_stop_state():
    st = flights.stop_state(None, heartbeat_epoch=NOW, heartbeat_age=5)
    assert st["present"] is False and st["state"] is None and st["condition"] is None


def test_a_quiet_runner_under_a_marker_is_stopped_by_owner_not_down():
    st = flights.stop_state({"stopped_at": NOW - 900, "operator": "William"},
                            heartbeat_epoch=NOW - 900, heartbeat_age=900,
                            heartbeat_down_seconds=300)
    assert st["state"] == "off" and st["condition"] == flights.STOPPED
    assert st["present"] is True and st["operator"] == "William" and st["at"] == NOW - 900


def test_a_never_ticked_runner_under_a_marker_is_also_stopped():
    # No heartbeat at all is the same positive "nothing is running" read a stale one gives.
    st = flights.stop_state({"stopped_at": NOW - 60}, heartbeat_epoch=None, heartbeat_age=None)
    assert st["condition"] == flights.STOPPED and st["state"] == "off"


def test_a_fresh_stop_over_a_runner_still_in_its_tick_is_stopping_not_a_lie():
    # The marker lands BEFORE anything is taken down, so this window is normal — and it must not
    # be reported as either a completed stop or a failed one.
    st = flights.stop_state({"stopped_at": NOW - 5}, heartbeat_epoch=NOW - 20, heartbeat_age=20,
                            heartbeat_down_seconds=300)
    assert st["state"] == "stopping" and st["condition"] == flights.STOPPED


def test_a_tick_that_landed_after_the_marker_is_a_stop_that_did_not_take():
    # The runner completed a whole tick with the off switch already on disk: the stop did not hold,
    # or a `start` failed to withdraw the marker and latched it over a live runner.
    st = flights.stop_state({"stopped_at": NOW - 600}, heartbeat_epoch=NOW - 30, heartbeat_age=30,
                            heartbeat_down_seconds=300)
    assert st["state"] == "not-taken" and st["condition"] == flights.STOP_NOT_TAKEN


def test_a_tick_after_the_marker_but_long_since_quiet_is_stopped_after_all():
    # It survived the stop briefly, then went quiet: the positive no-live-runner read decides, so
    # this is 'off', not a permanent STOP NOT TAKEN over a loop that really is down.
    st = flights.stop_state({"stopped_at": NOW - 4000}, heartbeat_epoch=NOW - 3990,
                            heartbeat_age=3990, heartbeat_down_seconds=300)
    assert st["state"] == "off"


def test_a_corrupt_marker_over_a_live_runner_never_claims_the_stop_took():
    # No `stopped_at` to compare against ⇒ we cannot prove a tick came after it. With the runner
    # demonstrably ticking, 'stopping' is the honest read; 'off' would be a flat lie.
    st = flights.stop_state({}, heartbeat_epoch=NOW - 10, heartbeat_age=10)
    assert st["present"] is True and st["state"] == "stopping"
    assert st["at"] is None and st["operator"] is None


def test_stop_state_is_total_over_junk():
    # It runs inside the 2s poll; a marker written by an engine this build has not met, or a
    # nonsense timestamp, must degrade rather than raise.
    for junk in ({"stopped_at": "yesterday"}, {"stopped_at": float("nan")}, {"stopped_at": True}):
        st = flights.stop_state(junk, heartbeat_epoch=NOW, heartbeat_age=1)
        assert st["present"] is True and st["state"] in ("stopping", "off")


# --------------------------- the pill: stopped is not crashed ---------------------------

def _state(stop=None, age=None):
    return flights.repo_state(slug="a/b", states=[], spinning=False, merges_frozen=None, alert=None,
                              heartbeat_age=age, heartbeat_down_seconds=300, stop=stop)


def test_without_a_marker_a_stale_heartbeat_is_still_runner_down():
    # The dead-man's switch is untouched by this issue in every case but the deliberate one.
    assert _state(age=900)["state"] == "runner-down"


def test_a_deliberate_stop_replaces_runner_down_rather_than_riding_beside_it():
    st = flights.stop_state({"stopped_at": NOW - 900}, heartbeat_epoch=NOW - 900,
                            heartbeat_age=900, heartbeat_down_seconds=300)
    r = _state(stop=st, age=900)
    assert r["state"] == flights.STOPPED
    assert r["level"] == "attention", "an off switch the owner threw is not an alarm (§0.2)"


def test_stopped_outranks_a_park_so_the_reason_nothing_moves_is_named_first():
    st = flights.stop_state({"stopped_at": NOW - 900}, heartbeat_epoch=None, heartbeat_age=None)
    r = flights.repo_state(slug="a/b", states=[flights.PARKED, flights.AWAITING], spinning=False,
                           merges_frozen=None, alert=None, heartbeat_age=None, stop=st)
    assert r["state"] == flights.STOPPED


def test_an_alert_still_outranks_a_deliberate_stop():
    # A factory-stop the runner declared is a louder fact than an off switch, and stays louder.
    st = flights.stop_state({"stopped_at": NOW - 900}, heartbeat_epoch=None, heartbeat_age=None)
    r = flights.repo_state(slug="a/b", states=[], spinning=False, merges_frozen=None,
                           alert={"reasons": ["x"]}, heartbeat_age=None, stop=st)
    assert r["state"] == "alert" and r["level"] == "alert"


def test_a_stop_that_did_not_take_is_its_own_condition_and_outranks_a_working_one():
    st = flights.stop_state({"stopped_at": NOW - 600}, heartbeat_epoch=NOW - 30, heartbeat_age=30)
    r = _state(stop=st, age=30)
    assert r["state"] == flights.STOP_NOT_TAKEN
    assert flights.condition_rank(flights.STOP_NOT_TAKEN) > flights.condition_rank(flights.STOPPED)
    assert r["level"] == "attention"


def test_stopping_shows_immediately_rather_than_waiting_out_the_down_threshold():
    # The tap has to change the board NOW. Five minutes of "all systems ok" after an owner stopped
    # the loop is five minutes of a surface disagreeing with the thing it watches.
    st = flights.stop_state({"stopped_at": NOW - 3}, heartbeat_epoch=NOW - 10, heartbeat_age=10)
    assert _state(stop=st, age=10)["state"] == flights.STOPPED


# =============================== the assembled snapshot ===============================
# The pure functions above are the decisions; these pin that the snapshot actually CARRIES them —
# and, most of all, that the dashboard's own guardian stands down. The dead-man's-switch push is
# the dashboard's one outbound message: firing RUNNER DOWN at an owner who just tapped Stop is
# precisely the 3am text issue #239 exists to retire, arriving from the other side of the machine.

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
SLUG = "will-titan/superlooper-sandbox"
SNAP_NOW = 1783364300


@pytest.fixture
def home(tmp_path):
    dst = tmp_path / "will-titan__superlooper-sandbox"
    shutil.copytree(FIXTURE, dst)
    (dst / "state" / "ALERT").unlink()          # a real ALERT would outrank the stop we're testing
    (dst / "state" / "merges_frozen.json").unlink()
    return dst


def _config(home):
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300,
            "repos": [{"slug": SLUG, "owner": "will-titan", "name": "superlooper-sandbox",
                       "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
                       "required_checks": ["tests"], "airline": "Sandbox Air"}]}


def _mark(home, *, stopped_at, operator="William", source="command-center"):
    (home / "state" / "runner.stopped").write_text(json.dumps(
        {"stopped_at": stopped_at, "operator": operator, "source": source, "home": "login-item"}))


def _beat(home, epoch):
    (home / "state" / "runner.heartbeat").write_text(str(epoch))


def test_a_crashed_runner_with_no_marker_is_still_runner_down_and_still_pushes(home):
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    assert snap["runner"]["down"] is True
    assert snap["pill"]["message"] == "RUNNER DOWN"
    assert [r["slug"] for r in watchdog_mod.Watchdog().newly_down(snap["runner"]["repos"])] == [SLUG]


def test_a_stopped_runner_is_shown_as_stopped_and_never_as_down(home):
    _mark(home, stopped_at=SNAP_NOW - 900)
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    repo = snap["repos"][0]
    assert repo["state"]["state"] == flights.STOPPED
    assert repo["runner_down"] is False, "a deliberate stop is not an outage"
    assert snap["runner"]["down"] is False and snap["runner"]["repos"][0]["down"] is False


def test_the_dead_mans_switch_does_not_text_the_owner_about_their_own_off_switch(home):
    _mark(home, stopped_at=SNAP_NOW - 900)
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    assert watchdog_mod.Watchdog().newly_down(snap["runner"]["repos"]) == []


def test_the_snapshot_carries_who_stopped_it_when_and_the_way_back(home):
    _mark(home, stopped_at=SNAP_NOW - 900, operator="William")
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    stopped = snap["repos"][0]["stopped"]
    assert stopped["present"] is True and stopped["state"] == "off"
    assert stopped["operator"] == "William" and stopped["at"] == SNAP_NOW - 900
    # The sentence is composed SERVER-side (design B.1) so the JS carries no duration math and the
    # wording is pinned by a test.
    assert "William" in stopped["message"] and "15m" in stopped["message"]
    assert snap["runner"]["stopped"] is True
    assert snap["runner"]["stopped_message"] == stopped["message"]
    # The big word and the sentence under it are decided together, server-side, and read from the
    # SAME repo — a banner whose headline and sub-line described two machines would be worse than
    # no banner.
    assert stopped["headline"] == "STOPPED BY OWNER"
    assert snap["runner"]["stopped_headline"] == stopped["headline"]


def test_the_pill_and_the_trouble_banner_both_name_the_stop(home):
    _mark(home, stopped_at=SNAP_NOW - 900)
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    assert snap["pill"]["message"] == "STOPPED BY OWNER"
    assert snap["pill"]["level"] == "attention"
    assert "topped by owner" in snap["trouble"]["text"]
    assert snap["trouble"]["state"] == flights.STOPPED


def test_a_deliberate_stop_offers_no_debugger_to_deploy(home):
    # The trouble banner carries Deploy Fixer, which LAUNCHES an interactive debugger session on
    # this machine. There is nothing to debug about an off switch the owner threw — offering it
    # would cost a real agent launch and, worse, would frame the owner's own decision as damage.
    _mark(home, stopped_at=SNAP_NOW - 900)
    _beat(home, SNAP_NOW - 900)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    assert snap["trouble"]["present"] is True, "the banner still NAMES the state"
    assert snap["trouble"]["fixable"] is False, "…but offers no fixer for it"


def test_a_stop_that_did_not_take_is_worth_debugging(home):
    # The contradiction is the opposite case: something really is wrong — a stop that would not
    # hold, or an off switch latched over a live runner — and that is a patient.
    _mark(home, stopped_at=SNAP_NOW - 600)
    _beat(home, SNAP_NOW - 30)
    assert server.assemble_snapshot(_config(home), now=SNAP_NOW)["trouble"]["fixable"] is True


def test_ordinary_trouble_still_offers_the_fixer(home):
    _beat(home, SNAP_NOW - 900)
    assert server.assemble_snapshot(_config(home), now=SNAP_NOW)["trouble"]["fixable"] is True


def test_a_stop_that_did_not_take_says_so_rather_than_reassuring_anyone(home):
    # The marker is down and the runner completed a tick after it. The board must NOT read as
    # stopped: the loop is live, merging, and its guardians will stand down the moment it dies.
    _mark(home, stopped_at=SNAP_NOW - 600)
    _beat(home, SNAP_NOW - 30)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    assert snap["repos"][0]["stopped"]["state"] == "not-taken"
    assert snap["repos"][0]["stopped"]["headline"] == "STOP NOT TAKEN"
    assert snap["pill"]["message"] == "STOP NOT TAKEN"
    assert "not taken" in snap["trouble"]["text"].lower()
    assert snap["runner"]["down"] is False


def test_a_stop_still_in_flight_reads_as_stopping(home):
    _mark(home, stopped_at=SNAP_NOW - 3)
    _beat(home, SNAP_NOW - 20)
    snap = server.assemble_snapshot(_config(home), now=SNAP_NOW)
    stopped = snap["repos"][0]["stopped"]
    assert stopped["state"] == "stopping"
    assert stopped["headline"] == "STOPPING", "on its way out is neither off nor failed"
    assert "tick" in stopped["message"].lower(), "say what it is waiting for"
    assert snap["pill"]["message"] == "STOPPED BY OWNER"


def test_a_healthy_repo_carries_an_absent_stop_block_rather_than_no_key(home):
    # The JS binds `repo.stopped` on every render; an absent key is how a surface starts throwing.
    _beat(home, SNAP_NOW - 10)
    stopped = server.assemble_snapshot(_config(home), now=SNAP_NOW)["repos"][0]["stopped"]
    assert stopped["present"] is False and stopped["message"] == "" and stopped["headline"] == ""
