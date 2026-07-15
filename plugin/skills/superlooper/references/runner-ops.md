# Operating the loop

This is the day-to-day operator's guide, written for William. The **runner** is a small,
deterministic process on your Mac — no AI inside it, zero model tokens to run, one per adopted
repo. It takes `agent-ready` issues, builds each in a fresh coding-agent session in its own worktree,
and merges to the dev mainline when the mechanical gate passes. It is designed to run unattended
and to fail *safely*: every problem lands as either "continued safely around it" (one issue
parks, the rest keep going) or "stopped early and safely" (merges freeze, nothing half-lands,
a restart rebuilds from GitHub + disk).

**Two human gates, a machine middle.** You approve issues by your word (Gate 1 — see
`approval-protocol.md`); the machine builds and merges to dev; **you** decide dev→prod promotion
(Gate 2 — evidence, never a switch; below). Everything in between is mechanical.

**Scope.** This is 1→1.2 machinery for a repo that already works — the e2e/browser test gate is
built collaboratively with you *before* the loop leans on it (spec §2). The loop is universal: it
runs on any repo through `.superlooper/config.json`, with nothing repo-specific baked into the
skill. To wire up a new repo, see `docs/ADOPTING.md` (every config field + the label set) and the
doctor checklist at the end of this file.

---

## Start, stop, status

The runner is one foreground process, and there is exactly **one** way to run it: a **visible
`superlooper run` in a cmux tab you can watch**. launchd runs the *nightly* (below), never the
runner — a launchd runner is a detached daemon with no cmux tab, which can't work (see
"Restarting the runner" and the launchd section for why).

(The bare `superlooper` command comes from publishing: `bin/install.sh` links it onto your PATH,
pointing at the installed copy. If your shell can't find it, re-run the installer — it prints the
exact PATH line to add. See `docs/ADOPTING.md` → "Getting the `superlooper` command".)

```bash
superlooper run    --repo /path/to/repo      # the tick loop (foreground; Ctrl-C to stop)
superlooper status --repo /path/to/repo      # lanes / queue / freeze state, from journal + disk
```

- **Start:** `superlooper run` in a cmux tab — **that's it, no pane id to set.** The runner
  auto-detects the pane of the tab it's running in (via `cmux identify`) and opens every worker as
  a sibling tab in that same pane, grouped and watchable. This survives a machine restart that
  reassigns pane UUIDs — you never hardcode a pane. It takes a pidfile singleton, so a second `run`
  on the same repo refuses to start rather than double-drive it.
  - It **fails hard at startup** (never a quiet warning) if it can't reach cmux or resolve a pane —
    e.g. started outside cmux, or detached/`nohup` so it lost the cmux socket (the same reason there
    is no launchd runner — see the launchd section below). The message tells you to run it inside a
    cmux tab.
  - To pin a *specific* pane instead of the current tab's, pass `--pane <id>` or set `$SL_PANE`
    (an override; rarely needed).
- **Stop:** Ctrl-C (SIGTERM). It exits cleanly and **leaves in-flight sessions untouched** —
  nothing merges while it is down, so a stop is always safe. Restarting rebuilds all state from
  GitHub + disk (GitHub is the source of truth), so any manual daytime work you did is absorbed
  automatically and no launch is duplicated.
  - **If you run the unattended-debugger watchdog LaunchAgent** (below), `touch
    <state-home>/state/WATCHDOG_OFF` **before** a deliberate stop and **delete it when you
    restart**. A stopped runner leaves its heartbeat to go stale, which is exactly the fault the
    watchdog exists to catch — so without the kill-switch it will text you and, after the grace,
    launch an sl-debugger session against a loop you stopped on purpose. (The watchdog cannot
    tell a deliberate stop from a crash; the kill-switch is how you tell it.)
- **Status:** `superlooper status` renders the current lanes, the queue, and whether merges are
  frozen — read from the journal and disk, so it works whether or not the runner is up.

**One command for the runner *and* the command-center dashboard (optional).** If you run the
command-center dashboard, its `bin/liftoff` brings up — or verifies already-running — **both** from a
single cmux tab: it starts the dashboard in the background, then foregrounds `superlooper run` in
that tab, exactly as below. So the runner still lands in a visible cmux tab (this same procedure),
and `liftoff` just spares you starting the dashboard separately. It's idempotent (a second run
double-starts neither) and stays on the dashboard side — it shells the runner's own `superlooper run`
through the dashboard's configured CLI path; the runner knows nothing about it. See the
command-center README ▸ *One command*. Everything below applies unchanged whether you start the
runner directly or through `liftoff`.

