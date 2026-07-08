"""Guard the conftest's fail-closed neutralization + import contract.

conftest.py is load-bearing: every other test imports the dashboard's modules through the
sys.path it sets up, and the whole suite relies on its autouse fixture to keep tests off real
``gh``/``cmux``/``osascript`` and the network (2026-07-03 toast-spam ratchet — stubbing must be
fail-closed-global, never opt-in-per-test). These tests fail the moment either protection is
removed, on EVERY machine — not only ones that happen to have the real binaries installed.
"""
import os
import sys
import urllib.request
from pathlib import Path

import pytest

import conftest  # the sibling conftest; importable because pytest puts tests/ on sys.path

_ROOT = Path(__file__).resolve().parent.parent


def test_external_binary_neutralization_fixture_ran():
    # The sentinel is set ONLY by the autouse fixture, so its presence proves the fixture RAN —
    # not merely that the current environment happens to hold absent paths. Deleting the fixture
    # makes this None (no shell exports this name), so a removed guard can never false-negative.
    assert os.environ.get(conftest.NEUTRALIZATION_SENTINEL) == "1", (
        "the conftest autouse neutralization fixture did not run — external binaries are unguarded")
    # And every egress binary override resolves to its EXACT neutralized (absent) sentinel path,
    # so no test can reach a live gh / cmux / osascript.
    for var, expected in conftest.NEUTRALIZED_BINARIES.items():
        assert os.environ.get(var) == expected, (
            f"{var} is {os.environ.get(var)!r}, expected the neutralized {expected!r}")
        assert not Path(expected).exists(), (
            f"{var}={expected!r} must point at an absent path; tests must never reach a live binary")


def test_network_urlopen_is_neutralized():
    # The autouse fixture replaces urllib.request.urlopen with a raiser carrying OUR sentinel
    # message — so this proves the guard is active, not that a real connection merely failed. No
    # network is touched: the block raises before any socket work. If the guard were removed, this
    # would raise a urllib error (not our RuntimeError) and the test fails.
    with pytest.raises(RuntimeError, match="no real network"):
        urllib.request.urlopen("http://example.invalid/")


def test_lib_and_bin_on_syspath_lib_before_bin():
    lib = str(_ROOT / "lib")
    binp = str(_ROOT / "bin")
    assert lib in sys.path, "lib/ must be importable in tests"
    assert binp in sys.path, "bin/ must be importable in tests"
    # lib must precede bin so a pure-core module wins a name collision with a same-named script.
    assert sys.path.index(lib) < sys.path.index(binp), "lib/ must precede bin/ on sys.path"
