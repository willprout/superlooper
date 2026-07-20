"""Issue #121 — the server's Janitor endpoints (the GitHub-debris sweep button's HTTP contract).

Janitor's SEMANTICS live in ``lib/janitor.py`` (unit-tested there against fake-superlooper, which
drives the real ``superlooper janitor`` CLI). This file defends the HTTP CONTRACT that exposes them
— a pure ``route()`` with an injected ``janitor`` object, so the whole request path is testable with
no socket:

  * **Two endpoints, two steps.** ``/api/janitor/propose`` returns the proposals the dialog groups
    and shows; ``/api/janitor`` executes EXACTLY the ``keys`` the owner tapped (only ever reached
    after the in-UI confirm) — nothing sweeps that he did not tap.
  * **Same-origin gated, like every write.** Janitor runs a LOCAL COMMAND that writes GitHub, so a
    foreign page must not trigger it — cross-origin → 403, before any command runs.
  * **Honest outcomes.** A command failure is a truthful ``ok: false`` body at HTTP 200 — never a
    silent success, never a 500.

One end-to-end test drives a real loopback socket through a REAL ``lib.janitor.Janitor`` bound to
fake-superlooper and asserts the recorded invocation carried the tapped subset — proving socket →
route → janitor → CLI, with no real ``superlooper`` reachable.
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import server
import janitor as janitor_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
REPO = "will-titan/command-center"
CHECKOUT = "/home/pat/code/command-center"


class _RecordingJanitor:
    """A stand-in for ``lib.janitor.Janitor`` that records dispatch instead of running a command — so
    the ROUTE contract (which endpoint, which repo, which keys) is tested independently of the verb
    semantics."""

    def __init__(self):
        self.calls = []

    def propose(self, repo):
        self.calls.append(("propose", repo))
        return {"ok": True, "verb": "janitor-propose", "repo": repo,
                "groups": [{"kind": "pr", "label": "Superseded PRs",
                            "items": [{"key": "pr:14", "what": "close PR #14", "why": "w",
                                       "target": 14}]}],
                "count": 1, "held": []}

    def execute(self, repo, keys, retry=False):
        self.calls.append(("execute", repo, keys, retry))
        return {"ok": True, "verb": "janitor", "repo": repo,
                "results": [{"key": k, "outcome": "ok"} for k in keys],
                "executed": len(keys), "failed": 0, "skipped": 0, "held": 0}


def _post(path, payload, janitor, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        janitor=janitor, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_propose_dispatches_with_repo():
    j = _RecordingJanitor()
    resp = _post("/api/janitor/propose", {"repo": REPO}, j)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "janitor-propose" and out["count"] == 1
    assert j.calls[-1] == ("propose", REPO)


def test_execute_dispatches_with_the_tapped_subset():
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO, "keys": ["pr:14", "issue:9"]}, j)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "janitor" and out["executed"] == 2
    assert j.calls[-1] == ("execute", REPO, ["pr:14", "issue:9"], False)


def test_execute_defaults_to_no_retry():
    # The holdback contract (issue #121) is the default: a body with no `retry` sweeps normally, so
    # a held-back key stays held. Retry is opt-in, never a side effect of an ordinary sweep.
    j = _RecordingJanitor()
    _post("/api/janitor", {"repo": REPO, "keys": ["pr:14"]}, j)
    assert j.calls[-1] == ("execute", REPO, ["pr:14"], False)


def test_execute_threads_an_explicit_retry_flag():
    # Issue #131: the per-held-row Retry tap sends `retry: true`, which the route hands to the verb
    # (→ the CLI's --retry-refused). The key set is still exactly what was tapped.
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO, "keys": ["branch:sl/i7-x"], "retry": True}, j)
    assert resp.status == 200
    assert j.calls[-1] == ("execute", REPO, ["branch:sl/i7-x"], True)


def test_a_retry_of_more_than_one_action_is_400_and_never_dispatched():
    # Cross-review round 1 (medium): the retry is ONE held row's own tap. A body asking to retry a
    # batch — or nothing at all — is not a request this route understands, and is refused at the
    # boundary before the verb (which enforces the same rule again on the filtered subset).
    for keys in (["branch:sl/i7-x", "pr:41"], []):
        j = _RecordingJanitor()
        resp = _post("/api/janitor", {"repo": REPO, "keys": keys, "retry": True}, j)
        assert resp.status == 400, keys
        assert j.calls == [], keys


def test_a_sweep_of_many_keys_is_still_fine_without_retry():
    # …and the ordinary multi-key sweep is untouched by that rule.
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO, "keys": ["pr:14", "issue:9"], "retry": False}, j)
    assert resp.status == 200
    assert j.calls[-1] == ("execute", REPO, ["pr:14", "issue:9"], False)


def test_execute_with_a_non_boolean_retry_is_400_and_never_dispatched():
    # `retry` re-runs a KNOWN-FAILING GitHub write, so its input is strict: only a real boolean is a
    # request this route understands. A truthy string is refused before any command runs — never
    # silently read as "yes" (and never silently read as "no", which would hide the owner's intent).
    for bad in ("true", 1, [], {"x": 1}):
        j = _RecordingJanitor()
        resp = _post("/api/janitor", {"repo": REPO, "keys": ["pr:14"], "retry": bad}, j)
        assert resp.status == 400, bad
        assert j.calls == [], bad


def test_janitor_results_are_never_cached():
    resp = _post("/api/janitor/propose", {"repo": REPO}, _RecordingJanitor())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== not wired / bad input ===============================

def test_propose_without_janitor_wired_is_405():
    resp = server.route("POST", "/api/janitor/propose", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y"}')
    assert resp.status == 405


def test_execute_without_janitor_wired_is_405():
    resp = server.route("POST", "/api/janitor", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y","keys":["pr:1"]}')
    assert resp.status == 405


def test_missing_repo_is_400():
    j = _RecordingJanitor()
    resp = _post("/api/janitor/propose", {}, j)
    assert resp.status == 400
    assert j.calls == []


def test_execute_without_keys_is_400_and_never_dispatched():
    # keys must be present and a list — a body missing it is a bad request, refused before the CLI.
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO}, j)
    assert resp.status == 400
    assert j.calls == []


def test_execute_with_non_list_keys_is_400():
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO, "keys": "pr:14"}, j)
    assert resp.status == 400
    assert j.calls == []


def test_malformed_json_is_400():
    resp = server.route("POST", "/api/janitor", (lambda: {}), static_root="/x",
                        janitor=_RecordingJanitor(), body=b"not json {{{")
    assert resp.status == 400


# =============================== CSRF / loopback bright line ===============================

def test_cross_origin_propose_is_refused_403():
    j = _RecordingJanitor()
    resp = _post("/api/janitor/propose", {"repo": REPO}, j, origin="https://evil.example.com")
    assert resp.status == 403
    assert j.calls == []


def test_cross_origin_execute_is_refused_403():
    j = _RecordingJanitor()
    resp = _post("/api/janitor", {"repo": REPO, "keys": ["pr:14"]}, j,
                 origin="https://evil.example.com")
    assert resp.status == 403
    assert j.calls == []                       # a foreign page can't sweep GitHub


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8611", "http://localhost:8611", None])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/janitor/propose", {"repo": REPO}, _RecordingJanitor(), origin=origin)
    assert resp.status == 200


# =============================== honest failure (ok:false at 200) ===============================

def test_command_failure_is_ok_false_at_200_not_an_http_error():
    class _FailingJanitor:
        def propose(self, repo):
            return {"ok": False, "verb": "janitor-propose", "repo": repo, "groups": [],
                    "count": 0, "held": [], "error": "could not run the superlooper CLI"}
        def execute(self, repo, keys, retry=False):
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0, "error": "boom"}

    resp = _post("/api/janitor/propose", {"repo": REPO}, _FailingJanitor())
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["ok"] is False and out["error"]


# =============================== end-to-end over a real socket (real Janitor + fake CLI) ==========

def test_propose_then_execute_over_the_socket_to_fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_JANITOR_FIXTURES", str(tmp_path))
    real = janitor_mod.Janitor("/nonexistent/configured", {REPO: CHECKOUT})

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, janitor=real)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/janitor/propose", body=json.dumps({"repo": REPO}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        body = json.loads(r.read())
        assert body["ok"] is True and body["count"] == 3      # the fake proposes three
        conn.close()

        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/janitor", body=json.dumps({"repo": REPO, "keys": ["pr:41"]}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["executed"] == 1
        conn.close()

        # …and the held-back retry (issue #131): the same route, one key, `retry: true` → the CLI
        # gains --retry-refused. Socket → route → real Janitor → CLI, with no real superlooper.
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/janitor",
                     body=json.dumps({"repo": REPO, "keys": ["branch:sl/i7-x"], "retry": True}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["executed"] == 1
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    calls = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines() if ln.strip()]
    argvs = [c["argv"] for c in calls]
    assert ["janitor", "--repo", CHECKOUT, "--json"] in argvs
    assert ["janitor", "--repo", CHECKOUT, "--json", "--execute-keys", "pr:41"] in argvs
    assert ["janitor", "--repo", CHECKOUT, "--json", "--execute-keys", "branch:sl/i7-x",
            "--retry-refused"] in argvs
