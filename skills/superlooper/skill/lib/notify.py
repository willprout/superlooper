"""The notification adapter (plan Task 11, spec §2 "long-running work finishing / stalling /
needing input reaches William"). ONE function — send() — with a fixed precedence:

    notify.imessage_to  → text via Messages.app (skill/bin/imessage-notify.sh, an osascript
                          one-liner; the first send triggers a one-time macOS permission click —
                          see references/runner-ops.md, and the launchd-started nightly needs it too)
    notify.cmd          → a shell command template with {title}/{body} (an ntfy/Pushover curl, say)
    cmux notify         → the local desktop toast ($SL_CMUX, same binary doctor probes)
    log-only            → nothing configured and no cmux: the action is already journaled by the
                          runner, so the content is never lost, only unsent

Two hard rules, both bought by the autocode postmortems (desktop-only alerts that never reached
the phone, a hung notifier that wedged a tick):
  1. NEVER raises. Every channel is wrapped; a failure — missing binary, nonzero exit, timeout —
     becomes a returned outcome STRING the runner journals, never an exception into the tick.
  2. Bounded. Every subprocess carries a hard timeout so a hung Messages/cmux cannot stall the
     loop. Notifications are a convenience layer, never a safety layer (the ALERT file + journal
     are the real signal); so a best-effort send that fails is fine, it is just recorded.

The chosen channel does NOT cascade on failure: if imessage_to is set and the send fails, the
outcome is "imessage send failed …" — we do not silently re-route to cmux (that would hide a
misconfigured primary channel behind a desktop toast William may never see). Precedence selects
the ONE channel to use; log-only is only reached when nothing higher is configured/available.
"""
import os
import subprocess
from collections import namedtuple

# The full outcome of one delivery attempt. send() flattens this to a journaled string (its
# unchanged contract); the stack doctor reads it whole (via send_test) to FAIL the notify block
# on a nonzero send and print the actual error — rc + stderr — instead of a bare "configured".
#   channel: "imessage" | "cmd" | "cmux" | "log-only"
#   ok:      the send exited 0 (log-only is ok: nothing to send is not a failure)
#   rc:      the channel command's return code (0 when ok)
#   stderr:  the command's captured stderr (why it failed), "" on success/log-only
SendResult = namedtuple("SendResult", ["channel", "ok", "rc", "stderr"])

_HERE = os.path.dirname(os.path.abspath(__file__))
# imessage-notify.sh lives beside the other entry-point scripts in skill/bin (this module is
# skill/lib). Resolved once, absolutely, so it works whether invoked from a worktree or an install.
_IMESSAGE_SCRIPT = os.path.abspath(os.path.join(_HERE, "..", "bin", "imessage-notify.sh"))

# Same default + override as the doctor check and the ported cmux machinery, so one env var
# (SL_CMUX) points every cmux caller — including this one — at a stub in tests.
_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"

SEND_TIMEOUT = 15   # generous: Messages can be slow to hand off; still bounds a hung notifier.


def _str_or_none(v):
    """A configured channel value is a non-empty string; anything else (None, wrong-typed) reads
    as 'not configured' — the same fail-closed coercion every view in this codebase uses."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def _run(args, timeout=SEND_TIMEOUT, env=None):
    """Run a channel's command. Returns (return_code, stderr); a missing binary / OSError / timeout
    is a nonzero rc with an explanatory stderr, never a raise (mirrors gh._run and the runner's
    _run_script discipline). The stderr rides back so the stack doctor can print WHY a send failed,
    not just that it did."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **env} if env else None)
        return r.returncode, (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except (OSError, ValueError) as e:
        # OSError: missing binary. ValueError: a pathological arg (an embedded null byte, or
        # non-UTF-8 output — UnicodeDecodeError subclasses ValueError). Both become a nonzero
        # outcome, never a raise: the module contract is "never an exception into the tick", and
        # the read-only stack doctor now calls this path too.
        return 127, str(e)


def _cmux_binary():
    return os.environ.get("SL_CMUX", _CMUX_DEFAULT)


# One outcome-string pair per channel: (delivered, failed-template). send() renders these so its
# journaled contract stays byte-for-byte what the runner and its tests expect after the refactor.
_OUTCOME = {
    "imessage": ("sent via imessage", "imessage send failed (rc={rc})"),
    "cmd": ("sent via cmd", "cmd notify failed (rc={rc})"),
    "cmux": ("sent via cmux", "cmux notify failed (rc={rc})"),
}


def _deliver(config, title, body):
    """Select the ONE channel by precedence, run it, and return the full SendResult. Never raises.
    This is the single home of precedence + the never-raise guarantee: both send() (which flattens
    it to a journaled string) and send_test() (which the doctor reads whole) call it, so the two
    can never drift apart. `title`/`body` are coerced to str so a stray non-string payload can
    never break the send."""
    title = "" if title is None else str(title)
    body = "" if body is None else str(body)
    cfg = config if isinstance(config, dict) else {}
    n = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    imessage_to = _str_or_none(n.get("imessage_to"))
    cmd = _str_or_none(n.get("cmd"))

    if imessage_to is not None:
        rc, err = _run([_IMESSAGE_SCRIPT, imessage_to, title, body])
        return SendResult("imessage", rc == 0, rc, err)

    if cmd is not None:
        # The untrusted VALUES never enter the shell string. {title}/{body} are replaced with
        # VARIABLE REFERENCES ("$SL_TITLE"/"$SL_BODY"), and the values ride in the environment.
        # A bash variable's value is not re-parsed for command substitution, so a memo containing
        # `$(...)`/backticks/quotes is delivered verbatim NO MATTER how the config author quotes
        # the placeholder — shlex.quote alone only protected a BARE token and re-opened injection
        # inside `"{body}"` (Codex R2 C1). $SL_TITLE/$SL_BODY are equivalently available for
        # authors who prefer to reference them directly. Put {title}/{body} as BARE tokens for
        # verbatim delivery — the adapter supplies the quoting; a placeholder the author ALSO
        # wraps in quotes stays SAFE (never executes) but its value may word-split.
        rendered = cmd.replace("{title}", '"$SL_TITLE"').replace("{body}", '"$SL_BODY"')
        rc, err = _run(["bash", "-lc", rendered], env={"SL_TITLE": title, "SL_BODY": body})
        return SendResult("cmd", rc == 0, rc, err)

    cmux = _cmux_binary()
    if os.path.exists(cmux):
        # `--title` is the form that actually sets visible text (autocode CMUX-NOTES spike); a bare
        # positional is accepted but ignored. --body carries the detail line.
        rc, err = _run([cmux, "notify", "--title", title, "--body", body])
        return SendResult("cmux", rc == 0, rc, err)

    return SendResult("log-only", True, 0, "")


def send(config, title, body):
    """Deliver one notification by the configured precedence; return a short outcome string the
    caller journals. Never raises."""
    r = _deliver(config, title, body)
    if r.channel == "log-only":
        return "log-only"
    ok_msg, fail_msg = _OUTCOME[r.channel]
    return ok_msg if r.ok else fail_msg.format(rc=r.rc)


def send_test(config, title, body):
    """Deliver ONE notification through the configured precedence and return the full SendResult
    (channel, ok, rc, stderr) — the stack doctor's hook for PROVING the channel works. Same
    precedence, same never-raise guarantee as send(); the only difference is the caller gets rc +
    stderr instead of a flattened string, so a failed send can be reported with its actual reason.
    A real message really goes out: callers announce the side effect first."""
    return _deliver(config, title, body)
