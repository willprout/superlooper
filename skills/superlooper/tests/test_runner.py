"""runner.py — the deterministic ~15s tick shell. Everything decision-shaped lives in
actions.decide (tested in test_actions.py); these tests pin what the SHELL must guarantee:

  * the env contract launch-session.sh depends on (SL_RUN_ROOT/SL_REPO/SL_PANE/SL_DEV_BRANCH/
    SL_MODEL/SL_EFFORT/SL_AGENT) on every launch, worker AND answerer, plus the per-issue model/effort
    override (label > config > default; durable across a cold restart; answerer unaffected);
  * executor mechanics: ordered gh mutations, loopstate stamps, marker hygiene, fail-and-retry
    (a failed gh write never advances local state past the truth);
  * the pidfile singleton, the per-tick heartbeat, SIGTERM as a clean fail-stop;
  * event persistence (events/ -> processed/, seq-numbered) and the restart rebuild that never
    re-fires a latched token event;
  * a tick that survives EVERY helper failing (gh down, scripts missing) — fail closed, journal,
    never raise.

All shell effects run through injected stubs: a recording run_script, fake-gh via SL_GH, and
monkeypatched gitops — no real tabs, no real GitHub, no real claude (kickoff rule).
"""
import json
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest

import issues
import journal
import loopstate
import runner as runner_mod

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

NOW = 1_750_000_000


