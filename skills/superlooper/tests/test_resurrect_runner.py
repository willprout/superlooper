"""Delivery/verification tests for bin/resurrect-runner.sh (issue #208 — runner resurrection).

The watchdog calls this to RESTART a provably-gone runner in a fresh cmux tab in its recorded anchor
pane, via the SAME keystroke-free launch shim workers use, running `superlooper run`. It must:
  * VERIFY the runner actually came up (state/runner.lock names a LIVE pid) before returning 0 —
    never fabricate a restart that did not happen;
  * FAIL LOUDLY (exit 2) and close the orphan tab when nothing runs the dropped command (the RC6
    overnight-drop bug), or the runner never acquires its singleton;
  * touch NO issues.json / launch counters / worktrees — the reborn runner reconciles from
    GitHub + disk exactly like a manual restart.

Modeled on test_launch_delivery.py. The stub cmux has modes via $STUB_MODE:
  deliver -> `new-surface` spawns the tab's shell, which sources the REAL launch shim and self-runs
             the dropped command (a fake `superlooper` that writes a LIVE pid into runner.lock, so
             the pidfile-based verify passes) — a true integration test of the keystroke-free path.
  drop    -> nothing runs the dropped command -> runner.lock keeps the OLD dead pid -> verify times
             out and the launcher must fail loudly and close the tab.
new-surface ALWAYS succeeds (the tab existed; only the keystrokes were lost).
"""
import os
import re
import shutil
import stat
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
RESURRECT = os.path.join(REPO_ROOT, "skill", "bin", "resurrect-runner.sh")
SHIM_PATH = os.path.join(REPO_ROOT, "skill", "shell", "launch-shim.zsh")

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh required for the launch shim")

UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")

STUB_CMUX = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    while [ "${1:-}" = "--id-format" ]; do shift 2 || shift; done
    sub="${1:-}"; shift || true
    SURF="11111111-1111-1111-1111-111111111111"
    case "$sub" in
      new-surface)
        echo "OK $SURF 22222222-2222-2222-2222-222222222222 33333333-3333-3333-3333-333333333333"
        case "${STUB_MODE:-drop}" in
          deliver)
            # Mimic cmux spawning the new tab's shell, which sources the launch shim and self-runs
            # the dropped command. Run the REAL shim so this is a true integration test.
            ( CMUX_SURFACE_ID="$SURF" zsh -c "source '$SHIM_PATH'" ) >/dev/null 2>&1 &
            ;;
          late)
            # A runner that acquires the lock 1.5s in — INSIDE the gap between the poll's last read
            # (t=1s) and the window closing (t=2s), with SL_LAUNCH_VERIFY_SECONDS=2. Anchored to the
            # new-surface call (milliseconds before the poll starts) rather than to a shim/zsh boot,
            # whose startup jitter under load is a large fraction of the 1s gap being targeted — the
            # timing must be pinned to the same clock as the loop it is probing.
            ( sleep 1.5; bash -c 'echo $$ > "$SL_RUN_ROOT/state/runner.lock"; sleep 5' ) \\
              >/dev/null 2>&1 &
            ;;
          drop) : ;;   # nothing runs the dropped command -> runner never comes up -> verify times out
        esac
        ;;
      close-surface) printf 'closed %s\\n' "$*" >> "$STUB_DIR/closed" ;;
      rename-tab) : ;;
      *) : ;;
    esac
    exit 0
""")

# A FAKE `superlooper`: on `run`, write a LIVE pid (a real, alive process) into state/runner.lock,
# exactly as acquire_singleton would, so resurrect-runner.sh's pidfile verify passes. Stay alive a
# few seconds so kill -0 succeeds while the launcher polls, then exit.
FAKE_SUPERLOOPER = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    if [ "${1:-}" = "run" ]; then
      mkdir -p "$SL_RUN_ROOT/state"
      echo "$$" > "$SL_RUN_ROOT/state/runner.lock"
      # record that a runner actually started, for the test to assert the real path ran
      echo "cwd=$(pwd) args=$*" > "$SL_RUN_ROOT/state/fake_runner_started"
      sleep 4
    fi
    exit 0
""")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.launch_dir = tmp_path / "launch"
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        self.cmux = self.stub_dir / "cmux"
        self.superlooper = self.stub_dir / "superlooper"
        _x(str(self.cmux), STUB_CMUX)
        _x(str(self.superlooper), FAKE_SUPERLOOPER)
        # The dead runner left its pidfile behind, naming a DEAD pid (999999 — test convention).
        (self.state / "runner.lock").write_text("999999")

    def run(self, mode, rid="r1", verify_seconds="8", superlooper_bin=None):
        env = {**os.environ,
               "SL_RUN_ROOT": str(self.home),
               "SL_PANE": "pane-uuid-abc",
               "SL_CMUX": str(self.cmux),
               "SL_SUPERLOOPER_BIN": superlooper_bin or str(self.superlooper),
               "SL_LAUNCH_DIR": str(self.launch_dir),
               "SL_LAUNCH_VERIFY_SECONDS": verify_seconds,
               "STUB_MODE": mode,
               "STUB_DIR": str(self.stub_dir),
               "SHIM_PATH": SHIM_PATH}
        return subprocess.run([RESURRECT, "--cwd", str(self.repo), rid],
                              env=env, capture_output=True, text=True, timeout=60)


