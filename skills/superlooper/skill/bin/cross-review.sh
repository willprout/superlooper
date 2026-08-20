#!/usr/bin/env bash
# bin/cross-review.sh — pin the CROSS-REVIEWER's model + reasoning-effort per repo, run the review
# with them, and stamp the lane's PHASE around it.
#
# The pin is the mechanical fix for issue #158 / the 2026-07-14→15 incident: the plugin's
# cross-review ran `codex exec` BARE, so when the owner changed his machine-global
# ~/.codex/config.toml for unrelated work, every in-flight review silently inherited ultra effort,
# timed out, and aged workers past the freeze threshold. The truth for how a review is invoked must
# live in the loop, not in ambient machine state.
#
# Contract: read the prompt on STDIN, resolve `models.reviewer` / `models.reviewer_effort` from the
# repo's `.superlooper/config.json` (the per-repo pin — the loader fills concrete defaults, so the
# fields are ALWAYS present), and run `codex exec` with those as EXPLICIT flags, exiting with the
# reviewer's own status. It NEVER runs `codex` bare and NEVER reads ~/.codex/config.toml for the
# model/effort. If no config is resolvable, it FAILS LOUD rather than fall back to a bare
# (ambient-poisoned) invocation.
#
# Phase contract (issue #443): a worker builds, cross-reviews, pushes and files its report inside
# ONE session, and the engine advances a lane on journal LANDMARKS only — so a lane read "building"
# for essentially its whole flight and then flicked through every remaining stage in a tick or two.
# The cross-review is the one long sub-step the engine can honestly sense, BECAUSE it runs through
# this script: engine-owned code the worker merely invokes. So this stamps `state/phase/<id>` when
# the review starts and again when it ends, and the runner reads the file — no screen read, no new
# GitHub read, and no reliance on a worker remembering to announce anything. The stamp is a LABEL
# ON A BOARD and nothing more: it holds no launch, raises no alert, and reaches no decision, so
# every failure to write it is swallowed and the review runs regardless.
#
# AGENT BOUNDARY: like start-session.sh, this is the ONE place the codex-specific review command
# line (`-m`, `-c model_reasoning_effort=`) lives. The pin itself is per-repo CONFIG, never a
# hardcoded Codex fact — swap the reviewer by editing `models.reviewer`, not this script. On a
# Claude-only machine the cross-review skill uses a fresh subagent instead and never calls this.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LIB="$SCRIPT_DIR/../lib"

