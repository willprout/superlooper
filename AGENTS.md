# AGENTS.md instructions for /Users/willprout/Projects/superlooper

## Collaboration

- If the work is to run commands that Codex can run directly, Codex should run them instead of asking William to run them. This includes Git, test, CLI, and smoke-test commands, as long as doing so is reasonable in the current context.
- If running the commands would consume too much context or would be cleaner in a separate environment, Codex should suggest or start a fresh session rather than handing manual command execution back to William.

## graphify

- **graphify** (`~/.Codex/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
- When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

## superpowers

- **superpowers** (`/Users/willprout/.Codex/plugins/cache/Codex-plugins-official/superpowers/5.0.7`) - use the skills in here when user mentions using superpowers to do a task.
