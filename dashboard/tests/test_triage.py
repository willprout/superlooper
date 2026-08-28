"""Issue #451 — the triage flight as the board sees it: one pure read of the journal.

The `t<N>` triage flight (#448) is the third session class the launcher spawns, beside the `i<N>`
worker and the `d<N>` debugger. #463 settled that it is structurally absent from everything that
tracks a lane — no loopstate record, no branch, no PR, and nothing in the runner's published view —
and said in as many words that **#451 owns how a flight renders**. So the board's only honest source
for a flight is the one #449 built for exactly this: the journal. ``report.py``'s own comment is the
contract — *"the journal is what the report and the dashboard read, so they can never drift from
each other"* — and the assembler already reads it every tick, so the card costs no new read.

What this module has to get right:

* **The run's tally is the FLIGHT's, not ours, whenever the flight wrote one.** ``triage-finish``
  journals ``counts`` and the summary sentence it put in the run log; the card binds both. Deriving
  our own beside a flight that already stated its own is how two surfaces come to disagree about one
  night's work.
* **A flight that died halfway still says what it did.** No ``triage_finish`` ⇒ count the acts, the
  same fallback ``report._triage_summary`` takes and for the same reason.
* **Silence on a day with no flight.** Absent is the honest render (``report._triage`` returns [] and
  the section vanishes), and it is what makes "the board is exactly as it was" true for every repo
  that has not opted in — which, since the flight ships disabled, is all of them.
"""
import pytest

import triage


NOW = 1783364300.0
IDLE, FREEZE = 480, 2700


def _rec(act, ts_offset=-600, **fields):
    rec = {"act": act, "ts": NOW + ts_offset}
    rec.update(fields)
    return rec


def _launch(ts_offset=-3600, outcome="launched", flight="t7", **over):
    return _rec("triage_launch", ts_offset, id=flight, num=None, outcome=outcome,
                detail="3 issues changed since the last flight", **over)


def _run(records, mtime=NOW - 60, **over):
    """``mtime`` is the flight's OWN activity stamp; the module looks it up out of the scan the
    assembler already made, so the tests hand it the same shape (``{id: mtime}``)."""
    kw = {"now": NOW, "activity": {} if mtime is None else {"t7": mtime},
          "idle_seconds": IDLE, "freeze_seconds": FREEZE}
    kw.update(over)
    return triage.run(records, **kw)


# =============================== the flight id — the dashboard's own spelling ===============================

@pytest.mark.parametrize("value", ["t7", "t0", "t451", "t12345"])
def test_a_t_number_is_a_flight_id(value):
    assert triage.is_flight_id(value) is True
    assert triage.flight_num(value) == int(value[1:])


@pytest.mark.parametrize("value", ["i7", "d7", "t", "t7a", "7", "", None, 7, "T7", "t 7", "t-7"])
def test_nothing_else_is(value):
    # The shape is the ENGINE's (`triage.FLIGHT_ID_RE`), mirrored rather than imported — the
    # dashboard imports no engine module. A lane id, a debugger seat, a bare number and a
    # wrong-typed value are all simply not flights.
    assert triage.is_flight_id(value) is False
    assert triage.flight_num(value) is None


# =============================== silence on a day with no flight ===============================

def test_an_empty_journal_produces_no_run_and_no_plane():
    block = _run([])
    assert block["present"] is False
    assert block["on_field"] is False


def test_a_journal_of_ordinary_loop_acts_produces_no_run():
    # The overwhelmingly common case — every repo that has not opted in (the flight ships disabled).
    block = _run([_rec("launch", id="i23", num=23), _rec("merge", id="i23", num=23, pr=25),
                  _rec("debug_launch", id="d4", outcome="launched")])
    assert block["present"] is False


def test_acts_with_no_launch_behind_them_are_not_a_run():
    # A hand-run `superlooper triage-act` from the owner's own shell journals a real act with an
    # EMPTY flight id (the CLI reads it from the session's environment). That act is real and the
    # tower log shows it — but there was no flight, so there is no run and no card claiming one.
    block = _run([_rec("triage_close", num=452, flight="", verdict="overtaken", commit="abc1234")])
    assert block["present"] is False


def test_a_flight_older_than_the_window_has_left_the_board():
    old = _launch(ts_offset=-(triage.WINDOW_SECONDS + 60))
    assert _run([old])["present"] is False


# =============================== a flight in the air ===============================

def test_a_launched_flight_that_has_not_finished_is_on_the_field():
    block = _run([_launch()])
    assert block["present"] is True
    assert block["id"] == "t7"
    assert block["num"] == 7
    assert block["label"] == "t7"
    assert block["state"] == triage.FLYING
    assert block["on_field"] is True


