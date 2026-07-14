"""The Restart verb — a LOCAL COMMAND execution, the dashboard's SECOND button class (issue #116),
a sibling of Tidy (``lib/tidy.py``).

Every GitHub-write button lives in ``lib/actions.py``. Restart, like Tidy, is deliberately
different: it shells the local ``superlooper`` CLI — here ``superlooper request-restart`` — to ask
the LIVE runner to restart ITSELF in its own cmux tab. The runner honors the request between ticks
by re-exec'ing in place: a fresh process image that reloads the currently-installed engine and
clears in-memory episode state (e.g. a tripped systemic-launch hold). This is "turn the loop off
and on again" as one tap instead of finding the runner's tab, Ctrl-C, and retyping ``superlooper
run``.

The bright line that shapes the whole design: **the button never spawns or places a cmux tab**
(owner ruling, 2026-07-09 — automated tab placement is out). It only asks a runner that is ALREADY
running in its own tab to re-exec there. So:

* **This adapter never raises into a caller.** A missing binary, a timeout, a killed process — all
  become a nonzero rc + empty stdout (mirrors ``lib/tidy._run`` / ``lib/gh._run``), so a tap can
  only ever fail closed.
* **The dead-runner case is an HONEST outcome, not a crash.** No live runner ⇒ the CLI exits
  nonzero but prints a well-formed JSON body (``running: false`` + the one-line manual start). So
  unlike Tidy (which treats any nonzero rc as an error), this adapter parses the CLI's JSON FIRST —
  a refusal is a truthful result the button shows plainly ("no loop running"), never a generic
  error, and never a launch or placement attempt. Only when stdout carries NO parseable object does
  the adapter fall back to an rc-based error (the CLI is missing or truly crashed).
* **A WATCHED repo only.** Every invocation is gated on an allow-list mapping each configured repo
  slug to its checkout path; a stray/forged request for an unwatched repo is refused BEFORE any
  subprocess runs (the same bright line ``Actions`` and ``Tidy`` draw).

The CLI to run is the CONFIGURED path (config's ``superlooper_cli``), but ``SL_SUPERLOOPER`` overrides
it — exactly so ``tests/conftest.py`` can point every test at an absent binary by default and a
restart test can inject the fake in-body. This mirrors ``lib/tidy``'s ``SL_SUPERLOOPER`` precedence.
"""
import json
import os
import subprocess

# Per-call hard timeout (seconds). A module constant, not a literal, so a test can shrink it and
# trip the timeout path (mirrors tidy._DEFAULT_TIMEOUT).
_DEFAULT_TIMEOUT = 30


def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured path
    (config's ``superlooper_cli``), mirroring ``lib/tidy``'s precedence so the entry point and the
    tests agree on binary resolution."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>``; returns ``(rc, stdout, stderr)``. NEVER raises: a timeout, a missing
    binary, or any OSError is caught and returned as a nonzero rc with empty stdout so the caller
    fails closed (mirrors ``tidy._run``)."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


def parse_result(stdout):
    """The single JSON object ``superlooper request-restart --json`` prints, or ``None`` when stdout
    carries no parseable object (a missing/crashed CLI). Pure and unit-tested, so the coupling to the
    CLI's ``--json`` contract is pinned by a test rather than discovered in production."""
    txt = (stdout or "").strip()
    if not txt:
        return None
    try:
        val = json.loads(txt)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _error(rc, stderr, binary):
    """A plain, honest failure message for a CLI that didn't answer — what the UI shows instead of a
    fake success. Names the CLI on a missing binary so the operator knows exactly what to fix."""
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "superlooper request-restart timed out"
    return stderr or ("superlooper request-restart failed (exit %d)" % rc)


class Restart:
    """The Restart verb, bound to the configured superlooper CLI path, an allow-list mapping each
    WATCHED repo slug to its checkout path, and the operator display name it signs the request with
    (issue #58 — recorded in the marker, journaled by the runner). Two methods back the button's
    two-step flow: :meth:`preflight` reports whether a live runner exists and writes NOTHING (the
    dialog decides what to show), :meth:`execute` drops the request (only after the in-UI confirm).
    Every result is honest — ``running``/``ok`` are the real command outcome, never a pretend one."""

    def __init__(self, binary, repo_paths, operator=None, timeout=None):
        self._binary = binary
        self._paths = dict(repo_paths or {})
        self._operator = operator if (isinstance(operator, str) and operator.strip()) else None
        self._timeout = timeout

    def _refuse(self, verb):
        # An unwatched repo is refused BEFORE any subprocess — the command runner only ever targets
        # the checkouts the operator configured (bright line: never steerable off-machine/off-repo).
        return {"ok": False, "verb": verb, "running": None, "error": "unknown repo"}

    def preflight(self, repo):
        """Report whether a live runner exists for ``repo`` — runs ``request-restart --check`` (which
        writes NOTHING). ``running`` is the honest liveness (``None`` if the CLI couldn't answer);
        ``manual`` carries the one-line manual start the dead-runner dialog shows."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("restart-check")
        return self._invoke("restart-check", path, ["--check"])

    def execute(self, repo):
        """Ask the live runner to restart itself — runs ``request-restart`` (drops the marker; the
        in-UI confirm already happened), signed with the operator + a ``command-center`` source. On a
        runner that died since the preflight, the CLI's honest refusal (``running: false`` + the
        manual start) is surfaced as-is — never a launch attempt, never a generic error."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("restart")
        extra = ["--source", "command-center"]
        if self._operator:
            extra += ["--operator", self._operator]
        return self._invoke("restart", path, extra)

    def _invoke(self, verb, path, extra):
        binary = _binary(self._binary)
        rc, out, err = _run(binary, ["request-restart", "--repo", path, "--json", *extra],
                            timeout=self._timeout)
        parsed = parse_result(out)
        if parsed is not None:
            # The CLI answered (even a dead-runner refusal is a well-formed body at rc 1) — surface
            # its honest outcome, normalizing the verb name for the UI trail.
            parsed["verb"] = verb
            return parsed
        # No parseable JSON ⇒ the CLI is missing or crashed: a plain, honest failure (never a false
        # "runner up", never a silent success).
        return {"ok": False, "verb": verb, "running": None, "error": _error(rc, err, binary)}
