#!/usr/bin/env bash
# bin/install.sh — the explicit publish step for the superlooper monorepo (plan Task 14,
# locked decision B.3), extended with the engine-diff publish gate (MIGRATION-2026-07-08).
#
# A deliberate "publish button": copy the finished skill payload from THIS source repo
# (skills/superlooper/skill) into the live ~/.claude/skills/superlooper/, register the two
# activity hooks, and install the launch shim. NEVER a symlink — a symlink would leak
# half-finished edits into live sessions and a running loop. Run it by hand when you want the
# installed copy to catch up with this repo; dev churn here never touches ~/.claude until you do.
#
# THE ENGINE-DIFF GATE (why publish is the human checkpoint): the running loop executes the
# INSTALLED copy, not this repo, so a bad engine change merged to `main` is INERT until someone
# republishes. That makes publish — not a live guard — the trustworthy place to catch an unwanted
# engine change. Before touching anything, this script shows exactly which payload files changed
# since the last publish (the source commit is recorded in $DEST/VERSION) and refuses to proceed
# without an explicit OK: an interactive [y/N], or --yes once William has reviewed the list. This
# is the fence that makes `skills/**` a bright line trustworthy: the engine is supervised-only, and
# no engine change reaches a live loop without a human saying so here.
#
# THE ONLY PUBLISH PATH: this repo-root bin/install.sh is the one door into ~/.claude/skills, and
# nothing else in the repo writes there (issue #197). The engine's standalone-era nested installer
# at skills/superlooper/bin/install.sh once published the same payload to the same location with no
# gate; it is now a tombstone that refuses and points here. Two mechanical guards keep it that way:
# skills/superlooper/tests/test_install.py drives the tombstone, and
# skills/superlooper/tests/test_one_publish_door.py fails if ANY script but this one names — or
# writes to — the installed-skill home. That fence also pins the SHAPE of this script's own gate
# (engine diff, explicit OK, the TTY test, consent defaulting to false) so the pieces cannot be
# deleted quietly. It reads for those strings; it does not execute the gate, so it is a tripwire
# against removal, not a proof the gate still behaves.
#
# Idempotent: re-running re-syncs the payload, never duplicates a hook or the shim block, and leaves
# an unchanged settings.json byte-for-byte. --dry-run prints what WOULD change (including the gate
# assessment) and writes nothing.
#
# The settings.json merge is python stdlib json, NOT jq (cross-review M4): jq stays only inside the
# ported pretrust.sh, which `doctor` checks for. This script reuses pretrust.sh's disciplines: an
# atomic tmp+rename (a reader always sees the whole old OR whole new file — never a partial write)
# guarantees no corruption, and a best-effort flock serializes concurrent superlooper installs so
# they don't lose each other's edit. A wrong-typed existing settings.json fails closed, never
# clobbered.
set -euo pipefail

DRY=false
ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    -y|--yes) ASSUME_YES=true ;;
    -h|--help) echo "usage: install.sh [--dry-run] [--yes]"; exit 0 ;;
    *) echo "install: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
SRC="$REPO_ROOT/skills/superlooper/skill"                # the publishable payload (nested: the
                                                         # superlooper skill lives under skills/)
DEST="$HOME/.claude/skills/superlooper"                  # the live installed copy
SETTINGS_DIR="$HOME/.claude"
SETTINGS="$SETTINGS_DIR/settings.json"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_HOOKS="$CODEX_DIR/hooks.json"
PAYLOAD_REL="skills/superlooper/skill"                   # repo-relative, for the diff gate

# The two hooks, registered EXACTLY as autocode registers its own (decision B.3): $HOME is left
# LITERAL — Claude Code expands it when it fires the hook, so the entry is portable across HOMEs.
# Both hooks are strict no-ops in any session that isn't a superlooper worker (they exit early
# unless SL_ISSUE_ID + SL_RUN_ROOT are exported), so registering them globally is safe.
ACT_CMD='$HOME/.claude/skills/superlooper/bin/activity-hook.sh'
STOP_CMD='$HOME/.claude/skills/superlooper/bin/stop-hook.sh'
# PreToolUse deny hook (issue #156). CLAUDE ONLY — registered in settings.json below but NOT in the
# Codex hooks.json (Codex has no PreToolUse event — spike verdict), so it lives on the Claude side of
# merge_hooks alone. A strict no-op outside a worker session, like the other two.
DENY_CMD='$HOME/.claude/skills/superlooper/bin/pretooluse-hook.sh'

[ -d "$SRC" ] || { echo "install: payload not found at $SRC" >&2; exit 1; }

