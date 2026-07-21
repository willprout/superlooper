"""Machine-level stack doctor checks for `superlooper doctor --stack`.

The repo-level doctor in the CLI validates one adopted repository. This module validates the
ambient machine blocks the loop depends on before it can run reliably overnight. Every external
edge is behind Probe so tests can inject fake command resolution, command output, file reads, and
environment without reaching real binaries or the network.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

import config as config_lib
import notify
import ops_docs


_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"
# cmux's CFBundleIdentifier — the macOS user-defaults domain App Nap reads NSAppSleepDisabled from
# (verified from /Applications/cmux.app/Contents/Info.plist). Overridable via SL_CMUX_BUNDLE_ID for
# a differently-signed build or a test. See check_cmux_app_nap.
_CMUX_BUNDLE_ID = "com.cmuxterm.app"
_APP_NAP_TRUE = {"1", "yes", "true", "on"}
_APP_NAP_FALSE = {"0", "no", "false", "off"}
GH_MIN_REMAINING = 500

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
        found = shutil.which(name)
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


def check_claude(probe):
    claude = probe.command("claude", envvar="SL_CLAUDE")
    if not claude:
        return CheckResult(
            "claude login", False, "claude not found",
            "Install Claude Code, then run `claude auth login` with a subscription account.",
        )
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
    and a tab appears) but defers spawning that tab's shell past launch-session.sh's 30s verify
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


def check_runner_anchor(probe, config):
    """A LIVE runner's recorded launch anchor must still resolve — else every worker tab is born in
    a dead/misplaced pane and the whole queue parks (issue #33; the 2026-07-09 misplacement, when a
    runner's cmux tab was dragged to another window). Cheap and read-only: it fires ONLY when a
    runner is actually live (its pidfile pid is alive), then re-runs the SAME read-only probe the
    startup preflight uses. No live runner, a stale pidfile, or an unreadable config are clean SKIPS
    (pass, never FAIL) — this only judges a live runner with a resolvable claim to check. A live
    runner that recorded no anchor is a WARN (older runner, or one started before this shipped)."""
    name = "runner anchor (live)"
    cfg = config if isinstance(config, dict) else {}
    try:
        state = os.path.join(str(config_lib.state_home(cfg)), "state")
    except (KeyError, AttributeError, TypeError, ValueError):
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
    # caller's workspace by default (nudge-pane.sh / launch-session.sh: the 156/156-lost-rings
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

    claude = probe.command("claude", envvar="SL_CLAUDE")
    if not claude:
        return CheckResult(
            name, True,
            "no `claude` CLI found to read plugin state — cannot tell whether %s is installed. The "
            "claude login block above names the real problem; fix that first." % plugin_id,
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
        check_claude(probe),
        check_gh_auth(probe),
        check_gh_headroom(probe),
        check_notify(config, config_error=config_error, sender=sender, announce=announce),
        check_launch_shim(probe),
        check_cmux_app_nap(probe),
        check_runner_anchor(probe, config),
        check_engine_drift(probe, repo_path=repo_path, dev_branch=dev),
        check_ops_docs(probe),
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
