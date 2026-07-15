# Shareability hardening — the to-do queue (2026-07-10, owner-prioritized)

From the 2026-07-10 read-only audit of `origin/main` `d8bd42b` (fresh clone + live state home +
installed skill), with William's priority rulings applied the same day, cross-checked the same
afternoon against the planning-machine incident reports (see the cross-check record below), and
**filed as GitHub issues #21–#45 that evening** — issue numbers are recorded per item. The Last
tier is deliberately UNFILED: the owner sequences it after everything above, and the README is a
supervised session with William directly, never a loop issue. No issue carries `agent-ready`;
approval is William's word, applied per the approval protocol when he releases work.

Priority is encoded on GitHub as: root causes `priority:high`, rest of Now unlabeled (normal
band), Next + Bundle `priority:low`. Line numbers in this doc are dated pointers to `d8bd42b`;
the issues themselves point at functions/files per the thin-issue doctrine.

Goal, verbatim intent: a stranger — a competent computer user, not necessarily a developer — can
install, run, and misuse this with ordinary user error without wrecking their repo, their GitHub
state, or their machine. Usability only; security was explicitly out of scope.

---

## Now — the root causes first (owner ruling 2026-07-10 PM), then in order

**Q25+Q2 → filed as #21 (priority:high) — Never strand or wrongly park a finished
investigation.** The two ways a finished investigation dies, one fix session owns both. (a) Live
2026-07-10, this repo's #8: marker comment posted 06:42:05Z, parked "no marker comment" at
06:42:46Z — `gh.issue_comments` collapses every error to `[]` (refused ≠ empty), the ladder is
nudge-once-then-park off a single unverified read, no self-heal when the marker appears
(`skill/lib/gh.py:145-148`, `skill/lib/gate.py:233-251`). (b) Verified live in monorepo code: the
poll want-set includes every issue with a report file on disk and reports are never pruned, so it
grows with merged history (`skill/bin/runner.py:444-449`); the fetch walk starves the tail under
`MAX_POLL_CALLS = 30` (`runner.py:49,450`); the rescue path refreshes finishing PRs only
(`runner.py:477`); a starved investigation hits `if iid not in issue_comments: continue` — no
journal line, forever (`skill/lib/actions.py:431-432`). On the planning machine this stranded the
repo's first investigation 90+ minutes while builds sailed past. *Size: medium.*

**Q25 dashboard half → filed as #22 (priority:high) — Render a stranded gate as its own state.**
A completed-session-with-stuck-gate paints as a dead session today: `gating` maps to `FINAL`
(`dashboard/lib/flights.py:216-217`), `FINAL` ∈ `_IN_AIR` (`:201`), aged liveness turns any
in-air stage `SESSION_FROZEN` (`:259-260`). "Stranded at gate" is its own state. Joy gate
applies. *Size: small.*

**Q26 → filed as #23 (priority:high) — Auto-unfreeze must be reachable.** The unfreeze rule feeds
on `gh.branch_checks` — REST check-runs only (`skill/lib/gh.py:165-169`,
`skill/lib/actions.py:360-364`) — while PR gating reads GraphQL `statusCheckRollup` (check-runs
AND commit statuses, `gh.py:30`). A repo with a PR-only commit status in `required_checks` can
never evaluate dev "green": freeze = permanent merge outage until a human deletes
`state/merges_frozen.json` (observed 2026-07-09). Dev view must see the PR view's universe,
and/or config splits PR-required vs dev-required. Q3/#26 carries the adoption-time catch.
*Size: medium.*

**Q27 → filed as #24 (priority:high) — A dead launch anchor must never walk the queue.** Pane
resolved once at boot (`skill/bin/superlooper:198`, `skill/bin/runner.py:241`), never
re-validated; delivery failure handled only per-issue (`LAUNCH_FAILURE_CAP = 2` → park → notify,
`skill/lib/actions.py:70,586-588`). An anchor lost to normal tab-tidying converted 10 approved
issues into 10 parks + 10 texts in ~8 minutes while gating/merging stayed healthy. One runner-level
alert, hold launches, keep merging, leave `agent-ready` intact. *Size: medium.*

