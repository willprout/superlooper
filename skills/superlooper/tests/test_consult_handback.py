"""consult.py — the handback consult's pure core (issue #539).

Every issue-level handback the command center puts in front of the owner — a park, a bounce, a frozen
lane, a lane at a dialog, a lane the session host reports blocked — goes first to a fork of the
session that approved the issue. Its outcome file picks ONE move from a small set the runner can
already carry out; anything else is refused and reaches the owner."""
import re
from pathlib import Path

import pytest

import consult

_REPO = Path(__file__).resolve().parents[3]
_RULE = _REPO / "plugin" / "skills" / "superlooper" / "references" / "consult-standing-rule.md"
_TEMPLATE = (Path(__file__).resolve().parents[1] / "skill" / "templates"
             / "handback-consult-brief.md")


def _file(*lines):
    return "\n".join(lines) + "\nEND\n"


# --------------------------------------------------------------------------- the move set

def test_the_move_set_is_exactly_the_six_the_runner_can_carry_out():
    assert consult.MOVES == (consult.RELAUNCH, consult.RESUME, consult.END_RELAUNCH, consult.WAIT,
                             consult.FOLLOW_UP, consult.ESCALATE)


def test_the_handback_kinds_are_the_five_the_dashboard_shows():
    assert consult.HANDBACK_KINDS == (consult.PARK, consult.BOUNCE, consult.FROZEN,
                                      consult.AT_DIALOG, consult.HOST_BLOCKED)


@pytest.mark.parametrize("word,move", [
    ("RELAUNCH", consult.RELAUNCH), ("relaunch", consult.RELAUNCH),
    ("RESUME", consult.RESUME),
    ("END_AND_RELAUNCH", consult.END_RELAUNCH), ("end-and-relaunch", consult.END_RELAUNCH),
    ("End and relaunch", consult.END_RELAUNCH),
])
def test_each_move_word_parses(word, move):
    state, out, refused = consult.parse_handback_outcome(
        _file("MOVE: %s" % word, "REASONING:", "he said so"))
    assert state == consult.READ and refused is None
    assert out.move == move and out.reasoning == "he said so"


def test_a_relaunch_carries_its_clarification():
    state, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: RELAUNCH", "REASONING:", "the park was the flaky review step",
        "CLARIFICATION:", "Use approach A.", "It was ruled on 11-04."))
    assert state == consult.READ
    assert out.clarification == "Use approach A.\nIt was ruled on 11-04."


def test_a_wait_names_its_bounded_minutes():
    state, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: WAIT", "REASONING:", "CI is slow tonight", "MINUTES: 30"))
    assert state == consult.READ and out.move == consult.WAIT and out.minutes == 30


def test_a_wait_longer_than_the_bound_is_clamped_to_it():
    _, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: WAIT", "REASONING:", "r", "MINUTES: 100000"))
    assert out.minutes == consult.WAIT_MAX_MINUTES


@pytest.mark.parametrize("minutes", ["", "0", "-5", "soon", "1.5"])
def test_a_wait_without_a_usable_bound_is_never_a_decision(minutes):
    state, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: WAIT", "REASONING:", "r", "MINUTES: %s" % minutes))
    assert state == consult.UNREADABLE and out is None


def test_a_follow_up_carries_its_title_type_and_body():
    state, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: FOLLOW_UP", "REASONING:", "the fix belongs in the engine",
        "TITLE: Fix the flaky review step", "TYPE: diagnose-and-fix",
        "BODY:", "## Goal", "Make it stop.", "REASONING: a line the body itself carries"))
    assert state == consult.READ and out.move == consult.FOLLOW_UP
    assert out.title == "Fix the flaky review step"
    assert out.issue_type == "type:diagnose-and-fix"
    assert out.body.startswith("## Goal\nMake it stop.")
    assert "a line the body itself carries" in out.body
    assert out.reasoning == "the fix belongs in the engine"


def test_a_follow_up_with_no_type_is_a_build():
    _, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: FOLLOW_UP", "REASONING:", "r", "TITLE: t", "BODY:", "b"))
    assert out.issue_type == "type:build"


@pytest.mark.parametrize("missing", ["TITLE", "BODY"])
def test_a_follow_up_missing_its_title_or_body_is_never_a_decision(missing):
    lines = ["MOVE: FOLLOW_UP", "REASONING:", "r", "TITLE: t", "BODY:", "b"]
    if missing == "TITLE":
        lines.remove("TITLE: t")
    else:
        lines = lines[:4]
    assert consult.parse_handback_outcome(_file(*lines))[0] == consult.UNREADABLE


