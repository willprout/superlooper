"""Night replay (Task 11 / design record §4) — a journal window becomes ordered field frames.

The replay is a beloved TREAT (§0.1, §4): a scrubbable time-lapse of the append-only journal. These
tests pin the PURE derivation — the frames that drive the field engine — against fixture-shaped
journals: windowing, chronological order, cumulative flights, discrete stage reconstruction through
the same tested truth layer the live field uses, and the click-through payload each frame carries.
"""
import json
import os

import flights
import replay


HOME = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")


def _rec(ts, act, **kw):
    r = {"ts": ts, "act": act}
    r.update(kw)
    return r


def _fixed_hhmm(ts):
    return "12:00"


# --------------------------- windowing + ordering ---------------------------

def test_empty_journal_is_empty_replay():
    rp = replay.build_replay([], slug="o/r")
    assert rp["empty"] is True
    assert rp["frames"] == []
    assert rp["window"]["frames"] == 0


def test_frames_are_chronological_even_when_file_order_is_not():
    # File order deliberately scrambled (mirrors the real journal: a late crash line writes an
    # older ts after a newer block). The replay sorts by ts, so the movie always runs forward.
    j = [_rec(300, "launch", num=3), _rec(100, "launch", num=1), _rec(200, "launch", num=2)]
    rp = replay.build_replay(j, slug="o/r")
    tss = [f["ts"] for f in rp["frames"]]
    assert tss == [100, 200, 300]


def test_non_finite_and_non_dict_records_are_dropped():
    j = [_rec(100, "launch", num=1), "not a dict", [1, 2, 3],
         _rec(float("nan"), "park", num=2), _rec(None, "merge", num=3)]
    rp = replay.build_replay(j, slug="o/r")
    # only the one finite-ts dict record makes a frame
    assert len(rp["frames"]) == 1
    assert rp["frames"][0]["num"] == 1


def test_start_end_bounds_are_inclusive():
    j = [_rec(t, "launch", num=t) for t in (50, 100, 150, 200)]
    rp = replay.build_replay(j, slug="o/r", start=100, end=150)
    assert [f["ts"] for f in rp["frames"]] == [100, 150]


# --------------------------- cumulative flights ---------------------------

def test_flights_accumulate_across_frames():
    j = [_rec(100, "launch", num=1), _rec(200, "launch", num=2), _rec(300, "launch", num=3)]
    rp = replay.build_replay(j, slug="o/r")
    counts = [len(f["flights"]) for f in rp["frames"]]
    assert counts == [1, 2, 3]                     # each new flight joins and stays


def test_frame_flight_shape_has_engine_keys():
    j = [_rec(100, "launch", num=7)]
    f = replay.build_replay(j, slug="o/r")["frames"][0]["flights"][0]
    for k in ("num", "label", "stage", "circuit_stage", "runway", "contrail",
              "spinning", "trouble", "tail"):
        assert k in f, k
    assert f["num"] == 7


# --------------------------- stage reconstruction (the truth layer, in motion) ---------------------------

def test_launch_then_build_then_merge_walks_the_circuit():
    j = [_rec(100, "launch", num=5),
         _rec(200, "update", num=5, outcome="working"),
         _rec(300, "event", event={"type": "session_finished", "id": "i5"}),
         _rec(400, "gate", num=5, outcome="ok"),
         _rec(500, "merge", num=5, pr=9, outcome="ok")]
    frames = replay.build_replay(j, slug="o/r")["frames"]

    def stage_at(i):
        return frames[i]["flights"][0]["stage"]

    assert stage_at(0) == flights.TAKEOFF        # just launched
    assert stage_at(1) == flights.DOWNWIND       # session doing work
    assert stage_at(2) == flights.BASE_TURN      # report filed
    assert stage_at(3) == flights.FINAL          # gate cleared
    assert stage_at(4) == flights.TOUCHDOWN      # landed


def test_park_reads_as_parked_and_needs_william_as_awaiting():
    parked = replay.build_replay([_rec(100, "park", num=1, needs_william=False)], slug="o/r")
    assert parked["frames"][0]["flights"][0]["stage"] == flights.PARKED
    amber = replay.build_replay([_rec(100, "park", num=2, needs_william=True)], slug="o/r")
    assert amber["frames"][0]["flights"][0]["stage"] == flights.AWAITING


def test_frozen_event_reads_as_session_frozen():
    j = [_rec(100, "launch", num=1), _rec(200, "event", event={"type": "frozen", "id": "i1"})]
    frames = replay.build_replay(j, slug="o/r")["frames"]
    assert frames[-1]["flights"][0]["stage"] == flights.SESSION_FROZEN
    assert frames[-1]["flights"][0]["contrail"] == "none"   # grey, no contrail (§5)


