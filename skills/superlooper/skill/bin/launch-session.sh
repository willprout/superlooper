#!/usr/bin/env bash
# Mechanical launch of ONE session. Called by the runner (never a background watcher).
# Two modes:
#   launch-session.sh <i-id>            worker: create the issue's worktree off origin/<dev>, launch
#   launch-session.sh --cwd <dir> <d-id>  debugger: launch in an existing dir (no worktree/branch)
# Both open a visible cmux tab, pre-trust the folder, and fire the brief into an interactive
# bypass-permission Claude — via the keystroke-free launch shim, then VERIFY delivery.
set -euo pipefail

# ---- Parse mode + id ----------------------------------------------------------------------------
CWD_MODE=0
CWD=""
if [ "${1:-}" = "--cwd" ]; then
  CWD_MODE=1
  CWD="${2:?usage: launch-session.sh --cwd <dir> <d-id>}"
  ID_IN="${3:?usage: launch-session.sh --cwd <dir> <d-id>}"
else
  ID_IN="${1:?usage: launch-session.sh <i-id>  (or --cwd <dir> <d-id>)}"
fi

SL_RUN_ROOT="${SL_RUN_ROOT:?SL_RUN_ROOT required}"
# The cmux PANE that hosts all superlooper tabs (runner + every session) so they are grouped and
# watchable in one place, NOT scattered as separate workspaces. The runner sets this at startup.
SL_PANE="${SL_PANE:?SL_PANE (target cmux pane id for tabs) required}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Overridable so the delivery-verification test can inject a stub cmux; defaults to the real app.
CMUX="${SL_CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
# Same override convention as lib/gh.py and stack_doctor: SL_GH names the binary, so no test ever
# reaches the real `gh`. In production it is UNSET, so this is the bare name `gh` and each side
# resolves it from its OWN PATH — which is what a positive assert of the worker's environment
# wants: the worker must be authenticated with the gh IT would actually run, not one the launcher
# hand-picked. Naming it in the dropped command carries a test's override across the spawn (the
# fresh tab inherits nothing), it does not pin production to the launcher's binary.
GH="${SL_GH:-gh}"
# Two DISTINCT exit codes for the positive gh-auth assert (issue #299), because the two failures
# have opposite blast radii and must be charged to opposite parties (issue #153):
#   4 — THIS SESSION's own environment cannot authenticate: a per-issue fault, parks this issue with
#       a memo naming auth (evidence.py -> `gh_auth_dead`).
#   5 — the RUNNER's environment cannot authenticate: no queued issue caused it and none can fix it,
#       so it is a CHANNEL fault and the queue is held intact (evidence.py -> `gh_auth_dead_runner`).
AUTH_DEAD_RC=4
AUTH_DEAD_RUNNER_RC=5
MODEL="${SL_MODEL:-}"
EFFORT="${SL_EFFORT:-}"
CODEX_DANGEROUS_BYPASS="${SL_CODEX_DANGEROUS_BYPASS:-}"
CODEX_BYPASS_HOOK_TRUST="${SL_CODEX_BYPASS_HOOK_TRUST:-}"
CODEX_NO_ALT_SCREEN="${SL_CODEX_NO_ALT_SCREEN:-}"
AGENT="${SL_AGENT:-claude}"
# Is a PERSON at the keyboard for the session we are about to launch? `1` only when the caller is
# `superlooper debug`'s owner tap (issue #144), which launches a d<N> session a human just asked
# for; EMPTY for every unattended launch — the runner (_script_env) and the watchdog
# (_debugger_shim_run) each pin it so an ambient `export SL_ATTENDED=1` in the shell or LaunchAgent
# they were started from cannot ride in. The PreToolUse deny (issue #185) reads it to stand its
# AskUserQuestion duty down rather than tell an attended session the falsehood "no human is at this
# pane" — and ignores it for worker ids regardless, so the two halves back each other up. Passed
# through verbatim: this script does not judge it, it only makes sure the fresh tab shell (which
# inherits nothing) is TOLD it.
ATTENDED="${SL_ATTENDED:-}"
case "$AGENT" in
  claude) ;;
  codex) ;;
  *)
    echo "[$ID_IN] unsupported agent '$AGENT' (expected: claude or codex)" >&2
    exit 64
    ;;
esac

# ---- Resolve identity + worktree ----------------------------------------------------------------
if [ "$CWD_MODE" -eq 1 ]; then
  # Debugger: no worktree, no branch. Validate the id, use the provided dir verbatim as the cwd.
  # (The caller passes the target repo checkout; nothing here is created or checked out.)
  if ! ID="$(python3 - "$HERE/../lib" "$ID_IN" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])          # $HERE/../lib — robust regardless of cwd
