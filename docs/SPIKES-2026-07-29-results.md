# Deciding spikes — results (2026-07-29)

Five spikes run overnight on William's Mac to settle the cmux-replacement / session-host question.
Every verdict below is backed by raw command output captured in the same run. Working directory for
all lab artifacts: session scratchpad (`/private/tmp/hlab` is a short symlink to it — see Test 1
note on `sun_path`). Nothing in `~/Projects/superlooper` was modified except this file.

**Environment of record**

| Fact | Value |
| --- | --- |
| Host | macOS 26.2 (25C56), arm64, Mac |
| claude binary used by every lab session | `/Users/willprout/.local/bin/claude` → `versions/2.1.220` (standalone, **not** cmux's shim) |
| herdr | v0.7.5 official release `herdr-macos-aarch64`, adhoc-signed; matches cloned source `e16d7d8` (Cargo.toml `version = "0.7.5"`) |
| tmux | 3.7b (homebrew), isolated server `-L sl-lab` |
| Live runner present on the machine | yes — superlooper runner pid 2503, worker i225. Untouched throughout. |

## Verdicts at a glance

| # | Spike | Verdict |
| --- | --- | --- |
| 1 | herdr crash drill (kill -9 → auto-revive with memory) | **PASS, client-gated** — revives and remembers, but only once a client attaches; reports `idle`/`ready` while no process exists |
| 2 | plain claude `--session-id` / `--resume` | **PASS, unqualified** — one stable id, one transcript, fact recalled verbatim |
| 3 | keychain probe from a `gui/$UID` LaunchAgent | **PASS** — `gh` read the keyring token, no prompt, no hang; PATH is the real gotcha |
| 4 | tmux type-into-claude (claude-squad recipe) | **PASS** — 3/3 delivery variants submitted; settle delay not required |
| 5 | overnight tmux soak | **PASS for long-idle** — 7h02m, 423/423 ticks, zero gaps, same server pid; idle claude survived and recalled its codeword. Sleep/wake still untested (machine never slept) |

**Headline for the decision:** the *recovery primitive* superlooper needs is not herdr's — it is
claude's own `--session-id` / `--resume`, which passed unqualified and belongs to no host at all
(Test 2). herdr's contribution on top is the bookkeeping (capture the id, re-launch on restart), and
that contribution is the part that proved client-gated and state-dishonest (Test 1). Meanwhile plain
tmux delivered prompts more reliably than herdr's own typed-prompt verb (Test 4 vs Test 1).

---

## Test 1 — herdr crash drill: **PASS, with a decisive caveat**

**Claim under test:** host an interactive claude session in a headless `herdr server`, teach it a
fact, `kill -9` the server, restart it, and confirm it auto-revives the session with the fact intact.

**Result:** it does revive, and the fact survives — but **the revive is gated on a client attaching.**
A purely headless server (the way superlooper would run it) leaves the pane a phantom indefinitely,
while its state API reports the agent as `idle` and `interactive_ready: true`.

### What happened, in order

1. **Integration install.** `herdr integration install claude` wrote
   `~/.claude/hooks/herdr-agent-state.sh` and added exactly one `SessionStart` hook to the global
   `~/.claude/settings.json`. Semantic diff of the whole file:

   ```
   top-level keys equal: True
   non-hook subtrees identical: True
   hook events before: ['PostToolUse', 'PreToolUse', 'Stop']
   hook events after : ['PostToolUse', 'PreToolUse', 'SessionStart', 'Stop']
   --- SessionStart CHANGED ---
     before: null
     after : [{"hooks": [{"command": "bash '/Users/willprout/.claude/hooks/herdr-agent-state.sh' session",
                          "timeout": 10, "type": "command"}], "matcher": "*"}]
   ```

   Nothing else changed semantically — superlooper's three hooks were preserved. **But** it rewrote
   the file with `serde_json::to_string_pretty` and no `preserve_order` feature
   (`Cargo.toml:37 — serde_json = "1"`), so every key was **alphabetically reordered** and the
   trailing newline was dropped. Harmless to JSON semantics, ugly in a git-tracked or
   hand-maintained settings file. Full textual diff: `evidence/settings-diff.txt`.

2. **Session hosted.** `herdr agent start labclaude --kind claude --pane w1:p1` returned
   `"argv":["claude"]` — the plain interactive binary, no print/SDK flags. The pane banner read
   **`Claude Max`**, confirming subscription billing (S10) under herdr.

3. **Fact taught.** Prompt `Remember this codeword exactly for later: PELICAN-7731-ZULU` → reply
   `stored`.

4. **Session id captured and persisted.** After the ~5s debounce,
   `session.json` held:

   ```json
   .workspaces[0].tabs[0].panes.1.agent_session =
     {"source": "herdr:claude", "agent": "claude", "kind": "id",
      "value": "5d53d4f3-84e9-4436-a541-46ef7a78b064"}
   ```

   and the matching transcript existed on disk:
   `~/.claude/projects/…-lab-herdr-work/5d53d4f3-84e9-4436-a541-46ef7a78b064.jsonl`.

5. **kill -9.** SIGKILL on the server took the whole tree with it — pane shell and claude both died
   (`pane session terminated pane=1 pid=42410 signal=Hangup`). Stale `herdr.sock` /
   `herdr-client.sock` were left behind and cleaned up by the next start.

6. **Restart → revive → fact intact.** After restart plus a client attach, the pane ran
   `claude --resume 5d53d4f3-84e9-4436-a541-46ef7a78b064` and answered:

   ```
   ❯ What was the codeword I asked you to remember? Reply with only the codeword.
   ⏺ PELICAN-7731-ZULU
   ```

### The decisive caveat, confirmed in a second controlled round

`evidence/t1-headless-vs-attach.log` — kill -9, restart headless, poll 120s with **no** client, then
attach:

```
[00:07:09]   server alive after kill -9: NO
[00:07:09]   claude procs after kill -9: (none)
[00:07:12]   server4 pid=61969
[00:07:27]   t+15s   agent_status=idle/ready=True  claude_proc=NONE
[00:07:43]   t+30s   agent_status=idle/ready=True  claude_proc=NONE
[00:07:58]   t+45s   agent_status=idle/ready=True  claude_proc=NONE
[00:08:13]   t+60s   agent_status=idle/ready=True  claude_proc=NONE
[00:08:28]   t+75s   agent_status=idle/ready=True  claude_proc=NONE
[00:08:43]   t+90s   agent_status=idle/ready=True  claude_proc=NONE
[00:08:58]   t+105s  agent_status=idle/ready=True  claude_proc=NONE
[00:09:13]   t+120s  agent_status=idle/ready=True  claude_proc=NONE
[00:09:13] STEP 3 — now attach a client and re-poll
[00:09:23]   attach+10s  claude_proc=67774 67768 claude --resume 5d53d4f3-84e9-4436-a541-46ef7a78b064
```

The server log confirms restore ran but never spawned:
`persist.restore … outcome="ok" workspaces=1` with **no** following `pane.spawn.start` — whereas
every live spawn does log one.

### Why this matters for superlooper

- **The auto-recovery mechanism is real and works.** `resume_agents_on_restore` + the official
  integration's session-id capture + `claude --resume` is a genuine, code-verified,
  now-live-verified recovery path. This is the strongest resume story of any candidate examined.
- **But it does not hold in the mode we would run it in.** superlooper would run `herdr server`
  headless with no human attached. In that mode a crash leaves every flight a phantom until a person
  opens a client — the recovery is real but *human-triggered*, which is exactly the property the
  loop cannot depend on.
- **It fails "honest operations" during the gap.** For 120 consecutive seconds herdr answered
  `agent_status: idle, interactive_ready: true` for a pane with **no process at all**. That is the
  c2 bug class (absence-of-signal → `idle` instead of `UNKNOWN`) and the #258–#261 hollow-tab family,
  reproduced in the candidate we were considering *to escape* that family. Any adoption must treat
  herdr's `idle` as untrusted and verify liveness independently (c8's `pgrep -P` remains necessary).
