"""Issue #451 — the triage flight reaches the shipped VIEW, and changes nothing when it is absent.

``test_triage.py`` unit-tests the gloss core and ``test_tower_triage.py`` the sentences. This is the
FIXTURE test that drives the real assembler over a real state home: the block the card binds, the
plane the field draws, and — the load-bearing half — the proof that a board with no triage flight is
byte-for-byte the board that shipped before this issue.

Three properties:

* **No new server read.** The run comes from the journal the assembler ALREADY reads for the tower
  log, the shipped counter and the incident sign, and its liveness from the ``state/activity``
  mtimes it already scans. No file is opened that was not opened before, no CLI is shelled, no
  GitHub is asked. #463 settled that a flight is absent from the runner's published view by design,
  so that document is not — and must not become — where this is looked for.
* **A flight is additive.** It has no loopstate record, so it is not a lane; folding one into
  ``repo["flights"]`` would put it through every derivation that assumes an issue number (titles,
  arrivals, departures, the stand, the landmarks, the repo's own state). It rides its own key.
* **Absent means absent.** DoD item 4 — with no ``t`` flight in state the board renders exactly as
  today — pinned by assembling the same home twice and diffing every other key.
"""
import json
import os
import shutil

import pytest

import server
import triage as triage_mod

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
SLUG = "will-titan/superlooper-sandbox"
NOW = 1783364300


@pytest.fixture
def home(tmp_path):
    dst = tmp_path / "will-titan__superlooper-sandbox"
    shutil.copytree(FIXTURE, dst)
    return dst


def _journal(home, *records):
    with open(str(home / "journal.jsonl"), "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _activity(home, iid, mtime):
    path = home / "state" / "activity" / iid
    path.write_text(str(int(mtime)))
    os.utime(str(path), (mtime, mtime))


def _config(home):
    repo = {"slug": SLUG, "owner": "will-titan", "name": "superlooper-sandbox",
            "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
            "required_checks": ["tests"], "airline": "Sandbox Air"}
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300, "repos": [repo]}


def _snap(home):
    return server.assemble_snapshot(_config(home), now=NOW)


def _repo(home):
    return _snap(home)["repos"][0]


LAUNCH = {"ts": NOW - 5400, "act": "triage_launch", "id": "t7", "num": None,
          "outcome": "launched", "detail": "4 issues changed since the last run"}
FINISH_COUNTS = {"judged": 6, "merged": 1, "closed": 2, "ledger": 1, "fixed": 1, "escalated": 1}
FINISH_SUMMARY = "judged 6 · 1 merged · 2 closed (1 to the ledger) · 1 fixed · 1 escalated"
FINISH = {"ts": NOW - 300, "act": "triage_finish", "flight": "t7", "num": None,
          "counts": dict(FINISH_COUNTS), "detail": FINISH_SUMMARY, "outcome": "ok"}


# =============================== absent means absent (DoD item 4) ===============================

def test_a_board_with_no_triage_flight_carries_the_block_saying_so(home):
    block = _repo(home)["triage"]
    assert block["present"] is False
    assert block["on_field"] is False
    # Always PRESENT AS A BLOCK — an absent key is how a surface starts throwing at 3am (the same
    # discipline `stopped` is held to). The honest silence is `present: False`, not a missing field.


def test_the_flight_changes_nothing_else_on_the_board(home, tmp_path):
    # The pin. Assemble the pristine fixture, then the same fixture with a whole triage run in its
    # journal, and diff EVERY other key of the repo slice. Only the surfaces that exist to carry the
    # flight may move; a flight that quietly re-ordered the boards, re-derived the repo's state or
    # invented a lane would fail here.
    before = _repo(home)
    with_flight = tmp_path / "with-flight"
    shutil.copytree(str(home), str(with_flight))
    _journal(with_flight, LAUNCH, FINISH)
    _activity(with_flight, "t7", NOW - 300)
    after = server.assemble_snapshot(_config(with_flight), now=NOW)["repos"][0]

    moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert moved == {"triage", "tower_log"}, (
        "a triage flight moved %s — it may only add its own block and its own comms lines"
        % sorted(moved - {"triage", "tower_log"}))
    # And the tower log moved by ADDITION only: every row that was there is still there, unchanged
    # (the feed is chronological, so a flight's lines interleave rather than append — order is the
    # journal's, and it is not this issue's to change).
    for row in before["tower_log"]:
        assert row in after["tower_log"], "a triage flight rewrote an existing comms line: %r" % row


def test_the_pristine_fixtures_tower_log_gains_nothing(home):
    # The fence gloss is gated on a t<N> id, so a worker's fence record still renders as it did.
    # Belt to the braces above: nothing in the shipped fixture's feed learned a triage word.
    for row in _repo(home)["tower_log"]:
        assert "triage" not in row["text"].lower()


# =============================== a flight in the air ===============================

