"""The triage flight's RUN — the verbs it acts through, driven end to end (issue #449).

The flight itself is a session and its judgement is a model's; that half cannot be faked and is
not what this pins. What IS pinned is everything downstream of the judgement: the four delegated
classes each landing a real, checkable act on a faked GitHub, the delegation's edges refusing at
the call, and the run's own outputs — the verdicts file, the run log with its sitting sheet, and
the morning report's triage section.

Nothing here reaches a real binary. GitHub is ``tests/fakes/fake-gh`` in STATEFUL mode (a little
GitHub whose state.json the acts really mutate, so a close is visible as a close rather than as a
line in a mutation log), git is a REAL throwaway repo pair (a bare origin plus a working clone) —
because "overtaken" evidence is verified against ``origin/<dev>``, and a fake ancestry check would
prove nothing about the one guard that makes that class honest.

The four classes the standing rule delegates, each proven end to end:

  duplicate    -> the absorber's body gains the content, the absorbed closes cross-referenced
  overtaken    -> closed citing a commit that really is on origin/main (and refused when it isn't)
  nit          -> closed AND filed in the limitations ledger, both links present
  owner-decision -> untouched, and on the owner's sitting sheet with a recommendation

Run from skills/superlooper:  python -m pytest tests/test_triage_flight.py
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import journal
import triage
import triage_run

_ROOT = Path(__file__).resolve().parents[1]
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_BODY = "## Goal\n{goal}\n\n## Definition of done\n- [ ] done\n\n## Boundaries\nnone\n\n## Loop metadata\ntouches: engine\n"

# A stub launcher: records what it was asked to launch and exits 0, so the flight's launch
# handshake is asserted without a session host, a pane, or a real agent anywhere near it.
_FAKE_LAUNCH = """#!/bin/bash
{ printf 'ARGS %s\\n' "$*"
  printf 'ROOT %s\\n' "${SL_RUN_ROOT:-}"
  printf 'TRIAGE_HOME %s\\n' "${SL_TRIAGE_HOME:-}"
  printf 'MODEL %s\\n' "${SL_MODEL:-}"
  printf 'ATTENDED %s\\n' "${SL_ATTENDED:-}"
} >> "$STUB_LOG"
exit "${STUB_RC:-0}"
"""


def _sh(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "git %s failed: %s%s" % (" ".join(args), r.stdout, r.stderr)
    return r.stdout.strip()


class Rig:
    """One adopted repo: a real git checkout, a stateful fake GitHub, and a private state home."""

    def __init__(self, tmp_path, cfg_extra=None):
        self.tmp = tmp_path
        # --- a real origin + working checkout (the flight's home is the real checkout) ---
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       capture_output=True, check=True, timeout=60)
        self.repo = tmp_path / "repo"
        subprocess.run(["git", "clone", str(self.origin), str(self.repo)],
                       capture_output=True, check=True, timeout=60)
        _sh(self.repo, "config", "user.email", "t@example.invalid")
        _sh(self.repo, "config", "user.name", "triage-test")
        (self.repo / "seed.txt").write_text("seed\n")
        _sh(self.repo, "add", "seed.txt")
        _sh(self.repo, "commit", "-m", "seed")
        _sh(self.repo, "push", "origin", "HEAD:main")
        _sh(self.repo, "fetch", "origin")

        (self.repo / ".superlooper").mkdir()
        cfg = {"version": 1, "repo": "o/r", "triage": {"enabled": True, "home": "checkout"}}
        cfg.update(cfg_extra or {})
        (self.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))

        self.home = tmp_path / "slhome" / "o__r"
        (self.home / "state").mkdir(parents=True)
        self.fixdir = tmp_path / "gh"
        self.fixdir.mkdir()
        self.stub_log = tmp_path / "launch-calls.log"
        launcher = tmp_path / "fake-launch-session.sh"
        launcher.write_text(_FAKE_LAUNCH)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        (tmp_path / "userhome").mkdir()
        self.env = {**os.environ,
                    "HOME": str(tmp_path / "userhome"),
                    "SL_HOME": str(tmp_path / "slhome"),
                    "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(self.fixdir),
                    "SL_CMUX": "/nonexistent/superlooper-test-cmux",
                    "SL_LAUNCH_SESSION": str(launcher),
                    "STUB_LOG": str(self.stub_log)}
        for leaky in ("SL_PANE", "GH_FAIL", "SL_ISSUE_ID"):
            self.env.pop(leaky, None)
        self.today = time.strftime("%Y-%m-%d", time.localtime())

    # --- the little GitHub ---
    def github(self, issues, next_num=900):
        state = {"issues": {str(i["number"]): i for i in issues}, "prs": {},
                 "dev_branch": "main", "check_names": ["ci"],
                 "branch_checks": {"main": []}, "next_num": next_num,
                 "next_comment_id": 1}
        (self.fixdir / "state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.fixdir / "state.json").read_text())

    def issue(self, num):
        return self.state()["issues"][str(num)]

    def comments(self, num):
        return [c.get("body", "") for c in self.issue(num).get("comments", [])]

    # --- the loop's own state home ---
    def verdicts(self):
        return triage.load_verdicts(str(self.home))

    def run_log(self, date=None):
        p = Path(triage.run_log_path(str(self.home), date or self.today))
        return p.read_text(encoding="utf-8") if p.exists() else None

    def records(self, act=None):
        recs = [r for r in journal.read(str(self.home))
                if str(r.get("act", "")).startswith("triage")]
        return [r for r in recs if act is None or r.get("act") == act]

    def launches(self):
        if not self.stub_log.exists():
            return []
        blocks, cur = [], {}
        for line in self.stub_log.read_text().splitlines():
            k, _, v = line.partition(" ")
            if k == "ARGS" and cur:
                blocks.append(cur)
                cur = {}
            cur[k] = v
        if cur:
            blocks.append(cur)
        return blocks

    def lease(self):
        """Take the day's lease the way `triage-flight` does — the run log IS the day stamp, and
        nothing but the trigger may create it (a hand-run act must never forge one)."""
        assert triage.mark_launched(str(self.home), self.today) is not None
        return self

    def head(self):
        return _sh(self.repo, "rev-parse", "HEAD")

    def land(self, name, msg):
        """Land a commit on origin/main and return its sha (real evidence for an overtaken close)."""
        (self.repo / name).write_text("landed\n")
        _sh(self.repo, "add", name)
        _sh(self.repo, "commit", "-m", msg)
        sha = self.head()
        _sh(self.repo, "push", "origin", "HEAD:main")
        _sh(self.repo, "fetch", "origin")
        return sha


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def run(rig, *args, env_over=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def out(r):
    assert r.stdout.strip(), "expected --json output, got rc=%s stderr=%s" % (r.returncode, r.stderr)
    return json.loads(r.stdout)


def act(rig, num, verdict, *extra, flight="t1", expect=0):
    r = run(rig, "triage-act", "--repo", str(rig.repo), "--issue", str(num),
            "--verdict", verdict, "--json", *extra, env_over={"SL_ISSUE_ID": flight})
    assert r.returncode == expect, "rc=%s\n%s\n%s" % (r.returncode, r.stdout, r.stderr)
    return out(r)


# --------------------------------------------------------------------------- the issue pile

def _issue(num, title, labels=(), goal=None, state="open", comments=None):
    return {"number": num, "title": title, "state": state,
            "labels": list(labels) or ["type:build", "needs-owner"],
            "body": _BODY.format(goal=goal or ("the goal of %d" % num)),
            "comments": comments or [], "createdAt": "2026-08-01T00:00:0%dZ" % (num % 10)}


def _ledger(num=12):
    import limitations
    return {"number": num, "title": limitations.LEDGER_TITLE, "state": "open",
            "labels": [limitations.LEDGER_LABEL], "body": "## Entries\n\n_(none yet)_\n",
            "comments": [], "isPinned": True, "createdAt": "2026-07-01T00:00:00Z"}


# ======================================================================================
# The four delegated classes, end to end
# ======================================================================================

def test_a_duplicate_pair_absorbs_and_the_absorbed_closes_cross_referenced(rig):
    rig.github([_issue(20, "The absorber"), _issue(21, "The duplicate", goal="same thing")])

    res = act(rig, 21, "duplicate-of-#20", "--why", "both ask for the same retry cap")

    assert res["ok"] and res["action"] == "merge" and res["absorber"] == 20
    # the absorbed issue is CLOSED, and its close comment cross-references the absorber
    assert rig.issue(21)["state"] == "closed"
    closing = [c for c in rig.comments(21) if triage_run.MARKER in c]
    assert closing and "#20" in closing[0]
    # ...the absorber's BODY gained the content (the rule's own words), and it says where from
    absorber = rig.issue(20)
    assert absorber["state"] == "open"
    assert "#21" in absorber["body"] and "same thing" in absorber["body"]
    assert any("#21" in c and triage_run.MARKER in c for c in rig.comments(20))
    # ...and the verdict is on record against the body it was reached on
    assert rig.verdicts()["21"]["verdict"] == "duplicate-of-#20"


def test_an_overtaken_issue_closes_citing_a_commit_that_really_is_on_the_dev_branch(rig):
    sha = rig.land("fix.txt", "the fix that overtook #30")
    rig.github([_issue(30, "Already fixed")])

    res = act(rig, 30, "overtaken", "--commit", sha, "--why", "the retry cap shipped in that commit")

    assert res["ok"] and res["action"] == "close"
    assert rig.issue(30)["state"] == "closed"
    body = [c for c in rig.comments(30) if triage_run.MARKER in c][0]
    assert sha in body and "origin/main" in body and "retry cap shipped" in body
    assert rig.verdicts()["30"]["verdict"] == "overtaken"


def test_an_overtaken_close_is_refused_when_the_cited_commit_is_not_on_the_dev_branch(rig):
    # The guard that makes "commit-level evidence" a check rather than a habit: a commit that
    # exists only in the flight's own checkout is NOT evidence, and the issue stays open.
    (rig.repo / "local.txt").write_text("never pushed\n")
    _sh(rig.repo, "add", "local.txt")
    _sh(rig.repo, "commit", "-m", "local only")
    local = rig.head()
    rig.github([_issue(30, "Not actually fixed")])

    res = act(rig, 30, "overtaken", "--commit", local, "--why", "I think it landed", expect=3)

    assert not res["ok"] and "origin/main" in res["error"]
    assert rig.issue(30)["state"] == "open"
    assert rig.comments(30) == []
    assert "30" not in rig.verdicts(), "a refused act must not record a verdict"
    assert rig.records("triage_refused"), "a refusal is part of the audit trail"


def test_a_nit_closes_to_the_ledger_with_both_links_present(rig):
    rig.github([_issue(40, "The report rounds to whole minutes"), _ledger(12)])

    res = act(rig, 40, "nit(N3)", "--why", "a 20-second run reads as 0m in the report only")

    assert res["ok"] and res["action"] == "close" and res["ledger"] == 12
    assert rig.issue(40)["state"] == "closed"
    # the LEDGER entry: the rubric line, the limitation, and a link back to the closed issue
    entry = [c for c in rig.comments(12) if triage_run.ledger_marker(40) in c]
    assert entry, "the nit must be FILED, not merely closed"
    assert "[N3]" in entry[0] and "Cost exceeds consequence" in entry[0] and "#40" in entry[0]
    # ...and the close comment names the rubric line and links the entry
    closing = [c for c in rig.comments(40) if triage_run.MARKER in c][0]
    assert "N3" in closing and "#12" in closing
    assert res["ledger_link"] in closing and "issuecomment-" in res["ledger_link"]
    assert rig.verdicts()["40"]["verdict"] == "nit(N3)"


def test_a_nit_with_no_ledger_in_the_repo_escalates_instead_of_closing(rig):
    # Boundaries: the ledger protocol here ASSUMES the ledger exists and degrades to escalation
    # when it does not (scaffolding it is #450's, and a flight may not create one).
    rig.github([_issue(40, "A nit with nowhere to file it")])

    res = act(rig, 40, "nit(N3)", "--why", "cosmetic only")

    assert res["ok"] and res["action"] == "escalate"
    assert rig.issue(40)["state"] == "open" and rig.comments(40) == []
    assert "ledger" in res["reason"].lower()


def test_an_unknown_rubric_line_is_refused_before_anything_is_written(rig):
    rig.github([_issue(40, "A nit"), _ledger(12)])
    res = act(rig, 40, "nit(N9)", "--why", "whatever", expect=3)
    assert not res["ok"] and "N9" in res["error"]
    assert rig.issue(40)["state"] == "open"
    assert rig.comments(12) == [], "no ledger entry for a close that never happened"


def test_an_issue_containing_an_owner_decision_is_untouched_and_escalated(rig):
    rig.github([_issue(50, "Which storage engine?")])

    res = act(rig, 50, "contains-owner-decision",
              "--finding", "the body asks which database to use",
              "--recommend", "decide the engine; I will then fix the body")

    assert res["ok"] and res["action"] == "escalate"
    assert rig.issue(50)["state"] == "open"
    assert rig.comments(50) == [], "an escalation is silent on the issue — it goes to the owner"
    assert rig.issue(50)["labels"] == ["type:build", "needs-owner"], "labels untouched"
    assert rig.verdicts()["50"]["verdict"] == "contains-owner-decision"
    rec = rig.records("triage_escalate")[-1]
    assert rec["num"] == 50 and "database" in rec["finding"] and "decide the engine" in rec["recommend"]


def test_an_escalation_without_a_recommendation_is_refused(rig):
    # "one line and a recommendation each" — an escalation with no recommendation hands the owner
    # the work of thinking about it, which is the one thing the flight is for.
    rig.github([_issue(50, "Which storage engine?")])
    res = act(rig, 50, "contains-owner-decision", "--finding", "it asks a question", expect=1)
    assert not res["ok"] and "recommend" in res["error"].lower()


# ======================================================================================
# The delegation's edges
# ======================================================================================

def test_an_approved_issue_is_at_most_flagged_never_edited_merged_or_closed(rig):
    rig.github([_issue(60, "Approved work", labels=["type:build", "agent-ready"]),
                _issue(20, "The absorber"), _ledger(12)])
    before = json.dumps(rig.issue(60), sort_keys=True)

    for verdict, extra in (("overtaken", ("--commit", rig.head(), "--why", "x")),
                           ("nit(N3)", ("--why", "x")),
                           ("duplicate-of-#20", ("--why", "x")),
                           ("underspecified", ("--add-label", "type:investigate"))):
        res = act(rig, 60, verdict, *extra)
        assert res["ok"] and res["action"] == "escalate", verdict
        assert res["held"] is True, verdict

    assert json.dumps(rig.issue(60), sort_keys=True) == before, (
        "an approved issue's body, labels, state and comments are frozen owner text")
    assert "60" not in rig.verdicts(), "an approved issue never earns a verdict"
    # what it DOES get is a line on the owner's sitting sheet, naming what the flight would have done
    assert len(rig.records("triage_escalate")) == 4


def test_an_in_flight_or_awaiting_answer_issue_is_equally_untouchable(rig):
    # `agent-ready` alone is not the predicate — the runner STRIPS it at launch (#448's own
    # finding), so the whole life of a live lane carries `in-progress` instead.
    rig.github([_issue(61, "Running now", labels=["type:build", "in-progress"]),
                _issue(62, "Owner is deciding", labels=["type:build", "awaiting-answer"])])
    for num in (61, 62):
        res = act(rig, num, "overtaken", "--commit", rig.head(), "--why", "x")
        assert res["held"] is True and res["action"] == "escalate"
        assert rig.issue(num)["state"] == "open" and rig.comments(num) == []


def test_a_reopened_issue_with_an_unchanged_body_is_never_re_closed(rig):
    """A reopened issue is owner PROTEST. The flight closed #70 yesterday; the owner put it back."""
    issue = _issue(70, "The owner wants this after all")
    rig.github([issue, _ledger(12)])
    triage.record_verdict(str(rig.home), 70, issue["body"], triage.OVERTAKEN, "2026-08-20")

    res = act(rig, 70, "overtaken", "--commit", rig.head(), "--why", "still overtaken", expect=3)

    assert not res["ok"] and "reopen" in res["error"].lower()
    assert rig.issue(70)["state"] == "open" and rig.comments(70) == []
    # ...and the same protest holds for the other closing classes
    for verdict, extra in (("nit(N3)", ("--why", "x")), ("duplicate-of-#20", ("--why", "x"))):
        assert act(rig, 70, verdict, *extra, expect=3)["ok"] is False
    # the yesterday verdict stands, unchanged — nothing was re-litigated
    assert rig.verdicts()["70"]["date"] == "2026-08-20"


