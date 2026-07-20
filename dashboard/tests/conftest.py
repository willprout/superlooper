"""Make command-center's importable modules resolvable from tests without an install step, and
fail-closed-neutralize every path to a real external binary.

The pure decision cores live under ``lib/`` and the entry-point scripts under ``bin/``; tests
import them by bare name (``import config``), so we prepend both dirs to ``sys.path`` — ``lib``
before ``bin`` so a pure-core module wins a name collision with a same-named script. Paths are
computed relative to THIS file (the repo root is its parent), so imports resolve no matter where
pytest is invoked from.

Fail-closed external-binary neutralization (ported from superlooper's 2026-07-03 toast-spam
ratchet, then hardened): **no test may reach a real ``gh``, ``cmux``, ``osascript``, or the
``superlooper`` CLI — nor the network behind them.** The dashboard's egress to the outside world
is the ``gh`` CLI (all GitHub reads and label/comment/issue writes), the notifier (``cmux notify``
/ an ``osascript`` iMessage one-liner), and — from issue #41 — the local ``superlooper`` CLI the
Tidy button drives (which CLOSES session windows, so a stray real invocation in a test would touch
William's live cmux). Each MUST resolve through an env-var override:

    SL_GH          the gh binary          (lib/gh.py)
    SL_CMUX        the cmux binary         (lib/notify.py)
    SL_OSASCRIPT   the osascript binary    (lib/notify.py — the iMessage one-liner; forward
                   contract for Task 10: notify code MUST resolve osascript via THIS var so this
                   fixture can neutralize it globally, never a per-test PATH stub — that opt-in
                   stubbing is exactly the pattern the ratchet outlawed)
    SL_SECURITY    the macOS `security` binary (lib/pollers.py — the usage reader's Keychain read).
                   Neutralizing it fail-closes the usage path's Keychain access AND, transitively,
                   its network call (no token ⇒ no request to api.anthropic.com), so the whole usage
                   egress is off by default; a test that wants a real token injects a fake instead.
    SL_SUPERLOOPER the superlooper CLI     (lib/tidy.py — the Tidy button's `superlooper tidy`).
                   The runtime default is the CONFIGURED path (config's ``superlooper_cli``), but
                   ``lib/tidy`` lets THIS var override it exactly so the fixture can point every
                   test at an absent binary — `tidy` closes cmux windows, so a test must never
                   reach the real one; a tidy test injects tests/fakes/fake-superlooper in-body.
    SL_LAUNCH_SESSION the engine's launch shim (lib/fixer.py — the Deploy Fixer button, issue #141).
                   The shim OPENS A REAL CMUX TAB and starts an interactive Claude session, so a
                   stray real call in a test would spawn a live agent on William's machine — the
                   most expensive stray call in this repo. Same name the ENGINE's own watchdog
                   resolves it by, so the dashboard and the engine agree on the override; the
                   runtime default is derived from config's ``superlooper_cli`` (a sibling in the
                   engine's bin/). A fixer test injects tests/fakes/fake-launch-session in-body.

The autouse fixture points every one at a guaranteed-absent path **unconditionally** — even a
real value exported into the caller's shell is overridden — so the suite is fail-closed by
DEFAULT and cannot be tricked into hitting a live binary. A test that genuinely exercises one of
these overrides it IN-BODY with ``monkeypatch`` (its setenv runs after this fixture and wins for
that one test); subprocess-driven tests that pass an explicit ``env`` dict are untouched. Because
the only network path is ``gh``, neutralizing it is also what keeps the suite off the network.

``tests/test_conftest_guard.py`` fails loudly if this neutralization is ever removed — it asserts
a sentinel that ONLY this fixture sets, so a deleted fixture can never masquerade as "the shell
already had absent paths".
"""
import os
import sys
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# Insert in REVERSE of priority so the first-listed dir ends up first on sys.path (each
# insert(0) prepends): lib must precede bin so a pure-core module wins a name collision.
for _sub in reversed(("lib", "bin")):
    _p = str(_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Guaranteed-absent paths: nothing exists here, so any resolver that reaches its fallback fails
# closed (log-only writes, empty-but-typed reads) instead of touching a real binary.
NEUTRALIZED_BINARIES = {
    "SL_GH": "/nonexistent/command-center-test-gh",
    "SL_CMUX": "/nonexistent/command-center-test-cmux",
    "SL_OSASCRIPT": "/nonexistent/command-center-test-osascript",
    "SL_SECURITY": "/nonexistent/command-center-test-security",
    "SL_SUPERLOOPER": "/nonexistent/command-center-test-superlooper",
    "SL_LAUNCH_SESSION": "/nonexistent/command-center-test-launch-session",
}

# Set ONLY by the autouse fixture below — the guard asserts it, so deleting the fixture cannot
# false-negative (no shell would ever export this name).
NEUTRALIZATION_SENTINEL = "_CC_TEST_EXTERNALS_NEUTRALIZED"

# The message our blocked urlopen raises — the guard test matches it to prove the block is OURS,
# not merely a connection that happened to fail.
NETWORK_BLOCK_MESSAGE = "command-center test guard: no real network — inject a fake transport"


def _blocked_urlopen(*args, **kwargs):
    raise RuntimeError(NETWORK_BLOCK_MESSAGE)


@pytest.fixture(autouse=True)
def _never_reach_real_externals(monkeypatch):
    # Unconditional: overwrite even a real value the caller exported, so the whole suite is
    # fail-closed by default. Tests that need a specific fake set it in-body with monkeypatch —
    # that runs after this fixture and wins; it is the ONLY sanctioned override path (never a
    # shell export, never a per-test PATH stub).
    for var, absent in NEUTRALIZED_BINARIES.items():
        monkeypatch.setenv(var, absent)
    # The usage reader is the repo's first network-capable code (lib/pollers.py hits
    # api.anthropic.com). Neutralizing the egress BINARY (SL_SECURITY) already fail-closes the
    # DEFAULT path (no token ⇒ no request), but block the transport itself too so a future test
    # that injects a token can't silently reach the wire — the same fail-closed-by-default ratchet,
    # at the network layer. A test genuinely exercising HTTP injects a fake http_get / transport;
    # it never unblocks this.
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)
    monkeypatch.setenv(NEUTRALIZATION_SENTINEL, "1")


@pytest.fixture(autouse=True)
def _telemetry_off_by_default():
    # GitHub API-burn telemetry (issue #15) is a process-global toggle the server turns ON at boot
    # (bin/command-center). A test that boots the server in-process (test_command_center) would
    # otherwise leak that ON state into every later test — and with telemetry on, a gh call writes
    # burn rows into the REAL state home. Force it OFF before every test so the default is fail-closed
    # exactly like the external-binary neutralization above; a telemetry test re-enables it in-body.
    import gh
    gh.set_telemetry_enabled(False)
    yield
