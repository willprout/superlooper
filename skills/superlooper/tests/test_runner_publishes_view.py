"""The runner WRITES its GitHub view down every tick (issue #146) — the engine half.

`published_view.build` (test_published_view.py) shapes the document; these tests pin that the
runner actually publishes it, on the tick, with the disciplines the dashboard depends on:

  * every tick republishes — the document's `published_at` is how the dashboard ages the data it
    renders, so a stalled document must be visible as a stalled document;
  * `polled_at` tracks the GitHub poll, NOT the tick — the data is only as fresh as the last
    successful read (up to GH_POLL_SECONDS older than the tick that copied it out);
  * publishing can never wedge the tick. It runs before the heartbeat stamp, and the heartbeat is
    the loop's dead-man's switch: a publish failure must cost the document, never the loop
    (the class the 2026-07-07 binary-file incident bought off);
  * the state-format stamp names the new shape, so an OLD dashboard reading this NEW home says so
    out loud (issue #45's handshake) instead of silently rendering a home it can't fully read.

Same rig as test_runner.py: fake-gh via SL_GH, injected run_script, no real GitHub.
"""
import json
import shutil
from pathlib import Path

import pytest

import loopstate
import runner as runner_mod

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

NOW = 1_750_000_000

VIEW = ("state", "gh_view.json")


def make_config(**over):
    c = {
        "repo": "o/r", "dev_branch": "main", "prod_branch": None,
        "lanes": 2, "affinity": "hard", "areas": {},
        "touches_required": False, "required_checks": ["ci"], "merge_method": "squash",
        "ship_cmd": None, "ship_recheck_cmd": None,
        "report_required_sections": ["Tests"], "bright_lines": [],
        "cleanup_merged_worktrees": True, "report_time": "08:45",
        "models": {"worker": "opus", "debugger": "fable"},
        "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2},
        "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
               "quarantine": [], "nightly_time": "02:00"},
        "notify": {"imessage_to": None, "cmd": None},
        "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
    }
    c.update(over)
    return c


@pytest.fixture
def rig(tmp_path, monkeypatch):
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))
    monkeypatch.delenv("GH_FAIL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    r = runner_mod.Runner(
        repo=str(repo), config=make_config(), state_home=str(home), pane="pane-1",
        run_script=lambda *a, **k: 0,
        fetch_usage=lambda: {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0})
    r._anchor_status = lambda: {"ok": True, "reason": ""}
    return type("Rig", (), {"r": r, "home": home, "repo": repo, "fixdir": fixdir})


def view(rig):
    return json.loads((rig.home / Path(*VIEW)).read_text())


def test_a_tick_publishes_the_view(rig):
    rig.r.tick(now=NOW)
    doc = view(rig)
    assert doc["published_at"] == int(NOW)
    # The shape the dashboard binds — present even on a quiet loop, so a reader can always tell a
    # published-but-empty view from no view at all.
    for k in ("polled_at", "stale", "issues", "titles", "closed_nums", "prs"):
        assert k in doc, k


def test_every_tick_republishes_so_the_data_age_is_honest(rig):
    rig.r.tick(now=NOW)
    rig.r.tick(now=NOW + 15)
    # The tick is what the dashboard's "data age" is measured from; a document that stopped moving
    # while the loop ran would read as fresh forever.
    assert view(rig)["published_at"] == int(NOW + 15)


def test_polled_at_tracks_the_github_poll_not_the_tick(rig):
    rig.r.tick(now=NOW)
    first = view(rig)
    assert first["polled_at"] == int(NOW)
    # A tick INSIDE the poll window reuses the last read (GH_POLL_SECONDS throttle), so the tick
    # advances but the DATA does not — and the document must say so, or the dashboard would age
    # GitHub data by the tick clock and call a 90s-old answer current.
    rig.r.tick(now=NOW + 15)
    later = view(rig)
    assert later["published_at"] == int(NOW + 15)
    assert later["polled_at"] == int(NOW), "polled_at must age with the poll, not the tick"


def test_the_published_view_carries_the_polled_issues(rig):
    rig.r.tick(now=NOW)
    doc = view(rig)
    # The fixture repo has open agent-ready issues; whatever they are, each published row must carry
    # the identity the dashboard renders. (The fixture's exact contents are pinned in test_runner.)
    assert doc["issues"], "the runner polled issues but published none"
    for iid, row in doc["issues"].items():
        assert isinstance(row.get("number"), int), iid


