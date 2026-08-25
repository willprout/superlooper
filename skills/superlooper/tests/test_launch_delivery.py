"""End-to-end delivery tests for bin/launch-session.py — the ONE spawn path (issue #308).

These drive the REAL launcher as a subprocess, through the real five-verb wrapper, against a stub
session host — and in the delivered case the pane really does run the real launch shim, the real
start-session.sh and its whole floor. So what is proven here is the actual chain a worker takes,
not a mock of it.

The stub host has three modes via ``$STUB_MODE``:

``deliver``
    ``agent start`` runs the pane chain for real: a fresh zsh that sources the launch shim, which
    arms the agent verb, which runs start-session.sh, which does the floor and runs the stub agent.
    This is the true integration test of the handoff.
``hollow``
    ``agent start`` REPORTS success and starts nothing — the measured phantom (the host classified
    a pane with no agent in it as ``idle``/``interactive_ready``). The wrapper's post-spawn
    confirmation must catch it, and the launcher must call it a non-delivery rather than a launch.
``refuse``
    ``agent start`` returns a structured error. The launcher must then read the session's OWN
    refusal markers before blaming the machinery.

(This file replaced the cmux delivery suite. Its subject — a tab created by an RPC whose keystroke
delivery was then silently dropped — cannot happen here: the host starts the agent itself. What
survives is every REASON that suite existed: never fabricate liveness, never let a hollow launch
read as a launch, and always name the right fault.)
"""
import os
import shutil
import signal
import stat
import subprocess
import textwrap
import time

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
LAUNCH = os.path.join(REPO_ROOT, "skill", "bin", "launch-session.py")
SHIM_PATH = os.path.join(REPO_ROOT, "skill", "shell", "launch-shim.zsh")

# The stub session host. It speaks the same JSON envelope the real one does — `{"result": {...}}`
# on success, `{"error": {"code", "message"}}` with rc=1 on failure — because that envelope is what
# lib/session_host.py parses, and a stub speaking a friendlier dialect would prove nothing.
STUB_HOST = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    group="${1:-}"; shift || true
    verb="${1:-}"; shift || true
    ok()  { printf '{"id":"stub","result":%s}\\n' "$1"; exit 0; }
    err() { printf '{"id":"stub","error":{"code":"%s","message":"%s"}}\\n' "$1" "$2"; exit 1; }

    case "$group:$verb" in
      workspace:create)
        # Record the pane environment EXACTLY as argv carried it, so a test can assert what a pane
        # was (and was not) handed — which is where the fence's token wiring is decided.
        : > "$STUB_DIR/pane.env"
        : > "$STUB_DIR/pane.exports"
        cwd=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --cwd) cwd="$2"; shift 2 ;;
            --env) printf '%s\\n' "$2" >> "$STUB_DIR/pane.env"
                   printf 'export %s=%q\\n' "${2%%=*}" "${2#*=}" >> "$STUB_DIR/pane.exports"
                   shift 2 ;;
            --label) printf '%s\\n' "$2" > "$STUB_DIR/label"; shift 2 ;;
            *) shift ;;
          esac
        done
        printf '%s' "$cwd" > "$STUB_DIR/cwd"
        : > "$STUB_DIR/workspace_live"
        ok '{"type":"workspace_created","workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"},"root_pane":{"pane_id":"w9:p1"}}'
        ;;

      agent:start)
        name="${1:-}"
        printf '%s\\n' "$name" >> "$STUB_DIR/started_agents"
        case "${STUB_MODE:-hollow}" in
          refuse) err "agent_start_failed" "the agent did not start" ;;
          deliver)
            # THE pane: a fresh shell in the workspace cwd holding ONLY what `workspace create`
            # was given. A pane inherits nothing from the launcher, and this reproduces that.
            ( set -a; . "$STUB_DIR/pane.exports"; set +a
              cd "$(cat "$STUB_DIR/cwd")" || exit 1
              exec zsh -c "source '$SHIM_PATH'; ${STUB_AGENT_VERB:-claude}" ) \\
              >> "$STUB_DIR/pane.log" 2>&1 &
            printf '%s' "$!" > "$STUB_DIR/pane.pid"
            ;;
          hollow)
            # A pid that is ALIVE and childless: the pane is a bare shell. This IS the phantom —
            # the host says ready, and nothing is behind it.
            sleep 25 &
            printf '%s' "$!" > "$STUB_DIR/pane.pid"
            ;;
        esac
        : > "$STUB_DIR/agent_live"
        ok '{"type":"agent_started","agent":{"name":"'"$name"'","pane_id":"w9:p1","agent_status":"idle"}}'
        ;;

      agent:get)
        [ -f "$STUB_DIR/agent_live" ] || err "agent_not_found" "no such agent"
        ok '{"type":"agent","agent":{"pane_id":"w9:p1","agent_status":"idle"}}'
        ;;

      pane:process-info)
        pid="$(cat "$STUB_DIR/pane.pid" 2>/dev/null || echo 1)"
        ok '{"type":"process_info","process_info":{"shell_pid":'"$pid"'}}'
        ;;

      workspace:close)
        printf 'closed\\n' >> "$STUB_DIR/closed"
        rm -f "$STUB_DIR/agent_live" "$STUB_DIR/workspace_live"
        ok '{"type":"workspace_closed"}'
        ;;

      workspace:get)
        [ -f "$STUB_DIR/workspace_live" ] || err "workspace_not_found" "no such workspace"
        ok '{"type":"workspace","workspace":{"workspace_id":"w9"}}'
        ;;

      *) err "unsupported" "stub host: $group $verb" ;;
    esac
