"""Issue metadata parser + queue ordering (§C.2). Pure functions over gh's issue JSON shapes."""
import issues


def _issue(number, title="a title", labels=None, body="", created="2026-07-01T10:00:00Z"):
    # gh --json returns labels as objects {"name": ...}; model that shape.
    return {
        "number": number,
        "title": title,
        "labels": [{"name": n} for n in (labels or [])],
        "body": body,
        "createdAt": created,
    }


FULL_BODY = """## Goal
Make the widget render.

## Definition of done
- [ ] the widget renders
- [ ] a regression test covers it

## Boundaries
Do not touch authentication.

## Loop metadata
touches: frontend, api
blocked-by: #41, #52
parent: #40
"""


# --------------------------- body / section parsing ---------------------------

def test_parse_sections_splits_h2():
    s = issues.parse_sections(FULL_BODY)
    assert "Make the widget render." in s["Goal"]
    assert "the widget renders" in s["Definition of done"]
    assert "Do not touch authentication." in s["Boundaries"]
    assert "touches: frontend, api" in s["Loop metadata"]
    # a nested list item starting with "-" is not mistaken for a heading
    assert "## " not in s["Goal"]


def test_parse_sections_ignores_h3_and_missing():
    body = "## Goal\ntext\n### Sub\nsubtext\n"
    s = issues.parse_sections(body)
    assert s["Goal"].strip().startswith("text")
    assert "Sub" not in s          # ### is not an H2 heading


def test_parse_loop_metadata_fields():
    m = issues.parse_loop_metadata(FULL_BODY)
    assert m["touches"] == ["frontend", "api"]
    assert m["blocked_by"] == [41, 52]
    assert m["parent"] == 40


def test_parse_loop_metadata_missing_block_is_empty():
    m = issues.parse_loop_metadata("## Goal\njust a goal, no metadata\n")
    assert m == {"touches": [], "blocked_by": [], "parent": None}


def test_parse_loop_metadata_tolerates_absent_fields():
    m = issues.parse_loop_metadata("## Loop metadata\ntouches: db\n")
    assert m["touches"] == ["db"]
    assert m["blocked_by"] == []
    assert m["parent"] is None


# --------------------------- parse_issue ---------------------------

def test_parse_issue_full():
    p = issues.parse_issue(_issue(123, title="Widget", labels=["type:build", "agent-ready"], body=FULL_BODY))
    assert p["num"] == 123
    assert p["id"] == "i123"
    assert p["title"] == "Widget"
    assert p["type"] == "build"
    assert "agent-ready" in p["labels"]
    assert p["touches"] == ["frontend", "api"]
    assert p["blocked_by"] == [41, 52]
    assert p["parent"] == 40
    assert p["created_at"] == "2026-07-01T10:00:00Z"
    assert p["priority"] == 2      # no priority label => normal
    assert p["expedite"] is False


def test_parse_issue_labels_may_be_plain_strings():
    # tolerate a query that returns labels as bare strings, not {"name": ...}
    gh = {"number": 5, "title": "t", "labels": ["type:investigate", "expedite"], "body": ""}
    p = issues.parse_issue(gh)
    assert p["type"] == "investigate"
    assert p["expedite"] is True


def test_parse_issue_minimal_dict_does_not_crash():
    p = issues.parse_issue({"number": 9})
    assert p["id"] == "i9"
    assert p["type"] == "invalid"
    assert p["labels"] == [] and p["touches"] == [] and p["blocked_by"] == []
    assert p["parent"] is None


def test_parse_issue_never_raises_on_malformed_shapes():
    # THE cardinal invariant: no gh shape may raise into a tick (a parked blocker held two issues
    # all night — a crash there would be far worse). Every wrong-TYPED field must parse, not throw.
    malformed = [
        {"number": 1, "labels": [{"name": 123}]},        # non-string label name
        {"number": 1, "labels": 7},                       # labels not a list
        {"number": 1, "labels": [None, 5, {"nope": 1}]},  # junk entries
        {"number": 1, "body": 42},                        # body not a string
        {"number": 1, "createdAt": None},                 # null timestamp
        {"number": 1, "title": None},                     # null title
        {},                                               # nothing at all
    ]
    for gh in malformed:
        p = issues.parse_issue(gh)               # must not raise
        assert p["type"] == "invalid"            # nothing malformed is ever a valid, launchable type
        assert isinstance(p["created_at"], str)  # never None (would break sorting)
        assert isinstance(p["labels"], list)


