"""`doctor --stack` and the WATCHDOG's launchd job (issue #328).

The sibling of `tests/test_runner_home_doctor.py`, for the other job launchd runs — and the reason
it needed one is that its failure is silent BY CONSTRUCTION. The watchdog's heartbeat and ALERT
detectors read FILES, so a job with a broken PATH keeps reporting them and looks perfectly healthy;
but every GitHub read refuses, which `lib/watchdog.py` correctly treats as UNOBSERVABLE and so
FREEZES its clocks. The `no_progress` detector can then never fire. A whole detector goes dark and
nothing in the stack says so — which is exactly what was true on the fleet machine, whose installed
job carried launchd's own `/usr/bin:/bin:/usr/sbin:/sbin` and no `gh`.

Two things this block deliberately does NOT copy from its runner sibling, both pinned below:

* **liveness.** The watchdog is a scheduled ONE-SHOT (`StartInterval`, no `KeepAlive`), so "not
  running" is its healthy steady state — the runner block's pid reads and its loaded-but-idle WARN
  would fire on every healthy watchdog, which is how a block teaches an operator to ignore it.
* **repair.** The installed plist under `~/Library/LaunchAgents` is the owner's. This block reports;
  a doctor that repairs is not a doctor (the issue's boundary), and the launchctl-call test below
  holds that mechanically rather than by promise.

And one thing it deliberately does differently, found by a fresh-agent review: a LOADED job is
judged on the PATH **launchd is actually holding**, read out of `launchctl print`, not on the plist
sitting on disk. The two disagree for as long as the remedy takes, because the remedy has two steps
— edit the file, THEN bootout+bootstrap. Judging the file would go green the moment it was saved,
while launchd went on running the old environment and the detector stayed dark, which is precisely
the failure this block exists to end. An UNLOADED job is judged on its plist, because that is what a
future bootstrap would load and there is no live environment to read.

Uses the FakeProbe shape from tests/test_stack_doctor.py: nothing here resolves a real launchctl.
"""
import os
from pathlib import Path

import pytest

import runner_home
import stack_doctor

from test_stack_doctor import FakeProbe

LABEL = "com.superlooper.watchdog.o__r"
RUNNER_LABEL = "com.superlooper.runner.o__r"
PLIST = "/home/will/Library/LaunchAgents/%s.plist" % LABEL
_LAUNCHCTL = "/stub/launchctl"
# This process's own uid — there is no override, by design: the gui/$UID rule is the keychain rule,
# and a rule that can be passed as an argument is one that will one day be passed the wrong one.
_UID = os.getuid()

_TEMPLATE = (Path(__file__).resolve().parent.parent / "skill" / "templates"
             / "launchd.watchdog.plist")

# The binaries a machine has. Stat'ed rather than trusted as a string: a PATH entry that exists but
# no longer holds `gh` is the same failure as one never there. `git` is here so the "a PATH without
# git still passes" test below is proving a verdict, not an absent fixture.
_BINS = ("/opt/homebrew/bin/gh", "/usr/bin/git")

_GOOD_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _plist(path=_GOOD_PATH):
    """The SHIPPED template, rendered — not a hand-written stub. The whole subject of this block is
    the plist an operator actually installs, so a template that stopped carrying `{path}` must break
    these tests rather than pass them against a fixture that still does."""
    text = _TEMPLATE.read_text()
    for key, value in (("label", LABEL), ("superlooper_bin", "/eng/bin/superlooper"),
                       ("repo_path", "/repo"), ("state_home", "/home/will/.superlooper/o__r"),
                       ("interval_seconds", "300"), ("path", path)):
        text = text.replace("{" + key + "}", value)
    return text


