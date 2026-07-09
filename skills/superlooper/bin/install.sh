#!/usr/bin/env bash
# bin/install.sh — the explicit publish step (plan Task 14, locked decision B.3).
#
# A deliberate "publish button": copy the finished skill payload from THIS source repo into the
# live ~/.claude/skills/superlooper/, register the two activity hooks, and install the launch shim.
# NEVER a symlink — a symlink would leak half-finished edits into live sessions and a running loop.
# Run it by hand when you want the installed copy to catch up with this repo; dev churn here never
# touches ~/.claude until you do.
#
# Idempotent: re-running re-syncs the payload, never duplicates a hook or the shim block, and leaves
# an unchanged settings.json byte-for-byte. --dry-run prints what WOULD change and writes nothing.
#
# The settings.json merge is python stdlib json, NOT jq (cross-review M4): jq stays only inside the
# ported pretrust.sh, which `doctor` checks for. This script reuses pretrust.sh's disciplines: an
# atomic tmp+rename (a reader always sees the whole old OR whole new file — never a partial write)
# guarantees no corruption, and a best-effort flock serializes concurrent superlooper installs so
# they don't lose each other's edit. A wrong-typed existing settings.json fails closed, never
# clobbered.
set -euo pipefail

DRY=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    -h|--help) echo "usage: install.sh [--dry-run]"; exit 0 ;;
    *) echo "install: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
SRC="$REPO_ROOT/skill"                                   # the publishable payload
DEST="$HOME/.claude/skills/superlooper"                  # the live installed copy
SETTINGS_DIR="$HOME/.claude"
SETTINGS="$SETTINGS_DIR/settings.json"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_HOOKS="$CODEX_DIR/hooks.json"

# The two hooks, registered EXACTLY as autocode registers its own (decision B.3): $HOME is left
# LITERAL — Claude Code expands it when it fires the hook, so the entry is portable across HOMEs.
# Both hooks are strict no-ops in any session that isn't a superlooper worker (they exit early
# unless SL_ISSUE_ID + SL_RUN_ROOT are exported), so registering them globally is safe.
ACT_CMD='$HOME/.claude/skills/superlooper/bin/activity-hook.sh'
STOP_CMD='$HOME/.claude/skills/superlooper/bin/stop-hook.sh'

[ -d "$SRC" ] || { echo "install: payload not found at $SRC" >&2; exit 1; }

# VERSION stamp: git SHA of THIS source repo + the install date. `nogit` if the tree has no git
# (published tarball); the date always lands so a VERSION is never empty.
SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
DATE="$(date +%Y-%m-%d)"                                 # Mac-local, matching the loop's clock convention
VERSION="$SHA $DATE"

