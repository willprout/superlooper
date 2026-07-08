# Founding documents

These four documents are the constitution of superlooper. They were copied verbatim on
2026-07-02 from the autocode skill repo (`~/.claude/skills/autocode/`), which is FROZEN —
we port from it, we never modify it.

| File | Source | Role here |
|---|---|---|
| `SPEC-2026-07-02-issue-loop-workflow.md` | `~/.claude/skills/autocode/SPEC-2026-07-02-issue-loop-workflow.md` | **The constitution.** The settled v1 direction (rev 2 + 2.1). §2 lists William's explicit directives — fixed points, never relitigated. |
| `EAPP-ANSWERS-2026-07-02.md` | `~/.claude/skills/autocode/EAPP-ANSWERS-2026-07-02.md` | Binding execution pointers from the eApp planning session — answers to the spec's §9 open questions. Sharpest: the conflict-ladder step 1 must be MERGE-based + ship.sh-driven on the eApp (force-push is a bright line). |
| `AUDIT-2026-07-02-first-hardened-run.md` | `~/.claude/skills/autocode/AUDIT-2026-07-02-first-hardened-run.md` | The evidence for the architecture: across three real runs every judgment-fired LLM duty eventually failed to fire; only mechanical duties fired. This is WHY the runner is deterministic. |
| `EVENT-MODEL.md` | `~/.claude/skills/autocode/EVENT-MODEL.md` | The proven signal/event contract (delivery proof, liveness tiers, safe pane writes) the runner ports its sensing layer from. |

If a copy here ever disagrees with its source file, the source (autocode repo, frozen as of
2026-07-02) wins for provenance questions; this repo's PLAN and code win for what superlooper
actually builds.
