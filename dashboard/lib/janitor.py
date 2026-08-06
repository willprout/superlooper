"""The Janitor verb — the command center's GitHub-side debris sweep (issue #121), the THIRD button
in the LOCAL-COMMAND class the Tidy verb (issue #41) opened.

Owner ruling 2026-07-13: no one should have to use the terminal to use superlooper fully. This is
the janitor's half of that — the owner sees, and taps, every GitHub-side cleanup the CLI's
``superlooper janitor`` would propose without leaving the dashboard. Four classes get a tile of
their own: stale merged/superseded ``sl/*`` branches, open ``superseded`` PRs, aged parked/
needs-owner issues, and — issue #229 — an issue closed as COMPLETED by a bare commit-message
keyword, proposed for REOPEN.

That fourth class was proposed and executed by the CLI for weeks while this adapter's kind ordering
still knew only three, so it was DROPPED from the tiles and yet still COUNTED — and the dialog's
all-clear fired on "no groups to draw". A keyword-closed issue as the only debris therefore read as
"apron's clear" on the owner's primary surface while the terminal said it would reopen an issue:
the same trusted-signal failure #229 exists to stop, one layer up (issue #289). So the rule here is
now the opposite of dropping: **every proposal the CLI names is either shown or counted as
unshowable.** A kind this file has no tile for lands in a trailing catch-all group, named by its raw
key and the CLI's own action word and marked NOT tappable (the dashboard must not offer a tap whose
consequence it cannot state); anything it cannot even name is reported in ``unreadable`` so the
dialog can say so plainly instead of claiming a clean apron.

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

# The debris kinds, in the CLI's own deterministic emission order (branches, then PRs, then issues,
# then reopens), each with the human label the front-end tiles show. The spellings are the ENGINE's,
# read off its emit — note that the reopen's KIND (`issue-reopen`) and its KEY prefix (`reopen:`)
# deliberately differ, so a close and a reopen of the same issue can never conflate.
_KIND_ORDER = ("branch", "pr", "issue", "issue-reopen")
_KIND_LABEL = {
    "branch": "Stale branches",
    "pr": "Superseded PRs",
    "issue": "Aged parked issues",
    "issue-reopen": "Accidentally closed",
}
# The catch-all every OTHER kind lands in. This file never invents a verb for a kind it does not
# know — but it never drops one either (issue #289): the row is shown under its raw key and the
# CLI's own action word, and the group is marked untappable so the dialog offers no consent it
# cannot state the consequence of. The terminal's `superlooper janitor` remains the way to act on
# these. (`metadata`, issue #225's mutually-exclusive repair menus, is exactly such a kind today.)
_OTHER_KIND = "other"
_OTHER_LABEL = "Not renderable here — terminal only"


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
    if kind in ("issue", "issue-reopen"):
        title = p.get("title") or ""
        title = title[:60] if isinstance(title, str) else ""
        verb = "reopen" if kind == "issue-reopen" else "close"
        return "%s issue #%s%s" % (verb, target, (": %s" % title) if title else "")
    # A kind with no tile of its own: name the raw facts the CLI gave us — its action word and its
    # target — rather than inventing a verb, and never fall through to an empty string that would
    # render as a blank row (issue #289).
    action = p.get("action") if isinstance(p.get("action"), str) else ""
    bits = [b for b in (action, "" if target is None else str(target)) if b]
    return " ".join(bits) or str(p.get("key") or "")


def _held_what(key):
    """The plain-language verb a held-back key would run if retried. The CLI reports ``held`` as
    bare keys (``branch:<name>`` / ``pr:<n>`` / ``issue:<n>`` / ``reopen:<n>`` — the identities it
    minted), so unlike :func:`_what` there is no proposal dict to read; the verb comes from the
    key's own kind prefix. This names a CONSEQUENCE, it does not decide one: the CLI still
    re-derives freshly at act time and can skip the key. An unrecognized shape names itself rather
    than inventing a verb."""
    kind, sep, rest = key.partition(":")
    if sep and rest:
        if kind == "branch":
            return "delete branch %s" % rest
        if kind == "pr":
            return "close PR #%s" % rest
        if kind == "issue":
            return "close issue #%s" % rest
        if kind == "reopen":
            return "reopen issue #%s" % rest
    return key


def held_rows(held):
    """The held-back keys as rows the front-end binds: ``[{"key", "what"}, ...]``, in the CLI's own
    order. Issue #131 made each held row a RETRY target, so it must state what tapping it would do —
    derived here (pure, tested) so the JS stays logic-free (design B.1). Wrong-typed/empty entries
    are dropped; an unknown kind is still SHOWN under its raw key (a held action the owner cannot
    see is one he can never clear)."""
    items = held if isinstance(held, list) else []
    return [{"key": k, "what": _held_what(k)} for k in items if isinstance(k, str) and k]


def _row(p):
    return {"key": p["key"], "what": _what(p), "why": p.get("why") or "",
            "target": p.get("target")}


def _actionable(p):
    """A proposal this file can put on a row at all: a dict carrying the string ``key`` that IS its
    identity (what a tap sends back to ``--execute-keys``). Anything else cannot be tapped and
    cannot even name itself — see :func:`unreadable_count`, which counts exactly these."""
    return isinstance(p, dict) and isinstance(p.get("key"), str)


def group_proposals(proposals):
    """Group the CLI's flat, already-sorted proposal list into the tiles the front-end binds:
    ``[{"kind", "label", "tappable", "items": [{"key", "what", "why", "target"}, ...]}, ...]`` —
    the known kinds first, in ``_KIND_ORDER``, then ONE trailing catch-all holding every other kind
    in the CLI's own order.

    An empty group is omitted (no empty tile). ``tappable`` is ``True`` for the known kinds and
    ``False`` for the catch-all: those rows are SHOWN — a proposal the owner cannot see is a
    proposal he can never act on, and a hidden one made the dialog claim a clean apron (issue
    #289) — but never armed, because the dashboard must not offer a tap whose consequence it cannot
    state. A wrong-typed entry, or one with no string ``key``, is not grouped at all; it is counted
    by :func:`unreadable_count` so nothing the CLI proposed goes unmentioned."""
    items = [p for p in (proposals if isinstance(proposals, list) else []) if _actionable(p)]
    groups = []
    for kind in _KIND_ORDER:
        kitems = [_row(p) for p in items if p.get("kind") == kind]
        if kitems:
            groups.append({"kind": kind, "label": _KIND_LABEL[kind], "tappable": True,
                           "items": kitems})
    other = [_row(p) for p in items if p.get("kind") not in _KIND_ORDER]
    if other:
        groups.append({"kind": _OTHER_KIND, "label": _OTHER_LABEL, "tappable": False,
                       "items": other})
    return groups


def unreadable_count(proposals):
    """How many of the CLI's proposals this file could not place on a row at ALL — not a dict, or
    with no string ``key``. They are COUNTED rather than silently dropped so the dialog can say
    plainly that the sweep found more than it can show: with the catch-all group above, every
    proposal is now either shown or in this number, which is exactly what lets the all-clear be
    gated on the honest count instead of on "did we find a tile to draw" (issue #289)."""
    items = proposals if isinstance(proposals, list) else []
    return sum(1 for p in items if not _actionable(p))


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
                    "count": 0, "unreadable": 0, "held": [], "held_items": [],
                    "error": doc.get("error") or _error(rc, err, binary), "raw": out}
        proposals = doc.get("proposals")
        proposals = proposals if isinstance(proposals, list) else []
        held = doc.get("held") or []
        # `held` (the raw keys) is the ORIGINAL contract and stays byte-for-byte; `held_items` is
        # added beside it (issue #131) so the dialog can name what a retry would do — and so the
        # front-end can tell a retry-capable server from an older one still serving only `held`.
        #
        # `count` is every proposal the CLI named and `unreadable` is the part of it no row could
        # carry; the dialog's all-clear is gated on `count`, so a proposal that never reaches a tile
        # can no longer read as "nothing to sweep" (issue #289).
        return {"ok": True, "verb": "janitor-propose", "repo": repo,
                "groups": group_proposals(proposals), "count": len(proposals),
                "unreadable": unreadable_count(proposals),
                "held": held, "held_items": held_rows(held), "raw": out}

    def execute(self, repo, keys, retry=False):
        """Execute EXACTLY the proposal keys the owner tapped — runs ``janitor --json
        --execute-keys k1,k2`` (the CLI re-derives fresh and executes only what is still eligible,
        journaling and holding back failures itself). Returns the per-key outcomes. ``ok`` is the
        honest outcome: ``False`` on a nonzero-envelope / missing binary; a partial failure is
        ``ok: True`` with ``failed > 0`` and the failing keys marked, surfaced so nothing is
        mistaken for a clean sweep. An empty/garbage selection is refused before any subprocess —
        the dashboard never sweeps what the owner did not tap.

        ``retry=True`` (issue #131) adds ``--retry-refused``, the ONE thing that lets a key the CLI
        is holding back from a previous failure run again. It is the owner's separate, explicit
        per-row tap — never part of an ordinary sweep — so it fails closed on anything but a real
        boolean ``True``: a merely-truthy value must not re-run a known-failing GitHub write. The
        holdback itself is unchanged and still the CLI's: without this flag a held key comes back
        ``outcome: held``, exactly as it did before."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("janitor")
        # keys are transported to the CLI comma-joined, so a key containing a comma would round-trip
        # wrong (splitting into two). Tool-minted keys are comma-free by construction; a comma-bearing
        # key is anomalous, so it fails closed (dropped) rather than corrupting the subset.
        keys = ([k for k in keys if isinstance(k, str) and k and "," not in k]
                if isinstance(keys, list) else [])
        if not keys:
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0, "error": "no proposals selected"}
        # A retry is ONE held row's own deliberate tap, so it is refused for any other shape —
        # strict-boolean alone would still let a caller batch a bulk re-run of known-failing writes
        # through the dialog's narrowest consent path (cross-review round 1). Counted on the FILTERED
        # subset: the keys that would really be sent.
        if retry is True and len(keys) != 1:
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0,
                    "error": "a retry runs one held-back action at a time"}
        binary = _binary(self._binary)
        extra = ["--execute-keys", ",".join(keys)]
        if retry is True:
            extra.append("--retry-refused")
        rc, out, err = _run(binary, self._args(path, *extra), timeout=self._timeout)
        doc = parse_execute(out)
        if not doc.get("ok"):
            return {"ok": False, "verb": "janitor", "repo": repo, "results": [], "executed": 0,
                    "failed": 0, "skipped": 0, "held": 0,
                    "error": doc.get("error") or _error(rc, err, binary), "raw": out}
        return {"ok": True, "verb": "janitor", "repo": repo, "results": doc.get("results") or [],
                "executed": doc.get("executed") or 0, "failed": doc.get("failed") or 0,
                "skipped": doc.get("skipped") or 0, "held": doc.get("held") or 0, "raw": out}
