#!/usr/bin/env bash
# Claude/Codex "Stop" hook. Fires when a session comes to rest. Acts only for worker sessions
# (start-session.sh exports SL_ISSUE_ID + SL_RUN_ROOT). No-ops for the answerer, ad-hoc, and
# every other session, so it is safe to register globally.
#
# Its SOLE job is the final activity stamp. It does NOT write a completion/report marker and
# does NOT notify — a session yields the turn to await its own background work on EVERY rest,
# so writing a ".done"/notify on every rest = ~50% false wakes + notify spam (autocode RC1).
# Completion is read from reports/<id>.md, blocking from state/blocked/<id>, neither from rest.
# See docs/founding/EVENT-MODEL.md.
set -uo pipefail
[ -n "${SL_ISSUE_ID:-}" ] || exit 0
[ -n "${SL_RUN_ROOT:-}" ] || exit 0
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
else
  cat >/dev/null 2>&1 || true            # drain hook JSON on stdin
fi
mkdir -p "$SL_RUN_ROOT/state/activity"
date +%s > "$SL_RUN_ROOT/state/activity/$SL_ISSUE_ID"     # liveness stamp only
exit 0
