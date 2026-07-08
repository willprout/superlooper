# Task 16 — live sandbox dry-run findings (2026-07-03)

First real-world run of the loop against real GitHub, real cmux tabs, real Opus worker sessions,
and real iMessage. Sandbox repo: `will-titan/superlooper-sandbox` (private, left intact).
Runner run from the installed copy; state home `~/.superlooper/will-titan__superlooper-sandbox/`.

## Verifiably proven live

- Operator path end-to-end: `install.sh` → `adopt` → config → `doctor` (all-green) → branch
  protection (`ci` required, strict off, force-push blocked).
- **Launch stack** — the keystroke-free shim self-launches a worker in a fresh cmux tab and
  delivery is verified; re-proven in isolation (a dropped `.cmd` self-ran).
- **iMessage** channel — William received the test text (osascript → Messages).
- **Full gate → merge** — issue #2 merged end-to-end: PR #4 with a fresh-agent
  `<!-- superlooper-review -->` comment, CI `ci` green, squash-merge, issue closed.
- **Wander detection** — #2's diff touched `test_app.py` (a `logic`-area file) while declaring only
  `style`; the gate journaled the wander and still merged (non-blocking, as designed).
- **Conflict ladder (initiation)** — #1 hit a real conflict (its `test_app.py` edit vs #2's merged
  wander); the runner marked PR #3 `superseded` (branch preserved, PR left open, **no force-push**)
  and cut a fresh `-r1` branch.
- **Fail-safe posture** — every failure parked safely and texted William; no half-merge, no lost
  work, no force-push, no auto-approval.

## Findings

| ID | Severity | Status |
|----|----------|--------|
| D1 | P1 | OPEN (workaround) — `gh` ops ignore `--repo` (use process cwd). Run superlooper from the target repo dir or export `GH_REPO`. Fix: set `GH_REPO` from config at CLI/runner startup. |
| D2 | LOW | DEFERRED (owner ruling) — iMessage to the owner's own number lands but doesn't push-notify (self-message). Fix later via a distinct recipient/Apple ID. |
| D3 | P1 | FIXED (commit 744cd5a) — gate false-parked completed work read against a ≤90s-stale PR snapshot. `_refresh_finishing_prs` freshens a finished issue's PR before the gate acts. Tested + fresh-agent reviewed (found+fixed a P0 in the first cut). Proven live. |
| D4 | P1 | FIXED (commit 5112a30) — relaunch/regenerate blocked by the finished-but-ALIVE worker's singleton lock (real claude idles at the prompt, never releasing it). `_close_stale_session` closes the stale pane + frees the lock before relaunch, wired into `_exec_launch` and `_exec_resolve_conflict`. Tested + fresh-agent reviewed (SHIP). Close mechanism proven live (freed the lock, closed the tab). |
| D5 | UNISOLATED | OPEN — after D4 freed the lock, #1's `-r1` relaunch still failed delivery. The shim works in isolation; #1 is a mangled instance after ~6 recovery cycles. Could not tell a real close→relaunch race from corrupted #1 state. Needs a clean-slate repro. |
| D6 | P1 | FIXED (offline; awaiting live re-run) — the review-evidence gate false-parks completed, properly-reviewed work when the `<!-- superlooper-review -->` marker comment lands AFTER the poll first cached the PR. `_refresh_finishing_prs` (the D3 fix) freshens a MISSING PR but never re-fetches comments for an already-cached PR, so a review comment posted in the window before the next 90s poll is invisible; the gate nudges then parks within that stale window. Fix: the finishing refresh now re-fetches comments for a finished, still-OPEN PR that has no review evidence yet (bounded — self-terminates when the marker lands or the PR leaves OPEN). |

**D5 — 2026-07-04 update:** clean-slate repro (`bin/repro-d5.sh`, 5 close→relaunch cycles on
real cmux from a fresh state) delivered 5/5. Two of #1's four post-fix "delivery not verified"
lines predate the D4 fix (they ARE D4); the residual is #1's mangled state, NOT a live
close→relaunch race. D5 downgraded to **characterized, not a standing defect** — the race the
mystery feared does not reproduce from clean state.

**D6 — live 2026-07-04, the headline catch of the dry run.** Proof (same code, same worker,
opposite outcome, decided by timing alone):

| issue | PR created | review marker posted | gate verdict (same tick, 17:22:39Z) |
|---|---|---|---|
| #10 shout | 17:22:08Z | 17:22:14Z (+6s) | **merged** |
| #9 farewell | 17:21:46Z | 17:21:54Z (+8s) | **parked** — "no review-marker comment" |

