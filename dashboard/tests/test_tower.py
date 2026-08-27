"""Task 9 — the tower log (comms feed) gloss/mapping logic.

The tower log is a *comms feed*: each append-only journal record is glossed into a plain,
flight-numbered sentence a non-engineer reads at a glance, with an optional radio-flavor prefix
BESIDE it (design record §7: "Tower-log radio prefixes ('roger,' 'going around') always carry the
real sentence beside them; boring mode strips all flavor"). All of that mapping lives here, pure
and tested (design record B.1 — the JS binds the strings, it derives none of them). The costume
discipline (§3, rule 2): the metaphor may flavor, but the real sentence a reader acts on is always
present and honest — a wandered merge earns NO celebratory radio call (§7).

Two derived facts pinned here:

* ``comms_row(rec)`` — one record → ``{radio, text, kind, num}``. ``text`` is always a real
  sentence (never empty, never only the flavor prefix); ``radio`` is flavor and may be empty.
* ``apply_divider(rows, last_seen)`` — the "since you last looked" boundary (design record §4):
  rows newer than the persisted watermark are ``fresh``; exactly the first of them carries the
  ``divider`` marker the client draws its line before.
"""
import tower
import flights


# =============================== comms_row — the real sentence is always there ===============================

def test_launch_reads_as_a_departure_with_radio_flavor():
    row = tower.comms_row({"act": "launch", "id": "i23", "num": 23})
    assert row["num"] == 23
    assert "SL-23" in row["text"]
    assert "depart" in row["text"].lower()      # a real, plain sentence
    assert row["radio"]                          # radio flavor rides BESIDE the sentence
    assert row["kind"] == "launch"


def test_clean_merge_reads_as_a_touchdown_with_the_pr():
    row = tower.comms_row({"act": "merge", "id": "i16", "num": 16, "pr": 19, "outcome": "ok"})
    assert "SL-16" in row["text"]
    assert "19" in row["text"]                   # the real PR number the vet can click through
    assert "touchdown" in row["text"].lower() or "merged" in row["text"].lower()
    assert row["kind"] == "merge"


def test_wandered_merge_earns_no_celebratory_radio_call():
    # §7 honesty law: a wandered merge is a real landing but a dishonest one to celebrate. It gets
    # the plain "see report" sentence and NO "nice landing" radio flourish.
    row = tower.comms_row({"act": "merge", "id": "i23", "num": 23, "pr": 25,
                           "wander": True, "outcome": "ok"})
    assert "report" in row["text"].lower() or "wander" in row["text"].lower()
    assert row["radio"] == ""                    # no flourish for a wandered landing


def test_failed_merge_is_not_a_landing():
    # i16's first merge failed (outcome carries the failure) — it must NOT read as a touchdown.
    row = tower.comms_row({"act": "merge", "id": "i16", "num": 16, "pr": 18,
                           "outcome": "merge failed (will retry next tick)"})
    assert "touchdown" not in row["text"].lower()
    assert "SL-16" in row["text"]


def test_park_reads_as_gave_up_your_call():
    row = tower.comms_row({"act": "park", "id": "i7", "num": 7, "memo": "answerer timed out"})
    assert "SL-7" in row["text"]
    t = row["text"].lower()
    assert "park" in t or "gave up" in t
    assert "your call" in t
    assert row["kind"] == "park"


def test_hold_reads_as_number_two_for_landing():
    row = tower.comms_row({"act": "hold", "id": "i15", "num": 15,
                           "overlap_lane": "i16", "reason": "diff overlaps"})
    assert "SL-15" in row["text"]
    assert "number 2" in row["text"].lower() or "number two" in row["text"].lower()
    assert row["kind"] == "hold"


def test_regenerate_reads_as_a_go_around():
    row = tower.comms_row({"act": "regenerate", "id": "i16", "num": 16, "conflicts": 1})
    assert "SL-16" in row["text"]
    assert "go-around" in row["text"].lower() or "rebuild" in row["text"].lower()
    assert row["radio"].lower().startswith("going around")
    assert row["kind"] == "regen"


