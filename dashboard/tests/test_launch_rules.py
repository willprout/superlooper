"""The mirror's own tests: does ``lib/launch_rules`` say what the RUNNER says?

Two layers, deliberately:

  * the unit tests — each mirrored rule pinned in the dashboard's own terms (a bad ``type:``
    label refuses, a doubled ``model:`` refuses, ⚡ beats the band, a requeue jumps its band,
    oldest-first breaks a tie). These run everywhere, including a standalone install.
  * ``test_parity_*`` — the real bridge (issue #138): whenever the engine's source is on disk,
    read the runner's OWN ``eligible()`` / ``sort_key()`` and fail the moment the two disagree.
    This is what a transcription cannot give you — a copy tested against a copy proves nothing.

No test here reaches ``gh``, ``cmux``, ``osascript`` or the network: every rule is a pure
function of a label list, a body, and loopstate.
"""
import importlib.util
import os

import pytest

import launch_rules


# --------------------------------------------------------------------------- helpers

def _lbl(*names):
    """gh's label shape: a list of {"name": ...} dicts."""
    return [{"name": n} for n in names]


# --------------------------------------------------------------------------- priority vocabulary

def test_only_the_two_labels_the_runner_knows_move_a_band():
    # The runner reads EXACTLY `priority:high` and `priority:low` (issues.parse_issue); everything
    # else — including the absent label — is the middle band.
    assert launch_rules.priority_rank(_lbl("priority:high")) == 1
    assert launch_rules.priority_rank(_lbl("priority:low")) == 3
    assert launch_rules.priority_rank([]) == 2
    assert launch_rules.priority_rank(_lbl("priority:normal")) == 2


def test_a_bare_numeric_priority_label_is_not_a_band():
    # Drift the board used to carry (issue #138): `priority:0` ranked AHEAD of priority:high on the
    # board while the runner read it as the plain middle band. The runner never learned numbers.
    assert launch_rules.priority_rank(_lbl("priority:0")) == 2
    assert launch_rules.priority_rank(_lbl("priority:5")) == 2
    assert launch_rules.priority_rank(_lbl("priority:0")) > launch_rules.priority_rank(_lbl("priority:high"))


def test_the_medium_alias_is_not_a_band_the_runner_has():
    # The board used to display a `medium` band; the runner has no such band — it reads normal.
    assert launch_rules.priority_rank(_lbl("priority:medium")) == 2
    assert launch_rules.band_name(launch_rules.priority_rank(_lbl("priority:medium"))) == "normal"


def test_priority_matching_is_exact_like_the_runners():
    # The runner does an EXACT `"priority:high" in labels` test, so a differently-cased label is
    # simply not a band to it. The board must not read urgency the runner will never act on.
    assert launch_rules.priority_rank(_lbl("Priority:High")) == 2
    assert launch_rules.priority_rank(_lbl("priority:HIGH")) == 2


def test_high_wins_when_both_band_labels_are_present():
    # The runner's if-chain checks high FIRST, so high wins; order in the label list is irrelevant.
    assert launch_rules.priority_rank(_lbl("priority:low", "priority:high")) == 1
    assert launch_rules.priority_rank(_lbl("priority:high", "priority:low")) == 1


# --------------------------------------------------------------------------- refusals: type:

def test_a_missing_type_label_is_refused_and_names_itself():
    r = launch_rules.refusal(_lbl("agent-ready"))
    assert r is not None
    assert r["code"] == "type_missing"
    assert "type:" in r["text"]              # names the bad label in plain words
    assert "type:build" in r["text"]         # ...and how to fix it, where it is read


def test_an_unknown_type_value_is_refused_and_names_the_label():
    r = launch_rules.refusal(_lbl("type:frobnicate"))
    assert r["code"] == "type_unknown"
    assert "type:frobnicate" in r["text"]


def test_two_type_labels_are_refused():
    # issues.parse_issue takes the type only when there is EXACTLY one; two is "invalid".
    r = launch_rules.refusal(_lbl("type:build", "type:investigate"))
    assert r["code"] == "type_duplicate"
    assert "two" in r["text"].lower()


def test_each_valid_type_launches():
    for kind in launch_rules.TYPE_KINDS:
        assert launch_rules.refusal(_lbl("type:" + kind)) is None


# --------------------------------------------------------------------------- refusals: model:/effort:

