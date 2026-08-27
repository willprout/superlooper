"""`superlooper debug` (issue #144): the OWNER-TAP debugger launch — the attended sibling of
`superlooper watchdog`'s unattended, episode-gated fallback.

Invoked as a real subprocess, with everything external injected: the state base via SL_HOME, a
FAKE launch script via SL_LAUNCH_SESSION (no test reaches a real cmux / a real Claude), fake-gh
via SL_GH. The verb exists so a local ops UI (the command center's Deploy Fixer button, issue
#141) can ask for a debugger session without re-implementing five engine internals it has no
contract over — the id namespace, the brief path, the shim handshake, the pane anchor, the
worker lock.

The two properties that could NOT be had from outside the engine, and that these tests pin:

1. **The id allocator advances.** A tapped launch takes its id from `state/watchdog.json` ▸
   `next_debugger` and writes the counter FORWARD, so a later watchdog launch can never reuse it
   and overwrite the brief.
2. **Single-flight under the watchdog's OWN lock.** The whole check-allocate-launch runs while
   holding `state/watchdog.lock` — the same lock `cmd_watchdog` holds across its entire check,
   including its launch subprocess. So a tap and a watchdog check can never both pass the
   "is a debugger already running?" test and launch two sessions onto one patient.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import focus
import journal

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

_FAKE_LAUNCH = """#!/bin/bash
{ printf 'ARGS %s\\n' "$*"
  printf 'PANE %s\\n' "${SL_PANE:-}"
  printf 'ROOT %s\\n' "${SL_RUN_ROOT:-}"
  printf 'MODEL %s\\n' "${SL_MODEL:-}"
  printf 'AGENT %s\\n' "${SL_AGENT:-}"
  printf 'ATTENDED %s\\n' "${SL_ATTENDED:-}"
  printf 'VERIFY %s\\n' "${SL_LAUNCH_VERIFY_SECONDS:-unset}"
  printf 'RESUME_ID %s\\n' "${SL_RESUME_SESSION_ID-unset}"
  # What the watchdog counter looked like AS THE LAUNCHER SAW IT: proof the id was durably
  # advanced BEFORE anything could launch, not merely after the shim returned.
  printf 'STATE_AT_LAUNCH %s\\n' "$(cat "${SL_RUN_ROOT:-}/state/watchdog.json" 2>/dev/null | tr -d '\\n ')"
} >> "$STUB_LOG"
# What the REAL launcher does at delivery confirmation and nothing else does: record the session
# handle under state/panes/ (lib/launch.py -> lib/panes.record). Opt-in via $STUB_WS so every test
# that predates issue #459 sees the byte-identical state home it always did; the tests that care
# about the window say which workspace the launcher came back with. Only on a launch that
# CONFIRMS — a shim that failed recorded no handle, because there is no session to record.
if [ -n "${STUB_WS:-}" ] && [ "${STUB_RC:-0}" = "0" ]; then
  mkdir -p "${SL_RUN_ROOT:-}/state/panes"
  printf '%s' "$STUB_WS"     > "${SL_RUN_ROOT:-}/state/panes/$3.ws"
  printf '%s:p1' "$STUB_WS"  > "${SL_RUN_ROOT:-}/state/panes/$3"
