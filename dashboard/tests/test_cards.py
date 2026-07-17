"""Task 9 — the Needs You cards + the flight-card drawer (gloss/mapping logic).

Both surfaces speak the design record's two-altitude language (§1) under costume rule 2 (§3):
**the plain-language gloss leads; the literal term is secondary/on-hover for the vet.** This module
holds that translation, pure and tested (design record B.1) — a decision card's headline and gloss,
the conflict-cap card that names the collision in one plain sentence with Discuss as the default
(§8 open risk: "consider making Discuss the highlighted default there"), and the drawer's circuit
rail + clearance checklist glosses + memo history + go-around counter.

Everything here is a pure function of an already-built flight object (``flights.build_flight``) and
its journal slice — no clock except an injected ``now``/``hhmm`` — so the JS binds strings it never
derives.
"""
import cards
import flights


def _flight(**over):
    """A minimal flight object shaped like ``flights.build_flight`` output, overridable per test."""
    f = {"num": 7, "label": "SL-7", "attempt": 1, "stage": flights.PARKED,
         "circuit_stage": flights.DOWNWIND, "awaiting_reason": None, "wander": False,
         "gate": {"report": False, "review": False, "ci": False, "mergeable": False, "cleared": False},
         "cargo": {"present": False, "added": 0, "removed": 0, "files": 0},
         "branch": "sl/i7-x", "pr": None, "memo": "answerer a1 timed out after 15 min"}
    f.update(over)
    return f


# =============================== card kind — the four decisions ===============================

def test_parked_flight_is_a_parked_card():
    assert cards.card_kind(_flight(stage=flights.PARKED)) == "parked"


def test_needs_william_flight_is_a_needs_william_card():
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="needs-owner")) == "needs-owner"


def test_bounced_flight_is_a_bounced_card():
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="bounced")) == "bounced"


def test_a_decision_that_went_around_is_a_conflict_cap_card():
    # A flight that was rebuilt after a merge conflict (attempt >= 2) and still landed on William's
    # desk is the conflict-cap case — the go-around cap was hit (design record §3).
    assert cards.card_kind(_flight(stage=flights.PARKED, attempt=2)) == "conflict-cap"


def test_a_durable_question_is_a_question_card():
    # #163: a durable owner-decision question is its own kind and takes precedence over the go-around
    # count — it is ANSWERED, not approved.
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="question")) == "question"
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="question",
                                   attempt=3)) == "question"


def test_question_card_offers_a_typed_answer_action():
    # The primary verb on a question card is Answer — a mechanical comment+label that takes the
    # operator's typed text (input: "answer"), plus Discuss and Drop. Never a bare Approve.
    card = cards.needs_you_card(_flight(stage=flights.AWAITING, awaiting_reason="question",
                                        memo="QUESTION: A or B?"), "will-titan/sandbox")
    assert card["kind"] == "question" and card["badge_base"] == "QUESTION"
    assert card["memo"] == "QUESTION: A or B?"           # the whole question rides on the card
    acts = card["actions"]
    answer = [a for a in acts if a["act"] == "answer"]
    assert answer and answer[0]["input"] == "answer" and answer[0]["tone"] == "primary"
    verbs = {a["act"] for a in acts}
    assert "answer" in verbs and "discuss" in verbs and "drop" in verbs
    assert "approve" not in verbs and "bounce-yes" not in verbs   # a question is answered, not approved


def test_question_dossier_omits_the_mid_build_gate_row():
    # A question flight paused MID-build, so every gate check is naturally not-yet — showing "gate at
    # hand-back: not yet: ..." would imply the question is about a failed gate. It must be omitted;
    # the question text is the whole evidence.
    dossier = cards.decision_dossier(
        _flight(stage=flights.AWAITING, awaiting_reason="question", memo="QUESTION: A or B?"), [])
    assert not any("gate at hand-back" in (it["label"] or "") for it in dossier["items"])


# =============================== the card — plain gloss leads, literal term secondary ===============================

def test_parked_card_leads_with_a_plain_headline_and_a_hover_term():
    card = cards.needs_you_card(_flight(stage=flights.PARKED), "will-titan/sandbox")
    assert card["kind"] == "parked"
    assert card["headline"] and card["headline"][0].isupper()      # a plain sentence, not a label
    assert card["gloss"]["plain"]                                  # the gloss the card leads with
    assert card["gloss"]["term"] == "parked"                       # the literal term, for hover
    assert card["memo"] == "answerer a1 timed out after 15 min"    # the raw memo rides along
    assert card["badge_base"] == "PARKED"
    assert card["repo"] == "will-titan/sandbox"
    assert card["discuss_default"] is False


def test_bounced_card_explains_the_bounce_in_plain_words():
    card = cards.needs_you_card(_flight(stage=flights.AWAITING, awaiting_reason="bounced",
                                        memo="BOUNCED: the premise is gone. Proposed amendment: ..."),
                                "will-titan/sandbox")
    assert card["kind"] == "bounced"
    assert card["badge_base"] == "BOUNCED"
    assert "amend" in (card["gloss"]["plain"] + card["headline"]).lower()  # what a bounce means
    assert card["gloss"]["term"] == "bounced"


