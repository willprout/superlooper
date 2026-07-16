"""The standing truth strip (issue #166) — the line that stops the dashboard from silently lying.

The owner read this surface for weeks as a live, perfect mirror of the runner. It never was one: a
dead session showed as "launching", a day-stale server sat under fresh UI, an externally-closed
issue was never absorbed. Every one of those was the surface looking CONFIDENT while blind.

So the strip states, always and without being asked, the three things that decide how much of what
is on screen to believe:

  * is the loop alive? (the runner's last tick)
  * is this the runner's own view, or a blind/second-hand one? (dark-tower / stale)
  * is the engine the loop is RUNNING the one that was merged? (publish drift)

The whole test file is really one assertion said many ways: **no failure may render as an
all-clear.** ``test_a_stale_runner_view_is_never_a_confident_mirror`` is the DoD's own case.

Pure semantics, per design record B.1 — the JS binds these strings and derives nothing, so the strip
and the board can never tell two different stories.
"""
import engine
import flights
import truth


def _live(tick="4s ago", data="12s ago"):
    """A source_mode verdict shaped exactly as lib/flights.source_mode returns it."""
    return {"mode": flights.SOURCE_LIVE, "reason": None, "tick_age": 4.0, "data_age": 12.0,
            "tick_age_text": tick, "data_age_text": data, "silent_since": None, "banner": None}


def _silent(tick_age=900.0, tick="15m ago"):
    return {"mode": flights.SOURCE_FALLBACK, "reason": flights.FALLBACK_RUNNER_SILENT,
            "tick_age": tick_age, "data_age": 30.0, "tick_age_text": tick,
            "data_age_text": "30s ago", "silent_since": 1000.0,
            "banner": {"lines": ["runner silent since 09:12", "showing GitHub directly"]}}


def _never_ticked():
    return {"mode": flights.SOURCE_FALLBACK, "reason": flights.FALLBACK_RUNNER_SILENT,
            "tick_age": None, "data_age": None, "tick_age_text": "?", "data_age_text": "?",
            "silent_since": None, "banner": {"lines": ["runner silent — no tick seen"]}}


def _no_view():
    return {"mode": flights.SOURCE_FALLBACK, "reason": flights.FALLBACK_NO_VIEW,
            "tick_age": 3.0, "data_age": 30.0, "tick_age_text": "3s ago",
            "data_age_text": "30s ago", "silent_since": None,
            "banner": {"lines": ["runner publishes no view"]}}


def _drift(behind=3):
    return {"known": True, "behind": behind, "installed_sha": "abc1234",
            "installed_at": "2026-07-11", "source": "/src",
            "message": engine.drift_message(behind), "remedy": "bin/install.sh"}


def _engine_ok():
    return {"known": True, "behind": 0, "installed_sha": "abc1234", "installed_at": "2026-07-16",
            "source": "/src", "message": None, "remedy": "bin/install.sh"}


# --------------------------- the tick line: is the loop alive? ---------------------------

def test_a_healthy_tick_is_stated_plainly_and_calmly():
    t = truth.banner(_live())
    assert t["tick"]["state"] == "ok"
    assert t["tick"]["text"] == "last tick 4s ago"
    assert t["level"] == "ok"


def test_a_silent_runner_says_the_loop_may_be_down():
    # The DoD's phrase. "last tick 15m ago" alone is a NUMBER — the owner has to know the threshold
    # to read it. Naming the conclusion is the difference between data and truth.
    t = truth.banner(_silent())
    assert t["tick"]["state"] == "down"
    assert t["tick"]["text"] == "last tick 15m ago — loop may be down"
    assert t["level"] == "down"


def test_a_runner_that_never_ticked_never_renders_as_fresh():
    # No heartbeat at all must not become "last tick ? ago" — a shrug where the alarm belongs.
    t = truth.banner(_never_ticked())
    assert t["tick"]["state"] == "down"
    assert t["tick"]["text"] == "no tick seen — loop may be down"
    assert t["level"] == "down"


def test_a_ticking_runner_with_no_published_view_is_not_called_down():
    # An engine older than #146 ticks perfectly and simply publishes nothing. Calling that "loop may
    # be down" would send the owner to debug a runner that is fine.
    t = truth.banner(_no_view())
    assert t["tick"]["state"] == "ok"
    assert t["tick"]["text"] == "last tick 3s ago"


# --------------------------- the data line: whose truth is this? ---------------------------

def test_live_names_the_runners_own_view_as_the_source():
    t = truth.banner(_live())
    assert t["data"]["state"] == "ok"
    assert t["data"]["text"] == "data 12s ago"


