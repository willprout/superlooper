"""The Tidy verb — a LOCAL COMMAND execution, the dashboard's SECOND button class (issue #41 /
design record §2 verb amendment, owner-approved 2026-07-07).

Every other button the dashboard shows writes a GitHub label/comment/issue (``lib/actions.py``).
Tidy is deliberately different: it runs the local ``superlooper tidy`` CLI to close the cmux windows
of FINISHED sessions. This creates a new capability class — executing a machine on this box, not
writing to GitHub — so it is fenced with the same discipline the GitHub egress carries, and one
more besides (the CLI is external and it CLOSES things):

* **This adapter never raises into a caller.** A missing binary, a timeout, a killed process — all
  become a nonzero rc + empty stdout (mirrors ``lib/gh._run``), so a tap can only ever fail closed.
* **Failure surfaces plainly, never a silent success.** Unlike gh (where a failed write just reads
  as ``ok: False``), tidy KEEPS stderr and reports a plain error string — a nonzero exit or a
  missing CLI must be visible in the UI, never mistaken for "nothing needed closing".
* **A WATCHED repo only.** Every invocation is gated on an allow-list mapping each configured repo
  slug to its checkout path; a stray/forged request for an unwatched repo is refused BEFORE any
  subprocess runs (the same bright line ``Actions`` draws for the label writer).
* **Merged-only scope, always.** ``--all`` is NEVER passed — the dashboard deliberately does not
  expose the wider scope in this issue (issue #41 boundary). The CLI's own pure selector
  additionally guarantees a still-building session can never be closed; the button rides on that.

The CLI to run is the CONFIGURED path (config's ``superlooper_cli``, default
``~/.claude/skills/superlooper/bin/superlooper``), but ``SL_SUPERLOOPER`` overrides it — exactly so
``tests/conftest.py`` can point every test at an absent binary by default (the CLI closes real
windows; a test must never reach it) and a tidy test can inject the fake in-body. This mirrors
``lib/gh``'s ``SL_GH`` precedence.

The SEMANTICS — turning the CLI's human list into structured window rows the front-end binds — live
in the pure, unit-tested :func:`parse_windows` / :func:`parse_closed` (design record B.1), so the JS
never parses CLI text and the server stays a thin dispatcher.
"""
import os
import re
import subprocess

# Per-call hard timeout (seconds). A module constant, not a literal, so a test can shrink it and
# trip the timeout path in a fraction of a second (mirrors gh._DEFAULT_TIMEOUT).
_DEFAULT_TIMEOUT = 30


