"""The snapshot renders the RUNNER's view, and says when it isn't (issue #146) — assembly end.

`flights.source_mode` decides the mode and `runner_source.RunnerSource` answers from the published
view; these tests pin that `assemble_snapshot` actually WIRES them — that a fresh runner's snapshot
is built from the runner's document and touches GitHub zero times, that a silent runner falls back
to direct polling with a banner naming both facts, and that the round trip back to LIVE clears it.

The counting stub is the point. The DoD's headline is not "the code prefers the view" but "in
steady state the dashboard makes NO GitHub reads of its own" — the second poller on one rate-limit
budget is what helped drain the hourly GraphQL quota behind the 2026-07-08 park/notify storms
(§1b). A stub that RECORDS every read is the only way to assert an absence of egress.
"""
import os
import shutil

import pytest

import flights
import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
SLUG = "will-titan/superlooper-sandbox"
NOW = 1783364300

SILENT_AFTER = 90


@pytest.fixture
def home(tmp_path):
    dst = tmp_path / "will-titan__superlooper-sandbox"
    shutil.copytree(FIXTURE, dst)
    for iid in ("i16", "i23"):
        os.utime(dst / "state" / "activity" / iid, (NOW - 100, NOW - 100))
    (dst / "state" / "ALERT").unlink()
    (dst / "state" / "merges_frozen.json").unlink()
    return dst


def _config(home, **over):
    repo = {"slug": SLUG, "owner": "will-titan", "name": "superlooper-sandbox",
            "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
            "required_checks": ["tests"], "airline": "Sandbox Air"}
    cfg = {"poll_seconds": 2, "heartbeat_down_seconds": 300,
           "runner_silent_seconds": SILENT_AFTER, "repos": [repo]}
    cfg.update(over)
    return cfg


def _heartbeat(home, age):
    (home / "state" / "runner.heartbeat").write_text(str(int(NOW - age)))


def _publish(home, **over):
    """Write a runner-published view into the fixture home, as the live runner does each tick."""
    import json
    doc = {"published_at": NOW - 5, "polled_at": NOW - 20, "stale": False,
           "issues": {"i42": {"number": 42, "title": "Add a splash screen",
                              "labels": [{"name": "agent-ready"}], "body": "",
                              "createdAt": "2026-07-15T10:00:00Z"}},
           "titles": {"i23": "Add a motto footer", "i42": "Add a splash screen"},
           "closed_nums": [23, 16], "prs": {}}
    doc.update(over)
    (home / "state" / "gh_view.json").write_text(json.dumps(doc))
    return doc


class _CountingGh:
    """A gh stub that RECORDS every read. Any entry in `.reads` during a LIVE poll is egress that
    must not exist."""

    def __init__(self):
        self.reads = []

    def open_issues_probe(self, repo, label=None, limit=200):
        self.reads.append(("open_issues_probe", repo, label))
        return ([{"number": 42, "title": "Add a splash screen",
                  "labels": [{"name": "agent-ready"}]}], True)

    def open_issues(self, repo, label=None, limit=200):
        self.reads.append(("open_issues", repo, label))
        return self.open_issues_probe(repo, label=label, limit=limit)[0]

    def issue(self, repo, num):
        self.reads.append(("issue", repo, num))
        return {"number": num, "title": "from github directly", "state": "OPEN"}

    def pr_for_branch(self, repo, branch):
        self.reads.append(("pr_for_branch", repo, branch))
        return {}

    def pr_comments(self, repo, num):
        self.reads.append(("pr_comments", repo, num))
        return []


def _repo(snap):
    return snap["repos"][0]


# =============================== LIVE ===============================

def test_a_fresh_runner_makes_the_snapshot_live(home):
    _heartbeat(home, 10)
    _publish(home)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    assert _repo(snap)["source"]["mode"] == "live"