def _printed(state="not running", live_path=_GOOD_PATH, default_path=runner_home.LAUNCHD_PATH):
    """One `launchctl print` block for a scheduled one-shot between firings — its normal state.

    Shaped from a REAL `launchctl print gui/501/com.superlooper.session-host` on the fleet machine,
    tabs and all, including the two decoys: `inherited environment` (which carries the caller's
    SSH_AUTH_SOCK and never a PATH) and `default environment` (which carries launchd's own four
    ALWAYS, even for a job whose own EnvironmentVariables override them). Reading the wrong one of
    those three is how this check would report the opposite of the truth.
    """
    env = "" if live_path is None else "\t\tPATH => %s\n" % live_path
    default = "" if default_path is None else "\t\tPATH => %s\n" % default_path
    return ("%s = {\n"
            "\tstate = %s\n"
            "\tinherited environment = {\n"
            "\t\tSSH_AUTH_SOCK => /private/tmp/com.apple.launchd.3friD2AGSE/Listeners\n"
            "\t}\n"
            "\n"
            "\tdefault environment = {\n"
            "%s"
            "\t}\n"
            "\n"
            "\tenvironment = {\n"
            "\t\tOSLogRateLimit => 64\n"
            "%s"
            "\t\tXPC_SERVICE_NAME => %s\n"
            "\t}\n"
            "\n"
            "\tdomain = gui/%d [100022]\n"
            "}" % (LABEL, state, default, env, LABEL, _UID))


def _probe(*, plist=None, printed=None, print_rc=0, bins=_BINS, extra_files=None,
           live_path=_GOOD_PATH):
    files = {b: "" for b in bins}
    if plist is not None:
        files[PLIST] = plist
    files.update(extra_files or {})
    if printed is None:
        printed = _printed(live_path=live_path)
    commands = {"launchctl": {"path": _LAUNCHCTL,
                              ("print", "gui/%d/" % _UID + LABEL): (print_rc, printed, "")}}
    return FakeProbe(commands=commands, files=files, env={"SL_LAUNCHCTL": _LAUNCHCTL})


def _cfg(**over):
    cfg = {"repo": "o/r"}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------- the label
def test_the_watchdog_job_has_its_own_label_in_the_shipped_shape():
    # com.superlooper.<job>.<owner>__<name> — the shape docs/OPERATING.md documents and the one an
    # operator's `launchctl list` groups a repo's jobs by.
    assert runner_home.watchdog_label("o/r") == LABEL
    assert runner_home.watchdog_label("willprout/superlooper") == \
        "com.superlooper.watchdog.willprout__superlooper"


def test_a_watchdog_label_refuses_a_slug_it_cannot_address():
    # Raises rather than sanitizing, for the same reason the runner label does: a quietly-repaired
    # label addresses a DIFFERENT job, or none, and every later verb inherits the mistake.
    for bad in ("o", "o/r/s", "/r", "o/", None, 7):
        with pytest.raises((ValueError, TypeError)):
            runner_home.watchdog_label(bad)


# --------------------------------------------------------------- the block exists and is wired
def test_the_watchdog_job_block_is_part_of_the_stack_doctor():
    names = [r.name for r in stack_doctor.check_stack({}, probe=FakeProbe(),
                                                      sender=stack_doctor.SKIP_SEND)]
    assert "watchdog job" in names


def test_a_repo_with_no_config_is_skipped_rather_than_judged():
    r = stack_doctor.check_watchdog_job(_probe(), None)
    assert r.ok and not r.warn


# --------------------------------------------------------------- not installed is not a fault
def test_no_watchdog_job_installed_is_a_clean_skip_not_a_failure():
    # Running the unattended-debugger watchdog is OPTIONAL
    # (plugin/skills/superlooper/references/runner-ops.md). A machine that never installed one is
    # not broken, and failing the whole stack over a job nobody asked for is how a doctor stops
    # being read.
    r = stack_doctor.check_watchdog_job(_probe(), _cfg())
    assert r.ok and not r.warn, r.detail
    assert "optional" in r.detail


# --------------------------------------------------------------- the installed job
def test_a_loaded_job_whose_path_resolves_everything_passes_clean():
    r = stack_doctor.check_watchdog_job(_probe(plist=_plist()), _cfg())
    assert r.ok and not r.warn, r.detail
    assert LABEL in r.detail


