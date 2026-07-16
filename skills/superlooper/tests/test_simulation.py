"""Task 15 — the offline end-to-end simulation (the acceptance harness).

REAL runner ticks (the actual Runner class, its actual executors, the actual launch stack:
launch-session.sh -> fake-cmux's tab-shell/shim -> start-session.sh -> fake-claude) against a
REAL local git repo pair (a bare origin + a working clone in tmp) and three fakes:

  fake-gh      a stateful little GitHub (tests/fakes/fake-gh, state.json mode): label moves,
               comments, PR creation and REAL squash-merges into the bare origin's dev branch.
  fake-cmux    tabs/screens/sends against tmp state; deliver mode executes the dropped .cmd
               (the launch-shim contract); drop mode loses the keystrokes (the overnight bug).
  fake-claude  plays each session per a SCENARIO spec — invoked exactly like the real binary
               (brief contents as final argv, SL_ISSUE_ID/SL_RUN_ROOT from env) and reads its
               contract paths out of the brief text itself, so a broken brief template fails
               the rehearsal loudly.

Time is VIRTUAL: each tick advances a private clock ~91s so every tick re-polls GitHub, while
file mtimes stay real — liveness scenarios therefore configure their own idle/freeze thresholds
instead of sleeping. Every wait in the harness and in the fakes is BOUNDED (the watchdog rule);
teardown touches a global SIM_STOP file so no fake session outlives its test.

This suite is the evidence William sees before ANY live run (plan Task 15): every §C.4 gate
path, the answerer loop, the conflict ladder, freeze ownership, restart reconciliation, and the
paid-for pokes from waves 3-4 are asserted here against the real tick loop.
"""
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
FAKES = os.path.join(HERE, "fakes")

# conftest.py already puts skill/lib before skill/bin on sys.path (a pinned ordering —
# test_conftest_paths guards it); this file must not re-insert and scramble it.
import actions as actions_lib    # noqa: E402
import config as config_lib      # noqa: E402
import journal as journal_lib    # noqa: E402
import loopstate                 # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

GREEN_CI = [{"name": "ci", "status": "completed", "conclusion": "success"}]
RED_CI = [{"name": "ci", "status": "completed", "conclusion": "failure"}]

ISSUE_BODY = """\
## Goal
{goal}

## Definition of done
- [ ] the simulated change exists on dev

## Boundaries
Only what the goal says.

## Loop metadata
touches: {touches}
{extra_meta}"""


def _run(args, cwd=None, env=None, check=True):
    r = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise AssertionError("command failed rc=%d: %s\n%s%s"
                             % (r.returncode, " ".join(map(str, args)), r.stdout, r.stderr))
    return r


