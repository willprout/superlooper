"""report.morning — the one batched overnight surface (plan Task 11, runner-ops "The morning
report"). PURE: (journal_records, gh_view, ledger, config) -> markdown str, so the whole thing is
a fixture table + a golden file.

The journal is the durable record of what the runner DID overnight (merge/park/bounce/regenerate/
wander/nightly actions, each already ts-stamped and outcome-stamped by runner.py); gh_view carries
the CURRENT live facts the report also needs (the date + reference clock, the freeze marker, the
ready queue, usage); ledger + config supply the accepted-failure count and quarantine size.

Two postmortem-driven invariants under test: a quiet night renders HONESTLY ("nothing happened,
queue empty" — never a blank that reads as broken), and a nightly that could not parse its results
is an honest "could not parse" line, NEVER a silent green.
"""
from pathlib import Path

import nightly
import report

REPO = "titan/eapp"
_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "reports"


def _rec(ts, act, **kw):
    return dict(ts=ts, act=act, **kw)


def _full_journal():
    # ts are within a single overnight window; NOW below is just after the latest.
    return [
        _rec(1000, "merge", id="i7", num=7, pr=12, outcome="ok"),
        _rec(1001, "absorb_merged", id="i8", num=8, outcome="ok"),
        _rec(1010, "park", id="i9", num=9, needs_william=False,
             memo="retry cap hit (2 relaunches, still no report)", outcome="ok"),
        _rec(1011, "park", id="i10", num=10, needs_william=True,
             memo="conflict cap hit — collided with #7 twice", outcome="ok"),
        _rec(1012, "bounce", id="i11", num=11,
             memo="BOUNCED: the crash is already fixed on dev; propose closing", outcome="ok"),
        _rec(1013, "regenerate", id="i7", num=7, pr=12,
             new_branch="sl/i7-widget-r2", conflicts=1, outcome="ok"),
        _rec(1014, "merge", id="i12", num=12, pr=20, wander=True, outcome="ok"),
        _rec(1015, "post_question", id="i13", num=13,
             question="QUESTION: approach A or B?", outcome="ok"),
        _rec(1016, "post_question", id="i13", num=13,
             question="QUESTION: and what about C?", outcome="ok"),
        _rec(1020, "nightly", date="2026-07-02", green=False, flakes=2, persistent=1,
             filed=[30], parse_error=False, outcome="ok"),
    ]


def _view(now=1100, **kw):
    v = {"date": "2026-07-02", "now": now, "frozen": None,
         "queue": [{"num": 15, "title": "add the export button"},
                   {"num": 16, "title": "fix the login redirect"}],
         "usage": {"pct": 42}}
    v.update(kw)
    return v


def _cfg(**kw):
    c = {"repo": REPO, "qa": {"quarantine": ["tests/test_flaky_widget.py::test_drag"]}}
    c.update(kw)
    return c


def test_full_report_has_every_section_with_its_entries():
    out = report.morning(_full_journal(), _view(), ledger={}, config=_cfg())

    assert "2026-07-02" in out                                # the date in the title/header
    # Merged — both a clean merge and an absorbed out-of-band merge, cross-linked
    assert "Merged" in out
    assert "#7" in out and "#8" in out
    assert f"https://github.com/{REPO}/pull/12" in out        # PR link built from repo
    # Parked / needs-william — memos verbatim, needs-william flagged distinctly
    assert "retry cap hit" in out
    assert "conflict cap hit" in out and "needs-owner" in out.lower()
    # Bounces — the BOUNCED memo verbatim
    assert "BOUNCED: the crash is already fixed" in out
    # Conflict regenerations — the tuning metric, with the rebuilt branch
    assert "sl/i7-widget-r2" in out
    # Wanders — the declared-vs-actual touches metric
    assert "#12" in out and "wander" in out.lower()
    # Owner questions — the #163 question-rate, counted per issue (i13 asked twice)
    q_section = out.split("## Owner questions")[1].split("\n## ")[0]
    assert "#13" in q_section and "asked 2 owner question(s)" in q_section
    assert "2 question(s)" in out                             # the summary tally counts total asks
    # Gate health — nightly result + flake count + quarantine size
    assert "flake" in out.lower() and "quarantine" in out.lower()


def test_quiet_night_renders_honestly():
    out = report.morning([], _view(now=0, queue=[], usage=None), ledger={}, config=_cfg())
    low = out.lower()
    assert "nothing happened" in low and "queue empty" in low
    # it must NOT fabricate activity
    assert "https://github.com" not in out


def test_broken_nightly_results_are_honest_never_silent_green():
    j = [_rec(2000, "nightly", date="2026-07-03", parse_error=True, green=False,
              flakes=0, persistent=0, outcome="ok")]
    out = report.morning(j, _view(now=2100), ledger={}, config=_cfg())
    low = out.lower()
    assert "could not parse" in low or "unparse" in low
    assert "nightly" in low
    # honest failure, not a green claim
    assert "nightly: green" not in low and "nightly (2026-07-03): green" not in low


def test_conflict_regenerations_are_windowed_to_the_last_7_days():
    week = 7 * 24 * 3600
    now = 1_000_000
    j = [
        _rec(now - week - 10, "regenerate", id="i1", num=1, new_branch="sl/i1-old-r2",
             conflicts=1, outcome="ok"),                       # older than 7 days -> excluded
        _rec(now - 100, "regenerate", id="i2", num=2, new_branch="sl/i2-new-r2",
             conflicts=1, outcome="ok"),                       # recent -> included
    ]
    out = report.morning(j, _view(now=now, queue=[]), ledger={}, config=_cfg())
    assert "sl/i2-new-r2" in out
    assert "sl/i1-old-r2" not in out


def test_freeze_state_is_reflected():
    frozen = {"reason": "dev checks red: quality-gate (failure)", "since": 999}
    out = report.morning([], _view(frozen=frozen, queue=[]), ledger={}, config=_cfg())
    assert "FROZEN" in out or "frozen" in out.lower()
    assert "quality-gate" in out


