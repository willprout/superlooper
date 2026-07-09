#!/usr/bin/env bash
# The SINGLE safe primitive for every write into any cmux session pane — the runner's
# resume/answer/nudge into a worker or answerer pane. See docs/founding/EVENT-MODEL.md.
#
# Usage: nudge-pane.sh <surface> <id> <message>
#   <id> is a loop session id (i<N> worker, a<N> answerer). There is NO orchestrator here — the
#   deterministic runner is a normal terminal/launchd process, never a cmux Claude pane — so this
#   drops autocode's "orchestrator" special case entirely. (The `orchestrator=` param survives,
#   unused, in lib/pane_state.py, still unit-tested there.) Every pane this writes to is an exec
#   session, so classification uses the standard (non-fail-closed) table; an unreadable/empty screen
#   already DEFERS for all surfaces (pane_state), which is the fail-closed behaviour the answerer
#   delivery relies on.
#
# EXIT CODES (load-bearing — the runner branches on these):
#   0 = sent (text + Enter). NOTE: "sent" is NOT "a turn was taken" — mid-generation input is
#       queued/coalesced; the runner confirms real delivery via activity/report progress.
#   1 = FAILED (a cmux send/send-key error).
#   3 = DEFERRED (pane at a menu / ambiguous / unreadable / Codex attention prompt). Caller retries later.
#   4 = DEAD (the Claude process is gone; the pane is a bash shell). Caller RESTARTS — it must
#       NEVER type, or the message would run as a permission-bypassed shell command (RC-DEADPANE).
set -uo pipefail
SURF="${1:?usage: nudge-pane.sh <surface> <id> <message>}"
ID="${2:?usage: nudge-pane.sh <surface> <id> <message>}"
MSG="${3:?usage: nudge-pane.sh <surface> <id> <message>}"
# Port fix 2: every caller MUST export SL_RUN_ROOT (an unset RUN_ROOT was an observed failure).
SL_RUN_ROOT="${SL_RUN_ROOT:?SL_RUN_ROOT required}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Overridable so tests can inject a stub cmux; defaults to the real app.
CMUX="${SL_CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

# Resolve the surface's WORKSPACE for the SEND path. cmux scopes surface resolution to --workspace
# (default = the CALLER's $CMUX_WORKSPACE_ID), so even a globally-unique UUID surface living in a
# DIFFERENT workspace than the caller resolves as "not found" without it — which is why every
# send/read failed and all 156/156 doorbell rings fail-closed-DEFERRED in run-20260625-1857.
# launch-session.sh records state/panes/<id>.ws per session. If the file is missing we omit
# --workspace (graceful: the UUID may still resolve when caller and target share a workspace).
WS_FILE="$SL_RUN_ROOT/state/panes/$ID.ws"
WS_ARGS=()
if [ -s "$WS_FILE" ]; then WS_ARGS=(--workspace "$(cat "$WS_FILE")"); fi
# bash 3.2 (macOS): "${arr[@]}" on an EMPTY array trips `set -u`; the ${arr[@]+...} guard is safe.

# 1. Deterministic DEAD check: the exited marker is the primary signal.
EXITED=0
if [ -e "$SL_RUN_ROOT/state/exited/$ID" ]; then
  EXITED=1
fi

# 2. Classify the live screen through the pure, unit-tested lib/pane_state. The screen is passed
#    base64 so no shell quoting/injection is possible. An empty read proceeds to pane_state, which
#    DEFERS an empty screen (a transient read glitch must not wedge a session's nudge, but neither
#    may a stray Enter land in a dead/garbage pane).
#
# PORT FIX 1 (cmux read-screen rejects --workspace): current cmux ERRORS if `read-screen` is given
# --workspace; that error is swallowed by `2>/dev/null || true` here, yielding an empty screen and a
# permanent fail-closed DEFER (the launch machinery never recovered). So read-screen carries NO
# --workspace — ONLY --surface. `send`/`send-key` below KEEP --workspace (they accept it and need it
# for cross-workspace addressing). Do NOT "restore symmetry" by adding --workspace back here.
SCREEN_B64="$("$CMUX" read-screen --surface "$SURF" --lines 40 2>/dev/null | base64 || true)"
STATE="$(EXITED="$EXITED" SCREEN_B64="$SCREEN_B64" python3 - "$HERE/../lib" <<'PY'
import sys, os, base64
sys.path.insert(0, sys.argv[1])
import pane_state
raw = base64.b64decode(os.environ.get("SCREEN_B64", "") or "").decode("utf-8", "replace")
print(pane_state.classify_screen(
    raw,
    exited_marker=(os.environ.get("EXITED") == "1"),
    agent=os.environ.get("SL_AGENT", "claude"),
))
PY
)"

case "$STATE" in
  dead) echo "[nudge] $ID pane is DEAD — not typing; caller must restart" >&2; exit 4 ;;
  menu) echo "[nudge] $ID pane at a menu/ambiguous — deferring" >&2; exit 3 ;;
  trust_blocked) echo "[nudge] $ID Codex pane is waiting for directory trust — deferring" >&2; exit 3 ;;
  permission_blocked) echo "[nudge] $ID Codex pane is waiting for permission approval — deferring" >&2; exit 3 ;;
  quota_blocked) echo "[nudge] $ID Codex pane is usage/quota blocked — deferring" >&2; exit 3 ;;
  unknown) echo "[nudge] $ID Codex pane state is unknown — deferring" >&2; exit 3 ;;
  busy|idle) : ;;                      # safe to send
  *)    echo "[nudge] $ID unknown pane state '$STATE' — deferring" >&2; exit 3 ;;
esac

# 3. Send text, then a separate Enter (spike A2: `send` types, `send-key Enter` submits). Both carry
#    the explicit --workspace (when known) for the cross-workspace addressing reason above.
"$CMUX" send --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} "$MSG" \
  || { echo "[nudge] send failed for surface $SURF" >&2; exit 1; }
sleep 0.4                              # let the TUI register the pasted text before Enter submits
"$CMUX" send-key --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} Enter \
  || { echo "[nudge] send-key Enter failed for surface $SURF" >&2; exit 1; }
