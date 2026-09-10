"""The dashboard's logbook (issue #481) — every log line dated, the file bounded, floods counted.

On 2026-09-09 ``command-center.log`` reached **331 MB / 3,386,416 lines**, of which 3,382,6xx were
one sentence repeating — ``Python(<pid>) MallocStackLogging: can't turn off malloc stack logging
because it was not enabled.`` — at ~1.6 lines/second, and the file carried **no timestamps**, so
the flood could not even be dated. The ~3,700 lines that mattered (282 client-disconnect
tracebacks, 247 ``RUNNER DOWN push``, 5 ``port … already in use``) were buried inside it. Evidence
record: issue #477 item 1.

This module is the cure, and it is deliberately **source-agnostic**. Why the noise existed at all
(a ``MallocStackLogging`` state inherited from the interactive session the dashboard happened to be
started from, rather than from a login shell) is out of scope for #481 and, more importantly,
unknowable from inside the process. What IS knowable is where the noise lands, and that is the one
thing worth building against:

**Capture.** Every ``subprocess.run`` in ``lib/`` already passes ``capture_output=True``, and the
flood still reached the log — because a line written to the *inherited* fd 2 (before a child's own
``dup2``, or by anything else still holding it) never passes through a capture pipe. So the
dashboard takes over its own stdout and stderr: :func:`install` replaces fds 1 and 2 with a pipe,
reads that pipe on one daemon thread, and writes what it reads — timestamped and collapsed — to
the fd the launcher actually gave it. Anything any descendant can print, the pump can see.

**Bound.** The launcher owns the file, not us: ``bin/liftoff`` opens
``<state-home>/command-center.log`` append-only and hands it over as fd 1/2, and the launchd job
does the same with ``~/Library/Logs/command-center.log``. We never learn that path, and the fd is
write-only — so the cap cannot rename the file or re-read it. :class:`BoundedSink` truncates it in
place (precisely what the operator did by hand on 2026-09-09 — ``: > …``, fd kept valid) and
rewrites a tail it kept in memory, leaving a marker line that names the bytes it dropped. The bound
is therefore hard, documented and checkable: **the file never exceeds** :data:`MAX_BYTES`.

**Count.** :class:`Collapser` keys each line on a *fingerprint* — the sentence with numbers and hex
addresses scrubbed — because #477's flood differed only in its pid and an exact-string dedup would
have collapsed nothing. The first sighting goes in verbatim (a collapsed flood must still be
diagnosable); repeats are suppressed and summarised once per :data:`COLLAPSE_SECONDS` window as
``… repeated N× in Ms, suppressed``. A 24-hour flood is then ~1,440 lines instead of ~138,000, and
every suppressed occurrence is accounted for.

Stdlib only, Python 3.9-compatible, and it never raises into the writer: the pump is the log's
single writer, so a fault in it must degrade to "write straight to the file" (:meth:`Handle.restore`)
rather than leave every writer in the process blocked on a full pipe.
"""
import atexit
import errno
import os
import re
import select
import signal
import stat
import sys
import threading
import time
from collections import OrderedDict, deque

# ---------------------------------------------------------------------------------------------
# The documented bounds. MAX_BYTES is the promise the README quotes: the dashboard's log file can
# never exceed it. KEEP_BYTES is how much of the recent past survives a capping — the lines you
# came to the log to read. Both are overridable by the operator through the environment (the CC_*
# convention already used for CC_CONFIG / CC_LAUNCHD_DIR) so a 331 MB surprise never needs a code
# change to answer, but the defaults are what ships.
# ---------------------------------------------------------------------------------------------
MAX_BYTES = 8 * 1024 * 1024      # 8 MiB — ~40× the useful content of the 2026-09-09 log
KEEP_BYTES = 256 * 1024          # 256 KiB of the most recent lines survive each capping
COLLAPSE_SECONDS = 60.0          # at most one summary line per repeating message per minute
MAX_KEYS = 512                   # distinct messages tracked for collapsing (bounded memory)
MAX_PARTIAL = 16 * 1024          # a child writing without newlines is flushed at this length
# A floor under the cap. The capping marker is a mandatory ~160-byte line, so a configured cap
# below that could not be honoured by any rewrite — the bound would be a promise the code cannot
# keep (fresh-agent review, issue #481). 4 KiB is the smallest cap that can hold a marker plus a
# useful handful of lines; anything smaller is a typo, not a preference.
MIN_MAX_BYTES = 4 * 1024

