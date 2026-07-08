"""Task 10 — the dead-man's switch (``lib/watchdog.py``) + its push wiring.

The watchdog is a pure per-repo edge detector: fed the runner states the snapshot already carries
(each ``{slug, down, heartbeat_age}`` — ``down`` computed ONCE by ``flights.repo_state``, so the
grey surface and the push can never disagree), it returns the repos that transitioned UP→DOWN
since the last poll — the ones needing a push NOW. A still-down repo returns nothing (the DoD's
"exactly one push · no repeat nagging"); a recovered repo re-arms so the next down fires again.

These are fake-clock tests: we advance ``now``, recompute ``down`` from the heartbeat age exactly
as the server does, feed each poll to the watchdog, and count fires. No wall clock, no sleeps —
the proof that the switch fires once per episode and never nags is arithmetic.
"""
import threading

import server
import watchdog

THRESHOLD = 300
T0 = 1_783_000_000


def _repos(now, epochs):
    """The ``runner.repos`` list the snapshot carries, computed the way the server does: a repo is
    down when its heartbeat is absent (epoch None) or older than the threshold. ``epochs`` maps
    slug → last-heartbeat epoch (or None for 'never written')."""
    out = []
    for slug, epoch in epochs.items():
        age = None if epoch is None else float(now - epoch)
        down = age is None or age > THRESHOLD
        out.append({"slug": slug, "down": down, "heartbeat_age": age})
    return out


# --------------------------- edge detection over a fake clock ---------------------------

def test_a_fresh_heartbeat_never_fires():
    wd = watchdog.Watchdog()
    beat = T0
    for tick in range(10):
        now = T0 + tick * 30
        beat = now                      # runner is healthy: it beats every tick
        assert wd.newly_down(_repos(now, {"a/b": beat})) == []


def test_going_down_fires_exactly_once_then_stays_silent():
    wd = watchdog.Watchdog()
    beat = T0
    fires = 0
    # 60 ticks at 30s each = 30 min; the runner dies at tick 3 and never beats again.
    for tick in range(60):
        now = T0 + tick * 30
        if tick < 3:
            beat = now
        fired = wd.newly_down(_repos(now, {"a/b": beat}))
        fires += len(fired)
    assert fires == 1, "one push per down episode — no repeat nagging over 27 minutes down"


def test_the_single_fire_names_the_offender_and_carries_its_age():
    wd = watchdog.Watchdog()
    # up, then a heartbeat 400s stale (> 300 threshold).
    assert wd.newly_down(_repos(T0, {"a/b": T0})) == []
    fired = wd.newly_down(_repos(T0 + 400, {"a/b": T0}))
    assert len(fired) == 1
    assert fired[0]["slug"] == "a/b"
    assert fired[0]["heartbeat_age"] == 400.0


def test_re_arms_after_recovery_and_fires_again_on_a_new_episode():
    wd = watchdog.Watchdog()
    epochs = {"a/b": T0}
    # episode 1: goes down → fires.
    assert wd.newly_down(_repos(T0, epochs)) == []
    assert len(wd.newly_down(_repos(T0 + 400, epochs))) == 1
    assert wd.newly_down(_repos(T0 + 430, epochs)) == []          # still down: silent
    # recovery: a fresh beat → re-arm (no fire on recovery itself).
    assert wd.newly_down(_repos(T0 + 500, {"a/b": T0 + 500})) == []
    # episode 2: goes down again → fires again (recovery re-armed the switch).
    assert len(wd.newly_down(_repos(T0 + 900, {"a/b": T0 + 500}))) == 1


def test_per_repo_independent_arming():
    wd = watchdog.Watchdog()
    # both up.
    assert wd.newly_down(_repos(T0, {"a/b": T0, "c/d": T0})) == []
    # a goes down, c stays up → only a fires.
    fired = wd.newly_down(_repos(T0 + 400, {"a/b": T0, "c/d": T0 + 400}))
    assert [r["slug"] for r in fired] == ["a/b"]
    # now c goes down too; a is still down → only c fires (a stays silent).
    fired = wd.newly_down(_repos(T0 + 800, {"a/b": T0, "c/d": T0 + 400}))
    assert [r["slug"] for r in fired] == ["c/d"]


def test_absent_heartbeat_is_down_and_fires_once():
    wd = watchdog.Watchdog()
    # a state home that never wrote a heartbeat reads down from the very first poll (fail closed).
    fired = wd.newly_down(_repos(T0, {"a/b": None}))
    assert [r["slug"] for r in fired] == ["a/b"]
    assert wd.newly_down(_repos(T0 + 30, {"a/b": None})) == []     # no repeat


def test_empty_runner_list_is_safe():
    assert watchdog.Watchdog().newly_down([]) == []


# --------------------------- the push message (offender named, wording matches the sub-line) ---------------------------

def test_runner_down_push_names_the_offender_and_mirrors_the_screen_wording():
    repo = {"slug": "will-titan/sandbox", "down": True, "heartbeat_age": 372.0}
    title, body = server.runner_down_push(repo)
    assert "will-titan/sandbox" in title
    assert "RUNNER DOWN" in title
    # the body reuses the on-screen sub-line so the phone push and the grey banner agree.
    assert body == server._runner_message([repo])
    assert "heartbeat" in body.lower()


