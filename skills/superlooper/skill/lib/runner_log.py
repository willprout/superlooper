"""The runner's own voice reaching disk: its stderr, its last words, and a bound on its children.

Issue #480, from the 2026-09-09 evidence record (#477 items 2 and 1c). The eApp runner — pane home,
work laptop — exited at 09:43:31 and left NOTHING: no line in ``logs/runner.log``, no journal
record, a watchdog still reading "healthy" eleven seconds after the last heartbeat. Its stderr went
to the cmux pane and nowhere else, so whatever it said on the way out died with the window. The
same report's item 1c is the other half of the same wound: a per-tick subprocess child emitted
thousands of identical ``MallocStackLogging`` lines, which is how a real reason gets drowned even on
the days it IS written down.

Three properties, deliberately independent — each survives the other two failing:

* **The tee.** What the runner writes to stderr also lands in ``logs/runner.log``. This is a
  PANE-HOME gap only: the login-item home's launchd plist already points both StandardOutPath and
  StandardErrorPath at that exact file, so arming a second writer there would print every line
  twice. Rather than branch on the home (two copies of a rule somebody arms one of), the tee asks
  the only question that actually matters — *does this stream already land in that file?* — by
  comparing the stream's fstat with the log's. A runner started as ``superlooper run 2>>
  logs/runner.log`` is covered by the same answer, and so is any future home nobody has invented.

  The wrap is at the ``sys.stderr`` OBJECT, not at fd 2. That covers everything Python itself writes
  — tracebacks, every ``print(..., file=sys.stderr)`` — and deliberately does NOT dup2 the file
  descriptor: a pump thread and a swapped fd 2 are real behaviour changes in a process whose whole
  posture is fail-stopped, and this issue is observability only. What it therefore does not catch is
  C-level writes straight to fd 2 (a crashing extension, dyld's own chatter). Stated here rather
  than implied: the tee makes a Python-level death legible, not a segfault.

* **The exit record.** An uncaught exception or a terminating signal writes exactly ONE
  ``runner_exit`` journal act naming the reason, before the process dies. The journal is the durable
  half — it is timestamped, it is what the morning report and the dashboard read, and it survives a
  pane the tee cannot save. Exactly one, however many hooks fire: a SIGTERM sets the fail-stop flag,
  ``run()`` returns normally, and the interpreter then exits, so the same death reaches three hooks,
  and a log that says the runner exited three times is a log that lies.

* **The bound.** Everything the runner logs goes through ``bounded()`` at ``Runner._log`` — the one
  doorway into ``runner.log`` — so the guarantee is structural rather than a promise made at each
  spawn helper in turn. Identical runs fold to one line and a count, the whole is capped by lines
  and by characters keeping HEAD and TAIL (the head names what ran, the tail carries the error), and
  the one known-benign runtime chatter pattern is dropped outright.

Never raises, anywhere. Observability that can kill the loop is worse than no observability: every
public function here fails to a no-op or an empty string, and the exit record's write failure is
left RETRYABLE on purpose so the next hook gets a turn (the same discipline as the wedged-tick
ALERT, which is retried until it lands).
"""
import atexit
import os
import re
import signal
import sys
import traceback

import journal

# The journal act. One name for every way a runner can go, with `reason` telling them apart —
# "signal" (SIGTERM/SIGINT/^C), "exception" (an uncaught raise), "clean" (the interpreter simply
# ended). Reading back "did this runner ever say goodbye?" must not mean knowing three act names.
EXIT_ACT = "runner_exit"

# What of an exception survives into the record. `error` is the repr (which for a UnicodeDecodeError
# embeds the entire offending byte string — the incident that grew a live journal 47 MB -> 74 MB in
# forty minutes, so bounding is not decoration); `traceback` is the formatted stack. Both are CLIPPED
# head-and-tail rather than truncated: the head of a traceback names the raise site and the tail
# names the exception, and an enormous exception message would otherwise push one of them out.
EXIT_ERROR_MAX = 500
EXIT_TRACEBACK_MAX = 4000

# The per-write bound on anything reaching runner.log. Sized to be GENEROUS — a failed launch's
# stderr and a red recheck's test output are exactly what an operator needs at 3am, and a bound that
# eats them buys log hygiene with the thing the log is for. What it stops is the pathological case:
# no single write can put more than this on disk, and a child repeating itself puts one line there
# however many thousand times it says it.
CHILD_MAX_LINES = 200
CHILD_MAX_CHARS = 16000

# Known-benign runtime chatter, dropped outright rather than folded. ONE pattern, and it earns its
# place with 3,386,416 lines of evidence: macOS libmalloc prints this on a child's startup when the
# process it was launched from carries a malloc-logging environment, once per child, forever. It
# says nothing about the child, the loop, or the machine. Folding is not enough for it, because the
# flood is ACROSS invocations — one line per tick per child, which is still a flood at one line a
# tick. Keep this list short and provable: a pattern here is output nobody will ever read, and the
# cap above — not this list — is what actually bounds an unknown noisy child.
_CHATTER = (re.compile(r"MallocStackLogging"),)