def test_accepted_failures_and_quarantine_counts_show_in_gate_health():
    ledger = {"abc123": {"note": "known flaky widget"}, "def456": {"note": "third-party 500"}}
    out = report.morning(_full_journal(), _view(), ledger=ledger, config=_cfg())
    assert "2" in out                                          # 2 accepted known failures
    assert "quarantine" in out.lower()


def test_failed_actions_are_not_reported_as_successes():
    # a merge whose outcome is a failure string must NOT appear in the Merged section
    j = [_rec(1, "merge", id="i7", num=7, pr=12, outcome="merge failed (will retry next tick)")]
    out = report.morning(j, _view(now=2, queue=[]), ledger={}, config=_cfg())
    assert f"https://github.com/{REPO}/pull/12" not in out


def test_overnight_sections_window_since_the_last_report():
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},           # last report at T
        {"ts": T - 100, "act": "merge", "id": "i1", "num": 1, "pr": 5, "outcome": "ok"},   # before -> out
        {"ts": T + 100, "act": "merge", "id": "i2", "num": 2, "pr": 6, "outcome": "ok"},   # after -> in
        {"ts": T - 100, "act": "park", "id": "i3", "num": 3, "needs_william": False,
         "memo": "old park", "outcome": "ok"},
        {"ts": T + 100, "act": "bounce", "id": "i4", "num": 4, "memo": "recent bounce", "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 200, queue=[]), ledger={}, config=_cfg())
    assert f"https://github.com/{REPO}/pull/6" in out       # merge AFTER the last report -> shown
    assert f"https://github.com/{REPO}/pull/5" not in out   # merge BEFORE the last report -> excluded
    assert "recent bounce" in out and "old park" not in out


def test_overnight_defaults_to_24h_when_no_prior_report():
    now, day = 1_000_000, 24 * 3600
    j = [
        {"ts": now - day - 10, "act": "merge", "id": "i1", "num": 1, "pr": 5, "outcome": "ok"},  # >24h
        {"ts": now - 10, "act": "merge", "id": "i2", "num": 2, "pr": 6, "outcome": "ok"},         # recent
    ]
    out = report.morning(j, _view(now=now, queue=[]), ledger={}, config=_cfg())
    assert f"https://github.com/{REPO}/pull/6" in out
    assert f"https://github.com/{REPO}/pull/5" not in out


def test_quiet_night_stays_honest_after_old_activity():
    # the reviewer's regression: an old merge must NOT keep every future night from being quiet
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T - 100, "act": "merge", "id": "i1", "num": 1, "pr": 5, "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 50, queue=[]), ledger={}, config=_cfg())
    assert "nothing happened" in out.lower() and "queue empty" in out.lower()


def test_park_then_reapprove_then_merge_renders_once_as_merged_never_open_ask():
    # DoD (#37): an issue that parked, was re-approved, and then MERGED in the same window must
    # render once — under Merged — and NEVER as an open ask in Parked. The park record must be
    # reconciled against the issue's final outcome (it landed), not reported from the raw window.
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "conflict cap hit — re-approve to retry", "outcome": "ok"},
        {"ts": T + 20, "act": "reapprove", "id": "i9", "num": 9, "outcome": "ok"},
        {"ts": T + 30, "act": "merge", "id": "i9", "num": 9, "pr": 42, "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())

    merged_section = out.split("## Merged")[1].split("\n## ")[0]
    parked_section = out.split("## Parked / needs-owner")[1].split("\n## ")[0]
    # landed: it shows once under Merged, with its PR link
    assert "#9" in merged_section
    assert f"https://github.com/{REPO}/pull/42" in out
    # ...annotated as a resolved park episode (labeled, not a second open ask)
    assert "parked earlier, later merged" in merged_section
    # NOT an open ask: neither the issue nor its memo appears under Parked
    assert "#9" not in parked_section
    assert "re-approve to retry" not in out
    # the summary counts it as merged, not parked
    assert "1 merged · 0 parked/needs-owner" in out


def test_genuine_park_without_a_later_merge_still_renders_as_open_ask():
    # the other half of the DoD: a park with NO later landing is a real open ask and must survive
    # reconciliation unchanged (needs-william flagged, memo verbatim).
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "retry cap hit — genuinely stuck", "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())
    parked_section = out.split("## Parked / needs-owner")[1].split("\n## ")[0]
    assert "#9" in parked_section and "retry cap hit — genuinely stuck" in parked_section
    assert "needs-owner" in parked_section.lower()
    assert "0 merged · 1 parked/needs-owner" in out


def test_merge_before_a_later_park_stays_an_open_ask():
    # reconciliation is by FINAL outcome, not mere co-occurrence: if the merge came BEFORE the park
    # (an issue that landed, was re-opened, then parked again), the park is the latest word and
    # remains a genuine open ask — the merge must not silently resolve it away.
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "merge", "id": "i9", "num": 9, "pr": 42, "outcome": "ok"},
        {"ts": T + 20, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "reopened and stuck again", "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())
    parked_section = out.split("## Parked / needs-owner")[1].split("\n## ")[0]
    assert "#9" in parked_section and "reopened and stuck again" in parked_section


def test_park_the_owner_closed_is_not_reported_as_an_open_ask():
    # issue #108: a park the owner STOOD DOWN by closing the issue on GitHub (an absorb_close) is no
    # longer an open ask — it must leave the "Open asks only" Parked section, and NOT appear under
    # Merged (an absorbed close is a drop, never a landing).
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "conflict cap hit", "outcome": "ok"},
        {"ts": T + 30, "act": "absorb_close", "id": "i9", "num": 9, "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())
    parked_section = out.split("## Parked / needs-owner")[1].split("\n## ")[0]
    merged_section = out.split("## Merged")[1].split("\n## ")[0]
    assert "#9" not in parked_section and "conflict cap hit" not in out
    assert "#9" not in merged_section                  # a drop is never listed as a landing
    # nothing else happened, so the reconciled-away park leaves a genuinely quiet night
    assert "nothing happened" in out.lower()


def test_bounce_the_owner_closed_is_not_reported_as_an_open_ask():
    # the same for a bounce: the owner closed it, so it is resolved and drops out of Bounces.
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "bounce", "id": "i11", "num": 11,
         "memo": "BOUNCED: already fixed on dev", "outcome": "ok"},
        {"ts": T + 30, "act": "absorb_close", "id": "i11", "num": 11, "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())
    bounces_section = out.split("## Bounces")[1].split("\n## ")[0]
    assert "#11" not in bounces_section and "already fixed on dev" not in out
    assert "nothing happened" in out.lower()           # the reconciled-away bounce leaves it quiet


def test_absorb_close_before_a_later_repark_stays_an_open_ask():
    # reconciliation is by FINAL outcome (mirrors #37): a close that came BEFORE a later re-park
    # (owner closed, reopened, re-approved, parked again) leaves the new park a genuine open ask.
    T = 1_000_000
    j = [
        {"ts": T, "act": "morning_report", "date": "d", "outcome": "ok"},
        {"ts": T + 10, "act": "absorb_close", "id": "i9", "num": 9, "outcome": "ok"},
        {"ts": T + 20, "act": "park", "id": "i9", "num": 9, "needs_william": True,
         "memo": "reopened and stuck again", "outcome": "ok"},
    ]
    out = report.morning(j, _view(now=T + 100, queue=[]), ledger={}, config=_cfg())
    parked_section = out.split("## Parked / needs-owner")[1].split("\n## ")[0]
    assert "#9" in parked_section and "reopened and stuck again" in parked_section


def test_a_green_nightly_only_night_is_still_quiet():
    # a routine green nightly is the system working, not activity that needs William — otherwise
    # (a nightly runs EVERY night) there could never be a quiet night in production.
    now = 2000
    j = [{"ts": 1900, "act": "nightly", "date": "d", "green": True, "flakes": 0,
          "persistent": 0, "parse_error": False, "outcome": "ok"}]
    out = report.morning(j, _view(now=now, queue=[]), ledger={}, config=_cfg())
    assert "nothing happened" in out.lower()
    assert "Nightly (d): green" in out             # ...but gate health still reports it ran


def test_gate_health_corrupt_boolean_is_not_rendered_green():
    # Codex R2 M1: a corrupt journal line ("green": "false", a truthy string) must NOT read as
    # green, and a wrong-typed parse_error must not be trusted — render unclear / not-auto-verified.
    # green is a truthy STRING and there is no parse_error, so the buggy `elif latest.get("green")`
    # would render it green. Also assert a wrong-typed parse_error isn't trusted as a real one.
    j = [{"ts": 100, "act": "nightly", "date": "d", "green": "false", "flakes": 0,
          "persistent": 0, "outcome": "ok"}]
    out = report.morning(j, _view(now=200, queue=[]), ledger={}, config=_cfg())
    low = out.lower()
    assert "nightly (d): green" not in low
    assert "not auto-verified" in low or "unclear" in low


def _golden_holds():
    """Two standing holds for the "everything on" golden (#405): one past the alert threshold (so
    the golden pins the alert-tier line and its summary count) and one young enough to be a plain
    listing whose reason is read back from the journal."""
    return {"version": 1, "issues": {
        "i17": {"status": "ready", "launch_hold_since": 1100 - 3 * 24 * 3600 - 3600,
                "launch_hold_reason": "no usage headroom (the meter is unreadable/unhealthy, or "
                                      "at-or-over a ceiling) — the restart waits for quota, "
                                      "exactly as a fresh launch does"},
        "i18": {"status": "ready", "queue_invalid_signature": "sig-a1b2",
                "queue_invalid_since": 1100 - 25 * 60}}}


def test_full_report_matches_golden():
    out = report.morning(_full_journal() + [_rec(1017, "queue_invalid", id="i18", num=18,
                                                 signature="sig-a1b2",
                                                 reason="missing `## Loop metadata`",
                                                 outcome="ok")],
                         _view(issues_state=_golden_holds()),
                         ledger={"abc123": {"note": "x"}, "def456": {"note": "y"}}, config=_cfg())
    assert out == (_GOLDEN / "morning-full.md").read_text()


def test_quiet_report_matches_golden():
    out = report.morning([], _view(now=0, queue=[], usage=None), ledger={}, config=_cfg())
    assert out == (_GOLDEN / "morning-quiet.md").read_text()


def test_wrong_typed_inputs_never_raise():
    # every arg garbage -> a still-honest report, never an exception (fail-closed like the runner)
    out = report.morning(None, None, ledger=None, config=None)
    assert isinstance(out, str) and out
    assert report.morning("nope", 5, ledger=7, config=[])       # no raise


# --- standing holds, and their AGE (issue #405) -------------------------------------------------
# A hold condition is re-derived every tick, but its JOURNALING dedups on a durable stamp that
# resets only when the issue launches, parks or is re-approved — so an issue that never does any of
# those goes completely silent (a realized 3-day silent hold on the eApp loop, 2026-07-31 -> 08-03).
# The report is the surface that has to say it out loud: WHO is held, WHY, and for HOW LONG. The
# impure caller hands in the runner's loopstate (`view['issues_state']`); this module derives the
# hold list purely, exactly as it derives everything else.

DAY = 24 * 3600


def _held_state(**issues):
    return {"version": 1, "issues": issues}


def _launch_held(since, reason="usage is at 97% of the seven-day ceiling", **over):
    d = {"status": "ready", "launch_hold_reason": reason, "launch_hold_since": since}
    d.update(over)
    return d


def test_the_standing_holds_section_lists_every_held_issue_with_reason_and_age():
    now = 1_000_000
    state = _held_state(
        i12=_launch_held(now - 3 * DAY - 4 * 3600),
        i13={"status": "ready", "queue_invalid_signature": "sig-1",
             "queue_invalid_since": now - 90 * 60},
        i14={"status": "ready", "wildcard_hold_journaled": True,
             "wildcard_hold_since": now - 20 * 60})
    j = [_rec(now - 3 * DAY, "queue_invalid", id="i13", num=13,
              reason="missing `## Loop metadata`", outcome="ok"),
         _rec(now - 20 * 60, "wildcard_hold", id="i14", num=14,
              reason="launch held: it declares no `touches:` (wildcard '*')", outcome="ok")]
    out = report.morning(j, _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    section = out.split("## Standing holds")[1].split("\n## ")[0]
    # every held issue, named, with the reason and the age of the hold
    assert "#12" in section and "97% of the seven-day ceiling" in section and "3d 4h" in section
    assert "#13" in section and "missing `## Loop metadata`" in section and "1h 30m" in section
    assert "#14" in section and "wildcard" in section and "20m" in section


def test_a_hold_older_than_the_threshold_raises_an_alert_tier_line():
    now = 1_000_000
    state = _held_state(i12=_launch_held(now - report.HOLD_ALERT_SECONDS - 60))
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    # beyond the listing: an alert-tier line ABOVE the sections, where the owner cannot coffee past
    head = out.split("## ")[0]
    assert "#12" in head and "stall" in head.lower()
    # ...and the daily push's own summary line says one is standing, so the alert rides the push
    # that already goes out rather than earning a new one
    summary = next(ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#"))
    assert "hold" in summary.lower()


def test_a_young_hold_is_listed_but_never_alerted():
    now = 1_000_000
    state = _held_state(i12=_launch_held(now - 60))
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    assert "#12" in out.split("## Standing holds")[1]        # listed
    assert "stall" not in out.split("## ")[0].lower()        # but no alert-tier line


def test_an_aged_hold_breaks_the_quiet_night_claim():
    now = 1_000_000
    state = _held_state(i12=_launch_held(now - 5 * DAY))
    out = report.morning([], _view(now=now, queue=[], usage=None, issues_state=state),
                         ledger={}, config=_cfg())
    assert "nothing happened" not in out.lower()


def test_a_relaunch_held_lane_says_the_worker_is_gone_not_that_it_is_running():
    # The exited-worker + over-ceiling case (#405): the lane's status is still `running`, so the
    # ONLY thing that can tell the truth is the hold record. It must name the dead worker.
    now = 1_000_000
    state = _held_state(i12=_launch_held(now - 3600, status="running", relaunch_held=True))
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    section = out.split("## Standing holds")[1].split("\n## ")[0]
    assert "#12" in section and "relaunch" in section.lower() and "exited" in section.lower()


def test_a_hold_with_no_recorded_start_is_listed_without_an_age_and_never_alerts():
    # A stamp written by an engine older than #405 has no clock. Say so — never invent an age, and
    # never alert on one that cannot be proven.
    now = 1_000_000
    state = _held_state(i12={"status": "ready", "launch_hold_reason": "waiting on #3"})
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    section = out.split("## Standing holds")[1].split("\n## ")[0]
    assert "#12" in section and "not recorded" in section
    assert "stall" not in out.split("## ")[0].lower()


def test_a_terminal_issue_never_renders_as_held():
    # park/merge/bounce END the episode; a leftover stamp on one of those is history, not a hold.
    now = 1_000_000
    state = _held_state(i12=_launch_held(now - 5 * DAY, status="parked"),
                        i13=_launch_held(now - 5 * DAY, status="merged"),
                        i14=_launch_held(now - 5 * DAY, status="bounced"),
                        i15=_launch_held(now - 5 * DAY, status="needs_william"))
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    assert "None — nothing is held." in out.split("## Standing holds")[1]


def test_no_holds_renders_an_honest_empty_section():
    out = report.morning([], _view(now=0, queue=[]), ledger={}, config=_cfg())
    assert "## Standing holds" in out
    assert "None — nothing is held." in out.split("## Standing holds")[1]


def test_standing_holds_never_raises_on_wrong_typed_state():
    for bad in (None, "nope", 5, [], {"issues": "nope"}, {"issues": {"i1": "nope", 7: {}}},
                {"issues": {"i1": {"launch_hold_reason": 5, "launch_hold_since": "soon"}}}):
        assert isinstance(report.standing_holds(bad), list)
        out = report.morning([], _view(now=0, queue=[], issues_state=bad),
                             ledger={}, config=_cfg())
        assert isinstance(out, str) and "## Standing holds" in out


def test_a_non_finite_timestamp_never_takes_the_report_down():
    # json round-trips the bare literals NaN and Infinity, so a corrupt or hand-edited issues.json
    # can hand this module a stamp int() refuses (ValueError / OverflowError). The report's whole
    # contract is that a broken overnight never blanks it — the surface #405 exists to keep speaking.
    for bad in (float("nan"), float("inf"), float("-inf")):
        state = _held_state(i12={"status": "ready", "launch_hold_reason": "held",
                                 "launch_hold_since": bad})
        for frozen in (None, {"reason": "nightly red", "since": bad}):
            out = report.morning([], _view(now=1_000_000, queue=[], issues_state=state,
                                           frozen=frozen), ledger={}, config=_cfg())
            section = out.split("## Standing holds")[1].split("\n## ")[0]
            assert "#12" in section and "not recorded" in section   # listed, with no invented age
            assert "None" not in out.split("## ")[0]                # and never an alert reading None
    # ...and a non-finite reference clock (view['now']) is just as survivable.
    state = _held_state(i12=_launch_held(0))
    assert isinstance(report.morning([], _view(now=float("inf"), queue=[], issues_state=state),
                                     ledger={}, config=_cfg()), str)


def test_standing_holds_terminal_set_tracks_the_runners_own():
    # The report's "this episode is over" test must be the SAME set the runner acts on, or a status
    # the loop treats as terminal would keep renderings a hold forever (or vice versa).
    import actions
    assert report._TERMINAL_STATUSES == actions.TERMINAL_STATUSES


# --- the one stamp that means NOT held -----------------------------------------------------------
# The engine has no clear-the-stamp verb, so #172 retires a stale unlanded-read stamp by OVERWRITING
# it with an honest all-clear. A truthy `launch_hold_reason` is therefore not, by itself, evidence of
# a hold — and listing that one would print a line that refutes itself, then alert on it at 24h.

def _retirement_stamp():
    import actions
    return actions._LANE_BOUND_AFTER_UNLANDED_READ


def test_the_lane_bound_all_clear_stamp_is_never_listed_as_a_hold():
    now = 1_000_000
    state = _held_state(i50={"status": "ready", "launch_hold_reason": _retirement_stamp(),
                             "launch_hold_since": now - 5 * DAY})
    out = report.morning([], _view(now=now, queue=[], issues_state=state),
                         ledger={}, config=_cfg())
    assert report.standing_holds(state) == []
    assert "None — nothing is held." in out.split("## Standing holds")[1]
    assert "STALL" not in out                       # ...and no self-refuting alert at 24h
    assert "nothing happened" in out.lower()        # ...and it never breaks a genuinely quiet night


def test_the_all_clear_prefix_tracks_the_engines_own():
    # Matched on the PREFIX, and pinned to the engine's constant: the tail prose is free to change
    # across releases (durable stamps written by an older one are still on disk), but if the PREFIX
    # ever drifts this report starts listing all-clears as holds again.
    import actions
    assert report._LANE_BOUND_PREFIX == actions._LANE_BOUND_PREFIX
    assert _retirement_stamp().startswith(report._LANE_BOUND_PREFIX)


def test_a_wrong_typed_stamp_cannot_smuggle_the_all_clear_in_through_the_journal():
    # The skip has to sit on the RESOLVED reason, not the raw stamp: a wrong-typed
    # `launch_hold_reason` (a truthy list) falls through to the journal fallback, which would
    # re-supply the retirement prose — and the line would read "has been held 5d — this issue is
    # no longer held by the eligibility gate at all", with a STALL alert on top of it.
    now = 1_000_000
    state = _held_state(i50={"status": "ready", "launch_hold_reason": ["nope"],
                             "launch_hold_since": now - 5 * DAY})
    j = [_rec(now - 5 * DAY, "launch_hold", id="i50", num=50, reason=_retirement_stamp(),
              outcome="ok")]
    assert report.standing_holds(state, j) == []
    out = report.morning(j, _view(now=now, queue=[], issues_state=state), ledger={}, config=_cfg())
    assert "None — nothing is held." in out.split("## Standing holds")[1]
    assert "STALL" not in out


def test_an_unlanded_read_hold_IS_still_listed():
    # The mirror: only the RETIREMENT is an all-clear. The unlanded-read hold it replaces is a real
    # hold and must still be reported.
    import actions
    now = 1_000_000
    state = _held_state(i50={"status": "ready",
                             "launch_hold_reason": actions.UNLANDED_CLOSED_READ_PREFIX + " — …",
                             "launch_hold_since": now - 2 * 3600})
    held = report.standing_holds(state)
    assert len(held) == 1 and held[0]["id"] == "i50"


def test_an_unhashable_status_or_journal_act_never_raises():
    # `x in frozenset` RAISES on an unhashable value, so the coercion contract has to hold at the
    # membership tests too — not just at the type checks around them.
    state = _held_state(i12={"status": ["nope"], "launch_hold_reason": "held",
                             "launch_hold_since": 1})
    assert len(report.standing_holds(state)) == 1               # a wrong-typed status is not terminal
    j = [{"ts": 1, "act": ["nope"], "id": "i12", "reason": "x"}, {"ts": 2, "act": {}, "id": "i12"}]
    assert isinstance(report.standing_holds(state, j), list)
    assert isinstance(report.morning(j, _view(now=1_000_000, queue=[], issues_state=state),
                                     ledger={}, config=_cfg()), str)


# --- the freeze marker's age (issue #405) -------------------------------------------------------

def test_the_freeze_line_carries_its_age():
    now = 1_000_000
    frozen = {"reason": "dev checks red: quality-gate (failure)", "since": now - 2 * DAY - 5 * 3600}
    out = report.morning([], _view(now=now, frozen=frozen, queue=[]), ledger={}, config=_cfg())
    freeze = out.split("## Freeze state")[1]
    assert "FROZEN" in freeze and "quality-gate" in freeze and "2d 5h" in freeze


def test_an_old_freeze_raises_an_alert_tier_line():
    now = 1_000_000
    frozen = {"reason": "nightly red: 2 persistent failure(s)",
              "since": now - report.FREEZE_ALERT_SECONDS - 60}
    out = report.morning([], _view(now=now, frozen=frozen, queue=[]), ledger={}, config=_cfg())
    head = out.split("## ")[0]
    assert "frozen" in head.lower() and "nightly red" in head


def test_a_fresh_freeze_is_reported_without_an_alert_line():
    now = 1_000_000
    frozen = {"reason": "dev checks red: ci (failure)", "since": now - 600}
    out = report.morning([], _view(now=now, frozen=frozen, queue=[]), ledger={}, config=_cfg())
    head = out.split("## ")[0]
    assert "10m" in out.split("## Freeze state")[1]
    assert "frozen for" not in head.lower()


def test_a_freeze_with_no_since_still_renders_its_reason():
    # Existence IS the freeze (fail closed): an unreadable/legacy marker with no clock must still
    # render as frozen, just without an age — never dropped, never with an invented one.
    out = report.morning([], _view(now=1_000_000, frozen={"reason": "marker unreadable"}, queue=[]),
                         ledger={}, config=_cfg())
    freeze = out.split("## Freeze state")[1]
    assert "FROZEN" in freeze and "marker unreadable" in freeze


# --- installed-engine publish drift notice (issue #39) -----------------------------------------
# The runner/CLI pre-computes the drift (git lives in the impure assembler; report.py stays pure)
# and hands it in via view['engine_drift']. The report carries a one-line nudge ONLY when the
# installed engine is BEHIND — every other state (in sync, skipped, unknown) stays silent here.

def _drift(status="behind", behind=6, ref="origin/main"):
    return {"status": status, "behind": behind, "ref": ref, "installed_sha": "abc123"}


def test_morning_report_carries_a_drift_notice_when_installed_engine_is_behind():
    out = report.morning([], _view(now=0, queue=[], engine_drift=_drift(behind=6)),
                         ledger={}, config=_cfg())
    low = out.lower()
    assert "installed engine" in low and "6" in out
    assert "origin/main" in out
    assert "install.sh" in out                                  # names the gated publish step
    assert "republish" in low                                   # the nudge


def test_drift_notice_uses_singular_for_one_commit():
    out = report.morning([], _view(now=0, queue=[], engine_drift=_drift(behind=1)),
                         ledger={}, config=_cfg())
    assert "1 commit behind" in out and "1 commits" not in out


def test_no_drift_notice_when_engine_is_in_sync_or_skipped_or_absent():
    for ed in (_drift(status="in_sync", behind=0), _drift(status="skipped", behind=None),
               _drift(status="unknown", behind=None), None):
        out = report.morning([], _view(now=0, queue=[], engine_drift=ed),
                             ledger={}, config=_cfg())
        assert "behind" not in out.lower()
        assert "republish" not in out.lower()


def test_drift_notice_does_not_hijack_the_push_summary_line():
    # The push notification body is the FIRST non-title, non-blank line — the tally / "nothing
    # happened". The drift nudge must sit AFTER it, never replace it.
    out = report.morning([], _view(now=0, queue=[], engine_drift=_drift(behind=6)),
                         ledger={}, config=_cfg())
    summary = next(ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#"))
    assert "behind" not in summary.lower()                      # summary is untouched
    assert "nothing happened" in summary.lower()                # a quiet night stays quiet


def test_drift_notice_does_not_flip_a_quiet_night_to_noisy():
    # Drift is a standing condition, not overnight activity — a quiet night with drift still reads
    # "nothing happened overnight", with the nudge as an extra line.
    out = report.morning([], _view(now=0, queue=[], engine_drift=_drift(behind=6)),
                         ledger={}, config=_cfg())
    assert "nothing happened" in out.lower()
    assert "installed engine" in out.lower()


# =============================== promotion evidence ===============================

def _f(tid, text):
    return {"test_id": tid, "text": text}


def test_promotion_is_evidence_only_never_a_verdict():
    suite = {"ok": True, "failures": [], "source": "fresh suite"}
    out = report.promotion("2026-07-02", suite, ledger={},
                           compare={"prod_branch": "prod", "dev_branch": "main", "result": {}},
                           open_issues=[], config=_cfg())
    low = out.lower()
    # the §4.6 bright line: no pass/fail logic, no "must pass", no promote/don't-promote verdict
    assert "evidence only" in low or "no pass/fail" in low or "no verdict" in low
    assert "must pass" not in low
    assert "do not promote" not in low and "ready to promote" not in low


def test_promotion_highlights_new_failures_and_folds_accepted():
    new = _f("t::regression", "new boom after PR #40")
    known = _f("t::flaky", "third-party widget 500")
    ledger = {nightly.fingerprint(known): {"note": "known-flaky widget"}}
    suite = {"ok": True, "failures": [new, known], "source": "fresh suite"}
    out = report.promotion("2026-07-02", suite, ledger,
                           compare={"prod_branch": None, "dev_branch": "main", "result": None},
                           open_issues=[], config=_cfg())
    assert "t::regression" in out                     # a NEW failure is highlighted by name
    assert nightly.fingerprint(new) in out             # ...with its fingerprint to copy into accept
    assert "t::flaky" not in out                       # accepted -> folded away, not itemized
    assert "1" in out                                  # ...but counted (1 known failure folded)


def test_promotion_no_prod_branch_points_at_the_repo_checklist():
    suite = {"ok": True, "failures": [], "source": "fresh suite"}
    out = report.promotion("2026-07-02", suite, ledger={},
                           compare={"prod_branch": None, "dev_branch": "main", "result": None},
                           open_issues=[], config=_cfg())
    assert "no prod branch configured" in out.lower()


def test_promotion_shows_merges_since_last_promotion_when_prod_set():
    suite = {"ok": True, "failures": [], "source": "fresh suite"}
    out = report.promotion("2026-07-02", suite, ledger={},
                           compare={"prod_branch": "prod", "dev_branch": "main",
                                    "result": {"ahead_by": 7, "total_commits": 7}},
                           open_issues=[], config=_cfg())
    assert "7" in out and "prod" in out


def test_promotion_lists_open_issues_and_could_not_parse_is_honest():
    suite = {"ok": False, "failures": [], "source": "fresh suite"}
    out = report.promotion("2026-07-02", suite, ledger={},
                           compare={"prod_branch": None, "dev_branch": "main", "result": None},
                           open_issues=[{"num": 42, "title": "wire the export button"}],
                           config=_cfg())
    assert "#42" in out and "export button" in out
    assert "could not parse" in out.lower()            # honest, never a silent "all clear"


def test_promotion_wrong_typed_inputs_never_raise():
    out = report.promotion(None, None, None, None, None, None)
    assert isinstance(out, str) and out


# ------------------------- unattended debugger (issue #66) -------------------------

def test_watchdog_launches_render_and_break_quiet():
    j = [_rec(1030, "watchdog", outcome="launched", id="d1",
              signals=["heartbeat_stale"], authority="full")]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Unattended debugger" in out
    assert "d1" in out and "heartbeat_stale" in out and "full" in out
    assert "nothing happened" not in out.lower()      # an unattended launch is never a quiet night


def test_watchdog_failed_launches_are_honest():
    j = [_rec(1030, "watchdog", outcome="launch_failed", id="d1", rc="no_pane",
              signals=["alert"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "FAILED" in out and "no_pane" in out and "alert" in out
    assert "nothing happened" not in out.lower()


def test_watchdog_notify_only_episodes_stay_quiet():
    # a notified-then-stood-down episode never launched: the journal holds the record, the
    # morning summary stays honest about a night where nothing ultimately happened.
    j = [_rec(1030, "watchdog", outcome="notified", signals=["heartbeat_stale"]),
         _rec(1040, "watchdog", outcome="stand_down", signals=["heartbeat_stale"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "nothing happened" in out.lower()


# --------------------------- runner resurrection (issue #208) ---------------------------

def test_runner_resurrection_renders_and_breaks_quiet():
    j = [_rec(1030, "runner_resurrect", outcome="resurrected", id="r1",
              signals=["heartbeat_stale"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Runner resurrection" in out
    assert "RESTARTED" in out and "r1" in out and "heartbeat_stale" in out
    assert "nothing happened" not in out.lower()      # the runner going down is never a quiet night
    # Pin the honest phrasing (second fresh review): rc==0 proves the PIDFILE went live, nothing
    # more. The reconcile is an inference from what `superlooper run` does, not something this path
    # witnessed — so the line must name the verified fact and infer the rest, never assert
    # "It reconciled from GitHub + disk" as past-tense history. Unpinned, that regresses silently.
    assert "verified live via its pidfile" in out
    assert "It reconciled from GitHub" not in out


def test_runner_resurrection_failure_and_cap_are_honest():
    j = [_rec(1030, "runner_resurrect", outcome="resurrect_failed", id="r4", rc="no_pane",
              signals=["heartbeat_stale"]),
         _rec(1040, "runner_resurrect", outcome="resurrect_capped", attempts=5, max_per_hour=5,
              signals=["heartbeat_stale"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "FAILED" in out and "no_pane" in out
    assert "PAUSED" in out and "5 time" in out
    assert "nothing happened" not in out.lower()


def test_runner_resurrection_cap_line_claims_attempts_not_asserted_restarts():
    # Fresh-review P1-2 (report face): attempts are recorded before delivery, so an undeliverable
    # (no_pane) attempt counts toward the cap without ever restarting anything. The morning line
    # must describe ATTEMPTS, never assert "it was restarted N time(s)" — fabricated history.
    j = [_rec(1040, "runner_resurrect", outcome="resurrect_capped", attempts=5, max_per_hour=5,
              signals=["heartbeat_stale"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "PAUSED" in out and "5 time" in out         # the count still reaches the owner
    assert "attempt" in out.lower()
    assert "it was restarted 5 time" not in out


def test_runner_resurrection_disabled_report_line_is_honest():
    # max_per_hour=0 (auto-restart disabled): the report must say DISABLED, not "restarted 0 time(s)".
    j = [_rec(1040, "runner_resurrect", outcome="resurrect_capped", attempts=0, max_per_hour=0,
              signals=["heartbeat_stale"])]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "DISABLED" in out and "0 time" not in out
    assert "nothing happened" not in out.lower()


# --------------------------- notify-channel canary (issue #164) ---------------------------
# The daily morning push doubles as the channel heartbeat: the runner journals its delivery result
# as `notify_canary`, and the report surfaces it here — the owner-read, out-of-band surface a
# silently-dead channel could never reach (once dead for days, found only by reading the journal).

def test_dead_notify_channel_is_surfaced_loudly_in_the_report():
    j = [_rec(1000, "notify_canary", ok=False, channel="imessage", rc=1,
              detail="osascript: not authorized to send", outcome="ok")]
    out = report.morning(j, _view(), ledger={}, config=_cfg())
    low = out.lower()
    assert "notify channel" in low
    # the owner must read that pushes are NOT reaching the phone, with the failing channel + reason
    assert "imessage" in low and ("not reaching" in low or "dead" in low or "not deliver" in low)
    assert "osascript: not authorized" in out


def test_healthy_notify_channel_reads_as_confirmed():
    j = [_rec(1000, "notify_canary", ok=True, channel="cmd", rc=0, detail="", outcome="ok")]
    out = report.morning(j, _view(), ledger={}, config=_cfg())
    low = out.lower()
    assert "notify channel" in low and ("healthy" in low or "delivered" in low or "confirmed" in low)


def test_log_only_channel_is_named_as_unconfigured_not_healthy():
    # log-only means NOTHING is configured — reporting that as "healthy" would hide the real gap.
    j = [_rec(1000, "notify_canary", ok=True, channel="log-only", rc=0, detail="", outcome="ok")]
    out = report.morning(j, _view(), ledger={}, config=_cfg())
    low = out.lower()
    assert "notify channel" in low and "no" in low and "configured" in low


def test_latest_canary_wins_and_absence_reads_as_not_verified():
    # newest record wins; with no canary at all the report says so honestly (never a false green).
    j = [_rec(1000, "notify_canary", ok=False, channel="imessage", rc=1, detail="x", outcome="ok"),
         _rec(1005, "notify_canary", ok=True, channel="imessage", rc=0, detail="", outcome="ok")]
    out = report.morning(j, _view(), ledger={}, config=_cfg())
    assert "not reaching" not in out.lower() and "dead" not in out.lower()   # latest is healthy

    none_out = report.morning([], _view(now=0, queue=[], usage=None), ledger={}, config=_cfg())
    assert "notify channel" in none_out.lower() and "not verified" in none_out.lower()


# --------------------------- the triage flight's section (issue #449) ---------------------------
# The flight's acts reach the owner exactly where every other overnight act does. The section is
# SILENT on a day with no run: a heading reading "None." over a delegation that never flew would
# put a standing question in front of the owner every single morning.

def _triage_night():
    return [
        _rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launched",
             detail="3 open issue(s) changed since the last recorded verdicts"),
        _rec(1031, "triage_merge", num=21, date="2026-07-02", absorber=20,
             detail="absorbed into #20", outcome="ok"),
        _rec(1032, "triage_close", num=30, date="2026-07-02", verdict="overtaken",
             commit="abc1234", ledger=None, detail="overtaken by abc1234", outcome="ok"),
        _rec(1033, "triage_close", num=40, date="2026-07-02", verdict="nit(N3)", rubric="N3",
             ledger=12, detail="nit under N3, filed in the ledger (#12)", outcome="ok"),
        _rec(1034, "triage_fix", num=45, date="2026-07-02", fixed=["labels"],
             detail="fixed labels (underspecified)", outcome="ok"),
        _rec(1035, "triage_escalate", num=50, date="2026-07-02", held=False,
             finding="the body asks which database to use",
             recommend="decide the engine, then I will fix the body",
             line="- **#50** Storage — the body asks which database to use\n"
                  "  - **Recommend:** decide the engine, then I will fix the body",
             detail="the body asks which database to use", outcome="ok"),
        _rec(1036, "triage_finish", date="2026-07-02",
             counts={"judged": 5, "merged": 1, "closed": 2, "ledger": 1, "fixed": 1,
                     "escalated": 1},
             detail="judged 5 · 1 merged · 2 closed (1 to the ledger) · 1 fixed · 1 escalated",
             outcome="ok"),
    ]


def test_the_triage_section_renders_the_runs_counts_and_links():
    out = report.morning(_triage_night(), _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" in out
    # the tally the run log carries, repeated verbatim so the two surfaces cannot disagree
    assert "1 merged" in out and "2 closed (1 to the ledger)" in out
    assert "1 fixed" in out and "1 escalated" in out
    # every act names its issue as a LINK — the owner reads the report, then the issue
    for num in (21, 30, 40, 45, 50):
        assert f"https://github.com/{REPO}/issues/{num}" in out
    assert "#20" in out and "abc1234" in out and "N3" in out
    # the sitting sheet's recommendation is what makes an escalation actionable at breakfast
    assert "decide the engine" in out


def test_a_triage_run_breaks_the_quiet_night():
    out = report.morning(_triage_night(), _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "nothing happened" not in out.lower()


def test_the_triage_section_is_absent_on_a_day_with_no_run():
    out = report.morning(_full_journal(), _view(), ledger={}, config=_cfg())
    assert "## Triage" not in out
    out = report.morning([], _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" not in out


def test_a_flight_that_only_looked_still_renders_but_claims_nothing():
    # A flight that judged a queue and found nothing to do IS news — it is the delegation working —
    # but it must not be dressed up as activity. The tally says what happened; no act lines follow.
    j = [_rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launched", detail="1 changed"),
         _rec(1031, "triage_keep", num=10, date="2026-07-02", verdict="buildable",
              detail="kept (buildable)", outcome="ok"),
         _rec(1032, "triage_finish", date="2026-07-02",
              counts={"judged": 1, "merged": 0, "closed": 0, "ledger": 0, "fixed": 0,
                      "escalated": 0},
              detail="judged 1 · 0 merged · 0 closed (0 to the ledger) · 0 fixed · 0 escalated",
              outcome="ok")]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" in out and "judged 1" in out
    assert "https://github.com/%s/issues/10" % REPO not in out, (
        "a kept issue gets silence on GitHub and silence here — only acts are reported")


def test_a_refused_triage_act_reaches_the_owner():
    # The delegation's edges refusing IS the audit trail. A flight that tried to close a reopened
    # issue, or cited a commit that never landed, is something the owner should see once.
    j = [_rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launched", detail="x"),
         _rec(1031, "triage_refused", num=70, date="2026-07-02", verdict="overtaken",
              detail="a previous flight closed #70 ... it has since been REOPENED",
              outcome="refused")]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" in out and "REFUSED" in out and "#70" in out


def test_a_failed_triage_launch_is_reported_not_swallowed():
    j = [_rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launch failed (rc=124)",
              detail="timed out")]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" in out and "FAILED" in out and "t7" in out
    assert "nothing happened" not in out.lower()


def test_the_triage_section_never_raises_on_wrong_typed_records():
    j = [_rec(1030, "triage_close", num=None, date=7, ledger="twelve", detail=None),
         _rec(1031, "triage_escalate", num="fifty", line=42, recommend=None),
         _rec(1032, "triage_finish", counts="not a dict", detail=None)]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert isinstance(out, str) and out


def test_the_summary_tally_names_the_triage_run():
    """The summary line IS the push body. A night on which a flight closed three issues must not
    reach the owner's phone as "0 merged · 0 parked · queue: 1" — the one surface that would make
    an autonomous delegation invisible is the one it has to be visible on."""
    out = report.morning(_triage_night(), _view(queue=[], usage=None), ledger={}, config=_cfg())
    summary = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")][0]
    assert "triage" in summary.lower()
    assert "1 merged" in summary and "2 closed" in summary and "1 escalated" in summary


def test_a_night_with_no_flight_leaves_the_summary_exactly_as_it_was():
    out = report.morning(_full_journal(), _view(), ledger={}, config=_cfg())
    summary = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")][0]
    assert "triage" not in summary.lower()


def test_refusal_lines_are_bounded_and_never_flood_the_report():
    """A refusal's reason is written for the FLIGHT — it can name a whole label vocabulary. The
    owner's morning surface gets the gist and a pointer, never the wall."""
    long_reason = "`type:buidl` is not a label this engine registers. Choose from: " + \
                  ", ".join("label-%02d" % i for i in range(40))
    j = [_rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launched", detail="x")]
    j += [_rec(1040 + i, "triage_refused", num=90 + i, date="2026-07-02",
               detail=long_reason, outcome="refused") for i in range(9)]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    section = out.split("## Triage", 1)[1].split("\n## ", 1)[0]
    for line in section.splitlines():
        assert len(line) <= 320, line
    # the cap is STATED, never silent
    assert "4 more refusal" in section
    assert section.count("REFUSED on #") == 5


def test_a_flight_that_died_before_finishing_still_reports_what_it_closed():
    """`triage-finish` writes the tally, and a session can die before running it. What it already
    did to the queue is not conditional on it having tidied up afterwards."""
    j = [_rec(1030, "triage_launch", id="t7", date="2026-07-02", outcome="launched", detail="x"),
         _rec(1031, "triage_close", num=30, date="2026-07-02", verdict="overtaken",
              commit="abc1234", ledger=None, detail="overtaken by abc1234", outcome="ok"),
         _rec(1032, "triage_merge", num=21, date="2026-07-02", absorber=20,
              detail="absorbed into #20", outcome="ok")]
    out = report.morning(j, _view(queue=[], usage=None), ledger={}, config=_cfg())
    assert "## Triage" in out and "#21" in out and "#30" in out
    summary = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")][0]
    assert "Triage: 1 merged · 1 closed · 0 escalated." in summary
