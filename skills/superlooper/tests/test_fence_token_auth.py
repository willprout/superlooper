"""The fence (issue #305): token auth on the host's control socket, and who is given the token.

The fence itself is a CARRIED patch against the pinned herdr release — it lives in
``vendor/herdr/`` and is enforced inside the host process, where a worker cannot argue with it.
This file pins the half that lives in OUR code: which spawns are handed the token and which are
structurally incapable of being handed it.

**Why a token at all**, when the launcher could just scrub the environment. Three facts, and the
third is the one that settles it (plan §5.3, and every one of them re-observed end-to-end while
building this — see reports/i305.md):

1. the socket path is deterministic AND injected into every pane — the drive confirmed a worker
   pane really does carry ``HERDR_SOCKET_PATH``, ``HERDR_ENV``, ``HERDR_PANE_ID``;
2. the protocol is plain newline-JSON, so a worker needs no ``herdr`` binary to drive the whole
   fleet — ten lines of python and that path is enough;
3. the socket's own 0600 permission bit keeps out other UNIX USERS, and a worker is not another
   user: it runs as the same uid as the server.

So env hygiene alone cannot be the fence. It is kept anyway, as the second layer.

**The rule the tests below encode.** The NAME decides. ``d<N>`` (sl-debugger/Fixer) receives the
token because repair has to be able to drive herdr; ``i<N>`` never does; anything else is treated
as a worker, because deny-by-default is the only default that survives a caller's mistake. There
is deliberately NO parameter that grants the token — "the i<N> spawn path provably never does"
must be a property of the code's shape, not of every caller's care.

Holding the token is CAPABILITY, never PERMISSION: the D13 supervised/unattended rails still
govern what a ``d<N>`` session may do with it.
"""
import json
import os
import pathlib
import socket
import subprocess
import threading

import pytest

import session_host


# --------------------------------------------------------------------- a minimal fake host

def _ok(payload):
    return json.dumps({"id": "1", "result": payload})


def _ws_created(ws="w1", tab="w1:t1", pane="w1:p1"):
    return _ok({
        "type": "workspace_created",
        "workspace": {"workspace_id": ws, "active_tab_id": tab},
        "tab": {"tab_id": tab, "workspace_id": ws},
        "root_pane": {"pane_id": pane, "workspace_id": ws, "tab_id": tab},
    })


def _agent(name="i305", pane="w1:p1", kind="agent_info"):
    return _ok({"type": kind, "agent": {"name": name, "pane_id": pane}})


def _process_info(pane="w1:p1", shell_pid=4242):
    return _ok({"type": "pane_process_info",
                "process_info": {"pane_id": pane, "shell_pid": shell_pid}})


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeHerdr:
    """Captures argv. That is the whole point here: the token is either in a spawn's argv or it
    is not, and no amount of prose about intent substitutes for reading it."""

    def __init__(self):
        self.calls = []
        self.script = {
            ("workspace", "create"): (0, _ws_created(), ""),
            ("agent", "start"): (0, _agent(kind="agent_started"), ""),
            ("agent", "get"): (0, _agent(), ""),
            ("pane", "process-info"): (0, _process_info(), ""),
        }

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        rc, out, err = self.script.get(tuple(argv[1:3]), (1, "", "{}"))
        return _Completed(rc, out, err)

    def pid_alive(self, pid):
        return True

    def child_pids(self, pid):
        return [4243]

    def sleep(self, seconds):
        pass

    def create_argv(self):
        for call in self.calls:
            if tuple(call[1:3]) == ("workspace", "create"):
                return call
        raise AssertionError("no workspace create was attempted: %r" % (self.calls,))

    def env_flags(self):
        """The ``--env K=V`` pairs the spawn actually asked the host to set on the pane."""
        argv = self.create_argv()
        return [argv[i + 1] for i, word in enumerate(argv) if word == "--env"]


def _host(fake, env):
    return session_host.SessionHost(probe=fake, binary="/nonexistent/superlooper-test-herdr",
                                    env=env)


TOKEN = "fence-token-3f9a1c8e7b2d4056"


def _fenced_env(**extra):
    env = {session_host.API_TOKEN_ENV_VAR: TOKEN}
    env.update(extra)
    return env


# --------------------------------------------------------------------- who receives the token

