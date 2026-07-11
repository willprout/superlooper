# Superlooper Machine Stack

Superlooper has two kinds of prerequisites:

- Repo state, checked by `superlooper doctor --repo /path/to/repo`.
- Machine state, checked by `superlooper doctor --stack --repo /path/to/repo`.

`doctor --stack` is read-only but for ONE deliberate, announced side effect: it sends a single
test notification through the configured `notify` channel to prove it can actually deliver (see
the `notify channel` block below). It does not install, repair, source, log in, write config,
create tabs, or spend model calls. It prints one status line per machine block and exits nonzero
only when a block **FAILs**. A block may also be a **WARN** — an advisory that does not fail the
stack, used for a tool that is only conditionally needed on this machine (a missing Codex CLI on a
Claude-only machine is the standing example; see `codex CLI` below).

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

Run:

```bash
superlooper doctor --stack --repo /path/to/repo
```

Fix every `FAIL` line before starting `superlooper run`.

## Tier 2: Orchestrator

An orchestrator additionally needs the tools used by the gate and by worker handoff:

- `codex CLI` - Needed only when this machine actually runs Codex: a repo whose config sets
  `agent: codex`, so worker sessions launch through Codex. `/cross-review` (a Codex second opinion)
  is the *default* fresh-agent review, but an independent same-model fresh subagent is an equally
  valid review path (owner ruling 2026-07-10), so a Claude-only machine satisfies the fresh-agent
  review duty without Codex and can reach an all-green stack. The stack doctor therefore reports a
  missing or unauthenticated Codex as a **WARN** on a Claude-only machine (stack still PASSes); it
  is a hard **FAIL** only when a repo's config selects `agent: codex`.
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
