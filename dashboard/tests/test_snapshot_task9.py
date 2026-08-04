"""Task 9 — the three decision surfaces, wired into the snapshot + the tower-seen endpoint.

The gloss/mapping cores (``lib/tower``, ``lib/cards``, ``lib/desk``) are unit-tested in their own
files. Here we prove the ASSEMBLER folds them in: the tower window carries the radio-flavored comms
rows and the "since you last looked" divider; Needs You cards carry the plain headline/gloss and the
conflict-cap collision sentence; every flight carries a drawer; and the ``POST /api/tower-seen``
endpoint advances the persisted watermark (a dashboard-local write — never GitHub).
"""
import json
import os
import shutil

import pytest

import desk as desk_mod
import flights
import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
SLUG = "will-titan/superlooper-sandbox"
NOW = 1783364300


@pytest.fixture
def home(tmp_path):
    dst = tmp_path / "will-titan__superlooper-sandbox"
    shutil.copytree(FIXTURE, dst)
    for iid in ("i16", "i23"):
        os.utime(dst / "state" / "activity" / iid, (NOW - 100, NOW - 100))
    return dst


def _config(home, **over):
    repo = {"slug": SLUG, "owner": "will-titan", "name": "superlooper-sandbox",
            "state_home": str(home), "idle_seconds": 480, "freeze_seconds": 2700,
            "required_checks": ["tests"], "airline": "Sandbox Air"}
    cfg = {"poll_seconds": 2, "heartbeat_down_seconds": 300, "repos": [repo]}
    cfg.update(over)
    return cfg


# =============================== tower log — radio flavor + raw + divider ===============================

def test_tower_rows_carry_radio_flavor_and_kind_beside_the_plain_sentence(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    rows = snap["repos"][0]["tower_log"]
    assert rows
    for r in rows:
        for key in ("ts", "hhmm", "text", "radio", "kind", "num", "raw", "fresh"):
            assert key in r, "tower row missing %r" % key
        assert r["text"]                              # the real sentence is always present
    # the launch row reads as a departure with its radio call beside it
    launch = next(r for r in rows if "depart" in r["text"].lower())
    assert launch["radio"]


def test_tower_divider_marks_rows_since_the_persisted_watermark(home, tmp_path):
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    d.mark_tower_seen(1783363800)                     # between i23's launch/blocked and its merge
    snap = server.assemble_snapshot(_config(home), now=NOW, desk=d)
    rows = snap["repos"][0]["tower_log"]
    assert snap["tower_last_seen"] == 1783363800
    fresh = [r for r in rows if r["fresh"]]
    assert fresh, "the merge after the watermark should be fresh"
    dividers = [r for r in rows if r.get("divider")]
    assert len(dividers) == 1                          # exactly one 'since you last looked' line
    assert snap["repos"][0]["tower_new"] == len(fresh)


def test_no_desk_means_no_divider_and_null_watermark(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["tower_last_seen"] is None
    assert all(r["fresh"] is False for r in snap["repos"][0]["tower_log"])


def test_divider_is_drawn_even_when_more_rows_are_fresh_than_the_window(tmp_path):
    # The divider is applied to the DISPLAYED window, so a flood of new traffic (more fresh than the
    # window shows) still draws exactly one divider — never a "N NEW" badge with no line (Codex).
    dst = tmp_path / "will-titan__flood"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "running", "branch": "sl/i1", "pr": None}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    lines = [{"ts": NOW - 300 + i, "act": "nudge", "id": "i1", "num": 1, "message": "tick %d" % i}
             for i in range(30)]
    (dst / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\n")
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    d.mark_tower_seen(NOW - 1000)                      # older than all 30 → all fresh
    snap = server.assemble_snapshot(_config(dst), now=NOW, desk=d)
    rows = snap["repos"][0]["tower_log"]
    assert len(rows) <= 16                            # the server windows to a bounded display slice
    assert all(r["fresh"] for r in rows)              # every shown row is new
    assert sum(1 for r in rows if r.get("divider")) == 1   # …and the line is still drawn (row 0)
    assert snap["repos"][0]["tower_new"] == 30        # the badge counts ALL new, not just the shown


def test_assembled_needs_william_and_bounced_cards(tmp_path):
    dst = tmp_path / "will-titan__decisions"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i9": {"status": "needs_william", "branch": "sl/i9", "pr": None},
        "i8": {"status": "bounced", "branch": "sl/i8", "pr": None}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    (dst / "state" / "blocked").mkdir(parents=True)
    (dst / "state" / "blocked" / "i8").write_text("BOUNCED: premise gone. Proposed amendment: restyle.")
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 200, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "which phrasing do you prefer?"}) + "\n")
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    by = {c["num"]: c for c in snap["needs_you"]}
    assert by[9]["kind"] == "needs-owner" and by[9]["badge"].startswith("AWAITING")
    assert by[8]["kind"] == "bounced" and by[8]["badge"] == "BOUNCED"
    assert "amend" in (by[8]["memo"] or "").lower() or "amend" in by[8]["gloss"]["plain"].lower()


