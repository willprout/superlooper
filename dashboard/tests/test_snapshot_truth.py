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


# =============================== boring mode's strip (issue #180) ===============================
# Everything above lands on `repo.truth`, which only the FIELD binds — and boring mode has no field.
# `snapshot.truth` is the same verdict for the view that shows every repo in one table.

def test_the_whole_field_strip_reaches_the_snapshot(home):
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    assert "truth" in snap, "boring mode has no field — it needs the whole-field strip on the snapshot"
    assert [r["name"] for r in snap["truth"]["repos"]] == ["superlooper-sandbox"]
    assert snap["truth"]["level"] == "ok"


def test_a_stale_runner_view_never_renders_as_a_confident_boring_table(home):
    # THE DoD case for this issue, end to end on a real state home. This is the 90s–300s window: the
    # runner has been quiet long enough for the strip to fire but NOT long enough for the RUNNER DOWN
    # banner (heartbeat_down_seconds=300), so boring mode's only other honesty signal is still silent.
    # Without this block the table below renders authoritative while the loop may already be dead.
    _publish(home)
    _heartbeat(home, 120)                       # > 90s silent, < 300s down: the gap this issue is about
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    assert snap["runner"]["down"] is False, "the RUNNER DOWN banner has NOT fired — that is the point"
    t = snap["truth"]
    assert t["level"] == "down"
    row = t["repos"][0]
    assert "loop may be down" in row["tick"]["text"]
    assert "not the runner's view" in row["data"]["text"]


def test_boring_modes_strip_cannot_disagree_with_the_fields(home):
    # The two views bind different blocks; they must be the same verdict. whole_field passes each
    # repo's own banner through by reference, so this is identity — nothing was recomputed.
    _publish(home)
    _heartbeat(home, SILENT_AFTER + 500)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    assert snap["truth"]["repos"][0]["tick"] is _strip(snap)["tick"]
    assert snap["truth"]["repos"][0]["data"] is _strip(snap)["data"]
    assert snap["truth"]["level"] == _strip(snap)["level"]


def test_engine_drift_reaches_boring_mode_stated_once(home):
    # DoD item 3: a merged-but-not-live engine fix used to be invisible in boring mode entirely.
    _publish(home)
    _heartbeat(home, 10)
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh(),
                                    engine=_Engine(_drifted(3)))
    assert snap["truth"]["engine"]["state"] == "drift"
    assert snap["truth"]["engine"]["text"].startswith("3 engine fixes merged but not yet live")
    assert snap["truth"]["level"] == "notice"
    assert "engine" not in snap["truth"]["repos"][0], "one installed engine — one line, not one per repo"


def test_the_worst_repo_sets_the_level_and_the_healthy_one_keeps_its_own_words(tmp_path):
    # The multi-repo aggregation, decided: worst-of on the level, exact per-repo on the words. A
    # single worst-of sentence would say "loop may be down" without saying WHOSE loop.
    homes = {}
    for name in ("alpha", "bravo"):
        dst = tmp_path / name
        shutil.copytree(FIXTURE, dst)
        (dst / "state" / "ALERT").unlink()
        (dst / "state" / "merges_frozen.json").unlink()
        for iid in ("i16", "i23"):
            os.utime(dst / "state" / "activity" / iid, (NOW - 100, NOW - 100))
        _publish(dst)
        homes[name] = dst
    _heartbeat(homes["alpha"], 10)                    # healthy
    _heartbeat(homes["bravo"], SILENT_AFTER + 500)    # silent

    def _entry(name):
        return {"slug": "will-titan/%s" % name, "owner": "will-titan", "name": name,
                "state_home": str(homes[name]), "idle_seconds": 480, "freeze_seconds": 2700,
                "required_checks": ["tests"], "airline": "%s Air" % name.title()}

    cfg = {"poll_seconds": 2, "heartbeat_down_seconds": 300, "runner_silent_seconds": SILENT_AFTER,
           "repos": [_entry("alpha"), _entry("bravo")]}
    snap = server.assemble_snapshot(cfg, now=NOW, gh_mod=_Gh())
    t = snap["truth"]
    assert t["level"] == "down", "one dead loop is enough — the field is not green"
    rows = {r["name"]: r for r in t["repos"]}
    assert rows["alpha"]["level"] == "ok"
    assert rows["alpha"]["tick"]["text"].startswith("last tick ")
    assert "loop may be down" not in rows["alpha"]["tick"]["text"], (
        "the worst repo must not smear its alarm over a healthy one")
    assert rows["bravo"]["level"] == "down"
    assert "loop may be down" in rows["bravo"]["tick"]["text"]
