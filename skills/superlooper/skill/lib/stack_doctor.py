"""Machine-level stack doctor checks for `superlooper doctor --stack`.

The repo-level doctor in the CLI validates one adopted repository. This module validates the
ambient machine blocks the loop depends on before it can run reliably overnight. Every external
edge is behind Probe so tests can inject fake command resolution, command output, file reads, and
environment without reaching real binaries or the network.
"""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass

import config as config_lib
import herdr_hook
import notify
import ops_docs
import runner_home
import session_host


_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"
# cmux's CFBundleIdentifier — the macOS user-defaults domain App Nap reads NSAppSleepDisabled from
# (verified from /Applications/cmux.app/Contents/Info.plist). Overridable via SL_CMUX_BUNDLE_ID for
# a differently-signed build or a test. See check_cmux_app_nap.
_CMUX_BUNDLE_ID = "com.cmuxterm.app"
_APP_NAP_TRUE = {"1", "yes", "true", "on"}
_APP_NAP_FALSE = {"0", "no", "false", "off"}
GH_MIN_REMAINING = 500

# How long a control-socket probe waits (issue #331). A doctor that hangs on a wedged socket is a
# doctor nobody runs, and a host that needs longer than this to refuse a one-line request has
# already told us something.
_SOCKET_PROBE_SECONDS = 5.0

# The one message the doctor actually sends to prove the channel. Static (no clock) so the check
# is deterministic and the owner learns to recognize it. Reads as an explanation on arrival.
NOTIFY_TEST_TITLE = "superlooper doctor: notify channel test"
NOTIFY_TEST_BODY = (
    "doctor --stack sent this to prove your notify channel delivers. "
    "Receiving it means overnight stall alerts can reach you here."
)


class _SkipSend:
    """The sentinel a READ-ONLY caller passes as `sender` (issue #200).

    ``check_notify`` proves the channel by SENDING — that is the whole point of the block, and
    ``doctor --stack`` owns that one deliberate side effect. But ``superlooper upkeep`` runs the
    same stack checks under a read-only contract, and it must not push a message every time the
    owner glances at the weekly report. A no-op callable is NOT a safe substitute: it returns
    something without an `.ok`, which check_notify would render as a FAILED send — a false red on a
    healthy channel. So the skip is explicit and the block says what it did and did not prove.

    A class with a repr rather than ``object()`` so a stray sentinel in a traceback or a log line
    reads as itself.
    """

    def __repr__(self):
        return "stack_doctor.SKIP_SEND"


SKIP_SEND = _SkipSend()


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    # An advisory block: printed as WARN, does NOT fail the stack. A warn result always carries
    # ok=True (it passes), so `not r.ok` — the failure test everywhere — never counts it. Used when
    # a tool is only conditionally needed on THIS machine (see check_codex / issue #30).
    warn: bool = False


class Probe:
    def __init__(self, env=None):
        self.env = env if env is not None else os.environ
        self.home = self.env.get("HOME") or os.path.expanduser("~")

    def command(self, name, envvar=None, default=None):
        override = self.env.get(envvar) if envvar else None
        if override:
            return override
        # Search THIS probe's PATH, not the process's. They are the same object in production
        # (env defaults to os.environ), so nothing changes there — but a caller that hands the probe
        # an isolated env used to have its lookups quietly resolve against the host's real PATH,
        # which both contradicts the env it passed and is a hole in the "no test reaches a real
        # external binary" ratchet (fresh-agent review, P1). A None here keeps shutil's own
        # os.environ default, so an env without PATH behaves exactly as before.
        found = shutil.which(name, path=self.env.get("PATH"))
        if found:
            return found
        if default and os.path.exists(default):
            return default
        return None

    def run(self, argv, timeout=10):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", "")
        except (OSError, ValueError):
            return subprocess.CompletedProcess(argv, 127, "", "")

    def exists(self, path):
        return os.path.exists(path)

    def executable(self, path):
        """Can this path actually be RUN? `exists` is not enough for a binary pin (issue #303):
        the shell ladder in start-session.sh tests `-x`, and a doctor that tested only presence
        would pass a pin the launcher is about to refuse."""
        return os.path.isfile(path) and os.access(path, os.X_OK)

    def read_head(self, path, limit=4096):
        """The first `limit` BYTES of a file, decoded leniently — never the whole file.

        The bound is the point. The one caller sniffs a candidate `claude` for a wrapper marker,
        and the standalone build is a ~270MB single binary: `read_text` on it would pull a quarter
        of a gigabyte through memory on every doctor run. Binary content decodes with replacement
        rather than raising, so an unreadable head is 'no marker', never an exception."""
        try:
            with open(path, "rb") as f:
                return f.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

    def read_text(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def expanduser(self, path):
        return os.path.expanduser(path)

    def pid_alive(self, pid):
        """Is `pid` a live process? Signal 0 probes without delivering. A pid we may not signal
        (EPERM) still exists, so it counts as alive. Injected in tests to avoid a real os.kill."""
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True


def _out(proc):
    return ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).strip()


def _json(proc):
    try:
        return json.loads(getattr(proc, "stdout", "") or "{}")
    except (TypeError, ValueError):
        return {}


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _zshrc_path(probe):
    zdotdir = getattr(probe, "env", {}).get("ZDOTDIR") if hasattr(probe, "env") else None
    if _nonempty_string(zdotdir):
        return os.path.join(zdotdir, ".zshrc")
    return os.path.join(probe.home, ".zshrc")


def _codex_required(config):
    """Whether THIS machine actually needs Codex. True only when a repo's config selects the Codex
    coding agent (`agent: codex`) — i.e. worker sessions launch through Codex. Codex is NOT required
    merely to review: `/cross-review` (a Codex second opinion) is the default fresh-agent review,
    but an independent same-model fresh subagent is an equally valid review path (owner ruling
    2026-07-10, issue #30), so a Claude-only machine reaches an all-green stack without Codex.
    Absence is therefore a WARN unless this returns True. Tolerant of a None/wrong-typed config
    (an unreadable config never forces the requirement).

    Scope: this reads the repo's CONFIG agent only. A one-off `superlooper run --agent codex` that
    overrides a claude-default config is out of scope for this preflight (the doctor takes no
    `--agent`); that run fails loudly at launch if Codex is missing, so nothing is silently lost."""
    cfg = config if isinstance(config, dict) else {}
    return cfg.get("agent") == "codex"


def check_codex(probe, required=False):
    """`required` (see _codex_required) decides the severity of a missing/unauthenticated Codex: a
    hard FAIL when this machine launches Codex, otherwise a WARN that leaves the stack green. When
    it is a WARN the whole story rides in `detail` (format_results only prints `fix` for a FAIL), so
    the advisory names the same-model-subagent review path and when you would actually need Codex."""
    codex = probe.command("codex", envvar="SL_CODEX")
    if not codex:
        if required:
            return CheckResult(
                "codex CLI", False, "codex not found",
                "Install the Codex CLI, then run `codex login`.",
            )
        return CheckResult(
            "codex CLI", True,
            "codex not found — not needed by this machine's config (agent is not codex); a "
            "Claude-only stack satisfies the fresh-agent review with an independent same-model "
            "subagent. Install the Codex CLI and run `codex login` only if you switch a repo to "
            "--agent codex.",
            warn=True,
        )
    proc = probe.run([codex, "login", "status"], timeout=10)
    if getattr(proc, "returncode", 1) == 0:
        detail = _out(proc) or codex
        return CheckResult("codex CLI", True, detail)
    if required:
        return CheckResult(
            "codex CLI", False, _out(proc) or "not authenticated",
            "Run `codex login` and confirm `codex login status` succeeds.",
        )
    detail = _out(proc) or "codex present but not authenticated"
    return CheckResult(
        "codex CLI", True,
        detail + " — not needed unless a repo runs --agent codex; run `codex login` if you plan "
        "to use it.",
        warn=True,
    )


def check_cmux(probe):
    env = getattr(probe, "env", {})
    cmux = env.get("SL_CMUX") or _CMUX_DEFAULT
    if cmux and probe.exists(cmux):
        return CheckResult("cmux present", True, cmux)
    detail = cmux or "cmux not found"
    return CheckResult(
        "cmux present", False, detail,
        "Install cmux, or set SL_CMUX to the cmux binary used by the runner.",
    )


