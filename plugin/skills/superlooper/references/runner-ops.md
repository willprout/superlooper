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

## First: which runner home is this repo on?

**Almost every "how do I start / stop / restart it?" answer on this page branches on one config
key, so read it before you do anything else.** `runner_home` in `.superlooper/config.json` selects
where the runner's own process lives:

```bash
superlooper runner-home --repo /path/to/repo      # read-only: which home, the job, and whether it is live
```

| | `pane` (the default) | `login-item` |
|---|---|---|
| Where the runner runs | a visible session tab **a person opened** | a `gui/$UID` LaunchAgent |
| Started by | `superlooper run`, typed in that tab | `RunAtLoad`, at login |
| Stopped by | Ctrl-C in the tab | `launchctl bootout` on the job |
| Restarted by | re-exec in place, or a fresh tab | exiting — `KeepAlive` brings it back |
| Boot preflight | the tab's pane resolves | Aqua session, PATH, `gh` login, the session host answers |
| Doctor block | `runner anchor (live)` | `runner home` |

The 2026-07-31 owner ruling (`docs/HERDR-ADOPTION-PLAN.md` §8.1) moved the runner **outside the
session host** — the supervisor never lives inside what it supervises — and that is what the
`login-item` home is. The `pane` home is the older arrangement and keeps every one of its original
rules; a config written before the split keeps behaving exactly as it did.

The design record for both homes, including the explicit disposition of every verb that was built
around the old one, is `skills/superlooper/docs/RUNNER-HOME.md` in the source checkout — cited by
path rather than linked, because it is not one of the pages published beside this one on a machine
that only *runs* the loop. This page is the playbook; that page is the record. Where they disagree,
the code wins and both are wrong.

---

## The verbs, at a glance

Every one takes `--repo <path>` (default: cwd). `tidy` and `janitor` are the two that clean up
after the loop — each proposes first and executes only what you approve. The rest either read, act
only on their own schedule, or refuse rather than guess.

| Verb | What it does | Writes anything? |
|---|---|---|
| `superlooper run` | the tick loop itself — the `pane` home's foreground start | drives the loop |
| `superlooper runner-home` | which home this repo's runner lives in, the job, whether it is live; `--install` sets up the `login-item` LaunchAgent | only with `--install` |
| `superlooper request-restart` | ask the **live** runner to restart itself between ticks | drops a marker |
| `superlooper status` | heartbeat, ALERT, freeze state, lanes, gate and a journal tail — no GitHub read | no |
| `superlooper doctor` | the preflight checklist; `--stack` judges the machine and this repo's runner home | no |
| `superlooper upkeep` | the weekly once-over, read-only by construction | no |
| `superlooper tidy` | close FINISHED sessions' windows, on your y/N | on your word |
| `superlooper janitor` | propose GitHub-side cleanup, execute only what you approve | on your word |
| `superlooper resume` | re-enter an interrupted lane's conversation | launches a session |
| `superlooper debug` | launch one sl-debugger session because **you** asked | launches a session |
| `superlooper watchdog` | one mechanical health check (run on an interval, not by hand) | may launch a session |
| `superlooper morning-report` | render today's report now, instead of waiting for `report_time` | writes the report |
| `superlooper nightly` | the nightly QA run (usually launchd-scheduled) | may freeze merges |
| `superlooper promote-report` / `superlooper accept-failure` | Gate 2 evidence, and accepting a known failure | writes evidence / the ledger |
| `superlooper adopt` | wire a new repo into the loop | writes config + labels |
| `superlooper fleet` | judge (or build up) this machine's session host | only with `--install` |

---

## Start, watch, stop

```bash
superlooper status --repo /path/to/repo      # lanes / gate / freeze state, from journal + disk
```

(The bare `superlooper` command comes from publishing: `bin/install.sh` links it onto your PATH,
pointing at the installed copy. If your shell can't find it, re-run the installer — it prints the
exact PATH line to add. See `docs/ADOPTING.md` → "Getting the `superlooper` command".)

### Starting

**Under `login-item`** — nothing to type on an ordinary day. The LaunchAgent starts the runner at
login and restarts it whenever it exits. Setting it up once, or after moving the repo, is the
runner-home section below. To check it is actually up, `superlooper runner-home` prints the job's
own answer (a pid, "not running", or "not loaded") rather than a guess.

**Under `pane`** — `superlooper run` in a session tab you can watch. **That's it, no pane id to
set:** the runner detects the pane of the tab it is running in and records it as its **anchor**.
This survives a machine restart that reassigns pane UUIDs — you never hardcode a pane. It takes a
pidfile singleton, so a second `run` on the same repo refuses to start rather than double-drive it.

What the anchor is *for* has shrunk. Worker sessions are no longer born as sibling tabs in it:
every spawn now goes through the session host, which creates each session its own unfocused
workspace and needs no anchor at all. What still rides on it in this home is the runner's own boot
preflight, the `runner anchor (live)` doctor block, and the watchdog's resurrection path — which
births a replacement runner tab there. So a `pane`-home runner whose tab is closed or dragged
elsewhere is still a real fault, just a narrower one than it used to be.

