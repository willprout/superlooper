"""Issue #141 — the server's Deploy Fixer endpoints (the local-session-launch button's HTTP contract).

Deploy Fixer's SEMANTICS live in ``lib/fixer.py`` (unit-tested there against fake-launch-session).
This file defends the HTTP CONTRACT that exposes them — a pure ``route()`` with an injected ``fixer``
object, so the whole request path is testable with no socket and no launch:

  * **Two endpoints, two steps.** ``/api/fixer/check`` is the preflight the note box reads (is a
    fixer already live? what trouble will ride along?); ``/api/fixer`` composes the prompt and
    launches (only ever reached after the owner taps Deploy).
  * **Same-origin gated, like every write.** This endpoint STARTS AN AI SESSION on William's
    machine — the single most consequential thing any button here does. A foreign page must not be
    able to trigger it any more than it could drive the label writer: cross-origin → 403, before
    anything launches.
  * **The snapshot at TAP TIME is the context.** The route reads the CURRENT snapshot from the
    provider and hands it to the verb, so the prompt describes what the owner was actually looking
    at — never a stale context baked at page load, and never a client-supplied one (a client that
    could name the trouble could lie about it).
  * **Honest outcomes.** A live-fixer refusal, an unresolvable shim, a failed launch — each is a
    truthful body at HTTP 200 (the request itself was fine), never a silent success, never a 500.
"""
import json

import pytest

import server

REPO = "will-titan/command-center"
SNAP = {"repos": [{"slug": REPO, "name": "command-center", "flights": [],
                   "alert": {}, "merges_frozen": None, "runner_down": False,
                   "heartbeat_age": 9.0,
                   "state": {"slug": REPO, "level": "alert", "state": "alert", "rank": 90}}]}


class _RecordingFixer:
    """A stand-in for ``lib.fixer.Fixer`` that records dispatch instead of launching a session — so
    the ROUTE contract (which endpoint, which repo, which note, which snapshot) is tested
    independently of verb semantics. Nothing here can ever start an agent."""

    def __init__(self, ok=True, live=False):
        self.calls = []
        self._ok = ok
        self._live = live

    def preflight(self, repo, snapshot):
        self.calls.append(("preflight", repo, None, snapshot))
        return {"ok": True, "verb": "fixer-check", "live": self._live, "live_id": None,
                "trouble": {"slug": repo, "healthy": False, "items": [{"kind": "alert"}]}}

    def execute(self, repo, note, snapshot):
        self.calls.append(("execute", repo, note, snapshot))
        if self._live:
            return {"ok": False, "verb": "fixer", "live": True, "live_id": "d1",
                    "error": "a fixer session (d1) is already running for this repo"}
        return {"ok": self._ok, "verb": "fixer", "id": "d1", "live": self._ok,
                "error": None if self._ok else "the launch failed"}


def _post(path, payload, fixer, origin=None, host=None, provider=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (provider or (lambda: SNAP)), static_root="/nonexistent",
                        fixer=fixer, body=body, origin=origin, host=host)


# =============================== dispatch ===============================

def test_check_dispatches_with_the_repo_and_the_current_snapshot():
    f = _RecordingFixer()
    resp = _post("/api/fixer/check", {"repo": REPO}, f)
    assert resp.status == 200
    assert f.calls == [("preflight", REPO, None, SNAP)]
    body = json.loads(resp.body)
    assert body["ok"] is True and body["live"] is False
    assert body["trouble"]["items"][0]["kind"] == "alert"


def test_execute_dispatches_the_repo_the_note_and_the_snapshot():
    f = _RecordingFixer()
    resp = _post("/api/fixer", {"repo": REPO, "note": "the queue is frozen"}, f)
    assert resp.status == 200
    assert f.calls == [("execute", REPO, "the queue is frozen", SNAP)]
    assert json.loads(resp.body)["id"] == "d1"


def test_the_context_is_read_fresh_at_tap_time_never_supplied_by_the_client():
    # The prompt must describe what the board ACTUALLY shows, so the route reads the provider itself.
    # A client-supplied "trouble" is ignored outright: a client that could name the trouble could
    # lie about it, and the whole honesty claim of this button rests on the server's own read.
    f = _RecordingFixer()
    _post("/api/fixer", {"repo": REPO, "note": "x", "trouble": ["totally-made-up"],
                         "snapshot": {"repos": []}}, f)
    _verb, _repo, _note, snap = f.calls[0]
    assert snap is SNAP, "the snapshot must come from the server's provider, not the request body"