def test_conflict_cap_card_names_the_collision_and_defaults_to_discuss():
    # The one plain sentence naming the collision, with Discuss highlighted as the default (§8).
    f = _flight(stage=flights.PARKED, num=16, label="SL-16·A2", attempt=2)
    card = cards.needs_you_card(f, "will-titan/sandbox")
    assert card["kind"] == "conflict-cap"
    assert card["collision"]                                       # a plain sentence, not a bare badge
    assert "SL-16" in card["collision"]
    assert "collid" in card["collision"].lower() or "conflict" in card["collision"].lower()
    assert card["discuss_default"] is True                         # Discuss is the highlighted default
    assert card["badge_base"].startswith("CONFLICT")


def test_non_conflict_cards_have_no_collision_sentence():
    card = cards.needs_you_card(_flight(stage=flights.PARKED), "r")
    assert card["collision"] is None


# =============================== the drawer — ground truth one click away ===============================

def test_drawer_carries_the_title_links_and_go_around_counter():
    f = _flight(num=16, label="SL-16·A2", attempt=2, pr=19, branch="sl/i16-r1",
                stage=flights.TOUCHDOWN, circuit_stage=flights.TOUCHDOWN)
    d = cards.flight_drawer(f, [], "will-titan/sandbox", "Sandbox Air", title="Make it formal")
    assert d["num"] == 16
    assert d["title"] == "Make it formal"
    assert d["links"]["issue"].endswith("/will-titan/sandbox/issues/16")
    assert d["links"]["pr"].endswith("/will-titan/sandbox/pull/19")
    assert d["links"]["branch"] == "sl/i16-r1"
    assert d["go_arounds"] == 1                                    # attempt 2 → one go-around survived


def test_drawer_circuit_rail_marks_the_current_position():
    f = _flight(stage=flights.DOWNWIND, circuit_stage=flights.DOWNWIND)
    d = cards.flight_drawer(f, [], "r", "Air")
    rail = d["circuit"]
    assert [step["stage"] for step in rail] == list(flights.CIRCUIT_STAGES)
    cur = [step for step in rail if step["current"]]
    assert len(cur) == 1 and cur[0]["stage"] == flights.DOWNWIND
    # stages before the current one read as done; each step carries a label + literal term.
    at_stand = next(s for s in rail if s["stage"] == flights.AT_STAND)
    assert at_stand["done"] is True and at_stand["label"] and at_stand["term"] == "at-stand"


def test_drawer_circuit_rail_leads_with_developer_terms_flavor_secondary():
    # Costume rule 2 / joy-pass owner ruling (2026-07-07): the ground-truth drawer's rail LEADS
    # with the real developer state name; the airport metaphor is the secondary flavor, never the
    # primary word you read for truth. ``label`` is the developer term the JS renders first;
    # ``flavor`` is the airport skin it renders small and secondary.
    d = cards.flight_drawer(_flight(circuit_stage=flights.DOWNWIND), [], "r", "Air")
    by_stage = {s["stage"]: s for s in d["circuit"]}
    dev = {
        flights.AT_STAND:  ("queued",         "at the stand"),
        flights.TAXI_OUT:  ("launching",      "taxiing out"),
        flights.TAKEOFF:   ("session started", "takeoff"),
        flights.DOWNWIND:  ("building",       "downwind"),
        flights.BASE_TURN: ("report filed",   "base turn"),
        flights.FINAL:     ("gate checks",    "final"),
        flights.TOUCHDOWN: ("merged",         "touchdown"),
        flights.TAXI_IN:   ("closed",         "taxi in"),
    }
    for stage, (want_label, want_flavor) in dev.items():
        step = by_stage[stage]
        assert step["label"] == want_label, "%s should lead with the developer term" % stage
        assert step["flavor"] == want_flavor, "%s keeps the airport term as secondary flavor" % stage


def test_drawer_gate_step_names_the_real_checks():
    # DoD: the gate step (final) names the real mechanical checks — report / review / CI / mergeable.
    d = cards.flight_drawer(_flight(circuit_stage=flights.FINAL), [], "r", "Air")
    gate = next(s for s in d["circuit"] if s["stage"] == flights.FINAL)
    desc = gate["desc"].lower()
    for check in ("report", "review", "ci", "mergeable"):
        assert check in desc, "the gate step's detail should name the real check %r" % check


def test_drawer_off_path_state_is_named_in_plain_words():
    f = _flight(stage=flights.PARKED, circuit_stage=flights.DOWNWIND)
    d = cards.flight_drawer(f, [], "r", "Air")
    # The plane renders at its honest circuit position, but the drawer names the off-path state too.
    assert d["off_path"]["state"] == flights.PARKED
    assert "gave up" in d["off_path"]["plain"].lower() or "park" in d["off_path"]["plain"].lower()
    cur = next(s for s in d["circuit"] if s["current"])
    assert cur["stage"] == flights.DOWNWIND                        # honest position, not teleported


def test_drawer_stranded_gate_names_the_gate_and_holds_its_final_position(tmp_path):
    # A stranded gate's drawer (issue #22) must tell the owner where the problem is — the GATE, not a
    # dead session — and keep the plane AT its honest final position (the gate) on the circuit rail.
    f = _flight(stage=flights.STRANDED, circuit_stage=flights.FINAL)
    d = cards.flight_drawer(f, [], "r", "Air")
    assert d["off_path"]["state"] == flights.STRANDED
    plain = d["off_path"]["plain"].lower()
    assert "gate" in plain and "frozen" not in plain              # points at the gate, not a dead session
    cur = next(s for s in d["circuit"] if s["current"])
    assert cur["stage"] == flights.FINAL                          # held at the gate, never teleported


