"""Owner texts are headlines written to fit; the runbooks live where a keyboard is (issue #490).

#493 gave every owner text one doorway and a cap, and cut an over-long sender to fit — journaling the
cut as the interim. This issue removes the cut at its source: every sender writes a one-clause
headline (and at most one ask line) inside the per-part budgets the doorway publishes, and the
per-reason remedy paragraphs the phone used to carry move to `superlooper doctor`, the morning-report
file and — where one renders alert reasons — the dashboard.

What is pinned here:
  * the part budgets add up to the cap, for the longest identity and URL the budgets name;
  * every ALERT reason — static, dynamic, every auth-death variant, the runner's own — has a
    one-clause headline beside its remedy, and a multi-reason alert is one line of plain words;
  * THE FULL SURFACE: every sender's message, rendered for that worst-case identity, arrives whole —
    decide's alert/recovery/park/bounce/question/freeze acts, all eight watchdog texts, the doctor's
    test send, the morning report, and the CLI's nightly/promotion/morning sends; a source scan keeps
    a new sender from slipping past the list (the runner's own sends are driven in test_runner.py,
    whose rig this module cannot share);
  * `superlooper doctor` prints the remedy for each standing reason, and nothing for none.
"""
import ast
import json
import time
from pathlib import Path

import pytest

import actions
import notify
import report
import stack_doctor
import watchdog as wd
from test_actions import NOW, cfg, decide, disk, ghv, ist, only, parsed
from test_cli import _stack_env, _write_junit, cli, rig  # noqa: F401  (rig is a fixture)

_SKILL = Path(__file__).resolve().parent.parent / "skill"

# The longest identity and URL the doorway's budgets are written for: a 24-byte repo name and a
# 24-byte machine label, and a 16-byte owner with a six-digit issue number. A sender that fits for
# these fits for every shorter one.
WORST_REPO = "o" * 16 + "/" + "r" * 24
WORST = {"repo": WORST_REPO, "notify": {"machine_label": "m" * 24}}
WORST_URL = "https://github.com/%s/issues/999999" % WORST_REPO


def _whole(tier, headline, ask=None, url=None, caller="test"):
    """Render one message for the worst-case identity and assert the doorway cut nothing."""
    t = notify.render(WORST, tier, headline, ask=ask, url=WORST_URL if url else None, caller=caller)
    assert not t.truncated, (caller, t.full_bytes, t.lines)
    assert t.lines[0].endswith(" · " + " ".join(str(headline).split())), (caller, t.lines)
    return t


def _within_parts(headline, ask, caller):
    assert isinstance(headline, str) and headline.strip() and "\n" not in headline, caller
    assert len(headline.encode("utf-8")) <= notify.HEADLINE_MAX_BYTES, (caller, headline)
    if ask is not None:
        assert "\n" not in ask and len(ask.encode("utf-8")) <= notify.ASK_MAX_BYTES, (caller, ask)


# ================================ the budgets ================================

def test_the_part_budgets_add_up_to_the_cap():
    parts = (notify.IDENTITY_MAX_BYTES + notify.HEADLINE_MAX_BYTES + 1 + notify.ASK_MAX_BYTES + 1
             + notify.URL_MAX_BYTES)
    assert parts <= notify.TEXT_MAX_BYTES
    for tier, emoji in notify.TIER_EMOJI.items():
        ident = notify.render(WORST, tier, "x").lines[0][:-1]
        assert len(ident.encode("utf-8")) <= notify.IDENTITY_MAX_BYTES, emoji
    assert len(WORST_URL.encode("utf-8")) <= notify.URL_MAX_BYTES
    # ...and a message at every budget at once arrives whole
    _whole(notify.WAITING, "h" * notify.HEADLINE_MAX_BYTES, "a" * notify.ASK_MAX_BYTES, url=True)


def test_clauses_names_what_fits_and_counts_the_rest():
    assert notify.clauses(["a", "b"]) == "a, b"
    assert notify.clauses(["a", "a", "", "b"]) == "a, b"                  # deduped, blanks dropped
    assert notify.clauses([]) == ""
    many = ["reason number %02d" % i for i in range(20)]
    s = notify.clauses(many, budget=60)
    assert len(s.encode("utf-8")) <= 60 and s.endswith(" more") and s.startswith("reason number 00")
    shown = s.split(" +")[0].split(", ")
    assert int(s.rsplit("+", 1)[1].split()[0]) == len(many) - len(shown)  # the count is honest
    # one clause longer than the whole budget is clipped, never dropped...
    s1 = notify.clauses(["x" * 100, "y"], budget=40)
    assert len(s1.encode("utf-8")) <= 40 and s1.startswith("x") and s1.endswith("+1 more")
    # ...unless the caller would rather say something whole; and no budget is ever exceeded
    assert notify.clauses(["x" * 100, "y"], budget=40, clip_first=False) == ""
    for budget in range(0, 12):
        assert len(notify.clauses(["x" * 100, "y"], budget=budget).encode("utf-8")) <= budget