### Restarting the runner (the proven procedure)

There is **one** way to (re)start the runner, and it is by hand: **open a tab in the cmux window you
want the loop to live in, and run `superlooper run --repo <path>`.** The runner detects its own pane
(`cmux identify` → the tab you are IN) and opens every worker as a sibling tab there — no pane id,
no placement flag. The boot line prints the anchor it locked onto:

```
superlooper run: repo=… state=… pane=<uuid> [this cmux tab] workspace=<uuid> window=<uuid> agent=claude
```

**Check that `window=` names the window you intended before you walk away** — a runner started in
the wrong window is visible right there, not hours later when every launch has parked. (This is the
proven procedure from the 2026-07-09 incident, owner-ruled: a human opens the tab and runs
`superlooper run`; the self-pane detection does the rest.)

This is not a stopgap for missing automation. **Automated tab-placement is out**, and it stays out
because it was tried and it failed — two automated placement attempts each broke a *different* way
the same night (2026-07-09):

- **Focused-window fallback.** The placement targeted whatever cmux window was *focused* when it
  fired, not the intended one — so the runner, and every worker tab it would open, landed in the
  wrong window. cmux `identify` reports both `caller` (the tab you are IN) and `focused` (whatever
  is focused right now); anything that resolves placement from `focused` misplaces the runner.
- **CLI-created workspace whose tabs never boot shells.** Creating the target workspace from the CLI
  produced tabs whose shells never started — so the launch shim (sourced by a booting `~/.zshrc`)
  never ran, and every worker launch was dropped silently, with no shell to receive it.

A human opening a real tab sidesteps both: the tab *is* the `caller`, and its shell boots and
sources the shim like any other. So the restart story is deliberately manual — and the boot-line
anchor plus `doctor`'s live-anchor check (see the doctor checklist) are how you confirm it landed in
the right window.

### Agent selection

Claude remains the default:

```bash
superlooper run --repo /path/to/repo
superlooper run --repo /path/to/repo --agent claude
```

Codex v1 is opt-in:

```bash
superlooper run --repo /path/to/repo --agent codex
```

The queue, labels, merge gate, issue protocol, and command-center surfaces are unchanged by the
agent choice. The runner sets `SL_AGENT=codex` only at the launch/nudge boundary; Codex sessions
are interactive `codex` sessions in the issue worktree, not `codex exec`.

Codex launch options live in `.superlooper/config.json`:

```json
"codex": {
  "dangerous_bypass": false,
  "bypass_hook_trust": true,
  "no_alt_screen": true
}
```

`dangerous_bypass` is the explicit switch for Codex's dangerous approval/sandbox bypass flag. Leave
it `false` unless you are supervising that repo and accept the same risk profile as Claude's
permission-bypassed worker sessions. `bypass_hook_trust` and `no_alt_screen` default on because the
loop installs its own hooks and needs stable pane reads.

Codex usage/quota accounting is intentionally deferred in v1. A Codex pane that shows trust,
permission, quota, or unknown attention states is treated as a safe defer; the runner does not
estimate Codex quota or make scheduling decisions from it yet.

Automated tests use fake `codex`, fake `cmux`, and temporary `CODEX_HOME`/`HOME` fixtures only.
Real cmux/Codex smoke tests are supervised/manual, not part of the automated suite. When you want a
real smoke, run:

```bash
superlooper doctor --repo /path/to/repo
superlooper run --repo /path/to/repo --agent codex --ticks 1
```

Expected observations: doctor shows Claude hooks as passing and Codex hooks as either passing or a
Codex-only warning to fix before `--agent codex`; the run starts inside a visible cmux tab, prints
`agent=codex`, resolves the pane, and exits after one tick. For a live issue smoke, apply
`agent-ready` to one small issue, run without `--ticks`, and watch for a sibling cmux tab whose
command is an interactive Codex session in `state/worktrees/i<N>` with the issue brief as the
initial prompt.

---

## Tidying up finished session windows

