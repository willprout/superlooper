"""Issue #45 — ``bin/liftoff``'s ``main()``: the composition root of the ONE command.

``liftoff`` starts (or verifies already-running) BOTH the dashboard and one watched repo's runner.
``main()`` takes injectable probes/executors (defaulting to the real socket/kill/Popen/execv) so the
orchestration is testable WITHOUT touching a real port, process, or replacing the interpreter:

  * a bad config or an ambiguous ``--repo`` fails FRIENDLY (one line, a clean exit code) — never a
    traceback (mirrors bin/command-center, issue #34);
  * idempotent: an up dashboard is not respawned; a live runner is not re-exec'd;
  * both down ⇒ the dashboard is spawned in the BACKGROUND and the runner is exec'd in the
    FOREGROUND (it takes over this cmux tab — the proven procedure);
  * an exec failure fails friendly too.

The bin is a hyphenated, extension-less script (conftest already put lib/ + bin/ on sys.path), so it
is loaded by path like test_command_center.py loads command-center.
"""
import importlib.util
import io
import json
import os
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin" / "liftoff"


def _load():
    loader = SourceFileLoader("liftoff_bin", str(_BIN))
    spec = importlib.util.spec_from_loader("liftoff_bin", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lo = _load()


# --------------------------- config on disk (config.load enriches from each repo) ---------------------------

def _repo_checkout(base, name, slug):
    d = base / name
    (d / ".superlooper").mkdir(parents=True)
    (d / ".superlooper" / "config.json").write_text(json.dumps({"repo": slug}))
    return d


def _config_file(tmp_path, *repos, **top):
    entries = [{"path": str(p)} for p in repos]
    body = {"repos": entries}
    body.update(top)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(body))
    return p


@pytest.fixture
def one_repo(tmp_path, monkeypatch):
    # SL_HOME under tmp so state homes + the dashboard log dir are writable and isolated.
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    co = _repo_checkout(tmp_path, "sandbox", "will-titan/sandbox")
    return _config_file(tmp_path, co)


class _Recorder:
    def __init__(self, ret=None):
        self.calls = []
        self._ret = ret

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self._ret


def _run(cfg_path, *, up=False, pid=None, extra_argv=(), **over):
    """Drive main() with fully-injected probes/executors; nothing real is touched."""
    spawn = over.pop("spawn", _Recorder())
    execr = over.pop("execr", _Recorder())
    out = io.StringIO()
    rc = lo.main([str(_BIN), str(cfg_path), *extra_argv],
                 is_dashboard_up=lambda host, port: up,
                 live_runner_pid=lambda state_home: pid,
                 spawn_dashboard=spawn, exec_runner=execr, out=out, **over)
    return rc, out.getvalue(), spawn, execr


# --------------------------- friendly failures ---------------------------

def test_missing_config_fails_friendly(tmp_path):
    rc, text, spawn, execr = _run(tmp_path / "nope.json")
    assert rc == lo.EXIT_CONFIG_ERROR
    assert "liftoff:" in text and not spawn.calls and not execr.calls


