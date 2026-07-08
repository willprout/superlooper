"""Task 12 — end-to-end smoke: a stranger's clone → configure → run actually serves the dashboard.

Every other test pins one layer. This one wires the WHOLE install path a stranger walks — a real
``config.json`` on disk → ``config.load`` (which reads each repo's own ``.superlooper/config.json``
for its slug and derives the state home under ``$SL_HOME``) → ``assemble_snapshot`` over a real
fixture state home → the REAL ``bin/command-center`` static bundle served over a real loopback
socket → an HTTP round-trip. Nothing in the render path is mocked; ``gh`` is simply absent (the
honest offline poll), so titles/queue are empty but every locally-derived semantic is real.

It is the automated twin of the screenshot evidence: the screenshot proves it LOOKS right, this
proves the assembled JSON + static bundle actually SERVE from a config file. It round-trips through
``http.client`` (a loopback connection to our own in-process server is not egress; conftest blocks
``urllib`` but not this).
"""
import json
import shutil
import threading
from http import client as http_client
from pathlib import Path

import pytest

import config as config_mod
import server

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "statehome"
_STATIC = _ROOT / "static"
SLUG = "will-titan/superlooper-sandbox"
NOW = 1783364300   # just after the fixture's i23 merge — the field is populated and real-shaped


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A stranger's install laid out on disk: an $SL_HOME with the fixture state home, an adopted
    repo checkout that declares the slug, and the dashboard config.json that points at it."""
    base = tmp_path / "sl-home"
    shutil.copytree(_FIXTURE, base / "will-titan__superlooper-sandbox")
    monkeypatch.setenv("SL_HOME", str(base))

    repo_checkout = tmp_path / "code" / "superlooper-sandbox"
    (repo_checkout / ".superlooper").mkdir(parents=True)
    (repo_checkout / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": SLUG, "required_checks": ["tests"]}))

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"repos": [{"path": str(repo_checkout)}]}))
    return config_file


def _serve(config_file):
    cfg = config_mod.load(str(config_file))
    provider = lambda: server.assemble_snapshot(cfg, now=NOW)
    srv = server.build_server(provider, str(_STATIC), port=0)
    return srv


def test_configured_dashboard_serves_a_real_snapshot_and_the_static_shell(installed):
    srv = _serve(installed)
    host, port = srv.server_address
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http_client.HTTPConnection(host, port, timeout=5)

        # /api/snapshot — the assembled truth document, from the fixture, through the real loader.
        conn.request("GET", "/api/snapshot")
        r = conn.getresponse()
        assert r.status == 200
        snap = json.loads(r.read())
        # Real-shaped: every issue in the fixture became a flight; the panels the shell binds exist.
        assert {f["num"] for f in snap["flights"]} == {23, 16, 15, 7, 21}
        repo = snap["repos"][0]
        assert repo["slug"] == SLUG
        assert set(repo["boards"]) == {"departures", "arrivals"}
        assert repo["tower_log"], "tower log should carry the fixture's journal comms"
        # The arrivals board shows the landed flights, newest first (i23 merged last).
        assert repo["boards"]["arrivals"][0]["num"] == 23

        # / — the REAL front-end shell that binds that snapshot (proves the static bundle is served).
        conn.request("GET", "/")
        r = conn.getresponse()
        assert r.status == 200
        body = r.read()
        assert r.getheader("Content-Type") == "text/html"
        assert b'id="root"' in body and b"/shell.js" in body

        # a static asset resolves with the right content type — the whole bundle, not just index.
        conn.request("GET", "/shell.js")
        r = conn.getresponse()
        assert r.status == 200
        assert "javascript" in r.getheader("Content-Type")
        r.read()
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
