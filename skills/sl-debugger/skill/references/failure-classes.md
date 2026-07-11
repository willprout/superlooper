# Failure classes — signature → diagnosis → repair

The documented incident classes, each validated against how the real incident actually
resolved. The corpus lives in `skills/superlooper/docs/INCIDENT-*.md` (plus issue #21 /
PR #49 for class 4); read the source doc before doing anything clever — each one records
what was tried, what worked, and what the engine has since grown to prevent recurrence.
"Current engine" below means main at `a77801d` (2026-07-11); a recurrence of a "fixed"
class on a live machine should FIRST be checked against publish drift
(`health-readout.md` §8) — the fix may be merged but never republished.

---

## Class 1 — the wedged tick (INCIDENT 2026-07-07: binary file in `reports/`)

**Signature.** Journal shows the same `tick_error` repeating at tick cadence (~15s apart,
dozens to hundreds of times). Heartbeat stale while `runner.lock` pid is alive
(alive-but-wedged). Finished sessions never detected; the gate never runs — a PR can sit
OPEN / CI-green / review-posted and nothing merges; nothing launches; no notification.
In the live incident: 130+ repeats of a `UnicodeDecodeError` over 42 minutes, from PNG
screenshots (and then a Finder `.DS_Store`) dropped loose into `<home>/reports/`, which the
tick's report scan read as UTF-8 text.

**Diagnosis.** Read the repeated `tick_error` payload — it names the exact exception. If it
points at a file read, list the scanned dir for foreign entries: anything in `reports/`
that is not an expected `*.md`. More generally: ANY repeating identical `tick_error` is
this class — the tick hits the same rock every 15s and will do so forever; the payload is
the rock's name.

**Repair.** Remove the rock; recovery is immediate — the next clean tick detected the
finished session, gated, and merged with **no restart needed** (that is exactly how the
live incident resolved: PNGs moved into a `reports/screenshots/` subdir — subdirectories
are safe — then the `.DS_Store` removed). Move, don't delete (reversible). Current engine
defenses (shipped from this incident's fix session): binary reads count as absent, dotfiles
skipped, ≥4 consecutive tick crashes raise ALERT `runner_tick_errors:<n>` + one notify,
heartbeat stamped at end-of-successful-tick so a wedge reads stale, `tick_error` records
bounded to ~500 chars. So on a current engine a repeating `tick_error` means a NEW rock —
same playbook (read payload → find rock → remove it), then record the payload for a fix
issue. The incident also left a cleanup procedure for a journal bloated by the storm
(archive-don't-delete, runner stopped) — that is state surgery, rung 3 of
`repair-ladder.md`.

---

## Class 2 — the park/notify storm (INCIDENT 2026-07-08: hourly API-quota dead zone)

**Signature.** Park + notify pairs at tick cadence for an issue that is genuinely finished
— gate verdict "finished but no PR exists" while the PR is visibly open on GitHub; the park
label move and park comment fail in lockstep the whole window; the owner's phone buzzes
every 15 seconds (41 texts across two storms in the live incident). Distinctive
fingerprint: storms end within seconds of the same minute past the hour — the account's
hourly GraphQL window reset (the live one reset at ~:11:55).

**Diagnosis.** `gh api rate_limit` — read the `graphql` bucket's remaining/reset. Correlate
the journal's park-record timestamps with the reset minute. The mechanism: PR lookups ride
GraphQL; under combined machine load (runner poll + dashboard + workers on one token) the
hourly budget runs dry minutes before each reset, the read adapter used to collapse
"GitHub refused" into "GitHub answered: nothing", and the park path had no
already-notified memory while its own label write kept failing.

**Repair.** Mechanically: nothing — the live storms self-healed at quota refill and both
issues merged cleanly; no work was lost. The repair surface is the engine, and it has since
shipped (park-notify guards, #61/#77 merged 2026-07-10, plus the rate-limit posture): a
refused PR read is now distinguished from answered-empty, the gate HOLDs up to a ~15-min
bound before parking once, notify fires at most once per (issue, park-cause), and a park
label stuck past ~10 min raises one ALERT (`park_label_stuck`). If you see this storm
signature on a current engine: check publish drift first, then treat it as a regression or
a new refused-read path — capture the journal window + a live `gh api rate_limit` reading
into an issue; do not hand-patch the engine live. (A superseded twin doc from the same
night records the dashboard's own quota burn — concluded flights are now fetched once and
remembered, so a dashboard re-polling merged PRs forever is also a drift/regression flag.)

---

## Class 3 — held-territory regeneration (INCIDENT 2026-07-09: declared territory unprotected between finish and merge)

**Signature.** Journal reads: `launch` of issue B whose `touches:` overlap issue A's, one
tick after A entered `gating`/`holding` (finished, PR open, gate waiting); later `merge B`,
then A's update reports a real conflict → `regenerate A` → relaunch (`-r1`). A finished
build (77 minutes in the live incident) is discarded and rebuilt; the cost is pure
wall-clock, no work corrupted — the ladder (no LLM in merge mechanics, no force-push,
branch preserved) worked as designed.

**Diagnosis.** Reconstruct the ordering from the journal; check `conflicts` counters in
`issues.json`. Root cause was an engine design gap: anti-affinity protection ended when a
session stopped RUNNING, not when its PR merged, so a finished-but-unmerged issue's
territory was invisible to the scheduler — and the window is widest exactly on well-gated
repos where the gate must wait on CI.

**Repair.** Shipped and live: issue #6 / PR #14 (merged 2026-07-10, republished + runner
bounced same day) — territory claims now persist through `gating`/`holding` until merge;
they release on merge, regenerate, and terminal parks (a parked wildcard releases, so a
no-touches repo can't freeze); investigations neither hold nor are held. The old interim
mitigation (`blocked-by` chaining overlapping issues) is retired. If regenerations recur
today: (1) publish drift check; (2) were `touches:` honest on both issues? (the morning
report's Wanders section is the tell); (3) the conflict cap (default `conflict_cap: 2`, counting conflicts, not
regenerations) is the designed endpoint: the first conflict regenerates, the second parks
`needs-william` — resolution is the owner re-scoping the pair, or a `preserve` label to
route an expensive PR to in-branch conflict resolution. That
re-scoping is an owner decision; the debugger's job is the memo that makes it one touch.

---

## Class 4 — the mis-parked finished investigation (2026-07-10, live #8; record: issue #21 / PR #49)

**Signature.** A finished investigation: the worker posted its
`<!-- superlooper-investigation -->` marker comment, and within a minute the runner parked
the issue "no marker comment" — the marker is visibly THERE on the issue when you look.
Sibling shape (same class, other direction — observed on a second machine the same day): a
finished investigation waits silently forever, no park, no journal record — the poll's
want-set grew with merged history until the per-poll call budget starved the tail.

**Diagnosis.** Compare the marker comment's GitHub timestamp against the journal's park
record (in the live incident: marker 06:42:05Z, park 06:42:46Z — the park read was refused
or stale, not empty). The mechanism was the same refused ≠ empty collapse as class 2, one
adapter over: `issue_comments` failed closed to `[]`, indistinguishable from "no comments",
and the nudge→park ladder was terminal off that single unverified read.

**Repair.** On the current engine this class self-heals — the #21 fix (PR #49, merged
2026-07-10) made comment reads carry an ok/refused flag, made the investigate gate HOLD on
refused reads (one bounded `await_read` journal record per episode) and park only on a
fresh clean read, and — the part that matters when you find one already parked — made a
parked investigation whose marker appears on a later clean read **reconcile automatically**
(closed without re-approval). The poll want-set now excludes terminal issues and a
budget-exempt rescue refreshes finishing investigations, so the starved-tail sibling can't
recur either. Finding this signature today: publish drift first; a parked investigation
with a visible marker on a CURRENT engine should clear itself within a poll — if it
doesn't, capture the journal episode and the comment read outcomes into an issue. On an
old engine, the mechanical path is the owner's: he re-approves or closes on the strength of
the posted report — his word, not yours.

---

## Beyond the corpus — a differential for undocumented symptoms

| Symptom | First suspects (in readout order) |
|---|---|
| Queue full, nothing launches | usage meter fail-closed (fresh over-ceiling read); ALERT `launch_anchor_down`/`launch_systemic_failure` (anchor died — queue deliberately held intact); `gh_unreachable`; corrupt `issues.json` (launches held); duplicate `model:*`/`effort:*` labels (the runner waits, never guesses — no park, no memo) or missing `touches:` where required (parks `needs-william` with a memo) |
| Builds finish, nothing merges | merges frozen (designed idle — check `source`); required check name typo (green PR "waits" forever — `doctor` cross-checks names); review-marker comment missing (gate nudges once then parks); CI genuinely pending |
| Parks with baffling memos | dev branch missing on origin — current engines name the missing branch in the memo (`launch_base_missing`, issue #28); a memo blaming the launch shim is the generic delivery failure (`launch_delivery`) or a drifted pre-#28 engine. The park cause taxonomy in `lib/actions.py` is the index — the memo's `cause` string is your lookup key |
| Owner never notified | notify channel: config `notify.imessage_to`/`notify.cmd`, Messages automation permission, `doctor --stack` sends a live test; sends are journaled (`cmd notify failed (rc=…)`) |
| Dashboard says everything is dead | believe the heartbeat, not the paint: `runner-down` pill = heartbeat stale (whole surface untrusted); grey plane on a finished session = the stranded-vs-dead mispaint (check `reports/i<N>.md` yourself) |
| "Fixed" bug is back | publish drift (`VERSION` vs main) before regression |
