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

    def seed_lane(self, iid="i1", branch="sl/i1-thing", worktree=True, status=None):
        import loopstate
        st = loopstate.new_state()
        issue = loopstate.new_issue()
        issue["branch"] = branch
        if status:
            issue["status"] = status
        st["issues"][iid] = issue
        loopstate.save(str(self.home / "state" / "issues.json"), st)
        # A worker resumes INTO its worktree; a lane whose worktree was reclaimed has nowhere to be
        # revived, which is its own refusal (test_a_reclaimed_worktree_is_refused).
        if worktree:
            (self.home / "worktrees" / iid).mkdir(parents=True, exist_ok=True)

    def brief(self, iid="i1"):
        """The RESUME brief — a separate file from the lane's own brief, deliberately (P0-1)."""
        return (self.home / "briefs" / ("%s.resume.md" % iid)).read_text()

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
    assert not (rig.home / "briefs" / "i1.resume.md").exists(), "--check must write nothing"


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


def test_the_lanes_own_brief_is_never_overwritten_by_the_preamble(rig):
    # P0-1. The runner's crash-recovery relaunch re-runs launch-session.sh WITHOUT rebuilding the
    # brief. A preamble written over briefs/<id>.md would therefore be handed, verbatim, to a
    # brand-new empty session — "your conversation above survived intact" with no conversation and
    # no work instruction. The two must be separate files.
    rig.seed_lane()
    rig.record_session()
    (rig.home / "briefs").mkdir(parents=True, exist_ok=True)
    (rig.home / "briefs" / "i1.md").write_text("THE REAL ISSUE BRIEF: goal, DoD, boundaries")
    assert run(rig, "resume", "i1", "--json").returncode == 0
    assert (rig.home / "briefs" / "i1.md").read_text() == \
        "THE REAL ISSUE BRIEF: goal, DoD, boundaries"
    assert "Re-orientation" in rig.brief()


def test_a_codex_repo_is_refused_rather_than_told_it_was_resumed(rig):
    # P0-2. start-session.sh does not thread the session id into the codex branch, so a "resume"
    # there would start a COLD session while handing it a preamble asserting its conversation
    # survived — and report success. Refusing is the only honest answer.
    (rig.repo / ".superlooper" / "config.json").write_text(
        json.dumps({"version": 1, "repo": "o/r", "agent": "codex"}))
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 1
    assert "codex" in jbody(r)["error"].lower()
    assert rig.launch_calls() == []


def test_a_second_debugger_is_refused_even_under_a_different_id(rig):
    # P1-6. `superlooper debug` refuses on ANY live worker.d*.lock — never two debuggers on one
    # patient. A revive of d3 while d7 is live must answer to the same rule, or it puts two
    # permissions-bypassed agents in one checkout.
    rig.record_session("d3")
    (rig.home / "state" / "worker.d7.lock").write_text(str(os.getpid()))
    r = run(rig, "resume", "d3", "--json")
    assert r.returncode == 1
    assert "d7" in jbody(r)["error"]
    assert rig.launch_calls() == []


def test_a_corrupt_recorded_id_is_refused_not_handed_to_the_launcher(rig):
    # Disk state is never trusted on shape. claude refuses a non-UUID, so the operator should get a
    # sentence here rather than a session that dies with its tab.
    rig.seed_lane()
    rig.record_session(sid="not-a-uuid")
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 1
    assert "uuid" in jbody(r)["error"].lower()
    assert rig.launch_calls() == []


def test_a_failed_launch_leaves_no_preamble_behind(rig):
    rig.seed_lane()
    rig.record_session()
    assert run(rig, "resume", "i1", "--json", env_over={"STUB_RC": "2"}).returncode == 1
    assert not (rig.home / "briefs" / "i1.resume.md").exists(), \
        "a preamble that reached nobody must not linger as the next reader's opening message"


def test_check_no_longer_answers_a_cmux_question_the_launch_never_asks(rig):
    """The preflight used to report `resumable: false` whenever no cmux pane resolved. Issue #308
    made the launch ask the session host for a workspace instead, so that answer described a fact
    the revive no longer depends on — and on a login-item runner (#306) it would have reported
    every recorded session unresumable."""
    rig.seed_lane()
    rig.record_session()
    (rig.home / "state" / "runner.anchor.json").unlink()
    r = run(rig, "resume", "i1", "--check", "--json")
    assert r.returncode == 0
    out = jbody(r)
    assert out["verb"] == "resume-check"
    assert out["resumable"] is True and out["error"] is None
    assert rig.launch_calls() == [], "--check writes nothing and launches nothing"


