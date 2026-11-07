"""`superlooper focus-session` — the engine's one read-only door onto a lane's window (issue #339).

Two layers, and the split matters:

* **``lib/focus``** — how a lane id becomes a window, and which of the four outcomes each way out
  produces. Driven with an injected doorway, so nothing here resolves a host binary.
* **the CLI** — the William-facing contract: the flags, the exit codes, the JSON a UI parses. Run
  as a real subprocess against ``fakes/fake-sessionhost`` (via ``SL_HERDR``), which speaks the real
  envelope and REFUSES a workspace it never issued — the property the cross-repo test rests on.

The multi-repo case is the one this issue was written around. On a multi-repo install ``i310`` does
not name one agent host-wide: two adopted repos both have one, and the host clears an agent's name
when it exits, so repo A's finished lane and repo B's live one can carry the same name minutes
apart. Everything below that says "repo A cannot reach repo B" is testing that the id is used to
select a file under ONE repo's state home and never to address the host.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import focus as focus_lib
import viewer
import panes
import session_host

from test_cli import cli, rig  # noqa: F401  (rig is a fixture)

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FAKE_HOST = Path(__file__).resolve().parent / "fakes" / "fake-sessionhost"


# --------------------------------------------------------------------- lib/focus

class FakeDoor:
    """A stand-in for the doorway's `focus` verb. Records what it was handed, answers to order."""

    def __init__(self, answer=None):
        self.answer = answer or session_host.Focus(session_host.FOCUSED, "front")
        self.sessions = []

    def focus(self, session):
        self.sessions.append(session)
        return self.answer


def _record(home, iid, pane, workspace):
    panes.record(os.path.join(str(home), "state"), iid,
                 session_host.Session(name=iid, workspace=workspace, pane=pane))


@pytest.mark.parametrize("iid", ["", None, 339, "339", "i", "../i339", "i33 9", "i339/../i340",
                                 "banana"])
def test_an_id_that_is_not_a_lane_never_reaches_the_state_directory(tmp_path, iid):
    # The id is about to select a file under state/panes/. A shape check first is what keeps
    # `../../../etc` a refusal rather than a read.
    door = FakeDoor()
    got = focus_lib.focus_lane(tmp_path, iid, host=door)
    assert got.outcome == focus_lib.UNKNOWN_LANE
    assert door.sessions == [], "the host is never asked about something that is not a lane"


def test_surrounding_whitespace_is_tolerated_and_nothing_else_is(tmp_path):
    # Trimmed like `resume` trims its own argument: a UI that passes " i339\n" means i339, and
    # refusing it would be pedantry. The character class itself is NOT relaxed — see above.
    _record(tmp_path, "i339", "w4:p1", "w4")
    assert focus_lib.focus_lane(tmp_path, "  i339\n", host=FakeDoor()).outcome == focus_lib.FOCUSED


def test_a_lane_with_no_recorded_window_answers_no_window_without_asking_the_host(tmp_path):
    """The COMMON case (issue #339): the session exited, or `tidy` closed the window and removed
    the marker. There is nothing to ask the host about, so nothing is asked."""
    door = FakeDoor()
    got = focus_lib.focus_lane(tmp_path, "i339", host=door)
    assert (got.outcome, got.focused) == (focus_lib.NO_WINDOW, False)
    assert door.sessions == []
    assert "i339" in got.detail


def test_a_half_written_marker_is_no_window_rather_than_a_focus_of_nothing(tmp_path):
    """`lib/panes` writes the workspace first precisely so the surviving half-state is the harmless
    one. A pane with no workspace cannot name a window — `as_session` returns None — and this verb
    must read that as "no window", never build a handle out of it."""
    panes.write_atomic(os.path.join(str(tmp_path), "state", "panes", "i339"), "w9:p1")
    door = FakeDoor()
    got = focus_lib.focus_lane(tmp_path, "i339", host=door)
    assert got.outcome == focus_lib.NO_WINDOW
    assert door.sessions == []


def test_the_window_is_addressed_by_the_recorded_workspace(tmp_path):
    _record(tmp_path, "i339", "w4:p1", "w4")
    door = FakeDoor()
    got = focus_lib.focus_lane(tmp_path, "i339", host=door)
    assert (got.outcome, got.workspace) == (focus_lib.FOCUSED, "w4")
    assert [s.workspace for s in door.sessions] == ["w4"]
    assert door.sessions[0].name == "i339"


