"""Make the skill's importable modules resolvable from the tests without an install step.

The publishable payload lives under ``skill/`` (``skill/lib`` for the pure decision cores,
``skill/bin`` for the entry-point scripts). Tests import those modules by bare name
(``import sanitize``), exactly as autocode's tests do — so we prepend both dirs to
``sys.path`` here. Paths are computed relative to THIS file (the repo root is its parent),
so imports resolve no matter where pytest is invoked from.
"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# List the dirs in priority order (skill/lib holds the pure decision cores; skill/bin holds
# entry-point scripts — a lib module must win a name collision over a same-named bin script,
# matching autocode's ["lib","bin"] ordering). Insert in REVERSE so the first-listed dir ends
# up first on sys.path (each insert(0) prepends). test_conftest_paths.py pins this order.
for _sub in reversed(("skill/lib", "skill/bin")):
    _p = str(_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _clear_worker_launch_env(monkeypatch):
    # The suite often launches subprocesses by copying os.environ. A test run can itself be inside a
    # superlooper worker, which means SL_AGENT/SL_EFFORT/etc. are ambient and would silently change
    # launcher defaults under test. Tests that need these knobs set them explicitly in-body.
    for name in (
        "SL_AGENT",
        "SL_MODEL",
        "SL_EFFORT",
        "SL_CODEX_DANGEROUS_BYPASS",
        "SL_CODEX_BYPASS_HOOK_TRUST",
        "SL_CODEX_NO_ALT_SCREEN",
        # Session identity (issue #298). A worker pane running this suite carries its OWN minted
        # session id and (on a revived lane) SL_RESUME=1, both live in the environment. Inherited
        # into a launcher under test, they would silently turn a "fresh launch" case into a resume
        # of the test runner's own conversation.
        "SL_SESSION_ID",
        "SL_RESUME",
        "SL_RESUME_SESSION_ID",
        # Drift-check overrides (issue #39): a dogfooding machine may export these ambiently. If a
        # future test calls stack_doctor.engine_drift() without a FakeProbe, an ambient
        # SL_SOURCE_REPO/SL_GIT would send it to real git — against the "no test reaches a real
        # external binary" ratchet. Neutralize them so such a call resolves to no source checkout.
        "SL_SOURCE_REPO",
        "SL_GIT",
        # App Nap check overrides (issue #120): same ratchet. stack_doctor.check_cmux_app_nap runs
        # `defaults read <bundle> NSAppSleepDisabled`, and cmd_stack_doctor builds a REAL Probe. An
        # ambient SL_DEFAULTS (pointing at real /usr/bin/defaults) or SL_CMUX_BUNDLE_ID would send a
        # FakeProbe-less doctor test to the host's real user defaults — against the "no test reaches a
        # real external binary / reads real macOS defaults" rule. Neutralize so such a call can't.
        "SL_DEFAULTS",
        "SL_CMUX_BUNDLE_ID",
        # Plugin-presence override (issue #90): same ratchet, one step subtler. No real binary is at
        # risk here — but cmd_stack_doctor builds a REAL Probe reading os.environ, and the CLI rig
        # copies os.environ into the subprocess, so an ambient SL_PLUGIN_ID would send the doctor
        # looking for a DIFFERENT plugin id than the test's stub reports and false-fail the
        # green-stack assertion. STACK.md advertises this var to fork users, and a fork user
        # dogfooding this repo is exactly who would have it exported.
        "SL_PLUGIN_ID",
        # The gh-auth assert's own vars (issue #299). These matter MORE than the rest, because
        # launch-session.sh now NAMES SL_GH and SL_EXPECT_GH_LOGIN in the dropped worker command —
        # so once this ships, every worker session (including one running this suite) carries them
        # ambiently. Left inherited, SL_GH would be set for every test, which would make
        # _never_reach_real_gh below no-op and quietly re-open the path to the owner's real,
        # logged-in gh; and an inherited SL_EXPECT_GH_LOGIN would let a launcher under test SKIP the
        # "resolve the identity in the runner's env" step it is supposed to be proving. Scrubbed
        # here, BEFORE that fixture runs, so the ratchet actually fires.
        "SL_GH",
        "SL_EXPECT_GH_LOGIN",
        # The claude binary pin (issue #303). Same shape as SL_GH one line up: it steers which
        # binary start-session.sh actually launches and which one the doctor judges, so an ambient
        # value from a dogfooding shell would silently change every launcher and doctor test under
        # it. Scrubbed here so _never_reach_real_claude below can then set the fail-closed default.
        "SL_CLAUDE",
        # Same shape: the per-launch delivery token is ambient in a worker pane, and a launcher
        # test that inherited it would stamp its sentinel under a token it did not mint.
        "SL_START_TOKEN",
        # The session-host binary override (issue #304). Same shape as SL_GH/SL_CLAUDE one line up,
        # with a sharper blast radius: the wrapper's verbs SPAWN, PROMPT and CLOSE sessions, so a
        # test that reached a real host binary would not just read the owner's fleet — it could
        # drive it. Scrubbed here so _never_reach_real_session_host below can set the fail-closed
        # default.
        "SL_HERDR",
        # The runner's process home (issue #306). SL_LAUNCHCTL steers the service manager the CLI
        # and doctor talk to, SL_LAUNCHD_DIR steers where a LaunchAgent is PLACED, and SL_UID
        # steers whose gui domain a job is addressed in. All three are ambient on a machine that
        # has installed a login-item runner, and all three change what an install/verify under test
        # actually touches — an inherited SL_LAUNCHD_DIR would let a test write into the owner's
        # real ~/Library/LaunchAgents. Scrubbed here so _never_reach_real_launchctl below can then
        # set the fail-closed default.
        "SL_LAUNCHCTL",
        "SL_LAUNCHD_DIR",
        "SL_UID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _telemetry_off_by_default():
    # GitHub API-burn telemetry (issue #15) is a process-global sink the LIVE runner turns ON at its
    # entrypoint (superlooper `cmd_run` -> gh.set_telemetry(state_home)). A CLI test that goes through
    # cmd_run would otherwise leak that sink into every later test, which would then write burn rows
    # under the leaked home. Force it OFF before every test so the default is fail-closed; a telemetry
    # test re-enables it in-body.
    import gh
    gh.set_telemetry(None)
    yield


@pytest.fixture(autouse=True)
def _never_reach_real_cmux(monkeypatch):
    # Ratchet rule (2026-07-03 toast-spam incident, CLAUDE.md): no test may resolve cmux to
    # the real /Applications binary and fire a live desktop notification. notify._cmux_binary
    # falls back to the installed app when SL_CMUX is unset, so two runner tests with an
    # unconfigured notify channel toasted the owner's machine on every suite run — visible
    # only on machines that HAVE cmux, which is why per-test stubbing missed it. Point every
    # test at a guaranteed-absent path so an unconfigured send falls through to "log-only".
    # Tests that exercise cmux set their own SL_CMUX in-body (their monkeypatch wins), and
    # subprocess-driven tests pass explicit env dicts, untouched by this.
    if not os.environ.get("SL_CMUX"):
        monkeypatch.setenv("SL_CMUX", "/nonexistent/superlooper-test-cmux")


@pytest.fixture(autouse=True)
def _never_reach_real_gh(monkeypatch, _clear_worker_launch_env):
    # The `_clear_worker_launch_env` parameter is an ORDERING DEPENDENCY, not a value: this guard
    # only fires when SL_GH is unset, and inside a worker pane SL_GH is ambient (launch-session.sh
    # names it in the dropped command). Declaring the scrub as a dependency makes "scrub first, then
    # neutralize" explicit rather than an accident of definition order.
    # Same ratchet as _never_reach_real_cmux, for the GitHub CLI. lib/gh.py, stack_doctor and — since
    # the positive auth assert (issue #299) — launch-session.sh + start-session.sh ALL resolve their
    # gh through SL_GH, falling back to whatever `gh` is on PATH. On a dogfooding machine that is a
    # real, logged-in gh: a test that forgot to stub would quietly make live GitHub calls as the
    # owner's account. Point the default at a guaranteed-absent path so a missed stub fails loudly
    # (rc 127) instead of succeeding for real. Tests that exercise gh set their own SL_GH in-body.
    if not os.environ.get("SL_GH"):
        monkeypatch.setenv("SL_GH", "/nonexistent/superlooper-test-gh")


@pytest.fixture(autouse=True)
def _never_reach_real_claude(monkeypatch, _clear_worker_launch_env):
    # Same ratchet as _never_reach_real_cmux / _never_reach_real_gh, for the coding agent itself —
    # `claude` is named in CLAUDE.md's rule but had no neutralizer, and issue #303 made the gap
    # matter: the doctor's claude blocks now resolve through one ladder (SL_CLAUDE -> the standalone
    # install at ~/.local/bin/claude -> PATH), and cmd_stack_doctor builds a REAL Probe. On a
    # dogfooding machine both of the lower rungs hit a real, logged-in Claude Code, so a test that
    # forgot to stub would run `claude auth status` / `claude plugin list` for real. Pointing the pin
    # at a guaranteed-absent path makes the ladder stop at rung 1 and fail LOUDLY (the same
    # fail-closed refusal start-session.sh gives a broken pin) instead of succeeding for real. Tests
    # that exercise a claude set their own SL_CLAUDE in-body, and subprocess-driven tests build
    # explicit env dicts. The ordering dependency on _clear_worker_launch_env is the same as
    # _never_reach_real_gh's: scrub an ambient value first, then neutralize.
    if not os.environ.get("SL_CLAUDE"):
        monkeypatch.setenv("SL_CLAUDE", "/nonexistent/superlooper-test-claude")


@pytest.fixture(autouse=True)
def _never_reach_real_session_host(monkeypatch, _clear_worker_launch_env):
    # Same ratchet as _never_reach_real_cmux / _never_reach_real_gh / _never_reach_real_claude, for
    # the session host (issue #304). lib/session_host.py resolves its binary through SL_HERDR and
    # falls back to the bare name on PATH — and on the fleet machine that is a real binary talking
    # to a real server over a socket. Unlike the read-only probes above, this one's verbs CREATE and
    # CLOSE sessions: a test that forgot to inject a probe could tear down the owner's own window.
    # Point the default at a guaranteed-absent path so a missed injection fails loudly (rc 127)
    # instead of driving the live fleet. The ordering dependency on _clear_worker_launch_env is the
    # same as the others': scrub an ambient value first, then neutralize.
    if not os.environ.get("SL_HERDR"):
        monkeypatch.setenv("SL_HERDR", "/nonexistent/superlooper-test-herdr")


@pytest.fixture(autouse=True)
def _never_reach_real_launchctl(monkeypatch, _clear_worker_launch_env):
    # Same ratchet as the four above, for macOS's service manager (issue #306). The runner's
    # login-item home is a launchd job, so the CLI and the doctor shell `launchctl` to bootstrap,
    # bootout, kickstart and print it — verbs that START AND STOP the owner's own jobs, including
    # the live runner and the watchdog. `/bin/launchctl` is the unconditional fallback and always
    # exists on this platform, so an un-stubbed test would reach the real one every time. Point the
    # default at a guaranteed-absent path so a missed stub fails loudly instead of moving a real
    # service. Tests that exercise it set their own SL_LAUNCHCTL in-body.
    if not os.environ.get("SL_LAUNCHCTL"):
        monkeypatch.setenv("SL_LAUNCHCTL", "/nonexistent/superlooper-test-launchctl")


@pytest.fixture(autouse=True)
def _never_probe_real_auth(monkeypatch):
    # Same ratchet as _never_reach_real_cmux, for the account-auth probe (issue #159). The runner's
    # default Runner._probe_auth shells out to the REAL `claude auth status` + `security` keychain on
    # every tick with a spend pending — external binaries the "no test reaches a real binary" rule
    # forbids (and on a dev machine that HAS claude, a live auth read). Neutralize the DEFAULT to a
    # fail-open "unknown" snapshot so any ticking runner test is safe without opting in. Tests that
    # exercise the probe set their own instance _probe_auth in-body (that instance attr wins); the
    # probe FUNCTION itself (usage.probe_auth) is untouched, so test_usage still tests the real thing.
    import runner as runner_mod
    monkeypatch.setattr(runner_mod.Runner, "_probe_auth",
                        lambda self: {"cli": "unknown", "keychain_present": None,
                                      "keychain_mtime": None, "valid": None, "status_raw": ""},
                        raising=False)


@pytest.fixture(autouse=True)
def _never_probe_real_display(monkeypatch):
    # Same ratchet as _never_probe_real_auth, for the display-sleep probe (issue #124). The runner's
    # default Runner._display_asleep shells out to the REAL `pmset -g systemstate` on every tick with
    # launch demand — an external binary the "no test reaches a real binary" rule forbids, AND a read
    # whose result (display asleep vs awake) would make ticking runner tests non-deterministic on the
    # dev machine. Neutralize the DEFAULT to a fail-open "unknown" (None) so any ticking runner test
    # is safe without opting in and launches proceed. Tests that exercise the hold set their own
    # instance _display_asleep in-body (the instance attr wins over this class patch); the probe
    # FUNCTION itself (runner.display_asleep) is untouched, so test_runner still tests the real parse.
    import runner as runner_mod
    monkeypatch.setattr(runner_mod.Runner, "_display_asleep", lambda self: None, raising=False)
