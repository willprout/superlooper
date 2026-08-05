"""The recorded session handle — ONE writer, ONE reader vocabulary (issue #334).

    state/panes/<id>       the SESSION HOST's pane id for that lane's live session
    state/panes/<id>.ws    the SESSION HOST's workspace id

**What these files mean changed under everyone's feet, and that is why this module exists.** Before
issue #308 they held cmux surface and workspace UUIDs; #308 moved every spawn onto the five-verb
wrapper, so `lib/launch.py` now records what `session_host.Session` came back with — the HOST's
pane and workspace. The format changed; the four call sites that each spelled `os.path.join(state,
"panes", iid)` for themselves did not, and kept handing what they found to cmux. The nudge, the
screen read, the exit-interview wake ping and the whole liveness tier were all addressing a host
handle to a multiplexer that never issued it.

So the point of this module is not tidiness. A handle format with one writer and four
hand-rolled readers has no place to state what it MEANS, and no single test can pin it — which is
exactly how a format change becomes invisible. Everything that touches these two files goes
through here now, and `tests/test_panes.py` pins the round trip from what the launcher writes to
the `session_host.Session` the doorway accepts.

The handle is a CONVENIENCE, never an address. The wrapper addresses agents by NAME (the lane id)
and re-resolves the pane fresh on every read, because a cached pane id stops resolving the moment
the owner rearranges his window — the dragged-anchor incident class, which is precisely what a
recorded handle would re-import. What the recorded workspace is genuinely FOR is teardown: `exit`
and `kill` need the window to close, and after the agent has gone there is nothing left to resolve
it from. The recorded pane rides along so a teardown has something to report; nothing here sends a
prompt or reads a screen through it.
"""
import os
import re

import session_host

DIRNAME = "panes"
WORKSPACE_SUFFIX = ".ws"

# A lane id, and nothing else. The `.ws` sidecar shares the directory, so a lister that matched
# anything would report a phantom lane called `i7.ws` — and `superlooper tidy` would offer to close
# a window that does not exist.
_LANE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class Handle:
    """What is recorded for one lane. Falsy when nothing is — so `if not panes.read(...)` is the
    "this lane has no session window" test every caller already wanted to write."""

    __slots__ = ("pane", "workspace")

    def __init__(self, pane="", workspace=""):
        self.pane, self.workspace = pane or "", workspace or ""

    def __bool__(self):
        return bool(self.pane or self.workspace)

    def __repr__(self):
        return "Handle(pane=%r, workspace=%r)" % (self.pane, self.workspace)

    def as_session(self, iid):
        """The `session_host.Session` the doorway's teardown verbs accept, or None.

        None when there is no WORKSPACE, and that refusal is the whole value of returning a type
        rather than two strings: `exit`/`kill` close a window, a pane id alone cannot name one, and
        "close whatever is at that pane" is how a stale handle ends someone else's session.

        `owned=True` because this file is only ever written for a session THIS loop spawned —
        `lib/launch.py` records it from the handle its own `spawn` returned, and nothing else
        writes here.
        """
        if not self.workspace:
            return None
        return session_host.Session(name=iid, workspace=self.workspace, pane=self.pane or None,
                                    owned=True)


def _dir(state):
    return os.path.join(state, DIRNAME)


def _paths(state, iid):
    return (os.path.join(_dir(state), iid),
            os.path.join(_dir(state), "%s%s" % (iid, WORKSPACE_SUFFIX)))


def record(state, iid, session):
    """THE writer. Both halves, atomically each, from the handle `spawn` returned.

    Returns True only when BOTH landed: a recorded pane whose workspace write failed is the
    un-closable handle `as_session` refuses on, and the caller (the launcher) would rather know.
    """
    pane_path, ws_path = _paths(state, iid)
    ok = write_atomic(pane_path, getattr(session, "pane", "") or "")
    return write_atomic(ws_path, getattr(session, "workspace", "") or "") and ok


def read(state, iid):
    """THE reader. Always a Handle — an absent or unreadable record is an EMPTY one, never a raise:
    every caller here is a sweep or a nudge, and a lane with no window is an ordinary answer."""
    pane_path, ws_path = _paths(state, iid)
    return Handle(_read(pane_path), _read(ws_path))


def forget(state, iid):
    """THE remover — both halves together (D9: no marker outlives its session). Deleting only the
    pane would leave a `.ws` that `recorded_ids` ignores and nothing ever cleans."""
    for path in _paths(state, iid):
        try:
            os.remove(path)
        except OSError:
            pass


def recorded_ids(state):
    """The lanes with a recorded session window. Missing/unreadable directory -> empty set."""
    out = set()
    try:
        names = os.listdir(_dir(state))
    except OSError:
        return out
    for name in names:
        if name.endswith(WORKSPACE_SUFFIX) or not _LANE_RE.match(name):
            continue
        out.add(name)
    return out


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_atomic(path, text):
    """tmp + mv, like every other durable state write in this stack, so a reader can never see a
    half-written handle. (The launcher's own `_write_atomic` is the same function; this module owns
    a copy so `lib/panes` stays importable without pulling in the whole launcher.)"""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
