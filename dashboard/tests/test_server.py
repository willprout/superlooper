"""Task 5 — the localhost server + static router.

Two disciplines pinned here, both load-bearing constraints (not nice-to-haves):

  * **Localhost only (bright line).** The server can write GitHub labels — William's word — so it
    must never be reachable off the machine. ``build_server`` binds ``127.0.0.1`` and ONLY that;
    a test asserts the bound address and that any non-loopback host is refused loudly.
  * **A pure router.** ``route()`` maps (method, path) to a response with NO socket in sight, so
    the snapshot handler is unit-tested with an INJECTED snapshot (design record B.1: the JS binds
    values; the semantics — here, the HTTP contract — are tested in Python). Static serving is
    path-traversal safe: a ``..`` escape resolves to 404, never a file outside the static root.

The one end-to-end test binds a real loopback socket on an ephemeral port and round-trips through
``http.client`` (NOT ``urllib`` — the conftest blocks that as a network egress; a loopback
connection to our own in-process server is not egress). It proves the socket path actually serves.
"""
import json
import os
import threading
from http import client as http_client

import pytest

import server


# =============================== format_duration (boring-mode numerals) ===============================
# Every boring-mode visual channel is paired with an EXACT numeral (design record §4). The duration
# formatter is the honest human string beside the raw seconds the table sorts by.

def test_format_duration_none_is_em_dash():
    assert server.format_duration(None) == "—"


def test_format_duration_seconds_under_a_minute():
    assert server.format_duration(45) == "45s"


def test_format_duration_minutes():
    assert server.format_duration(41 * 60) == "41m"


def test_format_duration_hours_never_roll_into_days():
    # The design shows a 26h-old park as "26H", never "1d 2h" — hours stay hours (design record §4).
    assert server.format_duration(26 * 3600) == "26h"
    assert server.format_duration(3 * 86400) == "72h"


def test_format_duration_handles_non_finite():
    # A corrupt journal ts can be JSON NaN/Infinity (json.loads accepts them). The formatter must
    # degrade to "—", never raise a ValueError that would fail-close the whole snapshot to a 500.
    assert server.format_duration(float("nan")) == "—"
    assert server.format_duration(float("inf")) == "—"


def test_format_duration_floors_partial_minutes():
    # 9m24s reads "9m" — the numeral is honest to the minute, never rounded up to look busier.
    assert server.format_duration(9 * 60 + 24) == "9m"


# =============================== route: /api/snapshot with an INJECTED snapshot ===============================

def test_snapshot_route_serves_injected_snapshot_as_json():
    snap = {"pill": {"level": "ok"}, "flights": [], "poll_seconds": 2}
    resp = server.route("GET", "/api/snapshot", lambda: snap, static_root="/nonexistent")
    assert resp.status == 200
    assert resp.content_type == "application/json"
    assert json.loads(resp.body) == snap


def test_snapshot_route_is_never_cached():
    # A 2-second poll must always get FRESH truth — a cached snapshot would show a stale field.
    resp = server.route("GET", "/api/snapshot", lambda: {"x": 1}, static_root="/nonexistent")
    assert resp.headers.get("Cache-Control") == "no-store"


def test_snapshot_route_ignores_query_string():
    resp = server.route("GET", "/api/snapshot?t=123", lambda: {"ok": True}, static_root="/nonexistent")
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True}


def test_snapshot_provider_failure_fails_closed_to_500():
    # A provider that raises must not wedge the poll loop — a typed 500, not a crashed connection.
    def boom():
        raise RuntimeError("assembly blew up")
    resp = server.route("GET", "/api/snapshot", boom, static_root="/nonexistent")
    assert resp.status == 500
    assert resp.content_type == "application/json"


# =============================== route: static files ===============================

