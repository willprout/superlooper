#!/usr/bin/env bash
# Pre-trust a folder so Claude's first-run "trust this folder?" prompt won't hang it.
# Usage: pretrust.sh /abs/path/to/worktree [claude-config-dir]
#
# Spike A3 confirmed the key:
#   .projects["<absolute folder path>"].hasTrustDialogAccepted = true
# Atomic (tmp + mv) and idempotent so it never corrupts the live config or rewrites it
# needlessly.
#
# WHICH file that key goes in is the second argument's whole job (issue #345). Trust is keyed PER
# CONFIG DIR, and #311's acceptance run measured this step writing into the operator's DEFAULT
# config while the session it was trusting for would be launched under a per-worker
# CLAUDE_CONFIG_DIR (#314's seam, merged 2026-08-05). A record in one file while the session reads
# another is INERT — and every issue gets a FRESH worktree, so on a fleet machine that is every
# launch stalling at an attended dialog in a folder nobody has ever opened.
#
# Nothing NEW is written to close that: #311 measured a fresh folder showing two gates (the folder
# trust, then a bypass-permissions warning defaulting to "No, exit") and pre-accepting the folder
# key alone closing BOTH. Accepting the warning persists nothing, so there is no second key to
# write and inventing one would be guessing at another program's private state.
set -euo pipefail
DIR_IN="${1:?usage: pretrust.sh <abs-folder> [claude-config-dir]}"
DIR="$(cd "$DIR_IN" 2>/dev/null && pwd -P || echo "$DIR_IN")"   # resolve to PHYSICAL path
AGENT="${SL_AGENT:-claude}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$AGENT" = "codex" ]; then
  # DELIBERATELY UNAFFECTED by #345 (owner ruling, 2026-08-05: Claude Code only). Codex keys trust
  # in $CODEX_HOME/config.toml and reads no CLAUDE_CONFIG_DIR at all, so a Claude config dir names
  # nothing on this path — it is dropped at the hand-off rather than forwarded into a file format
  # with nowhere to put it. tests/test_pretrust.py pins that this stays true.
  exec "$HERE/pretrust-codex.sh" "$DIR"
fi
if [ "$AGENT" != "claude" ]; then
  echo "[pretrust] unsupported agent '$AGENT' (expected: claude or codex)" >&2
  exit 64
fi

# (pwd -P resolves symlinks, e.g. /tmp -> /private/tmp, so the key MATCHES the path Claude
#  keys trust on. Spike A3 caught a logical-vs-physical mismatch that left the prompt hanging.)

# ---- which trust store (issue #345) -------------------------------------------------------------
# The ARGUMENT is authoritative when given, INCLUDING when it is empty: empty is the launcher
# saying "this machine assigns no config dir", which is every machine but the fleet. It has to
# out-rank the environment, because this script runs as a child of the launcher and therefore
# INHERITS the runner's shell — a stray CLAUDE_CONFIG_DIR there would otherwise aim the record at a
# dir the launch is not using, which is this same bug one directory over. Identity is assigned,
# never inherited (claim c3), and lib/launch.py names the assignment on every call.
#
# With no argument at all this is a HAND run (an operator, a drill), and there the honest answer is
# the file a `claude` started in that same shell would read — which is the agent's own variable.
if [ "$#" -ge 2 ]; then
  CFG_DIR="$2"
else
  CFG_DIR="${CLAUDE_CONFIG_DIR:-}"
fi

# The layout is ASYMMETRIC and lib/identity.py's `config_file` is its Python twin (measured
# 2026-08-06): with a config dir the file lives INSIDE it, with none it is $HOME/.claude.json, a
# SIBLING of ~/.claude rather than a child of it.
if [ -n "$CFG_DIR" ]; then
  if [ ! -d "$CFG_DIR" ]; then
    # FAIL CLOSED rather than mkdir: creating it would provision a credential namespace nobody
    # chose, and `claude` derives its keychain item from the config-dir STRING, so the session
    # would then present as logged-out rather than as an error (#300 landmine 1). The launcher's
    # rc gate turns this into "not launching" — no pane, no dialog, one accurate memo. (The launch
    # path cannot reach here in practice: #314's identity read refuses an unusable dir several
    # steps earlier. This is the hand-run's belt.)
    echo "[pretrust] the assigned Claude config dir '$CFG_DIR' is not a directory — refusing to" \
         "create it, because a config dir nobody provisioned is a credential namespace nobody" \
         "chose. Provision it in a supervised window (#313) or correct the assignment." >&2
    exit 66
  fi
  CONF="$CFG_DIR/.claude.json"
else
  CONF="$HOME/.claude.json"
fi
[ -f "$CONF" ] || echo '{}' > "$CONF"

# Serialize the read-modify-write against CONCURRENT superlooper launches (RC-DEADFEATURES): two
# launches editing one .claude.json at once would lost-update each other's trust
# entries. flock on a sibling lockfile makes the whole check-and-write a critical section.
# The lock follows the FILE, not the operator's home — a lock still pinned to the default store
# would serialize two fleet launches against a file neither of them is editing.
# (Best-effort vs Claude Code's OWN writes to the same file — a different process that may not honor
# the lock — but the realistic loop-vs-loop race is the one this closes.)
LOCK="$CONF.lock"
exec 9>"$LOCK"
flock 9 2>/dev/null || true        # if flock is unavailable, fall through (best-effort)

already="$(jq -r --arg d "$DIR" '.projects[$d].hasTrustDialogAccepted // false' "$CONF" 2>/dev/null || echo false)"
if [ "$already" = "true" ]; then
  echo "[pretrust] already trusted $DIR in $CONF"
  exit 0
fi

# Temp file in the SAME directory as the config so `mv` is an atomic same-filesystem rename
# (mktemp's default $TMPDIR may be a different filesystem -> non-atomic copy). Clean up on fail.
tmp="$(mktemp "${CONF}.XXXXXX")"
if ! jq --arg d "$DIR" '.projects[$d].hasTrustDialogAccepted = true' "$CONF" > "$tmp"; then
  rm -f "$tmp"; echo "[pretrust] jq failed; left $CONF untouched" >&2; exit 1
fi
mv "$tmp" "$CONF"
echo "[pretrust] trusted $DIR in $CONF"