class Sim:
    """One simulated adopted repo + its runner. See the module docstring for the moving parts."""

    def __init__(self, tmp_path, monkeypatch, lanes=2, affinity="hard", areas=None,
                 required_checks=("ci",), session=None, retry_cap=None, conflict_cap=None,
                 cleanup_merged_worktrees=True, qa=None, touches_required=False):
        self.tmp = tmp_path
        self.origin = tmp_path / "origin"
        self.repo = tmp_path / "repo"
        self.home_dir = tmp_path / "home"            # isolated $HOME
        self.gh_dir = tmp_path / "gh"                # GH_FIXTURES (state.json mode)
        self.cmux_dir = tmp_path / "cmux"
        self.launch_dir = tmp_path / "launch"
        self.scenario_dir = tmp_path / "scenarios"
        self.stop_file = tmp_path / "SIM_STOP"
        self.notify_log = tmp_path / "notify.log"
        bin_dir = tmp_path / "bin"
        for d in (self.home_dir, self.gh_dir, self.cmux_dir, self.launch_dir,
                  self.scenario_dir, bin_dir):
            d.mkdir(parents=True, exist_ok=True)

        # ---- a real git repo pair: bare origin (workers push here) + working clone ----
        genv = {**os.environ, "HOME": str(self.home_dir), "GIT_TERMINAL_PROMPT": "0"}
        (self.home_dir / ".gitconfig").write_text(
            "[user]\n\tname = sim\n\temail = sim@example.com\n[init]\n\tdefaultBranch = main\n")
        _run(["git", "init", "--bare", "-b", "main", str(self.origin)], env=genv)
        seed = tmp_path / "seed"
        _run(["git", "clone", str(self.origin), str(seed)], env=genv)
        (seed / "README.md").write_text("simulated repo\n")
        (seed / "src").mkdir()
        (seed / "src" / "shared.txt").write_text(
            "\n".join("line %d" % i for i in range(1, 6)) + "\n")
        _run(["git", "checkout", "-q", "-b", "main"], cwd=seed, env=genv, check=False)
        _run(["git", "add", "-A"], cwd=seed, env=genv)
        _run(["git", "commit", "-q", "-m", "seed"], cwd=seed, env=genv)
        _run(["git", "push", "-q", "-u", "origin", "main"], cwd=seed, env=genv)
        _run(["git", "clone", str(self.origin), str(self.repo)], env=genv)

        # ---- the adopted repo's config ----
        sess = {"idle_seconds": 100000, "freeze_seconds": 200000,
                "retry_cap": 2, "conflict_cap": 2}
        sess.update(session or {})
        if retry_cap is not None:
            sess["retry_cap"] = retry_cap
        if conflict_cap is not None:
            sess["conflict_cap"] = conflict_cap
        cfg = {
            "repo": "sim/repo", "dev_branch": "main", "lanes": lanes, "affinity": affinity,
            "areas": areas or {}, "required_checks": list(required_checks),
            # This general harness adds issues with empty `touches:` by default, so it models a
            # touches_required:false repo unless a test opts in (issue #36 enforcement is unit-tested
            # in test_actions and exercised end-to-end by the touches_required=True sim below).
            "touches_required": touches_required,
            "session": sess, "cleanup_merged_worktrees": cleanup_merged_worktrees,
            "notify": {"cmd": "printf '%s|%s\\n' \"$SL_TITLE\" \"$SL_BODY\" >> "
                              + str(self.notify_log)},
        }
        if qa:
            cfg["qa"] = qa
        (self.repo / ".superlooper").mkdir()
        (self.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg, indent=1))

        # ---- fake-gh's little GitHub ----
        self.state_json = self.gh_dir / "state.json"
        self._write_gh_state({
            "next_num": 1, "issues": {}, "prs": {}, "dev_branch": "main",
            "check_names": ["ci"], "pr_check_conclusion": "SUCCESS",
            # REST check-runs use LOWERCASE conclusions (the GraphQL PR rollup is uppercase) —
            # the fake mirrors the real service, so the runner must handle both.
            "branch_checks": {"main": [dict(c) for c in GREEN_CI]},
        })

        # ---- claude on PATH = fake-claude, exactly as start-session.sh invokes it ----
        claude = bin_dir / "claude"
        claude.write_text("#!/usr/bin/env bash\nexec '%s' \"$@\"\n"
                          % os.path.join(FAKES, "fake-claude"))
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # ---- environment (inherited by the runner's scripts, the fakes, and fake workers) ----
        for k, v in {
            "HOME": str(self.home_dir),
            "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
            "SL_HOME": str(tmp_path / "slhome"),
            "SL_GH": os.path.join(FAKES, "fake-gh"),
            "GH_FIXTURES": str(self.gh_dir),
            "GH_GIT_REPO": str(self.origin),
            "SL_CMUX": os.path.join(FAKES, "fake-cmux"),
            "FAKE_CMUX_DIR": str(self.cmux_dir),
            "CMUX_MODE": "deliver",
            "SL_LAUNCH_DIR": str(self.launch_dir),
            "SIM_SCENARIO_DIR": str(self.scenario_dir),
            "SIM_STOP": str(self.stop_file),
            "GIT_TERMINAL_PROMPT": "0",
            "SL_LAUNCH_VERIFY_SECONDS": "15",
        }.items():
            monkeypatch.setenv(k, v)

        self.config = config_lib.load(str(self.repo))
        self.home = str(config_lib.state_home(self.config))
        self.runner = self._make_runner()
        # suppress the once-a-day morning report unless a test asks for it
        with open(os.path.join(self.home, "state", "last_morning_report"), "w") as f:
            f.write(time.strftime("%Y-%m-%d"))
        self.now = time.time()

    def _make_runner(self, fetch_usage=None):
        from runner import Runner
        return Runner(repo=str(self.repo), config=self.config, state_home=self.home,
                      pane="11111111-aaaa-aaaa-aaaa-111111111111",
                      fetch_usage=fetch_usage or (lambda: {
                          "auth_status": "ok", "five_hour_pct": 5, "seven_day_pct": 5}))

    # ------------------------- GitHub-side helpers -------------------------

    @staticmethod
    def _lock_holder(path):
        """(pid_or_None, alive) — the exact fake-gh._holder semantics. pid<=0 (an empty or
        garbage lock) is unreadable, never 'alive' (os.kill(0,0) signals our own process
        group and would read as alive forever — Codex round-2 nit)."""
        try:
            with open(path) as f:
                pid = int(f.read().strip() or "0")
        except (OSError, ValueError):
            return None, False
        if pid <= 0:
            return None, False
        try:
            os.kill(pid, 0)
            return pid, True
        except ProcessLookupError:
            return pid, False
        except (OSError, OverflowError):
            return pid, True

    @contextlib.contextmanager
    def _gh_lock(self):
        """The SAME lock protocol as fake-gh's _Lock — atomic link-with-pid; steal ONLY a
        provably-dead holder (or an unreadable-and-ancient lock), re-checking the content
        before the unlink so a fresh claimer's lock is never deleted; ownership-checked
        release (Codex round-2: a weaker mirror re-opened the very lost-update class the
        round-1 fix closed). A test editing state.json while fake workers are live must be
        indistinguishable from another fake-gh."""
        lock = str(self.state_json) + ".lock"
        tmp = "%s.%d" % (lock, os.getpid())
        with open(tmp, "w") as f:
            f.write(str(os.getpid()))
        try:
            deadline = time.time() + 20
            while True:
                try:
                    os.link(tmp, lock)
                    break
                except FileExistsError:
                    pid, alive = self._lock_holder(lock)
                    try:
                        stale = time.time() - os.stat(lock).st_mtime > 30
                    except OSError:
                        continue                     # vanished between link and stat: retry
                    if (pid is not None and not alive) or (pid is None and stale):
                        cur, _ = self._lock_holder(lock)
                        if cur == pid:               # never delete a fresh claimer's lock
                            try:
                                os.remove(lock)
                            except OSError:
                                pass
                        continue
                    assert time.time() < deadline, "gh state lock timeout in the harness"
                    time.sleep(0.01)
            try:
                yield
            finally:
                pid, _ = self._lock_holder(lock)     # ownership-checked release
                if pid == os.getpid():
                    try:
                        os.remove(lock)
                    except OSError:
                        pass
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _write_gh_state(self, st):
        fd, tmp = tempfile.mkstemp(dir=str(self.gh_dir), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, self.state_json)     # readers see old-or-new, never a torn file

    def gh_state(self):
        with open(self.state_json) as f:
            return json.load(f)

    def edit_gh_state(self, fn):
        with self._gh_lock():
            st = self.gh_state()
            out = fn(st)
            self._write_gh_state(st)
        return out

    def add_issue(self, title="Do the thing", touches="", labels=("type:build", "agent-ready"),
                  goal="Make the simulated change.", extra_meta="", scenario=None):
        """Seed one issue directly into the fake GitHub (test setup — William already wrote and
        approved it). Returns the issue number. `scenario` (a dict) is stored for the id."""
        def seed(st):
            num = st["next_num"]
            st["next_num"] = num + 1
            st["issues"][str(num)] = {
                "number": num, "title": title, "state": "open", "labels": list(labels),
                "comments": [],
                "createdAt": "2026-07-01T00:00:%02dZ" % (num % 60),
                "body": ISSUE_BODY.format(goal=goal, touches=touches, extra_meta=extra_meta),
            }
            return num
        num = self.edit_gh_state(seed)
        if scenario is not None:
            self.set_scenario("i%d" % num, scenario)
        return num

    def set_scenario(self, sid, spec):
        with open(self.scenario_dir / ("%s.json" % sid), "w") as f:
            json.dump(spec, f)

    def fail_next(self, match, times=1, stderr=None):
        """Arm a fake-gh blip: the next `times` calls whose argv contains `match` fail. `stderr`
        (optional) sets the failure's stderr verbatim — use a realistic message when the product
        surfaces that stderr into a later gh call, so it doesn't echo `match` and re-arm the rule."""
        path = self.gh_dir / "fail_rules.json"
        rules = []
        if path.exists():
            rules = json.loads(path.read_text())
        rule = {"match": match, "times": times}
        if stderr is not None:
            rule["stderr"] = stderr
        rules.append(rule)
        path.write_text(json.dumps(rules))

    def issue(self, num):
        return self.gh_state()["issues"][str(num)]

    def prs_for(self, num=None):
        prs = list(self.gh_state()["prs"].values())
        return prs if num is None else [p for p in prs if p["number"] == num]

    def mutations(self, kind=None):
        path = self.gh_dir / "mutations.jsonl"
        if not path.exists():
            return []
        out = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        return out if kind is None else [m for m in out if m["kind"] == kind]

    # ------------------------- runner-side helpers -------------------------

    def tick(self, advance=91):
        self.now += advance
        self.runner.tick(now=self.now)

    def tick_until(self, pred, ticks=25, advance=91, settle=0.1):
        """Bounded tick loop: advance the virtual clock, run a real tick, check. The small real
        sleep lets background fake sessions make progress between ticks."""
        for _ in range(ticks):
            self.tick(advance)
            if pred():
                return True
            time.sleep(settle)
        return False

    def wait_file(self, path, timeout=30, gone=False):
        """Bounded REAL-time wait for an async fake session to produce (or consume) a file."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(str(path)) != gone:
                return True
            time.sleep(0.05)
        return False

    def journal(self, act=None):
        recs = journal_lib.read(self.home)
        return recs if act is None else [r for r in recs if r.get("act") == act]

    def loop_issue(self, sid):
        st = loopstate.load(os.path.join(self.home, "state", "issues.json"))
        return st["issues"].get(sid, {})

    def notify_lines(self):
        if not self.notify_log.exists():
            return []
        return [l for l in self.notify_log.read_text().splitlines() if l.strip()]

    def sends(self):
        path = self.cmux_dir / "sends.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def surfaces(self):
        path = self.cmux_dir / "surfaces.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def origin_file(self, path, ref="main"):
        r = _run(["git", "-C", str(self.origin), "show", "%s:%s" % (ref, path)], check=False)
        return r.stdout if r.returncode == 0 else None

    def origin_tip(self, branch):
        r = _run(["git", "-C", str(self.origin), "rev-parse", "--verify",
                  "refs/heads/%s" % branch], check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def frozen_marker(self):
        path = os.path.join(self.home, "state", "merges_frozen.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def stop(self):
        """Teardown: end every fake session (bounded), so nothing outlives the test."""
        self.stop_file.write_text("stop")
        deadline = time.time() + 10
        state = os.path.join(self.home, "state")
        while time.time() < deadline:
            live = []
            for n in os.listdir(state):
                if n.startswith("worker.") and n.endswith(".lock"):
                    try:
                        pid = int(open(os.path.join(state, n)).read().strip())
                        os.kill(pid, 0)
                        live.append(n)
                    except (ValueError, OSError):
                        pass
            if not live:
                return
            time.sleep(0.1)


@pytest.fixture
def sim_factory(tmp_path, monkeypatch):
    sims = []

    def make(**kw):
        s = Sim(tmp_path, monkeypatch, **kw)
        sims.append(s)
        return s

    yield make
    for s in sims:
        s.stop()


# =====================================================================================
# happy path: issue -> launch -> build -> gate -> merged + labels + journal + closed
# =====================================================================================

def test_happy_path_issue_to_merged(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Add the widget", scenario={"scenario": "happy"})
    sid = "i%d" % num

    # launch tick: the runner claims the issue and launches through the REAL stack
    sim.tick()
    assert sim.loop_issue(sid).get("status") == "running", sim.journal()
    assert "in-progress" in sim.issue(num)["labels"]
    assert "agent-ready" not in sim.issue(num)["labels"]

    # the fake worker builds asynchronously: report is its LAST action
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid)), \
        "worker never finished: %s" % sim.journal()

    # gate ticks: PR + report + review evidence + checks green + mergeable -> squash-merge
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        "never merged: %s" % [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # GitHub truth: PR merged, issue closed via Closes #N, labels settled
    pr = sim.prs_for()[0]
    assert pr["state"] == "MERGED"
    assert sim.issue(num)["state"] == "closed"
    assert "in-progress" not in sim.issue(num)["labels"]
    # the merge REALLY landed on the origin's dev branch
    assert sim.origin_file("src/%s.txt" % sid) is not None
    # review evidence was a PR comment beginning the exact marker
    assert any(c["body"].startswith("<!-- superlooper-review -->") for c in pr["comments"])
    # the runner journaled the lifecycle
    assert [r for r in sim.journal("launch") if r.get("outcome") == "ok"]
    assert [r for r in sim.journal("merge") if r.get("outcome") == "ok"]
    # merged worktree cleaned up (config default)
    assert not os.path.isdir(os.path.join(sim.home, "worktrees", sid))
    # exactly one merge mutation — never a double merge
    assert len(sim.mutations("merge_pr")) == 1


# =====================================================================================
# the review-evidence gate (§C.4 step 2b): no verdict comment -> nudge once -> park
# =====================================================================================

def test_no_review_evidence_nudges_once_then_parks_never_merges(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Widget without review",
                        scenario={"scenario": "no-review", "linger": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    assert not sim.mutations("merge_pr"), "an unreviewed PR must NEVER merge"
    # the one nudge was really delivered into a LIVE pane, and named the review contract
    gate_nudges = [s for s in sim.sends()
                   if "review" in s.get("text", "") and "[superlooper gate]" in s.get("text", "")]
    assert len(gate_nudges) == 1, sim.sends()
    assert "parked" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    memos = [m for m in sim.mutations("comment") if "parked" in m["body"]]
    assert memos and "review" in memos[0]["body"]
    assert any("parked" in ln for ln in sim.notify_lines()), \
        "every transition to parked must notify (standing rule)"


def test_one_nudge_recovery_report_fixed_after_nudge_merges(sim_factory):
    # the nudge is a HANDBACK, not a death sentence: a worker that rewrites its empty-sectioned
    # report after the one nudge passes the gate and merges (invented scenario — proves the
    # nudge->rewrite->re-gate path end to end, not just nudge->park).
    sim = sim_factory()
    num = sim.add_issue(title="Fixes report on nudge",
                        scenario={"scenario": "empty-sections", "linger": True,
                                  "comply_on_nudge": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert len(sim.mutations("merge_pr")) == 1
    assert any("[superlooper gate]" in s.get("text", "") for s in sim.sends())


def test_empty_report_sections_nudge_then_park_never_merge(sim_factory):
    # cross-review C3 end to end: headings present, bodies EMPTY — looks "complete" to a
    # headings-only check, must never merge.
    sim = sim_factory()
    num = sim.add_issue(title="Empty report bodies",
                        scenario={"scenario": "empty-sections", "linger": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked")
    assert not sim.mutations("merge_pr"), "empty headings must never merge"
    assert "parked" in sim.issue(num)["labels"]
    nudges = [s for s in sim.sends() if "[superlooper gate]" in s.get("text", "")]
    assert len(nudges) == 1, "exactly one nudge per cause, then park"


def test_pr_check_failure_hands_back_once_then_parks(sim_factory):
    # §C.4 step 5: a red REQUIRED check on the PR -> one handback nudge, then park. The check
    # stays red (the fake worker ignores the nudge), so the gate must never merge.
    sim = sim_factory()
    num = sim.add_issue(title="Red PR checks",
                        scenario={"scenario": "happy", "linger": True})
    sid = "i%d" % num
    sim.edit_gh_state(lambda st: st.update(pr_check_conclusion="FAILURE"))
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked")
    assert not sim.mutations("merge_pr")
    nudges = [s for s in sim.sends() if "required check failed" in s.get("text", "")]
    assert len(nudges) == 1


def test_unreported_required_check_escalates_once_to_william(sim_factory):
    # issue #26: a required_checks entry naming a check the repo NEVER reports reads as pending
    # forever — a green PR that never merges, with no park, no memo, no notify. Past the bound the
    # runner escalates ONCE to needs-owner, naming the unreported check. The repo reports "ci";
    # "ghost-check" is required but never reported, so the gate sits at pending until the bound.
    sim = sim_factory(required_checks=("ghost-check",),
                      session={"checks_pending_cap": 90})       # bound < one tick's 91s advance
    num = sim.add_issue(title="Green PR that never merges", scenario={"scenario": "happy"})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william"), \
        "never escalated: %s" % [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    # the merge decision stays fail-closed: pending NEVER merges
    assert not sim.mutations("merge_pr")
    assert (not sim.prs_for()) or sim.prs_for()[0]["state"] != "MERGED"
    # the label moved to needs-owner and the memo NAMES the unreported check
    assert "needs-owner" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    memos = [m for m in sim.mutations("comment")
             if "ghost-check" in m["body"] and "pending" in m["body"].lower()]
    assert memos, "the park memo must name the unreported check: %s" % sim.mutations("comment")
    # the escalation notifies (standing rule) exactly once
    notices = [ln for ln in sim.notify_lines() if "needs-owner" in ln]
    assert len(notices) == 1, sim.notify_lines()


def test_late_but_in_bound_check_still_merges(sim_factory):
    # issue #26: a check that reports LATE but within the bound merges cleanly — zero behavior
    # change for a healthy-but-slow repo. The required "ci" is pending, then reports SUCCESS.
    sim = sim_factory(session={"checks_pending_cap": 100000})   # generous bound: never escalates
    sim.edit_gh_state(lambda st: st.update(pr_check_conclusion="PENDING"))
    num = sim.add_issue(title="Slow but green", scenario={"scenario": "happy"})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    # a couple of pending ticks: the runner waits, never escalates, never merges
    sim.tick(); sim.tick()
    assert sim.loop_issue(sid).get("status") in ("gating", "holding")
    assert "needs-owner" not in sim.issue(num)["labels"] and not sim.mutations("merge_pr")
    # the check reports green within the bound -> the PR merges cleanly
    sim.edit_gh_state(lambda st: st.update(pr_check_conclusion="SUCCESS"))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        "never merged after the check reported green: %s" \
        % [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.prs_for()[0]["state"] == "MERGED"


# =====================================================================================
# issue #27: a gate-green PR whose MERGE GitHub refuses (branch protection / no merge rights)
# =====================================================================================

def test_refused_merge_is_bounded_and_escalates_once_to_william(sim_factory):
    # The end-to-end acceptance for issue #27 (DoD #1): the gate is green (report + review +
    # checks + mergeable), but GitHub REFUSES the squash-merge every tick — exactly what ordinary
    # branch protection (required approvals / strict up-to-date) or a token without merge rights
    # does. Before this fix the runner retried the merge every tick FOREVER: no counter, no cap, no
    # park, no notify — a green PR that never lands and never explains itself. Now the runner bounds
    # the retries, then parks needs-owner ONCE with the gh refusal reason in the memo, and STOPS.
    sim = sim_factory()
    num = sim.add_issue(title="Green PR whose merge is refused", scenario={"scenario": "happy"})
    sid = "i%d" % num
    # Every squash-merge refused with a realistic branch-protection stderr (the runner surfaces this
    # verbatim into the park memo, so it must not echo the 'pr merge' match — see fail_next).
    sim.fail_next("pr merge", times=99,
                  stderr="failed to merge: Protected branch update failed (2 approving reviews required)")
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid)), \
        "worker never finished: %s" % sim.journal()

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william"), \
        "never escalated: %s" % [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # retries are BOUNDED: the merge was attempted exactly to the cap, then stopped — never the
    # forever-retry this issue exists to kill. (A refused merge is journaled but never records a
    # merge_pr mutation, because the merge never actually happened.)
    merge_attempts = sim.journal("merge")
    assert len(merge_attempts) == actions_lib.MERGE_REFUSAL_CAP, \
        "expected exactly %d bounded merge attempts, got %r" \
        % (actions_lib.MERGE_REFUSAL_CAP, [r.get("outcome") for r in merge_attempts])
    assert all("refused" in (r.get("outcome") or "") for r in merge_attempts)
    assert not sim.mutations("merge_pr"), "a refused merge must never actually land"
    assert (not sim.prs_for()) or sim.prs_for()[0]["state"] != "MERGED"

    # further ticks do NOT resurrect the retry: the counter stays capped, the issue stays terminal
    sim.tick(); sim.tick()
    assert len(sim.journal("merge")) == actions_lib.MERGE_REFUSAL_CAP, "retries restarted after park"

    # label moved to needs-owner; the memo NAMES branch protection and carries the gh stderr
    assert "needs-owner" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    memos = [m for m in sim.mutations("comment")
             if "branch protection" in m["body"] and "2 approving reviews required" in m["body"]]
    assert memos, "the park memo must name branch protection AND carry gh's stderr: %s" \
        % sim.mutations("comment")
    # the escalation notifies (standing rule) exactly once
    notices = [ln for ln in sim.notify_lines() if "needs-owner" in ln]
    assert len(notices) == 1, sim.notify_lines()


def test_transient_merge_refusal_clears_within_the_bound_and_merges(sim_factory):
    # DoD #2: a refusal that clears WITHIN the bound still merges cleanly with zero noise — no
    # park, no needs-owner, no alarm. (fake-gh refuses only the FIRST merge; the retry lands.)
    sim = sim_factory()
    num = sim.add_issue(title="Merge refused once then clears", scenario={"scenario": "happy"})
    sid = "i%d" % num
    sim.fail_next("pr merge", times=1)                   # one blip, then the merge succeeds
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        "never merged after the transient refusal cleared: %s" \
        % [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.prs_for()[0]["state"] == "MERGED"
    assert len(sim.mutations("merge_pr")) == 1            # the retry really landed, exactly once
    # zero noise: never escalated, and no needs-owner notify fired
    assert "needs-owner" not in sim.issue(num)["labels"]
    assert not [ln for ln in sim.notify_lines() if "needs-owner" in ln], sim.notify_lines()


def test_finished_without_pr_parks_with_memo(sim_factory):
    # §C.4 step 1: a report with no PR anywhere is an immediate park-with-memo (no nudge —
    # there is nothing mechanical the worker could be reminded to re-post).
    sim = sim_factory()
    num = sim.add_issue(title="No PR", scenario={"scenario": "no-pr", "linger": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked")
    assert not sim.mutations("merge_pr")
    memos = [m for m in sim.mutations("comment") if "no PR exists" in m["body"]]
    assert memos, sim.mutations("comment")


def test_referee_path_pr_parks_needs_william_once_without_merging(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Referee path wander", touches="frontend",
                        scenario={"scenario": "happy",
                                  "edit": {"file": ".superlooper/config.json",
                                           "append": "\n"}})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    assert not sim.mutations("merge_pr")
    assert "needs-owner" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    memos = [m for m in sim.mutations("comment")
             if "diff reaches live referee path" in m["body"]]
    assert len(memos) == 1 and ".superlooper/config.json" in memos[0]["body"]
    notices = sim.notify_lines()
    assert len(notices) == 1 and ".superlooper/config.json" in notices[0]


def test_touches_required_parks_a_no_touches_issue_before_launch(sim_factory):
    # issue #36 end-to-end: with touches_required ON, an approved build issue that declares no
    # `touches:` is refused at INTAKE — parked needs-owner with a memo naming the missing block —
    # and NEVER launched (no worker session, no PR, no merge).
    sim = sim_factory(touches_required=True)
    num = sim.add_issue(title="Forgot the touches block", touches="")   # empty -> no declaration
    sid = "i%d" % num
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "needs-owner" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    assert not sim.mutations("merge_pr")                     # never launched -> never merged
    memos = [m for m in sim.mutations("comment") if "touches_required" in m["body"]]
    assert len(memos) == 1 and "touches:" in memos[0]["body"]
    notices = sim.notify_lines()
    assert notices and any(sid in n and "needs-owner" in n for n in notices)


def test_touches_required_off_launches_a_no_touches_issue(sim_factory):
    # The false branch documented: a no-touches issue on a touches_required:false repo launches
    # normally (relaxed) — the enforcement is exactly the knob, nothing more.
    sim = sim_factory(touches_required=False)
    num = sim.add_issue(title="No touches, relaxed repo", touches="",
                        scenario={"scenario": "happy"})
    sid = "i%d" % num
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") in ("running", "gating",
                                                                        "merged")), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "in-progress" in sim.issue(num)["labels"] or sim.loop_issue(sid)["status"] == "merged"
    assert not [m for m in sim.mutations("comment") if "touches_required" in m["body"]]


# =====================================================================================
# bounce: the worker writes ONLY the BOUNCED: marker; the RUNNER does comment + labels
# =====================================================================================

def test_bounce_runner_posts_memo_and_moves_labels(sim_factory):
    sim = sim_factory()
    memo = ("BOUNCED: the premise is stale — the target module shipped differently.\n\n"
            "Proposed amendment: retarget the Goal at src/newmod and drop DoD item 2.")
    num = sim.add_issue(title="Stale premise", scenario={"scenario": "bounce", "memo": memo})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid))

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "bounced")

    # the memo reached the issue VERBATIM, quoted by the runner
    bounce_comments = [m for m in sim.mutations("comment") if memo in m["body"]]
    assert bounce_comments, "the worker's memo must be posted verbatim by the RUNNER"
    assert "needs-owner" in sim.issue(num)["labels"]
    assert "in-progress" not in sim.issue(num)["labels"]
    # the runner consumed the marker; NO answerer was ever hired for a bounce
    assert not os.path.exists(os.path.join(sim.home, "state", "blocked", sid))
    assert len(sim.surfaces()) == 1, "a bounce must not hire an answerer session"
    assert any("bounced" in ln for ln in sim.notify_lines())
    assert not sim.mutations("merge_pr")


# =====================================================================================
# issue #108: the exact 2026-07-13 incident — bounce + a missing needs-owner label +
# an external close mid-storm -> one notify total, terminal settle, no storm
# =====================================================================================

def test_bounce_missing_label_texts_once_then_external_close_absorbs(sim_factory):
    sim = sim_factory()
    memo = ("BOUNCED: already fixed by #61 — the module this issue targets shipped differently.\n\n"
            "Proposed amendment: close as duplicate.")
    num = sim.add_issue(title="Stale premise", scenario={"scenario": "bounce", "memo": memo})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid))
    # the needs-owner label did NOT exist in the repo (the incident): every bounce label move fails.
    # Target ONLY the bounce's `--add-label needs-owner` argv, so the launch's in-progress add is
    # untouched.
    sim.fail_next("--add-label needs-owner", times=99,
                  stderr="could not add label: 'needs-owner' not found")
    # ride the storm a few ticks: the label never lands, so decide re-derives the bounce each tick
    for _ in range(4):
        sim.tick()
    # ...but the owner was texted EXACTLY ONCE (notify-once per bounce), never once per tick
    assert len([ln for ln in sim.notify_lines() if "bounced" in ln]) == 1, sim.notify_lines()
    # ...and the verbatim memo comment posted exactly once (never 21 duplicates)
    assert len([m for m in sim.mutations("comment") if "already fixed by #61" in m["body"]]) == 1
    assert sim.loop_issue(sid).get("status") != "bounced"     # label never landed -> not settled
    # the owner presses Drop mid-storm: the issue is closed on GitHub
    def close_it(st):
        st["issues"][str(num)]["state"] = "closed"
    sim.edit_gh_state(close_it)
    # within a poll the loop ABSORBS the close: settles terminal, stands the episode down
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    texts_after_absorb = len(sim.notify_lines())
    labels_after_absorb = len(sim.mutations("set_labels"))
    for _ in range(3):
        sim.tick()
    # stood down: no more texts, no more label writes, the blocked marker is consumed
    assert len(sim.notify_lines()) == texts_after_absorb, sim.notify_lines()
    assert len(sim.mutations("set_labels")) == labels_after_absorb
    assert not os.path.exists(os.path.join(sim.home, "state", "blocked", sid))
    # the whole incident produced EXACTLY ONE owner text about this bounce
    assert len([ln for ln in sim.notify_lines() if "bounced" in ln]) == 1, sim.notify_lines()


def test_external_close_of_a_parked_issue_absorbs_and_concludes(sim_factory):
    # the dashboard-lingering half of #108: a terminally-parked issue that the owner then closes on
    # GitHub must be ABSORBED to a concluded terminal state, so the flight leaves the field — not
    # linger forever as a stale "awaiting your call".
    sim = sim_factory()
    num = sim.add_issue(title="Widget without review",
                        scenario={"scenario": "no-review", "linger": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    # the owner closes it on GitHub (Drop)
    def close_it(st):
        st["issues"][str(num)]["state"] = "closed"
    sim.edit_gh_state(close_it)
    labels_before = len(sim.mutations("set_labels"))
    texts_before = len(sim.notify_lines())
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    # stood down: no label churn, no new texts after the absorb
    for _ in range(3):
        sim.tick()
    assert len(sim.mutations("set_labels")) == labels_before
    assert len(sim.notify_lines()) == texts_before


# =====================================================================================
# blocked -> a real answerer session -> answer nudged in -> resumed -> merged
# =====================================================================================

def test_blocked_answerer_roundtrip_resumes_and_merges(sim_factory):
    sim = sim_factory()
    question = "Should the simulated widget use approach A or B? One line is enough."
    num = sim.add_issue(title="Blocks on a question",
                        scenario={"scenario": "blocked", "question": question})
    sid = "i%d" % num
    sim.set_scenario("a1", {"scenario": "answerer",
                            "answer": "Use approach A — it matches the existing pattern."})
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid))

    # the answerer is HIRED through the same delivery-verified stack, in the answers dir
    assert sim.tick_until(
        lambda: os.path.exists(os.path.join(sim.home, "answers", "%s.md" % sid)), ticks=10), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert os.path.exists(os.path.join(sim.home, "briefs", "a1.md"))
    assert not os.path.isdir(os.path.join(sim.home, "worktrees", "a1")), \
        "an answerer launches --cwd, never a worktree"

    # answer delivered into the worker pane; marker cleared; the worker resumes and ships
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    deliveries = [s for s in sim.sends()
                  if "Answer from a fresh answerer" in s.get("text", "")]
    assert deliveries and "approach A" in deliveries[0]["text"]
    assert not os.path.exists(os.path.join(sim.home, "state", "blocked", sid))
    assert len(sim.mutations("merge_pr")) == 1
    # the answerer session was a REAL second launch (worker tab + answerer tab)
    assert len(sim.surfaces()) == 2


def test_answerer_park_escalates_to_william(sim_factory):
    # the answerer refuses to guess: a PARK: answer parks the issue needs-owner with the
    # question quoted in the memo.
    sim = sim_factory()
    question = "May I spend money on a paid API for this?"
    num = sim.add_issue(title="Owner question",
                        scenario={"scenario": "blocked", "question": question})
    sid = "i%d" % num
    sim.set_scenario("a1", {"scenario": "answerer-park"})
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william")
    assert "needs-owner" in sim.issue(num)["labels"]
    memos = [m for m in sim.mutations("comment") if question in m["body"]]
    assert memos, "the park memo must quote the worker's question"
    assert not sim.mutations("merge_pr")


def test_answerer_timeout_parks_with_question(sim_factory):
    # a hired answerer that never writes its file: the 15-minute (virtual) freeze tier is its
    # timeout — the issue parks with the question quoted, never wedges the lane forever.
    sim = sim_factory()
    question = "What color should the widget be?"
    num = sim.add_issue(title="Silent answerer",
                        scenario={"scenario": "blocked", "question": question})
    sid = "i%d" % num
    sim.set_scenario("a1", {"scenario": "answerer-silent"})
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked",
                          ticks=15, advance=181)     # ~15 virtual minutes pass quickly
    memos = [m for m in sim.mutations("comment") if "timed out" in m["body"]]
    assert memos and question in memos[0]["body"]


# =====================================================================================
# investigate: marker comment + parent:-linked child; parent closed; child WAITS
# =====================================================================================

def test_investigate_marker_and_child_parent_closed_child_waits(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Why is the nightly flaky?",
                        labels=("type:investigate", "agent-ready"),
                        scenario={"scenario": "investigate"})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.issue(num)["state"] == "closed"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # the marker comment is the mechanical completion signal (cross-review C1)
    assert any(c["body"].startswith("<!-- superlooper-investigation -->")
               for c in sim.issue(num)["comments"])
    # the child: parent-linked, needs-owner, NOT agent-ready — and never launched
    children = [i for i in sim.gh_state()["issues"].values()
                if "parent: #%d" % num in i["body"]]
    assert len(children) == 1
    assert "needs-owner" in children[0]["labels"]
    assert "agent-ready" not in children[0]["labels"]
    sim.tick()
    sim.tick()
    assert len(sim.surfaces()) == 1, "the child must WAIT for William, never auto-launch"
    assert not sim.mutations("merge_pr"), "an investigation opens zero PRs"


def test_investigate_missing_marker_nudges_then_parks(sim_factory):
    # report exists but the marker comment is missing -> one nudge, then park (§C.4).
    sim = sim_factory()
    num = sim.add_issue(title="Investigation without a report comment",
                        labels=("type:investigate", "agent-ready"),
                        scenario={"scenario": "investigate", "skip_marker": True,
                                  "skip_child": True, "linger": True})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked")
    assert sim.issue(num)["state"] == "open", "an unproven investigation must not close"
    nudges = [s for s in sim.sends() if "investigation" in s.get("text", "")]
    assert len(nudges) == 1


# =====================================================================================
# silent death: exited with no markers -> relaunch ladder -> parked at the retry cap
# =====================================================================================

def test_silent_exit_relaunch_ladder_parks_at_cap(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Vanishes silently", scenario={"scenario": "vanish"})
    sid = "i%d" % num

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked", ticks=20), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # retry_cap=2: launch + 2 relaunches = 3 delivered launches, then park
    ist = sim.loop_issue(sid)
    assert ist.get("launches") == 3 and ist.get("retries") == 2, ist
    memos = [m for m in sim.mutations("comment") if "relaunched" in m["body"]]
    assert memos, sim.mutations("comment")
    assert "parked" in sim.issue(num)["labels"]
    assert any("parked" in ln for ln in sim.notify_lines())


# =====================================================================================
# launch anchor liveness (#24): a dead launch anchor must never walk the queue
# =====================================================================================

def test_dead_launch_anchor_alerts_once_and_never_walks_the_queue(sim_factory, monkeypatch):
    # The 2026-07-09 incident, end to end. Four approved issues are queued; then the runner's launch
    # anchor (its cmux pane) stops resolving mid-run — the tab was dragged to another cmux window, so
    # fake-cmux's list-pane-surfaces resolves only a DIFFERENT pane. The old per-issue cap walked all
    # four into parks + notifies; the fix detects a RUNNER-level fault: ONE alert + notify, ZERO
    # parks, the queue left fully intact (every issue keeps agent-ready), across many ticks.
    sim = sim_factory()
    nums = [sim.add_issue(title="Queued %d" % i) for i in range(4)]
    monkeypatch.setenv("FAKE_CMUX_GOOD_PANE", "99999999-dead-dead-dead-999999999999")
    for _ in range(3):                                 # several ticks — the queue must NOT walk
        sim.tick()

    assert sim.journal("launch") == []                 # not one launch attempted
    assert sim.journal("park") == []                   # ZERO parks
    assert len(sim.journal("alert")) == 1              # exactly ONE alert (deduped across ticks)
    assert len(sim.notify_lines()) == 1                # exactly ONE notify — the alert itself
    assert "launch anchor gone" in sim.notify_lines()[0]
    alert = json.load(open(os.path.join(sim.home, "state", "ALERT")))
    assert alert["reasons"] == ["launch_anchor_down"]
    for num in nums:                                   # every issue kept agent-ready, unclaimed
        assert "agent-ready" in sim.issue(num)["labels"]
        assert "in-progress" not in sim.issue(num)["labels"]
    assert sim.mutations("set_labels") == []           # not one label moved on any issue


def test_launches_resume_after_the_anchor_resolves(sim_factory, monkeypatch):
    # DoD #2: once the pane resolves again the held queue launches with NO relabeling — recovery
    # needs no William touch, because agent-ready was never stripped.
    sim = sim_factory()
    num = sim.add_issue(title="Resume me")
    sid = "i%d" % num
    monkeypatch.setenv("FAKE_CMUX_GOOD_PANE", "99999999-dead-dead-dead-999999999999")
    sim.tick()
    assert sim.loop_issue(sid).get("status") != "running"   # held while the anchor is gone
    assert "agent-ready" in sim.issue(num)["labels"]

    monkeypatch.delenv("FAKE_CMUX_GOOD_PANE", raising=False)  # the tab is back in its own window
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "running"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "in-progress" in sim.issue(num)["labels"]
    assert "agent-ready" not in sim.issue(num)["labels"]


# =====================================================================================
# hard anti-affinity: overlapping declared touches NEVER build concurrently
# =====================================================================================

def test_hard_affinity_overlapping_issues_run_sequentially(sim_factory):
    sim = sim_factory(areas={"a": ["src/a/*"], "b": ["src/b/*"]})
    n1 = sim.add_issue(title="First in area a", touches="a",
                       scenario={"scenario": "happy",
                                 "edit": {"file": "src/a/one.txt", "append": "one\n"}})
    n2 = sim.add_issue(title="Second in area a", touches="a",
                       scenario={"scenario": "happy",
                                 "edit": {"file": "src/a/two.txt", "append": "two\n"}})
    s1, s2 = "i%d" % n1, "i%d" % n2

    sim.tick()
    # lanes=2 but the touches overlap under hard affinity: ONLY the first launches
    assert sim.loop_issue(s1).get("status") == "running"
    assert sim.loop_issue(s2).get("status") is None, "overlapping issue must be HELD"
    assert len(sim.surfaces()) == 1

    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged"
                          and sim.loop_issue(s2).get("status") == "merged", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    # the second launched only AFTER the first's lane freed (report landed): the journal
    # shows its launch strictly after i1's finished event
    launches = [r for r in sim.journal("launch") if r.get("outcome") == "ok"]
    assert [r["id"] for r in launches] == [s1, s2]
    fin1 = [r["ts"] for r in self_events(sim, s1, "session_finished")]
    assert fin1 and launches[1]["ts"] >= fin1[0]
    assert len(sim.mutations("merge_pr")) == 2


def test_finished_issue_holds_declared_territory_until_merge(sim_factory):
    sim = sim_factory(areas={"submission": ["src/shared.txt"]}, lanes=2, affinity="hard")
    line3 = {"file": "src/shared.txt", "line": 3}
    n1 = sim.add_issue(title="i160 finished at gate", touches="submission",
                       scenario={"scenario": "happy",
                                 "edit": dict(line3, text="i160 finished version")})
    n2 = sim.add_issue(title="i163 overlapping candidate", touches="submission",
                       scenario={"scenario": "happy",
                                 "edit": dict(line3, text="i163 later version")})
    s1, s2 = "i%d" % n1, "i%d" % n2

    # Hold the finished PR in the gate-wait window; this is where the incident launched i163.
    sim.edit_gh_state(lambda st: st.update(pr_check_conclusion="PENDING"))
    sim.tick()
    assert sim.loop_issue(s1).get("status") == "running"
    assert sim.loop_issue(s2).get("status") is None
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s1))
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "gating", ticks=5)

    sim.tick()
    assert sim.loop_issue(s1).get("status") == "gating"
    assert sim.loop_issue(s2).get("status") is None, \
        "overlapping candidate must stay held while the finished PR waits at the gate"
    assert [r["id"] for r in sim.journal("launch") if r.get("outcome") == "ok"] == [s1]

    sim.edit_gh_state(lambda st: st.update(pr_check_conclusion="SUCCESS"))
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged", ticks=10), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "i160 finished version"

    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "merged", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    launches = [r for r in sim.journal("launch") if r.get("outcome") == "ok"]
    merges = [r for r in sim.journal("merge") if r.get("outcome") == "ok"]
    assert [r["id"] for r in launches] == [s1, s2]
    assert launches[1]["ts"] > merges[0]["ts"], "i163 launches only after i160 merges"
    assert not sim.journal("regenerate")
    assert len(sim.mutations("merge_pr")) == 2
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "i163 later version"


def self_events(sim, sid, etype):
    return [r for r in sim.journal("event")
            if r.get("event", {}).get("type") == etype and r.get("event", {}).get("id") == sid]


# =====================================================================================
# touch verification (§C.4 step 3): a wander into a live lane's area HOLDS the merge
# =====================================================================================

def test_actual_overlap_with_inflight_lane_holds_merge_until_lane_resolves(sim_factory):
    sim = sim_factory(areas={"a": ["src/a/*"], "b": ["src/b/*"]})
    sync = sim.tmp / "release-i1"
    n1 = sim.add_issue(title="Slow lane in a", touches="a",
                       scenario={"scenario": "happy", "wait_for": str(sync),
                                 "edit": {"file": "src/a/one.txt", "append": "one\n"}})
    n2 = sim.add_issue(title="Declares b, wanders into a", touches="b",
                       scenario={"scenario": "happy",
                                 "edit": {"file": "src/a/wander.txt", "append": "oops\n"}})
    s1, s2 = "i%d" % n1, "i%d" % n2

    sim.tick()   # both launch: declared touches are disjoint
    assert sim.loop_issue(s1).get("status") == "running"
    assert sim.loop_issue(s2).get("status") == "running"

    # i2 finishes first (i1 is held by the sync file) — its ACTUAL diff is in area a
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "holding")
    holds = [r for r in sim.journal("hold") if r.get("id") == s2]
    assert holds and holds[0].get("overlap_lane") == s1
    assert holds[0].get("wander") is True, "declared b, touched a — the wander must be journaled"
    assert not sim.mutations("merge_pr"), "the merge must HOLD while the overlapped lane is live"

    # release i1 -> it merges -> the hold lifts -> i2 merges too
    sync.write_text("go")
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged"
                          and sim.loop_issue(s2).get("status") == "merged", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert len(sim.mutations("merge_pr")) == 2


# =====================================================================================
# the conflict ladder (§C.4 step 6): REAL same-line conflict -> mechanical merge-update
# genuinely conflicts -> regenerate (M1 hygiene) -> second conflict -> park at the cap
# =====================================================================================

def test_conflict_regenerate_then_park_at_cap_branches_preserved(sim_factory):
    sim = sim_factory(affinity="soft")     # let the same-line racers co-schedule
    sync1, sync2 = sim.tmp / "sync1", sim.tmp / "sync2"
    line3 = {"file": "src/shared.txt", "line": 3}
    n1 = sim.add_issue(title="A takes line three",
                       scenario={"scenario": "happy", "edit": dict(line3, text="A version")})
    n2 = sim.add_issue(title="B races line three",
                       scenario={"scenario": "conflict",
                                 "edit": dict(line3, text="B version"),
                                 "wait_for": str(sync1),
                                 "gen1": {"edit": dict(line3, text="B rebuild version"),
                                          "wait_for": str(sync2)}})
    n3 = sim.add_issue(title="C takes line three too", labels=("type:build",),  # not yet approved
                       scenario={"scenario": "happy", "edit": dict(line3, text="C version")})
    s1, s2 = "i%d" % n1, "i%d" % n2

    # both racers launch; A finishes and merges while B (PR already open) holds pre-report
    sim.tick()
    assert sim.loop_issue(s1).get("status") == "running"
    assert sim.loop_issue(s2).get("status") == "running"
    # soft affinity allowed the undeclared-touches overlap but FLAGGED it for the journal
    assert all(r.get("soft_overlap") for r in sim.journal("launch")), sim.journal("launch")
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged")
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "A version"

    # release B -> its PR is now GENUINELY conflicting -> mechanical merge-update REALLY
    # conflicts in the worktree -> regenerate
    b_branch_0 = sim.loop_issue(s2).get("branch")
    b_tip_0 = sim.origin_tip(b_branch_0)
    assert b_tip_0, "B pushed its branch before blocking on the sync file"
    sync1.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "ready", ticks=10), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # M1 relaunch hygiene, checked at the regenerate EDGE (before the rebuild launches):
    # the stale report and worktree are GONE, so they cannot false-gate the rebuilt run
    regen = [r for r in sim.journal("regenerate") if r.get("id") == s2]
    assert len(regen) == 1 and regen[0]["outcome"] == "ok"
    assert not os.path.exists(os.path.join(sim.home, "reports", "%s.md" % s2))
    assert not os.path.isdir(os.path.join(sim.home, "worktrees", s2))
    assert sim.loop_issue(s2).get("branch", "").endswith("-r1"), \
        "the rebuild must get a FRESH suffixed branch (no force path exists)"
    # the superseded PR: labeled, commented, left OPEN, branch PRESERVED on the remote
    pr_b0 = [p for p in sim.prs_for() if p["headRefName"] == b_branch_0][0]
    assert "superseded" in pr_b0["labels"] and pr_b0["state"] == "OPEN"
    assert sim.origin_tip(b_branch_0) == b_tip_0, "the superseded branch must keep its work"

    # the rebuild launches on -r1 (fresh from CURRENT dev) and opens a NEW PR
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "running", ticks=10)
    assert sim.tick_until(
        lambda: any(p["headRefName"].endswith("-r1") for p in sim.prs_for()), ticks=10)

    # now C (approved late) merges line three AGAIN while the rebuild is still working...
    sim.edit_gh_state(
        lambda st: st["issues"][str(n3)]["labels"].append("agent-ready"))
    assert sim.tick_until(lambda: sim.issue(n3)["state"] == "closed", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # ...so releasing the rebuild produces the SECOND real conflict -> conflict cap -> William
    sync2.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "needs_william",
                          ticks=15), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "needs-owner" in sim.issue(n2)["labels"]
    memos = [m for m in sim.mutations("comment") if "conflict cap" in m["body"]]
    assert memos, sim.mutations("comment")
    # exactly ONE regenerate ever (the cap parks, it never loops); both B branches preserved
    assert len([r for r in sim.journal("regenerate") if r.get("id") == s2]) == 1
    assert sim.origin_tip(b_branch_0) == b_tip_0
    b_branch_1 = sim.loop_issue(s2).get("branch")
    assert sim.origin_tip(b_branch_1), "the rebuild's branch must also survive on the remote"
    assert not [m for m in sim.mutations("merge_pr")
                if str(pr_b0["number"]) == m["num"]], "a superseded PR must never merge"
    assert any("needs-owner" in ln for ln in sim.notify_lines())


def test_preserve_labeled_pr_resolved_in_place_never_regenerated(sim_factory):
    # §C.4 step 6c: William marks a conflicted PR `preserve` -> instead of regenerating, the
    # runner hires a conflict-resolution SESSION into the PR's own branch; every gate re-runs
    # on the resolved head and the SAME branch merges. No supersede, no fresh branch.
    sim = sim_factory(affinity="soft")
    sync = sim.tmp / "sync-preserve"
    line3 = {"file": "src/shared.txt", "line": 3}
    n1 = sim.add_issue(title="A lands first",
                       scenario={"scenario": "happy", "edit": dict(line3, text="A version")})
    n2 = sim.add_issue(title="B is precious",
                       scenario={"scenario": "conflict",
                                 "edit": dict(line3, text="B version"),
                                 "wait_for": str(sync),
                                 "resolution": {"file": "src/shared.txt",
                                                "content": "line 1\nline 2\nA and B merged\n"
                                                           "line 4\nline 5\n"}})
    s1, s2 = "i%d" % n1, "i%d" % n2

    sim.tick()
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged")
    b_branch = sim.loop_issue(s2).get("branch")

    # William's judgment: this PR's history is worth preserving
    def mark_preserve(st):
        for p in st["prs"].values():
            if p["headRefName"] == b_branch:
                p["labels"].append("preserve")
    sim.edit_gh_state(mark_preserve)

    sync.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "merged", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]

    # resolved IN PLACE: a resolution session ran, nothing regenerated, the branch survived
    assert [r for r in sim.journal("resolve_conflict") if r.get("outcome") == "ok"]
    assert not sim.journal("regenerate"), "preserve replaces regenerate — never both"
    assert sim.loop_issue(s2).get("branch") == b_branch, "the preserved branch is THE branch"
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "A and B merged"
    pr_b = [p for p in sim.prs_for() if p["headRefName"] == b_branch][0]
    assert pr_b["state"] == "MERGED" and "superseded" not in pr_b["labels"]
    # the re-run gate demanded FRESH review evidence of the resolved diff
    assert sum(1 for c in pr_b["comments"]
               if c["body"].startswith("<!-- superlooper-review -->")) >= 2


# =====================================================================================
# Task-16 live-run regressions (D3/D4): the two live realities the offline sim was
# structurally blind to. D3: fake-gh answers synchronously, so the cached PR view was
# never stale relative to worker completion — modeled here by gating on sub-90s ticks
# (the poll throttle holds the cache pre-PR; only the finishing-issue refresh can see
# the PR). D4: fake-claude EXITED when done, freeing the worker singleton — a real
# interactive claude idles at the prompt holding it; modeled with linger:true on the
# finished generation, on BOTH same-id relaunch paths (regenerate + preserve/resolve).
# =====================================================================================

def _wait_dead(pid, timeout=5):
    """Bounded wait for a process to be gone (close-surface kills asynchronously)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.05)
    return False


