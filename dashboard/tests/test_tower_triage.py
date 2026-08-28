"""Issue #451 — the tower narrates a triage run in honest words, act by act.

The generic fallback at the bottom of ``tower.comms_row`` renders an unglossed record as
``"%s %s." % (who, act)``. For a record that names no lane, ``who`` is the literal string *"the
flight"* — so an unglossed engine-level act reaches the owner as a sentence about an aircraft that
does not exist. That is the **#253 defect class**, and this repo has now paid for it four times:
``debug_launch`` (#144), then ``watchdog`` / ``runner_resurrect`` / ``runner_restart`` (#253).

#449 gave a whole autonomous delegation its own act vocabulary — a session that CLOSES AND MERGES
ISSUES on its own word — and every one of those acts falls into that same fallback today:

    the flight triage_close.        the flight triage_escalate.        the flight triage_refused.

This file is the guard that they never do. One test per act the engine journals, plus the one record
a flight produces that is not a triage act at all: the launcher's ``fence_preflight``, stamped with
the flight's ``t<N>`` id, which reaches the same fallback as *"the flight fence_preflight."*

Two properties beyond the words:

* **A triage row carries no flight chip.** ``row["num"]`` drives tower.js's clickable ``SL-<n>``
  chip, which opens that number's FLIGHT CARD. An issue a flight judged is not a flight — it has no
  lane, no branch and no drawer — so the number rides inside the sentence (``#452``, exactly as the
  morning report writes it) and the chip stays off. A chip onto a flight that does not exist is the
  same lie as a sentence about one.
* **``triage_keep`` is routine, not comms.** A flight may keep dozens of issues in one run, and
  ``report.py`` leaves every one of them out of the morning report on the stated ground that
  *"silence means kept … listing every one would bury the four that are"* news. ``ROUTINE_ACTS`` is
  the extension point for exactly that: classified server-side as data, hidden from the feed by
  default, revealable on demand — no per-type UI debate (owner ruling 2026-07-07).
"""
import tower
import triage


def _row(act, **fields):
    rec = {"act": act, "ts": 1783364300}
    rec.update(fields)
    return tower.comms_row(rec)


def _plain(row):
    """Everything the owner actually reads on one line — radio flavor is stripped in boring mode, so
    the real sentence has to stand alone (§7)."""
    return row["text"]


# =============================== never the fallback ===============================

def test_no_triage_act_falls_through_to_the_generic_gloss():
    # The blunt sweep: every act #449 journals, rendered, must name what happened — never the bare
    # act token, and never the phantom "the flight".
    acts = {
        "triage_launch": {"id": "t7", "outcome": "launched", "detail": "3 issues changed"},
        "triage_keep": {"num": 440, "flight": "t7", "detail": "buildable — left alone"},
        "triage_fix": {"num": 441, "flight": "t7", "fixed": ["touches"], "detail": "metadata"},
        "triage_merge": {"num": 452, "flight": "t7", "absorber": 440, "detail": "duplicate"},
        "triage_close": {"num": 453, "flight": "t7", "verdict": "overtaken", "commit": "abc1234"},
        "triage_escalate": {"num": 460, "flight": "t7", "finding": "needs an owner decision"},
        "triage_refused": {"num": 461, "flight": "t7", "detail": "the issue is approved"},
        "triage_finish": {"flight": "t7", "counts": {"judged": 4}, "detail": "judged 4 · 1 merged"},
    }
    for act, fields in acts.items():
        row = _row(act, **fields)
        text = _plain(row)
        assert act not in text, "%s reached the owner as its own act name" % act
        assert "the flight %s" % act not in text, (
            "%s rendered as a sentence about a flight that does not exist (#253)" % act)
        assert text.strip().endswith("."), "%s must be a sentence" % act
        assert "%" not in text, "%s left an unformatted template in the feed" % act


def test_every_journaled_act_is_covered_by_this_file():
    # A guard on the guard: if the engine grows a triage act, the sweep above must grow with it.
    # `tower.TRIAGE_ACTS` is the tower's own declared coverage; drift goes red here, not on a board.
    assert tower.TRIAGE_ACTS == ("triage_launch", "triage_keep", "triage_fix", "triage_merge",
                                 "triage_close", "triage_escalate", "triage_refused",
                                 "triage_finish")


