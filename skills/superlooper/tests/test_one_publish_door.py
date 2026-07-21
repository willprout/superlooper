"""Permanent mechanical fence: exactly ONE door publishes into ``~/.claude/skills`` (issue #197).

The README's promise — "nothing executable ever reaches your machine without you seeing it and
saying yes" — rests on a single script keeping it: the gated repo-root ``bin/install.sh``, which
shows the engine diff since the last publish and refuses without an explicit OK. That promise is
only as good as the *absence* of a second way in. The migration cross-review (2026-07-08) found one
such second way — the standalone-era ``skills/superlooper/bin/install.sh``, which published the same
payload to the same ``~/.claude`` with no gate — and it was closed to a refusing tombstone
(``tests/test_install.py`` pins that one file's behaviour).

This fence is the general form of that fix, and it is what issue #197 actually asks for: not "is
that one script still a tombstone" but "**can any script publish without the gate**". It is
deliberately SELF-CONTAINED — it re-derives the invariant from the real repo tree, so it goes red
the moment a NEW publisher appears, whatever it is called and wherever it lives.

Two layers, because a door can be opened two ways:

1. **The naming ratchet.** You cannot write to a path you never name. Every script on the scanned
   surface that so much as *names* the installed-skill home must appear in ``_ALLOWED`` below, with
   a written reason. A new script that wants to publish has to add itself here first — which puts a
   human in the loop at review time, exactly where the gate's trust is supposed to live.

2. **The write ban.** Naming it is allowed (five files legitimately read it); *writing* it is not.
   Among the allow-listed files, only ``bin/install.sh`` may combine an installed-skill-home
   reference with a filesystem-write verb. The check follows variable indirection — a shell
   ``DEST="$HOME/.claude/skills/…"`` later handed to ``rsync``, or a Python name bound to that path
   later passed to ``shutil.copytree`` — because that is precisely how the gated installer itself is
   written, and a copycat would be written the same way.

**Scanned surface.** Every ``.sh`` and ``.py`` file in the repo except test files and ``conftest.py``.
Tests are exempt on purpose: they exercise the real installers against a fixture ``HOME``
(``tmp_path``) and must name the path to assert on it — including this file, whose meta-tests
construct violations deliberately. No test publishes into a real ``~/.claude`` (each overrides
``HOME``), and CI runs nothing but ``pytest``.

**Out of scope, stated so the boundary is honest.** This fence guards the installed-skill home. It
does not guard the other places the gated installer writes on its way past (``~/.zshrc`` via
``install-launch-shim.sh``, a 755 shim on ``PATH`` via ``install-cli-link.sh``) — those carry no
engine payload, and both are runnable standalone. That gap is filed as issue #280 rather than
widened into this issue.

The ``test_fence_flags_*`` meta-tests construct each violation class from synthetic source and
assert the fence catches it, so this guard can never rot into a vacuously-green test — the failure
mode that makes structural guards worthless.
"""
import ast
import os
import re
from pathlib import Path

# tests/test_one_publish_door.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]

# The one gated door, repo-relative.
_THE_DOOR = "bin/install.sh"

# Every script allowed to NAME the installed-skill home, and why. Only _THE_DOOR may write there;
# every other entry is a read/reference and is held to the write ban below.
_ALLOWED = {
    _THE_DOOR:
        "THE GATED DOOR. Shows the engine diff since the last publish and refuses without an "
        "explicit OK, then rsyncs the payload into the installed-skill home. The only publisher.",
    "skills/superlooper/skill/bin/install-cli-link.sh":
        "Reference only. Writes a thin 755 shim into a PATH dir whose body EXECS the installed "
        "CLI; the installed-skill home appears as that shim's exec target and in a 'not published "
        "yet' note, never as a write target.",
    "skills/superlooper/skill/lib/stack_doctor.py":
        "Read only. Reads the installed VERSION stamp (through an injectable probe) to measure "
        "publish drift — the doctor reports the gap, it never closes it.",
    "dashboard/lib/engine.py":
        "Read only. DEFAULT_INSTALL_DIR + install_dir() locate the installed copy so the drift "
        "banner can read its VERSION; the remedy it names is bin/install.sh, never itself.",
    "dashboard/lib/config.py":
        "Reference only. The default `superlooper_cli` config value points at the installed CLI.",
    "dashboard/lib/tidy.py":
        "Reference only. A docstring naming where the CLI is found by default.",
}

# Directory names never scanned.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}

# The installed-skill home, in every form a script can spell it: `~/.claude/skills/...`,
# `"$HOME/.claude/skills/..."`, `os.path.join(h, ".claude", "skills", ...)`,
# `home / ".claude" / "skills"`. Matching `.claude` followed within a few non-word characters by
# `skills` covers all of them without enumerating quoting styles. \W spans newlines, so a path
# joined across lines is caught too.
_SKILL_HOME = re.compile(r"\.claude\W{1,8}?skills\b")

