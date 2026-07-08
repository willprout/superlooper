---
name: superlooper
description: The universal issue-loop workflow. Use when writing or approving loop issues, adopting a repo into the loop, running or operating the loop runner, reading the morning report, or preparing a dev→prod promotion. Agents write GitHub issues that William approves by his word (recorded as a label); a deterministic runner builds one fresh Claude session per approved issue in its own worktree; a mechanical per-PR gate merges to the dev mainline; promotion is William's batched judgment. Routes to references for issue-writing, approval, and ops.
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
see `docs/ADOPTING.md` in the superlooper source repo.

## Router — open the one reference for the job in front of you

| You are… | Read | For |
|---|---|---|
| **writing issues** for the loop (an agent, in a planning conversation) | `references/issue-writing.md` | the rigorous issue format, the thin-issue doctrine, `type:`/`touches:`/`blocked-by`, cross-PR-promises-become-issues, how to file with `gh` |
| **approving issues** (applying `agent-ready` after William's say-so) | `references/approval-protocol.md` | approval-by-conversation, the audit comment, the one standing-rule exception, never editing an approved Goal/DoD |
| **running or operating the loop** (start/stop, the morning report, a bounce, a freeze, promotion, launchd) | `references/runner-ops.md` | day-to-day operation + `parked`/`needs-william`/`expedite`/`preserve`/freeze semantics + the doctor checklist |
| **adopting a repo** into the loop (config, labels, branch protection) | `docs/ADOPTING.md` (superlooper source repo) + `references/runner-ops.md` | every config field, the label set, the adopt/doctor walkthrough |

These references are loaded **on demand** — open only the one the current job needs; do not read
all three at startup.

## The bright lines (true everywhere; the references carry the detail)

- **`agent-ready` is William's word.** No agent applies it without his explicit say-so in
  conversation, or a standing rule he himself defined (which carries its own distinct label,
  e.g. `auto-approved:nightly-red`). See `references/approval-protocol.md`.
- **Agents write issues; William never does.** The rigorous format is the mechanism against
  low-quality issues. See `references/issue-writing.md`.
- **No LLM in merge mechanics, ever, and no force-push.** The runner rebases-if-clean, else
  regenerates from the issue, else parks. Merge policy detail lives in `references/runner-ops.md`.
- **Promotion is a human decision.** No "must pass everything to promote" logic exists; the
  gate produces evidence, William decides. See `references/runner-ops.md`.
- **Standing global rules hold:** no metered/paid spend without William's explicit
  confirmation; every review is by a fresh agent that did not write the code; long-running work
  that finishes, stalls, or needs input sends a notification.