def test_nudge_carries_its_message_as_the_sentence():
    row = tower.comms_row({"act": "nudge", "id": "i9", "num": 9, "nudge_key": "review",
                           "message": "The gate found no review evidence. Get a fresh-agent review."})
    assert "SL-9" in row["text"]
    assert "review" in row["text"].lower()       # the real nudge content, not a generic label
    assert row["kind"] == "nudge"


def test_reapprove_row_signs_the_configured_operator_name():
    # issue #58: a re-approval is the owner's own gate — its tower line renders the configured
    # operator, never a hardcoded "William". Default (no operator) reads neutrally.
    row = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5}, operator="Ada")
    assert row["text"] == "SL-5 re-approved by Ada."
    assert "William" not in row["text"]
    default = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5})
    assert "the owner" in default["text"] and "William" not in default["text"]


def test_answerer_exchange_renders_as_radio_calls():
    # The worker asking and the auto-tower answering are a back-and-forth radio exchange (design §7).
    ask = tower.comms_row({"act": "hire_answerer", "id": "i23", "num": 23,
                           "question": "What motto should the footer carry?\n\n(exact text please)"})
    ans = tower.comms_row({"act": "deliver_answer", "id": "i23", "num": 23,
                           "text": "Use this motto verbatim:\n\nSmall issues, shipped in loops."})
    assert "SL-23" in ask["text"] and "motto" in ask["text"].lower()
    assert ask["radio"]                           # the worker calls the tower
    assert "SL-23" in ans["text"]
    assert "verbatim" in ans["text"].lower()      # the real answer content, not a generic label
    assert ans["radio"]                           # the tower answers
    assert ask["kind"] == "radio" and ans["kind"] == "answer"


def test_session_blocked_and_finished_events_read_plainly():
    blocked = tower.comms_row({"act": "event", "event": {"type": "session_blocked", "id": "i23"}})
    finished = tower.comms_row({"act": "event", "event": {"type": "session_finished", "id": "i23"}})
    assert blocked["text"] and "SL-23" in blocked["text"]
    assert finished["text"] and "SL-23" in finished["text"]
    assert "block" in blocked["text"].lower()


def test_gate_reads_cleared_only_when_it_passed():
    ok = tower.comms_row({"act": "gate", "id": "i23", "num": 23, "outcome": "ok"})
    assert "cleared" in ok["text"].lower()
    # a failed/held gate must NOT read as cleared (honesty — the gate did not pass).
    bad = tower.comms_row({"act": "gate", "id": "i23", "num": 23, "outcome": "review evidence missing"})
    assert "cleared" not in bad["text"].lower()
    assert "SL-23" in bad["text"]


def test_blocked_event_with_no_flight_number_has_clean_radio():
    # A no-number session_blocked must not render a dangling ' to tower.' with a leading space.
    row = tower.comms_row({"act": "event", "event": {"type": "session_blocked"}})
    assert not row["radio"].startswith(" ")
    assert row["text"]


def test_notify_is_a_plain_memo_line():
    row = tower.comms_row({"act": "notify", "title": "superlooper: i7 parked"})
    assert "i7 parked" in row["text"]
    assert row["kind"] == "notify"


def test_unknown_act_still_gets_a_plain_sentence():
    # Costume rule 4: any journaled event renders in plain words the day it exists — a dashboard
    # that silently under-reports an autonomous system is worse than none.
    row = tower.comms_row({"act": "some_future_verb", "id": "i5", "num": 5})
    assert row["text"]                            # never empty
    assert "SL-5" in row["text"]


def test_text_is_a_sentence_not_just_the_radio_prefix():
    # "radio flavor always carries the REAL SENTENCE beside it": the sentence must stand alone
    # without the flavor — stripping radio never empties the row.
    for rec in ({"act": "launch", "num": 1}, {"act": "regenerate", "num": 2},
                {"act": "park", "num": 3}):
        row = tower.comms_row(rec)
        assert row["text"].strip()
        assert row["text"] != row["radio"]


# =============================== routine-bookkeeping tier (issue #36) ===============================
# The tower log is the CURATED comms channel (§4) — machine bookkeeping does not belong on the radio.
# `relabel` (label convergence) fires several times per launch as GitHub's read lags the write; it is
# honest but noise. Classified server-side (B.1) into a "routine" tier the tower log hides by default,
# so future noisy-but-honest event types land in the right bucket as data — no per-type UI debate.

