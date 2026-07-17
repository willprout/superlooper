"""Regression tests for the file-signal sensing core, ported from autocode's test_watcher.py
alongside the events.py port (plan Task 8). Each section keeps its original incident tag:
these cases were each bought with a real false-wake/false-restart in a live run — the
marker-EXISTENCE resolution test in particular is the P0 where frozen-recovery restarted
finished sessions and deleted their reports. Rotation/ring/stall/ship cases are NOT ported:
that machinery died with the standing orchestrator (enforcement by absence).
"""
import events


def snap(**kw):
    base = dict(id="i1", status="running", launched=True, activity_mtime=1000,
                report_hash=None, report_mtime=None, blocked_hash=None, blocked_mtime=None,
                exited_token=None, exited_mtime=None, awaiting=False, now=1000)
    base.update(kw)
    return base


# --------------------------- the headline fix (RC1) ---------------------------

def test_bg_park_produces_no_event():
    # THE fix: a session that yielded to await its own background work (recent activity, NO
    # report/blocked/exited marker, not yet idle) generates ZERO events. This is the direct
    # repair of the 3/6 false rests in run-20260625-1516.
    ev, _ = events.detect_events([snap(activity_mtime=1000, now=1000 + 60)], set())
    assert ev == []


def test_finished_fires_once_per_report_content():
    s = snap(report_hash="abc", report_mtime=1000)
    ev, em = events.detect_events([s], set())
    assert ev == [{"type": "session_finished", "id": "i1", "report_token": "abc"}]
    # same content -> no re-fire (A6: identical rewrite, even with a new mtime, must not re-trigger)
    ev2, em = events.detect_events([snap(report_hash="abc", report_mtime=2000)], em)
    assert ev2 == []
    # new content -> re-fires
    ev3, _ = events.detect_events([snap(report_hash="def", report_mtime=3000)], em)
    assert ev3 and ev3[0]["type"] == "session_finished" and ev3[0]["report_token"] == "def"


def test_blocked_fires_with_token():
    ev, em = events.detect_events([snap(blocked_hash="q1", blocked_mtime=1000)], set())
    assert ev[0]["type"] == "session_blocked" and ev[0]["blocked_token"] == "q1"
    ev2, _ = events.detect_events([snap(blocked_hash="q1", blocked_mtime=1000)], em)
    assert ev2 == []


def test_exited_fires_once():
    ev, em = events.detect_events([snap(exited_token=111, exited_mtime=0.000000111)], set())
    assert ev[0]["type"] == "session_exited" and ev[0]["exited_token"] == 111
    ev2, _ = events.detect_events([snap(exited_token=111, exited_mtime=0.000000111)], em)
    assert ev2 == []


# --------------------------- tiered staleness (RC1/RC-FREEZE/Finding 1) ---------------------------

def test_idle_after_threshold_not_before():
    assert events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.IDLE_SECONDS - 1)], set())[0] == []
    ev, _ = events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.IDLE_SECONDS + 1)], set())
    assert ev[0]["type"] == "session_idle"


def test_idle_suppressed_by_awaiting_marker():
    # A session that declared it is awaiting long background work gets NO idle peek...
    ev, _ = events.detect_events(
        [snap(awaiting=True, activity_mtime=1000, now=1000 + events.IDLE_SECONDS + 60)], set())
    assert ev == []
    # ...but the hard freeze backstop still applies (then the response is a SAFE peek anyway).
    ev2, _ = events.detect_events(
        [snap(awaiting=True, activity_mtime=1000, now=1000 + events.FREEZE_SECONDS + 1)], set())
    assert ev2[0]["type"] == "frozen"


def test_frozen_supersedes_idle():
    ev, em = events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.FREEZE_SECONDS + 1)], set())
    assert [e["type"] for e in ev] == ["frozen"]
    assert ("i1", "idle") not in em


