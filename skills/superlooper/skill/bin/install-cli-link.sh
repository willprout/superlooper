#!/usr/bin/env bash
# install-cli-link.sh — put a stable `superlooper` command on PATH (issue #31).
#
# Every doc invokes the CLI bare (`superlooper adopt`, `superlooper doctor`, `superlooper run`),
# but the real binary lives INSIDE the published skill at
# ~/.claude/skills/superlooper/bin/superlooper — a location no install step ever put on PATH, so a
# newcomer's very first documented command answered "command not found". This is the missing link
# step; the gated root bin/install.sh runs it right AFTER it publishes the payload.
#
# THIN SHIM, not a symlink. The CLI resolves its lib/ imports relative to __file__ via
# os.path.abspath, which does NOT follow symlinks — so a symlink invoked as ~/.local/bin/superlooper
# would look for lib/ under ~/.local/lib and fail every import. The shim `exec`s the installed binary
# by its real path, so the CLI runs with __file__ pointing at the installed copy and its imports
# resolve. (The DoD allows "symlink or thin shim"; the import trap makes the shim the safe one.)
#
# The shim's exec target is $HOME/.claude/... — the INSTALLED copy, NEVER this source repo — and
# $HOME is written LITERAL so the shim stays portable and re-resolves at run time on any machine.
#
# Idempotent: re-running rewrites the SAME shim byte-for-byte, and sweeps a stale shim it wrote into
# a different candidate dir on a prior run (marker-guarded, so it only ever removes ITS OWN shim,
# never a foreign binary). If NO standard bin dir is on PATH it does not silently skip: it writes the
# shim into the preferred dir and prints the EXACT line to add that dir to PATH.
set -euo pipefail

# The installed CLI (published by bin/install.sh just before this runs). The LITERAL form is what
# the emitted shim carries; the REAL form is used only for the human-facing "does it exist yet" note.
INSTALLED_LITERAL='$HOME/.claude/skills/superlooper/bin/superlooper'
INSTALLED_REAL="$HOME/.claude/skills/superlooper/bin/superlooper"

# The marker line the emitted shim carries; the sweep removes ONLY files bearing it.
MARKER='superlooper-cli-shim (bin/install.sh, issue #31)'

# Candidate standard user bin dirs, priority order. The first that is BOTH on PATH and writable (or
# creatable without sudo) wins — so on a machine that already has one on PATH, `superlooper` just
# works with no further step. ~/.local/bin leads: user-owned (never needs sudo) and the widely
# adopted per-user bin location. /usr/local/bin is last and only used if already on PATH AND writable
# (never via sudo — an installer that silently needs root is worse than one that prints a PATH line).
CANDIDATES=("$HOME/.local/bin" "$HOME/bin" "/usr/local/bin")
PREFERRED="$HOME/.local/bin"          # fallback when nothing is on PATH (always creatable under $HOME)

on_path() {  # is $1 a component of $PATH? (trailing slash tolerated on either side)
  local dir="${1%/}" p
  local IFS=:
  for p in $PATH; do
    [ "${p%/}" = "$dir" ] && return 0
  done
  return 1
}

usable() {  # can we write a file into $1 without sudo? (exists+writable, or creatable)
  local dir="$1" parent
  if [ -d "$dir" ]; then
    [ -w "$dir" ]
  else
    parent="$dir"
    while [ ! -d "$parent" ]; do parent="$(dirname "$parent")"; done
    [ -w "$parent" ]
  fi
}

is_our_shim() {  # does the file at $1 bear our marker?
  [ -f "$1" ] && grep -qF "$MARKER" "$1" 2>/dev/null
}

write_shim() {  # write_shim <path> — emit the thin shim (single-quoted heredoc keeps $HOME/$@ LITERAL)
  local path="$1"
  mkdir -p "$(dirname "$path")"
  # Remove any existing entry FIRST so we never write THROUGH a symlink onto its target (a symlink
  # named `superlooper` would otherwise have its destination clobbered); this makes replace clean.
  rm -f "$path"
  cat > "$path" <<'SHIM'
#!/usr/bin/env bash
# superlooper-cli-shim (bin/install.sh, issue #31)
# A stable `superlooper` on PATH. Execs the PUBLISHED skill copy, never a source checkout.
# Re-created idempotently on every publish — do not edit; edits are overwritten.
exec "$HOME/.claude/skills/superlooper/bin/superlooper" "$@"
SHIM
  chmod 755 "$path"
}

