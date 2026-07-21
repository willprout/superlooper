#!/usr/bin/env bash
# Mechanically RESTART a provably-gone runner (issue #208). Called ONLY by `superlooper watchdog`
# after watchdog.evaluate() decides a runner is provably gone — heartbeat stale AND its recorded pid
# dead. It starts `superlooper run` (a DETERMINISTIC, ZERO-TOKEN process — never an LLM) in a fresh
# visible cmux tab in the dead runner's recorded anchor pane, using the SAME keystroke-free launch
# shim workers use, then VERIFIES the runner actually came up before returning 0.
#
# Usage: resurrect-runner.sh --cwd <repo> <r-id>       (r-id is r<N>, minted by the watchdog)
#
# PLACEMENT (the DoD asks the implementer to state this): a NEW tab in the runner's OWN recorded
# pane group — NOT a worker tab, NOT the dead runner's stale shell. Two reasons it must be a fresh
# surface rather than the dead runner's own tab:
#   * cmux keystroke delivery is GATED by macOS while the display sleeps (RC6, the overnight launch
#     killer) — the exact 1am scenario this issue names. The keystroke-FREE shim only works in a
#     FRESH shell that sources it at boot; the dead runner's existing shell already sourced it once
#     and will not re-run it. A new surface spawns a fresh shell that self-runs the dropped command
#     regardless of display state.
#   * launchd/nohup placement is ruled out: a paneless runner fails preflight_pane and every worker
#     launch dies with "Broken pipe" (finding D7). The runner must live in a real cmux tab.
# The reborn runner detects its OWN pane in the new tab and births workers there (same-pane group,
# adjacent to the old). It reconciles from GitHub + disk exactly like a MANUAL restart — so this
# script deliberately touches NO issues.json, NO launch/retry counters, NO worktrees. The only
# state it writes is the transient launch-shim command file; everything else is the runner's own.
set -euo pipefail

# ---- Parse (mirrors launch-session.sh --cwd) ----------------------------------------------------
[ "${1:-}" = "--cwd" ] || { echo "usage: resurrect-runner.sh --cwd <repo> <r-id>" >&2; exit 64; }
CWD="${2:?usage: resurrect-runner.sh --cwd <repo> <r-id>}"
ID_IN="${3:?usage: resurrect-runner.sh --cwd <repo> <r-id>}"
# Runner-resurrection ids are r<N> ONLY — the symmetric contract to launch-session.sh's d<N>
# --cwd ids, so a stray issue/debugger id can never route a session launch through the runner path.
# ANCHORED (^r[0-9]+$), matching launch-session.sh: a `case` glob of `r[0-9]*` accepts everything
# trailing the first digit ("r1; touch ..."), which today is harmless only because $ID reaches
# nothing but quoted argv. Refuse at the door rather than rest on that downstream accident.
if [[ "$ID_IN" =~ ^r[0-9]+$ ]]; then
  ID="$ID_IN"
else
  echo "[$ID_IN] resurrect-runner.sh takes a runner id (r<N>) only — refusing" >&2; exit 64
fi

