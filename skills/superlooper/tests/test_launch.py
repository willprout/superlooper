"""The ONE spawn path (issue #308) — the pre-flight half, and the doorway it hands off to.

Three spawners (runner worker, watchdog debugger, dashboard Fixer's owner tap) plus the revive
path all call ``lib/launch.py``; it is the only thing left that creates a session. These tests
pin what the plan's §9 said must survive the port to the session-host wrapper:

  * the ``^i[0-9]+$`` / ``^d[0-9]+$`` mode guards (a debugger id can never spin up a worktree,
    and an issue id can never take the in-place ``--cwd`` path),
  * the base-ref check with its DISTINCT exit 3,
  * worktree creation under a lock, and the preservation rule (an existing checkout is reused,
    never destroyed),
  * pretrust — herdr does NOT remove the first-run trust dialog,
  * the launch-floor env scrub's launcher-side half: nothing poisonous, and no HERDR_* at all,
    is ever forwarded into a pane,
  * the fence's token wiring: a ``d<N>`` receives the grant, an ``i<N>`` provably never does.

Every edge is injected. No test here resolves git, gh, pretrust or a session host.
"""
import fcntl
import json
import os

import pytest

import launch
import session_host


# --------------------------------------------------------------------------- fakes

class FakeEdges:
    """The launcher's external edges: subprocesses, the clock, and the launch token.

    ``answers`` maps a matched argv fragment to (rc, stdout, stderr). Anything unmatched
    succeeds silently — a test names only the edge it is about.
    """

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.slept = 0.0

    def run(self, argv, timeout=None, cwd=None, env=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, answer in self.answers.items():
            if needle in joined:
                rc, out, err = answer
                return launch.Ran(rc, out, err)
        return launch.Ran(0, "", "")

    def sleep(self, seconds):
        self.slept += seconds

    def token(self):
        return "tok-1"


class FakeHost:
    """A stand-in for the five-verb wrapper. Records the spawn it was asked for."""

    def __init__(self, raises=None):
        self.raises = raises
        self.spawned = []
        self.killed = []

    def spawn(self, name, cwd, env=None, kind="claude", agent_args=(), label=None,
              start_timeout_ms=30000):
        self.spawned.append({"name": name, "cwd": str(cwd), "env": dict(env or {}),
                             "kind": kind, "agent_args": list(agent_args), "label": label,
                             "start_timeout_ms": start_timeout_ms})
        if self.raises is not None:
            raise self.raises
        return session_host.Session(name=name, workspace="w9", tab="w9:t1", pane="w9:p1",
                                    shell_pid=4242, owned=True)

    def kill(self, session):
        self.killed.append(session.name)
        return session_host.Teardown(closed=True)


def _home(tmp_path, iid="i308", branch="sl/i308-x"):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "briefs").mkdir(parents=True, exist_ok=True)
    issues = {}
    if iid.startswith("i"):        # only a WORKER has a queue entry; a d<N> is not a tracked issue
        (home / "briefs" / f"{iid}.md").write_text("do the thing")
        issues[iid] = {"branch": branch, "status": "ready"}
    else:
        (home / "briefs" / f"{iid}.md").write_text("diagnose the wedge")
    (home / "state" / "issues.json").write_text(json.dumps({"issues": issues}))
    return home


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def _spec(tmp_path, iid="i308", **over):
    home = over.pop("home", None) or _home(tmp_path, iid)
    kw = {"id": iid, "run_root": str(home), "repo": str(_repo(tmp_path)),
          "dev_branch": "main", "engine_bin": str(tmp_path / "bin"),
          "expect_gh_login": "loopbot"}
    kw.update(over)
    return launch.Spec(**kw)


def _run(spec, edges=None, host=None, started=True):
    """Drive one launch. By default the in-pane floor stamps its start sentinel."""
    edges = edges if edges is not None else FakeEdges()
    host = host if host is not None else FakeHost()
    if started:
        # start-session.sh stamps this from inside the pane, before the agent starts. The
        # launcher polls for it, so a test that wants a delivered launch pre-stamps it.
        started_dir = os.path.join(spec.run_root, "state", "started")
        os.makedirs(started_dir, exist_ok=True)
        with open(os.path.join(started_dir, f"{spec.id}.{edges.token()}"), "w") as f:
            f.write(edges.token())
    return launch.launch(spec, host=host, edges=edges), edges, host


# --------------------------------------------------------------------------- the mode guards

