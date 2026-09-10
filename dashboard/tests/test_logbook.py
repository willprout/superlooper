"""Issue #481 — the command center's log stays readable and BOUNDED.

On 2026-09-09 ``command-center.log`` reached **331 MB / 3,386,416 lines** (evidence record #477
item 1). 3,382,6xx of those lines were one line repeating —
``Python(<pid>) MallocStackLogging: can't turn off malloc stack logging because it was not
enabled.`` — printed by the dashboard's per-tick short-lived children at ~1.6/s, and the file had
**no timestamps at all**, so the flood could not even be dated. The ~3,700 lines that mattered
(282 ``BrokenPipeError`` tracebacks from the response write path, 247 ``RUNNER DOWN push``, 5
``port 8611 is already in use``) were buried in it.

Two structural facts shape what is tested here:

* **``capture_output=True`` is not a defence.** Every ``subprocess.run`` in ``lib/`` already
  captures its child's stderr, and the flood still landed in the dashboard's own log — because a
  line written to the inherited fd 2 *before* the child's own ``dup2`` (or by anything else that
  still holds it) never passes through the capture pipe at all. The only place in this process that
  can see such a line is **fd 2 itself**. So the dashboard OWNS its stdout+stderr: a pipe it reads,
  timestamps, collapses, and writes to the launcher's file. That is the "capture and bound" the
  issue asks for, and it is source-agnostic by construction (#477's own root-cause question — why
  ``MallocStackLogging`` leaked from a cmux-inherited environment — is explicitly out of scope).
* **The launcher owns the file, not us.** ``bin/liftoff`` opens ``<state-home>/command-center.log``
  append-only and hands it over as fd 1/2; the launchd job does the same with
  ``~/Library/Logs/command-center.log``. We never learn that path, and the fd is write-only — so
  the cap cannot rename or re-read the file. It truncates it in place (exactly what the operator
  did by hand on 2026-09-09, ``: > …``, fd kept valid) and rewrites a retained tail the pump kept
  in memory. The bound is therefore hard and knowable: ``MAX_BYTES``.

The four halves of the definition of done, in order: timestamps, the cap, the collapsed flood,
and the quiet client disconnect (that last one lives in ``tests/test_server_disconnect.py``,
beside the write path it fixes).
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import logbook

_ROOT = Path(__file__).resolve().parent.parent

# The real flood line from #477, with the pid that varied line to line.
_FLOOD = ("Python(%d) MallocStackLogging: can't turn off malloc stack logging because it was "
          "not enabled.")

_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}"


class _Sink:
    """A recording stand-in for the bounded file sink: collects whole written chunks."""

    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def lines(self):
        return "".join(self.chunks).splitlines()


class _Clock:
    """A hand-cranked clock — cadence is asserted, never slept for."""

    def __init__(self, t=1757400000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs
        return self.t


def _book(sink=None, clock=None, **kw):
    sink = _Sink() if sink is None else sink
    clock = _Clock() if clock is None else clock
    return logbook.Logbook(sink, clock=clock, **kw), sink, clock


# =============================== (a) every line carries a timestamp ===============================

def test_every_line_the_logbook_writes_carries_a_timestamp():
    # The headline failure of 2026-09-09: 331 MB of log that could not be DATED. Every line out of
    # the logbook must carry its own wall-clock reading, flood lines included.
    book, sink, _ = _book()
    book.feed("command-center: serving http://127.0.0.1:8611 — Ctrl-C to stop")
    assert len(sink.lines()) == 1
    assert re.match(_TS + r" command-center: serving ", sink.lines()[0]), sink.lines()[0]


def test_the_timestamp_is_the_lines_own_clock_reading_in_local_time():
    # Local time with a UTC offset, matching the house style (lib/digest, lib/replay read
    # time.localtime): the operator correlates this log against his own clock and against the
    # runner's panes, and an offset-stamped local time is both readable and unambiguous.
    clock = _Clock()
    book, sink, _ = _book(clock=clock)
    book.feed("first")
    clock.advance(3600)
    book.feed("second")
    want_first = time.strftime(logbook.TIME_FORMAT, time.localtime(clock.t - 3600))
    want_second = time.strftime(logbook.TIME_FORMAT, time.localtime(clock.t))
    assert sink.lines()[0] == "%s first" % want_first
    assert sink.lines()[1] == "%s second" % want_second
    assert want_first != want_second, "the two stamps must actually differ"


def test_a_blank_line_is_dropped_rather_than_stamped_on_its_own():
    # `command-center: stopped` is written as "\ncommand-center: stopped\n" (a Ctrl-C cosmetic).
    # A lone timestamp on the empty half would be noise in a log built for grepping.
    book, sink, _ = _book()
    book.feed("")
    book.feed("   ")
    assert sink.lines() == []


def test_an_already_stamped_line_is_not_stamped_twice():
    # Idempotence matters because the sink's own cap marker re-enters the file after a truncation:
    # a line that already begins with our stamp must pass through unchanged.
    book, sink, clock = _book()
    stamped = "%s command-center: log capped" % time.strftime(logbook.TIME_FORMAT,
                                                              time.localtime(clock.t))
    book.feed(stamped)
    assert sink.lines() == [stamped]


# =============================== (c) the flood collapses to a count ===============================

def test_the_fingerprint_ignores_the_varying_pid():
    # This is what makes #477's shape collapsible at all: the flood line differed only in its pid,
    # so an exact-string dedup would have collapsed NOTHING. Numbers and hex addresses are the
    # per-occurrence noise; the sentence is the identity.
    assert logbook.fingerprint(_FLOOD % 59258) == logbook.fingerprint(_FLOOD % 82016)
    assert logbook.fingerprint("worker 0x7ff8 died") == logbook.fingerprint("worker 0x1a2b died")
    # ...and genuinely different sentences stay different.
    assert logbook.fingerprint("RUNNER DOWN push [a/b]") != logbook.fingerprint(
        "RUNNER DOWN push [c/d]")


def test_the_flood_shape_from_477_collapses_to_a_bounded_counted_record():
    # The definition of done, stated exactly: 3.4M repeats of one line must not become 3.4M log
    # lines. Feed the real shape (varying pid) at its measured rate and demand a bounded record
    # that still says HOW MANY were suppressed.
    clock = _Clock()
    book, sink, _ = _book(clock=clock, collapse_seconds=60.0)
    for i in range(10000):
        book.feed(_FLOOD % (59000 + i))
        clock.advance(0.625)          # ~1.6 lines/s, the rate measured on 2026-09-09
    book.flush()                      # what atexit does — the final window gets its count too
    out = sink.lines()
    # 10,000 lines over ~104 minutes: one verbatim first sighting plus one summary per collapse
    # window. Nothing near 10,000.
    assert len(out) <= 120, "%d lines is still a flood" % len(out)
    assert len(out) >= 2, "the flood must still be recorded, not silently dropped"
    summaries = [l for l in out[1:] if "suppressed" in l]
    assert summaries, out[:5]
    counted = sum(int(m.group(1)) for l in summaries
                  for m in [re.search(r"repeated (\d+)", l)] if m)
    assert counted == 9999, "every suppressed occurrence must be accounted for, got %d" % counted


def test_the_first_sighting_of_a_repeating_line_is_kept_verbatim():
    # A collapsed flood must still be diagnosable: the first occurrence goes in whole, so the
    # operator can read the actual message that flooded.
    book, sink, _ = _book()
    for i in range(50):
        book.feed(_FLOOD % (59000 + i))
    assert sink.lines()[0].endswith(_FLOOD % 59000)


def test_the_summary_names_the_line_it_is_counting():
    # A bare "repeated 400×" would be useless in a log with several noisy children.
    clock = _Clock()
    book, sink, _ = _book(clock=clock, collapse_seconds=10.0)
    for _ in range(400):
        book.feed(_FLOOD % 1)
        clock.advance(0.1)
    summary = [l for l in sink.lines() if "suppressed" in l][0]
    assert "MallocStackLogging" in summary


def test_distinct_lines_are_never_collapsed_into_one_another():
    # The 247 RUNNER DOWN pushes were REAL signal. Two repos going down must read as two events.
    book, sink, _ = _book()
    book.feed("command-center: RUNNER DOWN push [will-titan/agent-360-eapp] — sent")
    book.feed("command-center: RUNNER DOWN push [titancasket/titan-apps-partner] — sent")
    assert len(sink.lines()) == 2


def test_a_flood_that_stops_still_gets_its_trailing_count_on_tick():
    # A child that goes quiet must not leave its tail of suppressed lines uncounted — otherwise the
    # log ends mid-flood with no record of how big it got.
    clock = _Clock()
    book, sink, _ = _book(clock=clock, collapse_seconds=60.0)
    for _ in range(500):
        book.feed(_FLOOD % 1)
    assert len(sink.lines()) == 1, "still inside the first window — nothing to summarise yet"
    clock.advance(61)
    book.tick()
    assert "repeated 499" in sink.lines()[-1], sink.lines()[-1]


def test_flush_accounts_for_everything_still_pending():
    # atexit's job: a process that dies mid-flood still leaves an honest count behind.
    book, sink, _ = _book()
    for _ in range(30):
        book.feed(_FLOOD % 1)
    book.flush()
    assert "repeated 29" in sink.lines()[-1]
    book.flush()
    assert len([l for l in sink.lines() if "suppressed" in l]) == 1, "flush must not double-count"


def test_the_collapse_table_is_bounded_so_a_varied_flood_cannot_eat_memory():
    # The other flood shape: a child printing a line that is different EVERY time. The dedup table
    # must not grow with it (this process runs for weeks).
    book, sink, _ = _book(max_keys=64)
    for i in range(5000):
        book.feed("unique message number %s" % ("x" * (i % 500) + str(i)))
    assert book.tracked() <= 64


# =============================== (b) the log is capped ===============================

def _sink_over(path, **kw):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    return logbook.BoundedSink(fd, **kw), fd


def test_the_cap_is_documented_as_a_byte_bound():
    # "A documented bound" — the module states it as a number the README can quote.
    assert isinstance(logbook.MAX_BYTES, int) and logbook.MAX_BYTES > 0
    assert logbook.KEEP_BYTES < logbook.MAX_BYTES


def test_writing_far_past_the_bound_never_grows_the_file_past_it(tmp_path):
    # The test the definition of done asks for: drive PAST the bound. 4 MB of traffic through a
    # 256 KB cap, with the size checked after every write, so no intermediate moment escapes it.
    log = tmp_path / "command-center.log"
    sink, fd = _sink_over(log, max_bytes=256 * 1024, keep_bytes=16 * 1024)
    try:
        for i in range(20000):
            sink.write("2026-09-09T09:45:01-0700 flood line %d %s\n" % (i, "y" * 150))
            assert log.stat().st_size <= 256 * 1024, "cap breached at write %d" % i
    finally:
        os.close(fd)
    assert log.stat().st_size > 0, "capping must not leave an empty log"


def test_the_cap_leaves_a_marker_saying_what_it_dropped(tmp_path):
    # A log that silently loses its own history is a log that lies. The truncation must be visible
    # IN the file, with the byte count it dropped and the cap it enforced.
    log = tmp_path / "command-center.log"
    sink, fd = _sink_over(log, max_bytes=64 * 1024, keep_bytes=4 * 1024)
    try:
        for i in range(2000):
            sink.write("line %d %s\n" % (i, "z" * 100))
    finally:
        os.close(fd)
    text = log.read_text()
    marker = [l for l in text.splitlines() if "capped" in l]
    assert marker, text[:400]
    assert re.match(_TS + r" command-center: log capped", marker[-1]), marker[-1]
    assert "65536" in marker[-1], "the marker must name the cap it enforced: %s" % marker[-1]


def test_the_most_recent_lines_survive_the_cap(tmp_path):
    # Capping is only useful if what survives is the RECENT past — the lines you came to the log to
    # read. The pump keeps a bounded tail in memory and rewrites it after the truncation.
    log = tmp_path / "command-center.log"
    sink, fd = _sink_over(log, max_bytes=32 * 1024, keep_bytes=4 * 1024)
    try:
        for i in range(1000):
            sink.write("line %d %s\n" % (i, "w" * 80))
        sink.write("command-center: RUNNER DOWN push [will-titan/agent-360-eapp] — sent\n")
    finally:
        os.close(fd)
    text = log.read_text()
    assert "RUNNER DOWN push" in text, "the newest line must always be in the file"
    assert "line 999" in text
    assert "line 0 " not in text, "the oldest lines are the ones dropped"


def test_a_log_already_past_the_cap_is_compacted_at_the_first_write(tmp_path):
    # The 331 MB file was already on disk when the dashboard restarted. A cap that only counts its
    # OWN writes would have left it there forever, growing. The sink adopts the file's real size.
    log = tmp_path / "command-center.log"
    log.write_text("ancient history\n" * 60000)          # ~ 960 KB, already over the cap below
    assert log.stat().st_size > 128 * 1024
    sink, fd = _sink_over(log, max_bytes=128 * 1024, keep_bytes=8 * 1024)
    try:
        sink.write("2026-09-09T09:45:01-0700 command-center: serving\n")
    finally:
        os.close(fd)
    assert log.stat().st_size <= 128 * 1024
    assert "command-center: serving" in log.read_text()


def test_a_sink_over_a_pipe_is_never_truncated_and_never_raises(tmp_path):
    # Foreground `bin/command-center` has a terminal on fd 2, and a pipe is what a test harness
    # hands it. Neither can be ftruncate()d — the sink must degrade to "just write", not blow up
    # the pump thread that is the whole log's single writer.
    r, w = os.pipe()
    try:
        sink = logbook.BoundedSink(w, max_bytes=1024, keep_bytes=256)
        for i in range(20):
            sink.write("x" * 40 + "\n")
        assert os.read(r, 4096), "the lines still went out"
    finally:
        os.close(r)
        os.close(w)


# ====================== (c) end to end: a real noisy child, a real fd takeover ======================

_DRIVER = r'''
import os, subprocess, sys
sys.path.insert(0, %(lib)r)
import logbook
logbook.install(max_bytes=%(cap)d, keep_bytes=8192, collapse_seconds=%(win)s)
sys.stderr.write("command-center: serving http://127.0.0.1:8611 — Ctrl-C to stop\n")
# A noisy child of exactly #477's shape: it inherits fd 2 (no capture_output can see this) and
# prints one line, over and over, with a pid that changes every time.
noisy = [sys.executable, "-c",
         "import sys\n"
         "for i in range(%(n)d):\n"
         "    sys.stderr.write('Python(%%d) MallocStackLogging: can\\'t turn off malloc stack "
         "logging because it was not enabled.\\n' %% (59000 + i))\n"]
subprocess.run(noisy)
sys.stderr.write("command-center: RUNNER DOWN push [will-titan/agent-360-eapp] — sent\n")
'''


def _drive(tmp_path, n, cap=200 * 1024, win="0.05"):
    """Run the real thing in a real process: ``logbook.install()`` over a real inherited log file,
    a real noisy grandchild on the inherited fd 2, then exit so atexit flushes. Returns the log."""
    log = tmp_path / "command-center.log"
    src = _DRIVER % {"lib": str(_ROOT / "lib"), "cap": cap, "n": n, "win": win}
    with open(str(log), "a") as fh:
        subprocess.run([sys.executable, "-c", src], stdout=fh, stderr=subprocess.STDOUT,
                       timeout=120, check=True)
    return log


def test_a_noisy_child_on_the_inherited_stderr_is_captured_bounded_and_counted(tmp_path):
    # The whole mechanism, end to end, in the shape that actually happened: 20,000 flood lines from
    # a child that inherits fd 2. They must reach the log CAPTURED (they are the dashboard's noise
    # to own), COUNTED, and BOUNDED — and the real lines around them must survive.
    log = _drive(tmp_path, 20000)
    lines = log.read_text().splitlines()
    assert len(lines) < 500, "%d lines — the flood shape recurred" % len(lines)
    assert any("MallocStackLogging" in l for l in lines), "the noise must be recorded, not dropped"
    assert any("suppressed" in l for l in lines), lines[:10]
    counted = sum(int(m.group(1)) for l in lines
                  for m in [re.search(r"repeated (\d+)", l)] if m)
    assert counted >= 19000, "the count must be honest, got %d" % counted
    assert any("RUNNER DOWN push" in l for l in lines), "real signal must survive the flood"


def test_every_line_in_a_real_run_is_timestamped_whoever_wrote_it(tmp_path):
    # Timestamps are not a courtesy the dashboard's own writes get: the pump stamps the CHILD's
    # lines too, which is the only reason the 2026-09-09 flood would have been datable.
    log = _drive(tmp_path, 200)
    lines = [l for l in log.read_text().splitlines() if l]
    assert lines
    for line in lines:
        assert re.match(_TS + r" ", line), "unstamped line in the log: %r" % line
    assert any("MallocStackLogging" in l for l in lines)


def test_a_real_run_over_a_log_already_at_the_cap_stays_at_the_cap(tmp_path):
    # Restarting the dashboard onto 2026-09-09's own 331 MB file: the cap must bite immediately and
    # the file must come out under the bound, with the fresh boot line in it.
    log = tmp_path / "command-center.log"
    log.write_text("ancient flood\n" * 40000)            # ~ 560 KB
    src = _DRIVER % {"lib": str(_ROOT / "lib"), "cap": 64 * 1024, "n": 5000, "win": "0.05"}
    with open(str(log), "a") as fh:
        subprocess.run([sys.executable, "-c", src], stdout=fh, stderr=subprocess.STDOUT,
                       timeout=120, check=True)
    assert log.stat().st_size <= 64 * 1024, log.stat().st_size
    text = log.read_text()
    assert "command-center: RUNNER DOWN push" in text, "the newest real line must survive"
    assert "log capped" in text


def test_install_is_reversible_so_it_can_never_wedge_the_process(tmp_path):
    # The pipe the pump reads is the process's ONLY stderr: if the pump ever stopped, every writer
    # would block on a full pipe and the dashboard would wedge. `restore()` is the escape hatch the
    # pump itself uses on the way out, and it must put the original fds back.
    log = tmp_path / "out"
    src = ("import os, sys\n"
           "sys.path.insert(0, %r)\n"
           "import logbook\n"
           "h = logbook.install(max_bytes=65536)\n"
           "sys.stderr.write('through the pump\\n')\n"
           "h.restore()\n"
           "os.write(2, b'straight to the file\\n')\n" % str(_ROOT / "lib"))
    with open(str(log), "a") as fh:
        subprocess.run([sys.executable, "-c", src], stdout=fh, stderr=subprocess.STDOUT,
                       timeout=60, check=True)
    text = log.read_text()
    assert re.search(_TS + r" through the pump", text), text
    assert "straight to the file" in text, text


def test_a_pump_that_dies_never_wedges_its_writers(tmp_path):
    # The one failure mode that could take the dashboard down instead of just its log: every writer
    # in the process writes into a 64 KiB pipe that only the pump drains, so a dead pump means the
    # next writer to fill it blocks forever — with no log to say why. Kill the pump deliberately,
    # then push ~1 MB through fd 2 (16× the pipe buffer) and demand the process still finishes and
    # its output still lands.
    log = tmp_path / "command-center.log"
    src = ("import os, sys\n"
           "sys.path.insert(0, %r)\n"
           "import logbook\n"
           "h = logbook.install(max_bytes=8 * 1024 * 1024)\n"
           "def boom(line):\n"
           "    raise RuntimeError('pump fault')\n"
           "h.logbook.feed = boom\n"
           "sys.stderr.write('this kills the pump\\n')\n"
           "for i in range(5000):\n"
           "    os.write(2, b'x' * 200 + b'\\n')\n"
           "os.write(2, b'SURVIVED\\n')\n" % str(_ROOT / "lib"))
    with open(str(log), "a") as fh:
        subprocess.run([sys.executable, "-c", src], stdout=fh, stderr=subprocess.STDOUT,
                       timeout=60, check=True)      # a wedge shows up here as a TimeoutExpired
    assert "SURVIVED" in log.read_text(), "the writers were released but their output was lost"


# =============================== fresh-agent review fixes (issue #481) ===============================

def test_two_of_our_own_lines_differing_only_by_a_digit_stay_two_events():
    # Fresh-agent review: fingerprint() scrubs every digit, so `[org/app-1]` and `[org/app-2]` —
    # two DIFFERENT repos going down — shared one collapse key and the second was suppressed. The
    # 247 RUNNER DOWN pushes in the 2026-09-09 log are real signal; merging two repos' down-pushes
    # into one count would be a truth bug in the name of tidiness.
    book, sink, _ = _book()
    book.feed("command-center: RUNNER DOWN push [org/app-1] — sent")
    book.feed("command-center: RUNNER DOWN push [org/app-2] — sent")
    out = sink.lines()
    assert len(out) == 2, out
    assert "app-1" in out[0] and "app-2" in out[1]


def test_our_own_line_repeating_verbatim_still_collapses():
    # The other half of that rule: our lines DO repeat verbatim (282 identical disconnect lines),
    # and those must still collapse — the exact-string key is a fence, not an exemption.
    clock = _Clock()
    book, sink, _ = _book(clock=clock, collapse_seconds=10.0)
    for _ in range(300):
        book.feed("command-center: client disconnected during GET /api/snapshot")
        clock.advance(0.1)
    book.flush()
    assert len(sink.lines()) <= 6, sink.lines()
    assert any("repeated" in l for l in sink.lines())


def test_a_foreign_flood_still_collapses_across_its_varying_number():
    # And a descendant's output — the thing we do not control — keeps the scrubbing that makes
    # #477's varying-pid flood collapsible at all.
    assert logbook.collapse_key(_FLOOD % 1) == logbook.collapse_key(_FLOOD % 99999)
    assert logbook.collapse_key("command-center: a [x-1]") != logbook.collapse_key(
        "command-center: a [x-2]")


def test_one_oversized_line_cannot_push_the_rewritten_file_past_the_cap(tmp_path):
    # Fresh-agent review: the retained tail always keeps at least one chunk, even one bigger than
    # keep_bytes, and the post-truncation rewrite did not re-check the bound. With a small
    # configured cap and a child writing a very long unbroken line, the "capped" file came back
    # OVER the cap — breaking the one promise the README makes about this file.
    log = tmp_path / "command-center.log"
    sink, fd = _sink_over(log, max_bytes=32 * 1024, keep_bytes=1024)
    try:
        for _ in range(6):
            sink.write("2026-09-09T09:45:01-0700 " + "q" * (48 * 1024) + "\n")
            assert log.stat().st_size <= 32 * 1024, "cap breached: %d" % log.stat().st_size
    finally:
        os.close(fd)
    assert "log capped" in log.read_text()


def test_a_sigterm_still_leaves_the_suppressed_count_in_the_log(tmp_path):
    # Fresh-agent review: atexit does NOT run for a default-disposition SIGTERM, and SIGTERM is how
    # this process is normally stopped (`liftoff --restart-dashboard` signals the pid the dashboard
    # published for itself; launchd stops a job the same way). A restart mid-flood was therefore
    # dropping the current window's count silently.
    log = tmp_path / "command-center.log"
    src = ("import os, signal, sys, time\n"
           "sys.path.insert(0, %r)\n"
           "import logbook\n"
           "logbook.install(max_bytes=8 * 1024 * 1024, collapse_seconds=600)\n"
           "for i in range(300):\n"
           "    sys.stderr.write('Noise(%%d) from a child\\n' %% i)\n"
           "sys.stderr.flush()\n"
           "time.sleep(0.5)\n"
           "os.kill(os.getpid(), signal.SIGTERM)\n"
           "time.sleep(5)\n" % str(_ROOT / "lib"))
    with open(str(log), "a") as fh:
        proc = subprocess.run([sys.executable, "-c", src], stdout=fh,
                              stderr=subprocess.STDOUT, timeout=60)
    # The exit status the supervisor sees is still a plain SIGTERM death, not a swallowed signal.
    assert proc.returncode == -signal.SIGTERM, proc.returncode
    text = log.read_text()
    assert "repeated 299" in text, "the count was lost to the SIGTERM:\n%s" % text