def test_idle_then_frozen_progression():
    # crosses idle first (one idle event), then later frozen (one frozen event)
    ev1, em = events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.IDLE_SECONDS + 1)], set())
    assert ev1[0]["type"] == "session_idle"
    ev2, em = events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.FREEZE_SECONDS + 1)], em)
    assert ev2[0]["type"] == "frozen"


def test_idle_clears_and_refires_after_recovery():
    ev1, em = events.detect_events(
        [snap(activity_mtime=1000, now=1000 + events.IDLE_SECONDS + 1)], set())
    assert ev1 and ev1[0]["type"] == "session_idle"
    # recovered: fresh activity -> no event, idle edge cleared
    ev2, em = events.detect_events([snap(activity_mtime=5000, now=5000 + 10)], em)
    assert ev2 == [] and ("i1", "idle") not in em
    # idles again -> re-fires
    ev3, _ = events.detect_events(
        [snap(activity_mtime=6000, now=6000 + events.IDLE_SECONDS + 1)], em)
    assert ev3 and ev3[0]["type"] == "session_idle"


# --------------------------- post-wake grace (issue #42) ---------------------------

def test_wake_grace_suppresses_idle_and_frozen_within_the_window():
    # Closing the laptop overnight makes a healthy session's activity_mtime look hours old on the
    # first post-sleep tick (wall clock jumped, the file did not). WITHIN the wake grace window,
    # neither idle nor frozen fires — the resume artifact must not poke a healthy worker.
    now = 1000 + events.FREEZE_SECONDS + 50_000        # activity looks ancient purely from the gap
    ev, _ = events.detect_events([snap(activity_mtime=1000, now=now)], set(),
                                 wake_grace_until=now + 300)
    assert ev == []


def test_liveness_rearms_after_the_wake_grace_for_a_still_stale_session():
    # A session that is genuinely dead across the sleep (never re-stamps) still alarms once the grace
    # expires — and the in-grace suppression must NOT latch the dedup key, or the frozen event could
    # never re-fire.
    now = 1000 + events.FREEZE_SECONDS + 50_000
    ev1, em = events.detect_events([snap(activity_mtime=1000, now=now)], set(),
                                   wake_grace_until=now + 300)
    assert ev1 == [] and ("i1", "frozen") not in em    # suppressed AND un-latched during grace
    ev2, _ = events.detect_events([snap(activity_mtime=1000, now=now + 301)], em,
                                  wake_grace_until=now + 300)
    assert ev2 and ev2[0]["type"] == "frozen"          # past the grace, still stale -> re-arms


def test_wake_grace_does_not_suppress_a_session_that_restamped_after_wake():
    # A healthy worker resumes and re-stamps activity within the grace; even past the grace it is
    # fresh, so it never idles/freezes.
    now = 1000 + events.FREEZE_SECONDS + 50_000
    ev, _ = events.detect_events([snap(activity_mtime=now - 5, now=now + 301)], set(),
                                 wake_grace_until=now + 300)
    assert ev == []


# --------------------------- resolved is NOT sticky (Finding 4) ---------------------------

def test_finished_session_never_false_idles_or_freezes():
    # REGRESSION for the P0 the impl review caught: a cleanly-finished session writes its report,
    # then the Stop/PostToolUse hooks stamp activity AFTER it (activity_mtime > report_mtime). The
    # old `activity <= report` resolved test made this look UNresolved and fired a false idle +
    # false frozen on a DONE session (which frozen-recovery would then restart, deleting the
    # report). v2 resolves on marker EXISTENCE, so no idle/frozen ever — even far in the future.
    ev, _ = events.detect_events(
        [snap(report_hash="r", report_mtime=2000, activity_mtime=2003,   # activity AFTER report
              now=2003 + events.FREEZE_SECONDS + 9999)], set())
    assert ev == [] or all(e["type"] == "session_finished" for e in ev)
    assert "frozen" not in [e["type"] for e in ev]
    assert "session_idle" not in [e["type"] for e in ev]


