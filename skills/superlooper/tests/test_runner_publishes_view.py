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
