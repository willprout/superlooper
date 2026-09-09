"""lib/runner_log.py — the runner's own voice reaching disk (issue #480).

The incident these pin: on 2026-09-09 the eApp runner (pane home) exited at 09:43:31 and left
NOTHING behind — no line in `logs/runner.log`, no journal record, a watchdog still reading
"healthy". Its stderr went only to the cmux pane, so the reason died with the window. The same
report's item 1c: a per-tick subprocess child printed thousands of identical `MallocStackLogging`
lines, which is how a real reason gets drowned even when it IS written down.

Three properties, tested here as three separate things:

  * **the tee** — what the runner writes to stderr also lands in `logs/runner.log`, in BOTH process
    homes. The login-item home already had this (launchd points StandardErrorPath at that exact
    file), so the tee must recognise a stderr that ALREADY lands there and not write a second copy;
  * **the exit record** — an uncaught exception or a terminating signal writes one `runner_exit`
    journal act NAMING the reason before the process dies;
  * **the bound** — child output reaching the log is folded, capped, and stripped of known runtime
    noise, so no single child can flood it.
"""
import io
import json
import os
import signal
import sys

import pytest

import journal
import runner_log


NOISE = ("Python(3938) MallocStackLogging: can't turn off malloc stack logging because it "
         "was not enabled.")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "logs").mkdir(parents=True)
    return h


@pytest.fixture(autouse=True)
def _always_disarm():
    """Arming is process-global (like gh.set_repo/set_telemetry). Nothing may leak a wrapped
    sys.stderr or an installed excepthook into the next test."""
    yield
    runner_log.disarm()


def _log_text(home):
    p = home / "logs" / "runner.log"
    return p.read_text() if p.exists() else ""


def _exits(home):
    return [r for r in journal.read(str(home)) if r.get("act") == runner_log.EXIT_ACT]


# --------------------------- the tee: stderr reaches the log ---------------------------

def test_stderr_reaches_the_runner_log_in_the_pane_home(home):
    # The pane home: stderr is the cmux tab, which is not the log file. Both must get the line —
    # the operator watching the tab loses nothing, and the reason survives the tab's death.
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    print("the reason this runner is about to die", file=sys.stderr)
    sys.stderr.flush()
    assert "the reason this runner is about to die" in pane.getvalue()
    assert "the reason this runner is about to die" in _log_text(home)


def test_stderr_already_pointed_at_the_log_is_not_teed_twice(home):
    # The login-item home: launchd's StandardErrorPath IS logs/runner.log, so fd 2 already lands
    # there. A tee that cannot see that writes every line twice — the same reason, told twice,
    # which is how a log stops being trustworthy.
    path = home / "logs" / "runner.log"
    with open(path, "a") as fd2:
        armed = runner_log.arm(str(home), stream=fd2)
        assert armed["teed"] is False
        assert not isinstance(sys.stderr, runner_log.Tee)   # nothing was wrapped
        fd2.write("launchd already carries this\n")
        fd2.flush()
    assert _log_text(home).count("launchd already carries this") == 1


def test_the_tee_never_raises_when_the_log_cannot_be_written(home, monkeypatch):
    # Observability must never be a new way for the runner to die. An unwritable log costs the
    # log line and nothing else — the pane still gets it.
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)

    def _refuse(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("builtins.open", _refuse)
    sys.stderr.write("still reaches the pane\n")
    assert "still reaches the pane" in pane.getvalue()


def test_the_tee_reports_a_write_count_so_print_still_works(home):
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    assert sys.stderr.write("abc") == 3


def test_arming_twice_does_not_stack_two_tees(home):
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    first = sys.stderr
    runner_log.arm(str(home), stream=pane)
    assert sys.stderr is first
    sys.stderr.write("once\n")
    assert _log_text(home).count("once") == 1


def test_disarm_restores_the_original_stderr(home):
    pane = io.StringIO()
    before = sys.stderr
    runner_log.arm(str(home), stream=pane)
    assert sys.stderr is not before
    runner_log.disarm()
    assert sys.stderr is before


# --------------------------- the exit record ---------------------------

def test_an_uncaught_exception_writes_an_exit_act_naming_it(home):
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    try:
        raise ZeroDivisionError("the rock the loop tripped on")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())
    rec = _exits(home)
    assert len(rec) == 1
    assert rec[0]["reason"] == "exception"
    assert "ZeroDivisionError" in rec[0]["error"]
    assert "the rock the loop tripped on" in rec[0]["error"]
    assert rec[0]["pid"] == os.getpid()


