"""Issue #136 — ``liftoff --restart-dashboard``: the mechanical way to heal a stale dashboard.

**Why this is a command and not a button.** A stale server is stale precisely BECAUSE it lacks the
newly merged routes — so a "restart the dashboard" endpoint would 404 on exactly the servers that
need it. ``bin/liftoff`` is read fresh from disk on every invocation, so it works no matter how old
the running server is. That catch-22 is what put the remedy here.

**Why liftoff needed a new flag at all.** liftoff's normal path is *idempotent by contract*: it
probes, and an already-serving dashboard is left alone ("dashboard already serving — leaving it").
That is exactly right for the start path and exactly useless for a stale one — a routine liftoff
never heals the skew. So the flag is an EXPLICIT second verb, and the tests below pin that the
normal path's never-double-start guarantee is untouched by it.

The three properties that matter, in order of how badly each would hurt:

  * **never double-start** — the fresh dashboard is spawned only once the PORT is confirmed free.
    Two dashboards on one port means one dies at bind and the owner can't tell which is answering,
    so the contended resource itself is the arbiter. Both softer proxies were tried and both were
    wrong in one direction (fresh review, issue #136): the snapshot probe going quiet counts a
    wedged-but-alive server as gone, and process liveness counts an exited-but-unreaped zombie —
    which holds no socket — as still here. A raw TCP connect is right in both cases.
  * **never a pattern kill** — the SIGTERM goes to the pid the dashboard published for itself,
    alongside an explicit ``product`` claim; a responder that merely *resembles* a snapshot is
    refused rather than signalled. ``pkill -f`` collateral-killed William's live dashboard once
    already (2026-07-07), and the port-holder is no safer — ``_dashboard_up``'s own contract admits
    a stranger can squat the port. Note the asymmetry these tests pin: the port decides whether to
    START, the published identity decides whom to SIGNAL. Neither question answers the other.
  * **dashboard-only** — the flag never touches the runner and never claims the tab, so it is safe to
    run from any terminal.
"""
import importlib.util
import io
import json
import os
import socket
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import liftoff as liftoff_mod

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin" / "liftoff"


def _load():
    loader = SourceFileLoader("liftoff_bin", str(_BIN))
    spec = importlib.util.spec_from_loader("liftoff_bin", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lo = _load()

URL = "http://127.0.0.1:8611"


def _snap(pid=4242, skew=True, product="command-center"):
    v = {"server": "aaaa", "server_on_disk": "bbbb" if skew else "aaaa",
         "assets": "cccc", "assets_at_boot": "cccc", "skew": skew,
         "message": "stale" if skew else None,
         "remedy": "bin/liftoff --restart-dashboard", "pid": pid}
    if product is not None:
        v["product"] = product
    return {"generated_at": 1, "repos": [], "version": v}


# =============================== the pure decision ===============================

def test_nothing_serving_and_a_free_port_just_starts():
    d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=False)
    assert d["action"] == "start"
    assert d["pid"] is None


def test_a_silent_port_that_is_still_HELD_is_refused_not_treated_as_empty():
    """Round 2's P0 (issue #136): silent is not empty.

    A dashboard wedged before the first probe answers nothing yet still holds the socket. If that
    read as "nothing serving", liftoff would spawn a replacement that dies at bind, leave the stale
    server answering, and return 0 — the exact failure this flag exists to fix, wearing a success
    message. We cannot identify it (it never told us its pid), so we cannot stop it, so we start
    nothing.
    """
    d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=True)
    assert d["action"] == "refuse"
    assert d["pid"] is None, "never signal something that never identified itself"
    assert "Ctrl-C" in d["message"]


def test_a_live_dashboard_is_stopped_then_started():
    d = liftoff_mod.dashboard_restart_decision(URL, _snap(pid=4242))
    assert d["action"] == "stop-then-start"
    assert d["pid"] == 4242