import sanitize
print(sanitize.worktree_id(sys.argv[2]))
PY
)"; then
    echo "[$ID_IN] id sanitize validation failed — not launching" >&2
    exit 1
  fi
  # --cwd is the in-place path: the watchdog's unattended sl-debugger sessions (d<N>, issue #66),
  # which launch in an existing dir with no worktree and no branch. It ALSO carried the answerer
  # (a<N>) until #194 retired that seat, so the shape is now d<N> alone — narrowed, not deleted.
  # The id shape is enforced so a caller bug can never route an issue id (i<N>) through here and
  # silently skip worktree creation + the issue counter (fail closed on wrong-typed input, not just
  # unsafe input — cross-review, Task 6). worktree_id already rejected unsafe chars; this pins the
  # mode's contract.
  if ! [[ "$ID" =~ ^d[0-9]+$ ]]; then
    echo "[$ID] --cwd mode is for debugger (d<N>) ids only — refusing" >&2; exit 1
  fi
  BRANCH=""
  NAME="superlooper $ID"
  [ -d "$CWD" ] || { echo "[$ID] --cwd dir does not exist: $CWD" >&2; exit 1; }
  # Resolve to an ABSOLUTE physical path: the dropped `cd %q` runs in the NEW tab's fresh shell,
  # which starts in its own default dir (not the launcher's cwd), so a relative dir would `cd` into
  # nothing there and the launch would silently fail to verify (cross-review, Task 6).
  WT="$(cd "$CWD" && pwd -P)"
else
  # Worker: identity + branch come from state/issues.json via loopstate (NOT plan.json — issues are
  # the queue). R3: validate the id AND branch/base through lib/sanitize.py before anything reaches
  # the shell or git. sanitize RAISES on an unsafe value BEFORE printing, so on failure Python emits
  # nothing, `read` hits EOF and returns non-zero, and the explicit check below aborts the launch.
  # (The guard is INTENTIONAL — don't let a future edit slip in `|| true`.) Import path is anchored
  # to $HERE/../lib (NOT cwd-relative). The worktree bases off origin/<dev_branch>: the worker must
  # start from the CURRENT dev mainline, so the runner keeps origin fresh (it fetches in its poll
  # loop) and we branch from origin/<dev>, never a stale local ref.
  SL_REPO="${SL_REPO:?SL_REPO (target repo path) required}"
  SL_DEV_BRANCH="${SL_DEV_BRANCH:?SL_DEV_BRANCH required}"
  # PORT GOTCHA (macOS /bin/bash 3.2): a heredoc inside `<(…)`/`$(…)` has a fragile parser — the
  # closing `)` MUST sit at column 0 (not indented) and the heredoc body must contain NO apostrophe
  # (a lone `'` derails its quote scan → "bad substitution: no closing `)'"). Keep this block ASCII-
  # apostrophe-free and the `)` un-indented, exactly as below.
  if ! read -r ID BRANCH BASE_BRANCH < <(python3 - "$HERE/../lib" "$SL_RUN_ROOT/state/issues.json" "$ID_IN" "$SL_DEV_BRANCH" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])          # HERE/../lib — robust regardless of cwd
import sanitize, loopstate
st = loopstate.load(sys.argv[2]); iid = sys.argv[3]; dev = sys.argv[4]
issue = st["issues"][iid]                # KeyError if the runner has not registered it -> abort
print(sanitize.worktree_id(iid),
      sanitize.branch(issue["branch"]),  # None/empty -> ValueError -> no output -> read fails -> abort
      sanitize.branch(dev))
PY
); then
    echo "[$ID_IN] issues.json load / sanitize validation failed — not launching" >&2
    exit 1
  fi
  # Worker path is for issue ids (i<N>) only — the symmetric contract to --cwd's d<N> check above,
  # so a debugger id can never accidentally spin up a worktree here (cross-review, Task 6).
  if ! [[ "$ID" =~ ^i[0-9]+$ ]]; then
    echo "[$ID] worker mode expects an issue id (i<N>) — refusing" >&2; exit 1
  fi
  BASE="origin/$BASE_BRANCH"
  WT="$SL_RUN_ROOT/worktrees/$ID"
  NAME="superlooper $ID ($BRANCH)"
fi

