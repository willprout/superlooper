# Architecture proposals

Proposed structural changes to the loop, born from the 2026-07 assumptions audit. Each entry
records the current behavior, the proposed change, why, and the known design costs — enough for a
planning session to turn it into issues, not a step-by-step plan. Status moves
`proposed → approved → issued (#N) → landed`.

---

## 1. End the flight at evidence-posted; the runner owns CI; finishers fix red

**Status:** proposed (discussed 2026-07-24)

### Current behavior

The worker's ship gate is five steps, and step 5 is "CI is green on your PR"
(`skills/superlooper/skill/lib/brief.py` — `_BUILD_WORK_BLOCK`). So the builder session stays
alive after posting the PR, babysitting CI — the moment its context is at maximum size and
minimum marginal value. The merge itself is already runner-side and mechanical (`gate.py`
checklist), as is conflict handling (retire-and-rebuild). Only the CI tail is misplaced.

### Proposed change

- The flight ends when the PR is posted **with its evidence complete**: report filed, pinned
  review verdict posted, branch pushed. Ship gate step 5 is removed from the worker brief; the
  session exits there.
- The runner owns the CI wait (it already polls check states for the gate).
- On a red required check, the runner spawns a short-lived **finisher** session with a thin brief:
  the issue, the diff, the failing logs, and the PR report. Same pattern as the existing
  specialized briefs (`templates/answerer-brief.md`, `templates/debugger-brief.md`).

### Why

- **Context engineering.** Fixing a CI failure needs the issue, the diff, and the logs — not the
  builder's history of dead ends. A fresh session is cheaper and has no sunk-cost attachment to
  the failing approach.
- **Cost asymmetry.** Keeping the builder alive "just in case" is paid on every flight; a finisher
  is paid only on the flights that actually go red.
- **Selection effect.** A worker that reaches PR-posted has already passed its local suite, driven
  the behavior end-to-end, and cleared cross-review. CI failures after that are disproportionately
  environmental or flaky — exactly what fresh eyes handle well.
- **Lane and window economics.** Sessions end sooner: lane slots free earlier (more parallelism),
  less usage-window burn per flight.
- **Deletes a fragility class.** Workers no longer perform any long wait, retiring the
  background-sleep / `gh run watch` babysitting quirks entirely.

### Design costs (acknowledged, not blockers)

1. **The review pin.** The gate only accepts a verdict pinned to the PR's current head — by
   design. A finisher that pushes a commit voids the existing review evidence, so the finisher
   brief must include: get the delta re-reviewed and post a fresh pinned verdict. Bounded cost —
   the re-review covers a small delta — but omitting it would park every finisher-touched PR on
   evidence it used to have.
2. **The PR report becomes a machine handoff.** Today it is prose for the arrivals board. Once a
   contextless finisher works from it, its required sections should be specced for that reader:
   what changed and why, how to run the relevant tests, anything known-fragile. This also improves
   the retire-and-rebuild path.
3. **One new runner state.** "PR posted, CI pending" becomes an explicit place a flight can sit,
   with the usual staleness and wedge questions any new state carries. Kept deliberately small:
   the state's only transitions are green → gate, red → spawn finisher, repeat-red → park.

### First-principles frame

The CI wait is unit-independent time — it belongs to the line (runner), not the cell (worker).
The CI fix is a rework station entered only on defect, off the happy path. The cell's exit
criterion moves from "landed" to "evidence posted," which is where the builder's context stops
paying rent.

---

## 1b. Pre-flight input gates (candidate — discussed 2026-07-21/28, never formalized)

**Status:** CANDIDATE (owner has not ruled)

Two cheap gates at the cell's entrance, from the assembly-line discussion:

- **Pre-flight triage.** Before launching, a cheap model reads the approved issue and verdicts
  *buildable / underspecified / contains-owner-decision*, parking pre-launch. Catches bad inputs
  before an Opus session burns on them (the README's own recordings-retention example parks
  post-build today). Output is a launch/don't decision — no new handoff artifact, no forgeable
  surface.