# Local time with its UTC offset: the operator reads this log against his own clock and against the
# runner's panes, and `time.localtime` is the house style (lib/digest, lib/replay). The offset makes
# it unambiguous, and the format sorts lexically within a zone.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} ")
# Fingerprint scrubbing: hex addresses first (so 0x7ff8 does not become 0x# only in part), then any
# run of digits. What is left is the sentence — the thing that makes two lines "the same message".
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

_MAX_ECHO = 400   # how much of a repeating line a summary quotes back
# Room reserved for the cap marker itself when the retained tail is sized: a timestamp,
# the sentence, and two byte counts. Generous, so the marker can never be the thing that
# pushes a freshly capped file back over its own bound.
_MARKER_ALLOWANCE = 256

# Everything the dashboard itself writes carries this prefix (server.py, the launcher,
# the cap marker). It is what tells our own sentences apart from a descendant's output.
OWN_PREFIX = "command-center: "


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if v > 0 else default


def fingerprint(line):
    """The identity of a *foreign* log message, ignoring its per-occurrence noise.

    #477's flood was ``Python(59258) MallocStackLogging: …`` then ``Python(59261) …`` then
    ``Python(59264) …`` — three million lines that were the same message and no two of them equal
    as strings. Scrubbing digits and hex addresses is what makes them one key.

    Scrubbing numbers is lossy by design, so it is applied only to output the dashboard did not
    write (see :func:`collapse_key`): a foreign line's numbers are noise we have to bound, but two
    of OUR sentences that differ by a digit are two different events."""
    s = _HEX_RE.sub("0x#", line)
    s = _NUM_RE.sub("#", s)
    return _WS_RE.sub(" ", s).strip()


def collapse_key(line):
    """What the collapser actually keys on — and the one place the lossy half is fenced off.

    The dashboard's own lines are prefixed ``command-center: ``, and they repeat *verbatim* when
    they repeat at all (282 identical client-disconnect lines, 247 ``RUNNER DOWN push`` lines). So
    they are keyed on the exact string: two repos whose slugs differ only by a digit —
    ``[org/app-1]`` and ``[org/app-2]`` — stay two events, never one merged count (fresh-agent
    review, issue #481). Everything else is a descendant's output we do not control, which is
    exactly where number-scrubbing has to happen for a varying-pid flood to collapse at all."""
    return line if line.startswith(OWN_PREFIX) else fingerprint(line)


# =============================== BoundedSink — the hard size cap ===============================