def test_a_job_whose_path_lost_gh_fails_and_names_the_remedy():
    # The realized fault this block exists for: on the fleet machine the installed job carried
    # launchd's own four entries, so `gh` was simply not found — every GitHub read refused, the
    # clocks froze, and no_progress could never fire on a job that printed as healthy.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(path=runner_home.LAUNCHD_PATH), live_path=None), _cfg())
    assert not r.ok
    assert "PATH" in r.detail and "gh" in r.detail
    # The Fix must name the CONCRETE remedy: re-render from the shipped template with an explicit
    # PATH, then re-bootstrap. "Set a PATH" is not a remedy anybody can follow at 3am.
    assert "launchd.watchdog.plist" in r.fix
    assert "bootstrap" in r.fix and "gui/%d" % _UID in r.fix


def test_the_fix_never_tells_the_operator_the_doctor_rewrote_anything():
    # Report only (the issue's boundary). The remedy is phrased as something the OPERATOR does to a
    # file that is theirs.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(path=runner_home.LAUNCHD_PATH), live_path=None), _cfg())
    assert PLIST in r.fix


def test_a_path_that_resolves_gh_but_not_git_still_passes():
    # `git` is NOT in the watchdog's set. A fresh-agent review traced every bare-name command on the
    # check's own path — `gh`, and nothing else — and failing a job over a command it never shells
    # is a false red, which is the one thing a health check may not produce.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(path="/opt/homebrew/bin:/bin"), live_path="/opt/homebrew/bin:/bin",
               bins=("/opt/homebrew/bin/gh",)), _cfg())
    assert r.ok and not r.warn, r.detail


def test_the_watchdog_command_set_is_exactly_what_the_watchdog_shells():
    assert runner_home.WATCHDOG_COMMANDS == ("gh",)


def test_a_job_installed_but_not_loaded_fails_and_names_the_domain():
    # A plist on disk that launchd holds nothing for is a watchdog that never fires at all — the
    # same darkness as the PATH gap, one layer up.
    r = stack_doctor.check_watchdog_job(_probe(plist=_plist(), print_rc=1), _cfg())
    assert not r.ok
    assert "gui/%d" % _UID in (r.detail + r.fix)
    assert "bootstrap" in r.fix


def test_a_broken_path_is_reported_even_while_the_job_is_not_loaded():
    # Both faults at once, and the PATH one wins the message: bootstrapping a job whose PATH lost
    # `gh` just re-establishes the silent failure. The single remedy fixes both, in that order.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(path=runner_home.LAUNCHD_PATH), print_rc=1), _cfg())
    assert not r.ok
    assert "PATH" in r.detail and "gh" in r.detail
    assert "not loaded" in r.detail


def test_an_unreadable_plist_on_an_unloaded_job_is_a_failure_not_a_pass():
    r = stack_doctor.check_watchdog_job(_probe(plist="<not a plist>", print_rc=1), _cfg())
    assert not r.ok


# ------------------------------------------------- live launchd truth vs the file on disk (P0)
def test_a_loaded_job_is_judged_on_the_path_launchd_holds_not_the_file_on_disk():
    # The fresh-agent review's blocking finding, reproduced: the remedy has TWO steps, and between
    # them the plist on disk is already correct while launchd is still running the old environment.
    # A check that read the file would go green there — on a watchdog whose no_progress detector is
    # still dark — which is the exact silence this block exists to end.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(), live_path=None), _cfg())          # good file, launchd's own PATH
    assert not r.ok, r.detail
    assert "PATH" in r.detail and "gh" in r.detail
    assert "launchd" in r.detail
    assert "bootstrap" in r.fix


def test_the_live_read_prefers_the_jobs_own_environment_over_launchds_default():
    # `default environment` is printed for EVERY job and always names launchd's own four, including
    # for a job whose EnvironmentVariables override them. Reading it in preference would fail every
    # correctly-installed watchdog on the machine.
    r = stack_doctor.check_watchdog_job(_probe(plist=_plist()), _cfg())
    assert r.ok and not r.warn, r.detail


