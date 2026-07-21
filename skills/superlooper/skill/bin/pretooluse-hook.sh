#!/usr/bin/env bash
# Claude "PreToolUse" hook. Fires before EVERY tool call. Denies exactly two named hazards —
# AskUserQuestion, and pattern-kills (pkill -f / killall) — in the UNATTENDED sessions superlooper
# launches, and lets everything else proceed untouched. This script fences on SL_ISSUE_ID +
# SL_RUN_ROOT (present for the whole family — start-session.sh launches issue workers, answerers and
# the watchdog's sl-debugger through it) and on Claude; lib/worker_pretooluse.py then makes the fine
# decision, adapting the AskUserQuestion fallback to the session's own role (`i<N>` worker ->
# blocked file, `a<N>` answerer -> `PARK:` in its answer file, `d<N>` debugger -> memo + notify) and
# no-opping for ad-hoc and everything else. Safe to register globally: outside that family this
# exits before reading a byte.
#
# The deny makes two of the costliest session-instruction-drift incidents mechanically impossible
# rather than instructed-against (issue #156, widened to every unattended session by the owner
# ruling on #185): AskUserQuestion in an unattended lane (i280) and a pattern-kill that matched the
# owner's own live dashboard. lib/worker_pretooluse.py decides; the spike proved
# permissionDecision:"deny" blocks the call even under --dangerously-skip-permissions, with the
# reason delivered to the model verbatim.
#
# CLAUDE ONLY. Codex has no PreToolUse event (spike verdict), so this hook is registered only in
# Claude's settings.json — never in Codex's hooks.json. The SL_AGENT=codex guard below is
# belt-and-suspenders: even if a codex session somehow reached here, it no-ops.
#
# FAIL OPEN, ALWAYS. A broken duty must degrade to "allow" (the brief still instructs against both
# hazards), NEVER to blocking every tool and wedging the session. The harness reads the payload from
# OUR stdin; the ONLY channel to Claude is decision JSON on OUR stdout. Printing nothing lets the
# call proceed. An older install without the lib, or a broken python3, drains stdin and allows.
set -uo pipefail
[ -n "${SL_ISSUE_ID:-}" ] || exit 0
[ -n "${SL_RUN_ROOT:-}" ] || exit 0

# Resolve our own dir BEFORE moving — the registered command is absolute, so this holds even when the
# cwd below is already gone.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || HOOK_DIR=""

# CWD SAFETY (the same guard stop-hook.sh / activity-hook.sh carry). We inherit the session's cwd,
# and a worker's worktree can be pruned out from under a live session. Stand somewhere that certainly
# exists before doing any work: python's os.getcwd() raises from an unlinked cwd and anything
# relative is a coin flip. Nothing below depends on the cwd — the lib needs only stdin + the env.
cd "$SL_RUN_ROOT" 2>/dev/null || cd / 2>/dev/null || true

# Codex: no PreToolUse event exists, so this branch is defensive only. Drain stdin (an unread hook
# pipe can hand the writer an EPIPE) and allow.
if [ "${SL_AGENT:-claude}" = "codex" ]; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

# Claude. Hand the payload to the decision core, which prints deny JSON (if any) to OUR stdout and
# exits 0 even when a duty fails. A non-zero here means python3 is broken or the lib is missing:
# drain stdin and let the call proceed (fail OPEN — never block on a broken hook).
if [ -n "$HOOK_DIR" ] && [ -f "$HOOK_DIR/../lib/worker_pretooluse.py" ]; then
  python3 "$HOOK_DIR/../lib/worker_pretooluse.py" || cat >/dev/null 2>&1 || true
else
  cat >/dev/null 2>&1 || true
fi
exit 0
