"""The window side of Open session window (issue #547): find, open and raise the fleet's viewer.

Owner ruling, 2026-11-06 (quoted verbatim in issue #547): clicking Open session window opens a
terminal window, even when no terminal is attached to the fleet's session. Before #547 the button
moved the session host's focus and stopped there — and the host runs windowless, so nothing appeared unless a viewer happened
to be attached. This module is the half of the fix that has nothing to do with the host: whether a
viewer window is on this screen, opening one, and bringing an attached one forward. ``lib/focus``
composes it with the host's own focus, which goes through the one door (``lib/session_host``) and
nowhere else. Nothing here speaks the host's protocol, opens its socket or runs its binary.

**What it reads, and why that and not the host.** The pinned host cannot say whether a client is
attached: its ``status`` reports the local binary and the server, its session listing reports
running/stopped, and its snapshot carries workspaces, tabs, panes and agents — no clients (read out
of the pinned release's own ``src/cli/status.rs``, ``src/cli.rs`` and ``src/api/schema/session.rs``).
So the answer comes from the OS: a viewer is a process whose command line is EXACTLY the one the
viewer script execs (``fleet.viewer_argv`` — spelled once for both), and it counts as on THIS
screen unless its ancestry shows it is remote — an ssh or mosh session is attached but in no window
the owner here can be shown. The app it runs in is the outermost app bundle above it, when there is
one; when there is not (a terminal whose shells are re-parented away from the app, a viewer inside
tmux) it is still attached — so a tap never opens a second one beside it — it just cannot be raised
by app, and the answer says so.

**What this does not see**, stated rather than discovered: ``ps`` vis-encodes control and non-ASCII
bytes in a command line, so a fleet prefix whose path carries them never matches and every tap would
open a window. The default prefix (``~/.superlooper/fleet``) carries none.

**What it runs.** Three things, each named where it is done:

* ``open <viewer.command>`` — the machine's normal handler for the script, with no app named, so a
  machine that opens ``.command`` files in some other terminal gets that terminal;
* ``open -a <the viewer's app>`` — activation through LaunchServices, which needs no Automation
  (Apple Events) grant. Scripting the terminal app instead would need one, and the grant would go
  to whatever process asked — which on the fleet machine can be the session host whose panes are
  the very workers the fence keeps out of everything;
* two xterm window operations written to the viewer's own terminal device — de-iconify (CSI 1 t)
  and raise (CSI 5 t) — so the viewer's window, not merely its app, comes to the front. That is
  OUTPUT to a terminal emulator, not input to any process: nothing is typed into the viewer, a
  pane or a session.

Every edge is a method on :class:`Screen` and every one has an environment override, the same
shape as ``SL_GH``, so the conftest can point all of them at paths that cannot exist (the "no test
reaches a real external binary" ratchet — here, the owner's real display).
"""
import os
import re
import subprocess
import time
from collections import namedtuple

# One process-table row. `args` is the whole command line exactly as `ps` prints it.
Proc = namedtuple("Proc", "pid ppid tty args")

# An attached viewer: its pid, its controlling terminal, and the app bundle it is running in.
Attached = namedtuple("Attached", "pid tty app")

# What marks an ancestor as a GUI app — an executable inside a bundle's Contents/MacOS/. Not merely
# ".app/": an app's helper binaries also live under Contents/Resources/ (cmux's own CLI does), and
# those are not the app a window belongs to.
_APP_EXEC = "/Contents/MacOS/"

# Ancestors that mean "this viewer is attached from somewhere else, not on this screen".
_REMOTE = ("sshd", "mosh-server")

# How far up the tree an app may be. A terminal app is the viewer's grandparent (app -> login ->
# viewer); the bound is only there so a corrupt table cannot keep the walk going.
_MAX_DEPTH = 32

# A pseudo-terminal's device name as `ps` reports it. Checked before it is joined onto a path: it is
# about to be OPENED FOR WRITING, and "../anything" is not a terminal.
_TTY_RE = re.compile(r"^ttys?[0-9]{1,4}$")

# xterm window operations: de-iconify, then raise to the front of the stacking order.
RAISE_SEQUENCE = b"\x1b[1t\x1b[5t"

# Bounds on the three edges. `open` only asks LaunchServices to start the work, so it answers in a
# fraction of a second when the machine is well; anything longer is a session that cannot show a
# window, and the owner is watching for one.
OPEN_SECONDS = 5
PS_SECONDS = 5

_DEFAULT_PS = "/bin/ps"
_DEFAULT_OPEN = "/usr/bin/open"
_DEFAULT_CONSOLE = "/dev/console"
_DEFAULT_TTY_DIR = "/dev"


