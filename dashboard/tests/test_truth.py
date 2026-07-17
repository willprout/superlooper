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


def test_a_live_dark_tower_keeps_the_runners_own_last_successful_read():
    # Raised in review, and the mirror of the case above. In LIVE, `unreachable` is the RUNNER's own
    # stale flag and the age is when the RUNNER last got through to GitHub — a true, useful number,
    # and exactly what the owner wants while the link is down. Blanking it fails safe but throws away
    # honest information at the moment it matters most.
    t = truth.banner(_live(data="20m ago"), github={"unreachable": True})
    assert t["data"]["state"] == "dark"
    assert "data 20m ago" in t["data"]["text"], (
        "the runner's own last successful read is real — don't discard it")
    assert "can't reach GitHub" in t["data"]["text"]


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


# ===================== the WHOLE field's truth (issue #180) — boring mode =====================
# Boring mode shows every repo in ONE table and has no per-repo field to hang a strip on, so
# between 90s (the strip's threshold) and 300s (the RUNNER DOWN banner's) it rendered a confident
# table of flights with nothing saying the data may be a picture of the past. Same class of silent
# lie #166 exists to close, on the other view.
#
# The aggregation is DECIDED (DoD): worst-of on the LEVEL, exact per-repo on the WORDS. A single
# worst-of sentence would say "loop may be down" without saying whose loop; boring mode's whole rule
# is every visual channel paired with an exact numeral.

def _repo(name, banner):
    return {"name": name, "slug": "acme/" + name, "truth": banner}


def test_boring_modes_strip_reuses_each_repos_own_verdict_never_a_second_opinion():
    # THE structural guard (DoD). The rows must BE the repo.truth blocks the field strip binds —
    # not a re-derivation. If this composed its own tick line against its own threshold, boring mode
    # and the shell could tell two different stories about the same runner: the original bug with a
    # third hat on. Identity, not equality: nothing was recomputed.
    a = truth.banner(_live())
    t = truth.whole_field([_repo("titan", a)])
    assert t["repos"][0]["tick"] is a["tick"], "the row must carry the repo's OWN verdict object"
    assert t["repos"][0]["data"] is a["data"]


def test_a_healthy_field_states_every_repos_tick_and_stays_calm():
    t = truth.whole_field([_repo("titan", truth.banner(_live())),
                           _repo("acme", truth.banner(_live()))])
    assert t["level"] == "ok"
    assert [r["name"] for r in t["repos"]] == ["titan", "acme"]
    assert t["repos"][0]["tick"]["text"] == "last tick 4s ago"


def test_one_silent_repo_takes_the_whole_strip_down():
    # Worst-of on the level: one glance at the strip's colour is a true summary of everything under
    # it. A field where any loop may be dead is not a green field.
    t = truth.whole_field([_repo("titan", truth.banner(_live())),
                           _repo("acme", truth.banner(_silent()))])
    assert t["level"] == "down"


def test_the_worst_repo_never_erases_the_healthy_ones_names():
    # Why the words are NOT aggregated. "loop may be down" over a two-repo table, with no name on
    # it, sends the owner to check the wrong runner — and hides that the other one is fine.
    t = truth.whole_field([_repo("titan", truth.banner(_live())),
                           _repo("acme", truth.banner(_silent()))])
    rows = {r["name"]: r for r in t["repos"]}
    assert rows["titan"]["level"] == "ok"
    assert rows["titan"]["tick"]["text"] == "last tick 4s ago"
    assert rows["acme"]["level"] == "down"
    assert rows["acme"]["tick"]["text"] == "last tick 15m ago — loop may be down"


def test_drift_alone_is_a_notice_not_an_alarm():
    t = truth.whole_field([_repo("titan", truth.banner(_live(), engine=_drift(3)))])
    assert t["level"] == "notice"


def test_a_stated_drift_always_colours_the_strip_even_under_a_calm_level():
    # Pins the promotion clause DIRECTLY. Review's mutation testing showed the test above passes via
    # row-level propagation (banner already promotes the repo to notice), so it would stay green with
    # the clause gone — it was pinning the composition, not the boundary. This states drift under an
    # `ok` level, which only a hand-built block can do: the line and the ground must not contradict.
    hand_built = {"level": "ok",
                  "tick": {"state": "ok", "text": "last tick 4s ago"},
                  "data": {"state": "ok", "text": "data 12s ago"},
                  "engine": {"state": "drift", "text": "3 engine fixes merged but not yet live",
                             "behind": 3, "remedy": "bin/install.sh"}}
    t = truth.whole_field([_repo("titan", hand_built)])
    assert t["engine"]["text"] == "3 engine fixes merged but not yet live"
    assert t["level"] == "notice", "a strip that STATES drift may not paint the calm ground"


