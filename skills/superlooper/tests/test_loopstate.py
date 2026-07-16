"""Ported from autocode's test_state.py — the lock / update / S6-guard / atomic-write behaviour
survives unchanged (each bought with a corrupted-state incident); the run/PR schema tests are
replaced by the per-issue schema, and all rotation / run-completion / render tests are dropped
(that machinery does not exist in the deterministic runner)."""
import json
import pytest
import loopstate


def _state_with(issue_ids):
    s = loopstate.new_state()
    for iid in issue_ids:
        s["issues"][iid] = loopstate.new_issue()
    return s


def test_new_state_defaults():
    s = loopstate.new_state()
    assert s == {"version": 1, "issues": {}}


def test_default_issue_schema():
    d = loopstate.DEFAULT_ISSUE
    assert d["status"] == "ready"
    assert d["branch"] is None and d["lane"] is None and d["pr"] is None
    assert d["launches"] == 0 and d["retries"] == 0 and d["conflicts"] == 0
    assert d["requeue_front"] is False
    assert d["declared_touches"] == []
    assert "ready" in loopstate.VALID and "merged" in loopstate.VALID
    # Durable-question fields (#163): a blocked worker exits cleanly and the runner tracks the
    # question count (2-cap), the pending question, and the answered Q&A history embedded in a
    # relaunch brief. qa_log is a MUTABLE list, so new_issue must deep-copy it (like declared_touches).
    assert d["questions_asked"] == 0
    assert d["pending_question"] is None
    assert d["qa_log"] == []
    assert "awaiting_answer" in loopstate.VALID


def test_qa_log_is_deep_copied_not_shared():
    # qa_log is a mutable list on the template — new_issue() must deep-copy it so two issues never
    # alias one shared Q&A history (the declared_touches aliasing class, #163).
    a = loopstate.new_issue()
    b = loopstate.new_issue()
    a["qa_log"].append({"question": "q", "answer": "a"})
    assert b["qa_log"] == []
    assert loopstate.DEFAULT_ISSUE["qa_log"] == []


def test_roundtrip_and_default(tmp_path):
    p = tmp_path / "issues.json"
    s = _state_with(["i1", "i2"])
    loopstate.save(p, s)
    loaded = loopstate.load(p)
    assert loaded["version"] == 1
    assert loaded["issues"]["i1"]["status"] == "ready"
    assert loaded["issues"]["i1"]["launches"] == 0


def test_new_issue_is_deep_copied_not_shared():
    # new_issue() must DEEP-copy DEFAULT_ISSUE, never share by reference (the autocode DEFAULT_PR
    # aliasing class of bug). The scalar case is easy; the load-bearing case is the MUTABLE
    # `declared_touches` list — a shallow dict(DEFAULT_ISSUE) would alias one shared list across
    # every issue and the template (cross-review, Task 1). Two distinct issues must not share it.
    a = loopstate.new_issue()
    b = loopstate.new_issue()
    a["retries"] = 5
    a["declared_touches"].append("frontend")
    assert b["retries"] == 0
    assert b["declared_touches"] == [], "declared_touches must not be shared between issues"
    assert loopstate.DEFAULT_ISSUE["retries"] == 0
    assert loopstate.DEFAULT_ISSUE["declared_touches"] == [], "the template must not be mutated"


def test_atomic_save_no_partial(tmp_path):
    p = tmp_path / "issues.json"
    loopstate.save(p, _state_with(["i1"]))
    # a valid JSON file exists (atomic mv, never a truncated write)
    json.loads(p.read_text())


def test_retry_increment_persists(tmp_path):
    p = tmp_path / "issues.json"
    s = _state_with(["i1"])
    s["issues"]["i1"]["retries"] += 1
    loopstate.save(p, s)
    assert loopstate.load(p)["issues"]["i1"]["retries"] == 1


# --------------------------- locked issues.json read-modify-write ---------------------------

def test_update_applies_mutation_and_releases_lock(tmp_path):
    p = str(tmp_path / "issues.json")
    loopstate.save(p, _state_with(["i1"]))

    def mutate(obj):
        obj["issues"]["i1"]["status"] = "running"

    out = loopstate.update(p, mutate)
    assert out["issues"]["i1"]["status"] == "running"
    assert loopstate.load(p)["issues"]["i1"]["status"] == "running"
    assert not (tmp_path / "issues.json.lock").exists(), "lock must be released after update"


def test_update_releases_lock_even_when_mutate_raises(tmp_path):
    p = str(tmp_path / "issues.json")
    loopstate.save(p, _state_with(["i1"]))

    def boom(obj):
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        loopstate.update(p, boom)
    assert not (tmp_path / "issues.json.lock").exists(), "lock must be released on exception"


