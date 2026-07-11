# The repair ladder — safest first, and most repairs are rung 1

Repairs go in strict order: **read-only forensics → reversible steps → state surgery.**
You never skip a rung, and you stop at the lowest rung that resolves the symptom. Two
design facts do most of the repair work for you:

- **The engine self-heals more than you think.** ALERT clears itself on a clean tick; a
  dev-check freeze unfreezes itself on green; stale pid locks (`runner.lock`,
  `worker.<id>.lock`) are stolen/freed automatically when the holder is dead; a parked
  investigation with a visible marker reconciles itself; a wedged tick recovers the moment
  the rock is removed — no restart. Before repairing anything, ask what the next clean tick
  would do about it on its own.
- **GitHub is the source of truth; a restart rebuilds from GitHub + disk.** Nothing merges
  while the runner is down, so a stop is always safe, manual daytime work is absorbed, and
  no launch is duplicated. The heaviest reliable repair is usually "stop, fix the one
  thing, start" — not editing state.

Prefer the engine's own mechanical verbs over hand-editing, always: `superlooper status`,
`doctor`, `doctor --stack` (read-only, minus one test notify), `tidy --dry-run`,
`janitor --dry-run` for looking; the runner's own reconciliation, re-approval flow, and
unfreeze logic for fixing. Hand-edits compete with a 15-second tick loop; verbs don't.

---

## Rung 1 — read-only forensics

The whole of `health-readout.md`, plus the incident-class matching in
`failure-classes.md`. Costs nothing, risks nothing, and in the documented incidents was
most of the resolution (class 2 needed literally nothing else). Output of this rung: a
diagnosis naming the mechanism, or an honest "unknown — here is what was ruled out."

## Rung 2 — reversible steps

Each of these can be undone or has no destructive reach. In a human-present session,
narrate what you're about to do; the human's word in conversation is live authority for
this rung.

- **Remove a foreign object from a scanned directory** (the class-1 repair): move — never
  delete — the binary/unexpected file out of `<home>/reports/` (a subdirectory like
  `reports/screenshots/` is safe). Recovery is next-tick, no restart.
- **Wait one tick / one poll.** Gate verdicts, ALERT clears, label reconciliation, and
  investigation self-reconciliation all ride the 15s tick and ~90s GitHub poll. A repair
  you can't distinguish from "it was about to fix itself" teaches you nothing.
- **Restart the runner** — human-present only, and the human does it: Ctrl-C is always
  safe (nothing merges while down; in-flight sessions untouched). The restart is the owner
  opening a cmux tab and running `superlooper run --repo <repo>`; the boot line prints the
  anchor — **check `window=` names the intended window** before walking away. Automated
  tab placement is ruled out (two different failures, 2026-07-09) — never script this,
  never suggest scripting it.
- **Re-approval of a parked issue is the owner's word**, never yours: your output is the
  memo that makes his decision one touch (what parked it, what changed, why relaunch would
  now succeed). He re-labels `agent-ready` (or taps approve on the dashboard — same word,
  audited); the runner then resets counters and relaunches on its own.
- **Window/GitHub debris**: `tidy` and `janitor` execute only on the owner's y/N — run
  their `--dry-run` yourself and hand him the list. Never wire either into anything
  automatic.
- **Machine-level fixes outside the loop's state**: the notify channel (config
  `notify.imessage_to`/`notify.cmd`, macOS Messages automation permission — `doctor
  --stack` proves the channel live), Python TLS roots for the usage meter
  (`Install Certificates.command` — a proven dark-meter cause), `gh auth login`. All
  reversible, none touch loop state.
- **Publish drift repair is owner-gated by design**: the fix is republishing via the
  engine's `bin/install.sh` (it shows the diff and asks for an explicit OK) and the owner
  restarting the live runner. You surface the drift (`VERSION` vs main) and hand over; you
  don't run the installer for him.
- **Do NOT hand-unfreeze merges.** `merges_frozen.json` is the engine honestly containing
  a red mainline; frozen-but-building is the designed safe idle. The runner unfreezes on
  green (dev-check source) or a green nightly (nightly source). Deleting the marker by
  hand is rung-3 surgery with a real blast radius (merges resume onto red) and is almost
  always the wrong move.

## Rung 3 — state surgery (owner-confirmed, journaled, runner stopped)

Direct edits to the state home. Only when a mechanical verb can't get there, only on the
human's **explicit go** in conversation for the specific edit (unattended: only at
authority `full`, per `unattended-contract.md` — the standing setting is the pre-given
go), and every step journaled.

The protocol, every time:

1. **Stop the runner first** (the human's Ctrl-C) for anything the tick reads or writes —
   you cannot out-edit a 15-second loop. Nothing merges while it's down. (Unattended at
   authority `full`: the permitted stop is a PID-specific SIGTERM of the pid in
   `state/runner.lock`, positively verified — see `unattended-contract.md`; the instance
   then stays down until the owner restarts it.)
2. **Copy aside before touching**: `cp <file> <file>.pre-surgery-<date>` (or for the
   journal: filter into an archive kept beside it — append-only audit record, archive,
   never delete).
3. **Minimal edit.** The known-safe surgeries, roughly in order of how often they're
   actually warranted:
   - *Journal cleanup after a storm* (the class-1 owed cleanup): filter the offending
     records out into an archive file, write the filtered journal atomically, verify line
     counts before/after and that first/last surviving records are intact.
   - *Removing `state/ALERT` by hand* — only when the runner cannot run to clear it
     itself and the cause is confirmed gone.
   - *Deleting a truly orphaned marker* (`blocked/<id>`, `awaiting/<id>`, a stale
     `panes/<id>`) whose session you have positively confirmed dead by PID — remember the
     lock files free themselves; most "stale lock" diagnoses are wrong.
   - *A poisoned `issues.json`* — strongly prefer deleting nothing: stop the runner, move
     the file aside whole, restart, and let the rebuild-from-GitHub absorb reality. Editing
     individual entries by hand is a last resort.
4. **Verify** (parse the JSON, re-run `superlooper status`, count the lines) before the
   runner restarts.
5. **Journal what you did**: one bounded JSON line per action appended to
   `journal.jsonl` — `{"ts": <epoch>, "act": "sl-debugger", "step": ..., "target": ...,
   "outcome": ...}` — or in the memo if the journal itself was the patient.
6. **The human restarts the runner** (rung 2's restart procedure), and you watch the first
   ticks in the journal to confirm the story reads healthy again.

## What no rung ever touches

The constitution holds at every rung, with the human present or not: never apply
`agent-ready` (or any approval label — that's the owner's word); never force-push; never
merge or close PRs by hand; never edit frozen issue text; never modify `.superlooper/**`
(the referee's own rulebook) or `.github/workflows/**` (the referee itself); never kill a
process by name or pattern — never `pkill -f`, never `killall`; a PID you positively
identified, or nothing.
