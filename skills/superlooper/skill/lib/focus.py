"""Bring ONE lane's session window to the front — the engine's read-only door (issue #339).

The owner ruled on 2026-08-04 (issue #310) that a caller must never name the session host: the
dashboard's open-session-window button reaches it through an ENGINE verb instead. This module is
that verb's decision core, and `superlooper focus-session` is its command line.

**Why the engine and not the caller.** Focusing a lane's window needs the host binary, the map from
"repo + i<N>" to the window that lane actually opened, and — on a fenced machine (#305) — a
credential for the host's control socket. A caller holding those IS a second doorway: exactly what
``tests/test_one_session_host_door.py`` exists to prevent, and why #333 was closed with "the
dashboard is NOT a token holder; the engine verb is the doorway". Note what the engine does NOT do
about the third: it hands no token to the host and holds none in this code — ``SessionHost._call``
runs the binary in the engine process's OWN environment, so whether a fenced host serves a given
invocation is a property of who ran the engine, never something a caller can supply.

**Three id shapes, and the flight is one of them (issue #463).** ``i<N>`` an issue worker, ``d<N>``
a debugger seat, ``t<N>`` a triage flight. The flight was left out when #448 shipped it — not by a
decision, but by a character class in :data:`LANE_ID_RE` that nobody revisited — and the effect was
that the one session the owner most wants to watch, the unattended one judging his queue, was the
one session he had no verb to bring forward. Nothing about a flight makes it a worse subject here:
this verb opens nothing, sends nothing and closes nothing.

**How a lane id becomes a window, and why it is not the lane's NAME.** The doorway addresses agents
by name everywhere else, and refuses to here. A lane id is unique inside ONE repo's state home and
nowhere else: on a multi-repo install two adopted repos both have an ``i310``, and the host clears
an agent's name when it exits — so repo A's finished lane and repo B's live one legitimately carry
the same name minutes apart. A name-addressed focus would surface the wrong repo's session, and
would do it silently. So the id is used for exactly one thing: selecting a file under THIS repo's
``state/panes/`` (the same per-lane marker `superlooper tidy` selects on, written by the one writer
in ``lib/panes``). The window is then addressed by the workspace recorded there. The repo boundary
is structural — a lane id from repo A only ever reaches a workspace repo A itself recorded, with no
check standing between the two that somebody has to remember to write.

What that does NOT promise, because a recorded id can go stale (fresh-agent review): that the
workspace is still the one this lane opened. If a marker outlives its window and the host later
mints the same id for somebody else, this addresses a stranger's window — issue #389.
``SessionHost.focus`` states that exposure where it is paid. It is the SAME staleness ``exit`` and
``kill`` already act on, through the same file — but the consequence here is worse than theirs, and
worth saying so plainly: a teardown aimed at a stranger's window closes it, while a focus aimed at
one brings it up in front of an owner whose next move is to type into it.

**Four outcomes, and none of them is an exception.** The caller RENDERS this, so every answer has
to be one it can render honestly:

``focused``            the host brought that window to the front. It is a claim about the HOST's
                       focus, not about a screen — a screen shows it wherever a viewer is attached
``no_window``          that lane has no open window — it exited, or `tidy` closed it. The COMMON
                       answer, not an error, and the one thing this verb must never dress up as a
                       failure
``host_unreachable``   the host could not be reached, or refused in words that say nothing about
                       the window. Absence of signal is never read as "your session is gone"
``unknown_lane``       that repo or that id is not one we know — a malformed id, or a directory
                       with no superlooper config in it

The three host-side words are ``session_host``'s, imported rather than re-spelled: this module and
the doorway must not be able to drift into two vocabularies for the same three answers.

**Two callers now, and both are attended** (issue #459). Besides ``superlooper focus-session`` —
the dashboard's open-session-window button — ``superlooper debug`` calls :func:`focus_lane` itself
once an owner-tapped fixer's spawn confirms, so the terminal the owner asked for comes up in front
of him. That caller renders the outcome exactly as this one does (it journals the word beside the
launch) and branches on none of it: the launch's verdict is the shim's, never this module's. The
watchdog's unattended debugger deliberately does not call here at all.
"""
import contextlib
import fcntl
import os
import re

import fleet
import panes
import session_host
import viewer

