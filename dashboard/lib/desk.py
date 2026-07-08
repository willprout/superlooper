"""The dashboard's own tiny state file (Task 9 — the "since you last looked" watermark).

The command center is a read-only poller over each repo's loop state (design record §6); the ONE
thing it persists about ITSELF is the tower-log watermark — the timestamp of the newest comms row
William has already seen — so the next poll can draw the "since you last looked" divider (§4). That
lives in the dashboard's OWN file under ``$SL_HOME`` (shareability + testability, decision B.4),
never inside a repo's loop state, which the center must never write.

Two properties are load-bearing, both tested:

* **Forgiving reads.** A missing or corrupt file reads as ``None`` ("never looked") — a first-run
  install and a half-written file both degrade to "no divider", never a crash that would wedge the
  snapshot.
* **Monotonic writes.** ``mark_tower_seen`` only ever ADVANCES the watermark. A stale or racy write
  with an older timestamp is dropped, so already-seen rows can never be resurrected as "new".

This is file I/O only — no external binary, no network — so it is unit-tested against a tmp path
exactly like ``lib/readers`` (the conftest's fail-closed guard doesn't touch it).
"""
import json
import math
import os
import threading
from pathlib import Path

_KEY = "tower_last_seen"


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def default_path():
    """The dashboard's state file: ``<base>/command-center/desk.json``, where ``base`` is
    ``$SL_HOME`` or ``~/.superlooper`` — the same base ``config.state_home`` uses, so the dashboard's
    own state sits beside (never inside) the loop state homes it reads."""
    base = os.environ.get("SL_HOME") or os.path.expanduser("~/.superlooper")
    return Path(base) / "command-center" / "desk.json"


class Desk:
    """The dashboard's own persisted state, keyed by a file path (injected in tests, ``default_path``
    in prod). Only the tower watermark lives here today; the shape is a plain JSON object so more
    dashboard-local facts can join it later without a migration."""

    def __init__(self, path):
        self._path = Path(path)
        # ThreadingHTTPServer serves POSTs concurrently, so the read-compare-write in
        # ``mark_tower_seen`` must be serialized — otherwise an older request could clobber a newer
        # watermark and resurrect already-seen rows as "new".
        self._lock = threading.Lock()

    def _read(self):
        try:
            body = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    def tower_last_seen(self):
        """The persisted watermark, or ``None`` when the file is absent, corrupt, or has never been
        written — every one of which means "no divider, this is a first look"."""
        v = self._read().get(_KEY)
        return v if _finite(v) else None

    def mark_tower_seen(self, ts):
        """Advance the watermark to ``ts`` — but only forward. A non-finite/non-numeric ``ts`` is
        ignored (a corrupt value must never become the watermark); an older ``ts`` than the stored
        one is dropped (monotonic — no rewind). The read-compare-write is held under a lock so
        concurrent POSTs can't interleave into a rewind, and the file is replaced atomically (temp +
        ``os.replace``) so a concurrent reader never sees a half-written file. Creates the parent
        directory on first write."""
        if not _finite(ts):
            return
        with self._lock:
            current = self.tower_last_seen()
            if current is not None and ts <= current:
                return
            body = self._read()
            body[_KEY] = ts
            # A failed write must never crash the endpoint — the divider is a nicety, never load-bearing.
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(json.dumps(body))
                os.replace(str(tmp), str(self._path))   # atomic — a reader sees old or new, never torn
            except OSError:
                pass