# =============================== one test per act ===============================

def test_a_launched_flight_departs_in_plain_words():
    row = _row("triage_launch", id="t7", outcome="launched",
               detail="3 issues changed since the last flight")
    assert "t7" in _plain(row)
    assert "triage" in _plain(row).lower() or "queue" in _plain(row).lower()
    assert "3 issues changed" in _plain(row)
    assert row["radio"], "a departure is real traffic — it earns its radio flavor"
    assert row["kind"] == "launch"


def test_a_launch_that_failed_is_never_read_as_a_flight_on_the_field():
    # No flourish for a dishonest state (§7). The engine's own wording rides through, and the
    # consequence — a night with nothing triaged — is stated, exactly as the morning report does.
    row = _row("triage_launch", id="t7", outcome="launch failed (rc=1)", detail="no pane")
    assert "did not launch" in _plain(row).lower()
    assert "launch failed (rc=1)" in _plain(row)
    assert "nothing was triaged" in _plain(row).lower()
    assert row["radio"] == ""
    assert row["kind"] == "alert"


def test_a_kept_issue_says_it_was_looked_at_and_left_alone():
    row = _row("triage_keep", num=440, flight="t7", detail="buildable — nothing to do")
    assert "#440" in _plain(row)
    assert "kept" in _plain(row).lower() or "left" in _plain(row).lower()
    assert "buildable" in _plain(row)
    assert row["tier"] == "routine", (
        "a run keeps far more than it acts on; the feed must not be buried in them (issue #36)")


def test_a_fixed_issue_names_what_was_fixed():
    row = _row("triage_fix", num=441, flight="t7", fixed=["touches", "type label"],
               detail="metadata repaired")
    assert "#441" in _plain(row)
    assert "touches" in _plain(row) and "type label" in _plain(row)
    assert row["tier"] == "comms"


def test_a_fix_with_no_list_still_says_what_kind_of_change_it_was():
    row = _row("triage_fix", num=441, flight="t7", fixed="everything")
    assert "#441" in _plain(row)
    assert "metadata" in _plain(row).lower()


def test_a_merged_duplicate_names_both_issues_and_which_way_it_went():
    row = _row("triage_merge", num=452, flight="t7", absorber=440, detail="duplicate of #440")
    text = _plain(row)
    assert "#452" in text and "#440" in text
    assert "into #440" in text, "the direction of an absorb is the whole fact"


def test_a_merge_whose_absorber_is_unrecorded_says_so_rather_than_inventing_one():
    text = _plain(_row("triage_merge", num=452, flight="t7"))
    assert "#452" in text
    assert "#None" not in text and "#?" in text


def test_a_nit_close_names_the_rubric_line_and_the_ledger_entry():
    row = _row("triage_close", num=453, flight="t7", verdict="nit(cosmetic)", rubric="cosmetic",
               ledger=300)
    text = _plain(row)
    assert "#453" in text
    assert "nit" in text.lower()
    assert "cosmetic" in text
    assert "#300" in text, "the ledger entry is the evidence the close leans on"


def test_an_overtaken_close_cites_the_commit_it_was_closed_on():
    text = _plain(_row("triage_close", num=453, flight="t7", verdict="overtaken", commit="abc1234"))
    assert "#453" in text
    assert "overtaken" in text.lower()
    assert "abc1234" in text


def test_an_overtaken_close_with_no_commit_never_claims_evidence_it_does_not_have():
    text = _plain(_row("triage_close", num=453, flight="t7", verdict="overtaken"))
    assert "abc" not in text
    assert "unrecorded" in text.lower()


def test_an_escalation_is_the_loudest_line_and_carries_the_finding():
    row = _row("triage_escalate", num=460, flight="t7",
               finding="this hides an owner decision about the ledger", held=False)
    text = _plain(row)
    assert "#460" in text
    assert "escalat" in text.lower()
    assert "hides an owner decision" in text
    assert row["kind"] == "escalate"


