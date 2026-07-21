#!/usr/bin/env bash
# The SINGLE safe primitive for every write into any cmux session pane — the runner's
# resume/answer/nudge into a worker pane. See docs/founding/EVENT-MODEL.md.
#
# Usage: nudge-pane.sh <surface> <id> <message>
#   <id> is a loop session id (i<N> worker, d<N> debugger). There is NO orchestrator here — the
#   deterministic runner is a normal terminal/launchd process, never a cmux Claude pane — so this
#   drops autocode's "orchestrator" special case entirely. (The `orchestrator=` param survives,
#   unused, in lib/pane_state.py, still unit-tested there.) Every pane this writes to is an exec
#   session, so classification uses the standard (non-fail-closed) table; an unreadable/empty screen
#   already DEFERS for all surfaces (pane_state), which is the fail-closed behaviour the delivery
#   delivery relies on.
#
# EXIT CODES (load-bearing — the runner branches on these):
#   0 = sent (text + Enter). NOTE: "sent" is NOT "a turn was taken" — mid-generation input is
#       queued/coalesced; the runner confirms real delivery via activity/report progress.
#   1 = FAILED (a cmux send/send-key error).
#   3 = DEFERRED (pane at a menu / ambiguous / unreadable / Codex attention prompt). Caller retries later.
#   4 = DEAD (the Claude process is gone; the pane is a bash shell). Caller RESTARTS — it must
#       NEVER type, or the message would run as a permission-bypassed shell command (RC-DEADPANE).
#   5 = AUTH DEAD IN-WINDOW (issue #151, widened by #174). The TUI is alive but every turn is
#       refused. Nothing was typed. Caller ALERTS THE OWNER — it must not retry (a nudge cannot be
#       answered) and must not restart (the relaunch re-enters dead auth). Was 94 min of typing into
#       a dead pane when this looked like plain 'idle'. ONE code covers the whole auth-death family
#       (bad external key, revoked token, org policy, failing apiKeyHelper, gateway) because the
#       caller's handling is identical for all of them; WHICH one it was rides out on stderr as
#       `auth=<variant>` so the caller's alert can name the owner's actual remedy.
#   6 = AT DIALOG (issue #151). The session raised its OWN question (AskUserQuestion) and is waiting
#       on an answer. Nothing was typed. This is a LIVE, working session — the caller must surface
#       it, not escalate it; treating it as frozen false-parked a working lane.
#
# 5 and 6 are both REFUSALS, exactly like 3 — they differ only in telling the caller WHY, which is
# the whole point: the old single "deferred" code made a dead-auth pane, a session asking a
# question, and a genuine menu indistinguishable, so the runner treated all three as "stuck".
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
# ONE python3 call returns BOTH halves of the judgement (issue #152): the verdict, and the bounded
# screen snippet it was read from. They come from the same read of the same screen, so the evidence
# can never describe a different screen than the one that was classified.
#
# The snippet is bounded by lib/evidence.bound — the SAME function the runner's records use, not a
# second implementation in shell. That is not tidiness: a `tail -c` byte cut splits a multi-byte
# character (a TUI screen is full of box-drawing glyphs) and emits invalid UTF-8, which raises
# UnicodeDecodeError inside the runner's own subprocess capture and takes down the tick that was
# only trying to explain itself. Python slices by CHARACTER, and bound() strips the control bytes.
CLASSIFY="$(EXITED="$EXITED" SCREEN_B64="$SCREEN_B64" python3 - "$HERE/../lib" <<'PY'
import sys, os, base64
sys.path.insert(0, sys.argv[1])
import pane_state, evidence
raw = base64.b64decode(os.environ.get("SCREEN_B64", "") or "").decode("utf-8", "replace")
state = pane_state.classify_screen(
    raw,
    exited_marker=(os.environ.get("EXITED") == "1"),
    agent=os.environ.get("SL_AGENT", "claude"),
)
print(state)
# Line 2 is the AUTH-DEATH VARIANT (issue #174), and is emitted ONLY for logged_out — an auth
# verdict printed beside any other state would be a reader trap. Blank when the state is anything
# else. It is NEVER blank for a logged_out state: classify_screen and auth_death_variant read the
# same table, so a logged_out verdict always has a variant. The blank case exists for the READER
# (a state that is not logged_out), not for a banner we half-recognised (fresh-review P2-6).
print(pane_state.auth_death_variant(raw) or "" if state == "logged_out" else "")
print("---8<--- screen ---8<---")
print(evidence.bound(raw, limit=evidence.SCREEN_SNIPPET_MAX))
PY
)"
STATE="$(printf '%s\n' "$CLASSIFY" | sed -n 1p)"
AUTH_VARIANT="$(printf '%s\n' "$CLASSIFY" | sed -n 2p)"

