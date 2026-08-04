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


def test_a_debugger_spawn_carries_the_token(tmp_path):
    fake = FakeHerdr()
    _host(fake, _fenced_env()).spawn("d12", cwd=tmp_path)

    assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR, TOKEN) in fake.env_flags()


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
    assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR, TOKEN) in flags


@pytest.mark.parametrize("name", ["i305", "i1", "runner", "d12x", "debugger", "dd1"])
def test_only_a_real_debugger_id_is_treated_as_one(name):
    assert session_host.receives_token(name) is False


@pytest.mark.parametrize("name", ["d1", "d12", "d305"])
def test_a_debugger_id_is_recognised(name):
    assert session_host.receives_token(name) is True


# --------------------------------------------------------------------- where the token is read

def test_the_token_can_come_from_a_file_so_it_need_not_sit_in_an_inheritable_env(tmp_path):
    # A pane inherits the SERVER's environment, so a token that lives only in a file was never in
    # a position to leak that way. The carried patch prefers the file for the same reason.
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    fake = FakeHerdr()
    _host(fake, {session_host.API_TOKEN_FILE_ENV_VAR: str(path)}).spawn("d12", cwd=tmp_path)

    # The trailing newline is how every editor ends a file; it is not part of the secret.
    assert "%s=%s" % (session_host.API_TOKEN_ENV_VAR, TOKEN) in fake.env_flags()


def test_an_unfenced_host_spawns_a_debugger_with_no_token(tmp_path):
    # No token configured = stock herdr = there is nothing to inject. The spawn must still work:
    # a dev machine running an unpatched host is a real case, and failing here would make the
    # wrapper unusable everywhere the fence is not deployed.
    fake = FakeHerdr()
    _host(fake, {}).spawn("d12", cwd=tmp_path)

    assert not [f for f in fake.env_flags() if f.startswith("HERDR")]


def test_an_empty_token_is_not_a_token(tmp_path):
    fake = FakeHerdr()
    _host(fake, {session_host.API_TOKEN_ENV_VAR: ""}).spawn("d12", cwd=tmp_path)

    assert not [f for f in fake.env_flags() if f.startswith("HERDR")]


def test_an_unreadable_token_file_does_not_crash_a_spawn(tmp_path):
    fake = FakeHerdr()
    _host(fake, {session_host.API_TOKEN_FILE_ENV_VAR: str(tmp_path / "nope")}).spawn(
        "d12", cwd=tmp_path)

    assert not [f for f in fake.env_flags() if f.startswith("HERDR")]


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
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(sock)
        client.sendall((json.dumps({"id": "probe", "method": "ping", "params": {},
                                    "auth": token}) + "\n").encode())
        reply = client.recv(65536).decode()
        client.close()
        assert "pong" in reply, reply
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
