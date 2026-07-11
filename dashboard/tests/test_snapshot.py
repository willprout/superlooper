"""Task 5 — assembling the honest snapshot from a real state home.

``assemble_snapshot`` is the composition layer: it folds the tested pure truth functions
(``lib/readers`` → ``lib/flights``, fed by ``lib/gh`` + ``lib/pollers``) into the one JSON document
the front-end binds. It computes NO new semantics — every derived field routes through the pure
functions already pinned by their own tests — so these tests assert the *assembly contract*: the
right flights appear, the boards order correctly, Needs You collapses when empty, the pill names the
worst offender, and every value the boring table sorts by is present.

Data comes from the committed ``tests/fixtures/statehome`` (built from real sample shapes, never
invented). Tests copy it to a tmp dir so ``now``, the heartbeat, and activity mtimes are controlled
— the snapshot is then fully deterministic. ``gh`` is injected (a stub or ``None``): with no GitHub
reachable the assembler still produces an honest, if title-less, snapshot — exactly what a poll must
do when the network is down.
"""
import json
import os
import shutil

import pytest

import flights
import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "statehome")
SLUG = "will-titan/superlooper-sandbox"
NOW = 1783364300   # just after i23's merge (ts 1783364266) in the fixture journal


@pytest.fixture
def home(tmp_path):
    """A writable copy of the committed state home, so mtimes/heartbeat are ours to control."""
    dst = tmp_path / "will-titan__superlooper-sandbox"
    shutil.copytree(FIXTURE, dst)
    # Deterministic activity ages: the two live-session markers read FRESH as of NOW.
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


def _fresh_heartbeat(home):
    (home / "state" / "runner.heartbeat").write_text(str(NOW - 10))


def _calm(home):
    """Drop the trouble markers so the fixture reads like the healthy screen-7a field."""
    _fresh_heartbeat(home)
    (home / "state" / "ALERT").unlink()
    (home / "state" / "merges_frozen.json").unlink()


# =============================== top-level shape ===============================