# ============================ ALERT headlines and remedies ============================

VARIANTS = sorted(k for k in actions.AUTH_DEATH_REMEDIES if k is not None)
DYNAMIC = (["session_at_dialog:i12345", "session_logged_out:i12345", "park_label_stuck:i12345",
            "launch_runaway:i12345", "update_errors:i12345", "runner_tick_errors:123456",
            "migration_hold:create:awaiting-answer", actions.ALERT_UNREADABLE]
           + ["session_logged_out:i12345:%s" % v for v in VARIANTS])
EVERY_REASON = sorted(actions.ALERT_REMEDIES) + DYNAMIC


def test_every_static_reason_has_a_headline_beside_its_remedy():
    assert set(actions.ALERT_HEADLINES) == set(actions.ALERT_REMEDIES)
    assert set(actions.LAUNCH_ALERT_REASONS.values()) <= set(actions.ALERT_HEADLINES)
    assert {actions.AUTH_DEATH_ALERT_REASON, "usage_stale", "launch_anchor_down",
            "launch_systemic_failure", "auth_dead"} <= set(actions.ALERT_HEADLINES)


@pytest.mark.parametrize("reason", EVERY_REASON)
def test_each_reason_is_one_clause_that_fits_with_a_real_remedy(reason):
    h = actions.alert_headline(reason)
    _within_parts(h, None, reason)
    assert ";" not in h and ". " not in h and not h.endswith(".")          # one clause, no runbook
    assert h != reason                                                     # plain words, not a code
    remedy = actions.alert_remedy(reason)
    assert isinstance(remedy, str) and len(remedy) > 2 * len(h) and remedy != reason
    _whole(notify.DOWN, h, actions.ALERT_ASK, caller=reason)


def test_the_auth_variants_name_themselves_in_the_headline():
    # the remedy differs per banner, so the headline must too: an owner whose API key was fixed
    # and whose session fell back to a dead login is told a DIFFERENT thing broke
    heads = {actions.alert_headline("session_logged_out:i5:%s" % v) for v in VARIANTS}
    assert len(heads) == len(VARIANTS)
    assert actions.alert_headline("session_logged_out:i5") not in heads
    assert all(h.startswith("i5 ") for h in heads)


def test_a_multi_reason_alert_is_one_line_of_plain_words_never_joined_remedies():
    line = actions.alert_headlines(["gh_auth_dead_runner", "usage_stale"])
    assert line == "%s, %s" % (actions.ALERT_HEADLINES["gh_auth_dead_runner"],
                               actions.ALERT_HEADLINES["usage_stale"])
    for r in ("gh_auth_dead_runner", "usage_stale"):
        assert actions.alert_remedy(r) not in line
    everything = actions.alert_headlines(EVERY_REASON)
    _within_parts(everything, None, "all reasons")
    assert everything.endswith(" more")                                    # names what fits, counts the rest
    assert actions.alert_headlines([]) == ""


def test_a_green_never_cuts_a_still_standing_headline_mid_word():
    for standing in (["fence_down", "gh_unreachable"],
                     ["claude_auth_dead_machine", "usage_stale"],
                     ["session_logged_out:i12345:cloud_credentials", "auth_dead"]):
        ask = actions._still_down(sorted(standing))
        assert "…" not in ask, ask
        assert len(ask.encode("utf-8")) <= notify.ASK_MAX_BYTES
        assert ask.startswith("still down: ") or ask == "2 reason(s) still down"
    assert actions._still_down([]) is None


