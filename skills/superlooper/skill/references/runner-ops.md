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

The runner is one foreground process. The normal way to run it is a **visible `superlooper run` in
a cmux tab you can watch**; launchd (below) is the keep-alive option for unattended nights.

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
    e.g. started outside cmux, or detached/`nohup` so it lost the cmux socket (the launchd caveat
    below). The message tells you to run it inside a cmux tab.
  - To pin a *specific* pane instead of the current tab's, pass `--pane <id>` or set `$SL_PANE`
    (an override; rarely needed).
- **Stop:** Ctrl-C (SIGTERM). It exits cleanly and **leaves in-flight sessions untouched** —
  nothing merges while it is down, so a stop is always safe. Restarting rebuilds all state from
  GitHub + disk (GitHub is the source of truth), so any manual daytime work you did is absorbed
  automatically and no launch is duplicated.
- **Status:** `superlooper status` renders the current lanes, the queue, and whether merges are
  frozen — read from the journal and disk, so it works whether or not the runner is up.

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
superlooper tidy --repo /path/to/repo --all           # also parked / needs-william / bounced
superlooper tidy --repo /path/to/repo --yes           # skip the confirmation
```

- **What it will close:** by default, only the windows of **merged** sessions (truly done). `--all`
  extends that to the other terminal states — **parked**, **needs-william**, **bounced**. It lists
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
  For a **re-approvable** session (`--all`'s parked / needs-william / bounced) it closes the window
  but **leaves the state markers to the runner** — that session could be re-approved and relaunched
  at any moment, and the runner's own relaunch path frees the stale lock and rewrites the marker,
  so tidy never touches state a live worker might be using. (Cost: a repeat `--all` may re-list an
  already-closed re-approvable window; closing it again is a harmless no-op.)
- Re-approving a `--all`-closed **parked** issue later is unaffected: the runner relaunches it from
  the issue in a fresh window. Closing a **merged** session's window never resurrects it — merged
  work is done.

---

## The morning report

Every day at **`report_time` (default 08:45, Mac-local)** the runner writes a report to
`reports/morning-YYYY-MM-DD.md` in the repo's state home and pushes you a notification. It is the
one batched, one-touch surface for everything that happened overnight — read it with coffee, act
on the few items that need you, ignore the rest.

Sections:

- **Merged** — issues/PRs that landed, cross-linked.
- **Parked / needs-william** — with the memo comment for each, so you can act without digging.
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
| `needs-william` | an **owner decision is required** — a bounce, a **conflict-cap** hit, a fail-closed gate, or an answerer that punted | decide: see "Answering a bounce" / "a parked conflict" below |
| `expedite` | **jump the queue** — slotted into the very next free lane ahead of everything | apply it to an issue you want built next |
| `preserve` (on a PR) | on a conflict, resolve **in the PR's own branch** instead of regenerating from scratch | apply it to a PR whose diff is expensive to rebuild |
| `model:<name>` (on an issue) | run **this issue's** worker sessions on `<name>` instead of the config default | apply it to an issue you want built on a specific model |
| `effort:<level>` (on an issue) | run **this issue's** worker sessions at reasoning effort `<level>` (nothing sent when absent) | apply it to a gnarly issue that needs more, or a trivial one that needs less |
| `superseded` (on a PR) | the loop replaced this PR with a rebuild on current dev; branch kept, PR left open, nothing auto-closed | none — housekeeping only |
| `auto-approved:nightly-red` | a fix issue the nightly filed to restore a red mainline; entered by your standing rule, not by hand | none — it builds automatically; the distinct label is just the audit trail |

**`parked` vs `needs-william`** is the distinction that matters: `parked` is *mechanical
exhaustion* (retries ran out — no decision pending, look when convenient); `needs-william` is *a
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
  (`model:opus`, `model:opus[1m]`, `model:fable`, `effort:low…max`), but any `model:<x>`/`effort:<x>`
  label you create and apply works. An **unknown** value fails the launch loudly and the retry cap
  parks the issue — so a typo surfaces as a parked issue with a memo, not a silent wrong-model run.

### Answering a bounce

A worker that finds **premise-level drift** — the problem is already gone, or what actually shipped
invalidates the approach — does not guess. It writes a `BOUNCED:` memo, and the **runner** (not the
worker) posts that memo to the issue, applies `needs-william`, and reclaims the lane. The memo
always includes a **ready-to-approve proposed amendment** to the Goal/DoD, so your touch is
**yes/no, never authoring**:

- **Yes** — the amendment is right: approve it (re-label `agent-ready`; the amended text becomes the
  brief on the next launch).
- **No** — re-scope it back through a normal planning conversation (Gate 1), or drop the issue.

You never edit the Goal/DoD in place, even here — approval flows through the label, not through an
edit (see `approval-protocol.md`).

### A parked conflict (`needs-william` from the conflict cap)

Two conflict-regenerations on one issue means two work items are fighting over the same code — a
scoping error only you can untangle. The runner parks it with `needs-william` and a memo naming the
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
`needs-william`, every freeze, and every ALERT — the standing rule that long-running work
finishing, stalling, or needing input reaches you (spec §2). A send failure is journaled, never
fatal; notifications are a convenience layer, never a safety layer.

**One-time setup:** the first time it texts you, macOS asks permission to let the terminal control
Messages — click **Allow** once. A **launchd-started** runner needs that same permission granted to
whatever launchd runs it under, so grant it once there too, or the first night's texts silently
no-op (they're journaled, so you'll see it in the log).

---

## launchd (keep-alive for unattended runs)

The default is a visible `superlooper run` in a cmux tab. For unattended keep-alive, install the
launchd jobs from the templates:

- **`launchd.runner.plist`** — `KeepAlive=true`, label `com.superlooper.<owner>__<repo>`, logs to
  the repo's state home. Relaunches the runner if it dies. **Caveat (D7):** a *detached* launchd
  process is not inside a cmux tab, so it can't self-detect a pane and its `new-surface` calls fail
  ("Broken pipe" — lost cmux socket). The runner now catches this at startup and refuses to run
  rather than burning retry caps. The working keep-alive is a `superlooper run` in a **visible cmux
  tab** (which you can also arrange to relaunch on login); the pure-daemon launchd path needs a
  cmux surface to launch into and is not the blessed unattended mode.
- **`launchd.nightly.plist`** — `StartCalendarInterval` at `qa.nightly_time` (02:00 Mac-local),
  invoking `superlooper nightly --repo <path>`.

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