""")

# The agent LINGERS, because the wrapper's liveness is a process fact: the pane shell must have a
# live child or the launch is (correctly) refused as hollow.
# `auth status` is answered on the SAME asymmetry the gh stub uses below, keyed on SL_ISSUE_ID —
# only a PANE carries that — so the launcher's own account read and the worker's can be made to
# disagree (issue #314). That is the whole failure this contract exists for: an identity that is
# perfectly healthy where the launcher stands and something else inside the session, because the
# credential namespace is a hash of the CLAUDE_CONFIG_DIR string the pane ended up with.
STUB_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "${1:-}" = "auth" ] && [ "${2:-}" = "status" ]; then
      printf '%s=%s\\n' "${SL_ISSUE_ID:-launcher}" "${CLAUDE_CONFIG_DIR:-<none>}" \\
        >> "$STUB_DIR/claude_auth_reads"
      if [ -n "${SL_ISSUE_ID:-}" ]; then org="${STUB_CLAUDE_WORKER_ORG:-fleet-org}"
      else                               org="${STUB_CLAUDE_ORG:-fleet-org}"; fi
      if [ "${CLAUDE_CONFIG_DIR:-}" != "${STUB_CLAUDE_LOGGED_IN_DIR:-}" ]; then
        printf '{"loggedIn": false, "authMethod": "none"}\\n'; exit 0
      fi
      printf '{"loggedIn":true,"authMethod":"claude.ai","email":"l@x.com","orgId":"%s","subscriptionType":"max"}\\n' "$org"
      exit 0
    fi
    printf "%s\\n" "$@" >> "$STUB_DIR/claude_args"
    env > "$STUB_DIR/claude_env"
    sleep "${STUB_AGENT_SECONDS:-20}"
    exit 0
""")
STUB_CODEX = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf "%s\\n" "$@" >> "$STUB_DIR/codex_args"
    env > "$STUB_DIR/codex_env"
    sleep "${STUB_AGENT_SECONDS:-20}"
    exit 0
""")

# `gh` for the positive auth assert (#299). It answers differently on the two sides of the spawn,
# keyed on SL_ISSUE_ID — only a PANE carries that — so the launcher's own environment and the
# worker's can be de-authed independently. That asymmetry is the whole point: the spike's failure
# mode is auth that dies in the WORKER while the runner's own gh stays perfectly healthy.
STUB_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    if [ "${1:-}" = "api" ] && [ "${2:-}" = "user" ]; then
      if [ -n "${SL_ISSUE_ID:-}" ]; then login="${STUB_GH_WORKER_LOGIN:-loopbot}"
      else                               login="${STUB_GH_LOGIN:-loopbot}"; fi
      if [ "$login" = "DEAD" ]; then
        echo "gh: To get started with GitHub CLI, please run:  gh auth login" >&2
        exit 4
      fi
      printf '%s\\n' "$login"
      exit 0
    fi
    echo "stub gh: unsupported call: $*" >&2
    exit 1
""")

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="zsh required for the launch shim")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(autouse=True)
def _no_ambient_claude_pin(monkeypatch, _never_reach_real_claude):
    """These cases run REAL launches through to start-session.sh, whose binary ladder (#303) fails
    closed on a pin that names nothing runnable — which is exactly conftest's never-reach-a-real-
    claude default. Left in place it would turn every delivery case into a pin case. Safe here for
    the same reason as in test_start_session.py: every case owns both HOME (a tmp dir with no
    standalone install) and the front of PATH (a stub agent), so the ladder can only land on a
    stub."""
    monkeypatch.delenv("SL_CLAUDE", raising=False)


@pytest.fixture(autouse=True)
def _stub_gh(tmp_path_factory, monkeypatch):
    gh = tmp_path_factory.mktemp("ghstub") / "gh"
    _x(str(gh), STUB_GH)
    monkeypatch.setenv("SL_GH", str(gh))
    monkeypatch.setenv("STUB_GH_LOGIN", "loopbot")
    monkeypatch.setenv("STUB_GH_WORKER_LOGIN", "loopbot")
    # A worker pane running this suite has its OWN id ambient, and the stub reads exactly that var
    # to tell the two sides apart — inherited, it would make every launcher-side probe answer as
    # the worker and silently invert the asymmetry cases.
    monkeypatch.delenv("SL_ISSUE_ID", raising=False)
    monkeypatch.delenv("SL_EXPECT_GH_LOGIN", raising=False)
    return gh


