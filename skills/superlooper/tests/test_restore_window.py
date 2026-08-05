"""herdr's restore window (issue #317, herdrdev/herdr#2065): `agent_not_found` after a server
restart means WAIT, never "this session died".

Upstream closed #2065 as `duplicate` and it is NOT fixed on the pinned release. After the host
server restarts, `persist.restore` puts every agent's RECORD back long before the pane's runtime
exists — so for the length of that window the state surface says the agent is fine and the action
surface says it is missing:

    agent list / agent get      ->  idle, interactive_ready=true      <- lies
    pane process-info --pane …  ->  ERROR pane_not_found              <- honest
    agent prompt … --wait       ->  ERROR agent_not_found             <- honest
    the pid recorded at spawn   ->  gone (the whole tree died)

The hazard is not the inconsistency, it is what a caller CONCLUDES from it: `agent_not_found` is
the same error a genuinely-dead agent produces, and a wrapper that read it as "the session is gone"
would tear down or cold-restart a flight that was seconds away from coming back on its own.

**The discriminator, measured on the pinned build from both sides** (reports/i317.md carries the
transcripts). Only the restore window has the host resolving the NAME while refusing the PANE:

    surface                | restore window | agent killed  | workspace closed
    -----------------------|----------------|---------------|-----------------
    agent get <name>       | ok (idle)      | not_found     | not_found
    pane process-info      | pane_not_found | ok, bare zsh  | pane_not_found
    agent prompt           | agent_not_found| agent_not_found | agent_not_found

So the evidence this wrapper acts on is a PROCESS FACT (no runtime behind the pane) plus the host's
own restore bookkeeping (the name still resolves) — never `agent list`'s status field, which is the
one surface proven to lie here.

**The measured window on the pinned build** is recorded in `test_the_measured_window_is_recorded`
and in the opt-in real drill at the bottom of this file; a version bump that moves it fails there
visibly rather than silently eating the default bound.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import time

import pytest

import herdr_hook
import session_host


# --------------------------------------------------------------------- the fake host
# The envelopes are transcribed from the real drill, not invented: `agent get` answering while
# `pane process-info` refuses is the whole point, and a fake that answered both would prove nothing.

def _ok(payload):
    return json.dumps({"id": "1", "result": payload})


def _err(code, message):
    return json.dumps({"id": "1", "error": {"code": code, "message": message}})


def _agent(name="i317", pane="w1:p1", status="idle", kind="agent_info", ready=True):
    return _ok({
        "type": kind,
        "agent": {"name": name, "pane_id": pane, "agent_status": status, "workspace_id": "w1",
                  "tab_id": "w1:t1", "interactive_ready": ready, "agent": "claude"},
    })


def _process_info(pane="w1:p1", shell_pid=4242):
    return _ok({"type": "pane_process_info",
                "process_info": {"pane_id": pane, "shell_pid": shell_pid,
                                 "foreground_processes": [{"pid": 4243, "name": "claude"}]}})


# What the host actually said during the window, byte-shape for byte-shape.
PANE_GONE = _err("pane_not_found", "pane not found")
AGENT_GONE = _err("agent_not_found", "no agent named i317")


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeHerdr:
    """The host CLI and the OS process facts around it.

    ``script`` maps the first two argv words after the binary to a ``(rc, stdout, stderr)`` triple
    or to a LIST of them consumed in order — which is how a restore window is staged: the same read
    answers `not_found` while the window is open and normally once it has closed.
    """

    def __init__(self, script=None, alive=(), children=None):
        self.script = {k: list(v) if isinstance(v, list) else v for k, v in (script or {}).items()}
        self.alive = set(alive)
        self.children = dict(children or {})
        self.calls = []
        self.slept = 0.0

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        entry = self.script.get(tuple(argv[1:3]), (1, "", _err("unscripted", " ".join(argv[1:3]))))
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        rc, out, err = entry
        return _Completed(rc, out, err)

    def verbs(self, *pair):
        return [c for c in self.calls if tuple(c[1:3]) == pair]

    def pid_alive(self, pid):
        return int(pid) in self.alive

    def child_pids(self, pid):
        return self.children.get(int(pid))

    def sleep(self, seconds):
        self.slept += seconds


class FakeDelivery:
    """The transcript-side oracle: ``answers`` are consumed in order, the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [True]
        self.checks = 0

    def mark(self):
        return "mark"

    def landed(self, mark):
        self.checks += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _host(fake, **kw):
    kw.setdefault("restore_wait_seconds", 10.0)
    kw.setdefault("restore_poll_seconds", 1.0)
    return session_host.SessionHost(probe=fake, binary="/nonexistent/superlooper-test-herdr", **kw)


