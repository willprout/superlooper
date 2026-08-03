# Clientless `--wait` delivery test — 2026-07-30 (overnight, unattended)

Do not commit this file. Companion to `SPIKES-2026-07-30-supervised.md` (methods reused from there:
§0.1 allowlist scrub, §2 herdr setup, transcript-oracle verification).

**The question:** does `herdr agent prompt <name> --wait` deliver into a hosted claude session on a
herdr server to which NO client has EVER attached — no TUI, no robot viewer, no observe/control
stream, ever? Every reliable delivery observed so far happened on a server that had (at some point)
a viewer. This is the untested cell named in supervised §8 ("fresh agent + `--wait` + ZERO clients
attached — the actual deployment shape").

## Verdict table

| Condition | Description | Verdict |
|---|---|---|
| PRE | Throwaway claude under scrub: Max banner, transcript file written | **PASS** — §2 |
| CW1 | Clientless `--wait`, FIRST prompt into fresh agent | **PASS** — delivered in 4.2s, transcript-verified (§4) |
| CW2 | Clientless `--wait`, SECOND prompt (steady-state) | **PASS** — delivered in 10.2s, transcript-verified (§5) |
| CN | Clientless plain prompt (no `--wait`), contrast | **DELIVERED (seasoned pane)** — rc=0 in 13ms with no observed state change, yet the transcript shows submission 0.3s later; the "silent drop" applies to FRESH panes (supervised A1a), not seasoned ones (§6) |
| TEARDOWN | Zero lab processes left; ~/.claude/settings.json untouched | **PASS** — §7 |

Invariant held throughout: the herdr server was started headless and **no client of any kind ever
attached** — every interaction was a one-shot CLI verb over the socket. The claude integration was
NEVER installed (no resume in this test; ~/.claude/settings.json never written).

## §0 Baselines (before any lab spawn)

- `~/.claude/settings.json`: sha256 `c6ecfa30e0e701f8b738d0443ff5fec987040f9d34b1227a28347e49ebe373ef`,
  mtime epoch `1785304539`, 1927 bytes (identical to the supervised session's §12 restored hash).
- No herdr processes running (`pgrep -fl herdr` → rc=1). No tmux server on socket `hlab2` or `hlab31`.
- Live superlooper runner processes identified and EXCLUDED from all kills: pids 23662/23680 (a3),
  60811/60827 (a2), 48602 (i258), 4737/4746 (i225), 38226 (owner's interactive claude).
- This session's own env is contaminated as expected (CMUX_*, CLAUDE_CODE_*, CLAUDECODE,
  ANTHROPIC_API_KEY still present in this pre-existing terminal); every lab spawn goes through the
  scrub wrapper below, so none of it is inherited.

## §1 Lab setup

| Item | Value |
| --- | --- |
| Lab root | `/private/tmp/hlab31` → symlink into this session's scratchpad `…/6c9eeb73-…/scratchpad/lab31` |
| herdr binary | copied from supervised lab (`/private/tmp/hlab30/bin/herdr-macos-aarch64`, official 0.7.5 release); `hcli --version` → `herdr 0.7.5` rc=0 |
| Scrub wrapper | `/private/tmp/hlab31/scrub` = `env -i HOME USER LOGNAME SHELL TERM=xterm-256color PATH=/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin` |
| hcli wrapper | scrub + `XDG_CONFIG_HOME=/private/tmp/hlab31/hd` + `XDG_STATE_HOME=/private/tmp/hlab31/hd/state` + binary |
| Socket (expected) | `/private/tmp/hlab31/hd/herdr/herdr.sock` (39 chars — sun_path safe) |
| Integration | **NOT installed** — `integration install claude` never run; settings.json never touched |

Scrub proof — full `env` output inside the scrub (6 vars total, contraband grep CLEAN):

```
HOME=/Users/willprout
USER=willprout
LOGNAME=willprout
PATH=/Users/willprout/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin
SHELL=/bin/zsh
TERM=xterm-256color
```

`command -v claude` inside the scrub → `/Users/willprout/.local/bin/claude` → versions/2.1.220
(`claude --version` → `2.1.220 (Claude Code)`).

## §2 PRE — throwaway claude under the scrub: **PASS**

Isolated tmux server `-L hlab31` created under the scrub (tmux by absolute path
`/opt/homebrew/bin/tmux` — homebrew deliberately not on the allowlist PATH). Throwaway session
`pre`, cwd `/private/tmp/hlab31/work` (claude resolved the symlink to the real scratchpad path —
same behavior as supervised §1), pre-assigned `--session-id ffb9ec43-283a-4ed2-a73f-a38503b8a83f`,
pane command `env > pre.pane.env; exec claude --session-id …`.

- **pre.pane.env** (per-spawn proof): allowlist + tmux-added vars only (`TMUX`, `TMUX_PANE`,
  `TERM=tmux-256color`, `TERM_PROGRAM`, `COLORTERM`, `SHLVL`, `PWD`, `OLDPWD`, `_`).
  Contraband grep: **CLEAN**.
- Trust gate (first run in new cwd) appeared, accepted via send-keys Enter — this pre-trusts the
  exact cwd the herdr agent will use, so the herdr phase measures prompt delivery, not the gate.
- **Banner**: `Fable 5 with xhigh effort · Claude Max · williamdebarany@gmail.com's Organization`
  → subscription billing, NOT API. No "Transcript saving is off" warning.
- **Transcript oracle** — file appeared at
  `~/.claude/projects/-private-tmp-claude-501--Users-willprout-Projects-superlooper-6c9eeb73-…-scratchpad-lab31-work/ffb9ec43-….jsonl`:

  ```
  2026-07-30T06:39:53.485Z ffb9ec43 [user]      What is 2 plus 3? Reply with only the number.
  2026-07-30T06:39:56.055Z ffb9ec43 [assistant] 5
  ```

- `/exit` clean; tmux server self-terminated (verified: `no server running`, throwaway pid gone).
  **No tmux exists from here on** — the herdr phase runs with zero tmux servers, zero viewers.

## §3 Headless herdr server + fresh agent (no client, ever)

- `herdr server` started as a plain background process (no tty, no tmux) via
  `scrub + XDG pair + sh -c 'env > server.env; exec herdr server'`. Server pid **84538**.
- **server.env** (per-spawn proof): the 6 allowlist vars + `XDG_CONFIG_HOME`/`XDG_STATE_HOME`
  + sh-added `PWD`/`SHLVL`/`_`. Contraband grep: **CLEAN**.
- Server banner confirmed the isolated sockets:
  `api socket: /private/tmp/hlab31/hd/herdr/herdr.sock`,
  `client socket: /private/tmp/hlab31/hd/herdr/herdr-client.sock`.
  **The client socket was never connected to at any point in this run.**
- `workspace create --cwd /private/tmp/hlab31/work --label w1` → `workspace_created`, root pane
  `w1:p1`, viewport 23–24 rows (headless default), cwd resolved by herdr to the real scratchpad
  path (same as supervised §2). Pane shell = zsh pid 85847, child of the server.
- `agent start labclaude --kind claude --pane w1:p1 --timeout 60000 -- --session-id 39c26db1-edc1-4f6b-a5a2-1e020e737657`
  → `agent_started`, rc=0, `agent_status: idle`, `interactive_ready: true`,
  argv `["claude","--session-id","39c26db1-…"]`. Session id pre-assigned by the lab so the
  transcript file is attributable beyond doubt.
- Real process verified: server 84538 → pane zsh 85847 → claude **86966**
  (`claude --session-id 39c26db1-…`). `ps eww 86966` env sweep: **contraband CLEAN** —
  zero CMUX_*/CLAUDE_CODE_*/ANTHROPIC_* vars. (Note: the pane shell is a login zsh, so
  path_helper widened PATH with system dirs; with the API-key line commented out of ~/.zshrc
  since the supervised session, nothing contraband re-entered. HOME/USER/SHELL/TERM as allowlisted.)
- Deliberately NOT done before the prompt tests: no `pane` capture/read of any kind, no status
  polling beyond the start call's own response — nothing that could plausibly touch herdr's
  view/render state. First contact with the pane is the `--wait` prompt itself.

## §4 CW1 — clientless `--wait`, FIRST prompt into a fresh agent: **PASS**

The exact untested cell from supervised §8: fresh agent + `--wait` + zero clients ever attached.

```
2026-07-30T06:42:13Z  hcli agent prompt labclaude 'Reply with only this codeword: LANTERN-0731-KILO' --wait --timeout 120000
rc=0   wall time 4.153s
result: type=agent_prompted  agent_status=idle  interactive_ready=true  state_change_seq=3
        terminal_title="✳ Codeword verification request"   (claude's own auto-title — corroborating)
```

Transcript oracle (`…-scratchpad-lab31-work/39c26db1-….jsonl`):

```
2026-07-30T06:42:13.544Z [user]      Reply with only this codeword: LANTERN-0731-KILO
2026-07-30T06:42:17.169Z [assistant] LANTERN-0731-KILO
```

Delivered ~0.4s after the CLI call started; assistant reply exists (criterion) and matches (bonus).

**No-client proof at CW1 time:** `lsof -U | grep hlab31` → exactly two entries, both the server's
own LISTENING sockets (pid 84538 on `herdr.sock` and `herdr-client.sock`); zero established
connections. `grep -ciE 'client.*(attach|connect)' herdr-server.log` → **0**.

**This kills the "some client must have existed at some point" hypothesis.** `--wait` first-prompt
delivery works on a server no client has ever touched.

## §5 CW2 — clientless `--wait`, steady-state (second prompt): **PASS**

```
2026-07-30T06:42:42Z  hcli agent prompt labclaude 'Reply with only this codeword: MARMOT-5150-DELTA' --wait --timeout 120000
rc=0   wall time 10.171s
result: type=agent_prompted  agent_status=idle  state_change_seq=5
```

Transcript oracle:

```
2026-07-30T06:42:42.466Z [user]      Reply with only this codeword: MARMOT-5150-DELTA
2026-07-30T06:42:51.853Z [assistant] (empty text record)
2026-07-30T06:42:52.027Z [assistant] MARMOT-5150-DELTA
```

(The empty assistant record preceding the real one is a transcript artifact, not a failure —
reply-exists criterion satisfied.)

## §6 CN — clientless plain prompt (no `--wait`), seasoned pane: **DELIVERED — with the false-green signature intact**

```
2026-07-30T06:42:59Z  hcli agent prompt labclaude 'Reply with only this codeword: OSPREY-2718-VICTOR'
rc=0   wall time 0.013s   (thirteen milliseconds)
result: type=agent_prompted  agent_status=idle  state_change_seq=5   <- UNCHANGED from CW2's return
```

Transcript oracle (checked again 100s later; full file dump):

```
2026-07-30T06:42:59.799Z [user]      Reply with only this codeword: OSPREY-2718-VICTOR
2026-07-30T06:43:02.187Z [assistant] OSPREY-2718-VICTOR
```

`agent list` afterwards: `state_change_seq=7` — the exchange happened AFTER the verb returned.

Interpretation — the never-attached condition does NOT change the no-wait picture established in
the supervised session:

- **Fresh pane + plain verb + never-attached = silent drop** was already proven there (A1a ran on
  a server no client had yet touched: rc=0, text stranded in composer 65+s).
- **Seasoned pane + plain verb + headless = submits** (supervised events 2–3/A1d) — reproduced
  here in the strictly never-attached condition (2 completed exchanges prior).
- Constant either way: the plain verb returns rc=0 instantly with **no observed state change**,
  i.e., its return value carries zero delivery information. Tonight it happened to be true;
  on a fresh pane it is a lie. The verb is unusable as a delivery signal regardless.

Methodological note: an intermediate poll using `grep -l <codeword>` timed out spuriously
(grep -l prints the FILENAME, which never contains the codeword). Caught before misclassification;
the verdict above is from the full transcript dump. Recorded as a reminder that delivery oracles
must read transcript content, not wrapper exit codes — including the lab's own wrappers.

## §7 Teardown and hygiene (verified, not asserted)

| Item | State |
| --- | --- |
| herdr | graceful `server stop` rc=0; pids 84538 (server) / 85847 (pane zsh) / 86966 (lab claude) all verified dead; `pgrep herdr-macos` empty; both sockets removed |
| tmux `-L hlab31` | self-terminated after PRE's `/exit` (before the herdr phase even began); verified `no server running` |
| PRE throwaway claude | dead (pgrep on its session id: empty) |
| `~/.claude/settings.json` | **never written**: sha256 `c6ecfa30…` AND mtime epoch `1785304539` identical to the §0 pre-lab baseline. Integration was never installed, so nothing to restore |
| `~/.claude/hooks/` | only `suggest-cross-review.sh` — no herdr hook ever created |
| Live superlooper workers (23680 a3, 60827 a2, 4746 i225, 38226 owner claude) | all still alive post-teardown; never touched |
| cmux | never touched |
| Lab artifacts | `…/scratchpad/lab31/` via symlink `/private/tmp/hlab31` (env proofs, cw1/cw2/cn JSON, server log, session.json); disposable, delete freely |
| Transcripts | two new jsonl files under the `…-scratchpad-lab31-work` project slug (`ffb9ec43` PRE, `39c26db1` lab agent) — claude's own records, left in place as evidence |

## §8 Bottom line for superlooper adoption

1. **The deployment shape works.** `agent prompt --wait` delivered first-prompt AND steady-state
   into a fresh headless claude on a herdr server that never had any client — the exact
   "runner drives sessions with no window anywhere" topology. The last untested cell is closed,
   in the direction adoption needed.
2. The supervised session's model v4 survives unmodified in the never-attached condition:
   `--wait` delivers everywhere tested; the plain verb's rc=0 is delivery-information-free
   (instant return, no state observation) even when it happens to deliver.
3. Unchanged prescription: every programmatic prompt uses `--wait` + an independent
   transcript-side delivery check; the plain verb stays disqualified.
4. Scope honesty: tonight's clientless PASS is on FRESH spawns. Post-revive first prompts were
   NOT retested here (no integration installed, no resume in this lab) — supervised §9/§11's
   "post-revive first prompt stalls honestly, needs an Enter chaser" stands as the operative
   caveat for the crash-recovery path, and revive itself remains client-gated (supervised §10).
