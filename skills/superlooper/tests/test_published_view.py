"""The runner's PUBLISHED view (issue #146) — the shaping core.

The runner has always held a complete GitHub view in memory (`self.gh_view` + `_raw_by_id`) and
thrown it away every tick. The dashboard, unable to see it, went and asked GitHub the same
questions itself — a second poller on the same rate-limit budget (a contributor to the 2026-07-08
GraphQL exhaustion, INCIDENT-2026-07-08-park-notify-storm §1b) whose answers could and did diverge
from the runner's (an externally-closed issue the local state never absorbed; a dead session
rendered as "launching").

`published_view.build` is the pure shaping step: in-memory view -> the document written to
`state/gh_view.json`. It is pure so the whole contract is testable without a runner, a state home,
or gh. Two disciplines it must hold:

  * NEVER invent. A datum the runner does not hold is ABSENT from the document, never a
    fabricated empty that a reader would mistake for an answer (the refused-vs-answered-empty
    discipline the poll path already keeps — issues #21/#61/#78).
  * TITLES CARRY FORWARD. The runner polls only agent-ready + in-progress issues, so a MERGED
    flight's issue (now closed) leaves the poll set and its title would vanish from the document —
    blanking the arrivals board on the very landing it just celebrated. Titles are therefore
    carried for issues the runner still TRACKS in loopstate, and pruned once it doesn't.
"""
import published_view


def _issue(num, title="a title", labels=(), body="", created="2026-07-15T10:00:00Z"):
    return {"number": num, "title": title, "labels": [{"name": n} for n in labels],
            "body": body, "createdAt": created}


def _view(**kw):
    base = {"stale": False, "consecutive_failures": 0, "closed_nums": set(),
            "prs": {}, "issue_comments": {}, "dev_checks": {}}
    base.update(kw)
    return base


def test_publishes_the_polled_issues_and_their_shape():
    raw = {"i7": _issue(7, "add a widget", labels=("agent-ready",), body="Loop metadata")}
    doc = published_view.build(_view(), raw, tracked_ids=set(), now=1000, polled_at=990)
    assert doc["issues"]["i7"]["number"] == 7
    assert doc["issues"]["i7"]["title"] == "add a widget"
    # body/labels/createdAt ride along: the departures board parses connections out of the body and
    # orders by createdAt, so a view without them could not feed the queue.
    assert doc["issues"]["i7"]["body"] == "Loop metadata"
    assert doc["issues"]["i7"]["labels"] == [{"name": "agent-ready"}]
    assert doc["issues"]["i7"]["createdAt"] == "2026-07-15T10:00:00Z"


def test_stamps_when_it_was_published_and_when_github_was_last_polled():
    # The two clocks the dashboard shows are DIFFERENT and must both be honest: published_at is
    # this tick, polled_at is the last SUCCESSFUL GitHub read (up to GH_POLL_SECONDS older).
    doc = published_view.build(_view(), {}, tracked_ids=set(), now=1000, polled_at=930)
    assert doc["published_at"] == 1000
    assert doc["polled_at"] == 930


def test_polled_at_is_none_when_github_was_never_reached():
    doc = published_view.build(_view(stale=True), {}, tracked_ids=set(), now=1000, polled_at=None)
    assert doc["polled_at"] is None
    assert doc["stale"] is True


def test_closed_nums_are_published_as_a_sorted_list():
    # A set is not JSON — and the ORDER must be stable so an unchanged view writes an unchanged file.
    doc = published_view.build(_view(closed_nums={9, 2, 5}), {}, tracked_ids=set(), now=1, polled_at=1)
    assert doc["closed_nums"] == [2, 5, 9]