def test_delivered_restart_verifies_via_the_pidfile_and_succeeds(tmp_path):
    rig = _Rig(tmp_path)
    r = rig.run("deliver")
    assert r.returncode == 0, r.stderr
    # the fake runner actually ran in the repo dir
    started = (rig.state / "fake_runner_started")
    assert started.exists()
    assert f"--repo {rig.repo}" in started.read_text()
    # runner.lock now names a LIVE pid (not the old dead 999999)
    assert (rig.state / "runner.lock").read_text().strip() != "999999"
    assert "verified live" in r.stdout


def test_no_delivery_fails_loudly_and_closes_the_orphan_tab(tmp_path):
    rig = _Rig(tmp_path)
    r = rig.run("drop")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "NOT VERIFIED" in r.stderr
    # the orphan tab was closed (never leave a dead tab behind)
    assert (rig.stub_dir / "closed").exists()
    # the dropped command was removed so no straggler shell starts a rogue runner later
    assert not list(rig.launch_dir.glob("*.cmd"))


def test_resurrection_touches_no_issues_json_or_counters(tmp_path):
    # The reborn runner reconciles from GitHub + disk like a manual restart: the launcher must not
    # reset or bump any loop state. It writes ONLY the transient shim command file (cleaned up) and
    # the runner's own pidfile (written by the fake runner, not the launcher).
    rig = _Rig(tmp_path)
    r = rig.run("deliver")
    assert r.returncode == 0, r.stderr
    assert not (rig.state / "issues.json").exists()        # never created/touched
    assert not list(rig.launch_dir.glob("*.cmd"))          # shim command cleaned up on success


def test_rejects_a_non_runner_id(tmp_path):
    rig = _Rig(tmp_path)
    r = rig.run("deliver", rid="i7")                       # an issue id must never route here
    assert r.returncode == 64
    assert "runner id" in r.stderr


@pytest.mark.parametrize("rid", ["r1; touch /tmp/sl-pwned", "r1abc", "r1 r2", "r", "r1/../x"])
def test_the_runner_id_contract_is_anchored_not_a_prefix_glob(tmp_path, rid):
    # Fresh-review P2-3: the id guard was `case $ID_IN in r[0-9]*)`, an UNANCHORED glob that accepts
    # anything trailing the first digit ("r1; touch ..." ACCEPTED). Not exploitable today ($ID only
    # reaches quoted argv, never eval/a filename), but the script's own comment claims the symmetric
    # contract to launch-session.sh's anchored ^[ad][0-9]+$ — so make the code match the claim
    # rather than rest on a downstream accident. Refuse at the door: r<N> and nothing else.
    rig = _Rig(tmp_path)
    r = rig.run("deliver", rid=rid)
    assert r.returncode == 64, (rid, r.stdout, r.stderr)
    assert "runner id" in r.stderr
    assert not os.path.exists("/tmp/sl-pwned")


def test_a_runner_that_lands_in_the_last_second_is_verified_not_killed(tmp_path):
    # Fresh-review P2-5: the poll's last READ is a full second before the window closes, so a runner
    # that acquired the lock inside that gap was declared NOT VERIFIED — and the cleanup then
    # close-surface'd the tab of a runner that was actually UP (SIGHUP to a live loop), journaled a
    # failure, and paged the owner falsely. Re-check once after the loop, before concluding.
    rig = _Rig(tmp_path)
    r = rig.run("late", verify_seconds="2")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "verified live" in r.stdout
    assert (rig.state / "runner.lock").read_text().strip() != "999999"   # the NEW runner's pid
    assert not (rig.stub_dir / "closed").exists(), "must never close a LIVE runner's tab"


# A noop `superlooper`: does NOT write runner.lock — so the pidfile keeps whatever pid was there.
FAKE_SUPERLOOPER_NOOP = "#!/usr/bin/env bash\nsleep 2\nexit 0\n"


def test_verify_requires_the_lock_pid_to_change_from_the_dead_one(tmp_path):
    # Guards the PID-reuse false-positive: if the crashed runner's pid gets recycled to an unrelated
    # LIVE process, kill -0 on the (unchanged) lock pid would wrongly read as "runner up". Verify
    # must require the lock to be REWRITTEN to a DIFFERENT live pid. Here the "old" pid is our own
    # (alive) and the noop runner never rewrites the lock, so verify must NOT pass.
    rig = _Rig(tmp_path)
    (rig.state / "runner.lock").write_text(str(os.getpid()))   # a LIVE pid, unchanged by the runner
    noop = rig.stub_dir / "noop-superlooper"
    _x(str(noop), FAKE_SUPERLOOPER_NOOP)
    r = rig.run("deliver", verify_seconds="4", superlooper_bin=str(noop))
    assert r.returncode == 2, (r.stdout, r.stderr)         # never false-verifies on the unchanged pid
    assert "NOT VERIFIED" in r.stderr
    assert (rig.stub_dir / "closed").exists()              # orphan tab closed
