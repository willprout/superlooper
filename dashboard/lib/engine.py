"""Engine publish drift (issue #166) — is the loop RUNNING the fixes that were merged?

**The gap this names.** The runner executes the INSTALLED engine copy at ``~/.claude/skills/superlooper``,
never this checkout. A merged engine change is therefore INERT until someone republishes it through
the gated ``bin/install.sh`` — William's own touch, by design: that gate is the fence that makes
``skills/**`` a supervised bright line, and it is not up for negotiation. The gate is RIGHT. The
SILENCE around it was not. A fix could sit merged for days while the loop kept running the old code
and nothing on the owner's screen said so — he would watch a bug he had already fixed keep
happening, with no way to tell "not fixed yet" from "fixed, but not switched on".

This module makes that gap VISIBLE and does nothing else. It never publishes, never runs the
installer, never touches the installed copy: it counts, and it tells. The owner's tap stays the only
thing that switches an engine change on.

**It mirrors install.sh's own arithmetic, deliberately.** The installer records the source commit it
published in ``$DEST/VERSION`` (first token) and diffs that against the checkout's HEAD, scoped to
the payload path. This reads the SAME stamp and asks the SAME question, so the number on screen is
exactly what a re-run of the named remedy would switch on. Counting against anything else — a
freshly fetched ``origin/main``, say — would let the banner promise fixes the remedy would not
actually deliver, which is a new lie planted in the surface built to end them.

**HEAD, not literally ``main``.** The issue asks for "behind ``main``"; the honest baseline is the
checkout's HEAD, because HEAD is what ``bin/install.sh`` publishes. On the owner's machine that
checkout sits on main, so they are the same commit and the distinction is invisible. When they are
NOT the same, HEAD is still the truthful answer to the only question the banner actually asks: what
would republishing right now switch on? (See the PR body — this is the one stated assumption.)

**Unknown is never zero.** Every failure — no in-history baseline, a ``nogit`` tarball stamp, a git
that errors — reports ``known: False`` and ``behind: None``, never ``behind: 0``. A false "you are
up to date" is precisely the failure class this issue exists to close, so the one direction this
module may never fail in is the reassuring one.

**Silence is reserved for the two cases that have nothing to say**: no engine source among the
watched checkouts (a friend who adopted superlooper for their own repo has no monorepo to compare
against), and no ``VERSION`` at the install dir (nothing was ever published through install.sh
here). Both are the honest empty answer, not a mystery — and a line that could never clear would be
exactly the nag §0.2 forbids.
"""
import os

import pollers

# The publishable payload, repo-relative. This MUST mirror bin/install.sh's own PAYLOAD_REL: the
# installer and this counter have to scope the same tree, or the number on screen answers a
# different question than the remedy performs. tests/test_engine.py pins the two together.
PAYLOAD_REL = "skills/superlooper/skill"

# The one remedy this names: the GATED installer at the repo root. Deliberately NOT the engine's own
# nested copy (skills/superlooper/bin/install.sh), which publishes the same payload WITHOUT the diff
# gate — a banner that sent the owner through the ungated door would be helping him skip his own
# fence, which is the opposite of this module's job.
REMEDY = "bin/install.sh"

VERSION_FILE = "VERSION"

# install.sh's own sentinel: it stamps `nogit` when publishing from a tree with no git (a released
# tarball). There is no commit behind it, so there is no baseline and no honest number.
NOGIT = "nogit"

# Drift moves only when someone MERGES or REPUBLISHES — both minutes-scale, human-paced events. The
# snapshot is built every 2 seconds; shelling git twice a second for an answer that changes twice a
# day is waste the poll loop should never pay. 30s is far below the human timescale of the thing
# being watched and costs one git call a minute.
DRIFT_POLL_SECONDS = 30

# Indirected through the module global so a test can drive the failure paths (a git that errors)
# without a real broken checkout. Reused from pollers rather than re-implemented: one read-only git
# wrapper that can never raise into the poll loop, not two that can drift apart.
_git = pollers.git_run

_UNKNOWN = {"known": False, "behind": None, "installed_sha": None, "installed_at": None,
            "source": None, "message": None, "remedy": REMEDY}


def install_dir(superlooper_cli):
    """The installer's ``$DEST`` — the live engine copy — derived from the ``superlooper_cli`` path
    the dashboard's config ALREADY carries. ``None`` for an empty/absent setting.

    install.sh publishes the payload to ``$DEST`` and the CLI to ``$DEST/bin/superlooper``, so the
    parent of the CLI's ``bin/`` *is* ``$DEST``. That structural relationship is why this needs no
    new config key: an operator with a non-standard install already points ``superlooper_cli`` at it
    and gets the right answer for free, and there is no second setting to fall out of sync with the
    first. (The engine's own installer is what creates that layout, so the two cannot drift without
    the CLI itself moving — which this derivation would then follow.)
    """
    if not isinstance(superlooper_cli, str) or not superlooper_cli.strip():
        return None
    cli = os.path.abspath(os.path.expanduser(superlooper_cli.strip()))
    dest = os.path.dirname(os.path.dirname(cli))
    return dest or None


