# superlooper

The universal issue-loop workflow skill. Agents write GitHub issues; William approves them by
label; a deterministic loop runner (no standing LLM seat) runs one fresh Claude session per
issue in its own worktree; a mechanical per-PR gate merges to the dev mainline; dev→prod
promotion is William's batched, evidence-backed judgment.

- **Constitution:** `docs/founding/SPEC-2026-07-02-issue-loop-workflow.md`
- **Implementation plan:** `PLAN-2026-07-02-superlooper-v1.md`
- **Project rules:** `CLAUDE.md`

Source lives here. `~/.claude/skills/superlooper` is a published copy, created only by
`bin/install.sh`.
