"""The command center's localhost server (Task 5 / decision B.3) — snapshot API + static shell.

The dashboard is a *renderer with buttons*: this module assembles the honest snapshot the pure
truth layer produces (``lib/readers`` → ``lib/flights`` fed by ``lib/gh`` + ``lib/pollers``) into
one JSON document, and serves it plus the static front-end over a **loopback-only** socket.

Two constraints are load-bearing bright lines, not conveniences:

* **Localhost only.** ``build_server`` binds ``127.0.0.1`` and refuses any other host at
  construction — the server can write GitHub labels (William's word), so it must never be reachable
  off the machine (decision B.3; a bright line in the loop contract).
* **A pure router.** ``route()`` is a pure function of (method, path, snapshot-provider,
  static-root) — no socket, no globals — so the whole HTTP contract is unit-tested with an injected
  snapshot. Static serving is path-traversal safe: a resolved path that escapes the static root is
  a 404, never a file leak.

The SEMANTICS in the snapshot all come from the tested pure ``lib/`` functions (design record B.1);
this module only *composes* them and adds the few honest presentational numerals the boring-mode
table sorts by (durations), each computed here from real timestamps and unit-tested.
"""
import json
import math
import os
import posixpath
import sys
import threading
import time
import urllib.parse
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import actions as actions_mod
import cards as cards_mod
import config as config_mod
import digest as digest_mod
import engine as engine_mod
import flights
import launch_rules
import notify as notify_mod
import pollers
import readers
import review_marker
import runner_source
import replay as replay_mod
import tower as tower_mod
import truth
import version as version_mod

# The one address this server may ever bind. It writes labels; off-machine reachability is a
# bright line, so the host is validated against exactly this (decision B.3 / loop contract).
BIND_HOST = "127.0.0.1"

# A socket-free response: the router returns one of these, the handler writes it. Pure in, pure out.
Response = namedtuple("Response", ["status", "content_type", "body", "headers"])


def _resp(status, content_type, body, headers=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return Response(status, content_type, body, headers or {})


# =============================== duration numerals (boring-mode §4) ===============================

def _finite(v):
    """``True`` only for a real, finite number (never a bool, never NaN/Infinity). ``json.loads``
    accepts ``NaN``/``Infinity``, so a corrupt journal ts can be a non-finite float — every ts/age
    the server does arithmetic on is screened through this so one bad line can't crash a snapshot."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def format_duration(seconds):
    """A compact human duration string for a numeral channel (``"41m"``, ``"26h"``), or ``"—"`` when
    unknown (``None``) or unusable (a non-finite ts, e.g. a corrupt JSON ``NaN`` — which must degrade
    to "—", never raise a ValueError that would fail-close the whole snapshot to a 500). Floored to
    the coarsest whole unit — a duration is never rounded UP to look busier than it is (design record
    §5/§7 honesty). The boring table pairs this string with the raw seconds it sorts by, so the art
    stays flavor and the number stays truth (§4)."""
    if not _finite(seconds):
        return "—"
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh" % (s // 3600)   # hours never roll into days — the design shows "26h", never "1d 2h"


# =============================== the pure router ===============================

_CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json",
}


def _content_type(path):
    return _CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _serve_static(rel_path, static_root):
    """Serve ``rel_path`` from under ``static_root``, or a 404. Path-traversal safe: the requested
    path is normalized and resolved, and anything landing OUTSIDE the real static root (a ``..``
    escape, an absolute path, a symlink out) is a 404 — never a file leak. An empty path is the
    shell's ``index.html``."""
    rel = urllib.parse.unquote(rel_path).lstrip("/")
    # Normalize with POSIX semantics and strip any leading traversal so an absolute/escape can't
    # survive the join. We still verify containment after realpath — belt and suspenders.
    rel = posixpath.normpath(rel) if rel else "index.html"
    if rel in (".", ""):
        rel = "index.html"
    root_real = os.path.realpath(static_root)
    candidate = os.path.realpath(os.path.join(root_real, rel))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return _resp(404, "text/plain", "not found")
    if not os.path.isfile(candidate):
        return _resp(404, "text/plain", "not found")
    try:
        with open(candidate, "rb") as fh:
            body = fh.read()
    except OSError:
        return _resp(404, "text/plain", "not found")
    return _resp(200, _content_type(candidate), body)


# =============================== the verbs (POST) — Task 6 ===============================
# The ONLY writes in the product. Each endpoint dispatches to a tested pure action (lib/actions.py);
# this layer is just the HTTP contract — parse the body, enforce the CSRF/loopback bright line, map
# to a Response. No gh, no semantics here.

_MAX_BODY = 64 * 1024                    # a flag body is short; cap the read so a POST can't balloon RAM
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# The label/close write verbs keyed on (repo, num) → the Actions method name.
_LABEL_VERBS = {
    "/api/approve": "approve",           # also serves re-approve (same mechanical effect + comment)
    "/api/drop": "drop",
    "/api/expedite": "expedite",
    "/api/bounce-yes": "bounce_yes",
    "/api/rebuild": "rebuild",           # issue #161: the explicit rebuild-from-scratch verb
}
_ACTION_PATHS = set(_LABEL_VERBS) | {"/api/flag", "/api/discuss", "/api/answer"}

# The Tidy endpoints (issue #41) — the dashboard's SECOND button class: a LOCAL COMMAND execution
# (``superlooper tidy``), not a GitHub write. Two steps, two paths: dry-run lists what WOULD close
# (the confirm dialog shows exactly this), execute closes it on the in-UI confirm. Backed by a
# ``lib.tidy.Tidy``, kept separate from ``actions`` because it drives a different egress entirely.
_TIDY_PATHS = {"/api/tidy/dry-run": "dry_run", "/api/tidy": "execute"}
# The Restart endpoints (issue #116) — the dashboard's second local-command verb, a sibling of Tidy:
# ``/api/restart/check`` is the confirm dialog's preflight (is a live runner there to ask?);
# ``/api/restart`` drops the request the runner honors by re-exec'ing itself in its own cmux tab.
_RESTART_PATHS = {"/api/restart/check": "preflight", "/api/restart": "execute"}

# The Janitor endpoints (issue #121) — the dashboard's THIRD button in the local-command class:
# the GitHub-side debris sweep (``superlooper janitor``). Two steps: propose lists what the sweep
# WOULD do (grouped by kind, the dialog shows exactly this), execute runs EXACTLY the keys the owner
# tapped. Backed by a ``lib.janitor.Janitor`` — like Tidy, it drives the CLI so the janitor's whole
# safety contract stays the single source of truth (the dashboard re-derives none of it).
_JANITOR_PATHS = {"/api/janitor/propose": "propose", "/api/janitor": "execute"}
# ``/api/fixer/check`` is the note box's preflight (is a fixer already live? what trouble will ride
# into the prompt?); ``/api/fixer`` composes the prompt and launches ONE interactive sl-debugger
# session through the engine's shim (issue #141). Both are POST-only and same-origin gated: this is
# the most consequential endpoint in the product — it starts an agent on the owner's machine.
_FIXER_PATHS = {"/api/fixer/check": "preflight", "/api/fixer": "execute"}


def _is_allowed_origin(origin, host):
    """True when a POST's ``Origin`` is our own page — or absent (a non-browser caller like curl or a
    test; browsers always send Origin on a POST). The server binds 127.0.0.1, but that alone would
    NOT stop a page in the same browser from POSTing to it, so this is the CSRF half of the
    label-writer bright line. Two checks, both required when present:

    * the Origin's host must be a loopback name — defeats a DNS-rebinding page (``Origin: evil.com``)
      even if it spoofs a matching ``Host``;
    * the Origin's ``host:port`` must equal the ``Host`` header the request targeted — so a DIFFERENT
      loopback origin (``http://localhost:3000``) can't drive our writes with a simple cross-origin
      POST whose response the browser merely hides.

    ``null`` (a sandboxed/opaque origin) has no loopback host and is refused. When ``host`` is unknown
    (no ``Host`` header, e.g. HTTP/1.0) we fall back to the loopback-name check alone."""
    if not origin:
        return True
    try:
        parts = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    if parts.hostname not in _LOOPBACK_HOSTS:
        return False
    return (not host) or (parts.netloc == host)


def _json_resp(status, obj):
    return _resp(status, "application/json", json.dumps(obj), {"Cache-Control": "no-store"})


def _num_of(payload):
    """The issue number from a POST body — a POSITIVE int, or a digit-string coerced to one (a JSON
    client may send either). ``None`` for anything else (a bool, a float, ``"abc"``, or a
    non-positive value like ``0``/``-5``) → the caller 400s, so invalid input never reaches the
    label writer."""
    n = payload.get("num")
    if isinstance(n, bool):
        return None
    if isinstance(n, str) and n.strip().lstrip("-").isdigit():
        n = int(n)
    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
        return n
    return None


def _parse_json_body(body_bytes):
    """Parse a POST body to a dict, or return ``(None, error_response)``. Shared by the verbs and
    the tower-seen endpoint so both fail closed identically on malformed input."""
    try:
        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except (ValueError, UnicodeDecodeError):
        return None, _json_resp(400, {"ok": False, "error": "malformed JSON body"})
    if not isinstance(payload, dict):
        return None, _json_resp(400, {"ok": False, "error": "body must be a JSON object"})
    return payload, None


def _route_tower_seen(body_bytes, desk):
    """``POST /api/tower-seen`` — advance the persisted "since you last looked" watermark (§4). A
    DASHBOARD-LOCAL write only: it writes the dashboard's own tiny state file, never GitHub, so it
    is NOT one of the six mechanical verbs and needs no ``Actions`` — but it IS still same-origin
    gated (checked by the caller) so a foreign page can't scribble the watermark. ``desk=None``
    (a read-only embedder) → 405."""
    if desk is None:
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    ts = payload.get("ts")
    if not _finite(ts):
        return _json_resp(400, {"ok": False, "error": "missing or bad 'ts'"})
    desk.mark_tower_seen(ts)
    return _json_resp(200, {"ok": True, "verb": "tower-seen", "tower_last_seen": desk.tower_last_seen()})


def _route_tidy(clean, body_bytes, tidy):
    """The Tidy endpoints (issue #41) — a LOCAL COMMAND execution, the dashboard's second button
    class. Same-origin is already enforced by the caller (a foreign page must not be able to run a
    local command any more than it could drive the label writer). ``tidy=None`` (a read-only
    embedder, or writes disabled) → 405. Dispatches to the tested pure ``lib.tidy.Tidy``: dry-run
    returns the window list the confirm dialog shows, execute closes them on the in-UI confirm. A
    command failure is the action's own honest ``ok: false`` body at 200 — the request itself was
    fine — never an HTTP error and never a silent success."""
    if tidy is None:
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    repo = payload.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _json_resp(400, {"ok": False, "error": "missing 'repo'"})
    return _json_resp(200, getattr(tidy, _TIDY_PATHS[clean])(repo))


def _route_restart(clean, body_bytes, restart):
    """The Restart endpoints (issue #116) — a local-command verb. Same-origin is already enforced by
    the caller (a foreign page must not be able to ask the runner to restart any more than it could
    drive the label writer). ``restart=None`` (a read-only embedder, or writes disabled) → 405.
    Dispatches to the tested pure ``lib.restart.Restart``: check reports whether a live runner exists
    (the confirm dialog's preflight), execute drops the request on the in-UI confirm. A dead-runner
    refusal is the verb's own honest ``running: false`` body at 200 — the request itself was fine —
    never an HTTP error and never a launch/placement attempt."""
    if restart is None:
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    repo = payload.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _json_resp(400, {"ok": False, "error": "missing 'repo'"})
    return _json_resp(200, getattr(restart, _RESTART_PATHS[clean])(repo))


def _route_janitor(clean, body_bytes, janitor):
    """The Janitor endpoints (issue #121) — the GitHub-side debris sweep, a LOCAL COMMAND button
    (same class as Tidy). Same-origin is already enforced by the caller (a foreign page must not be
    able to sweep GitHub any more than it could drive the label writer). ``janitor=None`` (a
    read-only embedder, or writes disabled) → 405. Dispatches to the tested ``lib.janitor.Janitor``:
    ``propose`` returns the proposals grouped by kind (the dialog shows exactly this), ``execute``
    runs EXACTLY the ``keys`` the owner tapped. A command failure is the action's own honest ``ok:
    false`` body at 200 — never an HTTP error, never a silent success."""
    if janitor is None:
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    repo = payload.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _json_resp(400, {"ok": False, "error": "missing 'repo'"})
    if clean == "/api/janitor/propose":
        return _json_resp(200, janitor.propose(repo))
    # execute: the tapped subset. `keys` must be a list (the CLI/adapter then refuses an empty or
    # garbage selection) — a body missing it, or sending a non-list, is a bad request. Nothing ever
    # sweeps that the owner did not tap.
    keys = payload.get("keys")
    if not isinstance(keys, list):
        return _json_resp(400, {"ok": False, "error": "missing or bad 'keys'"})
    return _json_resp(200, janitor.execute(repo, keys))


def _unroutable(clean, version):
    """The answer to a POST this router has no route for — the dashboard's own publish drift (issue
    #136).

    ``no such action`` is technically true and practically a lie. On 2026-07-14 the owner's day-old
    server served the freshly merged RAMP SWEEP button (static assets are read from disk every
    request) and answered the tap with exactly that string, beside a Retry that could never succeed:
    it read as "this button is broken" when the truth was "this button is newer than me".

    So when the server KNOWS it is stale, say so and name the remedy — a **409**, not a 404, because
    the two cases need different reactions and every client should be able to tell them apart
    mechanically: 409 ⇒ this server is behind the disk, restart it; 404 ⇒ this route never existed,
    which is a real bug. An unstale server (or one with no ``version`` surface wired) keeps the exact
    old 404 — laundering a genuine bug into a reassuring "you're just stale" would trade one lie for
    another.

    Never raises: the stamp walks the filesystem, and no failure to take it may become a 500 on the
    button path. A bug in our own bookkeeping must degrade to the plain old 404 — the same
    fail-toward-the-old-behavior posture the snapshot route takes with its provider.
    """
    try:
        stale = version is not None and version.skew()
    except Exception:                    # a stamp bug must never break a button that would 404 anyway
        stale = False
    if not stale:
        return _json_resp(404, {"ok": False, "error": "no such action"})
    return _json_resp(409, {"ok": False, "skew": True,
                            "error": version_mod.stale_action_message(clean)})


def _route_fixer(clean, body_bytes, fixer, snapshot_provider):
    """The Deploy Fixer endpoints (issue #141) — a LOCAL SESSION LAUNCH, the same button class as
    Tidy/Restart/Janitor. Same-origin is already enforced by the caller (a foreign page must not be
    able to start an AI session on this machine any more than it could drive the label writer).
    ``fixer=None`` (a read-only embedder, or writes disabled) → 405.

    The CONTEXT is read here, from the server's own snapshot provider at TAP TIME — never taken from
    the request body. That is what makes the composed prompt honest: it describes the board the owner
    was actually looking at, and a client that could name the trouble could lie about it. A provider
    that raises fails closed to a refusal (no context ⇒ no launch), never a 500 and never a launch
    with a blank picture.

    Dispatches to the tested pure ``lib.fixer.Fixer``: ``preflight`` reports whether a fixer is
    already live (writes nothing), ``execute`` composes the brief and launches on the owner's tap. A
    live-fixer refusal or a failed launch is the verb's own honest ``ok: false`` body at 200 — the
    request itself was fine — never an HTTP error, never a silent success."""
    if fixer is None:
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    repo = payload.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _json_resp(400, {"ok": False, "error": "missing 'repo'"})
    try:
        snapshot = snapshot_provider()
    except Exception:
        return _json_resp(200, {"ok": False, "verb": "fixer",
                                "error": "the dashboard can't read this loop's state right now — "
                                         "nothing launched"})
    if clean == "/api/fixer/check":
        return _json_resp(200, fixer.preflight(repo, snapshot))
    # execute: the note is OPTIONAL (the box is skippable — DoD), so an absent note is an empty
    # one, never a bad request. A non-string note IS refused: the client is broken, and coercing it
    # would put junk in the session's opening prompt.
    note = payload.get("note", "")
    if not isinstance(note, str):
        return _json_resp(400, {"ok": False, "error": "'note' must be a string"})
    return _json_resp(200, fixer.execute(repo, note, snapshot))


def _route_post(clean, body_bytes, origin, host, actions, snapshot_provider, desk=None, tidy=None,
                restart=None, janitor=None, version=None, fixer=None):
    """The pure POST router. Order is deliberate: cross-origin → 403 (before any parsing, for every
    POST); the Tidy local-command endpoints (need only ``tidy``); the dashboard-local tower-seen
    write (needs only ``desk``); then the gh verbs — writes-disabled → 405; unknown action path →
    404 (or, on a known-stale server, a 409 that explains the skew — issue #136); body validation →
    400; then dispatch → 200 with the action's honest ``{ok, …}`` result (a gh/command failure is a
    truthful ``ok: false``, not an HTTP error — the request itself was fine)."""
    if not _is_allowed_origin(origin, host):
        return _json_resp(403, {"ok": False, "error": "cross-origin write refused"})
    if clean in _TIDY_PATHS:
        return _route_tidy(clean, body_bytes, tidy)
    if clean in _RESTART_PATHS:
        return _route_restart(clean, body_bytes, restart)
    if clean in _JANITOR_PATHS:
        return _route_janitor(clean, body_bytes, janitor)
    if clean in _FIXER_PATHS:
        return _route_fixer(clean, body_bytes, fixer, snapshot_provider)
    if clean == "/api/tower-seen":
        return _route_tower_seen(body_bytes, desk)
    if actions is None:                  # gh writes not wired → method not allowed (Task 5 contract)
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})
    if clean not in _ACTION_PATHS:
        return _unroutable(clean, version)

    payload, err = _parse_json_body(body_bytes)
    if err is not None:
        return err
    repo = payload.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _json_resp(400, {"ok": False, "error": "missing 'repo'"})

    if clean == "/api/flag":
        text = payload.get("text")
        if not isinstance(text, str):
            return _json_resp(400, {"ok": False, "error": "missing 'text'"})
        return _json_resp(200, actions.flag(repo, text))

    if clean == "/api/answer":           # #163: post the owner's typed answer + re-approve
        text = payload.get("text")
        if not isinstance(text, str):
            return _json_resp(400, {"ok": False, "error": "missing 'text'"})
        num = _num_of(payload)
        if num is None:
            return _json_resp(400, {"ok": False, "error": "missing or bad 'num'"})
        return _json_resp(200, actions.answer(repo, text, num))

    if clean == "/api/discuss":
        num = _num_of(payload)
        if num is None:
            return _json_resp(400, {"ok": False, "error": "missing or bad 'num'"})
        try:
            snap = snapshot_provider()
        except Exception:                # a provider bug must not turn a compose into a stack trace
            return _json_resp(500, {"ok": False, "error": "snapshot unavailable"})
        return _json_resp(200, {"ok": True, "verb": "discuss",
                                "text": actions_mod.compose_briefing(snap, repo, num)})

    num = _num_of(payload)               # the four (repo, num) label/close verbs
    if num is None:
        return _json_resp(400, {"ok": False, "error": "missing or bad 'num'"})
    return _json_resp(200, getattr(actions, _LABEL_VERBS[clean])(repo, num))


