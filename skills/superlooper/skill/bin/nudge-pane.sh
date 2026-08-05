#!/usr/bin/env bash
# The SINGLE safe write into any lane's live session — the runner's resume/answer/nudge/probe/
# wake-ping path. See docs/founding/EVENT-MODEL.md.
#
# Usage: nudge-pane.sh <id> <message>
#   <id> is a loop session id (i<N> worker, d<N> debugger) AND the session host's agent name for
#   it. There is NO surface argument any more (issue #334): #308 moved every spawn onto the
#   five-verb wrapper, so a recorded pane handle names something cmux never issued — and the
#   wrapper addresses agents by NAME, never by a cached id, because a pane that moves gets a new id
#   and the old one stops resolving (the dragged-anchor incident class). A caller cannot pass a
#   handle to a path that has nowhere to put one.
#
#   There is no orchestrator here either — the deterministic runner is a normal process, never an
#   agent pane — so autocode's "orchestrator" special case stays dropped. (The `orchestrator=`
#   parameter survives, unused, in lib/pane_state.py, still unit-tested there.)
#
# All the decisions live in lib/nudge.py: the doorway calls, the exit-code contract, the delivery
# oracle and the evidence line. This file is the entry point and nothing else — the logic moved to
# a module so it can be unit-tested against a staged host instead of only end to end through a
# stub binary, which is precisely the blindness #334 exists to close.
#
# EXIT CODES are the runner's contract and are documented in full in lib/nudge.py:
#   0 sent (and PROVEN delivered)   1 failed   3 deferred (nothing was typed)
#   4 dead   5 auth dead in-session   6 at its own dialog
#   7 submitted, delivery unproven — the one code that does NOT mean "nothing was typed"
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/../lib/nudge.py" "$@"