def test_a_stale_runner_view_is_never_a_confident_mirror():
    # THE DoD case. When the runner's view has gone stale the dashboard is showing GitHub directly —
    # a second opinion on a stale premise. That must be impossible to read as the real thing.
    t = truth.banner(_silent())
    assert t["data"]["state"] == "blind"
    assert "not the runner's view" in t["data"]["text"]
    assert t["level"] == "down"


def test_an_unreachable_github_is_an_explicit_dark_tower_never_a_blank():
    # Fallback AND GitHub down: the surface knows nothing new at all. The strip must say so rather
    # than show a calm "data 30s ago" over a picture that is going nowhere.
    t = truth.banner(_silent(), github={"unreachable": True})
    assert t["data"]["state"] == "dark"
    assert "can't reach GitHub" in t["data"]["text"]


def test_a_dark_tower_never_dates_the_screen_by_a_failed_read():
    # Regression, found by driving a real browser against a real server with gh removed (issue
    # #166). source_mode dates FALLBACK by when the dashboard's own fetch last RAN — and with the
    # link down that fetch RAN, then came back empty. The strip rendered "data 12s ago · can't reach
    # GitHub — the tower is blind": a freshness number for a layer holding nothing, sitting next to
    # the sentence contradicting it. A blind surface may not state an age at all.
    src = _silent()
    src["data_age"], src["data_age_text"] = 12.0, "12s ago"
    t = truth.banner(src, github={"unreachable": True})
    assert "12s ago" not in t["data"]["text"], (
        "a blind tower must not date the screen by a read that returned nothing")
    assert "data ?" in t["data"]["text"]


def test_an_unknown_age_is_a_question_mark_never_a_confident_zero():
    # "0s ago" would claim the freshest possible data at the exact moment we have none.
    t = truth.banner(_never_ticked())
    assert "?" in t["data"]["text"]
    assert "0s" not in t["data"]["text"]


# --------------------------- the engine line: is the merged fix live? ---------------------------

def test_engine_drift_rides_the_strip_with_the_servers_own_sentence():
    t = truth.banner(_live(), engine=_drift(3))
    assert t["engine"]["state"] == "drift"
    assert t["engine"]["text"] == ("3 engine fixes merged but not yet live; re-run the installer "
                                   "to switch them on")
    assert t["level"] == "notice", "drift is a notice, not an alarm — nothing is broken"


def test_a_live_engine_says_nothing():
    # §0.2: a surface that congratulates itself every two seconds is one the owner stops reading.
    assert truth.banner(_live(), engine=_engine_ok())["engine"] is None


def test_no_engine_source_says_nothing():
    # A friend watching only their own repo has no monorepo to compare against.
    assert truth.banner(_live(), engine=None)["engine"] is None


def test_an_unknown_engine_state_is_stated_never_swallowed():
    eng = {"known": False, "behind": None, "installed_sha": "abc1234", "installed_at": None,
           "source": "/src", "message": "can't tell which engine build is live — reasons",
           "remedy": "bin/install.sh"}
    t = truth.banner(_live(), engine=eng)
    assert t["engine"]["state"] == "unknown"
    assert t["engine"]["text"] == "can't tell which engine build is live — reasons"


def test_an_engine_with_nothing_to_say_stays_silent():
    # known:False AND message:None — nothing was ever published here. No mystery, no line.
    eng = {"known": False, "behind": None, "installed_sha": None, "installed_at": None,
           "source": None, "message": None, "remedy": "bin/install.sh"}
    assert truth.banner(_live(), engine=eng)["engine"] is None


# --------------------------- the level: the worst thing wins ---------------------------

def test_a_down_loop_outranks_engine_drift():
    t = truth.banner(_silent(), engine=_drift(2))
    assert t["level"] == "down", "a dead loop is the headline; the drift can wait"


def test_the_strip_never_derives_the_mode_itself():
    # design B.1: the mode is decided ONCE in flights.source_mode. If the strip second-guessed it,
    # the banner and the board could tell two different stories — the bug, wearing a new hat.
    src = _silent()
    src["reason"] = None            # server says: fallback, but NOT because the runner is silent
    assert truth.banner(src)["tick"]["state"] == "ok"


# --------------------------- never raises into the poll ---------------------------

def test_a_missing_or_junk_source_degrades_to_unknown_never_an_all_clear():
    for junk in (None, {}, {"mode": "live"}):
        t = truth.banner(junk)
        assert t["tick"]["state"] == "down", "no source verdict is not an all-clear"
        assert t["level"] == "down"
