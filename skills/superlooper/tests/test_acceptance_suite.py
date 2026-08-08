"""The acceptance suite as a runnable artifact (issue #348).

``bin/acceptance.py`` is the version-bump ritual `docs/HERDR-ADOPTION-PLAN.md` §8.1 requires,
turned from #311's transcript into something a person RUNS. These tests pin the half of it that can
be judged without standing a host up and paying for real agent sessions: the criteria it reports
against, the verdict arithmetic, the paired-control discipline, the refusals that keep it off the
live fleet, and the promise that every signal it sends goes to a pid it started itself.

What is deliberately NOT tested here is the half that costs real sessions — a drill against a real
patched binary with a real agent in the pane. Faking that would be testing the fake: the substrate's
failures are exactly at the agent seam, which is why the suite is opt-in and why #311's run had to be
a live one. The seam between the two is the pure verdict functions below, which take drill
OBSERVATIONS and produce findings; that is what makes the judging testable at all.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import tempfile

import pytest

import session_host


_REPO = pathlib.Path(__file__).resolve().parents[3]
_SUITE = _REPO / "skills/superlooper/bin/acceptance.py"


def _load():
    spec = importlib.util.spec_from_file_location("sl_acceptance", _SUITE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = _load()


# --------------------------------------------------------------- the criteria are READ, not owned

def test_the_criteria_come_from_the_architecture_doc_not_from_the_script():
    """The boundary in the issue: the criteria live in the doc and the script reports against them.

    A suite that spelled its own twelve requirements would drift from the bar it claims to measure,
    silently, in the direction of whatever the script happens to do.
    """
    found = acceptance.read_criteria(_REPO)
    assert [c.id for c in found] == ["S%d" % n for n in range(1, 13)]
    assert all(c.requirement.strip() for c in found), "a criterion with no requirement text"
    # Spot-anchors read out of docs/ARCHITECTURE-PROPOSALS.md §3 itself.
    by_id = {c.id: c for c in found}
    assert "atomic spawn" in by_id["S1"].title
    assert "hollow-tab" in by_id["S1"].requirement
    assert "subscription billing" in by_id["S10"].title


def test_a_criteria_doc_that_cannot_be_parsed_is_a_loud_failure():
    """No silent fallback to a built-in list — that is how the script would start owning them."""
    with pytest.raises(acceptance.SuiteError):
        acceptance.criteria("## Some other document\n\nNothing here names a criterion.\n")


def test_the_criteria_parser_keeps_the_doc_s_own_wording():
    text = ("### The criteria (each: requirement → acceptance test → what it retires)\n\n"
            "**I. Process contract** — S1 atomic spawn (launch runs the exact command or errors).\n"
            "S2 lifecycle as events (exit code + liveness as process facts).\n\n"
            "**II. Host independence** — S3 something (a third one).\n")
    got = acceptance.criteria(text)
    assert [(c.id, c.title) for c in got] == [
        ("S1", "atomic spawn"), ("S2", "lifecycle as events"), ("S3", "something")]
    assert got[0].requirement == "launch runs the exact command or errors"
    assert got[0].group == "I. Process contract"
    assert got[2].group == "II. Host independence"


# ------------------------------------------------------------------- verdicts and the exit code

def _report(ids=("S1", "S2", "S3")):
    return acceptance.Report([acceptance.Criterion(i, "t" + i, "req " + i, "G") for i in ids])


def test_a_failed_criterion_makes_the_run_exit_non_zero():
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    report.record(acceptance.Finding("S2", acceptance.FAIL, "an artefact", "broken"))
    report.record(acceptance.Finding("S3", acceptance.PASS, "an artefact", "fine"))
    assert report.exit_code() == acceptance.CRITERION_FAILED


def test_a_clean_run_exits_zero():
    report = _report()
    for cid in ("S1", "S2", "S3"):
        report.record(acceptance.Finding(cid, acceptance.PASS, "an artefact", "fine"))
    assert report.exit_code() == acceptance.OK


def test_a_criterion_the_suite_cannot_judge_is_reported_not_run_and_is_never_a_pass():
    """DoD: never silently skipped, never counted as a pass."""
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    report.record(acceptance.Finding("S2", acceptance.NOT_RUN, "nothing",
                                     "a full night with the display off is not one session's work"))
    report.record(acceptance.Finding("S3", acceptance.PARTIAL, "an artefact", "as designed"))
    counts = report.counts()
    assert counts[acceptance.PASS] == 1
    assert counts[acceptance.NOT_RUN] == 1
    assert counts[acceptance.PARTIAL] == 1
    # Never a pass, never silently skipped, and the verdict line says so out loud. The exit code
    # is RUN_INCOMPLETE rather than OK: this criterion was not run because the run could not run
    # it, which is a different thing from a standing ruling (see the next test).
    assert report.exit_code() == acceptance.RUN_INCOMPLETE
    assert "NOT GREEN" in report.summary()
    assert "S2" in report.summary()


def test_a_criterion_not_run_by_standing_ruling_does_not_make_the_run_incomplete():
    """S11 is NOT RUN permanently and correctly; a run carrying only that IS a complete run.

    The distinction the suite's second real run needed. Every lane failed to start, so most
    criteria came back NOT RUN — and without this the suite exited 0 on a run that had measured
    nothing at all, which is the worst answer it could give.
    """
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    report.record(acceptance.Finding("S2", acceptance.PASS, "an artefact", "fine"))
    report.record(acceptance.Finding("S3", acceptance.NOT_RUN, "nothing — deliberately",
                                     "attaching a client would break the clientless posture",
                                     by_ruling=True))
    assert report.fell_over() == []
    assert report.exit_code() == acceptance.OK


def test_a_run_whose_lanes_never_started_does_not_exit_zero():
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    report.record(acceptance.Finding("S2", acceptance.NOT_RUN, "nothing", "no lane started"))
    report.record(acceptance.Finding("S3", acceptance.NOT_RUN, "nothing", "no lane started"))
    assert report.fell_over() == ["S2", "S3"]
    assert report.exit_code() == acceptance.RUN_INCOMPLETE
    assert "did not complete its own plan" in report.summary()


def test_a_not_run_finding_must_carry_its_reason():
    with pytest.raises(ValueError):
        acceptance.Finding("S5", acceptance.NOT_RUN, "nothing", "")


def test_a_criterion_with_no_finding_at_all_breaks_the_run_rather_than_passing_quietly():
    """A suite that reports on eleven of twelve criteria and exits 0 is worse than no suite."""
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    assert set(report.unjudged()) == {"S2", "S3"}
    assert report.exit_code() == acceptance.RUN_INCOMPLETE


def test_every_criterion_gets_its_own_named_evidence_file(tmp_path):
    report = _report()
    report.record(acceptance.Finding("S1", acceptance.PASS, "pane process-info argv", "detail one",
                                     artefacts=(("argv", "claude --settings /x"),)))
    report.record(acceptance.Finding("S2", acceptance.FAIL, "a state read", "detail two"))
    report.record(acceptance.Finding("S3", acceptance.NOT_RUN, "nothing", "needs a whole night"))
    written = report.write(str(tmp_path))

    for cid in ("S1", "S2", "S3"):
        path = tmp_path / ("%s.md" % cid)
        assert path.exists(), "%s has no evidence file" % cid
        body = path.read_text()
        assert "req " + cid in body, "the evidence file does not carry the criterion's requirement"
    assert "claude --settings /x" in (tmp_path / "S1.md").read_text()
    assert (tmp_path / "summary.md").exists()
    assert str(tmp_path / "summary.md") in written


# ------------------------------------------------------------------ the paired-control discipline

def _outcome(lane, captured, detail="d"):
    return acceptance.LaneOutcome(lane=lane, captured=captured, detail=detail)


def _lanes(**captured):
    return {name: _outcome(name, value) for name, value in captured.items()}


def test_the_capture_drill_passes_only_when_every_lane_answered_as_its_pair_predicts():
    finding = acceptance.capture_verdict(_lanes(**{
        "fenced-hooked": True, "unfenced-hooked": True, "fenced-unhooked": False}))
    assert finding.verdict == acceptance.PASS
    assert "fence" in finding.detail.lower()


def test_a_capture_drill_whose_control_did_not_run_is_not_run_rather_than_a_pass():
    """The pairing is the whole point: an unpaired pass can be a pass for the wrong reason."""
    finding = acceptance.capture_verdict(_lanes(**{
        "fenced-hooked": True, "fenced-unhooked": False}))
    assert finding.verdict == acceptance.NOT_RUN
    assert "unfenced-hooked" in finding.detail


def test_a_silent_capture_beside_a_capturing_control_reads_as_a_refused_call():
    """#311's headline. One variable — the fence — and the control is what makes it conclusive."""
    finding = acceptance.capture_verdict(_lanes(**{
        "fenced-hooked": False, "unfenced-hooked": True, "fenced-unhooked": False}))
    assert finding.verdict == acceptance.FAIL
    assert "refused" in finding.detail.lower()
    assert "build" in finding.detail.lower()          # ...and NOT a broken build; say which


