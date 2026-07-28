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
worker-scoped and left the question open; the ruling closed it — the watchdog's sl-debugger is
unattended too, so both hazards apply to it as well. Each id the loop's launchers can produce gets
its OWN AskUserQuestion fallback (_ROLES):

  * `i<N>` WORKER   -> write state/blocked/<id>; the runner acts on that file, and only for `i<N>`.
  * `d<N>` DEBUGGER -> the memo under <state home>/reports/ plus the notify that EVERY unattended
                       sl-debugger run ends with (plugin/skills/sl-debugger/references/
                       unattended-contract.md). Never the worker's blocked file: nothing reads one
                       for a `d<N>`, so that fallback would be a dead drop.

The ruling named a third seat — the answerer `a<N>` — conditionally: "while any remain pre-#194".
None remain. #194 merged on 2026-07-21 and retired the answerer scaffolding outright, narrowing
launch-session.sh's `--cwd` mode to `^d[0-9]+$`, so NO launcher can produce an `a<N>` any more.
Carrying a role for a session that cannot exist would re-add the very scaffolding #194 removed, so
it is out: the ruling's condition, not the ruling, is what lapsed.

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
there must never quietly disarm the deny i280 paid for. (Belt AND suspenders: runner._script_env
and _debugger_shim_run both PIN the flag empty on every unattended launch, so the leak is closed at
the launcher too — neither half relies on the other.)

Two accepted limits of that flag, stated so they stay conscious choices (same posture as the kill
matcher's documented misses — this is a safety net for ACCIDENTAL drift, not an adversarial jail):
a `d<N>` session's cwd IS the repo, so an in-repo `.claude/settings.json` env block could assert
attendance for the watchdog's debugger (nothing in this repo does, and writing one is a deliberate
act, not a slip); and `superlooper debug` asserts attendance for every --source, the command
center's button included, which is the same claim debugger-brief-owner.md already makes to the
session in words — the person who clicked is at the machine, and nothing re-arms the duty if they
then walk away.

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


def _debugger_ask_reason(state_home, issue_id):
    # The unattended sl-debugger has no blocked file to write; its contract ends EVERY run the same
    # way — memo + notify — so an unanswerable question is a finding, not a dialog.
    return _ASK_PREFIX % "sl-debugger" + (
        "The watchdog launched you, not a person, so behave as unattended (the stricter mode is "
        "always safe): decide from the state home's own truth within your authority tier, and turn "
        "anything you cannot decide into a named finding in the memo you write under %s — plus the "
        "notify — then end the session. The memo is the owner's whole picture of tonight."
        % os.path.join(state_home, "reports"))


# The ONE place session id -> role -> fallback is decided. `i<N>` and `d<N>` are exactly the shapes
# launch-session.sh's own mode guards enforce (`^i[0-9]+$` for a worker, `^d[0-9]+$` for --cwd since
# #194), so this cannot recognize a session the loop cannot launch — and it stays in step with the
# launcher: a seat retired there falls out of here, which is why `a<N>` is gone. ASCII digits only,
# deliberately: `str.isdigit()` would also accept unicode digits that no launcher can produce.
_ROLES = {"i": ("worker", _worker_ask_reason),
          "d": ("debugger", _debugger_ask_reason)}
_ID_RE = re.compile(r"^([id])([0-9]+)$")


def _role(issue_id):
    """('worker'|'debugger', reason_fn) for a loop session id, else None. None means a session whose
    escalation protocol we do not know — we deny it nothing (fail open)."""
    m = _ID_RE.match(issue_id) if isinstance(issue_id, str) else None
    return _ROLES[m.group(1)] if m else None


# --------------------------- duty 2: pattern-kills ---------------------------

_KILL_REASON = (
    "Killing processes by name or pattern (pkill / killall) is forbidden in a superlooper loop "
    "session: the pattern can also match the OWNER's own live processes — a worker once killed the "
    "owner's live dashboard this way. Record the PID of anything you background ($!) and kill only "
    "that exact PID (`kill <pid>` / `kill -9 <pid>`). If this was a SEARCH over text and not a kill "
    "at all — a grep over docs or logs — you hit the documented false positive: a shell separator "
    "inside your quoted pattern (`|`, `;`, `&`, `(`, a backtick, a newline) reads as a command "
    "position. Rephrase the SEARCH (`grep -e pkill -e killall …`) and it goes through."
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
    meaning a person is at this pane right now. Honored for the debugger role ALONE — a worker is
    never attended, and reading the flag for one would let an ambient export in the runner's shell
    disarm duty 1 (see the module docstring)."""
    return role == "debugger" and (env.get("SL_ATTENDED") or "").strip().lower() in _TRUE


# --------------------------- the decision ---------------------------

def decide(tool_name, tool_input, state_home, issue_id, ask_reason, attended=False):
    """Return a deny-reason string, or None to let the call proceed. Deny ONLY the two named
    hazards — no broad allowlist. `ask_reason` is the caller's role-specific fallback text builder;
    `attended` stands duty 1 down when a human is genuinely present.

    `ask_reason` is REQUIRED, deliberately (fresh-agent review): a default would mean a caller that
    forgets it silently hands some other role the WORKER's protocol — the exact drift this module
    says is worse than no deny at all. Better a TypeError, which main() turns into a fail-open
    allow, than a confident wrong instruction."""
    if tool_name == "AskUserQuestion":
        if attended:
            return None                  # a person IS at this pane — the dialog will be answered
        return ask_reason(state_home, issue_id)
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if _is_pattern_kill(command):
            return _KILL_REASON          # never carved out, attended or not
    return None


def run(payload, env):
    """Decide the PreToolUse outcome for one loop session. Returns the deny-reason string, or None
    to ALLOW. No-ops (returns None) outside the session ids the loop's own launchers produce —
    `i<N>`/`d<N>`, so an ad-hoc or owner's-own session is untouched — and for Codex, and for
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