def test_parse_issue_non_dict_input_does_not_crash():
    # the top-level input itself may be junk from a broken gh call — None/list/str/int must all
    # coerce to an invalid parsed issue, never raise (cross-review round 2, Task 3).
    for junk in (None, [], "x", 42, {"number": 1, "createdAt": 42}, {"number": 1, "createdAt": {"bad": "shape"}}):
        p = issues.parse_issue(junk)
        assert p["type"] == "invalid"
        assert isinstance(p["created_at"], str)


def test_sorting_truthy_nonstring_createdat_does_not_crash():
    # a truthy non-string createdAt (42, {}) must not slip through and crash the sort in py3.9.
    a = issues.parse_issue({"number": 1, "labels": [{"name": "type:build"}], "createdAt": 42})
    b = issues.parse_issue({"number": 2, "labels": [{"name": "type:build"}], "createdAt": {"x": 1}})
    c = issues.parse_issue({"number": 3, "labels": [{"name": "type:build"}],
                            "createdAt": "2026-07-01T00:00:00Z"})
    ordered = sorted([c, a, b], key=lambda p: issues.sort_key(p, False))   # must not raise
    assert len(ordered) == 3


def test_sorting_partial_issues_does_not_crash():
    # createdAt: None must not make sort raise TypeError (None < str) in py3.9.
    a = issues.parse_issue({"number": 1, "labels": [{"name": "type:build"}], "createdAt": None})
    b = issues.parse_issue({"number": 2, "labels": [{"name": "type:build"}],
                            "createdAt": "2026-07-01T00:00:00Z"})
    ordered = sorted([b, a], key=lambda p: issues.sort_key(p, False))   # must not raise
    assert len(ordered) == 2


def test_type_extraction_exactly_one():
    assert issues.parse_issue(_issue(1, labels=["type:build"]))["type"] == "build"
    assert issues.parse_issue(_issue(1, labels=["type:investigate"]))["type"] == "investigate"
    assert issues.parse_issue(_issue(1, labels=["type:diagnose-and-fix"]))["type"] == "diagnose-and-fix"
    # zero type labels -> invalid
    assert issues.parse_issue(_issue(1, labels=["agent-ready"]))["type"] == "invalid"
    # two type labels -> invalid (ambiguous)
    assert issues.parse_issue(_issue(1, labels=["type:build", "type:investigate"]))["type"] == "invalid"
    # an unknown type value -> invalid
    assert issues.parse_issue(_issue(1, labels=["type:refactor"]))["type"] == "invalid"


def test_priority_bands():
    assert issues.parse_issue(_issue(1, labels=["priority:high"]))["priority"] == 1
    assert issues.parse_issue(_issue(1, labels=[]))["priority"] == 2
    assert issues.parse_issue(_issue(1, labels=["priority:low"]))["priority"] == 3


# --------------------- per-issue model / effort override labels (§C.2) ---------------------

def test_model_label_extracted_as_override():
    # a single model:<value> label carries a per-issue worker-model override; the value is
    # pass-through (no allowlist), so any model string the agent accepts survives verbatim —
    # including the bracketed 1M-context form.
    assert issues.parse_issue(_issue(1, labels=["type:build", "model:fable"]))["model"] == "fable"
    assert issues.parse_issue(_issue(1, labels=["type:build", "model:opus[1m]"]))["model"] == "opus[1m]"
    # an unknown value is still passed through — validation is the launch's job (fail loud + park),
    # not the parser's, so there is no allowlist to keep in sync with new model names.
    assert issues.parse_issue(_issue(1, labels=["type:build", "model:whatever"]))["model"] == "whatever"


def test_effort_label_extracted_as_override():
    assert issues.parse_issue(_issue(1, labels=["type:build", "effort:high"]))["effort"] == "high"
    assert issues.parse_issue(_issue(1, labels=["type:build", "effort:max"]))["effort"] == "max"


def test_no_model_or_effort_label_means_no_override():
    # the default path: no control label -> None, so the runner falls back to config/loader default
    # for the model and sends NOTHING at all for effort (never a default).
    p = issues.parse_issue(_issue(1, labels=["type:build", "agent-ready"]))
    assert p["model"] is None
    assert p["effort"] is None
    assert p["label_conflict"] is False


