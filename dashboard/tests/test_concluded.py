"""Issue #48 — concluded flights stop consuming GitHub budget (fetch once, remember).

A CONCLUDED flight (status ``merged``, or its issue closed) can never change: its PR, its title,
and its posted review are settled facts. Yet the snapshot re-asked GitHub for every one of them on
the ``gh_poll_seconds`` clock, forever — a cost that grew with every landing and helped drain the
hourly quota (2026-07-08). ``ConcludedFlights`` remembers those facts so a concluded flight is
fetched AT MOST ONCE per dashboard run, while IN-FLIGHT flights keep riding the normal clock.

These tests pin the call-count contract with a counting gh stub across many repeated assemblies —
the only honest proof that the re-asking actually stopped.
"""
import json

import flights
import server

SLUG = "will-titan/cc"
NOW = 1783364300
# A PR's current head oid, and the pinned review verdict a worker posts for it (#154/#176). The
# verdict names the head it reviewed, so the board can prove it covers THIS diff.
HEAD_OID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_PINNED_REVIEW = "<!-- superlooper-review sha=%s --> ok" % HEAD_OID


def _make_home(tmp_path, issues, journal=()):
    dst = tmp_path / "will-titan__cc"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": issues}))
    (dst / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in journal))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 10))
    return dst


def _config(home):
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300,
            "repos": [{"slug": SLUG, "name": "cc", "state_home": str(home),
                       "required_checks": ["tests"]}]}


# One merged flight (concluded) and one running flight (in-flight), both with a branch + PR.
_ISSUES = {
    "i16": {"status": "merged", "branch": "sl/i16-x", "pr": 19, "lane": None},
    "i9": {"status": "running", "branch": "sl/i9-x", "pr": 20, "lane": "i9"},
}


class _CountingGh:
    """A gh stub that counts every read per key, so a test proves exactly how often each question is
    asked. ``open_nums`` is what the (capped, fail-closed) open-issue LIST returns; ``closed_nums``
    is what a POSITIVE per-issue ``issue()`` read reports as CLOSED — deliberately DECOUPLED, because
    a gh outage empties the list while ``issue()`` still reports the truth, and conclusion must key on
    the positive signal, never on mere absence from the list (issue #48 Codex review). Every branch
    has a settled (MERGED) PR carrying a diff size, so a concluded flight's cargo comes from its PR.
    ``pr_empty_for`` forces an empty (fail-closed) PR read for a branch until it is cleared."""

    def __init__(self, open_nums, closed_nums=None, pr_empty_for=None):
        self._open = set(open_nums)
        self._closed = set(closed_nums) if closed_nums is not None else set()
        self._empty = set(pr_empty_for or ())
        self.pr_calls = {}
        self.issue_calls = {}
        self.review_calls = {}

    def open_issues(self, repo, label=None, limit=200):
        if label == "agent-ready":
            return []
        return [{"number": n, "title": "issue %d" % n} for n in self._open]

    def issue(self, repo, num):
        self.issue_calls[num] = self.issue_calls.get(num, 0) + 1
        return {"number": num, "title": "issue %d" % num,
                "state": "CLOSED" if num in self._closed else "OPEN"}

    def pr_for_branch(self, repo, branch):
        self.pr_calls[branch] = self.pr_calls.get(branch, 0) + 1
        if branch in self._empty:
            return {}
        return {"number": 19, "state": "MERGED", "mergeable": "MERGEABLE", "statusCheckRollup": [],
                "headRefName": branch, "headRefOid": HEAD_OID,
                "additions": 100, "deletions": 20, "changedFiles": 4}

    def pr_comments(self, repo, num):
        self.review_calls[num] = self.review_calls.get(num, 0) + 1
        return [{"body": _PINNED_REVIEW}]


def _assemble_n(cfg, gh, mem, n):
    for _ in range(n):
        snap = server.assemble_snapshot(cfg, now=NOW, gh_mod=gh, concluded=mem)
    return snap


# =============================== the budget contract ===============================

def test_concluded_flight_pr_is_fetched_once_but_in_flight_refetches(tmp_path):
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9})            # i9 open (in-flight); i16 closed+merged (concluded)
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 6)
    assert gh.pr_calls["sl/i16-x"] == 1        # concluded: asked ONCE, remembered for the whole run
    assert gh.pr_calls["sl/i9-x"] == 6         # in-flight: re-read every assembly (the clock's job)


