"""Permanent mechanical fence: exactly ONE module talks to the session host (issue #304).

``skill/lib/session_host.py`` is the five-verb doorway. Its value is not the code inside it — it is
the ABSENCE of any second way to reach the host. Two things rest on that absence:

1. **The host swap stays bounded.** "Replace herdr with tmux" is a rewrite of one module only for
   as long as no other file has quietly learned a herdr verb. The moment a second one has, the
   swap becomes an archaeology exercise across the engine.
2. **The distrust rules are enforced exactly once.** ``--wait`` always, rc-is-never-proof,
   process-fact liveness, verify-or-teardown, names-not-ids: each is a rule someone has to
   REMEMBER at every call site, or a rule the doorway keeps for everybody. A second caller is a
   place where "just this once" happens — and the failures those rules exist for (a 6/6 silent
   prompt drop, a hollow launch reported ready) are silent by nature, so nothing would notice.

The same shape as ``test_one_publish_door.py``, and deliberately SELF-CONTAINED for the same
reason: it re-derives the invariant from the real repo tree, so it goes red the moment a NEW caller
appears, whatever it is called and wherever it lives.

**What counts as reaching the host.** Not the WORD "herdr" — this repo's docs and comments name it
constantly and must stay free to (``docs/HERDR-ADOPTION-PLAN.md`` is cited in engine comments; the
launcher's comments discuss the host's tracker). What counts is the machinery:

* **the binary** — a literal ``herdr`` / ``…/herdr``, a ``herdr <group> …`` command line, or a
  shell ``$HERDR``-style handle to one;
* **the socket** — the control surface a caller can drive with plain JSON and no binary at all
  (which is exactly why the fence covers it, and why the env vars below count too);
* **HERDR_\\* / SL_HERDR environment** — the injected pane context and the binary override;
* **a verb pair** — ``("agent", "prompt")``, ``("workspace", "create")`` and friends spelled as
  adjacent argv elements, which is what driving the host actually looks like in code.

Python is read through ``ast`` (and matched against ``ast.unparse``'s comment-free rendering), so a
comment or a docstring discussing the host is never an offence while a string literal that IS a
command is. Shell is read comment-stripped for the same reason.

**Scanned surface.** Every tracked script in the repo — by extension (``.sh``/``.bash``/``.zsh``/
``.py``) and by shebang, because the load-bearing entry points carry no extension. ``conftest.py``
and ``test_*.py`` are exempt: the conftest must NAME ``SL_HERDR`` to neutralize it (the
"no test reaches a real external binary" ratchet), the wrapper's own tests must spell the verbs
they assert on, and this file constructs violations deliberately.

The ``test_fence_flags_*`` meta-tests build each violation class from synthetic source and assert
the fence catches it, so this guard cannot rot into a vacuously-green test — the failure mode that
makes structural guards worthless.
"""
import ast
import os
import re
import subprocess
from pathlib import Path

# tests/test_one_session_host_door.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]

_THE_DOOR = "skills/superlooper/skill/lib/session_host.py"

# Every script allowed to reach the session host, and why. There is one, and adding a second is a
# decision a human has to make in review — which is the whole mechanism.
_ALLOWED = {
    "skills/superlooper/tests/fakes/fake-sessionhost":
        "A STAND-IN FOR the host, not a second caller of it. The offline simulation needs something "
        "on the other end of the doorway that speaks the real envelope, and a fake that spoke a "
        "friendlier dialect would prove nothing about the wrapper. It is exempt for the same reason "
        "the wrapper's own tests are — it must spell the verbs it answers — and it is listed here "
        "rather than pattern-exempted so that adding a SECOND such fake is still a decision a human "
        "makes in review.",
    _THE_DOOR:
        "THE DOORWAY. The five-verb wrapper (spawn/send/state/exit/kill) where every distrust rule "
        "is enforced once: --wait always, transcript-side delivery proof, process-fact liveness, "
        "verify-or-teardown, names-not-ids. Swapping the host is a rewrite of this file alone.",
    "skills/superlooper/skill/vendor/herdr/herdr-agent-state.sh":
        "NOT OURS — the host's own state-report hook asset, carried byte-for-byte from the pinned "
        "release (issue #307; checksum pinned in skill/lib/herdr_hook.py, procedure in that "
        "directory's README). It does reach the control socket, and that is the vendor's design: "
        "it is how an agent tells its host which session id to `--resume` after a crash. It is "
        "listed here rather than rewritten because the issue's boundary is explicit — carry the "
        "invocation, never fork the script — and because a fork would be OUR code speaking the "
        "host's protocol, which is exactly what this fence exists to prevent. Nothing in the "
        "engine calls it: Claude Code does, from the per-worker settings file the launcher writes. "
        "A host swap deletes this file and the settings composer with it, so the swap stays "
        "bounded; the distrust rules are untouched because this asset drives no verb.",
}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".zsh")

