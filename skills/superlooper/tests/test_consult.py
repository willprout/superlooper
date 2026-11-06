"""consult.py — the pure core of the approver consult (issue #538)."""
import re
from pathlib import Path

import pytest

import consult
import launch
import worker_pretooluse

_REPO = Path(__file__).resolve().parents[3]
_RULE = _REPO / "plugin" / "skills" / "superlooper" / "references" / "consult-standing-rule.md"
_TEMPLATE = Path(__file__).resolve().parents[1] / "skill" / "templates" / "consult-brief.md"


# --------------------------------------------------------------------------- the id shape

def test_the_consult_id_is_the_issue_number_behind_a_c():
    assert consult.consult_id(538) == "c538"
    assert consult.issue_id_of("c538") == "i538"
    assert consult.is_consult_id("c538")


@pytest.mark.parametrize("bad", [0, -1, True, "538", None, 5.0])
def test_no_consult_id_for_anything_but_a_positive_int(bad):
    assert consult.consult_id(bad) is None


@pytest.mark.parametrize("bad", ["c", "i538", "c5x", "c-1", " c5", 5, None, ["c5"]])
def test_is_consult_id_is_total_and_strict(bad):
    assert not consult.is_consult_id(bad)
    assert consult.issue_id_of(bad) is None


def test_the_launcher_and_the_hook_spell_the_same_id_shape():
    """A seat the launcher can start must be one the PreToolUse table recognises (#540's rule)."""
    for sid in ("c1", "c538", "c99999"):
        assert launch.CONSULT_RE.match(sid)
        assert worker_pretooluse._role(sid) is not None
        assert worker_pretooluse._role(sid)[0] == "consult"
    for sid in ("c", "cx", "i5c"):
        assert not launch.CONSULT_RE.match(sid)


# --------------------------------------------------------------------------- same machine

@pytest.mark.parametrize("stamp,here,ok", [
    ("Williams-Mini", "Williams-Mini.lan", True),
    ("Williams-Mini", "williams-mini.local", True),
    ("Williams-Mini", "Williams-MBP", False),
    ("", "Williams-Mini", False),
    (None, "Williams-Mini", False),
    ("Williams-Mini", "", False),
])
def test_same_machine_compares_first_labels(stamp, here, ok):
    assert consult.same_machine(stamp, here) is ok


# --------------------------------------------------------------------------- the outcome file

def test_an_answer_parses():
    state, out = consult.parse_outcome(
        "OUTCOME: ANSWER\nANSWER:\nUse option B.\nKeep it small.\nREASONING:\nWilliam said so on "
        "11-04.\nEND\n")
    assert state == consult.READ
    assert out.kind == consult.ANSWER
    assert out.answer == "Use option B.\nKeep it small."
    assert out.reasoning == "William said so on 11-04."


def test_an_escalation_parses_with_its_reasoning_and_recommendation():
    state, out = consult.parse_outcome(
        "\n\nOUTCOME: escalate\nREASONING: never discussed\nRECOMMENDATION: take A\n\nEND\n\n")
    assert state == consult.READ
    assert out.kind == consult.ESCALATE
    assert out.reasoning == "never discussed"
    assert out.recommendation == "take A"


def test_a_file_without_its_end_line_is_still_being_written():
    assert consult.parse_outcome("OUTCOME: ANSWER\nANSWER: B\n") == (consult.INCOMPLETE, None)
    assert consult.parse_outcome("") == (consult.INCOMPLETE, None)


def test_no_file_is_absent():
    assert consult.parse_outcome(None) == (consult.ABSENT, None)


@pytest.mark.parametrize("text", [
    "ANSWER: B\nEND",                              # no OUTCOME line
    "OUTCOME: MAYBE\nEND",                         # not one of the two
    "OUTCOME: ANSWER\nREASONING: x\nEND",          # an answer with no answer text
    "OUTCOME: ANSWER\nANSWER:\n   \nEND",
])
def test_an_unreadable_outcome_is_never_a_decision(text):
    assert consult.parse_outcome(text) == (consult.UNREADABLE, None)


def test_an_escalation_with_nothing_else_is_still_an_escalation():
    state, out = consult.parse_outcome("OUTCOME: ESCALATE\nEND")
    assert state == consult.READ and out.kind == consult.ESCALATE


def test_the_format_the_brief_hands_out_parses():
    """The two example shapes in `outcome_format` must be what `parse_outcome` reads."""
    blocks = re.findall(r"```\n(.*?)```", consult.outcome_format(), re.S)
    assert len(blocks) == 2
    kinds = [consult.parse_outcome(b)[1].kind for b in blocks]
    assert kinds == [consult.ANSWER, consult.ESCALATE]


# --------------------------------------------------------------------------- the history

def _rec(act, ts, **kw):
    r = {"act": act, "ts": ts, "num": 5, "id": "i5", "outcome": "ok"}
    r.update(kw)
    return r


def test_history_carries_every_landed_handback_for_this_issue_in_order():
    records = [
        _rec("post_question", 1_750_000_000, question="QUESTION: A or B?"),
        _rec("answer_relaunch", 1_750_000_100),
        _rec("park", 1_750_000_200, cause="exited", memo="crashed twice"),
        _rec("reapprove", 1_750_000_300),
        _rec("recover", 1_750_000_400, tier="frozen"),
        _rec("recover", 1_750_000_500, tier="idle"),           # a nudge, not a handback
        _rec("bounce", 1_750_000_600, memo="BOUNCED: premise gone"),
        _rec("park", 1_750_000_700, outcome="label move failed"),   # never landed
        _rec("post_question", 1_750_000_800, num=6, id="i6", question="other issue"),
        _rec("consult_outcome", 1_750_000_900, outcome_kind="answered", answer="use B"),
    ]
    lines = consult.history_lines(records, 5)
    assert len(lines) == 7
    assert "A or B?" in lines[0]
    assert "owner answered" in lines[1]
    assert "crashed twice" in lines[2]
    assert "re-approved" in lines[3]
    assert "session froze" in lines[4]
    assert "premise gone" in lines[5]
    assert "use B" in lines[6]
    assert not any("other issue" in l for l in lines)