def test_the_engine_line_reaches_boring_mode_stated_once_not_once_per_repo():
    # DoD item 3. There is ONE installed engine behind every watched repo, so repeating its drift on
    # each row would be noise AND a lie of shape — it would imply the drift were per-repo.
    eng = _drift(3)
    t = truth.whole_field([_repo("titan", truth.banner(_live(), engine=eng)),
                           _repo("acme", truth.banner(_live(), engine=eng))])
    assert t["engine"]["text"] == ("3 engine fixes merged but not yet live; re-run the installer "
                                   "to switch them on")
    for row in t["repos"]:
        assert "engine" not in row, "the engine line is global — it must not ride each row"


def test_a_live_engine_says_nothing_here_either():
    assert truth.whole_field([_repo("titan", truth.banner(_live(), engine=_engine_ok()))])["engine"] is None


def test_a_stale_runner_view_never_renders_as_a_confident_boring_table():
    # THE DoD guard, said in one assertion. This is the 90s–300s window the issue is about: the
    # RUNNER DOWN banner has not fired yet, and without this strip the table below looks authoritative.
    t = truth.whole_field([_repo("titan", truth.banner(_silent()))])
    assert t["level"] == "down"
    row = t["repos"][0]
    assert "loop may be down" in row["tick"]["text"]
    assert "not the runner's view" in row["data"]["text"]


def test_a_repo_with_no_truth_block_is_called_down_never_skipped():
    # Unknown is never an all-clear — the asymmetry the whole module is built on. A repo silently
    # dropped from the strip reads exactly like a repo that is fine.
    for junk in (None, {}, "nonsense", {"level": "ok"}):
        t = truth.whole_field([_repo("titan", junk)])
        assert t["level"] == "down"
        assert len(t["repos"]) == 1, "a repo with no verdict must still get a row"
        assert "loop may be down" in t["repos"][0]["tick"]["text"]


def test_junk_repos_never_raise_into_the_two_second_poll():
    for junk in (None, {}, [], "nonsense", 7):
        t = truth.whole_field(junk)
        assert t["level"] == "down", "nothing to report is not an all-clear"
        assert t["repos"] == []


def test_the_clamps_vocabulary_is_the_modules_vocabulary():
    # Raised in review, and it is this issue's own bug class aimed at the future. The clamp ranks
    # against _LEVEL_RANK; the CSS ratchet reflects over LEVEL_*. Add a LEVEL_WARN with colours for
    # both strips but forget _LEVEL_RANK, and every check stays green while a `warn` repo renders
    # `warn` on the field and `down` in boring mode — the two views telling two different stories
    # about one runner, silently. The two lists must BE one list.
    assert set(truth._LEVEL_RANK) == {v for k, v in vars(truth).items() if k.startswith("LEVEL_")}, (
        "every LEVEL_* must be rankable, or boring mode clamps a level the field strip renders")


def test_an_unhashable_level_never_raises_into_the_poll():
    # `in` on a dict hashes, so `[]` or `{}` would TypeError out of the one function whose whole job
    # is junk defense — on the 2-second poll (raised in review).
    for junk in ([], {}, set()):
        b = truth.banner(_live())
        b["level"] = junk
        assert truth.whole_field([_repo("titan", b)])["level"] == "down"


def test_an_unknown_level_can_never_paint_the_calm_strip():
    # Raised in review, and the sharpest version of this issue's own thesis. The CSS styles one class
    # per KNOWN level and the base .btruth IS the healthy ground — so a level this module doesn't
    # recognise reaches the browser as lvl-<junk>, matches no rule, and renders CALM. Ranking it as
    # down is not enough: `max` returns the element it ranked, so the junk string would survive to
    # the class attribute. It must be REPLACED at the boundary.
    b = truth.banner(_live())
    b["level"] = "totally-bogus"
    t = truth.whole_field([_repo("titan", b)])
    assert t["repos"][0]["level"] == "down", "an unplaceable level is not an all-clear"
    assert t["level"] == "down"
    assert t["level"] in ("ok", "notice", "down"), "the strip may only emit levels the CSS colours"


def test_a_level_that_contradicts_its_own_words_is_not_believed():
    # Also from review: `{"tick": {}}` satisfies a bare isinstance check, then renders the binder's
    # fallback text ("no tick seen — loop may be down") under a level claiming all is well — the
    # level and the words contradicting each other on screen. A line with no words is no line.
    t = truth.whole_field([_repo("titan", {"level": "ok", "tick": {}, "data": {}, "engine": None})])
    assert t["level"] == "down"
    assert "loop may be down" in t["repos"][0]["tick"]["text"]


def test_a_nameless_repo_still_gets_a_named_row():
    # The slug is the fallback the rest of boring mode already uses (§7 — the literal slug stays
    # visible everywhere in boring mode). An unnamed row is an unattributable alarm.
    t = truth.whole_field([{"slug": "acme/titan", "truth": truth.banner(_silent())}])
    assert t["repos"][0]["name"] == "acme/titan"