# The host's command groups, read from its own CLI help. The first four are the ones a caller
# actually drives; the rest round out the surface so a `herdr <group>` command line is recognised
# whichever group it names.
_ARGV_GROUPS = ("agent", "pane", "workspace", "tab")
_ALL_GROUPS = _ARGV_GROUPS + (
    "worktree", "terminal", "notification", "integration", "session", "server", "api", "config",
    "channel", "plugin",
)
# Subcommands, likewise from the CLI's group help. Used ONLY in the adjacent-argv-pair check, so a
# generic word like "list" can never fire on its own.
_SUBCOMMANDS = frozenset("""
list get read send-keys send-text prompt rename focus wait attach start explain current layout
process-info neighbor edges resize zoom split swap move close run wait-output report-agent
report-agent-session release-agent report-metadata create
""".split())

# A literal that IS the binary: `herdr`, `/opt/homebrew/bin/herdr`, `$HOME/bin/herdr`.
_BINARY_LITERAL = re.compile(r"^(?:[\w./$~{}-]*/)?herdr$")
# A command line spelled in one string: `herdr agent prompt …`.
_COMMAND_LINE = re.compile(r"\bherdr\s+(?:%s)\b" % "|".join(_ALL_GROUPS))
# The injected pane context and the binary override. `HERDR-ADOPTION-PLAN.md` deliberately does NOT
# match (hyphen, not underscore) — engine comments cite that document by name.
_ENV_NAME = re.compile(r"\b(?:HERDR_[A-Z0-9_]+|SL_HERDR)\b")
# The control socket, in the forms a caller could name it.
_SOCKET = re.compile(r"herdr[\w.-]*\.sock\b|/herdr/[\w.-]*\.sock\b")
# A shell handle on the binary: `$HERDR`, `${HERDR_BIN}`, `"$SL_HERDR"`.
_SH_VAR = re.compile(r"\$\{?(?:SL_)?HERDR\w*\}?")
# `herdr` at a COMMAND position — start of a logical line, or after a pipe/semicolon/&&/subshell,
# with any leading VAR=value assignments skipped. Prose that merely says the word is not a command.
_SH_COMMAND = re.compile(r"(?:^|[;&|(`]|\$\()\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:command\s+)?herdr\b")
# The binary put into a variable first: `BIN=herdr`, `HOST="/opt/homebrew/bin/herdr"`. Neither that
# line nor the later `"$BIN" agent prompt` matches "herdr at a command position", so the ASSIGNMENT
# has to count — you cannot run the binary without naming it exactly once.
_SH_BINARY_ASSIGN = re.compile(r"\b[A-Za-z_]\w*=(['\"]?)(?:[\w./$~{}-]*/)?herdr\1(?:\s|;|$)")
# A command line in a string constant: `agent prompt <name>` without the binary in front.
_BARE_COMMAND = re.compile(r"^(?:%s)\s+(?:%s)\b" % ("|".join(_ARGV_GROUPS), "|".join(sorted(_SUBCOMMANDS))))


# --------------------------------------------------------------- the scanned surface