def test_a_silent_capture_on_both_lanes_reads_as_a_broken_build_not_as_the_fence():
    finding = acceptance.capture_verdict(_lanes(**{
        "fenced-hooked": False, "unfenced-hooked": False, "fenced-unhooked": False}))
    assert finding.verdict == acceptance.FAIL
    assert "neither" in finding.detail.lower() or "both" in finding.detail.lower()
    assert "fence" in finding.detail.lower()


def test_a_control_lane_that_captures_without_the_hook_invalidates_the_drill():
    """#307's control: if an unhooked lane captures an id, the capture is not coming from the hook
    and the drill is measuring something other than what it claims."""
    finding = acceptance.capture_verdict(_lanes(**{
        "fenced-hooked": True, "unfenced-hooked": True, "fenced-unhooked": True}))
    assert finding.verdict == acceptance.FAIL
    assert "hook" in finding.detail.lower()


def test_the_fence_drill_runs_its_own_control_too():
    """A socket that refused EVERY caller would pass the fenced half and be a broken host."""
    ok = acceptance.fence_verdict({"fenced": session_host.FENCED, "unfenced": session_host.OPEN})
    assert ok.verdict == acceptance.PASS
    broken = acceptance.fence_verdict({"fenced": session_host.FENCED,
                                       "unfenced": session_host.FENCED})
    assert broken.verdict == acceptance.FAIL
    assert "control" in broken.detail.lower()
    unpaired = acceptance.fence_verdict({"fenced": session_host.FENCED})
    assert unpaired.verdict == acceptance.NOT_RUN


