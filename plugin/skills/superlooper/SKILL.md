---
name: superlooper
description: The universal issue-loop workflow. Use when writing or approving loop issues, adopting a repo into the loop, running or operating the loop runner, reading the morning report, or preparing a dev→prod promotion. Agents write GitHub issues that William approves by his word (recorded as a label); a deterministic runner builds one fresh Claude session per approved issue in its own worktree; a mechanical per-PR gate merges to the dev mainline; promotion is William's batched judgment. Routes the issue-writing job to the write-issue skill and adoption to the adopt skill; carries approval and ops as its own references.
---

# superlooper — the issue-loop workflow

Work exists only as GitHub issues. Agents write them in planning conversations; **William
approves by his word** (the runner records that as an `agent-ready` label — the label is the
record, never the decision). A small deterministic **runner** — no standing LLM session
anywhere, no LLM ever in merge mechanics — takes each approved issue, builds it in one fresh
Claude session in its own worktree, and merges to the dev mainline when mechanical gates pass:
the session's tests, a real-browser drive of the changed feature, a fresh-agent review, CI
green. **Promotion (dev→prod) is William's deliberate, batched, evidence-backed decision** —
never a mechanical switch.

**This is a universal skill.** It runs on any repo through one per-repo config
(`.superlooper/config.json`); nothing repo-specific is hardcoded here. Repo-specific facts (an
eApp's `ship.sh`, its bright-line areas, its required checks) enter only through that config —
see the published `docs/ADOPTING.md` contract at `~/.claude/skills/superlooper/docs/ADOPTING.md`
(the **adopt** skill wires up a repo from scratch).

## Router — for the job in front of you, open the one reference or invoke the one sibling skill

| You are… | Go to | For |
|---|---|---|
| **writing issues** for the loop (an agent, in a planning conversation) | the **write-issue** skill (`/superlooper:write-issue`) | the rigorous issue format, the thin-issue doctrine, `type:`/`touches:`/`blocked-by`, cross-PR-promises-become-issues, how to file with `gh` |
| **approving issues** (applying `agent-ready` after William's say-so) | `references/approval-protocol.md` | approval-by-conversation, the audit comment, the one standing-rule exception, never editing an approved Goal/DoD |
| **running or operating the loop** (start/stop, the morning report, a bounce, a freeze, promotion, launchd) | `references/runner-ops.md` | day-to-day operation + `parked`/`needs-owner`/`expedite`/`preserve`/freeze semantics + the doctor checklist |
| **adopting a repo** into the loop (config, labels, branch protection) | the **adopt** skill (`/superlooper:adopt`) | the bootstrap walkthrough — clone → gated `./bin/install.sh` → `doctor --stack` → `adopt`/`doctor`/`run` — plus the published `docs/ADOPTING.md` contract (every config field, the label set) |

Open only what the current job needs — a reference row's file, or a sibling-skill row's skill —
never all of them at startup. The two sibling skills (`write-issue`, `adopt`) install from this
same plugin; `approval-protocol.md` and `runner-ops.md` travel beside this router as its own
references.

## The bright lines (true everywhere; the references carry the detail)

- **`agent-ready` is William's word.** No agent applies it without his explicit say-so in
  conversation, or a standing rule he himself defined (which carries its own distinct label,
  e.g. `auto-approved:nightly-red`). See `references/approval-protocol.md`.
- **Agents write issues; William never does.** The rigorous format is the mechanism against
  low-quality issues. See the **write-issue** skill (`/superlooper:write-issue`).
- **No LLM in merge mechanics, ever, and no force-push.** The runner rebases-if-clean, else
  regenerates from the issue, else parks. Merge policy detail lives in `references/runner-ops.md`.
- **Promotion is a human decision.** No "must pass everything to promote" logic exists; the
  gate produces evidence, William decides. See `references/runner-ops.md`.
- **Standing global rules hold:** no metered/paid spend without William's explicit
  confirmation; every review is by a fresh agent that did not write the code; long-running work
  that finishes, stalls, or needs input sends a notification.