def test_relabel_is_classified_routine_bookkeeping():
    assert tower.tier({"act": "relabel", "id": "i23", "num": 23}) == "routine"
    row = tower.comms_row({"act": "relabel", "id": "i23", "num": 23})
    assert row["tier"] == "routine"
    assert row["text"]                            # still a real sentence — nothing becomes invisible


def test_comms_acts_are_classified_comms():
    # Everything a human reads as real radio traffic stays comms — only named bookkeeping is routine.
    for act in ("launch", "merge", "park", "hold", "regenerate", "nudge",
                "hire_answerer", "deliver_answer", "gate", "notify", "approve", "reapprove",
                "update", "alert", "freeze", "unfreeze", "event"):
        assert tower.tier({"act": act, "num": 1}) == "comms", act
        assert tower.comms_row({"act": act, "num": 1})["tier"] == "comms", act


def test_unknown_and_nondict_records_default_to_comms():
    # Fail toward VISIBLE: an unclassified/unreadable record is comms, so the dashboard never silently
    # swallows a record it did not recognise (costume rule 4 / honesty §7).
    assert tower.tier({"act": "some_future_verb", "num": 5}) == "comms"
    assert tower.tier("not a dict") == "comms"
    assert tower.comms_row("not a dict")["tier"] == "comms"


def test_routine_acts_are_a_named_extensible_set():
    # The routine tier is a named set — a future noisy-but-honest act joins it as data, never a
    # per-type UI debate (#36). `relabel` is its charter member.
    assert "relabel" in tower.ROUTINE_ACTS


# =============================== apply_divider — since you last looked (§4) ===============================

def test_apply_divider_marks_rows_newer_than_the_watermark_fresh():
    rows = [{"ts": 100}, {"ts": 200}, {"ts": 300}]
    count = tower.apply_divider(rows, last_seen=150)
    assert count == 2                             # ts 200 and 300 are new since the watermark
    assert rows[0]["fresh"] is False
    assert rows[1]["fresh"] is True and rows[2]["fresh"] is True


def test_apply_divider_marks_exactly_the_first_fresh_row():
    rows = [{"ts": 100}, {"ts": 200}, {"ts": 300}]
    tower.apply_divider(rows, last_seen=150)
    assert rows[1].get("divider") is True         # the line is drawn before the first new row
    assert not rows[0].get("divider")
    assert not rows[2].get("divider")


def test_no_watermark_means_nothing_is_fresh_and_no_divider():
    # First-ever look (no persisted watermark): everything is just "the log", no divider drawn.
    rows = [{"ts": 100}, {"ts": 200}]
    count = tower.apply_divider(rows, last_seen=None)
    assert count == 0
    assert all(r["fresh"] is False for r in rows)
    assert all(not r.get("divider") for r in rows)


def test_divider_ignores_non_finite_timestamps():
    # A corrupt JSON NaN ts must never be "fresh" (that comparison is meaningless) and never crash.
    rows = [{"ts": float("nan")}, {"ts": 500}]
    count = tower.apply_divider(rows, last_seen=100)
    assert rows[0]["fresh"] is False
    assert rows[1]["fresh"] is True
    assert count == 1


def test_divider_lands_on_the_first_fresh_comms_row_not_a_routine_row():
    # Routine rows are hidden by default, so the "since you last looked" line must never anchor to
    # one (it would float with no visible row). It lands on the first fresh COMMS row (#36).
    rows = [{"ts": 100, "tier": "comms"},
            {"ts": 200, "tier": "routine"},
            {"ts": 300, "tier": "comms"}]
    count = tower.apply_divider(rows, last_seen=150)
    assert not rows[1].get("divider")             # the fresh routine row is NOT the divider anchor
    assert rows[2].get("divider") is True         # the first fresh COMMS row is
    assert count == 1                             # only real comms traffic counts as "new"


def test_routine_rows_are_never_marked_fresh():
    # "Since you last looked" is a comms-traffic signal — routine bookkeeping never lights it up (#36),
    # so a flurry of relabels since the last look never fakes "new radio traffic".
    rows = [{"ts": 200, "tier": "routine"}, {"ts": 300, "tier": "comms"}]
    tower.apply_divider(rows, last_seen=100)
    assert rows[0]["fresh"] is False
    assert rows[1]["fresh"] is True


