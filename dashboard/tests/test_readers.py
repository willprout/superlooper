"""Task 2 — loop-state readers (pure, fail-tolerant).

These tests pin the contract of ``lib/readers.py``: turn a superlooper state home's files into a
plain facts dict, tolerating every missing/corrupt/half-written file without raising. Shapes come
from ``design/project/uploads/sample-data.txt`` (never invented) and the committed fixture under
``tests/fixtures/statehome/``. Time-sensitive facts (heartbeat age, activity mtimes) are exercised
against tmp homes we build with controlled mtimes + an injected ``now`` — git does not preserve a
checked-out file's mtime, so the committed fixture can only pin shape/presence, not clocks.
"""
import os
from pathlib import Path

import readers

FIX = Path(__file__).parent / "fixtures" / "statehome"

# Deeply nested JSON: json.loads() recurses and raises RecursionError (not a JSONDecodeError),
# which a naive parser lets escape. A half-written/adversarial file must never crash a reader.
_DEEP_JSON = "[" * 20000 + "0" + "]" * 20000


# --------------------------- read_journal (tolerant) ---------------------------

def test_read_journal_returns_only_valid_dict_records_in_file_order():
    recs = readers.read_journal(FIX)
    # 6 real dict lines; the blank line, the half-written crash line, the JSON array and the
    # bare scalar are all skipped.
    assert [r["act"] for r in recs] == [
        "launch", "relabel", "event", "merge", "regenerate", "park"]
    assert recs[0]["id"] == "i23"
    assert recs[-1]["id"] == "i7"


def test_read_journal_preserves_nested_and_typed_values():
    recs = readers.read_journal(FIX)
    assert recs[2]["event"]["type"] == "session_blocked"   # nested dict survives
    assert recs[3]["act"] == "merge" and recs[3]["wander"] is True   # a wandered merge


def test_read_journal_skips_non_dict_json():
    # every returned record is a dict — the "[1, 2, 3]" and "42" lines never leak through.
    assert all(isinstance(r, dict) for r in readers.read_journal(FIX))


def test_read_journal_missing_file_is_empty_list(tmp_path):
    assert readers.read_journal(tmp_path) == []


def test_read_journal_all_corrupt_is_empty_list(tmp_path):
    (tmp_path / "journal.jsonl").write_text("not json\n\n[1,2]\n{oops\n")
    assert readers.read_journal(tmp_path) == []


# --------------------------- tail_journal (log window) ---------------------------

def test_tail_journal_returns_last_n_valid_records_in_order():
    assert [r["act"] for r in readers.tail_journal(FIX, 2)] == ["regenerate", "park"]


def test_tail_journal_limit_larger_than_history_returns_all():
    assert len(readers.tail_journal(FIX, 100)) == len(readers.read_journal(FIX)) == 6


def test_tail_journal_skips_corrupt_when_filling_the_window(tmp_path):
    # A window of 2 must yield 2 *valid* records even when a corrupt line sits among them.
    (tmp_path / "journal.jsonl").write_text(
        '{"act": "a"}\ngarbage\n{"act": "b"}\n[1]\n{"act": "c"}\n')
    assert [r["act"] for r in readers.tail_journal(tmp_path, 2)] == ["b", "c"]


def test_tail_journal_nonpositive_limit_is_empty():
    assert readers.tail_journal(FIX, 0) == []
    assert readers.tail_journal(FIX, -5) == []


def test_tail_journal_missing_file_is_empty_list(tmp_path):
    assert readers.tail_journal(tmp_path, 5) == []


def test_journal_readers_survive_deeply_nested_line(tmp_path):
    # The pathological line is dropped like any other corrupt line; the real records survive and
    # neither reader raises RecursionError.
    (tmp_path / "journal.jsonl").write_text(
        '{"act": "a"}\n' + _DEEP_JSON + '\n{"act": "b"}\n')
    assert [r["act"] for r in readers.read_journal(tmp_path)] == ["a", "b"]
    assert [r["act"] for r in readers.tail_journal(tmp_path, 5)] == ["a", "b"]


# --------------------------- read_state_home: shapes ---------------------------

def test_state_home_returns_every_contract_key():
    facts = readers.read_state_home(FIX)
    assert set(facts) == {
        "issues_state", "activity", "blocked", "exited", "awaiting",
        "heartbeat_epoch", "heartbeat_age", "merges_frozen", "alert", "reports"}


def test_state_home_issues_json_content():
    facts = readers.read_state_home(FIX)
    st = facts["issues_state"]
    assert st["version"] == 1
    assert st["issues"]["i16"]["conflicts"] == 1
    assert st["issues"]["i16"]["pr"] == 19
    assert st["issues"]["i21"]["status"] == "parked"


def test_state_home_activity_is_per_issue_mtimes():
    facts = readers.read_state_home(FIX)
    assert set(facts["activity"]) == {"i23", "i16"}
    assert all(isinstance(v, float) for v in facts["activity"].values())