```bash
superlooper run --repo /path/to/repo      # the pane home's tick loop (foreground; Ctrl-C to stop)
```

Under `pane` it **fails hard at startup** (never a quiet warning) if it cannot resolve a pane —
started outside a session tab, or detached so it lost the host socket. Under `login-item` the same
refusal exists but the faults are different ones: a non-Aqua session, launchd's bare PATH, a dead
`gh` login, or a session host that does not answer. Either way a runner that starts wrong aborts
every launch and burns every issue's retry cap while looking perfectly alive, so it refuses to
start instead.

### Watching

`superlooper status` renders the heartbeat age, any ALERT, whether merges are frozen, the occupied
lanes and what is at the gate, the merged count, and a tail of the journal — read from the journal
and disk, so it works whether or not the runner is up. It does **not** read GitHub, so it shows no
queue; the morning report (below) is where queue depth and what's next up live.

### Stopping

Stopping is always safe. The runner exits cleanly and **leaves in-flight sessions untouched** —
nothing merges while it is down, so no work is half-landed. Restarting rebuilds all state from
GitHub + disk (GitHub is the source of truth), so any manual daytime work you did is absorbed
automatically and no launch is duplicated.

- **Under `pane`:** Ctrl-C (SIGTERM) in the runner's tab.
- **Under `login-item`:** bootout the job, or launchd will simply restart it —
  `launchctl bootout gui/$UID/<job label>`. `superlooper runner-home` prints this repo's exact job
  label and domain; `superlooper runner-home --install` prints the ready-made bootout line. A plain
  `kill` is **not** a stop here: `KeepAlive` is doing its job and brings the runner straight back.