def test_pr_opened_between_polls_merges_never_false_parks(sim_factory):
    # D3 (live dry-run 2026-07-03, fix 744cd5a): a worker that finished AND opened its PR
    # inside the 90s poll window was gated against the stale, pre-PR snapshot and
    # false-parked on "no PR exists" (fast worker #1 parked live; slow worker #2 merged).
    sim = sim_factory()
    num = sim.add_issue(title="Fast finisher", scenario={"scenario": "happy"})
    sid = "i%d" % num

    sim.tick()                     # advance 91: polls a pre-PR view, launches the worker
    assert sim.loop_issue(sid).get("status") == "running", sim.journal()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid)), \
        "worker never finished: %s" % sim.journal()

    # The worker's PR now EXISTS on (fake) GitHub, but the runner's cached view is still
    # pre-PR — and every tick below stays inside the poll throttle (5 x 16s < 90s), so a
    # re-poll can never bail the gate out. Only _refresh_finishing_prs can see the PR.
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged",
                          ticks=5, advance=16), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert not sim.journal("park"), \
        "a finished issue must never false-park on a stale pre-PR snapshot"
    assert len(sim.mutations("merge_pr")) == 1


def test_out_of_band_merge_of_an_inflight_lane_is_absorbed_never_stalls(sim_factory):
    """Issue #155 / i328, against REAL ticks. The worker has pushed and opened its PR and is STILL
    BUILDING (held on the sync file, no report) when the PR is merged OUT OF BAND — which, exactly
    like the real service, squash-merges into origin/main and CLOSES the issue via `Closes #N`.

    That close is the trap: it drops the issue from BOTH open-issue lists the poll's want-set is
    built from, and a building lane reaches that want-set only through its `in-progress` LABEL,
    read off those very lists. So the lookup stopped happening at the moment it began to matter —
    `pr` stayed null, the lane held its slot, and the queue behind it waited two hours. The
    per-tick reconcile must absorb the merge instead: settle to merged, free the lane, never park.
    """
    sim = sim_factory()
    sync = sim.tmp / "hold-the-worker"          # the worker waits here: PR open, report unwritten
    num = sim.add_issue(title="Merged out from under a building lane",
                        scenario={"scenario": "happy", "wait_for": str(sync)})
    sid = "i%d" % num

    sim.tick()
    assert sim.loop_issue(sid).get("status") == "running", sim.journal()

    # the worker really opened its PR on (fake) GitHub, and is still building behind it
    assert sim.tick_until(lambda: sim.prs_for()), \
        "worker never opened a PR: %s" % sim.journal()
    pr = sim.prs_for()[0]
    assert pr["state"] == "OPEN"
    assert not os.path.exists(os.path.join(sim.home, "reports", "%s.md" % sid))   # not finished
    assert sim.loop_issue(sid).get("status") == "running"                         # in flight

    # ---- somebody merges the PR by hand, outside the loop ----
    _run([os.path.join(FAKES, "fake-gh"), "pr", "merge", str(pr["number"]), "--squash"])
    assert sim.prs_for()[0]["state"] == "MERGED"
    assert sim.issue(num)["state"] == "closed"       # ...and GitHub closed the issue (Closes #N)
    merges = len(sim.mutations("merge_pr"))          # == 1: the hand-merge just above, not the loop's

    # ---- the runner must learn this and settle, within a tick or two ----
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged", ticks=3), \
        "never absorbed the out-of-band merge: %s" % [
            (r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert [r for r in sim.journal("absorb_merged") if r.get("outcome") == "ok"]
    assert not sim.journal("park"), "a lane whose PR merged out of band must never park"
    assert len(sim.mutations("merge_pr")) == merges, \
        "the runner must absorb an already-merged PR, never re-merge it"
    assert "in-progress" not in sim.issue(num)["labels"]
    assert not os.path.isdir(os.path.join(sim.home, "worktrees", sid))   # lane freed, worktree gone

    sync.write_text("go\n")                          # release the held worker for teardown


def test_review_marker_after_pr_cached_still_merges(sim_factory):
    # D6 (live 2026-07-04): a `<!-- superlooper-review -->` marker comment posted AFTER the poll
    # first cached the PR was invisible to the gate until the next 90s poll — and the gate
    # nudged THEN parked within that stale window, false-parking completed, properly-reviewed
    # work (#9 parked while its identical twin #10 merged — decided by timing alone). The
    # finishing refresh now re-fetches comments for a finished, still-OPEN PR that has no review
    # evidence yet, so a late marker reaches the gate before it parks.
    sim = sim_factory()
    sync = sim.tmp / "sync-review-late"
    num = sim.add_issue(title="Reviews after the PR is cached",
                        scenario={"scenario": "review-late", "wait_for": str(sync),
                                  "linger": True})
    sid = "i%d" % num

    sim.tick()                                 # advance 91: launch
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid)), \
        "worker never opened its PR + wrote its report: %s" % sim.journal()

    # the PR is cached WITHOUT the review (not posted yet); the gate spends its one review nudge.
    # tick_until returns the instant the nudge lands — BEFORE the next tick would park — so the
    # issue is still gating here, not parked.
    assert sim.tick_until(lambda: "review" in (sim.loop_issue(sid).get("nudged") or []),
                          ticks=3), \
        "the gate should nudge once for the missing review: %s" % \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.loop_issue(sid).get("status") != "parked"

    # the marker lands NOW. Wait (bounded, real time) until it is actually on fake-gh so the
    # gate's next evaluation can't race ahead of it.
    sync.write_text("go")
    deadline = time.time() + 30
    while time.time() < deadline:
        if any(c["body"].startswith("<!-- superlooper-review -->")
               for p in sim.prs_for() for c in p.get("comments", [])):
            break
        time.sleep(0.05)
    else:
        assert False, "worker never posted the review marker after the sync"

    # each tick advances only 16s, so the 90s poll throttle holds — a normal re-poll can never
    # carry the marker; ONLY the finishing refresh's comment re-fetch can. The discriminator is
    # decisive on tick ONE: PRE-fix the cached PR is skipped and the gate parks (nudge already
    # spent), POST-fix the re-fetched marker reaches the gate and it merges. (Kept at ticks=5 so
    # the whole post-sync window stays < 90s and the assertion can't be rescued by a stray poll.)
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged",
                          ticks=5, advance=16), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert not sim.journal("park"), \
        "properly-reviewed work must never false-park on a stale comment view"
    assert len(sim.mutations("merge_pr")) == 1