# =============================== the owner-tap fixer (issue #144) ===============================

def test_a_deployed_fixer_reads_as_a_fixer_not_as_a_flight():
    # The engine journals every owner-tap debugger launch (`superlooper debug`) so it "appears in
    # the tower log like every other act" — issue #144's own DoD. Its id is d<N>, NOT an issue
    # number, so the generic fallback rendered it as the nonsense "the flight debug_launch." A
    # repo-wide act must read as a repo-wide sentence.
    row = tower.comms_row({"act": "debug_launch", "outcome": "launched", "id": "d9",
                           "operator": "William", "source": "command-center",
                           "note": "the departures board is lying about SL-12"})
    assert "the flight" not in row["text"], "a fixer is not a flight — it has no issue number"
    assert "debug_launch" not in row["text"], "the raw act name must never reach the radio"
    assert "d9" in row["text"]
    assert "William" in row["text"]
    assert row["num"] is None
    assert row["tier"] == "comms", "a session launched on the owner's machine is real traffic"


def test_a_fixer_that_failed_to_launch_is_never_read_as_deployed():
    # The honesty law (§7): no flourish for a dishonest state. A launch the shim did not verify
    # must not read as a session that exists.
    row = tower.comms_row({"act": "debug_launch", "outcome": "launch_failed", "id": "d9",
                           "operator": "William",
                           "error": "the launch timed out — no session was confirmed"})
    low = row["text"].lower()
    assert "fail" in low or "did not" in low or "no session" in low
    assert "deployed" not in low


def test_a_fixer_launch_names_the_operator_it_was_signed_with_not_the_configured_one():
    # The journal record carries WHO tapped. A row that fell back to the dashboard's own configured
    # operator would misattribute a launch made from a terminal by someone else.
    row = tower.comms_row({"act": "debug_launch", "outcome": "launched", "id": "d3",
                           "operator": "Pat"}, operator="William")
    assert "Pat" in row["text"] and "William" not in row["text"]


# ===================== the engine-level acts that named no flight (issue #253) =====================
# Four acts the gloss table did not know: the UNATTENDED debugger launch (#66), the loop restarting
# its own dead runner (#208), the Restart button's re-exec landing (#116), and re-approval's
# retire-and-rebuild (#177). The first three name no issue, so they fell through to the generic
# fallback and reached the owner as a sentence about a flight that does not exist; the fourth WAS
# glossed, but the gloss predated #177 and still narrated the pre-rotation world. The record shapes
# below are the engine's own (skills/superlooper/skill/lib/watchdog.py, bin/runner.py) — read, never
# invented, because this repo renders them and never writes them.

def test_an_unattended_launch_says_nobody_is_in_it():
    # #66: the loop hiring a debugger to repair ITSELF, with nobody watching. The owner-tap fixer
    # (#144) already reads as a DEPLOYED fixer someone chose to send; what separates this one is
    # that no human decided it and no human is in it. The line has to say so.
    row = tower.comms_row({"act": "watchdog", "outcome": "launched", "id": "d1",
                           "signals": ["heartbeat_stale"], "authority": "full"})
    low = row["text"].lower()
    assert "d1" in row["text"]
    assert "unattended" in low
    assert "nobody" in low, "the whole point of #66 is that no human is in this session"
    assert "heartbeat_stale" not in row["text"], "signal codes are machine tokens, not words"
    assert "tick" in low or "runner" in low, "the signal must reach the owner in plain words"
    assert row["num"] is None
    assert row["tier"] == "comms"


def test_an_unattended_launch_that_failed_is_never_read_as_a_session_on_the_field():
    # The honesty law (§7): no flourish for a dishonest state, and never a session that does not
    # exist. A failed hire is the WORSE event — the loop needed repair and could not get any.
    row = tower.comms_row({"act": "watchdog", "outcome": "launch_failed", "id": "d1",
                           "signals": ["heartbeat_stale"], "rc": "no_pane"})
    low = row["text"].lower()
    assert "did not launch" in low or "did not" in low
    assert "no_pane" in row["text"], "the rc is what tells the owner why it could not start"
    assert row["radio"] == "", "a failed hire earns no flourish"
    assert row["kind"] == "alert"
    ok = tower.comms_row({"act": "watchdog", "outcome": "launched", "id": "d1",
                          "signals": ["heartbeat_stale"]})
    assert row["text"] != ok["text"], "a launched session and a failed launch must read differently"


