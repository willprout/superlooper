# V2/V3 Loop Catalog — reusable skills, repos, and implementation guides

Compiled 2026-07-03 by an Opus research pass (web + live doc verification), commissioned in the
"automation first principles" session. Purpose: when a session builds a V2/V3 loop (see
SPEC-2026-07-02-issue-loop-workflow.md §8 roadmap), start here — each candidate carries a
verdict: USE AS-IS / ADAPT / STEAL PROMPT-DESIGN / SKIP. Unverified claims are listed in Gaps.

**Billing reconciliation (verified against docs):** gh-aw does NOT use GitHub Models. gh-aw
inference bills via its `engine:` — Copilot (default; Copilot AI Credits, 1 AIC = $0.01, Copilot
Pro $10/mo ≈ 1,500 credits) or bring-your-own ANTHROPIC/OPENAI/GEMINI API key. Claude
subscription OAuth is explicitly NOT supported by gh-aw (https://github.github.com/gh-aw/reference/auth/)
— so gh-aw's Claude engine = metered API dollars (William's money rule applies). Separately,
PLAIN GitHub Actions can request `models: read` and get free rate-limited GitHub Models
inference via the built-in GITHUB_TOKEN (docs.github.com billing → GitHub Models) — that's why
pelikhan's dedup action is $0 LLM while gh-aw runs aren't. All Actions also consume minutes:
free on public repos; private: 2,000 min/mo free plan, 3,000 Pro, then $0.006/min.

## Catalog

### Foundation substrate (feeds V2 a/b/d and more)

- **GitHub Agentic Workflows (gh-aw)** | https://github.com/github/gh-aw · docs
  https://github.github.com/gh-aw/ · announce github.blog changelog 2026-06-11 | runs in GitHub
  Actions (markdown in `.github/workflows/` compiled via `gh aw compile`; triggers: cron/fuzzy
  schedule, issue/PR events, slash_command, label_command, dispatch) | billed: Actions minutes +
  engine inference (Copilot credits default or BYO API key; NO GitHub Models; NO Claude
  subscription OAuth) | MIT | **ADAPT.** Solo dev on private repo explicitly supported (FAQ:
  "Yes, and in many cases we recommend it"). Engines: copilot/claude/codex/gemini + experimental.
  Safe-outputs is the killer feature: agent runs read-only; a separate privileged job executes
  validated structured outputs — create-issue, update/close-issue, link-sub-issue, add-comment,
  add/remove-labels, create-pull-request, create-discussion, create-code-scanning-alert,
  dispatch-workflow, plus `expires:` (auto-close agent-filed issues after 7d/2w via a generated
  maintenance sweeper), `close-older-issues: true` (supersede), `deduplicate-by-title` (exact or
  Levenshtein 0–100), `group: true` (sub-issue nesting ≤64), `max` caps, `title-prefix`. It
  duplicates the local runner in the cloud — adapt selectively for always-on lanes (triage,
  stale) that shouldn't depend on the Mac being awake.
