# AUDIT 2026-07-02 — run-20260701-1750 (first run on the hardened machinery)

Audit of the first live run after the 2026-07-01 reliability hardening
(PLAN-2026-07-01-reliability-checkpoint.md), plus the owner debrief of 2026-07-02.
This doc LOCKS the agreed fixes, catalogs the observed symptoms, and records the
root-cause question that was handed to a fresh-eyes first-principles review
(commissioned 2026-07-02 in the owner's audit session).

## Run outcome (context)

7-PR eApp batch (sub-1..5, cat-1a/1b), checkpoints ON, all strong_gates.
- 4 PRs merged autonomously overnight (#85 sub-2, #86 sub-3, #87 cat-1a, #88 cat-1b),
  every one through a green gate. cat-1b's cascade found and auto-fixed a real P0.
- sub-1 parked correctly on a genuinely owner-only decision (C9 ever-P0); merged
  next morning (#89, 09:28) after the owner ran the override — three times (S2).
- sub-4/sub-5 held correctly behind sub-1; launching as the audit closed.
- No bad merges, no data loss, no false DONE, watcher uptime 100%, Mac never slept.
- Owner verdict on the experience: "messy." The mechanical floor worked; ALL the
  friction came from the judgment layer above it.

## Symptoms observed (evidence: the run dir `~/autocode-runs/eapp/run-20260701-1750/`
— decisions.md, watcher.log, run.json, state/checkpoints/ — and the owner's account)

- **S1 — rotation deferred by judgment, again (3rd run in a row).** The watcher's new
  rotation_due event (20:50) and rotation_overdue alarm (21:36) both fired correctly,
  but the orchestrator read the non-discretionary rule, QUOTED it, and wrote a
  reasoned deferral anyway; it rotated ~23:11 only after the owner told it twice.
  Instructions were read and consciously overridden — the same failure the hardening
  targeted, now visible instead of silent.
- **S2 — the owner ran the same override command 3 times for one decision.**
  (1) His first run was invalidated by a foreseeable merge-order race (cat-1b merged
  right after, branch went BEHIND under strict protection, diff-pinned gate stripped).
  (2) A ~3h "Option B" detour was structurally doomed from the start — the cascade's
  ever-P0 rule means C9 could NEVER self-clear, knowable from the repo's own docs —
  and ended back at "please run the same command." (3) A morning API cert error ate
  one more attempt. One decision ≈ three keystroke sessions + a chat approval.
- **S3 — the ad-hoc Fable escalation never fired.** §5a's bright-line triggers
  (deviating from the written plan, calls reaching beyond one PR) matched at least 3
  moments (the rotation deferral, the Option A→B flip, the merge-train strategy
  improvisation) and fired 0 times; §5a expects ~1–2/run. The SCHEDULED checkpoints
  did run (3 × Fable) and added real value (rewrote sub-4's stale baseline via 5
  amendments; 4 amendments folded into cat-1b's handoff pre-launch). Same disease as
  S1: judgment-fired duties don't fire.
- **S4 — orchestrator churn while idle.** 3 fresh Opus successors (02:14, 05:36) plus
  1 aborted spin-up (08:15) during a night where every wake was "gh-poll: unchanged."
  gen-2 rotated at ~16% context purely because the 3h wall clock expired. Wall-clock
  rotation is the wrong trigger on an idle run.
- **S5 — LLM turns as a polling loop.** Background ship-watch processes are SIGTERM'd
  by the harness ~every 30 min (twice, then abandoned), so the orchestrator polled
  GitHub itself on 30-min self-wakes all night — paid model turns doing a job a free
  deterministic process should own.
- **S6 — run.json corrupted to `[null,null]`.** A state.update mutate lambda returned
  a tuple and state.update persisted it verbatim. Hand-reconstructed from context;
  lesson propagated through successor seeds and correctly applied twice after. The
  watcher survived (6 caught per-tick errors, no crash).
- **S7 — alarm re-arm anomaly.** rotation_overdue fired once (21:36) and never
  re-armed during the 96-min overdue window; EVENT-MODEL promises ~30-min re-arms.
  Suspected: any single not-due tick resets both the overdue clock and the re-arm
  state (watcher.py rotation block), and nothing logs the flap. Unproven from disk.
- **S8 — rotation baseline stamped before the successor is verified alive.** The
  aborted gen-4 rotation left a window (baseline reset, no brain seeded); recovered
  by a correct reclaim, but the ordering violates the "stamp only after verified
  delivery" honesty rule the launch path already follows.
- **S9 — small stuff.** `decisions: 0` dead counter in run.json (next to a 43KB
  decisions.md). The `wave-checkpoint` subagent type isn't usable by the session that
  installed it (agent defs load at session start) — worked around with a
  general-purpose Fable agent. usage.json `stale` flag after the network incident
  (handled fine). Two checkpoints were orchestrator-written rather than Fable
  (cat-1b: no dependents; sub-1: no report existed) — defensible, undocumented.

## LOCKED fixes (agreed 2026-07-02; build regardless of the architecture outcome)

- **L1 [autocode]** `state.update` validates the mutate result (dict containing
  `prs`) before persisting; reject otherwise; unit test the tuple-lambda case. (S6)
- **L2 [autocode]** Alarm-flap robustness: a single not-due tick must not reset the
  rotation overdue clock / re-arm state; log a `rotation_flap` event; test re-arm
  behavior across a long overdue window. (S7)
- **L3 [autocode]** Stamp the rotation baseline only AFTER the successor is
  delivery-verified (mirror launch-pr.sh's honesty rule). (S8)
- **L4 [autocode]** Delete or mechanically stamp the `decisions` counter. (S9)
- **L5 [autocode docs]** Ratify the checkpoint adaptations in §5a (last-PR/no-dependent
  and no-report cases may be orchestrator-written with an honest degradation note;
  parked-mid-owner-decision defers to resolution) + the subagent_type install caveat. (S9)
- **L6 [eApp, own PR]** Guard heredoc false-positives — pre-packaged as Appendix A of
  PLAN-2026-07-01-reliability-checkpoint.md. (Run-1 fact-3 tail)
- **L7 [eApp review engine, metered-CI path]** Owner-approval durability: an
  owner-accepted finding (e.g. C9) survives rebases — one approval, one keystroke,
  ever. (S2 root fix)
- **L8 [protocol]** Quiesce-before-ask: never hand the owner a command to run while a
  pending merge can invalidate it; sequence owner keystrokes after the train settles. (S2)

## HELD for the fresh-eyes outcome (agreed in direction; not built, because the
architecture they'd bolt onto is itself under review)

- **H1** Dormant-when-blocked-on-owner: no polling turns; the owner's chat message or
  a watcher-owned GitHub poll → doorbell are the wake channels. (S4, S5)
- **H2** Context-stamped rotation: orchestrator stamps its context % each wake; rotate
  in a ~25–50% band (compaction is the real cliff, not a fixed number); rotate-then-
  launch, never drain-then-rotate; if the owner is actively in-conversation, ask
  "now or after?" (one-line owner touch) — otherwise no deferral. (S1, S4)
- **H3** Fable-before-William: any ask/park that reaches the owner triggers a
  checkpoint FIRST (plus the deviation/reversal triggers); guard-rail: a checkpoint
  may reshape/re-time/replace an ask but never converts an owner-only action into an
  autonomous one. (S2, S3)
- **H4** The thin-executor hypothesis (owner, 2026-07-02): an away-run whose kickoffs
  are already written may need a far smaller duty set than a live planning
  orchestrator — the protocol overload itself may be why instructions lose to
  judgment under load. (S1, S3 root-cause candidate)

## The root-cause question (why fresh-eyes was commissioned)

Pattern across all three runs: every failure lives in the judgment layer; every fix
moves one more duty into machinery; the protocol grows; the next run finds the next
judgment hole. The owner's question: are we curing symptoms of an architectural
problem — is one long-lived orchestrator session juggling supervision, strategy,
merge mechanics, owner comms, and self-maintenance simply too much — and is there a
fundamentally better loop for pushing reviewed code at velocity without the owner in
the loop on every PR? A neutral fresh-eyes prompt was issued from the audit session;
its verdict decides whether H1–H4 get built here or superseded.