def test_a_tracked_issues_title_survives_it_leaving_the_poll_set(rig):
    rig.r.tick(now=NOW)
    doc = view(rig)
    iid = next(iter(doc["titles"]), None)
    assert iid, "expected at least one polled title to carry"
    title = doc["titles"][iid]

    # Track the issue in loopstate (as a merged flight is), then make GitHub answer with nothing —
    # the issue has left the poll set exactly as a closed one does.
    def m(st):
        st["issues"].setdefault(iid, loopstate.new_issue())["status"] = "merged"
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    rig.r._parsed_by_id, rig.r._raw_by_id = {}, {}
    rig.r._last_poll = NOW + 1000        # inside the window: no re-poll, the view stays as-is
    rig.r.tick(now=NOW + 1000)
    # The arrivals board still names this landing, so the title must outlive the poll set.
    assert view(rig)["titles"].get(iid) == title


def test_publish_failure_never_wedges_the_tick_or_the_heartbeat(rig, monkeypatch):
    # The heartbeat is the dead-man's switch and is stamped LAST; a publish that raises must not
    # steal it, or a healthy loop would read as dead (2026-07-07 class).
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(runner_mod.loopstate, "save", boom)
    rig.r.tick(now=NOW)                                  # must not raise
    assert (rig.home / "state" / "runner.heartbeat").read_text().strip() == str(int(NOW))


def test_the_engine_stamps_the_state_format_that_names_this_shape():
    # The home now carries state/gh_view.json, which a pre-#146 dashboard doesn't read. The stamp is
    # what makes that mismatch LOUD instead of silent (issue #45), so the version must have moved.
    assert runner_mod.STATE_FORMAT_VERSION >= 2


# --------------------------- the landing sequence, as the runner actually performs it ---------------------------
# The test whose ABSENCE let a broken carry pass twice (fresh-agent review, round 2). Every earlier
# carry test handed `build` a PR already reading MERGED — the one input the runner never produces.
# The gate can only merge a PR that reads OPEN + MERGEABLE + green, so the cached read at the moment
# of merging says OPEN, and `_exec_merge` writes the landing to LOOPSTATE, never back into gh_view.
# The next poll's want-set skips the now-terminal issue and its PR leaves the view for good.
#
# So this drives the real order — cached OPEN read, loopstate merged, poll drops it — through the
# runner's own `_publish_view`. It fails against a carry that waits for gh to say "MERGED", because
# gh is never asked again.

def _seed_merged(rig, iid, pr):
    """The runner's state on a tick AFTER a landing: loopstate says merged (`_exec_merge` wrote it
    on some earlier tick), and the cached PR is still the pre-merge OPEN read the gate acted on —
    nothing ever writes the merge back into gh_view, and the issue is terminal now, so it is never
    polled again.

    The LANDING tick itself is driven end-to-end further down (issue #276): these seeded cases pin
    that the settle keeps holding on every later tick, which is what the arrivals board reads."""
    def m(st):
        st["issues"].setdefault(iid, loopstate.new_issue()).update(
            {"status": "merged", "branch": "sl/%s-a-thing" % iid, "pr": pr["number"]})
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    rig.r.gh_view = {**rig.r.gh_view, "prs": {iid: pr}, "stale": False}
    rig.r._last_poll = NOW + 10_000          # inside the window: no re-poll will rebuild `prs`


_PRE_MERGE_READ = {"number": 25, "state": "OPEN", "mergeable": "MERGEABLE",
                   "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
                   "files": [{"path": "a.py", "additions": 100, "deletions": 5},
                             {"path": "b.py", "additions": 20, "deletions": 3}]}


def test_the_tick_after_a_landing_keeps_its_pr_facts(rig):
    _seed_merged(rig, "i15", _PRE_MERGE_READ)
    rig.r.tick(now=NOW + 10_000)
    pr = view(rig)["prs"].get("i15")
    assert pr, "the flight the runner just merged published no PR facts"
    assert pr["state"] == "MERGED"
    assert (pr["additions"], pr["deletions"], pr["changedFiles"]) == (120, 8, 2)