def parse_ps(text):
    """``ps -axww -o pid=,ppid=,tty=,args=`` output as :class:`Proc` rows. PURE.

    A line that does not parse is skipped rather than guessed at: a half-read row is not evidence
    that a viewer exists.
    """
    out = []
    for line in (text or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, tty, args = parts
        try:
            out.append(Proc(int(pid), int(ppid), tty, args.strip()))
        except ValueError:
            continue
    return out


def _bundle(args):
    """The app bundle a command line's executable lives in, or ``None``. PURE.

    The OUTERMOST bundle, because a helper nested inside an app (an IDE's ``Code Helper
    (Plugin).app`` under ``Contents/Frameworks/``) is not what to activate — the app it belongs to
    is. A bundle inside a ``.framework`` (``Python.framework/…/Python.app``) is a runtime's stub,
    not a terminal anybody looks at, so it does not count."""
    if not args.startswith("/") or _APP_EXEC not in args:
        return None
    head = args[:args.index(_APP_EXEC)]
    if ".app" not in head:
        return None
    bundle = head[:head.index(".app") + len(".app")]
    if ".framework/" in bundle:
        return None
    return bundle


def _is_remote(args):
    first = args.split(" ", 1)[0]
    return os.path.basename(first).startswith(_REMOTE) or args.startswith(_REMOTE)


def _ancestry(procs, pid):
    """``pid``'s ancestors, nearest first. PURE. Survives a cycle and a missing parent (a process
    table is a snapshot, and a parent can exit between the rows)."""
    by_pid = {p.pid: p for p in procs}
    out, seen = [], set()
    cur = by_pid.get(pid)
    for _ in range(_MAX_DEPTH):
        if cur is None or cur.pid in seen:
            break
        seen.add(cur.pid)
        cur = by_pid.get(cur.ppid)
        if cur is not None and cur.pid not in seen:
            out.append(cur)
    return out


def app_of(procs, pid):
    """The app bundle ``pid`` runs in — the bundle of its nearest app ancestor — or ``None``."""
    for anc in _ancestry(procs, pid):
        bundle = _bundle(anc.args)
        if bundle:
            return bundle
    return None


def is_remote(procs, pid):
    """Is ``pid`` running under an ssh/mosh session — attached, but not on this screen?"""
    return any(_is_remote(anc.args) for anc in _ancestry(procs, pid))


def find(procs, argv, exclude=()):
    """The attached viewer to raise, or ``None``. PURE.

    A viewer is a process whose command line is exactly ``argv`` — so the server (the same binary
    and session plus ``server``) never matches — that is not attached from an ssh/mosh session.
    Its ``app`` is ``None`` when no app bundle sits above it (it is still attached; it just cannot
    be activated by app). Of several, one in an app wins over one in none, then the newest (highest
    pid) — the one the owner most recently opened. ``exclude`` lists pids that do not count, which
    is how "a NEW viewer attached" is asked after an open.
    """
    want = " ".join(str(a) for a in argv)
    best = None
    for p in procs or ():
        if p.args != want or p.pid in exclude or is_remote(procs, p.pid):
            continue
        cand = Attached(p.pid, p.tty, app_of(procs, p.pid))
        if best is None or (cand.app is not None, cand.pid) > (best.app is not None, best.pid):
            best = cand
    return best


def pids(procs, argv):
    """Every pid running exactly ``argv``, attached to a screen or not. PURE."""
    want = " ".join(str(a) for a in argv)
    return {p.pid for p in procs or () if p.args == want}


class Screen:
    """Every edge this module touches: the console, the process table, the opener, the terminal.

    Injected so no test reaches a real display. Each edge resolves through an environment override
    (``SL_CONSOLE`` / ``SL_PS`` / ``SL_OPEN`` / ``SL_TTY_DIR``) that the conftest points at a path
    that cannot exist, so a test that forgot to inject one fails closed rather than popping a window.
    """

    def __init__(self, env=None):
        # An EXPLICIT env (a test's) fails closed for every seam it does not name: a missing key
        # resolves to a path that cannot exist, never to the real binary (fresh-agent review). Only
        # the process environment — production, or the conftest's neutralized one — has defaults.
        self._explicit = env is not None
        self.env = os.environ if env is None else env

    def _get(self, name, default):
        value = self.env.get(name)
        if value:
            return value
        return "/nonexistent/unset-" + name if self._explicit else default

    def console_is_ours(self):
        """Is there a GUI login session for THIS user in front of the machine?

        The console device belongs to whoever is logged in at the screen — root at the login
        window, another user after a fast-user switch — so "this uid owns it" is the question
        "would a window opened now appear in front of the person this loop belongs to". Asked
        before anything is opened: without it, an ssh session or a machine at the login window
        would launch a terminal nobody can see, and the answer would wait out the whole attach
        bound before saying so.
        """
        try:
            return os.stat(self._get("SL_CONSOLE", _DEFAULT_CONSOLE)).st_uid == os.getuid()
        except (OSError, ValueError):
            return False

    def processes(self):
        """The process table, or ``None`` when it could not be read — never ``[]`` for that.

        "No viewer is attached" opens a window; "we could not look" must not, or a broken read is
        exactly how a tap opens a second viewer beside the first."""
        argv = [self._get("SL_PS", _DEFAULT_PS), "-axww", "-o", "pid=,ppid=,tty=,args="]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=PS_SECONDS)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return parse_ps(proc.stdout)

    def _open(self, args):
        argv = [self._get("SL_OPEN", _DEFAULT_OPEN)] + list(args)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=OPEN_SECONDS)
        except subprocess.TimeoutExpired:
            return 124, "the opener did not answer within %ss" % OPEN_SECONDS
        except (OSError, ValueError):
            return 127, "could not run %s" % argv[0]
        return proc.returncode, (proc.stderr or "").strip()

    def open(self, path):
        """Open ``path`` with the machine's normal handler for it — no app named."""
        return self._open([str(path)])

    def activate(self, app):
        """Bring ``app`` to the front. LaunchServices activation, so no Automation grant needed."""
        return self._open(["-a", str(app)])

    def raise_tty(self, tty):
        """Ask the terminal showing ``tty`` to de-iconify and raise its window. ``(ok, why)``.

        Only a pseudo-terminal name is accepted, and only :data:`RAISE_SEQUENCE` is written. The
        device is opened write-only and without becoming anyone's controlling terminal."""
        if not isinstance(tty, str) or not _TTY_RE.match(tty):
            return False, "%r is not a terminal this can address" % (tty,)
        path = os.path.join(self._get("SL_TTY_DIR", _DEFAULT_TTY_DIR), tty)
        try:
            fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError as e:
            return False, "could not open %s (%s)" % (path, e.strerror or e)
        try:
            os.write(fd, RAISE_SEQUENCE)
        except OSError as e:
            return False, "could not write to %s (%s)" % (path, e.strerror or e)
        finally:
            os.close(fd)
        return True, ""

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.monotonic()