A finished worker session does **not** close its own cmux window — a real `claude` idles at its
prompt forever after writing its report (dry-run finding D4), so over a busy run the windows of
merged (and parked/bounced) sessions pile up. `superlooper tidy` closes them on **your** say-so —
it is a manual command, never wired into the runner, a schedule, or any automatic path (the V1
"nothing auto-closed" rule stands; closing a window is your word, like `agent-ready`).

```bash
superlooper tidy --repo /path/to/repo                 # close MERGED sessions' windows (asks y/N)
superlooper tidy --repo /path/to/repo --dry-run       # just list what it WOULD close
superlooper tidy --repo /path/to/repo --all           # also parked / needs-owner / bounced
superlooper tidy --repo /path/to/repo --yes           # skip the confirmation
```

- **What it will close:** by default, only the windows of **merged** sessions (truly done). `--all`
  extends that to the other terminal states — **parked**, **needs-owner**, **bounced**. It lists
  every window (issue id, status, surface) and asks `y/N` before closing; `--dry-run` prints the
  list and closes nothing; `--yes` skips the prompt.
- **What it can never close:** anything still **in flight** ({running, blocked, frozen, exited}) or
  **mid-gate** ({gating, holding}) — those are excluded mechanically, so **tidy is safe to run
  while the runner is live**. It also skips any session with no recorded window (nothing to close).
  It always closes the exact surface it listed (captured up front), so a relaunch that happens
  while you read the list can never redirect a close onto a fresh, live window.