def test_a_reopened_issue_whose_body_changed_is_judgeable_again(rig):
    old = _issue(70, "Reopened and rewritten")
    rig.github([old])
    triage.record_verdict(str(rig.home), 70, old["body"], triage.OVERTAKEN, "2026-08-20")
    # the owner rewrote it after reopening — that is a new issue in every sense that matters
    st = rig.state()
    st["issues"]["70"]["body"] = old["body"] + "\n\nAnd now it also needs the other half.\n"
    (rig.fixdir / "state.json").write_text(json.dumps(st))

    res = act(rig, 70, "buildable")
    assert res["ok"] and rig.verdicts()["70"]["verdict"] == "buildable"


def test_the_approval_labels_are_refused_at_the_call(rig):
    rig.github([_issue(10, "An unapproved issue")])
    for flag, label in (("--add-label", "agent-ready"), ("--remove-label", "agent-ready"),
                        ("--add-label", "pre-authorized:referee")):
        res = act(rig, 10, "underspecified", flag, label, expect=3)
        assert not res["ok"] and label in res["error"]
    assert rig.issue(10)["labels"] == ["type:build", "needs-owner"]


def test_the_verb_refuses_entirely_on_a_repo_that_has_not_opted_in(tmp_path):
    off = Rig(tmp_path, cfg_extra={"triage": {"enabled": False, "home": "checkout"}})
    off.github([_issue(10, "An issue in a repo with triage off")])
    r = run(off, "triage-act", "--repo", str(off.repo), "--issue", "10",
            "--verdict", "buildable", "--json")
    assert r.returncode != 0
    assert "triage.enabled" in out(r)["error"]


