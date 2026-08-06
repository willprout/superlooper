"""`superlooper stop` — the deliberate off switch, and the guardians that must respect it (#239).

The gesture the login-item home never had. A runner in a visible tab has Ctrl+C; a runner that is a
`gui/$UID` LaunchAgent has KeepAlive, which restarts even a CLEAN exit, and a watchdog whose
resurrection path restarts anything that slips past launchd. "Off for the night" was an undocumented
`launchctl bootout` incantation, and an owner who typed it still got a 3am text from the watchdog
saying the loop was down.

So the DoD here is not "the process died" — it is "the process died AND STAYS dead":

  * a marker records that this was deliberate (who, when) — the fact both guardians read;
  * launchd cannot restart what is not loaded, so the job is BOOTED OUT, not merely exited;
  * the watchdog reads the marker and declines to resurrect (the CLI-level proof lives in
    tests/test_runner_home_watchdog.py, the pure decision in tests/test_watchdog.py);
  * a deliberate start CLEARS the marker, so the off switch can never latch permanently — and
  * `status` says "stopped by owner" in words no crash can produce.

Driven as a real subprocess, like tests/test_cli.py: argparse, exit codes and the operator-facing
text ARE the contract. `launchctl` is a stub this file writes — a faithful mini-launchd that
actually loads, unloads and signals — so nothing here can reach a real job. The "runner" is a real
process this file owns the pid of, and kills by that pid alone.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

LABEL = "com.superlooper.runner.o__r"

# A mini-launchd. It records every argv (so a test can assert WHICH domain and WHICH job were
# addressed) and it keeps real state: `$LAUNCHD_JOBS/<label>` exists exactly while the job is
# loaded and holds the pid it is supervising. `bootout` unloads AND signals that pid — which is what
# makes "the job is gone" and "the process is gone" two separately observable facts here, exactly as
# they are on a real machine.
_FAKE_LAUNCHCTL = r"""#!/bin/sh
echo "$@" >> "$LAUNCHCTL_LOG"
case "$1" in
  managername) echo Aqua ;;
  bootstrap)
      lbl=$(basename "$3" .plist)
      echo "${SL_TEST_JOB_PID:-4242}" > "$LAUNCHD_JOBS/$lbl"
      ;;
  bootout)
      lbl="${2##*/}"
      [ -f "$LAUNCHD_JOBS/$lbl" ] || exit 3
      pid=$(cat "$LAUNCHD_JOBS/$lbl")
      rm -f "$LAUNCHD_JOBS/$lbl"
      # It signals ONLY a pid this test file spawned and recorded as killable. A stub that killed
      # whatever number happened to be in the jobs file would, the first time someone booted out a
      # job it had bootstrapped itself, SIGTERM a placeholder pid belonging to the developer's own
      # machine — the ratchet that says no test may reach a real process, arrived at sideways.
      ok=$(cat "$LAUNCHD_JOBS/.killable" 2>/dev/null)
      if [ -n "$ok" ] && [ "$pid" = "$ok" ]; then kill "$pid" 2>/dev/null; fi
      ;;
  kickstart)
      t="$2"; [ "$t" = "-k" ] && t="$3"
      lbl="${t##*/}"
      echo "${SL_TEST_JOB_PID:-4242}" > "$LAUNCHD_JOBS/$lbl"
      ;;
  print)
      lbl="${2##*/}"
      [ -f "$LAUNCHD_JOBS/$lbl" ] || exit 113
      printf '\tpid = %s\n\tstate = running\n' "$(cat "$LAUNCHD_JOBS/$lbl")"
      ;;
