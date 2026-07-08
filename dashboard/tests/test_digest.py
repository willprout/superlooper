"""Mechanical morning digest (Task 11 / design record §4) — counts + one sentence per exception.

The digest is the LOAD-BEARING answer to "what happened" (§4): a mechanically generated plain
account — counts plus one honest sentence per exception (parks, go-arounds, freeze arcs, …) — over
a timestamped, clickable event table. No AI, no composed prose (§9): every sentence is a pure
function of a journal record. These tests pin that derivation against fixture-shaped journals.
"""
import json
import os

import digest


HOME = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")


def _rec(ts, act, **kw):
    r = {"ts": ts, "act": act}
    r.update(kw)
    return r


def _fixed_hhmm(ts):
    return "%02d:00" % (int(ts) % 24)


# --------------------------- windowing + counts ---------------------------

def test_empty_journal_is_empty_and_clean():
    d = digest.build_digest([], slug="o/r")
    assert d["empty"] is True
    assert d["clean"] is True
    assert d["events"] == []
    assert d["exceptions"] == []
    assert d["counts"]["landings"] == 0


def test_counts_tally_the_windowed_records():
    j = [
        _rec(100, "launch", num=1, outcome="ok"),
        _rec(200, "merge", num=1, pr=5, outcome="ok"),
        _rec(210, "merge", num=2, pr=6, outcome="ok", wander=True),
        _rec(300, "regenerate", num=3, conflicts=1),
        _rec(400, "park", num=4, needs_william=False),
        _rec(500, "hold", num=5),
        _rec(600, "merge", num=6, outcome="failed"),
        _rec(700, "freeze"),
        _rec(800, "alert"),
    ]
    c = digest.build_digest(j, slug="o/r")["counts"]
    assert c["departures"] == 1
    assert c["landings"] == 2                 # both successful merges
    assert c["wandered"] == 1                 # a subset of landings
    assert c["go_arounds"] == 1
    assert c["parks"] == 1
    assert c["holds"] == 1
    assert c["missed_approaches"] == 1
    assert c["freezes"] == 1
    assert c["alerts"] == 1


def test_window_bounds_restrict_counts_and_events():
    j = [_rec(t, "merge", num=t, outcome="ok") for t in (50, 100, 150, 200)]
    d = digest.build_digest(j, slug="o/r", start=100, end=150)
    assert d["counts"]["landings"] == 2
    assert [e["ts"] for e in d["events"]] == [100, 150]


def test_non_dict_and_non_finite_records_dropped():
    j = ["junk", [1], _rec(float("nan"), "park", num=1), _rec(100, "launch", num=2)]
    d = digest.build_digest(j, slug="o/r")
    assert len(d["events"]) == 1
    assert d["events"][0]["num"] == 2


def test_finite_out_of_range_ts_never_raises():
    # A corrupt-but-finite ts must degrade (blank HH:MM), never crash the digest (Codex review).
    d = digest.build_digest([_rec(10 ** 20, "park", num=1, needs_william=False)], slug="o/r")
    assert len(d["events"]) == 1
    assert d["events"][0]["hhmm"] == ""
    assert len(d["exceptions"]) == 1


# --------------------------- the event table (timestamped, clickable) ---------------------------

def test_routine_bookkeeping_stays_in_the_event_table():
    # The tower log hides routine bookkeeping (relabel) by default (#36), but the digest's event
    # table is the full firehose — every record is still glossed and present, none filtered out.
    j = [_rec(100, "launch", num=1), _rec(150, "relabel", num=1, add=["in-progress"]),
         _rec(200, "merge", num=1, pr=3, outcome="ok")]
    events = digest.build_digest(j, slug="o/r", hhmm=_fixed_hhmm)["events"]
    assert [json.loads(e["raw"])["act"] for e in events] == ["launch", "relabel", "merge"]
    relabel = next(e for e in events if json.loads(e["raw"])["act"] == "relabel")
    assert relabel["text"]                                # still glossed to a plain sentence