# Every refusal below is EVIDENCE the caller records (issue #152), so it must carry both halves of
# the judgement: the classifier's verdict AND the screen text it was drawn from. A bare "deferring"
# is unfalsifiable after the fact — i160 sat 43 minutes on an ambiguous defer that nobody could
# re-classify later, because the screen that produced it was never kept. This script is the only
# place that can see the screen, so this is the only place the snippet can be captured.
#
# `state=<verdict>` is machine-readable on purpose (the runner's evidence record reads it back);
# the sentence after it is for the human reading the park memo.
#
# `auth=<variant>` rides on the SAME line for the same reason (issue #174). The exit code is one bit
# — "this pane cannot answer" — and that is all the runner branches on, deliberately: every member
# of the auth-death family gets the identical never-type/never-relaunch treatment, so giving each
# one its own code would have multiplied the sites that can silently forget a member. But the
# OWNER'S remedy differs per banner ("unset ANTHROPIC_API_KEY" is not "/login"), and only this
# script can see the screen — so the variant travels out here, on the stderr the runner already
# collects, pinned to the state token so a tail cut can never keep one without the other.
#
# The snippet was already bounded and sanitized by lib/evidence.bound in the classify call above.
# An EMPTY screen still refuses (pane_state defers an unreadable read) and must say so honestly
# rather than print nothing — "captured: none, reason unknown" is a finding; silence is not.
refuse() {                             # refuse <exit-code> <state> <human sentence>
  code="$1"; state="$2"; why="$3"
  auth=""
  if [ "$state" = "logged_out" ] && [ -n "$AUTH_VARIANT" ]; then auth=" auth=$AUTH_VARIANT"; fi
  echo "[nudge] $ID state=$state$auth — $why" >&2
  snip="$(printf '%s\n' "$CLASSIFY" | sed '1,3d')"
  if [ -n "$(printf '%s' "$snip" | tr -d '[:space:]')" ]; then
    echo "[nudge] $ID screen (bounded tail — what the verdict was read from):" >&2
    printf '%s\n' "$snip" >&2
  else
    echo "[nudge] $ID screen: captured: none, reason unknown (empty or unreadable read)" >&2
  fi
  exit "$code"
}

case "$STATE" in
  dead) refuse 4 dead "pane is DEAD (the agent process is gone; the pane is a bare shell) — not typing; caller must restart" ;;
  logged_out) refuse 5 logged_out "session auth is DEAD in-window (${AUTH_VARIANT:-variant unrecognised}) — not typing; caller must alert the owner" ;;
  at_dialog) refuse 6 at_dialog "session is asking a question in-window — not typing; live, not stuck" ;;
  menu) refuse 3 menu "pane at a menu/ambiguous — deferring" ;;
  trust_blocked) refuse 3 trust_blocked "Codex pane is waiting for directory trust — deferring" ;;
  permission_blocked) refuse 3 permission_blocked "Codex pane is waiting for permission approval — deferring" ;;
  quota_blocked) refuse 3 quota_blocked "Codex pane is usage/quota blocked — deferring" ;;
  unknown) refuse 3 unknown "Codex pane state is unknown — deferring" ;;
  busy|idle) : ;;                      # safe to send
  *)    refuse 3 "${STATE:-unclassified}" "unknown pane state — deferring" ;;
esac

# 3. Send text, then a separate Enter (spike A2: `send` types, `send-key Enter` submits). Both carry
#    the explicit --workspace (when known) for the cross-workspace addressing reason above.
"$CMUX" send --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} "$MSG" \
  || { echo "[nudge] send failed for surface $SURF" >&2; exit 1; }
sleep 0.4                              # let the TUI register the pasted text before Enter submits
"$CMUX" send-key --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} Enter \
  || { echo "[nudge] send-key Enter failed for surface $SURF" >&2; exit 1; }