@pytest.fixture
def static_root(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>cc</title>")
    (tmp_path / "shell.js").write_text("console.log('hi')")
    (tmp_path / "shell.css").write_text("body{}")
    (tmp_path / "secret_outside.txt").write_text("should never be served via ..")
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><h1>shell</h1>")
    (root / "shell.js").write_text("const CC=1;")
    (root / "shell.css").write_text("body{margin:0}")
    return str(root)


def test_root_path_serves_index_html(static_root):
    resp = server.route("GET", "/", lambda: {}, static_root=static_root)
    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert b"<h1>shell</h1>" in resp.body


def test_serves_javascript_with_js_content_type(static_root):
    resp = server.route("GET", "/shell.js", lambda: {}, static_root=static_root)
    assert resp.status == 200
    assert "javascript" in resp.content_type
    assert b"const CC=1;" in resp.body


def test_serves_css_with_css_content_type(static_root):
    resp = server.route("GET", "/shell.css", lambda: {}, static_root=static_root)
    assert resp.status == 200
    assert resp.content_type == "text/css"


def test_missing_static_file_is_404(static_root):
    resp = server.route("GET", "/does-not-exist.js", lambda: {}, static_root=static_root)
    assert resp.status == 404


def test_path_traversal_escape_is_refused(static_root):
    # ../secret_outside.txt sits OUTSIDE the static root — it must never be served.
    resp = server.route("GET", "/../secret_outside.txt", lambda: {}, static_root=static_root)
    assert resp.status == 404
    assert b"should never be served" not in resp.body


def test_encoded_path_traversal_is_refused(static_root):
    resp = server.route("GET", "/%2e%2e/secret_outside.txt", lambda: {}, static_root=static_root)
    assert resp.status == 404
    assert b"should never be served" not in resp.body


def test_non_get_method_is_405(static_root):
    # POST verbs arrive in Task 6; until then a POST is a clean 405, never a stack trace.
    resp = server.route("POST", "/api/snapshot", lambda: {}, static_root=static_root)
    assert resp.status == 405


def test_head_is_routed_like_get(static_root):
    # The handler advertises HEAD; route it like GET (the handler omits the body) so a HEAD probe
    # gets real headers, not a 405.
    resp = server.route("HEAD", "/api/snapshot", lambda: {"ok": 1}, static_root=static_root)
    assert resp.status == 200
    assert resp.content_type == "application/json"


# =============================== build_server: LOCALHOST ONLY (bright line) ===============================

def test_build_server_binds_loopback_only(static_root):
    srv = server.build_server(lambda: {}, static_root, port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_build_server_refuses_a_non_loopback_host(static_root):
    # 0.0.0.0 would expose a label-writing server to the whole LAN — refused at construction.
    with pytest.raises(ValueError):
        server.build_server(lambda: {}, static_root, port=0, host="0.0.0.0")


def test_build_server_refuses_a_public_interface(static_root):
    with pytest.raises(ValueError):
        server.build_server(lambda: {}, static_root, port=0, host="192.168.1.5")


# =============================== end-to-end over a real loopback socket ===============================

# =============================== CachedGh: gh stays on the slow clock ===============================
# The front-end polls every ~2s, but `gh` is rate-limited, so its reads ride a slower clock
# (decision B.2). CachedGh memoizes each (method, args) query for `interval` seconds on an injectable
# clock, so a 2s snapshot loop never hammers GitHub.

class _CountingGh:
    def __init__(self):
        self.calls = 0

    def open_issues(self, repo, label=None, limit=200):
        self.calls += 1
        return [{"number": self.calls}]


def test_cached_gh_serves_one_fetch_within_the_interval():
    clock = [1000.0]
    inner = _CountingGh()
    cached = server.CachedGh(inner, interval=30, clock=lambda: clock[0])
    a = cached.open_issues("r")
    clock[0] = 1020.0                     # 20s later — still inside the 30s window
    b = cached.open_issues("r")
    assert inner.calls == 1               # only one real gh call
    assert a == b


def test_cached_gh_refetches_after_the_interval():
    clock = [1000.0]
    inner = _CountingGh()
    cached = server.CachedGh(inner, interval=30, clock=lambda: clock[0])
    cached.open_issues("r")
    clock[0] = 1031.0                     # past the 30s window
    cached.open_issues("r")
    assert inner.calls == 2


def test_cached_gh_caches_per_query_key():
    # Different args are independent cache slots — a query for one repo can't serve another.
    inner = _CountingGh()
    cached = server.CachedGh(inner, interval=30, clock=lambda: 1000.0)
    cached.open_issues("repo-a")
    cached.open_issues("repo-b")
    assert inner.calls == 2


class _CountingProbeGh:
    def __init__(self):
        self.calls = 0

    def open_issues_probe(self, repo, label=None, limit=200):
        self.calls += 1
        return [{"number": self.calls}], True


def test_cached_gh_caches_open_issues_probe_on_the_slow_clock():
    # The reachability read (issue #38) rides the SAME slow gh clock as every other read (decision
    # B.2) — a 2s snapshot loop must not re-ask GitHub whether it is reachable every tick.
    clock = [1000.0]
    inner = _CountingProbeGh()
    cached = server.CachedGh(inner, interval=30, clock=lambda: clock[0])
    a = cached.open_issues_probe("r")
    clock[0] = 1020.0                     # 20s later — still inside the 30s window
    b = cached.open_issues_probe("r")
    assert inner.calls == 1               # served from cache, one real gh call
    assert a == b == ([{"number": 1}], True)
    clock[0] = 1031.0                     # past the window → a fresh read
    cached.open_issues_probe("r")
    assert inner.calls == 2


def test_server_round_trips_snapshot_and_index_over_loopback(static_root):
    snap = {"pill": {"level": "attention"}, "poll_seconds": 2, "flights": []}
    srv = server.build_server(lambda: snap, static_root, port=0)
    host, port = srv.server_address
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/snapshot")
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read()) == snap

        conn.request("GET", "/")
        r = conn.getresponse()
        assert r.status == 200
        assert b"<h1>shell</h1>" in r.read()
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
