"""The LIVE source (issue #146) — the runner's published view, wearing the gh adapter's face.

The dashboard used to ask GitHub its own questions on its own clock. That made it a SECOND poller
on one rate-limit budget (a contributor to the 2026-07-08 GraphQL exhaustion behind the park/notify
storms, INCIDENT-2026-07-08-park-notify-storm §1b), and — worse — gave it a second opinion. Its
board could disagree with the runner's own state, and did: an externally-closed issue the local
state never absorbed, a dead session rendered as "launching" (2026-07-15).

The runner already knew all of it. Since #146 it writes that knowledge to ``state/gh_view.json``
every tick, and this class answers the assembler's questions from that document — with NO egress.
It deliberately mirrors the duck-typed surface the assembler already calls on ``gh``
(``open_issues_probe`` / ``open_issues`` / ``issue`` / ``pr_for_branch`` / ``pr_comments``) so the
SOURCE changes without the assembler learning a second way to ask. The GitHub-polling adapter still
exists and is still wired — as the loud FALLBACK for when the runner goes quiet, never as the
primary (server.py picks; flights.source_mode decides).

**Zero egress is the bright line.** This object holds no gh reference, so there is nothing here to
call and no quiet place for "just one" read to reappear (pinned in tests/test_runner_source.py).

Two shape facts the runner's view forces, both load-bearing:

  * **Its open set is PARTIAL.** The runner polls agent-ready + in-progress issues only, so absence
    from ``issues`` proves nothing — a parked issue is open and absent. Closure is therefore read
    ONLY from the runner's positive ``closed_nums``. Reading absence as closure (which the GitHub
    path can afford, since its list is every open issue) would conclude a live flight and land a
    still-flying plane.
  * **Its PR read carries ``files``, not ``additions``/``deletions``/``changedFiles``.** Those are
    summed here rather than re-asked of GitHub. Absent stays ABSENT — a PR whose files the runner
    never read must not render as an empty diff, which would look like a worker that did nothing.

Every method fails closed to an empty-but-typed answer, exactly as ``lib/gh``'s do, so a corrupt or
half-written document degrades the surface instead of raising into the 2-second poll loop.
"""

# `sl/i146-some-slug` -> `i146`. Every loop branch is named by brief.branch_for, so the issue id is
# the branch's own second segment — which is what lets a PR keyed by issue id answer a question
# asked by branch name. A branch that doesn't fit the pattern simply has no PR here (never a guess).
_BRANCH_PREFIX = "sl/"


def _dict(v):
    return v if isinstance(v, dict) and not isinstance(v, bool) else {}


def _int(v):
    # bool is an int subclass; a True in a numeric field is junk, not a number.
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _iid_of_branch(branch):
    """``sl/i146-a-slug`` -> ``i146``; anything else -> ``None``."""
    if not isinstance(branch, str) or not branch.startswith(_BRANCH_PREFIX):
        return None
    seg = branch[len(_BRANCH_PREFIX):].split("-", 1)[0]
    return seg if seg.startswith("i") and seg[1:].isdigit() else None


def _iid(num):
    return "i%d" % num


def _label_names(row):
    return [l.get("name") for l in row.get("labels") or [] if isinstance(l, dict)]


# The cargo chip's three numbers. `_pr_size` is the ONLY authority on them: they are stripped from
# the raw entry before its validated answer is merged in, so a wrong-typed total (a string, a bool)
# can never ride through untouched and reach the chip.
_SIZE_KEYS = ("additions", "deletions", "changedFiles")


def _pr_size(pr):
    """``{additions, deletions, changedFiles}`` for a PR, or ``{}`` when nobody has measured it.

    Two shapes arrive, both from the runner. A freshly polled PR carries the per-file rows (its
    ``_PR_FIELDS`` includes ``files``) and is summed here — DERIVED rather than re-asked of GitHub,
    the whole point of one source. A CARRIED PR (a landed flight, kept alive by the published view)
    already had its rows collapsed to totals engine-side, so those pass straight through; a landing
    that lost its chip here would defeat the carry the engine pays for every tick.

    Absent stays absent: an empty ``+0/−0`` on a PR nobody measured would read as a worker that
    changed nothing."""
    files = pr.get("files")
    if isinstance(files, list) and files:
        added = deleted = 0
        for f in files:
            if not isinstance(f, dict):
                continue
            added += _int(f.get("additions")) or 0
            deleted += _int(f.get("deletions")) or 0
        return {"additions": added, "deletions": deleted, "changedFiles": len(files)}
    out = {}
    for k in ("additions", "deletions", "changedFiles"):
        if _int(pr.get(k)) is not None:
            out[k] = pr[k]
    return out


