"""The LIVE source (issue #146): the runner's published view, answering as a gh adapter.

The dashboard's assembler asks GitHub-shaped questions (open issues, is this one closed, what PR is
on this branch, does it carry a review). In LIVE mode those questions are answered from the
runner's published view — `state/gh_view.json` — and NOTHING goes out to GitHub. `RunnerSource`
keeps the assembler's existing duck-typed gh surface, so the answers change source without the
assembler learning a second way to ask.

The bright line these tests exist to hold: **zero egress**. A RunnerSource that reached gh would
re-create the very defect this issue closes (a second poller on one rate-limit budget — the
2026-07-08 storm, §1b), so it holds no gh reference at all and there is nothing for it to call.

The semantic trap this pins: the runner polls only agent-ready + in-progress issues, so its known
open set is PARTIAL. Absence from it therefore proves NOTHING (a parked issue is open and absent).
Closure must come from the runner's POSITIVE `closed_nums`, never from absence — reading absence as
closure would land a still-flying plane on the arrivals board.
"""
import pytest

import runner_source


def _view(**kw):
    v = {"published_at": 1000, "polled_at": 990, "stale": False,
         "issues": {}, "titles": {}, "closed_nums": [], "prs": {}}
    v.update(kw)
    return v


def _issue(num, title="t", labels=("agent-ready",), body="", created="2026-07-15T10:00:00Z"):
    return {"number": num, "title": title, "labels": [{"name": n} for n in labels],
            "body": body, "createdAt": created}


def src(**kw):
    return runner_source.RunnerSource(_view(**kw))


# --------------------------- zero egress ---------------------------

def test_the_source_holds_no_gh_at_all():
    # Structural, not behavioural: there is no gh handle on the object, so no future edit can quietly
    # add "just one" read behind the LIVE path.
    s = src()
    assert not hasattr(s, "_gh")
    assert not any("gh" in a.lower() for a in vars(s)), vars(s)


# --------------------------- open issues ---------------------------

def test_open_issues_probe_returns_the_published_issues_and_reachable():
    s = src(issues={"i7": _issue(7, "add a widget")})
    lst, reachable = s.open_issues_probe("o/r")
    assert [i["number"] for i in lst] == [7]
    assert reachable is True, "a fresh published view IS the answer — the runner reached GitHub"


def test_a_stale_published_view_reports_unreachable():
    # The runner's own probe found GitHub down. That is the honest signal to pass through: the
    # dashboard's dark-tower state must reflect the RUNNER's reachability, not a second opinion.
    lst, reachable = src(stale=True).open_issues_probe("o/r")
    assert reachable is False


def test_open_issues_filters_by_label():
    s = src(issues={"i7": _issue(7, labels=("agent-ready",)),
                    "i8": _issue(8, labels=("in-progress",))})
    assert [i["number"] for i in s.open_issues("o/r", label="agent-ready")] == [7]


def test_open_issues_unlabelled_returns_everything_known():
    s = src(issues={"i7": _issue(7, labels=("agent-ready",)),
                    "i8": _issue(8, labels=("in-progress",))})
    assert sorted(i["number"] for i in s.open_issues("o/r")) == [7, 8]


def test_the_published_issue_rows_carry_what_a_queue_row_needs():
    # The departures board parses connections out of the body, orders by createdAt, and renders the
    # labels. A row missing these would quietly produce a wrong-ordered or unblocked queue.
    s = src(issues={"i7": _issue(7, "add a widget", body="connections: #3")})
    row = s.open_issues("o/r", label="agent-ready")[0]
    assert row["title"] == "add a widget"
    assert row["body"] == "connections: #3"
    assert row["createdAt"] == "2026-07-15T10:00:00Z"
    assert row["labels"] == [{"name": "agent-ready"}]


# --------------------------- closed: POSITIVE proof only ---------------------------

def test_issue_reads_closed_from_the_runners_closed_nums():
    assert src(closed_nums=[7]).issue("o/r", 7)["state"] == "CLOSED"


def test_an_issue_absent_from_a_partial_view_is_never_reported_closed():
    # THE trap. The runner polls only agent-ready + in-progress, so a parked issue (open, but
    # neither) is absent from `issues` — and absence must not read as CLOSED, or the dashboard would
    # conclude a live flight and land it.
    assert src(issues={}, closed_nums=[]).issue("o/r", 7).get("state") != "CLOSED"


def test_a_known_open_issue_reads_open():
    assert src(issues={"i7": _issue(7)}).issue("o/r", 7)["state"] == "OPEN"