def test_an_uncaught_exception_still_prints_its_traceback(home):
    # The journal record is a summary; the operator's traceback must not be swallowed to get it.
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    try:
        raise ZeroDivisionError("boom")
    except ZeroDivisionError:
        sys.excepthook(*sys.exc_info())
    assert "ZeroDivisionError" in pane.getvalue()
    assert "Traceback" in pane.getvalue()
    assert "ZeroDivisionError" in _log_text(home)     # ...and it survives the pane


def test_the_exit_act_carries_a_bounded_traceback(home):
    runner_log.arm(str(home), stream=io.StringIO())
    try:
        raise ValueError("x" * 50_000)
    except ValueError:
        sys.excepthook(*sys.exc_info())
    rec = _exits(home)[0]
    assert "ValueError" in rec["traceback"]
    assert len(rec["traceback"]) <= runner_log.EXIT_TRACEBACK_MAX + 200
    assert len(rec["error"]) <= runner_log.EXIT_ERROR_MAX + 200


def test_sigterm_writes_the_exit_act_naming_the_signal(home):
    runner_log.arm(str(home), stream=io.StringIO())
    assert runner_log.record_signal(signal.SIGTERM) is True
    rec = _exits(home)
    assert len(rec) == 1
    assert rec[0]["reason"] == "signal" and rec[0]["signal"] == "SIGTERM"


def test_sigint_names_its_own_signal(home):
    runner_log.arm(str(home), stream=io.StringIO())
    runner_log.record_signal(signal.SIGINT)
    assert _exits(home)[0]["signal"] == "SIGINT"


def test_an_unknown_signal_number_is_still_recorded(home):
    runner_log.arm(str(home), stream=io.StringIO())
    runner_log.record_signal(9999)
    assert _exits(home)[0]["signal"] == "9999"


def test_a_keyboard_interrupt_is_recorded_as_its_signal_not_as_a_crash(home):
    runner_log.arm(str(home), stream=io.StringIO())
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    rec = _exits(home)[0]
    assert rec["reason"] == "signal" and rec["signal"] == "SIGINT"


def test_a_clean_exit_still_leaves_a_reason(home):
    runner_log.arm(str(home), stream=io.StringIO())
    assert runner_log.record_clean() is True
    assert _exits(home)[0]["reason"] == "clean"


def test_only_one_exit_act_is_written_however_many_hooks_fire(home):
    # SIGTERM sets the fail-stop flag, run() returns normally, the interpreter exits: three hooks,
    # ONE death. A log that says the runner exited three times is a log that lies.
    runner_log.arm(str(home), stream=io.StringIO())
    runner_log.record_signal(signal.SIGTERM)
    runner_log.record_signal(signal.SIGTERM)
    runner_log.record_clean()
    assert len(_exits(home)) == 1
    assert _exits(home)[0]["reason"] == "signal"


def test_a_failed_journal_write_leaves_the_door_open_for_the_next_hook(home):
    # Same discipline as the wedged-tick ALERT: a transient write failure must be RETRIED by the
    # next hook, not silently consume the one record this exit gets.
    calls = []

    def flaky(state_home, record, now=None):
        calls.append(record)
        if len(calls) == 1:
            raise OSError("disk full")
        journal.append(state_home, record, now)

    runner_log.arm(str(home), stream=io.StringIO(), append=flaky)
    assert runner_log.record_signal(signal.SIGTERM) is False
    assert _exits(home) == []
    assert runner_log.record_clean() is True
    assert len(_exits(home)) == 1


def test_recording_before_arming_is_a_no_op(home):
    assert runner_log.record_signal(signal.SIGTERM) is False
    assert runner_log.record_clean() is False
    assert _exits(home) == []