- **githubnext/agentics sample pack** | https://github.com/githubnext/agentics (install:
  `gh aw add githubnext/agentics/<name>`) | Actions | gh-aw billing | MIT | **STEAL
  PROMPT-DESIGN / ADAPT.** 61 workflow .md files. V2 mappings: `issue-triage.md` +
  `repo-assist.md` (V2a), `ci-doctor.md` + `pr-fix.md` (CI investigation), `code-simplifier.md` /
  `repository-quality-improver.md` / `daily-test-improver.md` / `large-file-simplifier.md` (V2b
  family), `vex-generator.md` + `daily-malicious-code-scan.md` (V2c-adjacent),
  `issue-arborist.md` + `sub-issue-closer.md` (issue hygiene), `weekly-issue-activity.md` /
  `weekly-research.md` / `cost-tracker.md` (digests). Caveats: no dedicated duplicate-issue
  sample (dedup lives in safe-outputs) and no stale-PR/branch sample (that's `expires:`). Even
  without running gh-aw, this is the best prompt corpus to mine for local loops.
- **githubnext/awesome-continuous-ai** | https://github.com/githubnext/awesome-continuous-ai |
  catalog | — | — | **USE AS-IS** as the reference index of Continuous-AI actions/frameworks.

### V2a — Issue triage & dedup

- **pelikhan/action-genai-issue-dedup** | https://github.com/pelikhan/action-genai-issue-dedup |
  Actions on new-issue events | **$0 LLM** (GitHub Models via `models: read`; only Actions
  minutes on private repo) | MIT | **USE AS-IS.** Two-tier LLM dedup (small model batch-scans,
  large model validates), labels dupes. Drop-in `uses:` block. The cleanest solo-dev win in this
  report. Pairs with **pelikhan/action-genai-issue-labeller** (same author, labeling).
- **home-assistant/core detect-duplicate-issues.yml** |
  https://github.com/home-assistant/core/blob/dev/.github/workflows/detect-duplicate-issues.yml |
  Actions | GitHub Models free | Apache-2.0 | **STEAL PROMPT-DESIGN.** Short production
  reference of Actions+Models dedup.
- **openclaw/clawsweeper** (Steinberger) | https://github.com/openclaw/clawsweeper | Actions or
  local CLI; 50 parallel Codex workers; state in sibling clawsweeper-state repo | OpenAI key,
  metered | MIT | **STEAL PROMPT-DESIGN.** Org-scale (~4,000 closes/day) — overkill solo, but
  its dedup logic ("at most one evidence-backed canonical item per root cause; close only when
  duplicate/superseded", `close:duplicate`/`dedupe:child` labels, review/apply/repair/commit
  lanes) is the gold-standard prompt to fold into the triage skill.
- **gh-aw safe-outputs dedup** (`deduplicate-by-title`, `close-older-issues`,
  `issue-arborist.md`) | above | Actions | gh-aw billing | MIT | **ADAPT** if triage moves to
  gh-aw — dedup then comes free at the output layer.
- **GitHub first-party AI issue dedup** | docs.github.com → triaging-an-issue-with-ai | — | — |
  — | **SKIP for now.** First-party AI triage/labeling exists; automatic duplicate detection as
  GA NOT confirmed.

### V2b — Tech-debt sweeper

- **ksimback/tech-debt-skill** | https://github.com/ksimback/tech-debt-skill | local Claude Code
  (`/tech-debt-audit`) | Claude subscription | MIT | **ADAPT.** 9-dimension audit with
  file:line/severity/effort; repeat-run mode diffs NEW/RESOLVED across runs (week-over-week
  dedup for free). Gap: emits a markdown report, not issues — bolt on a weekly wrapper that
  diffs the audit and opens batched issues via `gh`.
- **Boris Cherny's /post-merge-sweeper (+ /babysit, /pr-pruner, /slack-feedback)** |
  https://howborisusesclaudecode.com/ · x.com/bcherny/status/2038454341884154269 | local Claude
  Code via `/loop` | Claude subscription | not published | **STEAL PROMPT-DESIGN.** The actual
  .md definition files are NOT published anywhere (confirmed: site, gists, anthropics org
  search). Only one-line intents exist. The reusable idea is the architecture: thin
  single-purpose skills, each wrapped in `/loop <interval>`, one PR-lifecycle chore each.
- **tilomitra babysit-pr gist** |
  https://gist.github.com/tilomitra/e0dca29b3a63b5b5aba62c1baeaa27b4 | local Claude Code |
  Claude subscription | no license | **ADAPT (as reference).** Most faithful downloadable
  /babysit clone: 8-step loop, max-5 iterations, no-force-push/no-auto-merge guardrails.
  Unlicensed → reimplement, don't copy.
- **Anthropic official plugins** | https://github.com/anthropics/claude-plugins-official (+
  bundled marketplace.json in anthropics/claude-code) | local Claude Code via `/plugin` | Claude
  subscription | Apache-2.0 | **USE AS-IS as building blocks.** `ralph-loop`/`ralph-wiggum`
  (loop engine with completion-promise guard), `pr-review-toolkit` (/review-pr, 6 specialist
  agents), `code-review` (authored by Boris), `commit-commands`, `code-simplifier`. No shipped
  triage/pruner/babysit plugin exists as of mid-2026.
- **GitHub billing-team tech-debt practice** | github.blog (billing team + Copilot coding agent
  burn-down) | — | — | — | **STEAL PROMPT-DESIGN** — the discipline: small digestible
  single-concern issues, never 100-file sweep PRs.

### V2c — Dependency/security update lane

- **dependabot/fetch-metadata + `gh pr merge --auto` pattern** | docs.github.com →
  automate-dependabot-with-actions | Actions | free (no LLM) | MIT | **USE AS-IS.** Canonical
  recipe: read update-type, gate on required status checks (the existing test gates), auto-merge
  **patch-only**; never minor/major unattended (supply-chain amplification).
