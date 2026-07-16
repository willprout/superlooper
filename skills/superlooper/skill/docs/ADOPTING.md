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
| `lanes` | `2` | How many issues may run at once (parallel worktrees/sessions). Either an **integer ≥ 1** — one shared pool, any issue type may take any lane — or an **object** `{"build": N, "investigate": M}` that reserves capacity by type (see **Reserved investigation lanes** below). The integer form is unchanged; existing configs keep working exactly as before. |
| `affinity` | `"hard"` | `"hard"` = two issues co-schedule only if their declared `touches:` areas are **disjoint** (no two lanes editing the same area at once). `"soft"` = overlap allowed but journaled. |
| `areas` | `{}` | A map of area-name → list of path globs (fnmatch). It defines what "touches the same thing" means for affinity and for wander-detection at the gate. A path matching no area maps to the wildcard `*`, which overlaps **everything** under hard affinity (see the wildcard rule below). If a PR's files map to `*` because no glob matched them, the merge is **held** behind every in-flight lane and the journal records that the hold is wildcard-caused — so add a glob covering those files if you don't want that serialization. |
| `touches_required` | `true` | **If true**, every approved *build* / *diagnose-and-fix* issue must declare a non-empty `touches:` in its Loop metadata. An approved issue that doesn't is **refused at launch** and handed back to William (`needs-owner`) with a memo naming the missing block — it never launches until the declaration is added. Investigations are exempt (they produce no merge). The declared areas are what anti-affinity and the gate's wander check verify against (a diff that leaves its declared areas is logged as a wander, never blocked). **If false**, the declaration is optional — but an issue with no `touches:` maps to the wildcard `*` (see below), so under hard affinity it can only run alone; when that serializes a lane the journal records why. Turn off only for a small repo where areas don't matter. |

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