# The doorway's own words for what it found. Re-exported so a caller (and the CLI) has ONE import.
FOCUSED = session_host.FOCUSED
NO_WINDOW = session_host.NO_WINDOW
HOST_UNREACHABLE = session_host.HOST_UNREACHABLE
# The one outcome the doorway can never produce: it is a question about OUR bookkeeping, asked and
# answered before the host is involved at all.
UNKNOWN_LANE = "unknown_lane"

# What `show_lane` adds (issue #547): the WINDOW, which the host's focus alone never put on a screen
# — the host runs windowless, so "focused" showed nothing unless a viewer happened to be attached.
# Two successes, and they stay apart because the owner should be told which happened:
OPENED = "opened"                     # no viewer was attached; one was opened, showing the lane
SWITCHED = "switched"                 # a viewer was attached; it now shows the lane, raised
# ...and three ways no window could be put in front of him, each said plainly and none a success:
NO_DISPLAY = "no_display"             # no GUI login session of this user at the screen
OPEN_FAILED = "open_failed"           # the viewer could not be opened (handler, script, a busy tap)
VIEWER_NOT_ATTACHED = "viewer_not_attached"   # it opened, but no viewer attached in time

OUTCOMES = (FOCUSED, NO_WINDOW, HOST_UNREACHABLE, UNKNOWN_LANE,
            OPENED, SWITCHED, NO_DISPLAY, OPEN_FAILED, VIEWER_NOT_ATTACHED)

# The answers that mean "something is in front of you now". FOCUSED is lib/focus's own success and
# stays one for `superlooper debug`, which calls focus_lane and opens no window (#547 boundary).
SUCCESSES = frozenset((FOCUSED, OPENED, SWITCHED))

# A session id, and the same shape `superlooper resume` enforces on its own argument: i<N> is an
# issue worker, d<N> a debugger seat, t<N> a triage flight (#448). All three are sessions the loop
# records a window for, and focusing is read-only — so unlike `tidy` (which CLOSES, and deliberately
# leaves a debugger seat to the owner) there is nothing here that any of them needs protecting from.
#
# THE `t<N>` IS AN ANSWER, NOT AN INHERITANCE (issue #463). #448 made the flight a third class the
# launcher spawns, and `lib/launch.py` records `state/panes/t<N>` for it exactly as it does for a
# lane — so the file this verb selects on is already there, and the only thing standing between an
# owner and the window of the session judging his queue was a two-letter character class nobody had
# revisited. Widening it is deliberate and it is the WHOLE change here: focusing opens nothing,
# sends nothing and closes nothing, so there is no property of a flight that makes it a worse
# subject than a lane. The refusal below names all three, so an id shape the engine does not know
# is still refused by name rather than by silence.
#
# Checked before the id reaches a path join: it is about to select a file under state/panes/, and
# "i339/../../../etc" is not a lane.
LANE_ID_RE = re.compile(r"^[idt][0-9]+$")

# What this verb exits with, one code per outcome, so a SHELL caller can tell the four apart
# without parsing JSON. `no_window` is deliberately not 0 — nothing was focused, and a script that
# read rc=0 would report a window it never brought forward — and just as deliberately not 1: it is
# an ordinary fact about a lane, and a caller that renders every nonzero rc as a failure is exactly
# what this issue asked us not to force. The JSON's `outcome` field remains the field to branch on.
#
# NOTHING USES 2 (fresh-agent review). argparse exits 2 on a usage error, and an INSTALLED engine
# that predates this verb exits 2 on `invalid choice: 'focus-session'` — both with no JSON on
# stdout. A caller reading rc alone would render "this engine has never heard of this command" as
# "that repo or that id is not one we know", which is the one diagnosis that would stop it being
# reported. So the four outcomes start above argparse's range and 2 stays argparse's.
EXIT_CODES = {FOCUSED: 0, NO_WINDOW: 3, HOST_UNREACHABLE: 4, UNKNOWN_LANE: 5,
              OPENED: 0, SWITCHED: 0, NO_DISPLAY: 6, OPEN_FAILED: 7, VIEWER_NOT_ATTACHED: 8}

