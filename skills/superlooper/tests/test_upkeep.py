"""`superlooper upkeep` — the weekly once-over as ONE read-only command (issue #200).

Two halves, the same split the rest of the engine uses:

  * the PURE renderer (``lib/upkeep.py``) — census arithmetic and the one-page layout, unit-tested
    here against hand-built views, including every fail-closed wrong-typed shape;
  * the CLI verb, driven END-TO-END as a real subprocess against the fixture home + fake gh that
    ``test_cli.py`` already builds, asserting the thing the issue's Boundaries actually promise:
    **nothing anywhere changes**. Not the repo, not the state home, not GitHub.

The read-only contract is not a comment — it is asserted three ways: a byte-for-byte snapshot of
the state home and the repo before/after, an empty gh mutation log (fake-gh appends one line per
WRITE), and the notify channel proving itself from the JOURNAL's canary rather than by sending.
`doctor --stack` sends one live message on purpose; upkeep must not, so it passes the
``stack_doctor.SKIP_SEND`` sentinel and reports the channel's last real delivery instead.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import report
import upkeep

from test_cli import cli, rig, mutations, ALL_LABELS, _stack_env  # noqa: F401  (rig is a fixture)


def report_notify_canary_at(ts):
    """report.notify_canary over a single delivered canary stamped `ts`, aged against NOW/WEEK —
    the exact call upkeep's CLI makes, so the freshness bound is tested through the real function."""
    return report.notify_canary(
        [{"ts": ts, "act": "notify_canary", "ok": True, "channel": "imessage", "rc": 0}],
        now=NOW, max_age_seconds=WEEK)

_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _ROOT.parent.parent

WEEK = 7 * 24 * 3600
NOW = 1_700_000_000.0


def _upkeep_env(rig):                                     # noqa: F811
    """The env every `upkeep` CLI call runs under.

    `cmd_upkeep` builds a REAL stack_doctor.Probe, so — exactly like `doctor --stack` — it would
    shell the host's real `claude`, `codex` and `defaults` if their SL_* overrides were absent
    (Probe.command() falls back to shutil.which; conftest's delenv does not stop that). `_stack_env`
    stubs all of them. But we KEEP the fixture-driven fake-gh (rig's own SL_GH), not `_stack_env`'s
    minimal gh stub, so the branch/janitor census reads the committed gh fixtures rather than an
    empty sweep. This is the CLAUDE.md ratchet: no test may reach a real external binary.
    """
    env = _stack_env(rig)
    env["SL_GH"] = rig.env["SL_GH"]
    env["GH_FIXTURES"] = rig.env["GH_FIXTURES"]
    return env


# --------------------------------------------------------------------------------------------
# the pure renderer
# --------------------------------------------------------------------------------------------

def _view(**over):
    view = {
        "repo": "o/r", "repo_path": "/tmp/repo", "state_home": "/tmp/home", "date": "2026-07-21",
        "stack": [{"name": "gh auth", "ok": True, "warn": False, "detail": ""}],
        "engine_drift": {"status": "in_sync", "behind": 0, "installed_sha": "abc1234",
                         "ref": "origin/main", "detail": "up to date"},
        "janitor": {"error": None, "proposals": [], "held": []},
        "doc_lint": {"status": "clean", "docs": 12, "findings": [], "detail": ""},
        "branches": upkeep.branch_census({}, []),
        "worktrees": upkeep.worktree_census([], {}, {}),
        "notify": {"status": "healthy", "channel": "imessage", "rc": None, "detail": ""},
        "week": upkeep.week_counts([], NOW),
    }
    view.update(over)
    return view


def _text(view):
    return "\n".join(upkeep.render(view))


def test_the_report_carries_every_section_the_dod_names():
    out = _text(_view())
    for label in upkeep.SECTIONS:
        assert label in out, "the one-page report dropped the %r section" % label


def test_a_clean_week_reads_as_clean_and_still_says_it_changed_nothing():
    out = _text(_view())
    assert "changed nothing" in out
    assert "clean" in out