# Process-global by design, exactly like `gh.set_repo` / `gh.set_telemetry`: this is a posture the
# ONE long-lived runner process adopts at its entrypoint, and a Runner built inside a unit test or a
# one-shot CLI command must not silently acquire it. None until `arm()`.
_STATE = None


def log_path(state_home):
    """The runner's own log inside a state home. One spelling, shared by the tee and the runner."""
    return os.path.join(os.fspath(state_home), "logs", "runner.log")


# ------------------------------- the bound on what reaches the log -------------------------------

def _clip(text, limit):
    """Clip to ``limit`` chars keeping HEAD and TAIL, with the drop declared in the middle. Never
    the head alone: for a traceback the head names the raise site and the tail names the exception,
    and for a script's output the head names what ran and the tail carries why it failed."""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return "%s\n...<+%d chars truncated>...\n%s" % (text[:head], len(text) - limit, text[-tail:])


def _is_chatter(line):
    return any(p.search(line) for p in _CHATTER)


def _fold(lines):
    """Collapse RUNS of identical lines into one line and a count. Consecutive only, on purpose:
    the interleaved case (A B A B ... a thousand times) is not a fold, it is a flood, and the line
    cap below is what answers it. A fold that reordered or deduplicated across the whole stream
    would change what the log says happened."""
    runs = []
    for line in lines:
        if runs and runs[-1][0] == line:
            runs[-1][1] += 1
        else:
            runs.append([line, 1])
    return [ln if n == 1 else "%s   [x%d identical lines]" % (ln, n) for ln, n in runs]


def _cap_lines(lines, max_lines):
    if len(lines) <= max_lines:
        return lines
    head = max_lines // 2
    tail = max_lines - head - 1
    dropped = len(lines) - head - tail
    return lines[:head] + ["...<%d more line(s) dropped>..." % dropped] + lines[-tail:]


def bounded(text, max_lines=CHILD_MAX_LINES, max_chars=CHILD_MAX_CHARS):
    """The bounded form of some text on its way into ``runner.log`` (issue #480).

    Chatter dropped, identical runs folded to one line and a count, then capped by lines and by
    characters. Returns "" for anything with nothing left to say — including output that was ONLY
    chatter, which is the whole point: a per-tick child whose entire contribution is one suppressed
    line must contribute NOTHING, or the flood simply becomes a flood of "1 line suppressed" notes.
    When real output survives beside dropped chatter the drop IS declared, so nothing vanishes from
    a log an operator is reading for a reason.

    Fail-open on wrong-typed input (never raise into a tick): a non-string reads as nothing to log.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    kept, dropped = [], 0
    for line in text.splitlines():
        if _is_chatter(line):
            dropped += 1
        else:
            kept.append(line)
    if not any(ln.strip() for ln in kept):
        return ""
    out = "\n".join(_cap_lines(_fold(kept), max_lines))
    if dropped:
        out += "\n[%d line(s) of macOS malloc stack-logging chatter suppressed]" % dropped
    return _clip(out, max_chars)


# ------------------------------- the tee -------------------------------

class Tee:
    """A write-through stderr: every write reaches the original stream AND the runner's log.

    The log handle is opened per write rather than held: appends are O_APPEND and interleave
    line-wise with the runner's own ``_log`` writes, no descriptor is held across a re-exec, and a
    log that is moved out from under us costs nothing. Stderr from the runner itself is a handful of
    lines a day; the cost of an open() per write is not a real cost here.

    Every write is guarded on BOTH sides. A stderr that has gone away (a closed pane) must not stop
    the log from getting the reason, and an unwritable log must not stop the pane.
    """

    def __init__(self, stream, path):
        self._stream = stream
        self._path = path

    def write(self, s):
        try:
            n = self._stream.write(s)
        except Exception:
            n = None
        append_log(self._path, s)
        # `print` and friends do not need the count, but a caller that does must not get None from a
        # stream that swallowed its own error.
        return n if isinstance(n, int) else len(s if isinstance(s, str) else "")

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self):
        # Delegated deliberately: a caller handing `stderr=sys.stderr` to a subprocess gets the REAL
        # descriptor, so the child's output goes where it always went. Such a child bypasses the tee
        # — the engine spawns with `capture_output=True` everywhere, so nothing in it does.
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _already_lands_in(stream, path):
    """True when writes to ``stream`` ALREADY end up in ``path`` — the login-item home, where
    launchd's StandardErrorPath is this very file, and any ``superlooper run 2>> …`` invocation.
    Judged by inode, not by the process home: one question, asked of the thing itself."""
    try:
        s = os.fstat(stream.fileno())
        f = os.stat(path)
    except Exception:
        return False
    return (s.st_dev, s.st_ino) == (f.st_dev, f.st_ino)


# ------------------------------- arming / the exit record -------------------------------

def append_log(path, text):
    """Append text to a log file, never raising. The one write doorway `runner.log` has — the tee
    uses it and so does ``Runner._log``, so there is one policy rather than two.

    ``errors="replace"`` is the load-bearing part, and it is not hypothetical. A login-item runner
    is started by launchd with a minimal environment: LANG unset means the preferred encoding is
    ASCII, and text mode then RAISES UnicodeEncodeError on the first em-dash — and the engine's own
    FATAL messages are full of them. Unguarded, that raise leaves the logger and lands in whatever
    tick called it. A mangled character in a log line is a cost; a logger that can kill a tick is a
    defect, and the logger is the last thing that should be able to.
    """
    try:
        with open(path, "a", errors="replace") as f:
            f.write(text)
    except Exception:
        pass


def _ensure_log(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            pass
    except Exception:
        pass


def arm(state_home, stream=None, append=None):
    """Arm the runner's exit visibility for THIS process. Idempotent; returns the armed state.

    Called once, at the live runner's entrypoint — never from a Runner constructor, a unit test's
    Runner, or a one-shot CLI verb, all of which would then wrap a stderr they do not own and
    register an atexit hook nobody asked for.

    ``stream`` and ``append`` are test seams (the stderr to tee, the journal writer). Production
    passes neither.
    """
    global _STATE
    if _STATE is not None:
        return _STATE
    home = os.fspath(state_home)
    path = log_path(home)
    _ensure_log(path)
    target = sys.stderr if stream is None else stream
    original = sys.stderr
    teed = not _already_lands_in(target, path)
    if teed:
        sys.stderr = Tee(target, path)
    _STATE = {"home": home, "log": path, "teed": teed, "wrote": False,
              "append": append or journal.append,
              "stderr": original, "excepthook": sys.excepthook}
    sys.excepthook = _excepthook
    atexit.register(record_clean)
    return _STATE


def disarm():
    """Undo ``arm``: restore stderr and the excepthook, drop the atexit hook. For tests, and for a
    caller that armed against the wrong home — never part of a runner's own life."""
    global _STATE
    st, _STATE = _STATE, None
    if st is None:
        return
    sys.stderr = st["stderr"]
    sys.excepthook = st["excepthook"]
    try:
        atexit.unregister(record_clean)
    except Exception:
        pass


