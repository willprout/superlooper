"""The Open-session-window verb (issue #340) — a LOCAL COMMAND execution, a sibling of Tidy
(``lib/tidy``), Restart (``lib/restart``), Janitor (``lib/janitor``) and the Fixer (``lib/fixer``).

Owner ruling 2026-07-30 (§9 of the session-host adoption plan, in ``docs/``): the dashboard's
live-view ambition is
REPLACED by a button on the flight card that opens that session's own window — attach, which is
proven. One tap, one window comes to the front, nothing about the loop changes. No observe stream,
no frame rendering, no pane reads: this module opens a window and does nothing else, and
``tests/test_session_window.py`` pins that as a regression test rather than a comment.

**Why this file names no session host, and holds none.** It was built once before, on the merged
#310 branch, driving the host's own CLI — and removed unshipped. The owner ruled on 2026-08-04 that
the dashboard names no host at all: the capability lives in the ENGINE as ``superlooper
focus-session`` (issue #339), behind the same one doorway every other host-shaped thing goes
through (``skills/superlooper/skill/lib/session_host.py``). Reaching a window from here would take
the host binary, the repo→lane map and — on a fenced machine — a credential for the host's control
socket, which is exactly the second doorway the engine's fence exists to prevent, and why #333 was
closed with "the dashboard is NOT a token holder; the engine verb is the doorway". So this is a
thin shell over one CLI call, exactly like its four siblings, and
``tests/test_host_neutrality.py`` is the ratchet that keeps it one.

**The target is DERIVED, never received.** :func:`lane_id` turns a flight NUMBER into the engine's
``i<N>`` argument and refuses anything else, so no string from a request body can reach a
subprocess argument. That id is the loop's own lane id — the key of the engine's state home, the
same one this dashboard already renders — not an address on any host: the engine resolves it to a
window through THIS repo's recorded marker, which is what keeps one repo's ``i310`` from ever
raising another repo's window.

**All four outcomes are the engine's, and they stay apart.** ``focus-session --json`` answers
``focused`` / ``no_window`` / ``host_unreachable`` / ``unknown_lane``, and the middle one is the
COMMON answer, not a failure: a lane whose session exited, or whose window ``superlooper tidy``
closed, has no window to raise. It is surfaced in the engine's own words — never a silent success,
never flattened into a generic error, and never confused with "the host would not answer" (absence
of signal must not read as "your session is gone"). ``outcome`` is ``None`` for the one case the
engine did not answer at all: a missing CLI, a timeout, or an INSTALLED engine too old to know the
verb (the publish-drift gap ``lib/engine`` exists to name — it exits 2 with no JSON, and reading
that as "unknown lane" would hide the one diagnosis that leads to the fix).

The CLI to run is the CONFIGURED path (config's ``superlooper_cli``), but ``SL_SUPERLOOPER``
overrides it — exactly so ``tests/conftest.py`` can point every test at an absent binary by default
and a session-window test can inject the fake in-body. Same precedence as every sibling, so the
entry point and the tests agree on binary resolution.
"""
import json
import os
import re
import subprocess

# Per-call hard timeout (seconds). The engine grants its own single control call 10s (lib/focus's
# CALL_SECONDS) precisely because a person is watching for their window; this leaves room for that
# plus process start-up, and no more — a wedged host must produce an honest failure while the owner
# is still looking at the screen. A module constant, not a literal, so a test can shrink it and trip
# the timeout path in a fraction of a second (mirrors tidy._DEFAULT_TIMEOUT).
_DEFAULT_TIMEOUT = 20

VERB = "session-window"

# The engine's word for "the host brought that window to the front" — the only outcome that is a
# success. Spelled once here so nothing below has to decide it twice.
FOCUSED = "focused"


