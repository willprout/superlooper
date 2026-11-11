"""`superlooper approve` and the approver stamp (issue #537).

Owner ruling (2026-11-04): the loop consults the session that APPROVED an issue, not the one that
wrote it — so an approving session leaves a signature carrying its session id. Approval used to be
a three-step ceremony taught in prose (label, clear the hand-back labels, audit comment), and a step
taught in prose gets skipped (#215/#225). One verb now performs all of it and appends a hidden,
machine-readable stamp; a reader beside it finds an issue's current approver.

Four things are pinned here:

* the CEREMONY, end to end against the stateful fake GitHub — all three effects, in one call per
  issue, with only the labels actually present removed;
* the STAMP round-trips (session id, host, working directory), and a plain terminal approves
  without one and says so;
* the REFUSAL inside every session kind the runner launches, keyed on the launcher's own
  environment contract so a kind added tomorrow is covered the day it ships;
* the READER trusts only the repo owner's comments, returns the newest stamp, and never lets an
  unstamped approval (the dashboard's button, or one that predates this) clear an earlier stamp.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import approval
import brief
import delivery
import gh
import identity
import labels as labels_lib
import launch

_ROOT = Path(__file__).resolve().parents[1]
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"
_PROTOCOL = (_ROOT.parents[1] / "plugin" / "skills" / "superlooper" / "references"
             / "approval-protocol.md")

SESSION = "1c1e6ffb-0d84-4f0d-b3e0-ae10c5fa781c"
OTHER_SESSION = "9f0e8d7c-6b5a-4f3e-9d2c-1b0a9f8e7d6c"
OWNER = "o"


def _stamp(session=SESSION, host="Williams-Mini", cwd="/Users/w/Projects/superlooper"):
    return approval.Stamp(session=session, host=host, cwd=cwd)


def _comment(body, login=OWNER, created="2026-11-06T19:55:30Z"):
    return {"author": {"login": login}, "body": body, "createdAt": created}


def _approved(stamp=None, operator="William", date="2026-11-06"):
    return approval.audit_comment(operator, date, stamp)


# ============================================================================ the stamp

def test_the_stamp_round_trips_through_the_audit_comment():
    s = _stamp()
    assert approval.parse_stamp(_approved(s)) == s


def test_the_stamp_is_the_hidden_last_line_and_nothing_else():
    """The DoD asks for a HIDDEN machine-readable marker. A visible prose line would also be read,
    verbatim, by every worker whose brief embeds the owner's comments as binding amendments."""
    body = _approved(_stamp())
    first, *_middle, last = [ln for ln in body.splitlines() if ln.strip()]
    assert first == "Approved by William in conversation, 2026-11-06."
    assert last == approval.render_stamp(_stamp()) and last.startswith("<!-- ")
    assert len([ln for ln in body.splitlines() if ln.strip()]) == 2


@pytest.mark.parametrize("cwd", [
    "/Users/w/My Projects/super looper",          # spaces
    "/tmp/a-->b",                                 # the HTML-comment terminator itself
    "/tmp/<script>&amp;",                         # markup characters
    "/Users/w/Prøjects/ünïcode",                  # non-ASCII
])
def test_the_stamp_round_trips_a_directory_whatever_it_contains(cwd):
    """The marker is an HTML comment, so a `-->` in the payload would end it early and leak the
    rest onto the rendered page. The payload is escaped so that can never happen, and the round
    trip is what proves the escaping is lossless."""
    s = _stamp(cwd=cwd)
    body = _approved(s)
    marker_lines = [ln for ln in body.splitlines() if ln.startswith("<!-- ")]
    assert len(marker_lines) == 1 and marker_lines[0].count("-->") == 1
    assert approval.parse_stamp(body) == s


def test_the_audit_comment_begins_with_the_audit_sentence():
    """The sentence comes FIRST, for two readers. A human scanning the issue sees the same audit line
    sessions have always written. And the brief drops a comment that BEGINS with a machine marker
    (`brief._MARKER_PREFIX`), so a stamp-first comment would silently vanish from the worker's view
    of the thread — the stamp rides after the sentence, never before it."""
    body = _approved(_stamp())
    assert body.startswith("Approved by William in conversation, 2026-11-06.")
    assert not body.lstrip().startswith(brief._MARKER_PREFIX)


def test_with_no_session_the_comment_is_the_bare_audit_sentence():
    assert _approved(None) == "Approved by William in conversation, 2026-11-06."
    assert approval.parse_stamp(_approved(None)) is None


def test_make_stamp_records_the_short_host_name():
    """`socket.gethostname()` answers `Williams-Mini.lan` on one network and `Williams-Mini.local`
    on another; a consult routes on the MACHINE, so the stamp keeps only the stable part."""
    s = approval.make_stamp(SESSION, "Williams-Mini.lan", "/x")
    assert s == approval.Stamp(session=SESSION, host="Williams-Mini", cwd="/x")


