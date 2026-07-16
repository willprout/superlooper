"""The evidence core (issue #152): every non-success outcome becomes a RECORD, never a bare code.

The 2026-07-09 launch storm is the case these tests encode. Ten issues parked under a memo asking
"is the launch shim installed?" while the real cause — a launch anchor pointing at a deleted cmux
workspace — sat in runner.log, read by nobody. The rc survived; the reason was thrown away.
"""
import pytest

import evidence


# ---------------------------------------------------------------- bound()

def test_bound_keeps_the_tail_because_the_error_is_at_the_end():
    # A failing command's last words name the cause; its first words are boilerplate.
    text = "boring preamble\n" * 500 + "Error: not_found"
    out = evidence.bound(text, limit=60)
    assert out.endswith("Error: not_found")
    assert len(out) <= 61                          # the ellipsis marker is the only slack


def test_bound_caps_size_so_a_memo_can_never_become_an_stderr_dump():
    assert len(evidence.bound("x" * 100_000, limit=100)) <= 101


def test_bound_strips_control_bytes_a_raw_binary_would_carry():
    # Incident 2026-07-07: a raw binary in a report wedged the runner. Captured text is
    # caller-controlled (a worker's screen, a tool's stderr), so it is never trusted verbatim.
    assert "\x00" not in evidence.bound("before\x00\x07\x1b[31mafter")
    assert "before" in evidence.bound("before\x00after")


def test_bound_keeps_newlines_and_tabs_because_a_stderr_tail_is_multi_line():
    assert "\n" in evidence.bound("line one\nline two")


@pytest.mark.parametrize("bad", [None, 17, [], {}, object()])
def test_bound_never_raises_on_wrong_typed_input(bad):
    # Fail-open on type: evidence formatting must never crash the tick it is describing.
    assert evidence.bound(bad) == ""


# ---------------------------------------------------------------- the schema: fail closed

def test_a_failure_record_always_carries_a_captured_field():
    rec = evidence.build("launch", rc=1, captured="Error: not_found")
    assert rec["captured"] == "Error: not_found"
    assert rec["rc"] == 1 and rec["kind"] == "launch"
    assert rec["reason"] and rec["detail"]


@pytest.mark.parametrize("nothing", [None, "", "   ", "\n\n"])
def test_an_evidence_free_record_fails_closed_to_an_honest_admission(nothing):
    """The DoD's fail-closed clause. When truly nothing was captured the field still EXISTS and
    says so — an absent field would read as 'nothing went wrong'."""
    rec = evidence.build("launch", rc=1, captured=nothing)
    assert rec["captured"] == evidence.CAPTURED_NONE
    assert "reason unknown" in rec["captured"]


def test_validate_rejects_a_record_with_no_evidence_field():
    # "an evidence-free failure record cannot be written" — the schema is enforced, not hoped for.
    with pytest.raises(ValueError):
        evidence.validate({"kind": "launch", "rc": 1, "reason": "x", "detail": "y"})


@pytest.mark.parametrize("rec", [None, "launch rc=1", 1, ["launch"]])
def test_validate_rejects_a_bare_code_masquerading_as_a_record(rec):
    with pytest.raises(ValueError):
        evidence.validate(rec)


def test_validate_rejects_a_blank_captured_field():
    with pytest.raises(ValueError):
        evidence.validate({"kind": "launch", "rc": 1, "reason": "x", "detail": "y", "captured": ""})


def test_build_output_always_survives_its_own_validator():
    for rc in (1, 2, 3, 124, 127, 99):
        evidence.validate(evidence.build("launch", rc=rc, captured=None))


def test_build_bounds_the_captured_text_itself():
    rec = evidence.build("launch", rc=1, captured="x" * 100_000)
    assert len(rec["captured"]) <= evidence.STDERR_TAIL_MAX + 1


# ---------------------------------------------------------------- launch classification

def test_the_storm_names_the_dead_anchor_and_never_the_shim():
    """THE case. cmux resolved no surface because the anchor's workspace was deleted; the launch
    never reached the shim at all, so blaming the shim sends the reader to the wrong component."""
    captured = ("[i5] could not parse a surface UUID from new-surface output: "
                "Error: not_found: Pane or workspace not found")
    rec = evidence.build("launch", rc=1, captured=captured)
    assert rec["reason"] == "anchor_workspace_missing"
    assert "workspace" in rec["detail"].lower()
    # The 07-09 memo's exact lie was sending the reader to DEBUG the shim. Naming the shim to
    # EXONERATE it ("never reached the shim") is the opposite and is welcome — so this bans the
    # misdirection, not the word.
    assert "is the shim installed" not in rec["detail"].lower()
    assert "install-launch-shim" not in rec["detail"]