def test_week_counts_counts_only_successful_in_window_records():
    records = [
        {"ts": NOW - 10, "act": "post_question", "num": 5, "outcome": "ok"},
        {"ts": NOW - 20, "act": "post_question", "num": 5, "outcome": "ok"},
        {"ts": NOW - 30, "act": "post_question", "num": 9, "outcome": "ok"},
        # a FAILED question never reached the owner — it is not a question asked
        {"ts": NOW - 40, "act": "post_question", "num": 11, "outcome": "fail"},
        # older than the window
        {"ts": NOW - WEEK - 1, "act": "post_question", "num": 12, "outcome": "ok"},
        {"ts": NOW - 50, "act": "park", "num": 5, "outcome": "ok"},
        {"ts": NOW - 60, "act": "park", "num": 6, "needs_william": True, "outcome": "ok"},
        {"ts": NOW - 70, "act": "bounce", "num": 7, "outcome": "ok"},
        {"ts": NOW - 80, "act": "merge", "num": 8, "outcome": "ok"},
        {"ts": NOW - 90, "act": "merge", "num": 9, "outcome": "ok"},
    ]
    c = upkeep.week_counts(records, NOW)
    assert c["questions"] == 3
    assert c["question_issues"] == 2
    assert c["parks"] == 2
    assert c["needs_owner"] == 1
    assert c["bounces"] == 1
    assert c["merges"] == 2


def test_week_counts_merges_include_absorbed_out_of_band_landings():
    """`absorb_merged` is a landing too — a PR that merged on GitHub between merge and poll.

    The morning report's Merged section counts both (report._MERGE_ACTS); upkeep's merge
    denominator must agree, or "3 questions across N merges" understates N.
    """
    records = [{"ts": NOW - 10, "act": "merge", "num": 1, "outcome": "ok"},
               {"ts": NOW - 20, "act": "absorb_merged", "num": 2, "outcome": "ok"}]
    assert upkeep.week_counts(records, NOW)["merges"] == 2


def test_week_counts_fails_closed_on_garbage():
    for junk in (None, "records", 7, [None, "x", 3, {"act": "park"}]):
        c = upkeep.week_counts(junk, NOW)
        assert set(c) == {"questions", "question_issues", "parks", "needs_owner",
                          "bounces", "merges"}
        assert all(isinstance(v, int) for v in c.values())


def test_a_record_with_no_timestamp_is_kept_rather_than_silently_dropped():
    # journal.append always stamps a ts, so an unstamped record is a corrupt line. Honest
    # over-reporting beats a silently-shrinking count (report._in_window's own posture).
    c = upkeep.week_counts([{"act": "park", "num": 3, "outcome": "ok"}], NOW)
    assert c["parks"] == 1


def test_branch_census_separates_proposed_deletions_from_branches_that_stay():
    branches = {"main": "aaa", "sl/i5-a": "bbb", "sl/i7-b": "ccc", "sl/i9-c": "ddd"}
    proposals = [{"kind": "branch", "target": "sl/i5-a"}, {"kind": "issue", "target": 3}]
    c = upkeep.branch_census(branches, proposals)
    assert c["total"] == 4 and c["sl"] == 3
    assert c["proposed"] == 1
    # the two the janitor will NOT touch are the census's whole point: they are the ones a human
    # has to look at, because nothing mechanical can prove their work landed.
    assert c["kept"] == 2


def test_branch_census_fails_closed_on_wrong_typed_input():
    c = upkeep.branch_census("not-a-dict", "not-a-list")
    assert c == {"total": 0, "sl": 0, "proposed": 0, "kept": 0}
    # a wrong-typed (unhashable) proposal target must be skipped, never raise inside the set build
    c = upkeep.branch_census({"sl/i5-a": "x"}, [{"kind": "branch", "target": ["a"]}])
    assert c == {"total": 1, "sl": 1, "proposed": 0, "kept": 1}


