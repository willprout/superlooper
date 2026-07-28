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

This skill inherits the loop's bright lines without exception. **The governing split —
William's 2026-07-16 ruling, ledger D13:**

- **Supervised (William present and directing).** His direct instruction in the conversation
  is live authority, full stop. If he tells you to hand-merge a PR, hand-merge it; if he
  directs a state surgery, do it (still stop the runner and journal — see below). The
  merge/close/surgery prohibitions in this skill bind you when you are acting on your OWN
  judgment; they are never a reason to refuse his direct, present instruction. The lines under
  "Absolute at every rung" below are the sole exception — they are not recovery actions an
  agent performs on anyone's word.
- **Unattended (the #66 watchdog's launches, or any session unsure a human is present).** You
  never hand-merge, never self-merge, never close a PR — no standing authority tier unlocks
  these. When you cannot tell, behave as unattended; the stricter half is always safe. The
  full unattended contract is `references/unattended-contract.md`.

**Absolute at every rung, with the human present or not** — these are the owner's own signals,
machinery that deliberately does not exist, or a standing safety rail; none of them is a
recovery action an agent performs, so no supervised instruction releases them:

- **Never apply `agent-ready`** or any approval-recording label — approval is William's word,
  and he applies it himself (the label, or a dashboard approve tap). Present or not, the agent
  never applies it; your best output for a parked issue is the memo that makes his re-approval
  one touch. Likewise **never edit frozen issue text** — an approved Goal/DoD is William-signed;
  he edits his own issue, you never rewrite it for him.
- **Never force-push.** The loop's repos take merge-based updates only, and the constitution
  builds no `--force`/`--force-with-lease` path at all — a force-push is not a supervised
  recovery verb, it is machinery that does not exist.
- **Never kill a process by name/pattern** — never `pkill -f`, never `killall`
  (the 2026-07-07 collateral kill of the owner's live dashboard is the standing lesson).
  A PID you positively identified, or nothing.
- **Never modify `.superlooper/**`** (the loop's executable config — the referee's own
  rulebook) **or `.github/workflows/**`** (the referee itself). A live-incident seat never
  reprograms its own referee; that change comes through a normal supervised dev session, not
  this skill.
- **Never edit the installed engine tree in place.** The installed engine
  (`~/.claude/skills/superlooper`) is **read-only for every session, including a supervised
  debugger**: it is a disposable copy the gated `bin/install.sh` republish overwrites
  wholesale, so an in-place edit is silently lost on the next publish (the 2026-07-15
  `cb161ef` incident). If you find an engine bug, **open a GitHub issue on the engine's source
  repository** — the repo this installation was published from (a fork points at its own fork;
  do not assume a slug). An emergency fix with William present is still allowed — it just goes
  into the engine REPO and reaches this machine only through the gated `bin/install.sh`
  republish (which shows the diff and asks for an explicit OK), never an edit to the installed
  tree.

**These follow the split** — bound when you act on your own judgment or unattended, done on
William's direct word when he is present:

- **State surgery only on the human's explicit go** for the specific edit, runner stopped,
  backup taken, and every action journaled (one bounded `act: "sl-debugger"` line in the
  state home's `journal.jsonl`). In unattended mode the standing authority tier is that go
  — within the absolute lines above, which no tier ever unlocks (see
  `references/unattended-contract.md`).

Whatever the mode, **prefer the engine's own mechanical verbs** — `doctor`, `status`,
`tidy`/`janitor` dry-runs, the runner's own reconciliation and re-approval flows — over
hand-editing state; hand-edits compete with a 15-second tick loop.

Human-present (supervised) is the default mode: the human's word in conversation is live
authority for reversible steps, hand-merges he directs, and surgery go/no-go. The unattended
contract exists solely for the mechanical watchdog's launches (issue #66) and is stricter,
never looser.