def bring_forward(screen, attached):
    """Put an attached viewer's window in front. ``(raised, why)`` — never raises.

    Window first, app second: the window operations order it within its app, and activation then
    brings the app — with that window on top — in front of everything else. Activation is what
    decides the answer; the window sequence is best effort, because an app that ignores it still
    came forward with its own front window.
    """
    ok_tty, why_tty = screen.raise_tty(attached.tty)
    if attached.app is None:
        return False, ("could not tell which app it is running in (no app sits above it — tmux, or "
                       "a terminal that runs its shells apart from the app), so it was not brought "
                       "to the front")
    rc, err = screen.activate(attached.app)
    if rc != 0:
        return False, ("could not bring %s to the front (%s)"
                       % (os.path.basename(attached.app), err or "exit %d" % rc))
    if not ok_tty:
        return True, "its app came to the front, but its window could not be raised (%s)" % why_tty
    return True, ""


def wait_for_new(screen, argv, before, seconds, settle, poll=0.25):
    """Wait up to ``seconds`` for a NEW viewer (a pid not in ``before``) to attach on this screen.

    "Attached" is "seen alive, in an app, for ``settle`` seconds": a client the fence refuses
    exits at once (that is what the pre-#547 script did, every time), so a viewer that is still
    there a beat later has been let in. Returns the :class:`Attached` or ``None``.
    """
    deadline = screen.now() + seconds
    first = None
    seen_at = None
    while True:
        procs = screen.processes()
        got = find(procs, argv, exclude=before) if procs is not None else None
        now = screen.now()
        if got is None:
            first = None
        elif first is None or first.pid != got.pid:
            first, seen_at = got, now
        elif now - seen_at >= settle:
            return got
        # A viewer first seen near the bound is still allowed its settle (fresh-agent review): one
        # that turned up at 9.8s of 10 is a cold terminal launch, not a refusal, and cutting it off
        # would report "never attached" over a working window.
        if now >= (deadline if first is None else max(deadline, seen_at + settle)):
            return None
        screen.sleep(poll)