def test_a_debugger_lane_is_focusable_too(tmp_path):
    # `tidy` narrows to i<N> because it CLOSES windows and a debugger seat is the owner's to close.
    # Focusing destroys nothing, so the narrowing would only stop him looking at his own session.
    _record(tmp_path, "d12", "w7:p1", "w7")
    got = focus_lib.focus_lane(tmp_path, "d12", host=FakeDoor())
    assert got.outcome == focus_lib.FOCUSED


@pytest.mark.parametrize("outcome", [session_host.NO_WINDOW, session_host.HOST_UNREACHABLE])
def test_the_doorways_verdict_is_carried_out_verbatim(tmp_path, outcome):
    """The three host-side words are the doorway's, re-spelled nowhere. A translation layer here is
    how "the host could not be reached" would one day arrive at a UI as "your session is gone"."""
    _record(tmp_path, "i339", "w4:p1", "w4")
    door = FakeDoor(session_host.Focus(outcome, "because"))
    got = focus_lib.focus_lane(tmp_path, "i339", host=door)
    assert (got.outcome, got.focused, got.detail) == (outcome, False, "because")


@pytest.mark.parametrize("home", [None, "", "   ", 7, b"/tmp"])
def test_a_state_home_that_is_not_a_path_is_refused_rather_than_guessed(home):
    """The house 'fail-open on wrong-typed input' class. A None home joins to "None/state" against
    whatever the process's CWD happens to be and finds nothing — which would answer NO_WINDOW: a
    confident statement about a lane's window, made after looking in a directory nobody named."""
    door = FakeDoor()
    got = focus_lib.focus_lane(home, "i339", host=door)
    assert got.outcome == focus_lib.UNKNOWN_LANE
    assert door.sessions == []


def test_every_failure_has_its_own_exit_code_and_only_a_window_in_front_is_zero():
    # Zero is "something is in front of you": the host's focus (lib/focus's own answer, which
    # `superlooper debug` still uses) or, through the CLI, a viewer window opened or switched
    # (#547). Every other answer is non-zero and distinct, so a shell caller can tell them apart.
    successes = {focus_lib.FOCUSED, focus_lib.OPENED, focus_lib.SWITCHED}
    assert {o for o in focus_lib.OUTCOMES if focus_lib.EXIT_CODES[o] == 0} == successes
    failures = [focus_lib.EXIT_CODES[o] for o in focus_lib.OUTCOMES if o not in successes]
    assert len(set(failures)) == len(failures), "a shell caller must be able to tell these apart"
    assert all(focus_lib.Result(o).ok == (o in successes) for o in focus_lib.OUTCOMES)


def test_a_caller_with_less_patience_than_the_default_gets_a_shorter_doorway(tmp_path, monkeypatch):
    """`superlooper debug` reaches here having already spent most of its budget on the launch
    (issue #459), and must be able to say so without building a doorway of its own — the one
    construction stays in this module. Absent, the caller gets the default this verb documents."""
    built = []

    class _Door:
        def __init__(self, **kw):
            built.append(kw)

        def focus(self, session):
            return session_host.Focus(session_host.FOCUSED, "front")

    monkeypatch.setattr(session_host, "SessionHost", _Door)
    _record(tmp_path, "d4", "w1:p1", "w1")
    assert focus_lib.focus_lane(tmp_path, "d4", call_seconds=3).outcome == focus_lib.FOCUSED
    assert built[-1]["call_seconds"] == 3
    focus_lib.focus_lane(tmp_path, "d4")
    assert built[-1]["call_seconds"] == focus_lib.CALL_SECONDS
    # It can only SHORTEN. The ceiling belongs to this verb — a caller that asked for a minute
    # would be quietly re-deciding that a person watching for their window can wait at a spinner.
    focus_lib.focus_lane(tmp_path, "d4", call_seconds=600)
    assert built[-1]["call_seconds"] == focus_lib.CALL_SECONDS


def test_a_recorded_window_that_cannot_address_anything_is_an_answer_not_a_traceback(tmp_path):
    """The doorway RAISES on a handle it cannot address — right for it, wrong to let out of here.
    `focus_lane` promises a UI four outcomes, and a traceback with rc=1 and no JSON is the exact
    failure shape this verb exists to replace (verification pass)."""
    _record(tmp_path, "i339", "w4:p1", "--help")
    # The REAL doorway over a probe that cannot be touched — a FakeDoor would prove nothing here,
    # since what is under test is the doorway's own refusal arriving as an outcome.
    got = focus_lib.focus_lane(tmp_path, "i339", host=session_host.SessionHost(probe=_NoHost()))
    assert got.outcome == focus_lib.UNKNOWN_LANE
    assert got.exit_code in focus_lib.EXIT_CODES.values()
    assert "--help" in got.detail, "the offending marker is what a reader needs to see"


