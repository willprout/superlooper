#!/usr/bin/env bash
# Pre-trust a folder so Claude's first-run "trust this folder?" prompt won't hang it.
# Usage: pretrust.sh /abs/path/to/worktree
#
# Spike A3 confirmed the key in ~/.claude.json:
#   .projects["<absolute folder path>"].hasTrustDialogAccepted = true
# Atomic (tmp + mv) and idempotent so it never corrupts the live config or rewrites it
# needlessly.
set -euo pipefail
DIR_IN="${1:?usage: pretrust.sh <abs-folder>}"
DIR="$(cd "$DIR_IN" 2>/dev/null && pwd -P || echo "$DIR_IN")"   # resolve to PHYSICAL path
AGENT="${SL_AGENT:-claude}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$AGENT" = "codex" ]; then
  exec "$HERE/pretrust-codex.sh" "$DIR"
fi
if [ "$AGENT" != "claude" ]; then
  echo "[pretrust] unsupported agent '$AGENT' (expected: claude or codex)" >&2
  exit 64
fi

# (pwd -P resolves symlinks, e.g. /tmp -> /private/tmp, so the key MATCHES the path Claude
#  keys trust on. Spike A3 caught a logical-vs-physical mismatch that left the prompt hanging.)
CONF="$HOME/.claude.json"
[ -f "$CONF" ] || echo '{}' > "$CONF"

# Serialize the read-modify-write against CONCURRENT superlooper launches (RC-DEADFEATURES): two
# launch-session.sh runs editing ~/.claude.json at once would lost-update each other's trust
# entries. flock on a sibling lockfile makes the whole check-and-write a critical section.
# (Best-effort vs Claude Code's OWN writes to the same file — a different process that may not honor
# the lock — but the realistic loop-vs-loop race is the one this closes.)
LOCK="$CONF.lock"
exec 9>"$LOCK"
flock 9 2>/dev/null || true        # if flock is unavailable, fall through (best-effort)

already="$(jq -r --arg d "$DIR" '.projects[$d].hasTrustDialogAccepted // false' "$CONF" 2>/dev/null || echo false)"
if [ "$already" = "true" ]; then
  echo "[pretrust] already trusted $DIR"
  exit 0
fi

# Temp file in the SAME directory as the config so `mv` is an atomic same-filesystem rename
# (mktemp's default $TMPDIR may be a different filesystem -> non-atomic copy). Clean up on fail.
tmp="$(mktemp "${CONF}.XXXXXX")"
if ! jq --arg d "$DIR" '.projects[$d].hasTrustDialogAccepted = true' "$CONF" > "$tmp"; then
  rm -f "$tmp"; echo "[pretrust] jq failed; left $CONF untouched" >&2; exit 1
fi
mv "$tmp" "$CONF"
echo "[pretrust] trusted $DIR"