def test_worktree_census_names_the_checkouts_that_hold_unsaved_work():
    issues = {"i5": {"status": "merged"}, "i7": {"status": "parked"},
              "i9": {"status": "running"}, "i11": {"status": "bounced"}}
    blocks = {"i9": None, "i11": "dirty+unpushed", "i7": None, "i5": None}
    c = upkeep.worktree_census(["i5", "i7", "i9", "i11"], issues, blocks)
    assert c["total"] == 4
    # park-family + provably saved -> the runner's opt-in reaper could take it
    assert c["reclaimable"] == ["i7"]
    # holds the only copy of its work: named, never proposed for reclaim
    assert c["held"] == [{"id": "i11", "status": "bounced", "block": "dirty+unpushed"}]
    # an in-flight lane is a LIVE checkout: neither reclaimable nor a finding
    assert "i9" not in [r["id"] for r in c["held"]] and "i9" not in c["reclaimable"]


def test_a_live_lanes_dirty_worktree_is_not_a_finding():
    """A worker writes in its worktree; of course it is dirty.

    Found by running `upkeep` against this repo — the very lane building this feature was the first
    thing the report flagged. A weekly page that prints a line every week for the healthiest
    possible reason teaches its reader to skim, so a live lane is excluded whatever its git state.
    `merged` is excluded too: its checkout rides the merge-time removal path.
    """
    issues = {"i9": {"status": "running"}, "i10": {"status": "gating"},
              "i11": {"status": "merged"}}
    blocks = {"i9": "dirty", "i10": "dirty+unpushed", "i11": "dirty"}
    c = upkeep.worktree_census(["i9", "i10", "i11"], issues, blocks)
    assert c["held"] == []
    assert c["total"] == 3


def test_an_orphan_checkout_holding_work_is_still_a_finding():
    """No lane record at all AND unsaved work: nothing tracks it, nothing will come back for it.

    This is the case a status-based exclusion could quietly swallow, so it is pinned separately.
    """
    c = upkeep.worktree_census(["i42"], {}, {"i42": "unpushed"})
    assert c["held"] == [{"id": "i42", "status": "(no lane record)", "block": "unpushed"}]


def test_worktree_census_fails_closed_on_wrong_typed_input():
    c = upkeep.worktree_census(None, None, None)
    assert c["total"] == 0 and c["reclaimable"] == [] and c["held"] == []
    # a wrong-typed lane record is not a status: it reads as "no lane record", never as a live lane
    # that would silently exclude a stranded checkout from the report
    c = upkeep.worktree_census(["i1"], {"i1": {"status": []}}, {"i1": "dirty"})
    assert [h["id"] for h in c["held"]] == ["i1"]


def test_a_drifting_engine_names_the_one_gated_publish_door():
    out = _text(_view(engine_drift={"status": "behind", "behind": 12, "installed_sha": "abc1234",
                                    "ref": "origin/main", "detail": "12 behind"}))
    assert "12" in out
    assert "bin/install.sh" in out, "a drift notice that does not name the publish door is a chore"


def test_janitor_proposals_are_listed_with_the_command_that_approves_them():
    props = [{"kind": "branch", "key": "branch:sl/i5-a", "action": "delete-branch",
              "target": "sl/i5-a", "why": "PR #34 merged — the work is on the mainline"}]
    out = _text(_view(janitor={"error": None, "proposals": props, "held": []}))
    assert "sl/i5-a" in out and "PR #34 merged" in out
    assert "superlooper janitor" in out


def test_a_refused_janitor_read_is_reported_not_swallowed():
    out = _text(_view(janitor={"error": "state/issues.json is unreadable", "proposals": [],
                               "held": []}))
    assert "unreadable" in out


def _notify_line(text):
    """The `notify` ROW itself — the label line, not the wrapped remedy under it (which contains
    the word 'notify' inside `notify.imessage_to`). Tested directly so the assertion cannot pass by
    reading the wrong line."""
    for line in text.splitlines():
        if line.startswith("notify"):
            return line
    raise AssertionError("no notify row in:\n" + text)


