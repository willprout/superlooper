#!/usr/bin/env bash
# Pre-trust a folder for Codex so the interactive session does not stop at the directory trust
# prompt. Codex stores project trust in $CODEX_HOME/config.toml (default: ~/.codex/config.toml):
#   [projects."/abs/worktree"]
#   trust_level = "trusted"
set -euo pipefail
DIR_IN="${1:?usage: pretrust-codex.sh <abs-folder>}"
DIR="$(cd "$DIR_IN" 2>/dev/null && pwd -P || echo "$DIR_IN")"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CONF="$CODEX_DIR/config.toml"
mkdir -p "$CODEX_DIR"
[ -f "$CONF" ] || : > "$CONF"

LOCK="$CONF.lock"
exec 9>"$LOCK"
flock 9 2>/dev/null || true

tmp="$(mktemp "${CONF}.XXXXXX")"
python3 - "$CONF" "$DIR" > "$tmp" <<'PY'
import re
import sys

path, project = sys.argv[1], sys.argv[2]
try:
    text = open(path, encoding="utf-8").read()
except FileNotFoundError:
    text = ""

def toml_basic(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

header = "[projects.%s]" % toml_basic(project)
lines = text.splitlines()
out = []
i = 0
done = False
table_re = re.compile(r"^\s*\[.*\]\s*$")

while i < len(lines):
    if lines[i].strip() == header:
        out.append(lines[i])
        i += 1
        section = []
        while i < len(lines) and not table_re.match(lines[i]):
            section.append(lines[i])
            i += 1
        replaced = False
        for line in section:
            if re.match(r"^\s*trust_level\s*=", line):
                out.append('trust_level = "trusted"')
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append('trust_level = "trusted"')
        done = True
        continue
    out.append(lines[i])
    i += 1

if not done:
    if out and out[-1].strip():
        out.append("")
    out.append(header)
    out.append('trust_level = "trusted"')

sys.stdout.write("\n".join(out) + "\n")
PY
mv "$tmp" "$CONF"
echo "[pretrust] codex trusted $DIR"