# ------------------------------------ criteria derived from a drill that did not run

def _capture(verdict=None):
    return acceptance.Finding(acceptance.CAPTURE_DRILL, verdict or acceptance.PASS, "a drill",
                              "detail")


def test_criteria_derived_from_a_drill_that_did_not_run_are_not_run_rather_than_failed():
    """The defect the suite's SECOND real run produced.

    Every lane failed to start (a loaded machine), the capture drill was correctly NOT RUN — and S6
    and S12 were reported FAIL anyway, because they were derived from "did the fenced lane
    capture?" without first asking whether anything had been measured. A FAIL nobody measured is
    the same defect as a PASS nobody measured with the sign flipped, and it is the more dangerous
    one: it sends somebody to rebuild a host that was never tested.
    """
    findings = acceptance.derived_verdicts(
        _capture(acceptance.NOT_RUN),
        {"fenced-hooked": acceptance.LaneOutcome("fenced-hooked", None, "the lane never started")})
    verdicts = {f.criterion: f.verdict for f in findings}
    assert verdicts == {"S5": acceptance.NOT_RUN, "S6": acceptance.NOT_RUN,
                        "S12": acceptance.NOT_RUN}
    assert all(f.detail.strip() for f in findings)


def test_a_capture_drill_that_ran_and_passed_carries_its_criteria():
    findings = acceptance.derived_verdicts(
        _capture(acceptance.PASS),
        {"fenced-hooked": acceptance.LaneOutcome("fenced-hooked", True, "came back resumed",
                                                 resolves=True)})
    verdicts = {f.criterion: f.verdict for f in findings}
    assert verdicts == {"S5": acceptance.PARTIAL, "S6": acceptance.PASS, "S12": acceptance.PASS}