def test_an_alert_about_one_lane_points_at_its_issue():
    assert actions.alert_issue_num(["session_logged_out:i5:login"]) == 5
    assert actions.alert_issue_num(["park_label_stuck:i7", "usage_stale"]) == 7
    assert actions.alert_issue_num(["park_label_stuck:i7", "session_at_dialog:i8"]) is None
    assert actions.alert_issue_num(["usage_stale", "runner_tick_errors:4"]) is None
    assert actions.alert_issue_num(["park_label_stuck:dx", 7, None]) is None
    out = decide(dsk=disk(issues_state={"version": 1, "issues": {
        "i5": ist("running", sensed_state="logged_out", sensed_auth="login")}}))
    (a,) = [x for x in only(out, "notify") if x["caller"] == "decide:alert"]
    assert a["url"] == "https://github.com/o/r/issues/5"


def test_a_remedy_for_an_unknown_reason_is_its_code_never_nothing():
    assert actions.alert_headline("brand_new_reason:i9") == "brand_new_reason:i9"
    assert actions.alert_remedy("brand_new_reason:i9") == "brand_new_reason:i9"


def test_standing_alerts_reads_the_marker_tri_state():
    assert actions.standing_alerts(None) == []
    assert actions.standing_alerts({"reasons": []}) == []
    rows = actions.standing_alerts({"reasons": ["usage_stale", "park_label_stuck:i7"], "since": 1})
    assert [r["reason"] for r in rows] == ["usage_stale", "park_label_stuck:i7"]
    assert rows[0]["headline"] == actions.alert_headline("usage_stale")
    assert rows[0]["remedy"] == actions.alert_remedy("usage_stale")
    for damaged in ({}, [], {"reasons": "usage_stale"}):
        (row,) = actions.standing_alerts(damaged)
        assert row["reason"] == actions.ALERT_UNREADABLE and "state/ALERT" in row["remedy"]
    # a wrong-typed entry inside a readable list is skipped, never raised on
    assert [r["reason"] for r in actions.standing_alerts({"reasons": [7, None, "auth_dead"]})] \
        == ["auth_dead"]


# ================================ the full surface ================================
# Every sender, driven, its message rendered for the worst-case identity. The callers each scenario
# covers are collected against COVERED, which the source scan below holds complete.

COVERED = {
    "decide:alert", "decide:alert_cleared", "decide:park", "decide:bounce", "decide:question",
    "decide:freeze",
    "watchdog:runner_back", "watchdog:resurrect_disabled", "watchdog:resurrect_capped",
    "watchdog:episode_cleared", "watchdog:episode", "watchdog:debugger_launch_failed",
    "watchdog:resurrected", "watchdog:resurrect_failed",
    "doctor:notify_channel",
    "cli:morning_report", "cli:nightly", "cli:promote_report",
    # driven in test_runner.py (test_every_runner_direct_send_arrives_whole)
    "runner:migration_hold", "runner:tick_errors", "runner:own_page_cleared", "runner:morning_report",
}

HUGE = "a worker's memo that runs on for a long time. " * 60
LONG_CHECK = "engine tests " + "(matrix) " * 20


def _decide_texts(out):
    return [a for a in only(out, "notify")]


def test_every_decide_text_arrives_whole():
    busy = [parsed(5)]
    cases = [
        # an ALERT naming several reasons, and the recovery of one of them
        decide(parsed_issues=busy, usage={"auth_status": "ok", "five_hour_pct": None,
                                          "seven_day_pct": None, "last_ok_at": NOW - 99999,
                                          "first_attempt_at": NOW - 99999},
               gh_view=ghv(consecutive_failures=actions.GH_ALERT_FAILURES),
               dsk=disk(launch_anchor={"ok": False, "reason": "gone"},
                        auth_probe={"valid": False, "cli": "logged_out", "keychain_present": True,
                                    "keychain_mtime": NOW - 3600})),
        decide(dsk=disk(alert={"reasons": ["gh_unreachable", "usage_stale"], "since": NOW - 100,
                               "paged": ["gh_unreachable", "usage_stale"],
                               "delivered": ["gh_unreachable", "usage_stale"]},
                        issues_state={"version": 1, "issues": {"i7": ist("running")}}),
               usage={"auth_status": "ok", "five_hour_pct": None, "seven_day_pct": None,
                      "last_ok_at": NOW - 99999, "first_attempt_at": NOW - 99999}),
        # the owner hand-backs, each carrying a memo far past any phone
        decide(dsk=disk(blocked={"i7": "BOUNCED: " + HUGE},
                        issues_state={"version": 1, "issues": {"i7": ist("blocked")}})),
        decide(dsk=disk(blocked={"i7": "QUESTION: " + HUGE},
                        issues_state={"version": 1, "issues": {"i7": ist("blocked")}})),
        decide(dsk=disk(blocked={"i7": HUGE},
                        issues_state={"version": 1,
                                      "issues": {"i7": ist("blocked", questions_asked=2)}})),
        # a freeze on a check whose name alone would fill a text
        decide(config=cfg(required_checks=[LONG_CHECK],
                          dev_branch="a-very-long-development-branch-name"),
               gh_view=ghv(dev_checks=[{"name": LONG_CHECK, "status": "COMPLETED",
                                        "conclusion": "FAILURE"}])),
    ]
    seen = set()
    for out in cases:
        for a in _decide_texts(out):
            _within_parts(a["headline"], a["ask"], a["caller"])
            _whole(a["tier"], a["headline"], a["ask"], a["url"], a["caller"])
            assert HUGE[:40] not in (a["ask"] or ""), a["caller"]            # never the memo itself
            seen.add(a["caller"])
    assert seen == {c for c in COVERED if c.startswith("decide:")}


