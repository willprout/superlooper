# Superlooper V1 Implementation Plan

> **Historical record — one path has moved.** This plan was written when the engine was a
> standalone repo, so every `bin/install.sh` below means *this directory's* installer. After the
> 2026-07-08 monorepo migration the publish step is the **repo-root** `bin/install.sh` (it shows
> the engine diff and requires an explicit OK); `skills/superlooper/bin/install.sh` is a tombstone
> that refuses. See issue #197 and `tests/test_one_publish_door.py`. The rest of the plan stands as
> the record of what was built.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. Exec sessions run **Opus**. Read
> `CLAUDE.md` and `docs/founding/SPEC-2026-07-02-issue-loop-workflow.md` §2–§5 before Task 1.

**Goal:** Build the universal issue-loop skill from the settled spec: issue conventions + an
issue-writing discipline, a deterministic loop runner (one fresh Claude session per approved
GitHub issue per worktree, no standing LLM seat), the mechanical per-PR ship gate with
merge-on-green and the conflict ladder, the morning report, the nightly-QA/known-failure-ledger
/promotion-report stack — plus the thin eApp adaptation package.

**Architecture:** One deterministic Python daemon (`runner.py`) per adopted repo merges
autocode's proven *sensing* layer (file-backed signals, delivery verification, liveness tiers)
with the *acting* layer the old LLM orchestrator used to hold (launch, recover, gate, merge,
label, report). GitHub is the work-queue state store; a small per-repo disk state dir holds
counters and session signals. LLM judgment is hired per-event (a fresh visible answerer
session per question; fresh worker sessions — never headless `claude -p`, decision B.9) and
the runner never needs judgment to act safely. Everything
repo-specific enters through a per-repo config contract (`.superlooper/config.json`).

**Tech Stack:** Python 3 stdlib only (like autocode: no pip deps in the runtime), bash for the
launch/pane machinery, `gh` CLI for all GitHub I/O, cmux CLI for visible sessions, pytest for
tests, launchd for keep-alive/nightly scheduling.

---

## A. What survives from autocode, what dies (constitution §7, audit-backed)

**Ports (copy + adapt from `~/.claude/skills/autocode/` — FROZEN, never edit the source):**

| Source | Destination | Why it survives |
|---|---|---|
| `lib/sanitize.py` | `skill/lib/sanitize.py` | injection guard on ids/branches before shell/git |
| `lib/pane_state.py` | `skill/lib/pane_state.py` | the safe-pane-write classifier (proven across 3 runs) |
| `lib/state.py` | `skill/lib/loopstate.py` | atomic writes + advisory lock + the S6 mutate-validation guard |
| `bin/usage.py` | `skill/lib/usage.py` | usage fail-closed launch gates (RC-USAGEFAILOPEN) |
| `bin/watcher.py` (pure core only) | `skill/lib/events.py` | detect_events / dedup tokens / marker-existence resolution — the false-wake fixes |
| `bin/launch-pr.sh` | `skill/bin/launch-session.sh` | keystroke-free launch + delivery verification (RC6 + RC-LAUNCHVERIFY) |
| `bin/start-pr.sh` | `skill/bin/start-session.sh` | worker singleton + exited marker (RC-WORKER-SINGLETON, RC-DEADPANE) |
| `bin/nudge-pane.sh` | `skill/bin/nudge-pane.sh` | the single safe pane-write primitive |
| `bin/pretrust.sh` | `skill/bin/pretrust.sh` | trust-dialog pre-acceptance (Spike A3, flock-serialized) |
| `bin/activity-hook.sh`, `bin/stop-hook.sh` | same names | liveness stamps |
| `shell/launch-shim.zsh` + `bin/install-launch-shim.sh` | same names | display-independent launch delivery |
| `tests/` (matching subset) | `tests/` | port each test alongside its module |

**Dies (do not port; do not rebuild):** the standing orchestrator + ORCHESTRATOR.md as runtime
doc; rotation and all its machinery (`rotation_*` in watcher/state); the doorbell/ring/self-wake
layers (`ring_*`, `last_drain`/`last_progress`, wake-orchestrator.sh); orchestrator.lock (the
runner gets a plain pidfile singleton); the wave-checkpoint; `plan.json`/handoff apparatus
(issues replace both); ship-watch.sh (runner polls in-process); the merge train.

**Mandatory port fixes (paid-for discoveries; apply during the port, cite in comments):**

1. `nudge-pane.sh`: current cmux **rejects `--workspace` on `read-screen`** (the error is
   swallowed by `2>/dev/null || true` → empty screen → permanent fail-closed defer). Drop
   `--workspace` from the `read-screen` call ONLY; keep it on `send`/`send-key`.
2. Every caller of `nudge-pane.sh` must export `RUN_ROOT` (observed failure when unset).
3. No background shell processes inside Claude sessions may be relied on (harness SIGTERMs
   them ~every 30 min). The runner is a normal terminal/launchd process — durable. Nothing in
   a worker brief may say "start a background watch."
4. The S6/L1 mutate-validation guard in `state.py` is already present — keep it and its test.
5. Rebase is not built anywhere. Branch updates are **merge-based universally** (see §C.4).

## B. Locked decisions (made in this plan; William approves the plan, not each one separately)

1. **One process.** Watcher and actor merge into one deterministic `runner.py` per repo. The
   watcher's event loop is the runner's sensing pass; the act pass replaces the orchestrator.
2. **One installed skill.** `~/.claude/skills/superlooper/` with `SKILL.md` routing to
   `references/issue-writing.md`, `references/approval-protocol.md`, `references/runner-ops.md`.
   (Not two skills: one name, one install, the references load on demand.)
