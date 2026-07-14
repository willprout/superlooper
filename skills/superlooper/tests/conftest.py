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
    ):
        monkeypatch.delenv(name, raising=False)


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
