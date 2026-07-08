# INCIDENT 2026-07-07 — binary file in `reports/` silently wedges every runner tick

**For a fix session in THIS repo (superlooper).** Findings verified live during the
command-center build run; two read-only investigation agents + direct observation. The loop
was stalled ~42 minutes and the owner was never notified. Mitigations at the command-center
level are already applied (see §5); the engine fixes below are NOT yet built. This is
engine surgery: full house discipline (TDD, cross-review, ≤2 rounds).

## 1. What happened (timeline, Mac-local)

- 13:41 — worker i6 launches (command-center repo, state home
  `~/.superlooper/will-titan__command-center/`).
- 14:08 — i6 runs `pkill -f "bin/command-center"` to reap its own backgrounded evidence
  server; the pattern also matches William's live dashboard → collateral SIGTERM (separate
  finding, §4; already mitigated repo-side).
- 14:21 — i6 writes three PNG screenshots loose into `<state_home>/reports/`.
- 14:21:38 — first `tick_error`: `UnicodeDecodeError` reading a PNG as UTF-8. **Every tick
  thereafter crashes at the same line** (~15s apart, 130+ repeats): i6's finished report is
  never detected, the gate never runs (its PR #18 was OPEN/MERGEABLE/CI-green/review-posted
  the whole time), nothing launches, no notification fires.
- ~15:00 — owner moves PNGs to `reports/screenshots/` (subdir is safe: `IsADirectoryError`
  ⊂ `OSError` is caught). Finder immediately drops a `.DS_Store` into `reports/` — **another
  binary file, same wedge**, new error signature (`Bud1` bytes).
- 15:02 — `.DS_Store` removed; next tick: `session_finished i6` → `gate ok` → `merge PR 18`
  → `launch i7`. Full recovery, no restart needed.

## 2. Root cause (exact locations, installed skill = repo `skill/`)

`bin/runner.py` `tick()` → `disk_view(now)` (~line 575) → `self._scan_dir("reports")`
(~line 482) → `_read()` (~lines 86–91):

```python
def _read(path):
    try:
        with open(path) as f:      # text mode, default utf-8
            return f.read()
    except OSError:                # UnicodeDecodeError is NOT an OSError
        return None
```

`_scan_dir` reads EVERY entry in `reports/` as text. A binary file raises
`UnicodeDecodeError`, which escapes to `run()`'s outer guard (~285–289): journal a
`tick_error`, retry next tick — same rock forever.

## 3. The three systemic gaps this exposed (each is a fix, each traces to this incident)

1. **Binary-intolerant report scan.** `_read` must survive non-text files (catch
   `UnicodeDecodeError`, or open `errors="replace"`, or have `_scan_dir` skip non-`*.md`
   entries — pick one, test with a PNG and a `.DS_Store` fixture). Note macOS WILL put
   `.DS_Store` here whenever the owner browses the folder in Finder — "workers won't do
   that anymore" is not a fix.
2. **A crashing tick is silent.** The `tick_error` path (~287) only journals and retries —
   no counter, no ALERT, no notify. N consecutive tick_errors (suggest 4 ≈ one minute)
   should raise the standard ALERT + notify path (William got zero pings during a 42-min
   stall; he found it by eye). Clear the counter on the first clean tick.
