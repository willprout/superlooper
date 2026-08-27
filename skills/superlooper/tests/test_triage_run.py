"""The triage flight's BRAIN — what a verdict means, and what the run writes down (issue #449).

The delegation is RULED in ``plugin/skills/superlooper/references/triage-standing-rule.md``;
#448 shipped the state contract and the trigger. This suite pins the layer between them: the
per-issue verdict vocabulary as an ACT, the mechanical guards that keep the delegation inside its
own edges, the prose every close carries, and the sitting sheet the escalations compose into.

Everything here is PURE — no gh, no disk, no clock. The CLI verbs that spend these decisions are
driven end-to-end against a faked GitHub in ``tests/test_triage_flight.py``; what this file holds
is the reasoning those verbs must not be free to improvise.
"""
import pytest

import triage
import triage_run


# --------------------------------------------------------------------------- the rubric

def test_the_default_rubric_applies_when_the_repo_says_nothing():
    assert triage_run.rubric({}) == triage_run.DEFAULT_RUBRIC
    assert triage_run.rubric({"triage": {}}) == triage_run.DEFAULT_RUBRIC
    assert triage_run.rubric({"triage": {"rubric": None}}) == triage_run.DEFAULT_RUBRIC
    # every default line carries the three things a close comment has to print
    for line in triage_run.DEFAULT_RUBRIC:
        assert line.id and line.title and line.test


def test_a_per_repo_rubric_replaces_the_default_set_entirely():
    cfg = {"triage": {"rubric": [{"id": "X1", "title": "Ours", "test": "only what we say."}]}}
    lines = triage_run.rubric(cfg)
    assert [l.id for l in lines] == ["X1"]
    assert triage_run.rubric_line(cfg, "X1").title == "Ours"
    # the default lines are GONE — an override is a replacement, not an addition
    assert triage_run.rubric_line(cfg, "N1") is None


def test_an_unreadable_rubric_override_falls_back_to_the_default_set():
    # The LOADER is where a malformed override fails loudly (tests/test_config.py). Every runtime
    # reader may still be handed a half-read config, and the safe direction is the owner's own
    # default rubric — never an empty one, which would make every nit unclosable... or worse,
    # make any string a rubric line.
    for bad in ("N1", [], [{"id": "", "title": "x", "test": "y"}], [{"nope": 1}], 7):
        assert triage_run.rubric({"triage": {"rubric": bad}}) == triage_run.DEFAULT_RUBRIC


def test_the_default_rubric_matches_the_standing_rule_document():
    """The rule is the AUTHORITY; this module implements it. Drift between them is a defect."""
    import re
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[3] / "plugin" / "skills" / "superlooper"
           / "references" / "triage-standing-rule.md").read_text(encoding="utf-8")
    # The document is hand-wrapped prose, so compare on collapsed whitespace: a rubric line that
    # happens to straddle a line break in the rule is still the same line.
    flat = re.sub(r"\s+", " ", doc)
    for line in triage_run.DEFAULT_RUBRIC:
        assert "%s %s" % (line.id, line.title) in flat, (
            "rubric line %s is not the one the standing rule defines" % line.id)
        assert re.sub(r"\s+", " ", line.test) in flat, (
            "rubric line %s's test is not the rule's own wording" % line.id)
    for phrase in triage_run.NEVER_A_NIT:
        assert phrase in flat


# --------------------------------------------------------------------------- the verdicts

def test_every_verdict_in_the_rules_vocabulary_parses_to_one_act():
    assert triage_run.parse_verdict("buildable").act == triage_run.KEEP
    assert triage_run.parse_verdict("underspecified").act == triage_run.KEEP
    assert triage_run.parse_verdict("contains-owner-decision").act == triage_run.ESCALATE
    assert triage_run.parse_verdict("overtaken").act == triage_run.CLOSE
    d = triage_run.parse_verdict("duplicate-of-#98")
    assert d.act == triage_run.ABSORB and d.param == 98
    n = triage_run.parse_verdict("nit(N3)")
    assert n.act == triage_run.CLOSE and n.param == "N3"


def test_a_verdict_outside_the_vocabulary_is_refused_not_guessed():
    for bad in ("close", "", None, 7, "duplicate-of-#", "duplicate-of-#0", "nit()", "nit(",
                "duplicate-of-#-3", "NIT(N1)"):
        assert triage_run.parse_verdict(bad) is None


def test_the_verdict_round_trips_through_the_stores_own_spelling():
    """The store (#448) and this module must agree letter-for-letter, or a recorded verdict
    cannot be read back as the act it was."""
    assert triage_run.parse_verdict(triage.duplicate_of(98)).param == 98
    assert triage_run.parse_verdict(triage.nit("N3")).param == "N3"
    assert triage_run.parse_verdict(triage.OVERTAKEN).act == triage_run.CLOSE
    assert triage_run.parse_verdict(triage.BUILDABLE).act == triage_run.KEEP
    assert triage_run.parse_verdict(triage.CONTAINS_OWNER_DECISION).act == triage_run.ESCALATE


# --------------------------------------------------------------------------- the guards

def _issue(num=5, labels=(), body="b", title="t"):
    return {"number": num, "title": title, "body": body,
            "labels": [{"name": n} for n in labels]}