# THE OPERATIONAL DOCS (issue #199, defect class D12). Beyond the payload, this installer also
# mirrors the operator-facing docs — STACK.md, runner-ops, the approval protocol and the whole
# sl-debugger playbook — into $DEST/docs/ops. D12's third root cause was "the debugger playbook
# wasn't installed on the machine having the incident": since the plugin restructure those docs
# travel as optional plugin CONTENT, so a machine can run the loop with no playbook at 3am, and the
# watchdog's unattended brief points a fresh session straight at a reference that isn't there.
# Mirroring them here makes the ONE gated publisher put them on the machine, and
# `superlooper doctor --stack`'s `installed ops docs` block FAILs when they are absent or stale.
#
# The table of what ships lives in the payload's lib/ops_docs.py, not here, so the installer and
# the doctor read the same source of truth. $OPS_DOC_PATHS carries the source paths into the
# engine-diff gate below: a doc that lands on the machine must be shown to the human first, exactly
# like every other published file.
#
# PARSED, NOT IMPORTED. This runs BEFORE the gate, so it must not execute a single line of the
# payload the human has not yet approved — running `ops_docs.py --list` would execute its module
# body, and until this gate says yes that file is unreviewed engine code. So we read OPS_DOCS out
# of the source with `ast`, which evaluates nothing. (The publish call at step 6 does run the
# module, and by then the human has said yes.) The gate's own promise — nothing on this MACHINE is
# touched before the OK — was never in question here; this is about not executing the diff's
# subject while deciding whether to accept the diff.
#
# `|| OPS_DOC_PATHS=""` is load-bearing: under `set -e` a bare assignment whose command
# substitution fails aborts the script on the spot, and the refusal below — written for exactly
# that case — would never print. stderr is deliberately NOT swallowed, so python's own reason
# (syntax error, missing file, no python3) reaches the operator before this script gives up.
OPS_DOCS_PY="$REPO_ROOT/$PAYLOAD_REL/lib/ops_docs.py"
OPS_DOC_PATHS="$(python3 -c '
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "OPS_DOCS" for t in node.targets):
        for pair in node.value.elts:
            print(ast.literal_eval(pair).__getitem__(0))
        sys.exit(0)
sys.exit("ops_docs.py defines no OPS_DOCS table")
' "$OPS_DOCS_PY" | tr '\n' ' ')" || OPS_DOC_PATHS=""
if [ -z "${OPS_DOC_PATHS// }" ]; then
  echo "install: could not read the ops-doc list from $OPS_DOCS_PY — refusing to publish a" >&2
  echo "install: machine whose gate would not show the docs it is about to install." >&2
  exit 1
fi

# VERSION stamp: git SHA of THIS source repo + the install date. `nogit` if the tree has no git
# (published tarball); the date always lands so a VERSION is never empty.
SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
DATE="$(date +%Y-%m-%d)"                                 # Mac-local, matching the loop's clock convention
VERSION="$SHA $DATE"

# --- the engine-diff publish gate. mode=report only prints the assessment (dry-run); mode=gate
#     prints AND, when the payload changed since last publish, requires an explicit OK. Returns 0 to
#     proceed, 3 to abort. Reads the last-published source commit from $DEST/VERSION (first token)
#     and diffs it against HEAD, scoped to the payload path. Fails SAFE: if there is no in-history
#     baseline (first publish, or a VERSION from an unrelated history), the whole payload counts as
#     new and still needs an OK. ---
engine_gate() {  # engine_gate <report|gate>
  local mode="$1"
  local last_sha="" baseline_ok=false changed=""

  if [ -f "$DEST/VERSION" ]; then
    last_sha="$(awk 'NR==1{print $1}' "$DEST/VERSION" 2>/dev/null || true)"
  fi
  if [ -n "$last_sha" ] && [ "$last_sha" != "nogit" ] \
     && git -C "$REPO_ROOT" cat-file -e "${last_sha}^{commit}" 2>/dev/null; then
    baseline_ok=true
  fi

  # The gate's scope is the payload PLUS the ops-doc sources (issue #199): both land on the machine
  # when this script runs, so both have to be in the list the human says yes to. Unquoted on
  # purpose — $OPS_DOC_PATHS is a space-separated list of repo paths and must word-split into
  # separate pathspecs.
  echo "[install] engine-diff gate (payload: $PAYLOAD_REL + ops docs)"
  if ! $baseline_ok; then
    echo "  no in-history baseline (first publish on this machine, or the last-published commit is"
    echo "  not in this repo's history) — treating the ENTIRE payload as new:"
    # shellcheck disable=SC2086
    git -C "$REPO_ROOT" ls-files -- "$PAYLOAD_REL" $OPS_DOC_PATHS 2>/dev/null | sed 's/^/    A       /' || true
  else
    # Capture the diff's exit status explicitly (set -e is disabled inside this function, invoked
    # as `engine_gate gate || exit`). A git ERROR must NOT be mistaken for "no changes" and waved
    # through — that would be the one fail-OPEN branch in an otherwise fail-safe gate. On error we
    # fall through to requiring an explicit OK, exactly as if the payload had changed.
    # shellcheck disable=SC2086
    if changed="$(git -C "$REPO_ROOT" diff --name-status "$last_sha" HEAD -- "$PAYLOAD_REL" $OPS_DOC_PATHS 2>/dev/null)"; then
      if [ -z "$changed" ]; then
        echo "  no engine changes since last publish ($last_sha) — payload and ops docs unchanged."
        return 0
      fi
      echo "  engine files changed since last publish ($last_sha):"
      printf '%s\n' "$changed" | sed 's/^/    /'
    else
      echo "  WARNING: could not compute the engine diff against $last_sha — refusing to assume"
      echo "  'unchanged'. Treating the payload as changed; an explicit OK is required below."
    fi
  fi

  if [ "$mode" = report ]; then
    echo "  (dry-run: not prompting; nothing will be published)"
    return 0
  fi
  if $ASSUME_YES; then
    echo "  --yes: engine changes accepted on the command line."
    return 0
  fi
  if [ ! -t 0 ]; then
    echo "  REFUSING: engine changes need an explicit OK, but stdin is not a TTY." >&2
    echo "  Re-run interactively, or pass --yes once you have reviewed the list above." >&2
    return 3
  fi
  printf "  Publish these engine changes to %s? [y/N] " "$DEST"
  local reply=""
  read -r reply || true
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) echo "  aborted — nothing published."; return 3 ;;
  esac
}

