"""Guard (Task 12 / DoD): the repo carries NO William-specific absolute path.

command-center is shareable from day one (decision A.3) — when William shares the skill, a stranger
gets the dashboard too. That promise breaks the instant a hardcoded checkout path under the local
home leaks into a committed file: the stranger's clone would then point at a home directory that
does not exist on their machine. Per-user facts belong ONLY in the git-ignored
``config.json`` (see ``config.example.json``); the tracked tree a stranger receives must be
path-clean.

This greps every git-TRACKED text file for the actual home path of the user running the suite. It
deliberately does NOT ban ``/home/...`` or ``/Users/...`` wholesale: ``test_config.py`` uses a
synthetic stranger home (``/home/pat``) to PROVE the state-home derivation works for a non-William
user, and banning those would forbid the very shareability the guard exists to protect. It also does
not ban repo slugs like ``will-titan``; those are fixture/sample data, not local machine paths.

The needle is derived at runtime so THIS guard file is itself clean — the scan covers the whole tree
including this file, with nothing special-cased out.
"""
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Binary / vendored-art suffixes: reading them as text is meaningless and they cannot carry a path a
# clone would follow. Everything else — code, docs, JSON, shell, plists, fixtures — is scanned.
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".docx", ".pdf"}


def _tracked_files(root=_ROOT):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


def _forbidden_home_needles(home=None):
    raw_home = Path.home() if home is None else Path(home).expanduser()
    needles = []
    for candidate in (raw_home, raw_home.resolve()):
        text = str(candidate)
        if len(text) > 1 and text not in needles:
            needles.append(text)
    return tuple(needles)


def _find_offenders(root=_ROOT, tracked_files=None, needles=None):
    root = Path(root)
    if tracked_files is None:
        tracked_files = _tracked_files(root)
    if needles is None:
        needles = _forbidden_home_needles()

    offenders = []
    for rel in tracked_files:
        path = root / rel
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                       # unreadable-as-text ⇒ cannot carry a followable path
        for needle in needles:
            if needle in text:
                offenders.append("%s contains %r" % (rel, needle))
    return offenders


def test_forbidden_needles_are_derived_from_the_current_home():
    assert str(Path.home()) in _forbidden_home_needles()


def test_guard_flags_tracked_file_containing_the_current_home(tmp_path):
    rel = "sample.txt"
    (tmp_path / rel).write_text("leaked path: %s\n" % Path.home(), encoding="utf-8")

    offenders = _find_offenders(
        root=tmp_path,
        tracked_files=[rel],
        needles=_forbidden_home_needles(),
    )

    assert offenders == ["%s contains %r" % (rel, str(Path.home()))]


def test_will_titan_fixture_strings_are_not_machine_specific():
    text = "will-titan/command-center will-titan__superlooper-sandbox"

    assert not [needle for needle in _forbidden_home_needles() if needle in text]


def test_no_william_specific_absolute_paths_in_the_repo():
    offenders = _find_offenders()
    assert not offenders, (
        "William-specific absolute paths must never be committed (shareability, decision A.3) — "
        "move per-user facts into the git-ignored config.json:\n  " + "\n  ".join(offenders))
