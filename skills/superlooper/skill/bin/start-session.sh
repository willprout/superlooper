#!/usr/bin/env bash
# Runs INSIDE each session's cmux pane. Seeds an interactive coding-agent session with the brief.
# When the agent exits, the pane's shell persists — THIS launcher never closes it. (The runner
# auto-closes a lane's window after a SUCCESSFUL merge — owner ruling 2026-07-16 / #168, config
# auto_close_merged_windows — and never while a parked/needs-william/bounced session is still live;
# see runner.py _teardown_session / _reclaim_terminal_worktrees.)
# SL_ISSUE_ID + SL_RUN_ROOT arrive in the environment (set by launch-session.sh's --command prefix);
# re-export them so agent hook children inherit them.
#
# <id> is the loop id: i<N> for an issue worker, d<N> for a debugger (both share this launcher and
# the same marker discipline; the runner tells them apart, not this script).
set -uo pipefail
ID="${1:?usage: start-session.sh <id>}"
: "${SL_RUN_ROOT:?}"
export SL_ISSUE_ID="$ID"
export SL_RUN_ROOT
mkdir -p "$SL_RUN_ROOT/state/started" "$SL_RUN_ROOT/state/exited" "$SL_RUN_ROOT/state/launch_stderr"

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

# ---- LAUNCH-FLOOR ENV SCRUB + POST-SCRUB ASSERT (issue #301, tool-dive claim c1) ---------------
# A session inherits the FULL parent environment, and three inherited variables each tell the loop a
# confident lie rather than raising an error:
#   * ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL flip the session from Max-subscription billing to API
#     billing (or to somebody else's gateway) with no error, no signal, just a bill. Two tool
#     codebases document the flip (tool-dive c1) and it is REALIZED in our own lab: the 2026-07-30
#     clean-room checkpoint found a LIVE key at the owner's ~/.zshrc:5
#     (docs/SPIKES-2026-07-30-supervised.md §0.3).
#   * inherited CLAUDE_CODE_* / CLAUDECODE turn TRANSCRIPT SAVING OFF ("⚠ Transcript saving is off
#     — inherited CLAUDE_CODE…"). No transcript means nothing to `--resume`, so the entire #298
#     resurrection floor evaporates silently (docs/SPIKES-2026-07-29-results.md).
#   * XDG_CONFIG_HOME points `gh` at a config dir with no hosts.yml, and it keeps answering
#     confidently as nobody — the same spike's second landmine.
#
# THIS FILE, and no other. The tab's shell SOURCES ~/.zshrc on the way up — which is exactly where
# the realized key lived — so the poison is injected AFTER the launcher has finished and BEFORE this
# script runs. A launcher that scrubbed its own environment would prove nothing about the one the
# worker actually gets. Same reason the gh-auth assert lives here (#299), one step earlier in the
# chain. Above the agent branch, so claude and codex are covered by construction, and shared by
# `launch-session.sh --cwd d<N>`, so both spawn paths are the same code rather than two copies.
#
# DENYLIST, never an aggressive allowlist. c25's documented landmine is that overriding HOME breaks
# macOS keychain OAuth and kills the standalone story outright; PATH, HOME and the keychain context
# a session legitimately needs must survive untouched. (Per-worker CLAUDE_CONFIG_DIR / GH_CONFIG_DIR
# is the two-subscription spike's business — issue #300 — and is deliberately NOT touched here: this
# floor removes poison, it does not assign identity.)
sl_env_names() {
  # NAMES ONLY, read from a CHILD process — `env` is what the agent's own process will be handed.
  # A value may contain newlines and `=`, so only a line that OPENS with a valid shell identifier
  # followed by `=` names a variable; a continuation line that happens to look like one costs a
  # false REFUSAL, which fails closed, never a false pass.
  env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'
}
sl_is_poison() {                             # one predicate for BOTH the scrub and the assert, so
  case "$1" in                               # the assert can never cover less than the scrub strips
    ANTHROPIC_*|CLAUDE_CODE_*|CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|XDG_CONFIG_HOME) return 0 ;;
    *) return 1 ;;
  esac
}
# The scrub. Whole families, not a fixed list of names: a new CLAUDE_CODE_* or ANTHROPIC_* variable
# shipped by a future release is poison the day it appears, and a list would learn about it from a
# bill. Nothing in this loop passes model or billing configuration through the environment — the
# launcher NAMES everything a session needs as SL_*, and --model/--effort are command-line flags —
# so no legitimate input is lost. (An ADOPTED repo whose own test suite calls the Anthropic API
# reads its key from that repo's config the way any other secret arrives; inheriting one through
# the worker's environment is the same door this floor exists to close, and there is deliberately
# no config key to re-open it — a floor with an off switch is not a floor.) `unset` failures are
# NOT silenced: bash's own "cannot unset: readonly variable" names the mechanism the assert below
# can only ever observe the result of.
for _sl_n in $(sl_env_names); do
  if sl_is_poison "$_sl_n"; then unset "$_sl_n"; fi
