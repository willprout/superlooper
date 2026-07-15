"""Issue #136 — the server's honest answer when it has gone stale (the HTTP contract).

The skew SEMANTICS live in ``lib/version.py`` (unit-tested there). This file defends the two places
the decision reaches the wire, both through the pure ``route()`` with an injected ``version`` object
— no socket, no real checkout:

  * **The snapshot carries the version block**, so the UI can mechanically tell "this page is newer
    than this server" instead of discovering it when a button fails.
  * **An unroutable POST explains the skew instead of ``no such action``.** This is the live 2026-07-14
    failure: the owner's day-old server served the freshly merged RAMP SWEEP button, then answered
    the tap with a bare ``no such action`` beside a Retry that could never succeed.

The two regressions most worth pinning, because both are silent:

  * a skewed server must still route the buttons it DOES have (skew is not an outage — nothing about
    an old build stops ``/api/flag`` from working, and refusing it would turn a notice into a breakage);
  * an UNSKEWED server must still say ``no such action`` for a genuinely unknown path — that is a
    real bug and must not be laundered into a soothing "you're just stale" message.
"""
import json

import pytest

import server
import version as version_mod

REPO = "will-titan/command-center"
LOCAL = "127.0.0.1:8611"
ORIGIN = "http://127.0.0.1:8611"

# Stands in for the live failure's ``/api/janitor/propose``: a path THIS build has no route for, as
# that one had no route on the owner's day-old server. It must stay fictional — naming a real
# endpoint (janitor's included) would silently start testing that surface's 405 the moment the route
# landed, which is exactly how this test would rot into passing for the wrong reason.
UNKNOWN = "/api/a-control-from-a-newer-build"


class _Version:
    """A stand-in for ``lib.version.Version``: the route contract must depend only on the decision,
    never on a real checkout being hashed."""

    def __init__(self, stale):
        self._stale = stale

    def skew(self):
        return self._stale

    def state(self):
        return {"server": "aaaaaaaaaaaaaaaa",
                "server_on_disk": "bbbbbbbbbbbbbbbb" if self._stale else "aaaaaaaaaaaaaaaa",
                "assets": "cccccccccccccccc", "assets_at_boot": "cccccccccccccccc",
                "skew": self._stale,
                "message": version_mod.skew_message() if self._stale else None,
                "remedy": version_mod.REMEDY, "pid": 4242}


class _Actions:
    """Enough of ``lib.actions.Actions`` to prove a known route still dispatches under skew."""

    def __init__(self):
        self.calls = []

    def flag(self, repo, text):
        self.calls.append((repo, text))
        return {"ok": True, "verb": "flag", "num": 7}


def _post(path, version=None, actions=None, body=None):
    payload = json.dumps({"repo": REPO} if body is None else body).encode("utf-8")
    return server.route("POST", path, lambda: {}, "/static", actions=actions, body=payload,
                        origin=ORIGIN, host=LOCAL, version=version)


def _body(resp):
    return json.loads(resp.body.decode("utf-8"))


# =============================== the snapshot exposes the identity ===============================