# --- which `claude` the launch stack actually runs (issue #303) --------------------------------
# The tool-dive gap-fill (docs/TOOL-DIVE-2026-07-28.md, 2026-07-29) found a machine whose only
# `claude` was cmux's BUNDLED wrapper. That wrapper contains no Claude Code: it walks PATH for some
# OTHER claude and execs it, injecting cmux's own hooks on the way. Two consequences the stack was
# blind to, and this block exists for:
#   * retire cmux and every launch path loses its binary at the exact moment of migration — the
#     sequencing gate docs/HERDR-ADOPTION-PLAN.md §6 puts BEFORE any cmux removal;
#   * until then, WHICH claude a worker tab runs is decided by PATH order, which no one configured
#     and nothing reports. That is the "PATH luck" this ladder replaces.
#
# THE LADDER. start-session.sh (the agent-boundary file that owns the claude command line) resolves
# the binary by exactly these three steps, and `resolve_claude` below is its Python TWIN — the
# doctor must judge the same binary the launcher will run, or its verdict is about a different
# process. The two are deliberately duplicated rather than shared: start-session.sh runs in a fresh
# tab shell that inherits nothing and knows no engine paths (the same reasoning that duplicates the
# gh probe there), so the agreement is pinned by a test that drives BOTH, not by an import.
#   1. SL_CLAUDE — an explicit operator pin. FAILS CLOSED: a pin naming something unrunnable is
#      refused, never quietly downgraded to PATH, because a silent fallback restores the exact luck
#      the pin was set to remove, at the moment the operator most believes it is pinned.
#   2. ~/.local/bin/claude — Claude Code's standalone native install (a symlink into
#      ~/.local/share/claude/versions/<v>, maintained across upgrades by `claude install`). This is
#      the DEFAULT pin and it is cmux-independent by construction: no PATH entry participates.
#   3. PATH — the last-resort fallback, so a machine with a claude installed some other way still
#      launches. It works, but nothing pinned it, so the block WARNs.
CLAUDE_STANDALONE_REL = os.path.join(".local", "bin", "claude")
# Where `claude install` unpacks the native build. Step 2's symlink points inside it, and a resolved
# path that already lives here is the standalone build reached by another name.
_CLAUDE_STANDALONE_PAYLOAD = os.path.join(".local", "share", "claude", "versions")
# cmux ships its wrapper inside the app bundle; the marker is the wrapper's own header comment
# (verified against /Applications/cmux.app/Contents/Resources/bin/claude, 2026-08-03). The path test
# is the cheap one and the marker backs it up for a wrapper copied out of the bundle.
_CMUX_BUNDLE_MARK = "cmux.app/"
_CMUX_SHIM_MARKER = "cmux claude wrapper"
# What the last REAL worker launch resolved, stamped by start-session.sh. The doctor resolves in the
# OPERATOR's environment; a worker resolves in its own fresh tab's. That is the same gap #299/#301
# were written for, so the stamp — not this process's re-resolution — is what proves what ran.
CLAUDE_BIN_RECORD_REL = os.path.join(".superlooper", "claude-bin.last")


def resolve_claude(probe):
    """Which `claude` the launch stack will run, and why — the twin of start-session.sh's ladder.

    Returns {"path": str|None, "source": "pin"|"standalone"|"PATH"|None, "ok": bool,
    "reason": "relative"|"unrunnable"|"absent"|None}. `ok` is False only for a pin the launcher
    would refuse (relative, or naming nothing runnable) or for a machine with no claude at all;
    `reason` says which, and the caller decides severity."""
    env = getattr(probe, "env", {}) or {}
    pin = env.get("SL_CLAUDE")
    # `pin != ""`, NOT _nonempty_string: the shell twin tests `[ -n "$SL_CLAUDE" ]`, which is true
    # for a whitespace-only value. Using the strip-based helper here would make a pin of "   "
    # REFUSE the launch while the doctor quietly reported on the standalone install instead — a
    # divergence in the one direction that matters, since it hides a launcher that cannot start.
    if isinstance(pin, str) and pin != "":
        # A RELATIVE pin resolves against the CWD, and this process's cwd is not the worker's (it
        # has already cd-ed into its worktree), so `SL_CLAUDE=./claude` would have this block
        # validate one file while a launch ran — or failed to find — a different one. The launcher
        # refuses it outright; so does this.
        if not os.path.isabs(pin):
            return {"path": pin, "source": "pin", "ok": False, "reason": "relative"}
        ok = bool(probe.executable(pin))
        return {"path": pin, "source": "pin", "ok": ok,
                "reason": None if ok else "unrunnable"}
    # probe.home is $HOME with a passwd-entry fallback — and the shell twin now spells this rung as
    # an UNQUOTED `~`, which bash expands from the passwd entry on an unset HOME and to "" on an
    # empty one, matching os.path.expanduser in both. So the two agree on all three states rather
    # than only on the ordinary one.
    standalone = os.path.join(probe.home, CLAUDE_STANDALONE_REL)
    if probe.executable(standalone):
        return {"path": standalone, "source": "standalone", "ok": True, "reason": None}
    # No envvar here: the pin was already consulted above, and passing it again would let a broken
    # pin fall through to PATH — the fallback this ladder exists to refuse. shutil.which finds an
    # executable FILE only, which is why the shell rung is `type -P` and not `command -v` (the
    # latter answers with the bare name for a shell function, which no doctor could ever see).
    found = probe.command("claude")
    if found:
        # Absolute only, the ladder's ONE invariant. A PATH with an empty element (`PATH=":/usr/bin"`)
        # or a literal `.` means the CURRENT DIRECTORY, and the worker's is its worktree — so a
        # `claude` file checked into a repo could be launched as the agent. The two ladders do not
        # even agree on the string there (bash `type -P` says `./claude`, shutil.which says
        # `claude`), which is why neither is allowed to accept one.
        ok = os.path.isabs(found)
        return {"path": found, "source": "PATH", "ok": ok,
                "reason": None if ok else "relative"}
    return {"path": None, "source": None, "ok": False, "reason": "absent"}


def classify_claude(probe, path):
    """`standalone`, `cmux-shim`, or `other` for a resolved claude path.

    Ordered so the standalone build is decided on PATH ALONE — it is a ~270MB single binary, and
    only the cheap string tests ever touch it. The content sniff is reached only for a candidate
    that is neither the standalone install nor inside a cmux bundle, i.e. a small wrapper script."""
    if not _nonempty_string(path):
        return "other"
    # NORMALIZED before comparing. The shell builds its path by concatenation, so a HOME with a
    # trailing slash stamps `/home//​.local/bin/claude` while os.path.join here yields the
    # single-slash form — the same file, which would otherwise classify as `standalone` live and
    # `other` from the stamp, printing "resolved standalone; last launch ran other" about one
    # binary (third review round).
    norm = os.path.normpath(path)
    home = os.path.normpath(probe.home)
    if (norm == os.path.normpath(os.path.join(home, CLAUDE_STANDALONE_REL))
            or os.path.normpath(os.path.join(home, _CLAUDE_STANDALONE_PAYLOAD)) in norm):
        return "standalone"
    if _CMUX_BUNDLE_MARK in path:
        return "cmux-shim"
    head = probe.read_head(path, 4096) or ""
    if _CMUX_SHIM_MARKER in head:
        return "cmux-shim"
    return "other"


_CLAUDE_INSTALL_FIX = (
    "Install Claude Code's standalone native build so `~/.local/bin/claude` exists — "
    "`claude install stable` from a working claude, or Claude Code's own installer on a machine "
    "that has none. The launch stack then pins that binary and cmux can be retired without "
    "stranding a launch path."
)


def check_claude_binary(probe):
    """doctor --stack's "which claude does a worker actually run" line (issue #303).

    Two independent readings, and the second can veto the first:
      * what the ladder resolves HERE, now — the binary a launch started from this environment
        would use, classified standalone / cmux-shim / other;
      * what the LAST REAL WORKER LAUNCH stamped (~/.superlooper/claude-bin.last). A worker's fresh
        tab has its own PATH, so a healthy resolution in the operator's shell is not evidence about
        the worker's; only the stamp is. A stamp naming cmux's shim FAILs even when this process
        resolves the standalone install.

    FAILs on the states that either break a launch now or strand it at cmux retirement (a broken
    pin, no claude at all, cmux's shim on either reading); WARNs when resolution fell through to
    PATH — that works and is cmux-independent, but nothing pinned it. This is the block the cmux
    cutover issue asserts cmux-independence against before retiring anything."""
    name = "claude binary"
    r = resolve_claude(probe)
    path, source = r["path"], r["source"]

    if path is None:
        return CheckResult(
            name, False,
            "no claude binary found: SL_CLAUDE is unset, ~/%s does not exist, and no `claude` is on "
            "PATH — no worker session can start." % CLAUDE_STANDALONE_REL,
            _CLAUDE_INSTALL_FIX)
    if not r["ok"]:
        # Every not-ok state the launcher REFUSES, spoken in the operator's terms. A relative path
        # gets its own wording on both rungs, because "not an executable file" would send someone
        # hunting for a missing binary when the actual fault is that the name means a different file
        # in every directory — and the two rungs have different cures (fix the pin vs fix PATH).
        if r.get("reason") == "relative":
            whose = ("SL_CLAUDE pins" if source == "pin"
                     else "this shell's PATH resolves `claude` to")
            cure = ("Give SL_CLAUDE an absolute path to a real claude binary."
                    if source == "pin" else
                    "Remove the empty or relative element from PATH (an empty element — a leading, "
                    "trailing or doubled `:` — means the current directory), or set SL_CLAUDE to an "
                    "absolute path.")
            return CheckResult(
                name, False,
                "%s %s, a RELATIVE path — it names a different file depending on which directory "
                "the reader happens to be in, and a worker's is its own worktree, never this one. "
                "start-session.sh refuses the launch." % (whose, path),
                cure)
        return CheckResult(
            name, False,
            "SL_CLAUDE pins %s, which is not an executable file — start-session.sh refuses the "
            "launch rather than falling back to PATH, so every worker launch fails here." % path,
            "Give SL_CLAUDE an absolute path to a real claude binary, or unset it to take the "
            "standalone install at ~/%s." % CLAUDE_STANDALONE_REL)

    kind = classify_claude(probe, path)
    where = {"pin": "pinned by SL_CLAUDE", "standalone": "the standalone native install",
             "PATH": "found on PATH"}[source]
    resolved = "the launch stack resolves %s (%s)" % (path, where)

    # What actually ran, read before any verdict: it can veto a healthy-looking resolution.
    record = probe.read_text(os.path.join(probe.home, CLAUDE_BIN_RECORD_REL))
    last = record.strip() if _nonempty_string(record) else None
    last_kind = classify_claude(probe, last) if last else None
    if last:
        launched = "the last worker launch ran %s (%s)" % (last, last_kind)
    else:
        launched = "no worker launch has recorded a binary yet"

    if last_kind == "cmux-shim":
        # Deliberately a FAIL even when the resolution above is healthy, and deliberately NOT
        # expired by age: the claim this block makes is "a worker has been observed running a
        # cmux-independent binary", and until one has, that claim is simply unproven. The cure is
        # therefore a real launch, not the passage of time — and the fix line has to SAY so, or an
        # operator who has already installed the standalone build reads a red line with no exit.
        return CheckResult(
            name, False,
            "%s, but %s — a worker tab resolves in its OWN environment, and that one ran cmux's "
            "bundled wrapper, which contains no Claude Code (it execs another claude off PATH). "
            "Retiring cmux takes that launch path with it." % (resolved, launched),
            _CLAUDE_INSTALL_FIX + " This line then clears on the NEXT worker launch, which "
            "re-stamps ~/%s from inside the session — a launch is what proves it, not a re-run of "
            "this doctor." % CLAUDE_BIN_RECORD_REL)
    if kind == "cmux-shim":
        return CheckResult(
            name, False,
            "%s — that is cmux's BUNDLED wrapper, not Claude Code: it walks PATH for another "
            "claude and execs it. Retiring cmux removes this launch path outright, and today "
            "nothing but PATH order decides it. (%s.)" % (resolved, launched),
            _CLAUDE_INSTALL_FIX)
    if source == "PATH":
        return CheckResult(
            name, True,
            "%s — it is not cmux's shim, so it survives a cmux retirement, but nothing PINS it: "
            "PATH order decides which claude a worker tab runs, and the operator's PATH is not the "
            "worker's. Install the standalone build at ~/%s (`claude install stable`) to pin it, or "
            "set SL_CLAUDE. (%s.)" % (resolved, CLAUDE_STANDALONE_REL, launched),
            warn=True)
    return CheckResult(name, True, "%s; %s" % (resolved, launched))