def test_the_landings_pr_facts_survive_the_poll_that_forgets_it(rig):
    # The window that actually broke: the next poll rebuilds `prs` from the want-set, which skips a
    # terminal issue outright — so the PR is gone from the live view and ONLY the carry can hold it.
    # The empty-`prs` assignment below MODELS that skip rather than driving `_poll` (which would need
    # a live GitHub answer); the behaviour it stands in for is `runner.py`'s want-set loop, which
    # `continue`s on `status in actions.TERMINAL_STATUSES`. That is the pinned assumption here.
    _seed_merged(rig, "i15", _PRE_MERGE_READ)
    rig.r.tick(now=NOW + 10_000)                     # publishes, seeding the carry
    rig.r.gh_view = {**rig.r.gh_view, "prs": {}}     # the poll drops the terminal issue
    rig.r.tick(now=NOW + 10_015)
    pr = view(rig)["prs"].get("i15")
    assert pr and pr["state"] == "MERGED", "the cargo chip blanks one poll window after landing"
    assert pr["additions"] == 120


def test_the_landings_facts_still_stand_many_ticks_later(rig):
    # A landed flight's chip is meant to outlive the flight — its worktree is gone, so the PR is the
    # only thing that remembers. Re-carrying must be a fixed point, not a slow fade.
    _seed_merged(rig, "i15", _PRE_MERGE_READ)
    rig.r.tick(now=NOW + 10_000)
    rig.r.gh_view = {**rig.r.gh_view, "prs": {}}
    for i in range(6):
        rig.r.tick(now=NOW + 10_015 + i * 15)
    pr = view(rig)["prs"].get("i15")
    assert pr and pr["additions"] == 120 and pr["state"] == "MERGED"


def test_a_parked_flights_open_pr_is_not_frozen_into_the_view(rig):
    # The other half of the discipline: only a merge the RUNNER recorded promotes an OPEN read. A
    # parked flight's PR can still change, so freezing its green CI would be a false clearance.
    def m(st):
        st["issues"].setdefault("i15", loopstate.new_issue()).update(
            {"status": "parked", "branch": "sl/i15-a-thing", "pr": 25})
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    rig.r.gh_view = {**rig.r.gh_view, "prs": {"i15": dict(_PRE_MERGE_READ)}, "stale": False}
    rig.r._last_poll = NOW + 10_000
    rig.r.tick(now=NOW + 10_000)
    rig.r.gh_view = {**rig.r.gh_view, "prs": {}}
    rig.r.tick(now=NOW + 10_015)
    assert "i15" not in view(rig)["prs"]


# --------------------------- the LANDING tick itself (issue #276) ---------------------------
# Everything above seeds a landing that already happened and drives the ticks after it. This drives
# the landing itself — a real gating lane, through decide, through `_exec_merge`, on one tick — and
# pins that the document THAT tick publishes already names the merge.
#
# It has to be an end-to-end tick, because the bug it guards is purely one of ORDER inside `tick`:
# the issue map is loaded at the top, the executors move statuses in the middle, and publishing
# reads that map at the bottom. Any test that hands `_publish_view` a map directly cannot see it.

_LANDING_REPORT = "## Tests\n" + "all green, evidence attached " * 4


_LANDING_TITLE = "Render the widget"


def _seed_landing_lane(rig, in_the_poll_set=False):
    """The fixture flight the gate can actually land on the very next tick: issue #123's PR 555
    reads OPEN + MERGEABLE with a green `quality-gate` rollup and a review marker pinned to its
    head oid (tests/fixtures/gh), and the lane has a report with the required section.

    ``in_the_poll_set`` puts the issue in the ``in-progress`` queue too, which is where a live
    gating lane actually is — that is what gives the published document a TITLE for it to carry."""
    rig.r.config = make_config(required_checks=["quality-gate"])

    def m(st):
        st["issues"].setdefault("i123", loopstate.new_issue()).update(
            {"status": "gating", "branch": "sl/i123-render-the-widget", "num": 123,
             "type": "build", "declared_touches": []})
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    (rig.home / "reports" / "i123.md").write_text(_LANDING_REPORT)
    if in_the_poll_set:
        (rig.fixdir / "issue_list_in-progress.json").write_text(json.dumps([{
            "number": 123, "title": _LANDING_TITLE, "createdAt": "2026-07-01T09:00:00Z",
            "labels": [{"name": "type:build"}, {"name": "in-progress"}],
            "body": "## Goal\nRender the widget.\n\n## Loop metadata\ntouches: frontend\n"}]))


def _status(rig, iid):
    return loopstate.load(str(rig.home / "state" / "issues.json"))["issues"][iid]["status"]


