"""The standing truth strip reaches the snapshot (issue #166) — the assembly end.

``lib/truth`` decides the strip and ``lib/engine`` counts the publish drift; both are unit-tested
beside their own logic. This file pins that ``assemble_snapshot`` actually WIRES them onto the
document the browser binds — that the strip is built from the same ``source`` verdict the board
uses, that the global engine block folds into every repo's strip, and that neither can 500 the poll.

**The DoD's own case is** :func:`test_a_stale_runner_view_shows_the_down_state_not_a_confident_mirror`.
It drives a REAL state home whose runner has gone quiet under a published view that is still sitting
on disk looking perfectly parseable — the exact 2026-07-15 shape, where a dead session rendered as
"launching". A stale view is the most dangerous input this surface has, because nothing about it
looks broken: the document parses, the numbers are all there, and every one of them is a lie about
the present. The snapshot must come back saying so.
"""
import json
import os
import shutil

import pytest

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
    doc = {"published_at": NOW - 5, "polled_at": NOW - 20, "stale": False,
           "issues": {}, "titles": {}, "closed_nums": [], "prs": {}}
    doc.update(over)
    (home / "state" / "gh_view.json").write_text(json.dumps(doc))


class _Gh:
    """A minimal gh stand-in — the fallback path consults it, and it must never be a real binary."""

    def open_issues_probe(self, repo, label=None, limit=200):
        return ([], True)

    def open_issues(self, repo, label=None, limit=200):
        return []

    def issue(self, repo, num):
        return {}

    def pr_for_branch(self, repo, branch):
        return {}

    def pr_comments(self, repo, num):
        return []


class _Engine:
    """A stand-in for lib.engine.EngineDrift — the wiring must depend on the DECISION, never on a
    real checkout being shelled out to."""

    def __init__(self, state):
        self._state = state

    def state(self):
        return dict(self._state)


class _Exploding:
    def state(self):
        raise RuntimeError("git fell over")


def _drifted(behind=3):
    return {"known": True, "behind": behind, "installed_sha": "abc1234",
            "installed_at": "2026-07-11", "source": "/src", "remedy": "bin/install.sh",
            "message": "%d engine fixes merged but not yet live; re-run the installer "
                       "to switch them on" % behind}


def _strip(snap):
    return snap["repos"][0]["truth"]


# =============================== the DoD case ===============================

def test_a_stale_runner_view_shows_the_down_state_not_a_confident_mirror(home):
    # The runner published a view and then went quiet. The document on disk still parses perfectly
    # — that is exactly what makes it dangerous. Nothing may render as a live mirror of a loop that
    # may be dead.
    _publish(home)
    _heartbeat(home, SILENT_AFTER + 500)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    t = _strip(snap)
    assert t["tick"]["state"] == "down"
    assert "loop may be down" in t["tick"]["text"]
    assert t["data"]["state"] == "blind", "a second opinion must never pass for the runner's view"
    assert "not the runner's view" in t["data"]["text"]
    assert t["level"] == "down"


def test_a_runner_that_never_ticked_is_down_not_blank(home):
    # No heartbeat file at all. The absence of evidence rendered as calm is the original bug.
    _publish(home)
    hb = home / "state" / "runner.heartbeat"
    if hb.exists():
        hb.unlink()
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    assert _strip(snap)["tick"]["state"] == "down"
    assert _strip(snap)["level"] == "down"


def test_a_fresh_runner_reads_calm_and_says_the_tick_age(home):
    _publish(home)
    _heartbeat(home, 10)
    t = _strip(server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh()))
    assert t["tick"]["state"] == "ok"
    assert t["tick"]["text"].startswith("last tick ")
    assert t["data"]["state"] == "ok"
    assert t["level"] == "ok", "the healthy case must be quiet, or the alarm becomes wallpaper"


def test_the_strip_agrees_with_the_board_it_sits_above(home):
    # The strip is built from the SAME source verdict the flights are. If these could disagree, the
    # dashboard would be lying in a new place instead of an old one.
    _publish(home)
    _heartbeat(home, SILENT_AFTER + 500)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    repo = snap["repos"][0]
    assert repo["source"]["mode"] == "fallback"
    assert repo["truth"]["data"]["state"] == "blind"


# =============================== the engine block ===============================

def test_engine_drift_reaches_every_repos_strip(home):
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh(),
                                    engine=_Engine(_drifted(3)))
    t = _strip(snap)
    assert t["engine"]["state"] == "drift"
    assert t["engine"]["text"].startswith("3 engine fixes merged but not yet live")
    assert t["level"] == "notice"
    assert snap["engine"]["behind"] == 3, "the raw block rides too, for inspection"


def test_no_engine_wired_leaves_the_line_silent(home):
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    assert snap["engine"] is None
    assert _strip(snap)["engine"] is None


def test_an_engine_that_explodes_never_takes_down_the_poll(home):
    # The field is the truth the owner came for; a drift stamp must never be why he can't see it.
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh(), engine=_Exploding())
    assert snap["repos"], "the field still renders"


def test_an_engine_that_explodes_says_so_rather_than_going_quiet(home):
    # Raised in review, and it is the sharpest version of this PR's own thesis: an exception used to
    # become `engine: None`, which renders as NO engine line — pixel-for-pixel identical to a live,
    # up-to-date engine. The error path was quietly issuing an all-clear. A wired engine that cannot
    # answer must SAY it cannot answer.
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh(), engine=_Exploding())
    assert snap["engine"] is not None, "a broken engine must not be indistinguishable from a live one"
    assert snap["engine"]["known"] is False
    assert snap["engine"]["behind"] is None
    t = _strip(snap)["engine"]
    assert t is not None and t["state"] == "unknown"
    assert "can't tell" in t["text"]
    assert _strip(snap)["level"] == "notice", "an unknown engine is worth a notice, not silence"


def test_a_down_loop_outranks_engine_drift_end_to_end(home):
    _publish(home)
    _heartbeat(home, SILENT_AFTER + 500)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh(),
                                    engine=_Engine(_drifted(2)))
    assert _strip(snap)["level"] == "down", "a dead loop is the headline"
    assert _strip(snap)["engine"]["state"] == "drift", "the drift is still stated underneath"