def test_drawer_clearance_checklist_has_real_names_and_plain_glosses():
    f = _flight(stage=flights.FINAL, circuit_stage=flights.FINAL,
                gate={"report": True, "review": True, "ci": False, "mergeable": True, "cleared": False})
    d = cards.flight_drawer(f, [], "r", "Air")
    by_key = {c["key"]: c for c in d["clearance"]}
    assert set(by_key) == {"report", "review", "ci", "mergeable"}   # the four REAL check names (§3)
    assert by_key["report"]["ok"] is True and by_key["ci"]["ok"] is False
    # mergeable leads with the plain gloss the vet's literal term hangs off of (costume rule 2).
    assert "cleanly" in by_key["mergeable"]["gloss"].lower()


# --------------------------- issue #176: a stale review is not a bare cross ---------------------------

def _review_row(flight):
    d = cards.flight_drawer(flight, [], "r", "Air")
    return next(c for c in d["clearance"] if c["key"] == "review")


def test_clearance_review_stale_reads_reviewed_then_rebuilt_not_a_bare_cross():
    """Issue #176: a verdict pinned to a superseded diff (stale) must NOT look identical to 'never
    reviewed'. The review row carries state='stale', stays not-ok, and leads with a DISTINCT label so
    the owner reads 'reviewed, then rebuilt' rather than the same line an unreviewed flight shows."""
    f = _flight(stage=flights.FINAL, circuit_stage=flights.FINAL,
                gate={"report": True, "review": False, "ci": True, "mergeable": True,
                      "review_state": "stale", "cleared": False})
    row = _review_row(f)
    assert row["ok"] is False and row["state"] == "stale"
    assert "rebuilt" in row["label"].lower()             # 'reviewed, then rebuilt'
    assert row["label"] != "independently reviewed"      # not the same line as a fresh review
    assert "earlier" in row["gloss"].lower() or "fresh" in row["gloss"].lower()


def test_clearance_review_reviewed_and_absent_carry_their_state():
    reviewed = _review_row(_flight(gate={"report": True, "review": True, "ci": True,
                                         "mergeable": True, "review_state": "reviewed",
                                         "cleared": True}))
    assert reviewed["ok"] is True and reviewed["state"] == "reviewed"
    assert reviewed["label"] == "independently reviewed"
    absent = _review_row(_flight(gate={"report": True, "review": False, "ci": True,
                                       "mergeable": True, "review_state": "absent",
                                       "cleared": False}))
    assert absent["ok"] is False and absent["state"] == "absent"
    assert absent["label"] == "independently reviewed"   # 'never reviewed' keeps the plain name


def test_clearance_review_state_defaults_from_bool_when_absent():
    """Back-compat: a gate dict with no review_state (older callers/fixtures) derives the row state
    from the review bool — True->reviewed, False->absent — so the drawer never crashes on it."""
    on = _review_row(_flight(gate={"report": True, "review": True, "ci": True, "mergeable": True,
                                   "cleared": True}))
    assert on["ok"] is True and on["state"] == "reviewed"
    off = _review_row(_flight(gate={"report": True, "review": False, "ci": True, "mergeable": True,
                                    "cleared": False}))
    assert off["ok"] is False and off["state"] == "absent"


def test_non_review_rows_carry_a_plain_ok_state():
    """Every clearance row carries a `state` so the pixel layer maps glyphs uniformly; the binary
    checks are just ok/no."""
    d = cards.flight_drawer(_flight(gate={"report": True, "review": True, "ci": False,
                                          "mergeable": True, "review_state": "reviewed"}), [], "r", "Air")
    by_key = {c["key"]: c for c in d["clearance"]}
    assert by_key["report"]["state"] == "ok" and by_key["ci"]["state"] == "no"


def test_dossier_names_a_stale_review_distinctly_from_never_reviewed():
    """The parked dossier's 'gate at hand-back: not yet …' list must name a stale review as
    'reviewed, then rebuilt', not the 'independently reviewed' phrase a never-reviewed flight shows —
    the two demand different owner responses (re-review the new diff vs get a first review)."""
    stale = cards.decision_dossier(
        _flight(gate={"report": True, "review": False, "ci": True, "mergeable": True,
                      "review_state": "stale", "cleared": False}), [])
    val = {i["label"]: i["value"] for i in stale["items"]}["gate at hand-back"]
    assert "rebuilt" in val.lower() and "independently reviewed" not in val
    never = cards.decision_dossier(
        _flight(gate={"report": True, "review": False, "ci": True, "mergeable": True,
                      "review_state": "absent", "cleared": False}), [])
    val2 = {i["label"]: i["value"] for i in never["items"]}["gate at hand-back"]
    assert "independently reviewed" in val2 and "rebuilt" not in val2.lower()


