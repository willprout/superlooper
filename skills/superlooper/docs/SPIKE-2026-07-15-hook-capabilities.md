# SPIKE 2026-07-15 — worker-harness hook capabilities on both CLIs

**Question (Wave-0 gate).** Before writing W1 issues that depend on a hook-based worker
harness, prove empirically — on THIS machine, under the exact `--dangerously-skip-permissions`
launch mode the machinery uses — whether each of these behaves as the design assumes, on
**Claude Code** and on **Codex**:

1. **Stop-hook block + inject** — a Stop hook returning `{"decision":"block","reason":"…"}`
   forces the session to keep going, with `reason` delivered to the model as the continuation
   instruction. (The transport under the mailbox and the challenge-response ladder.)
2. **PreToolUse deny** — a PreToolUse hook returning `permissionDecision:"deny"` blocks a tool
   call *even in bypass-permissions mode*. (The mechanism that makes AskUserQuestion and
   pattern-kills impossible rather than instructed-against.)
3. **cwd-safety (the D14 mechanism)** — does a hook still run when the session's worktree cwd
   has been deleted out from under the CLI?

**Method.** Isolated fixture under a scratch dir with its own `.claude/settings.json`
registering three command hooks (Stop→mailbox, PreToolUse:AskUserQuestion→deny,
PostToolUse→liveness-log). Live `claude --model haiku --dangerously-skip-permissions`
sessions driven to (a) rest with mail armed, (b) attempt AskUserQuestion. Codex tested via
`codex exec --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust` with
a `hooks_path` Stop hook attempting the same block+inject. D14 reproduced directly by spawning
a child process with an explicit deleted cwd vs an inherited deleted cwd.

**Environment.** claude 2.1.210, codex-cli 0.143.0 (gpt-5.5), macOS (Darwin 25.2.0), cmux
present. A live superlooper dogfood loop was running on the machine throughout; the spike used
only isolated scratch dirs / a throwaway cmux workspace and never touched it.

---

## Verdict

| Capability | Claude 2.1.210 | Codex 0.143.0 |
|---|---|---|
| Stop-hook block + inject | **PASS** | **FAIL** (Stop is notify-only) |
| PreToolUse deny under bypass | **PASS** | **N/A** (no PreToolUse event) |
| cwd-safety of hook spawn | **root cause confirmed** (fix is spawn-cwd + teardown order) | same (CLI-agnostic) |

**Bottom line:** the hook-based worker harness (mailbox delivery, report harvest via Stop,
PreToolUse deny) is **fully supported on Claude and must be Claude-only**. Codex workers get
the **degraded path** the fresh-eyes design already specifies: typed challenge-response probes
with machine-readable file acks, and no in-session tool-denial (rely on the classifier's
`at_dialog`/`logged_out` states as the backstop). This is the agent-boundary answer W1 needs.

---

## Evidence

### Claude — Stop block + inject: PASS
Mail file armed (`state/mail/w1` analogue) with "write ack.txt containing PONG". The stop hook,
on the session's first rest, consumed the mail and returned
`{"decision":"block","reason":"MAILBOX: …"}`. Result: `ack.txt` = `PONG` appeared within ~2s;
the session then rested a second time. Stop-hook log across the episode:

```
stop_hook_active=False   ← first rest; hook injected the block
stop_hook_active=True    ← second rest; the built-in loop guard is set
```

Two things proven at once: (a) the continuation instruction reached the model verbatim and was
obeyed with **no keystrokes typed into the pane**; (b) Claude Code sets `stop_hook_active=true`
on the injected turn, giving a built-in guard against an infinite block loop (the harness must
still bound its own re-injection, but the runaway is not silent).

### Claude — PreToolUse deny under `--dangerously-skip-permissions`: PASS
Session instructed to call `AskUserQuestion`. The PreToolUse hook logged the full tool input
and returned `permissionDecision:"deny"` with a reason. The model received the **exact** reason
string and wrote it to `denied.txt`:

```
AskUserQuestion is forbidden in unattended worker sessions - write your blocked-question file instead.
```

Deny fires even though the session runs bypass-permissions — confirming a worker cannot escape
the denial. This is the mechanism that retires the i280 (AskUserQuestion in an unattended lane)
and the dashboard collateral-kill (`pkill -f`) incidents by construction.

### Claude — cwd-safety / D14 mechanism: root cause confirmed
Reproduced the `posix_spawn '/bin/sh' ENOENT` failure directly:

```
explicit-cwd spawn  (cwd = deleted worktree)  → FileNotFoundError [Errno 2]   ← the D14 failure
inherited-cwd spawn (deleted dir, no explicit cwd) → hook STILL RAN (pwd broken, rc kept 0)
```

The failure is specifically a spawn that passes the **deleted worktree as an explicit cwd**
(Node's `child_process` model). A spawn that runs from a safe cwd succeeds even with the
worktree gone. **Both halves of the C3 fix are therefore load-bearing:** (1) the hook launcher
must spawn from a safe cwd (`$HOME`/skills dir), and (2) the runner must not prune a worktree
while its CLI is still live (teardown ordering). Either alone narrows the window; both together
close it.

### Codex — Stop block + inject: FAIL (notify-only)
`codex exec` with a `hooks_path` Stop hook attempting `{"decision":"block",…}`. Codex printed
its own internal dispatch —

```
hook: Stop
hook: Stop Completed
tokens used 11,955
READY            ← session exited normally
```

— but our command hook **never executed** (its log stayed empty) and **no continuation
occurred** (no file written). Codex treats Stop as a fire-and-forget notification, not a
continuation gate. Codex hooks are additionally **trust-gated**: `~/.codex/config.toml` carries
`[hooks.state]` entries with `enabled = false` + a `trusted_hash`, requiring
`--dangerously-bypass-hook-trust` even to attempt them. There is **no PreToolUse event** in
codex's model at all.

Note also (from forensics, corroborated here): Codex's `notify` callback on this machine points
at `SkyComputerUseClient`, which the 07-15 forensics logged crashing 10× — not a channel to
lean on.

---

## What this changes in the issue set

- The mailbox transport and report-harvest issues are scoped **Claude-only**, living behind the
  agent boundary (`start-session.sh` + the hook scripts), with the Codex adapter degrading to
  typed-probe + file-ack. State this in each affected issue's Boundaries.
- The PreToolUse-deny issue is **Claude-only**; the `at_dialog`/`logged_out` classifier states
  (which are CLI-agnostic screen scrapes) are the Codex backstop and ship regardless.
- The teardown-ordering issue and the safe-spawn-cwd issue are **both** required for D14 and are
  CLI-agnostic. Neither depends on this spike; they can proceed immediately.
- **Owner-decision dependency (e):** "retire keystrokes after mailbox soak" stays open — this
  spike proves the mailbox *works*, not that it has *soaked*. Keystrokes stay as the wake-ping
  and the Codex path until William rules.
