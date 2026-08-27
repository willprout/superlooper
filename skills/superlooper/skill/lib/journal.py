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


# How much of the hot journal a bounded tail read looks at. Big enough that a ~20 s tick can never
# outrun it (a burst would have to write a quarter-megabyte of records between two ticks), small
# enough that the read cost is a constant rather than a function of the retention window.
TAIL_MAX_BYTES = 256 * 1024


def tail(state_home, max_bytes=TAIL_MAX_BYTES):
    """The last `max_bytes` of the hot journal, parsed — a bounded read for a per-tick reader.

    The runner learns about launches it did not run — the watchdog's unattended sl-debugger, the
    owner's `superlooper debug` tap — only from this file (issue #457), and it may not re-parse a
    14-day window every ~20 s to do it.

    DELIBERATELY STATELESS: no byte offset is carried between calls, and the caller filters what it
    has already seen by RECORD (its own `ts` watermark). An offset would be wrong here, because the
    file this follows is not append-only over time — `rotate()` rewrites it WITHOUT its archived
    prefix, and the runner rotates on its first tick and every few hours after. A saved offset then
    indexes a different logical position: it either lands mid-record (silently dropping the record
    it cut, which is exactly the record class this detector is built on) or past the end. Re-reading
    a bounded tail every tick costs a constant few hundred KB and cannot be wrong about either.

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
    if not aligned and lines:
        lines = lines[1:]                               # the seek landed mid-record
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
    tail(). Losing one there costs at most one SAMPLE of an outage that is still refusing every
    launch, so it fails OPEN (a hold arrives a tick later, never a hold that should not have).

    Concurrency caveat: unlike append() (lock-free, O_APPEND-atomic across processes), the read ->
    os.replace here is NOT atomic against a CONCURRENT appender in another process (watchdog / a
    stray CLI). A record appended between this call's readlines() and its os.replace() would be
    overwritten. The runner is the journal's dominant writer and the sole rotate caller (4x/day, a
    ms-scale window), and the only runner decision that reads this file back fails OPEN on a lost
    record (see the note above) — so this is an accepted narrow window, not a safety risk; call
    rotate only from the runner tick.

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