@pytest.fixture
def rig(tmp_path):
    """A whole launchable world: a run root, a clone with a real origin/main, and the stubs."""
    run_root = tmp_path / "run"
    (run_root / "briefs").mkdir(parents=True)
    (run_root / "reports").mkdir()
    (run_root / "state").mkdir()
    (run_root / "briefs" / "i1.md").write_text("do the thing")

    import loopstate
    st = loopstate.new_state()
    issue = loopstate.new_issue()
    issue["branch"] = "sl/i1-thing"
    st["issues"]["i1"] = issue
    loopstate.save(str(run_root / "state" / "issues.json"), st)

    home = tmp_path / "home"
    home.mkdir()
    origin = tmp_path / "origin"
    origin.mkdir()
    genv = {**os.environ, "HOME": str(home), "GIT_TERMINAL_PROMPT": "0"}

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True, env=genv)

    git("init", "-b", "main", str(origin))
    git("-C", str(origin), "config", "user.email", "t@example.com")
    git("-C", str(origin), "config", "user.name", "t")
    (origin / "README").write_text("x\n")
    git("-C", str(origin), "add", "-A")
    git("-C", str(origin), "commit", "-m", "init")
    repo = tmp_path / "repo"
    git("clone", str(origin), str(repo))
    git("-C", str(repo), "config", "user.email", "t@example.com")
    git("-C", str(repo), "config", "user.name", "t")

    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    host = stubdir / "sessionhost"
    _x(str(host), STUB_HOST)
    _x(str(stubdir / "claude"), STUB_CLAUDE)
    _x(str(stubdir / "codex"), STUB_CODEX)

    yield {"run_root": run_root, "repo": repo, "home": home, "stub": stubdir, "host": host}

    # House rule: never kill by name or pattern. We recorded the pane's own pid; kill only that.
    pidfile = stubdir / "pane.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def _launch(rig, mode="deliver", args=("i1",), extra_env=None, timeout=120):
    env = {
        **os.environ,
        "HOME": str(rig["home"]),                       # isolate pretrust's ~/.claude.json write
        "PATH": f"{rig['stub']}:{os.environ['PATH']}",  # the stub agent, for start-session.sh
        "SL_RUN_ROOT": str(rig["run_root"]),
        "SL_REPO": str(rig["repo"]),
        "SL_DEV_BRANCH": "main",
        "SL_HERDR": str(rig["host"]),
        "STUB_DIR": str(rig["stub"]),
        "SHIM_PATH": SHIM_PATH,
        "STUB_MODE": mode,
        "SL_LAUNCH_VERIFY_SECONDS": "10",
        # The stub agent lingers just long enough for the wrapper's process-fact confirmation to
        # see a live child, then exits — so a case that RELAUNCHES can wait for the prior session
        # to end. The worker singleton is real here: a second worker for a live lane is refused,
        # exactly as in production (where the runner frees the lock first, _close_stale_session).
        "STUB_AGENT_SECONDS": "6",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["python3", LAUNCH, *args], env=env, capture_output=True, text=True,
                          timeout=timeout)