def check_claude(probe):
    # Probe the binary the LAUNCH STACK resolved, not whatever `claude` this process's PATH offers:
    # a login verdict read off a different binary is a confident statement about the wrong process,
    # the same class of mistake as asserting gh auth in the launcher's environment (#299).
    r = resolve_claude(probe)
    claude = r["path"]
    if not claude:
        return CheckResult(
            "claude login", False, "claude not found",
            "Install Claude Code, then run `claude auth login` with a subscription account.",
        )
    if not r["ok"]:
        # A pin naming something unrunnable. Still a FAIL — a machine that cannot run claude cannot
        # be logged in — but it must not ALSO invent a second diagnosis: running the broken pin here
        # would return 127 and this block would report a garbled `authMethod=None loggedIn=None`,
        # sending the operator to re-login over a binary-path typo. One cause, one alarm; the same
        # deference check_superlooper_plugin pays this block.
        return CheckResult(
            "claude login", False,
            "cannot read the login: %s is not a runnable claude" % claude,
            "The `claude binary` block above names the real problem; fix that first.")
    proc = probe.run([claude, "auth", "status", "--json"], timeout=10)
    data = _json(proc)
    logged_in = data.get("loggedIn") is True
    auth_method = data.get("authMethod")
    if getattr(proc, "returncode", 1) == 0 and logged_in and auth_method == "claude.ai":
        return CheckResult("claude login", True, "claude.ai subscription auth active")
    detail = _out(proc) or ("authMethod=%r loggedIn=%r" % (auth_method, data.get("loggedIn")))
    return CheckResult(
        "claude login", False, detail,
        "Run `claude auth login` with the subscription account the loop uses.",
    )


def _gh_cmd(probe):
    return probe.command("gh", envvar="SL_GH")


def check_gh_auth(probe):
    gh = _gh_cmd(probe)
    if not gh:
        return CheckResult(
            "gh auth", False, "gh not found",
            "Install GitHub CLI, then run `gh auth login --hostname github.com`.",
        )
    proc = probe.run([gh, "auth", "status", "--active", "--hostname", "github.com"], timeout=10)
    if getattr(proc, "returncode", 1) == 0:
        return CheckResult("gh auth", True, "active github.com login")
    return CheckResult(
        "gh auth", False, _out(proc) or "not authenticated",
        "Run `gh auth login --hostname github.com` and select the account that owns the loop repo.",
    )


def check_gh_headroom(probe, min_remaining=GH_MIN_REMAINING):
    gh = _gh_cmd(probe)
    if not gh:
        return CheckResult(
            "gh API headroom", False, "gh not found",
            "Install GitHub CLI, then run `gh auth login --hostname github.com`.",
        )
    proc = probe.run([gh, "api", "rate_limit"], timeout=10)
    data = _json(proc)
    core = data.get("resources", {}).get("core", {}) if isinstance(data, dict) else {}
    remaining = core.get("remaining")
    limit = core.get("limit")
    if (getattr(proc, "returncode", 1) == 0 and isinstance(remaining, int)
            and remaining >= min_remaining):
        detail = "%s/%s core requests remaining" % (remaining, limit or "?")
        return CheckResult("gh API headroom", True, detail)
    detail = _out(proc) or "%r/%r core requests remaining" % (remaining, limit)
    return CheckResult(
        "gh API headroom", False, detail,
        "Wait for the hourly GitHub API quota to reset, or switch `gh auth` to an account "
        "with at least %d core requests remaining." % min_remaining,
    )


def _stderr_tail(stderr, limit=240):
    """The tail of a failed send's stderr, collapsed to one readable clause for the FAIL line —
    the actual reason (e.g. 'recipients: No such file or directory'), capped so a multi-line
    traceback can't blow up the doctor's output."""
    tail = " ".join((stderr or "").split())
    if len(tail) > limit:
        tail = "…" + tail[-limit:]
    return tail


def check_notify(config, config_error=None, sender=None, announce=None):
    """Prove the notify channel by SENDING one real test message through the configured path — a
    channel that only checks 'is a value set' passed the live 2026-07-10 incident where every send
    exited 2 (recipient file gone) and a park alert never reached the owner. A nonzero send FAILs
    the block carrying rc + the stderr tail; a delivered send PASSes. This is doctor --stack's one
    deliberate side effect, so we announce exactly what is about to go out before it does."""
    if config_error:
        return CheckResult(
            "notify channel", False, str(config_error),
            "Run from an adopted repo or pass `--repo`; then set notify.cmd or "
            "notify.imessage_to in .superlooper/config.json.",
        )
    cfg = config if isinstance(config, dict) else {}
    notify_cfg = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    # Determine the configured channel by the SAME precedence notify.send uses. cmux is a local
    # fallback, not a channel the doctor will accept — an unconfigured channel FAILs unchanged and
    # nothing is sent (no announce, no side effect) when there is nothing real to prove.
    if _nonempty_string(notify_cfg.get("imessage_to")):
        channel = "imessage"
    elif _nonempty_string(notify_cfg.get("cmd")):
        channel = "cmd"
    else:
        return CheckResult(
            "notify channel", False, "notify.cmd and notify.imessage_to are empty",
            "Set notify.cmd or notify.imessage_to in .superlooper/config.json; cmux desktop toasts "
            "are not enough for overnight stalls.",
        )

    if sender is SKIP_SEND:
        # A read-only caller (`superlooper upkeep`). The channel is CONFIGURED — that much is
        # proven above — but delivery is not, so this is a WARN carrying the command that does
        # prove it, never a pass dressed up as one. The notify canary the morning push journals is
        # the evidence a read-only report leans on instead (report.notify_canary).
        return CheckResult(
            "notify channel", True,
            "%s configured; NOT sent — this check is read-only. `superlooper doctor --stack` "
            "sends the live test message that proves delivery." % channel,
            warn=True)

    announce = announce if announce is not None else print
    sender = sender if sender is not None else notify.send_test
    announce(
        "  notify channel: sending one live test message via %s "
        "(doctor --stack's one deliberate side effect)\n"
        "      title: %s\n      body:  %s"
        % (channel, NOTIFY_TEST_TITLE, NOTIFY_TEST_BODY)
    )
    result = sender(config, NOTIFY_TEST_TITLE, NOTIFY_TEST_BODY)

    if getattr(result, "ok", False):
        return CheckResult(
            "notify channel", True,
            "test message delivered via %s" % getattr(result, "channel", channel),
        )
    rc = getattr(result, "rc", "?")
    detail = "test send via %s failed (rc=%s)" % (getattr(result, "channel", channel), rc)
    tail = _stderr_tail(getattr(result, "stderr", ""))
    if tail:
        detail += ": " + tail
    fix = (
        "Run your notify.cmd yourself with SL_TITLE/SL_BODY set; it must exit 0."
        if channel == "cmd" else
        "Check Messages.app is signed in and the recipient is valid; the first send needs a "
        "one-time macOS permission click."
    )
    return CheckResult("notify channel", False, detail, fix)