def test_the_quiet_watchdog_outcomes_still_read_plainly():
    # Costume rule 4: every journaled outcome renders in plain words the day it exists. The episode
    # opening, its silent stand-down, the held-off launch and the kill switch are all journaled, and
    # none of them may read as a session that launched.
    opened = tower.comms_row({"act": "watchdog", "outcome": "notified",
                              "signals": ["no_progress"], "grace_seconds": 1800,
                              "authority": "full"})
    assert "30 min" in opened["text"], "the grace is the actionable number — how long you have"
    assert "waiting" in opened["text"].lower()
    stood_down = tower.comms_row({"act": "watchdog", "outcome": "stand_down",
                                  "signals": ["heartbeat_stale"]})
    assert "cleared" in stood_down["text"].lower()
    assert "runner" in stood_down["text"].lower(), "name the signal that cleared, not the enum"
    # The engine stands the episode down on "self-recovery OR OWNER INTERVENTION", and it emits the
    # SAME record for an episode that already launched a session (after_launch keeps the episode
    # alive). So the record proves exactly one thing — the tripped signal is gone — and the line may
    # claim neither agency nor an empty field (fresh review, Codex, rounds 1 and 2).
    assert "on its own" not in stood_down["text"].lower()
    assert "itself" not in stood_down["text"].lower()
    assert "session" not in stood_down["text"].lower()
    held = tower.comms_row({"act": "watchdog", "outcome": "skipped_live_session",
                            "signals": ["alert"]})
    assert "already" in held["text"].lower()
    off = tower.comms_row({"act": "watchdog", "outcome": "disabled", "signals": ["alert"]})
    assert "off" in off["text"].lower() or "disabled" in off["text"].lower()
    launched = tower.comms_row({"act": "watchdog", "outcome": "launched", "id": "d1",
                                "signals": ["heartbeat_stale"]})
    for row in (opened, stood_down, held, off):
        assert row["text"].strip()
        assert row["text"] != launched["text"]
        # None of the quiet outcomes may claim an unattended session that never started.
        assert "nobody is in it" not in row["text"].lower()


def test_a_resurrected_runner_a_failed_restart_and_a_capped_one_are_three_sentences():
    # #208: the loop restarting its own dead runner. The engine distinguishes three outcomes, and
    # flattening a corpse into a success is the exact lie the tower log exists to prevent.
    up = tower.comms_row({"act": "runner_resurrect", "outcome": "resurrected", "id": "r1",
                          "signals": ["heartbeat_stale"]})
    dead = tower.comms_row({"act": "runner_resurrect", "outcome": "resurrect_failed", "id": "r4",
                            "signals": ["heartbeat_stale"], "rc": "no_pane"})
    capped = tower.comms_row({"act": "runner_resurrect", "outcome": "resurrect_capped",
                              "signals": ["heartbeat_stale"], "attempts": 5, "max_per_hour": 5})
    assert len({up["text"], dead["text"], capped["text"]}) == 3
    assert "r1" in up["text"] and "restart" in up["text"].lower()
    assert "r4" in dead["text"] and "no_pane" in dead["text"]
    assert "not running" in dead["text"].lower() or "could not" in dead["text"].lower()
    assert dead["kind"] == "alert" and capped["kind"] == "alert"
    assert "5" in capped["text"] and "paused" in capped["text"].lower()
    # report.py's own P1-2 lesson, owed here too: an undeliverable attempt burns a cap slot without
    # restarting anything, so the capped line claims ATTEMPTS, never restarts that happened.
    assert "attempt" in capped["text"].lower()
    for row in (dead, capped):
        assert "restarted itself" not in row["text"].lower()


def test_a_disabled_auto_restart_is_not_read_as_a_crash_loop_pause():
    # `resurrect_capped` with max_per_hour == 0 is auto-restart switched OFF in config, not a cap a
    # crash loop hit. Saying "paused after 0 attempts" would be a small lie about why the loop is down.
    off = tower.comms_row({"act": "runner_resurrect", "outcome": "resurrect_capped",
                           "signals": ["heartbeat_stale"], "attempts": 0, "max_per_hour": 0})
    low = off["text"].lower()
    assert "disabled" in low
    assert "attempt" not in low
    assert "stay" in low or "until you" in low