# --- the settings.json hook-merge, as a stdlib-json helper (no jq). mode=apply writes atomically;
#     mode=report only prints. Fails CLOSED (exit 2, file untouched) on a wrong-typed settings.json
#     — a malformed hooks section must never be silently overwritten (the fail-open-on-wrong-typed
#     defect class this project has hit twice). ---
merge_hooks() {  # merge_hooks <mode:apply|report>
  local mode="$1"
  python3 - "$SETTINGS" "$mode" "$ACT_CMD" "$STOP_CMD" <<'PY'
import json, os, sys, tempfile

path, mode, act_cmd, stop_cmd = sys.argv[1:5]
targets = [("PostToolUse", act_cmd), ("Stop", stop_cmd)]  # (event, command)

def fail(msg):
    sys.stderr.write("install: " + msg + " — refusing to overwrite %s.\n" % path)
    sys.exit(2)

# Load (missing/empty file -> fresh {}; unreadable JSON -> fail closed, never clobber).
settings = {}
if os.path.exists(path):
    raw = open(path).read()
    if raw.strip():
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as e:
            fail("%s is not valid JSON (%s)" % (path, e))
if not isinstance(settings, dict):
    fail("%s top-level is %s, expected a JSON object" % (path, type(settings).__name__))

hooks = settings.get("hooks", {})
if not isinstance(hooks, dict):
    fail("%s 'hooks' is %s, expected an object" % (path, type(hooks).__name__))

# Every (event, command) pair already registered — keyed by EVENT, not by command alone, so a hook
# counts as present only under its REQUIRED event (activity-hook -> PostToolUse, stop-hook -> Stop);
# the same command mis-registered under the wrong event does NOT suppress the correct insertion.
# Fail CLOSED on ANY wrong-typed node (an event's list, a group, a group's 'hooks' list, a hook
# entry, or a non-string command): if we cannot fully parse the existing structure we cannot know
# whether our hook is already there, and guessing risks a duplicate or a clobber — the exact
# wrong-typed-input defect class. MISSING keys are tolerated ("no command here"); only WRONG types
# fail.
have = set()
for event, groups in hooks.items():
    if not isinstance(groups, list):
        fail("hooks['%s'] is %s, expected a list" % (event, type(groups).__name__))
    for g in groups:
        if not isinstance(g, dict):
            fail("a hooks['%s'] entry is %s, expected an object" % (event, type(g).__name__))
        ghooks = g.get("hooks", [])
        if not isinstance(ghooks, list):
            fail("a hooks['%s'] group's 'hooks' is %s, expected a list" % (event, type(ghooks).__name__))
        for h in ghooks:
            if not isinstance(h, dict):
                fail("a hooks['%s'] hook entry is %s, expected an object" % (event, type(h).__name__))
            cmd = h.get("command")
            if cmd is not None and not isinstance(cmd, str):
                fail("a hooks['%s'] hook has a non-string 'command'" % event)
            if isinstance(cmd, str):
                have.add((event, cmd))

added = []
for event, cmd in targets:
    if (event, cmd) in have:
        print("  present   : %-11s <- %s" % (event, cmd))
        continue
    added.append((event, cmd))
    if mode == "apply":
        hooks.setdefault(event, []).append(
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    print("  %-9s : %-11s <- %s" % ("added" if mode == "apply" else "would add", event, cmd))

# Only rewrite when something actually changed — an idempotent re-run leaves the file byte-for-byte
# unchanged. Atomic: write a sibling tmp file, fsync-free rename over the original (same-dir mkstemp
# guarantees a same-filesystem, atomic os.replace).
if mode == "apply" and added:
    settings["hooks"] = hooks
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
PY
}

merge_codex_hooks() {  # merge_codex_hooks <mode:apply|report>
  local mode="$1"
  python3 - "$CODEX_HOOKS" "$mode" "$ACT_CMD" "$STOP_CMD" <<'PY'
import json, os, sys, tempfile

path, mode, act_cmd, stop_cmd = sys.argv[1:5]
targets = [("PostToolUse", act_cmd), ("Stop", stop_cmd)]

def fail(msg):
    sys.stderr.write("install: " + msg + " — refusing to overwrite %s.\n" % path)
    sys.exit(2)

doc = {}
if os.path.exists(path):
    raw = open(path).read()
    if raw.strip():
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            fail("%s is not valid JSON (%s)" % (path, e))
if not isinstance(doc, dict):
    fail("%s top-level is %s, expected a JSON object" % (path, type(doc).__name__))

hooks = doc.get("hooks", {})
if not isinstance(hooks, dict):
    fail("%s 'hooks' is %s, expected an object" % (path, type(hooks).__name__))

have = set()
for event, groups in hooks.items():
    if not isinstance(groups, list):
        fail("hooks['%s'] is %s, expected a list" % (event, type(groups).__name__))
    for g in groups:
        if not isinstance(g, dict):
            fail("a hooks['%s'] entry is %s, expected an object" % (event, type(g).__name__))
        ghooks = g.get("hooks", [])
        if not isinstance(ghooks, list):
            fail("a hooks['%s'] group's 'hooks' is %s, expected a list" % (event, type(ghooks).__name__))
        for h in ghooks:
            if not isinstance(h, dict):
                fail("a hooks['%s'] hook entry is %s, expected an object" % event)
            cmd = h.get("command")
            if cmd is not None and not isinstance(cmd, str):
                fail("a hooks['%s'] hook has a non-string 'command'" % event)
            if isinstance(cmd, str):
                have.add((event, cmd))

added = []
for event, cmd in targets:
    if (event, cmd) in have:
        print("  present   : %-11s <- %s" % (event, cmd))
        continue
    added.append((event, cmd))
    if mode == "apply":
        hooks.setdefault(event, []).append(
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    print("  %-9s : %-11s <- %s" % ("added" if mode == "apply" else "would add", event, cmd))

if mode == "apply" and added:
    doc["hooks"] = hooks
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".hooks.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
PY
}

if $DRY; then
  echo "[install] DRY RUN — nothing will be written."
  echo "[install] would publish : $SRC/ -> $DEST/   (rsync -a --delete)"
  echo "[install] would stamp   : $DEST/VERSION = $VERSION"
  echo "[install] settings hooks ($SETTINGS):"
  merge_hooks report
  echo "[install] codex hooks ($CODEX_HOOKS):"
  merge_codex_hooks report
  echo "[install] would run     : skill/bin/install-launch-shim.sh"
  exit 0
fi

# 1) Merge the hooks FIRST — so a malformed settings.json fails closed BEFORE any payload lands
#    (nothing half-installed on a machine whose settings we refuse to touch). flock scopes the
#    read-modify-write against a concurrent writer; the lock releases when the subshell exits.
merge_hooks report >/dev/null
merge_codex_hooks report >/dev/null
mkdir -p "$SETTINGS_DIR"
(
  exec 9>"$SETTINGS.lock"
  flock 9 2>/dev/null || true
  merge_hooks apply
)
mkdir -p "$CODEX_DIR"
(
  exec 9>"$CODEX_HOOKS.lock"
  flock 9 2>/dev/null || true
  merge_codex_hooks apply
)

# 2) Publish the payload. --delete so a file removed from the repo is removed from the install;
#    the payload is already curated, so nothing is excluded (decision B.3 / Task 14).
mkdir -p "$DEST"
rsync -a --delete "$SRC"/ "$DEST"/

# 3) Stamp VERSION into the installed copy (after rsync --delete, which would otherwise remove it).
printf '%s\n' "$VERSION" > "$DEST/VERSION"

# 4) Install the keystroke-free launch shim (idempotent; its own installer guards the ~/.zshrc line).
"$SRC/bin/install-launch-shim.sh"

echo "[install] published skill -> $DEST"
echo "[install] VERSION         -> $VERSION"
echo "[install] hooks + shim registered. Open a NEW cmux tab (or source ~/.zshrc) to load the shim."
