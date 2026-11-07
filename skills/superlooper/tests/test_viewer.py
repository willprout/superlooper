"""The window side of Open session window (issue #547): find, open and raise the fleet's viewer.

`lib/viewer` is the OS half of putting a terminal window on a lane in front of the owner. It never
talks to the session host — the focus itself goes through the one door (`lib/session_host`) via
`lib/focus` — and these tests pin what it reads and what it runs:

* the process table, to answer "is a viewer already attached?" — because the pinned host's API has
  no way to list its clients (#547 finding 1);
* the machine's normal handler for the viewer script (`open <viewer.command>`), to open one;
* the app the viewer runs in, to bring an attached one forward.

Everything is injected. Nothing here reaches a real `ps`, a real `open`, the real console or a
real terminal device — the conftest points all four at paths that cannot exist, and the last test
in this file is the guard that the neutralization is still in place.
"""
import os
import stat
from pathlib import Path

import pytest

import viewer

_BIN = "/home/will/.superlooper/fleet/bin/host"
_ARGV = [_BIN, "--session", "fleet"]
_TERMINAL = "/System/Applications/Utilities/Terminal.app"


def _ps(*rows):
    """`ps -axww -o pid=,ppid=,tty=,args=` output for the given (pid, ppid, tty, args) rows."""
    return "".join("%5d %5d %-8s %s\n" % row for row in rows)


_TABLE = _ps(
    (1, 0, "??", "/sbin/launchd"),
    (500, 1, "??", _TERMINAL + "/Contents/MacOS/Terminal"),
    (510, 500, "ttys004", "/usr/bin/login -pfl will /bin/bash -c exec -la bash /x/viewer.command"),
    (520, 510, "ttys004", " ".join(_ARGV)),                   # the attached viewer
    (600, 1, "??", " ".join(_ARGV + ["server"])),             # the server: NOT a viewer
    (700, 1, "??", "/usr/sbin/sshd"),
    (710, 700, "ttys009", " ".join(_ARGV)),                   # a viewer over ssh: not on a screen
)


# ---------------------------------------------------------------- reading the process table

def test_the_process_table_parses_into_pid_ppid_tty_and_the_whole_command_line():
    procs = viewer.parse_ps(_TABLE + "garbage line\n\n")
    by_pid = {p.pid: p for p in procs}
    assert by_pid[520] == viewer.Proc(520, 510, "ttys004", " ".join(_ARGV))
    assert by_pid[500].args == _TERMINAL + "/Contents/MacOS/Terminal"
    assert 0 not in by_pid and len(procs) == 7, "an unparseable line is skipped, never guessed"


def test_a_command_line_with_spaces_survives_parsing():
    procs = viewer.parse_ps(_ps((42, 1, "??", "/Applications/VMware Fusion.app/Contents/MacOS/VMware Fusion")))
    assert procs[0].args == "/Applications/VMware Fusion.app/Contents/MacOS/VMware Fusion"


def test_the_app_a_process_runs_in_is_its_nearest_app_bundle_ancestor():
    procs = viewer.parse_ps(_TABLE)
    assert viewer.app_of(procs, 520) == _TERMINAL
    assert viewer.app_of(procs, 710) is None, "sshd is not an app: that viewer is on no screen here"


def test_the_ancestry_walk_survives_a_cycle_and_a_missing_parent():
    procs = viewer.parse_ps(_ps((10, 11, "??", "a"), (11, 10, "??", "b"), (12, 99, "??", "c")))
    assert viewer.app_of(procs, 10) is None
    assert viewer.app_of(procs, 12) is None
    assert viewer.app_of(procs, 404) is None


# ---------------------------------------------------------------- finding an attached viewer

def test_an_attached_viewer_is_found_by_its_exact_command_line_and_the_app_it_runs_in():
    got = viewer.find(viewer.parse_ps(_TABLE), _ARGV)
    assert got == viewer.Attached(pid=520, tty="ttys004", app=_TERMINAL)


def test_the_server_is_never_mistaken_for_a_viewer():
    # Same binary, same session, one more word. A prefix match would find the server and report a
    # window that does not exist; the match is exact.
    table = _ps((1, 0, "??", "/sbin/launchd"), (600, 1, "??", " ".join(_ARGV + ["server"])))
    assert viewer.find(viewer.parse_ps(table), _ARGV) is None


def test_a_viewer_in_no_app_is_not_a_window_on_this_screen():
    # A client attached over ssh, or nested in a session-host pane, is attached — but not to any
    # window the owner can be shown. Counting it would answer "switched" with nothing in front.
    table = _ps((1, 0, "??", "/sbin/launchd"), (700, 1, "??", "/usr/sbin/sshd"),
                (710, 700, "ttys009", " ".join(_ARGV)))
    assert viewer.find(viewer.parse_ps(table), _ARGV) is None


def test_another_sessions_viewer_or_another_binary_is_not_this_fleets_viewer():
    table = _ps((1, 0, "??", "/sbin/launchd"),
                (500, 1, "??", _TERMINAL + "/Contents/MacOS/Terminal"),
                (520, 500, "ttys004", "%s --session other" % _BIN),
                (521, 500, "ttys005", "/usr/local/bin/host --session fleet"))
    assert viewer.find(viewer.parse_ps(table), _ARGV) is None


