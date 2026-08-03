"""lib/reorient.py — the preamble a REVIVED session reads before anything else (issue #298).

The rule this module exists to keep (docs/HERDR-ADOPTION-PLAN.md §4): *a revived session
remembers the conversation, not the world.* `claude --resume` restores the transcript perfectly
and tells the session nothing about what happened to the repo, the PR or the branch while it was
dead — so the resumed session's confident memory of "I just pushed" is exactly the kind of belief
that merges the wrong thing. Every fact here is re-read at revive time and LABELLED as such.

Pure-function tests: no git, no gh, no cmux (the suite's fail-closed rule). The CLI's own
collection of these facts is covered in test_resume_cli.py.
"""
import reorient


BASE = {
    "id": "i298",
    "session_id": "5ddf8f39-7ec2-4936-967f-9eca52d71a9d",
    "branch": "sl/i298-launch-floor",
    "worktree": "/run/worktrees/i298",
    "head": "9624520abcdef0123456789abcdef0123456789a",
    "dirty": 0,
    "pr": {},
    "pr_ok": True,
}


def _render(**over):
    facts = dict(BASE)
    facts.update(over)
    return reorient.render(facts)


def test_the_preamble_names_the_lane_and_the_session_it_re_enters():
    out = _render()
    assert "i298" in out
    assert "5ddf8f39-7ec2-4936-967f-9eca52d71a9d" in out
    assert "sl/i298-launch-floor" in out


def test_it_says_plainly_that_this_is_a_revival_not_a_fresh_start():
    # The session's transcript ends mid-flight with no marker that anything happened. Unless the
    # preamble SAYS so, the model reads its own last message as the present moment.
    out = _render().lower()
    assert "resum" in out or "reviv" in out
    assert "interrupt" in out


def test_the_world_facts_are_dated_to_the_revive_not_to_memory():
    out = _render(head="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert "deadbeefdeadbeef"[:12] in out or "deadbeef" in out
    # the whole point: these are stated as freshly read, not as things the session already knew.
    assert "re-read" in out.lower() or "as of" in out.lower()


def test_a_clean_tree_and_a_dirty_tree_read_differently():
    assert "clean" in _render(dirty=0).lower()
    dirty = _render(dirty=3)
    assert "3" in dirty and "clean" not in dirty.lower().split("uncommitted")[0][-40:]


def test_an_open_pr_is_named_with_its_number_and_state():
    out = _render(pr={"number": 313, "state": "OPEN"}, pr_ok=True)
    assert "313" in out
    assert "OPEN" in out or "open" in out


def test_a_clean_no_pr_answer_says_there_is_no_pr():
    out = _render(pr={}, pr_ok=True)
    assert "no pr" in out.lower() or "no open pr" in out.lower()


def test_a_refused_pr_lookup_must_not_read_as_no_pr():
    # gh.pr_for_branch returns ok=False when GitHub REFUSED (timeout, no binary, unparseable). A
    # revived session told "there is no PR" would open a second one. Emptiness is not an answer.
    out = _render(pr={}, pr_ok=False)
    low = out.lower()
    assert "could not" in low or "did not answer" in low or "unknown" in low
    assert "there is no pr" not in low


def test_the_preamble_precedes_any_new_instruction():
    # The DoD's ordering requirement: re-orientation BEFORE new work. A note appended above the
    # facts would have the session acting on an instruction while still believing a stale world.
    out = _render(note="now rebase onto main and push")
    assert "now rebase onto main and push" in out
    assert out.index("5ddf8f39") < out.index("now rebase onto main and push")


def test_no_note_is_a_normal_revive_not_an_empty_instruction_block():
    out = _render()
    assert "{" not in out and "}" not in out, "an unsubstituted placeholder reached the session"
    assert out.strip()


def test_it_warns_that_an_in_flight_action_may_have_half_completed():
    # A kill -9 lands wherever it lands — mid-push, mid-write, mid-gh-call. The transcript shows
    # the tool CALL; it cannot show whether the effect landed. This is the single most dangerous
    # thing a resumed session can assume, so the preamble must name it.
    low = _render().lower()
    assert "half" in low or "may not have completed" in low or "mid-" in low


def test_unknown_facts_degrade_honestly_rather_than_inventing():
    out = _render(head=None, dirty=None, branch=None)
    low = out.lower()
    assert "unknown" in low or "could not" in low
    assert "none" not in low.replace("no one", ""), "a Python None leaked into the prose"
