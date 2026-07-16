# SPIKE 2026-07-15 — mailbox round-trip in a live cmux worker

**Question (Wave-0 gate).** The runner–worker delivery channel is the proven failure surface:
`rc=0` "sent" is not "arrived" (i280: 3 of 6 nudges registered, one corrupted/interleaved),
`rc=3` carries no diagnosis, mid-generation keystrokes coalesce. The fresh-eyes design replaces
message delivery with a **mailbox**: the runner drops a file, the session's Stop hook consumes
it and returns its content as the continuation reason, and consumption stamps a receipt —
**verified delivery with zero keystrokes into the pane**. Prove it works end-to-end in a session
hosted by a real cmux surface (not just a bare terminal), because the cmux transport is exactly
what was lossy in the incidents.

**Method.** Spawned `claude --model haiku --dangerously-skip-permissions` **inside a fresh cmux
workspace** via `cmux new-workspace --command` (the same surface-hosted model the machinery
uses; isolated from the live dogfood loop). Armed a mail file *before* the session rested. The
Stop hook (see the hook-capabilities spike) consumes mail → `mv`s it to a `.consumed.<ts>`
receipt → returns `{"decision":"block","reason":"MAILBOX: <content>"}`. **No `cmux send` /
`send-key` was ever issued** — delivery is by file-drop alone.

**Environment.** claude 2.1.210, cmux (com.cmuxterm.app), macOS. Live dogfood loop present and
untouched; test ran in a throwaway workspace, cleaned up after.

---

## Verdict: **PASS**

A session running in a real cmux surface picked up a runner-dropped file and acted on it, with
no keystrokes typed into its pane. Delivery is **verified** by the consumption receipt, not
inferred from an exit code — closing the rc=0/rc=3 ambiguity in both directions for any session
that takes turns.

## Evidence

Mail armed: "write `ack2.txt` containing exactly `CMUX-PONG`". Session spawned in
`workspace:5`. Within ~4s:

```
ack2.txt        → CMUX-PONG          ← session obeyed the mailed instruction
mail dir        → w1.consumed.1784156653   ← receipt: the hook consumed the drop
cmux send calls → 0                   ← nothing was typed into the pane
```

The receipt (`.consumed.<epoch>`) is the delivery proof the design calls for: the runner reads
it and knows the mail was *taken*, not merely *sent*. Contrast the incident channel, where a
`send` returning 0 meant only "typed", and three of six such sends never became turns.

## Scope / limits (honest)

- **Proven:** claude-hosted-in-cmux, file-drop delivery, block+inject continuation, consumption
  receipt, keystroke-free. This is the mechanism, working.
- **Not proven here (deliberately):** (1) delivery into a *busy mid-generation* session —
  Claude's stop hook fires at rest, so mail is delivered on the next turn boundary; a session
  mid-generation gets the mail when it next rests, which is the intended semantics (a WORKING
  session isn't interrupted), but the latency envelope under real worker load wants a soak.
  (2) The Codex path — see the hook-capabilities spike: Codex Stop is notify-only, so Codex
  workers use the degraded typed-probe + file-ack channel, not the mailbox. (3) Long-run
  reliability across many nights — that is the "mailbox soak" that gates owner-decision (e)
  (retire keystrokes). Until then keystrokes survive as the idle wake-ping only.

## Consequence for the issue set

- The mailbox delivery + report-harvest issues are cleared to be written (Claude-only, behind
  the agent boundary). DoD includes the consumption receipt as the runner-visible delivery
  proof, and a bounded re-injection guard (Claude sets `stop_hook_active=true` on the injected
  turn — the harness keys its own cap off that, so an unread mailbox can't spin forever).
- Keystroke transport is demoted, not deleted, pending the soak + owner-decision (e).