def test_the_decision_names_the_pid_from_the_snapshot_never_the_port_holder():
    """The pid must come from the process that answered OUR snapshot shape — the only identification
    that cannot name a stranger squatting the port (see the module docstring)."""
    assert liftoff_mod.dashboard_restart_decision(URL, _snap(pid=99))["pid"] == 99


def test_a_dashboard_that_reports_no_pid_is_refused_never_guessed():
    """A server predating issue #136 reports no version block. liftoff must NOT fall back to
    guessing (a pattern kill, or killing the port-holder) — it says so and stops."""
    d = liftoff_mod.dashboard_restart_decision(URL, {"generated_at": 1, "repos": []})
    assert d["action"] == "refuse"
    assert d["pid"] is None
    assert "Ctrl-C" in d["message"], "a refusal must tell the owner how to do it by hand"


def test_a_malformed_pid_is_refused_not_coerced():
    for bad in (None, 0, -1, "4242", 4242.7, True):
        d = liftoff_mod.dashboard_restart_decision(URL, _snap(pid=bad))
        assert d["action"] == "refuse", "pid %r must not be trusted as a kill target" % (bad,)


def test_a_responder_that_does_not_claim_to_be_a_command_center_is_never_signalled():
    """The snapshot's general shape is a RESEMBLANCE, not a proof of identity. Any localhost
    responder carrying generated_at/repos/a pid could otherwise aim a SIGTERM at any process it
    named. The product marker makes identity an explicit claim. (Fresh review, issue #136.)"""
    for impostor in (None, "", "something-else", "Command-Center", 1):
        d = liftoff_mod.dashboard_restart_decision(URL, _snap(pid=4242, product=impostor))
        assert d["action"] == "refuse", (
            "product %r must not be trusted to hand over a kill target" % (impostor,))
        assert d["pid"] is None


def test_an_already_current_dashboard_still_restarts_because_the_owner_asked():
    """The flag is the owner's explicit act, not a repair the machine decides on. It reports that
    nothing was stale, and does what it was told."""
    d = liftoff_mod.dashboard_restart_decision(URL, _snap(skew=False))
    assert d["action"] == "stop-then-start"
    assert "already current" in d["message"]


def test_the_decision_says_it_was_stale_when_it_was():
    assert "stale" in liftoff_mod.dashboard_restart_decision(URL, _snap(skew=True))["message"]


# =============================== the bin flow ===============================

def _repo_checkout(base, name, slug):
    """A checkout config.load enriches from — same shape test_liftoff_bin.py builds."""
    d = base / name
    (d / ".superlooper").mkdir(parents=True)
    (d / ".superlooper" / "config.json").write_text(json.dumps({"repo": slug}), encoding="utf-8")
    return d


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # SL_HOME under tmp so the dashboard log dir is writable and isolated from William's real one.
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    co = _repo_checkout(tmp_path, "sandbox", "will-titan/sandbox")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"repos": [{"path": str(co)}]}), encoding="utf-8")
    return path


class _Recorder:
    def __init__(self, ret=None):
        self.calls = []
        self._ret = ret

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self._ret


class _Probe:
    """A dashboard that answers with ``snaps`` in order, one per probe — so a test can stage "up,
    then gone after the stop" (or "up, still up, still up" for the won't-die case)."""

    def __init__(self, snaps):
        self._snaps = list(snaps)
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        return self._snaps[min(self.calls - 1, len(self._snaps) - 1)]


class _Port:
    """The kernel's answer to "is anything accepting on that port", staged: held for the first
    ``held_for`` checks, then free. ``held_for=None`` means it is never released (the wedged
    dashboard); ``held_for=0`` means free from the start (nothing there)."""

    def __init__(self, held_for=0):
        self._n = held_for
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        return True if self._n is None else self.calls <= self._n