def test_a_debugger_id_can_never_be_launched_as_a_worker(tmp_path):
    """`^i[0-9]+$` on the worker path. Without it a caller bug routes a d<N> through worktree
    creation and the issue counter — the launcher's own fail-closed-on-wrong-typed-input rule."""
    spec = _spec(tmp_path, iid="d12")
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "worker mode expects an issue id" in result.stderr
    assert host.spawned == [], "nothing may be created for a wrongly-routed id"


def test_a_worker_id_can_never_take_the_in_place_debugger_path(tmp_path):
    """`^d[0-9]+$` on the --cwd path — the symmetric guard. An i<N> here would silently skip
    worktree creation and run the worker in whatever directory the caller passed."""
    spec = _spec(tmp_path, iid="i308", cwd=str(tmp_path))
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "debugger (d<N>) ids only" in result.stderr
    assert host.spawned == []


def test_an_unsafe_id_is_refused_before_anything_is_created(tmp_path):
    spec = _spec(tmp_path)
    spec.id = "i3; rm -rf /"
    result, _edges, host = _run(spec, started=False)
    assert result.rc == launch.ABORTED
    assert "sanitize validation failed" in result.stderr    # evidence.py's identity_invalid needle
    assert host.spawned == []


def test_a_debugger_launches_in_place_with_no_worktree_and_no_branch(tmp_path):
    cwd = tmp_path / "patient"
    cwd.mkdir()
    home = _home(tmp_path, "d12")
    spec = _spec(tmp_path, iid="d12", home=home, cwd=str(cwd))
    result, edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["cwd"] == os.path.realpath(str(cwd))
    assert not any("worktree" in " ".join(c) for c in edges.calls), \
        "the --cwd path creates nothing: no worktree, no branch"


# --------------------------------------------------------------------------- pre-flight: base ref

def test_a_missing_base_ref_exits_3_and_names_the_branch(tmp_path):
    """Issue #28's distinct code. A missing base must never become a hollow launch, and the park
    memo must blame the branch rather than the launch machinery."""
    edges = FakeEdges({"worktree add": (1, "", "fatal: invalid reference"),
                       "rev-parse --verify": (128, "", "unknown revision")})
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.BASE_MISSING
    assert "origin/main" in result.stderr
    assert host.spawned == [], "a missing base costs no pane"


def test_a_git_level_worktree_failure_is_not_a_missing_base(tmp_path):
    """The base EXISTS and creation still failed — a per-issue git fault (evidence.py's
    worktree_create_failed), deliberately distinct from exit 3."""
    edges = FakeEdges({"worktree add": (1, "", "fatal: already checked out")})
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.ABORTED
    assert "could not create the worktree" in result.stderr
    assert host.spawned == []


# --------------------------------------------------------------------------- pre-flight: worktree

def test_worktree_creation_holds_a_lock_for_the_whole_critical_section(tmp_path):
    """c17's salvaged half. Two launches racing on one repo must not both run `worktree add`."""
    spec = _spec(tmp_path)
    result, edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    lock = os.path.join(spec.run_root, "state", "worktree.lock")
    assert os.path.exists(lock), "the worktree critical section takes a real lock file"
    # And it is a REAL exclusive lock, not a marker file: a second holder must wait for it.
    with launch.WorktreeLock(lock) as held:
        assert held._fh is not None
        second = launch.WorktreeLock(lock)
        second._fh = open(lock, "a+")
        with pytest.raises(BlockingIOError):
            fcntl.flock(second._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        second._close()
    assert not any("worktree add" in " ".join(c) for c in edges.calls[-1:]), \
        "the lock is released again — a launch never leaves it held"


def test_an_existing_worktree_is_reused_never_destroyed(tmp_path):
    """The preservation rule (#190/#168): a launch only ever CREATES a missing worktree. Its
    committed and uncommitted work is never in the launcher's hands."""
    spec = _spec(tmp_path)
    wt = os.path.join(spec.run_root, "worktrees", spec.id)
    os.makedirs(wt)
    with open(os.path.join(wt, "wip.txt"), "w") as f:
        f.write("unpushed work")
    result, edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert os.path.exists(os.path.join(wt, "wip.txt")), "the launcher never touches existing work"
    assert not any("worktree add" in " ".join(c) for c in edges.calls)
    assert not any("worktree remove" in " ".join(c) for c in edges.calls)


# --------------------------------------------------------------------------- pre-flight: pretrust

def test_every_launch_pretrusts_its_folder(tmp_path):
    """herdr does NOT remove the first-run trust dialog — the supervised run hit one and the host
    classified the blocked pane `idle`. So S9 pre-trust stays load-bearing on the new path."""
    spec = _spec(tmp_path)
    result, edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust, "no pretrust ran"
    assert pretrust[0][1] == os.path.join(spec.run_root, "worktrees", spec.id)


def test_the_debugger_path_pretrusts_the_repo_it_was_pointed_at(tmp_path):
    cwd = tmp_path / "patient"
    cwd.mkdir()
    spec = _spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(cwd))
    result, edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust and pretrust[0][1] == os.path.realpath(str(cwd))