def _query_params(path):
    """The query string of ``path`` as a flat ``{str: str}`` (first value per key) — what the
    on-demand replay/digest providers read (``repo``, ``range``/``start``/``end``)."""
    q = path.split("?", 1)[1] if "?" in path else ""
    parsed = urllib.parse.parse_qs(q, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _provider_get(provider, params):
    """A GET backed by an on-demand data provider (Task 11 replay/digest). Not wired ⇒ a clean 404
    (the feature is off for this embedder — e.g. a read-only test harness); a provider bug ⇒ a typed
    500 that never wedges the caller. Always no-store — a fresh journal read every request."""
    if provider is None:
        return _resp(404, "application/json", json.dumps({"error": "not found"}),
                     {"Cache-Control": "no-store"})
    try:
        body = json.dumps(provider(params)).encode("utf-8")
    except Exception as e:   # an on-demand compose bug must not become an unhandled stack trace
        return _resp(500, "application/json",
                     json.dumps({"error": "unavailable", "detail": str(e)}),
                     {"Cache-Control": "no-store"})
    return _resp(200, "application/json", body, {"Cache-Control": "no-store"})


def route(method, path, snapshot_provider, static_root, *, actions=None, body=b"", origin=None,
          host=None, desk=None, tidy=None, restart=None, janitor=None, replay_provider=None,
          digest_provider=None, version=None, fixer=None):
    """Map one request to a :class:`Response`, with no socket in sight (unit-testable with an
    injected ``snapshot_provider`` and ``actions``). ``POST`` drives the six mechanical verbs
    (Task 6) plus the dashboard-local tower-seen write (Task 9) via :func:`_route_post`;
    ``origin``/``host`` are the request's ``Origin``/``Host`` headers, checked for same-origin before
    any write, and ``desk`` (a ``lib.desk.Desk``) backs the tower-seen watermark. ``GET
    /api/snapshot`` returns the provider's dict as never-cached JSON (a 2-second poll must always see
    fresh truth); a provider that raises fails closed to a typed 500 so the client's poll loop
    survives. Every other ``GET`` serves a static file (traversal-safe). ``HEAD`` routes like ``GET``
    (the handler omits the body); any other method is a clean 405. ``tidy`` (a ``lib.tidy.Tidy``)
    backs the Tidy local-command endpoints (issue #41) and ``janitor`` (a ``lib.janitor.Janitor``)
    the Janitor GitHub-sweep endpoints (issue #121); ``None`` leaves that surface off (405).
    ``version`` (a ``lib.version.Version``) is the server's own code identity, captured at boot: it
    turns a bare ``no such action`` into an honest "this server is older than that button" when the
    checkout has moved on under a running process (issue #136); ``None`` keeps the plain old 404.
    ``fixer`` (a ``lib.fixer.Fixer``) backs the Deploy Fixer endpoints (issue #141), which compose a
    prompt from the CURRENT snapshot and launch one interactive sl-debugger session."""
    clean = path.split("?", 1)[0]
    if method == "POST":
        return _route_post(clean, body, origin, host, actions, snapshot_provider, desk, tidy,
                           restart, janitor, version, fixer)
    if method not in ("GET", "HEAD"):
        return _resp(405, "text/plain", "method not allowed", {"Allow": "GET, HEAD"})

    if clean == "/api/snapshot":
        try:
            body = json.dumps(snapshot_provider()).encode("utf-8")
        except Exception as e:  # a provider bug must not wedge the poll loop
            return _resp(500, "application/json",
                         json.dumps({"error": "snapshot unavailable", "detail": str(e)}),
                         {"Cache-Control": "no-store"})
        return _resp(200, "application/json", body, {"Cache-Control": "no-store"})

    # The on-demand treat + digest (Task 11): computed only when a button asks, never on the poll.
    if clean == "/api/replay":
        return _provider_get(replay_provider, _query_params(path))
    if clean == "/api/digest":
        return _provider_get(digest_provider, _query_params(path))

    return _serve_static(clean, static_root)


# =============================== the server (loopback only) ===============================

def make_handler(snapshot_provider, static_root, actions=None, desk=None, tidy=None, restart=None,
                 janitor=None, replay_provider=None, digest_provider=None, version=None,
                 fixer=None):
    """A ``BaseHTTPRequestHandler`` subclass that delegates to :func:`route`. Kept thin: the socket
    machinery lives here (reading the POST body + Origin), every decision lives in the pure router
    above. ``actions`` (an ``lib.actions.Actions``) enables the POST verbs; ``desk`` (a
    ``lib.desk.Desk``) enables the dashboard-local tower-seen write; ``tidy`` (a ``lib.tidy.Tidy``)
    enables the Tidy local-command endpoints (issue #41); ``restart`` (a ``lib.restart.Restart``)
    enables the Restart endpoints (issue #116); ``janitor`` (a ``lib.janitor.Janitor``) enables the
    Janitor GitHub-sweep endpoints (issue #121); ``replay_provider`` / ``digest_provider``
    (``fn(params)->dict``) enable the on-demand Task-11 GETs. ``None`` for any of them leaves that
    surface off (POST → 405; replay/digest GET → 404)."""

    class _Handler(BaseHTTPRequestHandler):
        # Quiet by default — a 2-second poll would otherwise spam stderr with one line per request.
        def log_message(self, *args):
            return

        def _write(self, resp):
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            length = max(0, min(length, _MAX_BODY))   # bounded so a POST can't exhaust memory
            return self.rfile.read(length) if length else b""

        def _get(self):
            self._write(route(self.command, self.path, snapshot_provider, static_root,
                              replay_provider=replay_provider, digest_provider=digest_provider))

        def do_GET(self):
            self._get()

        def do_HEAD(self):
            self._get()

        def do_POST(self):
            self._write(route(self.command, self.path, snapshot_provider, static_root,
                              actions=actions, body=self._read_body(),
                              origin=self.headers.get("Origin"), host=self.headers.get("Host"),
                              desk=desk, tidy=tidy, restart=restart, janitor=janitor,
                              version=version, fixer=fixer))

    return _Handler


def build_server(snapshot_provider, static_root, port=8611, host=BIND_HOST, actions=None, desk=None,
                 tidy=None, restart=None, janitor=None, replay_provider=None, digest_provider=None,
                 version=None, fixer=None):
    """Construct (do NOT start) the loopback HTTP server. Refuses any non-loopback ``host`` with a
    ``ValueError`` — binding ``0.0.0.0`` or a LAN interface would expose a label-writing (and now
    local-command-running, issue #41) server off the machine, a bright line (decision B.3).
    ``actions`` wires the POST verbs (Task 6); ``desk`` wires the tower-seen watermark write (Task
    9); ``tidy`` wires the Tidy local-command endpoints (issue #41); ``restart`` wires the Restart
    local-command endpoints (issue #116); ``janitor`` wires the Janitor GitHub-sweep endpoints (issue
    #121); ``fixer`` wires the Deploy Fixer session-launch endpoints (issue #141); ``replay_provider``
    / ``digest_provider`` wire the on-demand replay + digest GETs (Task 11); ``version`` (a
    ``lib.version.Version``) wires the boot-identity/skew honesty (issue #136); omit any for a
    surface that stays off. ``port=0`` binds an ephemeral port (tests). Call ``.serve_forever()`` to
    run it."""
    if host != BIND_HOST:
        raise ValueError(
            "command center binds %s only (refusing %r) — it can write GitHub labels, so it must "
            "never be reachable off the machine" % (BIND_HOST, host))
    return ThreadingHTTPServer((host, port),
                               make_handler(snapshot_provider, static_root, actions, desk, tidy,
                                            restart, janitor, replay_provider, digest_provider,
                                            version, fixer))


# =============================== CachedGh — the gh slow clock (decision B.2) ===============================

class CachedGh:
    """A caching proxy over the ``gh`` adapter (or any object with the same surface) that memoizes
    each distinct (method, args) query for ``interval`` seconds on an injectable ``clock``. The
    front-end polls every ~2s, but GitHub is rate-limited, so its reads must ride a slower clock
    (decision B.2). The wrapper is transparent — the assembler calls ``gh_mod.open_issues(...)`` and
    never knows a fetch was served from cache. Only READS are wrapped; writes (Task 6) must never
    be cached and go straight to the adapter."""

    _READS = ("open_issues", "open_issues_probe", "issue", "pr_for_branch", "pr_comments")

    def __init__(self, gh_mod, interval, clock=None):
        self._gh = gh_mod
        self._interval = interval
        self._clock = clock if clock is not None else time.time
        self._caches = {}

    def _cached(self, key, fetch):
        c = self._caches.get(key)
        if c is None:
            c = pollers.Cached(fetch, self._interval, self._clock)
            self._caches[key] = c
        return c.get()

    def open_issues_probe(self, repo, label=None, limit=200):
        # The reachability read (issue #38) rides the SAME slow clock as every other read (decision
        # B.2) — a 2s poll must not re-ask GitHub whether it is reachable every tick. The (list,
        # reachable) tuple is memoized whole, so surface and reachability always come from one fetch.
        return self._cached(("open_issues_probe", repo, label, limit),
                            lambda: self._gh.open_issues_probe(repo, label=label, limit=limit))

    def open_issues(self, repo, label=None, limit=200):
        # The list-only surface OVER open_issues_probe — mirrors lib/gh.open_issues so both share the
        # ONE probe cache entry per query (a caller asking for the list and the probe of the same
        # query never double-fetches gh, and the list can never drift from its reachability signal —
        # Codex review, issue #38).
        return self.open_issues_probe(repo, label=label, limit=limit)[0]

    def issue(self, repo, num):
        return self._cached(("issue", repo, num), lambda: self._gh.issue(repo, num))

    def pr_for_branch(self, repo, branch):
        return self._cached(("pr_for_branch", repo, branch),
                            lambda: self._gh.pr_for_branch(repo, branch))

    def pr_comments(self, repo, num):
        return self._cached(("pr_comments", repo, num), lambda: self._gh.pr_comments(repo, num))

    def newest_fetch_at(self):
        """When the most recent real ``gh`` fetch landed, across every memoized query (``None``
        before the first). This is the age of what FALLBACK mode is showing (issue #146): with the
        runner quiet, its published document is abandoned, so the only honest clock left is our own
        last read. The newest fetch is the right one — the caches expire independently, and the
        picture is as fresh as its freshest part."""
        stamps = [c.last_fetch_at() for c in self._caches.values()]
        stamps = [s for s in stamps if s is not None]
        return max(stamps) if stamps else None


# =============================== ConcludedFlights — fetch once, remember (issue #48) ===============================

class ConcludedFlights:
    """Remembers the settled GitHub facts of CONCLUDED flights (status ``merged``, or the issue
    closed) so the snapshot asks GitHub for each concluded flight AT MOST ONCE per dashboard run.

    A concluded flight's PR, title, and posted review can NEVER change — yet ``CachedGh``'s
    ``gh_poll_seconds`` clock re-asks all three every window, forever, a cost that grows with every
    landing and helped drain the hourly quota (2026-07-08). In-flight flights still ride that clock
    (their answers DO change); a flight only becomes eligible here once it has concluded.

    Only a NON-EMPTY answer is remembered. If gh is unreachable the instant a flight concludes, the
    first read fails closed to empty — locking THAT in would blank the flight's cargo/title for the
    whole run. Instead an empty read is retried on the next poll until a real answer arrives, then
    that answer is remembered forever. Self-healing: a transient outage costs a few extra reads, not
    a permanently wrong flight. Purely in-memory — a dashboard restart simply re-remembers, and
    nothing is ever written into the loop's state home (issue #48 boundary)."""

    # A concluded flight's PR reads MERGED or CLOSED — never OPEN. So an OPEN reading is a stale
    # value the 30s ``gh`` cache handed back in the ≤30s window between the flight's last in-flight
    # poll and its conclusion; remembering it would freeze pre-merge facts for the run. Only a
    # SETTLED read is locked in; a stale OPEN is re-read on the next (slow-clock) poll until it
    # settles (Codex review).
    _SETTLED_PR_STATES = {"MERGED", "CLOSED"}

    def __init__(self):
        self._store = {}      # (kind, *key) -> remembered value

    def _remember(self, key, fetch, keep):
        if key in self._store:
            return self._store[key]
        value = fetch()
        if keep(value):       # a value worth locking in; otherwise retry on the next poll
            self._store[key] = value
        return value

    def pr_facts(self, repo, branch, fetch):
        """The concluded flight's PR facts (incl. its diff size), fetched once via ``fetch`` and
        remembered. ``fetch`` is a no-arg thunk hitting gh, called ONLY on a miss — and only a
        SETTLED read (state MERGED/CLOSED) is remembered, so a stale OPEN value the gh cache handed
        back is never frozen in (it is re-read until the PR settles)."""
        def keep(v):
            return isinstance(v, dict) and v.get("state") in self._SETTLED_PR_STATES
        return self._remember(("pr", repo, branch), fetch, keep) or {}

    def confirm_closed(self, repo, num, fetch):
        """Whether issue ``num`` is POSITIVELY closed — a real ``issue()`` read whose ``state`` is
        ``CLOSED`` (issue #48 Codex review: mere absence from the capped, fail-closed open-issue list
        is NOT proof, and would freeze a still-live flight's facts during a gh outage). Fetched once
        and remembered; a non-CLOSED / failed read is retried on the next poll (fail-closed to
        still-open)."""
        def keep(v):
            return isinstance(v, dict) and v.get("state") == "CLOSED"
        v = self._remember(("closed", repo, num), fetch, keep)   # returns the dict, not a verdict
        return isinstance(v, dict) and v.get("state") == "CLOSED"

    def title(self, repo, num, fetch):
        """The closed issue's title, fetched once and remembered (a closed issue's title is fixed)."""
        return self._remember(("title", repo, num), fetch, bool)

    def review_state(self, repo, pr, fetch):
        """The concluded flight's review STATE (issue #176 — ``reviewed`` / ``stale`` / ``absent`` /
        ``unread``, see lib/review_marker) — fetched once and remembered. Only the POSITIVE settled
        reading, ``reviewed``, is locked in: the dashboard's ``gh.pr_comments`` fail-closes a refused
        read to ``[]`` (it never raises), which parses as ``absent`` — indistinguishable from a clean
        empty read — so an ``absent`` is never frozen (a gh blip would otherwise brand a reviewed PR
        'no review' for the whole run). The common merged flight carries a verdict pinned to its head
        and settles here on the first reachable read. The exceptions re-read every poll, fail-closed:
        a merge-updated merged flight reads ``stale`` (its merged head differs from the reviewed pin;
        the engine carries the verdict across via ``review_carry``, which the board does not have —
        see lib/review_marker), and an unreachable read reads ``absent``/``unread``. Re-reading that
        minority is the safe cost of never locking in a false 'no review'."""
        keep = lambda v: v == review_marker.REVIEWED
        return self._remember(("review", repo, pr), fetch, keep)


# =============================== the snapshot — folding the truth layer together ===============================
# assemble_snapshot composes the tested pure functions into the one JSON document the front-end
# binds (design record B.1 — the JS computes nothing). It reads FRESH local state every call
# (journal/state/diff are cheap and change fast) and takes ``gh_mod`` and ``usage`` as inputs so the
# expensive GitHub + usage egress can ride the slow clock (decision B.2, wired by the bin entry).

_UNKNOWN_USAGE = {"known": False, "status": "unknown", "five_hour_pct": None, "seven_day_pct": None,
                  "five_hour_resets_epoch": None, "seven_day_resets_epoch": None}

_LANDED = (flights.TOUCHDOWN, flights.TAXI_IN)
# The stages where a live session's contrail age (activity mtime) is the honest "idle" reading; off
# them, "idle" means "time since anything happened to this flight" (its last journal event).
_LIVE_AIR = (flights.TAXI_OUT, flights.TAKEOFF, flights.DOWNWIND, flights.BASE_TURN, flights.FINAL)

# The review-marker parse lives in lib/review_marker — a documented mirror of the engine's gate,
# pinned to it by tests/test_review_marker (issue #176). The dashboard once kept its own literal
# here and substring-matched it, which silently broke the moment #154 pinned the verdict to its
# diff; the mirror + its pin test are what stop that drift from recurring.

# The sort rank for the boring table's STAGE column — the traffic-pattern order (on-circuit first,
# by circuit position; off-path states grouped after). Server-side so the JS sorts by a supplied
# number and never encodes the circuit order itself (design record B.1). Unknown ⇒ 99 (sorts last).
_STAGE_RANK = {s: i for i, s in enumerate(flights.CIRCUIT_STAGES)}
for _i, _s in enumerate((flights.HOLDING, flights.STRANDED, flights.SESSION_FROZEN,
                         flights.MERGES_FREEZE, flights.PARKED, flights.AWAITING),
                        start=len(flights.CIRCUIT_STAGES)):
    _STAGE_RANK[_s] = _i

# A plain one-line banner per worst-condition, for the camera-independent trouble slot (design §4/§5).
_TROUBLE_TEXT = {
    "runner-down": "Runner down — the dashboard can't be trusted until it's back",
    "alert": "ALERT raised — a factory-stop the runner declared",
    flights.AWAITING: "A decision is waiting on you",
    flights.PARKED: "Flights parked — your call",
    flights.SESSION_FROZEN: "A session has frozen on the field",
    flights.STRANDED: "A finished flight is stranded at the gate — the runner hasn't landed it",
    "spinning": "A flight looks busy but is making no progress",
    flights.MERGES_FREEZE: "Landings paused — a repair flight is out",
}


def _flight_num(iid):
    """``i23`` -> ``23``; a non-``i<N>`` id -> ``None`` (kept off the boards, never guessed)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _rec_num(rec):
    """The issue number a journal record is about — from ``num``, else its ``id``, else an
    ``event.id`` envelope. ``None`` when the record names no flight."""
    n = rec.get("num")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    n = _flight_num(rec.get("id"))
    if n is not None:
        return n
    ev = rec.get("event")
    if isinstance(ev, dict):
        return _flight_num(ev.get("id"))
    return None


def _hhmm(ts):
    """Local wall-clock ``HH:MM`` for a journal ts, or ``""`` when the ts is unusable. A corrupt
    JSON ``NaN``/``Infinity`` (``json.loads`` accepts both) is screened by ``_finite`` BEFORE
    ``time.localtime`` — which raises ``OverflowError`` on an infinite ts (time_t out of range) —
    so one bad journal line can never crash a snapshot. ``OverflowError`` is also caught for an
    extreme-but-finite ts."""
    if not _finite(ts):
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return ""


def _last_ts(records, act=None, outcome=None):
    """The ts of the last record (file order) matching ``act``/``outcome``, or ``None``. Non-finite
    ts (a corrupt JSON ``NaN``) are ignored, so bad lines never poison a duration."""
    found = None
    for r in records:
        if act is not None and r.get("act") != act:
            continue
        if outcome is not None and r.get("outcome") != outcome:
            continue
        ts = r.get("ts")
        if _finite(ts):
            found = ts
    return found


def _first_ts(records, act=None):
    for r in records:
        if act is not None and r.get("act") != act:
            continue
        ts = r.get("ts")
        if _finite(ts):
            return ts
    return None


def _flight_note(flight, memo):
    """A short plain note for the boring table's NOTE column — honest, one line, no metaphor
    flourish (the rich Needs-You gloss + tower radio flavor are Task 9's job)."""
    stage = flight["stage"]
    if flight.get("spinning"):
        return "spinning? — active but making no progress"
    if stage == flights.PARKED:
        return (memo or "parked — your call").strip().replace("\n", " ")[:90]
    if stage == flights.AWAITING:
        return (memo or "awaiting your decision").strip().replace("\n", " ")[:90]
    if stage == flights.HOLDING:
        return "holding — number 2 for landing"
    if stage == flights.STRANDED:
        return "stranded at the gate — report filed, runner hasn't landed it"
    if flight.get("merged"):
        pr = flight.get("pr")
        base = "merged" + (" PR #%s" % pr if pr else "")
        if flight.get("wander"):
            return base + " · wandered — see report"
        if flight.get("attempt", 1) > 1:
            return base + " · 2nd attempt"
        return base
    return ""


# Seconds a PROVEN-fresh landing keeps taxiing on the field before the arrivals board alone
# carries it — without this, every merged flight ever would sit on the apron forever.
_FIELD_LINGER = 600


def _on_field(flight, merged_ts, now):
    """Whether the field still draws this flight. Everything un-landed is on the field (a parked
    plane demanding attention NEVER leaves, §5); a landed flight lingers only while its merge is
    PROVEN fresh — a status-merged flight with no journal merge proof has unprovable recency, so
    the board carries it and the field does not (celebration honesty's placement twin, §7)."""
    if flight["stage"] not in _LANDED:
        return True
    return _finite(merged_ts) and (now - merged_ts) < _FIELD_LINGER


def _lit(flight, worst_state):
    """Whether the trouble dimming leaves THIS flight lit (§5 alarm salience: the field dims and
    the problem is lit). Only flight-attributable conditions light planes; runner-down/ALERT/
    merges-freeze are whole-surface or tower treatments, never a plane spotlight."""
    if worst_state == "spinning":
        return bool(flight.get("spinning"))
    if worst_state in (flights.PARKED, flights.AWAITING, flights.SESSION_FROZEN, flights.STRANDED):
        return flight["stage"] == worst_state
    return False


def _field_caption(repo_flights, state, arrivals, now, stand=(), gh_unreachable=False):
    """The quiet-state caption (§5: calm is never ambiguous — the empty field SAYS it is clear).
    Only a genuinely quiet field carries it: repo condition ``ok`` and NO plane on the field at
    all — a holding orbit, a plane at the stand, or a fresh landing still taxiing suppress it
    even though none is a pill condition (review fix 2026-07-07: a caption over a visible plane
    is a false calm). ``stand`` is the queued-flights projection (issue #32); a plane standing at a
    gate is just as visible as one in the air, so it too suppresses the all-clear.

    ``gh_unreachable`` (issue #38) suppresses the all-clear entirely: when GitHub is unreachable the
    field's GitHub-derived layer (queue, arrivals enrichment, titles) is BLIND, so a quiet field is
    not proven quiet — the honest read is "we can't see," which the dark-tower state carries, never a
    false "all clear." A genuinely EMPTY-but-reachable answer (``gh_unreachable`` False) is unchanged."""
    if gh_unreachable:
        return None
    if state["state"] != "ok":
        return None
    if stand or any(f["display"]["on_field"] for f in repo_flights):
        return None
    if arrivals and _finite(arrivals[0]["ts"]):
        return "last landing %s ago — all clear" % format_duration(now - arrivals[0]["ts"])
    return "no landings yet — all clear"


def _field_banners(repo_flights):
    """One towed name banner per airborne circuit-leg flight that has a name to tell and no tag of
    its own — chosen HERE, not in the pixels (review fix 2026-07-07, squint test). Generalises the
    former single-featured pick to a LIST (issue #204): William watched the one cloth cover a
    neighbouring in-flight plane that itself showed no name — "which flight is that?" was the exact
    thing he reached for and it wasn't on the screen. Now EVERY plane on the working DOWNWIND leg
    (the long building leg over Build Island) tows its own name, so no in-flight plane is nameless.

    Excluded, by owner ruling #1 — no double labelling:
      * HOLDING keeps its amber holding-pattern tag; FINAL keeps its gold gate tag.
      * TAKEOFF and BASE_TURN are airborne circuit legs too, but their runway-owned anchors fan out
        only 12–16px apart (sub-hull — see #203/#206) — too tight to tow a legible 74px cloth
        occlusion-free without re-spacing those anchors, which is out of this issue's scope. They
        are deferred to a needs-owner follow-up and carry no banner yet.

    Ordered by flight number so the list never flickers between polls; the client staggers the
    cloths so no banner ever covers a plane or another banner (issue #204, owner ruling #3). Empty
    (never ``None``) when nothing is on the leg."""
    working = sorted((f for f in repo_flights
                      if f["stage"] == flights.DOWNWIND and f["display"]["on_field"]),
                     key=lambda f: f["num"])
    return [{"num": f["num"], "label": f["label"],
             "text": "%s · BUILDING · %s" % (f["label"], f["display"]["elapsed"].upper())}
            for f in working]


def _fun_map(config):
    """The resolved fun toggles the front-end binds as plain booleans (design record B.1): each
    mechanic AND the master switch. A config assembled without a ``fun`` block (tests, embedders)
    honestly defaults to all-on — joy is the default (§0.1)."""
    fun = config.get("fun") or {}
    master = bool(fun.get("master", True))
    return {k: master and bool(fun.get(k, True)) for k in config_mod.FUN_MECHANICS}


def _idle_seconds(flight, activity_mtime, last_event_ts, now):
    """The boring table's staleness numeral (design §4). A LANDED flight has none ("—"). A live
    in-air flight's staleness is its contrail age (activity mtime). Anything else (parked, holding)
    reads staleness as time since its last journal event — how long it has sat still."""
    if flight["stage"] in _LANDED:
        return None
    if activity_mtime is not None and flight["stage"] in _LIVE_AIR:
        return max(0.0, now - activity_mtime)
    if last_event_ts is not None:
        return max(0.0, now - last_event_ts)
    return None


def _project_display(flight, repo_name, records, activity_mtime, now):
    """The presentational numerals the boring table sorts by (design §4): a human string PLUS the
    raw number for every channel, so the art stays flavor and the sort stays honest."""
    launch_ts = _first_ts(records, act="launch")
    merged_ts = _last_ts(records, act="merge", outcome="ok")
    last_event_ts = _last_ts(records)

    end = merged_ts if merged_ts is not None else now
    elapsed_seconds = (end - launch_ts) if launch_ts is not None else None
    idle_seconds = _idle_seconds(flight, activity_mtime, last_event_ts, now)

    cargo = flight.get("cargo") or {}
    present = bool(cargo.get("present"))
    added, removed = int(cargo.get("added", 0)), int(cargo.get("removed", 0))
    diff = ("+%d/−%d" % (added, removed)) if present else "—"
    files = cargo.get("files") if present else None

    return {
        "flight": flight["label"], "num": flight["num"], "repo": repo_name, "stage": flight["stage"],
        "stage_rank": _STAGE_RANK.get(flight["stage"], 99),   # the table sorts STAGE by this number
        "in_air": flight["stage"] in _LIVE_AIR,               # for the field's "N in the air" tally
        "elapsed": format_duration(elapsed_seconds), "elapsed_seconds": elapsed_seconds,
        "idle": format_duration(idle_seconds), "idle_seconds": idle_seconds,
        "diff": diff, "diff_added": added, "diff_removed": removed,
        "files": files,
        "on_field": _on_field(flight, merged_ts, now),
        "attempt": flight.get("attempt", 1),
        "note": _flight_note(flight, flight.get("memo")),
        # unknown staleness sorts to the bottom of a "most stale first" sort, never masquerades as 0.
        "staleness": idle_seconds if idle_seconds is not None else -1,
        "merged_ts": merged_ts,
    }


def _required_checks(repo):
    """The repo's required check names for the gate checklist. Prefer an explicit value on the repo
    entry (tests inject it); else read the repo's own ``.superlooper/config.json`` (decision B.4);
    else fail closed to ``[]`` — an unreadable required set never falsely clears a gate (§3)."""
    rc = repo.get("required_checks")
    if isinstance(rc, list):
        return rc
    path = repo.get("path")
    if path:
        try:
            body = json.loads((Path(path) / ".superlooper" / "config.json").read_text())
            rc = body.get("required_checks")
            return rc if isinstance(rc, list) else []
        except (OSError, ValueError):
            return []
    return []


def _probe_open_issues(gh_mod, slug):
    """The unlabeled open-issue list PLUS gh reachability (issue #38), from the ONE open-issue read
    the snapshot makes every poll. Returns ``(issues, reachable)`` where ``reachable`` is:

      * ``True`` — gh answered (even empty: an honest all-clear);
      * ``False`` — gh FAILED (missing binary / unauthenticated / timeout / nonzero rc): the honest
        GitHub-unreachable state, never a false all-clear;
      * ``None`` — reachability UNKNOWN: no gh wired (a read-only embedder), or a legacy gh-like that
        predates ``open_issues_probe``. Unknown is deliberately NOT "unreachable" — the dashboard
        never alarms a state it cannot actually observe.

    The real adapter (``gh`` + ``CachedGh``) always exposes ``open_issues_probe``; the ``getattr``
    fallback keeps ``gh_mod`` duck-typed so an embedder without the probe still assembles (with
    reachability unknown). NO extra gh call: the probe IS the unlabeled open-issue read, reused below
    for both ``open_nums`` and titles."""
    if gh_mod is None:
        return [], None
    probe = getattr(gh_mod, "open_issues_probe", None)
    if probe is None:                       # a legacy gh-like without the probe → reachability unknown
        return list(gh_mod.open_issues(slug) or []), None
    issues, reachable = probe(slug)
    return (list(issues) if isinstance(issues, (list, tuple)) else []), reachable


def _titles(slug, open_issue_list, gh_mod, merged_nums, concluded=None):
    """Best-effort ``{num: title}`` from GitHub: the ALREADY-fetched open-issue list (queue +
    in-flight titles, passed in so the unlabeled read is made once — issue #38) plus a per-issue view
    for each landed flight (closed, so absent from the open list). Empty when GitHub is unreachable —
    titles are enrichment, never required (the flight number always shows). A landed flight is
    concluded, so its title is fetched through ``concluded`` (fetch once, remember — issue #48) when
    supplied; the per-issue reads stay on the live clock (their titles DO change)."""
    titles = {}
    for iss in open_issue_list:
        if isinstance(iss, dict) and isinstance(iss.get("number"), int):
            titles[iss["number"]] = iss.get("title")
    if gh_mod is None:
        return titles
    for num in merged_nums:
        def fetch(n=num):
            try:
                iss = gh_mod.issue(slug, n)
            except Exception:
                return {}
            return iss if isinstance(iss, dict) else {}
        iss = concluded.title(slug, num, fetch) if concluded is not None else fetch()
        if isinstance(iss, dict) and iss.get("title"):
            titles[num] = iss["title"]
    return titles


def _review_state(gh_mod, slug, pr, head_oid, concluded=None):
    """PR ``pr``'s review evidence, as the three-way state the board draws (issue #176):
    ``reviewed`` (a verdict pinned to the PR's current head — provably this diff), ``stale`` (a
    review exists but not for this head: superseded pin, or the legacy unpinned form), ``absent``
    (no marker), or ``unread`` (the comments/head couldn't be read — fail closed, NOT "no review").
    ``head_oid`` is the PR's current head, needed to judge the pin (the whole point of #154's pin,
    which the old substring check ignored). The parse itself is lib/review_marker, a documented
    mirror of the engine's gate.

    For a CONCLUDED flight a ``concluded`` memory is supplied so the settled answer is fetched once
    and remembered (issue #48); an in-flight flight passes ``concluded=None`` and re-reads every poll
    (its review is still arriving)."""
    if gh_mod is None or not pr:
        return review_marker.ABSENT      # no PR yet ⇒ no review posted — fail closed to not-reviewed

    def fetch():
        try:
            comments = gh_mod.pr_comments(slug, pr)
        except Exception:
            comments = None              # unreadable ⇒ UNREAD, never a confident "no review"
        return review_marker.review_state(comments, head_oid)

    return concluded.review_state(slug, pr, fetch) if concluded is not None else fetch()


def _connection_resolver(gh_mod, slug):
    """A fail-closed ``satisfied(n)`` for the departures board: ``True`` only with POSITIVE proof
    that blocker issue ``n`` has landed — its GitHub issue reads ``state == "CLOSED"`` (a merge
    closes the issue). An open blocker, an unreadable one, or no gh at all ⇒ ``False`` (still
    awaiting). This is deliberately NOT "n is absent from the open-issue list": that list is capped
    (``--limit``) and fails closed to ``[]``, so absence could mean "beyond the page" or "gh down",
    and treating either as arrived would fly a still-blocked flight (Codex cross-review, Task 8).
    Per-poll memoized so a queue full of the same connection costs one gh read."""
    cache = {}

    def satisfied(n):
        if n not in cache:
            try:
                iss = gh_mod.issue(slug, n) if gh_mod is not None else {}
            except Exception:            # a cached/injected adapter that raises must still fail closed
                iss = {}
            cache[n] = isinstance(iss, dict) and iss.get("state") == "CLOSED"
        return cache[n]

    return satisfied


def _departures(gh_mod, slug, titles, issues_state):
    """The launch queue in REAL order (design record §3 / Task 8) — the RUNNER's order, mirrored.

    The candidates are the open ``agent-ready`` issues the runner would still launch. Launching
    strips ``agent-ready`` (the runner's own label move), so an in-flight issue leaves this board by
    itself; the loopstate check behind ``launch_rules.is_launch_candidate`` is what makes that
    airtight when a label write is mid-flight — and, far more importantly, is what lets a
    conflict-REBUILT issue back onto the board at all (issue #138). The regenerate path puts
    ``agent-ready`` back with status ``ready`` + ``requeue_front``: that flight is queued again,
    front of its band. The board used to drop every issue that had a loopstate entry, so a rebuild —
    the moment the owner most needs the truth — was exactly when its order went wrong.

    The ordering, the eligibility refusals and the connection semantics are all the tested pure
    :func:`flights.queue_rows`; this only gathers facts and hands it a fail-closed connection
    resolver. Empty when GitHub is unreachable (the honest empty board)."""
    if gh_mod is None:
        return []
    candidates = []
    for iss in gh_mod.open_issues(slug, label="agent-ready"):
        if not isinstance(iss, dict):
            continue
        num = iss.get("number")
        if not isinstance(num, int):
            continue
        state = issues_state.get("i%d" % num)
        if not launch_rules.is_launch_candidate(state):
            continue
        candidates.append({"num": num, "title": iss.get("title") or titles.get(num) or "",
                           "labels": iss.get("labels"), "body": iss.get("body"),
                           "created_at": iss.get("createdAt"),
                           "requeue_front": launch_rules.requeue_front(state)})
    return flights.queue_rows(candidates, satisfied=_connection_resolver(gh_mod, slug))


# The field's physical row of jet-bridged west gates (issue #32). The engine parks a queued plane at
# each of these stands (``BAYS_STAND`` in airfield_live.js), so the projection below caps to the same
# count — the full queue always lives, paginated, on the departures board; the gates show only its
# front. Keep this in step with the length of ``BAYS_STAND`` (a real terminal has finite gates).
STAND_BAYS = 3


def _stand(departures, flying_nums=()):
    """The queued flights standing at the gates (design record §3: "at the stand — approved, queued").
    A pure projection of the ALREADY-derived departures queue (``flights.queue_rows`` via
    :func:`_departures`) — no new semantics: exactly the LAUNCHABLE rows, in the queue's own launch
    order, capped to the physical gate count. A blocked "awaiting connection" row and a "paperwork"
    row are never launchable, so they are shown only on the board and never park a plane (§3: never
    in the air). The field binder turns each row into a healthy waiting plane; it computes nothing.

    ``flying_nums`` are the issues that ALREADY have a plane on the field (they have a loopstate
    entry, so ``_assemble_repo`` built a flight for them). A requeued flight is both — queued on the
    board AND a plane the runner has flown before — so it is skipped here: one issue is one plane,
    and the field must never show its aircraft twice (the board is where its queue position lives)."""
    flying = set(flying_nums)
    launchable = [d for d in departures if d.get("launchable") and d.get("num") not in flying]
    return [{"num": d["num"], "flight": d.get("flight") or ("SL-%d" % d["num"]),
             "destination": d.get("destination") or "", "pos": d.get("pos"),
             "expedited": bool(d.get("expedited"))}
            for d in launchable[:STAND_BAYS]]


def _arrivals(repo_flights, titles, now):
    """The arrivals board: landed flights, newest first (design §3), capped to the split-flap board's
    bounded backlog (issue #30 owner amendment — the smaller of 5 pages or 3 days of landings, older
    entries drop off). The ordering + both caps are the tested pure :func:`flights.cap_arrivals`; this
    only shapes each row and hands it the list. Remark is honest — a wandered merge gets "see report"
    (no flourish, §7), a rebuilt flight is marked 2nd attempt, else a clean "landed ✓"."""
    landed = [f for f in repo_flights if f["stage"] in _LANDED]

    def remark(f):
        if f.get("wander"):
            return "▪ see report"
        if f.get("attempt", 1) > 1:
            return "landed ✓ · 2nd attempt"
        return "landed ✓"

    rows = []
    for f in landed:
        num = f["num"]
        ts = f["display"]["merged_ts"]
        rows.append({"num": num, "flight": f["label"], "landed": titles.get(num) or f["label"],
                     "remark": remark(f), "ts": ts, "hhmm": _hhmm(ts)})
    return flights.cap_arrivals(rows, now)


def _needs_you(all_flights, now):
    """Whole-field cards for every flight waiting on William — parked or awaiting an owner decision
    (design §4; Needs You never filters by camera). The plain-language headline, leading gloss (with
    the literal term for hover), the four decision kinds, and the conflict-cap collision sentence
    (with Discuss defaulted) all come from the tested ``lib/cards`` gloss layer; this only computes
    the EXACT age numeral the badge carries (the boring-mode discipline: every state paired with a
    real number)."""
    out = []
    for f, records, slug in all_flights:
        stage = f["stage"]
        if stage not in (flights.PARKED, flights.AWAITING):
            continue
        # The journal slice rides along so the card can carry its dossier — the evidence behind the
        # decision (issue #162), so William judges the hand-back without opening a terminal.
        card = cards_mod.needs_you_card(f, slug, journal_slice=records)
        # The age numeral: time since the machine gave up (a park). An amber decision (needs-william/
        # bounce) has no single "since" event, so it carries the state word alone (never a fake age).
        age = None
        if stage == flights.PARKED:
            park_ts = _last_ts(records, act="park")
            age = (now - park_ts) if park_ts is not None else None
        dur = format_duration(age)
        card["badge"] = card["badge_base"] + ("" if dur == "—" else " " + dur)
        card["age_seconds"] = age
        out.append(card)
    return out


def _ts_key(rec):
    ts = rec.get("ts")
    return ts if _finite(ts) else 0   # non-finite (NaN) sorts as oldest, never poisons the sort


def _tower_window(records, last_seen=None, limit=14, max_rows=120, operator="the owner"):
    """The tower-log comms window (design record §4 / Task 9): journal records as rows in
    CHRONOLOGICAL order (a comms feed reads by time, never by raw file position — the journal is
    normally append-ordered, but the reader must not assume it). Each row is glossed through the
    tested ``lib/tower`` vocabulary — a plain ``text`` sentence with its optional ``radio`` flavor,
    ``kind``, and server-side ``tier`` beside it — and carries the exact ``raw`` journal line so
    every row expands to ground truth.

    The window holds the most recent ``limit`` COMMS rows, so routine bookkeeping (the ``relabel``
    flurry GitHub's read-lag produces, issue #36) can never crowd real traffic out of the feed: the
    last ``limit`` comms rows are ALWAYS kept. The routine rows interleaved within that span ride
    along tagged ``tier: "routine"`` — the client hides them by default and reveals them on demand.
    ``max_rows`` bounds a pathologically routine-heavy journal by trimming the OLDEST routine rows
    only — never a comms row (Codex #36). The window is sliced BEFORE the divider is placed, so the
    "since you last looked" line always lands on a comms row the client actually shows — a flood of
    new traffic can never leave a "N NEW" badge with no divider drawn (Codex, Task 9). ``new_count``
    is the TOTAL number of COMMS records newer than ``last_seen`` across the whole journal — the
    honest badge count of real traffic (routine noise never inflates it), which may exceed the shown
    window. Returns ``(rows, new_count)``."""
    ordered = sorted(records, key=_ts_key)
    tiers = [tower_mod.tier(r) for r in ordered]
    # Start at the limit-th comms row from the end (or 0 when fewer than `limit` comms exist), so the
    # last `limit` real comms rows are ALWAYS shown — routine noise can never crowd them out (#36).
    comms_at = [i for i, t in enumerate(tiers) if t == "comms"]
    start = comms_at[-limit] if len(comms_at) >= limit else 0
    sel = list(range(start, len(ordered)))
    # Bound a pathologically routine-heavy journal WITHOUT ever dropping a comms row: keep every comms
    # row in the span plus the NEWEST routine rows up to the remaining budget (oldest routine trimmed).
    if len(sel) > max_rows:
        n_comms = sum(1 for i in sel if tiers[i] == "comms")
        routine_budget = max(0, max_rows - n_comms)
        kept, seen_routine = [], 0
        for i in reversed(sel):                   # newest-first: every comms, newest routine to budget
            if tiers[i] == "comms":
                kept.append(i)
            elif seen_routine < routine_budget:
                kept.append(i)
                seen_routine += 1
        sel = sorted(kept)
    rows = []
    for i in sel:
        rec = ordered[i]
        ts = rec.get("ts")
        c = tower_mod.comms_row(rec, operator)
        rows.append({"ts": ts, "hhmm": _hhmm(ts), "text": c["text"], "radio": c["radio"],
                     "kind": c["kind"], "num": c["num"], "tier": c["tier"],
                     "raw": json.dumps(rec, separators=(",", ":"))})
    tower_mod.apply_divider(rows, last_seen)      # the divider lands within the SHOWN window
    new_count = 0
    if last_seen is not None:
        new_count = sum(1 for r in ordered
                        if tower_mod.tier(r) == "comms" and _finite(r.get("ts")) and r.get("ts") > last_seen)
    return rows, new_count


def _newest_direct_fetch(gh_mod):
    """When the dashboard's OWN GitHub read last landed (``None`` if never / not a cached adapter).
    In FALLBACK this is the age of what's on screen — the runner's document is abandoned, so the
    only honest clock is our own last read."""
    fn = getattr(gh_mod, "newest_fetch_at", None)
    return fn() if callable(fn) else None


def _pick_source(facts, config, now, gh_mod):
    """Choose which source answers this repo's GitHub questions, and the honest words for it
    (issue #146).

    The runner's published view is PRIMARY: when it is usable and the heartbeat is fresh, the
    assembler is handed a :class:`runner_source.RunnerSource` and reaches GitHub zero times. When
    the runner goes quiet (or is too old to publish), the GitHub adapter takes over — the loud
    FALLBACK — and the mode dict carries the banner that says so.

    Returns ``(source, mode)``. ``concluded`` memory is deliberately NOT used in LIVE (see the call
    site): there is no egress to memoize, and a value remembered from the runner's document must
    never be served later as though a real GitHub read had confirmed it."""
    mode = flights.source_mode(
        facts["published_view"],
        heartbeat_age=facts["heartbeat_age"], heartbeat_epoch=facts["heartbeat_epoch"],
        now=now, silent_after=config.get("runner_silent_seconds", config_mod.RUNNER_SILENT_SECONDS),
        fetched_at=_newest_direct_fetch(gh_mod), hhmm=_hhmm, fmt=format_duration)
    if mode["mode"] == flights.SOURCE_LIVE:
        return runner_source.RunnerSource(facts["published_view"]), mode
    return gh_mod, mode


def _flight_closed(source, slug, num, open_nums):
    """Whether flight ``num``'s issue has closed, asked the way THIS source can honestly answer.

    A source that knows closure POSITIVELY (the runner's published ``closed_nums``) is asked
    outright: its open set is partial — it polls agent-ready + in-progress only — so inferring
    closure from absence would conclude every parked/holding flight. The GitHub adapter has no such
    oracle (a per-flight ``issue()`` read would be exactly the egress this issue removes), so it
    keeps the original inference from its complete open-issue list."""
    oracle = getattr(source, "is_closed", None)
    if callable(oracle):
        return bool(oracle(slug, num))
    return bool(source is not None and num not in open_nums)


def _assemble_repo(repo, config, now, gh_mod, diff_reader, last_seen=None, concluded=None):
    """Fold one repo's state home into its snapshot slice: flights (each with its drawer), boards,
    tower window (with the since-you-last-looked divider against ``last_seen``), shipped delta,
    incident sign, and the repo's worst-condition state (for the pill). ``concluded`` (a
    ``ConcludedFlights``, ``None`` in embedders that don't wire it) makes each concluded flight's
    settled GitHub facts a once-per-run fetch instead of a forever poll (issue #48).

    Since issue #146 the GitHub facts come from the RUNNER's published view whenever it is fresh
    (``_pick_source``); ``gh_mod`` is the fallback for a silent runner, never the primary."""
    slug = repo["slug"]
    name = repo.get("name") or slug
    operator = config_mod.operator(config)             # signs the re-approval line + drawer (issue #58)
    home = repo["state_home"]
    facts = readers.read_state_home(home, now=now)
    journal = readers.read_journal(home)
    required_checks = _required_checks(repo)

    # Which source is answering — the runner's own view (normal) or GitHub directly (loud fallback).
    # Everything below asks `source`, never `gh_mod`, so LIVE is zero-egress by construction.
    source, source_mode = _pick_source(facts, config, now, gh_mod)
    if source_mode["mode"] == flights.SOURCE_LIVE:
        concluded = None      # nothing to memoize when nothing goes out; see _pick_source

    issues_state = facts["issues_state"].get("issues", facts["issues_state"]) or {}
    flight_repo = {"idle_seconds": repo.get("idle_seconds", 480),
                   "freeze_seconds": repo.get("freeze_seconds", 2700),
                   "required_checks": required_checks, "now": now,
                   "merges_frozen": facts["merges_frozen"], "progress_window_seconds": 900}

    # The unlabeled open-issue read — made ONCE (issue #38): it yields both the open-issue numbers
    # (for the closed/concluded signal + queue exclusion) AND gh reachability, and its list is reused
    # for titles below. gh_reachable: True answered · False unreachable · None unknown (no gh wired).
    open_issue_list, gh_reachable = _probe_open_issues(source, slug)
    gh_unreachable = gh_reachable is False
    open_nums = {iss["number"] for iss in open_issue_list
                 if isinstance(iss, dict) and isinstance(iss.get("number"), int)}

    # Each real lane owns a runway for the whole poll (§3: "2 runways = 2 concurrent builds").
    lane_runways = flights.assign_runways(
        [st.get("lane") for st in issues_state.values() if isinstance(st, dict)])

    repo_flights = []
    flight_records = []       # (flight, its journal slice, slug) for whole-field Needs You
    for iid, st in issues_state.items():
        if not isinstance(st, dict):
            continue
        num = _flight_num(iid)
        if num is None:
            continue
        jslice = [r for r in journal if _rec_num(r) == num]
        branch = st.get("branch")
        pr = st.get("pr")
        # A concluded flight (merged, or its issue closed) can never change, so its PR/title/review
        # are fetched once and remembered (issue #48); an in-flight flight re-reads every poll.
        # Conclusion needs POSITIVE proof: "merged" is the runner's own settled word, but the "closed"
        # case must be a real ``issue().state == "CLOSED"`` read — mere absence from the capped,
        # fail-closed open-issue list is not proof (a gh outage empties it), and concluding on it
        # would freeze a still-live flight's facts for the run (Codex review; the departures blocker
        # resolver already holds this line). ``is_closed`` (absence) still feeds the pre-existing
        # ``closed`` stage input; only the MEMORY demands the stronger signal.
        is_closed = _flight_closed(source, slug, num, open_nums)
        is_concluded = st.get("status") == "merged"
        if not is_concluded and concluded is not None and is_closed:
            is_concluded = concluded.confirm_closed(slug, num, lambda n=num: source.issue(slug, n))
        mem = concluded if is_concluded else None
        # An INVESTIGATE flight never opens a PR and never merges — its completion signal is a
        # marker COMMENT on the issue, so the lookup answers "no PR" for the loop's whole lifetime
        # of one (issue #16). The behaviour change this DOES make: a PR opened on an investigation
        # branch BY HAND would no longer show its cargo/gate here. Accepted — the runner would not
        # gate or merge it either (its three PR paths skip investigations, #21), so surfacing it
        # would draw a plane the loop will never land. The runner's skip is the precedent;
        # this is the board's half of that parity — in LIVE the runner's view already carries no PR
        # for one, so this makes the FALLBACK mode agree with the mode that is already primary.
        # Concluded investigations are what bite, in that fallback mode: ConcludedFlights.pr_facts
        # remembers only a SETTLED read, and {} is not settled, so the #48 memo can never latch one
        # — the read then repeats every gh_poll_seconds window for as long as the runner stays
        # silent, once per completed investigation, growing with every investigation the loop lands.
        # (The PR read is the one this removes; an investigation's title/closed reads still ride the
        # #48 memo at once-per-run, so this makes it cheap, not free.)
        # Keyed on a POSITIVE type stamp — an untyped flight is not provably an investigation and
        # keeps the build path, so a real PR's facts are never blanked on a missing stamp.
        is_investigation = st.get("type") == "investigate"
        if source is None or not branch or is_investigation:
            pr_facts = {}
        elif mem is not None:
            pr_facts = mem.pr_facts(slug, branch, lambda b=branch: source.pr_for_branch(slug, b))
        else:
            pr_facts = source.pr_for_branch(slug, branch)
        worktree = os.path.join(os.fspath(home), "worktrees", iid)
        cargo = diff_reader(worktree)
        issue = {
            "id": iid, "num": num, "status": st.get("status"), "branch": branch, "pr": pr,
            # Investigations file a report but open no PR and are REBUILT (not resumed) on re-approval;
            # the decision card reads this to keep its resume/rebuild verb honest (issue #161).
            "is_investigation": is_investigation,
            "activity_mtime": facts["activity"].get(iid), "blocked": facts["blocked"].get(iid),
            "awaiting_marker": iid in facts["awaiting"], "report_present": iid in facts["reports"],
            # The three-way review state (issue #176), judged against the PR's CURRENT head so a
            # verdict pinned to a superseded diff reads 'stale', not a false green. ``review_present``
            # is the field's historical name; it now carries that state string (gate_checklist and
            # build_flight both accept it — a legacy bool still works too).
            "review_present": _review_state(source, slug, pr,
                                            (pr_facts or {}).get("headRefOid"), mem),
            "journal": jslice,
            "pr_facts": pr_facts, "cargo": cargo, "diff_delta": None,
            "closed": is_closed,
            # #163: the durable owner-decision question this flight is waiting on (loopstate), so the
            # Questions surface can show it and offer the Answer action.
            "pending_question": st.get("pending_question"),
        }
        f = flights.build_flight(issue, flight_repo)
        f["display"] = _project_display(f, name, jslice, issue["activity_mtime"], now)
        # A flight whose lane is unknown still gets a deterministic runway (num parity) so the
        # field never flickers a plane between runways across polls.
        f["display"]["runway"] = lane_runways.get(st.get("lane"), num % 2)
        repo_flights.append(f)
        flight_records.append((f, jslice, slug))

    merged_nums = [f["num"] for f in repo_flights if f["stage"] in _LANDED]
    titles = _titles(slug, open_issue_list, source, merged_nums, concluded)   # one unlabeled read (#38)
    flying_nums = {f["num"] for f in repo_flights}

    # The flight-card drawer (Task 9): ground truth one click from anywhere — attached per flight now
    # that titles are known. The gloss/mapping (circuit rail, clearance glosses, memo history) all
    # come from the tested ``lib/cards`` layer; the server only supplies the title + locale HH:MM.
    for f, jslice, _ in flight_records:
        f["drawer"] = cards_mod.flight_drawer(f, jslice, slug, name,
                                              title=titles.get(f["num"]), hhmm=_hhmm,
                                              operator=operator)

    states = [f["stage"] for f in repo_flights]
    spinning = any(f["spinning"] for f in repo_flights)
    state = flights.repo_state(slug, states, spinning=spinning,
                               merges_frozen=facts["merges_frozen"], alert=facts["alert"],
                               heartbeat_age=facts["heartbeat_age"],
                               heartbeat_down_seconds=config.get("heartbeat_down_seconds", 300))

    # The trouble dimming's spotlight list (§5): annotated once the repo's worst condition is
    # known — the field dims and exactly the offending planes stay lit.
    for f in repo_flights:
        f["display"]["trouble"] = _lit(f, state["state"])

    arrivals = _arrivals(repo_flights, titles, now)
    last_landing_text = "no landings yet today"
    if arrivals and _finite(arrivals[0]["ts"]):
        last_landing_text = "last landing %s ago" % format_duration(now - arrivals[0]["ts"])

    tower_rows, tower_new = _tower_window(journal, last_seen, operator=operator)

    # The launch queue in real order (departures board), and its front projected to planes standing at
    # the gates (the field's "at the stand" stage, issue #32). The stand is derived FROM departures —
    # one queue, two honest renderings: the split-flap board (full, paginated) and the physical gates.
    # When GitHub is UNREACHABLE the whole GitHub-derived queue is dark BY CONSTRUCTION (issue #38,
    # Codex review): the reachability probe is our oracle, so we never fly a stale-cached agent-ready
    # list that could outlive the probe's failure by a cache window — the dark-tower state and a
    # populated departures board must never contradict each other.
    departures = [] if gh_unreachable else _departures(source, slug, titles, issues_state)
    stand = _stand(departures, flying_nums)

    repo_snap = {
        "slug": slug, "name": name, "airline": repo.get("airline") or name,
        "colors": {"tail": flights.airline_color(slug)},
        "flights": repo_flights,
        "boards": {"departures": departures, "arrivals": arrivals},
        # The empty-queue caption states the repo's REAL lane count (issue #35), never a hardcoded
        # "2": the count travels here and the JS binds the finished string (design record B.1). The
        # raw `lanes` rides along (None when unknown) so the truth is inspectable in the snapshot.
        "lanes": repo.get("lanes"),
        "queue_empty_caption": flights.empty_queue_caption(repo.get("lanes")),
        "stand": stand,
        "last_landing_text": last_landing_text,
        "field_caption": _field_caption(repo_flights, state, arrivals, now, stand, gh_unreachable),
        # GitHub reachability (issue #38): the honest signal the field's dark-tower state binds. When
        # `unreachable`, the queue/arrivals/titles above are BLIND (fail-closed to empty), so the JS
        # must render the lost-data-link state, never a false all-clear. `reachable` keeps the raw
        # tri-state inspectable (True answered · False refused · None no gh wired / unknown).
        "github": {"reachable": gh_reachable, "unreachable": gh_unreachable},
        "field_banners": _field_banners(repo_flights),
        "tower_log": tower_rows,
        "tower_new": tower_new,
        "shipped": flights.corner_stats(journal, now=now),
        "incident": flights.incident_stats(journal),
        "state": state,
        "merges_frozen": facts["merges_frozen"], "alert": facts["alert"],
        "runner_down": bool(state["state"] == "runner-down"),
        "heartbeat_age": facts["heartbeat_age"],
        # The state-format handshake (issue #45): the engine stamps the shape it wrote, and this is
        # the honest verdict on it. When NOT compatible the field names the mismatch (an engine that
        # changed the on-disk shape past what these readers parse) instead of silently blanking every
        # field it can no longer read; a pre-handshake home has no stamp and is grandfathered.
        "state_format": flights.state_format_status(facts["state_format"]),
        # WHICH SOURCE this slice was built from, and how old it is (issue #146). `mode` is `live`
        # (the runner's own published view — the normal state) or `fallback` (the runner went quiet,
        # so we polled GitHub ourselves). `data_age`/`tick_age` are shown in BOTH modes so the owner
        # can always see how old this picture is and when the runner last completed a tick; `banner`
        # is the fallback's two-fact shout and is None in LIVE. Nothing latches — a recovered runner
        # returns the next poll to LIVE and the banner clears itself.
        "source": source_mode,
    }
    return repo_snap, flight_records, journal, state


def _pill_message(pill, needs_you, runner_down):
    """The one plain sentence the global pill shows — computed server-side (design record B.1: the
    JS binds it, never derives it) so the pill and the trouble banner always agree, and so the
    wording is pinned by a test. Leads with the single WORST condition (design §4)."""
    if runner_down:
        return "RUNNER DOWN"
    st = pill.get("state")
    if st == "ok":
        return "all systems ok"
    parked = sum(1 for c in needs_you if c.get("state") == flights.PARKED)
    awaiting = sum(1 for c in needs_you if c.get("state") == flights.AWAITING)
    offender = pill.get("offender") or ""
    if st == "runner-down":
        return "RUNNER DOWN"
    if st == "alert":
        return "ALERT — %s" % offender
    if st == flights.AWAITING:
        return ("%d awaiting your decision" % awaiting) if awaiting > 1 else "a decision is waiting on you"
    if st == flights.PARKED:
        return "%d parked — your call" % parked
    if st == flights.SESSION_FROZEN:
        return "a session has frozen"
    if st == flights.STRANDED:
        return "a flight is stranded at the gate"
    if st == "spinning":
        return "a flight is spinning — no progress"
    if st == flights.MERGES_FREEZE:
        return "landings paused — repair flight out"
    return str(st or "attention").replace("-", " ")


def _runner_message(runner_repos):
    """The RUNNER DOWN sub-line for a downed runner, or ``""`` when all runners are up. Formatted
    server-side so the JS carries no duration math (design record B.1)."""
    down = [r for r in runner_repos if r["down"]]
    if not down:
        return ""
    age = down[0].get("heartbeat_age")
    if _finite(age):
        return "last heartbeat %s ago · state/runner.heartbeat stale" % format_duration(age)
    return "no runner heartbeat found — the dashboard watches the runner, not the other way around"


# =============================== the dead-man's switch push (design record §6) ===============================
# The ONE push the dashboard owns: the runner cannot announce its own death, so the backend that
# watches its heartbeat does. Every OTHER push stays the runner's (issue #10 boundary). The edge
# detection — fire once per down episode, re-arm on recovery — lives in the pure lib.watchdog.
# Watchdog; here we only turn a newly-down repo into a message and hand the (bounded, never-raising)
# notify.send off the poll thread so a slow notifier can never stall the 2-second snapshot.

def runner_down_push(repo):
    """``(title, body)`` for one repo's RUNNER DOWN push. The title names the offender; the body
    reuses the on-screen sub-line (``_runner_message``) so the phone push and the grey banner say
    the same thing."""
    slug = repo.get("slug") or "the runner"
    return "RUNNER DOWN — %s" % slug, _runner_message([repo])


def runner_down_pushes(snap, watchdog):
    """The ``(title, body)`` pairs to send THIS poll: the watchdog's newly-down repos turned into
    messages. Empty when nothing newly went down (still-down repos already pushed; healthy ones
    never did). Reads the snapshot's ``runner.repos`` — the single source of down-truth the grey
    surface renders from — so surface and push can never disagree."""
    repos = (snap.get("runner") or {}).get("repos", []) if isinstance(snap, dict) else []
    return [(runner_down_push(r), r) for r in watchdog.newly_down(repos)]


def _daemon_spawn(fn):
    """Run ``fn`` on a fire-and-forget daemon thread — the send is bounded and never raises, so a
    hung notifier can't outlive the process or block the request thread that triggered it."""
    threading.Thread(target=fn, daemon=True).start()


def _stderr_log(slug, title, outcome):
    """Default push-outcome sink: one stderr line so a misconfigured channel (or a plain log-only
    fallback) is visible in the server's own output rather than silently swallowed."""
    sys.stderr.write("command-center: RUNNER DOWN push [%s] — %s\n" % (slug, outcome))


def dispatch_runner_pushes(snap, watchdog, config, *, send=None, spawn=None, log=None):
    """Fire exactly one RUNNER DOWN push per down episode and return the slugs pushed this call.
    ``send`` is ``notify.send`` (injectable); ``spawn(fn)`` runs it off the poll thread (a daemon
    thread by default); ``log(slug, title, outcome)`` records the send's outcome string so a
    misconfigured iMessage/cmd — or a bare ``log-only`` — is never silent (the notifier contract is
    that the caller journals the result). Tests pass a synchronous ``spawn`` + recording ``send``/
    ``log`` to observe the once-per-episode contract end-to-end."""
    send = notify_mod.send if send is None else send
    spawn = _daemon_spawn if spawn is None else spawn
    log = _stderr_log if log is None else log
    pushed = []
    for (title, body), repo in runner_down_pushes(snap, watchdog):
        slug = repo.get("slug")
        spawn(lambda t=title, b=body, s=slug: log(s, t, send(config, t, b)))
        pushed.append(slug)
    return pushed


def _sum_shipped(repo_snaps):
    keys = ("landings_total", "landings_window", "go_arounds", "parks")
    total = {k: 0 for k in keys}
    for rs in repo_snaps:
        for k in keys:
            total[k] += rs["shipped"].get(k, 0)
    return total


def _live_cargo(all_flights):
    """The corner counter's "IN FLIGHT" cargo: the summed diff size of flights STILL FLYING. Landed
    flights are excluded — since issue #48 their cargo survives landing (read from the PR), and
    folding a merged flight's +N/−N into a figure labelled "IN FLIGHT" would be a lie (the honesty
    twin of the cargo-survives-landing fix)."""
    added = removed = 0
    present = False
    for f in all_flights:
        if f.get("stage") in _LANDED:
            continue
        c = f.get("cargo") or {}
        if c.get("present"):
            present = True
            added += int(c.get("added", 0))
            removed += int(c.get("removed", 0))
    return {"present": present, "added": added, "removed": removed}


def assemble_snapshot(config, *, now=None, gh_mod=None, usage=None, diff_reader=None, desk=None,
                      concluded=None, version=None, engine=None):
    """Compose the whole snapshot the front-end binds. Reads FRESH local state every call; ``gh_mod``
    and ``usage`` are inputs so GitHub + usage egress ride the slow clock (decision B.2). With
    ``gh_mod=None`` the snapshot is honest but title-less — exactly what a poll produces when GitHub
    is unreachable. ``desk`` (a ``lib.desk.Desk``, ``None`` in read-only embedders) supplies the
    persisted tower watermark that draws the "since you last looked" divider (§4). ``concluded`` (a
    ``ConcludedFlights``, created once per dashboard run) makes a concluded flight's settled GitHub
    facts a once-per-run fetch instead of a forever poll (issue #48); ``None`` keeps the old
    per-poll behavior (embedders/tests that don't wire it). ``version`` (a ``lib.version.Version``,
    created once at boot) carries this server's own code identity so the UI can see it has gone stale
    under a moving checkout (issue #136); ``None`` omits the block entirely. ``engine`` (a
    ``lib.engine.EngineDrift``, created once at boot) carries whether the INSTALLED engine is behind
    the checkout, which every repo's truth strip folds in (issue #166); ``None`` simply leaves the
    strip's engine line silent. Semantics all route through the tested ``lib/`` functions (design
    record B.1)."""
    now = time.time() if now is None else now
    diff_reader = pollers.diff_stat if diff_reader is None else diff_reader
    last_seen = desk.tower_last_seen() if desk is not None else None

    repo_snaps, all_flight_records, all_journal, repo_states = [], [], [], []
    runner_repos = []
    for repo in config.get("repos", []):
        rs, frecs, journal, state = _assemble_repo(repo, config, now, gh_mod, diff_reader,
                                                   last_seen, concluded)
        repo_snaps.append(rs)
        all_flight_records.extend(frecs)
        repo_states.append(state)
        runner_repos.append({"slug": repo["slug"], "down": rs["runner_down"],
                             "heartbeat_age": rs["heartbeat_age"]})
        for rec in journal:
            all_journal.append({"repo": repo.get("name") or repo["slug"], "ts": rec.get("ts"),
                                "num": _rec_num(rec), "raw": json.dumps(rec, separators=(",", ":")),
                                "act": rec.get("act")})

    pill = flights.global_pill(repo_states)
    needs = _needs_you(all_flight_records, now)
    all_flights = [f for f, _, _ in all_flight_records]
    runner_down = any(r["down"] for r in runner_repos)
    pill["message"] = _pill_message(pill, needs, runner_down)

    trouble = {"present": False}
    if pill["level"] != "ok":
        state = pill["state"]
        trouble = {"present": True, "level": pill["level"], "state": state,
                   "offender": pill["offender"],
                   "text": "%s · %s" % (_TROUBLE_TEXT.get(state, state), pill["offender"])}

    # A whole-field GitHub-reachability aggregate (issue #38): gh is one binary/auth for every repo,
    # so unreachable is effectively global. `unreachable` is True the moment ANY repo's read refused;
    # `reachable` is True only when every wired repo answered (None ⇒ no gh wired anywhere → unknown).
    repo_gh = [rs["github"] for rs in repo_snaps]
    gh_unreachable = any(g["unreachable"] for g in repo_gh)
    answered = [g["reachable"] for g in repo_gh if g["reachable"] is not None]
    gh_reachable = all(answered) if answered else None

    # Global firehose: the newest records across ALL repos, in time order. Sorting BEFORE the
    # truncation is load-bearing for multi-repo — otherwise a later repo's older block could push a
    # newer record off the tail (a per-repo append + slice is not a global tail).
    all_journal.sort(key=lambda r: r["ts"] if _finite(r["ts"]) else 0)

    snap = {
        "generated_at": now,
        "clock": _hhmm(now),
        "daypart": flights.daypart(now),   # the living clock (§7) — lighting only, never weather
        "fun": _fun_map(config),
        "operator": config_mod.operator(config),   # the name the approve toast signs with (issue #58)
        "tower_last_seen": last_seen,      # the persisted watermark the divider is drawn against (§4)
        "poll_seconds": config.get("poll_seconds", 2),
        "pill": pill,
        "tower_status": flights.tower_status(pill),
        # Whole-field GitHub reachability (issue #38) — the honest signal the boards/field bind so a
        # dead data link never masquerades as an all-clear. Per-repo truth lives on each repo slice.
        "github": {"reachable": gh_reachable, "unreachable": gh_unreachable},
        "runner": {"down": runner_down, "repos": runner_repos,
                   "message": _runner_message(runner_repos),
                   "down_seconds": config.get("heartbeat_down_seconds", 300)},
        "usage": usage if isinstance(usage, dict) else dict(_UNKNOWN_USAGE),
        "trouble": trouble,
        "needs_you": needs,
        "all_clear": len(needs) == 0,
        "shipped_total": _sum_shipped(repo_snaps),
        "live_cargo": _live_cargo(all_flights),
        "repos": repo_snaps,
        # The whole-field flight list the boring table + Needs You read (they never filter by
        # camera). Each is the full flight object with its boring-mode ``display`` projection attached.
        "flights": all_flights,
        "journal_tail": all_journal[-500:],
    }
    # This server's own code identity (issue #136) — the one block that describes the DASHBOARD
    # rather than the field. Taken here, on the poll, because a stale server can only notice it has
    # gone stale by re-reading disk; the stamp is cached behind a stat signature so an unchanged
    # checkout costs ~40 stats, not a megabyte of reads. Never allowed to fail the whole snapshot:
    # if the identity can't be taken, the field is still the truth the owner came for.
    if version is not None:
        try:
            snap["version"] = version.state()
        except Exception:   # the field is the truth the owner came for; never 500 the poll over a stamp
            pass

    # The engine's publish drift (issue #166) — like `version`, a fact about the TOOLING rather than
    # the field, and global: there is one installed engine behind every watched repo. Measured on its
    # own 30s clock inside EngineDrift, so this costs a dict copy on the 2-second poll.
    engine_state = None
    if engine is not None:
        try:
            engine_state = engine.state()
        except Exception:
            # Never a reason to 500 — but never silence either. `None` here would render as NO engine
            # line, which reads exactly like a live, up-to-date engine: the false all-clear this strip
            # exists to delete, coming back through the error path (caught in review). A wired engine
            # that cannot answer says so out loud; only an UNWIRED one (engine is None) stays quiet.
            engine_state = engine_mod.unmeasurable("the dashboard could not read the engine's state")
    snap["engine"] = engine_state

    # The standing truth strip (issue #166), per repo. Built HERE rather than inside _assemble_repo
    # because it is the one line that folds a GLOBAL fact (engine drift) into a PER-REPO one (that
    # repo's runner tick + data source) — this is the first point where both are known. Each repo
    # gets its own strip: the runner, the heartbeat and the published view are all per-repo, so a
    # single field-wide freshness line would be a lie the moment two repos disagreed.
    for rs in repo_snaps:
        rs["truth"] = truth.banner(rs.get("source"), engine=engine_state, github=rs.get("github"))

    # The same truth, for the view that has no field to hang a strip on (issue #180). Boring mode
    # shows every repo in ONE table, so it needs the whole field's verdict rather than one repo's:
    # worst-of on the level, each repo's own words on its own row. Built from the per-repo strips
    # just composed above — by reference, never re-derived — so the two views cannot disagree.
    snap["truth"] = truth.whole_field(repo_snaps)
    return snap


# =============================== on-demand replay + digest (Task 11 / design record §4) ===============================
# The night replay (a treat) and the morning digest (mechanical) are computed ON DEMAND — behind a
# button, never on the 2-second poll — so they never bloat the snapshot and honor the no-ritual rule
# (§0.2). Both read the SAME fresh journal the snapshot does, then hand it to the tested pure lib
# (replay.build_replay / digest.build_digest); this layer only picks the repo + window and formats
# HH:MM in the server's locale (the same _hhmm the tower log uses, so every surface reads alike).

_DEFAULT_WINDOW_SECONDS = 86400   # a full day back — the default "what did my field do" window


def _repo_by_slug(config, slug):
    """The configured repo entry for ``slug`` (its ``owner/name``), or the FIRST repo when ``slug``
    is absent/unknown — the single-repo MVP default. ``None`` only when no repo is configured."""
    repos = config.get("repos", []) or []
    if slug:
        for r in repos:
            if r.get("slug") == slug:
                return r
    return repos[0] if repos else None


def _num_param(v):
    """A query-string value → a finite number, or ``None`` — a client sends strings, so ``"3600"``
    and a float both parse; junk (``"abc"``, empty, a non-finite) is ``None`` (the caller defaults)."""
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _window_bounds(params, now):
    """``(start, end)`` epoch bounds for a replay/digest window from query params. Explicit
    ``start``/``end`` (epoch seconds) win; else ``range`` is seconds back from ``now`` (or ``all`` for
    the whole journal); absent ⇒ the last day. A window is the honest scope both surfaces share."""
    start = _num_param(params.get("start"))
    end = _num_param(params.get("end"))
    if start is not None or end is not None:
        return start, end
    rng = str(params.get("range") or "").strip().lower()
    if rng == "all":
        return None, None
    secs = _num_param(rng)
    if secs is None or secs <= 0:
        secs = _DEFAULT_WINDOW_SECONDS
    return now - secs, now


def _no_repo(kind):
    empty = {"empty": True, "slug": "", "name": "", "error": "no repo configured"}
    if kind == "replay":
        empty["frames"] = []
        empty["window"] = {"start": None, "end": None, "frames": 0, "truncated": False}
    else:
        empty["clean"] = True
        empty["counts"] = {}
        empty["exceptions"] = []
        empty["events"] = []
        empty["window"] = {"start": None, "end": None, "count": 0}
    return empty


def assemble_replay(config, params, *, now=None, hhmm=None):
    """Compose one repo's night replay for the requested window. ``params`` is the parsed query dict
    (``repo``, ``range``/``start``/``end``); reads a FRESH journal every call. Semantics are the
    tested pure :func:`replay.build_replay` — this only picks the repo and window."""
    now = time.time() if now is None else now
    hhmm = _hhmm if hhmm is None else hhmm
    params = params or {}
    repo = _repo_by_slug(config, params.get("repo"))
    if repo is None:
        return _no_repo("replay")
    start, end = _window_bounds(params, now)
    journal = readers.read_journal(repo["state_home"])
    return replay_mod.build_replay(journal, slug=repo["slug"],
                                   name=repo.get("name") or repo["slug"],
                                   start=start, end=end, hhmm=hhmm)


def assemble_digest(config, params, *, now=None, hhmm=None):
    """Compose one repo's mechanical digest for the requested window (same repo/window selection as
    :func:`assemble_replay`). Semantics are the tested pure :func:`digest.build_digest`."""
    now = time.time() if now is None else now
    hhmm = _hhmm if hhmm is None else hhmm
    params = params or {}
    repo = _repo_by_slug(config, params.get("repo"))
    if repo is None:
        return _no_repo("digest")
    start, end = _window_bounds(params, now)
    journal = readers.read_journal(repo["state_home"])
    return digest_mod.build_digest(journal, slug=repo["slug"],
                                   name=repo.get("name") or repo["slug"],
                                   start=start, end=end, hhmm=hhmm,
                                   operator=config_mod.operator(config))