3. **Publish = explicit install script**, never a symlink. `bin/install.sh` rsyncs `skill/` →
   `~/.claude/skills/superlooper/`, stamps `VERSION` (git SHA + date), and registers the two
   activity hooks in `~/.claude/settings.json` (they no-op unless `SL_ISSUE_ID`/`SL_RUN_ROOT`
   are set, exactly like autocode's). Rationale: a symlink would publish half-finished edits
   instantly to live sessions and a running loop; superlooper will eventually dogfood itself,
   so dev churn must not hit the installed copy. (User-scope install of the skill + hooks
   is justified under William's install rule: the loop is cross-project by design, and the
   hooks are strict no-ops outside loop sessions.)
4. **Merge-based branch updates everywhere, no rebase machinery, no force-push path at all.**
   The spec's §4.4 ladder step 1 ("mechanical rebase") is implemented as a mechanical
   **merge-update** (`git merge origin/<dev>`) universally: it is the sanctioned override on
   the eApp (force-push is a bright line there) and costs nothing elsewhere because final
   merges are squash — dev history stays clean either way.
5. **Env/naming:** env prefix `SL_` (`SL_RUN_ROOT`, `SL_ISSUE_ID`, `SL_PANE`, `SL_CMUX`,
   `SL_LAUNCH_DIR`…). Issue id `i<N>` (e.g. `i123`). Branch `sl/i<N>-<slug>`. Launch-shim dir
   `~/.superlooper/launch` (coexists with autocode's — different marker dir, both shims are
   strict no-ops otherwise). State home per repo: `~/.superlooper/<owner>__<repo>/`.
6. **CLI:** one entry point `skill/bin/superlooper` (python, argparse) with subcommands
   `adopt`, `doctor`, `run`, `status`, `nightly`, `morning-report`, `promote-report`,
   `accept-failure`. William-facing; friends get the same.
7. **Ordering mechanism for "William's priority order":** labels `priority:high` /
   (none = normal) / `priority:low`. Sort key: `expedite` > priority band > conflict-requeued
   flag > oldest-first. `expedite` IS the queue-bypass lane William asked for (owner ruling
   2026-07-02): an expedited issue is slotted into the very next free lane ahead of
   everything, exactly as spec §4.1 defines it.
8. **Notifications: iMessage, from William's own Mac** (owner ruling 2026-07-02: "text me").
   The runner is not a Claude session, so it cannot use the PushNotification tool; instead a
   tiny osascript wrapper drives the Mac's Messages app to text William's number — free, no
   accounts, one one-time macOS "allow automation" click. Config: `notify.imessage_to`
   (primary), `notify.cmd` (generic escape hatch), else `cmux notify`, else log-only.
9. **No headless `claude -p` anywhere** (owner ruling 2026-07-02: print-mode may be billed as
   extra usage in the future). Workers AND answerers run as normal interactive sessions
   through the same delivery-verified launch stack, all on the subscription's ordinary
   interactive path.

## C. The contracts (interfaces everything else is built against)

### C.1 Per-repo config — `.superlooper/config.json` (in the target repo)

```json
{
  "version": 1,
  "repo": "owner/name",
  "dev_branch": "main",
  "prod_branch": null,
  "lanes": 2,
  "affinity": "hard",
  "areas": {
    "frontend": ["src/components/**", "src/styles/**"],
    "api": ["src/api/**", "src/server/**"],
    "db": ["migrations/**", "src/db/**"]
  },
  "touches_required": true,
  "required_checks": ["review/local-gate", "quality-gate"],
  "merge_method": "squash",
  "ship_cmd": null,
  "ship_recheck_cmd": null,
  "report_required_sections": ["Tests", "Browser evidence", "Regression tests", "Review"],
  "bright_lines": [],
  "models": { "worker": "opus", "answerer": "fable" },
  "session": { "idle_seconds": 480, "freeze_seconds": 2700,
               "retry_cap": 2, "conflict_cap": 2 },
  "qa": { "nightly_cmd": null, "results_glob": null,
          "retry_once": true, "quarantine": [],
          "nightly_time": "02:00" },
  "cleanup_merged_worktrees": true,
  "notify": { "imessage_to": null, "cmd": null },
  "report_time": "08:45"
}
```

Semantics the code must honor:
- `affinity: "hard"` — two issues co-schedule only if declared `touches` areas are disjoint;
  `"soft"` — overlap allowed but journaled. A path matching no `areas` glob maps to the
  wildcard area `*`, which overlaps everything under hard affinity.
- `ship_cmd` — if set, worker briefs say "ship EXCLUSIVELY via this command" (eApp:
  `scripts/ship.sh`); if null, brief says push branch + `gh pr create` with `Closes #N`.
- `ship_recheck_cmd` — run by the runner from the worktree after a merge-update to re-post a
  diff-pinned gate (eApp: ship.sh reuses its verdict for an identical diff — no model). Exit
  0 → proceed; nonzero → **park** (never coach around a fail-closed gate).
- `bright_lines` — prose lines injected verbatim into every worker brief (the eApp adaptation
  fills these; the skill never hardcodes any).
- eApp-specific facts (Render-parity migration role, SSN/bank restricted journeys, cascade
  behavior) live ONLY in the eApp's own config + adaptation doc, expressed through these
  fields (`bright_lines`, `qa.nightly_cmd`, `ship_cmd`, …).

### C.2 Issue metadata contract (what the issue-writing skill emits, what the parser reads)

Labels: `type:build` | `type:investigate` | `type:diagnose-and-fix` (exactly one);
`agent-ready`, `in-progress`, `needs-william`, `parked`, `expedite`, `preserve`,
`auto-approved:nightly-red`, `superseded` (PRs), `priority:high`, `priority:low`.

Issue body sections (H2 headings, parsed mechanically):

```markdown
## Goal
<durable intent — never asserts current code facts; points at where truth lives>

## Definition of done
- [ ] <machine-checkable where possible>

## Boundaries
<what this issue must NOT touch / decide>

## Loop metadata
touches: frontend, api
blocked-by: #41, #52
parent: #40
```

(`parent:` is set only on investigation children — it is how the runner mechanically counts
a parent's children and how the morning report groups them.)

Rules the issue-writer enforces (and the parser tolerates missing only where stated):
`touches:` mandatory when config `touches_required` (the eApp), optional elsewhere;
`blocked-by:` is a smell that must be justified in the Goal; **cross-PR promises become
issues, never code comments** (the #1 systemic miss of the autocode runs); agents never edit
an approved issue's Goal/DoD — reconciliation appends comments only.

### C.3 Session signals + report contract (ported EVENT-MODEL, per issue id)

Disk layout under `~/.superlooper/<owner>__<repo>/`:

```
state/
  runner.lock            # pidfile singleton
  runner.heartbeat       # epoch, every tick (external-watchdog visible)
  ALERT                  # exists while an alarm is active (JSON names the reason)
  merges_frozen.json     # fix-forward freeze marker {reason, fingerprint, since}
  issues.json            # counters: launches/retries/conflicts/lane/branch per issue (loopstate)
  activity/<id>          # liveness stamps (hooks)
  started/<id>.<token>   # per-launch delivery proof
  blocked/<id>           # worker's plain-text question
  exited/<id>            # process-gone marker (rc)
  awaiting/<id>          # suppress idle peek during long background work
  panes/<id>[.ws]        # cmux surface/workspace uuids
worktrees/i<N>/          # one per in-flight issue
briefs/i<N>.md           # the session's entire brief
reports/i<N>.md          # worker's final report (existence = finished)
answers/i<N>.md          # answerer transcripts
ledger.json              # known-failure ledger (content-fingerprinted)
journal.jsonl            # append-only record of every runner action
reports/morning-YYYY-MM-DD.md
reports/promotion-YYYY-MM-DD.md
logs/runner.log
```

Worker report (`reports/i<N>.md`) must contain the config's `report_required_sections` as H2
headings — the runner checks their presence mechanically as part of the gate. Blocked =
`state/blocked/<id>` with the question. `resolved = report ∨ blocked ∨ exited` by marker
EXISTENCE (never mtime comparison — the founding EVENT-MODEL explains the P0 this prevents).

### C.4 The gate + merge state machine (per finished issue)

```
finished (report exists)
  → investigate-type (mechanical contract — cross-review C1): an issue comment beginning
    `<!-- superlooper-investigation -->` exists → close parent, done (children are counted
    via their `parent: #N` metadata and journaled; zero children is legal — "nothing to do"
    is a valid root cause). Report exists but marker comment missing → one nudge, then park.
  → build/diagnose-and-fix:
    1. PR exists (branch sl/i<N>-*, "Closes #N")?         no → park (memo)
    2. report has required sections?                       no → one nudge, then park
       ("Review" is a default section: what was reviewed + P0/P1 outcome)
    2b. review evidence, MECHANICAL (the fresh-agent-review standing rule must never be
        an LLM-remembered duty): either the repo's own pipeline owns review (`ship_cmd`
        set — e.g. ship.sh's diff-pinned review/local-gate) OR a fresh-agent review
        verdict exists as a PR comment beginning `<!-- superlooper-review -->`.
        Missing → one nudge, then park.
    3. touch verification: gh pr diff --name-only → areas
       - wander (actual ⊄ declared) → journal + morning report
       - actual overlaps an in-flight lane → HOLD this merge until that lane resolves
    4. merges frozen? → hold (journal once)
    5. required_checks green?
       - PENDING → wait (poll)   - FAIL → hand back once (nudge), then park
    6. mergeable?
       - CONFLICTING → conflict ladder:
         a. worktree: git fetch; git merge origin/<dev>
            clean → ship_recheck_cmd (if set; nonzero → park) → plain push (ff)
                  → wait checks → merge
         b. real conflict → git merge --abort → REGENERATE:
            label PR `superseded` + comment (branch preserved on the REMOTE, PR left open —
            nothing auto-closed); comment issue "conflicted with #M — rebuilding on current
            <dev>"; conflicts += 1; if conflicts ≥ conflict_cap → needs-william + memo;
            else relabel agent-ready with requeue-front flag. Rebuilds use a SUFFIXED branch
            `sl/<id>-<slug>-r<generation>` via `brief.branch_for(generation)` (wave-3
            ruling: the superseded PR keeps its branch, GitHub refuses a second PR on the
            same head, and no force-push exists — so a fresh generation gets a fresh head).
            CONTRACTUAL relaunch hygiene
            (cross-review M1): the runner removes the stale worktree so the rebuild starts
            fresh from current <dev>, and launch-session.sh's per-id marker cleanup (report/
            blocked/exited — the ported restart-hygiene block) is what prevents the OLD
            report from false-gating the rebuilt run; simulation-tested (Task 15)
         c. `preserve` label on the PR → instead of (b): launch a conflict-resolution
            SESSION in the PR's own branch, then every gate re-runs
    7. squash-merge; close issue happens via "Closes #N"; comment cross-links; labels off;
       journal; worktree cleanup if configured
post-merge: poll dev required checks on the new head
  red → freeze merges + auto-file fix issue ONCE per fingerprint:
        labels: type:diagnose-and-fix, agent-ready, auto-approved:nightly-red, expedite
        body: scoped strictly to RESTORING GREEN
  green → unfreeze
```

The runner never resolves conflicts, never force-pushes, never posts a status by hand, and
never converts an owner-only decision into an autonomous one. Frozen-but-building is the safe
idle state.

## D. File structure

```
superlooper/                        # this repo (source of truth, local-only)
├── CLAUDE.md  README.md  PLAN-2026-07-02-superlooper-v1.md
├── bin/install.sh                  # the publish step (repo → ~/.claude/skills/superlooper)
├── docs/
│   ├── founding/…                  # constitution (already committed)
│   └── ADOPTING.md                 # the config-contract doc for any repo (friends included)
├── skill/                          # the publishable payload
│   ├── SKILL.md
│   ├── references/{issue-writing.md, approval-protocol.md, runner-ops.md}
│   ├── templates/{config.example.json, brief-footer.md, answerer-brief.md,
│   │              launchd.runner.plist, launchd.nightly.plist}
│   ├── bin/{superlooper, runner.py, launch-session.sh, start-session.sh,
│   │        nudge-pane.sh, pretrust.sh, activity-hook.sh, stop-hook.sh,
│   │        install-launch-shim.sh, imessage-notify.sh}
│   ├── lib/{config.py, issues.py, gh.py, scheduler.py, gate.py, events.py,
│   │        loopstate.py, actions.py, gitops.py, brief.py, ledger.py, report.py,
│   │        notify.py, journal.py, sanitize.py, pane_state.py, usage.py}
│   └── shell/launch-shim.zsh
└── tests/                          # pytest; NOT published
    ├── fakes/{fake-gh, fake-cmux, fake-claude}   # executable stubs for e2e simulation
    └── test_*.py
```

Design rule (ported from autocode): **every decision is a pure function in `lib/`**, unit-
tested without cmux/GitHub/subprocesses; `runner.py` and the bash scripts are thin I/O shells.

---

## Tasks

Tasks are ordered; 1–5 are pure-python and parallelizable after 1; 6+ depend on earlier ones.
Every task: test-first, run the test, implement, run green, commit. Commit messages
`feat(<area>): …` / `port(<area>): …`.

**Session schedule (owner-approved 2026-07-02; ≤2 parallel lanes, each lane in its own git
worktree, lanes only ever run when their file sets are disjoint):**

| Wave | Lane A | Lane B (parallel) |
|---|---|---|
| 1 | Tasks 0–5 (Opus) — foundation, nothing can parallelize | — |
| 2 | Tasks 6–7 (Opus) — launch machinery + brief (shell + brief.py) | Tasks 8–9 (Fable) — events + gate (pure python, disjoint) |
| 3 | Task 10 (Fable) — the runner | Tasks 13–14 (Opus) — skill docs + install (disjoint) |
| 4 | Tasks 11–12 (Opus) — reports + nightly (share files with T10 output) | — |
| 5 | Task 15 (Fable) — rehearsal harness | — |
| 6 | Task 16 (with William) → then Task 17 (eApp-context session, Fable) | — |

William is the router between lanes: each wave = paste the kickoff(s), collect the report(s),
bring them to the orchestrating session for reconciliation before the next wave launches.

**Review close-out (owner directive 2026-07-02, CLAUDE.md):** this repo has no built-in
review pipeline, so every task ends with a cross-review by an agent that wrote none of the
code — `/cross-review` (Codex) by default, a fresh subagent reviewer as fallback. Fix P0/P1
before the task's final commit; at most 2 review/fix rounds, then consolidate for William.
Every review prompt names this repo's two proven defect classes (found repeatedly across
Waves 1–2): shared mutable defaults, and fail-OPEN on wrong-TYPED (not just missing) input.

### Task 0: Scaffold + toolchain

**Files:** `.gitignore`, `pyproject.toml`, `tests/conftest.py`, empty package dirs.

- [ ] `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.tmp`
- [ ] `pyproject.toml` mirroring autocode's (pytest config only; no runtime deps)
- [ ] `python3 -m venv .venv && .venv/bin/pip install pytest`
- [ ] `tests/conftest.py` adds `skill/lib` and `skill/bin` to `sys.path`
- [ ] Verify: `.venv/bin/pytest` reports "no tests ran" cleanly. Commit `chore: scaffold`.

### Task 1: Pure-core ports (sanitize, pane_state, loopstate, usage, hooks)

**Files:** `skill/lib/{sanitize,pane_state,loopstate,usage}.py`,
`skill/bin/{activity-hook.sh,stop-hook.sh}`, `tests/test_{sanitize,pane_state,loopstate,usage}.py`

- [ ] Port `tests/test_sanitize.py` + `lib/sanitize.py` verbatim. Green.
- [ ] Port `tests/test_pane_state.py` + `lib/pane_state.py` verbatim — KEEP the WS1 NBSP-idle
      regression cases and the orchestrator param (harmless; answerer delivery uses
      fail-closed classification for unreadable screens). Green.
- [ ] Port `lib/state.py` → `skill/lib/loopstate.py`: keep `save/load/_acquire/_release/update`
      + the S6 guard **unchanged except** the guard key becomes `"issues"`; replace the run/PR
      schema with:

```python
VALID = ["ready", "running", "blocked", "frozen", "exited",
         "gating", "holding", "merged", "parked", "needs_william", "bounced"]
DEFAULT_ISSUE = {"status": "ready", "branch": None, "lane": None,
                 "launches": 0, "retries": 0, "conflicts": 0,
                 "requeue_front": False, "declared_touches": [], "pr": None}
def new_state(): return {"version": 1, "issues": {}}
```
      Drop `due_for_rotation`, `rotation_baseline_token`, `run_complete*`, `held_ids`,
      `render_status` (status rendering moves to `report.py`). Port the lock/update/S6 tests;
      delete rotation tests. Green.
- [ ] Port `bin/usage.py` → `skill/lib/usage.py` + its test; env prefix `SL_`. Green.
- [ ] Port both hooks with `PR_ID→SL_ISSUE_ID`, `RUN_ROOT→SL_RUN_ROOT`. Commit
      `port(core): sanitize, pane_state, loopstate, usage, hooks`.

### Task 2: Config contract

**Files:** `skill/lib/config.py`, `skill/templates/config.example.json`, `docs/ADOPTING.md`,
`tests/test_config.py`

- [ ] Write failing tests: valid example loads with defaults filled; unknown keys rejected;
      `affinity` outside {hard,soft} rejected; `lanes < 1` rejected; missing file → clear
      error naming the path; `areas` globs compile; defaults match §C.1 exactly.
- [ ] Implement `config.py`: `load(repo_path) -> dict` (schema-validated by hand — stdlib
      only, no jsonschema dep), `path_to_area(config, path) -> str` (fnmatch against `areas`,
      first match wins, else `"*"`), `state_home(config) -> Path`
      (`~/.superlooper/<owner>__<repo>/`, override `SL_HOME`).
- [ ] `config.example.json` = §C.1 verbatim. `docs/ADOPTING.md`: every field, its semantics,
      the label set, the branch-protection recommendation (drop strict up-to-date; keep the
      repo's required checks — on the eApp `review/local-gate` stays required and
      diff-pinned), the adopt/doctor walkthrough.
- [ ] Green; commit `feat(config): per-repo contract + ADOPTING.md`.

### Task 3: Issue model + ordering

**Files:** `skill/lib/issues.py`, `tests/test_issues.py`

- [ ] Failing tests first, covering: body parsing (Goal/DoD/Boundaries/Loop metadata,
      `touches:`, `blocked-by: #41, #52`), type extraction from labels (exactly one `type:*`
      else `invalid`), the full sort key, blocked-by eligibility, and the paid-for regression:
      a dependency chain where `blocked-by` holds two issues behind a parked one (they must
      stay ineligible, never crash the tick).
