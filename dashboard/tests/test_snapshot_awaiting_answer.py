"""Issue #471 — one ``awaiting_answer`` issue must never black out the whole command center.

**The incident (2026-09-02, reproduced 2026-08-19).** Every ``/api/snapshot`` poll answered
HTTP 500 ``{"error":"snapshot unavailable","detail":"list index out of range"}``, so the page
showed the pre-first-snapshot "can't reach the tower" card forever — on a freshly started
dashboard, on the current build, with both runners healthy and flowing. It self-cleared in August
the way a state-shaped crash does: the repo state churned past the offending issue.

**The shape that did it.** ``lib/cards.decision_actions`` grew a #163 QUESTION kind whose verbs are
Answer/Discuss/Drop — deliberately no ``approve`` and no ``bounce-yes`` ("a question is answered,
not approved"). ``flight_drawer`` picked its yes-verb by naming those two acts and indexing
``[0]``, so the first flight built from an ``awaiting_answer`` issue raised ``IndexError`` — inside
``assemble_snapshot``, which builds a drawer for EVERY flight in EVERY watched repo. One paused
worker's question therefore took down the whole board, for every repo, until someone answered it.

These tests pin the state shape itself, at the two altitudes that matter: the assembler must build
a snapshot from it, and the HTTP route must answer 200 with it. The unit-level guard that stops the
class recurring — exactly one marked yes-verb per decision kind — lives in ``test_cards``.
"""
import json
import os

import pytest

import flights
import server

NOW = 1783364300
SLUG = "will-titan/agent-360-eapp"
QUESTION = ("QUESTION: does the fixer own the retry, or the runner?\n"
            "OPTIONS: fixer owns it / runner owns it\n"
            "RECOMMENDATION: fixer owns it")


def _home(tmp_path, status, **issue_over):
    """A minimal but REAL state home: one issue at ``status``, a live heartbeat, one journal line."""
    home = tmp_path / "will-titan__agent-360-eapp"
    (home / "state").mkdir(parents=True)
    issue = {"status": status, "branch": "sl/i9-a-question", "pr": None}
    issue.update(issue_over)
    (home / "state" / "issues.json").write_text(
        json.dumps({"version": 1, "issues": {"i9": issue}}), encoding="utf-8")
    (home / "state" / "runner.heartbeat").write_text(str(NOW - 5), encoding="utf-8")
    (home / "journal.jsonl").write_text(
        json.dumps({"ts": NOW - 500, "act": "launch", "id": "i9", "num": 9}) + "\n",
        encoding="utf-8")
    return home


def _config(home):
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300,
            "repos": [{"slug": SLUG, "owner": "will-titan", "name": "agent-360-eapp",
                       "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
                       "required_checks": ["tests"], "airline": "360 Air"}]}


def test_a_paused_workers_question_does_not_crash_the_assembler(tmp_path):
    home = _home(tmp_path, "awaiting_answer", pending_question=QUESTION)
    snap = server.assemble_snapshot(_config(home), now=NOW)

    flight = snap["flights"][0]
    assert flight["stage"] == flights.AWAITING
    assert flight["awaiting_reason"] == "question"
    # The whole question rides through to the surfaces that show it — the crash used to happen
    # while building exactly this drawer.
    assert flight["memo"] == QUESTION
    assert flight["drawer"]["decision"]["kind"] == "question"
    assert flight["drawer"]["decision"]["approve_act"] == "answer"
    assert flight["drawer"]["decision"]["approve_input"] == "answer"


def test_the_snapshot_route_answers_200_for_a_repo_holding_a_question(tmp_path):
    """The owner-visible half: a 500 here is the "can't reach the tower" card, forever."""
    cfg = _config(_home(tmp_path, "awaiting_answer", pending_question=QUESTION))
    resp = server.route("GET", "/api/snapshot", lambda: server.assemble_snapshot(cfg, now=NOW),
                        str(tmp_path))
    assert resp.status == 200, resp.body
    body = json.loads(resp.body)
    assert body["repos"][0]["slug"] == SLUG
    assert body["needs_you"], "a question is a decision waiting on the owner"
    assert body["needs_you"][0]["kind"] == "question"


def test_a_question_in_one_repo_never_blacks_out_the_others(tmp_path):
    """The blast radius that made this a whole-field outage rather than one bad card.

    ``assemble_snapshot`` builds every repo's flights into ONE body, so an exception raised while
    building one repo's drawer takes the other repo's board with it. William's config watches two
    repos; both runners were healthy and flowing, and he could see neither.
    """
    a = _home(tmp_path / "a", "awaiting_answer", pending_question=QUESTION)
    b = _home(tmp_path / "b", "running")
    cfg = _config(a)
    cfg["repos"].append({"slug": "titancasket/titan-apps-partner", "owner": "titancasket",
                         "name": "titan-apps-partner", "state_home": str(b), "idle_seconds": 480,
                         "freeze_seconds": 2700, "required_checks": ["tests"],
                         "airline": "Partner Air"})
    snap = server.assemble_snapshot(cfg, now=NOW)
    assert [r["slug"] for r in snap["repos"]] == [SLUG, "titancasket/titan-apps-partner"]
    assert len(snap["flights"]) == 2


@pytest.mark.parametrize("status", ["queued", "launched", "running", "blocked", "final", "merged",
                                    "parked", "needs_william", "bounced", "awaiting_answer",
                                    "consulting", "frozen", "holding", "launch_hold", "closed"])