class _NoHost:
    """A probe that fails the test loudly if anything reaches the host edge at all.

    This is the live guard in the test above (verification pass): delete the doorway's flag check
    and this fires, because the unaddressable id would otherwise go straight into argv.
    """

    def run(self, argv, timeout=None):
        raise AssertionError("no host call may be made for an unaddressable handle: %s" % (argv,))


def test_the_json_shape_cannot_be_built_contradicting_itself():
    """`focused` is derived from the outcome so the two can never disagree; a free-form `**extra`
    on the serialiser would have handed that contradiction straight back (fresh-agent review)."""
    payload = focus_lib.Result(focus_lib.NO_WINDOW, "i339").as_dict(repo="o/r")
    assert (payload["ok"], payload["outcome"], payload["repo"]) == (False, "no_window", "o/r")
    assert set(payload) == {"ok", "verb", "id", "outcome", "workspace", "repo", "detail"}


def test_a_state_home_with_an_embedded_nul_is_an_answer_rather_than_a_traceback(tmp_path):
    """`open()` raises ValueError — not OSError — on a NUL in the path, and `lib/panes` caught only
    OSError. Reachable from a config `repo` carrying one, and `focus_lane` is called OUTSIDE the
    CLI's own try, so it would have surfaced as a traceback to whatever shelled the verb."""
    assert focus_lib.focus_lane(str(tmp_path) + "/a\x00b", "i339",
                                host=FakeDoor()).outcome == focus_lib.NO_WINDOW


def test_no_outcome_borrows_argparses_own_exit_code():
    """2 belongs to argparse — a usage error, and what an engine too old to have this verb exits
    with on `invalid choice`. An outcome sharing it would let "this engine has never heard of this
    command" render as one of the four answers (fresh-agent review)."""
    assert 2 not in focus_lib.EXIT_CODES.values()


def test_a_lane_id_from_one_repo_cannot_reach_another_repos_window(tmp_path):
    """THE multi-repo regression (issue #339 DoD), at the resolution layer.

    Two adopted repos, each with its own state home, each with a lane called `i310` — the exact
    collision #310's worker surfaced. The id is repo-scoped bookkeeping and the workspace is the
    address, so neither repo's call can name the other's window.
    """
    home_a, home_b = tmp_path / "o__a", tmp_path / "o__b"
    _record(home_a, "i310", "wA:p1", "wA")
    _record(home_b, "i310", "wB:p1", "wB")

    door_a, door_b = FakeDoor(), FakeDoor()
    assert focus_lib.focus_lane(home_a, "i310", host=door_a).workspace == "wA"
    assert focus_lib.focus_lane(home_b, "i310", host=door_b).workspace == "wB"
    assert [s.workspace for s in door_a.sessions] == ["wA"]
    assert [s.workspace for s in door_b.sessions] == ["wB"]


def test_a_repo_whose_lane_window_is_gone_never_falls_back_to_another_repos(tmp_path):
    """The nastier half of the same collision: repo A's lane FINISHED (its marker is gone, and the
    host has cleared the name) while repo B's `i310` is live. A name-addressed focus would land on
    repo B's session; a recorded-workspace one has nothing to land on and says so."""
    _record(tmp_path / "o__b", "i310", "wB:p1", "wB")
    door = FakeDoor()
    got = focus_lib.focus_lane(tmp_path / "o__a", "i310", host=door)
    assert got.outcome == focus_lib.NO_WINDOW
    assert door.sessions == []


# --------------------------------------------- show_lane: a window in front of the owner (#547)
#
# Owner ruling 2026-11-06: "When I click that button, it should open a terminal window, even if
# terminal is not running with the herdr session." `show_lane` is focus_lane plus the window: the
# host's focus goes through the one door exactly as before, and then a viewer window is either
# switched-and-raised (one is attached) or opened (none is). Measured on a private fenced server
# while building this: a client that attaches AFTER the focus lands on the focused workspace, so
# the focus is asked first — which is also what lets a lane with no window open nothing at all.

_TERMINAL = "/System/Applications/Utilities/Terminal.app"


