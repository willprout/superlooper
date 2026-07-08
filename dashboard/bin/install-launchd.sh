#!/usr/bin/env bash
#
# install-launchd.sh — install command-center's optional launchd keep-alive (Task 12).
#
# The DEFAULT way to run the dashboard is a visible `bin/command-center` you can watch. This script
# is the always-on option: it writes a macOS LaunchAgent that keeps ONE localhost dashboard alive
# (KeepAlive) and starts it at login (RunAtLoad). It follows the superlooper skill's launchd
# pattern — a plist template with placeholders, substituted with absolute paths.
#
# It renders + PLACES the plist and prints the one `launchctl load` command to activate it. It does
# not run launchctl unless you pass --load (so a dry install never touches the running system).
#
# Usage:
#   bin/install-launchd.sh [--load] [config.json]
#     config.json   the dashboard config to keep alive (default: <repo>/config.json)
#     --load        also `launchctl load` the job now (otherwise just print the command)
#
# The install directory is $CC_LAUNCHD_DIR (default ~/Library/LaunchAgents) — overridable so the
# test suite can install into a sandbox without touching the real LaunchAgents.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN="$ROOT/bin/command-center"

LOAD=0
CONFIG_ARG=""
for arg in "$@"; do
    case "$arg" in
        --load) LOAD=1 ;;
        *) CONFIG_ARG="$arg" ;;
    esac
done

# Config path: the given arg, else <repo>/config.json. Must exist — a keep-alive over a missing
# config would just crash-loop, so fail loud and point at the configure step (README ▸ Configure).
CONFIG_ARG="${CONFIG_ARG:-$ROOT/config.json}"
if [ ! -f "$CONFIG_ARG" ]; then
    echo "install-launchd.sh: no config file at '$CONFIG_ARG'" >&2
    echo "  Copy config.example.json to config.json and list your adopted repos first" >&2
    echo "  (see README ▸ Configure), then re-run this installer." >&2
    exit 2
fi
# Resolve to an ABSOLUTE path — launchd runs from '/', so a relative path would not resolve.
CONFIG="$(cd "$(dirname "$CONFIG_ARG")" && pwd)/$(basename "$CONFIG_ARG")"

# One localhost process → one label → one plist. Read the label from the lib so bash and Python
# agree on the single source of truth.
LABEL="$(PYTHONPATH="$ROOT/lib" python3 -c 'import launchd; print(launchd.DEFAULT_LABEL)')"

# stdout+stderr log under ~/Library/Logs; create its dir so launchd can write from first launch.
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/command-center.log"
mkdir -p "$LOG_DIR"

OUT_DIR="${CC_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
mkdir -p "$OUT_DIR"
PLIST="$OUT_DIR/$LABEL.plist"

# Render via the tested pure module (semantics server-side); the shell only supplies absolute paths.
# Render to a temp file and mv into place ONLY on success, so a failed render (e.g. an edited,
# broken template) never leaves a zero-byte plist behind in LaunchAgents.
TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/command-center-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
python3 "$ROOT/lib/launchd.py" --bin "$BIN" --config "$CONFIG" --log "$LOG" --label "$LABEL" > "$TMP_PLIST"
mv "$TMP_PLIST" "$PLIST"

echo "Wrote LaunchAgent: $PLIST"
echo "  dashboard: $BIN $CONFIG"
echo "  log:       $LOG"
echo

if [ "$LOAD" -eq 1 ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "Loaded. The dashboard is now kept alive and starts at login."
    echo "To stop it:   launchctl unload $PLIST"
else
    echo "To activate:  launchctl load $PLIST"
    echo "To stop it:   launchctl unload $PLIST"
fi