def test_multiple_model_labels_are_a_conflict_not_a_silent_pick():
    # mirror the exactly-one type:* rule: 2+ model:* labels is ambiguous -> no override value AND a
    # conflict flag eligible() refuses to launch on (the issue waits for William to fix the labels,
    # rather than the parser silently picking one).
    p = issues.parse_issue(_issue(1, labels=["type:build", "model:fable", "model:opus"]))
    assert p["model"] is None
    assert p["label_conflict"] is True


def test_multiple_effort_labels_are_a_conflict():
    p = issues.parse_issue(_issue(1, labels=["type:build", "effort:low", "effort:high"]))
    assert p["effort"] is None
    assert p["label_conflict"] is True


def test_empty_value_control_label_is_invalid_not_a_silent_default():
    # a malformed control label with an EMPTY (or whitespace-only) value — a bare `model:` /
    # `effort:` — must NOT fail open to the config default. It is invalid, so it sets the conflict
    # flag (eligible() refuses) exactly like an unknown type:* value: fail CLOSED, wait for William.
    pm = issues.parse_issue(_issue(1, labels=["type:build", "model:"]))
    assert pm["model"] is None and pm["label_conflict"] is True
    pe = issues.parse_issue(_issue(1, labels=["type:build", "effort:"]))
    assert pe["effort"] is None and pe["label_conflict"] is True
    pw = issues.parse_issue(_issue(1, labels=["type:build", "model:   "]))
    assert pw["model"] is None and pw["label_conflict"] is True


def test_model_and_effort_parse_never_raises_on_malformed_labels():
    # defensive parity with the rest of parse_issue: a wrong-typed label set must never raise into a
    # tick — it parses to something eligible() simply refuses.
    for junk in ({"number": 1, "labels": "model:fable"},               # labels not a list
                 {"number": 1, "labels": [{"name": None}, {"name": "model:fable"}]}):
        p = issues.parse_issue(junk)                 # must not raise
        assert "model" in p and "effort" in p and "label_conflict" in p


# --------------------------- eligible ---------------------------

def _ready(number=1, labels=("type:build", "agent-ready"), body=""):
    return issues.parse_issue(_issue(number, labels=list(labels), body=body))


def test_eligible_happy():
    assert issues.eligible(_ready(), closed_issue_nums=set(), frozen=False) is True


def test_eligible_requires_agent_ready():
    assert issues.eligible(_ready(labels=["type:build"]), set(), frozen=False) is False


# ---- resume: the SAME predicate serves every restart path (issue #150 / D8) ----

def test_resume_accepts_the_runners_own_in_progress_stamp_as_the_approval():
    # A launch moves `agent-ready` -> `in-progress`, so a session the runner is RESTARTING never
    # carries `agent-ready`. Demanding it would refuse every recovery; the runner's own stamp is
    # the approval it already acted on. Fresh launches are untouched by this — they still demand
    # `agent-ready`.
    p = _ready(labels=["type:build", "in-progress"])
    assert issues.eligible(p, set(), frozen=False) is False                  # fresh: not approved
    assert issues.eligible(p, set(), frozen=False, resume=True) is True      # restart: approved


def test_resume_still_demands_every_other_condition():
    # The whole point of D8: `resume` relaxes WHICH approval token is accepted and nothing else.
    open_dep = _ready(labels=["type:build", "in-progress"],
                      body="## Loop metadata\nblocked-by: #41\n")
    assert issues.eligible(open_dep, set(), frozen=False, resume=True) is False
    assert issues.eligible(open_dep, {41}, frozen=False, resume=True) is True

    bad_type = _ready(labels=["type:build", "type:investigate", "in-progress"])
    assert issues.eligible(bad_type, set(), frozen=False, resume=True) is False

    conflicted = _ready(labels=["type:build", "in-progress"])
    conflicted["label_conflict"] = True
    assert issues.eligible(conflicted, set(), frozen=False, resume=True) is False


def test_resume_refuses_an_issue_with_no_approval_token_at_all():
    # William parked it mid-flight (both tokens gone): a restart is not his word to continue.
    assert issues.eligible(_ready(labels=["type:build", "parked"]), set(), frozen=False,
                           resume=True) is False


def test_eligible_requires_valid_type():
    assert issues.eligible(_ready(labels=["agent-ready"]), set(), frozen=False) is False
    assert issues.eligible(_ready(labels=["type:build", "type:investigate", "agent-ready"]),
                           set(), frozen=False) is False