def _run(cfg_path, *, probe, stop=None, spawn=None, execr=None, sleep=None, port=None,
         identity=None):
    spawn = spawn if spawn is not None else _Recorder()
    execr = execr if execr is not None else _Recorder()
    stop = stop if stop is not None else _Recorder()
    # Nothing identifies itself by default (issue #471): every pre-existing case in this file was
    # written against a dashboard that answers the snapshot or answers nothing at all.
    identity = identity if identity is not None else _Recorder(None)
    out = io.StringIO()
    rc = lo.main([str(_BIN), str(cfg_path), "--restart-dashboard"],
                 dashboard_snapshot=probe, dashboard_identity=identity, stop_process=stop,
                 spawn_dashboard=spawn, exec_runner=execr,
                 # held for the opening decision probe, released once stopped — the normal case
                 port_busy=(port if port is not None else _Port(held_for=1)),
                 sleep=(sleep if sleep is not None else _Recorder()), out=out)
    return rc, out.getvalue(), stop, spawn, execr


def test_restart_stops_exactly_the_reported_pid_then_starts_a_fresh_one(cfg):
    probe = _Probe([_snap(pid=4242), None])          # up, then gone once stopped
    rc, text, stop, spawn, execr = _run(cfg, probe=probe)
    assert rc == 0
    assert stop.calls == [((4242,), {})], "must stop exactly the pid the dashboard reported"
    assert len(spawn.calls) == 1, "a fresh dashboard is started after the old one is gone"
    assert execr.calls == [], "--restart-dashboard must never touch the runner"


def test_restart_never_double_starts_when_the_port_is_never_released(cfg):
    """The whole point of the guarantee: if the old server keeps the socket, spawning a second one
    gives two dashboards racing for one port. Start nothing, say so."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(pid=4242)]),
                                        port=_Port(held_for=None))   # never released
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], "must NOT start a second dashboard while the port is still held"
    assert "still held" in text
    assert "4242" in text, "the owner needs the pid to finish the job by hand"


def test_a_hung_dashboard_that_stops_answering_is_not_mistaken_for_a_dead_one(cfg):
    """Round 1's P0 (issue #136): the snapshot probe going quiet is not death.

    It answers None for a timeout or a truncated body — every symptom of a dashboard HUNG BUT ALIVE
    and still holding the port. If probe-silence counted as gone, liftoff would spawn a replacement,
    the new process would die at bind, the stale server would keep answering, and liftoff would
    report success. The kernel's listen socket is the arbiter, not the HTTP handler.
    """
    probe = _Probe([_snap(pid=4242), None])           # stops answering right after the stop…
    rc, text, stop, spawn, execr = _run(cfg, probe=probe, port=_Port(held_for=None))  # …still holds
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], (
        "a silent-but-alive dashboard still holds the port — starting a second one is the double "
        "start this flag exists to avoid")
    assert "still held" in text


def test_a_dashboard_that_exits_without_being_reaped_does_not_block_the_restart(cfg):
    """Round 2's P1 (issue #136): a zombie holds no socket.

    The old dashboard was spawned by a previous liftoff, whose process then became the runner via
    exec. If that parent never reaps it, ``os.kill(pid, 0)`` keeps reporting the exited process as
    alive — so gating the restart on process liveness would refuse to start, and strand the owner
    with NO dashboard at all, over a process that had already released the port. Asking the port
    instead is right in both directions.
    """
    probe = _Probe([_snap(pid=4242), None])
    rc, text, stop, spawn, execr = _run(cfg, probe=probe, port=_Port(held_for=1))  # freed after stop
    assert rc == 0
    assert len(spawn.calls) == 1, "the port is free — a zombie must not block the replacement"


def test_a_dashboard_we_could_not_signal_never_gets_a_replacement_beside_it(cfg):
    """EPERM: the pid is not ours to kill. We could not stop it ⇒ we do not start a rival."""
    def boom(pid):
        raise PermissionError("not yours")
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(pid=4242)]), stop=boom)
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], "never start a second dashboard beside one we could not stop"
    assert "starting nothing" in text


def test_restart_with_nothing_serving_just_starts_one(cfg):
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([None]), port=_Port(held_for=0))
    assert rc == 0
    assert stop.calls == [], "nothing to stop — never signal a pid we never saw"
    assert len(spawn.calls) == 1


def test_restart_refuses_a_port_held_by_something_that_never_answers(cfg):
    """Wedged from the very first probe: never answered, never released. Nothing to identify, so
    nothing to signal — and nothing started beside it."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([None]), port=_Port(held_for=None))
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert stop.calls == [], "never signal a process we could not identify"
    assert spawn.calls == [], "never start a second dashboard on a port that is still held"