def test_a_dead_notify_channel_is_loud_and_a_log_only_channel_is_not_green():
    dead = _text(_view(notify={"status": "dead", "channel": "imessage", "rc": 2,
                               "detail": "recipient file gone"}))
    assert "DEAD" in dead and "recipient file gone" in dead
    unconfigured = _text(_view(notify={"status": "unconfigured", "channel": "log-only",
                                       "rc": None, "detail": ""}))
    row = _notify_line(unconfigured)
    assert "NO CHANNEL CONFIGURED" in row
    assert "healthy" not in row.lower(), "a log-only channel must never read as green"


def test_a_stale_delivery_is_downgraded_from_healthy_to_unverified():
    """A DELIVERED canary older than the report window is not 'healthy' — it is unproven.

    This is the weekly-cadence exposure: the morning report's canary is minutes old, but a channel
    nothing has exercised in a week must not read green on the once-over.
    """
    fresh = report_notify_canary_at(NOW - 100)
    stale = report_notify_canary_at(NOW - WEEK - 100)
    assert fresh["status"] == "healthy"
    assert stale["status"] == "unverified" and "ago" in stale["detail"]
    # rendered, the stale row says why and names the command that proves the channel
    out = _text(_view(notify=stale))
    assert "not verified" in out and "superlooper doctor --stack" in out


def test_the_stack_summary_names_every_failure_and_warning_but_not_the_passes():
    stack = [{"name": "gh auth", "ok": False, "warn": False, "detail": "not logged in"},
             {"name": "notify channel", "ok": True, "warn": True, "detail": "not sent"},
             {"name": "cmux present", "ok": True, "warn": False, "detail": "/bin/true"}]
    out = _text(_view(stack=stack))
    assert "gh auth" in out and "notify channel" in out
    assert "cmux present" not in out, "a one-page report lists what needs looking at, not the passes"
    assert "superlooper doctor --stack" in out


def test_doc_lint_findings_are_shown_and_a_skip_says_why():
    out = _text(_view(doc_lint={"status": "findings", "docs": 12,
                                "findings": ["README.md: dead verb `superlooper resurrect`"],
                                "detail": ""}))
    assert "resurrect" in out
    skipped = _text(_view(doc_lint={"status": "skipped", "docs": 0, "findings": [],
                                    "detail": "not a superlooper source checkout"}))
    assert "not a superlooper source checkout" in skipped


def test_a_garbage_stack_entry_never_reads_as_a_pass():
    """A non-dict entry is corrupt, not a passing block. Counting it as `ok` would read a broken
    stack as a greener one — the fail-open-on-wrong-typed defect class pointing the wrong way."""
    out = _text(_view(stack=["junk", {"name": "gh auth", "ok": True, "warn": False}]))
    row = next(ln for ln in out.splitlines() if ln.startswith("stack"))
    assert "1 ok" in row and "1 unreadable" in row


def test_a_multi_line_stack_detail_is_collapsed_to_one_line():
    """A `claude auth status --json` dump on a FAIL is multiple lines; the one-page layout assumes
    one line per sub-entry, so the detail is whitespace-collapsed."""
    blob = '{\n  "loggedIn": true,\n  "authMethod": "api_key"\n}'
    out = _text(_view(stack=[{"name": "claude login", "ok": False, "warn": False, "detail": blob}]))
    detail_lines = [ln for ln in out.splitlines() if "claude login" in ln]
    assert len(detail_lines) == 1
    assert "\n" not in detail_lines[0] and "loggedIn" in detail_lines[0]


def test_render_fails_closed_on_wrong_typed_census_counts():
    """render()'s docstring promises 'fails closed to an honest row rather than raising' — so a
    wrong-typed count in a hand-built view must never reach a `%d` and raise."""
    # would have raised `TypeError: %d format: a real number is required, not str` before the fix
    out = _text(_view(branches={"sl": "x", "kept": None, "proposed": []},
                      worktrees={"total": "nope", "reclaimable": "x", "held": "y"}))
    assert "branches" in out and "worktrees" in out
    # and the whole page still renders — no row swallowed the ones after it
    for label in upkeep.SECTIONS:
        assert label in out


# --------------------------------------------------------------------------------------------
# the CLI verb, end to end
# --------------------------------------------------------------------------------------------

