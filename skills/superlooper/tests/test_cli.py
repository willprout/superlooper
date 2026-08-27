"""The `superlooper` CLI (plan Task 10): run / status / adopt / doctor + the Task-11/12 stubs.

Invoked as a real subprocess (argparse, exit codes, output — the William-facing contract).
Everything external is injected: fake-gh via SL_GH, the state base via SL_HOME, HOME pointed
at a tmp dir for the shim/hooks checks, a stub cmux via SL_CMUX, a stub jq on PATH.

The one plan-named hard requirement: doctor (and adopt's printout) FAIL HARD when
`required_checks` is empty — a repo with no CI check enforcing its tests has no mechanical
§4.3 gate, so adoption requires at least one (cross-review C3).
"""
import json
import os
import re
import shutil
import select
import subprocess
import time
import sys
from pathlib import Path

import pytest

import janitor as janitor_lib
import labels as labels_lib
import limitations
import loopstate

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

ALL_LABELS = ["agent-ready", "in-progress", "needs-owner", "parked",
              # the #163 question hand-back's control label (#337). adopt must CREATE it — gh
              # refuses to apply a label that does not exist, which is exactly how #310 froze for
              # ~9h on 2026-08-04 — and unlike the owner verbs below, the RUNNER applies this one,
              # so it is also boot-healed (that runner-managed split is pinned in test_labels.py).
              "awaiting-answer",
              "expedite",
              "preserve", "auto-approved:nightly-red", "superseded",
              # the owner's explicit rebuild-from-scratch verb (#161). adopt must CREATE it — gh
              # refuses to apply a label that does not exist — but the runner never APPLIES it (that
              # owner-applied split is pinned in test_labels.py).
              "rebuild",
              # the owner's referee pre-authorization (#165). adopt must CREATE it — gh refuses to
              # apply a label that does not exist, so an unseeded label is a grant he cannot make —
              # but the runner never APPLIES it (that split is pinned in test_labels.py).
              "pre-authorized:referee",
              "priority:high", "priority:low",
              "type:build", "type:investigate", "type:diagnose-and-fix",
              # per-issue model/effort control knobs — gh refuses to apply a label that does not
              # exist, so adopt must seed the starter set (owner ruling 2026-07-07).
              "model:opus", "model:opus[1m]", "model:fable", "model:sonnet",
              "effort:low", "effort:medium", "effort:high", "effort:xhigh", "effort:max",
              # the provenance family (#400): WHO FILED an issue, display-and-filter only. adopt
              # seeds all six — gh refuses the whole `issue create` for a label the repo lacks, so
              # an unseeded value is an instruction no session can follow — and `source:qa` alone is
              # also boot-healed, because the ENGINE applies that one (split pinned in
              # test_labels.py).
              "source:orchestration", "source:build", "source:investigation", "source:debugger",
              "source:qa", "source:dashboard-flag",
              # the marker on the repo's ONE pinned limitations ledger issue (#450). adopt creates
              # the label and THEN creates the issue that carries it — `gh issue create` is
              # all-or-nothing on labels, so an unseeded marker means no ledger at all.
              "limitations-ledger"]

RULE_START = "<!-- loop-standing-rules:start -->"
RULE_END = "<!-- loop-standing-rules:end -->"
RULE_REQUIRED_SNIPPETS = [
    "Approval is the repo owner's word",
    "`agent-ready` is never applied by an agent",
    "Read the parked-issue memo before re-approving",
    "Reviews are performed by a fresh agent",
    "shared mutable defaults",
    "fail-open on wrong-typed input",
    "No metered or paid spend",
    "Never work in the loop's own checkout",
]


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "userhome"
    (home / ".superlooper").mkdir(parents=True)
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    (fixdir / "label_list.json").write_text(json.dumps([{"name": n} for n in ALL_LABELS]))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for stub in ("jq",):
        p = bindir / stub
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    # required_checks match the names the committed gh fixtures report (pr_list.json rollup +
    # check_runs.json), so the doctor's issue-#26 name cross-check passes on a healthy repo.
    (repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["review/local-gate", "quality-gate"]}))
    env = {**os.environ,
           "HOME": str(home), "SL_HOME": str(tmp_path / "slhome"),
           "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
           "SL_CMUX": "/bin/ls",
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    env.pop("GH_FAIL", None)
    # a healthy shim + hooks footprint (doctor checks these)
    (home / ".superlooper" / "launch-shim.zsh").write_text("# shim")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": "activity-hook.sh", "Stop": "stop-hook.sh"}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": "activity-hook.sh", "Stop": "stop-hook.sh"}}))
    return type("Rig", (), {"env": env, "repo": repo, "fixdir": fixdir,
                            "home": home, "tmp": tmp_path})


def cli(rig, *args, env_over=None, inp=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env, timeout=60, input=inp)


def mutations(rig):
    p = rig.fixdir / "mutations.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def standing_rules_block(text):
    start = text.index(RULE_START)
    end = text.index(RULE_END, start) + len(RULE_END)
    return text[start:end]


# --------------------------- doctor ---------------------------

def test_doctor_ok_when_everything_is_healthy(rig):
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "required_checks" in r.stdout


def test_doctor_fails_hard_on_empty_required_checks(rig):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": []}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "required_checks" in r.stdout + r.stderr
    # a repo with no CI check enforcing its tests has no mechanical gate: the message says why
    assert "check" in (r.stdout + r.stderr).lower()


def test_doctor_fails_on_invalid_config(rig):
    (rig.repo / ".superlooper" / "config.json").write_text("{not json")
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0


def test_doctor_fails_when_gh_is_unreachable(rig):
    r = cli(rig, "doctor", "--repo", str(rig.repo), env_over={"GH_FAIL": "1"})
    assert r.returncode != 0
    assert "gh" in (r.stdout + r.stderr).lower()


def test_doctor_fails_on_missing_labels(rig):
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": "agent-ready"}]))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "preserve" in r.stdout + r.stderr        # names what's missing


def test_doctor_fails_when_shim_is_missing(rig):
    (rig.home / ".superlooper" / "launch-shim.zsh").unlink()
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "shim" in (r.stdout + r.stderr).lower()


def test_doctor_fails_when_the_dev_branch_is_missing_on_origin(rig):
    # issue #28: the worktree base is origin/<dev_branch>. If that branch does not exist on the
    # remote, every launch dies at worktree creation. doctor must FAIL and NAME the branch, so the
    # cause is caught at adoption time, not chased through a "shim not installed?" park memo.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "dev_branch": "develop",
         "required_checks": ["review/local-gate", "quality-gate"]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo), env_over={"GH_MISSING_BRANCHES": "develop"})
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "develop" in out                          # the failure NAMES the missing branch
    assert "FAIL" in out


def test_doctor_passes_when_the_dev_branch_exists(rig):
    # the healthy repo's dev_branch (default "main") exists on origin -> the new check must pass.
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "main" in r.stdout and "dev_branch" in r.stdout.lower()


def test_doctor_warns_when_codex_hooks_are_missing(rig):
    (rig.home / ".codex" / "hooks.json").unlink()
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "WARN" in out
    assert "Codex activity hooks registered" in out
    assert "hooks.json" in out


# ------------- doctor: required_checks name cross-check (issue #26) -------------

def _set_checks(rig, checks):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": checks}))


def test_doctor_healthy_repo_passes_the_check_name_cross_check(rig):
    # the rig's required_checks match what the fixtures report on PRs and the dev branch
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr


def test_doctor_flags_a_typo_with_a_case_or_shape_hint(rig):
    # config says "Quality Gate" but the repo reports "quality-gate": a name it cannot find
    _set_checks(rig, ["Quality Gate"])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "Quality Gate" in out                       # names the offending config entry
    assert "quality-gate" in out                       # case/shape hint -> the real reported name


def test_doctor_fails_a_never_wired_required_check(rig):
    _set_checks(rig, ["nonexistent-check"])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "nonexistent-check" in (r.stdout + r.stderr)


def test_doctor_flags_a_check_that_reports_on_prs_but_never_on_dev(rig):
    # the 2026-07-09 incident shape: reported on PRs (pr_list.json) but never on the dev branch.
    _set_checks(rig, ["quality-gate"])
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "review/local-gate", "status": "completed",
                         "conclusion": "success"}]}))   # dev reports OTHER checks, never quality-gate
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "quality-gate" in out and "dev" in out.lower()


def test_doctor_flags_a_check_that_reports_on_dev_but_never_on_prs(rig):
    # the mirror of pr-only: reported on the dev branch but never on recent PRs — every PR reads
    # pending forever, so the green PR never merges (Codex R1).
    _set_checks(rig, ["quality-gate"])
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 700, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "review/local-gate",
             "status": "COMPLETED", "conclusion": "SUCCESS"}]}]))   # PRs never report quality-gate
    # dev branch DOES report quality-gate
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "quality-gate", "status": "completed",
                         "conclusion": "success"}]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "quality-gate" in out and "PR" in out


def test_doctor_passes_a_pr_only_check_excluded_from_the_dev_set(rig):
    # issue #52: `ship` gates PR merges but never reports on the dev branch, and the config EXCLUDES
    # it from the dev set. That exclusion is exactly the fix — the doctor must NOT flag it (under the
    # old single-list model this was the 2026-07-09 pr_only FAIL).
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate"]}}))
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ship", "state": "SUCCESS"}]}]))
    # dev reports quality-gate (default check_runs.json) but NEVER ship
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ship" in r.stdout                      # shown in the required_checks display line


def test_doctor_flags_a_dev_required_check_that_never_reports_on_dev(rig):
    # the mis-split the doctor MUST still catch: `ship` is listed as dev-required but reports only on
    # PRs -> the dev-side poll reads pending forever, so a mainline freeze never lifts.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate", "ship"]}}))
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ship", "state": "SUCCESS"}]}]))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "ship" in out and "dev" in out.lower()


def test_doctor_flags_a_pr_required_check_that_reports_only_on_dev(rig):
    # split mirror of the dev-gap case: `ship` is PR-required but reports ONLY on the dev branch,
    # never on a PR -> every PR reads pending forever, so a green PR never merges. Must FAIL.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate"]}}))
    # PRs report quality-gate only (never ship); the dev branch reports quality-gate + ship
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"}]}]))
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "quality-gate", "status": "completed", "conclusion": "success"},
                        {"name": "ship", "status": "completed", "conclusion": "success"}]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "ship" in out and "PR" in out


def test_doctor_warns_when_no_checks_observed_yet(rig):
    # a freshly adopted repo with no CI history: cannot verify names -> WARN, never a hard FAIL.
    (rig.fixdir / "pr_list.json").write_text("[]")
    (rig.fixdir / "check_runs.json").write_text(json.dumps({"check_runs": []}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no checks observed" in (r.stdout + r.stderr).lower()


# ------------- doctor: the dev surface is a WINDOW of commits, not one HEAD (issue #406) -------
# The PR surface is read across ~30 recent PRs; the dev surface was read at the branch's single
# HEAD commit. Minutes after a merge that HEAD carries no check-run object yet, so the doctor
# announced "no check history yet to confirm it runs there" — a FAIL downgraded to a WARN, false
# reassurance — while the commits right behind it carried green runs of that exact check.

def _dev_window(rig, shas):
    (rig.fixdir / "commits.json").write_text(json.dumps([{"sha": s} for s in shas]))


def _dev_commit_checks(rig, sha, names):
    (rig.fixdir / ("check_runs_%s.json" % sha)).write_text(json.dumps(
        {"check_runs": [{"name": n, "status": "completed", "conclusion": "success"}
                        for n in names]}))


def test_doctor_reads_recent_dev_commits_not_only_the_branch_head(rig):
    _set_checks(rig, ["quality-gate"])
    # the incident shape: HEAD (== the branch ref the old read used) reports nothing yet...
    _dev_commit_checks(rig, "main", [])
    _dev_window(rig, ["fresh", "prior"])
    _dev_commit_checks(rig, "fresh", [])
    _dev_commit_checks(rig, "prior", ["quality-gate"])         # ...the commit behind it is green

    r = cli(rig, "doctor", "--repo", str(rig.repo))

    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "no check history" not in out                       # the false reassurance is gone
    assert "every name reports on its expected surface" in out


def test_doctor_still_fails_a_dev_gap_the_whole_window_confirms(rig):
    # The window may only ADD evidence. A dev-required name that reports on NO commit in it is
    # still the 2026-07-09 incident — the dev-side poll reads pending forever — so it still FAILs.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate", "ship"]}}))
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ship", "state": "SUCCESS"}]}]))
    _dev_window(rig, ["c1", "c2", "c3"])
    for sha in ("c1", "c2", "c3"):
        _dev_commit_checks(rig, sha, ["quality-gate"])         # never `ship`, on any commit

    r = cli(rig, "doctor", "--repo", str(rig.repo))

    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "ship" in out and "dev" in out.lower()


def test_doctor_stays_cautious_when_the_dev_window_read_is_refused(rig):
    # An API blip must never manufacture evidence: with nothing observable on dev the doctor holds
    # the cautious answer (a re-runnable "cannot confirm"), never a false ok.
    _set_checks(rig, ["quality-gate"])
    (rig.fixdir / "commits.json").write_text('"a bare string, wrong type"')   # the blip
    _dev_commit_checks(rig, "main", [])                        # and the fallback HEAD read is bare

    r = cli(rig, "doctor", "--repo", str(rig.repo))

    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "quality-gate" in out
    assert "check history" in out                              # the cautious WARN...
    assert "every name reports on its expected surface" not in out   # ...never an ok


# --- doctor's accidental-close audit (issue #229) ----------------------------------------------
# An issue closed as COMPLETED by a bare commit-message keyword reads as fixed while nothing
# shipped (the 2026-07-16 ledger commit that auto-closed the never-built #189). The doctor's job
# here is VISIBILITY — it WARNs and lists the evidence; it never fails the preflight, because
# closing issues from commit messages is ordinary practice in a repo adopting the loop.

def test_doctor_flags_a_keyword_closed_issue_with_the_commit_and_the_missing_branch(rig):
    _seed_closed_issues(rig, _keyword_closed(189, title="The never-built fix"))
    (rig.fixdir / "pr_list_heads.json").write_text("[]")
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    out = r.stdout
    assert "WARN accidental issue closes" in out
    assert "#189" in out and "The never-built fix" in out
    assert "8b79d7ac" in out                                  # the closing commit
    assert "ledger: 07-16 overnight" in out                   # its subject
    # the third column: nothing was ever built for it
    assert "no sl/i189 PR was ever opened" in out and "no sl/i189 branch" in out
    assert "superlooper janitor" in out                       # the one command that acts on it
    assert r.returncode == 0, out + r.stderr                  # a WARN never fails the preflight


def test_doctor_names_the_sl_pr_that_did_exist(rig):
    _seed_closed_issues(rig, _keyword_closed(5))
    (rig.fixdir / "pr_list_heads.json").write_text(json.dumps(
        [{"number": 12, "state": "MERGED", "headRefName": "sl/i5-fix-thing"}]))
    out = cli(rig, "doctor", "--repo", str(rig.repo)).stdout
    assert "#12" in out and "MERGED" in out
    # branches.json still carries sl/i5-fix-thing, so the branch half is named too
    assert "sl/i5-fix-thing still on the remote" in out


def test_doctor_is_clean_when_every_close_has_a_merged_pr_or_the_owners_hand(rig):
    _seed_closed_issues(rig, _pr_closed(150, 242), _owner_closed(98))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok   accidental issue closes" in r.stdout
    assert "all 2 closed issues" in r.stdout                  # the window it actually audited


def test_doctor_does_not_count_a_not_planned_close_as_audited(rig):
    # a NOT_PLANNED close never claimed the work was done; counting it would overstate the audit.
    _seed_closed_issues(rig, _pr_closed(150, 242),
                        {"number": 60, "title": "Won't fix", "stateReason": "NOT_PLANNED",
                         "closedAt": "z", "timelineItems": {"nodes": []}})
    out = cli(rig, "doctor", "--repo", str(rig.repo)).stdout
    assert "all 2 closed issues" in out and "every one of the 1 closed as COMPLETED" in out


def test_doctor_says_when_it_only_audited_a_window(rig):
    # an unqualified "every" is a completeness claim, and the read is bounded. A repo with more
    # closed issues than the bound must be told it was a window, or a clean line over-claims.
    (rig.fixdir / "graphql_ClosedIssueClosers.json").write_text(json.dumps(
        {"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": True, "endCursor": "NEXT"},
            "nodes": [_pr_closed(n, 100 + n) for n in range(1, 6)]}}}}))
    out = cli(rig, "doctor", "--repo", str(rig.repo)).stdout
    assert "ok   accidental issue closes" in out
    assert "most recently updated" in out and "older ones were NOT checked" in out


