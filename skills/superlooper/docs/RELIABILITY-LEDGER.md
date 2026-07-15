# Reliability ledger — every way the overnight promise has broken

**What this is.** One short entry per incident, in plain language, so anyone can answer in two
minutes: *how broken is this system, really, and in what ways?* Deep dives stay in their own
`INCIDENT-<date>-*.md` beside this file; this is the index and the pattern record. When a new
incident happens: add an entry here (date, which promise broke, mechanism in 1–3 sentences,
class, fix pointer) even if no full incident doc is warranted.

**The promise being measured.** The owner approves issues; the loop builds, reviews, gates, and
merges them unattended — overnight, while he sleeps — interrupting him only for decisions that
are genuinely his. An incident is any night or wave where that failed: work stalled, the owner
was interrupted for something mechanical, recovery needed his hands, or the system was loud/wrong
at him.

**Standing to date (2026-07-15).** The loop's best stretch is real: 33 issues approved→merged in
~28 hours with zero interventions (2026-07-11→12), and a second adopted repo at 88 lifetime
merges. Across every incident below: **no data loss, no bad merge, ever** — the failure mode is
always *stall + owner interrupt*, never corruption. But of the eight nights this ledger covers,
roughly half broke the promise somewhere. The owner's felt estimate — "works three out of four
times" — is about right, and the misses cluster in the classes below rather than being random.

---

## The failure classes

1. **Trusted-signal failures** — the system believed a signal instead of verifying it. The
   refused-GitHub-read-collapsing-to-"empty" family; completion inferred from a report file the
   agent must remember to write. Every major reliability jump so far came from converting one of
   these to a mechanical check (launch delivery sentinel, typed reads, hook liveness stamps).
2. **Ambient machine state** — the loop runs on a consumer Mac and silently inherits its
   environment: TLS roots, App Nap, macOS Automation permissions, machine-global agent-CLI
   config, per-window login state. Nothing pinned these or verified them at the moment of use.
3. **Publish/config drift** — merged fixes are inert until republished + restarted; per-repo
   migrations ride `adopt` and silently don't exist until it is re-run; the dashboard can serve
   new UI over a stale server. The system improves itself faster than its deployments follow.
4. **Recovery-path defects** — the happy path is verified, recovery is not: relaunching into a
   wedged window, nudging sessions a nudge cannot help, a breaker that holds correctly but can't
   re-arm itself. Recovery failures are the most expensive class because they arrive stacked on
   top of a first failure.
5. **Owner-interrupt design** — the loop is loud or confusing at the owner: notify storms (41
   texts one night, ~15 another), parks at the finish line for conditions knowable at approval,
   needs-input cards that don't explain their verbs.
6. **Worker instruction drift** — an agent doesn't follow its standing orders: finishes without
   writing the report, asks the owner in-session instead of using the answerer, kills processes
   by pattern. Softened by briefs and mechanical gates; never fully mechanizable.

---

## Ledger

**2026-07-07 — binary file in `reports/` wedges every runner tick** · *Classes 1, 5* ·
Three PNGs saved loose in `reports/` stalled the whole loop ~42 minutes, silently — the owner was
never notified. Full doc: `INCIDENT-2026-07-07-runner-binary-report-wedge.md`. Standing rule
since: only `.md` at `reports/` top level; images go in `reports/screenshots/`.

**2026-07-07 — worker collateral-kills the owner's live dashboard** · *Class 6* ·
A worker ran a kill-by-pattern (`pkill -f`) that matched the owner's own running dashboard.
Standing order since (CLAUDE.md): never kill by name/pattern; record and kill exact PIDs only.

**2026-07-08 — hourly quota dead zone fires park+notify every tick (41 texts)** · *Classes 1, 5* ·
GitHub's hourly GraphQL window ran dry; the gate mistook "rate-limited" for "no PR exists" and the
notify path repeated unbounded — two storms in one night, 41 texts, though both issues self-healed
and merged. Full doc: `INCIDENT-2026-07-08-park-notify-storm.md` (its sibling rate-limit doc is
superseded). Led to the notify-once guards and the refused≠empty read contracts.

**2026-07-09 — declared territory unprotected between session-finish and merge** · *Class 4* ·
A finished 77-minute build was invalidated by a conflicting merge the scheduler itself allowed in
the window after the session finished but before its merge. Metadata discipline was perfect; the
protection window was wrong. Full doc: `INCIDENT-2026-07-09-held-territory-window.md`.