done
unset _sl_n
# The assert. NOT a restatement of the scrub: it re-reads the environment from a fresh child, which
# is the same thing the agent process will be handed, so it catches what the scrub could not — a
# readonly variable `unset` refused to remove, a re-injection from anything sourced in between, and
# a scrub some future edit moved, narrowed or deleted. A scrub that rots is silent by nature; this
# is what makes it loud.
#
# It runs BEFORE the gh-auth assert deliberately. A poisoned env is CAUSALLY UPSTREAM of the auth
# death it produces (XDG_CONFIG_HOME is exactly how gh dies), so probing first would park the issue
# under a memo telling the owner to re-login — a confidently wrong remedy for an environment fault.
# It is also network-free, so it costs a healthy launch nothing.
ENV_DEAD_RC=6                                # launch-session.sh + evidence.py read this same code
TOKEN="${SL_START_TOKEN:-$ID}"               # per-launch key, shared with the auth marker below
refuse_env() {                               # loud, torn down, and NAMED — never a live poisoned session
  local why="$1"
  mkdir -p "$SL_RUN_ROOT/state/envfail"
  # The launcher is sitting in its delivery-verify window waiting on a start sentinel that will now
  # never appear. This per-launch marker (same token discipline as the sentinel, so a stale or
  # overlapping launch cannot false-fire it) is what lets it tear the tab down AT ONCE and blame the
  # environment, instead of timing out 30s later and blaming the launch shim.
  printf '%s\n' "$why" > "$SL_RUN_ROOT/state/envfail/$ID.$TOKEN"
  echo "[$ID] ENV POISONED: $why" >&2
  # Deliberately NO exited marker, for the same reason the auth refusal writes none: that marker
  # means "a worker WAS running and its process is gone", and the runner recovers from it by
  # RELAUNCHING — straight back into the same poisoned environment.
  exit "$ENV_DEAD_RC"
}
SL_STILL_POISONED=""
for _sl_n in $(sl_env_names); do
  if sl_is_poison "$_sl_n"; then SL_STILL_POISONED="${SL_STILL_POISONED:+$SL_STILL_POISONED }$_sl_n"; fi
done
unset _sl_n
if [ -n "$SL_STILL_POISONED" ]; then
  refuse_env "the launch env scrub did not remove: $SL_STILL_POISONED — these still reach this session's own process, so it could be billed to an API key instead of the subscription, or start with transcript saving off (which silently breaks --resume). Find where they are exported (a shell rc file, a LaunchAgent, a wrapper) and remove them; if a variable is readonly, whatever made it so is sourced before this launcher"
fi

