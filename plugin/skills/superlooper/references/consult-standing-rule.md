# The consult — standing rule

**Status: RULED — owner rulings, William in conversation 2026-11-04; built by #538 (questions)
and #539 (every other handback).** Per `approval-protocol.md`, a standing rule William himself
defines is the one sanctioned non-conversational path to autonomous action. This document records
that rule; the engine implements it (`skills/superlooper/skill/lib/consult.py`). Precedents:
`auto-approved:nightly-red` and `triage-standing-rule.md`.

William, on why: "ninety percent of the time that a session is frozen or parked or has a question
for me, I just tell my orchestrator for that project to unfuck the loop."

## What happens

Everything the command center would put in front of William about ONE issue goes first to a fork
of the orchestration session that **approved** that issue, not to William. The runner launches the
fork (`c<N>`, through the same launch path as a worker) in the approving session's own directory,
hands it a packet, and waits for one outcome file. The runner acts on that file; the fork never
acts itself.

**A worker's question (#538).** The worker stops to ask (its blocked file, #163):

- **ANSWER:** the runner posts the answer on the issue, marked clearly as the approving session's
  answer and not William's word, and relaunches the worker with the question and answer in its
  brief. Nobody applies `agent-ready`.
- **ESCALATE:** the question goes to William as it does today, carrying the fork's reasoning for why
  it is a real fork and its recommendation.