def test_a_measured_but_failing_capture_fails_hook_continuity_and_not_the_others():
    findings = acceptance.derived_verdicts(
        _capture(acceptance.FAIL),
        {"fenced-hooked": acceptance.LaneOutcome("fenced-hooked", False, "a bare shell",
                                                 resolves=True)})
    verdicts = {f.criterion: f.verdict for f in findings}
    # The lane WAS measured across the restart and the host still addressed it BY NAME, so
    # decoupling held; what failed is the capture.
    assert verdicts["S12"] == acceptance.FAIL
    assert verdicts["S6"] == acceptance.PASS


def test_decoupling_fails_when_the_name_stopped_resolving_even_if_a_lane_was_measured():
    """S6 is a DIFFERENT question from the capture drill's, and an earlier draft conflated them:
    it read "the drill produced a number" as decoupling and passed S6 while its own appended
    detail said the pane had come back a bare shell."""
    findings = acceptance.derived_verdicts(
        _capture(acceptance.FAIL),
        {"fenced-hooked": acceptance.LaneOutcome("fenced-hooked", False, "a bare shell",
                                                 resolves=False)})
    assert {f.criterion: f.verdict for f in findings}["S6"] == acceptance.FAIL


def test_no_criterion_is_derived_twice():
    """Two findings for one criterion would make `counts()` disagree with the table."""
    for capture, outcome in ((acceptance.PASS, True), (acceptance.NOT_RUN, None)):
        findings = acceptance.derived_verdicts(
            _capture(capture),
            {"fenced-hooked": acceptance.LaneOutcome("fenced-hooked", outcome, "d")})
        ids = [f.criterion for f in findings]
        assert len(ids) == len(set(ids)), ids


# --------------------------------------------- S9: refuse to name a mechanism nobody measured

def _gate(drove, advisory, control_drove=True, trust_record_appeared=False):
    return acceptance.gate_verdict(drove=drove, advisory=advisory, control_drove=control_drove,
                                   trust_record_appeared=trust_record_appeared)


def test_a_lane_the_host_reports_blocked_is_the_first_run_gate():
    finding = _gate(drove=False, advisory="blocked")
    assert finding.verdict == acceptance.FAIL
    assert "blocked" in finding.detail.lower()


def test_an_untrusted_lane_that_delivered_anyway_is_a_pass():
    finding = _gate(drove=True, advisory="idle")
    assert finding.verdict == acceptance.PASS


def test_a_lane_that_merely_failed_to_deliver_is_not_evidence_of_a_gate():
    """The defect the suite's own first real run produced, turned into a test.

    The untrusted lane could not be driven and the drill wrote "so a first-run gate is still
    there" — while the host's advisory word for that lane was ``done``, i.e. a session that ENDED.
    "Could not be driven" and "a gate stopped it" are two different claims.
    """
    finding = _gate(drove=False, advisory="done")
    assert finding.verdict == acceptance.NOT_RUN
    assert "did not measure" in finding.detail or "not measure" in finding.detail
    assert "done" in finding.detail


def test_the_gate_drill_needs_its_control_to_have_delivered():
    finding = _gate(drove=False, advisory="blocked", control_drove=False)
    assert finding.verdict == acceptance.NOT_RUN
    assert "control" in finding.detail.lower()


def test_the_gate_drill_reports_whether_the_agent_wrote_its_own_trust_record():
    """A fact worth carrying: the agent writes that file itself, so it says whether it met the
    decision. It is REPORTED, never turned into the verdict."""
    seen = _gate(drove=False, advisory="done", trust_record_appeared=True)
    unseen = _gate(drove=False, advisory="done", trust_record_appeared=False)
    assert "DID" in seen.detail and "did not" in unseen.detail
    assert seen.verdict == unseen.verdict == acceptance.NOT_RUN


# ------------------------------------------------------ the lab leaves no state in a shared config