**Q1 → filed as #25 — Notify must be provably working, not merely configured.** Live on the
owner's machine 2026-07-10: `~/.superlooper/notify_to` doesn't exist, every send exits 2, journal
shows `cmd notify failed (rc=2)`, the #8 park never reached the owner — and the stack doctor
passes on presence alone (`skill/lib/stack_doctor.py:189-192`). Doctor sends a real test message.
(Machine fix, outside the repo: create `notify_to`.) *Size: small.*

**Q3 → filed as #26 — Validate `required_checks` actually report; bound the pending wait.** A
never-reporting check name = permanent silent `gating` (`skill/lib/gate.py:166-174,306-309`);
doctor only checks non-emptiness (`skill/bin/superlooper:385-389`). Amended per root cause A:
doctor also verifies each check reports on the DEV branch, not just PRs. *Size: medium.*

**Q4 → filed as #27 — Cap and surface refused merges.** Branch protection or a merge-right-less
token makes `gh pr merge` fail every tick forever with no counter, park, or notify
(`skill/bin/runner.py:1199-1209`). *Size: medium.*

**Q5 → filed as #28 — Detect and validate `dev_branch`; memo names the real cause.** Adopt writes
`"main"` blind (`skill/bin/superlooper:291-301`); on a `master` repo launches fail and the park
memo blames the launch shim (`skill/lib/actions.py:586-588`). *Size: small–medium.*

**Q6 → filed as #29 — `adopt` fails loudly when label creation fails.** FAIL lines then exit 0
today (`skill/bin/superlooper:286-340`). *Size: small.*

**Q7 → filed as #30 — Stack doctor: missing Codex is a WARN, not a FAIL.** Owner ruling: an
independent same-model fresh-subagent review is a valid review path
(`skill/lib/stack_doctor.py:227-237` vs `skill/bin/superlooper:414-418`). *Size: small.*

**Q8 → filed as #31 — Put the `superlooper` command on PATH at publish.** Every doc invokes it
bare; nothing links it (`bin/install.sh`, `docs/ADOPTING.md:17-19`, `docs/STACK.md:34`).
*Size: small.*

**Q9 → filed as #32 — Fix ADOPTING.md's stale text and impossible order.** "Built in a later
task" is stale; adopt → doctor → install guarantees a red doctor
(`docs/ADOPTING.md:25-26,163-170`). *Size: small.*

**Q10 → filed as #33 — Ship a truthful keep-alive/restart story.** The launchd runner template
can never start (no cmux pane detached; preflight hard-fails) and KeepAlive crash-loops it
(`skill/templates/launchd.runner.plist:10-11`, `skill/bin/runner.py:158-195`). Amended per root
cause C: document the proven manual restart as THE procedure (automated placement failed two ways
in one night — do not build it); boot line already prints the resolved pane + source
(`skill/bin/superlooper:198-203`); add workspace visibility and, if cheap, a doctor anchor check.
*Size: small–medium.*

**Q11 → filed as #34 — Dashboard: friendly port-in-use error and honest first paint.** Taken port
→ raw `OSError` traceback (+ launchd crash-loop); dead server → eternal "connecting to the
field…" (`dashboard/bin/command-center:113`, `dashboard/lib/server.py:417-421`,
`dashboard/static/shell.js:77-91,175-206`). *Size: small.*

**Q12 → filed as #35 — Runway count reflects truth** (owner bumped). "2 RUNWAYS OPEN" hardcoded
regardless of `lanes` (`dashboard/static/shell.js:277`, `static/boards.js:107`). *Size: small.*

**Q13 → filed as #36 — Config knobs must not lie.** `touches_required` validated then never read;
wildcard-touches silently serialize lanes; no-match `areas` serialize merges
(`skill/lib/config.py:28,113,200-208`, `skill/lib/scheduler.py:57-66`,
`skill/lib/gate.py:120,295-298`). *Size: small.*

