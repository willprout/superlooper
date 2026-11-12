"""The runner's half of the handback consult (issue #539): what it reads into the view, how it
launches the fork, and how it carries out each move with the engine's existing verbs."""
import json

import pytest

import approval as approval_mod
import consult as consult_mod
import journal
from test_runner import (NOW, _seed_gh_comments, issue_state, mutations, rig,  # noqa: F401
                         seed_issue)

_SESSION = "1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c"
_HB = {"memo": "frozen and already relaunched 2 times (cap 2) — parking", "needs_william": False,
       "cause": "frozen_cap", "bright_line": False}
_KEY = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")


def _stamp(rig):
    return {"session": _SESSION, "host": "Williams-Mini", "cwd": str(rig.repo)}


def _consultable(rig, transcript=True, host="Williams-Mini"):
    rig.r._hostname = lambda: host
    rig.r._transcript_exists = lambda sid: transcript and sid == _SESSION


def _approver(rig, session=_SESSION, host="Williams-Mini", ok=True):
    return {"ok": ok, "session": session, "host": host, "cwd": str(rig.repo)}


# =============================== the view: can this issue's handback be consulted ===============================

def test_the_view_finds_an_eligible_approver_without_reading_github(rig, monkeypatch):
    seed_issue(rig, "i5", status="frozen", approver=_approver(rig))
    _consultable(rig)
    monkeypatch.setenv("GH_FAIL", "1")             # nothing here may need GitHub
    check = rig.r.disk_view(NOW)["handback_checks"]["i5"]
    assert check == {"state": "eligible", "session": _SESSION, "host": "Williams-Mini",
                     "cwd": str(rig.repo)}


@pytest.mark.parametrize("appr,transcript,reason", [
    (dict(session=None), True, consult_mod.NO_STAMP),
    (dict(host="Some-Other-Mac"), True, consult_mod.OTHER_MACHINE),
    (dict(), False, consult_mod.NO_TRANSCRIPT),
])
def test_the_view_names_why_a_handback_cannot_be_consulted(rig, appr, transcript, reason):
    seed_issue(rig, "i5", status="running", approver=_approver(rig, **appr))
    _consultable(rig, transcript=transcript)
    check = rig.r.disk_view(NOW)["handback_checks"]["i5"]
    assert check["state"] == "fallback" and check["reason"] == reason


def test_an_issue_the_view_cannot_vouch_for_has_no_check(rig, monkeypatch):
    """No approver on record, and GitHub refusing the one backfill read: absent — today's handback."""
    seed_issue(rig, "i5", status="running")
    _consultable(rig)
    monkeypatch.setenv("GH_FAIL", "1")
    assert "i5" not in rig.r.disk_view(NOW)["handback_checks"]
    assert issue_state(rig, "i5").get("approver") is None, "a refused read is never NO_STAMP"


def test_an_in_flight_lane_launched_before_539_is_backfilled_once(rig):
    seed_issue(rig, "i5", status="running")
    body = approval_mod.audit_comment("William", "2026-11-06",
                                      approval_mod.Stamp(_SESSION, "Williams-Mini", str(rig.repo)))
    _seed_gh_comments(rig, 5, [body])
    _consultable(rig)
    assert rig.r.disk_view(NOW)["handback_checks"]["i5"]["state"] == "eligible"
    assert issue_state(rig, "i5")["approver"]["session"] == _SESSION


def test_a_finished_issue_is_never_checked(rig):
    seed_issue(rig, "i5", status="merged", approver=_approver(rig))
    seed_issue(rig, "i6", status="parked", approver=_approver(rig))
    _consultable(rig)
    assert rig.r.disk_view(NOW)["handback_checks"] == {}


