"""Engine publish drift (issue #166) — is the loop RUNNING the fixes that were merged?

**The gap this names.** The runner executes the INSTALLED engine copy at ``~/.claude/skills/superlooper``,
never a checkout. A merged engine change is therefore INERT until someone republishes it through the
gated ``bin/install.sh`` — William's own touch, by design: that gate is the fence that makes
``skills/**`` a supervised bright line, and it is not up for negotiation. The gate is RIGHT. The
SILENCE around it was not. A fix could sit merged for days while the loop kept running the old code
and nothing on the owner's screen said so — he would watch a bug he had already fixed keep
happening, unable to tell "not fixed yet" from "fixed, but not switched on".

This module makes that gap VISIBLE and does nothing else. It never publishes, never runs the
installer, never fetches, never touches the installed copy: it counts, and it tells.

**"Merged" means ``origin/main``, and that is why the count is taken against it.** The obvious
baseline is the checkout's HEAD, since HEAD is what install.sh publishes — and it is a trap. THIS
repo's local checkout lags ``origin/main`` *by design*: the loop merges its PRs to origin, so a
freshly merged engine fix is on ``origin/main`` and NOT in the working checkout until someone pulls.
Count against HEAD and that fix reads ``behind: 0`` → no message → an empty engine line, which is
pixel-identical to a live engine. The banner built to end confident-while-blind would go silent
about precisely the fix it exists to name. So the question asked here is the owner's real one —
what is merged and not live? — and the remedy NAMES the pull when the checkout is the thing in the
way. ``origin/main`` is read from the local remote-tracking ref: no network, no fetch, no egress.

**It asks install.sh's question, not a lookalike.** The installer's gate diffs CONTENT
(``git diff --name-status <last_sha> <target> -- <payload>``); a bare commit count is a different
question that disagrees with it in both directions. A payload commit and its revert leave the
content identical while the count says "2 fixes waiting" — sending the owner to a remedy that
answers "payload is unchanged", which is a nag AND a small lie. And an installed engine AHEAD of the
checkout has no commits between, so a count reads 0 while the installer would report real changes.
So content decides first, and the commit count only ever NAMES a gap the content already proved.

**A DIRTY payload cannot be compared at all.** install.sh does not publish a commit — it ``rsync``s
the WORKING TREE and then stamps HEAD's sha (bin/install.sh §2–3). While the tree is dirty the stamp
names a commit whose content was never what got copied, so no count describes anything that was, or
would be, published: publishing a dirty tree stamps HEAD and reads 0 ("all live") though live code
exists in no commit, and committing afterwards reads 1 ("not live") though it went live before the
commit. Unknowable from the stamp ⇒ reported UNKNOWN, never a confident number in either direction.

**Unknown is never zero, and never silence.** Every failure — no in-history baseline, a ``nogit``
tarball stamp, an unreadable stamp, a git that errors, a measurement that raises — reports
``known: False`` with a MESSAGE. Returning a silent unknown would render as no engine line, which is
indistinguishable from a live engine: the false all-clear this module exists to prevent, sneaking
back through the error path. "I broke" and "nothing to report" must never look alike.

**Silence is reserved for the two cases that genuinely have nothing to say**: no engine source among
the watched checkouts (a friend who adopted superlooper for their own repo has no monorepo to
compare against), and no ``VERSION`` at the install dir (nothing was ever published through
install.sh here). Those are the honest empty answer — and a line that could never clear would be
exactly the nag §0.2 forbids.
"""
import errno
import os
import threading

import pollers

# The publishable payload, repo-relative. This MUST mirror bin/install.sh's own PAYLOAD_REL: the
# installer and this counter have to scope the same tree, or the number on screen answers a
# different question than the remedy performs. tests/test_engine.py pins the two together.
PAYLOAD_REL = "skills/superlooper/skill"

# The one remedy this names: the GATED installer at the repo root — which, since issue #197, is the
# only script in the repo that writes into ~/.claude/skills at all (the engine's standalone-era
# nested copy at skills/superlooper/bin/install.sh is a refusing tombstone, and
# skills/superlooper/tests/test_one_publish_door.py fails if a second door ever appears). A banner
# that sent the owner around his own fence would be the opposite of this module's job, so there is
# deliberately no other path to name here.
REMEDY = "bin/install.sh"

VERSION_FILE = "VERSION"

# install.sh's own sentinel: it stamps `nogit` when publishing from a tree with no git (a released
# tarball). There is no commit behind it, so there is no baseline and no honest number.
NOGIT = "nogit"

