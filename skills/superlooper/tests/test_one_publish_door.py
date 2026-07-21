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

   "Names" is resolved, not literal, because the cheap way around a literal check is to spell the
   path in two halves. Shell and Python bindings are expanded to a fixpoint before matching, so
   ``CLAUDE_DIR="$HOME/.claude"`` followed by ``DEST="$CLAUDE_DIR/skills/superlooper"`` counts, and
   so does ``B="$A"`` three hops down. Importing one of the module constants that already resolve to
   the install dir (``engine.DEFAULT_INSTALL_DIR``, ``engine.install_dir``,
   ``config._DEFAULT_SUPERLOOPER_CLI``) counts too — otherwise a publisher could borrow the path
   from an allow-listed file and never spell it itself.

2. **The write ban.** Naming it is allowed (six files legitimately read it); *writing* it is not.
   Among the allow-listed files, only ``bin/install.sh`` may combine an installed-skill-home
   reference with a filesystem-write verb, and the check follows the same resolved bindings — a
   shell ``DEST=…`` later handed to ``rsync``, or a Python name bound to the path (in this scope
   *or any enclosing one*, which is how a module constant reaches a function body) later passed to
   ``shutil.copytree``. That is precisely how the gated installer is written, and how a copycat
   would be. Note the reach: this layer only inspects files that name the home, i.e. in practice the
   allow-list. Layer 1 is what stops everything else — do not read layer 2 as a general sweep.

**Scanned surface.** Every tracked script in the repo — by extension (``.sh``, ``.bash``, ``.zsh``,
``.py``) *and* by shebang, because the load-bearing ones carry no extension at all: the engine CLI
``skill/bin/superlooper``, ``dashboard/bin/command-center`` and ``dashboard/bin/liftoff`` are
extensionless ``#!/usr/bin/env python3`` files, and an extension-only sweep would leave the engine's
own entry point outside the fence. The surface is ``git ls-files`` (with a filesystem walk as the
fallback for a git-less tree), so it judges exactly what CI checks out — an untracked scratch script
in a working tree neither reddens the suite nor slips a door past it, because it cannot merge.

``conftest.py`` and ``test_*.py`` are exempt: they exercise the real installers against a fixture
``HOME`` (``tmp_path``) and must name the path to assert on it — including this file, whose
meta-tests construct violations deliberately. No test publishes into a real ``~/.claude`` (each
overrides ``HOME``), and CI runs nothing but ``pytest``. Everything else under ``tests/`` — fakes,
fixtures, helper scripts — IS scanned.

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
import subprocess
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
    "dashboard/bin/command-center":
        "Read only. Passes the resolved install dir to engine's drift reader when it assembles the "
        "snapshot; it renders the drift banner, it never acts on it.",
}

# Directory names never scanned (only used by the no-git fallback walk).
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}

# The installed-skill home, in every form a script can spell it: `~/.claude/skills/...`,
# `"$HOME/.claude/skills/..."`, `os.path.join(h, ".claude", "skills", ...)`,
# `home / ".claude" / "skills"`. Matching `.claude` followed within a few non-word characters by
# `skills` covers all of them without enumerating quoting styles. \W spans newlines, so a path
# joined across lines is caught too.
_SKILL_HOME = re.compile(r"\.claude\W{1,8}?skills\b")
# The two halves, for the split-path case: a value that reaches `.claude`, and a value that adds
# `skills` onto one. Neither alone is the installed-skill home; joined across bindings, they are.
_CLAUDE_DIR = re.compile(r"\.claude\b")
_SKILLS_WORD = re.compile(r"\bskills\b")

# Names that already RESOLVE to the install dir. Borrowing one of these from an allow-listed module
# is a way to reach the installed-skill home without ever spelling it, so they count as naming it.
_ALIASES = ("DEFAULT_INSTALL_DIR", "_DEFAULT_SUPERLOOPER_CLI", "install_dir")
_ALIAS_RE = re.compile(r"\b(?:%s)\b" % "|".join(_ALIASES))

