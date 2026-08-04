"""The per-worker hook config that gives the session host its session-id capture (issue #307).

The host resumes a crashed agent by re-running `claude --resume <id>`, and the ONLY way it learns
that id is a state-report hook the agent fires at session start. Its own installer
(`integration install claude`) puts that hook in the machine's GLOBAL Claude settings file. This
module puts the same hook in a file that belongs to ONE worker instead, which the launcher hands
to that worker alone with `--settings`. The operator's `~/.claude/settings.json` is never opened,
never read and never written by anything in this engine or in the host.

**Measured, not assumed** (lab drill, reports/i307.md, pinned host build, isolated socket):

* a session started with this file — and nothing else changed — was reported by the host as
  `agent_session {kind: "id", source: "herdr:claude", value: <the runner's own minted id>}`;
* the CONTROL, an identical command line with the `--settings` flag removed, reported no session
  at all. So the capture is the hook, not the host reading `--session-id` out of argv;
* after `kill -9` of the host and a headless restart with no client ever attached, the hooked lane
  came back as `claude --resume <that same id>` and still knew a codeword from before the crash,
  while the control lane came back as a bare shell;
* the operator's global settings file was byte-identical (sha256) throughout.

**The pin.** Every constant below is a contract with a THIRD-PARTY release, read out of that
release's own source (`src/integration/claude_settings.rs` installs one `SessionStart` hook with
matcher `*` and timeout 10; `src/integration/command.rs` spells the command `bash '<path>' session`
and quotes the path exactly as `_quote` does here). The asset itself is carried verbatim in
`vendor/herdr/` and checksummed — see that directory's README for the version-bump procedure.
Nothing here is a re-implementation of the hook: the SCRIPT is the vendor's, byte for byte, and
this module only chooses where it lives and writes the two lines that invoke it.

**What this module deliberately does not do.** It does not run, or offer to run, the vendor's
installer; it does not write to a global settings file on any path; and it holds no knowledge of
the control socket or the report protocol — that all lives inside the carried asset, where the
vendor maintains it.
"""
import json
import hashlib
import os
import re
import sys

# --------------------------------------------------------------------------- the pin
# The upstream release these constants were read from. A bump re-runs the acceptance in
# vendor/herdr/README.md; it is never a silent adaptation (issue #307 boundary).
PINNED_VERSION = "0.8.0"

# The vendor's own integration-contract number, stamped inside the asset. Independent of the
# checksum on purpose: a checksum says "the file changed", this says "the contract changed".
INTEGRATION_VERSION = 7

# sha256 of the carried asset, verified against BOTH the release tag's tree and the raw file.
HOOK_SCRIPT_SHA256 = "ffd5a76b7c62f5313040fc1e98fa010ff19a7aa85dd9fe6f325b9729d5f01b46"

# Where the asset lives inside the publishable payload, and what the vendor names it. Spelled as
# whole path strings rather than os.path.join'd components on purpose: a bare "herdr" literal is
# how a file names the host BINARY, and tests/test_one_session_host_door.py rightly reads one as a
# call waiting to happen. This module writes a JSON file and drives nothing, so it should not be
# on that fence's allowlist — the honest fix is not to spell the binary.
SCRIPT_NAME = "herdr-agent-state.sh"
_VENDOR_REL = "vendor/herdr/" + SCRIPT_NAME

# The registration, exactly as the pinned release writes it. ONE event: the release actively
# REMOVES the older ones (Stop/PreToolUse/UserPromptSubmit/…) when it installs, so carrying more
# than this would be carrying a contract the vendor has retired.
HOOK_EVENT = "SessionStart"
HOOK_MATCHER = "*"
HOOK_ACTION = "session"
HOOK_TIMEOUT = 10

# Every action word the vendor has ever registered this script under, newest first. Used ONLY to
# RECOGNISE a hook somebody else installed — the doctor's question is "was the installer ever run
# on this machine", and a stale registration from an older release is just as much a yes.
_KNOWN_ACTIONS = (HOOK_ACTION, "idle", "working", "blocked", "release")