def _session(**over):
    kwargs = dict(name="i317", workspace="w1", tab="w1:t1", pane="w1:p1", shell_pid=4242)
    kwargs.update(over)
    return session_host.Session(**kwargs)


def _window(prompts, gets=None, panes=None):
    """A host staged in the restore window: the name resolves, the pane has no runtime."""
    return FakeHerdr(script={
        ("agent", "prompt"): prompts,
        ("agent", "get"): gets or (0, _agent(), ""),
        ("pane", "process-info"): panes or (1, "", PANE_GONE),
    })


_REFUSED = (1, "", AGENT_GONE)
_PROMPTED = (0, _agent(kind="agent_prompted", status="working"), "")


# --------------------------------------------------------------------- the vocabulary

def test_restoring_is_its_own_verdict_and_is_neither_alive_nor_dead():
    # A restore window is not "probably alive" and not "probably dead" — those are the two readings
    # that cost work. It gets its own word so no caller can accidentally spend a teardown on it.
    assert session_host.RESTORING not in (session_host.ALIVE, session_host.DEAD,
                                          session_host.UNKNOWN)


# --------------------------------------------------------------------- send: the wait

def test_a_prompt_refused_during_the_restore_window_is_waited_out_rather_than_reported_gone():
    # The measured sequence: the host refuses while its own pane has no runtime, then the restore
    # completes and the very same prompt is accepted and proven delivered.
    fake = _window(
        prompts=[_REFUSED, _REFUSED, _PROMPTED],
        panes=[(1, "", PANE_GONE), (1, "", PANE_GONE), (0, _process_info(), "")])
    fake.alive.add(4242)
    fake.children[4242] = [4243]

    sent = _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False, False, True))

    assert sent.delivered is True
    assert sent.restored is True, "the caller has to learn its session came back through a restore"
    assert fake.slept > 0, "the wrapper must actually wait out the window"


def test_the_wait_ends_loudly_with_its_own_error_rather_than_a_silent_retry_loop():
    fake = _window(prompts=[_REFUSED])
    with pytest.raises(session_host.RestoreWindowExpired) as caught:
        _host(fake, restore_wait_seconds=3.0).send(_session(), "carry on",
                                                   delivery=FakeDelivery(False))
    message = str(caught.value)
    assert "restore" in message.lower()
    assert "3" in message, "the bound that expired has to be in the memo: %s" % message
    # Park-worthy, but NOT the oracle's error: nothing was typed, so a caller holding a one-shot
    # nudge must not spend it blaming a worker for ignoring a message the host never carried.
    assert not isinstance(caught.value, session_host.DeliveryUnproven)


def test_an_agent_the_host_no_longer_resolves_is_not_waited_on_at_all():
    # The control case, measured: a killed agent CLEARS its name, so `agent get` refuses too. That
    # is the genuinely-absent signature and it must cost zero seconds.
    fake = _window(prompts=[_REFUSED], gets=(1, "", AGENT_GONE))
    with pytest.raises(session_host.HostError) as caught:
        _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False))
    assert not isinstance(caught.value, session_host.RestoreWindowExpired)
    assert fake.slept == 0, "a genuinely absent agent is not a window to wait out"
    assert len(fake.verbs("agent", "prompt")) == 1, "and the prompt is not re-issued"


