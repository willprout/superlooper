#!/usr/bin/env bash
# Claude/Codex "Stop" hook. Fires when a session comes to rest. Acts only for worker sessions
# (start-session.sh exports SL_ISSUE_ID + SL_RUN_ROOT). No-ops for the answerer, ad-hoc, and
# every other session, so it is safe to register globally.
#
# It does NOT write a completion/report marker and does NOT notify — a session yields the turn to
# await its own background work on EVERY rest, so writing a ".done"/notify on every rest = ~50%
# false wakes + notify spam (autocode RC1). Completion is read from reports/<id>.md, blocking from
# state/blocked/<id>, neither from rest. See docs/founding/EVENT-MODEL.md.
#
# Beyond the activity stamp, a CLAUDE worker's rest is the runner's one reliable in-process moment
# (issue #148), so lib/worker_hook.py then: stamps the state/status/<id>.json progress clock and
# delivers state/mail/<id> by blocking the stop. It does NOT harvest a misplaced report any more
# (issue #189): a rest is not an ending, so on 07-16 that promoted two live drafts to "finished".
# The runner owns that trigger now — it harvests only once a worker acks DONE. CODEX
# STAYS NOTIFY-ONLY — its Stop cannot block a stop, so a "delivery" there would be a lie; Codex
# workers keep the typed-probe + file-ack path (spike verdict). This split is the agent boundary:
# both branches' agent-specific knowledge lives here, in a hook script, and nowhere else.
set -uo pipefail
[ -n "${SL_ISSUE_ID:-}" ] || exit 0
[ -n "${SL_RUN_ROOT:-}" ] || exit 0

# Resolve our own dir BEFORE moving — the registered command is absolute, so this holds even when
# the cwd below is already gone.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || HOOK_DIR=""

# CWD SAFETY. We inherit the session's cwd, and a worker's worktree can be pruned out from under a
# live session (teardown; the 07-15 forensics' pruned-cwd shape). A process whose cwd is unlinked
# can still fork/exec, but python's os.getcwd() raises and anything relative is a coin flip — so
# stand somewhere that certainly exists before doing any work. The state home is the run's own
# root; / is the last resort. Nothing below depends on the cwd: the worktree arrives in the hook
# payload and is passed to git explicitly.
cd "$SL_RUN_ROOT" 2>/dev/null || cd / 2>/dev/null || true

if [ "${SL_AGENT:-claude}" = "codex" ]; then
  python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
    sys.exit(1)
' || exit 0
  mkdir -p "$SL_RUN_ROOT/state/activity"
  date +%s > "$SL_RUN_ROOT/state/activity/$SL_ISSUE_ID"     # liveness stamp only
  exit 0
fi

# Claude. Stamp liveness FIRST and unconditionally: it is the hook's oldest promise, and a payload
# we can't parse still proves this session is alive and resting. The harness must never be able to
# cost us the clock.
mkdir -p "$SL_RUN_ROOT/state/activity"
date +%s > "$SL_RUN_ROOT/state/activity/$SL_ISSUE_ID"

# The harness reads the payload from OUR stdin and prints decision JSON (if any) to OUR stdout —
# that JSON is the only channel to Claude. It exits 0 even when a duty fails, so a non-zero here
# means the file is missing or python3 is broken: fall back to draining stdin (an unread hook pipe
# can hand the writer an EPIPE) and let the stop proceed. An older install without the lib
# degrades to exactly the activity-stamp-only hook this replaced.
if [ -n "$HOOK_DIR" ] && [ -f "$HOOK_DIR/../lib/worker_hook.py" ]; then
  python3 "$HOOK_DIR/../lib/worker_hook.py" || cat >/dev/null 2>&1 || true
else
  cat >/dev/null 2>&1 || true
fi
exit 0
