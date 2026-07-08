# superlooper launch shim — sourced from ~/.zshrc (cmux sources ~/.zshrc in every terminal via its
# ZDOTDIR shell-integration). Lets a freshly-created cmux tab SELF-LAUNCH its superlooper worker
# WITHOUT any cmux keystroke delivery.
#
# WHY THIS EXISTS (run-20260626-1656 post-mortem, RC6): launching a worker used to be `cmux send`
# + `send-key Enter` into the new tab. cmux exposes NO keystroke-free "run a command in a surface"
# API (only surface.send_text / surface.send_key), and macOS GATES that keystroke delivery to a
# fresh/background tab when the display sleeps or the app is backgrounded — so overnight Wave-2
# launches never reached the new tabs even though the Mac never locked. The shell cmux spawns at
# surface creation, however, runs as a normal child process REGARDLESS of display state. So instead
# of typing the launch command in, launch-session.sh drops it in a file keyed by the new tab's
# surface UUID, and this shim — running in that tab's shell — reads and runs it. No keystrokes, no
# display dependency, no lock dependency.
#
# It is a strict no-op for every normal shell: it only acts when (a) this is a cmux surface AND
# (b) superlooper has dropped a command file for THIS exact surface. Outside an active launch it
# returns instantly. (While a launch is actively in flight, a hand-opened terminal may wait up to
# the bounded ceiling below — ~5s — for a command that never comes, then return; it can NEVER run
# another tab's command, since the file is keyed by this shell's own CMUX_SURFACE_ID.)
#
# The marker dir is ~/.superlooper/launch — DISTINCT from autocode's ~/.autocode/launch — so both
# shims can be installed at once and each is a strict no-op for the other's launches (plan §B.5).

_superlooper_launch_shim() {
  emulate -L zsh                                        # local options; don't disturb the user shell
  [[ -n "${CMUX_SURFACE_ID:-}" ]] || return 0           # only inside a cmux terminal
  local dir="${SL_LAUNCH_DIR:-$HOME/.superlooper/launch}"
  local cmd="$dir/${CMUX_SURFACE_ID}.cmd"

  if [[ ! -f "$cmd" ]]; then
    # No command for this tab yet. Return INSTANTLY unless a launch is actively in flight — so a
    # hand-opened terminal is never delayed. A launch in flight is signalled by a FRESH .active
    # marker; launch-session.sh writes the command file within milliseconds of creating the surface,
    # but this shell can win the race to here, so we briefly wait for it.
    local active="$dir/.active"
    [[ -f "$active" ]] || return 0
    local now mt
    now=$(command date +%s 2>/dev/null) || return 0
    mt=$(command stat -f %m "$active" 2>/dev/null) || return 0
    (( now - mt <= 60 )) || return 0                    # stale marker => no launch really running
    local i=0 max="${SL_SHIM_WAIT_TICKS:-25}"           # 25 * 0.2s = 5s ceiling (file is dropped before any other RPC, so it appears in ~1s)
    while [[ ! -f "$cmd" && i -lt max ]]; do command sleep 0.2; (( i++ )); done
    [[ -f "$cmd" ]] || return 0
  fi

  # Claim the command atomically (so a re-source can never double-launch), read it, then run it.
  # We do NOT `exec`: running it as a child and returning afterward preserves the existing behavior
  # where the tab drops back to an interactive shell when the session ends (scroll up / claude
  # --resume). The command was built by launch-session.sh with bash %q quoting, so run it under bash.
  local claimed="$cmd.claimed.$$"
  command mv -f -- "$cmd" "$claimed" 2>/dev/null || return 0   # lost the race to another claimer
  local script
  script="$(command cat -- "$claimed" 2>/dev/null)"
  command rm -f -- "$claimed" 2>/dev/null
  [[ -n "$script" ]] || return 0
  command bash -c "$script"
}

_superlooper_launch_shim