def _wait_for(path, seconds=25):
    """The launcher returns the moment the floor's sentinel proves a worker STARTED — which is
    deliberately before the agent process itself is up (the sentinel is stamped first, so a
    session that dies at startup is still observed as having started). A test that inspects what
    the agent received therefore has to wait for the agent, not for the launcher."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def _pane_env(rig):
    """The environment the pane was actually handed, as argv carried it."""
    out = {}
    path = rig["stub"] / "pane.env"
    for line in path.read_text().splitlines() if path.exists() else []:
        key, _, value = line.partition("=")
        out[key] = value
    return out


# --------------------------------------------------------------------------- the delivered path

def test_a_delivered_launch_runs_the_real_floor_and_records_liveness(rig):
    r = _launch(rig)
    assert r.returncode == 0, f"rc={r.returncode}\nOUT:{r.stdout}\nERR:{r.stderr}"
    run_root = rig["run_root"]
    assert (run_root / "state" / "activity" / "i1").exists(), "the freeze-net baseline is stamped"
    assert (run_root / "state" / "panes" / "i1").read_text() == "w9:p1"
    assert (run_root / "state" / "panes" / "i1.ws").read_text() == "w9"
    assert (run_root / "worktrees" / "i1").is_dir(), "the worktree was created off origin/main"
    # ...and the agent really ran, in the pane, with the brief — through the shim and the floor.
    assert _wait_for(rig["stub"] / "claude_args"), (rig["stub"] / "pane.log").read_text()
    args = (rig["stub"] / "claude_args").read_text()
    assert "do the thing" in args, "the brief reached the agent as its opening prompt"
    assert "--dangerously-skip-permissions" in args


def test_the_pane_ran_our_floor_and_not_a_bare_agent(rig):
    """The handoff is the whole reason the floor survives the port: the host types the agent verb,
    and start-session.sh is what answers it."""
    r = _launch(rig)
    assert r.returncode == 0, r.stderr
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    env = (rig["stub"] / "claude_env").read_text()
    assert "SL_ISSUE_ID=i1" in env, "the agent ran inside the floor's own environment"
    assert "SL_START_SESSION=" not in env, \
        "the floor disarms the handoff, so a nested shell cannot re-launch the lane"
    # The floor's per-launch sentinel was stamped and then OBSERVED (cleaned up on success).
    assert not list((rig["run_root"] / "state" / "started").glob("i1.*"))


def test_a_relaunch_reuses_the_worktree_and_never_touches_its_work(rig):
    """THE preservation rule (#190/#168): the launcher only ever CREATES a missing worktree."""
    assert _launch(rig).returncode == 0
    wip = rig["run_root"] / "worktrees" / "i1" / "unpushed.txt"
    wip.write_text("work nobody has seen")
    assert _wait_for(rig["run_root"] / "state" / "exited" / "i1", 30), "the prior session ended"
    assert _launch(rig).returncode == 0
    assert wip.read_text() == "work nobody has seen"


def test_a_relaunch_clears_run_markers_but_keeps_delivery_receipts(rig):
    run_root = rig["run_root"]
    mail = run_root / "state" / "mail"
    mail.mkdir(parents=True, exist_ok=True)
    (mail / "i1").write_text("a dead session's instruction")
    (mail / "i1.consumed.1700000000").write_text("what was really delivered last run")
    (run_root / "reports" / "i1.md").write_text("a prior session's report")
    assert _launch(rig).returncode == 0
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    assert not (mail / "i1").exists()
    assert not (run_root / "reports" / "i1.md").exists()
    assert (mail / "i1.consumed.1700000000").exists(), "history survives a restart"


# --------------------------------------------------------------------------- non-delivery

def test_a_hollow_launch_is_never_reported_as_a_launch(rig):
    """The measured phantom: the host reports the agent started and ready while nothing is behind
    it. The wrapper's process-fact confirmation catches it; the launcher must not stamp liveness."""
    r = _launch(rig, mode="hollow")
    assert r.returncode == 2, f"rc={r.returncode}\n{r.stderr}"
    assert "LAUNCH NOT DELIVERED" in r.stderr
    assert not (rig["run_root"] / "state" / "activity" / "i1").exists(), \
        "a hollow launch must never fabricate liveness"
    assert (rig["stub"] / "closed").exists(), "the pane it created is torn down, not left running"


def test_a_refused_spawn_is_a_delivery_channel_fault(rig):
    r = _launch(rig, mode="refuse")
    assert r.returncode == 2, r.stderr
    assert not (rig["run_root"] / "state" / "activity" / "i1").exists()


def test_a_missing_brief_refuses_before_any_pane_exists(rig):
    (rig["run_root"] / "briefs" / "i1.md").unlink()
    r = _launch(rig)
    assert r.returncode == 1
    assert "missing brief" in r.stderr           # evidence.py's brief_missing needle
    assert not (rig["stub"] / "pane.env").exists(), "no pane was created"


def test_a_missing_base_ref_exits_3_and_opens_nothing(rig):
    r = _launch(rig, extra_env={"SL_DEV_BRANCH": "nope"})
    assert r.returncode == 3, f"rc={r.returncode}\n{r.stderr}"
    assert "origin/nope" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


# --------------------------------------------------------------------------- the mode guards

def test_the_cwd_mode_refuses_an_issue_id(rig):
    r = _launch(rig, args=("--cwd", str(rig["repo"]), "i1"))
    assert r.returncode == 1
    assert "debugger (d<N>) ids only" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_the_worker_mode_refuses_a_debugger_id(rig):
    (rig["run_root"] / "briefs" / "d7.md").write_text("diagnose")
    r = _launch(rig, args=("d7",))
    assert r.returncode == 1
    assert "worker mode expects an issue id" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_the_worker_mode_refuses_a_triage_id(rig):
    """The third id shape, through the real CLI (issue #448). A t<N> routed as a worker would take
    a lane's worktree and bump an issue counter that names nothing."""
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("t3",))
    assert r.returncode == 1
    assert "worker mode expects an issue id" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_the_triage_mode_refuses_an_issue_id(rig):
    r = _launch(rig, args=("--triage", "i1"))
    assert r.returncode == 1
    assert "triage mode expects a triage id" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_the_triage_mode_refuses_a_debugger_id(rig):
    """The crossing that matters most: a d<N> is the class the fence GRANTS its token to, and a
    triage flight is a tokenless session that acts on the queue unattended."""
    (rig["run_root"] / "briefs" / "d7.md").write_text("diagnose")
    r = _launch(rig, args=("--triage", "d7"))
    assert r.returncode == 1
    assert "triage mode expects a triage id" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_the_cwd_mode_refuses_a_triage_id(rig):
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("--cwd", str(rig["repo"]), "t3"))
    assert r.returncode == 1
    assert "debugger (d<N>) ids only" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


# ------------------------------------------------------- the t<N> session class (issue #448)

def test_the_triage_flight_runs_in_the_repos_real_checkout(rig):
    """The RULED default home: the flight opens the checkout an orchestrator would open, so it
    sees the gitignored working files a fresh worktree by definition cannot show."""
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    (rig["repo"] / "notes.local").write_text("a gitignored working file\n")
    r = _launch(rig, args=("--triage", "t3"))
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert not (rig["run_root"] / "worktrees").exists(), "the checkout home creates no worktree"
    assert (rig["stub"] / "cwd").read_text() == os.path.realpath(str(rig["repo"]))
    assert (rig["run_root"] / "state" / "panes" / "t3").read_text() == "w9:p1"
    assert (rig["repo"] / "notes.local").exists(), "the launch writes nothing in the working tree"