def test_regenerate_relaunch_survives_finished_but_alive_worker(sim_factory):
    # D4 (live dry-run 2026-07-03, fix 5112a30), regenerate path: a real claude worker does
    # NOT exit after its report — it idles at the prompt, start-session.sh's EXIT trap never
    # fires, and worker.<id>.lock stays held for the whole process life. The conflict-
    # regeneration relaunch of the SAME id must close the stale pane, free the lock, and
    # deliver on the FIRST attempt (pre-fix: delivery timeout -> false-park at the retry cap).
    sim = sim_factory(affinity="soft")
    sync1 = sim.tmp / "sync-d4"
    line3 = {"file": "src/shared.txt", "line": 3}
    n1 = sim.add_issue(title="A takes line three",
                       scenario={"scenario": "happy", "edit": dict(line3, text="A version")})
    n2 = sim.add_issue(title="B lingers at the prompt",
                       scenario={"scenario": "conflict", "linger": True,
                                 "edit": dict(line3, text="B version"),
                                 "wait_for": str(sync1),
                                 "gen1": {"edit": dict(line3, text="B rebuild version"),
                                          "wait_for": None}})
    s1, s2 = "i%d" % n1, "i%d" % n2

    sim.tick()
    assert sim.loop_issue(s1).get("status") == "running"
    assert sim.loop_issue(s2).get("status") == "running"
    with open(os.path.join(sim.home, "state", "panes", s2)) as f:
        surf0 = f.read().strip()                      # gen0's tab — must be the one closed
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged")

    sync1.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    # THE premise the original sim was blind to: B is FINISHED (report on disk) yet its
    # process is ALIVE and still holds the worker singleton lock
    lock = os.path.join(sim.home, "state", "worker.%s.lock" % s2)
    pid0, alive0 = sim._lock_holder(lock)
    assert pid0 and alive0, "gen0 must linger holding its lock — the D4 premise"

    # conflict -> regenerate -> the SAME-ID relaunch delivers FIRST TRY
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("branch", "").endswith("-r1")
                          and sim.loop_issue(s2).get("status") == "running", ticks=10), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    bad = [r for r in sim.journal("launch") if r.get("id") == s2 and r.get("outcome") != "ok"]
    assert not bad, "the relaunch must not fight the stale lock: %s" % bad
    assert sim.loop_issue(s2).get("launch_failures") in (None, 0)
    # the stale pane was really closed (fake-cmux records it), and the rebuild — which also
    # lingers — owns a FRESH lock
    closed = [json.loads(l)["surface"]
              for l in (sim.cmux_dir / "closed.jsonl").read_text().splitlines() if l.strip()]
    assert surf0 in closed, closed
    assert _wait_dead(pid0), "close-surface must KILL the old tab's process tree, " \
        "not merely record a close (the lingering gen0 session must be gone)"
    pid1, alive1 = sim._lock_holder(lock)
    assert alive1 and pid1 != pid0, "the rebuild must own a fresh singleton, not the corpse's"

    # and the ladder completes: a COMPLETED regeneration-merge on the -r1 branch
    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "merged", ticks=15), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "B rebuild version"