#9's marker was on the PR 45s before the gate ran, yet the gate parked on a stale empty-comment
cache; the nudge→park spacing was a single tick (16s), so the worker's nudge-response comment
(17:23:02Z) landed 23s after the park. Same family as D3, incomplete fix. Secondary observation
(NOT fixed — owner's call): even with fresh comments, the one-nudge-then-next-tick-park spacing
gives a genuinely-unreviewed worker no real chance to respond to the nudge; left as-is (bounded,
fail-to-William is safe) pending William's ruling.

**Operator finding (2026-07-04):** starting `superlooper run` WITHOUT a pane
(`--pane`/`SL_PANE`) prints one easy-to-miss warning, then every launch aborts and the issue
burns its retry cap and parks. Worse, a parked-on-launch-failure issue cannot be un-stuck by
re-approval alone: the local `launch_failures` counter (at cap) persists across a re-added
`agent-ready` label and keeps the issue filtered from fresh launches forever. Recovery in the
live run was to file fresh issues (#9/#10). Both worth a product follow-up (louder no-pane
guard; reset `launch_failures` on re-approval / on the parked→agent-ready transition).

Full detail (mechanism, why the offline sim missed each, repro) lives in the session's DEFECTS
notes; D3/D4 rationale is in their commit messages.

## Acceptance items — status (as of the 2026-07-03 partial run)

- [x] Clean merge through the full gate — #2 (one).
- [~] Conflict-regeneration — ladder INITIATED (superseded + `-r1`), not driven to a completed
  regen-merge (blocked first by D4, then by the D5 residual on the mangled #1).
- [ ] Second clean merge — not reached (#1 mangled).
- [ ] blocked → answerer round-trip — not exercised.
- [ ] Morning report render — not exercised (a morning report DID auto-render on first tick and
  texted William, but the full "renders overnight activity" acceptance wasn't driven).
- [ ] Runner kill-9 + clean reclaim — not exercised.
- [ ] Display-off launch — not exercised.

## Acceptance items — 2026-07-04 continuation (fixed runner, VERSION 0d2000d)

Driven autonomously (William blanket-approved the remaining sandbox test issues, then stepped
away). Fresh issues on a clean state home; the mangled #1 was never resurrected.

- [x] **Second clean merge through the full gate** — #10 (shout) and #15 (warmer greeting), both
  squash-merged with real fresh-agent review evidence and green `ci`.
- [x] **Completed conflict-regeneration** — #15/#16 both rewrote `greet()` off the same `main`;
  #15 merged, #16's PR then genuinely conflicted → mechanical merge-update conflicted in the
  worktree → `regenerate` (PR superseded, branch preserved, no force-push) → rebuilt on
  `-r1` → **merged**. Final `main` carries #16's text (it rebuilt last). This also drove the D4
  same-id relaunch live (the `-r1` launch delivered first try) and re-proved **D6** (#15 merged
  clean through the very review gate that false-parked #9).
- [x] **Runner kill-9 + clean reclaim** — `kill -9` the runner mid-#16-rebuild: the worker (pid
  7094) survived untouched; the restarted runner reclaimed it with NO relaunch (worker singleton),
  and #16 built through to merge under the reclaimed runner. Zero double-launch in the journal.
- [x] **Morning report renders** — `reports/morning-2026-07-04.md`: 3 merged (cross-linked), the
  #16 regeneration in the conflict metric, parks with memos, freeze/queue state, texted William.
- [~] **blocked → answerer round-trip** — the MECHANISM is proven end-to-end: #7 blocked on its
  data-behavior decision, the runner hired an answerer (1 transient delivery flake, `rc=2`, then
  delivered on retry), and when the answerer produced nothing the **15-min timeout safely parked
  #7 to William** — the fail-safe firing exactly as designed. The answer itself was never produced
  because the **Fable answerer hit the account's monthly spend limit** (screen: "You've hit your
  monthly spend limit… keep using Fable 5 or switch models"). NOT a loop defect; needs William
  (raise the Fable limit, or switch the answerer model) — deliberately not worked around.
- [ ] **Display-off launch** — still requires William physically (turn the display off, then
  approve #8). The keystroke-free stack's delivery is otherwise proven: the D5 repro delivered
  5/5 to real cmux tabs with no keystrokes, and every launch today delivered.

### New findings from the 2026-07-04 run

- **D7 (operational, P2) — the runner must run in the SAME cmux workspace as its `SL_PANE`.**
  A `nohup`/detached runner fails every `new-surface` with "Broken pipe" (loses the cmux socket
  connection); a runner in a *different* workspace fails with "Pane not found" (`new-surface
  --pane` resolves within the caller's workspace). The working configuration is the design-
  intended one: the runner runs in a visible tab in the target pane's own workspace (this is why
  the original William-tab runner worked). Worth an explicit note in `runner-ops.md` and a louder
  startup check (fail hard if the configured pane is not resolvable from the runner's workspace).
- **Answerer launch had one transient delivery flake** (`rc=2`) then delivered on the next tick's
  re-hire — the retry path is the designed resilience, but answerer-launch delivery is not 100%
  first-try; note it.
- **Morning report double-lists an issue** that has both a park and a later merge in its history
  (#15/#16 appear under both Merged and Parked). Cosmetic; the merges are real (verified on
  `main`). Consider de-duping by the issue's CURRENT terminal state.
- The **operator no-pane** and **re-approval-doesn't-reset-`launch_failures`** findings from
  2026-07-03 both RE-BIT this run (each cost a burned issue pair). They are the highest-value
  operator-experience fixes to make before any unattended run.

## Why the offline sim (Task 15, 32 scenarios) missed D3 and D4

The sim is structurally blind to two live realities:
- **D3:** `fake-gh` creates the PR synchronously in the same tick, so the cached PR view was never
  stale relative to worker completion. Real GitHub + async worker timing exposes the race.
- **D4:** `fake-claude` EXITS after writing its report, releasing the singleton lock. Real
  interactive claude stays alive at the prompt, holding it — so relaunch deadlocks.

These are the class of bug Task 16 exists to catch; consider adding sim scenarios that model
(a) a PR that appears one tick AFTER the report, and (b) a worker whose process stays alive after
finishing.

## State left for inspection (nothing auto-deleted)

- Sandbox repo intact. Merged on `main`: #2, #4, #10, #15, #16 (greeting now "Good day, {name}!").
  Parked artifacts (honest records, left alone): #1 (old mangled), #5/#6/#13/#14 (no-pane +
  counter findings), #7 (answerer spend-limit park), #9 (D6 false-park; its PR #11 work is good).
- Old 2026-07-03 state home archived (not deleted):
  `~/.superlooper/will-titan__superlooper-sandbox.archive-20260703` (journal preserved inside).
- Runner STOPPED cleanly (SIGTERM, lock released) at end of the 2026-07-04 run. The #7 worker and
  a1 answerer sessions were left alive (design: nothing auto-closed). Restart with:
  `~/.claude/skills/superlooper/bin/superlooper run --repo ~/projects/superlooper-sandbox --pane <ws26-pane-uuid>`
  run IN a cmux tab in the pane's own workspace (see D7).
- `wave6-dryrun` branch commits (all **not merged to main** — orchestrator reconciles):
  744cd5a (D3), 5112a30 (D4), 1494973 (D1), e2fa0e1 (D3/D4 sim), 726577f+a287b84+e35b035 (D5 repro
  + review), 27d96ef+0d2000d (**D6 fix + boundedness**). Installed copy is at **0d2000d**.
- `bin/repro-d5.sh` added (D5 clean-slate harness). Sandbox config `affinity` set to `soft` in the
  local working copy only (needed so a same-line conflict pair co-schedules; not committed).

## Recommended next steps (updated 2026-07-04)

Done this cycle: D1 fixed+tested, D3/D4 modeled in the sim, D5 characterized (repro 5/5),
**D6 found+fixed+proven live**, and every acceptance box except the two below driven green.

Remaining, all needing William:
1. **blocked→answerer answer** — raise the Fable monthly spend limit OR switch the answerer model,
   then re-run #7 for a full happy-path round-trip (mechanism + fail-safe already proven).
2. **Display-off launch** — turn the display off/lock, approve #8, watch it deliver.
3. **Operator-experience fixes** (highest value before any unattended run): louder/failing no-pane
   guard; reset `launch_failures` on re-approval; D7 same-workspace pane check.
4. Reconcile `wave6-dryrun` (D1+D3/D4-sim+D5+D6) into main once reviewed.

### Original 2026-07-03 next steps (superseded, kept for the record)

1. Clean-slate repro to isolate **D5** (reset #1 fully or run a fresh conflict pair; watch a
   regenerate-relaunch on the fixed runner from clean state).
2. Fix **D1** (`GH_REPO` from config) — small, removes the run-from-repo-dir footgun.
3. Finish the un-exercised acceptance items on a clean run: 2nd clean merge, completed
   conflict-regen, blocked→answerer, kill-9 reclaim, display-off.
4. Reconcile `wave6-dryrun` (D3+D4) into main once D5 is understood.