# ======================================================================================
# The run's own outputs
# ======================================================================================

def test_a_kept_issue_is_silent_on_github_and_recorded_in_the_store(rig):
    rig.github([_issue(10, "Perfectly buildable")])
    res = act(rig, 10, "buildable")
    assert res["ok"] and res["action"] == "keep"
    assert rig.comments(10) == [], "silence means kept — the rule's own words"
    assert rig.verdicts()["10"] == {"body_hash": triage.body_hash(rig.issue(10)["body"]),
                                    "verdict": "buildable", "date": rig.today}


def test_an_unchanged_body_is_skipped_by_the_next_runs_trigger(rig):
    issue = _issue(10, "Judged once")
    rig.github([issue])
    act(rig, 10, "buildable")
    # the trigger's own question, asked with the store this run just wrote
    fresh = triage.changed([rig.issue(10)], rig.verdicts())
    assert fresh == [], "an issue whose body has not changed is never re-litigated"
    # ...and it becomes a cue again the moment the owner edits it
    edited = dict(rig.issue(10), body=issue["body"] + "\nnow with more detail\n")
    assert triage.changed([edited], rig.verdicts()) == [10]


def test_a_mechanical_format_fix_lands_on_an_unapproved_issue(rig):
    rig.github([_issue(10, "Mislabelled", labels=["type:build", "type:investigate"])])
    # composed where the flight is allowed to write — its state home, never the working tree
    fixed = rig.home / "triage" / "bodies" / "i10.md"
    fixed.parent.mkdir(parents=True, exist_ok=True)
    fixed.write_text(_BODY.format(goal="Mislabelled") + "\nparent: #9\n")

    res = act(rig, 10, "underspecified", "--fix-body-file", str(fixed),
              "--remove-label", "type:investigate", "--add-label", "needs-owner")

    assert res["ok"] and res["action"] == "fix"
    assert rig.issue(10)["labels"] == ["type:build", "needs-owner"]
    assert "parent: #9" in rig.issue(10)["body"]
    assert rig.comments(10) == [], "a format fix is not an announcement"
    assert rig.verdicts()["10"]["verdict"] == "underspecified"


