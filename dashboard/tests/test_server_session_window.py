"""Issue #340 — the server's Open-session-window endpoint (the button's HTTP contract).

The SEMANTICS live in ``lib/session_window.py`` (unit-tested there against the fake superlooper
CLI). This file defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected
``session_window`` object, so the whole request path is testable with no socket:

  * **One endpoint, one step.** ``/api/session-window`` opens a window the owner already owns; it
    changes nothing about the loop, so unlike Tidy/Restart/Janitor there is no preflight and no
    confirm gate to defend — the defence here is that the endpoint cannot be steered.
  * **Same-origin gated, like every write.** It runs a LOCAL COMMAND, so a foreign page must not be
    able to trigger it any more than it could drive the label writer — cross-origin → 403, before
    any command runs.
  * **The target is a NUMBER.** The route parses ``num`` with the same ``_num_of`` the label verbs
    use and refuses anything else at 400, so no client-supplied string ever becomes a subprocess
    argument (the verb refuses again behind it — ``lib/session_window.lane_id``).
  * **Honest outcomes.** A lane with no window is a truthful body at HTTP 200 (the request itself
    was fine) — never a silent success, never a 500.
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import server
import session_window as sw_mod


REPO = "will-titan/command-center"
CHECKOUT = "/home/pat/code/command-center"
FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")


class _RecordingSessionWindow:
    """A stand-in for ``lib.session_window.SessionWindow`` that records dispatch instead of shelling
    the engine — so the ROUTE contract (which repo, which number) is tested independently of the
    verb."""

    def __init__(self, ok=True, outcome="focused", message="opened"):
        self.calls = []
        self._ok, self._outcome, self._message = ok, outcome, message

    def open(self, repo, num):
        self.calls.append((repo, num))
        return {"ok": self._ok, "verb": "session-window", "repo": repo, "num": num,
                "id": "i%s" % num, "outcome": self._outcome, "message": self._message,
                "error": None if self._ok else self._message}


def _post(path, payload, session_window, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        session_window=session_window, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_open_dispatches_with_repo_and_number():
    s = _RecordingSessionWindow()
    resp = _post("/api/session-window", {"repo": REPO, "num": 340}, s)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["verb"] == "session-window" and out["ok"] is True and out["id"] == "i340"
    assert s.calls[-1] == (REPO, 340)


def test_a_lane_with_no_window_is_an_honest_200_body():
    # The command ran fine and the engine correctly said there is nothing to raise → 200 with an
    # honest body the button shows plainly, never an HTTP error (which the UI would render as "the
    # command center is broken" rather than "that session's window is closed").
    s = _RecordingSessionWindow(ok=False, outcome="no_window",
                                message="no session window is recorded for i340")
    resp = _post("/api/session-window", {"repo": REPO, "num": 340}, s)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["ok"] is False and out["outcome"] == "no_window"
    assert out["error"] == "no session window is recorded for i340"


def test_results_are_never_cached():
    resp = _post("/api/session-window", {"repo": REPO, "num": 340}, _RecordingSessionWindow())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== not wired / bad input ===============================

def test_without_the_verb_wired_is_405():
    resp = server.route("POST", "/api/session-window", (lambda: {}), static_root="/x",
                        body=b'{"repo":"x/y","num":1}')
    assert resp.status == 405


def test_missing_repo_is_400():
    s = _RecordingSessionWindow()
    resp = _post("/api/session-window", {"num": 340}, s)
    assert resp.status == 400
    assert s.calls == []                       # never dispatched on bad input


@pytest.mark.parametrize("bad", [None, "i340", "abc", "", [340], {"n": 1}, 0, -5, True])
def test_a_num_that_is_not_a_flight_number_is_400(bad):
    # The bright line of this endpoint: a client cannot name the engine's target. Only a positive
    # integer gets through the router, and lib/session_window derives the lane id behind it.
    s = _RecordingSessionWindow()
    payload = {"repo": REPO} if bad is None else {"repo": REPO, "num": bad}
    resp = _post("/api/session-window", payload, s)
    assert resp.status == 400
    assert s.calls == []


def test_malformed_json_is_400():
    resp = server.route("POST", "/api/session-window", (lambda: {}), static_root="/x",
                        session_window=_RecordingSessionWindow(), body=b"not json {{{")
    assert resp.status == 400


@pytest.mark.parametrize("weird", ["²", "½", " ³ "])
def test_a_numeric_looking_character_int_cannot_parse_is_a_400_not_a_crash(weird):
    # `"²".isdigit()` is True but `int("²")` raises. The router's own num parse must answer 400
    # rather than throw out of the request handler and drop the connection — a client can send this.
    s = _RecordingSessionWindow()
    resp = _post("/api/session-window", {"repo": REPO, "num": weird}, s)
    assert resp.status == 400
    assert s.calls == []


# =============================== CSRF / loopback bright line ===============================

def test_cross_origin_is_refused_403():
    s = _RecordingSessionWindow()
    resp = _post("/api/session-window", {"repo": REPO, "num": 340}, s,
                 origin="https://evil.example.com")
    assert resp.status == 403
    assert s.calls == []                       # a foreign page can't yank the owner's window


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8611", "http://localhost:8611", None])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/session-window", {"repo": REPO, "num": 340}, _RecordingSessionWindow(),
                 origin=origin)
    assert resp.status == 200


def test_a_get_on_the_endpoint_is_not_a_verb():
    # The verb is POST-only, like every other local command here — a link or an <img src> must not
    # be able to open a window.
    resp = server.route("GET", "/api/session-window", (lambda: {}), static_root="/nonexistent",
                        session_window=_RecordingSessionWindow())
    assert resp.status == 404


# =============================== end-to-end (real verb + the fake engine CLI) ===============================

def test_open_over_a_real_socket_shells_the_engines_verb(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_FOCUS_FIXTURES", str(tmp_path))
    real = sw_mod.SessionWindow("/nonexistent/configured-superlooper", {REPO: CHECKOUT})

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, session_window=real)
    host, port = srv.server_address
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/session-window", body=json.dumps({"repo": REPO, "num": 340}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        body = json.loads(r.read())
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=5)

    assert body["ok"] is True and body["outcome"] == "focused" and body["id"] == "i340"
    # The fake logs its argv, so this pins the exact command the whole socket path produced — the
    # engine's read-only verb, scoped to the WATCHED checkout, targeting the derived lane.
    calls = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines() if ln]
    assert [c["argv"] for c in calls] == [
        ["focus-session", "--repo", CHECKOUT, "--id", "i340", "--json"]]


def test_an_unwatched_repo_is_refused_over_the_route(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_FOCUS_FIXTURES", str(tmp_path))
    real = sw_mod.SessionWindow("/nonexistent/configured-superlooper", {REPO: CHECKOUT})
    resp = _post("/api/session-window", {"repo": "someone/else", "num": 340}, real)
    assert resp.status == 200
    assert json.loads(resp.body)["error"] == "unknown repo"
    assert not (tmp_path / "calls.jsonl").exists(), "an unwatched repo must run nothing"