There is no single `stop` verb yet — a deliberate off switch that the runner's own guardians
(the watchdog's resurrection path, `KeepAlive`) respect rather than fight is filed as issue #239.
When it lands, it becomes the one answer to this question for both homes and replaces the two
procedures above. Until then, the kill-switch below is the part you must not skip.

**If you run the unattended-debugger watchdog** (below), `touch <state-home>/state/WATCHDOG_OFF`
**before** a deliberate stop and **delete it when you restart**. A stopped runner leaves its
heartbeat to go stale, which is exactly the fault the watchdog exists to catch — so without the
kill-switch it will text you and, after the grace, launch an sl-debugger session against a loop you
stopped on purpose. Worse under `login-item`: the watchdog's resurrection path will `kickstart` the
job and simply restart the runner you just stopped. The watchdog cannot tell a deliberate stop from
a crash; the kill-switch is how you tell it.

### Restarting the runner

`superlooper request-restart` is the home-independent *request*: it drops a marker in the state
home that a **live** runner honors at the safe point between ticks. It never launches or places
anything, and with no live runner it refuses and prints the manual remedy for **this repo's home**.

```bash
superlooper request-restart --repo /path/to/repo            # ask the live runner to restart itself
superlooper request-restart --repo /path/to/repo --check    # read-only preflight; writes nothing
```

What a restart *is* differs by home:

- **`login-item` — a clean exit the supervisor completes.** The loop stops, the runner releases its
  singleton and clears its home record, and `KeepAlive` starts a fresh process: the same engine
  reload and the same cleared episode state, without any in-place trickery. Both halves are
  journaled as `act: "runner_restart"` — `phase: "exit_to_supervisor"` by the departing runner and
  `phase: "up"` by its successor — because an exit followed by a *failed* restart otherwise reads
  exactly like a successful one, and that is the single question you will be asking. A departure
  with no matching `up` is a runner that did not come back. With no live runner, restart the job by
  hand: `launchctl kickstart -k gui/$UID/<job label>`.
- **`pane` — a re-exec in place.** The runner replaces its own process image, **preserving its
  pid**, because it has to stay the foreground process of its own tab; a new pid would orphan from
  the tab and the shell would fall back to its prompt. With no live runner, the remedy is manual:
  open a tab in the window you want the loop to live in and run `superlooper run --repo <path>`.
  The boot line prints the anchor it locked onto — **check that `window=` names the window you
  intended before you walk away**, because a runner started in the wrong window is visible right
  there, not hours later when every launch has parked.

**Why the `pane` home's restart is deliberately manual (history, and still correct there).**
Automated tab-placement was tried and it failed — two attempts each broke a *different* way the
same night (2026-07-09). A **focused-window fallback** targeted whatever window was *focused* when
it fired rather than the intended one, so the runner and every worker tab landed in the wrong
window. A **CLI-created workspace** produced tabs whose shells never started, so the launch shim
never ran and every worker launch was dropped silently. A human opening a real tab sidesteps both:
the tab *is* the caller, and its shell boots and sources the shim like any other. Under
`login-item` none of this applies — there is no tab to place.

### Which agent runs the sessions

Claude is the default and, by the owner's 2026-08-05 ruling (issue #352), **the only live target**:
Codex is not a lane the fleet runs today, so no Codex-specific readiness work is owed.

**Do not opt a repo into Codex expecting it to work.** The config still carries a `codex` block and
`superlooper run` still accepts `--agent codex`, but the delivery oracle — the thing that proves a
prompt actually arrived, because an exit code carries no delivery information — exists for Claude
only. A Codex lane therefore launches and can never be **nudged**: every send refuses with
`no_oracle` rather than pressing a prompt nobody could confirm arrived. That is the fail-closed
direction, but it means the session sits there with nothing in the queue explaining why. Treat
`--agent codex` as unbuilt until #352's posture changes.

---

## Tidying up finished session windows

A finished worker session does **not** close its own window — a real `claude` idles at its prompt
forever after writing its report (dry-run finding D4). The runner auto-closes a lane that
**successfully merged** (config `auto_close_merged_windows`, default on), but by default it never
auto-closes a parked / needs-owner / bounced window while its session is live: you must be able to
open that stalled work and look at it. So those pile up, and `superlooper tidy` closes them on
**your** say-so — a manual command, never wired into the runner, a schedule, or any automatic path
(closing a window is your word, like `agent-ready`).

```bash
superlooper tidy --repo /path/to/repo                 # close MERGED sessions' windows (asks y/N)
superlooper tidy --repo /path/to/repo --dry-run       # just list what it WOULD close
superlooper tidy --repo /path/to/repo --all           # also parked / needs-owner / bounced
superlooper tidy --repo /path/to/repo --yes           # skip the confirmation
```

- **What it will close:** by default, only the windows of **merged** sessions (truly done). `--all`
  extends that to the other terminal states — **parked**, **needs-owner**, **bounced**. It lists
  every window (issue id, status, surface) and asks `y/N` before closing; `--dry-run` prints the
  list and closes nothing; `--yes` skips the prompt. It closes the window only — it never prunes a
  worktree.
- **What it can never close:** anything still **in flight** ({running, frozen, exited}) or
  **mid-gate** ({gating, holding}) — those are excluded mechanically, so **tidy is safe to run
  while the runner is live**. It also skips any session with no recorded window.
- **How it closes:** through the same session-host wrapper the runner uses, which **verifies the
  teardown or reports it refused** — a close is never assumed from an exit code. It re-reads the
  session's recorded handle immediately before closing, so a relaunch that happens while you read
  the list can never redirect a close onto a fresh, live window. For a **merged** session it then
  clears the pane markers and singleton lock (safe — merged work never relaunches). For a
  **re-approvable** session (`--all`'s parked / needs-owner / bounced) it closes the window but
  **leaves the state markers to the runner**, which frees the stale lock and rewrites the marker on
  its own relaunch path. (Cost: a repeat `--all` may re-list an already-closed re-approvable
  window; closing it again is a harmless no-op.)

---

## Reviving an interrupted lane

`superlooper resume` re-enters an interrupted session's **conversation** instead of restarting it
cold from zero. The launch stack mints each flight's session id at spawn and records it in lane
state; this verb spends that record, handing the id back to the same launch stack so the session is
resumed rather than started fresh. It goes *through* the launcher, not around it, so the worktree,
the folder pre-trust, the delivery verification and the worker singleton stay one contract.

```bash
superlooper resume i123 --repo /path/to/repo             # revive issue 123's worker
superlooper resume d4  --repo /path/to/repo              # ...or a debugger session
superlooper resume i123 --repo /path/to/repo --check     # can it be resumed? writes nothing
superlooper resume i123 --repo /path/to/repo --note "…"  # a new instruction, placed AFTER the re-orientation
```

It is deliberately owner-driven: a person decides a lane is worth reviving and types this. A
revived session remembers the *conversation*, not the *world*, so the brief opens with a
re-orientation preamble that re-reads the branch, the head, the dirty count, the PR and the lane's
recorded status **now** — each degrading to a named unknown rather than a guess if it cannot be
read. `--note` lands after that preamble on purpose, so the session re-reads the world before
acting on your instruction. A successful launch means **delivery was verified**, not that the
conversation was re-entered — watch the window to confirm.

---

## The janitor: GitHub-side debris, propose-and-approve

As the loop runs, debris accumulates **on GitHub** that no other mechanism owns, and the tracker
itself drifts. `superlooper janitor` is tidy's discipline pointed at GitHub: it **proposes** a
one-touch list, each item with a one-line why, and executes **only what you approve** — the y/N (or
`--yes`) is your word, like `agent-ready`. Nothing is ever auto-closed, auto-deleted or
**auto-reopened**; there is no schedule wiring for the execute path and none may ever be added.

```bash
superlooper janitor --repo /path/to/repo                  # propose, then ask y/N
superlooper janitor --repo /path/to/repo --dry-run        # just list; changes NOTHING anywhere
superlooper janitor --repo /path/to/repo --yes            # skip the confirmation (still your word)
superlooper janitor --repo /path/to/repo --retry-refused  # re-propose previously failed actions
```

**The five classes it proposes:**

1. **Delete a remote `sl/*` branch** whose PR **merged**, or whose PR is **closed and labeled
   `superseded`** — never a branch with no PR, an open PR, or a closed-unmerged PR without the
   label, and only when the branch's current tip is still the PR's last-known head (commits pushed
   after the merge/close keep the branch off the list — an unmerged branch's work is never proposed
   for deletion).
2. **Close an open PR labeled `superseded`.** The branch stays; it becomes deletable on a *later*
   sweep, once its PR is closed.
3. **Close a parked / needs-owner issue** with no activity for `janitor.aged_park_days` (config,
   default 14). Age is proven, never guessed: an issue with an unparseable timestamp is skipped.
4. **Reopen an accidentally-closed issue** — one closed as COMPLETED by a bare commit-message
   keyword with nothing shipped behind it. This is not debris but a **lie in the tracker**: a
   regression vector recorded as fixed. A reopen is proposed only when the accidental close is
   PROVEN — never an owner-closed issue, never one closed by its own merged PR, never one whose
   closer could not be read. Executing it posts an **audit comment naming the closing commit**, so
   the reopened issue explains itself to whoever finds it next.
5. **Repair a mechanically-invalid issue's metadata** — one the runner can never launch for want of
   a `type:` label or a parseable `## Loop metadata`. The janitor **never invents a value**: which
   kind an issue is and which territory it touches are judgment calls, and there is no standing LLM
   seat to make them. It offers the closed set of values the repo itself declares as
   mutually-exclusive alternatives and **your tap picks one**. A bulk `y/N` never executes a grouped
   alternative (it would apply all three `type:` labels at once); only an explicit per-key tap does.
   The one ungrouped fix is the case where nothing is being chosen — the author already wrote the
   `touches:` value and only the heading above it is missing.

**So an approved sweep does change issue state** — it closes, reopens, adds a label and rewrites a
body — and every executed fix leaves an audit comment where a comment is possible. What is never
touched without your tap is anything at all.

**The reopen class is capped at 10 per sweep**, and it is the only capped class. A blanket approval
that fired hundreds of reopens — each posting a comment and re-releasing an issue into the queue —
is not a decision anyone would consciously make from one `y/N`. The cap keeps the most recently
closed by `closedAt`, and the remainder is **reported, never dropped** — the sweep prints a line
naming exactly what it withheld:

```
(3 more keyword-closed issue(s) were found and NOT proposed this sweep — at most 10 reopens per sweep; `superlooper doctor` lists them all and a later sweep proposes the rest)
```

A cap that is not *said* reads as "there was nothing else" — the same over-claim the
accidental-close audit exists to stop. Previously-refused reopens are reported rather than
re-proposed, and they never occupy one of the ten slots.

**What it can never propose:** anything in-flight or mid-gate ({running, frozen, exited, gating,
holding}) — excluded mechanically by the issue number in the branch name AND by the
loopstate-recorded branch. If `state/issues.json` is unreadable, the janitor refuses to propose
anything at all (nothing is provably idle). Every wrong-typed input fails **closed**.

**How it executes:** after your y/N it re-fetches and re-derives, executing only items that are
*still* eligible — a re-approval that happened while you read the list can never get its branch
deleted. Every approved action is journaled (`act: janitor`); a refused/failed action surfaces
once (loud FAIL line, nonzero exit, `state/janitor_refused.json`) and is held back from future
sweeps — never silently retried — until `--retry-refused`.

---

## The weekly once-over

```bash
superlooper upkeep --repo /path/to/repo
```

One batched touch that replaces a handful of remembered chores: the machine stack, how far the
installed engine has drifted from main, what the janitor would propose, whether the ops docs still
name live things, branch and worktree sprawl, whether the notify channel actually delivered
anything this week, and the loop's own question and park rates. It is **read-only by construction**
— it has no flags at all, deliberately — and every finding ends in a line naming the exact existing
command that fixes it. It reads *this page*, among others: a verb, label, doctor block or repo path
named here that the running system no longer has shows up as a docs finding.

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
- **Owner questions** — durable questions workers handed you overnight (see "Answering a question").
- **Conflict regenerations this week** — the tuning metric: if this climbs, tighten `affinity` or
  reduce `lanes`; if it's always zero, you can loosen. This is how you turn the parallelism dial.
- **Wanders** — PRs whose actual diff touched areas the issue didn't declare in `touches:`.
- **Unattended debugger** — every watchdog-launched sl-debugger session, verified or failed.
  **Owner-tapped sessions are deliberately absent from this section** — see `superlooper debug`.
- **Runner resurrection** — every automatic restart of a provably-gone runner.
- **Gate health** — nightly pass rate, flake count, quarantine size.
- **Freeze state, usage, queue depth + next up.**

A quiet night renders "nothing happened, queue empty" honestly — no news is real news.

Routine owner-decision pages (a park, a bounce, a durable question) are **batched here instead of
pushed** during `notify.quiet_hours` (default 21:00–08:00): nobody answers a 3am page and a park is
a safe state. Systemic-stop ALERTs and the merge-freeze notice always push.

---

## Label semantics (what each state means and what it asks of you)

The runner drives these; a few ask for a decision from you.

| Label | Meaning | Your action |
|---|---|---|
| `in-progress` | a worker is building it now | none — watch if you like |
| `parked` | the build **failed its retry cap** (relaunched, still not done); handed back with a memo | when you have time: read the memo, re-scope or re-approve, or drop it |
| `needs-owner` | an **owner decision is required** — a bounce, a **conflict-cap** hit, a fail-closed gate, or a third question on one issue (renamed from `needs-william`; `adopt` migrates the old label in place and the runner recognizes both) | decide: see "Answering a bounce" / "a parked conflict" below |
| `awaiting-answer` | a worker asked you a **durable question** and exited; the lane is free and nothing is building | answer in a comment, then re-apply `agent-ready` — see "Answering a question" |
| `expedite` | **jump the queue** — slotted into the very next free lane ahead of everything | apply it to an issue you want built next |
| `preserve` (on a PR) | on a conflict, resolve **in the PR's own branch** instead of regenerating from scratch | apply it to a PR whose diff is expensive to rebuild |
| `rebuild` (on an issue) | re-approving a finished lane **resumes at the gate** by default (keeps the PR, report and worktree); this is your separately-named choice to **discard** that work and build anew | apply it when the existing PR is not worth salvaging |
| `model:<name>` (on an issue) | run **this issue's** worker sessions on `<name>` instead of the config default | apply it to an issue you want built on a specific model |
| `effort:<level>` (on an issue) | run **this issue's** worker sessions at reasoning effort `<level>` (nothing sent when absent) | apply it to a gnarly issue that needs more, or a trivial one that needs less |
| `superseded` (on a PR) | the loop replaced this PR with a rebuild on current dev; branch kept, PR left open, nothing auto-closed | none — housekeeping only |
| `auto-approved:nightly-red` | a fix issue the nightly filed to restore a red mainline; entered by your standing rule, not by hand | none — it builds automatically; the distinct label is just the audit trail |
| `pre-authorized:referee` (on an issue) | **your word, granted early**: the gate may *merge* this issue's touches to `.superlooper/**` / `.github/workflows/**` instead of parking them for you at the finish line; also lets the launch gate start such an issue unattended | apply it **by hand, at approval**, to an issue whose referee reach you have read and accept. Without it, any referee-path diff still parks for you — unchanged |

**`parked` vs `needs-owner`** is the distinction that matters: `parked` is *mechanical
exhaustion* (retries ran out — no decision pending, look when convenient); `needs-owner` is *a
specific decision only you can make* (look sooner). Both always carry a memo comment.

> **Known gap — `awaiting-answer` is not in the vocabulary `adopt` creates (issue #337).** The
> runner writes it, but it is not in the engine's label set, and `gh` refuses to apply a label that
> does not exist. On a repo where nobody created it by hand, the question flow's label move fails
> and retries **silently** every tick, leaving the issue looking `in-progress` forever — this froze
> one lane for ~9 hours on 2026-08-04. Until #337 lands, `gh label create awaiting-answer` once per
> repo is the workaround, and a lane stuck `in-progress` with a posted question is the symptom.

### Per-issue model / effort (control knobs)

`model:<name>` and `effort:<level>` are **your** knobs, like `expedite`/`preserve`: apply or remove
them any time — they never touch the frozen issue text, and issue-writers don't set them. They
change which model / reasoning effort **that issue's worker sessions** run on (first launch,
crash-relaunch, and a regenerated rebuild all inherit them — the override rides the label, not the
first launch). The **sl-debugger** seat is never affected; it stays on `models.debugger` from
config.

- **Precedence (model):** issue `model:*` label → `models.worker` in config → the built-in default.
  **Effort:** issue `effort:*` label → `models.worker_effort` in config (a repo-wide default; `null`
  by default) → nothing sent. So with no label and no config default, no `--effort` flag is passed.
- **Exactly one each.** Two `model:*` (or two `effort:*`) labels on one issue is ambiguous, so the
  runner **refuses to launch it** until you fix the labels — exactly like a missing/duplicate
  `type:` label. It waits; it never guesses.
- **No allowlist.** The value is passed straight to the agent. `adopt` seeds a starter set
  (`model:opus`, `model:opus[1m]`, `model:fable`, `model:sonnet`, `effort:low…max`), but any
  `model:<x>`/`effort:<x>` label you create and apply works. An **unknown** value fails the launch
  loudly and the retry cap parks the issue — so a typo surfaces as a parked issue with a memo, not
  a silent wrong-model run.

### Answering a question

A worker that needs a decision from you does **not** sit at its prompt waiting (a waiting session is
a session that dies, and there is no second session hired to answer it — that seat is retired). It
writes its question to a file, **pushes its work-in-progress**, and **ends its turn**. The runner
then posts the question as a **durable GitHub comment**, closes the window, and **releases the lane**
— the issue moves to `awaiting-answer` and occupies nothing while it waits. The worktree is
preserved, and the branch is on origin either way, so no live window is the only copy of anything.

Your side is one touch: **answer in a comment, then re-apply `agent-ready`.** That relaunches a
fresh session whose brief embeds the question and your answer verbatim, reusing the pushed WIP (or,
if it no longer applies cleanly, letting the mechanical conflict ladder rebuild it with the Q&A
carried forward). Only an answer posted **after** the question is ever read, so a previous round's
answer is never reused.

**At most two questions per issue.** A worker that would ask a third is no longer scoping — the
runner hands the issue back as `needs-owner` with the question quoted, rather than opening a third
round-trip. Answering does **not** reset the count: the cap spans a whole answer-and-relaunch cycle,
so a second question still costs the second slot. Re-approving a **parked** issue is a different
act and *is* a fresh cap — `agent-ready` on a park-family issue zeroes `questions_asked` along with
the retry and conflict counters, which is what lets a question-capped issue be re-scoped and tried
again.

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
`notify.cmd`, then the session host's own notify channel, then log-only. It fires on every
transition to `parked` or `needs-owner`, every freeze, and every ALERT — the standing rule that
long-running work finishing, stalling, or needing input reaches you (spec §2). A send failure is
journaled, never fatal; notifications are a convenience layer, never a safety layer.

**One-time setup:** the first time it texts you, macOS asks permission to let the terminal control
Messages — click **Allow** once. **Every launchd-started job** — the nightly, the watchdog, and (in
the `login-item` home) the runner itself — needs that same permission granted to whatever user
launchd runs it under, so grant it once there too, or the first night's texts silently no-op
(they're journaled, so you'll see it in the log). This is one of the reasons every one of those
jobs is a `gui/$UID` **user LaunchAgent** and never a system daemon.

---

## launchd: which jobs exist, and which home they belong to

**The rule is per runner home, not absolute.** launchd runs the nightly and the watchdog for every
repo, and — only in the `login-item` home — the runner as well.

### `launchd.nightly.plist` (both homes)

`StartCalendarInterval` at `qa.nightly_time` (02:00 Mac-local), invoking
`superlooper nightly --repo <path>`. It needs no session anchor — it builds a fresh worktree and runs the browser suite,
and never opens worker windows. A nonzero exit is journaled + pushed, never restart-looped: it is a
scheduled one-shot, not a keep-alive.

### The runner: no launchd job under `pane`, a LaunchAgent under `login-item`

**Under `pane` there is deliberately no launchd runner, and that prohibition stands (issue #33).**
A launchd-started process is detached with no session tab of its own, so it can never self-detect
the anchor pane that home is built around — the pane its boot preflight demands, the one the
watchdog births a replacement tab into, and the one `runner anchor (live)` judges. Its startup
preflight correctly fails hard, and a `KeepAlive=true` would just relaunch it into the identical
failure forever, filling the log while nothing ever launches. There is no way to make launchd start
a `pane`-home runner *correctly*, because the only correct start is inside a tab a human opened and
automated tab-placement is owner-ruled out (above). Keep-alive of a `pane`-home runner is that
visible tab, which you can arrange to reopen on login. The shipped guard is narrower than "no plist
may keep a runner alive" — the runner template below *is* a keep-alive — it is that **the runner
template is the only plist allowed to invoke `run` at all**, so no second, undeclared launchd runner
can appear.

**Under `login-item` that reasoning does not apply, and there IS a runner job.** #33's argument was
a fact about *a host whose spawn needs an anchor*, not about launchd. The runner now talks to the
session host's server, which needs no anchor to spawn a session, so the prohibition dissolves — for
that home only. Set it up with:

```bash
superlooper runner-home --repo /path/to/repo --install          # render + place the LaunchAgent, print the activate command
superlooper runner-home --repo /path/to/repo --install --load   # ...and bootstrap it now
superlooper runner-home --repo /path/to/repo --verify           # run the boot preflight and exit
```

`--install` refuses outright on a `pane`-home repo — there is no job to install for a home that IS
a visible tab, and quietly rendering one would re-create exactly the impossible mode #33 deleted.
It records **where `gh` and `git` actually resolve on this machine** and bakes that into the job's
PATH; if it cannot resolve them it refuses rather than writing a dishonest PATH. `--verify` runs the
same preflight the runner runs at boot and nothing else — which is what makes it useful from
*inside* the job: bootstrap a one-shot that runs it and you have read the context the runner will
actually get, keychain and PATH included, rather than the one your terminal has.

Three things can silently be wrong in this home, and each is refused at boot rather than explained
in a document — the job must be in the **`gui/$UID` domain** (a system daemon is a different
session with no login keychain, which is where the "intermittent `gh` auth-death under launchd"
reports live), its **PATH must be explicit**, and the **session host's server must answer**. The
measurements behind all three, and the disposition of every verb that changed with the home, are in
`skills/superlooper/docs/RUNNER-HOME.md`.

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

The shipped implementation of that contract, plus a third detector.
`superlooper watchdog --repo <path>` is ONE mechanical check — no LLM anywhere on the path, no repair decisions: it detects,
notifies, waits, launches, journals. Its attended sibling — the one **you** tap — is the
`superlooper debug` section directly below; the two share a lock, an id namespace and a singleton,
so read both.

**Installing it.** Wire it to fire every few minutes by loading `templates/launchd.watchdog.plist`
as a user **LaunchAgent** — a check needs no session anchor, so launchd is fine here; the #33
prohibition above is about the *runner*, and only in the `pane` home. The template's placeholders
are substituted literally, and a hand-install must fill in every one: `{label}`,
`{superlooper_bin}`, `{repo_path}`, `{state_home}`, `{interval_seconds}` and — the one that is
easy to skip and expensive to get wrong — **`{path}`**.

> **`{path}` is load-bearing, not boilerplate (issue #328).** launchd hands a job only
> `/usr/bin:/bin:/usr/sbin:/sbin` — no Homebrew, no `~/.local/bin` — so a bare `gh` is simply not
> found. A watchdog without an explicit PATH still reads the heartbeat (that's a file) but **every
> GitHub read refuses**, which it correctly treats as UNOBSERVABLE and so freezes its clocks: the
> no-progress detector then **never fires, silently, on a job that looks perfectly healthy**.
> Substitute the absolute directories THIS machine resolves `gh` and `git` in, ahead of launchd's
> own four. Nothing judges this for you yet — `doctor --stack` checks the *runner's* job PATH but
> not the watchdog's, which is what #328 is open to close, so check it by hand after installing.

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
sl-debugger session launches through the same interactive launch stack workers use (never a
headless `claude -p`), its brief carrying the tripped signal and the standing `watchdog.authority`
tier; the session follows the sl-debugger skill's `references/unattended-contract.md`. If the
signal cleared meanwhile, it stands down SILENTLY (journal only). There is **no pane gate on this
launch any more**: the session host's server creates the session's workspace, so there is no anchor
to find and nothing to place — under a `login-item` runner none would resolve at all, and the old
gate would have made every unattended repair fail at exactly the moment repair is needed.

**Resurrection (issue #208).** A runner that is **provably gone** — heartbeat stale AND its
recorded pid dead — is a corpse to restart, not a patient to diagnose, so the watchdog restarts it
directly (a deterministic, zero-token act, never an LLM). *How* is the runner home: under
`login-item` it is `launchctl kickstart -k` on the job (the `-k` matters — the case includes a job
still loaded around a wedged process, which a plain kickstart would leave exactly where it was);
under `pane` it is a fresh tab in the dead runner's recorded anchor pane, verified by the pidfile
naming a live pid, and a gone or unresolvable pane means it cannot resurrect and notifies instead.
Restarts are capped per rolling hour (`watchdog.resurrection_max_per_hour`, default 5); hitting the
cap escalates loudly and pauses, because a repeatedly-dying runner is an incident, not a flap. A
restart that did not happen is journaled as FAILED, never as recovery, and every one appears in the
morning report's "Runner resurrection" section.

**Rails.** Singleton (a live `worker.d*.lock` blocks a second session, and concurrent checks
yield on `state/watchdog.lock`); once-per-incident (a continuing episode never relaunches — a
genuinely new episode after recovery may); failed launches retry at most 3× with ONE failure
text; every transition is journaled (`act: "watchdog"`) and every launch — verified or failed —
appears in the morning report's "Unattended debugger" section. **Kill-switch:**
`touch <state-home>/state/WATCHDOG_OFF` — the check keeps observing and journaling but notifies and
launches nothing; delete it to re-arm. Episode state lives in `state/watchdog.json`; deleting it
resets the clocks (safe — the bounds simply restart).

---

## The owner tap: `superlooper debug`

The watchdog's attended sibling. Same seat, same rails, different trigger: **a person asked for
it.** Use it when you can see something is wrong and do not want to wait out a grace window — or
when nothing has tripped at all but you want a session looking at the loop with you.

```bash
superlooper debug --repo /path/to/repo
superlooper debug --repo /path/to/repo --note "merges have been frozen since the 02:00 nightly"
superlooper debug --repo /path/to/repo --context-file board.txt   # '-' reads stdin
superlooper debug --repo /path/to/repo --check                    # read-only preflight; writes NOTHING
superlooper debug --repo /path/to/repo --json                     # one JSON object, for a UI over the loop
```

- `--note` is **your own words, carried into the session's brief verbatim** — rendered in a single
  pass so braces in your text are never expanded into something else. `--context-file` is for a
  caller that has a screenful of state to hand over (`-` reads stdin, so a large board readout can
  never hit an argv limit); an unreadable file is an error, because a launch that silently dropped
  the context it was handed would send the session in half-informed. Both are bounded, and a
  truncation is **stated in the brief** rather than silently swallowing the end of an instruction.
- `--operator` and `--source` are audit fields, named in the brief and in the journal line.

**The id namespace, the lock and the singleton are SHARED with the watchdog.** This is the whole
reason the verb exists in the engine rather than being re-implemented by whatever taps it:

- **Ids.** A tapped session takes the next `d<N>` from the same counter the watchdog uses
  (`state/watchdog.json` ▸ `next_debugger`) and writes the counter **forward before anything
  launches**, so a later watchdog launch can never reuse the number and overwrite the brief. It
  also allocates past any `d<N>` that already has a brief or a lock on disk, so an id handed out
  some other way still bars its number.
- **The lock.** The whole check-allocate-launch runs while holding `state/watchdog.lock` — the same
  lock a watchdog check holds across its entire run, launch included. So a tap and a watchdog check
  can never both pass the "is a debugger already running?" test and put two sessions on one patient.
- **The singleton.** "Already running" is read from the per-id `worker.d<N>.lock` files, the same
  ones the watchdog reads. A tapped session blocks the watchdog, and a watchdog session blocks a tap.

**What it refuses** (changing nothing, launching nothing — each comes back as a plain sentence, or
as the `--json` contract, never a traceback):

- **a debugger session is already running for this repo** — never two debuggers on one patient;
- **a watchdog check is running right now** — it does not queue behind the lock, it says so and
  changes nothing (a tap is cheap to repeat; a second debugger is not);
- **the state home, the id counter or the brief could not be written** — it refuses rather than
  launching on an id it cannot prove is its own;
- **the launch itself failed** — reported as the launcher's real verdict, never a pretend success,
  and the burned id is never handed out again.

**Why an owner-tapped session never shows up in the morning report's "Unattended debugger"
section.** It is journaled under a **distinct act — `debug_launch`** — while that report section is
built from `act: "watchdog"` records only. This is deliberate: that section answers "what happened
while nobody was watching", and a session you launched yourself is not that. Its brief says so too
— it asserts that a person is at the keyboard, so the sl-debugger skill's human-present contract
applies and its unattended contract does not.

---

## The doctor checklist

Run this until it is all-green before starting the runner on a repo; it changes nothing, only
reports:

```bash
superlooper doctor --repo /path/to/repo
```

It verifies: the config parses; `required_checks` is non-empty — a repo with no CI check enforcing
its own tests has no mechanical ship gate, so `doctor` **fails hard** here and `adopt` prints the
same requirement; `gh` is authenticated; the label set exists; the `dev_branch` actually exists on
origin (if it doesn't, every launch dies at worktree creation); every `required_checks` name has
really been reported on the surface that needs it; the **queue lint** — an approved issue that can
never launch for want of a `type:` label or parseable `## Loop metadata` is a live wedge and FAILs,
anything unapproved is a WARN; the **accidental-close audit**, which names keyword-closed issues
the janitor would offer to reopen; `jq` is present; the launch shim is installed; and the Claude
activity hooks are registered in `~/.claude/settings.json`. Fix anything red, then start the runner.

> **One leftover check.** The repo-level doctor still tests for the retired multiplexer's binary,
> unconditionally, whichever runner home the repo is on — a `pane`-home leftover. It passes on any
> machine where that app is still installed, which is why it has gone unnoticed; on a machine built
> session-host-only it would **FAIL and take the whole doctor red** with nothing actually wrong.
> That is an engine fix, not an operator one — reported as a finding on issue #329.

**`--stack` checks the machine, and judges the runner's home.**

```bash
superlooper doctor --stack
```

This runs the machine-level checks (the coding-agent binary and login, `gh` auth and API headroom,
the notify channel — which sends one real test message — the launch shim, the session host's own
fence and state-report hook, the installed engine and ops docs) plus **exactly one of two
home-specific blocks**, and the other prints as a clean skip:

- **`runner anchor (live)`** — the `pane` home's block. When a runner is live it re-runs the
  read-only pane probe the startup preflight uses against the anchor that runner recorded at
  launch, scoped to the runner's own recorded workspace, so it gives the same answer whichever tab
  you run it from. It FAILs only when a live runner's recorded pane no longer resolves in the
  workspace it launched in — its tab was closed or dragged elsewhere, so every worker launch would
  be born in a dead pane and the queue would park. A `login-item` repo is skipped here: that home
  has no anchor by design.
- **`runner home`** — the `login-item` home's block, and the whole outside view of the job. Each of
  its failures is a way the runner can be running and wrong while every other block reads green:
  the LaunchAgent is not installed, or not loaded in `gui/$UID` (nothing is supervising the runner,
  so the next time it exits nothing brings it back); launchd's pid and the loop's own pidfile name
  **different live processes** (two runners, one about to be restarted out from under the other);
  or the job's recorded PATH no longer resolves `gh`/`git` — which FAILs whether or not the job is
  running right now, because it is a static property of the installed home that will bite on its
  next start. A job that reports itself loaded and **not running** is a WARN (that is simply true
  between a restart and the next boot); a job that reports neither a pid nor a recognised state is
  a FAIL, deliberately, because nothing can then say whether a runner is supervised at all. The fix
  for most of it is `superlooper runner-home --install --load`. A `pane` repo is skipped here.

Both are safe to run before starting and while the loop is up.

**On a machine that hosts the fleet**, `superlooper fleet` judges the session host itself — the
fence on its control socket, the pinned build, the config directory, the server LaunchAgent. With
no flags it writes nothing and exits nonzero if the machine is not ready; `--install --load`
configures and loads it. See `skills/superlooper/docs/FLEET.md` for the build-up.