def test_every_act_appends_a_line_to_the_days_run_log(rig):
    rig.lease()
    rig.github([_issue(10, "Kept"), _issue(50, "Owner call"), _ledger(12)])
    act(rig, 10, "buildable")
    act(rig, 50, "contains-owner-decision", "--finding", "a decision", "--recommend", "decide it")
    log = rig.run_log()
    assert log is not None
    assert "#10" in log and "#50" in log
    assert "triage_keep" in log and "triage_escalate" in log


def test_triage_finish_composes_the_sitting_sheet_and_the_runs_tally(rig):
    rig.lease()
    sha = rig.land("fix.txt", "landed")
    rig.github([_issue(10, "Kept"), _issue(20, "Absorber"), _issue(21, "Dup"),
                _issue(30, "Overtaken"), _issue(40, "Nit"), _issue(50, "Owner call"),
                _ledger(12)])
    act(rig, 10, "buildable")
    act(rig, 21, "duplicate-of-#20", "--why", "same ask")
    act(rig, 30, "overtaken", "--commit", sha, "--why", "landed")
    act(rig, 40, "nit(N2)", "--why", "wording only")
    act(rig, 50, "contains-owner-decision", "--finding", "which engine", "--recommend", "pick one")

    r = run(rig, "triage-finish", "--repo", str(rig.repo), "--json",
            env_over={"SL_ISSUE_ID": "t1"})
    assert r.returncode == 0, r.stderr
    res = out(r)

    assert res["counts"] == {"judged": 5, "merged": 1, "closed": 2, "ledger": 1,
                             "fixed": 0, "escalated": 1}
    log = rig.run_log()
    assert triage_run.SITTING_HEADING in log
    assert "#50" in log and "pick one" in log
    assert "judged 5" in log and "1 merged" in log and "2 closed (1 to the ledger)" in log
    # the sheet carries ONLY what the flight could not act on
    sheet = log.split(triage_run.SITTING_HEADING, 1)[1]
    for acted in ("#21", "#30", "#40"):
        assert acted not in sheet


