# The unattended-invocation contract (for the #66 watchdog)

This reference defines how sl-debugger behaves when it is launched by the **mechanical
watchdog** (issue #66) rather than by a person. In that mode there is no human in the
conversation: nobody's word is live, nobody can say "go." Everything below exists to make
that safe. (Human-present sessions ignore this file and follow the SKILL.md rails, where the
human's word in conversation is live.)

## How you know you are unattended

The watchdog delivers the invocation context: it launches one fresh interactive session (via
the same launch shim worker sessions use — never a headless `claude -p`) whose brief names
the tripped signal (stale heartbeat / present ALERT / no-progress) and states the standing
**authority tier** it read from config. If your brief carries that watchdog context, you are
unattended. If you are unsure whether a human is present, behave as if unattended — the
stricter mode is always safe.

## Authority tiers (read from config; the watchdog passes yours in)

`diagnose-only` | `allowlist` | `full` — **default `full`**.

- **`diagnose-only`** — the health readout and a diagnosis, nothing else. Zero mutation of
  any kind: no label, no comment, no file write inside the state home except the memo and
  journal records described below. Mutating engine verbs are off-limits even when they would
  be "obviously right."
- **`allowlist`** — diagnosis plus ONLY the reversible repair verbs the owner enumerated in
  the config allowlist, exactly as written there. Anything not listed is treated as
  `diagnose-only`. Never interpret the allowlist expansively; a verb with options is
  authorized only in the form listed.
- **`full`** — diagnosis plus any repair in the repair ladder (`repair-ladder.md`),
  including state surgery. The standing authority setting IS the pre-given go for state
  surgery (owner amendment to #64, 2026-07-11) — you do not wait for a human word that
  cannot come. Everything in "Absolute exclusions" below still stands.

## Absolute exclusions — at EVERY tier, including `full`

Even `full` excludes the constitution absolutely. Unattended, you never:

- apply `agent-ready` (or any approval-recording label) — approval is William's word alone;
- merge anything, close a PR, or **force-push** — no exceptions, no `--force-with-lease`;
- edit frozen issue text (an approved Goal/DoD is William-signed and immutable);
- touch `.superlooper/**` (the repo-side executable config — the referee's rulebook) or
  `.github/workflows/**` (CI is the gate; nothing may edit its own referee);
- kill a process by name or pattern — never `pkill -f`, never `killall`; PID-specific
  only, and only a PID you positively identified as part of the broken instance (the
  2026-07-07 collateral kill of the owner's live dashboard is the standing lesson);
- run the owner's-word-only verbs: `superlooper tidy` (execute), `superlooper janitor`
  (execute), window closing of any kind. Their `--dry-run` forms are read-only and fine.
  Closing a window is the owner's word, like `agent-ready` — a standing authority tier is
  not that word.
- restart the runner. The only correct start is a human opening a cmux tab and running
  `superlooper run` (automated tab placement was tried twice and failed both ways,
  2026-07-09 — it stays out). If the diagnosis is "the runner needs a restart," that is a
  finding for the memo and the notify, not an action.

## Episode discipline

- **Act once per incident.** One diagnosis pass, at most one repair attempt per finding,
  then write the memo and end the session. Never loop, never re-trigger yourself, never
  schedule anything. (The watchdog enforces its own singleton and once-per-incident guard;
  this contract binds you to the same shape from the inside.)
- If a repair attempt fails, do not escalate to a more invasive one. Record what you tried,
  what happened, and what you would try next — in the memo, for a human.
- **Journal every action.** Each mutating step appends one bounded JSON line to the state
  home's `journal.jsonl` — `{"act": "sl-debugger", "step": <what>, "target": <path/issue>,
  "outcome": <ok|fail>}` (the engine's `journal.append` stamps `ts` itself; a hand-append
  must include its own epoch `ts`). One line per action, written before you rely on the
  action having worked. If the journal itself is the patient (unwritable, corrupt), record
  the same lines in the memo instead and say so.

## How the session ends — memo + notify, always

Every unattended run ends the same way, whether it repaired something, found nothing, or
hit its authority ceiling:

1. **A plain-language memo** at `<state-home>/reports/sl-debugger-<YYYY-MM-DD-HHMM>.md`:
   what tripped the watchdog, what the readout showed, what you concluded, what you did
   (each action with its journal line), and what you deliberately did NOT do (with the tier
   or exclusion that stopped you). Write it for the owner reading it over coffee — no
   jargon, no codenames. The filename shape is deliberate: only `i<N>.md` names in
   `reports/` are event-bearing to the runner (`morning-*.md` is the precedent for
   non-session files living there safely).
2. **A notify** through the instance's own channel (the engine's precedence: config
   `notify.imessage_to`, then `notify.cmd`, then `cmux notify`, then log-only): one line
   naming the tripped signal, the outcome, and the memo path. A send failure is journaled,
   never fatal.

The memo is the deliverable. A repair that isn't in the memo didn't happen; a finding that
isn't in the memo is lost — the owner was not watching.
