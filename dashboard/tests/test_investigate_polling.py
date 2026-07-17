"""Issue #16 — investigation flights stop consuming GitHub PR budget.

An ``investigate`` flight NEVER opens a PR and never merges: its completion signal is a marker
COMMENT on the issue, not a branch->PR association. The runner already knows this and skips
``pr_for_branch`` on every one of its three PR paths (issue #21). The dashboard did not — snapshot
assembly asked GitHub for PR facts on every investigation branch, running and concluded alike.

Concluded investigations are the ones that bite. ``ConcludedFlights.pr_facts`` only remembers a
SETTLED read (state MERGED/CLOSED), and an investigation's honest answer is ``{}`` — so the #48
"fetch once, remember" memo can never latch it, and the read repeats every ``gh_poll_seconds``
window FOREVER. That is the same shape as the concluded-flight polling bug #48 bought off, and it
grows linearly with every investigation the loop completes.

These tests pin the call-count contract with a counting gh stub across repeated assemblies — the
only honest proof that the asking actually stopped — and pin that the BUILD path is untouched.
"""
import json

import server

SLUG = "will-titan/cc"
NOW = 1783364300
HEAD_OID = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_PINNED_REVIEW = "<!-- superlooper-review sha=%s --> ok" % HEAD_OID


def _make_home(tmp_path, issues):
    dst = tmp_path / "will-titan__cc"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": issues}))
    (dst / "journal.jsonl").write_text("")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 10))
    return dst


def _config(home):
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300,
            "repos": [{"slug": SLUG, "name": "cc", "state_home": str(home),
                       "required_checks": ["tests"]}]}


class _CountingGh:
    """Counts every PR lookup per branch, so a test can prove a question is never asked.
    ``open_nums`` is what the open-issue LIST returns; ``closed_nums`` is what a positive per-issue
    ``issue()`` read reports CLOSED (the signal #48 requires before concluding a flight)."""

    def __init__(self, open_nums, closed_nums=None, no_pr_for=()):
        self._open = set(open_nums)
        self._closed = set(closed_nums) if closed_nums is not None else set()
        self._no_pr = set(no_pr_for)
        self.pr_calls = {}

    def open_issues(self, repo, label=None, limit=200):
        if label == "agent-ready":
            return []
        return [{"number": n, "title": "issue %d" % n} for n in self._open]

    def issue(self, repo, num):
        return {"number": num, "title": "issue %d" % num,
                "state": "CLOSED" if num in self._closed else "OPEN"}

    def pr_for_branch(self, repo, branch):
        self.pr_calls[branch] = self.pr_calls.get(branch, 0) + 1
        if branch in self._no_pr:
            return {}       # GitHub's honest answer for an investigation branch: no PR exists
        return {"number": 19, "state": "MERGED", "mergeable": "MERGEABLE", "statusCheckRollup": [],
                "headRefName": branch, "headRefOid": HEAD_OID,
                "additions": 100, "deletions": 20, "changedFiles": 4}

    def pr_comments(self, repo, num):
        return [{"body": _PINNED_REVIEW}]


def _assemble_n(cfg, gh, mem, n):
    for _ in range(n):
        snap = server.assemble_snapshot(cfg, now=NOW, gh_mod=gh, concluded=mem)
    return snap


def _flight(snap, num):
    return next(f for f in snap["flights"] if f["num"] == num)


# =============================== the budget contract ===============================

def test_running_investigation_is_never_asked_for_pr_facts(tmp_path):
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": None,
                     "lane": "i9", "type": "investigate"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums={9}, no_pr_for={"sl/i9-x"})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 6)
    assert gh.pr_calls == {}, "an investigation never opens a PR — never ask GitHub for one"


def test_concluded_investigation_is_never_asked_for_pr_facts(tmp_path):
    # THE issue-#16 case. A concluded investigation's honest PR answer is {}, which
    # ConcludedFlights.pr_facts refuses to remember (it keeps only a SETTLED MERGED/CLOSED read) —
    # so before the fix this re-asked GitHub every gh_poll_seconds window, forever, once per
    # completed investigation. Six assemblies would mean six reads.
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": None,
                     "lane": None, "type": "investigate"}}
    home = _make_home(tmp_path, issues)
    # positively CLOSED ⇒ concluded; and GitHub honestly answers "no PR" — the un-memoizable read
    gh = _CountingGh(open_nums=set(), closed_nums={9}, no_pr_for={"sl/i9-x"})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 6)
    assert gh.pr_calls == {}


def test_investigation_marked_merged_is_never_asked_for_pr_facts(tmp_path):
    # The other conclusion route: the runner's own settled word. Real state homes carry investigate
    # flights stamped status "merged" (i1 on the live loop), so this path must skip too.
    issues = {"i9": {"status": "merged", "branch": "sl/i9-x", "pr": None,
                     "lane": None, "type": "investigate"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums=set(), no_pr_for={"sl/i9-x"})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 6)
    assert gh.pr_calls == {}


# =============================== the build path is untouched ===============================

def test_build_flight_still_resolves_pr_facts(tmp_path):
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": 20,
                     "lane": "i9", "type": "build"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums={9})
    snap = _assemble_n(_config(home), gh, server.ConcludedFlights(), 4)
    assert gh.pr_calls["sl/i9-x"] == 4          # in-flight build: re-read every assembly, as before
    # The PR facts still reach the board: cargo is the PR's diff size and the gate reads its
    # mergeable/review state — both blank if the lookup had been skipped.
    assert _flight(snap, 9)["cargo"] == {"present": True, "files": 4, "added": 100, "removed": 20}
    assert _flight(snap, 9)["gate"]["mergeable"] is True
    assert _flight(snap, 9)["gate"]["review_state"] == "reviewed"


def test_diagnose_and_fix_flight_still_resolves_pr_facts(tmp_path):
    # diagnose-and-fix DOES open a PR (launch_rules.TYPE_KINDS) — the skip is investigate-only.
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": 20,
                     "lane": "i9", "type": "diagnose-and-fix"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums={9})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 3)
    assert gh.pr_calls["sl/i9-x"] == 3


def test_untyped_flight_still_resolves_pr_facts(tmp_path):
    # A flight with no type stamp is NOT provably an investigation — fail open to the build
    # behavior rather than silently blanking a real PR's facts.
    issues = {"i9": {"status": "running", "branch": "sl/i9-x", "pr": 20, "lane": "i9"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums={9})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 3)
    assert gh.pr_calls["sl/i9-x"] == 3


def test_a_concluded_build_alongside_an_investigation_still_fetches_once(tmp_path):
    # The mixed field: the #48 memo keeps working for builds while investigations cost nothing.
    issues = {"i9": {"status": "merged", "branch": "sl/i9-x", "pr": 20,
                     "lane": None, "type": "build"},
              "i16": {"status": "merged", "branch": "sl/i16-x", "pr": None,
                      "lane": None, "type": "investigate"}}
    home = _make_home(tmp_path, issues)
    gh = _CountingGh(open_nums=set(), no_pr_for={"sl/i16-x"})
    _assemble_n(_config(home), gh, server.ConcludedFlights(), 5)
    assert gh.pr_calls["sl/i9-x"] == 1         # concluded build: fetched once, remembered (#48)
    assert "sl/i16-x" not in gh.pr_calls       # concluded investigation: never fetched at all