class FakeScreen:
    """The window side, scripted: a process table, an opener, an app to activate, a terminal to
    raise. `open` attaches a viewer to the table (inside Terminal) unless told the viewer fails to
    attach; time is virtual, so a bounded wait costs the suite nothing."""

    def __init__(self, prefix, console=True, attached=0, open_rc=0, open_err="", attaches=True,
                 ps=True, activate_rc=0, raise_ok=True, log=None):
        import fleet
        self.argv = fleet.viewer_argv(fleet.host_binary(str(prefix)), fleet.SESSION_NAME)
        self.console, self.open_rc, self.open_err = console, open_rc, open_err
        self.attaches, self.ps, self.activate_rc, self.raise_ok = attaches, ps, activate_rc, raise_ok
        self.log = log if log is not None else []
        self.t = 0.0
        self.rows = [viewer.Proc(1, 0, "??", "/sbin/launchd"),
                     viewer.Proc(500, 1, "??", _TERMINAL + "/Contents/MacOS/Terminal"),
                     viewer.Proc(600, 1, "??", " ".join(self.argv + ["server"]))]
        self.next_pid = 700
        self.opened, self.activated, self.raised = [], [], []
        for _ in range(attached):
            self._attach()

    def _attach(self):
        pid = self.next_pid
        self.next_pid += 1
        self.rows.append(viewer.Proc(pid, 500, "ttys%03d" % (pid % 1000), " ".join(self.argv)))

    def viewers(self):
        return [r for r in self.rows if r.args == " ".join(self.argv)]

    def console_is_ours(self):
        return self.console

    def processes(self):
        return list(self.rows) if self.ps else None

    def open(self, path):
        self.opened.append(str(path))
        self.log.append(("open", str(path)))
        if self.open_rc:
            return self.open_rc, self.open_err
        if self.attaches:
            self._attach()
        return 0, ""

    def activate(self, app):
        self.activated.append(app)
        return self.activate_rc, "" if not self.activate_rc else "no such app"

    def raise_tty(self, tty):
        self.raised.append(tty)
        return (True, "") if self.raise_ok else (False, "not a terminal")

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


class LoggingDoor(FakeDoor):
    def __init__(self, log, answer=None):
        FakeDoor.__init__(self, answer)
        self.log = log

    def focus(self, session):
        self.log.append(("focus", session.workspace))
        return FakeDoor.focus(self, session)


def _fleet(tmp_path):
    """A fleet prefix with a viewer script in it, as `superlooper fleet --install` leaves one."""
    prefix = tmp_path / "fleet"
    prefix.mkdir(exist_ok=True)
    (prefix / "viewer.command").write_text("#!/bin/sh\n# superlooper fleet viewer\n")
    return prefix


def _show(tmp_path, screen, door=None, iid="i547", **kw):
    return focus_lib.show_lane(tmp_path / "home", iid, str(tmp_path / "fleet"),
                               host=door or FakeDoor(), screen=screen, **kw)


def test_with_no_viewer_attached_one_tap_opens_one_on_the_lane(tmp_path):
    """DoD: no viewer attached -> the machine's handler opens the viewer script ONCE, and the focus
    lands on that lane. The focus is asked FIRST: the measured host lands a client that attaches
    afterwards on the focused workspace, so the window comes up already showing the lane."""
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    log = []
    screen = FakeScreen(prefix, log=log)
    got = _show(tmp_path, screen, door=LoggingDoor(log))
    assert got.outcome == focus_lib.OPENED and got.ok, got.detail
    assert screen.opened == [str(prefix / "viewer.command")], "the opener runs exactly once"
    assert log == [("focus", "w9"), ("open", str(prefix / "viewer.command"))]
    assert len(screen.viewers()) == 1
    assert (got.id, got.workspace) == ("i547", "w9")
    assert "opened" in got.detail


def test_with_a_viewer_attached_a_tap_switches_and_raises_it_and_opens_nothing(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, attached=1)
    door = FakeDoor()
    got = _show(tmp_path, screen, door=door)
    assert got.outcome == focus_lib.SWITCHED and got.ok, got.detail
    assert screen.opened == [], "an attached viewer is switched, never joined by a second window"
    assert [s.workspace for s in door.sessions] == ["w9"]
    assert screen.raised == ["ttys700"] and screen.activated == [_TERMINAL]
    assert len(screen.viewers()) == 1