def _snapshot(root):
    """Every file under `root`, path -> bytes. The read-only contract, byte for byte."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            try:
                out[str(p.relative_to(root))] = p.read_bytes()
            except OSError:
                out[str(p.relative_to(root))] = b"<unreadable>"
    return out


def _seed_home(rig, *, canary=True):
    """A state home with a week of journal history the CLI will actually read.

    The timestamps hang off the REAL clock: the CLI stamps its 7-day window from ``time.time()``,
    so a fixed epoch would drop every record out of the window and the counts would read zero for
    the wrong reason.
    """
    home = Path(rig.env["SL_HOME"]) / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "worktrees" / "i7").mkdir(parents=True, exist_ok=True)
    now = time.time()
    records = [
        {"ts": now - 100, "act": "post_question", "num": 5, "id": "i5", "outcome": "ok"},
        {"ts": now - 200, "act": "park", "num": 7, "id": "i7", "needs_william": True,
         "memo": "needs a call", "outcome": "ok"},
        {"ts": now - 300, "act": "merge", "num": 9, "id": "i9", "outcome": "ok"},
    ]
    if canary:
        records.append({"ts": now - 400, "act": "notify_canary", "date": "2026-07-20", "ok": True,
                        "channel": "imessage", "rc": 0, "outcome": "ok"})
    (home / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (home / "state" / "issues.json").write_text(json.dumps(
        {"issues": {"i7": {"status": "parked", "branch": "sl/i7-old-thing"},
                    "i9": {"status": "merged", "branch": "sl/i9-done"}}}))
    return home


@pytest.fixture
def upkeep_rig(rig):                                       # noqa: F811
    """The test_cli rig plus a state home carrying a week of journal history."""
    _seed_home(rig)
    return rig


def test_upkeep_runs_read_only_and_exits_zero(upkeep_rig):
    rig = upkeep_rig
    home = Path(rig.env["SL_HOME"])
    before_home, before_repo = _snapshot(home), _snapshot(rig.repo)

    r = cli(rig, "upkeep", "--repo", str(rig.repo), env_over=_upkeep_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for label in upkeep.SECTIONS:
        assert label in out, "the CLI report dropped %r:\n%s" % (label, out)
    # nothing moved, anywhere
    assert _snapshot(home) == before_home, "upkeep wrote to the state home"
    assert _snapshot(rig.repo) == before_repo, "upkeep wrote to the repo"
    assert mutations(rig) == [], "upkeep made a GitHub WRITE"


def test_upkeep_reads_the_journal_for_the_weeks_counts_and_the_notify_canary(upkeep_rig):
    r = cli(upkeep_rig, "upkeep", "--repo", str(upkeep_rig.repo), env_over=_upkeep_env(upkeep_rig))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    # the canary the morning report journaled — NOT a fresh send (and seeded recent, so within the
    # weekly freshness window)
    assert "healthy" in out and "imessage" in out
    assert "1 owner question" in out
    assert "1 park" in out


def test_upkeep_never_sends_a_notify_message(rig):                     # noqa: F811
    """`doctor --stack` proves the channel by SENDING; upkeep must not — it is read-only.

    The config's notify.cmd touches a file, so a send is visible. And with NO canary in the journal
    there is no evidence of delivery either, which is the case that would tempt a report into
    sending one: upkeep must report the channel as UNVERIFIED and name the command that proves it,
    not prove it itself.
    """
    _seed_home(rig, canary=False)
    beacon = rig.tmp / "notify-fired"
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "touch %s" % beacon, "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))

    r = cli(rig, "upkeep", "--repo", str(rig.repo), env_over=_upkeep_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    assert not beacon.exists(), "upkeep sent a notify message — it is read-only"
    assert "not verified" in r.stdout
    assert "superlooper doctor --stack" in r.stdout


def test_the_folded_stack_blocks_are_real_doctor_block_names():
    """`upkeep` folds a WARN out of its stack summary when a dedicated row already covers it.

    That fold is keyed on the block NAME, so a rename in stack_doctor would silently stop folding
    (harmless noise) — or, worse, a typo here would fold nothing and nobody would notice. Pin both
    names against the doctor's own live block list.
    """
    import doc_lint
    live = doc_lint.live_doctor_blocks(_ROOT / "skill" / "lib" / "stack_doctor.py")
    for name in upkeep.STACK_BLOCKS_WITH_OWN_ROW:
        assert name in live, "%r is no longer a doctor --stack block name" % name


def test_a_failing_block_with_its_own_row_is_never_folded_away():
    """Folding is for WARNs only. `notify channel` FAILs when NO channel is configured at all —
    something the canary row cannot say, because there is no canary to report."""
    class _R:
        def __init__(self, name, ok, warn, detail=""):
            self.name, self.ok, self.warn, self.detail = name, ok, warn, detail

    folded = upkeep.fold_stack([
        _R("notify channel", True, True, "imessage configured; NOT sent"),
        _R("installed engine current", True, True, "12 behind"),
        _R("cmux present", True, False, "/bin/true"),
    ])
    assert [f["name"] for f in folded] == ["cmux present"]

    kept = upkeep.fold_stack([_R("notify channel", False, False, "notify.cmd is empty")])
    assert [f["name"] for f in kept] == ["notify channel"]
    assert "notify.cmd is empty" in _text(_view(stack=kept))


def test_upkeep_reports_the_branch_and_worktree_census(upkeep_rig):
    r = cli(upkeep_rig, "upkeep", "--repo", str(upkeep_rig.repo), env_over=_upkeep_env(upkeep_rig))
    assert r.returncode == 0, r.stdout + r.stderr
    # branches.json carries main + two sl/* branches — assert the real rendered row, so the COUNT
    # is checked, not just the presence of the substring "sl/*"
    assert "2 `sl/*` branches on the remote" in r.stdout, r.stdout
    # the fixture home has exactly one worktree on disk (i7, parked)
    assert "1 on disk" in r.stdout


def test_upkeep_marks_the_gh_backed_rows_unread_when_github_is_down(upkeep_rig):
    """gh reads fail closed to empty — correct for the acting verbs, wrong for a weekly glance.

    "0 branches, nothing to propose" for a repo that was simply unreachable is the exact reassuring
    page upkeep must never fabricate. With GH_FAIL the probe fails and both rows must say UNREAD,
    while the report still exits 0 (it is a report, not a gate).
    """
    env = {**_upkeep_env(upkeep_rig), "GH_FAIL": "1"}
    r = cli(upkeep_rig, "upkeep", "--repo", str(upkeep_rig.repo), env_over=env)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines() if ln and not ln.startswith(" ")}
    assert "not read — gh was unreachable" in lines.get("janitor", ""), r.stdout
    assert "not read — gh was unreachable" in lines.get("branches", ""), r.stdout
    # ...and it still changed nothing
    assert mutations(upkeep_rig) == []


def test_upkeep_refuses_an_unadopted_repo_and_names_adopt(rig):        # noqa: F811
    (rig.repo / ".superlooper" / "config.json").unlink()
    r = cli(rig, "upkeep", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "adopt" in (r.stdout + r.stderr)


def test_upkeep_is_a_registered_verb_the_ops_docs_may_name():
    """The doc lint's manifest reads verbs out of the CLI's argparse. Pin that `upkeep` is in it,
    so a doc naming `superlooper upkeep` is legal and a future rename fails loudly here."""
    import doc_lint
    assert "upkeep" in doc_lint.live_verbs(_ROOT / "skill" / "bin" / "superlooper")


def test_upkeep_verb_has_no_execute_flag():
    """Issue #200 Boundaries: 'adding an --execute to upkeep is out of scope and stays out.'

    A read-only contract asserted by tests can be widened by one argparse line; this makes that
    line fail here first.
    """
    r = subprocess.run([sys.executable, str(_ROOT / "skill" / "bin" / "superlooper"),
                        "upkeep", "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    for flag in ("--execute", "--yes", "--apply", "--fix"):
        assert flag not in r.stdout, "upkeep grew %s — it is read-only by contract" % flag