@pytest.mark.parametrize("session, host, cwd", [
    ("", "h", "/x"),                              # no session at all
    (None, "h", "/x"),
    ("not a uuid; rm -rf", "h", "/x"),            # not an id shape
    ("x" * 200, "h", "/x"),                       # absurdly long
    (SESSION, "", "/x"),                          # no host
    (SESSION, "h", ""),                           # no directory
    (SESSION, "h", None),
    (SESSION, 7, "/x"),                           # wrong-typed
    (SESSION[:8] + "\n" + SESSION[8:], "h", "/x"),  # an id with a newline inside it
    (SESSION, "h", "/x\n<!-- injected -->"),      # a control character smuggles a second line
    (SESSION, "h\tx", "/x"),
])
def test_make_stamp_refuses_anything_it_could_not_read_back(session, host, cwd):
    assert approval.make_stamp(session, host, cwd) is None


def test_identity_owns_the_spelling_of_the_agents_session_variable():
    """`CLAUDE_CODE_SESSION_ID` is Claude Code's own variable, and the agent boundary (CLAUDE.md)
    says such a spelling lives only in the named agent files. identity.py is one; the verb and the
    stamp module ask it rather than spelling it themselves."""
    assert identity.SESSION_ID_VAR == "CLAUDE_CODE_SESSION_ID"
    assert identity.own_session_id({"CLAUDE_CODE_SESSION_ID": " %s \n" % SESSION}) == SESSION
    assert identity.own_session_id({}) == ""
    assert identity.own_session_id({"CLAUDE_CODE_SESSION_ID": ""}) == ""
    src = (_ROOT / "skill" / "lib" / "approval.py").read_text()
    assert "CLAUDE_CODE_SESSION_ID" not in src
    assert "CLAUDE_CODE_SESSION_ID" not in CLI.read_text()


# ============================================================================ the reader

def test_the_reader_returns_the_newest_stamp():
    comments = [_comment(_approved(_stamp(OTHER_SESSION)), created="2026-11-01T00:00:00Z"),
                _comment("a discussion comment"),
                _comment(_approved(_stamp(SESSION)), created="2026-11-06T00:00:00Z")]
    assert approval.latest_stamp(comments, OWNER) == _stamp(SESSION)


def test_the_reader_ignores_a_stamp_in_a_comment_the_owner_did_not_author():
    comments = [_comment(_approved(_stamp(SESSION))),
                _comment(_approved(_stamp(OTHER_SESSION)), login="some-bot")]
    assert approval.latest_stamp(comments, OWNER) == _stamp(SESSION)
    assert approval.latest_stamp(comments[1:], OWNER) is None


@pytest.mark.parametrize("author", [None, "o", {"login": None}, {"login": 7}, {}, {"login": "O"}])
def test_the_reader_never_trusts_an_ambiguous_author(author):
    """Fail closed, the same rule as the brief's amendments: a missing or wrong-typed author is not
    the owner, and nor is a near-match (logins are compared exactly, as the brief compares them)."""
    c = {"author": author, "body": _approved(_stamp())}
    assert approval.latest_stamp([c], OWNER) is None


def test_the_reader_ignores_a_stamp_that_is_not_on_its_own_line():
    """A comment DISCUSSING the format — quoting a marker in backticks or mid-sentence — is not an
    approval."""
    line = approval.render_stamp(_stamp())
    for body in ("the marker looks like `%s`" % line, "see %s for the format" % line):
        assert approval.latest_stamp([_comment(body)], OWNER) is None


_QUOTED_AUDIT = _approved(_stamp(OTHER_SESSION))


@pytest.mark.parametrize("body", [
    # an orchestrator explaining the format, quoting a whole audit comment in a code block
    "Here is what an approval looks like:\n\n```\n%s\n```" % _QUOTED_AUDIT,
    "~~~\n%s\n~~~" % _QUOTED_AUDIT,
    # a fenced block that opens before the sentence and never closes
    "```\n%s" % _QUOTED_AUDIT,
    # a memo that pastes the marker on its own line, under some other first line
    "Worker memo: copied for the record\n\n%s" % approval.render_stamp(_stamp(OTHER_SESSION)),
    # the marker on its own line but NOT last: prose after it is a comment about it
    "%s\n\nand that is how it is done." % _QUOTED_AUDIT,
    # the dashboard's sentence, which never carries a stamp, with one bolted on
    "Approved by William via command-center, 2026-11-06.\n\n%s"
    % approval.render_stamp(_stamp(OTHER_SESSION)),
    # a bare marker
    approval.render_stamp(_stamp(OTHER_SESSION)),
])
def test_only_a_comment_shaped_like_an_audit_comment_carries_a_stamp(body):
    """Every loop session posts as the owner's own login, so authorship cannot tell an approval
    from a comment that merely QUOTES one. Shape can: the audit sentence first, the marker last,
    no code fence. A quoted stamp is no stamp, and it never displaces the real, earlier one."""
    assert approval.parse_stamp(body) is None
    comments = [_comment(_approved(_stamp())), _comment(body)]
    assert approval.latest_stamp(comments, OWNER) == _stamp()


