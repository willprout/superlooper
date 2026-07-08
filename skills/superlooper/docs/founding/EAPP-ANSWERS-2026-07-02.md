# eApp answers to SPEC §9 — from the eApp planning/orchestration session, 2026-07-02

What this is: the eApp orchestrator's verbatim response to the loop-adoption handoff — its
answers to SPEC-2026-07-02-issue-loop-workflow.md §9 plus the eApp-specific facts the loop
runner must design around. These are EXECUTION POINTERS binding on the implementation plan,
not spec changes. Sharpest item: §4.4's conflict-ladder step 1 ("mechanical rebase if clean")
must be MERGE-BASED + ship.sh-driven on the eApp (point 2 below) — force-push is a bright line
and only ship.sh may post the review gate.

---

From the eApp planning/orchestration session → the loop-building session (2026-07-02). Answers
to your SPEC §9 open questions + the eApp-specific facts your runner has to design around. Not
a re-brief of your spec — just what changes your build.

1. Placement is done — the eApp deps are now OWNED, you don't chase them. Items 1 (dev/prod +
promotion) and 2 (QA suite) are now phases on private/ROADMAP.md: promotion pipeline = Phase 9
reshaped as your Gate 2; QA split into QA-EVAL (William judges the existing Playwright/axe
harness himself) + QA-SUITE (journey library + nightly + quarantine). 3a/3b/3c carried as
flagged owner-decisions. So the eApp infra your skill assumes is eApp phase work — build the
universal skill against the contract; the eApp delivers the environments.

2. THE load-bearing gotcha for your loose-merge model (§9.1/§9.3) — your §4.4 rebase ladder
DEAD-ENDS on the eApp unless wired this way. The per-PR gate is scripts/ship.sh posting a
diff-pinned review/local-gate status, pinned to git diff origin/main...HEAD. Consequences your
runner must handle:
- A rebase/merge-update changes the base → the gate is stripped → it must be re-posted, and
  only ship.sh may post it (posting the status by hand is a bright line). ship.sh reuses the
  verdict for an identical diff (no model, fast) — so re-running it after an update is cheap.
- A rebase rewrites history → plain git push is rejected (non-fast-forward), and force-push is
  a hard bright line (no --force-with-lease mode exists — that's an engine change,
  metered-gated). The autocode run proved the workaround: update via a MERGE not a rebase —
  git merge origin/main, or git merge -s ours <old-origin-tip> to preserve already-done rebase
  work — then re-run ship.sh (plain push fast-forwards). Your "rebase-if-clean → re-run CI →
  merge" step must be merge-based + ship.sh-driven on the eApp, or it can't land.
- The cascade's jury/fix/verify run as Task subagents inside the shipping session (they can't
  run from ship.sh's bash) — which fits your "one fresh session per issue" model perfectly.
  The session runs its own cascade, then ships via ship.sh.

3. Fail-closed PARK cases your runner must NOT fight (not away-default around):
- Cascade-engine-path changes → ship.sh fails the local gate closed and routes to metered CI.
  The runner can't auto-merge these; it's a park.
- Any P0 / owner-finding → --human-approved is William-only and agent-guard-forbidden. Park +
  ping; never coach around the guard. (This is exactly what stalled sub-1 all night last run —
  the away-default genuinely can't manufacture it.)
- Ship EXCLUSIVELY via ship.sh — never direct git push / gh pr create / hand-posted status.

4. Gate-parity requirements — don't re-import the environment-parity miss (§9.6): two concrete
gate rules, both from real eApp failures:
- Run migrations in the gate as a Render-parity NON-superuser role. CI Postgres runs as
  superuser, which masked a migration class (SET ROLE/42501) that broke every Render deploy
  (FIX-0010).
- Your §4.3(2) real-browser drive must exercise the sensitive paths (SSN/bank). The entire SUB
  wave existed because E2E withheld the SSN and "transport live" got mistaken for "payload
  complete." The browser gate is the right mechanism — just make sure it hits the
  RESTRICTED-data paths, not the happy path around them.

5. Runner-home constraints (§9.4) — autocode's machinery ports, with these cmux realities:
launch-delivery verification + activity/liveness + timeout tiers WORK (3 runs + the 2026-07-02
bridge patch); the no-standing-LLM-seat design kills the symptoms that lived in that seat
(rotation defiance, idle churn, paid polling — the mechanical floor ran clean). But: bg Bash
watches get SIGTERM'd ~every 30 min (don't rely on a durable background watch — poll on wake /
gh-poll instead); cmux read-screen rejects --workspace (send/send-key only); nudge-pane needs
RUN_ROOT exported.

6. Dev/prod reality today (§9.2): ONE environment — staging, auto-deploying from main on Render
(long-running container, not serverless; Render-managed Postgres, private-networked). No prod,
no promotion step exists. Your Gate 2 assumes infra that's being built, not present. Current
main protection (per last run; verify at change-time): strict=true, required checks =
review/local-gate + quality-gate, required human PR reviews = 0. The one property to preserve
when you drop strict: review/local-gate stays a required, diff-pinned check on dev.

7. The one duty the standing orchestrator quietly did that your issues must absorb: owning
cross-PR contract promises. Our #1 systemic miss was that every payload gap
(bank-two-ciphertext, SSN client gate, beneficiary mapping) was known somewhere — a code
comment, a PR body, session memory — and owned in no durable queue item. Your thin-issue +
blocked-by + touches: + the V2 tech-debt sweeper cover this only if the issue-writing skill
records cross-PR constraints as issues, never as code comments. That's the highest-leverage
thing to bake into the issue-writer.
