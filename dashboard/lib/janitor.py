"""The Janitor verb — the command center's GitHub-side debris sweep (issue #121), the THIRD button
in the LOCAL-COMMAND class the Tidy verb (issue #41) opened.

Owner ruling 2026-07-13: no one should have to use the terminal to use superlooper fully. This is
the janitor's half of that — the owner sees, and taps, every GitHub-side cleanup the CLI's
``superlooper janitor`` would propose (stale merged/superseded ``sl/*`` branches, open ``superseded``
PRs, aged parked/needs-owner issues) without leaving the dashboard.

**The dashboard re-derives NONE of the janitor's safety rules.** Exactly like Tidy drives
``superlooper tidy``, this adapter drives ``superlooper janitor`` — the CLI, backed by the engine's
pure ``lib/janitor.py`` selector, remains the single source of truth for every rule (open-PR
branches never proposed, moved tips never proposed, in-flight territory excluded, unproven age
skipped, wrong-typed input fails closed to propose-nothing) AND for execution (the same reconcile →
execute → journal → refused-holdback flow). Two CLI modes make this work:

    superlooper janitor --repo <path> --json                        → the proposal snapshot as JSON
    superlooper janitor --repo <path> --json --execute-keys k1,k2   → executes EXACTLY that subset

So this file adds only two things, both pure and unit-tested (design B.1, so the JS stays
logic-free): parsing the CLI's JSON envelope, and grouping the flat proposal list by kind into the
tiles the front-end binds. It carries the SAME fences Tidy does:

* **Never raises into a caller.** A missing binary, a timeout, a killed process — all become a
  nonzero rc + empty stdout (mirrors ``lib/gh._run`` / ``lib/tidy._run``), so a tap fails closed.
* **Failure surfaces plainly, never a silent success.** stderr is kept and reported; a failed sweep
  must be visible in the UI, never mistaken for "nothing to sweep".
* **A WATCHED repo only.** Every invocation is gated on the allow-list mapping each configured repo
  slug to its checkout path; a stray/forged request for an unwatched repo is refused BEFORE any
  subprocess runs. And execute passes EXACTLY the tapped keys — nothing beyond the owner's taps.

The CLI to run is the CONFIGURED path (config's ``superlooper_cli``), but ``SL_SUPERLOOPER``
overrides it — exactly so ``tests/conftest.py`` can point every test at an absent binary by default
(the CLI writes GitHub; a test must never reach it) and a janitor test injects the fake in-body.
This mirrors ``lib/tidy``'s ``SL_SUPERLOOPER`` precedence.
"""
import json
import os
import subprocess

# Per-call hard timeout (seconds). A module constant so a test can shrink it and trip the timeout
# path in a fraction of a second (mirrors lib/tidy._DEFAULT_TIMEOUT).
_DEFAULT_TIMEOUT = 30

# The debris kinds, in the CLI's own deterministic emission order (branches, then PRs, then issues),
# each with the human label the front-end tiles show. The grouping below never invents a kind — an
# unknown kind is simply dropped (fail closed: an unrenderable proposal is not rendered).
_KIND_ORDER = ("branch", "pr", "issue")
_KIND_LABEL = {
    "branch": "Stale branches",
    "pr": "Superseded PRs",
    "issue": "Aged parked issues",
}


