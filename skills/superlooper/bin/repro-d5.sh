#!/usr/bin/env bash
# repro-d5.sh — Task-16 D5 isolation harness (2026-07-03 live dry-run, findings doc D5).
#
# QUESTION UNDER TEST: after the D4 fix (close the finished-but-alive session, free the
# worker lock), issue #1's same-id relaunch STILL failed delivery (journal: four
# "launch rc=2 (delivery not verified)"). Is close-stale-session -> IMMEDIATE same-id
# relaunch a real race on real cmux, or was it #1's mangled state (~6 hand-surgery
# recovery cycles)? This reproduces the exact sequence from a CLEAN slate, N times,
# with NO GitHub and NO runner — just the launch stack the runner calls:
#
#   cycle k:  [k>1: replicate runner._close_stale_session verbatim —
#              cmux close-surface on the recorded pane, rm pane markers + worker lock]
#             re-point issues.json at a fresh -r<k> branch (regenerate's branch move)
#             bash -x launch-session.sh i1   (the REAL script, full trace captured)
#             rc==0 -> delivered; rc==2 -> D5 REPRODUCED
#             then VERIFY THE PREMISE: the worker lock names a LIVE pid and no exited
#             marker appeared — a claude that died at boot (bad model/auth) would make
#             every later "delivered" cycle meaningless (Codex review C1), so a broken
#             premise aborts the run loudly instead of reporting hollow PASSes.
#
# Instrumentation (the live failure path rm's its own evidence, so we watch it happen):
#   xtrace-<k>.log    every step of launch-session.sh, incl. raw new-surface output
#   samples-<k>.log   4x/s: launch-dir listing, started/ sentinels, worker lock, pgrep
#
# Fidelity notes: the launch dir must be the REAL ~/.superlooper/launch (the new tab's
# shell knows only its CMUX_SURFACE_ID + the fixed well-known path — do NOT override
# SL_LAUNCH_DIR); the shim must be the installed one (~/.zshrc sources it). The worker
# is a REAL interactive claude told to idle — it must stay at its prompt holding the
# singleton lock, exactly like a finished live worker (the D4/D5 precondition).
#
# Usage:  SL_PANE=<cmux-pane-uuid> bin/repro-d5.sh [cycles]   (default 5)
# Safe: everything lives under ~/.superlooper/d5-repro/<ts>/ (kept for inspection);
# only tabs THIS script opened are closed (on exit/interrupt too — trap below); the
# loop's sandbox state is never touched. Every wait is bounded (watchdog rule).
set -euo pipefail

CYCLES="${1:-5}"
SL_PANE="${SL_PANE:?SL_PANE (cmux pane uuid hosting the repro tabs) required}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_SH="$HERE/skill/bin/launch-session.sh"
[ -f "$LAUNCH_SH" ] || { echo "no $LAUNCH_SH — run from the repo"; exit 1; }
CMUX="${SL_CMUX:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
LAUNCH_DIR="$HOME/.superlooper/launch"       # the real well-known path — NEVER overridden
LAUNCH_DEADLINE=120                          # outer watchdog, mirrors the runner's LAUNCH_TIMEOUT

TS="$(date +%Y%m%d-%H%M%S)"
ROOT="$HOME/.superlooper/d5-repro/$TS"
RUN="$ROOT/run"                               # plays SL_RUN_ROOT (the state home)
mkdir -p "$RUN/state/panes" "$RUN/briefs" "$RUN/reports" "$RUN/worktrees" "$RUN/logs"

echo "== D5 repro: $CYCLES close->relaunch cycles; artifacts in $ROOT"

# --- a tiny local git pair (bare origin + clone) so worker-mode worktree creation works ---
ORIGIN="$ROOT/origin"; REPO="$ROOT/repo"
git init -q --bare -b main "$ORIGIN"
git clone -q "$ORIGIN" "$REPO"
( cd "$REPO" && echo repro > README.md && git add README.md \
  && git -c user.name=d5 -c user.email=d5@repro commit -qm seed && git push -q origin main )

# --- the do-nothing brief: a real claude session that answers once and IDLES at its prompt ---
cat > "$RUN/briefs/i1.md" <<'BRIEF'
You are a launch-delivery test probe (superlooper D5 repro). Do NOTHING: run no tools,
edit no files, run no commands. Reply with exactly: "D5 probe up." and end your turn.
Stay at your prompt; you may be closed at any moment. Everything here is expected.
BRIEF

seed_branch() {  # issues.json -> branch sl/i1-d5-r<k> (regenerate's branch move, minimally)
  python3 - "$RUN/state/issues.json" "$1" <<'PY'
import json, sys
path, branch = sys.argv[1], sys.argv[2]
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {"issues": {}}
st.setdefault("issues", {}).setdefault("i1", {})["branch"] = branch
json.dump(st, open(path, "w"))
PY
}

sampler() {      # 4x/s snapshot of everything the failure path would otherwise destroy
  local out="$1"
  while :; do
    {
      echo "--- $(date '+%H:%M:%S') ---"
      ls -la "$LAUNCH_DIR" 2>/dev/null | tail -n +2
      echo "started: $(ls "$RUN/state/started" 2>/dev/null | tr '\n' ' ')"
      echo "lock: $(cat "$RUN/state/worker.i1.lock" 2>/dev/null || echo none)"
      echo "procs: $(pgrep -lf 'start-session.sh i1' 2>/dev/null | tr '\n' ';')"
    } >> "$out" 2>/dev/null
    sleep 0.25
  done
}