# ---- SESSION IDENTITY, MINTED HERE (issue #298) ----
# The runner assigns each flight its conversation id BEFORE the session exists — identity handed
# down, never self-asserted (claim c3) — so that after ANY interruption (killed pane, crashed host,
# closed lid) the same conversation can be re-entered with `claude --resume <id>` instead of
# restarting cold from zero. The 2026-07-29 spikes passed this unqualified and host-independent;
# the 2026-07-30 supervised run proved it live through two kill -9s and a real sleep.
#
# SL_RESUME_SESSION_ID is the REVIVE seam: `superlooper resume` hands back the id it read from lane
# state, and we pass that through verbatim rather than minting. Minting on a revive would strand
# the very transcript the operator asked to re-enter. Empty/absent -> a fresh conversation.
# A RELAUNCH always mints anew: `--session-id` on an already-used id is an error, so a dead
# session's id can never be recycled as a fresh one (reviving it is the resume path's job).
#
# CLAUDE ONLY. `--session-id`/`--resume` are Claude Code's spelling; codex has its own `codex
# resume` and start-session.sh deliberately does not thread the id into that branch. Minting for a
# codex repo anyway would write a UUID into lane state that names no conversation anywhere — a
# fabricated identity claim, and `superlooper resume --check` would then promise a revive that
# cannot happen (fresh-agent review P0-2).
RESUME=""
SESSION_ID=""
if [ "$AGENT" = "claude" ]; then
  SESSION_ID="${SL_RESUME_SESSION_ID:-}"
  if [ -n "$SESSION_ID" ]; then
    RESUME=1
  else
    SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')" || {
      echo "[$ID] could not mint a session id" >&2; exit 1; }
  fi
fi

# A REVIVE opens on its own brief — the re-orientation preamble `superlooper resume` composed —
# and the lane's ORIGINAL brief is left untouched beside it. They must be separate files: the
# runner's crash-recovery relaunch (_exec_recover) re-runs this script WITHOUT rebuilding the
# brief, so a preamble written over briefs/<id>.md would later be delivered verbatim to a
# brand-new, empty session — telling it "your conversation above survived intact" when there is no
# conversation and no work instruction anywhere (fresh-agent review P0-1). Selected on RESUME
# alone, so a cold relaunch always gets the real brief back.
#
# FAIL CLOSED, never fall back: if this is a resume and the preamble is missing, ABORT. Silently
# substituting the lane's original brief would deliver the whole issue brief — goal, DoD,
# boundaries — as a NEW instruction into a conversation that already built it, with no
# re-orientation first, which is exactly the ordering the resume exists to guarantee.
# The test is spelled to MATCH start-session.sh's `truthy "$SL_RESUME"` on the far side, so the
# invariant "these two selections are the same selection" is visible here rather than inferred
# from RESUME happening to have only two assignment sites.
BRIEF="$SL_RUN_ROOT/briefs/$ID.md"
if [ "$RESUME" = "1" ]; then
  BRIEF="$SL_RUN_ROOT/briefs/$ID.resume.md"
fi
[ -f "$BRIEF" ] || { echo "[$ID] missing brief $BRIEF" >&2; exit 1; }

# ---- THE EXPECTED gh LOGIN (issue #299) --------------------------------------------------------
# The session's own assert (start-session.sh) needs something to assert AGAINST, and the honest
# expectation is the loop's OWN identity: the gh account the runner acts as — the one that writes
# labels, posts comments and merges PRs. A worker answering as anyone else is not this loop, and a
# worker answering as nobody cannot read its issue or post its evidence.
#
# Read it ONCE here, in the runner's env, and hand it down. Deriving it rather than configuring it
# means every adopted repo gets the floor with no new config key to set (and nothing to leave
# unset), and it catches the exact 2026-07-29 spike shape: an inherited XDG_CONFIG_HOME that
# de-authenticates gh in the WORKER's fresh env while the runner's own gh stays healthy.
#
# FAIL CLOSED and fail EARLY: if the launcher's own gh cannot say who it is, a machine-level auth
# fault is already underway, there is no expectation to check the worker against, and no session
# started now could do its job. Placed BEFORE worktree creation and before any cmux RPC, so a
# refusal costs no orphan tab and no leftover checkout — the base-missing discipline (#28).
# BOUNDED. `gh` has no default request timeout, so on a black-holed network this can hang for tens
# of seconds — and the runner kills the whole launch at LAUNCH_TIMEOUT=120, turning a diagnosable
# auth/network fault into an undiagnosable rc=124 hang. stack_doctor already bounds its own gh calls
# at 10s; this is the same discipline in shell. macOS ships no `timeout(1)`, so we background the
# read and wait on OUR OWN recorded pid — never a kill by name or pattern (house rule), which could
# match the owner's live processes.
GH_PROBE_SECONDS="${SL_GH_PROBE_SECONDS:-10}"
gh_login_bounded() {                 # stdout = gh's answer (or its error text); rc = gh's, or 124
  # Polled in TENTHS, not seconds: the child is always still alive at the first check, so a 1s
  # granularity charged EVERY healthy launch a full second of dead wait. Both BSD and GNU sleep
  # take fractions. `rc=0; wait || rc=$?` rather than `wait; rc=$?` because this file runs under
  # `set -e` — a bare failing `wait` aborts inside the command substitution before `cat` runs,
  # silently returning an EMPTY capture and leaking the temp file: P1-1 through a second door.
  local out_file pid ticks=0 limit rc
  case "$GH_PROBE_SECONDS" in ""|*[!0-9]*) GH_PROBE_SECONDS=10 ;; esac   # a typo must not mean "refuse"
  limit=$((GH_PROBE_SECONDS * 10))
  out_file="$(mktemp "${TMPDIR:-/tmp}/sl-ghwho.XXXXXX")" || return 1
  "$GH" api user --jq .login > "$out_file" 2>&1 &
  pid=$!
  while [ "$ticks" -lt "$limit" ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.1 2>/dev/null || sleep 1   # a sleep without fractions must cost
    ticks=$((ticks + 1))                # granularity, never a refused launch
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true             # OUR pid, captured from $! — nothing else can match
    wait "$pid" 2>/dev/null || true
    printf 'no answer within %ss (gh did not return)' "$GH_PROBE_SECONDS" > "$out_file"
    rc=124
  else
    rc=0; wait "$pid" || rc=$?
  fi
  cat "$out_file"; rm -f "$out_file"
  return "$rc"
}
EXPECT_GH_LOGIN="${SL_EXPECT_GH_LOGIN:-}"
GH_PROBE_SAID=""
if [ -z "$EXPECT_GH_LOGIN" ]; then
  # Keep gh's OWN WORDS whatever the rc. An `|| EXPECT_GH_LOGIN=""` here would discard the capture
  # on every real failure — which is every failure — and the refusal below could then only ever say
  # "no answer", flattening not-logged-in, rate-limited, network-down and gh-not-installed into one
  # indistinguishable memo. That is precisely the mis-blame evidence.py exists to end, so the text
  # is preserved and the SHAPE CHECK below (not the rc) decides whether it is a usable login.
  GH_PROBE_SAID="$(gh_login_bounded)" || true
  EXPECT_GH_LOGIN="$GH_PROBE_SAID"
