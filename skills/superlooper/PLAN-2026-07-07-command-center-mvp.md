# PLAN 2026-07-07 — Command-center MVP

**Status 2026-07-07 evening — MVP BUILD COMPLETE (pending i12 + owner joy pass).** The loop
built its own dashboard in one day: issues #1–#11 all merged (PRs 13–23), **zero parks, zero
bounces, zero conflicts, zero wanders — 11 first-attempt landings**; #12 (install story) in
flight. #7 ran on Fable via the model label as ruled. One incident mid-run (binary files in
`reports/` wedged the runner ~42 min, silently): mitigated same-day, no recurrence
(last tick_error 15:03); engine-fix scope for a fresh session in
`docs/INCIDENT-2026-07-07-runner-binary-report-wedge.md` (includes the deferred journal trim).
CLOSEOUT DONE: i12 landed (PR #24, 12/12); owner joy pass 2026-07-07 — ACCEPTED with punch
list; **v0.1 tagged and pushed**. Steady-state maintenance is now tracked ON GITHUB, not in this plan: joy-pass rounds file
issues, William approves by word/label, the loop builds them. (Round 1: #27–#30 approved,
#25/#26 closed after the restart explained them, #30 DoD owner-amended; round 2: #31–#34 +
#36 approved 2026-07-07. `superlooper tidy` [close finished cmux tabs] is with William +
the superlooper orchestrator; its dashboard button waits on that verb existing.)

**Status 2026-07-07 (morning):** Task 0 CLOSED (repo live at github.com/will-titan/command-center,
`tests` check green on main, doctor 8/8, labels created, cross-review findings fixed).
Exec-session deltas accepted: CI Python pinned 3.9; `touches_required: false`;
`report_required_sections: ["Tests","Screenshot evidence","Review"]`; `notify` left null
(owner call pending — see open items); conftest hardened beyond the verbatim port
(unconditional overrides + sentinel guard + SL_OSASCRIPT). Plan vendored into the repo as
`docs/BUILD-PLAN.md`; T1–T12 filed as issues #1–#12 (blocked-by DAG, not a pure chain).
**All 12 approved by William in conversation 2026-07-07** (agent-ready + audit comments).
Branch protection on main: `tests` required, non-strict, force-push blocked, admins exempt.
Notify resolved: `notify.cmd` wrapper (committed) reads the recipient from
`~/.superlooper/notify_to` (NOT in the repo); live-tested end-to-end, "sent via cmd".
**Watch item RESOLVED 2026-07-07:** wave8 per-issue model/effort labels merged (d9f3bad) and
republished (installed VERSION matches); adopt re-run seeded the starter labels; issue #7 now
carries `model:fable`; `models.worker_effort: "xhigh"` set repo-wide in command-center's
config (loader-validated, doctor 8/8). Runner start instructions delivered to William:
`~/.claude/skills/superlooper/bin/superlooper run --repo ~/projects/command-center` inside a
cmux tab (fails hard outside cmux by design; pidfile singleton; Ctrl-C safe — nothing merges
while it's down).

> **Build mode (owner-approved 2026-07-07): hybrid loop-first.** Task 0 is hand-built in one
> supervised exec session (Opus). Tasks 1–12 run **through the superlooper loop itself** —
> each task becomes a GitHub issue on the `command-center` repo (issue-writing discipline:
> Goal / Definition of done / Boundaries / Loop metadata), chained with `blocked-by`, approved
> by William's word (`agent-ready`). This is the loop's first real production run — dogfooding
> is a goal, not a side effect; expect and surface friction. Loop workers read the
> command-center repo's own CLAUDE.md (written in Task 0), which carries the duties below.
> Whether hand-built or loop-built, every change: TDD with pytest; review by a fresh agent
> that did not write the code (Codex cross-review default), evidence posted on the PR;
> ≤2 review/fix rounds; no metered spend beyond the owner-approved GitHub Actions free tier.
> **Joy is a first-class requirement of this surface (design record §0.1): every review of it
> must include joy in its goal function — never trade fun away for efficiency.**

**Goal:** Build the command center MVP — the animated 16-bit airport dashboard over the
superlooper loop — per `docs/DESIGN-2026-07-06-command-center-ux.md` §9, recreating the
handoff-bundle prototypes pixel-perfectly, as its own repo that the loop later maintains.

**Architecture:** A small read-only Python backend polls each adopted repo's truth surfaces
(journal.jsonl, state dir, `gh`, worktree `git diff --stat`), normalizes them into "flight"
objects (ALL semantics computed server-side in tested pure Python), and serves one JSON
snapshot + static files over localhost. A vanilla HTML/JS/canvas front-end (the lifted
prototype renderers + real panels) renders the snapshot; button taps POST to mechanical-verb
endpoints (label/comment/issue writes — the only writes anywhere). No AI in the dashboard,
no standing seat, no new machinery in the runner.

**Tech Stack:** Python 3 stdlib only (no pip deps in runtime, like superlooper), `gh` CLI for
GitHub I/O, vanilla JS + canvas 2D + WAAPI front-end (no framework, no build step), pytest.

---

## A. Settled owner decisions (2026-07-07)

1. **Repo home: `command-center`**, a new private GitHub repo (free), adopted into the loop
   once the MVP exists with tests. MVP is hand-built by exec sessions; afterward the machine
   maintains its own face and can never touch the engine (this repo). Rationale: publishing
   hygiene (skill installer must never carry dashboard code into `~/.claude`) and brain/face
   separation (automated merges land far from the loop's machinery).
2. **Stack: plain HTML/JS/canvas + Python** — same materials as the prototypes; Python matches
   the all-Python loop machinery and its test culture.
3. **Shareability from day one:** when William shares the skill, people get the dashboard too.
   No William-specific hardcoded paths; per-user facts enter through the dashboard's own
   config; a real install/run story in README. Sharing = both repos travel together.
4. **Hybrid loop-first build (2026-07-07).** Task 0 supervised; Tasks 1–12 loop-built as
   approved issues. Dogfooding the loop is an explicit goal; low stakes by design (worst case
   an issue parks and comes back as a Needs You card — fitting, since that card is what we're
   building).
5. **GitHub Actions confirmed (2026-07-07):** free allowance, no credit card on file. On
   exhaustion Actions simply stop and the gate fail-closes (no CI → not green → nothing
   merges → nothing charged) until the allowance resets.
6. **Per-issue model label (in progress, William, 2026-07-07):** a skill feature letting a
   single issue request a specific model. Task 7 (the airfield) runs on **Fable** via this
   label. Contingency if the feature isn't ready when T7 unblocks: T7 simply waits
   (blocked-by holds it), or falls back to a supervised Fable session — owner's call then.

## B. Locked plan decisions (William approves the plan, not each separately)

1. **Semantics server-side, pixels client-side.** Stage mapping, liveness tiers, progress
   heuristic, gate checklist, pill aggregation — all derived in pure Python `lib/` with tests.
   The JS binds values to visuals and stays logic-free. (Testability + the squint test:
   delete the art and the JSON is still a correct state diagram.)
2. **Polling, not push plumbing.** Front-end GETs `/api/snapshot` every ~2s; backend re-reads
   journal/state at the same cadence and polls `gh` on a slower clock with caching. Single
   local user; simplest thing that is honest.
3. **Localhost only.** The server binds `127.0.0.1` exclusively — it can write labels
   (William's word!), so it must never be reachable off the machine.
4. **Dashboard config lists repo checkout paths.** One entry per adopted repo (path to its
   local checkout); the backend reads each repo's own `.superlooper/config.json` for its slug
   (→ state home `~/.superlooper/<owner>__<name>/`, overridable base via `SL_HOME`) and its
   idle/freeze thresholds (defaults 480/2700 s). Explicit and shareable; no scanning magic.
5. **Single-repo field first.** The multi-repo prototype screen was parked by owner
   (ring-camera concept rejected; §0.5 rules a square tile grid). MVP ships a full single-repo
   field with a repo selector; the square-grid overview is Task 11's stretch goal
   (`airfield3.drawMultiField/drawMultiWorld` assets already exist — only the grid composition
   is new).
6. **The `flag` label** is created by the dashboard on first use (it is not in the loop's
   adopt set and no engine code reads it — by design).
7. **Usage pill** is fed by porting the skill's fail-closed usage reader
   (`skill/lib/usage.py` pattern); on failure the pill shows an honest "usage ?" — never a
   stale bar.
8. **Approve button audit format** follows `skill/references/approval-protocol.md`:
   `Approved by William via command-center, <date>.` A button he taps is his word.
9. **Boring mode is fully static** (owner curation ruling) — zero animation, no exceptions.
10. **Sound:** only the Solari clack ships in MVP, toggleable, default per design record
    ("clicks low"); murmur/voice deferred.
11. **Gate configuration for this repo** (set at adoption, Task 0): `required_checks:
    ["tests"]`; `report_required_sections` includes rendered-dashboard screenshot evidence on
    visual tasks (T5, T7–T9, T11); `bright_lines`: `.superlooper/**` and
    `.github/workflows/**` (executable data and the referee — loop workers never touch them;
    changes there come only through supervised sessions).
12. **Promotion dial: absent** (design record §0.7, "absent where main is live"). Merges land
    on `main`, which is what William's running dashboard serves — him looking at the live
    field IS the acceptance; he is the joy inspector.
13. **Issue drafting is the orchestrator's job, not an exec task.** After Task 0 merges, the
    orchestration session drafts the T1–T12 issues from this plan per the skill's
    issue-writing reference, files them with the `blocked-by` chain, and William approves
    each by his word (`agent-ready`).

## C. What survives from the prototypes, what dies (scout-verified 2026-07-07)

Bundle: `design/command-center-handoff/project/` (copied into the new repo by Task 0).

**Lift near-verbatim (framework-free vanilla JS — the flagship fun is already built):**
- `airfield3.js` (1072 ln) — pixel-art field renderer: 400×270 logical field @2×, SNES `PAL`
  palette, procedural sprites, `drawOverview/drawFleet/drawTier/drawMultiField/drawMultiWorld`
  + `live{}` sub-API, incident sign, night lighting.
- `airfield_live.js` (260 ln) — rAF animation engine over Airfield3: circuit path, contrail
  tiers (fresh/idle/frozen), touchdown dust, tower FX (ok/attention/alert), modes
  (circuit/holding/awaiting/spinning), `setU()` scrub (the replay hook), `prefers-reduced-motion`.
- `solari.js` (111 ln) — standalone split-flap arrivals board, WAAPI flips, real-glyph paths,
  `replay()`, reduced-motion support.
- `airfield.js` → **only `drawCrest`** (airline crest); fold into airfield3.
- All panel CSS/HTML (cards, tower log, boards, drawer, boring table, digest, RUNNER DOWN,
  flag box) — inline-styled static markup in `Command Center.dc.html`; copy pixel-for-pixel.
  Screens: 7a (assembled shell), 8a (live field), 8b (Solari), 8c (boring+firehose),
  8d (runner down), 8e (digest), 8f (conflict card/flag/toggles), 8g (drawer), 1e (drawer v1).

**Dies:** `support.js` (Claude Design React harness) and all `<x-dc>`/`DCLogic` machinery;
`airfield2.js` and airfield.js's superseded overview/state; `multi_view.js` (dormant; targets
the rejected ring camera — do not build from it).

**Missing (the actual build):** the entire data layer (zero fetch/poll code exists — all data
is hardcoded); state→visual mapping (prototype has manual demo setters); N concurrent flights
(engine flies exactly one plane on a fixed path); every button is inert; replay has a scrub
hook but no event-playback source; sound has no code.

## D. Data sources (scout-verified against skill code; exact paths)

Per adopted repo, state home `~/.superlooper/<owner>__<name>/`:

| Surface | Path / call | Notes |
|---|---|---|
| Journal | `<home>/journal.jsonl` | append-only; `act` verbs incl. launch, gate, merge (w/ `wander`), hold, park (`needs_william`, memo), bounce, regenerate (conflicts), freeze/unfreeze, alert, notify, nudge, hire_answerer/deliver_answer, event{session_finished/blocked/exited/idle/frozen}, reapprove, nightly |
| Durable issue state | `<home>/state/issues.json` | per-issue: status (ready/running/blocked/frozen/exited/gating/holding/merged/parked/needs_william/bounced), branch, lane, launches, retries, conflicts, pr |
| Liveness | `<home>/state/activity/<id>` mtime | tiers: idle ≥ `session.idle_seconds` (480), frozen ≥ `freeze_seconds` (2700) from the repo's config |
| Dead-man's switch | `<home>/state/runner.heartbeat` | epoch text, written every runner tick |
| Freeze | `<home>/state/merges_frozen.json` | existence = frozen; `{reason, fingerprint, since, source}` |
| ALERT | `<home>/state/ALERT` | `{reasons:[…], since}` |
| Blocked question | `<home>/state/blocked/<id>` | plaintext; `BOUNCED:` prefix = bounce memo |
| Diff size | `git diff --stat` in `<home>/worktrees/<id>/` | read-only |
| GitHub | `gh` (labels agent-ready/in-progress/needs-william/parked/expedite/preserve/superseded/priority:*/type:*; PR state/checks/mergeable; comments) | `blocked-by` lives in issue-body Loop metadata, NOT a label |
| Reports | `<home>/reports/<id>.md`, `reports/morning-<date>.md` | drawer/digest links |

**Known gaps (accepted):** progress signal is heuristic (diff-stat delta + journal event
variety — design record §5/§9); no usage state file (decision B.7); composed arrival prose is
V2 (MVP: issue titles).

---

## Tasks (Task 0 = one supervised exec session; Tasks 1–12 = one loop issue each,
chained with `blocked-by` in numeric order)

### Task 0: Bootstrap + CI + loop adoption (supervised exec session, Opus)
**Repo:** create `command-center` (private, `gh repo create` — free), clone beside superlooper.
- [ ] Scaffold: `pyproject.toml` (pytest only, dev), `lib/`, `bin/`, `static/`, `tests/`,
      `README.md` (goal + install/run story stub), `.gitignore`.
- [ ] Copy in: the design record (as `docs/DESIGN-RECORD.md`) and the full handoff bundle
      (as `design/`) from superlooper — loop workers there must not reach across repos.
- [ ] `tests/conftest.py`: port superlooper's fail-closed neutralization pattern — no test may
      reach real `gh`/`osascript`/network (autouse env override + guard test), per the
      2026-07-03 toast-spam ratchet rule.
- [ ] CLAUDE.md for the new repo — the loop workers' standing orders: constitution pointer
      (design record §0 rulings are fixed points; joy in review goal functions), the
      B-decisions above, TDD duty, fresh-agent review duty with PR evidence, screenshot
      evidence on visual tasks, bright lines (B.11), no metered spend.
- [ ] **CI:** `.github/workflows/tests.yml` running pytest on every PR (owner-approved free
      tier, A.5). Evidence: a real green run on GitHub, linked.
- [ ] **Adoption:** `.superlooper/config.json` (slug, `required_checks: ["tests"]`, report
      sections + bright lines per B.11, `prod_branch: null` per B.12); `superlooper adopt`
      (labels created); `superlooper doctor` green — output shown, not claimed.
- [ ] Green suite + guard test; initial commits; hand back to the orchestrator for issue
      drafting (B.13).

### Task 1: Dashboard config contract
**Files:** `lib/config.py`, `tests/test_config.py`, `config.example.json`
- [ ] Schema: `repos: [{path}]` (checkout paths), `port` (default 8611), `poll_seconds` (2),
      `gh_poll_seconds` (30), `heartbeat_down_seconds` (300), `notify: {imessage_to, cmd}`
      (same shapes as skill's notify block), `fun: {master, solari_clack, …}` toggle map.
- [ ] Loader: loud validation (unknown keys/wrong types rejected naming the offender —
      mirror `skill/lib/config.py`); expand `~`; read each repo's own
      `.superlooper/config.json` for slug + thresholds (defaults 480/2700 if absent).
- [ ] State-home derivation incl. `SL_HOME` override (shareability + testability).
- [ ] Green; commit.

### Task 2: Loop-state readers (pure)
**Files:** `lib/readers.py`, `tests/test_readers.py` (+ `tests/fixtures/statehome/…`)
- [ ] Tolerant journal reader (skip corrupt/blank lines, missing → `[]` — mirror
      `skill/lib/journal.py:41-57`); tail-window read for the log/firehose.
- [ ] issues.json, activity mtimes, blocked/exited/awaiting markers, heartbeat age,
      merges_frozen.json, ALERT, reports presence — one `read_state_home()` facts dict.
- [ ] Fixtures built from `design/…/uploads/sample-data.txt` real shapes — never invent shapes.
- [ ] Green; commit.

### Task 3: The flight model (all semantics live here)
**Files:** `lib/flights.py`, `tests/test_flights.py`
- [ ] Facts → flights: circuit stage mapping (status+markers+journal → at-stand/taxi-out/
      takeoff/downwind/base-turn/final/touchdown/taxi-in; discrete only — position never
      encodes time); off-path states parked (chocks), awaiting/amber (needs-william, bounced),
      holding, frozen-session vs merges-freeze (distinct!); attempt counter → `SL-N·A2` on
      regenerate; wander flag (no flourish).
- [ ] Liveness tier from activity age vs per-repo thresholds; **progress heuristic** (diff-stat
      delta + journal event variety over rolling window) → explicit `spinning` warning when
      crisp-liveness + flat-progress.
- [ ] Gate checklist derivation (report ✓ review ✓ CI ✓ mergeable ✓) from PR facts.
- [ ] Global pill aggregation (worst state across repos, names the offender) + tower status
      (ok/attention/alert) + incident counter ("N landings since last incident": machine-side
      failures only — park, conflict-cap, failed auto-fix, runner death; William's gates NEVER
      touch it) + corner-counter stats (outcome stats only; NO human-latency stats, ever).
- [ ] Green; commit. (This task is the squint test in code form — review against design
      record §3/§5 mappings line by line.)

### Task 4: GitHub adapter + diff/usage pollers
**Files:** `lib/gh.py`, `lib/pollers.py`, `tests/fakes/fake-gh`, tests
- [ ] `gh.py` read set: open/ready issues + labels + titles, PR for branch (state, mergeable,
      checks), comments; write set: `set_labels`, `comment`, `create_issue`, label-create.
      Thin `_run` wrapper, hard timeout, fail-closed empty-but-typed reads / `False` writes
      (mirror `skill/lib/gh.py`); `SL_GH` binary override for tests.
- [ ] `fake-gh` fixture harness recording mutations (port the pattern from superlooper
      `tests/fakes/fake-gh`).
- [ ] Diff-stat poller over `<home>/worktrees/<id>` (read-only, absent-worktree safe);
      usage reader ported fail-closed (B.7) → "usage ?" on failure.
- [ ] Poll cadence + cache (gh on the slow clock); green; commit.

### Task 5: Server + snapshot API + truth-first shell
**Files:** `bin/command-center` (entry), `lib/server.py`, `static/index.html`,
`static/shell.js`, `static/shell.css`, tests
- [ ] stdlib HTTP server bound `127.0.0.1:<port>`; `GET /api/snapshot` (flights, boards data,
      tower-log window, pill, needs-you list, per-repo shipped-delta); static file serving;
      2s front-end poll loop.
- [ ] Four-panel shell copied pixel-for-pixel from screen 7a (grid, top bar, pill, panels) —
      canvas areas placeholder for now.
- [ ] **Boring mode first** (screen 8c): dense flat table sortable by stage/staleness/elapsed/
      repo + journal firehose with time-range + free-text filters, clickable flight numbers;
      keystroke toggle; fully static (B.9). Every visual channel paired with an exact numeral.
      This proves the whole data path end-to-end before any art goes live.
- [ ] "All clear" ribbon when Needs You is empty; camera-independent trouble banner slot.
- [ ] Green (server handlers unit-tested with injected snapshot); commit.

### Task 6: The verbs (buttons become real)
**Files:** `lib/actions.py`, `lib/server.py` (POST routes), `static/shell.js`, tests
- [ ] POST endpoints, each an existing mechanical verb, each journal-greppable via audit
      comments: **approve/re-approve** (add `agent-ready`, remove `parked`/`needs-william`,
      audit comment per B.8), **drop** (close + comment; one inline confirm — it's the only
      destructive tap), **expedite** (label), **bounce-yes**, **flag** (raw text →
      `create_issue` labeled `flag`, creating the label on first use, no AI ever),
      **discuss** (server composes the briefing snippet from the flight's facts; client
      copies to clipboard).
- [ ] Tap-where-you-read: same endpoints callable from any card/row/drawer.
- [ ] All writes through fake-gh in tests; mutation assertions; green; commit.

### Task 7: The airfield, live and true — **model: Fable via per-issue model label (A.6;
owner-approved 2026-07-07; if the label feature isn't live when T7 unblocks, it waits or
drops to a supervised Fable session — owner's call then)**
**Files:** `static/airfield3.js`, `static/airfield_live.js` (lifted then extended),
`static/field.js` (binding), tests for any logic that creeps beyond pixels
- [ ] Lift `airfield3.js` + `airfield_live.js` + `drawCrest` verbatim first (pixel-perfect
      baseline vs prototype screens), then extend the engine: **N concurrent flights** —
      one per active issue, positioned by discrete stage on the circuit, two runways = the
      repo's real lanes, parked planes w/ chocks + "MX REQ", holding pattern, amber awaiting
      ring, spinning spotlight.
- [ ] Bind real state: contrail from real liveness tier; tower FX from real status; RUNNER
      DOWN grey (full-surface, screen 8d) wired to heartbeat age; field dims + problem lit on
      trouble; quiet-state caption ("last landing 3h ago — all clear").
- [ ] The living clock: wall-clock drives day/dusk/night lighting (no weather — banned §7);
      incident sign + corner counter bound to Task-3 numbers; airline identity (crest + name
      from config, auto-generated default, renameable; literal slug stays on cards/boring).
- [ ] Repo selector (single-field MVP per B.5); flight click → drawer stub event.
- [ ] Squint-test review + joy review (is it *delightful*?); commit.

### Task 8: The boards (Solari is the flagship — polish accordingly)
**Files:** `static/solari.js` (lifted), `static/boards.js`, `static/boards.css`, tests for
queue-order logic (server-side)
- [ ] Departures: real launch order (eligibility + priority + expedite-on-top + blocked-by
      as "awaiting connection SL-N" — recompute read-only, semantics in `lib/flights.py`),
      split-flap CSS styling from 7a.
- [ ] Solari arrivals: lift `solari.js`, feed real merges newest-first (issue titles, MVP);
      flutter on new arrival, settle <1s, readable mid-flutter, reduced-motion honored;
      optional clack (toggle, default low — B.10). **This is the owner's favorite moment —
      it must be genuinely satisfying; joy review required.**
- [ ] Boards filter to selected repo; green; commit.

### Task 9: Tower log, Needs You, the drawer
**Files:** `static/tower.js`, `static/needsyou.js`, `static/drawer.js`, CSS, server additions,
tests for gloss/mapping logic
- [ ] Tower log: journal verbs → plain-sentence comms feed (radio flavor always beside the
      real sentence; answerer exchanges as radio calls; nudges; memos), each row expandable
      to its raw journal line; **since-you-last-looked divider** (last-seen ts persisted in
      the dashboard's own tiny state file).
- [ ] Needs You: one card per parked/needs-william/bounced/conflict-cap decision — plain
      headline + gloss (literal term on hover), memo, buttons (Task 6); conflict-cap card
      names the collision in one plain sentence, **Discuss highlighted as default there**;
      badge count; never filters, never moves; collapses to all-clear ribbon.
- [ ] Flight drawer from any plane/row/board line: title, circuit rail, clearance checklist
      w/ glosses, links (issue/PR/branch), memo history, cargo chips (+N/−N, files,
      touches), that flight's journal slice, go-around counter.
- [ ] Green; commit.

### Task 10: Monitoring armor + pushes
**Files:** `lib/notify.py` (ported precedence), `lib/watchdog.py`, tests
- [ ] Port notify precedence (imessage_to → cmd → log; call the same
      `imessage-notify.sh`-style osascript via injected binary, stubbed in tests).
- [ ] Dead-man's switch: heartbeat age > threshold → RUNNER DOWN surface state (Task 7 hook)
      **+ one push**, re-armed only after recovery (no repeat nagging); per-repo.
- [ ] Pill/banner correctness tests: worst-state aggregation, offender naming, off-screen
      trouble banner independent of camera/scroll. (All other pushes remain the runner's —
      the center adds no machinery to the runner, §6.)
- [ ] Green; commit.

### Task 11: Replay + morning digest (+ grid stretch)
**Files:** `lib/replay.py`, `static/replay.js`, `lib/digest.py`, `static/digest.js`, tests
- [ ] Replay: journal window → ordered event frames driving the field via the engine's
      `setU`/state setters; scrubbable/steppable, every frame clickable to its event; a
      *treat* behind a button (never load-bearing).
- [ ] Morning digest view: mechanical counts + one sentence per exception (parks, go-arounds,
      freeze arc) over a timestamped clickable event table (read the same aggregations as
      `skill/lib/report.py`, recomputed read-only).
- [ ] Stretch (only if >1 repo adopted by then): square-tile-grid overview composing
      `drawMultiField` small fields, click-to-enter (B.5; §0.5 — grid, never a ring).
- [ ] Green; commit.

### Task 12: Ship it — the install story
**Files:** `README.md`, `bin/install-launchd.sh` + template
- [ ] README for strangers (A.3): clone → configure (`config.example.json` walkthrough) →
      run (`bin/command-center`) → optional launchd keep-alive template (port the pattern
      from `skill/templates/launchd.runner.plist`).
- [ ] End-to-end smoke vs a fixture state-home: screenshot-level evidence of the assembled
      dashboard rendering real-shaped data (the verification artifact for William).
- [ ] Tag `v0.1`. Acceptance is already structural: Tasks 1–12 were themselves loop-built —
      the machine has been maintaining its own face since Task 1 — and William's joy pass on
      the live dashboard (B.12) closes the MVP.

---

## Self-review vs design record §9 (coverage check)

Airfield animated ✓T7 · boards ✓T8 · tower log ✓T9 · Needs You + working buttons ✓T9+T6 ·
flight drawer ✓T9 · boring table + firehose ✓T5 · dead-man's switch ✓T10+T7 · replay +
digest ✓T11 · diff chip ✓T4 (data) +T9 (render) · flag box ✓T6 · since-you-last-looked ✓T9 ·
global pill/alert rail ✓T3+T5+T10. Fun-layer MVP set (§7): Solari ✓T8 · airlines ✓T7 ·
living clock ✓T7 · corner counter ✓T3+T7 · incident sign ✓T3+T7 · sound (clack only) ✓T8.
Deferred per record: presence modes, per-tool verbs, AI-drafted flags, composed prose,
multi-repo grid (stretch T11), remaining soundscape.
