#!/usr/bin/env bash
# Mechanical launch of ONE session. Called by the runner (never a background watcher).
# Two modes:
#   launch-session.sh <i-id>            worker: create the issue's worktree off origin/<dev>, launch
#   launch-session.sh --cwd <dir> <a-id>  answerer: launch in an existing dir (no worktree/branch)
# Both open a visible cmux tab, pre-trust the folder, and fire the brief into an interactive
# bypass-permission Claude — via the keystroke-free launch shim, then VERIFY delivery.
set -euo pipefail

# ---- Parse mode + id ----------------------------------------------------------------------------
CWD_MODE=0
CWD=""
if [ "${1:-}" = "--cwd" ]; then
  CWD_MODE=1
  CWD="${2:?usage: launch-session.sh --cwd <dir> <a-id>}"
  ID_IN="${3:?usage: launch-session.sh --cwd <dir> <a-id>}"
else
  ID_IN="${1:?usage: launch-session.sh <i-id>  (or --cwd <dir> <a-id>)}"
fi

SL_RUN_ROOT="${SL_RUN_ROOT:?SL_RUN_ROOT required}"
# The cmux PANE that hosts all superlooper tabs (runner + every session) so they are grouped and
# watchable in one place, NOT scattered as separate workspaces. The runner sets this at startup.
SL_PANE="${SL_PANE:?SL_PANE (target cmux pane id for tabs) required}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Overridable so the delivery-verification test can inject a stub cmux; defaults to the real app.
CMUX="${SL_CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
MODEL="${SL_MODEL:-}"
EFFORT="${SL_EFFORT:-}"
CODEX_DANGEROUS_BYPASS="${SL_CODEX_DANGEROUS_BYPASS:-}"
CODEX_BYPASS_HOOK_TRUST="${SL_CODEX_BYPASS_HOOK_TRUST:-}"
CODEX_NO_ALT_SCREEN="${SL_CODEX_NO_ALT_SCREEN:-}"
AGENT="${SL_AGENT:-claude}"
# Is a PERSON at the keyboard for the session we are about to launch? Empty for every launch this
# script makes on the loop's behalf (workers, the watchdog's debugger); `1` only when the caller is
# `superlooper debug`'s owner tap (issue #144), which launches a d<N> session a human just asked
# for. The PreToolUse deny (issue #185) reads it to stand its AskUserQuestion duty down rather than
# tell an attended session the falsehood "no human is at this pane". Passed through verbatim — the
# callers pin it; this script does not judge it.
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
  # Answerer: no worktree, no branch. Validate the id, use the provided dir verbatim as the cwd.
  # (The runner passes the state-home answers/ dir; nothing here is created or checked out.)
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
  # --cwd is the in-place path: ANSWERER ids (a<N>) and the watchdog's unattended sl-debugger
  # sessions (d<N>, issue #66) — both launch in an existing dir, no worktree, no branch. The id
  # shape is enforced so a runner bug can never route an issue id (i<N>) through here and silently
  # skip worktree creation + the issue counter (fail closed on wrong-typed input, not just unsafe
  # input — cross-review, Task 6). worktree_id already rejected unsafe chars; this pins the mode's
  # contract.
  if ! [[ "$ID" =~ ^[ad][0-9]+$ ]]; then
    echo "[$ID] --cwd mode is for answerer (a<N>) / debugger (d<N>) ids only — refusing" >&2; exit 1
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
  # Worker path is for issue ids (i<N>) only — the symmetric contract to --cwd's a<N> check above,
  # so an answerer id can never accidentally spin up a worktree here (cross-review, Task 6).
  if ! [[ "$ID" =~ ^i[0-9]+$ ]]; then
    echo "[$ID] worker mode expects an issue id (i<N>) — refusing" >&2; exit 1
  fi
  BASE="origin/$BASE_BRANCH"
  WT="$SL_RUN_ROOT/worktrees/$ID"
  NAME="superlooper $ID ($BRANCH)"
fi

BRIEF="$SL_RUN_ROOT/briefs/$ID.md"
[ -f "$BRIEF" ] || { echo "[$ID] missing brief $BRIEF" >&2; exit 1; }

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
         "$SL_RUN_ROOT/reports"
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
printf -v CMD 'cd %q && SL_ISSUE_ID=%q SL_RUN_ROOT=%q SL_SESSION_NAME=%q SL_MODEL=%q SL_EFFORT=%q SL_AGENT=%q SL_ATTENDED=%q SL_CODEX_DANGEROUS_BYPASS=%q SL_CODEX_BYPASS_HOOK_TRUST=%q SL_CODEX_NO_ALT_SCREEN=%q SL_START_TOKEN=%q %q %q' \
  "$WT" "$ID" "$SL_RUN_ROOT" "$NAME" "$MODEL" "$EFFORT" "$AGENT" "$ATTENDED" "$CODEX_DANGEROUS_BYPASS" "$CODEX_BYPASS_HOOK_TRUST" "$CODEX_NO_ALT_SCREEN" "$SURF" "$HERE/start-session.sh" "$ID"
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
STARTED="$SL_RUN_ROOT/state/started/$ID.$SURF"
VERIFY_WINDOW="${SL_LAUNCH_VERIFY_SECONDS:-30}"   # generous single window; << the 45-min freeze
delivered=0
waited=0
while [ "$waited" -lt "$VERIFY_WINDOW" ]; do
  if [ -e "$STARTED" ]; then delivered=1; break; fi
  sleep 1; waited=$((waited + 1))
done

if [ "$delivered" -ne 1 ]; then
  # FAIL LOUDLY and leave NO time-bomb. Remove the dropped command so no shell can pick it up later,
  # and CLOSE the orphan tab. Only a tab that NEVER became a worker is closed (its per-launch start
  # marker is absent → nothing of value is lost), so this never closes a real worker session.
  # start-session.sh's worker singleton lock is the backstop: at most ONE worker exists per id even
  # if a straggler shell runs the command late.
  rm -f "$CMD_FILE" "$CMD_FILE".claimed.* "$STARTED" 2>/dev/null || true
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
# redos). retries = launches - 1 (first launch = 0). Worker ids only — an answerer (a<N>) is not a
# tracked issue, so it has no counter. setdefault covers an id the runner registered without a
# counter yet. Guarded: telemetry must never fail a delivered launch.
if [ "$CWD_MODE" -eq 0 ]; then
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
