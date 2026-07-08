#!/usr/bin/env bash
# Runs INSIDE each session's cmux pane. Seeds an interactive coding-agent session with the brief.
# When the agent exits, the pane's shell persists (nothing auto-closes).
# SL_ISSUE_ID + SL_RUN_ROOT arrive in the environment (set by launch-session.sh's --command prefix);
# re-export them so agent hook children inherit them.
#
# <id> is the loop id: i<N> for an issue worker, a<N> for an answerer (both share this launcher and
# the same marker discipline; the runner tells them apart, not this script).
set -uo pipefail
ID="${1:?usage: start-session.sh <id>}"
: "${SL_RUN_ROOT:?}"
export SL_ISSUE_ID="$ID"
export SL_RUN_ROOT
mkdir -p "$SL_RUN_ROOT/state/started" "$SL_RUN_ROOT/state/exited"

write_exited() {  # the deterministic process-gone signal the runner recovers from (RC-DEADPANE)
  printf '%s rc=%s\n' "$(date +%s)" "${1:-0}" > "$SL_RUN_ROOT/state/exited/$ID"
}

# WORKER SINGLETON per id (RC-WORKER-SINGLETON). Guarantee AT MOST ONE Claude worker per id /
# worktree, no matter (a) how the launch keystrokes buffered and flushed — a locked Mac buffers the
# `cd … && start-session.sh` line and flushes it on unlock, and a re-launched tab would otherwise add
# a SECOND copy → two workers clobbering one branch — or (b) a frozen-recovery RESTART relaunching a
# tab while the old session is still alive. The lock is a FILE created atomically WITH its pid via
# `ln` of a fully-written temp (NOT mkdir-then-write-pid, whose empty-pid window let a racer read an
# empty pid, call it stale, and double-acquire — codex verify P1-a). `ln` fails if the lock exists,
# so it is the exclusive primitive; the pid gives liveness (a dead holder is reclaimed). A duplicate
# that finds a LIVE holder exits 0 (idempotent) WITHOUT stamping the start sentinel, so
# launch-session.sh does not mistake this tab for the real worker — it times out and closes it.
WLOCK="$SL_RUN_ROOT/state/worker.$ID.lock"
acquire_worker() {
  local tries=0 opid tmp
  while [ "$tries" -lt 50 ]; do
    tmp="$(mktemp "$SL_RUN_ROOT/state/worker.$ID.XXXXXX")" || return 1
    echo "$$" > "$tmp"                                   # full content BEFORE it becomes the lock
    if ln "$tmp" "$WLOCK" 2>/dev/null; then rm -f "$tmp"; return 0; fi   # atomic create-with-content
    rm -f "$tmp"
    opid="$(cat "$WLOCK" 2>/dev/null || true)"
    if [ -n "$opid" ] && kill -0 "$opid" 2>/dev/null; then return 1; fi  # a LIVE worker owns it
    # dead/unreadable holder -> reclaim, but only if the lock STILL names that dead pid (so we never
    # remove a fresh lock a concurrent claimer just won between our read and our remove).
    if [ "$(cat "$WLOCK" 2>/dev/null || true)" = "$opid" ]; then rm -f "$WLOCK" 2>/dev/null || true; fi
    tries=$((tries + 1))
  done
  return 1
}
release_worker() {   # ownership-checked: remove the lock ONLY if it still names US (codex verify
  # P1-a: an unconditional trap could delete a DIFFERENT worker's lock after a reclaim race).
  [ "$(cat "$WLOCK" 2>/dev/null || true)" = "$$" ] && rm -f "$WLOCK" 2>/dev/null || true
}
if ! acquire_worker; then
  echo "[$ID] a live worker is already running for this id — not starting a second (idempotent)." >&2
  exit 0
fi
trap release_worker EXIT                     # free the slot when THIS worker truly ends (only ours)

