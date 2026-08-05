"""Issue #334 — the recorded session handle has ONE writer and ONE reader vocabulary.

`state/panes/<id>` and `state/panes/<id>.ws` used to hold cmux surface/workspace UUIDs and were
read by four call sites that each spelled the path themselves. #308 moved the SPAWN onto the
session-host wrapper, so those files now hold the HOST's pane and workspace ids — and the readers
that were still handing them to cmux could not see the change, because nothing in the codebase
said what a recorded handle MEANS.

This module is that statement, and these tests are the pin: what `lib/launch.py` writes is exactly
what every reader reads back, through one vocabulary, into the one type the doorway accepts.
"""
import os

import launch
import panes
import session_host


def _state(tmp_path):
    d = tmp_path / "state" / "panes"
    d.mkdir(parents=True)
    return str(tmp_path / "state")


def test_a_recorded_handle_round_trips_through_the_one_vocabulary(tmp_path):
    state = _state(tmp_path)
    panes.record(state, "i7", session_host.Session(name="i7", workspace="w3", pane="w3:p1"))
    handle = panes.read(state, "i7")
    assert handle.pane == "w3:p1" and handle.workspace == "w3"


def test_what_the_launcher_writes_is_what_the_reader_reads(tmp_path):
    """The pin the #308 landmine needed. `lib/launch.py` is the ONE writer; if it ever records a
    handle some other way, this goes red rather than the nudge path silently addressing nothing."""
    state = _state(tmp_path)
    spawned = session_host.Session(name="i9", workspace="ws-abc", pane="ws-abc:p1", tab="ws-abc:t1",
                                   shell_pid=4242)
    launch._record_delivery(_spec(tmp_path), "i9", spawned, debugger=True, resume=False)
    handle = panes.read(state, "i9")
    assert (handle.pane, handle.workspace) == (spawned.pane, spawned.workspace)


def test_the_handle_becomes_the_session_the_doorway_accepts(tmp_path):
    # The doorway's teardown verbs refuse anything that is not a Session carrying a workspace, so
    # the vocabulary has to produce that type rather than two loose strings.
    state = _state(tmp_path)
    panes.record(state, "i7", session_host.Session(name="i7", workspace="w3", pane="w3:p1"))
    session = panes.read(state, "i7").as_session("i7")
    assert isinstance(session, session_host.Session)
    assert (session.name, session.workspace, session.pane, session.owned) == ("i7", "w3", "w3:p1",
                                                                              True)


def test_a_handle_with_no_workspace_cannot_become_a_session(tmp_path):
    # Without a workspace the doorway has nothing to close, and closing "whatever is at that pane"
    # is how a stale handle ends someone else's window. None, never a half-built Session.
    state = _state(tmp_path)
    panes.record(state, "i7", session_host.Session(name="i7", workspace="", pane="w3:p1"))
    assert panes.read(state, "i7").as_session("i7") is None


def test_an_unrecorded_lane_reads_as_an_empty_handle(tmp_path):
    state = _state(tmp_path)
    handle = panes.read(state, "i404")
    assert not handle and handle.pane == "" and handle.workspace == ""


def test_forget_clears_both_halves_together(tmp_path):
    # D9: no marker outlives its session, and the pane must never survive its workspace (a lone
    # pane id is exactly the un-closable handle _close_pane refuses on).
    state = _state(tmp_path)
    panes.record(state, "i7", session_host.Session(name="i7", workspace="w3", pane="w3:p1"))
    panes.forget(state, "i7")
    assert not panes.read(state, "i7")
    assert not os.path.exists(os.path.join(state, "panes", "i7"))
    assert not os.path.exists(os.path.join(state, "panes", "i7.ws"))


def test_recorded_ids_names_lanes_not_sidecars(tmp_path):
    state = _state(tmp_path)
    for iid in ("i7", "i12"):
        panes.record(state, iid, session_host.Session(name=iid, workspace="w", pane="w:p1"))
    assert panes.recorded_ids(state) == {"i7", "i12"}


def test_recorded_ids_survives_a_missing_directory(tmp_path):
    assert panes.recorded_ids(str(tmp_path / "nope")) == set()


def _spec(tmp_path):
    return launch.Spec(id="i9", run_root=str(tmp_path), repo=str(tmp_path))