- **How it closes:** the same best-effort close the runner uses (`cmux close-surface`, exit code
  ignored — a dead window is a silent no-op). For a **merged** session it then clears the pane
  markers and singleton lock (safe to do — merged work never relaunches, so nothing is racing it).
  For a **re-approvable** session (`--all`'s parked / needs-owner / bounced) it closes the window
  but **leaves the state markers to the runner** — that session could be re-approved and relaunched
  at any moment, and the runner's own relaunch path frees the stale lock and rewrites the marker,
  so tidy never touches state a live worker might be using. (Cost: a repeat `--all` may re-list an
  already-closed re-approvable window; closing it again is a harmless no-op.)
- Re-approving a `--all`-closed **parked** issue later is unaffected: the runner relaunches it from
  the issue in a fresh window. Closing a **merged** session's window never resurrects it — merged
  work is done.

---

## The janitor: GitHub-side debris, propose-and-approve

As the loop runs, debris accumulates **on GitHub** that no other mechanism owns: stale `sl/*`
remote branches whose PRs merged or were superseded, PRs labeled `superseded` left open by design
(the regenerate ladder never auto-closes them), and parked / needs-owner issues gathering dust.
`superlooper janitor` is tidy's discipline pointed at GitHub: it **proposes** a one-touch list,
each item with a one-line why, and executes **only what you approve** — the y/N (or `--yes`) is
your word, like `agent-ready`. Nothing is ever auto-closed or auto-deleted; there is no schedule
wiring for the execute path and none may ever be added.

```bash
superlooper janitor --repo /path/to/repo                  # propose, then ask y/N
superlooper janitor --repo /path/to/repo --dry-run        # just list; changes NOTHING anywhere
superlooper janitor --repo /path/to/repo --yes            # skip the confirmation (still your word)
superlooper janitor --repo /path/to/repo --retry-refused  # re-propose previously failed actions
```

- **What it proposes:** (1) *delete* a remote `sl/*` branch whose PR **merged**, or whose PR is
  **closed and labeled `superseded`** — never a branch with no PR, an open PR, or a closed-unmerged
  PR without the label, and only when the branch's current tip is still the PR's last-known head
  (commits pushed after the merge/close keep the branch off the list — an unmerged branch's work
  is never proposed for deletion); (2) *close* an
  **open PR labeled `superseded`** (the branch stays — it becomes deletable on a *later* sweep,
  once its PR is closed); (3) *close* a **parked / needs-owner issue** with no activity for
  `janitor.aged_park_days` (config, default 14).
- **What it can never propose:** anything in-flight or mid-gate ({running, blocked, frozen,
  exited, gating, holding}) — excluded mechanically by the issue number in the branch name AND by
  the loopstate-recorded branch. If `state/issues.json` is unreadable, the janitor refuses to
  propose anything at all (nothing is provably idle).
- **How it executes:** after your y/N it re-fetches and re-derives, executing only items that are
  *still* eligible — a re-approval that happened while you read the list can never get its branch
  deleted. Every approved action is journaled (`act: janitor`); a refused/failed action surfaces
  once (loud FAIL line, nonzero exit, `state/janitor_refused.json`) and is held back from future
  sweeps — never silently retried — until `--retry-refused`.

---

## The morning report

Every day at **`report_time` (default 08:45, Mac-local)** the runner writes a report to
`reports/morning-YYYY-MM-DD.md` in the repo's state home and pushes you a notification. It is the
one batched, one-touch surface for everything that happened overnight — read it with coffee, act
on the few items that need you, ignore the rest.

Sections:

- **Merged** — issues/PRs that landed, cross-linked.
- **Parked / needs-owner** — with the memo comment for each, so you can act without digging.
- **Bounces** — issues a worker bounced on premise drift, each with its proposed amendment.
- **Conflict regenerations this week** — the tuning metric: if this climbs, tighten `affinity` or
  reduce `lanes`; if it's always zero, you can loosen. This is how you turn the parallelism dial.
- **Wanders** — PRs whose actual diff touched areas the issue didn't declare in `touches:`.
- **Gate health** — nightly pass rate, flake count, quarantine size.
- **Freeze state, usage, queue depth + next up.**

A quiet night renders "nothing happened, queue empty" honestly — no news is real news.

---

## Label semantics (what each state means and what it asks of you)

The runner drives these; a few ask for a decision from you.

| Label | Meaning | Your action |
|---|---|---|
| `in-progress` | a worker is building it now | none — watch if you like |
| `parked` | the build **failed its retry cap** (relaunched, still not done); handed back with a memo | when you have time: read the memo, re-scope or re-approve, or drop it |
| `needs-owner` | an **owner decision is required** — a bounce, a **conflict-cap** hit, a fail-closed gate, or an answerer that punted (renamed from `needs-william`; `adopt` migrates the old label in place and the runner recognizes both) | decide: see "Answering a bounce" / "a parked conflict" below |
| `expedite` | **jump the queue** — slotted into the very next free lane ahead of everything | apply it to an issue you want built next |
| `preserve` (on a PR) | on a conflict, resolve **in the PR's own branch** instead of regenerating from scratch | apply it to a PR whose diff is expensive to rebuild |
| `model:<name>` (on an issue) | run **this issue's** worker sessions on `<name>` instead of the config default | apply it to an issue you want built on a specific model |
| `effort:<level>` (on an issue) | run **this issue's** worker sessions at reasoning effort `<level>` (nothing sent when absent) | apply it to a gnarly issue that needs more, or a trivial one that needs less |
| `superseded` (on a PR) | the loop replaced this PR with a rebuild on current dev; branch kept, PR left open, nothing auto-closed | none — housekeeping only |
| `auto-approved:nightly-red` | a fix issue the nightly filed to restore a red mainline; entered by your standing rule, not by hand | none — it builds automatically; the distinct label is just the audit trail |

**`parked` vs `needs-owner`** is the distinction that matters: `parked` is *mechanical
exhaustion* (retries ran out — no decision pending, look when convenient); `needs-owner` is *a
specific decision only you can make* (look sooner). Both always carry a memo comment.

### Per-issue model / effort (control knobs)

`model:<name>` and `effort:<level>` are **your** knobs, like `expedite`/`preserve`: apply or remove
them any time — they never touch the frozen issue text, and issue-writers don't set them. They
change which model / reasoning effort **that issue's worker sessions** run on (first launch,
crash-relaunch, and a regenerated rebuild all inherit them — the override rides the label, not the
first launch). The **answerer** is never affected; it stays on `models.answerer` from config.

- **Precedence (model):** issue `model:*` label → `models.worker` in config → the built-in default.
  **Effort:** issue `effort:*` label → `models.worker_effort` in config (a repo-wide default; `null`
  by default) → nothing sent. So with no label and no config default, no `--effort` flag is passed.
- **Exactly one each.** Two `model:*` (or two `effort:*`) labels on one issue is ambiguous, so the
  runner **refuses to launch it** until you fix the labels — exactly like a missing/duplicate
  `type:` label. It waits; it never guesses.