def test_the_alert_page_is_the_headlines_and_a_pointer_at_the_doctor():
    out = decide(parsed_issues=[parsed(5)],
                 dsk=disk(auth_probe={"valid": False, "cli": "logged_out", "keychain_present": True,
                                      "keychain_mtime": NOW - 3600},
                          launch_anchor={"ok": False, "reason": "gone"}))
    (a,) = [x for x in only(out, "notify") if x["caller"] == "decide:alert"]
    assert a["headline"] == actions.alert_headlines(["auth_dead", "launch_anchor_down"])
    assert a["ask"] == actions.ALERT_ASK and "superlooper doctor" in a["ask"]
    assert a["pages"] == ["auth_dead", "launch_anchor_down"]               # the codes stay on the act


def _watchdog_texts():
    """Drive every text the watchdog core can emit; return the entries."""
    cfg0 = {"watchdog": {"authority": "full", "allowlist": [], "grace_minutes": 30,
                         "heartbeat_stale_minutes": 20, "no_progress_minutes": 30,
                         "resurrection_max_per_hour": 3}, "repo": "o/r"}
    t0, m = 1_700_000_000, 60

    def view(now, **over):
        v = {"heartbeat": now - 15, "alert": None, "lanes_busy": False, "gh_ok": True,
             "eligible_nums": [], "usage_exhausted": False, "kill_switch": False,
             "debugger_live": False, "stopped_by_owner": False, "runner_live": False,
             "demand": True}
        v.update(over)
        return v

    def delivered(res):
        st = res["state"]
        for n in res["notify"]:
            st = wd.record_delivered(st, n)
        return st

    out = []
    # an episode on every paged signal at once, its launch failing, then clearing
    st = wd.new_state()
    long_queue = list(range(100000, 100040))
    r = wd.evaluate(t0, cfg0, view(t0, eligible_nums=long_queue), st)
    r = wd.evaluate(t0 + 31 * m, cfg0, view(t0 + 31 * m, heartbeat=t0 - 40 * m, runner_live=True,
                                            eligible_nums=long_queue,
                                            alert={"reasons": EVERY_REASON}), r["state"])
    out += r["notify"]
    st = delivered(r)
    launch = {"id": "d123456", "signals": ["alert", "heartbeat_stale", "no_progress"]}
    fail = wd.after_launch(t0 + 31 * m, cfg0, st, launch, rc=-9, demand=True)
    out += fail["notify"]
    cleared = wd.evaluate(t0 + 40 * m, cfg0, view(t0 + 40 * m), delivered(fail))
    out += cleared["notify"]
    # a dead runner: capped, disabled, failed, restarted, back
    for cap in (0, 1):
        c = dict(cfg0, watchdog=dict(cfg0["watchdog"], resurrection_max_per_hour=cap))
        st = wd.new_state()
        r1 = wd.evaluate(t0, c, view(t0, heartbeat=t0 - 30 * m, runner_dead=True), st)
        if r1["resurrect"]:
            bad = wd.after_resurrect(t0, c, r1["state"], r1["resurrect"], rc=127, demand=True)
            out += bad["notify"]
            r1 = wd.evaluate(t0 + 6 * m, c, view(t0 + 6 * m, heartbeat=t0 - 30 * m,
                                                 runner_dead=True), delivered(bad))
        out += r1["notify"]
        back = wd.evaluate(t0 + 20 * m, c, view(t0 + 20 * m), delivered(r1))
        out += back["notify"]
    st = wd.new_state()
    r2 = wd.evaluate(t0, cfg0, view(t0, heartbeat=t0 - 30 * m, runner_dead=True), st)
    down = wd.record_delivered(r2["state"], {"tier": "down", "marks": "runner"})
    out += wd.after_resurrect(t0, cfg0, down, dict(r2["resurrect"], id="r123456"), rc=0)["notify"]
    return out


