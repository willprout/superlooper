"""journal.py — the append-only decision log (plan Task 10).

Every action the runner takes is journaled to <state_home>/journal.jsonl: this is the
decision-log discipline mechanized, and the morning report (Task 11) + the future ratchet read
it back. Contract under test:

  append(state_home, record, now=None) — one JSON line, epoch-stamped by THE JOURNAL (single
    time authority: any caller-supplied 'ts' is overwritten), atomic single-write, creates the
    state home if missing, never mutates the caller's record. A non-dict record raises loudly
    (a programmer error, not a runtime shape to tolerate — same posture as loopstate's S6 guard).

  read(state_home) — the tolerant reader: list of dicts in file order; corrupt/partial lines
    and wrong-typed (non-dict) lines are SKIPPED, never raised (a half-written crash line must
    not take down the morning report); missing file -> [].
"""
import json

import pytest

import journal


def test_append_writes_one_stamped_jsonl_line(tmp_path):
    journal.append(tmp_path, {"act": "launch", "id": "i5"}, now=1000)
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec == {"ts": 1000, "act": "launch", "id": "i5"}


def test_append_appends_in_order_and_reader_round_trips(tmp_path):
    journal.append(tmp_path, {"act": "a"}, now=1)
    journal.append(tmp_path, {"act": "b"}, now=2)
    recs = journal.read(tmp_path)
    assert [r["act"] for r in recs] == ["a", "b"]
    assert [r["ts"] for r in recs] == [1, 2]


def test_append_stamps_wall_clock_when_now_omitted(tmp_path):
    journal.append(tmp_path, {"act": "x"})
    rec = journal.read(tmp_path)[0]
    assert isinstance(rec["ts"], (int, float)) and rec["ts"] > 1_500_000_000


def test_append_is_the_single_time_authority(tmp_path):
    # A caller-supplied ts is overwritten — one clock, no forged history.
    journal.append(tmp_path, {"act": "x", "ts": 42}, now=1000)
    assert journal.read(tmp_path)[0]["ts"] == 1000


def test_append_does_not_mutate_the_callers_record(tmp_path):
    rec = {"act": "launch", "id": "i5"}
    journal.append(tmp_path, rec, now=1000)
    assert rec == {"act": "launch", "id": "i5"}      # no injected ts (shared-mutation defense)


def test_append_creates_state_home_if_missing(tmp_path):
    home = tmp_path / "deep" / "state_home"
    journal.append(home, {"act": "x"}, now=1)
    assert journal.read(home) == [{"ts": 1, "act": "x"}]


def test_append_rejects_a_non_dict_record(tmp_path):
    # Fail LOUD on wrong-typed input from our own code — never write garbage into the log.
    for bad in (None, "act", ["a"], 42):
        with pytest.raises(ValueError):
            journal.append(tmp_path, bad, now=1)
    assert not (tmp_path / "journal.jsonl").exists()


def test_reader_skips_corrupt_and_wrong_typed_lines(tmp_path):
    journal.append(tmp_path, {"act": "good1"}, now=1)
    with open(tmp_path / "journal.jsonl", "a") as f:
        f.write('{"half": "written\n')          # crash mid-write
        f.write('"just a string"\n')            # valid JSON, wrong type
        f.write("\n")                           # blank
    journal.append(tmp_path, {"act": "good2"}, now=2)
    assert [r["act"] for r in journal.read(tmp_path)] == ["good1", "good2"]


def test_reader_returns_empty_for_missing_file(tmp_path):
    assert journal.read(tmp_path / "nowhere") == []
