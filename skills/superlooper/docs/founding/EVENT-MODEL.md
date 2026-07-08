# EVENT-MODEL.md — the signal/event model (v2, bombproofing pass 2026-06-25)

This is the contract the watcher, hooks, and orchestrator all obey. It replaces the v1 model
where a single `.done` file (written by the Stop hook on *every* turn-yield) was the completion
signal — which conflated "genuinely finished" with "yielded the turn to wait on my own
background work" and produced ~50% false wakes (see the 2026-06-25 post-mortem of
run-20260625-1516: 4 of 6 wakes were false rests).

## Principle

**Never infer a session's state from the fact that it paused.** A Claude session comes to rest
every time it ends a turn — including when it deliberately steps back to await its own
background work (a `ship.sh`, a backgrounded build, a review). State is read from **explicit,
file-backed signals**; silence is caught by a **tiered staleness timer** whose response is a
**safe peek** (look before you ever type, and never type into a dead or menu'd pane), never a
blind nudge.

## On-disk signals (per PR id), who writes them

| Path | Writer | Meaning |
|---|---|---|
| `state/started/<id>` (mtime) | `start-pr.sh`, as its **first** action | **delivery proof** — the launch keystrokes reached the tab and the worker shell ran. `launch-pr.sh` polls for this; it is NOT an event source (the watcher ignores it) |
| `state/activity/<id>` (mtime) | PostToolUse hook (every tool), Stop hook (final stamp), `launch-pr.sh` (**only after `state/started` verifies delivery** — never at launch-intent), orchestrator (on resume) | liveness clock |
| `reports/<id>.md` | the session, as its **final** action, when truly done | **finished** |
| `state/blocked/<id>` | the session, plain-text question, then ends its turn | **blocked / needs input** |
| `state/exited/<id>` (mtime) | `start-pr.sh` when the Claude process returns to the shell (crash/quit/limit/normal) | **process gone** |
| `state/awaiting/<id>` | the session before parking to await long background work; removed on resume | suppress the idle peek for the await window |
| `state/ship/<id>.json` | `bin/watcher.py` (GitHub poll, H1) | GitHub/ship status (poll-driven ~90 s; GitHub has no push signal). The **watcher** now owns this poll — armed by `run.json prs[id].ship_watch` (a branch name), needs `REPO` in the watcher's env. It **replaces** the old `bin/ship-watch.sh` background process the harness SIGTERM'd every ~30 min (S5); ship-watch.sh survives only as a fallback for a pre-2026-07-02 watcher (see `state/watcher.caps`) |
| `state/watcher.caps` (JSON) | `bin/watcher.py` at startup | capability marker (`{"github_poll": bool}`) — `github_poll:true` iff this watcher has `REPO` and is polling GitHub itself. The orchestrator reads it to decide whether it still needs `bin/ship-watch.sh` (H1 backward-compat) |
| `state/last_drain` (epoch) | the orchestrator, at the **start** of every wake | **"woke"** — proves the orchestrator took a turn (the orchestrator-singleton liveness signal — see `state/orchestrator.lock` — and a human/debug signal). NOT what the stall/ring-health alarms trust (WS4) |
| `state/last_progress` (epoch) | the orchestrator, the moment it has **moved ≥1 event to `processed/`** (and at startup as a baseline) | **"real progress"** — the watcher's stall + doorbell-health alarms key on THIS, so a half-broken wake that stamps "woke" but acks nothing is still caught (WS4 split) |
| `state/orchestrator.lock` | `bin/orchestrator-guard.sh` (claim/handoff) | the **single-orchestrator** mutex: surface UUID of the live brain + epoch + generation; a duplicate/replayed seed that finds a live holder refuses to start (RC-ORCH-SINGLETON). Liveness = freshest of `last_drain`/`last_progress` (MAX — a quiet-but-self-waking brain stays "alive" so it is never false-taken-over) |
| `state/ALERT` / `state/DONE` (JSON, watcher→) | `bin/watcher.py` | the **loud, deterministic signal** William's external Codex process-watchdog polls: `ALERT` exists while a stall / dead-doorbell is active (its JSON names the reason; also a greppable `{"event":"alert",…}` line in `watcher.log`); `DONE` is written once on run completion as the watcher self-stops (WS2/WS3/WS5) |

The Stop hook no longer writes `.done` and no longer notifies — it only stamps activity. The
**delivery-proof handshake** (`state/started/<id>`) closes the run-20260625-1857 overnight killer:
a tab was created but its keystrokes were dropped (Mac locked), so no worker started — yet
`launch-pr.sh` had already stamped activity, fabricating "launched & alive" for up to 45 min. Now
activity is stamped ONLY after `start-pr.sh` proves the keystrokes were delivered; an undelivered
launch fails loudly (exit 2) and the watcher never sees the PR as alive.

## Events the watcher emits (edge-triggered; dedup token in parens)

| Event | Fires when | Token |
|---|---|---|
| `session_finished {id}` | `reports/<id>.md` present | **content hash** of the report (not mtime — an identical rewrite must not re-fire; A6) |
| `session_blocked {id}` | `state/blocked/<id>` present | content hash of the marker |
| `session_exited {id}` | `state/exited/<id>` present | exited mtime (sticky until relaunch clears it) |
| `session_idle {id}` | launched, **not resolved**, **no `awaiting`**, activity stale ≥ `IDLE_SECONDS` (and not yet frozen) | `(id,'idle')` edge |
| `frozen {id}` | launched, **not resolved**, activity stale ≥ `FREEZE_SECONDS` | `(id,'frozen')` edge — supersedes idle |
| `ship_update {id,status}` | `state/ship/<id>.json` status token changes | **stable** ship token (volatile `mergeStateStatus=UNKNOWN`/transient states normalized out; Finding 2) |
| `ship_watch_stale {id}` | `run.json` says a ship-watch is armed for `<id>` but `state/ship/<id>.json` heartbeat is stale ≥ `SHIP_STALE_SECONDS`, or gh has returned no usable status since arming. With watcher-owned polling (H1) the heartbeat is refreshed every poll, so this now chiefly means **gh returns no PR status** for the branch (or, for a pre-polling watcher, a dead `ship-watch.sh`) | `(id,'ship_stale')` edge |
| `resume` | `state/resume_at` epoch reached (5h-limit wake) | none |
| `rotation_due` | `state.due_for_rotation(run, now)` true for the current rotation epoch **AND the run is not mechanically idle** (H2/S4 — no PR `running`/`in_review` and an empty queue suppresses it, so a quiet night doesn't churn a fresh orchestrator on the wall-clock arm) | rotation baseline token (`last_rotation_wake:last_rotation_ts`) — re-fires only after a real rotation starts a new epoch |

### resolved (exempt from idle/frozen) = marker EXISTENCE

```
resolved = (report present) OR (blocked present) OR (exited present)
```

**Not** an activity-vs-marker mtime comparison. The Stop and PostToolUse hooks stamp
`state/activity/<id>` *after* the session writes its report (writing the report is itself a tool
use; the turn-end Stop fires after that), so activity is always newer than the report — an mtime
compare made every finished session look unresolved and fired a false `session_idle` (+8m) and
false `frozen` (+45m) on done work, which frozen-recovery would then *restart*, deleting the report
(the P0 the implementation review caught). Existence is the robust signal. Two further guards:

- **Settled-status gate:** idle/frozen never fire for a PR whose `run.json` status is
  `success`/`in_review`/`published`/`merged`/`parked`/`done_failed` — the orchestrator already
  owns it, so a parked-but-alive session or a published PR is never falsely nudged/restarted.
- **Dedup un-latch on marker removal:** the finished/blocked/exited dedup keys are dropped the
  moment the marker file is absent, so after the orchestrator answers a block (`rm`s the marker)
  or `launch-pr.sh` clears a report on restart, a genuine *re-create* — even byte-identical —
  re-fires. To re-arm idle/frozen for a session you deliberately resume **after** it reported,
  remove its `reports/<id>.md` first (the orchestrator does this when it chooses to re-drive a
  reported session; `launch-pr.sh` does it on every relaunch).

## Tiers (how silence is handled)

- **`session_idle`** (default `IDLE_SECONDS` = 8 min): the cheap, fast catch for a session that
  rested without a report and without a blocked marker (e.g. it idled mid-task, or finished but
  forgot the report). The orchestrator **peeks** via the safe `nudge-pane.sh` primitive — which
  refuses a dead pane and defers at a menu — and only sends if the pane is a live, idle/queuing
  Claude. A session merely awaiting its own background work resumes (its activity advances) and
  never crosses this line; if it set `state/awaiting/<id>`, the idle peek is suppressed outright.
- **`frozen`** (default `FREEZE_SECONDS` = 45 min): the hard backstop. Same safe peek, then the
  stuck-recovery ladder (resume → restart → rewrite → abort), counting toward the retry cap.

This is the resolution of the 45-min blind spot the v1 "report-only" idea would have created:
the idle tier recovers an idled-no-report session in ~8 min (not 45) **without** re-introducing
false wakes, because awaiting/background-work sessions resume before the idle threshold.

## Doorbell (ring) — idempotent & truthful (RC2/RC3); two wake channels (WS1/WS4)

The orchestrator has **two** ways to wake, which cover each other:
1. **The doorbell ring** (`wake-orchestrator.sh` → `nudge-pane.sh`) — the low-latency fast path. It
   is a `cmux send`, so it only delivers when cmux's display is awake. WS1 repaired the classifier
   that mis-read the modern `❯`+NBSP idle composer as a "menu" and deferred 119/119 rings in
   run-20260626-1656.
2. **The standing self-wake** (a `ScheduleWakeup` the orchestrator re-arms every ~10 min, §1/§7) —
   the display-independent liveness *layer*. It fires *inside* the running Claude process, so a
   locked-but-awake Mac can't block it. It is **LLM-driven, not deterministic** — backstopped by the
   deterministic detectors below.

- Ring **once** when a new event appears; while events stay undrained, re-ring only every
  `RING_BACKOFF` (default 90 s) — not every 15 s tick.
- **Delivery is judged by drain PROGRESS, never by the send exit code.** `wake-orchestrator.sh`
  returning 0 means "typed + Enter", which is NOT "a turn was taken" (mid-generation input
  coalesces). The watcher confirms delivery by watching **`state/last_progress`** advance (events
  actually moved) and the queue empty — keyed on PROGRESS, not "woke", so a half-broken wake that
  stamps `last_drain` but acks nothing is still caught (WS4 split).
- **Loud, deterministic alarms the external watchdog detects (WS2/WS3).** Every alarm lands as a
  `state/ALERT` file (existence = active; JSON names the reason) + a greppable `{"event":"alert",…}`
  line in `watcher.log` + a best-effort `cmux notify` echo, and **re-arms every ~30 min while still
  active** (not once-per-episode):
  - **Stall** — queue non-empty and `last_progress` flat for `RING_STALL_SECONDS` (default 30 min,
    safely longer than any single legit wake turn, so a busy orchestrator never false-alarms).
  - **Doorbell-health (WS3)** — ≥`RING_HEALTH_THRESHOLD` consecutive deferred/failed rings AND
    `last_progress` flat for ≥2 self-wake intervals. Keyed on ring **exit codes** + the progress
    marker, never the screen, so it still pages if `classify_screen` re-breaks on a future render.
    The "both channels confirmed down" gate is the anti-cry-wolf calibration: a healthy self-wake
    advances progress and silences it.
  - **No-progress baseline (B2)** — `last_progress` has no deterministic writer (only the LLM
    orchestrator stamps it). If it **never appears** while events pile for the full stall window, the
    watcher pages `no_progress_baseline` — so the LLM dependency failing (an orchestrator not on the
    WS4 protocol) is loud, not a silent revert to the old conflated-marker behaviour.
  - **Rotation overdue** — `rotation_due` emitted but the run.json rotation baseline never advanced
    within `ROTATION_OVERDUE_SECONDS` (default 45 min). Rotation is a required, watcher-verified
    step (fact-1 fix), not an orchestrator judgment call.
  - At startup the watcher emits a rate-limited **self-test** alarm so a misconfigured/headless host
    can't silently swallow the path; a missing `cmux` echo is loud-warned (file+log still fire).
- **Completion self-stop (WS5).** When `state.run_complete_observed(run, pr_ids)` is true (every
  tracked PR terminal AND nothing observed-but-untracked — the plan ∪ activity union, fact-2) the
  watcher writes `state/DONE` once and exits — a deterministic anti-zombie that does not depend on
  the LLM noticing completion (run-20260626-1656's watcher ran ~9 h past the final merge). The
  observed-union guard also stops a split sibling mid-registration from tripping a false DONE
  (run-20260629-1752 ran one unwatched for 884 s).
- **Watcher liveness:** the watcher writes `state/watcher.heartbeat` (epoch) every tick — free,
  no notification — so its own death (the run's silent-killer, since it is the sole observer) is
  detectable by William or the external launchd/cron watchdog. A push heartbeat is opt-in via
  `AUTOCODE_HEARTBEAT_SECONDS`.

**Out of scope (do not over-claim a smooth night):** repairing the doorbell does **not** fix
keystroke delivery into *newly created* tabs (RC6, a separate track), and the **external
process-watchdog** that restarts a dead watcher/wedged orchestrator from outside the Claude/cmux
process is William's separate floor — the only true mitigation for a self-wake chain that dies
mid-run, and for a truly-asleep (not merely locked) Mac.

## Pane writes — one safe primitive (RC6/RC-DEADPANE/RC-MENUREGEX)

Every write into any pane (the doorbell AND the orchestrator's resume/answer/nudge) goes through
`bin/nudge-pane.sh <surface> <pr-id> <message>`:

1. `state/exited/<pr-id>` present → exit **4 (DEAD)** — caller restarts, **never** types into bash.
2. `read-screen` → `lib/pane_state.classify_screen` → `menu`/`dead` → exit **3 (DEFER)**;
   `busy`/`idle` → proceed (Claude safely queues mid-generation).
3. send text + Enter; real send/send-key failure → exit **1**.

For the **orchestrator surface specifically**, classification fails **closed** (any
unrecognized/ambiguous footer → DEFER), because a stray Enter into the orchestrator corrupts the
brain of the whole run, and a deferred ring is merely retried (A5).

## The nobody-responds-for-8-hours standard (2026-07-01)

Paging is a convenience layer (daytime glances, morning digest), not a safety layer: nobody
answers a 3am page. Every failure mode must therefore land in one of two acceptable states with
NO human response: "the run continued around it" or "the run ended early and safely." Current
accounting:

| Failure mode | Unattended behavior | Mechanism |
|---|---|---|
| Guard/permission dialog | that action abandoned/parked; run continues | §2 bright line + dialog=menu DEFER (fact-3) |
| Rotation ignored | rotation_due event → required at wake end; overdue = ALERT (re-armed every ~30 min; a single not-due tick no longer resets the clock — L2/S7) | watcher (fact-1 + L2) |
| GitHub poll process killed | the **watcher** polls in-process (~90 s, hard timeout); no bg process to SIGTERM, no LLM polling turns | watcher-owned poll (H1/S5) |
| Blocked on an owner decision | orchestrator goes **dormant** (no polling turns): a long ~3 h dead-man self-wake; woken early by William's chat or a watcher `ship_update`/doorbell. **Dead-brain-while-dormant latency:** with an empty queue the stall alarm cannot fire, so a brain that dies *while* dormant is caught only by the ~3 h dead-man timer or William's external watchdog | §8 dormant-when-blocked (H1) + external watchdog |
| False all-terminal (split race) | run stays alive; watcher keeps observing | run_complete_observed union (fact-2) |
| Worker doom-loop | retry cap parks at 2; runaway ALERT at 4 | launch-pr.sh counter + watcher (fact-4) |
| Watcher death | orchestrator wake-loop relaunches it (singleton-safe) | §1 step 4 pulse check |
| Orchestrator death/wedge | stall + ring-health + no-baseline ALERTs → external watchdog | WS2/WS3/B2 (existing) |
| Launch not delivered | resume_at retry ×2 → park + ping | RC-LAUNCHVERIFY (existing) |
| 5h usage limit | resume_at → automatic resume | R4 (existing) |
| 7d usage limit | in-flight finishes; no new launches | §8 (existing, accepted stop) |
| fail_stop brake | new launches stop; in-flight finishes | §4 (existing, deliberate brake) |
| Checkpoint failure | degrade Fable→Opus→skip-logged; run continues | §5a degradation |
| True system sleep | everything freezes together; resumes on wake | accepted + UNTESTED (§7 caveat) |
