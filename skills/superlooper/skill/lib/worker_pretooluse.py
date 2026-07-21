"""The Claude PreToolUse hook's core (issues #156 + #185): two of the costliest
session-instruction-drift incidents made mechanically impossible rather than instructed-against.

pretooluse-hook.sh fences the session (loop-only, Claude-only, cwd-safe) and hands the hook
payload here. This decides ONE thing: does this tool call cross one of two named hard lines, and if
so, what reason does the session receive? The spike (docs/SPIKE-2026-07-15-hook-capabilities.md)
proved a PreToolUse hook returning permissionDecision:"deny" blocks the call even under
--dangerously-skip-permissions, with the reason delivered to the model verbatim.

  1. ASKUSERQUESTION. An interactive dialog in an unattended lane has no human to answer it; it
     stalls the lane until someone clears it by hand (incident i280 — all night). The deny does not
     just forbid; it hands back the DURABLE protocol that session was supposed to use — and WHICH
     protocol that is depends on the session's role, because handing a session someone else's
     escalation channel is itself the drift this hook exists to stop.
  2. PATTERN-KILLS. `pkill -f` / `killall` match by name/pattern, so a session's kill can also match
     the OWNER's own live processes — a worker once killed the owner's live dashboard this way. The
     deny restates the standing CLAUDE.md rule: record the PID ($!) and kill only that exact PID.

DENY ONLY THE TWO NAMED HAZARDS. Everything else is allowed — no broad allowlist that could break
legitimate tool use (issue Boundaries).

EVERY UNATTENDED SESSION THE LOOP LAUNCHES (owner ruling on #185, 2026-07-16). #156 shipped this
worker-scoped and left the question open; the ruling closed it — an answerer and a watchdog
debugger are unattended too, so both hazards apply to them as well. Each of the three ids the
loop's launchers can produce gets its OWN AskUserQuestion fallback (_ROLES):

  * `i<N>` WORKER   -> write state/blocked/<id>; the runner acts on that file, and only for `i<N>`.
  * `a<N>` ANSWERER -> be decisive, or a `PARK:` line in the one answer file it was hired to write.
                       (Post-#163 the runner no longer hires answerers and #194 retires the leftover
                       scaffolding; this covers any that still exist — the ruling's "while any
                       remain pre-#194" — and costs one table row once they are gone.)
  * `d<N>` DEBUGGER -> the memo under <state home>/reports/ plus the notify that EVERY unattended
                       sl-debugger run ends with (plugin/skills/sl-debugger/references/
                       unattended-contract.md). Never the worker's blocked file: nothing reads one
                       for a `d<N>`, so that fallback would be a dead drop.

An id of any other shape is a session whose protocol we do not know, so we hand it nothing and deny
nothing — the same fail-open posture as everything else here.

ATTENDANCE — NOT ROLE — IS THE ONE CARVE-OUT. `superlooper debug` (issue #144) launches a `d<N>`
session through the SAME shim as the watchdog, but with a person at the keyboard; its brief says so
and invites them to ask. That launch sets SL_ATTENDED=1 and duty 1 stands down for it: the deny's
whole premise ("no human is at this pane") would be a falsehood, and a falsehood that pushes the
session into the unattended contract costs more than the dialog. Duty 2 is NOT carved out — a
pattern can match the owner's live processes whether or not anyone is watching, and no brief ever
promises pattern-kills (the sl-debugger contract forbids them at every authority tier, `full`
included). The flag is honored for `d<N>` ALONE, because the owner tap is the only attended launch
that exists: a worker's env descends from the runner's shell, and an ambient `export SL_ATTENDED=1`
there must never quietly disarm the deny i280 paid for.

CLAUDE ONLY. Codex has no PreToolUse event (spike verdict); its backstop is the classifier's
at_dialog/logged_out states. run() no-ops for SL_AGENT=codex so a global registration is inert there.

FAIL OPEN, ALWAYS. This fires before EVERY tool call. A broken duty must degrade to "allow" (today's
behavior — the brief still instructs against both), never to blocking every tool and wedging the
session. The hook speaks to Claude ONLY by printing deny JSON on stdout; printing nothing lets the
call proceed untouched.
"""
import json
import os
import re
import sys


# --------------------------- duty 1: AskUserQuestion ---------------------------

