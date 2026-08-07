"""Issue #365 — the server's Stop/Start endpoints (the off switch's HTTP contract).

The verb's SEMANTICS live in ``lib/stopswitch.py`` (unit-tested there against fake-superlooper).
This file defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected
``stopswitch`` object, so the whole request path is testable with no socket:

  * **Two endpoints, one per direction.** ``/api/stop`` is the off switch; ``/api/start`` is the way
    back on. Both are reached only after the dialog's in-UI confirm; there is no preflight, because
    the engine has none and a stop is recorded before anything can die (see ``lib/stopswitch``).
  * **Same-origin gated, like every write — and this is the most consequential one on the box.**
    Restart asks a runner to come back; this one takes production down. A foreign page must not be
    able to reach it, and the refusal happens BEFORE any command runs.
  * **Honest outcomes.** A stop that did not take is a truthful body at HTTP 200 (the request itself
    was fine) — never a silent success, never a 500, and never an inferred one: the ``summary`` the
    server computed rides along so the dialog binds sentences it did not compose.
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import server
import stopswitch as stop_mod

REPO = "will-titan/command-center"
CHECKOUT = "/home/pat/code/command-center"
FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")


class _RecordingSwitch:
    """A stand-in for ``lib.stopswitch.StopSwitch`` that records dispatch instead of taking a real
    runner down — so the ROUTE contract (which endpoint, which repo) is tested independently of
    verb semantics, and no test can ever stop a live loop."""

    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def stop(self, repo):
        self.calls.append(("stop", repo))
        return {"ok": self._ok, "verb": "stop", "process_gone": self._ok,
                "summary": {"level": "ok" if self._ok else "err", "headline": "h",
                            "lines": [], "remedy": None, "stopped": self._ok, "started": False}}

    def start(self, repo):
        self.calls.append(("start", repo))
        return {"ok": self._ok, "verb": "start", "started": self._ok,
                "summary": {"level": "ok" if self._ok else "err", "headline": "h",
                            "lines": [], "remedy": None, "stopped": False, "started": self._ok}}


def _post(path, payload, switch, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        stopswitch=switch, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_stop_dispatches_with_repo():
    s = _RecordingSwitch()
    resp = _post("/api/stop", {"repo": REPO}, s)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "stop" and out["summary"]["stopped"] is True
    assert s.calls[-1] == ("stop", REPO)


def test_start_dispatches_with_repo():
    s = _RecordingSwitch()
    resp = _post("/api/start", {"repo": REPO}, s)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "start" and out["summary"]["started"] is True
    assert s.calls[-1] == ("start", REPO)


def test_a_stop_that_did_not_take_is_an_honest_200_body():
    # The command ran fine and correctly refused → 200 with a body the dialog shows plainly. An
    # HTTP error here would collapse "your loop is still running" into "something went wrong".
    s = _RecordingSwitch(ok=False)
    resp = _post("/api/stop", {"repo": REPO}, s)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["ok"] is False and out["summary"]["stopped"] is False


def test_stop_results_are_never_cached():
    resp = _post("/api/stop", {"repo": REPO}, _RecordingSwitch())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== not wired / bad input ===============================

@pytest.mark.parametrize("path", ["/api/stop", "/api/start"])
def test_without_the_switch_wired_it_is_405(path):
    # A read-only embedder (or writes disabled) must not expose an off switch at all.
    resp = server.route("POST", path, (lambda: {}), static_root="/x", body=b'{"repo":"x/y"}')
    assert resp.status == 405


@pytest.mark.parametrize("path", ["/api/stop", "/api/start"])
def test_missing_repo_is_400(path):
    s = _RecordingSwitch()
    resp = _post(path, {}, s)
    assert resp.status == 400
    assert s.calls == []                       # never dispatched on bad input


def test_malformed_json_is_400():
    s = _RecordingSwitch()
    resp = server.route("POST", "/api/stop", (lambda: {}), static_root="/x",
                        stopswitch=s, body=b"not json {{{")
    assert resp.status == 400
    assert s.calls == []


@pytest.mark.parametrize("path", ["/api/stop", "/api/start"])
def test_the_off_switch_is_post_only(path):
    # A GET that stopped the loop would fire on a prefetch, a link, or an image tag.
    resp = server.route("GET", path, (lambda: {}), static_root="/nonexistent", stopswitch=_RecordingSwitch())
    assert resp.status != 200


# =============================== CSRF / loopback bright line ===============================

@pytest.mark.parametrize("path", ["/api/stop", "/api/start"])
def test_cross_origin_is_refused_403_before_anything_runs(path):
    s = _RecordingSwitch()
    resp = _post(path, {"repo": REPO}, s, origin="https://evil.example.com")
    assert resp.status == 403
    assert s.calls == [], "a foreign page must never be able to stop the loop"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8611", "http://localhost:8611", None])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/stop", {"repo": REPO}, _RecordingSwitch(), origin=origin)
    assert resp.status == 200


# =============== end-to-end over a real socket (real StopSwitch + fake CLI) ===============

def test_stop_then_start_over_the_socket_to_fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))     # the fake logs calls/mutations here
    real = stop_mod.StopSwitch("/nonexistent/configured", {REPO: CHECKOUT}, operator="William")

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, stopswitch=real)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        for path, expect in (("/api/stop", "stopped"), ("/api/start", "started")):
            conn = http_client.HTTPConnection(host, port, timeout=10)
            conn.request("POST", path, body=json.dumps({"repo": REPO}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            assert r.status == 200
            body = json.loads(r.read())
            assert body["summary"][expect] is True
            assert body["summary"]["headline"], "the dialog always has a sentence to show"
            conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    # The recorded invocations prove the socket path reached the CLI with the right shape and the
    # audit fields the engine records in its marker and journal.
    argvs = [json.loads(ln)["argv"]
             for ln in (tmp_path / "calls.jsonl").read_text().splitlines() if ln.strip()]
    assert ["stop", "--repo", CHECKOUT, "--json", "--source", "command-center",
            "--operator", "William"] in argvs
    assert ["start", "--repo", CHECKOUT, "--json", "--source", "command-center",
            "--operator", "William"] in argvs
    muts = [json.loads(ln)
            for ln in (tmp_path / "mutations.jsonl").read_text().splitlines() if ln.strip()]
    assert {m["kind"] for m in muts} == {"runner_stop", "runner_start"}
