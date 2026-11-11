"""decide's handback consult routing (issue #539).

Every issue-level handback the command center shows the owner — a park whatever its cause, a bounce,
a frozen lane, a lane at a dialog, a lane the session host reports blocked — goes FIRST to a fork of
the session that approved the issue, when the runner found it can be asked (`handback_checks`, read
into the view exactly as #538's `consult_checks`). A view WITHOUT the key is today's behaviour, byte
for byte; a check that cannot consult is today's behaviour with the reason riding along."""
import pytest

import actions
import consult as consult_mod
from test_actions import (GOOD_REPORT, NOW, cfg, decide, disk, ghv, has_notify, ist, only,
                          parsed, pr_view, usage_ok)

_STAMP = {"session": "1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c", "host": "Williams-Mini",
          "cwd": "/Users/w/Projects/superlooper"}
_ELIGIBLE = {"state": "eligible", **_STAMP}
_IN_FLIGHT = ("in-progress", "type:build")


def _dsk(status="running", check=_ELIGIBLE, iid="i5", extra=None, **ist_over):
    over = {"issues_state": {"version": 1, "issues": {iid: ist(status, **ist_over)}}}
    if check is not None:
        over["handback_checks"] = {iid: check}
    over.update(extra or {})
    return disk(**over)


def _frozen_at_cap(check=_ELIGIBLE, **over):
    """A lane that froze with its relaunches spent: today it parks (frozen_cap) with a page."""
    return dict(events=[{"type": "frozen", "id": "i5"}],
                parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                dsk=_dsk("running", check=check, retries=2, **over))


def _bounced(check=_ELIGIBLE, **over):
    memo = "BOUNCED: the endpoint was removed in #44.\nProposed amendment: target /v2/widgets."
    return dict(parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                dsk=_dsk("running", check=check, extra={"blocked": {"i5": memo}}, **over)), memo


def _frozen_lane(check=_ELIGIBLE, since=NOW - actions.RECOVER_RETRY_SECONDS - 5, **over):
    """A lane LATCHED frozen whose first context-free nudge has had its ten minutes."""
    return dict(parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                dsk=_dsk("frozen", check=check, frozen_since=since,
                         last_recover_at=NOW - actions.RECOVER_RETRY_SECONDS - 5, **over))


def _at_dialog(check=_ELIGIBLE, **over):
    return dict(events=[{"type": "frozen", "id": "i5"}],
                parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                dsk=_dsk("frozen", check=check, sensed_state="at_dialog",
                         sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS - 1, **over))


def _host_blocked(check=_ELIGIBLE, **over):
    return dict(events=[{"type": "frozen", "id": "i5"}],
                parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                dsk=_dsk("frozen", check=check, sensed_state="host_blocked",
                         sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS, sensed_at=NOW, **over))


def _alert_reasons(out):
    return [r for a in only(out, "alert") for r in a.get("reasons", [])]


def _mine(out, act):
    return [a for a in only(out, act) if a.get("id") == "i5"]


# ===================== each kind launches a consult when the issue has a stamp =====================

def test_a_park_goes_to_the_approving_session_first():
    out = decide(**_frozen_at_cap())
    (c,) = only(out, "handback_consult")
    assert c["id"] == "i5" and c["num"] == 5 and c["kind"] == consult_mod.PARK
    assert c["handback"]["cause"] == "frozen_cap" and "frozen" in c["handback"]["memo"]
    assert c["stamp"] == _STAMP and c["number"] == 1
    assert c["key"] == consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    assert only(out, "park") == [] and not has_notify(out), "the owner hears nothing yet"


def test_a_park_of_another_cause_goes_first_too():
    """Whatever its cause: the two-question cap's park, a scoping problem."""
    dsk = _dsk("running", questions_asked=2, extra={"blocked": {"i5": "QUESTION: a third?"}})
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk)
    (c,) = only(out, "handback_consult")
    assert c["handback"]["cause"] == "question_cap" and c["handback"]["needs_william"] is True
    assert only(out, "park") == [] and only(out, "consult_start") == []


def test_a_bounce_goes_to_the_approving_session_first():
    kw, memo = _bounced()
    out = decide(**kw)
    (c,) = only(out, "handback_consult")
    assert c["kind"] == consult_mod.BOUNCE and c["handback"]["memo"] == memo
    assert only(out, "bounce") == [] and not has_notify(out)


