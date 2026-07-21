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
  * **Titles and SETTLED PRs carry forward.** The poll set is agent-ready + in-progress issues
    only, and the want-set skips ``TERMINAL_STATUSES`` outright, so a MERGED flight drops out of
    both ``raw_by_id`` and ``prs`` the moment it lands. Left alone, its title and its PR's
    ``+N/−N/files`` would vanish exactly when the arrivals board wants to celebrate it — and that
    cargo chip is meant to outlive the flight forever (the worktree is cleaned up; the PR is what
    remembers). Both survive for issues the runner still TRACKS in loopstate, and are pruned the
    moment it doesn't, which is what bounds the document's growth.

This module never raises: it runs inside the tick, ahead of the heartbeat stamp, and a raise here
would wedge the loop exactly as the 2026-07-07 binary-file incident did.
"""

# The issue fields the dashboard needs to draw a queue row: the identity (number/title), the
# labels it renders as chips, the body it parses `connections:` out of, and the createdAt it
# orders the departures board by. Copied field-by-field rather than passing the raw gh dict
# through, so the published shape is a NAMED contract and a future gh field can't silently
# balloon the file.
_ISSUE_KEYS = ("number", "title", "labels", "body", "createdAt")

# The PR states a carry may remember. A MERGED/CLOSED PR is SETTLED — its checks, its mergeability
# and its diff can never move again, so republishing it forever is simply the truth. An OPEN PR is
# not: its CI can go red, its mergeability can rot. When one goes missing from a poll window (the
# want-set skipped it, or MAX_POLL_CALLS starved the tail) the honest answer is ABSENT — the gate
# then fails closed to not-cleared, whereas a frozen "green" would be a false clearance. Same line
# the dashboard's own ConcludedFlights drew for the same reason.
_SETTLED_PR_STATES = frozenset({"MERGED", "CLOSED"})

# How many landed flights keep their PR facts. `tracked_ids` looks like a bound but isn't: nothing
# prunes loopstate, so it grows with every landing forever — and this file is rewritten every tick
# and re-read by the dashboard every ~2s. The newest landings are the only ones the arrivals board
# can show (it caps at 5 pages / 3 days), so this sits comfortably above anything renderable while
# keeping the document's size flat. Past the cap a flight's cargo chip reads absent — honest, and
# never the "+0/−0" that would libel a worker as having done nothing.
CARRY_PR_LIMIT = 60

# The cargo chip's three numbers. `_size_totals` is the ONLY authority on them: they are stripped
# from an entry before its validated answer is merged back, so a wrong-typed total (from a corrupt
# or hand-edited document the carry seeds itself from) can't ride through. The dashboard revalidates
# too, so this is belt-and-braces — but the carry is a FIXED POINT, and junk allowed in here would
# republish itself forever.
_SIZE_TOTAL_KEYS = ("additions", "deletions", "changedFiles")


def _dict(v):
    return v if isinstance(v, dict) else {}


def _issue_row(raw):
    """One published issue: only the named keys, only when present. A key gh didn't answer stays
    ABSENT rather than becoming a None the dashboard would render as a real (empty) value."""
    return {k: raw[k] for k in _ISSUE_KEYS if k in raw}


def _iid_num(iid):
    """``i23`` -> ``23``; anything else -> ``None`` (sorts last under the carry cap)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _size_totals(pr):
    """``{additions, deletions, changedFiles}`` for a PR, from its per-file rows when it still has
    them, else from totals a previous carry already summed. Empty when neither is present — absent
    must stay absent, never a "+0/−0" the dashboard would render as a worker who changed nothing."""
    files = pr.get("files")
    if isinstance(files, list) and files:
        added = deleted = 0
        for f in files:
            if not isinstance(f, dict):
                continue
            a, d = f.get("additions"), f.get("deletions")
            added += a if isinstance(a, int) and not isinstance(a, bool) else 0
            deleted += d if isinstance(d, int) and not isinstance(d, bool) else 0
        return {"additions": added, "deletions": deleted, "changedFiles": len(files)}
    out = {}
    for k in ("additions", "deletions", "changedFiles"):
        v = pr.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    return out


def _carried_pr(pr, state):
    """The reduced entry a carried PR keeps: everything the dashboard reads (``number``, ``state``,
    ``mergeable``, ``statusCheckRollup`` and the review ``comments`` its gate checklist needs) with
    the per-file rows COLLAPSED to their totals. ``files`` is the bulk of a PR read and nothing
    downstream wants the rows — only the chip's three numbers — so summing here keeps a document
    that is rewritten every tick and re-read every 2s from growing with every landing.

    Summing at carry time also makes the entry a FIXED POINT: it re-carries itself unchanged on
    every later tick, which is what stops the chip blanking one poll window later instead of one."""
    out = {k: v for k, v in pr.items() if k != "files" and k not in _SIZE_TOTAL_KEYS}
    out["state"] = state
    out.update(_size_totals(pr))     # the validated totals are the only ones that survive the carry
    return out


