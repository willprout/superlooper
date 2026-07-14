# sl-debugger — moved into the plugin

The sl-debugger skill now lives in the superlooper plugin at
**`plugin/skills/sl-debugger/`** (`SKILL.md` + four `references/`). This directory is a
tombstone: it keeps the provenance record, and the skill payload moved out of it.

## The manual install is superseded

Issue #64 shipped this skill under `skills/sl-debugger/skill/` with **no publish path** — its
install was a manual `cp -R` into `~/.claude/skills/sl-debugger`, and it explicitly deferred
packaging to the #65 plugin restructure. That restructure (issue #87, design §6.5) `git mv`d
the payload into the plugin, so the manual copy is **superseded**: the skill now installs the
way every other superlooper skill does — through the plugin (marketplace add → plugin install;
see `plugin/` and the `adopt` skill). There is no separate copy step, and no half-finished
checkout can leak into a live session, because the plugin is pure content that reaches a
machine only through its own install path — never a symlink into `~/.claude`.

The content-lint tests that pin this skill's load-bearing properties moved with it, into the
engine suite the CI `tests` check actually runs:
`skills/superlooper/tests/test_sl_debugger_skill.py`.

## Provenance

Authored under issue #64 (approved 2026-07-10, amended 2026-07-11 to add the
unattended-invocation contract for the #66 watchdog). Moved into the plugin under issue #87
(child of #65). Incident corpus: `skills/superlooper/docs/INCIDENT-*.md` plus the issue #21 /
PR #49 record of the 2026-07-10 mis-parked-investigation incident.