def test_hold_reads_as_holding():
    frames = replay.build_replay([_rec(100, "hold", num=1)], slug="o/r")["frames"]
    assert frames[0]["flights"][0]["stage"] == flights.HOLDING


def test_regenerate_increments_attempt_and_relaunches():
    j = [_rec(100, "launch", num=4),
         _rec(200, "event", event={"type": "session_finished", "id": "i4"}),
         _rec(300, "regenerate", num=4, conflicts=1, new_branch="sl/i4-r1")]
    frames = replay.build_replay(j, slug="o/r")["frames"]
    last = frames[-1]["flights"][0]
    assert last["label"] == flights.flight_label(4, 2)     # SL-4·A2
    assert last["stage"] == flights.TAKEOFF                # go-around: back to a fresh takeoff


# --------------------------- click-through payload ---------------------------

def test_each_frame_carries_its_event_for_click_through():
    rec = _rec(100, "park", num=9, needs_william=False, memo="answerer timed out")
    frame = replay.build_replay([rec], slug="o/r")["frames"][0]
    assert frame["num"] == 9
    assert frame["kind"] == "park"
    assert "parked" in frame["text"].lower()               # glossed sentence (lib.tower)
    assert json.loads(frame["raw"]) == rec                 # the exact journal record


def test_hhmm_is_injected():
    frame = replay.build_replay([_rec(100, "launch", num=1)], slug="o/r",
                                hhmm=_fixed_hhmm)["frames"][0]
    assert frame["hhmm"] == "12:00"


# --------------------------- lighting + tower status ---------------------------

def test_daypart_tracks_each_events_own_clock():
    # Two events; the frame lighting is the living clock at THAT event's ts, not "now".
    j = [_rec(1783188000, "launch", num=1), _rec(1783230000, "launch", num=2)]
    frames = replay.build_replay(j, slug="o/r")["frames"]
    assert frames[0]["daypart"] == flights.daypart(1783188000)
    assert frames[1]["daypart"] == flights.daypart(1783230000)


def test_status_is_attention_when_an_off_path_flight_is_present():
    ok = replay.build_replay([_rec(100, "launch", num=1)], slug="o/r")["frames"][0]
    assert ok["status"] == "ok"
    trouble = replay.build_replay([_rec(100, "park", num=1)], slug="o/r")["frames"][0]
    assert trouble["status"] == "attention"


# --------------------------- truncation ---------------------------

def test_max_frames_keeps_the_most_recent_window_and_flags_truncation():
    j = [_rec(i, "launch", num=i) for i in range(1, 11)]
    rp = replay.build_replay(j, slug="o/r", max_frames=4)
    assert len(rp["frames"]) == 4
    assert [f["ts"] for f in rp["frames"]] == [7, 8, 9, 10]   # most recent kept
    assert rp["window"]["truncated"] is True


# --------------------------- fail-tolerance + windowed fidelity (Codex review, round 1) ---------------------------

def test_finite_but_out_of_range_ts_never_raises():
    # A corrupt-but-FINITE ts (a huge/negative epoch that slips past the NaN screen) overflows
    # time.localtime — the replay must degrade, never 500 the endpoint on one bad line.
    for bad in (10 ** 20, -(10 ** 20)):
        rp = replay.build_replay([_rec(bad, "launch", num=1)], slug="o/r")
        assert len(rp["frames"]) == 1
        assert rp["frames"][0]["daypart"] == "day"   # guarded fallback lighting
        assert rp["frames"][0]["hhmm"] == ""


def test_windowed_replay_inherits_pre_window_state():
    # A flight that launched BEFORE the window is still in the air when the window opens — it must
    # not replay as at-stand (a plane teleporting back to the gate would be a lie, even for a treat).
    j = [_rec(1, "launch", num=1), _rec(2, "update", num=1, outcome="working")]
    rp = replay.build_replay(j, slug="o/r", start=2)
    assert [f["ts"] for f in rp["frames"]] == [2]            # only the in-window record emits a frame
    assert rp["frames"][0]["flights"][0]["stage"] == flights.DOWNWIND   # launch pre-roll carried in


# --------------------------- against the real fixture journal ---------------------------

def test_fixture_journal_reconstructs_a_coherent_movie():
    import readers
    journal = readers.read_journal(HOME)
    rp = replay.build_replay(journal, slug="will-titan/command-center", name="command-center")
    assert not rp["empty"]
    # i23 lands in the fixture — its final reconstructed stage is a touchdown.
    last = rp["frames"][-1]
    by_num = {fl["num"]: fl for fl in last["flights"]}
    assert by_num[23]["stage"] == flights.TOUCHDOWN
    # i7 was parked; i16 regenerated (attempt 2).
    assert by_num[7]["stage"] == flights.PARKED
    assert by_num[16]["label"] == flights.flight_label(16, 2)
