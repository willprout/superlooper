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
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

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
  printf 'VERIFY %s\\n' "${SL_LAUNCH_VERIFY_SECONDS:-unset}"
  # What the watchdog counter looked like AS THE LAUNCHER SAW IT: proof the id was durably
  # advanced BEFORE anything could launch, not merely after the shim returned.
  printf 'STATE_AT_LAUNCH %s\\n' "$(cat "${SL_RUN_ROOT:-}/state/watchdog.json" 2>/dev/null | tr -d '\\n ')"
} >> "$STUB_LOG"
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
    assert call["AGENT"] == "claude"
    assert call["MODEL"] == "opus[1m]"


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

def test_debug_refuses_when_no_cmux_pane_resolves(rig):
    # No recorded anchor and not inside cmux: the shim would abort anyway, and a launch that
    # half-happens is worse than one that plainly did not. Refuse BEFORE burning an id.
    rig.wstate(next_debugger=4)
    r = run(rig, "debug", "--repo", str(rig.repo), "--json", "--note", "x")
    assert r.returncode != 0
    b = body(r)
    assert b["ok"] is False and "pane" in b["error"]
    assert rig.launch_calls() == []
    assert rig.read_wstate()["next_debugger"] == 4
    assert not (rig.home / "briefs").exists()


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
