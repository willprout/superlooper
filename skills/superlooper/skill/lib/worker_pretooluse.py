"""The Claude worker PreToolUse hook's core (issue #156): two of the costliest
worker-instruction-drift incidents made mechanically impossible rather than instructed-against.

pretooluse-hook.sh fences the session (worker-only, Claude-only, cwd-safe) and hands the hook
payload here. This decides ONE thing: does this tool call cross one of two named hard lines, and if
so, what reason does the worker receive? The spike (docs/SPIKE-2026-07-15-hook-capabilities.md)
proved a PreToolUse hook returning permissionDecision:"deny" blocks the call even under
--dangerously-skip-permissions, with the reason delivered to the model verbatim.

  1. ASKUSERQUESTION. An interactive dialog in an unattended lane has no human to answer it; it
     stalls the lane until someone clears it by hand (incident i280 — all night). The deny does not
     just forbid; it hands back the DURABLE protocol the worker was supposed to use — write the
     blocked-question file a fresh answerer actually reads — at the exact path the brief names.
  2. PATTERN-KILLS. `pkill -f` / `killall` match by name/pattern, so a worker's kill can also match
     the OWNER's own live processes — a worker once killed the owner's live dashboard this way. The
     deny restates the standing CLAUDE.md rule: record the PID ($!) and kill only that exact PID.

DENY ONLY THE TWO NAMED HAZARDS. Everything else is allowed — no broad allowlist that could break
legitimate worker tool use (issue Boundaries).

WORKER SESSIONS ONLY. start-session.sh launches BOTH issue workers (id `i<N>`) and answerers
(id `a<N>`) through the same stack, so both carry SL_ISSUE_ID + SL_RUN_ROOT. This deny is
worker-scoped: the AskUserQuestion fallback it hands back — write state/blocked/<id> — is a WORKER
protocol the runner acts on (it recognizes only `i<N>`), and would be wrong for an answerer, which
is told to touch nothing but its one answer file and to escalate with a `PARK:` line. So run()
denies only in worker sessions and no-ops for answerers, ad-hoc, and everything else. (Whether the
same deny should also cover answerer sessions — also unattended — is a separate owner call.)

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


def _ask_reason(state_home, issue_id):
    return (
        "AskUserQuestion is forbidden in an unattended superlooper worker session: no human is at "
        "this pane to answer it, so the dialog would stall the lane until someone clears it by hand "
        "(incident i280). Do NOT ask interactively. Instead write your single, specific question to "
        "%s and end your turn — a fresh answerer replies into this session. If you can safely "
        "proceed on one reasonable assumption, prefer stating it in the PR body over blocking."
        % _blocked_path(state_home, issue_id)
    )


# --------------------------- duty 2: pattern-kills ---------------------------

_KILL_REASON = (
    "Killing processes by name or pattern (pkill / killall) is forbidden in a superlooper worker "
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


# --------------------------- the decision ---------------------------

def _is_worker(issue_id):
    """A superlooper WORKER id is `i<N>`; an answerer is `a<N>`. Mirrors actions._iid_num's
    convention exactly (start-session.sh's own id contract), so worker-vs-answerer is decided the
    one way the whole runner already decides it."""
    return isinstance(issue_id, str) and issue_id.startswith("i") and issue_id[1:].isdigit()


def decide(tool_name, tool_input, state_home, issue_id):
    """Return a deny-reason string, or None to let the call proceed. Deny ONLY the two named
    hazards — no broad allowlist."""
    if tool_name == "AskUserQuestion":
        return _ask_reason(state_home, issue_id)
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if _is_pattern_kill(command):
            return _KILL_REASON
    return None


def run(payload, env):
    """Decide the PreToolUse outcome for a worker session. Returns the deny-reason string, or None
    to ALLOW. No-ops (returns None) outside a superlooper WORKER session — answerers (`a<N>`),
    ad-hoc, and every other session — and for Codex, and for any non-PreToolUse payload, so the hook
    is safe to register globally."""
    if (env.get("SL_AGENT") or "claude").strip() == "codex":
        return None                      # Codex has no PreToolUse event; Claude-only (spike verdict)
    issue_id = (env.get("SL_ISSUE_ID") or "").strip()
    state_home = (env.get("SL_RUN_ROOT") or "").strip()
    if not state_home or not _is_worker(issue_id):
        return None                      # not a WORKER session (answerer / ad-hoc / anything else)
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    return decide(payload.get("tool_name"), payload.get("tool_input"), state_home, issue_id)


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