fi
# A GitHub login is [A-Za-z0-9-] and nothing else, so anything that is not exactly that shape is not
# an answer we may assert against. This matters beyond tidiness: `gh` merges its diagnostics into
# the capture, and an error path prints a MULTI-LINE message. A line-anchored regex would happily
# match a bare word on line 2 of that message and hand a launcher's error text down as the
# "expected login" — so the test is a whole-string glob (a `case` negated class matches a newline
# like any other stray character), not a per-line one.
case "$EXPECT_GH_LOGIN" in
  ""|*[!A-Za-z0-9-]*) EXPECT_GH_LOGIN_OK=0 ;;
  *)                  EXPECT_GH_LOGIN_OK=1 ;;
esac
if [ "$EXPECT_GH_LOGIN_OK" -ne 1 ]; then
  # DISTINCT from the worker-side refusal below (AUTH_DEAD_RC). This one says the RUNNER'S OWN
  # environment cannot authenticate — a fault no queued issue caused and none can fix, exactly the
  # shape CHANNEL_FAULT_REASONS exists for (the sibling of anchor_socket_lost). Charging it to an
  # issue's launch cap would walk the whole approved queue into parks over one machine-level fault,
  # the 2026-07-09 storm shape. evidence.py maps this rc to a HELD queue instead.
  echo "[$ID] GH AUTH DEAD (runner env): \`$GH api user\` did not return a usable login, so there is no identity to launch any session against — got: ${GH_PROBE_SAID:-${EXPECT_GH_LOGIN:-<no answer>}}" >&2
  echo "[$ID] Run \`gh auth login --hostname github.com\` as the account that owns the loop repo. NOT launching; no tab was opened." >&2
  exit "$AUTH_DEAD_RUNNER_RC"
fi

# Create the worktree (worker mode only) off the fresh dev base; the fallback attaches an EXISTING
# branch (a relaunch/regenerate reuses the same branch name). Both attempts are guarded so a
# failure is DIAGNOSED here, not left to abort with git's generic code under set -e (issue #28): if
# both fail because the base ref origin/<dev_branch> is missing — the master/develop-repo case — we
# exit with the DISTINCT code 3 and NAME the base, so the runner's park memo blames the branch, not
# the launch shim. This runs BEFORE any cmux tab is created, so a missing base costs no orphan tab.
if [ "$CWD_MODE" -eq 0 ] && [ ! -d "$WT" ]; then
  if ! git -C "$SL_REPO" worktree add -b "$BRANCH" "$WT" "$BASE" 2>/dev/null; then
    if ! git -C "$SL_REPO" worktree add "$WT" "$BRANCH" 2>/dev/null; then
      if ! git -C "$SL_REPO" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null 2>&1; then
        echo "[$ID] worktree base '$BASE' does not exist on '$SL_REPO' — the configured dev_branch is not on origin, so no worktree can be created. Run 'superlooper doctor' and set dev_branch to the repo's default, then re-approve." >&2
        exit 3
      fi
      echo "[$ID] could not create the worktree at '$WT' for branch '$BRANCH' (base '$BASE' exists)" >&2
      exit 1
    fi
  fi
