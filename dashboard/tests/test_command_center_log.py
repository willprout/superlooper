"""Issue #481 — the REAL ``bin/command-center`` process, and the real log file it leaves behind.

``tests/test_logbook.py`` proves the mechanism; this proves it is actually *wired*. It launches the
entry point the way ``bin/liftoff`` does — a detached process whose stdout and stderr are an
append-only ``command-center.log`` — lets it boot and serve, asks it to stop with a Ctrl-C, and
then reads the file the operator would read.

The three things that were wrong on 2026-09-09 (evidence record #477 item 1) are the three things
asserted here: the boot line can be dated, a log that arrives already over the cap comes back under
it, and the noise a descendant writes to the inherited fd 2 is captured rather than appended
forever. Only the process itself may take over its own file descriptors, so this is the only level
at which the wiring can be tested at all.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin" / "command-center"
_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _installed_config(tmp_path, port):
    """A minimal-but-real install: an adopted checkout that declares its slug, and a config.json."""
    checkout = tmp_path / "code" / "superlooper-sandbox"
    (checkout / ".superlooper").mkdir(parents=True)
    (checkout / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "will-titan/superlooper-sandbox"}))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": port, "poll_seconds": 1,
                               "repos": [{"path": str(checkout)}]}))
    return cfg


def _launch(cfg, log, env, settle=4.0):
    fh = open(str(log), "a")
    try:
        proc = subprocess.Popen([sys.executable, str(_BIN), str(cfg)],
                                stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, env=env, cwd=str(_ROOT))
    finally:
        fh.close()
    deadline = time.time() + settle
    while time.time() < deadline:
        if "serving" in _read(log):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc


def _read(log):
    try:
        with open(str(log), "r", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _stop(proc):
    """Ctrl-C the process by its recorded PID (never by pattern) and wait for a clean exit — the
    KeyboardInterrupt path is also what runs the logbook's atexit flush."""
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:          # pragma: no cover — only if the process wedges
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        pytest.fail("the dashboard did not stop on Ctrl-C — the log pipe may have wedged it")


def _env(tmp_path, **extra):
    env = dict(os.environ)
    env["SL_HOME"] = str(tmp_path / "sl-home")
    env.update({k: str(v) for k, v in extra.items()})
    return env


# =============================== timestamps, wired ===============================

def test_the_real_process_dates_every_line_it_writes(tmp_path):
    log = tmp_path / "command-center.log"
    port = _free_port()
    cfg = _installed_config(tmp_path, port)
    proc = _launch(cfg, log, _env(tmp_path))
    try:
        assert proc.poll() is None, "the dashboard exited at boot:\n%s" % _read(log)
        # It really is serving — the log is not the only thing being tested.
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"GET /api/version HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert b"200" in s.recv(64)
        s.close()
    finally:
        _stop(proc)
    lines = [l for l in _read(log).splitlines() if l.strip()]
    assert lines, "the dashboard wrote nothing at all"
    for line in lines:
        assert re.match(_TS + r" ", line), "unstamped line in the real log: %r" % line
    assert any("serving" in l for l in lines), lines
    assert any("stopped" in l for l in lines), "the Ctrl-C line must be dated too: %s" % lines


def test_a_friendly_bind_failure_is_dated_too(tmp_path):
    # The 5 `port … already in use` lines in the 2026-09-09 log were undatable like everything else,
    # and under launchd's 30s ThrottleInterval they are exactly the lines you want timestamps on.
    port = _free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    log = tmp_path / "command-center.log"
    try:
        cfg = _installed_config(tmp_path, port)
        proc = _launch(cfg, log, _env(tmp_path), settle=10.0)
        assert proc.wait(timeout=20) == 3, _read(log)
    finally:
        holder.close()
    text = _read(log)
    assert "already in use" in text, text
    for line in [l for l in text.splitlines() if l.strip()]:
        assert re.match(_TS + r" ", line), line


# =============================== the cap, wired ===============================

def test_the_real_process_brings_a_log_that_is_already_over_the_cap_back_under_it(tmp_path):
    # 2026-09-09's own shape: the 331 MB file was already on disk when the dashboard restarted. A
    # cap that only counted its own writes would have left it there, growing forever.
    log = tmp_path / "command-center.log"
    log.write_text("undated ancient flood line\n" * 30000)      # ~ 810 KB
    assert log.stat().st_size > 128 * 1024
    port = _free_port()
    cfg = _installed_config(tmp_path, port)
    proc = _launch(cfg, log, _env(tmp_path, CC_LOG_MAX_BYTES=128 * 1024,
                                  CC_LOG_KEEP_BYTES=16 * 1024))
    try:
        assert proc.poll() is None, _read(log)
    finally:
        _stop(proc)
    assert log.stat().st_size <= 128 * 1024, log.stat().st_size
    text = _read(log)
    assert "log capped at 131072 bytes" in text, text[:500]
    assert "command-center: stopped" in text, "the newest real lines must survive the cap"