def test_triage_finish_is_honest_on_a_run_that_escalated_nothing(rig):
    rig.lease()
    rig.github([_issue(10, "Kept")])
    act(rig, 10, "buildable")
    res = out(run(rig, "triage-finish", "--repo", str(rig.repo), "--json"))
    assert res["counts"]["escalated"] == 0
    log = rig.run_log()
    assert triage_run.SITTING_HEADING not in log, (
        "a heading over nothing reads as 'the owner has something to do' on a morning he does not")
    assert "judged 1" in log


# ======================================================================================
# The flight itself: the brief, the lease, the launch
# ======================================================================================

def test_the_flight_hands_the_session_a_rendered_brief_and_launches_it(rig):
    rig.github([_issue(10, "Something to look at"), _ledger(12)])
    r = run(rig, "triage-flight", "--repo", str(rig.repo), "--json")
    assert r.returncode == 0, r.stderr
    res = out(r)
    assert res["ok"] and res["id"].startswith("t")

    brief = (rig.home / "briefs" / ("%s.md" % res["id"])).read_text()
    assert "{" not in brief.replace("{}", ""), "an unsubstituted placeholder reached a session"
    assert res["id"] in brief and str(rig.repo) in brief
    assert "#10" in brief, "the pile the flight is to judge must be in its brief"
    assert "#12" in brief, "the ledger it may file nits to must be in its brief"
    assert "triage-standing-rule.md" in brief

    call = rig.launches()[0]
    assert "--triage" in call["ARGS"] and res["id"] in call["ARGS"]
    assert call["TRIAGE_HOME"] == "checkout"
    assert call["ATTENDED"] == "", "nobody is watching a flight"
    assert rig.records("triage_launch")


def test_the_flight_takes_the_day_and_a_second_call_launches_nothing(rig):
    rig.github([_issue(10, "Something to look at")])
    first = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))
    assert first["ok"]
    second = run(rig, "triage-flight", "--repo", str(rig.repo), "--json")
    assert second.returncode != 0
    assert "already" in out(second)["reason"].lower()
    assert len(rig.launches()) == 1


def test_the_flight_check_reports_without_consuming_the_day(rig):
    rig.github([_issue(10, "Something to look at")])
    res = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--check", "--json"))
    assert res["due"] is True and "#10" in res["reason"]
    assert rig.run_log() is None, "--check must not take the lease"
    assert rig.launches() == []
    # ...and the real call still gets the day
    assert out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))["ok"]