def test_prs_and_dev_checks_ride_through_untouched():
    pr = {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE", "comments": [{"body": "hi"}]}
    doc = published_view.build(_view(prs={"i7": pr}, dev_checks={"ok": True}),
                               {}, tracked_ids=set(), now=1, polled_at=1)
    assert doc["prs"]["i7"] == pr
    assert doc["dev_checks"] == {"ok": True}


def test_a_tracked_issues_title_carries_forward_after_it_leaves_the_poll_set():
    # i7 merged: its issue is closed, so the runner no longer polls it and `raw` no longer carries
    # it. The arrivals board still names that flight, so the title must survive on the strength of
    # loopstate still tracking i7.
    carried = {"i7": "add a widget"}
    doc = published_view.build(_view(), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_titles=carried)
    assert doc["titles"]["i7"] == "add a widget"


def test_an_untracked_issues_title_is_pruned():
    # The carry is BOUNDED by loopstate: once the runner stops tracking an issue, its title stops
    # being republished forever. Without this the document grows without limit.
    doc = published_view.build(_view(), {}, tracked_ids=set(), now=1, polled_at=1,
                               carry_titles={"i7": "gone"})
    assert "i7" not in doc["titles"]


def test_a_fresh_title_wins_over_a_carried_one():
    # The issue was RENAMED while still in the poll set: the live read is the truth.
    doc = published_view.build(_view(), {"i7": _issue(7, "the new name")},
                               tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_titles={"i7": "the old name"})
    assert doc["titles"]["i7"] == "the new name"


def test_a_wrong_typed_view_never_raises():
    # The document is written inside the tick, before the heartbeat stamp. A raise here would wedge
    # the loop — the exact class the 2026-07-07 incident bought off. Garbage in, empty-but-typed out.
    doc = published_view.build(None, None, tracked_ids=None, now=5, polled_at=None)
    assert doc["published_at"] == 5
    assert doc["issues"] == {} and doc["titles"] == {} and doc["prs"] == {}
    assert doc["closed_nums"] == []
    # A view we could not read is not a view we can trust: it must publish as STALE, never as a
    # confident all-clear the dashboard would render as live truth.
    assert doc["stale"] is True


def test_a_non_dict_raw_issue_is_skipped_not_published():
    doc = published_view.build(_view(), {"i7": "not a dict", "i8": _issue(8)},
                               tracked_ids=set(), now=1, polled_at=1)
    assert "i7" not in doc["issues"]
    assert doc["issues"]["i8"]["number"] == 8


# --------------------------- the settled-PR carry (fresh-agent review, P0) ---------------------------
# The runner's `want` set EXCLUDES terminal statuses (actions.TERMINAL_STATUSES) — a merged flight is
# done being gated, so the poll never re-reads its PR and `prs` is rebuilt from scratch each window.
# Left alone, a landing's PR facts vanish from the document the moment it lands, and the dashboard's
# arrivals board loses the cargo chip it is supposed to keep FOREVER (issue #47/#48: the worktree is
# cleaned up, but the PR remembers +N/−N/files). That is a joy regression (§0.1) traded for plumbing.
#
# So a SETTLED PR carries forward, exactly as titles do. The line is the one ConcludedFlights already
# proved: only MERGED/CLOSED is remembered. Those facts can never change again. An OPEN PR that is
# merely missing this window (a poll-budget starve) is NOT carried — its CI/mergeable can still move,
# and serving a frozen "green" would be the false-clearance class the gate refuses.

def test_a_settled_prs_facts_carry_forward_after_the_flight_lands():
    carried = {"i7": {"number": 12, "state": "MERGED",
                      "files": [{"path": "a.py", "additions": 10, "deletions": 2}]}}
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs=carried)
    assert doc["prs"]["i7"]["state"] == "MERGED"
    # The cargo chip's own numbers survive — as TOTALS, not the per-file rows (see the size test).
    assert doc["prs"]["i7"]["additions"] == 10
    assert doc["prs"]["i7"]["changedFiles"] == 1


def test_a_closed_prs_facts_carry_forward_too():
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs={"i7": {"number": 12, "state": "CLOSED"}})
    assert doc["prs"]["i7"]["state"] == "CLOSED"


def test_an_open_pr_is_never_carried():
    # It can still change. A frozen OPEN read would show yesterday's CI as today's — the exact
    # false-clearance the gate exists to prevent.
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs={"i7": {"number": 12, "state": "OPEN"}})
    assert "i7" not in doc["prs"]


# --------------------------- the merge the runner performed itself (review round 2) ---------------------------
# THE bug the first carry missed. The gate can only merge a PR that reads OPEN + MERGEABLE + green,
# so the cached read at the moment of merging says "OPEN" — and `_exec_merge` records the landing in
# LOOPSTATE (`status: merged`), never back into gh_view. The next poll's want-set then skips the now
# terminal issue, its PR leaves `prs`, and a carry keyed only on a SETTLED gh state refuses it: the
# cargo chip worked for one poll window and blanked forever after.
#
# loopstate's `merged` status IS the runner's own positive record of its own merge — the strongest
# fact in the building, written by the executor after gh.merge_pr returned ok. So the carry reads it
# and stamps the state the runner established by ACTION rather than waiting for a poll to observe it.

def test_a_flight_the_runner_merged_carries_even_though_its_cached_pr_still_reads_open():
    # The real sequence: the cached PR is the pre-merge read (OPEN), loopstate says merged.
    cached = {"i7": {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE",
                     "files": [{"path": "a.py", "additions": 10, "deletions": 2}]}}
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs=cached, merged_ids={"i7"})
    assert "i7" in doc["prs"], "the flight the runner merged lost its PR facts"
    assert doc["prs"]["i7"]["state"] == "MERGED", "the runner's own merge must be recorded as one"
    assert doc["prs"]["i7"]["additions"] == 10