def test_every_watchdog_text_arrives_whole():
    seen = set()
    for n in _watchdog_texts():
        _within_parts(n["headline"], n["ask"], n["caller"])
        _whole(n["tier"], n["headline"], n["ask"], n.get("url"), n["caller"])
        seen.add(n["caller"])
    assert seen == {c for c in COVERED if c.startswith("watchdog:")}


def test_a_no_progress_page_points_at_the_oldest_waiting_issue():
    cfg0 = {"watchdog": {"no_progress_minutes": 30}, "repo": "o/r"}
    base = {"heartbeat": 0, "alert": None, "lanes_busy": False, "gh_ok": True,
            "usage_exhausted": False, "kill_switch": False, "debugger_live": False,
            "stopped_by_owner": False, "runner_live": False, "demand": True}
    t0 = 1_700_000_000
    r = wd.evaluate(t0, cfg0, dict(base, heartbeat=t0, eligible_nums=[42]), wd.new_state())
    r = wd.evaluate(t0 + 600, cfg0, dict(base, heartbeat=t0 + 600, eligible_nums=[42, 43]),
                    r["state"])
    r = wd.evaluate(t0 + 1800, cfg0, dict(base, heartbeat=t0 + 1800, eligible_nums=[42, 43]),
                    r["state"])
    (n,) = r["notify"]
    assert n["url"] == "https://github.com/o/r/issues/42"
    # the detail the text no longer carries is journaled with the episode
    (opened,) = [j for j in r["journal"] if j.get("outcome") == "notified"]
    assert "#42" in opened["detail"]


def test_the_doctor_test_send_arrives_whole():
    _within_parts(stack_doctor.NOTIFY_TEST_HEADLINE, stack_doctor.NOTIFY_TEST_ASK,
                  "doctor:notify_channel")
    _whole(notify.TEST, stack_doctor.NOTIFY_TEST_HEADLINE, stack_doctor.NOTIFY_TEST_ASK)


def _busiest_night():
    rec = lambda ts, act, **kw: dict(ts=ts, act=act, **kw)          # noqa: E731
    j = []
    for i in range(30):
        j += [rec(1000 + i, "merge", id="i%d" % (100 + i), num=100 + i, pr=500 + i, wander=True,
                  outcome="ok"),
              rec(1100 + i, "park", id="i%d" % (200 + i), num=200 + i, needs_william=bool(i % 2),
                  memo=HUGE, outcome="ok"),
              rec(1200 + i, "bounce", id="i%d" % (300 + i), num=300 + i, memo=HUGE, outcome="ok"),
              rec(1300 + i, "post_question", id="i%d" % (400 + i), num=400 + i, question=HUGE,
                  outcome="ok"),
              rec(1400 + i, "regenerate", id="i%d" % (100 + i), num=100 + i, pr=500 + i,
                  new_branch="b", conflicts=1, outcome="ok"),
              rec(1500 + i, "watchdog", outcome="launch_failed", signals=["heartbeat_stale"], id="d%d" % i,
                  rc=2),
              rec(1600 + i, "runner_resurrect", outcome="resurrect_failed", signals=["heartbeat_stale"],
                  id="r%d" % i, rc=2)]
    j += [rec(1700, "triage_launch", id="t7", date="2026-07-02", outcome="launched", detail="x"),
          rec(1701, "triage_finish", id="t7", counts={"merged": 11, "closed": 22, "escalated": 33},
              outcome="ok")]
    view = {"date": "2026-07-02", "now": 2000, "frozen": {"reason": "x", "since": 1},
            "queue": [{"num": 9, "title": "t"}], "usage": None,
            "queue_hold": {"reasons": ["launch_anchor_down"], "since": 1},
            "alerts": actions.standing_alerts({"reasons": EVERY_REASON})}
    return j, view


