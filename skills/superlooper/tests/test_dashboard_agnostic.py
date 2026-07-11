"""Issue #45 — the engine stays dashboard-agnostic (a grep-guard).

The command-center (the dashboard) is a renderer OVER the engine's state; the dependency runs one
way only. The engine must never NAME, IMPORT, or SHELL the dashboard. The one coupling the issue
introduces — a single command that brings up both the dashboard and a repo's runner — rides a
GENERIC config contract on the DASHBOARD side, which shells the engine's own documented
``superlooper run``; the engine learns nothing about the dashboard's existence.

This guard fails if engine code ever grows a reference to the dashboard's entry points, its
directory, or its reader modules — so the boundary can't erode silently in a later change.

Scanned: the engine's executable code (``skill/bin`` + ``skill/lib``). NOT the reference docs under
``skill/references`` — those legitimately DESCRIBE the command-center's surfaces in prose (the word
"command-center" appears there on purpose), and prose is not coupling.
"""
import re
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parent.parent / "skill"
_CODE_DIRS = (_SKILL / "bin", _SKILL / "lib")

# Dashboard-identifying tokens no engine file may name (case-insensitive substrings): the two bin
# entry points, the new one-command, and any path INTO the dashboard tree. Bare "dashboard" is
# deliberately absent — engine comments legitimately mention the dashboard as the external reader
# ("the dashboard's dead-man's switch never fired"); a slash-qualified path or a bin name is the
# coupling, a descriptive noun in a comment is not.
_FORBIDDEN_TOKENS = ("command-center", "command_center", "liftoff", "dashboard/")

# Dashboard-only lib modules — names that exist ONLY in the dashboard's lib/, never the engine's
# — so an engine ``import`` of any one is a hard coupling. The names the two suites SHARE (gh,
# config, notify, tidy, actions, watchdog) are excluded: they name the engine's own modules too.
_DASHBOARD_ONLY_MODULES = ("readers", "flights", "cards", "desk", "tower", "digest",
                           "pollers", "replay")
_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(%s)\b" % "|".join(_DASHBOARD_ONLY_MODULES), re.MULTILINE)


def _engine_code_files():
    files = []
    for d in _CODE_DIRS:
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix in ("", ".py", ".sh"):
                files.append(p)
    return files


def test_there_is_engine_code_to_scan():
    # Guard against a broken glob silently scanning nothing (a green that would prove nothing).
    names = {p.name for p in _engine_code_files()}
    assert "runner.py" in names and "superlooper" in names


@pytest.mark.parametrize("path", _engine_code_files(), ids=lambda p: p.name)
def test_engine_file_names_no_dashboard_token(path):
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    hits = [t for t in _FORBIDDEN_TOKENS if t in text]
    assert not hits, (f"{path.name} references dashboard token(s) {hits} — the engine must stay "
                      f"dashboard-agnostic; any coupling rides the dashboard-side config contract")


@pytest.mark.parametrize("path", _engine_code_files(), ids=lambda p: p.name)
def test_engine_file_imports_no_dashboard_module(path):
    m = _IMPORT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    assert not m, (f"{path.name} imports dashboard-only module {m.group(1)!r} — the engine must "
                   f"stay dashboard-agnostic")