def test_snapshot_carries_every_panel_the_shell_binds(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    for key in ("generated_at", "poll_seconds", "pill", "tower_status", "runner", "usage",
                "trouble", "needs_you", "all_clear", "repos", "flights", "journal_tail"):
        assert key in snap, "snapshot missing %r" % key
    assert snap["poll_seconds"] == 2
    assert snap["generated_at"] == NOW
    assert len(snap["repos"]) == 1
    repo = snap["repos"][0]
    for key in ("slug", "airline", "flights", "boards", "tower_log", "shipped", "incident", "state",
                "lanes", "queue_empty_caption", "state_format"):
        assert key in repo, "repo snapshot missing %r" % key
    assert set(repo["boards"]) == {"departures", "arrivals"}


# =============================== state-format handshake (issue #45) ===============================
# The fixture home predates the handshake (no stamp), so the assembled snapshot must grandfather it —
# render normally, never blank. A newer/unreadable stamp must instead surface a NAMED mismatch.

def _stamp(home, body):
    (home / "state" / "state_format.json").write_text(body)


def test_unstamped_home_is_grandfathered_compatible(home):
    repo = server.assemble_snapshot(_config(home), now=NOW)["repos"][0]
    sf = repo["state_format"]
    assert sf["compatible"] is True and sf["present"] is False and sf["message"] is None


def test_current_version_stamp_is_compatible(home):
    _stamp(home, '{"version": 1}')
    sf = server.assemble_snapshot(_config(home), now=NOW)["repos"][0]["state_format"]
    assert sf["compatible"] is True and sf["version"] == 1 and sf["message"] is None


def test_unknown_version_stamp_surfaces_a_named_mismatch(home):
    # An engine that bumped the state format past what this dashboard reads: the snapshot names it
    # instead of the readers silently blanking every field they can no longer parse.
    _stamp(home, '{"version": 99}')
    sf = server.assemble_snapshot(_config(home), now=NOW)["repos"][0]["state_format"]
    assert sf["compatible"] is False and sf["version"] == 99
    assert "v99" in sf["message"]


def test_corrupt_stamp_surfaces_a_mismatch_not_a_blank(home):
    _stamp(home, "{ half-written not json")
    sf = server.assemble_snapshot(_config(home), now=NOW)["repos"][0]["state_format"]
    assert sf["compatible"] is False and "unreadable" in sf["message"].lower()


# =============================== flights ===============================

def test_every_issue_in_state_becomes_a_flight(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    by_num = {f["num"]: f for f in snap["flights"]}
    assert set(by_num) == {23, 16, 15, 7, 21}
    assert by_num[7]["stage"] == flights.PARKED
    assert by_num[21]["stage"] == flights.PARKED
    assert by_num[15]["stage"] == flights.HOLDING
    assert by_num[23]["stage"] == flights.TOUCHDOWN
    assert by_num[16]["stage"] == flights.TOUCHDOWN


def test_wandered_landing_earns_no_celebration(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    f23 = next(f for f in snap["flights"] if f["num"] == 23)
    assert f23["wander"] is True
    assert f23["celebrate"] is False


def test_each_flight_carries_the_boring_table_numerals(home):
    # Every boring-mode visual channel pairs with an EXACT numeral the table sorts by (design §4).
    snap = server.assemble_snapshot(_config(home), now=NOW)
    f = next(f for f in snap["flights"] if f["num"] == 7)
    d = f["display"]
    for key in ("flight", "repo", "stage", "elapsed", "elapsed_seconds",
                "idle", "idle_seconds", "diff", "files", "attempt", "note", "staleness"):
        assert key in d, "flight display missing %r" % key
    assert d["flight"] == "SL-7"
    assert d["repo"] == "superlooper-sandbox"


# =============================== boards ===============================

def test_arrivals_are_landed_flights_newest_first(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    arrivals = snap["repos"][0]["boards"]["arrivals"]
    nums = [a["num"] for a in arrivals]
    assert 23 in nums and 16 in nums
    # i23 merged (ts 1783364266) is the most recent landing → first row.
    assert arrivals[0]["num"] == 23


def test_wandered_arrival_is_flagged_see_report_not_celebrated(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    a23 = next(a for a in snap["repos"][0]["boards"]["arrivals"] if a["num"] == 23)
    assert "see report" in a23["remark"].lower()


def test_second_attempt_arrival_is_marked(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    a16 = next(a for a in snap["repos"][0]["boards"]["arrivals"] if a["num"] == 16)
    assert "2" in a16["remark"]  # the go-around is surfaced (attempt 2)


def test_departures_empty_without_a_reachable_queue(home):
    # No gh → no readable agent-ready queue → an honest empty board ("queue empty, runways open").
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["repos"][0]["boards"]["departures"] == []


# --- the empty-queue caption reflects the repo's real lanes (issue #35) ---
# The lane count travels server → snapshot → caption; the JS binds `queue_empty_caption` verbatim
# (design record B.1). The snapshot also carries the raw `lanes` so the truth is inspectable.

def test_empty_queue_caption_reflects_the_repos_configured_lanes(home):
    cfg = _config(home)
    cfg["repos"][0]["lanes"] = 3
    repo = server.assemble_snapshot(cfg, now=NOW)["repos"][0]
    assert repo["lanes"] == 3
    assert repo["queue_empty_caption"] == "QUEUE EMPTY · 3 RUNWAYS OPEN"


def test_single_lane_repo_gets_a_singular_runway_caption(home):
    cfg = _config(home)
    cfg["repos"][0]["lanes"] = 1
    repo = server.assemble_snapshot(cfg, now=NOW)["repos"][0]
    assert repo["queue_empty_caption"] == "QUEUE EMPTY · 1 RUNWAY OPEN"


def test_unknown_lane_count_yields_a_numberless_caption(home):
    # No `lanes` on the repo entry (an unreadable/older adopted config) → the caption drops the
    # runway clause entirely: an honest empty board with no invented number (issue #35 DoD).
    repo = server.assemble_snapshot(_config(home), now=NOW)["repos"][0]
    assert repo["lanes"] is None
    assert repo["queue_empty_caption"] == "QUEUE EMPTY"


# ---- arrivals backlog cap (issue #30 owner amendment): applied where _arrivals is built ----
def _landed(num, merged_ts, attempt=1, wander=False):
    return {"num": num, "label": "SL-%d" % num, "stage": flights.TOUCHDOWN,
            "attempt": attempt, "wander": wander, "display": {"merged_ts": merged_ts}}


def test_arrivals_backlog_caps_at_five_pages_newest_first():
    now = 1_800_000_000
    flts = [_landed(i, now - i) for i in range(30)]          # 30 recent landings, num i is i sec old
    rows = server._arrivals(flts, {}, now)
    assert len(rows) == 25                                   # 5 pages × 5 rows, no more
    assert [r["num"] for r in rows[:3]] == [0, 1, 2]         # newest first


def test_arrivals_backlog_drops_landings_older_than_three_days():
    now = 1_800_000_000
    flts = [_landed(1, now - 60), _landed(2, now - 5 * 86400)]  # #2 is 5 days old → drops off
    rows = server._arrivals(flts, {}, now)
    assert [r["num"] for r in rows] == [1]


def test_arrivals_keeps_unprovable_recency_landing():
    now = 1_800_000_000
    flts = [_landed(1, now - 60), _landed(2, None)]          # #2 has no merge proof → still carried
    nums = [r["num"] for r in server._arrivals(flts, {}, now)]
    assert 1 in nums and 2 in nums


# =============================== Needs You + all-clear ===============================

def test_needs_you_lists_parked_flights_with_their_memos(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    nums = {c["num"] for c in snap["needs_you"]}
    assert nums == {7, 21}          # the two parked flights, whole-field
    assert snap["all_clear"] is False
    c7 = next(c for c in snap["needs_you"] if c["num"] == 7)
    assert "answerer" in (c7["memo"] or "")
    assert c7["badge"].lower().startswith("parked")


def test_empty_needs_you_collapses_to_all_clear(tmp_path):
    # A state home with only a merged flight → nothing waits on William → the all-clear ribbon.
    dst = tmp_path / "will-titan__clean"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 50, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["needs_you"] == []
    assert snap["all_clear"] is True


def test_a_decision_appearing_flips_all_clear_and_restores_needs_you(tmp_path):
    # Issue #28: the front-end collapses the panel when all_clear and restores the FULL panel when a
    # decision appears — driven only by the 2s poll re-reading the snapshot, never a reload. This is
    # that driver at the logic layer: the SAME state home, all-clear one moment, then a park lands and
    # the very next assemble flips all_clear false with the waiting card in hand.
    dst = tmp_path / "will-titan__restore"
    (dst / "state").mkdir(parents=True)
    issues = dst / "state" / "issues.json"
    issues.write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 50, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))

    before = server.assemble_snapshot(_config(dst), now=NOW)
    assert before["all_clear"] is True and before["needs_you"] == []

    # A build gives up: the runner parks i3. The next poll re-reads the same home.
    issues.write_text(json.dumps(
        {"version": 1, "issues": {
            "i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2},
            "i3": {"status": "parked", "branch": None, "lane": None, "launches": 0,
                   "retries": 0, "conflicts": 0, "requeue_front": False,
                   "declared_touches": [], "pr": None}}}))
    with (dst / "journal.jsonl").open("a") as f:
        f.write(json.dumps({"ts": NOW - 10, "act": "park", "id": "i3", "num": 3}) + "\n")

    after = server.assemble_snapshot(_config(dst), now=NOW)
    assert after["all_clear"] is False, "a parked decision must flip all_clear so the panel restores"
    assert [c["num"] for c in after["needs_you"]] == [3], "the waiting decision must be in the panel"


def test_needs_you_carries_long_needs_william_memo_untrimmed(tmp_path):
    # Issue #3: a worker/answerer question can be longer than the compact card's old memo well. The
    # assembled dashboard payload must carry the whole memo into the Needs You rendering path; CSS
    # then decides whether the card grows or scrolls, but the text must not be shortened here.
    dst = tmp_path / "will-titan__longmemo"
    (dst / "state").mkdir(parents=True)
    long_memo = (
        "Which behavior should win when the imported fixture has both a stale local cache and a "
        "fresh remote answer?\n\n"
        "Option A: trust the local cache so the command center stays fully offline during the "
        "operator review. Option B: discard the stale cache and require a fresh read before the "
        "session can continue. Recommendation: choose Option B because the decision affects merge "
        "safety, but I need William to approve that tradeoff before I change the dashboard surface."
    )
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {
            "i3": {"status": "needs_william", "branch": "sl/i3-long-memo", "pr": None}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 20, "act": "park", "id": "i3", "num": 3, "needs_william": True,
         "memo": long_memo, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))

    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["all_clear"] is False
    assert len(snap["needs_you"]) == 1
    card = snap["needs_you"][0]
    assert card["kind"] == "needs-william"
    assert card["memo"] == long_memo
    assert "Recommendation: choose Option B" in card["memo"]


# =============================== pill + trouble banner + runner ===============================

def test_absent_heartbeat_reads_runner_down(home):
    # The committed fixture has NO runner.heartbeat → the dead-man's switch trips (a state stale
    # data cannot fake). A dead-man's switch must fail closed, never open.
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["runner"]["down"] is True
    assert snap["pill"]["level"] == "alert"
    assert snap["pill"]["offender"] == SLUG
    assert snap["trouble"]["present"] is True


def test_calm_field_pill_names_the_parked_offender(home):
    _calm(home)
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["runner"]["down"] is False
    assert snap["pill"]["level"] == "attention"   # parked is attention, not a factory-stop
    assert snap["pill"]["state"] == flights.PARKED
    assert snap["pill"]["offender"] == SLUG
    assert snap["tower_status"] == "attention"


def test_all_clear_field_has_no_trouble_banner(tmp_path):
    dst = tmp_path / "will-titan__clean"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text("")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["pill"]["level"] == "ok"
    assert snap["trouble"]["present"] is False


def test_stranded_gate_surfaces_as_its_own_state_end_to_end(tmp_path):
    # The whole pipeline (issue #22): a finished investigation — report on disk, status `gating` —
    # whose activity file has aged past the frozen tier is STRANDED at the gate. The snapshot must
    # name it distinctly (never as a frozen session) on the flight, the pill, and the trouble banner,
    # sort it in the off-path band, and keep the plane LIT under the trouble spotlight — and every
    # word it shows the owner must point at the GATE/runner, never at a dead session.
    dst = tmp_path / "will-titan__strand"
    (dst / "state" / "activity").mkdir(parents=True)
    (dst / "reports").mkdir()
    (dst / "reports" / "i22.md").write_text("## Tests\nok\n")            # report on disk ⇒ session done
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i22": {"status": "gating", "branch": "sl/i22-x", "pr": 9}}}))
    (dst / "journal.jsonl").write_text("")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))        # runner is UP (not runner-down)
    act = dst / "state" / "activity" / "i22"
    act.write_text("")
    os.utime(act, (NOW - 2700 - 60, NOW - 2700 - 60))                   # activity aged past frozen

    snap = server.assemble_snapshot(_config(dst), now=NOW)
    f = next(f for f in snap["flights"] if f["num"] == 22)
    assert f["stage"] == flights.STRANDED
    assert f["stage"] != flights.SESSION_FROZEN
    assert f["display"]["stage_rank"] >= len(flights.CIRCUIT_STAGES)     # sorts in the off-path band
    assert f["display"]["trouble"] is True                              # stays LIT, never dimmed away

    assert snap["pill"]["state"] == flights.STRANDED
    assert snap["pill"]["level"] == "attention"
    assert "gate" in snap["pill"]["message"].lower()                    # points at the gate/runner
    assert "frozen" not in snap["pill"]["message"].lower()              # NOT a dead session
    assert snap["trouble"]["present"] is True
    assert "gate" in snap["trouble"]["text"].lower()


# =============================== shipped-delta + firehose + tower window ===============================

def test_per_repo_shipped_delta_counts_outcomes_only(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    shipped = snap["repos"][0]["shipped"]
    assert shipped["landings_total"] == 1    # only i23's merge is in the committed journal
    assert shipped["go_arounds"] == 1        # i16 regenerate
    assert shipped["parks"] == 1             # i7 park
    # The incident sign: the park is a machine stumble, so the count reset after it.
    assert snap["repos"][0]["incident"]["landings_since_incident"] == 0


def test_journal_firehose_is_populated(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert len(snap["journal_tail"]) >= 1
    # Firehose lines are raw journal records (the vet's escape hatch), each a dict.
    assert all(isinstance(r, dict) for r in snap["journal_tail"])


def test_tower_window_rows_carry_time_flight_and_raw_line(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    rows = snap["repos"][0]["tower_log"]
    assert rows, "tower log should not be empty"
    r = rows[0]
    for key in ("ts", "hhmm", "text", "raw"):
        assert key in r


def test_tower_window_is_chronological_not_file_order(home):
    # The committed fixture journal is deliberately out of ts order (to exercise tolerance). The
    # tower log is a comms feed — it must read in TIME order, never raw file-insertion order.
    snap = server.assemble_snapshot(_config(home), now=NOW)
    ts = [row["ts"] for row in snap["repos"][0]["tower_log"] if row["ts"] is not None]
    assert ts == sorted(ts), "tower rows must be ascending by timestamp"


# =============================== gh injection (titles + queue when reachable) ===============================

class _FakeGh:
    """A stub GitHub adapter with the same surface the assembler calls — no subprocess, no network.
    Proves the assembler folds real titles/queue/gate facts in when GitHub IS reachable."""
    def open_issues(self, repo, label=None, limit=200):
        issues = [{"number": 42, "title": "Add a splash screen",
                   "labels": [{"name": "agent-ready"}]}]
        if label == "agent-ready":
            return issues
        return issues

    def issue(self, repo, num):
        return {"number": num, "title": "Add a motto footer"}

    def pr_for_branch(self, repo, branch):
        return {}

    def pr_comments(self, repo, num):
        return []


def test_reachable_queue_populates_departures(home):
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_FakeGh())
    deps = snap["repos"][0]["boards"]["departures"]
    assert any(d["num"] == 42 for d in deps)
    d = next(d for d in deps if d["num"] == 42)
    assert "splash" in d["destination"].lower()


# =============================== server-owned presentation fields (design record B.1) ===============================
# The JS binds values to pixels and computes NO semantics/numerals — so the server, not the client,
# must supply the sort rank, the in-air flag, the pill sentence, the aggregate counters, and every
# formatted duration string. These pin those fields so the front-end can stay logic-free.

def test_flight_display_carries_stage_rank_and_in_air(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    by_num = {f["num"]: f["display"] for f in snap["flights"]}
    # circuit order: a holding flight ranks after an on-circuit one; both are ints the table sorts by.
    assert isinstance(by_num[15]["stage_rank"], int)
    assert by_num[15]["stage_rank"] > by_num[23]["stage_rank"]   # holding sorts after taxi-in
    # in_air: none of this fixture's flights are in a live-air leg (all merged/parked/holding).
    assert by_num[7]["in_air"] is False


def test_pill_carries_a_ready_made_message(home):
    _calm(home)
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert "parked" in snap["pill"]["message"].lower()


def test_pill_message_says_all_clear_when_ok(tmp_path):
    dst = tmp_path / "will-titan__clean"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text("")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["pill"]["message"] == "all systems ok"


def test_global_shipped_total_and_live_cargo_are_server_side(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["shipped_total"]["landings_window"] == 1
    assert "added" in snap["live_cargo"] and "removed" in snap["live_cargo"]


def test_live_cargo_counts_only_in_flight_flights_not_landed():
    # Now that a landed flight carries real PR cargo (issue #48), the corner counter's "IN FLIGHT"
    # figure MUST exclude it — else a merged flight's +N/−N would masquerade as cargo still being
    # loaded, a lie the moment cargo survived landing.
    in_flight = {"stage": flights.DOWNWIND, "cargo": {"present": True, "added": 18, "removed": 2}}
    landed = {"stage": flights.TOUCHDOWN, "cargo": {"present": True, "added": 999, "removed": 999}}
    assert server._live_cargo([in_flight, landed]) == {"present": True, "added": 18, "removed": 2}


def test_live_cargo_is_absent_when_only_landed_flights_carry_cargo():
    # A field of only-landed flights (all cargo now real) reports NOTHING in flight — honest calm.
    landed = {"stage": flights.TAXI_IN, "cargo": {"present": True, "added": 40, "removed": 5}}
    assert server._live_cargo([landed]) == {"present": False, "added": 0, "removed": 0}


def test_clock_and_per_repo_last_landing_text(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["clock"]                                   # HH:MM string
    assert "last landing" in snap["repos"][0]["last_landing_text"].lower()


def test_runner_message_present_when_down(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)   # committed fixture has no heartbeat
    assert snap["runner"]["down"] is True
    assert snap["runner"]["message"]


# =============================== multi-repo aggregation ===============================

def _clean_home(dst, iid_status, now, extra_journal=None, heartbeat_epoch=None):
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": iid_status}))
    if heartbeat_epoch is not None:
        (dst / "state" / "runner.heartbeat").write_text(str(heartbeat_epoch))
    lines = extra_journal or []
    (dst / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))


def _two_repo_cfg(a, b):
    return {"poll_seconds": 2, "heartbeat_down_seconds": 300, "repos": [
        {"slug": "will-titan/a", "name": "a", "state_home": str(a), "idle_seconds": 480,
         "freeze_seconds": 2700, "required_checks": ["tests"]},
        {"slug": "will-titan/b", "name": "b", "state_home": str(b), "idle_seconds": 480,
         "freeze_seconds": 2700, "required_checks": ["tests"]},
    ]}


# =============================== Task 10: monitoring armor — pill/banner across repos ===============================
# The whole surface must not be fooled by stale data: when ANY repo's runner is down, the global
# pill escalates to that worst state, names the offending repo, and the trouble banner lights — and
# because those are top-level snapshot fields (never nested under a repo tile), the banner shows no
# matter where the camera/scroll sits. These pin that aggregation contract at the snapshot layer.

def test_one_downed_repo_makes_the_global_pill_runner_down_and_names_it(tmp_path):
    a, b = tmp_path / "will-titan__a", tmp_path / "will-titan__b"
    _clean_home(a, {"i1": {"status": "merged", "branch": "sl/i1", "pr": 1}}, NOW, heartbeat_epoch=NOW - 5)
    _clean_home(b, {"i2": {"status": "running", "branch": "sl/i2", "pr": None}}, NOW,
                heartbeat_epoch=NOW - 4000)   # b's runner heartbeat is 4000s stale → RUNNER DOWN
    snap = server.assemble_snapshot(_two_repo_cfg(a, b), now=NOW)
    assert snap["runner"]["down"] is True
    assert snap["pill"]["level"] == "alert"            # runner-down is a factory-stop
    assert snap["pill"]["state"] == "runner-down"
    assert snap["pill"]["offender"] == "will-titan/b"  # the worst offender is NAMED
    assert snap["pill"]["message"] == "RUNNER DOWN"


def test_runner_down_outranks_a_healthy_repos_attention_state(tmp_path):
    # repo a is merely parked (attention); repo b's runner is down (a factory-stop). Worst-state
    # aggregation must surface b's runner-down, never a's calmer park.
    a, b = tmp_path / "will-titan__a", tmp_path / "will-titan__b"
    _clean_home(a, {"i1": {"status": "parked", "branch": "sl/i1", "pr": None}}, NOW, heartbeat_epoch=NOW - 5)
    (a / "state" / "blocked").mkdir()
    (a / "state" / "blocked" / "i1").write_text("stuck on a question")
    _clean_home(b, {"i2": {"status": "running", "branch": "sl/i2", "pr": None}}, NOW)  # no heartbeat
    snap = server.assemble_snapshot(_two_repo_cfg(a, b), now=NOW)
    assert snap["pill"]["state"] == "runner-down"
    assert snap["pill"]["offender"] == "will-titan/b"
    # per-repo runner facts are exposed so the front-end can grey exactly the downed field(s).
    downed = [r for r in snap["runner"]["repos"] if r["down"]]
    assert [r["slug"] for r in downed] == ["will-titan/b"]


def test_trouble_banner_is_a_top_level_field_so_it_is_camera_independent(tmp_path):
    # The banner's independence from camera/scroll is structural: it is a GLOBAL snapshot field, not
    # a property of any repo tile — so whichever repo the camera is on, the banner still renders.
    a, b = tmp_path / "will-titan__a", tmp_path / "will-titan__b"
    _clean_home(a, {"i1": {"status": "merged", "branch": "sl/i1", "pr": 1}}, NOW, heartbeat_epoch=NOW - 5)
    _clean_home(b, {"i2": {"status": "running", "branch": "sl/i2", "pr": None}}, NOW)  # b down
    snap = server.assemble_snapshot(_two_repo_cfg(a, b), now=NOW)
    assert "trouble" in snap                            # top-level, not under snap["repos"][*]
    assert snap["trouble"]["present"] is True
    assert snap["trouble"]["level"] == "alert"
    assert snap["trouble"]["offender"] == "will-titan/b"
    assert "will-titan/b" in snap["trouble"]["text"]


def test_all_runners_up_leaves_the_banner_dark(tmp_path):
    a, b = tmp_path / "will-titan__a", tmp_path / "will-titan__b"
    _clean_home(a, {"i1": {"status": "merged", "branch": "sl/i1", "pr": 1}}, NOW, heartbeat_epoch=NOW - 5)
    _clean_home(b, {"i2": {"status": "merged", "branch": "sl/i2", "pr": 2}}, NOW, heartbeat_epoch=NOW - 5)
    snap = server.assemble_snapshot(_two_repo_cfg(a, b), now=NOW)
    assert snap["runner"]["down"] is False
    assert snap["pill"]["level"] == "ok"
    assert snap["trouble"]["present"] is False


def test_journal_firehose_is_globally_time_ordered_across_repos(tmp_path):
    # Two repos: repo A's records are OLDER than repo B's. The tail must be the globally-newest
    # records in time order, never one repo's block appended after another's (medium finding).
    a = tmp_path / "will-titan__a"
    b = tmp_path / "will-titan__b"
    _clean_home(a, {"i1": {"status": "merged", "branch": "sl/i1", "pr": 1}}, NOW,
                [{"ts": NOW - 5000, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}],
                heartbeat_epoch=NOW - 5)
    _clean_home(b, {"i2": {"status": "merged", "branch": "sl/i2", "pr": 2}}, NOW,
                [{"ts": NOW - 10, "act": "merge", "id": "i2", "num": 2, "outcome": "ok"}],
                heartbeat_epoch=NOW - 5)
    cfg = {"poll_seconds": 2, "heartbeat_down_seconds": 300, "repos": [
        {"slug": "will-titan/a", "name": "a", "state_home": str(a), "idle_seconds": 480,
         "freeze_seconds": 2700, "required_checks": ["tests"]},
        {"slug": "will-titan/b", "name": "b", "state_home": str(b), "idle_seconds": 480,
         "freeze_seconds": 2700, "required_checks": ["tests"]},
    ]}
    snap = server.assemble_snapshot(cfg, now=NOW)
    ts = [r["ts"] for r in snap["journal_tail"] if r["ts"] is not None]
    assert ts == sorted(ts)
    assert len(snap["repos"]) == 2


# =============================== corrupt-but-parseable timestamps (fail closed, never crash) ===============================

@pytest.mark.parametrize("bad_ts", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timestamp_never_crashes_the_snapshot(tmp_path, bad_ts):
    # json.loads accepts NaN/Infinity/-Infinity, so a corrupt journal line can carry a non-finite ts.
    # NaN broke format_duration (ValueError); Infinity broke time.localtime in the tower window
    # (OverflowError). Assembly must degrade to "—"/"" for both, never fail-close the poll to a 500.
    dst = tmp_path / ("will-titan__" + bad_ts.strip("-"))
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "running", "branch": "sl/i1", "pr": None}}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    (dst / "journal.jsonl").write_text(
        '{"ts": %s, "act": "launch", "id": "i1", "num": 1}\n' % bad_ts)
    snap = server.assemble_snapshot(_config(dst), now=NOW)      # must not raise
    f = next(f for f in snap["flights"] if f["num"] == 1)
    assert f["display"]["elapsed"] == "—"
    # the tower window (which formats each ts via time.localtime) also renders without crashing
    rows = snap["repos"][0]["tower_log"]
    assert rows and rows[0]["hhmm"] == ""


# =============================== the field bindings (Task 7) ===============================
# The animated field binds ONLY these pre-derived values (design B.1) — the JS never decides
# which runway a lane owns, whether a landed plane still taxis, or which flight the trouble
# dimming must leave lit.

def test_every_flight_carries_the_field_bindings(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["flights"], "fixture must produce flights"
    for f in snap["flights"]:
        assert f["contrail"] in ("crisp", "thin", "sputter", "none")
        assert f["circuit_stage"] in flights.CIRCUIT_STAGES
        d = f["display"]
        assert d["runway"] in (0, 1)
        assert isinstance(d["on_field"], bool)
        assert isinstance(d["trouble"], bool)


def test_two_lanes_get_the_two_runways(home):
    # "2 runways = 2 concurrent builds" (§3): flights in distinct lanes never share a runway.
    p = home / "state" / "issues.json"
    body = json.loads(p.read_text())
    body["issues"]["i16"]["lane"] = "laneA"
    body["issues"]["i23"]["lane"] = "laneB"
    p.write_text(json.dumps(body))
    snap = server.assemble_snapshot(_config(home), now=NOW)
    by = {f["num"]: f["display"]["runway"] for f in snap["flights"]}
    assert {by[16], by[23]} == {0, 1}


def test_fresh_landing_taxis_and_old_landing_leaves_the_field(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    by = {f["num"]: f for f in snap["flights"]}
    assert by[23]["display"]["on_field"] is True    # merged 34 s ago — still taxiing in
    # i16 is status-merged with NO journal merge proof: recency unprovable → the arrivals board
    # carries it, the field does not (a plane may only linger on a PROVEN fresh landing).
    assert by[16]["display"]["on_field"] is False
    assert by[7]["display"]["on_field"] is True     # parked demands attention — never leaves


def test_trouble_lights_the_offending_flights_only(home):
    # §5 alarm salience: the field dims and THE PROBLEM is lit — parked flights here, nothing else.
    _calm(home)
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["repos"][0]["state"]["state"] == flights.PARKED
    by = {f["num"]: f["display"]["trouble"] for f in snap["flights"]}
    assert by[7] is True and by[21] is True
    assert by[15] is False and by[23] is False and by[16] is False


def test_no_flight_is_lit_when_the_worst_condition_is_not_a_flight(home):
    # Fixture raw: no heartbeat → runner-down is the worst condition. That is a whole-surface
    # treatment (screen 8d), so no individual plane gets the spotlight.
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["repos"][0]["state"]["state"] == "runner-down"
    assert all(f["display"]["trouble"] is False for f in snap["flights"])


def test_busy_or_troubled_field_has_no_quiet_caption(home):
    _calm(home)
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["repos"][0]["field_caption"] is None   # parked flights → not "all clear"


def test_quiet_field_carries_the_all_clear_caption(tmp_path):
    # §5: calm is never ambiguous — the empty field SAYS it is clear and when it last landed.
    dst = tmp_path / "will-titan__quiet"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 7200, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["repos"][0]["field_caption"] == "last landing 2h ago — all clear"


def test_quiet_field_with_no_landings_still_says_all_clear(tmp_path):
    dst = tmp_path / "will-titan__new"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["repos"][0]["field_caption"] == "no landings yet — all clear"


def test_airline_identity_and_living_clock_are_bound(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    repo = snap["repos"][0]
    assert repo["colors"]["tail"] == flights.airline_color(SLUG)
    assert snap["daypart"] == flights.daypart(NOW)
    assert snap["daypart"] in ("day", "dusk", "night")


def test_fun_map_defaults_all_on_and_master_gates_everything(home):
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["fun"]["airlines"] is True
    assert snap["fun"]["living_clock"] is True
    assert snap["fun"]["incident_sign"] is True
    dark = server.assemble_snapshot(_config(home, fun={"master": False}), now=NOW)
    assert all(v is False for v in dark["fun"].values())


def test_caption_suppressed_while_any_plane_is_on_the_field(tmp_path):
    # Review fix (cross-review, 2026-07-07): "all clear" may only caption an EMPTY field — a
    # holding orbit, a plane at the stand, or a fresh landing still taxiing all suppress it, even
    # though none of them is a pill condition. A caption over a visible plane is a false calm.
    dst = tmp_path / "will-titan__holding"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2},
        "i5": {"status": "holding", "branch": "sl/i5-y", "pr": 6}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 7200, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    assert snap["repos"][0]["state"]["state"] == "ok"      # holding is not a pill condition…
    assert snap["repos"][0]["field_caption"] is None       # …but the field is not empty either


def test_caption_suppressed_by_a_fresh_landing_still_taxiing(tmp_path):
    dst = tmp_path / "will-titan__freshland"
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - 60, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    snap = server.assemble_snapshot(_config(dst), now=NOW)
    f1 = snap["flights"][0]
    assert f1["display"]["on_field"] is True               # merged 60 s ago — still rolling out
    assert snap["repos"][0]["field_caption"] is None


def test_field_banner_is_the_longest_flying_downwind_flight(home):
    # Review fix (cross-review, 2026-07-07): the towed banner's flight is CHOSEN server-side
    # (squint test) — the longest-elapsed downwind flight tells the field's current story, with
    # its real elapsed time on the cloth. Rebuild the fixture's i16/i23 as two pure downwind
    # flights (running, fresh activity, no filed report) with different launch ages.
    p = home / "state" / "issues.json"
    body = json.loads(p.read_text())
    body["issues"]["i16"]["status"] = "running"
    body["issues"]["i23"]["status"] = "running"
    p.write_text(json.dumps(body))
    for iid in ("i16", "i23"):
        ap = home / "state" / "activity" / iid
        os.utime(ap, (NOW - 100, NOW - 100))
        rp = home / "reports" / ("%s.md" % iid)
        if rp.exists():
            rp.unlink()          # a filed report is base-turn; this case needs pure downwind
    (home / "journal.jsonl").open("a").write(
        json.dumps({"ts": NOW - 9000, "act": "launch", "id": "i16", "num": 16}) + "\n" +
        json.dumps({"ts": NOW - 300, "act": "launch", "id": "i23", "num": 23}) + "\n")
    snap = server.assemble_snapshot(_config(home), now=NOW)
    repo = snap["repos"][0]
    by = {f["num"]: f for f in repo["flights"]}
    assert by[16]["stage"] == flights.DOWNWIND and by[23]["stage"] == flights.DOWNWIND
    banner = repo["field_banner"]
    assert banner["num"] == 16                             # launched earliest — longest flying
    assert banner["text"].startswith("SL-16")
    assert "BUILDING" in banner["text"]


# =============================== Task 8 — the departures board is the real launch order ===============================
# The queue-order semantics are pinned pure in test_flights.py; here we prove the ASSEMBLER wires
# them: the agent-ready queue GitHub returns becomes an ordered board, expedite on top, and a
# blocked-by connection reads "awaiting connection SL-N" — resolved FAIL-CLOSED (a blocker is only
# "arrived" with positive proof it is CLOSED), so an open or unreadable blocker never flies.

class _QueueGh:
    """A gh stub whose agent-ready queue carries labels + bodies. ``closed_nums`` are the issues that
    have landed (their ``issue`` view reads state CLOSED); everything else reads OPEN, and an issue
    listed in ``unreadable`` fails closed to ``{}`` (a gh error)."""
    def __init__(self, ready, closed_nums=(), unreadable=()):
        self._ready = ready
        self._closed = set(closed_nums)
        self._unreadable = set(unreadable)

    def open_issues(self, repo, label=None, limit=200):
        if label == "agent-ready":
            return list(self._ready)
        return [{"number": r["number"], "title": r.get("title", "")} for r in self._ready]

    def issue(self, repo, num):
        if num in self._unreadable:
            return {}                                   # gh failure → fail closed to empty
        return {"number": num, "title": "issue %d" % num,
                "state": "CLOSED" if num in self._closed else "OPEN"}

    def pr_for_branch(self, repo, branch):
        return {}

    def pr_comments(self, repo, num):
        return []


def test_departures_are_ordered_expedite_then_priority_then_number(home):
    ready = [
        {"number": 40, "title": "low one", "labels": [{"name": "agent-ready"}, {"name": "priority:low"}]},
        {"number": 33, "title": "expedited", "labels": [{"name": "agent-ready"}, {"name": "expedite"}]},
        {"number": 41, "title": "high one", "labels": [{"name": "agent-ready"}, {"name": "priority:high"}]},
    ]
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_QueueGh(ready))
    deps = snap["repos"][0]["boards"]["departures"]
    assert [d["num"] for d in deps] == [33, 41, 40]      # ⚡ first, then high, then low
    assert deps[0]["expedited"] is True
    assert deps[0]["pos"] == 1


def test_departures_blocked_by_open_issue_reads_awaiting_connection(home):
    ready = [
        {"number": 26, "title": "needs the shell", "labels": [{"name": "agent-ready"}],
         "body": "## Loop metadata\nblocked-by: #5\n"},
        {"number": 27, "title": "free to fly", "labels": [{"name": "agent-ready"}], "body": ""},
    ]
    # #5 reads OPEN → the connection has not arrived; #26 must be awaiting, never launchable.
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_QueueGh(ready))
    deps = snap["repos"][0]["boards"]["departures"]
    by_num = {d["num"]: d for d in deps}
    assert by_num[26]["launchable"] is False
    assert by_num[26]["blocked_by"] == 5
    assert "AWAITING CONNECTION SL-5" in by_num[26]["status_text"].upper()
    assert deps.index(by_num[27]) < deps.index(by_num[26])   # the free flight ranks ahead


def test_departures_blocked_by_closed_issue_becomes_launchable(home):
    ready = [{"number": 26, "title": "needs the shell", "labels": [{"name": "agent-ready"}],
              "body": "blocked-by: #5"}]
    # #5 reads CLOSED → it landed → the connection arrived → #26 can fly.
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_QueueGh(ready, closed_nums={5}))
    d = snap["repos"][0]["boards"]["departures"][0]
    assert d["launchable"] is True
    assert d["blocked_by"] is None
    assert d["pos"] == 1


def test_departures_blocked_by_unreadable_connection_fails_closed(home):
    # gh can't read the blocker (a timeout/failure → {}). The flight must STAY awaiting, never fly on
    # a hopeful guess (Codex cross-review, Task 8): a blocked flight is never in the air.
    ready = [{"number": 26, "title": "needs the shell", "labels": [{"name": "agent-ready"}],
              "body": "blocked-by: #5"}]
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_QueueGh(ready, unreadable={5}))
    d = snap["repos"][0]["boards"]["departures"][0]
    assert d["launchable"] is False
    assert d["blocked_by"] == 5


def test_departures_blocked_by_raising_adapter_fails_closed(home):
    # An injected/cached gh whose issue() RAISES (not just returns {}) must still leave the flight
    # awaiting — the connection resolver catches it and never flies a blocked flight (Codex round 2).
    ready = [{"number": 26, "title": "needs the shell", "labels": [{"name": "agent-ready"}],
              "body": "blocked-by: #5"}]

    class _RaisingGh(_QueueGh):
        def issue(self, repo, num):
            raise RuntimeError("gh exploded")

    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_RaisingGh(ready))
    d = snap["repos"][0]["boards"]["departures"][0]
    assert d["launchable"] is False
    assert d["blocked_by"] == 5


def test_departures_exclude_flights_already_in_the_air(home):
    # An agent-ready issue that is ALSO a live flight (in issues.json) is departing, not queued —
    # it must not double-appear. i15 is holding (flying) in the fixture.
    ready = [{"number": 15, "title": "already flying", "labels": [{"name": "agent-ready"}]},
             {"number": 99, "title": "waiting", "labels": [{"name": "agent-ready"}]}]
    snap = server.assemble_snapshot(_config(home), now=NOW, gh_mod=_QueueGh(ready))
    nums = {d["num"] for d in snap["repos"][0]["boards"]["departures"]}
    assert 99 in nums
    assert 15 not in nums


def test_snapshot_fun_map_carries_the_solari_toggles(home):
    # The Solari flutter + its clack are gated by the fun map the client binds (Task 8 / §7 / B.10).
    snap = server.assemble_snapshot(_config(home), now=NOW)
    assert snap["fun"]["solari"] is True
    assert snap["fun"]["solari_clack"] is True
    dark = server.assemble_snapshot(_config(home, fun={"master": False}), now=NOW)
    assert dark["fun"]["solari"] is False and dark["fun"]["solari_clack"] is False


# =============================== the stand — queued flights as planes at the gates (issue #32) ===============================
# The departures queue (open ``agent-ready`` issues not yet flying) is the design's "at the stand
# (approved, queued)" circuit stage (§3). A queued issue has no state-dir entry yet, so it never
# becomes a ``flights`` object — the field would draw nothing for it. ``repo["stand"]`` is the
# server-side projection the field renders as planes standing at the west gates: the LAUNCHABLE queue
# rows (a blocked "awaiting connection" row is never in the air, §3), in launch order, capped to the
# physical gate count. These planes are healthy and waiting — never the parked "gave up" state.

def _landed_home(tmp_path, name, merge_age=7200):
    """A calm, ok-state home whose only flight landed long enough ago to have left the field — so the
    field is empty of real planes and any plane on it comes from the stand."""
    dst = tmp_path / ("will-titan__%s" % name)
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps(
        {"version": 1, "issues": {"i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2}}}))
    (dst / "journal.jsonl").write_text(json.dumps(
        {"ts": NOW - merge_age, "act": "merge", "id": "i1", "num": 1, "outcome": "ok"}) + "\n")
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    return dst


def test_queued_agent_ready_issues_stand_as_planes_at_the_gates(tmp_path):
    ready = [{"number": 51, "title": "second in line", "labels": [{"name": "agent-ready"}]},
             {"number": 50, "title": "first in line", "labels": [{"name": "agent-ready"}]}]
    dst = _landed_home(tmp_path, "stand")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))
    stand = snap["repos"][0]["stand"]
    # every launchable queued flight is a plane at a gate, in launch order (pos ascending → 50, 51)
    assert [s["num"] for s in stand] == [50, 51]
    first = stand[0]
    assert first["flight"] == "SL-50"
    assert first["pos"] == 1
    assert "first in line" in first["destination"]


def test_a_stand_flight_leaves_the_gate_once_it_launches(tmp_path):
    # §3 "queue empties/fills live as launches happen": a queued flight standing at the gate becomes a
    # real in-air flight the moment the runner picks it up (it enters issues.json), and its gate frees.
    ready = [{"number": 50, "title": "next off the stand", "labels": [{"name": "agent-ready"}]},
             {"number": 51, "title": "behind it", "labels": [{"name": "agent-ready"}]}]
    dst = _landed_home(tmp_path, "launch")
    issues = dst / "state" / "issues.json"

    before = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))
    assert {s["num"] for s in before["repos"][0]["stand"]} == {50, 51}
    assert {f["num"] for f in before["flights"]} == {1}      # only the landed flight exists yet

    # The runner launches SL-50: it gains a state entry + a fresh session. The next poll re-reads.
    issues.write_text(json.dumps({"version": 1, "issues": {
        "i1": {"status": "merged", "branch": "sl/i1-x", "pr": 2},
        "i50": {"status": "running", "branch": "sl/i50-x", "lane": "i50", "launches": 1,
                "retries": 0, "conflicts": 0, "requeue_front": False, "declared_touches": [],
                "pr": None}}}))
    (dst / "state" / "activity").mkdir(parents=True, exist_ok=True)
    (dst / "state" / "activity" / "i50").write_text("")
    os.utime(dst / "state" / "activity" / "i50", (NOW - 60, NOW - 60))

    after = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))
    repo = after["repos"][0]
    # SL-50 is now a real flight in the air, no longer a plane at the stand nor a departures row.
    assert 50 in {f["num"] for f in after["flights"]}
    assert after["flights"] and next(f for f in after["flights"] if f["num"] == 50)["stage"] != flights.AT_STAND
    assert 50 not in {s["num"] for s in repo["stand"]}
    assert 50 not in {d["num"] for d in repo["boards"]["departures"]}
    # …and SL-51 is still waiting its turn at the gate.
    assert {s["num"] for s in repo["stand"]} == {51}