def test_history_is_total_on_garbage():
    assert consult.history_lines(None, 5) == []
    assert consult.history_lines([None, 3, {"act": "park"}], 5) == []


def test_qa_lines_name_who_answered():
    lines = consult.qa_lines([{"question": "A?", "answer": "yes"},
                              {"question": "B?", "answer": "no", "by": "consult"}], "William")
    assert "Answered by William" in lines[0]
    assert "Answered by the approving session" in lines[1]


# --------------------------------------------------------------------------- the packet

def _packet(**over):
    kw = dict(cid="c5", num=5, title="Do the thing", body="## Goal\nBuild {branch} literally.",
              question="QUESTION: A or B?\nOPTIONS: A, B\nRECOMMENDATION: B",
              comments=[{"author": {"login": "o"}, "body": "Approved by William in conversation, "
                         "2026-11-06.", "createdAt": "2026-11-06T19:55:00Z"},
                        {"author": {"login": "o"}, "body": "<!-- superlooper-question -->\nq",
                         "createdAt": "2026-11-06T20:00:00Z"},
                        {"author": {"login": "x"}, "body": "drive-by"}],
              owner="o", operator="William", repo_slug="o/r", dev_branch="main",
              branch_state=["- branch: `sl/i5-x`"], history=["- an earlier park"],
              qa=["- **Q1:** old"], outcome_path="/h/reports/c5.md", home="/repo",
              approver_session="1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c")
    kw.update(over)
    return consult.render_packet(_TEMPLATE.read_text(), **kw)


def test_the_packet_carries_everything_the_issue_names():
    text = _packet()
    assert "Do the thing" in text
    assert "QUESTION: A or B?" in text
    assert "Approved by William in conversation" in text
    assert "drive-by" in text and "not the owner" in text
    assert "machine comment" in text                       # the loop's own comment, attributed
    assert "sl/i5-x" in text
    assert "an earlier park" in text and "Q1:** old" in text
    assert "/h/reports/c5.md" in text
    assert "OUTCOME: ANSWER" in text and "OUTCOME: ESCALATE" in text
    assert "consult-standing-rule.md" in text


def test_the_packet_never_re_substitutes_a_brace_inside_the_body():
    assert "Build {branch} literally." in _packet()
    assert "{" + "outcome_path}" not in _packet()          # its own placeholders are all consumed


def test_the_packet_says_when_there_is_no_history():
    assert "first handback" in _packet(history=[], qa=[])


def test_the_template_has_no_unconsumed_placeholder():
    leftovers = set(re.findall(r"\{([a-z_]+)\}", _packet(body="", question="q")))
    assert not leftovers, leftovers


# --------------------------------------------------------------------------- what is posted

def test_the_answer_comment_is_marked_and_attributed_away_from_the_owner():
    out = consult.Outcome(consult.ANSWER, "Use B.", "He said so.", "")
    text = consult.answer_comment("QUESTION: A or B?", out, "sess-1234", "William")
    assert text.startswith(consult.MARKER)
    assert text.startswith("<!-- superlooper-")           # the brief skips it as a machine marker
    assert not text.startswith("<!-- superlooper-answer -->")   # never read as the owner's answer
    assert "not by William" in text
    assert "Use B." in text and "He said so." in text and "sess-1234" in text
    assert "nobody applied `agent-ready`" in text


def test_the_escalation_note_carries_the_reasoning_and_recommendation():
    out = consult.Outcome(consult.ESCALATE, "", "Never discussed.", "Take A.")
    text = consult.escalation_note(out, "sess-1234", "William")
    assert "Never discussed." in text and "Take A." in text and "sess-1234" in text


def test_every_fallback_reason_has_a_sentence():
    for reason in (consult.NO_STAMP, consult.OTHER_MACHINE, consult.NO_TRANSCRIPT,
                   consult.LAUNCH_FAILED, consult.FORK_EXITED, consult.TIMEOUT, consult.MALFORMED):
        assert consult.FALLBACK_REASONS[reason]
    assert "unknown" in consult.fallback_sentence(None)
    assert consult.fallback_sentence(consult.LAUNCH_FAILED, "rc=2").endswith("(rc=2)")


# --------------------------------------------------------------------------- the standing rule

def test_the_standing_rule_exists_beside_the_triage_rule_and_the_brief_points_at_it():
    assert _RULE.is_file()
    assert (_REPO / consult.RULE_PATH) == _RULE
    # The fallback copy the brief names is exactly where the gated installer mirrors the rule.
    import ops_docs
    (dst,) = [d for src, d in ops_docs.OPS_DOCS if src == consult.RULE_PATH]
    mirror = "~/.claude/skills/superlooper/" + "/".join(ops_docs.MIRROR_REL) + "/" + dst
    text = _packet()
    assert consult.RULE_PATH in text and mirror in text
    rule = _RULE.read_text()
    for must in ("approving session", "Same machine only", "ESCALATE", "agent-ready",
                 "Goal", "Definition of done", "Boundaries", "no approver stamp"):
        assert must in rule, must