def installed_stamp(dest):
    """``{"sha", "at"}`` from ``$DEST/VERSION`` — the build the loop is actually RUNNING — or
    ``None`` when nothing was ever published here (absent, empty, or unreadable).

    install.sh writes ``"<short-sha> <YYYY-MM-DD>"``. Only the first line is read: the file is the
    installer's one-line receipt, and anything after it is not part of the contract.
    """
    if not dest:
        return None
    try:
        with open(os.path.join(os.fspath(dest), VERSION_FILE),
                  encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    parts = first.split()
    if not parts:
        return None
    return {"sha": parts[0], "at": parts[1] if len(parts) > 1 else None}


def source_repo(paths):
    """The watched checkout that carries the engine payload — the superlooper monorepo — or ``None``
    when none does.

    Derived from the repo list config already declares (never a filesystem hunt): the engine has
    exactly one source tree, and a checkout either contains ``skills/superlooper/skill`` or it does
    not. ``None`` is the correct, quiet answer for a dashboard watching only repos that the loop
    BUILDS rather than the repo the loop IS — there is no engine source to compare, so there is
    nothing to say about drift.
    """
    for p in paths or []:
        if isinstance(p, str) and p.strip() \
                and os.path.isdir(os.path.join(os.path.expanduser(p), PAYLOAD_REL)):
            return p
    return None


def drift_message(behind):
    """The DoD's own sentence, in the owner's terms: what is waiting, and the one thing that would
    switch it on. Pluralized because a banner that says "1 engine fixes" is a banner he trusts a
    little less, and this surface's whole currency is trust."""
    if behind == 1:
        return "1 engine fix merged but not yet live; re-run the installer to switch it on"
    return ("%d engine fixes merged but not yet live; re-run the installer to switch them on"
            % behind)


def _cant_tell(reason):
    """The honest non-answer. It must never read as an all-clear, and must never invent a number."""
    return "can't tell which engine build is live — %s" % reason


def drift(source_path, dest):
    """Compare the INSTALLED engine against what ``source_path``'s HEAD would publish.

    Returns ``{known, behind, installed_sha, installed_at, source, message, remedy}``. ``behind`` is
    a count ONLY when ``known``; every failure leaves it ``None`` with an honest ``message``. Never
    raises — it is read by the 2-second poll.
    """
    out = dict(_UNKNOWN)
    out["source"] = source_path
    stamp = installed_stamp(dest)

    # The two silent cases: nothing published here, or no source to compare against. Both are the
    # honest empty answer — there is no drift question to ask, so the strip shows no engine line.
    if stamp is None or not source_path:
        return out

    out["installed_sha"] = stamp["sha"]
    out["installed_at"] = stamp["at"]

    if stamp["sha"] == NOGIT:
        out["message"] = _cant_tell("the live engine was published from a tree with no git history")
        return out

    # Is the published commit even in this checkout's history? install.sh asks exactly this before
    # trusting its baseline, and fails SAFE (treats the whole payload as new). A banner cannot
    # honestly claim a NUMBER on a baseline it can't resolve, so its safe direction is to say so.
    rc, _ = _git(source_path, "cat-file", "-e", "%s^{commit}" % stamp["sha"])
    if rc != 0:
        out["message"] = _cant_tell(
            "the live engine's build (%s) is not in this checkout's history" % stamp["sha"])
        return out

    # `--` scopes the count to the payload, so a README or dashboard commit is not an "engine fix".
    rc, txt = _git(source_path, "rev-list", "--count",
                   "%s..HEAD" % stamp["sha"], "--", PAYLOAD_REL)
    if rc != 0:
        out["message"] = _cant_tell("git could not count the engine commits since %s" % stamp["sha"])
        return out
    try:
        behind = int(txt.strip())
    except (TypeError, ValueError):
        out["message"] = _cant_tell("git's answer for the commits since %s was unreadable"
                                    % stamp["sha"])
        return out

    out["known"] = True
    out["behind"] = behind
    # Silent at zero: the engine IS live, and §0.2 forbids a surface that congratulates itself every
    # two seconds. The strip only speaks when there is a gap.
    out["message"] = drift_message(behind) if behind > 0 else None
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

    def _safe(self, fn):
        try:
            return fn(self._source, self._dest)
        except Exception:
            # The field is the truth the owner came for; a drift stamp must never be the reason a
            # poll 500s. An unmeasurable engine is an UNKNOWN engine — never a silent all-clear.
            out = dict(_UNKNOWN)
            out["source"] = self._source
            return out

    def state(self):
        """The snapshot's ``engine`` block. A copy, so no consumer can mutate the cached answer."""
        return dict(self._cached.get())
