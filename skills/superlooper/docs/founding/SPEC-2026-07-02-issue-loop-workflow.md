# SPEC 2026-07-02 — The issue-loop workflow (v1 direction, for review)

**Status: agreed direction between William and the fresh-eyes session, 2026-07-02. Not yet
built. Supersedes PROPOSAL-2026-07-02-conductor-architecture.md** (whose addenda A1–A4 are
absorbed below; A5/A6 are obsoleted by this spec). **Rev 2 (2026-07-02, same day):** folds in
the context-rich review — owner rulings on per-PR security review (§4.3) and the red-nightly
standing rule (§4.4), plus agreed fixes for issue relationships/drift (§4.1–4.2) and same-line
merge conflicts with per-repo parallelism policy (§4.2, §4.4). Reviewers: assess against your deeper
context on autocode and the eApp; §9 lists what your input is specifically wanted on. §2 lists
William's explicit directives — those are fixed points, not open questions.

## §1 — Provenance (how this direction was reached)

1. A fresh session derived an architecture blind (no access to autocode), producing the
   "conductor" proposal: deterministic process acts, ephemeral AI sessions judge, no standing
   orchestrator. Verdict on autocode's H4: the standing LLM duty set should be zero.
2. William then lifted his own constraints ("don't anchor on past decisions; don't protect
   decisions I already made") and supplied two reframes that simplified the design further:
   the eApp is at ~1.0 (this is 1→1.2 machinery, not 0→1), and release judgment can move to a
   dev→prod promotion gate rather than per-PR ceremony (later scoped by the rev-2 ruling:
   security/SOC-2 review stays per-PR — §4.3).
3. Researched evidence (Opus agent, primary sources + live GitHub API pulls, 2026-07-02)
   established what top practitioners actually do (§6). This killed the batch/merge-train
   premise and the strict-serial answer both, and produced the two-gate model below.

## §2 — William's explicit directives (verbatim intent; do NOT relitigate or safety-creep)

- **Approval-by-conversation.** In a planning session, William saying "these issues are
  approved" IS the approval; the agent then applies the `agent-ready` labels for him. The label
  records the approval; it is not itself the approval. The only bright line: no agent labels
  work `agent-ready` absent his explicit say-so in conversation (or a standing auto-approval
  rule he himself defines). *"I don't want some agent between now and when this gets actually
  built deciding that that's not possible."*
- **Packaging.** The deliverable is (a) a UNIVERSAL skill — shareable with friends, usable on
  William's other repos/products — plus (b) a separate, thin set of eApp process changes that
  adapt the eApp to the skill. Never build it eApp-specific.
- **Collaborative QA build.** The e2e/browser test suite and general browser-testing machinery
  are built WITH William in the loop — it is new technology to him and he wants to work on that
  section collaboratively, not receive it.
- **Agents write issues, never William.** Issue creation always goes through agents using a
  dedicated issue-writing skill that enforces the rigorous format (this is the mechanism against
  the low-quality-issue failure mode).
- **Promotion is a human decision, never a mechanical switch.** No "must pass everything to
  promote" logic anywhere. Deterministic checks produce evidence; William decides. No agent
  nitpicks can block promotion. (Full design in §5.4.)
- **Full simulated-user browser runs** are included in the nightly and pre-promotion test runs —
  per-PR browser testing is not the only browser testing — and nightly failures create GitHub
  issues automatically.
- **Sequencing.** The e2e browser gate gets built BEFORE this workflow is fully implemented.
  Treat the loop as 1→1.2 machinery for the eApp.
- **Tolerances.** Occasional cross-PR semantic breaks on dev, rare silent overnight stops, and
  stuck-label states are acceptable if designed-for; do not over-engineer against them in v1
  (loops can tackle them later).
- **Standing global rules remain:** no metered/paid spend without explicit confirmation; every
  review by a fresh agent that did not write the code; push notifications for long-running
  work finishing/stalling/needing input.
- William is a solo, non-professional developer with ADHD who moves on from finished projects:
  bias every choice toward self-maintaining systems and batched one-touch upkeep.

## §3 — The model in one paragraph

Two human gates with a loose, fast, machine-run middle. **Gate 1 (intake):** work exists only as
GitHub issues, written by agents in planning conversations with William and approved by his word
(recorded as a label). **The middle:** a small deterministic loop runner takes approved issues,
spawns one fresh Claude session per issue in its own worktree, and merges to the dev mainline
when mechanical gates pass — tests, a real-browser drive of the changed feature, a fresh-agent
review. No standing LLM session anywhere; no LLM ever manages merge mechanics. **Gate 2
(promotion):** dev→prod is William's deliberate, batched, evidence-backed decision, and it is
where the RELEASE judgment lives — feature acceptance, copy/UX, change-management evidence.
Security and SOC-2/data-handling review stays in the per-PR gate at full strength (§4.3).

## §4 — Components

### 4.1 Issues (the work queue — GitHub is the state store)

- **Types:** `build` (pre-scoped; one issue → one PR; must carry a definition of done),
  `investigate` (undiagnosed problem; output = root-cause report on the issue + scoped child
  issues as sub-issues under the parent; zero PRs), `diagnose-and-fix` (small bugs; one session
  diagnoses AND fixes if the fix stays within declared boundaries; splits into approval-needing
  children only if the root cause is big; on the eApp, a fix touching bright-line areas always
  splits).
- **Labels as protocol:** `agent-ready` (approved, queued), `in-progress`, `needs-william`
  (parked on an owner decision, with a memo comment), `parked` (failed after retry cap),
  `expedite` (jumps the queue). Ordering: expedite first, then William's priority order, then
  oldest-first.
- **Approval of investigation children:** on the eApp, children wait for William's label
  (one touch releases a series). On lower-stakes products, a William-defined standing rule may
  auto-approve small children.
- **Thin-issue doctrine (rev 2 — the drift fix at its source):** an issue states GOAL,
  definition of done, and boundaries — durable intent — and POINTS at where truth lives ("see
  the shape in src/types/application.ts"); it never ASSERTS current code facts. Intent doesn't
  rot; assertions do (the last run's stale-brief incident was a repo-state assertion rotting in
  the queue). The issue-writing skill enforces this. **Agents never edit an approved issue's
  goal or DoD** — reconciliation (§4.2) appends comments only; scope changes go back through
  Gate 1.
- **Ordering (rev 2):** where one issue genuinely must not start before another's PR merges, it
  declares `blocked-by: #N` (GitHub's native issue relationship where available; a parsed body
  line otherwise). Runner eligibility rule: all blocked-by references closed. The issue-writing
  skill treats blocked-by as a smell to justify, not a default — prefer re-scoping into one
  issue or independently-landable pieces (dependency chains are where nights die: sub-1 parked
  held sub-4/sub-5 all night in run-20260701-1750).
- **Touch declarations (rev 2):** issues carry a `touches:` hint naming the areas they hit —
  MANDATORY on the eApp (issue-writing skill enforces), optional elsewhere. Feeds the
  co-scheduling policy in §4.2.
- The loop runs whenever labeled work exists — daytime included. "Overnight" is not a mode.

### 4.2 The loop runner (deterministic, no AI inside)

- A small plain script/process on William's Mac, running in a terminal, alive indefinitely.
  Picks the top N ready issues, spawns each as a fresh Claude session in a fresh worktree with
  the issue as its entire brief, watches via the file-signal machinery autocode already proved
  (delivery verification, activity/heartbeat, timeout ladders), and executes all mechanics
  itself: merge-on-green, label transitions, retry caps (2, then `parked`), queue reclaim of
  orphaned `in-progress` issues, the morning report, push notifications.
- **Per-repo parallelism policy (rev 2 — one skill, per-repo config, per §2 packaging).**
  Lanes (N) and co-scheduling strictness are repo config, because conflict probability ≈
  branch lifetime × overlap, and the eApp maximizes both (longest per-PR gate, highest cost of
  mess — William's observation). eApp defaults: N=2 with **hard anti-affinity** — two issues
  co-schedule only when their declared `touches:` areas are disjoint (e.g. frontend CSS ∥ API
  layer); otherwise effectively sequential. Other repos: soft anti-affinity (avoid overlap,
  don't forbid it), small PRs as the main defense. The morning report tracks **conflict
  regenerations per week** as the evidence for tuning the dial either direction.
  **Declared areas are verified, not trusted (rev 2.1):** at gate time the runner mechanically
  compares the PR's actual diff paths against the issue's declared `touches:`. A wander is
  logged (morning report + ratchet input); if the actual paths overlap another in-flight lane,
  the runner falls back to sequential handling for those two (hold the second merge; normal
  conflict ladder applies). Without this check the disjointness guarantee is an honor system
  that a wandering session silently breaks.
- **Launch-time reconciliation (rev 2 — the drift fix at execution; pull, not push).** The
  FIRST mandated step of every session: reconcile the issue against current main. Small drift
  (stale pointer, renamed file) → proceed, append a note to the issue. Premise-level drift
  (problem gone, approach invalidated) → bounce: `needs-william` + one-line explanation, take
  the next issue. This replaces the wave-checkpoint's push model (rewrite all queued briefs
  after every merge; could still miss late merges) with a pull that checks at the moment it
  matters against the freshest state. Approved goal/DoD text is never modified (§4.1).
  **Accepted tradeoff (rev 2.1):** genuine cross-issue contract changes now cost a William-touch
  where the old wave-checkpoint silently rewrote queued briefs — correct, because scope changes
  should pass Gate 1. Softener: every bounce memo includes a ready-to-approve proposed
  amendment to the goal/DoD, so the touch is yes/no, never authoring.
- Judgment is hired per-event exactly as in the conductor proposal: a blocked worker's question
  goes to a fresh answerer agent with a one-page role brief; unclassifiable situations park.
- Model policy: planning/judgment on the strong tier; loop workers and reviews on the cheaper
  strong tier; the runner itself costs zero model tokens. Quiet nights are zero-turn.

### 4.3 The per-PR ship gate (mechanical, strict — strictness lives HERE)

All required, all machine-checked: (1) the session's own tests pass; (2) the session **drives
the changed feature in a real browser** and records evidence (Cherny's "can the agent run the
thing"); (3) it adds/updates coded regression tests covering what it built (the suite must grow
with every PR); (4) **the repo's own mechanical review pipeline at full strength** — on the
eApp that is the existing multi-stage cross-model cascade INCLUDING its security and
SOC-2/data-handling review (owner ruling, rev 2: security review never moves to promotion; it
was catching real findings per-PR and stays there); at minimum, everywhere, a fresh agent that
did not write the code (cross-review). P0/P1 block; owner-only findings park that one issue
(park-and-continue, decisions one-touch durable); lower severities recorded for the sweeper;
(5) CI green. Then the runner merges (squash) to dev main and closes the issue, cross-linked.
The per-PR friction this spec removes is OWNER KEYSTROKES, never machine rigor — the cascade
is $0 and machine-run and was never the friction.

### 4.4 Merge policy on dev (loose by design)

- **Drop the strict up-to-date-branch requirement on dev main.** Lanes merge in completion
  order. Rationale in §6 — this is what made parallelism expensive and forced the old merge
  train.
- **Fix-forward:** if dev main goes red after a merge (nightly or post-merge CI), the runner
  freezes further merges, auto-files a fix issue at the head of the queue, and building
  continues. Red dev is contained by Gate 2; prod is never exposed.
- **Red-nightly standing rule (rev 2 — the worked example of §2's standing auto-approval
  mechanism, resolving the overnight Gate-1 deadlock):** issues auto-filed from a red nightly
  are pre-approved as `diagnose-and-fix`, scoped strictly to RESTORING GREEN (never
  opportunistic improvements), and carry the distinct label `auto-approved:nightly-red` so the
  morning report and audit trail show exactly which work entered by standing rule vs William's
  word. The bright line holds because William defined the rule in advance; no agent approved
  anything. If the fix fails its cap, merges stay frozen until morning — frozen-but-building is
  the safe idle state, not something to escape at 3am.
- **Same-line merge conflicts (rev 2 — git refuses these regardless of protections):** the
  runner's deterministic ladder: (1) try a mechanical rebase in the worktree; clean → re-run CI
  → merge. (eApp adaptation, rev 2.1: a rebased commit needs its review-gate stamp re-posted —
  the runner re-runs ship.sh, which mechanically reuses its verdict for an identical diff, no
  model involved. This must be wired in or step 1 dead-ends on the eApp; §9.1/§9.3 work.) (2) Real conflict → **regenerate, don't resolve**: mark the PR superseded (branch
  preserved, nothing auto-closed), comment the issue "conflicted with #M — rebuilding on
  current main," relabel `agent-ready` at the front of its priority band; a fresh session
  rebuilds against a main that already contains the other change, and every gate re-runs. The
  brief is the durable artifact, the diff is disposable ("prompt requests" — §6); this converts
  a merge problem (forbidden: no LLM in merge mechanics, no auto-resolving conflicts into main)
  into a build problem (the system's home turf). (3) Conflicts get their OWN counter, cap 2 —
  two collisions on one issue means two work items are fighting over the same code: park with
  `needs-william` (a scoping error only he can untangle). Optional per-PR escape: a William-
  applied `preserve` label routes an expensive PR to a conflict-resolution session in the PR's
  own branch instead, followed by full re-review and re-gates. Prevention is §4.2's
  anti-affinity policy.
- No LLM in merge mechanics, ever — the single invariant both researched camps share (§6).

### 4.5 The QA stack (four layers)

1. **Per-PR:** agent-driven browser run of the changed area + the relevant saved regression
   scripts (§4.3).
2. **Nightly on dev:** the FULL regression suite — every saved simulated-user journey in a real
   browser — merges or no merges. Failures auto-file issues at the head of the queue. Catches
   cross-PR interactions, drift, and flakiness between promotions.
3. **Pre-promotion:** the full suite again, as evidence for Gate 2.
4. **Post-promotion:** a small smoke run against prod (a handful of critical journeys).

Flakiness handling (expected #1 operational annoyance): auto-retry-once on e2e failure, a
quarantine list for known-flaky tests (Playwright healer pattern — distinguishing "stale test"
from "broken app"), and gate-health stats in the morning report.

### 4.6 The promotion gate (Gate 2 — evidence + judgment, never a switch)

- The promotion run is deterministic: execute the full suite, diff results against the
  **known-failure ledger**, compile a report (NEW failures highlighted; accepted-known failures
  folded away; summary of everything merged since last promotion; open issue summary).
- **William decides.** Once he accepts a failure as non-blocking, that acceptance persists —
  fingerprinted to the failure content, not to a commit — so the same finding never re-blocks
  (the same one-touch durability principle as autocode's L7). New findings discovered during
  promotion become ordinary queue issues; they do not stand in front of the gate.
- An optional hard-blocker list exists only if William defines one (e.g., compliance-critical
  flow red, unresolved security finding), and even those mean "requires William's explicit
  override," never "cannot promote."
- **Why this shape:** a mechanical all-green gate plus an ever-growing test suite is a proven
  doom loop (promote → fail → new issues → fix → new failures → never promote). Strictness
  lives per-PR where failures are small and cheap; the release boundary gets judgment. This is
  also where SOC 2 change-management evidence is generated: batched, deliberate, owner-signed.

### 4.7 The ratchet (how the system compounds)

Boris Cherny's habit, mechanized for an unattended system: every mistake becomes a permanent
rule. The runner captures failure artifacts as they happen (parks, guard trips, red nightlies,
quarantines, recurring review findings); a weekly ratchet pass drafts proposed rules — CLAUDE.md
lines, issue-writing-skill tightenings, worker-brief edits — each citing the incident that
motivated it. William approves rules like he approves issues. Target: every line of config
traceable to a specific thing that went wrong; no failure recurs.

## §5 — Failure model

The nobody-responds-for-8-hours standard is inherited unchanged: every failure lands as either
"continued safely around it" (issue-scoped: park + next) or "stopped early and safely"
(system-scoped: freeze merges, journal, resumable). Notifications are a convenience layer.
Specific handled modes: latency chains (daytime running + `diagnose-and-fix` + `expedite`),
flaky e2e (retry + quarantine + health stats), red dev main (freeze + fix-forward; overnight,
the §4.4 standing rule keeps diagnosis running without breaching Gate 1), same-line merge
conflicts (rebase-if-clean → regenerate → park at 2; prevented by per-repo anti-affinity),
queued-issue drift (thin-issue doctrine + launch-time reconciliation, §4.1–4.2), the 2am
silent stop (auth expiry/disk/hangs — reuse autocode's delivery-proof and liveness machinery,
plus a keep-alive restart), stuck labels (timeout reclaim), runner death (fail-stopped: nothing
merges; restart rebuilds from GitHub + disk, treating GitHub as truth so William's manual
daytime work is absorbed automatically).

## §6 — Evidence base (why these decisions; verified 2026-07-02)

- **No standing orchestrator:** three real autocode runs
  (AUDIT-2026-07-02-first-hardened-run.md): every unresolved symptom (S1 rotation defiance, S3
  judgment-duties never firing, S4 idle successor churn, S5 paid polling) lives in the standing
  LLM seat; the mechanical floor ran clean. Rotation's own design proves a fresh session can
  resume everything from disk — so no session needs to persist.
- **Issues as the queue:** the "loops, not prompts" school (Cherny anniversary material,
  Steinberger's June 8 framing, Osmani's loop-engineering essay): durable state outside the
  chat; GitHub as memory; label-gated conveyors as the documented pattern (Anthropic-internal
  example: Cat Wu's ticket-listener → fix-PR routine; GitHub's own failure-investigator files
  diagnostic issues autonomously).
- **Loose dev protections + parallel dial:** live GitHub data. Steinberger: 187 authored PRs
  merged on 2026-07-01 alone, no required reviews, self-merge in minutes, an agent reviewer
  (clawsweeper) whose findings get addressed pre-merge — throughput comes from loose gates +
  strong local verification, not from orchestration. William's old strict up-to-date protection
  is what forced serial merges and the LLM-improvised merge train that caused his worst
  overnight failures. GitHub's native merge queue is unavailable to a solo private repo
  (Enterprise Cloud only), so "strict + parallel" has no free mechanical solution — loose + two
  gates is the coherent alternative.
- **Human gates at intake and promotion:** Anthropic public-repo data (5/5 sampled agent-era
  PRs carry exactly one non-author human approval, 7–25 min turnarounds — a thin accountability
  click, with agents doing the reading; their shipped tooling cannot self-approve and never
  blocks unilaterally). William inverts the shape to fit a solo owner: thick-but-infrequent
  judgment at promotion instead of thin-per-PR clicks. **Scope correction (rev 2, owner
  ruling):** what promotion carries is the broader RELEASE judgment — copy, UX, feature
  acceptance, and change-management evidence. Security and SOC-2/data-handling review stays in
  the per-PR gate at full strength (§4.3); the original "compliance weight moves to promotion"
  framing overreached.
- **Verification as the load-bearing gate:** Cherny — verification "2-3x's" quality; the agent
  must RUN the thing, not just pass CI. Hence §4.3(2) and the four-layer QA stack.

## §7 — Relationship to autocode (what survives, what dies)

**Survives (port into the runner):** launch-delivery verification (the shim +
`state/started` handshake), activity/liveness detection and timeout tiers, the decision-log
discipline, the two-state failure taxonomy, William-only bright lines enforced by absence,
usage-limit fail-closed gates, L7's content-fingerprinted decision durability (generalized into
the known-failure ledger and label-recorded approvals).

**Dies:** the standing orchestrator and ORCHESTRATOR.md as a runtime document; §7 rotation and
all its machinery; the doorbell/ring/self-wake layers; the wave-checkpoint (its drift-correction
job disappears when each session reads current main instead of stale parallel kickoffs); the
batch plan.json/handoff-file apparatus (issues replace both); the conductor proposal's merge
train.

## §8 — Roadmap

- **V1:** the universal skill (issue conventions + issue-writing skill + loop runner + per-PR
  ship gate + morning report) and the QA stack, with the e2e suite built collaboratively with
  William FIRST. Separately: the eApp adaptation (its gates wired in, bright-line list,
  promotion checklist, branch-protection changes).
- **V2 (immediate):** triage/dedup loop (also the Slack bug-report bot → drafted issues, and a
  phone/voice-note → drafted-issue path); the tech-debt sweeper (adapted from Boris's
  post-merge-sweeper: harvests sub-blocking review findings into deduped, batched WEEKLY issues
  awaiting William's label — issues, never direct PRs, because all work must enter through Gate
  1); dependency/security loop built on Dependabot (already enabled on the eApp), its PRs
  passing the same e2e gate; the janitor (weekly stale-branch/worktree/aged-park proposals,
  one-touch approval — nothing auto-closed); the ratchet loop (§4.7).
- **V3 (longer term):** analytics + bug-monitoring stack (LogRocket-class: crashes, dead/rage
  clicks, drop-off anomalies) feeding INVESTIGATION issues — findings spawn scoped issues via
  the normal approval path, never direct-to-build.

## §9 — Open questions for context-rich reviewers

> **ANSWERED 2026-07-02** by the eApp planning session — verbatim in `EAPP-ANSWERS-2026-07-02.md`
> (binding execution pointers for the implementation plan). Headline: §4.4's conflict-ladder
> step 1 must be MERGE-based + ship.sh-driven on the eApp (never a rebase — force-push is a
> bright line); eApp deps are placed on its roadmap (Phase 9 = Gate 2; QA-EVAL + QA-SUITE).

1. **eApp reconciliation:** map this spec onto the eApp's existing ship.sh/cascade. What does
   the per-PR ship gate keep from the cascade (per the rev-2 ruling: all of its machine rigor,
   including security review), what OWNER-TOUCH friction lightens, and what breaks? (You know
   the cascade; this session doesn't.)
2. **Dev/prod reality:** what do the eApp's environments actually look like today, and what's
   the real work to stand up the promotion pipeline this spec assumes?
3. **Branch protection changes:** exactly which settings change on dev main, and is anything
   about the current protections load-bearing in a way this spec missed?
4. **The runner's home:** confirm the loop runner can reuse autocode's launch/liveness machinery
   as §7 assumes, and flag session-spawning constraints (cmux, permissions, keep-alive).
5. **Issue-writing skill:** design input — required fields, definition-of-done format,
   machine-checkable-DoD enforcement, eApp bright-line declarations on issues.
6. **What did we get wrong?** Given your context on the three runs and the eApp: any symptom
   this spec re-introduces, any duty it forgot the orchestrator was quietly doing, any
   1→1.2-era assumption that's false.
