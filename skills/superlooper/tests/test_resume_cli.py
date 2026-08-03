"""`superlooper resume <id>` (issue #298): the minimal owner verb that re-enters an interrupted
session's conversation instead of restarting it cold from zero.

Before #298 the launch stack passed neither `--session-id` nor `--resume`, so a killed pane, a
crashed host or a closed lid ended a flight's conversation permanently. The runner now mints the id
at spawn and records it in lane state; this verb is the door that spends it.

Everything external is injected, per the suite's fail-closed rule: the state base via SL_HOME, a
FAKE launch script via SL_LAUNCH_SESSION (no test reaches a real cmux or a real Claude), fake-gh
via SL_GH. What the tests pin is the handoff — the recorded id reaches the launcher as
SL_RESUME_SESSION_ID, and the brief the revived session opens on is the re-orientation preamble.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

_FAKE_LAUNCH = """#!/bin/bash
{ printf 'ARGS %s\\n' "$*"
  printf 'RESUME_ID %s\\n' "${SL_RESUME_SESSION_ID:-}"
  printf 'PANE %s\\n' "${SL_PANE:-}"
  printf 'ROOT %s\\n' "${SL_RUN_ROOT:-}"
  printf 'REPO %s\\n' "${SL_REPO:-}"
  printf 'AGENT %s\\n' "${SL_AGENT:-}"
  printf 'ATTENDED %s\\n' "${SL_ATTENDED:-}"
} >> "$STUB_LOG"
exit "${STUB_RC:-0}"
"""

SID = "5ddf8f39-7ec2-4936-967f-9eca52d71a9d"


class _Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        fixdir = tmp_path / "gh"
        shutil.copytree(_FIXTURES, fixdir)
        self.repo = tmp_path / "repo"
        (self.repo / ".superlooper").mkdir(parents=True)
        (self.repo / ".superlooper" / "config.json").write_text(
            json.dumps({"version": 1, "repo": "o/r"}))
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
        self.env.pop("SL_PANE", None)
        self.env.pop("GH_FAIL", None)
        self.anchor()

    def anchor(self, pane="PANE-UUID-1"):
        (self.home / "state" / "runner.anchor.json").write_text(
            json.dumps({"pane": pane, "workspace": "", "window": "", "pid": 1}))

    def record_session(self, iid="i1", sid=SID):
        d = self.home / "state" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / iid).write_text(sid)

    def seed_lane(self, iid="i1", branch="sl/i1-thing", worktree=True):
        import loopstate
        st = loopstate.new_state()
        issue = loopstate.new_issue()
        issue["branch"] = branch
        st["issues"][iid] = issue
        loopstate.save(str(self.home / "state" / "issues.json"), st)
        # A worker resumes INTO its worktree; a lane whose worktree was reclaimed has nowhere to be
        # revived, which is its own refusal (test_a_reclaimed_worktree_is_refused).
        if worktree:
            (self.home / "worktrees" / iid).mkdir(parents=True, exist_ok=True)

    def brief(self, iid="i1"):
        return (self.home / "briefs" / ("%s.md" % iid)).read_text()

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


@pytest.fixture
def rig(tmp_path):
    return _Rig(tmp_path)


def run(rig, *args, env_over=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args, "--repo", str(rig.repo)],
                          capture_output=True, text=True, env=env, timeout=60)


def jbody(r):
    return json.loads(r.stdout)


# --------------------------- the handoff ---------------------------

def test_the_recorded_id_reaches_the_launcher_as_the_resume_seam(rig):
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 0, r.stderr
    out = jbody(r)
    assert out["ok"] is True and out["session_id"] == SID
    call = rig.launch_calls()[0]
    # The whole point: the launcher must be told to re-enter THIS conversation, not mint a new one.
    assert call["RESUME_ID"] == SID
    assert call["ARGS"] == "i1", "a worker lane resumes through worker mode, not --cwd"


def test_the_revived_session_opens_on_the_re_orientation_preamble(rig):
    # DoD: "Resumed sessions receive a re-orientation preamble before any new work instruction."
    # start-session.sh cats the brief in as the opening message, so the brief IS that preamble.
    rig.seed_lane()
    rig.record_session()
    assert run(rig, "resume", "i1", "--json").returncode == 0
    brief = rig.brief()
    assert SID in brief
    assert "interrupt" in brief.lower()
    assert "re-read" in brief.lower() or "as of" in brief.lower()


def test_an_operator_note_lands_after_the_re_orientation_not_before(rig):
    rig.seed_lane()
    rig.record_session()
    assert run(rig, "resume", "i1", "--note", "rebase onto main", "--json").returncode == 0
    brief = rig.brief()
    assert "rebase onto main" in brief
    assert brief.index(SID) < brief.index("rebase onto main"), \
        "an instruction placed above the facts is acted on under a stale world-model"


def test_a_debugger_lane_resumes_in_place_through_cwd_mode(rig):
    # d<N> has no worktree and no branch — it launches in an existing dir, so its revive must use
    # the same --cwd form the original launch did.
    rig.record_session("d3")
    r = run(rig, "resume", "d3", "--json")
    assert r.returncode == 0, r.stderr
    call = rig.launch_calls()[0]
    assert call["RESUME_ID"] == SID
    assert call["ARGS"].startswith("--cwd "), f"expected --cwd form, got {call['ARGS']!r}"
    assert call["ARGS"].endswith("d3")


def test_the_resume_is_attended_because_a_person_typed_it(rig):
    # Same reasoning as `superlooper debug`'s owner tap (#185): a human is at the keyboard, so the
    # PreToolUse deny must stand its AskUserQuestion duty down rather than assert nobody is there.
    rig.record_session("d3")
    assert run(rig, "resume", "d3", "--json").returncode == 0
    assert rig.launch_calls()[0]["ATTENDED"] == "1"


# --------------------------- refusals ---------------------------

def test_a_lane_with_no_recorded_id_is_refused_plainly(rig):
    # Sessions launched before #298 have no recorded id. There is nothing to resume, and the honest
    # answer is to say so — never to launch a cold session while calling it a resume.
    rig.seed_lane()
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 1
    out = jbody(r)
    assert out["ok"] is False and "no recorded session id" in out["error"].lower()
    assert rig.launch_calls() == [], "nothing may launch when there is nothing to resume"


def test_a_live_session_is_never_joined_by_a_second_one(rig):
    # start-session.sh's per-id worker lock is the same singleton `debug` respects: two agents in
    # one worktree clobber one branch.
    rig.seed_lane()
    rig.record_session()
    (rig.home / "state" / "worker.i1.lock").write_text(str(os.getpid()))
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 1
    assert "already" in jbody(r)["error"].lower()
    assert rig.launch_calls() == []


def test_a_stale_lock_from_a_dead_worker_does_not_block_the_revive(rig):
    # The lock names a pid. A crashed host leaves it behind naming a pid that is long gone — which
    # is EXACTLY the situation this verb exists for, so it must not be read as "still running".
    rig.seed_lane()
    rig.record_session()
    (rig.home / "state" / "worker.i1.lock").write_text("999999")
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 0, r.stderr
    assert rig.launch_calls()[0]["RESUME_ID"] == SID


def test_a_malformed_lane_id_is_refused_before_anything_is_written(rig):
    r = run(rig, "resume", "../../etc/passwd", "--json")
    assert r.returncode == 1
    assert rig.launch_calls() == []


def test_a_failed_launch_is_reported_as_failed_not_as_success(rig):
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1", "--json", env_over={"STUB_RC": "2"})
    assert r.returncode == 1
    out = jbody(r)
    assert out["ok"] is False and out["error"]


def test_check_is_read_only_and_reports_what_could_be_resumed(rig):
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1", "--check", "--json")
    assert r.returncode == 0
    out = jbody(r)
    assert out["session_id"] == SID and out["resumable"] is True
    assert rig.launch_calls() == [], "--check must launch nothing"
    assert not (rig.home / "briefs" / "i1.md").exists(), "--check must write nothing"


def test_the_human_output_names_the_session_without_json(rig):
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1")
    assert r.returncode == 0, r.stderr
    assert SID in r.stdout and "i1" in r.stdout


def test_a_reclaimed_worktree_is_refused_rather_than_resumed_into_nothing(rig):
    # The runner prunes a finished lane's worktree. Resuming into a directory that no longer holds
    # the branch would revive the conversation somewhere it cannot do its work.
    rig.seed_lane(worktree=False)
    rig.record_session()
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 1
    assert "worktree" in jbody(r)["error"].lower()
    assert rig.launch_calls() == []
