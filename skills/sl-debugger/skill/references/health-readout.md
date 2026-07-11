# The health readout — read-only forensics, always the first move

Every diagnosis starts here, whatever the symptom. Everything in this file is **read-only**:
nothing below mutates the loop, the state home, GitHub, or the dashboard. Run the whole
readout before touching anything — half the documented incidents self-healed or needed a
one-file nudge, and the readout is what tells the difference.

Substitutions: `<repo>` = the adopted checkout's path; `<home>` = its state home,
`~/.superlooper/<owner>__<name>/` (the `SL_HOME` env var overrides the `~/.superlooper`
base). The slug comes from `.superlooper/config.json`'s `repo` field. Numeric thresholds
below are dated to engine `a77801d` (2026-07-11) — the engine source is truth
(`skills/superlooper/skill/lib/`, `bin/runner.py`); verify before leaning on an exact value.

## 0. The engine's own probes first

```bash
superlooper status --repo <repo>    # heartbeat age, ALERT, freeze, lanes, gate, last 8 journal records
superlooper doctor --repo <repo>    # repo-level validation: config, required_checks, labels, dev branch, hooks
```

Both are read-only and work whether or not the runner is up (`status` renders from journal +
disk). `superlooper doctor --stack --repo <repo>` adds machine-level checks — claude/gh
auth, gh API headroom, launch shim, and the **live runner anchor probe** (catches a runner
whose cmux tab was closed/moved, which otherwise parks the whole queue) — but note its one
deliberate side effect: it sends a single real test message through the notify channel.
Fine when notify is the patient; skip it when the owner is asleep.

## 1. Process up vs progress being made — two different questions

- `<home>/state/runner.lock` — pidfile: **the process is up**. `ps -p $(cat .../runner.lock)`.
- `<home>/state/runner.heartbeat` — one epoch int, stamped at the **end of a successful
  tick**: **the loop is making progress**. Age = `now − heartbeat`. Tick cadence is 15s, so
  a healthy age is seconds; minutes is wrong.
- **Lock alive + heartbeat stale = alive-but-wedged** (the 2026-07-07 class — the stamp was
  deliberately moved to end-of-tick so a wedge reads stale, not healthy).
- `<home>/state/ALERT` — existence = an active alert; JSON `{"reasons": [...], "since": epoch}`.
  Reason codes (from `lib/actions.py` / `bin/runner.py`): `runner_tick_errors:<n>` (wedged
  tick loop), `gh_unreachable` (≥10 failed polls), `usage_stale` (usage meter dark),
  `launch_anchor_down` / `launch_systemic_failure` (launches failing as a class — queue held
  intact behind one alert), `launch_runaway:<id>`, `update_errors:<id>`,
  `park_label_stuck:<id>`. The runner clears the file itself on a clean tick.

## 2. What has the runner been doing — the journal

`<home>/journal.jsonl` — append-only JSON-lines, `ts` epoch-stamped by the writer, one
record per detected event and per executed action (with `outcome`). No rotation. Recipes:

```bash
tail -20 <home>/journal.jsonl
jq -r 'select(.act=="tick_error" or .act=="poll_error")' <home>/journal.jsonl | tail    # crashes
jq -r 'select(.act=="park" or .act=="alert" or .act=="notify")' <home>/journal.jsonl | tail
jq -r 'select(.id=="i42")' <home>/journal.jsonl                                        # one issue's story
```