def test_restart_refuses_a_dashboard_that_reports_no_pid(cfg):
    """A server predating this issue. liftoff must refuse rather than guess — and must not leave the
    owner stranded: the message tells them how to do it by hand."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([{"generated_at": 1, "repos": []}]))
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert stop.calls == [], "never signal a process we cannot identify"
    assert spawn.calls == [], "never start a second dashboard beside one we couldn't stop"
    assert "Ctrl-C" in text


def test_restart_does_not_exec_the_runner_even_when_none_is_live(cfg):
    """liftoff's normal path foregrounds the runner in this tab. --restart-dashboard is a focused,
    dashboard-only verb: run it from any terminal without it hijacking the tab."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(), None]))
    assert execr.calls == []


def test_restart_reports_the_stale_build_it_healed(cfg):
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(skew=True), None]))
    assert "stale" in text


# =============================== the port probe itself (a real socket, no network) ===============================

def test_port_busy_reads_a_free_port_as_free():
    """If this ever read a free port as held, --restart-dashboard could never start anything."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert lo._port_busy("127.0.0.1", port) is False


def test_port_busy_reads_a_live_listener_as_held():
    """The wedged-dashboard case: a listener that never answers HTTP still holds the socket, and the
    kernel says so even when the snapshot probe cannot."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        assert lo._port_busy("127.0.0.1", srv.getsockname()[1]) is True
    finally:
        srv.close()


def test_port_busy_treats_a_connect_timeout_as_HELD_not_free(monkeypatch):
    """The last P0 of the review (issue #136): only a refusal proves a port is free.

    A listener whose accept backlog is full still owns the port while new connects hang. On loopback
    an unbound port is refused instantly — there is nothing to wait for — so a TIMEOUT is evidence
    that something IS bound and simply not getting to us. Reading it as free lets liftoff spawn onto
    a taken port, watch the child die at bind, and report success.
    """
    def hangs(addr, timeout=None):
        raise socket.timeout("timed out")
    monkeypatch.setattr(lo.socket, "create_connection", hangs)
    assert lo._port_busy("127.0.0.1", 8611) is True, (
        "a connect that times out must read as HELD — never double-start on a guess")


def test_port_busy_treats_an_unclear_oserror_as_HELD(monkeypatch):
    """Anything that is not a clean refusal fails toward "held": one honest message asking the owner
    to look, rather than a silent double start."""
    def weird(addr, timeout=None):
        raise OSError("something unclear")
    monkeypatch.setattr(lo.socket, "create_connection", weird)
    assert lo._port_busy("127.0.0.1", 8611) is True