def test_drawer_cargo_chip_is_size_never_risk():
    f = _flight(cargo={"present": True, "added": 340, "removed": 12, "files": 3})
    d = cards.flight_drawer(f, [], "r", "Air")
    assert d["cargo"]["added"] == 340 and d["cargo"]["removed"] == 12
    assert "340" in d["cargo"]["chip"] and "12" in d["cargo"]["chip"]
    assert d["cargo"]["files"] == 3


def test_drawer_absent_cargo_reads_empty_not_zero_risk():
    d = cards.flight_drawer(_flight(cargo={"present": False}), [], "r", "Air")
    assert d["cargo"]["present"] is False


def test_drawer_collects_memo_history_from_the_journal_slice():
    jslice = [
        {"ts": 100, "act": "launch", "id": "i7", "num": 7},
        {"ts": 200, "act": "park", "id": "i7", "num": 7, "memo": "first park reason"},
        {"ts": 300, "act": "regenerate", "id": "i7", "num": 7},
        {"ts": 400, "act": "park", "id": "i7", "num": 7, "memo": "second park reason"},
    ]
    d = cards.flight_drawer(_flight(memo="second park reason"), jslice, "r", "Air")
    assert d["memos"] == ["first park reason", "second park reason"]   # history, in order


def test_drawer_decision_metadata_is_server_computed():
    # The drawer's action verbs are the SERVER's, not the JS's (design record B.1) — a bounced
    # flight must fire bounce-yes (its distinct audit trail), never a plain approve.
    bounced = cards.flight_drawer(_flight(stage=flights.AWAITING, awaiting_reason="bounced"),
                                  [], "r", "Air")
    assert bounced["decision"]["approve_act"] == "bounce-yes"
    assert bounced["decision"]["discuss_default"] is False

    parked = cards.flight_drawer(_flight(stage=flights.PARKED), [], "r", "Air")
    assert parked["decision"]["approve_act"] == "approve"
    # The label names the effect. Issue #162 made that name HONEST: a re-approval rebuilds from
    # scratch (the engine's `_exec_reapprove` prunes the worktree and deletes the report), so the
    # word is "rebuild" — "relaunch" undersold what the button throws away.
    assert "rebuild" in parked["decision"]["approve_label"].lower()


def test_drawer_conflict_cap_defaults_to_discuss_in_the_drawer_too():
    # The §8 guard against a blind Approve must hold in the drawer, not only on the card.
    d = cards.flight_drawer(_flight(stage=flights.PARKED, attempt=2), [], "r", "Air")
    assert d["decision"]["kind"] == "conflict-cap"
    assert d["decision"]["discuss_default"] is True


def test_drawer_for_a_non_decision_flight_has_no_decision_actions():
    d = cards.flight_drawer(_flight(stage=flights.DOWNWIND, circuit_stage=flights.DOWNWIND), [], "r", "Air")
    assert d["decision"] is None


def test_drawer_journal_slice_is_glossed_and_expandable_to_raw():
    jslice = [{"ts": 100, "act": "launch", "id": "i7", "num": 7}]
    d = cards.flight_drawer(_flight(), jslice, "r", "Air", hhmm=lambda ts: "09:41")
    entry = d["journal"][0]
    assert "depart" in entry["text"].lower()      # glossed via the tower vocabulary
    assert entry["hhmm"] == "09:41"               # server-formatted time (injected)
    assert '"act": "launch"' in entry["raw"] or '"act":"launch"' in entry["raw"]  # raw ground truth


# =============================== issue #162 — the full-text decision card ===============================
# Every owner hand-back is a decision card carrying the WHOLE question, the dossier of evidence
# behind it, a link to the issue, and verbs whose labels state their effect. William must be able to
# read the entire question he is being asked to answer and judge it without opening a terminal.

_LONG_QUESTION = "\n".join(
    ["The runner cannot decide which base the rebuild should target, and the answer changes what "
     "merges. Please pick one:"] +
    ["  option %d — rebuild onto dev at the commit the PR branched from, keeping line %d intact"
     % (i, i) for i in range(1, 41)] +
    ["Recommendation: option 7, because it is the only one that preserves the gate's evidence."])


def test_needs_you_card_carries_the_whole_question_never_truncated():
    # THE issue #162 promise: the card shows the complete question — no ellipsis, no first-N-lines.
    card = cards.needs_you_card(_flight(stage=flights.AWAITING, memo=_LONG_QUESTION), "r/s")
    assert card["memo"] == _LONG_QUESTION                    # byte-for-byte, whole
    assert len(card["memo"].splitlines()) == 42              # every line survives
    assert "Recommendation: option 7" in card["memo"]        # the LAST line, not just the first few
    assert "…" not in card["memo"] and "..." not in card["memo"]


def test_needs_you_card_links_to_the_issue():
    # The owner reads the card and needs the issue itself one click away (never a terminal).
    card = cards.needs_you_card(_flight(num=7), "will-titan/sandbox")
    assert card["issue_url"] == "https://github.com/will-titan/sandbox/issues/7"


# --------------------------------- the dossier (evidence behind the decision) ---------------------------------