# --- choose the target dir: first candidate that is on PATH AND writable; else the preferred fallback.
chosen=""
for dir in "${CANDIDATES[@]}"; do
  if on_path "$dir" && usable "$dir"; then
    chosen="$dir"; break
  fi
done
[ -n "$chosen" ] || chosen="$PREFERRED"
target="$chosen/superlooper"

# A real directory occupying the target is pathological (bin dirs hold executables, not dirs). Do
# NOT `rm -rf` a path we computed — leave it untouched, print the fix, and exit 0 so a successful
# publish is never reported as failed over this. (A SYMLINK named superlooper is not caught here;
# write_shim's `rm -f` removes the link cleanly without touching its target.)
if [ -d "$target" ] && [ ! -L "$target" ]; then
  echo "[install-cli-link] WARNING: a directory occupies $target — cannot install the shim there." >&2
  echo "[install-cli-link] Remove that directory and re-run bin/install.sh" \
       "(the skill itself is already published)." >&2
  exit 0
fi

# --- classify what we are about to do, for an honest report (and to never clobber silently).
action="linked"
if [ -e "$target" ] || [ -L "$target" ]; then
  if is_our_shim "$target"; then
    action="refreshed"                # our own shim already here; rewrite keeps it byte-identical
  else
    action="replaced"                 # a foreign `superlooper` — the name is ours, but say so loudly
  fi
fi

write_shim "$target"

# --- sweep a stale shim WE wrote into a different candidate dir on a prior run, so exactly one
#     superlooper shim is ever on PATH. Marker-guarded: a foreign binary named `superlooper` is left
#     untouched. Removal is best-effort — a dir we can't delete from just keeps its (harmless,
#     same-target) shim.
for dir in "${CANDIDATES[@]}"; do
  other="$dir/superlooper"
  [ "$other" = "$target" ] && continue
  if is_our_shim "$other"; then
    if rm -f "$other" 2>/dev/null; then
      echo "[install-cli-link] removed stale shim -> $other"
    fi
  fi
done

# --- report using the shell's ACTUAL resolution, not an inference: after writing + sweeping, ask
#     what `superlooper` now resolves to on this PATH. This keeps "resolves now" honest even when a
#     DIFFERENT superlooper earlier on PATH (in any dir, candidate or not) would shadow our shim.
hash -r 2>/dev/null || true
resolved="$(command -v superlooper 2>/dev/null || true)"

if [ "$action" = replaced ]; then
  echo "[install-cli-link] NOTE: replaced an existing non-shim entry at $target (its name is ours)."
fi
echo "[install-cli-link] $action superlooper -> $INSTALLED_LITERAL"
echo "[install-cli-link]   shim: $target"
if [ ! -e "$INSTALLED_REAL" ]; then
  echo "[install-cli-link]   note: $INSTALLED_REAL is not present yet — publish the skill via" \
       "bin/install.sh so the command resolves."
fi
if [ "$resolved" = "$target" ]; then
  echo "[install-cli-link]   \`superlooper\` resolves now -> this shim" \
       "(open a new shell if $chosen was just created)."
elif [ -n "$resolved" ]; then
  echo "[install-cli-link] WARNING: wrote the shim to $target, but \`superlooper\` currently" \
       "resolves to $resolved — something earlier on your PATH shadows it." >&2
  echo "[install-cli-link] Put $chosen ahead of it on PATH, or remove the other superlooper." >&2
else
  echo "[install-cli-link] WARNING: $chosen is not on your PATH — \`superlooper\` will not resolve" \
       "until you add it." >&2
  echo "[install-cli-link] Add it: append this to your shell profile (e.g. ~/.zshrc), then open a" \
       "new shell:" >&2
  echo "[install-cli-link]   export PATH=\"$chosen:\$PATH\"" >&2
fi