fi
"$HERE/pretrust.sh" "$WT"                       # first-run trust prompt won't hang
mkdir -p "$SL_RUN_ROOT/state/activity" "$SL_RUN_ROOT/state/panes" "$SL_RUN_ROOT/state/started" \
         "$SL_RUN_ROOT/state/blocked" "$SL_RUN_ROOT/state/exited" "$SL_RUN_ROOT/state/awaiting" \
         "$SL_RUN_ROOT/state/sessions" "$SL_RUN_ROOT/state/authfail" "$SL_RUN_ROOT/reports"
# Record the id NOW, before the tab exists. Deliberately UNLIKE the activity/pane stamps below,
# which are withheld until delivery is verified because writing them early once fabricated
# "launched & alive" for 45 minutes: an id is an IDENTITY claim, not a liveness one — nothing reads
# this file as proof anything is running. The verify window is up to 30s, and a host that dies
# inside it must still leave behind the handle to resume by; a failed launch therefore leaves the
# record in place rather than deleting it (a stale id costs a clean "no conversation found" at
# resume time, while a deleted one loses the only handle on a straggler shell that started late).
# Written tmp+mv like every other durable state write in this stack, so a reader can never see a
# half-written id.
# Guarded at each step like the other multi-step writes here: a half-done write must NAME itself,
# not abort the launch with a bare rc=1 under `set -e`, and a failed mv must not leave its temp
# behind in a directory readers scan.
if [ -n "$SESSION_ID" ]; then
  if ! sid_tmp="$(mktemp "$SL_RUN_ROOT/state/sessions/.$ID.XXXXXX")" \
     || ! printf '%s' "$SESSION_ID" > "$sid_tmp" \
     || ! mv -f "$sid_tmp" "$SL_RUN_ROOT/state/sessions/$ID"; then
    rm -f "${sid_tmp:-}" 2>/dev/null || true
    echo "[$ID] could not record the session id — this session will not be resumable" >&2
    exit 1
  fi
fi
# Restart hygiene: clear ONLY this id's run-state markers so a prior session's report/exited/blocked
# can't mis-fire for the fresh session. The worktree and any committed work are PRESERVED (never
# touched here). Scope is strictly these named markers — a wrong glob would discard a real report, so
# this is intentionally explicit, not a wildcard. This is the block the conflict-regenerate ladder
# (§C.4.6b) relies on so a stale report cannot false-gate a rebuilt run (cross-review M1). The
# `started` marker is NOT cleared here: it is PER-LAUNCH (state/started/<id>.<token>), keyed on this
# tab's fresh surface UUID, so a stale one cannot collide — and clearing a shared one would let an
# overlapping launch delete this launch's own proof (codex verify P1-b).
# state/mail/<id> and state/status/<id>.json join this list for the same reason (issue #148): mail
# was addressed to the session that just died — the fresh one would consume a stranger's
# instruction as if it were its own — and a stale progress clock would let a relaunched session
# that has not yet rested look like it had already stamped HEAD. Delivery RECEIPTS
# (mail/<id>.consumed.*/.claimed.*/.discarded.*) are deliberately NOT cleared: they are the record
# of what was actually handed over, and history survives a restart.
# A RESUME (#298) clears exactly the same set. Keeping the report for the revived session was
# tried and is WRONG in both directions (fresh-agent review P1-4): a present report makes
# events.py read the lane as `resolved`, which switches OFF the session_idle/frozen tiers — the
# revived session would run with no liveness net at all — and it makes actions.decide emit `gate`
# for a lane whose live session is still editing and pushing that branch. The report is a
# COMPLETION signal the runner acts on, so a session that has been revived to keep working must
# re-earn it, exactly as a replacement session must.
# Sweep this id's STALE auth markers (issue #299). Nothing else prunes them: the launcher only
# removes the marker it actually OBSERVED inside its verify window, so one written just after a
# timeout (or after the runner killed the launcher at LAUNCH_TIMEOUT) would otherwise sit here
# forever. AGE-BOUNDED on purpose — an id-wide `rm` would delete an OVERLAPPING launch's live
# marker, and that launch would then time out and report `shim_not_fired`, blaming the shim for an
# auth fault: the same class of bug as the shared start sentinel that codex-verify P1-b caught, and
# the reason `started` is per-token AND excluded from the hygiene block just below. Five minutes is
# far beyond any verify window, so only genuinely abandoned markers can match.
find "$SL_RUN_ROOT/state/authfail" -maxdepth 1 -name "$ID.*" -mmin +5 -delete 2>/dev/null || true
rm -f "$SL_RUN_ROOT/reports/$ID.md" \
      "$SL_RUN_ROOT/state/blocked/$ID" "$SL_RUN_ROOT/state/exited/$ID" "$SL_RUN_ROOT/state/awaiting/$ID" \
      "$SL_RUN_ROOT/state/mail/$ID" "$SL_RUN_ROOT/state/status/$ID.json"