def test_dossier_surfaces_the_structured_evidence_the_runner_captured():
    # Forward-compatible with issue #152: when the park record carries structured evidence, the card
    # shows it — the owner judges from what the machine actually saw.
    jslice = [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "delivery not verified",
               "evidence": {"reason": "Workspace not found", "rc": 1,
                            "stderr_tail": "launch-session.sh: workspace 'will-titan' is gone"}}]
    d = cards.decision_dossier(_flight(memo="delivery not verified"), jslice)
    assert d["captured"] is True
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert pairs["reason"] == "Workspace not found"
    assert pairs["rc"] == "1"
    assert "workspace 'will-titan' is gone" in pairs["stderr_tail"]


def test_dossier_renders_evidence_captured_as_a_bare_string_as_is():
    # #152 fail-closes to a plain "captured: none, reason unknown" string; render it as-is, never
    # crash. Whether that counts as CAPTURED is pinned separately, in
    # test_evidence_captured_means_STRUCTURED_evidence_only — it does not.
    jslice = [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "m",
               "evidence": "captured: none, reason unknown"}]
    d = cards.decision_dossier(_flight(memo="m"), jslice)
    assert d["items"][0]["value"] == "captured: none, reason unknown"


def test_dossier_is_honest_when_the_runner_captured_no_evidence():
    # #152 has not landed for every path. The card must SAY so rather than imply the reason is
    # everything the machine saw — an honest empty, never a fabricated dossier.
    d = cards.decision_dossier(_flight(memo="m"), [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "m"}])
    assert d["captured"] is False
    assert d["note"]                                  # a plain sentence naming the absence
    assert "no structured evidence" in d["note"].lower()


def test_dossier_names_the_recorded_cause_when_it_differs_from_the_memo():
    # The park record's `cause` is the runner's own episode key — real evidence when it says more
    # than the memo. Identical text is not repeated back at the owner.
    jslice = [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "the gate stopped this",
               "cause": "retry cap reached after 3 launches"}]
    d = cards.decision_dossier(_flight(memo="the gate stopped this"), jslice)
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert pairs["recorded cause"] == "retry cap reached after 3 launches"

    same = cards.decision_dossier(_flight(memo="same text"),
                                  [{"ts": 1, "act": "park", "id": "i7", "num": 7,
                                    "memo": "same text", "cause": "same text"}])
    assert "recorded cause" not in {i["label"] for i in same["items"]}


def test_dossier_names_the_gate_checks_the_machine_saw():
    # What the gate actually read at the hand-back — the four real check names (§3), never a guess.
    f = _flight(gate={"report": True, "review": True, "ci": False, "mergeable": False})
    d = cards.decision_dossier(f, [])
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert "checks green" in pairs["gate at hand-back"]        # the failing checks, by real name
    assert "fits cleanly" in pairs["gate at hand-back"]
    assert "report filed" not in pairs["gate at hand-back"]    # the green ones are not "evidence"


def test_dossier_counts_the_go_arounds_on_a_conflict_cap():
    d = cards.decision_dossier(_flight(attempt=3), [])
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert "2" in pairs["rebuilt after conflicts"]


def test_needs_you_card_carries_its_dossier():
    jslice = [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "m",
               "evidence": {"reason": "Workspace not found"}}]
    card = cards.needs_you_card(_flight(memo="m"), "r/s", journal_slice=jslice)
    assert card["dossier"]["captured"] is True
    assert card["dossier"]["items"][0]["value"] == "Workspace not found"


def test_a_card_built_without_a_journal_slice_still_has_an_honest_dossier():
    # The server always passes the slice; a caller that does not must get an honest empty, never a crash.
    card = cards.needs_you_card(_flight(), "r/s")
    assert card["dossier"]["captured"] is False
    assert card["dossier"]["note"]


# --------------------------------- consequence-named verbs ---------------------------------

def test_every_action_names_its_consequence():
    # No button whose name hides what it does. Each carries a label that states its effect and a
    # plain consequence sentence beneath it.
    for f in (_flight(stage=flights.PARKED),
              _flight(stage=flights.AWAITING, awaiting_reason="bounced"),
              _flight(stage=flights.AWAITING),
              _flight(stage=flights.PARKED, attempt=2),
              _flight(stage=flights.PARKED, pr=555,        # a FINISHED lane (resume + rebuild, #161)
                      gate={"report": True, "review": True, "ci": False,
                            "mergeable": False, "cleared": False})):
        acts = cards.decision_actions(f)
        assert acts, "a waiting flight always offers the owner a way out"
        for a in acts:
            # the mechanical verbs only — approve/bounce-yes/drop/discuss plus the #161 rebuild split
            assert a["act"] in ("approve", "bounce-yes", "drop", "discuss", "rebuild")
            assert a["consequence"] and a["consequence"][-1] == "."           # a plain sentence
            assert len(a["label"]) > len(a["act"])                            # never a bare verb


def test_the_destructive_action_names_the_close_in_its_own_label():
    # "Drop" hid a close-for-good behind a friendly word. The label itself must say what it does —
    # the owner must not have to arm the button to discover the consequence.
    drop = [a for a in cards.decision_actions(_flight()) if a["act"] == "drop"][0]
    assert drop["destructive"] is True
    assert "close" in drop["label"].lower()          # the effect is IN the name
    assert "for good" in drop["label"].lower()
    assert "close" in drop["armed_label"].lower()    # and the armed second tap repeats it
    # Issue #44's two-tap gesture, preserved now that the armed string is the SERVER's: the armed
    # button still names the gesture, and still names the number it would close.
    assert "tap again" in drop["armed_label"].lower()
    assert "#7" in drop["armed_label"]


