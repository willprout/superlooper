# INCIDENT 2026-07-09 — declared territory unprotected between session-finish and merge

**For a fix session in THIS repo.** Found live on the agent-360-eapp loop (the strictest-configured
repo: `affinity: hard`, `touches_required: true`, honest declarations on both issues). A 77-minute
finished build was invalidated by a conflicting merge the scheduler itself allowed. The metadata
discipline was perfect; the engine's protection window is wrong. NOT an orchestrator/issue-writing
failure — do not "fix" this with documentation.

*Recreated in the monorepo 2026-07-09 from the planning machine's original. All code locations
below re-verified against this repo at commit `41ff49c` — treat them as dated pointers and verify
against current `main` before building.*

## 1. What happened (journal: `~/.superlooper/will-titan__agent-360-eapp/journal.jsonl`, Jul 9)

- 17:06 — `launch i160`, `touches: [submission, insurance, data, docs]`. Builds 77 min.
- 18:23 — `gate i160` → 18:24 `hold i160` (finished; PR open; gate waiting — capture the exact hold
  reason from the journal/state during the fix).
- 18:24 — `launch i163`, `touches: [submission]` — **overlaps i160's declared territory, launched
  one tick after i160's hold**, on a hard-affinity repo.
- 18:53 — `gate i163` → `merge i163` → same minute, `update i160`: "real conflict — gate decides
  regenerate/preserve next pass" → 18:54 `regenerate i160` → 18:55 relaunch (`-r1`). The 77-min
  build is discarded; the ladder worked exactly as designed (no LLM, no force-push, branch
  preserved) — the cost was pure wall-clock, and it was avoidable.

## 2. Root cause (engine design gap — monorepo locations, verified 2026-07-09)

Anti-affinity protection ends when a session stops RUNNING, not when its PR merges:

- `skills/superlooper/skill/lib/actions.py:83` —
  `INFLIGHT_STATUSES = {"running", "blocked", "frozen", "exited"}`. Statuses `gating`/`holding`
  are NOT inflight.
- `actions.py` `lane_state_from` (:149) builds the occupied-lane set from INFLIGHT only — a
  lane (and its declared touches) vanish from the scheduler's view the instant the issue leaves
  "running".
- `skills/superlooper/skill/lib/scheduler.py` `launchable` (:89) checks candidates' touches
  against that lane set only. So a finished-but-unmerged issue's territory is invisible, and an
  overlapping candidate launches straight into the finish→merge window — which is precisely when
  a conflicting merge destroys the finished work.
- Precedent inside the codebase: the label-reconciliation pass (`actions.py:533`) already treats
  `gating`/`holding` as active alongside INFLIGHT — the engine recognizes these as live states
  everywhere except lane/territory arithmetic.

The window is normally seconds (finish→gate→merge same tick). It becomes minutes-to-hours exactly
when the gate must WAIT (CI pending, ship-recheck, mergeable UNKNOWN, review nudge) — i.e., on
well-gated repos like the eApp, the most protected repos have the widest exposure.

## 2b. Reconciliation with the investigation exemption (PR #4, merged 2026-07-09)

After this incident was written, the scheduler gained a type-aware anti-affinity exemption:
investigations (`type:investigate` — no PR, no merge) are exempt in both directions
(`_merge_affinity_subject`, `scheduler.py:77`; `lane_state_from` now threads each lane's `type`).
This COMPOSES cleanly with the fix below — it already establishes the principle that only
merge-producing issues participate in territory arithmetic:

- Held-until-merge territory claims apply only to merge-producing issues. Investigations neither
  hold territory nor are held by it — unchanged by this fix.
- The `type` plumbing PR #4 added to lane entries is the natural carrier for the
  parked/wildcard decision in §3.
- Regression duty: the PR #4 behavior (and its tests) must survive this fix intact.

## 3. The fix

Split "lane capacity" from "territory": a finished-but-unmerged issue consumes NO lane slot (its
worker is done; concurrency capacity is genuinely free) but its declared touches REMAIN claimed for
anti-affinity until the issue reaches a state where the claim is meaningless.

- Territory held while status is gating/holding (or any finished-with-open-PR wait state).
- Territory released on: merge, supersede/regenerate (the new generation re-claims at relaunch),
  and terminal parks (`parked`/`needs_william`) — DECIDE the park case deliberately and write it
  down: holding territory for a parked open PR protects the parked work from guaranteed conflict,
  but on a NO-touches repo every issue is the wildcard `*`, so a parked wildcard holding territory
  would freeze all launches until re-approval. Suggested resolution: hold territory only for
  DECLARED (non-wildcard) touches; wildcard territory releases at park. Keep it simple and tested
  either way.
- Free-lane arithmetic (`free = lanes - len(lane_state)`) keeps counting RUNNING only — this fix
  must not reduce build concurrency, only launch ORDER around claimed files.

## 4. Interim mitigation — RETIRED 2026-07-10

The fix shipped as issue #6 / PR #14 (merged 2026-07-10) and went LIVE with the 14:42 republish
+ runner bounce the same day (installed engine carries `TERRITORY_CLAIM_STATUSES`). The interim
blocked-by chaining rule for territory-overlapping issues is retired — territory is now held
until merge by the engine itself. (Historical text: overlapping issues were chained with
`blocked-by: #N` to span the unprotected finish→merge window.)

## 5. Definition of done for the fix session

- [ ] Scheduler test: candidate overlapping a GATING/HOLDING issue's declared touches is NOT
      launchable under hard affinity; becomes launchable the tick after that issue merges.
- [ ] Capacity test: a gating/holding issue does not consume a lane slot — a NON-overlapping
      candidate still launches into the free lane while it waits.
- [ ] Release tests: territory released on merge, on regenerate (old generation), and the
      documented park behavior (including the wildcard/no-touches repo case — no loop freeze).
- [ ] Investigation exemption intact: investigations still neither hold nor are held (PR #4
      tests green, both directions).
- [ ] Regression: the exact i160/i163 shape replayed against fake-gh — overlapping candidate held,
      finished build merges un-conflicted, zero regenerations.
- [ ] Full engine suite green (735 baseline in this repo, 2026-07-09 + new); Codex cross-review
      (free) verdict recorded, ≤2 rounds, prompt names the two proven defect classes (shared
      mutable defaults, fail-OPEN on wrong-typed input); republish/restart is the orchestrator's.