def test_a_frozen_lane_goes_to_the_approving_session_after_its_first_nudge():
    out = decide(**_frozen_lane())
    (c,) = only(out, "handback_consult")
    assert c["kind"] == consult_mod.FROZEN
    assert c["key"] == consult_mod.handback_key(consult_mod.FROZEN,
                                                NOW - actions.RECOVER_RETRY_SECONDS - 5)
    assert _mine(out, "recover") == [], "the consult replaces the second context-free nudge"


def test_a_lane_that_just_froze_still_gets_its_one_context_free_nudge_first():
    out = decide(**_frozen_lane(since=NOW - 60))
    assert only(out, "handback_consult") == []
    assert len(_mine(out, "recover")) == 1


def test_a_lane_at_a_dialog_goes_to_the_approving_session_first():
    out = decide(**_at_dialog())
    (c,) = only(out, "handback_consult")
    assert c["kind"] == consult_mod.AT_DIALOG
    assert "session_at_dialog:i5" not in _alert_reasons(out)
    assert not has_notify(out)


def test_a_lane_the_host_reports_blocked_goes_to_the_approving_session_first():
    out = decide(**_host_blocked())
    (c,) = only(out, "handback_consult")
    assert c["kind"] == consult_mod.HOST_BLOCKED
    assert "session_host_blocked:i5" not in _alert_reasons(out)
    assert not has_notify(out)


# ========================= and behaves exactly as today when it cannot ==========================

def _strip(out):
    """The acts with any `consult` rider removed — what today's code emits."""
    return [{k: v for k, v in a.items() if k != "consult"} for a in out]


@pytest.mark.parametrize("make", [_frozen_at_cap, lambda check: _bounced(check)[0], _frozen_lane,
                                  _at_dialog, _host_blocked],
                         ids=["park", "bounce", "frozen", "at_dialog", "host_blocked"])
def test_no_check_in_the_view_is_todays_handback_exactly(make):
    today = decide(**make(check=None))
    assert only(today, "handback_consult") == []
    assert today == decide(**make(check=None)), "pure: same inputs, same acts"
    # a check that cannot consult changes nothing but the reason riding the act
    for reason in (consult_mod.NO_STAMP, consult_mod.OTHER_MACHINE, consult_mod.NO_TRANSCRIPT,
                   consult_mod.STAMP_UNREAD):
        out = decide(**make(check={"state": "fallback", "reason": reason, "detail": "d"}))
        assert only(out, "handback_consult") == []
        assert _strip(out) == today, reason


def test_todays_park_and_bounce_still_page():
    assert len(only(decide(**_frozen_at_cap(check=None)), "park")) == 1
    assert has_notify(decide(**_frozen_at_cap(check=None)))
    kw, _ = _bounced(check=None)
    assert len(only(decide(**kw), "bounce")) == 1 and has_notify(decide(**kw))


def test_a_fallback_check_names_its_reason_on_the_park():
    out = decide(**_frozen_at_cap(check={"state": "fallback", "reason": consult_mod.NO_STAMP,
                                         "detail": ""}))
    (p,) = only(out, "park")
    assert p["consult"]["outcome"] == "fell_back" and p["consult"]["reason"] == consult_mod.NO_STAMP


def test_todays_lane_handbacks_still_alert():
    assert "session_at_dialog:i5" in _alert_reasons(decide(**_at_dialog(check=None)))
    assert "session_host_blocked:i5" in _alert_reasons(decide(**_host_blocked(check=None)))


# ====================================== while a consult runs ======================================

def _running(kind=consult_mod.PARK, started=NOW - 60, outcome=None, exited=False, bright=False,
             status="running", key=None, **ist_over):
    hb = {"memo": "frozen and already relaunched 2 times (cap 2) — parking",
          "needs_william": False, "cause": "frozen_cap", "bright_line": bright}
    key = key or consult_mod.handback_key(kind, "frozen_cap")
    rec = {"key": key, "kind": kind, "state": "running", "id": "c5", "started_at": started,
           "session": _STAMP["session"], "handback": hb}
    rec.update(ist_over.pop("rec", {}))
    extra = {}
    if outcome is not None:
        extra["consult_outcomes"] = {"i5": outcome}
    if exited:
        extra["consult_exited"] = {"i5": "1750000000 rc=0"}
    return dict(parsed_issues=[parsed(5, labels=_IN_FLIGHT)],
                events=[{"type": "frozen", "id": "i5"}],
                dsk=_dsk(status, extra=extra, retries=2, handback=rec, handback_consults=1,
                         **ist_over))