def test_eligible_refuses_on_a_model_label_conflict():
    # two model:* labels -> a control-label conflict eligible() must refuse (mirrors the invalid-type
    # rule): a mislabeled issue never launches; it waits for William to fix the labels.
    p = _ready(labels=["type:build", "agent-ready", "model:fable", "model:opus"])
    assert issues.eligible(p, set(), frozen=False) is False


def test_eligible_refuses_on_an_effort_label_conflict():
    p = _ready(labels=["type:build", "agent-ready", "effort:low", "effort:max"])
    assert issues.eligible(p, set(), frozen=False) is False


def test_eligible_happy_with_a_single_model_and_effort_label():
    # one of each is the valid override case — it must NOT be treated as a conflict.
    p = _ready(labels=["type:build", "agent-ready", "model:fable", "effort:high"])
    assert issues.eligible(p, set(), frozen=False) is True


def test_eligible_refuses_on_an_empty_value_control_label():
    # a bare `model:` is malformed -> ineligible (fail closed), never a silent launch on the default.
    p = _ready(labels=["type:build", "agent-ready", "model:"])
    assert issues.eligible(p, set(), frozen=False) is False


def test_eligible_requires_all_blocked_by_closed():
    p = _ready(body="## Loop metadata\nblocked-by: #41, #52\n")
    assert issues.eligible(p, closed_issue_nums={41}, frozen=False) is False       # 52 still open
    assert issues.eligible(p, closed_issue_nums={41, 52}, frozen=False) is True    # both closed


def test_eligible_unaffected_by_freeze():
    # freeze only stops MERGES, not builds — a frozen mainline must not change eligibility.
    p = _ready()
    assert issues.eligible(p, set(), frozen=True) == issues.eligible(p, set(), frozen=False) is True


def test_dependency_chain_behind_parked_stays_ineligible_no_crash():
    # THE paid-for regression (sub-1 held sub-4 and sub-5 all night): a parked blocker (#1, NOT
    # closed) must keep its dependents ineligible, and the tick must never crash on the chain.
    p4 = _ready(4, body="## Loop metadata\nblocked-by: #1\n")
    p5 = _ready(5, body="## Loop metadata\nblocked-by: #1\n")
    closed = set()                       # #1 is parked, not closed
    assert issues.eligible(p4, closed, frozen=False) is False
    assert issues.eligible(p5, closed, frozen=False) is False
    # a deeper chain (#1 itself blocked-by #0, also unmet) must not crash either
    p1 = _ready(1, body="## Loop metadata\nblocked-by: #0\n")
    assert issues.eligible(p1, closed, frozen=False) is False
    # once #1 closes, its direct dependents unblock
    assert issues.eligible(p4, {1}, frozen=False) is True


# --------------------------- sort_key / ordering ---------------------------

def test_sort_key_full_ordering():
    # build a mixed queue and sort by (not expedite, priority, not requeue_front, created_at)
    a = issues.parse_issue(_issue(1, labels=["type:build"], created="2026-07-01T09:00:00Z"))          # normal, old
    b = issues.parse_issue(_issue(2, labels=["type:build", "priority:high"], created="2026-07-02T09:00:00Z"))  # high
    c = issues.parse_issue(_issue(3, labels=["type:build", "expedite"], created="2026-07-03T09:00:00Z"))       # expedite
    d = issues.parse_issue(_issue(4, labels=["type:build", "priority:low"], created="2026-06-01T09:00:00Z"))   # low, oldest
    e = issues.parse_issue(_issue(5, labels=["type:build"], created="2026-06-15T09:00:00Z"))          # normal, older than a
    # requeue_front only for issue 5
    requeue = {5}
    ordered = sorted([a, b, c, d, e], key=lambda p: issues.sort_key(p, p["num"] in requeue))
    nums = [p["num"] for p in ordered]
    # expedite (3) first; then high (2); then normal band with requeue_front (5) ahead of (1);
    # then low (4) last regardless of being oldest.
    assert nums == [3, 2, 5, 1, 4]


def test_sort_key_created_at_tiebreak_oldest_first():
    older = issues.parse_issue(_issue(1, labels=["type:build"], created="2026-07-01T00:00:00Z"))
    newer = issues.parse_issue(_issue(2, labels=["type:build"], created="2026-07-05T00:00:00Z"))
    ordered = sorted([newer, older], key=lambda p: issues.sort_key(p, False))
    assert [p["num"] for p in ordered] == [1, 2]