def test_the_flight_dry_run_prints_the_brief_and_changes_nothing(rig):
    rig.github([_issue(10, "Something to look at")])
    r = run(rig, "triage-flight", "--repo", str(rig.repo), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "Triage flight" in r.stdout and "#10" in r.stdout
    assert rig.run_log() is None and rig.launches() == []


def test_a_repo_that_has_not_opted_in_never_flies(tmp_path):
    off = Rig(tmp_path, cfg_extra={"triage": {"enabled": False, "home": "checkout"}})
    off.github([_issue(10, "Something")])
    r = run(off, "triage-flight", "--repo", str(off.repo), "--json")
    assert r.returncode != 0
    assert "triage.enabled" in out(r)["reason"]
    assert off.launches() == []


def test_the_flight_does_not_fly_when_no_issue_has_changed(rig):
    issue = _issue(10, "Already judged")
    rig.github([issue])
    triage.record_verdict(str(rig.home), 10, issue["body"], triage.BUILDABLE, "2026-08-20")
    r = run(rig, "triage-flight", "--repo", str(rig.repo), "--json")
    assert r.returncode != 0
    assert "changed" in out(r)["reason"]
    assert rig.launches() == []


def test_the_brief_quotes_the_last_three_run_logs_and_the_verdicts_on_record(rig):
    issue = _issue(10, "Judged before")
    rig.github([_issue(11, "New"), issue])
    triage.record_verdict(str(rig.home), 10, issue["body"], triage.BUILDABLE, "2026-08-20")
    for day, text in (("2026-08-22", "an old run"), ("2026-08-23", "a newer run"),
                      ("2026-08-24", "the newest run"), ("2026-08-21", "too old to quote")):
        triage.mark_launched(str(rig.home), day)       # each day's own lease made its log
        triage.append_run_log(str(rig.home), day, "- %s" % text)

    res = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))
    brief = (rig.home / "briefs" / ("%s.md" % res["id"])).read_text()
    for quoted in ("the newest run", "a newer run", "an old run"):
        assert quoted in brief
    assert "too old to quote" not in brief, "the rule says the last THREE"
    assert "buildable" in brief and "#10" in brief


def test_the_brief_never_lists_an_issue_the_loop_holds(rig):
    rig.github([_issue(10, "Unapproved and open"),
                _issue(60, "Approved", labels=["type:build", "agent-ready"]),
                _issue(61, "Running", labels=["type:build", "in-progress"])])
    res = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))
    brief = (rig.home / "briefs" / ("%s.md" % res["id"])).read_text()
    pile = brief.split("## The pile", 1)[1].split("## ", 1)[0]
    assert "#10" in pile
    assert "#60" not in pile and "#61" not in pile


# ======================================================================================
# The morning report's triage section
# ======================================================================================

def test_the_morning_report_carries_the_triage_section_with_counts_and_links(rig):
    sha = rig.land("fix.txt", "landed")
    rig.github([_issue(20, "Absorber"), _issue(21, "Dup"), _issue(30, "Overtaken"),
                _issue(40, "Nit"), _issue(50, "Owner call"), _ledger(12)])
    act(rig, 21, "duplicate-of-#20", "--why", "same ask")
    act(rig, 30, "overtaken", "--commit", sha, "--why", "landed")
    act(rig, 40, "nit(N2)", "--why", "wording only")
    act(rig, 50, "contains-owner-decision", "--finding", "which engine", "--recommend", "pick one")
    run(rig, "triage-finish", "--repo", str(rig.repo), "--json")

    r = run(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stderr
    report = (rig.home / "reports" / ("morning-%s.md" % rig.today)).read_text()

    assert "## Triage" in report
    assert "1 merged" in report and "2 closed (1 to the ledger)" in report
    assert "1 escalated" in report
    for num in (21, 30, 40, 50):
        assert "https://github.com/o/r/issues/%d" % num in report
    assert "pick one" in report, "the sitting sheet's recommendation reaches the owner"


def test_the_morning_report_is_silent_on_a_day_with_no_triage_run(rig):
    rig.github([_issue(10, "Nothing happened to it")])
    r = run(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stderr
    report = (rig.home / "reports" / ("morning-%s.md" % rig.today)).read_text()
    assert "## Triage" not in report, "a report renders nothing about a flight that never flew"


def test_an_unregistered_label_is_refused_with_the_vocabulary_rather_than_by_gh(rig):
    """The #165/#337 defect class from a new direction: the flight composes label names from prose,
    and gh refuses a label the repo does not have. Refused HERE, with the set it may choose from."""
    rig.github([_issue(10, "An unapproved issue")])
    res = act(rig, 10, "underspecified", "--add-label", "type:buidl", expect=3)
    assert not res["ok"] and "type:buidl" in res["error"] and "type:build" in res["error"]
    assert rig.issue(10)["labels"] == ["type:build", "needs-owner"]
    # a RETIRED label is still removable — clearing `needs-william` beside a live `needs-owner`
    rig.github([_issue(11, "Mid-migration", labels=["type:build", "needs-william"])])
    assert act(rig, 11, "underspecified", "--remove-label", "needs-william")["ok"]
    assert rig.issue(11)["labels"] == ["type:build"]


def test_the_limitations_ledger_is_never_a_triage_subject(rig):
    """A flight FILES to the ledger; a thing you write to is not a thing you triage. It is out of
    the pile the brief hands over, and the verb refuses it outright if one is ever named."""
    rig.github([_issue(10, "An ordinary issue"), _ledger(12)])

    res = act(rig, 12, "underspecified", "--add-label", "needs-owner", expect=3)
    assert not res["ok"] and "ledger" in res["error"].lower()
    assert rig.issue(12)["labels"] == ["limitations-ledger"]
    assert act(rig, 12, "nit(N2)", "--why", "it is wordy", expect=3)["ok"] is False

    # #10 is still an unjudged cue, so a flight is still due — the ledger simply is not one
    flight = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))
    assert flight["ok"], flight
    brief = (rig.home / "briefs" / ("%s.md" % flight["id"])).read_text()
    pile = brief.split("## The pile", 1)[1].split("## ", 1)[0]
    assert "#10" in pile and "#12" not in pile
    # ...but the brief still NAMES it, as the place nits get filed
    assert "#12" in brief