# NOTE: the activity stamp (the runner's liveness/freeze baseline) is deliberately NOT written here.
# Writing it before delivery is confirmed is exactly what fabricated "launched & alive" for up to
# 45 min in run-20260625-1857 while no worker had actually started. It is written ONLY after the
# start-sentinel verifies the keystrokes were delivered (see below).
# Open a NEW TAB (surface) in the run's designated pane — visible, watchable, and grouped with the
# other superlooper tabs rather than scattered as separate workspaces. new-surface takes no
# --cwd/--command, so we then cd into the folder and fire the bypass-permission Claude via the
# keystroke-free shim (dropped .cmd file), not typed keystrokes (see below).
# One consistent label for the tab title, the session's --name, and the Remote Control dashboard, so
# the operator can tell what's running from their phone.
# Capture the tab's UUIDs, not the short ref. `--id-format uuids new-surface` prints
# "OK <surface-uuid> <pane-uuid> <workspace-uuid>". Surface UUIDs are GLOBALLY unique, so the runner
# can read & ring this tab from any workspace; a `surface:NN` short ref resolves only within the
# CALLER's workspace, which silently lost 156/156 doorbell rings (and every safe-peek read) in
# run-20260625-1857. We record the workspace UUID too and pass it as a belt-and-suspenders
# `--workspace` on every send (see nudge-pane.sh).
UUID_RE='[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
# Mark a launch IN-FLIGHT *before* creating the surface. The new tab's shell sources the launch shim
# during boot; writing .active first guarantees that shell sees a FRESH launch marker and WAITS for
# its command file — closing the race where a fast-booting shell reaches the shim before we have
# written anything (which would make the shim no-op and the launch falsely time out). The per-tab
# command file (keyed by the surface UUID) is written just after new-surface returns (below). The
# launch dir is a FIXED well-known path (NOT under SL_RUN_ROOT): the shim runs in a bare shell that
# knows only its CMUX_SURFACE_ID. Overridable for tests.
LAUNCH_DIR="${SL_LAUNCH_DIR:-$HOME/.superlooper/launch}"
mkdir -p "$LAUNCH_DIR"; chmod 700 "$LAUNCH_DIR" 2>/dev/null || true
: > "$LAUNCH_DIR/.active"
# Capture cmux EXPLICITLY: under `set -euo pipefail`, a cmux failure on the OUT= line, or a no-match
# grep in the SURF=/WS= pipelines, would abort the script with a non-deterministic code BEFORE the
# guard below could run (review P2). The `|| { … exit 1; }` and `|| true` make the guard reachable
# and the failure diagnostic + exit code (1) deterministic.
OUT="$("$CMUX" --id-format uuids new-surface --type terminal --pane "$SL_PANE")" || {
  echo "[$ID] new-surface failed (rc=$?) targeting pane '$SL_PANE'" >&2; exit 1; }