# ---- POSITIVE gh-AUTH ASSERT (issue #299) ------------------------------------------------------
# THIS is the only code in the stack that runs inside the spawned session's OWN environment and
# worktree, which is exactly why the assert lives here and not in the launcher: the fresh tab
# inherits nothing, so a launcher that checks ITSELF proves nothing about the env the worker is
# actually about to run in. The 2026-07-29 spikes showed an inherited XDG_CONFIG_HOME
# de-authenticating `gh` while it keeps answering, and herdr's tracker carries an unresolved
# keychain-in-launchd incident that presents as intermittent gh auth death (tool-dive c27).
#
# A worker whose gh auth has died does not stop — it works on CONFIDENTLY: it cannot read its own
# issue, cannot post evidence, cannot open its PR, and every one of those failures reads downstream
# as worker misbehavior instead of auth death. So the flight never starts.
#
# POSITIVE means the assert must OBSERVE SUCCESS: a real authenticated read (`gh api user`) that
# answers with the login this loop runs as. Absence of an error is not auth — a `gh` answering
# happily as somebody else's account is a worker that would write labels and comments under a
# stranger's identity, so the login is COMPARED, never merely "did it exit 0".
#
# Runs ABOVE the agent branch: gh auth is agent-independent, so claude and codex are covered by
# construction rather than by a second copy of the check that could drift.
# Runs BEFORE the start sentinel below on purpose. The sentinel means "a worker started here"; a
# refused session never became one, and stamping it would tell the launcher the launch succeeded
# moments before this script exits with no agent — a lane the runner would then read as live.
#
# Runs AFTER the env floor above, and depends on it: the 2026-07-29 landmine that kills gh here is
# an inherited XDG_CONFIG_HOME, which the scrub has already removed — so a refusal at this point is
# a real credential fault, not an environment one wearing its costume.
GH="${SL_GH:-gh}"
EXPECT_LOGIN="${SL_EXPECT_GH_LOGIN:-}"       # TOKEN was resolved by the env floor above (same key)
AUTH_DEAD_RC=4                               # launch-session.sh + evidence.py read this same code
# BOUNDED, and the bound matters MORE here than on the launcher side. `gh` has no default request
# timeout, so an unreachable network can hang this read for tens of seconds — and the launcher is
# meanwhile counting down its 30s delivery-verify window. Overrun it and the launcher exits rc=2
# (`shim_not_fired`, a CHANNEL fault) and the memo blames the launch shim: the exact mis-blame the
# auth marker was added to prevent, reached through the SLOW path instead of the dead one. Two
# attempts at this bound stay comfortably inside the window.
# Deliberately duplicated from launch-session.sh rather than sourced: this script runs in a fresh
# tab shell that inherits nothing and knows no engine paths, so a sourced helper would add a
# failure mode exactly where the stack is least able to report one.
GH_PROBE_SECONDS="${SL_GH_PROBE_SECONDS:-8}"
gh_login() {                                 # one real authenticated read; stdout = login or error
  # Polled in TENTHS: the child is always still alive at the first check, so a 1s granularity would
  # charge every healthy launch a full second of dead wait on BOTH sides of the spawn. Both BSD and
  # GNU sleep accept fractions. (`rc=0; wait || rc=$?` is the errexit-safe spelling its launcher
  # twin needs; kept identical here so the two cannot drift.)
  local out_file pid ticks=0 limit rc
  case "$GH_PROBE_SECONDS" in ""|*[!0-9]*) GH_PROBE_SECONDS=8 ;; esac   # a typo must not mean "refuse"
  limit=$((GH_PROBE_SECONDS * 10))
  out_file="$(mktemp "${TMPDIR:-/tmp}/sl-ghwho.XXXXXX")" || return 1
  "$GH" api user --jq .login > "$out_file" 2>&1 &
  pid=$!
  while [ "$ticks" -lt "$limit" ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.1 2>/dev/null || sleep 1   # a sleep without fractions must cost
    ticks=$((ticks + 1))                # granularity, never a refused launch
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true          # OUR pid, captured from $! — never a name/pattern kill
    wait "$pid" 2>/dev/null || true
    printf 'no answer within %ss (gh did not return)' "$GH_PROBE_SECONDS" > "$out_file"
    rc=124
  else
    rc=0; wait "$pid" || rc=$?
  fi
  cat "$out_file"; rm -f "$out_file"
  return "$rc"
}
refuse_auth() {                              # loud, torn down, and NAMED — never a live dead-auth session
  local why="$1"
  mkdir -p "$SL_RUN_ROOT/state/authfail"
  # The launcher is sitting in its delivery-verify window waiting on the start sentinel that will
  # now never appear. This per-launch marker (same token discipline as the sentinel, so a stale or
  # overlapping launch cannot false-fire it) is what lets it tear the tab down AT ONCE and blame
  # auth, instead of timing out 30s later and blaming the launch shim.
  printf '%s\n' "$why" > "$SL_RUN_ROOT/state/authfail/$ID.$TOKEN"
  echo "[$ID] GH AUTH DEAD: $why" >&2
  # Deliberately NO exited marker: that marker means "a worker WAS running and its process is
  # gone", and the runner recovers from it by RELAUNCHING — straight back into the same dead auth.
  # No worker ever ran here; the launcher's rc is the whole signal.
  exit "$AUTH_DEAD_RC"
}
if [ -z "$EXPECT_LOGIN" ]; then
  # launch-session.sh ALWAYS names the expectation in the dropped command. If it ever stops, there
  # is nothing to assert against — and a floor that silently disables itself when its input goes
  # missing is not a floor. Fail closed rather than degrade to "some gh answered".
  refuse_auth "no expected gh login was handed to this session (SL_EXPECT_GH_LOGIN empty) — the launcher must name it, so refusing rather than starting a session whose identity cannot be checked"