def test_two_model_labels_are_refused():
    r = launch_rules.refusal(_lbl("type:build", "model:opus", "model:sonnet"))
    assert r["code"] == "model_duplicate"
    assert "model:" in r["text"]


def test_two_effort_labels_are_refused():
    r = launch_rules.refusal(_lbl("type:build", "effort:high", "effort:low"))
    assert r["code"] == "effort_duplicate"
    assert "effort:" in r["text"]


def test_a_blank_control_label_is_refused_not_silently_defaulted():
    # `_single_control_label` fails CLOSED on a bare `model:` — the runner refuses rather than
    # quietly using the default. The board must say the same.
    assert launch_rules.refusal(_lbl("type:build", "model:"))["code"] == "model_blank"
    assert launch_rules.refusal(_lbl("type:build", "effort:  "))["code"] == "effort_blank"


def test_one_model_and_one_effort_label_launch_fine():
    assert launch_rules.refusal(_lbl("type:build", "model:sonnet", "effort:high")) is None


def test_the_type_refusal_is_reported_before_a_control_conflict():
    # eligible() checks type BEFORE label_conflict, so a doubly-broken issue reports the rule the
    # runner hits first — the board never names a second-order reason.
    r = launch_rules.refusal(_lbl("model:a", "model:b"))
    assert r["code"] == "type_missing"


def test_refusal_survives_junk_labels():
    # A half-read/wrong-typed label set must never raise into a poll; it reads as unlabelled.
    assert launch_rules.refusal([None, 3, {"nope": 1}])["code"] == "type_missing"
    assert launch_rules.refusal(None)["code"] == "type_missing"


# --------------------------------------------------------------------------- sort key

def _key(num=1, expedite=False, rank=2, requeue=False, created="2026-01-01T00:00:00Z"):
    return launch_rules.sort_key(num=num, expedite=expedite, rank=rank,
                                 requeue_front=requeue, created_at=created)


def test_expedite_outranks_every_band():
    assert _key(expedite=True, rank=3) < _key(expedite=False, rank=1)


def test_band_outranks_a_requeue():
    assert _key(rank=1, requeue=False) < _key(rank=2, requeue=True)


def test_a_requeued_issue_goes_to_the_front_of_its_own_band():
    # The conflict-rebuilt issue jumps its band — but never leaves it.
    assert _key(rank=2, requeue=True) < _key(rank=2, requeue=False)


def test_creation_time_breaks_a_tie_not_the_issue_number():
    # The drift the board used to carry: it tied by issue NUMBER. The runner ties by createdAt, so
    # a low-numbered issue created LATER launches second.
    older_but_higher_numbered = _key(num=99, created="2026-01-01T00:00:00Z")
    newer_but_lower_numbered = _key(num=2, created="2026-06-01T00:00:00Z")
    assert older_but_higher_numbered < newer_but_lower_numbered


def test_issue_number_is_the_last_resort_tiebreak():
    # Same instant (or no createdAt at all): the runner's candidate list is pre-sorted by issue
    # number and its sort is stable, so number IS its final tiebreak. Mirror it, so the board can
    # never flap between two same-instant flights.
    assert _key(num=3, created="") < _key(num=9, created="")


# --------------------------------------------------------------------------- relaunchable statuses

def test_a_never_launched_issue_is_a_candidate():
    assert launch_rules.is_launch_candidate(None) is True
    assert launch_rules.is_launch_candidate({}) is True


def test_a_requeued_ready_issue_is_still_a_candidate():
    # The regenerate path leaves status "ready" + requeue_front — the runner WILL launch it again.
    assert launch_rules.is_launch_candidate({"status": "ready", "requeue_front": True}) is True


def test_an_in_flight_issue_is_not_a_candidate():
    for status in ("running", "gating", "holding", "merged", "blocked", "frozen", "exited"):
        assert launch_rules.is_launch_candidate({"status": status}) is False


# =========================================================================== the engine bridge
# The point of issue #138: the board's order is only true if it is the RUNNER's order. These read
# the engine's own code and compare. They SKIP when the engine's source isn't on disk (a
# standalone dashboard install) — in the monorepo, where CI runs both suites, they always fire.

