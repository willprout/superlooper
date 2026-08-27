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
ARCHIVE_FILENAME = "journal-archive.jsonl"

# The hot window rotate() keeps in FILENAME. Older records move to the archive. This MUST exceed
# the widest window the readers use (the morning report's 7-day gate-health/regeneration trend) with
# margin, so read() — the path status and the report share — never loses a record they still need.
HOT_RETAIN_SECONDS = 14 * 24 * 3600


def _path(state_home):
    return os.path.join(os.fspath(state_home), FILENAME)


def _archive_path(state_home):
    return os.path.join(os.fspath(state_home), ARCHIVE_FILENAME)


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


def _read_records(path):
    """Tolerant line reader shared by read()/read_all(): dict records in file order; corrupt JSON,
    wrong-typed (non-dict) entries, and blank lines are skipped; a missing file reads as []."""
    try:
        with open(path) as f:
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


def read(state_home):
    """The HOT window only (FILENAME) — the path status and the morning report share. After
    rotate() has run this is bounded to HOT_RETAIN_SECONDS of history, so both readers' cost is
    independent of total history. Fail closed per line: corrupt/wrong-typed/blank lines skipped;
    a missing file reads as []. (Byte-identical behaviour to before rotation existed — rotate()
    is what makes FILENAME bounded; read() itself is unchanged.)"""
    return _read_records(_path(state_home))


# How much of the hot journal a bounded tail read looks at. Sized by HISTORY, not by rate: its one
# caller (issue #457's launch-attempt streak) reads back to its last verified delivery, so the slice
# has to span however long a machine can go without starting a session — and the 2026-08-26 incident
# put its two independent samples 20 h 16 m apart.
#
# MEASURED, because the first guess was wrong by an order of magnitude: this repo's own archive runs
# 250-270 KB on an ordinary busy day and hit 9.7 MB on 2026-08-04 (a question storm, ~5 KB a
# record). Two megabytes is roughly a week of ordinary days and comfortably past that incident's
# span; a storm can still outrun it, but a storm means the loop is DELIVERING, and a delivery is
# what the caller is looking back to anyway. Truncation past the slice is safe rather than merely
# bounded — a delivery that scrolls off takes every refusal it answered with it (see tail()) — so
# the failure direction is a hold that arrives late, never one that should not have.
#
# Cost is a constant rather than a function of the retention window: ~3 ms per megabyte parsed,
# against a tick measured in tens of seconds.
TAIL_MAX_BYTES = 2 * 1024 * 1024


def tail(state_home, max_bytes=TAIL_MAX_BYTES):
    """The last `max_bytes` of the hot journal, parsed — a bounded read for a per-tick reader.

    The runner learns about launches it did not run — the watchdog's unattended sl-debugger, the
    owner's `superlooper debug` tap — only from this file (issue #457), and it may not re-parse a
    14-day window every ~20 s to do it.

    DELIBERATELY STATELESS: nothing is carried between calls. The caller derives its whole answer
    from the slice each time, which is what lets it be a pure function of the journal rather than a
    running total to be reconciled with one. A byte offset would be wrong here anyway, because this
    file is not append-only over time — `rotate()` rewrites it WITHOUT its archived prefix, and the
    runner rotates on its first tick and every few hours after — so a saved offset lands mid-record
    (silently dropping the record it cut) or past the end.

    TRUNCATING A PREFIX IS SAFE for that caller by construction: its answer is a function of the
    records after the last verified delivery, so a delivery that scrolls off the front takes every
    refusal it answered with it. What the slice must be big enough for is the caller's own history
    (issue #457's streak reaches back to its last delivery, which on a healthy loop is minutes and
    on a dead one is however long it has been dead) — not for any per-tick rate.

    Fails closed exactly like read(): an unreadable file yields [], a corrupt or wrong-typed line is
    skipped, the FIRST line is dropped when the byte seek landed INSIDE it (checked, not assumed),
    and a TRAILING partial line is left unparsed rather than parsed in halves. (append() writes each record with a
    single write+flush+fsync, so a partial tail is rare; being wrong about it once would mis-read a
    record, which is why it is handled.)
    """
    path = _path(state_home)
    try:
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
            except OSError:                             # pragma: no cover - defensive
                size = 0
            start = max(0, size - int(max_bytes))
            # Is `start` already a record boundary? Reading the byte BEFORE it answers that, and it
            # is worth one seek: without the check the first line is dropped unconditionally, which
            # throws away a COMPLETE record whenever the arithmetic happens to land on a newline.
            # The streak this feeds trips on two samples, so one lost record is a hold that arrives
            # late — cheap to prevent, awkward to reproduce.
            aligned = start == 0
            if not aligned:
                f.seek(start - 1)
                aligned = f.read(1) == b"\n"
            f.seek(start)
            blob = f.read()
    except OSError:
        return []
    consumed = blob.rfind(b"\n") + 1                    # 0 when no COMPLETE line is present
    # split("\n"), never splitlines(): str.splitlines also breaks on \x0b/\x0c/\x1c/\x85/\u2028,
    # which would cut ONE record into two unparseable halves. append() writes ensure_ascii JSON, so
    # none of those can appear raw today — but _read_records splits on \n only, and two readers of
    # one file that disagree about what a line is would be a defect nobody could reproduce.
    lines = blob[:consumed].decode("utf-8", "replace").split("\n")
    if not aligned and len(lines) > 1:
        lines = lines[1:]                               # the seek landed mid-record. Only when
                                                        # something survives it: a slice holding ONE
                                                        # line is either that record whole or
                                                        # unparseable, and dropping it unread would
                                                        # cost the caller its whole answer.
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_all(state_home):
    """Every record ever logged — archived first, then the hot window — for audit/forensics. NOT
    the hot path: its cost grows with total history BY DESIGN (that is what read() avoids). Same
    per-line tolerance as read()."""
    return _read_records(_archive_path(state_home)) + read(state_home)