# --------------------------------------------------------------------------- pre-flight: identity

def test_the_runners_own_dead_gh_refuses_before_any_pane_exists(tmp_path):
    """rc=5, the CHANNEL half of #299: no queued issue caused it and none can fix it."""
    spec = _spec(tmp_path, expect_gh_login=None)
    edges = FakeEdges({"api user": (1, "", "not logged in")})
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.AUTH_DEAD_RUNNER
    assert "gh auth dead (runner env)" in result.stderr.lower()
    assert host.spawned == []


def test_a_missing_brief_refuses_before_any_pane_exists(tmp_path):
    spec = _spec(tmp_path)
    os.remove(os.path.join(spec.run_root, "briefs", f"{spec.id}.md"))
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "missing brief" in result.stderr
    assert host.spawned == []


def test_a_resume_takes_the_resume_brief_and_never_falls_back(tmp_path):
    """#298: silently substituting the lane's original brief would deliver the whole issue brief
    as a NEW instruction into a conversation that already built it."""
    spec = _spec(tmp_path, resume_session_id="11111111-2222-3333-4444-555555555555")
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "missing brief" in result.stderr
    with open(os.path.join(spec.run_root, "briefs", f"{spec.id}.resume.md"), "w") as f:
        f.write("you were interrupted")
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["env"]["SL_RESUME"] == "1"
    assert host.spawned[0]["env"]["SL_SESSION_ID"] == spec.resume_session_id


def test_a_fresh_launch_mints_its_own_session_id(tmp_path):
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    minted = host.spawned[0]["env"]["SL_SESSION_ID"]
    assert minted and host.spawned[0]["env"]["SL_RESUME"] == ""
    with open(os.path.join(spec.run_root, "state", "sessions", spec.id)) as f:
        assert f.read() == minted, "the id must be recorded before the session exists"


# --------------------------------------------------------------------------- the pane environment

def test_no_poison_is_ever_forwarded_into_a_pane(tmp_path):
    """The launcher's half of the #301 floor. The floor itself runs INSIDE the pane (only code in
    the session's own environment can prove anything about it), but the launcher must not be the
    one handing the poison over."""
    spec = _spec(tmp_path, forwarded_env={"ANTHROPIC_API_KEY": "sk-live",
                                          "ANTHROPIC_BASE_URL": "http://gw",
                                          "CLAUDE_CODE_ENTRYPOINT": "cli",
                                          "CLAUDECODE": "1",
                                          "XDG_CONFIG_HOME": "/tmp/elsewhere",
                                          "PATH": "/usr/bin"})
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    env = host.spawned[0]["env"]
    for poison in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ENTRYPOINT",
                   "CLAUDECODE", "XDG_CONFIG_HOME"):
        assert poison not in env, f"{poison} reached the pane env"


def test_the_launcher_never_hands_a_pane_a_host_variable(tmp_path):
    """Defense in depth beside the fence: whatever a caller believes it is doing, no HERDR_* the
    launcher composes reaches a pane. The wrapper strips them too — this is the near side."""
    spec = _spec(tmp_path, forwarded_env={"HERDR_API_TOKEN": "real-secret",
                                          "HERDR_SOCKET_PATH": "/tmp/h.sock"})
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert not [k for k in host.spawned[0]["env"] if k.startswith("HERDR")]


def test_the_pane_env_names_everything_the_in_pane_floor_needs(tmp_path):
    """A pane inherits nothing from the launcher, so every SL_* the floor reads must be NAMED.
    An absent SL_EXPECT_GH_LOGIN does not weaken the floor's assert — it refuses every launch."""
    spec = _spec(tmp_path, model="opus[1m]", effort="high", agent="claude")
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    env = host.spawned[0]["env"]
    assert env["SL_ISSUE_ID"] == spec.id
    assert env["SL_RUN_ROOT"] == spec.run_root
    assert env["SL_MODEL"] == "opus[1m]"
    assert env["SL_EFFORT"] == "high"
    assert env["SL_AGENT"] == "claude"
    assert env["SL_EXPECT_GH_LOGIN"] == "loopbot"
    assert env["SL_START_TOKEN"] == "tok-1"
    assert env["SL_START_SESSION"].endswith("start-session.sh"), \
        "the pane's shim handoff needs the absolute path — a pane knows no engine paths"