def test_the_restart_buttons_landing_reads_as_a_runner_that_came_back():
    # #116: the Restart button re-execs the runner; the reborn image journals the LANDING. Old pid →
    # new pid is the whole fact — "it came back, and here is the proof it is a different process."
    landed = tower.comms_row({"act": "runner_restart", "phase": "up", "old_pid": 41, "new_pid": 42})
    assert "41" in landed["text"] and "42" in landed["text"]
    assert "back up" in landed["text"].lower() or "landed" in landed["text"].lower()
    assert landed["num"] is None
    # The re-exec path carries no old pid (the process replaced itself in place) — still a landing.
    reexec_landed = tower.comms_row({"act": "runner_restart", "phase": "up", "new_pid": 42})
    assert "42" in reexec_landed["text"]
    assert reexec_landed["text"].strip()


def test_a_restart_that_did_not_land_never_reads_as_one_that_did():
    # `reexec_failed` means the exec itself failed and the OLD image is still running. The button
    # already reported success on the request, so this line is the only place the truth surfaces.
    failed = tower.comms_row({"act": "runner_restart", "phase": "reexec_failed",
                              "error": "OSError('no such interpreter')"})
    low = failed["text"].lower()
    assert "did not" in low or "failed" in low
    assert "back up" not in low
    assert "old image" in low or "still running" in low
    assert failed["kind"] == "alert"
    assert "no such interpreter" in failed["text"]


def test_the_restart_phases_before_the_landing_name_who_asked_for_it():
    # The restart marker carries the operator who tapped. As with the #144 fixer, the name comes
    # from the RECORD — a restart requested from a terminal by someone else is not the owner's.
    going = tower.comms_row({"act": "runner_restart", "phase": "reexec", "old_pid": 41,
                             "request": {"operator": "Pat", "source": "command-center"}},
                            operator="William")
    assert "Pat" in going["text"] and "William" not in going["text"]
    assert "41" in going["text"]
    exiting = tower.comms_row({"act": "runner_restart", "phase": "exit_to_supervisor",
                               "old_pid": 41, "request": {"operator": "Pat"}})
    assert "supervisor" in exiting["text"].lower()
    assert exiting["text"] != going["text"]
    # A leftover marker aimed at a runner that already died is dropped, not honored — say exactly that.
    stale = tower.comms_row({"act": "runner_restart", "phase": "stale",
                             "target_pid": 41, "our_pid": 42})
    assert "41" in stale["text"] and "42" in stale["text"]
    assert "not restarting" in stale["text"].lower() or "dropped" in stale["text"].lower()


def test_reapprove_renders_the_retirement_it_now_performs():
    # #177: re-approval RETIRES the lane's branch and rebuilds on a new generation. The old gloss
    # ("SL-5 re-approved by the owner.") described the pre-#177 world, where the branch was
    # preserved — while `regenerate` already gets the design record's honest retire-and-rebuild
    # treatment (§3, the conflict row). The engine journals both branch names on the record.
    row = tower.comms_row({"act": "reapprove", "id": "i5",
                           "old_counters": {"launches": 3},
                           "old_branch": "sl/i5-fix-the-board",
                           "new_branch": "sl/i5-fix-the-board-r2"}, operator="William")
    t = row["text"]
    assert "SL-5" in t and "William" in t
    assert "sl/i5-fix-the-board" in t and "sl/i5-fix-the-board-r2" in t
    assert "retir" in t.lower(), "the retirement is the fact the old gloss omitted"
    assert "generation 2" in t.lower(), "the generation the rebuild runs on"
    assert row["radio"] == "", "a human gate stays a fun-free zone (§7)"
    assert row["kind"] == "approve"


def test_reapprove_names_the_superseded_pr_when_the_record_carries_one():
    # A PR still open on the retired branch is labelled `superseded`; the owner needs to know which.
    row = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5, "had_rebuild": False,
                           "outcome": "reapproved (reset {'launches': 3}; rebuilding on "
                                      "sl/i5-fix-the-board-r2; superseded PR #42)"})
    assert "#42" in row["text"]
    assert "supersede" in row["text"].lower()