def test_two_taps_in_a_row_leave_exactly_one_viewer(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    _record(tmp_path / "home", "i548", "w3:p1", "w3")
    screen = FakeScreen(prefix)
    door = FakeDoor()
    first = _show(tmp_path, screen, door=door)
    second = _show(tmp_path, screen, door=door, iid="i548")
    assert (first.outcome, second.outcome) == (focus_lib.OPENED, focus_lib.SWITCHED)
    assert len(screen.opened) == 1 and len(screen.viewers()) == 1
    assert [s.workspace for s in door.sessions] == ["w9", "w3"], "and the second lane is shown"


def test_a_tap_while_another_is_still_opening_waits_for_it_rather_than_opening_a_second(tmp_path):
    # The dashboard serves each request on its own thread, so a double tap is two CLI processes.
    # The second waits for the first's open, bounded; it never opens a viewer beside it.
    import fcntl
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix)
    with open(str(prefix / focus_lib.VIEWER_LOCK), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.OPEN_FAILED and not got.ok
    assert screen.opened == []
    assert "still opening" in got.detail


def test_a_lane_with_no_window_answers_no_window_and_opens_no_viewer(tmp_path):
    prefix = _fleet(tmp_path)
    screen = FakeScreen(prefix)
    got = _show(tmp_path, screen)                                 # nothing recorded for i547
    assert got.outcome == focus_lib.NO_WINDOW and not got.ok
    assert screen.opened == [] and screen.activated == [] and screen.raised == []


@pytest.mark.parametrize("outcome", [session_host.NO_WINDOW, session_host.HOST_UNREACHABLE])
def test_a_focus_the_host_did_not_give_opens_no_viewer(tmp_path, outcome):
    # A window the host says is gone, or a host that would not answer: nothing to show, so nothing
    # is opened — and the host's own answer is what comes back.
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, attached=1)
    got = _show(tmp_path, screen, door=FakeDoor(session_host.Focus(outcome, "because")))
    assert (got.outcome, got.detail) == (outcome, "because")
    assert screen.opened == [] and screen.activated == [] and screen.raised == []


def test_no_gui_login_session_is_said_plainly_and_is_no_success(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, console=False)
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.NO_DISPLAY and not got.ok
    assert got.exit_code not in (0, 2)
    assert "no one is logged in" in got.detail or "login session" in got.detail
    assert screen.opened == []


def test_a_failed_opener_is_said_plainly_and_is_no_success(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, open_rc=1, open_err="LSOpenURLsWithRole() failed with error -10810")
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.OPEN_FAILED and not got.ok
    assert "-10810" in got.detail, "the opener's own words reach the owner"
    assert screen.opened == [str(prefix / "viewer.command")]


def test_a_missing_viewer_script_names_the_install_that_writes_it(tmp_path):
    (tmp_path / "fleet").mkdir()
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(tmp_path / "fleet")
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.OPEN_FAILED and not got.ok
    assert "superlooper fleet --install" in got.detail
    assert screen.opened == []


def test_a_viewer_that_never_attaches_is_said_plainly_after_a_bounded_wait(tmp_path):
    # The tokenless pre-#547 script was refused by the fence and exited in ~40ms (measured): the
    # window opened, the viewer died, and the old button would still have reported success.
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, attaches=False)
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.VIEWER_NOT_ATTACHED and not got.ok
    assert screen.opened == [str(prefix / "viewer.command")]
    assert screen.t <= focus_lib.VIEWER_ATTACH_SECONDS + 1, "the wait is bounded"
    assert "superlooper fleet --install" in got.detail, "it names the likeliest cure"


def test_a_process_table_that_cannot_be_read_opens_nothing(tmp_path):
    # "We could not look" is not "nobody is attached": opening on it is how a second window starts.
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, ps=False)
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.OPEN_FAILED and not got.ok
    assert screen.opened == []


def test_a_switched_viewer_whose_app_will_not_come_forward_says_so(tmp_path):
    # It IS switched — the window on the screen now shows the lane — so it is not a failure; but
    # the answer must not claim the window came to the front when it did not.
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix, attached=1, activate_rc=1)
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.SWITCHED and got.ok
    assert "could not bring" in got.detail
    assert "brought it to the front" not in got.detail


def test_a_viewer_attached_over_ssh_is_not_a_window_on_this_screen(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix)
    screen.rows += [viewer.Proc(800, 1, "??", "/usr/sbin/sshd"),
                    viewer.Proc(801, 800, "ttys801", " ".join(screen.argv))]
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.OPENED, "that client is in no window the owner can be shown"
    assert screen.raised == [] and len(screen.opened) == 1


def test_debugs_own_focus_is_untouched_and_opens_no_window():
    # Boundary: `superlooper debug`'s focus attempt and the watchdog's debugger open no window.
    # They call focus_lane, which knows nothing about a screen; only the CLI verb calls show_lane.
    import inspect
    assert "screen" not in inspect.signature(focus_lib.focus_lane).parameters
    src = (_ROOT / "skill" / "bin" / "superlooper").read_text()
    assert src.count("focus_lib.show_lane(") == 1
    debug = src[src.index("def _debug_focus("):]
    debug = debug[:debug.index("\ndef ", 1)]
    assert "show_lane" not in debug and "focus_lib.focus_lane(" in debug


