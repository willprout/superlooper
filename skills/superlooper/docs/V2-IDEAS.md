# V2 idea ledger (running; owner-curated)

Captured from working conversations so they survive session context. Not commitments —
candidates for whenever V2 planning opens (`founding/SPEC-2026-07-02-issue-loop-workflow.md` §8
is the baseline roadmap; the research catalog in `RESEARCH-2026-07-03-v2-v3-loop-catalog.md`
maps external tools).

*Recreated in the monorepo 2026-07-09 from the planning machine's ledger, reconciled against
what this repo had already built (Codex runner lane; investigation anti-affinity exemption,
PR #4).*

## Packaging / distribution (2026-07-08 skills-audit thread)

- **Plugin restructure.** Split the installed skill into a superlooper PLUGIN of 2–3 skills:
  `superlooper` (ops/router), `write-issue` (the issue-writing front-end, promoted from
  references/issue-writing.md so any session discovers it in the skill list and invoking it
  loads the full discipline — today form is enforced mechanically at parse time but quality
  rides on an agent following a router pointer), maybe `adopt`. Socket for adapting the
  feature-dev plugin's explore/clarify phases as the front half of `write-issue`.
- **Stack doctor + STACK.md.** The loop's success depends on machine-level blocks outside the
  package: /cross-review + authenticated Codex CLI (audit-proven highest-value external
  block; the brief only says "fresh-agent review" — vendor choice is ambient), cmux +
  same-workspace rule, subscription claude login, gh auth (one login = one shared hourly API
  budget — the 2026-07-08 rate-limit incident), notify channel, launch shim sourced. Write
  docs/STACK.md (two tiers: loop user vs orchestrator — /brief belongs to the orchestrator
  tier) and extend the doctor pattern to a machine-level `doctor --stack` check.
- **`adopt` seeds a starter CLAUDE.md block** into target repos carrying the loop-critical
  human rules that today live only in William's global CLAUDE.md / orchestrator kickoff
  habits: approval is the owner's word; read the park memo before re-approving; reviewer
  independence + naming known defect classes in review prompts; money gates; never work in
  the loop's checkout.

## Prompt architecture (2026-07-08 brief-anatomy thread)

- **Contract-in-system-prompt experiment.** Move the mechanical loop contract (brief footer)
  to an appended system prompt at launch (`start-session.sh` owns this — inside the agent
  boundary) and deliver the issue body as the ENTIRE user message. Gives machinery-first
  ordering by construction plus channel separation (rules in the instruction channel, task in
  the task channel). Do NOT do this without a failure signature or a corpus large enough to
  measure: current evidence shows no degradation (11/12 first-attempt landings, zero
  malformed reports across 36 audited transcripts). The tell to watch for: malformed/missing
  report sections late in long sessions.
- Note (settled, recorded to avoid relitigating): the brief is typed as a plain first user
  message — slash-command argument semantics/limits do not apply to brief delivery.
- **`/goal` as a report-contract enforcer (2026-07-08).** `/goal` is a Claude Code BUILT-IN
  (harness-level, present in every session on every machine, no install): it sets a
  session-scoped Stop hook — the session cannot end its turn until the stated condition
  holds. That is mechanically exactly the loop's "the report is your LAST action" duty. The
  launch path could set `/goal <report path> exists with the required H2 sections` when
  starting a worker, making finish-without-report impossible at the harness level instead of
  detected-and-nudged after the fact. IMPORTANT boundary: /goal is Claude-specific, so this
  belongs ONLY in the Claude launch path (the agent boundary), never in the brief/footer
  text, which must stay agent-agnostic. This boundary is now LIVE, not theoretical: the
  Codex runner lane merged in this repo 2026-07-08→09 (launch path, hooks, pane-state
  classification, per-repo agent selection), so Codex workers receive the same briefs. Worth
  prototyping when a worker next finishes without a report (the failure this would delete).

## Ready engine fixes (kickoff-ready now; owner sequences when)

- **Held-territory window — SHIPPED.** Built as issue #6 / PR #14 (merged 2026-07-10), live on
  the machine since the 14:42 republish + bounce. The interim blocked-by chaining rule is
  retired (incident doc §4). History: `INCIDENT-2026-07-09-held-territory-window.md` (sibling).

## Parked with preconditions (see the incident docs for detail)

- **Park-notify-storm engine guards — UNBLOCKED 2026-07-10, scope narrowed.** Both owner
  preconditions are met: the census (issue #8) confirmed the 5,000-point hourly GraphQL budget
  suffices (~1,333/hr steady, dashboard pollers dominant, reset ~:36). PARTIALLY BUILT since:
  the hardening wave's #21 / PR #49 (merged 2026-07-10) gave `issue_comments`/`pr_comments` a
  refused≠empty read contract, HOLD-on-unreadable for the investigate gate with bounded
  journaling, and self-reconciling mis-parks. REMAINING unfiled scope: (a) refused≠empty for
  `pr_for_branch` — the actual 41-text storm path ("finished but no PR exists" in a quota dead
  zone) with a HOLD posture for build gates; (b) the notify-once per (issue, park-cause) guard
  in the park path itself. Any issue drafted from the incident doc must reconcile against
  PR #49 first or it re-specifies finished work; adjacent fences: #27 (merge-refusal cap),
  #24 (launch-side faults). `INCIDENT-2026-07-08-park-notify-storm.md` (sibling) carries the
  original analysis.
- **Dogfooding.** Superlooper running its own fix loop — now real: this monorepo is adopted
  and has merged loop-built PRs. Standing candidate issue: dedup the close mechanics between
  runner.py `_close_stale_session` and tidy's `_close_window` (deferred from the tidy session
  while runner.py was owned by a parallel lane).

## Migration follow-ups (2026-07-08 monorepo cross-review; each is a deliberate accept or a known follow-up)

- **Second, un-gated publish door (accepted by William).** The engine's own
  `skills/superlooper/bin/install.sh` publishes the same payload to the same `~/.claude` as
  the gated root `bin/install.sh`, but with no gate — and running it resets the gated one's
  VERSION baseline. Left deliberately (engine byte-for-byte untouched at migration); root
  installer documented as canonical. To harden later: neuter/redirect the nested script +
  update `skills/superlooper/tests/test_install.py`.
- **Fence flags, doesn't hard-stop, for config/CI paths.** The gate only flags an out-of-lane
  wander (journal + morning report); it does not block the merge — and unlike the engine
  (inert until republish), `.superlooper/**` / `.github/workflows/**` changes are live
  immediately once merged. Candidate engine enhancement: park/hold a PR whose diff reaches an
  undeclared or bright-lined area.
- **Shareability guard ineffective (loop-fixable dashboard bug).**
  `dashboard/tests/test_no_absolute_paths.py` checks for the wrong account string, so a real
  home-path leak would pass. Zero live leaks confirmed at migration time; the guard is a
  no-op. Filed as a loop issue 2026-07-09 (issue #7).
- **`will-titan` left in ~22 tracked files on purpose.** Old GitHub org name in dashboard
  test fixtures + sample data. Harmless (not a credential; tests self-contained). Left
  intentionally — bulk-renaming risks breaking slug-dependent assertions (airline colors,
  state-home path derivation). Don't rename without re-running the suite.
