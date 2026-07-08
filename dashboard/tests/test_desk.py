"""Task 9 — the dashboard's own tiny state file (the "since you last looked" watermark).

The command center is a read-only poller over each repo's loop state; the ONE thing it persists
about ITSELF is where William last looked at the tower log, so it can draw the "since you last
looked" divider (design record §4). That watermark lives in the dashboard's OWN file — never in a
repo's loop state — so a friend's install and the tests keep it wherever ``$SL_HOME`` points.

The store is deliberately forgiving (a missing or corrupt file reads as "never looked", never a
crash) and monotonic (marking only ever ADVANCES the watermark, so a stale/racy write can't rewind
it and resurrect already-seen rows as "new").
"""
import os

import desk


def test_fresh_desk_has_never_been_looked_at(tmp_path):
    d = desk.Desk(str(tmp_path / "desk.json"))
    assert d.tower_last_seen() is None


def test_mark_persists_across_instances(tmp_path):
    path = str(tmp_path / "desk.json")
    desk.Desk(path).mark_tower_seen(1000)
    assert desk.Desk(path).tower_last_seen() == 1000    # a NEW instance reads what the last wrote


def test_mark_creates_the_parent_directory(tmp_path):
    # A fresh install has no dashboard state dir yet — marking must create it, not fail.
    path = str(tmp_path / "nested" / "dir" / "desk.json")
    desk.Desk(path).mark_tower_seen(1234)
    assert os.path.isfile(path)
    assert desk.Desk(path).tower_last_seen() == 1234


def test_mark_only_advances_never_rewinds(tmp_path):
    path = str(tmp_path / "desk.json")
    d = desk.Desk(path)
    d.mark_tower_seen(2000)
    d.mark_tower_seen(1500)                              # an older watermark must not stick
    assert d.tower_last_seen() == 2000


def test_corrupt_file_reads_as_never_looked(tmp_path):
    path = tmp_path / "desk.json"
    path.write_text("{ this is not json")
    assert desk.Desk(str(path)).tower_last_seen() is None
    # and a mark still lands, overwriting the corruption
    desk.Desk(str(path)).mark_tower_seen(999)
    assert desk.Desk(str(path)).tower_last_seen() == 999


def test_non_finite_or_bad_watermark_is_ignored(tmp_path):
    path = str(tmp_path / "desk.json")
    d = desk.Desk(path)
    d.mark_tower_seen(float("nan"))                      # a corrupt ts must never become the watermark
    d.mark_tower_seen("not a number")
    assert d.tower_last_seen() is None


def test_concurrent_marks_never_rewind_the_watermark(tmp_path):
    # ThreadingHTTPServer can handle two /api/tower-seen POSTs at once; the read-compare-write must
    # be serialized so an older write can't clobber a newer watermark (Codex — a real race).
    import threading
    path = str(tmp_path / "desk.json")
    d = desk.Desk(path)
    d.mark_tower_seen(1000)
    barrier = threading.Barrier(20)

    def worker(ts):
        barrier.wait()
        d.mark_tower_seen(ts)

    threads = [threading.Thread(target=worker, args=(1000 + i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Whatever interleaving happened, the persisted watermark is the MAX ever written, never lower.
    assert desk.Desk(path).tower_last_seen() == 1019


def test_default_path_lives_under_sl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path))
    p = desk.default_path()
    assert str(p).startswith(str(tmp_path))
    assert p.name == "desk.json"