def test_the_safe_actions_are_not_marked_destructive():
    for a in cards.decision_actions(_flight()):
        if a["act"] != "drop":
            assert a["destructive"] is False
    discuss = [a for a in cards.decision_actions(_flight()) if a["act"] == "discuss"][0]
    assert "nothing" in discuss["consequence"].lower()   # Discuss changes no state; say so


def test_every_yes_verb_says_it_rebuilds_from_scratch():
    # The honest consequence of a re-approval TODAY (engine `_exec_reapprove`): a fresh `agent-ready`
    # on any park-family status (`REAPPROVAL_STATUSES` = parked/needs_william/bounced) prunes the
    # worktree, DELETES the filed report, zeroes the counters and relaunches. "Re-approve" hid that
    # entirely — the owner could not tell the button threw his finished work away. Issue #161 splits
    # the verb (resume-at-gate vs rebuild); until it lands, the label names what really happens.
    for f in (_flight(stage=flights.PARKED), _flight(stage=flights.AWAITING),
              _flight(stage=flights.AWAITING, awaiting_reason="bounced")):
        yes = [a for a in cards.decision_actions(f) if a["act"] in ("approve", "bounce-yes")][0]
        assert "rebuild" in yes["label"].lower(), "the yes verb must not hide the rebuild"
        low = yes["consequence"].lower()
        assert "worktree" in low and "report" in low, "say what is discarded, in the sentence"


# ------------- D11: a finished lane resumes at the gate; rebuild is a separate action (issue #161) -------------

def _finished_flight(**over):
    # a FINISHED build handed back for a decision: report filed (⇒ a PR was opened), so re-approval
    # resumes at the gate rather than rebuilding.
    f = _flight(stage=flights.PARKED, pr=555,
                gate={"report": True, "review": True, "ci": False, "mergeable": False, "cleared": False})
    f.update(over)
    return f


def test_a_finished_lane_reapproves_to_resume_at_the_gate_not_rebuild():
    # THE D11 button fix: on a finished lane the primary yes verb RESUMES AT THE GATE — its label and
    # consequence say the PR/report/review are KEPT and the gate re-runs, never that work is discarded.
    yes = [a for a in cards.decision_actions(_finished_flight()) if a["act"] == "approve"][0]
    assert yes["destructive"] is False
    low = yes["label"].lower() + " " + yes["consequence"].lower()
    assert "resume" in low and "gate" in low
    assert "kept" in low or "keep" in low                       # says the work is preserved
    assert "discard" not in yes["consequence"].lower()          # the yes verb never threatens the work


def test_a_finished_lane_offers_rebuild_as_a_separate_destructive_action():
    # Rebuild-from-scratch exists as a distinct, armed, destructive verb whose LABEL names the
    # consequence (discards the PR + review). Only it throws the finished work away.
    acts = cards.decision_actions(_finished_flight())
    rb = [a for a in acts if a["act"] == "rebuild"]
    assert len(rb) == 1, "a finished lane must offer an explicit rebuild"
    rb = rb[0]
    assert rb["destructive"] is True
    assert "rebuild" in rb["label"].lower()
    assert "discard" in rb["label"].lower() and ("pr" in rb["label"].lower() or "review" in rb["label"].lower())
    assert "tap again" in rb["armed_label"].lower()             # the two-tap arm, like Drop
    low = rb["consequence"].lower()
    assert "worktree" in low and "report" in low               # the discard is named in the sentence


def test_the_rebuild_armed_caption_names_the_unique_target_with_the_slug():
    # Like Drop, the armed caption names repo AND number (Needs You is whole-field, #44) — omitted
    # without a slug rather than naming an ambiguous target.
    rb = [a for a in cards.decision_actions(_finished_flight(), slug="w/cc") if a["act"] == "rebuild"][0]
    assert "w/cc" in rb["armed_caption"] and "#7" in rb["armed_caption"]
    rb_noslug = [a for a in cards.decision_actions(_finished_flight()) if a["act"] == "rebuild"][0]
    assert rb_noslug["armed_caption"] is None


def test_an_unfinished_lane_has_no_rebuild_button_and_still_rebuilds_on_approve():
    # Nothing to discard: a lane parked before it ever finished keeps the single honest "re-approve &
    # rebuild" yes verb (no separate rebuild button — approve already rebuilds it).
    acts = cards.decision_actions(_flight(stage=flights.PARKED))   # report:False by default
    assert [a for a in acts if a["act"] == "rebuild"] == []
    yes = [a for a in acts if a["act"] == "approve"][0]
    assert "rebuild" in yes["label"].lower()


def test_a_finished_needs_you_card_carries_the_rebuild_action():
    card = cards.needs_you_card(_finished_flight(stage=flights.AWAITING, awaiting_reason="needs-owner"), "r/s")
    assert "rebuild" in [a["act"] for a in card["actions"]]
    assert "approve" in [a["act"] for a in card["actions"]]     # both: resume (approve) AND rebuild


def test_a_bounce_accepts_the_amendment_and_says_so():
    acts = {a["act"]: a for a in cards.decision_actions(_flight(stage=flights.AWAITING,
                                                               awaiting_reason="bounced"))}
    assert "bounce-yes" in acts
    assert "approve" not in acts                       # a bounce's yes IS bounce-yes, not approve
    assert "amendment" in acts["bounce-yes"]["label"].lower()