- **Two further honesty notes, one good one bad.**
  - *Good:* `agent prompt` refused to lie. Both times the typed text failed to submit it returned
    `{"error":{"code":"agent_prompt_stalled","message":"agent prompt produced no observed state
    change within 5000 ms…"}}` rather than reporting success. That is the S3 behaviour we want.
  - *Bad:* the typed prompt **landed in the composer but was never submitted** — both times I had to
    send an explicit `pane send-keys <pane> enter` to make claude accept it. So herdr's headline
    "typed prompt with outcomes" is, in this configuration, a two-step affair with a failure on the
    happy path.
- **A first-run gate exists (S9).** On the first launch in a new cwd, claude showed its
  *"Quick safety check: Is this a project you created or one you trust?"* dialog — and herdr reported
  that pane as `agent_status: idle, interactive_ready: true` while it sat blocked on the dialog.
  A second false-idle, on the exact screen a launch-verification step would need to catch.

### Two environment landmines found on the way (both generalize beyond herdr)

1. **Inherited `CLAUDE_CODE_*` silently disables transcript saving — which silently breaks
   `--resume`.** The first drill run spawned claude from inside my own Claude Code session, so the
   child inherited `CLAUDE_CODE_SESSION_ID` and `CLAUDE_CODE_CHILD_SESSION=1`. The pane banner read
   **`⚠ Transcript saving is off — inherited CLAUDE_CODE…`**. No transcript ⇒ nothing to resume ⇒ the
   entire recovery story evaporates, with no error anywhere. I re-ran the whole drill under an
   explicit scrub (`env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_EXECPATH -u
   CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_PID -u CLAUDE_EFFORT`).
   **This widens claim c1**: the launch-time env assert must cover the `CLAUDE_CODE_*` family, not
   just `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`. Billing is not the only thing a stray inherited
   variable can silently flip.
