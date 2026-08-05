# Operating the loop — the owner's guide

The map and the handful of commands an owner actually types, once a repo is adopted and
running. It assumes the always-on setup: `runner_home: "login-item"` and a Herdr session
host. (In the default `pane` home the runner is a normal program in a terminal tab you
opened — there, stopping is Ctrl-C and starting is `superlooper run`, and you can skip
the stopping-and-starting section below.) The deep reference for runner behavior is
[runner-ops.md](../plugin/skills/superlooper/references/runner-ops.md).

Throughout, `<owner>__<repo>` stands for your repo's slug — `acme__webshop` for
`github.com/acme/webshop`. Each adopted repo keeps its working files in one folder, the
**state home**: `~/.superlooper/<owner>__<repo>/`.

## The map — what is running

Three background jobs, started at login. macOS restarts the first two if they ever die;
the third fires on a timer:

| Job | What it is |
|---|---|
| `com.superlooper.session-host` | the Herdr server. Every build session lives inside it, one workspace per session. If it restarts, it brings its sessions back on its own — no window needs to be open. |
| `com.superlooper.runner.<owner>__<repo>` | the runner — the small, deterministic program that launches, gates, and merges work. No terminal, no window; what it prints goes to the state home's `logs/runner.log`. |
| `com.superlooper.watchdog.<owner>__<repo>` | the health check. If the loop goes quiet or raises an alarm, it texts you once, waits a grace period, then sends in one repair session. |

A fourth job, the nightly QA run, exists only once you wire up a nightly test suite.
There is no AI inside any of these three: judgment lives in the sessions they launch,
never in the machinery.

The dashboard is separate — clone it, run it, and it serves the airport at
`http://127.0.0.1:8611` (localhost only; nothing is ever reachable from outside your
machine). Its `bin/liftoff` command brings up — or checks — both the dashboard and the
runner in one step.