def test_concluded_flight_title_and_review_are_fetched_once(tmp_path):
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9})
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 6)
    assert gh.issue_calls.get(16) == 1         # the closed issue's title: asked once
    assert gh.review_calls.get(19) == 1        # the merged PR's review: asked once


def test_in_flight_review_is_not_permanently_remembered(tmp_path):
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9})
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 4)
    assert gh.review_calls.get(20) == 4        # in-flight PR review re-read every assembly, unchanged


def test_a_closed_but_unmerged_flight_counts_as_concluded(tmp_path):
    # A dropped/closed issue (not "merged" in issues.json) is still concluded once a POSITIVE issue()
    # read confirms CLOSED — its PR is then fetched once and remembered.
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": 20, "lane": "i9"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums=set(), closed_nums={9})   # absent from list AND confirmed CLOSED
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 5)
    assert gh.pr_calls["sl/i9-x"] == 1


def test_gh_outage_does_not_conclude_a_running_flight(tmp_path):
    # THE fail-closed guard (Codex review): during a gh outage the open-issue LIST fail-closes to []
    # so every flight is "absent", yet a positive issue() read still reports the running flight OPEN.
    # Absence alone must NOT conclude it — else its PR facts would freeze mid-flight for the whole run.
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": 20, "lane": "i9"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums=set(), closed_nums=set())   # list empty (outage) but issue() says OPEN
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 5)
    assert gh.pr_calls["sl/i9-x"] == 5    # still live: re-read every poll, never frozen into memory


def test_pr_memory_does_not_lock_in_an_unsettled_open_read():
    # Codex review: in production the fetch rides CachedGh, so the FIRST concluded read can return a
    # ≤30s-stale OPEN PR dict. Remembering that would freeze pre-merge facts for the run. The memory
    # remembers a PR only once it reads a SETTLED state (MERGED/CLOSED); a stale OPEN is re-read.
    mem = server.ConcludedFlights()
    calls = []

    def open_read():
        calls.append("open")
        return {"number": 9, "state": "OPEN", "additions": 10, "deletions": 1, "changedFiles": 2}

    def merged_read():
        calls.append("merged")
        return {"number": 9, "state": "MERGED", "additions": 10, "deletions": 1, "changedFiles": 2}

    assert mem.pr_facts("r", "b", open_read)["state"] == "OPEN"      # returned, but NOT remembered
    assert mem.pr_facts("r", "b", merged_read)["state"] == "MERGED"  # settled → remembered
    assert mem.pr_facts("r", "b", merged_read)["state"] == "MERGED"  # served from memory, no re-read
    assert calls == ["open", "merged"]                              # open re-read once, merged once


def test_a_transient_empty_read_is_retried_then_locked_in(tmp_path):
    # If gh is down the moment a flight concludes, the first read is empty — the memory must NOT lock
    # that in (which would blank the flight's cargo forever); it retries until a real answer, then
    # remembers. Self-healing, never permanently wrong.
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9}, pr_empty_for={"sl/i16-x"})
    mem = server.ConcludedFlights()
    _assemble_n(_config(home), gh, mem, 3)
    assert gh.pr_calls["sl/i16-x"] == 3        # still retrying while the read is empty
    gh._empty.clear()                          # gh recovers
    _assemble_n(_config(home), gh, mem, 3)
    assert gh.pr_calls["sl/i16-x"] == 4        # one real read after recovery, then locked in forever


def test_concluded_cargo_survives_landing_in_the_snapshot(tmp_path):
    # The payoff: a merged flight whose worktree is long gone still shows real +100/−20 cargo, read
    # from its PR and remembered — retroactively, no live worktree needed.
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9})
    mem = server.ConcludedFlights()
    snap = _assemble_n(_config(home), gh, mem, 2)
    landed = next(f for f in snap["flights"] if f["num"] == 16)
    assert landed["cargo"] == {"present": True, "files": 4, "added": 100, "removed": 20}
    assert landed["display"]["diff"] == "+100/−20"      # boring-mode Δ diff: real, not an em-dash


def test_no_memory_falls_back_to_per_poll_fetch(tmp_path):
    # ConcludedFlights is optional: with concluded=None the assembler behaves exactly as before —
    # every flight re-read each assembly (backward compatible for embedders that don't wire it).
    home = _make_home(tmp_path, _ISSUES)
    gh = _CountingGh(open_nums={9})
    for _ in range(3):
        server.assemble_snapshot(_config(home), now=NOW, gh_mod=gh, concluded=None)
    assert gh.pr_calls["sl/i16-x"] == 3