# How long the one control call may hang. Short on purpose: the caller is a person who tapped a
# button and is watching for their window, and a wedged host must produce an honest
# `host_unreachable` while they are still looking at the screen rather than a spinner that outlives
# their patience. (`tidy` grants 15s per call because it is closing things and a half-torn-down
# lane is worse than a slow one; nothing here is left half-done.)
CALL_SECONDS = 10


class Result:
    """One focus attempt's answer, in the shape the CLI serialises.

    Not a dataclass only because it carries the CLI's exit code as behaviour (`exit_code`), and
    keeping that beside the outcome is what stops a second caller inventing its own mapping.
    """

    __slots__ = ("outcome", "id", "workspace", "detail")

    def __init__(self, outcome, iid="", workspace="", detail=""):
        self.outcome, self.id, self.workspace, self.detail = outcome, iid, workspace, detail

    @property
    def focused(self):
        return self.outcome == FOCUSED

    @property
    def ok(self):
        """Something is in front of the owner: the host's focus, or a viewer opened/switched."""
        return self.outcome in SUCCESSES

    @property
    def exit_code(self):
        return EXIT_CODES.get(self.outcome, 1)

    def as_dict(self, repo=None):
        """The JSON a caller parses. EVERY key is present on every outcome, including the ones a
        given path has nothing to put in them — a UI that has to test for a missing key writes a
        different branch for each answer, which is how one of the four ends up unhandled.

        A NAMED parameter rather than ``**extra`` (second fresh-agent review): a free-form update
        could overwrite ``ok`` or ``outcome`` and re-open the very contradiction ``focused`` was
        made a derived property to close — ``{"ok": true, "outcome": "no_window"}``.
        """
        return {"ok": self.ok, "verb": "focus-session", "id": self.id,
                "outcome": self.outcome, "workspace": self.workspace or None,
                "repo": repo, "detail": self.detail}

    def __repr__(self):
        return "Result(outcome=%r, id=%r, workspace=%r)" % (self.outcome, self.id, self.workspace)


def focus_lane(home, iid, host=None, call_seconds=None):
    """Bring lane ``iid``'s session window to the front, for the repo whose state home is ``home``.

    Never raises, and that is a promise rather than a happy accident — see the ``except`` below.
    Every way this can fail — a malformed id, a lane with no recorded window, a recorded window
    that cannot address anything, a host that will not answer — is one of the four outcomes above,
    because the caller is a UI rendering the answer rather than a runner deciding on it.

    ``host`` is injected for the tests; production builds the doorway here so that no caller has to
    know one exists.

    ``call_seconds`` SHORTENS that doorway's own budget, for callers who have less than
    :data:`CALL_SECONDS` to spend — today ``superlooper debug``, whose tap has already spent most of
    its patience on the launch (issue #459). It exists so that caller does not have to build a
    doorway of its own to say so: the one construction stays here, where the default is documented
    and where a host swap finds it.

    It can only shorten, and the clamp is the enforcement rather than the prose (second fresh-agent
    review): the reason this verb's ceiling is low is written on :data:`CALL_SECONDS` and belongs to
    the verb, not to whoever calls it — a later caller that asked for a minute would be quietly
    re-deciding that a person watching for their window can be left at a spinner.
    """
    iid = (iid or "").strip() if isinstance(iid, str) else ""
    if not LANE_ID_RE.match(iid):
        return Result(UNKNOWN_LANE, iid,
                      detail="%r is not a session id — expected i<N> (an issue worker), d<N> "
                             "(a debugger session) or t<N> (a triage flight)" % iid)
    if not isinstance(home, (str, os.PathLike)) or not str(home).strip():
        # The house "fail-open on wrong-typed input" class, closed here rather than downstream: a
        # None home would join to "None/state" against the process's CWD and answer NO_WINDOW —
        # i.e. report a confident fact about a lane while looking in a directory nobody named.
        return Result(UNKNOWN_LANE, iid,
                      detail="no state home to look in (%r) — this repo's loop state could not be "
                             "located, so nothing can be said about %s's window" % (home, iid))

    handle = panes.read(os.path.join(str(home), "state"), iid)
    session = handle.as_session(iid)
    if session is None:
        # NOT a failure and not an error: the loop records this marker at spawn and removes it when
        # the window is closed, so its absence is the honest answer "there is no window for that
        # lane". Answered WITHOUT asking the host — there is nothing to ask about.
        return Result(NO_WINDOW, iid,
                      detail="no session window is recorded for %s — it has not launched, its "
                             "session ended, or `superlooper tidy` closed the window" % iid)

    door = host if host is not None else session_host.SessionHost(
        call_seconds=CALL_SECONDS if call_seconds is None else min(CALL_SECONDS, call_seconds))
    try:
        got = door.focus(session)
    except ValueError as e:
        # The doorway REFUSES a handle it cannot address — today, one that would read as a
        # command-line flag. That refusal is right where it lives (it is a caller bug, and the
        # doorway raises on those exactly as `send` does for a missing oracle), but "never raises"
        # above is a promise to a UI: a traceback with rc=1 and no JSON is the failure shape this
        # whole verb exists to replace with an answer. So the raise stops HERE and becomes the
        # bookkeeping outcome it actually is — what is broken is the marker this repo recorded,
        # not the host, and not the lane's window.
        return Result(UNKNOWN_LANE, iid, workspace=handle.workspace,
                      detail="the window recorded for %s cannot address anything: %s" % (iid, e))
    return Result(got.outcome, iid, workspace=session.workspace, detail=got.detail)


