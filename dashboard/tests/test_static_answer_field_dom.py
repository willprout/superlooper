"""Issue #475 — run the BEHAVIOURAL proof that a typed answer survives the poll redraw.

The acceptance bar for #475 is behavioural: "typed, unsubmitted text survives snapshot polls — a
test drives at least two redraw cycles with a dirty field and asserts the value and focus survive."
A string guard on the bundle cannot show that. So ``tests/js/answer_field_survives.test.js`` loads
the REAL ``static/*.js`` into a minimal DOM (``tests/js/minidom.js``), types into the real rendered
Answer field and drives real redraw cycles — three per survival case, one more than the bar.

Why ``node`` and not a browser-driver dependency: the runtime is Python 3 stdlib only, CI installs
nothing but pytest, and the CI workflow is a bright line a worker may not edit. ``node`` is already
on the GitHub runner image and on the deployment Mac, so this runs in the gate with no new
dependency and no workflow change. It never touches the network or any external binary beyond
``node`` itself (conftest's fail-closed neutralization is about ``gh``/``cmux``/``superlooper``).

If ``node`` is genuinely absent the behavioural proof skips — but the WIRING it protects is also
guarded unconditionally by ``test_static_answer_field_survives_poll.py``, so a removal of the fix
still fails the suite on a machine with no node.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SUITE = _ROOT / "tests" / "js" / "answer_field_survives.test.js"


def test_typed_answer_survives_the_poll_redraw_in_a_real_dom():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed — the string guards in "
                    "test_static_answer_field_survives_poll.py still hold the wiring")
    proc = subprocess.run([node, str(_SUITE)], cwd=str(_ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, "the behavioural answer-field suite failed:\n" + out
    # Fail loudly rather than pass on an empty run: a harness that silently asserted nothing would
    # be the same class of inert guard this repo has been bitten by before (a green suite that
    # proved nothing — issue #165's near-miss).
    m = re.search(r"(\d+) checks, (\d+) failure", out)
    assert m, "the behavioural suite printed no tally — did it run?\n" + out
    assert int(m.group(1)) > 0, "the behavioural suite asserted nothing:\n" + out
