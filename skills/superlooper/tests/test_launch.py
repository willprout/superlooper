"""The ONE spawn path (issue #308) — the pre-flight half, and the doorway it hands off to.

Three spawners (runner worker, watchdog debugger, dashboard Fixer's owner tap) plus the revive
path all call ``lib/launch.py``; it is the only thing left that creates a session. These tests
pin what the plan's §9 said must survive the port to the session-host wrapper:

  * the ``^i[0-9]+$`` / ``^d[0-9]+$`` / ``^t[0-9]+$`` mode guards (a debugger id can never spin up
    a worktree, an issue id can never take the in-place ``--cwd`` path, and a triage flight is
    mutually exclusive with both — issue #448),
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

import journal
import launch
import session_host
import triage


# --------------------------------------------------------------------------- fakes

class FakeEdges:
    """The launcher's external edges: subprocesses, the clock, and the launch token.

    ``answers`` maps a matched argv fragment to (rc, stdout, stderr). Anything unmatched
    succeeds silently — a test names only the edge it is about.
    """

    def __init__(self, answers=None, fence=None):
        self.answers = answers or {}
        self.calls = []
        self.slept = 0.0
        # What a tokenless probe of the control socket would find (#326). FENCED by default so a
        # test that is not about the fence never has to say so.
        self.fence_verdict = fence if fence is not None else session_host.FENCED
        self.fence_asked = []

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

    def fence(self, socket_path, timeout=None):
        self.fence_asked.append(socket_path)
        return self.fence_verdict


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
    # rc=1 is what `rev-parse --verify --quiet` returns for a ref that is genuinely not there.
    edges = FakeEdges({"worktree add": (1, "", "fatal: invalid reference"),
                       "rev-parse --verify": (1, "", "")})
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.BASE_MISSING
    assert "origin/main" in result.stderr
    assert host.spawned == [], "a missing base costs no pane"


def test_a_git_that_could_not_answer_is_never_read_as_a_missing_base(tmp_path):
    """Exit 3's memo tells the owner to change dev_branch and re-approve. A hung or unrunnable git
    has not said the ref is absent — it has said nothing — and sending the owner to edit config for
    it is the confidently-wrong remedy the evidence table exists to end."""
    for rc in (124, 127, 128):
        edges = FakeEdges({"worktree add": (1, "", "fatal: could not read"),
                           "rev-parse --verify": (rc, "", "git said nothing useful")})
        spec = _spec(tmp_path)
        result, _edges, _host = _run(spec, edges=edges)
        assert result.rc == launch.ABORTED, "rc=%s must not become exit 3" % rc
        assert "could not create the worktree" in result.stderr


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
    lock = os.path.join(spec.run_root, "state", "worktree.lock")

    class Watching(FakeEdges):
        """Answers `git worktree add` only while the lock is genuinely held by this process."""

        def run(self, argv, timeout=None, cwd=None, env=None):
            if "worktree" in argv and "add" in argv:
                probe = open(lock, "a+")
                try:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)          # someone holds it — which is the point
                else:
                    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    held.append(False)
                finally:
                    probe.close()
            return super().run(argv, timeout=timeout, cwd=cwd, env=env)

    held = []                       # one entry per `git worktree add`: was the lock held then?
    result, edges, _host = _run(spec, edges=Watching())
    assert result.rc == launch.OK, result.stderr
    assert os.path.exists(lock), "the worktree critical section takes a real lock file"
    assert held and all(held), \
        "the lock must be HELD across `git worktree add`, not merely taken beside it"
    # And it is a REAL exclusive lock, not a marker file: a second holder must wait for it.
    with launch.WorktreeLock(lock) as holder:
        assert holder._fh is not None
        second = launch.WorktreeLock(lock)
        second._fh = open(lock, "a+")
        with pytest.raises(BlockingIOError):
            fcntl.flock(second._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        second._close()
    # ...and it is RELEASED: a second holder acquires it without blocking once the launch is over.
    # (Asserted non-blockingly — a launch that leaked the lock would otherwise hang this test
    # rather than fail it.)
    after = open(lock, "a+")
    try:
        fcntl.flock(after.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held
        fcntl.flock(after.fileno(), fcntl.LOCK_UN)
    finally:
        after.close()


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


# ------------------------------------------------------- the identity env contract (issue #314)
# The credential namespace a `claude` session uses is `sha256` of the CLAUDE_CONFIG_DIR string AS
# WRITTEN (#300), so isolation holds only while every spawn emits the SAME string — and the wrong
# one presents as a logged-out session rather than as an error. The launcher derives it ONCE.

_FLEET_DIR = "/Users/loop/.claude-fleet"
_FLEET_ORG = "512c95fc-0638-4911-a131-32f411f70afc"
_FLEET_STATUS = json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                            "apiProvider": "firstParty", "email": "loop@example.com",
                            "orgId": _FLEET_ORG, "subscriptionType": "max"})


def _fleet_spec(tmp_path, iid="i308", **over):
    """A launch on a machine that HAS assigned a fleet config dir."""
    kw = {"claude_config_dir": _FLEET_DIR, "claude_bin": "/opt/claude"}
    kw.update(over)
    return _spec(tmp_path, iid=iid, **kw)


def test_both_spawn_paths_are_handed_the_same_byte_identical_config_dir(tmp_path):
    """DoD: one canonical string, and `i<N>` and `d<N>` pass the SAME one. They share
    `pane_environment`, so this is by construction — asserted because the construction is the
    guarantee, and a later split of the two paths is exactly how it would be lost."""
    edges = FakeEdges({"auth status": (0, _FLEET_STATUS, "")})
    worker = _fleet_spec(tmp_path)
    _r, _e, host = _run(worker, edges=edges)
    cwd = tmp_path / "patient"
    cwd.mkdir()
    debugger = _fleet_spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(cwd))
    result, _e, host2 = _run(debugger, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == _FLEET_DIR
    assert host2.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == _FLEET_DIR
    # ...and both were told which ACCOUNT that dir must turn out to hold.
    assert host.spawned[0]["env"]["SL_EXPECT_CLAUDE_ACCOUNT"] == _FLEET_ORG
    assert host2.spawned[0]["env"]["SL_EXPECT_CLAUDE_ACCOUNT"] == _FLEET_ORG


def test_a_non_canonical_assignment_is_normalised_once_and_never_reaches_a_pane_twice(tmp_path):
    """#300 landmine 1: five spellings of one directory produced five credential namespaces. The
    launcher normalises at the seam, so the pane can only ever see the canonical one."""
    for spelling in (_FLEET_DIR + "/", _FLEET_DIR + "//", "/Users/loop/./.claude-fleet"):
        spec = _fleet_spec(tmp_path, claude_config_dir=spelling)
        result, _e, host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
        assert result.rc == launch.OK, result.stderr
        assert host.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == _FLEET_DIR, spelling


def test_a_config_dir_that_cannot_be_canonicalised_refuses_before_any_pane_exists(tmp_path):
    spec = _fleet_spec(tmp_path, claude_config_dir="claude-fleet")
    result, _e, host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.CLAUDE_IDENTITY_RUNNER
    assert "absolute" in result.stderr and host.spawned == []


def test_the_launcher_reads_the_account_under_the_assigned_dir_and_hands_it_down(tmp_path):
    spec = _fleet_spec(tmp_path)
    edges = FakeEdges({"auth status": (0, _FLEET_STATUS, "")})
    result, _e, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    probes = [c for c in edges.calls if c[1:3] == ["auth", "status"]]
    assert probes and probes[0][0] == "/opt/claude"
    assert host.spawned[0]["env"]["SL_EXPECT_CLAUDE_ACCOUNT"] == _FLEET_ORG


def test_a_runner_environment_on_an_api_key_holds_the_queue_instead_of_launching(tmp_path):
    """MEASURED shape (2026-08-04): with ANTHROPIC_API_KEY exported the real binary answers
    `loggedIn: true` with null org and subscription. rc=8 is the CHANNEL half — no queued issue
    caused it and re-approving fixes nothing."""
    on_key = json.dumps({"loggedIn": True, "apiKeySource": "ANTHROPIC_API_KEY", "email": None,
                         "orgId": None, "subscriptionType": None})
    spec = _fleet_spec(tmp_path)
    result, _e, host = _run(spec, edges=FakeEdges({"auth status": (0, on_key, "")}))
    assert result.rc == launch.CLAUDE_IDENTITY_RUNNER
    assert "api key" in result.stderr.lower() and host.spawned == []


def test_a_pinned_account_the_runner_does_not_hold_refuses_the_launch(tmp_path):
    """The operator's own expectation, when they name one: the fleet dir being logged in is not
    the same fact as it being logged in to the account the capacity plan assigned it."""
    spec = _fleet_spec(tmp_path, expect_claude_account="9f0d1a22-dead-4b33-9999-abcdefabcdef")
    result, _e, host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.CLAUDE_IDENTITY_RUNNER
    assert _FLEET_ORG in result.stderr and host.spawned == []


def test_a_machine_that_assigns_no_config_dir_launches_exactly_as_before(tmp_path):
    """Production's path, and the reason there is no default: a config dir applied by the launcher
    on every machine would move every worker onto a dir nobody provisioned, and an unprovisioned
    dir parks a session at the first-run theme picker. The pane is still TOLD there is no
    assignment, so the floor can tell "none assigned" from "an old launcher"."""
    spec = _spec(tmp_path)
    edges = FakeEdges()
    result, _e, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == ""
    assert host.spawned[0]["env"]["SL_EXPECT_CLAUDE_ACCOUNT"] == ""
    assert not [c for c in edges.calls if c[1:3] == ["auth", "status"]], \
        "an unassigned machine must not pay for a status read it has nothing to compare"


def test_pretrust_is_aimed_at_the_file_the_session_it_trusts_for_will_read(tmp_path):
    """Issue #345, and the whole of it. Trust is keyed PER CONFIG DIR, so a pre-trust written to
    one file while the session reads another is inert — and every issue gets a fresh worktree, so
    on a fleet machine that is every launch stopping at a first-run dialog with nobody home.

    The assertion is the identity, not a resemblance: the string handed to pretrust is the SAME
    string named in the pane, byte for byte, because both come off the one derivation at the seam.
    """
    spec = _fleet_spec(tmp_path)
    result, edges, host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust, "no pretrust ran"
    assert pretrust[0][2] == host.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == _FLEET_DIR


def test_the_debugger_path_pretrusts_under_the_same_assigned_dir(tmp_path):
    """An owner-tap repair on an unvisited repo is exactly the case that would hang, and it runs
    under the same identity — so it gets the same answer, from the same derivation."""
    cwd = tmp_path / "patient"
    cwd.mkdir()
    spec = _fleet_spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(cwd))
    result, edges, _host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust and pretrust[0][2] == _FLEET_DIR


def test_a_non_canonical_spelling_reaches_pretrust_canonicalised_too(tmp_path):
    """The trust store is a FILE PATH, so a trailing slash costs nothing there — but the pane's
    credential namespace is a hash of the string, and one derivation feeding both is what keeps
    the two from ever being asked to agree about two different strings."""
    spec = _fleet_spec(tmp_path, claude_config_dir=_FLEET_DIR + "//")
    result, edges, _host = _run(spec, edges=FakeEdges({"auth status": (0, _FLEET_STATUS, "")}))
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust and pretrust[0][2] == _FLEET_DIR


def test_a_machine_that_assigns_no_config_dir_pretrusts_the_default_store(tmp_path):
    """The unchanged path, and it is NAMED rather than omitted: pretrust inherits the launcher's
    own environment, so leaving the argument off would let a stray CLAUDE_CONFIG_DIR in the
    runner's shell aim the record at a dir this launch is not using — the same bug one directory
    over. An explicit empty string is the launcher saying 'this machine assigns none'."""
    spec = _spec(tmp_path)
    result, edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    pretrust = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert pretrust and pretrust[0][2] == ""
    assert host.spawned[0]["env"]["SL_CLAUDE_CONFIG_DIR"] == ""


def test_an_inherited_identity_variable_is_never_forwarded_into_a_pane(tmp_path):
    """The launcher must not be the one handing over either half of the contract. (The floor
    inside the pane is what PROVES it — the pane's shell sources the operator's rc files after
    this launcher is gone.)"""
    spec = _spec(tmp_path, forwarded_env={"CLAUDE_CONFIG_DIR": "/somebody/elses",
                                          "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
                                          "PATH": "/usr/bin"})
    result, _e, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    env = host.spawned[0]["env"]
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env


def test_the_launchers_own_probe_env_reads_the_assigned_namespace_and_no_other(tmp_path):
    """The two reads — the launcher's and the session's — must consult the same credential
    namespace, or the comparison between them names a fault that is not there. Same reason
    `identity_probe_env` drops XDG_CONFIG_HOME for the gh probe (#299)."""
    env = launch.identity_probe_env({"HOME": "/h", "XDG_CONFIG_HOME": "/x",
                                     "ANTHROPIC_API_KEY": "sk-live",
                                     "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""},
                                    config_dir=_FLEET_DIR)
    assert env["CLAUDE_CONFIG_DIR"] == _FLEET_DIR
    assert env["HOME"] == "/h", "HOME must survive — overriding it breaks macOS keychain OAuth"
    for gone in ("XDG_CONFIG_HOME", "ANTHROPIC_API_KEY", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        assert gone not in env, gone
    # No assignment -> ABSENT, never empty: an empty value is its own namespace, not "the default".
    assert "CLAUDE_CONFIG_DIR" not in launch.identity_probe_env({"HOME": "/h"})


def test_a_session_that_refuses_its_own_identity_is_read_as_identity_not_as_delivery(tmp_path):
    """The floor refuses from inside the pane, so no agent ever starts. rc=7 is per-issue: the
    memo names the account, and the owner is the only one who can repair it."""
    spec = _spec(tmp_path)
    marker = os.path.join(spec.run_root, "state", "identityfail")
    os.makedirs(marker)
    with open(os.path.join(marker, f"{spec.id}.tok-1"), "w") as f:
        f.write("this environment is running on an API key")
    host = FakeHost(raises=session_host.SpawnRefused("no process is behind it"))
    result, _edges, _host = _run(spec, host=host, started=False)
    assert result.rc == launch.CLAUDE_IDENTITY
    assert "claude identity refused" in result.stderr.lower()
    assert "running on an API key" in result.stderr


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


# ------------------------------------------------------------ the fence pre-flight (#326)
# #305 shipped the fence and the probe that measures it; nothing called the probe on a launch
# path, so the only thing establishing that a fleet was fenced was a human running the acceptance
# check by hand. The carried patch is deliberately INERT with no token configured (that is what
# lets upstream's own suite pass unmodified), so a stock or misconfigured host serves any tokenless
# worker — and from the runner's seat that host does not look broken, because it answers.

_SOCK = "/tmp/superlooper-test-fence.sock"


def _fleet(monkeypatch, switch="required", socket=_SOCK):
    """Declare what this MACHINE says about its fence, where the launcher actually reads it.

    The PROCESS environment, not a Spec field. That is not a test-rig detail — it is the property
    under test: the doorway runs the host CLI with no explicit env, so `os.environ` is what the
    child inherits, and a launcher that read the switch or the socket from anywhere else could
    probe one socket and spawn onto another. (conftest neutralizes both variables autouse, so a
    test that does not call this gets a disarmed gate and an absent socket.)
    """
    if switch is None:
        monkeypatch.delenv("SL_FLEET_FENCE", raising=False)
    else:
        monkeypatch.setenv("SL_FLEET_FENCE", switch)
    if socket is None:
        monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    else:
        monkeypatch.setenv("HERDR_SOCKET_PATH", socket)


def test_an_open_socket_refuses_a_worker_launch(tmp_path, monkeypatch):
    """THE point of the issue. OPEN means a tokenless caller was SERVED: every worker pane already
    carries the socket path, so the session about to be started could drive the whole fleet with
    ten lines of python. Refused, never warned about."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.FENCE_DOWN
    assert "FENCE DOWN" in result.stderr
    assert "served" in result.stderr.lower()
    assert host.spawned == [], "an unfenced fleet costs no pane"


def test_a_fenced_socket_permits_a_worker_launch(tmp_path, monkeypatch):
    """The other half, and the one that keeps this from being a gate that refuses everything."""
    edges = FakeEdges(fence=session_host.FENCED)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert [s["name"] for s in host.spawned] == ["i308"]


def test_an_unreachable_socket_refuses_a_worker_launch(tmp_path, monkeypatch):
    """The RULED behaviour (#326 DoD), decided rather than defaulted: UNREACHABLE refuses.

    Silence is not a fence — `fence_probe` will not call it one (c2), and neither may this. Two
    further reasons it is a refusal rather than a proceed: the spawn is about to go through that
    same socket, so a socket that is genuinely down fails the launch a step later anyway; and a
    pre-flight that PROCEEDED on UNREACHABLE would be silently disarmed by anything that breaks the
    probe — a fail-open on the one check whose whole job is to fail closed.
    """
    edges = FakeEdges(fence=session_host.UNREACHABLE)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.FENCE_DOWN
    assert "FENCE DOWN" in result.stderr
    assert "did not answer" in result.stderr
    assert host.spawned == []


def test_an_unfenced_dev_host_stays_usable(tmp_path, monkeypatch):
    """The switch is what makes this shippable. A dev workstation runs a stock host, so its socket
    is OPEN by construction — and a hardcoded assumption here would break every dev spawn."""
    edges = FakeEdges(fence=session_host.OPEN)
    # A machine that declares no fence — every checkout that is not the fleet's.
    _fleet(monkeypatch, switch=None)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert [s["name"] for s in host.spawned] == ["i308"]
    assert edges.fence_asked == [_SOCK], \
        "a disarmed gate still MEASURES: an unfenced machine's state has to be journalable"


def test_an_explicit_off_is_the_same_as_saying_nothing(tmp_path, monkeypatch):
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch, switch="off")
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned != []


def test_a_switch_value_this_engine_cannot_read_arms_the_gate_and_names_itself(tmp_path, monkeypatch):
    """Fails closed, and says why. A typo'd switch on the fleet machine reading as `off` is a
    silently disarmed fence — the exact class of silence this pre-flight exists to end."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch, switch="requried")
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.FENCE_DOWN
    assert "requried" in result.stderr
    assert host.spawned == []


def test_the_gate_cannot_be_disarmed_by_the_attended_flag(tmp_path, monkeypatch):
    """There is deliberately no attended bypass, for the reason `receives_token` has no grant
    parameter: `SL_ATTENDED` is read from the environment, so an ambient `export SL_ATTENDED=1` in
    the shell or LaunchAgent that started the runner would otherwise disarm the fence for every
    worker on the machine. The MODE decides, and nothing else does."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path, attended=True)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.FENCE_DOWN
    assert host.spawned == []


def test_a_debugger_launch_is_never_gated_by_the_fence(tmp_path, monkeypatch):
    """A `d<N>` RECEIVES the token by design (#305), so an open socket grants it nothing it does
    not already hold — and refusing repair because the fence is down would mean no unattended
    repair at exactly the moment repair is needed, which is the landmine the whole spawn port was
    built around."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), cwd=str(tmp_path))
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned != []
    assert edges.fence_asked == [], "the repair path is not gated, so it does not probe either"


def test_the_refusal_costs_no_worktree_no_pretrust_and_no_pane(tmp_path, monkeypatch):
    """Ordered with the other machine-level asserts, ahead of anything that CREATES: a refusal must
    leave no orphan pane and no leftover checkout (the base-missing discipline, #28)."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges, started=False)
    assert result.rc == launch.FENCE_DOWN
    assert not any("worktree" in " ".join(c) for c in edges.calls), edges.calls
    assert not any("pretrust" in " ".join(c) for c in edges.calls), edges.calls
    assert host.spawned == []


def test_the_probe_asks_the_socket_the_spawn_itself_would_use(tmp_path, monkeypatch):
    """Not theatre: the verdict has to be about the socket this launch is about to drive. The
    launcher resolves it through the doorway's own resolver, from the same environment the host
    CLI child inherits — so the two agree by construction rather than by two copies of a rule."""
    edges = FakeEdges(fence=session_host.FENCED)
    _fleet(monkeypatch, socket=None)
    monkeypatch.setenv("HOME", "/Users/x")
    monkeypatch.setenv("HERDR_SESSION", "fleet")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    spec = _spec(tmp_path)
    result, edges, _host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    assert edges.fence_asked == [session_host.control_socket_path(os.environ)]
    assert edges.fence_asked == ["/Users/x/.config/herdr/sessions/fleet/herdr.sock"]


def test_a_machine_whose_socket_cannot_be_resolved_refuses_rather_than_guessing(tmp_path, monkeypatch):
    """`control_socket_path` returns None when there is nothing to resolve against. An armed gate
    that shrugged there would be a fence nobody measured; a guessed path would be a verdict about
    a machine the probe never looked at."""
    edges = FakeEdges(fence=session_host.FENCED)
    _fleet(monkeypatch, socket=None)                      # and no HOME to fall back on either
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    spec = _spec(tmp_path)
    result, edges, host = _run(spec, edges=edges)
    assert result.rc == launch.FENCE_DOWN
    assert edges.fence_asked == [], "there was no socket to ask"
    assert host.spawned == []


def test_the_probe_verdict_is_journaled_on_every_worker_launch(tmp_path, monkeypatch):
    """So a morning report can show the fence state OVER TIME. Journaled on the permitted path too
    — an armed gate that only ever wrote a line when it refused would leave 'this fleet has been
    fenced all week' unprovable, and the disarmed case unrecorded entirely."""
    edges = FakeEdges(fence=session_host.FENCED)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, _edges, _host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    records = [r for r in journal.read(spec.run_root) if r.get("act") == "fence_preflight"]
    assert len(records) == 1, records
    assert records[0]["id"] == "i308"
    assert records[0]["verdict"] == session_host.FENCED
    assert records[0]["required"] is True
    assert records[0]["socket"] == _SOCK
    assert records[0]["refused"] is False
    assert records[0]["ts"] > 0


def test_an_unfenced_machine_that_launches_anyway_still_leaves_a_record(tmp_path, monkeypatch):
    """The line that keeps 'default off' from being a silent no-op: a dev host's OPEN socket is
    written down every launch, so a machine that was quietly never armed is visible in the report
    rather than indistinguishable from a fenced one."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch, switch=None)
    spec = _spec(tmp_path)
    result, _edges, _host = _run(spec, edges=edges)
    assert result.rc == launch.OK, result.stderr
    record = [r for r in journal.read(spec.run_root) if r.get("act") == "fence_preflight"][0]
    assert record["verdict"] == session_host.OPEN
    assert record["required"] is False
    assert record["refused"] is False


def test_a_refusal_is_journaled_as_a_refusal(tmp_path, monkeypatch):
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    result, _edges, _host = _run(spec, edges=edges, started=False)
    assert result.rc == launch.FENCE_DOWN
    record = [r for r in journal.read(spec.run_root) if r.get("act") == "fence_preflight"][0]
    assert record["verdict"] == session_host.OPEN
    assert record["required"] is True
    assert record["refused"] is True


def test_an_unjournalable_verdict_never_decides_the_launch(tmp_path, monkeypatch):
    """Telemetry may not fail a launch, and — far more important — it may not PASS one. The
    refusal is the Result; the journal line is a record of it."""
    edges = FakeEdges(fence=session_host.OPEN)
    _fleet(monkeypatch)
    spec = _spec(tmp_path)
    # A journal that cannot be written: the state home is a FILE where the directory must be.
    unwritable = tmp_path / "nowhere"
    unwritable.write_text("not a directory")
    spec.run_root = str(unwritable / "home")
    result, _edges, host = _run(spec, edges=edges, started=False)
    assert result.rc == launch.FENCE_DOWN, result.stderr
    assert host.spawned == []


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


def test_a_relaunch_clears_a_stale_cross_review_breadcrumb(tmp_path):
    """(#443) The phase breadcrumb is a claim about what a session is doing RIGHT NOW, so it must
    never outlive the session that wrote it. A worker killed between the cross-review script's start
    stamp and its end trap leaves an OPEN one — and a relaunch inside its staleness window would
    otherwise publish the fresh session as "cross-reviewing" while it is only building."""
    spec = _spec(tmp_path)
    state = os.path.join(spec.run_root, "state")
    os.makedirs(os.path.join(state, "phase"), exist_ok=True)
    mine = os.path.join(state, "phase", spec.id)
    other = os.path.join(state, "phase", "i999")
    for path in (mine, other):
        with open(path, "w") as f:
            f.write("1755712345 phase=cross-reviewing event=start\n")
    _result, _edges, _host = _run(spec)
    assert not os.path.exists(mine), "a fresh session must not inherit the last one's phase claim"
    assert os.path.exists(other), "another lane's live breadcrumb is none of this launch's business"


def test_a_worker_launch_without_a_repo_names_that_rather_than_the_branch(tmp_path):
    """Left to git it would fail the base-ref probe too, and exit 3 would send the owner to fix a
    dev_branch that is not what went wrong."""
    spec = _spec(tmp_path, repo="")
    result, _edges, host = _run(spec, started=False)
    assert result.rc == launch.ABORTED
    assert "SL_REPO" in result.stderr
    assert host.spawned == []


def test_a_failed_pretrust_refuses_the_launch_rather_than_flying_into_a_dialog(tmp_path):
    """herdr does NOT remove the first-run trust dialog, and the host classifies a pane blocked on
    one as `idle`. Ignoring pretrust's rc fails OPEN into exactly that stall: the pane opens, the
    agent blocks, no sentinel is stamped, and the launch reads as rc=2 — a CHANNEL fault that holds
    the whole queue and blames the launch shim for a missing `jq`."""
    edges = FakeEdges({"pretrust.sh": (1, "", "jq: command not found")})
    spec = _spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.ABORTED
    assert "pre-trust" in result.stderr and "rc=1" in result.stderr
    assert host.spawned == [], "nothing is created for a folder we could not pre-trust"


def test_a_stalled_pretrust_is_never_reported_as_a_github_outage(tmp_path):
    """The refusal above carries NO third-party text, and this is why: `Edges.run` renders a
    timeout as "no answer within Ns", and evidence.py matches that CHANNEL needle before anything
    else. A stalled local pretrust reported as "GitHub is not answering; the queue resumes on its
    own" is a remedy for a fault that is not happening — and holds the queue until someone looks."""
    import evidence
    edges = FakeEdges({"pretrust.sh": (124, "", "no answer within 60s")})
    result, _edges, _host = _run(_spec(tmp_path), edges=edges)
    assert result.rc == launch.ABORTED
    assert "no answer within" not in result.stderr
    rec = evidence.build("launch", result.rc, result.stderr)
    assert rec["reason"] != "gh_probe_unreachable", rec
    assert not evidence.is_channel_fault(rec) or rec["reason"] == "launch_failed_before_delivery"


def test_a_session_id_that_could_not_be_recorded_refuses_the_launch(tmp_path):
    """The id is minted so the flight can be re-entered after any interruption. A session that
    launched without its handle recorded is one `superlooper resume` can never find, with nothing
    anywhere saying why."""
    spec = _spec(tmp_path)
    sessions = os.path.join(spec.run_root, "state", "sessions")
    os.makedirs(sessions, exist_ok=True)
    os.chmod(sessions, 0o500)                       # writable by nobody
    try:
        result, _edges, host = _run(spec, started=False)
    finally:
        os.chmod(sessions, 0o700)
    assert result.rc == launch.ABORTED
    # The SUBJECT has to be the crash rendering, not the refusal's own words: `launch`'s catch-all
    # interpolates the exception verbatim, so every word of a _Refused message also appears when it
    # is rendered as a crash. Asserting on those words guards nothing — proven by reverting
    # _Refused to a bare OSError, which the previous spelling of this test still passed.
    assert "failed unexpectedly" not in result.stderr, \
        "a deliberate refusal must not render as 'the launcher failed unexpectedly'"
    assert "could not record the session id" in result.stderr
    assert host.spawned == []


# ------------------------------------------------------- the t<N> session class (issue #448)
# A THIRD session class beside the worker and the debugger: the triage flight. The standing rule
# it implements is ruled and recorded (plugin/skills/superlooper/references/
# triage-standing-rule.md); what is pinned here is the plumbing that rule needs — that the class
# exists, that it is mutually exclusive with the other two, that its home is the repo's REAL
# checkout by default, and that it is handed no capability over the fleet.

def _triage_home(tmp_path, iid="t1"):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "briefs").mkdir(parents=True, exist_ok=True)
    (home / "briefs" / f"{iid}.md").write_text("triage the queue")
    (home / "state" / "issues.json").write_text(json.dumps({"issues": {}}))
    return home


def _triage_spec(tmp_path, iid="t1", **over):
    kw = {"home": _triage_home(tmp_path, iid), "mode": launch.TRIAGE}
    kw.update(over)
    return _spec(tmp_path, iid=iid, **kw)


# --------------------------------------------------------- the mode guards, all six crossings

def test_a_triage_id_can_never_be_launched_as_a_worker(tmp_path):
    """The `^i[0-9]+$` guard, now load-bearing for a third id shape: a t<N> routed through the
    worker path would take a lane's worktree and bump an issue counter that names nothing."""
    spec = _spec(tmp_path, iid="t1", home=_triage_home(tmp_path))
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "worker mode expects an issue id" in result.stderr
    assert host.spawned == []


def test_a_triage_id_can_never_take_the_in_place_debugger_path(tmp_path):
    """A t<N> handed --cwd would be launched as a REPAIR session — and a repair session is given
    the control-socket token. The guard is what keeps the fence's grant on d<N> alone."""
    spec = _spec(tmp_path, iid="t1", home=_triage_home(tmp_path), cwd=str(tmp_path))
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "debugger (d<N>) ids only" in result.stderr
    assert host.spawned == []


def test_a_worker_id_can_never_be_launched_as_a_triage_flight(tmp_path):
    """The symmetric guard. An i<N> here would run the issue's session in the repo's REAL
    checkout — no worktree, no branch, straight onto the owner's working tree."""
    spec = _spec(tmp_path, iid="i308", mode=launch.TRIAGE)
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "triage mode expects a triage id" in result.stderr
    assert host.spawned == []


def test_a_debugger_id_can_never_be_launched_as_a_triage_flight(tmp_path):
    spec = _spec(tmp_path, iid="d12", home=_home(tmp_path, "d12"), mode=launch.TRIAGE)
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "triage mode expects a triage id" in result.stderr
    assert host.spawned == []


def test_a_triage_spec_that_also_passes_cwd_is_refused(tmp_path):
    """--cwd belongs to the debugger and to nothing else. A triage flight's home is chosen by the
    repo's config, so a caller offering a directory here is a caller that has confused two
    session classes — and the one it confused this with holds the fence's token."""
    spec = _triage_spec(tmp_path, cwd=str(tmp_path))
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "debugger (d<N>) ids only" in result.stderr
    assert host.spawned == []


def test_a_mode_this_launcher_does_not_recognise_is_refused(tmp_path):
    """Fail closed on wrong-typed input, the rule the two original guards were written for: an
    unrecognised mode must never fall through to the worker path."""
    spec = _triage_spec(tmp_path, mode="janitor")
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "unknown session mode" in result.stderr
    assert host.spawned == []


# ------------------------------------------------------------------------------- the home

def test_a_triage_flight_runs_in_the_repos_real_checkout_by_default(tmp_path):
    """The ruled default (the standing rule's Home section): the flight sees what an orchestrator
    sees, gitignored working files included — which a fresh worktree by definition cannot show."""
    spec = _triage_spec(tmp_path)
    result, edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["cwd"] == os.path.realpath(spec.repo)
    assert not any("worktree" in " ".join(c) for c in edges.calls), \
        "the checkout home creates nothing: no worktree, no branch"
    assert not os.path.isdir(os.path.join(spec.run_root, "worktrees", "t1"))


def test_a_repo_may_select_a_worktree_home_and_gets_a_detached_checkout(tmp_path):
    """The opt-out for a repo whose gitignored overlay is sensitive. It is DETACHED: the flight
    never commits, never pushes and must never create a ref of its own."""
    spec = _triage_spec(tmp_path, triage_home=triage.WORKTREE)
    result, edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["cwd"] == os.path.join(spec.run_root, "worktrees", "t1")
    added = [c for c in edges.calls if "worktree" in c and "add" in c]
    assert added, "the worktree home creates one"
    assert "--detach" in added[0] and "-b" not in added[0], \
        "a triage worktree carries no branch — the flight creates no ref"


def test_an_unreadable_triage_home_refuses_rather_than_choosing_one(tmp_path):
    """A typo'd home must not silently pick the other one: the two see DIFFERENT repositories
    (one shows the gitignored overlay, the other cannot), so guessing is a wrong answer either way."""
    spec = _triage_spec(tmp_path, triage_home="somewhere")
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "unknown triage home" in result.stderr
    assert host.spawned == []


def test_a_triage_flight_without_a_repo_names_that_rather_than_the_branch(tmp_path):
    spec = _triage_spec(tmp_path, repo="")
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "no target repo" in result.stderr
    assert host.spawned == []


def test_a_triage_flight_opens_on_its_own_brief(tmp_path):
    spec = _triage_spec(tmp_path)
    os.remove(os.path.join(spec.run_root, "briefs", "t1.md"))   # AFTER the rig built the world
    result, _edges, host = _run(spec)
    assert result.rc == launch.ABORTED
    assert "missing brief" in result.stderr
    assert host.spawned == []


# ------------------------------------------------------------------------------- the floor

def test_a_triage_flight_is_pretrusted_like_a_worker(tmp_path):
    """The same floor: a folder whose first-run trust dialog would block the session with nobody
    there to answer it stalls a triage flight exactly as it stalls a worker."""
    spec = _triage_spec(tmp_path)
    result, edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    trusted = [c for c in edges.calls if c and c[0].endswith("pretrust.sh")]
    assert trusted and trusted[0][1] == os.path.realpath(spec.repo)


def test_a_failed_pretrust_refuses_a_triage_launch_too(tmp_path):
    edges = FakeEdges({"pretrust.sh": (1, "", "")})
    spec = _triage_spec(tmp_path)
    result, _edges, host = _run(spec, edges=edges)
    assert result.rc == launch.ABORTED
    assert "could not pre-trust" in result.stderr
    assert host.spawned == []


def test_no_poison_and_no_host_variable_reaches_a_triage_pane(tmp_path):
    """DoD: no fence token and no host env variables reach a t<N> pane. The token is decided by
    the NAME (session_host.receives_token), so a triage id provably cannot receive it; the
    HERDR_* scrub is the launcher's near half of the same rule."""
    assert session_host.receives_token("t1") is False
    spec = _triage_spec(tmp_path, forwarded_env={"HERDR_API_TOKEN": "real-secret",
                                                 "HERDR_SOCKET_PATH": "/tmp/h.sock",
                                                 "ANTHROPIC_API_KEY": "sk-live",
                                                 "XDG_CONFIG_HOME": "/tmp/elsewhere",
                                                 "PATH": "/usr/bin"})
    result, _edges, host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    env = host.spawned[0]["env"]
    assert not [k for k in env if k.startswith("HERDR")]
    assert session_host.API_TOKEN_ENV_VAR not in env
    assert session_host.API_TOKEN_FILE_ENV_VAR not in env
    for poison in ("ANTHROPIC_API_KEY", "XDG_CONFIG_HOME"):
        assert poison not in env
    assert env["PATH"] == "/usr/bin"
    assert host.spawned[0]["name"] == "t1", \
        "the wrapper decides the grant from the NAME, so the name must be the session id"


def test_a_triage_launch_is_fenced_exactly_like_a_worker(tmp_path, monkeypatch):
    """A t<N> is TOKENLESS, so on an open socket it could drive every pane on the machine — the
    same exposure the worker gate exists for. The d<N> exemption is about the token it RECEIVES,
    and a triage flight receives none."""
    _fleet(monkeypatch)
    spec = _triage_spec(tmp_path)
    result, _edges, host = _run(spec, edges=FakeEdges(fence=session_host.OPEN))
    assert result.rc == launch.FENCE_DOWN
    assert "FENCE DOWN" in result.stderr
    assert host.spawned == []


def test_a_triage_flight_never_bumps_an_issue_counter(tmp_path):
    """`launches`/`retries` are mechanical telemetry about a tracked ISSUE's lane. A triage
    flight is not a tracked issue and has no counter — exactly as a debugger has none."""
    spec = _triage_spec(tmp_path)
    result, _edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    with open(os.path.join(spec.run_root, "state", "issues.json")) as f:
        assert json.load(f)["issues"] == {}


def test_a_verified_triage_delivery_records_the_handle_and_the_liveness_baseline(tmp_path):
    spec = _triage_spec(tmp_path)
    result, _edges, _host = _run(spec)
    assert result.rc == launch.OK, result.stderr
    with open(os.path.join(spec.run_root, "state", "panes", "t1")) as f:
        assert f.read() == "w9:p1"
    assert os.path.exists(os.path.join(spec.run_root, "state", "activity", "t1"))


def test_a_wrong_typed_mode_is_refused_rather_than_read_as_absent(tmp_path):
    """Fail closed on WRONG-TYPED input, not merely on unsafe input — the guards' own stated rule
    (fresh-agent review, P1). Only the EXACT empty string is the legacy "say nothing" case that
    derives worker/debugger from ``cwd``; every other unreadable value is a caller that meant
    something, and reading it as "worker" is precisely the silent mis-route the guards exist for."""
    for junk in (None, False, 0, [], " ", "  \t "):
        spec = _spec(tmp_path, iid="i308", mode=junk)
        result, _edges, host = _run(spec)
        assert result.rc == launch.ABORTED, junk
        assert "unknown session mode" in result.stderr, junk
        assert host.spawned == []


def test_an_absent_mode_field_still_derives_the_two_original_classes(tmp_path):
    """The compatibility case, asserted rather than assumed: a Spec that predates #448 — no mode
    attribute at all — is still routed by ``cwd``, so no existing call site changed behaviour."""
    class Legacy:
        pass

    spec = _spec(tmp_path)
    legacy = Legacy()
    for name, value in vars(spec).items():
        if name != "mode":
            setattr(legacy, name, value)
    assert not hasattr(legacy, "mode")
    result, _edges, host = _run(legacy)
    assert result.rc == launch.OK, result.stderr
    assert host.spawned[0]["cwd"] == os.path.join(spec.run_root, "worktrees", "i308")