def test_live_rendering_performs_zero_dashboard_originated_github_reads(home):
    # THE bright line (DoD): with a fresh heartbeat, every datum comes from the runner's document
    # and the dashboard asks GitHub nothing at all.
    _heartbeat(home, 10)
    _publish(home)
    gh = _CountingGh()
    server.assemble_snapshot(_config(home), now=NOW, gh_mod=gh)
    assert gh.reads == [], "LIVE mode made GitHub reads: %r" % (gh.reads,)


def test_live_steady_state_stays_at_zero_reads_across_repeated_polls(home):
    # Steady state, not just the first paint: a per-poll read would still drain the budget, just
    # more slowly. Poll many times over a window and assert the egress stays exactly zero.
    _heartbeat(home, 10)
    _publish(home)
    gh = _CountingGh()
    for i in range(10):
        server.assemble_snapshot(_config(home), now=NOW + i * 2, gh_mod=gh)
    assert gh.reads == []


def test_live_boards_are_built_from_the_runners_view(home):
    # Not merely "no reads" — the board must actually carry the runner's data, or zero reads would
    # be trivially satisfied by an empty screen.
    _heartbeat(home, 10)
    _publish(home)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    deps = _repo(snap)["boards"]["departures"]
    assert any(d["num"] == 42 for d in deps), "the runner's queued issue never reached the board"


def test_live_titles_come_from_the_views_carry_not_from_github(home):
    # i23 is a merged flight: its issue is closed and gone from the poll set, so its title exists
    # ONLY in the view's carry. If the board names it, the carry works end to end.
    _heartbeat(home, 10)
    _publish(home)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    arr = _repo(snap)["boards"]["arrivals"]
    landed = [a for a in arr if a["num"] == 23]
    assert landed and "motto" in (landed[0].get("landed") or "").lower()


def test_live_reports_both_clocks(home):
    _heartbeat(home, 10)
    _publish(home, published_at=NOW - 5, polled_at=NOW - 20)
    src = _repo(server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh()))["source"]
    assert src["data_age"] == 20        # aged by the runner's GitHub read, not its tick
    assert src["tick_age"] == 10


def test_live_passes_through_the_runners_own_github_reachability(home):
    # The runner's probe found GitHub down. The dashboard must show the runner's verdict, not go
    # form a second opinion — a dark tower and a populated board must never contradict each other.
    _heartbeat(home, 10)
    _publish(home, stale=True)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    assert _repo(snap)["github"]["unreachable"] is True


# =============================== FALLBACK ===============================

def test_a_silent_runner_falls_back_to_direct_polling(home):
    _heartbeat(home, SILENT_AFTER + 60)
    _publish(home)
    gh = _CountingGh()
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=gh)
    assert _repo(snap)["source"]["mode"] == "fallback"
    assert _repo(snap)["source"]["reason"] == "runner-silent"
    assert gh.reads, "fallback must actually poll GitHub — a silent runner leaves no other source"


def test_the_fallback_banner_names_both_facts(home):
    _heartbeat(home, SILENT_AFTER + 60)
    _publish(home)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    text = " ".join(_repo(snap)["source"]["banner"]["lines"]).lower()
    assert "silent since" in text
    assert "github" in text and "directly" in text


def test_an_old_engine_that_publishes_nothing_falls_back_and_says_which(home):
    # A pre-#146 runner: ticking happily, publishing nothing. Fallback — but NOT "silent".
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    src = _repo(snap)["source"]
    assert src["mode"] == "fallback"
    assert src["reason"] == "no-published-view"
    assert "silent" not in " ".join(src["banner"]["lines"]).lower()


def test_fallback_still_reports_the_tick_timer(home):
    # "in BOTH modes" — the tick timer is exactly what the owner needs while the runner is quiet.
    _heartbeat(home, SILENT_AFTER + 60)
    _publish(home)
    src = _repo(server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh()))["source"]
    assert src["tick_age"] == SILENT_AFTER + 60


# =============================== the round trip ===============================