def test_queued_stand_flights_suppress_the_all_clear_caption(tmp_path):
    # A plane at the stand is a visible plane — the "all clear" caption over it would be a false calm
    # (the review-fix invariant §5, whose docstring already names "a plane at the stand"). Now that
    # queued flights actually reach the field, the caption must honour it.
    ready = [{"number": 50, "title": "waiting", "labels": [{"name": "agent-ready"}]}]
    dst = _landed_home(tmp_path, "standcaption")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))
    repo = snap["repos"][0]
    assert repo["state"]["state"] == "ok"      # not a pill condition — a healthy waiting plane…
    assert repo["stand"], "the queued flight must be a plane at the stand"
    assert repo["field_caption"] is None       # …but the field is not empty, so no false calm


def test_a_blocked_queue_row_never_stands_at_the_gates(tmp_path):
    # §3: a flight whose blocked-by connection has NOT arrived reads "awaiting connection SL-N" on the
    # departures board and is NEVER in the air — so it is not a plane at the stand either.
    ready = [{"number": 26, "title": "needs the shell", "labels": [{"name": "agent-ready"}],
              "body": "blocked-by: #5"},
             {"number": 27, "title": "free to fly", "labels": [{"name": "agent-ready"}], "body": ""}]
    dst = _landed_home(tmp_path, "standblocked")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))   # #5 reads OPEN
    stand_nums = {s["num"] for s in snap["repos"][0]["stand"]}
    assert 27 in stand_nums          # launchable → a plane at the gate
    assert 26 not in stand_nums      # awaiting connection → board only, never on the field