def _closed_list(closed_nums):
    """The runner's closed-issue set as a sorted list of ints — JSON has no set, and a stable order
    means an unchanged view rewrites an unchanged file. Non-int members are dropped (bool is an int
    subclass, so it is excluded explicitly): the dashboard tests membership by issue number, and a
    True in that set would answer `num in closed` for issue 1."""
    if not isinstance(closed_nums, (set, frozenset, list, tuple)):
        return []
    return sorted(n for n in closed_nums if type(n) is int)


def build(gh_view, raw_by_id, tracked_ids, now, polled_at=None, carry_titles=None,
          carry_prs=None, merged_ids=None):
    """The document for ``state/gh_view.json``.

    ``gh_view``     the runner's in-memory view (``stale``, ``consecutive_failures``,
                    ``closed_nums``, ``prs``, ``dev_checks``).
    ``raw_by_id``   ``{iid: raw gh issue dict}`` for the issues polled this window.
    ``tracked_ids`` the iids loopstate still tracks — what bounds both carries.
    ``now``         this tick's wall clock (stamped as ``published_at``).
    ``polled_at``   the last SUCCESSFUL GitHub poll's clock, or ``None`` if never reached. A
                    DIFFERENT clock from ``published_at`` on purpose: the dashboard shows how old
                    the DATA is, which is the poll, not the tick that copied it out.
    ``carry_titles`` the previous document's ``titles`` map (see the carry discipline above).
    ``carry_prs``    the previous document's ``prs`` map; only SETTLED entries are remembered.
    ``merged_ids``   the iids loopstate records as ``merged`` — the runner's own record of its own
                     landings, and what settles a cached PR still reading OPEN (see below).

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

    # The same carry for SETTLED PRs — what keeps a landed flight's cargo chip alive once the
    # want-set stops polling its now-terminal issue.
    #
    # An entry is settled two ways, and the second is the load-bearing one:
    #   * gh SAID so — its cached state already reads MERGED/CLOSED (an externally closed PR, or the
    #     crash-recovery path that re-observes a merge someone else's tick performed);
    #   * the RUNNER DID it — loopstate says `merged`. This is the normal landing, and it is the only
    #     path that fires in practice: the gate can only merge a PR that reads OPEN + MERGEABLE +
    #     green, so the cached read at the moment of merging says OPEN, and `_exec_merge` records the
    #     landing in loopstate and never back into gh_view. Waiting for a poll to observe MERGED is
    #     waiting for a poll that never comes — the issue is terminal, so it is never fetched again.
    #     loopstate's `merged` is the runner's own positive record of its own action (written after
    #     gh.merge_pr returned ok), which is a stronger fact than any re-read would be.
    #
    # An OPEN PR the runner did NOT merge (a parked flight) is still never carried: its CI and
    # mergeability can still move, and a frozen "green" would be the false clearance the gate exists
    # to refuse. A fresh read always wins over any carry.
    merged = merged_ids if isinstance(merged_ids, (set, frozenset)) else set(merged_ids or ())
    prs = {}
    for iid, pr in _dict(view.get("prs")).items():
        # A flight this runner merged: loopstate's record beats the cached read, which is
        # DEFINITIONALLY pre-merge (the gate only merges an OPEN one) and will never be refreshed —
        # the issue is terminal, so it is never polled again.
        #
        # Precisely: this settles from the tick AFTER the landing, not the landing tick itself. The
        # tick loads `ist_map` before `_exec_merge` writes `status: merged` to disk, so the landing
        # tick still sees the pre-merge map and publishes the raw OPEN read. Harmless — nothing in
        # the dashboard's LIVE path reads a PR's `state`, and cargo/gate render correctly from that
        # raw entry — and it self-corrects on the very next tick, including the worst interleaving
        # (a poll firing in between, which drops the entry to the carry: the `iid in merged` test
        # below runs BEFORE the settled-state test in both loops, so an OPEN-stamped carry entry is
        # promoted anyway and nothing is ever lost).
        prs[iid] = _carried_pr(pr, "MERGED") if (isinstance(pr, dict) and iid in merged) else pr
    settling = []
    for iid, pr in _dict(carry_prs).items():
        if iid not in tracked or iid in prs or not isinstance(pr, dict):
            continue
        if iid in merged:
            settling.append((iid, _carried_pr(pr, "MERGED")))
        elif pr.get("state") in _SETTLED_PR_STATES:
            settling.append((iid, _carried_pr(pr, pr["state"])))
    # Newest landings first, then capped — the file must not grow with every landing forever.
    settling.sort(key=lambda kv: (_iid_num(kv[0]) is None, -(_iid_num(kv[0]) or 0)))
    for iid, pr in settling[:CARRY_PR_LIMIT]:
        prs[iid] = pr

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
        # Does the runner VOUCH for that closed set (issue #172)? An empty `closed_nums` is
        # ambiguous on its own — GitHub answered "nothing is closed", or it REFUSED the read and the
        # fail-closed parser produced the same empty — and the probe (`gh api rate_limit`) is exempt
        # from throttling, so `stale` stays False through a throttle either way. Trusted ONLY in the
        # direction it explicitly asserts: True demands an explicit True, so a view that never
        # claimed the read landed publishes an honest False rather than a confident all-clear.
        "closed_read_ok": view.get("closed_read_ok") is True,
        "prs": prs,
        "dev_checks": _dict(view.get("dev_checks")),
    }