def test_fresh_then_stale_then_fresh_returns_to_live_without_a_restart(home):
    # The DoD's transition, driven through the REAL assembler on ONE long-lived server state — the
    # way it happens in life: a runner goes quiet, the dashboard falls back and shouts, the runner
    # comes back, and the next poll is live and silent again. No restart, no sticky banner.
    gh = _CountingGh()
    cfg = _config(home)
    _publish(home)

    _heartbeat(home, 10)
    first = _repo(server.assemble_snapshot(cfg, now=NOW, gh_mod=gh))["source"]
    assert first["mode"] == "live" and first["banner"] is None

    _heartbeat(home, SILENT_AFTER + 60)
    gone = _repo(server.assemble_snapshot(cfg, now=NOW, gh_mod=gh))["source"]
    assert gone["mode"] == "fallback" and gone["banner"] is not None

    _heartbeat(home, 5)
    _publish(home, published_at=NOW - 1, polled_at=NOW - 1)
    back = _repo(server.assemble_snapshot(cfg, now=NOW, gh_mod=gh))["source"]
    assert back["mode"] == "live", "the dashboard never returned to the runner's view"
    assert back["banner"] is None, "the fallback banner outlived the fallback"


def test_returning_to_live_stops_the_egress(home):
    # The recovery must actually stop the polling, not just hide the banner — otherwise the second
    # poller lives on behind a green screen, which is the 07-08 burn wearing a friendlier face.
    gh = _CountingGh()
    cfg = _config(home)
    _publish(home)
    _heartbeat(home, SILENT_AFTER + 60)
    server.assemble_snapshot(cfg, now=NOW, gh_mod=gh)
    assert gh.reads

    _heartbeat(home, 5)
    gh.reads.clear()
    server.assemble_snapshot(cfg, now=NOW, gh_mod=gh)
    assert gh.reads == []


# =============================== the 07-08 burn class ===============================

def test_a_concluded_flight_is_never_read_from_github_in_live_mode(home):
    # i23/i16 are merged. In LIVE their facts come from the document; nothing may go out for them.
    _heartbeat(home, 10)
    _publish(home)
    gh = _CountingGh()
    server.assemble_snapshot(_config(home), now=NOW, gh_mod=gh)
    assert not [r for r in gh.reads if 23 in r or 16 in r]


def test_a_partial_view_never_concludes_a_live_flight(home):
    # The trap, end to end: i15 is HOLDING (open, and neither agent-ready nor in-progress), so it is
    # absent from the runner's partial poll set. Absence must not read as closed — that would land a
    # still-flying plane on the arrivals board.
    _heartbeat(home, 10)
    _publish(home, closed_nums=[23, 16])         # i15 deliberately NOT closed
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    f15 = next(f for f in _repo(snap)["flights"] if f["num"] == 15)
    assert f15["stage"] not in (flights.TOUCHDOWN, flights.TAXI_IN), (
        "an issue merely absent from the runner's partial view was concluded")


# =============================== the settled-PR carry, end to end (review P0) ===============================
# The regression the fresh-agent review caught: the runner's want-set skips TERMINAL_STATUSES, so a
# merged flight's PR is never re-polled and vanished from the published view the tick it landed. In
# LIVE that blanked the arrivals cargo chip (+N/−N/files) and left a merged flight's gate checklist
# unticked — while FALLBACK, which remembers concluded facts (issue #48), still showed them. A
# landing losing its cargo is a §0.1 joy regression traded for plumbing; these pin it shut.

# A merged PR carries a review verdict PINNED to the head it reviewed (#154/#176), and the runner
# publishes that head oid on the PR view — so the board can prove the verdict covers this diff.
_MERGED_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_MERGED_PR = {"number": 25, "state": "MERGED", "mergeable": "MERGEABLE",
              "headRefOid": _MERGED_HEAD,
              "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
              "comments": [{"body": "<!-- superlooper-review sha=%s --> verdict: ok" % _MERGED_HEAD}],
              "files": [{"path": "a.py", "additions": 100, "deletions": 5},
                        {"path": "b.py", "additions": 20, "deletions": 3}]}


def _flight(snap, num):
    return next(f for f in _repo(snap)["flights"] if f["num"] == num)