@pytest.mark.parametrize("later", [
    "Approved by William via command-center, 2026-11-07.",      # the dashboard's button
    "Approved by William in conversation, 2026-11-07.",         # a hand ceremony that predates #537
    "Approved by William in conversation, 2026-11-07.\n\n"
    "Approving session: `%s` on Williams-Mini, cwd `/x`." % OTHER_SESSION,  # the prose precedent
])
def test_an_unstamped_approval_never_clears_an_earlier_stamp(later):
    """Owner ruling: an unstamped approval falls back to William — but only where there is no stamp
    at all. An unstamped re-approval AFTER a stamped one is not evidence that some other session
    approved; it is no evidence about the session at all, so the earlier stamp stands."""
    comments = [_comment(_approved(_stamp())), _comment(later)]
    assert approval.latest_stamp(comments, OWNER) == _stamp()


@pytest.mark.parametrize("garbage", [
    "<!-- superlooper-approver {not json} -->",
    "<!-- superlooper-approver [1, 2] -->",
    '<!-- superlooper-approver {"host": "h", "cwd": "/x"} -->',
    '<!-- superlooper-approver {"session": "bad id!", "host": "h", "cwd": "/x"} -->',
    '<!-- superlooper-approver {"session": "%s", "host": 3, "cwd": "/x"} -->' % OTHER_SESSION,
    '<!-- superlooper-approver {"session": "%s", "host": "h", "cwd": ""} -->' % OTHER_SESSION,
    '<!-- superlooper-approver {"session": "%s\\n", "host": "h", "cwd": "/x"} -->' % OTHER_SESSION,
    '<!-- superlooper-approver {"session": "%s", "host": "h", "cwd": "/x\\u0000"} -->'
    % OTHER_SESSION,
])
def test_a_malformed_stamp_is_no_stamp_and_never_clears_an_earlier_one(garbage):
    later = "Approved by William in conversation, 2026-11-07.\n\n" + garbage
    comments = [_comment(_approved(_stamp())), _comment(later)]
    assert approval.latest_stamp(comments, OWNER) == _stamp()
    assert approval.latest_stamp(comments[1:], OWNER) is None


@pytest.mark.parametrize("comments", [None, "x", {}, [None, 3, "s"], [{"body": None}]])
def test_the_reader_fails_closed_on_wrong_typed_input(comments):
    assert approval.latest_stamp(comments, OWNER) is None


@pytest.mark.parametrize("owner", [None, "", 7])
def test_with_no_derivable_owner_nothing_is_trusted(owner):
    assert approval.latest_stamp([_comment(_approved(_stamp()))], owner) is None


def test_the_engine_reader_uses_the_briefs_owner_rule_and_reports_a_refused_read():
    """`approval.read` is the call the engine makes. It derives the owner exactly as the brief's
    amendments do (one owner definition), and it keeps a REFUSED comment read distinct from "no
    stamp": a consult that read a GitHub outage as "nobody stamped this" would route to William
    for no reason he could see (the #21 refused-vs-answered-empty discipline)."""
    cfg = {"repo": "o/r"}
    assert brief.owner_login(cfg) == OWNER
    thread = [_comment(_approved(_stamp()))]
    got = approval.read(5, cfg, comments_reader=lambda n: gh.CommentRead(thread, True))
    assert got == approval.ApproverRead(stamp=_stamp(), ok=True)
    got = approval.read(5, cfg, comments_reader=lambda n: gh.CommentRead([], False))
    assert got == approval.ApproverRead(stamp=None, ok=False)
    got = approval.read(5, cfg, comments_reader=lambda n: gh.CommentRead([], True))
    assert got == approval.ApproverRead(stamp=None, ok=True)
    got = approval.read(5, {"repo": "nope"}, comments_reader=lambda n: gh.CommentRead(thread, True))
    assert got == approval.ApproverRead(stamp=None, ok=True)


# ============================================================================ the labels

def test_the_ceremony_labels_are_registered_vocabulary():
    registered = {name for name, _c, _d in labels_lib.LABELS}
    assert approval.AGENT_READY in registered
    assert set(approval.CLEARED_BY_APPROVAL) <= registered | set(labels_lib.RETIRED_LABELS)
    assert set(approval.CLEARED_BY_APPROVAL) == {"needs-owner", "needs-william", "parked"}