# What "merged" means, best ref first. `origin/main` is the truth (the loop merges there); local
# `main` is the fallback for a checkout with no remote — e.g. every test fixture. Both are read from
# refs already on disk; nothing here fetches.
MERGED_REFS = ("origin/main", "main")

# Drift moves only when someone MERGES or REPUBLISHES — both minutes-scale, human-paced events. The
# snapshot is built every 2 seconds; shelling git twice a second for an answer that changes twice a
# day is waste the poll loop should never pay.
DRIFT_POLL_SECONDS = 30

# The default install dir — install.sh's $DEST. Named so the CLI-derived guess can fall back to it
# (see install_dir): the documented way to invoke the CLI is a bare `superlooper` on PATH, which is
# a shim in ~/.local/bin, and deriving $DEST from THAT would silently point at ~/.local forever.
DEFAULT_INSTALL_DIR = "~/.claude/skills/superlooper"

# A present-but-unreadable VERSION: a failure, NOT an absence. Absent means "never published here"
# (silent, honest); unreadable means "something is wrong and I cannot tell you the truth" (speaks).
UNREADABLE = object()

# Indirected through the module global so a test can drive the failure paths (a git that errors)
# without a real broken checkout. Reused from pollers rather than re-implemented: one read-only git
# wrapper that can never raise into the poll loop, not two that can drift apart.
_git = pollers.git_run

_UNKNOWN = {"known": False, "behind": None, "installed_sha": None, "installed_at": None,
            "source": None, "message": None, "remedy": REMEDY}


def _has_version(d):
    return bool(d) and os.path.isfile(os.path.join(d, VERSION_FILE))


def install_dir(superlooper_cli):
    """The installer's ``$DEST`` — the live engine copy — derived from the ``superlooper_cli`` path
    the dashboard's config ALREADY carries, with the default install as a backstop.

    install.sh publishes the payload to ``$DEST`` and the CLI to ``$DEST/bin/superlooper``, so the
    parent of the CLI's ``bin/`` *is* ``$DEST``. That is why this needs no new config key: an
    operator with a non-standard install already points ``superlooper_cli`` at it.

    The backstop exists because that derivation has one real trap (raised in review): install.sh ALSO
    drops a ``superlooper`` shim on PATH (``~/.local/bin``), which is how every doc invokes it. An
    operator who points the config there would derive ``$DEST = ~/.local``, find no VERSION, and get
    a permanently SILENT engine line — a config typo quietly disabling the honesty surface. So when
    the derived dir holds no VERSION but the default install does, prefer the default: the worst case
    is naming the standard install on a machine that has one, and the alternative is silence.
    """
    default = os.path.expanduser(DEFAULT_INSTALL_DIR)
    if not isinstance(superlooper_cli, str) or not superlooper_cli.strip():
        return default if _has_version(default) else None
    cli = os.path.abspath(os.path.expanduser(superlooper_cli.strip()))
    derived = os.path.dirname(os.path.dirname(cli)) or None
    if derived and _has_version(derived):
        return derived
    if _has_version(default):
        return default
    return derived


def installed_stamp(dest):
    """``{"sha", "at"}`` from ``$DEST/VERSION`` — the build the loop is actually RUNNING.

    Three outcomes, and the distinction between the last two is the point:
      * ``None``       — no VERSION: nothing was ever published here (honest absence ⇒ silent)
      * ``UNREADABLE`` — present but unreadable or empty: a FAILURE (⇒ speaks). Mapping this to
        ``None`` would let a permission error or a half-written stamp render as an all-clear.
      * a dict         — the stamp install.sh wrote (``"<short-sha> <YYYY-MM-DD>"``)

    Only the first line is read: the file is the installer's one-line receipt.
    """
    if not dest:
        return None
    path = os.path.join(os.fspath(dest), VERSION_FILE)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError as e:
        # Absent is an answer; anything else (permission, EISDIR, I/O) is a failure that must speak.
        return None if e.errno == errno.ENOENT else UNREADABLE
    parts = first.split()
    if not parts:
        return UNREADABLE          # install.sh always writes content; empty means something broke
    return {"sha": parts[0], "at": parts[1] if len(parts) > 1 else None}


def source_repo(paths):
    """The watched checkout that carries the engine payload — the superlooper monorepo — or ``None``
    when none does.

    Derived from the repo list config already declares (never a filesystem hunt): the engine has
    exactly one source tree, and a checkout either contains ``skills/superlooper/skill`` or it does
    not. ``None`` is the correct, quiet answer for a dashboard watching only repos the loop BUILDS
    rather than the repo the loop IS.
    """
    for p in paths or []:
        if isinstance(p, str) and p.strip() \
                and os.path.isdir(os.path.join(os.path.expanduser(p), PAYLOAD_REL)):
            return p
    return None