def _engine_issues():
    """The runner's own ``lib/issues.py``, loaded straight from source — never imported through
    sys.path (the engine is not the dashboard's dependency; this is a read-only parity probe)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "skills", "superlooper", "skill", "lib", "issues.py")
    if not os.path.exists(path):
        pytest.skip("engine source not on disk (standalone dashboard install) — mirror untestable here")
    spec = importlib.util.spec_from_file_location("engine_issues_readonly", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# One shared fixture set, exercised by BOTH the engine and the board (DoD: "a unit test pins a
# shared fixture set to the runner-identical order"). It deliberately contains every rule that
# drifted: a numeric priority label, a `medium` alias, a mis-cased band, a requeue, a creation-time
# tie against issue number, an unknown type, a doubled model, and a blank effort.
_FIXTURES = [
    {"number": 10, "title": "plain normal", "createdAt": "2026-01-10T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}], "body": ""},
    {"number": 3, "title": "high band, newest", "createdAt": "2026-05-01T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "priority:high"}], "body": ""},
    {"number": 44, "title": "high band, oldest — low number must NOT win", "createdAt": "2026-01-01T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:investigate"}, {"name": "priority:high"}], "body": ""},
    {"number": 7, "title": "expedited from the low band", "createdAt": "2026-06-01T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "priority:low"},
                {"name": "expedite"}], "body": ""},
    {"number": 12, "title": "numeric priority is NOT a band", "createdAt": "2026-01-11T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "priority:0"}], "body": ""},
    {"number": 13, "title": "medium is NOT a band", "createdAt": "2026-01-12T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "priority:medium"}], "body": ""},
    {"number": 14, "title": "mis-cased band is NOT a band", "createdAt": "2026-01-13T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "Priority:High"}], "body": ""},
    {"number": 20, "title": "requeued after a conflict", "createdAt": "2026-04-01T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}], "body": ""},
    {"number": 31, "title": "unknown type — the runner refuses", "createdAt": "2026-01-02T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:frobnicate"}], "body": ""},
    {"number": 32, "title": "no type at all — the runner refuses", "createdAt": "2026-01-03T00:00:00Z",
     "labels": [{"name": "agent-ready"}], "body": ""},
    {"number": 33, "title": "two models — the runner refuses", "createdAt": "2026-01-04T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "model:opus"},
                {"name": "model:sonnet"}], "body": ""},
    {"number": 34, "title": "blank effort — the runner refuses", "createdAt": "2026-01-05T00:00:00Z",
     "labels": [{"name": "agent-ready"}, {"name": "type:build"}, {"name": "effort:"}], "body": ""},
]

# Only #20 came back from a conflict rebuild (loopstate's requeue_front).
_REQUEUED = {20}


def _engine_order():
    """What the RUNNER would launch, in its order — computed with the engine's own functions, the
    way scheduler.launchable() does it (pre-sort by issue number, then a stable sort on sort_key)."""
    eng = _engine_issues()
    parsed = sorted((eng.parse_issue(i) for i in _FIXTURES), key=lambda p: p["num"])
    live = [p for p in parsed if eng.eligible(p, closed_issue_nums=set(), frozen=False)]
    live.sort(key=lambda p: eng.sort_key(p, p["num"] in _REQUEUED))
    return [p["num"] for p in live]


def test_parity_the_board_launch_order_is_the_runners_launch_order():
    import flights
    cands = [{"num": i["number"], "title": i["title"], "labels": i["labels"], "body": i["body"],
              "created_at": i["createdAt"], "requeue_front": i["number"] in _REQUEUED}
             for i in _FIXTURES]
    rows = flights.queue_rows(cands, satisfied=lambda _n: False)
    board_order = [r["num"] for r in rows if r["launchable"]]
    assert board_order == _engine_order()


def test_parity_the_board_refuses_exactly_what_the_runner_refuses():
    import flights
    eng = _engine_issues()
    refused_by_runner = {i["number"] for i in _FIXTURES
                         if not eng.eligible(eng.parse_issue(i), set(), False)}
    cands = [{"num": i["number"], "title": i["title"], "labels": i["labels"], "body": i["body"],
              "created_at": i["createdAt"], "requeue_front": False} for i in _FIXTURES]
    rows = flights.queue_rows(cands, satisfied=lambda _n: False)
    refused_by_board = {r["num"] for r in rows if r["status"] == "paperwork"}
    assert refused_by_board == refused_by_runner
    assert refused_by_board == {31, 32, 33, 34}          # and it is not vacuously empty


def test_parity_the_mirrors_type_kinds_are_the_engines():
    assert tuple(launch_rules.TYPE_KINDS) == tuple(_engine_issues().VALID_TYPES)
