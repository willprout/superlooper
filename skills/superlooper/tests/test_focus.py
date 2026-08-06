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


def test_every_outcome_has_its_own_exit_code_and_only_focused_is_zero():
    codes = [focus_lib.EXIT_CODES[o] for o in focus_lib.OUTCOMES]
    assert len(set(codes)) == len(codes), "a shell caller must be able to tell the four apart"
    assert focus_lib.EXIT_CODES[focus_lib.FOCUSED] == 0
    assert all(focus_lib.EXIT_CODES[o] != 0 for o in focus_lib.OUTCOMES if o != focus_lib.FOCUSED)


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


# --------------------------------------------------------------------- the CLI

def _fake_host_env(rig, tmp_path):
    hostdir = tmp_path / "fakehost"
    hostdir.mkdir(exist_ok=True)
    return {"SL_HERDR": str(_FAKE_HOST), "FAKE_HOST_DIR": str(hostdir), "HOST_MODE": "hollow"}, \
        hostdir


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
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", env_over=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "w1" in r.stdout
    assert [json.loads(x)["workspace"] for x in
            (hostdir / "focused.jsonl").read_text().splitlines()] == ["w1"]


def test_cli_focus_is_the_only_thing_the_verb_asks_the_host_for(rig, tmp_path):
    """The DoD's "its whole effect is a focus", end to end: every argv the host binary received,
    across a real CLI run, is one `workspace focus`."""
    env, hostdir = _fake_host_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    assert cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339",
               env_over=env).returncode == 0
    calls = [json.loads(x) for x in (hostdir / "calls.jsonl").read_text().splitlines()]
    assert calls == [["workspace", "focus", "w1"]], (
        "focus must neither spawn, send, exit nor kill: %s" % (calls,))


def test_cli_json_carries_the_outcome_in_a_machine_readable_field(rig, tmp_path):
    env, hostdir = _fake_host_env(rig, tmp_path)
    _record(_home(rig, "o/r"), "i339", "w1:p1", "w1")
    _issue(hostdir, "w1")

    r = cli(rig, "focus-session", "--repo", str(rig.repo), "--id", "i339", "--json", env_over=env)
    got = json.loads(r.stdout)
    assert got["outcome"] == "focused"
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