@pytest.mark.parametrize("move", ["RELAUNCH", "RESUME", "END_AND_RELAUNCH", "WAIT", "FOLLOW_UP"])
def test_a_move_the_runner_acts_on_needs_its_reasoning(move):
    extra = {"WAIT": ["MINUTES: 5"], "FOLLOW_UP": ["TITLE: t", "BODY:", "b"]}.get(move, [])
    assert consult.parse_handback_outcome(_file("MOVE: %s" % move, *extra))[0] == \
        consult.UNREADABLE


def test_an_escalation_with_nothing_else_is_still_an_escalation():
    state, out, _ = consult.parse_handback_outcome(_file("MOVE: ESCALATE"))
    assert state == consult.READ and out.move == consult.ESCALATE


def test_an_escalation_carries_its_reasoning_and_recommendation():
    _, out, _ = consult.parse_handback_outcome(_file(
        "MOVE: ESCALATE", "REASONING:", "never discussed", "RECOMMENDATION:", "take A"))
    assert out.reasoning == "never discussed" and out.recommendation == "take A"


# ------------------------------------------------- the moves the set must never include (DoD)

@pytest.mark.parametrize("word", [
    "HAND_MERGE", "MERGE", "hand-merge the PR", "FORCE_PUSH", "git push --force",
    "AGENT_READY", "APPLY agent-ready", "PRE_AUTHORIZE", "pre-authorized:referee",
    "PUBLISH", "bin/install.sh", "EDIT .superlooper/config.json", "SEND_KEYS",
    "TYPE into the worker's pane", "keystrokes", "ANSWER",
])
def test_a_move_outside_the_set_is_refused_and_named(word):
    state, out, refused = consult.parse_handback_outcome(_file("MOVE: %s" % word, "REASONING:", "r"))
    assert state == consult.REFUSED and out is None
    assert refused == word


def test_the_never_moves_are_named_for_the_brief_and_none_is_a_move():
    for never in ("hand-merge", "force-push", "agent-ready", "pre-authorized:", "publish",
                  ".superlooper/**", "keystrokes"):
        assert any(never in n for n in consult.NEVER_MOVES), never
    for word in consult.NEVER_MOVES:
        assert consult.parse_handback_outcome(_file("MOVE: %s" % word, "REASONING:", "r"))[0] == \
            consult.REFUSED


def test_a_question_outcome_is_not_a_handback_move():
    state, _, _ = consult.parse_handback_outcome(_file("OUTCOME: ANSWER", "ANSWER:", "x"))
    assert state == consult.UNREADABLE


def test_a_file_without_its_end_line_is_still_being_written():
    assert consult.parse_handback_outcome("MOVE: RELAUNCH\nREASONING:\nr\n")[0] == \
        consult.INCOMPLETE


@pytest.mark.parametrize("garbage", [None, 5, ["MOVE: WAIT"], b"MOVE: WAIT\nEND\n"])
def test_the_parser_is_total_on_garbage(garbage):
    assert consult.parse_handback_outcome(garbage)[0] == consult.ABSENT


def test_the_format_the_brief_hands_out_parses():
    fmt = consult.handback_outcome_format()
    blocks = re.findall(r"```\n(.*?)```", fmt, re.S)
    assert len(blocks) == len(consult.MOVES)
    for block in blocks:
        filled = re.sub(r"<[^>]+>", "x", block).replace("MINUTES: x", "MINUTES: 10")
        state, out, _ = consult.parse_handback_outcome(filled)
        assert state == consult.READ, block
    for word in consult.NEVER_MOVES:
        assert word in fmt, word


# --------------------------------------------------------------------------- the cap

def test_the_handback_cap_defaults_to_three_and_the_owner_can_change_it():
    assert consult.HANDBACK_CAP_DEFAULT == 3
    assert consult.handback_cap({}) == 3
    assert consult.handback_cap({"session": {"handback_cap": 5}}) == 5
    assert consult.handback_cap({"session": {"handback_cap": 0}}) == 0
    for bad in (None, "3", True, -1, 2.5):
        assert consult.handback_cap({"session": {"handback_cap": bad}}) == 3


# --------------------------------------------------------------------------- the keys

def test_a_handback_key_names_its_kind_and_episode():
    assert consult.handback_key(consult.FROZEN, 1700000000) == "frozen:1700000000"
    a = consult.handback_key(consult.PARK, "frozen_cap")
    assert a.startswith("park:") and a == consult.handback_key(consult.PARK, "frozen_cap")
    assert a != consult.handback_key(consult.PARK, "exited_cap")
    long_memo = "x" * 5000
    assert len(consult.handback_key(consult.PARK, long_memo)) < 40