# DELIVERY PROOF (RC-LAUNCHVERIFY — the run-20260625-1857 overnight killer). Now that we hold the
# worker lock and are about to start Claude, stamp the PER-LAUNCH start marker. Its NAME carries the
# launch token (this tab's surface UUID, passed by launch-session.sh), so it is unique to THIS launch
# — concurrent or stale launches use different filenames and cannot stomp each other's proof (codex
# verify P1-b: a single shared state/started/<id> let an overlapping launch's hygiene delete the
# proof and the real launch then close its OWN worker tab). Stamped before the brief check so
# delivery is proven even if the brief is missing — that branch writes the exited marker so the
# dead state is observed promptly, not 45 min later.
TOKEN="${SL_START_TOKEN:-$ID}"
printf '%s' "$TOKEN" > "$SL_RUN_ROOT/state/started/$ID.$TOKEN"
BRIEF="$SL_RUN_ROOT/briefs/$ID.md"
[ -f "$BRIEF" ] || { echo "[$ID] no brief" >&2; write_exited 1; exit 1; }
# Name the session so William can tell what's running when he's away:
#   --name           -> local terminal/tab title + /resume picker
#   --remote-control -> ENABLES + labels this session on the Remote Control dashboard
# These are independent (Claude Code docs): --name does NOT set the dashboard label, so we pass
# both. Without --name, Claude would overwrite the tab title with its own auto-summary.
NAME="${SL_SESSION_NAME:-superlooper $ID}"
# Model comes from config via the runner (default opus[1m] for both; per-repo override in config.models); passed in SL_MODEL. Guard
# the unbound/empty case under `set -u` so a missing model never aborts the launch — omit --model and
# let Claude use its default rather than pass `--model ""`.
MODEL="${SL_MODEL:-}"
# Effort comes from a per-issue effort:* label or the repo-wide models.worker_effort default (the
# runner resolves precedence and sends SL_EFFORT="" when neither is set). Pass --effort ONLY when
# non-empty — no forced default when it's empty. Same %q-quoted stack as --model.
EFFORT="${SL_EFFORT:-}"
AGENT="${SL_AGENT:-claude}"

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

toml_string() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

case "$AGENT" in
  claude)
    CLAUDE_ARGS=(--dangerously-skip-permissions)
    [ -n "$MODEL" ] && CLAUDE_ARGS+=(--model "$MODEL")
    [ -n "$EFFORT" ] && CLAUDE_ARGS+=(--effort "$EFFORT")
    CLAUDE_ARGS+=(--name "$NAME" --remote-control "$NAME")
    # Do NOT pipe Claude through tee/cat — piping drops it into print mode and kills the
    # interactive pane you want to watch. (No headless `claude -p` anywhere — owner billing rule B.9.)
    claude "${CLAUDE_ARGS[@]}" "$(cat "$BRIEF")"
    ;;
  codex)
    WORKTREE="$(pwd -P)"
    CODEX_ARGS=(-C "$WORKTREE")
    if truthy "${SL_CODEX_NO_ALT_SCREEN:-1}"; then CODEX_ARGS=(--no-alt-screen "${CODEX_ARGS[@]}"); fi
    if truthy "${SL_CODEX_DANGEROUS_BYPASS:-}"; then
      CODEX_ARGS+=(--dangerously-bypass-approvals-and-sandbox)
    fi
    if truthy "${SL_CODEX_BYPASS_HOOK_TRUST:-1}"; then
      CODEX_ARGS+=(--dangerously-bypass-hook-trust)
    fi
    [ -n "$MODEL" ] && CODEX_ARGS+=(-m "$MODEL")
    [ -n "$EFFORT" ] && CODEX_ARGS+=(-c "model_reasoning_effort=$(toml_string "$EFFORT")")
    # Interactive Codex, not `codex exec`; the brief is the initial prompt.
    codex "${CODEX_ARGS[@]}" "$(cat "$BRIEF")"
    ;;
  *)
    echo "[$ID] unsupported agent '$AGENT' (expected: claude or codex)" >&2
    write_exited 64
    exit 64
    ;;
esac
rc=$?
# Deterministic crash/quit/limit signal (RC-DEADPANE): when the agent process returns to the
# shell, write state/exited/<id> with the real exit code. The runner emits session_exited from
# this marker (recovered by RESTART), and nudge-pane.sh refuses to type into the now-bash pane —
# so a runner "resume" can never execute as a permission-bypassed shell command. The EXIT
# trap then frees the worker lock so a legitimate restart can take this id over.
write_exited "$rc"
echo
resume_cmd="claude --resume"
[ "$AGENT" = "codex" ] && resume_cmd="codex resume"
echo "[$ID] session ended $(date '+%H:%M') rc=$rc — scroll up to inspect, or: $resume_cmd"