def test_the_newest_of_several_attached_viewers_is_the_one_raised():
    table = _TABLE + _ps((530, 500, "ttys006", " ".join(_ARGV)))
    assert viewer.find(viewer.parse_ps(table), _ARGV).pid == 530


def test_pids_already_present_can_be_excluded_so_only_a_new_viewer_counts():
    assert viewer.find(viewer.parse_ps(_TABLE), _ARGV, exclude={520}) is None


# ---------------------------------------------------------------- the real probe, neutralized

def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_console_is_ours_only_when_this_uid_owns_it(tmp_path):
    mine = tmp_path / "console"
    mine.write_text("")
    assert viewer.Screen(env={"SL_CONSOLE": str(mine)}).console_is_ours() is True
    assert viewer.Screen(env={"SL_CONSOLE": str(tmp_path / "absent")}).console_is_ours() is False


def test_the_probe_reads_the_process_table_through_the_configured_ps(tmp_path):
    ps = _script(tmp_path / "ps", "cat <<'EOF'\n%sEOF\n" % _TABLE)
    procs = viewer.Screen(env={"SL_PS": str(ps)}).processes()
    assert viewer.find(procs, _ARGV).pid == 520


def test_a_process_table_that_cannot_be_read_is_unknown_not_empty(tmp_path):
    # "No viewer is attached" and "we could not look" must not be the same answer: the first opens
    # a window, and reading the second as the first is how a tap opens a second one.
    assert viewer.Screen(env={"SL_PS": str(tmp_path / "no-ps")}).processes() is None
    failing = _script(tmp_path / "ps-fails", "echo boom >&2\nexit 1\n")
    assert viewer.Screen(env={"SL_PS": str(failing)}).processes() is None


def test_the_opener_is_the_machines_normal_handler_given_only_the_script_path(tmp_path):
    log = tmp_path / "opened"
    opener = _script(tmp_path / "open", 'printf "%%s\\n" "$@" > "%s"\n' % log)
    rc, err = viewer.Screen(env={"SL_OPEN": str(opener)}).open("/x/viewer.command")
    assert (rc, err) == (0, "")
    assert log.read_text().splitlines() == ["/x/viewer.command"], (
        "no -a, no app named: whatever handler this machine has for the script opens it")


def test_an_opener_that_fails_reports_its_own_words(tmp_path):
    opener = _script(tmp_path / "open", 'echo "LSOpenURLsWithRole() failed with error -10810" >&2\nexit 1\n')
    rc, err = viewer.Screen(env={"SL_OPEN": str(opener)}).open("/x/viewer.command")
    assert rc == 1 and "-10810" in err
    rc, err = viewer.Screen(env={"SL_OPEN": str(tmp_path / "no-open")}).open("/x/viewer.command")
    assert rc == 127 and err


def test_activating_the_viewers_app_names_the_app_and_nothing_else(tmp_path):
    log = tmp_path / "activated"
    opener = _script(tmp_path / "open", 'printf "%%s\\n" "$@" > "%s"\n' % log)
    assert viewer.Screen(env={"SL_OPEN": str(opener)}).activate(_TERMINAL) == (0, "")
    assert log.read_text().splitlines() == ["-a", _TERMINAL]


def test_raising_writes_only_the_window_sequences_to_the_viewers_own_terminal(tmp_path):
    # The viewer's terminal is told to de-iconify and raise ITS window (xterm window operations,
    # CSI 1 t and CSI 5 t). These are OUTPUT to a terminal, not input to any process: nothing is
    # typed into the viewer, the fleet, or any pane.
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttys004").write_text("")
    ok, err = viewer.Screen(env={"SL_TTY_DIR": str(dev)}).raise_tty("ttys004")
    assert (ok, err) == (True, "")
    assert (dev / "ttys004").read_bytes() == viewer.RAISE_SEQUENCE == b"\x1b[1t\x1b[5t"


@pytest.mark.parametrize("tty", ["??", "", None, "../ttys004", "ttys004/../../etc", "console"])
def test_raising_refuses_anything_that_is_not_a_pseudo_terminal_name(tmp_path, tty):
    dev = tmp_path / "dev"
    dev.mkdir()
    ok, err = viewer.Screen(env={"SL_TTY_DIR": str(dev)}).raise_tty(tty)
    assert ok is False and err
    assert list(dev.iterdir()) == [], "nothing was written anywhere"


def test_raising_a_terminal_that_is_gone_is_an_answer_not_a_traceback(tmp_path):
    ok, err = viewer.Screen(env={"SL_TTY_DIR": str(tmp_path)}).raise_tty("ttys004")
    assert ok is False and err


def test_no_test_reaches_the_real_screen_by_default():
    """The ratchet (CLAUDE.md, the 2026-07-03 toast-spam rule): a test that forgot to inject a
    screen must not open a real window, raise a real one, or read the owner's process table. The
    conftest points every seam at a path that cannot exist, and this pins that it still does."""
    for var in ("SL_PS", "SL_OPEN", "SL_CONSOLE", "SL_TTY_DIR"):
        assert os.environ.get(var, "").startswith("/nonexistent/"), var
    real = viewer.Screen()
    assert real.console_is_ours() is False
    assert real.processes() is None
    assert real.open("/x/viewer.command")[0] == 127
    assert real.activate(_TERMINAL)[0] == 127
    assert real.raise_tty("ttys004")[0] is False
