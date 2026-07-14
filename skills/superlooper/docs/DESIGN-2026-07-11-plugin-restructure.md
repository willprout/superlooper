# Design: the superlooper plugin restructure (issue #65)

> **Provenance.** Durable record of the approved plugin-restructure design, committed by
> issue #82 — the first child of investigation #65. It reproduces, verbatim from here down,
> the investigation report posted to issue #65: the comment beginning
> `<!-- superlooper-investigation -->` (2026-07-11). Truth lives in that #65 report comment;
> this file is its committed copy. The report's separately-posted child index and its
> "investigation complete" closing note are not part of the report comment and are not
> reproduced here.

Investigation report, 2026-07-11. The three owner rulings of 2026-07-10 (five skills; the paste-URL sharing bar; updates keep the human gate) are frozen inputs — this design chooses mechanisms under them, never relitigates them. Plugin mechanics below were researched against the official Claude Code docs (code.claude.com/docs/en/plugins.md, plugins-reference.md, plugin-marketplaces.md, discover-plugins.md), not assumed, and cross-checked against a real installed plugin registry on this machine (`~/.claude/plugins/installed_plugins.json`, `known_marketplaces.json`).

## 0. Verified current state (what the design starts from)

- **#64 landed.** `skills/sl-debugger/` is on `origin/main` (commit d65fe03): `skill/SKILL.md` + 4 references + content-lint tests. It has **no publish path** — its README documents a manual `cp -R` and explicitly defers packaging to this issue.
- **The publish path to preserve.** Root `bin/install.sh` is the one gated publisher: it rsyncs `skills/superlooper/skill/` → `~/.claude/skills/superlooper/`, shows the engine diff since the last-published SHA (`$DEST/VERSION`), and refuses without an explicit OK. It also registers the two activity hooks (Claude + Codex settings), installs the launch shim, and links the `superlooper` CLI onto PATH. The nested `skills/superlooper/bin/install.sh` is already a tombstone (#11) — no second door exists and this design opens none.
- **The payload today mixes content and engine.** `skills/superlooper/skill/` = `SKILL.md` + `references/` (issue-writing, approval-protocol, runner-ops) **plus** `bin/` (runner.py, 11 scripts, the `superlooper` CLI), `lib/` (23 modules), `shell/`, `templates/`.
- **cross-review** is machine-local: `~/.claude/commands/cross-review.md` (a 140-line command that shells to `codex exec`) plus `~/.claude/hooks/suggest-cross-review.sh` (a PostToolUse hook that nudges toward review after plan/spec writes on superpowers-style paths).
- **The dashboard** is clone-and-run (stdlib Python, per-machine `config.json`, localhost-only); nothing of it installs to `~/.claude`.
- **The repo has no root README** (dropped at migration), and the loop config's `areas` map covers only `engine: skills/**` and `dashboard: dashboard/**`.

## 1. Plugin facts the design rests on (researched, with the load-bearing consequences)

1. A single GitHub repo can be **both a marketplace and its plugin**: `.claude-plugin/marketplace.json` at repo root, plugin entries with a relative `source: "./plugin"`. Users add it with `/plugin marketplace add willprout/superlooper` (or `claude plugin marketplace add …` non-interactively from inside a session) and install with `/plugin install superlooper@superlooper`. There is **no** direct install-from-git-URL without a marketplace — so the repo must be its own marketplace to meet the paste-URL bar.
2. Plugin skills live at `<plugin-root>/skills/<name>/SKILL.md` with supporting files beside them; they are invoked namespaced (`/superlooper:write-issue`). Installed plugins are **cache copies** (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`) — a plugin file cannot reference anything outside the plugin directory, so every reference a skill needs must ship inside `plugin/` (or point at a stable machine path the gated installer owns).
3. **Plugin hooks, MCP servers, `bin/` executables, and monitors all execute automatically once the plugin is enabled — with no further human gate — and ride updates.** Marketplace auto-update exists and can be on by default. This is the decisive fact for ruling 3: *nothing executable may ship in the plugin payload at all*. The gate must hold by construction, not by convention.
4. Version semantics: if `plugin.json` carries no `version`, every git commit is a new version; if it carries one, pushes without a bump do nothing for existing installs.
5. There is no install-time script execution (no postinstall) — a plugin install lands files only. That is exactly the property the human gate needs.

## 2. The design in one paragraph

The repo becomes its own marketplace with one plugin, `superlooper`, living at `plugin/` — a **pure-content payload**: five skills (`superlooper`, `write-issue`, `adopt`, `cross-review`, `sl-debugger`) as markdown, with zero hooks, zero `bin/`, zero MCP/monitors, enforced by a mechanical repo test. Skill content **moves** out of the gated engine payload into the plugin (moved, never copied — one home, no drift, no double-load). The engine (runner, lib, launch stack, CLI) stays exactly where it is: source at `skills/superlooper/skill/`, published only through the gated root `bin/install.sh` to `~/.claude/skills/superlooper/`, hooks registered by that installer pointing at the installed copy — never at a plugin path. Plugin updates therefore carry only prose; anything that executes still reaches a machine exclusively through the diff-showing, OK-requiring republish. The stranger's install is: marketplace add → plugin install (content lands) → the `adopt` skill walks them through clone + gated `./bin/install.sh` (the one human-gated step, by design) → `superlooper doctor --stack` names every missing machine block → `adopt`/`doctor`/`run`.

## 3. Repo layout: what moves where

```
.claude-plugin/marketplace.json          NEW   repo root = the marketplace
plugin/                                  NEW   the plugin (pure content; the ONLY thing plugin updates touch)
  .claude-plugin/plugin.json             NEW   name: superlooper (no version field — SHA versioning)
  skills/
    superlooper/SKILL.md                 MOVED from skills/superlooper/skill/SKILL.md (router, rewritten: routes to the 4 sibling skills)
    superlooper/references/approval-protocol.md   MOVED from skills/superlooper/skill/references/
    superlooper/references/runner-ops.md          MOVED from skills/superlooper/skill/references/
    write-issue/SKILL.md                 PROMOTED from skills/superlooper/skill/references/issue-writing.md
    adopt/SKILL.md                       NEW   bootstrap walkthrough + routes to the published ADOPTING.md
    cross-review/SKILL.md                ABSORBED from ~/.claude/commands/cross-review.md (adapted; see §6.4)
    sl-debugger/SKILL.md + references/   MOVED from skills/sl-debugger/skill/ (4 references travel with it)
skills/superlooper/skill/                ENGINE ONLY after the move: bin/, lib/, shell/, templates/
                                         (+ docs/ADOPTING.md joins the payload — see §6.3)
skills/superlooper/docs/, tests/         unchanged homes (design record lands in docs/)
bin/install.sh                           UNCHANGED role; payload contents shrink to engine-only
dashboard/                               unchanged — stays clone-and-run, deliberately outside the plugin
README.md                                NEW   root README leading with the paste-URL install flow
```

Moved means `git mv` — the installed `~/.claude/skills/superlooper/` loses `SKILL.md` + `references/` at the next gated republish (rsync `--delete`), becoming a pure engine home. That prevents the same skill loading twice (once from the plugin, once from the skills dir). The installed dir keeps its exact path, so **nothing else changes**: the two hooks in settings.json, the CLI shim target, the launch shim, doctor's checks, and sl-debugger's "where truth lives" all still point at real things.

## 4. The URL-install flow, end to end (the v1 sharing bar)

A stranger pastes `https://github.com/willprout/superlooper` into any Claude Code session and says "install this":

1. The session runs `claude plugin marketplace add willprout/superlooper`, then `claude plugin install superlooper@superlooper --scope user` (both non-interactive; `/plugin …` works interactively too). **Five skills land. Nothing executes** — the plugin is markdown by construction. `/reload-plugins` or the next session activates them.
2. The session (guided by the root README and the `adopt` skill's bootstrap section) clones the repo to a checkout the user controls — e.g. `git clone https://github.com/willprout/superlooper ~/superlooper` — and runs `./bin/install.sh` from it. **This is the human gate, and it fires exactly as designed**: a first publish has no VERSION baseline, so the gate lists the entire payload as new and requires the user's explicit OK (interactive y/N, or `--yes` after the agent shows them the list). The engine lands at `~/.claude/skills/superlooper/`, hooks and launch shim register, `superlooper` lands on PATH.
3. `superlooper doctor --stack` names every missing machine block with its exact fix — cmux, `claude` subscription login, `gh auth`, notify channel, launch shim — and downloading cmux etc. is within the bar. The user fixes reds and re-runs to green.
4. `superlooper adopt --repo <their repo>` writes the config template, seeds the CLAUDE.md standing-rules block, creates the labels; they edit the config, run `doctor` to green, then `superlooper run`.

Two deliberate rejections in this flow: (a) the marketplace clone at `~/.claude/plugins/marketplaces/superlooper` is a full repo copy that *could* serve as the engine-install source, but it is a Claude-managed directory that silently pulls on marketplace update — a publish source must be a checkout the human controls, so the flow uses a normal clone; (b) no checked-in `.claude/settings.json` with `extraKnownMarketplaces`/`enabledPlugins` self-recommendation — it would fire install prompts inside every loop worker's worktree session, injecting nondeterminism into the loop's own sessions for zero stranger benefit (the stranger arrives via URL, not by cloning first).

## 5. How gated republish coexists with plugin updates

| Channel | Carries | Reaches a machine when | Human gate |
|---|---|---|---|
| Plugin update (`/plugin update`, or marketplace auto-update) | The five skills' markdown only | Next session after update | None needed — nothing in the payload can execute, and a repo test (child) mechanically keeps it that way |
| Gated republish (`./bin/install.sh`) | Everything that executes: runner, lib, hooks, shim, CLI — plus published docs | Only when a human reviews the engine diff and says OK | The existing engine-diff gate, unchanged |
| Dashboard | Its own clone-and-run repo dir | `git pull` by hand | The human doing the pull |

The invariant, stated once: **the plugin payload is inert by construction; the engine is inert-until-republish by the existing gate; no new door opens.** Plugin `hooks/`, `bin/`, `.mcp.json`, `monitors/`, `agents/`, and `settings.json` are banned from `plugin/` permanently — because the platform executes all of them ungated — and a content-lint test in CI enforces the ban (no executable components, no executable file bits). Merged skill-content changes ride to users on plugin semantics (they already passed the loop's own PR gate to reach main); merged engine changes stay inert until each machine's owner republishes. `plugin.json` carries **no version field**, so every commit to main is an update candidate — right for prose, and pinning would silently strand users on stale content after an unbumped push.

## 6. How each of the five skills lands

1. **`superlooper` (ops/router).** The current SKILL.md moves in and its router shrinks to what it owns: approval (`references/approval-protocol.md`) and ops (`references/runner-ops.md`) stay as its references; the issue-writing row now routes to the sibling `write-issue` skill and the adoption row to `adopt`. Engine prose that names `references/runner-ops.md` (stack_doctor.py, notify.py, a test docstring) gets its wording updated to the new home. Cosmetic quirk accepted: the manual invocation is `/superlooper:superlooper` (plugin name : skill name); the ruling fixed both names, auto-invocation is unaffected, and renaming either would relitigate a frozen input.
2. **`write-issue`.** `references/issue-writing.md` promotes nearly verbatim to `plugin/skills/write-issue/SKILL.md` — its rules cite the incidents that motivate them and must survive the move intact. Frontmatter description triggers on drafting/filing loop issues. The V2-IDEAS "open socket" — adapting the feature-dev plugin's explore/clarify phases as the skill's front half — is **not** folded into the restructure (owner decision O1 below).
3. **`adopt`.** New skill: a self-contained bootstrap (clone → gated `./bin/install.sh` → `doctor --stack` → `adopt`/`doctor`/`run`) that must live wholly inside the plugin (a cache copy can't read repo files), plus a route to the **published** full contract: `docs/ADOPTING.md` joins the gated payload so it lands at `~/.claude/skills/superlooper/docs/ADOPTING.md` — a stable path on any machine where `adopt` can run at all (the CLI it wraps arrives by the same install). One canonical file, no duplicated contract to drift; `test_docs_adopting.py` keeps pinning it against the real CLI. Preferred mechanism is moving the file into the payload dir (`skill/docs/ADOPTING.md`); if the child instead adds a copy step to `bin/install.sh`, that child is candidate-supervised.
4. **`cross-review`.** The machine-local command's body absorbs into `plugin/skills/cross-review/SKILL.md`, adapted for the loop: same prompt-assembly and honest-value discipline, same "don't fall back silently" default, plus the owner ruling of 2026-07-10 recorded in STACK.md — a fresh same-model subagent is an equally valid review path on a Claude-only machine, so the skill states the fallback explicitly instead of leaving vendor choice ambient. Codex remains a runtime dependency that `doctor --stack` already reports honestly (WARN on Claude-only machines). The **suggest-cross-review hook is not absorbed** (decision D5): a hook executes automatically, so shipping it in the plugin is exactly what ruling 3 forbids, and its trigger paths (`docs/superpowers/…`) are William's personal setup, not loop machinery. Repo docs that say `/cross-review` (skills/superlooper/CLAUDE.md, STACK.md) update to the namespaced invocation with a transition note.
5. **`sl-debugger`.** Lands by absorption, as ruled: `skills/sl-debugger/skill/` moves to `plugin/skills/sl-debugger/` with all four references. Two content edits: its pointer to the ops skill's runner-ops reference becomes the plugin-internal sibling path, and its "where truth lives" note keeps `~/.claude/skills/superlooper/` as the engine's installed home (still true). Its content-lint tests move with it (new home under the plugin or a top-level `tests/`; the child decides and keeps them in CI). The interim manual `cp -R` install in its README is superseded by the plugin and the README says so. This also closes the gap that sl-debugger currently has no distribution path at all.

## 7. Decisions resolved (rationale stated, none silently defaulted)

- **D1 — repo is its own marketplace; plugin at `plugin/`.** No direct-from-git plugin install exists, so the marketplace is the only way to meet the paste-URL bar; a dedicated `plugin/` subtree keeps the cache copy to pure content and keeps the engine/dashboard/design corpus out of every user's plugin cache.
- **D2 — nothing executable in the plugin, enforced mechanically.** Plugin hooks/bin/MCP/monitors execute ungated and ride updates; ruling 3 therefore demands their absence, and a CI test makes the absence a property of main rather than a convention.
- **D3 — skill content moves (git mv), never forks.** Two copies of SKILL.md/references would double-load the skill and drift; the plugin becomes the one home for content, the installed dir the one home for the engine.
- **D4 — engine home stays `~/.claude/skills/superlooper/`.** Moving it (e.g. to `~/.superlooper/engine/`) would churn hook registrations, the CLI shim, doctor, sl-debugger's readouts, and every doc, for zero v1 benefit. Revisitable post-v1. Residual risk, verified at build time by the scaffold child: a SKILL.md-less directory under `~/.claude/skills/` must not warn or load as an empty skill (loose files like `find-docs.md` already sit there harmlessly today).
- **D5 — the suggest-cross-review hook stays machine-local.** Rationale in §6.4. The ruling's "absorbed" is honored for the skill content; the hook is adjacent personal tooling whose absorption would breach ruling 3.
- **D6 — no `version` field in plugin.json.** SHA versioning tracks main; a pinned version turns every content merge into a silent no-op for users until someone remembers to bump.
- **D7 — dashboard stays out of the plugin.** It executes (a server); ruling 3 places it on the gated/clone side of the line. Its clone-and-run story is already documented and unchanged.
- **D8 — no checked-in `.claude/settings.json` plugin recommendation** (rationale in §4b).
- **D9 — ADOPTING.md stays the single canonical contract, published via the gated channel** (mechanism in §6.3). Content riding the *gated* channel is always allowed — the ruling constrains only the reverse direction.
- **D10 — loop machines install the plugin too.** After cutover the ops/write-issue/debugger skills reach planning sessions and workers from the plugin. The runner itself never depended on the skill being installed (briefs are self-contained), so this is availability, not correctness; a new `doctor --stack` WARN block makes a missing/disabled plugin visible.

## 8. Explicit owner decisions (needed from William, none defaulted)

- **O1 — write-issue's explore/clarify front half** (the V2-IDEAS open socket: adapting the feature-dev plugin's phases). Recommendation: keep it out of the restructure — the restructure's bar is content parity in the new shape; grafting a new interview flow onto write-issue in the same wave couples a format migration to a behavior change. If wanted, it becomes its own issue after the promotion lands. **Decide: defer (recommended) or fold into the promotion child.**
- **O2 — auto-update posture on your own machines.** Content-only updates are safe by construction (D2), so marketplace auto-update ON is within the rulings; manual `/plugin update` gives you a deliberate moment instead. Recommendation: ON for ordinary machines, your call for loop machines. **Decide: on or manual.**
- **O3 — retire the machine-local `~/.claude/commands/cross-review.md` after cutover?** Keeping it duplicates the verb (`/cross-review` vs `/superlooper:cross-review`); deleting it makes the plugin the one home. Recommendation: delete once the plugin copy is verified working on your machine. Your machine, your call.
- **O4 — prerequisite supervised config edit (required before approving the children).** `.superlooper/config.json` is bright-lined, so a supervised session — not a loop worker — must add the new area so children can declare it honestly and the gate stops mapping `plugin/**` to the wildcard: `"plugin": ["plugin/**", ".claude-plugin/**", "README.md"]` under `areas`. Without this, every plugin child serializes behind the wildcard rule and gets wander-flagged at the gate.

## 9. Cutover checklist for already-running machines (owner-run, ordered)

1. Merge the children; supervised config edit (O4) already in place.
2. On each machine: `claude plugin marketplace add willprout/superlooper && claude plugin install superlooper@superlooper --scope user`.
3. Immediately republish the engine from the machine's checkout: `git pull && ./bin/install.sh` — the gate shows SKILL.md/references as deletions (expected; content moved to the plugin). Between steps 2 and 3 both copies of the skill exist; keep the window short.
4. Verify: new session lists the five namespaced skills once each; `superlooper doctor --stack` green (plus the new plugin block once that child lands); optionally delete the machine-local cross-review command (O3).

## 10. Child issues

Filed as this report's children (each `parent: #65`, labeled `needs-william`, sized in-body; none may start before O4's supervised config edit):

1. **Commit the plugin-restructure design record** — this report into `skills/superlooper/docs/DESIGN-2026-07-11-plugin-restructure.md`, plus a V2-IDEAS pointer update. (S)
2. **Scaffold the marketplace + plugin; move the superlooper ops skill** — manifests, ops SKILL.md + 2 references move, router rewrite, engine prose pointer updates, double-load verification. Candidate-supervised (it redefines what the gated installer publishes). (M)
3. **Promote write-issue into the plugin.** (S)
4. **Author the adopt skill and publish ADOPTING.md with the engine** — candidate-supervised if it edits `bin/install.sh`. (M)
5. **Absorb cross-review into the plugin; update repo docs' invocation wording.** (S)
6. **Move sl-debugger into the plugin; repoint its cross-references and tests.** (M)
7. **Mechanical inert-plugin fence in CI** — the test that keeps `plugin/` executable-free forever. (S)
8. **Root README: the paste-URL install path.** (S)
9. **`doctor --stack`: plugin-presence WARN block.** (S)

Zero PRs from this investigation; no code changed.
