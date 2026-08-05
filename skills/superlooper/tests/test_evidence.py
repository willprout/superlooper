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


def test_a_large_nudge_screen_keeps_the_verdict_line_the_rc_cannot_carry():
    """rc=3 maps to ONE reason for five screen verdicts (menu/trust/permission/quota/unknown); the
    only carrier of WHICH is the `state=` line nudge-pane.sh prints FIRST. bound() keeps the tail,
    so a big screen must not push the verdict off the front of the record — the i160 case: the
    ambiguous defer nobody could re-classify afterwards (fresh-review P1)."""
    composite = ("[nudge] i5 state=quota_blocked — Codex pane is usage/quota blocked — deferring\n"
                 "[nudge] i5 screen (bounded tail — what the verdict was read from):\n"
                 + "busy filler line of screen text\n" * 30)          # > SCREEN_SNIPPET_MAX
    assert len(composite) > evidence.SCREEN_SNIPPET_MAX
    rec = evidence.build("nudge", rc=3, captured=composite)
    assert "state=quota_blocked" in rec["captured"]                   # the verdict survived
    assert len(rec["captured"]) <= evidence.STDERR_TAIL_MAX + 1       # still bounded


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


# ---------------------------------------------------------------- channel vs per-issue (#153)
# A launch failure is one of two kinds, and they are charged in opposite ways: a DELIVERY-CHANNEL
# fault (the cmux anchor, the launch shim, the launch machinery) is a fault NO queued issue caused —
# it must hold the queue systemically and charge nobody — while a PER-ISSUE fault (a bad base branch,
# a git-level worktree failure, unusable issue state) parks the one issue that owns it. is_channel_fault
# reads the classified reason, so the runner can tell them apart on the FIRST failure instead of
# inferring "channel" only after a second distinct issue also fails (issue #153).

@pytest.mark.parametrize("rc", [1, 2, 124, 127, 64])
def test_bare_launch_rc_faults_are_channel_faults(rc):
    # With no captured text the reason comes straight from the rc table, and every rc-only launch
    # failure names the launch MACHINERY, never the session: rc=1 "nothing about the session itself
    # is at fault", rc=2 the shim, rc=124 a hung launcher, rc=127 an unrunnable script, rc=64 a
    # repo-wide wrong agent. All are channel-attributable.
    assert evidence.is_channel_fault(evidence.build("launch", rc=rc, captured=None)) is True


@pytest.mark.parametrize("needle,reason", [
    ("Error: not_found: Pane or workspace not found", "anchor_workspace_missing"),  # 07-09 storm
    ("cmux: broken pipe", "anchor_socket_lost"),                                     # lost socket
    # An rc=1 with text that matches no per-issue pattern falls to the generic rc=1 reason, which is
    # itself a channel reason (the launcher aborted before delivery — not the session's fault):
    ("the launcher gave up before any tab appeared", "launch_failed_before_delivery"),
])
def test_anchor_and_generic_rc1_text_faults_are_channel_faults(needle, reason):
    # The storm's own cause (anchor targeting a deleted workspace) and a lost cmux socket are the
    # canonical channel faults; a generic pre-delivery abort is too. All arrive as rc=1 and classify
    # as channel — the classifier reads the reason, and confirms which reason it resolved to here.
    rec = evidence.build("launch", rc=1, captured=needle)
    assert rec["reason"] == reason
    assert evidence.is_channel_fault(rec) is True


@pytest.mark.parametrize("captured,reason", [
    ("could not create the worktree for i5", "worktree_create_failed"),
    ("[i5] sanitize validation failed", "identity_invalid"),
    ("launch aborted: missing brief file", "brief_missing"),
])
def test_per_issue_text_faults_are_not_channel_faults(captured, reason):
    # These name THIS issue's own state (its worktree, its identity, its brief) — a fault the issue
    # owns and must park for. They arrive as rc=1 but the captured text refines them past the generic
    # channel reading, so they are NOT channel-attributable.
    rec = evidence.build("launch", rc=1, captured=captured)
    assert rec["reason"] == reason
    assert evidence.is_channel_fault(rec) is False