- [ ] Implement pure functions:

```python
def parse_issue(gh_issue: dict) -> dict
    # -> {"num", "id" ("i123"), "title", "type", "labels", "touches": [...],
    #     "blocked_by": [41, 52], "created_at", "priority": 1|2|3, "expedite": bool}
def eligible(parsed, closed_issue_nums: set, frozen: bool) -> bool
    # agent-ready ∧ valid type ∧ all blocked_by ⊆ closed  (freeze only stops MERGES, not builds)
def sort_key(parsed, requeue_front: bool) -> tuple
    # (not expedite, priority, not requeue_front, created_at)
```
- [ ] Green; commit `feat(issues): metadata parser + queue ordering`.

### Task 4: GitHub adapter + fakes

**Files:** `skill/lib/gh.py`, `tests/fakes/fake-gh`, `tests/test_gh.py`

- [ ] `gh.py`: one thin `_run(args, timeout=30)` subprocess wrapper (hard timeout, captured
      stderr, never raises into the tick — returns `(rc, stdout)`); pure JSON parsers above
      it: `ready_issues()`, `issue(num)`, `set_labels(num, add, remove)`, `comment(num, body)`,
      `create_issue(title, body, labels)`, `pr_for_branch(branch)` (state, mergeable,
      statusCheckRollup, files), `pr_comments(num)`, `issue_comments(num)`,
      `child_issues(parent_num)` (issues whose Loop metadata carries `parent: #N`),
      `merge_pr(num, method)`, `branch_checks(branch)`, `compare(base, head)`. Binary override `SL_GH` (tests point it at fake-gh).
- [ ] `tests/fakes/fake-gh`: executable python script backed by a JSON fixture dir
      (`GH_FIXTURES` env): serves canned `gh issue list/view`, `gh pr view/diff/merge`,
      `gh api` responses and RECORDS mutations (labels, comments, merges) to
      `$GH_FIXTURES/mutations.jsonl` for assertions. This is the harness Task 15 reuses.
- [ ] Tests: parser correctness on captured real `gh --json` shapes (grab once via real gh
      against any public repo, commit as fixtures); timeout path; nonzero-rc path returns
      empty-but-typed results (fail closed = act on nothing).
- [ ] Green; commit `feat(gh): adapter + fake-gh harness`.

### Task 5: Scheduler (lanes + anti-affinity + usage gates)

**Files:** `skill/lib/scheduler.py`, `tests/test_scheduler.py`

