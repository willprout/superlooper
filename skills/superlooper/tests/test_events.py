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