def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured path
    (config's ``superlooper_cli``), mirroring ``lib/tidy`` / ``lib/restart`` / ``lib/fixer``'s
    precedence so every local-command button and the tests agree on binary resolution."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>`` with a HARD timeout. Returns ``(rc, stdout, stderr)``. NEVER raises:
    a timeout, a missing binary, or any OSError is caught and returned as a nonzero rc with empty
    stdout so the caller fails closed (mirrors ``tidy._run`` / ``fixer._run``). Unlike gh, stderr is
    RETURNED, not swallowed — a failure to open a window must be able to say plainly what went
    wrong."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


def lane_id(num):
    """The engine's ``--id`` argument for flight ``num`` (``340`` -> ``"i340"``), or ``None`` when
    ``num`` is not a flight number. PURE.

    Two jobs in one function. The first is agreement: ``i<N>`` is the loop's own lane id — the key
    of the engine's state home and the name of the marker under ``state/panes/`` the button is gated
    on — so the id the drawer offers and the id the engine resolves are the same string by
    construction. The second is the fence between the network and a subprocess: only a positive
    integer ever becomes an id, so no string a client sends can be handed to the CLI as a target.

    ``bool`` is screened out explicitly because ``True`` is an ``int`` in Python and would otherwise
    become the perfectly plausible ``i1``. The string test is ``isdecimal``, not ``isdigit``:
    ``"²".isdigit()`` is True but ``int("²")`` RAISES, so the obvious spelling would turn this
    "returns ``None``" contract into an exception thrown out of a request handler — from the one
    function standing between a request body and a subprocess.
    """
    if isinstance(num, bool):
        return None
    if isinstance(num, int):
        n = num
    elif isinstance(num, str) and num.strip().isdecimal():   # excludes signs, spaces and "²"
        n = int(num.strip())
    else:
        return None
    return "i%d" % n if n > 0 else None


# A debugger seat's id, as the engine spells it (``skill/lib/focus.LANE_ID_RE`` is ``^[id][0-9]+$``:
# ``i<N>`` is an issue worker, ``d<N>`` a debugger). Bounded at nine digits — a real counter never
# reaches that, and an unbounded one lets a caller push an arbitrarily long argument at a
# subprocess for nothing.
_DEBUGGER_ID_RE = re.compile(r"^d([0-9]{1,9})$")


def debugger_lane_id(sid):
    """The engine's ``--id`` argument for a FIXER's own seat (``"d4"`` -> ``"d4"``), or ``None`` when
    ``sid`` is not one. PURE.

    The sibling of :func:`lane_id`, and the same fence for the other lane shape. A launched fixer is
    named in the journal as ``d<N>`` — the dashboard does not mint that id and cannot derive it from
    a number, so unlike a flight this one is READ from the published view and handed on. That makes
    the fence the only thing standing between a journal string and argv, so the returned id is
    REBUILT from the parsed integer rather than passed through: whatever the caller sent, what
    reaches the subprocess is a string this function composed.

    ``bool`` is screened out with every other non-string, because there is no debugger seat a
    non-string could plausibly name.
    """
    if not isinstance(sid, str):
        return None
    m = _DEBUGGER_ID_RE.match(sid.strip())
    return "d%d" % int(m.group(1)) if m else None


def parse_result(stdout):
    """The single JSON object ``superlooper focus-session --json`` prints, or ``None`` when stdout
    carries no parseable object (a missing/crashed/too-old CLI). Pure and unit-tested, so the
    coupling to the CLI's ``--json`` contract is pinned by a test rather than discovered in
    production."""
    txt = (stdout or "").strip()
    if not txt:
        return None
    try:
        val = json.loads(txt)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _said(stderr):
    """The last non-empty line of stderr — what the CLI actually complained about, without dragging
    a traceback into a toast."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _unanswered(rc, stderr, binary):
    """The message for a CLI that never gave a readable answer. Distinct from any of the engine's
    four outcomes: nothing is known about the lane's window here, so nothing is claimed about it.

    Exit code 2 gets its own sentence, and it is the most useful one in this module. The runner
    executes the INSTALLED engine copy, so a merged verb is inert until someone republishes through
    ``bin/install.sh`` (the whole subject of ``lib/engine``): an engine that predates
    ``focus-session`` exits 2 on ``invalid choice`` with no JSON at all. Reading that as "unknown
    lane" would hide the one diagnosis that leads to the fix — so it names the remedy instead.
    """
    said = _said(stderr)
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "opening the session window timed out — the superlooper CLI did not answer"
    if rc == 2:
        return ("this machine's installed engine has no `focus-session` verb — re-run "
                "bin/install.sh to publish the merged engine" + ((" (%s)" % said) if said else ""))
    return said or ("superlooper focus-session gave no readable answer (exit %d)" % rc)


class SessionWindow:
    """The Open-session-window verb, bound to the configured superlooper CLI path and an allow-list
    mapping each WATCHED repo slug to its checkout path (the ``--repo`` the engine resolves this
    repo's state home from).

    Two entry points and no confirm gate between them, because the verb is one tap: it opens a
    window the owner already owns and changes nothing (contrast Tidy/Restart/Janitor, every one
    of which closes, restarts or deletes something). :meth:`open` names a FLIGHT, :meth:`open_debugger`
    a fixer's own seat (issue #458); both go through the same :meth:`_focus`, so the engine's four
    outcomes are rendered once."""

    def __init__(self, superlooper_cli, repo_paths, timeout=None):
        self._binary = superlooper_cli
        self._paths = dict(repo_paths or {})
        self._timeout = timeout

    def _answer(self, repo, num, iid, outcome, message, ok=False):
        """One shape for every answer, including the outcomes that have nothing to put in a key.

        The engine holds this same rule on its own side of the wire (``lib/focus.Result.as_dict``)
        and for the same reason: a UI that has to test for a missing key writes a different branch
        per answer, which is how one of the four ends up unhandled."""
        return {"ok": ok, "verb": VERB, "repo": repo, "num": num, "id": iid,
                "outcome": outcome, "message": message, "error": None if ok else message}

    def open(self, repo, num):
        """Bring THIS flight's session window to the front, by shelling the engine's read-only
        ``focus-session`` verb. Changes nothing about the loop, this repo, or that session's work.

        ``ok`` is true for exactly one answer — the host moved its focus to that window. Every other
        answer carries the engine's own words in ``error``/``message`` and its own ``outcome``, so
        the caller can render "that lane has no window" as the ordinary fact it is rather than as a
        failure, and can tell it apart from a host that would not answer at all.
        """
        return self._focus(repo, num, lane_id(num), "SL-%s" % num,
                           "%r is not a flight number — there is no lane to open" % (num,))

    def open_debugger(self, repo, sid):
        """Bring a FIXER's own session window to the front (issue #458) — the same read-only verb,
        aimed at a debugger seat instead of a flight.

        A Deploy Fixer tap that lands puts a real interactive session on the field, and the board now
        names it at the button it was tapped from. Naming it and offering no way in would be half an
        answer, so a launched fixer carries the same affordance a flight card already has. The engine
        has always resolved a ``d<N>`` lane (``lib/focus.LANE_ID_RE``), and focusing is read-only —
        unlike ``tidy``, which CLOSES and deliberately leaves a debugger seat to the owner.

        The id is not derived from a number here (there is none): it is READ from the published
        journal and handed on, so :func:`debugger_lane_id` is the whole fence — see its docstring.
        """
        return self._focus(repo, None, debugger_lane_id(sid), "fixer %s" % sid,
                           "%r is not a fixer id — there is no lane to open" % (sid,))

    def _focus(self, repo, num, iid, label, bad_target):
        """One focus attempt, whichever lane shape asked for it. ``label`` is how the lane is NAMED
        back to the owner ("SL-340", "fixer d4") — the only difference between the two callers, so
        the four outcomes cannot drift into two vocabularies for the same four answers."""
        path = self._paths.get(repo)
        if path is None:
            # An unwatched repo is refused BEFORE any subprocess — the command runner only ever acts
            # for the checkouts the operator configured (the bright line every button here draws).
            return self._answer(repo, num, None, None, "unknown repo")
        if iid is None:
            return self._answer(repo, num, None, None, bad_target)

        binary = _binary(self._binary)
        rc, out, err = _run(binary, ["focus-session", "--repo", path, "--id", iid, "--json"],
                            timeout=self._timeout)
        body = parse_result(out)
        if body is None:
            return self._answer(repo, num, iid, None, _unanswered(rc, err, binary))

        outcome = body.get("outcome")
        detail = body.get("detail")
        detail = detail.strip() if isinstance(detail, str) else ""
        if outcome == FOCUSED:
            # The engine's claim, in the engine's terms: the HOST moved its focus. Not "it is on
            # your screen" — a screen shows it only where a viewer is attached, which is a fact
            # about the machine rather than about the lane, and over-claiming here would send the
            # owner hunting for a window that came up somewhere he is not looking.
            return self._answer(repo, num, iid, FOCUSED,
                                "%s — the session host moved its focus to that window" % label,
                                ok=True)
        if not isinstance(outcome, str) or not outcome.strip():
            # A body without an outcome is a body we cannot read. Say that; never infer one.
            return self._answer(repo, num, iid, None,
                                "superlooper focus-session gave no readable answer for %s" % label)
        # Every other outcome — including one a LATER engine invents — is reported in the engine's
        # own words. ``ok`` is derived from the outcome rather than trusted from the body, so a
        # future ``{"ok": true, "outcome": "no_window"}`` could never toast a success over a window
        # that was never opened.
        return self._answer(repo, num, iid, outcome,
                            detail or ("superlooper focus-session answered %r for %s"
                                       % (outcome, label)))