**2026-07-10 — refused comment read parks a finished investigation** · *Class 1* ·
A worker posted its required marker comment; the runner's read failed, was represented as "no
comments," and the issue was wrongly parked ("no marker"). Fixed via typed read results
(issue #21/PR #49); the same bug class was then hunted down in three more read paths (#46, #61,
#78 — all built).

**2026-07-10 — missing TLS roots: zero launches all night** · *Class 2* ·
The owner's default `python3` had become a framework build with no TLS certificates; the runner's
usage-quota fetch failed and nothing launched. Third documented cause of the same `usage_stale`
symptom (recorded on #40). Fixed by running the Python `Install Certificates.command`.

**2026-07-10 — notifications silently dead for days** · *Classes 2, 5* ·
`~/.superlooper/notify_to` didn't exist; every notify exited rc=2 and nothing surfaced it. The
stack doctor now checks more of this chain; found only because a human read the journal.

**2026-07-11→12 — THE PROMISE HELD: 33 merges in ~28h, zero interventions** ·
Recorded so the ledger stays honest in both directions. 0 parks, 0 regenerations, 1
self-recovered nudge, 1 predicted scope-wander. This is what the system does when the
environment holds still.

**2026-07-12→13 — publish drift strands a fixed issue 38 hours** · *Class 3* ·
An investigation sat stranded at its gate for 38h on a bug that was already fixed on `main` — the
fix was merged but never republished/restarted into the running engine. The one-command
`liftoff` (#45) and drift surfacing came from this; the deeper lesson (deployments lag the
self-improvement loop) recurs below.

**2026-07-13 — bounce-notify storm: ~15 texts in minutes** · *Classes 3, 5* ·
A label rename migration had merged but was never applied (migrations ride `adopt`, which nobody
re-ran after republish), so a bounce's label write failed every 18-second tick and re-notified
every retry; the owner's mid-storm Drop didn't help because local state never absorbed the
external issue-close. Fixed live by creating the label; hardened by #108 (bounce notify-once,
external-close absorption, boot preflight of runner-managed labels).

**2026-07-13 (night, ×3) — launches die ~40 min after the owner walks away** · *Classes 2, 4* ·
macOS App Nap suspended the terminal app once its window went unwatched; new tabs were created
but their shells never ran, so delivery verification failed (rc=2) and the systemic-launch
breaker tripped — three times in one evening. The breaker held the queue correctly each time but
cannot re-arm itself; every recovery needed a manual restart. Fixed: App Nap pinned off (#120);
breaker self-re-arm filed as #115; Restart button #116.

**2026-07-14 — new UI over a stale server: "no such action"** · *Class 3* ·
The dashboard reads its JS from disk per request but the server keeps boot-time code; after the
loop merged the janitor UI, the owner saw a brand-new button whose tap returned a raw
"no such action" with a Retry that could never succeed. Filed as #136 (skew detection + honest
notice). Same night, smaller: Tidy couldn't see answerer-session windows (#132).

**2026-07-14→15 — the eApp night (second machine; owner-pasted report, canonical copy in that
repo's private/ROADMAP.md)** · *Classes 2, 4, 5, 6* ·
The densest incident to date; three high-priority lanes stalled overnight while merges kept
flowing. Chain: (a) the owner changed his machine-global Codex config for unrelated work → every
in-flight cross-review silently ran at ultra effort and timed out → workers aged past the freeze
threshold — *ambient state, nothing pins the reviewer per repo* (the plugin's cross-review runs
`codex exec` bare); (b) recovery relaunched a worker into a wedged window whose agent reported
logged-out — twice across two days; `/login` inside it never stuck; only closing the window
worked — *recovery trusts the window it revives*; (c) recovery nudges failed rc=3 four times,
visible nowhere; (d) a worker asked the owner a question in-session, bypassing the answerer — the
answer would have died with the window; (e) the publish sync silently clobbered an owner-ratified
edit made to the installed tree (recovered as `cb161ef`); (f) a finished, CI-green PR was parked
at the finish line because its diff touched a protected workflow file — knowable from the issue's
declared touches at approval time, and the park condition can never become false, so release
without a new owner verb just re-parks. Fix wave (reviewer pinning per repo; auth-probe before
brief on launch and recovery; sync drift refusal; stage telemetry incl. in-external-call state and
nudge-failure surfacing; evidence-based stop hook; approval-time merge pre-authorization +
resume-at-gate; durable Q&A on the issue) — proposed 2026-07-15, pending owner's word and a
fresh-eyes architecture review he requested.

---

## Reading the pattern

The classes are not equally expensive. Class 2 (ambient machine state) has caused the most lost
nights; class 4 (recovery) turns one-failure nights into multi-day sagas; class 5 costs owner
trust even when no work is lost; class 1 keeps shrinking as signals get mechanized and is the
system's clearest success story. Class 3 is self-inflicted: the loop improves itself faster than
its own deployment story propagates the improvements. Any proposal claiming to fix reliability
should say which class it attacks.
