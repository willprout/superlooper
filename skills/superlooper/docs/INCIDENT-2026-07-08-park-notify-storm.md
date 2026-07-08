# INCIDENT 2026-07-08 — hourly API-quota dead zone fires park+notify every tick (41 texts, two issues)

**STATUS: PARKED (owner ruling 2026-07-08).** The engine guards below wait until (a) the
command-center quota fix (deployed 2026-07-08) has produced some running data, and (b) a paired
investigation session counts GitHub API calls across everything sharing the token (runner,
dashboard, workers) to size the real budget. Do not start the fix session before both.

**For a fix session in THIS repo.** Found live on the command-center loop; first investigated by
a read-only subagent at ~00:20 (whose snapshot saw only storm 1), root cause revised by the
orchestrator at ~08:05 with the full night's journal plus a live `gh api rate_limit` reading.
No work was lost; both issues self-healed and merged. Two defects: the UNBOUNDED repeat-notify,
and the gate mistaking "GitHub is rate-limited" for "no PR exists."

## 1. What happened (Mac-local, night of Jul 7→8) — TWO identical storms, one hour apart

- 00:02:12 — worker i32 (command-center #32) creates PR #43. Build fully successful.
- 00:04:45–00:11:16 — gate cannot see the (existing) PR → park "finished but no PR exists" +
  **notify text**, repeating at tick cadence: **21 park+notify pairs**. The park's label move
  and the park comment fail in lockstep the whole window.
- 00:11:33 — visibility returns; next gate sees PR #43 → clean squash merge.
- 00:27:45 — i34 finishes → **merges the same tick, zero parks** (mid-hour, quota available).
- 01:04:26–01:11:33 — i36 finishes into the same dead zone: **20 more park+notify pairs**,
  identical signature. 01:11:49 — clean merge. 01:50:34 — i41 merges same-tick, clean.
- Total: **41 texts, 00:04→01:11**, two ~6.5-min storms ending at the same minute past the hour.

## 1b. Root cause of the blindness: hourly GraphQL quota exhaustion (high confidence)

The fingerprint: both storms END within seconds of **:11 past the hour**. Live check at 08:05
(`gh api rate_limit`): the account's GraphQL window **resets at :11:55 past each hour**, and was
already 86% consumed (4,297/5,000) mid-window. `gh pr list/view` — the gate's PR lookups — ride
GraphQL. Under the machine's steady combined load (runner poll + command-center dashboard
pollers + worker sessions, all one token), the 5,000-point hourly budget runs dry a few minutes
before each reset; any issue finishing inside that dead zone gets "no PR" (and failing label
moves and comments) until the refill. The earlier "transient GitHub blip" hypothesis is
falsified in its specifics: this is self-inflicted, recurring, and will hit any hour with heavy
load. Unproven: the burn-share per client (dashboard vs runner vs workers) — the adapter's
fail-closed design swallows the 403s (zero rate-limit strings in runner.log), so attribution
needs the observability fix below. Command-center's dashboard cadence/token is that repo's own
follow-up, out of scope here.

## 2. Root cause of the storm (exact locations)

The park path has no "already handled" memory when its own persistence fails:

- `gate.py` (~189–250): the "no PR → park" verdict is re-derived from scratch every tick — no
  memoization of a prior identical verdict.
- `actions.py` `park()` (~276–284): calls `notify()` **unconditionally** on every invocation.
  Other failure paths in the same file use a `nudge_or_park()`-style once-then-silence guard;
  this one does not.
- `runner.py` `_exec_park()` (~992–1015): local status only settles to `parked` (terminal,
  stops re-deciding) AFTER the label move succeeds. When GitHub is the thing that's down, the
  label move fails every tick → state never settles → decide() re-parks → notify() re-fires.
  Unbounded: 21 was this run's count only because GitHub recovered.

## 3. The fix (one incident, three guards — plus an optional grace)

1. **Notify at most once per (issue, park-cause).** Mirror the existing once-guard discipline:
   a durable local marker (stamped BEFORE attempting the label move) suppresses repeat notifies
   for the same cause; clear it when the issue leaves the failing state (merge/relaunch/label
   success). The park RETRY may continue silently — it's the texting that must be once.
2. **Rate-limited is not "no PR".** The adapter must DISTINGUISH "GitHub refused (rate
   limit/403/5xx)" from "GitHub answered: nothing there" — today both collapse to the same
   empty-typed return (fail-closed, gh.py), so the gate treats quota exhaustion as a missing
   PR. On a refused read, the gate should HOLD (no park, no text, freeze-is-safe-idle posture)
   until reads succeed or a bound (~15 min) expires — then park once. Keep fail-closed for
   WRITES; this changes only what an unreadable READ means to the gate.
3. **Observability: journal the refusal.** A bounded (`_short_repr`-style) journal record when
   gh calls fail with rate-limit/HTTP errors — last night produced ZERO log evidence of 41
   failed calls, which cost the diagnosis a wrong first hypothesis. Include enough (status,
   which call) to attribute quota burn later.
4. Optional (weigh it, don't gold-plate): a short grace window (2–3 ticks) before the FIRST
   "no PR" park-notify, since `_refresh_finishing_prs` usually wins within one tick. Largely
   subsumed by guard 2 if implemented; keep any wait bounded.
   (Superseded item from the first draft: "settle local state even when GitHub writes fail" —
   still correct, now folded into guard 1's durable marker.)

## 4. Explicitly NOT the fix

- Do not weaken the fail-to-William posture: real parks still text, once.
- No retry-forever suppression that could hide a genuinely stuck issue: if the park label move
  keeps failing past a bound (suggest ~10 min), that itself is ALERT-worthy — one more text,
  not zero and not twenty.
- No LLM anywhere in this path; it stays mechanical.

## 5. Definition of done for the fix session

- [ ] Fake-gh test: `set_labels` failing N ticks in a row with a "no PR" gate verdict produces
      EXACTLY ONE notify (and journal shows one park + retries, not N parks).
- [ ] Rate-limit test: fake-gh returns "refused" (rate-limit/403) for PR reads across N ticks
      after session_finished → gate HOLDS (zero parks, zero notifies), journal carries bounded
      refusal records; reads recover → clean merge; bound expires instead → ONE park + notify.
- [ ] Distinguishability test: fake-gh "answered empty" (branch truly has no PR) still parks
      (once) — refused ≠ empty is visible in the adapter's return contract, fail-closed intact.
- [ ] Recovery test: PR becomes visible after k failing ticks → clean merge, no further notify,
      marker cleared; a LATER genuine park on the same issue notifies again (guard is
      per-cause-episode, not forever).
- [ ] Real park (no PR anywhere, label move succeeds) still notifies exactly once — unchanged.
- [ ] Bounded-failure escalation: label move failing past the bound raises one ALERT+notify.
- [ ] Full suite green (682 baseline + new); Codex cross-review (free) verdict recorded,
      ≤2 rounds, prompt names the two proven defect classes (shared mutable defaults,
      fail-OPEN on wrong-typed input); republish/restart is the orchestrator's, not yours.