def test_an_escalation_on_an_approved_issue_says_it_was_only_flagged():
    text = _plain(_row("triage_escalate", num=460, flight="t7", held=True,
                       finding="approved and in the air"))
    assert "flagged" in text.lower(), (
        "a held issue is escalated but NOT touched — the sentence must not read as an action on it")


def test_a_refusal_names_the_edge_that_stopped_the_flight():
    row = _row("triage_refused", num=461, flight="t7", verdict="close",
               detail="the issue carries `agent-ready` — the owner's word, never a flight's")
    text = _plain(row)
    assert "#461" in text
    assert "refus" in text.lower()
    assert "agent-ready" in text
    assert row["kind"] == "alert", "a refusal is the delegation's edge doing its job — show it"


def test_a_refusal_with_no_reason_recorded_says_that_rather_than_nothing():
    text = _plain(_row("triage_refused", num=461, flight="t7"))
    assert "#461" in text
    assert "unrecorded" in text.lower()


def test_the_finish_reads_as_the_runs_own_tally():
    row = _row("triage_finish", flight="t7",
               counts={"judged": 6, "merged": 1, "closed": 2, "ledger": 1, "fixed": 1,
                       "escalated": 1},
               detail="judged 6 · 1 merged · 2 closed (1 to the ledger) · 1 fixed · 1 escalated")
    text = _plain(row)
    assert "t7" in text
    assert "judged 6" in text, "the flight's OWN summary sentence, not one we recomputed"
    assert row["kind"] == "triage"


def test_a_finish_with_no_summary_still_states_the_run_closed():
    text = _plain(_row("triage_finish", flight="t7", counts={"judged": 3}))
    assert "t7" in text
    assert "closed its run" in text.lower()


# =============================== the one non-triage record a flight makes ===============================

def test_a_flights_fence_preflight_never_reads_as_a_phantom_flight():
    # The launcher journals this on EVERY launch, stamped with the session's own id — so a triage
    # launch produces one carrying `t7`. Unglossed, it reached the owner as "the flight
    # fence_preflight." (#253, verbatim). The engine's words are that a flight "holds no fence token
    # and drives no herdr surface", so it is gated exactly as a worker is; the gloss says what the
    # check DID, in the board's own host-neutral vocabulary.
    row = _row("fence_preflight", id="t7", verdict="fenced", required=True, refused=False,
               socket="/Users/somebody/.config/somewhere/some.sock")
    text = _plain(row)
    assert "t7" in text
    assert "fence_preflight" not in text
    assert "the flight fence_preflight" not in text
    assert "cleared" in text.lower() or "passed" in text.lower()
    assert "/Users/" not in text, "a socket path is a machine's business, not the owner's board"


def test_a_flight_refused_at_the_fence_says_no_flight_ran():
    row = _row("fence_preflight", id="t7", verdict="open", required=True, refused=True,
               socket="/tmp/x.sock")
    text = _plain(row)
    assert "t7" in text
    assert "refused" in text.lower()
    assert "no session" in text.lower() or "did not" in text.lower()
    assert row["kind"] == "alert"


def test_a_workers_fence_preflight_is_left_exactly_as_it_was():
    # DoD item 4, at the record level: this issue renders a TRIAGE flight. A worker's fence record is
    # not one, and glossing it here would change the board of every repo that has never flown a
    # flight — which is the thing this issue promises not to do.
    row = _row("fence_preflight", id="i23", verdict="fenced", required=True, refused=False)
    assert row["kind"] == "unknown"
    assert row["text"] == "SL-23 fence_preflight."      # byte-for-byte what main renders today
    assert row["num"] == 23


# =============================== a triage row never claims a flight card ===============================