Sessions are named by their lane: `i<N>` for issue workers, `d<N>` for repair sessions.
Inside the state home you'll find `journal.jsonl` (a line for every decision the loop
made), `state/` (what's running right now), `reports/` (morning and promotion reports),
`worktrees/` (each session's working copy), and `logs/`. GitHub is the source of truth
and everything local can be rebuilt from it — which is why stopping the loop is always
safe.

## A normal day

Read the morning report (`reports/morning-YYYY-MM-DD.md`, written at 08:45 by default,
with one notification) or glance at the dashboard. Act on what asks for you, ignore the
rest:

- **`needs-owner`** — a decision only you can make (a bounce, a conflict, a question).
  Look sooner. Always comes with a short memo explaining itself.
- **`parked`** — the build ran out of retries. Nothing is waiting on a decision; look
  when convenient, then re-scope, re-approve, or drop it.

Everything else — building, reviewing, merging, freezing when the mainline goes red and
unfreezing when it's green again — is the machine's job. A loop that is frozen but still
building is in its designed safe state, not an emergency.

## Approving work (the one gate that is you)

Your word in conversation is the approval; the `agent-ready` label only records it — an
agent applies the label for you and leaves an audit comment (the full protocol is in
[approval-protocol.md](../plugin/skills/superlooper/references/approval-protocol.md)).
You steer with labels, never by editing an approved issue's text:

- `model:<name>` / `effort:<level>` — run this issue's sessions on that model, at that
  effort.
- `expedite` — jump the queue: the next free lane takes this first.
- `preserve` (on a PR) — fix a conflict in the PR's own branch instead of rebuilding it.
- `pre-authorized:referee` — your yes, granted early, for a change you already know will
  touch the loop's own rulebook (`.superlooper/**` or `.github/workflows/**`).

Never approve an issue you haven't read: the label is also your safety filter.

## The skills — how you talk to the loop

The plugin installs five skills into Claude Code. Type the slash command in any Claude
session; each one loads the playbook for that job:

- **`/superlooper:write-issue`** — use it in a planning conversation. It turns what you
  talked about into loop-ready issues: small, self-contained, carrying every label except
  `agent-ready` — that last one is yours.
- **`/superlooper:superlooper`** — the workflow itself. Use it when you're working the
  loop with an agent: approving issues (it applies your word as labels, with audit
  comments), reading the morning report, deciding what to do with parked work.
- **`/superlooper:adopt`** — use it once per repo: it walks the agent through wiring a
  new repo into the loop, end to end.
- **`/superlooper:cross-review`** — a second opinion on code from a reviewer that wrote
  none of it. The loop runs this on its own during builds; you can also invoke it
  yourself on any diff.
- **`/superlooper:sl-debugger`** — use it when the loop itself looks off. It loads the
  diagnosis playbook: what to check, in what order, and what never to touch.

## Watching

- The dashboard is the glance: departures, the field, arrivals, and the Needs You cards.
- `superlooper status --repo <path>` — lanes, gate, freeze state, read from disk; works
  whether or not the runner is up.
- The actual windows: the install writes an attach script, `viewer.command`, next to the
  Herdr binary — double-click it for a window onto every running session. Watching is
  optional in the strongest sense: nothing depends on the window being open, and closing
  it kills no session.
- `tail -f <state home>/logs/runner.log` when you want the runner's own voice.

## Answering

- **A bounce** comes with a ready-to-approve amendment: yes = re-approve, and the amended
  text is what builds next; no = re-scope it in a planning chat, or drop it.
- **A parked conflict** names the issue it collided with: re-scope one of the two, or put
  `preserve` on the PR that's too expensive to rebuild.
- **A question**: it arrives as a comment on the issue, and the lane is already free —
  the worker stepped aside rather than guessing. Answer it (the dashboard's Answer button
  posts your answer and re-approves in one touch); a fresh session picks it up from there.

## Stopping and starting

To stop the loop on purpose, two commands:

```sh
touch <state home>/state/WATCHDOG_OFF     # tell the watchdog the stop is deliberate
launchctl bootout gui/$UID/com.superlooper.runner.<owner>__<repo>
```

There's no Ctrl-C in this home (the runner has no terminal), and just killing the process
is not a stop — macOS restarts a dead runner, and the watchdog revives one that's gone.
The `WATCHDOG_OFF` file is how you tell them you meant it. A stop is always safe: nothing
merges while the runner is down, running sessions keep living in the session host
untouched, and a restart rebuilds everything from GitHub and disk.

To start again:

```sh
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.superlooper.runner.<owner>__<repo>.plist
rm <state home>/state/WATCHDOG_OFF
```

Leave the session host itself running as a rule — sessions live inside it, and it needs
no nightly rest.

## Restarting and recovering

- **Restart the runner** (picks up a newly published engine, clears its short-term
  memory): the dashboard's Restart button, or
  `superlooper request-restart --repo <path>`. The runner finishes what it's doing, exits
  cleanly, and macOS brings it straight back. For one that's truly wedged:
  `launchctl kickstart -k gui/$UID/com.superlooper.runner.<owner>__<repo>`.
- **The watchdog** covers the 3am case on its own: one text naming what tripped, a grace
  window, then one repair session working under its unattended rules.
- **You cover the daytime case**: the dashboard's Deploy Fixer button, or
  `superlooper debug --repo <path> --note "what you're seeing"`.
- **Revive an interrupted session** instead of restarting the work cold:
  `superlooper resume i<N>` (or `d<N>`) re-enters the same conversation, opening on a
  recap of the current state of the world so it re-reads before it acts.
- **Health checks**, after any change to the machine or whenever something looks off:
  `superlooper doctor --repo <path>` checks the repo, `superlooper doctor --stack` checks
  the machine, `superlooper fleet` checks the session host, and `superlooper upkeep` is
  the weekly read-only once-over. Each names what's wrong and the exact fix.

## Housekeeping

- `superlooper tidy` — closes the session windows of finished builds, after showing you
  the list and asking. It never touches anything still running.
- `superlooper janitor` — cleans up GitHub-side debris (stale branches, superseded PRs,
  long-parked issues): it proposes a list with reasons and executes only what you
  approve.
- **Publishing the engine**: a merged engine change does nothing until you re-run
  `./bin/install.sh` from your source checkout — it shows you exactly what changed since
  your last publish and asks before touching anything. The dashboard and
  `doctor --stack` both show when the installed engine is behind.

## Where everything lives

| Path | What |
|---|---|
| `~/.superlooper/<owner>__<repo>/` | the repo's state home: journal, state, reports, worktrees, logs |
| `~/.superlooper/fleet/` | the session-host build: the Herdr binary, its token, `viewer.command` |
| `~/Library/LaunchAgents/com.superlooper.*` | the background jobs |
| `~/.claude/skills/superlooper/` | the installed engine (the published copy the loop actually runs) |
| your source checkout | publish from here (`bin/install.sh`); build the session host from here |