def make_config(**over):
    c = {
        "repo": "o/r", "dev_branch": "main", "prod_branch": None,
        "lanes": 2, "affinity": "hard", "areas": {},
        "touches_required": False, "required_checks": ["ci"], "merge_method": "squash",
        "ship_cmd": None, "ship_recheck_cmd": None,
        "report_required_sections": ["Tests"], "bright_lines": [],
        "cleanup_merged_worktrees": True, "report_time": "08:45",
        "models": {"worker": "opus", "answerer": "fable"},
        "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2},
        "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
               "quarantine": [], "nightly_time": "02:00"},
        "notify": {"imessage_to": None, "cmd": None},
        "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
    }
    c.update(over)
    return c


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A Runner over tmp dirs, fake-gh, a recording run_script, and healthy stub usage."""
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    monkeypatch.setenv("SL_GH", str(_FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))
    monkeypatch.delenv("GH_FAIL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"

    calls = []
    rc_queue = []

    def run_script(args, env=None, timeout=None):
        calls.append({"args": [str(x) for x in args], "env": dict(env or {})})
        return rc_queue.pop(0) if rc_queue else 0

    r = runner_mod.Runner(
        repo=str(repo), config=make_config(), state_home=str(home), pane="pane-1",
        run_script=run_script,
        fetch_usage=lambda: {"auth_status": "ok", "five_hour_pct": 10.0, "seven_day_pct": 20.0})
    # Healthy launch anchor by default (mirrors the healthy stub usage/run_script above): the real
    # probe would shell out to cmux, which the suite neutralizes — anchor tests override this.
    r._anchor_status = lambda: {"ok": True, "reason": ""}
    return type("Rig", (), {"r": r, "calls": calls, "rc_queue": rc_queue,
                            "home": home, "repo": repo, "fixdir": fixdir})


def mutations(rig):
    p = rig.fixdir / "mutations.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def issue_state(rig, iid):
    return loopstate.load(str(rig.home / "state" / "issues.json"))["issues"].get(iid)


def _all_issue_states(rig):
    return loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]


def seed_issue(rig, iid, **fields):
    def m(st):
        st["issues"].setdefault(iid, loopstate.new_issue()).update(fields)
    loopstate.update(str(rig.home / "state" / "issues.json"), m)


# --------------------------- layout / singleton / heartbeat ---------------------------

def test_init_creates_the_c3_layout(rig):
    for sub in ("state/activity", "state/blocked", "state/exited", "state/awaiting",
                "state/panes", "state/started", "state/launch_stderr", "state/events/processed",
                "briefs", "reports", "answers", "worktrees", "logs"):
        assert (rig.home / sub).is_dir(), f"missing {sub}"
    assert (rig.home / "state" / "issues.json").is_file()


def test_pidfile_singleton(rig, tmp_path):
    assert rig.r.acquire_singleton() is True
    other = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                              state_home=str(rig.home), pane="p",
                              run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    assert other.acquire_singleton() is False          # live holder wins
    rig.r.release_singleton()
    (rig.home / "state" / "runner.lock").write_text("999999")   # dead pid -> stale
    assert other.acquire_singleton() is True


def test_heartbeat_written_every_tick(rig):
    rig.r.tick(now=NOW)
    hb = (rig.home / "state" / "runner.heartbeat").read_text().strip()
    assert hb == str(int(NOW))
    rig.r.tick(now=NOW + 15)
    assert (rig.home / "state" / "runner.heartbeat").read_text().strip() == str(int(NOW + 15))


def test_sigterm_is_a_clean_fail_stop(rig):
    rig.r.acquire_singleton()
    rig.r._handle_signal(signal.SIGTERM, None)
    rig.r.run(max_ticks=5, sleep=lambda s: None)       # returns immediately, no ticks forced
    assert not (rig.home / "state" / "runner.lock").exists()


def _anchor_file(rig):
    return rig.home / "state" / "runner.anchor.json"


def test_run_records_the_live_anchor_and_clears_it_on_exit(rig):
    # issue #33: a LIVE runner records its anchor (the pane worker tabs are born in, plus the
    # workspace/window it lives in, plus its pid) so `doctor` can later verify it still resolves.
    # It is present WHILE running and gone after a clean exit — exactly like the pidfile.
    rig.r.workspace, rig.r.window = "WS-1", "WIN-1"
    seen = {}

    def spy_tick(now=None):
        f = _anchor_file(rig)
        seen["existed"] = f.exists()
        seen["data"] = json.loads(f.read_text()) if f.exists() else None
        rig.r.stop = True                              # one tick, then clean stop
    rig.r.tick = spy_tick
    rig.r.run(sleep=lambda s: None)

    assert seen["existed"] is True
    assert seen["data"]["pane"] == "pane-1"
    assert seen["data"]["workspace"] == "WS-1" and seen["data"]["window"] == "WIN-1"
    assert seen["data"]["pid"] == os.getpid()
    assert not _anchor_file(rig).exists()              # cleared on clean exit, like runner.lock


def test_a_runner_that_loses_the_singleton_leaves_the_live_anchor_untouched(rig):
    # The loser's early exit (another runner is live) must never clobber or clear the holder's
    # recorded anchor — only the owning instance writes/clears it.
    rig.r.workspace, rig.r.window = "WS-live", "WIN-live"
    assert rig.r.acquire_singleton() is True
    rig.r._write_anchor()
    before = _anchor_file(rig).read_text()

    other = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                              state_home=str(rig.home), pane="other-pane",
                              run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    assert other.run(sleep=lambda s: None) == 1        # loses the singleton, exits
    assert _anchor_file(rig).read_text() == before      # holder's anchor intact


# --------------------------- the Restart button: self re-exec (issue #116) ---------------------------

def _restart_marker(rig):
    return rig.home / "state" / "runner.restart"


def _journal(rig):
    p = rig.home / "journal.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_restart_marker_between_ticks_reexecs_in_place(rig):
    # The command-center Restart button drops a marker in the STATE HOME (never .superlooper/**); the
    # runner honors it at the safe point BETWEEN ticks (never mid-executor) by re-exec'ing itself in
    # place — the SAME invocation, so a fresh process image reloads the installed engine in the same
    # cmux tab and starts with cleared in-memory state.
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(list(argv))
    rig.r.tick = lambda now=None: None                                # isolate honoring from tick internals
    _restart_marker(rig).write_text(json.dumps({"operator": "William", "source": "command-center"}))
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert recorded == [[sys.executable] + list(sys.argv)]             # re-runs THIS exact invocation
    assert not _restart_marker(rig).exists()                          # consumed BEFORE the exec (no re-loop)
    assert "runner_restart" in [j.get("act") for j in _journal(rig)]  # the verb is journaled


def test_no_restart_marker_means_no_reexec(rig):
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    rig.r.run(max_ticks=2, sleep=lambda s: None)
    assert recorded == []


def test_restart_is_honored_even_when_the_systemic_launch_hold_is_tripped(rig):
    # Tonight's shape (issue #116): a wedged anchor tripped the in-memory systemic-launch hold, and
    # the owner wants the button to clear it. The hold must never SUPPRESS the restart — the re-exec
    # is what wipes the streak (see test_a_restart_clears_the_systemic_launch_streak: a fresh Runner
    # starts with an empty set).
    rig.r._launch_fail_ids = {"i1", "i2", "i3", "i4"}                 # mid-episode: a wedged anchor
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    _restart_marker(rig).write_text("{}")
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert len(recorded) == 1


def test_a_present_but_corrupt_restart_marker_is_still_honored(rig):
    # Existence is the signal (like state/ALERT): a present-but-unparseable marker still restarts —
    # the button's intent is not lost to a malformed body.
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    _restart_marker(rig).write_text("not json {{{")
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert len(recorded) == 1


def test_reexec_adopts_the_singleton_without_a_double_start_window(rig):
    # os.execv PRESERVES the pid, so after a self-restart the lock still holds our OWN live pid — the
    # normal singleton check would read that as "another live runner" and refuse. A one-shot env
    # token (set by the pre-exec image, matching our post-exec pid) proves the lock is ours-by-
    # re-exec, so the reborn image adopts it in place. The lock is NEVER released across the exec, so
    # a concurrent `run` WITHOUT the token still sees our live pid and refuses — no double-start.
    assert rig.r.acquire_singleton() is True
    (rig.home / "state" / "runner.lock").write_text(str(os.getpid()))   # our pid = the live holder
    other = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                              state_home=str(rig.home), pane="p")
    assert other.acquire_singleton() is False                           # no token → refuses (no window)
    reborn = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                               state_home=str(rig.home), pane="pane-1")
    os.environ["SL_RESTART_ADOPT"] = str(os.getpid())
    try:
        assert reborn.acquire_singleton() is True                       # token → adopts in place
        assert reborn._reexec_adopted is True
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert os.environ.get("SL_RESTART_ADOPT") is None                   # token consumed exactly once


def test_reexec_adoption_never_rewrites_the_pidfile(rig):
    # Critical (fresh-agent review): the lock ALREADY holds our pid (execv preserved it), so adoption
    # must take it WITHOUT reopening/truncating the pidfile — else a concurrent `run` could read an
    # empty file mid-rewrite and double-start (the very window this feature must not open). Proof: a
    # READ-ONLY pidfile is still adopted cleanly, because adoption never opens it for write.
    lock = rig.home / "state" / "runner.lock"
    lock.write_text(str(os.getpid()))
    lock.chmod(0o444)
    reborn = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                               state_home=str(rig.home), pane="pane-1")
    os.environ["SL_RESTART_ADOPT"] = str(os.getpid())
    try:
        assert reborn.acquire_singleton() is True          # adopted without any write
        assert reborn._reexec_adopted is True
        assert lock.read_text() == str(os.getpid())        # pidfile intact — never emptied
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
        lock.chmod(0o644)


def test_a_marker_targeting_us_is_honored(rig):
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    _restart_marker(rig).write_text(json.dumps({"target_pid": os.getpid()}))
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert len(recorded) == 1


def test_a_marker_targeting_a_different_runner_is_cleared_not_honored(rig):
    # A marker written for a runner that died before honoring it (the operator then started a FRESH
    # runner — a new pid): it was never a request for US, so clear it and never spuriously restart.
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    _restart_marker(rig).write_text(json.dumps({"target_pid": os.getpid() + 1}))
    rig.r.run(max_ticks=1, sleep=lambda s: None)
    assert recorded == []                                  # not honored
    assert not _restart_marker(rig).exists()               # the stale marker is cleared
    assert any(j.get("act") == "runner_restart" and j.get("phase") == "stale" for j in _journal(rig))


def test_an_undecodable_restart_marker_is_still_honored(rig):
    # Existence is the signal (like state/ALERT): even a marker of undecodable bytes still restarts —
    # a corrupt body never loses the request. (No target_pid parses out ⇒ honored by any runner.)
    recorded = []
    rig.r._reexec = lambda argv: recorded.append(argv)
    rig.r.tick = lambda now=None: None
    _restart_marker(rig).write_bytes(b"\xff\xfe not utf-8 \x00")
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    assert len(recorded) == 1


def test_a_reexec_adopted_start_journals_the_completed_restart(rig):
    # After the exec the reborn image records the "up" half of the restart trail (old pid → new),
    # so the journal (and morning report) show the restart landed, not just that it was requested.
    (rig.home / "state" / "runner.lock").write_text(str(os.getpid()))
    rig.r.tick = lambda now=None: None
    os.environ["SL_RESTART_ADOPT"] = str(os.getpid())
    try:
        rig.r.run(max_ticks=1, sleep=lambda s: None)
    finally:
        os.environ.pop("SL_RESTART_ADOPT", None)
    ups = [j for j in _journal(rig)
           if j.get("act") == "runner_restart" and j.get("phase") == "up"]
    assert ups and ups[0].get("new_pid") == os.getpid()


# --------------------------- Task-11 seams: morning report + notify ---------------------------

def test_morning_report_seam_writes_the_file_and_pushes(rig, tmp_path):
    marker = tmp_path / "notified.txt"
    rig.r.config["notify"]["cmd"] = f'printf "%s" {{title}} > {marker}'   # bare {title}: adapter quotes it
    journal.append(str(rig.home), {"act": "merge", "id": "i5", "num": 5, "pr": 9, "outcome": "ok"},
                   now=NOW)
    rig.r._exec_morning_report({"act": "morning_report", "date": "2026-07-02"}, NOW)

    report_file = rig.home / "reports" / "morning-2026-07-02.md"
    assert report_file.exists(), "the seam must render the report file"
    text = report_file.read_text()
    assert "superlooper morning report" in text and "#5" in text
    assert (rig.home / "state" / "last_morning_report").read_text() == "2026-07-02"  # due stamp
    assert marker.exists() and "morning report" in marker.read_text()               # push fired


def test_exec_notify_delivers_and_returns_the_channel_outcome(rig, tmp_path):
    marker = tmp_path / "note.txt"
    # bare {title}/{body} — notify shell-quotes them, so a spaced/backtick memo can't break the cmd
    rig.r.config["notify"]["cmd"] = f'printf "%s|%s" {{title}} {{body}} > {marker}'
    out = rig.r._exec_notify({"act": "notify", "title": "superlooper: i7 parked",
                              "body": "retry cap hit"}, NOW)
    assert out.startswith("sent via cmd")            # the journaled outcome names the channel
    assert marker.read_text() == "superlooper: i7 parked|retry cap hit"


def test_exec_notify_never_raises_when_no_channel(rig, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(rig.home / "no-such-cmux"))
    out = rig.r._exec_notify({"act": "notify", "title": "t", "body": "b"}, NOW)
    assert out == "log-only"


# --------------------------- freeze ownership (Codex R2 C2) ---------------------------

def test_freeze_tags_the_marker_as_a_dev_check_freeze(rig):
    rig.r._exec_freeze({"act": "freeze", "reason": "dev red: ci", "fingerprint": "abc"}, NOW)
    m = loopstate.load(str(rig.home / "state" / "merges_frozen.json"))
    assert m["source"] == "dev-check"


def test_unfreeze_holds_a_nightly_owned_freeze(rig):
    # the runner unfreezes on dev-CHECK green; it must NOT clear a nightly/browser-suite freeze,
    # or merges would flow while the nightly is still red (only a green nightly clears that one).
    fp = rig.home / "state" / "merges_frozen.json"
    loopstate.save(str(fp), {"reason": "nightly red", "source": "nightly", "since": NOW})
    out = rig.r._exec_unfreeze({"act": "unfreeze"}, NOW)
    assert fp.exists() and "held" in out.lower()        # freeze REMAINS


def test_unfreeze_clears_a_dev_check_freeze(rig):
    fp = rig.home / "state" / "merges_frozen.json"
    loopstate.save(str(fp), {"reason": "dev red", "source": "dev-check", "since": NOW})
    assert rig.r._exec_unfreeze({"act": "unfreeze"}, NOW) == "ok"
    assert not fp.exists()                              # dev-check freeze cleared, as before


# --------------------------- polling / resilience ---------------------------

def test_poll_builds_a_fresh_gh_view(rig):
    seed_issue(rig, "i123", status="gating", branch="sl/i123-render-the-widget", type="build")
    (rig.home / "reports" / "i123.md").write_text("## Tests\n" + "x" * 60)
    rig.r.tick(now=NOW)
    gv = rig.r.gh_view
    assert gv["stale"] is False and gv["consecutive_failures"] == 0
    assert gv["closed_nums"] == {41, 52}
    assert "i123" in gv["prs"] and gv["prs"]["i123"]["number"] == 555
    assert any(c["body"].startswith("<!-- superlooper-review -->")
               for c in gv["prs"]["i123"]["comments"])
    assert isinstance(gv["dev_checks"], list) and gv["dev_checks"]


def test_gh_outage_marks_the_view_stale_and_counts_failures(rig, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    rig.r.tick(now=NOW)
    assert rig.r.gh_view["stale"] is True
    assert rig.r.gh_view["consecutive_failures"] == 1
    rig.r.tick(now=NOW + 100)                          # next poll window
    assert rig.r.gh_view["consecutive_failures"] == 2


def test_tick_survives_an_unhashable_status_in_issues_json(rig):
    # Issue #95: a corrupt state/issues.json carrying a wrong-typed UNHASHABLE status ([]/{}) must
    # not wedge the tick. Before the fix, detect_events (and its sibling status-membership tests all
    # along the tick path — the poll, the finishing-PR/investigation refreshes, lane_state_from,
    # decide) raised `unhashable type` BEFORE the heartbeat stamp, so the dead-man's switch read a
    # LIVE runner as dead. A whole healthy loop must not be taken down by one poisoned entry: the
    # tick completes, the poll stays fresh (never perpetually stale on the corrupt entry), and the
    # heartbeat is stamped.
    seed_issue(rig, "i5", status=[])
    seed_issue(rig, "i7", status={})
    seed_issue(rig, "i123", status="gating", branch="sl/i123-render-the-widget", type="build")
    (rig.home / "reports" / "i123.md").write_text("## Tests\n" + "x" * 60)
    rig.r.tick(now=NOW)                                # must not raise
    assert (rig.home / "state" / "runner.heartbeat").read_text().strip() == str(int(NOW))
    assert rig.r.gh_view["stale"] is False            # the poll survived the corrupt entry, not wedged-stale
    assert "i123" in rig.r.gh_view["prs"]             # the healthy finishing issue was still refreshed


def test_tick_survives_every_helper_failing(rig, monkeypatch):
    monkeypatch.setenv("GH_FAIL", "1")
    rig.r._run_script = lambda *a, **k: 127            # no scripts, no cmux
    rig.r._fetch_usage = lambda: (_ for _ in ()).throw(RuntimeError("keychain gone"))
    seed_issue(rig, "i5", status="running")
    (rig.home / "state" / "blocked" / "i5").write_text("q?")
    rig.r.tick(now=NOW)                                # must not raise
    rig.r.tick(now=NOW + 15)
    assert (rig.home / "state" / "runner.heartbeat").exists()


def test_usage_cache_only_refetches_after_60s(rig):
    fetches = []
    rig.r._fetch_usage = lambda: fetches.append(1) or {
        "auth_status": "ok", "five_hour_pct": 1.0, "seven_day_pct": 1.0}
    rig.r.tick(now=NOW)
    rig.r.tick(now=NOW + 15)
    rig.r.tick(now=NOW + 61)
    assert len(fetches) == 2


# --------------------------- events: persistence + restart rebuild ---------------------------

def test_token_events_persist_and_never_refire_across_restart(rig):
    seed_issue(rig, "i5", status="running", branch="sl/i5-x")
    (rig.home / "reports" / "i5.md").write_text("## Tests\n" + "x" * 60)
    rig.r.tick(now=NOW)
    processed = list((rig.home / "state" / "events" / "processed").glob("*.json"))
    assert len(processed) == 1
    ev = json.loads(processed[0].read_text())
    assert ev["type"] == "session_finished" and ev["id"] == "i5"

    rig.r.tick(now=NOW + 15)                           # same runner: latched
    fresh = runner_mod.Runner(                          # restarted runner: rebuilt + reconciled
        repo=str(rig.repo), config=make_config(), state_home=str(rig.home), pane="p",
        run_script=lambda *a, **k: 0,
        fetch_usage=lambda: {"auth_status": "ok", "five_hour_pct": 1.0, "seven_day_pct": 1.0})
    fresh.tick(now=NOW + 30)
    processed = list((rig.home / "state" / "events" / "processed").glob("*.json"))
    assert len(processed) == 1                         # still exactly one finished event


# --------------------------- executors ---------------------------

def _launch_action(iid="i101", num=101, branch="sl/i101-render-the-widget"):
    return {"act": "launch", "id": iid, "num": num, "branch": branch,
            "touches": [], "soft_overlap": False, "orphan": False}


def _relabel_parsed(rig, iid, extra_labels):
    """Re-derive the runner's cached parsed issue with EXTRA control labels appended, exactly as a
    fresh poll would after William applies them — so these tests prove the label -> parse -> launch
    env path end to end, not just the runner reading a pre-set field."""
    raw = dict(rig.r._raw_by_id[iid])
    raw["labels"] = list(raw.get("labels") or []) + [{"name": n} for n in extra_labels]
    rig.r._parsed_by_id[iid] = issues.parse_issue(raw)


def test_launch_env_contract_and_registration(rig):
    rig.r.tick(now=NOW)                                # poll: i101 lands in the parsed view
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    call = rig.calls[-1]
    assert call["args"][0].endswith("launch-session.sh") and call["args"][1] == "i101"
    env = call["env"]
    assert env["SL_RUN_ROOT"] == str(rig.home)
    assert env["SL_REPO"] == str(rig.repo)
    assert env["SL_PANE"] == "pane-1"
    assert env["SL_DEV_BRANCH"] == "main"
    assert env["SL_MODEL"] == "opus"                   # models.worker
    assert env["SL_AGENT"] == "claude"                  # default agent path stays Claude
    ist = issue_state(rig, "i101")
    assert ist["status"] == "running" and ist["branch"] == "sl/i101-render-the-widget"
    brief_text = (rig.home / "briefs" / "i101.md").read_text()
    assert "#101" in brief_text
    m = mutations(rig)[-1]
    assert m["kind"] == "set_labels" and m["add"] == "in-progress" and m["remove"] == "agent-ready"


def test_launch_env_uses_per_issue_model_override(rig):
    # a model:<value> label on the issue beats config.models.worker for THIS issue's worker.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:fable"])
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "fable"                  # per-issue label, not config's "opus"
    assert env["SL_EFFORT"] == ""                      # no effort label -> nothing sent


def test_launch_env_uses_per_issue_sonnet_model_override(rig):
    # issue #134: model:sonnet is a first-class seeded knob, so the label an owner drops must
    # reach the worker's SL_MODEL exactly like the other model:* values (no allowlist, no mapping).
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:sonnet"])
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "sonnet"                 # per-issue label, not config's "opus"
    assert env["SL_EFFORT"] == ""


def test_launch_env_carries_explicit_codex_agent_without_changing_protocol(rig):
    # Runner-level selection only: the same launch action / labels / issue selection path is used,
    # but Codex defaults to its own model unless an explicit label/env override is present.
    rig.r.agent = "codex"
    rig.r.tick(now=NOW)
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    call = rig.calls[-1]
    assert call["args"][0].endswith("launch-session.sh") and call["args"][1] == "i101"
    assert call["env"]["SL_AGENT"] == "codex"
    assert call["env"]["SL_MODEL"] == ""
    assert call["env"]["SL_EFFORT"] == ""
    assert call["env"]["SL_CODEX_DANGEROUS_BYPASS"] == "0"
    assert call["env"]["SL_CODEX_BYPASS_HOOK_TRUST"] == "1"
    assert call["env"]["SL_CODEX_NO_ALT_SCREEN"] == "1"


def test_codex_usage_is_deferred_and_does_not_call_claude_usage(rig):
    # Codex usage/quota accounting is deferred in v1, so stale/unavailable Claude usage must not
    # block an opt-in Codex launch.
    rig.r.agent = "codex"
    rig.r._fetch_usage = lambda: (_ for _ in ()).throw(AssertionError("Claude usage called"))

    rig.r.tick(now=NOW)

    assert any(c["args"][0].endswith("launch-session.sh") and c["args"][1] == "i101"
               for c in rig.calls)
    usage = rig.r.usage_view()
    assert usage["auth_status"] == "ok"
    assert usage["usage_deferred"] is True
    assert usage["agent"] == "codex"


def test_codex_launch_env_uses_config_and_label_overrides(rig):
    rig.r.agent = "codex"
    rig.r.config["codex"]["dangerous_bypass"] = True
    rig.r.config["codex"]["bypass_hook_trust"] = False
    rig.r.config["models"]["worker_effort"] = "medium"
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:gpt-5.5"])
    rig.calls.clear()
    rig.r._execute(_launch_action(), NOW)
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "gpt-5.5"
    assert env["SL_EFFORT"] == "medium"
    assert env["SL_CODEX_DANGEROUS_BYPASS"] == "1"
    assert env["SL_CODEX_BYPASS_HOOK_TRUST"] == "0"


def test_launch_env_carries_per_issue_effort_and_default_model(rig):
    # an effort:<level> label is forwarded; with no model label the model falls back to config.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["effort:high"])
    rig.calls.clear()
    rig.r._execute(_launch_action(), NOW)
    env = rig.calls[-1]["env"]
    assert env["SL_EFFORT"] == "high"
    assert env["SL_MODEL"] == "opus"                   # config models.worker default


def test_launch_env_effort_is_empty_when_unlabeled(rig):
    # the default path: an issue with no effort label sends SL_EFFORT="" so start-session.sh omits
    # --effort entirely (never a default effort).
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.r._execute(_launch_action(), NOW)
    assert rig.calls[-1]["env"]["SL_EFFORT"] == ""


def test_launch_embeds_post_approval_comments_and_journals_fetch(rig):
    # Incident 2026-07-07 §8: comments William posts AFTER approving an issue must reach the worker.
    # _exec_launch fetches the issue's live comment thread and folds it into the brief; owner comments
    # render as binding amendments, the runner's own marker comments are skipped, and the fetched
    # count is journaled (the proceed-and-journal posture).
    rig.r.tick(now=NOW)                                    # poll: i101 lands in the parsed view
    rig.r.config["repo"] = "will-titan/r"                  # make the fixture's owner-authored comment bind
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    brief_text = (rig.home / "briefs" / "i101.md").read_text()
    # key on the rendered section heading, not the bare phrase (Step 0 names the block too)
    assert "## Amendments posted after approval (BINDING" in brief_text
    assert "Approved by William in conversation, 2026-07-02." in brief_text
    # the non-owner `<!-- superlooper-investigation -->` marker comment is skipped, not embedded
    assert "Root cause: the cache key omitted the tenant id." not in brief_text
    rec = [r for r in journal.read(rig.home) if r.get("act") == "brief_comments"][-1]
    assert rec["id"] == "i101" and rec["fetched"] == 2     # both comments fetched (then filtered)


def test_launch_proceeds_when_comment_fetch_yields_nothing(rig, monkeypatch):
    # A refused comment read carries comments=[] (CommentRead ok=False), so a fetch failure is, for
    # brief-building, indistinguishable from a comment-less issue — either way the brief is complete
    # and the launch proceeds (never park a fully-approved issue over a supplementary channel). The
    # journal records fetched=0.
    rig.r.tick(now=NOW)
    monkeypatch.setattr(runner_mod.gh, "issue_comments", lambda num: runner_mod.gh.CommentRead([], False))
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    brief_text = (rig.home / "briefs" / "i101.md").read_text()
    assert "## Amendments posted after approval (BINDING" not in brief_text
    assert "### Other comments (context only" not in brief_text
    rec = [r for r in journal.read(rig.home) if r.get("act") == "brief_comments"][-1]
    assert rec["fetched"] == 0


def test_recover_relaunch_carries_the_per_issue_override(rig):
    # D4 exited-recovery relaunch resolves the model INDEPENDENTLY of first launch — it must consult
    # the same parsed issue, or a crashed worker silently reverts to the default on relaunch.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:fable", "effort:high"])
    rig.calls.clear()
    out = rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "fable" and env["SL_EFFORT"] == "high"


def test_resolve_conflict_relaunch_carries_the_per_issue_override(rig):
    # the preserve-path conflict resolver relaunches the worker; it too must keep the override.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:fable", "effort:xhigh"])
    seed_issue(rig, "i101", branch="sl/i101-render-the-widget", pr=7)
    rig.calls.clear()
    out = rig.r._execute({"act": "resolve_conflict", "id": "i101", "num": 101, "pr": 7}, NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "fable" and env["SL_EFFORT"] == "xhigh"


def test_answerer_env_ignores_per_issue_model_and_effort_labels(rig):
    # the answerer is config-only: a per-issue model:/effort: label on the issue it is hired FOR must
    # NOT leak into the answerer session (it stays config.models.answerer, no effort).
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:opus[1m]", "effort:max"])
    rig.calls.clear()
    out = rig.r._execute({"act": "hire_answerer", "id": "i101", "num": 101,
                          "answerer_id": "a1", "question": "A or B?"}, NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "fable"                  # config answerer, NOT the issue's model:opus[1m]
    assert env["SL_EFFORT"] == ""                      # never a per-issue effort for the answerer
    assert env["SL_AGENT"] == "claude"


def test_codex_answerer_uses_only_explicit_env_model_and_effort(rig, monkeypatch):
    rig.r.agent = "codex"
    monkeypatch.setenv("SL_MODEL", "gpt-5.5")
    monkeypatch.setenv("SL_EFFORT", "low")
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:opus[1m]", "effort:max"])
    rig.calls.clear()
    out = rig.r._execute({"act": "hire_answerer", "id": "i101", "num": 101,
                          "answerer_id": "a1", "question": "A or B?"}, NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "gpt-5.5"
    assert env["SL_EFFORT"] == "low"
    assert env["SL_AGENT"] == "codex"


def test_launch_stamps_the_override_into_issue_state(rig):
    # the durable record recover/resolve_conflict fall back to when the parsed cache is unavailable
    # (a cold restart / gh outage). Stamped at first launch, from the parsed label.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:fable", "effort:high"])
    rig.r._execute(_launch_action(), NOW)
    ist = issue_state(rig, "i101")
    assert ist["model"] == "fable" and ist["effort"] == "high"


def test_launch_env_uses_config_worker_effort_when_no_label(rig):
    # repo-wide default: models.worker_effort applies to a worker with no effort:* label.
    rig.r.config["models"]["worker_effort"] = "high"
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.r._execute(_launch_action(), NOW)
    assert rig.calls[-1]["env"]["SL_EFFORT"] == "high"


def test_issue_effort_label_beats_config_worker_effort(rig):
    # precedence: an issue effort:* label wins over the repo-wide config default.
    rig.r.config["models"]["worker_effort"] = "low"
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["effort:max"])
    rig.calls.clear()
    rig.r._execute(_launch_action(), NOW)
    assert rig.calls[-1]["env"]["SL_EFFORT"] == "max"


def test_answerer_env_ignores_config_worker_effort(rig):
    # worker_effort is a WORKER default only — it must never reach the config-only answerer.
    rig.r.config["models"]["worker_effort"] = "max"
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.r._execute({"act": "hire_answerer", "id": "i101", "num": 101,
                    "answerer_id": "a1", "question": "?"}, NOW)
    assert rig.calls[-1]["env"]["SL_EFFORT"] == ""


def test_recover_uses_stamped_override_when_parsed_view_is_empty(rig):
    # Codex review 2026-07-07 #1: recover-exited fires from the on-disk marker even with an EMPTY
    # parsed cache (cold restart before the first poll / gh unreachable). The override must survive
    # via the value stamped at launch — never silently reverting to the config default (which would
    # defeat model:fable's whole budget purpose). And it must still relaunch, never block.
    seed_issue(rig, "i101", model="fable", effort="high")   # what _exec_launch stamped earlier
    rig.r._parsed_by_id = {}                                 # gh unreachable -> empty cache
    rig.calls.clear()
    out = rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert out == "ok"                                       # relaunch is NOT blocked
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "fable" and env["SL_EFFORT"] == "high"


def test_relaunch_refreshes_the_stamp_to_the_current_labels(rig):
    # Codex round-2: a label removed mid-flight must not be resurrected from a first-launch stamp.
    # When the fresh parsed view resolves the override, the durable stamp is REFRESHED to match — so
    # a later cache-empty relaunch falls back to the current labels, not a stale value.
    rig.r.tick(now=NOW)
    seed_issue(rig, "i101", model="fable", effort="high")   # stale stamp; the label is now gone
    rig.r._execute(_launch_action(), NOW)                   # launch through the fresh (label-less) view
    ist = issue_state(rig, "i101")
    assert ist["model"] is None and ist["effort"] is None   # stamp refreshed to match live labels


def test_relaunch_stamp_captures_a_mid_flight_added_label(rig):
    # symmetric: a label ADDED after first launch is durably preserved — the fresh relaunch uses it
    # AND refreshes the stamp, so a later outage-restart keeps it too.
    rig.r.tick(now=NOW)
    _relabel_parsed(rig, "i101", ["model:fable"])
    rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert issue_state(rig, "i101")["model"] == "fable"


def test_corrupt_stamped_control_degrades_safely_when_cache_empty(rig):
    # Codex round-2 (medium): a wrong-typed stamp read back during an outage relaunch must fall back
    # to the default, never stringify garbage into `claude --model/--effort`.
    seed_issue(rig, "i101", model=["fable"], effort={"x": 1})   # corrupt state
    rig.r._parsed_by_id = {}                                    # cache unavailable
    rig.calls.clear()
    out = rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert out == "ok"
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "opus" and env["SL_EFFORT"] == ""   # corrupt stamp ignored -> defaults


def test_fresh_parsed_view_wins_over_a_stale_stamped_value(rig):
    # the fresh parsed view is authoritative: a label William REMOVED mid-flight must not be
    # resurrected from the stamp. Parsed i101 has no model/effort label -> config default.
    rig.r.tick(now=NOW)
    seed_issue(rig, "i101", model="fable", effort="high")   # stale stamp from a prior launch
    rig.calls.clear()
    rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    env = rig.calls[-1]["env"]
    assert env["SL_MODEL"] == "opus"                         # config default wins, stamp ignored
    assert env["SL_EFFORT"] == ""


def test_failed_launch_bumps_the_counter_and_moves_no_labels(rig):
    rig.r.tick(now=NOW)
    rig.calls.clear()
    before = len(mutations(rig))
    rig.rc_queue.append(2)                             # delivery never verified
    out = rig.r._execute(_launch_action(), NOW)
    assert out != "ok"
    ist = issue_state(rig, "i101")
    assert ist["status"] == "ready" and ist["launch_failures"] == 1
    assert len(mutations(rig)) == before               # no label move without a live worker


# --------------------------- launch anchor liveness (#24) ---------------------------

def test_missing_base_branch_launch_records_the_cause_and_skips_the_streak(rig):
    # issue #28: launch-session.sh exits 3 when the worktree base branch is missing. The runner must
    # stamp launch_error="base_missing" (so the park memo names the branch) and must NOT feed this to
    # the systemic-anchor streak — a missing base is a per-repo config fault, not a dead cmux anchor.
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(3)                             # launch-session.sh: worktree base missing
    out = rig.r._execute(_launch_action(), NOW)
    assert out != "ok"
    ist = issue_state(rig, "i101")
    assert ist["status"] == "ready" and ist["launch_failures"] == 1
    assert ist["launch_error"] == "base_missing"
    assert "i101" not in rig.r._launch_fail_ids        # NOT a systemic-anchor fault


def test_verified_launch_clears_a_stale_base_missing_error(rig):
    # A verified delivery proves the base now exists (config fixed + re-approved): the stale cause
    # must be cleared so a later unrelated park can't inherit the wrong memo.
    rig.r.tick(now=NOW)
    seed_issue(rig, "i101", launch_error="base_missing")
    rig.calls.clear()                                  # rc_queue empty -> run_script returns 0 (ok)
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    assert issue_state(rig, "i101")["launch_error"] is None


def test_generic_delivery_failure_does_not_stamp_base_missing(rig):
    # A plain non-delivery (exit 2) is NOT a base problem — launch_error must stay clear so the
    # default "is the shim installed?" memo is used.
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(2)
    rig.r._execute(_launch_action(), NOW)
    assert issue_state(rig, "i101").get("launch_error") is None


def test_failed_launch_delivery_records_the_issue_in_the_systemic_streak(rig):
    # A launch whose delivery is not verified feeds the runner-level systemic-failure streak — the
    # signal decide uses to tell a dead anchor (many issues) from a bad issue (one).
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(2)                             # delivery never verified
    rig.r._execute(_launch_action(), NOW)
    assert "i101" in rig.r._launch_fail_ids


def test_verified_launch_clears_the_systemic_streak(rig):
    # A verified delivery is proof the anchor is alive: it clears the whole streak.
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i9", "i101"}            # a prior run of failures
    rig.calls.clear()                                  # rc_queue empty -> run_script returns 0 (ok)
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok" and rig.r._launch_fail_ids == set()


def test_verified_recover_delivery_clears_the_systemic_streak(rig):
    # ANY verified delivery proves the anchor is live — including a recover-exited relaunch, not just
    # a fresh launch — so it clears the streak and lets a systemic hold lift without a restart.
    rig.r.tick(now=NOW)                                # poll lands the view (for _worker_env)
    rig.r._launch_fail_ids = {"i9", "i101"}            # a prior run of failures
    rig.calls.clear()                                  # rc_queue empty -> run_script returns 0 (ok)
    out = rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert out == "ok" and rig.r._launch_fail_ids == set()


def test_failed_recover_delivery_does_not_clear_the_streak(rig):
    # A recover that does NOT verify delivery is not proof of anything — the streak must persist.
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i9", "i101"}
    rig.calls.clear()
    rig.rc_queue.append(2)                             # delivery not verified
    rig.r._execute({"act": "recover", "id": "i101", "tier": "exited"}, NOW)
    assert rig.r._launch_fail_ids == {"i9", "i101"}


def test_brief_failure_does_not_touch_the_streak(rig):
    # An early skip (issue no longer in the view) is NOT a delivery failure — it must not pollute
    # the anchor streak, or one dropped issue could masquerade as a systemic fault.
    rig.r.tick(now=NOW)
    out = rig.r._execute({"act": "launch", "id": "i999", "num": 999, "branch": "sl/i999-x",
                          "touches": [], "soft_overlap": False, "orphan": False}, NOW)
    assert out.startswith("skipped") and rig.r._launch_fail_ids == set()


def test_wants_launch_reflects_the_agent_ready_queue(rig):
    rig.r._parsed_by_id = {"i5": {"labels": ["agent-ready", "type:build"]}}
    assert rig.r._wants_launch() is True
    rig.r._parsed_by_id = {"i5": {"labels": ["in-progress", "type:build"]}}  # claimed
    assert rig.r._wants_launch() is False
    rig.r._parsed_by_id = {}                           # empty queue
    assert rig.r._wants_launch() is False


def test_tick_holds_launches_and_alerts_when_the_anchor_is_gone(rig):
    # End to end at the shell: a tick with launch demand re-probes the anchor; a probe that reports
    # the pane unresolvable holds every launch and raises ONE runner-level alert — no per-issue
    # parks, no label moves, the approved queue left intact.
    rig.r._anchor_status = lambda: {"ok": False, "reason": "pane 'pane-1' not found"}
    before = len(mutations(rig))
    rig.r.tick(now=NOW)                                # poll lands the queue; probe reports down
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert alert["reasons"] == ["launch_anchor_down"]
    assert issue_state(rig, "i101") is None            # never launched -> no loopstate stamp
    assert len(mutations(rig)) == before               # agent-ready never moved to in-progress
    launches = [c for c in rig.calls if c["args"][0].endswith("launch-session.sh")]
    assert launches == []                              # launch-session.sh never even invoked


def test_tick_launches_normally_when_the_anchor_is_healthy(rig):
    # The healthy path is unchanged: a resolvable anchor + an empty streak launches the queue.
    rig.r.tick(now=NOW)
    assert issue_state(rig, "i101")["status"] == "running"
    assert "ALERT" not in os.listdir(rig.home / "state")


def test_a_restart_clears_the_systemic_launch_streak(rig):
    # The systemic streak is in-memory on purpose: a restart in a visible tab — the documented
    # recovery for a wedged anchor (issue #24) — resets it to a clean slate.
    rig.r._launch_fail_ids = {"i1", "i2", "i3"}        # mid-episode: a wedged anchor
    fresh = runner_mod.Runner(repo=str(rig.repo), config=make_config(),
                              state_home=str(rig.home), pane="pane-1")
    assert fresh._launch_fail_ids == set()


# --------------------------- canary re-arm of the systemic hold (#115) ---------------------------

def test_a_launch_delivery_failure_stamps_the_canary_retry_clock(rig):
    # The canary retry clock is anchored at the most recent delivery failure, so the FIRST probe
    # waits a full interval after the breaker trips (never fires the instant the hold engages).
    rig.r.tick(now=NOW)
    rig.calls.clear()
    rig.rc_queue.append(2)                             # delivery never verified
    rig.r._execute(_launch_action(), NOW)
    assert rig.r._launch_fail_at == NOW


def test_a_verified_launch_resets_the_canary_retry_clock(rig):
    # A verified delivery clears the streak AND resets the clock, so a later degraded episode starts
    # its interval fresh rather than inheriting a stale timestamp.
    rig.r.tick(now=NOW)
    rig.r._launch_fail_at = NOW - 500
    rig.r._launch_fail_ids = {"i9", "i101"}
    rig.calls.clear()                                  # rc_queue empty -> run_script returns 0 (ok)
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok" and rig.r._launch_fail_at == 0


def test_a_failed_canary_probe_charges_no_per_issue_launch_cap(rig):
    # DoD #2: the canary is a SYSTEMIC probe, never charged to the issue. A failed canary must not
    # bump the per-issue launch-failure counter (else repeated probes would eventually park the very
    # front-of-queue issue the breaker is trying to protect).
    rig.r.tick(now=NOW)
    seed_issue(rig, "i101", status="ready", launch_failures=1)
    rig.calls.clear()
    rig.rc_queue.append(2)                             # canary delivery still failing
    out = rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert out != "ok"
    assert issue_state(rig, "i101")["launch_failures"] == 1     # UNCHANGED — no cap charged
    assert rig.r._launch_fail_at == NOW                         # but the retry clock advances


def test_a_failed_canary_probe_re_enters_the_hold_without_parking(rig):
    # A failed canary keeps the streak intact (the hold persists) and parks nothing.
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i101", "i102"}
    rig.calls.clear()
    rig.rc_queue.append(2)
    rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert rig.r._launch_fail_ids == {"i101", "i102"}          # streak preserved -> still held
    assert issue_state(rig, "i101")["status"] == "ready"       # queued, not parked


def test_a_verified_canary_clears_the_streak_and_launches_the_issue(rig):
    # A verified canary delivery IS a real launch: it clears the systemic streak, resets the clock,
    # and the probed issue is genuinely running (labels moved) — the queue re-arms from here.
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i101", "i102"}
    rig.r._launch_fail_at = NOW - 500
    rig.calls.clear()                                  # rc_queue empty -> run_script returns 0 (ok)
    out = rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert out == "ok"
    assert rig.r._launch_fail_ids == set() and rig.r._launch_fail_at == 0
    assert issue_state(rig, "i101")["status"] == "running"
    m = mutations(rig)[-1]
    assert m["kind"] == "set_labels" and m["add"] == "in-progress" and m["remove"] == "agent-ready"


def test_a_failed_canary_via_base_missing_charges_no_cap_and_re_spaces_the_clock(rig):
    # A canary is a systemic probe (#115): even a base-missing (rc=3) canary must charge NO per-issue
    # cap and must re-space the retry clock (so the next probe waits a full interval, never a per-tick
    # re-fire). base_missing deliberately stays OUT of the streak (a config fault, not a dead anchor),
    # so the hold persists on the existing streak.
    rig.r.tick(now=NOW)
    seed_issue(rig, "i101", status="ready", launch_failures=1)
    rig.r._launch_fail_ids = {"i101", "i102"}
    rig.r._launch_fail_at = NOW - 500
    rig.calls.clear()
    rig.rc_queue.append(3)                             # launch-session.sh: worktree base missing
    out = rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert out != "ok"
    assert issue_state(rig, "i101")["launch_failures"] == 1     # NO per-issue cap charged to a probe
    assert rig.r._launch_fail_at == NOW                         # clock re-spaced (no per-tick re-fire)
    assert rig.r._launch_fail_ids == {"i101", "i102"}          # base_missing out of the streak; held


def test_a_canary_whose_brief_fails_still_re_spaces_the_retry_clock(rig, monkeypatch):
    # If the front-of-queue canary issue cannot be briefed, the probe must STILL re-space the retry
    # clock — otherwise it would busy-spin every tick and the systemic hold could never self-clear
    # (the exact freeze #115 targets, re-triggered on a narrow input).
    rig.r.tick(now=NOW)
    rig.r._launch_fail_ids = {"i101", "i102"}
    rig.r._launch_fail_at = NOW - 500
    rig.calls.clear()
    monkeypatch.setattr(runner_mod.brief, "build",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("unbriefable issue")))
    out = rig.r._execute(dict(_launch_action(), canary=True), NOW)
    assert out.startswith("brief failed")
    assert rig.r._launch_fail_at == NOW                         # clock re-spaced despite the brief error
    launches = [c for c in rig.calls if c["args"][0].endswith("launch-session.sh")]
    assert launches == []                                       # never even reached launch-session.sh


def test_launch_recovered_executor_is_wired_and_returns_its_reason(rig):
    # Journal-only executor (no label move, no crash, no "no executor for ...").
    assert rig.r._execute({"act": "launch_recovered", "reason": "hold cleared"}, NOW) == "hold cleared"


def test_systemic_hold_re_arms_via_canary_end_to_end(rig):
    # DoD #1 (#115): trip (2 distinct-issue delivery failures) -> hold; a canary after the retry
    # interval with delivery STILL failing -> still held, zero parks, zero new notifies, per-issue
    # caps untouched; delivery recovers -> the canary verifies -> streak clears, the ALERT clears
    # with a journaled recovery record, and the held queue resumes launching in priority order.
    # Soft affinity so the resume is a clean parallel launch, not an affinity-serialized dribble.
    rig.r.config["affinity"] = "soft"
    launches = lambda: [c for c in rig.calls if c["args"][0].endswith("launch-session.sh")]

    # ---- trip: two DISTINCT issues fail delivery back-to-back (the breaker's own signal) ----
    rig.r._poll_github(NOW)                            # populate the queue WITHOUT auto-launching
    rig.rc_queue.extend([2, 2])                        # i101, i102 both fail delivery
    rig.r._execute(_launch_action("i101", 101, "sl/i101-render-the-widget"), NOW)
    rig.r._execute(_launch_action("i102", 102, "sl/i102-add-the-api-route"), NOW)
    assert rig.r._launch_fail_ids == {"i101", "i102"}          # streak is systemic (>= cap)
    assert rig.r._launch_fail_at == NOW                        # the retry clock is anchored here
    assert mutations(rig) == [] or all(m.get("kind") != "set_labels"    # failed launches move no
                                       for m in mutations(rig))          # labels -> queue intact

    # ---- hold: a tick sees the streak, alerts once, holds every launch, parks nothing ----
    rig.calls.clear()
    rig.r.tick(now=NOW + 15)                           # interval (300s) not elapsed -> no canary
    assert launches() == []                            # held: launch-session.sh not invoked
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert alert["reasons"] == ["launch_systemic_failure"]
    assert [i for i in _all_issue_states(rig).values() if i.get("status") == "parked"] == []

    # ---- canary after the interval, delivery still failing: ONE probe, still held, no cap charged ----
    rig.calls.clear()
    rig.rc_queue.append(2)                             # canary delivery still fails
    rig.r.tick(now=NOW + 400)                          # >= CANARY_RETRY_SECONDS since the trip
    probes = launches()
    assert len(probes) == 1 and probes[0]["args"][1] == "i102"   # front-of-queue (priority:high)
    assert rig.r._launch_fail_ids == {"i101", "i102"}           # still held
    assert issue_state(rig, "i102")["launch_failures"] == 1      # probe charged NO per-issue cap
    assert [i for i in _all_issue_states(rig).values() if i.get("status") == "parked"] == []
    assert alert == json.loads((rig.home / "state" / "ALERT").read_text())   # no new/changed alert

    # ---- recovery: a later canary verifies; streak + clock clear, the issue runs ----
    rig.calls.clear()
    rig.r.tick(now=NOW + 800)                          # rc_queue empty -> delivery verified (rc 0)
    assert rig.r._launch_fail_ids == set() and rig.r._launch_fail_at == 0
    assert issue_state(rig, "i102")["status"] == "running"

    # ---- resume: the next tick journals the recovery, clears the alert, launches the rest ----
    rig.calls.clear()
    rig.r.tick(now=NOW + 900)
    rec = [r for r in journal.read(rig.home) if r.get("act") == "launch_recovered"]
    assert len(rec) == 1
    assert "ALERT" not in os.listdir(rig.home / "state")        # alert cleared on recovery
    resumed = {c["args"][1] for c in launches()}
    assert "i101" in resumed and "i102" not in resumed          # held queue resumes; canary'd issue
    assert resumed <= {"i101", "i103"}                          # ...is already running, not relaunched


# --------------------------- fail-open on an unreadable meter (issue #46) ---------------------------

def test_dark_meter_past_grace_fails_open_launches_and_journals(rig):
    # End to end at the shell: the usage endpoint is dark (api_error). WITHIN the grace the loop
    # fails closed (no launch); PAST the grace it FAILS OPEN — i101 launches anyway and the journal
    # carries exactly one bounded fail_open record (proving the executor is wired: no "no executor").
    rig.r._fetch_usage = lambda: {"auth_status": "api_error", "five_hour_pct": None,
                                  "seven_day_pct": None}
    rig.r.tick(now=NOW)                                        # first dark fetch: within grace
    assert issue_state(rig, "i101") is None                    # fail closed: not launched yet
    assert [r for r in journal.read(rig.home) if r.get("act") == "fail_open"] == []

    # Advance in sub-cadence-gap steps so the loop reads as AWAKE: a real 30-min outage has ~120
    # continuous ticks, not one jump — a single 30-min jump would look like a wake gap and grant the
    # post-wake grace, holding the alarm (issue #42, covered by its own tests).
    rig.r.tick(now=NOW + 900)                                  # still within the fail-open grace
    rig.r.tick(now=NOW + 1801)                                 # 30 min + 1s dark: past the grace
    assert issue_state(rig, "i101")["status"] == "running"     # FAIL OPEN: launched despite the dark meter
    fo = [r for r in journal.read(rig.home) if r.get("act") == "fail_open"]
    assert len(fo) == 1 and "FAILING OPEN" in fo[0].get("outcome", "")
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert alert["reasons"] == ["usage_stale"]                 # owner alerted while work continues

    rig.r.tick(now=NOW + 1802)                                 # still dark, still past grace
    fo2 = [r for r in journal.read(rig.home) if r.get("act") == "fail_open"]
    assert len(fo2) == 1                                       # ...one record for the whole episode


def test_meter_recovery_closes_the_fail_open_episode_and_clears_the_alert(rig):
    # A fail-open episode is active on disk (usage_stale alerted). A healthy fetch closes it: the
    # journal gets a usage_recovered record and the usage_stale alert clears.
    (rig.home / "state" / "ALERT").write_text(json.dumps({"reasons": ["usage_stale"], "since": NOW - 10}))
    rig.r.tick(now=NOW)                                        # healthy stub usage -> meter reads again
    rec = [r for r in journal.read(rig.home) if r.get("act") == "usage_recovered"]
    assert len(rec) == 1
    assert "ALERT" not in os.listdir(rig.home / "state")       # the alert cleared


def test_fail_open_executors_are_wired_and_return_their_reason(rig):
    # Direct smoke of the two journal-only executors (no label move, no crash, no "no executor").
    assert rig.r._execute({"act": "fail_open", "reason": "dark past grace"}, NOW) == "dark past grace"
    assert rig.r._execute({"act": "usage_recovered", "reason": "readable again"}, NOW) == "readable again"


def test_restart_during_an_outage_does_not_falsely_recover(rig):
    # End to end across a restart: the ALERT (durable) says the episode is open, but a FRESH Runner's
    # grace clock (in-memory) is reset. The first post-restart tick, with the meter still dark, must
    # NOT journal usage_recovered nor clear the outage alert — the episode carries across the restart.
    (rig.home / "state" / "ALERT").write_text(
        json.dumps({"reasons": ["usage_stale"], "since": NOW - 9000}))
    fresh = runner_mod.Runner(repo=str(rig.repo), config=make_config(), state_home=str(rig.home),
                              pane="pane-1", run_script=lambda *a, **k: 0,
                              fetch_usage=lambda: {"auth_status": "api_error"})   # meter still dark
    fresh._anchor_status = lambda: {"ok": True, "reason": ""}
    fresh.tick(now=NOW)
    assert [r for r in journal.read(rig.home) if r.get("act") == "usage_recovered"] == []
    assert (rig.home / "state" / "ALERT").exists()     # outage alert NOT retracted mid-incident


# --------------------------- post-wake grace (issue #42) ---------------------------
# Closing the laptop overnight suspends the runner mid-tick; on wake the next tick lands hours later
# than the ~15s cadence predicts. Every in-flight worker's activity_mtime and the usage meter's
# last-success then look ancient purely from the wall-clock jump, which used to fire a cascade of
# false frozen-recovery nudges + a self-clearing usage_stale ALERT. These pin the wake-gap grace.

def _activity(rig, iid, mtime):
    p = rig.home / "state" / "activity" / iid
    p.write_text("x")
    os.utime(p, (mtime, mtime))


def _frozen_nudges(rig):
    return [c for c in rig.calls if c["args"][0].endswith("nudge-pane.sh")
            and "inactive for a long time" in c["args"][3]]


def test_a_wake_gap_opens_the_grace_window_and_journals_it(rig):
    rig.r.tick(now=NOW)
    assert rig.r._wake_grace_until == 0.0                       # a first tick opens no grace
    big = NOW + runner_mod.WAKE_GAP_SECONDS + 100              # the resume tick lands far past cadence
    rig.r.tick(now=big)
    assert rig.r._wake_grace_until == big + runner_mod.WAKE_GRACE_SECONDS
    wg = [r for r in journal.read(rig.home) if r.get("act") == "wake_gap"]
    assert len(wg) == 1


def test_a_normal_tick_cadence_opens_no_grace(rig):
    rig.r.tick(now=NOW)
    rig.r.tick(now=NOW + runner_mod.TICK_SECONDS)
    assert rig.r._wake_grace_until == 0.0
    assert [r for r in journal.read(rig.home) if r.get("act") == "wake_gap"] == []


def test_wake_gap_suppresses_frozen_nudges_then_rearms_for_a_dead_session(rig):
    seed_issue(rig, "i5", status="running")
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    _activity(rig, "i5", NOW)                                   # healthy and fresh before the sleep
    rig.r.tick(now=NOW)
    assert _frozen_nudges(rig) == []

    # laptop slept for hours: activity looks ancient purely from the wall-clock jump
    big = NOW + runner_mod.WAKE_GAP_SECONDS + 2700
    rig.r.tick(now=big)
    assert _frozen_nudges(rig) == []                           # wake grace: no false frozen nudge
    rig.r.tick(now=big + 30)
    assert _frozen_nudges(rig) == []                           # still within the grace: still silent

    # the session never re-stamped -> genuinely dead -> the recovery nudge finally fires past the grace
    rig.r.tick(now=big + runner_mod.WAKE_GRACE_SECONDS + 1)
    assert len(_frozen_nudges(rig)) == 1
    assert issue_state(rig, "i5")["status"] == "frozen"


def test_a_healthy_session_that_restamps_after_wake_never_gets_a_frozen_nudge(rig):
    seed_issue(rig, "i5", status="running")
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    _activity(rig, "i5", NOW)
    rig.r.tick(now=NOW)
    big = NOW + runner_mod.WAKE_GAP_SECONDS + 2700
    rig.r.tick(now=big)                                        # wake: grace opens, no nudge
    assert _frozen_nudges(rig) == []
    _activity(rig, "i5", big + 10)                            # worker resumed and re-stamped in-grace
    rig.r.tick(now=big + runner_mod.WAKE_GRACE_SECONDS + 1)    # past the grace, but activity is fresh
    assert _frozen_nudges(rig) == []                          # never falsely nudged


def test_wake_gap_holds_the_usage_stale_alert_then_rearms_if_still_dark(rig):
    rig.r.tick(now=NOW)                                        # healthy fetch: last_ok = NOW
    rig.r._fetch_usage = lambda: {"auth_status": "api_error", "five_hour_pct": None,
                                  "seven_day_pct": None}       # network still down on wake
    big = NOW + runner_mod.WAKE_GAP_SECONDS + 2000            # a wake gap AND past the 30-min fail-open grace
    rig.r.tick(now=big)
    assert "ALERT" not in os.listdir(rig.home / "state")      # wake grace holds the usage_stale alert
    assert [r for r in journal.read(rig.home) if r.get("act") == "fail_open"] == []

    rig.r.tick(now=big + runner_mod.WAKE_GRACE_SECONDS + 1)    # past the wake grace, meter still dark
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert alert["reasons"] == ["usage_stale"]                # a genuinely dark meter re-arms


def test_hire_answerer_env_brief_and_record(rig):
    rig.r.tick(now=NOW)
    (rig.home / "answers" / "i123.md").write_text("stale answer from a prior question")
    rig.calls.clear()
    out = rig.r._execute({"act": "hire_answerer", "id": "i123", "num": 123,
                          "answerer_id": "a1", "question": "A or B?"}, NOW)
    assert out == "ok"
    call = rig.calls[-1]
    assert call["args"][1] == "--cwd" and call["args"][2] == str(rig.home / "answers")
    assert call["args"][3] == "a1"
    assert call["env"]["SL_MODEL"] == "fable"          # models.answerer
    assert not (rig.home / "answers" / "i123.md").exists()   # stale answer purged at hire
    b = (rig.home / "briefs" / "a1.md").read_text()
    assert "A or B?" in b and "#123" in b and str(rig.home / "answers" / "i123.md") in b
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["answerers"]["a1"] == {"for": "i123", "launched_at": NOW}
    assert st["next_answerer"] == 2


def test_deliver_answer_clears_marker_and_record(rig):
    seed_issue(rig, "i5", status="blocked")
    def m(st):
        st["answerers"] = {"a1": {"for": "i5", "launched_at": NOW - 100}}
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    (rig.home / "state" / "blocked" / "i5").write_text("q?")
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    out = rig.r._execute({"act": "deliver_answer", "id": "i5", "answerer_id": "a1",
                          "text": "Use A."}, NOW)
    assert out == "ok"
    call = rig.calls[-1]
    assert call["args"][0].endswith("nudge-pane.sh")
    assert call["args"][1] == "surf-uuid" and call["args"][2] == "i5"
    assert "Use A." in call["args"][3]
    assert not (rig.home / "state" / "blocked" / "i5").exists()
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["answerers"] == {} and st["issues"]["i5"]["status"] == "running"


def test_deliver_to_a_dead_pane_converts_to_the_exited_flow(rig):
    seed_issue(rig, "i5", status="blocked")
    (rig.home / "state" / "blocked" / "i5").write_text("q?")
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    rig.rc_queue.append(4)                             # RC-DEADPANE: never type into bash
    rig.r._execute({"act": "deliver_answer", "id": "i5", "answerer_id": "a1",
                    "text": "Use A."}, NOW)
    assert (rig.home / "state" / "exited" / "i5").exists()
    assert issue_state(rig, "i5")["answer_delivery_failures"] == 1
    assert (rig.home / "state" / "blocked" / "i5").exists()    # question preserved


def test_bounce_posts_memo_then_labels_then_state(rig):
    seed_issue(rig, "i7", status="blocked")
    (rig.home / "state" / "blocked" / "i7").write_text("BOUNCED: premise gone")
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7,
                          "memo": "BOUNCED: premise gone"}, NOW)
    assert out == "ok"
    ms = mutations(rig)
    assert ms[-2]["kind"] == "comment" and "BOUNCED: premise gone" in ms[-2]["body"]
    assert ms[-1]["kind"] == "set_labels" and ms[-1]["add"] == "needs-owner" \
        and ms[-1]["remove"] == "in-progress"
    assert issue_state(rig, "i7")["status"] == "bounced"
    assert not (rig.home / "state" / "blocked" / "i7").exists()


def test_bounce_gh_failure_keeps_the_marker_for_retry(rig, monkeypatch):
    seed_issue(rig, "i7", status="blocked")
    (rig.home / "state" / "blocked" / "i7").write_text("BOUNCED: x")
    monkeypatch.setenv("GH_FAIL", "1")
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: x"}, NOW)
    assert out != "ok"
    assert issue_state(rig, "i7")["status"] == "blocked"       # truth not advanced
    assert (rig.home / "state" / "blocked" / "i7").exists()


def test_bounce_stamps_the_notify_marker_before_the_label_move(rig):
    """Issue #108: the bounce reuses #61's notify-once marker — stamped BEFORE gh is asked to move
    the needs-owner label, so a label write failing in the dead zone that NEEDS the bounce (the
    2026-07-13 missing needs-owner label) cannot re-text: decide sees the re-derived bounce as the
    SAME episode. The memo comment posts once per episode; the episode clock never re-stamps."""
    seed_issue(rig, "i7", status="running")
    (rig.home / "state" / "blocked" / "i7").write_text("BOUNCED: premise gone")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue edit", "times": 99,
          "stderr": "could not add label: 'needs-owner' not found"}]))
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7,
                          "memo": "BOUNCED: premise gone", "cause": "bounce"}, NOW)
    assert "label move failed" in out
    st = issue_state(rig, "i7")
    assert st["park_notify_cause"] == "bounce"         # stamped despite the failed write
    assert st["park_notify_at"] == NOW
    assert st["status"] == "running"                   # never advanced past the truth
    assert (rig.home / "state" / "blocked" / "i7").exists()   # marker kept for retry
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    # the retry tick re-attempts the label but must NOT re-post the memo, nor reset the clock
    # (the stuck-label alert bound runs from episode start)
    rig.r._execute({"act": "bounce", "id": "i7", "num": 7,
                    "memo": "BOUNCED: premise gone", "cause": "bounce", "retry": True}, NOW + 15)
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    assert issue_state(rig, "i7")["park_notify_at"] == NOW


def test_bounce_comment_retries_until_it_lands_then_never_reposts(rig):
    """A bounce whose memo comment ALSO failed retries the comment on later ticks until it lands —
    the worker's verbatim memo must reach the issue — but never double-posts (mirrors park)."""
    seed_issue(rig, "i7", status="running")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue comment", "times": 1, "stderr": "rate limited"},
         {"match": "issue edit", "times": 1, "stderr": "rate limited"}]))
    rig.r._execute({"act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: m",
                    "cause": "bounce"}, NOW)
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: m",
                          "cause": "bounce", "retry": True}, NOW + 15)
    assert out == "ok"
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    assert issue_state(rig, "i7")["status"] == "bounced"