def test_the_tick_that_merges_a_lane_publishes_it_as_merged(rig, monkeypatch):
    # The published view is what the dashboard renders, and it polls every ~2s behind a runner that
    # ticks every ~15s — so a landing missing from THIS tick's document is a PR the loop has already
    # merged rendering as still in flight for a whole tick's worth of polls.
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    _seed_landing_lane(rig)
    rig.r.tick(now=NOW)
    assert _status(rig, "i123") == "merged", "the fixture flight never landed — this test drives nothing"
    pr = view(rig)["prs"].get("i123")
    assert pr, "the flight merged on this tick published no PR facts at all"
    assert pr["state"] == "MERGED", "the tick that merged the lane still published its PR as OPEN"


def test_the_landing_ticks_own_document_carries_the_flights_cargo(rig, monkeypatch):
    # The chip the arrivals board draws, on the tick it becomes true. The fixture PR's two files are
    # +40/−2 and +18/−0, and a settled entry collapses them to the three totals.
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    _seed_landing_lane(rig)
    rig.r.tick(now=NOW)
    pr = view(rig)["prs"]["i123"]
    assert (pr["additions"], pr["deletions"], pr["changedFiles"]) == (58, 2, 2)


def test_a_real_landings_title_and_pr_outlive_the_poll_that_forgets_it(rig, monkeypatch):
    # The other half of the change (issue #276): the map the publish reads is not only the merged
    # set, it is also the TRACKED set that bounds both carries. Reading it post-execute must not
    # drop a lane that just went terminal — that lane is exactly the one whose title and PR the
    # board still wants, and the next poll's want-set skips it outright.
    #
    # This holds BEFORE and AFTER the change, which is the point: it is the invariant the change
    # had to keep, driven off a real landing rather than a seeded one (the seeded siblings above
    # never exercise the map the runner itself hands the publish).
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    _seed_landing_lane(rig, in_the_poll_set=True)
    rig.r.tick(now=NOW)
    assert _status(rig, "i123") == "merged", "the fixture flight never landed — this test drives nothing"
    assert view(rig)["titles"].get("i123") == _LANDING_TITLE

    # ...and now GitHub forgets it, exactly as it does for a closed issue: the want-set skips a
    # terminal lane, so neither the issue nor its PR is read again. Only the carry can hold them.
    rig.r._parsed_by_id, rig.r._raw_by_id = {}, {}
    rig.r.gh_view = {**rig.r.gh_view, "prs": {}}
    rig.r._last_poll = NOW + 10_000              # inside the window: no re-poll rebuilds either map
    rig.r.tick(now=NOW + 15)
    doc = view(rig)
    assert doc["titles"].get("i123") == _LANDING_TITLE, "the landing lost its carried title"
    assert doc["prs"].get("i123", {}).get("state") == "MERGED", "the landing lost its carried PR"


def test_an_unreadable_post_execute_state_read_never_prunes_the_carries(rig, monkeypatch):
    """The failure the post-execute read opens, and the fallback that closes it (fresh-agent
    review of #276).

    `_load_state` fails CLOSED to an empty state, so an unreadable loopstate and a loopstate that
    tracks nothing reach the publish as the same empty map — and an empty tracked set prunes every
    carried title and settled PR, out of the document AND out of the in-memory carry that re-seeds
    it. One bad read would blank every landed flight on the arrivals board for good. So a
    post-execute read that comes back empty where the pre-execute one had lanes falls back to the
    pre-execute map: nothing removes an issue from loopstate, so that emptiness is never real."""
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    _seed_landing_lane(rig, in_the_poll_set=True)
    rig.r.tick(now=NOW)                              # lands: the document now carries title + PR
    assert view(rig)["prs"]["i123"]["state"] == "MERGED"

    # GitHub forgets the now-terminal issue (the want-set skips it), so ONLY the carry holds it...
    rig.r._parsed_by_id, rig.r._raw_by_id = {}, {}
    rig.r.gh_view = {**rig.r.gh_view, "prs": {}}
    rig.r._last_poll = NOW + 10_000                  # inside the window: no poll, so exactly two
                                                     # state reads this tick — disk_view's, then
                                                     # the post-execute one
    reads = []
    real_load = rig.r._load_state

    def flaky():                                     # the top-of-tick read lands; every later one
        reads.append(1)                              # comes back unreadable -> fail-closed empty
        return real_load() if len(reads) == 1 else loopstate.new_state()

    rig.r._load_state = flaky
    rig.r.tick(now=NOW + 15)
    assert len(reads) >= 2, "the post-execute read never happened — this test proves nothing"
    doc = view(rig)
    assert doc["titles"].get("i123") == _LANDING_TITLE, "an unreadable read pruned the carried title"
    assert doc["prs"].get("i123", {}).get("state") == "MERGED", \
        "an unreadable read pruned the landed flight's PR"


