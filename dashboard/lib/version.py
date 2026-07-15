"""Code identity + the UI/server skew decision (issue #136) — the dashboard's own publish drift.

**The failure, live on 2026-07-14.** Static assets are read from disk on EVERY request; the Python
server keeps the code it loaded at process start. The loop merged the janitor UI (#121, PR #130) at
12:22Z while the owner's dashboard — a process up since the previous morning — was serving. That
server happily handed the browser the NEW ``janitor.js``, so the page rendered a RAMP SWEEP button;
tapping it POSTed ``/api/janitor/propose`` to a router that had never heard of it, and the dispatch
fell through to a bare ``no such action`` 404 beside a Retry that could never succeed. Every
ingredient was working as designed. The dashboard simply had no way to *notice* it had gone stale —
and it will go stale again every time the loop improves its own face while a dashboard is running.

**The two-stamp split is the whole honesty of this module.** The identity is taken twice, over two
different trees, because they fail differently:

* the **server** stamp — ``lib`` + ``bin``, the Python this process actually loaded at boot. Only a
  change here can add a route, so only a change here can make a button 404. This alone gates the
  notice and the 409.
* the **assets** stamp — ``static``, what the browser loads, re-read from disk every request.

A static-only merge moves the assets stamp and NOT the server stamp: new pixels against the same
router, nothing can break — so the dashboard stays **silent**. That asymmetry is a §0.2 requirement
(no nagging), not an optimization: a single combined stamp would post a notice after every CSS tweak,
and a notice that cries wolf is one the owner learns to ignore before the one time it matters.

**Content-addressed, never mtime-addressed.** A ``touch``, or a checkout that rewrites a file
byte-identically, is not a new build and must not raise a notice. The cheap ``(path, size, mtime)``
stat signature is used ONLY as a cache key — when it is unchanged the tree cannot have changed under
us in any way we'd act on, so ``current()`` skips the re-read; the answer it returns is always a
content hash. That keeps the 2-second poll at ~40 stats instead of ~1MB of file reads.

**The remedy is a command, never a button** — the catch-22 that shapes the whole issue. A stale
server is stale precisely BECAUSE it lacks the newly merged routes, so a "restart the dashboard"
endpoint would 404 on exactly the servers that need it. ``bin/liftoff --restart-dashboard`` is read
fresh from disk on every invocation, so it works no matter how old the running server is.

Nothing here restarts anything. This module only tells the truth; the owner's tap or word starts
every restart (the same posture as tidy/janitor/approve).
"""
import hashlib
import os

# The one from-disk remedy the notice names. A command, not a button — see the module docstring.
REMEDY = "bin/liftoff --restart-dashboard"

# The trees each stamp covers. `server` is the code the PROCESS loaded (only a change here can add a
# route); `assets` is what the BROWSER loads. Split because they fail differently — see the docstring.
SERVER_TREES = ("lib", "bin")
ASSET_TREES = ("static",)

# Indirected so a test can count reads and prove the 2s poll doesn't re-read the tree every tick.
_open = open

_CHUNK = 65536


def _walk(root, trees):
    """Every real file under ``trees``, as sorted ``(relpath, abspath)``. Sorted so the digest is
    deterministic across filesystems (readdir order is not). Missing trees contribute nothing rather
    than raising — a stamp must never be the reason a snapshot 500s."""
    found = []
    for tree in trees:
        base = os.path.join(os.fspath(root), tree)
        for dirpath, dirnames, filenames in os.walk(base):
            # Never let a stray venv/cache masquerade as the build's identity: __pycache__ is a
            # BYPRODUCT of running the code, so hashing it would make the server's own boot move the
            # stamp and manufacture skew out of nothing.
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".venv"))
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.isfile(full):
                    found.append((os.path.relpath(full, os.fspath(root)), full))
    return sorted(found)