def test_bounce_comment_only_failure_still_settles_the_bounce(rig):
    """Codex-M1 tradeoff, mirrored for bounce (issue #108 review P2): when the memo COMMENT fails
    but the label move succeeds, the issue settles terminal (bounced) — decide stops re-deriving, so
    the on-issue memo is NOT retried further. Accepted: the worker's verbatim memo already reached
    the owner via the notify text and the journal; the on-issue comment is best-effort once the
    bounce has landed (exactly the park path's accepted behavior)."""
    seed_issue(rig, "i7", status="running")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue comment", "times": 1, "stderr": "rate limited"}]))
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7, "memo": "BOUNCED: m",
                          "cause": "bounce"}, NOW)
    assert out == "ok"
    st = issue_state(rig, "i7")
    assert st["status"] == "bounced" and st["park_comment_posted"] is False
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert lab["add"] == "needs-owner"                 # the label DID land — the bounce is real


def test_bounce_stands_down_a_lingering_answerer_record(rig):
    """Issue #132 review: a bounce settles the issue terminal ('bounced'), so it must also stand
    down any answerer record still filed for that issue — mirroring _exec_park / _exec_absorb_close.
    Without this the "answerers holds exactly the active answerers" invariant that
    tidy.closable_answerers rests on would have a gap: a bounced issue's finished answerer window
    would stay protected from tidy until the issue was reapproved or closed by hand. Only THIS
    issue's record is dropped — an answerer for another issue is untouched."""
    seed_issue(rig, "i7", status="blocked")
    (rig.home / "state" / "blocked" / "i7").write_text("BOUNCED: premise gone")
    def add_answerers(st):
        st["answerers"] = {"a1": {"for": "i7", "launched_at": NOW},
                           "a2": {"for": "i9", "launched_at": NOW}}
    loopstate.update(str(rig.home / "state" / "issues.json"), add_answerers)
    out = rig.r._execute({"act": "bounce", "id": "i7", "num": 7,
                          "memo": "BOUNCED: premise gone"}, NOW)
    assert out == "ok"
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["issues"]["i7"]["status"] == "bounced"
    assert st["answerers"] == {"a2": {"for": "i9", "launched_at": NOW}}   # only i7's record dropped


