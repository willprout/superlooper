# Superlooper Machine Stack

Superlooper has two kinds of prerequisites:

- Repo state, checked by `superlooper doctor --repo /path/to/repo`.
- Machine state, checked by `superlooper doctor --stack --repo /path/to/repo`.

`doctor --stack` is read-only. It does not install, repair, source, log in, write config, create
tabs, or spend model calls. It prints one pass/fail line for each machine block and exits nonzero
when any block fails.

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
- `notify command configured` - the adopted repo must set `notify.cmd` or `notify.imessage_to` in
  `.superlooper/config.json`. Desktop cmux toasts are only a local fallback and are not enough for
  unattended overnight operation.
- `launch shim sourced` - `~/.superlooper/launch-shim.zsh` must be installed and sourced from
  `.zshrc`, so new cmux tabs self-run the dropped worker command without keystrokes.

Run:

```bash
superlooper doctor --stack --repo /path/to/repo
```

Fix every `FAIL` line before starting `superlooper run`.

## Tier 2: Orchestrator

An orchestrator additionally needs the tools used by the gate and by worker handoff:

- `codex CLI` - Codex CLI must be present and authenticated. The default fresh-agent review path
  depends on it.
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

- `codex CLI`: install Codex CLI and run `codex login`.
- `cmux present`: install cmux or set `SL_CMUX` to the runner's cmux binary.
- `claude login`: run `claude auth login` with the subscription account.
- `gh auth`: run `gh auth login --hostname github.com`.
- `gh API headroom`: wait for the hourly quota reset or switch `gh auth` to an account with enough
  core requests remaining.
- `notify command configured`: set `notify.cmd` or `notify.imessage_to` in
  `.superlooper/config.json`.
- `launch shim sourced`: run `skills/superlooper/skill/bin/install-launch-shim.sh`, then open a new
  cmux tab or source `.zshrc`.
