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
        "models": {"worker": "opus", "answerer": "fable"},
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
    """The runner's state on the tick AFTER a landing: loopstate says merged (`_exec_merge` wrote it
    last tick), and the cached PR is still the pre-merge OPEN read the gate acted on — nothing ever
    writes the merge back into gh_view, and the issue is terminal now, so it is never polled again.

    Deliberately the tick AFTER: a tick loads `ist_map` before `_exec_merge` writes to disk, so the
    landing tick itself still publishes the raw OPEN read. That one-tick lag is by design (see
    published_view.build) and self-corrects here."""
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