2. **herdr's socket path is bound by `sun_path` (104 chars).** Starting the server with a data dir
   under the normal scratchpad path failed outright:
   `Error: Custom { kind: InvalidInput, error: "local socket name length exceeds capacity of sun_path
   of sockaddr_un" }`. Worked around with a short symlink. Any adoption must keep herdr's data dir
   shallow — a deep `.superlooper/...` state home would break it.

### Fence note (S8)

The API socket is created `srw-------` (owner-only), so it is fenced against *other users* but not
against other processes of William's own uid — which is every worker. `HERDR_SOCKET_PATH` exists as
an override, but the default path is deterministic, so an env-strip does not hide it. The
small-auth-fork-at-`handle_connection` mitigation identified earlier remains the required fix; this
run found nothing that changes it.

---

## Test 2 — plain claude `--session-id` / `--resume`: **PASS (unqualified)**

**Claim under test:** pre-assign `--session-id`, state a fact, exit, `--resume`, ask the fact back.
No herdr involved; isolated tmux server `-L sl-lab`.

Pre-assigned id `c44fd9b3-3452-43e9-a689-9f8f5b60efbe`. Phase A taught the codeword and exited via
`/exit`; phase B started a **brand-new process** `claude --resume c44fd9b3-…`.

**Verified from the transcript, not the screen.** (My first pass "verified" by grepping the pane —
which matched my own typed prompt text and produced two false PASSes. The transcript is the only
honest oracle here; the numbers below are from it.)

```
2026-07-29T06:12:17.865Z  sessionId=c44fd9b3-…  [user]      Remember this exact codeword for later: ALBATROSS-4419-KILO…
2026-07-29T06:12:20.777Z  sessionId=c44fd9b3-…  [assistant] stored
        <-- process exited (/exit), tmux session gone, new `claude --resume` process started -->
2026-07-29T06:12:33.044Z  sessionId=c44fd9b3-…  [user]      What exact codeword did I ask you to remember?…
2026-07-29T06:12:36.206Z  sessionId=c44fd9b3-…  [assistant] ALBATROSS-4419-KILO

distinct sessionIds in file: {'c44fd9b3-3452-43e9-a689-9f8f5b60efbe'}
file holds BOTH the pre-exit and post-resume exchanges in one transcript: True
```

**What this establishes**