def test_port_busy_does_not_mistake_time_wait_for_a_listener():
    """A finished connection leaves TIME_WAIT behind, but no listener — so a connect is refused and
    the port is correctly free. Reading TIME_WAIT as held would block a legitimate restart."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    client.close()
    conn.close()
    srv.close()
    assert lo._port_busy("127.0.0.1", port) is False


# =============================== the normal path is untouched ===============================

def test_the_normal_path_still_leaves_an_up_dashboard_alone(cfg):
    """liftoff's idempotence contract: without the flag, an already-serving dashboard is verified,
    never respawned. The new flag must not have loosened this."""
    spawn, execr = _Recorder(), _Recorder()
    out = io.StringIO()
    rc = lo.main([str(_BIN), str(cfg)],
                 is_dashboard_up=lambda h, p: True, live_runner_pid=lambda s: 777,
                 spawn_dashboard=spawn, exec_runner=execr, out=out)
    assert rc == 0
    assert spawn.calls == [], "the never-double-start guarantee on the normal path"
    assert "leaving it" in out.getvalue()


def test_the_normal_path_never_stops_a_running_dashboard(cfg):
    """Only the explicit flag may stop anything. A routine liftoff must stay a pure start/verify —
    an owner running it to bring up a runner must never lose their dashboard to it."""
    stop = _Recorder()
    out = io.StringIO()
    lo.main([str(_BIN), str(cfg)], is_dashboard_up=lambda h, p: True,
            live_runner_pid=lambda s: 777, spawn_dashboard=_Recorder(), exec_runner=_Recorder(),
            stop_process=stop, out=out)
    assert stop.calls == []


def test_help_mentions_the_restart_flag():
    out = io.StringIO()
    rc = lo.main([str(_BIN), "--help"], out=out)
    assert rc == 0
    assert "--restart-dashboard" in out.getvalue(), "a mechanical remedy nobody can find is not one"


# ================== the wedged dashboard that CAN still name itself (issue #471) ==================
# The 2026-09-02 incident (and its 2026-08-19 rehearsal): the dashboard answered every
# /api/snapshot with HTTP 500 — alive, holding the port, serving static files, and useless. The
# snapshot probe reads a 500 as "not ours" (HTTPError is an OSError), so --restart-dashboard took
# the `refuse` branch and told the owner to Ctrl-C a background process with no tab. Recovery meant
# a hand-kill, at exactly the moment the command exists to spare him one.
#
# The refusal itself was never wrong — liftoff had nothing to identify. What was missing is that
# the wedged server COULD still identify itself: its own code identity (product + pid) is built by
# lib.version, which reads the checkout, not the state homes that crashed the snapshot builder. So
# the fix asks on a second, snapshot-free route (/api/version) rather than loosening the bar for
# whom to signal — the pid still comes from the process's own explicit product claim.


def _ident(pid=98833, product="command-center", skew=False):
    """What /api/version answers: the same version block the snapshot carries, served alone."""
    v = {"server": "aaaa", "server_on_disk": "bbbb" if skew else "aaaa",
         "assets": "cccc", "assets_at_boot": "cccc", "skew": skew,
         "message": "stale" if skew else None,
         "remedy": "bin/liftoff --restart-dashboard", "pid": pid}
    if product is not None:
        v["product"] = product
    return v


def test_a_dashboard_whose_snapshot_500s_is_restarted_not_refused():
    """The incident, as the pure decision. Snapshot silent, port held — but the process named
    itself on the route that does not depend on the state it choked on."""
    d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=True, identity=_ident(pid=98833))
    assert d["action"] == "stop-then-start"
    assert d["pid"] == 98833
    assert "not answering" in d["message"], "the message must say WHY this restart is happening"


def test_an_identity_without_the_product_claim_is_never_signalled():
    """The second route must not be a softer bar than the first: a pid is a number anything can
    print, and /api/version is as easy to squat as /api/snapshot."""
    for impostor in (None, "", "something-else", "Command-Center", 1):
        d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=True,
                                                   identity=_ident(product=impostor))
        assert d["action"] == "refuse", (
            "product %r must not be trusted to hand over a kill target" % (impostor,))
        assert d["pid"] is None


def test_a_malformed_identity_pid_is_refused_not_coerced():
    for bad in (None, 0, -1, "98833", 98833.7, True):
        d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=True,
                                                   identity=_ident(pid=bad))
        assert d["action"] == "refuse", "pid %r must not be trusted as a kill target" % (bad,)


def test_a_held_port_with_no_identity_at_all_is_still_refused():
    """The property the new route must not erode: nothing identified ⇒ nothing signalled. A
    stranger squatting the port answers neither route, and still gets the honest refusal."""
    d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=True, identity=None)
    assert d["action"] == "refuse" and d["pid"] is None
    assert "Ctrl-C" in d["message"]


def test_an_identity_on_a_FREE_port_starts_fresh_and_signals_nothing():
    """A stale identity read (the process died between the two probes) must never become a SIGTERM
    at a pid the kernel has already recycled. The port is the arbiter for starting, as before."""
    d = liftoff_mod.dashboard_restart_decision(URL, None, port_busy=False, identity=_ident())
    assert d["action"] == "start" and d["pid"] is None


def test_a_healthy_snapshot_still_decides_on_its_own_version_block():
    """The identity probe is a FALLBACK. When the snapshot answers, its version block is the
    identity — one probe, one answer, unchanged from issue #136."""
    d = liftoff_mod.dashboard_restart_decision(URL, _snap(pid=4242), port_busy=True,
                                               identity=_ident(pid=98833))
    assert d["pid"] == 4242


