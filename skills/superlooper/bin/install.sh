#!/bin/bash
# Tombstone for the standalone-era nested installer.
#
# The monorepo root bin/install.sh is the only canonical publish path because it shows the
# engine diff and requires an explicit OK before writing to the installed skill home.
set -euo pipefail

printf '%s\n' \
  "install: refusing to publish from skills/superlooper/bin/install.sh." \
  "install: canonical publish path is the gated repo-root bin/install.sh." \
  "install: run ./bin/install.sh from the monorepo root after reviewing the gate." \
  "install: nothing was published." >&2
exit 1