def check_launch_shim(probe):
    shim = os.path.join(probe.home, ".superlooper", "launch-shim.zsh")
    if not probe.exists(shim):
        return CheckResult(
            "launch shim sourced", False, "%s missing" % shim,
            "Run `skills/superlooper/skill/bin/install-launch-shim.sh`, then open a new cmux tab.",
        )
    zshrc = _zshrc_path(probe)
    text = probe.read_text(zshrc) or ""
    sourced = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "launch-shim.zsh" in stripped and (
                "source " in stripped or stripped.startswith(". ")):
            sourced = True
            break
    if sourced:
        return CheckResult("launch shim sourced", True, "sourced from %s" % zshrc)
    return CheckResult(
        "launch shim sourced", False, "%s does not source the shim" % zshrc,
        "Run `skills/superlooper/skill/bin/install-launch-shim.sh`, then open a new cmux tab "
        "or source your zshrc.",
    )


def check_cmux_app_nap(probe):
    """cmux must have App Nap disabled, or overnight launch delivery dies ~40 min after the operator
    walks away (issue #120). With display/system sleep disabled (`pmset` displaysleep=0, sleep=0),
    macOS App Nap is the ONE mechanism that suspends an idle, occluded cmux while the rest of the
    machine stays fully awake: a napped cmux still answers `new-surface` (a surface UUID comes back
    and a tab appears) but defers spawning that tab's shell past the launcher's 30s verify
    window — so the launch shim never runs, no worker starts (rc=2 'LAUNCH NOT DELIVERED'), and
    enough back-to-back failures trip the systemic launch breaker. The cure is the persistent
    `NSAppSleepDisabled` default on the cmux bundle, which AppKit reads at app launch.

    FAIL loudly (like check_launch_shim) when the default is absent or explicitly false — that is a
    machine that WILL systemically fail delivery once cmux naps. The `fix` names the exact command
    AND that cmux must be fully relaunched (the flag is read only at startup, so a cmux already
    running stays nap-eligible — the manual `defaults write` alone never protects the live app). An
    undeterminable state (no `defaults` binary, or the read fails to execute — a non-macOS-ish env
    where cmux would not run anyway) WARNs rather than FAILs: we never fail the stack on a state we
    could not read."""
    env = getattr(probe, "env", {}) or {}
    bundle = env.get("SL_CMUX_BUNDLE_ID") or _CMUX_BUNDLE_ID
    name = "cmux App Nap disabled"
    fix = (
        "Run `defaults write %s NSAppSleepDisabled -bool true` (or re-run "
        "bin/install-launch-shim.sh), then FULLY QUIT and relaunch cmux — the flag is read only at "
        "app launch, so a cmux that is already running stays App-Nap-eligible until you restart it."
        % bundle
    )
    defaults = probe.command("defaults", envvar="SL_DEFAULTS", default="/usr/bin/defaults")
    if not defaults:
        return CheckResult(
            name, True,
            "could not find a `defaults` binary to read NSAppSleepDisabled for %s — App Nap state "
            "unknown (cmux runs only on macOS, where `defaults` is always present)." % bundle,
            warn=True)
    proc = probe.run([defaults, "read", bundle, "NSAppSleepDisabled"])
    rc = getattr(proc, "returncode", 1)
    value = (getattr(proc, "stdout", "") or "").strip().lower()
    if rc == 0:
        if value in _APP_NAP_TRUE:
            return CheckResult(
                name, True,
                "NSAppSleepDisabled=%s for %s (App Nap off; takes effect for cmux started AFTER it "
                "was set, so relaunch cmux once if you enabled it while cmux was running)."
                % (value, bundle))
        if value in _APP_NAP_FALSE:
            return CheckResult(
                name, False,
                "NSAppSleepDisabled=%s for %s — App Nap is explicitly ENABLED, so an idle/occluded "
                "cmux gets napped and worker launches stop delivering ~40 min after you walk away."
                % (value, bundle),
                fix)
        # Present but an unexpected value: don't silently trust it, but don't fail the stack on it.
        return CheckResult(
            name, True,
            "NSAppSleepDisabled for %s read back an unexpected value %r — verify by hand with "
            "`defaults read %s NSAppSleepDisabled`." % (bundle, value or "(empty)", bundle),
            warn=True)
    # rc 1 is `defaults`' own "does not exist" — a genuine ABSENT default (App Nap PERMITTED). Any
    # OTHER non-zero (124 = our Probe's timeout, 127 = exec failure, or an unexpected error from a
    # binary that did run) means the read is not trustworthy: can't determine -> WARN, never a
    # false "App Nap permitted" FAIL.
    if rc == 1:
        return CheckResult(
            name, False,
            "NSAppSleepDisabled is not set for %s — macOS App Nap is PERMITTED, so an idle/occluded "
            "cmux gets napped and defers spawning worker-tab shells past the 30s launch verify "
            "window (the ~40-min-after-you-walk-away systemic launch failure)." % bundle,
            fix)
    return CheckResult(
        name, True,
        "could not read NSAppSleepDisabled for %s (`defaults read` exited %s) — App Nap state "
        "unknown this run." % (bundle, rc),
        warn=True)


def _has_surface_row(out):
    """True if cmux `list-pane-surfaces` output contains a real surface row (`[* ]surface:<n> …`).
    The exact positive-signal test the runner's D7 preflight uses (bin/runner.py): judge on a real
    row, never a broad 'error:' scan — a valid tab literally titled 'Error: build log' must not
    false-fail. Mirrored (not imported) to keep lib/ free of a bin/ entry-point dependency."""
    for ln in (out or "").splitlines():
        if ln.lstrip().lstrip("*").strip().startswith("surface:"):
            return True
    return False


def _anchor_where(rec):
    """The human-readable ' (workspace=… window=…)' suffix from a recorded anchor — whichever of the
    two the runner resolved. Empty when neither is present, so the line never trails empty noise."""
    parts = [f"{k}={rec.get(k)}" for k in ("workspace", "window")
             if isinstance(rec, dict) and _nonempty_string(rec.get(k))]
    return (" (" + " ".join(parts) + ")") if parts else ""


def _state_dir(probe, config):
    """``<state home>/state`` for this repo, or None when the config cannot name one."""
    cfg = config if isinstance(config, dict) else {}
    try:
        return os.path.join(str(config_lib.state_home(cfg)), "state")
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def _live_runner_pid(probe, state):
    """The pid in ``state/runner.lock`` IF that process is alive, else None. The same read the
    anchor block has always done, lifted so the home block judges the same 'live' as it does."""
    lock = probe.read_text(os.path.join(state, "runner.lock"))
    if not _nonempty_string(lock):
        return None
    try:
        pid = int(lock.strip())
    except ValueError:
        return None
    return pid if probe.pid_alive(pid) else None


def check_runner_home(probe, config):
    """Where this repo's runner LIVES, and whether that home is actually usable (issue #306).

    The pane home is a clean skip — ``check_runner_anchor`` below judges it, and two blocks
    answering one question is how they come to disagree. For the login-item home this is the whole
    outside view, and each branch below is a way the runner can be running and WRONG while every
    other block in the stack reads green:

    * **not installed / not bootstrapped** — nothing is supervising the runner, so the next time it
      exits (a restart, a crash) nothing brings it back. Silent until the morning it matters.
    * **launchd's pid ≠ the pidfile's pid** — the dangerous one. launchd believes it supervises the
      runner while the loop's own singleton names a different process; one is about to be restarted
      out from under the other.
    * **the job's PATH lost a required command** — the spike-proven gotcha, caught statically. With
      launchd's own four entries the job comes up, looks perfectly alive, and fails every GitHub
      read.

    Read-only throughout: one ``launchctl print`` and two file reads.
    """
    name = "runner home"
    home = runner_home.kind(config)
    state = _state_dir(probe, config)
    if state is None:
        return CheckResult(name, True, "no repo config — runner-home check skipped")
    if home != runner_home.LOGIN_ITEM:
        return CheckResult(name, True, "runner_home is `%s` — the runner lives in a visible tab a "
                                       "person opened; the runner-anchor block judges it" % home)

    try:
        job = runner_home.label(config["repo"])
    except (KeyError, TypeError, ValueError) as e:
        return CheckResult(name, False, "cannot derive a job label for this repo: %s" % e,
                           "Fix `repo` in .superlooper/config.json (it must be owner/name).")
    uid = _launchd_uid(probe)
    plist = os.path.join(_launchagents_dir(probe), job + ".plist")
    fix = ("Install the job: `superlooper runner-home --repo <path> --install --load`. It records "
           "where gh and git actually resolve, which launchd's own PATH does not.")
    if not probe.exists(plist):
        return CheckResult(name, False, "no LaunchAgent at %s for job %s" % (plist, job), fix)

    proc = probe.run(runner_home.print_argv(_launchctl_bin(probe), uid, job))
    if getattr(proc, "returncode", 1) != 0:
        return CheckResult(
            name, False,
            "the job %s is installed at %s but not loaded in %s" % (job, plist,
                                                                    runner_home.domain(uid)),
            fix)

    job_pid = runner_home.service_pid(_out(proc))
    runner_pid = _live_runner_pid(probe, state)
    path_problem = _job_path_problem(probe, plist)
    # A broken recorded PATH is judged BEFORE liveness, and fails whatever the job is doing right
    # now (fresh-agent review). It is a static property of the installed home: the job will fail
    # every GitHub read on its NEXT start too, so downgrading it to a warning because the job
    # happens to be between runs would hide the one condition this check exists to catch.
    if path_problem:
        return CheckResult(name, False, "job %s %s but %s"
                           % (job, "is running (pid %s)" % job_pid if job_pid else "is installed",
                              path_problem), fix)
    if job_pid is None:
        # No pid. "The service SAYS it is not running" and "we could not tell" are different facts
        # and are answered differently — folding the second into the first would turn a changed
        # `launchctl print` shape, or any unreadable answer, into a benign-looking idle job.
        if runner_home.service_is_idle(_out(proc)) is True:
            # Genuinely loaded and idle. Between a restart and the next boot this is simply true,
            # so it is a WARN — failing here would cry wolf every time the owner taps Restart.
            return CheckResult(name, True, "job %s is loaded in %s and not running"
                               % (job, runner_home.domain(uid)), warn=True)
        return CheckResult(
            name, False,
            "the job %s is loaded in %s but neither a pid nor a recognisable state could be read "
            "from it — so nothing here can say whether a runner is supervised at all"
            % (job, runner_home.domain(uid)),
            "Read it yourself: `launchctl print %s`. If the output looks unfamiliar, this check's "
            "reader is out of date with the service manager and lib/runner_home.py needs updating "
            "— it deliberately refuses to guess." % runner_home.service_target(uid, job))
    if runner_pid is not None and runner_pid != job_pid:
        return CheckResult(
            name, False,
            "launchd is supervising pid %s for %s, but the loop's own pidfile names a live pid %s "
            "— two runners, and one is about to be restarted out from under the other"
            % (job_pid, job, runner_pid),
            # Deliberately does NOT name either pid as the stray one: `bootout` stops the
            # LAUNCHD-supervised process, which in a mismatch may well be the legitimate runner.
            # This block cannot tell which is authoritative, so it must not tell the operator to
            # act as though it can (fresh-agent review).
            "Establish which process is the real runner before ending either — `superlooper "
            "status` and the journal in the state home say which one is ticking. `launchctl "
            "bootout %s` stops the supervised one; the other must be ended by hand. Then restart "
            "the job so exactly one runner is live."
            % runner_home.service_target(uid, job))
    return CheckResult(name, True, "job %s is running in %s (pid %s)%s"
                       % (job, runner_home.domain(uid), job_pid,
                          "" if runner_pid else "; no live runner pidfile yet"))


