"""The push channel (Task 10 / design record §6) — ported from the skill's notifier.

ONE function — ``send()`` — with a fixed precedence. The dead-man's switch is the safety net for
an ABSENT owner (nobody watches the watcher, §6), so the channels are ordered to REACH him, not to
decorate a screen he isn't looking at:

    notify.imessage_to → a text via Messages.app (an ``osascript`` one-liner — the same AppleScript
                         the skill's ``imessage-notify.sh`` drives, inlined here and resolving the
                         binary through ``$SL_OSASCRIPT`` so the conftest can neutralize it globally
                         in tests, never a per-test PATH stub — the 2026-07-03 ratchet)
    notify.cmd         → a shell-command template with {title}/{body} (an ntfy/Pushover curl, say)
    log-only           → nothing configured: the content is already journaled by the runner, so it
                         is never lost, only unsent

This is deliberately the skill's precedence MINUS its ``cmux`` desktop-toast tier: a desktop toast
cannot reach the absent owner a runner-down push exists for (issue #10 + BUILD-PLAN Task 10 both
pin the three tiers above). The conftest still neutralizes ``SL_CMUX`` as a harmless superset guard.

Two hard rules, both bought by the autocode postmortems (a desktop-only alert that never reached
the phone; a hung notifier that wedged a tick):
  1. NEVER raises. Every channel is wrapped; a failure — missing binary, nonzero exit, timeout —
     becomes a returned outcome STRING the caller journals, never an exception into the tick.
  2. Bounded. Every subprocess carries a hard timeout so a hung Messages/notifier cannot stall the
     loop. Notifications are a convenience layer, never the safety layer (the RUNNER DOWN surface
     + journal are the real signal); a best-effort send that fails is fine — it is just recorded.

The chosen channel does NOT cascade on failure: if ``imessage_to`` is set and the send fails, the
outcome is "imessage send failed …" — we never silently re-route to ``cmd`` (that would hide a
misconfigured primary channel). Precedence selects the ONE channel; ``log-only`` is reached only
when nothing higher is configured.
"""
import os
import subprocess

# The Messages.app AppleScript, verbatim from skill/bin/imessage-notify.sh: argv-passed recipient +
# message (never string-interpolated), so quotes/AppleScript metacharacters in either can neither
# break the script nor inject. Fed to ``osascript -`` on stdin.
_APPLESCRIPT = (
    "on run {targetRecipient, targetMessage}\n"
    '\ttell application "Messages"\n'
    "\t\tset targetService to 1st account whose service type = iMessage\n"
    "\t\tset targetBuddy to participant targetRecipient of targetService\n"
    "\t\tsend targetMessage to targetBuddy\n"
    "\tend tell\n"
    "end run"
)

SEND_TIMEOUT = 15   # generous: Messages can be slow to hand off; still bounds a hung notifier.


def _osascript_binary():
    # Resolved through SL_OSASCRIPT (the conftest contract): tests neutralize it to an absent path
    # so the default imessage path fail-closes; a test exercising success injects a stub here.
    return os.environ.get("SL_OSASCRIPT", "osascript")


def _str_or_none(v):
    """A configured channel value is a non-empty string; anything else (None, wrong-typed) reads as
    'not configured' — the same fail-closed coercion every view in this codebase uses."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def _run(args, timeout=None, env=None, input_text=None):
    """Run a channel's command. Returns its return code; a timeout → 124, and any failure to even
    launch the process → 127 — never a raise (mirrors gh._run and the runner's _run_script
    discipline). ``OSError`` covers a missing/unexecutable binary; ``ValueError`` covers an
    un-spawnable argv/env — chiefly an embedded NUL in a title/body/recipient, which subprocess
    rejects BEFORE the binary runs. Both must become a reported outcome, never an exception into
    the poll tick."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=SEND_TIMEOUT if timeout is None else timeout,
                           input=input_text,
                           env={**os.environ, **env} if env else None)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 124
    except (OSError, ValueError):
        return 127


def send(config, title, body):
    """Deliver one notification by the configured precedence; return a short outcome string the
    caller journals. Never raises. ``title``/``body`` are coerced to str so a stray non-string
    payload can never break the send."""
    title = "" if title is None else str(title)
    body = "" if body is None else str(body)
    cfg = config if isinstance(config, dict) else {}
    n = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    imessage_to = _str_or_none(n.get("imessage_to"))
    cmd = _str_or_none(n.get("cmd"))

    if imessage_to is not None:
        # One message: the title, and the body on its own line when present.
        message = "%s\n%s" % (title, body) if body else title
        rc = _run([_osascript_binary(), "-", imessage_to, message], input_text=_APPLESCRIPT)
        return "sent via imessage" if rc == 0 else "imessage send failed (rc=%d)" % rc

    if cmd is not None:
        # The untrusted VALUES never enter the shell string. {title}/{body} are replaced with
        # VARIABLE REFERENCES ("$SL_TITLE"/"$SL_BODY") and the values ride in the environment. A
        # bash variable's value is not re-parsed for command substitution, so a memo containing
        # `$(...)`/backticks/quotes is delivered verbatim no matter how the author quotes the
        # placeholder (skill's Codex R2 C1 fix). Bare {title}/{body} tokens are the intended form.
        rendered = cmd.replace("{title}", '"$SL_TITLE"').replace("{body}", '"$SL_BODY"')
        rc = _run(["bash", "-lc", rendered], env={"SL_TITLE": title, "SL_BODY": body})
        return "sent via cmd" if rc == 0 else "cmd notify failed (rc=%d)" % rc

    return "log-only"