# `…/herdr-agent-state.sh <action>` at the END of a command line, with the script name anchored to
# a path separator or an opening quote. `.ps1` is included even though this loop is macOS-only: the
# vendor's Windows install name differs, and a machine carrying THAT registration is just as much a
# machine the installer was run on — a miss here would clear it wrongly.
# The boundary class carries a BACKSLASH as well as `/`: the Windows registration is
# `-File "C:\…\herdr-agent-state.ps1" session`, and a POSIX-only separator would miss it.
_HOOK_COMMAND_RE = re.compile(
    r"""(?:^|[/\\'"\s])%s\.(?:sh|ps1)['"]?\s+(?:%s)$"""
    % (re.escape(SCRIPT_NAME.rsplit(".", 1)[0]), "|".join(_KNOWN_ACTIONS)))


class HookConfigError(Exception):
    """The per-worker hook config could not be written, and no session should launch believing it
    was. Every failure here is raised rather than returned: a launcher that carried on would
    produce a session that runs perfectly and is invisible to the host's revive machinery."""


# --------------------------------------------------------------------------- the command line

def _quote(value):
    """The vendor's `shell_single_quote`: wrap in single quotes, rewrite an inner quote as '"'"'.

    Reproduced rather than imported because it is three characters of shell grammar and the
    alternative is shelling out to the host binary to ask. A Mac path really can contain an
    apostrophe (`/Users/o'brien`), and a naive quote there yields a hook command line that is a
    shell syntax error — which fails at every session start, silently, in a subprocess nobody
    watches."""
    return "'%s'" % value.replace("'", "'\"'\"'")


def hook_command(script_path):
    """The command line the settings entry carries: `bash '<script>' session`."""
    if not script_path:
        raise ValueError("the hook script path is required to build the hook command")
    return "bash %s %s" % (_quote(str(script_path)), HOOK_ACTION)


# --------------------------------------------------------------------------- the document

def settings_document(script_path):
    """The whole per-worker settings file, as a dict.

    HOOKS AND NOTHING ELSE. Claude Code merges this over the operator's own settings, so any other
    key here would silently override their choice — model, permissions, environment — for every
    worker on the machine. This file exists to ADD one hook; that is the entire mandate.
    """
    return {"hooks": {HOOK_EVENT: [{"matcher": HOOK_MATCHER, "hooks": [
        {"type": "command", "command": hook_command(script_path), "timeout": HOOK_TIMEOUT}]}]}}


def write_settings(path, script_path):
    """Write the per-worker settings file at ``path``; return the path written.

    Refuses when the hook script is not on disk. That refusal is the point: a settings file naming
    a script that is not there produces a session which starts, works, and is never resumable —
    the exact half-working state that is invisible until the machine reboots and a flight is lost.
    """
    script_path = str(script_path)
    if not os.path.isfile(script_path):
        raise HookConfigError(
            "the session host's state-report hook is not installed at %s — publish the engine "
            "(bin/install.sh) so the vendored asset lands on this machine" % script_path)
    path = str(path)
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise HookConfigError("could not create %s for the per-worker hook config: %s"
                              % (parent, exc))
    body = json.dumps(settings_document(script_path), indent=2) + "\n"
    # tmp+rename, like every other config write in this engine: a launcher reading this file
    # concurrently must never see a half-written document, and a failed write must leave the
    # previous one intact rather than a truncated one.
    #
    # O_EXCL and a 0600 CREATION mode rather than open()+chmod (fresh-agent review, P2). The temp
    # name is predictable, so a plain open() would follow a symlink another same-uid process had
    # already planted there — writing this document, which names a command the agent executes at
    # every session start, wherever that link pointed. O_EXCL refuses an existing path outright,
    # and the mode is set by the CREATE, so there is no window where the file exists readable.
    tmp = os.path.join(parent, ".%s.tmp.%d" % (os.path.basename(path), os.getpid()))
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise HookConfigError("could not write the per-worker hook config %s: %s" % (path, exc))
    return path


# --------------------------------------------------------------------------- the asset

def vendored_script(home=None):
    """Absolute path to the carried asset.

    ``home`` is the directory that holds ``vendor/`` — ONE meaning for both layouts, which is the
    point: in a checkout that is the payload's ``skill/``, and on a machine it is the installed
    engine home, because the publish rsyncs ``skill/``'s CONTENTS rather than the directory. (That
    home is spelled in exactly one place, ``stack_doctor._installed_home``; naming it here would
    make this a second file that resolves a path into the install tree, which
    ``tests/test_one_publish_door.py`` rightly refuses.) Default is derived from this file, so the
    installed copy finds the installed asset without any caller knowing which layout it is in.
    """
    if home is None:
        home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))    # …/lib -> …
    return os.path.normpath(os.path.join(str(home), _VENDOR_REL))


