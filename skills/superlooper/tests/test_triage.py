"""The triage flight's state contract and its daily trigger (issue #448).

The delegation itself is RULED and lives in
``plugin/skills/superlooper/references/triage-standing-rule.md``; this suite pins the MECHANICS
that rule needs and nothing about what the flight decides:

  * the state contract — a ``triage/`` folder in the state home holding per-run markdown logs and
    the durable verdicts file (issue -> body-hash, verdict, date), verdicts surviving across runs,
  * the body-hash comparison that keeps an unchanged issue from ever being re-litigated,
  * the trigger's three rules: OFF by default, at most one flight per local day, and only when
    some open issue's body has changed since the last recorded verdicts.

The clock is INJECTED everywhere — the local date is a parameter, never read from the wall clock
(the #164 lesson: an e2e harness inherits the real clock, so a trigger that read one could only be
tested by waiting for midnight).
"""
import json
import os
import subprocess
import sys

import triage


# --------------------------------------------------------------------------- helpers

def _issue(num, body):
    """The raw gh issue shape the open-issue view already carries (`gh.py` _ISSUE_FIELDS)."""
    return {"number": num, "title": "t%s" % num, "body": body, "labels": []}


def _cfg(enabled=None, home=None):
    block = {}
    if enabled is not None:
        block["enabled"] = enabled
    if home is not None:
        block["home"] = home
    return {"repo": "o/r", "triage": block}


# --------------------------------------------------------------------------- the state contract

def test_the_state_contract_is_a_runs_folder_and_a_verdicts_file(tmp_path):
    """DoD: a triage folder in the state home holding per-run markdown logs and the durable
    verdicts file. Named here so the layout is a pinned contract rather than four hand-rolled
    path joins spread across the wave's other parts."""
    assert triage.home(tmp_path) == os.path.join(str(tmp_path), "triage")
    assert triage.runs_dir(tmp_path) == os.path.join(str(tmp_path), "triage", "runs")
    assert triage.verdicts_path(tmp_path) == os.path.join(str(tmp_path), "triage",
                                                          "verdicts.json")
    assert triage.run_log_path(tmp_path, "2026-08-25") == os.path.join(
        str(tmp_path), "triage", "runs", "2026-08-25.md")


def test_a_verdict_survives_across_runs(tmp_path):
    """The whole point of the durable file: an issue judged once is never re-litigated, so the
    verdict has to outlive the session that reached it."""
    triage.record_verdict(tmp_path, 12, "the body", triage.BUILDABLE, "2026-08-25")
    again = triage.load_verdicts(tmp_path)          # a FRESH read, as the next flight would do
    assert again["12"] == {"body_hash": triage.body_hash("the body"),
                           "verdict": triage.BUILDABLE, "date": "2026-08-25"}


def test_recording_a_verdict_never_drops_the_others(tmp_path):
    triage.record_verdict(tmp_path, 12, "a", triage.BUILDABLE, "2026-08-25")
    triage.record_verdict(tmp_path, 13, "b", triage.OVERTAKEN, "2026-08-25")
    v = triage.load_verdicts(tmp_path)
    assert sorted(v) == ["12", "13"]


def test_a_re_judged_issue_replaces_its_own_entry(tmp_path):
    """One entry per issue: the LATEST verdict is what "has this body been judged" is asked of."""
    triage.record_verdict(tmp_path, 12, "first", triage.UNDERSPECIFIED, "2026-08-24")
    triage.record_verdict(tmp_path, 12, "second", triage.BUILDABLE, "2026-08-25")
    v = triage.load_verdicts(tmp_path)
    assert list(v) == ["12"]
    assert v["12"]["verdict"] == triage.BUILDABLE
    assert v["12"]["body_hash"] == triage.body_hash("second")