def test_finished_dedup_unlatches_when_report_removed():
    # F1: identical content re-dedups within a session, but once the report file is REMOVED
    # (launch-session.sh clears it on restart) the finished key un-latches so a re-created report
    # re-fires — even byte-identical.
    ev, em = events.detect_events([snap(report_hash="r", report_mtime=2000)], set())
    assert ev and ev[0]["type"] == "session_finished"
    # report gone (restart cleared it): key un-latched
    ev2, em = events.detect_events([snap(report_hash=None, report_mtime=None)], em)
    assert ev2 == [] and ("i1", "finished", "r") not in em
    # the restarted session reproduces the SAME report content -> re-fires
    ev3, _ = events.detect_events([snap(report_hash="r", report_mtime=3000)], em)
    assert ev3 and ev3[0]["type"] == "session_finished"


def test_blocked_dedup_unlatches_when_marker_removed():
    # F1: the runner answers a blocked session by removing the marker; if the session re-blocks
    # with the SAME question text, a new session_blocked must fire.
    ev, em = events.detect_events([snap(blocked_hash="q", blocked_mtime=1000)], set())
    assert ev and ev[0]["type"] == "session_blocked"
    ev2, em = events.detect_events([snap(blocked_hash=None, blocked_mtime=None)], em)
    assert ("i1", "blocked", "q") not in em
    ev3, _ = events.detect_events([snap(blocked_hash="q", blocked_mtime=1100)], em)
    assert ev3 and ev3[0]["type"] == "session_blocked"


def test_settled_status_never_idles_or_freezes():
    # P2-3/F6 (adapted to the superlooper lifecycle): an issue the runner has already taken
    # ownership of — gating, holding a merge, merged, parked, bounced, or waiting on William —
    # must NOT fire idle/frozen even with an alive idle session and no marker (no false restart
    # of done work).
    for st in ("gating", "holding", "merged", "parked", "needs_william", "bounced"):
        ev, _ = events.detect_events(
            [snap(status=st, report_hash=None, report_mtime=None, activity_mtime=1000,
                  now=1000 + events.FREEZE_SECONDS + 999)], set())
        assert ev == [], f"{st} should not fire idle/frozen, got {[e['type'] for e in ev]}"


# --------------------------- corrupt / wrong-typed status (issue #95) ---------------------------

def test_detect_events_skips_an_unhashable_status_without_raising():
    # A corrupt state/issues.json can carry a wrong-typed UNHASHABLE status ([] or {}). The
    # settled-suppression test `status in SETTLED_STATUSES` raises `unhashable type` on it and wedges
    # the whole tick BEFORE the heartbeat is stamped (the dashboard's dead-man's switch then reads a
    # live runner as dead). detect_events must SKIP the corrupt entry, fail closed, and still detect
    # every other issue's events. Seeds the two DoD shapes ([] and {}) beside a healthy finished issue.
    for bad in ([], {}):
        snaps = [snap(id="i1", status=bad, activity_mtime=1000, now=1000 + 60),
                 snap(id="i2", status="running", report_hash="rep", report_mtime=1000)]
        ev, _ = events.detect_events(snaps, set())          # must NOT raise
        types = {(e["id"], e["type"]) for e in ev}
        assert ("i2", "session_finished") in types, f"healthy event lost for status={bad!r}"
        assert ("i1", "corrupt_status") in types, f"corrupt status {bad!r} swallowed silently"


def test_corrupt_status_is_fail_closed_not_settled():
    # Fail closed: a wrong-typed status is NOT a settled status, so the idle/frozen liveness tiers
    # still evaluate (their response is a safe peek, never a blind action). A launched, stale,
    # unhashable-status session therefore still fires session_idle.
    ev, _ = events.detect_events(
        [snap(status=[], activity_mtime=1000, now=1000 + events.IDLE_SECONDS + 1)], set())
    assert "session_idle" in [e["type"] for e in ev]