# TOML-quote a value so a quote/backslash inside it can't break out of the -c assignment (the exact
# escaping start-session.sh's codex branch uses).
toml_string() {
  local s="${1:-}"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# Find the repo root by walking up from the review's working dir (SL_REVIEW_REPO_ROOT overrides the
# start point; tests use it, and it also lets a caller review from outside the tree). Git-independent
# so it works in any worktree and needs no `git` on PATH. The start point is canonicalized to an
# ABSOLUTE path first (`cd … && pwd`) — a relative start would let `dirname` collapse to "." and
# loop forever, and a bad SL_REVIEW_REPO_ROOT falls back to $PWD (still absolute) rather than spin.
find_repo_root() {
  local d
  d="$(cd "${SL_REVIEW_REPO_ROOT:-$PWD}" 2>/dev/null && pwd)" || d="$PWD"
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    [ -f "$d/.superlooper/config.json" ] && { printf '%s' "$d"; return 0; }
    d="$(dirname "$d")"
  done
  [ -f "/.superlooper/config.json" ] && { printf '%s' "/"; return 0; }
  return 1
}

REPO_ROOT="$(find_repo_root)" || {
  echo "[cross-review] no .superlooper/config.json found from '$PWD' up — cannot pin the reviewer" \
       "model/effort. Refusing to run \`codex\` bare (a bare review would silently inherit" \
       "~/.codex/config.toml, the exact ambient-poison issue #158 ends). Run from inside a" \
       "superlooper-configured repo, or pass the equivalent explicit flags yourself." >&2
  exit 1
}

# Resolve the pin through the real config loader (validation + concrete defaults). tab-separated so a
# value never splits; STDERR is captured SEPARATELY (not folded into stdout) so a stray loader
# warning can never corrupt the parsed model/effort — only the clean stdout feeds `read`. rc is
# captured immediately so a load error fails LOUD instead of degrading to a bare invocation.
_pin_err="$(mktemp "${TMPDIR:-/tmp}/sl-review-pin.XXXXXX")" || _pin_err=/dev/null
RESOLVED="$(PYTHONPATH="$LIB" python3 -c '
import sys, config
c = config.load(sys.argv[1])
m = c["models"]["reviewer"]
e = c["models"]["reviewer_effort"]
sys.stdout.write(m + "\t" + e)
' "$REPO_ROOT" 2>"$_pin_err")"
_pin_rc=$?
_pin_msg="$(cat "$_pin_err" 2>/dev/null || true)"; [ "$_pin_err" = /dev/null ] || rm -f "$_pin_err"
if [ "$_pin_rc" -ne 0 ] || [ -z "$RESOLVED" ]; then
  echo "[cross-review] could not resolve the reviewer pin from $REPO_ROOT/.superlooper/config.json:" \
       "$_pin_msg — refusing to run codex bare." >&2
  exit 1
fi
IFS=$'\t' read -r MODEL EFFORT <<<"$RESOLVED"
if [ -z "${MODEL:-}" ] || [ -z "${EFFORT:-}" ]; then
  echo "[cross-review] resolved an empty reviewer model/effort ('$MODEL'/'$EFFORT') —" \
       "refusing to run codex bare." >&2
  exit 1
fi

# LAUNCH EVIDENCE (DoD #2): surface the pinned values so a review that ran at the wrong tier is
# diagnosable. (1) a stderr line — lands in the worker's transcript, the session's evidence surface;
# (2) a durable state file when running inside a loop worker — readable off-session by the runner or
# the owner, next to the other state markers.
echo "[cross-review] pinned reviewer: model=$MODEL reasoning_effort=$EFFORT" \
     "(from $REPO_ROOT/.superlooper/config.json models.reviewer/reviewer_effort — NOT ~/.codex/config.toml)" >&2
if [ -n "${SL_RUN_ROOT:-}" ] && [ -n "${SL_ISSUE_ID:-}" ]; then
  if mkdir -p "$SL_RUN_ROOT/state/review_pin" 2>/dev/null; then
    printf '%s model=%s reasoning_effort=%s repo=%s\n' \
      "$(date +%s)" "$MODEL" "$EFFORT" "$REPO_ROOT" \
      > "$SL_RUN_ROOT/state/review_pin/$SL_ISSUE_ID" 2>/dev/null || true
  fi
fi

# PHASE BREADCRUMB (issue #443). One line, the state home's own house style — a leading epoch then
# `key=value`, exactly what `state/exited` (`<epoch> rc=<n>`) and the pin file above already use.
# Only inside a loop worker (same guard as the pin evidence): run standalone this script is just a
# review command and must invent no lane state anywhere.
#
# Written to a temp file and RENAMED, because the runner re-reads this file every tick while the
# review runs and must never catch it half-written. The temp name is FIXED (not mktemp) and
# dot-prefixed: fixed so a stamp killed between create and rename is overwritten by the next one
# instead of accumulating, and dot-prefixed so no directory scan can ever read one as a lane marker
# (the .DS_Store rule the runner's marker scans already keep). One session owns one lane and the
# review is synchronous, so nothing else is ever writing this name.
# Every failure path returns 0: losing the label must never cost the review.
_phase_stamp() {                       # $1 = start|end, $2 = the review's rc (end only)
  [ -n "${SL_RUN_ROOT:-}" ] && [ -n "${SL_ISSUE_ID:-}" ] || return 0
  local dir="$SL_RUN_ROOT/state/phase" line tmp
  mkdir -p "$dir" 2>/dev/null || return 0
  line="$(date +%s) phase=cross-reviewing event=$1"
  [ -n "${2:-}" ] && line="$line rc=$2"
  tmp="$dir/.$SL_ISSUE_ID.tmp"
  if printf '%s\n' "$line" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$dir/$SL_ISSUE_ID" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
  fi
  return 0
}

# Stamped only from HERE — past every refusal above — so a lane never reads "cross-reviewing" for a
# review that was refused and never ran. The end stamp rides an EXIT trap rather than a line after
# the review, so it lands whether codex succeeded, failed, or the script was signalled: a start with
# no end would pin the lane at "cross-reviewing" for the rest of its flight, the same lie this issue
# exists to end, in a different place. bash runs an EXIT trap for a SIGTERM/SIGINT delivered to the
# process group — the realistic kill (a tool timeout, a Ctrl-C), and what the tests drive — but not
# for a SIGKILL, which is what the reader's staleness rule is for. The `rc=` it records is
# BEST-EFFORT diagnostic: on a signal path `$?` is the last completed command's status, not the
# reviewer's. `event=end` is the load-bearing half; nothing reads `rc`.
_phase_stamp start
trap '_phase_stamp end "$?"' EXIT

# Run the review: interactive-free `codex exec -` reads the prompt from OUR stdin (the caller's
# prompt); the -m / -c flags are pinned above, so codex never consults ~/.codex/config.toml for
# them. It is the LAST command, so this script exits with the reviewer's own status and the caller
# sees the review's outcome unchanged.
#
# Deliberately NOT `exec` any more (it was, until #443): exec replaces this process, and a replaced
# process cannot stamp its own end. codex still runs in the foreground of the same process group
# and inherits this script's stdin/stdout/stderr and tty directly, so the review itself is
# unaffected — the cost is one bash process left waiting, which is what buys the end stamp.
codex exec -m "$MODEL" -c "model_reasoning_effort=$(toml_string "$EFFORT")" -