def _blocked_path(state_home, issue_id):
    """The blocked-question file, the durable fallback the deny points at — the SAME path the
    brief's "Blocked?" clause names (state/blocked/<id> under the run root)."""
    return os.path.join(state_home, "state", "blocked", issue_id)


# The shared opening: WHY the dialog cannot stand. Identical for every role — the fact that nobody
# is at the pane is the same fact — so only the fallback below differs.
_ASK_PREFIX = ("AskUserQuestion is forbidden in an unattended superlooper %s session: no human is "
               "at this pane to answer it, so the dialog would stall this session until someone "
               "clears it by hand (incident i280). Do NOT ask interactively. ")


def _worker_ask_reason(state_home, issue_id):
    return _ASK_PREFIX % "worker" + (
        "Instead write your single, specific question to %s and end your turn — a fresh answerer "
        "replies into this session. If you can safely proceed on one reasonable assumption, prefer "
        "stating it in the PR body over blocking." % _blocked_path(state_home, issue_id))


def _answerer_ask_reason(state_home, issue_id):
    # An answerer must NOT be sent to state/blocked/<id>: the runner acts on that file only for a
    # worker id, so an answerer writing one would be shouting down a well. Its brief gives it two
    # legitimate exits and this names both.
    return _ASK_PREFIX % "answerer" + (
        "You were hired to be DECISIVE: give ONE recommendation with a one-line why. If the "
        "question is genuinely the owner's to decide (money, scope, product judgment, a bright-line "
        "area), do not guess — write a single line beginning `PARK: ` saying why it needs the owner. "
        "Either way it goes in the one answer file your brief names, as your final action.")


def _debugger_ask_reason(state_home, issue_id):
    # The unattended sl-debugger has neither of the other two channels; its contract ends EVERY run
    # the same way — memo + notify — so an unanswerable question is a finding, not a dialog.
    return _ASK_PREFIX % "sl-debugger" + (
        "The watchdog launched you, not a person, so behave as unattended (the stricter mode is "
        "always safe): decide from the state home's own truth within your authority tier, and turn "
        "anything you cannot decide into a named finding in the memo you write under %s — plus the "
        "notify — then end the session. The memo is the owner's whole picture of tonight."
        % os.path.join(state_home, "reports"))


# The ONE place session id -> role -> fallback is decided. `i<N>` / `a<N>` / `d<N>` are exactly the
# shapes launch-session.sh's own mode guards enforce (`^i[0-9]+$` for a worker, `^[ad][0-9]+$` for
# the --cwd modes), so this cannot recognize a session the loop cannot launch. ASCII digits only,
# deliberately: `str.isdigit()` would also accept unicode digits that no launcher can produce.
_ROLES = {"i": ("worker", _worker_ask_reason),
          "a": ("answerer", _answerer_ask_reason),
          "d": ("debugger", _debugger_ask_reason)}
_ID_RE = re.compile(r"^([iad])([0-9]+)$")


def _role(issue_id):
    """('worker'|'answerer'|'debugger', reason_fn) for a loop session id, else None. None means a
    session whose escalation protocol we do not know — we deny it nothing (fail open)."""
    m = _ID_RE.match(issue_id) if isinstance(issue_id, str) else None
    return _ROLES[m.group(1)] if m else None


# --------------------------- duty 2: pattern-kills ---------------------------

_KILL_REASON = (
    "Killing processes by name or pattern (pkill / killall) is forbidden in a superlooper loop "
    "session: the pattern can also match the OWNER's own live processes — a worker once killed the "
    "owner's live dashboard this way. Record the PID of anything you background ($!) and kill only "
    "that exact PID (`kill <pid>` / `kill -9 <pid>`)."
)