def test_a_worker_spawn_never_carries_the_token(tmp_path):
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn("i305", cwd=tmp_path)

    flags = fake.env_flags()
    assert not any(f.startswith(session_host.API_TOKEN_ENV_VAR) for f in flags), flags
    # And not anywhere else in the argv either — a token smuggled in as a label or a --cwd is
    # still a token in the worker's pane.
    assert TOKEN not in " ".join(fake.create_argv())


def test_a_debugger_spawn_asks_for_the_token_with_the_sentinel_not_the_secret(tmp_path):
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn("d12", cwd=tmp_path)

    assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR,
                      session_host.GRANT_SENTINEL) in fake.env_flags()


def test_no_spawn_ever_puts_the_secret_in_argv(tmp_path):
    # The finding that reshaped this design. These arguments become a process's argv, and on macOS
    # a same-uid reader is REFUSED another process's environment but SERVED its argv (both
    # measured — reports/i305.md). A debugger spawn carrying the real token would therefore publish
    # it to every worker on the machine for the duration of the call. The wrapper never holds the
    # token, so there is nothing for it to leak.
    for name in ("i305", "d12"):
        fake = FakeHerdr()
        _host(fake, _fenced_env()).spawn(name, cwd=tmp_path)
        assert TOKEN not in " ".join(fake.create_argv()), (name, fake.create_argv())


def test_the_name_decides_and_a_caller_cannot_override_it(tmp_path):
    # The structural half of "the i<N> spawn path PROVABLY never does". There is no keyword that
    # grants the token, so no call site can grant it by mistake, and no future caller can be
    # talked into it. If someone adds such a parameter, this test is what fails.
    import inspect
    params = set(inspect.signature(session_host.SessionHost.spawn).parameters)
    assert not {p for p in params if "token" in p.lower() or "fence" in p.lower()}, params


def test_a_caller_cannot_hand_a_worker_the_token_through_the_env_argument(tmp_path):
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn(
        "i305", cwd=tmp_path, env={session_host.API_TOKEN_ENV_VAR: TOKEN})

    assert TOKEN not in " ".join(fake.create_argv()), fake.create_argv()


def test_every_herdr_variable_is_stripped_from_a_worker_spawn(tmp_path):
    # Defense in depth, per the ruling. The host injects its OWN pane-identity vars regardless of
    # what we pass (the end-to-end drive confirmed HERDR_ENV/HERDR_PANE_ID/HERDR_SOCKET_PATH all
    # arrive in a worker pane) — which is exactly why this is the second layer and not the fence.
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn("i305", cwd=tmp_path, env={
        "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
        "HERDR_API_TOKEN_FILE": "/tmp/token",
        "SL_ISSUE_ID": "i305",
    })

    flags = fake.env_flags()
    assert not [f for f in flags if f.startswith("HERDR")], flags
    assert "SL_ISSUE_ID=i305" in flags, "a non-host variable must still be forwarded"


def test_a_debugger_keeps_its_other_variables(tmp_path):
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn("d12", cwd=tmp_path, env={"SL_ISSUE_ID": "d12"})

    flags = fake.env_flags()
    assert "SL_ISSUE_ID=d12" in flags
    assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR,
                      session_host.GRANT_SENTINEL) in flags


@pytest.mark.parametrize("name", ["i305", "i1", "runner", "d12x", "debugger", "dd1"])
def test_only_a_real_debugger_id_is_treated_as_one(name):
    assert session_host.receives_token(name) is False


@pytest.mark.parametrize("name", ["d1", "d12", "d305"])
def test_a_debugger_id_is_recognised(name):
    assert session_host.receives_token(name) is True


# --------------------------------------------------------------------- where the token is read

def test_the_wrapper_never_reads_a_token_from_anywhere(tmp_path):
    """The strongest form of "it cannot leak the token": it never obtains one.

    Where the token lives, whether the file is readable, and what happens when it is broken are
    all questions for the HOST now (``src/api/auth.rs``, which fails closed on a token file it was
    told to use and could not read). The wrapper's spawn behaves identically however this process
    is configured — so there is no configuration in which it starts handling the secret.
    """
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    configurations = (
        {},                                                          # unfenced host
        {session_host.API_TOKEN_ENV_VAR: TOKEN},                     # token in env
        {session_host.API_TOKEN_ENV_VAR: ""},                        # empty
        {session_host.API_TOKEN_FILE_ENV_VAR: str(path)},            # token in a file
        {session_host.API_TOKEN_FILE_ENV_VAR: str(tmp_path / "no")},  # broken file
    )
    flags = []
    for env in configurations:
        fake = FakeHerdr()
        _host(fake, env).spawn("d12", cwd=tmp_path)
        assert TOKEN not in " ".join(fake.create_argv()), env
        flags.append(fake.env_flags())

    assert all(f == flags[0] for f in flags), flags