def test_a_loaded_job_whose_live_path_is_good_but_whose_plist_regressed_only_warns():
    # The same two-step remedy seen from its other side: the file the NEXT bootstrap would load has
    # lost `gh` while the running job is fine. Nothing is dark right now, so it cannot be a FAIL —
    # but it is a booby trap set for the next reboot, so it cannot be silent either.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(path=runner_home.LAUNCHD_PATH)), _cfg())
    assert r.ok and r.warn, r.detail
    assert PLIST in r.detail


def test_a_loaded_job_whose_print_output_names_no_path_at_all_fails_closed():
    # Same rule as the runner block's unreadable-state branch: "we could not tell" is not "it is
    # fine". A `launchctl print` this build cannot read means nothing here can say whether the
    # detector is dark, and guessing is how a dark job reads green.
    r = stack_doctor.check_watchdog_job(
        _probe(plist=_plist(), printed=_printed(live_path=None, default_path=None)), _cfg())
    assert not r.ok
    assert "refuses to guess" in r.fix


# ------------------------------------------------- the live PATH reader itself
def test_service_path_reads_the_shape_launchctl_actually_prints():
    text = _printed(live_path="/opt/homebrew/bin:/usr/bin")
    assert runner_home.service_path(text) == "/opt/homebrew/bin:/usr/bin"


def test_service_path_falls_back_to_launchds_default_when_the_job_sets_none():
    assert runner_home.service_path(_printed(live_path=None)) == runner_home.LAUNCHD_PATH


def test_service_path_never_answers_from_the_inherited_environment():
    # `inherited environment` is the CALLER's leftovers (SSH_AUTH_SOCK and friends), not the job's.
    text = ("com.x = {\n\tinherited environment = {\n\t\tPATH => /caller/only\n\t}\n}")
    assert runner_home.service_path(text) is None


def test_service_path_is_none_when_nothing_readable_is_there():
    for text in ("", None, "com.x = {\n\tstate = not running\n}"):
        assert runner_home.service_path(text) is None


def test_a_bad_repo_slug_fails_and_names_the_config_key():
    r = stack_doctor.check_watchdog_job(_probe(), _cfg(repo="not-a-slug"))
    assert not r.ok
    assert "repo" in r.fix


# --------------------------------------------------------------- what it must NOT do
def test_a_one_shot_between_firings_is_not_judged_on_liveness():
    # The runner block WARNs on a loaded-but-idle job, correctly: a runner that is not running is
    # not running. The watchdog is a StartInterval one-shot, so "not running" is what a healthy one
    # says nearly all the time — copying that WARN across would make the block permanently yellow.
    r = stack_doctor.check_watchdog_job(_probe(plist=_plist(), printed=_printed("not running")),
                                        _cfg())
    assert r.ok and not r.warn, r.detail


def test_the_block_addresses_the_watchdog_job_never_the_runners():
    p = _probe(plist=_plist())
    stack_doctor.check_watchdog_job(p, _cfg())
    printed = [c for c in p.calls if "print" in c]
    assert printed, p.calls
    assert all(RUNNER_LABEL not in " ".join(c) for c in printed), p.calls
    assert any(LABEL in " ".join(c) for c in printed), p.calls


def test_the_block_only_ever_reads_it_never_repairs():
    # The mechanical form of the issue's boundary: the installed plist is the owner's. `print` is a
    # read; bootout / bootstrap / kickstart / unload are not, and none of them may appear.
    for kwargs in ({}, {"plist": _plist()}, {"plist": _plist(path=runner_home.LAUNCHD_PATH)},
                   {"plist": _plist(), "print_rc": 1}):
        p = _probe(**kwargs)
        stack_doctor.check_watchdog_job(p, _cfg())
        for call in p.calls:
            assert not ({"bootout", "bootstrap", "kickstart", "load", "unload"} & set(call)), call


def test_the_check_names_and_fixes_list_documents_the_block():
    # The doc lint enforces this too, from the other direction; naming it here means the person who
    # renames the block sees the doc obligation in their own test run.
    stack_md = (Path(__file__).resolve().parent.parent / "docs" / "STACK.md").read_text()
    assert "- `watchdog job`:" in stack_md