def _is_test_file(rel):
    parts = Path(rel).parts
    if "tests" not in parts:
        return False
    name = parts[-1]
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def _shebang_kind(path):
    try:
        with open(path, "rb") as f:
            first = f.readline(256).decode("utf-8", "replace")
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    return "py" if "python" in first else "sh"


def _kind(path, rel):
    if rel.endswith(".py"):
        return "py"
    if rel.endswith((".sh", ".bash", ".zsh")):
        return "sh"
    return _shebang_kind(path)


def _tracked_files():
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


# --------------------------------------------------------------- python detection

def _py_source_without_comments(text):
    """The module as ``ast.unparse`` renders it: every string literal kept, every comment gone.

    That distinction is the fence's whole tone. A comment explaining what the doorway does is
    documentation; a string literal carrying `herdr agent prompt` is a call waiting to happen.
    """
    try:
        return ast.unparse(ast.parse(text))
    except (SyntaxError, ValueError):
        return text                      # unparseable: scan it raw rather than skip it


def _py_string_bindings(tree):
    """``{name: "literal"}`` for every plain string binding in the module.

    The cheapest way around an adjacent-constants check is to put the group in one variable and the
    subcommand in another, so the argv list holds no strings at all. Resolving bindings before
    pairing closes that, the same way the publish fence resolves a path spelled in halves. Scope is
    deliberately ignored: this is a fence, and a name that means "prompt" in one function and
    something innocent in another is not a shape worth protecting.
    """
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
            if not isinstance(node.value.value, str):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _py_word(element, bindings):
    """The string an argv element carries — spelled, or reached through a name."""
    if isinstance(element, ast.Constant) and isinstance(element.value, str):
        return element.value
    if isinstance(element, ast.Name):
        return bindings.get(element.id)
    return None


def _py_offences(text):
    hits = []
    stripped = _py_source_without_comments(text)
    # Substring rules first, over the comment-free rendering. The binary is NOT here: it is matched
    # as a whole literal in the AST pass below, so the word `herdr` inside a sentence stays prose.
    for regex, why in ((_COMMAND_LINE, "spells a host command line"),
                       (_ENV_NAME, "names the host's environment"),
                       (_SOCKET, "names the host control socket")):
        found = regex.search(stripped)
        if found:
            hits.append((0, "%s: %s" % (why, found.group(0))))
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return hits
    bindings = _py_string_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if _BINARY_LITERAL.match(value) or _BARE_COMMAND.match(value):
                hits.append((getattr(node, "lineno", 0), "host command literal: %r" % value[:60]))
        elif isinstance(node, (ast.List, ast.Tuple)):
            words = [_py_word(e, bindings) for e in node.elts]
            for first, second in zip(words, words[1:]):
                if first in _ARGV_GROUPS and second in _SUBCOMMANDS:
                    hits.append((getattr(node, "lineno", 0),
                                 "host argv pair: %s %s" % (first, second)))
    return hits


# --------------------------------------------------------------- shell detection

def _strip_sh_comment(line):
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
    hits = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_sh_comment(raw)
        if not line.strip():
            continue
        for regex, why in ((_SH_COMMAND, "runs the host binary"),
                           (_SH_BINARY_ASSIGN, "puts the host binary in a variable"),
                           (_SH_VAR, "holds a handle on the host binary"),
                           (_ENV_NAME, "names the host's environment"),
                           (_SOCKET, "names the host control socket")):
            found = regex.search(line)
            if found:
                hits.append((n, "%s: %s" % (why, line.strip()[:100])))
                break
    return hits


def _offences(kind, text):
    return _py_offences(text) if kind == "py" else _sh_offences(text)


# --------------------------------------------------------------- the fence

def test_only_the_wrapper_reaches_the_session_host():
    offenders = {}
    for rel, kind, text in _surface():
        if rel in _ALLOWED:
            continue
        hits = _offences(kind, text)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "these scripts reach the session host directly, but %s is the only doorway: %s\n"
        "Call the wrapper's five verbs (spawn/send/state/exit/kill) instead. If a new file "
        "genuinely has to hold the host itself, it has to be added to _ALLOWED here with a reason "
        "— which is the point: that is a decision a human makes in review."
        % (_THE_DOOR, offenders)
    )


