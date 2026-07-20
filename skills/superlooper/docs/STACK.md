# Superlooper Machine Stack

Superlooper has two kinds of prerequisites:

- Repo state, checked by `superlooper doctor --repo /path/to/repo`.
- Machine state, checked by `superlooper doctor --stack --repo /path/to/repo`.

Both are invoked through the `superlooper` command itself, which the gated `bin/install.sh` links
onto your PATH when it publishes the skill (a thin shim pointing at the installed copy; if no
standard bin dir is on your PATH the installer prints the exact `export PATH="…"` line to add). See
`ADOPTING.md` → "Getting the `superlooper` command".

`doctor --stack` is read-only but for ONE deliberate, announced side effect: it sends a single
test notification through the configured `notify` channel to prove it can actually deliver (see
the `notify channel` block below). It does not install, repair, source, log in, write config,
create tabs, or spend model calls. It prints one status line per machine block and exits nonzero
only when a block **FAILs**. A block may also be a **WARN** — an advisory that does not fail the
stack. A WARN is used for something that is only conditionally needed on this machine (a missing
Codex CLI on a Claude-only machine; see `codex CLI` below), for something that costs session
*quality* rather than correctness (a missing `superlooper plugin`), for a state the doctor
could not actually read (it never fails the stack on a fact it could not determine), and for a
by-design state worth seeing but never worth failing on (an `installed engine current` that is
behind, since publishing is deliberately manual). A WARN carries
its whole story on its own line — only a FAIL prints a separate `Fix:` line.

## Tier 1: Loop User

A loop user needs enough local stack for a worker session to launch, work, report, and notify:

- `cmux present` - cmux must be installed, and the runner must be started from a visible cmux tab in
  the same workspace as the target pane. The run command's pane preflight checks the same-workspace
  rule before the loop starts.
- `claude login` - Claude Code must be logged in through the `claude.ai` subscription account used
  for workers, not only through an API key.
- `gh auth` - GitHub CLI must be authenticated to `github.com` as the account that owns the loop
  repo and can read issues, PRs, labels, checks, and rate limits.
- `gh API headroom` - the active GitHub token needs hourly core API quota left. The stack doctor
  fails below the local safety floor so quota exhaustion is visible before the runner stalls.
- `notify channel` - the adopted repo must set `notify.cmd` or `notify.imessage_to` in
  `.superlooper/config.json`, AND that channel must actually deliver. The doctor announces and then
  sends one real test message through the configured channel: a delivered send PASSes; a nonzero
  send FAILs the block with the command's return code and the tail of its stderr (the actual
  reason), so a channel that is set but broken — the live incident where a missing recipient file
  made every send exit 2 and a park alert never arrived — is caught here instead of overnight.
  Desktop cmux toasts are only a local fallback and are not enough for unattended overnight
  operation, so a configured-cmux-only setup still FAILs.
- `launch shim sourced` - `~/.superlooper/launch-shim.zsh` must be installed and sourced from
  `.zshrc`, so new cmux tabs self-run the dropped worker command without keystrokes.
- `superlooper plugin` - the `superlooper@superlooper` plugin should be installed and enabled, so
  planning and worker sessions on this machine load the superlooper ops, write-issue and debugger
  skills. This is a **WARN**, never a FAIL: the runner does not depend on the skills being
  installed — every brief it writes is self-contained, so a machine without the plugin still runs
  the loop correctly, just with less capable sessions. The doctor reads the state from
  `claude plugin list --json` (the documented CLI); if it cannot read it, it says so and still
  passes.

Run:

```bash
superlooper doctor --stack --repo /path/to/repo
```

Fix every `FAIL` line before starting `superlooper run`.

## Tier 2: Orchestrator

An orchestrator additionally needs the tools used by the gate and by worker handoff:

- `codex CLI` - Needed only when this machine actually runs Codex: a repo whose config sets
  `agent: codex`, so worker sessions launch through Codex. `/superlooper:cross-review` (a Codex
  second opinion) is the *default* fresh-agent review, but an independent same-model fresh subagent
  is an equally valid review path (owner ruling 2026-07-10), so a Claude-only machine satisfies the
  fresh-agent review duty without Codex and can reach an all-green stack. The stack doctor therefore
  reports a missing or unauthenticated Codex as a **WARN** on a Claude-only machine (stack still
  PASSes); it is a hard **FAIL** only when a repo's config selects `agent: codex`. (Transition note:
  the machine-local cross-review command may coexist with this namespaced skill until the owner
  retires it — owner decision O3.)
- Repo-level doctor green - `superlooper doctor --repo /path/to/repo` must pass for config,
  `required_checks`, labels, hooks, jq, and the repo adoption contract.
- Same-workspace launch discipline - start `superlooper run` in the visible cmux tab that owns the
  target pane, or pass an explicit pane that resolves from the runner's workspace.
