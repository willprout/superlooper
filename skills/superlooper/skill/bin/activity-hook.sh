#!/usr/bin/env bash
# Claude "PostToolUse" hook. Fires after every tool use in a worker session. Stamps liveness
# so a healthy long-running session is never mistaken for frozen (R2). No-ops for any session
# that isn't a superlooper worker (SL_ISSUE_ID + SL_RUN_ROOT are exported only by
# start-session.sh), so it is safe to register globally.
set -uo pipefail
cat >/dev/null 2>&1 || true            # drain hook JSON on stdin
[ -n "${SL_ISSUE_ID:-}" ] || exit 0
[ -n "${SL_RUN_ROOT:-}" ] || exit 0
mkdir -p "$SL_RUN_ROOT/state/activity"
date +%s > "$SL_RUN_ROOT/state/activity/$SL_ISSUE_ID"
exit 0