**Reserved investigation lanes.** A plain integer `lanes` serializes *everything* at 1 (including
investigations, which open no PR and can't cause a merge conflict) and parallelizes *everything* at
2+ (accepting conflict risk between builds). The missing middle — strictly-sequential
merge-producing work **with** investigations still flowing in parallel — is expressed by the object
form:

```json
"lanes": { "build": 1, "investigate": 1 }
```

- `build` — lanes for **merge-producing** work: `build` *and* `diagnose-and-fix` issues both draw on
  this pool (they open PRs and merge, so they're the ones anti-affinity governs). `build: 1` keeps
  merge-producing work strictly sequential — the whole point.
- `investigate` — lanes **reserved** for investigations (`type:investigate`, which produce a report
  and child issues, never a PR).

Both pool sizes are **required** when you use the object form (a lone `{"build": 2}` that silently
zeroes investigations is rejected at load), each is an integer ≥ 0, and their total must be ≥ 1. The
canonical `1 + 1` gives you: a running build holds the sole build lane, a second approved build
**waits**, and an approved investigation launches **immediately** into the reserved lane instead of
queuing behind the build.

The reservation is **strict, in both directions — no borrowing:**

- A merge-producing issue **never** occupies the reserved investigation lane, even when it's idle and
  a build is queued (that lane stays idle by design — preserving sequential build discipline is why
  you reserved it).
- An investigation **never** borrows an idle build lane. With `investigate: 1`, a second pending
  investigation waits rather than spilling into the build pool — so a build is always free to launch
  the moment the running build finishes, never stuck behind an investigation that grabbed its lane.

Anti-affinity, territory claims, the usage/quota ceilings, and `blocked-by` all still apply
unchanged within each pool; this is purely how lane *capacity* is counted. (Prefer the integer form
unless you specifically want this build-vs-investigation split.)

### The ship gate

| Field | Default | Meaning |
|---|---|---|
| `required_checks` | `[]` | GitHub check names that must be **green** before the loop merges a PR. Either a **list of strings** — the same set gates PR merges *and* the dev-branch freeze/unfreeze — or an **object** `{"pr": [...], "dev": [...]}` that declares the two surfaces separately (see **PR-required vs dev-required checks** below). **`doctor` fails hard if the PR set is empty** — a repo with no CI check enforcing its own tests has no mechanical gate, so at least one is required before you run. |
| `merge_method` | `"squash"` | How the loop merges a green PR (`squash` \| `merge` \| `rebase`). Squash keeps dev history clean; it is the recommended default. |
| `ship_cmd` | `null` | If set, worker briefs say "ship EXCLUSIVELY via this command" (e.g. a repo's own `scripts/ship.sh` that owns review + CI). If `null`, the brief tells the worker to push the branch and open the PR itself, and the gate requires a fresh-agent review comment on the PR. |
| `ship_recheck_cmd` | `null` | Run by the runner from the worktree after a merge-update, to re-post a diff-pinned gate verdict. Exit 0 → proceed; nonzero → **park** (the loop never coaches around a fail-closed gate). |
| `report_required_sections` | `["Tests", "Review"]` | H2 headings a worker's final report must contain, each with real prose — the runner checks their presence mechanically as part of the gate. The default is deliberately **web-agnostic**: every worker can produce passing **Tests** and a fresh-agent **Review**, so a CLI/library/service repo is never nudged-then-parked for evidence it cannot give. Web/UI repos opt into richer evidence explicitly (see below). |
| `bright_lines` | `[]` | Prose rules injected **verbatim** into every worker brief (e.g. "force-push forbidden", "ship only via ship.sh"). The skill hardcodes none; the repo's adaptation fills these. |

**Review is always mechanically gated, and pinned to the diff it reviewed.** On a repo with its own
pipeline (`ship_cmd` set), that pipeline owns review. On a repo without one, the gate refuses to
merge until a fresh agent that wrote none of the code posts a review verdict as a PR comment
beginning `<!-- superlooper-review sha=REVIEWED_HEAD_OID -->`, posted after the last push, with
`REVIEWED_HEAD_OID` replaced by the oid `git rev-parse HEAD` then prints — **pasted in literally**.
Do not write `sha=$(git rev-parse HEAD)`: a body containing `<!--` and `-->` wants single quotes,
and single quotes do not expand `$(...)`, so the marker would carry unexpanded text and pin
nothing. The `sha=` names the commit the reviewer actually saw, and the gate honors the verdict
only while the PR's head still matches it (a 7+ hex abbreviation is fine). A verdict for a
superseded diff stops counting: when a PR is rebuilt or pushed to again, the old verdict no longer
merges the new code, and the worker is nudged to re-review what is on the PR now. The merge itself
is pinned to the same oid (`--match-head-commit`), so a push that races the gate is refused rather
than merged unreviewed. The runner's own mechanical merge-update is the one head move that carries
a verdict forward — it merges the dev branch in without touching the authored diff, and only when
the worktree really was at the reviewed head.
A marker with no readable pin — the legacy `<!-- superlooper-review -->` form, a placeholder left
unsubstituted, an unexpanded `$(...)` — cannot prove which diff it reviewed and so never satisfies
the gate; it fails closed to a nudge asking for a repin, then park.
Either way, no code merges unreviewed — and the reviewer is never the author.

**Adding browser evidence (web/UI repos) — opt-in.** The default `report_required_sections` asks only
for what *any* repo can honestly show. A web or UI repo that wants a screenshot/recording section in
every report sets the list explicitly — e.g. `["Tests", "Browser evidence", "Regression tests", "Review"]` —
and the gate then requires that H2 with real prose like any other. This is an opt-in, deliberately
*not* the default, precisely so a CLI, library, or service repo is never asked for browser evidence it
can never produce (and then parked for the missing section).

**PR-required vs dev-required checks.** The loop reads `required_checks` on **two** surfaces: the merge
gate folds a PR's checks down to it before merging, and the dev-branch **freeze/unfreeze** poll folds
the dev HEAD's checks down to it (red on dev freezes merges; back to green lifts the freeze). With a
plain list, the *same* set governs both. That breaks for a check that **gates PR merges but never
reports on the dev branch** — e.g. a status a ship script stamps on the PR head commit only, which the
post-squash-merge dev HEAD never receives. The merge gate legitimately requires it, but the dev poll
sees it as forever-missing → `pending`, so once the genuinely-dev checks green, the freeze **still
never lifts**. The split expresses the two question sets separately:

```json
"required_checks": { "pr": ["review/local-gate", "quality-gate"], "dev": ["quality-gate"] }
```

- `pr` — checks that must be green on the **PR** before it merges (the mechanical ship gate). **At
  least one is required**; `doctor` fails hard on an empty PR set.
- `dev` — checks expected to report on (and gate the freeze/unfreeze of) the **dev branch**. Usually a
  *subset* of `pr`: drop any check that never reports on dev, so it can't strand a mainline freeze
  forever. May be empty (a repo whose CI runs on PRs only, never on dev push — the freeze mechanism
  then simply idles).

Both keys are **required** when you use the object form (a lone `{"pr": [...]}` silently defaulting
`dev` back to `pr` — which would recreate the exact stranded-freeze bug — is rejected at load).
`doctor`'s name cross-check is surface-aware: it flags a `dev`-required check that never reports on the
dev branch, and a `pr`-required check that never reports on a PR, but a check you deliberately excluded
from `dev` is **not** flagged. (Prefer the plain list unless a required check genuinely never reports
on dev.)

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
| `cleanup_parked_worktrees` | `true` | Reclaim the worktrees of park-family terminal issues (parked / needs-owner / bounced), which otherwise linger forever (issue #41). Safe — re-approval rebuilds from the issue on a fresh branch. Set `false` to keep them for manual inspection. |
| `notify.imessage_to` | `null` | Phone number / Apple ID the runner texts via the Mac's Messages app. `null` falls back to `notify.cmd`, then `cmux notify`, then log-only. |
| `notify.cmd` | `null` | A generic notify command template (`{title}`/`{body}`) if you don't use iMessage. |
| `notify.quiet_hours` | `{"start": "21:00", "end": "08:00"}` | The nightly window (Mac-local, `end` exclusive, wraps midnight) during which routine owner-**decision** pages (a park, a bounce, a durable question) are **batched to the morning report** instead of pushed — nobody answers a 3am page and a park is a safe state. Systemic-stop alerts (runner/auth dead, whole queue stalled) and the merge-freeze notice always push. Set to `null` to page on every hand-back at any hour. |
| `report_time` | `"08:45"` | When the morning report is generated + pushed (Mac-local time). The morning push doubles as the notify-channel canary: its delivery result surfaces on the next report's **Notify channel** line, so a silently dead channel shows up on the dashboard. |
| `watchdog.authority` | `"full"` | Standing authority tier for an **unattended** sl-debugger session the watchdog launches (issue #66): `diagnose-only` \| `allowlist` \| `full`. Even `full` excludes the constitution absolutely (never `agent-ready`, never merge/force-push, never frozen issue text, never `.superlooper/**` or `.github/workflows/**`) — enforced by the sl-debugger skill's unattended contract. |
| `watchdog.allowlist` | `[]` | The exact repair verbs permitted at the `allowlist` tier, as strings, interpreted literally (never expansively). Ignored at the other tiers. |
| `watchdog.grace_minutes` | `30` | How long after the watchdog texts you it waits before launching the unattended session. If the signal clears meanwhile it stands down silently. `0` launches on the tripping check. |
| `watchdog.heartbeat_stale_minutes` | `20` | How stale `state/runner.heartbeat` must be to count as a wedged/dead loop. Keep it comfortably above the longest legitimate tick (a ship recheck can hold one ~10 min). |
| `watchdog.no_progress_minutes` | `30` | How long eligible `agent-ready` work may wait with **every lane empty and nothing launching** before that reads as a fault. Designed-safe waits (CI gates, blocked-by holds, parked/needs-owner, a building lane during a freeze, a usage meter that reads exhausted) never start this clock. |

All schedule times are **Mac-local** — no timezone field, the runner and launchd read the system
clock.

**The unattended-debugger watchdog** (issue #66) is opt-in wiring: load
`templates/launchd.watchdog.plist` as a user LaunchAgent to run `superlooper watchdog --repo
<path>` every few minutes (300 s is a good interval). Each firing is a mechanical one-shot — no
LLM anywhere in it: it reads the health signals (stale heartbeat, present `state/ALERT`, the
no-progress shape), texts you when one trips, waits `watchdog.grace_minutes`, and if the signal
still stands launches ONE fresh sl-debugger session through the same interactive launch shim
workers use. Every launch is journaled and lands in the morning report. `touch
<state-home>/state/WATCHDOG_OFF` disables the whole path (it keeps observing and journaling,
launches nothing); delete the file to re-arm. Operations detail:
`plugin/skills/superlooper/references/runner-ops.md` → "The unattended-debugger watchdog".

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
  `adopt` seeds `model:opus`, `model:opus[1m]`, `model:fable`, `model:sonnet`; create more as you
  need them.
- `effort:<level>` — run this issue's worker sessions at reasoning effort `<level>` (when absent,
  falls back to `models.worker_effort`, else nothing). `adopt` seeds `effort:low`, `effort:medium`,
  `effort:high`, `effort:xhigh`, `effort:max`.
- **Exactly one of each per issue** (2+ makes the issue wait for you, like a duplicate `type:`).
  The value is pass-through — no allowlist — so an **unknown** value fails the launch and parks the
  issue with a memo. The one-shot answerer is unaffected (config-only). See
  `plugin/skills/superlooper/references/runner-ops.md`.

**Workflow state (the runner drives these):**
- `in-progress` — a worker is building it.
- `needs-owner` — an owner decision is required (a bounce, a cap hit, a fail-closed gate).
  (Renamed from the older `needs-william`; `adopt` migrates any existing `needs-william` label in
  place — preserving it on every issue — and the runner recognizes both, so an already-adopted repo
  keeps working. Re-run `adopt` to migrate.)
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
   parses, the labels exist, the `dev_branch` exists on origin, **the PR-required checks are
   non-empty**, and **every `required_checks` name actually matches a check the repo has reported**
   on recent PRs and the dev branch. A name typo (`quality-gate` vs `Quality Gate`) or a check the
   repo never wired reads as "pending" forever, so a green PR would gate without merging; `doctor`
   fails it here with a case/shape hint, and separately flags a **dev-required** check that reports
   on PRs but never on the dev branch (and the mirror — a PR-required check that reports only on
   dev). A check you deliberately excluded from the `dev` set is not flagged. *Why here:* it is the all-green gate before you run — and only now, with step 1
   published and step 2's config written, does every check have something real to inspect. Fix
   anything red and re-run `doctor` until it passes.
4. `superlooper run --repo <path>` — start the runner in a cmux tab you can watch (it targets that
   tab's own pane automatically — no `--pane` needed). This is the *only* way to run it: there is no
   launchd runner, because a paneless launchd daemon can't open the worker tabs the loop needs
   (issue #33; restart it the same way — see
   plugin/skills/superlooper/references/runner-ops.md → "Restarting the runner").
   *Why last:* it launches worker sessions against approved issues, so it needs a green `doctor` and
   your approvals in hand first. Approve issues by conversation (William's word applies
   `agent-ready`); the runner picks them up on its next tick.

## The config file is trusted, executable data — protect it accordingly

`.superlooper/config.json` names commands the runner executes verbatim (`ship_cmd`,
`ship_recheck_cmd`, `notify.cmd`) and defines the gates the loop enforces. A PR that edits it
is a PR that reprograms the loop. On any repo with a security-review floor or protected-path
list, add `.superlooper/**` to it before the loop's first run, so the loop's own contract
can't be rewritten by an ordinary change. (Surfaced by the eApp adaptation session,
2026-07-06 — recorded there as adoption-checklist item #1.)
