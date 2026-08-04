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

_superlooper_launch_shim() {
  emulate -L zsh                                        # local options; don't disturb the user shell

  # Only for a pane THIS loop launched. These three are set by lib/launch.py's pane_environment
  # and by nothing else, so an ordinary terminal, an unrelated pane and the operator's own window
  # can never match — and the test is HOST-AGNOSTIC on purpose. Keying on the host's own injected
  # pane variable would have read the same, but it would have taught this file the host's name for
  # no gain, and the one-doorway fence (tests/test_one_session_host_door.py) is right to refuse
  # that: swapping the host must stay a rewrite of the wrapper alone.
  [[ -n "${SL_ISSUE_ID:-}" && -n "${SL_RUN_ROOT:-}" && -n "${SL_START_SESSION:-}" ]] || return 0
  # SL_START_SESSION is the one of the three that start-session.sh UNSETS before it launches the
  # agent, which is what stops a nested shell inside a live session (a worker typing `zsh`) from
  # re-arming the handoff and launching a second worker into its own lane.

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
  # `unfunction` runs FIRST, before anything that can fail: a body that errored while still defined
  # would be re-entered by the next `claude` and try to launch a second worker into the same lane.
  #
  # NOT `exec`: running start-session.sh as a child and returning afterwards is what leaves the
  # pane at an interactive shell when the session ends, so its transcript can be scrolled and the
  # printed `claude --resume <id>` pasted. The cmux path made the same choice for the same reason,
  # and #168 ("a live session is the owner's to inspect") rests on it.
  functions[$agent]="unfunction $agent; command ${(q)SL_START_SESSION} ${(q)SL_ISSUE_ID}"
}

_superlooper_launch_shim