3. **The heartbeat lies.** `runner.heartbeat` is stamped at the TOP of `tick()` (~562–565),
   before the code that can crash — a fully wedged runner reads as perfectly alive. This
   defeats the dashboard's dead-man's switch (command-center #10) by design. Stamp the
   heartbeat at the END of a successful tick (or add a second `tick.ok` stamp and let the
   dashboard watch that one). Decide deliberately what "alive" should mean and write it down.

Bonus fix while in there: the `tick_error` journal record embedded the **entire PNG byte
repr** — the journal grew ~47 MB → 74 MB in ~40 minutes. Truncate `repr(e)` in both
`tick_error` and `poll_error` records (a few hundred chars is plenty).

## 4. The collateral-kill finding (repo-side, already mitigated, recorded for the ratchet)

Worker sessions share the machine with the owner's own processes. i6's ephemeral
`pkill -f "bin/command-center"` matched William's live dashboard. Mitigated in
command-center's CLAUDE.md (standing order: record `$!`, kill only your own PIDs) + comments
on open issues. **Optional engine-level consideration for the fix session:** the worker
brief template could carry a universal "never pkill by pattern" rule so every adopted repo
inherits it, not just command-center.

## 5. Already done (do not redo)

- PNGs relocated to `reports/screenshots/`; `.DS_Store` removed; runner confirmed resumed
  (journal: `gate ok` / `merge pr 18` / `launch i7`).
- command-center CLAUDE.md standing orders (screenshots location + pkill rule), commit
  `50e2806`; reconciliation comments on command-center issues #7–#12.

## 6. Cleanup owed (needs the runner STOPPED — do not do live)

The journal at `~/.superlooper/will-titan__command-center/journal.jsonl` (~74 MB) carries
130+ bloated `tick_error` lines. With the runner stopped (Ctrl-C is safe; nothing merges
while it's down): filter out only `"act": "tick_error"` lines with `UnicodeDecodeError`
payloads into an archive file kept beside it (append-only audit record — archive, don't
delete), write the filtered journal atomically, restart the runner. Verify line counts
before/after and that the first/last non-error records survive intact.

## 7. Definition of done for the fix session

- [x] A PNG and a `.DS_Store` dropped into a fixture state home's `reports/` do not raise,
      do not block event detection, and are visibly skipped (unit tests for `_scan_dir`/`_read`).
- [x] N consecutive tick crashes raise ALERT + notify exactly once (fake-clock test); counter
      resets on a clean tick.
- [x] A wedged tick no longer produces a fresh heartbeat (test: heartbeat age grows while
      tick raises) — or the documented alternative stamp exists and is tested. *(Chose to MOVE
      the stamp to end-of-successful-tick; rationale in §9.)*
- [x] `tick_error`/`poll_error` journal records are bounded in size (test with a huge error).
- [x] Full suite green; cross-review (Codex) verdict posted; ≤2 rounds (one round; NEEDS
      REVISION → both findings fixed, see §9).
- [ ] Republish via `bin/install.sh` and restart the live runner — **orchestrator's**, not this
      session's (owner coordination; a stop is safe).

## 8. Adjacent finding (same day, different fault line — optional scope for this session)

**Owner comments posted after approval are invisible to workers.** The brief embeds the
issue BODY verbatim at launch; `templates/brief-footer.md` Step 0 says "Read the issue
above" — nothing fetches or mentions the live comment thread. Observed live: William
approved command-center #30, then commented concrete requirements (page cap, corner page
number, inactivity flap-back); the worker would have built without them. Orchestrator
mitigated by folding his words into the DoD with his yes (the sanctioned amendment path).
Candidate fix: brief includes issue comments at build time, and/or Step 0 adds "also read
the issue's comments on GitHub before starting." Small, but it closes a channel owners
will naturally use.

## 9. Resolution (fix session 2026-07-07, branch `worktree-binary-report-wedge`)

All four §3 fixes built test-first in this repo's `skill/` (the publishable payload); the
orchestrator owns republish (`bin/install.sh`) + live restart (§6, §7 last box).

**What shipped (`skill/bin/runner.py`):**
1. **Binary tolerance.** `_read` now catches `UnicodeDecodeError` (a `ValueError`, not the
   `OSError` it used to guard) → a binary file reads as "absent", never a crash. `_scan_dir`
   *additionally* skips dotfiles by name, so macOS metadata (`.DS_Store`, `._*`) is ignored
   regardless of byte content (a small `.DS_Store` can even decode as valid text).
2. **A crashing tick alerts.** `run()`'s guard counts consecutive tick crashes; at
   `TICK_ERROR_ALERT=4` (~1 min) it raises the standard ALERT + notify. It fires from `run()`,
   **not** `actions.decide` — a wedged tick never reaches the decide brain. Fires on `>=` the
   threshold with idempotence flags so a transient ALERT-write failure is retried (not lost)
   while notify fires exactly once; a clean tick resets the counter and re-arms.
3. **Honest heartbeat.** The `runner.heartbeat` stamp moved from the *top* of `tick()` to the
   *end of a successful tick*.
4. **Bounded journal records** (`_short_repr`) on `tick_error`/`poll_error`/executor-error —
   the PNG-in-the-repr bloat (§ "Bonus fix") can't recur.

**Heartbeat semantics decision (§3.3 asked to decide + write down).** Chose to MOVE the stamp
(not add a second `tick.ok`). Reason: `runner.heartbeat` is already the file the CLI status and
the command-center dead-man's switch read. Moving it makes that *existing* signal honest with
zero change in another repo; adding a second stamp would leave the lying one in place and force
a coordinated command-center edit (out of this session's scope). The two liveness signals now
mean different things on purpose: `state/runner.lock` (pidfile) = the PROCESS is up;
`state/runner.heartbeat` = the loop is making PROGRESS. A wedged-but-running loop now reads
stale, not healthy. Contract text updated in `references/runner-ops.md` and `launchd.runner.plist`.

**Cross-review (Codex, one round, free).** Verdict NEEDS REVISION → both real findings fixed:
- *Critical (fail-OPEN on wrong-typed input — a named repo defect class):* making `_read`
  binary-tolerant made a *present-but-binary* file indistinguishable from *absent*, so
  `_read_json` would read a binary `merges_frozen.json` as "not frozen" (fail open). Fixed:
  `_read_json` maps present-but-unreadable → `{}` (exists, fail closed), absent → `None`.
- *Medium:* the alert was one-shot at exactly count 4 — a transient ALERT-write miss there lost
  the alarm. Fixed by the `>=`-threshold retry described above.
Both got regression tests. No shared-mutable-default instances found.

**Evidence:** full pytest suite green (641 baseline + 11 new incident/review tests). Housekeeping
rider done: `.DS_Store` added to `.gitignore`. Adjacent finding §8 and the §4 pkill idea were
left as open items (not taken this session).
