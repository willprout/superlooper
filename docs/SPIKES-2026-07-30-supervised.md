# Supervised herdr/tmux deciding re-run — 2026-07-30

Owner-present session. Do not commit this file.

## Verdict table

| Condition | Description | Verdict |
|---|---|---|
| ENV | Clean-room proof (zero CMUX_*/CLAUDE_CODE_*/ANTHROPIC_* vars; claude → ~/.local/bin → 2.1.220) | **FAIL as inherited** → owner ruled: proceed under allowlist scrub (§0.1); scrub proof CLEAN (§0.2) |
| PRE | Throwaway claude under scrub: Max banner (not API), transcript file written | **PASS** — §1 |
| A1 | `agent prompt` submit, no viewer | **FAIL (plain verb)** — first prompt never auto-submits headless, rc=0 false success; `--wait` on fresh+headless still untested (§3, §7, §8) |
| A2 | `agent prompt` submit, robot viewer attached | **PASS** — robot was VIEWING the pane; view is what mattered, not attachment (§4, §6, §7) |
| A3 | `agent prompt` submit, real window attached | **FAIL (plain verb)** — out-of-view panes never auto-submit, rc=0 false success; in-view or `--wait` submits (§5–§8) |
| B | Crash drill, daily mode: codeword → kill -9 → restart → auto-revive with owner window; memory verified from transcript | **PASS** — 6/6 sessions revived on owner attach, codeword recalled, one transcript across the crash; post-revive first prompt stalls honestly, chaser needed (§9) |
| C | Phantom-state demo: headless restart, herdr reports ready for nonexistent process; robot viewer → revive | **DEMONSTRATED** — owner ran the API-vs-pgrep pair himself; robot revive in 1s, 6/6 ids; plus new state-vs-action inconsistency (§10) |
| D | Sleep test: ticker + live sessions, owner sleeps Mac ~2 min, report survivors + tick gaps (tmux lab AND herdr) | **PASS** — real 5m46s sleep; 9/9 pids survived; one symmetric 332s tick gap on each substrate, resumed at DarkWake; post-sleep codeword recall intact (§11) |

## §0 Clean-room proof — FAIL (2026-07-30, first action of session)

Requirement: environment must show **zero** `CMUX_*` / `CLAUDE_CODE_*` / `ANTHROPIC_*` vars, and
`claude` must resolve to `~/.local/bin/claude` → versions/2.1.220. On failure: stop and report,
do not scrub and continue.

### Raw output — exact kickoff patterns (`env | grep -E '^(CMUX_|CLAUDE_CODE_|ANTHROPIC_)'`)

```
ANTHROPIC_API_KEY=sk-ant-api03-…[REDACTED in this file; full value seen live by owner]
CLAUDE_CODE_ENTRYPOINT=cli
CLAUDE_CODE_EXECPATH=/Users/willprout/.local/share/claude/versions/2.1.220
CLAUDE_CODE_SESSION_ID=2876a4ec-55ee-4796-93b3-a849683dd2c7
CLAUDE_CODE_CHILD_SESSION=1
```

### Raw output — broader case-insensitive sweep (`env | grep -iE 'cmux|claude|anthropic'`)

Additional hits beyond the above:

```
AI_AGENT=claude-code_2-1-220_agent
CLAUDECODE=1
CLAUDE_PID=38226
CLAUDE_EFFORT=xhigh
```

Total env vars: 34. Zero `CMUX_*` vars (that part passes).

### Binary check — PASS

```
command -v claude  → /Users/willprout/.local/bin/claude
ls -l              → lrwxr-xr-x … /Users/willprout/.local/bin/claude -> /Users/willprout/.local/share/claude/versions/2.1.220
claude --version   → 2.1.220 (Claude Code)
```

### Why it fails and why it matters

This session is itself a Claude Code session; every shell it spawns inherits Claude Code's own
markers (`CLAUDECODE=1`, `CLAUDE_CODE_CHILD_SESSION=1`, session id, execpath) **and a live
`ANTHROPIC_API_KEY`**. Two concrete confounds for the lab:

1. Any interactive `claude` launched from here can detect it is nested and may behave
   differently — the exact confound the clean-room requirement exists to remove.
2. With `ANTHROPIC_API_KEY` exported, spawned claude sessions would authenticate/bill via the
   API key, **not the subscription** — violating the session boundary "small claude runs on the
   subscription."