def test_a_hand_run_act_outside_a_flight_never_forges_the_days_lease(rig):
    """The run log IS the day stamp. An operator fixing one issue by hand at 9am must not consume
    the flight — but the act is still durable, in the journal the morning report reads."""
    rig.github([_issue(10, "Fixed by hand"), _issue(50, "Owner call")])
    res = act(rig, 10, "buildable", flight="")           # no SL_ISSUE_ID: nobody launched this
    assert res["ok"]
    assert rig.run_log() is None, "a hand-run act must not stamp the day"
    assert rig.records("triage_keep"), "...but it IS on the record"
    assert rig.records("triage_keep")[0]["flight"] == ""

    # the day is still the flight's to take
    flight = out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))
    assert flight["ok"] and rig.run_log() is not None


# --------------------------- fresh-agent review round 1 ---------------------------

def test_the_ledger_can_never_be_the_ABSORBER_of_a_duplicate_merge(rig):
    """The source-issue guard alone was half a guard (fresh-agent review, P1): `duplicate-of-#<the
    ledger>` would have appended an arbitrary issue's content into the repo's accepted-limitations
    record and then closed the source."""
    rig.github([_issue(21, "An ordinary issue"), _ledger(12)])
    before = json.dumps(rig.issue(12), sort_keys=True)

    res = act(rig, 21, "duplicate-of-#12", "--why", "they feel related", expect=3)

    assert not res["ok"] and "ledger" in res["error"].lower()
    assert json.dumps(rig.issue(12), sort_keys=True) == before, "the ledger is untouched"
    assert rig.issue(21)["state"] == "open"


def test_the_flights_json_contract_survives_a_chatty_launcher(rig):
    """`--json` promises ONE object on stdout. The launcher's own output used to be printed there
    first, so any warning line broke `json.loads(stdout)` for a dashboard or a test."""
    rig.github([_issue(10, "Something to look at")])
    chatty = rig.tmp / "chatty-launch.sh"
    chatty.write_text("#!/bin/bash\necho 'warning: pane reuse'\necho 'note: on stderr' >&2\nexit 0\n")
    chatty.chmod(chatty.stat().st_mode | stat.S_IXUSR)

    r = run(rig, "triage-flight", "--repo", str(rig.repo), "--json",
            env_over={"SL_LAUNCH_SESSION": str(chatty)})
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)                       # would have raised before the fix
    assert res["ok"] and "pane reuse" in res["launcher_output"]
    # a human running it WITHOUT --json still sees the launcher's words
    rig.github([_issue(11, "Another")])
    (rig.home / "triage" / "runs").exists() and None
    plain = run(rig, "triage-flight", "--repo", str(rig.repo),
                env_over={"SL_LAUNCH_SESSION": str(chatty)})
    assert "already went out" in plain.stdout or "pane reuse" in plain.stdout


def test_a_nit_with_no_ledger_stays_judgeable_so_it_closes_once_one_exists(rig):
    """The escalation tells the owner to run `adopt` and says the nit then closes itself. That is
    only true if the issue stays a CUE — recording a verdict against the current body would retire
    it as settled and the finding would be lost forever (fresh-agent review, P1)."""
    rig.github([_issue(40, "A nit with nowhere to file it")])
    res = act(rig, 40, "nit(N3)", "--why", "cosmetic only")
    assert res["ok"] and res["action"] == "escalate"
    assert "40" not in rig.verdicts(), "an unfiled nit must not be retired as judged"
    assert triage.changed([rig.issue(40)], rig.verdicts()) == [40]

    # the owner scaffolds the ledger; the very next act closes and files it
    st = rig.state()
    st["issues"]["12"] = _ledger(12)
    (rig.fixdir / "state.json").write_text(json.dumps(st))
    again = act(rig, 40, "nit(N3)", "--why", "cosmetic only")
    assert again["ok"] and again["action"] == "close" and again["ledger"] == 12
    assert rig.verdicts()["40"]["verdict"] == "nit(N3)"


def test_a_body_file_outside_the_flights_own_state_home_is_refused(rig):
    """A flight's only writes are GitHub and its own state home — so the body it publishes must be
    one it composed THERE. Without this, `--fix-body-file` is a pipe from any local path (a
    gitignored overlay, a credentials file) onto a public issue (fresh-agent review, P1)."""
    rig.github([_issue(10, "An issue")])
    secret = rig.tmp / "not-mine.txt"
    secret.write_text("## Goal\nsomething that is not the flight's to publish\n")
    res = act(rig, 10, "underspecified", "--fix-body-file", str(secret), expect=3)
    assert not res["ok"] and "state home" in res["error"]
    assert "not the flight's to publish" not in rig.issue(10)["body"]

    # the same content, composed where the flight is allowed to write, goes through
    staged = rig.home / "triage" / "bodies" / "i10.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(_BODY.format(goal="A properly composed body") + "\nparent: #9\n")
    ok = act(rig, 10, "underspecified", "--fix-body-file", str(staged))
    assert ok["ok"] and "parent: #9" in rig.issue(10)["body"]


