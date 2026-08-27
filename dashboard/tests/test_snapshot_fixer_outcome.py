"""Issue #458 — the Deploy Fixer tap RENDERS ITS OUTCOME, in the snapshot the banner binds.

On 2026-08-26 the owner tapped Deploy Fixer during a Claude-auth outage. The request was consumed,
the engine attempted the launch, the attempt failed, and the failure was journaled — and the
dashboard showed nothing. "Seems to have done nothing" about a button that did something is the
dishonest surface this project keeps killing, and the mechanism was structural: the ONLY surface
that ever named a result was the note box, and a close, a reload or a superseding open threw it
away. Nothing durable carried the outcome back to the button.

The gloss core (``lib/fixer.last_launch``) is unit-tested in ``test_fixer.py``. This file is the
FIXTURE test that drives the shipped VIEW: a real state home, a real journal, the real assembler —
proving that each of the three outcomes reaches the surface the tap was made on.

Two properties are load-bearing:

* **No new server read.** The outcome comes from the ``debug_launch`` acts the assembler ALREADY
  reads for the tower log — the same records, glossed twice, so the banner and the tower can never
  disagree about the same launch. No fixer log is opened, no CLI is shelled, no GitHub is asked.
* **It reaches the BUTTON.** Deploy Fixer lives in the trouble banner (tap-where-you-read, §0.3), so
  the block is folded into ``snapshot["trouble"]`` for the repo the banner is naming — not left on a
  repo slice the owner may not have on camera.
"""
import json
import os
import shutil

import pytest

import fixer as fixer_mod
import readers
import server

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


def _config(home):
    repo = {"slug": SLUG, "owner": "will-titan", "name": "superlooper-sandbox",
            "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
            "required_checks": ["tests"], "airline": "Sandbox Air"}
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300, "repos": [repo]}


def _snap(home):
    return server.assemble_snapshot(_config(home), now=NOW)


def _launch(**over):
    rec = {"ts": NOW - 90, "act": "debug_launch", "outcome": "launched", "id": "d4",
           "operator": "william", "source": "command-center"}
    rec.update(over)
    return rec


# =============================== the failed launch — the 2026-08-26 defect ===============================

def test_a_failed_launch_is_named_at_the_button_with_its_journaled_reason(home):
    reason = "claude auth expired — the session tab refused the prompt"
    _journal(home, _launch(outcome="launch_failed", error=reason))
    snap = _snap(home)

    # The banner is where the button is, so the banner is where the answer has to be.
    trouble = snap["trouble"]
    assert trouble["present"] is True, "the fixture board is unhealthy — the banner must render"
    block = trouble["fixer"]
    assert block["present"] is True
    assert block["outcome"] == fixer_mod.FAILED
    assert reason in block["text"], "the owner must read the reason the ENGINE journaled"
    assert "d4" in block["text"]
    assert block["session"] is False, "nothing launched — there is no window to offer"


def test_the_failed_launch_also_rides_the_repo_slice_it_belongs_to(home):
    _journal(home, _launch(outcome="launch_failed", error="no pane"))
    snap = _snap(home)
    repo_block = snap["repos"][0]["fixer"]
    assert repo_block["outcome"] == fixer_mod.FAILED
    assert repo_block == snap["trouble"]["fixer"], (
        "the banner must show the OFFENDING repo's own block — one derivation, bound twice")


def test_the_outcome_carries_a_wall_clock_so_the_owner_can_place_it(home):
    _journal(home, _launch(outcome="launch_failed", error="no pane"))
    block = _snap(home)["repos"][0]["fixer"]
    assert block["hhmm"], "a stated outcome with no time is a fact the owner cannot place"
    assert block["age_seconds"] == pytest.approx(90, abs=1)


# =============================== the successful launch ===============================

def test_a_successful_launch_renders_the_launched_fixers_identity(home):
    _journal(home, _launch())
    block = _snap(home)["trouble"]["fixer"]
    assert block["outcome"] == fixer_mod.LAUNCHED
    assert block["id"] == "d4"
    assert "d4" in block["text"]
    # The open-session affordance the flight card already has — honest only on a confirmed launch.
    assert block["session"] is True


# =============================== the outcome nobody has named yet ===============================

def test_an_outcome_not_yet_known_renders_as_pending_never_as_silence(home):
    _journal(home, _launch(outcome=None))
    block = _snap(home)["trouble"]["fixer"]
    assert block["present"] is True, "a tap that happened must never render as no tap at all"
    assert block["outcome"] == fixer_mod.PENDING
    assert block["text"]
    assert block["session"] is False


