"""`doctor --stack` and the runner's process home (issue #306).

The doctor's job here is the OUTSIDE view of the home: the anchor block already answers "does this
live runner's pane still resolve?" for the pane home, and this adds its sibling for the login-item
home — is the job installed, is it bootstrapped in the gui domain, is the process launchd is
supervising the same one the pidfile names, and does the job's own PATH carry what the runner
shells. Every one of those is a way the runner can be running and wrong while looking alive.

Uses the FakeProbe shape from tests/test_stack_doctor.py: nothing here resolves a real launchctl.
"""
import json

import pytest

import runner_home
import stack_doctor

from test_stack_doctor import FakeProbe

@pytest.fixture(autouse=True)
def _state_home_under_the_fake_home(monkeypatch):
    # config.state_home resolves against $SL_HOME, while the FakeProbe reads files under its own
    # `home`. Point them at the same place or every state-file read here silently misses.
    monkeypatch.setenv("SL_HOME", "/home/will/.superlooper")


LABEL = "com.superlooper.runner.o__r"
PLIST = "/home/will/Library/LaunchAgents/%s.plist" % LABEL
STATE = "/home/will/.superlooper/o__r/state"
_LAUNCHCTL = "/stub/launchctl"


def _plist(path="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"):
    return runner_home.render_plist(label=LABEL, superlooper_bin="/eng/bin/superlooper",
                                    repo_path="/repo", state_home="/home/will/.superlooper/o__r",
                                    path=path)


def _printed(pid=None):
    body = "%s = {\n\tstate = running\n" % LABEL
    if pid is not None:
        body += "\tpid = %d\n" % pid
    return body + "}"


# The binaries the job's recorded PATH must actually resolve. The doctor stats them rather than
# trusting the directory string: a PATH entry that exists but no longer holds `gh` is the same
# failure as one that was never there.
_BINS = ("/opt/homebrew/bin/gh", "/opt/homebrew/bin/git")


def _probe(*, lock=None, plist=None, printed=None, print_rc=0, alive=(), home_rec=None,
           bins=_BINS):
    files = {b: "" for b in bins}
    if lock is not None:
        files[STATE + "/runner.lock"] = str(lock)
    if plist is not None:
        files[PLIST] = plist
    if home_rec is not None:
        files[STATE + "/runner.home.json"] = json.dumps(home_rec)
    commands = {"launchctl": {"path": _LAUNCHCTL,
                              ("print", "gui/501/" + LABEL): (print_rc, printed or "", "")}}
    p = FakeProbe(commands=commands, files=files, env={"SL_LAUNCHCTL": _LAUNCHCTL, "SL_UID": "501"},
                  alive_pids=alive)
    return p


def _cfg(**over):
    cfg = {"repo": "o/r", "runner_home": "login-item"}
    cfg.update(over)
    return cfg


# --------------------------------------------------------------- the block exists and is wired

def test_the_runner_home_block_is_part_of_the_stack_doctor():
    names = [r.name for r in stack_doctor.check_stack({}, probe=FakeProbe(),
                                                      sender=stack_doctor.SKIP_SEND)]
    assert "runner home" in names


# --------------------------------------------------------------- the pane home

def test_the_pane_home_is_a_clean_skip():
    # The anchor block already judges that home. Two blocks answering the same question would
    # disagree the day one of them is changed.
    r = stack_doctor.check_runner_home(_probe(), {"repo": "o/r", "runner_home": "pane"})
    assert r.ok and "pane" in r.detail


def test_the_anchor_block_does_not_judge_a_login_item_runner():
    # It would fire "a runner is live but recorded no matching anchor" on every healthy login-item
    # runner — a permanent WARN teaching the operator to ignore the block.
    p = _probe(lock=4242, alive=[4242])
    r = stack_doctor.check_runner_anchor(p, _cfg())
    assert r.ok and not r.warn
    assert "login-item" in r.detail


# --------------------------------------------------------------- the login-item home

def test_an_uninstalled_job_fails_and_names_the_installer():
    r = stack_doctor.check_runner_home(_probe(), _cfg())
    assert not r.ok
    assert LABEL in r.detail
    assert "runner-home" in r.fix and "--install" in r.fix


def test_a_job_that_is_not_bootstrapped_fails_and_names_the_domain():
    r = stack_doctor.check_runner_home(_probe(plist=_plist(), print_rc=1), _cfg())
    assert not r.ok
    assert "gui/501" in (r.detail + r.fix)


def test_a_healthy_installed_running_job_whose_pid_matches_the_runner_passes():
    r = stack_doctor.check_runner_home(
        _probe(plist=_plist(), printed=_printed(4242), lock=4242, alive=[4242]), _cfg())
    assert r.ok and not r.warn, r.detail
    assert "4242" in r.detail


def test_a_job_supervising_a_different_process_than_the_pidfile_names_is_a_failure():
    # The dangerous shape: launchd believes it is supervising the runner while the loop's own
    # pidfile names something else. One of the two is about to be restarted out from under the
    # other, and nothing else in the stack would notice.
    r = stack_doctor.check_runner_home(
        _probe(plist=_plist(), printed=_printed(4242), lock=9999, alive=[4242, 9999]), _cfg())
    assert not r.ok
    assert "4242" in r.detail and "9999" in r.detail


def test_a_loaded_job_with_no_live_runner_is_reported_but_not_failed():
    # Between a restart and the next boot this is simply true, and a doctor that failed on it would
    # cry wolf every time the owner taps Restart.
    r = stack_doctor.check_runner_home(_probe(plist=_plist(), printed=_printed()), _cfg())
    assert r.ok and r.warn
    assert "not running" in r.detail


def test_a_job_whose_path_lost_gh_fails_even_while_it_runs():
    # The spike-proven gotcha, caught statically: with launchd's own four entries the job comes up,
    # looks perfectly alive, and fails every GitHub read.
    r = stack_doctor.check_runner_home(
        _probe(plist=_plist(path=runner_home.LAUNCHD_PATH), printed=_printed(4242),
               lock=4242, alive=[4242]), _cfg())
    assert not r.ok
    assert "PATH" in r.detail and "gh" in r.detail


def test_an_unreadable_plist_is_a_failure_not_a_pass():
    r = stack_doctor.check_runner_home(
        _probe(plist="<not a plist>", printed=_printed(4242), lock=4242, alive=[4242]), _cfg())
    assert not r.ok


def test_a_repo_with_no_config_is_skipped_rather_than_judged():
    r = stack_doctor.check_runner_home(_probe(), None)
    assert r.ok