# --------------------------------------------------------------------- the CLI

def _fake_host_env(rig, tmp_path):
    hostdir = tmp_path / "fakehost"
    hostdir.mkdir(exist_ok=True)
    return {"SL_HERDR": str(_FAKE_HOST), "FAKE_HOST_DIR": str(hostdir), "HOST_MODE": "hollow"}, \
        hostdir


def _screen_env(rig, tmp_path, attached=True):
    """A scripted SCREEN for a real CLI run (#547): a console this uid owns, a `ps` that prints a
    table, an opener that logs what it was handed, and a device directory holding the viewer's tty.

    With ``attached`` the table already holds a viewer inside Terminal; without it, the opener
    attaches one — the fake `ps` shows it once the opener has run, exactly as a real viewer
    appears in the real table a beat after `open`."""
    import fleet
    scr = tmp_path / "screen"
    scr.mkdir(exist_ok=True)
    (scr / "console").write_text("")
    (scr / "dev").mkdir(exist_ok=True)
    (scr / "dev" / "ttys004").write_text("")
    prefix = Path(rig.env["SL_HOME"]) / "fleet"
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "viewer.command").write_text("#!/bin/sh\n# superlooper fleet viewer\n")
    argv = " ".join(fleet.viewer_argv(fleet.host_binary(str(prefix)), fleet.SESSION_NAME))
    terminal = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
    if attached:
        (scr / "attached").write_text("")
    ps = scr / "ps"
    ps.write_text("#!/bin/sh\n"
                  "echo '    1     0 ??       /sbin/launchd'\n"
                  "echo '  500     1 ??       %s'\n"
                  "if [ -e '%s' ]; then echo '  520   500 ttys004  %s'; fi\n"
                  % (terminal, scr / "attached", argv))
    ps.chmod(0o755)
    opener = scr / "open"
    opener.write_text("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> '%s'\n"
                      "case \"$1\" in -a) ;; *) : > '%s';; esac\n"
                      % (scr / "opened", scr / "attached"))
    opener.chmod(0o755)
    return {"SL_CONSOLE": str(scr / "console"), "SL_PS": str(ps), "SL_OPEN": str(opener),
            "SL_TTY_DIR": str(scr / "dev")}, scr


def _issue(hostdir, ws):
    """Mark a workspace LIVE on the fake host — what `workspace create` would have left behind."""
    (hostdir / ("live.%s" % ws)).write_text("")


def _second_repo(rig, slug):
    """A second adopted repo, so the multi-repo case is two real configs and two real state homes."""
    repo = rig.tmp / slug.replace("/", "__")
    (repo / ".superlooper").mkdir(parents=True)
    (repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": slug, "required_checks": ["quality-gate"]}))
    return repo


def _home(rig, slug):
    return Path(rig.env["SL_HOME"]) / slug.replace("/", "__")


def test_cli_focuses_the_lane_window_through_the_doorway(rig, tmp_path):
    env, hostdir = _fake_host_env(rig, tmp_path)
    screen, scr = _screen_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339",
            env_over={**env, **screen})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "w1" in r.stdout and "switched" in r.stdout
    assert [json.loads(x)["workspace"] for x in
            (hostdir / "focused.jsonl").read_text().splitlines()] == ["w1"]


def test_cli_opens_the_fleet_viewer_when_none_is_attached_and_reports_opened(rig, tmp_path):
    """#547 end to end through the real CLI: fake host, fake opener. The opener is handed the
    fleet's viewer script ONCE, a viewer attaches, and the host's focus landed on that lane."""
    env, hostdir = _fake_host_env(rig, tmp_path)
    screen, scr = _screen_env(rig, tmp_path, attached=False)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json",
            env_over={**env, **screen})
    got = json.loads(r.stdout)
    assert (got["outcome"], got["ok"], r.returncode) == ("opened", True, 0), r.stdout + r.stderr
    viewer_script = Path(rig.env["SL_HOME"]) / "fleet" / "viewer.command"
    assert (scr / "opened").read_text().splitlines() == [str(viewer_script)]
    assert [json.loads(x)["workspace"] for x in
            (hostdir / "focused.jsonl").read_text().splitlines()] == ["w1"]