def test_ambiguous_repo_choice_fails_friendly(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    a = _repo_checkout(tmp_path, "a", "o/a")
    b = _repo_checkout(tmp_path, "b", "o/b")
    cfg = _config_file(tmp_path, a, b)
    rc, text, spawn, execr = _run(cfg)
    assert rc == lo.EXIT_CONFIG_ERROR
    assert "--repo" in text and "o/a" in text and "o/b" in text
    assert not spawn.calls and not execr.calls


# --------------------------- idempotent start / verify ---------------------------

def test_both_down_spawns_dashboard_and_execs_runner(one_repo):
    rc, text, spawn, execr = _run(one_repo, up=False, pid=None)
    assert len(spawn.calls) == 1, "dashboard must be spawned when its port is free"
    assert len(execr.calls) == 1, "runner must be exec'd (foreground) when none is live"
    # the runner argv shells the configured engine CLI + `run --repo <checkout>` (config contract).
    runner_argv = execr.calls[0][0][0]
    assert runner_argv[1:] == ["run", "--repo", str(Path(one_repo).parent / "sandbox")]


def test_dashboard_already_up_is_not_respawned(one_repo):
    rc, text, spawn, execr = _run(one_repo, up=True, pid=None)
    assert not spawn.calls, "an already-serving dashboard must not be respawned"
    assert len(execr.calls) == 1, "the runner half is independent — still started"
    assert "leaving it" in text


def test_live_runner_is_not_reexeced(one_repo):
    rc, text, spawn, execr = _run(one_repo, up=False, pid=4321)
    assert len(spawn.calls) == 1, "the dashboard half is independent — still started"
    assert not execr.calls, "a live runner must not be re-exec'd"
    assert rc == 0 and "pid 4321" in text and "leaving it" in text


def test_both_up_starts_neither_and_verifies(one_repo):
    rc, text, spawn, execr = _run(one_repo, up=True, pid=4321)
    assert not spawn.calls and not execr.calls
    assert rc == 0
    assert "leaving it" in text            # both halves report the verified, already-running state


def test_explicit_repo_selects_the_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    a = _repo_checkout(tmp_path, "a", "o/a")
    b = _repo_checkout(tmp_path, "b", "o/b")
    cfg = _config_file(tmp_path, a, b)
    rc, text, spawn, execr = _run(cfg, up=True, pid=None, extra_argv=["--repo", "o/b"])
    assert len(execr.calls) == 1
    assert execr.calls[0][0][0][1:] == ["run", "--repo", str(b)]


# --------------------------- exec failure fails friendly ---------------------------

def test_help_prints_usage_and_exits_zero():
    out = io.StringIO()
    rc = lo.main([str(_BIN), "--help"], is_dashboard_up=lambda h, p: True,
                 live_runner_pid=lambda s: None, spawn_dashboard=_Recorder(),
                 exec_runner=_Recorder(), out=out)
    assert rc == 0                       # help is not an error
    assert "usage: liftoff" in out.getvalue()


def test_runner_exec_failure_is_friendly(one_repo):
    def boom(argv):
        raise OSError(2, "no such file")
    rc, text, spawn, execr = _run(one_repo, up=True, pid=None, execr=boom)
    assert rc == lo.EXIT_LAUNCH_FAILED
    assert "liftoff:" in text


# --------------------------- _dashboard_up identifies command-center, not just a bound port ---------------------------

class _FakeResp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _patch_urlopen(monkeypatch, handler):
    # monkeypatch runs AFTER conftest's autouse network-block, so it wins for this test only.
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: handler(url))


def test_dashboard_up_true_only_for_our_snapshot_shape(monkeypatch):
    _patch_urlopen(monkeypatch, lambda url: _FakeResp({"generated_at": 1, "repos": []}))
    assert lo._dashboard_up("127.0.0.1", 8611) is True


def test_dashboard_up_false_for_a_foreign_listener(monkeypatch):
    # Some OTHER app holds the port and answers with its own JSON — not command-center. liftoff must
    # NOT call that "already serving" (it would skip starting the dashboard yet leave none running).
    _patch_urlopen(monkeypatch, lambda url: _FakeResp({"hello": "some other app"}))
    assert lo._dashboard_up("127.0.0.1", 8611) is False


def test_dashboard_up_false_when_nothing_answers(monkeypatch):
    def refused(url):
        raise OSError("connection refused")
    _patch_urlopen(monkeypatch, refused)
    assert lo._dashboard_up("127.0.0.1", 8611) is False


def test_dashboard_up_false_on_non_json(monkeypatch):
    _patch_urlopen(monkeypatch, lambda url: _FakeResp(b"<html>not json</html>"))
    assert lo._dashboard_up("127.0.0.1", 8611) is False


def test_exec_runner_uses_execvp_for_path_lookup(monkeypatch):
    # A bare/relative superlooper_cli (the config contract allows it, "resolved against PATH like gh")
    # must be found on PATH — so the real exec path uses execvp, not execv (which needs a full path).
    seen = {}
    monkeypatch.setattr(lo.os, "execvp", lambda file, args: seen.update(file=file, args=args))
    lo._exec_runner(["superlooper", "run", "--repo", "/co/a"])
    assert seen == {"file": "superlooper", "args": ["superlooper", "run", "--repo", "/co/a"]}
