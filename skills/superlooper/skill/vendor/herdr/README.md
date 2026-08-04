# The carried state-report hook (issue #307)

`herdr-agent-state.sh` is **herdr's own asset, carried byte-for-byte** from the pinned release. It
is not ours, it is not modified, and it must never be edited — the issue's boundary is explicit:
*carry the invocation, never fork the script*.

## Why it is here at all

herdr revives a crashed agent by re-running `claude --resume <id>`, and the only way it ever learns
that id is this hook: Claude Code fires it at `SessionStart`, and it reports
`pane.report_agent_session` over herdr's control socket. Without a captured id, herdr's
`persist.restore` brings the pane back as a **bare shell** — measured, not assumed (see below).

herdr installs the hook with `herdr integration install claude`, which writes it into the machine's
**global** Claude settings file. This loop never runs that command on any machine it manages. The
launcher renders the same registration into a settings file belonging to one lane and hands it to
that session alone with `--settings`, which Claude Code *merges* over the user's settings rather
than replacing them. So the operator's `~/.claude/settings.json` is never a party to a launch, and
the engine's own three globally-registered hooks keep firing untouched.

The pieces:

| | |
|---|---|
| The asset | `herdr-agent-state.sh` — this directory, verbatim |
| The registration | `../../lib/herdr_hook.py` — the pin, the renderer, the detector |
| The wiring | `../../bin/start-session.sh`, claude branch (`--settings`) |
| The check | `doctor --stack`'s `host state hook` block |
| The fence patch | `../../../vendor/herdr/` — a **build** input, never installed; this asset is a **runtime** one, so it lives inside the publishable payload |

## The pin

| | |
|---|---|
| Upstream | `https://github.com/herdrdev/herdr` |
| Version | **v0.8.0** (tag `v0.8.0`, commit `346411f`) — the release #302 pinned |
| Asset path upstream | `src/integration/assets/claude/herdr-agent-state.sh` |
| sha256 | `ffd5a76b7c62f5313040fc1e98fa010ff19a7aa85dd9fe6f325b9729d5f01b46` |
| Integration version | `7` (`CLAUDE_INTEGRATION_VERSION`, stamped inside the asset) |
| License | **Apache-2.0** — herdr relicensed from AGPL-3.0-or-later in 0.8.0 |

The registration this reproduces, read out of `src/integration/claude_settings.rs` and
`src/integration/command.rs` at that tag:

```json
"SessionStart": [ { "matcher": "*", "hooks": [
  { "type": "command", "command": "bash '<path>/herdr-agent-state.sh' session", "timeout": 10 } ] } ]
```

One event and no other. The same release actively **removes** the older registrations
(`Stop`/`PreToolUse`/`UserPromptSubmit`/`PostToolUse`/`SubagentStop`/`PermissionRequest`/
`SessionEnd`) when it installs, so carrying more than this would be carrying a retired contract.
`herdr_hook.py` still *recognises* those older spellings, because the doctor's question is "was the
installer ever run on this machine", and a stale registration is just as much a yes.

## What was measured (2026-08-04, pinned build, isolated socket, no client ever attached)

1. A session launched with only the per-lane settings file added: herdr reported
   `agent_session {kind: "id", source: "herdr:claude", value: <the runner's own minted id>}`.
2. **The control** — the identical command line with `--settings` removed, same pre-assigned
   `--session-id` in argv — reported **no session at all**. So the capture is this hook, not herdr
   reading the id out of argv.
3. `kill -9` the server, restart headless: the hooked lane came back as
   `claude --resume <that same id>` and still knew a codeword from before the crash; the control
   lane came back as a bare shell.
4. The operator's global settings file was byte-identical (sha256 `47b5e518…`) throughout.

Full transcript in `reports/i307.md`.

## Re-applying at a version bump — the deliberate-event rule

A bump re-runs this, exactly like the fence patch next door. **A bump that skips it may have
silently changed the integration contract**, and the failure mode is quiet: sessions launch and
work, and simply never come back after a crash.

1. **Re-read the two source files at the new tag** — `src/integration/claude_settings.rs`
   (which event, which matcher, which timeout) and `src/integration/command.rs` (the command
   spelling and its shell quoting). Update `herdr_hook.py`'s constants if either moved.
2. **Copy the new asset in**, recompute its sha256, and update `HOOK_SCRIPT_SHA256` here and in
   `herdr_hook.py`. `tests/test_herdr_hook.py` goes red until both agree — that is the mechanism.
3. **Check `CLAUDE_INTEGRATION_VERSION`** in `src/integration/mod.rs` against `INTEGRATION_VERSION`.
   A changed number is the vendor telling you the contract moved, independently of the checksum.
4. **Re-run the capture drill** — it is the only check that proves the whole chain rather than the
   file's shape. An isolated server on its own socket, one agent started with the rendered settings
   file and one without, then `kill -9` and a headless restart. The one without is not optional:
   a drill where both lanes come back proves nothing about the hook.
5. **Confirm the global settings file is untouched** (sha256 before and after), and that
   `doctor --stack`'s `host state hook` block still passes.

## A known interaction — the fence (#305) refuses this hook

The carried fence patch rejects **every unauthenticated connection** to the control socket before
dispatch. This hook opens the socket directly and presents no token (it is stock herdr, and herdr
has no token concept), and `i<N>` workers deliberately never hold one. So **on a fenced host this
capture goes silent** and herdr-side revive stops working — quietly, because a session that cannot
report its id still runs perfectly.

That is not a defect in either piece; it is a seam between two accepted decisions, and closing it
is a security-posture call the owner makes, not something to work around here. Filed as **#331** (`needs-owner`) rather than patched in this lane. Until it is decided, the loop's own
resurrection floor (#298 — the runner mints `--session-id` and relaunches with `--resume`) is
unaffected, because it never asks herdr anything.