def test_an_open_pr_the_runner_did_not_merge_is_still_never_carried():
    # The discipline holds: only the runner's OWN recorded merge promotes an OPEN read. A parked
    # flight (open PR, terminal, never merged) stays absent rather than frozen.
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs={"i7": {"number": 12, "state": "OPEN"}}, merged_ids=set())
    assert "i7" not in doc["prs"]


def test_a_merged_flights_pr_keeps_carrying_across_many_ticks():
    # The carry must be a fixed point: what it publishes this tick is what it re-carries next tick,
    # forever. If the reduced shape couldn't survive its own round trip, the chip would blank on the
    # SECOND poll instead of the first — the same bug, one window later.
    prs = {"i7": {"number": 12, "state": "OPEN",
                  "files": [{"path": "a.py", "additions": 10, "deletions": 2}]}}
    for _ in range(5):
        doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                                   carry_prs=prs, merged_ids={"i7"})
        prs = doc["prs"]
    assert prs["i7"]["state"] == "MERGED"
    assert prs["i7"]["additions"] == 10 and prs["i7"]["changedFiles"] == 1


# --------------------------- the carry stays bounded (review round 2, P1) ---------------------------
# `tracked_ids` is loopstate, and NOTHING prunes loopstate — it grows with every landing, forever. So
# "bounded by tracked_ids" is not a bound at all once the carry actually fires. This file is rewritten
# by the runner every tick and re-read by the dashboard every 2s, so it must not accumulate.

def test_the_carry_drops_the_per_file_rows_and_keeps_the_totals():
    # `files` is the bulk of a PR read and the dashboard only ever wants the totals (its cargo chip).
    pr = {"number": 12, "state": "MERGED",
          "files": [{"path": "a.py", "additions": 10, "deletions": 2},
                    {"path": "b.py", "additions": 5, "deletions": 1}]}
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs={"i7": pr})
    got = doc["prs"]["i7"]
    assert "files" not in got, "the per-file rows must not accumulate in a file re-read every 2s"
    assert (got["additions"], got["deletions"], got["changedFiles"]) == (15, 3, 2)


def test_the_carry_is_capped_to_the_most_recent_landings():
    carry = {"i%d" % n: {"number": n, "state": "MERGED"} for n in range(1, 300)}
    tracked = set(carry)
    doc = published_view.build(_view(prs={}), {}, tracked_ids=tracked, now=1, polled_at=1,
                               carry_prs=carry)
    assert len(doc["prs"]) == published_view.CARRY_PR_LIMIT
    # The cap keeps the NEWEST landings — the only ones the arrivals board can show anyway.
    assert "i299" in doc["prs"]
    assert "i1" not in doc["prs"]


def test_a_fresh_read_is_never_dropped_by_the_cap():
    # The cap bounds the CARRY, never this window's live answers — starving the gate of a finishing
    # flight's PR to make room for old landings would be a spectacular own goal.
    carry = {"i%d" % n: {"number": n, "state": "MERGED"} for n in range(1, 300)}
    doc = published_view.build(_view(prs={"i7": {"number": 7, "state": "OPEN"}}), {},
                               tracked_ids=set(carry) | {"i7"}, now=1, polled_at=1, carry_prs=carry)
    assert doc["prs"]["i7"]["state"] == "OPEN"


def test_a_fresh_pr_read_always_wins_over_a_carried_one():
    fresh = {"number": 12, "state": "MERGED", "mergeable": "MERGEABLE"}
    doc = published_view.build(_view(prs={"i7": fresh}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                               carry_prs={"i7": {"number": 12, "state": "CLOSED"}})
    assert doc["prs"]["i7"] == fresh


def test_an_untracked_prs_facts_are_pruned():
    # Bounded by loopstate, exactly like the title carry — the document must not grow forever.
    doc = published_view.build(_view(prs={}), {}, tracked_ids=set(), now=1, polled_at=1,
                               carry_prs={"i7": {"number": 12, "state": "MERGED"}})
    assert "i7" not in doc["prs"]


def test_a_wrong_typed_carried_pr_never_raises():
    for bad in ("nope", None, 7, []):
        doc = published_view.build(_view(prs={}), {}, tracked_ids={"i7"}, now=1, polled_at=1,
                                   carry_prs={"i7": bad})
        assert "i7" not in doc["prs"]


def test_a_wrong_typed_total_is_never_immortalized_by_the_carry():
    # The carry seeds itself from the document ON DISK, and it is a FIXED POINT: junk allowed in
    # would republish itself forever. `_size_totals` only emits well-typed numbers, so the raw keys
    # must be stripped rather than copied — else a corrupt/hand-edited total rides through untouched.
    pr = {"number": 25, "state": "MERGED", "additions": "banana", "changedFiles": True}
    doc = published_view.build(_view(prs={}), {}, tracked_ids={"i15"}, now=1, polled_at=1,
                               carry_prs={"i15": pr})
    got = doc["prs"]["i15"]
    assert "additions" not in got and "changedFiles" not in got