def test_a_landed_flights_cargo_survives_in_live(home):
    # i23 is merged; its worktree is long gone, so the PR is the ONLY thing that remembers what it
    # carried. The chip must show the real numbers, not an empty +0/−0 that reads as "did nothing".
    _heartbeat(home, 10)
    _publish(home, prs={"i23": _MERGED_PR})
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    cargo = _flight(snap, 23)["cargo"]
    assert cargo["added"] == 120 and cargo["removed"] == 8 and cargo["files"] == 2


def test_a_landed_flights_gate_checklist_is_complete_in_live(home):
    # The runner refuses to merge without the review marker and green CI, so a merged flight showing
    # review/ci unticked would have the dashboard contradicting a known invariant.
    _heartbeat(home, 10)
    _publish(home, prs={"i23": _MERGED_PR})
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    gate = _flight(snap, 23)["gate"]
    assert gate["review"] is True and gate["ci"] is True and gate["mergeable"] is True


# --------------------------- issue #176: the review line reads the pin, not a literal ---------------------------
# The regression the issue asks for: pinned / legacy-unpinned / stale / absent bodies driven all the
# way through assemble_snapshot's _review_state into the flight's gate. Before #176 the board kept a
# private literal and substring-matched it, so a #154 pinned verdict read as "no review" on every
# reviewed PR. These pin the three-way state the board now draws.
_I23_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_I23_OLD_HEAD = "b9c8d7e6f5a41302f1e0d9c8b7a6958473625140"


def _pr_with_review(body, head=_I23_HEAD):
    return {"number": 25, "state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": head,
            "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
            "comments": [{"body": body}] if body is not None else [],
            "files": [{"path": "a.py", "additions": 3, "deletions": 1}]}


def _gate_for(home, pr):
    _heartbeat(home, 10)
    _publish(home, prs={"i23": pr})
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    return _flight(snap, 23)["gate"]


def test_pinned_verdict_for_the_head_reads_reviewed(home):
    """The headline #176 fix: a #154 pinned verdict must tick the review line — the exact case the
    old substring check broke."""
    gate = _gate_for(home, _pr_with_review("<!-- superlooper-review sha=%s --> ok" % _I23_HEAD))
    assert gate["review"] is True and gate["review_state"] == "reviewed"


def test_legacy_unpinned_verdict_reads_stale_not_absent(home):
    """A pre-#154 unpinned marker cannot prove which diff it reviewed. The board shows it as
    'reviewed, then rebuilt' (stale), never a confident tick and never a bare 'never reviewed'."""
    gate = _gate_for(home, _pr_with_review("<!-- superlooper-review --> looked fine"))
    assert gate["review"] is False and gate["review_state"] == "stale"


def test_verdict_pinned_to_a_superseded_head_reads_stale(home):
    """The distinction #176 exists to draw: a verdict pinned to an OLD head is stale, not reviewed —
    the head moved since (a worker rebuilt). The gate would nudge; the board must say so, not tick."""
    gate = _gate_for(home, _pr_with_review("<!-- superlooper-review sha=%s --> gen-1" % _I23_OLD_HEAD))
    assert gate["review"] is False and gate["review_state"] == "stale"


def test_no_review_marker_reads_absent(home):
    gate = _gate_for(home, _pr_with_review("looks good, shipping"))
    assert gate["review"] is False and gate["review_state"] == "absent"


def test_the_settled_carry_still_costs_no_github_reads(home):
    # The fix must come from the document, not from quietly re-opening the egress it closed.
    _heartbeat(home, 10)
    _publish(home, prs={"i23": _MERGED_PR})
    gh = _CountingGh()
    server.assemble_snapshot(_config(home), now=NOW, gh_mod=gh)
    assert gh.reads == []


def test_a_view_without_the_pr_still_fails_closed_not_open(home):
    # An old document (or a PR the runner genuinely never read): the gate must read NOT cleared —
    # blank is honest, a hopeful tick would not be.
    _heartbeat(home, 10)
    _publish(home, prs={})
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_CountingGh())
    assert _flight(snap, 23)["gate"]["cleared"] is False