def test_the_doorway_still_exists_and_is_still_five_verbs_wide():
    # A fence whose door has been deleted or widened is worse than no fence: it passes.
    import session_host
    public = {n for n in vars(session_host.SessionHost)
              if not n.startswith("_") and callable(getattr(session_host.SessionHost, n))}
    assert public == {"spawn", "send", "state", "exit", "kill"}
    source = (_REPO / _THE_DOOR).read_text(encoding="utf-8")
    assert "--wait" in source, "the doorway must still carry the --wait rule"


def test_the_fence_would_flag_the_doorway_itself():
    # The strongest anti-rot check available, and the one synthetic sources cannot give: run the
    # detector over the REAL doorway, which drives the host on every line the fence cares about. If
    # this ever comes back thin, the detector has stopped recognising the machinery it exists to
    # find — and the sweep above is passing for the wrong reason.
    hits = _py_offences((_REPO / _THE_DOOR).read_text(encoding="utf-8"))
    kinds = {hit[1].split(":")[0] for hit in hits}
    assert "host argv pair" in kinds and "host command literal" in kinds, (
        "the fence no longer recognises the doorway's own host calls: %s" % (hits[:5],))


def test_the_allow_list_has_not_rotted():
    missing = [rel for rel in _ALLOWED if not (_REPO / rel).is_file()]
    assert not missing, "allow-listed files no longer exist (rename or delete the entry): %s" % missing
    reasonless = [rel for rel, why in _ALLOWED.items() if len(why.strip()) < 40]
    assert not reasonless, "every allow-list entry needs a real reason: %s" % reasonless


def test_the_test_ratchet_keeps_the_real_binary_out_of_reach():
    # The other half of "one doorway": the door must not open onto the owner's live fleet during a
    # test run. conftest points SL_HERDR at a path that cannot exist.
    assert not os.path.exists(os.environ["SL_HERDR"])


# --------------------------------------------------------------- meta-tests: the fence bites

_PY_ARGV = """import subprocess
def nudge(name, text):
    subprocess.run(["herdr", "agent", "prompt", name, text])
"""

_PY_ARGV_VIA_VARIABLE = """import subprocess
def nudge(binary, name, text):
    subprocess.run([binary, "agent", "prompt", name, text, "--wait"])
"""

_PY_COMMAND_STRING = """import os
def nudge(name):
    os.system("herdr agent send-keys %s enter" % name)
"""

_PY_SOCKET = """import socket, os
def drive(payload):
    s = socket.socket(socket.AF_UNIX)
    s.connect(os.path.expanduser("~/.config/herdr/herdr.sock"))
    s.sendall(payload)
"""

_PY_ENV = """import os
def pane():
    return os.environ.get("HERDR_PANE_ID")
"""

_SH_DIRECT = """#!/usr/bin/env bash
herdr agent prompt "$ID" "$BRIEF" --wait --timeout 120000
"""

_SH_VIA_VARIABLE = """#!/usr/bin/env bash
HERDR="${SL_HERDR:-herdr}"
"$HERDR" workspace close "$WS"
"""

_SH_PIPED = """#!/usr/bin/env bash
echo hi | herdr pane send-text "$PANE"
"""

_PY_VERBS_VIA_NAMES = """import subprocess
GROUP = "agent"
VERB = "prompt"
def nudge(cfg, name, text):
    subprocess.run([cfg["host_binary"], GROUP, VERB, name, text, "--wait"])
"""

_SH_BINARY_IN_A_VARIABLE = """#!/usr/bin/env bash
BIN=herdr
"$BIN" agent prompt "$ID" "$BRIEF" --wait
"""

_PY_PROSE = '''"""The session host is herdr today (docs/HERDR-ADOPTION-PLAN.md).

herdr is muscle, never truth — its idle state means "seen in the focused UI", so the runner reads
its own hooks instead. See the wrapper for the agent read rules.
"""
# a comment mentioning herdr agent prompt --wait, and pane close, for the reader
def decide(state):
    return "park" if state == "unknown" else "go"
'''

