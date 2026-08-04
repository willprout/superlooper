# superlooper launch shim — sourced from ~/.zshrc, and therefore by the login shell the session
# host spawns in every new pane. It is what keeps the ENTIRE in-pane launch floor ours after the
# move onto the host's own `agent start` verb (issue #308).
#
# WHAT THE HOST'S START VERB ACTUALLY DOES (measured while building #308 — see reports/i308.md):
# it TYPES the bare agent word — `claude`, `codex` — into the pane's own interactive shell, then
# judges readiness from the SCREEN. It never runs a command of the launcher's choosing: the verb
# takes a name, a kind, a pane and native agent args, and nothing else.
#
# So without this shim the host would start a bare agent and the whole floor would go with it: the
# worker singleton, the launch-floor env scrub (#301), the positive gh-auth assert (#299), the
# claude binary pin (#303), the stderr tail the relaunch-cap park memo reads (#40), and the exited
# marker the runner recovers from (RC-DEADPANE). Every one of those MUST run inside the pane — the
# pane's shell sources the operator's own rc files AFTER the launcher has finished, which is
# exactly where the realized ANTHROPIC_API_KEY lived, so a launcher that scrubbed ITSELF would
# prove nothing about the environment a worker actually gets.
#
# The shim therefore ARMS a one-shot shell function named after the agent verb. The host types the
# verb; the function runs start-session.sh instead; start-session.sh does the floor and launches
# the real agent with every flag it already owned (the agent-boundary rule: the launch command line
# lives there and nowhere else, which is also why this file passes no flags of its own). The host's
# screen-based detection then sees the agent's own TUI and reports the pane ready, exactly as if it
# had started the agent itself — verified end to end.
#
# It is a strict no-op for every normal shell, and instant: it acts only when THIS launch put its
# own SL_* into that pane's environment. A hand-opened terminal, an unrelated pane, and the
# operator's own window are all untouched — and unlike the cmux command-file shim this replaces,
# there is no waiting, no marker directory and no race to lose.
#
# ONE-SHOT, and that matters: the function removes itself before handing off, so a `claude` the
# operator types in that pane afterwards is the real binary and not a second worker. The worker
# singleton inside start-session.sh is the backstop underneath it.

# ------------------------------------------------------------------------------------------------
# The shim has TWO independent halves, and each is a strict no-op for the other's case.
#
#   1. `_superlooper_launch_shim`  — the SESSION handoff above (issue #308), used by every worker
#      and every sl-debugger, on the session host.
#   2. `_superlooper_command_shim` — the cmux command-file mechanism below, which now has exactly
#      ONE remaining writer: bin/resurrect-runner.sh, the watchdog's restart of a provably-gone
#      runner in its recorded cmux pane. It is not a session launch and it is not this issue's to
#      retire; it goes when cmux does (the runner's own home is issue #306, the cutover #311).
# ------------------------------------------------------------------------------------------------