# Shell verbs that mutate the filesystem. `install` needs its coreutils flag form so the word
# "install" in prose or a filename (install-cli-link.sh) is not mistaken for the command.
_SH_WRITE = re.compile(
    r"(?:^|[\s;&|(`$])(?:rsync|cp|mv|ln|scp|tee|touch|mkdir|rmdir|rm|chmod|chown|unzip|tar"
    r"|dd|ditto|unlink|truncate)\b"
    r"|\binstall\s+-"
    r"|\bsed\s+-i"
    r"|\bgit\s+(?:clone|checkout|worktree\s+add)\b"
)
# A redirect into a file. `>&2`, `2>&1`, `<<'HEREDOC'` and a prose arrow `->` are not writes; a
# numbered redirect to a PATH (`2> file`) is.
_SH_REDIRECT = re.compile(r"(?<![<>&=-])>>?(?![&|])")

# Python calls that mutate the filesystem outright.
_PY_WRITE_FUNCS = frozenset({
    "open", "makedirs", "mkdir", "replace", "rename", "remove", "unlink", "rmdir",
    "copy", "copy2", "copyfile", "copytree", "move", "rmtree", "symlink", "link",
    "write_text", "write_bytes", "touch", "unpack_archive", "extractall",
})
# Calls that shell out. These write only if the command they carry writes — `subprocess.run(["git",
# "log", path])` over the installed VERSION is a read, and flagging it would push a maintainer to
# mute the fence. So these are checked against _SH_WRITE rather than assumed hostile.
_PY_SUBPROCESS_FUNCS = frozenset({
    "run", "call", "check_call", "check_output", "Popen", "system", "popen",
})
# A mutating command at the head of a string literal — how a shelled-out publish reads in an argv
# list (`["rsync", "-a", dest]`) or a command string (`os.system("cp -R … " + dest)`).
_ARGV_WRITE = re.compile(
    r"['\"](?:rsync|cp|mv|ln|scp|tee|touch|mkdir|rm|ditto|dd|install|unzip|tar)\b")

_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".zsh")


# --------------------------------------------------------------- the scanned surface

def _is_test_file(rel):
    name = Path(rel).name
    return name == "conftest.py" or name.startswith("test_")


def _shebang_kind(path):
    """'py' / 'sh' / None from a file's shebang, read without slurping the whole file."""
    try:
        with open(path, "rb") as f:
            first = f.readline(256).decode("utf-8", "replace")
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    if "python" in first:
        return "py"
    # Any other shebang (sh/bash/zsh/node/ruby/perl) is read with the line-based detector. It is
    # looser than the Python AST pass, but the naming ratchet — the layer that actually stops a new
    # door — does not depend on getting the language right.
    return "sh"


def _kind(path, rel):
    if rel.endswith(".py"):
        return "py"
    if rel.endswith((".sh", ".bash", ".zsh")):
        return "sh"
    return _shebang_kind(path)


