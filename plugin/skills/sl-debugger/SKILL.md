---
name: sl-debugger
description: Live diagnosis and repair of a broken superlooper + command-center instance on this machine. Use when the loop or dashboard misbehaves — a wedged tick, a storm of parks or notify texts, a stuck label, a frozen or silent queue, a runner that looks alive but does nothing, a dashboard painting everything dead — or when asked for a health check of a running loop. Routes to a read-only health readout, documented failure classes with repairs, a safest-first repair ladder, and the unattended-invocation contract for the mechanical watchdog.
---

# sl-debugger — diagnose and repair a live loop instance

The patient is a running superlooper instance: a deterministic **runner** (one foreground
process in a cmux tab, tick loop ~15s) driving GitHub-issue work through worker sessions,
its **state home** at `~/.superlooper/<owner>__<name>/` (journal, per-issue state, liveness
markers, heartbeat), and optionally the **command-center dashboard** on `127.0.0.1`. Where
truth lives: the state home; the engine source (`skills/superlooper/skill/` in the
superlooper repo) — the installed copy at `~/.claude/skills/superlooper/` is what actually
runs (they drift; the `VERSION` file arbitrates); the incident corpus in
`skills/superlooper/docs/`; and the superlooper skill's runner-ops reference — the plugin
sibling `../superlooper/references/runner-ops.md` (this skill and the ops skill ship as
siblings in the installed plugin, so the relative path resolves inside the plugin cache;
runner-ops moved out of the engine's installed `references/` into the plugin) — for how the
loop is *meant* to be operated.

**Diagnosis before repair, always.** The readout is read-only; run it fully before mutating
anything. Most documented incidents needed rung 1–2 repairs or none at all.

## Router — open the one reference the moment needs

| Situation | Read | For |
|---|---|---|
| any symptom, or "how is the loop doing?" — **always start here** | `references/health-readout.md` | the read-only forensics pass: process-vs-progress, heartbeat/ALERT, journal recipes, lanes/queue/territory, freeze, usage meter, dashboard probe, publish drift, the healthy-instance checklist |
| the readout matches something seen before | `references/failure-classes.md` | the documented incident classes — wedged tick (2026-07-07), park/notify storm (2026-07-08), held-territory regeneration (2026-07-09), mis-parked investigation (2026-07-10) — each as signature → diagnosis → repair, plus a differential for undocumented symptoms |
| about to change ANYTHING | `references/repair-ladder.md` | the safest-first ladder: read-only → reversible → owner-confirmed state surgery, the surgery protocol, and what no rung ever touches |
| launched by the #66 watchdog, not a person (or unsure) | `references/unattended-contract.md` | authority tiers (`diagnose-only` / `allowlist` / `full`), the absolute exclusions, once-per-incident discipline, the memo + notify every run ends with |

References load **on demand** — open only what the current moment needs, not all four at
startup.

## The rails (the constitution — every mode, every rung)

This skill inherits the loop's bright lines without exception. Whatever the diagnosis:

- **Never apply `agent-ready`** or any approval-recording label — approval is the owner's
  word alone; your best output for a parked issue is the memo that makes his re-approval
  one touch.
- **Never force-push, never merge or close a PR by hand, never edit frozen issue text**
  (an approved Goal/DoD is owner-signed and immutable).
- **Never modify `.superlooper/**`** (the loop's executable config — the referee's own
  rulebook) **or `.github/workflows/**`** (the referee itself).
- **Never kill a process by name/pattern** — never `pkill -f`, never `killall`
  (the 2026-07-07 collateral kill of the owner's live dashboard is the standing lesson).
  A PID you positively identified, or nothing.
- **Prefer the engine's own mechanical verbs** — `doctor`, `status`, `tidy`/`janitor`
  dry-runs, the runner's own reconciliation and re-approval flows — over hand-editing
  state. Hand-edits compete with a 15-second tick loop.
- **State surgery only on the human's explicit go** for the specific edit, runner stopped,
  backup taken, and every action journaled (one bounded `act: "sl-debugger"` line in the
  state home's `journal.jsonl`). In unattended mode the standing authority tier is that go
  — within the exclusions above, which no tier ever unlocks (see
  `references/unattended-contract.md`).

Human-present is the default mode: the human's word in conversation is live authority for
reversible steps and surgery go/no-go. The unattended contract exists solely for the
mechanical watchdog's launches (issue #66) and is stricter, never looser.