def test_an_unreadable_verdicts_file_reads_as_no_verdicts(tmp_path):
    """FAIL CLOSED in the direction that costs a re-read, never a missed one: an unreadable
    ledger of verdicts must make every issue look UNJUDGED (so it is looked at again), never
    JUDGED (so it is silently skipped forever)."""
    os.makedirs(triage.home(tmp_path))
    with open(triage.verdicts_path(tmp_path), "w") as f:
        f.write("{not json")
    assert triage.load_verdicts(tmp_path) == {}
    assert triage.changed([_issue(1, "x")], triage.load_verdicts(tmp_path)) == [1]


def test_a_wrong_typed_verdicts_file_reads_as_no_verdicts(tmp_path):
    os.makedirs(triage.home(tmp_path))
    with open(triage.verdicts_path(tmp_path), "w") as f:
        json.dump(["not", "a", "map"], f)
    assert triage.load_verdicts(tmp_path) == {}


def test_no_verdicts_file_yet_reads_as_no_verdicts(tmp_path):
    assert triage.load_verdicts(tmp_path) == {}


# --------------------------------------------------------------------------- the body hash

def test_the_body_hash_is_stable_and_content_addressed():
    assert triage.body_hash("hello") == triage.body_hash("hello")
    assert triage.body_hash("hello") != triage.body_hash("hello ")


def test_line_endings_are_never_a_body_change():
    """GitHub returns CRLF for a body edited in the web UI. A re-fetch that only differed in line
    endings would re-litigate every issue in the repo, every day, forever."""
    assert triage.body_hash("a\r\nb\r\n") == triage.body_hash("a\nb\n")


def test_a_wrong_typed_body_still_hashes():
    """Nothing in this module may raise into a tick — a missing or wrong-typed gh field hashes as
    empty, which reads as "changed" and therefore as "look at it", the safe direction."""
    for junk in (None, 42, [], {}):
        assert triage.body_hash(junk) == triage.body_hash("")


# --------------------------------------------------------------------------- changed bodies

def test_an_unchanged_body_is_never_re_litigated(tmp_path):
    triage.record_verdict(tmp_path, 7, "same", triage.BUILDABLE, "2026-08-24")
    assert triage.changed([_issue(7, "same")], triage.load_verdicts(tmp_path)) == []


def test_an_edited_body_is_changed(tmp_path):
    triage.record_verdict(tmp_path, 7, "before", triage.BUILDABLE, "2026-08-24")
    assert triage.changed([_issue(7, "after")], triage.load_verdicts(tmp_path)) == [7]


def test_an_issue_with_no_verdict_at_all_is_changed(tmp_path):
    triage.record_verdict(tmp_path, 7, "same", triage.BUILDABLE, "2026-08-24")
    assert triage.changed([_issue(7, "same"), _issue(9, "new")],
                          triage.load_verdicts(tmp_path)) == [9]


def test_changed_is_ordered_and_deduplicated(tmp_path):
    got = triage.changed([_issue(9, "a"), _issue(2, "b"), _issue(9, "a")], {})
    assert got == [2, 9]


def test_an_approved_issue_never_summons_a_flight_however_long_it_sits(tmp_path):
    """The bound this module exists to keep, against the one input that would break it forever.

    The standing rule forbids the flight from acting on an `agent-ready` issue, so no verdict is
    ever recorded for one — and "no verdict means changed" would then make every approved issue
    permanently changed. A repo with one approved issue and an otherwise quiet queue would have
    launched an unattended session every day, forever, with nothing it was permitted to do.
    """
    home = str(tmp_path)
    cfg = {"triage": {"enabled": True}}
    approved = {"number": 100, "body": "approved and frozen",
                "labels": [{"name": "type:build"}, {"name": "agent-ready"}]}
    unapproved = {"number": 200, "body": "still a draft", "labels": [{"name": "type:build"}]}

    assert triage.changed([approved, unapproved], {}) == [200], "only the unapproved one is a cue"
    triage.record_verdict(home, 200, unapproved["body"], triage.BUILDABLE, "2026-08-25")
    triage.mark_launched(home, "2026-08-25")

    # Day after day, with the approved issue sitting there untouched, nothing is due.
    for day in ("2026-08-26", "2026-08-27", "2026-08-28"):
        due, why = triage.due([approved, unapproved], home, day, cfg)
        assert due is False, "%s: %s" % (day, why)
        assert "changed" in why

    # ...and editing the APPROVED issue's frozen text still summons nothing: that edit is the
    # owner's own, and the flight is not allowed to have an opinion that acts on it.
    approved["body"] = "approved, and edited by the owner"
    assert triage.due([approved, unapproved], home, "2026-08-29", cfg)[0] is False
    # while one character in the UNAPPROVED one does.
    unapproved["body"] = "still a draft, revised"
    assert triage.due([approved, unapproved], home, "2026-08-29", cfg)[0] is True