def test_rc1_and_rc2_are_not_the_same_failure():
    """The distinction launch-session.sh already draws and the memo used to flatten: rc=1 never
    created a tab; rc=2 created one the shim never woke."""
    before = evidence.build("launch", rc=1, captured="[i5] new-surface failed (rc=1)")
    shim = evidence.build("launch", rc=2, captured="[i5] LAUNCH NOT DELIVERED: no worker started")
    assert before["reason"] != shim["reason"]
    assert "shim" in shim["detail"].lower()         # rc=2 is the ONE case the shim question fits
    assert "shim" not in before["detail"].lower()


def test_a_missing_base_branch_blames_the_branch_not_the_launcher():
    rec = evidence.build("launch", rc=3, captured="[i5] worktree base 'origin/dev' does not exist")
    assert rec["reason"] == "base_missing"
    assert "branch" in rec["detail"].lower()


def test_a_lost_cmux_socket_is_its_own_reason():
    rec = evidence.build("launch", rc=1, captured="Error: Broken pipe (could not connect)")
    assert rec["reason"] == "anchor_socket_lost"


def test_a_timeout_says_the_script_never_returned():
    rec = evidence.build("launch", rc=124, captured=None)
    assert rec["reason"] == "launch_timeout"


def test_an_unrunnable_script_is_distinct_from_a_failed_one():
    assert evidence.build("launch", rc=127, captured=None)["reason"] == "launch_script_unrunnable"


def test_an_unmapped_rc_is_recorded_honestly_rather_than_guessed():
    rec = evidence.build("launch", rc=42, captured="something new")
    assert rec["rc"] == 42
    assert rec["captured"] == "something new"       # the text still reaches the reader
    assert rec["reason"]                            # named, not blank


def test_stderr_evidence_outranks_the_rc_only_reading():
    """rc=1 covers several steps in launch-session.sh; only the captured text says which one."""
    generic = evidence.build("launch", rc=1, captured="[i5] missing brief /x/i5.md")
    assert generic["reason"] == "brief_missing"


# ---------------------------------------------------------------- nudge classification

def test_a_nudge_refusal_carries_the_classifier_verdict_not_just_rc3():
    """nudge rc=3 records used to carry no verdict and no screen — 43 minutes were lost to one."""
    rec = evidence.build("nudge", rc=3, captured="[nudge] i5 pane at a menu/ambiguous — deferring")
    assert rec["reason"] == "pane_deferred"
    assert "menu" in rec["captured"]


def test_a_dead_pane_nudge_is_distinct_from_a_deferral():
    assert evidence.build("nudge", rc=4, captured="[nudge] dead")["reason"] == "pane_dead"


def test_a_logged_out_pane_names_auth_and_not_a_freeze():
    rec = evidence.build("nudge", rc=5, captured="[nudge] i5 session is LOGGED OUT")
    assert rec["reason"] == "pane_logged_out"
    assert "auth" in rec["detail"].lower()


def test_a_session_asking_a_question_is_recorded_as_alive():
    rec = evidence.build("nudge", rc=6, captured="[nudge] i5 asking a question")
    assert rec["reason"] == "pane_at_dialog"
    assert "alive" in rec["detail"].lower()


def test_a_failed_send_is_not_a_refusal():
    assert evidence.build("nudge", rc=1, captured="[nudge] send failed")["reason"] == "send_failed"


# ---------------------------------------------------------------- the park memo

def test_the_park_memo_names_the_captured_diagnostic():
    """The DoD's headline: the storm memo must read 'deleted workspace', not 'is the shim
    installed?'."""
    rec = evidence.build("launch", rc=1, captured=(
        "[i5] could not parse a surface UUID from new-surface output: "
        "Error: not_found: Pane or workspace not found"))
    memo = evidence.park_memo(rec, attempts=3)
    assert "workspace" in memo.lower()
    assert "not_found" in memo                      # the captured diagnostic itself, verbatim
    assert "is the shim installed" not in memo.lower()      # the wrong-component directive
    assert "install-launch-shim" not in memo
    assert "3" in memo                              # the attempt count still survives


def test_the_park_memo_admits_when_it_captured_nothing():
    memo = evidence.park_memo(evidence.build("launch", rc=1, captured=None), attempts=3)
    assert evidence.CAPTURED_NONE in memo


@pytest.mark.parametrize("bad", [None, "", 3, [], {"kind": "launch"}])
def test_the_park_memo_never_raises_on_a_missing_or_corrupt_record(bad):
    # A memo is written on the worst tick of the run. It degrades; it never crashes the park.
    memo = evidence.park_memo(bad, attempts=3)
    assert isinstance(memo, str) and memo.strip()


def test_the_park_memo_is_bounded():
    rec = evidence.build("launch", rc=1, captured="x" * 100_000)
    assert len(evidence.park_memo(rec, attempts=3)) < 4000