def test_preserve_resolution_survives_finished_but_alive_worker(sim_factory):
    # D4, preserve path (5112a30's second wiring point): the resolve-conflict session
    # relaunches into the id's OWN worktree while the finished-but-alive prior session holds
    # the lock — and unlike regenerate, a failed launch here leaves status 'gating', so
    # pre-fix the runner re-failed the resolve launch FOREVER.
    sim = sim_factory(affinity="soft")
    sync = sim.tmp / "sync-d4-preserve"
    line3 = {"file": "src/shared.txt", "line": 3}
    n1 = sim.add_issue(title="A lands first",
                       scenario={"scenario": "happy", "edit": dict(line3, text="A version")})
    n2 = sim.add_issue(title="B is precious and lingers",
                       scenario={"scenario": "conflict", "linger": True,
                                 "edit": dict(line3, text="B version"),
                                 "wait_for": str(sync),
                                 "resolution": {"file": "src/shared.txt",
                                                "content": "line 1\nline 2\nA and B merged\n"
                                                           "line 4\nline 5\n"}})
    s1, s2 = "i%d" % n1, "i%d" % n2

    sim.tick()
    assert sim.loop_issue(s2).get("status") == "running"
    with open(os.path.join(sim.home, "state", "panes", s2)) as f:
        surf0 = f.read().strip()
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged")
    b_branch = sim.loop_issue(s2).get("branch")

    def mark_preserve(st):
        for p in st["prs"].values():
            if p["headRefName"] == b_branch:
                p["labels"].append("preserve")
    sim.edit_gh_state(mark_preserve)

    sync.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s2))
    lock = os.path.join(sim.home, "state", "worker.%s.lock" % s2)
    pid0, alive0 = sim._lock_holder(lock)
    assert pid0 and alive0, "gen0 must linger holding its lock — the D4 premise"

    assert sim.tick_until(lambda: sim.loop_issue(s2).get("status") == "merged", ticks=30), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    # the resolve session launched cleanly FIRST TRY (no lock fight, no eternal re-fail)...
    resolves = sim.journal("resolve_conflict")
    assert resolves and all(r.get("outcome") == "ok" for r in resolves), resolves
    assert len(resolves) == 1, "one resolve launch — never a retry loop against a stale lock"
    # ...after the stale pane was really closed; and it resolved IN PLACE, never regenerated
    closed = [json.loads(l)["surface"]
              for l in (sim.cmux_dir / "closed.jsonl").read_text().splitlines() if l.strip()]
    assert surf0 in closed, closed
    assert _wait_dead(pid0), "the lingering gen0 session must be gone after close-surface"
    assert not sim.journal("regenerate"), "preserve replaces regenerate — never both"
    assert sim.origin_file("src/shared.txt").splitlines()[2] == "A and B merged"