class RunnerSource:
    """Answers the assembler's GitHub questions from the runner's published view. No egress."""

    def __init__(self, view):
        v = _dict(view)
        self._issues = _dict(v.get("issues"))
        self._titles = _dict(v.get("titles"))
        self._prs = _dict(v.get("prs"))
        self._closed = {n for n in (v.get("closed_nums") or []) if _int(n) is not None} \
            if isinstance(v.get("closed_nums"), list) else set()
        # The runner's OWN reachability verdict. Passing it through (rather than forming a second
        # opinion) is what keeps the dark-tower state and the board from contradicting each other.
        self._stale = bool(v.get("stale", True))

    # --------------------------- issues ---------------------------

    def _rows(self):
        return [r for r in self._issues.values() if isinstance(r, dict)]

    def open_issues_probe(self, repo, label=None, limit=200):
        """``(issues, reachable)`` — the same tuple ``gh.open_issues_probe`` returns, so the
        assembler's one-read discipline (issue #38) is untouched. ``reachable`` is the RUNNER's
        verdict: its view is fresh (it reached GitHub) or stale (it didn't). Note the list is the
        runner's PARTIAL open set (agent-ready + in-progress) — see the module docstring; absence
        from it is never evidence of closure."""
        return (self.open_issues(repo, label=label, limit=limit), not self._stale)

    def open_issues(self, repo, label=None, limit=200):
        rows = self._rows()
        if label is not None:
            rows = [r for r in rows if label in _label_names(r)]
        return rows[:limit] if isinstance(limit, int) and limit >= 0 else rows

    def is_closed(self, repo, num):
        """POSITIVE proof that issue ``num`` has closed, from the runner's ``closed_nums``.

        The assembler's GitHub path infers closure from ABSENCE in the open-issue list, which it can
        afford because that list is every open issue. The runner's is NOT — it polls agent-ready +
        in-progress only — so the same inference here would conclude every parked or holding flight
        and land a still-flying plane. Hence an explicit oracle: the assembler asks the source, and
        only a source that can answer positively (this one) gets asked. Free — no egress."""
        n = _int(num)
        return n is not None and n in self._closed

    def issue(self, repo, num):
        """One issue as the assembler expects it (``state``/``title``), or ``{}`` when the runner
        simply doesn't know — never an invented verdict. ``CLOSED`` comes ONLY from the runner's
        positive ``closed_nums``; a closed issue's title rides the view's carry, which is what keeps
        the arrivals board naming flights whose issues have left the poll set."""
        n = _int(num)
        if n is None:
            return {}
        if n in self._closed:
            out = {"number": n, "state": "CLOSED"}
            title = self._titles.get(_iid(n))
            if title:
                out["title"] = title
            return out
        row = self._issues.get(_iid(n))
        if isinstance(row, dict):
            return dict(row, state="OPEN")
        # Not in the poll set and not provably closed — but the view may still REMEMBER its title
        # (the carry outlives the poll set, and `closed_nums` is capped at the 200 most recently
        # closed, so a flight that landed long enough ago falls off it while loopstate still tracks
        # it). Hand back the title alone and assert NO state: it is evidence of a name, never of
        # closure, and inventing a state here would let the connection resolver fly a blocked
        # flight. Without this the arrivals board silently reverts to bare flight numbers once a
        # repo has closed 200 issues — the carry the engine pays for every tick, unreachable.
        title = self._titles.get(_iid(n))
        if title:
            return {"number": n, "title": title}
        return {}          # unknown to the runner: the callers all fail closed on {}

    # --------------------------- PRs ---------------------------

    def pr_for_branch(self, repo, branch):
        """The runner's PR view for ``branch``'s issue, in the dashboard's pr_facts shape (its size
        totals summed from the runner's file rows). ``{}`` when the runner holds no PR for it —
        which the gate checklist reads as not-cleared, the same fail-closed direction as an
        unreadable gh answer."""
        iid = _iid_of_branch(branch)
        if iid is None:
            return {}
        pr = _dict(self._prs.get(iid))
        if not pr:
            return {}
        out = {k: v for k, v in pr.items() if k not in _SIZE_KEYS}
        out.update(_pr_size(pr))       # the validated totals are the only ones that reach the chip
        return out

    def pr_comments(self, repo, num):
        """The PR's comments as the runner read them. ``[]`` when it holds none — the runner OMITS a
        refused comments read (issues #61/#78), so "absent" means "not read", and the dashboard's
        review line fails closed to not-passed exactly as the gate does."""
        n = _int(num)
        if n is None:
            return []
        for pr in self._prs.values():
            if isinstance(pr, dict) and _int(pr.get("number")) == n:
                c = pr.get("comments")
                return c if isinstance(c, list) else []
        return []
