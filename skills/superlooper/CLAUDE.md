# superlooper — project instructions

Superlooper is the universal issue-loop workflow skill: agents write GitHub issues that
William approves by label; a deterministic loop runner (no standing LLM seat) runs one fresh
Claude session per issue per worktree; a mechanical per-PR gate merges to the dev mainline;
dev→prod promotion is William's batched judgment.

## The constitution

`docs/founding/SPEC-2026-07-02-issue-loop-workflow.md` is the constitution. Its §2 owner
directives are fixed points — never relitigate them, never safety-creep around them.
`docs/founding/EAPP-ANSWERS-2026-07-02.md` carries binding execution pointers.
`PLAN-2026-07-02-superlooper-v1.md` is the implementation plan exec sessions build from.

## Bright lines (enforced by absence — the code for the forbidden thing must not exist)

- **No standing LLM seat, ever.** Three real runs proved every judgment-fired LLM duty
  eventually fails to fire; only mechanical duties fired (see the AUDIT founding doc). The
  runner is deterministic; LLM judgment is hired per-event (fresh session, one question) and
  the runner never waits on judgment to act safely.
- **No LLM in merge mechanics.** Conflicts are never "resolved" by a model into the mainline:
  rebase/merge-if-clean, else regenerate from the issue, else park.
- **No force-push machinery.** Branch updates are merge-based where the repo requires it
  (the eApp always does). Don't build a `--force`/`--force-with-lease` path at all.
- **`agent-ready` is William's word.** No agent applies the approval label without his
  explicit say-so in conversation or a standing rule he himself defined (which must carry its
  own distinct label, e.g. `auto-approved:nightly-red`).
- **Universal skill + per-repo config.** eApp-specific facts (ship.sh, cascade, bright-line
  areas, Render parity) live in the eApp's own adaptation files, NEVER inside this skill.
  If a feature needs a repo-specific fact, that fact enters through the config contract.

## Port discipline

- `~/.claude/skills/autocode/` is FROZEN. Port (copy + adapt) from it; never edit it.
- Every ported file keeps its hard-won comments unless they reference machinery that died
  (orchestrator, rotation, doorbell). Known port fixes are listed in the plan — apply them.
- Proven machinery ports as-is before it gets "improved": launch-delivery verification,
  worker singleton, liveness tiers, safe pane writes, atomic state writes, usage fail-closed.

## Working rules

- Planning sessions run Fable; exec/build sessions run Opus — EXCEPT plan Tasks 8–10, 15,
  and 17, which run Fable (owner ruling 2026-07-02: extra budget spent on the
  highest-judgment work).
- **No headless `claude -p` anywhere in the machinery** (owner billing rule, 2026-07-02:
  print-mode/headless calls may be metered separately by Anthropic in the future). Every
  model invocation — workers AND answerers — runs as a normal interactive session through
  the same launch stack.
- TDD with pytest, same layout as autocode (`lib/` pure core, `tests/` unit tests, shell
  machinery tested via injected stubs — see autocode's `tests/` for the pattern). Pure logic
  lives in `lib/` so it is testable without cmux/GitHub.
- Every review by a fresh agent that did not write the code. This repo has no built-in
  review pipeline, so the duty is explicit (owner directive 2026-07-02): **every exec/build
  session ends its task with a cross-review** — `/superlooper:cross-review` (Codex second
  opinion) by default, a fresh subagent reviewer if Codex is unavailable. P0/P1 findings are
  fixed before the task's final commit. This is a non-regulated project: at most 2 review/fix
  rounds per change, then stop and present William a consolidated decision. (Transition note:
  the machine-local cross-review command may coexist with this namespaced skill until the
  owner retires it — owner decision O3.)
- Never claim done without evidence: run the tests, run the dry-run harness, show output.
- **No test may reach a real external binary** (cmux, osascript, gh, claude) — conftest
  neutralizes the resolution env vars by default (autouse), and a guard test fails if the
  neutralization is ever removed. Ratchet rule from the 2026-07-03 toast-spam incident:
  two tests fired real desktop notifications on every suite run because stubbing was
  opt-in per-test instead of fail-closed global.
- No metered/paid spend (CI, API credits, new GitHub paid features) without William's
  explicit confirmation.
- Commit early and often; this repo is local-only until William decides to publish it.
- **Shared-checkout discipline (incident, 2026-07-02):** while any exec session is live in
  this checkout, other sessions commit by EXPLICIT file path only — never `git add -A`/`-u`
  (an orchestrator's `-A` once swept an exec session's in-progress files into an unrelated
  commit). Parallel exec lanes never share a checkout at all: one worktree per lane.

## The agent boundary (portability rule, owner-requested 2026-07-02)

The loop must stay swappable to a different coding agent (Codex etc.) without untangling.
Everything agent-specific — the launch command line, liveness/hook stamping, the screen
classifier's TUI patterns, trust pre-acceptance, usage/quota reading — lives ONLY in:
`start-session.sh`, the hook scripts + install.sh's hook registration, `pane_state.py`,
`pretrust.sh`, `usage.py`. No other file may reference Claude Code specifics; the runner,
gate, contracts, and GitHub protocol stay agent-agnostic.

## Publishing

Source NEVER lives in `~/.claude`. Installing to `~/.claude/skills/` is an explicit publish
step via `bin/install.sh` (deliberate copy, version-stamped) — never a symlink, so
half-finished edits in this repo can't leak into live sessions or a running loop.