SL_RUN_ROOT="${SL_RUN_ROOT:?SL_RUN_ROOT required}"
# The cmux PANE the dead runner recorded as its anchor — the new runner tab is born here so it is
# grouped with (and adjacent to) where the loop was already running/watchable.
SL_PANE="${SL_PANE:?SL_PANE (target cmux pane id) required}"
# The superlooper CLI to (re)start. The watchdog passes its OWN resolved bin path so a fresh tab
# shell (which inherits none of the watchdog's environment) runs the exact installed engine.
SL_SUPERLOOPER_BIN="${SL_SUPERLOOPER_BIN:?SL_SUPERLOOPER_BIN (path to the superlooper CLI) required}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CMUX="${SL_CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

[ -d "$CWD" ] || { echo "[$ID] repo dir does not exist: $CWD" >&2; exit 1; }
# ABSOLUTE physical path: the dropped `cd %q` runs in the NEW tab's fresh shell, which starts in its
# own default dir, so a relative path would `cd` into nothing there and the launch would not verify.
REPO="$(cd "$CWD" && pwd -P)"

# The DEAD runner's pid (the pidfile still names it — that is why we are here). Captured up front so
# the verify below can require the lock to be REWRITTEN to a DIFFERENT live pid: a fresh `superlooper
# run` steals the lock and writes its own pid, so `pid != OLD_PID` proves the NEW runner acquired it
# — and it closes the narrow PID-reuse window where the dead pid gets recycled to an unrelated live
# process before the new runner boots (kill -0 would otherwise false-verify on it).
OLD_PID="$(cat "$SL_RUN_ROOT/state/runner.lock" 2>/dev/null | tr -d '[:space:]' || true)"

UUID_RE='[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}'
LAUNCH_DIR="${SL_LAUNCH_DIR:-$HOME/.superlooper/launch}"
mkdir -p "$LAUNCH_DIR"; chmod 700 "$LAUNCH_DIR" 2>/dev/null || true
# Mark a launch IN-FLIGHT *before* creating the surface so the fast-booting new shell sees a fresh
# .active marker and WAITS for its command file (closes the shell-boot race — launch-session.sh's
# lesson). The per-tab command file is written just after new-surface returns.
: > "$LAUNCH_DIR/.active"

# ---- Create the runner's new tab in the anchor pane --------------------------------------------
OUT="$("$CMUX" --id-format uuids new-surface --type terminal --pane "$SL_PANE")" || {
  echo "[$ID] new-surface failed (rc=$?) targeting pane '$SL_PANE' — the runner's recorded pane is gone; cannot resurrect without you." >&2; exit 1; }
SURF="$(printf '%s\n' "$OUT" | grep -oE "$UUID_RE" | sed -n 1p || true)"   # OK <surf> <pane> <ws>
WS="$(printf '%s\n' "$OUT" | grep -oE "$UUID_RE" | sed -n 3p || true)"
[ -n "$SURF" ] || { echo "[$ID] could not parse a surface UUID from new-surface output: $OUT" >&2; exit 1; }
WS_ARGS=()
[ -n "$WS" ] && WS_ARGS=(--workspace "$WS")

# ---- KEYSTROKE-FREE DELIVERY (RC6) --------------------------------------------------------------
# Drop the runner start command in a file keyed by this tab's surface UUID; the launch shim, sourced
# by the new tab's fresh shell, reads and runs it (no keystrokes, no display dependency). We start
# `superlooper run` with an EXPLICIT --repo so it targets the right repo, and the runner detects its
# own pane from this new tab. %q-quote the paths so a space/quote can't break or inject the command.
printf -v CMD 'cd %q && %q run --repo %q' "$REPO" "$SL_SUPERLOOPER_BIN" "$REPO"
CMD_FILE="$LAUNCH_DIR/$SURF.cmd"
cmd_tmp="$(mktemp "$LAUNCH_DIR/.cmd.XXXXXX")"
printf '%s' "$CMD" > "$cmd_tmp"
mv -f "$cmd_tmp" "$CMD_FILE"                    # atomic: the shim never reads a half-written command
: > "$LAUNCH_DIR/.active"                       # refresh mtime across the verify window (shim's gate)
"$CMUX" rename-tab --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} "superlooper runner ($ID)" \
  >/dev/null 2>&1 || true

# ---- VERIFY the runner actually came up ---------------------------------------------------------
# The runner's boot proof is its own pidfile: `superlooper run` acquires the singleton and writes a
# LIVE pid into state/runner.lock. The dead runner left that file naming a DEAD pid (that is exactly
# why we are here), so kill -0 stays false until the NEW runner steals the lock and writes its live
# pid. Poll for that — a REAL "the loop is running again" proof, not a fabricated one. If it never
# appears (shim not installed, pane vanished, preflight failed) we fail LOUDLY and close the orphan
# tab, never claiming a restart that did not happen.
LOCK="$SL_RUN_ROOT/state/runner.lock"
VERIFY_WINDOW="${SL_LAUNCH_VERIFY_SECONDS:-30}"
up=0
waited=0
runner_up() {
  pid="$(cat "$LOCK" 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$pid" ] && [ "$pid" != "$OLD_PID" ] \
    && printf '%s' "$pid" | grep -qE '^[0-9]+$' && kill -0 "$pid" 2>/dev/null
}
while [ "$waited" -lt "$VERIFY_WINDOW" ]; do
  if runner_up; then up=1; break; fi
  sleep 1; waited=$((waited + 1))
done
# One FINAL read before concluding: the loop's last read happens a full second before the window
# actually closes, and a runner that acquires the lock inside that gap is UP. Concluding "not
# verified" there would close-surface a LIVE runner's tab (SIGHUP to a running loop) and page the
# owner about a restart that in fact succeeded — a self-inflicted outage. Costs one file read.
if [ "$up" -ne 1 ] && runner_up; then up=1; fi

if [ "$up" -ne 1 ]; then
  # Leave no time-bomb: remove the dropped command so no straggler shell starts a second runner, and
  # close the orphan tab (the runner never came up, so nothing of value is lost — the runner's own
  # pidfile singleton is the backstop even if a late shell runs the command).
  rm -f "$CMD_FILE" "$CMD_FILE".claimed.* 2>/dev/null || true
  "$CMUX" close-surface --surface "$SURF" ${WS_ARGS[@]+"${WS_ARGS[@]}"} >/dev/null 2>&1 || true
  echo "[$ID] RESURRECTION NOT VERIFIED: the runner did not come up in tab $SURF within ${VERIFY_WINDOW}s." >&2
  echo "[$ID] is the launch shim installed (bin/install-launch-shim.sh) and does the recorded pane still exist?" >&2
  exit 2
fi

rm -f "$CMD_FILE" "$CMD_FILE".claimed.* 2>/dev/null || true   # shim already claimed it; clean leftovers
echo "[resurrect] $ID  runner restarted; tab=$SURF ws=${WS:-?} pid=$pid (keystroke-free; verified live)"