def test_only_the_hand_back_labels_actually_present_are_cleared():
    """A repo that finished the #58 rename no longer defines `needs-william`, and `gh issue edit` is
    all-or-nothing: naming a label the REPO lacks fails the whole edit, the `agent-ready` add
    included (the dashboard paid for this in #114). Clearing only what the issue carries means the
    verb never names a label the repo might not have."""
    assert approval.labels_to_clear(["type:build", "needs-owner"]) == ["needs-owner"]
    assert approval.labels_to_clear(["parked", "needs-william", "needs-owner"]) == [
        "needs-owner", "needs-william", "parked"]
    assert approval.labels_to_clear(["type:build"]) == []
    assert approval.labels_to_clear(None) == []


# ============================================================================ the refusal

def _launched_env(iid, tmp_path):
    """The environment the launcher hands a session of this kind — built by the launcher's own
    function, so the refusal is tested against the contract every spawn path actually uses."""
    spec = launch.Spec(id=iid, run_root=str(tmp_path / "home"), engine_bin=str(tmp_path / "bin"),
                       repo=str(tmp_path / "repo"))
    return launch.pane_environment(spec, iid, SESSION, False, "willprout", "tok-123")


# One case per session kind the engine's launchers can produce today. `actions._is_session_id` is
# the engine's own list of launchable shapes (i/d/t); if a kind is added there, it belongs here.
_KINDS = [("i7", "a build worker"), ("d3", "a repair (sl-debugger) session"),
          ("t2", "a triage flight")]


@pytest.mark.parametrize("iid, kind", _KINDS, ids=[k for _i, k in _KINDS])
def test_every_session_the_runner_launches_is_recognised(iid, kind, tmp_path):
    assert approval.launched_by_runner(_launched_env(iid, tmp_path)), kind


def test_the_kinds_here_are_every_kind_the_engine_can_launch():
    import actions
    for iid, _kind in _KINDS:
        assert actions._is_session_id(iid)
    assert {iid[0] for iid, _k in _KINDS} == {"i", "d", "t"}
    assert not actions._is_session_id("c4"), (
        "the engine can now launch a new session kind — add it to _KINDS above")


def test_a_kind_the_engine_does_not_know_yet_is_refused_too(tmp_path):
    """A consult (#536-#539) does not exist yet. It will be launched through the same launcher, and
    the refusal keys on what the LAUNCHER names, not on the id's shape — so it is covered the day it
    ships, without anyone remembering to widen a list."""
    assert approval.launched_by_runner(_launched_env("c4", tmp_path))


@pytest.mark.parametrize("var", approval.LAUNCH_MARKERS)
def test_any_one_launch_marker_is_enough(var):
    assert approval.launched_by_runner({var: "x"}) == var


def test_a_plain_terminal_or_a_supervised_session_is_not_refused():
    assert approval.launched_by_runner({}) is None
    assert approval.launched_by_runner({"CLAUDE_CODE_SESSION_ID": SESSION, "HOME": "/h"}) is None


@pytest.mark.parametrize("value", ["", "  ", None, 7])
def test_a_blanked_or_wrong_typed_marker_still_counts(value):
    """The launcher never leaves a marker out, so one that is PRESENT but blank is a launched session
    that blanked it (`SL_ISSUE_ID= superlooper approve 5`). Fail closed: a wrong refusal costs the
    owner a re-run; a wrong pass costs `agent-ready` applied by a loop session."""
    assert approval.launched_by_runner({"SL_ISSUE_ID": value}) == "SL_ISSUE_ID"


@pytest.mark.parametrize("env", [None, ["SL_ISSUE_ID"], "SL_ISSUE_ID=i7", 7])
def test_an_unreadable_environment_is_refused(env):
    assert approval.launched_by_runner(env) == approval.UNREADABLE_ENV


def test_every_launch_marker_is_one_the_launcher_names():
    """The other direction: a marker the launcher never sets would be a check that cannot fire."""
    env = _launched_env("i7", Path("/tmp/x"))
    for var in approval.LAUNCH_MARKERS:
        assert var in env, "%s is not named by launch.pane_environment" % var


# ============================================================================ the verb, end to end