def test_a_worktree_home_gives_the_flight_a_detached_checkout(rig):
    """The opt-out for a repo whose gitignored overlay is sensitive. DETACHED, because the flight
    never commits, never pushes and must never create a ref of its own."""
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("--triage", "t3"), extra_env={"SL_TRIAGE_HOME": "worktree"})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    wt = rig["run_root"] / "worktrees" / "t3"
    assert wt.is_dir()
    assert (rig["stub"] / "cwd").read_text() == str(wt)
    head = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode != 0, "a triage worktree is detached — it is on no branch at all"


def test_a_triage_home_the_launcher_cannot_read_refuses_rather_than_choosing_one(rig):
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("--triage", "t3"), extra_env={"SL_TRIAGE_HOME": "Checkout"})
    assert r.returncode == 1
    assert "unknown triage home" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_a_triage_flight_provably_never_receives_the_token(rig):
    """DoD: no fence token and no host env variables reach a t<N> pane — asserted where it is
    decided, in the environment the pane was actually handed."""
    import session_host
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("--triage", "t3"))
    assert r.returncode == 0, r.stderr
    env = _pane_env(rig)
    assert session_host.API_TOKEN_ENV_VAR not in env
    assert session_host.API_TOKEN_FILE_ENV_VAR not in env
    assert not [k for k in env if k.startswith("HERDR")], \
        "no host variable of ours reaches a triage pane"
    assert env["SL_ISSUE_ID"] == "t3"


def test_a_triage_flight_bumps_no_issue_counter(rig):
    import loopstate
    (rig["run_root"] / "briefs" / "t3.md").write_text("triage the queue")
    r = _launch(rig, args=("--triage", "t3"))
    assert r.returncode == 0, r.stderr
    st = loopstate.load(str(rig["run_root"] / "state" / "issues.json"))
    assert "t3" not in st["issues"]
    assert st["issues"]["i1"].get("launches", 0) == 0


# --------------------------------------------------------------------------- the two d<N> paths

def test_the_debugger_path_launches_in_place_with_no_worktree(rig):
    """The watchdog's unattended repair and the dashboard Fixer's owner tap both land here."""
    (rig["run_root"] / "briefs" / "d7.md").write_text("diagnose the wedge")
    r = _launch(rig, args=("--cwd", str(rig["repo"]), "d7"))
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert not (rig["run_root"] / "worktrees").exists(), "a d<N> creates no worktree"
    assert (rig["run_root"] / "state" / "panes" / "d7").read_text() == "w9:p1"
    assert (rig["stub"] / "cwd").read_text() == os.path.realpath(str(rig["repo"]))


def test_a_debugger_receives_the_control_socket_token_at_spawn(rig):
    """The fence's token wiring, realized where it is decided: the pane's own environment."""
    import session_host
    (rig["run_root"] / "briefs" / "d7.md").write_text("diagnose the wedge")
    r = _launch(rig, args=("--cwd", str(rig["repo"]), "d7"))
    assert r.returncode == 0, r.stderr
    env = _pane_env(rig)
    assert env.get(session_host.API_TOKEN_ENV_VAR) == session_host.GRANT_SENTINEL, \
        "a repair session must be able to drive the host"


def test_a_worker_provably_never_receives_the_token(rig):
    import session_host
    r = _launch(rig)
    assert r.returncode == 0, r.stderr
    env = _pane_env(rig)
    assert session_host.API_TOKEN_ENV_VAR not in env
    assert session_host.API_TOKEN_FILE_ENV_VAR not in env
    assert not [k for k in env if k.startswith("HERDR")], \
        "no host variable of ours reaches a worker pane"


# --------------------------------------------------------------------------- the floor's refusals

def test_a_worker_with_dead_gh_auth_refuses_from_inside_its_own_environment(rig):
    r = _launch(rig, extra_env={"STUB_GH_WORKER_LOGIN": "DEAD"})
    assert r.returncode == 4, f"rc={r.returncode}\n{r.stderr}"
    assert "GH AUTH DEAD" in r.stderr
    assert not (rig["stub"] / "claude_args").exists(), "the agent never ran"
    assert not (rig["run_root"] / "state" / "activity" / "i1").exists()


def test_a_worker_authed_as_the_wrong_account_is_refused(rig):
    r = _launch(rig, extra_env={"STUB_GH_WORKER_LOGIN": "someone-else"})
    assert r.returncode == 4, f"rc={r.returncode}\n{r.stderr}"
    assert "someone-else" in r.stderr


def test_the_runners_own_dead_gh_refuses_before_anything_is_created(rig):
    r = _launch(rig, extra_env={"STUB_GH_LOGIN": "DEAD"})
    assert r.returncode == 5, f"rc={r.returncode}\n{r.stderr}"
    assert "GH AUTH DEAD (runner env)" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()
    assert not (rig["run_root"] / "worktrees").exists(), "not even a worktree is left behind"