def test_a_triage_row_carries_no_flight_chip_for_the_issue_it_names():
    # tower.js draws a clickable SL-<num> chip from row["num"] that opens that number's FLIGHT CARD.
    # An issue a flight judged has no lane and no card; the number belongs in the sentence instead.
    for act, fields in (("triage_close", {"num": 453, "verdict": "overtaken", "commit": "abc1234"}),
                        ("triage_escalate", {"num": 460, "finding": "x"}),
                        ("triage_refused", {"num": 461, "detail": "held"}),
                        ("triage_merge", {"num": 452, "absorber": 440})):
        row = _row(act, flight="t7", **fields)
        assert row["num"] is None, "%s must not offer a flight card for an issue with no lane" % act
        assert "#%d" % fields["num"] in _plain(row), "the number still has to be readable"


# =============================== nothing here may raise ===============================

def test_every_triage_act_survives_a_corrupt_record():
    # One bad journal line must never take the tower panel down — it renders inside the 2s poll.
    for act in tower.TRIAGE_ACTS + ("fence_preflight",):
        for junk in ({"act": act}, {"act": act, "num": "452", "flight": 7, "id": 7},
                     {"act": act, "detail": None, "counts": "six", "fixed": {"a": 1}},
                     {"act": act, "num": float("nan"), "outcome": [], "verdict": 7}):
            row = tower.comms_row(junk)
            assert isinstance(row["text"], str) and row["text"].strip()
            assert isinstance(row["kind"], str)


def test_the_dashboards_triage_vocabulary_and_the_towers_agree():
    # The card's counted acts are a subset of the acts the tower narrates. If one side learns a new
    # act and the other does not, the board reports a run whose lines it cannot render (or the
    # reverse) — so pin them together rather than leaving the seam to a reader.
    assert set(triage.ACT_COUNTS) <= set(tower.TRIAGE_ACTS)
    assert triage.LAUNCH_ACT in tower.TRIAGE_ACTS and triage.FINISH_ACT in tower.TRIAGE_ACTS


# =============================== the two claims a truthy read would forge ===============================
# (Fresh-agent review, medium.) Both of these fields are booleans the ENGINE writes, and both decide
# a claim about what did NOT happen — "the issue was not touched", "no session was created". A
# truthy read turns the string "false" (a hand-edited or half-written record) into that claim.

def test_a_wrong_typed_held_never_claims_the_issue_was_left_untouched():
    for junk in ("false", "no", 0.0, [], {}, "true", 1, object()):
        text = _plain(_row("triage_escalate", num=460, flight="t7", held=junk, finding="x"))
        if junk is True:                                    # unreachable; kept for the reader
            continue
        assert "untouched" not in text, (
            "held=%r is not the engine's boolean claim — the line must not say the issue was left "
            "alone" % (junk,))
        assert "escalat" in text.lower(), "it is still an escalation, whatever `held` says"
    assert "untouched" in _plain(_row("triage_escalate", num=460, flight="t7", held=True,
                                      finding="x"))


def test_a_fence_record_with_no_readable_outcome_claims_neither_way():
    # `refused` is the engine's own boolean. Absent or wrong-typed, the honest render is that the
    # record does not say — never "cleared" (a false all-clear on the one gate standing between an
    # unattended, issue-closing session and an unfenced machine) and never "refused" either.
    for junk in (None, "false", "true", 0, 1, [], {}):
        row = _row("fence_preflight", id="t7", verdict="fenced", required=True, refused=junk)
        text = _plain(row)
        assert "t7" in text
        assert "cleared" not in text.lower(), "refused=%r must not read as a pass" % (junk,)
        assert "no session was created" not in text, "refused=%r must not read as a refusal" % (junk,)
        assert "records no outcome" in text or "does not say" in text
        assert row["kind"] == "unknown"
    assert "cleared" in _plain(_row("fence_preflight", id="t7", verdict="fenced",
                                    required=True, refused=False)).lower()
    assert "no session was created" in _plain(_row("fence_preflight", id="t7", verdict="open",
                                                   required=True, refused=True))


# =============================== an act with no flight is nobody's flight ===============================
# (Fresh-agent review round 2.) `lib/triage` already refuses to attribute an unstamped record to the
# latest `t<N>`; the FEED has to refuse the same thing, or the two surfaces disagree about the same
# line. The engine's own words for the empty id: "a legitimate thing for an operator to do and is
# recorded honestly as such rather than attributed to a flight that never ran."