class Rig:
    """An adopted repo's config, a stateful fake GitHub, and a clean (supervised) environment."""

    def __init__(self, tmp_path, operator="William"):
        self.repo = tmp_path / "repo"
        (self.repo / ".superlooper").mkdir(parents=True)
        cfg = {"version": 1, "repo": "o/r"}
        if operator:
            cfg["operator"] = operator
        (self.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))
        self.fixdir = tmp_path / "gh"
        self.fixdir.mkdir()
        self.cwd = tmp_path / "orchestrator"
        self.cwd.mkdir()
        (tmp_path / "userhome").mkdir()
        self.env = {**os.environ, "HOME": str(tmp_path / "userhome"),
                    "SL_HOME": str(tmp_path / "slhome"),
                    "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(self.fixdir),
                    identity.SESSION_ID_VAR: SESSION}
        for leaky in approval.LAUNCH_MARKERS + ("GH_FAIL", "GH_REPO"):
            self.env.pop(leaky, None)

    def github(self, issues):
        state = {"issues": {str(i["number"]): i for i in issues}, "prs": {},
                 "dev_branch": "main", "next_num": 900, "next_comment_id": 1,
                 "viewer": OWNER}
        (self.fixdir / "state.json").write_text(json.dumps(state))

    def fail(self, match, times=1):
        (self.fixdir / "fail_rules.json").write_text(json.dumps([{"match": match, "times": times}]))

    def issue(self, num):
        return json.loads((self.fixdir / "state.json").read_text())["issues"][str(num)]

    def mutations(self):
        p = self.fixdir / "mutations.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def calls(self):
        """EVERY gh invocation's argv, reads included (the fake logs them before anything else)."""
        p = self.fixdir / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def approve(self, *nums, env_over=None, drop=()):
        env = {**self.env, **(env_over or {})}
        for name in drop:
            env.pop(name, None)
        return subprocess.run([sys.executable, str(CLI), "approve", "--repo", str(self.repo),
                               *[str(n) for n in nums]],
                              capture_output=True, text=True, env=env, cwd=str(self.cwd),
                              timeout=120)


def _issue(num, labels=("type:build", "needs-owner")):
    return {"number": num, "title": "issue %d" % num, "state": "open", "labels": list(labels),
            "body": "## Goal\ng\n", "comments": [], "createdAt": "2026-11-01T00:00:00Z"}


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _today():
    import time
    return time.strftime("%Y-%m-%d", time.localtime())


def test_approve_performs_the_whole_ceremony_in_one_call(rig):
    rig.github([_issue(5, labels=("type:build", "needs-owner", "needs-william", "parked"))])
    r = rig.approve(5)
    assert r.returncode == 0, r.stdout + r.stderr
    issue = rig.issue(5)
    # 1. agent-ready applied
    assert "agent-ready" in issue["labels"]
    # 2. every hand-back label removed
    assert not {"needs-owner", "needs-william", "parked"} & set(issue["labels"])
    assert "type:build" in issue["labels"]
    # 3. the audit comment, with the stamp
    bodies = [c["body"] for c in issue["comments"]]
    assert len(bodies) == 1
    assert bodies[0].startswith("Approved by William in conversation, %s." % _today())
    assert approval.parse_stamp(bodies[0]) == approval.Stamp(
        session=SESSION, host=socket.gethostname().split(".")[0],
        cwd=os.path.realpath(str(rig.cwd)))


def test_the_stamp_the_verb_posts_is_the_one_the_reader_returns(rig):
    """The full round trip: posted by the verb, read back through the engine's own reader, over the
    same comment shape `gh issue view --json comments` serves."""
    rig.github([_issue(5)])
    assert rig.approve(5).returncode == 0
    thread = rig.issue(5)["comments"]
    got = approval.read(5, {"repo": "o/r"}, comments_reader=lambda n: gh.CommentRead(thread, True))
    assert got.ok and got.stamp.session == SESSION


def test_approve_takes_several_issues_and_approves_each(rig):
    rig.github([_issue(5), _issue(6, labels=("type:build", "parked")), _issue(7)])
    r = rig.approve(5, 6, 7)
    assert r.returncode == 0, r.stdout + r.stderr
    for n in (5, 6, 7):
        assert "agent-ready" in rig.issue(n)["labels"]
        assert len(rig.issue(n)["comments"]) == 1
    assert "parked" not in rig.issue(6)["labels"]


def test_a_repeated_number_is_approved_once(rig):
    rig.github([_issue(5)])
    assert rig.approve(5, 5).returncode == 0
    assert len(rig.issue(5)["comments"]) == 1


def test_approve_names_only_labels_the_issue_carries(rig):
    """The #114 trap, live: if the verb ever named `needs-william` on an issue that lacks it, this
    fake fails that call — as real gh does on a repo that finished the rename."""
    rig.github([_issue(5, labels=("type:build", "needs-owner"))])
    rig.fail("needs-william", times=99)
    r = rig.approve(5)
    assert r.returncode == 0, r.stdout + r.stderr
    edits = [m for m in rig.mutations() if m.get("kind") == "set_labels"]
    assert edits and all("needs-william" not in json.dumps(m) for m in edits)
    assert "agent-ready" in rig.issue(5)["labels"]
    assert "needs-owner" not in rig.issue(5)["labels"]


def test_a_plain_terminal_approves_without_a_stamp_and_says_so(rig):
    rig.github([_issue(5)])
    r = rig.approve(5, drop=(identity.SESSION_ID_VAR,))
    assert r.returncode == 0, r.stdout + r.stderr
    issue = rig.issue(5)
    assert "agent-ready" in issue["labels"]
    assert [c["body"] for c in issue["comments"]] == [
        "Approved by William in conversation, %s." % _today()]
    assert "no session was recorded" in r.stderr
    assert "an earlier stamp on an issue still stands" in r.stderr