# =====================================================================================
# fix-forward: red dev required check -> freeze + standing-rule fix issue (ONCE) ->
# building continues under the freeze -> green -> unfreeze -> held merges flow
# =====================================================================================

def test_red_dev_freezes_files_once_builds_continue_green_unfreezes(sim_factory):
    sim = sim_factory()
    # dev goes red BEFORE anything runs — REST check-runs use lowercase conclusions
    sim.edit_gh_state(lambda st: st["branch_checks"].update(
        main=[{"name": "ci", "status": "completed", "conclusion": "failure"}]))
    num = sim.add_issue(title="Builds during the freeze",
                        scenario={"scenario": "happy", "linger": True})
    sid = "i%d" % num

    sim.tick()
    marker = sim.frozen_marker()
    assert marker and marker.get("source") == "dev-check", marker
    # freeze stops MERGES, never builds: the launch happened under the freeze
    assert sim.loop_issue(sid).get("status") == "running", \
        "frozen-but-building is the safe idle state — builds must continue"
    # the standing-rule fix issue, with EXACTLY the owner-defined label set
    fixes = sim.mutations("create_issue")
    assert len(fixes) == 1
    assert fixes[0]["labels"] == "type:diagnose-and-fix,agent-ready,auto-approved:nightly-red,expedite"
    assert any("frozen" in ln for ln in sim.notify_lines())

    # the finished PR HOLDS while frozen (never merges), and the fix issue never re-files
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "holding")
    sim.tick()
    assert not sim.mutations("merge_pr"), "a frozen mainline must hold every merge"
    assert len(sim.mutations("create_issue")) == 1, \
        "one fix issue per distinct breakage (fingerprint dedup)"

    # CI recovers -> unfreeze -> the held merge flows
    sim.edit_gh_state(lambda st: st["branch_checks"].update(main=[dict(c) for c in GREEN_CI]))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.frozen_marker() is None
    assert [r for r in sim.journal("unfreeze") if r.get("outcome") == "ok"]