# --------------------------------------------------------------------------- the packet

def _packet(**over):
    kw = dict(cid="c5", num=5, title="Do the thing", body="## Goal\nBuild {branch} literally.",
              kind=consult.PARK, what="frozen and already relaunched 2 times (cap 2) — parking",
              comments=[{"author": {"login": "o"}, "body": "Approved by William.",
                         "createdAt": "2026-11-06T19:55:00Z"}],
              owner="o", operator="William", repo_slug="o/r", dev_branch="main",
              branch_state=["- branch: `sl/i5-x`"], history=["- an earlier park"],
              qa=["- **Q1:** old"], outcome_path="/h/reports/c5.md", home="/repo",
              approver_session="1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c",
              handback_number=1, cap=3)
    kw.update(over)
    return consult.render_handback_packet(_TEMPLATE.read_text(), **kw)


def test_the_packet_carries_the_handback_and_everything_the_issue_names():
    text = _packet()
    assert "frozen and already relaunched 2 times" in text
    assert "Do the thing" in text and "Approved by William." in text
    assert "sl/i5-x" in text and "an earlier park" in text and "Q1:** old" in text
    assert "/h/reports/c5.md" in text
    for word in ("MOVE: RELAUNCH", "MOVE: RESUME", "MOVE: END_AND_RELAUNCH", "MOVE: WAIT",
                 "MOVE: FOLLOW_UP", "MOVE: ESCALATE"):
        assert word in text, word
    assert "consult-standing-rule.md" in text


def test_the_packet_never_re_substitutes_a_brace_inside_the_body():
    assert "Build {branch} literally." in _packet()


def test_the_template_has_no_unconsumed_placeholder():
    for kind in consult.HANDBACK_KINDS:
        leftovers = set(re.findall(r"\{([a-z_]+)\}", _packet(body="", kind=kind)))
        assert not leftovers, (kind, leftovers)


def test_the_packet_says_when_a_bright_line_park_will_reach_the_owner_regardless():
    text = _packet(bright_line=True)
    assert "bright line" in text.lower() and "whatever you decide" in text


def test_the_packet_says_which_handback_this_is_against_the_cap():
    assert "handback 2" in _packet(handback_number=2, cap=3)
    assert "goes to William" in _packet(handback_number=2, cap=3)


def test_the_template_points_at_the_rule_and_its_published_mirror():
    import ops_docs
    (dst,) = [d for src, d in ops_docs.OPS_DOCS if src == consult.RULE_PATH]
    mirror = "~/.claude/skills/superlooper/" + "/".join(ops_docs.MIRROR_REL) + "/" + dst
    text = _packet()
    assert consult.RULE_PATH in text and mirror in text


@pytest.mark.parametrize("kind", consult.HANDBACK_KINDS)
def test_every_kind_has_a_plain_description(kind):
    assert consult.KIND_WORDS[kind]


# --------------------------------------------------------------------------- what is posted

def test_the_move_comment_is_marked_and_attributed_away_from_the_owner():
    out = consult.HandbackOutcome(consult.RELAUNCH, "Use A.", "the review step flaked", "", None,
                                  "", "", "")
    text = consult.move_comment(consult.PARK, "parked: frozen at cap", out, "sess-1234", "William")
    assert text.startswith(consult.MARKER)
    assert "not by William" in text and "sess-1234" in text
    assert "Use A." in text and "the review step flaked" in text
    assert "nobody applied `agent-ready`" in text


@pytest.mark.parametrize("move", [consult.RELAUNCH, consult.RESUME, consult.END_RELAUNCH,
                                  consult.WAIT])
def test_every_acted_move_has_a_sentence(move):
    out = consult.HandbackOutcome(move, "", "r", "", 30, "", "", "")
    assert consult.MOVE_SENTENCES[move]
    assert consult.move_comment(consult.FROZEN, "frozen", out, "s", "William")


def test_the_handback_note_carries_the_reasoning_and_recommendation():
    text = consult.handback_note({"outcome": "escalated", "reasoning": "never discussed",
                                  "recommendation": "take A", "session": "sess-1234",
                                  "kind": consult.PARK}, "William")
    assert text.startswith(consult.MARKER)
    assert "never discussed" in text and "take A" in text and "sess-1234" in text


def test_the_note_for_a_forced_bright_line_escalation_says_what_the_fork_proposed():
    text = consult.handback_note({"outcome": "escalated", "forced": "bright_line",
                                  "proposed": consult.RELAUNCH, "reasoning": "safe enough",
                                  "session": "s", "kind": consult.PARK}, "William")
    assert "bright line" in text.lower() and "relaunch" in text.lower()


