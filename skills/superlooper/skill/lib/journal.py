"""Append-only action journal: <state_home>/journal.jsonl (§C.3).

Every action the runner takes lands here — the decision-log discipline mechanized. The
morning report (Task 11) and the future ratchet read it back, and `superlooper status`
renders from it, so the WRITE path fails loud (a record we can't journal is a bug) while
the READ path fails closed (a half-written crash line must never take down a report).

The journal is the single time authority: append() stamps 'ts' itself (injectable for
tests), overwriting any caller-supplied value — one clock, no forged history. Each record
is one json.dumps line written with a single f.write on a line-buffered append handle, so
concurrent writers (the runner + a stray CLI invocation) interleave line-wise rather than
corrupting each other mid-record.
"""
import json
import os
import time

FILENAME = "journal.jsonl"


def _path(state_home):
    return os.path.join(os.fspath(state_home), FILENAME)


def append(state_home, record, now=None):
    """Append one epoch-stamped record. Raises ValueError on a non-dict record (programmer
    error — garbage must never enter the log); creates the state home if missing; never
    mutates the caller's record."""
    if not isinstance(record, dict):
        raise ValueError(f"journal.append needs a dict record, got {type(record).__name__}")
    out = {"ts": now if now is not None else time.time()}
    out.update({k: v for k, v in record.items() if k != "ts"})
    line = json.dumps(out) + "\n"
    os.makedirs(os.fspath(state_home), exist_ok=True)
    with open(_path(state_home), "a") as f:
        f.write(line)               # one write call per record: line-wise interleaving
        f.flush()
        os.fsync(f.fileno())        # survive a crash right after the action executed


def read(state_home):
    """All records, in file order. Fail closed per line: corrupt JSON, wrong-typed (non-dict)
    entries, and blank lines are skipped; a missing file reads as []."""
    try:
        with open(_path(state_home)) as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