# ------------------------------------------------------------ the window (issue #547)

# How long a freshly opened viewer gets to attach. A cold terminal launch plus the client's own
# start-up is a couple of seconds on the fleet machine; past this the owner is still looking at an
# empty screen and deserves the answer instead of a longer spinner.
VIEWER_ATTACH_SECONDS = 10

# How long a new viewer must be SEEN ALIVE to count as attached. The fence refuses a tokenless
# client and it exits at once — 0.04s, measured against a private fenced server while building
# this — so a viewer still there a second later has been let in.
VIEWER_SETTLE_SECONDS = 1.0

# Serialises taps. The dashboard serves each request on its own thread, so a double tap is two CLI
# processes, and without this both would see "no viewer attached" and open one each. The second
# waits for the first's open, then finds that viewer and switches it.
VIEWER_LOCK = "viewer.lock"


def _lock_seconds(attach, settle):
    """How long a tap waits for the one ahead of it: that tap's worst case — its focus call, a
    process-table read, the opener, the attach wait and its settle — plus a beat. Past it the
    answer is "another tap is still opening", never a second viewer."""
    return CALL_SECONDS + viewer.PS_SECONDS + viewer.OPEN_SECONDS + attach + settle + 2


@contextlib.contextmanager
def _one_tap_at_a_time(fleet_prefix, screen, seconds):
    """Hold the fleet's viewer lock for the body, waiting up to ``seconds`` for it. Yields whether
    it is held. A prefix that does not exist yet has no viewer script to open either, so it is
    not a reason to refuse the read-only half (finding an attached viewer)."""
    path = os.path.join(str(fleet_prefix), VIEWER_LOCK)
    try:
        handle = open(path, "a")
    except OSError:
        yield True
        return
    try:
        deadline = screen.now() + seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if screen.now() >= deadline:
                    yield False
                    return
                screen.sleep(0.25)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def show_lane(home, iid, fleet_prefix, host=None, screen=None, session=None,
              attach_seconds=None, settle_seconds=None):
    """Put a terminal window showing lane ``iid`` in front of the owner (issue #547).

    The dashboard's Open session window button rides this, through ``superlooper focus-session``.
    It is :func:`focus_lane` and then the window — never instead of it:

    0. **One tap at a time.** The whole tap — the focus included — runs under the fleet's viewer
       lock, so a double tap cannot open two viewers, nor move the focus under a viewer still
       attaching for the other lane.
    1. **The host's focus, through the one door, first.** Anything but ``focused`` — the lane has
       no window, the host would not answer, the id is not ours — is returned exactly as
       :func:`focus_lane` gave it, and NO viewer is opened: there is nothing to show.
       First rather than after, because the measured host lands a client that attaches later on
       the workspace it has focused (a private fenced server, while building this: focus ``w2``,
       attach, and the client draws ``w2``'s pane) — so the window comes up on the lane.
    2. **A GUI login session of this user at the screen**, or ``no_display``. Asked before anything
       is opened: otherwise the answer is a terminal launched where nobody can see it.
    3. **A viewer already attached on this screen** is switched (step 1 did that — the host's focus
       is what every attached client draws) and brought forward: ``switched``, no second window.
       One with no app above it (tmux, a terminal that runs its shells apart from its app) is
       still switched and never joined by a second, and the answer says it could not be raised.
    4. **Otherwise the viewer script is opened** by the machine's normal handler and the answer
       waits, bounded, for a NEW viewer to attach: ``opened``, or ``open_failed`` /
       ``viewer_not_attached`` in words that say which.

    Opening a viewer is the only act this adds. Nothing here spawns, sends to, exits or kills a
    session, or types into a pane; the token is read by the viewer, from the file the server reads.
    ``superlooper debug`` does not come here — it calls :func:`focus_lane` and opens no window.

    Never raises. ``host`` and ``screen`` are injected for the tests; ``session`` names the host
    session the viewer attaches to (the fleet's, unless a caller says otherwise).
    """
    screen = screen if screen is not None else viewer.Screen()
    attach = VIEWER_ATTACH_SECONDS if attach_seconds is None else attach_seconds
    settle = VIEWER_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    argv = fleet.viewer_argv(fleet.host_binary(str(fleet_prefix)), session or fleet.SESSION_NAME)
    script = fleet.viewer_command(str(fleet_prefix))
    asked = iid.strip() if isinstance(iid, str) else ""
    got = None

    def answer(outcome, detail):
        return Result(outcome, got.id if got else asked,
                      workspace=got.workspace if got else "", detail=detail)

    try:
        # The WHOLE tap runs under the lock, the host's focus included (fresh-agent review): were
        # the focus outside it, a second tap's focus could land between this tap's focus and its
        # viewer attaching, and this tap would report its own lane while the window drew another.
        with _one_tap_at_a_time(fleet_prefix, screen, _lock_seconds(attach, settle)) as held:
            if not held:
                return answer(OPEN_FAILED,
                              "another Open session window tap is still opening the viewer; "
                              "nothing was focused or opened beside it — tap again in a moment")
            got = focus_lane(home, iid, host=host)
            if got.outcome != FOCUSED:
                return got
            if not screen.console_is_ours():
                return answer(NO_DISPLAY,
                              "no window can be put in front of you: there is no GUI login session "
                              "for this user at this machine's screen (it is at the login window, "
                              "another user is in front, or this ran over ssh). The session host "
                              "did move its focus to %s, so a viewer you attach later will show it"
                              % got.id)
            procs = screen.processes()
            if procs is None:
                return answer(OPEN_FAILED,
                              "could not read this machine's process table, so whether a viewer "
                              "is already attached cannot be told — nothing was opened, rather "
                              "than risk a second window")
            attached = viewer.find(procs, argv)
            if attached is not None:
                raised, why = viewer.bring_forward(screen, attached)
                if raised and not why:
                    return answer(SWITCHED, "switched the open terminal window to it and brought "
                                            "it to the front")
                return answer(SWITCHED, "switched the open terminal window to it, but %s"
                              % (why if raised else why + " — look for it behind your other "
                                                          "windows"))
            if not os.path.isfile(script):
                return answer(OPEN_FAILED,
                              "there is no viewer script at %s to open — `superlooper fleet "
                              "--install` writes it" % script)
            before = viewer.pids(procs, argv)
            rc, err = screen.open(script)
            if rc != 0:
                return answer(OPEN_FAILED,
                              "this machine could not open %s (exit %d%s)"
                              % (script, rc, ": " + err if err else ""))
            if viewer.wait_for_new(screen, argv, before, attach, settle) is None:
                return answer(VIEWER_NOT_ATTACHED,
                              "%s was opened, but no viewer attached to the fleet within %ss. A "
                              "window may be showing why; the usual cause is a viewer script "
                              "from before #547, which the fenced host refuses — `superlooper "
                              "fleet --install` rewrites it" % (script, attach))
            return answer(OPENED, "opened a terminal window on it")
    except Exception as e:                  # never raises: the caller is a UI rendering an answer
        return answer(OPEN_FAILED, "opening the window failed inside the engine (%s: %s)%s"
                      % (type(e).__name__, e,
                         "; the session host did move its focus to %s" % got.id
                         if got is not None and got.outcome == FOCUSED else ""))