Per the kickoff: stopped and reported to owner. No scrubbing performed. Owner ruled — see §0.1.

## §0.1 Owner ruling (2026-07-30): proceed under empty-env allowlist scrub

Owner authorized proceeding with a scrub STRONGER than plain `env -u` removal:

1. Every lab spawn (herdr server, tmux panes, claude sessions) launches `env -i` style with an
   explicit allowlist ONLY: `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, and
   `PATH=/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`. Nothing else inherited.
   In-context `env` output logged in this file as per-spawn proof.
2. Before A1: one throwaway claude under the scrub must show the **Max subscription banner**
   (not API billing) and **write a transcript file**. Fallback if empty-env claude fails to
   start: explicit `env -u` removal of the full observed list (ANTHROPIC_API_KEY, CLAUDECODE,
   CLAUDE_PID, CLAUDE_EFFORT, AI_AGENT, every CLAUDE_CODE_*), recorded here, check repeated.
3. The API key must not be used anywhere, by anything.
4. Read-only: locate where ANTHROPIC_API_KEY is set in ~/.zsh* — owner edits it himself.

Canonical scrub prefix used for every lab spawn from here on:

```sh
env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" SHELL="$SHELL" TERM="xterm-256color" \
  PATH="/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" <command…>
```

## §0.2 Scrub proof — CLEAN

Raw output of `env` inside the scrub (full environment, 6 vars total):

```
HOME=/Users/willprout
LOGNAME=willprout
PATH=/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin
SHELL=/bin/zsh
TERM=xterm-256color
USER=willprout
```

Contraband grep inside the scrub (`env | grep -iE 'cmux|claude|anthropic|ai_agent'`): **CLEAN**.
`command -v claude` inside the scrub: `/Users/willprout/.local/bin/claude` (→ versions/2.1.220).

## §0.3 ANTHROPIC_API_KEY source located (read-only, owner ruling item 4)

`~/.zshrc` line 5: `export ANTHROPIC_API_KEY="sk-ant-api03-…"` (value redacted here).
No matches in ~/.zshenv (absent), ~/.zprofile, ~/.zlogin (absent), ~/.zlogout (absent).
Owner to edit personally before lab work begins.

Consequence noted for the lab even after the edit: tmux panes that start a login/interactive
zsh re-source ~/.zshrc and could re-inject profile exports. Mitigation regardless of the edit:
panes are launched with a direct command (`sh -c`, non-login, non-interactive — sources no zsh
files), and every claude pane logs its in-context env via `env > proof && exec claude …`.

## §0.4 Prior lab tooling check (see §2 for the rebuild)

Last night's herdr clone under the old session scratchpad is **gone**
(`/private/tmp/claude-501/-Users-willprout/*/scratchpad/tools/herdr` — no matches).
Will re-clone ogulcancelik/herdr and re-fetch the 0.7.5 release binary when the lab starts.

---

## §1 PRE — throwaway claude under the scrub: **PASS**

Isolated tmux server `-L hlab2` started via the scrub wrapper (`/private/tmp/hlab30/scrub`;
tmux invoked by absolute path `/opt/homebrew/bin/tmux` because homebrew is deliberately NOT on
the allowlisted PATH — the Test-3 PATH landmine, now by design). Server global env = the 6
allowlist vars + `PWD` (added by tmux itself). Contraband grep of server process env: CLEAN.

Throwaway: session `pre`, cwd `/private/tmp/hlab30/pre`, pre-assigned
`--session-id 5ddf8f39-7ec2-4936-967f-9eca52d71a9d`, pane command
`env > pane.env; exec claude --session-id …` (per-spawn in-context proof).

- **pane.env**: allowlist + tmux-added vars (`TMUX`, `TMUX_PANE`, `TERM=tmux-256color`,
  `TERM_PROGRAM`, `COLORTERM`, `SHLVL`, `PWD`, `OLDPWD`, `_`). Contraband grep: **CLEAN**.
- **S9 trust gate** appeared (first run in new cwd), accepted via send-keys Enter.
- **Banner**: `Fable 5 with xhigh effort · Claude Max · williamdebarany@gmail.com's Organization`
  → **subscription billing, NOT API**. **No** "Transcript saving is off" warning anywhere.
- **Prompt delivery** (variant A: `send-keys -l`, then Enter): submitted first try.
- **Transcript oracle** — file appeared at
  `~/.claude/projects/-private-tmp-claude-501-…-scratchpad-lab-pre/5ddf8f39-….jsonl`
  (note: claude resolved the `/private/tmp/hlab30` symlink to the real scratchpad path):

  ```
  2026-07-30T04:11:46.317Z 5ddf8f39 [user]      What is 46 plus 27? Reply with only the number.
  2026-07-30T04:11:48.658Z 5ddf8f39 [assistant] 46 + 27 = 73... but per your request: **73**
  ```

- `/exit` clean; only `sentinel` remains on hlab2.

Owner ruling item 4 follow-through, verified: with the key line commented out, a login+interactive
zsh started WITHOUT the key does not re-gain it (`env -u ANTHROPIC_API_KEY zsh -lic` → UNSET;
full-scrub zsh -lic → UNSET). Pre-edit terminals still carry it until closed/unset.

---

## §2 Lab setup for A/B/C (herdr)

| Item | Value |
| --- | --- |
| Lab root | `/private/tmp/hlab30` → symlink into session scratchpad `…/scratchpad/lab` |
| herdr source | clone at tag `v0.7.5` (`ef4c23f5`); geometry gate confirmed at `src/app/agent_resume.rs:79` (zero terminal area → no resume candidates) |
| herdr binary | official release `herdr-macos-aarch64` 0.7.5 via `gh release download`; reports `herdr 0.7.5` |
| herdr isolation | `XDG_CONFIG_HOME=/private/tmp/hlab30/hd`, `XDG_STATE_HOME=/private/tmp/hlab30/hd/state` — deliberate lab parameters on top of the allowlist (herdr reads XDG_CONFIG_HOME at config/io.rs:30). gh is NEVER run in this env (Test-3 XDG landmine) |
| Socket | `/private/tmp/hlab30/hd/herdr/herdr.sock` (33 chars via short symlink — sun_path safe; herdr does not canonicalize) |
| settings.json | backed up sha256 `c6ecfa30…` (= last night's restored hash). `integration install claude` semantic diff: ONLY SessionStart hook added, non-hook subtrees identical, alphabetical reordering as before. Hook script at `~/.claude/hooks/herdr-agent-state.sh` |
| Server | `herdr server` headless as a background process (NO tty, NO tmux), pid 99378. server.env proof: allowlist + XDG pair + sh-added vars; contraband CLEAN |
| Workspace | `w1` cwd `/private/tmp/hlab30/work`, root pane `w1:p1` |

**Finding en route (new):** headless `workspace create` DID spawn a real pane shell (server child
zsh pid 1446, default viewport 23–24 rows) with no client ever attached — fresh spawn is NOT
client-gated; the geometry gate bites restores (agent_resume), not creates. Also: herdr resolves
the pane cwd symlink, so hosted-claude transcripts land under the resolved
`…-scratchpad-lab-work` slug.

Work dir pre-trusted via a 3-second tmux throwaway (accept S9 dialog, `/exit`) so the A tests
measure prompt submission, not the trust gate.

---

## §3 A1 — `agent prompt`, NO viewer: **FAIL (first prompt), with a sharper lie than last night**

Agent: `labclaude`, fresh `agent start` into headless pane `w1:p1` (start itself succeeded in
3s with no client ever attached — argv `["claude"]`, session id `9ba8a129-cd6d-4fe8-b08a-c115c7f759f2`
captured by the SessionStart hook; real process verified: server 99378 → pane zsh 1446 →
claude 5993; claude-process env contraband: CLEAN; banner `Claude Max`, no transcript warning).

| Step | Command | herdr said | Reality (transcript oracle) |
| --- | --- | --- | --- |
| A1a | `agent prompt` (no --wait), FIRST prompt after start | `agent_prompted`, rc=0, returned same second | **NOT SUBMITTED** — text sat in composer; NO transcript file existed 65+s later; status stayed `idle`/`ready` |
| A1c | `agent send-keys labclaude enter` (chaser) | ok | **SUBMITTED** — transcript created, `58+16 → 74` |
| A1b | `agent prompt --wait --until working --until done`, 2nd prompt | `working`, rc=0 | **SUBMITTED** — `63+29 → 92` |
| A1d | `agent prompt` (no --wait), 3rd prompt | `agent_prompted`, rc=0 | **SUBMITTED** — `27+45 → 72` |

Transcript (`…-scratchpad-lab-work/9ba8a129-….jsonl`):

```
2026-07-30T04:19:52.770Z [user]      What is 58 plus 16? Reply with only the number.   <- A1a text, submitted only by A1c chaser
2026-07-30T04:19:55.967Z [assistant] 58 plus 16 is 74.
2026-07-30T04:20:16.086Z [user]      What is 63 plus 29? Reply with only the number.
2026-07-30T04:20:19.550Z [assistant] 92
2026-07-30T04:20:55.000Z [user]      What is 27 plus 45? Reply with only the number.
2026-07-30T04:20:57.112Z [assistant] 72
```

**Characterization (new tonight):** the failure is NOT "agent prompt never submits" — it is
**the FIRST prompt into a freshly started agent never submits**; all later prompts submit, with
or without `--wait`. And the confound theory is dead: this reproduced under the owner-ruled
empty-env allowlist scrub.

**Honesty regression vs last night:** last night `agent prompt` at least returned
`agent_prompt_stalled`. Tonight the plain (no `--wait`) verb returned success rc=0 in <1s for a
prompt that never submitted — an rc=0 non-delivery, the exact "operations that LIE" class of the
owner's reframing. (`--wait` was not tested on a first prompt yet — A2 covers first-prompt with
a viewer; a `--wait` first-prompt trial is worth adding if time allows.)

Minor: `agent send-keys` rejects `ctrl-c` as a key name; `esc` delivered ok but did not clear
the composer (composer text survived Esc — unverified whether that is claude UI behavior or
non-delivery; not pursued).

---

## §4 A2 — `agent prompt`, ROBOT viewer attached: **PASS (first prompt submits)**

Robot viewer = herdr's own client TUI run inside a scrubbed tmux pane on hlab2
(session `robot`, 200x50; verified rendering the workspace list + claude pane).

Fresh workspace `w2`, fresh `agent start labclaude2` (session id
`efb84639-ff27-40cb-818e-e50529fc5d8e`), then the FIRST prompt, plain `agent prompt`
(no `--wait`) — the exact call shape that failed silently in A1:

```
22:22:24  agent prompt labclaude2 'What is 36 plus 48? …'  → agent_prompted rc=0
transcript efb84639-…jsonl:  [user] What is 36 plus 48? …  →  [assistant] 84
```

Submitted immediately; pane rendered at full viewer width (~173 cols vs 51 headless).

**A1×A2 conclusion:** the first-prompt non-submission is **viewer-gated**. Identical command
sequence, identical scrub; the only variable is an attached client. Any attached client —
including herdr's own TUI in a robot tmux pane — is sufficient to make `agent prompt` deliver
its submission keystroke on a fresh agent. Timing is exonerated: A1's failed first prompt came
LATER after `agent start` (~40s) than A2's successful one (~10s).

---

## §5 A3 — `agent prompt`, REAL window attached: **FAIL (first prompt), owner-witnessed**

Robot viewer detached first; owner's terminal running `/private/tmp/hlab30/hcli` was the ONLY
attached client. Fresh workspace `w3`, fresh `agent start labclaude3` (session
`a0d1d434-b6d0-4857-b10b-2e44e8f27395`), then FIRST `agent prompt` (no `--wait`) at 22:27:51.

- herdr: `agent_prompted` rc=0, instant — **false success again**.
- Reality: text sat in the composer unsubmitted; NO transcript file for 4+ minutes; owner
  personally saw the stuck prompt after clicking into lab3.
- **Owner testimony (the load-bearing fact):** his client was attached but he was NOT viewing
  workspace 3 when the prompt fired — "I saw the agent get created but didn't click into it."
  The w3 pane still rendered at his client's ~91-col geometry, so geometry was applied even
  though his view was elsewhere.
- Enter chaser at 22:32:15 (owner watching): submitted in 4s.

```
2026-07-30T04:32:15.250Z [user]      What is 52 plus 37? Reply with only the number.
2026-07-30T04:32:17.269Z [assistant] 57
```

(Yes — claude answered 52+37 with **57**, on screen and in the transcript. Wrong arithmetic
(89), recorded verbatim; irrelevant to the submission mechanics under test, but a good reminder
the transcript oracle checks for AN answer, not a correct one.)

**Revised model after A1/A2/A3:** the first-prompt gate is not "any client attached" — A3 had a
real client attached and still failed. Best-fit hypothesis: the target pane must be RENDERED IN
A CLIENT'S CURRENT VIEW at first-prompt time. A2's robot viewer (never manually navigated)
auto-followed server focus to the new workspace and displayed the pane → PASS; the owner's
client, manually navigated earlier, stayed on its selected workspace → w3 attached-but-not-viewed
→ FAIL. Discriminating trial A3c next: owner clicks into the new workspace BEFORE the fresh
start + first prompt.

---

## §6 A3c — deciding trial: owner VIEWING the pane before first prompt: **SUBMITS**

Owner clicked into lab4 BEFORE `agent start labclaude4` (session
`74dcdb1d-bc8c-4bda-895f-3fcb0ee875a0`) and the first `agent prompt` (22:33:51, no `--wait`):

```
2026-07-30T04:33:51.125Z [user]      What is 64 plus 18? Reply with only the number.
2026-07-30T04:33:53.235Z [assistant] 64 plus 18 is **82**.
```

Submitted at t=+1s, owner-witnessed live.

### A-condition verdict (the settled version)

herdr `agent prompt`'s FIRST submission into a fresh agent succeeds **iff the target pane is
currently rendered in an attached client's view**:

| Trial | Client attached | Pane in view | First prompt submitted |
| --- | --- | --- | --- |
| A1 | none | no | NO — and rc=0 false success |
| A2 | robot tmux client | yes (auto-followed focus) | yes |
| A3 | owner's real window | no (owner viewing another workspace) | NO — rc=0 false success |
| A3c | owner's real window | yes | yes (+1s) |

Consequences for adoption: (1) a robot viewer works as a workaround ONLY if it displays the
target workspace — a viewer parked elsewhere is as useless as no viewer; (2) the plain verb's
rc=0-on-non-delivery makes it untrustworthy without an independent transcript-side delivery
check; (3) after the first successful exchange, subsequent prompts submit even fully headless
(A1b/A1d ran with zero clients attached).

---

## §7 CORRECTION to §3/§6 — the gate is not "first prompt": full event timeline and model v3

While teaching the drill-B codeword, a FOURTH prompt to `labclaude` (3 successful exchanges
already) failed to auto-submit — so "only the first prompt fails" (§3) is wrong. A follow-up
probe on `labclaude2` (also seasoned) reproduced it. Full timeline of every `agent prompt`
issued tonight:

| # | Agent (prior exchanges) | Clients attached | Pane in a client's view | Auto-submitted |
| --- | --- | --- | --- | --- |
| 1 | labclaude (0) | none | — | **NO** (rc=0 "success") |
| 2 | labclaude (1, post-chaser) | none | — | yes |
| 3 | labclaude (2) | none | — | yes |
| 4 | labclaude2 (0) | robot | yes | yes |
| 5 | labclaude3 (0) | owner | no | **NO** (rc=0 "success") |
| 6 | labclaude4 (0) | owner | yes | yes (+1s) |
| 7 | labclaude (3) — codeword teach | owner | no | **NO** (rc=0 "success") |
| 8 | labclaude2 (2) | owner | no | **NO** (rc=0 "success") |

**Model v3 (fits all 8 events):** `agent prompt` auto-submits iff the target pane is currently
rendered in an attached client's view, OR no client is attached at all AND the pane has at
least one completed exchange. The residual oddity is event 1 vs 2–3: fully headless, a
never-used pane drops the submission but a seasoned one doesn't. Attached-but-out-of-view
drops it ALWAYS (events 5, 7, 8 — including seasoned panes).

Constant across all failures: the text always lands in the composer; the plain verb always
returns `agent_prompted` rc=0 within a second (false green); status stays `idle`/`ready`; and
the `agent send-keys <name> enter` chaser submitted **4/4** times (events 1, 5, 7, 8).

Codeword for drill B taught and acknowledged (via chaser), verified in transcript:

```
2026-07-30T04:36:27.028Z [user]      Remember this exact codeword for later: IBEX-6407-QUEBEC. Reply with only t…
2026-07-30T04:36:31.054Z [assistant] IBEX-6407-QUEBEC
```

---

## §8 Owner's counter-experiment, and the settled model (v4): the variable is `--wait`

Owner installed the herdr skill in a fresh claude session INSIDE herdr (w4) and had it run a
background-tab test: `tab create --no-focus` → `agent start bgmath` (w4:p3, session
`af92087c-59f7-…`) → `agent prompt bgmath "what is 46+83 reply only with a number" --wait
--timeout 120000` → reported success with `agent_status: done`.

**Verified from the jsonl transcript (not the screen): the submission claim is REAL.**

```
2026-07-30T04:49:43.012Z [user]      what is 46+83 reply only with a number
2026-07-30T04:49:45.261Z [assistant] 326
```

But that test changed TWO variables at once vs tonight's failures (`--wait`, and
tab-in-focused-workspace topology). Single-variable discriminating trials:

| Trial | Pane | Topology | Verb | Result |
| --- | --- | --- | --- | --- |
| E11 | labclaude2 (seasoned) | out-of-view WORKSPACE (failed 22:37 with plain verb) | `--wait` | **SUBMITTED** in 3s, `done`, answer 49 ✓ |
| E10 | bgmath (seasoned) | unfocused TAB, focused workspace (succeeded 04:49 with --wait) | plain | **NOT SUBMITTED** — text in composer, rc=0 false success |

**Model v4 (fits all 11 events, both sessions'):**

- `agent prompt` **WITH `--wait`**: submitted in every condition tested tonight — out-of-view
  workspace, unfocused tab, headless-seasoned (A1b).
- `agent prompt` **WITHOUT `--wait`**: submits ONLY when the pane is rendered in an attached
  client's current view (plus the headless-seasoned oddity, events 2–3); in every other
  condition it types the text, drops the submission, and returns `agent_prompted` rc=0 —
  a false green, 6/6 failures tonight.
- `agent send-keys <name> enter` chaser: 4/4.

The owner's session attributed its success to being inside herdr / having the skill; both are
wrong (same CLI verbs, same socket; the wrapper proves the env is identical). Its walkthrough
also mis-verified: it read the SCREEN and accepted **326 as the answer to 46+83** (=129)
without noticing. The transcript confirms claude genuinely answered 326 — the SECOND wrong
trivial-arithmetic answer tonight (52+37→57 in §5), both with intact prompts in the transcript.
Methodological consequence: delivery oracles must check that an assistant reply EXISTS, never
grep for the expected number (a flub would read as a delivery failure), and never trust a
narrated screen-read.

**Untested cell that matters for superlooper:** fresh agent + `--wait` + ZERO clients attached
(the actual deployment shape; last night's 2/2 `agent_prompt_stalled` suggests this may fail
honestly rather than silently). To be folded into condition C if practical.

Adoption read-through: herdr prompt delivery is workable IFF every prompt uses `--wait` and is
paired with an independent transcript-side delivery check; the plain verb is disqualified by
rc=0 non-delivery. (bgmath's stray E10 composer text chased+submitted after the trial to leave
clean state.)

### §8.1 Corroboration from the herdr skill's own documentation (owner-relayed, verbatim quotes)

The herdr skill's "Start and coordinate an agent" section, quoted by the owner's in-herdr session:

> `herdr agent prompt reviewer "…" --wait --timeout 120000`
> agent prompt atomically submits text and encoded Enter while honoring the pane's live
> bracketed-paste mode. For normal agent work, --wait is enough: it waits for the first settled
> idle, done, or blocked state. Do not repeat those defaults with --until.
> A prompt sent from a non-working state must produce an observed lifecycle change within five
> seconds. Otherwise Herdr returns agent_prompt_stalled instead of waiting indefinitely.

Three consequences for the record:

1. `--wait` is the CANONICAL prompt form; the plain form's post-submit behavior is undocumented.
   Model v4's practical rule ("always --wait") is also herdr's own prescription.
2. Last night's 2/2 `agent_prompt_stalled` errors match the documented 5s lifecycle check —
   i.e., last night ran the honest path and correctly reported non-delivery; tonight's plain-verb
   rc=0 false greens are the no-observation path. Same delivery flakiness, two honesty levels.
3. Mechanism hypothesis (unproven, best fit): "honoring the pane's live bracketed-paste mode" —
   if herdr's model of an UNWATCHED pane's paste mode is stale, the encoded Enter lands as a
   literal newline in the composer instead of a submit. Fits the view-dependence exactly.
   Upstream-issue material.

Owner's session also explained the two wrong arithmetic answers (57, 326): terse
"reply with only the number" prompts elicit no-reasoning answers, which flub math. Plausible;
delivery oracles must remain reply-exists checks, not correctness checks. Option-2 independent
replication was offered and skipped as redundant after E10/E11.

---

## §9 B — crash drill, daily mode: **PASS, with the two asterisks now precisely located**

Timeline (owner watching throughout):

1. **Pre-kill state snapshotted** (23:15:04): session.json held all six agent session ids
   (9ba8a129, a0d1d434, af92087c, d5e3b50f, efb84639, faa6e820); server 99378 had six pane
   shells and six live claude processes.
2. **kill -9 99378** at 23:15:15 → whole tree died: server DEAD, 0/6 pane shells, 0/6 claudes.
   Stale sockets left (cleaned by next start), owner's client died with it.
3. **Headless restart** (pid 66477, server2.env contraband CLEAN):
   `persist.restore … outcome="ok" workspaces=4` with **zero** pane.spawn events after it.
   **Phantom state held for ~4 minutes pre-attach**: API reported all six agents
   `idle`, five `interactive_ready: True`, while `ps` showed ZERO claude processes and the
   server had no children. (Live capture of the §C phenomenon, en route.)
4. **Owner reattached** (~23:19) → **auto-revive fired at 23:19:06 for ALL SIX panes**, each
   with the exact bookkeeping: `claude --resume <its pre-crash session id>` (all six pids +
   argvs captured; ids match the session.json snapshot 6/6).
5. **Recall question** via canonical `agent prompt --wait`: **`agent_prompt_stalled`** —
   "no observed state change within 5000 ms; status is idle and state_change_seq remained 0".
   This is LAST NIGHT'S EXACT ERROR, reproduced for the first time tonight, on a freshly
   REVIVED pane — strong evidence last night's 2/2 stalls were post-revive first prompts.
   Text had landed in the composer; Enter chaser submitted it in 2s (chaser now 5/5).
6. **Memory verified FROM THE TRANSCRIPT** — one file, one sessionId, crash in the middle:

```
2026-07-30T04:36:27.028Z [user]      Remember this exact codeword for later: IBEX-6407-QUEBEC…
2026-07-30T04:36:31.054Z [assistant] IBEX-6407-QUEBEC
        <-- kill -9 (05:15:15Z), headless restart, 4-min phantom, owner attach, auto-revive -->
2026-07-30T05:21:28.605Z [user]      What exact codeword did I ask you to remember earlier?…
2026-07-30T05:21:42.064Z [assistant] IBEX-6407-QUEBEC
distinct sessionIds in file: {'9ba8a129-cd6d-4fe8-b08a-c115c7f759f2'}
```

**Verdict: PASS for daily mode** — with a human window in the loop, crash recovery is real,
complete (6/6 sessions), and memory-intact. Asterisks: (a) revive waited ~4 min as a phantom
until the owner attached (headless mode gets NO revive — condition C demos this); (b) the
first prompt into a revived pane stalls honestly even with `--wait` and needs a send-keys
enter chaser — the launch/recovery path must treat post-revive first prompts as a two-step.

---

## §10 C — phantom-state demo + robot revive: **DEMONSTRATED, owner-witnessed**

1. kill -9 (23:29:31, server 66477) → tree dead. Headless restart (server3, pid 6978,
   env proof CLEAN): `persist.restore outcome="ok" workspaces=4`, zero spawn events after.
2. **Owner personally ran the two commands from his own terminal during the phantom:**
   - `hcli agent list` → six agents, `"agent_status":"idle"`, `"interactive_ready":true` on
     five (raw JSON pasted into session log by owner).
   - `pgrep -fl "claude --resume"` → empty. Zero claude processes on the machine.
   The hollow-tab class, witnessed first-hand: ready agents with no processes behind them.
3. **NEW inconsistency found:** during the phantom, `agent prompt labclaude2` (and labclaude3)
   returned `agent_not_found` — at 23:30:32, seconds after `agent list` returned those exact
   names as idle/ready (and again at 60s+, so not a rehydration race). herdr's STATE surface
   (list: exists, ready) and ACTION surface (prompt: not found) disagree about the same agent.
   Action-side honesty by accident; state-side lie unchanged.
4. **Robot revive:** tmux session `robot2` running herdr's own client TUI attached at 23:32:31
   — revive fired at **23:32:32** (1s), all six panes: `claude --resume <exact pre-crash id>`,
   6/6 ids correct, no human window involved. A robot viewer is a sufficient and instant
   revive trigger — but per §6/§7, note it only helps prompt-delivery for the workspace it
   is actually displaying.

Owner's B-question (was he viewing lab at the 23:20:52 stall?) remains unconfirmed; the
"revived panes may stall even in-view" nuance stays open.

---

## §11 D — sleep test: **PASS, both substrates, symmetric and clean**

Setup: 5s tickers on BOTH substrates (tmux session `ticker` on hlab2; herdr `pane run` in new
workspace `sleeplab` w5:p1) + live sessions (tmux: sentinel, robot2 viewer; herdr: server +
six revived claudes). Pre-sleep snapshot 23:34:09 in `logs/pre-sleep-snapshot.txt`.

Owner slept the Mac via Apple menu. Ground truth (`pmset -g log`):
`Entering Sleep … 'Software Sleep' 23:37:10` → DarkWake 23:42:41 → FullWake (HID) 23:42:56 —
a real ~5m46s sleep, longer than planned (better).

**Survivors: 9/9 pids identical across the sleep** — tmux server 86000, herdr server 6978,
robot client 14847, all six claudes 14941–14946. All three tmux sessions intact.

**Tick gaps (the whole story in two lines):**

```
tmux : 59 ticks/619s — one gap: 332s, last tick 23:37:09 → next 23:42:41
herdr: 58 ticks/614s — one gap: 332s, last tick 23:37:11 → next 23:42:43
```

Both substrates froze for exactly the sleep window and resumed AT DARKWAKE (before FullWake),
in lockstep, with zero deaths, restarts, or divergence. First LIVE data point on the
wake-crash lore: acquittal. (Scope: one ~6-min Apple-menu sleep on AC; not a lid-close, not
battery, not hours-long.)

**Post-sleep liveness+memory probe:** `agent prompt --wait` on labclaude STALLED again
(honest `agent_prompt_stalled`) — consistent with the established pattern, as this was the
first prompt since revive #2; robot2's displayed workspace at stall time not conclusively
determined, so the in-view nuance for revived panes stays open. Enter chaser (6/6 now)
submitted; transcript:

```
2026-07-30T05:45:01.218Z [user]      One more time: what is the codeword? Reply with only the codeword.
2026-07-30T05:45:04.012Z [assistant] IBEX-6407-QUEBEC
```

One session id, one transcript, spanning: teach → kill -9 → owner-triggered revive → recall →
kill -9 → robot-triggered revive → 5m46s system sleep → recall. The resume primitive plus
herdr's bookkeeping carried memory through all of it.

---

## §12 Cleanup and hygiene (verified, not asserted)

| Item | State |
| --- | --- |
| `~/.claude/settings.json` | **restored byte-identical**: sha256 `c6ecfa30…` = pre-lab backup, `diff -q` clean, 1927 bytes (same hash as last night's restoration) |
| `~/.claude/hooks/` | herdr hook removed by `integration uninstall`; only `suggest-cross-review.sh` remains |
| herdr | graceful `server stop`, then verified: **zero** processes matching herdr; sockets removed by the stop |
| tmux `-L hlab2` | `kill-server` — "no server running", pid 86000 dead |
| Lab claude sessions | all dead (0 remaining) |
| Live superlooper runner workers (a2, a3, i258, i225 — pids 23680/60827/48611/4746) | **never touched**; identified and excluded from every kill |
| `~/Projects/superlooper` | untouched except this results file (uncommitted, per kickoff) |
| Lab artifacts | scratchpad `lab/` via symlink `/private/tmp/hlab30` (env proofs, tick logs, snapshots, settings diff, backups); disposable, delete freely |
| Owner's ~/.zshrc | edited by OWNER only (line 5 commented); verified clean-start shells don't re-gain the key |

## Open items

1. **Owner never confirmed** whether he was viewing lab at the 23:20:52 stall — the
   "revived panes stall even in-view" nuance is plausible (2 stalls, both post-revive,
   viewer attached both times) but not proven.
2. Fresh pane + `--wait` + zero clients: still not directly tested (robot2 was attached
   during the post-revive stalls; a truly clientless --wait-on-fresh-pane run remains open —
   low value now that the stall/chaser behavior is characterized, but open).
3. Upstream-issue candidates for ogulcancelik/herdr: (a) plain `agent prompt` returns rc=0
   for undelivered submissions; (b) first prompt into fresh-unviewed/revived panes drops the
   submission keystroke (bracketed-paste-state hypothesis, §8.1); (c) phantom state:
   list/pane/workspace surfaces report idle+ready for processless panes while `agent prompt`
   says `agent_not_found` for the same names; (d) revive remains client-gated (robot client
   suffices, 1s).
4. Sleep scope: one 5m46s Apple-menu sleep on AC. Lid-close, battery, and hours-long sleeps
   untested.