fi
# ONE retry: a single momentary network blip is not auth death, and with LAUNCH_FAILURE_CAP=2 two
# consecutive launches lost to one would park the issue. Deliberately not more — every attempt can
# cost gh's own multi-second timeout on an unreachable network, and the whole probe has to finish
# inside the launcher's verify window (30s) or the launch reads as a shim that never fired, which
# is the mis-blame this code exists to prevent. A healthy env answers on the first try, inside one 100ms poll tick.
ACTUAL_LOGIN=""
attempt=1
while : ; do
  ACTUAL_LOGIN="$(gh_login)" && [ -n "$ACTUAL_LOGIN" ] && break
  [ "$attempt" -ge 2 ] && break
  attempt=$((attempt + 1)); sleep 1
done
if [ "$ACTUAL_LOGIN" != "$EXPECT_LOGIN" ]; then
  refuse_auth "\`$GH api user\` did not answer as '$EXPECT_LOGIN' from inside this session's own environment (got: ${ACTUAL_LOGIN:-<no answer>}). Run \`gh auth login --hostname github.com\` as the account that owns the loop repo"
fi

# DELIVERY PROOF (RC-LAUNCHVERIFY — the run-20260625-1857 overnight killer). Now that we hold the
# worker lock and are about to start Claude, stamp the PER-LAUNCH start marker. Its NAME carries the
# launch token (this tab's surface UUID, passed by launch-session.sh), so it is unique to THIS launch
# — concurrent or stale launches use different filenames and cannot stomp each other's proof (codex
# verify P1-b: a single shared state/started/<id> let an overlapping launch's hygiene delete the
# proof and the real launch then close its OWN worker tab). Stamped before the brief check so
# delivery is proven even if the brief is missing — that branch writes the exited marker so the
# dead state is observed promptly, not 45 min later. (TOKEN is resolved by the env floor above; the
# env and auth refusals key their own markers on the same per-launch token.)
printf '%s' "$TOKEN" > "$SL_RUN_ROOT/state/started/$ID.$TOKEN"
# Clear THIS launch's captured stderr tail now (issue #40, review P1-1) — AFTER the singleton lock
# (so a duplicate launch of a still-live worker can never wipe the real worker's tail) but BEFORE the
# brief-missing early-exit just below (which itself writes an exited marker): a prior FAILED launch's
# stderr must never bleed into a later park memo, on EVERY path that can emit an exited marker.
# run_agent (re)writes this file when the agent exits.
ERR_TAIL="$SL_RUN_ROOT/state/launch_stderr/$ID"
rm -f "$ERR_TAIL"
# Defined HERE, above its first use (the brief selection just below) rather than beside the other
# flag helpers: this script runs under `set -uo pipefail` with no `-e`, so calling truthy before it
# exists would not abort — it would return non-zero and silently leave the ORIGINAL brief selected,
# which is the exact fail-open the selection below is written to prevent.
truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}
# A REVIVE opens on its own brief — the re-orientation preamble — kept in a SEPARATE file so the
# lane's original brief survives untouched for any later cold relaunch (#298; the runner's
# crash-recovery path re-launches without rebuilding the brief, and would otherwise hand a
# brand-new session a "you were interrupted" preamble and no work instruction). Selection must
# match launch-session.sh's, which makes the same choice for its existence check — including
# FAILING CLOSED: this check runs in the new tab up to 30s after the launcher's, so a preamble
# that disappeared in between must abort, never silently become the lane's original brief
# delivered as a new instruction into a conversation that already built it.
# `truthy` is the SAME predicate the --resume flag is chosen by, below: two different tests for
# one variable is how SL_RESUME=0 would pick the resume brief and `--session-id` together.
BRIEF="$SL_RUN_ROOT/briefs/$ID.md"
truthy "${SL_RESUME:-}" && BRIEF="$SL_RUN_ROOT/briefs/$ID.resume.md"
[ -f "$BRIEF" ] || { echo "[$ID] no brief" >&2; write_exited 1; exit 1; }
# Name the session so the operator can tell what's running when they're away:
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
# ---- SESSION IDENTITY (issue #298) ----
# The per-flight conversation id, MINTED BY THE RUNNER at spawn (launch-session.sh) and handed down
# here — identity ASSIGNED, never self-asserted (claim c3). Two spellings, one id:
#   SL_RESUME empty -> `--session-id <id>`: pre-assign the id this brand-new conversation will own.
#   SL_RESUME truthy -> `--resume <id>`:    re-enter that SAME conversation after an interruption.
# Passing `--session-id` for an id that already exists is an ERROR, so the two are exclusive — the
# resume path must never also assign. Both are Claude Code's own spelling and so live ONLY here
# (agent-boundary rule); codex has its own `codex resume` and would abort on an unknown flag, so the
# id is deliberately not threaded into that branch.
SESSION_ID="${SL_SESSION_ID:-}"
RESUME="${SL_RESUME:-}"