def test_snapshot_carries_the_version_block(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "server.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "shell.js").write_text("// x\n", encoding="utf-8")
    version = version_mod.Version(tmp_path)
    snap = {"generated_at": 1, "repos": [], "version": version.state()}
    resp = server.route("GET", "/api/snapshot", lambda: snap, "/static")
    got = _body(resp)["version"]
    assert got["skew"] is False
    assert got["server"] and got["assets"], "the UI needs both identities to compare mechanically"


# =============================== the unroutable POST ===============================

def test_unknown_action_on_a_stale_server_explains_the_skew(monkeypatch):
    """The live #121 failure, in one assertion: the tap must not come back as ``no such action``."""
    resp = _post(UNKNOWN, version=_Version(stale=True), actions=_Actions())
    body = _body(resp)
    assert body["ok"] is False
    assert body["error"] != "no such action"
    assert "no such action" not in body["error"]
    assert version_mod.REMEDY in body["error"], "the honest error names the mechanical remedy"


def test_unknown_action_on_a_stale_server_is_flagged_so_the_ui_can_drop_the_retry():
    """A Retry against the same old server can never succeed — the UI needs a mechanical signal, not
    a string to pattern-match."""
    body = _body(_post(UNKNOWN, version=_Version(stale=True), actions=_Actions()))
    assert body["skew"] is True


def test_unknown_action_on_a_stale_server_is_a_conflict_not_a_not_found():
    """409 lets ANY client — the UI, a curl, a future dialog — mechanically separate "this server is
    stale, restart it" from "that route never existed, which is a bug"."""
    assert _post(UNKNOWN, version=_Version(stale=True), actions=_Actions()).status == 409


def test_unknown_action_on_a_FRESH_server_still_says_no_such_action():
    """A genuinely unknown path on an up-to-date server is a real bug. It must NOT be laundered into
    a reassuring skew message — that would trade one lie for another."""
    resp = _post(UNKNOWN, version=_Version(stale=False), actions=_Actions())
    assert resp.status == 404
    assert _body(resp)["error"] == "no such action"


def test_unknown_action_without_a_version_surface_is_unchanged():
    """``version=None`` leaves the surface off (the codebase's injection idiom) — the old 404 stands."""
    resp = _post(UNKNOWN, version=None, actions=_Actions())
    assert resp.status == 404
    assert _body(resp)["error"] == "no such action"


# =============================== skew is a notice, not an outage ===============================

def test_a_stale_server_still_routes_the_buttons_it_does_have():
    """Skew must never break a working button. An old build's own verbs work exactly as they always
    did — the notice is the whole intervention."""
    acts = _Actions()
    resp = server.route("POST", "/api/flag", lambda: {}, "/static", actions=acts,
                        body=json.dumps({"repo": REPO, "text": "a note"}).encode("utf-8"),
                        origin=ORIGIN, host=LOCAL, version=_Version(stale=True))
    assert resp.status == 200
    assert _body(resp)["ok"] is True
    assert acts.calls == [(REPO, "a note")]


def test_cross_origin_is_still_refused_before_any_skew_reasoning():
    """The cross-origin bright line is checked first for every POST — a stale server must not become
    a chattier one. A foreign page learns nothing about this machine's build."""
    resp = server.route("POST", UNKNOWN, lambda: {}, "/static", actions=_Actions(),
                        body=b"{}", origin="http://evil.example", host=LOCAL,
                        version=_Version(stale=True))
    assert resp.status == 403
    assert version_mod.REMEDY not in _body(resp)["error"]


def test_writes_disabled_still_wins_over_the_skew_explanation():
    """``actions=None`` means this embedder wired no gh writes at all — a 405, as before. Skew must
    not manufacture a different answer for a surface that is simply off."""
    resp = _post(UNKNOWN, version=_Version(stale=True), actions=None)
    assert resp.status == 405


# =============================== our own bookkeeping never breaks a button ===============================

class _BrokenVersion:
    """A stamp that blows up — a corrupt tree, a permissions change, a bug in our own walk."""

    def skew(self):
        raise RuntimeError("the stamp exploded")

    def state(self):
        raise RuntimeError("the stamp exploded")


def test_a_broken_stamp_degrades_to_the_plain_404_never_a_500():
    """This feature is bookkeeping ABOUT the dashboard. If it fails, the honest fallback is the
    behavior we had before it existed — never a 500 that turns an informational miss into an outage."""
    resp = _post(UNKNOWN, version=_BrokenVersion(), actions=_Actions())
    assert resp.status == 404
    assert _body(resp)["error"] == "no such action"


def test_a_broken_stamp_never_fails_the_whole_snapshot(tmp_path):
    """The field is the truth the owner opened the dashboard for. A stamp that can't be taken must
    cost the version block and nothing else — the poll loop must not wedge over it."""
    (tmp_path / "lib").mkdir()
    snap = server.assemble_snapshot(
        {"repos": [], "fun": {}, "operator": "William", "poll_seconds": 2},
        now=1_000_000, version=_BrokenVersion())
    assert "version" not in snap, "an unavailable stamp is omitted, not faked"
    assert snap["generated_at"] == 1_000_000, "…and the rest of the snapshot is untouched"