def test_a_nit_close_that_fails_after_its_ledger_entry_is_finished_not_re_filed(rig):
    """The two-write nit protocol, at the seam it exists for.

    The ledger entry is written FIRST so the close comment can link something real. If the close
    then fails, the recoverable state is a FILED limitation on a still-open issue — and the next
    flight must FINISH that close, recognising its own entry by the marker, rather than file a
    second entry for one accepted limitation.
    """
    rig.github([_issue(40, "The report rounds to whole minutes"), _ledger(12)])
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "issue close 40", "times": 1,
          "stderr": "HTTP 502: Bad gateway (issues/40)"}]))

    first = act(rig, 40, "nit(N3)", "--why", "a 20-second run reads as 0m", expect=3)
    assert not first["ok"]
    assert rig.issue(40)["state"] == "open", "the close is what failed"
    entries = [c for c in rig.comments(12) if triage_run.ledger_marker(40) in c]
    assert len(entries) == 1, "the filing landed and stands"
    assert "40" not in rig.verdicts(), "an unfinished act records no verdict"

    second = act(rig, 40, "nit(N3)", "--why", "a 20-second run reads as 0m")
    assert second["ok"] and second["action"] == "close"
    assert rig.issue(40)["state"] == "closed"
    entries = [c for c in rig.comments(12) if triage_run.ledger_marker(40) in c]
    assert len(entries) == 1, "one accepted limitation, one entry — never a second"
    # ...and the close still links the entry that already existed
    closing = [c for c in rig.comments(40) if triage_run.MARKER in c][0]
    assert second["ledger_link"] in closing


def test_a_github_that_cannot_be_read_writes_nothing_and_says_so(rig):
    """Fail closed, loudly. A refused read looks exactly like an unlabelled issue with an empty
    body — i.e. like the one shape every guard here would wave through."""
    rig.github([_issue(40, "An issue"), _ledger(12)])
    r = run(rig, "triage-act", "--repo", str(rig.repo), "--issue", "40",
            "--verdict", "overtaken", "--commit", rig.head(), "--why", "x", "--json",
            env_over={"GH_FAIL": "1", "SL_ISSUE_ID": "t1"})
    assert r.returncode == 1
    assert "could not be read" in out(r)["error"]
    assert rig.issue(40)["state"] == "open" and rig.comments(40) == []
    assert rig.verdicts() == {}


def test_a_ledger_read_that_gitHub_refuses_never_closes_a_nit_unfiled(rig):
    """`marked_issues_health` exists so a REFUSED read and an answered-empty are told apart. Here
    the difference is a nit closed with its filing silently lost."""
    rig.github([_issue(40, "An issue"), _ledger(12)])
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "--label limitations-ledger", "times": 1,
          "stderr": "HTTP 403: rate limit exceeded"}]))
    res = act(rig, 40, "nit(N3)", "--why", "cosmetic", expect=3)
    assert not res["ok"] and "could not be read" in res["error"]
    assert rig.issue(40)["state"] == "open"
    assert rig.comments(12) == [], "nothing filed, because nothing was closed"


def test_the_brief_describes_the_home_the_flight_is_actually_standing_in(tmp_path):
    """`triage.home` selects between two DIFFERENT repositories, and the checkout home is chosen
    precisely FOR the gitignored overlay. A brief that described the checkout to a flight running
    in a detached worktree would have it judging staleness against a repo it is not in."""
    for home, expect, forbid in ((triage.CHECKOUT, "REAL checkout", "DETACHED worktree"),
                                 (triage.WORKTREE, "DETACHED worktree", "REAL checkout")):
        rig = Rig(tmp_path / home, cfg_extra={"triage": {"enabled": True, "home": home}})
        rig.github([_issue(10, "Something to look at")])
        r = run(rig, "triage-flight", "--repo", str(rig.repo), "--dry-run")
        assert r.returncode == 0, r.stderr
        assert expect in r.stdout, home
        assert forbid not in r.stdout, home
        assert "{" not in r.stdout.replace("{}", ""), "the home note must render clean too"
    # ...and the launcher is told which one, so the two can never disagree
    rig = Rig(tmp_path / "launched", cfg_extra={"triage": {"enabled": True, "home": "worktree"}})
    rig.github([_issue(10, "Something")])
    assert out(run(rig, "triage-flight", "--repo", str(rig.repo), "--json"))["ok"]
    assert rig.launches()[0]["TRIAGE_HOME"] == "worktree"


def test_a_branch_name_can_never_stand_in_for_commit_level_evidence(rig):
    """`origin/main` is its own ancestor, so a ref name would satisfy the ancestry check while
    naming no evidence at all — the standing rule's "commit-level evidence" reduced to a formality
    (fresh-agent review round 3, P1)."""
    rig.github([_issue(30, "Allegedly fixed")])
    for cited in ("origin/main", "main", "HEAD"):
        res = act(rig, 30, "overtaken", "--commit", cited, "--why", "it is on main", expect=3)
        assert not res["ok"], cited
        assert "commit" in res["error"].lower()
        assert rig.issue(30)["state"] == "open" and rig.comments(30) == []
    # ...while a real sha still closes it
    sha = rig.land("fix.txt", "the fix")
    assert act(rig, 30, "overtaken", "--commit", sha, "--why", "it landed")["ok"]