def test_a_launch_records_its_approver_from_the_comments_it_already_read(rig):
    body = approval_mod.audit_comment("William", "2026-11-06",
                                      approval_mod.Stamp(_SESSION, "Williams-Mini", str(rig.repo)))
    _seed_gh_comments(rig, 5, [body])
    rig.r._parsed_by_id["i5"] = {"num": 5, "id": "i5", "title": "t", "type": "build",
                                 "labels": ["agent-ready", "type:build"], "touches": []}
    rig.r._raw_by_id["i5"] = {"body": "## Goal\nx"}
    (rig.home / "briefs").mkdir(exist_ok=True)
    assert rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x"}, NOW) == "ok"
    reads = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert reads == []
    assert issue_state(rig, "i5")["approver"] == {"ok": True, "session": _SESSION,
                                                   "host": "Williams-Mini",
                                                   "cwd": str(rig.repo)}


# ======================================== launching the fork ========================================

def _start(rig, **over):
    a = {"act": "handback_consult", "id": "i5", "num": 5, "kind": consult_mod.PARK, "key": _KEY,
         "handback": dict(_HB), "number": 1, "cap": 3, "stamp": _stamp(rig)}
    a.update(over)
    return rig.r._execute(a, NOW)


def test_a_handback_consult_forks_the_approver_and_leaves_the_lane_alone(rig):
    seed_issue(rig, "i5", status="running", branch="sl/i5-x", retries=2)
    (rig.home / "state" / "panes" / "i5").write_text("w1:p1")
    _seed_gh_comments(rig, 5, ["Approved by William in conversation, 2026-11-06."])
    journal.append(str(rig.home), {"act": "consult_outcome", "id": "i5", "num": 5, "outcome": "ok",
                                   "outcome_kind": "moved", "handback": consult_mod.FROZEN,
                                   "move": consult_mod.RESUME, "reasoning": "an earlier freeze"},
                   NOW - 9000)
    assert _start(rig) == "ok"
    (call,) = [c for c in rig.calls if "--consult" in c["args"]]
    assert call["args"][-2:] == ["--consult", "c5"]
    assert call["env"]["SL_FORK_PARENT"] == _SESSION
    assert call["env"]["SL_CONSULT_HOME"] == str(rig.repo)
    brief = (rig.home / "briefs" / "c5.md").read_text()
    assert "frozen and already relaunched 2 times" in brief
    assert "an earlier freeze" in brief, "the whole handback history rides in the packet"
    assert "MOVE: END_AND_RELAUNCH" in brief and str(rig.home / "reports" / "c5.md") in brief
    st = issue_state(rig, "i5")
    assert st["status"] == "running", "the status is untouched: the lane holds"
    assert st["handback"]["state"] == "running" and st["handback"]["id"] == "c5"
    assert st["handback"]["key"] == _KEY and st["handback"]["started_at"] == NOW
    assert st["handback_consults"] == 1
    assert (rig.home / "state" / "panes" / "i5").exists(), "the worker's window is not touched"
    assert [m for m in mutations(rig) if m["kind"] in ("comment", "set_labels")] == [], \
        "nothing is posted and no label moves until the consult decides"


def test_a_handback_consult_that_fails_to_launch_falls_back_with_the_reason(rig):
    seed_issue(rig, "i5", status="running")
    rig.rc_queue.append(2)
    _start(rig)
    rec = issue_state(rig, "i5")["handback"]
    assert rec["state"] == "fallback" and rec["reason"] == consult_mod.LAUNCH_FAILED
    assert "rc=2" in rec["detail"]


# ======================================== carrying out the moves ========================================

def _pending(rig, status="frozen", kind=consult_mod.PARK, key=_KEY, rec_state="running", **fields):
    seed_issue(rig, "i5", status=status, branch="sl/i5-x", retries=2, launch_failures=3,
               sensed_state="at_dialog", sensed_since=NOW - 4000, sensed_at=NOW - 10,
               handback={"key": key, "kind": kind, "state": rec_state, "id": "c5",
                         "session": _SESSION, "started_at": NOW - 60, "handback": dict(_HB)},
               handback_consults=1, **fields)
    (rig.home / "reports").mkdir(exist_ok=True)
    (rig.home / "reports" / "c5.md").write_text("MOVE: X\nEND\n")
    (rig.home / "state" / "panes" / "c5").write_text("w2:p1")
    (rig.home / "state" / "panes" / "i5").write_text("w1:p1")
    (rig.home / "state" / "panes" / "i5.ws").write_text("WS-5")
    for sub in ("blocked", "exited", "awaiting"):
        (rig.home / "state" / sub).mkdir(exist_ok=True)
        (rig.home / "state" / sub / "i5").write_text("x")
    (rig.home / "reports" / "i5.md").write_text("## Tests\nold")