def test_absorb_close_settles_terminal_and_stands_down(rig, monkeypatch):
    """Issue #108: the issue was closed on GitHub while the loop was bouncing/parking it. Absorb:
    settle terminal, clear the handback markers + blocked/awaiting files, reclaim the worktree, and
    NEVER write a label or a comment (the issue is closed — labels are moot, and the owner already
    answered)."""
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i7", status="running", park_notify_cause="bounce", park_notify_at=NOW - 30)
    (rig.home / "state" / "blocked" / "i7").write_text("BOUNCED: premise gone")
    (rig.home / "state" / "awaiting" / "i7").write_text("waiting")
    def add_answerer(st):
        st["answerers"] = {"a1": {"for": "i7", "launched_at": NOW}}
    loopstate.update(str(rig.home / "state" / "issues.json"), add_answerer)
    out = rig.r._execute({"act": "absorb_close", "id": "i7", "num": 7}, NOW)
    assert out == "ok"
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["issues"]["i7"]["status"] == "merged"    # terminal (loopstate has no 'closed')
    assert st["issues"]["i7"]["park_notify_cause"] is None and st["issues"]["i7"]["park_notify_at"] is None
    assert st["issues"]["i7"]["park_comment_posted"] is False
    assert st["answerers"] == {}                        # lingering answerer record cleaned
    assert not (rig.home / "state" / "blocked" / "i7").exists()
    assert not (rig.home / "state" / "awaiting" / "i7").exists()
    assert removed == [str(rig.r._worktree("i7"))]      # worktree reclaimed (mirrors absorb_merged)
    assert [m for m in mutations(rig) if m["kind"] == "set_labels"] == []
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []


def test_recover_exited_relaunches(rig):
    seed_issue(rig, "i5", status="running", branch="sl/i5-x")
    out = rig.r._execute({"act": "recover", "id": "i5", "tier": "exited"}, NOW)
    assert out == "ok"
    assert rig.calls[-1]["args"][0].endswith("launch-session.sh")
    assert rig.calls[-1]["args"][1] == "i5"
    assert issue_state(rig, "i5")["status"] == "running"


def test_recover_frozen_dead_pane_writes_the_exited_marker(rig):
    seed_issue(rig, "i5", status="running")
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    rig.rc_queue.append(4)                             # pane is a bash shell now
    rig.r._execute({"act": "recover", "id": "i5", "tier": "frozen"}, NOW)
    assert (rig.home / "state" / "exited" / "i5").exists()
    ist = issue_state(rig, "i5")
    assert ist["status"] == "frozen" and ist["last_recover_at"] == NOW


def test_nudge_spends_the_key_on_sent_and_dead_but_not_on_defer(rig):
    seed_issue(rig, "i5", status="gating", nudged=[])
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    a = {"act": "nudge", "id": "i5", "nudge_key": "sections", "message": "fix the report"}
    rig.rc_queue.append(3)                             # deferred: retry later, key not spent
    rig.r._execute(a, NOW)
    assert issue_state(rig, "i5")["nudged"] == []
    rig.rc_queue.append(0)                             # delivered: key spent
    rig.r._execute(a, NOW)
    assert issue_state(rig, "i5")["nudged"] == ["sections"]
    seed_issue(rig, "i6", status="gating", nudged=[])
    (rig.home / "state" / "panes" / "i6").write_text("surf-uuid")
    rig.rc_queue.append(4)                             # dead pane: nudge unspendable -> park next
    rig.r._execute({"act": "nudge", "id": "i6", "nudge_key": "sections", "message": "m"}, NOW)
    assert issue_state(rig, "i6")["nudged"] == ["sections"]


def test_codex_nudge_passes_agent_to_pane_classifier(rig):
    rig.r.agent = "codex"
    seed_issue(rig, "i5", status="gating", nudged=[])
    (rig.home / "state" / "panes" / "i5").write_text("surf-uuid")
    rig.r._execute({"act": "nudge", "id": "i5", "nudge_key": "sections", "message": "fix"}, NOW)
    call = rig.calls[-1]
    assert call["args"][0].endswith("nudge-pane.sh")
    assert call["env"]["SL_RUN_ROOT"] == str(rig.home)
    assert call["env"]["SL_AGENT"] == "codex"


def test_merge_executor_full_sequence(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x")
    out = rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555,
                          "method": "squash", "wander": False}, NOW)
    assert out == "ok"
    kinds = [m["kind"] for m in mutations(rig)]
    assert kinds[0] == "merge_pr" and "comment" in kinds and "set_labels" in kinds
    assert issue_state(rig, "i5")["status"] == "merged"
    assert removed and removed[0].endswith("worktrees/i5")


def test_merge_failure_retries_next_tick(rig, monkeypatch):
    seed_issue(rig, "i5", status="gating")
    monkeypatch.setenv("GH_FAIL", "1")
    out = rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555,
                          "method": "squash"}, NOW)
    assert out != "ok"
    assert issue_state(rig, "i5")["status"] == "gating"


def _fail_rule(rig, match, times):
    """Refuse the first `times` gh calls whose argv contains `match` (fake-gh fail rule)."""
    (rig.fixdir / "fail_rules.json").write_text(json.dumps([{"match": match, "times": times}]))


def test_merge_refusal_bumps_the_counter_and_records_a_bounded_reason(rig):
    # Issue #27: a refused merge accumulates a per-issue `merge_refusals` counter and captures the
    # gh stderr in `merge_refusal_reason`, so decide can cap and surface it — the status never
    # advances past the truth (still gating).
    seed_issue(rig, "i5", status="gating")
    _fail_rule(rig, "pr merge", 5)                       # every merge refused; other gh calls fine
    out = rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555, "method": "squash"}, NOW)
    assert out != "ok"
    ist = issue_state(rig, "i5")
    assert ist["status"] == "gating"
    assert ist["merge_refusals"] == 1
    assert isinstance(ist["merge_refusal_reason"], str) and ist["merge_refusal_reason"]
    assert "\n" not in ist["merge_refusal_reason"]
    # a second consecutive refusal accumulates (the cap is reached across ticks, not in one)
    rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555, "method": "squash"}, NOW)
    assert issue_state(rig, "i5")["merge_refusals"] == 2


def test_transient_merge_refusal_then_success_merges_cleanly(rig, monkeypatch):
    # DoD #2: a refusal that clears within the bound still merges — no residual block, status
    # settles to merged. (fake-gh refuses the FIRST merge only.)
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x")
    _fail_rule(rig, "pr merge", 1)
    rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555, "method": "squash"}, NOW)
    assert issue_state(rig, "i5")["merge_refusals"] == 1
    out = rig.r._execute({"act": "merge", "id": "i5", "num": 5, "pr": 555, "method": "squash"}, NOW)
    assert out == "ok"
    assert issue_state(rig, "i5")["status"] == "merged"


def test_reapprove_resets_the_merge_refusal_guard_episode_scoped(rig):
    # DoD #3: the merge-refusal guard is episode-scoped, never forever-latched. A fresh agent-ready
    # zeroes `merge_refusals` (journaling the old cost) and clears the captured reason, so the
    # rebuilt PR's merge is retried from scratch.
    seed_issue(rig, "i5", status="needs_william", merge_refusals=2,
               merge_refusal_reason="failed to merge: required approvals")
    rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)
    ist = issue_state(rig, "i5")
    assert ist["merge_refusals"] == 0
    assert ist["merge_refusal_reason"] is None
    assert ist["status"] == "ready"
    rec = [r for r in journal.read(rig.home) if r.get("act") == "reapprove"][-1]
    assert rec["old_counters"].get("merge_refusals") == 2


def test_update_executor_records_each_outcome(rig, monkeypatch):
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x")
    a = {"act": "update", "id": "i5", "num": 5, "pr": 555, "head_oid": "h1"}

    monkeypatch.setattr(runner_mod.gitops, "merge_update", lambda wt, dev: "clean")
    monkeypatch.setattr(runner_mod.gitops, "plain_push", lambda wt, branch=None: True)
    assert rig.r._execute(a, NOW) == "ok"
    ist = issue_state(rig, "i5")
    assert ist["update_result"] == "clean" and ist["update_head_oid"] == "h1"

    monkeypatch.setattr(runner_mod.gitops, "merge_update", lambda wt, dev: "conflict")
    rig.r._execute(a, NOW)
    assert issue_state(rig, "i5")["update_result"] == "conflict"

    monkeypatch.setattr(runner_mod.gitops, "merge_update", lambda wt, dev: "error")
    rig.r._execute(a, NOW)
    ist = issue_state(rig, "i5")
    assert ist["update_result"] == "error" and ist["update_errors"] == 1

    monkeypatch.setattr(runner_mod.gitops, "merge_update", lambda wt, dev: "clean")
    monkeypatch.setattr(runner_mod.gitops, "plain_push", lambda wt, branch=None: False)
    rig.r._execute(a, NOW)
    ist = issue_state(rig, "i5")
    assert ist["update_result"] == "error" and ist["update_errors"] == 2


def test_update_recheck_failure_sets_the_flag_and_never_pushes(rig, monkeypatch):
    rig.r.config = make_config(ship_recheck_cmd="./recheck.sh")
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x")
    monkeypatch.setattr(runner_mod.gitops, "merge_update", lambda wt, dev: "clean")
    pushed = []
    monkeypatch.setattr(runner_mod.gitops, "plain_push",
                        lambda wt, branch=None: pushed.append(1) or True)
    rig.r._run_cmd = lambda cmd, cwd, timeout=600: 1   # recheck says NO
    out = rig.r._execute({"act": "update", "id": "i5", "num": 5, "pr": 555,
                          "head_oid": "h1"}, NOW)
    assert "recheck" in out
    assert issue_state(rig, "i5")["recheck_failed"] is True
    assert pushed == []                                # never coached past a fail-closed gate


def test_park_executor_labels_comment_and_cleanup(rig):
    seed_issue(rig, "i5", status="blocked")
    (rig.home / "state" / "blocked" / "i5").write_text("q?")
    def m(st):
        st["answerers"] = {"a1": {"for": "i5", "launched_at": NOW}}
    loopstate.update(str(rig.home / "state" / "issues.json"), m)
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5,
                          "needs_william": True, "memo": "answerer escalated"}, NOW)
    assert out == "ok"
    ms = mutations(rig)
    assert any(m["kind"] == "comment" and "answerer escalated" in m["body"] for m in ms)
    lab = [m for m in ms if m["kind"] == "set_labels"][-1]
    assert lab["add"] == "needs-owner" and "in-progress" in lab["remove"]
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["issues"]["i5"]["status"] == "needs_william"
    assert st["answerers"] == {}
    assert not (rig.home / "state" / "blocked" / "i5").exists()


def test_park_stamps_the_notify_marker_before_the_label_move(rig):
    """Issue #61 (b): the durable notify-once marker lands BEFORE gh is asked to move labels, so
    a label write failing in the same dead zone that caused the park cannot re-text — decide
    recognizes the re-derived park as the SAME episode. The memo comment posts once per episode."""
    seed_issue(rig, "i5", status="gating")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue edit", "times": 99, "stderr": "API rate limit exceeded"}]))
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                          "memo": "no PR exists", "cause": "no_pr"}, NOW)
    assert "label move failed" in out
    st = issue_state(rig, "i5")
    assert st["park_notify_cause"] == "no_pr"          # stamped despite the failed write
    assert st["park_notify_at"] == NOW
    assert st["status"] == "gating"                    # never advanced past the truth
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    # the retry tick re-attempts the label but must NOT re-post the memo comment,
    # and must NOT reset the episode clock (the stuck-label alert bound runs from episode start)
    rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                    "memo": "no PR exists", "cause": "no_pr", "retry": True}, NOW + 15)
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    assert issue_state(rig, "i5")["park_notify_at"] == NOW


def test_park_comment_retries_until_it_lands_then_never_reposts(rig):
    """A park whose memo comment ALSO failed (the storm's lockstep shape) retries the comment on
    later ticks until it lands — the memo must reach the issue — but never double-posts."""
    seed_issue(rig, "i5", status="gating")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue comment", "times": 1, "stderr": "rate limited"},
         {"match": "issue edit", "times": 1, "stderr": "rate limited"}]))
    rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                    "memo": "m", "cause": "c"}, NOW)
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                          "memo": "m", "cause": "c", "retry": True}, NOW + 15)
    assert out == "ok"
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    assert issue_state(rig, "i5")["status"] == "parked"


def test_launch_stderr_tail_flows_from_disk_into_the_relaunch_cap_park_memo(rig):
    """End-to-end wiring (issue #40): a launch that died at startup leaves its stderr tail in
    state/launch_stderr/<id>; disk_view must surface it (agent-agnostic) so decide's relaunch-cap
    park memo NAMES the real error, not just the relaunch count."""
    import actions
    seed_issue(rig, "i5", status="running", branch="sl/i5-x", launches=3, retries=2)
    (rig.home / "state" / "exited" / "i5").write_text("%d rc=1\n" % NOW)
    (rig.home / "state" / "launch_stderr" / "i5").write_text(
        "error: unknown option '--effort'\nclaude: run `claude --help` for usage\n")
    d = rig.r.disk_view(NOW)
    assert d["launch_stderr"]["i5"].startswith("error: unknown option")   # runner surfaced the file
    out = actions.decide(NOW, rig.r.config,
                         {"auth_status": "ok", "last_ok_at": NOW, "first_attempt_at": NOW - 60},
                         [], [], [], d,
                         {"stale": False, "consecutive_failures": 0, "closed_nums": set(),
                          "prs": {}, "issue_comments": {}})
    parks = [a for a in out if a["act"] == "park"]
    assert len(parks) == 1 and parks[0]["cause"] == "exited_cap"
    assert "relaunched" in parks[0]["memo"]                    # cap framing preserved
    assert "unknown option '--effort'" in parks[0]["memo"]     # ...now names the real launch error