def test_doctor_never_claims_nothing_was_built_off_an_unreadable_pr_history(rig):
    # the never-built clause is the strongest sentence in the block; a refused PR list produces
    # the same empty answer as a genuinely PR-less issue, and asserting the negative off it would
    # be this very defect class one layer down.
    _seed_closed_issues(rig, _keyword_closed(189))
    (rig.fixdir / "pr_list_heads.json").write_text("{not json")
    out = cli(rig, "doctor", "--repo", str(rig.repo)).stdout
    assert "#189" in out
    assert "nothing was ever built" not in out
    assert "UNPROVEN" in out


def test_doctor_caps_the_printed_list_and_says_how_many_it_left_out(rig):
    _seed_closed_issues(rig, *[_keyword_closed(n, title="Never built %d" % n)
                               for n in range(1, 31)])
    out = cli(rig, "doctor", "--repo", str(rig.repo)).stdout
    assert "30 of all 30 closed issues" in out          # the COUNT is always the true total
    assert "…and 10 more" in out                        # the cap is stated, never silent
    assert out.count("closed by commit") == 20


def test_doctor_says_it_could_not_read_rather_than_giving_a_clean_bill(rig):
    # a refused read must never render as "no accidental closes found" — that is the exact
    # trusted-signal failure this block exists to catch, one layer up.
    (rig.fixdir / "graphql_ClosedIssueClosers.json").write_text("{not json")
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN accidental issue closes" in r.stdout and "refused" in r.stdout
    assert "ok   accidental issue closes" not in r.stdout


def _write_exe(path, body):
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _stack_env(rig, *, gh_remaining=4999):
    bindir = rig.tmp / "stack-bin"
    bindir.mkdir(exist_ok=True)
    codex = _write_exe(
        bindir / "codex",
        "#!/bin/sh\n"
        "if [ \"$1\" = login ] && [ \"$2\" = status ]; then\n"
        "  echo 'Logged in using ChatGPT'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    # The `claude` stub must answer `plugin list --json` too (issue #90): the stack doctor asks it
    # whether the superlooper plugin is installed, and cmd_stack_doctor builds a REAL Probe. Without
    # this arm the stub exits 64, the block degrades to its "could not determine" WARN, and the CLI
    # test would never exercise the real PASS path. Report the plugin installed+enabled, shaped as
    # the real CLI emits it, so the healthy stack is genuinely green everywhere.
    claude = _write_exe(
        bindir / "claude",
        "#!/bin/sh\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ] && [ \"$3\" = --json ]; then\n"
        "  printf '%s\\n' '{\"loggedIn\": true, \"authMethod\": \"claude.ai\"}'; exit 0\n"
        "fi\n"
        "if [ \"$1\" = plugin ] && [ \"$2\" = list ] && [ \"$3\" = --json ]; then\n"
        "  printf '%s\\n' '[{\"id\": \"superlooper@superlooper\", \"version\": \"1.0.0\","
        " \"scope\": \"user\", \"enabled\": true}]'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    gh = _write_exe(
        bindir / "gh",
        "#!/bin/sh\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ]; then exit 0; fi\n"
        "if [ \"$1\" = api ] && [ \"$2\" = rate_limit ]; then\n"
        f"  printf '%s\\n' '{{\"resources\": {{\"core\": {{\"limit\": 5000, \"remaining\": {gh_remaining}}}}}}}'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    cmux = _write_exe(bindir / "cmux", "#!/bin/sh\nexit 0\n")
    # `defaults` MUST be stubbed too (issue #120): the stack doctor now runs
    # `defaults read com.cmuxterm.app NSAppSleepDisabled`, and cmd_stack_doctor builds a REAL Probe.
    # Without this stub the CLI doctor would read the host's real com.cmuxterm.app domain — reaching a
    # real external binary AND making the green-stack assertion depend on the host's actual cmux App
    # Nap setting. Report App Nap disabled (rc 0, "1") so the healthy stack is green everywhere.
    defaults = _write_exe(
        bindir / "defaults",
        "#!/bin/sh\n"
        "if [ \"$1\" = read ] && [ \"$3\" = NSAppSleepDisabled ]; then echo 1; exit 0; fi\n"
        "exit 1\n",
    )
    return {"SL_CODEX": codex, "SL_CLAUDE": claude, "SL_GH": gh, "SL_CMUX": cmux,
            "SL_DEFAULTS": defaults}


def test_doctor_stack_ok_uses_fake_commands_and_mutates_nothing(rig):
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    zshrc = rig.home / ".zshrc"
    zshrc.write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    watched = [cfg_path, zshrc, rig.home / ".superlooper" / "launch-shim.zsh"]
    before = {p: p.read_text() for p in watched}

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for name in ("codex CLI", "cmux present", "claude login", "gh auth",
                 "gh API headroom", "notify channel", "launch shim sourced",
                 "cmux App Nap disabled", "superlooper plugin"):
        assert name in out
    # the plugin block resolved to a real ok (not its "could not determine" WARN) through the stub
    assert "ok   superlooper plugin" in out
    assert "required_checks" not in out
    # the one deliberate side effect is announced before it fires
    assert "sending" in out.lower() and "test" in out.lower()
    assert {p: p.read_text() for p in watched} == before


def test_doctor_stack_flags_a_live_runner_whose_anchor_no_longer_resolves(rig):
    # End-to-end (issue #33): a LIVE runner (pidfile = a live pid) whose recorded pane no longer
    # resolves in cmux FAILs doctor --stack with the manual-restart hint. _stack_env's cmux prints
    # nothing for list-pane-surfaces, so the recorded pane reads as unresolvable — the misplacement.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    state = rig.tmp / "slhome" / "o__r" / "state"
    state.mkdir(parents=True)
    (state / "runner.lock").write_text(str(os.getpid()))          # this test process = a live pid
    (state / "runner.anchor.json").write_text(json.dumps(
        {"pane": "DEADPANE", "workspace": "WS-x", "window": "WIN-x", "pid": os.getpid()}))

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL runner anchor (live)" in out
    assert "DEADPANE" in out
    assert "superlooper run" in out and "runner-ops" in out


def test_doctor_stack_fails_with_actionable_hint(rig):
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": None, "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text("# no shim source\n")

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_stack_env(rig, gh_remaining=0))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL gh API headroom" in out
    assert "Fix: Wait for the hourly GitHub API quota" in out
    assert "FAIL notify channel" in out
    assert "Fix: Set notify.cmd or notify.imessage_to" in out
    assert "FAIL launch shim sourced" in out
    assert "Fix: Run" in out and "install-launch-shim.sh" in out


def test_doctor_stack_flags_missing_app_nap_default(rig):
    # Issue #120 end-to-end: when NSAppSleepDisabled is not set for the cmux bundle, `doctor --stack`
    # must FAIL loudly with the exact remedy — this is the machine that systemically loses launch
    # delivery ~40 min after the operator walks away. The `defaults` stub reports the key absent
    # (rc 1), exactly as the real binary does on an un-nap-proofed machine.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    env = _stack_env(rig)
    env["SL_DEFAULTS"] = _write_exe(rig.tmp / "stack-bin" / "defaults-absent",
                                    "#!/bin/sh\nexit 1\n")   # every read -> "does not exist"

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=env)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL cmux App Nap disabled" in out
    assert "defaults write com.cmuxterm.app NSAppSleepDisabled -bool true" in out
    assert any(w in out.lower() for w in ("relaunch", "restart", "quit"))


def _codexless_stack_env(rig, **kw):
    """A healthy stack env with Codex made unresolvable: SL_CODEX unset AND PATH narrowed so
    `shutil.which('codex')` misses too. codex/claude/gh/cmux/defaults all resolve via their SL_* env
    (SL_DEFAULTS keeps the App Nap read off the host's real com.cmuxterm.app domain), so the only
    unresolved `which` left is for codex; PATH keeps the rig's jq bin plus /usr/bin:/bin so notify's
    `bash -lc` still resolves, but no standard installer ever puts codex in those dirs — so 'Codex is
    absent' stays hermetic regardless of what the host machine has installed."""
    env = _stack_env(rig, **kw)
    del env["SL_CODEX"]
    env["PATH"] = f"{rig.tmp / 'bin'}:/usr/bin:/bin"
    return env


def test_doctor_stack_warns_but_passes_when_codex_absent_on_a_claude_machine(rig):
    # Issue #30: a Claude-only newcomer (config agent defaults to claude) with no Codex installed
    # must still reach an all-green stack. Codex absence is a WARN, not a FAIL, and the exit is 0.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))            # no "agent" key -> defaults to claude
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_codexless_stack_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "WARN codex CLI" in out
    assert "FAIL" not in out
    assert "all stack checks passed" in out


def test_doctor_stack_fails_when_codex_absent_but_config_selects_codex_agent(rig):
    # Issue #30: the mirror case. A machine whose config runs `agent: codex` genuinely needs Codex,
    # so its absence is a hard FAIL with the install hint, exactly as before.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["agent"] = "codex"
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_codexless_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL codex CLI" in out
    assert "Install the Codex CLI" in out
    assert "STACK DOCTOR FAILED" in out


def test_doctor_stack_fails_when_the_notify_test_send_fails(rig):
    # The live 2026-07-10 incident, end to end: notify.cmd is SET but every send exits nonzero
    # (recipient file gone). The doctor must FAIL the block and print rc + the stderr reason,
    # instead of passing because a value was merely configured.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": 'printf "recipient file missing\\n" 1>&2; exit 2', "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL notify channel" in out
    assert "rc=2" in out
    assert "recipient file missing" in out          # the actual error rode onto the FAIL line


def test_doctor_stack_unconfigured_notify_names_config_and_cites_the_canary(rig):
    # Issue #406, end to end: with no channel configured the block still FAILs (the ruling is
    # unchanged — host toasts are not a channel), but it must say WHAT is wrong. Nothing was sent,
    # so nothing failed to deliver; and the journal's last canary DID deliver, which is the
    # evidence that keeps the FAIL from reading as a broken channel.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": None, "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    state_home = Path(rig.env["SL_HOME"]) / "o__r"
    state_home.mkdir(parents=True, exist_ok=True)
    (state_home / "journal.jsonl").write_text(json.dumps(
        {"ts": time.time(), "act": "notify_canary", "date": "2026-08-06",
         "ok": True, "channel": "imessage", "rc": 0, "detail": ""}) + "\n")

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL notify channel" in out
    assert "CONFIGURED" in out and "nothing was sent" in out
    assert "canary" in out.lower() and "imessage" in out
    assert "Fix: Set notify.cmd or notify.imessage_to" in out


# --------------------------- adopt ---------------------------

def test_adopt_writes_config_creates_labels_and_prints_requirements(rig):
    fresh = rig.tmp / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["repo"] == "will/proj"               # detected from the origin remote
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert created == set(ALL_LABELS)
    out = r.stdout
    assert "branch protection" in out.lower()
    assert "required_checks" in out                  # the same at-least-one-check requirement


def test_adopt_yields_web_agnostic_report_sections(rig):
    # issue #57 DoD: a fresh adopt on a fixture repo must yield a section list a NON-WEB worker can
    # honestly satisfy — never the old "Browser evidence" demand that nudged-then-parked every
    # finished issue on a CLI/library/service repo. adopt copies the shipped template, so this is the
    # end-to-end proof that the honest default reaches a freshly adopted repo's config.
    fresh = rig.tmp / "fresh-sections"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["report_required_sections"] == ["Tests", "Review"]
    assert "Browser evidence" not in cfg["report_required_sections"]


def test_adopt_detects_and_writes_the_repo_default_branch(rig):
    # issue #28: on a master/develop repo, dev_branch left at the template's "main" makes every
    # worktree creation fail off origin/main. adopt must detect the repo's real default (via gh)
    # and write it as dev_branch.
    fresh = rig.tmp / "fresh-branch"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    (rig.fixdir / "repo_view.json").write_text(json.dumps({"defaultBranchRef": {"name": "trunk"}}))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["dev_branch"] == "trunk"              # detected default, not the template's "main"
    # the branch-protection printout names the DETECTED branch, not the template default
    assert "`trunk`" in r.stdout


def test_adopt_keeps_the_template_default_branch_when_gh_cannot_detect(rig):
    # gh unreachable: adopt must not crash — it keeps the template's dev_branch ("main") and writes
    # the config. Its GitHub half (label creation) does fail here, so adopt now exits nonzero
    # (issue #29) — but cleanly (a handled exit 1, not a traceback), with the config on disk. doctor
    # is the backstop that later FAILs if that guessed branch is wrong.
    fresh = rig.tmp / "fresh-nogh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh), env_over={"GH_FAIL": "1"})
    assert r.returncode == 1, r.stdout + r.stderr     # labels failed -> nonzero, but no crash
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["dev_branch"] == "main"               # template fallback, config still written


def test_adopt_creates_the_model_and_effort_starter_labels(rig):
    # gh refuses to apply a label that doesn't exist in the repo, so adopt must seed every value
    # William can drop on an issue as a per-issue control knob (starter set, not an allowlist).
    fresh = rig.tmp / "fresh-knobs"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert {"model:opus", "model:opus[1m]", "model:fable", "model:sonnet"} <= created
    assert {"effort:low", "effort:medium", "effort:high", "effort:xhigh", "effort:max"} <= created


def test_readopt_reconciles_a_hand_created_awaiting_answer_label(rig):
    # Issue #337's other half. On 2026-08-04 the only way to unfreeze #310 was a supervised
    # `gh label create awaiting-answer` — so this repo (and any repo that hit the same freeze) now
    # carries a label nobody registered, with whatever colour and description the hand fix chose.
    # Re-adopting must RECONCILE it to the registered spec rather than skip it as "already there"
    # or error on the collision: gh's `--force` makes create-or-update one idempotent call, which
    # is what lets adopt be re-run safely on a repo that self-medicated.
    r = cli(rig, "adopt", "--repo", str(rig.repo))          # the rig's repo already HAS the label
    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig)
               if m["kind"] == "create_label" and m["name"] == "awaiting-answer"]
    assert len(created) == 1, "adopt must reconcile the existing label exactly once: %s" % created
    color, desc = labels_lib.label_spec("awaiting-answer")
    assert created[0]["color"] == color
    assert created[0]["description"] == desc.replace("{operator}", "o")   # the rig's owner login
    assert created[0]["force"] is True, "without --force this is an error on an existing label"


