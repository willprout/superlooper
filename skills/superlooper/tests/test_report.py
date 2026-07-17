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


def test_full_report_matches_golden():
    out = report.morning(_full_journal(), _view(),
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
