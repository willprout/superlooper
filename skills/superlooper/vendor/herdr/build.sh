#!/usr/bin/env bash
# Build the pinned + fenced session host, and install it into the fleet prefix (issue #309).
#
# This is the executable form of the "Build" section of the README beside it. It exists because
# the mini build-up must be REPRODUCIBLE rather than artisanal: the four commands in that section
# are easy to run slightly differently at a version bump, and the difference between a fenced
# binary and a stock one is invisible from the runner's seat — an unfenced socket answers.
#
# It drives no verb and speaks no protocol. It clones a tag, applies the patch next to it, builds,
# and copies one file. Everything that TALKS to the host lives behind the five-verb doorway
# (skill/lib/session_host.py), and nothing here reaches it.
#
# Deliberately NOT part of `superlooper fleet --install`: this needs the SOURCE checkout (the
# patch is a build input and never ships inside the published payload) and a Rust toolchain, and
# it takes minutes. `fleet --install` configures a host that already exists and names this script
# when one does not — the same split as publishing, where the gated installer is its own act.
#
# Usage:
#   vendor/herdr/build.sh [--prefix DIR] [--work DIR] [--force]
#
#   --prefix DIR   where the built binary is installed (default: $SL_HOME/fleet, SL_HOME
#                  defaulting to ~/.superlooper). The binary lands in <prefix>/bin/.
#   --work DIR     where the upstream tree is cloned and built (default: <prefix>/src).
#   --force        rebuild even when the installed binary already reports the pinned version.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="$(basename "$HERE")"          # the host's own name — this directory is named for it
LIB="$(cd "$HERE/../../skill/lib" && pwd)"
PATCH="$HERE/0001-fence-token-auth-on-the-control-socket.patch"
UPSTREAM="https://github.com/herdrdev/${NAME}"

PREFIX="${SL_HOME:-$HOME/.superlooper}/fleet"
WORK=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --work)   WORK="${2:?--work needs a directory}"; shift 2 ;;
    --force)  FORCE=1; shift ;;
    -h|--help) sed -n '1,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "build: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$WORK" ] || WORK="$PREFIX/src"

# The pin has ONE machine-readable home (skill/lib/herdr_hook.py). Reading it here rather than
# spelling the version again is what keeps a version bump from leaving two disagreeing pins on
# disk — the failure mode being a build that is a release behind the checksum it is verified
# against, which looks perfectly healthy.
VERSION="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import herdr_hook; print(herdr_hook.PINNED_VERSION)' "$LIB")"
[ -n "$VERSION" ] || { echo "build: could not read the pinned version from $LIB" >&2; exit 1; }

DEST="$PREFIX/bin/$NAME"

say() { printf '  %s\n' "$*"; }

echo "build: pinned v$VERSION -> $DEST"

if [ ! -f "$PATCH" ]; then
  echo "build: the fence patch is missing at $PATCH — it is a build input carried in this" >&2
  echo "       directory, and a host built without it has NO fence while still answering." >&2
  exit 1
fi

# Already there? An idempotent build-up is the whole point: the operator re-runs this after a
# reboot, after a bump, or because they are not sure, and it must be cheap and honest about it.
if [ "$FORCE" -eq 0 ] && [ -x "$DEST" ]; then
  have="$("$DEST" --version 2>/dev/null | tr -d '\r' | awk '{print $NF}')" || have=""
  if [ "$have" = "$VERSION" ]; then
    say "already installed and reporting v$VERSION — nothing to do (use --force to rebuild)"
    say "the fence is NOT proven by this check: run \`superlooper fleet\` for that"
    exit 0
  fi
  say "installed binary reports '${have:-unreadable}', wanted '$VERSION' — rebuilding"
fi

RUSTUP="$(command -v rustup || true)"
if [ -z "$RUSTUP" ]; then
  echo "build: no rustup on PATH. Install it (brew install rustup) — the tree pins its own" >&2
  echo "       toolchain in rust-toolchain.toml and rustup is what honours that pin." >&2
  exit 1
fi

mkdir -p "$WORK" "$PREFIX/bin"

if [ ! -d "$WORK/.git" ]; then
  say "cloning $UPSTREAM at v$VERSION"
  git clone --depth 1 --branch "v$VERSION" "$UPSTREAM" "$WORK"
else
  say "reusing the checkout at $WORK"
  git -C "$WORK" fetch --depth 1 origin "refs/tags/v$VERSION:refs/tags/v$VERSION" 2>/dev/null || true
  git -C "$WORK" checkout -q --force "v$VERSION"
  git -C "$WORK" reset -q --hard "v$VERSION"
  git -C "$WORK" clean -qfd -e target
fi

say "applying the fence patch"
# --3way so an upstream refactor produces a conflict to READ rather than a flat refusal. A hunk
# that will not apply is the version-bump procedure's step 1, not a reason to build without it.
git -C "$WORK" apply --3way "$PATCH"

# The two invariants the README says a bump must re-read, asserted here so a silently-relocated
# hunk cannot produce a green build with no fence. Neither is a substitute for the negative test
# (tests/test_fence_token_auth.py) — they are the cheap check that runs every time.
grep -q "pub mod auth;" "$WORK/src/api/mod.rs" \
  || { echo "build: the patch applied but src/api/mod.rs does not declare the auth module" >&2; exit 1; }
grep -q "env_remove" "$WORK/src/pane.rs" \
  || { echo "build: the patch applied but src/pane.rs carries no token scrub — a server holding" >&2
       echo "       the token would hand it to every pane it spawns, workers included." >&2; exit 1; }

CHANNEL="$(awk -F'"' '/^channel/ {print $2}' "$WORK/rust-toolchain.toml" 2>/dev/null || true)"
[ -n "$CHANNEL" ] || CHANNEL="stable"
say "building with rust $CHANNEL (this takes minutes)"
"$RUSTUP" toolchain install "$CHANNEL" --profile minimal >/dev/null 2>&1 || true
# `rustup run` is deliberately NOT used: Homebrew's rustup formula installs no ~/.cargo/bin shims,
# and `rustup run` puts that empty directory on PATH — so cargo starts and then dies with
# "could not execute process `rustc -vV`", which reads like a broken toolchain rather than a
# missing shim. Resolving the toolchain's own bin directory and putting THAT on PATH works on
# both a Homebrew rustup and a rustup-init one.
CARGO="$("$RUSTUP" which --toolchain "$CHANNEL" cargo 2>/dev/null || true)"
[ -x "$CARGO" ] || { echo "build: rustup has no cargo for toolchain $CHANNEL" >&2; exit 1; }
( cd "$WORK" && PATH="$(dirname "$CARGO"):$PATH" "$CARGO" build --release )

BUILT="$WORK/target/release/$NAME"
[ -x "$BUILT" ] || { echo "build: cargo reported success but $BUILT is not there" >&2; exit 1; }

install -m 0755 "$BUILT" "$DEST"
got="$("$DEST" --version 2>/dev/null | tr -d '\r' | awk '{print $NF}')" || got=""
if [ "$got" != "$VERSION" ]; then
  echo "build: the installed binary reports '${got:-unreadable}', not the pinned '$VERSION'" >&2
  exit 1
fi

say "installed $DEST (v$VERSION, fenced)"
say "next: superlooper fleet --install   # configure it, then judge it with: superlooper fleet"