def drift_message(behind, merged=True, needs_pull=False):
    """The DoD's own sentence, in the owner's terms: what is waiting, and the one thing that would
    switch it on. Pluralized because a banner that says "1 engine fixes" is one he trusts a little
    less, and this surface's whole currency is trust.

    ``needs_pull`` is load-bearing honesty, not a flourish: when the merged work is not in the
    checkout yet, ``bin/install.sh`` alone would publish the OLD code and the owner would watch the
    banner refuse to clear. Naming the pull is the difference between a remedy and a wild goose
    chase. ``merged`` narrows the claim for the exotic checkout with no ``main``/``origin/main`` at
    all, where the counted commits cannot be proven to be merged work.
    """
    what = "engine fix" if behind == 1 else "engine fixes"
    switch = "it" if behind == 1 else "them"
    state = "merged but not yet live" if merged else "in this checkout are not yet live"
    how = "pull, then re-run the installer" if needs_pull else "re-run the installer"
    return "%d %s %s; %s to switch %s on" % (behind, what, state, how, switch)


def _cant_tell(reason):
    """The honest non-answer. It must never read as an all-clear, and must never invent a number."""
    return "can't tell which engine build is live — %s" % reason


def unmeasurable(reason="the drift measurement itself failed"):
    """An explicit UNKNOWN engine block for when the measurement BLEW UP rather than answered.

    Its whole reason to exist is that ``message`` is set. A failure returning the plain silent
    ``_UNKNOWN`` would render as no engine line — indistinguishable from a live, up-to-date engine —
    which is the false all-clear this module exists to prevent, reintroduced through the error path.
    """
    out = dict(_UNKNOWN)
    out["message"] = _cant_tell(reason)
    return out


def _merged_ref(source_path):
    """``(ref, is_merged_work)`` — what "merged" means in this checkout.

    ``origin/main`` when the remote-tracking ref exists (the truth: the loop merges there), else
    local ``main``, else ``HEAD`` with the claim narrowed. Read from refs on disk — no fetch.
    """
    for ref in MERGED_REFS:
        rc, _ = _git(source_path, "rev-parse", "--verify", "--quiet", "%s^{commit}" % ref)
        if rc == 0:
            return ref, True
    return "HEAD", False


def _payload_dirty(source_path):
    """Does the checkout's payload differ from its own HEAD — uncommitted edits or untracked files?
    ``True``/``False``, or ``None`` when git could not say.

    This is what makes the ``VERSION`` stamp trustworthy or not: install.sh rsyncs the WORKING TREE
    and stamps HEAD, so the stamp identifies the published CONTENT only while the tree is clean.
    ``--porcelain`` reports modified, staged and untracked files under the path.
    """
    rc, out = _git(source_path, "status", "--porcelain", "--", PAYLOAD_REL)
    if rc != 0:
        return None
    return bool(out.strip())


def _payload_same(source_path, sha, ref):
    """Is the payload's CONTENT identical between ``sha`` and ``ref``? ``True``/``False``/``None``.

    install.sh's own gate question (it diffs content, not commits). Asking it the same way is what
    keeps the banner's number and the remedy's behaviour from contradicting each other — a commit
    plus its revert changes no content, and a banner that counted it would send the owner to a
    remedy that reports "payload is unchanged".
    """
    rc, _ = _git(source_path, "diff", "--quiet", sha, ref, "--", PAYLOAD_REL)
    if rc == 0:
        return True
    if rc == 1:                     # git's documented "differences found"
        return False
    return None                     # anything else is an error, never "no differences"


def _count(source_path, rng):
    """Payload-touching commits in ``rng``, or ``None`` on any error. ``--`` scopes the count so a
    README or dashboard commit is never an "engine fix"."""
    rc, txt = _git(source_path, "rev-list", "--count", rng, "--", PAYLOAD_REL)
    if rc != 0:
        return None
    try:
        return int(txt.strip())
    except (TypeError, ValueError):
        return None


