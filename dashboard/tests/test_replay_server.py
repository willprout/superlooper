"""Server wiring for the Task-11 on-demand endpoints (design record §4).

The replay + digest are computed behind a button, not on the 2-second poll: this pins the assembler
(repo + window selection over a fresh journal) and the pure GET routing (JSON, no-store, off ⇒ 404,
a provider bug ⇒ a typed 500 that never wedges the caller).
"""
import json
import os

import server


HOME = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
NOW = 1783364266 + 10   # just after the fixture's newest record (the i23 merge)


def _config():
    return {"repos": [{"slug": "will-titan/command-center", "name": "command-center",
                       "state_home": HOME}]}


# --------------------------- assemblers ---------------------------

def test_assemble_replay_reads_the_fixture_journal():
    rp = server.assemble_replay(_config(), {"range": "all"}, now=NOW)
    assert not rp["empty"]
    assert rp["slug"] == "will-titan/command-center"
    last = rp["frames"][-1]
    by_num = {f["num"]: f for f in last["flights"]}
    assert by_num[23]["stage"] == "touchdown"


def test_assemble_digest_reads_the_fixture_journal():
    d = server.assemble_digest(_config(), {"range": "all"}, now=NOW)
    assert not d["empty"]
    assert d["counts"]["landings"] == 1
    assert d["counts"]["parks"] == 1


def test_window_range_restricts_to_recent_records():
    # A tight 5-minute window around the newest block excludes the ~2-day-older i16/i7 records.
    d = server.assemble_digest(_config(), {"range": "300"}, now=NOW)
    nums = {e["num"] for e in d["events"]}
    assert 23 in nums
    assert 7 not in nums and 16 not in nums


def test_default_window_is_a_day():
    # No range param ⇒ the last 86400s; the fixture's newest events land inside it.
    d = server.assemble_digest(_config(), {}, now=NOW)
    assert not d["empty"]


def test_no_repo_configured_is_a_typed_empty():
    rp = server.assemble_replay({"repos": []}, {}, now=NOW)
    assert rp["empty"] is True
    assert rp["error"] == "no repo configured"
    d = server.assemble_digest({"repos": []}, {}, now=NOW)
    assert d["empty"] is True and d["events"] == []


def test_unknown_slug_falls_back_to_first_repo():
    rp = server.assemble_replay(_config(), {"repo": "who/what"}, now=NOW)
    assert rp["slug"] == "will-titan/command-center"


# --------------------------- pure routing ---------------------------

def _provider_ok(kind):
    def prov(params):
        return {"kind": kind, "params": params}
    return prov


def test_get_replay_routes_to_provider_with_query_params():
    resp = server.route("GET", "/api/replay?repo=o%2Fr&range=3600", lambda: {}, HOME,
                        replay_provider=_provider_ok("replay"))
    assert resp.status == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    body = json.loads(resp.body)
    assert body["kind"] == "replay"
    assert body["params"] == {"repo": "o/r", "range": "3600"}


def test_get_digest_routes_to_provider():
    resp = server.route("GET", "/api/digest?range=all", lambda: {}, HOME,
                        digest_provider=_provider_ok("digest"))
    assert resp.status == 200
    assert json.loads(resp.body)["params"] == {"range": "all"}


def test_replay_off_is_404():
    resp = server.route("GET", "/api/replay", lambda: {}, HOME)   # no provider wired
    assert resp.status == 404
    assert json.loads(resp.body)["error"] == "not found"


def test_provider_error_is_typed_500_not_a_crash():
    def boom(params):
        raise RuntimeError("journal exploded")
    resp = server.route("GET", "/api/digest", lambda: {}, HOME, digest_provider=boom)
    assert resp.status == 500
    assert json.loads(resp.body)["error"] == "unavailable"


def test_build_server_accepts_the_providers_and_stays_loopback():
    # Construction wires the providers without error and preserves the localhost bright line
    # (the provider→route path itself is covered by the pure route tests above).
    cfg = _config()
    srv = server.build_server(lambda: {}, os.path.join(os.path.dirname(__file__), "..", "static"),
                              port=0,
                              replay_provider=lambda p: server.assemble_replay(cfg, p, now=NOW),
                              digest_provider=lambda p: server.assemble_digest(cfg, p, now=NOW))
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()