@pytest.mark.parametrize("iid, kind", _KINDS + [("c4", "a kind not built yet (a consult)")],
                         ids=[k for _i, k in _KINDS] + ["a future consult"])
def test_approve_refuses_inside_every_session_the_runner_launched(rig, tmp_path, iid, kind):
    """`agent-ready` stays the owner's word relayed by a SUPERVISED session. Inside any session the
    runner launched, the verb refuses before reading or writing anything at all."""
    rig.github([_issue(5)])
    env = dict(_launched_env(iid, tmp_path))
    env["SL_GH"] = str(_FAKE_GH)            # if the refusal ever failed, still never reach real gh
    r = rig.approve(5, env_over=env)
    assert r.returncode == 3, "%s was not refused:\n%s%s" % (kind, r.stdout, r.stderr)
    assert "refused" in r.stderr and "nothing was written" in r.stderr.lower()
    assert rig.calls() == [], "the refusal must come before ANY GitHub call, reads included"
    assert "agent-ready" not in rig.issue(5)["labels"]
    assert "tok-123" not in r.stderr          # the launch token is never echoed


def test_blanking_the_launch_markers_on_the_command_line_is_still_refused(rig, tmp_path):
    """`SL_ISSUE_ID= SL_RUN_ROOT= … superlooper approve 5` from inside a worker: the fresh-agent
    review's P1. Present-but-blank is a launched session. (`env -u` removes them outright; the worker
    hook, which reads the AGENT's environment, is what denies that — test_pretooluse_hook.py.)"""
    rig.github([_issue(5)])
    r = rig.approve(5, env_over={var: "" for var in approval.LAUNCH_MARKERS})
    assert r.returncode == 3, r.stdout + r.stderr
    assert rig.calls() == []


def _record(cwd, session, message=None):
    """One user record in the agent's REAL field order, measured on a live transcript (#542):
    `message` comes BEFORE `cwd`. So everything up to a record's `cwd` is its message, and the
    first record carrying a `cwd` is the session's first user message — however large that is."""
    return json.dumps({"parentUuid": None, "isSidechain": False, "type": "user",
                       "message": message or {"role": "user", "content": "go"},
                       "uuid": "00000000-0000-4000-8000-000000000000",
                       "timestamp": "2026-11-11T00:00:00.000Z", "userType": "external",
                       "entrypoint": "cli", "cwd": cwd, "sessionId": session,
                       "version": "2.1.0", "gitBranch": "main"})


def _preamble(session):
    """The records the agent writes before the first user message, none of which carries a `cwd`."""
    return [json.dumps({"type": "custom-title", "customTitle": "t", "sessionId": session}),
            json.dumps({"type": "permission-mode", "permissionMode": "default",
                        "sessionId": session}),
            json.dumps({"type": "file-history-snapshot", "messageId": "m", "snapshot": {},
                        "isSnapshotUpdate": False})]


def _transcript(home, project_slug, session, cwds, first_message=None):
    d = home / ".claude" / "projects" / project_slug
    d.mkdir(parents=True, exist_ok=True)
    lines = [_record(c, session, first_message if i == 0 else None) for i, c in enumerate(cwds)]
    path = d / ("%s.jsonl" % session)
    path.write_text("\n".join(_preamble(session) + lines) + "\n")
    return path


def test_the_stamp_records_the_directory_the_session_started_in(rig, tmp_path):
    """A session's shell wanders (`cd skills/x` to run a suite), and so does the `cwd` on each of its
    transcript records — measured on a live session. The agent files the transcript under the
    directory the session STARTED in, which is the one a resume must run in: the first record."""
    started = str(tmp_path / "the-project")
    _transcript(tmp_path / "userhome", "-the-project", SESSION,
                [started, started + "/skills/superlooper", started])
    rig.github([_issue(5)])
    assert rig.approve(5).returncode == 0
    stamp = approval.parse_stamp(rig.issue(5)["comments"][0]["body"])
    assert stamp.cwd == started
    assert stamp.cwd != os.path.realpath(str(rig.cwd))


def test_the_agents_own_config_dir_is_where_its_transcript_is_looked_for(rig, tmp_path):
    started = str(tmp_path / "elsewhere")
    cfg = tmp_path / "fleet-config"
    d = cfg / "projects" / "-elsewhere"
    d.mkdir(parents=True)
    (d / ("%s.jsonl" % SESSION)).write_text(json.dumps({"cwd": started}) + "\n")
    rig.github([_issue(5)])
    assert rig.approve(5, env_over={identity.CONFIG_DIR_VAR: str(cfg)}).returncode == 0
    assert approval.parse_stamp(rig.issue(5)["comments"][0]["body"]).cwd == started


def test_with_no_transcript_the_stamp_falls_back_to_the_working_directory(rig):
    rig.github([_issue(5)])
    assert rig.approve(5).returncode == 0
    stamp = approval.parse_stamp(rig.issue(5)["comments"][0]["body"])
    assert stamp.cwd == os.path.realpath(str(rig.cwd))