fi
echo "${STUB_STDERR:-}" >&2
exit "${STUB_RC:-0}"
"""


class _Rig:
    def __init__(self, tmp_path, cfg_extra=None):
        self.tmp = tmp_path
        fixdir = tmp_path / "gh"
        shutil.copytree(_FIXTURES, fixdir)
        self.repo = tmp_path / "repo"
        (self.repo / ".superlooper").mkdir(parents=True)
        cfg = {"version": 1, "repo": "o/r"}
        cfg.update(cfg_extra or {})
        (self.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))
        self.home = tmp_path / "slhome" / "o__r"
        (self.home / "state").mkdir(parents=True)
        (tmp_path / "userhome").mkdir()
        self.stub_log = tmp_path / "launch-calls.log"
        launch = tmp_path / "fake-launch-session.sh"
        launch.write_text(_FAKE_LAUNCH)
        launch.chmod(launch.stat().st_mode | stat.S_IXUSR)
        self.env = {**os.environ,
                    "HOME": str(tmp_path / "userhome"),
                    "SL_HOME": str(tmp_path / "slhome"),
                    "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
                    "SL_CMUX": "/nonexistent/superlooper-test-cmux",
                    "SL_LAUNCH_SESSION": str(launch),
                    "STUB_LOG": str(self.stub_log)}
        # this test process may itself run inside a superlooper worker: its ambient pane must
        # never leak into the subject's pane resolution.
        self.env.pop("SL_PANE", None)
        self.env.pop("GH_FAIL", None)

    # --- state-home seeding ---
    def anchor(self, pane="PANE-UUID-1"):
        (self.home / "state" / "runner.anchor.json").write_text(
            json.dumps({"pane": pane, "workspace": "", "window": "", "pid": 1}))

    def wstate(self, **fields):
        st = {"episode": None, "no_progress_since": {}, "next_debugger": 1}
        st.update(fields)
        (self.home / "state" / "watchdog.json").write_text(json.dumps(st))

    def read_wstate(self):
        return json.loads((self.home / "state" / "watchdog.json").read_text())

    def brief(self, sid):
        return (self.home / "briefs" / ("%s.md" % sid)).read_text()

    def launch_calls(self):
        if not self.stub_log.exists():
            return []
        blocks, cur = [], {}
        for line in self.stub_log.read_text().splitlines():
            k, _, v = line.partition(" ")
            if k == "ARGS" and cur:
                blocks.append(cur)
                cur = {}
            cur[k] = v
        if cur:
            blocks.append(cur)
        return blocks

    def djournal(self):
        return [r for r in journal.read(str(self.home)) if r.get("act") == "debug_launch"]


@pytest.fixture
def rig(tmp_path):
    return _Rig(tmp_path)


def run(rig, *args, inp=None, env_over=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env, timeout=60, input=inp)


def body(r):
    return json.loads(r.stdout)


# --------------------------- the happy path ---------------------------

def test_debug_launches_one_session_through_the_shim(rig):
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json",
            "--note", "the board is showing a frozen lane", "--operator", "William",
            "--source", "command-center")
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["ok"] is True and b["verb"] == "debug" and b["id"] == "d4"

    call = rig.launch_calls()[0]
    assert call["ARGS"] == "--cwd %s d4" % rig.repo      # the engine's own --cwd invocation
    assert call["PANE"] == "PANE-UUID-1"                 # resolved from the runner's anchor
    assert call["ROOT"] == str(rig.home)
    assert call["VERIFY"] == "30"                        # PINNED, never inherited
    # (#298) PINNED EMPTY for the same reason: an ambient SL_RESUME_SESSION_ID would silently turn
    # this FRESH debugger launch into a resume of some unrelated conversation.
    assert call["RESUME_ID"] == ""
    assert call["AGENT"] == "claude"
    assert call["MODEL"] == "opus[1m]"
    # #185: the owner tap is the one loop-launched session with a PERSON at the keyboard, and its
    # brief says so. SL_ATTENDED is how the PreToolUse deny learns that, so its AskUserQuestion
    # duty (whose whole premise is "nobody is here") stands down instead of contradicting the brief.
    assert call["ATTENDED"] == "1"


def test_the_launched_id_is_durably_advanced_before_the_shim_runs(rig):
    # Gap 1 of the issue: the dashboard could not advance this counter from outside, so a later
    # watchdog launch could reuse the id and overwrite the brief. The counter must be on DISK
    # already when the launcher runs — a debug verb killed mid-launch must still have burned it.
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert rig.read_wstate()["next_debugger"] == 5
    seen = json.loads(rig.launch_calls()[0]["STATE_AT_LAUNCH"])
    assert seen["next_debugger"] == 5


def test_allocation_never_reuses_an_id_an_existing_brief_already_took(rig):
    # Legacy drift: briefs written before this verb existed can sit ABOVE the counter (that is
    # exactly what the dashboard's out-of-band allocator produced). Allocate past them too, so no
    # tap can ever clobber a prior session's brief.
    rig.anchor()
    rig.wstate(next_debugger=2)
    (rig.home / "briefs").mkdir(parents=True)
    (rig.home / "briefs" / "d7.md").write_text("an older debugger brief")
    (rig.home / "state" / "worker.d9.lock").write_text("999999")   # a dead session's lock
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert body(r)["id"] == "d10"
    assert rig.read_wstate()["next_debugger"] == 11
    assert rig.brief("d7") == "an older debugger brief"            # untouched


def test_the_state_home_preserves_the_rest_of_the_watchdog_document(rig):
    # Allocating an id must not amputate the watchdog's episode state machine — the anti-storm
    # rails live in the SAME document, and this verb is a co-author of exactly one field.
    rig.anchor()
    ep = {"signals": ["heartbeat_stale"], "opened_at": time.time() - 60, "detail": "seeded",
          "launched_at": None, "launch_id": None, "launch_attempts": 0,
          "launch_failure_notified": False}
    rig.wstate(next_debugger=3, episode=ep)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    after = rig.read_wstate()
    assert after["next_debugger"] == 4
    assert after["episode"]["signals"] == ["heartbeat_stale"]
    assert after["episode"]["launch_attempts"] == 0


# --------------------------- the brief ---------------------------

def test_the_brief_carries_the_note_verbatim_and_asserts_the_human_present_contract(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    note = "lane 3 has been on the same commit for an hour — check the gate"
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", note,
            "--operator", "William")
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert note in text                                   # VERBATIM — never summarized
    assert "sl-debugger" in text
    assert "human-present" in text
    assert "William" in text
    # The whole point of a separate verb: this is NOT the watchdog's unattended invocation.
    assert "UNATTENDED" not in text


def test_the_callers_context_is_piped_in_and_lands_verbatim(rig):
    # The caller composes what IT knows (the command center knows what the board is showing) and
    # hands it over; the engine frames it. Piped on stdin so a large context can never hit an
    # argv limit.
    rig.anchor()
    rig.wstate(next_debugger=1)
    ctx = "## What the dashboard is showing\n\n- SL-12 — parked\n- SL-19 — session frozen"
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "look at these",
            "--context-file", "-", inp=ctx)
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert ctx in text


def test_a_missing_note_and_context_still_produce_an_honest_brief(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert "no note" in text.lower()
    assert str(rig.repo) in text                          # the patient is always named
    assert str(rig.home) in text


def test_a_giant_note_is_bounded_rather_than_drowning_the_brief(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "z" * 20000)
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert "truncated" in text
    assert len(text) < 12000


# --------------------------- single flight ---------------------------

def test_debug_refuses_when_a_debugger_session_is_already_live(rig):
    # Never two debuggers on one patient — the same worker.d<N>.lock the watchdog reads.
    rig.anchor()
    rig.wstate(next_debugger=4)
    (rig.home / "state" / "worker.d2.lock").write_text(str(os.getpid()))
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode != 0
    b = body(r)
    assert b["ok"] is False and b["live"] is True and b["live_id"] == "d2"
    assert rig.launch_calls() == []                       # nothing launched
    assert rig.read_wstate()["next_debugger"] == 4        # no id burned
    assert not (rig.home / "briefs").exists()             # no brief left behind


def test_debug_refuses_while_a_watchdog_check_holds_the_lock(rig):
    # Gap 2 of the issue: the watchdog holds state/watchdog.lock across its ENTIRE check —
    # including its launch subprocess — so taking the same lock is what makes a tap and a
    # watchdog check mutually exclusive rather than merely both polite.
    rig.anchor()
    rig.wstate(next_debugger=4)
    (rig.home / "state" / "watchdog.lock").write_text(str(os.getpid()))   # a LIVE holder
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode != 0
    b = body(r)
    assert b["ok"] is False
    assert "watchdog" in b["error"]
    assert rig.launch_calls() == []
    assert rig.read_wstate()["next_debugger"] == 4


def test_a_successful_launch_releases_the_watchdog_lock(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (rig.home / "state" / "watchdog.lock").exists()


# --------------------------- refusals that change nothing ---------------------------

def test_the_owner_tap_needs_no_cmux_pane_to_launch_into(rig):
    """Issue #308 removed this gate, and the owner tap is the case it mattered most for.

    The launcher creates a workspace against the session host's server — there is no anchor to
    find and nothing to place. Under a login-item runner (#306) no pane resolves at all, and the
    dashboard shells this verb from its own non-cmux process, so the gate would have made Deploy
    Fixer's Debug button permanently dead on exactly the machine the migration is for."""
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stderr
    assert rig.launch_calls(), "the repair must launch with no pane to launch into"
    assert rig.read_wstate()["next_debugger"] == 5


def test_debug_reports_a_failed_launch_honestly(rig):
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x",
            env_over={"STUB_RC": "3", "STUB_STDERR": "the tab never took the prompt"})
    assert r.returncode != 0
    b = body(r)
    assert b["ok"] is False and b["id"] == "d4"
    assert "the tab never took the prompt" in b["error"]
    # The attempt happened: the id is burned (never re-handed out) and the record is honest.
    assert rig.read_wstate()["next_debugger"] == 5
    assert [(x["outcome"]) for x in rig.djournal()] == ["launch_failed"]
    # ...and it carries the launcher's EXIT CODE beside the prose (issue #457). This verb runs in
    # its own process, so this record is the whole of what the runner ever hears about a refused
    # tap, and it classifies it through lib/evidence — which reads the stderr first (relayed here
    # verbatim) and falls back to the rc for the refusals that name themselves in neither.
    assert rig.djournal()[0]["rc"] == 3