toml_string() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# ---- LAUNCH STDERR CAPTURE (issue #40, agent boundary) ----
# A launch that dies immediately — a bad --model, a renamed/dropped CLI flag — writes its real
# reason to STDERR and then vanishes with the doomed cmux tab; the runner afterward sees only
# "exited and already relaunched N times (cap N)". Capture a BOUNDED tail of the agent's stderr to a
# well-known file so the relaunch-cap park memo (which stays agent-agnostic) can NAME the actual
# error. Knowing the process is `claude`/`codex` is agent-specific, so the capture lives HERE.
#   * stderr ONLY. stdout stays the interactive TUI on the real terminal (fd3) — piping stdout drops
#     Claude into print mode and kills the watchable pane (see the claude branch), so fd1 is never
#     touched. stderr is still tee'd back to the terminal, so a healthy session is visually
#     unchanged even if the agent ever writes there.
#   * the AGENT's own exit code comes from PIPESTATUS[0], never the pipeline's.
#   * the tail is byte-bounded and written only AFTER the agent exits, so it adds no blocking wait to
#     the live launch path.
# ERR_TAIL was defined and cleared earlier (right after the singleton lock, before the brief check)
# so EVERY exited-marker path starts from a clean tail; run_agent only writes it.
LAUNCH_STDERR_MAX_BYTES="${SL_LAUNCH_STDERR_MAX_BYTES:-4096}"
AGENT_RC=0
run_agent() {                            # "$@" = the full agent command line
  local raw
  raw="$(mktemp "${TMPDIR:-/tmp}/sl-launch-err.XXXXXX")" || { "$@"; AGENT_RC=$?; return; }
  { "$@" 2>&1 1>&3 | tee "$raw" >&2; } 3>&1
  AGENT_RC=${PIPESTATUS[0]}
  tail -c "$LAUNCH_STDERR_MAX_BYTES" "$raw" > "$ERR_TAIL" 2>/dev/null || true
  rm -f "$raw"
}