def test_a_flying_flight_trails_the_contrail_of_its_own_liveness():
    # The same §5 ladder every other plane reads, over the flight's own `state/activity/t<N>` stamp
    # — so a survey that has gone quiet looks quiet, exactly like a worker that has.
    assert _run([_launch()], mtime=NOW - 30)["contrail"] == "crisp"
    assert _run([_launch()], mtime=NOW - (IDLE + 10))["contrail"] == "sputter"
    assert _run([_launch()], mtime=NOW - (FREEZE + 10))["contrail"] == "none"
    # No activity stamp at all is NOT "frozen" — it is no signal, and a plane is never painted
    # frozen for the lack of one (flights.contrail_kind's own rule).
    assert _run([_launch()], mtime=None)["contrail"] == "none"


def test_a_flight_still_flying_counts_what_it_has_done_so_far():
    block = _run([_launch(),
                  _rec("triage_keep", -3000, num=440, flight="t7"),
                  _rec("triage_close", -2400, num=452, flight="t7", verdict="overtaken",
                       commit="abc1234"),
                  _rec("triage_escalate", -1800, num=460, flight="t7", held=False,
                       finding="this needs an owner decision")])
    assert block["counts"] == {"judged": 3, "merged": 0, "closed": 1, "ledger": 0,
                               "fixed": 0, "escalated": 1}
    assert block["counts_source"] == triage.DERIVED


# =============================== a run the flight closed itself ===============================

FINISH_COUNTS = {"judged": 6, "merged": 1, "closed": 2, "ledger": 1, "fixed": 1, "escalated": 1}
FINISH_SUMMARY = "judged 6 · 1 merged · 2 closed (1 to the ledger) · 1 fixed · 1 escalated"


def test_a_finished_run_binds_the_flights_OWN_counts_and_OWN_summary():
    # The tally the flight wrote into the run log and the morning report is the tally the card
    # shows — verbatim. Recomputing it here is how the card and the report come to disagree about
    # one night, and the flight's is the one with authority (it counted its own journal at finish).
    block = _run([_launch(), _rec("triage_finish", -120, flight="t7", num=None,
                                  counts=dict(FINISH_COUNTS), detail=FINISH_SUMMARY)])
    assert block["state"] == triage.FINISHED
    assert block["counts"] == FINISH_COUNTS
    assert block["counts_source"] == triage.FLIGHT
    assert block["tally"] == FINISH_SUMMARY


def test_a_finished_flight_is_off_the_field_but_its_card_stays():
    block = _run([_launch(), _rec("triage_finish", -120, flight="t7", counts=dict(FINISH_COUNTS),
                                  detail=FINISH_SUMMARY)])
    assert block["on_field"] is False, "the survey is over — the plane does not orbit forever"
    assert block["present"] is True, "the day's counts are the news; they outlive the plane"


def test_a_finish_with_no_usable_counts_falls_back_to_the_acts():
    # A record from a newer engine, or a half-written one: `counts` is not a dict we can read. The
    # acts are still on record, so the run is still counted — never blanked, never invented.
    block = _run([_launch(),
                  _rec("triage_merge", -2000, num=452, flight="t7", absorber=440),
                  _rec("triage_finish", -120, flight="t7", counts="six", detail="")])
    assert block["state"] == triage.FINISHED
    assert block["counts"]["merged"] == 1
    assert block["counts_source"] == triage.DERIVED
    assert "1 merged" in block["tally"]


def test_a_finish_belonging_to_an_EARLIER_flight_never_closes_todays_run():
    # Yesterday's flight finished; today's took off after it. Reading the last finish in the file
    # without regard to when the run began would land today's flight on the ground with yesterday's
    # numbers on its card.
    block = _run([_rec("triage_finish", -30000, flight="t6", counts=dict(FINISH_COUNTS),
                       detail=FINISH_SUMMARY),
                  _launch(ts_offset=-3600, flight="t7"),
                  _rec("triage_close", -2400, num=452, flight="t7", verdict="overtaken",
                       commit="abc1234")])
    assert block["id"] == "t7"
    assert block["state"] == triage.FLYING
    assert block["on_field"] is True
    assert block["counts"]["closed"] == 1, "only t7's own acts count toward t7's run"


# =============================== the launch that never got off the ground ===============================

def test_a_failed_launch_puts_no_plane_on_the_field_and_says_why():
    reason = "launch failed (rc=1)"
    block = _run([_launch(outcome=reason)])
    assert block["present"] is True
    assert block["state"] == triage.NO_FLIGHT
    assert block["on_field"] is False
    assert reason in block["text"]
    assert "Nothing was triaged" in block["text"]


