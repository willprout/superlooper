#!/usr/bin/env bash
# Claude/Codex "PostToolUse" hook. Fires after every tool use in a worker session. Stamps
# liveness so a healthy long-running session is never mistaken for frozen (R2). No-ops for any
# session that isn't a superlooper worker (SL_ISSUE_ID + SL_RUN_ROOT are exported only by
# start-session.sh), so it is safe to register globally.
set -uo pipefail
[ -n "${SL_ISSUE_ID:-}" ] || exit 0
[ -n "${SL_RUN_ROOT:-}" ] || exit 0

# CWD SAFETY (issue #149; the same guard stop-hook.sh carries). We inherit the session's cwd, and a
# worker's worktree can be pruned out from under a live session. Stand somewhere that certainly
# exists before doing any work: bash itself spews `shell-init: getcwd` errors from an unlinked cwd,
# and anything relative (or any child that resolves the cwd) is a coin flip. Nothing below depends
# on the cwd — every path is absolute under $SL_RUN_ROOT. The state home is the run's own root; / is
# the last resort. This is defense-in-depth ONLY: it cannot save a hook the CLI never managed to
# spawn (an explicit-cwd spawn into a pruned dir dies in posix_spawn with ENOENT first — see
# tests/test_hooks.py). The real fix for that is the runner's ordered teardown, which never prunes
# a worktree while its CLI is alive.
cd "$SL_RUN_ROOT" 2>/dev/null || cd / 2>/dev/null || true
if [ "${SL_AGENT:-claude}" = "codex" ]; then
  python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse":
    sys.exit(1)
' || exit 0
else
  cat >/dev/null 2>&1 || true            # drain hook JSON on stdin
fi
mkdir -p "$SL_RUN_ROOT/state/activity"
date +%s > "$SL_RUN_ROOT/state/activity/$SL_ISSUE_ID"
exit 0