def test_corrupt_status_record_is_bounded_and_unlatches_on_repair():
    # The skip is visible but BOUNDED: one corrupt_status record per corrupt id (deduped via the
    # emitted set, like idle/frozen), never one per tick. It un-latches when the status becomes
    # well-typed again, so a later re-corruption re-fires exactly once.
    ev, em = events.detect_events([snap(status={})], set())
    assert [e["type"] for e in ev] == ["corrupt_status"]
    assert ("i1", "corrupt_status") in em
    ev2, em = events.detect_events([snap(status={})], em)          # still corrupt -> no re-fire
    assert ev2 == []
    ev3, em = events.detect_events([snap(status="running")], em)   # repaired -> un-latch
    assert ("i1", "corrupt_status") not in em
    ev4, _ = events.detect_events([snap(status=[])], em)           # re-corrupted -> re-fires once
    assert [e["type"] for e in ev4] == ["corrupt_status"]


def test_none_status_is_not_flagged_as_corrupt():
    # A genuinely status-less issue (None) is normal cold state, NOT corruption: it must never emit a
    # corrupt_status record (the guard keys on wrong-TYPED, not on absent).
    ev, _ = events.detect_events([snap(status=None, activity_mtime=1000, now=1000 + 60)], set())
    assert "corrupt_status" not in [e["type"] for e in ev]


def test_exited_is_resolved_and_unlatches_on_clear():
    ev, em = events.detect_events(
        [snap(exited_token=9, exited_mtime=1.0, activity_mtime=1000,
              now=1000 + events.FREEZE_SECONDS + 999)], set())
    assert ev and ev[0]["type"] == "session_exited"
    assert "frozen" not in [e["type"] for e in ev]    # exited -> resolved
    # marker cleared on relaunch -> un-latch so a future exit re-fires
    ev2, em = events.detect_events([snap(exited_token=None, exited_mtime=None)], em)
    assert ("i1", "exited", 9) not in em


# --------------------------- restart idempotency ---------------------------

def test_emitted_from_events_rebuilds_token_keys_only():
    on_disk = [
        {"type": "session_finished", "id": "i1", "report_token": "abc"},
        {"type": "session_blocked", "id": "i2", "blocked_token": "q"},
        {"type": "session_exited", "id": "i3", "exited_token": 7},
        {"type": "frozen", "id": "i5"},        # edge event: deliberately NOT rebuilt
    ]
    em = events.emitted_from_events(on_disk)
    assert ("i1", "finished", "abc") in em
    assert ("i2", "blocked", "q") in em
    assert ("i3", "exited", 7) in em
    assert ("i5", "frozen") not in em   # a still-frozen i5 SHOULD re-alert after restart
    # a restarted runner seeing i1 with the same report content must NOT re-emit
    ev, _ = events.detect_events([snap(id="i1", report_hash="abc", report_mtime=1)], em)
    assert ev == []


def test_reconcile_emitted_survives_restart_unlatch():
    # D1: emitted_from_events rebuilds finished/blocked keys from never-pruned processed/ events.
    # reconcile_emitted must drop a key whose marker is now ABSENT (runner answered / restart
    # cleared it) so a re-created identical marker re-fires after a runner restart, while keeping a
    # key whose marker is still present unchanged (don't re-gate on restart).
    rebuilt = {("i1", "blocked", "qHASH"),    # marker was rm'd by the runner
               ("i2", "finished", "rHASH"),   # report still present, unchanged
               ("i3", "finished", "oldHASH"),  # report changed while the runner was down
               ("i4", "exited", 7)}            # non finished/blocked -> always kept
    current = {("i1", "blocked"): None,        # gone
               ("i2", "finished"): "rHASH",    # same
               ("i3", "finished"): "newHASH"}  # changed
    out = events.reconcile_emitted(rebuilt, current)
    assert ("i1", "blocked", "qHASH") not in out      # dropped -> re-block will re-fire
    assert ("i2", "finished", "rHASH") in out         # kept -> no re-gate on restart
    assert ("i3", "finished", "oldHASH") not in out   # dropped -> changed report re-fires
    assert ("i4", "exited", 7) in out
    # and the end-to-end effect: after dropping the blocked key, an identical re-block re-fires
    ev, _ = events.detect_events([snap(blocked_hash="qHASH", blocked_mtime=2000)], out)
    assert ev and ev[0]["type"] == "session_blocked"


