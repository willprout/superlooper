"""The Open-session-window verb (issue #310) — a LOCAL COMMAND execution, and **the dashboard's
ONE doorway to the session host**.

Owner ruling 2026-07-30 (docs/HERDR-ADOPTION-PLAN.md §9): under herdr the dashboard's live-view
ambition is replaced by a BUTTON on the flight card that opens that session's herdr window —
attach, which is proven — and no observe-stream plumbing. This module is that button's mechanism.

**Why the dashboard names the host at all, and why exactly once.** Every other local-command verb
here (Tidy, Restart, Janitor, Fixer) reaches the host THROUGH the engine's CLI, which is right: the
engine owns a single doorway of its own (``lib/session_host.py``, issue #304) so a later host swap
is a bounded rewrite behind an unchanged interface. There is no engine verb for "focus this
session's window" — adding one is an engine change, and this issue is scoped to the dashboard — so
the dashboard opens its own doorway, and holds it to the same rule: **this file is the only place
in the dashboard allowed to name ``herdr``, its verbs, or its addressing.** A swap to another host
is then one file here and one file there, not a hunt.

**Addressing, copied from the engine's rule, not invented.** The host addresses agents by NAME,
never by a cached pane id: a pane moved to another workspace gets a new id and the old one stops
resolving, which is precisely the dragged-anchor incident class. The name is derived from the lane
id (``session_host.name_for``), so the dashboard spells it the same way — ``i<N>`` — and needs no
surface, no pane id, and no state-file plumbing to find a window. That is what makes the ruling's
"no observe-stream plumbing" cheap to honour: the flight number IS the address.

The discipline this verb inherits from its siblings, each pinned by a test:

* **Never raises into a caller.** A missing binary, a timeout, a killed process — all become a
  nonzero rc with empty stdout (mirrors ``lib/tidy._run`` / ``lib/gh._run``), so a tap can only
  ever fail closed.
* **Failure surfaces plainly, never a silent success.** The host's own stderr is KEPT and reported
  (like Tidy, unlike gh) — "the host has no window for this session" is the common answer and it
  must read as itself, never as a shrug.
* **A WATCHED repo only.** Gated on the allow-list mapping each configured repo slug to its
  checkout path; a stray/forged request for an unwatched repo is refused BEFORE any subprocess.
  The path is not passed to the host (agents are host-global, not per-repo) — the allow-list is
  here purely as the bright line every button in this class draws.
* **The target is DERIVED, never received.** :func:`name_for` turns a flight NUMBER into the host
  name, and refuses anything that is not a positive integer. No string from a request body can
  reach the host's argv, so the endpoint cannot be steered into addressing something else.

**The fence (#305) and this button.** The patched host refuses an unauthenticated control-socket
connection, and the token travels in the ENVIRONMENT (``HERDR_API_TOKEN``) — never in argv, because
on macOS a same-uid worker is refused another process's environment but is served its argv. So this
module builds no token handling at all: it runs the host CLI with the environment it already has,
and if the fence refuses, that refusal is surfaced in the host's own words like any other failure.
Whether the dashboard is a token holder is the owner's call (the 2026-07-31 ruling named the runner,
the watchdog and ``d<N>`` sessions) and is filed as a follow-up, not decided here.

The binary to run is the CONFIGURED path (config's ``herdr_cli``, default ``herdr``), but
``SL_HERDR`` overrides it — exactly so ``tests/conftest.py`` can point every test at an absent
binary by default (a real call would reach across into William's own live herd) and a
session-window test can inject the fake in-body. This mirrors ``lib/tidy``'s ``SL_SUPERLOOPER``
precedence, so the entry point and the tests agree on binary resolution.
"""
import os
import subprocess

# Per-call hard timeout (seconds). Shorter than Tidy's: focusing a window is one control-socket
# round trip, so a call that has not answered in this long is a dead host, not a slow one — and
# this one blocks a button the owner just tapped. A module constant, not a literal, so a test can
# shrink it and trip the timeout path in a fraction of a second (mirrors tidy._DEFAULT_TIMEOUT).
_DEFAULT_TIMEOUT = 15