def test_crash_before_the_park_executor_never_loses_the_text(rig):
    """Crash window (Codex review C1): decide orders notify BEFORE park, so a crash between the
    two executors leaves the suppression marker UNSTAMPED — the next tick re-derives the park and
    re-texts (a duplicate, failing toward the owner), never a silent loss. Simulate the crash by
    executing only the notify half, then re-deciding over the reloaded state."""
    import actions
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x")
    (rig.home / "reports" / "i5.md").write_text("## Tests\n" + "x" * 60)
    d = rig.r.disk_view(NOW)
    g = {"stale": False, "consecutive_failures": 0, "closed_nums": set(),
         "prs": {"i5": {}}, "issue_comments": {}}          # answered-empty -> park verdict
    out = actions.decide(NOW, rig.r.config, {"auth_status": "ok", "last_ok_at": NOW,
                         "first_attempt_at": NOW - 60}, [], [], [], d, g)
    acts = [a["act"] for a in out]
    assert acts.index("notify") < acts.index("park")       # the load-bearing executor order
    rig.r._execute(next(a for a in out if a["act"] == "notify"), NOW)   # ...then crash
    assert issue_state(rig, "i5").get("park_notify_cause") is None     # marker never stamped
    out2 = actions.decide(NOW + 15, rig.r.config, {"auth_status": "ok", "last_ok_at": NOW + 15,
                          "first_attempt_at": NOW - 60}, [], [], [], rig.r.disk_view(NOW + 15), g)
    assert [a for a in out2 if a["act"] == "notify"]       # the text re-fires, never lost


def test_comment_only_failure_still_settles_the_park(rig):
    """Codex review M1 pin: when the memo COMMENT fails but the label move succeeds, the issue
    settles terminal (parked) — decide stops re-deriving, so the issue-thread memo is NOT
    retried further. Accepted: the memo already reached William via the notify text and the
    journal; the comment is best-effort once the park has landed."""
    seed_issue(rig, "i5", status="gating")
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue comment", "times": 1, "stderr": "rate limited"}]))
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                          "memo": "m", "cause": "c"}, NOW)
    assert out == "ok"
    st = issue_state(rig, "i5")
    assert st["status"] == "parked" and st["park_comment_posted"] is False
    assert [m for m in mutations(rig) if m["kind"] == "comment"] == []


def test_a_new_park_cause_is_a_fresh_episode(rig):
    """A park under a DIFFERENT cause re-stamps the marker + clock and posts its own memo."""
    seed_issue(rig, "i5", status="gating", park_notify_cause="old-cause",
               park_notify_at=NOW - 500, park_comment_posted=True)
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                          "memo": "new memo", "cause": "new-cause"}, NOW)
    assert out == "ok"
    st = issue_state(rig, "i5")
    assert st["park_notify_cause"] == "new-cause" and st["park_notify_at"] == NOW
    assert any(m["kind"] == "comment" and "new memo" in m["body"] for m in mutations(rig))


def test_await_pr_read_and_clear_executors(rig):
    """Issue #61 (a): await_pr_read stamps the hold clock ONCE (idempotent — the bound must run
    from episode start) and journals its reason as the outcome; clear_pr_read ends the episode."""
    seed_issue(rig, "i5", status="gating")
    out = rig.r._execute({"act": "await_pr_read", "id": "i5", "num": 5,
                          "reason": "holding: PR lookup refused"}, NOW)
    assert out == "holding: PR lookup refused"
    assert issue_state(rig, "i5")["pr_read_pending_since"] == NOW
    rig.r._execute({"act": "await_pr_read", "id": "i5", "num": 5, "reason": "r"}, NOW + 50)
    assert issue_state(rig, "i5")["pr_read_pending_since"] == NOW
    rig.r._execute({"act": "clear_pr_read", "id": "i5"}, NOW + 60)
    assert issue_state(rig, "i5")["pr_read_pending_since"] is None


def test_clear_park_marker_executor(rig):
    seed_issue(rig, "i5", status="gating", park_notify_cause="no_pr",
               park_notify_at=NOW - 100, park_comment_posted=True)
    out = rig.r._execute({"act": "clear_park_marker", "id": "i5"}, NOW)
    assert out == "ok"
    st = issue_state(rig, "i5")
    assert st["park_notify_cause"] is None and st["park_notify_at"] is None
    assert st["park_comment_posted"] is False


def test_regenerate_executor_hygiene_state_then_gh(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x", conflicts=0,
               update_result="conflict", nudged=["checks"],
               merge_refusals=1, merge_refusal_reason="failed to merge: base moved")
    (rig.home / "reports" / "i5.md").write_text("old report")
    out = rig.r._execute({"act": "regenerate", "id": "i5", "num": 5, "pr": 555,
                          "new_branch": "sl/i5-x-r1", "conflicts": 1, "wander": False}, NOW)
    assert out == "ok"
    assert removed and removed[0].endswith("worktrees/i5")     # M1: stale worktree dies
    assert not (rig.home / "reports" / "i5.md").exists()       # old report can't false-gate
    ist = issue_state(rig, "i5")
    assert ist["status"] == "ready" and ist["branch"] == "sl/i5-x-r1"
    assert ist["conflicts"] == 1 and ist["requeue_front"] is True
    assert ist["update_result"] is None and ist["nudged"] == []
    # the merge-refusal guard is per-PR: the rebuilt PR's merge starts from zero (issue #27)
    assert ist["merge_refusals"] == 0 and ist["merge_refusal_reason"] is None
    kinds = [m["kind"] for m in mutations(rig)]
    assert "pr_add_labels" in kinds and "pr_comment" in kinds and "comment" in kinds
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert lab["add"] == "agent-ready" and lab["remove"] == "in-progress"


def test_resolve_conflict_launches_in_the_prs_own_branch(rig):
    rig.r.tick(now=NOW)
    seed_issue(rig, "i123", status="gating", branch="sl/i123-render-the-widget",
               update_result="conflict")
    # D4: the preserve-path resolver also relaunches into the id's own worktree while the finished-
    # but-alive prior session holds worker.<id>.lock — it must close/free it first (else infinite
    # retry). Seed a stale session + lock to assert the close happens before the relaunch.
    (rig.home / "state" / "panes" / "i123").write_text("PRESERVE-OLD-SURFACE")
    (rig.home / "state" / "worker.i123.lock").write_text("7777")
    rig.calls.clear()
    out = rig.r._execute({"act": "resolve_conflict", "id": "i123", "num": 123, "pr": 555}, NOW)
    assert out == "ok"
    b = (rig.home / "briefs" / "i123.md").read_text()
    assert "conflict" in b.lower() and "sl/i123-render-the-widget" in b
    assert "force" not in [w for w in b.lower().split() if w == "force"] or "never force" in b.lower()
    close_idx = next((i for i, c in enumerate(rig.calls) if "close-surface" in c["args"]), None)
    launch_idx = next((i for i, c in enumerate(rig.calls)
                       if c["args"][0].endswith("launch-session.sh") and c["args"][1] == "i123"), None)
    assert close_idx is not None and launch_idx is not None and close_idx < launch_idx
    assert not (rig.home / "state" / "worker.i123.lock").exists()     # singleton freed for the relaunch
    ist = issue_state(rig, "i123")
    assert ist["status"] == "running" and ist["update_result"] is None


def test_close_investigate_executor(rig):
    seed_issue(rig, "i7", status="gating", type="investigate")
    out = rig.r._execute({"act": "close_investigate", "id": "i7", "num": 7}, NOW)
    assert out == "ok"
    kinds = [m["kind"] for m in mutations(rig)]
    assert "close_issue" in kinds and "set_labels" in kinds
    assert issue_state(rig, "i7")["status"] == "merged"


def test_freeze_unfreeze_alert_and_fix_issue_files(rig):
    rig.r._execute({"act": "freeze", "reason": "dev red", "fingerprint": "fp1"}, NOW)
    frozen = json.loads((rig.home / "state" / "merges_frozen.json").read_text())
    assert frozen["fingerprint"] == "fp1" and frozen["since"] == NOW
    rig.r._execute({"act": "unfreeze"}, NOW)
    assert not (rig.home / "state" / "merges_frozen.json").exists()

    rig.r._execute({"act": "alert", "reasons": ["gh_unreachable"]}, NOW)
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert alert["reasons"] == ["gh_unreachable"]
    rig.r._execute({"act": "clear_alert"}, NOW)
    assert not (rig.home / "state" / "ALERT").exists()

    out = rig.r._execute({"act": "file_fix_issue", "fingerprint": "fp1",
                          "title": "Restore green", "body": "## Goal\nfix",
                          "labels": ["type:diagnose-and-fix", "agent-ready",
                                     "auto-approved:nightly-red", "expedite"]}, NOW)
    assert out == "ok"
    filed = json.loads((rig.home / "state" / "fix_issues.json").read_text())
    assert filed == {"fp1": 9001}
    m = [x for x in mutations(rig) if x["kind"] == "create_issue"][-1]
    assert m["labels"] == "type:diagnose-and-fix,agent-ready,auto-approved:nightly-red,expedite"


def test_absorb_merged_settles_labels_state_and_worktree(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i5", status="gating")
    out = rig.r._execute({"act": "absorb_merged", "id": "i5", "num": 5}, NOW)
    assert out == "ok"
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert lab["remove"] == "in-progress"
    assert issue_state(rig, "i5")["status"] == "merged"
    assert removed and removed[0].endswith("worktrees/i5")


def test_file_fix_issue_reconciles_an_already_filed_issue_from_github(rig):
    # Crash window (Codex round-1 C3): create_issue succeeded, the runner died before saving
    # the fingerprint. On retry the executor must FIND the standing-rule issue on GitHub and
    # record it — never file a duplicate auto-approved issue.
    (rig.fixdir / "issue_list_auto-approved:nightly-red.json").write_text(json.dumps([
        {"number": 7777, "title": "Restore green: x",
         "body": "## Goal\nFailure fingerprint: `fp1` (auto-filed once per distinct breakage).",
         "labels": [{"name": "auto-approved:nightly-red"}], "createdAt": "2026-07-02T00:00:00Z"}]))
    out = rig.r._execute({"act": "file_fix_issue", "fingerprint": "fp1",
                          "title": "Restore green", "body": "## Goal\nfix",
                          "labels": ["type:diagnose-and-fix", "agent-ready",
                                     "auto-approved:nightly-red", "expedite"]}, NOW)
    assert out != "ok" and "already" in out
    assert not any(m["kind"] == "create_issue" for m in mutations(rig))
    filed = json.loads((rig.home / "state" / "fix_issues.json").read_text())
    assert filed == {"fp1": 7777}


def test_file_fix_issue_ignores_a_bare_fingerprint_substring(rig):
    # Codex round-2: reconcile only on the CANONICAL marker, never a coincidental substring
    # (e.g. a log excerpt quoted in some other standing-rule issue's body).
    (rig.fixdir / "issue_list_auto-approved:nightly-red.json").write_text(json.dumps([
        {"number": 7777, "title": "Restore green: other",
         "body": "log excerpt mentions fp1 in passing, not as a fingerprint field",
         "labels": [{"name": "auto-approved:nightly-red"}], "createdAt": "2026-07-02T00:00:00Z"}]))
    out = rig.r._execute({"act": "file_fix_issue", "fingerprint": "fp1",
                          "title": "Restore green", "body": "## Goal\nfix",
                          "labels": ["type:diagnose-and-fix", "agent-ready",
                                     "auto-approved:nightly-red", "expedite"]}, NOW)
    assert out == "ok"                                 # created fresh, no false reconcile
    assert any(m["kind"] == "create_issue" for m in mutations(rig))
    filed = json.loads((rig.home / "state" / "fix_issues.json").read_text())
    assert filed == {"fp1": 9001}


def test_executor_counter_bumps_survive_corrupt_values(rig):
    # Codex round-1 M1: a corrupt persisted counter must not raise inside an executor and
    # wedge the bad state in place — the bump resets it to a real count.
    seed_issue(rig, "i5", status="ready", launch_failures="corrupt")
    rig.r.tick(now=NOW)                                # parsed view for i5? not needed:
    rig.rc_queue.append(2)
    rig.r._parsed_by_id["i5"] = {"num": 5, "id": "i5", "title": "x", "type": "build",
                                 "labels": ["agent-ready"], "touches": [], "blocked_by": [],
                                 "parent": None, "created_at": "", "priority": 2,
                                 "expedite": False}
    rig.r._raw_by_id["i5"] = {"body": ""}
    out = rig.r._execute({"act": "launch", "id": "i5", "num": 5, "branch": "sl/i5-x",
                          "touches": [], "soft_overlap": False, "orphan": False}, NOW)
    assert out != "ok"
    assert issue_state(rig, "i5")["launch_failures"] == 1     # corrupt -> honest restart, no raise


def test_morning_report_stamps_the_date(rig):
    out = rig.r._execute({"act": "morning_report", "date": "2026-07-03"}, NOW)
    assert out == "ok"
    assert (rig.home / "state" / "last_morning_report").read_text().strip() == "2026-07-03"


def test_reclaim_and_relabel_and_gate_and_hold(rig):
    seed_issue(rig, "i8", status="ready")
    assert rig.r._execute({"act": "reclaim", "id": "i8", "num": 8}, NOW) == "ok"
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert lab["add"] == "agent-ready" and lab["remove"] == "in-progress"

    assert rig.r._execute({"act": "relabel", "id": "i8", "num": 8,
                           "add": ["in-progress"], "remove": ["agent-ready"]}, NOW) == "ok"

    seed_issue(rig, "i5", status="running")
    rig.r._execute({"act": "gate", "id": "i5"}, NOW)
    assert issue_state(rig, "i5")["status"] == "gating"
    rig.r._execute({"act": "hold", "id": "i5", "reason": "frozen"}, NOW)
    assert issue_state(rig, "i5")["status"] == "holding"


def test_every_action_is_journaled_with_its_outcome(rig):
    seed_issue(rig, "i5", status="running", branch="sl/i5-x")
    (rig.home / "reports" / "i5.md").write_text("## Tests\n" + "x" * 60)
    rig.r.tick(now=NOW)
    recs = journal.read(rig.home)
    gate_recs = [r for r in recs if r.get("act") == "gate"]
    assert gate_recs and "outcome" in gate_recs[0] and gate_recs[0]["ts"] == NOW


def test_unknown_action_is_journaled_not_fatal(rig):
    assert "no executor" in rig.r._execute({"act": "warp_core_breach"}, NOW)


# --------------------------- re-approval resets a fresh cap (operator finding) -------------------

def test_reapprove_executor_zeroes_counters_rereleases_and_journals_the_old_ones(rig):
    """A fresh agent-ready on a parked issue is a fresh cap: every attempt counter zeroes (INCLUDING
    launches — launch-session.sh derives retries from it, so a non-zero launches would restore the
    retry count on the next launch and re-park at cap), status returns to ready, and the OLD
    counters are journaled so the issue's real prior cost is never lost."""
    seed_issue(rig, "i5", status="parked", launches=3, retries=2, conflicts=1,
               launch_failures=2, answerer_failures=1, requeue_front=True)
    out = rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)
    assert out.startswith("reapproved")
    ist = issue_state(rig, "i5")
    assert ist["status"] == "ready"
    for k in ("launches", "retries", "conflicts", "launch_failures", "answerer_failures"):
        assert ist[k] == 0, f"{k} not reset"
    assert ist["requeue_front"] is False
    # old counters journaled, not lost
    rec = [r for r in journal.read(rig.home) if r.get("act") == "reapprove"][-1]
    assert rec["id"] == "i5"
    assert rec["old_counters"] == {"launches": 3, "retries": 2, "conflicts": 1,
                                   "launch_failures": 2, "answerer_failures": 1}
    # stale park-family label cleared (current needs-owner + legacy needs-william, issue #58)
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert "parked" in lab["remove"] and "needs-owner" in lab["remove"]


def test_reapprove_executor_wipes_stale_markers_and_fields_for_a_clean_launch(rig, monkeypatch):
    """Codex review R1 (blocking): counters alone are not enough — a parked issue's leftover
    finished/in-flight artifacts still drive decide() BEFORE the fresh launch. A leftover report
    re-gates, an `exited` marker double-launches, a `blocked` marker re-enters the answerer flow,
    and a `recheck_failed` field re-parks immediately. Reapprove must present a clean slate."""
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i5", status="needs_william", launch_failures=2, recheck_failed=True,
               update_result="conflict", update_head_oid="deadbeef", nudged=["review"], pr=99)
    (rig.home / "reports" / "i5.md").write_text("## Tests\nstale report")
    for sub in ("blocked", "exited", "awaiting", "started"):
        (rig.home / "state" / sub / "i5").write_text("stale")
    def m(st):
        st["answerers"] = {"a1": {"for": "i5", "launched_at": NOW}}
    loopstate.update(str(rig.home / "state" / "issues.json"), m)

    rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)

    assert removed and removed[0].endswith("worktrees/i5")          # stale worktree removed
    assert not (rig.home / "reports" / "i5.md").exists()            # no report to re-gate
    for sub in ("blocked", "exited", "awaiting", "started"):
        assert not (rig.home / "state" / sub / "i5").exists(), f"{sub} marker survived"
    ist = issue_state(rig, "i5")
    assert ist["recheck_failed"] is False and ist["update_result"] is None
    assert ist["update_head_oid"] is None and ist["nudged"] == [] and ist["pr"] is None
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["answerers"] == {}                                    # active answerer dropped


# ---------------- bounded pending-checks clock executors (issue #26) ----------------

def test_note_checks_pending_stamps_once_and_is_idempotent(rig):
    seed_issue(rig, "i5", status="gating")
    assert rig.r._exec_note_checks_pending({"act": "note_checks_pending", "id": "i5"}, NOW) == "ok"
    assert issue_state(rig, "i5")["checks_pending_since"] == NOW
    # a later tick re-emits it; the stamp must NOT reset, or the bound never elapses
    rig.r._exec_note_checks_pending({"act": "note_checks_pending", "id": "i5"}, NOW + 500)
    assert issue_state(rig, "i5")["checks_pending_since"] == NOW


def test_note_checks_pending_restamps_a_corrupt_or_out_of_range_clock(rig):
    # non-numeric, a FUTURE value (would defeat the cap), and a NEGATIVE one all re-stamp (Codex R1)
    for bad in ("garbage", NOW + 10_000, -5):
        seed_issue(rig, "i5", status="gating", checks_pending_since=bad)
        rig.r._exec_note_checks_pending({"act": "note_checks_pending", "id": "i5"}, NOW)
        assert issue_state(rig, "i5")["checks_pending_since"] == NOW


def test_clear_checks_pending_nulls_the_clock(rig):
    seed_issue(rig, "i5", status="gating", checks_pending_since=NOW)
    assert rig.r._exec_clear_checks_pending({"act": "clear_checks_pending", "id": "i5"},
                                            NOW + 10) == "ok"
    assert issue_state(rig, "i5")["checks_pending_since"] is None


def test_reapprove_clears_a_stale_pending_clock(rig, monkeypatch):
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    seed_issue(rig, "i5", status="needs_william", checks_pending_since=NOW - 99999)
    rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)
    assert issue_state(rig, "i5")["checks_pending_since"] is None    # fresh slate, times from scratch


def test_reapprove_resets_the_pr_read_wait_and_park_notify_marker(rig, monkeypatch):
    """Issue #61: re-approval is a clean slate for both new guards — the PR-lookup hold clock
    times a fresh episode from scratch, and the re-run's own park (if any) texts again."""
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    seed_issue(rig, "i5", status="parked", pr_read_pending_since=NOW - 999,
               park_notify_cause="pr_read_refused", park_notify_at=NOW - 999,
               park_comment_posted=True)
    rig.r._execute({"act": "reapprove", "id": "i5", "num": 5}, NOW)
    st = issue_state(rig, "i5")
    assert st["pr_read_pending_since"] is None
    assert st["park_notify_cause"] is None and st["park_notify_at"] is None
    assert st["park_comment_posted"] is False


