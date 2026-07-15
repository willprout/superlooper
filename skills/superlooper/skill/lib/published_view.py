"""The runner's PUBLISHED GitHub view (issue #146) — shaping only, no I/O.

The runner polls GitHub every ``GH_POLL_SECONDS`` and holds the answer in memory (``gh_view`` +
``_raw_by_id``) purely to decide with. Nothing ever wrote it down, so the dashboard — which needs
the same facts to draw the boards — went and asked GitHub the same questions on its own clock.
Two pollers, one rate-limit budget (a contributor to the 2026-07-08 GraphQL exhaustion behind the
park/notify storms, INCIDENT-2026-07-08-park-notify-storm §1b), and two answers that could
disagree: the dashboard has rendered an externally-closed issue as open and a dead session as
"launching", because its view and the runner's were never the same view.

This module turns the in-memory view into the document the runner writes to
``state/gh_view.json`` each tick, which the dashboard then renders as its primary truth. It is
PURE — a dict in, a dict out — so the whole contract is tested without a runner, a state home, or
gh (tests/test_published_view.py). The runner owns the writing (atomic, via ``loopstate.save``).

Two disciplines are load-bearing:

  * **Never invent.** A fact the runner does not hold is ABSENT from the document, never a
    fabricated empty a reader would mistake for an answer — the refused-vs-answered-empty line the
    poll path already holds (issues #21/#61/#78). An unreadable view publishes as ``stale``.
  * **Titles carry forward.** The poll set is agent-ready + in-progress issues only, so a MERGED
    flight's issue (closed now) drops out and its title would vanish — blanking the arrivals board
    on the landing it just celebrated. Titles survive for issues the runner still TRACKS in
    loopstate, and are pruned the moment it doesn't, which is what bounds the document's growth.

This module never raises: it runs inside the tick, ahead of the heartbeat stamp, and a raise here
would wedge the loop exactly as the 2026-07-07 binary-file incident did.
"""

# The issue fields the dashboard needs to draw a queue row: the identity (number/title), the
# labels it renders as chips, the body it parses `connections:` out of, and the createdAt it
# orders the departures board by. Copied field-by-field rather than passing the raw gh dict
# through, so the published shape is a NAMED contract and a future gh field can't silently
# balloon the file.
_ISSUE_KEYS = ("number", "title", "labels", "body", "createdAt")


def _dict(v):
    return v if isinstance(v, dict) else {}


def _issue_row(raw):
    """One published issue: only the named keys, only when present. A key gh didn't answer stays
    ABSENT rather than becoming a None the dashboard would render as a real (empty) value."""
    return {k: raw[k] for k in _ISSUE_KEYS if k in raw}


def _closed_list(closed_nums):
    """The runner's closed-issue set as a sorted list of ints — JSON has no set, and a stable order
    means an unchanged view rewrites an unchanged file. Non-int members are dropped (bool is an int
    subclass, so it is excluded explicitly): the dashboard tests membership by issue number, and a
    True in that set would answer `num in closed` for issue 1."""
    if not isinstance(closed_nums, (set, frozenset, list, tuple)):
        return []
    return sorted(n for n in closed_nums if type(n) is int)


def build(gh_view, raw_by_id, tracked_ids, now, polled_at=None, carry_titles=None):
    """The document for ``state/gh_view.json``.

    ``gh_view``     the runner's in-memory view (``stale``, ``consecutive_failures``,
                    ``closed_nums``, ``prs``, ``dev_checks``).
    ``raw_by_id``   ``{iid: raw gh issue dict}`` for the issues polled this window.
    ``tracked_ids`` the iids loopstate still tracks — what bounds the title carry.
    ``now``         this tick's wall clock (stamped as ``published_at``).
    ``polled_at``   the last SUCCESSFUL GitHub poll's clock, or ``None`` if never reached. A
                    DIFFERENT clock from ``published_at`` on purpose: the dashboard shows how old
                    the DATA is, which is the poll, not the tick that copied it out.
    ``carry_titles`` the previous document's ``titles`` map (see the carry discipline above).

    An unreadable ``gh_view`` yields an empty-but-typed document marked ``stale`` — never a
    confident all-clear.
    """
    view = _dict(gh_view)
    raw = _dict(raw_by_id)
    tracked = tracked_ids if isinstance(tracked_ids, (set, frozenset)) else set(tracked_ids or ())

    issues, titles = {}, {}
    for iid, r in raw.items():
        if not isinstance(r, dict):
            continue                       # a wrong-typed entry is skipped, never half-published
        issues[iid] = _issue_row(r)
        if r.get("title"):
            titles[iid] = r["title"]
    # Carry a TRACKED issue's title only where this window has none — a live read (a renamed issue)
    # always wins over the remembered one.
    for iid, t in _dict(carry_titles).items():
        if iid in tracked and iid not in titles and t:
            titles[iid] = t

    return {
        "published_at": int(now),
        "polled_at": int(polled_at) if isinstance(polled_at, (int, float)) else None,
        # A view we could not read is not one to trust: fail closed to stale so the dashboard names
        # the doubt instead of rendering it as live truth.
        "stale": bool(view.get("stale", True)) if view else True,
        "consecutive_failures": view.get("consecutive_failures", 0),
        "issues": issues,
        "titles": titles,
        "closed_nums": _closed_list(view.get("closed_nums")),
        "prs": _dict(view.get("prs")),
        "dev_checks": _dict(view.get("dev_checks")),
    }