def test_poison_the_pane_shell_injects_never_reaches_the_agent(rig):
    """#301's floor, still doing its job on the new path — and still the only code that CAN, since
    it is the only code that runs in the session's own environment.

    The poison enters exactly where it really entered: the pane's own shell startup, AFTER the
    launcher has finished (the realized key lived at ~/.zshrc:5). The stub pane runs `zsh -c`,
    which reads .zshenv rather than .zshrc — what matters is the WHERE, not which of zsh's files
    carries it. A launcher that scrubbed itself would prove nothing about any of this."""
    zdot = rig["stub"] / "zdot"
    zdot.mkdir()
    (zdot / ".zshenv").write_text("export ANTHROPIC_API_KEY=sk-live-injected-by-the-shell\n"
                                  "export CLAUDECODE=1\n")
    r = _launch(rig, extra_env={"ZDOTDIR": str(zdot)})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    # What the AGENT'S OWN PROCESS was handed, three processes down the spawn chain. That is what
    # would have been billed, and what would have started with transcript saving off.
    agent_env = (rig["stub"] / "claude_env").read_text()
    assert "ANTHROPIC_API_KEY" not in agent_env
    assert "CLAUDECODE" not in agent_env


def test_an_unscrubbable_poison_refuses_the_flight_with_the_env_code(rig):
    """The post-scrub ASSERT, which is not a restatement of the scrub: a readonly variable `unset`
    cannot remove is exactly what it exists to catch. bash sources $BASH_ENV in a non-interactive
    shell, so this makes the variable readonly in start-session.sh's own process — the scrub then
    genuinely fails, and the flight must be refused rather than flown."""
    zdot = rig["stub"] / "zdot"
    zdot.mkdir()
    bashenv = rig["stub"] / "bashenv.sh"
    bashenv.write_text("readonly ANTHROPIC_API_KEY=sk-live-and-unremovable\n"
                       "export ANTHROPIC_API_KEY\n")
    (zdot / ".zshenv").write_text(f"export BASH_ENV={bashenv}\n")
    r = _launch(rig, extra_env={"ZDOTDIR": str(zdot)})
    assert r.returncode == 6, f"rc={r.returncode}\n{r.stderr}"
    assert "ENV POISONED" in r.stderr
    assert "ANTHROPIC_API_KEY" in r.stderr, "the memo must name the variable"
    assert not (rig["stub"] / "claude_args").exists(), "the agent never ran"
    assert not (rig["run_root"] / "state" / "activity" / "i1").exists()


def test_no_poison_the_launcher_holds_is_forwarded_into_a_pane(rig):
    r = _launch(rig, extra_env={"ANTHROPIC_API_KEY": "sk-live-should-never-travel",
                                "ANTHROPIC_BASE_URL": "http://gateway.invalid",
                                "CLAUDECODE": "1"})
    assert r.returncode == 0, r.stderr
    env = _pane_env(rig)
    for poison in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDECODE"):
        assert poison not in env, f"{poison} was handed to the pane by the launcher"


# --------------------------------------------------------------------------- identity

def test_a_launch_mints_a_session_id_records_it_and_names_it_to_the_pane(rig):
    r = _launch(rig)
    assert r.returncode == 0, r.stderr
    recorded = (rig["run_root"] / "state" / "sessions" / "i1").read_text().strip()
    assert len(recorded) == 36, recorded
    assert _pane_env(rig)["SL_SESSION_ID"] == recorded
    assert _wait_for(rig["stub"] / "claude_args"), (rig["stub"] / "pane.log").read_text()
    assert "--session-id" in (rig["stub"] / "claude_args").read_text()


def test_the_id_is_recorded_even_when_delivery_never_lands(rig):
    """A host that dies inside the verify window must still leave the handle to resume by."""
    r = _launch(rig, mode="hollow")
    assert r.returncode == 2
    assert (rig["run_root"] / "state" / "sessions" / "i1").read_text().strip()


def test_a_resume_reuses_the_recorded_id_and_opens_on_its_own_brief(rig):
    sid = "11111111-2222-3333-4444-555555555555"
    (rig["run_root"] / "briefs" / "i1.resume.md").write_text("you were interrupted")
    r = _launch(rig, extra_env={"SL_RESUME_SESSION_ID": sid})
    assert r.returncode == 0, r.stderr
    assert _wait_for(rig["stub"] / "claude_args"), (rig["stub"] / "pane.log").read_text()
    args = (rig["stub"] / "claude_args").read_text()
    assert "--resume" in args and sid in args
    assert "you were interrupted" in args
    assert (rig["run_root"] / "briefs" / "i1.md").read_text() == "do the thing", \
        "the lane's original brief is left untouched beside the preamble"


def test_a_resume_without_its_preamble_aborts_rather_than_substituting_the_real_brief(rig):
    r = _launch(rig, extra_env={"SL_RESUME_SESSION_ID": "11111111-2222-3333-4444-555555555555"})
    assert r.returncode == 1
    assert "missing brief" in r.stderr
    assert not (rig["stub"] / "pane.env").exists()


def test_a_codex_repo_mints_no_session_id_it_could_never_spend(rig):
    r = _launch(rig, extra_env={"SL_AGENT": "codex", "STUB_AGENT_VERB": "codex"})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert not (rig["run_root"] / "state" / "sessions" / "i1").exists()
    assert _wait_for(rig["stub"] / "codex_args"), (rig["stub"] / "pane.log").read_text()


def test_a_verified_launch_bumps_the_counter_and_a_failed_one_does_not(rig):
    import loopstate
    assert _launch(rig).returncode == 0
    st = loopstate.load(str(rig["run_root"] / "state" / "issues.json"))
    assert st["issues"]["i1"]["launches"] == 1 and st["issues"]["i1"]["retries"] == 0
    assert _wait_for(rig["run_root"] / "state" / "exited" / "i1", 30)
    assert _launch(rig, mode="hollow").returncode == 2
    st = loopstate.load(str(rig["run_root"] / "state" / "issues.json"))
    assert st["issues"]["i1"]["launches"] == 1, "an unverified launch counts nothing"