def test_cli_with_no_gui_session_says_so_and_opens_nothing(rig, tmp_path):
    # The conftest's default screen: no console of ours. The host's focus still happened (so a
    # viewer attached later shows the lane), but no window could be shown and none is claimed.
    env, hostdir = _fake_host_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json", env_over=env)
    got = json.loads(r.stdout)
    assert (got["outcome"], got["ok"]) == ("no_display", False)
    assert r.returncode == focus_lib.EXIT_CODES[focus_lib.NO_DISPLAY]
    plain = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", env_over=env)
    assert "login session" in plain.stderr


def test_cli_focus_is_the_only_thing_the_verb_asks_the_host_for(rig, tmp_path):
    """The DoD's "its whole effect is a focus", end to end: every argv the host binary received,
    across a real CLI run, is one `workspace focus`."""
    env, hostdir = _fake_host_env(rig, tmp_path)
    screen, _scr = _screen_env(rig, tmp_path, attached=False)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    # The OPENED path, the longest one: opening a viewer still asks the host for nothing else.
    assert cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339",
               env_over={**env, **screen}).returncode == 0
    calls = [json.loads(x) for x in (hostdir / "calls.jsonl").read_text().splitlines()]
    assert calls == [["workspace", "focus", "w1"]], (
        "focus must neither spawn, send, exit nor kill: %s" % (calls,))


def test_cli_json_carries_the_outcome_in_a_machine_readable_field(rig, tmp_path):
    env, hostdir = _fake_host_env(rig, tmp_path)
    screen, _scr = _screen_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json",
            env_over={**env, **screen})
    got = json.loads(r.stdout)
    assert got["outcome"] == "switched"
    assert (got["ok"], got["verb"], got["id"], got["workspace"]) == (
        True, "focus-session", "i339", "w1")
    assert got["repo"] == "o/r"


def test_cli_reports_no_window_as_an_answer_rather_than_a_failure(rig, tmp_path):
    """A lane whose session ended is the common case. It must be distinguishable from a host fault
    — a distinct outcome, a distinct exit code — and it must not read as an error: the line goes to
    STDOUT, and nothing about it says the machinery broke."""
    env, _hostdir = _fake_host_env(rig, tmp_path)
    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i777", "--json", env_over=env)
    assert json.loads(r.stdout)["outcome"] == "no_window"
    assert r.returncode == focus_lib.EXIT_CODES[focus_lib.NO_WINDOW]

    plain = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i777", env_over=env)
    assert plain.stderr.strip() == "", "an ordinary answer does not go to stderr"
    assert "i777" in plain.stdout


def test_cli_reports_a_window_the_host_has_forgotten_as_no_window(rig, tmp_path):
    """The other road to the same answer, and the one that needs the host: the marker is still on
    disk but the window is gone (a `tidy` that could not clean up, a host restart). The host's
    `workspace_not_found` is what says so."""
    env, hostdir = _fake_host_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w9:p1", "w9")        # never issued on the fake host

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json", env_over=env)
    assert json.loads(r.stdout)["outcome"] == "no_window"
    assert r.returncode == focus_lib.EXIT_CODES[focus_lib.NO_WINDOW]
    assert not (hostdir / "focused.jsonl").exists()


def test_cli_reports_an_unreachable_host_as_its_own_outcome(rig, tmp_path):
    """A host that cannot even be RUN is not the same answer as a lane with no window, and a caller
    that could not tell them apart would tell the owner his session had ended every time the host
    was down."""
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    env = {"SL_HERDR": str(tmp_path / "no-such-host-binary")}

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json", env_over=env)
    got = json.loads(r.stdout)
    assert got["outcome"] == "host_unreachable"
    assert r.returncode == focus_lib.EXIT_CODES[focus_lib.HOST_UNREACHABLE]

    plain = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", env_over=env)
    assert plain.stderr.strip(), "a machinery fault is reported on stderr"


@pytest.mark.parametrize("iid", ["banana", "", "339"])
def test_cli_reports_an_id_that_is_not_a_lane(rig, tmp_path, iid):
    env, _hostdir = _fake_host_env(rig, tmp_path)
    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", iid, "--json", env_over=env)
    assert json.loads(r.stdout)["outcome"] == "unknown_lane"
    assert r.returncode == focus_lib.EXIT_CODES[focus_lib.UNKNOWN_LANE]


def test_cli_reports_a_repo_it_does_not_know(rig, tmp_path):
    """An unadopted directory is a thing an owner can point a UI at by accident. It answers, it
    names the path, and it does not hand a traceback to whatever is parsing the output."""
    env, _hostdir = _fake_host_env(rig, tmp_path)
    stranger = tmp_path / "not-adopted"
    stranger.mkdir()
    r = cli(rig, "focus-session", "--repo", str(stranger), "--id", "i339", "--json", env_over=env)
    got = json.loads(r.stdout)
    assert got["outcome"] == "unknown_lane"
    assert str(stranger) in got["detail"]
    assert "Traceback" not in (r.stdout + r.stderr)


