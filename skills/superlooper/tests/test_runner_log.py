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
    runner_log.note_started()                      # the loop reached its tick loop
    assert runner_log.record_clean() is True
    assert _exits(home)[0]["reason"] == "clean"


def test_a_refused_boot_is_not_recorded_as_a_finished_run(home):
    # The 2026-09-09 morning had two failed starts. Both are worth recording — but "the runner
    # exited" must not read the same for a loop that ran all night and one that never ticked.
    runner_log.arm(str(home), stream=io.StringIO())
    assert runner_log.record_clean() is True
    assert _exits(home)[0]["reason"] == "boot_refused"


def test_only_one_exit_act_is_written_however_many_hooks_fire(home):
    # SIGTERM sets the fail-stop flag, run() returns normally, the interpreter exits: three hooks,
    # ONE death. A log that says the runner exited three times is a log that lies.
    runner_log.arm(str(home), stream=io.StringIO())
    runner_log.record_signal(signal.SIGTERM)
    runner_log.record_signal(signal.SIGTERM)
    runner_log.record_clean()
    assert len(_exits(home)) == 1
    assert _exits(home)[0]["reason"] == "signal"


def test_a_second_signal_landing_inside_the_first_write_cannot_double_the_record(home):
    # `journal.append` opens, writes, flushes and fsyncs — and a Python signal handler runs BETWEEN
    # bytecodes, so a second signal really can arrive mid-write. Simulated by re-entering from
    # inside the writer itself, which is exactly the shape of that race.
    seen = []

    def reentrant(state_home, record, now=None):
        seen.append(record)
        if len(seen) == 1:
            runner_log.record_signal(signal.SIGINT)      # the second signal, mid-write
        journal.append(state_home, record, now)

    runner_log.arm(str(home), stream=io.StringIO(), append=reentrant)
    runner_log.record_signal(signal.SIGTERM)
    assert len(seen) == 1, seen
    assert len(_exits(home)) == 1


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


# --------------------------- who is allowed to leave a record ---------------------------

def test_standing_down_renounces_the_record_but_keeps_the_tee(home):
    # A process that lost the singleton is NOT the runner: it must leave no exit act in the live
    # runner's journal. Its stderr still belongs in the log — somebody tried to start a second one.
    pane = io.StringIO()
    runner_log.arm(str(home), stream=pane)
    runner_log.stand_down()
    sys.stderr.write("another runner is live for this state home — exiting\n")
    assert runner_log.record_clean() is False
    assert runner_log.record_signal(signal.SIGTERM) is False
    assert _exits(home) == []
    assert "another runner is live" in _log_text(home)


def test_standing_down_before_arming_is_a_no_op(home):
    runner_log.stand_down()          # must not raise
    runner_log.note_started()


# --------------------------- the window before run() installs its handlers ---------------------------