def _screenshot_message(images):
    """A first message with pasted screenshots, which the agent stores INLINE as base64. 1.4MB a
    record is the largest single-image user record measured on the owner's machine (median 740KB)."""
    blob = "iVBORw0KGgo" + "A" * (1400 * 1024)
    return {"role": "user", "content": [{"type": "text", "text": "look at this"}] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": blob}}
        for _ in range(images)]}


@pytest.mark.parametrize("first_message", [
    _screenshot_message(1),
    _screenshot_message(3),
    {"role": "user", "content": "a long kickoff. " * 12000},          # ~190KB of typed brief
], ids=["one pasted screenshot", "three pasted screenshots", "a long kickoff"])
def test_a_large_first_message_still_yields_the_directory_the_session_started_in(
        rig, tmp_path, first_message):
    """Issue #542. The first record carrying a `cwd` is the session's first user message, and its
    `cwd` comes AFTER its `message`. A 64KB head read dropped that record as a fragment whenever the
    message was bigger than the head, and the stamp silently fell back to wherever the shell had
    wandered — the wrong directory for a resume, the exact case the transcript read exists for."""
    started = str(tmp_path / "the-project")
    path = _transcript(tmp_path / "userhome", "-the-project", SESSION,
                       [started, started + "/skills/superlooper"], first_message=first_message)
    raw = path.read_bytes()
    first = raw.split(b"\n")[len(_preamble(SESSION))]
    assert first.index(b'"message"') < first.index(b'"cwd"'), "the fixture must keep the real order"
    assert raw.index(b'"cwd"') > 64 * 1024, "the fixture must put the first cwd past the old head"
    rig.github([_issue(5)])
    r = rig.approve(5)
    assert r.returncode == 0, r.stdout + r.stderr
    stamp = approval.parse_stamp(rig.issue(5)["comments"][0]["body"])
    assert stamp.cwd == started


def test_a_pathologically_large_head_falls_back_rather_than_read_without_bound(rig, tmp_path):
    """End to end at the DEFAULT bound, `delivery.MAX_HEAD_BYTES`: a transcript whose first
    cwd-bearing record starts past it gets None, and the verb falls back to its working directory
    rather than stamping the `cwd` waiting past the bound. The file is sparse, so its size costs the
    test no disk. This pins where an answer may come from; that the read also TAKES no more than the
    bound is the counting test's job below."""
    d = tmp_path / "userhome" / ".claude" / "projects" / "-the-project"
    d.mkdir(parents=True)
    path = d / ("%s.jsonl" % SESSION)
    with open(path, "wb") as f:
        f.write(b'{"type": "user", "message": {"role": "user", "content": "')
        f.seek(delivery.MAX_HEAD_BYTES + 4096)                    # a hole of NULs, no newline
        f.write(b'"}}\n' + _record(str(tmp_path / "too-late"), SESSION).encode() + b"\n")
    rig.github([_issue(5)])
    r = rig.approve(5)
    assert r.returncode == 0, r.stdout + r.stderr
    stamp = approval.parse_stamp(rig.issue(5)["comments"][0]["body"])
    assert stamp.cwd == os.path.realpath(str(rig.cwd))


class _CountingFile:
    """A file that counts every byte it hands back, however it is asked for them."""

    def __init__(self, f):
        self._f, self.handed = f, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()

    def __iter__(self):
        for line in self._f:
            self.handed += len(line)
            yield line

    def __getattr__(self, name):
        attr = getattr(self._f, name)
        if not callable(attr):
            return attr

        def counted(*a, **kw):
            out = attr(*a, **kw)
            if isinstance(out, (bytes, bytearray)):
                self.handed += len(out)
            elif isinstance(out, list):
                self.handed += sum(len(x) for x in out if isinstance(x, (bytes, bytearray)))
            return out
        return counted


def test_the_start_directory_read_never_takes_more_than_its_bound(tmp_path, monkeypatch):
    """The bound is a ceiling on bytes READ, not only on where an answer is accepted from: past it
    the read stops, so a huge transcript costs at most the bound however large it is."""
    home = tmp_path / "home"
    started = str(tmp_path / "the-project")
    path = _transcript(home, "-the-project", SESSION, [started],
                       first_message={"role": "user", "content": "x" * 300000})
    with open(path, "a") as f:                       # a long session's worth after the answer
        f.write(_record(started + "/skills", SESSION,
                        {"role": "user", "content": "y" * 2000000}) + "\n")
    opened = []
    real_open = open

    def counting_open(*a, **kw):
        opened.append(_CountingFile(real_open(*a, **kw)))
        return opened[-1]
    monkeypatch.setattr(delivery, "open", counting_open, raising=False)
    env = {"HOME": str(home)}

    assert delivery.session_directory(SESSION, env=env, max_bytes=100000) is None
    assert opened and sum(f.handed for f in opened) <= 100000
    # The same transcript under a bound it fits inside: the answer, read once, through to its first
    # cwd and no further. The 2MB of records after it are never taken.
    assert delivery.session_directory(SESSION, env=env, max_bytes=4000000) == started
    assert sum(f.handed for f in opened[1:]) < 400000