def test_readopt_adds_a_new_starter_label_without_disturbing_the_others(rig):
    # issue #134: a NEW seeded knob (model:sonnet) has to reach repos that were adopted before it
    # existed, and migrations ride adopt — not install (the 2026-07-13 needs-owner storm lesson).
    # Re-running adopt on an ALREADY-adopted repo re-creates the whole set idempotently (--force):
    # the new label appears and nothing else is renamed or removed.
    r = cli(rig, "adopt", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    muts = mutations(rig)
    created = {m["name"] for m in muts if m["kind"] == "create_label"}
    assert "model:sonnet" in created                      # the new knob reaches an adopted repo
    assert set(ALL_LABELS) <= created                     # every pre-existing label survives, re-created
    # adopt never RENAMES an owner knob either: the only rename it performs is the historical
    # needs-william -> needs-owner migration (#58). (Deletion needs no assertion — it is structurally
    # impossible rather than merely untested: skill/lib/gh.py exposes no delete_label at all, so no
    # adopt path can emit one. A test for it would pass vacuously and imply a guard that isn't there.)
    assert not [m for m in muts if m["kind"] == "rename_label" and m["old"].startswith("model:")]


# --------------------------- adopt: the limitations ledger (issue #450) ---------------------------

def _ledger_fixture(rig, *entries):
    """Serve `issue list --label limitations-ledger` from its own fixture (fake-gh prefers
    issue_list_<label>.json over the shared issue_list.json)."""
    (rig.fixdir / "issue_list_limitations-ledger.json").write_text(json.dumps(list(entries)))


def _ledger_entry(num, pinned=True):
    return {"number": num, "labels": [{"name": "limitations-ledger"}], "isPinned": pinned}


def test_adopt_scaffolds_and_pins_the_limitations_ledger(rig):
    # A fresh repo has no ledger, so adopt creates one, marks it, and pins it. The ledger is the
    # durable home for true-but-not-worth-a-lane findings; it lives as a GitHub ISSUE precisely so
    # that changing it never needs a PR — which is what makes it writable by a triage flight.
    fresh = rig.tmp / "fresh-ledger"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert len(created) == 1, "adopt scaffolds exactly one ledger issue: %s" % created
    assert created[0]["labels"] == limitations.LEDGER_LABEL, (
        "the ledger must carry its marker label, or nothing can ever find it again")
    assert created[0]["title"] == limitations.LEDGER_TITLE
    assert created[0]["body"] == limitations.ledger_body()
    assert [m for m in mutations(rig) if m["kind"] == "pin_issue"], "the ledger must be pinned"
    assert "9001" in r.stdout, "adopt must NAME the ledger issue it created"


def test_the_scaffolded_ledger_body_documents_its_own_entry_format(rig):
    # DoD: the entry format lives on the ledger issue's OWN body — rubric line, the limitation's
    # content, a link to the closed source issue. Asserted on what adopt actually SENDS, so a
    # body assembled correctly in the lib but sent from somewhere else still fails here.
    fresh = rig.tmp / "fresh-ledger-body"
    fresh.mkdir()
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    body = [m for m in mutations(rig) if m["kind"] == "create_issue"][0]["body"]
    assert "rubric" in body.lower()
    assert "N1" in body and "N4" in body
    assert re.search(r"#\d+", body), "a worked example entry must show the closed-issue link"


def test_readopt_finds_the_existing_ledger_and_creates_nothing_new(rig):
    # The idempotence DoD, stated as the field states it: `superlooper adopt` is documented as safe
    # to re-run, and a second run that scaffolded a SECOND ledger would split the one place accepted
    # limitations live — the failure that makes the ledger worthless rather than merely untidy.
    _ledger_fixture(rig, _ledger_entry(42))

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"], \
        "a second adopt must create nothing new"
    assert not [m for m in mutations(rig) if m["kind"] == "pin_issue"], \
        "an already-pinned ledger must not be re-pinned — real gh REFUSES a second pin"
    assert "#42" in r.stdout, "adopt must name the ledger it found"


def test_readopt_repins_a_ledger_somebody_unpinned(rig):
    # Found-but-unpinned is the one case where re-adopt still writes: pinning is what makes the
    # ledger the first thing a reader sees, and `isPinned` is exactly why the read asks for it.
    _ledger_fixture(rig, _ledger_entry(42, pinned=False))

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert [m["num"] for m in mutations(rig) if m["kind"] == "pin_issue"] == ["42"]


def test_adopt_ignores_issues_that_do_not_actually_carry_the_marker(rig):
    # The `--label` flag is an argument, not a guarantee. adopt confirms the marker in the payload,
    # so a read that answered with unrelated issues scaffolds a ledger rather than writing into
    # somebody else's issue — the shared issue_list.json fixture is exactly that answer.
    fresh = rig.tmp / "fresh-unmarked"
    fresh.mkdir()
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert len(created) == 1, "the shared fixture's issues carry no marker — scaffold, don't adopt"


def test_adopt_refuses_to_scaffold_a_ledger_when_the_read_was_refused(rig):
    # The #92/#172 refused-vs-answered-empty discipline, at the one call site where "empty" means
    # CREATE. A throttled read that failed closed to [] would scaffold a duplicate ledger on every
    # re-run — so a refused read creates NOTHING and says so, and adopt exits nonzero because its
    # GitHub half is incomplete (the issue-#29 mixed-state rule).
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "--label limitations-ledger", "times": 1,
          "stderr": "API rate limit exceeded"}]))

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 1, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"], \
        "a refused read must never be read as 'this repo has no ledger'"
    assert "ledger" in r.stdout.lower()
    assert "idempotent" in r.stdout.lower() or "re-run" in r.stdout.lower(), \
        "the memo must name the one action that fixes it — adopt is safe to re-run"


def test_adopt_does_not_attempt_the_ledger_when_its_marker_label_failed(rig):
    # `gh issue create` is ALL-OR-NOTHING on labels: with the marker missing, the create is refused
    # outright and the work item silently never exists (#165/#337). So a failed marker create must
    # STOP the scaffold rather than fire a call that cannot succeed.
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "label create limitations-ledger", "times": 1,
          "stderr": "HTTP 403"}]))

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 1, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"], \
        "no ledger create may be attempted while its marker label does not exist"
    assert "limitations-ledger" in r.stdout


def test_adopt_reports_failure_when_the_ledger_could_not_be_pinned(rig):
    # A failed pin is NOT a success (fresh-agent review). Adoption promises a PINNED ledger, so
    # exiting 0 on a pin that never landed would report a success that did not happen — the
    # issue-#29 defect. The ledger itself still stands (create is never undone or re-attempted),
    # and the memo names the real cause: GitHub pins at most three issues per repo.
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue pin", "times": 1, "stderr": "HTTP 422"}]))
    fresh = rig.tmp / "fresh-pinfail"
    fresh.mkdir()

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 1, r.stdout + r.stderr
    assert len([m for m in mutations(rig) if m["kind"] == "create_issue"]) == 1, \
        "the ledger is created once and never undone or re-attempted over a pin refusal"
    assert "NOT PINNED" in r.stdout
    assert "THREE pinned issues" in r.stdout, "the memo must name the cause an operator can act on"
    assert "gh issue pin 9001" in r.stdout, "and the exact hand fix, with the real issue number"


def test_readopt_reports_failure_when_re_pinning_an_existing_ledger_fails(rig):
    # Same rule on the found-but-unpinned path: the ledger is found, the pin is attempted, and a
    # refusal is reported honestly rather than folded into a green re-adopt.
    _ledger_fixture(rig, _ledger_entry(42, pinned=False))
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue pin", "times": 1, "stderr": "HTTP 422"}]))

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 1, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"], \
        "a pin refusal must never be answered by scaffolding a second ledger"
    assert "gh issue pin 42" in r.stdout


def test_adopt_scaffolds_no_ledger_before_gh_is_reachable(rig):
    # gh entirely unreachable: adopt still writes the config (its file half), reports the mixed
    # state, and attempts NO ledger create — the same fail-closed shape as the label half.
    fresh = rig.tmp / "fresh-noledger-nogh"
    fresh.mkdir()
    r = cli(rig, "adopt", "--repo", str(fresh), env_over={"GH_FAIL": "1"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert (fresh / ".superlooper" / "config.json").exists(), "the file half still lands"


def test_adopt_never_overwrites_an_existing_config(rig):
    before = (rig.repo / ".superlooper" / "config.json").read_text()
    r = cli(rig, "adopt", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert (rig.repo / ".superlooper" / "config.json").read_text() == before
    assert "already" in r.stdout.lower()


def test_adopt_creates_claude_md_with_loop_standing_rules(rig):
    fresh = rig.tmp / "fresh-claude"
    fresh.mkdir()

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 0, r.stdout + r.stderr
    text = (fresh / "CLAUDE.md").read_text()
    assert RULE_START in text
    assert RULE_END in text
    block = standing_rules_block(text)
    for snippet in RULE_REQUIRED_SNIPPETS:
        assert snippet in block


def test_adopt_appends_standing_rules_without_touching_existing_claude_md(rig):
    prior = "# Existing CLAUDE.md\n\nKeep this byte-for-byte.\nNo final newline"
    claude = rig.repo / "CLAUDE.md"
    claude.write_text(prior)

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    text = claude.read_text()
    assert text.startswith(prior)
    assert text[len(prior):].startswith("\n\n")
    assert text.count(RULE_START) == 1
    assert text.count(RULE_END) == 1


def test_adopt_preserves_existing_claude_md_bytes_when_appending(rig):
    prior = b"# Existing CLAUDE.md\r\n\r\nKeep this byte-for-byte.\r\nInvalid byte: \xff"
    claude = rig.repo / "CLAUDE.md"
    claude.write_bytes(prior)

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    data = claude.read_bytes()
    assert data.startswith(prior)
    assert data.count(RULE_START.encode()) == 1
    assert data.count(RULE_END.encode()) == 1


def test_adopt_rerun_replaces_the_standing_rules_block_instead_of_duplicating_it(rig):
    fresh = rig.tmp / "fresh-rerun"
    fresh.mkdir()

    first = cli(rig, "adopt", "--repo", str(fresh))
    second = cli(rig, "adopt", "--repo", str(fresh))

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    text = (fresh / "CLAUDE.md").read_text()
    assert text.count(RULE_START) == 1
    assert text.count(RULE_END) == 1


def test_adopted_standing_rules_are_portable_text(rig):
    fresh = rig.tmp / "fresh-portable-rules"
    fresh.mkdir()

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 0, r.stdout + r.stderr
    block = standing_rules_block((fresh / "CLAUDE.md").read_text())
    for forbidden in ("William", "willprout", "owner/name", fresh.name, str(fresh), "superlooper"):
        assert forbidden not in block
    assert not re.search(r"(^|\s)(~?/[^/\s`]+/[^\s`]+)", block)


def test_adopt_exits_nonzero_when_every_label_create_fails(rig):
    # Issue #29: the reported scenario is `adopt` run BEFORE `gh auth login`. Every gh write
    # fails, so not one label exists — but the config IS written. adopt must NOT report success:
    # it exits nonzero, names `gh auth login` as the likely fix, states the mixed state plainly
    # (config kept, labels pending), and says the command is safe to re-run.
    fresh = rig.tmp / "fresh-nogh-labels"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh), env_over={"GH_FAIL": "1"})
    assert r.returncode != 0, r.stdout + r.stderr
    # the config is written and KEPT despite the gh failure (the mixed state the memo must name)
    assert (fresh / ".superlooper" / "config.json").exists()
    out = r.stdout + r.stderr
    assert "gh auth login" in out                     # the likely fix, named
    assert "re-run" in out.lower()                    # safe to re-run (idempotent)
    # the named re-run command must be RUNNABLE: adopt takes --repo, not a positional (a bare
    # `superlooper adopt <path>` argparse-errors), so the memo must spell the flag out.
    assert "adopt --repo" in out
    assert "config" in out.lower() and "pending" in out.lower()   # mixed state, explicit
    assert out.count("FAIL") >= len(ALL_LABELS)       # every label reported as failed


def test_adopt_exits_nonzero_when_some_labels_fail(rig):
    # A partial GitHub blip: two label creates fail, the rest succeed. Even a single failure must
    # flip the exit code — a half-created label set is still a runner that silently can't apply
    # the labels it's missing. The closing summary names the count that failed.
    fresh = rig.tmp / "fresh-partial-labels"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    (rig.fixdir / "fail_rules.json").write_text(json.dumps([
        {"match": "label create agent-ready", "times": 1},
        {"match": "label create type:build", "times": 1}]))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "gh auth login" in out
    assert "2 of %d" % len(ALL_LABELS) in out          # the failed count is reported
    # the two failed creates were NOT recorded (the fake dies before recording); the rest were
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert "in-progress" in created and "effort:max" in created
    assert "agent-ready" not in created and "type:build" not in created


def test_adopt_succeeds_when_all_labels_already_exist(rig):
    # Re-running adopt on a repo whose labels all exist: create-or-update (--force) succeeds for
    # every one, so adopt reports success and exits 0 — no failure guidance on the clean re-run.
    fresh = rig.tmp / "fresh-relabel"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    first = cli(rig, "adopt", "--repo", str(fresh))
    assert first.returncode == 0, first.stdout + first.stderr
    second = cli(rig, "adopt", "--repo", str(fresh))
    assert second.returncode == 0, second.stdout + second.stderr
    both = second.stdout + second.stderr
    assert "gh auth login" not in both                 # no failure guidance on a clean run
    assert "FAIL" not in second.stdout
    assert "already" in second.stdout.lower()          # config already adopted, left untouched


def _fresh_repo(rig, name):
    fresh = rig.tmp / name
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    return fresh


def test_adopt_migrates_the_legacy_needs_william_label(rig):
    # issue #58: a repo adopted before the operator-name change carries `needs-william`. Re-adopt
    # RENAMES it in place to the neutral `needs-owner` (gh label edit preserves it on every issue
    # that carries it) so a stranger's own audit trail stops reading another person's name.
    fresh = _fresh_repo(rig, "legacy")
    legacy = [n for n in ALL_LABELS if n != "needs-owner"] + ["needs-william"]
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": n} for n in legacy]))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    renames = [m for m in mutations(rig) if m["kind"] == "rename_label"]
    assert any(m["old"] == "needs-william" and m["new"] == "needs-owner" for m in renames)
    assert "needs-william -> needs-owner" in r.stdout


def test_adopt_does_not_rename_when_there_is_no_legacy_label(rig):
    # A fresh repo (or one already migrated) never renames — it just creates `needs-owner`.
    fresh = _fresh_repo(rig, "cleanlabels")
    (rig.fixdir / "label_list.json").write_text("[]")
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "rename_label"]
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert "needs-owner" in created and "needs-william" not in created


def test_adopt_label_descriptions_render_the_operator_name(rig):
    # issue #58: the seeded label descriptions sign the operator's name (defaulting to the repo
    # owner login "will"), never a hardcoded "William" and never a raw {operator} placeholder.
    fresh = _fresh_repo(rig, "opnamed")
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    descs = {m["name"]: m["description"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert descs["agent-ready"] == "will's approval: the runner may launch this issue"
    assert descs["parked"] == "handed back to will with a memo (runner-managed)"
    assert not any("William" in d for d in descs.values())
    assert not any("{operator}" in d for d in descs.values())


def test_run_uses_config_agent_and_cli_override(rig):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["ci"], "agent": "codex"}))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=codex" in r.stdout

    r2 = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1",
             "--agent", "claude", "--ticks", "0",
             env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "agent=claude" in r2.stdout


# --------------------------- status ---------------------------

def test_status_renders_lanes_gate_and_frozen(rig):
    state_home = rig.tmp / "slhome" / "o__r"
    (state_home / "state").mkdir(parents=True)
    st = loopstate.new_state()
    st["issues"]["i5"] = dict(loopstate.new_issue(), status="running", branch="sl/i5-x",
                              retries=1)
    st["issues"]["i7"] = dict(loopstate.new_issue(), status="gating")
    st["issues"]["i9"] = dict(loopstate.new_issue(), status="parked")
    loopstate.save(str(state_home / "state" / "issues.json"), st)
    loopstate.save(str(state_home / "state" / "merges_frozen.json"),
                   {"reason": "dev red: ci", "fingerprint": "fp", "since": 1})
    import journal as journal_mod
    journal_mod.append(state_home, {"act": "merge", "id": "i3", "outcome": "ok"}, now=100)
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "i5" in out and "running" in out and "sl/i5-x" in out
    assert "i7" in out and "gating" in out
    assert "FROZEN" in out and "dev red: ci" in out
    assert "merge" in out                            # journal tail is rendered


def test_status_says_the_queue_is_HELD_and_names_the_class(rig):
    """A systemic hold (#320) is the quietest state the loop has — one alert hours ago, then no
    park, no relabel, no text. `status` is where the owner asks "is anything running?", so it must
    answer "held", name the class, and say in the same breath that the hold took no action of its
    own; otherwise a paused queue reads exactly like an idle one."""
    state_home = rig.tmp / "slhome" / "o__r"
    (state_home / "state").mkdir(parents=True)
    loopstate.save(str(state_home / "state" / "issues.json"), loopstate.new_state())
    loopstate.save(str(state_home / "state" / "ALERT"),
                   {"reasons": ["gh_auth_dead_workers"], "since": 1})
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue: HELD" in r.stdout, r.stdout
    assert "gh_auth_dead_workers" in r.stdout
    assert "parks nothing" in r.stdout and "moves no label" in r.stdout


def test_status_says_the_queue_is_flowing_when_no_hold_stands(rig):
    state_home = rig.tmp / "slhome" / "o__r"
    (state_home / "state").mkdir(parents=True)
    loopstate.save(str(state_home / "state" / "issues.json"), loopstate.new_state())
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert "queue: flowing" in r.stdout, r.stdout


def test_status_on_a_never_run_repo_is_calm(rig):
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "never" in r.stdout.lower() or "no runner" in r.stdout.lower()


# --------------------------- run + stubs ---------------------------

