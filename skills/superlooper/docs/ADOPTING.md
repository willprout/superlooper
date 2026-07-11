# Adopting a repo into the superlooper loop

This is the contract any repository signs to run the issue-loop. Everything the loop needs to
know about *your* repo lives in one file — `.superlooper/config.json` at the repo root — plus a
small set of GitHub labels and a light branch-protection tweak. Nothing repo-specific lives in
the skill itself; it all enters through this config.

> **Who this is for:** the repo owner (William, or a friend running their own loop). You do not
> need to be a professional developer to adopt a repo — the two commands below do the mechanical
> parts, and this doc explains every knob in plain language.

---

## Getting the `superlooper` command

Both commands below — and every `superlooper …` in these docs — assume the `superlooper` command
resolves on your shell PATH. **Publishing puts it there.** The gated installer,

```
./bin/install.sh      # run once from the monorepo root; re-run to republish
```

copies the skill into `~/.claude/skills/superlooper/` and links a stable `superlooper` command into
a standard user bin dir (it prefers `~/.local/bin`, falling back through `~/bin` and `/usr/local/bin`
— whichever is already on your PATH). The link is a thin shim pointing at the installed copy, never
this source repo, and it is re-created idempotently on every publish. The installer prints exactly
what it linked and where; if the chosen dir is **not** on your PATH it does not silently skip — it
prints the exact `export PATH="…"` line to add, then open a new shell. (This is separate from the
launch shim, which self-runs *worker* sessions; the CLI link is what makes `superlooper` itself
resolve.)

## The two commands

```
superlooper adopt  --repo /path/to/your/repo      # writes the config template, creates the
                                                   # labels, prints branch-protection advice
superlooper doctor --repo /path/to/your/repo      # checks everything is wired correctly
```

`adopt` is safe to re-run: it never overwrites an existing config, and it skips labels that
already exist. `doctor` changes nothing — it only reports. Run `doctor` until it is all-green
before you start the runner. (Both commands are live today — publishing (above) puts the
`superlooper` command on your PATH so you can run them, and the schema below is the contract
they implement.)

---

## `.superlooper/config.json` — every field

The only **required** field is `repo`. Everything else has a sensible default (shown in
parentheses); omit a field to accept its default. Unknown keys, bad values, and typos are
rejected loudly at load time, naming the offender — a misconfiguration is an adopt-time error,
never a silent 3am surprise.

### Identity & branches

| Field | Default | Meaning |
|---|---|---|
| `repo` | *(required)* | `"owner/name"` — the GitHub repo. Also names the state dir `~/.superlooper/<owner>__<name>/`. |
| `dev_branch` | `"main"` | The mainline the loop merges approved work into. |
| `prod_branch` | `null` | The production branch, if you promote dev→prod. `null` = no prod branch yet; the promotion report then just points at your own checklist. |

### Lanes & scheduling

| Field | Default | Meaning |
|---|---|---|
| `lanes` | `2` | How many issues may build at once (parallel worktrees/sessions). Integer ≥ 1. |
| `affinity` | `"hard"` | `"hard"` = two issues co-schedule only if their declared `touches:` areas are **disjoint** (no two lanes editing the same area at once). `"soft"` = overlap allowed but journaled. |
| `areas` | `{}` | A map of area-name → list of path globs (fnmatch). It defines what "touches the same thing" means for affinity and for wander-detection at the gate. A path matching no area maps to the wildcard `*`, which overlaps **everything** under hard affinity (see the wildcard rule below). If a PR's files map to `*` because no glob matched them, the merge is **held** behind every in-flight lane and the journal records that the hold is wildcard-caused — so add a glob covering those files if you don't want that serialization. |
| `touches_required` | `true` | **If true**, every approved *build* / *diagnose-and-fix* issue must declare a non-empty `touches:` in its Loop metadata. An approved issue that doesn't is **refused at launch** and handed back to William (`needs-william`) with a memo naming the missing block — it never launches until the declaration is added. Investigations are exempt (they produce no merge). The declared areas are what anti-affinity and the gate's wander check verify against (a diff that leaves its declared areas is logged as a wander, never blocked). **If false**, the declaration is optional — but an issue with no `touches:` maps to the wildcard `*` (see below), so under hard affinity it can only run alone; when that serializes a lane the journal records why. Turn off only for a small repo where areas don't matter. |