# =============================== routine bookkeeping tier (issue #36) ===============================

def test_relabel_rows_are_tagged_routine_and_still_present(home):
    # The fixture journal carries a relabel (label convergence) beside the comms. Server-side
    # classification tags it routine so the client can hide it by default (#36) — but it is still
    # PRESENT in the window (nothing becomes invisible; it stops being announced).
    snap = server.assemble_snapshot(_config(home), now=NOW)
    rows = snap["repos"][0]["tower_log"]
    assert all("tier" in r for r in rows), "every tower row must carry its server-side tier"
    relabels = [r for r in rows if r["kind"] == "relabel"]
    assert relabels, "the fixture's relabel record should still be in the window"
    assert all(r["tier"] == "routine" for r in relabels)
    assert all(r["tier"] == "comms" for r in rows if r["kind"] != "relabel")


def test_routine_noise_never_crowds_real_comms_out_of_the_window(tmp_path):
    # relabel fires several times per launch (GitHub's read lags the write). Those routine repeats
    # must never push real comms rows out of the bounded window, nor inflate the "N NEW" badge (#36).
    dst = tmp_path / "will-titan__noisy"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "running", "branch": "sl/i1", "pr": None}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    recs = []
    # 14 real comms launches, each trailed by 5 routine relabels — comms are heavily outnumbered.
    for i in range(14):
        recs.append({"ts": NOW - 5000 + i * 10, "act": "launch", "id": "i1", "num": 1})
        for j in range(5):
            recs.append({"ts": NOW - 5000 + i * 10 + j + 1, "act": "relabel", "id": "i1", "num": 1,
                         "add": ["in-progress"], "remove": ["agent-ready"], "outcome": "ok"})
    (dst / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    d.mark_tower_seen(NOW - 100000)                   # everything is newer than the watermark
    snap = server.assemble_snapshot(_config(dst), now=NOW, desk=d)
    rows = snap["repos"][0]["tower_log"]
    comms = [r for r in rows if r["tier"] == "comms"]
    assert len(comms) >= 14, "all real comms rows must survive the window, noise notwithstanding"
    # the "N NEW" badge counts real comms traffic, not the routine relabel repeats
    assert snap["repos"][0]["tower_new"] == 14


def test_pathological_routine_flood_never_drops_a_comms_row(tmp_path):
    # Even a pathological flood — one real launch buried under 200 relabels, far past the window's
    # row cap — must keep the comms row VISIBLE with its "since you last looked" divider. The cap
    # trims only routine noise, never real traffic (#36 core guarantee; Codex review).
    dst = tmp_path / "will-titan__flood36"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "running", "branch": "sl/i1", "pr": None}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    recs = [{"ts": NOW - 500, "act": "launch", "id": "i1", "num": 1}]
    recs += [{"ts": NOW - 400 + i, "act": "relabel", "id": "i1", "num": 1, "outcome": "ok"}
             for i in range(200)]
    (dst / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    d.mark_tower_seen(NOW - 100000)                   # everything is newer than the watermark
    snap = server.assemble_snapshot(_config(dst), now=NOW, desk=d)
    rows = snap["repos"][0]["tower_log"]
    comms = [r for r in rows if r["tier"] == "comms"]
    assert len(comms) == 1, "the one real launch must survive the flood, never crowded out"
    assert any(r.get("divider") for r in comms), "its 'since you last looked' divider must be drawn"
    assert snap["repos"][0]["tower_new"] == 1         # exactly one real new comms, not 200 relabels
    assert len(rows) <= 120                            # the window is still bounded (routine trimmed)


def test_journal_firehose_still_carries_the_routine_records(home):
    # The boring-mode firehose (`journal_tail`) is the complete, unglossed record — hiding a relabel
    # from the tower log must NOT remove it from the firehose (#36: nothing becomes invisible).
    snap = server.assemble_snapshot(_config(home), now=NOW)
    acts = [r.get("act") for r in snap["journal_tail"]]
    assert "relabel" in acts, "the relabel must remain in the untouched journal firehose"


# =============================== Needs You — plain headline + gloss + conflict-cap ===============================

def test_needs_you_cards_lead_with_a_plain_headline_and_hover_term(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    c7 = next(c for c in snap["needs_you"] if c["num"] == 7)
    assert c7["kind"] == "parked"
    assert c7["headline"]                              # a plain sentence, not a bare label
    assert c7["gloss"]["plain"] and c7["gloss"]["term"] == "parked"
    assert c7["badge"].lower().startswith("parked")   # badge keeps the exact age numeral
    assert c7["discuss_default"] is False


def _conflict_home(dst, now):
    """A parked flight that went around (a regenerate in its journal) — the conflict-cap case."""
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i16": {"status": "parked", "branch": "sl/i16-r1", "pr": 18}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(now - 5))
    (dst / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": now - 5000, "act": "launch", "id": "i16", "num": 16},
        {"ts": now - 4000, "act": "regenerate", "id": "i16", "num": 16, "conflicts": 1},
        {"ts": now - 500, "act": "park", "id": "i16", "num": 16,
         "memo": "still conflicts after the rebuild — scope may be too wide"},
    ]) + "\n")