def test_the_claude_pin_rides_only_when_this_launcher_actually_has_one(tmp_path):
    """#303: naming it unconditionally would BLANK an operator's pin on every machine whose
    runner happens not to export it, silently restoring PATH luck."""
    spec = _spec(tmp_path)
    _result, _edges, host = _run(spec)
    assert "SL_CLAUDE" not in host.spawned[0]["env"]
    spec = _spec(tmp_path, claude_bin="/opt/claude")
    _result, _edges, host = _run(spec)
    assert host.spawned[0]["env"]["SL_CLAUDE"] == "/opt/claude"


# --------------------------------------------------------------------------- the fence (#305)

def test_a_debugger_receives_the_control_socket_token_and_a_worker_never_does(tmp_path):
    """The fence's token wiring, realized at spawn. The NAME decides — the launcher passes no
    flag that could grant it, so the i<N> path cannot get it wrong even by accident."""
    assert session_host.receives_token("d12") is True
    assert session_host.receives_token("i308") is False
    spec = _spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(tmp_path))
    _result, _edges, host = _run(spec)
    assert host.spawned[0]["name"] == "d12", \
        "the wrapper decides the grant from the NAME, so the name must be the lane id"


def test_the_launcher_passes_no_token_of_its_own(tmp_path):
    """The wrapper never handles the secret (it would land in argv, which a same-uid worker can
    read). The launcher must not smuggle one in under its own name either."""
    spec = _spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(tmp_path))
    _result, _edges, host = _run(spec)
    env = host.spawned[0]["env"]
    assert session_host.API_TOKEN_ENV_VAR not in env
    assert session_host.API_TOKEN_FILE_ENV_VAR not in env


# --------------------------------------------------------------------------- the handoff

def test_the_spawn_asks_for_the_lane_id_as_the_agent_name(tmp_path):
    spec = _spec(tmp_path)
    _result, _edges, host = _run(spec)
    assert host.spawned[0]["name"] == "i308"
    assert host.spawned[0]["kind"] == "claude"


def test_the_agent_command_line_is_never_built_here(tmp_path):
    """The agent boundary: the launch command line lives ONLY in start-session.sh. The launcher
    passes NO native agent args — the pane's shim hands the typed verb to start-session.sh, which
    owns every claude/codex flag."""
    spec = _spec(tmp_path)
    _result, _edges, host = _run(spec)
    assert host.spawned[0]["agent_args"] == []


def test_an_unsupported_agent_is_refused_repo_wide(tmp_path):
    spec = _spec(tmp_path, agent="parrot")
    result, _edges, host = _run(spec)
    assert result.rc == launch.UNSUPPORTED_AGENT
    assert host.spawned == []


# --------------------------------------------------------------------------- delivery

def test_a_hollow_spawn_reads_the_sessions_own_refusal_before_blaming_the_machinery(tmp_path):
    """The floor refuses from inside the pane and the agent therefore never starts. The launcher
    must speak the session's own diagnosis (rc=6), not "the shim did not fire"."""
    spec = _spec(tmp_path)
    envfail = os.path.join(spec.run_root, "state", "envfail")
    os.makedirs(envfail)
    with open(os.path.join(envfail, f"{spec.id}.tok-1"), "w") as f:
        f.write("ANTHROPIC_API_KEY survived")
    host = FakeHost(raises=session_host.SpawnRefused("no process is behind it"))
    result, _edges, _host = _run(spec, host=host, started=False)
    assert result.rc == launch.ENV_POISONED
    assert "env poisoned" in result.stderr.lower()
    assert "ANTHROPIC_API_KEY survived" in result.stderr


def test_a_dead_auth_refusal_is_read_the_same_way(tmp_path):
    spec = _spec(tmp_path)
    authfail = os.path.join(spec.run_root, "state", "authfail")
    os.makedirs(authfail)
    with open(os.path.join(authfail, f"{spec.id}.tok-1"), "w") as f:
        f.write("gh answered as nobody")
    host = FakeHost(raises=session_host.SpawnRefused("no process is behind it"))
    result, _edges, _host = _run(spec, host=host, started=False)
    assert result.rc == launch.AUTH_DEAD
    assert "gh auth dead" in result.stderr.lower()


def test_a_refused_spawn_with_no_marker_is_a_delivery_channel_fault(tmp_path):
    spec = _spec(tmp_path)
    host = FakeHost(raises=session_host.SpawnRefused("the agent did not start"))
    result, _edges, _host = _run(spec, host=host, started=False)
    assert result.rc == launch.NOT_DELIVERED
    assert "launch not delivered" in result.stderr.lower()


