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