- **Renovate** | https://docs.renovatebot.com/ | Actions/hosted | free | AGPL (app free) |
  **ADAPT (fallback)** if Dependabot's gates prove too coarse (90+ managers, merge-confidence).
- LLM-reviews-each-Dependabot-PR | — | — | — | — | **SKIP.** No credible OSS implementation;
  correct move is pointing the existing PR-review skill at dep PRs.

### V2d — Janitor (stale branches/worktrees/PRs)

- **wrannaman git-cleanup skill** |
  https://github.com/wrannaman/agentic-engineering/blob/main/skills/git/git-cleanup/SKILL.md |
  local Claude Code (`/git-cleanup [--stale-weeks N]`) | Claude subscription | **license
  undisclosed — check before copying** | **USE AS-IS / ADAPT.** Exactly the local
  worktree+branch janitor: removes worktrees tied to merged/closed PRs, stale branches past
  threshold (default 4w), protects main/develop/current, confirmation-gated before deletion —
  matches never-auto-close.
- **actions/stale** | https://github.com/actions/stale | Actions | free, no LLM | MIT | **USE
  AS-IS** for the dumb timer baseline on issues/PRs (doesn't touch branches/worktrees).
- **dosu-ai/better-stale-bot** | https://github.com/dosu-ai/better-stale-bot | Actions, built ON
  gh-aw | gh-aw billing | license not stated | **ADAPT.** Reads the thread and classifies
  resolved-vs-unresolved before acting. Best agentic stale option if gh-aw is adopted.
- **ashleywolf/continuous-ai-resolver** | https://github.com/ashleywolf/continuous-ai-resolver |
  Actions weekly cron | OpenAI key (or swap engine) | MIT | **ADAPT.** Weekly cadence +
  already-fixed detection with explanatory comments; "built to be forked." Swap to GitHub
  Models to make it free.
- **brtkwr bulk worktree cleanup** |
  https://brtkwr.com/posts/2026-03-06-bulk-cleaning-stale-git-worktrees/ | local shell, no LLM |
  free | blog | **STEAL DESIGN** for a zero-cost cron layer under the skill.

### V2e — Ratchet loop (failures → permanent CLAUDE.md/skill rules)

- **borghei/claude-skills `self-improving-agent`** | https://github.com/borghei/claude-skills |
  local Claude Code (`npx -y skills add borghei/claude-skills --skill self-improving-agent`) |
  Claude subscription | MIT + Commons Clause | **ADAPT.** Closest existing ratchet:
  Remember/Extract/Promote/Review sub-skills; captures session learnings into MEMORY.md and
  promotes proven patterns to enforced CLAUDE.md rules. Caveat: session-triggered, not fired off
  failure artifacts — wire it to the issue-loop's postmortem output. (Promote mechanic lightly
  verified.)
- **snarktank/ralph** | https://github.com/snarktank/ralph | local bash loop | Claude/Amp
  subscription | MIT | **STEAL PROMPT-DESIGN.** "Append discovered gotchas to AGENTS.md every
  iteration so later runs don't repeat mistakes" = the ratchet mechanic in minimal form; skip
  the build-loop scaffolding.