def test_a_flying_flight_reaches_the_snapshot_as_its_own_plane(home):
    _journal(home, LAUNCH)
    _activity(home, "t7", NOW - 60)
    block = _repo(home)["triage"]
    assert block["present"] is True
    assert block["on_field"] is True
    assert block["id"] == "t7"
    assert block["num"] == 7
    assert block["contrail"] == "crisp", "the flight's OWN activity stamp, read like every other"


def test_a_flying_flight_is_never_folded_into_the_lane_flights(home):
    # It has no loopstate record by design (#463). A lane list entry would put it through every
    # derivation that assumes an issue number — titles, arrivals, the stand, the repo's own state.
    _journal(home, LAUNCH)
    _activity(home, "t7", NOW - 60)
    repo = _repo(home)
    # NB the fixture already flies issue #7 as the lane `i7` — which is exactly why a flight may not
    # be keyed by its bare number anywhere: `t7` and `i7` are different aircraft.
    assert all(str(f["id"]).startswith("i") for f in repo["flights"])
    assert "t7" not in [f["id"] for f in repo["flights"]]


def test_a_quiet_flight_shows_its_quiet(home):
    _journal(home, LAUNCH)
    _activity(home, "t7", NOW - 3000)          # past the fixture repo's own freeze_seconds
    assert _repo(home)["triage"]["contrail"] == "none"


def test_a_flight_with_no_activity_stamp_still_flies(home):
    # The stamp is written at delivery-verify; a snapshot taken in the gap has none. No signal is
    # not "frozen", and the plane is not withheld for the lack of one.
    _journal(home, LAUNCH)
    block = _repo(home)["triage"]
    assert block["on_field"] is True
    assert block["contrail"] == "none"


# =============================== the card's counts (DoD item 3) ===============================

def test_the_card_shows_the_runs_summary_counts_as_the_flight_published_them(home):
    _journal(home, LAUNCH, FINISH)
    block = _repo(home)["triage"]
    assert block["counts"] == FINISH_COUNTS
    assert block["counts_source"] == triage_mod.FLIGHT
    assert block["tally"] == FINISH_SUMMARY, "the flight's own sentence, verbatim"
    assert block["escalated"] == 1
    assert block["on_field"] is False, "the survey is over"


def test_a_run_that_died_before_finishing_still_reports_what_it_did(home):
    _journal(home, LAUNCH,
             {"ts": NOW - 4000, "act": "triage_merge", "num": 452, "flight": "t7", "absorber": 440,
              "detail": "duplicate", "outcome": "ok"},
             {"ts": NOW - 3800, "act": "triage_escalate", "num": 460, "flight": "t7",
              "finding": "hides an owner decision", "outcome": "ok"})
    block = _repo(home)["triage"]
    assert block["counts"]["merged"] == 1 and block["counts"]["escalated"] == 1
    assert block["counts_source"] == triage_mod.DERIVED
    assert block["state"] == triage_mod.FLYING


def test_a_launch_that_never_landed_a_session_says_so_and_flies_nothing(home):
    _journal(home, dict(LAUNCH, outcome="launch failed (rc=1)", detail="the launcher refused"))
    block = _repo(home)["triage"]
    assert block["present"] is True
    assert block["on_field"] is False
    assert block["state"] == triage_mod.NO_FLIGHT
    assert "launch failed (rc=1)" in block["text"]


# =============================== the flight narrates in the tower (DoD item 2) ===============================

def test_the_run_narrates_itself_in_the_tower_feed(home):
    _journal(home, LAUNCH,
             {"ts": NOW - 4000, "act": "triage_close", "num": 452, "flight": "t7",
              "verdict": "overtaken", "commit": "abc1234", "outcome": "ok"},
             FINISH)
    texts = [row["text"] for row in _repo(home)["tower_log"]]
    joined = " ".join(texts)
    assert "t7 departed" in joined
    assert "closed #452 as overtaken by `abc1234`" in joined
    assert FINISH_SUMMARY in joined
    for text in texts:
        assert "triage_" not in text, "a raw act name reached the comms feed"


def test_a_flights_fence_record_reaches_the_feed_in_honest_words(home):
    _journal(home, {"ts": NOW - 5500, "act": "fence_preflight", "id": "t7", "verdict": "fenced",
                    "required": True, "socket": "/Users/somebody/.config/x/y.sock",
                    "refused": False},
             LAUNCH)
    joined = " ".join(row["text"] for row in _repo(home)["tower_log"])
    assert "t7 cleared its pre-flight fence check" in joined
    assert "/Users/" not in joined


def test_the_kept_issues_are_classified_routine_so_the_feed_is_not_buried(home):
    _journal(home, LAUNCH, *[
        {"ts": NOW - 4000 + i, "act": "triage_keep", "num": 400 + i, "flight": "t7",
         "detail": "buildable", "outcome": "ok"} for i in range(12)])
    rows = _repo(home)["tower_log"]
    kept = [r for r in rows if "kept #" in r["text"]]
    assert len(kept) == 12, "every one is still on record — routine hides, it never drops"
    assert all(r["tier"] == "routine" for r in kept)
