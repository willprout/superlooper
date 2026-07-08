"""Pin the conftest sys.path contract: both skill dirs importable, lib before bin.

conftest.py is load-bearing — every other test imports the skill's modules through the
sys.path it sets up. This regression test locks the precedence (a pure skill/lib module must
win a name collision over a same-named skill/bin script) and gives the scaffold a green test.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_skill_dirs_on_syspath_lib_before_bin():
    lib = str(_ROOT / "skill/lib")
    binp = str(_ROOT / "skill/bin")
    assert lib in sys.path, "skill/lib must be importable in tests"
    assert binp in sys.path, "skill/bin must be importable in tests"
    # lib must precede bin so a pure-core module wins a name collision with a bin script
    assert sys.path.index(lib) < sys.path.index(binp), "skill/lib must precede skill/bin"
