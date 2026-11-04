"""Issue #533 — the usage pill with no five-hour window.

``tests/js/usage_pill.test.js`` loads the real ``static/usagepill.js`` and reads what it renders: the
seven-day figure beside an empty five-hour, never a 0% invented from ``Math.round(null)``. The string
guards below hold the wiring, so a removal still fails the suite on a machine with no ``node``.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SUITE = _ROOT / "tests" / "js" / "usage_pill.test.js"
_STATIC = _ROOT / "static"


def test_usage_pill_renders_a_missing_five_hour_as_empty_in_a_real_js_engine():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed — the string guards below still hold the wiring")
    proc = subprocess.run([node, str(_SUITE)], cwd=str(_ROOT), timeout=120,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, "the usage pill suite failed:\n" + out
    m = re.search(r"(\d+) checks, (\d+) failure", out)
    assert m, "the usage pill suite printed no tally — did it run?\n" + out
    assert int(m.group(1)) > 0, "the usage pill suite asserted nothing:\n" + out


def test_the_shell_binds_the_pill_through_the_tested_bundle():
    shell = (_STATIC / "shell.js").read_text(encoding="utf-8")
    assert "window.UsagePill.html(usage)" in shell
    assert "Math.round(usage.five_hour_pct)" not in shell     # the 0%-from-null trap stays gone


def test_the_bundle_loads_before_the_shell():
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/usagepill.js"></script>' in index
    assert index.index("/usagepill.js") < index.index("/shell.js")