SURF="$(printf '%s\n' "$OUT" | grep -oE "$UUID_RE" | sed -n 1p || true)"   # OK <surf> <pane> <ws>
WS="$(printf '%s\n' "$OUT" | grep -oE "$UUID_RE" | sed -n 3p || true)"
[ -n "$SURF" ] || { echo "[$ID] could not parse a surface UUID from new-surface output: $OUT" >&2; exit 1; }
# Address THIS tab by surface + its workspace on every write. We are a child of the runner, so our
# $CMUX_WORKSPACE_ID is the runner's workspace, which may differ from SL_PANE's workspace. cmux
# scopes surface resolution to --workspace (default = caller's), so without the explicit workspace
# the keystroke-free path's follow-up RPCs can miss the new tab exactly like the doorbell did (codex
# P1-A). bash 3.2: guard the empty-array expansion under set -u.
WS_ARGS=()
[ -n "$WS" ] && WS_ARGS=(--workspace "$WS")
# ---- KEYSTROKE-FREE DELIVERY (RC6 — the run-20260626-1656 Wave-2 launch killer) ----
# We do NOT type the command in (`cmux send`/`send-key`). cmux exposes no keystroke-free "run a
# command in a surface" API (only surface.send_text/send_key), and macOS GATES that keystroke
# delivery to a fresh/background tab while the display sleeps or the app is backgrounded — so the
# overnight Wave-2 launches silently never reached the new tabs even though the Mac never locked.
# Instead we DROP the command in a file keyed by this tab's surface UUID; the launch shim — sourced
# by ~/.zshrc in the tab's freshly-spawned shell, which runs regardless of display state — reads and
# runs it. See shell/launch-shim.zsh + bin/install-launch-shim.sh. If the shim is NOT installed, no
# shell runs the command, the start sentinel never appears, and the verify below fails LOUDLY
# (exit 2) — never a fabricated "alive". (LAUNCH_DIR + the in-flight .active marker were set up
# before new-surface, above, to close the shell-boot race.)
#
# Build the worker command with %q so a path containing a space/quote can't break or inject (Claude
# runs permissions-bypassed; $ID is whitelist-validated above). SL_START_TOKEN = this tab's surface
# UUID: start-session.sh writes it into state/started/<id>.<token> so the verify below confirms OUR
# token — a delayed/orphan command from a PRIOR failed launch stamps a DIFFERENT token and can't
# false-verify.
# The new tab is a FRESH shell that inherits NONE of this launcher's env, so every var
# start-session.sh needs must be NAMED here. SL_EFFORT rides alongside SL_MODEL (empty on the
# default path; set only by a per-issue effort:* label) — %q-quoted like the rest so a value with
# brackets/spaces can't break or inject the command. Codex-specific knobs are named too, because the
# fresh tab shell inherits none of the runner's environment.
# SL_EXPECT_GH_LOGIN + SL_GH ride here for the same "the fresh tab inherits nothing" reason
# (issue #299): the session's own auth assert has nothing to compare against unless the expectation
# is NAMED, and it must probe with the binary this launch resolved rather than whatever `gh` the
# tab's PATH happens to find. Omitting either would not weaken the assert — start-session.sh fails
# closed on a missing expectation — it would refuse every launch, which is the loud failure a
# silently-disabled floor deserves.
printf -v CMD 'cd %q && SL_ISSUE_ID=%q SL_RUN_ROOT=%q SL_SESSION_NAME=%q SL_MODEL=%q SL_EFFORT=%q SL_AGENT=%q SL_ATTENDED=%q SL_SESSION_ID=%q SL_RESUME=%q SL_EXPECT_GH_LOGIN=%q SL_GH=%q SL_CODEX_DANGEROUS_BYPASS=%q SL_CODEX_BYPASS_HOOK_TRUST=%q SL_CODEX_NO_ALT_SCREEN=%q SL_START_TOKEN=%q %q %q' \
  "$WT" "$ID" "$SL_RUN_ROOT" "$NAME" "$MODEL" "$EFFORT" "$AGENT" "$ATTENDED" "$SESSION_ID" "$RESUME" "$EXPECT_GH_LOGIN" "$GH" "$CODEX_DANGEROUS_BYPASS" "$CODEX_BYPASS_HOOK_TRUST" "$CODEX_NO_ALT_SCREEN" "$SURF" "$HERE/start-session.sh" "$ID"
# Drop the command FIRST — before any further cmux RPC — so the new tab's shell finds it immediately
# and the shim's bounded wait can't be eaten by an unrelated slow RPC (e.g. rename-tab; review B6).
# Atomic write (tmp + mv) so the shim never reads a half-written command; refresh .active so its
# mtime stays fresh across the verify window (the shim's staleness gate).
CMD_FILE="$LAUNCH_DIR/$SURF.cmd"
cmd_tmp="$(mktemp "$LAUNCH_DIR/.cmd.XXXXXX")"
printf '%s' "$CMD" > "$cmd_tmp"
mv -f "$cmd_tmp" "$CMD_FILE"
: > "$LAUNCH_DIR/.active"
# Label the tab (non-critical; the worker can already start the moment the shim sees the dropped file).
"$CMUX" rename-tab --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} "$NAME" >/dev/null 2>&1 || true

# ---- VERIFY DELIVERY (RC-LAUNCHVERIFY — the run-20260625-1857 overnight killer) ----
# Never trust that the shim ran: poll for start-session.sh's per-launch sentinel
# state/started/<id>.<token> (keyed on THIS tab's surface UUID, so its mere existence proves
# start-session.sh ran in THIS tab and a stale/overlapping launch cannot stomp the proof — codex
# verify P1-b). If it never appears, the shim did not fire (not installed, or a deeper delivery
# failure) — fail LOUDLY without fabricating liveness, exactly as the old keystroke path did on a
# locked Mac.
#
# The loop watches for TWO outcomes, not one. The second is the auth marker (issue #299): a tab
# whose session ran the positive gh-auth assert and REFUSED will never stamp the sentinel, so
# without this it would look identical to a shim that never fired — and 30s later the park memo
# would send the owner to debug the launch shim while the real fault was dead GitHub auth. Keyed on
# the same per-launch token as the sentinel, so a stale or overlapping launch's marker cannot
# false-fire this one.
STARTED="$SL_RUN_ROOT/state/started/$ID.$SURF"
AUTHFAIL="$SL_RUN_ROOT/state/authfail/$ID.$SURF"
VERIFY_WINDOW="${SL_LAUNCH_VERIFY_SECONDS:-30}"   # generous single window; << the 45-min freeze
delivered=0
auth_dead=0
waited=0
while [ "$waited" -lt "$VERIFY_WINDOW" ]; do
  if [ -e "$STARTED" ]; then delivered=1; break; fi
  if [ -e "$AUTHFAIL" ]; then auth_dead=1; break; fi
  sleep 1; waited=$((waited + 1))
