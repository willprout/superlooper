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

import notify


_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"
GH_MIN_REMAINING = 500

# The one message the doctor actually sends to prove the channel. Static (no clock) so the check
# is deterministic and the owner learns to recognize it. Reads as an explanation on arrival.
NOTIFY_TEST_TITLE = "superlooper doctor: notify channel test"
NOTIFY_TEST_BODY = (
    "doctor --stack sent this to prove your notify channel delivers. "
    "Receiving it means overnight stall alerts can reach you here."
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


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


def check_codex(probe):
    codex = probe.command("codex", envvar="SL_CODEX")
    if not codex:
        return CheckResult(
            "codex CLI", False, "codex not found",
            "Install the Codex CLI, then run `codex login`.",
        )
    proc = probe.run([codex, "login", "status"], timeout=10)
    if getattr(proc, "returncode", 1) == 0:
        detail = _out(proc) or codex
        return CheckResult("codex CLI", True, detail)
    return CheckResult(
        "codex CLI", False, _out(proc) or "not authenticated",
        "Run `codex login` and confirm `codex login status` succeeds.",
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


def check_stack(config, config_error=None, probe=None, sender=None, announce=None):
    probe = probe or Probe()
    return [
        check_codex(probe),
        check_cmux(probe),
        check_claude(probe),
        check_gh_auth(probe),
        check_gh_headroom(probe),
        check_notify(config, config_error=config_error, sender=sender, announce=announce),
        check_launch_shim(probe),
    ]


def format_results(results):
    lines = []
    for result in results:
        detail = (" - " + result.detail) if result.detail else ""
        fix = (" Fix: " + result.fix) if (not result.ok and result.fix) else ""
        lines.append("  %s %s%s%s" % ("ok  " if result.ok else "FAIL", result.name, detail, fix))
    return lines