case "$AGENT" in
  claude)
    # ---- THE CLAUDE BINARY, RESOLVED EXPLICITLY (issue #303) ------------------------------------
    # This used to be a bare `claude`, which meant PATH ORDER decided which binary a worker ran —
    # and nobody had configured that order, nothing reported it, and it differs between the
    # operator's shell and a fresh tab's. The tool-dive gap-fill (docs/TOOL-DIVE-2026-07-28.md,
    # 2026-07-29) found the sharp edge: a machine whose only `claude` was cmux's BUNDLED wrapper.
    # That wrapper contains no Claude Code — it walks PATH for some OTHER claude and execs it — so
    # on such a machine retiring cmux takes the launcher's binary with it, at the exact moment of
    # migration. docs/HERDR-ADOPTION-PLAN.md §6 sequences this pin BEFORE any cmux removal.
    #
    # THE LADDER, and lib/stack_doctor.py's resolve_claude is its TWIN — the doctor must judge the
    # same binary this runs, or its verdict describes a different process. Deliberately duplicated
    # rather than sourced, for the reason the gh probe above is: this script runs in a fresh tab
    # shell that inherits nothing and knows no engine paths, so a sourced helper would add a failure
    # mode exactly where the stack is least able to report one. tests/test_start_session.py drives
    # BOTH ladders and compares, so the duplication cannot silently drift.
    #   1. SL_CLAUDE — an explicit operator pin, read from THIS session's own environment (a shell
    #      rc file or a LaunchAgent), never threaded through the launcher: `claude` is an
    #      agent-specific name and launch-session.sh is the agent-AGNOSTIC half of the stack.
    #   2. $HOME/.local/bin/claude — Claude Code's standalone native install (a symlink into
    #      ~/.local/share/claude/versions/<v>, re-pointed across upgrades by `claude install`).
    #      This is the DEFAULT pin, and it is cmux-independent by construction: no PATH entry takes
    #      part in the decision, so cmux's shim can neither be chosen nor be missed when it goes.
    #   3. PATH — the last resort, so a machine that installed Claude Code some other way still
    #      launches. `doctor --stack`'s `claude binary` block WARNs on this rung.
    #
    # A PIN THAT NAMES NOTHING RUNNABLE REFUSES THE LAUNCH. It does not fall back: a silent
    # fallback would restore the exact PATH luck the pin was set to remove, at the moment the
    # operator most believes the binary is pinned. The refusal writes the launch-stderr tail the
    # relaunch-cap park memo reads (issue #40), so the park NAMES the pin instead of the machinery.
    refuse_claude_bin() {
      printf '[%s] %s\n' "$ID" "$1" > "$ERR_TAIL" 2>/dev/null || true
      echo "[$ID] $1" >&2
      write_exited 127                   # 127 = "command not found", which is exactly what this is
      exit 127
    }
    # `-f AND -x`, never `-x` alone: a DIRECTORY is executable (that is traversal permission), so a
    # bare `-x` would accept `SL_CLAUDE=$HOME/.local/bin`, stamp it as the binary in use, and only
    # fail when bash tried to exec a directory — past the refusal, past the stamp, with a park memo
    # blaming the agent. This is also exactly what the Python twin tests (os.path.isfile + os.access),
    # so the two ladders agree on what "runnable" means (fresh-agent review, P1).
    sl_runnable() { [ -f "$1" ] && [ -x "$1" ]; }
    CLAUDE_STANDALONE="${HOME:-}/.local/bin/claude"
    if [ -n "${SL_CLAUDE:-}" ]; then
      CLAUDE_BIN="$SL_CLAUDE"
      sl_runnable "$CLAUDE_BIN" || refuse_claude_bin \
        "SL_CLAUDE pins '$CLAUDE_BIN', which is not an executable file — refusing to fall back to whatever \`claude\` this session's PATH happens to offer. Point SL_CLAUDE at a real claude binary, or unset it to take the standalone install at $CLAUDE_STANDALONE"
    elif sl_runnable "$CLAUDE_STANDALONE"; then
      CLAUDE_BIN="$CLAUDE_STANDALONE"
    else
      CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
      [ -n "$CLAUDE_BIN" ] || refuse_claude_bin \
        "no claude binary to launch: SL_CLAUDE is unset, $CLAUDE_STANDALONE does not exist, and no \`claude\` is on this session's PATH. Install Claude Code's standalone native build (\`claude install stable\`), then re-run \`superlooper doctor --stack\`"
    fi
    # Stamp what THIS launch resolved, machine-wide. The doctor re-walks the ladder in the
    # OPERATOR's environment, and a worker tab's is not the same one — the same gap #299/#301 were
    # written for. Only a stamp written from inside the session can say what actually ran, so
    # `doctor --stack` reads this file and lets it VETO an otherwise-healthy resolution.
    # ONE machine-wide file, not one per id, and deliberately so: the question it answers is "which
    # claude does a session on this MACHINE end up running", which is a machine fact — and a
    # per-lane file would need its own hygiene and would go stale on a lane that switched agents.
    # Concurrent launches race to last-writer-wins, which costs nothing: they walk the same ladder
    # in the same environment, so they agree. Written tmp+mv so a reader never sees a half-written
    # path, and best-effort throughout: telemetry must never fail a launch that is ready to fly.
    if [ -n "${HOME:-}" ] && mkdir -p "$HOME/.superlooper" 2>/dev/null; then
      if _sl_bin_tmp="$(mktemp "$HOME/.superlooper/.claude-bin.XXXXXX" 2>/dev/null)"; then
        if printf '%s\n' "$CLAUDE_BIN" > "$_sl_bin_tmp" 2>/dev/null; then
          mv -f "$_sl_bin_tmp" "$HOME/.superlooper/claude-bin.last" 2>/dev/null || \
            rm -f "$_sl_bin_tmp" 2>/dev/null || true
        else
          rm -f "$_sl_bin_tmp" 2>/dev/null || true
        fi
      fi
    fi
    CLAUDE_ARGS=(--dangerously-skip-permissions)
    [ -n "$MODEL" ] && CLAUDE_ARGS+=(--model "$MODEL")
    [ -n "$EFFORT" ] && CLAUDE_ARGS+=(--effort "$EFFORT")
    CLAUDE_ARGS+=(--name "$NAME" --remote-control "$NAME")
    # Guarded on NON-EMPTY exactly like --model/--effort above: `--session-id ""` fails the UUID
    # check and kills the launch with the tab. A bare `--resume` (no value) is worse than useless —
    # the flag takes an OPTIONAL value, so with nothing after it claude opens its interactive
    # session PICKER and an unattended launch sits at that menu until the freeze-net times out. No
    # id => neither flag, and the session behaves exactly as it did before #298.
    if [ -n "$SESSION_ID" ]; then
      if truthy "$RESUME"; then
        CLAUDE_ARGS+=(--resume "$SESSION_ID")
      else
        CLAUDE_ARGS+=(--session-id "$SESSION_ID")
      fi
    fi
    # Do NOT pipe Claude's STDOUT through tee/cat — piping stdout drops it into print mode and kills
    # the interactive pane you want to watch. (No headless `claude -p` anywhere — owner billing rule
    # B.9.) run_agent captures only STDERR (stdout stays the TTY via fd3), so the TUI is unaffected.
    run_agent "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}" "$(cat "$BRIEF")"
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
    # Interactive Codex, not `codex exec`; the brief is the initial prompt. Same stderr-only capture
    # as the claude branch — stdout stays the interactive TUI.
    run_agent codex "${CODEX_ARGS[@]}" "$(cat "$BRIEF")"
    ;;
  *)
    echo "[$ID] unsupported agent '$AGENT' (expected: claude or codex)" >&2
    write_exited 64
    exit 64
    ;;
esac
rc=$AGENT_RC        # the AGENT's own exit code, captured through run_agent (PIPESTATUS[0])
# Deterministic crash/quit/limit signal (RC-DEADPANE): when the agent process returns to the
# shell, write state/exited/<id> with the real exit code. The runner emits session_exited from
# this marker (recovered by RESTART), and nudge-pane.sh refuses to type into the now-bash pane —
# so a runner "resume" can never execute as a permission-bypassed shell command. The EXIT
# trap then frees the worker lock so a legitimate restart can take this id over.
write_exited "$rc"
echo
# NAME the id when we have one (#298). A bare `claude --resume` drops the operator into the session
# picker and makes them guess which entry was this pane; with the id it is a command they can paste.
resume_cmd="claude --resume${SESSION_ID:+ $SESSION_ID}"
[ "$AGENT" = "codex" ] && resume_cmd="codex resume"
echo "[$ID] session ended $(date '+%H:%M') rc=$rc — scroll up to inspect, or: $resume_cmd"