def test_conflict_cap_actions_lead_with_discuss():
    # The §8 guard against a blind Approve on a collision: Discuss is the primary tone there.
    acts = cards.decision_actions(_flight(stage=flights.PARKED, attempt=2))
    assert acts[0]["act"] == "discuss" and acts[0]["tone"] == "primary"
    assert [a for a in acts if a["act"] == "approve"][0]["tone"] != "primary"


def test_needs_you_card_carries_its_actions():
    card = cards.needs_you_card(_flight(stage=flights.PARKED), "r/s")
    assert [a["act"] for a in card["actions"]] == ["approve", "drop", "discuss"]


def test_drawer_decision_carries_the_same_consequence_named_actions():
    # One source for the verbs (design record B.1) — the drawer cannot drift from the card.
    f = _flight(stage=flights.PARKED)
    d = cards.flight_drawer(f, [], "r", "Air")
    assert d["decision"]["actions"] == cards.decision_actions(f)
    assert "rebuild" in d["decision"]["approve_label"].lower()    # the shared label names its effect


def test_drawer_carries_the_dossier_too():
    jslice = [{"ts": 300, "act": "park", "id": "i7", "num": 7, "memo": "m",
               "evidence": {"reason": "Workspace not found"}}]
    d = cards.flight_drawer(_flight(memo="m"), jslice, "r", "Air")
    assert d["dossier"]["captured"] is True


# =============================== Codex cross-review fixes (issue #162) ===============================

def test_the_dossier_follows_the_bounce_not_a_stale_park():
    # Codex P0: `_exec_bounce` clears the blocked marker and settles `bounced`, so a bounced card's
    # evidence must come from the BOUNCE record. Selecting "the last park" showed an older park's
    # evidence beside the bounce's memo — two different hand-backs, presented as one decision.
    jslice = [
        {"ts": 100, "act": "park", "id": "i7", "num": 7, "memo": "old launch failed",
         "cause": "launch_delivery", "evidence": {"reason": "cmux dead"}},
        {"ts": 200, "act": "reapprove", "id": "i7"},
        {"ts": 300, "act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: premise gone",
         "cause": "bounce", "evidence": {"reason": "the issue's premise already shipped"}},
    ]
    d = cards.decision_dossier(_flight(memo="BOUNCED: premise gone"), jslice)
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert pairs["reason"] == "the issue's premise already shipped"
    assert "cmux dead" not in pairs.values()          # the stale park's evidence must not appear


def test_the_dossier_does_not_echo_an_episode_key_that_only_names_the_act():
    # `cause` is the runner's notify-dedup key. On a bounce it is the literal "bounce" — which the
    # BOUNCED badge already says. A machine key that adds real information still shows.
    d = cards.decision_dossier(_flight(memo="m"), [
        {"ts": 1, "act": "bounce", "id": "i7", "num": 7, "memo": "m", "cause": "bounce"}])
    assert "recorded cause" not in {i["label"] for i in d["items"]}
    d2 = cards.decision_dossier(_flight(memo="m"), [
        {"ts": 1, "act": "park", "id": "i7", "num": 7, "memo": "m", "cause": "answerer_escalated"}])
    assert {i["label"]: i["value"] for i in d2["items"]}["recorded cause"] == "answerer_escalated"


def test_the_rebuild_consequence_does_not_claim_work_that_may_not_exist():
    # Codex P0: `needs_william` can be raised BEFORE any launch (e.g. a touches_missing park), where
    # there is no worktree and no filed report. The old sentence asserted both existed and would be
    # discarded. The engine removes them IF PRESENT — so the sentence must be conditional, and must
    # still warn that finished work is not resumed.
    for f in (_flight(stage=flights.PARKED), _flight(stage=flights.AWAITING),
              _flight(stage=flights.AWAITING, awaiting_reason="bounced")):
        yes = [a for a in cards.decision_actions(f) if a["act"] in ("approve", "bounce-yes")][0]
        low = yes["consequence"].lower()
        assert "any" in low, "the discard must be conditional — it may not exist yet"
        assert not ("discards this issue's worktree and filed report" in low), "no unconditional claim"
        assert "rebuild" in yes["label"].lower()


def test_the_dossier_is_fail_closed_on_a_malformed_flight():
    # Codex P1: this is a pure lib over an arbitrary dict; a malformed `attempt` must not crash the
    # whole Needs You panel (one bad flight would blank the owner's entire inbox).
    for bad in (None, "2", 2.5, True, float("nan")):
        d = cards.decision_dossier(_flight(attempt=bad), [])
        assert isinstance(d["items"], list)
        cards.decision_actions(_flight(attempt=bad))
        cards.card_kind(_flight(attempt=bad))
    # a real int still counts honestly
    assert {i["label"]: i["value"] for i in cards.decision_dossier(_flight(attempt=3), [])["items"]
            }["rebuilt after conflicts"] == "2 times"