def test_dev_commit_status_freeze_auto_lifts_when_status_greens(sim_factory):
    # issue #23: the required check reports on the dev branch ONLY as a commit STATUS (a
    # ship-script stamp), never a check-run. A dev poll that read only the REST check-runs API
    # was BLIND to it -> the dev view read pending forever -> a mainline freeze could NEVER
    # auto-lift (a human had to delete merges_frozen.json). The widened dev view (check-runs +
    # commit statuses) sees the ship status go green and lifts the freeze.
    sim = sim_factory(required_checks=("ship",))

    def to_status_only(st):
        st["check_names"] = []                                  # no required CHECK-RUN anywhere
        st["status_names"] = ["ship"]                           # "ship" rides the rollup as a status
        st["branch_checks"] = {"main": []}                      # dev HEAD carries NO check-runs
        st["branch_statuses"] = {"main": [{"context": "ship", "state": "failure"}]}   # red status
    sim.edit_gh_state(to_status_only)

    num = sim.add_issue(title="Builds during the status freeze",
                        scenario={"scenario": "happy", "linger": True})
    sid = "i%d" % num

    sim.tick()
    marker = sim.frozen_marker()
    assert marker and marker.get("source") == "dev-check", marker
    # freeze stops MERGES, never builds: the launch happened under the freeze
    assert sim.loop_issue(sid).get("status") == "running", \
        "frozen-but-building is the safe idle state — builds must continue"

    # the finished PR HOLDS while frozen; a check-runs-only dev view would hold it FOREVER (the
    # ship status is invisible to /check-runs, so the freeze would never auto-lift).
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "holding")
    sim.tick()
    assert not sim.mutations("merge_pr"), "a frozen mainline must hold every merge"

    # the ship STATUS goes green on dev -> the widened dev view sees it -> unfreeze -> merge flows
    sim.edit_gh_state(lambda st: st["branch_statuses"].update(
        main=[{"context": "ship", "state": "success"}]))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert sim.frozen_marker() is None
    assert [r for r in sim.journal("unfreeze") if r.get("outcome") == "ok"]


def test_red_dev_status_stays_frozen_until_green(sim_factory):
    # fail-closed companion to the above: a genuinely red required STATUS on dev freezes and
    # STAYS frozen while it is red — the widened view never fabricates a green.
    sim = sim_factory(required_checks=("ship",))

    def to_red_status(st):
        st["check_names"] = []
        st["status_names"] = ["ship"]
        st["branch_checks"] = {"main": []}
        st["branch_statuses"] = {"main": [{"context": "ship", "state": "failure"}]}
    sim.edit_gh_state(to_red_status)

    sim.tick()
    assert sim.frozen_marker(), "a red required status must freeze"
    # the status stays red across several ticks -> the mainline stays frozen (never auto-lifts)
    for _ in range(3):
        sim.tick()
    assert sim.frozen_marker(), "a still-red status must keep the mainline frozen (fail closed)"
    assert not [r for r in sim.journal("unfreeze") if r.get("outcome") == "ok"], \
        "no unfreeze may fire while the required status is red"


def test_fix_issue_create_blip_retries_without_duplicate(sim_factory):
    # a gh blip on the CREATE of the standing-rule fix issue: the next tick retries; the
    # GitHub-reconcile pass guarantees ONE issue per fingerprint, never two.
    sim = sim_factory()
    sim.edit_gh_state(lambda st: st["branch_checks"].update(
        main=[{"name": "ci", "status": "completed", "conclusion": "failure"}]))
    sim.fail_next("issue create", times=1)
    sim.tick()
    assert not sim.mutations("create_issue"), "the blipped create must not have landed"
    assert sim.tick_until(lambda: len(sim.mutations("create_issue")) == 1, ticks=5)
    sim.tick()
    sim.tick()
    assert len(sim.mutations("create_issue")) == 1, "never a duplicate fix issue"
    red_fixes = [i for i in sim.gh_state()["issues"].values()
                 if "auto-approved:nightly-red" in i["labels"]]
    assert len(red_fixes) == 1


# =====================================================================================
# runner kill -9 -> restart: state rebuilt from GitHub + disk, no duplicate launches,
# no duplicate fix issues, and the dead runner's in-flight work still completes
# =====================================================================================

def kill_dash_nine(sim):
    """A kill -9 leaves runner.lock pointing at a DEAD pid and an in-memory runner that never
    cleaned up. The restarted runner must steal the dead lock and rebuild from GitHub + disk."""
    with open(os.path.join(sim.home, "state", "runner.lock"), "w") as f:
        f.write("99999999")            # no such pid
    sim.runner = sim._make_runner()    # cold: fresh event dedup rebuild, stale gh view
    assert sim.runner.acquire_singleton() is True, "a dead holder's lock must be stolen"


def test_kill9_restart_no_duplicate_launch_and_work_completes(sim_factory):
    sim = sim_factory()
    sync = sim.tmp / "release"
    num = sim.add_issue(title="Survives a runner death",
                        scenario={"scenario": "happy", "wait_for": str(sync)})
    sid = "i%d" % num
    sim.tick()
    assert sim.loop_issue(sid).get("status") == "running"
    launches_before = len(sim.surfaces())

    kill_dash_nine(sim)
    sim.tick()
    sim.tick()
    # the live worker (its singleton lock names a live pid) was NOT relaunched or disturbed
    assert len(sim.surfaces()) == launches_before, \
        "restart must never double-launch a live session (worker singleton + live-lock sweep)"
    assert not [r for r in sim.journal("launch")
                if r.get("id") == sid and r.get("outcome") == "ok"][1:], \
        "exactly one delivered launch for the issue across the restart"

    # the restarted runner finishes the dead runner's job: gate + merge, exactly once
    sync.write_text("go")
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert len(sim.mutations("merge_pr")) == 1


def test_kill9_between_fix_issue_create_and_fingerprint_save_never_duplicates(sim_factory):
    # THE Codex round-1 C3 crash window, at sim level: the fix issue landed on GitHub but the
    # runner died before fix_issues.json recorded the fingerprint. The restarted runner must
    # reconcile from GitHub (the body's fingerprint marker) — never file a second issue.
    sim = sim_factory()
    sim.edit_gh_state(lambda st: st["branch_checks"].update(
        main=[{"name": "ci", "status": "completed", "conclusion": "failure"}]))
    sim.tick()
    assert len(sim.mutations("create_issue")) == 1
    os.remove(os.path.join(sim.home, "state", "fix_issues.json"))   # the lost save

    kill_dash_nine(sim)
    sim.tick()
    sim.tick()
    assert len(sim.mutations("create_issue")) == 1, \
        "the restarted runner must reconcile the filed issue from GitHub, not re-file it"
    filed = json.load(open(os.path.join(sim.home, "state", "fix_issues.json")))
    assert list(filed.values()) and all(isinstance(v, int) for v in filed.values())


def test_kill9_between_merge_and_bookkeeping_absorbs_the_merged_truth(sim_factory):
    # Codex round-1 C2 at sim level: the PR is MERGED on GitHub but the runner died before
    # settling labels/state. The restart must ABSORB the merged fact — no wedge, no re-merge.
    sim = sim_factory()
    num = sim.add_issue(title="Merged then crashed", scenario={"scenario": "happy"})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged")

    # rewind to the crash window: local state never heard about the merge
    import loopstate as ls
    ls.update(os.path.join(sim.home, "state", "issues.json"),
              lambda st: st["issues"][sid].update(status="gating"))
    sim.edit_gh_state(lambda st: st["issues"][str(num)]["labels"].append("in-progress"))

    kill_dash_nine(sim)
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "merged", ticks=5)
    assert "in-progress" not in sim.issue(num)["labels"]
    assert len(sim.mutations("merge_pr")) == 1, "absorb settles state; it never merges twice"


# =====================================================================================
# the wave-3 poke: an orphaned issue with a PUSHED branch but NO PR. The requeued fresh
# worker hits the push refusal and must block/park — the pushed work is never lost.
# =====================================================================================

def test_orphaned_pushed_branch_no_pr_blocks_and_preserves_remote_work(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Orphaned push", labels=("type:build", "in-progress"),
                        scenario={"scenario": "happy"})
    sid = "i%d" % num
    sim.set_scenario("a1", {"scenario": "answerer-park"})

    # the dead prior worker's legacy: its branch IS on the remote, with real work, and no PR.
    # branch_for(title "Orphaned push") == sl/i1-orphaned-push — the SAME name a requeued
    # fresh worker derives, which is exactly why its plain push must be refused.
    genv = {**os.environ}
    seed = sim.tmp / "orphan-seed"
    _run(["git", "clone", str(sim.origin), str(seed)], env=genv)
    _run(["git", "checkout", "-q", "-b", "sl/i%d-orphaned-push" % num], cwd=seed, env=genv)
    (seed / "precious.txt").write_text("work the dead worker already pushed\n")
    _run(["git", "add", "precious.txt"], cwd=seed, env=genv)
    _run(["git", "commit", "-q", "-m", "the dead worker's pushed work"], cwd=seed, env=genv)
    _run(["git", "push", "-q", "origin", "HEAD"], cwd=seed, env=genv)
    orphan_tip = sim.origin_tip("sl/i%d-orphaned-push" % num)
    assert orphan_tip

    # tick 1: in-progress + no live session + no PR -> reclaim (agent-ready)
    sim.tick()
    assert [r for r in sim.journal("reclaim") if r.get("id") == sid], \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "agent-ready" in sim.issue(num)["labels"]

    # tick 2: the fresh worker launches, builds from current dev on the SAME branch name,
    # and its plain push is REFUSED (no force path exists anywhere) -> it blocks
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "blocked", sid)), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    blocked_q = open(os.path.join(sim.home, "state", "blocked", sid)).read()
    assert "refused" in blocked_q and "force" in blocked_q

    # the answerer recognizes an owner call -> the issue parks needs-owner with the question
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "needs_william"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    assert "needs-owner" in sim.issue(num)["labels"]

    # THE INVARIANT THE POKE EXISTS FOR: the pushed work was never clobbered or lost
    assert sim.origin_tip("sl/i%d-orphaned-push" % num) == orphan_tip
    assert sim.origin_file("precious.txt", ref="sl/i%d-orphaned-push" % num) \
        == "work the dead worker already pushed\n"
    assert not sim.mutations("merge_pr")


# =====================================================================================
# the GitHub-blip contract, tightened by issue #61: a blip mid-park duplicates NOTHING —
# the retried park is silent (memo once per episode, wave-3's "accepted noise" removed)
# and a LABEL transition never duplicates (the original wave-3 invariant, unchanged)
# =====================================================================================

def test_gh_blip_mid_park_duplicates_neither_comment_nor_label_transition(sim_factory):
    sim = sim_factory()
    num = sim.add_issue(title="Park with a blip",
                        scenario={"scenario": "no-pr", "linger": True})
    sid = "i%d" % num
    # the park's label edit blips exactly once; its memo comment goes through
    sim.fail_next("issue edit %d --add-label parked" % num, times=1)
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % sid))
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked")

    memos = [m for m in sim.mutations("comment") if "parked this issue" in m["body"]]
    assert len(memos) == 1, "the retried park must NOT re-comment (notify-once episode, #61)"
    label_moves = [m for m in sim.mutations("set_labels")
                   if m.get("add") and "parked" in m["add"]]
    assert len(label_moves) == 1, "a LABEL transition must never duplicate"
    assert sim.issue(num)["labels"].count("parked") == 1


# =====================================================================================
# the nightly seam (SL_NIGHTLY_WORKTREE): red -> freeze(source:nightly) + fix issue;
# FREEZE OWNERSHIP (wave-4 Codex): a dev-green runner tick must NOT unfreeze a nightly
# freeze — only a green nightly clears it; and vice versa.
# =====================================================================================