def test_the_note_for_a_refused_move_names_it():
    text = consult.handback_note({"outcome": "fell_back", "reason": consult.REFUSED_MOVE,
                                  "detail": "HAND_MERGE", "kind": consult.PARK}, "William")
    assert "HAND_MERGE" in text and "no consult may" in text


def test_the_cap_note_attaches_the_consult_history():
    text = consult.handback_note({"outcome": "fell_back", "reason": consult.HANDBACK_CAP,
                                  "kind": consult.FROZEN, "consults": 2, "cap": 3},
                                 "William", history=["- 2026-11-06 — a consult relaunched it"])
    assert "a consult relaunched it" in text
    assert "3" in text


def test_the_follow_up_note_names_the_filed_issue():
    text = consult.handback_note({"outcome": "escalated", "move": consult.FOLLOW_UP,
                                  "follow_up": 612, "reasoning": "r", "session": "s",
                                  "kind": consult.PARK}, "William")
    assert "#612" in text


@pytest.mark.parametrize("reason", [consult.NO_STAMP, consult.NO_TRANSCRIPT, consult.TIMEOUT,
                                    consult.LAUNCH_FAILED, consult.FORK_EXITED, consult.MALFORMED,
                                    consult.HELD_NOW])
def test_a_plain_fallback_posts_nothing_new(reason):
    assert not consult.note_posts({"outcome": "fell_back", "reason": reason})


@pytest.mark.parametrize("via", [{"outcome": "escalated"},
                                 {"outcome": "fell_back", "reason": consult.REFUSED_MOVE},
                                 {"outcome": "fell_back", "reason": consult.HANDBACK_CAP}])
def test_an_escalation_a_refusal_and_the_cap_each_post_a_note(via):
    assert consult.note_posts(via)


def test_every_new_fallback_reason_has_a_sentence():
    for reason in (consult.HANDBACK_CAP, consult.REFUSED_MOVE, consult.HELD_NOW):
        assert consult.FALLBACK_REASONS[reason]


def test_the_follow_up_body_carries_its_provenance_and_loop_metadata():
    body = consult.follow_up_body("## Goal\nFix it.", 5, "sess-1234", ["engine", "plugin"])
    assert body.startswith("## Goal\nFix it.")
    assert "#5" in body and "sess-1234" in body
    assert "## Loop metadata\ntouches: engine, plugin" in body


def test_a_follow_up_body_that_already_declares_its_metadata_keeps_it():
    body = consult.follow_up_body("## Goal\nx\n\n## Loop metadata\ntouches: dashboard\n", 5, "s",
                                  ["engine"])
    assert body.count("## Loop metadata") == 1 and "touches: dashboard" in body


def test_the_follow_up_labels_are_unapproved():
    labels = consult.follow_up_labels("type:investigate")
    assert "needs-owner" in labels and "type:investigate" in labels
    assert consult.FOLLOW_UP_SOURCE in labels
    assert not any(l == "agent-ready" or l.startswith("pre-authorized:") for l in labels)


# --------------------------------------------------------------------------- the history

def test_history_carries_a_handback_consults_move():
    recs = [{"act": "consult_outcome", "num": 5, "ts": 1.7e9, "outcome": "ok",
             "outcome_kind": "moved", "handback": consult.FROZEN, "move": consult.RESUME,
             "reasoning": "it was mid-review"},
            {"act": "consult_outcome", "num": 5, "ts": 1.7e9 + 60, "outcome": "ok",
             "outcome_kind": "escalated", "handback": consult.PARK, "reasoning": "never discussed"},
            {"act": "consult_outcome", "num": 5, "ts": 1.7e9 + 90, "outcome": "ok",
             "outcome_kind": "fell_back", "handback": consult.AT_DIALOG,
             "reason": consult.NO_STAMP}]
    lines = consult.history_lines(recs, 5)
    assert len(lines) == 3
    assert "frozen" in lines[0] and "resume" in lines[0].lower()
    assert "park" in lines[1] and "never discussed" in lines[1]
    assert "dialog" in lines[2]


# --------------------------------------------------------------------------- the standing rule

def test_the_standing_rule_covers_the_handbacks():
    rule = _RULE.read_text()
    for must in ("park", "bounce", "frozen", "dialog", "blocked", "RELAUNCH", "RESUME",
                 "END_AND_RELAUNCH", "WAIT", "FOLLOW_UP", "ESCALATE", "bright line",
                 "watchdog", "handback_cap"):
        assert must in rule, must
    for never in ("hand-merge", "force-push", "agent-ready", "pre-authorized:", "publish",
                  ".superlooper/**", "keystrokes"):
        assert never in rule, never