def test_the_stand_shows_at_most_one_plane_per_gate(tmp_path):
    # The field has a finite row of gates; the full queue lives (paginated) on the departures board.
    # More launchable flights than gates → only the front of the queue stands, in launch order — never
    # a silent pile of overlapping planes on the last bay.
    ready = [{"number": n, "title": "q%d" % n, "labels": [{"name": "agent-ready"}]}
             for n in (60, 61, 62, 63, 64, 65)]
    dst = _landed_home(tmp_path, "standcap")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_QueueGh(ready))
    stand = snap["repos"][0]["stand"]
    assert len(stand) == server.STAND_BAYS
    assert [s["num"] for s in stand] == [60, 61, 62, 63][:server.STAND_BAYS]


# =============================== GitHub-unreachable: an honest state, never a false all-clear (#38) ===============================
# When gh is missing / unauthenticated every GitHub read fails closed to empty, and a quiet field
# would render a cheerful "all clear" indistinguishable from genuinely having no work. The snapshot
# must tell "gh answered: nothing there" from "gh unavailable/refused" — derived from the open-issue
# read it ALREADY makes (open_issues_probe), never an extra gh call (issue #38 boundary).

def _quiet_home(tmp_path, name):
    """An empty, healthy state home (no flights, fresh heartbeat) — the field where the caption logic
    engages, so a false vs honest all-clear is unambiguous."""
    dst = tmp_path / ("will-titan__" + name)
    (dst / "state").mkdir(parents=True)
    (dst / "state" / "issues.json").write_text(json.dumps({"version": 1, "issues": {}}))
    (dst / "state" / "runner.heartbeat").write_text(str(NOW - 5))
    return dst