def test_teardown_removes_project_records_the_agent_wrote_as_well_as_the_ones_it_did(tmp_path):
    """The other defect the first real run produced.

    The untrusted lane was never pre-trusted by the suite, so a cleanup keyed on "what we wrote"
    left the agent's own entry for that folder in the operator's config — pointing at a directory
    that no longer exists.
    """
    config = tmp_path / "cfg"
    config.mkdir()
    lab_root = tmp_path / "lab"
    (lab_root / "lanes" / "untrusted").mkdir(parents=True)
    ours = str(lab_root / "lanes" / "trusted")
    (config / ".claude.json").write_text(json.dumps({"projects": {
        ours: {"hasTrustDialogAccepted": True},
        str(lab_root / "lanes" / "untrusted"): {"hasTrustDialogAccepted": True},
        "/Users/somebody/real-project": {"hasTrustDialogAccepted": True},
    }}))
    lab = acceptance.Lab(str(lab_root), None, str(config))
    removed = lab.untrust_all()

    left = json.loads((config / ".claude.json").read_text())["projects"]
    assert list(left) == ["/Users/somebody/real-project"], (
        "the lab left records behind, or took one that was not its own: %s" % left)
    assert len(removed) == 2


def test_it_reads_back_whether_a_folder_it_never_pre_trusted_acquired_a_record(tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    lane = tmp_path / "lab" / "lanes" / "untrusted"
    lane.mkdir(parents=True)
    (config / ".claude.json").write_text(json.dumps({"projects": {
        str(lane.resolve()): {"hasTrustDialogAccepted": True}}}))
    lab = acceptance.Lab(str(tmp_path / "lab"), None, str(config))
    assert lab.trusted_by_someone_else(str(lane)) is True
    assert lab.trusted_by_someone_else(str(tmp_path / "lab" / "lanes" / "other")) is False


def test_the_matrix_s_declared_expectations_are_the_ones_the_verdict_uses():
    """`Lane.expect_capture` must be live, not decoration a `--list` prints while the arithmetic
    hardcodes something else."""
    expected = {lane.name: lane.expect_capture for lane in acceptance.LANES}
    assert acceptance.capture_verdict(_lanes(**{
        n: expected[n] for n in ("fenced-hooked", "unfenced-hooked", "fenced-unhooked")
    })).verdict == acceptance.PASS
    # Flip any one of them and the drill must stop passing.
    for flip in ("fenced-hooked", "unfenced-hooked", "fenced-unhooked"):
        lanes = {n: expected[n] for n in ("fenced-hooked", "unfenced-hooked", "fenced-unhooked")}
        lanes[flip] = not lanes[flip]
        assert acceptance.capture_verdict(_lanes(**lanes)).verdict != acceptance.PASS, flip


def test_a_lane_whose_argv_could_not_be_read_is_unmeasured_not_uncaptured():
    """A failed `pgrep` must not be promoted into a capture-drill FAIL about the host."""
    finding = acceptance.capture_verdict({
        "fenced-hooked": acceptance.LaneOutcome("fenced-hooked", None, "unreadable"),
        "unfenced-hooked": _outcome("unfenced-hooked", True),
        "fenced-unhooked": _outcome("fenced-unhooked", False)})
    assert finding.verdict == acceptance.NOT_RUN


def test_every_paired_drill_declares_a_control_that_the_matrix_actually_runs():
    """The discipline is structural: a drill cannot declare a control lane nobody stands up."""
    lanes = {lane.name for lane in acceptance.LANES}
    assert lanes, "the lane matrix is empty"
    for drill, controls in acceptance.CONTROLS.items():
        assert drill in lanes, "%s is a control-bearing drill with no lane" % drill
        assert controls, "%s declares no control" % drill
        for control in controls:
            assert control in lanes, "%s names a control lane nobody runs: %s" % (drill, control)


# --------------------------------------------------------- it cannot be pointed at the live fleet

def test_it_refuses_the_machine_s_own_control_socket():
    env = {"HOME": "/Users/somebody"}
    live = session_host.control_socket_path(env)
    assert acceptance.socket_refusal(live, env=env), "the operator's own socket was not refused"


def test_it_refuses_the_fleet_s_socket():
    env = {"HOME": "/Users/somebody"}
    import fleet
    fleet_socket = fleet.socket_path(session_host.config_dir(env))
    assert acceptance.socket_refusal(fleet_socket, env=env), "the fleet's socket was not refused"


def test_it_refuses_any_socket_that_already_exists(tmp_path):
    """Belt for the two named sockets above: anything already bound belongs to somebody else."""
    existing = tmp_path / "someone-elses.sock"
    existing.write_text("")
    assert acceptance.socket_refusal(str(existing), env={"HOME": str(tmp_path)})


def test_a_lab_socket_under_a_directory_the_suite_just_made_is_allowed():
    """A SHORT root, because that is what the suite makes — see the next test for why."""
    lab = tempfile.mkdtemp(prefix="sl-acc-test-", dir="/tmp")
    try:
        assert acceptance.socket_refusal(acceptance.lane_socket(lab, "fenced"),
                                         env={"HOME": "/Users/somebody"}) is None
    finally:
        shutil.rmtree(lab, ignore_errors=True)


def test_a_lab_path_too_long_to_bind_is_refused_rather_than_attempted(tmp_path):
    """`sun_path` is 104 bytes and pytest's own tmp dir is already past it.

    This is why the lab is made under a SHORT base by default: a socket nobody can bind produces a
    host that never comes up, which reads as a broken build rather than as a path that was too
    long. The suite refuses before it starts anything, using the fleet's own measurement.
    """
    deep = tmp_path / ("d" * 40) / ("e" * 40)
    refusal = acceptance.socket_refusal(acceptance.lane_socket(str(deep), "fenced"),
                                        env={"HOME": str(tmp_path)})
    assert refusal and "104" in refusal


def test_the_lab_base_default_is_short_enough_to_bind_a_socket():
    default = acceptance.parser().parse_args([]).lab_base
    probe = os.path.join(default, "sl-acc-XXXXXXXX")
    assert acceptance.socket_refusal(acceptance.lane_socket(probe, "unfenced"),
                                     env={"HOME": "/Users/somebody"}) is None


def test_the_command_line_offers_no_way_to_name_a_socket_or_an_existing_host():
    """By construction, not by warning: there is no flag that could aim this at a live server."""
    options = [opt for action in acceptance.parser()._actions for opt in action.option_strings]
    banned = ("socket", "session", "attach", "connect", "server")
    for opt in options:
        assert not any(word in opt for word in banned), (
            "%s could aim the suite at a host it did not start" % opt)


# ------------------------------------------------------- it never signals what it did not start

def test_it_refuses_to_signal_a_pid_it_did_not_start():
    signalled = []
    started = acceptance.Started(kill=lambda pid, sig: signalled.append((pid, sig)))
    with pytest.raises(acceptance.SuiteError):
        started.stop(4242)
    assert signalled == [], "a pid the suite never started was signalled"


def test_it_stops_what_it_started_and_only_that():
    signalled = []
    alive = {77}

    def kill(pid, sig):
        if pid not in alive:
            raise ProcessLookupError(pid)
        signalled.append((pid, sig))
        alive.discard(pid)

    # `alive` is injected, not left to the real OS: an un-injected liveness check would run
    # `os.kill(77, 0)` against whatever process really holds pid 77 on the machine, which is the
    # "no test reaches a real external thing" ratchet one signal number away from being a problem.
    started = acceptance.Started(kill=kill, sleep=lambda _s: None, alive=lambda pid: pid in alive)
    started.record(77, "the fenced lab server")
    started.stop_all()
    assert [pid for pid, _sig in signalled] == [77]
    assert started.outstanding() == []


def test_the_capture_drill_kills_the_host_outright_rather_than_asking_it_to_stop():
    """`kill -9`, with no SIGTERM first — the drill asks what a CRASHED host does.

    A host asked politely runs its own shutdown path, which is a different experiment wearing the
    same label. #311 measured `kill -9` and the DoD names it.
    """
    signalled = []
    started = acceptance.Started(kill=lambda pid, sig: signalled.append(sig),
                                 sleep=lambda _s: None, alive=lambda _pid: False)
    started.record(31, "a lab host")
    started.kill_now(31)
    assert signalled == [signal.SIGKILL], "the drill sent %r, not a bare SIGKILL" % (signalled,)
    assert started.outstanding() == []


def test_the_drill_kill_refuses_a_pid_this_run_did_not_start_too():
    signalled = []
    started = acceptance.Started(kill=lambda pid, sig: signalled.append(sig))
    with pytest.raises(acceptance.SuiteError):
        started.kill_now(4242)
    assert signalled == []


def test_an_interrupted_run_leaves_no_server_behind():
    """The teardown is the SAME code path an interrupt takes — not a second, untested one."""
    signalled = []
    started = acceptance.Started(kill=lambda pid, sig: signalled.append((pid, sig)),
                                 sleep=lambda _s: None, alive=lambda _pid: False)
    started.record(91, "the unfenced lab server")
    started.interrupted()
    assert [pid for pid, _sig in signalled] == [91]
    assert started.outstanding() == []


def test_stop_all_is_idempotent_so_a_teardown_after_an_interrupt_signals_nothing_twice():
    signalled = []
    started = acceptance.Started(kill=lambda pid, sig: signalled.append((pid, sig)),
                                 sleep=lambda _s: None, alive=lambda _pid: False)
    started.record(55, "a lab server")
    started.stop_all()
    started.stop_all()
    assert [pid for pid, _sig in signalled] == [55]


# ------------------------------------------------- the lab must not be poorer than a real fleet

def test_the_throwaway_host_carries_the_ambient_identity_a_real_fleet_server_has():
    """MEASURED while building this (2026-08-07): a pane whose environment has no ``USER`` runs an
    agent that reports **logged out** — `claude auth status` answers ``loggedIn: false`` with the
    identical config dir that answers ``true`` one variable later.

    A pane inherits the server's environment, so a lab server built from a hand-written variable
    list hands every drill session an environment poorer than the one a real fleet worker gets
    (launchd gives its jobs ``USER``). The suite would then measure its own poverty and report it
    as the host's — the exact false red an acceptance must never produce.
    """
    base = {"PATH": "/bin", "HOME": "/Users/somebody", "USER": "somebody", "LOGNAME": "somebody",
            "TERM": "xterm-256color", "SHELL": "/bin/zsh", "TMPDIR": "/tmp/x",
            "ANTHROPIC_API_KEY": "must-not-be-forwarded"}
    env = acceptance.Lab("/tmp/lab", None, "/cfg").host_env("fenced", fenced=True, base=base)
    assert env.get("USER") == "somebody"
    assert env.get("HOME") == "/Users/somebody", "HOME must be the operator's — the keychain needs it"
    assert "ANTHROPIC_API_KEY" not in env, "a credential redirect must not reach the lab's panes"
    assert env.get(session_host.API_TOKEN_ENV_VAR), "the fenced host needs a token of its own"


def test_the_unfenced_control_host_differs_in_exactly_one_variable():
    base = {"PATH": "/bin", "HOME": "/h", "USER": "u", "TERM": "xterm-256color"}
    lab = acceptance.Lab("/tmp/lab", None, "/cfg")
    fenced = lab.host_env("fenced", fenced=True, base=base)
    unfenced = lab.host_env("unfenced", fenced=False, base=base)
    differ = {k for k in set(fenced) | set(unfenced) if fenced.get(k) != unfenced.get(k)}
    # The XDG homes differ because two hosts need two sockets; the fence token is the variable the
    # drill is actually about. Nothing else may differ, or the pair proves nothing.
    assert differ == {session_host.API_TOKEN_ENV_VAR, "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                      "XDG_DATA_HOME"}, differ
    assert session_host.API_TOKEN_ENV_VAR not in unfenced


# ------------------------------------------------------------------------- the run says its cost

def test_the_suite_states_what_it_costs_before_it_spends_it():
    """Boundary: it must cost real sessions and should say so."""
    text = acceptance.cost_notice(acceptance.LANES)
    assert str(len(acceptance.LANES)) in text
    assert "session" in text.lower()


def test_the_summary_names_the_run_s_own_deviations():
    report = _report(("S1",))
    report.note("the host binary was supplied rather than built from the patch")
    report.record(acceptance.Finding("S1", acceptance.PASS, "an artefact", "fine"))
    assert "supplied rather than built" in report.summary()