def _launchctl_bin(probe):
    return probe.command("launchctl", envvar="SL_LAUNCHCTL", default="/bin/launchctl") \
        or "/bin/launchctl"


def _launchagents_dir(probe):
    env = getattr(probe, "env", {}) or {}
    return env.get("SL_LAUNCHD_DIR") or os.path.join(probe.home, "Library", "LaunchAgents")


def _launchd_uid(probe):
    """This process's own uid, and nothing else can say otherwise — see the CLI's `_home_uid` for
    why the override that used to live here was removed (a fresh-agent review called it blocking:
    a rule with a runtime escape hatch is not a rule, and the remedy this block PRINTS names a
    launchd domain the operator will type)."""
    return os.getuid()


def _job_path_problem(probe, plist):
    """"PATH does not resolve gh, git" for a job whose recorded PATH lost a required command, else
    "". An unreadable/unparseable plist is itself a problem — a job launchd cannot parse is a job
    that never starts."""
    text = probe.read_text(plist)
    if not _nonempty_string(text):
        return "its LaunchAgent could not be read"
    try:
        entries = plistlib.loads(text.encode()).get("EnvironmentVariables", {}).get("PATH", "")
    except Exception:
        return "its LaunchAgent is not parseable as a plist"
    dirs = [d for d in str(entries).split(":") if d]
    missing = [c for c in runner_home.REQUIRED_COMMANDS
               if not any(probe.executable(os.path.join(d, c)) for d in dirs)]
    if not missing:
        return ""
    return ("its recorded PATH does not resolve %s — launchd hands a job only %s, so these must be "
            "on the job's own PATH" % (", ".join(missing), runner_home.LAUNCHD_PATH))


def check_runner_anchor(probe, config):
    """A LIVE runner's recorded launch anchor must still resolve — else every worker tab is born in
    a dead/misplaced pane and the whole queue parks (issue #33; the 2026-07-09 misplacement, when a
    runner's cmux tab was dragged to another window). Cheap and read-only: it fires ONLY when a
    runner is actually live (its pidfile pid is alive), then re-runs the SAME read-only probe the
    startup preflight uses. No live runner, a stale pidfile, or an unreadable config are clean SKIPS
    (pass, never FAIL) — this only judges a live runner with a resolvable claim to check. A live
    runner that recorded no anchor is a WARN (older runner, or one started before this shipped).

    A LOGIN-ITEM runner is skipped outright (issue #306): it has no anchor by design, so without
    this guard the block would fire its no-matching-anchor WARN on every healthy one — a permanent
    warning that teaches the operator to ignore the block. ``check_runner_home`` judges that home.
    """
    name = "runner anchor (live)"
    home = runner_home.kind(config)
    if home != runner_home.PANE:
        return CheckResult(name, True, "runner_home is `%s` — no launch anchor exists in that home; "
                                       "the runner-home block judges it" % home)
    cfg = config if isinstance(config, dict) else {}
    state = _state_dir(probe, cfg)
    if state is None:
        return CheckResult(name, True, "no repo config — runner-anchor check skipped")

    lock = probe.read_text(os.path.join(state, "runner.lock"))
    pid = None
    if _nonempty_string(lock):
        try:
            pid = int(lock.strip())
        except ValueError:
            pid = None
    if pid is None or not probe.pid_alive(pid):
        return CheckResult(name, True, "no live runner for this repo — nothing to check")

    try:
        rec = json.loads(probe.read_text(os.path.join(state, "runner.anchor.json")) or "")
    except (TypeError, ValueError):
        rec = None
    pane = rec.get("pane") if isinstance(rec, dict) else None
    # Trust the anchor only if it belongs to THIS live pid: a hard-crashed runner leaves a stale
    # anchor, and if the OS later recycles its pid the pidfile reads "alive" — so require the
    # recorded pid to match, or an unrelated process would make us FAIL on a dead runner's record.
    rec_pid = rec.get("pid") if isinstance(rec, dict) else None
    if not _nonempty_string(pane) or rec_pid != pid:
        return CheckResult(
            name, True,
            "a runner is live (pid %s) but recorded no matching anchor — restart it from a "
            "visible cmux tab to record one" % pid, warn=True)

    # Scope the probe to the runner's OWN recorded workspace. cmux resolves --pane within the
    # caller's workspace by default (nudge-pane.sh / the cmux launcher: the 156/156-lost-rings
    # trap), and doctor runs from a DIFFERENT tab than the foreground runner — so without the
    # recorded --workspace this would resolve from the doctor's workspace and false-FAIL a healthy
    # runner. detect_self_anchor recorded caller.workspace_id, the same space --workspace expects.
    cmux = getattr(probe, "env", {}).get("SL_CMUX") or _CMUX_DEFAULT
    argv = [cmux, "list-pane-surfaces", "--pane", pane]
    ws = rec.get("workspace")
    if _nonempty_string(ws):
        argv += ["--workspace", ws]
    proc = probe.run(argv)
    if _has_surface_row(_out(proc)):
        return CheckResult(name, True, "live runner's anchor resolves%s" % _anchor_where(rec))
    return CheckResult(
        name, False,
        "live runner (pid %s) anchor no longer resolves: pane %r%s" % (pid, pane, _anchor_where(rec)),
        "The runner's recorded pane no longer resolves in the workspace it launched in (its cmux tab "
        "was closed or moved), so every worker launch will fail and the queue parks. Stop it, open a "
        "tab in the INTENDED cmux window, and re-run `superlooper run` (see "
        "plugin/skills/superlooper/references/runner-ops.md "
        "→ Restarting the runner).")


# --- installed-engine publish drift (issue #39) ------------------------------------------------
# The running loop executes the INSTALLED engine at ~/.claude/skills/superlooper, not this repo, so
# a merged engine change is inert until someone republishes through the gated bin/install.sh — that
# fence is the whole reason `skills/**` is a trustworthy bright line, and it stays. The gap this
# closes is VISIBILITY: on 2026-07-10 the installed copy sat six merged engine fixes behind main and
# nothing said so; an operator had to remember to diff VERSION by hand. These helpers measure that
# drift the SAME way bin/install.sh's engine_gate does — the installed VERSION stamp's source commit
# vs the source repo's current engine payload — so the doctor and the morning report can surface it.
ENGINE_PAYLOAD_REL = "skills/superlooper/skill"       # mirrors bin/install.sh PAYLOAD_REL


def _installed_home(probe):
    """The installed engine home ($HOME/.claude/skills/superlooper) as this probe sees it."""
    return os.path.join(probe.home, ".claude", "skills", "superlooper")


def _first_token(text):
    """The first whitespace-separated token of a VERSION stamp (`<sha> <date>`), or None.

    Both stamps the doctor compares — the engine's and the ops-doc mirror's — carry the source
    commit first and the publish date second; only the commit identifies the publish."""
    if not _nonempty_string(text):
        return None
    parts = text.split()
    return parts[0] if parts else None


def _installed_version_sha(probe):
    """First token of the installed VERSION stamp ($HOME/.claude/skills/superlooper/VERSION) — the
    source commit bin/install.sh recorded at the last publish (`<sha> <date>`, or `nogit <date>` for
    a non-git payload). None when the stamp is missing/empty (never published, or a pre-stamp
    install). Read via probe so tests inject the file without a real ~/.claude."""
    return _first_token(probe.read_text(os.path.join(_installed_home(probe), "VERSION")))