class _ProbeGh:
    """A gh stub that reports reachability through ``open_issues_probe`` (the real adapter's #38
    surface). ``reachable=False`` models a missing / unauthenticated / timed-out gh: every read fails
    closed to empty, exactly like the real adapter. Every call is counted so a test can prove the
    reachability signal rides a call the snapshot already makes — no extra gh call."""
    def __init__(self, reachable):
        self._reachable = reachable
        self.probe_calls = 0
        self.open_issues_unlabeled_calls = 0
        self.open_issues_labeled_calls = 0

    def open_issues_probe(self, repo, label=None, limit=200):
        self.probe_calls += 1
        return [], self._reachable

    def open_issues(self, repo, label=None, limit=200):
        if label == "agent-ready":
            self.open_issues_labeled_calls += 1
        else:
            self.open_issues_unlabeled_calls += 1
        return []

    def issue(self, repo, num):
        return {}

    def pr_for_branch(self, repo, branch):
        return {}

    def pr_comments(self, repo, num):
        return []


def test_snapshot_marks_github_unreachable_when_gh_refuses(tmp_path):
    dst = _quiet_home(tmp_path, "unreachable")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_ProbeGh(reachable=False))
    repo = snap["repos"][0]
    assert repo["github"]["unreachable"] is True
    assert repo["github"]["reachable"] is False
    assert snap["github"]["unreachable"] is True       # top-level aggregate for a global surface


