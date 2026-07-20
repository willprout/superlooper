"""Issue #34 — ``bin/command-center``'s ``main()`` fails FRIENDLY, never a raw traceback.

The config loader already answers a bad config with one loud, specific line and a clean exit code
(``tests/test_config`` pins the messages). The bind did not: a port already in use died with an
unhandled ``OSError: [Errno 48] Address already in use`` stack trace — and under launchd's KeepAlive
that becomes a hot crash-loop. The README even tells a stranger to "change it if that port is taken",
yet the runtime answered with a traceback instead of that sentence.

``main()`` must now answer a bind failure the way it answers a bad config: ONE friendly, actionable
line naming the port and the ``port`` config key to change — with an exit code DISTINCT from the
config-error code, so a supervisor (or a human reading the launchd log) can tell "wrong config" from
"port taken" apart.

The bin is a hyphenated, extension-less script, so it is loaded by path (conftest already puts
``lib`` + ``bin`` on ``sys.path`` for its imports to resolve).
"""
import errno
import importlib.util
import json
import socket
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin" / "command-center"


def _load_cc():
    # The bin is a hyphenated, extension-less script, so there is no importer to infer — load it
    # through an explicit source loader (conftest already put lib/ + bin/ on sys.path for its imports).
    loader = SourceFileLoader("command_center_bin", str(_BIN))
    spec = importlib.util.spec_from_loader("command_center_bin", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cc = _load_cc()


def _write_installed_config(tmp_path, port):
    """A stranger's minimal-but-real install on disk: an adopted repo checkout that declares its slug,
    and a dashboard config.json pointing at it on ``port``. Enough for ``config.load`` to succeed so
    ``main()`` reaches the socket bind (the state home need not exist — nothing reads it before the
    first poll, which the bind failure precedes)."""
    repo_checkout = tmp_path / "code" / "superlooper-sandbox"
    (repo_checkout / ".superlooper").mkdir(parents=True)
    (repo_checkout / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "will-titan/superlooper-sandbox"}))
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"port": port, "repos": [{"path": str(repo_checkout)}]}))
    return config_file


# =============================== the friendly message (pure) ===============================

def test_bind_error_line_names_the_port_and_the_config_key_for_a_port_in_use():
    # The EADDRINUSE case is the headline: a second copy, or another app, holds the port. The line
    # must name the port AND the 'port' config key to change — the README's "change it if that port
    # is taken" sentence, delivered by the runtime.
    line = cc._bind_error_line(8611, OSError(errno.EADDRINUSE, "Address already in use"))
    assert "8611" in line
    assert "port" in line.lower()
    assert "in use" in line.lower()
    assert line.count("\n") == 1 and line.endswith("\n"), "one friendly line, newline-terminated"
    assert "Traceback" not in line


def test_bind_error_line_is_still_actionable_for_any_other_bind_failure():
    # A non-EADDRINUSE bind failure (e.g. a privileged port the config allowed but the OS refuses)
    # must STILL be a friendly, port-and-key-naming line, never a bare OSError.
    line = cc._bind_error_line(80, OSError(errno.EACCES, "Permission denied"))
    assert "80" in line and "port" in line.lower()
    assert "Traceback" not in line and line.endswith("\n")


# =============================== the exit codes (contract) ===============================

def test_port_in_use_exits_with_the_bind_code_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl-home"))
    # Occupy a port with a real listener, exactly as a second command-center (or another app) would.
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    try:
        config_file = _write_installed_config(tmp_path, port)
        code = cc.main(["command-center", str(config_file)])
    finally:
        occupier.close()

    err = capsys.readouterr().err
    assert code == cc.EXIT_BIND_FAILED
    assert code != cc.EXIT_CONFIG_ERROR, "port-in-use must be distinguishable from a config error"
    assert str(port) in err and "port" in err.lower() and "in use" in err.lower()
    assert "Traceback" not in err


def test_a_config_error_keeps_its_own_distinct_exit_code(tmp_path):
    # Anchor the "distinct" claim: a missing config still exits with the CONFIG code (2), so the bind
    # code (3) is a genuinely different signal a supervisor can branch on.
    code = cc.main(["command-center", str(tmp_path / "does-not-exist.json")])
    assert code == cc.EXIT_CONFIG_ERROR
    assert cc.EXIT_CONFIG_ERROR != cc.EXIT_BIND_FAILED


# =============================== the verb wiring (issue #144) ===============================

def test_the_fixer_is_bound_to_the_same_slug_to_checkout_map_as_every_other_verb(
        tmp_path, monkeypatch, capsys):
    """Issue #144: ``lib/fixer`` is a thin CLI shell now — it passes each repo's CHECKOUT to
    ``superlooper debug --repo``, exactly as Restart/Janitor/Tidy do. Before #144 it was handed a
    ``{"path", "state_home"}`` dict, because it read the state home itself to resolve the pane, the
    brief dir and the worker locks.

    Pin the shape AT THE SEAM: a stale wiring type-checks fine (a dict is a valid mapping value) and
    would only blow up where the checkout reaches the subprocess — and no test may reach the real
    CLI, so nothing else in this suite would notice."""
    captured = {}

    class _CaptureFixer:
        def __init__(self, cli, repo_paths, **kwargs):
            captured["cli"] = cli
            captured["repo_paths"] = repo_paths

    monkeypatch.setattr(cc.fixer_mod, "Fixer", _CaptureFixer)
    monkeypatch.setenv("SL_HOME", str(tmp_path / "sl-home"))
    # Occupy the port so main() returns right after building the verbs — construction is what we
    # are inspecting, and this keeps the test off a real serve loop.
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    try:
        cc.main(["command-center", str(_write_installed_config(tmp_path, port))])
    finally:
        occupier.close()
    capsys.readouterr()

    paths = captured["repo_paths"]
    assert list(paths) == ["will-titan/superlooper-sandbox"]
    for slug, value in paths.items():
        assert isinstance(value, str), (
            "the fixer takes slug → checkout PATH (issue #144), not the pre-#144 "
            "{path, state_home} dict — %r got %r" % (slug, value))
        assert value == str(tmp_path / "code" / "superlooper-sandbox")