def test_park_purges_cached_agent_ready_to_stop_reapprove_churn(rig):
    """Live 2026-07-06: a park removes agent-ready on GitHub but the runner's cached view kept it
    until the next 90s poll, so the next tick saw 'parked + agent-ready' and reapproved the issue
    back — a park->reapprove->relaunch churn loop that defeats the launch cap. The park executor
    must sync the cache. Proven by feeding the post-park cache back through decide: no reapprove."""
    import actions
    rig.r._parsed_by_id = {"i5": {"num": 5, "id": "i5", "type": "build",
                                  "labels": ["agent-ready", "type:build"], "touches": [],
                                  "blocked_by": [], "priority": 2, "expedite": False,
                                  "created_at": "2026-07-01T00:00:05Z"}}
    seed_issue(rig, "i5", status="ready", num=5, launch_failures=2)
    out = rig.r._execute({"act": "park", "id": "i5", "num": 5, "needs_william": False,
                          "memo": "launch never delivered"}, NOW)
    assert out == "ok"
    # the cache no longer advertises agent-ready for i5 (the fix)
    assert "agent-ready" not in rig.r._parsed_by_id["i5"]["labels"]
    # and GitHub really had it removed (existing behavior)
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert "agent-ready" in lab["remove"]
    # end-to-end: decide over the post-park cache does NOT reapprove i5 (loop broken)
    disk = {"issues_state": loopstate.load(str(rig.home / "state" / "issues.json")),
            "blocked": {}, "reports": {}, "answers": {}, "exited": {}, "frozen": None,
            "alert": None, "live_lock_ids": set(), "filed_fingerprints": {},
            "local_date": "2026-07-06", "local_hhmm": "12:00", "last_report_date": "2026-07-06"}
    gh_view = {"stale": False, "consecutive_failures": 0, "closed_nums": set(), "prs": {},
               "issue_comments": {}, "dev_checks": [{"name": "ci", "status": "COMPLETED",
                                                     "conclusion": "SUCCESS"}]}
    acts = actions.decide(NOW, rig.r.config, {"auth_status": "ok", "last_ok_at": NOW,
                          "first_attempt_at": NOW - 60, "five_hour_pct": 10.0, "seven_day_pct": 10.0},
                          list(rig.r._parsed_by_id.values()), [], [], disk, gh_view)
    assert [a for a in acts if a["act"] == "reapprove"] == []


# --------------------------- D7: fail-hard pane preflight -------------------------------

def _fake_run(returncode=0, stdout="", stderr=""):
    return lambda argv: type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})


def test_preflight_fails_with_no_pane():
    ok, why = runner_mod.preflight_pane("", run=_fake_run(0, "  surface:1\n"))
    assert ok is False and "no cmux pane" in why.lower()


def test_preflight_passes_for_a_resolvable_pane():
    ok, why = runner_mod.preflight_pane("pane-uuid",
                                        run=_fake_run(0, "  surface:1  superlooper tab\n"))
    assert ok is True and why == ""


def test_preflight_passes_even_when_a_tab_title_contains_error():
    # Codex review R1 (medium): surface rows carry user-controlled tab titles. A valid pane with a
    # tab named 'Error: build log' must NOT false-fail — success is judged on a real surface ROW.
    out = "  surface:1  Error: build log failed  [selected]\n  surface:2  not found yet\n"
    ok, why = runner_mod.preflight_pane("pane-uuid", run=_fake_run(0, out))
    assert ok is True and why == ""


def test_preflight_fails_on_not_found_even_though_cmux_exits_zero():
    # The D7 exit-code trap: cmux returns 0 for a missing pane. The probe must judge on OUTPUT.
    ok, why = runner_mod.preflight_pane(
        "ghost", run=_fake_run(0, "Error: not_found: Pane or workspace not found\n"))
    assert ok is False and "resolve pane" in why.lower()


def test_preflight_fails_on_a_broken_pipe_detached_start():
    ok, why = runner_mod.preflight_pane(
        "pane-uuid", run=_fake_run(1, "", "Error: Broken pipe (could not connect)\n"))
    assert ok is False and ("broken pipe" in why.lower() or "workspace" in why.lower())


def test_preflight_fails_when_cmux_binary_is_missing():
    def boom(argv):
        raise OSError("No such file or directory")
    ok, why = runner_mod.preflight_pane("pane-uuid", run=boom)
    assert ok is False and "could not run cmux" in why.lower()


# --------------------------- self-pane auto-detection (owner request 2026-07-06) -----------------

_IDENTIFY_JSON = (
    '{"caller": {"surface_id": "S-UUID", "workspace_id": "WS-UUID", '
    '"pane_id": "PANE-UUID", "surface_type": "terminal"}, '
    '"focused": {"pane_id": "OTHER-PANE"}}')


def test_detect_self_pane_returns_the_callers_own_pane():
    # The runner targets the pane of the cmux tab it runs in — cmux `identify` names it as
    # `caller.pane_id` (NOT `focused`, which is whatever tab happens to be focused right now).
    got = runner_mod.detect_self_pane(run=_fake_run(0, _IDENTIFY_JSON))
    assert got == "PANE-UUID"


def test_detect_self_pane_passes_id_format_uuids():
    # Without --id-format uuids, cmux returns pane_id: null — the call MUST request UUIDs.
    seen = {}
    def rec(argv):
        seen["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": _IDENTIFY_JSON, "stderr": ""})
    runner_mod.detect_self_pane(run=rec)
    assert "identify" in seen["argv"] and "uuids" in seen["argv"]


def test_detect_self_pane_empty_when_not_in_cmux():
    # A detached/launchd start can't reach the cmux socket -> "" (caller falls back to $SL_PANE).
    def boom(argv):
        raise OSError("cmux socket unreachable")
    assert runner_mod.detect_self_pane(run=boom) == ""
    assert runner_mod.detect_self_pane(run=_fake_run(1, "", "Broken pipe")) == ""


@pytest.mark.parametrize("stdout", [
    "not json", "", "   ",            # unparseable / empty
    "123", "[]", '"just a string"',   # valid JSON but not an object
    '{"caller": {}}',                 # object but no pane_id
    '{"caller": []}',                 # wrong-typed caller (list, not object)
    '{"caller": {"pane_id": null}}',  # explicit null (the fail-OPEN-on-wrong-TYPED class)
    '{"caller": {"pane_id": 42}}',    # wrong-typed pane_id
    '{"caller": {"pane_id": "  "}}',  # whitespace-only pane_id
])
def test_detect_self_pane_fails_closed_on_wrong_typed_identify(stdout):
    # Never crash, never trust a wrong-typed field — fall closed to "" so the caller reaches the
    # explicit-override / hard-fail path instead (the project's fail-OPEN-on-wrong-TYPED rule).
    assert runner_mod.detect_self_pane(run=_fake_run(0, stdout)) == ""


# --------------------------- self-anchor identity (issue #33: a misplaced runner is visible) -----

# Real cmux `identify.caller` carries window_id too (verified against cmux 2026-07-11); the older
# _IDENTIFY_JSON fixture predates window plumbing, so a runner that lands in the wrong WINDOW — the
# 2026-07-09 focused-window failure mode — must be nameable from the boot line, not just the pane.
_IDENTIFY_FULL = (
    '{"caller": {"surface_id": "S-UUID", "workspace_id": "WS-UUID", "window_id": "WIN-UUID", '
    '"pane_id": "PANE-UUID", "surface_type": "terminal"}, '
    '"focused": {"pane_id": "OTHER-PANE"}}')


def test_detect_self_anchor_returns_pane_workspace_window():
    a = runner_mod.detect_self_anchor(run=_fake_run(0, _IDENTIFY_FULL))
    assert a["pane"] == "PANE-UUID"
    assert a["workspace"] == "WS-UUID"
    assert a["window"] == "WIN-UUID"


def test_detect_self_anchor_reads_caller_not_focused():
    # Same discipline as the pane: anchor identity is the tab we RUN in (caller), never whatever
    # window happens to be focused right now (the focused-window fallback that misplaced a runner).
    a = runner_mod.detect_self_anchor(run=_fake_run(0, _IDENTIFY_FULL))
    assert a["pane"] != "OTHER-PANE"


def test_detect_self_pane_delegates_to_anchor():
    # detect_self_pane is the anchor's pane field — one identify call, one fail-closed code path.
    assert runner_mod.detect_self_pane(run=_fake_run(0, _IDENTIFY_FULL)) == "PANE-UUID"


def test_detect_self_anchor_partial_when_older_cmux_omits_window():
    # A cmux that reports no window_id (older build, or the CLI test stub) still yields pane +
    # workspace; the missing field is "" (resolvable-when-present, never a crash).
    a = runner_mod.detect_self_anchor(run=_fake_run(0, _IDENTIFY_JSON))
    assert a["pane"] == "PANE-UUID" and a["workspace"] == "WS-UUID" and a["window"] == ""


@pytest.mark.parametrize("stdout", [
    "not json", "", "   ", "123", "[]", '"s"',
    '{"caller": {}}', '{"caller": []}',
    '{"caller": {"workspace_id": 42, "window_id": null, "pane_id": "  "}}',
])
def test_detect_self_anchor_fails_closed_to_empty_strings(stdout):
    # Never crash, never trust a wrong-typed field: every anchor field falls closed to "".
    a = runner_mod.detect_self_anchor(run=_fake_run(0, stdout))
    assert a == {"pane": "", "workspace": "", "window": ""}


def test_detect_self_anchor_empty_when_not_in_cmux():
    def boom(argv):
        raise OSError("cmux socket unreachable")
    assert runner_mod.detect_self_anchor(run=boom) == {"pane": "", "workspace": "", "window": ""}


# --------------------------- answerer default model (owner ruling 2026-07-05) --------------------

def test_default_models_are_opus_1m_when_config_omits_them(tmp_path):
    # owner ruling 2026-07-06: latest Opus + 1M context (the [1m] suffix) for both worker+answerer.
    r = runner_mod.Runner(repo="x", config={"repo": "o/r"}, state_home=str(tmp_path), pane="p",
                          run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    assert r._models() == ("opus[1m]", "opus[1m]")


def test_config_still_overrides_either_model(tmp_path):
    r = runner_mod.Runner(repo="x", config={"repo": "o/r", "models": {"answerer": "haiku"}},
                          state_home=str(tmp_path), pane="p",
                          run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    assert r._models() == ("opus[1m]", "haiku")


# --------------------------- D3: finished-issue PR refresh bypasses the 90s poll throttle -------

def test_refresh_finishing_prs_freshens_finished_issues_only(rig, monkeypatch):
    """A worker that finished AND opened its PR inside a 90s poll window must not be gated against
    the stale, pre-PR snapshot and false-parked on 'no PR exists' (the live-dry-run D3). A FINISHED
    issue (report on disk, or gating/holding) gets a FRESH pr_for_branch + comments each tick; a
    still-building issue is left untouched so the refresh stays cheap and bounded."""
    seed_issue(rig, "i7", status="running", branch="sl/i7-x", num=7)   # finished (report below)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    seed_issue(rig, "i8", status="running", branch="sl/i8-y", num=8)   # still building (no report)
    rig.r.gh_view = {"stale": False, "prs": {}, "issue_comments": {}}   # empty cache = the stale window

    looked = []

    def fake_pr_for_branch(branch):
        looked.append(branch)
        found = {"number": 42, "state": "OPEN", "headRefName": branch}
        return runner_mod.gh.PrRead(found if branch == "sl/i7-x" else {}, True)

    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", fake_pr_for_branch)
    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: runner_mod.gh.CommentRead([{"body": "<!-- superlooper-review -->\nAPPROVE"}], True))

    ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    rig.r._refresh_finishing_prs(ist_map)

    assert rig.r.gh_view["prs"]["i7"]["number"] == 42                  # the finished issue is now visible
    assert rig.r.gh_view["prs"]["i7"]["comments"][0]["body"].startswith("<!-- superlooper-review -->")
    assert looked == ["sl/i7-x"]                                       # ONLY the finished issue was looked up
    assert "i8" not in rig.r.gh_view["prs"]                            # a building issue is left alone


def test_refresh_rechecks_an_unreviewed_open_pr_but_never_downgrades(rig, monkeypatch):
    """D6: a cached OPEN PR with NO review evidence yet IS re-fetched every tick (a late
    review-marker comment must reach the gate before it nudges+parks) — but a transient
    gh.pr_for_branch failure (a refused PrRead) must NEVER downgrade the known PR to {},
    which would re-park completed work every tick (the P0 guard the D3 fix bought)."""
    seed_issue(rig, "i7", status="gating", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False,           # cached PR, OPEN, no comments yet = the D6 window
                     "prs": {"i7": {"number": 5, "state": "OPEN", "headRefName": "sl/i7-x"}}}

    called = []
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch",
                        lambda b: called.append(b) or runner_mod.gh.PrRead({}, False))

    ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    rig.r._refresh_finishing_prs(ist_map)

    assert called == ["sl/i7-x"]                                       # unreviewed OPEN PR IS re-checked (D6)
    assert rig.r.gh_view["prs"]["i7"]["number"] == 5                   # but a transient {} never downgrades it


def test_refresh_skips_a_cached_pr_that_already_has_review_evidence(rig, monkeypatch):
    """Boundedness (D6): once the review marker is present the PR is NOT re-fetched — so the
    D6 re-check set is the small transient (finished-but-not-yet-reviewed), not every open PR."""
    seed_issue(rig, "i7", status="gating", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False, "prs": {"i7": {
        "number": 5, "state": "OPEN", "headRefName": "sl/i7-x",
        "comments": [{"body": "<!-- superlooper-review -->\nAPPROVE"}]}}}

    called = []
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch",
                        lambda b: called.append(b) or runner_mod.gh.PrRead({}, True))
    rig.r._refresh_finishing_prs(loopstate.load(str(rig.home / "state" / "issues.json"))["issues"])
    assert called == []                                               # reviewed -> skipped (bounded)


def test_refresh_skips_a_terminal_parked_issue(rig, monkeypatch):
    """Boundedness (cross-review C1): a parked issue still has its report on disk and an OPEN,
    unreviewed PR — but it is DONE being gated, so the D6 re-check must not poll it every tick
    forever. The terminal-status skip keeps the whole refresh set bounded."""
    seed_issue(rig, "i7", status="parked", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False,
                     "prs": {"i7": {"number": 5, "state": "OPEN", "headRefName": "sl/i7-x"}}}

    called = []
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch",
                        lambda b: called.append(b) or runner_mod.gh.PrRead({}, True))
    rig.r._refresh_finishing_prs(loopstate.load(str(rig.home / "state" / "issues.json"))["issues"])
    assert called == []                                               # terminal -> never re-fetched


def test_refresh_does_not_write_empty_when_lookup_finds_nothing(rig, monkeypatch):
    """A finished issue with NO cached PR whose fresh lookup finds nothing — REFUSED or genuinely
    answered-empty (no PR yet: pr create may still be in flight) — leaves the view unwritten. The
    refresh is POSITIVE-FIND only; answered-empty enters the view via the 90s poll, whose snapshot
    is rebuilt from scratch, so a {} here would only race the poll it duplicates."""
    seed_issue(rig, "i9", status="running", branch="sl/i9-z", num=9)
    (rig.home / "reports" / "i9.md").write_text("# done\n## Tests\nok\n")
    for read in (runner_mod.gh.PrRead({}, False), runner_mod.gh.PrRead({}, True)):
        rig.r.gh_view = {"stale": False, "prs": {}}
        monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b, read=read: read)
        ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
        rig.r._refresh_finishing_prs(ist_map)
        assert "i9" not in rig.r.gh_view["prs"]                        # no {} stored; next tick can still find it


# --------- issue #78: the build-path comments attachment obeys the #21/#61 read discipline -------

def test_poll_attaches_comments_only_on_a_clean_read(rig, monkeypatch):
    """The PR LOOKUP can land while the comments endpoint is REFUSED (a partial dead zone). The
    poll must attach comments ONLY on a clean CommentRead — a refused read is OMITTED (the
    'comments' key left ABSENT), so the gate WAITs instead of reading the fail-closed [] as an
    authoritative 'no review marker' and parking a reviewed build (issue #78)."""
    seed_issue(rig, "i123", status="gating", branch="sl/i123-render-the-widget", type="build")
    (rig.home / "reports" / "i123.md").write_text("## Tests\n" + "x" * 60)

    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: runner_mod.gh.CommentRead([], False))    # REFUSED comments read
    rig.r._poll_github(NOW)
    pv = rig.r.gh_view["prs"]["i123"]
    assert pv["number"] == 555                                         # the PR lookup itself landed
    assert "comments" not in pv                                        # ...but the refused read is OMITTED

    # a CLEAN read (even genuinely empty) DOES attach — the answered-empty keeps the nudge ladder.
    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: runner_mod.gh.CommentRead([], True))     # clean answered-empty
    rig.r._last_poll = 0                                               # bypass the 90s poll throttle
    rig.r._poll_github(NOW + 1)
    assert rig.r.gh_view["prs"]["i123"].get("comments") == []


def test_refresh_attaches_comments_only_on_a_clean_read(rig, monkeypatch):
    """The finishing refresh obeys the same read discipline as the poll: a POSITIVE PR find whose
    comments read is REFUSED attaches the PR but leaves the 'comments' key ABSENT, so the gate
    WAITs rather than parking a reviewed build on a fail-closed empty (issue #78). (A refused
    pr_for_branch already never downgrades a cached PR — issue #61.)"""
    seed_issue(rig, "i7", status="gating", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False, "prs": {}, "issue_comments": {}}   # empty cache = the D3 window

    monkeypatch.setattr(runner_mod.gh, "pr_for_branch",
                        lambda b: runner_mod.gh.PrRead(
                            {"number": 42, "state": "OPEN", "headRefName": b}, True))
    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: runner_mod.gh.CommentRead([], False))    # REFUSED comments read

    ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    rig.r._refresh_finishing_prs(ist_map)
    pv = rig.r.gh_view["prs"]["i7"]
    assert pv["number"] == 42 and "comments" not in pv                 # PR visible, refused read omitted

    # once the comments endpoint recovers, a clean read (with the marker) DOES attach.
    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: runner_mod.gh.CommentRead(
                            [{"body": "<!-- superlooper-review -->\nAPPROVE"}], True))
    rig.r._refresh_finishing_prs(ist_map)
    assert rig.r.gh_view["prs"]["i7"]["comments"][0]["body"].startswith("<!-- superlooper-review -->")


def test_tick_refused_comments_holds_then_recovers_and_merges(rig, monkeypatch):
    """End-to-end through REAL ticks (issue #78): on a finished, reviewed build the PR LOOKUP lands
    while the comments endpoint is REFUSED (a partial dead zone — fake-gh fails only `pr view --json
    comments`, never `pr list`). Across ticks the gate WAITs — status stays gating, ZERO nudges,
    ZERO parks, exactly ONE bounded await_comments_read journal record, the review nudge key NEVER
    spent. When the comments read recovers, the marker is seen and the PR MERGES."""
    # align required_checks with the fixture PR's rollup so a recovered gate can actually merge
    rig.r.config = make_config(required_checks=["quality-gate"])
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove", lambda repo, path: True)
    seed_issue(rig, "i123", status="gating", branch="sl/i123-render-the-widget", num=123,
               type="build", declared_touches=[])
    (rig.home / "reports" / "i123.md").write_text("## Tests\n" + "all green, evidence attached " * 4)

    # refuse EVERY comments read; the PR lookup (`pr list ... --state all`) is left clean.
    (rig.fixdir / "fail_rules.json").write_text(
        json.dumps([{"match": "--json comments", "times": 99}]))

    rig.r.tick(now=NOW)                                    # tick 1: refused -> hold + stamp the clock
    rig.r.tick(now=NOW + 5)                                # tick 2: still refused -> bounded, no repeat
    st = issue_state(rig, "i123")
    assert st["status"] == "gating"                        # HELD — never parked, never merged
    assert st.get("nudged") in ([], None)                 # the review nudge key was NOT spent
    assert isinstance(st.get("comments_read_pending_since"), (int, float))   # wait clock stamped
    awaits = [r for r in journal.read(rig.home)
              if r.get("act") == "await_comments_read" and r.get("id") == "i123"]
    assert len(awaits) == 1                                # ONE bounded record across the two ticks
    assert not [r for r in journal.read(rig.home)
                if r.get("act") == "park" and r.get("id") == "i123"]
    assert not [r for r in journal.read(rig.home)
                if r.get("act") == "nudge" and r.get("id") == "i123"]

    # the comments endpoint recovers; the marker (fixture pr_comments.json) is now readable.
    (rig.fixdir / "fail_rules.json").write_text("[]")
    rig.r.tick(now=NOW + 200)                              # >90s: poll + refresh both re-read cleanly
    assert issue_state(rig, "i123")["status"] == "merged"
    assert any(m["kind"] == "merge_pr" for m in mutations(rig))
    assert [r for r in journal.read(rig.home)
            if r.get("act") == "clear_comments_read" and r.get("id") == "i123"]
    assert issue_state(rig, "i123").get("comments_read_pending_since") is None   # episode ended