def test_a_reapprove_that_did_not_complete_never_reads_as_one_that_did():
    # The engine journals the ACTION and its outcome too, and that outcome is not always a
    # re-approval that happened: `_exec_reapprove` aborts when a worker is still live in the
    # worktree, and the tick loop turns an executor crash into an `executor error: …` outcome.
    # Rendering either as the calm "SL-5 re-approved by the owner." hides a lane that did not
    # actually restart (fresh review, Codex).
    deferred = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5, "had_rebuild": False,
                                "outcome": "worker still live in the worktree (pid 8123) — "
                                           "deferring the fresh start (deferral 1 of 3; retries "
                                           "next tick)"})
    assert "SL-5" in deferred["text"]
    assert "did not complete" in deferred["text"].lower()
    assert "worker still live" in deferred["text"]
    assert deferred["kind"] == "alert", "a re-approval that did not take is not a cleared gate"
    crashed = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5,
                               "outcome": "executor error: OSError()"})
    assert "did not complete" in crashed["text"].lower()
    # The successful outcome record — the engine's own "reapproved (…)" form — stays the calm gate.
    ok = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5,
                          "outcome": "reapproved (reset nothing)"}, operator="Ada")
    assert ok["text"] == "SL-5 re-approved by Ada."
    assert ok["kind"] == "approve"


def test_a_reapprove_whose_github_bookkeeping_failed_says_what_did_not_land():
    # The rebuild can succeed while its owner-facing GitHub bookkeeping does not: the `superseded`
    # label, the supersede note, the retirement comment. NOTHING retries those — the engine names
    # them in the outcome for exactly that reason — so a row that swallowed the clause would leave a
    # retired PR orphaned behind a line that read as fully handled (fresh review, Codex).
    row = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5,
                           "outcome": "reapproved (reset nothing; rebuilding on sl/i5-x-r2; "
                                      "gh bookkeeping incomplete: the `superseded` label on PR "
                                      "#42, the retirement comment on issue #5)"}, operator="Ada")
    assert "SL-5 re-approved by Ada" in row["text"]
    assert "did not all land" in row["text"].lower()
    assert "the `superseded` label on PR #42" in row["text"]
    assert "the retirement comment on issue #5" in row["text"]
    assert not row["text"].endswith(")."), "the engine's own closing paren is not content"
    assert row["kind"] == "alert", "nothing retries this bookkeeping — it needs the owner's eye"


def test_a_reapprove_that_retired_nothing_still_reads_as_the_calm_human_gate():
    # A lane never handed to the launcher has no branch to retire, and the dashboard's own Approve
    # verb carries none either. Neither may grow a retirement clause about a branch that never was.
    plain = tower.comms_row({"act": "reapprove", "id": "i5", "num": 5, "old_counters": {}},
                            operator="Ada")
    assert plain["text"] == "SL-5 re-approved by Ada."
    approve = tower.comms_row({"act": "approve", "id": "i5", "num": 5}, operator="Ada")
    assert approve["text"] == "SL-5 re-approved by Ada."