def test_the_morning_text_is_a_headline_that_arrives_whole_on_the_busiest_night():
    j, view = _busiest_night()
    config = {"repo": "o/r"}
    assert report.morning_news(j, view, config)                      # it IS a texting morning
    h = report.morning_headline(j, view, config)
    _within_parts(h, None, "morning")
    _whole(notify.MORNING, h)
    assert h.startswith("launch queue HELD")                          # the worst news leads
    assert HUGE[:40] not in h


def test_every_news_class_has_a_headline_clause():
    # a news class with no clause would text "nothing new overnight" on a morning that has news
    assert {key for _cls, key in report._NEWS_FACTS} == {key for key, _say in report._HEADLINE_CLAUSES}


def test_an_unreadable_marker_headline_does_not_claim_a_hold_it_cannot_read():
    view = {"date": "2026-07-02", "now": 2000, "queue": [],
            "queue_hold": {"reasons": [actions.ALERT_UNREADABLE], "since": None}}
    h = report.morning_headline([], view, {})
    assert h == "ALERT marker unreadable, queue may be HELD"


def test_a_quiet_morning_headline_still_says_something_true():
    h = report.morning_headline([], {"date": "2026-07-02", "now": 2000, "queue": []}, {})
    assert h and "nothing" in h.lower()