def test_evidence_captured_means_STRUCTURED_evidence_only():
    # Codex P1: #152 fail-closes to a bare "captured: none, reason unknown" string. Treating that as
    # `captured=True` suppressed the honest-empty note and reported an ABSENCE of evidence as
    # evidence. Only a structured (dict) capture counts; a bare string is still shown, but the card
    # keeps saying no structured evidence was recorded.
    d = cards.decision_dossier(_flight(memo="m"), [
        {"ts": 1, "act": "park", "id": "i7", "num": 7, "memo": "m",
         "evidence": "captured: none, reason unknown"}])
    assert d["captured"] is False
    assert d["note"], "the honest-empty note must survive a fail-closed string"
    assert d["items"][0]["value"] == "captured: none, reason unknown"   # still shown, not hidden

    rich = cards.decision_dossier(_flight(memo="m"), [
        {"ts": 1, "act": "park", "id": "i7", "num": 7, "memo": "m", "evidence": {"reason": "x"}}])
    assert rich["captured"] is True and rich["note"] is None


def test_evidence_of_an_unexpected_shape_is_ignored_not_rendered_as_a_repr():
    for junk in ([1, 2], 7, True, {}, ""):
        d = cards.decision_dossier(_flight(memo="m"), [
            {"ts": 1, "act": "park", "id": "i7", "num": 7, "memo": "m", "evidence": junk}])
        assert d["captured"] is False
        assert all("[1, 2]" not in i["value"] for i in d["items"])


def test_the_armed_drop_caption_is_the_servers_words():
    # Codex P1 / design record B.1: the caption is a SEMANTIC (it names a destructive consequence),
    # so it belongs server-side beside the label it warns about — not hard-coded in the JS where the
    # two can drift. It names the UNIQUE target: repo AND number (issue #44 — Needs You is
    # whole-field, so two repos can each carry a #7).
    drop = [a for a in cards.decision_actions(_flight(), slug="will-titan/sandbox")
            if a["act"] == "drop"][0]
    cap = drop["armed_caption"]
    assert "will-titan/sandbox" in cap and "#7" in cap
    for phrase in ("for good", "never-mind", "not release"):
        assert phrase in cap.lower()


def test_the_armed_caption_is_absent_without_a_slug_never_a_half_named_target():
    # No slug ⇒ no caption at all, rather than one naming an ambiguous target (issue #44's whole
    # point). The card always has its slug; a caller that does not must not get a misleading warning.
    drop = [a for a in cards.decision_actions(_flight()) if a["act"] == "drop"][0]
    assert drop.get("armed_caption") is None


def test_the_card_threads_its_slug_into_the_armed_caption():
    card = cards.needs_you_card(_flight(), "will-titan/sandbox")
    drop = [a for a in card["actions"] if a["act"] == "drop"][0]
    assert "will-titan/sandbox" in drop["armed_caption"]


def test_a_live_bounce_marker_never_borrows_an_older_parks_evidence():
    # Codex cross-review ROUND 2, P0. Between the worker writing `state/blocked/<id>` and the
    # runner's next tick, the marker holds the amendment but NO bounce record exists yet — so
    # `_flight_memo` shows the MARKER's text while the dossier still found the last park and
    # attached its evidence. The card then paired one hand-back's question with another's evidence.
    # The invariant: the dossier describes the hand-back whose words are on the card, or nothing.
    jslice = [{"ts": 100, "act": "park", "id": "i7", "num": 7, "memo": "an older park",
               "cause": "launch_delivery", "evidence": {"reason": "STALE — a different decision"}}]
    live = _flight(stage=flights.AWAITING, awaiting_reason="bounced",
                   memo="BOUNCED: a brand-new amendment the runner has not journalled yet")
    d = cards.decision_dossier(live, jslice)
    assert d["captured"] is False
    assert d["note"], "with no record for these words, say so — never borrow another decision's"
    assert all("STALE" not in i["value"] for i in d["items"])
    assert "recorded cause" not in {i["label"] for i in d["items"]}


def test_the_dossier_matches_the_memo_exactly_as_flight_memo_picks_it():
    # Codex ROUND 2, P1: `_flight_memo` only accepts hand-backs that HAVE a memo. A later malformed
    # memo-less bounce made the card fall back to the older park's memo while the dossier used the
    # later bounce's evidence — the two describing different decisions again.
    jslice = [
        {"ts": 100, "act": "park", "id": "i7", "num": 7, "memo": "the park being shown",
         "evidence": {"reason": "the right evidence"}},
        {"ts": 200, "act": "bounce", "id": "i7", "num": 7, "evidence": {"reason": "no memo here"}},
    ]
    d = cards.decision_dossier(_flight(memo="the park being shown"), jslice)
    pairs = {i["label"]: i["value"] for i in d["items"]}
    assert pairs["reason"] == "the right evidence"


def test_memo_history_carries_bounces_too():
    # Codex ROUND 2, P2: bounces are first-class hand-backs now, so the drawer's memo HISTORY must
    # show them — a `bounce -> park` history that lists only the park hides that the worker ever
    # pushed back.
    jslice = [
        {"ts": 100, "act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: an earlier push-back"},
        {"ts": 200, "act": "park", "id": "i7", "num": 7, "memo": "a later park reason"},
    ]
    d = cards.flight_drawer(_flight(memo="a later park reason"), jslice, "r", "Air")
    assert d["memos"] == ["BOUNCED: an earlier push-back", "a later park reason"]
