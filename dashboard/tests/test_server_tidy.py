"""Issue #41 — the server's Tidy endpoints (the local-command button's HTTP contract).

Tidy's SEMANTICS live in ``lib/tidy.py`` (unit-tested there against fake-superlooper). This file
defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected ``tidy`` object,
so the whole request path is testable with no socket:

  * **Two endpoints, two steps.** ``/api/tidy/dry-run`` returns the list the confirm dialog shows;
    ``/api/tidy`` executes the close (only ever reached after the in-UI confirm).
  * **Same-origin gated, like every write.** Tidy runs a LOCAL COMMAND, so a foreign page must not
    be able to trigger it any more than it could drive the label writer — cross-origin → 403,
    before any command runs.
  * **Honest outcomes.** A command failure (nonzero exit, missing binary) is a truthful ``ok:
    false`` body at HTTP 200 (the request itself was fine) — never a silent success, never a 500.

One end-to-end test drives a real loopback socket through a REAL ``lib.tidy.Tidy`` bound to
fake-superlooper and asserts the recorded invocation — proving socket → route → tidy → CLI, with no
real ``superlooper`` reachable (the conftest keeps ``SL_SUPERLOOPER`` absent by default).
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import server
import tidy as tidy_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
REPO = "will-titan/command-center"
CHECKOUT = "/home/pat/code/command-center"


class _RecordingTidy:
    """A stand-in for ``lib.tidy.Tidy`` that records dispatch instead of running a command — so the
    ROUTE contract (which endpoint, which repo) is tested independently of the verb semantics."""

    def __init__(self):
        self.calls = []

    def dry_run(self, repo):
        self.calls.append(("dry_run", repo))
        return {"ok": True, "verb": "tidy-dry-run", "repo": repo,
                "windows": [{"id": "i23", "status": "merged", "surface": "s"}], "count": 1}

    def execute(self, repo):
        self.calls.append(("execute", repo))
        return {"ok": True, "verb": "tidy", "repo": repo, "closed": 1}


def _post(path, payload, tidy, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        tidy=tidy, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_dry_run_dispatches_with_repo():
    t = _RecordingTidy()
    resp = _post("/api/tidy/dry-run", {"repo": REPO}, t)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "tidy-dry-run"
    assert out["count"] == 1
    assert t.calls[-1] == ("dry_run", REPO)


def test_execute_dispatches_with_repo():
    t = _RecordingTidy()
    resp = _post("/api/tidy", {"repo": REPO}, t)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "tidy"
    assert out["closed"] == 1
    assert t.calls[-1] == ("execute", REPO)


def test_tidy_results_are_never_cached():
    resp = _post("/api/tidy/dry-run", {"repo": REPO}, _RecordingTidy())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== not wired / bad input ===============================

def test_tidy_without_tidy_wired_is_405():
    # Tidy surface off for this embedder (no tidy object) → method not allowed, never a crash.
    resp = server.route("POST", "/api/tidy/dry-run", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y"}')
    assert resp.status == 405


def test_execute_without_tidy_wired_is_405():
    resp = server.route("POST", "/api/tidy", (lambda: {}), static_root="/x", body=b'{"repo":"x/y"}')
    assert resp.status == 405


def test_missing_repo_is_400():
    t = _RecordingTidy()
    resp = _post("/api/tidy/dry-run", {}, t)
    assert resp.status == 400
    assert t.calls == []                       # never dispatched on bad input


def test_malformed_json_is_400():
    resp = server.route("POST", "/api/tidy", (lambda: {}), static_root="/x",
                        tidy=_RecordingTidy(), body=b"not json {{{")
    assert resp.status == 400


# =============================== CSRF / loopback bright line ===============================

def test_cross_origin_dry_run_is_refused_403():
    t = _RecordingTidy()
    resp = _post("/api/tidy/dry-run", {"repo": REPO}, t, origin="https://evil.example.com")
    assert resp.status == 403
    assert t.calls == []                       # a foreign page can't even LIST via the command


def test_cross_origin_execute_is_refused_403():
    t = _RecordingTidy()
    resp = _post("/api/tidy", {"repo": REPO}, t, origin="https://evil.example.com")
    assert resp.status == 403
    assert t.calls == []                       # ...and certainly can't run the close


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8611", "http://localhost:8611", None])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/tidy/dry-run", {"repo": REPO}, _RecordingTidy(), origin=origin)
    assert resp.status == 200


# =============================== honest failure (ok:false at 200, never a silent success) ========

def test_command_failure_is_ok_false_at_200_not_an_http_error():
    class _FailingTidy:
        def dry_run(self, repo):
            return {"ok": False, "verb": "tidy-dry-run", "repo": repo, "windows": [], "count": 0,
                    "error": "could not run the superlooper CLI"}
        def execute(self, repo):
            return {"ok": False, "verb": "tidy", "repo": repo, "closed": 0, "error": "boom"}

    resp = _post("/api/tidy/dry-run", {"repo": REPO}, _FailingTidy())
    assert resp.status == 200                  # the request was fine; the command outcome is honest
    out = json.loads(resp.body)
    assert out["ok"] is False and out["error"]


# =============================== end-to-end over a real socket (real Tidy + fake CLI) ============

def test_dry_run_then_execute_over_the_socket_to_fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_TIDY_FIXTURES", str(tmp_path))
    real_tidy = tidy_mod.Tidy("/nonexistent/configured", {REPO: CHECKOUT})

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, tidy=real_tidy)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/tidy/dry-run", body=json.dumps({"repo": REPO}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        body = json.loads(r.read())
        assert body["ok"] is True and body["count"] == 2      # the fake lists two windows
        conn.close()

        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/tidy", body=json.dumps({"repo": REPO}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["closed"] == 2
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    # The recorded invocations prove the socket path reached the CLI with the right shape.
    calls = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines() if ln.strip()]
    argvs = [c["argv"] for c in calls]
    assert ["tidy", "--repo", CHECKOUT, "--dry-run"] in argvs
    assert ["tidy", "--repo", CHECKOUT, "--yes"] in argvs
    assert all("--all" not in a for a in argvs)               # merged-only, end to end