# --------------------------------------------------------------------- proving the fence is up

@pytest.fixture
def sock_dir():
    """A SHORT directory for unix sockets.

    pytest's ``tmp_path`` is comfortably past the 104-character ``sun_path`` limit, which is the
    same constraint the adoption plan calls out for the real deployment (§2: "dedicated named
    session + short socket path"). A test that hit it would look like a fence failure and be a
    path-length failure.
    """
    import shutil
    import tempfile
    path = tempfile.mkdtemp(prefix="slf-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _serve_one(path, reply):
    """A one-shot unix socket that answers `reply`. Not herdr — just something at the other end,
    so the probe is exercised against a REAL socket rather than a mock of one."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            conn.recv(65536)
            if reply is not None:
                conn.sendall(reply.encode())
            conn.close()
        except OSError:
            pass
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_the_probe_reports_a_fenced_socket(sock_dir):
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, json.dumps(
        {"id": "probe", "error": {"code": "unauthorized", "message": "unauthorized"}}) + "\n")

    assert session_host.fence_probe(path) == session_host.FENCED


def test_the_probe_reports_an_OPEN_socket_when_a_tokenless_ping_is_served(sock_dir):
    # The dangerous state, and the reason this probe exists: an unfenced socket does not look
    # broken from the runner's seat — it looks like everything working. It answers.
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, json.dumps(
        {"id": "probe", "result": {"type": "pong", "version": "0.8.0"}}) + "\n")

    assert session_host.fence_probe(path) == session_host.OPEN


def test_the_probe_reports_unreachable_when_nothing_is_listening(tmp_path):
    assert session_host.fence_probe(str(tmp_path / "absent.sock")) == session_host.UNREACHABLE


def test_the_probe_reports_unreachable_on_silence(sock_dir):
    # Absence of signal is UNKNOWN, never "fine" (c2) — a socket that accepts and then says
    # nothing must NEVER be read as fenced.
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, None)

    assert session_host.fence_probe(path, timeout=1.0) == session_host.UNREACHABLE


def test_the_probe_presents_no_token_because_that_is_the_position_it_is_testing(sock_dir):
    # The probe must ask the question a WORKER would ask. If it ever authenticated itself, it
    # would report the fence up on a socket that is wide open to everyone else.
    path = os.path.join(sock_dir, "h.sock")
    seen = {}

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        seen["line"] = conn.recv(65536).decode()
        conn.sendall(json.dumps({"id": "probe", "error": {"code": "unauthorized",
                                                          "message": "no"}}).encode() + b"\n")
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    session_host.fence_probe(path, env={session_host.API_TOKEN_ENV_VAR: TOKEN})

    assert "auth" not in json.loads(seen["line"])
    assert TOKEN not in seen["line"]


# ------------------------------------------- the one hole in the fence: the state report (#331)
# Owner ruling, 2026-08-04: a fenced host ADMITS the vendor's `SessionStart` state report from a
# tokenless caller — the call the carried hook script (#307) makes to tell the host which session id
# to `--resume` after a crash — and refuses every other method before dispatch exactly as #305 built
# it. Without the hole a fenced host captures nothing and `persist.restore` returns a bare shell,
# silently. The allowance is host-side and method-scoped, inside the carried patch; the vendor hook
# is never taught a credential.
#
# The tests below are the OUR-CODE half: that the probe asks the worker's question, that it reads
# both answers, and — in the opt-in acceptance check further down — that a real patched host really
# does draw the line exactly one method wide.

def test_the_capture_probe_reports_an_admitted_state_report(sock_dir):
    path = os.path.join(sock_dir, "h.sock")
    # What a fenced host with the allowance actually answers: the call reached the method body and
    # the method refused the probe's deliberately unusable arguments.
    _serve_one(path, json.dumps(
        {"id": "superlooper-capture-probe",
         "error": {"code": "pane_not_found", "message": "pane … not found"}}) + "\n")

    assert session_host.state_report_probe(path) == session_host.ADMITTED


def test_the_capture_probe_reports_a_refused_state_report(sock_dir):
    # The state this issue exists for, and the one that is invisible from the runner's seat: the
    # fence is up, every worker launches and works, and no session id is ever captured.
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, json.dumps(
        {"id": "superlooper-capture-probe",
         "error": {"code": "unauthorized", "message": "unauthorized"}}) + "\n")

    assert session_host.state_report_probe(path) == session_host.REFUSED


def test_the_capture_probe_reads_a_served_report_as_admitted(sock_dir):
    # An unfenced host serves it outright. Still ADMITTED — this probe answers "did the fence turn
    # it away", and the doctor pairs it with fence_probe to tell the two apart.
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, json.dumps({"id": "superlooper-capture-probe", "result": {"type": "ok"}}) + "\n")

    assert session_host.state_report_probe(path) == session_host.ADMITTED


def test_an_unknown_error_code_is_read_as_admitted_not_as_refused(sock_dir):
    # `unauthorized` is the fence's own word and the only discriminator. Anything else came out of
    # the method body, which means the connection got past the check. A code that MOVES in a later
    # release must degrade to "the hole is there" rather than quietly reporting a working capture as
    # silent — the direction that would send an operator chasing a healthy machine.
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, json.dumps(
        {"id": "x", "error": {"code": "invalid_agent", "message": "…"}}) + "\n")

    assert session_host.state_report_probe(path) == session_host.ADMITTED


def test_the_capture_probe_reports_unreachable_when_nothing_is_listening(tmp_path):
    assert session_host.state_report_probe(str(tmp_path / "absent.sock")) \
        == session_host.UNREACHABLE


def test_the_capture_probe_reports_unreachable_on_silence(sock_dir):
    path = os.path.join(sock_dir, "h.sock")
    _serve_one(path, None)

    assert session_host.state_report_probe(path, timeout=1.0) == session_host.UNREACHABLE


def test_the_capture_probe_presents_no_token_and_asks_for_exactly_one_method(sock_dir):
    """It has to ask what a WORKER would ask — and it must not be able to ask for anything else."""
    path = os.path.join(sock_dir, "h.sock")
    seen = {}

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        seen["line"] = conn.recv(65536).decode()
        conn.sendall(json.dumps({"id": "x", "error": {"code": "pane_not_found",
                                                      "message": "no"}}).encode() + b"\n")
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    session_host.state_report_probe(path, env={session_host.API_TOKEN_ENV_VAR: TOKEN})

    sent = json.loads(seen["line"])
    assert "auth" not in sent, sent
    assert TOKEN not in seen["line"]
    assert sent["method"] == session_host.STATE_REPORT_METHOD == "pane.report_agent_session"


def test_the_capture_probe_cannot_write_over_a_real_pane_s_record(sock_dir):
    """A doctor command must not mutate the fleet it is describing.

    The probe has to reach the method BODY to learn that the fence let it in, and that body's whole
    job is writing a pane's resume record. So its arguments are unusable twice over: the pane id
    cannot resolve, and the agent label is empty — the host's handler checks the pane first and the
    label second, so even a pane id that somehow resolved is refused before anything is written.
    """
    path = os.path.join(sock_dir, "h.sock")
    seen = {}

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        seen["line"] = conn.recv(65536).decode()
        conn.sendall(b'{"id":"x","error":{"code":"pane_not_found","message":"no"}}\n')
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    session_host.state_report_probe(path)

    params = json.loads(seen["line"])["params"]
    assert not params["agent"].strip(), params
    assert ":" not in params["pane_id"] and not params["pane_id"].startswith("p_"), params
    # And it never carries a session id, so there is nothing for a host to record even if it did.
    assert "agent_session_id" not in params, params


# --------------------------------------------------------------- where the socket is, unasked

def test_the_socket_path_prefers_the_explicit_override():
    assert session_host.control_socket_path({"HERDR_SOCKET_PATH": "/tmp/h.sock",
                                             "HOME": "/Users/x"}) == "/tmp/h.sock"


def test_the_socket_path_falls_back_to_the_host_s_own_config_directory():
    assert session_host.control_socket_path({"HOME": "/Users/x"}) \
        == "/Users/x/.config/herdr/herdr.sock"
    assert session_host.control_socket_path({"XDG_CONFIG_HOME": "/cfg", "HOME": "/Users/x"}) \
        == "/cfg/herdr/herdr.sock"


def test_a_named_session_keeps_its_own_socket():
    # The adoption plan's deployment shape (§2: a dedicated named session), spelled the way the
    # host spells it.
    assert session_host.control_socket_path({"HOME": "/Users/x", "HERDR_SESSION": "loop"}) \
        == "/Users/x/.config/herdr/sessions/loop/herdr.sock"
    # "default" is not a named session — the host filters it out, and so does this.
    assert session_host.control_socket_path({"HOME": "/Users/x", "HERDR_SESSION": "default"}) \
        == "/Users/x/.config/herdr/herdr.sock"


def test_no_home_and_no_override_resolves_to_nothing_rather_than_a_guess():
    # A guessed path would let a probe answer UNREACHABLE about a machine it never looked at.
    assert session_host.control_socket_path({}) is None


# --------------------------------------------------- the deliberate-event check (opt-in, real)

_REAL = os.environ.get("SL_FENCE_HERDR")


@pytest.mark.skipif(not _REAL, reason="set SL_FENCE_HERDR=<patched herdr binary> to run the "
                                      "real-socket acceptance check (every version bump does)")
def test_a_tokenless_connection_to_a_real_patched_server_is_refused(sock_dir):
    """The negative test, against a REAL patched herdr — the check a version bump re-runs.

    Everything above this line proves what superlooper does with a token. This proves the thing
    superlooper cannot prove about itself: that the host refuses the worker's position. It is
    skipped by default because it needs a built, patched binary (and because the conftest ratchet
    keeps the suite away from real host binaries); ``vendor/herdr/README.md`` is where the bump
    procedure names it.
    """
    token = "acceptance-" + os.urandom(8).hex()
    root = pathlib.Path(sock_dir)          # short: the socket must fit sun_path's 104 chars
    (root / "home").mkdir(parents=True, exist_ok=True)
    sock = str(root / "h.sock")
    env = {
        "PATH": os.environ["PATH"], "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
        "XDG_DATA_HOME": str(root / "data"), "HERDR_CONFIG_PATH": str(root / "config.toml"),
        "HERDR_SOCKET_PATH": sock, "TERM": "xterm-256color",
        session_host.API_TOKEN_ENV_VAR: token,
    }
    server = subprocess.Popen([_REAL, "server"], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if os.path.exists(sock):
                break
            __import__("time").sleep(0.1)
        assert os.path.exists(sock), "the patched server never created its socket"
        __import__("time").sleep(0.5)

        # The worker's position: refused.
        assert session_host.fence_probe(sock, timeout=5.0) == session_host.FENCED

        # The runner's position: served. Both halves matter — a socket that refused EVERYONE
        # would also pass the first assertion, and would be a broken host, not a fence.
        def speak(request, timeout=5.0):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(timeout)
            client.connect(sock)
            client.sendall((json.dumps(request) + "\n").encode())
            reply = client.recv(65536).decode()
            client.close()
            return reply

        assert "pong" in speak({"id": "probe", "method": "ping", "params": {}, "auth": token})

        # ---- the ruled allowance, and its exact width (#331) -----------------------------------
        # POSITIVE: the vendor's own state report, presented by a caller with no credential at all,
        # reaches the method body. `pane_not_found` IS the admission — it is the handler talking.
        assert session_host.state_report_probe(sock, timeout=5.0) == session_host.ADMITTED

        # NEGATIVE, and this is the half the DoD leans on: a tokenless caller can do NOTHING else.
        # A socket that admitted everything would sail through the assertion above, so without this
        # loop the acceptance check would pass on a host with no fence at all. The list deliberately
        # includes the report's two NEIGHBOURS — `pane.report_agent` and `pane.report_metadata` are
        # the same hook family, same shape, same pane argument — because "one method wide" is the
        # claim, not "the reporting methods are open".
        for method, params in (
                ("ping", {}),
                ("server.stop", {}),
                ("workspace.create", {"cwd": str(root)}),
                ("workspace.list", {}),
                ("agent.start", {"name": "x", "kind": "claude"}),
                ("agent.list", {}),
                ("pane.run", {"pane_id": "w1:p1", "command": ["true"]}),
                ("pane.read", {"pane_id": "w1:p1"}),
                ("pane.report_agent", {"pane_id": "w1:p1", "source": "herdr:claude",
                                       "agent": "claude", "state": "working"}),
                ("pane.report_metadata", {"pane_id": "w1:p1", "source": "herdr:claude"}),
                ("pane.release_agent", {"pane_id": "w1:p1", "source": "herdr:claude"}),
        ):
            reply = json.loads(speak({"id": "n", "method": method, "params": params}))
            assert reply.get("error", {}).get("code") == "unauthorized", (method, reply)

        # And the allowance is the exact spelling, not a family of them. Every one of these would
        # be a way in if the check ever trimmed, folded case, or matched a prefix.
        for near_miss in (" pane.report_agent_session", "pane.report_agent_session ",
                          "PANE.REPORT_AGENT_SESSION", "pane.report_agent_session2",
                          "pane.report_agent_sessio"):
            reply = json.loads(speak({"id": "m", "method": near_miss, "params": {}}))
            assert reply.get("error", {}).get("code") == "unauthorized", (near_miss, reply)

        # The allowance is not a credential: naming the open method does not open the others. This
        # is the shape a "smuggle a second verb in on the same line" attempt would take.
        smuggled = json.loads(speak({"id": "s", "method": "server.stop", "params": {},
                                     "auth": session_host.STATE_REPORT_METHOD}))
        assert smuggled.get("error", {}).get("code") == "unauthorized", smuggled

        # ---- the pane grant, which is the other load-bearing half of the patch ----------------
        # Without this, a re-apply could silently drop or move the `pane.rs` hunk and everything
        # above would still pass while every worker pane quietly inherited the server's token.
        def pane_env(label, extra_args):
            """What the pane ACTUALLY got, reported by the pane itself.

            Not `ps -Eww`: macOS returns no environment for another process, so reading it that
            way is a check that passes whether or not the token is there. That mistake was made
            once here already.
            """
            out = root / (label + ".env")
            created = subprocess.run(
                [_REAL, "workspace", "create", "--cwd", str(root), "--label", label,
                 "--no-focus"] + extra_args,
                env=env, capture_output=True, text=True, timeout=60)
            pane = json.loads(created.stdout)["result"]["root_pane"]["pane_id"]
            subprocess.run([_REAL, "pane", "run", pane, "sh", "-c", "env > %s" % out],
                           env=env, capture_output=True, text=True, timeout=60)
            for _ in range(80):
                if out.exists() and out.stat().st_size:
                    break
                __import__("time").sleep(0.25)
            assert out.exists() and out.stat().st_size, "pane %s never reported its env" % label
            return out.read_text()

        # A worker pane, spawned by a server that HOLDS the token in its own environment.
        worker = pane_env("worker", [])
        assert "HERDR_SOCKET_PATH" in worker, (
            "precondition: the host injects the socket path into every pane — if this ever stops "
            "being true the fence is still right, but its rationale has changed")
        assert token not in worker, "a worker pane inherited the server's token"
        assert session_host.API_TOKEN_ENV_VAR + "=" not in worker

        # A debugger pane, asking with the sentinel: the server substitutes the real token.
        debugger = pane_env("debugger", ["--env", "%s=%s" % (session_host.API_TOKEN_ENV_VAR,
                                                             session_host.GRANT_SENTINEL)])
        assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR, token) in debugger, (
            "the grant did not deliver the token to a d<N> pane under its own variable")
        assert session_host.GRANT_SENTINEL not in debugger, (
            "the sentinel reached the pane verbatim — the server did not substitute it")

        # A caller supplying a literal value must NOT get to choose a pane's credential.
        forged = pane_env("forged", ["--env", "%s=%s" % (session_host.API_TOKEN_ENV_VAR,
                                                         "attacker-chosen")])
        assert "attacker-chosen" not in forged
        assert token not in forged
    finally:
        # By the pid we recorded, never by name or pattern (house rule).
        try:
            os.killpg(os.getpgid(server.pid), __import__("signal").SIGTERM)
        except OSError:
            server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