def _engine_callers():
    """Every sender the engine source names, read from its call sites: a `caller="..."` keyword (the
    runner, the CLI, the doctor), decide's `notify(tier, headline, ask, "<caller>", ...)` and the
    watchdog's `_text(tier, headline, ask, "<caller>", ...)` / `_green(headline, "<caller>")`."""
    found = set()
    for path in [*(_SKILL / "lib").glob("*.py"), _SKILL / "bin" / "runner.py",
                 _SKILL / "bin" / "superlooper"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "caller" and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    found.add(kw.value.value)
            name = node.func.id if isinstance(node.func, ast.Name) else None
            at = {("actions.py", "notify"): (3, "decide:"), ("watchdog.py", "_text"): (3, "watchdog:"),
                  ("watchdog.py", "_green"): (1, "watchdog:")}.get((path.name, name))
            if at and len(node.args) > at[0] and isinstance(node.args[at[0]], ast.Constant):
                found.add(at[1] + node.args[at[0]].value)
    return found


def test_a_new_sender_cannot_slip_past_the_full_surface():
    """Every caller the engine names is one of COVERED, so adding a sender forces it into a scenario
    above (or test_runner.py's)."""
    found = _engine_callers()
    assert {"decide:park", "watchdog:episode", "cli:nightly", "runner:tick_errors"} <= found, found
    assert found == COVERED, (sorted(found - COVERED), sorted(COVERED - found))


# ============================ the CLI senders, driven ============================

def _worst_config(rig, sent, **extra):
    cfg_path = rig.repo / ".superlooper" / "config.json"
    c = json.loads(cfg_path.read_text())
    c.update(extra)
    c["notify"] = {"machine_label": "m" * 24, "imessage_to": None,
                   "cmd": f'printf "%s|%s\\n==\\n" "$SL_TITLE" "$SL_BODY" >> {sent}'}
    cfg_path.write_text(json.dumps(c))


def _sent_parts(sent):
    """Each delivered text split back into (tier, headline, ask, url) — re-rendered for WORST."""
    out = []
    for chunk in (sent.read_text() if sent.exists() else "").split("\n==\n"):
        if not chunk.strip():
            continue
        title, body = chunk.split("|", 1)
        emoji = title.split(" ", 1)[0]
        tier = {v: k for k, v in notify.TIER_EMOJI.items()}[emoji]
        headline = title.split(" · ", 1)[1]
        lines = [ln for ln in body.split("\n") if ln]
        url = lines.pop() if lines and lines[-1].startswith("https://") else None
        assert len(lines) <= 1, body                                   # at most one ask line
        out.append((tier, headline, lines[0] if lines else None, url))
    return out


def _no_cut_journaled(rig):
    import journal
    home = rig.tmp / "slhome" / "o__r"
    return not [x for x in journal.read(str(home)) if x.get("act") == "notify_truncated"]


def test_every_cli_nightly_and_promotion_text_arrives_whole(rig, tmp_path):
    sent = tmp_path / "sent.txt"
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=True)
    in_wt = {"SL_NIGHTLY_WORKTREE": str(wt)}
    # red (files and freezes), unparseable (a long results glob), could-not-start (no git checkout)
    _worst_config(rig, sent, qa={"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                                 "results_glob": "results/*.xml", "retry_once": True})
    cli(rig, "nightly", "--repo", str(rig.repo), env_over=in_wt)
    _worst_config(rig, sent, qa={"nightly_cmd": "exit 254",
                                 "results_glob": "results/" + "deep/" * 40 + "*.xml"})
    cli(rig, "nightly", "--repo", str(rig.repo), env_over=in_wt)
    _worst_config(rig, sent, dev_branch="a-very-long-development-branch-name-indeed",
                  qa={"nightly_cmd": "true", "results_glob": "results/*.xml"})
    cli(rig, "nightly", "--repo", str(rig.repo))
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "last_nightly.json").write_text(json.dumps(
        {"date": "2026-07-01", "ok": True, "failures": []}))
    cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    parts = _sent_parts(sent)
    assert len(parts) == 4, parts
    for tier, headline, ask, url in parts:
        _within_parts(headline, ask, headline)
        _whole(tier, headline, ask, url, headline)
    assert _no_cut_journaled(rig)


def test_the_cli_morning_text_arrives_whole(rig, tmp_path):
    import journal
    home = rig.tmp / "slhome" / "o__r"
    now = time.time()
    for rec in _busiest_night()[0]:
        journal.append(str(home), {k: v for k, v in rec.items() if k != "ts"}, now - 3600 + rec["ts"])
    sent = tmp_path / "sent.txt"
    _worst_config(rig, sent)
    r = cli(rig, "morning-report", "--repo", str(rig.repo), "--always-send")
    assert r.returncode == 0, r.stdout + r.stderr
    (part,) = _sent_parts(sent)
    tier, headline, ask, url = part
    assert tier == notify.MORNING and ask is None and url is None
    _within_parts(headline, ask, "cli:morning_report")
    _whole(tier, headline, ask, url, "cli:morning_report")
    assert _no_cut_journaled(rig)


# ============================ doctor prints the remedy ============================

def _seed_alert(rig, reasons):
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "ALERT").write_text(json.dumps({"reasons": reasons, "since": 1}))


def test_doctor_prints_the_remedy_for_each_standing_reason(rig):
    reasons = ["launch_anchor_down", "session_logged_out:i5:invalid_api_key"]
    _seed_alert(rig, reasons)
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    for reason in reasons:
        assert actions.alert_headline(reason) in r.stdout, reason
        assert " ".join(actions.alert_remedy(reason).split()) in " ".join(r.stdout.split()), reason
    assert actions.alert_remedy("usage_stale") not in r.stdout          # only what stands


def test_doctor_prints_no_remedy_when_nothing_stands(rig):
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert "ALERT standing" not in r.stdout and "standing alerts" not in r.stdout
    for reason in actions.ALERT_REMEDIES:
        assert actions.alert_remedy(reason)[:60] not in r.stdout


def test_doctor_survives_an_alert_marker_that_is_not_text(rig):
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "ALERT").write_bytes(b"\xff\xfe\x00garbage")
    for args in (["doctor"], ["doctor", "--stack"]):
        r = cli(rig, *args, "--repo", str(rig.repo), env_over=_stack_env(rig))
        assert "Traceback" not in r.stderr, r.stderr
        assert "state/ALERT" in r.stdout and "config" in r.stdout


def test_doctor_names_an_unreadable_alert_marker(rig):
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "ALERT").write_text("{not json")
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert actions.alert_headline(actions.ALERT_UNREADABLE) in r.stdout


def test_doctor_stack_prints_the_standing_remedies_too(rig):
    _seed_alert(rig, ["gh_unreachable"])
    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))
    assert actions.alert_headline("gh_unreachable") in r.stdout
    assert " ".join(actions.alert_remedy("gh_unreachable").split()) in " ".join(r.stdout.split())


# ============================ the operator docs name the headlines ============================

def test_runner_ops_lists_the_headline_for_every_alert_reason():
    doc = (_SKILL.parent.parent.parent / "plugin" / "skills" / "superlooper" / "references"
           / "runner-ops.md").read_text()
    for code, headline in actions.ALERT_HEADLINES.items():
        assert f"| `{code}` | {headline} |" in doc, code
    for prefix in ("session_logged_out", "session_at_dialog", "park_label_stuck", "launch_runaway",
                   "update_errors", "runner_tick_errors", "migration_hold"):
        assert f"| `{prefix}:" in doc, prefix
    assert actions.MIGRATION_HOLD_HEADLINE in doc