esac
exit 0
"""


def _script(path, body):
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def rig(tmp_path):
    import shutil
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    home = tmp_path / "slhome" / "o__r"
    (home / "state").mkdir(parents=True)
    userhome = tmp_path / "userhome"
    (userhome / ".superlooper").mkdir(parents=True)
    jobs = tmp_path / "loaded"
    jobs.mkdir()
    launchagents = tmp_path / "LaunchAgents"
    launchagents.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / "git", "#!/bin/sh\nexit 0\n")
    launchctl = _script(tmp_path / "launchctl", _FAKE_LAUNCHCTL)
    # The session host answers the one thing the login-item boot preflight asks through the wrapper.
    host = _script(tmp_path / "sessionhost",
                   '#!/bin/sh\necho \'{"id":"p","error":{"code":"agent_not_found","message":"no"}}\'\n'
                   'exit 1\n')
    env = {**os.environ,
           "HOME": str(userhome), "SL_HOME": str(tmp_path / "slhome"),
           "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
           "SL_LAUNCHCTL": str(launchctl), "SL_LAUNCHD_DIR": str(launchagents),
           "SL_HERDR": str(host),
           "SL_CMUX": "/nonexistent/superlooper-test-cmux",
           "LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
           "LAUNCHD_JOBS": str(jobs),
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    env.pop("GH_FAIL", None)
    env.pop("SL_PANE", None)
    kids = []

    def spawn():
        """A real process standing in for the runner, recorded in state/runner.lock exactly as the
        runner's own singleton records it. Returns its pid.

        Spawned as a GRANDCHILD, so it is re-parented to launchd rather than to pytest. That is not
        fussiness: a killed child of pytest stays in the process table as a ZOMBIE until its parent
        reaps it, and `os.kill(pid, 0)` — the liveness read the stop verb and this file both use —
        reports a zombie as alive. Under launchd (which reaps its own jobs, and is the runner's
        actual parent in the home this verb exists for) a killed process really is gone."""
        # The grandchild's pid comes back through a FILE, and it inherits none of our streams: a
        # pipe it held open would keep `subprocess.run` below waiting for the very process we are
        # deliberately leaving alive.
        where = tmp_path / ("runner-pid-%d" % len(kids))
        subprocess.run(
            [sys.executable, "-c",
             "import subprocess, sys;"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],"
             " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
             "open(sys.argv[1], 'w').write(str(p.pid))", str(where)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30)
        pid = int(where.read_text().strip())
        kids.append(pid)
        (home / "state" / "runner.lock").write_text(str(pid))
        # A live runner writes BOTH the pidfile and its own home record, and `stop` refuses to
        # signal a pid the second one does not claim. Writing only the lock here would test a
        # crashed runner's leftovers, not a running one.
        (home / "state" / "runner.home.json").write_text(
            json.dumps({"kind": "login-item", "pid": pid, "label": LABEL}))
        return pid

    r = type("Rig", (), {"tmp": tmp_path, "repo": repo, "home": home, "env": env,
                         "jobs": jobs, "launchagents": launchagents,
                         "launchctl_log": tmp_path / "launchctl.log", "spawn": staticmethod(spawn)})
    write_config(r)
    yield r
    for pid in kids:                      # by ITS OWN recorded pid, never by name or pattern
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def write_config(rig, **over):
    cfg = {"version": 1, "repo": "o/r", "required_checks": ["ci"]}
    cfg.update(over)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(cfg))


def cli(rig, *args, env_over=None, timeout=90):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args, "--repo", str(rig.repo)],
                          capture_output=True, text=True, env=env, timeout=timeout)


def marker(rig):
    p = rig.home / "state" / "runner.stopped"
    return json.loads(p.read_text()) if p.exists() else None


def calls(rig):
    return rig.launchctl_log.read_text().splitlines() if rig.launchctl_log.exists() else []


def load_job(rig, pid=0):
    """Put the job in the stub launchd's loaded set, supervising `pid`. A pid passed here is one
    this file spawned, so it is also recorded as the ONE pid the stub may signal."""
    (rig.jobs / LABEL).write_text(str(pid))
    if pid:
        (rig.jobs / ".killable").write_text(str(pid))


def _dead(pid, deadline=20):
    end = time.time() + deadline
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.1)
    return False


# --------------------------------------------------- the marker: who, and when

def test_stop_records_a_deliberate_stop_with_who_and_when(rig):
    write_config(rig, runner_home="login-item")
    before = int(time.time())
    r = cli(rig, "stop", "--operator", "william", "--source", "cli")
    assert r.returncode == 0, r.stdout + r.stderr
    m = marker(rig)
    assert m is not None, "the whole point is a durable record that this was deliberate"
    assert before <= m["stopped_at"] <= int(time.time()) + 1
    assert m["operator"] == "william"
    assert m["source"] == "cli"


def test_stop_records_the_stop_even_when_no_runner_is_live(rig):
    # The marker is a DECLARATION, not a death certificate: an owner who stops an already-down loop
    # still means "stay down", and that is exactly the state the watchdog would otherwise resurrect.
    write_config(rig, runner_home="login-item")
    r = cli(rig, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker(rig) is not None


# --------------------------------------------------- launchd cannot restart what is not loaded

def test_stop_boots_the_login_item_job_out_of_the_gui_domain(rig):
    write_config(rig, runner_home="login-item")
    pid = rig.spawn()
    load_job(rig, pid)
    r = cli(rig, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bootout gui/%d/%s" % (os.getuid(), LABEL) in calls(rig), calls(rig)
    # UNLOADED, not merely exited. KeepAlive restarts a clean exit; it cannot restart a job launchd
    # no longer has — that is the whole mechanism behind "still down 60s later".
    assert not (rig.jobs / LABEL).exists()
    assert _dead(pid), "the runner process should be gone once its job is booted out"


def test_stop_never_reaches_for_another_launchd_domain(rig):
    write_config(rig, runner_home="login-item")
    load_job(rig)
    cli(rig, "stop")
    assert not any("system/" in line for line in calls(rig)), calls(rig)


def test_a_bootout_that_leaves_the_job_running_is_reported_as_a_failed_stop(rig):
    # Fabricated history is this codebase's cardinal sin: if launchd still has the job, saying "the
    # runner is stopped" tells the owner the loop is off while it is minutes from restarting.
    write_config(rig, runner_home="login-item")
    _script(rig.tmp / "launchctl", '#!/bin/sh\necho "$@" >> "$LAUNCHCTL_LOG"\n'
                                   'case "$1" in\n'
                                   '  managername) echo Aqua ;;\n'
                                   '  bootout) exit 1 ;;\n'
                                   '  print) printf "\\tpid = 4242\\n\\tstate = running\\n" ;;\n'
                                   'esac\nexit 0\n')
    r = cli(rig, "stop")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "still" in out.lower() or "fail" in out.lower(), out


# --------------------------------------------------- the pane home

def test_stop_in_the_pane_home_signals_the_recorded_runner_and_never_touches_launchd(rig):
    # There is no supervisor to hold here — the runner IS the foreground process of a tab — so the
    # stop is a signal to the pid the runner itself recorded. Never a name, never a pattern.
    pid = rig.spawn()
    r = cli(rig, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _dead(pid)
    assert marker(rig) is not None
    assert not calls(rig), "the pane home has no launchd job to address"


# --------------------------------------------------- status tells the three states apart

def test_status_names_a_deliberate_stop_in_words_a_crash_cannot_produce(rig):
    write_config(rig, runner_home="login-item")
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 3600))
    cli(rig, "stop", "--operator", "william")
    out = cli(rig, "status").stdout
    assert "STOPPED BY OWNER" in out, out
    assert "william" in out


def test_status_still_calls_a_crashed_runner_crashed(rig):
    # The regression that matters: a stop marker must not make every dead runner look deliberate.
    write_config(rig, runner_home="login-item")
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time()) - 3600))
    (rig.home / "state" / "runner.lock").write_text("999999")
    out = cli(rig, "status").stdout
    assert "STOPPED BY OWNER" not in out
    assert "CRASHED" in out or "crashed" in out, out


def test_status_calls_a_live_runner_running(rig):
    pid = rig.spawn()
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time())))
    out = cli(rig, "status").stdout
    assert "running" in out and str(pid) in out, out
    assert "STOPPED BY OWNER" not in out


def test_status_on_a_never_run_repo_is_still_calm(rig):
    r = cli(rig, "status")
    assert r.returncode == 0
    assert "never" in r.stdout.lower() or "no runner" in r.stdout.lower()


# --------------------------------------------------- the deliberate start clears it

def test_start_clears_the_stop_and_brings_the_job_back(rig):
    write_config(rig, runner_home="login-item")
    (rig.launchagents / (LABEL + ".plist")).write_text("<plist/>")
    pid = rig.spawn()
    load_job(rig, pid)
    assert cli(rig, "stop").returncode == 0
    assert marker(rig) is not None
    r = cli(rig, "start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker(rig) is None, "an off switch that cannot be switched back on is a broken loop"
    assert any(line.startswith("bootstrap gui/%d " % os.getuid()) for line in calls(rig)), calls(rig)
    assert (rig.jobs / LABEL).exists(), "stop -> start -> the runner's job is loaded again"
    assert "4242" in r.stdout, "a start reports the pid that proves it: " + r.stdout


def test_start_without_an_installed_job_refuses_and_names_the_installer(rig):
    write_config(rig, runner_home="login-item")
    r = cli(rig, "start")
    assert r.returncode != 0
    assert "runner-home" in (r.stdout + r.stderr)


def test_start_in_the_pane_home_names_the_manual_start_and_leaves_the_stop_standing(rig):
    # Automated tab placement is owner-ruled out (2026-07-09), so this verb cannot start anything
    # here — and therefore must not clear the stop. A cleared marker over a loop that is still down
    # re-arms the watchdog against it: the 3am text, from the verb meant to be the safe way back.
    cli(rig, "stop")
    r = cli(rig, "start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker(rig) is not None, "nothing was started, so the stop is still the truth"
    assert "superlooper run" in r.stdout


def test_a_runner_that_boots_clears_the_stop_marker(rig):
    # The self-healing property: the marker says "no runner is running, on purpose". A runner that
    # IS running makes that false, so ANY real start — this verb, a hand `run`, a login-time
    # bootstrap — clears it. Without this, one stop could silently disable resurrection forever.
    write_config(rig, runner_home="login-item")
    cli(rig, "stop")
    assert marker(rig) is not None
    r = cli(rig, "run", "--ticks", "0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker(rig) is None


# --------------------------------------------------- the button's view

def test_stop_emits_one_json_object_for_a_ui_over_the_loop(rig):
    write_config(rig, runner_home="login-item")
    pid = rig.spawn()
    load_job(rig, pid)
    r = cli(rig, "stop", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["verb"] == "stop" and doc["ok"] is True
    assert doc["home"] == "login-item"
    assert doc["was_running"] is True and doc["process_gone"] is True


def test_start_emits_one_json_object_for_a_ui_over_the_loop(rig):
    write_config(rig, runner_home="login-item")
    (rig.launchagents / (LABEL + ".plist")).write_text("<plist/>")
    cli(rig, "stop")
    r = cli(rig, "start", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["verb"] == "start" and doc["ok"] is True
    assert doc["cleared"] is True and doc["pid"] == 4242


# --------------------------------------------------- the journal keeps the record

def test_the_stop_and_the_start_are_journaled(rig):
    write_config(rig, runner_home="login-item")
    (rig.launchagents / (LABEL + ".plist")).write_text("<plist/>")
    cli(rig, "stop", "--operator", "william")
    cli(rig, "start")
    lines = [json.loads(l) for l in
             (rig.home / "journal.jsonl").read_text().splitlines() if l.strip()]
    acts = [(r.get("act"), r.get("outcome")) for r in lines]
    assert ("runner_stop", "stopped") in acts, acts
    assert ("runner_stop", "started") in acts, acts


# --------------------------------------------------- a stop that did NOT take
#
# The marker records a stop that was ASKED FOR. It can fail to take — a tick that outlives the wait,
# a bootout launchd refuses — and every reader that treats the request as the outcome then lies
# about a loop that is still running. These pin the honest answers (fresh-agent review).

def test_status_reports_a_stop_that_did_not_take_as_the_contradiction_it_is(rig):
    write_config(rig, runner_home="login-item")
    pid = rig.spawn()
    (rig.home / "state" / "runner.stopped").write_text(
        json.dumps({"stopped_at": int(time.time()), "operator": "william"}))
    (rig.home / "state" / "runner.heartbeat").write_text(str(int(time.time())))
    out = cli(rig, "status").stdout
    assert "STOP NOT TAKEN" in out, out
    assert str(pid) in out
    # The dangerous rendering is the reassuring one: never claim a live runner is stopped.
    assert "STOPPED BY OWNER" not in out


def test_a_failed_stop_says_the_loop_keeps_going_rather_than_reassuring(rig):
    write_config(rig, runner_home="login-item")
    _script(rig.tmp / "launchctl", '#!/bin/sh\necho "$@" >> "$LAUNCHCTL_LOG"\n'
                                   'case "$1" in\n'
                                   '  managername) echo Aqua ;;\n'
                                   '  bootout) exit 1 ;;\n'
                                   '  print) printf "\\tpid = 4242\\n\\tstate = running\\n" ;;\n'
                                   'esac\nexit 0\n')
    r = cli(rig, "stop")
    assert r.returncode != 0
    assert "will not resurrect" not in r.stdout, "a failed stop must not promise the watchdog is off"
    assert "still" in r.stdout.lower()


def test_a_bootout_the_service_confirms_took_is_a_stop_whatever_the_rc_said(rig):
    # launchd answers rc 36 ("Operation now in progress") for a bootout already in flight — running
    # `stop` twice, or a job slow to exit. The SERVICE's own word is the fact; the rc is evidence
    # about it. Believing the rc tells an owner their loop is running when launchd says it is not.
    write_config(rig, runner_home="login-item")
    _script(rig.tmp / "launchctl", '#!/bin/sh\necho "$@" >> "$LAUNCHCTL_LOG"\n'
                                   'case "$1" in\n'
                                   '  managername) echo Aqua ;;\n'
                                   '  bootout) exit 36 ;;\n'
                                   '  print) exit 113 ;;\n'
                                   'esac\nexit 0\n')
    r = cli(rig, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "runner stopped" in r.stdout


# --------------------------------------------------- never signal an unverifiable pid

def test_stop_refuses_to_signal_a_pid_the_runner_does_not_claim(rig):
    # A pidfile outlives a hard crash, and the OS reuses pids. Signalling that number then kills
    # whatever the owner happens to be running — the 2026-07-07 collateral kill from another angle.
    pid = rig.spawn()
    (rig.home / "state" / "runner.home.json").unlink()          # a crashed runner's leftovers
    r = cli(rig, "stop", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["process_gone"] is False
    assert "does not claim" in (doc.get("signal_refused") or ""), doc
    assert not _dead(pid, deadline=2), "the unverified process must be left alone"


def test_stop_signals_the_pid_the_runner_does_claim(rig):
    pid = rig.spawn()                                            # writes lock AND home record
    r = cli(rig, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _dead(pid)


# --------------------------------------------------- a start that fails changes nothing

def test_a_start_that_launchd_refuses_leaves_the_stop_standing(rig):
    # The worst state this issue can produce is a loop that is DOWN with its guardians re-armed: the
    # heartbeat goes stale, the watchdog opens an episode and texts about an outage the owner
    # created on purpose. So a failed start must clear nothing and journal no "started".
    write_config(rig, runner_home="login-item")
    (rig.launchagents / (LABEL + ".plist")).write_text("<plist/>")
    cli(rig, "stop")
    _script(rig.tmp / "launchctl", '#!/bin/sh\ncase "$1" in managername) echo Aqua ;; esac\nexit 5\n')
    r = cli(rig, "start")
    assert r.returncode != 0
    assert marker(rig) is not None, "the stop is still the truth — nothing came up"
    acts = [json.loads(l).get("outcome") for l in
            (rig.home / "journal.jsonl").read_text().splitlines() if l.strip()]
    assert "started" not in acts, acts


def test_a_start_that_comes_up_with_nothing_running_is_a_failure(rig):
    # bootstrap accepted, job immediately gone. Exiting 0 here would tell a button the loop is back.
    write_config(rig, runner_home="login-item")
    (rig.launchagents / (LABEL + ".plist")).write_text("<plist/>")
    cli(rig, "stop")
    _script(rig.tmp / "launchctl", '#!/bin/sh\ncase "$1" in\n'
                                   '  managername) echo Aqua ;;\n'
                                   '  print) exit 113 ;;\n'
                                   'esac\nexit 0\n')
    r = cli(rig, "start", "--json")
    assert r.returncode != 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and doc["pid"] is None
    assert marker(rig) is not None