# --------------------------- D4: relaunch closes the finished-but-alive session first -----------

def test_close_stale_session_frees_the_singleton_lock(rig):
    """D4: before a relaunch (conflict-regenerate / retry), the runner closes the PRIOR session's
    recorded pane (a real claude worker idles at the prompt after finishing, holding
    worker.<id>.lock) and clears the pane markers + stale lock so the fresh start-session can take
    the singleton. Without this, the relaunch's start-session can't acquire the lock, delivery times
    out, and the issue false-parks."""
    (rig.home / "state" / "panes" / "i3").write_text("SURFACE-UUID-3")
    (rig.home / "state" / "panes" / "i3.ws").write_text("WS-UUID-3")
    (rig.home / "state" / "worker.i3.lock").write_text("4242")

    rig.r._close_stale_session("i3")

    closes = [c for c in rig.calls if "close-surface" in c["args"]]
    assert closes, "expected a cmux close-surface call for the stale pane"
    assert "SURFACE-UUID-3" in closes[0]["args"]
    assert "--workspace" in closes[0]["args"] and "WS-UUID-3" in closes[0]["args"]
    assert not (rig.home / "state" / "panes" / "i3").exists()
    assert not (rig.home / "state" / "panes" / "i3.ws").exists()
    assert not (rig.home / "state" / "worker.i3.lock").exists()       # lock freed for the relaunch


def test_close_stale_session_is_a_noop_without_a_recorded_pane(rig):
    """A first launch (no prior pane) must neither attempt a close nor error."""
    rig.r._close_stale_session("i9")
    assert not [c for c in rig.calls if "close-surface" in c["args"]]


def test_launch_closes_a_stale_session_before_relaunching(rig):
    """End-to-end: _exec_launch closes the id's stale pane BEFORE invoking launch-session.sh, and
    clears the stale singleton lock — so a conflict-regenerate / retry of an id whose old session is
    still alive can actually deliver."""
    rig.r.tick(now=NOW)                                  # i101 lands in the parsed view
    (rig.home / "state" / "panes" / "i101").write_text("OLD-SURFACE")
    (rig.home / "state" / "worker.i101.lock").write_text("9999")
    rig.calls.clear()

    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    close_idx = next((i for i, c in enumerate(rig.calls) if "close-surface" in c["args"]), None)
    launch_idx = next((i for i, c in enumerate(rig.calls)
                       if c["args"][0].endswith("launch-session.sh")), None)
    assert close_idx is not None and launch_idx is not None
    assert close_idx < launch_idx                        # close the old session BEFORE relaunching
    assert "OLD-SURFACE" in rig.calls[close_idx]["args"]
    assert not (rig.home / "state" / "worker.i101.lock").exists()     # freed before the new start-session


def test_tick_skips_pr_refresh_when_github_is_stale(rig, monkeypatch):
    """When GitHub is unreachable the view is marked stale; the finishing-PR refresh must NOT run
    (a fail-closed empty lookup would wrongly erase a good cached PR — the gate then simply waits)."""
    seed_issue(rig, "i7", status="gating", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")

    called = []
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch",
                        lambda b: called.append(b) or runner_mod.gh.PrRead({}, False))
    # Force a stale view and neutralize the poll so it stays stale for this tick.
    monkeypatch.setattr(rig.r, "_poll_github", lambda now: None)
    rig.r.gh_view = {"stale": True, "prs": {"i7": {"number": 9, "state": "OPEN"}}}

    rig.r.tick(now=NOW)
    assert called == []                                                # no refresh attempted
    assert rig.r.gh_view["prs"]["i7"]["number"] == 9                   # good cached PR preserved


# --------------------------- D1: gh pinned to config.repo at init ---------------------------

def test_runner_init_pins_gh_to_config_repo(rig, tmp_path, monkeypatch):
    # the env-injection mechanics live in test_gh.py; this pins the WIRING — constructing a
    # Runner (however it is constructed: CLI, launchd, tests) targets gh at config.repo, so a
    # runner started from any cwd can never talk to the wrong repo (live dry-run D1)
    import gh
    monkeypatch.setattr(gh, "_repo", None)
    runner_mod.Runner(repo=str(rig.repo), config=make_config(repo="own/rep"),
                      state_home=str(tmp_path / "d1-home"), pane="p",
                      run_script=lambda *a, **k: 0, fetch_usage=lambda: {})
    assert gh._repo == "own/rep"


# ===================================================================================
# Incident 2026-07-07: a binary file (PNG, .DS_Store) in reports/ wedged every tick.
# Four independent fixes, each pinned here (INCIDENT-2026-07-07-runner-binary-report-wedge.md).
# ===================================================================================

# A stray real PNG header + a real Finder .DS_Store magic — both undecodable as UTF-8.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\xff\xd8\xfe"
_DS_STORE_BYTES = b"\x00\x00\x00\x01Bud1\x00\x00\x10\x00\x00\x00\x08\x00\x00\x00"


# --------------------------- fix 1: the scan tolerates binary files ---------------------------

def test_read_returns_none_for_a_binary_file(rig, tmp_path):
    # _read caught only OSError; UnicodeDecodeError (a ValueError) escaped and killed the tick.
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_BYTES)
    assert runner_mod._read(str(p)) is None          # binary -> absent, never a raise


def test_scan_dir_skips_binaries_and_macos_metadata(rig):
    reports = rig.home / "reports"
    (reports / "i5.md").write_text("## Tests\nall green")
    (reports / "shot.png").write_bytes(_PNG_BYTES)        # binary -> skipped by content (_read None)
    (reports / ".DS_Store").write_bytes(_DS_STORE_BYTES)  # dotfile -> skipped by name...
    (reports / ".hidden").write_text("valid utf-8 junk")  # ...even when it decodes cleanly as text
    out = rig.r._scan_dir("reports")                       # must not raise
    assert out == {"i5": "## Tests\nall green"}            # exactly the real report, nothing else


def test_binary_report_does_not_block_event_detection(rig):
    # The live wedge: a PNG in reports/ made disk_view -> _scan_dir raise, so the FINISHED
    # worker's report was never seen and the gate never ran. A whole tick must survive it.
    seed_issue(rig, "i5", status="gating", branch="sl/i5-widget", type="build")
    (rig.home / "reports" / "i5.md").write_text("## Tests\n" + "x" * 60)
    (rig.home / "reports" / "shot.png").write_bytes(_PNG_BYTES)
    (rig.home / "reports" / ".DS_Store").write_bytes(_DS_STORE_BYTES)
    rig.r.tick(now=NOW)                               # must not raise
    assert "i5" in rig.r.disk_view(NOW)["reports"]    # real report still detected


# --------------------------- fix 2: a crashing tick is no longer silent ---------------------------

def _raising_tick(exc):
    def _tick(now=None):
        raise exc
    return _tick


def test_consecutive_tick_crashes_raise_alert_and_notify_once(rig, tmp_path):
    marker = tmp_path / "pings.txt"
    rig.r.config["notify"]["cmd"] = f'printf "%s\\n" {{title}} >> {marker}'
    rig.r.tick = _raising_tick(
        UnicodeDecodeError("utf-8", _PNG_BYTES, 0, 1, "invalid start byte"))
    rig.r.run(max_ticks=6, sleep=lambda s: None)      # 6 crashes in a row
    alert = json.loads((rig.home / "state" / "ALERT").read_text())
    assert any("tick" in r for r in alert["reasons"]) # ALERT raised for the wedge
    assert marker.read_text().count("\n") == 1        # notify fired EXACTLY once, not per-tick


def test_tick_error_counter_resets_on_a_clean_tick(rig, tmp_path):
    # Never four crashes in a row (3, clean, 3): the counter must reset on the clean tick, so
    # the alert never fires. Without the reset the counter would reach 6 and alert at the 4th.
    marker = tmp_path / "pings.txt"
    rig.r.config["notify"]["cmd"] = f'printf x >> {marker}'
    behaviors = [True, True, True, False, True, True, True]   # True == raise
    def flaky(now=None):
        if behaviors.pop(0):
            raise ValueError("boom")
    rig.r.tick = flaky
    rig.r.run(max_ticks=7, sleep=lambda s: None)
    assert rig.r._consecutive_tick_errors == 3        # the clean tick zeroed it mid-run
    assert not (rig.home / "state" / "ALERT").exists() # never four in a row -> no alert
    assert not marker.exists()                         # ...and no notify


# --------------------------- fix 3: the heartbeat stops lying ---------------------------

def test_heartbeat_stays_stale_when_a_tick_wedges(rig, monkeypatch):
    hb = rig.home / "state" / "runner.heartbeat"
    rig.r.tick(now=NOW)                                # clean tick stamps the heartbeat
    assert hb.read_text().strip() == str(int(NOW))
    # A tick that crashes AFTER the old (top-of-tick) stamp point must NOT refresh the stamp.
    monkeypatch.setattr(rig.r, "disk_view", _raising_tick(RuntimeError("wedged mid-tick")))
    with pytest.raises(RuntimeError):
        rig.r.tick(now=NOW + 15)
    assert hb.read_text().strip() == str(int(NOW))     # still NOW, not NOW+15 — no false "alive"


# --------------------------- fix 4 (bonus): journal error records are bounded ---------------------------

def test_tick_error_record_is_truncated(rig):
    # The PNG's bytes rode along inside UnicodeDecodeError's repr; one bloated the live journal
    # ~47 MB -> 74 MB. The journal record must be a few hundred chars regardless of the payload.
    huge = b"\x00" * 2_000_000
    rig.r.tick = _raising_tick(UnicodeDecodeError("utf-8", huge, 0, 1, "bad"))
    rig.r.run(max_ticks=1, sleep=lambda s: None)
    recs = [r for r in journal.read(str(rig.home)) if r.get("act") == "tick_error"]
    assert recs and all(len(r["error"]) <= 600 for r in recs)


def test_poll_error_record_is_truncated(rig):
    rig.r._poll_github = _raising_tick(ValueError("z" * 2_000_000))
    rig.r.tick(now=NOW)                                # poll crash is caught + journaled in-tick
    recs = [r for r in journal.read(str(rig.home)) if r.get("act") == "poll_error"]
    assert recs and all(len(r["error"]) <= 600 for r in recs)


# --------------- fix 1b (Codex review R1): making _read binary-tolerant must not fail-OPEN safety state ---------------

def test_read_json_fails_closed_for_a_binary_safety_file(rig):
    # _read maps binary -> None, and _read_json reads None as "absent". A binary merges_frozen.json
    # must NOT thereby read as "not frozen": present-but-unreadable is {} (exists), absent is None.
    frozen_path = rig.home / "state" / "merges_frozen.json"
    frozen_path.write_bytes(_PNG_BYTES)                    # a present, undecodable freeze marker
    assert runner_mod._read_json(str(frozen_path)) == {}   # present-but-unreadable -> {}, not None
    frozen = rig.r.disk_view(NOW)["frozen"]
    assert frozen and isinstance(frozen, dict)             # merges stay FROZEN, not silently open


def test_read_json_is_none_only_when_the_file_is_absent(rig, tmp_path):
    assert runner_mod._read_json(str(tmp_path / "nope.json")) is None   # absent -> None (as before)


# --------------- fix 2b (Codex review R1): the wedge ALERT write is retried until it lands ---------------

def test_tick_error_alert_write_retries_until_it_lands(rig, tmp_path, monkeypatch):
    marker = tmp_path / "pings.txt"
    rig.r.config["notify"]["cmd"] = f'printf x >> {marker}'
    rig.r.tick = _raising_tick(ValueError("boom"))
    real_save, calls = runner_mod.loopstate.save, {"alert": 0}
    def flaky_save(path, data):
        if str(path).endswith("ALERT"):
            calls["alert"] += 1
            if calls["alert"] == 1:
                raise OSError("disk hiccup at the threshold tick")     # miss the first attempt
        return real_save(path, data)
    monkeypatch.setattr(runner_mod.loopstate, "save", flaky_save)
    rig.r.run(max_ticks=6, sleep=lambda s: None)
    assert (rig.home / "state" / "ALERT").exists()         # landed on a later crashing tick
    assert marker.read_text() == "x"                        # ...yet notify still fired exactly once


# =========================== issue #21: never strand or wrongly park an investigation ==========
# These drive the REAL tick loop against a stateful fake-gh (a controllable little GitHub) so the
# two proven failure modes are impossible: (a) a refused comment read false-parking a finished
# investigation, and (b) a want-set that grows with merged history starving the tail forever.

_GREEN_CHECK = [{"name": "ci", "status": "completed", "conclusion": "success"}]
_INV_BODY = ("## Goal\nInvestigate.\n\n## Definition of done\n- [ ] root cause\n\n"
             "## Boundaries\nnone\n\n## Loop metadata\ntouches:\n")


def _stateful(rig, issues, next_num=500, prs=None):
    """Flip fake-gh into stateful mode (state.json) so the runner's own reads reflect a
    controllable little GitHub. `issues` maps num-string -> issue dict; `prs` likewise."""
    state = {"issues": issues, "prs": prs or {}, "dev_branch": "main", "check_names": ["ci"],
             "branch_checks": {"main": list(_GREEN_CHECK)}, "next_num": next_num}
    (rig.fixdir / "state.json").write_text(json.dumps(state))


def _refuse_comment_reads(rig, times):
    (rig.fixdir / "fail_rules.json").write_text(
        json.dumps([{"match": "--json comments", "times": times}]))


def gh_calls(rig):
    p = rig.fixdir / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _inv_issue(num, comments):
    return {"number": num, "title": f"Investigate {num}", "state": "open",
            "labels": ["in-progress", "type:investigate"], "body": _INV_BODY, "comments": comments}


def test_investigation_refused_reads_hold_then_recover_and_close(rig):
    # DoD: comment reads refused across N ticks -> gate HOLDS (zero parks, zero notifies), ONE
    # bounded refusal record in the journal; reads recover -> the parent closes cleanly.
    marker = [{"body": "<!-- superlooper-investigation -->\nRoot cause: tenant id omitted."}]
    _stateful(rig, {"7": _inv_issue(7, marker)})
    seed_issue(rig, "i7", status="gating", type="investigate", branch="sl/i7-cache", num=7)
    (rig.home / "reports" / "i7.md").write_text("## Tests\n" + "x" * 60)
    _refuse_comment_reads(rig, times=999)                  # every comment read refused

    N = 4
    for k in range(N):
        rig.r.tick(now=NOW + k * 100)                      # +100 > GH_POLL_SECONDS -> re-polls each tick

    ms = mutations(rig)
    assert [m for m in ms if m["kind"] == "close_issue"] == []          # never closed on a refused read
    assert [m for m in ms if m["kind"] == "set_labels"
            and "parked" in (m.get("add") or "")] == []                 # never parked
    assert issue_state(rig, "i7")["status"] == "gating"                 # still holding at the gate
    assert issue_state(rig, "i7")["read_waited"] is True
    jrnl = journal.read(rig.home)
    assert len([r for r in jrnl if r.get("act") == "await_read"]) == 1  # ONE bounded record, not N
    assert [r for r in jrnl if r.get("act") == "notify"] == []          # zero notifies

    (rig.fixdir / "fail_rules.json").unlink()                           # reads recover
    rig.r.tick(now=NOW + (N + 2) * 100)
    assert [m for m in mutations(rig) if m["kind"] == "close_issue" and m["num"] == "7"]
    assert issue_state(rig, "i7")["status"] == "merged"


def test_answered_empty_investigation_still_nudges_then_parks(rig):
    # DoD: answered-empty (marker genuinely absent) still nudges once then parks — UNCHANGED. A
    # clean read that simply carries no marker is authoritative; only a REFUSED read holds.
    _stateful(rig, {"7": _inv_issue(7, [{"body": "just chatter, no marker"}])})
    seed_issue(rig, "i7", status="gating", type="investigate", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("## Tests\n" + "x" * 60)
    rig.calls.clear()

    rig.r.tick(now=NOW)                                    # clean read, no marker -> nudge once
    assert issue_state(rig, "i7")["nudged"] == ["investigation"]
    assert issue_state(rig, "i7")["status"] == "gating"
    assert issue_state(rig, "i7").get("read_waited") in (None, False)   # a clean read never "waited"

    rig.r.tick(now=NOW + 100)                              # still no marker, already nudged -> park
    assert issue_state(rig, "i7")["status"] == "parked"
    assert [m for m in mutations(rig) if m["kind"] == "set_labels"
            and "parked" in (m.get("add") or "")]


def test_want_set_independent_of_merged_history_no_starvation(rig):
    # DoD: want-set size is independent of merged-history length; a finished investigation sorting
    # LAST behind ~30 merged issues still closes within a tick, and poll calls stay under budget.
    issues = {}
    for n in range(10, 40):                                # i10..i39: long-merged, reports on disk
        iid = f"i{n}"
        seed_issue(rig, iid, status="merged", type="build", branch=f"sl/{iid}-x", num=n)
        (rig.home / "reports" / f"{iid}.md").write_text("## Tests\nmerged long ago " + "x" * 40)
        issues[str(n)] = {"number": n, "title": f"old {n}", "state": "closed", "labels": [],
                          "body": _INV_BODY, "comments": []}
    seed_issue(rig, "i99", status="gating", type="investigate", branch="sl/i99-x", num=99)
    (rig.home / "reports" / "i99.md").write_text("## Tests\n" + "x" * 60)
    issues["99"] = _inv_issue(99, [{"body": "<!-- superlooper-investigation -->\ndone."}])
    _stateful(rig, issues, next_num=200)
    rig.calls.clear()

    rig.r.tick(now=NOW)

    assert issue_state(rig, "i99")["status"] == "merged"               # closed within the one tick
    assert [m for m in mutations(rig) if m["kind"] == "close_issue" and m["num"] == "99"]
    # merged history consumed NO comment-read budget: the ONLY comment read was i99's.
    comment_reads = [c for c in gh_calls(rig) if "comments" in c and "--json" in c]
    assert len(comment_reads) == 1 and "99" in comment_reads[0]
    # the poll's whole fetch walk stayed well under the call budget despite 30 merged issues.
    assert len(gh_calls(rig)) < runner_mod.MAX_POLL_CALLS


def test_starved_investigation_read_is_rescued_budget_exempt(rig):
    # DoD-adjacent: even if the budgeted poll walk misses an investigation's comment read, the
    # budget-exempt rescue picks it up the same tick. Simulate a starved poll by shrinking the
    # budget to below the number of finishing issues, then assert the rescue still closes i99.
    monkey_budget = 6
    issues = {}
    # a wall of finishing INVESTIGATE issues (gating, reports on disk, no marker yet) ahead of our
    # investigation, each costing one comment read, so the fixed budget is exhausted before the
    # investigation's tail read. They HOLD (await_read) rather than parking, keeping the test clean.
    for n in range(10, 40):
        iid = f"i{n}"
        seed_issue(rig, iid, status="gating", type="investigate", branch=f"sl/{iid}-x", num=n)
        (rig.home / "reports" / f"{iid}.md").write_text("## Tests\n" + "x" * 60)
        issues[str(n)] = _inv_issue(n, [])                 # no marker yet -> holds, never parks
    seed_issue(rig, "i99", status="gating", type="investigate", branch="sl/i99-x", num=99)
    (rig.home / "reports" / "i99.md").write_text("## Tests\n" + "x" * 60)
    issues["99"] = _inv_issue(99, [{"body": "<!-- superlooper-investigation -->\ndone."}])
    _stateful(rig, issues, next_num=200)

    import runner as _r
    old = _r.MAX_POLL_CALLS
    try:
        _r.MAX_POLL_CALLS = monkey_budget                 # force the poll to starve the tail
        rig.r.tick(now=NOW)
    finally:
        _r.MAX_POLL_CALLS = old
    # despite the starved poll walk, the rescue fetched i99's marker and the gate closed it.
    assert issue_state(rig, "i99")["status"] == "merged"


# =========================== issue #61: the 2026-07-08 park-notify storm guards ================
# These drive the REAL tick loop against the stateful fake-gh through the exact storm shape:
# hourly GraphQL dead zone -> PR lookups (and label writes) refused for finished builds. Guard
# (a): refused != answered-empty on the PR-lookup path — the build gate HOLDs, journals once,
# parks only at the bound. Guard (b): notify-once per (issue, park-cause) — the label move may
# retry every tick, the TEXTING happens once; a move failing past its bound raises ONE alert.

_BUILD_BODY = ("## Goal\nBuild.\n\n## Definition of done\n- [ ] done\n\n"
               "## Boundaries\nnone\n\n## Loop metadata\ntouches:\n")

_RATE_LIMITED = "GraphQL: API rate limit exceeded"


def _build_issue(num):
    return {"number": num, "title": f"Build {num}", "state": "open",
            "labels": ["in-progress", "type:build"], "body": _BUILD_BODY, "comments": []}


def _green_pr(num, branch, body=""):
    """A gate-green PR: mergeable, required check 'ci' green, review marker present."""
    return {"number": num, "title": f"pr {num}", "body": body, "state": "OPEN",
            "headRefName": branch, "headRefOid": "oid1", "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            "files": [{"path": "x.py"}], "labels": [],
            "comments": [{"body": "<!-- superlooper-review -->\nreviewed, no P0/P1"}]}


def _finished_build(rig, iid="i5", num=5, branch="sl/i5-x"):
    seed_issue(rig, iid, status="gating", type="build", branch=branch, num=num)
    (rig.home / "reports" / f"{iid}.md").write_text("## Tests\n" + "x" * 60)


def _rules(rig, *rules):
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(list(rules)))