Act names mirror the runner's executors: `launch`, `gate`, `merge`, `park`, `notify`,
`freeze`/`unfreeze`, `regenerate`, `reapprove`, `bounce`, `event`, `tick_error`,
`poll_error`, `alert`, `fail_open`/`usage_recovered`, `brief_comments`, `janitor`,
`nightly`. An issue's whole lifecycle reads as `launch → event(session_finished) → gate →
merge`; anything else in that story is your lead.

## 3. Lanes, queue, territory

- `<home>/state/issues.json` — durable per-issue state: `status`, `branch`, `lane`,
  `launches`, `retries`, `conflicts`, `declared_touches`, `pr`. Status vocabulary:
  in-flight = `running | blocked | frozen | exited`; mid-gate = `gating | holding` (no lane
  held, but **territory still claimed** until merge — issue #6); terminal = `merged |
  parked | needs_william | bounced`.

  ```bash
  jq -r '.issues | to_entries[] | "\(.key)\t\(.value.status)\t\(.value.branch // "-")"' <home>/state/issues.json
  ```

- Lane capacity comes from config `lanes`: an integer (one shared pool) or
  `{"build": N, "investigate": M}` (issue #63 — reserved pools, no borrowing; investigations
  open no PR and are exempt from territory in both directions).
- The queue lives on GitHub: `gh issue list --label agent-ready` is what the runner will
  consider; ordering is expedite → priority band → conflict-requeue → oldest. An eligible
  issue that never launches usually trips one of: usage fail-closed, no free lane in its
  pool, hard-affinity overlap with a running lane or a held (gating/holding) territory
  claim, an unclosed `blocked-by`, duplicate `model:*`/`effort:*` labels, missing `touches:`
  where required, or `launch_degraded` (see the ALERT codes).

## 4. Freeze state

`<home>/state/merges_frozen.json` — **existence = frozen** (a present-but-unreadable marker
still means frozen; fail-closed by design). `source` field says who owns it: `dev-check`
(runner froze on a red dev required check; it unfreezes itself when dev goes green) or
`nightly` (sticky; only a green nightly clears it). Frozen stops **merges only** — builds
and investigation-closes continue. **Frozen-but-building is the designed safe idle state**,
not an emergency to escape.

## 5. Per-session markers (one issue's session, under `<home>/state/`)

`activity/<id>` (mtime = liveness heartbeat, stamped by the Claude hooks; idle peek at
~480s stale, recovery ladder at ~2700s), `blocked/<id>` (the worker's plain-text question —
an answerer session gets hired), `exited/<id>` (process gone, `<epoch> rc=<code>`),
`awaiting/<id>` (suppresses the idle peek during long background work), `panes/<id>` +
`panes/<id>.ws` (which cmux surface the session lives in), `worker.<id>.lock` (per-worker
pid singleton), `started/<id>.*` (launch delivery proof). Siblings at the home root:
`briefs/<id>.md`, `reports/<id>.md` (existence = session finished), `answers/<id>.md`,
`worktrees/<id>/`, `logs/runner.log`.

## 6. The usage meter

Launches gate on the Claude usage meter, fail-closed: fresh read over the ceilings (90%
five-hour / 96% seven-day) launches nothing. One deliberate exception (issue #46): a meter
that is **unreadable** past a 30-minute grace fails **open** (journal records `fail_open` on
entry, `usage_recovered` on exit; ALERT carries `usage_stale` meanwhile). So: queue stalled
with `usage_stale` in ALERT = meter dark < grace or reads-exhausted; check
`journal.jsonl` for `fail_open` and the machine for the three proven dark-meter causes
(Keychain locked, missing TLS roots in a new Python — `Install Certificates.command` —, API
change).

## 7. The dashboard (command-center), if installed

Bound to `127.0.0.1` only (default port 8611); it refuses any other bind by construction.

```bash
curl -s http://127.0.0.1:8611/api/snapshot | jq '{generated_at, clock, pill, github: .github.reachable, runner: .runner.down, usage: .usage.known}'
```

Healthy: `generated_at` advances every poll, `pill.level` `"ok"`, `github.reachable` true,
`runner.down` false, `usage.known` true. `runner-down` on the pill means the loop's
heartbeat is absent/stale (>300s) — believe the heartbeat, not the dashboard's memory.
Known mispaint (issue #22 lineage): a finished session whose report the dashboard can't see
paints as a dead session (`session-frozen`) instead of `stranded` — check
`<home>/reports/i<N>.md` existence yourself before trusting a grey plane. GitHub-derived
facts ride a ~30s cache; local state-home facts are fresh every poll. **Never** probe or
manage it with `pkill -f`/`killall` — the 2026-07-07 collateral kill of the owner's live
dashboard is the standing lesson; find its PID via `lsof -i :8611` (launchd label
`com.command-center`, log at `~/Library/Logs/command-center.log`).

## 8. Publish drift

The loop runs the **installed** engine, not the repo: `~/.claude/skills/superlooper/VERSION`
(commit + date, stamped by the gated `bin/install.sh`) vs `git -C <engine-repo> log -1
--format='%h %as' main`. Merged-but-never-republished fixes are inert — when a "fixed"
incident recurs, check drift before assuming regression. Republish is owner-gated (the
installer shows the diff and asks), and a live runner picks it up only on the owner's
restart.

## 9. What healthy looks like (the one-screen checklist)

| Probe | Healthy |
|---|---|
| `runner.lock` pid | alive |
| `runner.heartbeat` age | seconds (≲ 2 ticks) |
| `state/ALERT` | absent |
| `merges_frozen.json` | absent (or present with a red dev/nightly it is honestly containing) |
| journal tail | events → gate → merge stories; no `tick_error`/`poll_error` runs |
| `issues.json` statuses | in-flight ≤ lane capacity; gating/holding turning over within minutes |
| `agent-ready` queue | draining when lanes free |
| dashboard `/api/snapshot` | `generated_at` advancing, pill ok, `runner.down` false |
| installed `VERSION` | matches the engine main you think is deployed |

Anything off: match the symptom against `failure-classes.md`; before mutating anything,
read `repair-ladder.md`.