def _outcome(*lines):
    return "\n".join(lines) + "\nEND\n"


def test_a_running_consult_pauses_the_issue_entirely():
    out = decide(**_running())
    assert [a for a in out if a.get("id") == "i5"] == []
    assert not has_notify(out)


def test_a_frozen_lane_under_consult_keeps_its_lane():
    """The status is unchanged while the fork considers it: a frozen lane still holds its lane and
    its territory, so queued work never takes a lane a stuck session still occupies."""
    st = {"version": 1, "issues": {"i5": ist("frozen", handback={"state": "running"})}}
    assert [l["id"] for l in actions.lane_state_from(st)] == ["i5"]


def test_a_pending_consult_is_never_a_fresh_launch():
    kw = _running(status="ready")
    kw["parsed_issues"] = [parsed(5)]
    out = decide(**kw)
    assert _mine(out, "launch") == []


@pytest.mark.parametrize("word,move,fields", [
    ("RELAUNCH", consult_mod.RELAUNCH, ["CLARIFICATION:", "Use approach A."]),
    ("RESUME", consult_mod.RESUME, []),
    ("END_AND_RELAUNCH", consult_mod.END_RELAUNCH, []),
    ("WAIT", consult_mod.WAIT, ["MINUTES: 30"]),
    ("FOLLOW_UP", consult_mod.FOLLOW_UP, ["TITLE: Fix the flake", "BODY:", "## Goal", "x"]),
])
def test_each_move_reaches_the_runner_as_one_act(word, move, fields):
    out = decide(**_running(outcome=_outcome("MOVE: " + word, "REASONING:", "ruled 11-04",
                                             *fields)))
    (m,) = only(out, "handback_move")
    assert m["id"] == "i5" and m["move"] == move and m["reasoning"] == "ruled 11-04"
    assert m["kind"] == consult_mod.PARK
    assert only(out, "park") == [] and not has_notify(out)
    if move == consult_mod.RELAUNCH:
        assert m["clarification"] == "Use approach A."
    if move == consult_mod.WAIT:
        assert m["minutes"] == 30
    if move == consult_mod.FOLLOW_UP:
        assert m["title"] == "Fix the flake" and m["issue_type"] == "type:build"


def test_an_escalation_hands_the_park_over_exactly_as_today_with_the_reasoning():
    out = decide(**_running(outcome=_outcome("MOVE: ESCALATE", "REASONING:", "never discussed",
                                             "RECOMMENDATION:", "re-scope it")))
    (p,) = only(out, "park")
    assert p["cause"] == "frozen_cap" and p["memo"].startswith("frozen and already relaunched")
    assert p["consult"]["outcome"] == "escalated"
    assert p["consult"]["reasoning"] == "never discussed"
    assert p["consult"]["recommendation"] == "re-scope it"
    assert has_notify(out), "an escalated park pages exactly as a park does today"
    assert only(out, "handback_move") == []


def test_an_escalated_bounce_is_todays_bounce():
    kw = _running(kind=consult_mod.BOUNCE, outcome=_outcome("MOVE: ESCALATE"),
                  rec={"handback": {"memo": "BOUNCED: gone"}})
    out = decide(**kw)
    (b,) = only(out, "bounce")
    assert b["memo"] == "BOUNCED: gone" and b["consult"]["outcome"] == "escalated"
    assert has_notify(out)


@pytest.mark.parametrize("kind", [consult_mod.FROZEN, consult_mod.AT_DIALOG,
                                  consult_mod.HOST_BLOCKED])
def test_an_escalated_lane_handback_reaches_the_owner(kind):
    out = decide(**_running(kind=kind, outcome=_outcome("MOVE: ESCALATE", "REASONING:", "r")))
    (e,) = only(out, "handback_escalate")
    assert e["kind"] == kind and e["consult"]["outcome"] == "escalated"
    if kind == consult_mod.FROZEN:
        assert has_notify(out), "a frozen lane has no alert of its own: the escalation pages"


# ---------------------------------- the bright line always escalates ----------------------------------

