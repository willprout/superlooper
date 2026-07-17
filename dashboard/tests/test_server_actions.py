"""Task 6 — the server's POST action routes (the buttons become real writes).

The verbs' SEMANTICS live in ``lib/actions.py`` (unit-tested there against fake-gh). This file
defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected actions object, so
the whole request path is testable with no socket:

  * **Right verb, right params.** Each endpoint dispatches to the matching action with the repo +
    num (or text) parsed from the JSON body; discuss composes from the injected snapshot.
  * **CSRF / loopback bright line.** The server binds 127.0.0.1, but a page in the browser could
    still POST to it — so a cross-origin write is refused (403). A same-origin (loopback) or
    non-browser (no Origin) caller is allowed.
  * **Fail closed on bad input.** Malformed JSON, a missing repo, or a bad num is a clean 4xx, never
    a stack trace or an accidental write.

One end-to-end test drives a real loopback socket through a REAL ``Actions`` bound to the fake-gh
harness and asserts the recorded mutation — proving socket → route → actions → gh actually writes,
with no real gh reachable.
"""
import json
import threading
from http import client as http_client
from pathlib import Path

import pytest

import actions as actions_mod
import gh
import server

FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"
REPO = "will-titan/command-center"


class _RecordingActions:
    """A stand-in for ``lib.actions.Actions`` that records dispatch instead of touching gh — so the
    ROUTE contract (which verb, which params) is tested independently of the verb semantics."""

    def __init__(self):
        self.calls = []

    def _rec(self, verb, *a):
        self.calls.append((verb,) + a)
        return {"ok": True, "verb": verb}

    def approve(self, repo, num): return self._rec("approve", repo, num)
    def drop(self, repo, num): return self._rec("drop", repo, num)
    def expedite(self, repo, num): return self._rec("expedite", repo, num)
    def bounce_yes(self, repo, num): return self._rec("bounce-yes", repo, num)
    def rebuild(self, repo, num): return self._rec("rebuild", repo, num)

    def flag(self, repo, text):
        self.calls.append(("flag", repo, text))
        return {"ok": True, "verb": "flag", "num": 9001}

    def answer(self, repo, text, num):
        self.calls.append(("answer", repo, text, num))
        return {"ok": True, "verb": "answer"}


def _post(path, payload, acts, origin=None, snap=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: snap or {}), static_root="/nonexistent",
                        actions=acts, body=body, origin=origin, host=host)


# =============================== dispatch: the four label/close verbs ===============================

@pytest.mark.parametrize("path,verb", [
    ("/api/approve", "approve"),
    ("/api/drop", "drop"),
    ("/api/expedite", "expedite"),
    ("/api/bounce-yes", "bounce-yes"),
    ("/api/rebuild", "rebuild"),          # issue #161: the explicit rebuild-from-scratch verb
])
def test_label_verb_dispatches_with_repo_and_num(path, verb):
    acts = _RecordingActions()
    resp = _post(path, {"repo": REPO, "num": 12}, acts)
    assert resp.status == 200
    assert json.loads(resp.body)["verb"] == verb
    assert acts.calls[-1] == (verb, REPO, 12)


def test_num_given_as_a_string_is_coerced_to_int():
    acts = _RecordingActions()
    _post("/api/approve", {"repo": REPO, "num": "12"}, acts)
    assert acts.calls[-1] == ("approve", REPO, 12)


def test_action_results_are_never_cached():
    # A write result must never be served from a stale cache.
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, _RecordingActions())
    assert resp.headers.get("Cache-Control") == "no-store"


# =============================== flag ===============================

def test_flag_dispatches_the_raw_text():
    acts = _RecordingActions()
    resp = _post("/api/flag", {"repo": REPO, "text": "the clack is too loud"}, acts)
    assert resp.status == 200
    assert json.loads(resp.body)["num"] == 9001
    assert acts.calls[-1] == ("flag", REPO, "the clack is too loud")


def test_flag_missing_text_is_400():
    resp = _post("/api/flag", {"repo": REPO}, _RecordingActions())
    assert resp.status == 400


# =============================== answer (#163 — text + num) ===============================

def test_answer_dispatches_repo_text_and_num():
    acts = _RecordingActions()
    resp = _post("/api/answer", {"repo": REPO, "num": "12", "text": "use A"}, acts)
    assert resp.status == 200
    assert json.loads(resp.body)["verb"] == "answer"
    assert acts.calls[-1] == ("answer", REPO, "use A", 12)      # num coerced to int


def test_answer_missing_text_is_400():
    resp = _post("/api/answer", {"repo": REPO, "num": 12}, _RecordingActions())
    assert resp.status == 400


def test_answer_missing_num_is_400():
    resp = _post("/api/answer", {"repo": REPO, "text": "use A"}, _RecordingActions())
    assert resp.status == 400


def test_answer_cross_origin_is_refused_403():
    resp = _post("/api/answer", {"repo": REPO, "num": 1, "text": "x"}, _RecordingActions(),
                 origin="https://evil.example.com")
    assert resp.status == 403