def test_a_launch_outcome_this_build_cannot_read_is_never_flown_as_a_success():
    # Fail closed, the posture `fixer.last_launch` takes on the same question: the ONE word that
    # means a session exists is the engine's own "launched". Anything else — absent, blank, or a
    # word a newer engine invented — is not that claim, and a plane is a claim.
    for outcome in (None, "", "spawned", 7):
        block = _run([_launch(outcome=outcome)])
        assert block["state"] == triage.NO_FLIGHT, "outcome %r must not fly a plane" % (outcome,)
        assert block["on_field"] is False


def test_a_launch_whose_id_is_not_a_flight_id_is_named_honestly_and_flies_nothing():
    # A plane needs a stable identity to be drawn and tapped by; a record that does not name one
    # cannot supply it. The run is still reported — it happened — with an honest stand-in.
    block = _run([_launch(flight="???")])
    assert block["present"] is True
    assert block["id"] is None
    assert block["num"] is None
    assert block["label"] == triage.UNNAMED_LABEL
    assert block["on_field"] is False


# =============================== the sentences the card binds ===============================

def test_every_state_carries_a_real_sentence_and_a_short_headline():
    blocks = [
        _run([_launch()]),
        _run([_launch(), _rec("triage_finish", -120, flight="t7", counts=dict(FINISH_COUNTS),
                              detail=FINISH_SUMMARY)]),
        _run([_launch(outcome="launch failed (rc=1)")]),
    ]
    for block in blocks:
        assert block["text"].strip(), "a card with no sentence is the silence #458 killed"
        assert block["headline"].strip()
        assert block["tally"].strip()
        assert "%" not in block["text"], "an unformatted template reached the owner"


def test_the_flying_sentence_names_the_flight_and_what_it_is_doing():
    text = _run([_launch()])["text"]
    assert "t7" in text
    assert "triage" in text.lower() or "queue" in text.lower()


def test_the_escalation_count_rides_the_block_for_salience():
    # The one number that is a call on the owner's attention: an escalation is a finding the flight
    # was NOT authorised to act on. The card leans on it; the derivation is here, not in the pixels.
    block = _run([_launch(), _rec("triage_finish", -120, flight="t7", counts=dict(FINISH_COUNTS),
                                  detail=FINISH_SUMMARY)])
    assert block["escalated"] == 1


# =============================== nothing here may raise ===============================

def test_a_corrupt_journal_never_takes_the_block_down():
    # Every reader in this repo is fail-closed per record (the #139 defect class), and this one runs
    # inside the 2-second snapshot poll: one bad line must not blank the whole board.
    junk = ["not a dict", None, 7, {"act": None}, {"act": "triage_launch"},
            {"act": "triage_launch", "ts": float("nan"), "id": "t7", "outcome": "launched"},
            {"act": "triage_finish", "ts": "soon", "flight": "t7"},
            {"act": "triage_close", "ts": NOW - 10, "num": "452", "flight": "t7"}]
    block = _run(junk + [_launch()])
    assert block["present"] is True
    assert block["id"] == "t7"
    for key in triage.COUNT_KEYS:
        assert isinstance(block["counts"][key], int)


def test_a_records_view_that_is_not_a_list_is_simply_no_run():
    for bad in (None, "records", 7, {"act": "triage_launch"}):
        assert _run(bad)["present"] is False


def test_a_non_finite_timestamp_can_never_windowed_in_or_out_a_run():
    # json.loads parses the bare literals NaN/Infinity, and `math.isfinite(10**309)` RAISES rather
    # than answering — the shape `tower._finite` and `flights._stop_epoch` are both hardened against.
    for ts in (float("nan"), float("inf"), -float("inf"), 10 ** 400, True, "600"):
        block = _run([_launch(), {"act": "triage_close", "ts": ts, "num": 9, "flight": "t7"}])
        assert block["present"] is True
        assert block["counts"]["closed"] == 0, "a record with no usable clock joins no run"


def test_a_closed_run_that_wrote_no_tally_says_the_numbers_are_ours():
    # The one place the derived/flight distinction is NEWS: the run ended, and its own last move —
    # the one that writes the tally — did not land. On a run still in the air "derived" is just what
    # "so far" means, and captioning every one of those would teach the owner to ignore the caption.
    closed = _run([_launch(), _rec("triage_finish", -120, flight="t7", counts=None, detail="")])
    assert closed["counts_note"], "a finished run with no tally of its own must say so"
    flying = _run([_launch()])
    assert flying["counts_note"] == ""
    proper = _run([_launch(), _rec("triage_finish", -120, flight="t7",
                                   counts=dict(FINISH_COUNTS), detail=FINISH_SUMMARY)])
    assert proper["counts_note"] == ""


def test_the_absent_block_carries_every_key_the_present_one_does():
    # The card binds this block on every render; a key that appears only when a flight flew is how a
    # surface starts throwing at 3am on the one night it matters.
    assert set(_run([])) == set(_run([_launch()]))