- **Approach-decided discipline in write-issue.** The issue-authoring skill should force the
  question "is the approach decided?" — if contested (multiple viable hows with tradeoffs the
  owner cares about), either decide it in the room (encode in Goal/Boundaries) or file
  `type:investigate` first. Today a worker picks the approach at 3am. Note: partially overlaps
  the P2/effort:max plan-approval tier (claim c34); this gate is earlier and cheaper.

---

## 2. Split plan from execution inside the flight (launch-time, same worktree)

**Status:** TENTATIVE (discussed 2026-07-28; handoff topology deliberately unresolved — coupled
to the session-substrate exploration, see below)

### The problem

Context rot is a slop *generator*, through three specific channels:

- **Self-conditioning on dead hypotheses.** Once a wrong statement enters the session's context,
  later reasoning treats it as fact.
- **Goal drift.** The definition of done fades relative to recent tool output; the session starts
  optimizing "make the test pass" over "achieve the intent."
- **Late-context writing.** The final code, the regression tests, and the evidence report are all
  written at the end of the session — the most degraded context it will ever have. Tests written
  in the same polluted context as the code tend to assert what the code *does*, not what the
  issue *wanted*.

These are the two slop channels the merge gate is weakest against: late-context tests, and a
wrong *approach* that reviews fine as a diff (re-deriving the right approach from a finished diff
is hard for any reviewer).

### The potential solution

A launch-time plan → execute split **inside the flight**, both phases on the same worktree, the
executor starting immediately after the plan is done. Because plan and execution are adjacent in
time against the same tree, the thin-issue "plans rot" objection does not apply — that objection
is about *queued* (discussion-time) plans. Elements:

- **Context bottleneck.** The planner's messy exploration distills into a plan; the executor
  starts fresh with issue + plan at full attention strength. The planner's dead ends don't
  propagate unless they survived into the plan. Code and tests get written in the early,
  high-quality window of a fresh context, and TDD proceeds from the plan's acceptance facts —
  from intent, not from freshly-written implementation.
- **Pristine-worktree rule.** The planner leaves the tree clean except deliberately committed
  artifacts — no half-finished spike code as inherited noise.
- **Failing-test-as-plan** (diagnose-and-fix). The planner commits a failing test reproducing the
  root cause. A narrative can assert falsely; a committed failing test *runs* — the executor's
  first command mechanically re-verifies the diagnosis against the live tree.
- **Plan cross-review** (`effort:max` builds). A plan is reviewable for *approach* before code
  exists, when wrongness is cheapest — a detection layer the diff-review pipeline cannot provide,
  and the part of this proposal that survives even as models improve and rot shrinks.
- **Bounce protocol.** The executor is licensed to stop and hand back "the plan contradicts the
  repo" for re-planning, never to improvise off-plan (BOUNCED, one level down).
- **Label routing.** Two-phase only for `type:diagnose-and-fix` and `effort:max` builds;
  single-session for everything else, so trivial PRs never pay the tax.

### Why diagnose-and-fix benefits most

The split's value scales with two ratios: exploration : writing, and the compression ratio of the
handoff artifact. Diagnosis is maximum-entropy exploration (hypothesis after wrong hypothesis,
each stated plausibly in context — the worst self-conditioning substrate), and its output
compresses to almost nothing ("the cache key omits the tenant id; fix in X; regression in Y").
The loop already encodes this shape at coarser grain: `type:investigate` → children is
diagnose/execute split across the loop; the diagnose-and-fix split-on-overreach rule is a
conditional version. This completes a pattern rather than inventing one.

### The unresolved part: handoff topology

Two viable shapes, deliberately not chosen yet:

1. **Planner-as-orchestrator, executor as subagent.** One runner-visible session; the planner
   spawns the executor (fresh context) via the Task tool and stays alive to review the executor's
   diff with its full uncompressed knowledge — recovering the tacit knowledge lossy compression
   drops. Zero new runner states, zero new first-prompt deliveries. Per-phase model routing for
   free (plan on a strong model, execute on a cheaper one).
2. **Runner-mediated two-stage flight.** Cleaner separation and independently supervisable
   stages, but each flight pays two first-prompt deliveries through the session substrate and new
   runner states on the happy path.