done

if [ "$auth_dead" -eq 1 ]; then
  # The session told us, from inside its own environment, that its gh auth is dead. Tear the tab
  # down on the SAME terms as a non-delivery — it never became a worker, so nothing of value is
  # lost — but exit with the DISTINCT auth code and speak the session's own diagnosis, so the
  # runner's memo names auth death instead of the launch machinery.
  why="$(cat "$AUTHFAIL" 2>/dev/null || true)"
  rm -f "$CMD_FILE" "$CMD_FILE".claimed.* "$STARTED" "$AUTHFAIL" 2>/dev/null || true
  "$CMUX" close-surface --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} >/dev/null 2>&1 || true
  echo "[$ID] GH AUTH DEAD in the session's own environment — the flight was refused before it started." >&2
  echo "[$ID] ${why:-the session reported no detail}" >&2
  echo "[$ID] Closed the tab; NOT marking active. This is an auth fault, not a launch-delivery one." >&2
  exit "$AUTH_DEAD_RC"
fi

if [ "$delivered" -ne 1 ]; then
  # FAIL LOUDLY and leave NO time-bomb. Remove the dropped command so no shell can pick it up later,
  # and CLOSE the orphan tab. Only a tab that NEVER became a worker is closed (its per-launch start
  # marker is absent → nothing of value is lost), so this never closes a real worker session.
  # start-session.sh's worker singleton lock is the backstop: at most ONE worker exists per id even
  # if a straggler shell runs the command late.
  rm -f "$CMD_FILE" "$CMD_FILE".claimed.* "$STARTED" "$AUTHFAIL" 2>/dev/null || true
  "$CMUX" close-surface --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} >/dev/null 2>&1 || true
  echo "[$ID] LAUNCH NOT DELIVERED: no worker started in tab $SURF within ${VERIFY_WINDOW}s." >&2
  echo "[$ID] the launch shim did not run the command — is it installed? (bin/install-launch-shim.sh)" >&2
  echo "[$ID] Closed the orphan tab; NOT marking active." >&2
  exit 2
fi

# ---- Delivery verified -> only NOW is it honest to record liveness + the pane. ----
rm -f "$CMD_FILE" "$CMD_FILE".claimed.* 2>/dev/null || true   # shim already claimed it; clean any leftover
date +%s > "$SL_RUN_ROOT/state/activity/$ID"           # freeze-net baseline (post-delivery only)
printf '%s' "$SURF" > "$SL_RUN_ROOT/state/panes/$ID"    # how the runner rings/reads this session
[ -n "$WS" ] && printf '%s' "$WS" > "$SL_RUN_ROOT/state/panes/$ID.ws"   # workspace for read/send addressing
rm -f "$STARTED" 2>/dev/null || true                   # the per-launch proof has served its purpose

# Honest retry telemetry (fact-4): stamp launches/retries mechanically at the ONE point a worker
# verifiably started — never AI-remembered (both audited autocode runs showed retries:0 beside real
# redos). retries = launches - 1 (first launch = 0). Worker ids only — a debugger (d<N>) is not a
# tracked issue, so it has no counter. setdefault covers an id the runner registered without a
# counter yet. Guarded: telemetry must never fail a delivered launch.
# A RESUME is deliberately NOT counted (#298): `retries` is mechanical telemetry about how many
# times a lane had to be STARTED OVER, and re-entering the same conversation is the opposite of
# starting over. Counting it would inflate the one number nobody can audit after the fact — the
# same honesty this block was written to protect.
if [ "$CWD_MODE" -eq 0 ] && [ -z "$RESUME" ]; then
  python3 - "$HERE/../lib" "$SL_RUN_ROOT/state/issues.json" "$ID" <<'PY' || echo "[$ID] WARN: launch counter not updated" >&2
import sys
sys.path.insert(0, sys.argv[1])
import loopstate
path, iid = sys.argv[2], sys.argv[3]
def bump(st):
    issue = st["issues"].setdefault(iid, loopstate.new_issue())
    issue["launches"] = issue.get("launches", 0) + 1
    issue["retries"] = max(issue["launches"] - 1, 0)
loopstate.update(path, bump)
PY
fi
echo "[launch] $ID  branch=${BRANCH:-<none>} tab=$SURF ws=${WS:-?} name='$NAME' (keystroke-free; delivery verified)"