def _git(probe):
    return probe.command("git", envvar="SL_GIT")


def _source_checkout(probe, repo_path):
    """Locate a superlooper SOURCE checkout to compare the installed stamp against: a git work tree
    that actually carries the engine payload (skills/superlooper/skill). We look at the source tree,
    never the installed copy — the installed copy is rsync'd, has no .git, and is the very thing
    being measured. Publish drift only *means* something on the machine that develops AND publishes
    the engine, which is exactly where such a checkout exists. Candidates, in order:
      1. $SL_SOURCE_REPO — explicit override (tests; an operator whose checkout lives elsewhere).
      2. repo_path — the adopted repo. In the dogfood loop willprout/superlooper IS the source
         checkout (and `superlooper doctor --stack` defaults --repo to cwd), so this is the hit.
    Returns the git top-level of the first candidate that is a work tree carrying the payload, or
    None — a generic adopted repo (a plain eApp) has no source tree, so the drift check then skips."""
    git = _git(probe)
    if not git:
        return None
    env = getattr(probe, "env", {}) or {}
    candidates = []
    override = env.get("SL_SOURCE_REPO")
    if _nonempty_string(override):
        candidates.append(override)
    if _nonempty_string(repo_path):
        candidates.append(repo_path)
    for cand in candidates:
        proc = probe.run([git, "-C", cand, "rev-parse", "--show-toplevel"])
        if getattr(proc, "returncode", 1) != 0:
            continue
        lines = _out(proc).splitlines()
        top = lines[0].strip() if lines else ""
        if top and probe.exists(os.path.join(top, ENGINE_PAYLOAD_REL)):
            return top
    return None


def engine_drift(probe=None, repo_path=None, dev_branch="main"):
    """How many engine commits have landed in the source repo since the INSTALLED copy was last
    published — the installed-engine publish drift (issue #39). PURE of side effects; every external
    edge is behind `probe`, and it NEVER raises (a garbage input yields a structured skip, so the
    doctor and the morning-report assembler can call it blind). Returns a dict:
        {"status": "behind"|"in_sync"|"skipped"|"unknown",
         "behind": int|None, "installed_sha": str|None, "ref": str|None, "detail": str}
      behind   — N (>0) engine commits merged since the installed stamp; N in "behind".
      in_sync  — the installed stamp is at/after the compared ref (behind 0).
      skipped  — nothing to compare (no stamp, a nogit stamp, or no source checkout here). Not an
                 anomaly: no morning-report notice, a plain-ok doctor line.
      unknown  — an anomaly worth a WARN: the stamped commit is not in the checkout's history
                 (rebased/unrelated), or git errored computing the distance."""
    probe = probe or Probe()
    dev_branch = dev_branch if _nonempty_string(dev_branch) else "main"
    sha = _installed_version_sha(probe)
    if sha is None:
        return {"status": "skipped", "behind": None, "installed_sha": None, "ref": None,
                "detail": "installed engine carries no VERSION stamp — nothing published yet, or a "
                          "pre-stamp install."}
    if sha == "nogit":
        return {"status": "skipped", "behind": None, "installed_sha": "nogit", "ref": None,
                "detail": "installed engine was published from a non-git payload (VERSION 'nogit') "
                          "— drift cannot be measured."}
    top = _source_checkout(probe, repo_path)
    if not top:
        return {"status": "skipped", "behind": None, "installed_sha": sha, "ref": None,
                "detail": "no superlooper source checkout here to compare against — run from the "
                          "engine's source repo (or set SL_SOURCE_REPO) to measure drift."}
    git = _git(probe)
    # The stamped commit must be reachable in THIS checkout, or a rev-list distance is meaningless
    # (a rebased or unrelated history). Fail SAFE: surface it, never fabricate a count.
    inhist = probe.run([git, "-C", top, "cat-file", "-e", sha + "^{commit}"])
    if getattr(inhist, "returncode", 1) != 0:
        return {"status": "unknown", "behind": None, "installed_sha": sha, "ref": None,
                "detail": "installed stamp %s is not in this checkout's history (rebased or an "
                          "unrelated tree) — cannot measure drift; republish to re-stamp." % sha}
    # Prefer origin/<dev_branch> (what the loop merges INTO — this captures merged-but-unpublished
    # fixes even when the local branch is stale), then the local <dev_branch>, then HEAD. Report
    # which ref won so the count is honest about what it measured.
    ref = None
    for cand in ("origin/" + dev_branch, dev_branch, "HEAD"):
        proc = probe.run([git, "-C", top, "rev-parse", "--verify", "--quiet", cand + "^{commit}"])
        if getattr(proc, "returncode", 1) == 0:
            ref = cand
            break
    if ref is None:
        return {"status": "unknown", "behind": None, "installed_sha": sha, "ref": None,
                "detail": "could not resolve the %s ref in the source checkout — cannot measure "
                          "drift." % dev_branch}
    proc = probe.run([git, "-C", top, "rev-list", "--count", sha + ".." + ref, "--",
                      ENGINE_PAYLOAD_REL])
    # Parse STDOUT only for the count: git may print an advisory to stderr (e.g. an ambiguous
    # refname) while still emitting the number to stdout — merging the two (via _out) would fail
    # isdigit() and misreport a healthy repo as an anomaly. _out stays for the error-surfacing paths.
    out = (getattr(proc, "stdout", "") or "").strip()
    if getattr(proc, "returncode", 1) != 0 or not out.isdigit():
        return {"status": "unknown", "behind": None, "installed_sha": sha, "ref": ref,
                "detail": "git could not compute the engine-commit distance against %s — check by "
                          "hand." % ref}
    n = int(out)
    if n <= 0:
        return {"status": "in_sync", "behind": 0, "installed_sha": sha, "ref": ref,
                "detail": "installed engine is up to date with %s (stamp %s)." % (ref, sha)}
    unit = "commit" if n == 1 else "commits"
    return {"status": "behind", "behind": n, "installed_sha": sha, "ref": ref,
            "detail": "installed engine %d %s behind %s (stamp %s) — merged engine changes are "
                      "inert until you republish through the gated bin/install.sh (publishing stays "
                      "manual)." % (n, unit, ref, sha)}


def check_engine_drift(probe, repo_path=None, dev_branch="main"):
    """doctor --stack's installed-engine freshness line. This lives in the MACHINE-level --stack
    doctor, not the per-repo doctor, on purpose: the installed engine (~/.claude/skills/superlooper)
    is one copy per machine, shared by every adopted repo, so its publish drift is a machine fact —
    the per-repo doctor would print it identically for every repo and imply a per-repo cause. Being
    behind is BY DESIGN (a merged engine change is inert until republished through the gated
    bin/install.sh), so this NEVER fails the stack: 'behind' and every measurement anomaly are WARNs
    at most. The whole story rides in `detail` because format_results prints `fix` only for a FAIL."""
    d = engine_drift(probe, repo_path=repo_path, dev_branch=dev_branch)
    name = "installed engine current"
    if d["status"] in ("behind", "unknown"):
        return CheckResult(name, True, d["detail"], warn=True)
    return CheckResult(name, True, d["detail"])           # in_sync / skipped -> a plain ok line


# --- installed operational docs (issue #199, defect class D12) ----------------------------------
# D12: "the debugger playbook wasn't installed on the machine having the incident". The gated
# bin/install.sh now mirrors the ops docs into the installed engine home (see lib/ops_docs.py); this
# block is the half that notices when it did not, or when the mirror is left over from an older
# publish. Deliberately a FAIL where the sibling `superlooper plugin` line is only ever a WARN:
# that one is about session QUALITY (a self-contained brief still works without the skills), while
# this one is about whether an unattended 3am repair session can read the contract it is being held
# to. The cure is one command, and it is the same command the operator was going to run anyway.
# The one WARN this block does emit is the doctor's standing discipline, not a softening: docs all
# present but no engine stamp to compare them against is a state it could not READ, and it never
# asserts a mismatch it could not actually determine. See the docstring for all five states.

_OPS_DOCS_FIX = ("Republish through the gated `bin/install.sh` from a superlooper source checkout "
                 "— it mirrors the ops docs into the installed engine home and stamps them.")