def _tracked_files():
    """Repo-relative paths git tracks, or a pruned filesystem walk when there is no git."""
    try:
        out = subprocess.run(["git", "-C", str(_REPO), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            return sorted(p for p in out.stdout.split("\0") if p)
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            found.append((Path(root) / name).relative_to(_REPO).as_posix())
    return found


def _surface():
    """(relpath, kind, text) for every scanned script in the repo."""
    out = []
    for rel in _tracked_files():
        if _is_test_file(rel):
            continue
        path = _REPO / rel
        if not path.is_file():
            continue
        kind = _kind(path, rel)
        if kind:
            out.append((rel, kind, path.read_text(encoding="utf-8", errors="replace")))
    return out


def _mentions_home(text):
    """A literal reference to the installed-skill home, or to a name that already resolves to it."""
    return bool(_SKILL_HOME.search(text) or _ALIAS_RE.search(text))


# --------------------------------------------------------------- resolving bindings

def _taint(bindings, seed=()):
    """Names that resolve to the installed-skill home, given ``{name: (value_text, refs)}``.

    A path spelled in halves is the cheapest way around a literal check, so taint propagates over
    two levels, to a fixpoint (never textual expansion, which grows quadratically on a real
    module):

      * ``root`` — the value names ``.claude``, or references a name that does.
      * ``home`` — the value names the installed-skill home outright (or one of the resolving
        aliases); OR it joins ``skills`` onto a ``root`` name; OR it references a ``home`` name.

    So ``CLAUDE_DIR="$HOME/.claude"`` is root, ``SKILLS="$CLAUDE_DIR/skills"`` becomes home, and
    every hop after that stays home — even though neither line, read alone, spells the path.
    ``seed`` are names an enclosing Python scope already established as home.
    """
    root = {n for n, (text, _r) in bindings.items() if _CLAUDE_DIR.search(text)}
    home = set(seed) | {n for n, (text, _r) in bindings.items() if _mentions_home(text)}
    changed = True
    while changed:
        changed = False
        for name, (text, refs) in bindings.items():
            if name not in root and refs & root:
                root.add(name)
                changed = True
            if name in home:
                continue
            if (refs & home) or (refs & root and _SKILLS_WORD.search(text)):
                home.add(name)
                changed = True
    return home


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


_SH_ASSIGN = re.compile(
    r"\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)=(.*)$")


_SH_REF = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")


def _sh_home_vars(lines):
    """Shell variables that resolve to the installed-skill home, however many hops away."""
    values = {}
    for line in lines:
        m = _SH_ASSIGN.match(line)
        if m:
            values[m.group(1)] = values.get(m.group(1), "") + " " + m.group(2)
    return _taint({n: (v, set(_SH_REF.findall(v))) for n, v in values.items()})


def _sh_names_home(text):
    lines = [_strip_sh_comment(l) for l in text.splitlines()]
    return bool(_SKILL_HOME.search(text) or _ALIAS_RE.search(text) or _sh_home_vars(lines))


def _sh_offences(text):
    """Lines in a shell script that write to the installed-skill home, directly or via a variable."""
    lines = [_strip_sh_comment(l) for l in text.splitlines()]
    home_vars = _sh_home_vars(lines)
    var_re = (re.compile(r"\$\{?(?:" + "|".join(sorted(map(re.escape, home_vars))) + r")\}?")
              if home_vars else None)

    hits = []
    for n, line in enumerate(lines, 1):
        if not (_SH_WRITE.search(line) or _SH_REDIRECT.search(line)):
            continue
        if _mentions_home(line) or (var_re and var_re.search(line)):
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


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_write_open(node):
    """``open(p)`` reads; only a mode carrying w/a/x/+ writes."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def _py_write_aliases(tree):
    """`from shutil import copytree as ct` -> {'ct'}. A rename must not shed the check."""
    extra = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[-1] in _PY_WRITE_FUNCS:
                    extra.add(alias.asname)
    return extra


def _py_scope_home_names(scope, inherited):
    """Names in ``scope`` that resolve to the installed-skill home, seeded with ``inherited``.

    ``inherited`` carries the enclosing scopes' tainted names, so a module constant
    (``DEFAULT_INSTALL_DIR = "~/.claude/skills/superlooper"``) is visible to a function body that
    only ever says ``os.path.expanduser(DEFAULT_INSTALL_DIR)``.
    """
    bindings = {}
    for node in _own_scope_nodes(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        src = ast.unparse(value)
        refs = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
        for t in targets:
            if isinstance(t, ast.Name):
                prev = bindings.get(t.id, ("", set()))
                bindings[t.id] = (prev[0] + " " + src, prev[1] | refs)
    return _taint(bindings, seed=inherited)


def _py_scan(scope, inherited, write_names, hits, seen):
    # ``inherited`` only ever flows DOWN (module -> class -> function). A nested function's locals
    # must not leak sideways into its siblings, or one honest helper taints the whole module and
    # the fence starts crying wolf. ``seen`` accumulates every tainted name for the naming ratchet.
    home = _py_scope_home_names(scope, inherited)
    seen |= home

    for node in _own_scope_nodes(scope):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            src = ast.unparse(node)
            if name in _PY_SUBPROCESS_FUNCS:
                if not (_SH_WRITE.search(src) or _ARGV_WRITE.search(src)):
                    continue
            elif name in write_names:
                if name == "open" and not _is_write_open(node):
                    continue
            else:
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if _mentions_home(src) or (home & names):
                hits.append((getattr(node, "lineno", 0), src.splitlines()[0][:120]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _py_scan(node, home, write_names, hits, seen)
    return seen


def _py_parse(text):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _py_names_home(text):
    if _mentions_home(text):
        return True
    tree = _py_parse(text)
    if tree is None:
        return False
    return bool(_py_scan(tree, set(), _PY_WRITE_FUNCS, [], set()))


def _py_offences(text):
    """Write calls in a Python module whose target is the installed-skill home, directly or via a
    name bound to it in this scope or any enclosing one."""
    tree = _py_parse(text)
    if tree is None:
        return []
    hits = []
    _py_scan(tree, set(), _PY_WRITE_FUNCS | _py_write_aliases(tree), hits, set())
    return hits


def _names_home(kind, text):
    return _py_names_home(text) if kind == "py" else _sh_names_home(text)


def _offences(kind, text):
    return _py_offences(text) if kind == "py" else _sh_offences(text)


# --------------------------------------------------------------- layer 1: the naming ratchet

def test_only_allow_listed_scripts_name_the_installed_skill_home():
    strays = [rel for rel, kind, text in _surface()
              if _names_home(kind, text) and rel not in _ALLOWED]
    assert not strays, (
        "these scripts resolve a path into ~/.claude/skills but are not on the one-door "
        "allow-list: %s.\n"
        "If one of them publishes engine code, it must not — the gated %s is the only door "
        "(it shows the diff and requires an explicit OK). If it only READS or references the "
        "path, add it to _ALLOWED in this file with the reason." % (strays, _THE_DOOR)
    )


def test_extensionless_scripts_are_on_the_scanned_surface():
    # The reason the sweep reads shebangs and not just suffixes: the engine's own CLI and both
    # dashboard entry points carry no extension. An extension-only fence would leave the most
    # obvious place to bolt a republish command onto entirely unwatched.
    scanned = {rel for rel, _kind_, _text in _surface()}
    for rel in ("skills/superlooper/skill/bin/superlooper",
                "dashboard/bin/command-center",
                "dashboard/bin/liftoff"):
        assert rel in scanned, "extensionless script %s must be scanned (shebang sweep)" % rel


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
    for rel, kind, text in _surface():
        if rel == _THE_DOOR or not _names_home(kind, text):
            continue
        hits = _offences(kind, text)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "these scripts WRITE into ~/.claude/skills, but %s is the only gated door: %s"
        % (_THE_DOOR, offenders)
    )


def test_the_one_door_is_still_a_gated_door():
    # The fence is worthless if the door it blesses stops asking. Pin the properties that make
    # bin/install.sh a gate rather than a publisher: it computes an engine diff, it detects that it
    # cannot ask, it refuses rather than assuming consent, and its only non-interactive bypass is an
    # explicit --yes that defaults off.
    text = (_REPO / _THE_DOOR).read_text(encoding="utf-8")
    assert "engine_gate" in text, "the gate function is gone from %s" % _THE_DOOR
    assert "diff --name-status" in text, "%s no longer shows the engine diff" % _THE_DOOR
    assert "[y/N]" in text, "%s no longer asks for an explicit OK" % _THE_DOOR
    assert "[ ! -t 0 ]" in text, (
        "%s must still test for a TTY — that test is what makes 'refuse, never assume' fire "
        "in a non-interactive run" % _THE_DOOR)
    assert "REFUSING" in text and "not a TTY" in text, (
        "%s must refuse when it cannot ask — never assume consent" % _THE_DOOR)
    assert re.search(r"^ASSUME_YES=false\s*$", text, re.M), (
        "consent must default to OFF: the --yes bypass has to start false and be turned on only "
        "by the flag")


def test_the_standalone_era_nested_installer_stays_shut():
    # The specific door #197 was filed about. tests/test_install.py drives its behaviour; this
    # states the invariant next to the general fence so the two cannot drift apart.
    nested = _REPO / "skills" / "superlooper" / "bin" / "install.sh"
    assert nested.is_file(), "the nested installer's tombstone must stay in place"
    text = nested.read_text(encoding="utf-8")
    assert "refusing to publish" in text and "exit 1" in text
    assert not _sh_names_home(text), "the tombstone must not resolve the installed-skill home"


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

_UNGATED_SH_TWO_STEP = """#!/usr/bin/env bash
CLAUDE_DIR="$HOME/.claude"
SKILLS="$CLAUDE_DIR/skills"
DEST="$SKILLS/superlooper"
THERE="$DEST"
rsync -a --delete ./skill/ "$THERE"/
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

_UNGATED_PY_MODULE_CONST = """import os, shutil
DEFAULT_INSTALL_DIR = "~/.claude/skills/superlooper"
def publish_now():
    dest = os.path.expanduser(DEFAULT_INSTALL_DIR)
    shutil.copytree("payload", dest, dirs_exist_ok=True)
"""

_UNGATED_PY_BORROWED = """from engine import DEFAULT_INSTALL_DIR
from shutil import copytree as ct
def publish_now():
    ct("payload", DEFAULT_INSTALL_DIR, dirs_exist_ok=True)
"""

_UNGATED_PY_SUBPROCESS = """import os, subprocess
def publish(home):
    dest = os.path.join(home, ".claude", "skills", "superlooper")
    subprocess.run(["rsync", "-a", "--delete", "skill/", dest])
"""

_READ_ONLY_SH = """#!/usr/bin/env bash
# The installed CLI lives at ~/.claude/skills/superlooper/bin/superlooper — an install step puts it there.
INSTALLED="$HOME/.claude/skills/superlooper/bin/superlooper"
if [ ! -e "$INSTALLED" ]; then echo "note: $INSTALLED is not published yet" >&2; fi
"""

_READ_ONLY_PY = """import os, subprocess
def installed_sha(home):
    path = os.path.join(home, ".claude", "skills", "superlooper", "VERSION")
    with open(path) as f:
        return f.read().split()[0]
def last_touched(home):
    path = os.path.join(home, ".claude", "skills", "superlooper", "VERSION")
    return subprocess.check_output(["git", "log", "-1", path])
def unrelated_write(tmp):
    path = os.path.join(tmp, "scratch")
    os.makedirs(path, exist_ok=True)
"""


def test_fence_flags_a_new_ungated_shell_publisher():
    assert _sh_offences(_UNGATED_SH), "a direct rsync into the skill home must be caught"
    assert _sh_names_home(_UNGATED_SH)


def test_fence_follows_shell_variable_indirection():
    # The evasion that matters: name the path once, write to the variable. This is how the gated
    # installer itself is written, so a copycat would look exactly like this.
    hits = _sh_offences(_UNGATED_SH_INDIRECT)
    assert len(hits) >= 2, "both the mkdir and the rsync through $DEST must be caught: %s" % (hits,)


def test_fence_resolves_a_path_spelled_in_halves():
    # `.claude` and `skills` never appear adjacent in this script, and the write target is three
    # bindings removed from either. A literal scan sees nothing at all — both layers must still bite.
    assert _sh_names_home(_UNGATED_SH_TWO_STEP), (
        "a path composed across bindings must still count as naming the skill home")
    assert _sh_offences(_UNGATED_SH_TWO_STEP), "the rsync through the composed path must be caught"


def test_fence_flags_a_new_ungated_python_publisher():
    assert _py_offences(_UNGATED_PY), "a direct copytree into the skill home must be caught"


def test_fence_follows_python_name_indirection():
    hits = _py_offences(_UNGATED_PY_INDIRECT)
    assert len(hits) >= 2, "both the makedirs and the copytree through `dest` must be caught: %s" % (hits,)


def test_fence_sees_a_module_constant_from_inside_a_function():
    # dashboard/lib/engine.py already holds DEFAULT_INSTALL_DIR at module scope and is allow-listed,
    # so layer 2 is its only guard. A publish helper added to it would read exactly like this.
    assert _py_offences(_UNGATED_PY_MODULE_CONST), (
        "a module-scope constant must be visible to a function-scope write")


def test_fence_flags_a_path_borrowed_from_an_allow_listed_module():
    # The path is never spelled here — it is imported from a file that already resolves it, and the
    # write call is renamed on import. Both dodges must fail.
    assert _py_names_home(_UNGATED_PY_BORROWED), (
        "importing DEFAULT_INSTALL_DIR counts as naming the installed-skill home")
    assert _py_offences(_UNGATED_PY_BORROWED), "an aliased copytree must still be caught"


def test_fence_flags_a_publisher_that_shells_out():
    assert _py_offences(_UNGATED_PY_SUBPROCESS), "subprocess.run of rsync into the home must be caught"


def test_fence_does_not_flag_a_read_only_reference():
    # The other half of a useful fence: it must stay quiet on the files that legitimately name the
    # path, or it gets muted. The Python case covers the two traps that would otherwise fire — a
    # read-only `subprocess.check_output` over the installed VERSION, and the name `path` reused in
    # a second function for an unrelated write.
    assert not _sh_offences(_READ_ONLY_SH)
    assert not _py_offences(_READ_ONLY_PY)


def test_fence_classifies_an_extensionless_publisher_by_its_shebang(tmp_path):
    for shebang, expected in (("#!/usr/bin/env python3", "py"), ("#!/usr/bin/env bash", "sh")):
        script = tmp_path / ("republish-" + expected)
        script.write_text(shebang + "\n# body\n")
        assert _kind(script, script.name) == expected, (
            "a script with no extension must still be read as %s" % expected)
    plain = tmp_path / "notes.txt"
    plain.write_text("just prose about ~/.claude/skills\n")
    assert _kind(plain, plain.name) is None, "a non-script must not enter the surface"


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