def test_unreachable_github_suppresses_the_false_all_clear_caption(tmp_path):
    # The bug (issue #38): a quiet field + a dead gh must NOT read "all clear" — that false calm is
    # indistinguishable from genuinely having no work. The dark-tower state carries the truth instead.
    dst = _quiet_home(tmp_path, "unreachable2")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_ProbeGh(reachable=False))
    assert snap["repos"][0]["field_caption"] is None


def test_reachable_empty_github_still_says_all_clear(tmp_path):
    # DoD #3: when gh ANSWERS empty, the genuine all-clear is UNCHANGED — reachable=True is an honest
    # "no work", not an outage. This is the calm the unreachable state must never be confused with.
    dst = _quiet_home(tmp_path, "empty")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_ProbeGh(reachable=True))
    repo = snap["repos"][0]
    assert repo["github"]["unreachable"] is False
    assert repo["github"]["reachable"] is True
    assert repo["field_caption"] == "no landings yet — all clear"
    assert snap["github"]["unreachable"] is False


def test_github_reachability_is_unknown_without_a_wired_adapter(tmp_path):
    # gh_mod=None is an embedder that never wired GitHub — reachability UNKNOWN, never "unreachable".
    # The genuine all-clear (the pre-existing None-gh behavior) must be preserved, not alarmed.
    dst = _quiet_home(tmp_path, "nogh")
    snap = server.assemble_snapshot(_config(dst), now=NOW)     # no gh_mod
    repo = snap["repos"][0]
    assert repo["github"]["unreachable"] is False
    assert repo["github"]["reachable"] is None
    assert repo["field_caption"] == "no landings yet — all clear"


