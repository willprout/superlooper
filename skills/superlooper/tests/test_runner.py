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
    return type("Rig", (), {"r": r, "calls": calls, "rc_queue": rc_queue,
                            "home": home, "repo": repo, "fixdir": fixdir})


def mutations(rig):
    p = rig.fixdir / "mutations.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def issue_state(rig, iid):
    return loopstate.load(str(rig.home / "state" / "issues.json"))["issues"].get(iid)


def seed_issue(rig, iid, **fields):
    def m(st):
        st["issues"].setdefault(iid, loopstate.new_issue()).update(fields)
    loopstate.update(str(rig.home / "state" / "issues.json"), m)


# --------------------------- layout / singleton / heartbeat ---------------------------

def test_init_creates_the_c3_layout(rig):
    for sub in ("state/activity", "state/blocked", "state/exited", "state/awaiting",
                "state/panes", "state/started", "state/events/processed",
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


def test_launch_env_carries_explicit_codex_agent_without_changing_protocol(rig):
    # Runner-level selection only: the same launch action / labels / issue selection path is used,
    # but launch-session.sh receives SL_AGENT=codex and currently rejects it as unsupported.
    rig.r.agent = "codex"
    rig.r.tick(now=NOW)
    rig.calls.clear()
    out = rig.r._execute(_launch_action(), NOW)
    assert out == "ok"
    call = rig.calls[-1]
    assert call["args"][0].endswith("launch-session.sh") and call["args"][1] == "i101"
    assert call["env"]["SL_AGENT"] == "codex"


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
    # gh.issue_comments fails CLOSED to [] on any gh error, so a fetch failure is indistinguishable
    # from a comment-less issue — either way the brief is complete and the launch proceeds (never
    # park a fully-approved issue over a supplementary channel). The journal records fetched=0.
    rig.r.tick(now=NOW)
    monkeypatch.setattr(runner_mod.gh, "issue_comments", lambda num: [])
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
    assert ms[-1]["kind"] == "set_labels" and ms[-1]["add"] == "needs-william" \
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
    assert lab["add"] == "needs-william" and "in-progress" in lab["remove"]
    st = loopstate.load(str(rig.home / "state" / "issues.json"))
    assert st["issues"]["i5"]["status"] == "needs_william"
    assert st["answerers"] == {}
    assert not (rig.home / "state" / "blocked" / "i5").exists()


def test_regenerate_executor_hygiene_state_then_gh(rig, monkeypatch):
    removed = []
    monkeypatch.setattr(runner_mod.gitops, "worktree_remove",
                        lambda repo, path: removed.append(str(path)) or True)
    seed_issue(rig, "i5", status="gating", branch="sl/i5-x", conflicts=0,
               update_result="conflict", nudged=["checks"])
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
    # stale park-family label cleared
    lab = [m for m in mutations(rig) if m["kind"] == "set_labels"][-1]
    assert "parked" in lab["remove"] and "needs-william" in lab["remove"]


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
        return {"number": 42, "state": "OPEN", "headRefName": branch} if branch == "sl/i7-x" else {}

    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", fake_pr_for_branch)
    monkeypatch.setattr(runner_mod.gh, "pr_comments",
                        lambda n: [{"body": "<!-- superlooper-review -->\nAPPROVE"}])

    ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    rig.r._refresh_finishing_prs(ist_map)

    assert rig.r.gh_view["prs"]["i7"]["number"] == 42                  # the finished issue is now visible
    assert rig.r.gh_view["prs"]["i7"]["comments"][0]["body"].startswith("<!-- superlooper-review -->")
    assert looked == ["sl/i7-x"]                                       # ONLY the finished issue was looked up
    assert "i8" not in rig.r.gh_view["prs"]                            # a building issue is left alone


def test_refresh_rechecks_an_unreviewed_open_pr_but_never_downgrades(rig, monkeypatch):
    """D6: a cached OPEN PR with NO review evidence yet IS re-fetched every tick (a late
    review-marker comment must reach the gate before it nudges+parks) — but a transient
    gh.pr_for_branch failure (fails closed to {}) must NEVER downgrade the known PR to {},
    which would re-park completed work every tick (the P0 guard the D3 fix bought)."""
    seed_issue(rig, "i7", status="gating", branch="sl/i7-x", num=7)
    (rig.home / "reports" / "i7.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False,           # cached PR, OPEN, no comments yet = the D6 window
                     "prs": {"i7": {"number": 5, "state": "OPEN", "headRefName": "sl/i7-x"}}}

    called = []
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b: called.append(b) or {})

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
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b: called.append(b) or {})
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
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b: called.append(b) or {})
    rig.r._refresh_finishing_prs(loopstate.load(str(rig.home / "state" / "issues.json"))["issues"])
    assert called == []                                               # terminal -> never re-fetched


def test_refresh_does_not_write_empty_when_lookup_finds_nothing(rig, monkeypatch):
    """A finished issue with NO cached PR whose fresh lookup returns {} (transient, or genuinely no
    PR yet) leaves the view unwritten — we never store a {} entry, so a later successful tick can
    still catch the PR."""
    seed_issue(rig, "i9", status="running", branch="sl/i9-z", num=9)
    (rig.home / "reports" / "i9.md").write_text("# done\n## Tests\nok\n")
    rig.r.gh_view = {"stale": False, "prs": {}}
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b: {})

    ist_map = loopstate.load(str(rig.home / "state" / "issues.json"))["issues"]
    rig.r._refresh_finishing_prs(ist_map)
    assert "i9" not in rig.r.gh_view["prs"]                            # no {} stored; next tick can still find it


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
    monkeypatch.setattr(runner_mod.gh, "pr_for_branch", lambda b: called.append(b) or {})
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