def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured
    path (config's ``superlooper_cli``). The override is the ONE lever the fail-closed test fixture
    pulls — pointing it at an absent path neutralizes this globally — and it mirrors gh's ``SL_GH``
    precedence, so the entry point and the tests agree on binary resolution."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>`` with a HARD timeout. Returns ``(rc, stdout, stderr)``. Never raises:
    a timeout, a missing binary, or any OSError is caught and returned as a nonzero rc with empty
    stdout so the caller fails closed (mirrors ``gh._run``). Unlike gh, stderr is RETURNED, not
    swallowed — a tidy failure must be able to say plainly what went wrong."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


# =============================== the pure parser (semantics — design B.1) ===============================
# `superlooper tidy` prints (bin/superlooper cmd_tidy), for the default merged scope:
#     tidy will close N finished (merged) session window(s):
#       i23   merged        cmux:surface-23           <- f"  {id:5} {status:13} {surface or '(no surface)'}"
#       i16   merged        (no surface)
#       a1    finished      cmux:answerer-1           <- a FINISHED answerer session window (#132)
#     dry-run: nothing closed.                         (--dry-run)   /   closed N window(s).   (--yes)
# or, when nothing is finished:
#     tidy: no finished (merged) session windows to close.

# An indented window row: two leading spaces, the id, the single-token status, then the surface
# (which may itself contain non-space punctuation like `cmux:...`). The id is `iN` (a tracked issue
# session) OR `aN` (a finished answerer session — issue #132; the CLI folds these into the same
# list with a synthetic "finished" status). Header/footer prose never matches (it does not start
# with two-spaces-then-an-id), so it is ignored — an empty list is the honest "nothing finished
# to close" read.
_WINDOW_RE = re.compile(r"^ {2}([ia]\d+)\s+(\S+)\s+(.*\S)\s*$")
_CLOSED_RE = re.compile(r"closed\s+(\d+)\s+window")


def parse_windows(stdout):
    """Parse ``superlooper tidy`` stdout into the window rows it names, each
    ``{"id", "status", "surface"}`` in listed order. The ``(no surface)`` placeholder the CLI prints
    for a window whose surface it couldn't read becomes an empty ``surface`` string. Pure and
    unit-tested against the CLI's real print format, so the coupling to that format is pinned by a
    test rather than discovered in production."""
    windows = []
    for line in (stdout or "").splitlines():
        m = _WINDOW_RE.match(line)
        if not m:
            continue
        surface = m.group(3).strip()
        if surface == "(no surface)":
            surface = ""
        windows.append({"id": m.group(1), "status": m.group(2), "surface": surface})
    return windows


def parse_closed(stdout):
    """The count ``superlooper tidy --yes`` reports it closed (``closed N window(s).``), or ``0``
    when that line is absent (nothing was finished, or an unrecognized format — ``0`` is the safe,
    honest read that never overstates what happened)."""
    m = _CLOSED_RE.search(stdout or "")
    return int(m.group(1)) if m else 0


def _error(rc, stderr, binary):
    """A plain, honest failure message for a nonzero exit — what the UI shows instead of a fake
    success. Names the CLI on a missing binary so the operator knows exactly what to fix."""
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "superlooper tidy timed out"
    return stderr or ("superlooper tidy failed (exit %d)" % rc)


# =============================== the verb executor ===============================

class Tidy:
    """The Tidy verb, bound to the configured superlooper CLI path and an allow-list mapping each
    WATCHED repo slug to its checkout path (the ``--repo`` argument tidy resolves the state home
    from). Two methods back the button's two-step flow: :meth:`dry_run` lists what WOULD close (the
    dialog shows exactly this), :meth:`execute` actually closes it (only after the in-UI confirm).
    Every result is honest — ``ok`` is the real command outcome, never a pretend success."""

    def __init__(self, binary, repo_paths, timeout=None):
        self._binary = binary
        self._paths = dict(repo_paths or {})
        self._timeout = timeout

    def _refuse(self, verb):
        # An unwatched repo is refused BEFORE any subprocess — the command runner only ever targets
        # the checkouts the operator configured (bright line: never steerable off-machine/off-repo).
        return {"ok": False, "verb": verb, "error": "unknown repo"}

    def _args(self, path, *extra):
        # Merged-only, ALWAYS — ``--all`` is deliberately never in this list (issue #41 boundary).
        return ["tidy", "--repo", path, *extra]

    def dry_run(self, repo):
        """List the finished (merged) session windows tidy WOULD close — runs ``tidy --dry-run``
        (closes NOTHING) and returns the parsed window list. ``ok`` is the honest command outcome:
        ``True`` even when zero windows are found (the command ran and found nothing), ``False`` on
        a nonzero exit / missing binary — surfaced with ``error`` and an empty list, never a silent
        empty success that would read as 'nothing to tidy'."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("tidy-dry-run")
        binary = _binary(self._binary)
        rc, out, err = _run(binary, self._args(path, "--dry-run"), timeout=self._timeout)
        if rc != 0:
            return {"ok": False, "verb": "tidy-dry-run", "repo": repo, "windows": [], "count": 0,
                    "error": _error(rc, err, binary), "raw": out}
        windows = parse_windows(out)
        return {"ok": True, "verb": "tidy-dry-run", "repo": repo, "windows": windows,
                "count": len(windows), "raw": out}

    def execute(self, repo):
        """CLOSE the finished (merged) session windows — runs ``tidy --yes`` (skips the CLI's own
        y/N, because the in-UI confirm already happened). Returns the closed count. ``ok`` is the
        honest outcome: ``False`` on a nonzero exit / missing binary, surfaced with ``error`` so a
        failed close is never mistaken for a success."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("tidy")
        binary = _binary(self._binary)
        rc, out, err = _run(binary, self._args(path, "--yes"), timeout=self._timeout)
        if rc != 0:
            return {"ok": False, "verb": "tidy", "repo": repo, "closed": 0,
                    "error": _error(rc, err, binary), "raw": out}
        return {"ok": True, "verb": "tidy", "repo": repo, "closed": parse_closed(out), "raw": out}