- [ ] Failing tests: usage fail-closed cases ported from autocode's `test_scheduler.py`
      (missing pct / stale / bad auth → launch nothing; >90% five-hour → none; >96%
      seven-day → none); lanes math; hard affinity blocks overlapping `touches` and the
      wildcard `*`; soft affinity allows-but-flags; expedite jumps; requeue_front ordering.
- [ ] Implement:

```python
def launchable(parsed_issues, lane_state, config, usage, closed_nums, frozen) -> list[dict]
    # returns issues to launch NOW, respecting lanes, eligibility, affinity vs the
    # declared_touches of currently RUNNING lanes, and the usage ceilings (ported constants
    # FIVE_HOUR_LAUNCH_CEILING=90, SEVEN_DAY_NEW_WORK_CEILING=96)
def overlaps(touches_a, touches_b, affinity) -> bool
```
- [ ] Green; commit `feat(scheduler): lanes, anti-affinity, usage fail-closed`.

### Task 6: Launch machinery port (shim, pretrust, start/launch-session, nudge-pane)

**Files:** `skill/shell/launch-shim.zsh`, `skill/bin/{install-launch-shim.sh, pretrust.sh,
start-session.sh, launch-session.sh, nudge-pane.sh}`, `tests/test_{launch_shim,
install_shim, launch_delivery, nudge_pane}.py`

- [ ] Port the shim + installer: rename marker dir to `~/.superlooper/launch`
      (`SL_LAUNCH_DIR`), function `_superlooper_launch_shim`, wait-ticks env
      `SL_SHIM_WAIT_TICKS`. Port `test_launch_shim.py`/`test_install_shim.py`. Green.
- [ ] Port `pretrust.sh` verbatim.
- [ ] Port `start-pr.sh` → `start-session.sh`: same worker-singleton (`ln` atomic lock),
      same per-launch start sentinel + exited marker; brief path `$SL_RUN_ROOT/briefs/$ID.md`;
      launch line becomes
      `claude --dangerously-skip-permissions --model "$SL_MODEL" --name "$NAME" --remote-control "$NAME" "$(cat "$BRIEF")"`.
- [ ] Port `launch-pr.sh` → `launch-session.sh`: identity/branch comes from
      `state/issues.json` via `loopstate` (not plan.json); worktree
      `$SL_RUN_ROOT/worktrees/$ID` created from `origin/<dev_branch>`; same
      marker-hygiene / .active / cmd-drop / 30s delivery-verify / orphan-tab close / launch
      counter stamping (against `issues.json`). Keep every WHY comment. Second mode
      `--cwd <dir>` (no worktree, no branch) for answerer sessions (`a<N>` ids — Task 10);
      `sanitize.worktree_id` must accept both `i<N>` and `a<N>`. Port
      `test_launch_delivery.py` (stub cmux + tmp dirs — the proven pattern).
- [ ] Port `nudge-pane.sh` with **port fix 1** (no `--workspace` on read-screen) and drop the
      orchestrator branch (no orchestrator exists; the `orchestrator=` param stays in
      `pane_state.py` unused-but-tested). Callers export `SL_RUN_ROOT` (port fix 2). Port
      `test_nudge_pane.py`; add a regression test asserting the read-screen invocation
      carries no `--workspace` while send does.
- [ ] Green; commit `port(launch): delivery-verified keystroke-free launch stack`.

### Task 7: Brief builder (the worker's entire world)

**Files:** `skill/lib/brief.py`, `skill/templates/brief-footer.md`, `tests/test_brief.py`

- [ ] `brief.py: build(parsed_issue, config) -> str` = issue body verbatim (Goal/DoD/
      Boundaries are William-approved text — never rewritten) + rendered footer.
- [ ] `brief-footer.md` (template vars `{issue_num}`, `{dev_branch}`, `{ship_instructions}`,
      `{bright_lines}`, `{report_path}`, `{report_sections}`, `{blocked_path}`,
      `{awaiting_path}`, `{branch}`) — the load-bearing text, drafted here, tuned at review:

```markdown
---
# Loop contract (mechanical — the runner reads files, not your words)

**Step 0 — reconcile (MANDATORY FIRST STEP).** Read the issue above against CURRENT
`origin/{dev_branch}`. Small drift (a stale pointer, a renamed file): proceed and append a
one-line note to issue #{issue_num}. Premise-level drift (problem gone, approach invalidated):
STOP — write {blocked_path} beginning `BOUNCED:` with a one-line explanation PLUS a
ready-to-approve proposed amendment to the Goal/DoD, then end your session. The RUNNER (not
you) posts that memo to the issue and moves the labels — you touch no labels and never edit
the issue's Goal/DoD yourself.

**Build.** Work only in this worktree on branch `{branch}`. TDD. Stay inside the issue's
Boundaries. If you discover work this issue shouldn't do, file it as a NEW issue labeled
`needs-william` (cross-PR promises become issues, never code comments).

**Ship gate (all of it, before you finish):**
1. Your tests pass.
2. Drive the changed feature in a REAL browser; record what you drove and what you saw.
3. Add/update regression tests covering what you built.
4. {ship_instructions}
5. CI green on your PR.

{bright_lines}

**Blocked?** Write your single, specific question to {blocked_path} and end your turn. A
fresh answerer will reply into this session. If you can safely proceed on one reasonable
assumption, prefer stating it in the PR body over blocking.

**Long background wait?** touch {awaiting_path} first, remove it when you resume.

**Finish.** Open the PR with `Closes #{issue_num}` (unless shipping via the configured ship
command, which does this). Then write {report_path} with EXACTLY these H2 sections:
{report_sections} — the runner mechanically checks them. The report is your last action.
Never force-push. Never hand-post a commit status. Never label anything `agent-ready`.
```
- [ ] `ship_instructions` rendering: with `ship_cmd` → "Run the repo's own review pipeline and
      ship EXCLUSIVELY via `<ship_cmd>` — never direct `git push` to {dev_branch}, never
      `gh pr merge`, never a hand-posted status."; without → "Get a fresh-agent review of
      your diff (an agent that wrote none of it), address P0/P1 findings, push the branch,
      `gh pr create --fill --body 'Closes #{issue_num}'`, then post the reviewer's verdict
      as a PR comment beginning `<!-- superlooper-review -->` naming what was reviewed and
      the P0/P1 outcome — the runner mechanically refuses to merge without this comment."
- [ ] Type variants: `investigate` footer replaces the ship gate with "produce a root-cause
      report as an issue comment BEGINNING `<!-- superlooper-investigation -->` (the runner
      closes the parent only when that marker comment exists) + scoped child issues, each
      carrying `parent: #{issue_num}` in its Loop metadata and labeled `needs-william`,
      zero PRs"; `diagnose-and-fix` adds "if the root cause exceeds
      the issue's Boundaries [eApp: or touches any bright-line area], SPLIT: file child
      issues instead of fixing, comment the diagnosis, no PR."
- [ ] Tests: each type renders; bright lines injected; approved body passes through
      byte-identical. Green; commit `feat(brief): issue→session brief`.

### Task 8: Event sensing port — **exec model: Fable** (owner ruling 2026-07-02)

**Files:** `skill/lib/events.py`, `tests/test_events.py`

- [ ] Port from `watcher.py`'s pure core: `_hash_file`, `detect_events`, `_event_key`,
      `emitted_from_events`, `reconcile_emitted`, `next_seq`, `retry_runaway`, and the
      snapshot helper (`_snapshot`, reads the §C.3 markers) — DROP `rotation_*`, `ring_*`,
      `stall_*`, ship-token machinery (PR status is polled directly by the runner via gh; no
      `state/ship/*.json` relay needed when sensor and actor are one process).