def test_events_are_chronological_glossed_and_clickable():
    j = [_rec(300, "launch", num=3), _rec(100, "park", num=1, needs_william=False)]
    events = digest.build_digest(j, slug="o/r", hhmm=_fixed_hhmm)["events"]
    assert [e["ts"] for e in events] == [100, 300]        # sorted by ts
    first = events[0]
    assert first["num"] == 1
    assert first["hhmm"] == _fixed_hhmm(100)
    assert "parked" in first["text"].lower()              # glossed via lib.tower
    assert json.loads(first["raw"])["act"] == "park"      # click-through to ground truth


# --------------------------- one sentence per exception ---------------------------

def test_park_is_one_exception_sentence_with_its_memo():
    rec = _rec(100, "park", num=7, needs_william=False, memo="answerer timed out after 15 min")
    exc = digest.build_digest([rec], slug="o/r", hhmm=_fixed_hhmm)["exceptions"]
    assert len(exc) == 1
    e = exc[0]
    assert e["kind"] == "park"
    assert e["num"] == 7
    assert "SL-7" in e["sentence"]
    assert "answerer timed out" in e["sentence"]
    assert json.loads(e["raw"]) == rec


def test_needs_william_park_is_an_awaiting_exception():
    exc = digest.build_digest([_rec(100, "park", num=8, needs_william=True, memo="which API?")],
                              slug="o/r")["exceptions"]
    assert exc[0]["kind"] == "awaiting"
    assert exc[0]["num"] == 8


def test_go_around_exception_names_the_conflict():
    exc = digest.build_digest([_rec(100, "regenerate", num=4, conflicts=2)], slug="o/r")["exceptions"]
    assert len(exc) == 1
    assert exc[0]["kind"] == "go_around"
    assert "SL-4" in exc[0]["sentence"]
    assert "#2" in exc[0]["sentence"]


def test_freeze_arc_pairs_freeze_with_its_unfreeze():
    j = [_rec(1000, "freeze"), _rec(1000 + 3600, "unfreeze")]
    exc = digest.build_digest(j, slug="o/r", hhmm=_fixed_hhmm)["exceptions"]
    arcs = [e for e in exc if e["kind"] == "freeze_arc"]
    assert len(arcs) == 1
    s = arcs[0]["sentence"].lower()
    assert "paused" in s and "resumed" in s
    assert "1h" in arcs[0]["sentence"]                    # the arc's duration


def test_unresolved_freeze_arc_reads_as_still_paused():
    exc = digest.build_digest([_rec(1000, "freeze")], slug="o/r")["exceptions"]
    arcs = [e for e in exc if e["kind"] == "freeze_arc"]
    assert len(arcs) == 1
    assert "still paused" in arcs[0]["sentence"].lower()


def test_failed_merge_is_a_missed_approach_exception():
    exc = digest.build_digest([_rec(100, "merge", num=9, outcome="conflict")], slug="o/r")["exceptions"]
    assert exc[0]["kind"] == "missed_approach"
    assert exc[0]["num"] == 9


def test_clean_window_has_no_exceptions():
    j = [_rec(100, "launch", num=1, outcome="ok"), _rec(200, "merge", num=1, pr=3, outcome="ok")]
    d = digest.build_digest(j, slug="o/r")
    assert d["exceptions"] == []
    assert d["clean"] is True
    assert d["empty"] is False


def test_exceptions_are_time_ordered():
    j = [_rec(400, "regenerate", num=4, conflicts=1),
         _rec(100, "park", num=1, needs_william=False),
         _rec(200, "freeze"), _rec(300, "unfreeze")]
    exc = digest.build_digest(j, slug="o/r")["exceptions"]
    assert [e["ts"] for e in exc] == [100, 200, 400]      # freeze arc sorts by its freeze ts


# --------------------------- against the real fixture journal ---------------------------

def test_fixture_journal_digest_is_coherent():
    import readers
    journal = readers.read_journal(HOME)
    d = digest.build_digest(journal, slug="will-titan/command-center", name="command-center")
    assert not d["empty"]
    assert d["counts"]["landings"] == 1                   # i23 merged
    assert d["counts"]["go_arounds"] == 1                 # i16 regenerated
    assert d["counts"]["parks"] == 1                      # i7 parked
    kinds = {e["kind"] for e in d["exceptions"]}
    assert {"park", "go_around"} <= kinds
