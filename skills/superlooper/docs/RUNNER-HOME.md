# The runner's own process home

Issue #306. Ruled 2026-07-31 (`docs/HERDR-ADOPTION-PLAN.md` §8.1): **the runner lives OUTSIDE the
session host** — a plain `gui/$UID` login-item process talking to the host's server. The supervisor
never lives inside what it supervises.

This page is the operational record of that: the two homes, what each one costs, and the explicit
disposition of every verb that was built around the old one. It is a design record, not a playbook;
`docs/STACK.md` carries the doctor blocks and `plugin/skills/superlooper/references/runner-ops.md`
carries the day-to-day ops.

## The two homes

Selected per repo by `runner_home` in `.superlooper/config.json`. Default `pane` — a config written
before this issue keeps today's behaviour exactly, which is what the cross-machine parallel run
(plan §6) rests on. `lib/runner_home.py` owns every decision that branches on it.

| | `pane` (default) | `login-item` |
|---|---|---|
| Where it runs | a visible multiplexer tab a person opened | a `gui/$UID` LaunchAgent |
| Launch anchor | that tab's pane; every worker session is born in it | none — the host's spawn needs no anchor |
| Started by | `superlooper run` typed in the tab | `RunAtLoad`, at login |
| Restarted by | `os.execv` in place (#116), or the watchdog birthing a fresh tab (#208) | exiting; `KeepAlive` brings it back. The watchdog uses `launchctl kickstart -k` |
| Boot preflight | the pane resolves (D7) | Aqua session, PATH, `gh` login, the session host answers |
| Doctor block | `runner anchor (live)` | `runner home` |

### Why the `login-item` home was impossible before, and is not now

Issue #33 deleted a `launchd.runner.plist` and wrote that "there is no way to make launchd start
the runner *correctly*." The reasoning was right and is worth keeping: a launchd-started runner is a
detached process with no tab, so it can never self-detect the pane every worker is born in; its
startup preflight correctly fails hard, and `KeepAlive` would relaunch it into the same failure
forever (finding D7).

That is a fact about **a host whose spawn needs an anchor**, not about launchd. Move the runner to a
host whose spawn does not, and the prohibition dissolves — for that home only. The `pane` home keeps
every one of #33's rules, and `tests/test_templates.py` still refuses any *other* plist that invokes
`run`.

### What replaces it: three things that can silently be wrong

Each is refused at boot rather than explained in a document, and each was measured
(`docs/SPIKES-2026-07-29-results.md`, test 3):

1. **The session must be `gui/$UID`.** A gui LaunchAgent reports `managername = Aqua`, the login
   keychain is its default keychain, and `gh` read its keyring token in one second with no prompt. A
   `system/` domain daemon is a *different* session and was never tested — the "intermittent gh
   auth-death under launchd" reports live there. So the gui domain is spelled once, in
   `runner_home.domain`, and **no function in that module takes a domain parameter, and no
   environment variable can choose the uid**: a rule that can be passed as an argument — or
   exported for debugging — is a rule that will one day point `bootstrap`, `bootout` and
   `kickstart` at another user's login session.
2. **PATH.** launchd hands a job `/usr/bin:/bin:/usr/sbin:/sbin` and nothing else — no Homebrew, no
   `~/.local/bin`. A launchd-hosted runner that shells `gh` by bare name simply does not find it,
   and fails every GitHub read while looking perfectly alive. So `superlooper runner-home --install`
   records where `gh` and `git` *actually* resolve on this machine and bakes that into the job, and
   the doctor re-checks it statically.
3. **The session host.** The pane home fails hard at boot when the pane will not resolve (D7 — a
   quiet warning there once let a mis-started runner abort every launch and burn every issue's retry
   cap). The login-item home's equivalent is the host's server not answering, and it is refused the
   same way. That answer is obtained **through the five-verb wrapper** (`session_host.state` on a
   reserved probe name that nothing ever spawns), so the runner's boot needs no second door to the
   host.

## Disposition of the verbs built around the old home

The issue asked for each to be explicitly ported, reshaped, or retired, with the reasoning. All
four, plus the two that turned out to be attached to them:

### `superlooper run` — **RESHAPED**

One verb, two boot paths. Under `pane` it is byte-for-byte what it was: detect the anchor, print it,
fail hard without a resolvable pane. Under `login-item` it detects nothing, takes no `--pane`, and
runs the preflight above instead. Asking the multiplexer to identify a tab this process is not in
would answer `""` and then fail a preflight that has nothing to do with this home's health.

### The #116 in-place `os.execv` re-exec — **RETIRED in the `login-item` home, KEPT in `pane`**

`os.execv` was never about reloading code; a fresh process does that too. It was about **preserving
the pid**, because the runner had to stay the foreground process of its own tab — a new pid would
orphan from the tab and the shell would fall back to its prompt. That is also why it needed the
`SL_RESTART_ADOPT` singleton-adoption token: the lock still held its own live pid across the exec.

With no tab, the pid is worth nothing. The honest restart is a clean exit: the loop stops, `run()`'s
`finally` releases the singleton and clears the home record, and `KeepAlive` starts a fresh process —
the same engine reload and the same cleared episode state, without the adoption dance. None of the
`SL_RESTART_ADOPT` machinery is reachable in this home.

Both halves are journaled — `exit_to_supervisor` by the departing runner, `up` by its successor —
because an exit followed by a *failed* restart otherwise reads exactly like a successful one, which
is the single question the morning report is asked about a restart. The re-exec path passes that
baton in an environment variable; a process that actually dies cannot, so this one leaves it on
disk (`state/runner.restarted`) and the successor spends it exactly once.

### The #116 Restart *request* (`superlooper request-restart`, the marker) — **PORTED unchanged**

The request half is home-independent: drop a marker in the state home, and the live runner honors it
at the safe point between ticks. Only the *execution* differs, which is `runner_home.restart_mechanism`.
Two things in the verb are now home-aware, because printing the wrong one is the D12 defect class
this engine has already paid for — the manual remedy it names with no live runner (`launchctl
kickstart` vs "open a tab"), and its description of what a restart will do.

### The #208 watchdog resurrection — **RESHAPED**

Under `pane`: a fresh tab in the dead runner's recorded anchor pane, via `resurrect-runner.sh`,
verified by the pidfile naming a live pid. Under `login-item`: `launchctl kickstart -k` on the job.
Far simpler — no tab to place, no keystroke delivery to work around — and `-k` is load-bearing,
because the case includes a job still loaded around a wedged process, which a plain kickstart would
leave exactly where it was. A restart that did not happen is journaled as FAILED, never as recovery.

Getting this branch wrong is the adoption plan's own named migration landmine (§9): a watchdog left
calling a launcher that cannot work means no unattended repair at the moment repair is needed.

### The watchdog's own home — **ALREADY OUTSIDE; unchanged, one fix**

Decided the same way and needed no move: it already runs as a `gui/$UID` LaunchAgent
(`templates/launchd.watchdog.plist`) — outside the session host by construction, inside the Aqua
session the keychain rule requires, and a scheduled one-shot rather than a keep-alive. The one thing
that *was* wrong is PATH: with launchd's four entries the watchdog still reads the heartbeat (a
file) but every GitHub read refuses, which it correctly treats as UNOBSERVABLE — so its no-progress
detector silently never fires on a job that looks perfectly healthy. Its template now carries an
explicit `{path}`.

### Liftoff (`dashboard/bin/liftoff`) — **RESHAPE OWED, filed, NOT done here**

Liftoff lives in the `dashboard` area and this issue declares only `touches: engine`, so it is out
of lane. What it needs is recorded rather than half-built: its idempotent probes are home-independent
and keep working (the port probe, and the runner pidfile + liveness check), but its *action* for the
runner half — "foreground `superlooper run` in this cmux tab" — is a pane-home fact. Under
`login-item` the equivalent is ensuring the job is bootstrapped and running. Filed as a follow-up.

The dashboard's Restart button carries the same drift in its own copy: its dialog says the runner
will "restart itself in its own cmux tab". Same follow-up.

## Where the login-item home is NOT yet operational

Stated plainly because a half-migrated home that *looks* complete is worse than one that announces
its gap. This issue moves the **supervisor's own process**. The launch path it drives —
`launch-session.sh`, which still resolves a pane and births a tab — belongs to the wrapper migration
and has not landed. So on a machine set to `login-item` today the runner boots, preflights, ticks,
restarts and is doctored correctly, and its *worker launches* still expect the old launcher. The
parallel-run plan sequences it that way on purpose: the pieces land first, the cutover is one
deliberate event, and production stays on `pane` throughout.