def test_a_pane_the_host_still_backs_is_not_a_restore_window():
    # The other measured control: kill the agent and the pane survives as a bare shell, so
    # `pane process-info` ANSWERS. A host that can still report the pane's processes is not
    # mid-restore, whatever else is wrong.
    fake = _window(prompts=[_REFUSED], panes=(0, _process_info(), ""))
    fake.alive.add(4242)
    fake.children[4242] = []
    with pytest.raises(session_host.HostError) as caught:
        _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False))
    assert not isinstance(caught.value, session_host.RestoreWindowExpired)
    assert fake.slept == 0


def test_a_host_that_never_answered_is_not_mistaken_for_a_restore_window():
    # Absence of signal is UNKNOWN (c2). A timeout produces no envelope at all, and reading that as
    # "it must be restoring" would make a dead host look like a recovering one for the whole bound.
    fake = _window(prompts=[(124, "", "no answer within 20s")],
                   gets=(124, "", "no answer within 20s"))
    with pytest.raises(session_host.HostError) as caught:
        _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False))
    assert not isinstance(caught.value, session_host.RestoreWindowExpired)
    assert fake.slept == 0


def test_a_refusal_that_is_not_not_found_is_reported_at_once():
    # The window's signature is the not-found spelling. Any other refusal is a different question
    # and a retried diagnosis is a slow diagnosis.
    fake = _window(prompts=[(1, "", _err("agent_prompt_failed", "write failed"))])
    with pytest.raises(session_host.HostError) as caught:
        _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False))
    assert not isinstance(caught.value, session_host.RestoreWindowExpired)
    assert fake.slept == 0


def test_a_prompt_the_host_accepted_is_never_re_issued_by_the_wait():
    # The retry is safe ONLY because it re-sends a call the host REFUSED — nothing was carried. The
    # moment the host accepts one, the loop has to stop, or a slow oracle turns into two prompts in
    # the pane.
    fake = _window(prompts=[_REFUSED, _PROMPTED, _PROMPTED],
                   panes=[(1, "", PANE_GONE), (0, _process_info(), "")])
    fake.alive.add(4242)
    fake.children[4242] = [4243]
    with pytest.raises(session_host.DeliveryUnproven):
        _host(fake).send(_session(), "carry on", delivery=FakeDelivery(False))
    assert len(fake.verbs("agent", "prompt")) == 2, (
        "one refused call, one accepted, and then no more: %s" % fake.verbs("agent", "prompt"))


def test_the_bound_is_configurable_by_the_caller_and_by_the_operators_environment():
    default = session_host.SessionHost(probe=FakeHerdr(), binary="/nonexistent/x")
    assert default.restore_wait_seconds == session_host.RESTORE_WAIT_SECONDS

    assert _host(FakeHerdr(), restore_wait_seconds=45.0).restore_wait_seconds == 45.0

    from_env = session_host.SessionHost(
        probe=FakeHerdr(), binary="/nonexistent/x",
        env={session_host.RESTORE_WAIT_ENV_VAR: "300"})
    assert from_env.restore_wait_seconds == 300.0

    # A junk override must not take the wrapper down at construction, and must not silently become
    # zero either — zero is "never wait", i.e. the exact behaviour this issue exists to remove.
    junk = session_host.SessionHost(probe=FakeHerdr(), binary="/nonexistent/x",
                                    env={session_host.RESTORE_WAIT_ENV_VAR: "soon"})
    assert junk.restore_wait_seconds == session_host.RESTORE_WAIT_SECONDS


def test_the_default_bound_clears_the_window_measured_on_the_pinned_build():
    # The worst reading on the pinned v0.8.0: 28.9s, from the host's own
    # `persist.restore`→`pane.spawned` log lines (8.2s in the drill below, 19.6s in a second log
    # reading). That is an order of magnitude past the 0.78–4.06s #302 recorded on the same build a
    # day earlier, which is exactly why the number is asserted rather than trusted. A build whose
    # window outgrows the default fails the real drill; this is the paired reminder in the fast
    # suite of what the default was chosen against.
    assert session_host.RESTORE_WAIT_SECONDS >= 4 * 28.9


# --------------------------------------------------------------------- state

