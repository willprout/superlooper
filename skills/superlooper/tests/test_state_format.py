"""Issue #45 — the engine stamps a state-format version in the state home.

The command-center (the dashboard) reads a superlooper state home field-by-field, and every reader
fails CLOSED to empty. So a future change to the state SHAPE would silently BLANK the dashboard
rather than error — the most likely future "why is my dashboard empty" with no diagnostic. The
handshake: the engine stamps the format version it wrote (``state/state_format.json``); the
dashboard checks it and, on a version it doesn't recognize, NAMES the mismatch instead of blanking.

This half pins the ENGINE end of the handshake. Crucially the stamp is written by the LIVE runner
only — from ``run()`` AFTER the pidfile singleton is won — so a duplicate or preflight-failing start
(which constructs a Runner but never owns the loop) can't overwrite the running engine's stamp with
its own version and forge a false mismatch (or hide a real one). The dashboard end lives in the
command-center's own suite; the two agree only through this on-disk shape.
"""
import json

import runner as runner_mod

STAMP = ("state", "state_format.json")


def _runner(home, **over):
    # A bare Runner (mirrors test_runner.py's minimal construction); construction lays out the state
    # home but does NOT stamp — stamping is the live runner's act, gated behind run()/the singleton.
    kw = dict(repo="x", config={"repo": "o/r"}, state_home=str(home), pane="p",
              run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    kw.update(over)
    return runner_mod.Runner(**kw)


def _stamp_path(home):
    return home.joinpath(*STAMP)


def test_state_format_version_is_a_positive_int():
    v = runner_mod.STATE_FORMAT_VERSION
    assert isinstance(v, int) and not isinstance(v, bool) and v >= 1


def test_construction_alone_does_not_stamp(tmp_path):
    # The stamp is the LIVE runner's declaration, not a side effect of merely constructing one — so a
    # Runner that is built but never runs (preflight fails, or it loses the singleton) leaves no stamp.
    _runner(tmp_path)
    assert not _stamp_path(tmp_path).exists()


def test_run_stamps_state_format_after_winning_the_singleton(tmp_path):
    # max_ticks=0 runs the pre-loop startup (acquire singleton → stamp → anchor) and stops before any
    # tick — the cheapest way to exercise the exact path that stamps.
    r = _runner(tmp_path)
    assert r.run(max_ticks=0) == 0
    assert json.loads(_stamp_path(tmp_path).read_text()) == {"version": runner_mod.STATE_FORMAT_VERSION}


def test_stamp_is_rewritten_each_run(tmp_path):
    # A prior run (or a downgrade) may have left a stale/garbage stamp; the live runner overwrites it
    # with the truth — its OWN version — and never trusts the leftover.
    (tmp_path / "state").mkdir()
    _stamp_path(tmp_path).write_text("{ garbage not json")
    _runner(tmp_path).run(max_ticks=0)
    assert json.loads(_stamp_path(tmp_path).read_text()) == {"version": runner_mod.STATE_FORMAT_VERSION}


def test_stamp_is_a_complete_valid_version_dict(tmp_path):
    # The dashboard reads this file continuously; the atomic write means any reader sees a complete,
    # valid version dict, never a half write.
    _runner(tmp_path).run(max_ticks=0)
    body = json.loads(_stamp_path(tmp_path).read_text())
    assert isinstance(body, dict) and isinstance(body.get("version"), int)


def test_a_runner_that_loses_the_singleton_never_overwrites_the_stamp(tmp_path, monkeypatch):
    # The regression Codex flagged: a second start (e.g. a NEWER engine opened in another tab that
    # then loses the singleton to the live one) must NOT stamp its own version over the live runner's.
    live = _runner(tmp_path)
    assert live.acquire_singleton()                 # `live` owns the loop
    live._stamp_state_format()                      # ...and has declared its format (say, v1)
    assert json.loads(_stamp_path(tmp_path).read_text()) == {"version": runner_mod.STATE_FORMAT_VERSION}

    monkeypatch.setattr(runner_mod, "STATE_FORMAT_VERSION", 999)   # a different-version challenger
    loser = _runner(tmp_path)
    assert loser.run(max_ticks=0) == 1              # it loses the live singleton and exits...
    # ...and the on-disk stamp is still the LIVE runner's version, never the loser's 999.
    assert json.loads(_stamp_path(tmp_path).read_text())["version"] != 999
    live.release_singleton()