_SH_PROSE = """#!/usr/bin/env bash
# The launch shim predates herdr; herdr's tracker carries the open issue (see HERDR-ADOPTION-PLAN).
# Under herdr the anchor concept disappears and `agent start` moves behind the wrapper.
echo "launching $ID"
"""

_PY_GIT_WORKTREE = """import subprocess
def worktrees(repo):
    return subprocess.run(["git", "-C", repo, "worktree", "list"], capture_output=True)
"""


def test_fence_flags_a_python_caller_that_builds_host_argv():
    assert _py_offences(_PY_ARGV), "an argv list driving the host must be caught"
    assert _py_offences(_PY_ARGV_VIA_VARIABLE), (
        "hiding the binary in a variable must not shed the check — the verb pair is still there")


def test_fence_flags_a_command_spelled_in_one_string():
    assert _py_offences(_PY_COMMAND_STRING)


def test_fence_follows_verbs_bound_to_names():
    # The determined-caller case (fresh-agent review): put the group and the subcommand in
    # constants and the binary in config, and nothing on the line reads as a host call. Bindings
    # are resolved before pairing, exactly as the publish fence resolves a path spelled in halves.
    assert _py_offences(_PY_VERBS_VIA_NAMES), "a verb pair reached through names is still a call"


def test_fence_flags_a_shell_caller_that_hides_the_binary_in_a_variable():
    # `BIN=herdr` then `"$BIN" agent prompt …`: neither line matches "herdr at a command position",
    # so the assignment ITSELF has to count. You cannot run the binary without naming it once.
    assert _sh_offences(_SH_BINARY_IN_A_VARIABLE)


def test_fence_flags_the_socket_and_the_injected_environment():
    # The socket is the real fence-breaker: the protocol is plain JSON, so a caller needs no binary
    # at all. And HERDR_PANE_ID is how a pane learns where it lives.
    assert _py_offences(_PY_SOCKET)
    assert _py_offences(_PY_ENV)


def test_fence_flags_a_shell_caller_however_it_spells_the_binary():
    assert _sh_offences(_SH_DIRECT), "a direct call must be caught"
    assert _sh_offences(_SH_VIA_VARIABLE), "a call through $HERDR must be caught"
    assert _sh_offences(_SH_PIPED), "a call after a pipe is still a call"


def test_fence_stays_quiet_on_prose_about_the_host():
    # The half that keeps the fence usable. The adoption plan is cited all over this engine, and a
    # fence that reddened CI over a comment would be muted within a week.
    assert not _py_offences(_PY_PROSE), "docstrings and comments about the host are not calls"
    assert not _sh_offences(_SH_PROSE), "shell comments about the host are not calls"


def test_fence_does_not_mistake_another_tool_for_the_host():
    # `worktree list` is a herdr pair AND a git one. The pair check is scoped to the four groups a
    # caller actually drives, so git's own verbs stay out of it.
    assert not _py_offences(_PY_GIT_WORKTREE)


def test_fence_reads_extensionless_scripts_by_their_shebang(tmp_path):
    for shebang, expected in (("#!/usr/bin/env python3", "py"), ("#!/usr/bin/env bash", "sh")):
        script = tmp_path / ("drive-" + expected)
        script.write_text(shebang + "\n# body\n")
        assert _kind(script, script.name) == expected
    plain = tmp_path / "notes.txt"
    plain.write_text("prose about herdr agent prompt\n")
    assert _kind(plain, plain.name) is None, "a non-script must not enter the surface"


def test_the_engines_entry_points_are_on_the_scanned_surface():
    scanned = {rel for rel, _kind_, _text in _surface()}
    for rel in ("skills/superlooper/skill/bin/superlooper",
                "skills/superlooper/skill/bin/runner.py",
                "skills/superlooper/skill/bin/launch-session.py",
                "dashboard/bin/command-center"):
        assert rel in scanned, "%s must be scanned (it is exactly where a second door would go)" % rel