def test_no_engine_status_can_500_the_snapshot(tmp_path, status):
    """The sweep that found it, kept as the ratchet.

    Every status the engine writes was run through the assembler; ``awaiting_answer`` was the only
    one that raised. The dashboard reads a vocabulary the engine owns and can extend, so the value
    of this test is not the one status that broke — it is that a status the dashboard has no
    special handling for must still produce a snapshot, never an exception.
    """
    cfg = _config(_home(tmp_path, status))
    snap = server.assemble_snapshot(cfg, now=NOW)
    assert snap["generated_at"] == NOW


# =============================== consulting (issue #545, over the engine's #538) ===============================
# #538 added a loopstate status between "a worker asked" and "William is asked": `consulting`. The
# worker exited cleanly on its question, and a fork of the session that APPROVED the issue is
# considering it first. Like `awaiting_answer` it holds no lane and no worker window, and it is moved
# only by the fork's outcome. The board used to have no word for it, so it flew the issue as an
# ordinary in-air flight at whatever leg its markers suggested — and once the exited worker's
# activity file aged, as a DEAD session. Neither is true, and neither tells William the one thing
# that is: his approving session is considering a question about this issue, and nothing is needed
# from him yet. Only an escalation or a fallback makes it his question (owner ruling on #538).

# The consult record exactly as `runner._exec_consult_start` settles it.
_CONSULT = {"id": "c9", "state": "running", "session": "1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c",
            "host": "Williams-Mini", "cwd": "/Users/someone/Projects/repo",
            "question": QUESTION, "started_at": NOW - 120}


def _consulting(tmp_path, activity_age=None):
    """A real state home holding one issue in `consulting`, as #538 writes it. ``activity_age`` (s)
    leaves the exited worker's activity file behind at that age — the teardown keeps the worktree,
    so the file a dead-session read keys on is still there and only gets older."""
    home = _home(tmp_path, "consulting", pending_question=QUESTION, consult=dict(_CONSULT),
                 lane=None, consult_held=None, consult_answer_posted=False)
    if activity_age is not None:
        act = home / "state" / "activity" / "i9"
        act.parent.mkdir(parents=True, exist_ok=True)
        act.write_text("", encoding="utf-8")
        os.utime(act, (NOW - activity_age, NOW - activity_age))
    return home


def test_a_consulting_issue_renders_as_consulting_not_as_a_flight_in_the_air(tmp_path):
    snap = server.assemble_snapshot(_config(_consulting(tmp_path, activity_age=60)), now=NOW)
    flight = snap["flights"][0]

    assert flight["stage"] == flights.CONSULTING == "consulting"
    assert flight["stage"] not in flights.CIRCUIT_STAGES           # not queued, not on a leg
    assert flight["on_circuit"] is False
    assert flight["display"]["in_air"] is False                    # not in the "N in the air" tally
    assert flight["awaiting_reason"] is None                       # not an owner decision
    # Its own words, in every place the board names a flight's state: the drawer's off-path note…
    off = flight["drawer"]["off_path"]
    assert off["state"] == flights.CONSULTING
    plain = off["plain"].lower()
    assert "approv" in plain and "considering" in plain
    assert "nothing" in plain and "you" in plain                    # …and that it needs nothing from him
    # …which carries the question being considered (it is posted nowhere until the consult decides)…
    assert off["question"] == QUESTION
    # …and the boring table's note.
    assert "approving session" in flight["display"]["note"]
    # No towed BUILDING cloth: nothing is building.
    assert all(b["num"] != 9 for b in snap["repos"][0]["field_banners"])


def test_a_consulting_issue_whose_worker_activity_went_stale_is_not_a_dead_session(tmp_path):
    """The exited worker's activity file keeps ageing while the fork thinks. Read as a running
    flight, that is a frozen session — grey, dimmed, a pill condition, "a session has frozen on the
    field" — for a lane the loop released on purpose."""
    snap = server.assemble_snapshot(_config(_consulting(tmp_path, activity_age=4000)), now=NOW)
    flight = snap["flights"][0]
    assert flight["stage"] == flights.CONSULTING
    assert flight["contrail"] == "none"           # the worker is gone; nothing trails
    assert snap["repos"][0]["state"]["state"] == "ok"
    assert snap["pill"]["level"] == "ok"


def test_a_consulting_issue_is_never_in_the_questions_list(tmp_path):
    """Only an escalation or a fallback is William's question (owner ruling on #538). While his
    approving session considers it, the Questions list stays empty and the drawer offers no verb."""
    snap = server.assemble_snapshot(_config(_consulting(tmp_path)), now=NOW)
    assert snap["needs_you"] == []
    flight = snap["flights"][0]
    assert flight["drawer"]["decision"] is None   # no Answer / Approve / Drop buttons on it


def test_the_snapshot_route_answers_200_for_a_consulting_issue(tmp_path):
    cfg = _config(_consulting(tmp_path))
    resp = server.route("GET", "/api/snapshot", lambda: server.assemble_snapshot(cfg, now=NOW),
                        str(tmp_path))
    assert resp.status == 200, resp.body
    body = json.loads(resp.body)
    assert body["flights"][0]["stage"] == "consulting"
    assert body["needs_you"] == []


def test_the_same_question_is_his_once_the_consult_escalates(tmp_path):
    """The other side of the ruling, pinned so the two can't drift: when the consult escalates or
    falls back, the engine settles the issue to `awaiting_answer` — and THAT is the Questions card."""
    home = _home(tmp_path, "awaiting_answer", pending_question=QUESTION,
                 consult=dict(_CONSULT, state="escalated"))
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["flights"][0]["stage"] == flights.AWAITING
    assert [c["kind"] for c in snap["needs_you"]] == ["question"]