def script_checksum(path):
    """sha256 of the asset, or None when it cannot be read (never an exception: the doctor asks
    this about a machine that may not have published yet)."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def script_is_pinned(path):
    return script_checksum(path) == HOOK_SCRIPT_SHA256


# --------------------------------------------------------------------------- detection

def global_settings_path(env=None):
    """The settings file the VENDOR'S INSTALLER would have written on this machine.

    Its `claude_dir()` is `$CLAUDE_CONFIG_DIR` when set and non-empty, else `$HOME/.claude` — so
    the doctor asks about that same file. Resolving it any other way would clear a machine whose
    real settings file was never looked at.
    """
    env = os.environ if env is None else env
    override = env.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(_expand_home(override, env), "settings.json")
    return os.path.join(_expand_home("~", env), ".claude", "settings.json")


def _expand_home(path, env):
    """``~`` expanded against THIS env's HOME, never the process's (fresh-agent review, P1).

    ``os.path.expanduser`` reads ``os.environ``, so an injected env asking about ``~/.claude``
    resolved to the REAL operator's home — which both answers about the wrong machine and lets a
    test with an isolated env read the operator's own settings file. The vendor's own
    ``expand_tilde_path`` expands from its process HOME for the same reason: the answer must be
    about the environment being asked about.
    """
    home = env.get("HOME") or os.path.expanduser("~")
    if path == "~":
        return home
    if path.startswith("~/"):
        return os.path.join(home, path[2:])
    return path                       # a bare `~user` is not ours to resolve; leave it verbatim


def _is_carried_hook(command):
    """Does this command line invoke the vendor's state-report script?

    Matched on the script's FILE NAME at a path boundary, plus one of the action words — not on a
    whole command line, because the directory in front of it is whichever config dir that machine's
    installer resolved, and an older release used a different action word for the same script.

    The boundary is what keeps it honest (fresh-agent review, P2). A bare substring test called
    ``bash '/x/not-herdr-agent-state.sh' session`` a herdr install, and told an operator to go undo
    something nobody had done. Requiring `/` or a quote in front — which the vendor's own
    `<dir>/<name>` command line always has — costs no real detection and ends that class.
    """
    if not isinstance(command, str):
        return False
    return bool(_HOOK_COMMAND_RE.search(command.rstrip()))


def carried_hook_commands(document):
    """Every command in ``document``'s hooks that invokes the vendor's state-report script.

    TOLERANT BY DESIGN. This reads settings files this engine does not own, so every wrong-typed
    node is skipped rather than raised on — a doctor that crashed on somebody's hand-edited
    settings would report nothing about the thing it was asked to check.
    """
    found = []
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # `type` must say command. Claude Code only ever executes a command hook, so an
                # entry of some other type carrying a matching string is not something the vendor's
                # installer wrote (fresh-agent review, P2). Absent `type` is still read as a
                # command: that is how Claude Code itself treats it, and a doctor that missed a
                # real registration over a missing default would be the worse failure.
                if entry.get("type", "command") != "command":
                    continue
                if _is_carried_hook(entry.get("command")):
                    found.append(entry["command"])
    return found


def carried_hook_commands_in_file(path):
    """Same question, asked of a file. Unreadable / unparseable reads as "no hook" — an answer the
    caller must not confuse with proof, and the doctor says so in its own detail line."""
    try:
        with open(path) as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return []
    return carried_hook_commands(document)


# --------------------------------------------------------------------------- the launcher edge

def main(argv=None):
    """`python3 herdr_hook.py <settings-path> [<script-path>]` -> prints the path it wrote.

    The launcher owns the agent's command line and nothing else; JSON assembly lives here so the
    shell never hand-builds a settings document, and so the rendering the tests cover is literally
    the rendering a launch runs.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: herdr_hook.py <settings-path> [<hook-script-path>]", file=sys.stderr)
        return 64
    script = argv[1] if len(argv) > 1 else vendored_script()
    try:
        print(write_settings(argv[0], script))
    except HookConfigError as exc:
        print("%s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