def _move(rig, move, **over):
    a = {"act": "handback_move", "id": "i5", "num": 5, "kind": consult_mod.PARK, "key": _KEY,
         "move": move, "clarification": "", "reasoning": "ruled 11-04", "minutes": None,
         "title": "", "body": "", "issue_type": ""}
    a.update(over)
    return rig.r._execute(a, NOW)


def _consult_ended(rig):
    return (not (rig.home / "reports" / "c5.md").exists()
            and not (rig.home / "state" / "panes" / "c5").exists())


def _outcome_record(rig):
    (rec,) = [r for r in journal.read(str(rig.home)) if r.get("act") == "consult_outcome"]
    return rec


def _no_agent_ready(rig):
    for m in mutations(rig):
        assert "agent-ready" not in str(m.get("add") or ""), m
        assert "pre-authorized" not in str(m.get("add") or ""), m


def test_relaunch_requeues_a_fresh_session_with_its_clarification(rig):
    _pending(rig)
    assert _move(rig, consult_mod.RELAUNCH, clarification="Use approach A.") == "ok"
    (c,) = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert c["body"].startswith(consult_mod.MARKER) and "Use approach A." in c["body"]
    st = issue_state(rig, "i5")
    assert st["status"] == "ready" and st["consult_relaunch"] is True
    assert st["requeue_front"] is True and not st.get("consult_resume")
    assert st["launch_failures"] == 0, "a relaunch gets real launch attempts"
    assert st["consult_notes"][-1]["note"] == "Use approach A."
    assert st["consult_notes"][-1]["session"] == _SESSION
    assert st["handback"]["state"] == "moved" and st["handback"]["move"] == consult_mod.RELAUNCH
    assert st["sensed_state"] is None, "the old session's screen reading ends with it"
    for sub in ("blocked", "exited", "awaiting"):
        assert not (rig.home / "state" / sub / "i5").exists(), sub
    assert not (rig.home / "reports" / "i5.md").exists(), "a fresh session files a fresh report"
    assert not (rig.home / "state" / "panes" / "i5").exists(), "never two agents on one lane"
    assert _consult_ended(rig)
    rec = _outcome_record(rig)
    assert rec["outcome_kind"] == "moved" and rec["move"] == consult_mod.RELAUNCH
    assert rec["handback"] == consult_mod.PARK
    _no_agent_ready(rig)


def test_a_relaunched_gate_park_is_judged_on_a_fresh_gate_episode(rig):
    _pending(rig, status="gating", checks_pending_since=NOW - 99999, merge_refusals=3,
             nudged=["sections"], nudged_at={"sections": NOW - 50}, recheck_failed=True)
    _move(rig, consult_mod.RELAUNCH)
    st = issue_state(rig, "i5")
    assert st["checks_pending_since"] is None and st["merge_refusals"] == 0
    assert st["nudged"] == [] and st["nudged_at"] == {} and st["recheck_failed"] is False


def test_end_and_relaunch_ends_the_stuck_session_and_requeues(rig):
    _pending(rig)
    assert _move(rig, consult_mod.END_RELAUNCH) == "ok"
    assert any(c["args"][:2] == ["session-host", "end-session"] and "WS-5" in c["args"]
               for c in rig.calls), "the stuck session is ended through the doorway"
    st = issue_state(rig, "i5")
    assert st["status"] == "ready" and st["consult_relaunch"] is True
    assert st["handback"]["move"] == consult_mod.END_RELAUNCH
    _no_agent_ready(rig)