- **No allowlist.** The value is passed straight to the agent. `adopt` seeds a starter set
  (`model:opus`, `model:opus[1m]`, `model:fable`, `model:sonnet`, `effort:low…max`), but any
  `model:<x>`/`effort:<x>`
  label you create and apply works. An **unknown** value fails the launch loudly and the retry cap
  parks the issue — so a typo surfaces as a parked issue with a memo, not a silent wrong-model run.

### Answering a bounce

A worker that finds **premise-level drift** — the problem is already gone, or what actually shipped
invalidates the approach — does not guess. It writes a `BOUNCED:` memo, and the **runner** (not the
worker) posts that memo to the issue, applies `needs-owner`, and reclaims the lane. The memo
always includes a **ready-to-approve proposed amendment** to the Goal/DoD, so your touch is
**yes/no, never authoring**:

- **Yes** — the amendment is right: approve it (re-label `agent-ready`; the amended text becomes the
  brief on the next launch).
- **No** — re-scope it back through a normal planning conversation (Gate 1), or drop the issue.

You never edit the Goal/DoD in place, even here — approval flows through the label, not through an
edit (see `approval-protocol.md`).

### A parked conflict (`needs-owner` from the conflict cap)

Two conflict-regenerations on one issue means two work items are fighting over the same code — a
scoping error only you can untangle. The runner parks it with `needs-owner` and a memo naming the
issue it collided with. Re-scope one of the two so they stop overlapping (this is also what
`affinity: hard` and honest `touches:` declarations prevent up front). For an expensive PR you'd
rather not rebuild, apply `preserve` to route it to a conflict-resolution session in its own branch.

---

## The freeze state (fix-forward — a safe idle, not an emergency)

If dev main goes red after a merge (post-merge CI or the nightly), the runner **freezes further
merges**, auto-files a fix issue at the head of the queue, and **keeps building** — freezing stops
*merges*, never *builds*. Red dev is contained; prod is never exposed (that's Gate 2's job).
**Frozen-but-building is the safe idle state** — it is not something to escape at 3am.

- Overnight, a red **nightly** files its fix as `type:diagnose-and-fix` +
  `auto-approved:nightly-red` + `expedite`, scoped strictly to restoring green (never opportunistic
  improvements). This is your standing rule at work — no agent approved anything.
- If that fix fails its cap, merges **stay frozen until morning** and you'll see it at the top of
  the report. That's correct: a frozen-but-building loop is the designed-for safe state.
- When dev goes green again, the runner **unfreezes** on its own.

Occasional cross-PR semantic breaks on dev, a rare silent overnight stop, and the odd stuck label
are **designed-for tolerances** (spec §2) — the loop is built to contain them, not to be
over-engineered against them. If one bites, the morning report shows it and a restart recovers.

---

## Promotion (Gate 2 — evidence + your judgment, never a switch)

Promotion of dev→prod is **your** deliberate, batched decision. There is deliberately **no
"must-pass-everything-to-promote" logic anywhere** — the loop produces evidence; you decide.

```bash
superlooper promote-report --repo /path/to/repo      # (or --use-latest-nightly)
```

This writes `reports/promotion-YYYY-MM-DD.md`: the full suite's results diffed against the
**known-failure ledger** (NEW failures highlighted, already-accepted ones folded away), a summary
of everything merged since the last promotion, and the open-issue list. **No pass/fail verdict
appears anywhere** — it is evidence only.