def drift(source_path, dest):
    """Compare the INSTALLED engine against what is MERGED.

    Returns ``{known, behind, installed_sha, installed_at, source, message, remedy}``. ``behind`` is
    a count ONLY when ``known``; every failure leaves it ``None`` with an honest ``message``. Never
    raises — it is read by the 2-second poll.
    """
    out = dict(_UNKNOWN)
    out["source"] = source_path
    if not source_path:
        return out                 # no engine source among the watched repos — nothing to say

    stamp = installed_stamp(dest)
    if stamp is None:
        return out                 # never published here — nothing to say
    if stamp is UNREADABLE:
        out["message"] = _cant_tell("the live engine's VERSION stamp could not be read")
        return out

    out["installed_sha"] = stamp["sha"]
    out["installed_at"] = stamp["at"]
    sha = stamp["sha"]

    if sha == NOGIT:
        out["message"] = _cant_tell("the live engine was published from a tree with no git history")
        return out

    rc, _ = _git(source_path, "cat-file", "-e", "%s^{commit}" % sha)
    if rc != 0:
        out["message"] = _cant_tell(
            "the live engine's build (%s) is not in this checkout's history" % sha)
        return out

    # A dirty payload makes the stamp un-comparable in BOTH directions — refuse the number rather
    # than pick the flattering one.
    dirty = _payload_dirty(source_path)
    if dirty is None:
        out["message"] = _cant_tell("git could not tell whether this checkout's engine is clean")
        return out
    if dirty:
        out["message"] = _cant_tell(
            "this checkout has uncommitted engine changes — the installer would publish those, "
            "not its last commit")
        return out

    ref, merged = _merged_ref(source_path)

    # CONTENT first — the installer's own question. This is what decides "is anything waiting?";
    # the commit count below only ever puts a NUMBER on a gap the content has already proven.
    same = _payload_same(source_path, sha, ref)
    if same is None:
        out["message"] = _cant_tell("git could not compare the live engine against %s" % ref)
        return out
    if same:
        # Byte-identical payloads: the live engine IS the merged engine, whatever the commit graph
        # says. Silent — §0.2 forbids a surface that congratulates itself every two seconds.
        out["known"] = True
        out["behind"] = 0
        return out

    behind = _count(source_path, "%s..%s" % (sha, ref))
    if behind is None:
        out["message"] = _cant_tell("git could not count the engine commits since %s" % sha)
        return out
    if behind == 0:
        # The content differs but NO merged commit explains it: the installed engine is ahead of, or
        # divergent from, what is merged (a rolled-back tree, a hand-edited install). "0" here would
        # be a confident all-clear over an engine nobody can account for.
        out["message"] = _cant_tell(
            "the live engine's payload differs from %s but no commit explains it — the installed "
            "copy may be ahead of, or divergent from, this checkout" % ref)
        return out

    # Would install.sh alone actually deliver those commits? Only if they are IN the checkout. When
    # they are merged but unpulled, the remedy has to name the pull or it would publish the old code
    # and the banner would refuse to clear.
    unpulled = _count(source_path, "HEAD..%s" % ref) if merged else 0
    out["known"] = True
    out["behind"] = behind
    out["message"] = drift_message(behind, merged=merged, needs_pull=bool(unpulled))
    return out


class EngineDrift:
    """The engine's publish drift, measured on a slow clock.

    Constructed in the composition root (``bin/command-center``) and injected — like ``Version`` /
    ``Actions`` / ``Tidy`` — so tests drive it with a temp checkout and no surface is on by accident.
    ``measure`` is injectable for the same reason.
    """

    def __init__(self, source_path, dest, interval=DRIFT_POLL_SECONDS, clock=None, measure=None):
        self._source = source_path
        self._dest = dest
        fn = measure if measure is not None else drift
        self._cached = pollers.Cached(lambda: self._safe(fn), interval, clock=clock)
        # The server is a ThreadingHTTPServer: every request is its own thread, so two overlapping
        # polls (a second tab, a slow snapshot) can be inside state() at once. `Cached` reads and
        # refreshes without a lock of its own, so unsynchronized callers would each shell out to git
        # for one answer — and could observe `_value` set while `_last` is still None. Same reasoning
        # and same remedy as Version.current()'s lock; here the refresh is a SUBPROCESS, so the waste
        # is worth a mutex that costs nothing at two polls a second.
        self._lock = threading.Lock()

    def _safe(self, fn):
        try:
            return fn(self._source, self._dest)
        except Exception:
            # The field is the truth the owner came for; a drift stamp must never be the reason a
            # poll 500s. But an unmeasurable engine is an UNKNOWN engine that SAYS SO — a silent
            # unknown here would render as no engine line, which is indistinguishable from a live
            # one, and the error path would be quietly issuing an all-clear.
            out = unmeasurable()
            out["source"] = self._source
            return out

    def state(self):
        """The snapshot's ``engine`` block. A copy, so no consumer can mutate the cached answer."""
        with self._lock:
            return dict(self._cached.get())