def test_resume_requeues_the_interrupted_conversation(rig):
    _pending(rig)
    assert _move(rig, consult_mod.RESUME, clarification="Finish the review step.") == "ok"
    st = issue_state(rig, "i5")
    assert st["status"] == "ready" and st["consult_resume"] is True
    assert st["consult_resume_note"] == "Finish the review step."


def _launchable(rig):
    rig.r._parsed_by_id["i5"] = {"num": 5, "id": "i5", "title": "t", "type": "build",
                                 "labels": ["in-progress", "type:build"], "touches": []}
    rig.r._raw_by_id["i5"] = {"body": "## Goal\nx"}
    (rig.home / "briefs").mkdir(exist_ok=True)


def test_a_resume_is_launched_through_superlooper_resume_unattended(rig):
    seed_issue(rig, "i5", status="ready", consult_relaunch=True, consult_resume=True,
               consult_resume_note="Finish the review step.", branch="sl/i5-x")
    _launchable(rig)
    assert rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x"}, NOW) == "ok"
    (call,) = [c for c in rig.calls if "resume" in c["args"]]
    assert call["args"][0].endswith("/superlooper") and call["args"][1] == "resume"
    assert "i5" in call["args"] and "--unattended" in call["args"]
    assert call["args"][call["args"].index("--note") + 1].endswith("Finish the review step.")
    assert call["args"][call["args"].index("--repo") + 1] == str(rig.repo)
    st = issue_state(rig, "i5")
    assert st["status"] == "running"
    assert not st["consult_relaunch"] and not st["consult_resume"]


def test_a_resume_that_cannot_re_enter_relaunches_cold_next(rig):
    seed_issue(rig, "i5", status="ready", consult_relaunch=True, consult_resume=True,
               branch="sl/i5-x")
    _launchable(rig)
    rig.rc_queue.append(1)
    out = rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x"}, NOW)
    assert "resume" in str(out)
    st = issue_state(rig, "i5")
    assert st["status"] == "ready" and st["consult_relaunch"] is True
    assert not st["consult_resume"], "the next launch is a cold one, with the clarification"


def test_wait_holds_the_handback_for_its_bounded_minutes(rig):
    _pending(rig)
    assert _move(rig, consult_mod.WAIT, minutes=30, reasoning="CI is slow") == "ok"
    st = issue_state(rig, "i5")
    assert st["status"] == "frozen", "a wait changes nothing but the hold"
    assert st["handback"]["state"] == "waiting" and st["handback"]["until"] == NOW + 1800
    assert st["handback"]["reason"] == "CI is slow"
    assert [m for m in mutations(rig) if m["kind"] in ("comment", "set_labels")] == []
    assert _consult_ended(rig)
    rec = _outcome_record(rig)
    assert rec["move"] == consult_mod.WAIT and rec["minutes"] == 30


def test_a_wait_past_its_bound_is_clamped(rig):
    _pending(rig)
    _move(rig, consult_mod.WAIT, minutes=10_000)
    assert issue_state(rig, "i5")["handback"]["until"] == NOW + consult_mod.WAIT_MAX_MINUTES * 60


def test_follow_up_files_an_unapproved_issue_and_hands_the_park_over(rig):
    _pending(rig, declared_touches=["engine"])
    assert _move(rig, consult_mod.FOLLOW_UP, title="Fix the flaky review step",
                 issue_type="type:diagnose-and-fix", body="## Goal\nMake it stop.") == "ok"
    (c,) = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    labels = c["labels"].split(",")
    assert set(labels) == {"needs-owner", "type:diagnose-and-fix", consult_mod.FOLLOW_UP_SOURCE}
    assert "## Loop metadata\ntouches: engine" in c["body"] and "#5" in c["body"]
    rec = issue_state(rig, "i5")["handback"]
    assert rec["state"] == "escalated" and rec["via"]["follow_up"]
    assert rec["via"]["move"] == consult_mod.FOLLOW_UP
    assert _consult_ended(rig)
    _no_agent_ready(rig)