def test_next_seq_continues_past_existing_and_processed():
    assert events.next_seq([]) == 1
    assert events.next_seq(["0001.json", "0002.json"]) == 3
    assert events.next_seq(["0007.json"]) == 8


# --------------------------- processed-events bound (issue #41) ---------------------------
#
# The processed/ dir accumulates one file per event forever, and both next_seq() and the restart
# rebuild scan it — so their cost grows with total history. processed_overflow() picks the OLDEST
# names to move to an archive so the hot dir (and thus that scan cost) stays bounded. The newest
# file — carrying the global-max seq — is always kept, so next_seq stays monotonic and never
# collides with an archived seq.


def test_processed_overflow_empty_at_or_below_cap():
    names = [f"{i}.json" for i in range(1, events.PROCESSED_CAP + 1)]
    assert events.processed_overflow(names) == []          # exactly at the cap: nothing yet


def test_processed_overflow_archives_oldest_down_to_keep():
    n = events.PROCESSED_CAP + 300
    names = [f"{i}.json" for i in range(1, n + 1)]
    over = events.processed_overflow(names)
    # archives the oldest, leaving exactly PROCESSED_KEEP newest hot
    assert len(over) == n - events.PROCESSED_KEEP
    archived_seqs = sorted(int(x.split(".")[0]) for x in over)
    assert archived_seqs == list(range(1, n - events.PROCESSED_KEEP + 1))    # the lowest seqs
    kept = set(names) - set(over)
    assert f"{n}.json" in kept                              # the global-max seq is never archived


def test_next_seq_stays_correct_and_history_independent_after_pruning():
    # The bound's PAYOFF: after archiving the oldest, next_seq() over ONLY the retained newest names
    # still returns global-max+1 — monotonic, no collision with an archived seq, and its input size
    # is PROCESSED_KEEP regardless of how much total history was archived.
    n = events.PROCESSED_CAP + 5000
    names = [f"{i}.json" for i in range(1, n + 1)]
    retained = set(names) - set(events.processed_overflow(names))
    assert len(retained) == events.PROCESSED_KEEP          # scan cost bounded, not O(history)
    assert events.next_seq(retained) == n + 1              # still the true next seq


def test_processed_overflow_tolerates_wrong_typed_and_non_numeric():
    # Fail closed on garbage: a non-list input archives nothing; non-str / non-numeric names never
    # raise and are treated as oldest (archived first) so they can't outlive a real seq.
    assert events.processed_overflow(None) == []
    assert events.processed_overflow("junk") == []
    weird = ["notanumber.json", "x.json"] + [f"{i}.json" for i in range(1, events.PROCESSED_CAP)]
    over = events.processed_overflow(weird)
    assert set(over) <= set(weird) and "notanumber.json" in over and "x.json" in over


def test_retry_runaway():
    # fact-4, adapted to issues.json: ids whose mechanically-stamped retries blew far past the
    # retry cap (2) — cap enforcement failing must be loud. threshold = cap + slack.
    assert events.retry_runaway({"issues": {"i1": {"retries": 3}}}) == []
    assert events.retry_runaway({"issues": {"i1": {"retries": 4}, "i2": {"retries": 0}}}) == ["i1"]
    assert events.retry_runaway({}) == []
    assert events.retry_runaway(None) == []