def _cmux_stub(rig, *, resolve=True, self_pane=None, workspace="WS-9", window="WIN-9"):
    """A minimal cmux for the run command's pane resolution + D7 preflight. `identify` returns a
    caller.{pane_id,workspace_id,window_id} (self-pane + anchor auto-detection); `list-pane-surfaces`
    prints a surface line (or an rc-0 'Error: not_found', the exit-code trap) per `resolve`; every
    other subcommand fails so a tick's launch attempts stay inert (no real tabs)."""
    # `--id-format uuids` precedes the subcommand, so scan all args for the verb.
    # self_pane="" -> identify yields no pane_id (simulates running outside a cmux surface).
    pane = "SELFPANE" if self_pane is None else self_pane
    caller = (f'{{"pane_id": "{pane}", "workspace_id": "{workspace}", "window_id": "{window}"}}'
              if pane else "{}")
    body = ("#!/bin/sh\n"
            'for a in "$@"; do case "$a" in\n'
            f'  identify) echo \'{{"caller": {caller}}}\'; exit 0 ;;\n'
            '  list-pane-surfaces) '
            + ("echo '  surface:1  tab'; exit 0 ;;\n" if resolve
               else "echo 'Error: not_found'; exit 0 ;;\n")
            + 'esac; done\nexit 1\n')
    p = rig.tmp / ("cmux_ok" if resolve else "cmux_bad")
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def test_run_ticks_once_and_writes_the_heartbeat(rig):
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=claude" in r.stdout
    hb = rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat"
    assert hb.exists()


def test_run_accepts_explicit_codex_agent_selection(rig):
    # CLI plumbing only: --agent codex is accepted and reaches the runner. No launch happens in
    # this one-tick empty queue, so no codex/claude binary can be invoked.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--agent", "codex",
            "--ticks", "1", env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=codex" in r.stdout
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_auto_detects_its_own_pane_without_any_pane_flag(rig):
    # owner request 2026-07-06: no --pane, no $SL_PANE — the runner targets the cmux tab it runs
    # in (cmux identify -> caller.pane_id) and starts cleanly. No hardcoded pane anywhere.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="SELFPANE"),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFPANE" in r.stdout and "this cmux tab" in r.stdout
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_boot_line_carries_the_workspace_and_window_anchor(rig):
    # issue #33: the boot line must name WHERE the runner landed (workspace + window), so a runner
    # started in the wrong cmux window (the 2026-07-09 misplacement) is visible immediately — not an
    # opaque pane UUID that could be any window.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="SELFPANE",
                                            workspace="WS-42", window="WIN-42"),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "workspace=WS-42" in r.stdout and "window=WIN-42" in r.stdout


def test_run_boot_line_omits_anchor_fields_that_do_not_resolve(rig):
    # An explicit --pane in an environment cmux-identify can't answer: the pane still resolves
    # (preflight passes), but workspace/window are simply omitted — never printed as empty noise.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "workspace=" not in r.stdout and "window=" not in r.stdout


def test_run_fails_hard_when_the_pane_will_not_resolve(rig):
    # D7: an unresolvable pane must FAIL HARD before the loop (never a quiet warning that then
    # burns every issue's retry cap). cmux exits 0 with an 'Error: not_found' line; we still refuse.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "ghost", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=False)})
    assert r.returncode != 0
    assert "FATAL" in (r.stdout + r.stderr)
    assert "resolve pane" in (r.stdout + r.stderr).lower()
    # and it never started: no heartbeat written
    assert not (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_fails_hard_when_no_pane_and_not_in_cmux(rig):
    # No --pane, no $SL_PANE, and identify yields nothing (started outside a cmux surface):
    # fail hard, and tell the operator to run inside a cmux tab.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane=""),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode != 0
    assert "no cmux pane" in (r.stdout + r.stderr).lower()
    assert "cmux tab" in (r.stdout + r.stderr).lower()


# ------------- run refuses a repo whose merge gate would require NO checks (issue #401) -------------
# The gate folds an EMPTY required list to vacuously GREEN by design (§C.4 step 5), so a runner
# booted against a fresh or mis-edited config merges every PR with no CI requirement at all —
# silently, every light green. doctor has always failed hard on this, but nothing forces
# adopt -> doctor -> run, so the same D7 rule ("a runner that starts wrong is worse than one that
# never starts") applies at the doorway a runner actually comes through.

def _write_config(rig, **over):
    cfg = {"version": 1, "repo": "o/r", "required_checks": ["review/local-gate", "quality-gate"]}
    cfg.update(over)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))