def test_a_follow_up_github_refused_still_reaches_the_owner_with_the_proposal(rig, monkeypatch):
    _pending(rig)
    monkeypatch.setenv("GH_FAIL", "1")
    _move(rig, consult_mod.FOLLOW_UP, title="T", body="B", issue_type="type:build")
    rec = issue_state(rig, "i5")["handback"]
    assert rec["state"] == "escalated" and rec["via"]["follow_up"] is None
    assert rec["via"]["title"] == "T" and rec["via"]["body"] == "B"


@pytest.mark.parametrize("move", ["hand_merge", "force_push", "agent_ready", "pre_authorize",
                                  "publish", "edit_superlooper", "send_keys", "escalate", None])
def test_the_executor_refuses_anything_outside_the_set(rig, move):
    """Defence in depth under decide: a move outside the set never reaches a verb."""
    _pending(rig)
    out = _move(rig, move)
    assert str(out).startswith("refused")
    assert mutations(rig) == [] and rig.calls == []
    assert issue_state(rig, "i5")["handback"]["state"] == "running"


def test_a_move_with_no_consult_pending_does_nothing(rig):
    seed_issue(rig, "i5", status="frozen")
    assert "skipped" in str(_move(rig, consult_mod.RELAUNCH))
    assert mutations(rig) == []


# ======================================== handing it to the owner ========================================

def _park(rig, via, **over):
    a = {"act": "park", "id": "i5", "num": 5, "needs_william": False, "memo": _HB["memo"],
         "cause": "frozen_cap", "consult": dict(via, kind=consult_mod.PARK, key=_KEY)}
    a.update(over)
    return rig.r._execute(a, NOW)


def test_an_escalated_park_posts_the_marked_note_then_parks_as_today(rig):
    _pending(rig)
    _park(rig, {"outcome": "escalated", "reasoning": "never discussed",
                "recommendation": "re-scope it", "session": _SESSION})
    comments = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert comments[0]["body"].startswith(consult_mod.MARKER)
    assert "never discussed" in comments[0]["body"] and "re-scope it" in comments[0]["body"]
    assert comments[1]["body"].startswith("**superlooper parked this issue**"), \
        "the park's own memo is today's, byte for byte"
    labels = [m for m in mutations(rig) if m["kind"] == "set_labels"]
    assert labels and "parked" in str(labels[-1])
    st = issue_state(rig, "i5")
    assert st["status"] == "parked"
    assert st["handback"]["state"] == "delivered" and st["handback"]["via"]["outcome"] == "escalated"
    assert _consult_ended(rig)
    assert _outcome_record(rig)["outcome_kind"] == "escalated"


def test_a_park_with_no_consult_is_todays_park_exactly(rig):
    seed_issue(rig, "i5", status="frozen")
    _park(rig, {"outcome": "fell_back", "reason": consult_mod.NO_STAMP, "detail": ""})
    comments = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert len(comments) == 1 and comments[0]["body"].startswith("**superlooper parked")
    assert issue_state(rig, "i5").get("handback") is None
    assert [r for r in journal.read(str(rig.home)) if r.get("act") == "consult_outcome"] == []


def test_the_cap_attaches_the_consult_history(rig):
    seed_issue(rig, "i5", status="frozen")
    journal.append(str(rig.home), {"act": "consult_outcome", "id": "i5", "num": 5, "outcome": "ok",
                                   "outcome_kind": "moved", "handback": consult_mod.FROZEN,
                                   "move": consult_mod.RESUME, "reasoning": "mid-review"},
                   NOW - 9000)
    _park(rig, {"outcome": "fell_back", "reason": consult_mod.HANDBACK_CAP, "consults": 2,
                "cap": 3})
    note = [m for m in mutations(rig) if m["kind"] == "comment"][0]["body"]
    assert note.startswith(consult_mod.MARKER) and "mid-review" in note
    assert issue_state(rig, "i5")["handback"]["state"] == "delivered"