def test_the_four_engine_level_acts_never_leak_an_act_name_or_an_imaginary_flight():
    # The shape test_a_deployed_fixer_reads_as_a_fixer_not_as_a_flight established (#144), owed to
    # all four acts and every outcome/phase the engine writes: no raw act name on the radio, and no
    # sentence about "the flight" for an act that names no flight.
    records = [
        {"act": "watchdog", "outcome": "launched", "id": "d1", "signals": ["heartbeat_stale"],
         "authority": "full"},
        {"act": "watchdog", "outcome": "launch_failed", "id": "d1", "signals": ["alert"], "rc": 1},
        {"act": "watchdog", "outcome": "notified", "signals": ["no_progress"],
         "grace_seconds": 1800, "authority": "full"},
        {"act": "watchdog", "outcome": "stand_down", "signals": ["heartbeat_stale"]},
        {"act": "watchdog", "outcome": "skipped_live_session", "signals": ["alert"]},
        {"act": "watchdog", "outcome": "disabled", "signals": []},
        {"act": "watchdog", "outcome": "some_future_outcome", "signals": ["heartbeat_stale"]},
        {"act": "watchdog"},
        {"act": "runner_resurrect", "outcome": "resurrected", "id": "r1",
         "signals": ["heartbeat_stale"]},
        {"act": "runner_resurrect", "outcome": "resurrect_failed", "id": "r2",
         "signals": ["heartbeat_stale"], "rc": "no_pane"},
        {"act": "runner_resurrect", "outcome": "resurrect_capped", "signals": ["heartbeat_stale"],
         "attempts": 5, "max_per_hour": 5},
        {"act": "runner_resurrect", "outcome": "resurrect_capped", "signals": ["heartbeat_stale"],
         "attempts": 0, "max_per_hour": 0},
        {"act": "runner_resurrect", "outcome": "some_future_outcome"},
        {"act": "runner_resurrect"},
        {"act": "runner_restart", "phase": "up", "old_pid": 41, "new_pid": 42},
        {"act": "runner_restart", "phase": "up", "new_pid": 42},
        {"act": "runner_restart", "phase": "reexec", "old_pid": 41, "request": {"operator": "Pat"}},
        {"act": "runner_restart", "phase": "exit_to_supervisor", "old_pid": 41, "request": {}},
        {"act": "runner_restart", "phase": "reexec_failed", "error": "OSError()"},
        {"act": "runner_restart", "phase": "stale", "target_pid": 41, "our_pid": 42},
        {"act": "runner_restart", "phase": "some_future_phase"},
        {"act": "runner_restart"},
        {"act": "reapprove", "id": "i5", "old_counters": {"launches": 3},
         "old_branch": "sl/i5-x", "new_branch": "sl/i5-x-r2"},
        {"act": "reapprove", "id": "i5", "num": 5, "had_rebuild": True,
         "outcome": "reapproved (reset nothing; rebuilding on sl/i5-x-r2; superseded PR #42)"},
    ]
    for rec in records:
        row = tower.comms_row(rec)
        line = row["text"] + " " + row["radio"]
        assert row["text"].strip(), rec
        assert row["text"] != row["radio"], rec
        assert "the flight" not in line, rec
        assert rec["act"] not in line, rec
        assert row["tier"] == "comms", rec


def test_the_four_engine_level_acts_survive_corrupt_records():
    # The journal is the only input and it is append-only text: a wrong-typed field must degrade to
    # a plain sentence, never crash the poll that renders the whole feed (the #218 lesson).
    for rec in ({"act": "watchdog", "outcome": 7, "signals": "not-a-list", "id": None},
                {"act": "watchdog", "outcome": "notified", "grace_seconds": "soon"},
                {"act": "runner_resurrect", "outcome": "resurrect_capped",
                 "attempts": None, "max_per_hour": "five"},
                {"act": "runner_restart", "phase": 3, "request": "not-a-dict"},
                {"act": "reapprove", "id": "i5", "old_branch": 7, "new_branch": ["x"]},
                {"act": "reapprove", "id": "i5", "outcome": {"not": "a string"}}):
        row = tower.comms_row(rec)
        assert row["text"].strip(), rec
        assert "None" not in row["text"], rec


def test_a_fixer_launch_the_engine_never_named_is_not_reported_as_a_failure():
    # Issue #458. The engine journals exactly two outcomes today (`launched` / `launch_failed`), and
    # this gloss read EVERYTHING else — a record with no outcome, or a word a newer engine invents —
    # as "did not launch". That is a claim the record does not support, and once the trouble banner
    # started reading the same act it became a contradiction between two surfaces about ONE launch.
    # The classification is now `lib/fixer.launch_outcome`, shared by both, so it can only be made
    # once.
    row = tower.comms_row({"act": "debug_launch", "id": "d9", "operator": "William"})
    low = row["text"].lower()
    assert "did not launch" not in low, "an unrecorded outcome is not a failure"
    assert "deployed" not in low, "and it is certainly not a session that exists"
    assert "d9" in row["text"] and row["text"], "it must still render — silence is the worse answer"


def test_a_fixer_launch_outcome_from_a_newer_engine_is_quoted_not_translated():
    row = tower.comms_row({"act": "debug_launch", "id": "d9", "outcome": "queued"})
    assert "queued" in row["text"]
    assert "did not launch" not in row["text"].lower()