def test_state_calls_a_restore_window_restoring_and_never_alive():
    # THE assertion behind "the state verb never reports an agent live on agent-list evidence alone
    # while no process backs it". The host is saying `idle` and `interactive_ready` here.
    fake = _window(prompts=[_REFUSED])
    state = _host(fake).state(_session())

    assert state.liveness == session_host.RESTORING
    assert state.liveness not in (session_host.ALIVE, session_host.DEAD)
    assert state.advisory == "idle", "the host's word is still CARRIED — it just gates nothing"
    assert state.name_resolves is True


def test_state_reports_dead_for_a_pane_the_host_still_backs_with_no_child():
    # The restore verdict must not swallow the genuinely-finished case: a pane whose shell answers
    # and has no live child is a bare shell, and that is what authorises `exit`.
    fake = _window(prompts=[_REFUSED], panes=(0, _process_info(), ""))
    fake.alive.add(4242)
    fake.children[4242] = []
    assert _host(fake).state(_session()).liveness == session_host.DEAD


def test_exit_refuses_a_session_that_is_still_restoring():
    fake = _window(prompts=[_REFUSED])
    with pytest.raises(session_host.TeardownRefused) as caught:
        _host(fake).exit(_session())
    assert session_host.RESTORING in str(caught.value)
    assert fake.verbs("workspace", "close") == [], (
        "a restoring session must not have its window closed — that is the lost-work failure")


# --------------------------------------------------------------- the real drill (opt-in)

_REAL = os.environ.get("SL_RESTORE_HERDR")