**Q14 → filed as #37 — Morning report reconciles outcomes.** The 2026-07-10 report lists #2 under
both Merged and Parked (`skill/lib/report.py`; evidence
`~/.superlooper/willprout__superlooper/reports/morning-2026-07-10.md`). *Size: small.*

**Filed post-approval, 2026-07-10 late PM → #46 (normal band, NOT yet approved) — Fail open on an
unreadable usage meter; fail closed only when it reads exhausted.** From the live TLS incident
(usage meter dark for hours while actual usage sat at 0%/14% and nothing launched): the policy
conflates "can't read the meter" with "tank empty" — the quota version of refused ≠ empty. Owner
direction: launching into genuinely exhausted quota and letting sessions hit the wall beats a full
stop with plenty of usage. Keep fail-closed on a read-exhausted meter; fail open (bounded, journaled,
still alerting once) on an unreadable one; safety rides #24's systemic-launch breaker. *Size:
small–medium.*

## Next (agreed, deliberately behind the Now tier)

**Q15 → filed as #38 (priority:low) — Dashboard: honest "GitHub unreachable" state.** gh
missing/unauth → cheerful all-clear indistinguishable from no work (`dashboard/lib/gh.py:51-87`,
`static/shell.js:277`, `static/boards.js:107`). Calm is never ambiguous; make the fix a delight
moment (§0.1). *Size: small–medium.*

**Q16 → filed as #39 (priority:low) — Surface publish drift.** Installed skill was `cfa4db0` with
six merged fixes inert and no surface said so. Gated publishing stays; the invisibility is the
bug. *Size: small–medium.*

**Q17 → filed as #40 (priority:low) — Opaque parks on CLI/model drift.** Launch stderr dies with
the cmux tab; memo says "relaunched 2 times (cap 2)"; `usage.py` pins User-Agent `2.1.90` and the
`usage_stale` alert names no remedy (`skill/bin/start-session.sh:66,102-103,136`,
`skill/lib/usage.py:8-10,18,95`). *Size: medium.*

**Q18 → filed as #41 (priority:low) — Prune long-haul growth.** Journal never rotated and slurped
whole; `events/processed/` accumulates forever; only merged worktrees cleaned
(`skill/lib/journal.py:44-47`, `skill/lib/events.py:149,165-172`, `skill/lib/tidy.py`).
Coordinates with #21's want-set bound. *Size: medium.*

**Q19 → filed as #42 (priority:low) — Sleep/wake grace.** Wake fires spurious "inactive" nudges +
a false `usage_stale` text (`skill/lib/events.py:96-101`, `skill/bin/runner.py:1145-1149`,
`skill/lib/actions.py:65-66`). *Size: small–medium.*

**Q20 → filed as #43 (priority:low) — Universal brief footer: screenshots rule + never-pkill.**
The reports/screenshots/ rule lives only in command-center's CLAUDE.md; this repo's workers
dropped PNGs loose in `reports/`; the 2026-07-07 incident §4 already suggested promoting the
pkill rule (`skill/templates/brief-footer.md`). *Size: small.*

**Q28 → filed as #44 (priority:low) — Dashboard clarity leftovers (root cause D residuals).** The
big two are already built (two-tap drop confirm, `dashboard/static/needsyou.js:54-55`;
state-derived counters). Remaining: active repo tab must pass the glance test
(`static/shell.js:284`), and drop states its consequence in plain words. *Size: small.*

## Bundle (one combined effort, owner-requested scope)

**Q21 → filed as #45 (priority:low) — Single-command startup + engine↔dashboard schema
handshake.** One command brings up the pair; engine stamps a state-format version the dashboard
checks and names on the field when mismatched, converting silent blankness into one honest line
(`dashboard/lib/readers.py`, `lib/flights.py`, `lib/config.py:116`). Engine stays
dashboard-agnostic; runner stays in a visible cmux tab. *Size: medium–large.*

## Last (owner-sequenced to the end)

*Update 2026-07-10 evening: Q22 and Q23 are now FILED AND APPROVED as #57 and #58
(priority:low, so they run after everything else in the band — the owner's "go last" preserved
by band + number order). Only Q24 (the README) remains un-queued, deliberately: it is a
supervised session with William directly, after the queue has made its promises true. With that,
this to-do list is fully dispatched except the README.*