def test_run_refuses_to_start_when_required_checks_is_empty(rig):
    _write_config(rig, required_checks=[])
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode != 0
    # On STDERR, like every sibling refusal in this boot path: an operator who pipes stdout
    # somewhere (or a job whose stdout is a log nobody reads) must still SEE why it refused.
    assert "FATAL" in r.stderr
    # the three things the refusal must teach: the key, the consequence, the remedy
    assert "required_checks" in r.stderr
    assert "no ci requirement" in r.stderr.lower()
    assert "superlooper doctor" in r.stderr
    # and it never started: the loop's own heartbeat is the proof
    assert not (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_refuses_when_the_object_form_leaves_the_pr_set_empty(rig):
    # `required_checks` may be split by surface (issue #52). An empty DEV set is legitimate (a repo
    # whose CI runs on PRs only); an empty PR set is the vacuous-green hole. Judging through
    # config.pr_required_checks — the accessor the gate itself consumes — is what makes this true
    # by construction rather than by a second, drifting emptiness test.
    _write_config(rig, required_checks={"pr": [], "dev": ["quality-gate"]})
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode != 0
    assert "required_checks" in (r.stdout + r.stderr)
    assert not (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_starts_normally_when_the_object_form_names_a_pr_check(rig):
    # The other half of the same accessor: a non-empty PR set boots, and an empty DEV set beside it
    # is NOT a refusal — the freeze mechanism simply idles (config.dev_required_checks).
    _write_config(rig, required_checks={"pr": ["quality-gate"], "dev": []})
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_an_empty_required_checks_config_still_loads_while_run_refuses_it(rig):
    # Both halves in ONE test, because they are one decision: the loader deliberately keeps an empty
    # `required_checks` LOADABLE so a freshly-adopted stub can be inspected — doctor reaches its own
    # hard fail, which it could not do if load() rejected the file — and `run` is what refuses to
    # boot on it. Tightening the loader instead would break adopt/doctor on the very repo they exist
    # to fix; this pins that split so a later "fix" cannot quietly close it at the wrong layer.
    _write_config(rig, required_checks=[])
    d = cli(rig, "doctor", "--repo", str(rig.repo))
    assert "ok   config" in d.stdout, d.stdout + d.stderr        # the file LOADED
    assert "required_checks" in d.stdout and d.returncode != 0   # ...and doctor still fails on it
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode != 0 and "FATAL" in (r.stdout + r.stderr)


# ------------- request-restart: the command-center Restart button's shell (issue #116) -------------

def _state_dir(rig):
    return rig.tmp / "slhome" / "o__r" / "state"


def test_request_restart_drops_the_marker_when_a_runner_is_live(rig):
    # A LIVE runner (its pidfile pid is alive — this test process stands in) ⇒ the request lands: the
    # marker is dropped in the STATE HOME (never .superlooper/**), carrying the audit fields the
    # runner journals when it honors the restart.
    state = _state_dir(rig)
    state.mkdir(parents=True)
    (state / "runner.lock").write_text(str(os.getpid()))
    r = cli(rig, "request-restart", "--repo", str(rig.repo), "--json",
            "--operator", "William", "--source", "command-center")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["ok"] is True and body["running"] is True and body["requested"] is True
    marker = json.loads((state / "runner.restart").read_text())
    assert marker["source"] == "command-center" and marker["operator"] == "William"
    assert isinstance(marker["requested_at"], (int, float))
    assert marker["target_pid"] == os.getpid()               # bound to the runner it checked live


def test_request_restart_refuses_and_names_the_manual_start_when_no_runner_is_live(rig):
    # Dead-runner case: a STALE pidfile (dead pid) ⇒ the button makes NO attempt to launch or place
    # anything. It reports plainly that no loop is running and shows the one-line manual start.
    state = _state_dir(rig)
    state.mkdir(parents=True)
    (state / "runner.lock").write_text("999999")             # a dead pid → no live runner
    r = cli(rig, "request-restart", "--repo", str(rig.repo), "--json")
    assert r.returncode != 0
    body = json.loads(r.stdout)
    assert body["ok"] is False and body["running"] is False
    assert "superlooper run" in body["manual"]               # the one-line manual start
    assert not (state / "runner.restart").exists()           # nothing written, nothing launched


def test_request_restart_with_no_pidfile_at_all_refuses(rig):
    # The loop never ran here (no state home) — still the dead-runner path, and it creates nothing.
    r = cli(rig, "request-restart", "--repo", str(rig.repo), "--json")
    assert r.returncode != 0
    body = json.loads(r.stdout)
    assert body["running"] is False and body["ok"] is False


def test_request_restart_check_reports_liveness_without_writing(rig):
    # --check is the button's preflight (like tidy --dry-run): it reports whether a live runner
    # exists and writes NOTHING, so the confirm dialog can decide what to show before the owner taps.
    state = _state_dir(rig)
    state.mkdir(parents=True)
    (state / "runner.lock").write_text(str(os.getpid()))
    r = cli(rig, "request-restart", "--repo", str(rig.repo), "--check", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["running"] is True and body.get("requested") in (False, None)
    assert not (state / "runner.restart").exists()           # --check writes nothing


# ------------- run: runner-managed label boot preflight (issue #108) -------------

def _cli_module():
    """Import the extensionless CLI as a module so its pure helpers can be unit-tested. Safe: the
    file guards its entrypoint behind `if __name__ == '__main__'`, and conftest already put
    skill/lib + skill/bin on sys.path so its imports resolve."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("superlooper_cli", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_runner_managed_labels_is_the_tagged_subset():
    # the runner-managed subset is derived from the '(runner-managed)' tag in the LABELS
    # descriptions, so the LABELS list stays the single source of truth.
    sl = _cli_module()
    assert set(sl.runner_managed_labels()) == {"in-progress", "needs-owner", "parked",
                                               "awaiting-answer", "source:qa"}


def test_missing_runner_labels_pure():
    sl = _cli_module()
    _MANAGED = {"in-progress", "needs-owner", "parked", "awaiting-answer", "source:qa"}
    assert sl.missing_runner_labels(set(ALL_LABELS)) == []
    assert sl.missing_runner_labels(
        {"agent-ready", "in-progress", "parked", "awaiting-answer", "source:qa"}) == ["needs-owner"]
    assert set(sl.missing_runner_labels(set())) == _MANAGED
    assert set(sl.missing_runner_labels([])) == _MANAGED                       # list ok
    assert set(sl.missing_runner_labels("garbage")) == _MANAGED


def test_run_creates_a_missing_runner_managed_label_at_boot(rig):
    # issue #160 (extends #108): the 2026-07-13 incident was `needs-owner` not existing, so every
    # bounce/park label move retried forever. The runner now SELF-HEALS at boot — it CREATES the
    # missing runner-managed label (an already-installed migration step, applied idempotently) —
    # instead of #108's fail-loud refusal, then boots and ticks normally.
    (rig.fixdir / "label_list.json").write_text(json.dumps(
        [{"name": n} for n in ALL_LABELS if n != "needs-owner"]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert "needs-owner" in created                        # the missing label was created at boot
    # it booted and made progress: a heartbeat is written
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_holds_when_a_boot_migration_cannot_apply(rig):
    # issue #160: a migration that cannot be applied HOLDS the boot rather than storming. The
    # needs-owner create is forced to fail, so the runner refuses the tick loop (no heartbeat) and
    # writes a legible systemic hold (state/ALERT naming the migration) — one signal, not a per-tick
    # storm of failing writes.
    (rig.fixdir / "label_list.json").write_text(json.dumps(
        [{"name": n} for n in ALL_LABELS if n != "needs-owner"]))
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "label create", "times": 9}]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "HELD" in (r.stdout + r.stderr)                 # names the hold
    home = rig.tmp / "slhome" / "o__r"
    assert not (home / "state" / "runner.heartbeat").exists()   # the loop never ticked
    alert = json.loads((home / "state" / "ALERT").read_text())
    assert any("migration_hold" in x and "needs-owner" in x for x in alert["reasons"])


def test_run_boots_when_all_runner_managed_labels_present(rig):
    # the all-present case: the healthy rig has ALL_LABELS -> the preflight passes and the runner
    # ticks normally.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_skips_the_label_preflight_when_gh_is_unreachable(rig):
    # a transient gh blip at boot must NOT block a restart: a refused label read fails closed
    # (ok=False) and SKIPS the check, and fix-1's bounded-storm guards cover a genuinely-missing
    # label until the next doctor/adopt. gh unreachable -> boot proceeds; the tick's own poll marks
    # the view stale and simply waits.
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": "agent-ready"}]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True), "GH_FAIL": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_boots_when_the_label_read_is_refused_but_gh_probes_ok(rig):
    # P1 (issue #108 review, the #92 refused-vs-answered-empty class): `gh api rate_limit` (probe) is
    # EXEMPT from rate limiting, so during a core-throttle window it reads OK while the label LIST
    # read is throttled to a fail-closed empty set. The preflight must read that as a REFUSED read
    # (ok=False) and SKIP — never as "every runner-managed label missing" — or it would wedge the
    # boot during the very rate-limit window this issue hardens against. label_list.json is intact
    # (all labels present), but the ONE `label list` call is forced to fail, mimicking the throttle.
    (rig.fixdir / "fail_rules.json").write_text(json.dumps([{"match": "label list", "times": 1}]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


# --------------------------- promotion + accept-failure (Task 12) ---------------------------

def test_accept_failure_persists_into_the_ledger(rig):
    fp = "abc123def456abcd"
    r = cli(rig, "accept-failure", fp, "--note", "known flaky widget", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    home = rig.tmp / "slhome" / "o__r"
    led = json.loads((home / "ledger.json").read_text())
    assert fp in led and led[fp]["note"] == "known flaky widget"


def test_promote_report_use_latest_nightly_is_evidence_only(rig):
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "last_nightly.json").write_text(json.dumps(
        {"date": "2026-07-01", "ok": True,
         "failures": [{"test_id": "t::regression", "text": "new boom after PR #40"}]}))
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrote" in r.stdout
    reports = sorted((home / "reports").glob("promotion-*.md"))
    assert reports
    text = reports[0].read_text()
    assert "evidence only" in text.lower() and "must pass" not in text.lower()   # §4.6 bright line
    assert "t::regression" in text                                               # new failure shown


def test_promote_report_wrong_typed_cached_ok_is_not_treated_as_parsed(rig):
    # Codex R2 C3: a corrupt/hand-edited last_nightly.json with a truthy-but-wrong-typed `ok`
    # ("false", {}, 1, …) must NOT render as parsed evidence ("No new failures") — that would be
    # a silent all-clear. Require ok is True; anything else is the could-not-parse path.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "last_nightly.json").write_text(json.dumps(
        {"date": "2026-07-01", "ok": "false", "failures": []}))     # ok is a STRING, not True
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("promotion-*.md"))[0].read_text()
    assert "could not parse" in text.lower()
    assert "no new failures" not in text.lower()


def test_promote_report_missing_nightly_is_a_clean_error(rig):
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 1
    assert "no stored nightly" in (r.stdout + r.stderr).lower()


def test_promote_report_fresh_suite_runs_and_writes(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "promote-report", "--repo", str(rig.repo),
            env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    home = rig.tmp / "slhome" / "o__r"
    assert sorted((home / "reports").glob("promotion-*.md"))


# --------------------------- nightly QA (Task 12) ---------------------------

def _write_qa(rig, qa):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["ci"], "qa": qa}))


def _write_junit(path, failing):
    if failing:
        path.write_text('<testsuites><testsuite tests="1" failures="1">'
                        '<testcase classname="pkg" name="test_x">'
                        '<failure message="boom">at line 5</failure></testcase>'
                        '</testsuite></testsuites>')
    else:
        path.write_text('<testsuites><testsuite tests="1" failures="0">'
                        '<testcase classname="pkg" name="test_x"/></testsuite></testsuites>')


def _nightly_records(rig):
    home = rig.tmp / "slhome" / "o__r"
    jp = home / "journal.jsonl"
    recs = [json.loads(x) for x in jp.read_text().splitlines()] if jp.exists() else []
    return home, [r for r in recs if r.get("act") == "nightly"]


def test_nightly_null_cmd_is_a_clean_noop(rig):
    r = cli(rig, "nightly", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "null" in r.stdout.lower() and "nothing to run" in r.stdout.lower()


def test_nightly_persistent_failure_freezes_and_files_a_standing_rule_issue(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=True)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml", "retry_once": True})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert len(created) == 1
    # the exact standing-rule label set (§4.4 audit trail) + the filer's own provenance (#400) +
    # the runner's fingerprint dedup marker
    assert created[0]["labels"] == \
        "type:diagnose-and-fix,agent-ready,auto-approved:nightly-red,expedite,source:qa"
    assert "Failure fingerprint:" in created[0]["body"]
    home, recs = _nightly_records(rig)
    fm = json.loads((home / "state" / "merges_frozen.json").read_text())
    assert fm["source"] == "nightly"                                 # nightly claims freeze ownership
    assert recs and recs[-1]["persistent"] == 1 and recs[-1]["green"] is False


def test_nightly_seeds_its_own_provenance_label_before_filing(rig, tmp_path):
    """Issue #400 + fresh-agent review (Codex, 2026-08-06), P0.

    The standalone `superlooper nightly` runs on its own schedule and NEVER runs the runner's boot
    migration — so on a repo adopted before #400, `source:qa` simply would not exist there. `gh
    issue create` is ALL-OR-NOTHING for labels, so the whole call is refused: the merges freeze, and
    the fix issue that is supposed to lift it is never filed. The nightly self-heals the one label
    it adds, create-or-forced immediately before the create (the same defense the dashboard's Flag
    verb already uses), so the filer cannot be taken down by its own new label.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=True)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml", "retry_once": True})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    muts = mutations(rig)
    lab = [m for m in muts if m["kind"] == "create_label"]
    assert [m["name"] for m in lab] == ["source:qa"]        # exactly the one label it introduced
    assert lab[0]["force"] is True                          # idempotent on a repo that has it
    assert "{operator}" not in lab[0]["description"]        # signed, never a raw placeholder
    iss = next(m for m in muts if m["kind"] == "create_issue")
    assert muts.index(lab[0]) < muts.index(iss)             # seeded BEFORE the create, not after


def test_nightly_still_files_on_a_repo_whose_gh_refuses_the_unseeded_label(rig, tmp_path):
    """The proof the seeding above is load-bearing, driven through a gh that behaves like the real
    one: `GH_LABEL_NOT_IN_REPO` makes `issue create` refuse outright for a label the repo lacks.

    This is the #165/#337 defect class the suite could never see — every test set labels as Python
    strings, so a label gh would REFUSE looked exactly like one it would accept, and a green suite
    proved nothing twice. With the fake refusing, the create-or-force is what keeps the red-nightly
    fix issue getting filed at all.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=True)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml", "retry_once": True})
    r = cli(rig, "nightly", "--repo", str(rig.repo),
            env_over={"SL_NIGHTLY_WORKTREE": str(wt),
                      # the label exists only once the nightly creates it; the fake refuses any
                      # `issue create` naming it BEFORE that, exactly as gh does on an unadopted repo.
                      "GH_LABEL_NOT_IN_REPO": "source:qa"})
    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert len(created) == 1, "the fix issue must still be filed — a red mainline with no fix " \
                              "issue is a freeze nothing can lift"


def test_green_nightly_clears_its_own_freeze(rig, tmp_path):
    # Codex R2 C2 (review test 2): the next green nightly unfreezes a nightly-owned freeze.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text(json.dumps(
        {"reason": "nightly red", "source": "nightly", "since": 1}))
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (home / "state" / "merges_frozen.json").exists()      # nightly cleared its own freeze


def test_green_nightly_leaves_a_dev_check_freeze_alone(rig, tmp_path):
    # a green nightly must NOT clear a runner dev-check freeze — that one is the runner's to clear.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text(json.dumps(
        {"reason": "dev red: ci", "source": "dev-check", "since": 1}))
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "state" / "merges_frozen.json").exists()          # dev-check freeze untouched


def test_nightly_green_freezes_nothing_and_files_nothing(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert [m for m in mutations(rig) if m["kind"] == "create_issue"] == []
    home, recs = _nightly_records(rig)
    assert not (home / "state" / "merges_frozen.json").exists()
    assert recs[-1]["green"] is True


def test_nightly_unparseable_results_are_honest_never_a_silent_green(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_qa(rig, {"nightly_cmd": "true", "results_glob": "results/*.xml"})   # produces no results
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 1                                          # nonzero: could not confirm
    home, recs = _nightly_records(rig)
    assert not (home / "state" / "merges_frozen.json").exists()       # no freeze on could-not-parse
    assert [m for m in mutations(rig) if m["kind"] == "create_issue"] == []
    assert recs[-1]["parse_error"] is True and recs[-1]["green"] is False


def test_morning_report_treats_a_corrupt_freeze_marker_as_frozen(rig):
    # a present-but-non-dict freeze marker must read as FROZEN (existence = frozen), never flowing
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text('["nightly red"]')   # valid JSON, wrong type
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("morning-*.md"))[0].read_text()
    assert "FROZEN" in text


def test_morning_report_writes_and_reflects_the_journal(rig):
    import journal
    home = rig.tmp / "slhome" / "o__r"
    journal.append(str(home), {"act": "merge", "id": "i5", "num": 5, "pr": 9, "outcome": "ok"})
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrote" in r.stdout
    reports = sorted((home / "reports").glob("morning-*.md"))
    assert reports, "no morning report written"
    text = reports[0].read_text()
    assert "superlooper morning report" in text
    # the seeded merge is reflected, cross-linked against the repo (o/r)
    assert "#5" in text and "https://github.com/o/r/pull/9" in text


def test_morning_report_renders_standing_holds_from_loopstate(rig):
    # Issue #405: the hand-run report is the SECOND entry point (runner.py's own hook is the first)
    # and the two must assemble the same view — a hold visible only when the runner happens to write
    # the 08:45 report would be exactly the silence this issue closes. Also pins the tolerant read:
    # a corrupt/absent issues.json renders a report with no holds, never a crash.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    loopstate.save(str(home / "state" / "issues.json"),
                   {"version": 1, "issues": {"i5": {"status": "ready",
                                                    "launch_hold_reason": "waiting on #77",
                                                    "launch_hold_since": time.time() - 3 * 86400}}})
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("morning-*.md"))[0].read_text()
    section = text.split("## Standing holds")[1].split("\n## ")[0]
    assert "#5" in section and "waiting on #77" in section and "3d 0h" in section
    assert "STALL" in text                                   # past the threshold -> alert tier

    (home / "state" / "issues.json").write_text("{{{ not json")
    for f in (home / "reports").glob("morning-*.md"):
        f.unlink()
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("morning-*.md"))[0].read_text()
    assert "None — nothing is held." in text.split("## Standing holds")[1]


# --------------------------- D1: gh pinned to config.repo, never cwd ---------------------------

def _recording_gh(rig):
    """A wrapper SL_GH that records $GH_REPO per invocation, then behaves exactly like fake-gh —
    the assertion surface for 'every gh call this CLI makes carries the config repo'."""
    record = rig.tmp / "gh-env.log"
    wrapper = rig.tmp / "gh-wrapper"
    wrapper.write_text("#!/bin/sh\n"
                       'printf "%s\\n" "${GH_REPO:-}" >> "' + str(record) + '"\n'
                       'exec "' + str(_FAKE_GH) + '" "$@"\n')
    wrapper.chmod(0o755)
    return record, wrapper


def test_doctor_pins_gh_to_config_repo_not_cwd(rig):
    # D1 (live dry-run 2026-07-03): run the CLI from an UNRELATED cwd — every gh call must
    # still carry config.repo via GH_REPO, not fall back to gh's cwd-remote inference.
    record, wrapper = _recording_gh(rig)
    elsewhere = rig.tmp / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "doctor", "--repo", str(rig.repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = record.read_text().splitlines()
    assert lines and all(l == "o/r" for l in lines), (lines, r.stdout)


def test_adopt_pins_gh_to_the_adopted_repo(rig):
    # already-adopted path: label creation must target the config's repo from any cwd
    # (the fresh-adopt/origin-detection path is covered separately below)
    record, wrapper = _recording_gh(rig)
    elsewhere = rig.tmp / "elsewhere-adopt"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "adopt", "--repo", str(rig.repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = record.read_text().splitlines()
    assert lines and all(l == "o/r" for l in lines), (lines, r.stdout)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_fresh_adopt_pins_gh_to_the_detected_origin(rig):
    # fresh adopt: no config yet — the pin must come from the origin-detected slug that adopt
    # just wrote into the config, so the label set lands in the DETECTED repo from any cwd
    record, wrapper = _recording_gh(rig)
    repo = rig.tmp / "fresh-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=rig.env, timeout=30)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/det/ected.git"], check=True, env=rig.env, timeout=30)
    elsewhere = rig.tmp / "elsewhere-fresh"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "adopt", "--repo", str(repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((repo / ".superlooper" / "config.json").read_text())
    assert cfg["repo"] == "det/ected"
    lines = record.read_text().splitlines()
    assert lines and all(l == "det/ected" for l in lines), (lines, r.stdout)


# --------------------------- tidy (close finished session windows) ---------------------------
# `superlooper tidy` is William's explicit word — never automatic (V1 'nothing auto-closed').
# It closes the cmux windows of FINISHED sessions (default: merged; --all: every terminal
# status) and NEVER an in-flight lane. The close mirrors the runner's _close_stale_session:
# `cmux close-surface --surface <uuid> [--workspace <ws>]`, rc ignored, then the pane markers +
# singleton lock are cleared.

def _tidy_home(rig):
    return rig.tmp / "slhome" / "o__r"


def _seed_tidy_state(rig, issues, panes):
    """issues: {iid: status}. panes: {iid: (pane, workspace)} — the recorded handle, in the shape
    lib/panes writes it. Also drops a worker lock per lane so a test can assert tidy frees the
    singleton lock like _close_stale_session does.

    Both halves are seeded now (issue #334): the doorway closes a WORKSPACE, so a handle with no
    workspace names nothing it may act on — which is the point, since "close whatever is at that
    pane" is how a stale handle ends someone else's window."""
    home = _tidy_home(rig)
    (home / "state" / "panes").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    for iid, status in issues.items():
        st["issues"][iid] = dict(loopstate.new_issue(), status=status, branch=f"sl/{iid}")
    loopstate.save(str(home / "state" / "issues.json"), st)
    for iid, (pane, ws) in panes.items():
        (home / "state" / "panes" / iid).write_text(pane)
        if ws:
            (home / "state" / "panes" / f"{iid}.ws").write_text(ws)
        (home / "state" / f"worker.{iid}.lock").write_text("held")
    return home


def _recording_host(rig, *, gone=True):
    """A SESSION HOST stub that records every invocation's argv and plays a FINISHED, gone session.

    tidy used to drive `cmux close-surface` with a recorded surface UUID; after #308 that recorded
    value is the host's own workspace, so #334 moved the close onto the doorway. The stub answers
    the three calls the teardown ladder makes:

      agent get       -> `agent_not_found` (the host clears a name when the agent exits), which
                         makes the doorway's `state` read UNKNOWN — so `exit` refuses and the
                         ladder escalates to `kill`, exactly as it does against a real finished
                         session that has not been closed yet;
      workspace close -> ok;
      workspace get   -> `workspace_not_found`, the ONE answer a teardown may read as proof the
                         window went. `gone=False` instead keeps answering, which is how a close
                         that did not take is staged.
    """
    log = rig.tmp / "host-close.log"
    stub = rig.tmp / "host-rec"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "open(%r, 'a').write(' '.join(args) + chr(10))\n"
        "pair = (args[0], args[1])\n"
        "def emit(r):\n"
        "    print(json.dumps({'id': 's', 'result': r})); sys.exit(0)\n"
        "def fail(c, m):\n"
        "    print(json.dumps({'id': 's', 'error': {'code': c, 'message': m}})); sys.exit(1)\n"
        "if pair == ('agent', 'get'): fail('agent_not_found', 'the name no longer resolves')\n"
        "if pair == ('workspace', 'close'): emit({'type': 'workspace_closed'})\n"
        "if pair == ('workspace', 'get'):\n"
        "    fail('workspace_not_found', 'gone') if %r else emit({'workspace': {}})\n"
        "fail('unsupported', ' '.join(args))\n" % (str(log), bool(gone)))
    stub.chmod(0o755)
    return log, str(stub)


def _read_until(stream, marker, timeout=30.0):
    """Read from a live process's stdout until `marker` appears, and return what was read.

    A plain readline() cannot be used: the y/N prompt has no trailing newline, so the only reason
    the bytes are visible at all is that `input()` flushes stdout before it blocks — which is
    exactly the moment this test needs to catch.
    """
    got, deadline = "", time.monotonic() + timeout
    while marker not in got:
        assert time.monotonic() < deadline, f"never saw {marker!r}; got {got!r}"
        ready, _, _ = select.select([stream], [], [], 0.1)
        if ready:
            got += os.read(stream.fileno(), 4096).decode("utf-8", "replace")
    return got


def _closed_workspaces(log):
    """The workspaces tidy actually asked the host to close, in order."""
    if not log.exists():
        return []
    return [ln.split()[-1] for ln in log.read_text().splitlines()
            if ln.startswith("workspace close ")]


def test_tidy_dry_run_lists_merged_windows_and_closes_nothing(rig):
    _seed_tidy_state(rig, {"i1": "merged", "i2": "merged", "i5": "running"},
                     {"i1": ("w1:p1", "w1"), "i2": ("w2:p1", "w2"), "i5": ("w5:p1", "w5")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--dry-run", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "i1" in r.stdout and "i2" in r.stdout
    assert "i5" not in r.stdout                       # the in-flight lane is never listed
    assert not log.exists()                           # dry-run closed nothing


def test_tidy_yes_closes_merged_windows_through_the_doorway(rig):
    home = _seed_tidy_state(rig, {"i1": "merged", "i5": "running"},
                            {"i1": ("w1:p1", "w1"), "i5": ("w5:p1", "w5")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w1"]          # exactly the merged window
    # markers + lock cleared for the closed session; the in-flight lane untouched
    assert not (home / "state" / "panes" / "i1").exists()
    assert not (home / "state" / "worker.i1.lock").exists()
    assert (home / "state" / "panes" / "i5").exists()
    assert (home / "state" / "worker.i5.lock").exists()


def test_tidy_never_hands_a_recorded_handle_to_cmux(rig):
    """Issue #334's own regression. `state/panes/<id>` holds the session HOST's identifiers now, so
    a close that shelled cmux with them asked a multiplexer about ids it never issued — it exited
    cheerfully, closed nothing, and tidy reported N windows closed."""
    _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("w1:p1", "w1")})
    log, host = _recording_host(rig)
    cmux_log = rig.tmp / "cmux-must-not-run.log"
    cmux = rig.tmp / "cmux-tripwire"
    cmux.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"" + str(cmux_log) + "\"\nexit 0\n")
    cmux.chmod(0o755)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo),
            env_over={"SL_HERDR": host, "SL_CMUX": str(cmux)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w1"]
    assert not cmux_log.exists(), cmux_log.read_text()


def test_tidy_ignores_a_close_the_host_refuses(rig):
    # best-effort: a host that will not confirm the teardown must not wedge tidy, and the lane's
    # markers are still cleared so nothing outlives the session it identified.
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("w1:p1", "w1")})
    log, host = _recording_host(rig, gone=False)       # the workspace keeps answering: unverified
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w1"]
    assert not (home / "state" / "panes" / "i1").exists()


def test_tidy_closes_nothing_for_a_handle_with_no_workspace(rig):
    # The doorway closes a WINDOW, and a pane id alone cannot name one. Refusing is right —
    # "close whatever is at that pane" is how a stale handle ends someone else's session — and the
    # markers are still swept, so a half-written record does not become permanent.
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("w1:p1", None)})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == []
    assert not (home / "state" / "panes" / "i1").exists()


def test_tidy_default_scope_leaves_parked_windows_alone(rig):
    home = _seed_tidy_state(rig, {"i1": "merged", "i2": "parked"},
                            {"i1": ("w1:p1", "w1"), "i2": ("w2:p1", "w2")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w1"]
    assert (home / "state" / "panes" / "i2").exists()     # parked left for possible re-approval


def test_tidy_all_scope_closes_every_terminal_status_but_never_inflight(rig):
    home = _seed_tidy_state(
        rig, {"i1": "merged", "i2": "parked", "i3": "needs_william", "i4": "bounced",
              "i5": "running", "i6": "gating"},
        {f"i{n}": (f"w{n}:p1", f"w{n}") for n in range(1, 7)})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_closed_workspaces(log)) == {f"w{n}" for n in (1, 2, 3, 4)}
    assert (home / "state" / "panes" / "i5").exists()     # running: never closed
    assert (home / "state" / "panes" / "i6").exists()     # gating: never closed
    # merged is fully cleaned (never relaunches -> race-free); re-approvable sessions keep their
    # markers + lock (runner reconciles them) so tidy can never free a live worker's lock.
    assert not (home / "state" / "panes" / "i1").exists()
    assert not (home / "state" / "worker.i1.lock").exists()
    for n in (2, 3, 4):
        assert (home / "state" / "panes" / f"i{n}").exists()
        assert (home / "state" / f"worker.i{n}.lock").exists()


def test_tidy_confirm_yes_closes(rig):
    _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("w1:p1", "w1")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--repo", str(rig.repo), env_over={"SL_HERDR": host}, inp="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w1"]


def test_tidy_confirm_default_no_aborts_and_closes_nothing(rig):
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("w1:p1", "w1")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--repo", str(rig.repo), env_over={"SL_HERDR": host}, inp="\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()                               # empty answer = No = nothing closed
    assert (home / "state" / "panes" / "i1").exists()
    assert (home / "state" / "worker.i1.lock").exists()


def test_tidy_with_only_inflight_sessions_closes_nothing(rig):
    home = _seed_tidy_state(rig, {"i5": "running", "i6": "blocked", "i7": "exited"},
                            {f"i{n}": (f"w{n}:p1", f"w{n}") for n in (5, 6, 7)})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()
    for n in (5, 6, 7):
        assert (home / "state" / "panes" / f"i{n}").exists()


def test_tidy_on_a_never_run_repo_is_calm(rig):
    r = cli(rig, "tidy", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "no" in r.stdout.lower()                       # "no finished ... to close"


def test_tidy_survives_a_corrupt_issues_json(rig):
    # a wrong-typed issues.json (parses as a JSON list, not a state dict) must degrade to
    # "nothing to close", never crash — fail closed on wrong-typed input.
    home = _tidy_home(rig)
    (home / "state" / "panes").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text('["not", "a", "state", "dict"]')
    (home / "state" / "panes" / "i1").write_text("w1:p1")
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()
    assert "no" in r.stdout.lower()


def test_tidy_never_touches_a_reapprovable_sessions_markers_or_lock(rig):
    # (Codex cross-review rounds 1-2, critical) a parked/needs-william/bounced session can be
    # re-approved + relaunched by a LIVE runner at any time — and its pane markers/lock aren't
    # under any lock tidy can take, so a read-then-remove can never be made atomic. The airtight
    # fix is STRUCTURAL: tidy closes such a window but NEVER removes its markers/lock (that stays
    # the runner's _close_stale_session lifecycle), so tidy can never free a live worker's lock.
    home = _seed_tidy_state(rig, {"i2": "parked"}, {"i2": ("w2:p1", "w2")})
    log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["w2"]
    # the window is closed, but the session's markers + singleton lock are LEFT for the runner
    assert (home / "state" / "panes" / "i2").exists()
    assert (home / "state" / "panes" / "i2.ws").exists()
    assert (home / "state" / "worker.i2.lock").exists()
    assert "reconcile" in r.stdout.lower()


def test_tidy_leaves_a_lane_that_relaunched_during_the_y_N_wait_completely_alone(rig):
    """Under cmux the snapshot was a structural guarantee: `close-surface --surface <snapshot>`
    could not touch anything but that surface. The doorway is not shaped that way — its `kill`
    derives the pid it SIGNALS fresh from the lane NAME, so a re-approval that relaunched this lane
    while the owner read the prompt would have had its brand-new worker SIGTERMed. The handle is
    re-read immediately before the teardown and a mismatch stops everything."""
    home = _seed_tidy_state(rig, {"i2": "parked"}, {"i2": ("old:p1", "old")})
    log, host = _recording_host(rig)
    # Driven through the REAL y/N wait, because that wait IS the window: the snapshot is taken when
    # the list is printed and the close happens after the answer. Popen (not `cli`) so the relaunch
    # can land while the process is genuinely blocked on stdin.
    proc = subprocess.Popen([sys.executable, str(CLI), "tidy", "--all", "--repo", str(rig.repo)],
                            env={**rig.env, "SL_HERDR": host}, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen = _read_until(proc.stdout, "close these windows")     # the list is out; we are at the wait
    # the relaunch: a fresh spawn rewrites BOTH halves, because every spawn makes a new workspace
    (home / "state" / "panes" / "i2").write_text("new:p1")
    (home / "state" / "panes" / "i2.ws").write_text("new")
    proc.stdin.write("y\n")
    proc.stdin.flush()
    rest = proc.stdout.read()
    proc.wait(timeout=60)
    out = seen + rest
    assert proc.returncode == 0, out + proc.stderr.read()
    assert not log.exists(), f"nothing may be asked of the host: {log.read_text()}"
    assert "relaunched" in out
    # and the count is honest — reporting the LISTED number would tell the owner a window went
    assert "closed 0 window(s)" in out


def test_tidy_closes_the_snapshotted_workspace_not_one_the_host_reports_later(rig):
    # The close targets the workspace captured at LIST time. (A relaunch that rewrote the markers
    # is refused outright by the guard above; this pins the other half — that nothing the host says
    # mid-teardown can redirect which workspace gets closed.)
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("old:p1", "old")})
    log = rig.tmp / "host-close.log"
    marker = home / "state" / "panes" / "i1.ws"
    stub = rig.tmp / "host-relaunch"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "open(%r, 'a').write(' '.join(args) + chr(10))\n"
        "open(%r, 'w').write('new')\n"                   # simulate a relaunch mid-close
        "pair = (args[0], args[1])\n"
        "def fail(c, m):\n"
        "    print(json.dumps({'id': 's', 'error': {'code': c, 'message': m}})); sys.exit(1)\n"
        "if pair == ('workspace', 'close'):\n"
        "    print(json.dumps({'id': 's', 'result': {'type': 'workspace_closed'}})); sys.exit(0)\n"
        "fail('workspace_not_found', 'gone')\n" % (str(log), str(marker)))
    stub.chmod(0o755)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_HERDR": str(stub)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _closed_workspaces(log) == ["old"]         # the snapshot, not the re-read


# --------------------------- janitor (propose-and-approve GitHub debris sweep) ---------------------------
# `superlooper janitor` (issue #62, spec §8 V2) PROPOSES GitHub-side cleanup — stale sl/*
# branches whose PRs merged or were superseded, PRs labeled `superseded` left open by design,
# and parked / needs-william issues gathering dust — and executes ONLY what William approves
# (y/N or --yes, the same word-discipline as tidy). The list / --dry-run changes nothing
# anywhere; nothing is ever auto-closed or auto-deleted; in-flight work can never be proposed.

def _janitor_home(rig):
    return rig.tmp / "slhome" / "o__r"


def _seed_janitor_fixtures(rig):
    """The committed fixtures already carry one of each debris class:
      - branches.json: main, sl/i5-fix-thing, sl/i7-old-thing
      - pr_list_superseded.json: OPEN PR #14 labeled superseded on sl/i7-old-thing
      - issue_list_parked.json: issue #9 labeled parked, updatedAt 2026-06-01 (long aged)
    Add the per-head PR lookups: sl/i5-fix-thing's PR #12 MERGED with headRefOid matching the
    branch's current tip in branches.json (branch proposable); sl/i7-old-thing's PR #14 still
    OPEN (branch NOT proposable — the PR close comes first). And an explicit empty
    needs-william queue."""
    (rig.fixdir / "pr_list_head_sl__i5-fix-thing.json").write_text(json.dumps(
        [{"number": 12, "state": "MERGED", "headRefName": "sl/i5-fix-thing",
          "headRefOid": "bbb222", "labels": []}]))
    (rig.fixdir / "pr_list_head_sl__i7-old-thing.json").write_text(json.dumps(
        [{"number": 14, "state": "OPEN", "headRefName": "sl/i7-old-thing",
          "headRefOid": "ccc333", "labels": [{"name": "superseded"}]}]))
    # The janitor sweeps EVERY park-family label (issue #58: needs-owner + legacy needs-william).
    (rig.fixdir / "issue_list_needs-owner.json").write_text("[]")
    (rig.fixdir / "issue_list_needs-william.json").write_text("[]")
    # A CLEAN accidental-close audit (issue #229) by default: the committed fixture carries a
    # keyword-closed issue for the gh/doctor tests, and leaving it in would add a fourth proposal
    # to every three-class assertion below. The reopen tests seed their own world.
    _seed_closed_issues(rig)


def _seed_closed_issues(rig, *nodes):
    """Serve `superlooper`'s accidental-close audit exactly `nodes` (issue #229) — one GraphQL
    page, no more pages. No nodes = a repo with nothing wrongly closed."""
    (rig.fixdir / "graphql_ClosedIssueClosers.json").write_text(json.dumps(
        {"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}}}}))


def _keyword_closed(num, *, title="Never built", oid="8b79d7ac4f5cb0876e32ae839ab21237989195de",
                    headline="ledger: 07-16 overnight — harvest-promotes-drafts regression"):
    """The 2026-07-16 shape: COMPLETED, closed by a bare commit, no merged PR carrying it."""
    return {"number": num, "title": title, "stateReason": "COMPLETED",
            "closedAt": "2026-07-16T15:32:28Z",
            "timelineItems": {"nodes": [{"closer": {
                "__typename": "Commit", "oid": oid, "messageHeadline": headline,
                "associatedPullRequests": {"nodes": []}}}]}}


def _pr_closed(num, pr, *, title="Shipped through the gate"):
    return {"number": num, "title": title, "stateReason": "COMPLETED",
            "closedAt": "2026-07-15T09:00:00Z",
            "timelineItems": {"nodes": [
                {"closer": {"__typename": "PullRequest", "number": pr, "merged": True}}]}}


def _owner_closed(num, *, title="Closed by hand"):
    return {"number": num, "title": title, "stateReason": "COMPLETED",
            "closedAt": "2026-07-14T09:00:00Z", "timelineItems": {"nodes": []}}


def _janitor_journal(rig):
    p = _janitor_home(rig) / "journal.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _janitor_refused(rig):
    p = _janitor_home(rig) / "state" / "janitor_refused.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_janitor_dry_run_lists_all_three_classes_and_changes_nothing(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    # one proposal per debris class, each with its one-line why
    assert "sl/i5-fix-thing" in out and "#12" in out and "merged" in out.lower()
    assert "#14" in out and "superseded" in out
    assert "#9" in out and "parked" in out and "threshold 14d" in out
    # the branch under the OPEN superseded PR is NOT proposed for deletion
    assert "delete branch sl/i7-old-thing" not in out
    # dry-run changes NOTHING anywhere: no gh writes, no journal, no refused file, no state dir
    assert mutations(rig) == []
    assert _janitor_journal(rig) == []
    assert _janitor_refused(rig) is None


def test_janitor_prompt_default_no_aborts_and_executes_nothing(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--repo", str(rig.repo), inp="\n")
    assert r.returncode == 0
    assert "aborted" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []


def test_janitor_yes_executes_all_three_actions_and_journals_them(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    muts = mutations(rig)
    kinds = {m["kind"] for m in muts}
    assert kinds == {"delete_ref", "close_pr", "close_issue"}
    assert next(m for m in muts if m["kind"] == "delete_ref")["ref"] == "heads/sl/i5-fix-thing"
    pr_close = next(m for m in muts if m["kind"] == "close_pr")
    assert pr_close["num"] == "14" and "janitor" in pr_close["comment"]
    issue_close = next(m for m in muts if m["kind"] == "close_issue")
    assert issue_close["num"] == "9" and "janitor" in issue_close["comment"]
    recs = [x for x in _janitor_journal(rig) if x.get("act") == "janitor"]
    assert len(recs) == 3 and all(x["outcome"] == "ok" for x in recs)
    assert all(x.get("why") for x in recs)          # the one-line why rides into the journal
    assert "3 executed" in r.stdout


def _seed_merged_open_pair(rig, issue_num=101, pr=12, head=None):
    """A merged sl/i<N> PR whose issue is still OPEN (issue #404) — the pair the janitor's fourth
    close class exists to surface. `pr_list_heads.json` is gh.sl_head_prs's fixture; `issue_list.json`
    is gh.open_issues_all's."""
    head = head or f"sl/i{issue_num}-render-the-widget"
    (rig.fixdir / "pr_list_heads.json").write_text(json.dumps(
        [{"number": pr, "state": "MERGED", "headRefName": head}]))
    issues = json.loads((rig.fixdir / "issue_list.json").read_text())
    for i in issues:
        # a merged-PR issue carries neither owner word: launch removes `agent-ready`, merge removes
        # `in-progress`. Strip them so the fixture is the shape the defect actually leaves behind.
        i["labels"] = [l for l in i["labels"] if l["name"] not in ("agent-ready", "in-progress")]
    (rig.fixdir / "issue_list.json").write_text(json.dumps(issues))


def test_janitor_proposes_closing_an_issue_its_merged_pr_left_open(rig):
    _seed_janitor_fixtures(rig)
    _seed_merged_open_pair(rig)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    # the line names BOTH numbers: the issue being closed and the merged PR that justifies it
    assert "close issue #101 (PR #12 merged)" in r.stdout
    assert "still OPEN" in r.stdout and "run" in r.stdout
    assert mutations(rig) == []                       # --dry-run executes nothing


def test_janitor_executes_an_approved_merged_open_close_with_its_audit_comment(rig):
    _seed_janitor_fixtures(rig)
    _seed_merged_open_pair(rig)
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    closes = {m["num"]: m for m in mutations(rig) if m["kind"] == "close_issue"}
    assert "101" in closes
    # the audit comment names the janitor AND the merged PR — a close nobody can trace back to the
    # work that justified it is not evidence
    assert "janitor" in closes["101"]["comment"] and "#12" in closes["101"]["comment"]
    recs = [x for x in _janitor_journal(rig)
            if x.get("act") == "janitor" and x.get("target") == 101]
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["action"] == "close-issue"


def test_upkeep_names_what_the_merged_open_cap_left_out(rig):
    """The regression guard for the PLUMBING, not the renderer (third fresh review, P1).

    `superlooper upkeep` is the weekly "did I miss anything" read, and it once reported only
    `reopen_withheld` — so the second capped class was invisible there while `janitor` said "240
    more found". A renderer test cannot catch that: it hands `_janitor_row` a dict that already has
    the key. This drives the REAL command over a repo with more pairs than the cap, so deleting the
    key anywhere between propose() and the page turns it red."""
    _seed_janitor_fixtures(rig)
    over = janitor_lib.MERGED_OPEN_SWEEP_CAP + 4
    # numbered from 100 so none collides with the committed fixtures' own issues (the aged-park
    # sweep proposes closing #9, which would legitimately drop one pair out of this class and make
    # the arithmetic below read as an off-by-one rather than as the exclusion it is)
    nums = list(range(100, 100 + over))
    (rig.fixdir / "pr_list_heads.json").write_text(json.dumps(
        [{"number": 500 + n, "state": "MERGED", "headRefName": "sl/i%d-x" % n} for n in nums]))
    (rig.fixdir / "issue_list.json").write_text(json.dumps(
        [{"number": n, "title": "pair %d" % n, "createdAt": "2026-07-01T09:00:00Z",
          "labels": [{"name": "type:build"}],
          "body": "## Loop metadata\ntouches: frontend\n"} for n in nums]))
    r = cli(rig, "upkeep", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "%d issue-merged-open" % janitor_lib.MERGED_OPEN_SWEEP_CAP in r.stdout
    assert "4 more merged-PR" in r.stdout, \
        "upkeep showed the capped list and never said what the cap left out:\n" + r.stdout


def test_janitor_caps_the_merged_open_class_and_says_what_it_withheld_on_both_surfaces(rig):
    """The sibling of test_janitor_caps_the_reopen_class_..., and it pins the SAME two surfaces:
    the human note the owner reads and the `--json` envelope the command center reads. The cap
    itself is unit-tested; what this guards is that the count survives the trip out of propose()
    to each surface — mutation-proved necessary, because `mo_note = ""` and a dropped JSON key
    both left the whole suite green (fourth fresh review, P1)."""
    _seed_janitor_fixtures(rig)
    over = janitor_lib.MERGED_OPEN_SWEEP_CAP + 5
    _seed_merged_open_pair(rig)                      # strips the owner labels off the fixtures
    nums = list(range(200, 200 + over))
    (rig.fixdir / "pr_list_heads.json").write_text(json.dumps(
        [{"number": 700 + n, "state": "MERGED", "headRefName": "sl/i%d-x" % n} for n in nums]))
    (rig.fixdir / "issue_list.json").write_text(json.dumps(
        [{"number": n, "title": "pair %d" % n, "createdAt": "2026-07-01T09:00:00Z",
          "labels": [{"name": "type:build"}],
          "body": "## Loop metadata\ntouches: frontend\n"} for n in nums]))
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("(PR #") == janitor_lib.MERGED_OPEN_SWEEP_CAP
    assert "5 more merged-PR/still-open issue(s) were found and NOT proposed" in r.stdout
    doc = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    assert doc["merged_open_withheld"] == 5


def test_janitor_json_carries_the_merged_open_pair_for_the_command_center(rig):
    _seed_janitor_fixtures(rig)
    _seed_merged_open_pair(rig)
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    props = json.loads(r.stdout)["proposals"]
    pair = [p for p in props if p["kind"] == "issue-merged-open"]
    assert [p["key"] for p in pair] == ["closemerged:101"]
    assert pair[0]["pr"] == 12 and pair[0]["target"] == 101
    assert mutations(rig) == []                       # --json is propose-only


def test_janitor_prompt_y_executes(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--repo", str(rig.repo), inp="y\n")
    assert r.returncode == 0
    assert len(mutations(rig)) == 3


def test_janitor_never_proposes_inflight_or_midgate_work(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    st["issues"]["i5"] = dict(loopstate.new_issue(), status="running",
                              branch="sl/i5-fix-thing")
    st["issues"]["i7"] = dict(loopstate.new_issue(), status="gating",
                              branch="sl/i7-old-thing")
    st["issues"]["i9"] = dict(loopstate.new_issue(), status="holding")
    loopstate.save(str(home / "state" / "issues.json"), st)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to propose" in r.stdout
    assert mutations(rig) == []


def test_janitor_unreadable_loopstate_refuses_to_propose(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "unreadable" in (r.stdout + r.stderr)
    assert mutations(rig) == []


def test_janitor_respects_the_configured_age_threshold(rig):
    _seed_janitor_fixtures(rig)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["quality-gate"],
         "janitor": {"aged_park_days": 100000}}))
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "close issue" not in r.stdout        # issue #9 is no longer past the threshold
    assert "#12" in r.stdout                    # the branch/PR proposals are unaffected


def test_janitor_failed_action_surfaces_once_and_is_held_back(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "git/refs/heads/sl/i5-fix-thing", "times": 1,
          "stderr": "HTTP 403: refs are protected"}]))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    # the failure is LOUD: nonzero exit, a FAIL line, a fail journal record, a refused entry
    assert r.returncode != 0
    assert "FAIL" in r.stdout and "branch:sl/i5-fix-thing" in r.stdout
    recs = {x["target"]: x["outcome"] for x in _janitor_journal(rig)
            if x.get("act") == "janitor"}
    assert recs == {"sl/i5-fix-thing": "fail", 14: "ok", 9: "ok"}
    assert "branch:sl/i5-fix-thing" in _janitor_refused(rig)

    # sweep 2, with the world reflecting sweep 1 (PR closed, issue closed): the refused branch
    # is HELD BACK — surfaced as a held-back count, never silently retried.
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")
    r2 = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "nothing to propose" in r2.stdout and "held back" in r2.stdout
    deletes = [m for m in mutations(rig) if m["kind"] == "delete_ref"]
    assert deletes == []                         # sweep 1's delete FAILED, sweep 2 never retried

    # sweep 3 with --retry-refused (no fail rule left): the delete executes and the refusal
    # record clears.
    r3 = cli(rig, "janitor", "--yes", "--retry-refused", "--repo", str(rig.repo))
    assert r3.returncode == 0, r3.stdout + r3.stderr
    deletes = [m for m in mutations(rig) if m["kind"] == "delete_ref"]
    assert [d["ref"] for d in deletes] == ["heads/sl/i5-fix-thing"]
    assert _janitor_refused(rig) == {}


def test_janitor_never_prunes_a_refusal_on_absence(rig):
    # a key's ABSENCE from a sweep is not proof its debris is gone — a transient gh blip also
    # produces absence (every read fails closed to empty). Pruning on it would silently drop
    # the hold-back and let a later sweep retry a refused action without --retry-refused
    # (cross-review round 1, C1). A refusal clears ONLY via a later successful execution.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"branch:sl/i99-vanished": {"reason": "gone", "ts": 1}}))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "branch:sl/i99-vanished" in _janitor_refused(rig)


def test_janitor_unreadable_refused_file_refuses_to_run(rig):
    # corrupt hold-back state read as {} would re-propose every held-back action — the same
    # fail-open class as unreadable issues.json (cross-review round 1, C2). Refuse instead.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "janitor_refused.json" in (r.stdout + r.stderr)
    assert mutations(rig) == [] and _janitor_journal(rig) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_janitor_permission_denied_refused_file_refuses_to_run(rig):
    # a PRESENT hold-back file that cannot be OPENED (EPERM, not ENOENT) must read as
    # unreadable, never as missing/{} — only a genuinely absent file means "nothing ever
    # refused" (cross-review round 2: _read() collapsed every OSError to None).
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    p = home / "state" / "janitor_refused.json"
    p.write_text(json.dumps({"branch:sl/i99-x": {"reason": "r", "ts": 1}}))
    p.chmod(0o000)
    try:
        r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    finally:
        p.chmod(0o644)
    assert r.returncode != 0
    assert "janitor_refused.json" in (r.stdout + r.stderr)
    assert mutations(rig) == []


# --- the accidental-close class: propose a REOPEN, never reopen anything (issue #229) ---------
# The 2026-07-16 incident end to end: a ledger commit's "fixes #189" keyword closed an approved,
# never-built fix; the tracker read COMPLETED for a day. The hand-reopen that eventually fixed it
# becomes one janitor tap — but still a TAP: nothing reopens without the owner's word.

def test_janitor_proposes_reopening_a_keyword_closed_issue_and_changes_nothing(rig):
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REOPEN issue #189" in r.stdout
    assert "8b79d7ac" in r.stdout                       # the closing commit is named in the why
    assert "ledger: 07-16 overnight" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []


def test_janitor_never_proposes_reopening_a_pr_closed_or_owner_closed_issue(rig):
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _pr_closed(150, 242), _owner_closed(98))
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REOPEN" not in r.stdout
    assert "#150" not in r.stdout and "#98" not in r.stdout


def test_janitor_approved_reopen_executes_with_an_audit_comment_naming_the_commit(rig):
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    reopen = next(m for m in mutations(rig) if m["kind"] == "reopen_issue")
    assert reopen["num"] == "189"
    assert "janitor" in reopen["comment"] and "8b79d7ac" in reopen["comment"]
    rec = next(x for x in _janitor_journal(rig) if x.get("action") == "reopen-issue")
    assert rec["target"] == 189 and rec["outcome"] == "ok" and rec["why"]


def test_janitor_sweeps_all_four_classes_in_one_pass(rig):
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert {m["kind"] for m in mutations(rig)} == \
        {"delete_ref", "close_pr", "close_issue", "reopen_issue"}
    assert "4 executed" in r.stdout


def test_janitor_json_carries_the_reopen_key_for_a_one_touch_approval(rig):
    # the command center approves per KEY; a reopen must be tappable exactly like every other
    # proposal, and its key must not collide with a close of the same issue number.
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    doc = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    keys = {p["key"] for p in doc["proposals"]}
    # the aged-park close of #9 and the reopen of #189 sit side by side under distinct key spaces
    assert "reopen:189" in keys and "issue:9" in keys
    r = cli(rig, "janitor", "--execute-keys", "reopen:189", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert [m["kind"] for m in mutations(rig)] == ["reopen_issue"]


def test_janitor_never_proposes_reopening_an_issue_a_lane_is_working(rig):
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    st["issues"]["i189"] = dict(loopstate.new_issue(), status="running", branch="sl/i189-x")
    loopstate.save(str(home / "state" / "issues.json"), st)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REOPEN" not in r.stdout


def test_janitor_caps_the_reopen_class_and_says_what_it_withheld(rig):
    # `--yes` is a blanket approval. In a repo whose humans close issues from commit messages as
    # a matter of course, an uncapped class would fire hundreds of reopens — each a comment, each
    # a notification, with no one command to undo — on a single word.
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, *[_keyword_closed(n) for n in range(100, 125)])
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("REOPEN issue #") == 10
    assert "15 more keyword-closed issue(s) were found and NOT proposed" in r.stdout
    assert "a later sweep proposes the rest" in r.stdout
    doc = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    assert doc["reopen_withheld"] == 15


def test_janitor_executes_every_reopen_the_owner_was_shown(rig):
    # The reopen class fills its per-sweep cap from the NON-refused candidates, so the listing and
    # the act-time re-derivation must agree about which keys are refused. When they disagreed, the
    # owner approved a list, some of it ran, and the rest was reported "no longer eligible (state
    # moved)" — when nothing had moved at all. Approve what was listed; run exactly that.
    _seed_janitor_fixtures(rig)
    # 20 keyword-closed issues, the 5 newest stuck in the refused map -> 15 fresh, cap 10 shown.
    _seed_closed_issues(rig, *[_keyword_closed(n) for n in range(100, 120)])
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"reopen:%d" % n: {"reason": "gh refused", "ts": 1} for n in range(115, 120)}))
    listed = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    shown = [p["key"] for p in listed["proposals"] if p["kind"] == "issue-reopen"]
    assert len(shown) == 10                       # a full cap of FRESH work, none of it stuck
    assert listed["reopen_withheld"] == 5
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    reopened = {"reopen:%s" % m["num"] for m in mutations(rig) if m["kind"] == "reopen_issue"}
    assert reopened == set(shown)                 # every listed reopen ran; none was false-skipped
    assert "no longer eligible" not in r.stdout and "state moved" not in r.stdout


def test_janitor_execute_keys_holds_a_tapped_refused_reopen_with_the_right_reason(rig):
    # holdback parity survives the fix: a tapped-but-held key still reports "held back from a
    # prior failure", never the act-time "no longer eligible" skip.
    _seed_janitor_fixtures(rig)
    _seed_closed_issues(rig, _keyword_closed(189))
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"reopen:189": {"reason": "gh refused", "ts": 1}}))
    doc = json.loads(cli(rig, "janitor", "--execute-keys", "reopen:189",
                         "--repo", str(rig.repo)).stdout)
    assert doc["held"] == 1 and doc["executed"] == 0
    assert doc["results"][0]["reason"].startswith("held back from a prior failure")
    assert mutations(rig) == []
    # ...and --retry-refused lets the same explicit tap through
    r = cli(rig, "janitor", "--execute-keys", "reopen:189", "--retry-refused",
            "--repo", str(rig.repo))
    assert json.loads(r.stdout)["executed"] == 1
    assert [m["kind"] for m in mutations(rig)] == ["reopen_issue"]


def test_janitor_proposes_no_reopens_when_the_closed_read_is_refused(rig):
    # a refused GraphQL read fails closed to [] — the sweep proposes fewer things, never more.
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "graphql_ClosedIssueClosers.json").write_text("{not json")
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REOPEN" not in r.stdout
    assert "sl/i5-fix-thing" in r.stdout          # the rest of the sweep still works


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_janitor_permission_denied_loopstate_refuses_to_propose(rig):
    # the same distinction for issues.json: an unreadable-by-permissions exclusion source must
    # refuse the sweep (nothing is provably idle), never read as "no lanes exist".
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    p = home / "state" / "issues.json"
    p.write_text(json.dumps({"issues": {"i5": {"status": "running"}}}))
    p.chmod(0o000)
    try:
        r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    finally:
        p.chmod(0o644)
    assert r.returncode != 0
    assert "unreadable" in (r.stdout + r.stderr)
    assert mutations(rig) == []


def test_janitor_nothing_to_propose_is_a_clean_exit(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "branches.json").write_text("[]")
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")
    r = cli(rig, "janitor", "--repo", str(rig.repo))   # no --yes: must not hang on a prompt
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to propose" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []


# --------------------------- janitor --json / --execute-keys (command-center wiring, issue #121) ---------------------------
# The command center drives the SAME `superlooper janitor` machinery the terminal does (the way the
# Tidy button drives `superlooper tidy`), so it needs a machine-readable propose and a subset
# executor. `--json` prints the propose() snapshot (proposals + held-back keys) and changes NOTHING;
# `--execute-keys k1,k2` executes EXACTLY those approved keys through the same reconcile → execute →
# journal → refused-holdback flow. Neither weakens a single safety rule: propose() is still the pure
# selector, act-time re-verification still runs, and a currently-refused key is still held back.

def test_janitor_json_lists_proposals_as_structured_data_and_changes_nothing(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)                       # stdout is ONE json object, no human lines
    assert doc["ok"] is True
    by_key = {p["key"]: p for p in doc["proposals"]}
    assert set(by_key) == {"branch:sl/i5-fix-thing", "pr:14", "issue:9"}
    assert by_key["branch:sl/i5-fix-thing"]["kind"] == "branch"
    assert by_key["branch:sl/i5-fix-thing"]["action"] == "delete-branch"
    assert by_key["pr:14"]["action"] == "close-pr" and by_key["pr:14"]["target"] == 14
    assert by_key["issue:9"]["action"] == "close-issue"
    assert all(p.get("why") for p in doc["proposals"])
    assert doc["held"] == [] and doc["aged_park_days"] == 14
    # --json is a read: no gh writes, no journal, no refused file
    assert mutations(rig) == [] and _janitor_journal(rig) == [] and _janitor_refused(rig) is None


def test_janitor_json_reports_held_back_keys_separately(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"branch:sl/i5-fix-thing": {"reason": "HTTP 403", "ts": 1}}))
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    keys = {p["key"] for p in doc["proposals"]}
    assert "branch:sl/i5-fix-thing" not in keys       # held back, not proposed
    assert doc["held"] == ["branch:sl/i5-fix-thing"]  # surfaced once, separately
    assert mutations(rig) == []


def test_janitor_json_empty_sweep_is_ok_true_with_no_proposals(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "branches.json").write_text("[]")
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and doc["proposals"] == []


def test_janitor_json_unreadable_loopstate_emits_ok_false_and_exits_nonzero(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    assert r.returncode != 0
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and "unreadable" in doc["error"]
    assert mutations(rig) == []


def test_janitor_execute_keys_runs_only_the_named_subset(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--json", "--execute-keys", "pr:14", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and doc["executed"] == 1 and doc["failed"] == 0
    assert [x["key"] for x in doc["results"]] == ["pr:14"]
    assert doc["results"][0]["outcome"] == "ok"
    # ONLY the PR closed — the branch delete and issue close the owner did NOT tap never ran
    kinds = {m["kind"] for m in mutations(rig)}
    assert kinds == {"close_pr"}
    recs = [x for x in _janitor_journal(rig) if x.get("act") == "janitor"]
    assert len(recs) == 1 and recs[0]["target"] == 14 and recs[0]["outcome"] == "ok"
    assert recs[0].get("why")


def test_janitor_execute_keys_multiple_keys(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--json", "--execute-keys", "pr:14,issue:9",
            "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["executed"] == 2
    assert {m["kind"] for m in mutations(rig)} == {"close_pr", "close_issue"}


def test_janitor_execute_keys_skips_a_key_no_longer_eligible(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--json", "--execute-keys", "branch:sl/i999-vanished",
            "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["executed"] == 0 and doc["skipped"] == 1
    assert doc["results"][0]["outcome"] == "skipped"
    assert mutations(rig) == []


def test_janitor_execute_keys_act_time_reverification_excludes_inflight(rig):
    # the owner taps a branch in the dashboard; before the CLI acts, that issue goes in-flight.
    # a FRESH re-derivation no longer proposes it, so reconcile drops it — never deleted.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    st["issues"]["i5"] = dict(loopstate.new_issue(), status="running",
                              branch="sl/i5-fix-thing")
    loopstate.save(str(home / "state" / "issues.json"), st)
    r = cli(rig, "janitor", "--json", "--execute-keys", "branch:sl/i5-fix-thing",
            "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["results"][0]["outcome"] == "skipped"
    assert [m for m in mutations(rig) if m["kind"] == "delete_ref"] == []


def test_janitor_execute_keys_failure_is_held_back_then_retryable(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "git/refs/heads/sl/i5-fix-thing", "times": 1,
          "stderr": "HTTP 403: refs are protected"}]))
    r = cli(rig, "janitor", "--json", "--execute-keys", "branch:sl/i5-fix-thing",
            "--repo", str(rig.repo))
    assert r.returncode != 0
    doc = json.loads(r.stdout)
    assert doc["failed"] == 1 and doc["results"][0]["outcome"] == "fail"
    assert "branch:sl/i5-fix-thing" in _janitor_refused(rig)

    # a second tap of the SAME key, no retry flag: held back, never silently retried
    r2 = cli(rig, "janitor", "--json", "--execute-keys", "branch:sl/i5-fix-thing",
             "--repo", str(rig.repo))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc2 = json.loads(r2.stdout)
    assert doc2["results"][0]["outcome"] == "held" and doc2["executed"] == 0
    assert [m for m in mutations(rig) if m["kind"] == "delete_ref"] == []

    # explicit retry (no fail rule left): it executes and the refusal record clears
    r3 = cli(rig, "janitor", "--json", "--execute-keys", "branch:sl/i5-fix-thing",
             "--retry-refused", "--repo", str(rig.repo))
    assert r3.returncode == 0, r3.stdout + r3.stderr
    doc3 = json.loads(r3.stdout)
    assert doc3["results"][0]["outcome"] == "ok"
    assert [m["ref"] for m in mutations(rig) if m["kind"] == "delete_ref"] == \
        ["heads/sl/i5-fix-thing"]
    assert _janitor_refused(rig) == {}


def test_janitor_execute_keys_with_dry_run_changes_nothing(rig):
    # --dry-run wins over --execute-keys: it re-derives and reports what WOULD run, but performs no
    # gh write, no journal, no refused-map write (the CLI's documented "change NOTHING" contract).
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--json", "--execute-keys", "pr:14,issue:9", "--dry-run",
            "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["executed"] == 0
    assert {x["key"]: x["outcome"] for x in doc["results"]} == {
        "pr:14": "would-run", "issue:9": "would-run"}
    assert mutations(rig) == [] and _janitor_journal(rig) == []
    assert _janitor_refused(rig) is None      # nothing written anywhere


def test_janitor_execute_keys_dry_run_still_reports_a_refused_key_as_held(rig):
    # The holdback is now decided BEFORE the re-derivation rather than per-key after it, which made
    # --dry-run's own inline `held` branch dead code. The COUNT must still be right: a refused key
    # in the tap list reports `held`, not `would-run`, and still writes nothing.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"issue:9": {"reason": "gh refused", "ts": 1}}))
    doc = json.loads(cli(rig, "janitor", "--json", "--execute-keys", "pr:14,issue:9", "--dry-run",
                         "--repo", str(rig.repo)).stdout)
    assert doc["held"] == 1 and doc["executed"] == 0
    assert {x["key"]: x["outcome"] for x in doc["results"]} == {
        "pr:14": "would-run", "issue:9": "held"}
    assert mutations(rig) == [] and _janitor_journal(rig) == []
    # ...and the hold-back map is untouched by a dry run
    assert _janitor_refused(rig) == {"issue:9": {"reason": "gh refused", "ts": 1}}


def test_janitor_execute_keys_unreadable_loopstate_refuses(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--json", "--execute-keys", "pr:14", "--repo", str(rig.repo))
    assert r.returncode != 0
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and "unreadable" in doc["error"]
    assert mutations(rig) == []


# --------------------------- tidy: retired answerer windows (#194) ---------------------------
# `superlooper tidy` used to ALSO close finished answerer session windows (a<N>) — issue #132's
# fold-in over tidy.closable_answerers. #194 retired the answerer seat, so nothing mints an a<N>
# id any more and the selector is gone. What must be true now: a LEFTOVER a<N> pane marker on a
# long-lived state home (there are real ones on disk from the pre-#163 era) is simply IGNORED —
# tidy neither lists it nor deletes it. Silently ignoring is the right landing: those markers point
# at surfaces nothing will ever relaunch, and tidy's whole contract is "only act on a window I can
# positively name as a finished, tracked session".

def _tidy_state_home(rig):
    return rig.tmp / "slhome" / "o__r"


def test_tidy_ignores_a_leftover_answerer_pane_marker(rig):
    home = _tidy_state_home(rig)
    panes = home / "state" / "panes"
    panes.mkdir(parents=True)
    for name in ("a1", "a1.ws"):
        (panes / name).write_text("leftover-" + name)
    (panes / "i7").write_text("surf-i7")
    (panes / "i7.ws").write_text("ws-i7")
    loopstate.save(str(home / "state" / "issues.json"),
                   {"version": 1, "issues": {"i7": {"status": "merged", "branch": None}}})

    r = cli(rig, "tidy", "--repo", str(rig.repo), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert re.findall(r"^  (\w+)\s", r.stdout, re.M) == ["i7"], r.stdout
    assert "a1" not in r.stdout

    _log, host = _recording_host(rig)
    r = cli(rig, "tidy", "--repo", str(rig.repo), "--yes", env_over={"SL_HERDR": host})
    assert r.returncode == 0, r.stderr
    assert "closed 1 window(s)." in r.stdout, r.stdout
    # the merged issue's markers are cleaned; the orphaned a<N> pair is left exactly as found
    assert not (panes / "i7").exists() and not (panes / "i7.ws").exists()
    assert (panes / "a1").read_text() == "leftover-a1"
    assert (panes / "a1.ws").read_text() == "leftover-a1.ws"


# --------------------------- doctor: the queue lint (issue #225) ---------------------------
# The 2026-07-16 audit, made runnable. It found 25 of 35 open issues mechanically unschedulable —
# 16 of them `agent-ready` — and it was done by hand, once. Now it is a command.
#
# The split is deliberate. An APPROVED issue that cannot launch is a live wedge: the owner has said
# go and nothing will ever happen, so the doctor FAILS. Any other open issue with a defect is
# backlog hygiene — real, worth naming with its fix, but not a broken stack — so it WARNs and the
# doctor still passes.

def _write_issues(rig, issues):
    (rig.fixdir / "issue_list.json").write_text(json.dumps(issues))


def _issue(num, labels=(), body="## Loop metadata\ntouches: engine\n", title="An issue"):
    return {"number": num, "title": title, "labels": [{"name": n} for n in labels],
            "body": body, "createdAt": "2026-07-01T00:00:00Z"}


def _areas_config(rig, **over):
    body = {"version": 1, "repo": "o/r",
            "required_checks": ["review/local-gate", "quality-gate"],
            "areas": {"engine": ["skills/**"], "dashboard": ["dashboard/**"]},
            "touches_required": True}
    body.update(over)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(body))


def test_doctor_queue_lint_passes_a_clean_queue(rig):
    _areas_config(rig)
    _write_issues(rig, [_issue(1, ["agent-ready", "type:build"]),
                        _issue(2, ["needs-owner", "type:investigate"], body="## Goal\nwhy\n")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue lint" in r.stdout


def test_doctor_queue_lint_catches_a_missing_type_label_with_its_exact_fix(rig):
    _areas_config(rig)
    _write_issues(rig, [_issue(7, ["needs-owner"], title="no kind declared")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert "#7" in r.stdout
    assert "type:build" in r.stdout and "type:diagnose-and-fix" in r.stdout


def test_doctor_queue_lint_catches_a_bare_touches_line_outside_the_section(rig):
    _areas_config(rig)
    _write_issues(rig, [_issue(8, ["needs-owner", "type:build"],
                               body="## Goal\nship it\n\ntouches: engine\n")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert "#8" in r.stdout and "## Loop metadata" in r.stdout


def test_doctor_queue_lint_catches_an_area_the_repo_never_declared(rig):
    _areas_config(rig)
    _write_issues(rig, [_issue(9, ["needs-owner", "type:build"],
                               body="## Loop metadata\ntouches: plugin\n")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert "#9" in r.stdout and "plugin" in r.stdout
    assert "engine" in r.stdout and "dashboard" in r.stdout      # the real areas, named


def test_doctor_queue_lint_warns_but_passes_on_an_unapproved_invalid_issue(rig):
    _areas_config(rig)
    _write_issues(rig, [_issue(7, ["needs-owner"])])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout


def test_doctor_FAILS_on_an_approved_issue_that_can_never_launch(rig):
    # The wedge the audit found: `agent-ready` is on, and nothing will ever happen.
    _areas_config(rig)
    _write_issues(rig, [_issue(7, ["agent-ready"], title="approved but unlaunchable")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "queue lint" in (r.stdout + r.stderr)
    assert "#7" in r.stdout


def test_doctor_queue_lint_does_not_fail_over_an_approved_issue_it_would_still_launch(rig):
    # An undeclared AREA does not stop a launch — the runner never validates area names. It is
    # named, but it is not a wedge, so it must not turn the doctor red.
    _areas_config(rig)
    _write_issues(rig, [_issue(9, ["agent-ready", "type:build"],
                               body="## Loop metadata\ntouches: plugin\n")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#9" in r.stdout


def test_doctor_queue_lint_respects_a_repo_that_does_not_require_touches(rig):
    _areas_config(rig, touches_required=False)
    _write_issues(rig, [_issue(7, ["agent-ready", "type:build"], body="## Goal\nship it\n")])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr


def test_doctor_queue_lint_skips_itself_when_gh_is_unreachable(rig):
    # No issue list read means no verdict — never a confident "your whole queue is broken" built
    # on an empty answer from a refused read.
    _areas_config(rig)
    r = cli(rig, "doctor", "--repo", str(rig.repo), env_over={"GH_FAIL": "1"})
    assert "0 of 0" not in r.stdout


# --------------------------- janitor: metadata repair (issue #225) ---------------------------
# The owner's amendment of 2026-07-17: remediation for a mechanically-invalid issue belongs to the
# janitor's existing propose/approve contract, "so the manual batch-repair run on 2026-07-16
# becomes a janitor tap, never a hand job again."
#
# The safety rule this section exists to pin: a menu of ALTERNATIVES can never be bulk-approved.
# `--yes` applying all three `type:` labels would manufacture the very `type_duplicate` defect it
# was fixing, so the bulk path executes only the determined fixes and prints the menus with their
# keys for an explicit tap.

def _janitor_repo_config(rig, **over):
    body = {"version": 1, "repo": "o/r",
            "required_checks": ["review/local-gate", "quality-gate"],
            "areas": {"engine": ["skills/**"], "dashboard": ["dashboard/**"]},
            "touches_required": True}
    body.update(over)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(body))


def _seed_invalid_queue(rig, issues):
    _seed_janitor_fixtures(rig)
    _janitor_repo_config(rig)
    (rig.fixdir / "issue_list.json").write_text(json.dumps(issues))
    # nothing else to sweep, so the output is exactly the metadata story
    (rig.fixdir / "branches.json").write_text(json.dumps({"main": "aaa111"}))
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")


def test_janitor_proposes_a_type_label_menu_and_executes_none_of_it_on_a_bulk_yes(rig):
    _seed_invalid_queue(rig, [_issue(7, ["needs-owner"], title="no kind declared")])
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#7" in r.stdout and "type:build" in r.stdout and "type:investigate" in r.stdout
    assert "meta:7:type=type:build" in r.stdout           # the key to tap
    assert [m for m in mutations(rig) if m["kind"] == "set_labels"] == []


def test_janitor_executes_exactly_the_type_label_the_owner_tapped(rig):
    _seed_invalid_queue(rig, [_issue(7, ["needs-owner"])])
    r = cli(rig, "janitor", "--repo", str(rig.repo),
            "--execute-keys", "meta:7:type=type:build")
    assert r.returncode == 0, r.stdout + r.stderr
    labelled = [m for m in mutations(rig) if m["kind"] == "set_labels"]
    assert len(labelled) == 1
    assert labelled[0]["num"] == "7" and labelled[0]["add"] == "type:build"
    # every executed fix carries its audit comment (issue Boundaries)
    comments = [m for m in mutations(rig) if m["kind"] == "comment"]
    assert len(comments) == 1 and "janitor" in comments[0]["body"]
    recs = [x for x in _janitor_journal(rig) if x.get("act") == "janitor"]
    assert len(recs) == 1 and recs[0]["outcome"] == "ok" and recs[0]["action"] == "add-label"


def test_janitor_refuses_to_apply_two_alternatives_from_one_menu(rig):
    # The whole reason menus exist: applying two would create the defect it was fixing.
    _seed_invalid_queue(rig, [_issue(7, ["needs-owner"])])
    r = cli(rig, "janitor", "--repo", str(rig.repo),
            "--execute-keys", "meta:7:type=type:build,meta:7:type=type:investigate")
    out = json.loads(r.stdout)
    assert out["executed"] == 1
    assert [x for x in out["results"] if x["outcome"] == "skipped"]
    assert len([m for m in mutations(rig) if m["kind"] == "set_labels"]) == 1


def test_janitor_repairs_a_bare_touches_line_on_a_bulk_yes_because_nothing_is_chosen(rig):
    _seed_invalid_queue(rig, [_issue(8, ["type:build"],
                                     body="## Goal\nship it\n\ntouches: engine\n")])
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    edits = [m for m in mutations(rig) if m["kind"] == "set_body"]
    assert len(edits) == 1 and edits[0]["num"] == "8"
    assert "## Loop metadata\ntouches: engine" in edits[0]["body"]


def test_janitor_executes_the_touches_the_owner_tapped(rig):
    _seed_invalid_queue(rig, [_issue(9, ["type:build"], body="## Goal\nship it\n")])
    r = cli(rig, "janitor", "--repo", str(rig.repo),
            "--execute-keys", "meta:9:touches=dashboard")
    assert r.returncode == 0, r.stdout + r.stderr
    edits = [m for m in mutations(rig) if m["kind"] == "set_body"]
    assert len(edits) == 1
    assert "## Loop metadata" in edits[0]["body"] and "touches: dashboard" in edits[0]["body"]
    assert "## Goal\nship it" in edits[0]["body"]          # the author's words survive


def test_janitor_dry_run_proposes_metadata_fixes_and_writes_nothing(rig):
    _seed_invalid_queue(rig, [_issue(7, ["needs-owner"])])
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert "#7" in r.stdout and "type:build" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []


def test_janitor_json_carries_the_menus_for_the_command_center(rig):
    _seed_invalid_queue(rig, [_issue(7, ["needs-owner"])])
    r = cli(rig, "janitor", "--json", "--repo", str(rig.repo))
    out = json.loads(r.stdout)
    meta = [p for p in out["proposals"] if p["kind"] == "metadata"]
    assert {p["label"] for p in meta} == {"type:build", "type:investigate",
                                           "type:diagnose-and-fix"}
    assert {p["choose_group"] for p in meta} == {"issue:7:type"}
    assert mutations(rig) == []


def test_janitor_leaves_a_valid_queue_alone(rig):
    _seed_invalid_queue(rig, [_issue(7, ["type:build"])])
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert "nothing to propose" in r.stdout


def _corrupt_sl_head_prs(rig):
    """Make `gh.sl_head_prs` REFUSE (unparseable body -> ReadHealth.ok False), the shape
    tests/test_gh.py already pins. `pr_list_heads.json` is that read's own fixture."""
    (rig.fixdir / "pr_list_heads.json").write_text("{not json")


def test_a_refused_sl_pr_list_is_never_reported_as_a_clean_sweep(rig):
    """The PLUMBING guard for the sweep's health flag, on all three surfaces (sixth fresh review).

    `gh.sl_head_prs` reports ok=False on a refusal AND on a full page, and either way an ABSENCE in
    the merged-PR/still-open class is unproven — which the sweep would otherwise print as "no
    GitHub-side debris found". (A truncated read does propose from its partial page; only a refused
    one sweeps nothing. What both share is that "none found" proves nothing.) That is #21/#61's refused-vs-answered-empty on the LAST link of this
    chain: the post-merge verify journals an unverifiable merge and delegates it here, and `doctor`
    has no surface for the class.

    Driven through the real CLIs, not a hand-built view: a renderer test hands `_janitor_row` a dict
    that already carries the key and so cannot catch the key being dropped in the plumbing — the
    same vacuous shape this issue's third and sixth reviews both had to reject.
    """
    _seed_janitor_fixtures(rig)
    _corrupt_sl_head_prs(rig)
    assert "INCOMPLETE" in cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo)).stdout
    doc = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    assert doc["merged_open_swept"] is False
    assert "INCOMPLETE" in cli(rig, "upkeep", "--repo", str(rig.repo)).stdout


def test_a_healthy_sl_pr_list_says_nothing_about_completeness(rig):
    # the counterpart, so the flag cannot be pinned by a constant: a clean read must be silent, or
    # the note becomes boilerplate and stops being a signal.
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert "INCOMPLETE" not in r.stdout
    doc = json.loads(cli(rig, "janitor", "--json", "--repo", str(rig.repo)).stdout)
    assert doc["merged_open_swept"] is True
    assert "INCOMPLETE" not in cli(rig, "upkeep", "--repo", str(rig.repo)).stdout