def test_a_hand_run_verb_is_never_narrated_as_a_flight():
    for act, fields in (("triage_close", {"num": 452, "verdict": "overtaken", "commit": "abc1234"}),
                        ("triage_merge", {"num": 452, "absorber": 440}),
                        ("triage_fix", {"num": 455, "fixed": ["touches"]}),
                        ("triage_keep", {"num": 440}),
                        ("triage_escalate", {"num": 460, "finding": "x"}),
                        ("triage_refused", {"num": 461, "detail": "held"})):
        for missing in ({}, {"flight": ""}, {"flight": "  "}, {"flight": 7}, {"flight": "i7"}):
            text = _plain(_row(act, **dict(fields, **missing)))
            assert "hand-run" in text, (
                "%s with flight=%r must not be narrated as a flight's act" % (act, missing))
            assert "triage flight" not in text
            assert "#%d" % fields["num"] in text


def test_a_hand_run_finish_says_the_run_was_closed_by_hand():
    text = _plain(_row("triage_finish", flight="", detail="judged 2 · 0 merged"))
    assert "hand-run" in text
    assert "judged 2" in text


def test_a_launch_record_is_always_a_flights_even_when_it_names_none():
    # A `triage_launch` IS a flight launch by definition — that is what the record MEANS — so an
    # unreadable id there is a nameless flight, never a hand-run verb. (`triage-flight` is the only
    # thing that writes one, and it stamps the id it just allocated.)
    text = _plain(_row("triage_launch", outcome="launched", detail="3 issues changed"))
    assert "triage flight" in text.lower()
    assert "hand-run" not in text


# =============================== each record is read the way the ENGINE writes it ===============================
# (Fresh-agent review round 3.) The two keys are NOT interchangeable, and reading them as if they
# were is a superset of the engine's contract rather than a match for it:
#
#   `id`      is what a LAUNCH (`triage-flight`) and the launcher's `fence_preflight` stamp.
#   `flight`  is what every per-issue act and `triage_finish` stamp (`_triage_record`, from
#             `SL_ISSUE_ID` — assigned by the launcher, never self-asserted).
#
# So a hand-run act carrying an empty `flight` beside some other `id` must stay a hand-run act.

def test_a_hand_run_act_is_not_rescued_by_an_id_the_engine_never_writes_there():
    for act in ("triage_close", "triage_merge", "triage_fix", "triage_keep", "triage_escalate",
                "triage_refused", "triage_finish"):
        text = _plain(_row(act, num=452, flight="", id="t7", verdict="overtaken",
                           commit="abc1234", absorber=440, detail="x"))
        assert "hand-run" in text, "%s must be attributed by `flight` alone" % act
        assert "t7" not in text


def test_a_launch_is_not_named_by_a_flight_key_the_engine_never_writes_there():
    text = _plain(_row("triage_launch", flight="t7", outcome="launched", detail="x"))
    assert "A triage flight departed" in text, "a launch is named by `id`, which this record lacks"


def test_a_fence_record_with_no_id_is_not_a_flights():
    # The launcher writes `{act, id, verdict, required, socket, refused}` — no `flight` key at all,
    # so a `flight` here is not the engine's claim and the record falls through exactly as it did
    # before this issue.
    row = _row("fence_preflight", flight="t7", verdict="fenced", refused=False)
    assert row["kind"] == "unknown"
    assert "fence check" not in _plain(row)


# =============================== one corrupt line may not blank the board ===============================

def test_a_wrong_typed_act_never_raises_out_of_the_gloss():
    # `{"act": []}` is VALID JSON, so `readers._iter_records` yields it intact — and `act in
    # ROUTINE_ACTS` then raised TypeError (a list is unhashable) inside the 2-second snapshot poll,
    # taking the whole board down for every repo. Found by the round-3 review; pre-existing, and in
    # the classifier this issue extends, so it is fixed here rather than left as a landmine.
    for act in ([], {}, {"a": 1}):
        assert tower.tier({"act": act}) == "comms"
        row = tower.comms_row({"act": act})
        assert isinstance(row["text"], str) and row["text"].strip()
        assert row["tier"] == "comms"