class BoundedSink:
    """A write-only, size-capped view of the log file the launcher handed us as fd 1/2.

    The cap is enforced by truncating the file **in place**, because that is the only move
    available: we hold a write-only fd whose path we were never told (``bin/liftoff`` opened it;
    launchd opened the other one), so there is nothing to rename and nothing to read back. A
    bounded in-memory tail of the most recent lines is rewritten after each truncation, so capping
    costs the *oldest* history and never the newest — the operator's next ``tail`` still answers.

    The file's real size is adopted at construction, not assumed to be zero: on 2026-09-09 the
    331 MB file was already on disk when the dashboard restarted, and a sink that counted only its
    own writes would have left it there, growing forever.

    A non-regular fd — a terminal for a foreground ``bin/command-center``, a pipe under a test
    harness — cannot be truncated. The sink then degrades to plain writing rather than raising:
    it is the log's single writer, and a raise here would take the whole log with it (a terminal
    has its own scrollback bound anyway)."""

    def __init__(self, fd, max_bytes=None, keep_bytes=None, clock=None):
        self._fd = fd
        self._max = max_bytes if max_bytes is not None else _env_int("CC_LOG_MAX_BYTES", MAX_BYTES)
        self._max = max(self._max, MIN_MAX_BYTES)
        self._keep = keep_bytes if keep_bytes is not None else _env_int("CC_LOG_KEEP_BYTES",
                                                                       KEEP_BYTES)
        self._keep = min(self._keep, max(1, self._max // 2))
        self._clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._tail = deque()          # most recent lines, trimmed to _keep bytes
        self._tail_bytes = 0
        self._cappable = self._is_regular_file()
        self._size = self._file_size()

    # ------------------------------------------------------------------ probing the fd
    def _is_regular_file(self):
        try:
            return stat.S_ISREG(os.fstat(self._fd).st_mode)
        except OSError:
            return False

    def _file_size(self):
        if not self._cappable:
            return 0
        try:
            return os.fstat(self._fd).st_size
        except OSError:
            return 0

    # ------------------------------------------------------------------ writing
    def write(self, text):
        """Write ``text`` (already timestamped), then cap the file if it has passed the bound."""
        if not text:
            return
        data = text.encode("utf-8", "replace")
        with self._lock:
            self._raw_write(data)
            self._remember(data)
            if self._cappable and self._size >= self._max:
                self._cap()

    def _raw_write(self, data):
        try:
            written = os.write(self._fd, data)
        except OSError:
            return          # a gone log must never take the process with it
        self._size += written

    def _remember(self, data):
        self._tail.append(data)
        self._tail_bytes += len(data)
        while self._tail_bytes > self._keep and len(self._tail) > 1:
            self._tail_bytes -= len(self._tail.popleft())

    def _fit_tail(self, budget):
        """Trim the retained tail to ``budget`` bytes, so marker + tail can never breach the cap.

        ``_remember`` always keeps at least one chunk, even one larger than ``_keep`` (a child can
        write a 16 KiB line with no newline), and a small configured ``CC_LOG_MAX_BYTES`` would
        then let the rewrite land back over the bound. "Never exceeds MAX_BYTES" is the documented
        promise, so it is enforced here rather than assumed (fresh-agent review, issue #481)."""
        chunks = list(self._tail)
        total = sum(len(c) for c in chunks)
        while chunks and total > budget:
            total -= len(chunks.pop(0))
        if not chunks and budget > 0 and self._tail:
            last = self._tail[-1]
            chunks = [last[-budget:]]           # keep the newest bytes of an oversized line
        return chunks

    def _cap(self):
        """Truncate to zero and rewrite the retained tail behind one honest marker line."""
        old_size = self._size
        try:
            os.ftruncate(self._fd, 0)
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
            except OSError as e:
                if e.errno not in (errno.ESPIPE, errno.EINVAL):
                    raise
        except OSError:
            self._cappable = False   # cannot be capped after all; stop pretending it can
            self._size = 0
            return
        # Fit the tail to what the cap has left after a generous marker allowance, THEN count what
        # was dropped — as `old_size - kept`, not `old_size - tail_bytes`. `_fit_tail` may itself
        # discard part of the tail (an oversized retained chunk), and a marker that reported the
        # pre-trim figure would understate the loss, sometimes as zero (fresh-agent review, #481).
        chunks = self._fit_tail(max(0, self._max - _MARKER_ALLOWANCE))
        kept = sum(len(c) for c in chunks)
        dropped = max(0, old_size - kept)
        marker = ("%s command-center: log capped at %d bytes — dropped the oldest %d bytes, kept "
                  "the most recent %d (bound: CC_LOG_MAX_BYTES)\n"
                  % (stamp(self._clock()), self._max, dropped, kept))
        self._size = 0
        self._raw_write(marker.encode("utf-8", "replace"))
        for chunk in chunks:
            self._raw_write(chunk)
        self._tail = deque(chunks)
        self._tail_bytes = kept

    def size(self):
        return self._size


# =============================== Collapser — floods become counts ===============================

class _Repeat:
    __slots__ = ("suppressed", "first_at", "window_at", "sample")

    def __init__(self, now, sample):
        self.suppressed = 0
        self.first_at = now
        self.window_at = now
        self.sample = sample


class Collapser:
    """Turn a repeating line into a bounded, counted record.

    ``feed(line)`` returns the lines that should actually be written: the first sighting of a
    message verbatim, then nothing until :data:`COLLAPSE_SECONDS` have passed, at which point one
    summary names the message and how many occurrences were suppressed. ``tick()`` closes windows
    for messages that have gone quiet (a flood that stops must still leave its count), and
    ``flush()`` accounts for everything still pending — the process's last honest word, wired to
    ``atexit``.

    The tracked table is capped at ``max_keys`` (least-recently-seen evicted, with its count
    flushed) because the other flood shape is a child printing a line that is *different* every
    time, and this process runs for weeks."""

    def __init__(self, clock=None, collapse_seconds=COLLAPSE_SECONDS, max_keys=MAX_KEYS):
        self._clock = clock if clock is not None else time.time
        self._window = collapse_seconds
        self._max_keys = max(1, max_keys)
        self._seen = OrderedDict()

    def tracked(self):
        return len(self._seen)

    def feed(self, line):
        now = self._clock()
        key = collapse_key(line)
        entry = self._seen.get(key)
        if entry is None:
            out = []
            while len(self._seen) >= self._max_keys:
                _, evicted = self._seen.popitem(last=False)
                out.extend(self._summary(evicted, now))
            self._seen[key] = _Repeat(now, line)
            out.append(line)
            return out
        self._seen.move_to_end(key)
        entry.suppressed += 1
        entry.sample = line
        if now - entry.window_at >= self._window:
            return self._summary(entry, now)
        return []

    def tick(self):
        """Close any window whose message has gone quiet long enough to be summarised."""
        now = self._clock()
        out = []
        for entry in list(self._seen.values()):
            if entry.suppressed and now - entry.window_at >= self._window:
                out.extend(self._summary(entry, now))
        return out

    def flush(self):
        """Summarise every message with an outstanding count, then forget the counts."""
        now = self._clock()
        out = []
        for entry in list(self._seen.values()):
            out.extend(self._summary(entry, now))
        return out

    def _summary(self, entry, now):
        if not entry.suppressed:
            return []
        span = max(0.0, now - entry.first_at)
        line = ("command-center: repeated %d× in %ds, suppressed — %s"
                % (entry.suppressed, int(round(span)), _clip(entry.sample)))
        entry.suppressed = 0
        entry.window_at = now
        entry.first_at = now
        return [line]


def _clip(line):
    line = _WS_RE.sub(" ", line).strip()
    return line if len(line) <= _MAX_ECHO else line[:_MAX_ECHO] + "…"


# =============================== Logbook — stamp, collapse, write ===============================

def stamp(ts=None):
    """``2026-09-09T09:45:01-0700`` — one line's wall-clock reading, local with its UTC offset."""
    return time.strftime(TIME_FORMAT, time.localtime(time.time() if ts is None else ts))


class Logbook:
    """The composition: a line in, a dated (and possibly collapsed) line out to ``sink``.

    Blank lines are dropped rather than stamped — ``command-center: stopped`` is written as
    ``"\\ncommand-center: stopped\\n"`` and a lone timestamp on its empty half would be grep noise.
    A line that already carries our stamp passes through unchanged, so the sink's own cap marker
    can re-enter the file after a truncation without being dated twice."""

    def __init__(self, sink, clock=None, collapse_seconds=COLLAPSE_SECONDS, max_keys=MAX_KEYS):
        self._sink = sink
        self._clock = clock if clock is not None else time.time
        self._collapser = Collapser(clock=self._clock, collapse_seconds=collapse_seconds,
                                    max_keys=max_keys)
        self._lock = threading.Lock()

    def tracked(self):
        return self._collapser.tracked()

    def feed(self, line):
        """Record one raw line (no trailing newline needed)."""
        line = line.rstrip("\r\n")
        if not line.strip():
            return
        with self._lock:
            self._emit(self._collapser.feed(line))

    def tick(self):
        """Periodic upkeep: close the collapse window of anything that has gone quiet."""
        with self._lock:
            self._emit(self._collapser.tick())

    def flush(self):
        """Final accounting — safe to call more than once (``atexit`` plus an explicit shutdown)."""
        with self._lock:
            self._emit(self._collapser.flush())

    def _emit(self, lines):
        for line in lines:
            if _STAMP_RE.match(line):
                self._sink.write(line + "\n")
            else:
                self._sink.write("%s %s\n" % (stamp(self._clock()), line))


# =============================== the fd takeover ===============================

class Handle:
    """What :func:`install` hands back: the live logbook, plus the way to undo the takeover.

    ``restore()`` is not a nicety. The pipe the pump reads is the process's only stdout/stderr, so
    if the pump ever stopped reading, every writer in the process would block on a full pipe and
    the dashboard would wedge with no log to say why. The pump therefore restores the original fds
    on its own way out, and the handle exposes the same escape hatch to a caller."""

    def __init__(self, logbook, sink, read_fd, sink_fd, saved_fds, target_fds):
        self.logbook = logbook
        self.sink = sink
        self.read_fd = read_fd
        self.sink_fd = sink_fd
        self.pump = None                 # set by install(), so shutdown can wait for the drain
        self._saved = saved_fds          # {target fd: dup of the original}
        self._targets = tuple(target_fds)
        self._restored = False
        self._lock = threading.Lock()

    def flush(self):
        self.logbook.flush()

    def restore(self):
        """Put the original fds back and stop routing through the pump. Idempotent.

        This is also the orderly way to *end* the takeover: fds 1 and 2 hold the only references to
        the pipe's write end, so replacing them closes it, and the pump reads EOF, drains what is
        left and flushes. That is why ``_shutdown`` calls this before its final flush rather than
        racing the pump with a sleep."""
        with self._lock:
            if self._restored:
                return
            self._restored = True
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        for fd in self._targets:
            saved = self._saved.get(fd)
            if saved is None:
                continue
            try:
                os.dup2(saved, fd)
            except OSError:
                pass


class _Pump(threading.Thread):
    """The log's single writer: reads the takeover pipe, splits it into lines, feeds the logbook.

    Line-oriented with a bounded partial buffer — a child that writes megabytes without a newline
    must not be held in memory, so the buffer is flushed at :data:`MAX_PARTIAL`. ``select`` with a
    timeout gives the logbook its periodic ``tick`` (the trailing count of a flood that stopped)
    without a second thread. Any unexpected fault ends in ``handle.restore()``: writers then go
    straight to the file, degraded but never blocked."""

    def __init__(self, read_fd, handle, tick_seconds=1.0):
        threading.Thread.__init__(self, name="logbook-pump", daemon=True)
        self._fd = read_fd
        self._handle = handle
        self._tick = tick_seconds
        self._buf = b""

    def run(self):
        clean = False
        try:
            clean = self._loop()
        except Exception:
            clean = False
        try:
            if clean:
                # EOF: every writer is gone, the process is on its way out. Putting the original
                # fds back costs nothing and covers the case where the pipe closed but the process
                # lives on — a writer must never be left pointing at a pipe nobody reads.
                self._handle.restore()
            else:
                self._bail_out()
        except Exception:
            pass
        try:
            self._handle.logbook.flush()
        except Exception:
            pass

    def _bail_out(self):
        """The one failure that could actually wedge the dashboard, answered.

        Every writer in this process writes into a 64 KiB pipe that only this thread drains. If the
        thread dies and simply stops reading, the next writer to fill that pipe blocks forever —
        with no log to say why. So a dying pump first puts the original fds back (new writes go
        straight to the file) and then drains whatever is already in the pipe, raw and unfiltered,
        to the same file: writers already blocked mid-``write`` are still holding the *old* file
        description, and only a drain can release them. Bounded, and it never tries to be clever."""
        self._handle.restore()
        try:
            os.set_blocking(self._fd, False)
        except OSError:
            return
        drained = 0
        while drained < 4 * 1024 * 1024:
            try:
                chunk = os.read(self._fd, 65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not chunk:
                break
            drained += len(chunk)
            try:
                os.write(self._handle.sink_fd, chunk)
            except OSError:
                break

    def _loop(self):
        """Read until EOF. Returns ``True`` for a clean end-of-pipe, ``False`` for a fault.

        The distinction is load-bearing, not cosmetic: a fault means fds 1 and 2 still point at a
        pipe this thread has stopped draining, and the next writer to fill it would block forever.
        Only the caller can tell those apart, so every fault path must come back as ``False``
        rather than a bare ``return`` (fresh-agent review, issue #481)."""
        book = self._handle.logbook
        while True:
            try:
                ready, _, _ = select.select([self._fd], [], [], self._tick)
            except (OSError, ValueError):
                return False
            if not ready:
                book.tick()
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except OSError as e:
                if e.errno in (errno.EINTR, errno.EAGAIN):
                    continue
                return False
            if not chunk:
                self._drain()
                return True     # every writer is gone: the process is on its way out
            self._buf += chunk
            self._consume()

    def _consume(self):
        book = self._handle.logbook
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            book.feed(line.decode("utf-8", "replace"))
        if len(self._buf) >= MAX_PARTIAL:
            book.feed(self._buf.decode("utf-8", "replace"))
            self._buf = b""

    def _drain(self):
        if self._buf:
            self._handle.logbook.feed(self._buf.decode("utf-8", "replace"))
            self._buf = b""


def install(target_fds=(1, 2), max_bytes=None, keep_bytes=None,
            collapse_seconds=COLLAPSE_SECONDS, max_keys=MAX_KEYS, clock=None,
            tick_seconds=1.0):
    """Take over this process's stdout+stderr so every line reaching them is dated and bounded.

    Called once, from ``bin/command-center``'s process entry point — owning a process's fds is a
    thing only the real process does, never a library and never a test. Everything downstream of
    the pipe (:class:`Logbook`, :class:`Collapser`, :class:`BoundedSink`) is plain, injectable and
    unit-tested; this function is only the plumbing that puts them in the path of *every* writer,
    the dashboard's own ``sys.stderr`` and its descendants' inherited fd 2 alike.

    Returns a :class:`Handle`. The original fds are dup'd and kept, so the takeover is reversible."""
    targets = tuple(target_fds)
    saved = {}
    for fd in targets:
        try:
            saved[fd] = os.dup(fd)
        except OSError:
            pass
    if not saved:
        return None
    # The sink writes to the launcher's real file — the LAST target's original fd (stderr under
    # both launchers; liftoff merges stdout into it, launchd points both at one path).
    sink_fd = saved[targets[-1]]
    sink = BoundedSink(sink_fd, max_bytes=max_bytes, keep_bytes=keep_bytes, clock=clock)
    book = Logbook(sink, clock=clock, collapse_seconds=collapse_seconds, max_keys=max_keys)
    read_fd, write_fd = os.pipe()
    handle = Handle(book, sink, read_fd, sink_fd, saved, targets)
    for fd in targets:
        try:
            os.dup2(write_fd, fd)
        except OSError:
            pass
    os.close(write_fd)          # fds 1/2 now hold the only references to the write end
    # stdout is block-buffered when it is not a terminal, and it is never a terminal here; make it
    # line-buffered so a line lands in the log when it is written, not when a 8 KiB block fills.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    pump = _Pump(read_fd, handle, tick_seconds=tick_seconds)
    handle.pump = pump
    pump.start()
    atexit.register(_shutdown, handle)
    _catch_termination(handle)
    return handle


def _catch_termination(handle):
    """Flush the log on SIGTERM/SIGHUP too, not only on a clean exit.

    ``atexit`` does not run for a default-disposition SIGTERM — and SIGTERM is exactly how this
    process is normally stopped: ``bin/liftoff``'s ``--restart-dashboard`` signals the pid the
    dashboard published for itself, and launchd stops a job the same way. Without this, a restart
    during a flood drops the current window's suppressed count on the floor and the log's last word
    about it is a lie by omission (fresh-agent review, issue #481).

    The handler flushes, then re-raises the signal with its default disposition, so the exit status
    the supervisor sees is unchanged. Signals can only be installed from the main thread; anywhere
    else this is simply a no-op."""
    installed = []

    def _on_signal(signum, frame):
        # Put BOTH signals back to their default disposition FIRST, before touching a single lock.
        # The flush below takes the logbook's and the sink's locks, and a signal handler runs on the
        # main thread on top of whatever it interrupted — including a previous run of this handler,
        # or the identical `atexit` shutdown. A second SIGTERM arriving mid-flush would then block
        # forever on a lock its own suspended caller holds: the process would never die, the port
        # would stay held, and `liftoff --restart-dashboard` would time out instead of restarting
        # (fresh-agent review, issue #481). Disarming first makes that impossible — an impatient
        # second signal simply kills us, which is exactly what it is asking for.
        for s in installed:
            try:
                signal.signal(s, signal.SIG_DFL)
            except Exception:
                pass
        try:
            _shutdown(handle)
        except Exception:
            pass
        try:
            os.kill(os.getpid(), signum)
        except Exception:                        # pragma: no cover — the kill above ends us
            os._exit(1)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) in (signal.SIG_DFL, None):
                signal.signal(sig, _on_signal)
                installed.append(sig)
        except (ValueError, OSError, RuntimeError):
            pass          # not the main thread, or the platform refuses — the log is not worth a crash


def _shutdown(handle):
    """At exit: end the takeover in order, so the last lines are not lost to a race.

    ``restore()`` flushes the streams and puts the original fds back, which closes the pipe's last
    write end; the pump then reads EOF, drains the remainder, and flushes. Joining it is what makes
    that deterministic instead of a sleep-and-hope — with a hard timeout, because a shutdown must
    never hang waiting on a log. The final flush is the belt to that brace."""
    try:
        handle.restore()
    except Exception:
        pass
    pump = handle.pump
    if pump is not None:
        try:
            pump.join(timeout=2.0)
        except RuntimeError:
            pass
    try:
        handle.logbook.flush()
    except Exception:
        pass
