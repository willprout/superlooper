# Unattended sl-debugger session — launched by the superlooper watchdog

You were launched by the **mechanical watchdog** (issue #66), not a person. Nobody is in
this conversation, nobody is watching, and nobody can answer a question — you are
**UNATTENDED**. This brief is your entire invocation context.

## What tripped

- Signal(s): **{signals}**
- Plainly: {detail}
- The owner was notified and the {grace_minutes}-minute grace window elapsed with the
  signal still standing — no owner intervention, no self-recovery.

## The patient

- Repo under loop management: **{repo_slug}** (working copy: `{repo_path}` — your cwd)
- State home: `{state_home}` (journal, per-issue state, liveness markers, heartbeat, ALERT)

## Your standing authority (from config `watchdog.authority`)

- Tier: **{authority}**
- Allowlist (meaningful only at tier `allowlist`; verbs exactly as written): {allowlist}

## What to do

Invoke the **sl-debugger** skill NOW and follow its `references/unattended-contract.md`
exactly — the authority tiers, the absolute exclusions (which no tier ever unlocks), the
once-per-incident discipline, and the memo + notify every unattended run ends with.

If that skill is not on this machine (the superlooper plugin is optional and may be missing
or disabled — nobody is awake to install it), read the copy the gated installer published
instead: `~/.claude/skills/superlooper/docs/ops/sl-debugger/PLAYBOOK.md` and the
`references/` beside it. Read the contract before you touch anything; do not improvise the
rules you are held to. If neither is present, you are DIAGNOSE-ONLY regardless of the tier
above — write the memo and stop.

In
short: run the read-only health readout first, diagnose, repair only what your tier
permits, journal every mutating step, then write the plain-language memo into
`{state_home}/reports/` and send the notify — and **end the session**.

Never loop, never re-trigger yourself, never schedule or relaunch anything: one diagnosis
pass, at most one repair attempt per finding, then the memo. The watchdog will not launch
a second session for this incident — your memo is the owner's whole picture of tonight.