def test_retry_runaway_tolerates_wrong_typed_retries():
    # Codex cross-review (Task 8): a corrupt counter ("4", None, [], True) must be skipped —
    # fail closed for that issue — never raise TypeError into the tick. Bool is excluded too
    # (True is an int subclass; a boolean 'retries' is corruption, not a count of 1).
    for bad in ("4", None, [], {}, True):
        assert events.retry_runaway({"issues": {"i1": {"retries": bad}}}) == []
    # a corrupt sibling never hides a genuine runaway
    assert events.retry_runaway(
        {"issues": {"i1": {"retries": "junk"}, "i2": {"retries": 5}}}) == ["i2"]


def test_live_statuses_still_freeze():
    # Inverse of the settled-status suppression (Codex cross-review, Task 8): the recoverable
    # lifecycle statuses must KEEP firing frozen on stale activity with no marker — an accidental
    # future addition to SETTLED_STATUSES would silently disable stuck-session recovery.
    for st in ("ready", "running", "frozen", None):
        ev, _ = events.detect_events(
            [snap(status=st, activity_mtime=1000, now=1000 + events.FREEZE_SECONDS + 1)], set())
        assert [e["type"] for e in ev] == ["frozen"], f"{st} must stay recoverable"


def test_event_key_roundtrips_every_event_type():
    # _event_key is load-bearing for the durable-write failure path (the runner un-commits a
    # dedup key when the event file write fails, so the next tick re-emits instead of losing
    # the event). Pin that every emitted event type maps back to its detect_events dedup key.
    assert events._event_key({"type": "session_finished", "id": "i1", "report_token": "r"}) == \
        ("i1", "finished", "r")
    assert events._event_key({"type": "session_blocked", "id": "i1", "blocked_token": "q"}) == \
        ("i1", "blocked", "q")
    assert events._event_key({"type": "session_exited", "id": "i1", "exited_token": 7}) == \
        ("i1", "exited", 7)
    assert events._event_key({"type": "session_idle", "id": "i1"}) == ("i1", "idle")
    assert events._event_key({"type": "frozen", "id": "i1"}) == ("i1", "frozen")
    assert events._event_key({"type": "corrupt_status", "id": "i1"}) == ("i1", "corrupt_status")
    assert events._event_key({"type": "unknown", "id": "i1"}) is None


# --------------------------- snapshot: the §C.3 marker reader ---------------------------