**Q22 → filed as #57 — Non-web default report sections.** The default `report_required_sections` demands
"Browser evidence," so a CLI/library repo's workers park on a section they can never satisfy.
Verified NOT a dogfooding risk: this repo and the eApp both override the sections in their own
configs — so it waits. *Lives in:* `skill/lib/config.py:33`,
`skill/templates/config.example.json:19`, `skill/lib/gate.py:43-65,266-268`. *Size: small.*

**Q23 → filed as #58 — De-Williamization.** Every stranger's loop signs its work "William": dashboard audit
comments ("Approved by William via command-center") on their issues, the `needs-william` label in
their repo, briefs saying the word is "William's alone," the answerer reasoning about what
"William" would decide. Add an operator-name config field (both halves) and make labels/briefs/
comments generic. *Lives in:* `dashboard/lib/actions.py:39,49-62`, `dashboard/static/shell.js:437`,
`dashboard/lib/digest.py:157`, `dashboard/lib/tower.py:166`; engine
`skill/templates/brief-footer.md:28`, `skill/templates/answerer-brief.md:21-23`,
`skill/bin/superlooper:63-65`, `skill/lib/actions.py:332`, `skill/lib/gate.py:257,263`.
*Size: medium.*

**Q24 — THE README (the very last thing; supervised session with William directly, never a loop
issue).** Owner ruling: rewrite from scratch (the existing untracked draft is not it) and ship it
last, once the queue above has made its promises true. It carries: the front door and start-here
sequence (publish → adopt → doctor → run), the full prerequisite list including what cmux IS and
where to get it, and the plain "this runs on a Mac" statement (on Linux the loop starts and
silently launches nothing — Keychain-gated usage). Also clean the owner-specific text out of root
`AGENTS.md`. *Size: small–medium.*

## Cross-check record — planning-machine incidents vs current monorepo (2026-07-10 PM)

The 2026-07-09/10 incident report + addendum from the older-engine machine were checked mechanism
by mechanism against `origin/main` `d8bd42b`:

- **Root cause A (unreachable auto-unfreeze): STILL LIVE** → #23 (+ #26's doctor amendment).
- **Root cause B (anchor loss walks the queue): STILL LIVE** → #24.
- **Root cause C (restart ergonomics): partly done** — boot line prints the resolved pane + source
  (`skill/bin/superlooper:198-203`); preflight hard-fails outside cmux with an actionable message.
  Remainder (document the manual restart as THE procedure; anchor-visibility check) → #33.
- **Root cause D (dashboard UX): mostly already built post-merge** — drop is a two-tap confirm
  whose state survives re-renders (`dashboard/static/needsyou.js:54-55`,
  `dashboard/static/shell.js:13,37`); counters/stages are derived server-side from runner state
  with "queued — not yet in the air" rendered distinctly (`shell.js:158,304`; design record §3/§5),
  not from label-state. Residuals (active-tab glance test, drop consequence wording) → #44.
- **Root cause E (gate starvation): STILL LIVE, 1:1** — want-set growth, 30-call budget, PR-only
  rescue, silent `continue`, and the frozen-mislabel all verified in current code → #21 + #22.
- **§6 precursor (transient rc=2 delivery failure under a healthy anchor, possibly display-sleep
  gating of background-tab shell boot): recorded, not forced.** If #24's work touches the sentinel
  wait in `skill/bin/start-session.sh`, check the timeout tolerates a sleeping display.
- **Related self-heal note:** the same night's queue-walk would have been 10 parks but ONE text
  under the notify-once-per-cause guards — the parked park-storm work this incident independently
  argues for (second argument; preconditions already satisfied per #21's context).

## Dropped / corrected

- **Linux usage pill forever showing `usage ?`** — owner doesn't care; dropped.
- **`model:fable` starter label** — the audit's claim was wrong: Fable 5 is generally available
  (Claude Mythos 5 is the org-gated variant), and an unknown model string already fails the launch
  and parks with a memo. Seeding stays; no change needed.
