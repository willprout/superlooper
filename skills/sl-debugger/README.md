# sl-debugger — live diagnosis and repair of a broken loop instance

When a superlooper + command-center instance misbehaves on a machine — a wedged tick, a storm
of parks, a stuck label, a frozen queue nobody understands — this skill is the owner's recourse
beyond tribal knowledge. A person invokes it in a live Claude Code session (or, under issue
#66's mechanical watchdog, it is launched unattended within a strict authority contract) to
diagnose and, where authorized, repair the running instance.

The skill encodes how the whole system works operationally: what a healthy instance looks
like, the documented failure classes and their signatures (this repo's incident docs are the
corpus), and repair procedures ordered safest-first — read-only forensics, then reversible
steps, then owner-confirmed state surgery. It inherits the constitution: it never applies
`agent-ready`, never force-pushes, never edits frozen issue text, never kills processes by
name/pattern, and prefers the engine's own mechanical verbs (`doctor`, `status`, the runner's
own reconciliation) over hand-editing state.

## Layout

- `skill/` — the publishable payload: `SKILL.md` (router) + `references/` (loaded on demand).
- `tests/` — content-lint tests pinning the properties issue #64's DoD makes load-bearing.
  Run them from this directory: `python3 -m pytest tests/`. They are deliberately outside the
  engine suite (`skills/superlooper/tests`), which this skill never touches.

## Installing on a machine today (manual, human-executed)

Source never lives in `~/.claude` — installing is an explicit copy (never a symlink, so
half-finished edits in a checkout can't leak into live sessions):

```bash
rm -rf ~/.claude/skills/sl-debugger && cp -R skills/sl-debugger/skill ~/.claude/skills/sl-debugger
```

Run it from the repo root. Re-run it to update. That is the whole install; distribution /
packaging (plugin, marketplace) is deliberately out of scope here — the plugin restructure
(issue #65) absorbs it later.

## Provenance

Authored under issue #64 (approved 2026-07-10, amended 2026-07-11 to add the
unattended-invocation contract for the #66 watchdog). Incident corpus:
`skills/superlooper/docs/INCIDENT-*.md` plus the issue #21 / PR #49 record of the 2026-07-10
mis-parked-investigation incident.