# `pkill` / `killall` invoked as a COMMAND, not as an incidental substring. It matches only at a
# command position — the string start, or right after a shell separator/subshell opener
# (;  &  |  newline  (  `  {) — so `grep pkill log`, `echo "pkill"`, and a filename like
# notes-about-killall.txt are NOT denied, while `a && pkill x`, `x; killall y`, `$(pkill z)` and a
# newline-joined command ARE. A leading benign wrapper (sudo/env/xargs/…) and an absolute/relative
# path prefix (/usr/bin/pkill, ./pkill) are seen through. Trailing (?![\w.-]) keeps it a whole word
# so `pkilld`/`pkill.sh` don't trip it.
#
# DELIBERATELY NARROW (issue Boundaries: deny only the named patterns, no broad allowlist). It is a
# safety net for ACCIDENTAL drift, not an adversarial jail, and the brief still instructs against
# both hazards — so it accepts, by design, both a rare false DENY (a `; pkill` sitting inside a
# quoted commit message or a heredoc body reads as a command position — it errs toward denying,
# which merely costs the worker a rephrase, never a killed owner process) and a rare MISS of an
# unusual invocation form: `sh -c 'pkill x'` / `bash -c "…"` / `eval "…"` (the name sits behind a
# quote, past any anchor), `xargs -r pkill` (a flag breaks the xargs wrapper), and `if pkill …; then`
# (condition position). test_pretooluse_hook.py pins these accepted edges so the behavior is visible
# and any future tightening is a conscious change, not an accident.
_KILL_RE = re.compile(
    r"""
    (?:^|[\n;&|(`{])                                              # command position
    \s*
    (?:(?:sudo|env|nohup|time|command|builtin|exec|xargs|then|do|else)\s+)*   # benign leading words
    (?:[\w./-]*/)?                                                # optional path prefix
    (?:pkill|killall)
    (?![\w.-])                                                    # whole word
    """,
    re.VERBOSE,
)


def _is_pattern_kill(command):
    return isinstance(command, str) and bool(_KILL_RE.search(command))


# --------------------------- attendance ---------------------------

# The same words start-session.sh's truthy() accepts, so one boolean is read one way across the
# launch stack. Anything else — including the empty string every unattended launch pins — is False.
_TRUE = {"1", "true", "yes", "on"}


def _attended(env, role):
    """True only for the OWNER TAP: a `d<N>` session that `superlooper debug` marked SL_ATTENDED=1,
    meaning a person is at this pane right now. Honored for the debugger role ALONE — a worker or
    answerer is never attended, and reading the flag for them would let an ambient export in the
    runner's shell disarm duty 1 (see the module docstring)."""
    return role == "debugger" and (env.get("SL_ATTENDED") or "").strip().lower() in _TRUE


# --------------------------- the decision ---------------------------

def decide(tool_name, tool_input, state_home, issue_id, ask_reason=None, attended=False):
    """Return a deny-reason string, or None to let the call proceed. Deny ONLY the two named
    hazards — no broad allowlist. `ask_reason` is the caller's role-specific fallback text builder
    (defaults to the worker's); `attended` stands duty 1 down when a human is genuinely present."""
    if tool_name == "AskUserQuestion":
        if attended:
            return None                  # a person IS at this pane — the dialog will be answered
        return (ask_reason or _worker_ask_reason)(state_home, issue_id)
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if _is_pattern_kill(command):
            return _KILL_REASON          # never carved out, attended or not
    return None


def run(payload, env):
    """Decide the PreToolUse outcome for one loop session. Returns the deny-reason string, or None
    to ALLOW. No-ops (returns None) outside the session ids the loop's own launchers produce —
    `i<N>`/`a<N>`/`d<N>`, so an ad-hoc or owner's-own session is untouched — and for Codex, and for
    any non-PreToolUse payload, so the hook is safe to register globally."""
    if (env.get("SL_AGENT") or "claude").strip() == "codex":
        return None                      # Codex has no PreToolUse event; Claude-only (spike verdict)
    issue_id = (env.get("SL_ISSUE_ID") or "").strip()
    state_home = (env.get("SL_RUN_ROOT") or "").strip()
    role = _role(issue_id)
    if not state_home or role is None:
        return None                      # not a loop session (ad-hoc / anything else)
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    return decide(payload.get("tool_name"), payload.get("tool_input"), state_home, issue_id,
                  ask_reason=role[1], attended=_attended(env, role[0]))


# --------------------------- the turn ---------------------------

def _deny(reason):
    """The EXACT JSON Claude Code requires to block a tool call, reason delivered to the model
    verbatim. Blocks even under --dangerously-skip-permissions (PreToolUse fires before the
    permission-mode check), so a bypass-mode worker cannot escape the denial (spike verdict)."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                         # unreadable input fails OPEN (allow) and SILENT
    try:
        reason = run(payload, os.environ)
    except Exception:
        return 0                         # a broken deny must never block every tool — fail OPEN
    if not reason:
        return 0                         # nothing printed -> the call proceeds untouched
    try:
        sys.stdout.write(json.dumps(_deny(reason)))
        sys.stdout.flush()               # a buffered decision that never lands is not a denial
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