def test_update_rejects_tuple_mutate_and_leaves_issues_json_intact(tmp_path):
    # L1/S6: run-20260701-1750 wrote run.json = [null,null] because a mutate lambda returned a
    # tuple and update() persisted it verbatim. update() must REJECT any result that is not a
    # state dict containing 'issues' — and, critically, must NOT overwrite the good file.
    p = str(tmp_path / "issues.json")
    good = _state_with(["i1"])
    loopstate.save(p, good)

    def tuple_lambda(obj):
        obj["issues"]["i1"]["status"] = "running"
        return (None, None)          # the exact S6 bug: a tuple, not a dict

    with pytest.raises(ValueError):
        loopstate.update(p, tuple_lambda)
    # the good file is untouched — never corrupted to [null,null], and never partially saved
    # with the in-memory mutation (status must still be "ready", the whole file == good).
    reloaded = loopstate.load(p)
    assert isinstance(reloaded, dict) and "issues" in reloaded
    assert reloaded["issues"]["i1"]["status"] == "ready", "the pre-mutation good file must survive"
    assert reloaded == good, "the good file must be byte-for-byte intact after a rejected mutate"
    assert not (tmp_path / "issues.json.lock").exists(), "lock must be released even on rejection"


def test_update_rejects_dict_without_issues(tmp_path):
    # a replacement dict that dropped 'issues' is also corruption — reject it, keep the good file.
    p = str(tmp_path / "issues.json")
    loopstate.save(p, _state_with(["i1"]))

    def drop_issues(obj):
        return {"version": 1}        # a dict, but missing 'issues'

    with pytest.raises(ValueError):
        loopstate.update(p, drop_issues)
    assert "issues" in loopstate.load(p)


def test_update_accepts_valid_replacement_dict(tmp_path):
    # a mutate that RETURNS a full valid state dict (not in-place) still works.
    p = str(tmp_path / "issues.json")
    loopstate.save(p, _state_with(["i1"]))

    def replace(obj):
        obj["issues"]["i1"]["status"] = "merged"
        return obj

    out = loopstate.update(p, replace)
    assert out["issues"]["i1"]["status"] == "merged"
    assert loopstate.load(p)["issues"]["i1"]["status"] == "merged"


def test_update_serializes_concurrent_writers_no_lost_update(tmp_path):
    # The clobber a split writer produced: two writers read-modify-write and one update is lost.
    # With loopstate.update() serializing, N concurrent increments must all land.
    import threading
    p = str(tmp_path / "issues.json")
    loopstate.save(p, _state_with(["i1"]))

    def bump():
        def mutate(obj):
            obj["issues"]["i1"]["retries"] += 1
        loopstate.update(p, mutate, timeout=30)

    threads = [threading.Thread(target=bump) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loopstate.load(p)["issues"]["i1"]["retries"] == 20, "no update may be lost under contention"


# --------------------------- the advisory lock primitive ---------------------------

def test_acquire_steals_a_stale_lock(tmp_path):
    # a lockfile from a crashed holder (older than LOCK_STALE_SECONDS) must be steal-able, never a
    # permanent deadlock. Uses the REAL clock (advancing) with utime to age the lockfile — a fixed
    # mock clock would never reach the deadline and spin forever.
    import os
    import time
    lock = tmp_path / "issues.json.lock"
    lock.write_text("someone-elses-token")         # a (dead) holder's token
    now = time.time()
    # FRESH lock (mtime ~ now) -> cannot steal; returns None after the small timeout elapses.
    os.utime(lock, (now, now))
    assert loopstate._acquire(str(lock), timeout=0.15, stale=30) is None
    assert lock.exists(), "a fresh lock must not be stolen"
    # STALE lock (mtime well past the stale window) -> stolen and re-acquired, returns OUR token.
    os.utime(lock, (now - 60, now - 60))
    tok = loopstate._acquire(str(lock), timeout=0.15, stale=30)
    assert tok and lock.read_text() == tok
    os.remove(lock)                                # release what we just acquired


def test_release_only_removes_own_token(tmp_path):
    # codex P2-E: if our lock was stolen (now carries a different token), release must NOT delete it
    # — deleting a competitor's lock would break mutual exclusion.
    lock = tmp_path / "issues.json.lock"
    lock.write_text("mine-123")
    loopstate._release(str(lock), "mine-123")       # we own it -> removed
    assert not lock.exists()
    lock.write_text("stolen-by-someone-else")
    loopstate._release(str(lock), "mine-123")       # not ours -> left intact
    assert lock.exists() and lock.read_text() == "stolen-by-someone-else"