def test_the_claude_pin_rides_across_the_spawn_only_when_it_is_set(rig):
    r = _launch(rig)
    assert r.returncode == 0, r.stderr
    assert "SL_CLAUDE" not in _pane_env(rig)
    assert _wait_for(rig["run_root"] / "state" / "exited" / "i1", 30)
    pin = rig["stub"] / "claude"
    r = _launch(rig, extra_env={"SL_CLAUDE": str(pin)})
    assert r.returncode == 0, r.stderr
    assert _pane_env(rig)["SL_CLAUDE"] == str(pin)


def test_an_inherited_config_dir_redirect_never_reaches_the_launch(rig):
    """#299's asymmetry, and the reason the cmux launcher unset this one variable.

    `gh` resolves its config dir from XDG_CONFIG_HOME and the session's own floor REMOVES it. If
    the launcher kept an inherited value while the worker dropped it, the two `gh api user` reads
    would consult different config dirs — and #299 compares their answers, so every launch would
    refuse with a mismatch that names no real fault. The session host's CLI resolves its socket
    from it too, so a launch under it cannot even reach the host."""
    r = _launch(rig, extra_env={"XDG_CONFIG_HOME": "/nonexistent/poisoned-config"})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert "XDG_CONFIG_HOME" not in _pane_env(rig)
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    assert "XDG_CONFIG_HOME" not in (rig["stub"] / "claude_env").read_text()


# ---- the identity env contract, across the whole spawn (issue #314) ----------------------------
# Only an end-to-end drive can show this one, because the whole subject is a string surviving three
# processes: the launcher derives it, the wrapper carries it in the pane's environment, the pane's
# own shell (which sources the operator's rc files) hands it to start-session.sh, and the floor
# turns it into the agent's own CLAUDE_CONFIG_DIR. Every hop is a place a spelling could change,
# and a changed spelling is a different credential namespace that reports LOGGED OUT (#300).
# A config dir a shell rc file INJECTS — never one this suite provisions, and deliberately a path
# that exists nowhere: the one case still using it proves such a dir is refused rather than adopted,
# so nothing ever reads or writes it. The cases that need a REAL assigned dir take the `fleet_dir`
# fixture below instead (issue #345 gave that dir a file to hold, so it has to be per-run).
_E2E_FLEET_DIR = "/tmp/sl-i314-e2e-fleet"


def _auth_reads(rig):
    """Who asked `claude auth status`, and under which config dir, in order."""
    path = rig["stub"] / "claude_auth_reads"
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def fleet_dir(tmp_path):
    """The assigned config dir, PROVISIONED — which is what a real one is (an interactive login
    creates it, #313).

    It has to exist on disk now (issue #345): pre-trust writes the worktree's trust record into the
    file the assigned dir holds, and it REFUSES a dir nobody provisioned rather than creating one —
    a session launched into a config dir that does not exist runs first-run and parks at the theme
    picker, which no trust key can close, so pretrust cannot deliver its guarantee there and says so
    instead.

    Under `tmp_path` rather than the shared `/tmp` constant these cases used while the dir was only
    ever a STRING (cross-review, P2): now that a real config file is written into it, a fixed path
    would be state two runs could share. `tmp_path` is absolute and already canonical, which is all
    the byte-for-byte propagation assertions need.
    """
    d = tmp_path / "assigned-claude-config"
    d.mkdir()
    return str(d)


def _fleet_trusted(fleet_dir):
    """The folders the ASSIGNED config dir records as trusted — [] if it has no config file."""
    path = os.path.join(fleet_dir, ".claude.json")
    if not os.path.exists(path):
        return []
    import json as _json
    with open(path) as f:
        data = _json.load(f)
    return sorted(k for k, v in (data.get("projects") or {}).items()
                  if v.get("hasTrustDialogAccepted") is True)


def test_a_fleet_launch_pretrusts_the_worktree_in_the_file_its_own_session_will_read(rig, fleet_dir):
    """Issue #345, driven through the real launcher process rather than asserted about its argv.

    #311 measured the record going to the operator's DEFAULT config while the worker read a
    per-worker one — a pre-trust that exists and does nothing, on EVERY launch, because every issue
    gets a fresh worktree. Only an end-to-end drive shows the two halves at once: which file the
    pre-flight wrote, and which config dir the agent that started in that worktree was handed.
    """
    r = _launch(rig, extra_env={"SL_FLEET_CLAUDE_CONFIG_DIR": fleet_dir,
                                "STUB_CLAUDE_LOGGED_IN_DIR": fleet_dir})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    worktree = os.path.realpath(os.path.join(str(rig["run_root"]), "worktrees", "i1"))
    assert _fleet_trusted(fleet_dir) == [worktree]
    # ...and the operator's own store was not the one edited. (HOME here is the rig's isolated one,
    # which is exactly the file the pre-#345 step would have written.)
    assert not os.path.exists(os.path.join(str(rig["home"]), ".claude.json"))
    # The two halves meet: the agent really did start under the config dir that now holds the
    # record. Same dir, same string, one launch.
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    assert ("CLAUDE_CONFIG_DIR=" + fleet_dir + "\n") in (rig["stub"] / "claude_env").read_text()