def test_unreachable_state_self_heals_when_github_returns(tmp_path):
    # DoD #2: self-healing on recovery. The same field flips back the instant gh answers again — the
    # state is derived FRESH each poll, nothing is latched.
    dst = _quiet_home(tmp_path, "heal")
    down = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_ProbeGh(reachable=False))
    assert down["repos"][0]["github"]["unreachable"] is True
    assert down["repos"][0]["field_caption"] is None
    up = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_ProbeGh(reachable=True))
    assert up["repos"][0]["github"]["unreachable"] is False
    assert up["repos"][0]["field_caption"] == "no landings yet — all clear"


class _StaleQueueGh:
    """Models the cache-offset window (Codex review): the fresh reachability probe has FAILED
    (unreachable), yet a still-cached agent-ready queue read hands back stale rows. The snapshot must
    NOT fly those stale planes — an unreachable field shows no GitHub-derived queue at all, by
    construction, never relying on the labeled read also happening to fail."""
    def open_issues_probe(self, repo, label=None, limit=200):
        return [], False                      # the reachability oracle: gh is down
    def open_issues(self, repo, label=None, limit=200):
        if label == "agent-ready":
            return [{"number": 71, "title": "stale", "labels": [{"name": "agent-ready"}]}]
        return []
    def issue(self, repo, num):
        return {}
    def pr_for_branch(self, repo, branch):
        return {}
    def pr_comments(self, repo, num):
        return []