@pytest.mark.parametrize("word,fields", [("RELAUNCH", []), ("WAIT", ["MINUTES: 10"]),
                                         ("RESUME", []), ("END_AND_RELAUNCH", [])])
def test_a_bright_line_park_escalates_whatever_the_fork_decides(word, fields):
    out = decide(**_running(bright=True, outcome=_outcome("MOVE: " + word, "REASONING:",
                                                          "it is fine", *fields)))
    assert only(out, "handback_move") == []
    (p,) = only(out, "park")
    assert p["consult"]["outcome"] == "escalated" and p["consult"]["forced"] == "bright_line"
    assert p["consult"]["proposed"] == consult_mod.MOVES[["RELAUNCH", "RESUME", "END_AND_RELAUNCH",
                                                          "WAIT"].index(word)]
    assert has_notify(out)


def test_a_referee_path_park_is_marked_bright_line():
    pv = pr_view(files=(".superlooper/config.json",), branch="sl/i5-issue-5")
    dsk = _dsk("gating", extra={"reports": {"i5": GOOD_REPORT}},
               declared_touches=["frontend"], branch="sl/i5-issue-5")
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk,
                 gh_view=ghv(prs={"i5": pv}))
    (c,) = only(out, "handback_consult")
    assert c["handback"]["bright_line"] is True and c["handback"]["needs_william"] is True
    assert ".superlooper/config.json" in c["handback"]["memo"]


# ------------------------------- the runner refuses anything else -------------------------------

@pytest.mark.parametrize("word", ["HAND_MERGE", "FORCE_PUSH", "AGENT_READY", "PRE_AUTHORIZE",
                                  "PUBLISH", "EDIT .superlooper/config.json", "SEND_KEYS"])
def test_a_move_outside_the_set_is_refused_and_the_handback_reaches_the_owner(word):
    out = decide(**_running(outcome=_outcome("MOVE: " + word, "REASONING:", "trust me")))
    assert only(out, "handback_move") == []
    (p,) = only(out, "park")
    assert p["consult"]["outcome"] == "fell_back"
    assert p["consult"]["reason"] == consult_mod.REFUSED_MOVE and p["consult"]["detail"] == word
    assert not [a for a in out if a["act"] in ("merge", "set_labels", "relabel")]


# --------------------------------------- the fallbacks ---------------------------------------

@pytest.mark.parametrize("kw,reason", [
    (dict(outcome="MOVE: RELAUNCH\nEND\n"), consult_mod.MALFORMED),
    (dict(exited=True), consult_mod.FORK_EXITED),
    (dict(started=NOW - consult_mod.TIMEOUT_SECONDS), consult_mod.TIMEOUT),
    (dict(started="garbage"), consult_mod.TIMEOUT),
])
def test_a_consult_that_cannot_decide_hands_over_as_today(kw, reason):
    out = decide(**_running(**kw))
    (p,) = only(out, "park")
    assert p["consult"] == {"outcome": "fell_back", "reason": reason, "kind": consult_mod.PARK,
                            "key": consult_mod.handback_key(consult_mod.PARK, "frozen_cap"),
                            "session": _STAMP["session"]}
    assert has_notify(out)


def test_a_half_written_outcome_is_waited_for():
    out = decide(**_running(outcome="MOVE: RELAUNCH\nREASONING:\nhalf"))
    assert [a for a in out if a.get("id") == "i5"] == []


def test_a_consult_that_failed_to_launch_hands_over_as_today():
    out = decide(**_running(rec={"state": "fallback", "reason": consult_mod.LAUNCH_FAILED,
                                 "detail": "launch rc=2"}))
    (p,) = only(out, "park")
    assert p["consult"]["reason"] == consult_mod.LAUNCH_FAILED
    assert has_notify(out)


def test_a_follow_up_then_hands_the_park_over_naming_it():
    via = {"outcome": "escalated", "move": consult_mod.FOLLOW_UP, "follow_up": 612,
           "reasoning": "separate", "session": _STAMP["session"]}
    out = decide(**_running(rec={"state": "escalated", "via": via}))
    (p,) = only(out, "park")
    assert p["consult"]["follow_up"] == 612 and has_notify(out)


# ============================ the handback cap: the third goes to the owner ============================