def _journal_acts(rig, act):
    return [r for r in journal.read(rig.home) if r.get("act") == act]


def test_build_refused_pr_lookups_hold_then_recover_and_merge(rig):
    # DoD: fake-gh refuses PR reads across N ticks after session_finished -> the build gate HOLDs
    # (zero parks, zero notifies), the journal carries ONE bounded refusal record; reads recover
    # -> clean merge, the wait clock cleared. The PR exists the whole time — exactly the storm's
    # shape, where "GitHub refused" was mistaken for "no PR exists".
    _stateful(rig, {"5": _build_issue(5)}, prs={"55": _green_pr(55, "sl/i5-x", "Closes #5")})
    _finished_build(rig)
    _rules(rig, {"match": "pr list --head", "times": 999, "stderr": _RATE_LIMITED})

    N = 4
    for k in range(N):
        rig.r.tick(now=NOW + k * 100)                  # +100 > GH_POLL_SECONDS -> re-polls each tick

    assert [m for m in mutations(rig)
            if m["kind"] == "set_labels" and "parked" in (m.get("add") or "")] == []
    assert issue_state(rig, "i5")["status"] == "gating"            # safe idle, still holding
    assert _journal_acts(rig, "park") == []                        # zero parks
    assert _journal_acts(rig, "notify") == []                      # zero texts
    assert len(_journal_acts(rig, "await_pr_read")) == 1           # ONE bounded record, not N

    (rig.fixdir / "fail_rules.json").unlink()                      # the dead zone ends
    rig.r.tick(now=NOW + (N + 2) * 100)
    assert [m for m in mutations(rig) if m["kind"] == "merge_pr" and m["num"] == "55"]
    assert issue_state(rig, "i5")["status"] == "merged"
    assert issue_state(rig, "i5")["pr_read_pending_since"] is None
    assert _journal_acts(rig, "notify") == []                      # a clean merge never texts


def test_refused_pr_lookups_past_bound_park_once_even_while_labels_fail(rig):
    # DoD: the bound expires instead -> ONE park + ONE notify, even though the park's own label
    # move keeps failing in the same dead zone (the exact 2026-07-08 shape: 21 texts in 6.5 min).
    import actions
    _stateful(rig, {"5": _build_issue(5)}, prs={"55": _green_pr(55, "sl/i5-x", "Closes #5")})
    _finished_build(rig)
    pr_rule = {"match": "pr list --head", "times": 999, "stderr": _RATE_LIMITED}
    _rules(rig, pr_rule, {"match": "issue edit", "times": 999, "stderr": _RATE_LIMITED})

    rig.r.tick(now=NOW)                                            # stamps the hold clock
    cap = actions.PR_READ_HOLD_CAP_SECONDS
    for k in range(5):                                             # bound expired; storm cadence
        rig.r.tick(now=NOW + cap + 10 + k * 100)

    notifies = _journal_acts(rig, "notify")
    assert len(notifies) == 1                                      # the texting is what must be once
    parks = _journal_acts(rig, "park")
    assert len([r for r in parks if not r.get("retry")]) == 1      # one park episode...
    assert len(parks) >= 2 and all(r.get("retry") for r in parks[1:])   # ...with silent retries
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1  # one memo, not N
    assert issue_state(rig, "i5")["status"] == "gating"            # label never landed: unsettled

    _rules(rig, pr_rule)                                           # label writes recover; reads still dark
    rig.r.tick(now=NOW + cap + 560)
    assert issue_state(rig, "i5")["status"] == "parked"            # the retried move settles it
    assert len(_journal_acts(rig, "notify")) == 1                  # still exactly one text


def test_park_label_failures_notify_once_then_settle(rig):
    # DoD: label moves failing N ticks in a row under a park verdict produce EXACTLY ONE notify;
    # the journal shows one park + silent retries, not N parks. The park here is GENUINE
    # (answered-empty: no PR exists anywhere) — only the WRITE side is failing.
    _stateful(rig, {"5": _build_issue(5)})
    _finished_build(rig)
    _rules(rig, {"match": "issue edit", "times": 3, "stderr": "secondary rate limit"})

    for k in range(4):
        rig.r.tick(now=NOW + k * 100)

    assert len(_journal_acts(rig, "notify")) == 1
    parks = _journal_acts(rig, "park")
    assert len(parks) == 4 and len([r for r in parks if not r.get("retry")]) == 1
    assert len([m for m in mutations(rig) if m["kind"] == "comment"]) == 1
    assert issue_state(rig, "i5")["status"] == "parked"            # writes recovered -> settled


def test_real_park_still_notifies_exactly_once(rig):
    # DoD: a real park (no PR anywhere, label writes succeed) still texts exactly once — unchanged.
    _stateful(rig, {"5": _build_issue(5)})
    _finished_build(rig)
    for k in range(3):
        rig.r.tick(now=NOW + k * 100)
    assert len(_journal_acts(rig, "notify")) == 1
    assert len(_journal_acts(rig, "park")) == 1
    assert issue_state(rig, "i5")["status"] == "parked"


def test_park_label_stuck_past_bound_raises_one_alert(rig):
    # DoD: a label move failing past its bound raises exactly ONE ALERT + notify (incident §4:
    # one more text, not zero and not twenty).
    import actions
    _stateful(rig, {"5": _build_issue(5)})
    _finished_build(rig)
    _rules(rig, {"match": "issue edit", "times": 999, "stderr": _RATE_LIMITED})

    rig.r.tick(now=NOW)                                            # park + its one text
    stuck = actions.PARK_LABEL_STUCK_ALERT_SECONDS
    rig.r.tick(now=NOW + stuck + 5)                                # past the bound -> ALERT
    rig.r.tick(now=NOW + stuck + 105)                              # same reasons -> deduped

    alerts = _journal_acts(rig, "alert")
    assert len(alerts) == 1 and any("park_label_stuck:i5" in str(r.get("reasons")) for r in alerts)
    assert len(_journal_acts(rig, "notify")) == 2                  # one park text + one ALERT text
    assert (rig.home / "state" / "ALERT").exists()


def test_park_episode_recovers_and_merges_with_no_further_notify(rig):
    # DoD: the PR becomes visible after k failing ticks (the park label never landed) -> the gate
    # verdict flips to merge, the notify-once marker and hold clock are CLEARED, no further text.
    import actions
    _stateful(rig, {"5": _build_issue(5)}, prs={"55": _green_pr(55, "sl/i5-x", "Closes #5")})
    _finished_build(rig)
    _rules(rig, {"match": "pr list --head", "times": 999, "stderr": _RATE_LIMITED},
           {"match": "issue edit", "times": 999, "stderr": _RATE_LIMITED})

    rig.r.tick(now=NOW)                                            # hold stamped
    cap = actions.PR_READ_HOLD_CAP_SECONDS
    rig.r.tick(now=NOW + cap + 10)                                 # bound expired -> park (1 text)
    rig.r.tick(now=NOW + cap + 110)                                # label still failing -> silent
    (rig.fixdir / "fail_rules.json").unlink()                      # reads AND writes recover
    rig.r.tick(now=NOW + cap + 210)                                # PR visible -> clean merge

    st = issue_state(rig, "i5")
    assert st["status"] == "merged"
    assert st["park_notify_cause"] is None and st["pr_read_pending_since"] is None
    assert len(_journal_acts(rig, "notify")) == 1                  # the park's one text, no more
    assert [m for m in mutations(rig) if m["kind"] == "merge_pr" and m["num"] == "55"]


# ============================ long-run growth bounds (issue #41) ============================
#
# The tick shell must keep three append-forever stores from degrading status/report/restart with
# age — always by ARCHIVING, never deleting. The selection logic is pure and unit-tested
# (test_journal / test_events / test_tidy); these pin that the SHELL wires it in correctly.

_evmod = runner_mod.events_mod


# ---- journal rotation ----

def test_tick_rotates_the_journal_archiving_the_stale_tail(rig):
    # A pre-existing ancient record is archived on the first tick (rotation runs then); the hot
    # journal — what status/report read — keeps only the recent window, and read_all still finds the
    # archived record (nothing deleted).
    journal.append(rig.home, {"act": "ancient_marker"}, now=NOW - 30 * 24 * 3600)
    rig.r.tick(now=NOW)
    assert "ancient_marker" not in [r.get("act") for r in journal.read(rig.home)]
    assert (rig.home / journal.ARCHIVE_FILENAME).exists()
    assert any(r.get("act") == "ancient_marker" for r in journal.read_all(rig.home))


def test_journal_rotation_is_throttled_between_ticks(rig):
    # After the first tick rotates and arms the throttle, a record that goes stale is NOT archived on
    # the very next tick — only once JOURNAL_ROTATE_SECONDS has elapsed.
    rig.r.tick(now=NOW)
    journal.append(rig.home, {"act": "later_ancient"}, now=NOW - 30 * 24 * 3600)
    rig.r.tick(now=NOW + 15)                                       # throttled -> still hot
    assert any(r.get("act") == "later_ancient" for r in journal.read(rig.home))
    rig.r.tick(now=NOW + runner_mod.JOURNAL_ROTATE_SECONDS + 15)   # interval elapsed -> archived
    assert all(r.get("act") != "later_ancient" for r in journal.read(rig.home))


# ---- processed-events bound ----

def _seed_processed(rig, seqs):
    pdir = rig.home / "state" / "events" / "processed"
    for s in seqs:
        (pdir / f"{s}.json").write_text("{}")


def test_prune_processed_events_bounds_the_dir_and_archives_the_rest(rig):
    n = _evmod.PROCESSED_CAP + 200
    _seed_processed(rig, range(1, n + 1))
    rig.r._prune_processed_events()
    pdir = rig.home / "state" / "events" / "processed"
    adir = rig.home / "state" / "events" / "processed_archive"
    kept = sorted(int(p.stem) for p in pdir.glob("*.json"))
    archived = sorted(int(p.stem) for p in adir.glob("*.json"))
    assert len(kept) == _evmod.PROCESSED_KEEP
    assert kept[-1] == n                                          # newest (global-max seq) kept
    assert archived == list(range(1, n - _evmod.PROCESSED_KEEP + 1))   # oldest archived
    assert len(kept) + len(archived) == n                        # nothing deleted


def test_pruned_processed_keeps_next_seq_correct_and_history_independent(rig):
    n = _evmod.PROCESSED_CAP + 200
    _seed_processed(rig, range(1, n + 1))
    rig.r._prune_processed_events()
    names = os.listdir(rig.home / "state" / "events" / "processed")
    assert len(names) == _evmod.PROCESSED_KEEP                    # scan cost bounded, not O(history)
    assert _evmod.next_seq(names) == n + 1                        # still the true next seq


def test_tick_prunes_processed_after_archiving_an_event(rig):
    # Wiring: a finished session writes one event (moved to processed/); with processed/ already at
    # the cap, that tick prunes it back down and archives the overflow.
    _seed_processed(rig, range(1, _evmod.PROCESSED_CAP + 1))      # exactly at the cap
    seed_issue(rig, "i5", status="running", branch="sl/i5-x")
    (rig.home / "reports" / "i5.md").write_text("## Tests\n" + "x" * 60)   # -> session_finished
    rig.r.tick(now=NOW)
    pdir = rig.home / "state" / "events" / "processed"
    adir = rig.home / "state" / "events" / "processed_archive"
    assert len(list(pdir.glob("*.json"))) == _evmod.PROCESSED_KEEP
    assert adir.is_dir() and len(list(adir.glob("*.json"))) >= 1  # overflow archived, not deleted


# ---- worktree reclaim ----

def _mk_worktree(rig, iid):
    d = rig.home / "worktrees" / iid
    d.mkdir(parents=True, exist_ok=True)
    (d / "wip").write_text("uncommitted")


def test_reclaim_removes_parked_worktree_not_the_running_lane(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    _mk_worktree(rig, "i5")
    _mk_worktree(rig, "i6")
    seed_issue(rig, "i5", status="parked", branch="sl/i5-x")
    seed_issue(rig, "i6", status="running", branch="sl/i6-y")
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    rig.r._reclaim_terminal_worktrees(st)
    assert removed == [str(rig.r._worktree("i5"))]               # parked reclaimed, live lane untouched


def test_reclaim_respects_the_config_gate(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    rig.r.config = make_config(cleanup_parked_worktrees=False)
    _mk_worktree(rig, "i5")
    seed_issue(rig, "i5", status="parked", branch="sl/i5-x")
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    rig.r._reclaim_terminal_worktrees(st)
    assert removed == []                                          # gate off -> kept for inspection


def test_tick_reclaims_a_parked_worktree(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    _mk_worktree(rig, "i5")
    seed_issue(rig, "i5", status="parked", branch="sl/i5-x")
    rig.r.tick(now=NOW)
    assert str(rig.r._worktree("i5")) in removed


def test_reclaim_early_out_survives_an_unhashable_status(rig, monkeypatch):
    # A corrupt issues.json with a wrong-typed (unhashable) status must NOT raise `unhashable type`
    # on the reclaim early-out's `in REAPPROVAL_STATUSES` membership test — an unguarded raise there
    # is unhandled and would wedge the tick. Fail closed: skip the corrupt entry, never raise, and
    # still reclaim a genuinely parked sibling.
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    _mk_worktree(rig, "i9")
    st = {"issues": {"i1": {"status": []}, "i2": {"status": {}}, "i9": {"status": "parked"}}}
    rig.r._reclaim_terminal_worktrees(st)              # must not raise on the [] / {} statuses
    assert removed == [str(rig.r._worktree("i9"))]     # corrupt skipped, valid parked still reclaimed


# ============ the one launch-eligibility gate, end to end (issue #150 / D8) ============
# Fixture i103 declares `blocked-by: #101, #102` in its Loop metadata and neither is closed, so no
# session may start for it BY ANY PATH. These drive the real Runner (real decide, real executors,
# recording run_script) and assert on launch-session.sh itself — the thing that starts a session —
# rather than on decide's action list.

def _blocked_and_exited(rig, now):
    """Poll i103 into the view, then strand it: an in-flight session that died, blockers still open."""
    rig.r.tick(now=now)                                # i101/i102 launch; i103 is blocked, so waits
    (rig.home / "state" / "exited" / "i103").write_text("1751000000 rc=1\n")
    seed_issue(rig, "i103", status="running", branch="sl/i103-wire-widget-to-api")
    rig.calls.clear()


def _launches_of(rig, iid):
    return [c for c in rig.calls
            if c["args"][0].endswith("launch-session.sh") and iid in c["args"]]


def test_crash_recovery_never_launches_a_session_past_an_open_blocker(rig):
    _blocked_and_exited(rig, NOW)
    rig.r.tick(now=NOW + 60)
    assert _launches_of(rig, "i103") == []              # THE bug: this used to relaunch regardless
    ist = issue_state(rig, "i103")
    assert ist["status"] == "running"                   # held in place: not parked, not requeued
    assert "#101" in ist["launch_hold_reason"] and "#102" in ist["launch_hold_reason"]
    held = [j for j in _journal(rig) if j.get("act") == "launch_hold" and j.get("id") == "i103"]
    assert len(held) == 1 and "blocked-by" in held[0]["outcome"]     # the hold says WHY


def test_a_held_restart_relaunches_the_moment_its_blockers_close(rig):
    # The other half: the gate is a WAIT, not a verdict. Close #101/#102 in the fixture world and
    # the same stranded session recovers on the next tick, with no re-approval by William.
    _blocked_and_exited(rig, NOW)
    rig.r.tick(now=NOW + 60)
    assert _launches_of(rig, "i103") == []
    (rig.fixdir / "issue_list_closed.json").write_text(
        json.dumps([{"number": 41}, {"number": 52}, {"number": 101}, {"number": 102}]))
    rig.calls.clear()
    rig.r.tick(now=NOW + 200)                          # >GH_POLL_SECONDS: the new closed set lands
    assert len(_launches_of(rig, "i103")) == 1         # recovered, unblocked, no owner touch
    assert issue_state(rig, "i103")["launch_hold_reason"] is None    # the hold episode is over


def test_a_standing_hold_does_not_re_journal_every_tick(rig):
    # A 15s tick against a long-open blocker must not walk the journal.
    _blocked_and_exited(rig, NOW)
    for i in range(4):
        rig.r.tick(now=NOW + 60 + i * 20)
    held = [j for j in _journal(rig) if j.get("act") == "launch_hold" and j.get("id") == "i103"]
    assert len(held) == 1
    assert _launches_of(rig, "i103") == []


def test_a_github_outage_does_not_strand_a_crash_recovery(rig, monkeypatch):
    # The gate reads eligibility from the POLLED view, so the obvious way to break the loop with it
    # is a GitHub blip that reads as "the issue is gone" and strands every recovery behind it. It
    # can't: a failed poll returns before touching the parsed view, so the last good parse AND
    # closed set survive, and an unblocked issue still recovers mid-outage — off exactly the data a
    # fresh launch would have used.
    rig.r.tick(now=NOW)                                # one good poll: i101 lands in the view
    (rig.home / "state" / "exited" / "i101").write_text("1751000000 rc=1\n")
    seed_issue(rig, "i101", status="running", branch="sl/i101-render-the-widget")
    monkeypatch.setenv("GH_FAIL", "1")                 # GitHub goes dark
    rig.calls.clear()
    rig.r.tick(now=NOW + 200)
    assert len(_launches_of(rig, "i101")) == 1         # recovered anyway
    assert issue_state(rig, "i101").get("launch_hold_reason") is None