close_stale() {  # runner._close_stale_session, replicated verbatim (close pane, rm markers+lock)
  local surf ws
  surf="$(cat "$RUN/state/panes/i1" 2>/dev/null || true)"
  ws="$(cat "$RUN/state/panes/i1.ws" 2>/dev/null || true)"
  if [ -n "$surf" ]; then
    if [ -n "$ws" ]; then
      "$CMUX" close-surface --surface "$surf" --workspace "$ws" >/dev/null 2>&1 || true
    else
      "$CMUX" close-surface --surface "$surf" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$RUN/state/panes/i1" "$RUN/state/panes/i1.ws" "$RUN/state/worker.i1.lock"
}

SAMP=""
LAUNCH_PID=""
cleanup() {      # runs on EXIT/INT/TERM: never leave a sampler, a hung launch, or an open
  # permission-bypassed claude tab behind (Codex review C2 + the standing watchdog rule)
  [ -n "$SAMP" ] && kill "$SAMP" 2>/dev/null || true
  [ -n "$LAUNCH_PID" ] && kill "$LAUNCH_PID" 2>/dev/null || true
  close_stale
}
trap cleanup EXIT
# INT/TERM must TERMINATE, not fall back into the measurement loop (which runs under set +e
# and would classify the interruption as a launch failure — Codex round 2). cleanup is
# idempotent, so the EXIT trap re-running it after these is harmless.
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

premise_ok() {   # the finished-but-alive state really holds: live lock pid, no exited marker
  local pid waited=0
  while [ "$waited" -lt 20 ]; do
    pid="$(cat "$RUN/state/worker.i1.lock" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ ! -e "$RUN/state/exited/i1" ]; then
      return 0
    fi
    if [ -e "$RUN/state/exited/i1" ]; then
      echo "   PREMISE BROKEN: claude exited at boot ($(cat "$RUN/state/exited/i1"))" >&2
      return 1
    fi
    sleep 1; waited=$((waited + 1))
  done
  echo "   PREMISE BROKEN: no live worker lock within 20s" >&2
  return 1
}

pass=0; fail=0; results=""
for k in $(seq 1 "$CYCLES"); do
  echo
  echo "== cycle $k/$CYCLES =="
  if [ "$k" -gt 1 ]; then
    echo "   close_stale (runner D4 path) -> IMMEDIATE relaunch"
    close_stale
  fi
  seed_branch "sl/i1-d5-r$k"

  sampler "$ROOT/samples-$k.log" & SAMP=$!
  set +e             # the launch's nonzero rc IS the measurement — don't die on it
  SL_RUN_ROOT="$RUN" SL_REPO="$REPO" SL_DEV_BRANCH=main SL_PANE="$SL_PANE" \
    SL_MODEL="${SL_MODEL:-haiku}" \
    bash -x "$LAUNCH_SH" i1 > "$ROOT/xtrace-$k.log" 2>&1 & LAUNCH_PID=$!
  waited=0; rc=""
  while [ "$waited" -lt "$LAUNCH_DEADLINE" ]; do    # outer watchdog: a hung launch is killed
    if ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      wait "$LAUNCH_PID"; rc=$?; break
    fi
    sleep 1; waited=$((waited + 1))
  done
  if [ -z "$rc" ]; then
    kill "$LAUNCH_PID" 2>/dev/null; wait "$LAUNCH_PID" 2>/dev/null
    rc=124
  fi
  LAUNCH_PID=""
  set -e
  kill "$SAMP" 2>/dev/null || true; wait "$SAMP" 2>/dev/null || true; SAMP=""

  if [ "$rc" -eq 0 ]; then
    # settle FIRST, then verify the premise: the delivery sentinel is stamped BEFORE claude
    # runs, so a probe that dies during boot would pass an immediate check and exit during
    # the settle (Codex round 2) — checking after the settle window closes that hole
    sleep 4
    if premise_ok; then
      pass=$((pass+1)); results="$results $k:PASS"
      echo "   cycle $k: DELIVERED (rc=0) pane=$(cat "$RUN/state/panes/i1" 2>/dev/null)"
    else
      results="$results $k:PREMISE-BROKEN"
      echo "   cycle $k: delivered but the worker did not stay alive — fix the probe" >&2
      echo "   (a dead probe cannot exercise the close->relaunch race; aborting)" >&2
      exit 3
    fi
  else
    fail=$((fail+1)); results="$results $k:FAIL(rc=$rc)"
    echo "   cycle $k: NOT DELIVERED rc=$rc  *** D5 REPRODUCED ***"
    echo "   evidence: $ROOT/xtrace-$k.log  $ROOT/samples-$k.log"
    tail -5 "$ROOT/xtrace-$k.log" | sed 's/^/   | /'
  fi
done

echo
echo "== D5 repro done: $pass delivered / $fail failed of $CYCLES —$results"
echo "== artifacts kept: $ROOT (last repro tab closed by the exit trap)"
[ "$fail" -eq 0 ] && exit 0 || exit 2