def test_a_third_handback_goes_to_the_owner_regardless_with_the_history():
    out = decide(**_frozen_at_cap(handback_consults=2))
    assert only(out, "handback_consult") == []
    (p,) = only(out, "park")
    assert p["consult"]["reason"] == consult_mod.HANDBACK_CAP and p["consult"]["consults"] == 2
    assert has_notify(out)


def test_the_owner_can_change_the_cap():
    kw = _frozen_at_cap(handback_consults=2)
    out = decide(config=cfg(session={"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                                     "conflict_cap": 2, "handback_cap": 5}), **kw)
    assert len(only(out, "handback_consult")) == 1
    out = decide(config=cfg(session={"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                                     "conflict_cap": 2, "handback_cap": 1}),
                 **_frozen_at_cap())
    assert only(out, "handback_consult") == [] and len(only(out, "park")) == 1


def test_the_second_handback_is_still_consulted():
    out = decide(**_frozen_at_cap(handback_consults=1))
    (c,) = only(out, "handback_consult")
    assert c["number"] == 2


def test_a_lane_handback_at_the_cap_is_escalated_once_with_the_history():
    out = decide(**_frozen_lane(handback_consults=2))
    (e,) = only(out, "handback_escalate")
    assert e["consult"]["reason"] == consult_mod.HANDBACK_CAP
    assert has_notify(out)
    assert len(_mine(out, "recover")) == 1, "and today's nudge still fires"


# ========================== a decided handback, and a wait ==========================

def test_a_delivered_handback_is_todays_from_then_on():
    key = consult_mod.handback_key(consult_mod.FROZEN, NOW - actions.RECOVER_RETRY_SECONDS - 5)
    out = decide(**_frozen_lane(handback={"key": key, "kind": consult_mod.FROZEN,
                                          "state": "delivered", "via": {"outcome": "escalated"}}))
    assert only(out, "handback_consult") == [] and only(out, "handback_escalate") == []
    assert len(_mine(out, "recover")) == 1


def test_a_delivered_parks_label_retry_keeps_its_note():
    key = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    via = {"outcome": "escalated", "reasoning": "r"}
    out = decide(**_frozen_at_cap(park_notify_cause="frozen_cap",
                                  handback={"key": key, "kind": consult_mod.PARK,
                                            "state": "delivered", "via": via}))
    (p,) = only(out, "park")
    assert p["retry"] is True and p["consult"] == via and not has_notify(out)


def test_a_wait_holds_the_handback_until_its_bound():
    key = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    hold = {"key": key, "kind": consult_mod.PARK, "state": "waiting", "until": NOW + 600}
    out = decide(**_frozen_at_cap(handback=hold, handback_consults=1))
    assert only(out, "park") == [] and only(out, "handback_consult") == []
    assert not has_notify(out)


def test_a_handback_still_standing_past_its_wait_is_a_new_one():
    key = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    hold = {"key": key, "kind": consult_mod.PARK, "state": "waiting", "until": NOW - 1}
    out = decide(**_frozen_at_cap(handback=hold, handback_consults=1))
    (c,) = only(out, "handback_consult")
    assert c["number"] == 2


def test_a_handback_that_recurs_after_a_move_is_a_new_one():
    key = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    out = decide(**_frozen_at_cap(handback={"key": key, "kind": consult_mod.PARK,
                                            "state": "moved", "move": consult_mod.RELAUNCH},
                                  handback_consults=1))
    assert len(only(out, "handback_consult")) == 1


def test_a_lane_handback_waits_without_alerting():
    key = consult_mod.handback_key(consult_mod.AT_DIALOG, NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)
    hold = {"key": key, "kind": consult_mod.AT_DIALOG, "state": "waiting", "until": NOW + 600}
    out = decide(**_at_dialog(handback=hold))
    assert "session_at_dialog:i5" not in _alert_reasons(out)
    assert only(out, "handback_consult") == []


# ============================== one consult at a time, and the owner ==============================

def test_one_consult_per_issue_at_a_time():
    """A park consult is running; the same lane's dialog crosses its bound. No second consult."""
    kw = _running(status="frozen", sensed_state="at_dialog",
                  sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)
    out = decide(**kw)
    assert only(out, "handback_consult") == []
    assert "session_at_dialog:i5" not in _alert_reasons(out)


def test_a_question_consult_owns_its_issue():
    dsk = _dsk("consulting", consult={"id": "c5", "state": "running", "started_at": NOW - 60},
               sensed_state="at_dialog", sensed_since=NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)
    out = decide(dsk=dsk)
    assert only(out, "handback_consult") == []


def test_an_owner_close_mid_consult_is_absorbed():
    kw = _running()
    out = decide(gh_view=ghv(closed_nums={5}), **kw)
    assert len(_mine(out, "absorb_close")) == 1


def test_a_consult_waits_for_nothing_it_falls_back_when_launches_are_held():
    """A handback is already a safe state: a consult that cannot start now is not waited for."""
    over = usage_ok()
    over["five_hour_pct"] = 99.0
    out = decide(usage=over, **_frozen_at_cap())
    assert only(out, "handback_consult") == []
    (p,) = only(out, "park")
    assert p["consult"]["reason"] == consult_mod.HELD_NOW and has_notify(out)


# ============================ the machine-level faults stay with the watchdog ============================

@pytest.mark.parametrize("over", [
    dict(gh_view=ghv(consecutive_failures=99)),                                # a GitHub outage
    dict(usage={"auth_status": "error", "five_hour_pct": None, "seven_day_pct": None,
                "last_ok_at": NOW - 99999, "first_attempt_at": NOW - 99999}),   # a dark meter
], ids=["github_outage", "usage_outage"])
def test_a_machine_level_fault_never_launches_a_consult(over):
    dsk = _dsk("running", sensed_state="logged_out", sensed_since=NOW - 9999)
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk, **over)
    assert only(out, "alert"), "the fault is raised, for the watchdog and the owner"
    assert only(out, "handback_consult") == []


def test_a_session_whose_auth_died_is_not_an_issue_handback():
    dsk = _dsk("frozen", sensed_state="logged_out", sensed_since=NOW - 9999,
               frozen_since=NOW - 9999, last_recover_at=NOW - 9999)
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk)
    assert "session_logged_out:i5" in _alert_reasons(out)
    assert only(out, "handback_consult") == []


# ============================== the relaunch rides the answered-consult path ==============================

def test_a_relaunched_issue_launches_on_the_runners_own_stamp():
    dsk = _dsk("ready", consult_relaunch=True, requeue_front=True,
               handback={"state": "moved", "move": consult_mod.RELAUNCH})
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk)
    (l,) = _mine(out, "launch")
    assert l["num"] == 5


# ============================== fresh-review round 1 (P1/P2) ==============================

def test_a_consult_on_an_issue_the_owner_closed_never_hands_anything_back():
    """P1: absorb_close settles the issue `merged`; a consult record left pending must never
    deliver a park/bounce on the closed issue when its bound runs out."""
    for at in (NOW, NOW + consult_mod.TIMEOUT_SECONDS + 5):
        kw = _running(status="merged")
        out = decide(now=at, gh_view=ghv(closed_nums={5}), **kw)
        assert only(out, "park") == [] and only(out, "bounce") == [], at
        assert not [n for n in only(out, "notify") if "i5" in n.get("headline", "")], at


def test_two_handbacks_in_one_tick_start_one_consult():
    """P2: a lane at its dialog (Section A) that also wrote a BOUNCED: memo (Section D)."""
    kw = _at_dialog()
    kw["dsk"]["blocked"] = {"i5": "BOUNCED: gone"}
    out = decide(**kw)
    assert len(only(out, "handback_consult")) == 1
    assert _mine(out, "recover") == [] and _mine(out, "bounce") == []


def test_a_dialog_alert_already_standing_stays_the_owners():
    """P2: a dialog alert that already paged (a consult that could not start, or one standing
    before #539 shipped) is not pulled back into a consult — that would page a false recovery."""
    kw = _at_dialog()
    kw["dsk"]["alert"] = {"reasons": ["session_at_dialog:i5"], "since": NOW - 100,
                          "paged": ["session_at_dialog:i5"],
                          "delivered": ["session_at_dialog:i5"]}
    out = decide(**kw)
    assert only(out, "handback_consult") == []
    # the standing ALERT is unchanged — no re-raise, and above all no false recovery
    assert only(out, "clear_alert") == []
    assert all("session_at_dialog:i5" in a["reasons"] for a in only(out, "alert"))
    assert not [n for n in only(out, "notify") if n.get("tier") == "recovered"]


def test_an_owner_close_during_a_wait_is_absorbed():
    key = consult_mod.handback_key(consult_mod.PARK, "frozen_cap")
    hold = {"key": key, "kind": consult_mod.PARK, "state": "waiting", "until": NOW + 600}
    out = decide(gh_view=ghv(closed_nums={5}), **_frozen_at_cap(handback=hold))
    assert len(_mine(out, "absorb_close")) == 1
    assert only(out, "handback_consult") == []


def test_a_cap_with_no_consult_on_record_is_todays_handback_with_nothing_added():
    """P3: `handback_cap` 0 or 1 means straight to the owner — today's, not a cap note and not a
    page a frozen lane never sent."""
    c = cfg(session={"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                     "conflict_cap": 2, "handback_cap": 1})
    out = decide(config=c, **_frozen_lane())
    assert only(out, "handback_consult") == [] and only(out, "handback_escalate") == []
    assert not has_notify(out) and len(_mine(out, "recover")) == 1
    (p,) = only(decide(config=c, **_frozen_at_cap()), "park")
    assert not consult_mod.note_posts(p["consult"])


def test_a_park_record_that_cannot_be_read_escalates_rather_than_moves():
    """P3: a corrupt handback payload loses its bright-line flag — fail closed to the owner."""
    kw = _running(outcome=_outcome("MOVE: RELAUNCH", "REASONING:", "r"),
                  rec={"handback": "garbage"})
    out = decide(**kw)
    assert only(out, "handback_move") == []
    (p,) = only(out, "park")
    assert p["consult"]["outcome"] == "escalated"


# ============================ #549: the three edges #548's review left ============================

def _waiting_on(kind, key, until):
    return {"key": key, "kind": kind, "state": "waiting", "until": until, "id": "c5",
            "session": _STAMP["session"], "move": consult_mod.WAIT}


def _each_handback_kind():
    """Every kind of handback, each standing, with the key a consult of it is recorded under."""
    kw, memo = _bounced()
    frozen_since = NOW - actions.RECOVER_RETRY_SECONDS - 5
    return [
        (_frozen_at_cap, consult_mod.PARK, consult_mod.handback_key(consult_mod.PARK, "frozen_cap")),
        (lambda **o: _bounced(**o)[0], consult_mod.BOUNCE,
         consult_mod.handback_key(consult_mod.BOUNCE, memo)),
        (_frozen_lane, consult_mod.FROZEN, consult_mod.handback_key(consult_mod.FROZEN, frozen_since)),
        (_at_dialog, consult_mod.AT_DIALOG,
         consult_mod.handback_key(consult_mod.AT_DIALOG, NOW - actions.AT_DIALOG_ALERT_SECONDS - 1)),
        (_host_blocked, consult_mod.HOST_BLOCKED,
         consult_mod.handback_key(consult_mod.HOST_BLOCKED, NOW - actions.AT_DIALOG_ALERT_SECONDS)),
    ]


@pytest.mark.parametrize("until", [NOW + 600, NOW - 1, NOW - 86400],
                         ids=["holding", "just_ran_out", "long_ran_out"])
@pytest.mark.parametrize("make,kind,key", _each_handback_kind(),
                         ids=["park", "bounce", "frozen", "at_dialog", "host_blocked"])
def test_a_close_on_a_waiting_handback_is_absorbed_and_never_consulted(make, kind, key, until):
    """#549 edge 1: the owner closes the issue just after the WAIT its approving session chose has
    run out. The re-derived handback must not launch a consult on the closed issue (the fork would
    live one tick, until the absorb ended it): the close is the owner dropping the issue, absorbed
    this tick, whether or not the hold has expired."""
    out = decide(gh_view=ghv(closed_nums={5}),
                 **make(handback=_waiting_on(kind, key, until), handback_consults=1))
    assert len(_mine(out, "absorb_close")) == 1
    assert only(out, "handback_consult") == []
    assert _mine(out, "park") == [] and _mine(out, "bounce") == []
    assert only(out, "handback_escalate") == [] and not has_notify(out)
    assert not [r for r in _alert_reasons(out) if r.endswith(":i5")]


def test_a_handback_past_its_wait_on_an_open_issue_is_still_a_new_consult():
    """...and the fix is the close's alone: on an OPEN issue a wait that ran out re-derives."""
    for make, kind, key in _each_handback_kind():
        out = decide(**make(handback=_waiting_on(kind, key, NOW - 1), handback_consults=1))
        (c,) = only(out, "handback_consult")
        assert c["kind"] == kind and _mine(out, "absorb_close") == [], kind


def _standing(reason):
    return {"reasons": [reason], "since": NOW - 100, "paged": [reason], "delivered": [reason]}


def _park_consult(state):
    rec = {"key": consult_mod.handback_key(consult_mod.PARK, "frozen_cap"),
           "kind": consult_mod.PARK, "state": state, "id": "c5", "started_at": NOW - 60,
           "session": _STAMP["session"],
           "handback": {"memo": "frozen and already relaunched 2 times (cap 2) — parking",
                        "needs_william": False, "cause": "frozen_cap", "bright_line": False}}
    if state == "waiting":
        rec.update(until=NOW + 600, move=consult_mod.WAIT)
    return rec


@pytest.mark.parametrize("state", ["running", "waiting"])
@pytest.mark.parametrize("make,reason", [(_at_dialog, "session_at_dialog:i5"),
                                         (_host_blocked, "session_host_blocked:i5")],
                         ids=["at_dialog", "host_blocked"])
def test_a_standing_lane_alert_stays_standing_under_another_consult(make, reason, state):
    """#549 edge 2: a lane's dialog (or host-block) ALERT already paged the owner; then a consult of
    ANOTHER kind — here a park — starts on the same issue, or holds it with a WAIT. The reason is
    still the owner's: dropping it would page a false 🟢 recovery (and a second 🔴 later)."""
    kw = make(handback=_park_consult(state), handback_consults=1)
    kw["dsk"]["alert"] = _standing(reason)
    out = decide(**kw)
    assert only(out, "clear_alert") == []
    assert all(reason in a["reasons"] for a in only(out, "alert"))
    assert not [n for n in only(out, "notify") if n.get("tier") == "recovered"]
    assert only(out, "handback_consult") == []


def _stale_wait():
    """A WAIT that WORKED: the hold ran out an hour ago and its handback never re-derived (the
    lane woke), so the record simply stays `waiting`."""
    return _waiting_on(consult_mod.FROZEN, consult_mod.handback_key(consult_mod.FROZEN, NOW - 9999),
                       NOW - 3600)


@pytest.mark.parametrize("status", ["running", "gating"])
def test_a_close_after_a_wait_that_worked_is_not_the_owners_drop(status):
    """#549 review P2: a `waiting` record outlives a WAIT that worked. A later close of the plain
    running build is not a handback episode — absorbing it would end a live worker and prune its
    worktree. Only the handback the WAIT held, re-deriving, is absorbed at the door."""
    dsk = _dsk(status, handback=_stale_wait(), handback_consults=1)
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk,
                 gh_view=ghv(closed_nums={5}))
    assert _mine(out, "absorb_close") == [] and only(out, "handback_consult") == []


def test_an_out_of_band_merge_after_a_wait_that_worked_still_lands():
    """...and a PR merged by hand on that lane is still settled as LANDED (absorb_merged), never as
    the owner's drop."""
    dsk = _dsk("running", handback=_stale_wait(), handback_consults=1, pr=555,
               branch="sl/i5-issue-5")
    out = decide(parsed_issues=[parsed(5, labels=_IN_FLIGHT)], dsk=dsk,
                 gh_view=ghv(closed_nums={5}, prs={"i5": pr_view(state="MERGED")}))
    assert len(_mine(out, "absorb_merged")) == 1 and _mine(out, "absorb_close") == []


def test_a_door_absorb_is_one_act_and_the_issue_does_nothing_else_this_tick():
    """The door absorbs once per tick, and the absorbed issue takes no other act — no launch, no
    recover, no second absorb from Section D."""
    kw = _at_dialog(handback=_waiting_on(consult_mod.AT_DIALOG, consult_mod.handback_key(
        consult_mod.AT_DIALOG, NOW - actions.AT_DIALOG_ALERT_SECONDS - 1), NOW - 1))
    kw["dsk"]["blocked"] = {}
    out = decide(gh_view=ghv(closed_nums={5}), **kw)
    assert [a["act"] for a in out if a.get("id") == "i5"] == ["absorb_close"]