_superlooper_launch_shim() {
  emulate -L zsh                                        # local options; don't disturb the user shell

  # Only for a pane THIS loop launched. These three are set by lib/launch.py's pane_environment
  # and by nothing else, so an ordinary terminal, an unrelated pane and the operator's own window
  # can never match — and the test is HOST-AGNOSTIC on purpose. Keying on the host's own injected
  # pane variable would have read the same, but it would have taught this file the host's name for
  # no gain, and the one-doorway fence (tests/test_one_session_host_door.py) is right to refuse
  # that: swapping the host must stay a rewrite of the wrapper alone.
  [[ -n "${SL_ISSUE_ID:-}" && -n "${SL_RUN_ROOT:-}" && -n "${SL_START_SESSION:-}" ]] || return 0
  # SL_START_SESSION is the one of the three that gets DISARMED once the handoff has fired: the
  # function below unsets it in the pane's own login shell before handing over, and
  # start-session.sh unsets it again in its own process. Both are needed and they cover different
  # shells. Without the first, a shell started FROM THE PANE PROMPT — the operator poking at a
  # finished session, or an `exec zsh` — still inherits it from the login shell, re-arms, and its
  # next `claude` starts a whole second flight for the lane: it takes the now-free worker lock,
  # re-reads the lane brief, and asks for a `--session-id` that has already been used. Without the
  # second, the same is true of anything the agent itself spawns.

  local agent="${SL_AGENT:-claude}"
  case "$agent" in
    claude|codex) ;;
    *) return 0 ;;                                      # an agent this stack cannot launch
  esac

  if [[ ! -x "${SL_START_SESSION}" ]]; then
    # LOUD, never silent — but deliberately NOT fatal to the shell. Without the handoff the host
    # would start a bare agent with no floor, no brief and no singleton: a session that looks alive
    # and is not this loop's. Leaving the function unarmed is what makes that diagnosable, because
    # no start sentinel is then stamped and the launcher refuses the delivery (rc=2, naming the
    # shim) instead of recording a live lane.
    print -u2 "[superlooper] launch handoff unavailable: SL_START_SESSION=${SL_START_SESSION} is not an executable file"
    return 0
  fi

  # Arm the handoff. Defined through `functions[...]` rather than an `eval`ed heredoc so the body
  # is one quoted string with no parsing surprises, and the two values are ${(q)}-quoted so a state
  # home or engine path containing a space or a quote can neither break nor inject it.
  #
  # `unfunction` and the disarm run FIRST, before anything that can fail: a body that errored while
  # still armed would be re-entered by the next `claude` and try to launch a second worker into the
  # same lane. The path itself is expanded at DEFINITION time, so unsetting the variable at run
  # time disarms the shell without taking the handoff's own target with it.
  #
  # NOT `exec`: running start-session.sh as a child and returning afterwards is what leaves the
  # pane at an interactive shell when the session ends, so its transcript can be scrolled and the
  # printed `claude --resume <id>` pasted. The cmux path made the same choice for the same reason,
  # and #168 ("a live session is the owner's to inspect") rests on it.
  functions[$agent]="unfunction $agent; unset SL_START_SESSION; command ${(q)SL_START_SESSION} ${(q)SL_ISSUE_ID}"
}


_superlooper_command_shim() {
  # THE RUNNER'S OWN RESURRECTION, and nothing else (issue #208). A dropped command file keyed by
  # this tab's surface uuid, run by the fresh shell cmux spawns at surface creation.
  #
  # WHY A DROPPED FILE (run-20260626-1656 post-mortem, RC6): cmux exposes NO keystroke-free "run a
  # command in a surface" API — only send_text / send_key — and macOS GATES that keystroke delivery
  # to a fresh or background tab while the display sleeps, which is exactly the 1am scenario a
  # runner resurrection exists for. The shell cmux spawns, however, runs as a normal child process
  # regardless of display state. Sessions no longer need any of this (the host runs the agent
  # itself), so this half survives for one caller and retires with cmux.
  emulate -L zsh
  [[ -n "${CMUX_SURFACE_ID:-}" ]] || return 0           # only inside a cmux terminal
  local dir="${SL_LAUNCH_DIR:-$HOME/.superlooper/launch}"
  local cmd="$dir/${CMUX_SURFACE_ID}.cmd"

  if [[ ! -f "$cmd" ]]; then
    # No command for this tab. Return INSTANTLY unless a launch is actively in flight, so a
    # hand-opened terminal is never delayed. A fresh `.active` marker signals one; the command file
    # is written within milliseconds of the surface, but this shell can win the race to here.
    local active="$dir/.active"
    [[ -f "$active" ]] || return 0
    local now mt
    now=$(command date +%s 2>/dev/null) || return 0
    mt=$(command stat -f %m "$active" 2>/dev/null) || return 0
    (( now - mt <= 60 )) || return 0                    # stale marker => no launch really running
    local i=0 max="${SL_SHIM_WAIT_TICKS:-25}"           # 25 * 0.2s = 5s ceiling
    while [[ ! -f "$cmd" && i -lt max ]]; do command sleep 0.2; (( i++ )); done
    [[ -f "$cmd" ]] || return 0
  fi

  # Claim atomically (so a re-source can never double-launch), read, then run. NOT `exec`, for the
  # same reason as the handoff above: the tab drops back to an interactive shell afterwards. The
  # command was built with bash %q quoting, so it runs under bash.
  local claimed="$cmd.claimed.$$"
  command mv -f -- "$cmd" "$claimed" 2>/dev/null || return 0   # lost the race to another claimer
  local script
  script="$(command cat -- "$claimed" 2>/dev/null)"
  command rm -f -- "$claimed" 2>/dev/null
  [[ -n "$script" ]] || return 0
  command bash -c "$script"
}

_superlooper_launch_shim
_superlooper_command_shim