def _digest(root, trees):
    """A content hash over ``trees``: each file's RELPATH and its bytes. Path is bound in as well as
    content, so moving a file to a new name is a new build even when the bytes are identical. An
    unreadable file degrades to a marker instead of raising (fail honest, never fail the snapshot)."""
    h = hashlib.sha256()
    for rel, full in _walk(root, trees):
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        try:
            with _open(full, "rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()[:16]   # 16 hex chars — plenty to distinguish builds, short enough to read


def _signature(root, trees):
    """The CHEAP change probe: ``(relpath, size, mtime_ns)`` per file. A cache key ONLY — never the
    stamp itself, because mtime moves when content doesn't (a `touch`, a re-checkout) and that would
    manufacture skew. Costs a stat per file, which is what keeps the 2s poll honest and free."""
    sig = []
    for rel, full in _walk(root, trees):
        try:
            st = os.stat(full)
            sig.append((rel, st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append((rel, -1, -1))
    return tuple(sig)


def fingerprint(root):
    """This checkout's identity as ``{"server": <hex>, "assets": <hex>}`` — the two stamps, taken
    from disk right now. Never raises."""
    return {"server": _digest(root, SERVER_TREES), "assets": _digest(root, ASSET_TREES)}


def skew_message():
    """The ready-made notice line for a stale server (design B.1 — the semantics are computed here,
    the JS only binds them to pixels). Plain and calm: what actually happened, in the field's own
    voice. One line, and no nagging past it (§0.2).

    The SITUATION only — the command lives beside it in ``remedy`` rather than inline here, so the
    UI can set it as a copyable ``<code>`` span and no consumer ever has to parse a command back out
    of a sentence. The 409's :func:`stale_action_message` is the opposite case (one plain string with
    nowhere to put a second field) and spells the remedy inline."""
    return ("this tower booted from an older build — the code on disk has moved on since, so "
            "controls added since it started won't answer.")


def stale_action_message(path, remedy=REMEDY):
    """What an unroutable POST says INSTEAD of ``no such action`` when this server is known-stale.
    ``no such action`` is technically true and practically a lie: it reads as "that button is
    broken" when the truth is "that button is newer than me". Names the control and the remedy."""
    return ("this dashboard is running an older build than the code on disk: it has no route for %s, "
            "which came from a newer build. Nothing was changed. Restart the dashboard: %s"
            % (path, remedy))


class Version:
    """The server's own code identity, captured ONCE at construction (boot) and compared against disk
    on demand.

    Constructed in the composition root (``bin/command-center``) and injected — like ``Actions`` /
    ``Tidy`` / ``Restart`` — so tests drive it with a temp checkout and no surface is on by accident.

    ``boot`` is frozen at construction: it is the identity of the code this PROCESS is running, which
    is exactly the thing disk cannot change. ``current()`` reads disk, cached behind the cheap stat
    signature so a 2-second poll costs stats rather than a megabyte of reads.
    """

    def __init__(self, root, pid=None):
        self._root = os.fspath(root)
        self._pid = os.getpid() if pid is None else pid
        self._sig = None
        self._cached = None
        # Boot identity IS the first reading — taken through current() so the cache is primed in the
        # same safe order (see below) and the first poll costs stats, not a re-read of the tree.
        self.boot = self.current()

    def current(self):
        """The identity of the code on disk NOW. Re-stamped only when the cheap stat signature moves;
        otherwise the tree is byte-for-byte what we last hashed and the cached answer still holds.

        The order below is load-bearing: the signature is read BEFORE the content is hashed. A change
        landing mid-measurement then pairs a STALE signature with a FRESH hash, so the next call sees
        a signature mismatch and re-stamps — one wasted hash. Hashing first and stat-ing after would
        pair a fresh signature with a stale hash and cache it, and the change would be invisible
        forever: a permanently under-reported skew, which is the exact failure this module exists to
        end.
        """
        sig = (_signature(self._root, SERVER_TREES), _signature(self._root, ASSET_TREES))
        if sig == self._sig and self._cached is not None:
            return dict(self._cached)
        self._cached = fingerprint(self._root)
        self._sig = sig
        return dict(self._cached)

    def skew(self):
        """``True`` only when the PYTHON this process loaded no longer matches disk — the one
        condition that can make a freshly-served button hit a route this router doesn't have. A
        static-only change is deliberately NOT skew (see the module docstring)."""
        return self.current()["server"] != self.boot["server"]

    def state(self):
        """The snapshot's ``version`` block: both sides of both stamps, the decision, the ready-made
        notice, the remedy, and this process's pid.

        Both stamps are reported from both sides so the UI (and a test, and a human reading the raw
        JSON) can mechanically tell "this page is newer than this server" rather than take our word
        for it — the squint test: delete the art and the JSON still states the whole situation.

        ``pid`` is here so ``liftoff --restart-dashboard`` can stop EXACTLY this process. It is the
        only safe identification available: it comes from the process that answered our own snapshot
        shape, so it can never name a stranger squatting the port — and never a pattern kill
        (``pkill -f`` collateral-killed William's live dashboard once already, 2026-07-07).
        """
        cur = self.current()
        stale = cur["server"] != self.boot["server"]
        return {
            "server": self.boot["server"],          # the code THIS process is running
            "server_on_disk": cur["server"],         # what a restart would pick up
            "assets": cur["assets"],                 # what the page in the browser was served
            "assets_at_boot": self.boot["assets"],
            "skew": stale,
            "message": skew_message() if stale else None,
            "remedy": REMEDY,
            "pid": self._pid,
        }