def test_unreachable_forces_the_github_queue_dark_no_stale_departures_or_stand(tmp_path):
    # Blocking fix (Codex review): github.unreachable must actually force the GitHub-derived queue
    # dark. A stale-cached agent-ready list can outlive the fresh probe's failure by a cache window;
    # the snapshot must not render "NO DATA LINK" AND a populated departures board / planes at the
    # gate. When unreachable, departures and stand are empty by construction.
    dst = _quiet_home(tmp_path, "stalequeue")
    snap = server.assemble_snapshot(_config(dst), now=NOW, gh_mod=_StaleQueueGh())
    repo = snap["repos"][0]
    assert repo["github"]["unreachable"] is True
    assert repo["boards"]["departures"] == []     # the GitHub-derived queue is dark, never stale rows
    assert repo["stand"] == []                     # …and no plane stands at a gate on an unread queue


def test_reachability_rides_the_existing_open_issue_read_no_extra_gh_call(tmp_path):
    # Boundary (issue #38): derive the state from calls already made — no new gh call. The snapshot
    # learns reachability through open_issues_probe (the unlabeled open-issue read it already makes),
    # never a SECOND unlabeled open_issues call added alongside it.
    dst = _quiet_home(tmp_path, "count")
    gh = _ProbeGh(reachable=True)
    server.assemble_snapshot(_config(dst), now=NOW, gh_mod=gh)
    assert gh.probe_calls == 1                    # one unlabeled probe carries reachability + titles
    assert gh.open_issues_unlabeled_calls == 0    # …and it REPLACED the old unlabeled open_issues read
