# Preview-build retest of #2063 / #2064 — 2026-07-30 (daytime)

Do not commit this file. Companion to `SPIKES-2026-07-30-supervised.md` and
`SPIKES-2026-07-30-clientless.md`; methods reused unchanged (§0.1 allowlist scrub,
per-spawn env proofs, short socket paths, transcript-oracle verification).

Trigger: the maintainer (ogulcancelik) replied same-day on #2063 — a prompt-submission fix is
"currently available only on preview" — and asked us to retry the reproduction on the preview
channel. #2064 was labeled `pending-release` ("Implemented on master... not available in a
published Herdr release yet"). This lab determines what the current preview actually contains
and retests accordingly.

## Verdict table

| Retest | Recorded 0.7.5 behavior | Preview verdict |
|---|---|---|
| A. Plain `agent prompt` on unwatched panes | 6/6 silent drops, rc=0 false green | **FIXED** — 3/3 previously-dropping cells now deliver (transcript-verified); rc=0 still returns before submission, so it remains delivery-information-free, but the drop itself is gone (§4–§6) |
| A. `agent prompt --wait` | delivered everywhere tested | **UNCHANGED (correct)** — fresh clientless first prompt delivered in 3.1s, and now returns `agent_status: done` (richer than 0.7.5's `idle`) (§5) |
| B. Clientless crash-restore (#2064) | never revives without a client | **NOT-IN-PREVIEW** — fix commit is 11 commits ahead of the preview build commit; test skipped per plan (§2) |

## §1 Preview build identity and how it was obtained

Followed the maintainer's exact instructions (`herdr channel set preview` + `herdr update`),
scoped entirely to a lab directory — no global install, nothing outside the lab written:

- Lab root: `/private/tmp/hlab32` → symlink into this session's scratchpad (`…/scratchpad/lab32`).
- Started from the official stable `herdr-macos-aarch64` v0.7.5 release binary
  (`gh release download` into `/private/tmp/hlab32/bin/`), verified `herdr 0.7.5`.
- `hcli channel set preview` (hcli = scrub + `XDG_CONFIG_HOME=/private/tmp/hlab32/hd` +
  `XDG_STATE_HOME=/private/tmp/hlab32/hd/state` + lab binary) wrote
  `[update] channel = "preview"` to `/private/tmp/hlab32/hd/herdr/config.toml` and immediately
  self-updated **the lab binary in place**:

  ```
  Herdr update channel set to preview in /private/tmp/hlab32/hd/herdr/config.toml.
  checking preview channel for updates...
  downloading 0.7.5-preview.2026-07-29-44b3adb12552...
  installed 0.7.5-preview.2026-07-29-44b3adb12552
  ```

- `hcli --version` → **`herdr 0.7.5-preview.2026-07-29-44b3adb12552`**; explicit `hcli update`
  → `already up to date` (this IS the newest preview available on the channel).
- Build commit: **`44b3adb12552`** (master, 2026-07-29T13:24:23Z, "fix(windows): preserve
  parent agent ownership"); matches GitHub prerelease `preview-2026-07-29-44b3adb12552`.
- Containment proof (before install): `command -v herdr` → none; after install: still none;
  `~/.local/bin` unchanged; nothing herdr-named in `~/Library/Application Support`;
  `~/.claude/settings.json` sha256 `c6ecfa30…` / mtime `1785304539` unchanged throughout
  (integration never installed).

## §2 What the preview contains — the two fixes, by commit ancestry

GitHub compare API against the preview build commit `44b3adb12552`:

| Fix | Commit | Compare result | In preview? |
|---|---|---|---|
| Prompt submission ("fix: delay agent prompt submission", refs #1878 — the class #2063 was filed adjacent to; touches `src/app/api/agents.rs`, `src/pane.rs`, `src/terminal/runtime.rs`) | `bb29eedb7209` (2026-07-26T21:12Z) | `bb29eedb7209...44b3adb12552` → `status: ahead, behind_by: 0` | **YES** |
| Headless restore (#2064 fix, PR #2088 "fix: resume agents after headless restore", `Refs #2064`, merged 2026-07-30T16:15:00Z) | `ac47b9e67912` | `ac47b9e67912...44b3adb12552` → `status: behind, behind_by: 11` | **NO** |

The preview was built 2026-07-29T13:47Z; the #2064 fix merged to master ~27 hours later.
**Retest B skipped on this determination** — a crash-restore run against this preview would
retest the already-recorded 0.7.5 behavior, not the fix. Retest when a build at or past
`ac47b9e67912` ships on either channel.

## §3 Lab conditions (same invariants as the prior spikes)

- Scrub wrapper `/private/tmp/hlab32/scrub`: `env -i` + allowlist (HOME, USER, LOGNAME, SHELL,
  TERM=xterm-256color, PATH=/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin).
  In-scrub `env` = exactly 6 vars; contraband grep (cmux/claude/anthropic/ai_agent): **0**.
- Per-spawn env proofs captured and CLEAN for: PRE tmux pane, herdr server, robot client, and
  a `ps eww` sweep of the hosted claude process (a1, pid 32579): zero CMUX_*/CLAUDE_CODE_*/
  ANTHROPIC_* vars.
- Sockets: `/private/tmp/hlab32/hd/herdr/herdr.sock` + `herdr-client.sock` (short paths).
- PRE gate PASS: throwaway claude (session `11b45162-…`) under scrub in an isolated tmux
  (`-L hlab32`), trust gate accepted (pre-trusts the work cwd), banner
  `Fable 5 with xhigh effort · Claude Max · williamdebarany@gmail.com's Organization` (subscription,
  not API), no transcript-off warning; transcript oracle:

  ```
  2026-07-30T17:05:27.984Z [user]      Reply with only this codeword: PYLON-1187-ECHO
  2026-07-30T17:05:31.878Z [assistant] PYLON-1187-ECHO
  ```

  `/exit` clean; tmux server self-terminated before the herdr phase.
- Headless preview server pid 32345, started with no tty/tmux; claude pane cwd
  `/private/tmp/hlab32/work`; agent session ids pre-assigned so transcripts are attributable.
- Claude Code 2.1.220 throughout; all prompts one-codeword tiny.

## §4 Retest A, cell 1 — plain verb, FRESH pane, ZERO clients ever: **DELIVERED (was silent drop)**

0.7.5 recorded behavior (supervised A1a): rc=0 `agent_prompted` in <1s, text stranded in
composer, NO transcript file 65+s later.

Preview, same cell — fresh agent `a1` (session `94995d9f-…`), server never touched by any
client (`lsof -U | grep hlab32` at prompt time: exactly the server's two LISTENING sockets,
zero established connections):

```
2026-07-30T17:06:42Z  hcli agent prompt a1 'Reply with only this codeword: GARNET-4412-TANGO'
rc=0   wall 0.027s   result: agent_prompted, agent_status=idle, state_change_seq=1 (unchanged)
```

Transcript oracle (`…-scratchpad-lab32-work/94995d9f-….jsonl`):

```
2026-07-30T17:06:42.892Z [user]      Reply with only this codeword: GARNET-4412-TANGO
2026-07-30T17:06:45.021Z [assistant] GARNET-4412-TANGO
```

Submission landed ~0.5s AFTER the verb returned. `agent list` afterwards: seq 1→3.

## §5 Retest A, cell 2 — `--wait`, FRESH pane, ZERO clients ever (CW1 repro): **STILL CORRECT, now richer**

Fresh agent `a2` in w2 (session `f89f5b50-…`), still zero clients ever attached (lsof re-proof
immediately before the call):

```
2026-07-30T17:08:58Z  hcli agent prompt a2 'Reply with only this codeword: FJORD-9925-SIERRA' --wait --timeout 120000
rc=0   wall 3.142s   result: agent_prompted, agent_status=done, seq=6, title "✳ Provide codeword response"
```

```
2026-07-30T17:08:58.704Z [user]      Reply with only this codeword: FJORD-9925-SIERRA
2026-07-30T17:09:01.124Z [assistant] FJORD-9925-SIERRA
```

vs 0.7.5 CW1: 4.2s and returned `agent_status=idle`. Preview returns `done` — `--wait` now
also reports the settled state correctly on a clientless fresh pane.

## §6 Retest A, cells 3+4 — plain verb, attached client, pane OUT OF VIEW (the issue's exact repro): **DELIVERED, fresh and seasoned (was 100% drop)**

0.7.5 recorded behavior: attached-but-out-of-view dropped ALWAYS (supervised events 5, 7, 8 —
fresh and seasoned panes alike). This is the literal reproduction in #2063's own steps.

Setup: robot client (herdr's own TUI, scrubbed, in tmux `-L hlab32` session `robot`, 200x50)
attached; fresh workspace w3 + fresh agent `a3` (session `138f4fcf-…`); `workspace focus w1`
pinned the server focus (and the auto-following robot) to w1. View verified by content, not
markers: full robot capture-pane contained `GARNET-4412-TANGO` (= a1/w1's pane) and no other
codeword — a3's pane conclusively NOT rendered. (The sidebar ●/○ marks turned out to be
agent-status, not selection — capture content is the reliable oracle.)

Cell 3 — fresh a3, out of view:

```
2026-07-30T17:10:00Z  hcli agent prompt a3 'Reply with only this codeword: BASALT-3306-ROMEO'
rc=0   wall 0.028s
transcript: 2026-07-30T17:10:00.853Z [user] BASALT-…  →  17:10:03.225Z [assistant] BASALT-3306-ROMEO
```

Cell 4 — seasoned a3, still out of view (robot re-verified on w1):

```
2026-07-30T17:11:55Z  hcli agent prompt a3 'Reply with only this codeword: TUNDRA-6641-XRAY'
rc=0   wall 0.028s
transcript: 2026-07-30T17:11:56.284Z [user] TUNDRA-…  →  17:11:59.264Z [assistant] TUNDRA-6641-XRAY
```

## §7 Interpretation — what changed and what didn't

1. **The drop is fixed.** All three previously-dropping cells retested (fresh+clientless,
   fresh+out-of-view, seasoned+out-of-view) delivered on the preview, transcript-verified.
   Mechanism fits the fix commit's name ("delay agent prompt submission"): the submission
   keystroke now lands 0.4–0.9s after the verb returns instead of being lost against a stale
   pane state.
2. **The rc=0 semantics did NOT change.** The plain verb still returns `agent_prompted` rc=0 in
   ~28ms, before submission, with no state observation — exactly the maintainer's framing:
   "without `--wait`, success currently means the input was queued." It is now a truthful
   "queued", not a false "delivered". Delivery confirmation still requires `--wait` (or an
   independent transcript check).
3. **`--wait` remains the canonical form and got better** (`done` status on settle).

### Consequences for the two adoption workarounds

| Workaround | Status |
|---|---|
| Prompt delivery: ban plain verb, `--wait` + transcript-side delivery check + Enter chaser | **Shrinks, does not vanish.** The silent-drop premise is gone once a build ≥ `bb29eedb7209` is deployed; `--wait` stays as the canonical delivery-confirming form (herdr's own docs prescribe it). The Enter-chaser machinery for dropped submissions can be deleted for the tested cells. Caveat: post-revive first-prompt stalls were NOT retested (no crash test tonight) — keep the chaser for the revive path until retested. |
| Crash recovery: scripted viewer kept around to trigger clientless revive (#2064) | **Keep.** Fix exists on master (PR #2088) but is in NO published build, preview included. Delete only after a build ≥ `ac47b9e67912` ships and the clientless kill-9 drill passes. |

Scope honesty: the unfocused-tab-in-focused-workspace cell (supervised E10) was not retested;
untested here as low-risk given the mechanism fix covers the same path. Crash-restore, revive,
and post-revive prompting were not exercised at all tonight (deliberately — §2).

## §8 Teardown and hygiene (verified, not asserted)

| Item | State |
|---|---|
| herdr | graceful `server stop` rc=0; `pgrep -fl herdr-macos` empty; both sockets removed |
| tmux `-L hlab32` | killed; `no server running` |
| Lab claudes (11b45162 PRE, 94995d9f a1, f89f5b50 a2, 138f4fcf a3) | all dead (pgrep empty per id) |
| `~/.claude/settings.json` | never written: sha256 `c6ecfa30…` + mtime `1785304539` + 1927 bytes identical to pre-lab baseline; `~/.claude/hooks/` = only `suggest-cross-review.sh` |
| Global herdr | never existed: `command -v herdr` empty before and after; `~/.local/bin` unchanged; preview binary lives ONLY at `/private/tmp/hlab32/bin/herdr-macos-aarch64` |
| Live claude/cmux processes (13676, 14945, 68260, 69257, 69542 + cmux hooks) | never touched, all alive post-teardown |
| GitHub | reads only (issues, PR, commits, compare API, release download); **nothing posted** |
| Lab artifacts | `…/scratchpad/lab32/` via symlink `/private/tmp/hlab32` (env proofs, timed JSON results, server log, robot capture); disposable |
| Transcripts | 4 new jsonl files under `…-scratchpad-lab32-work` slug — claude's own records, left as evidence |

---

## §9 DRAFT reply comment for issue #2063 — **DRAFT ONLY, NOT POSTED**

> **This has NOT been posted to GitHub. Posting under the owner's identity requires his
> separate explicit approval. Text below is the proposed comment, verbatim.**

```markdown
Retested on preview as requested — `herdr channel set preview` + `herdr update` →
`0.7.5-preview.2026-07-29-44b3adb12552`.

The reproduction no longer reproduces. Plain `agent prompt` (no `--wait`) now submits in all
three conditions that dropped it on stable 0.7.5, verified against Claude's own transcript
files, one fresh agent per condition where noted:

- fresh pane, headless server, no client ever attached: submitted 0.5 s after the call
  returned; assistant replied 2 s later.
- fresh pane out of view (client attached, viewing another workspace — the repro steps from
  this issue): submitted 0.9 s after return.
- same pane again, seasoned, still out of view: submitted 0.4 s after return.

`--wait` also still works on a fresh clientless pane (3.1 s to `agent_status: done` — nice
that it reports `done` now; stable returned `idle` there).

One observation, not a complaint: the no-`--wait` form still returns `agent_prompted` / exit 0
in ~30 ms, before the submission actually lands. On this build that reads as a truthful
"queued" rather than the false "delivered" I filed about, and your comment already frames it
exactly that way — might be worth a line in the CLI docs so nobody reads exit 0 as delivery.
We'll keep using `--wait` for anything programmatic.

Also confirmed from commit ancestry that this preview predates the #2064 fix
(ac47b9e / #2088), so I haven't retested headless restore — happy to rerun that drill when a
build containing it ships on either channel.

Environment: macOS 26.2 (25C56), Apple Silicon; Claude Code 2.1.220; headless `herdr server`
driven via CLI; herdr's own TUI as the attached client for the out-of-view cells.
```