def armed():
    """True when this process has armed its exit visibility."""
    return _STATE is not None


def _write(fields):
    """Write THE exit record, at most once per process. True only when it actually landed.

    A failed write leaves the door open on purpose: the next hook down (atexit, a second signal)
    tries again, exactly as the wedged-tick ALERT is retried until it lands. Losing the one record
    an exit gets to a transient disk error is the failure this whole module exists to end.
    """
    st = _STATE
    if st is None or st["wrote"]:
        return False
    rec = {"act": EXIT_ACT, "pid": os.getpid()}
    rec.update(fields)
    try:
        st["append"](st["home"], rec)
    except Exception:
        return False
    st["wrote"] = True
    return True


def _signal_name(signum):
    try:
        return signal.Signals(signum).name
    except (ValueError, TypeError):
        return str(signum)


def record_signal(signum):
    """Record a terminating signal as this runner's exit reason. Called from the runner's own
    SIGTERM/SIGINT handler, which still does exactly what it did before — set the fail-stop flag."""
    return _write({"reason": "signal", "signal": _signal_name(signum)})


def record_exception(exc_type, exc, tb):
    """Record an uncaught exception as this runner's exit reason."""
    if exc_type is KeyboardInterrupt or isinstance(exc, KeyboardInterrupt):
        # ^C is a signal wearing an exception's clothes. Recording it as a crash would put a
        # phantom fault in the journal every time an operator stops a runner by hand.
        return record_signal(signal.SIGINT)
    try:
        err = repr(exc) if exc is not None else str(exc_type)
    except Exception:
        err = "<unrepresentable %s>" % getattr(exc_type, "__name__", "exception")
    try:
        stack = "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:
        stack = ""
    return _write({"reason": "exception", "error": _clip(err, EXIT_ERROR_MAX),
                   "traceback": _clip(stack, EXIT_TRACEBACK_MAX)})


def record_clean():
    """Record an exit nothing else claimed — the interpreter simply ended. Registered with atexit,
    so a runner that returns normally still says so; suppressed when a signal or an exception has
    already written the record for this death."""
    return _write({"reason": "clean"})


def _excepthook(exc_type, exc, tb):
    """Journal the reason, then let the ORIGINAL hook print the traceback exactly as it always did —
    which, with the tee armed, is how the traceback reaches both the pane and the log."""
    try:
        record_exception(exc_type, exc, tb)
    except Exception:
        pass
    st = _STATE
    prev = st["excepthook"] if st else sys.__excepthook__
    try:
        prev(exc_type, exc, tb)
    except Exception:
        sys.__excepthook__(exc_type, exc, tb)
