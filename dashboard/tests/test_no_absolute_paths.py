"""Guard (Task 12 / DoD): the repo carries NO William-specific absolute path.

command-center is shareable from day one (decision A.3) — when William shares the skill, a stranger
gets the dashboard too. That promise breaks the instant a hardcoded ``/Users/<william>`` checkout
path or his account name leaks into a committed file: the stranger's clone would then point at a
home directory that does not exist on their machine. Per-user facts belong ONLY in the git-ignored
``config.json`` (see ``config.example.json``); the tracked tree a stranger receives must be
path-clean.

This greps every git-TRACKED text file for William's account name (which is the tail of every one
of his absolute home paths, ``/Users/<account>/...`` — so catching the name catches the paths). It
deliberately does NOT ban ``/home/…`` or ``/Users/…`` wholesale: ``test_config.py`` uses a synthetic
stranger home (``/home/pat``) to PROVE the state-home derivation works for a non-William user, and
banning those would forbid the very shareability the guard exists to protect.

The needle is assembled from fragments so THIS guard file is itself clean — the scan covers the
whole tree including this file, with nothing special-cased out.
"""
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Assembled from fragments so the literal never appears in this file — the guard scans itself too.
# William's macOS account name; it is the tail of every absolute path under his home
# (``/Users/<account>/...``), so one needle covers every William-home leak.
_WILLIAM_ACCOUNT = "william" + "prout"
_FORBIDDEN = (_WILLIAM_ACCOUNT,)

# Binary / vendored-art suffixes: reading them as text is meaningless and they cannot carry a path a
# clone would follow. Everything else — code, docs, JSON, shell, plists, fixtures — is scanned.
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".docx", ".pdf"}


def _tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(_ROOT),
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


def test_no_william_specific_absolute_paths_in_the_repo():
    offenders = []
    for rel in _tracked_files():
        path = _ROOT / rel
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                       # unreadable-as-text ⇒ cannot carry a followable path
        for needle in _FORBIDDEN:
            if needle in text:
                offenders.append("%s contains %r" % (rel, needle))
    assert not offenders, (
        "William-specific absolute paths must never be committed (shareability, decision A.3) — "
        "move per-user facts into the git-ignored config.json:\n  " + "\n  ".join(offenders))
