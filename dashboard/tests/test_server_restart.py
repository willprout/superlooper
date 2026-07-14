"""Issue #116 — the server's Restart endpoints (the local-command button's HTTP contract).

Restart's SEMANTICS live in ``lib/restart.py`` (unit-tested there against fake-superlooper). This
file defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected ``restart``
object, so the whole request path is testable with no socket:

  * **Two endpoints, two steps.** ``/api/restart/check`` is the preflight the confirm dialog reads
    (is a runner live?); ``/api/restart`` drops the request (only ever reached after the in-UI
    confirm).
  * **Same-origin gated, like every write.** Restart runs a LOCAL COMMAND, so a foreign page must
    not be able to trigger it any more than it could drive the label writer — cross-origin → 403,
    before any command runs.
  * **Honest outcomes.** A dead-runner refusal (or any command failure) is a truthful body at HTTP
    200 (the request itself was fine) — never a silent success, never a 500.
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import restart as restart_mod
import server


REPO = "will-titan/command-center"
CHECKOUT = "/home/pat/code/command-center"
FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")


class _RecordingRestart:
    """A stand-in for ``lib.restart.Restart`` that records dispatch instead of shelling a command —
    so the ROUTE contract (which endpoint, which repo) is tested independently of verb semantics."""

    def __init__(self, running=True):
        self.calls = []
        self._running = running

    def preflight(self, repo):
        self.calls.append(("preflight", repo))
        return {"ok": True, "verb": "restart-check", "running": self._running, "manual": "run it"}

    def execute(self, repo):
        self.calls.append(("execute", repo))
        return {"ok": self._running, "verb": "restart", "running": self._running,
                "requested": self._running, "manual": "run it"}


def _post(path, payload, restart, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        restart=restart, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_check_dispatches_with_repo():
    r = _RecordingRestart()
    resp = _post("/api/restart/check", {"repo": REPO}, r)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "restart-check" and out["running"] is True
    assert r.calls[-1] == ("preflight", REPO)


def test_execute_dispatches_with_repo():
    r = _RecordingRestart()
    resp = _post("/api/restart", {"repo": REPO}, r)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "restart" and out["requested"] is True
    assert r.calls[-1] == ("execute", REPO)


def test_dead_runner_refusal_is_an_honest_200_body():
    # No live runner: the command ran fine and correctly refused → 200 with an honest body the button
    # shows plainly, never an HTTP error.
    r = _RecordingRestart(running=False)
    resp = _post("/api/restart", {"repo": REPO}, r)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["ok"] is False and out["running"] is False


def test_restart_results_are_never_cached():
    resp = _post("/api/restart/check", {"repo": REPO}, _RecordingRestart())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== not wired / bad input ===============================

def test_check_without_restart_wired_is_405():
    resp = server.route("POST", "/api/restart/check", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y"}')
    assert resp.status == 405


def test_execute_without_restart_wired_is_405():
    resp = server.route("POST", "/api/restart", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y"}')
    assert resp.status == 405


def test_missing_repo_is_400():
    r = _RecordingRestart()
    resp = _post("/api/restart", {}, r)
    assert resp.status == 400
    assert r.calls == []                       # never dispatched on bad input


def test_malformed_json_is_400():
    resp = server.route("POST", "/api/restart", (lambda: {}), static_root="/x",
                        restart=_RecordingRestart(), body=b"not json {{{")
    assert resp.status == 400


# =============================== CSRF / loopback bright line ===============================

def test_cross_origin_check_is_refused_403():
    r = _RecordingRestart()
    resp = _post("/api/restart/check", {"repo": REPO}, r, origin="https://evil.example.com")
    assert resp.status == 403
    assert r.calls == []                       # a foreign page can't even probe liveness


def test_cross_origin_execute_is_refused_403():
    r = _RecordingRestart()
    resp = _post("/api/restart", {"repo": REPO}, r, origin="https://evil.example.com")
    assert resp.status == 403
    assert r.calls == []                       # ...and certainly can't request the restart


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8611", "http://localhost:8611", None])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/restart/check", {"repo": REPO}, _RecordingRestart(), origin=origin)
    assert resp.status == 200


# =============================== end-to-end over a real socket (real Restart + fake CLI) ==========

def test_check_then_request_over_the_socket_to_fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))     # the fake logs calls/mutations here
    real_restart = restart_mod.Restart("/nonexistent/configured", {REPO: CHECKOUT}, operator="William")

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, restart=real_restart)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/restart/check", body=json.dumps({"repo": REPO}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["running"] is True       # the fake reports a live runner
        conn.close()

        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/restart", body=json.dumps({"repo": REPO}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["requested"] is True
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    # The recorded invocations prove the socket path reached the CLI with the right shape: --check
    # writes nothing (no mutation), the request signs with the operator + command-center source.
    calls = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines() if ln.strip()]
    argvs = [c["argv"] for c in calls]
    assert ["request-restart", "--repo", CHECKOUT, "--json", "--check"] in argvs
    muts = [json.loads(ln) for ln in (tmp_path / "mutations.jsonl").read_text().splitlines() if ln.strip()]
    req = [m for m in muts if m["kind"] == "restart_request"]
    assert req and req[-1]["operator"] == "William" and req[-1]["source"] == "command-center"