Option 1's decisive advantage — avoiding extra delivery seams — is contingent on the current cmux
substrate being the fragile thing it is. If the substrate exploration (next) yields a
delivery-verifiable, bomb-proof session host, option 2's costs drop sharply and its supervision
benefits may win. Decide the topology **after** the substrate question resolves.

---

## 3. Session substrate: the bar any cmux replacement must clear

**Status:** EXPLORATION — evidence gathered and criteria set 2026-07-28; candidates deliberately
not yet evaluated. Evidence: two-agent audit (code catalog of substrate-imposed machinery;
incident-history taxonomy from the reliability ledger, incident docs, and issues incl. the
#258-#261 hollow-tab family and #286).

### What the evidence established

- **~5,500-6,500 lines** of engine + dashboard code exist only because a worker is an interactive
  TUI in a GUI tab, not a supervised child process. Heaviest classes: liveness/progress inference
  (~1,600), launch delivery + storm control (~1,300), runner self-resurrection + anchor (~900),
  tab/worktree teardown (~800, incl. the ~400-line ordered teardown at runner.py:2167-2560),
  screen classification (pane_state.py, 423 lines, ~100% substrate).
- The substrate owns the most expensive **lost nights** (launch-into-dead-tab across every era;
  anchor geography; ambient-Mac state). The engine owns the most **frequent** incidents
  (refused-GitHub-read-as-empty, notify storms, recovery verbs) — a substrate swap does not touch
  those. The 07-15 forensics withdrew App Nap and re-attributed several "environment" failures to
  engine lifecycle bookkeeping: the substrate's proven share is smaller than its line-count share.
- Root pattern (ledger's own words): the runner's senses and its subjects speak different
  languages, and its probes contaminate their own measurements. One generative fact underneath:
  **no handle** — no atomic spawn, no waitpid, no channel, no state API.

### The criteria (each: requirement → acceptance test → what it retires)

**I. Process contract** — S1 atomic spawn (launch runs the exact command or errors; never
silently no-ops; retires shim/sentinel/hollow-tab class). S2 lifecycle as events (exit code +
liveness as process facts; "no live process in this worktree" queryable; retires pid-pulse,
teardown inference, D14/D4/D9; the progress-stall ladder shrinks but survives — "alive but
spinning" is an agent property). S3 addressed acknowledged messaging (channel write with distinct
delivered/busy/awaiting-input/dead outcomes; never keystrokes; retires nudge-pane, mailbox
receipts, RC-DEADPANE). S4 state as data not pixels (busy/idle/awaiting-input/dead/auth-dead
structurally exposed; a TUI reskin can never blind the loop; retires pane_state + auth-banner
family).

**II. Host independence** — S5 indifferent to display/lock/login/GUI state (full night, display
off, zero delivery faults). S6 supervisor decoupling (workers survive runner death; re-adoption
by handle; no anchor/terminal geography; retires resurrect/anchor/liftoff machinery, the 07-09
storm). S7 pinned environment + scoped credentials (ambient-Mac nights retired; per-worker
identity converts the #226/#227 forgery paths from undetectable to attributable).

**III. Containment** — S8 blast radius (a worker cannot signal processes outside its flight; the
pkill deny becomes defense-in-depth). S9 zero attended first-run gates (no trust/permission
dialogs; retires pretrust).

**IV. Preserved properties (disqualifiers, not scores)** — S10 subscription billing (sessions
bill as interactive Claude Code on the Max plan; the binding constraint given Anthropic's mixed
signals on headless billing). S11 attach-on-demand (live view of any session in seconds, type-in
optional, detach harmless, default unwatched — the one thing cmux does well; #168 ruling makes it
a requirement). S12 hook continuity (PreToolUse/Stop rails may be replaced by stronger
enforcement, never weaker).

**Weighting:** S1-S4 delete the four heaviest machinery classes; S5-S6 delete the worst realized
nights; S10-S11 disqualify trivial answers. Every acceptance test runs as a literal spike against
a candidate before any migration code.

**Explicitly not fixed by any substrate:** refused-read-as-empty (#286 era, most frequent class),
recovery-verb defects, notify storms, publish/config drift, unhashable-value crashes, and the
#226/#227 bad-merge paths (except S7's attribution upgrade). Engine-inherent, all of them.

### Parked for later: the engine-inherent work queue

Regardless of what happens to the substrate, these classes need their own hardening passes and
are explicitly on the backlog (owner-flagged 2026-07-28):

1. Refused-GitHub-read-as-empty family — still producing incidents (#286, 2026-07-27); the
   trusted-signal discipline needs a systematic sweep, not per-path patches.
2. Recovery-path / owner-verb defects (the D8–D14 class; #169's silent livelock shape).
3. Notify/park storm suppression as a designed system, not per-incident de-dup.
4. Publish/config drift — the running engine lagging its own merged improvements (38-hour strand,
   bootstrapping seam).
5. Wrong-typed/unhashable state values crashing whole ticks/boards (totality sweep).
6. The two prospective bad-merge paths: #226 (status forgery) and #227 (agent-ready
   self-approval) — shared-identity/provenance is the root (#232).

---

## 4. Tool deep-dive results: verified adoption claims (2026-07-28)

**Status:** EVIDENCE GATHERED — 32 verified claims awaiting owner triage. Full distillate in
`docs/TOOL-DIVE-2026-07-28.md`.

A 97-agent workflow deep-dove termic, herdr, t3code, humanlayer (+ amux, cao, claude-squad,
phantasm, open-code-review, ai-slop-detector, and the ACE-FCA/12-factor doctrine). 89 raw
adoption claims were deduped to 36 and adversarially verified (code-truth + constraint-fit);
32 survived. Headlines:

- **All four tools ride the Max subscription** (high confidence, code-verified) — including
  t3code's Agent-SDK path. **Owner ruling 2026-07-28: the no-headless bright line STANDS** —
  Anthropic's paused-but-likely-returning subscription-headless deprecation makes the SDK path
  a long-term risk; c28 is shelved; substrate candidates must run plain interactive `claude` in
  a PTY.
- **No tool replaces superlooper** (deep-dive confirmed: none has an evidence-gated autonomous
  loop), and none of the four is the substrate winner either — c27's verdict is a gated spike on
  **detached tmux + a cao/herdr-style control plane**, ranked deliberately below the floor work.
- **The floor work comes first**: c1–c18 harden the current substrate regardless of the substrate
  decision (billing/identity floor, total state enum with UNKNOWN≠idle, acknowledged delivery,
  verify-or-teardown launches, MISSING-sentinel reads, sole-writer evidence artifacts).
- **P1/P2 got sharper**: quiesced receipts + durable pause for P1; plan-as-on-disk-artifact with
  machine-checkable program-design forms for P2; plan-approval label tier for effort:max parked
  at the split boundary (the batched-attention translation of ACE-FCA's leverage pyramid).
- **Verification earned its keep**: 4 kills, including one claim whose "detectors" turned out to
  be blog posts (readme-ware), and two that defended owner rulings (#168/#190 worktree
  preservation; #194 no-live-blocked-sessions) against plausible-sounding adoptions.

### Owner triage rulings (2026-07-29)

- **Rate-limit park-and-resume: KEEP** — owner has been bitten by rate limits in practice, and
  the current session-usage checker is fragile; owner is open to adopting amux's usage module
  wholesale, not just the resume pattern.
- **Merge-gate hardening: KEEP BOTH** — strict evidence format and prove-against-the-installed-app
  e2e (the i165 false-green / publish-drift class is realized, twice).
- **Review pipeline: DEFERRED to the owner's own design** — borrow the 360eApp repo's process
  (4 review agents + 2 jury agents). To be designed together later; no review-architecture work
  until then.
- **Gap-fill results (2026-07-29, 4-agent workflow + skeptic audit; live experiments on the
  owner's Mac):** herdr is **Apache-2.0** (vendor + fork both legal — reverses the unverified
  flag); its headless server + AUTOMATIC crash-recovery (`claude --resume` per captured session
  id on restart, `resume_agents_on_restore=true`) are code-verified but never executed; its
  socket cannot be fenced by env-strip (deterministic path `data_dir/herdr.sock`) — fence = small
  auth fork at `handle_connection`. termic: orchestration verbs REQUIRE a live webview (window),
  windowless daemon unbuilt; `archive` DESTROYS worktrees (no stop-keep-worktree verb); AGPL =
  upstream-PR-only fixes → drops out as host, stays pattern donor. tmux experiments (live):
  spawn/exit-codes/pane-died hook/keychain-from-pane all verified; server SIGKILL kills all pane
  children; wake-crash lore traced to a 2015-closed issue (downgraded; soak test pending).
  Universal: `claude --session-id` pre-assign + `--resume` = blip-recovery on every path
  (superlooper uses neither today); PATH landmine — only `claude` on the Mac is cmux's bundled
  shim, install standalone before retiring cmux. Five deciding spikes listed in the Body
  Decision artifact. Conditional read: termic out as host; herdr if crash-drill + fence hold,
  else tmux rebuild.
- **Spikes RUN overnight 2026-07-29** (fresh session; full evidence
  docs/SPIKES-2026-07-29-results.md): herdr revive works but is CLIENT-GATED — headless it
  reported idle/ready for a nonexistent process for 120s (hollow-tab class; confirmed structural
  in source: agent_resume.rs:79 returns no candidates at zero terminal geometry); herdr
  `agent prompt` stalled 2/2 while tmux send-keys delivered 3/3 into real claude;
  `--session-id`/`--resume` passed unqualified (host-independent; adopt regardless);
  gui/$UID LaunchAgent keychain PASS (PATH is the gotcha); 7h idle soak clean but the Mac never
  slept — sleep/wake still untested (supervised lid-close pending). Two host-independent
  silent-lie landmines: inherited CLAUDE_CODE_* kills transcript saving (breaks --resume
  silently); XDG_CONFIG_HOME de-authenticates gh confidently. Post-spike read: evidence leans
  tmux rebuild, herdr → parts bin (Apache), pending a supervised herdr retry + upstream answer
  on headless revive. Owner decision pending.
- **Supervised re-run 2026-07-30** (owner present; evidence docs/SPIKES-2026-07-30-supervised.md):
  clean-room enforced via empty-env allowlist after the checkpoint caught a LIVE ANTHROPIC_API_KEY
  in the owner's ~/.zshrc:5 (owner removed it — c1's landmine found in the wild). Prompt mystery
  SOLVED: `agent prompt` without `--wait` silently drops on unwatched panes returning rc=0 (6/6
  false greens); with `--wait` reliable; fresh-revive first-prompt stalls honestly (last night's
  2/2) and an Enter chaser fixes it 6/6. Crash recovery PROVEN live: one codeword through
  teach → kill -9 → window revive → recall → kill -9 → ROBOT revive in 1s → real sleep → recall;
  one session id, one transcript. Phantom deepened: status surface says idle/ready while action
  surface says agent_not_found for the same agents. Sleep acquitted BOTH substrates (9/9 pids,
  symmetric 332s gap, DarkWake resume; one short AC nap only). Post-run read: herdr-as-host
  wrapped in distrust (herdr = muscle never truth: --wait always, transcript delivery checks,
  process-fact liveness, robot viewer per workspace, fence fork before unattended). Four upstream
  issue candidates listed in the evidence file §12. Owner decision pending.
- **Session-host decision: DEFERRED and re-framed.** Owner de-weights survivability ("the loop
  may stop when the lid closes — Amphetamine or a Mac mini solves that; my wifi can die anyway;
  flights happen"). **Stop-and-resume after ANY interruption is a first-class requirement**;
  sessions commit as they go, so recovery quality matters more than never-dying. The host's real
  sin to fix is operations that LIE (launches that silently no-op, rc=0 non-delivery, no state
  answer) — judge candidates on honest operations + resume quality, not uptime. The decision is
  a package across the launch/sensing/messaging/recovery rows (adopting herdr would cover several
  at once), to be walked as a set. Herdr's open fleet-socket needs a fence (env strip / PreToolUse
  deny on fleet verbs / upstream auth PR) — mitigation required, not a disqualifier.