def test_a_started_agent_with_no_floor_sentinel_is_not_a_delivery(tmp_path):
    """The host reporting `interactive_ready` proves a process, not OUR session: herdr classified
    a fake with no claude in it as idle+ready. The floor's own per-launch sentinel is the proof,
    and a pane that never stamped one is killed rather than recorded as a live lane."""
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, started=False)
    assert result.rc == launch.NOT_DELIVERED
    assert host.killed == ["i308"], "an unverified session is torn down, never left running"


def test_a_verified_delivery_records_the_handle_and_the_liveness_baseline(tmp_path):
    spec = _spec(tmp_path)
    result, _edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    state = os.path.join(spec.run_root, "state")
    with open(os.path.join(state, "panes", spec.id)) as f:
        assert f.read() == "w9:p1"
    with open(os.path.join(state, "panes", spec.id + ".ws")) as f:
        assert f.read() == "w9"
    assert os.path.exists(os.path.join(state, "activity", spec.id)), \
        "the freeze-net baseline is stamped only after delivery is verified"


def test_the_liveness_baseline_is_never_stamped_before_delivery(tmp_path):
    """Writing it early once fabricated 'launched & alive' for 45 minutes."""
    spec = _spec(tmp_path)
    host = FakeHost(raises=session_host.SpawnRefused("nope"))
    _result, _edges, _host = _run(spec, host=host, started=False)
    assert not os.path.exists(os.path.join(spec.run_root, "state", "activity", spec.id))


def test_a_verified_worker_delivery_bumps_the_launch_counter(tmp_path):
    spec = _spec(tmp_path)
    _result, _edges, _host = _run(spec)
    with open(os.path.join(spec.run_root, "state", "issues.json")) as f:
        issue = json.load(f)["issues"][spec.id]
    assert issue["launches"] == 1 and issue["retries"] == 0


def test_a_resume_is_never_counted_as_a_retry(tmp_path):
    """#298: `retries` counts how many times a lane had to be STARTED OVER, and re-entering the
    same conversation is the opposite of starting over."""
    spec = _spec(tmp_path, resume_session_id="11111111-2222-3333-4444-555555555555")
    with open(os.path.join(spec.run_root, "briefs", f"{spec.id}.resume.md"), "w") as f:
        f.write("you were interrupted")
    _result, _edges, _host = _run(spec)
    with open(os.path.join(spec.run_root, "state", "issues.json")) as f:
        issue = json.load(f)["issues"][spec.id]
    assert issue.get("launches", 0) == 0


def test_a_debugger_delivery_has_no_launch_counter(tmp_path):
    """A d<N> is not a tracked issue."""
    home = _home(tmp_path, "d12")
    spec = _spec(tmp_path, iid="d12", home=home, cwd=str(tmp_path))
    result, _edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    with open(os.path.join(spec.run_root, "state", "issues.json")) as f:
        assert json.load(f)["issues"] == {}, "no counter is invented for a debugger id"


# --------------------------------------------------------------------------- hygiene

def test_a_relaunch_clears_this_ids_stale_run_markers_only(tmp_path):
    """A prior session's report/exited/blocked must not mis-fire for the fresh session — and the
    scope is strictly these named markers, never a glob that could discard a real report."""
    spec = _spec(tmp_path)
    state = os.path.join(spec.run_root, "state")
    for sub in ("blocked", "exited", "awaiting", "mail", "status"):
        os.makedirs(os.path.join(state, sub), exist_ok=True)
    os.makedirs(os.path.join(spec.run_root, "reports"), exist_ok=True)
    stale = [os.path.join(spec.run_root, "reports", f"{spec.id}.md"),
             os.path.join(state, "blocked", spec.id),
             os.path.join(state, "exited", spec.id),
             os.path.join(state, "mail", spec.id)]
    keep = [os.path.join(spec.run_root, "reports", "i999.md"),
            os.path.join(state, "mail", f"{spec.id}.consumed.7")]
    for path in stale + keep:
        with open(path, "w") as f:
            f.write("x")
    _result, _edges, _host = _run(spec)
    assert not any(os.path.exists(p) for p in stale)
    assert all(os.path.exists(p) for p in keep), \
        "delivery receipts and other lanes' reports are history, and history survives a restart"


def test_a_worker_launch_without_a_repo_names_that_rather_than_the_branch(tmp_path):
    """Left to git it would fail the base-ref probe too, and exit 3 would send the owner to fix a
    dev_branch that is not what went wrong."""
    spec = _spec(tmp_path, repo="")
    result, _edges, host = _run(spec, started=False)
    assert result.rc == launch.ABORTED
    assert "SL_REPO" in result.stderr
    assert host.spawned == []
