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

* the **server** stamp — ``lib/`` + ``bin/command-center``, the Python this process runs. Only a
  change here can add a route, so only a change here can make a button 404. This alone gates the
  notice and the 409.
* the **assets** stamp — ``static``, what the browser loads, re-read from disk every request.

A static-only merge moves the assets stamp and NOT the server stamp: new pixels against the same
router, nothing can break — so the dashboard stays **silent**. That asymmetry is a §0.2 requirement
(no nagging), not an optimization: a single combined stamp would post a notice after every CSS tweak,
and a notice that cries wolf is one the owner learns to ignore before the one time it matters.

**Content-addressed, never mtime-addressed.** A ``touch``, or a checkout that rewrites a file
byte-identically, is not a new build and must not raise a notice. The cheap stat signature is used
ONLY as a cache key — the answer ``current()`` returns is always a content hash. That keeps the
2-second poll at ~40 stats instead of ~1MB of reads, and the key includes ``ctime``/``inode`` so the
cache can only ever err toward re-hashing, never toward missing a change (see :func:`_signature`).

**The remedy is a command, never a button** — the catch-22 that shapes the whole issue. A stale
server is stale precisely BECAUSE it lacks the newly merged routes, so a "restart the dashboard"
endpoint would 404 on exactly the servers that need it. ``bin/liftoff --restart-dashboard`` is read
fresh from disk on every invocation, so it works no matter how old the running server is.

