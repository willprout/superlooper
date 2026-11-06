# The consult — standing rule

**Status: RULED — owner rulings, William in conversation 2026-11-04; built by #538.** Per
`approval-protocol.md`, a standing rule William himself defines is the one sanctioned
non-conversational path to autonomous action. This document records that rule; the engine
implements it (`skills/superlooper/skill/lib/consult.py`). Precedents: `auto-approved:nightly-red`
and `triage-standing-rule.md`.

William, on why: "ninety percent of the time that a session is frozen or parked or has a question
for me, I just tell my orchestrator for that project to unfuck the loop."

## What happens

When a build worker stops to ask a question (its blocked file, #163), the first one asked is a
fork of the orchestration session that **approved** the issue, not William. The runner launches
the fork (`c<N>`, through the same launch path as a worker) in the approving session's own
directory, hands it a packet, and waits for one outcome file. The runner acts on that file; the
fork never acts itself.

- **ANSWER:** the runner posts the answer on the issue, marked clearly as the approving session's
  answer and not William's word, and relaunches the worker with the question and answer in its
  brief. Nobody applies `agent-ready`.
- **ESCALATE:** the question goes to William as it does today, carrying the fork's reasoning for why
  it is a real fork and its recommendation.

## William's rulings (2026-11-04)

1. **The consulted session judges its own authority.** It has the context of what William wanted
   when the work was shaped. If it knows what his answer would likely be, it answers and the work
   keeps going. If the question is a fork that was never discussed, or a deviation from what was
   approved, it escalates to William. Those escalations are the questions he should be seeing in
   the dashboard.
2. **Consult the session that approved the issue**, using the approver stamp `superlooper approve`
   records (#537), not the session that wrote it. A follow-up filed by a build session may not have
   the broader context of what William wants for the project.
3. **If there is no approver stamp or no transcript to fork, the question goes to William exactly
   as it does today.**
4. **Same machine only for now:** the runner and the approving session's transcript are on one
   machine. Revisit if builders split across machines.
5. **The consult has context on the issue's previous handbacks.** The packet carries every earlier
   question and answer, park, freeze and bounce on the issue, and how each was resolved.

## What a consult may do

- Read: the packet, the approving checkout, `origin/main`, the issue's branch and PR, GitHub.
- Re-check the current state before deciding (`git fetch`, reading code and the PR), because its
  memory dates from the approving session's last compaction.
- Write exactly one file: its outcome file, `ANSWER` or `ESCALATE`, in the format its brief gives.

## What a consult may never do

- Apply `agent-ready` or any `pre-authorized:*` label, move any label, or post anything. The runner
  does all of that from the outcome file. (The loop's tool-call hook denies these at the call.)
- Edit the approved Goal, Definition of done or Boundaries, or the issue at all.
- Touch code: no edits, commits or pushes. It runs in the owner's own working tree, which it reads
  and never changes.
- Type into a worker's pane, or drive any other session.
- Answer a question whose answer would change the approved Goal, Definition of done or Boundaries.
  That is by definition an escalation.

## When the question falls back to William

Each of these sends the question to William exactly as it does today, with the reason journaled:

- the issue has no approver stamp (an unstamped approval, or the dashboard's Approve button);
- the stamp names another machine;
- the stamped session's transcript is not on this machine;
- the fork fails to launch, or exits without writing an outcome;
- the fork writes no outcome within the bounded wait (20 minutes), or one nobody can read.

## Accountability and limits

- The two-question cap per issue holds whoever answered: a consult's answer counts as one of the
  two, and a third question parks the issue for William as a scoping problem.
- A consult holds no build lane, so it never takes one from queued work. It counts toward the usage
  ceilings like any session: an over-ceiling or held machine waits rather than starting one.
- Every consult outcome — answered, escalated or fell back — appears in the morning report with a
  link to the issue.
- Each consult leaves a transcript of its own in the approving session's project folder, named
  `superlooper c<N>` in `/resume`, so it can't be mistaken for the original conversation. The
  original transcript is never modified (#536 measured this).
- Questions only. Parks, bounces and frozen lanes are a later issue's.