**Every other handback (#539).** William, 2026-11-04: "parks and freezes too, anything in the dash.
The freeze responder doesn't have context on what it's working on and why, so I would prefer the
resume link." The handbacks are:

- a **park**, whatever its cause — the gate's, a retry cap's, the two-question cap's, a launch that
  never delivered, and the progress-stall parks of #513;
- a **bounce** — a worker's Step-0 `BOUNCED:` memo;
- a **frozen** lane — latched frozen, and its one context-free nudge has had its ten minutes;
- a lane sitting at its own **dialog** past the bound (#151);
- a lane the session host reports **blocked** on a person past the same bound (#520).

The fork's outcome file picks ONE move. The runner carries it out with a verb it already has:

| Move | What the runner does |
|---|---|
| `RELAUNCH` | ends whatever session the issue still has, keeping its worktree, and relaunches the issue fresh through the normal launch queue, lane- and usage-bound, on its own `in-progress` stamp. An optional clarification is embedded in the new brief as binding, attributed to the approving session. |
| `RESUME` | re-enters the interrupted session's own conversation with `superlooper resume`, unattended, when the launch queue admits it. A conversation that cannot be re-entered relaunches fresh instead, carrying the clarification. |
| `END_AND_RELAUNCH` | ends the stuck session (the one ordered teardown), then relaunches as `RELAUNCH` does. |
| `WAIT` | holds the handback for its stated minutes (at most 120), then lets it re-derive. If it still stands, that is a new handback. |
| `FOLLOW_UP` | files a follow-up issue, unapproved: `needs-owner`, one `type:`, `source:consult` and a `## Loop metadata` section. The handback itself then reaches William as today, naming the follow-up. |
| `ESCALATE` | the handback reaches William exactly as today: the park, the bounce, the dashboard's frozen lane, the dialog or host-block ALERT. It carries the fork's reasoning and recommendation as a marked comment of its own. |

Each move the runner carries out is posted (a `WAIT` is journaled) as the approving session's
decision, never William's word, and lands in the morning report.

## William's rulings (2026-11-04)

1. **The consulted session judges its own authority.** It has the context of what William wanted
   when the work was shaped. If it knows what his answer would likely be, it answers (or makes the
   move) and the work keeps going. If it is a fork that was never discussed, or a deviation from
   what was approved, it escalates to William. Those escalations are what he should be seeing in
   the dashboard.
2. **Consult the session that approved the issue**, using the approver stamp `superlooper approve`
   records (#537), not the session that wrote it. A follow-up filed by a build session may not have
   the broader context of what William wants for the project.
3. **If there is no approver stamp or no transcript to fork, the question or handback goes to
   William exactly as it does today.**
4. **Same machine only for now:** the runner and the approving session's transcript are on one
   machine. Revisit if builders split across machines.
5. **The consult has context on the issue's previous handbacks.** The packet carries every earlier
   question and answer, park, freeze, bounce and consult on the issue, and how each was resolved.
6. **Parks and freezes too — anything in the dash** (#539). Every issue-level handback goes to the
   approving session first, not only questions.

## What a consult may do

- Read: the packet, the approving checkout, `origin/main`, the issue's branch and PR, GitHub.
- Re-check the current state before deciding (`git fetch`, reading code and the PR), because its
  memory dates from the approving session's last compaction.
- Write exactly one file: its outcome file, in the format its brief gives — `ANSWER` or `ESCALATE`
  for a question; one of the six moves above for any other handback.

## What a consult may never do

- Apply `agent-ready` or any `pre-authorized:` grant, move any label, or post anything. The runner
  does all of that from the outcome file. (The loop's tool-call hook denies these at the call.)
- Edit the approved Goal, Definition of done or Boundaries, or the issue at all.
- Touch code: no edits, commits or pushes. It runs in the owner's own working tree, which it reads
  and never changes.
- Type into a worker's pane, or drive any other session.
- Answer a question, or make a move, that would change the approved Goal, Definition of done or
  Boundaries. That is by definition an escalation.

**The move set is closed.** The runner refuses any move outside it, and the handback reaches
William naming what was asked for. These are never moves, whatever a fork writes: a hand-merge, a
force-push, applying `agent-ready`, a `pre-authorized:` grant, an engine publish, a
`.superlooper/**` edit, or keystrokes into a worker's pane. The D13 rule stands: only William's
direct word in a supervised session may hand-merge.

**A bright line always escalates.** A park on a bright line, meaning a diff that reaches a referee
path (`.superlooper/**` or `.github/workflows/**`), reaches William whatever the fork decides. The
fork is still consulted, and the move it proposed rides along as its recommendation.

## When it falls back to William

Each of these sends the question or handback to William exactly as it does today, with the reason
journaled:

- the issue has no approver stamp (an unstamped approval, or the dashboard's Approve button);
- the stamp names another machine;
- the stamped session's transcript is not on this machine;
- the fork fails to launch, or exits without writing an outcome;
- the fork writes no outcome within the bounded wait (20 minutes), or one nobody can read;
- for a question: the consult cannot start within the same 20 minutes, because the usage ceilings
  or a machine-wide launch hold kept it waiting;
- for any other handback: a usage ceiling or a machine-wide launch hold keeps the consult from
  starting at that moment. A handback is already a safe state William can act on, so it is not
  held back to wait for one;
- for any other handback: the issue was never launched. The approver stamp is read when an issue
  first launches, from the comments its brief already fetched, so no GitHub read is spent per
  tick. Lanes launched before #539 are read once while in flight. A queued issue parked before
  its first launch, for a missing `touches:` declaration, reaches William as today; no move could
  supply that declaration anyway.

## Accountability and limits

- The two-question cap per issue holds whoever answered: a consult's answer counts as one of the
  two, and a third question parks the issue for William as a scoping problem.
- **The handback cap.** A third handback on the same issue goes to William regardless, with the
  consult history attached. It counts the handback consults since the issue's approval: parks,
  bounces, freezes, dialogs and host blocks. A worker's questions keep their own two-question cap.
  Three is the default. William changes it with `session.handback_cap` in the repo's config; `0` or
  `1` sends every handback straight to him, exactly as today. His re-approval starts the count
  again, whether it rebuilds the issue or resumes a finished build at the gate.
- A consult holds no build lane, so it never takes one from queued work. While it considers a
  handback, the issue's status is untouched: a frozen lane keeps its lane and a gating issue its
  territory, and nothing else acts on the issue until the fork decides. A consult counts toward the
  usage ceilings like any session.
- Once the question or handback has reached William, the consult is over: its session is ended at
  that moment, and nothing it writes afterwards can act.
- An owner who closes the issue mid-consult has dropped it: the consult is ended with it.
- Every consult outcome appears in the morning report with a link to the issue: answered, moved,
  escalated or fell back.
- Each consult leaves a transcript of its own in the approving session's project folder. In
  `/resume` it carries the consult's own session name (its `c<N>` id), so it can't be mistaken for
  the original conversation. The original transcript is never modified (#536 measured this).

## What stays with the watchdog

Machine-level faults that belong to no single issue stay with the watchdog and its repair session
(`superlooper watchdog`, the sl-debugger playbook): a stalled or dead runner, and a GitHub or usage
outage. So does a session whose account login died in-process, because the fix is the account's,
not the issue's. A consult is launched only where there is an issue and an approver.