- [ ] Port the matching `test_watcher.py` cases: marker-EXISTENCE resolution (the false-idle
      P0), content-hash dedup (identical rewrite doesn't re-fire), dedup un-latch on marker
      removal, idle→frozen tier edges with `awaiting` suppression, restart rebuild
      (`emitted_from_events` + `reconcile_emitted`).
- [ ] Green; commit `port(events): file-signal sensing core`.

### Task 9: Gate + git mechanics (pure decisions, then thin I/O) — **exec model: Fable**

**Files:** `skill/lib/gate.py`, `skill/lib/gitops.py`, `tests/test_gate.py`,
`tests/test_gitops.py`

- [ ] `gate.py` (pure; failing tests first, one per §C.4 numbered step):

```python
def report_sections_ok(report_text, required) -> bool
    # every required H2 present AND carries non-empty prose (≥40 non-whitespace chars) —
    # empty headings must never merge (cross-review C3); negative-tested
def review_evidence_ok(config, pr_comments) -> bool
    # ship_cmd set → True (the repo pipeline owns review, e.g. eApp's review/local-gate);
    # else any PR comment starts with "<!-- superlooper-review -->"
def investigation_done(issue_comments) -> bool
    # any comment starts with "<!-- superlooper-investigation -->" (cross-review C1)
def touch_verdict(declared, actual_areas, inflight: dict[str, list]) -> dict
    # {"wander": bool, "overlap_lane": str|None}
def gate_decision(issue_state, pr_view, report_text, config, frozen, inflight) -> dict
    # {"action": "merge"|"update"|"wait"|"hold"|"nudge"|"park"|"regenerate"|"close_investigate",
    #  "reason": str}   — the §C.4 state machine as a table-driven pure function.
    # GitHub computes mergeability ASYNC: mergeable UNKNOWN/null → "wait", never treated
    # as conflict/update/merge (cross-review M2); regression-tested
def fix_issue_fingerprint(check_name, summary) -> str   # normalize + sha256, L7-style
```
- [ ] `gitops.py` (thin, everything `-C <worktree>`, hard timeouts, rc-checked):
      `fetch`, `merge_update(dev_branch) -> "clean"|"conflict"` (aborts on conflict),
      `plain_push`, `worktree_add/remove`. Tests run against a THROWAWAY local git repo
      built in tmp (init two clones, diverge, assert clean-vs-conflict paths and that no
      command line ever contains `--force`).
- [ ] Green; commit `feat(gate): mechanical ship gate + merge-update ladder`.

### Task 10: Actions layer + the runner daemon — **exec model: Fable**

**Wave-2 contract addenda (binding — the merged code already implements these):**
- `gate.gate_decision` returns an additional action `"resolve_conflict"` (§C.4 step 6c, a
  `preserve`-labeled PR) and result fields `nudge_key`, `wander`, `overlap_lane`,
  `needs_william` — read its docstring for the exact view contract the runner must assemble.
- `gitops.merge_update` returns `"error"` for infrastructure failures (network/timeout) —
  the runner treats that as wait+journal (ALERT if persistent), NEVER as a conflict (a blip
  must not trigger a false regenerate).
- `brief.branch_for()` is the single source of truth for `sl/<id>-<slug>` branch names — the
  runner reuses it, never re-derives.
- The launch scripts expect this env contract, set by the runner:
  `SL_RUN_ROOT, SL_REPO, SL_PANE, SL_DEV_BRANCH, SL_MODEL`.

**Files:** `skill/lib/actions.py`, `skill/lib/journal.py`, `skill/bin/runner.py`,
`skill/bin/superlooper`, `skill/templates/launchd.runner.plist`, `tests/test_actions.py`

- [ ] `journal.py`: `append(state_home, record)` (jsonl, epoch-stamped, atomic) + reader.
      Every action the runner takes is journaled — this is the decision-log discipline
      mechanized, and the morning report + future ratchet read it.
- [ ] `actions.py` (pure; the runner's brain as data): 

```python
def decide(now, config, usage, parsed_issues, lane_state, events, disk, gh_view) -> list[dict]
    # ordered actions: [{"act": "launch", "id": ...}, {"act": "hire_answerer", ...},
    #   {"act": "recover", "tier": "idle"|"frozen", ...}, {"act": "gate", ...},
    #   {"act": "merge", ...}, {"act": "freeze"|"unfreeze", ...}, {"act": "file_fix_issue", ...},
    #   {"act": "reclaim", ...}, {"act": "park", ...}, {"act": "notify", ...}]
```
      Failing tests first — the scenario table IS the failure model (spec §5): blocked→hire;
      BOUNCED marker→runner posts the memo comment + `needs-william` + removes `in-progress`
      (label mechanics are never the worker's); idle→peek-nudge;
      frozen+dead-pane→relaunch(retries+1); retries==retry_cap→park;
      orphaned in-progress (no live session, no lock) with open PR→relaunch same branch,
      without PR→relabel agent-ready; red dev→freeze+file-once (fingerprint dedup);
      green→unfreeze; usage stale→launch nothing but everything else proceeds; runner
      restart→rebuild purely from GitHub+disk (feed `decide` a cold state and assert it
      reconstructs); nightly-red fix issue carries EXACTLY the standing-rule labels;
      **notify firing is asserted per scenario** (William's standing notification rule —
      every transition to `parked` or `needs-william`, every freeze, and every ALERT must
      emit a notify action; a scenario where one of these occurs without a notify FAILS).
- [ ] `runner.py`: the ~15s tick shell — heartbeat; refresh usage (cache 60s); poll GitHub
      (issues+PRs, ~90s cadence, hard timeouts, budget-capped like `poll_ship`); read disk
      snapshot; `events.detect_events`; `actions.decide`; execute each action via the
      Task-6/9 machinery (`launch-session.sh`, `nudge-pane.sh`, `gitops`, `gh`); journal
      everything; pidfile singleton; `state/ALERT` on persistent gh failure / launch runaway
      (port `retry_runaway`, threshold 4) / usage stale >1h; morning report at
      `report_time` (owner ruling: 08:45, Mac-local time = Mountain); SIGTERM → clean exit (in-flight sessions untouched — fail-stopped:
      nothing merges while it's down; restart rebuilds from GitHub+disk).
- [ ] Bounce mechanics are RUNNER-side (cross-review C2 — a label transition must never be
      an LLM-remembered duty): on a blocked marker beginning `BOUNCED:`, the runner posts
      the worker's memo (explanation + proposed amendment, quoted verbatim) as an issue
      comment, applies `needs-william`, removes `in-progress`, journals, notifies, and
      reclaims the lane. The worker only ever writes the marker file. Asserted in the
      Task-10 scenario table AND the Task-15 bounce simulation.
- [ ] Answerer hire — a VISIBLE interactive session, never `claude -p` (locked decision
      B.9, owner billing rule). The runner launches it through the same delivery-verified
      stack (`launch-session.sh` with id `a<N>`, model `models.answerer`, cwd = the state
      home `answers/` dir — NOT a worktree). Its brief (`templates/answerer-brief.md`):
      "you are a fresh senior engineer answering ONE question from a session building
      issue #N; the issue text and question follow; you may READ the worker's worktree at
      <path> but change nothing anywhere; write your answer — decisive, ≤10 lines, or
      `PARK: <why this needs William>` — to `answers/i<N>.md` as your final action, then
      end." Done signal = that file's existence (same marker discipline as workers);
      15-min freeze tier = timeout. Runner then delivers the answer into the worker pane
      via `nudge-pane.sh` and removes `state/blocked/<id>`. `PARK:`/timeout/error → park
      the issue (`needs-william` + memo comment quoting the question). A BOUNCED
      blocked-marker (step 0) skips the answerer entirely. The tab stays open like any
      session (nothing auto-closed).
- [ ] `superlooper` CLI: `run` (foreground tick loop), `status` (render lanes/queue/frozen
      from journal+state), `adopt` (write config template into a target repo, create the
      §C.2 label set via gh, print branch-protection recommendations — print-only in v1),
      `doctor` (gh auth, cmux binary, jq present, shim installed, hooks registered, config
      valid, labels exist — and FAILS HARD when `required_checks` is empty: a repo with no
      CI check enforcing its tests has no mechanical §4.3 gate, so adoption requires at
      least one; cross-review C3. `adopt` prints the same requirement). Plus stubs wired in Tasks 11–12: `nightly`, `morning-report`,
      `promote-report`, `accept-failure`.
- [ ] `launchd.runner.plist` template (KeepAlive=true, label
      `com.superlooper.<owner>__<repo>` — the same slash-safe normalization as the state
      home, logs to state home) + `references/runner-ops.md` section:
      default is a visible `superlooper run` in a cmux tab; launchd is the keep-alive
      option; the external-watchdog contract is `runner.heartbeat` + `ALERT`.
- [ ] Green; commit `feat(runner): deterministic loop daemon + CLI`.

### Task 11: Reports + notifications

**Wave-3 contract addenda (binding — the merged runner already implements these):**
- `journal.jsonl`: one JSON object per line, every record `ts`-stamped. Two kinds:
  `{"ts", "act": "event", "event": {type, id, …}}` and action records = the
  `actions.decide()` dict plus `"outcome"` ("ok" or a reason). Action names the morning
  report consumes: `launch, merge, park` (carries `needs_william` + `memo`), `bounce`
  (memo verbatim), `freeze/unfreeze, file_fix_issue` (fingerprint, labels), `regenerate`
  (new_branch, conflicts), `hold` (overlap_lane), `notify` (title, body), `alert`
  (reasons), `morning_report` (date), `absorb_merged`. Gate-derived actions carry a
  `wander` flag — that is the declared-vs-actual touches metric.
- Plug-in seams: `Runner._exec_notify` (notify.py replaces the logging stub) and
  `Runner._morning_report_hook(date, now)`; the due-date stamp lives at
  `state/last_morning_report`; fix-issue dedup at `state/fix_issues.json`
  (`{fingerprint: issue_num}`).
- **Freeze ownership (wave-4 Codex round 2):** `merges_frozen.json` carries a `source`
  field (`dev-check` | `nightly`). The runner's dev-green unfreeze clears ONLY dev-check
  freezes; a green nightly clears ONLY nightly freezes; a red nightly's freeze is sticky
  (overwrites — worst case the runner re-freezes a dev cause within one tick). Known
  limitation, accepted: single-owner marker can't attribute two simultaneous causes;
  a multi-owner marker is a ratchet candidate, never urgent (merges never wrongly open
  for more than a tick).

**Files:** `skill/lib/{report.py, notify.py}`, `skill/bin/imessage-notify.sh`,
`tests/test_report.py`, `tests/test_notify.py`

- [ ] `notify.py`: `send(config, title, body)` — precedence: `notify.imessage_to` (drive
      Messages via `skill/bin/imessage-notify.sh`, an osascript one-liner sending to that
      number/Apple ID) → `notify.cmd` (template with `{title}`/`{body}`) → `cmux notify` →
      log-only. Never raises; a send failure is journaled, never fatal. Test with a stubbed
      `osascript` on PATH. Document the one-time macOS automation-permission click (and
      that launchd-started runners need it granted too) in `references/runner-ops.md`.
- [ ] `report.py: morning(journal_records, gh_view, ledger, config) -> str` — sections:
      merged (issue/PR links), parked/needs-william (with memos), bounces, **conflict
      regenerations this week** (the §4.2 tuning metric), wanders, gate health (nightly
      pass rate, flake count, quarantine size), freeze state, usage, queue depth + next up.
      Tests: golden-file a representative journal fixture; a quiet night renders "nothing
      happened, queue empty" honestly.
- [ ] Wire `morning-report` subcommand + the runner's `report_time` trigger + notify push.
      All schedule times are Mac-local time (William's Mac runs Mountain time; launchd and
      the runner both read the system clock, so no timezone code is needed).
- [ ] Green; commit `feat(report): morning report + notify adapter`.

### Task 12: Nightly QA, known-failure ledger, promotion report

**Files:** `skill/lib/ledger.py`, nightly/promote logic in `skill/bin/superlooper`,
`skill/templates/launchd.nightly.plist`, `tests/test_ledger.py`, `tests/test_nightly.py`

- [ ] `ledger.py`: fingerprint (test id + normalized failure text: strip digits/timestamps,
      basename paths, collapse ws, first 200 chars → sha256[:16]); `accept(fp, note)`;
      `is_accepted(fp)`; persisted `ledger.json` via loopstate atomic write. Acceptance is
      fingerprinted to CONTENT, never a commit — one approval, ever (L7 generalized).
- [ ] `superlooper nightly`: fresh worktree of `origin/<dev>` → run `qa.nightly_cmd`
      (subprocess, generous timeout, exit code captured) → parse JUnit XML from
      `qa.results_glob` (stdlib `xml.etree`) → failures minus quarantine; if `retry_once`
      and failures: re-run once, intersect (once-only = flake → gate-health stats, never an
      issue); persistent failures minus accepted-ledger → **freeze merges** + file issues
      (dedup: skip if an open issue carries the fingerprint in body) with the standing-rule
      labels `type:diagnose-and-fix, agent-ready, auto-approved:nightly-red, expedite` and a
      body scoped to restoring green. Journal + notify. Tests: fixture JUnit XMLs through
      the full decision (flake vs persistent vs accepted vs quarantined); label-set
      regression test (the §4.4 audit-trail requirement).
- [ ] `launchd.nightly.plist` template: StartCalendarInterval 02:00 (Mac-local time),
      invoking `superlooper nightly --repo <path>`, logs to the state home.
- [ ] `superlooper promote-report`: run the suite (or `--use-latest-nightly`), diff vs
      ledger → NEW failures highlighted, accepted folded away; merges since last promotion
      (`gh compare prod_branch...dev_branch` when `prod_branch` set, else "no prod branch
      configured — see the repo's own promotion checklist"); open-issue summary. Output
      `reports/promotion-YYYY-MM-DD.md` + notify. NO pass/fail verdict anywhere — evidence
      only; William decides (§4.6 is a bright line: no "must pass to promote" logic).
- [ ] `accept-failure <fingerprint> --note "…"` subcommand.
- [ ] Green; commit `feat(qa): nightly loop, ledger, promotion evidence`.

### Task 13: The skill surface (SKILL.md + references + issue-writing discipline)

**Files:** `skill/SKILL.md`, `skill/references/{issue-writing.md, approval-protocol.md,
runner-ops.md}`

- [ ] `SKILL.md` (frontmatter name `superlooper`; description triggers: writing/approving
      loop issues, adopting a repo into the loop, running/operating the loop, morning
      report, promotion): a router — issue writing → `references/issue-writing.md`;
      approvals → `references/approval-protocol.md`; ops/adoption → `references/runner-ops.md`
      + `docs/ADOPTING.md`.
- [ ] `issue-writing.md` — the rigorous format, enforcing (each rule cites its incident):
      §C.2 body format; one `type:*`; thin-issue doctrine (point, never assert — the
      stale-brief incident); DoD machine-checkable where possible; `touches:` mandatory when
      config requires (verified at gate time, so lying wanders get logged); `blocked-by` =
      a smell to justify, prefer re-scoping (sub-1 held sub-4/sub-5 all night);
      **cross-PR promises become issues, never code comments** (the eApp's #1 systemic
      miss); investigation children default `needs-william`; bright-line work always splits
      (config `bright_lines`); issues created via `gh issue create` with labels minus
      `agent-ready`.
- [ ] `approval-protocol.md` — approval-by-conversation verbatim from spec §2: William's
      word IS the approval; the agent then applies `agent-ready` and appends an audit
      comment ("Approved by William in conversation, <date>"); the ONLY standing-rule
      exception is a rule William himself defined, which must carry its own distinct label
      (worked example: `auto-approved:nightly-red`); agents NEVER edit an approved Goal/DoD.
- [ ] `runner-ops.md` — start/stop/status, reading the morning report, `parked` vs
      `needs-william` vs `expedite` vs `preserve` semantics, the freeze state, answering a
      bounce (yes/no on the proposed amendment), launchd setup, the doctor checklist.
- [ ] Review pass: every spec §2 directive traceable into one of these files. Commit
      `feat(skill): SKILL.md + issue-writing + approval protocol + ops`.

### Task 14: Install/publish step

**Files:** `bin/install.sh`, `tests/test_install.py`

- [ ] `install.sh`: rsync `skill/` → `~/.claude/skills/superlooper/` (`--delete`, excludes
      nothing — the payload is already curated); write `VERSION` (git SHA + date); idempotent
      merge of the two hook registrations into `~/.claude/settings.json` via python stdlib
      `json` (NOT jq — cross-review M4; jq remains only inside the ported `pretrust.sh`,
      checked by `doctor`), same atomic tmp+mv and lockfile discipline as `pretrust.sh`,
      skip-if-present; run `install-launch-shim.sh`; print what changed. `--dry-run` flag.
- [ ] Test: install into a temp `HOME`, assert payload + VERSION + hooks JSON + shim line;
      run twice, assert idempotent (no duplicate hooks).
- [ ] Green; commit `feat(install): explicit publish step`.

### Task 15: Offline end-to-end simulation (the acceptance harness) — **exec model: Fable**

**Files:** `tests/fakes/{fake-cmux, fake-claude}`, `tests/test_simulation.py`

- [x] `fake-cmux` (extend autocode's stub-cmux pattern from `test_launch_delivery.py`):
      new-surface/rename/close/send/send-key/read-screen against tmp-dir state, honoring the
      launch-shim contract by executing dropped `.cmd` files in a subshell (simulating the
      shim). `fake-claude`: a script that plays a worker — invoked EXACTLY like the real
      `claude` (the brief CONTENTS arrive as the final argv; `SL_ISSUE_ID`/`SL_RUN_ROOT`
      come from env — cross-review nit 2), then follows a per-test SCENARIO env: `happy`
      (commit, open fake PR via fake-gh mutation, post the review comment, write a report
      with non-empty sections), `no-review` (omit the review comment), `empty-sections`
      (report headings present but bodies empty — must nudge then park, never merge),
      `investigate` (post the `<!-- superlooper-investigation -->` comment + create a
      `parent:`-linked child issue), `blocked` (write blocked marker), `answerer`, `bounce`
      (write `BOUNCED:` marker ONLY — the test asserts the RUNNER does the comment+labels),
      `freeze` (exit without markers), `conflict` (used twice on same-line edits).
- [x] Scenarios (each = one pytest, real runner ticks against fakes + a real local git repo
      pair in tmp): happy-path issue → merged + labels + journal + closed; blocked →
      answerer session (fake-claude in `answerer` scenario writes `answers/i<N>.md`) →
      answer nudged in → resumed → merged; review-evidence gate: happy path posts the
      `<!-- superlooper-review -->` PR comment and merges, a `no-review` scenario omits it
      and must be nudged then parked, never merged; bounce → needs-william + memo intact;
      freeze → relaunch → second freeze → parked at cap; investigate → marker comment +
      child issue → parent closed, child waits on `needs-william`; two overlapping-touch
      issues under hard affinity → sequential; same-line conflict → regenerate (assert the
      stale worktree/report cannot false-gate the rebuild — cross-review M1) → second
      conflict → parked at conflict cap; red dev checks → freeze + fix issue with
      standing-rule labels → green → unfreeze; runner kill -9 mid-run → restart → state
      rebuilt, no duplicate launches (worker singleton), no duplicate fix issues
      (fingerprint dedup); orphaned issue with a PUSHED branch but no PR → requeued fresh
      worker hits the push refusal and must block/park, never lose or clobber the pushed
      work (wave-3 flagged this path as needing a live poke); a GitHub blip mid-park/bounce
      may duplicate a comment (accepted noise, wave-3) — assert it never duplicates a LABEL
      transition or a fix issue.
- [x] Green; commit `test(e2e): offline simulation of the full loop`.
      **This suite is the evidence Task 16 shows William before any live run.**
      (Built 2026-07-03, branch wave5-sim: 32 scenarios, ~2min suite; surfaced + fixed one
      product bug — REST lowercase check conclusions made dev freeze/unfreeze inert.)

### Task 16: Live sandbox dry-run (WITH William — sandbox repo approved, ruling F.3)

- [ ] Create a throwaway PRIVATE GitHub repo (tiny web page + 3–5 line test suite + a
      trivial CI workflow), `superlooper adopt` it, seed 3 approved toy issues (one
      designed to same-line-conflict with another), install the skill, start the runner in
      a cmux tab William can watch.
- [ ] Pre-flight (revised, owner ruling 2026-07-03): cmux's per-session notifications stay
      ON — William uses them to know when sessions finish. The 2026-07-03 "spam burst"
      incident was substantially the real-cmux TEST leak (fixed via the fail-closed
      conftest rule), stacked on normal parallel-session pings. Known tradeoff to observe
      during the dry run: the loop's unattended workers also ping as they finish; if
      overnight noise bothers William, muting is a tuning knob then — superlooper's own
      iMessage channel remains the load-bearing signal path either way.
- [ ] Acceptance (William watches, evidence journaled): 2 clean merges through the full
      gate; 1 conflict-regeneration; 1 blocked→answerer round-trip; morning report renders;
      then a deliberate runner kill + restart mid-issue with clean reclaim; and one launch
      with the DISPLAY OFF/locked (the keystroke-free path's whole reason to exist is only
      provable on a real screen — Wave-2 Lane A flagged this as untestable by stubs).
- [ ] Fix what the dry run surfaces (≤2 review rounds, then consolidate for William).

**Task-16 close-out status (2026-07-05, orchestrator-verified, merged at `b530e39`):** all
acceptance items driven green live EXCEPT two William-gated ones — (a) answerer round-trip
COMPLETION (the machinery worked end-to-end and parked correctly at the 15-min timeout; the
answerer produced nothing because the monthly Fable spend cap was hit — model/limit ruling
needed), and (b) the display-off launch (needs William's hands). Findings D1–D7 recorded in
`docs/DRYRUN-2026-07-03-task16-findings.md`; D1/D3/D4/D6 fixed+reviewed, D5 debunked by
clean repro, D2 deferred (self-texts don't push-notify).

### Task 16b: Operator hardening (Opus; from the dry-run friction findings)

- [x] Startup pane check is FAIL-HARD + loud (D7): a runner that cannot reach cmux or its
      target pane (missing pane, detached/background start, different workspace) refuses to
      start with a clear message — never a quiet warning that silently burns retry caps.
      *(f4f33cb: `runner.preflight_pane()` probes `list-pane-surfaces --pane` read-only, judged
      on a real surface ROW not the rc-0 'Error: not_found' exit code; wired fail-hard into
      cmd_run. Proven against REAL cmux: `pane:54`→ok, bogus/absent→fail-hard.)*
- [x] Re-approving a parked issue resets its retry/launch counters (re-labeling
      `agent-ready` after a park is William's word — the issue gets a fresh cap; the old
      counters are journaled, not lost). *(f4f33cb: new `reapprove` action + `_exec_reapprove`
      — a full CLEAN SLATE mirroring `_exec_regenerate` (zero launches/retries/conflicts/
      launch_failures/answerer counters, clear the report + exited/blocked/awaiting/started
      markers + recheck_failed/update_*/nudged/pr + active answerer record, remove the stale
      worktree), launch held one tick. Codex R1 caught the markers-not-cleared bug; fixed.)*
- [x] Answerer default model per William's ruling — **CONFIRMED 2026-07-06: `opus[1m]`** (latest
      Opus + 1M context window) for BOTH worker and answerer, kept modular via config.models.
      *(`_models()` default + config.example.json + ADOPTING.md. `opus` alias auto-tracks latest
      Opus; `[1m]` opts into the 1M context — verified it survives the launch stack's `%q`
      quoting intact as `claude --model "opus[1m]"`.)*
- [x] Supervise the two remaining Task-16 boxes with William (display-off launch; one
      completed answerer round-trip). **CLOSED 2026-07-06, orchestrator-verified from the
      journal:** display-off launch — #22 launched delivery-verified while the Mac was
      LOCKED, gated, merged; answerer round-trip — #23 blocked → hire_answerer ok →
      deliver_answer ok → resumed → gated → merged; a park notify was delivered via
      iMessage. **TASK 16 IS FULLY CLOSED — V1 acceptance complete; only Task 17 (eApp
      adaptation, in flight) remains.** Earlier partial evidence from the 2026-07-06
      session run:
      - **Clean end-to-end merge on `opus[1m]` — PROVEN.** #7 launched → gate → **merged** to
        `main` (chose the `raise` error-behavior itself and shipped).
      - **Counter-reset fix — PROVEN LIVE.** #7 was stuck at the launch-failure cap; William's
        re-approval reset it and it launched clean (the exact stuck-forever bug the fix cures).
      - **Completed answerer round-trip — NOT driven.** #7 didn't block (opus[1m] is autonomous
        enough to decide the technical question rather than ask). A forcing issue (#21) hit an
        intermittent keystroke-free **delivery flake** (worker never started), which the
        re-approval logic then amplified into a park↔reapprove churn loop → **new bug found +
        fixed** (see D8 below). Answerer *mechanism* remains proven (dry-run fired `hire_answerer`;
        opus[1m] removes the old Fable spend-cap blocker) but the full round-trip is unproven.
      - **Display-off launch — not attempted** this run.
      - **D8 (NEW, found live, FIXED):** the counter-reset `reapprove` could ping-pong with `park`
        on a 90s-stale label cache (park removes `agent-ready` on GitHub; the cached view keeps it
        until the next poll; the next tick re-approves the just-parked issue → reset → relaunch →
        fail → park → loop). `_exec_park` now syncs the cache (`_forget_cached_label`). Codex
        APPROVED. Same stale-cache class as D3/D6.
      - **Operator note:** the runner MUST run in a live, ATTACHED cmux tab — a detached/nohup or
        Bash-spawned runner loses the cmux socket (every `new-surface` → "Broken pipe"), exactly
        as D7 warns. The self-pane auto-detect also degrades after a tab is DRAGGED between
        workspaces (cmux `identify` returns null caller pane); a fresh tab detects fine. Both worth
        a follow-up hardening of `detect_self_pane` (resolve pane from the tab tree, not identify).

**Task 16b fix status (2026-07-06, wave7-hardening `9a75e77` — NOT merged to main):** the three
planned fixes + `opus[1m]` model default + self-pane auto-detect + the D8 reapprove-loop fix, all
done. Full suite **573→607 green**, Codex cross-reviewed across the session (D8 fix APPROVED, no
findings), installed to the live copy (VERSION `9a75e77 2026-07-06`), D7 preflight + counter-reset
+ full pipeline all verified LIVE against real cmux/GitHub/Opus. Remaining, still William-gated: the
completed answerer round-trip and the display-off launch.

### Task 17: eApp adaptation package (build-phase; files land in the EAPP repo, under its
CLAUDE.md gates; collaborative with William)

**Executed by an eApp-context session on Fable, not a superlooper session (owner rulings
2026-07-02; a planning-type session, so Fable also matches the standing model policy).**
The session that writes these files must know how ship.sh, the cascade, and the eApp's
bright-line areas actually work — that knowledge lives in the eApp repo and its
planning/orchestration sessions, not here. Superlooper's contribution is the contract
(`docs/ADOPTING.md` + the config schema); this project hands the eApp session a THIN kickoff
briefing (via the brief skill) pointing at those docs, and the eApp session authors the
config and adaptation doc under the eApp's own CLAUDE.md gates.

- [ ] `.superlooper/config.json` for the eApp: `lanes: 2`, `affinity: "hard"`,
      `touches_required: true`, `required_checks: ["review/local-gate", "quality-gate"]`,
      `ship_cmd`/`ship_recheck_cmd: scripts/ship.sh …` (exact flags read from ship.sh at
      write-time), `report_required_sections` including a restricted-data browser section,
      `bright_lines`: force-push forbidden; ship EXCLUSIVELY via ship.sh; `--human-approved`
      is William-only (P0/owner findings park, never coached around); cascade-engine-path
      changes fail closed → park; migrations must pass as the Render-parity NON-superuser
      role (FIX-0010); the per-PR browser drive MUST exercise RESTRICTED-data paths
      (SSN/bank), never the happy path around them (the SUB-wave lesson).
- [ ] `SUPERLOOPER-ADAPTATION.md` in the eApp: the touches-area taxonomy (drafted from the
      repo tree with William), the branch-protection delta (drop `strict`, KEEP
      `review/local-gate` + `quality-gate` required — verify current protections at
      change-time per eApp answers §6), the promotion checklist skeleton (Gate 2 lands with
      eApp Phase 9; `prod_branch` stays null until then), and the note that QA-EVAL/QA-SUITE
      (nightly_cmd) are eApp roadmap phases this config points at when they exist.
- [x] Nothing in this task modifies the skill. Nothing here builds eApp infra (owned by the
      eApp roadmap). Boundary: this task is planned here but executed only with William, in
      the eApp repo, after V1 Tasks 1–16.

**Task 17 CLOSED (2026-07-06):** executed by an eApp-context Fable session in the eApp repo,
through the eApp's own full gate (PR #108, squash `4791887`, cascade CLEAN). Config validated
against this repo's real loader; branch-protection delta recorded from a live snapshot.
Owner's project-specific choice recorded: the eApp answerer runs on **Fable** (tradeoff
documented — quota exhaustion parks safely). Its sharpest finding — the loop config is
executable data and belongs on a repo's security-review floor — is now a universal note in
ADOPTING.md and item #1 on the eApp adoption checklist. Its bug catch (loader default still
`fable`, overriding the runner's `opus[1m]` fallback on repos that omit `models`) is fixed
at `fa64efb` and republished.

# ★ V1 IS COMPLETE (2026-07-06)

Every task 0–17 closed; 607 tests green; the loop live-proven end to end on the sandbox
(display-off launch, answerer round-trip, conflict-regeneration, kill-9 reclaim, morning
report, iMessage). What remains is V2 territory (spec §8) plus the small deferred items
recorded in the Task-16 close-out and dry-run findings doc.

**Post-V1 addition (2026-07-07, merged + republished at `d9f3bad`):** per-issue
`model:<name>` / `effort:<level>` override labels (William-applied control knobs, like
`expedite`) + repo-wide `models.worker_effort` config default. Precedence: issue label >
repo config > universal default (`opus[1m]`, no effort flag). Answerer untouched; overrides
are durably stamped so relaunches/rebuilds keep them; unknown values fail the launch loudly
and park. `adopt` seeds the starter labels.

---

## E. Spec-coverage self-review

| Spec item | Where |
|---|---|
| §2 approval-by-conversation, label ≠ approval | T13 approval-protocol.md; brief forbids workers labeling |
| §2 universal + thin eApp adaptation | §C.1 contract; T17 isolated in eApp repo |
| §2 collaborative QA build | T16/T17 collaborative; QA suite = eApp roadmap (answers §1) |
| §2 agents write issues via skill | T13 issue-writing.md |
| §2 promotion never mechanical | T12 promote-report: evidence only, no verdict |
| §2 nightly full browser runs + auto-issues | T12 nightly |
| §2 tolerances / don't over-engineer | retry caps 2, park-and-continue, freeze-is-safe-idle |
| §4.1 types, labels, ordering, thin-issue, blocked-by, touches | T3, T13 |
| §4.2 runner, per-repo lanes/affinity, touch verification, reconciliation, per-event judgment, model policy | T5, T7 step-0, T9 touch_verdict, T10 answerer, §C.1 models |
| §4.3 ship gate all-mechanical | §C.4, T9 |
| Standing rule: every review by a fresh non-author agent, mechanically verified | §C.4 step 2b, T9 `review_evidence_ok`, T7 brief, T15 `no-review` scenario; eApp via ship.sh's review/local-gate |
| §4.4 loose merges, fix-forward, red-nightly standing rule, conflict ladder + caps + preserve | §C.4, T10, T12 |
| §4.5 QA layers, flake handling | T12 (per-PR layer = T7/T9; post-promotion smoke = repo checklist, T17) |
| §4.6 ledger, fingerprint durability, no all-green gate | T12 |
| §4.7 ratchet | V1 captures artifacts (journal, T10); ratchet pass itself is V2 per §8 — deliberate non-goal |
| §5 failure modes table | T10 `decide` scenario tests map 1:1 |
| §7 survives/dies | §A tables |
| eApp answers 2 (merge-based + ship.sh recheck) | §B.4, §C.4.6a, T9 |
| eApp answers 3 (fail-closed parks) | T17 bright_lines; gate parks on recheck failure |
| eApp answers 4 (Render-parity role, SSN/bank paths) | T17 |
| eApp answers 5 (cmux realities, no bg watches) | §A port fixes 1–3 |
| eApp answers 7 (cross-PR promises → issues) | T7 footer + T13 issue-writing.md |

**Known deliberate gaps (all V2 per spec §8):** triage/dedup loop, tech-debt sweeper,
Dependabot lane, janitor, the weekly ratchet pass, analytics stack.

## F. Owner rulings (2026-07-02, answering this plan's original open questions)

1. **Priority labels: YES** — `priority:high` / (no label) / `priority:low`. The requested
   "bypass the queue, slotted immediately next" level is the existing `expedite` label
   (spec §4.1); §B.7 records this.
2. **Push channel: RESOLVED — iMessage** (owner: "can we have it text me?"). The runner
   texts William via his Mac's own Messages app (osascript wrapper; free, no accounts;
   one-time macOS automation-permission click). §B.8 + Task 11 carry the design. WhatsApp
   rejected (heavy Meta business-API setup); Twilio SMS rejected (paid account — money
   rule). Notifications remain a convenience layer per spec §5, never a safety layer.
3. **Sandbox repo: APPROVED** — Task 16 may create a throwaway private GitHub repo.
4. **Schedules:** nightly QA **02:00**, morning report **08:45**, Mountain time — encoded as
   Mac-local time in `qa.nightly_time` / `report_time` (§C.1) and the launchd templates.
5. **Publish choice:** install-script decision explained in plain language (a deliberate
   "publish button" copying finished work into `~/.claude/skills/`, vs a live wire where
   half-finished edits instantly hit live sessions). Stands unless William objects.
6. **Task 17 ownership:** the eApp adaptation is authored by an eApp-context session off a
   thin briefing from this project — ruled and folded into Task 17.
7. **No `claude -p` anywhere** (owner, on confirming the headless answerer: print-mode
   "might be billed as extra usage by Anthropic in the future"). Locked decision B.9; the
   answerer is now a visible interactive session (Task 10); also recorded in CLAUDE.md as
   a standing billing rule for this project.
8. **Model upgrades (2026-07-02):** William is funding Fable for the highest-judgment work —
   Tasks 8–10 (the dispatcher brain + gate/merge mechanics), Task 15 (the adversarial
   rehearsal harness), and Task 17 (eApp adaptation, planning-type). All other exec tasks
   stay on Opus with Codex cross-review as backstop.
9. **Codex cross-review, round 1 of 2 (2026-07-02): NEEDS REVISION → all findings applied.**
   C1: the investigate gate wasn't mechanically computable → `<!-- superlooper-investigation -->`
   marker-comment contract + `parent:` metadata + gh functions + simulation scenario.
   C2: bounce label mechanics were an LLM-remembered duty → moved runner-side (worker writes
   only the `BOUNCED:` marker). C3: empty report sections could merge on no-pipeline repos →
   non-empty-section check + `doctor` fails hard on empty `required_checks` + negative test.
   M1 stale-marker/worktree hygiene on regenerate (contractual + tested); M2 mergeable
   UNKNOWN → wait; M3 `imessage-notify.sh` now in file lists; M4 install.sh uses python
   stdlib not jq; M5 stale Task-16 header fixed; both nits (launchd label normalization,
   fake-claude argv contract) fixed. Per the 2-round discipline, fixes are applied without
   a re-review round; the per-task Codex reviews during build are the backstop.
10. **Wave-4 review record (2026-07-03):** session 4 used the fallback subagent reviewer
    (over-cautious about Codex spend, which was in fact pre-authorized); the orchestrator
    ran the missing Codex pass as round 2 → NEEDS REVISION (3 critical: quoted-placeholder
    shell injection in notify.cmd, freeze-ownership cross-clearing, promote-report
    fail-open on wrong-typed cached state; 2 medium). All fixed test-first by the author
    session (+9 regression tests, 518 total), verified and merged. Review closed at the
    2-round cap. Lesson: the same-family round had fixed an injection in the same file
    Codex then bypassed — cross-family review is not optional here.
11. **PLAN APPROVED (2026-07-02)** with three pre-build fixes, all applied: (a) the
   fresh-agent review is now MECHANICALLY gated on every repo — "Review" is a default
   report section AND, on repos without their own pipeline, the gate requires the
   reviewer's `<!-- superlooper-review -->` verdict comment on the PR (§C.4 step 2b) —
   closing the one instruction-only duty the review found; (b) notify firing on
   parked/needs-william/freeze/ALERT is an asserted test scenario (Task 10), so the
   standing notification rule can't quietly not fire; (c) user-scope install noted as
   justified under the install rule (§B.3).