def test_debug_removes_the_brief_when_the_shim_never_ran(rig):
    # An unrunnable shim reached nobody — leaving its brief behind would read to a later reader
    # as a session that existed.
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x",
            env_over={"SL_LAUNCH_SESSION": str(rig.tmp / "nope.sh")})
    assert r.returncode != 0
    assert body(r)["ok"] is False
    assert not (rig.home / "briefs" / "d4.md").exists()


# --------------------------- the preflight ---------------------------

def test_debug_check_reports_liveness_and_writes_nothing(rig):
    rig.anchor()
    rig.wstate(next_debugger=4)
    (rig.home / "state" / "worker.d2.lock").write_text(str(os.getpid()))
    r = run(rig, "debug", "--repo", str(rig.repo), "--check", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["ok"] is True and b["verb"] == "debug-check"
    assert b["live"] is True and b["live_id"] == "d2"
    assert rig.launch_calls() == []
    assert rig.read_wstate()["next_debugger"] == 4
    assert not (rig.home / "briefs").exists()


def test_debug_check_on_a_quiet_state_home_reports_no_debugger(rig):
    rig.anchor()
    r = run(rig, "debug", "--repo", str(rig.repo), "--check", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["live"] is False and b["live_id"] is None
    assert not (rig.home / "state" / "watchdog.json").exists()   # a read-only preflight


def test_a_stale_worker_lock_does_not_block_a_tap(rig):
    # A dead pid in the lock is a corpse, not a session — the engine's own reclaim rule.
    rig.anchor()
    rig.wstate(next_debugger=1)
    (rig.home / "state" / "worker.d1.lock").write_text("999999")
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert body(r)["id"] == "d2"                          # past the id that lock already took


# --------------------------- the journal ---------------------------

def test_one_journal_line_records_the_tap(rig):
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "the gate is stuck",
            "--operator", "William", "--source", "command-center")
    assert r.returncode == 0, r.stdout + r.stderr
    recs = rig.djournal()
    assert len(recs) == 1
    rec = recs[0]
    assert rec["outcome"] == "launched" and rec["id"] == "d4"
    assert rec["operator"] == "William" and rec["source"] == "command-center"
    # A DISTINCT act from `watchdog`: the morning report's "Unattended debugger" section must
    # never claim an owner-tapped session was unattended.
    assert rec["act"] == "debug_launch"


def test_the_human_line_is_readable_without_json(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "d1" in r.stdout


# --------------------------- fresh-agent review round 1 ---------------------------

def test_a_note_that_looks_like_a_placeholder_is_never_expanded(rig):
    # The ONE promise this brief makes about the note is that it is VERBATIM. A sequential
    # str.replace renderer breaks that: it substitutes {note} first, then goes looking for
    # {context} — and finds the one the note itself contained, rewriting the owner's words into
    # the board readout. One pass over the ORIGINAL template can never do that.
    rig.anchor()
    rig.wstate(next_debugger=1)
    note = "the {context} and {repo_path} placeholders render wrong somewhere"
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", note,
            "--context-file", "-", inp="BOARD-READOUT-SENTINEL")
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert note in text, "the note must survive rendering byte-for-byte"
    assert text.count("BOARD-READOUT-SENTINEL") == 1, "the context must land ONCE, in its own slot"


def test_a_context_that_looks_like_a_placeholder_is_never_expanded(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "NOTE-SENTINEL",
            "--context-file", "-", inp="the board mentions {note} and {operator}")
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert "the board mentions {note} and {operator}" in text
    assert text.count("NOTE-SENTINEL") == 1


def test_a_journal_failure_never_turns_a_real_launch_into_a_reported_failure(rig):
    # The session EXISTS — the shim verified delivery. A bookkeeping write that fails afterwards
    # must not make the caller believe nothing launched: that belief is exactly what invites a
    # second tap, and a second debugger on one patient is the thing this whole verb prevents.
    rig.anchor()
    rig.wstate(next_debugger=4)
    (rig.home / "journal.jsonl").mkdir(parents=True)      # an unwritable journal path
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["ok"] is True and b["id"] == "d4"
    assert "Traceback" not in r.stderr
    assert len(rig.launch_calls()) == 1


def test_an_unwritable_brief_dir_is_a_json_refusal_not_a_traceback(rig):
    # Every failure this verb can hit must arrive at the caller as its --json contract. A UI that
    # gets a traceback on stderr and nothing on stdout cannot tell a refusal from a crash.
    rig.anchor()
    rig.wstate(next_debugger=4)
    (rig.home / "briefs").write_text("not a directory")   # mkdir will fail here
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode != 0
    b = body(r)                                            # parses ⇒ the contract held
    assert b["ok"] is False and b["error"]
    assert "Traceback" not in r.stderr
    assert rig.launch_calls() == []


def test_a_giant_piped_context_is_bounded_rather_than_drowning_the_brief(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x",
            "--context-file", "-", inp="c" * 200_000)
    assert r.returncode == 0, r.stdout + r.stderr
    text = rig.brief("d1")
    assert "truncated" in text
    assert len(text) < 12000


# --------------------------- fresh-agent review round 2 ---------------------------

def test_an_unwritable_state_dir_is_a_json_refusal_not_a_traceback(rig):
    # The lock is acquired by hardlinking a temp file INTO state/ — so a state dir that exists but
    # cannot be written raises out of tempfile.mkstemp, before a single byte of the --json contract
    # has been printed. Every failure this verb can hit must arrive as its contract.
    rig.anchor()
    rig.wstate(next_debugger=4)
    state = rig.home / "state"
    mode = state.stat().st_mode
    state.chmod(0o555)                                    # r-x: readable, not writable
    try:
        r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    finally:
        state.chmod(mode)                                 # restore so tmp_path cleanup works
    assert r.returncode != 0
    b = body(r)                                           # parses ⇒ the contract held
    assert b["ok"] is False and b["error"]
    assert "Traceback" not in r.stderr
    assert rig.launch_calls() == []


# --------------------------- the tap surfaces its terminal (issue #459) ---------------------------
#
# The owner tapped Deploy Fixer, so he is AT the dashboard watching for the fixer to appear. Once
# the spawn confirms, the d<N> session's own window comes to the front through the engine's
# `focus-session` mechanism (issue #339's doorway verb) so he can read it and type into it.
#
# Everything here is driven against `fakes/fake-sessionhost`, which speaks the real envelope and
# REFUSES a workspace it never issued — so "the window was focused" is a fact about the host's own
# log, never a mock's shrug. The `focused.jsonl`/`calls.jsonl` pair is the same evidence
# tests/test_focus.py rests its own CLI cases on.
#
# The load-bearing half is the NEGATIVE: focus is garnish. A launch that confirmed is `ok` whatever
# the window did, because a fixer that launched but did not surface must never read as a fixer that
# did not launch.

_FAKE_HOST = Path(__file__).resolve().parent / "fakes" / "fake-sessionhost"


def host_env(rig, ws="w1", issued=True):
    """Point the engine's doorway at the fake host, and (by default) mark `ws` LIVE on it — what a
    real `workspace create` would have left behind for the session the launcher just recorded."""
    hostdir = rig.tmp / "fakehost"
    hostdir.mkdir(exist_ok=True)
    if issued:
        (hostdir / ("live.%s" % ws)).write_text("")
    rig.hostdir = hostdir
    return {"SL_HERDR": str(_FAKE_HOST), "FAKE_HOST_DIR": str(hostdir), "HOST_MODE": "hollow",
            "STUB_WS": ws}


def host_calls(rig):
    path = rig.hostdir / "calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def focused(rig):
    path = rig.hostdir / "focused.jsonl"
    return [json.loads(x)["workspace"] for x in path.read_text().splitlines()] \
        if path.exists() else []


def test_an_owner_tap_brings_the_new_fixers_window_to_the_front(rig):
    rig.anchor()
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "the gate is stuck",
            "--operator", "William", "--source", "command-center",
            env_over=host_env(rig, ws="w1"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert body(r)["id"] == "d4"
    # The window the LAUNCHER recorded for d4 — not a name, not a guess.
    assert focused(rig) == ["w1"]
    assert body(r)["focus"] == "focused"


def test_the_tap_asks_the_host_for_a_focus_and_nothing_else(rig):
    """The whole added effect is one `workspace focus`. The tap must not grow a second window verb
    on the way — no close, no kill, no send — and the fake answers every one of them happily, so a
    stray call would succeed rather than raise and could not hide."""
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x",
            env_over=host_env(rig, ws="w1"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert host_calls(rig) == [["workspace", "focus", "w1"]], host_calls(rig)


def test_the_focus_outcome_is_journaled_beside_the_launch(rig):
    rig.anchor()
    rig.wstate(next_debugger=1)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x",
            env_over=host_env(rig, ws="w1"))
    assert r.returncode == 0, r.stdout + r.stderr
    recs = rig.djournal()
    # ONE record for one tap, still `debug_launch` and still `launched`: the window is a field on
    # the launch, never a second act. (The command center renders journal acts it knows by name;
    # a new one would print as an unknown row beside the launch it belongs to.)
    assert len(recs) == 1
    assert recs[0]["act"] == "debug_launch" and recs[0]["outcome"] == "launched"
    assert recs[0]["focus"] == "focused"


def test_a_window_the_host_will_not_raise_is_journaled_and_the_launch_still_stands(rig):
    """The DoD's third line. The session EXISTS — the shim verified delivery — and the host then
    refuses the workspace the launcher recorded. That is a fact about a window, and a fixer that
    launched but did not surface must never read as a fixer that did not launch."""
    rig.anchor()
    rig.wstate(next_debugger=4)
    env = host_env(rig, ws="w9", issued=False)       # recorded, but never issued on this host
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x", env_over=env)
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["ok"] is True and b["id"] == "d4" and b["live"] is True
    assert b["focus"] == "no_window"
    assert "Traceback" not in r.stderr
    recs = rig.djournal()
    assert [x["outcome"] for x in recs] == ["launched"]
    assert recs[0]["focus"] == "no_window"


def test_a_host_that_cannot_be_reached_at_all_never_fails_the_launch(rig):
    """The other failure road: the doorway cannot reach the host — a wedged server, a binary that
    is not there, a fenced host refusing a tokenless caller. Absence of signal about a window says
    nothing about the session, and it must cost the launch nothing."""
    rig.anchor()
    rig.wstate(next_debugger=4)
    env = {**host_env(rig, ws="w1"), "SL_HERDR": "/nonexistent/superlooper-i459-no-host"}
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x", env_over=env)
    assert r.returncode == 0, r.stdout + r.stderr
    b = body(r)
    assert b["ok"] is True and b["id"] == "d4"
    assert b["focus"] == "host_unreachable"
    assert rig.djournal()[0]["focus"] == "host_unreachable"
    assert "Traceback" not in r.stderr


def test_a_launch_that_never_confirmed_focuses_nothing(rig):
    """AFTER spawn confirmation, and only then. A launch the shim refused has no session and no
    window; reaching for one would be the engine asking the host about a lane that does not exist."""
    rig.anchor()
    rig.wstate(next_debugger=4)
    env = {**host_env(rig, ws="w1"), "STUB_RC": "3", "STUB_STDERR": "no session was confirmed"}
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x", env_over=env)
    assert r.returncode == 1
    assert body(r)["ok"] is False
    assert host_calls(rig) == [], "a failed launch must ask the host for nothing"
    assert [x["outcome"] for x in rig.djournal()] == ["launch_failed"]


def test_a_tap_that_was_refused_outright_focuses_nothing(rig):
    """A refusal is not a launch. `--check`, a live debugger, a held lock: none of them spawned a
    session, so none of them has a window to raise."""
    rig.anchor()
    rig.wstate(next_debugger=4)
    env = host_env(rig, ws="w1")
    (rig.home / "state" / "worker.d2.lock").write_text(str(os.getpid()))   # a LIVE debugger
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x", env_over=env)
    assert r.returncode == 1 and body(r)["live"] is True
    assert host_calls(rig) == []
    assert run(rig, "debug", "--repo", str(rig.repo), "--json", "--check",
               env_over=env).returncode == 0
    assert host_calls(rig) == [], "the read-only preflight must touch no window"


def test_the_human_line_says_what_became_of_the_window(rig):
    """A person typing this in a terminal is told the truth about the window too — a launch that
    landed and a window that did not come forward is one line, not a silent half."""
    rig.anchor()
    rig.wstate(next_debugger=1)
    env = host_env(rig, ws="w9", issued=False)
    r = run(rig, "debug", "--repo", str(rig.repo), "--note", "x", env_over=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "d1" in r.stdout
    assert "launched" in r.stdout, r.stdout
    assert "window" in r.stdout.lower(), r.stdout


# --------------------------- the window never costs the answer ---------------------------
#
# Fresh-agent review, P0. `superlooper debug` runs inside a budget its caller sized for the LAUNCH
# alone: the command center kills it at the same 180s the launcher itself gets. A launch that
# confirmed near the end of that window and was then killed mid-focus would print no JSON at all,
# and the dashboard would render "no session was confirmed" about a live debugger — the exact
# failure this issue forbids, and the one that invites a second tap onto one patient.
#
# The arithmetic that prevents it is pure and lives in `_debug_focus_budget`, so it can be driven
# here without a three-minute launcher (which no suite can afford to run).


@pytest.fixture
def cli_module():
    """The `superlooper` entry point loaded as a MODULE (it guards main() under
    __name__ == '__main__', so importing runs no command) — the same trick test_watchdog_cli uses
    to unit-test its file-lock helpers."""
    loader = importlib.machinery.SourceFileLoader("superlooper_cli", str(CLI))
    spec = importlib.util.spec_from_loader("superlooper_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_a_prompt_launch_leaves_the_window_the_doorways_whole_budget(cli_module):
    assert cli_module._debug_focus_budget(0) == focus.CALL_SECONDS
    assert cli_module._debug_focus_budget(2.5) == focus.CALL_SECONDS


def test_a_launch_that_ate_the_taps_patience_leaves_the_window_nothing(cli_module):
    # The window is garnish and the answer is not: what is left over goes to the window, and when
    # there is nothing left the window gets nothing. Nothing here shortens the LAUNCH's own timeout
    # to make room — that would turn a slow-but-real launch into a killed one, the same forbidden
    # failure wearing the other hat.
    budget = cli_module.WATCHDOG_LAUNCH_TIMEOUT
    assert cli_module._debug_focus_budget(budget) <= 0
    assert cli_module._debug_focus_budget(budget - 1) <= 0
    # ...and the last seconds before that shrink rather than jump: the doorway is asked for only
    # what remains.
    tight = cli_module._debug_focus_budget(budget - cli_module._DEBUG_FOCUS_RESERVE - 2)
    assert 0 < tight < focus.CALL_SECONDS


def test_no_budget_means_the_host_is_never_asked_and_the_launch_still_reports(cli_module, rig):
    outcome, detail = cli_module._debug_focus(str(rig.home), "d4", 0)
    assert outcome == cli_module._DEBUG_FOCUS_NOT_ASKED
    assert "the session launched and is unaffected" in detail
    assert "focus-session" in detail, "tell the owner how to raise the window himself"
    # NOT one of lib/focus's four: those are answers ABOUT a window, and this is the verb saying it
    # never asked for one. A fifth outcome word in lib/focus would reach the `focus-session` exit
    # codes and the dashboard's rendering, neither of which this is about.
    assert outcome not in focus.OUTCOMES


def test_the_budget_is_what_the_doorway_is_actually_given(cli_module, rig, monkeypatch):
    """The arithmetic above is only worth anything if it reaches the host call."""
    seen = {}

    def fake_focus_lane(home, iid, call_seconds=None, **kw):
        seen["call_seconds"] = call_seconds
        return focus.Result(focus.FOCUSED, iid, workspace="w1", detail="front")

    monkeypatch.setattr(cli_module.focus_lib, "focus_lane", fake_focus_lane)
    assert cli_module._debug_focus(str(rig.home), "d4", 3.5) == (focus.FOCUSED, "front")
    assert seen["call_seconds"] == 3.5


def test_a_focus_that_blows_up_inside_the_engine_is_never_a_verdict_on_the_host(cli_module, rig,
                                                                                monkeypatch):
    """`focus_lane` answers rather than raises, by contract — and that contract is a promise to a
    UI. If it is ever broken, the launch's own JSON body must survive, and the detail must not read
    as a diagnosis of the host or the window."""
    def boom(*a, **kw):
        raise RuntimeError("focus_lane broke its own contract")

    monkeypatch.setattr(cli_module.focus_lib, "focus_lane", boom)
    outcome, detail = cli_module._debug_focus(str(rig.home), "d4", 5)
    assert outcome == focus.HOST_UNREACHABLE      # "we could not be told", never "it is gone"
    assert "inside the engine itself" in detail and "RuntimeError" in detail
    assert "says nothing about the host" in detail