def test_base_missing_is_a_per_issue_fault():
    # A missing base branch (rc=3) is a per-repo CONFIG fault the issue parks for — never the channel.
    assert evidence.is_channel_fault(evidence.build("launch", rc=3, captured=None)) is False


@pytest.mark.parametrize("bad", [None, "", 17, [], {}, {"reason": None}, {"reason": 5},
                                 {"reason": "launch_rc_99"}])
def test_unrecognized_or_corrupt_records_fail_safe_to_per_issue(bad):
    # Fail SAFE for the queue: an unmapped rc (launch_rc_99) or a corrupt record is NOT treated as a
    # channel fault. A novel per-issue fault must never silently freeze the whole loop; only the KNOWN
    # channel reasons hold it. (The worse failure is a wrongly-held queue, not one wrongly-parked issue.)
    assert evidence.is_channel_fault(bad) is False


# ---- rc=6: the launch-floor env scrub could not clean this session's env (issue #301) -----------

def test_a_poisoned_env_is_its_own_reason_and_names_the_environment_not_the_shim():
    # Without the distinct code, a session that refuses itself over an inherited ANTHROPIC_API_KEY
    # reads as a generic non-delivery, and the park memo sends the owner to debug the launch shim
    # over a line in their ~/.zshrc — the mis-blame class this whole table exists to end.
    rec = evidence.build("launch", rc=6,
                         captured="[i5] ENV POISONED: the launch env scrub did not remove: "
                                  "ANTHROPIC_API_KEY")
    assert rec["reason"] == "env_poisoned"
    assert "environment" in rec["detail"].lower()
    assert "shim" not in rec["detail"].lower()


def test_the_park_memo_for_a_poisoned_env_names_the_remedy_and_the_stake():
    """A newcomer reading the memo at 3am must know both what happened and what to do. The stake is
    what makes this worth parking a lane over: an API-billed session and a session with no
    transcript both look completely normal from outside."""
    rec = evidence.build("launch", rc=6,
                         captured="[i5] ENV POISONED: the launch env scrub did not remove: "
                                  "ANTHROPIC_API_KEY")
    memo = evidence.park_memo(rec, attempts=2)
    assert "env_poisoned" in memo
    detail = rec["detail"].lower()
    assert "billing" in detail or "billed" in detail
    assert "export" in detail, "the memo must point at where the variable comes from"


def test_a_poisoned_env_is_a_per_issue_fault():
    # Same call as gh_auth_dead (rc=4) and base_missing (rc=3): an ENVIRONMENT fault whose memo the
    # owner must actually SEE. Routed to the channel it would hold the queue behind the
    # systemic-launch ALERT, whose body names App Nap and the cmux anchor — so an exported API key
    # would be reported to the owner as a cmux problem.
    assert evidence.is_channel_fault(evidence.build("launch", rc=6, captured=None)) is False


def test_a_poisoned_env_is_read_from_the_text_even_without_the_rc():
    # The rc can be lost (a wrapper, a timeout kill, a shell that only forwards its own status)
    # while the captured stderr survives — the same belt-and-suspenders every other launch fault
    # gets. Ordered so it can never be swallowed by a cmux needle.
    rec = evidence.build("launch", rc=1,
                         captured="[i5] ENV POISONED in the session's own environment — the flight "
                                  "was refused before it started.")
    assert rec["reason"] == "env_poisoned", rec
    assert evidence.is_channel_fault(rec) is False


def test_an_env_refusal_is_never_read_as_dead_github_auth():
    """The two self-refusals sit next to each other and must not be confused: `gh auth login` fixes
    nothing when the fault is an exported API key, and unsetting a variable fixes nothing when the
    credential really is dead. A poisoned env that ALSO mentions gh (XDG_CONFIG_HOME is exactly how
    gh dies) must still read as the environment — it is the causally upstream fault."""
    rec = evidence.build("launch", rc=6,
                         captured="[i5] ENV POISONED: the launch env scrub did not remove: "
                                  "XDG_CONFIG_HOME (which de-authenticates gh)")
    assert rec["reason"] == "env_poisoned", rec


# ---- rc=4: the positive gh-auth assert refused the flight (issue #299) --------------------------