# Shell verbs that mutate the filesystem. `install` needs its coreutils flag form so the word
# "install" in prose or a filename (install-cli-link.sh) is not mistaken for the command.
_SH_WRITE = re.compile(
    r"(?:^|[\s;&|(`$])(?:rsync|cp|mv|ln|scp|tee|touch|mkdir|rmdir|rm|chmod|chown|unzip|tar)\b"
    r"|\binstall\s+-"
)
# A redirect into a file. `2>&1`, `>&2`, `<<'HEREDOC'` and a prose arrow `->` are not writes.
_SH_REDIRECT = re.compile(r"(?<![0-9<>&=-])>>?(?![&|])")

# Python calls that mutate the filesystem.
_PY_WRITE_FUNCS = frozenset({
    "open", "makedirs", "mkdir", "replace", "rename", "remove", "unlink", "rmdir",
    "copy", "copy2", "copyfile", "copytree", "move", "rmtree", "symlink", "link",
    "write_text", "write_bytes", "touch", "run", "call", "check_call", "Popen",
})


# --------------------------------------------------------------- the scanned surface

def _is_test_file(rel):
    parts = Path(rel).parts
    return (
        "tests" in parts
        or Path(rel).name == "conftest.py"
        or Path(rel).name.startswith("test_")
    )


def _surface():
    """(relpath, text) for every non-test script in the repo."""
    out = []
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith((".sh", ".py")):
                continue
            path = Path(root) / name
            rel = path.relative_to(_REPO).as_posix()
            if _is_test_file(rel):
                continue
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
    return out


def _names_skill_home(text):
    return bool(_SKILL_HOME.search(text))


# --------------------------------------------------------------- shell write detection

def _strip_sh_comment(line):
    """Drop a shell comment, tracking quotes so a `#` inside a string survives."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _sh_offences(text):
    """Lines in a shell script that write to the installed-skill home, directly or via a variable."""
    lines = [_strip_sh_comment(l) for l in text.splitlines()]
    bound = set()
    for line in lines:
        m = re.match(r"\s*(?:export\s+|local\s+)?([A-Za-z_][A-Za-z_0-9]*)=(.*)$", line)
        if m and _SKILL_HOME.search(m.group(2)):
            bound.add(m.group(1))
    var_re = re.compile(r"\$\{?(" + "|".join(sorted(map(re.escape, bound))) + r")\}?") if bound else None

    hits = []
    for n, line in enumerate(lines, 1):
        if not (_SH_WRITE.search(line) or _SH_REDIRECT.search(line)):
            continue
        if _SKILL_HOME.search(line) or (var_re and var_re.search(line)):
            hits.append((n, line.strip()))
    return hits


# --------------------------------------------------------------- python write detection

def _own_scope_nodes(scope):
    """Every node under ``scope`` that belongs to ``scope`` — nested defs are their own scopes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _scopes(tree):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def _is_write_open(node):
    """``open(p)`` reads; only a mode carrying w/a/x/+ writes."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _py_offences(text):
    """Write calls in a Python module whose target is the installed-skill home, directly or via a
    name bound to it in the same scope."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    hits = []
    for scope in _scopes(tree):
        own = list(_own_scope_nodes(scope))
        bound = set()
        for node in own:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            if not _SKILL_HOME.search(ast.unparse(node.value)):
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)

        for node in own:
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name not in _PY_WRITE_FUNCS:
                continue
            if name == "open" and not _is_write_open(node):
                continue
            src = ast.unparse(node)
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if _SKILL_HOME.search(src) or (bound & names):
                hits.append((getattr(node, "lineno", 0), src.splitlines()[0][:120]))
    return hits


def _offences(rel, text):
    return _sh_offences(text) if rel.endswith(".sh") else _py_offences(text)


# --------------------------------------------------------------- layer 1: the naming ratchet

def test_only_allow_listed_scripts_name_the_installed_skill_home():
    strays = [rel for rel, text in _surface()
              if _names_skill_home(text) and rel not in _ALLOWED]
    assert not strays, (
        "these scripts name ~/.claude/skills but are not on the one-door allow-list: %s.\n"
        "If one of them publishes engine code, it must not — the gated %s is the only door "
        "(it shows the diff and requires an explicit OK). If it only READS or references the "
        "path, add it to _ALLOWED in this file with the reason." % (strays, _THE_DOOR)
    )


def test_the_allow_list_has_not_rotted():
    # A stale entry silently widens the ratchet: a renamed file drops off the scan and its name
    # keeps sitting here as if it were still accounted for.
    missing = [rel for rel in _ALLOWED if not (_REPO / rel).is_file()]
    assert not missing, "allow-listed files no longer exist (rename or delete the entry): %s" % missing
    reasonless = [rel for rel, why in _ALLOWED.items() if len(why.strip()) < 40]
    assert not reasonless, "every allow-list entry needs a real reason: %s" % reasonless


# --------------------------------------------------------------- layer 2: the write ban

def test_only_the_gated_installer_writes_into_the_installed_skill_home():
    offenders = {}
    for rel, text in _surface():
        if rel == _THE_DOOR or not _names_skill_home(text):
            continue
        hits = _offences(rel, text)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "these scripts WRITE into ~/.claude/skills, but %s is the only gated door: %s"
        % (_THE_DOOR, offenders)
    )


