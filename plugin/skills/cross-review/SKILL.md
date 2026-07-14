---
name: cross-review
description: Get an independent second-opinion review of a file (plan, spec, code change, anything) from a different model family via the Codex CLI — or, on a Claude-only machine, from a fresh same-model subagent that wrote none of the code. Use when finishing a high-stakes artifact (plans, designs, refactors) or to satisfy the loop's fresh-agent review duty before a final commit; not for trivial edits. Invoked as /superlooper:cross-review <file> [focus hint].
---

# cross-review — second-opinion review

Get an independent review of a file (plan, spec, code change, anything) from a reviewer that
did **not** write it. The default path is a different model family via the Codex CLI — useful
for catching blind spots that same-family review misses: race conditions in async code, silent
behavior changes from edits, brittle test assumptions. On a Claude-only machine the review runs
through a fresh same-model subagent instead (see **Fallback**, below) — an equally valid path,
not a degraded one.

**Cost note:** the Codex path makes one round-trip to Codex per invocation (~30s–2min and
counts against your subscription). Use it for high-stakes artifacts (plans, designs, refactors),
not trivial edits. The fresh-subagent path spends one subagent turn instead.

## Arguments

`$ARGUMENTS` — the path to the file to review, optionally followed by a focus hint. If no focus
hint is given, default to "correctness, missing tests, ordering / sequencing bugs, brittle
assumptions, security."

## Steps

### 1. Parse arguments

Split `$ARGUMENTS` into the file path (first word) and the optional focus hint (everything
after). If the path doesn't exist, stop and ask for clarification.

### 2. Build the review prompt

Assemble a self-contained prompt for the reviewer with four parts:

1. **Project orientation** — if a `CLAUDE.md` exists in the working directory (project root),
   summarize the first ~200 words: what this project is, tech stack, key conventions. If no
   `CLAUDE.md`, write a one-line note: "No project orientation available — review the artifact
   on its own merits."
2. **Spec context (if applicable)** — if the artifact is a plan (`docs/**/plans/*.md` or
   similar), grep it for a `Design spec:` or `Related spec:` line and include that path so the
   reviewer can read it.
3. **The artifact** — full file contents inline.
4. **The review brief** — what to look for, structured output format (below).

Prompt template:

```
You are reviewing an artifact. Do not assume context I haven't given you.

# Project orientation

[200-word summary from CLAUDE.md, or "no orientation available"]

# Repo working directory

[absolute path to cwd]

You have full repo read access. Read related files (specs, existing code being
modified, tests) if helpful.

# What to review

Path: <FILE_PATH>
Focus: <FOCUS_HINT>

# The artifact

[full file contents]

# Review brief

Look for problems that would cause real pain in production or implementation:
- Correctness bugs (race conditions, off-by-one, silent type coercion).
- Missing tests for load-bearing behavior (concurrency, error paths,
  security-sensitive code).
- Ordering / sequencing bugs (Phase N depends on what Phase N-1 didn't ship;
  tests reference symbols not yet defined).
- Brittle assumptions (test fixtures that won't survive a library upgrade;
  hard-coded values; behavior depending on undocumented invariants).
- Inconsistencies between sections (docstring says X, code does Y).
- Security issues (binding 0.0.0.0, missing input validation, hardcoded
  secrets, command injection).

Be honest. If the artifact is good, say so clearly. Don't invent problems to
seem useful. Empty findings are fine — that's signal too.

# Output format

## Verdict
APPROVED | APPROVED WITH MINOR FIXES | NEEDS REVISION

## Critical issues (blocking)
[Each with file:line reference and proposed fix. Skip if none.]

## Worth fixing now (medium)
[Things cheap now and annoying later. Skip if none.]

## Nits
[Small polish. Skip if none.]

## Things that look right
[1–3 sentences highlighting non-trivial decisions that are well-handled.
Calibration signal — if you find nothing here, you may be over-indexing on
negatives.]

Keep total response under 1200 words.
```

### 3. Run the review

**Default path — Codex (a different model family).** Write the assembled prompt to a tempfile,
then:

```bash
codex exec - < /tmp/codex-review-prompt.txt 2>&1
```

Use the Bash tool with timeout >= 300000ms (5 minutes). Capture full output.

**Fallback path — a fresh same-model subagent (Claude-only machine).** If `codex exec` fails or
Codex isn't on this machine — network, auth, rate limit, missing binary, or a repo running the
Claude agent with no Codex CLI installed — do **not** fall back silently and do **not** skip the
review. The owner ruling of **2026-07-10** (recorded in the superlooper source repo's
`docs/STACK.md` → `codex CLI` block) makes the choice explicit: on a Claude-only machine, a **fresh same-model subagent that wrote
none of the code** is an equally valid review path. Dispatch that subagent with the exact same
self-contained prompt (parts 1–4 above), naming out loud which path you took. `doctor --stack`
already reports a missing Codex honestly (a WARN on a Claude-only machine, a hard FAIL only when
a repo's config selects `agent: codex`), so choosing the subagent path here is honoring that
posture, not working around it.

The discipline that survives both paths: **never fall back silently and never pretend a review
happened when it didn't.** Announce which reviewer ran; if the chosen path errors in a way you
can't explain (not just "Codex absent" — an actual failure of the fresh-subagent path too),
report it and stop rather than inventing a verdict.

### 4. Surface the review

Show the user the verdict line + critical issues verbatim. Then ask: "Apply these fixes inline,
present the full review for me to read first, or skip?"

- "apply inline": work through critical and medium items, applying fixes with the same TDD
  discipline as normal work. Skip nits unless asked.
- "present full review": dump the full reviewer output and wait.
- "skip": acknowledge and stop.

In the loop, the fresh-agent review is a ship-gate duty: P0/P1 (critical / worth-fixing-now)
findings are fixed before the task's final commit, at most two review/fix rounds per change
(this is a non-regulated project), then a consolidated decision.

### 5. Be honest about value (calibration)

After applying fixes, give a one-paragraph honest assessment of which catches were genuinely
valuable versus **review tax**. This is a calibration loop — if the reviewer consistently finds
nothing useful for an artifact type, the user should know so they don't burn cycles.

## Notes

- The reviewer doesn't see this conversation — always include orientation, the artifact, and
  (if relevant) the spec.
- For artifacts >2000 lines, warn the user that the reviewer's context may truncate. Offer to
  split the review into sections.
- Codex's default model comes from `~/.codex/config.toml`. Suggest `-m <model>` if the user
  wants to override for a specific review; don't override unprompted.
- This skill is pure content: it carries no hook. The machine-local `suggest-cross-review` hook
  (a PostToolUse nudge on the owner's personal paths) is deliberately **not** shipped here — a
  plugin hook would execute ungated, and its triggers are personal setup, not loop machinery.