def test_runner_down_push_when_no_heartbeat_ever():
    title, body = server.runner_down_push({"slug": "a/b", "down": True, "heartbeat_age": None})
    assert "a/b" in title
    assert "no runner heartbeat" in body.lower()


# --------------------------- dispatch: exactly one send per episode, off the poll thread ---------------------------

def _snap(runner_repos):
    return {"runner": {"down": any(r["down"] for r in runner_repos), "repos": runner_repos}}


def test_dispatch_sends_one_push_per_episode_and_re_arms():
    wd = watchdog.Watchdog()
    sent = []
    cfg = {"notify": {"imessage_to": "x"}}
    # a synchronous spawn makes the (normally off-thread) send observable in-test.
    spawn = lambda fn: fn()
    send = lambda config, title, body: sent.append((config, title, body)) or "ok"

    # up → nothing sent.
    server.dispatch_runner_pushes(_snap(_repos(T0, {"a/b": T0})), wd, cfg, send=send, spawn=spawn)
    assert sent == []
    # down → exactly one send, naming the offender, carrying our config.
    pushed = server.dispatch_runner_pushes(_snap(_repos(T0 + 400, {"a/b": T0})), wd, cfg,
                                           send=send, spawn=spawn)
    assert pushed == ["a/b"]
    assert len(sent) == 1
    assert sent[0][0] is cfg and "a/b" in sent[0][1]
    # still down → no second send.
    server.dispatch_runner_pushes(_snap(_repos(T0 + 430, {"a/b": T0})), wd, cfg, send=send, spawn=spawn)
    assert len(sent) == 1


def test_dispatch_surfaces_the_send_outcome_so_a_misconfigured_channel_is_not_silent():
    # notify.send returns an outcome string the caller journals; a dropped outcome makes a broken
    # iMessage/cmd (or log-only) invisible. dispatch must hand it to the log sink, naming the repo.
    wd = watchdog.Watchdog()
    logged = []
    send = lambda config, title, body: "imessage send failed (rc=127)"
    log = lambda slug, title, outcome: logged.append((slug, title, outcome))
    server.dispatch_runner_pushes(_snap(_repos(T0 + 400, {"a/b": T0})), wd, {},
                                  send=send, spawn=lambda fn: fn(), log=log)
    assert len(logged) == 1
    slug, title, outcome = logged[0]
    assert slug == "a/b" and "a/b" in title and outcome == "imessage send failed (rc=127)"


def test_dispatch_survives_a_snapshot_with_no_runner_block():
    wd = watchdog.Watchdog()
    assert server.dispatch_runner_pushes({}, wd, {}, send=lambda *a: "ok", spawn=lambda fn: fn()) == []


def test_concurrent_down_edges_fire_exactly_once_the_locks_job():
    # ThreadingHTTPServer evaluates the switch from many request threads at once. The lock's whole
    # purpose is that a repo going down still costs ONE fire even under a concurrent stampede.
    wd = watchdog.Watchdog()
    down = _repos(T0 + 400, {"a/b": T0})
    counts = []
    guard = threading.Lock()

    def worker():
        n = len(wd.newly_down(down))
        with guard:
            counts.append(n)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(counts) == 1, "50 concurrent down-edges must fire exactly once"


# --------------------------- end-to-end: a real assembled snapshot drives the switch ---------------------------

def test_a_real_downed_snapshot_fires_exactly_one_push(tmp_path):
    # Proves assemble_snapshot's runner.repos shape actually feeds the watchdog (key names agree),
    # and that a genuinely-down state home (no heartbeat) fires once and then goes quiet.
    home = tmp_path / "will-titan__x"
    (home / "state").mkdir(parents=True)
    (home / "state" / "issues.json").write_text('{"version": 1, "issues": {}}')
    (home / "journal.jsonl").write_text("")
    # deliberately NO runner.heartbeat → the dead-man's switch trips (fail closed).
    cfg = {"poll_seconds": 2, "heartbeat_down_seconds": 300, "notify": {"imessage_to": "x"},
           "repos": [{"slug": "will-titan/x", "name": "x", "state_home": str(home),
                      "idle_seconds": 480, "freeze_seconds": 2700, "required_checks": ["tests"]}]}
    wd = watchdog.Watchdog()
    sent = []
    send = lambda config, title, body: sent.append((title, body))
    spawn = lambda fn: fn()

    snap = server.assemble_snapshot(cfg, now=T0)
    assert snap["runner"]["down"] is True
    pushed = server.dispatch_runner_pushes(snap, wd, cfg, send=send, spawn=spawn)
    assert pushed == ["will-titan/x"]
    assert len(sent) == 1 and "will-titan/x" in sent[0][0]

    # a second poll of the same still-down snapshot must not push again.
    server.dispatch_runner_pushes(server.assemble_snapshot(cfg, now=T0 + 2), wd, cfg,
                                  send=send, spawn=spawn)
    assert len(sent) == 1