def test_a_fresh_snapshot_is_taken_per_tap():
    seen = []

    def provider():
        seen.append(1)
        return SNAP

    f = _RecordingFixer()
    _post("/api/fixer", {"repo": REPO, "note": "x"}, f, provider=provider)
    assert seen, "the route must call the provider — a context baked at page load would be stale"


# =============================== empty note (DoD: launching with an empty note works) ===============================

@pytest.mark.parametrize("payload", [
    {"repo": REPO},                       # no note key at all — the owner skipped the box
    {"repo": REPO, "note": ""},           # opened the box, typed nothing
    {"repo": REPO, "note": "   "},        # whitespace only
])
def test_an_absent_or_empty_note_still_launches(payload):
    f = _RecordingFixer()
    resp = _post("/api/fixer", payload, f)
    assert resp.status == 200, "the note is OPTIONAL — an empty one is not a bad request"
    assert json.loads(resp.body)["ok"] is True
    assert f.calls and f.calls[0][0] == "execute"


def test_a_non_string_note_is_a_bad_request():
    # A malformed body is the one note shape that IS refused: it means the client is broken, and
    # silently coercing it would put junk in the session's prompt.
    f = _RecordingFixer()
    resp = _post("/api/fixer", {"repo": REPO, "note": {"nope": 1}}, f)
    assert resp.status == 400
    assert f.calls == [], "nothing may launch on a malformed body"


# =============================== validation ===============================

def test_missing_repo_is_a_bad_request():
    for path in ("/api/fixer", "/api/fixer/check"):
        f = _RecordingFixer()
        resp = _post(path, {"note": "x"}, f)
        assert resp.status == 400, path
        assert f.calls == []


def test_a_malformed_body_never_launches():
    f = _RecordingFixer()
    resp = server.route("POST", "/api/fixer", (lambda: SNAP), static_root="/nonexistent",
                        fixer=f, body=b"{not json", origin=None, host=None)
    assert resp.status == 400
    assert f.calls == []


# =============================== the bright lines ===============================

def test_cross_origin_is_refused_before_anything_launches():
    f = _RecordingFixer()
    resp = _post("/api/fixer", {"repo": REPO, "note": "x"}, f,
                 origin="http://evil.example", host="127.0.0.1:8611")
    assert resp.status == 403
    assert f.calls == [], (
        "a foreign page must never start an AI session on this machine — the most consequential "
        "endpoint in the product")


def test_same_origin_is_allowed():
    f = _RecordingFixer()
    resp = _post("/api/fixer", {"repo": REPO, "note": "x"}, f,
                 origin="http://127.0.0.1:8611", host="127.0.0.1:8611")
    assert resp.status == 200
    assert f.calls


def test_no_fixer_wired_is_method_not_allowed():
    # A read-only embedder (or writes disabled) must not expose the launch surface at all.
    for path in ("/api/fixer", "/api/fixer/check"):
        resp = _post(path, {"repo": REPO}, None)
        assert resp.status == 405, path


def test_get_never_launches():
    # A launch behind a GET would be reachable by a bare link / an <img> tag. POST only.
    resp = server.route("GET", "/api/fixer", (lambda: SNAP), static_root="/nonexistent",
                        fixer=_RecordingFixer())
    assert resp.status != 200 or b"delivered" not in (resp.body or b"")
    assert resp.status in (404, 405), resp.status


# =============================== honest outcomes ===============================

def test_a_live_fixer_refusal_is_an_honest_200_body():
    f = _RecordingFixer(live=True)
    resp = _post("/api/fixer", {"repo": REPO, "note": "again"}, f)
    assert resp.status == 200, "the request was fine — the verb's answer is 'no'"
    body = json.loads(resp.body)
    assert body["ok"] is False and body["live"] is True
    assert "already running" in body["error"]


def test_a_failed_launch_is_never_a_silent_success():
    f = _RecordingFixer(ok=False)
    resp = _post("/api/fixer", {"repo": REPO, "note": "x"}, f)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is False and body["error"]