def test_issue_carries_the_title_for_a_closed_flight():
    # A merged flight's issue is closed, so it left the poll set — its title survives via the view's
    # carry. The arrivals board names that landing, so the title must come back with the read.
    s = src(closed_nums=[7], titles={"i7": "add a widget"})
    assert s.issue("o/r", 7)["title"] == "add a widget"


def test_an_unknown_issue_reads_as_an_empty_answer():
    # Never invent. The assembler's callers all fail closed on {}, which is the honest "the runner
    # doesn't know" — not a fabricated OPEN/CLOSED verdict.
    assert src().issue("o/r", 999) == {}


# --------------------------- PRs ---------------------------

def test_pr_for_branch_returns_the_runners_pr_view():
    pr = {"number": 12, "state": "OPEN", "mergeable": "MERGEABLE"}
    s = src(prs={"i7": pr})
    got = s.pr_for_branch("o/r", "sl/i7-a-thing")
    assert got["number"] == 12 and got["mergeable"] == "MERGEABLE"


def test_pr_for_branch_finds_the_pr_by_the_branchs_issue_id():
    # The view keys PRs by issue id (the runner's native key); the assembler asks by branch name.
    # Every loop branch is `sl/i<N>-…`, which is what makes the mapping honest.
    s = src(prs={"i146": {"number": 5, "state": "OPEN"}})
    assert s.pr_for_branch("o/r", "sl/i146-dashboard-render-the-runner-s-own-view")["number"] == 5


def test_pr_for_branch_on_an_unknown_branch_is_an_empty_answer():
    assert src(prs={}).pr_for_branch("o/r", "sl/i7-x") == {}


def test_a_branch_that_is_not_a_loop_branch_never_raises():
    assert src(prs={}).pr_for_branch("o/r", "some-hand-made-branch") == {}


def test_pr_size_is_derived_from_the_runners_file_list():
    # The runner's PR read carries `files`; the dashboard's cargo chip wants additions/deletions/
    # changedFiles. Derive rather than ask GitHub again — the whole point of one source.
    pr = {"number": 12, "state": "MERGED",
          "files": [{"path": "a.py", "additions": 10, "deletions": 2},
                    {"path": "b.py", "additions": 5, "deletions": 1}]}
    got = src(prs={"i7": pr}).pr_for_branch("o/r", "sl/i7-x")
    assert got["additions"] == 15
    assert got["deletions"] == 3
    assert got["changedFiles"] == 2


def test_pr_size_is_absent_when_the_runner_never_read_the_files():
    # Absent ≠ zero. A PR view with no file list must not render as an empty diff (`+0/−0`), which
    # would look like a worker that did nothing.
    got = src(prs={"i7": {"number": 12, "state": "OPEN"}}).pr_for_branch("o/r", "sl/i7-x")
    assert "additions" not in got and "changedFiles" not in got


def test_a_wrong_typed_file_list_never_raises():
    for bad in ("nope", 7, [{"additions": "x"}], [None]):
        got = src(prs={"i7": {"number": 1, "files": bad}}).pr_for_branch("o/r", "sl/i7-x")
        assert got["number"] == 1


# --------------------------- review evidence ---------------------------

def test_pr_comments_come_from_the_runners_own_read():
    pr = {"number": 12, "comments": [{"body": "<!-- superlooper-review --> ok"}]}
    assert src(prs={"i7": pr}).pr_comments("o/r", 12) == [{"body": "<!-- superlooper-review --> ok"}]


def test_pr_comments_are_empty_when_the_runner_has_not_read_them():
    # The runner OMITS a refused comments read (issues #61/#78), so absence means "not read", and
    # the dashboard's review line fails closed to not-passed — the same direction the gate takes.
    assert src(prs={"i7": {"number": 12}}).pr_comments("o/r", 12) == []


def test_pr_comments_for_an_unknown_pr_are_empty():
    assert src().pr_comments("o/r", 999) == []


# --------------------------- fail-closed on junk ---------------------------

@pytest.mark.parametrize("bad", [None, "x", [], 7])
def test_a_wrong_typed_view_never_raises(bad):
    s = runner_source.RunnerSource(bad)
    assert s.open_issues("o/r") == []
    assert s.issue("o/r", 1) == {}
    assert s.pr_for_branch("o/r", "sl/i1-x") == {}
    assert s.pr_comments("o/r", 1) == []


def test_a_wrong_typed_issue_row_is_skipped():
    s = src(issues={"i7": "not a dict", "i8": _issue(8)})
    assert [i["number"] for i in s.open_issues("o/r")] == [8]