def test_dead_gh_auth_is_its_own_reason_and_names_auth_not_the_shim():
    # The whole point of the distinct code: without it, an auth-death refusal reads as a generic
    # non-delivery and the park memo sends the reader to debug the launch shim — the wrong
    # component, exactly the 2026-07-09 mis-blame that _LAUNCH_TEXT was written to end.
    rec = evidence.build("launch", rc=4,
                         captured="[i5] GH AUTH DEAD: `gh api user` did not answer as 'loopbot'")
    assert rec["reason"] == "gh_auth_dead"
    assert "auth" in rec["detail"].lower()
    assert "shim" not in rec["detail"].lower()


def test_the_park_memo_for_dead_gh_auth_names_the_remedy():
    # A newcomer reading the memo at 3am must know what to type. `gh auth login` is the fix.
    rec = evidence.build("launch", rc=4, captured="[i5] GH AUTH DEAD: not logged in")
    memo = evidence.park_memo(rec, attempts=2)
    assert "gh auth login" in memo
    assert "gh_auth_dead" in memo


def test_dead_gh_auth_is_a_per_issue_fault():
    # Same call as base_missing (rc=3): an ENVIRONMENT fault whose memo the owner must actually see.
    # Routing it to the channel would hold the queue behind the systemic-launch ALERT, whose body
    # names App Nap and the cmux anchor — dead gh auth would then be reported as a cmux problem.
    assert evidence.is_channel_fault(evidence.build("launch", rc=4, captured=None)) is False


def test_a_dead_runner_env_is_a_channel_fault_that_holds_the_queue():
    """rc=5 — the RUNNER's own gh cannot authenticate. No tab was opened, every launch will fail
    identically, and no issue can fix it by re-approving. That is the definition of a channel
    fault (#153), and getting it wrong is the 2026-07-09 storm shape with a new cause: one
    machine-level fault walking the entire approved queue into per-issue parks."""
    rec = evidence.build("launch", rc=5,
                         captured="[i5] GH AUTH DEAD (runner env): `gh api user` did not return")
    assert rec["reason"] == "gh_auth_dead_runner"
    assert evidence.is_channel_fault(rec) is True
    assert "runner" in rec["detail"].lower()


def test_the_two_auth_refusals_are_charged_to_opposite_parties():
    # The pair is the point: same fault CLASS, opposite blast radius. A session whose own env is
    # broken parks that issue; a runner whose env is broken must never charge any issue at all.
    session = evidence.build("launch", rc=4, captured="[i5] GH AUTH DEAD: not logged in")
    runner = evidence.build("launch", rc=5, captured="[i5] GH AUTH DEAD (runner env): nope")
    assert evidence.is_channel_fault(session) is False
    assert evidence.is_channel_fault(runner) is True


@pytest.mark.parametrize("captured", [
    "[i5] GH AUTH DEAD: `gh api user` said: API rate limit exceeded for user",
    "[i5] GH AUTH DEAD: dial tcp: lookup api.github.com: no such host",
    "[i5] GH AUTH DEAD: no answer within 8s (gh did not return)",
    "[i5] GH AUTH DEAD: HTTP 503: Service Unavailable",
])
def test_github_not_answering_is_not_a_dead_credential(captured):
    """The assert cannot tell 'not authenticated' from 'GitHub did not answer', so the classifier
    must. Parking an issue under 'your auth is dead, run gh auth login' when the real fault is a
    rate limit or an outage is a confidently WRONG remedy — the mis-blame class this whole table
    exists to end. These hold the queue and resume on their own instead."""
    rec = evidence.build("launch", rc=4, captured=captured)
    assert rec["reason"] == "gh_probe_unreachable", rec
    assert evidence.is_channel_fault(rec) is True
    assert "login" not in rec["detail"].lower() or "fix nothing" in rec["detail"].lower()


def test_gh_error_text_cannot_be_read_as_a_dead_cmux_anchor():
    """The refusals relay gh's OWN words, and gh's wording is not ours to control. `_LAUNCH_TEXT` is
    consulted BEFORE the rc table, so an auth message containing a cmux-ish phrase would otherwise
    classify as anchor_socket_lost and raise a socket alert about a GitHub fault."""
    rec = evidence.build("launch", rc=4,
                         captured="[i5] GH AUTH DEAD: gh: could not connect to github.com")
    assert rec["reason"] != "anchor_socket_lost"
    assert rec["reason"] in ("gh_auth_dead", "gh_probe_unreachable"), rec