@pytest.mark.parametrize("bound", [0, -5])
def test_a_degenerate_bound_answers_none_rather_than_reading_everything(tmp_path, bound):
    home = tmp_path / "home"
    _transcript(home, "-p", SESSION, [str(tmp_path / "p")])
    assert delivery.session_directory(SESSION, env={"HOME": str(home)}, max_bytes=bound) is None


def test_a_last_record_with_no_trailing_newline_still_counts(tmp_path):
    """A transcript the agent is mid-way through writing may end without a newline; a complete
    record there is still a record."""
    home = tmp_path / "home"
    path = _transcript(home, "-p", SESSION, [str(tmp_path / "p")])
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    assert delivery.session_directory(SESSION, env={"HOME": str(home)}) == str(tmp_path / "p")


def test_an_unreadable_issue_is_not_approved_and_the_rest_still_are(rig):
    rig.github([_issue(5), _issue(7)])
    r = rig.approve(5, 6, 7)                  # #6 does not exist
    assert r.returncode == 1
    assert "#6" in r.stderr
    assert "agent-ready" in rig.issue(5)["labels"] and "agent-ready" in rig.issue(7)["labels"]
    assert not [m for m in rig.mutations() if str(m.get("num")) == "6"]


def test_a_failed_comment_never_leaves_a_label_without_its_audit_trail(rig):
    """The comment is posted FIRST. `agent-ready` without its audit comment is a label nobody can
    trace back to the owner's word — and, now, an approval with no stamp — so a failed comment
    stops that issue before the label moves."""
    rig.github([_issue(5)])
    rig.fail("issue comment 5")
    r = rig.approve(5)
    assert r.returncode == 1
    assert "agent-ready" not in rig.issue(5)["labels"]
    assert "needs-owner" in rig.issue(5)["labels"]


def test_a_failed_label_move_after_the_comment_says_so_and_fails(rig):
    rig.github([_issue(5)])
    rig.fail("issue edit 5")
    r = rig.approve(5)
    assert r.returncode == 1
    assert "re-run" in r.stderr
    assert "agent-ready" not in rig.issue(5)["labels"]


def test_with_no_operator_configured_the_comment_signs_the_owner_login(tmp_path):
    """The skill is universal: the name comes from config (`operator`, else the repo owner's login),
    the same rule the dashboard's button signs with — never a hardcoded name."""
    rig = Rig(tmp_path, operator=None)
    rig.github([_issue(5)])
    assert rig.approve(5).returncode == 0
    assert rig.issue(5)["comments"][0]["body"].startswith(
        "Approved by o in conversation, %s." % _today())


def test_a_missing_config_is_a_clean_failure(rig, tmp_path):
    rig.github([_issue(5)])
    (rig.repo / ".superlooper" / "config.json").unlink()
    r = rig.approve(5)
    assert r.returncode == 1 and "superlooper adopt" in r.stderr
    assert rig.calls() == []


# ============================================================================ the doc

def test_the_approval_protocol_names_the_verb():
    text = _PROTOCOL.read_text()
    assert "`superlooper approve" in text


def test_the_protocol_says_which_refusal_covers_which_agent(tmp_path):
    """Issue #542. The loop's tool-call hook is Claude-only (`worker_pretooluse.run` returns early for
    Codex), so the doc must not promise the hook in every launched session: a Codex session relies on
    the verb's own refusal. Both halves are pinned against the hook itself, so the doc's sentences
    get revisited if the hook stops denying the verb in a Claude session, or if `run` stops
    returning early for Codex."""
    import worker_pretooluse
    wt = tmp_path / "worktrees" / "i7" / ".superlooper"
    wt.mkdir(parents=True)
    (wt / "config.json").write_text(json.dumps({"version": 1, "repo": "o/r"}))
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "superlooper approve 5"}}
    worker = {"SL_ISSUE_ID": "i7", "SL_RUN_ROOT": str(tmp_path)}
    assert worker_pretooluse.run(payload, {**worker, "SL_AGENT": "claude"}), \
        "the doc says the hook denies the verb in a Claude session"
    assert worker_pretooluse.run(payload, {**worker, "SL_AGENT": "codex"}) is None
    text = " ".join(_PROTOCOL.read_text().split())
    assert "Both the verb and the loop's tool-call hook refuse" not in text
    assert "The verb itself refuses it in every session the runner launched" in text
    assert "In a Claude session the loop's tool-call hook also denies the command" in text
    assert "a Codex session relies on the verb's own refusal" in text