`areas` example:

```json
"areas": {
  "frontend": ["src/components/**", "src/styles/**"],
  "api":      ["src/api/**", "src/server/**"],
  "db":       ["migrations/**", "src/db/**"]
}
```

**The wildcard rule.** An issue that declares no `touches:`, or declares `touches: *`, and any file
that matches no `areas` glob, both map to the wildcard `*`. Under **hard** affinity `*` overlaps
**every** lane — the safe default, so an issue of unknown scope never collides with another lane by
surprise. The cost is serialization: a single wildcard occupant makes `lanes: N` behave like one
busy lane. That used to be silent; now the loop **says so in the journal** — once per episode when a
wildcard suppresses a launch, and on the merge side when a `*`-mapped diff holds behind an in-flight
lane. So if you set `lanes` high and see only one lane working, `grep wildcard_hold` (and the
wildcard-flagged `hold` records) in `journal.jsonl` will name the no-touches issue causing it. The
fix is to declare narrower `touches:` and add `areas` globs that cover your files.

### The ship gate

| Field | Default | Meaning |
|---|---|---|
| `required_checks` | `[]` | GitHub check names that must be **green** before the loop merges a PR. **`doctor` fails hard if this is empty** — a repo with no CI check enforcing its own tests has no mechanical gate, so at least one is required before you run. |
| `merge_method` | `"squash"` | How the loop merges a green PR (`squash` \| `merge` \| `rebase`). Squash keeps dev history clean; it is the recommended default. |
| `ship_cmd` | `null` | If set, worker briefs say "ship EXCLUSIVELY via this command" (e.g. a repo's own `scripts/ship.sh` that owns review + CI). If `null`, the brief tells the worker to push the branch and open the PR itself, and the gate requires a fresh-agent review comment on the PR. |
| `ship_recheck_cmd` | `null` | Run by the runner from the worktree after a merge-update, to re-post a diff-pinned gate verdict. Exit 0 → proceed; nonzero → **park** (the loop never coaches around a fail-closed gate). |
| `report_required_sections` | `["Tests", "Browser evidence", "Regression tests", "Review"]` | H2 headings a worker's final report must contain, each with real prose — the runner checks their presence mechanically as part of the gate. |
| `bright_lines` | `[]` | Prose rules injected **verbatim** into every worker brief (e.g. "force-push forbidden", "ship only via ship.sh"). The skill hardcodes none; the repo's adaptation fills these. |

**Review is always mechanically gated.** On a repo with its own pipeline (`ship_cmd` set), that
pipeline owns review. On a repo without one, the gate refuses to merge until a fresh agent that
wrote none of the code posts a review verdict as a PR comment beginning `<!-- superlooper-review -->`.
Either way, no code merges unreviewed — and the reviewer is never the author.

### Models, timers, QA, notifications, housekeeping

| Field | Default | Meaning |
|---|---|---|
| `models.worker` | `"opus[1m]"` | Model for the build sessions. `opus[1m]` = the latest Opus (the `opus` alias auto-tracks it) with the 1M-token context window (`[1m]` opts in; bare `opus` is standard ~200K). Passed to `claude --model`; any valid model string works. |
| `models.answerer` | `"opus[1m]"` | Model for the one-shot answerer that unblocks a stuck worker — the loop's highest-judgment hire (resolve vs. escalate), so it defaults to the strongest configuration (latest Opus + 1M context). |
| `models.worker_effort` | `null` | Repo-wide reasoning-effort default for **worker** launches, passed to `claude --effort`. `null` = today's behaviour (no `--effort` flag). A per-issue `effort:<level>` label overrides it; the answerer never reads it. Any value the agent accepts works — a bad value fails the launch and parks the issue (no allowlist). |
| `session.idle_seconds` | `480` | A launched, unresolved session with no activity for this long gets a safe peek-nudge (8 min). |
| `session.freeze_seconds` | `2700` | The hard stall backstop (45 min) → the recovery ladder. |
| `session.retry_cap` | `2` | Relaunch attempts before an issue is parked. |
| `session.conflict_cap` | `2` | Conflict-regenerations before an issue is handed to William. |
| `qa.nightly_cmd` | `null` | The full nightly QA command (e.g. a browser suite). `null` = no nightly loop yet. |
| `qa.results_glob` | `null` | Where the nightly run writes JUnit XML the loop parses. |
| `qa.retry_once` | `true` | Re-run a failing nightly once; a failure that clears on retry is a flake (stats only), a persistent one files a fix issue. |
| `qa.quarantine` | `[]` | Test ids excluded from nightly failure counting. |
| `qa.nightly_time` | `"02:00"` | When the nightly runs (Mac-local time). |
| `cleanup_merged_worktrees` | `true` | Remove a worktree after its issue merges. |
| `notify.imessage_to` | `null` | Phone number / Apple ID the runner texts via the Mac's Messages app. `null` falls back to `notify.cmd`, then `cmux notify`, then log-only. |
| `notify.cmd` | `null` | A generic notify command template (`{title}`/`{body}`) if you don't use iMessage. |
| `report_time` | `"08:45"` | When the morning report is generated + pushed (Mac-local time). |

All schedule times are **Mac-local** — no timezone field, the runner and launchd read the system
clock.

---

## The label set

`adopt` creates these in your repo. Exactly **one** `type:` label per issue defines its kind; the
rest are workflow state the runner and William drive.

**Type (exactly one, required):**
- `type:build` — implement a feature/change, opens a PR.
- `type:investigate` — produce a root-cause report + scoped child issues, **no PR**.
- `type:diagnose-and-fix` — investigate, then fix within boundaries (or split if it exceeds them).

**Approval & priority (William's words):**
- `agent-ready` — **William's approval.** No agent ever applies this without his explicit say-so.
- `priority:high` / *(no label = normal)* / `priority:low` — ordering band.
- `expedite` — bypass the queue: slotted into the very next free lane ahead of everything.

**Per-issue model / effort (William's control knobs — apply/remove any time):**
- `model:<name>` — run **this issue's** worker sessions on `<name>` instead of `models.worker`.
  `adopt` seeds `model:opus`, `model:opus[1m]`, `model:fable`; create more as you need them.
- `effort:<level>` — run this issue's worker sessions at reasoning effort `<level>` (when absent,
  falls back to `models.worker_effort`, else nothing). `adopt` seeds `effort:low`, `effort:medium`,
  `effort:high`, `effort:xhigh`, `effort:max`.
- **Exactly one of each per issue** (2+ makes the issue wait for you, like a duplicate `type:`).
  The value is pass-through — no allowlist — so an **unknown** value fails the launch and parks the
  issue with a memo. The one-shot answerer is unaffected (config-only). See `runner-ops.md`.

**Workflow state (the runner drives these):**
- `in-progress` — a worker is building it.
- `needs-william` — an owner decision is required (a bounce, a cap hit, a fail-closed gate).
- `parked` — handed back with a memo after a retry/conflict cap.
- `preserve` — on a PR: resolve conflicts in the PR's own branch instead of regenerating.
- `superseded` — on a PR the loop replaced by a rebuild (branch kept, PR left open).
- `auto-approved:nightly-red` — the one standing-rule auto-approval: a fix issue the nightly
  files to restore a red mainline. This is a distinct label precisely because `agent-ready` is
  William's word and a standing rule must carry its own.

---

## Branch-protection recommendation

The loop updates branches by **merge**, never force-push, and never rebases history. Two settings
make that work cleanly:

1. **Drop the "require branches to be up to date before merging" (strict) rule.** With `strict`
   on, GitHub demands every PR be rebased onto the latest dev tip before merge — which forces a
   force-push workflow the loop deliberately does not have. The loop instead does a mechanical
   `git merge origin/<dev>` in the worktree when a PR falls behind, which is a normal fast-forward
   push. Dropping `strict` is what lets that merge-based update satisfy protection without
   force-push.
2. **Keep your required status checks required.** List them in `required_checks`. The loop waits
   for them to go green and refuses to merge otherwise — that is the mechanical §4.3 gate.

On the Agent 360 eApp specifically: keep `review/local-gate` and `quality-gate` **required and
diff-pinned**, and drop only `strict`. Verify the live protections at change-time — never assume.

---

## The walkthrough — publish → adopt → doctor → run (in this order)

Run these four steps in order on a fresh machine and `doctor` reaches all-green *before* you
`run`. The order is not cosmetic: each step produces what the next one checks for, so following
them out of order (adopting or running a `doctor` before you have published) guarantees a red
report.

1. **Publish** — `./bin/install.sh`, once, from the monorepo root. *Why first:* it puts the
   `superlooper` command on your PATH (so steps 2–4 can be invoked at all) and it installs the
   launch shim and registers the two activity hooks — the exact artifacts step 3's `doctor`
   checks for. Skip it and `adopt`/`doctor` are "command not found"; run them from the source
   tree directly and `doctor` still reports a red launch-shim / activity-hooks check. Re-run any
   time to republish — it shows the engine diff and asks before overwriting. (See *Getting the
   `superlooper` command* above for where the link lands and what to do if that dir isn't on
   your PATH.)
2. `superlooper adopt --repo <path>` — writes `.superlooper/config.json` from the template,
   seeds the CLAUDE.md standing-rules block, creates the labels above, and prints the
   branch-protection advice. *Why here:* it produces the config `doctor` validates in the next
   step. Then edit the config: set `repo`, your `areas`, at least one `required_checks` entry,
   and any `bright_lines`.
3. `superlooper doctor --repo <path>` — verifies: `gh` is authenticated, `cmux` is present, `jq`
   is present, the launch shim is installed, the two activity hooks are registered, the config
   parses, the labels exist, the `dev_branch` exists on origin, **`required_checks` is non-empty**,
   and **every `required_checks` name actually matches a check the repo has reported** on recent
   PRs and the dev branch. A name typo (`quality-gate` vs `Quality Gate`) or a check the repo never
   wired reads as "pending" forever, so a green PR would gate without merging; `doctor` fails it
   here with a case/shape hint, and separately flags a check that reports on PRs but never on the
   dev branch. *Why here:* it is the all-green gate before you run — and only now, with step 1
   published and step 2's config written, does every check have something real to inspect. Fix
   anything red and re-run `doctor` until it passes.
4. `superlooper run --repo <path>` — start the runner in a cmux tab you can watch (it targets that
   tab's own pane automatically — no `--pane` needed), or under launchd for keep-alive. *Why last:*
   it launches worker sessions against approved issues, so it needs a green `doctor` and your
   approvals in hand first. Approve issues by conversation (William's word applies `agent-ready`);
   the runner picks them up on its next tick.

## The config file is trusted, executable data — protect it accordingly

`.superlooper/config.json` names commands the runner executes verbatim (`ship_cmd`,
`ship_recheck_cmd`, `notify.cmd`) and defines the gates the loop enforces. A PR that edits it
is a PR that reprograms the loop. On any repo with a security-review floor or protected-path
list, add `.superlooper/**` to it before the loop's first run, so the loop's own contract
can't be rewritten by an ordinary change. (Surfaced by the eApp adaptation session,
2026-07-06 — recorded there as adoption-checklist item #1.)