When you accept a failure as non-blocking, that acceptance **persists** (fingerprinted to the
failure's content, not to a commit — one approval, ever), so the same finding never re-blocks:

```bash
superlooper accept-failure <fingerprint> --note "known-flaky third-party widget"
```

New findings discovered during promotion become ordinary queue issues; they never stand in front of
the gate. If you ever want a hard blocker (a compliance-critical flow, an unresolved security
finding), you define it — and even then it means "requires your explicit override," never "cannot
promote."

---

## Nightly QA

```bash
superlooper nightly --repo /path/to/repo             # (usually launchd-scheduled, below)
```

At `qa.nightly_time` (default 02:00, Mac-local) the runner builds a fresh worktree of dev, runs
`qa.nightly_cmd` (your full simulated-user browser suite), and parses the results. A failure that
**clears on one retry** is a flake (gate-health stats only); a **persistent** failure that isn't in
the known-failure ledger **freezes merges and files a fix issue** with the standing-rule labels.
This is the layer that catches cross-PR interactions between promotions. (`qa.nightly_cmd` is null
until the browser suite exists — it's built with you first; the config just points at it.)

---

## Notifications

The runner texts you via your Mac's own Messages app (config `notify.imessage_to`), falling back to
`notify.cmd`, then `cmux notify`, then log-only. It fires on every transition to `parked` or
`needs-owner`, every freeze, and every ALERT — the standing rule that long-running work
finishing, stalling, or needing input reaches you (spec §2). A send failure is journaled, never
fatal; notifications are a convenience layer, never a safety layer.

**One-time setup:** the first time it texts you, macOS asks permission to let the terminal control
Messages — click **Allow** once. The **launchd-started nightly** needs that same permission granted
to whatever user launchd runs it under, so grant it once there too, or the first night's texts
silently no-op (they're journaled, so you'll see it in the log).

---

## launchd (the nightly only — there is no launchd runner)

launchd runs exactly **one** superlooper job: the nightly QA. There is deliberately **no launchd
runner** and no runner keep-alive template — the runner is started and restarted by hand in a cmux
tab (see "Restarting the runner" above).

**Why there is no launchd runner (issue #33).** A launchd-started process is a detached daemon with
no cmux tab of its own. The runner launches every worker as a tab in its own pane
(`new-surface --pane`), so it *needs* a pane — and a paneless daemon cannot self-detect one. Its
startup preflight (correctly) fails hard in that case, and a `KeepAlive=true` would just relaunch it
into the identical failure forever, filling the log while nothing ever launches. There is no way to
make launchd start the runner *correctly*, because the only correct start is inside a cmux tab and
automated tab-placement is out (the two 2026-07-09 failure modes above). So the old
`launchd.runner.plist` template — whose own comment advertised its `KeepAlive` as harmless
crash-recovery — was **removed, not fixed**: it offered a mode that could never work. If you still
have it installed from an earlier version, `launchctl unload` it and delete the plist; keep-alive of
the runner is the visible cmux tab, which you can arrange to reopen on login.

- **`launchd.nightly.plist`** — `StartCalendarInterval` at `qa.nightly_time` (02:00 Mac-local),
  invoking `superlooper nightly --repo <path>`. This one **does** run under launchd, because
  `superlooper nightly` needs no cmux pane — it builds a fresh worktree and runs the browser suite,
  and never opens worker tabs. (A nonzero exit is journaled + pushed, never restart-looped: it is a
  scheduled one-shot, not a keep-alive.)

All schedule times are **Mac-local** — your Mac runs Mountain time, launchd and the runner both
read the system clock, so there is no timezone setting to get wrong.

**External-watchdog contract.** The runner writes `state/runner.heartbeat` (epoch) at the END of
every SUCCESSFUL tick, and raises `state/ALERT` (a JSON file naming the reason) on a persistent
GitHub failure, a launch runaway, usage stale > 1h, or the loop itself wedging (≥4 consecutive tick
crashes, `runner_tick_errors:*`). A watchdog that only needs to know "is the loop alive and
healthy?" watches those two files — a stale heartbeat or a present ALERT is the whole signal, no
model required. The heartbeat deliberately marks tick *progress*, not mere process liveness: a tick
that crashes part-way leaves the heartbeat stale (the pidfile `state/runner.lock` still shows the
process is up), so a runner that is alive-but-wedged reads as stale, not healthy (incident
2026-07-07 — it used to stamp at the tick's TOP and a 42-min wedge read as perfectly alive).

---

## The unattended-debugger watchdog (`superlooper watchdog`, issue #66)

The shipped implementation of that contract, plus a third detector. `superlooper watchdog --repo
<path>` is ONE mechanical check — no LLM anywhere on the path, no repair decisions: it detects,
notifies, waits, launches, journals. Wire it to fire every few minutes by loading
`templates/launchd.watchdog.plist` as a user **LaunchAgent** (a check needs no cmux pane, so
launchd is fine here — the issue-#33 prohibition is about the *runner*).

**Trips on** (owner standing rule, 2026-07-10):
- `heartbeat_stale` — `state/runner.heartbeat` older than `watchdog.heartbeat_stale_minutes`
  (default 20). An ABSENT heartbeat never trips: the loop never ran in this state home.
- `alert` — `state/ALERT` present (even unreadable: existence is the signal).
- `no_progress` — work the SCHEDULER would launch RIGHT NOW exists (its own gh read, run through
  `scheduler.launchable` with the real lane state + territory claims, so every scheduler hold is
  respected), every lane is empty, and that has held for `watchdog.no_progress_minutes`
  (default 30) with a FRESH heartbeat and a usage meter that does NOT read exhausted.

**Never trips on designed-safe waits:** gate-waiting on CI and building work are `in-progress`
(not eligible); blocked-by holds wait for the dependency to close; parked / needs-owner is not
approval; a building lane during a merge freeze is lanes-busy (frozen-but-building is the safe
idle state); **a finished PR gate-waiting on CI (or holding through a merge freeze) holds a
territory claim** that occupies no lane but keeps overlapping eligible work behind it — the
no-progress view runs through `scheduler.launchable`, so that held work is not counted as
launchable (issue #92); a usage meter that successfully READS exhausted is the fail-closed hold
working (a DARK meter never suppresses — the #46/#76 asymmetry, so a Keychain-less launchd context
cannot neuter the detector). When the no-progress view is UNOBSERVABLE this check (gh unreachable —
a probe blip OR a refused list read), the clocks FREEZE and an open no_progress episode is HELD,
not stood down: a gh blip cannot drop the episode and re-trip it (a duplicate text + a restarted
grace) on recovery.

**The flow.** First trip → one text (naming the signal, the grace, the authority tier) → the
grace window (`watchdog.grace_minutes`, default 30) → if the signal still stands, ONE fresh
sl-debugger session launches through the same interactive launch shim workers use
(`launch-session.sh --cwd <repo> d<N>` — never a headless `claude -p`), its brief carrying the
tripped signal and the standing `watchdog.authority` tier; the session follows the sl-debugger
skill's `references/unattended-contract.md`. If the signal cleared meanwhile, it stands down
SILENTLY (journal only). The launch tab targets the runner's recorded anchor pane
(`state/runner.anchor.json`); with no resolvable pane the launch fails LOUDLY into a journaled +
notified outcome — never a fabricated success.

**Rails.** Singleton (a live `worker.d*.lock` blocks a second session, and concurrent checks
yield on `state/watchdog.lock`); once-per-incident (a continuing episode never relaunches — a
genuinely new episode after recovery may); failed launches retry at most 3× with ONE failure
text; every transition is journaled (`act: "watchdog"`) and every launch — verified or failed —
appears in the morning report's "Unattended debugger" section. **Kill-switch:** `touch
<state-home>/state/WATCHDOG_OFF` — the check keeps observing and journaling but notifies and
launches nothing; delete it to re-arm. Episode state lives in `state/watchdog.json`; deleting it
resets the clocks (safe — the bounds simply restart).

---

## The doctor checklist

Run this until it is all-green before starting the runner on a repo; it changes nothing, only
reports:

```bash
superlooper doctor --repo /path/to/repo
```

It verifies: `gh` is authenticated; `cmux` is on PATH; `jq` is present; the launch shim is
installed; the two Claude activity hooks are registered in `~/.claude/settings.json`; the config
parses; the labels exist; and **`required_checks` is non-empty** — a repo with no CI check enforcing
its own tests has no mechanical ship gate, so `doctor` **fails hard** on an empty `required_checks`
and `adopt` prints the same requirement. It also reports Codex hook readiness from
`$CODEX_HOME/hooks.json` or `~/.codex/hooks.json` as a warning when missing, because Codex is opt-in.
Fix anything red, then start the runner.

**`--stack` also checks a live runner's anchor.** `superlooper doctor --stack` runs the machine-level
checks (cmux, claude/gh/codex auth, the notify channel, the launch shim) *and* — new (issue #33) — a
**runner anchor (live)** check. When a runner is live (its pidfile pid is alive, and it recorded a
matching anchor), it re-runs the same read-only pane probe the startup preflight uses against the
anchor the runner recorded at launch (`state/runner.anchor.json`) — **scoped to the runner's own
recorded workspace**, so it gives the same answer whichever tab you run `doctor` from. If that pane
no longer resolves in the workspace it launched in (its cmux tab was closed or moved), it **FAILs**
with the restart hint, so you catch a runner that is up but launching into a dead pane before the
whole queue parks. With no live runner, a stale pidfile, or no matching recorded anchor it is a clean
pass/warn — nothing to fail — so it is safe to run both before starting and while the loop is up.