def test_state_home_markers_preserve_raw_text_including_bounced_prefix():
    facts = readers.read_state_home(FIX)
    assert "motto" in facts["blocked"]["i5"]
    assert facts["blocked"]["i8"].startswith("BOUNCED:")   # classification is NOT the reader's job
    assert "rc=1" in facts["exited"]["i21"]
    assert facts["awaiting"]["i9"] == ""                   # awaiting is a touch (empty) marker


def test_state_home_merges_frozen_and_alert_present():
    facts = readers.read_state_home(FIX)
    assert facts["merges_frozen"]["source"] == "dev-check"
    assert "runner heartbeat stale" in facts["alert"]["reasons"]


def test_state_home_reports_presence_excludes_morning_digest():
    # per-issue reports only; the morning-<date>.md digest is not a flight's report.
    assert readers.read_state_home(FIX)["reports"] == ["i16", "i23"]


def test_state_home_reports_presence_requires_md_file(tmp_path):
    # The documented shape is reports/<id>.md — a directory named like an id, or an extensionless
    # file, is NOT a report and must not be counted.
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "i5.md").write_text("real report")
    (reports / "i7").write_text("no extension — not a report")
    (reports / "i9").mkdir()                       # a directory that happens to look like an id
    assert readers.read_state_home(tmp_path)["reports"] == ["i5"]


def test_state_home_deeply_nested_issues_json_never_raises(tmp_path):
    (_state(tmp_path) / "issues.json").write_text(_DEEP_JSON)
    # unreadable (even pathologically) issue state fails open to {}, never a RecursionError.
    assert readers.read_state_home(tmp_path, now=1300)["issues_state"] == {}


# --------------------------- read_state_home: clocks (tmp) ---------------------------

def _state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_heartbeat_epoch_and_age_from_injected_now(tmp_path):
    (_state(tmp_path) / "runner.heartbeat").write_text("1000\n")
    facts = readers.read_state_home(tmp_path, now=1300)
    assert facts["heartbeat_epoch"] == 1000
    assert facts["heartbeat_age"] == 300.0


def test_heartbeat_missing_is_none(tmp_path):
    facts = readers.read_state_home(tmp_path, now=1300)
    assert facts["heartbeat_epoch"] is None
    assert facts["heartbeat_age"] is None


def test_heartbeat_unparseable_is_none(tmp_path):
    (_state(tmp_path) / "runner.heartbeat").write_text("not-a-number")
    facts = readers.read_state_home(tmp_path, now=1300)
    assert facts["heartbeat_epoch"] is None
    assert facts["heartbeat_age"] is None


def test_heartbeat_age_uses_wall_clock_when_now_omitted(tmp_path):
    (_state(tmp_path) / "runner.heartbeat").write_text("1000000000")   # far in the past
    facts = readers.read_state_home(tmp_path)
    assert isinstance(facts["heartbeat_age"], float) and facts["heartbeat_age"] > 0


def test_activity_mtimes_are_raw_file_mtimes(tmp_path):
    act = _state(tmp_path) / "activity"
    act.mkdir()
    (act / "i5").write_text("")
    os.utime(act / "i5", (500.0, 500.0))
    facts = readers.read_state_home(tmp_path, now=9999)
    assert facts["activity"]["i5"] == 500.0   # raw mtime, NOT an age (age lives in the flight model)


# --------------------------- read_state_home: fail-tolerance ---------------------------

def test_missing_home_never_raises_and_returns_empty_defaults(tmp_path):
    facts = readers.read_state_home(tmp_path / "does-not-exist", now=1300)
    assert facts["issues_state"] == {}
    assert facts["activity"] == {}
    assert facts["blocked"] == {} and facts["exited"] == {} and facts["awaiting"] == {}
    assert facts["heartbeat_epoch"] is None and facts["heartbeat_age"] is None
    assert facts["merges_frozen"] is None      # absent ⇒ NOT frozen
    assert facts["alert"] is None
    assert facts["reports"] == []


def test_undecodable_marker_bytes_never_raise(tmp_path):
    # Marker text is worker-written and can carry any bytes; reading it must never crash the poll
    # loop, regardless of the CI machine's locale. Invalid UTF-8 degrades to best-effort text.
    blocked = _state(tmp_path) / "blocked"
    blocked.mkdir()
    (blocked / "i3").write_bytes(b"\xff\xfe question with bad bytes")
    facts = readers.read_state_home(tmp_path, now=1300)
    assert isinstance(facts["blocked"]["i3"], str)


def test_corrupt_files_never_raise_and_fail_closed_where_existence_means_state(tmp_path):
    st = _state(tmp_path)
    (st / "issues.json").write_text("{ this is not json")
    (st / "merges_frozen.json").write_text("garbage")
    (st / "ALERT").write_text("garbage")
    (st / "runner.heartbeat").write_text("garbage")
    facts = readers.read_state_home(tmp_path, now=1300)
    assert facts["issues_state"] == {}          # unreadable issue state ⇒ empty, not a crash
    assert facts["heartbeat_epoch"] is None
    # existence == frozen / alerting: a corrupt-but-present marker still counts (fail closed).
    assert facts["merges_frozen"] == {}
    assert facts["alert"] == {}