def _binary(configured):
    """The host CLI to run: the ``SL_HERDR`` env override wins over the configured path (config's
    ``herdr_cli``). The override is the ONE lever the fail-closed test fixture pulls — pointing it
    at an absent path neutralizes this globally — and it mirrors tidy's ``SL_SUPERLOOPER``
    precedence."""
    return os.environ.get("SL_HERDR") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>`` with a HARD timeout. Returns ``(rc, stdout, stderr)``. Never raises:
    a timeout, a missing binary, or any OSError is caught and returned as a nonzero rc with empty
    stdout so the caller fails closed (mirrors ``tidy._run``). Unlike gh, stderr is RETURNED, not
    swallowed — the host's refusal must be able to say plainly what it refused."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


def name_for(num):
    """The host's agent name for flight ``num`` (``310`` -> ``"i310"``), or ``None`` when ``num`` is
    not a flight number. PURE.

    Two jobs in one function. The first is agreement: the engine's spawn path names a worker's agent
    after its lane id (``session_host.name_for``), and a dashboard that spelled it differently would
    address nothing. The second is the fence between the network and a subprocess — only a positive
    integer ever becomes a name, so no string a client sends can be handed to the host as a target.
    ``bool`` is screened out explicitly because ``True`` is an ``int`` in Python and would otherwise
    become the perfectly plausible ``i1``.
    """
    if isinstance(num, bool):
        return None
    if isinstance(num, int):
        n = num
    elif isinstance(num, str) and num.strip().isdigit():   # isdigit() excludes signs and spaces
        n = int(num.strip())
    else:
        return None
    return "i%d" % n if n > 0 else None


def _error(rc, stderr, binary, name):
    """A plain, honest failure message — what the UI shows instead of a fake success.

    The host's own words are always KEPT (``agent_not_found: i310`` is the real answer and the
    operator may need to recognise it), but they are FRAMED: this string lands in a toast, on its
    own, seconds after a tap, and a bare ``agent_not_found: i310`` there says nothing about which
    button produced it or what it means. Naming what was asked is the difference between a log line
    and an answer. A missing binary names the binary instead, so the operator knows what to fix.
    """
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the session host at %s — is herdr installed? "
                "(set 'herdr_cli' in config.json)" % binary)
    if rc == 124:
        return "the session host timed out opening the window for %s" % name
    if stderr:
        return "the session host would not open %s's window — %s" % (name, stderr)
    return "the session host would not open the window for %s (exit %d)" % (name, rc)


class SessionWindow:
    """The Open-session-window verb, bound to the configured host CLI path and an allow-list of the
    WATCHED repo slugs. One method — :meth:`open` — because the verb is one tap: it opens a window
    the owner already owns, changing nothing about the loop, so there is no confirm gate to build
    (contrast Tidy/Restart/Janitor, every one of which closes, restarts or deletes something)."""

    def __init__(self, binary, repo_paths, timeout=None):
        self._binary = binary
        self._paths = dict(repo_paths or {})
        self._timeout = timeout

    def open(self, repo, num):
        """Bring THIS flight's session window to the front on the session host.

        Returns an honest result: ``ok`` is the host's real outcome, ``name`` the agent it
        addressed, ``manual`` the one line the owner can run by hand, and ``error`` the host's own
        words when it refused. A host with no window for this flight (a session that never launched,
        or one already closed) is an honest ``ok: false`` — never a pretend success.
        """
        if self._paths.get(repo) is None:
            # An unwatched repo is refused BEFORE any subprocess — the command runner only ever acts
            # for the checkouts the operator configured (bright line: never steerable off-repo).
            return {"ok": False, "verb": "session-window", "error": "unknown repo"}
        name = name_for(num)
        if name is None:
            return {"ok": False, "verb": "session-window", "repo": repo,
                    "error": "%r is not a flight number — nothing to open" % (num,)}
        binary = _binary(self._binary)
        args = ["agent", "focus", name]
        # The manual line names the CONFIGURED binary, not the resolved one: it is what the owner
        # would type, and under test the resolved path is a fake that means nothing to them.
        manual = " ".join([self._binary, *args])
        rc, out, err = _run(binary, args, timeout=self._timeout)
        if rc != 0:
            return {"ok": False, "verb": "session-window", "repo": repo, "num": num, "name": name,
                    "manual": manual, "error": _error(rc, err, binary, name), "raw": out}
        return {"ok": True, "verb": "session-window", "repo": repo, "num": num, "name": name,
                "manual": manual, "raw": (out or "").strip()}