def test_the_exit_record_is_never_a_new_way_to_die(home):
    def explode(*a, **k):
        raise RuntimeError("the journal itself is broken")

    runner_log.arm(str(home), stream=io.StringIO(), append=explode)
    assert runner_log.record_signal(signal.SIGTERM) is False   # returns, does not raise


# --------------------------- the bound on child output ---------------------------

def test_a_noisy_child_cannot_flood_the_log():
    flood = "\n".join(["worker: retrying"] * 5000)
    out = runner_log.bounded(flood)
    assert out.count("\n") < 5
    assert "worker: retrying" in out
    assert "5000" in out                      # the count survives; the 5000 lines do not


def test_the_line_cap_holds_even_when_no_two_lines_repeat():
    varied = "\n".join("line %d" % i for i in range(5000))
    out = runner_log.bounded(varied)
    assert len(out.splitlines()) <= runner_log.CHILD_MAX_LINES + 1
    assert "line 0" in out and "line 4999" in out          # head and tail both kept
    assert "line 2500" not in out


def test_the_char_cap_holds_for_one_enormous_line():
    out = runner_log.bounded("x" * 500_000)
    assert len(out) <= runner_log.CHILD_MAX_CHARS + 200
    assert "truncated" in out


def test_malloc_stack_logging_chatter_is_dropped_entirely():
    # A child whose ONLY output is this line contributes NOTHING to the log. Emitting a "1 line
    # suppressed" note instead would just be the same flood at one line per tick.
    assert runner_log.bounded(NOISE + "\n") == ""
    assert runner_log.bounded("\n".join([NOISE] * 4000)) == ""


def test_real_output_survives_beside_the_chatter_and_the_drop_is_declared():
    out = runner_log.bounded("\n".join([NOISE, "FATAL: pane not found", NOISE]))
    assert "FATAL: pane not found" in out
    assert "MallocStackLogging" not in out
    assert "2" in out and "suppressed" in out


def test_short_ordinary_output_passes_through_untouched():
    text = "launched i5\nnudged i7\n"
    assert runner_log.bounded(text) == text.rstrip("\n")


def test_bounded_never_raises_on_wrong_typed_input():
    assert runner_log.bounded(None) == ""
    assert runner_log.bounded(b"bytes") == ""
    assert runner_log.bounded("   \n  \n") == ""


# --------------------------- the write doorway itself ---------------------------

def test_an_ascii_locale_costs_a_character_not_the_log_line(home, monkeypatch):
    # A login-item runner is started by launchd with a minimal environment: LANG unset means text
    # mode encodes as ASCII, and the engine's own FATAL messages are full of em-dashes. Strict mode
    # RAISES on one — and a UnicodeEncodeError out of the logger lands in whatever tick called it.
    path = str(home / "logs" / "runner.log")
    real_open = open
    with pytest.raises(UnicodeEncodeError):                # the test has teeth: strict really fails
        with real_open(path, "a", encoding="ascii") as f:
            f.write("— strict would die here\n")

    def ascii_open(p, mode="r", *a, **kw):
        if "a" in mode:
            return real_open(p, mode, *a, encoding="ascii", **kw)
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", ascii_open)
    runner_log.append_log(path, "FATAL: the pane — gone\n")     # must not raise
    monkeypatch.undo()
    text = (home / "logs" / "runner.log").read_text()
    assert "FATAL: the pane" in text and "gone" in text


def test_the_write_doorway_swallows_a_filesystem_refusal(home, monkeypatch):
    def _refuse(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("builtins.open", _refuse)
    runner_log.append_log(str(home / "logs" / "runner.log"), "anything\n")   # must not raise


def test_the_bound_emits_only_ascii_so_the_logger_cannot_trip_on_its_own_markers():
    # Whatever the child said, the markers this module ADDS must survive an ASCII log handle —
    # otherwise the bound would be a new way for the logger to fail on output that used to be fine.
    for text in ("\n".join(["x"] * 5000), "\n".join("line %d" % i for i in range(5000)),
                 "y" * 500_000, NOISE + "\nreal error\n"):
        runner_log.bounded(text).encode("ascii")   # raises on a non-ASCII marker of our own