def test_a_wrong_typed_label_set_reads_as_unapproved(tmp_path):
    """`issues.label_names` yields [] for any malformed label set, and [] means unapproved — the
    direction that costs a second look rather than an issue silently never triaged."""
    for bad in (None, "agent-ready", [None, 7], [{"nope": 1}], {}):
        assert triage.changed([{"number": 5, "body": "x", "labels": bad}], {}) == [5], bad


def test_changed_never_raises_on_a_malformed_view():
    """The open-issue view can come back partial or wrong-typed from a broken gh call. An entry
    this cannot read is simply not an issue to triage — never an exception into a tick."""
    assert triage.changed(None, {}) == []
    assert triage.changed("not a list", {}) == []
    assert triage.changed([None, 5, {}, {"number": "x"}, {"number": True}, _issue(3, "y")],
                          {}) == [3]
    assert triage.changed([_issue(3, "y")], "not a map") == [3]
    assert triage.changed([_issue(3, "y")], {"3": "not an entry"}) == [3]


# --------------------------------------------------------------------------- the day stamp

def test_the_day_is_stamped_at_launch_not_by_the_flight(tmp_path):
    """The bound is "never a second launch the same day", so the stamp has to land BEFORE the
    session exists. A flight that dies without ever writing its own log must still not be
    re-launched an hour later."""
    assert triage.ran_on(tmp_path, "2026-08-25") is False
    path = triage.mark_launched(tmp_path, "2026-08-25")
    assert path == triage.run_log_path(tmp_path, "2026-08-25")
    assert os.path.isfile(path), "the run log IS the day stamp"
    assert triage.ran_on(tmp_path, "2026-08-25") is True
    assert triage.ran_on(tmp_path, "2026-08-26") is False


def test_the_stamp_is_a_LEASE_that_exactly_one_caller_can_take(tmp_path):
    """The bound is not "check, then write" — that is a race two runners lose together. The stamp
    is CREATED EXCLUSIVELY, so of two callers asking for the same day exactly one is handed a
    path and the other is told no. Anything else and a machine running two runners (a restart
    overlapping its predecessor, a hand `superlooper run` beside the LaunchAgent) flies twice."""
    first = triage.mark_launched(tmp_path, "2026-08-25")
    assert first is not None
    assert triage.mark_launched(tmp_path, "2026-08-25") is None, \
        "the second caller must NOT be handed a launch"
    assert triage.mark_launched(tmp_path, "2026-08-26") is not None, "a new day is a new lease"


def test_a_lost_lease_never_touches_the_winners_log(tmp_path):
    """The loser of the race must not truncate the log the winner's flight is already writing."""
    triage.mark_launched(tmp_path, "2026-08-25")
    with open(triage.run_log_path(tmp_path, "2026-08-25"), "a") as f:
        f.write("\nthe flight wrote this\n")
    assert triage.mark_launched(tmp_path, "2026-08-25") is None
    assert "the flight wrote this" in open(triage.run_log_path(tmp_path, "2026-08-25")).read()


def test_a_stamp_that_could_not_be_written_is_reported_as_none(tmp_path):
    """A caller that cannot stamp the day MUST NOT launch — the stamp is the only thing bounding
    the flight to one a day, and launching without it is an unbounded relaunch loop."""
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")
    assert triage.mark_launched(blocked, "2026-08-25") is None


