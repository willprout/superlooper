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


# --------------------------- rotation / archival (issue #41) ---------------------------
#
# The journal is append-only forever and read WHOLE by `status` and the morning report, so both
# slow with age. rotate() moves records older than a hot window out to journal-archive.jsonl beside
# it, so read() (the hot path both readers use) stays bounded to the window — independent of total
# history — while every archived record survives byte-intact, findable via read_all(). Nothing is
# ever deleted; the archive only grows.


def _hot_lines(tmp_path):
    return (tmp_path / journal.FILENAME).read_text().splitlines()


def test_rotate_archives_old_records_and_keeps_recent_hot(tmp_path):
    old = 1_000_000
    keep = old + journal.HOT_RETAIN_SECONDS + 10          # inside the hot window
    journal.append(tmp_path, {"act": "ancient"}, now=old)
    journal.append(tmp_path, {"act": "recent"}, now=keep)
    n = journal.rotate(tmp_path, now=keep + journal.HOT_RETAIN_SECONDS - 5)
    assert n == 1                                          # exactly the ancient record archived
    # The hot file — what read() (status/report) sees — now holds only the recent record.
    assert [r["act"] for r in journal.read(tmp_path)] == ["recent"]
    # The ancient record is not lost: it is in the archive, and read_all() sees both, archive first.
    assert [r["act"] for r in journal.read_all(tmp_path)] == ["ancient", "recent"]


def test_rotate_preserves_records_byte_intact(tmp_path):
    # The audit contract: a rotated record is BYTE-IDENTICAL to what was logged (rotate moves the
    # original line verbatim, never re-serializes). Capture the exact hot bytes for the to-be-
    # archived records, rotate, and assert the archive file holds those exact bytes.
    base = 2_000_000
    journal.append(tmp_path, {"act": "a", "id": "i1", "num": 7}, now=base)
    journal.append(tmp_path, {"act": "b", "id": "i2", "detail": "café ☕"}, now=base + 1)
    original_bytes = (tmp_path / journal.FILENAME).read_bytes()   # both records, exact bytes
    # Rotate with a cutoff that archives BOTH records.
    journal.rotate(tmp_path, now=base + journal.HOT_RETAIN_SECONDS + 100)
    archive_bytes = (tmp_path / journal.ARCHIVE_FILENAME).read_bytes()
    assert archive_bytes == original_bytes                # byte-for-byte, unicode and all
    assert journal.read(tmp_path) == []                   # hot emptied of the archived records


def test_read_time_is_independent_of_total_history(tmp_path):
    # The whole point: read() cost tracks the HOT window, not total history. A huge archive plus a
    # tiny hot file must make read() return only the hot records (so its work is bounded by them).
    huge = "\n".join(json.dumps({"ts": i, "act": "archived"}) for i in range(5000)) + "\n"
    (tmp_path / journal.ARCHIVE_FILENAME).write_text(huge)
    journal.append(tmp_path, {"act": "hot1"}, now=9_000_000)
    journal.append(tmp_path, {"act": "hot2"}, now=9_000_001)
    assert [r["act"] for r in journal.read(tmp_path)] == ["hot1", "hot2"]     # only the hot 2
    assert len(journal.read_all(tmp_path)) == 5002                             # archive is findable


def test_rotate_is_a_noop_when_nothing_is_old_enough(tmp_path):
    now = 5_000_000
    journal.append(tmp_path, {"act": "fresh"}, now=now)
    assert journal.rotate(tmp_path, now=now + 10) == 0
    assert not (tmp_path / journal.ARCHIVE_FILENAME).exists()     # no archive created
    assert [r["act"] for r in journal.read(tmp_path)] == ["fresh"]


def test_rotate_tolerates_a_missing_journal(tmp_path):
    assert journal.rotate(tmp_path / "nowhere", now=1) == 0       # never raises, archives nothing


def test_rotate_keeps_corrupt_lines_hot_never_dropping_them(tmp_path):
    # A crash-corrupted line has no parseable ts; rotation must not silently drop it (never delete
    # audit data) — it stays in the hot file, where the tolerant reader already skips it.
    old = 3_000_000
    journal.append(tmp_path, {"act": "ancient"}, now=old)
    with open(tmp_path / journal.FILENAME, "a") as f:
        f.write("{corrupt half line\n")
    journal.append(tmp_path, {"act": "recent"}, now=old + journal.HOT_RETAIN_SECONDS + 100)
    journal.rotate(tmp_path, now=old + journal.HOT_RETAIN_SECONDS + 200)
    raw = _hot_lines(tmp_path)
    assert "{corrupt half line" in raw                    # corrupt line preserved in hot
    assert [r["act"] for r in journal.read(tmp_path)] == ["recent"]    # ancient archived, corrupt skipped


def test_rotate_appends_to_an_existing_archive(tmp_path):
    # Two rotations accumulate into the same archive (append-only), never clobber the first.
    a = 4_000_000
    journal.append(tmp_path, {"act": "first"}, now=a)
    journal.rotate(tmp_path, now=a + journal.HOT_RETAIN_SECONDS + 10)
    journal.append(tmp_path, {"act": "second"}, now=a + 100)
    journal.rotate(tmp_path, now=a + 100 + journal.HOT_RETAIN_SECONDS + 10)
    assert [r["act"] for r in journal.read_all(tmp_path)] == ["first", "second"]


def test_read_all_is_empty_when_nothing_written(tmp_path):
    assert journal.read_all(tmp_path / "nowhere") == []