def check_ops_docs(probe):
    """doctor --stack's installed-ops-docs line.

    Five states, in the order they are decided:

      1. **No installed engine home at all** — a plain ok. Another block (the launch shim, the
         activity hooks) already names that machine's real problem, and the doctor never invents a
         second alarm for one cause.
      2. **Docs missing** — FAIL, naming the first few absentees. Reached when the mirror step of a
         publish failed, or when the CLI is run from a source checkout against an engine published
         before the docs shipped; the fix line says exactly what to do.
      3. **Docs present, engine carries no stamp to compare** — WARN, and decided BEFORE the
         mismatch below, because with nothing to compare against there is no mismatch to claim. A
         hand-copied or pre-stamp install; we never assert a state we could not actually read.
      4. **Docs present, stamps disagree** — FAIL. The mirror survived from an older publish, so at
         least one page describes an engine that is no longer the one running.
      5. **Docs present, stamps agree** — ok, naming the count and the publish.
    """
    name = "installed ops docs"
    dest = _installed_home(probe)
    if not probe.exists(dest):
        return CheckResult(
            name, True,
            "no installed engine at %s — nothing published on this machine yet, so there are no "
            "ops docs to check (the launch shim / hooks blocks name the real problem)." % dest)

    missing = [p for p in ops_docs.expected_paths(dest) if not probe.exists(p)]
    if missing:
        shown = ", ".join(os.path.basename(p) for p in missing[:4])
        more = "" if len(missing) <= 4 else " (+%d more)" % (len(missing) - 4)
        return CheckResult(
            name, False,
            "%d of %d ops-doc files are missing from %s: %s%s — an unattended sl-debugger session "
            "on this machine has no playbook to follow."
            % (len(missing), len(ops_docs.expected_paths(dest)), ops_docs.mirror_dir(dest),
               shown, more),
            _OPS_DOCS_FIX)

    mirror_stamp = _first_token(probe.read_text(ops_docs.stamp_path(dest)))
    engine_stamp = _installed_version_sha(probe)
    if engine_stamp is None:
        return CheckResult(
            name, True,
            "ops docs present at %s (stamp %s), but the installed engine carries no VERSION stamp "
            "to compare against — cannot confirm they came from the same publish."
            % (ops_docs.mirror_dir(dest), mirror_stamp or "none"),
            warn=True)
    if mirror_stamp != engine_stamp:
        return CheckResult(
            name, False,
            "ops docs at %s were published at %s but the installed engine is %s — the docs are "
            "left over from an older publish and may describe an engine that is not running."
            % (ops_docs.mirror_dir(dest), mirror_stamp or "an unreadable stamp", engine_stamp),
            _OPS_DOCS_FIX)
    return CheckResult(
        name, True,
        "%d ops docs published at %s (%s), including the sl-debugger playbook"
        % (len(ops_docs.OPS_DOCS), ops_docs.mirror_dir(dest), engine_stamp))


# --- the session host's state-report hook (issue #307) -----------------------------------------
# The host learns a session's id from ONE hook, and its own installer writes that hook into the
# machine's GLOBAL Claude settings file. This loop never runs that installer: the launcher renders
# the same hook into a per-lane settings file instead. Both halves of that promise are checkable on
# a machine, and they fail for opposite reasons — so this block asks both.
_HOOK_UNINSTALL_FIX = (
    "the loop never runs the host's `integration install`, so something else did. Remove the hook "
    "entry from that settings file by hand (or run the host's own uninstall for its claude "
    "integration) and re-run this check — the loop's workers get the same hook from their own "
    "per-lane settings file and need nothing global.")
_HOOK_PUBLISH_FIX = (
    "republish the engine with the repo-root bin/install.sh — the vendored asset ships inside the "
    "gated payload, and until it lands every worker launches without host-side revive.")


def check_host_state_hook(probe):
    """doctor --stack's `host state hook` line.

    Order is the argument. The dirty-global-file case is decided FIRST and never masked by a
    healthy asset: a third-party installer editing the operator's hand-maintained settings file is
    the finding this whole issue exists to prevent, while a missing asset only costs host-side
    revive on a loop that still has its own `--resume` floor (#298).

    A machine with no installed engine at all gets a plain ok, the same posture as `installed ops
    docs`: another block already names that machine's real problem and the doctor never invents a
    second alarm for one cause.
    """
    name = "host state hook"
    # HOME from the PROBE, never from this process: an injected probe carries its own home, and a
    # block that fell through to os.path.expanduser would read the real operator's settings file in
    # the middle of a test run (the "no test reaches real state" ratchet).
    env = dict(probe.env)
    env.setdefault("HOME", probe.home)
    settings = herdr_hook.global_settings_path(env)
    text = probe.read_text(settings)
    if text is not None:
        try:
            carried = herdr_hook.carried_hook_commands(json.loads(text))
        except ValueError:
            # Somebody's hand-edited settings file is not ours to validate — and an unparseable one
            # is a real problem this block is not the right messenger for. Say what we could not
            # read rather than clearing a file we never actually checked.
            return CheckResult(
                name, True,
                "%s could not be parsed as JSON, so this check cannot say whether it carries the "
                "session host's hook." % settings, warn=True)
        if carried:
            return CheckResult(
                name, False,
                "%s carries the session host's state-report hook (%s) — `integration install` was "
                "run on this machine, which is exactly what the per-worker hook config exists to "
                "avoid." % (settings, carried[0]),
                _HOOK_UNINSTALL_FIX)

    dest = _installed_home(probe)
    if not probe.exists(dest):
        return CheckResult(
            name, True,
            "%s carries no host hook (clean). No installed engine at %s yet, so there is no "
            "vendored asset to check." % (settings, dest))

    asset = herdr_hook.vendored_script(home=dest)
    body = probe.read_text(asset)
    if body is None:
        return CheckResult(
            name, False,
            "%s is clean, but the host's state-report hook is not published at %s — workers launch "
            "without it, so a crashed session cannot be revived by the host (the loop's own "
            "--resume floor still applies)." % (settings, asset),
            _HOOK_PUBLISH_FIX)
    digest = hashlib.sha256(body.encode("utf-8", "surrogateescape")).hexdigest()
    if digest != herdr_hook.HOOK_SCRIPT_SHA256:
        return CheckResult(
            name, False,
            "the published hook asset at %s does not match the pinned checksum (%s… expected, "
            "%s… found) — a different integration contract is installed than the one this engine "
            "was accepted against." % (asset, herdr_hook.HOOK_SCRIPT_SHA256[:12], digest[:12]),
            _HOOK_PUBLISH_FIX)
    return CheckResult(
        name, True,
        "%s carries no host hook (clean); the pinned state-report asset is published at %s, so "
        "every worker gets it from its own per-lane settings file." % (settings, asset))


# --- the state report the fence lets through (issue #331) ---------------------------------------
# The block above asks about FILES: is the hook published, and is the operator's global settings
# document still clean. Both can be perfect on a machine where the capture never happens, because
# the hook's report has to cross the fence (#305) and it carries no token — so the owner's ruling
# (2026-08-04) punched one method-scoped hole for it, host-side, inside the carried patch.
#
# That hole is in a BINARY somebody built. Nothing on this machine's filesystem says whether the
# running host has it, and both states behave identically from the runner's seat: workers launch,
# work and report, and one of the two silently cannot be revived by the host after a crash. So this
# block asks the socket itself, exactly as a worker pane would — no token, one question each way.
_CAPTURE_REBUILD_FIX = (
    "rebuild the host from the pinned release with the CURRENT carried patch "
    "(skills/superlooper/vendor/herdr/, procedure in its README) and restart the server — the "
    "allowance lives in `auth::admit` and a build from before it, or a version bump that dropped "
    "the hunk, produces exactly this.")
# The variable is NAMED through the doorway's own constant rather than spelled here. Not a style
# choice: `tests/test_one_session_host_door.py` treats a host environment variable written out in
# any scanned script as reaching the host, and it is right to — a fix hint and a command look the
# same to a reader of string literals. One module spells it; everyone else asks that module.
_CAPTURE_UNFENCED_FIX = (
    "start the host from a build carrying the fence patch, with %s set in the SERVER's environment "
    "(skills/superlooper/vendor/herdr/README.md). Until then every worker pane can drive the whole "
    "fleet — issue #305 is the fence, #326 wires it into launch." % session_host.API_TOKEN_ENV_VAR)


def check_host_state_capture(probe, connect=None):
    """doctor --stack's `host state capture` line.

    Two probes, read together, because neither answers alone: `fence_probe` says whether an
    unauthenticated caller is refused at all, and `state_report_probe` says whether the ONE method
    the ruling opened is reachable. A socket that admits the state report because it admits
    everything is not a healthy machine, and this block must never render it as one.

    ``connect`` is the probes' own injection point — a ``(socket_path, payload, timeout) -> line``
    callable. Passing one exercises the real probe logic against a scripted socket; leaving it None
    opens the real one. It is the only external edge here, which is why this block does not take a
    second kind of fake.

    Nothing is written by either probe. The capture probe reaches the method BODY (that is how it
    learns it got past the fence) and the host's own handler refuses its deliberately unusable
    arguments before touching any state — see ``session_host._PROBE_PANE``.
    """
    name = "host state capture"
    # HOME from the PROBE, never this process's: same rule as the block above, and here it decides
    # which machine's socket gets opened.
    env = dict(probe.env)
    env.setdefault("HOME", probe.home)
    socket_path = session_host.control_socket_path(env)
    if not socket_path:
        return CheckResult(
            name, True,
            "no HOME and no explicit socket override, so this check could not work out where the "
            "session host would keep its control socket.", warn=True)
    if not probe.exists(socket_path):
        return CheckResult(
            name, True,
            "no session host control socket at %s — nothing is listening to ask." % socket_path)

    fence = session_host.fence_probe(socket_path, timeout=_SOCKET_PROBE_SECONDS, connect=connect)
    if fence == session_host.OPEN:
        return CheckResult(
            name, False,
            "a caller presenting NO token was served at %s — this host has no fence at all. The "
            "state report gets through because everything does, including every verb that drives "
            "the fleet." % socket_path,
            _CAPTURE_UNFENCED_FIX)
    if fence != session_host.FENCED:
        return CheckResult(
            name, True,
            "the control socket at %s did not answer, so this check cannot say whether a worker's "
            "session id would be captured. Absence of signal is unknown, not fine." % socket_path,
            warn=True)

    capture = session_host.state_report_probe(socket_path, timeout=_SOCKET_PROBE_SECONDS,
                                              connect=connect)
    if capture == session_host.ADMITTED:
        return CheckResult(
            name, True,
            "the host at %s is fenced, and admits the state report (%s) from a tokenless caller — "
            "so a worker's session id is captured and the host can revive a crashed pane."
            % (socket_path, session_host.STATE_REPORT_METHOD))
    if capture == session_host.REFUSED:
        return CheckResult(
            name, False,
            "the host at %s is fenced but REFUSES the state report (%s), so no worker's session id "
            "is ever captured and a crashed pane comes back as a bare shell. Nothing about this "
            "looks broken from the runner's seat — the sessions launch, run and work. The loop's "
            "own --session-id/--resume floor is unaffected; what is missing is the host-side second "
            "layer." % (socket_path, session_host.STATE_REPORT_METHOD),
            _CAPTURE_REBUILD_FIX)
    return CheckResult(
        name, True,
        "the host at %s refused a tokenless caller but did not answer the state-report probe, so "
        "this check cannot say whether the capture works." % socket_path, warn=True)