def test_arming_covers_sigterm_before_the_runner_installs_its_own_handler(home):
    # `Runner.run` installs the real handlers only AFTER the CLI resolves the anchor, runs the pane
    # preflight, and (login-item home) checks gh auth over the network. A SIGTERM in that window
    # used to hit SIG_DFL: dead process, nothing written down — the very silence this issue is about.
    before = signal.getsignal(signal.SIGTERM)
    armed = runner_log.arm(str(home), stream=io.StringIO())
    handler = signal.getsignal(signal.SIGTERM)
    assert handler is not before, "arm() left the boot window uncovered"
    assert armed["signals"][signal.SIGTERM] is before
    runner_log.disarm()
    assert signal.getsignal(signal.SIGTERM) is before      # and hands it straight back


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_the_boot_handler_records_and_then_dies_exactly_as_it_would_have(home, monkeypatch, signum):
    # It must not CHANGE the process's fate — only whether it left a note. So: record, put back
    # WHATEVER disposition was there before, re-raise at ourselves. Not SIG_DFL: CPython's default
    # for SIGINT is default_int_handler, which raises KeyboardInterrupt and unwinds the stack, and
    # forcing SIG_DFL turned a ^C that ran every `finally` into a hard kill that ran none (review
    # round 2). (os.kill is stubbed; a real one would end the test run.)
    killed = []
    monkeypatch.setattr(runner_log.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    before = signal.getsignal(signum)
    try:
        runner_log.arm(str(home), stream=io.StringIO())
        signal.getsignal(signum)(signum, None)
        assert _exits(home)[0]["signal"] == signal.Signals(signum).name
        assert killed == [(os.getpid(), signum)]
        assert signal.getsignal(signum) is before, "it did not die as it would have"
    finally:
        signal.signal(signum, before)      # restore what this test broke, whatever happened


def test_sigints_real_default_is_not_sig_dfl():
    # The fact the fix above turns on, pinned so nobody "simplifies" it back to SIG_DFL.
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_DFL


# --------------------------- the bound's own edges ---------------------------

def test_a_bound_cannot_be_switched_off_by_passing_a_small_limit():
    # `text[-0:]` is the WHOLE string, so limit 0/1 used to make the bound return MORE than it got.
    # Asserted against the CAPS, not the input: the bug's own output was still smaller than the
    # input, so an input-relative bound passed against it and pinned nothing (review round 2).
    for text in ("\n".join("line %d" % i for i in range(400)), "z" * 40_000):
        for kwargs in ({"max_lines": 0}, {"max_lines": 1}, {"max_lines": 2},
                       {"max_chars": 0}, {"max_chars": 1}, {"max_lines": True}):
            out = runner_log.bounded(text, **kwargs)
            assert len(out) <= runner_log.CHILD_MAX_CHARS + 300, (kwargs, len(out))
            assert len(out.splitlines()) <= runner_log.CHILD_MAX_LINES + 3, (kwargs, len(out))


def test_a_run_of_blank_lines_is_one_blank_line_not_a_count_of_nothing():
    out = runner_log.bounded("first\n\n\n\n\nsecond")
    assert out == "first\n\nsecond", out


def test_control_bytes_and_ansi_paint_never_reach_the_log():
    out = runner_log.bounded("\x1b[31mFATAL\x1b[0m: gone\x00\n")
    assert "FATAL" in out and "gone" in out
    assert "\x1b" not in out and "\x00" not in out


def test_chatter_is_recognised_whatever_the_child_is_called():
    # Real captured shapes plus the ones a spaced process name or an indented line produce. A hole
    # here is a DRIP — one line per child per tick — and neither the fold nor the cap answers a
    # drip (review round 2).
    for name in ("python3", "Python", "sh", "bash", "Google Chrome Helper", "  python3"):
        line = "%s(3938) MallocStackLogging: can't turn off malloc stack logging." % name
        assert runner_log.bounded(line) == "", name


def test_a_line_that_merely_mentions_the_chatter_is_not_censored():
    # `_run_cmd` pipes this repo's own recheck output through the same doorway. An unanchored
    # substring match ate the failing assertion line of the test named for this very feature.
    out = runner_log.bounded("FAILED test_runner_log.py::test_malloc_stack_logging_chatter\n"
                             "E   assert 'MallocStackLogging' not in out\n")
    assert "assert 'MallocStackLogging' not in out" in out
    assert "suppressed" not in out


def test_a_monstrous_write_is_bounded_without_reading_all_of_it():
    flood = "\n".join("row %d" % i for i in range(400_000))          # ~4 MB
    out = runner_log.bounded(flood)
    assert len(out) <= runner_log.CHILD_MAX_CHARS + 300
    assert "row 0" in out and "row 399999" in out      # head AND tail both kept
    assert "were not read" in out                      # and the gap is declared


def test_a_chatter_only_flood_past_the_scan_window_still_says_nothing():
    assert runner_log.bounded("\n".join([NOISE] * 40_000)) == ""


def test_the_tail_survives_a_write_whose_tail_slice_holds_one_huge_line():
    # The tail is where a failing command's reason lives. A tail slice whose only newline is its
    # LAST character used to trim to "" — every byte of the later, error-bearing line gone, with
    # only a char count hinting at it (review round 2).
    out = runner_log.bounded("A" * 500_000 + "\n" + "B" * 300_000 + "\n")
    assert "B" in out, out[:200]
    assert "A" in out


def test_one_colossal_line_with_no_newline_is_still_reported():
    out = runner_log.bounded("q" * 4_000_000)
    assert out and "q" in out and len(out) <= runner_log.CHILD_MAX_CHARS + 300