- GitHub quota discipline - one `gh` login shares one hourly API budget across the dashboard,
  manual commands, and every running loop. Treat low headroom as a machine-level block, not as a
  repo bug.
- Publish discipline - merged engine changes are inert until republished through the gated root
  installer. The stack doctor diagnoses only the current machine; it never republishes.

## Check Names And Fixes

`doctor --stack` emits these exact block names:

- `codex CLI`: install the Codex CLI and run `codex login` — but only required when a repo's config
  sets `agent: codex`. On a Claude-only machine a missing or unauthenticated Codex is a WARN and the
  stack still passes, because a fresh same-model subagent is a valid review path; install it only if
  you switch a repo to `--agent codex`.
- `cmux present`: install cmux or set `SL_CMUX` to the runner's cmux binary.
- `claude login`: run `claude auth login` with the subscription account.
- `gh auth`: run `gh auth login --hostname github.com`.
- `gh API headroom`: wait for the hourly quota reset or switch `gh auth` to an account with enough
  core requests remaining.
- `notify channel`: set `notify.cmd` or `notify.imessage_to` in `.superlooper/config.json`, and
  make sure a send works — the doctor sends one real test message and FAILs on a nonzero send,
  printing the return code and stderr tail. For `notify.cmd`, run the command yourself with
  `SL_TITLE`/`SL_BODY` set and confirm it exits 0; for `notify.imessage_to`, confirm Messages.app
  is signed in, the recipient is valid, and the one-time macOS permission click is granted.
- `launch shim sourced`: run `skills/superlooper/skill/bin/install-launch-shim.sh`, then open a new
  cmux tab or source `.zshrc`.
- `cmux App Nap disabled`: run `defaults write com.cmuxterm.app NSAppSleepDisabled -bool true` (or
  re-run `install-launch-shim.sh`, which sets it), then FULLY QUIT and relaunch cmux — AppKit reads
  the flag only at app launch, so a cmux that is already running stays App-Nap-eligible until you
  restart it. FAILs when the default is absent or explicitly false: that is a machine where an idle,
  occluded cmux gets napped and defers spawning worker-tab shells past the 30s launch verify window
  — the systemic "LAUNCH NOT DELIVERED" failure that starts ~40 minutes after you walk away
  (issue #120). A state the doctor could not pin down is a WARN, never a FAIL: no `defaults` binary,
  a read that errored, or a read that came back with a value that is neither true nor false (verify
  that one by hand). Override the checked bundle id with `SL_CMUX_BUNDLE_ID`.
- `runner anchor (live)`: mostly a state line, not a chore — it fires only when a runner for this
  repo is actually live, and re-runs the read-only pane probe the startup preflight uses against the
  anchor that runner recorded (issue #33). No live runner, a stale pidfile, or an unreadable config
  print as a clean skip; a live runner that recorded no matching anchor is a WARN (an older runner,
  or one started before anchors shipped) — restart it from a visible cmux tab to record one. It
  FAILs, with a `Fix:` line, only when a live runner's recorded pane no longer resolves in the
  workspace it launched in — its cmux tab was closed or dragged to another window, so every worker
  launch would be born in a dead pane and the queue parks. The fix there is to stop the runner, open
  a tab in the INTENDED cmux window, and re-run `superlooper run` (see
  `plugin/skills/superlooper/references/runner-ops.md` → Restarting the runner).
- `installed engine current`: a visibility line that never fails the stack — being behind is by
  design, since a merged engine change is inert until someone republishes through the gated
  `bin/install.sh` (issue #39). It compares the installed copy's VERSION stamp
  (`~/.claude/skills/superlooper/VERSION`) against the engine payload in a superlooper source
  checkout, at the first ref that resolves there: `origin/<dev_branch>`, else the local
  `<dev_branch>`, else `HEAD` — the line names which one it measured, so a checkout with a stale
  `origin` says so. In sync prints a plain ok; N commits behind is a WARN saying so —
  republish through `bin/install.sh` when you want those changes live. Nothing to compare (no stamp,
  a `nogit` stamp, or no source checkout on this machine — the normal case on a machine that only
  *runs* the loop) is also a plain ok. A WARN also covers the anomalies: a stamped commit that is
  not in this checkout's history (rebased or unrelated — republish to re-stamp), or git failing to
  compute the distance. Point it at a checkout elsewhere with `SL_SOURCE_REPO`.
- `superlooper plugin`: install it with `claude plugin marketplace add willprout/superlooper` then
  `claude plugin install superlooper@superlooper --scope user`; if it is installed but disabled, run
  `claude plugin enable superlooper@superlooper`. Always a WARN — the loop runs correctly without
  it, so it never fails the stack. Override the checked plugin id with `SL_PLUGIN_ID` (for a fork or
  a differently-named marketplace).