# =============================== the snapshot-free identity route ===============================

class _FakeVersion:
    def __init__(self, state):
        self._state = state

    def state(self):
        return dict(self._state)


def _boom():
    raise IndexError("list index out of range")


def test_the_version_route_answers_while_the_snapshot_builder_is_crashing():
    """The whole point: identity must not be reachable only through the thing that is broken. This
    is the exact pairing of the incident — /api/snapshot 500s, /api/version answers."""
    import server as server_mod
    v = _FakeVersion(_ident(pid=98833))
    assert server_mod.route("GET", "/api/snapshot", _boom, "/nonexistent").status == 500
    resp = server_mod.route("GET", "/api/version", _boom, "/nonexistent", version=v)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["product"] == "command-center" and body["pid"] == 98833


def test_the_version_route_is_a_clean_404_when_no_version_is_wired():
    import server as server_mod
    assert server_mod.route("GET", "/api/version", _boom, "/nonexistent").status == 404


def test_the_version_route_never_becomes_an_unhandled_stack_trace():
    import server as server_mod

    class _Broken:
        def state(self):
            raise RuntimeError("stat storm")

    resp = server_mod.route("GET", "/api/version", _boom, "/nonexistent", version=_Broken())
    assert resp.status == 500
    assert json.loads(resp.body)["error"]


def test_the_version_route_is_never_cached():
    import server as server_mod
    resp = server_mod.route("GET", "/api/version", _boom, "/nonexistent",
                            version=_FakeVersion(_ident()))
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== the bin flow, end to end ===============================

def test_restart_heals_a_dashboard_whose_snapshot_endpoint_is_500ing(cfg):
    """William's 2026-09-02 morning, driven: the snapshot probe answers nothing, the port stays
    held until the pid is stopped, and the identity probe is what makes the pid knowable."""
    ident = _Recorder(_ident(pid=98833))
    probe = _Probe([None, None])                      # /api/snapshot never answers
    rc, text, stop, spawn, execr = _run(cfg, probe=probe, identity=ident,
                                        port=_Port(held_for=1))
    assert rc == 0
    assert stop.calls == [((98833,), {})], "stop exactly the pid the wedged dashboard published"
    assert len(spawn.calls) == 1, "and bring a fresh one up on the current build"
    assert execr.calls == [], "--restart-dashboard must never touch the runner"