def test_the_one_door_is_still_a_gated_door():
    # The fence is worthless if the door it blesses stops asking. Pin the three properties that
    # make bin/install.sh a gate rather than a publisher: it computes an engine diff, it refuses
    # rather than assuming consent when it cannot ask, and its only non-interactive bypass is an
    # explicit --yes on the command line.
    text = (_REPO / _THE_DOOR).read_text(encoding="utf-8")
    assert "engine_gate" in text, "the gate function is gone from %s" % _THE_DOOR
    assert "diff --name-status" in text, "%s no longer shows the engine diff" % _THE_DOOR
    assert "[y/N]" in text, "%s no longer asks for an explicit OK" % _THE_DOOR
    assert "REFUSING" in text and "not a TTY" in text, (
        "%s must refuse when it cannot ask — never assume consent" % _THE_DOOR)
    assert re.search(r"-y\|--yes\)\s*ASSUME_YES=true", text), (
        "the only consent bypass must stay an explicit --yes flag")
    assert not re.search(r"ASSUME_YES=true\s*$", text, re.M), (
        "--yes must never become the default")


def test_the_standalone_era_nested_installer_stays_shut():
    # The specific door #197 was filed about. tests/test_install.py drives its behaviour; this
    # states the invariant next to the general fence so the two cannot drift apart.
    nested = _REPO / "skills" / "superlooper" / "bin" / "install.sh"
    assert nested.is_file(), "the nested installer's tombstone must stay in place"
    text = nested.read_text(encoding="utf-8")
    assert "refusing to publish" in text and "exit 1" in text
    assert not _names_skill_home(text), "the tombstone must not name the installed-skill home"


# --------------------------------------------------------------- meta-tests: the fence bites

_UNGATED_SH = """#!/usr/bin/env bash
SRC="$(dirname "$0")/../skill"
rsync -a --delete "$SRC"/ "$HOME/.claude/skills/superlooper"/
"""

_UNGATED_SH_INDIRECT = """#!/usr/bin/env bash
DEST="$HOME/.claude/skills/superlooper"
mkdir -p "$DEST"
rsync -a --delete ./skill/ "$DEST"/
"""

_UNGATED_PY = """import shutil
def publish(home):
    shutil.copytree("skill", home + "/.claude/skills/superlooper", dirs_exist_ok=True)
"""

_UNGATED_PY_INDIRECT = """import os, shutil
def publish(home):
    dest = os.path.join(home, ".claude", "skills", "superlooper")
    os.makedirs(dest, exist_ok=True)
    shutil.copytree("skill", dest, dirs_exist_ok=True)
"""

_READ_ONLY_SH = """#!/usr/bin/env bash
# The installed CLI lives at ~/.claude/skills/superlooper/bin/superlooper — an install step puts it there.
INSTALLED="$HOME/.claude/skills/superlooper/bin/superlooper"
if [ ! -e "$INSTALLED" ]; then echo "note: $INSTALLED is not published yet" >&2; fi
"""

_READ_ONLY_PY = """import os
def installed_sha(home):
    path = os.path.join(home, ".claude", "skills", "superlooper", "VERSION")
    with open(path) as f:
        return f.read().split()[0]
def unrelated_write(tmp):
    path = os.path.join(tmp, "scratch")
    os.makedirs(path, exist_ok=True)
"""


def test_fence_flags_a_new_ungated_shell_publisher():
    assert _sh_offences(_UNGATED_SH), "a direct rsync into the skill home must be caught"


def test_fence_follows_shell_variable_indirection():
    # The evasion that matters: name the path once, write to the variable. This is how the gated
    # installer itself is written, so a copycat would look exactly like this.
    hits = _sh_offences(_UNGATED_SH_INDIRECT)
    assert len(hits) >= 2, "both the mkdir and the rsync through $DEST must be caught: %s" % (hits,)


def test_fence_flags_a_new_ungated_python_publisher():
    assert _py_offences(_UNGATED_PY), "a direct copytree into the skill home must be caught"


def test_fence_follows_python_name_indirection():
    hits = _py_offences(_UNGATED_PY_INDIRECT)
    assert len(hits) >= 2, "both the makedirs and the copytree through `dest` must be caught: %s" % (hits,)


def test_fence_does_not_flag_a_read_only_reference():
    # The other half of a useful fence: it must stay quiet on the five files that legitimately
    # name the path, or it gets muted. Note the Python case reuses the name `path` in a second
    # function for an unrelated write — scope-aware binding is what keeps that from tripping.
    assert not _sh_offences(_READ_ONLY_SH)
    assert not _py_offences(_READ_ONLY_PY)


def test_fence_would_have_caught_the_original_second_door():
    # The pre-tombstone nested installer, reduced to its publishing core (git history: the copy
    # that shipped at migration). The fence exists so this can never come back under a new name.
    original = """#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../skill"
DEST="$HOME/.claude/skills/superlooper"
mkdir -p "$DEST"
rsync -a --delete "$SRC"/ "$DEST"/
printf '%s\\n' "$VERSION" > "$DEST/VERSION"
"""
    assert _sh_offences(original), "the fence must catch the door #197 was filed about"
