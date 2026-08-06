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
"""
import os
import re

import panes
import session_host

# The doorway's own words for what it found. Re-exported so a caller (and the CLI) has ONE import.
FOCUSED = session_host.FOCUSED
NO_WINDOW = session_host.NO_WINDOW
HOST_UNREACHABLE = session_host.HOST_UNREACHABLE
# The one outcome the doorway can never produce: it is a question about OUR bookkeeping, asked and
# answered before the host is involved at all.
UNKNOWN_LANE = "unknown_lane"

OUTCOMES = (FOCUSED, NO_WINDOW, HOST_UNREACHABLE, UNKNOWN_LANE)

# A lane id, and the same shape `superlooper resume` enforces on its own argument: i<N> is an issue
# worker, d<N> a debugger seat. Both are lanes the loop records a window for, and focusing is
# read-only — so unlike `tidy` (which CLOSES, and deliberately leaves a debugger seat to the owner)
# there is nothing here that a d<N> lane needs protecting from.
#
# Checked before the id reaches a path join: it is about to select a file under state/panes/, and
# "i339/../../../etc" is not a lane.
LANE_ID_RE = re.compile(r"^[id][0-9]+$")

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
EXIT_CODES = {FOCUSED: 0, NO_WINDOW: 3, HOST_UNREACHABLE: 4, UNKNOWN_LANE: 5}

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
        return {"ok": self.focused, "verb": "focus-session", "id": self.id,
                "outcome": self.outcome, "workspace": self.workspace or None,
                "repo": repo, "detail": self.detail}

    def __repr__(self):
        return "Result(outcome=%r, id=%r, workspace=%r)" % (self.outcome, self.id, self.workspace)


def focus_lane(home, iid, host=None):
    """Bring lane ``iid``'s session window to the front, for the repo whose state home is ``home``.

    Never raises. Every way this can fail — a malformed id, a lane with no recorded window, a host
    that will not answer — is one of the four outcomes above, because the caller is a UI rendering
    the answer rather than a runner deciding on it.

    ``host`` is injected for the tests; production builds the doorway here so that no caller has to
    know one exists.
    """
    iid = (iid or "").strip() if isinstance(iid, str) else ""
    if not LANE_ID_RE.match(iid):
        return Result(UNKNOWN_LANE, iid,
                      detail="%r is not a lane id — expected i<N> (an issue worker) or d<N> "
                             "(a debugger session)" % iid)
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

    got = (host if host is not None
           else session_host.SessionHost(call_seconds=CALL_SECONDS)).focus(session)
    return Result(got.outcome, iid, workspace=session.workspace, detail=got.detail)