Nothing here restarts anything. This module only tells the truth; the owner's tap or word starts
every restart (the same posture as tidy/janitor/approve).
"""
import hashlib
import os
import threading

# The one from-disk remedy the notice names. A command, not a button — see the module docstring.
REMEDY = "bin/liftoff --restart-dashboard"

# A literal "this is a command-center" marker in the version block. It exists for exactly one
# consumer: `liftoff --restart-dashboard`, which needs to be SURE the pid it is about to signal
# belongs to our dashboard. Inferring that from the snapshot's general shape is not proof — any
# localhost responder carrying `generated_at`/`repos`/a pid could otherwise steer a SIGTERM at a
# process of its choosing. A signal is the one irreversible thing this codebase does to another
# process, so it gets an explicit claim of identity rather than a resemblance.
PRODUCT = "command-center"

# What each stamp covers. `server` is the code the command-center PROCESS runs (only a change here
# can add a route); `assets` is what the BROWSER loads. Split because they fail differently.
#
# The server surface is `lib/` plus ONE entry script — command-center's own. The other bin/ scripts
# (liftoff, install-launchd.sh) are separate processes this server never imports, so a change to them
# cannot make a served control 404; stamping them would post STALE TOWER over an edit that changed
# nothing the browser can reach, which is exactly the nag §0.2 forbids.
#
# `lib/` is stamped WHOLE, deliberately, even though a couple of its modules (liftoff, launchd) are
# likewise only used by other entry points. The two errors are not symmetric: over-including costs at
# most one unnecessary restart, while excluding a module that server.py imports TOMORROW would be a
# silent false negative — the bug this module exists to end, back and quieter. lib/ is the server's
# import namespace, so the safe default is to stamp all of it. `test_version.py` pins the one
# assumption that keeps the exclusions above honest: nothing the server loads imports liftoff.
SERVER_TREES = ("lib",)
SERVER_FILES = ("bin/command-center",)
ASSET_TREES = ("static",)
ASSET_FILES = ()

# Indirected so a test can count reads and prove the 2s poll doesn't re-read the tree every tick.
_open = open

_CHUNK = 65536


def _walk(root, trees, files=()):
    """Every real file under ``trees``, plus each named path in ``files``, as sorted
    ``(relpath, abspath)``. Sorted so the digest is deterministic across filesystems (readdir order
    is not). A missing tree or file contributes nothing rather than raising — a stamp must never be
    the reason a snapshot 500s."""
    found = []
    root = os.fspath(root)
    for tree in trees:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, tree)):
            # Never let a stray venv/cache masquerade as the build's identity: __pycache__ is a
            # BYPRODUCT of running the code, so hashing it would make the server's own boot move the
            # stamp and manufacture skew out of nothing.
            dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".venv"))
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.isfile(full):
                    found.append((os.path.relpath(full, root), full))
    for rel in files:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            found.append((rel, full))
    return sorted(found)


def _digest(root, trees, files=()):
    """A content hash over ``trees``: each file's RELPATH and its bytes. Path is bound in as well as
    content, so moving a file to a new name is a new build even when the bytes are identical. An
    unreadable file degrades to a marker instead of raising (fail honest, never fail the snapshot)."""
    h = hashlib.sha256()
    for rel, full in _walk(root, trees, files):
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


def _signature(root, trees, files=()):
    """The CHEAP change probe: one stat per file. A cache key ONLY — never the stamp itself, because
    mtime moves when content doesn't (a `touch`, a re-checkout) and that would manufacture skew.

    ``st_ctime_ns`` and ``st_ino`` are in the key, and they are what make the cache safe rather than
    merely fast. Size+mtime alone can MISS a real change: ``rsync -t``, ``tar -x``, and any restore
    that preserves timestamps can land different bytes of the same length under the old mtime, and
    the cache would then serve the old hash forever — a permanent, silent "no skew" over a checkout
    that had moved, which is this whole module's worst failure and the original bug wearing a
    disguise. ctime cannot be set from userland (no API backdates it), so ANY content write moves it;
    st_ino catches an atomic replace-by-rename. False positives here are free (one extra hash);
    a false negative is the bug coming back.
    """
    sig = []
    for rel, full in _walk(root, trees, files):
        try:
            st = os.stat(full)
            sig.append((rel, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino))
        except OSError:
            sig.append((rel, -1, -1, -1, -1))
    return tuple(sig)


def fingerprint(root):
    """This checkout's identity as ``{"server": <hex>, "assets": <hex>}`` — the two stamps, taken
    from disk right now. Never raises."""
    return {"server": _digest(root, SERVER_TREES, SERVER_FILES),
            "assets": _digest(root, ASSET_TREES, ASSET_FILES)}


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

    ``no such action`` is technically true and practically a lie: it reads as "that button is broken"
    when the likeliest truth is "that button is newer than me". But the honest replacement must not
    overcorrect into a second lie. This server knows two things — it has no route for ``path``, and
    its own code is older than the disk's — and it does NOT know that ``path`` came from the newer
    build: a typo'd or genuinely nonexistent route reaches this same branch and would be told a
    confident, wrong story about itself.

    So: state the two facts, name the remedy, and say plainly what a persisting failure would mean.
    The owner ends up at the truth either way, and never at a fabricated cause."""
    return ("this dashboard is running an older build than the code on disk, and it has no route for "
            "%s. If that control arrived with the newer build, this is why. Nothing was changed. "
            "Restart the dashboard (%s) and try again — if it still fails, it is a real bug."
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
        # The server is a ThreadingHTTPServer: every request is its own thread, so two overlapping
        # polls (a second tab, a slow snapshot) can be inside current() at once. Without this lock a
        # reader could observe `_sig` already advanced to a new signature while `_cached` still held
        # the previous hash, and report "no skew" against a checkout that had moved. That answer
        # would correct itself on the next poll — but "the dashboard silently under-reports skew for
        # a moment" is a small copy of the exact bug this module exists to end, and the lock costs
        # nothing at two polls a second.
        self._lock = threading.Lock()
        # Boot identity IS the first reading — taken through current() so the cache is primed in the
        # same safe order (see below) and the first poll costs stats, not a re-read of the tree.
        self.boot = self.current()

    def current(self):
        """The identity of the code on disk NOW. Re-stamped only when the cheap stat signature moves;
        otherwise the tree is byte-for-byte what we last hashed and the cached answer still holds.

        The order inside the lock is load-bearing: the signature is read BEFORE the content is
        hashed. A change landing mid-measurement then pairs a STALE signature with a FRESH hash, so
        the next call sees a signature mismatch and re-stamps — one wasted hash, and the truth. Hash
        first and stat after and you cache a FRESH signature against a STALE hash, and the change is
        invisible for as long as the process lives: a permanently under-reported skew, which is
        precisely the failure this module exists to end.
        """
        with self._lock:
            sig = (_signature(self._root, SERVER_TREES, SERVER_FILES),
                   _signature(self._root, ASSET_TREES, ASSET_FILES))
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

        ``pid`` + ``product`` are here so ``liftoff --restart-dashboard`` can stop EXACTLY this
        process. A pid alone is a number anything could print; paired with the ``product`` claim it
        is a specific assertion by the process itself that it is a command-center. That is what makes
        it safe to signal — and it is never a pattern kill (``pkill -f`` collateral-killed William's
        live dashboard once already, 2026-07-07).
        """
        cur = self.current()
        stale = cur["server"] != self.boot["server"]
        return {
            "product": PRODUCT,                      # whose pid this is — see PRODUCT
            "server": self.boot["server"],           # the code THIS process is running
            "server_on_disk": cur["server"],         # what a restart would pick up
            "assets": cur["assets"],                 # what the page in the browser was served
            "assets_at_boot": self.boot["assets"],
            "skew": stale,
            "message": skew_message() if stale else None,
            "remedy": REMEDY,
            "pid": self._pid,
        }