def test_restart_still_refuses_a_port_holder_that_answers_neither_route(cfg):
    """The stranger case, unchanged: no snapshot, no identity ⇒ no signal and no second dashboard."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([None]),
                                        identity=_Recorder(None), port=_Port(held_for=None))
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert stop.calls == [] and spawn.calls == []
    assert "Ctrl-C" in text


def test_the_identity_probe_reads_the_version_route_not_the_snapshot(monkeypatch):
    """The probe must ask the route that does not depend on the state homes."""
    seen = []

    class _Body:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        return _Body(_ident(pid=4242))

    monkeypatch.setattr(lo.urllib.request, "urlopen", fake_urlopen)
    got = lo._dashboard_identity("127.0.0.1", 8611)
    assert seen == ["http://127.0.0.1:8611/api/version"]
    assert got["pid"] == 4242


def test_the_identity_probe_answers_None_for_anything_that_is_not_ours(monkeypatch):
    """A refusal, a timeout, a non-JSON body, or a JSON body that is not an identity block — all
    read as "nothing identified", which is the refuse branch, not a guess."""
    for payload in ("not json", json.dumps([1, 2, 3]), json.dumps({"hello": "world"})):
        class _Body:
            def __init__(self, raw):
                self._raw = raw

            def read(self):
                return self._raw.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(lo.urllib.request, "urlopen",
                            lambda url, timeout=None, _p=payload: _Body(_p))
        assert lo._dashboard_identity("127.0.0.1", 8611) is None

    def refused(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(lo.urllib.request, "urlopen", refused)
    assert lo._dashboard_identity("127.0.0.1", 8611) is None


def test_the_running_handler_actually_serves_the_identity_over_a_real_socket(tmp_path):
    """The gap the browser drive found, pinned.

    ``route`` had the identity endpoint and the router tests were green — but ``make_handler``'s GET
    branch never passed ``version`` through, so on the REAL server the route answered 404 and the
    whole fix was inert. The unit above proves the router; only a request over a real socket proves
    the wiring. Same class as the 2026-08-05 publish gap: a cure that never reaches the patient.

    The snapshot provider RAISES here — this is the incident's condition, not a healthy server.
    """
    import http.client as http_client
    import threading

    import server as server_mod
    import version as version_mod

    v = version_mod.Version(str(_ROOT))
    srv = server_mod.build_server(_boom, "/nonexistent", port=0, version=v)
    host, port = srv.server_address
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/snapshot")
        snap = conn.getresponse()
        assert snap.status == 500, "the wedged half — this is what the operator was seeing"
        snap.read()
        conn.request("GET", "/api/version")
        r = conn.getresponse()
        assert r.status == 200, "and the half that must keep answering anyway"
        body = json.loads(r.read())
        assert body["product"] == "command-center"
        assert body["pid"] == os.getpid(), "the identity is THIS process, not a number from a file"
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


# =============== a probe must never become a traceback (Codex cross-review, issue #471) ===============
# Both probes document "any error ⇒ None", and that promise is load-bearing: `None` lands on the
# honest refusal, whereas an exception out of the probe kills `liftoff` before it has printed
# anything — the operator's remedy failing with a stack trace instead of a sentence. `urlopen` can
# raise `http.client.HTTPException` (a truncated body, a bad status line) which is NOT an OSError,
# so catching OSError/ValueError alone left a hole in exactly the wedged-server case both probes
# exist for.

class _TruncatedResponse:
    def read(self):
        import http.client
        raise http.client.IncompleteRead(b"half a bo")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("probe", ["_dashboard_identity", "_dashboard_snapshot"])
def test_a_truncated_response_answers_None_rather_than_killing_liftoff(monkeypatch, probe):
    monkeypatch.setattr(lo.urllib.request, "urlopen",
                        lambda url, timeout=None: _TruncatedResponse())
    assert getattr(lo, probe)("127.0.0.1", 8611) is None


@pytest.mark.parametrize("probe", ["_dashboard_identity", "_dashboard_snapshot"])
def test_a_bad_status_line_answers_None_rather_than_killing_liftoff(monkeypatch, probe):
    import http.client

    def garbage(url, timeout=None):
        raise http.client.BadStatusLine("not http at all")

    monkeypatch.setattr(lo.urllib.request, "urlopen", garbage)
    assert getattr(lo, probe)("127.0.0.1", 8611) is None
