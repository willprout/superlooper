#!/usr/bin/env bash
# One-time installer for the keystroke-free launch shim (RC6 fix). Idempotent and reversible.
#
# What it does:
#   1. Installs shell/launch-shim.zsh to ~/.superlooper/launch-shim.zsh
#   2. Creates ~/.superlooper/launch/ (mode 700) — where launch-session.sh drops per-tab commands
#   3. Appends ONE guarded line to ~/.zshrc that sources the shim (only if not already present)
#
# The shim is a strict no-op in every normal shell (see shell/launch-shim.zsh), so this is safe to
# leave installed permanently. Re-running updates the shim copy and never duplicates the ~/.zshrc
# line. Uninstall: run with `--uninstall` (removes the block + the installed files).
#
# The marker dir (~/.superlooper) and the ~/.zshrc guard block are DISTINCT from autocode's, so
# installing both leaves two independent, mutually-no-op shims (plan §B.5).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../shell/launch-shim.zsh"
DEST_DIR="$HOME/.superlooper"
DEST="$DEST_DIR/launch-shim.zsh"
LAUNCH_DIR="$DEST_DIR/launch"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
BEGIN='# >>> superlooper launch shim >>>'
END='# <<< superlooper launch shim <<<'

remove_block() {  # strip any existing superlooper block from ~/.zshrc, atomically
  [ -f "$ZSHRC" ] || return 0
  if grep -qF "$BEGIN" "$ZSHRC" 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    awk -v b="$BEGIN" -v e="$END" '
      $0==b {skip=1} skip && $0==e {skip=0; next} !skip {print}' "$ZSHRC" > "$tmp"
    mv "$tmp" "$ZSHRC"
  fi
}

if [ "${1:-}" = "--uninstall" ]; then
  remove_block
  rm -f "$DEST"
  echo "[install-launch-shim] removed the ~/.zshrc block and $DEST (left $LAUNCH_DIR in place)."
  exit 0
fi

[ -f "$SRC" ] || { echo "[install-launch-shim] shim source not found: $SRC" >&2; exit 1; }
mkdir -p "$DEST_DIR"
mkdir -p "$LAUNCH_DIR"
chmod 700 "$LAUNCH_DIR" 2>/dev/null || true
cp "$SRC" "$DEST"
chmod 644 "$DEST" 2>/dev/null || true

# Idempotent: replace any prior block, then append the current one. The leading newline keeps the
# block from gluing onto an existing last line that lacks a trailing newline.
remove_block
{
  printf '\n%s\n' "$BEGIN"
  printf '%s\n' '[ -f "$HOME/.superlooper/launch-shim.zsh" ] && source "$HOME/.superlooper/launch-shim.zsh"'
  printf '%s\n' "$END"
} >> "$ZSHRC"

echo "[install-launch-shim] installed shim -> $DEST"
echo "[install-launch-shim] launch dir    -> $LAUNCH_DIR (mode 700)"
echo "[install-launch-shim] sourced from  -> $ZSHRC"

# ---- Nap-proof cmux (issue #120) ----------------------------------------------------------------
# Overnight launch delivery was dying ~40 min after the operator walked away: macOS App Nap suspends
# the idle/occluded cmux, which then still answers new-surface (a tab appears, a UUID comes back) but
# defers spawning that tab's shell past launch-session.sh's 30s verify window — so the shim never
# runs, no worker starts (rc=2), and back-to-back failures trip the systemic launch breaker. Set the
# persistent NSAppSleepDisabled default on the cmux bundle so App Nap stays off. It is read only at
# app LAUNCH, so this needs a cmux relaunch to take effect — hence the loud restart note. Best-effort:
# a failure here NEVER aborts the shim install (the critical part); it degrades to a WARN naming the
# exact manual command. DEFAULTS_BIN / CMUX_BUNDLE_ID are overridable so the test injects a recording
# stub and no test ever writes real macOS user defaults.
DEFAULTS_BIN="${SL_DEFAULTS:-defaults}"
CMUX_BUNDLE_ID="${SL_CMUX_BUNDLE_ID:-com.cmuxterm.app}"
if command -v "$DEFAULTS_BIN" >/dev/null 2>&1 \
   && "$DEFAULTS_BIN" write "$CMUX_BUNDLE_ID" NSAppSleepDisabled -bool true 2>/dev/null; then
  echo "[install-launch-shim] App Nap disabled -> $CMUX_BUNDLE_ID (NSAppSleepDisabled=true)"
  echo "[install-launch-shim] IMPORTANT: fully QUIT and relaunch cmux for this to take effect — it is"
  echo "[install-launch-shim]   read only at app launch, so a cmux already running stays App-Nap-eligible."
else
  echo "[install-launch-shim] WARN: could not set NSAppSleepDisabled for $CMUX_BUNDLE_ID — set it yourself, then relaunch cmux:" >&2
  echo "[install-launch-shim]   defaults write $CMUX_BUNDLE_ID NSAppSleepDisabled -bool true" >&2
fi

echo "[install-launch-shim] open a NEW cmux tab (or 'source $ZSHRC') for it to take effect."
