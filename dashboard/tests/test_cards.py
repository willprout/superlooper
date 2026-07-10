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
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="needs-william")) == "needs-william"


def test_bounced_flight_is_a_bounced_card():
    assert cards.card_kind(_flight(stage=flights.AWAITING, awaiting_reason="bounced")) == "bounced"


def test_a_decision_that_went_around_is_a_conflict_cap_card():
    # A flight that was rebuilt after a merge conflict (attempt >= 2) and still landed on William's
    # desk is the conflict-cap case — the go-around cap was hit (design record §3).
    assert cards.card_kind(_flight(stage=flights.PARKED, attempt=2)) == "conflict-cap"


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
    assert "relaunch" in parked["decision"]["approve_label"].lower()


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
