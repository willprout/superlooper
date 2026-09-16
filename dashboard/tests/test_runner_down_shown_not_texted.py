"""Issue #496 — the board SHOWS a downed runner; the dashboard no longer TEXTS about one.

Until #496 the command center carried its own dead-man's-switch push: past
``heartbeat_down_seconds`` it sent "RUNNER DOWN — <slug>" through a notifier of its own, guarded by
an in-memory once-per-episode edge detector. The engine watchdog (``superlooper watchdog``) owns
runner-down detection and paging — its ``heartbeat_stale`` signal, resurrection, and the texts
around them — so the dashboard's push was a second, earlier, noisier sender for the same fact: no
quiet hours, no demand awareness, and a detector every server restart re-armed and re-fired. Owner
ruling 2026-09-16: retired, not disabled.

What must survive is the other half. A stale heartbeat still greys the board RUNNER DOWN at the
same threshold, because showing the runner's state is the dashboard's job. So the headline test
drives the REAL composition root — ``bin/command-center``'s ``build_provider``, the exact code that
used to dispatch the push on every poll — over a stale heartbeat, across a simulated server restart,
and asserts both halves at once: the board says RUNNER DOWN on every poll, and nothing left the
process to say it anywhere else.
"""
import json
import shutil
import subprocess
import threading
import time
import importlib.util
import inspect
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import config as config_mod
import desk as desk_mod
import server

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "statehome"
SLUG = "will-titan/superlooper-sandbox"


def _load_cc():
    loader = SourceFileLoader("command_center_bin_496", str(_ROOT / "bin" / "command-center"))
    spec = importlib.util.spec_from_loader("command_center_bin_496", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cc = _load_cc()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A real install on disk — config.json → an adopted checkout → a state home under $SL_HOME —
    with the ALERT and freeze removed so RUNNER DOWN is the one thing wrong with the board."""
    base = tmp_path / "sl-home"
    home = base / "will-titan__superlooper-sandbox"
    shutil.copytree(_FIXTURE, home)
    (home / "state" / "ALERT").unlink()
    (home / "state" / "merges_frozen.json").unlink()
    monkeypatch.setenv("SL_HOME", str(base))

    checkout = tmp_path / "code" / "superlooper-sandbox"
    (checkout / ".superlooper").mkdir(parents=True)
    (checkout / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": SLUG, "required_checks": ["tests"]}))
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"repos": [{"path": str(checkout)}]}))
    return config_file, home


@pytest.fixture
def egress(monkeypatch):
    """Every way the retired push ever left the process, recorded: the daemon thread it was sent
    on (off the poll thread, whatever the channel — even log-only) and the subprocess a configured
    channel ran (the ``osascript`` iMessage one-liner, or a ``bash -lc`` cmd template)."""
    seen = {"threads": [], "spawns": []}
    real_start = threading.Thread.start
    real_run, real_popen = subprocess.run, subprocess.Popen

    def start(self, *a, **kw):
        seen["threads"].append(getattr(self, "_target", None))
        return real_start(self, *a, **kw)

    def run(args, *a, **kw):
        seen["spawns"].append(list(args) if isinstance(args, (list, tuple)) else [args])
        return real_run(args, *a, **kw)

    class Popen(real_popen):
        def __init__(self, args, *a, **kw):
            seen["spawns"].append(list(args) if isinstance(args, (list, tuple)) else [args])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    return seen


def _notifier_spawns(spawns):
    return [a for a in spawns
            if any("osascript" in str(x) for x in a) or (a[:2] == ["bash", "-lc"])]


def test_a_stale_heartbeat_still_reads_runner_down_and_nothing_is_sent(installed, egress, capsys):
    config_file, home = installed
    (home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 900))
    cfg = config_mod.load(str(config_file))

    # Two server lifetimes: the retired detector lived in memory, so a restart re-armed it and
    # re-pushed for a runner still down. Neither lifetime may send anything now.
    for _lifetime in range(2):
        provider = cc.build_provider(cfg, desk_mod.Desk(desk_mod.default_path()))
        for _poll in range(3):
            snap = provider()
            assert snap["runner"]["down"] is True
            assert snap["runner"]["repos"][0]["slug"] == SLUG
            assert snap["runner"]["repos"][0]["down"] is True
            assert snap["runner"]["down_seconds"] == 300, "the threshold is unchanged"
            assert snap["runner"]["message"].startswith("last heartbeat 15m")
            assert snap["pill"]["level"] == "alert"
            assert snap["pill"]["message"] == "RUNNER DOWN"
            assert snap["trouble"]["present"] is True

    assert egress["threads"] == [], "a poll must not spawn a sender thread"
    assert _notifier_spawns(egress["spawns"]) == [], "no osascript / cmd notifier ran"
    err = capsys.readouterr().err
    assert "RUNNER DOWN" not in err, "nor a log-only send recorded on stderr"


def test_a_heartbeat_that_never_existed_still_reads_runner_down_and_nothing_is_sent(installed,
                                                                                    egress):
    config_file, _home = installed                      # the fixture carries no runner.heartbeat
    cfg = config_mod.load(str(config_file))
    snap = cc.build_provider(cfg, None)()
    assert snap["runner"]["down"] is True
    assert snap["runner"]["message"].startswith("no runner heartbeat found")
    assert egress["threads"] == []
    assert _notifier_spawns(egress["spawns"]) == []


def test_the_threshold_that_greys_the_board_is_still_heartbeat_down_seconds(installed):
    config_file, home = installed
    cfg = config_mod.load(str(config_file))
    now = 1_783_364_300
    beat = home / "state" / "runner.heartbeat"
    beat.write_text(str(now - 299))
    assert server.assemble_snapshot(cfg, now=now)["runner"]["down"] is False
    beat.write_text(str(now - 301))
    assert server.assemble_snapshot(cfg, now=now)["runner"]["down"] is True


def test_the_provider_takes_no_push_memory():
    # The in-memory edge detector was a positional argument of the composition root. Removing the
    # push (not disabling it) means there is nothing left to hand it.
    params = list(inspect.signature(cc.build_provider).parameters)
    assert params == ["cfg", "dashboard_desk", "version", "engine"]


def test_the_push_machinery_is_removed_not_disabled():
    for gone in ("notify.py", "watchdog.py"):
        assert not (_ROOT / "lib" / gone).exists(), "lib/%s served only the retired push" % gone
    for name in ("runner_down_push", "runner_down_pushes", "dispatch_runner_pushes",
                 "notify_mod"):
        assert not hasattr(server, name), "server.%s belongs to the retired push" % name
    for path in [*(_ROOT / "lib").glob("*.py"), *(p for p in (_ROOT / "bin").iterdir()
                                                  if p.is_file())]:
        text = path.read_text(errors="replace")
        for token in ("import notify", "import watchdog", "notify.send", "newly_down("):
            assert token not in text, "%s still carries %r" % (path.name, token)