def _record_ts(line):
    """The numeric ts of a raw journal line, or None if the line is corrupt / ts is missing or
    wrong-typed. bool is not a timestamp (True is an int subclass)."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    t = rec.get("ts")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def rotate(state_home, now, retain_seconds=HOT_RETAIN_SECONDS):
    """Move records older than `now - retain_seconds` out of the hot FILENAME into the append-only
    ARCHIVE_FILENAME beside it, keeping the recent window hot. This is what bounds read() (and thus
    status/report read time) to the window, independent of total history. Returns the number of
    records archived. Never raises on a missing hot file (returns 0).

    Guarantees:
      * Nothing is deleted — the archive only grows; every archived record survives BYTE-INTACT
        because the original line string is moved verbatim (never re-serialized).
      * Crash-safe ordering: the old records are appended (fsync) to the archive FIRST, then the
        hot file is atomically rewritten (temp + fsync + os.replace) with the kept records. A crash
        between the two steps can at worst DUPLICATE records into the archive (a harmless audit
        dup — read() sees only the hot copy), never lose one.
      * A corrupt line (no parseable ts — the tolerant reader already skips it) is KEPT hot, never
        archived and never dropped.

    Caveat worth stating since #457: a record lost in the concurrency window below is no longer
    audit-only — the runner's machine-wide launch-attempt streak reads this file back through
    tail(). Losing a REFUSAL costs one sample of an outage that is still refusing every launch, and
    the hold arrives a tick later. Losing a foreign spawner's verified DELIVERY (`launched` /
    `resumed`) is the direction that costs something: the streak keeps refusals that delivery
    already answered, so a hold persists or re-arms over a machine that did start a session. Both
    are bounded by the next flight — the #115 probe clears the streak on its own — and the window
    is milliseconds, four times a day, against a runner that is this file's dominant writer.

    Concurrency caveat: unlike append() (lock-free, O_APPEND-atomic across processes), the read ->
    os.replace here is NOT atomic against a CONCURRENT appender in another process (watchdog / a
    stray CLI). A record appended between this call's readlines() and its os.replace() would be
    overwritten. The runner is the journal's dominant writer and the sole rotate caller (4x/day, a
    ms-scale window), and the one runner decision that reads this file back is bounded either way by
    its own recovery probe (see the note above) — so this is an accepted narrow window, not a safety
    risk; call rotate only from the runner tick.

    Records are partitioned by `ts`: ts < cutoff -> archived; everything else stays hot."""
    cutoff = now - retain_seconds
    path = _path(state_home)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return 0
    old, keep = [], []
    for line in lines:
        ts = _record_ts(line)
        (old if (ts is not None and ts < cutoff) else keep).append(line)
    if not old:
        return 0
    # archive-first (durable) so a crash can only ever duplicate, never drop
    with open(_archive_path(state_home), "a") as f:
        f.writelines(old)
        f.flush()
        os.fsync(f.fileno())
    # atomic hot rewrite with the kept tail
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(keep)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return len(old)