@pytest.mark.parametrize("captured,reason", [
    ("[i429] could not create the worktree at '/run/worktrees/i429' for branch 'sl/i429-x'",
     "worktree_create_failed"),
    ("[i429] missing brief /run/briefs/i429.md", "brief_missing"),
    ("[i429] issues.json load / sanitize validation failed — not launching", "identity_invalid"),
    ("[i429] new-surface failed: Pane or workspace not found", "anchor_workspace_missing"),
])
def test_an_issue_number_can_never_be_read_as_a_github_status_code(captured, reason):
    """THE bomb a bare "429" needle planted. Every launcher line is prefixed `[$ID]`, so on issue
    i429 (and i1429, i4290…) a substring match on "429" turned four unrelated launch faults into
    "GitHub is rate-limited" — a CHANNEL fault, which holds the whole queue on something that never
    self-heals, and which also masked anchor_workspace_missing, the 2026-07-09 storm's own reason.

    The needles that map to channel reasons must be impossible in our own launcher text."""
    rec = evidence.build("launch", rc=1, captured=captured)
    assert rec["reason"] == reason, rec
    assert rec["reason"] != "gh_probe_unreachable"


def test_the_id_prefix_never_swallows_a_shim_failure_either():
    rec = evidence.build("launch", rc=2,
                         captured="[i429] LAUNCH NOT DELIVERED: no worker started in tab 429e")
    assert rec["reason"] == "shim_not_fired"


def test_a_real_github_rate_limit_still_reads_as_unreachable():
    # The other side of the same coin: tightening the needles must not stop them matching what gh
    # actually prints.
    for text in ("[i5] GH AUTH DEAD: HTTP 429: You have exceeded a secondary rate limit",
                 "[i5] GH AUTH DEAD: API rate limit exceeded for user",
                 "[i5] GH AUTH DEAD: HTTP 503: Service Unavailable"):
        assert evidence.build("launch", rc=4, captured=text)["reason"] == "gh_probe_unreachable", text


def test_a_dead_cmux_socket_is_not_reported_as_an_unreachable_github():
    """`connection refused` reads as a network fault, but cmux's OWN socket error carries it too —
    and the gh needles are ordered ahead of anchor_socket_lost, so including it flipped a dead cmux
    socket to "wait for GitHub to come back": a remedy for a fault that never self-recovers. gh's Go
    error always spells the whole `dial tcp <ip>:443: connect: connection refused`, so dropping the
    bare phrase costs nothing."""
    dead_socket = ("[i7] new-surface failed (rc=1) targeting pane 'pane:1': "
                   "could not connect to cmux: connect: connection refused")
    assert evidence.build("launch", rc=1, captured=dead_socket)["reason"] == "anchor_socket_lost"
    # ...while gh's own refused connection is still caught, via `dial tcp`:
    gh_refused = "[i5] GH AUTH DEAD: dial tcp 140.82.113.6:443: connect: connection refused"
    assert evidence.build("launch", rc=4, captured=gh_refused)["reason"] == "gh_probe_unreachable"


# ---- the fence pre-flight (issue #326) ---------------------------------------------------------

def test_a_fence_refusal_is_a_channel_fault_that_holds_the_queue():
    """Machine-level, exactly like `gh_auth_dead_runner` and `claude_identity_wrong_runner`: every
    launch on this host reads the SAME control socket and gets the same verdict, so charging one
    issue a park for it would walk the whole approved queue into parks over a single machine fault
    — the 2026-07-09 storm's shape with a new cause. No re-approval can fix it; the fleet's server
    has to be rebuilt or restarted."""
    rec = evidence.build("launch", rc=9, captured=None)
    assert rec["reason"] == "fence_down"
    assert evidence.is_channel_fault(rec) is True


@pytest.mark.parametrize("captured", [
    "[i5] FENCE DOWN: a tokenless connection to /tmp/h.sock was SERVED",
    "[i5] FENCE DOWN: /tmp/h.sock did not answer, and silence is never proof of a fence",
])
def test_the_launchers_own_fence_words_classify_as_the_fence(captured):
    rec = evidence.build("launch", rc=9, captured=captured)
    assert rec["reason"] == "fence_down", rec