def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured
    path (config's ``superlooper_cli``). The override is the ONE lever the fail-closed test fixture
    pulls, and it mirrors ``lib/tidy._binary`` / gh's ``SL_GH`` precedence."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>``; returns ``(rc, stdout, stderr)``. Never raises: a timeout, a missing
    binary, or any OSError is caught and returned as a nonzero rc with empty stdout so the caller
    fails closed (mirrors ``lib/tidy._run``). stderr is RETURNED so a janitor failure can say plainly
    what went wrong."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except (OSError, ValueError):
        return 127, "", "command not found"


def _error(rc, stderr, binary):
    """A plain, honest failure message for a nonzero exit with no ``ok`` envelope to speak for
    itself — names the CLI on a missing binary so the operator knows exactly what to fix (mirrors
    ``lib/tidy._error``)."""
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "superlooper janitor timed out"
    return stderr or ("superlooper janitor failed (exit %d)" % rc)


# =============================== the pure parsers / grouping (semantics — design B.1) ===============================

def parse_propose(stdout):
    """The ``superlooper janitor --json`` envelope: a dict ``{"ok", "proposals", "held",
    "aged_park_days"}`` — or, on the CLI's own fail-closed refusal, ``{"ok": false, "error": ...}``.
    Anything that isn't a JSON object (empty output from a missing binary, a truncated stream, a
    list) fails closed to ``{"ok": false}`` — the honest read that never fabricates proposals."""
    try:
        doc = json.loads(stdout or "")
    except (ValueError, TypeError):
        return {"ok": False}
    return doc if isinstance(doc, dict) else {"ok": False}


def parse_execute(stdout):
    """The ``--execute-keys`` result envelope: ``{"ok", "results", "executed", "failed", "skipped",
    "held"}``. Same fail-closed read as :func:`parse_propose` — non-object output is ``{"ok":
    false}``, never a pretend success."""
    return parse_propose(stdout)


def _what(p):
    """One proposal, one plain-language verb — what tapping it DOES — mirroring the CLI's
    ``_janitor_line`` so the dashboard and terminal name the same act. Pure string; the JS escapes
    it before it reaches the DOM."""
    kind, target = p.get("kind"), p.get("target")
    if kind == "branch":
        return "delete branch %s" % target
    if kind == "pr":
        head = p.get("head")
        return "close PR #%s%s" % (target, (" (%s)" % head) if head else "")
    if kind == "issue":
        title = p.get("title") or ""
        title = title[:60] if isinstance(title, str) else ""
        return "close issue #%s%s" % (target, (": %s" % title) if title else "")
    return str(p.get("action") or "")


def group_proposals(proposals):
    """Group the CLI's flat, already-sorted proposal list into per-kind tiles the front-end binds:
    ``[{"kind", "label", "items": [{"key", "what", "why", "target"}, ...]}, ...]`` in
    ``_KIND_ORDER``. An empty kind is omitted (no empty tile); a wrong-typed entry, or one with no
    string ``key`` (the identity the execute call sends back), is dropped — fail closed: an
    unrenderable/unactionable proposal is never shown."""
    items = proposals if isinstance(proposals, list) else []
    groups = []
    for kind in _KIND_ORDER:
        kitems = [{"key": p["key"], "what": _what(p), "why": p.get("why") or "",
                   "target": p.get("target")}
                  for p in items
                  if isinstance(p, dict) and p.get("kind") == kind
                  and isinstance(p.get("key"), str)]
        if kitems:
            groups.append({"kind": kind, "label": _KIND_LABEL[kind], "items": kitems})
    return groups


# =============================== the verb executor ===============================

class Janitor:
    """The Janitor verb, bound to the configured superlooper CLI path and an allow-list mapping each
    WATCHED repo slug to its checkout path (the ``--repo`` argument the CLI resolves its state home
    and GitHub target from). Two methods back the surface's two-step flow: :meth:`propose` lists what
    the sweep WOULD do (the dialog shows exactly this, grouped by kind), :meth:`execute` runs the
    subset the owner tapped. Every result is honest — ``ok`` is the real outcome, never a pretend
    success."""

    def __init__(self, binary, repo_paths, timeout=None):
        self._binary = binary
        self._paths = dict(repo_paths or {})
        self._timeout = timeout

    def _refuse(self, verb):
        # An unwatched repo is refused BEFORE any subprocess — the command runner only ever targets
        # the checkouts the operator configured (bright line: never steerable off-machine/off-repo).
        return {"ok": False, "verb": verb, "error": "unknown repo"}

    def _args(self, path, *extra):
        return ["janitor", "--repo", path, "--json", *extra]

    def propose(self, repo):
        """List the GitHub-side debris the sweep WOULD act on — runs ``janitor --json`` (changes
        NOTHING) and returns the proposals grouped by kind plus the held-back keys. ``ok`` is the
        honest outcome: ``True`` even when zero proposals are found (the sweep ran and found
        nothing), ``False`` on a nonzero exit / missing binary / the CLI's own fail-closed refusal —
        surfaced with ``error`` and empty groups, never a silent empty success that would read as
        'nothing to sweep'."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("janitor-propose")
        binary = _binary(self._binary)
        rc, out, err = _run(binary, self._args(path), timeout=self._timeout)
        doc = parse_propose(out)
        if not doc.get("ok"):
            # the CLI speaks for itself when it can (its fail-closed envelope carries an error);
            # otherwise a missing binary / crash gets the rc-derived message.
            return {"ok": False, "verb": "janitor-propose", "repo": repo, "groups": [],
                    "count": 0, "held": [], "error": doc.get("error") or _error(rc, err, binary),
                    "raw": out}
        proposals = doc.get("proposals") or []
        return {"ok": True, "verb": "janitor-propose", "repo": repo,
                "groups": group_proposals(proposals), "count": len(proposals),
                "held": doc.get("held") or [], "raw": out}

    def execute(self, repo, keys):
        """Execute EXACTLY the proposal keys the owner tapped — runs ``janitor --json
        --execute-keys k1,k2`` (the CLI re-derives fresh and executes only what is still eligible,
        journaling and holding back failures itself). Returns the per-key outcomes. ``ok`` is the
        honest outcome: ``False`` on a nonzero-envelope / missing binary; a partial failure is
        ``ok: True`` with ``failed > 0`` and the failing keys marked, surfaced so nothing is
        mistaken for a clean sweep. An empty/garbage selection is refused before any subprocess —
        the dashboard never sweeps what the owner did not tap."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("janitor")
        keys = [k for k in keys if isinstance(k, str) and k] if isinstance(keys, list) else []
        if not keys:
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0, "error": "no proposals selected"}
        binary = _binary(self._binary)
        rc, out, err = _run(binary, self._args(path, "--execute-keys", ",".join(keys)),
                            timeout=self._timeout)
        doc = parse_execute(out)
        if not doc.get("ok"):
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0,
                    "error": doc.get("error") or _error(rc, err, binary), "raw": out}
        return {"ok": True, "verb": "janitor", "repo": repo, "results": doc.get("results") or [],
                "executed": doc.get("executed") or 0, "failed": doc.get("failed") or 0,
                "skipped": doc.get("skipped") or 0, "held": doc.get("held") or 0, "raw": out}