def test_an_issue_the_loop_holds_may_never_be_acted_on():
    for label in triage.HELD_LABELS:
        assert triage_run.held(_issue(labels=[label])) is True
    assert triage_run.held(_issue(labels=["needs-owner", "type:build"])) is False
    # a label set this cannot read counts as UNHELD only because `changed()` already says so —
    # the acting guard must be the strict one, so garbage reads as HELD (never act on a fog)
    assert triage_run.held({"number": 5, "labels": "broken"}) is True
    assert triage_run.held("not an issue") is True


def test_a_reopened_issue_with_an_unchanged_body_is_owner_protest():
    body = "the body the flight already judged"
    closed = {"body_hash": triage.body_hash(body), "verdict": triage.OVERTAKEN,
              "date": "2026-08-20"}
    assert triage_run.reopen_protest(closed, body) is True
    # the body CHANGED since that close -> the owner rewrote it, and it is judgeable again
    assert triage_run.reopen_protest(closed, body + " (rewritten)") is False
    # a KEEP verdict is not a close, so an open issue carrying one is not a reopen at all
    kept = {"body_hash": triage.body_hash(body), "verdict": triage.BUILDABLE, "date": "x"}
    assert triage_run.reopen_protest(kept, body) is False
    # nothing recorded / unreadable records -> never a protest (it was never closed by us)
    for record in (None, {}, {"verdict": 7}, "garbage", {"verdict": triage.OVERTAKEN}):
        assert triage_run.reopen_protest(record, body) is False


def test_the_approval_labels_are_refused_at_the_call_never_applied():
    assert triage_run.forbidden_label("agent-ready") is not None
    assert triage_run.forbidden_label("pre-authorized:referee") is not None
    assert triage_run.forbidden_label("pre-authorized:anything") is not None
    assert triage_run.forbidden_label("type:build") is None
    assert triage_run.forbidden_label("needs-owner") is None


# --------------------------------------------------------------------------- the prose

def test_every_close_comment_carries_the_machine_marker_and_its_evidence():
    dup = triage_run.duplicate_close_comment(98, "both ask for the same retry cap")
    assert triage_run.MARKER in dup and "#98" in dup
    assert "same retry cap" in dup, "the evidence rides in the close, not only in the log"

    over = triage_run.overtaken_close_comment("abc1234", "the fix landed", "main")
    assert triage_run.MARKER in over and "abc1234" in over and "the fix landed" in over
    assert "origin/main" in over

    line = triage_run.rubric_line({}, "N3")
    nit = triage_run.nit_close_comment(line, "rounds to whole minutes", 12,
                                       "https://github.com/o/r/issues/12#issuecomment-1")
    assert triage_run.MARKER in nit
    assert "N3" in nit and line.title in nit
    assert "#12" in nit and "issuecomment-1" in nit


def test_the_ledger_entry_names_the_rubric_line_and_links_back_to_the_closed_issue():
    line = triage_run.rubric_line({}, "N2")
    entry = triage_run.ledger_entry(line, "the label reads oddly", 140)
    assert triage_run.ledger_marker(140) in entry
    assert "[N2]" in entry and line.title in entry
    assert "#140" in entry


def test_the_absorbed_content_lands_in_the_absorbers_body_under_its_own_heading():
    new = triage_run.absorbed_body("## Goal\nthe original\n", 131, "A duplicate", "## Goal\ndup\n")
    assert new.startswith("## Goal\nthe original\n")
    assert "#131" in new and "A duplicate" in new and "## Goal\ndup" in new
    # idempotent: absorbing the same issue twice must not double the section
    assert triage_run.absorbed_body(new, 131, "A duplicate", "## Goal\ndup\n") == new


def test_a_sitting_sheet_line_is_one_line_plus_one_recommendation():
    line = triage_run.sitting_line(150, "Pick a storage engine",
                                   "the body asks which database to use",
                                   "decide the engine, then I will fix the body")
    assert line.count("\n") <= 1
    assert "#150" in line and "Pick a storage engine" in line
    assert "recommend" in line.lower()


def test_the_sitting_sheet_is_silent_with_nothing_to_escalate():
    assert triage_run.sitting_sheet([]) == ""
    sheet = triage_run.sitting_sheet([triage_run.sitting_line(1, "t", "n", "r")])
    assert triage_run.SITTING_HEADING in sheet and "#1" in sheet


def test_a_label_entry_this_cannot_read_counts_as_held():
    """Fail CLOSED on wrong-typed input, not merely on unsafe input — the acting guard's own rule.

    A dict label whose `name` is not a string is a label set this cannot read, and reading it as
    "no such label" is how `{"labels": [{"name": 7}]}` gets edited and closed. (Fresh-agent review.)
    """
    for broken in ({"name": 7}, {"name": None}, {}, {"nome": "agent-ready"}, ["agent-ready"], 7):
        assert triage_run.held({"number": 5, "labels": [broken]}) is True, broken
    # a readable set beside an unreadable entry is still unreadable
    assert triage_run.held({"number": 5, "labels": [{"name": "type:build"}, {"name": 7}]}) is True
    # ...and a wholly readable one answers honestly, in both spellings gh uses
    assert triage_run.held({"number": 5, "labels": [{"name": "type:build"}]}) is False
    assert triage_run.held({"number": 5, "labels": ["agent-ready"]}) is True