def test_a_fence_refusal_never_reads_as_a_github_outage_or_a_dead_anchor():
    """`_LAUNCH_TEXT` is consulted before the rc table and the first match wins, so the fence's own
    refusal text must contain none of the earlier needles — otherwise an unfenced fleet would be
    reported to the owner as "wait for GitHub to come back", a remedy for a fault that never
    self-recovers and that leaves the socket wide open in the meantime."""
    for captured in ("[i5] FENCE DOWN: a tokenless connection to /tmp/h.sock was SERVED",
                     "[i5] FENCE DOWN: /tmp/h.sock did not answer, and silence is never proof of "
                     "a fence"):
        rec = evidence.build("launch", rc=9, captured=captured)
        assert rec["reason"] not in ("gh_probe_unreachable", "anchor_socket_lost",
                                     "anchor_workspace_missing", "env_poisoned"), rec


@pytest.mark.parametrize("hostile", [
    "/tmp/env poisoned.sock",                  # the needle that leads the whole table
    "/tmp/dial tcp/h.sock",                    # a gh-transient needle -> would HOLD for GitHub
    "/tmp/gh auth dead/h.sock",                # -> would tell the owner to re-login
    "/tmp/not_found/h.sock",                   # -> would raise a cmux anchor alert
    "/tmp/could not connect/h.sock",           # -> anchor_socket_lost
    "/tmp/http 429/h.sock",                    # -> "wait for GitHub to come back"
])
def test_an_environment_chosen_socket_path_cannot_change_what_a_fence_refusal_MEANS(hostile):
    """The `[i429]` lesson, one interpolation over.

    The fence memo NAMES the socket it probed and the switch value it could not read — and both are
    strings this engine does not choose: an operator picks the path, and the switch may hold
    anything at all. `_LAUNCH_TEXT` is consulted BEFORE the rc table and the first match wins, so a
    socket path containing another reason's needle would relabel a machine-level fence failure as
    that other reason. Half of those are PER-ISSUE, which is the 2026-07-09 shape exactly: the
    queue walks into parks over one machine-level fault, each memo naming something the issue did
    not do.
    """
    captured = ("[i5] FENCE DOWN: a tokenless connection to %s was SERVED — the session host's "
                "control socket has no fence at all." % hostile)
    rec = evidence.build("launch", rc=9, captured=captured)
    assert rec["reason"] == "fence_down", rec
    assert evidence.is_channel_fault(rec) is True


def test_an_unreadable_switch_value_cannot_change_what_a_fence_refusal_means():
    captured = ("[i5] FENCE DOWN: a tokenless connection to /tmp/h.sock was SERVED.\n"
                "[i5] (SL_FLEET_FENCE is set to 'env poisoned', which this engine does not "
                "recognise — it reads as 'required'.)")
    rec = evidence.build("launch", rc=9, captured=captured)
    assert rec["reason"] == "fence_down", rec
    assert evidence.is_channel_fault(rec) is True


def test_the_fence_text_needle_still_covers_an_rc_this_engine_has_no_entry_for():
    """The needle's ONE remaining job, and the only window it fires in.

    rc=9 is classified by rc (`_RC_AUTHORITATIVE`), so this needle is not the fence's normal path.
    It covers publish drift: a merged launcher emits rc=9 while the INSTALLED engine judging it
    predates the rc table entry. Without the needle that reads as `launch_rc_<n>` — per-issue, with
    a memo that names no cause. With it the reason is right even though the rc means nothing here.
    """
    captured = "[i5] FENCE DOWN: a tokenless connection to /tmp/h.sock was SERVED"
    rec = evidence.build("launch", rc=97, captured=captured)
    assert rec["reason"] == "fence_down", rec
    assert evidence.is_channel_fault(rec) is True


def test_the_fence_needle_leads_the_table_so_a_later_needle_cannot_be_inserted_above_it():
    """Structural, not behavioural: the fallback above is only sound while nothing precedes it,
    because an interpolated socket path can contain any needle in this table."""
    first_needles = evidence._LAUNCH_TEXT[0][0]
    assert "fence down" in first_needles, evidence._LAUNCH_TEXT[0]