# --- the settings.json hook-merge, as a stdlib-json helper (no jq). mode=apply writes atomically;
#     mode=report only prints. Fails CLOSED (exit 2, file untouched) on a wrong-typed settings.json
#     — a malformed hooks section must never be silently overwritten (the fail-open-on-wrong-typed
#     defect class this project has hit twice). ---
merge_hooks() {  # merge_hooks <mode:apply|report>
  local mode="$1"
  python3 - "$SETTINGS" "$mode" "$ACT_CMD" "$STOP_CMD" "$DENY_CMD" <<'PY'
import json, os, sys, tempfile

path, mode, act_cmd, stop_cmd, deny_cmd = sys.argv[1:6]
# (event, command). PreToolUse is Claude-only (Codex has no such event — spike verdict); it appears
# here but NOT in merge_codex_hooks' targets.
targets = [("PostToolUse", act_cmd), ("Stop", stop_cmd), ("PreToolUse", deny_cmd)]

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
  engine_gate report
  echo "[install] would publish : $SRC/ -> $DEST/   (rsync -a --delete)"
  echo "[install] would stamp   : $DEST/VERSION = $VERSION"
  echo "[install] would mirror  : ops docs -> $DEST/docs/ops/ (stamped $VERSION)"
  # shellcheck disable=SC2086
  printf '%s\n' $OPS_DOC_PATHS | sed 's/^/    /'
  echo "[install] settings hooks ($SETTINGS):"
  merge_hooks report
  echo "[install] codex hooks ($CODEX_HOOKS):"
  merge_codex_hooks report
  echo "[install] would run     : skills/superlooper/skill/bin/install-launch-shim.sh"
  echo "[install] would run     : skills/superlooper/skill/bin/install-cli-link.sh (superlooper -> PATH)"
  exit 0
fi

# 0) The human checkpoint FIRST — nothing is touched (not even settings.json) unless the engine
#    diff since the last publish is accepted. A declined gate exits non-zero with the machine
#    entirely unchanged.
engine_gate gate || exit $?

# 1) Merge the hooks — so a malformed settings.json fails closed BEFORE any payload lands
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

# 5) Put a stable `superlooper` command on PATH, pointing at the installed copy (issue #31). Every
#    doc invokes the CLI bare (`superlooper adopt/doctor/run`); without this the first documented
#    command is "command not found". The linker writes a thin shim into a standard user bin dir and,
#    if that dir is not on PATH, prints the exact line to add it — it never silently skips. Idempotent.
"$SRC/bin/install-cli-link.sh"

# 6) Mirror the operational docs into $DEST/docs/ops (issue #199). AFTER the rsync, for the same
#    reason the VERSION stamp is: --delete would otherwise sweep them straight back out — and LAST,
#    after the shim and the PATH link, deliberately. Under `set -e` a failure here aborts the run,
#    and doing it earlier would leave a first-time install with an engine but no launch shim and no
#    `superlooper` on PATH. Note what a failure HERE leaves: step 2's `rsync --delete` has already
#    swept the previous mirror, so an aborted publish leaves the machine with NO ops docs rather
#    than the older ones — which `doctor --stack` then FAILs on, loudly, by design. The helper
#    rebuilds the mirror whole, so a doc retired upstream does not linger as a page an operator can
#    still find and act on, and it fails loud (non-zero) rather than publishing a partial playbook.
python3 "$OPS_DOCS_PY" --publish --repo-root "$REPO_ROOT" --dest "$DEST" --version "$VERSION" \
  | sed 's/^/[install] ops doc      -> /'

echo "[install] published skill -> $DEST"
echo "[install] VERSION         -> $VERSION"
echo "[install] hooks + shim registered. Open a NEW cmux tab (or source ~/.zshrc) to load the shim."
echo "[install] superlooper CLI linked onto PATH (see the [install-cli-link] lines above)."
