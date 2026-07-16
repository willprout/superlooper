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

**Standing to date (2026-07-15, corrected by the two-round forensics audit that day).** The best
*verified* stretch is **23 merges in ~26.6h** (07-09 18:53 → 07-10 21:30, eApp) — and a GitHub
cross-check found **at least 6 human touchpoints inside that window** (an `agent-ready` re-add
that recovered a park storm, four merges with no journal record, one session interrupt), so the
honest unattended claim is the untouched overnight core (~23:12 → 09:15), not the full stretch.
Two earlier figures in this ledger did not survive the audit: "33 in ~28h zero interventions" is
not in any journal, and "88 lifetime merges" was a *gate* count — real lifetime merges are **91
across all homes** (eApp 62, command-center 22, sandboxes 7). Across every incident below:
**no bad merge, ever, and no owner code lost** — the failure mode is always *stall + owner
interrupt*, never corruption. (One nuance, 07-15: a recovery step destroyed gate *evidence*,
forcing a full re-review; work product survived. *Corrected 2026-07-15 by the i154 worker's
reconciliation:* the destroyed evidence was the **report** — a per-run artifact the reapprove
verb deliberately wipes (D11) — NOT the review attestation, which is a GitHub PR comment and
survives teardown. The same reconciliation exposed the inverse latent defect: a surviving
gen-1 attestation mechanically vouches for rebuilt gen-2 code the reviewer never saw — caught
before it ever fired a bad merge; fix rescoped into #154 as diff-pinned review evidence.) Roughly half of covered nights broke the promise somewhere. The owner's felt estimate
— "works three out of four times" — is about right, and the misses cluster in the classes below
rather than being random.

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
*Correction (2026-07-15 forensics):* this entry's numbers are not in any journal. The real best
window is 07-09→10 (23 merges/~26.6h) and it contained ≥6 human touchpoints (see Standing).
07-11's own journal shows 1 launch, 0 merges, and 15 NEEDS-WILLIAM parks — the "promise held"
night as remembered did not happen as written. Kept, annotated, as an example of why this ledger
now insists on journal-backed numbers.

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
*Correction (2026-07-15 forensics):* the App Nap attribution is withdrawn. The unified log
(retention verified to 06-23) shows **zero** cmux suspension/nap events across 07-09→13, the
machine is held awake around the clock, and the owner confirms keep-awake was set *before*
07-11. The rc=2 delivery failures were real but their cause is unproven (the era predates launch
stderr capture; see the i161 sidebar in the 07-15 forensics entry). The breaker-can't-re-arm and
manual-restart findings stand.

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

**2026-07-15 (morning, second machine) — the i336/i337 recovery marathon** · *Classes 3, 4, 5, 6* ·
Cost: two owner-driven runner restarts, three hand-merges, two state surgeries, one full review
re-run, a morning of owner + agent attention. Seven distinct defects confirmed with journal
evidence: **D8** the crash-recovery relaunch path bypasses `blocked-by` (a worker launched past
its open blocker; contained only by the worker's own step-0 reconcile bounce); **D9** stale pane
markers survive a bounce — the runner typed "are you progressing?" into a dead session's window
while the owner watched, and only a runner stop + hand-deleting two marker files cleared it;
**D10** identical relabel journaled twice 18s apart; **D11** "accept and relaunch" has two
opposite meanings — on a finished issue with a live PR it wipes the finish report and rebuilds
from scratch; **D12** doc drift as a root cause (ops docs name dead verbs, a sync orphaned the
installed docs, the debugger playbook wasn't installed on the machine having the incident);
**D13** the debugger rails and the verified recovery procedure contradict each other on
hand-merging; **D14** stop hooks failed `posix_spawn '/bin/sh'` ENOENT (root-caused same day —
see the forensics entry). A reapprove cycle also forced a full re-review by wiping finish
evidence. *Correction (2026-07-15, i154 reconciliation):* this entry originally said the deleted
worktree held "the only copy of the gate's review attestation" — wrong: the attestation is a PR
comment and survived; what the reapprove wiped was the report (deliberate D11 behavior). The
imprecision propagated into issue #154 as filed (a class-3 doc-drift instance inside this ledger
itself); the worker's launch-time reconciliation caught it and surfaced the real, inverse defect
(stale attestation vouching for rebuilt code — #154 rescoped to diff-pinning).

**2026-07-15 — the two-round forensics audit: four mysteries solved, two beliefs refuted** ·
*Meta-entry; verdicts recorded here because the bundles live off-repo (owner's machines).*
Round 1 (journals + system logs): App Nap refuted as the freeze cause; the track record corrected
(see Standing); the failure tail is concentrated, not spread — and ~25 of 29 eApp night-parks
were *designed* owner-decision escalations, not faults. Round 2 (worker session transcripts —
the worker-side flight recorder, opened for the first time):
(a) the **07-09 launch storm** (10 issues parked in 7 min) died at `cmux new-surface`: the
runner's launch-anchor pane lived in a workspace that had been deleted; cmux was alive and
refusing correctly; the park memos blamed the launch shim — the wrong component; no CLI ever
spawned. Human recovery at 23:12 (owner's `agent-ready` re-add).
(b) **i280's 14 all-night rc=3 nudge defers were CORRECT**: the worker had opened an interactive
`AskUserQuestion` dialog (protocol violation in an unattended session); the classifier rightly
refused to type into it — then the runner falsely *parked the live, actively-working lane*. The
worker also wrote its report to a worktree-relative path, invisible to the runner.
(c) the **i336 logged-out wedge** was an in-process auth death inside one long-lived session
(00:15–08:09; it also killed the session's wake timer); Codex was never logged out (its failures
were shared-subscription contention timeouts). The runner nudged the "Not logged in" screen for
94 minutes because a login prompt classifies as "idle, safe to send." Root cause unknowable from
disk; forward captures specified.
(d) **D14 solved**: the runner prunes a finished lane's worktree while the worker CLI still sits
in it; the next turn's hook spawn dies (cwd deleted) — killing the liveness/exit stamp at the
exact moment of completion. Four occurrences on 07-15 alone; the mechanism behind zombie windows
and blind recovery cascades.
(e) **rc=0 "sent" is not "arrived"**: of six successfully-typed nudges to one session, only three
registered as turns and one arrived corrupted, interleaved with the session's own output.
Delivery-by-exit-code is a fiction in both directions.
Also: round 1's own report mislabeled which machine it ran on — a class-3 drift instance inside
our own forensics, caught by the owner.

**2026-07-15 (afternoon) — i328: a finished, merged issue stalls the queue two hours** ·
*Classes 1, 4, 6* · All three completion signals defeated at once: the worker wrote its report
worktree-relative (the second occurrence *that day*); the PR was merged out-of-band so the
runner — which only associates branch→PR once an issue *finishes* — still carried `pr: null` and
never knew the PR existed; and an interactive session doesn't exit when done. The idle ladder
then looped in a way it structurally cannot escape: each "are you progressing?" answer is a
tool-call turn that *refreshes the very liveness stamp the ladder watches*, so it can never
escalate — 8 nudges ~497s apart, the runner asking in prose and unable to hear the prose answer
("I'm done, complete"). The owner unstuck it by telling the session to move its report. Exposes:
report-path compliance must be mechanically rescued, not instructed; in-flight lanes need
per-tick branch→PR reconciliation; probes must demand machine-readable replies and must not
reset the clock they feed.

**2026-07-16 (overnight) — the first Wave-1 regression: the report harvest promotes drafts** ·
*Classes 1, 4* · Two lanes in one night (i153 03:29, i163 04:33): workers drafted their reports
in-worktree mid-session (against the "report is your LAST action" order — class-6 drift the new
machinery was meant to absorb, not amplify); the brand-new #148 Stop-hook harvest moved the
drafts to the canonical path at a turn boundary (report mtimes match the harvest moments to the
second; i153's Review section is a literal placeholder that also cleared the 40-char section
check); the runner read "finished", the gate — correctly fail-closed — parked both on "finished
but no PR exists", and the park-family reclaim then pruned worktrees whose branches carried ZERO
commits and no push: the only copies of both sessions' work were destroyed. No bad merge (the
gate held); cost = ~2 worker-sessions of output lost + 2 owner-decision parks. Fixes filed as
#189 (harvest fires only for a genuinely-ended run; placeholder sections never satisfy the
section check) and #190 (reclaim refuses to prune a dirty/unpushed worktree).

**2026-07-16 (overnight) — the diff-pinned review contract parks its own PR** · *Class 3* ·
i154 built the new pinned review marker (`<!-- superlooper-review sha=… -->`) and its reviewer
posted the verdict in that new format on PR #181 — which the *running* engine's gate (installed
before the change; the change itself was still unmerged) reads as "no review evidence"
(`_any_comment_begins` requires the legacy `<!-- superlooper-review -->` prefix): nudged once,
parked. The self-referee bootstrap case: a worker applying its own new contract to itself is
judged by the old referee until the change merges AND republishes. Work product fully intact
(PR #181 open, reviewed APPROVED-after-fixes, pinned to the unchanged head). Dashboard-side
sibling already filed as #176.

---

## The owner's documented frustrations (recorded 2026-07-15, in his words where quoted)

Recorded as first-class reliability data: this is what the failures *cost*, and any fix wave
that doesn't retire items from this list isn't fixing the right things.

- **The question flow is "so broken it's not even funny."** The ask card shows only the first
  ~3 lines, so he can't read the question he's being asked to answer. There is no answer path in
  the UI: Approve-and-relaunch "half the time throws away the work"; Drop kills the PR; the only
  real path is typing into the terminal window — which works, but leaves the ask card frozen
  forever, and unblocking *that* requires his helper agents, "and then they break it worse."
- **Finished work gets destroyed by the recovery verbs.** He sees "approve and relaunch" twice a
  day minimum, and understands it to mean throwing away a build that took 2 hours and multiple
  review rounds. (Root-caused as D11 — mostly a verb defect, not the regenerate policy — but the
  felt cost stands.)
- **Frozen sessions are immovable.** Lanes get blocked; sessions stay frozen "no matter if I
  restart or press drop or press approve"; recovery has required stopping the runner and editing
  state files by hand. "Nothing is intuitive for how to handle these failures."
- **His helper agents cannot understand superlooper.** They guess, and guessing has made
  incidents worse. The system "has gotten so complex and changes shape that I don't even
  understand how it works anymore." (D12 made this concrete: the docs they'd need were wrong,
  orphaned, or not installed.)
- **He assumed the dashboard was a live, perfect mirror of the runner. It isn't, and nothing
  says so.** Documented divergences: an externally-closed issue never absorbed; a dead session
  shown as "launching"; the frozen ask card; a day-stale server under fresh UI. (Fix queued as
  #146.)
- **"Questions" that aren't questions.** Much of what lands on him is the system's own
  anticipated mechanism — "I can't merge without human approval isn't a question, it's a failure
  mechanism to be anticipated" — arriving ungracefully, at night, blocking lanes, on repos where
  the sensitive-area refusal was knowable at approval time.
- **The queue stalls on agent sloppiness.** A whole afternoon queue stalled two hours because
  one agent put its report in the wrong folder (i328) — "an agent couldn't follow instructions"
  must not be able to cost a queue.
- **The overnight promise itself:** "kind of shattered when it only works [attended]." He has
  run several hardening passes and wants the next one to be the last — and is explicitly not
  confident that incremental patching is the right approach at all (a fresh-eyes architecture
  review was commissioned 2026-07-15 at his request).

---

## Reading the pattern

The classes are not equally expensive. Class 2 (ambient machine state) has caused the most lost
nights; class 4 (recovery) turns one-failure nights into multi-day sagas; class 5 costs owner
trust even when no work is lost; class 1 keeps shrinking as signals get mechanized and is the
system's clearest success story. Class 3 is self-inflicted: the loop improves itself faster than
its own deployment story propagates the improvements. Any proposal claiming to fix reliability
should say which class it attacks.

*Re-read after the 2026-07-15 forensics:* the audit moved weight between classes. Class 2's
headline villain (App Nap) is withdrawn — every adjudicated "environment" failure traced instead
to class 4 (our own lifecycle bookkeeping: a stale launch anchor aimed at a deleted workspace,
markers outliving sessions, worktrees pruned under live CLIs) or class 6 (workers writing reports
to the wrong path, opening interactive dialogs, merging their own PRs out-of-band). Two
cross-cutting patterns now have names: **the runner's senses and its subjects speak different
languages** (it reads files and exit codes; sessions answer in prose and dialogs — so "I'm done"
is inaudible and a question dialog reads as a hang), and **its probes contaminate their own
measurements** (a nudge answer refreshes the liveness clock the ladder watches; a "sent" exit
code counts as delivery). And one class got *larger*: most of what lands on the owner at night is
class 5 by design — anticipated owner-decisions arriving ungracefully — not failure at all.