def _mk_state_home(tmp_path):
    for sub in ("reports", "state/activity", "state/blocked", "state/exited", "state/awaiting"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_snapshot_reads_c3_markers(tmp_path):
    # Pins the §C.3 disk layout: reports/<id>.md + state/{activity,blocked,exited,awaiting}/<id>.
    home = _mk_state_home(tmp_path)
    (home / "reports" / "i1.md").write_text("## Tests\nall green\n")
    (home / "state" / "activity" / "i1").write_text("")
    (home / "state" / "blocked" / "i2").write_text("which auth flow?")
    (home / "state" / "activity" / "i2").write_text("")
    (home / "state" / "exited" / "i3").write_text("0")
    (home / "state" / "awaiting" / "i4").write_text("")
    (home / "state" / "activity" / "i4").write_text("")

    issues_state = {"issues": {"i1": {"status": "running"}, "i2": {"status": "blocked"},
                               "i3": {"status": "exited"}, "i4": {"status": "running"}}}
    snaps = {s["id"]: s for s in
             events.snapshot(home, ["i1", "i2", "i3", "i4", "i9"], issues_state, now=5000)}

    assert snaps["i1"]["report_hash"] and snaps["i1"]["report_mtime"] is not None
    assert snaps["i1"]["launched"] is True and snaps["i1"]["status"] == "running"
    assert snaps["i2"]["blocked_hash"] and snaps["i2"]["report_hash"] is None
    assert snaps["i3"]["exited_token"] is not None and snaps["i3"]["exited_mtime"] is not None
    assert snaps["i4"]["awaiting"] is True
    # i9: nothing on disk, not in issues.json -> unlaunched, all-None snapshot (never raises)
    assert snaps["i9"]["launched"] is False and snaps["i9"]["status"] is None
    assert snaps["i9"]["activity_mtime"] is None and snaps["i9"]["report_hash"] is None
    assert all(s["now"] == 5000 for s in snaps.values())


def test_snapshot_feeds_detect_events_end_to_end(tmp_path):
    # the two halves compose: a report on disk -> session_finished with a content-hash token,
    # and rewriting the SAME bytes does not re-fire (the A6 dedup, through the real file path).
    home = _mk_state_home(tmp_path)
    (home / "reports" / "i1.md").write_text("done\n")
    st = {"issues": {"i1": {"status": "running"}}}
    ev, em = events.detect_events(events.snapshot(home, ["i1"], st, now=100), set())
    assert ev and ev[0]["type"] == "session_finished"
    (home / "reports" / "i1.md").write_text("done\n")   # identical rewrite
    ev2, _ = events.detect_events(events.snapshot(home, ["i1"], st, now=200), em)
    assert ev2 == []


def test_snapshot_tolerates_wrong_typed_issues_state(tmp_path):
    # fail-closed on wrong-TYPED (not just missing) input — the defect class Session 1's reviews
    # caught twice: a corrupt issues.json (list, scalar, null) must yield status=None snapshots,
    # never raise into the tick.
    home = _mk_state_home(tmp_path)
    (home / "state" / "activity" / "i1").write_text("")
    for bad in (None, [], "junk", {"issues": []}, {"issues": {"i1": "junk"}}):
        snaps = events.snapshot(home, ["i1"], bad, now=1)
        assert snaps[0]["status"] is None and snaps[0]["launched"] is True


# --------------------------- the progress clock (issue #157) ---------------------------
# The probe ladder keys on state/status/<id>.json (worker_hook.stamp_status), NOT on activity
# staleness — the i328 trap was that each nudge refreshed the very activity stamp the ladder
# watched, so it could never escalate. progress_signature distills a status snapshot down to the
# fields that move ONLY on real progress; parse_ack reads the worker's machine-readable reply.

def test_progress_signature_keys_on_head_and_markers_not_dirty():
    # HEAD, report, blocked are the progress-bearing fields. `dirty` is DELIBERATELY excluded: the
    # DoD keys on "commit/marker/HEAD change", and a flapping git-lock read (dirty None<->bool)
    # would otherwise register as false progress and defeat escalation.
    base = {"id": "i1", "ts": 100, "cwd": "/w", "head": "abc123",
            "dirty": False, "report": False, "blocked": False}
    sig = events.progress_signature(base)
    assert sig is not None
    assert events.progress_signature({**base, "dirty": True}) == sig      # dirty flip: NOT progress
    assert events.progress_signature({**base, "head": "def456"}) != sig   # new commit: progress
    assert events.progress_signature({**base, "report": True}) != sig     # report marker: progress
    assert events.progress_signature({**base, "blocked": True}) != sig    # blocked marker: progress


def test_progress_signature_is_none_without_a_usable_clock():
    # A missing/empty/wrong-typed clock means the Stop hook never stamped this rest — there is no
    # progress signal, so the ladder must fall back to the activity tiers rather than invent one.
    for bad in (None, {}, [], "junk", 5):
        assert events.progress_signature(bad) is None


def test_progress_signature_stable_across_equal_snapshots():
    a = {"head": "h", "dirty": None, "report": True, "blocked": False, "ts": 1}
    b = {"head": "h", "dirty": None, "report": True, "blocked": False, "ts": 999}   # only ts moved
    assert events.progress_signature(a) == events.progress_signature(b)             # ts is not progress


def test_progress_advanced_only_on_a_proven_advance():
    # A real HEAD movement between two readable commits, or a report/blocked marker change.
    assert events.progress_advanced("A|False|False", "B|False|False") is True    # HEAD moved
    assert events.progress_advanced("A|False|False", "A|True|False") is True     # report appeared
    assert events.progress_advanced("A|False|False", "A|False|True") is True     # blocked appeared
    assert events.progress_advanced("A|False|False", "A|False|False") is False   # nothing changed


def test_progress_advanced_treats_an_unreadable_head_as_non_progress():
    # The i328 fail-closed guard: a head that became git-UNREADABLE ('None') is a flap, not movement —
    # exactly why progress_signature excludes `dirty`. Neither direction counts as an advance.
    assert events.progress_advanced("A|False|False", "None|False|False") is False   # readable -> None
    assert events.progress_advanced("None|False|False", "A|False|False") is False   # None -> readable
    assert events.progress_advanced("|False|False", "A|False|False") is False       # empty head
    # ...but a report marker change is a real milestone regardless of head readability
    assert events.progress_advanced("None|False|False", "None|True|False") is True


def test_progress_advanced_fails_closed_on_corrupt_input():
    # A non-str / malformed baseline is not a usable measurement -> never a (false) advance.
    for bad in (None, 42, ["A"], {}, True, "A|False", "a|b|c|d"):
        assert events.progress_advanced(bad, "A|False|False") is False
        assert events.progress_advanced("A|False|False", bad) is False


def test_usable_baseline_requires_a_wellformed_readable_head_signature():
    assert events.usable_baseline("A|False|False") is True
    assert events.usable_baseline("None|False|False") is False     # unreadable head -> poison
    assert events.usable_baseline("|False|False") is False         # empty head
    for bad in (None, 42, ["A"], {}, True, "A|False", "a|b|c|d"):
        assert events.usable_baseline(bad) is False


def test_progress_evidence_names_the_changed_signature_field():
    # The #231 un-latch journal names WHICH progress-bearing field advanced. Signature is
    # 'head|report|blocked'; compare the parts and label the differences.
    assert events.progress_evidence("A|False|False", "B|False|False") == "HEAD"
    assert events.progress_evidence("A|False|False", "A|True|False") == "report marker"
    assert events.progress_evidence("A|False|False", "A|False|True") == "blocked marker"
    assert events.progress_evidence("A|False|False", "B|True|False") == "HEAD, report marker"


def test_progress_evidence_is_fail_closed_on_unparseable_input():
    # Never raise into the tick: a non-str, or an identical signature, yields the generic phrase.
    for bad in (None, 123, ["A"], {}):
        assert events.progress_evidence(bad, "A|False|False") == "progress clock advanced"
        assert events.progress_evidence("A|False|False", bad) == "progress clock advanced"
    assert events.progress_evidence("A|False|False", "A|False|False") == "progress clock advanced"


def test_parse_ack_reads_a_valid_reply_only_when_the_nonce_matches():
    assert events.parse_ack("WORKING nonce-42", "nonce-42") == "WORKING"
    assert events.parse_ack("DONE nonce-42", "nonce-42") == "DONE"
    assert events.parse_ack("WAITING nonce-42", "nonce-42") == "WAITING"
    assert events.parse_ack("STUCK nonce-42", "nonce-42") == "STUCK"
    # a stale nonce (answering an OLD probe) must NOT be read as answering the current one
    assert events.parse_ack("WORKING nonce-41", "nonce-42") is None
    assert events.parse_ack("WORKING", "nonce-42") is None                # no nonce at all


def test_parse_ack_tolerates_prose_but_rejects_ambiguity():
    assert events.parse_ack("state: working  nonce=n7  (still on the tests)", "n7") == "WORKING"
    # the probe message lists all four states; a worker that echoes the menu is ambiguous, not an
    # ack — return None (keep probing) rather than guessing the first keyword.
    assert events.parse_ack("one of DONE, WORKING, WAITING, STUCK -> n7", "n7") is None
    assert events.parse_ack("i was WORKING but now DONE n7", "n7") is None


def test_parse_ack_wrong_typed_inputs_never_raise():
    for text in (None, 5, [], {"x": 1}):
        assert events.parse_ack(text, "n") is None
    for nonce in (None, "", 5, []):
        assert events.parse_ack("WORKING n", nonce) is None