def test_a_bounce_carries_its_note(rig):
    _pending(rig, kind=consult_mod.BOUNCE)
    rig.r._execute({"act": "bounce", "id": "i5", "num": 5, "memo": "BOUNCED: gone", "cause": "bounce",
                    "consult": {"outcome": "escalated", "reasoning": "r", "session": _SESSION,
                                "kind": consult_mod.BOUNCE, "key": _KEY}}, NOW)
    comments = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert comments[0]["body"].startswith(consult_mod.MARKER)
    assert comments[1]["body"].startswith("**Bounced by the worker session**")
    assert issue_state(rig, "i5")["status"] == "bounced"


@pytest.mark.parametrize("kind", [consult_mod.FROZEN, consult_mod.AT_DIALOG,
                                  consult_mod.HOST_BLOCKED])
def test_an_escalated_lane_handback_posts_its_note_and_is_delivered(rig, kind):
    _pending(rig, kind=kind, key="%s:1" % kind)
    rig.r._execute({"act": "handback_escalate", "id": "i5", "num": 5, "kind": kind,
                    "key": "%s:1" % kind,
                    "consult": {"outcome": "escalated", "reasoning": "never discussed",
                                "session": _SESSION, "kind": kind, "key": "%s:1" % kind}}, NOW)
    (c,) = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert c["body"].startswith(consult_mod.MARKER) and "never discussed" in c["body"]
    st = issue_state(rig, "i5")
    assert st["status"] == "frozen", "today's lane handling carries on from here"
    assert st["handback"]["state"] == "delivered"
    assert _consult_ended(rig)


def test_a_lane_consult_that_never_decided_is_delivered_silently(rig):
    _pending(rig, kind=consult_mod.FROZEN, key="frozen:1")
    rig.r._execute({"act": "handback_escalate", "id": "i5", "num": 5, "kind": consult_mod.FROZEN,
                    "key": "frozen:1",
                    "consult": {"outcome": "fell_back", "reason": consult_mod.TIMEOUT,
                                "session": _SESSION, "kind": consult_mod.FROZEN,
                                "key": "frozen:1"}}, NOW)
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []
    assert issue_state(rig, "i5")["handback"]["state"] == "delivered"
    assert _outcome_record(rig)["outcome_kind"] == "fell_back"


# ======================================== the owner's own acts ========================================

def test_an_owner_close_mid_consult_ends_the_fork(rig):
    _pending(rig)
    rig.r._execute({"act": "absorb_close", "id": "i5", "num": 5}, NOW)
    assert not (rig.home / "state" / "panes" / "c5").exists()


def test_a_fresh_approval_resets_the_handback_cap_and_record(rig):
    _pending(rig, status="parked", consult_notes=[{"note": "x"}], consult_resume=True)
    rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)
    st = issue_state(rig, "i5")
    assert st["handback_consults"] == 0 and st["handback"] is None
    assert st["consult_notes"] == [] and not st["consult_resume"]


def test_a_freeze_stamps_when_its_episode_began(rig):
    seed_issue(rig, "i5", status="running")
    (rig.home / "state" / "panes" / "i5").write_text("w1:p1")
    rig.r._worker_liveness = lambda iid: "alive"
    rig.r._execute({"act": "recover", "id": "i5", "tier": "frozen"}, NOW)
    assert issue_state(rig, "i5")["frozen_since"] == NOW
    rig.r._execute({"act": "recover", "id": "i5", "tier": "frozen"}, NOW + 700)
    assert issue_state(rig, "i5")["frozen_since"] == NOW, "one freeze, one episode"


# ============================== fresh-review round 1 ==============================

def test_an_owner_close_mid_consult_drops_the_consult_record(rig):
    _pending(rig)
    rig.r._execute({"act": "absorb_close", "id": "i5", "num": 5}, NOW)
    assert issue_state(rig, "i5")["handback"]["state"] == "dropped"