def test_a_machine_with_no_assignment_still_pretrusts_the_operators_own_store(rig):
    """The unchanged path, end to end: no assignment means the default file, exactly as before."""
    r = _launch(rig)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    worktree = os.path.realpath(os.path.join(str(rig["run_root"]), "worktrees", "i1"))
    assert _fleet_trusted(str(rig["home"])) == [worktree]


def test_the_assigned_config_dir_survives_the_whole_spawn_unchanged(rig, fleet_dir):
    r = _launch(rig, extra_env={"SL_FLEET_CLAUDE_CONFIG_DIR": fleet_dir,
                                "STUB_CLAUDE_LOGGED_IN_DIR": fleet_dir})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    # The launcher NAMES it as SL_*; CLAUDE_CONFIG_DIR itself is the agent's variable and is set by
    # the floor, one process later, inside the session's own shell.
    assert _pane_env(rig)["SL_CLAUDE_CONFIG_DIR"] == fleet_dir
    assert "CLAUDE_CONFIG_DIR=" + fleet_dir not in _pane_env(rig)
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    agent_env = dict(line.partition("=")[::2]
                     for line in (rig["stub"] / "claude_env").read_text().splitlines() if "=" in line)
    assert agent_env["CLAUDE_CONFIG_DIR"] == fleet_dir
    # Both reads consulted the SAME namespace — the launcher's, and the session's own. Two spellings
    # here would be two identities, and the session's would simply report logged out.
    reads = _auth_reads(rig)
    assert ("launcher=" + fleet_dir) in reads
    assert ("i1=" + fleet_dir) in reads


def test_a_non_canonical_assignment_is_canonicalised_before_anything_downstream_sees_it(
        rig, fleet_dir):
    r = _launch(rig, extra_env={"SL_FLEET_CLAUDE_CONFIG_DIR": fleet_dir + "/",
                                "STUB_CLAUDE_LOGGED_IN_DIR": fleet_dir})
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert _pane_env(rig)["SL_CLAUDE_CONFIG_DIR"] == fleet_dir
    assert _wait_for(rig["stub"] / "claude_env"), (rig["stub"] / "pane.log").read_text()
    assert ("CLAUDE_CONFIG_DIR=" + fleet_dir + "\n") in (rig["stub"] / "claude_env").read_text()


def test_an_account_that_is_right_here_and_wrong_in_the_session_refuses_the_flight(rig, fleet_dir):
    """The asymmetry only an end-to-end drive can produce, and the exact shape the contract exists
    for: the launcher's own read is healthy, and the SESSION's is a different account. A launcher
    that checked only itself would have flown this."""
    r = _launch(rig, extra_env={"SL_FLEET_CLAUDE_CONFIG_DIR": fleet_dir,
                                "STUB_CLAUDE_LOGGED_IN_DIR": fleet_dir,
                                "STUB_CLAUDE_WORKER_ORG": "somebody-elses-org"})
    assert r.returncode == 7, f"rc={r.returncode}\n{r.stderr}"
    assert "CLAUDE IDENTITY REFUSED" in r.stderr
    assert "somebody-elses-org" in r.stderr and "fleet-org" in r.stderr
    assert not (rig["stub"] / "claude_env").exists(), "the agent must never have started"


def test_a_credential_redirect_the_pane_shell_injects_refuses_the_flight(rig, fleet_dir):
    """#300 landmine 2 entering exactly where the realized API key entered — the pane's own shell
    startup, after the launcher is gone. Set-but-EMPTY collapses the credential namespace back to
    the owner's unsuffixed default, so this session would have spent the owner's subscription with
    nothing anywhere erroring."""
    zdot = rig["stub"] / "zdot"
    zdot.mkdir()
    (zdot / ".zshenv").write_text("export CLAUDE_SECURESTORAGE_CONFIG_DIR=\n")
    r = _launch(rig, extra_env={"ZDOTDIR": str(zdot),
                                "SL_FLEET_CLAUDE_CONFIG_DIR": fleet_dir,
                                "STUB_CLAUDE_LOGGED_IN_DIR": fleet_dir})
    assert r.returncode == 7, f"rc={r.returncode}\n{r.stderr}"
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" in r.stderr
    assert not (rig["stub"] / "claude_env").exists(), "the agent must never have started"


def test_a_config_dir_the_pane_shell_injects_is_refused_not_adopted(rig):
    """An unassigned machine plus a shell rc that exports one is identity picked up rather than
    assigned (claim c3) — and it is silently a different credential namespace from the one the
    launcher measured."""
    zdot = rig["stub"] / "zdot"
    zdot.mkdir()
    (zdot / ".zshenv").write_text("export CLAUDE_CONFIG_DIR=%s\n" % _E2E_FLEET_DIR)
    r = _launch(rig, extra_env={"ZDOTDIR": str(zdot)})
    assert r.returncode == 7, f"rc={r.returncode}\n{r.stderr}"
    assert "was not assigned" in r.stderr
    assert not (rig["stub"] / "claude_env").exists()