def test_conflict_cap_card_names_the_collision_with_discuss_default(tmp_path):
    dst = tmp_path / "will-titan__conflict"
    _conflict_home(dst, NOW)
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    card = next(c for c in snap["needs_you"] if c["num"] == 16)
    assert card["kind"] == "conflict-cap"
    assert card["collision"] and "SL-16" in card["collision"]
    assert card["discuss_default"] is True
    assert "CONFLICT" in card["badge"]


# =============================== the drawer — attached to every flight ===============================

def test_every_flight_carries_a_drawer(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    for f in snap["flights"]:
        d = f["drawer"]
        for key in ("title", "circuit", "clearance", "links", "memos", "cargo", "journal", "go_arounds"):
            assert key in d, "drawer missing %r" % key
        assert [s["stage"] for s in d["circuit"]] == list(flights.CIRCUIT_STAGES)
        assert {c["key"] for c in d["clearance"]} == {"report", "review", "ci", "mergeable"}


def test_drawer_title_uses_the_github_title_when_reachable(home):
    class _Gh:
        def open_issues(self, repo, label=None, limit=200): return []
        def issue(self, repo, num): return {"number": num, "title": "Add a motto footer"}
        def pr_for_branch(self, repo, branch): return {}
        def pr_comments(self, repo, num): return []
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_Gh())
    f23 = next(f for f in snap["flights"] if f["num"] == 23)
    assert f23["drawer"]["title"] == "Add a motto footer"


def test_drawer_journal_slice_is_only_this_flights_records(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    f7 = next(f for f in snap["flights"] if f["num"] == 7)
    for entry in f7["drawer"]["journal"]:
        assert '"i7"' in entry["raw"] or '"num":7' in entry["raw"] or '"num": 7' in entry["raw"]


# =============================== POST /api/tower-seen — the dashboard-local write ===============================

def _post(path, payload, desk=None, origin=None, host=None):
    body = json.dumps(payload).encode("utf-8")
    return server.route("POST", path, (lambda: {}), static_root="/nonexistent",
                        body=body, origin=origin, host=host, desk=desk)


def test_tower_seen_advances_the_watermark(tmp_path):
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    resp = _post("/api/tower-seen", {"ts": 1783364266}, desk=d)
    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True
    assert d.tower_last_seen() == 1783364266


def test_tower_seen_bad_ts_is_400(tmp_path):
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    resp = _post("/api/tower-seen", {"ts": "nope"}, desk=d)
    assert resp.status == 400
    assert d.tower_last_seen() is None


def test_tower_seen_without_desk_is_405(tmp_path):
    # Not wired (a read-only embedder) → method not allowed, never a crash.
    resp = _post("/api/tower-seen", {"ts": 1}, desk=None)
    assert resp.status == 405


def test_tower_seen_cross_origin_is_refused(tmp_path):
    # It writes only the dashboard's own file, but CSRF hygiene still applies (loopback bright line).
    d = desk_mod.Desk(str(tmp_path / "desk.json"))
    resp = _post("/api/tower-seen", {"ts": 1783364266}, desk=d,
                 origin="https://evil.example.com", host="127.0.0.1:8611")
    assert resp.status == 403
    assert d.tower_last_seen() is None


def test_tower_seen_never_reaches_gh_actions():
    # The endpoint is dashboard-local — it must not require or touch the gh action verbs.
    resp = server.route("POST", "/api/tower-seen", (lambda: {}), static_root="/x",
                        actions=None, body=b'{"ts": 5}', desk=desk_mod.Desk("/nonexistent/x"))
    # desk write to /nonexistent fails silently (mkdir may fail) but the route still answers cleanly.
    assert resp.status in (200, 400)