QA_SUITE = """\
#!/usr/bin/env bash
mkdir -p results
mode="$(cat '{mode_file}' 2>/dev/null || echo green)"
n=$(( $(cat '{count_file}' 2>/dev/null || echo 0) + 1 ))
echo "$n" > '{count_file}'
fail=0
case "$mode" in
  red) fail=1 ;;
  flake) [ "$n" -eq 1 ] && fail=1 ;;
esac
if [ "$fail" -eq 1 ]; then
  cat > results/results.xml <<'XML'
<testsuite tests="2"><testcase classname="suite" name="test_ok"/><testcase classname="suite" name="test_widget"><failure message="widget broke">assert widget == expected at t=1234</failure></testcase></testsuite>
XML
else
  cat > results/results.xml <<'XML'
<testsuite tests="2"><testcase classname="suite" name="test_ok"/><testcase classname="suite" name="test_widget"/></testsuite>
XML
fi
exit 0
"""


def nightly_sim(sim_factory, tmp_path, monkeypatch):
    """A sim whose repo has a wired (fake) nightly browser suite + the injected worktree seam."""
    mode_file = tmp_path / "qa-mode"
    qa_sh = tmp_path / "qa.sh"
    qa_sh.write_text(QA_SUITE.format(mode_file=mode_file, count_file=tmp_path / "qa-count"))
    qa_sh.chmod(0o755)
    sim = sim_factory(qa={"nightly_cmd": "bash %s" % qa_sh, "results_glob": "results/*.xml"})
    qa_wt = tmp_path / "qa-worktree"
    qa_wt.mkdir()
    monkeypatch.setenv("SL_NIGHTLY_WORKTREE", str(qa_wt))
    return sim, mode_file


def superlooper_cli(sim, *args, check=True):
    return _run([sys.executable, os.path.join(REPO_ROOT, "skill", "bin", "superlooper"),
                 *args, "--repo", str(sim.repo)], check=check)


def test_nightly_red_freezes_with_ownership_dev_green_cannot_unfreeze(sim_factory, tmp_path,
                                                                      monkeypatch):
    sim, mode_file = nightly_sim(sim_factory, tmp_path, monkeypatch)
    mode_file.write_text("red")
    superlooper_cli(sim, "nightly")

    # red nightly: sticky freeze OWNED by the nightly + the standing-rule fix issue + notify
    marker = sim.frozen_marker()
    assert marker and marker.get("source") == "nightly", marker
    fixes = sim.mutations("create_issue")
    assert len(fixes) == 1
    assert fixes[0]["labels"] == "type:diagnose-and-fix,agent-ready,auto-approved:nightly-red,expedite"
    assert any("nightly RED" in ln for ln in sim.notify_lines())
    ln_path = os.path.join(sim.home, "state", "last_nightly.json")
    assert json.load(open(ln_path))["ok"] is True   # parsed fine; failures recorded

    # a second red nightly re-freezes but NEVER re-files (fingerprint dedup, runner-shared)
    superlooper_cli(sim, "nightly")
    assert len(sim.mutations("create_issue")) == 1

    # FREEZE OWNERSHIP: dev checks are green, but runner ticks must HOLD the nightly freeze
    # (the fix issue it filed builds meanwhile — freeze never stops builds)
    sim.tick()
    sim.tick()
    assert sim.frozen_marker() is not None and sim.frozen_marker()["source"] == "nightly", \
        "a dev-green tick must NOT clear a nightly-owned freeze"
    held = [r for r in sim.journal("unfreeze") if str(r.get("outcome", "")).startswith("held")]
    assert held, [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    fix_id = "i%s" % list(json.load(
        open(os.path.join(sim.home, "state", "fix_issues.json"))).values())[0]
    # freeze never stops BUILDS: the auto-approved fix issue launched and worked under the
    # freeze (it may already have finished and be holding at the gate — also correct)
    assert sim.loop_issue(fix_id).get("status") in ("running", "gating", "holding"), \
        sim.loop_issue(fix_id)
    assert [r for r in sim.journal("launch")
            if r.get("id") == fix_id and r.get("outcome") == "ok"]
    assert not sim.mutations("merge_pr"), "nothing may MERGE while frozen"

    # only a GREEN NIGHTLY clears its own freeze; then the held fix PR flows to merge
    mode_file.write_text("green")
    superlooper_cli(sim, "nightly")
    assert sim.frozen_marker() is None, "a green nightly clears the nightly freeze"
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % fix_id))
    assert sim.tick_until(lambda: sim.loop_issue(fix_id).get("status") == "merged"), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]


def test_green_nightly_never_clears_a_dev_check_freeze(sim_factory, tmp_path, monkeypatch):
    sim, mode_file = nightly_sim(sim_factory, tmp_path, monkeypatch)
    import loopstate as ls
    ls.save(os.path.join(sim.home, "state", "merges_frozen.json"),
            {"reason": "dev checks red: ci", "since": 1, "source": "dev-check"})
    mode_file.write_text("green")
    superlooper_cli(sim, "nightly")
    marker = sim.frozen_marker()
    assert marker and marker.get("source") == "dev-check", \
        "a green nightly must clear ONLY a nightly-owned freeze (ownership, wave-4 Codex)"


def test_nightly_flake_fails_once_passes_on_retry_no_freeze_no_issue(sim_factory, tmp_path,
                                                                     monkeypatch):
    sim, mode_file = nightly_sim(sim_factory, tmp_path, monkeypatch)
    mode_file.write_text("flake")
    superlooper_cli(sim, "nightly")
    assert sim.frozen_marker() is None, "a flake must never freeze merges"
    assert not sim.mutations("create_issue"), "a flake must never file an issue"
    rec = [r for r in sim.journal("nightly")][-1]
    assert rec.get("green") is True and rec.get("flakes") == 1, rec


def test_promote_report_wrong_typed_cached_ok_is_never_a_silent_all_clear(sim_factory,
                                                                          tmp_path,
                                                                          monkeypatch):
    # Codex R2 C3 at sim level: a corrupt/hand-edited last_nightly.json whose "ok" is the
    # truthy STRING "false" must render as could-not-parse evidence, never as a green suite.
    sim, _ = nightly_sim(sim_factory, tmp_path, monkeypatch)
    import loopstate as ls
    ls.save(os.path.join(sim.home, "state", "last_nightly.json"),
            {"date": "2026-07-03", "ok": "false", "failures": []})
    superlooper_cli(sim, "promote-report", "--use-latest-nightly")
    date = time.strftime("%Y-%m-%d")
    text = open(os.path.join(sim.home, "reports", "promotion-%s.md" % date)).read()
    assert "Could not parse the suite results" in text
    assert "0 failures" not in text


# =====================================================================================
# the two proven defect classes, poked at simulation level: wrong-TYPED garbage from
# GitHub / disk must land on the safe action — never a crash, never a fail-open
# =====================================================================================

def test_wrong_typed_github_garbage_ticks_safely_and_launches_nothing(sim_factory):
    sim = sim_factory()
    sim.edit_gh_state(lambda st: st["issues"].update({
        # every field wrong-typed, plus the agent-ready label where it can pretend to be work
        "7": {"number": "7", "title": None, "labels": ["type:build", "agent-ready"],
              "body": {"goal": "?"}, "createdAt": 99, "state": "open", "comments": []},
        "8": {"number": True, "labels": 42, "body": None, "state": "open"},
        "9": {"number": 9, "title": "no type at all", "labels": ["agent-ready"],
              "body": "## Loop metadata\nblocked-by: #banana\n", "state": "open",
              "createdAt": "2026-07-01T00:00:09Z", "comments": []},
    }))
    for _ in range(3):
        sim.tick()
    assert not sim.journal("tick_error"), \
        "wrong-typed GitHub garbage must never crash a tick: %s" % sim.journal("tick_error")
    assert not sim.journal("launch"), "unidentifiable/invalid issues must never launch"
    assert len(sim.surfaces()) == 0
    assert not sim.mutations(), "no mutation may be derived from garbage"


def test_corrupt_retry_counter_parks_instead_of_relaunch_looping(sim_factory):
    # the fail-OPEN-on-wrong-TYPED counter class: an explicit null retries (which nothing in
    # this system ever writes) must read as CORRUPTION -> park to William, never as 0 -> an
    # uncapped relaunch loop.
    sim = sim_factory()
    num = sim.add_issue(title="Corrupt counter", scenario={"scenario": "vanish"})
    sid = "i%d" % num
    sim.tick()
    assert sim.wait_file(os.path.join(sim.home, "state", "exited", sid))
    path = os.path.join(sim.home, "state", "issues.json")
    st = json.load(open(path))
    st["issues"][sid]["retries"] = None
    with open(path, "w") as f:
        json.dump(st, f)
    launches_before = len(sim.surfaces())
    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked", ticks=5)
    assert len(sim.surfaces()) == launches_before, "a corrupt counter must never relaunch"
    memos = [m for m in sim.mutations("comment") if "unreadable" in m["body"]]
    assert memos, sim.mutations("comment")


def test_wrong_typed_usage_stops_launches_but_gates_proceed(sim_factory):
    # RC-USAGEFAILOPEN at sim level: usage that is PRESENT but wrong-typed ("low"), with a
    # healthy auth, must stop fresh launches — while an already-finished issue still gates
    # and merges (the failure is contained to the launch decision).
    sim = sim_factory()
    n1 = sim.add_issue(title="Already running", scenario={"scenario": "happy"})
    s1 = "i%d" % n1
    sim.tick()
    assert sim.loop_issue(s1).get("status") == "running"

    sim.runner._fetch_usage = lambda: {"auth_status": "ok", "five_hour_pct": "low",
                                       "seven_day_pct": float("nan")}
    n2 = sim.add_issue(title="Never launches", scenario={"scenario": "happy"})
    s2 = "i%d" % n2
    assert sim.wait_file(os.path.join(sim.home, "reports", "%s.md" % s1))
    assert sim.tick_until(lambda: sim.loop_issue(s1).get("status") == "merged")
    assert sim.loop_issue(s2).get("status") is None, \
        "wrong-typed usage must launch NOTHING (fail closed), even with ok auth"
    assert len(sim.surfaces()) == 1
    assert len(sim.mutations("merge_pr")) == 1, "the gate still flows under a usage outage"


# =====================================================================================
# launch delivery failure (drop mode = shim not installed / keystrokes lost): two
# verified non-deliveries -> park with the shim memo; orphan tabs closed; no liveness
# =====================================================================================

def test_launch_never_delivered_parks_after_cap_with_shim_memo(sim_factory, monkeypatch):
    sim = sim_factory()
    monkeypatch.setenv("CMUX_MODE", "drop")
    monkeypatch.setenv("SL_LAUNCH_VERIFY_SECONDS", "2")
    num = sim.add_issue(title="Keystrokes lost", scenario={"scenario": "happy"})
    sid = "i%d" % num

    assert sim.tick_until(lambda: sim.loop_issue(sid).get("status") == "parked", ticks=5), \
        [(r.get("act"), r.get("outcome")) for r in sim.journal()]
    memos = [m for m in sim.mutations("comment") if "launch shim" in m["body"]]
    assert memos, "the park memo must point at the shim installer"
    assert sim.loop_issue(sid).get("launch_failures") == 2
    # both orphan tabs were CLOSED (no buffered-keystroke time bomb), and no liveness was
    # ever fabricated for a worker that never started
    closed = (sim.cmux_dir / "closed.jsonl")
    assert closed.exists() and len(closed.read_text().splitlines()) == 2
    assert not os.path.exists(os.path.join(sim.home, "state", "activity", sid))
    assert "in-progress" not in sim.issue(num)["labels"]