def test_cli_a_lane_id_from_one_repo_cannot_focus_another_repos_window(rig, tmp_path):
    """THE multi-repo regression (issue #339 DoD), end to end through the real CLI.

    Two adopted repos on one machine, both with a lane `i310`, both windows live on the same host.
    Each call must reach its OWN repo's workspace — and the fake host refuses any id it never
    issued, so a call that addressed the host by lane name could not quietly pass here either.
    """
    env, hostdir = _fake_host_env(rig, tmp_path)
    env = {**env, **_screen_env(rig, tmp_path)[0]}
    other = _second_repo(rig, "o/other")
    _record(_home(rig, "o/r"), "i310", "wA:p1", "wA")
    _record(_home(rig, "o/other"), "i310", "wB:p1", "wB")
    _issue(hostdir, "wA")
    _issue(hostdir, "wB")

    assert cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i310",
               env_over=env).returncode == 0
    assert cli(rig, "focus-session", "--repo", str(other), "--id", "i310",
               env_over=env).returncode == 0
    focused = [json.loads(x)["workspace"]
               for x in (hostdir / "focused.jsonl").read_text().splitlines()]
    assert focused == ["wA", "wB"], (
        "each repo's lane must reach its own window and no other: %s" % (focused,))


def test_cli_will_not_focus_a_sibling_repos_window_for_a_lane_of_its_own_that_ended(rig, tmp_path):
    """The collision that actually bites: repo A's `i310` finished (marker gone, host name cleared)
    while repo B's `i310` is live. The honest answer is "no window", not repo B's session."""
    env, hostdir = _fake_host_env(rig, tmp_path)
    other = _second_repo(rig, "o/other")
    _record(_home(rig, "o/other"), "i310", "wB:p1", "wB")
    _issue(hostdir, "wB")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i310", "--json", env_over=env)
    assert json.loads(r.stdout)["outcome"] == "no_window"
    assert not (hostdir / "focused.jsonl").exists(), (
        "nothing may be focused for a lane this repo has no window for")
    assert other.exists()


def test_a_usage_error_stays_argparses_and_is_not_one_of_the_four_outcomes(rig):
    """The other half of the exit-code rule, driven: a bad flag exits 2 with NO json on stdout, so
    a caller cannot confuse it with an answer this verb gave."""
    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--not-a-flag")
    assert r.returncode == 2
    assert r.stdout.strip() == "", "a usage error produces no outcome object to misread"


def test_focus_session_is_documented_in_the_clis_own_help():
    top = subprocess.run([sys.executable, str(CLI), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert "focus-session" in top.stdout
    verb = subprocess.run([sys.executable, str(CLI), "focus-session", "--help"],
                          capture_output=True, text=True, timeout=60)
    for word in ("--id", "--json", "--repo", "no_window", "host_unreachable", "unknown_lane"):
        assert word in verb.stdout, "`focus-session --help` must document %s" % word


def test_an_attached_viewer_with_no_app_is_switched_never_joined_by_a_second(tmp_path):
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    screen = FakeScreen(prefix)
    screen.rows += [viewer.Proc(900, 1, "??", "/opt/homebrew/bin/tmux"),
                    viewer.Proc(901, 900, "ttys901", " ".join(screen.argv))]
    got = _show(tmp_path, screen)
    assert got.outcome == focus_lib.SWITCHED and got.ok
    assert screen.opened == [] and screen.activated == []
    assert "not brought to the front" in got.detail


def test_the_focus_waits_its_turn_behind_a_tap_still_opening(tmp_path):
    # Fresh-agent review: with the focus OUTSIDE the lock, tap B's focus could land between tap A's
    # focus and A's viewer attaching — A would report its own lane while the window drew B's. So
    # the whole tap, focus included, runs under the lock; a tap that cannot get it focuses nothing.
    import fcntl
    prefix = _fleet(tmp_path)
    _record(tmp_path / "home", "i547", "w9:p1", "w9")
    door = FakeDoor()
    with open(str(prefix / focus_lib.VIEWER_LOCK), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        got = _show(tmp_path, FakeScreen(prefix), door=door)
    assert got.outcome == focus_lib.OPEN_FAILED
    assert door.sessions == [], "no focus was moved while another tap held the window"