- **Concept sources** | addyosmani.com/blog/self-improving-agents/ · dev.to/aviadr1 (one prompt
  that makes Claude learn from every mistake) · productcompass.pm self-improving-claude-system |
  prose/prompts | — | — | **STEAL PROMPT-DESIGN.** aviadr1's one-mistake→one-rule prompt is
  directly reusable. Anthropic has NOT shipped anything official (issue #57830 = open request).

### V2f — Slack bug-report bot

- **GitHub for Slack + Copilot `@GitHub` issue drafting** | https://github.com/integrations/slack ·
  github.blog changelog 2026-03-30 | GitHub-hosted cloud app, zero infra | Slack app free; AI
  drafting needs a Copilot plan (all plans incl. limited Free tier) | proprietary service |
  **USE AS-IS.** GA March 30, 2026: mention `@GitHub` in a channel, natural language →
  structured issue (title/body/labels/assignees, parent/child split, in-thread refinement,
  per-channel default repo via `@GitHub settings`). Least-maintenance path by a wide margin.
  Caveats: drafting rigor = Copilot quality; whether it ingests the reporter's original message
  verbatim is unverified. NOTE: pair with the triage/dedup lane so `@GitHub`-drafted issues get
  RE-DRAFTED into the rigorous issue format by our own skill before they can be labeled ready.
- **Zapier/n8n: Slack → Claude draft → GitHub issue** | zapier.com GitHub+Slack templates ·
  n8n.io/workflows | managed no-code runtime | platform fee + Anthropic tokens |
  proprietary/fair-code | **ADAPT (fallback).** Same shape, platform owns the runtime; we own
  only the drafting prompt (where the rigor lives).
- **claude-code-action via Slack→repository_dispatch bridge** |
  https://github.com/anthropics/claude-code-action · code.claude.com/docs/en/github-actions |
  GitHub Actions | Actions minutes + Anthropic API key OR `CLAUDE_CODE_OAUTH_TOKEN`
  (**subscription token accepted — unlike gh-aw**) | MIT | **ADAPT (max-rigor fallback).** v1
  GA, mature (~18k repos), any GitHub event in automation mode; needs a thin Slack→dispatch
  bridge we own. repository_dispatch inferred from docs, not example-verified.
- **Anthropic Claude in Slack / Claude Tag** | Slack marketplace A08SF47R6P4 ·
  anthropic.com/news/introducing-claude-tag | Anthropic-hosted | Claude Tag = token-metered,
  Team/Enterprise beta only (replaces the Slack app Aug 3, 2026) | proprietary | **SKIP for
  solo** (steal the channel-resident-agent design).
- **OpenClaw (steipete)** | https://github.com/openclaw/openclaw · steipete.me/posts/2026/openclaw |
  self-hosted always-on daemon (launchd), Slack + many channels, agents run bash/`gh` | free +
  own LLM tokens | MIT | **SKIP for this task.** Connects Slack to tool-running agents (~382k
  stars; Steinberger joined OpenAI Feb 2026, project moving to a foundation) — but an always-on
  shell-capable agent on a semi-public #bugs channel is a real prompt-injection surface, and its
  own docs demand sandboxing + treating inbound as untrusted. Opposite of least-maintenance.
- **Custom Bolt app** | starter: github.com/slack-samples/bolt-python-ai-chatbot
  (Anthropic-wired); priors: aelkugia/Issue-Slack-Bot (no AI), thea1lab/bug-triage-bot
  (Claude→Jira) | self-hosted (~100–200 lines, Socket Mode) | Anthropic tokens + hosting | MIT
  samples | **ADAPT only if the above fail the rigor bar.** Most control, most upkeep.

### Regression-suite substrate (V1 gates; the collaborative piece)

- **Playwright Test Agents (planner/generator/healer)** | https://playwright.dev/docs/test-agents |
  local, in Claude Code | $0 tooling (Apache-2.0); inference on existing Claude subscription
  (~110–140k tokens per focused task) | Apache-2.0 | **USE AS-IS to start, then ADAPT.**
  Setup: `npm init playwright@latest` → `npx playwright install chromium` →
  `npx playwright init-agents --loop=claude` → writes `.claude/agents/` (3 agent defs),
  `.mcp.json` (Playwright MCP, local, no key), `specs/`, `tests/seed.spec.ts`. Workflow: planner
  explores the RUNNING app → markdown plan into specs/ (human reviews) → generator emits specs
  validated live → run → healer patches stale tests or flags real regressions (reject patches
  that weaken assertions or add timeouts). Prerequisite: green seed test with auth/storageState.
  Walkthroughs: stevekinney.com self-testing-ai-agents lab (Claude-Code-specific; includes
  break-then-heal exercise), dev.to/playwright planner-generator-healer-in-action,
  shipyard.build/blog/playwright-agents-claude-code.

### V3 — Analytics/bug-monitoring substrate

- **PostHog** | https://posthog.com/docs/model-context-protocol (hosted MCP:
  https://mcp.posthog.com/mcp) | cloud (replay is Cloud-only) | free tier: 1M events, 5K
  replays/mo, then usage-based | MIT core | **USE AS-IS — the V3 pick.** Only single product
  covering ALL needed signals — errors w/ stack traces, session replay, rage clicks (>3
  clicks/1s) AND dead clicks, native funnels — through a structured official MCP: ~24
  error-tracking tools (`query-error-tracking-issues-list`), `query-funnel`/
  `query-funnel-actors`, ~10 replay tools incl. `session-recording-summarize`, `heatmaps-list`,
  HogQL. Published rate limits (HogQL 120/hr — batch weekly queries). Plus Max AI in-product.
- **Sentry** | https://mcp.sentry.dev/ · getsentry/sentry-mcp-stdio · docs → replay rage-clicks |
  cloud (self-hostable) | free Developer tier (5K errors, 50 replays); Team $26/mo | FSL |
  **ADAPT (add later if debugging depth becomes the bottleneck).** Best-in-class errors +
  replay with first-class rage/dead-click Replay Issues; Seer root-cause agent invocable
  through the MCP. Fatal gap for the full brief: no funnels/product analytics — answers "what
  broke," not "where users abandon."
- **LogRocket** | docs.logrocket.com/docs/mcp (mcp.logrocket.com/mcp) ·
  logrocket.com/features/struggle-detection | cloud only | free 1K sessions/mo; Team pricing
  conflicting ($69–$99/mo — verify) | proprietary | **SKIP for the agent loop.** Richest native
  frustration signals, but the MCP is natural-language Ask-Galileo-centric rather than
  deterministic structured tools; REST rate limits unpublished; wrong shape for a repeatable
  weekly scheduled agent.

### Cross-cutting security note (applies to ALL cloud-hosted loops and to V2f intake)

A June 2026 claude-code-action flaw let one malicious issue hijack repos / leak CI secrets
(thehackernews.com 2026/06 + Microsoft security blog 2026/06/05 case study). Rules: pin actions,
minimum token scope, treat issue/PR text as UNTRUSTED INPUT — or prefer gh-aw's safe-outputs
architecture (read-only agent + privileged validated-output job), designed for exactly this.
For our system: the moment V2f lets non-William reporters put text into the queue, the issue
body becomes an injection surface for every downstream agent that reads it — the triage skill
must re-draft (never pass through) reporter text, and worker briefs should treat quoted
reporter content as data, not instructions.

## Best-fit picks (primary → fallback)

- **V2a triage/dedup:** pelikhan/action-genai-issue-dedup (free, MIT, drop-in) + steal
  agentics `issue-triage.md` prompt for the local drafting skill → gh-aw issue-triage +
  safe-outputs dedup.
- **V2b sweeper:** ksimback/tech-debt-skill + weekly gh-issue-batching wrapper (DIY) → gh-aw
  repository-quality-improver / code-simplifier prompt designs.
- **V2c dependency lane:** dependabot/fetch-metadata + required checks + `gh pr merge --auto`,
  patch-only → Renovate with merge-confidence.
- **V2d janitor:** wrannaman git-cleanup skill (verify license) + actions/stale baseline →
  better-stale-bot (if on gh-aw) or forked continuous-ai-resolver on GitHub Models.
- **V2e ratchet:** write from scratch, adapting borghei's MEMORY→CLAUDE.md promotion +
  aviadr1's one-mistake→one-rule prompt → ralph's append-to-AGENTS.md mechanic.
- **V2f Slack bot:** GitHub for Slack + Copilot `@GitHub` (zero infra, GA) feeding our triage
  re-draft lane → Zapier/n8n Slack→Claude→GitHub → claude-code-action + dispatch bridge.
- **V3 monitoring:** PostHog Cloud alone (one MCP, all five signals, free tier) → add Sentry
  MCP+Seer later for debugging depth.
- **Regression suite:** Playwright Test Agents `--loop=claude` (no fallback needed).

## Gaps

**Must write from scratch:**
1. **Ratchet loop (V2e)** — no maintained "failure artifact → proposed CLAUDE.md rule" product
   exists; only session-memory skills and prompt patterns. The failure-artifact-reader half is
   entirely DIY.
2. **Tech-debt findings → batched weekly issues bridge (V2b)** — auditors exist; the
   harvest-into-deduped-weekly-issues half doesn't (Boris's version unpublished).
3. **Slack→dispatch bridge** if the claude-code-action route is chosen.
4. **V3 investigation-filing glue** — PostHog MCP delivers signals; the weekly "query →
   threshold → file INVESTIGATION issue" skill must be written (scheduled session + PostHog MCP
   + gh).

**Could not verify (re-check before relying):**
- Boris's four command definition files: confirmed NOT published.
- Licenses: wrannaman git-cleanup (undisclosed), better-stale-bot (not stated), tilomitra gist
  (none).
- GitHub first-party automatic issue-dedup GA status.
- Whether Slack `@GitHub` ingests the reporter's original message verbatim; Copilot Free-tier
  quota for it.
- claude-code-action via repository_dispatch (inferred, no published example).
- LogRocket Team pricing ($69 vs $99) and REST rate limits.
- Playwright minimum version (1.56 vs 1.59); `.claude/agents/` path from secondary sources.
- gh-aw personal-Copilot billing nuance (docs framed around org subscriptions); Copilot Pro
  1,500-credit figure is from Copilot billing coverage, not gh-aw docs.