def test_an_unreadable_day_stamp_reads_as_already_ran(tmp_path):
    """The other direction of the same fail-closed rule: if we cannot tell whether today's flight
    already went out, the answer that costs a missed day beats the one that flies twice."""
    runs = tmp_path / "triage" / "runs"
    runs.mkdir(parents=True)
    assert triage.ran_on(tmp_path, None) is True
    assert triage.ran_on(tmp_path, "") is True
    assert triage.ran_on(tmp_path, "../escape") is True


def test_a_date_that_is_not_one_can_never_name_a_path(tmp_path):
    """The date becomes a path SEGMENT, so the guard belongs on the path builder itself and not
    only on the two callers that happen to check first — a later caller would inherit the hole."""
    for junk in ("../escape", "2026-08-25/../../x", "..", "/etc/passwd", "", None, 20260825,
                 "2026-8-25"):
        assert triage.run_log_path(tmp_path, junk) is None, junk
        assert triage.mark_launched(tmp_path, junk) is None, junk
        assert triage.ran_on(tmp_path, junk) is True, junk


def test_concurrent_verdict_writers_do_not_lose_each_others_rulings(tmp_path):
    """Atomicity is not exclusion. `loopstate.save` guarantees no reader sees half a ruling, but
    two writers that both load, both add an entry and both save leave only the second one's — and
    the first issue is then permanently unjudged, which is the one thing this store exists to
    prevent. Driven with REAL processes, because a threaded stand-in would not exercise the
    file-level mutex the fix uses (fresh-agent review, P2)."""
    home = str(tmp_path)
    triage.record_verdict(home, 1, "seed", triage.BUILDABLE, "2026-08-25")   # create the folder
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import triage\n"
        "triage.record_verdict(sys.argv[1], int(sys.argv[2]), 'body-' + sys.argv[2],\n"
        "                      triage.BUILDABLE, '2026-08-25')\n"
        % os.path.dirname(triage.__file__))
    procs = [subprocess.Popen([sys.executable, str(script), home, str(n)])
             for n in range(10, 26)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    recorded = triage.load_verdicts(home)
    assert sorted(int(k) for k in recorded) == [1] + list(range(10, 26)), \
        "every writer's ruling must survive: %s" % sorted(recorded)


def test_recent_run_logs_never_raises_on_a_wrong_typed_limit(tmp_path):
    """The module's no-raise posture holds for every argument, not only the ones a caller is
    likely to get right."""
    triage.mark_launched(tmp_path, "2026-08-25")
    assert triage.recent_run_logs(tmp_path) == [("2026-08-25", "# Triage flight 2026-08-25\n\n")]
    for junk in (None, "three", [], -1, 0):
        assert triage.recent_run_logs(tmp_path, limit=junk) == []
    assert triage.recent_run_logs(tmp_path / "nowhere") == []


def test_recent_run_logs_are_newest_first_and_bounded(tmp_path):
    for date in ("2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25"):
        triage.mark_launched(tmp_path, date)
    assert [d for d, _ in triage.recent_run_logs(tmp_path)] == ["2026-08-25", "2026-08-24",
                                                                "2026-08-23"]


# --------------------------------------------------------------------------- the config reads

def test_triage_is_off_unless_a_repo_says_otherwise():
    assert triage.enabled({"repo": "o/r"}) is False
    assert triage.enabled(_cfg(enabled=False)) is False
    assert triage.enabled(_cfg(enabled=True)) is True


def test_a_wrong_typed_enable_is_not_an_enable():
    """Fail closed on the master switch: only a real boolean true arms the flight, so a config
    read half-way through a write can never turn it on."""
    for junk in ("true", 1, [], None, {}):
        assert triage.enabled({"repo": "o/r", "triage": {"enabled": junk}}) is False
    assert triage.enabled(None) is False
    assert triage.enabled({"repo": "o/r", "triage": "on"}) is False


def test_the_home_defaults_to_the_repos_real_checkout():
    """The ruled default: the flight sees what an orchestrator sees, gitignored overlay included."""
    assert triage.home_kind({"repo": "o/r"}) == triage.CHECKOUT
    assert triage.home_kind(_cfg(home="worktree")) == triage.WORKTREE
    for junk in ("Checkout", "", None, 3, {}):
        assert triage.home_kind({"repo": "o/r", "triage": {"home": junk}}) == triage.CHECKOUT


# --------------------------------------------------------------------------- the trigger

def test_the_master_switch_off_never_launches(tmp_path):
    """Ships DISABLED (issue #448): the brief the session receives is part 2 of the wave, so
    until it lands nothing may fly — and a repo that says nothing about triage says OFF."""
    due, why = triage.due([_issue(1, "brand new")], tmp_path, "2026-08-25", {"repo": "o/r"})
    assert due is False
    assert "disabled" in why
    assert not os.path.exists(triage.home(tmp_path)), "a disabled repo touches no state at all"


def test_unchanged_bodies_since_the_last_verdicts_do_not_launch(tmp_path):
    triage.record_verdict(tmp_path, 1, "same", triage.BUILDABLE, "2026-08-24")
    triage.record_verdict(tmp_path, 2, "also same", triage.OVERTAKEN, "2026-08-24")
    due, why = triage.due([_issue(1, "same"), _issue(2, "also same")], tmp_path,
                          "2026-08-25", _cfg(enabled=True))
    assert due is False
    assert "changed" in why


def test_a_changed_issue_launches_exactly_one_flight_that_day(tmp_path):
    """The full trigger, driven with an injected clock: a changed body launches, the launch
    stamps the day, and the same day never launches again — however many ticks it sees."""
    triage.record_verdict(tmp_path, 1, "before", triage.BUILDABLE, "2026-08-24")
    view = [_issue(1, "AFTER")]

    due, why = triage.due(view, tmp_path, "2026-08-25", _cfg(enabled=True))
    assert due is True, why
    assert "#1" in why

    assert triage.mark_launched(tmp_path, "2026-08-25") is not None
    for _tick in range(5):
        due, why = triage.due(view, tmp_path, "2026-08-25", _cfg(enabled=True))
        assert due is False
        assert "2026-08-25" in why


def test_a_new_issue_launches_a_flight(tmp_path):
    due, why = triage.due([_issue(48, "a brand new issue")], tmp_path, "2026-08-25",
                          _cfg(enabled=True))
    assert due is True, why
    assert "#48" in why


def test_the_next_day_flies_again_while_something_is_still_unjudged(tmp_path):
    """The bound is per DAY, not per change: yesterday's flight went out, the issue it was
    launched for is still unjudged (it crashed, say), and tomorrow's tick flies again."""
    view = [_issue(1, "unjudged")]
    triage.mark_launched(tmp_path, "2026-08-25")
    assert triage.due(view, tmp_path, "2026-08-25", _cfg(enabled=True))[0] is False
    assert triage.due(view, tmp_path, "2026-08-26", _cfg(enabled=True))[0] is True


def test_a_flight_that_recorded_its_verdicts_does_not_fly_again_tomorrow(tmp_path):
    """The two rules together: yesterday's flight judged everything, nothing has changed since,
    so tomorrow is quiet. This is the steady state — a triage flight is not a daily chore."""
    view = [_issue(1, "judged")]
    triage.mark_launched(tmp_path, "2026-08-25")
    triage.record_verdict(tmp_path, 1, "judged", triage.BUILDABLE, "2026-08-25")
    assert triage.due(view, tmp_path, "2026-08-26", _cfg(enabled=True))[0] is False


def test_an_empty_view_never_launches(tmp_path):
    assert triage.due([], tmp_path, "2026-08-25", _cfg(enabled=True))[0] is False
    assert triage.due(None, tmp_path, "2026-08-25", _cfg(enabled=True))[0] is False


def test_a_date_the_trigger_cannot_read_never_launches(tmp_path):
    """No usable local date means the one-a-day bound cannot be applied at all, and a trigger
    that fired anyway would be unbounded. Fail closed."""
    for junk in (None, "", 20260825, "  "):
        due, why = triage.due([_issue(1, "new")], tmp_path, junk, _cfg(enabled=True))
        assert due is False, junk
        assert "date" in why