# =============================== discuss (composes from the snapshot) ===============================

def test_discuss_composes_briefing_from_the_snapshot():
    snap = {"repos": [{"slug": REPO, "name": "command-center", "flights": [
        {"num": 8, "label": "SL-8", "stage": "parked", "memo": "scope looks wrong",
         "pr": None, "attempt": 1, "cargo": {"present": False}}]}]}
    resp = _post("/api/discuss", {"repo": REPO, "num": 8}, _RecordingActions(), snap=snap)
    assert resp.status == 200
    out = json.loads(resp.body)
    assert out["ok"] is True
    assert "SL-8" in out["text"]
    assert "scope looks wrong" in out["text"]      # composed from the flight's own facts


def test_discuss_does_not_write_to_gh():
    # Discuss is a read/compose verb — it must never call an action write method.
    acts = _RecordingActions()
    snap = {"repos": [{"slug": REPO, "name": "cc", "flights": []}]}
    _post("/api/discuss", {"repo": REPO, "num": 8}, acts, snap=snap)
    assert acts.calls == []


# =============================== CSRF / loopback bright line ===============================

def test_cross_origin_post_is_refused_403():
    # A page on evil.com must not be able to drive the label-writer, even though it binds localhost.
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, _RecordingActions(),
                 origin="https://evil.example.com")
    assert resp.status == 403


def test_null_origin_is_refused():
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, _RecordingActions(), origin="null")
    assert resp.status == 403


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8611", "http://localhost:8611", None,
])
def test_loopback_or_absent_origin_is_allowed(origin):
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, _RecordingActions(), origin=origin)
    assert resp.status == 200


def test_cross_origin_never_reaches_the_action():
    acts = _RecordingActions()
    _post("/api/approve", {"repo": REPO, "num": 1}, acts, origin="https://evil.example.com")
    assert acts.calls == []                         # refused BEFORE any write


def test_another_localhost_port_is_refused_when_host_is_known():
    # A page at http://localhost:3000 is a DIFFERENT origin — a loopback hostname alone must not
    # clear it. When we know the Host the request targeted, require the Origin to match it.
    acts = _RecordingActions()
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, acts,
                 origin="http://localhost:3000", host="127.0.0.1:8611")
    assert resp.status == 403
    assert acts.calls == []


def test_same_host_and_port_origin_is_allowed():
    resp = _post("/api/approve", {"repo": REPO, "num": 1}, _RecordingActions(),
                 origin="http://127.0.0.1:8611", host="127.0.0.1:8611")
    assert resp.status == 200


# =============================== bad input fails closed ===============================

def test_malformed_json_is_400():
    resp = server.route("POST", "/api/approve", (lambda: {}), static_root="/x",
                        actions=_RecordingActions(), body=b"not json {{{")
    assert resp.status == 400


def test_missing_repo_is_400():
    resp = _post("/api/approve", {"num": 1}, _RecordingActions())
    assert resp.status == 400


def test_bad_num_is_400():
    resp = _post("/api/approve", {"repo": REPO, "num": "not-a-number"}, _RecordingActions())
    assert resp.status == 400


@pytest.mark.parametrize("bad", [0, -5, "0", "-5"])
def test_non_positive_num_is_400(bad):
    # Issue numbers are positive; 0 / negatives are invalid input that must never reach the writer.
    resp = _post("/api/approve", {"repo": REPO, "num": bad}, _RecordingActions())
    assert resp.status == 400


def test_unknown_post_path_is_404_when_writes_enabled():
    resp = _post("/api/nope", {"repo": REPO, "num": 1}, _RecordingActions())
    assert resp.status == 404


def test_post_without_actions_wired_is_405():
    # Writes not enabled at all → method not allowed (Task 5's contract, preserved).
    resp = server.route("POST", "/api/approve", (lambda: {}), static_root="/x")
    assert resp.status == 405


def test_get_still_serves_snapshot_after_post_wiring():
    # POST support must not disturb the GET contract.
    resp = server.route("GET", "/api/snapshot", (lambda: {"ok": 1}), static_root="/x")
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": 1}


# =============================== end-to-end over a real loopback socket (real Actions + fake-gh) ===============================

def test_post_flag_writes_through_the_socket_to_fake_gh(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_GH", str(FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(tmp_path))
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "555")
    real_actions = actions_mod.Actions(gh, allowed_repos=[REPO], today=lambda: "2026-07-07")

    srv = server.build_server(lambda: {}, "/nonexistent", port=0, actions=real_actions)
    host, port = srv.server_address
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/api/flag",
                     body=json.dumps({"repo": REPO, "text": "socket path works"}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read())["num"] == 555
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    muts = [json.loads(ln) for ln in (tmp_path / "mutations.jsonl").read_text().splitlines() if ln.strip()]
    created = [m for m in muts if m["kind"] == "create_issue"][-1]
    assert created["body"] == "socket path works"
    assert created["labels"] == "flag"
