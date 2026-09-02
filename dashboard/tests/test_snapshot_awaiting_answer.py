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
                                    "frozen", "holding", "launch_hold", "closed"])
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