# --- superlooper plugin presence (issue #90) ---------------------------------------------------
# After the plugin restructure (design D10), the loop's SKILL CONTENT ships as a plugin, not inside
# the gated engine payload. A machine without it silently loses the ops / write-issue / debugger
# skills in planning and worker sessions — nothing errors, the sessions are just dumber. This block
# makes that absence visible.
_PLUGIN_ID = "superlooper@superlooper"
_PLUGIN_INSTALL = (
    "install it with `claude plugin marketplace add willprout/superlooper` then "
    "`claude plugin install superlooper@superlooper --scope user`"
)


def _plugin_rows(probe, claude):
    """(rows, problem) from `claude plugin list --json` — the DOCUMENTED CLI surface for plugin
    state (plugins reference → `plugin list`), which reports install AND enable state in one call.

    We deliberately do NOT read ~/.claude/plugins/installed_plugins.json: that file appears nowhere
    in the official plugin docs, so its shape is an internal detail that could change under us and
    turn this block into a confident liar. The CLI's --json *schema* is itself only half-documented
    (the flag is documented, the output shape is not), so every unexpected shape degrades to a
    `problem` string — an honest "could not determine" — rather than a fabricated verdict.

    Returns (list_of_rows, None) on a clean read, or (None, problem) when the state is unreadable."""
    proc = probe.run([claude, "plugin", "list", "--json"], timeout=10)
    if getattr(proc, "returncode", 1) != 0:
        return None, "`claude plugin list --json` exited %s: %s" % (
            getattr(proc, "returncode", "?"), _out(proc) or "no output")
    try:
        data = json.loads(getattr(proc, "stdout", "") or "")
    except (TypeError, ValueError):
        return None, "`claude plugin list --json` did not return parseable JSON"
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return None, ("`claude plugin list --json` returned an unexpected shape (%s) — the CLI's "
                      "JSON schema is not documented and may have changed" % type(data).__name__)
    return data, None


def check_superlooper_plugin(probe):
    """doctor --stack's superlooper-plugin presence line. WARN-only, NEVER a FAIL: the runner does
    not depend on the skills being installed (every brief the runner writes is self-contained), so a
    missing plugin must not block an otherwise-healthy stack from passing — it costs session quality,
    not correctness. An undeterminable state (no `claude` CLI, a nonzero list, an unexpected JSON
    shape) also WARNs; we never assert an absence we could not actually read.

    The whole story rides in `detail` because format_results prints `fix` only for a FAIL — a WARN
    that named a problem with no cure would be worse than silence."""
    name = "superlooper plugin"
    env = getattr(probe, "env", {}) or {}
    plugin_id = env.get("SL_PLUGIN_ID") or _PLUGIN_ID

    # Same binary the launch stack runs (issue #303), for the same reason check_claude uses it:
    # plugin state read off a different claude describes a machine the workers do not live on.
    r = resolve_claude(probe)
    claude = r["path"]
    if not claude:
        return CheckResult(
            name, True,
            "no `claude` CLI found to read plugin state — cannot tell whether %s is installed. The "
            "claude login block above names the real problem; fix that first." % plugin_id,
            warn=True)
    if not r["ok"]:
        # NEVER RUN a path the ladder refused. This is not merely about saying something consistent:
        # `_plugin_rows` EXECUTES what it is handed, so on a machine whose PATH carries a relative
        # element this block would run an executable `./claude` out of whatever directory the doctor
        # was invoked from — the arbitrary-binary-from-the-CWD case the absolute-only invariant
        # exists to refuse, reached through a read-only block nobody thought of as a launcher
        # (fresh-agent review, P0). Report the state as unread, and point at the block that owns it.
        return CheckResult(
            name, True,
            "cannot read plugin state: %s is not a claude this stack will run, so it is not run "
            "here either — the `claude binary` block above names why. Fix that first; whether %s is "
            "installed is unknown until then." % (claude, plugin_id),
            warn=True)

    rows, problem = _plugin_rows(probe, claude)
    if problem:
        return CheckResult(
            name, True,
            "%s — cannot tell whether %s is installed this run. Check by hand with `claude plugin "
            "list`." % (problem, plugin_id),
            warn=True)

    matching = [r for r in rows if r.get("id") == plugin_id]
    if not matching:
        return CheckResult(
            name, True,
            "%s is not installed — planning and worker sessions on this machine lose the "
            "superlooper ops, write-issue and debugger skills (they still RUN: briefs are "
            "self-contained, so this never fails the stack). To fix, %s."
            % (plugin_id, _PLUGIN_INSTALL),
            warn=True)

    def _where(row):
        scope = row.get("scope")
        return " at %s scope" % scope if _nonempty_string(scope) else ""

    # ANY enabled row means the skills load, so judge on that rather than on the first row: the same
    # id can legitimately appear at more than one scope (user/project/local), and a disabled row
    # sorting ahead of an enabled one must not be read as "disabled".
    enabled = next((r for r in matching if r.get("enabled") is True), None)
    if enabled is not None:
        version = enabled.get("version")
        stamp = " v%s" % version if _nonempty_string(version) else ""
        return CheckResult(
            name, True, "%s installed and enabled%s%s" % (plugin_id, _where(enabled), stamp))

    # Installed but not enabled — the same silent skill loss as absence, but a different cure:
    # re-installing would not help, enabling would. Claim this ONLY on a literal `enabled: false`.
    # The CLI's --json schema is not documented, so a row that simply lacks the key (or carries an
    # unexpected value) is a state we could not read — and asserting DISABLED there would hand the
    # operator a confident wrong diagnosis with a cure that changes nothing. Same discipline
    # _plugin_rows applies to the list shape, applied one level down to the row.
    row = matching[0]
    if not any(r.get("enabled") is False for r in matching):
        return CheckResult(
            name, True,
            "%s is installed%s but `claude plugin list --json` did not report a usable `enabled` "
            "flag for it (%r) — cannot tell whether its skills load. The CLI's JSON schema is not "
            "documented and may have changed; check by hand with `claude plugin list`."
            % (plugin_id, _where(row), row.get("enabled")),
            warn=True)
    return CheckResult(
        name, True,
        "%s is installed%s but DISABLED — its skills do not load, so planning and worker sessions "
        "lose the superlooper ops, write-issue and debugger skills (they still RUN: briefs are "
        "self-contained, so this never fails the stack). To fix, run `claude plugin enable %s`."
        % (plugin_id, _where(row), plugin_id),
        warn=True)


def check_stack(config, config_error=None, probe=None, sender=None, announce=None, repo_path=None):
    probe = probe or Probe()
    cfg = config if isinstance(config, dict) else {}
    dev = cfg.get("dev_branch")
    dev = dev if _nonempty_string(dev) else "main"
    return [
        check_codex(probe, required=_codex_required(config)),
        check_cmux(probe),
        # Before `claude login`, deliberately: which binary is in use is upstream of whether it is
        # logged in, and the login block probes whatever this one resolved (issue #303).
        check_claude_binary(probe),
        check_claude(probe),
        check_gh_auth(probe),
        check_gh_headroom(probe),
        check_notify(config, config_error=config_error, sender=sender, announce=announce),
        check_launch_shim(probe),
        check_cmux_app_nap(probe),
        check_runner_anchor(probe, config),
        check_runner_home(probe, config),
        check_engine_drift(probe, repo_path=repo_path, dev_branch=dev),
        check_ops_docs(probe),
        check_host_state_hook(probe),
        # After the hook block, deliberately: that one asks whether the asset is published and the
        # operator's settings file is clean, and this one asks whether the report it fires can
        # actually cross the fence on THIS machine (issue #331).
        check_host_state_capture(probe),
        check_superlooper_plugin(probe),
    ]


def format_results(results):
    lines = []
    for result in results:
        # WARN only when the block actually passes (warn ⇒ ok). A malformed warn+not-ok result
        # renders FAIL, matching how cmd_stack_doctor counts it (`not r.ok`), so the printed label
        # and the exit code can never disagree.
        warn = getattr(result, "warn", False) and result.ok
        label = "WARN" if warn else ("ok  " if result.ok else "FAIL")
        detail = (" - " + result.detail) if result.detail else ""
        # Only a FAIL prints a `Fix:` line; a WARN carries its guidance inline in `detail`.
        fix = (" Fix: " + result.fix) if (not result.ok and result.fix) else ""
        lines.append("  %s %s%s%s" % (label, result.name, detail, fix))
    return lines