def test_an_outcome_word_a_newer_engine_invents_is_pending_in_that_engines_word(home):
    _journal(home, _launch(outcome="queued"))
    block = _snap(home)["trouble"]["fixer"]
    assert block["outcome"] == fixer_mod.PENDING
    assert "queued" in block["text"]


# =============================== absence, and the newest tap ===============================

def test_a_repo_that_has_never_had_a_tap_says_so_rather_than_inventing_one(home):
    block = _snap(home)["repos"][0]["fixer"]
    assert block["present"] is False
    assert block["text"] == ""
    assert _snap(home)["trouble"]["fixer"]["present"] is False


def test_the_newest_tap_is_the_one_the_banner_reports(home):
    _journal(home,
             _launch(ts=NOW - 900, id="d3", outcome="launch_failed", error="auth expired"),
             _launch(ts=NOW - 60, id="d4"))
    block = _snap(home)["trouble"]["fixer"]
    assert block["id"] == "d4" and block["outcome"] == fixer_mod.LAUNCHED


# =============================== the DoD's no-new-reads line ===============================

def test_the_outcome_comes_from_the_published_view_not_a_new_read(home):
    # The whole block is derived from records the assembler already holds for the tower log. This
    # assembly runs with gh unwired and every external binary neutralized by the conftest, so a
    # snapshot that still carries the outcome cannot have gone anywhere to get it — and the tower
    # log's own sentence about the SAME launch is right there beside it.
    _journal(home, _launch(outcome="launch_failed", error="claude auth expired"))
    snap = _snap(home)
    assert snap["repos"][0]["fixer"]["outcome"] == fixer_mod.FAILED
    tower = [r for r in snap["repos"][0]["tower_log"] if "Fixer" in r["text"]]
    assert tower, "the same record must still gloss into the tower log — one source, two renderings"
    assert "claude auth expired" in tower[0]["text"]


def test_the_journal_is_read_exactly_once_for_the_whole_slice(home, monkeypatch):
    # The sharper half of "no new server reads" (fresh-agent review nit): the guard below proves the
    # dashboard's own fixer log is never opened, but it would have stayed green if the outcome came
    # from a SECOND pass over journal.jsonl. It does not — the block is derived from the very list
    # the tower log is glossed from, so one repo's assembly reads that file exactly once.
    _journal(home, _launch(outcome="launch_failed", error="claude auth expired"))
    real = readers.read_journal
    calls = []

    def counting(h):
        calls.append(h)
        return real(h)

    monkeypatch.setattr(readers, "read_journal", counting)
    snap = _snap(home)
    assert snap["repos"][0]["fixer"]["outcome"] == fixer_mod.FAILED
    assert len(calls) == 1, "the outcome must ride the journal read the tower log already makes"


def test_the_assembler_never_opens_the_dashboards_own_fixer_log(home, monkeypatch, tmp_path):
    # lib/fixer keeps its OWN launch log beside desk.json (decision B.4). Reading it here would be a
    # second source of truth for the same fact — and the new server read the DoD rules out.
    monkeypatch.setenv("SL_HOME", str(tmp_path / "cc-home"))
    opened = []
    real_open = open

    def watching_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    _journal(home, _launch(outcome="launch_failed", error="no pane"))
    monkeypatch.setattr("builtins.open", watching_open)
    _snap(home)
    assert not [p for p in opened if p.endswith("fixer-log.jsonl")], (
        "the snapshot must derive the outcome from the journal it already reads"
    )


# =============================== one record, two surfaces, one reading ===============================

@pytest.mark.parametrize("rec,banner_says,tower_must_not_say", [
    ({"outcome": "launched"}, fixer_mod.LAUNCHED, "did not launch"),
    ({"outcome": "launch_failed", "error": "auth expired"}, fixer_mod.FAILED, "deployed"),
    ({"outcome": None}, fixer_mod.PENDING, "did not launch"),
    ({"outcome": "queued"}, fixer_mod.PENDING, "did not launch"),
])
def test_the_banner_and_the_tower_log_never_contradict_each_other(home, rec, banner_says,
                                                                  tower_must_not_say):
    # Both surfaces gloss the SAME journal record. Before #458 the tower read every outcome that was
    # not the literal word "launched" as "did not launch" — so the moment the banner learned to say
    # "not yet known", the two would have disagreed about one launch in the same 2-second frame.
    # The classification lives in one place (lib/fixer.launch_outcome) precisely so it cannot.
    _journal(home, _launch(**rec))
    snap = _snap(home)
    assert snap["repos"][0]["fixer"]["outcome"] == banner_says
    row = [r for r in snap["repos"][0]["tower_log"] if "d4" in r["text"]][-1]
    assert tower_must_not_say not in row["text"].lower()