# ======================= the lane's current phase, on the tick (issue #443) =======================
# `published_view.build` shapes the field and `phase.derive` is the rule; these pin that the RUNNER
# actually feeds it — from loopstate's status, from the cross-review script's own breadcrumb file,
# and from the report on disk. No screen is read and no GitHub call is added: the breadcrumb and the
# report are files the engine already owns, and the PR fact comes from the view this tick already
# holds.

def _seed_flying(rig, iid, status="running", pr=None):
    def m(st):
        rec = st["issues"].setdefault(iid, loopstate.new_issue())
        rec["status"] = status
        if pr is not None:
            rec["pr"] = pr["number"]
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    if pr is not None:
        rig.r.gh_view = {**rig.r.gh_view, "prs": {iid: pr}, "stale": False}
    rig.r._last_poll = NOW + 10_000          # inside the window: no re-poll rebuilds `prs`


def _stamp(rig, iid, text):
    d = rig.home / "state" / "phase"
    d.mkdir(parents=True, exist_ok=True)
    (d / iid).write_text(text)


def test_the_state_home_has_a_place_for_the_phase_breadcrumb(rig):
    # The cross-review script writes into this directory with `mkdir -p`, but the runner laying it
    # out is what makes the location a CONTRACT rather than a coincidence.
    assert (rig.home / "state" / "phase").is_dir()


def test_a_running_lane_publishes_a_phase(rig):
    _seed_flying(rig, "i15")
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "building"


def test_the_runner_reads_the_cross_review_breadcrumb_off_disk(rig):
    # THE issue: the long mid-session step the engine could never see. The script stamps the file;
    # the supervisor reads the file. No screen read anywhere in that sentence.
    _seed_flying(rig, "i15")
    _stamp(rig, "i15", "%d phase=cross-reviewing event=start\n" % (NOW + 10_000))
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "cross-reviewing"


def test_the_end_stamp_takes_the_lane_back_off_cross_reviewing(rig):
    _seed_flying(rig, "i15")
    _stamp(rig, "i15", "%d phase=cross-reviewing event=end rc=0\n" % (NOW + 10_000))
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "building"


def test_a_filed_report_reaches_the_published_phase(rig):
    _seed_flying(rig, "i15")
    (rig.home / "reports" / "i15.md").write_text("## Tests\nok\n")
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "report-posted"


def test_an_open_pr_reaches_the_published_phase(rig):
    _seed_flying(rig, "i15", pr={"number": 25, "state": "OPEN", "mergeable": "MERGEABLE"})
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "pr-open"


def test_a_finished_lane_publishes_no_phase_at_all(rig):
    _seed_flying(rig, "i15", status="merged")
    _stamp(rig, "i15", "%d phase=cross-reviewing event=start\n" % (NOW + 10_000))
    rig.r.tick(now=NOW + 10_000)
    assert "i15" not in view(rig)["phases"], "a landed flight must not read as a live worker"


def test_a_report_the_loop_refuses_to_read_is_not_called_report_posted(rig):
    # `disk_view` hands the DECIDER its reports through `_scan_dir`, which drops a file that will
    # not decode — so an unreadable report is "no report" to every decision. The board must agree,
    # or it would announce a landmark the loop itself refuses to see (fresh-agent review).
    _seed_flying(rig, "i15")
    (rig.home / "reports" / "i15.md").write_bytes(b"\x89PNG\r\n\x1a\n\x00not text")
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "building"


def test_a_corrupt_breadcrumb_costs_a_label_and_never_the_tick(rig):
    # Fail-soft end to end: the file is unreadable junk, the phase degrades to building, the tick
    # completes and the heartbeat (the dead-man's switch) is still stamped.
    _seed_flying(rig, "i15")
    (rig.home / "state" / "phase").mkdir(parents=True, exist_ok=True)
    (rig.home / "state" / "phase" / "i15").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00garbage")
    rig.r.tick(now=NOW + 10_000)
    assert view(rig)["phases"].get("i15") == "building"
    assert (rig.home / "state" / "runner.heartbeat").read_text().strip() == str(int(NOW + 10_000))