- The **pre-assigned id is stable across exit and resume** — `--resume` appends to the *same*
  `<uuid>.jsonl`, it does not fork a new session id. So a runner-minted per-flight id (claim c3) can
  double as the permanent handle to that flight's conversation, across any number of crashes.
- **This recovery path is host-independent.** It needs no herdr, no cmux, no daemon — just the
  ability to re-exec `claude --resume <id>` in the right cwd. Whatever the loop's future body is,
  this primitive is available to it. superlooper uses neither flag today.
- Cost of adoption is small and mostly bookkeeping: mint the uuid at launch, pass `--session-id`,
  store it with the flight, and re-exec on recovery.

**Caveat worth carrying:** resume restores the *conversation*, not the *world*. The worktree, any
running commands, and the shell are not restored — the resumed session wakes up believing its last
tool call just happened. Any adoption needs a re-orientation step ("re-read state, re-run the
suite") rather than assuming the session can simply continue mid-thought.

---

## Test 3 — keychain from a `gui/$UID` LaunchAgent: **PASS**

**Claim under test:** a one-shot `gui/$UID` LaunchAgent can run `launchctl managername` and
`gh auth status`; does the keychain-backed `gh` token survive outside an interactive Aqua shell?
(This is the herdr #966 "keychain-in-launchd ≙ intermittent gh auth-death" worry.)

Bootstrapped from a plist held **in the lab dir only** — nothing was installed into
`~/Library/LaunchAgents`. `launchctl bootstrap gui/501 …` rc=0, `last exit code = 0`, then
`launchctl bootout` rc=0 and the service is gone.

```
context_label   : launchd-gui-501
managername     : Aqua
managerpid      : 1
manageruid      : 501
whoami          : willprout
HOME            : /Users/willprout
PATH            : /usr/bin:/bin:/usr/sbin:/sbin
SSH_AUTH_SOCK   : /private/tmp/com.apple.launchd.JoDL4cCV34/Listeners
default keychain: "/Users/willprout/Library/Keychains/login.keychain-db"
gh auth status  : rc=0 after 1s
                  github.com
                    ✓ Logged in to github.com account willprout (keyring)
                    - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

**What this establishes**

- **A `gui/$UID` LaunchAgent *is* in the Aqua session** — `managername = Aqua`, identical to an
  interactive shell. The login keychain is the default keychain, and `gh` read its keyring token in
  1 second with **no prompt and no hang**. The launchd-vs-Aqua gate is cleared for the gui domain.
  (This says nothing about `system/` domain daemons, which are a different session and were not
  tested — anything adopted here must stay in `gui/$UID`.)
- **The real gotcha is PATH, not the keychain.** launchd hands the job `/usr/bin:/bin:/usr/sbin:/sbin`
  — no `/opt/homebrew/bin`, no `~/.local/bin`. `claude` is **not** on that PATH. A launchd-hosted
  supervisor that shells out to `claude`, `gh`, or `tmux` by bare name will fail to find all three.
  PATH must be set explicitly in the plist or at spawn.

### Landmine found: `XDG_CONFIG_HOME` silently de-authenticates `gh`

My first baseline probe reported `You are not logged into any GitHub hosts` — alarming, and *wrong*.
Cause: I had set `XDG_CONFIG_HOME` to isolate **herdr's** data dir, and `gh` also honours
`XDG_CONFIG_HOME` for its config dir. Pointed elsewhere, `gh` finds no `hosts.yml` and reports a
clean, confident, **logged-out** state — rc=1, no error, no hint that a config path was redirected:

```
$ XDG_CONFIG_HOME=/private/tmp/hlab/herdr/xdg gh auth status
You are not logged into any GitHub hosts. To log in, run: gh auth login

$ env -u XDG_CONFIG_HOME gh auth status
github.com
  ✓ Logged in to github.com account willprout (keyring)
```

This is **the refused-read-as-empty class (#286) in a new costume**, and it lands directly on
claim c25 (per-worker `GH_CONFIG_DIR`/`CLAUDE_CONFIG_DIR`): an isolation knob set for one tool
silently de-authenticated a different tool, and the failure presents as a legitimate state rather
than an error. Any per-worker config-dir isolation must be paired with a positive post-spawn assert
("this worker can see an authenticated gh"), never assumed from a clean exit.

---

## Test 4 — typing into claude in tmux: **PASS (3/3 variants)**

**Claim under test:** run real claude in an isolated tmux server, deliver a prompt via `send-keys`
using claude-squad's recipe, verify submission, exit cleanly.

Because Test 1 showed herdr's typed prompt *landing without submitting*, I compared three delivery
variants instead of just one. Each question's **answer does not appear in its prompt**, and the
oracle is the transcript — so a screen match cannot produce a false PASS.

| Variant | Recipe | Result |
| --- | --- | --- |
| A | `send-keys -l <text>`, sleep 1s, `send-keys Enter` (claude-squad's) | **SUBMITTED** |
| B | `send-keys -l <text>`, `send-keys Enter` immediately, no settle | **SUBMITTED** |
| C | single call: `send-keys <text> Enter` (tmux key-parses) | **SUBMITTED** |

```
2026-07-29T06:14:07.986Z [user     ] What is 17 plus 25? Reply with only the number.
2026-07-29T06:14:11.182Z [assistant] 42
2026-07-29T06:14:12.062Z [user     ] What is 11 plus 8? Reply with only the number.
2026-07-29T06:14:13.978Z [assistant] 19
2026-07-29T06:14:16.148Z [user     ] What is 30 plus 4? Reply with only the number.
2026-07-29T06:14:18.443Z [assistant] 34
```

Exit was clean: `/exit` ended the session, `has-session` returned false, and the tmux server itself
survived with its other sessions (`herdrattach sentinel soak`) intact.

**What this establishes**

- **tmux `send-keys` into claude is reliable**, and the 1-second settle in claude-squad's recipe was
  **not required** — B and C submitted without it. (Keep a settle anyway for very long prompts; this
  test used short ones. The claim proven is narrow: short prompts do not need it.)
- **Plain tmux out-delivered herdr's purpose-built verb.** herdr's `agent prompt` failed to submit
  on both attempts in Test 1 and needed a manual `send-keys enter` chaser; raw tmux submitted 3/3.
  If typed-prompt reliability was a reason to prefer herdr, this run inverts it.
- `new-session -d` returning **rc=0 proves only a fork**, not a running program — the brief's warning
  held, and every spawn here was confirmed with a `has-session` poll before use.
- **claude-squad's trust-prompt detector is stale.** It greps for
  `"Do you trust the files in this folder?"`; claude 2.1.220 now prints
  `"Quick safety check: Is this a project you created or one you trust?"`. Anything borrowing that
  recipe inherits a screen-scrape that silently no longer fires — the exact fragility that makes
  scraping a last resort (c31).

---

## Test 5 — overnight soak: setup, and what it can honestly prove

**Running since 2026-07-28T23:57:31-06:00** on isolated tmux server `-L sl-lab` (server pid 30583):

| Session | Contents |
| --- | --- |
| `sentinel` | idle keep-alive, so the server never has a last-session-kill orphan race |
| `soak` | ticker writing one `date` line per 60s to `soak/ticks.log` (ticker pid 30645) |
| `soakclaude` | **addition** — a real interactive `claude --session-id 67e5d07f-…`, taught the codeword `NARWHAL-8823-OSCAR`, then left idle (costs nothing while idle) |

The morning summary is produced by `soak/summarize.sh`, which appends its own section to the end of
this file. It reports ticker gaps >90s, tmux server survival, `pmset -g log` sleep/wake events, and
whether the idle claude session both survived and still recalls its codeword.

**Scope limit, stated up front so the result is not over-read:** this Mac is configured **never to
sleep on AC** — `pmset -g custom` shows `sleep 0`, `displaysleep 0`, and `PreventUserIdleSystemSleep`
is currently asserted. So the soak measures **long-idle survival**, not sleep/wake survival. It
cannot settle the wake-crash question.

### BLOCKED: the controlled sleep/wake cycle

`sudo -n pmset -g sched` → `sudo: a password is required`. Per the brief's boundary, anything needing
interactive sudo is skipped and reported. **Consequence:** the sleep/wake half of the substrate
question is untested. The earlier downgrade of the wake-crash lore (traced to a 2015-closed issue)
still stands on documentary evidence only — no live confirmation either way. If this matters to the
decision it needs a supervised run where William can authorize `pmset`, or simply closing the lid
with the lab running.

---

## Cleanup and hygiene (verified, not asserted)

| Item | State |
| --- | --- |
| `~/.claude/settings.json` | **restored byte-identical** to the pre-lab backup — `diff -q` clean, sha `c6ecfa30…` both sides |
| `~/.claude/hooks/herdr-agent-state.sh` | removed by `herdr integration uninstall claude`; hooks dir back to only `suggest-cross-review.sh` |
| herdr server(s) | all stopped — `pgrep -f herdr-macos-aarch64` returns nothing. herdr was never installed system-wide (release binary run from the lab dir; no brew, no `~/.config/herdr`) |
| LaunchAgent | `launchctl bootout` rc=0, service gone; the plist lived only in the lab dir — **nothing** was written to `~/Library/LaunchAgents` |
| `~/Projects/superlooper` | untouched apart from this results file (`git status` shows only the three untracked docs) |
| Live runner (pid 2503, worker i225) | never signalled, never inspected beyond `ps`. No process I did not start was touched. |
| Still running by design | tmux `sl-lab` server (sentinel + soak ticker + overnight claude) and the summary waiter |

Lab artifacts, including all raw logs cited above, are under the session scratchpad at
`lab/` (`evidence/`, `herdr/`, `resume/`, `tmux/`, `keychain/`, `soak/`), reachable via the short
symlink `/private/tmp/hlab`. Nothing there is needed by anything of William's; delete freely.

---

## What these five results mean for the session-host decision

The owner's reframing was to judge candidates on **honest operations + resume quality**, not uptime.
Judged that way, tonight moves the decision in a specific direction:

1. **The resume primitive is host-independent and it works.** Test 2 is the cleanest PASS of the
   night, and it required no host at all — `--session-id` at launch, `--resume` to recover, one
   stable transcript. superlooper can adopt this *now*, before and independently of any substrate
   decision, and it is the single highest-value item found. It directly serves the "stop-and-resume
   after ANY interruption is a first-class requirement" ruling.
2. **herdr's added value is the bookkeeping, and the bookkeeping is where it broke.** Its recovery
   machinery is genuinely well-built and did revive a session with memory intact — but only when a
   human attached a client, and it reported `idle`/`interactive_ready` for two solid minutes while
   nothing was running. In a headless deployment (ours) that is a false-green on the exact axis the
   loop is most often burned by. Adoption is not disqualified, but it would need: a liveness check
   that does not trust herdr's status, and either an upstream fix or a forced-attach workaround for
   the client-gated spawn.
3. **The tmux floor is stronger than expected.** Plain `send-keys` delivered 3/3 (including without
   the settle delay that claude-squad's recipe implies is needed), while herdr's purpose-built
   `agent prompt` failed to submit 2/2. If typed-prompt reliability was an argument for a
   control-plane, it now argues the other way.
4. **launchd hosting is viable on the keychain axis.** A `gui/$UID` agent is in the Aqua session and
   read `gh`'s keyring token instantly, no prompt. The blocker there is mundane (PATH), not
   architectural — which removes one of the scarier unknowns from any supervisor redesign.
5. **Two silent-lie classes were found tonight that no substrate would have fixed** — inherited
   `CLAUDE_CODE_*` disabling transcripts (which would have destroyed resume with no error), and
   `XDG_CONFIG_HOME` de-authenticating `gh` into a confident logged-out state. Both are the
   refused-read-as-empty family, both are engine-side, and both argue for the floor work (c1/c25's
   env assertions, widened) ahead of any host swap. **I hit both of these by accident, in one night,
   just by setting up a lab** — which is a fair estimate of how often they are waiting in production.

**Not answered tonight:** sleep/wake behaviour (BLOCKED, needs sudo), multi-session scale, and
whether herdr's client-gated spawn is a bug or a deliberate design choice — worth one upstream issue
before any adoption decision, since a one-line fix upstream changes the verdict on Test 1 materially.

---

## Test 5 — overnight tmux soak: results

Started 2026-07-28T23:57:31-06:00 on isolated tmux server `-L sl-lab`: a ticker writing `date`
every 60s, plus (addition) a real interactive claude session taught one codeword and left idle.

```
===== soak summary generated 2026-07-29T07:00:01-06:00 =====

--- tmux server ---
soak session: ALIVE
server pid now : 30583
server pid at start: 30583  (ticker pid 30645)
sessions now   : sentinel soak soakclaude waiter 

--- ticker coverage ---
ticks recorded : 423
first tick     : 2026-07-28 23:57:31
last tick      : 2026-07-29 06:59:38
wall span      : 7h 2m (25327s)
expected ticks : ~423
gaps >90s      : 0
   (none — ticker never missed more than one minute)

--- did the machine actually sleep? ---
2026-07-27 07:13:11 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008ac4 [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-27 07:13:11 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008ac4 [System: PrevIdle DeclUser kDisp]          
2026-07-28 00:43:57 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd000081a0 [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-28 00:43:57 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd000081a0 [System: PrevIdle DeclUser kDisp]          
2026-07-28 06:40:31 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd0000848c [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-28 06:40:31 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd0000848c [System: PrevIdle DeclUser kDisp]          
2026-07-28 07:40:31 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008530 [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-28 07:40:31 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008530 [System: PrevIdle DeclUser kDisp]          
2026-07-28 08:40:31 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008682 [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-28 08:40:31 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008682 [System: PrevIdle DeclUser kDisp]          
2026-07-29 02:05:36 -0600 Assertions          	PID 344(powerd) Created InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008061 [System: PrevIdle DeclUser SRPrevSleep kCPU kDisp]          
2026-07-29 02:05:36 -0600 Assertions          	PID 344(powerd) Released InternalPreventSleep "Holding in darkwake for up to 20 seconds to query model for inactivity prediction" 00:00:00  id:0x0xd00008061 [System: PrevIdle DeclUser kDisp]          

--- overnight claude session ---
tmux session soakclaude: ALIVE
claude process: 98263  (session id 67e5d07f-bc91-4948-97b0-611c3303baec)

--- can it still answer? ---
RESULT: session survived the night AND recalled the codeword

full transcript:
  2026-07-29T06:19:50.247Z [user     ] Remember this exact codeword for the whole night: NARWHAL-8823-OSCAR. Reply with
  2026-07-29T06:19:52.989Z [assistant] stored
  2026-07-29T13:00:02.871Z [user     ] What exact codeword did I ask you to remember last night? Reply with only the co
  2026-07-29T13:00:07.619Z [assistant] NARWHAL-8823-OSCAR

===== end =====
```

### Test 5 verdict: **PASS for long-idle survival — and only that**

**What is proven.** Over **7h 02m** the tmux lab server never blinked: **423 ticks recorded against
~423 expected, zero gaps >90s**, same server pid (30583) start to finish, all four sessions alive.
The idle interactive claude session survived on its **original pid 98263** — no resume, no restart —
and answered `NARWHAL-8823-OSCAR` after ~6h40m idle. A detached tmux server plus a live claude
session is stable across a full night of idleness on this machine.

**What is NOT proven, stated plainly.** The machine **never slept**. `pmset -g log` shows **zero**
`Entering Sleep` / `Wake from` transitions in the 23:00→07:30 window — exactly as `sleep 0` on AC
predicts. The one darkwake-adjacent event (02:05:36, a powerd inactivity-prediction assertion) did
not interrupt the ticker at all: the 60-second cadence runs unbroken straight through it.

So the wake-crash question is **still open**, and this soak must not be cited as having settled it.
Combined with the BLOCKED `sudo -n pmset` above, nothing in this run tested sleep/wake in either
direction. The honest state: the 2015-closed-issue downgrade of the wake-crash lore still rests on
documentary evidence alone. Settling it needs either a supervised `pmset` run or simply closing the
lid with the lab up — both cheap, neither done tonight.