@pytest.fixture
def sock_dir():
    """A SHORT directory for unix sockets — pytest's tmp_path is past sun_path's 104 chars."""
    import tempfile
    path = tempfile.mkdtemp(prefix="slr-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _LabProbe(session_host.Probe):
    """The real Probe, with the lab's environment handed to every host call.

    ``SessionHost`` reads its own ``env`` for the binary and the fence token but runs the CLI on the
    parent's environment, which in a test is the conftest ratchet's deliberately-broken
    ``HERDR_SOCKET_PATH``. Production has no such split — the runner's own environment carries the
    socket path — so this is the drill's way of standing where the runner stands.
    """

    def __init__(self, env):
        self._env = dict(env)

    def run(self, argv, timeout=session_host._CALL_SECONDS):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  env=self._env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", "no answer within %ss" % timeout)
        except (OSError, ValueError):
            return subprocess.CompletedProcess(argv, 127, "", "could not run %s" % (argv[:1],))


@pytest.mark.skipif(not _REAL or not shutil.which("claude"),
                    reason="set SL_RESTORE_HERDR=<pinned herdr binary> (and have `claude` on PATH) "
                           "to run the real restore-window drill — every version bump does")
def test_the_wrapper_survives_a_real_server_restart(sock_dir):
    """Kill a real host, restart it headless, and drive the WRAPPER while the window is open.

    A mocked `agent_not_found` proves the branch; only this proves the branch is aimed at the thing
    that actually happens. Three assertions, all made while the contradiction is live:

    * ``state`` says RESTORING — not ALIVE (which `agent get` is inviting) and not DEAD;
    * ``exit`` refuses, so nothing closes the window of a session about to come back;
    * ``send`` WAITS and then raises ``RestoreWindowExpired`` on a deliberately short bound, rather
      than any verdict that reads "gone".

    **Why a real claude agent is needed.** The window only exists when herdr has an agent to
    resume: with no captured session id there is no restore plan, the pane comes back as a bare
    shell in ~3ms, and there is nothing to measure. Measured, first attempt. So the drill arms the
    lane the way the loop does — the carried vendor hook declared PROJECT-level (#307), the
    engine's own `pretrust.sh`, and one real turn so `claude --resume` has a transcript to find.

    **What it deliberately does not isolate.** ``HOME``: claude has to be the operator's
    authenticated claude or it parks on onboarding and captures no id. And it must NEVER set
    ``XDG_DATA_HOME`` — claude's launcher installs itself under it and relinks ``~/.local/bin/
    claude`` to point there, so a lab that sets it leaves the operator's claude a broken symlink
    when the lab is cleaned up. Both learned the hard way while building this (reports/i317.md).

    **The measured window on the pinned build** (v0.8.0, tag `v0.8.0`, commit `346411f`; Mac mini,
    2026-08-05): **8.2s** here, and **19.6s** / **28.9s** read off the host's own
    `persist.restore`→`pane.spawned` log lines while a probe grid was hammering it. Against the
    0.78–4.06s #302 recorded on the same build one day earlier. That spread is why the default
    bound is generous, and the assertion below is what makes a build that moves it fail visibly.
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    root = pathlib.Path(sock_dir)
    # The work dir is STABLE while the socket dir is fresh, and the split is deliberate: trust is
    # keyed on the folder path, so a per-run work dir would add a dead `hasTrustDialogAccepted`
    # entry to the operator's live ~/.claude.json at every version bump. One reused path, one entry.
    work = pathlib.Path("/tmp/superlooper-restore-drill")
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    sock = str(root / "h.sock")
    token = "restore-" + os.urandom(8).hex()
    # The engine's own hook contract, not a second spelling of it: `bash '<asset>' session`. Built
    # by hand this drill once pointed straight at the asset, which is carried mode 0644 — the hook
    # never ran, herdr captured no id, and the window it exists to measure did not exist.
    herdr_hook.write_settings(str(work / ".claude" / "settings.json"),
                              herdr_hook.vendored_script())
    subprocess.run(["bash", str(repo / "skill" / "bin" / "pretrust.sh"), str(work)], check=True)

    # An ALLOWLIST, not the ambient environment: this suite runs inside a claude pane of its own,
    # and an inherited CLAUDE_CODE_CHILD_SESSION made the lab's claude write no transcript at all,
    # which left `claude --resume` nothing to resume. What is kept is what a login shell and a real
    # claude actually need — `SHELL`/`USER`/`TMPDIR` included, because a claude started without
    # them refused every prompt here and then took the server down with it.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR")
           if k in os.environ}
    env.update({
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
        "HERDR_SOCKET_PATH": sock, "TERM": "xterm-256color",
        session_host.API_TOKEN_ENV_VAR: token,
    })
    # NOT XDG_DATA_HOME — see the docstring. Isolating it costs the operator their `claude` binary.
    name = "i317drill"

    runs = 0

    def serve():
        nonlocal runs
        runs += 1
        log = open(str(root / ("server-%d.log" % runs)), "w")
        proc = subprocess.Popen([_REAL, "server"], env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(400):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        time.sleep(0.5)
        return proc

    def evidence(why):
        """Everything a person needs to tell an arming problem from a wrapper regression."""
        parts = [why]
        for path in sorted(root.glob("server-*.log")) + sorted(
                (root / "config").rglob("herdr-server.log")):
            parts.append("--- %s\n%s" % (path, path.read_text(errors="replace")[-2000:]))
        return "\n".join(parts)

    def build(**kw):
        return session_host.SessionHost(probe=_LabProbe(env), binary=_REAL, env=env, **kw)

    host = build(restore_wait_seconds=5.0, restore_poll_seconds=0.5)
    server = serve()
    try:
        # The pane shell here sources no rc file, so PATH is whatever we hand it — without this the
        # pane answers `zsh: command not found: claude` and `agent start` reports a startup timeout.
        session = host.spawn(name=name, cwd=str(work),
                             env={"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~")})
        assert host.state(session).liveness == session_host.ALIVE

        # One real turn, so a transcript exists for `claude --resume` to find. Without it herdr's
        # restore relaunches claude, claude exits within a second, and the lane comes back empty.
        refusals = []
        for _ in range(3):
            done = host._call(["agent", "prompt", name, "Reply with exactly: PELICAN-317",
                               "--wait", "--timeout", "90000"], timeout=120)
            if done.ok:
                break
            refusals.append(done.error)
            time.sleep(3)
        if refusals and not done.ok:
            pytest.fail(evidence("the lab agent refused every arming prompt (%s) — the drill was "
                                 "never armed, so nothing below is a wrapper verdict"
                                 % "; ".join(refusals)))
        captured = None
        for _ in range(90):
            # The lab server's own liveness, checked FIRST and every round. Without it a server
            # that died took 90 reads x the call timeout to notice — half an hour of a suite
            # looking busy, and then a failure that blamed the wrapper. `poll()` is also the only
            # thing that can say HOW it died: a negative value is the signal that killed it.
            if server.poll() is not None:
                pytest.fail(evidence("the lab server exited (poll()=%s; negative means it was "
                                     "killed by that signal) while the drill was arming — that is "
                                     "a lab failure, not a wrapper verdict" % server.poll()))
            got = host._call(["agent", "get", name])
            captured = _dig_session(got.payload) if got.ok else None
            if captured:
                break
            time.sleep(1)
        if not captured:
            pytest.fail(evidence(
                "herdr captured no agent session id, so `persist.restore` builds no resume plan "
                "and there is no window to drill. Check the SessionStart hook actually ran."))
        # …and it has to be in the SNAPSHOT, which herdr writes on its own timer. Restoring reads
        # the file, not the live state, so arming before it lands drills a window that never opens.
        snapshot = None
        for _ in range(120):
            found = [p for p in (root / "config").rglob("session.json")
                     if captured in p.read_text(errors="replace")]
            if found:
                snapshot = found[0]
                break
            time.sleep(0.5)
        if snapshot is None:
            pytest.fail(evidence("herdr never persisted the captured session id (%s)" % captured))

        os.kill(server.pid, signal.SIGKILL)
        # `wait()`, not a `kill(pid, 0)` spin: a SIGKILLed CHILD becomes a zombie, a zombie keeps
        # its pid entry, and signal 0 answers happily for one — so the obvious spin never ends. It
        # cost half an hour of drills that looked like a dying host and were a hung test.
        server.wait(timeout=30)
        killed = time.monotonic()
        server = serve()

        # ---- the window, live -----------------------------------------------------------------
        state = host.state(session)
        assert state.liveness == session_host.RESTORING, (
            "the wrapper must name the window; it read %s (%s)" % (state.liveness, state.detail))
        assert state.advisory == "idle", (
            "and the host really is saying `idle` about a pane with nothing behind it — that is "
            "the surface this verdict exists to disbelieve")
        assert state.name_resolves is True

        with pytest.raises(session_host.TeardownRefused):
            host.exit(session)

        started = time.monotonic()
        with pytest.raises(session_host.RestoreWindowExpired):
            host.send(session, "carry on", delivery=_NeverLanded())
        assert time.monotonic() - started >= 4.0, "the send has to actually WAIT out its bound"

        # ---- and the window closing, which is the whole reason for waiting ---------------------
        while time.monotonic() - killed < 180:
            if host.state(session).liveness != session_host.RESTORING:
                break
            time.sleep(0.5)
        window = time.monotonic() - killed
        print("\nmeasured restore window on %s: %.2fs" % (_REAL, window))
        assert host.state(session).liveness != session_host.RESTORING, (
            "the window never closed in 180s — the host is not restoring, it is broken")
        assert window < session_host.RESTORE_WAIT_SECONDS, (
            "the measured window (%.2fs) has outgrown the wrapper's default bound (%.1fs) — the "
            "pinned build moved and the default must be re-decided, not quietly outrun"
            % (window, session_host.RESTORE_WAIT_SECONDS))
    finally:
        info = host._call(["pane", "process-info", "--pane", "%s:p1" % "w1"])
        pids = [p.get("pid") for p in _dig_procs(info.payload)] if info.ok else []
        try:
            os.kill(server.pid, signal.SIGKILL)
        except OSError:
            pass
        # BY RECORDED PID ONLY — never a name or a pattern (house rule).
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                pass


class _NeverLanded:
    """An oracle that can never confirm — so the drill's send is judged on the host's refusals
    alone, which is exactly what a caller has during a restore window."""

    def mark(self):
        return "drill"

    def landed(self, mark):
        return False


def _dig_session(payload):
    agent = (payload or {}).get("agent") or {}
    return (agent.get("agent_session") or {}).get("value")


def _dig_procs(payload):
    return ((payload or {}).get("process_info") or {}).get("foreground_processes") or []
