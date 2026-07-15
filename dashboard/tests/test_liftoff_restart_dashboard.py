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

  * **never double-start** — the fresh dashboard is spawned only after the old process is confirmed
    GONE. If the old one won't die, liftoff starts nothing and says so. Two dashboards on one port
    means one of them dies at bind and the owner can't tell which is answering.
  * **never a pattern kill** — the pid comes from the snapshot of the process that answered our own
    shape check, so it can only ever name OUR dashboard. ``pkill -f`` collateral-killed William's
    live dashboard once already (2026-07-07); the port-holder is not good enough either, since
    ``_dashboard_up``'s own contract admits a stranger can squat the port.
  * **dashboard-only** — the flag never touches the runner and never claims the tab, so it is safe to
    run from any terminal.
"""
import importlib.util
import io
import json
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

def test_nothing_serving_just_starts():
    d = liftoff_mod.dashboard_restart_decision(URL, None)
    assert d["action"] == "start"
    assert d["pid"] is None


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


class _Life:
    """Process liveness, staged: alive for the first ``alive_for`` checks, then dead. ``alive_for=None``
    means it never dies (the hung-dashboard case)."""

    def __init__(self, alive_for=0):
        self._n = alive_for
        self.calls = 0

    def __call__(self, pid):
        self.calls += 1
        return True if self._n is None else self.calls <= self._n


def _run(cfg_path, *, probe, stop=None, spawn=None, execr=None, sleep=None, alive=None):
    spawn = spawn if spawn is not None else _Recorder()
    execr = execr if execr is not None else _Recorder()
    stop = stop if stop is not None else _Recorder()
    out = io.StringIO()
    rc = lo.main([str(_BIN), str(cfg_path), "--restart-dashboard"],
                 dashboard_snapshot=probe, stop_process=stop,
                 spawn_dashboard=spawn, exec_runner=execr,
                 pid_alive=(alive if alive is not None else _Life(alive_for=0)),
                 sleep=(sleep if sleep is not None else _Recorder()), out=out)
    return rc, out.getvalue(), stop, spawn, execr


def test_restart_stops_exactly_the_reported_pid_then_starts_a_fresh_one(cfg):
    probe = _Probe([_snap(pid=4242), None])          # up, then gone once stopped
    rc, text, stop, spawn, execr = _run(cfg, probe=probe)
    assert rc == 0
    assert stop.calls == [((4242,), {})], "must stop exactly the pid the dashboard reported"
    assert len(spawn.calls) == 1, "a fresh dashboard is started after the old one is gone"
    assert execr.calls == [], "--restart-dashboard must never touch the runner"


def test_restart_never_double_starts_when_the_old_one_will_not_die(cfg):
    """The whole point of the guarantee: if the old process survives, spawning a second one gives two
    dashboards racing for one port. Start nothing, say so."""
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(pid=4242)]),
                                        alive=_Life(alive_for=None))   # never dies
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], "must NOT start a second dashboard while the old one is still alive"
    assert "still alive" in text
    assert "4242" in text, "the owner needs the pid to finish the job by hand"


def test_a_hung_dashboard_that_stops_answering_is_not_mistaken_for_a_dead_one(cfg):
    """The P0 the fresh review caught (issue #136).

    The snapshot probe returns None for a timeout, a transient 500, a truncated body — every symptom
    of a dashboard that is HUNG BUT ALIVE and still holding the port. If probe-silence counted as
    death, liftoff would spawn a replacement beside it; the new process would die at bind, the stale
    server would keep answering, and liftoff would report success. Death is decided by the PROCESS,
    never by the port going quiet.
    """
    probe = _Probe([_snap(pid=4242), None])          # stops answering immediately after the stop…
    rc, text, stop, spawn, execr = _run(cfg, probe=probe, alive=_Life(alive_for=None))  # …but lives
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], (
        "a silent-but-alive dashboard still holds the port — starting a second one is the double "
        "start this flag exists to avoid")
    assert "still alive" in text


def test_a_dashboard_we_could_not_signal_never_gets_a_replacement_beside_it(cfg):
    """EPERM: the pid is not ours to kill. We could not stop it ⇒ we do not start a rival."""
    def boom(pid):
        raise PermissionError("not yours")
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([_snap(pid=4242)]), stop=boom)
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert spawn.calls == [], "never start a second dashboard beside one we could not stop"
    assert "starting nothing" in text


def test_restart_with_nothing_serving_just_starts_one(cfg):
    rc, text, stop, spawn, execr = _run(cfg, probe=_Probe([None]))
    assert rc == 0
    assert stop.calls == [], "nothing to stop — never signal a pid we never saw"
    assert len(spawn.calls) == 1


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