def test_the_preamble_states_the_real_repository_facts(rig):
    """The collection half, against a REAL git worktree (the suite's ban is on cmux/gh/claude/
    osascript; git is used throughout these tests). Without this the branch/HEAD/dirty path of
    _resume_facts is never executed — every fact would degrade to 'unknown' and the tests would
    still pass."""
    rig.seed_lane(status="running")
    rig.record_session()
    wt = rig.home / "worktrees" / "i1"
    env = {**os.environ, "HOME": str(rig.tmp / "userhome"), "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, env=env)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(wt), "config", k, v], check=True, env=env)
    (wt / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "init"], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "-C", str(wt), "checkout", "-qb", "sl/i1-thing"], check=True, env=env)
    (wt / "SCRATCH").write_text("uncommitted\n")
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True,
                          text=True, env=env).stdout.strip()

    assert run(rig, "resume", "i1", "--json").returncode == 0
    brief = rig.brief()
    assert "sl/i1-thing" in brief, "the branch must be the one git actually reports"
    assert head[:12] in brief, "the HEAD must be re-read, not remembered"
    assert "1 uncommitted change" in brief
    assert "**Lane status:** running" in brief, "the runner's recorded status must be named"
    # fake-gh answers from the fixture, which carries an OPEN PR on this head — so the whole
    # gh.pr_for_branch path is genuinely exercised, not just its unknown fallback.
    assert "**PR:** #555 (OPEN)" in brief


def test_a_refused_github_lookup_is_never_rendered_as_no_pr(rig):
    # The fail-closed half, end to end: gh REFUSES (GH_FAIL), so pr_for_branch returns ok=False.
    # A revived session told "there is no PR" opens a second one on the same branch.
    rig.seed_lane()
    rig.record_session()
    wt = rig.home / "worktrees" / "i1"
    env = {**os.environ, "HOME": str(rig.tmp / "userhome"), "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", "-b", "sl/i1-thing", str(wt)], check=True, env=env)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(wt), "config", k, v], check=True, env=env)
    (wt / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "init"], check=True,
                   capture_output=True, env=env)

    assert run(rig, "resume", "i1", "--json", env_over={"GH_FAIL": "1"}).returncode == 0
    brief = rig.brief()
    assert "GitHub did not answer" in brief
    assert "no PR exists on this branch" not in brief


# ---- the watchdog-lock singleton for debugger revives (fresh-agent review, round 3) ----
# These pin behaviour added late; without them a future edit could drop the lock and keep a green
# suite. `superlooper debug` holds watchdog.lock across check-and-launch because a just-launched
# debugger does not hold its worker.d<N>.lock until start-session.sh acquires it in the new tab,
# up to 30s later — so the lock, not the lock-file read, is what makes the two exclusive.

def test_a_live_watchdog_check_blocks_a_debugger_revive_and_keeps_its_own_lock(rig):
    rig.record_session("d3")
    lock = rig.home / "state" / "watchdog.lock"
    lock.write_text(str(os.getpid()))          # a LIVE holder — this very process
    r = run(rig, "resume", "d3", "--json")
    assert r.returncode == 1
    assert "watchdog" in jbody(r)["error"].lower()
    assert rig.launch_calls() == []
    # _watchdog_release_lock is ownership-checked, so our finally must not steal a stranger's lock.
    assert lock.exists() and lock.read_text() == str(os.getpid())


def test_a_debugger_revive_releases_the_lock_so_the_next_one_can_run(rig):
    rig.record_session("d3")
    assert run(rig, "resume", "d3", "--json").returncode == 0
    assert not (rig.home / "state" / "watchdog.lock").exists(), "the lock must not be held after"
    assert run(rig, "resume", "d3", "--json").returncode == 0, "a second revive must not deadlock"
    assert len(rig.launch_calls()) == 2


def test_a_worker_revive_takes_no_watchdog_lock_at_all(rig):
    # The lock belongs to the debugger seat's singleton; an i-lane has no business touching it.
    rig.seed_lane()
    rig.record_session()
    assert run(rig, "resume", "i1", "--json").returncode == 0
    assert not (rig.home / "state" / "watchdog.lock").exists()


def test_a_spawned_but_failed_launch_keeps_its_preamble(rig):
    """The counterpart to test_a_failed_launch_leaves_no_preamble_behind. A launch that SPAWNED but
    returned neither 0 nor 2 may still have a tab about to read the preamble — _resume_launch
    reports a timeout as spawned=True for exactly that reason. Removing it would abort that session
    at the (now fail-closed) brief selection."""
    rig.seed_lane()
    rig.record_session()
    r = run(rig, "resume", "i1", "--json", env_over={"STUB_RC": "1"})
    assert r.returncode == 1                    # honest failure...
    assert (rig.home / "briefs" / "i1.resume.md").exists(), \
        "...but the preamble stays: only rc=2 and a never-spawned exec prove nobody read it"


def test_a_revive_launches_with_no_cmux_anchor_at_all(rig):
    """The LAUNCH half of the gate #308 removed — `--check` is covered above, but a gate re-added
    below it would not be caught by that. Under a login-item runner (#306) no anchor resolves, and
    a revive is the operator's own recovery verb: it must still fly."""
    rig.seed_lane()
    rig.record_session()
    (rig.home / "state" / "runner.anchor.json").unlink()
    r = run(rig, "resume", "i1", "--json")
    assert r.returncode == 0, r.stderr
    out = jbody(r)
    assert out["ok"] is True, out
    assert rig.launch_calls(), "the revive must reach the launcher with no pane to launch into"