def test_a_re_approval_that_resumes_at_the_gate_is_a_fresh_handback_episode(rig):
    _pending(rig, status="parked")
    seed_issue(rig, "i5", handback={"key": _KEY, "state": "delivered"}, handback_consults=2)
    rig.r._execute({"act": "resume_at_gate", "id": "i5", "num": 5}, NOW)
    st = issue_state(rig, "i5")
    assert st["handback"] is None and st["handback_consults"] == 0


def test_the_resume_note_is_never_rendered_as_the_owners_instruction(rig):
    seed_issue(rig, "i5", status="ready", consult_relaunch=True, consult_resume=True,
               consult_resume_note="Finish the review step.", branch="sl/i5-x")
    _launchable(rig)
    rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x"}, NOW)
    (call,) = [c for c in rig.calls if "resume" in c["args"]]
    note = call["args"][call["args"].index("--note") + 1]
    assert note.endswith("Finish the review step.")
    assert "approving session" in note and "not" in note.lower()


def test_a_canary_launch_still_resumes(rig):
    seed_issue(rig, "i5", status="ready", consult_relaunch=True, consult_resume=True,
               branch="sl/i5-x")
    _launchable(rig)
    rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x", "canary": True},
                   NOW)
    assert [c for c in rig.calls if "resume" in c["args"]]
    assert not issue_state(rig, "i5")["consult_resume"]


def test_a_follow_up_is_never_filed_twice(rig):
    _pending(rig)
    seed_issue(rig, "i5", handback={**issue_state(rig, "i5")["handback"],
                                    "follow_up_attempted": True})
    _move(rig, consult_mod.FOLLOW_UP, title="T", body="B", issue_type="type:build")
    assert [m for m in mutations(rig) if m["kind"] == "create_issue"] == []
    assert issue_state(rig, "i5")["handback"]["state"] == "escalated"


def test_a_new_consult_announces_its_own_move(rig):
    seed_issue(rig, "i5", status="running", handback_move_posted=True)
    _start(rig)
    assert issue_state(rig, "i5")["handback_move_posted"] is False


# ============================ #549: a fork left on a terminal issue ============================

@pytest.mark.parametrize("act", ["reapprove", "resume_at_gate"])
@pytest.mark.parametrize("state", ["running", "fallback", "escalated"])
def test_a_re_approval_ends_the_fork_a_pending_record_names_before_clearing_it(rig, act, state):
    """#549 edge 3: a crash window can leave a PENDING handback record on a terminal issue, and the
    overlay never acts on a terminal issue. The owner's re-approval clears the record, so it must
    end the fork the record names first — otherwise nothing ever will."""
    _pending(rig, status="parked", rec_state=state)
    ended = []
    real_end = rig.r._end_consult
    rig.r._end_consult = lambda cid: (ended.append((cid, issue_state(rig, "i5")["handback"])),
                                      real_end(cid))
    rig.r._execute({"act": act, "id": "i5", "num": 5}, NOW)
    assert issue_state(rig, "i5")["handback"] is None
    assert [cid for cid, _ in ended] == ["c5"]
    assert ended[0][1] is not None and ended[0][1]["state"] == state, \
        "the fork is ended BEFORE its record is cleared"
    assert _consult_ended(rig)


@pytest.mark.parametrize("act", ["reapprove", "resume_at_gate"])
@pytest.mark.parametrize("state", ["delivered", "waiting", "moved", "dropped"])
def test_a_re_approval_leaves_a_settled_records_consult_id_alone(rig, act, state):
    """A settled record's fork was ended when it settled, and `c<N>` is the issue's one consult id —
    a question consult (#538) uses it too — so only a PENDING record's names a fork to end."""
    _pending(rig, status="parked", rec_state=state)
    ended = []
    rig.r._end_consult = ended.append
    rig.r._execute({"act": act, "id": "i5", "num": 5}, NOW)
    assert ended == [] and issue_state(rig, "i5")["handback"] is None
