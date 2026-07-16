# The approval protocol

`agent-ready` is William's word. This is the single most important bright line in the whole system,
and it is enforced by discipline here and by absence everywhere else (no code path applies
`agent-ready` on its own judgment).

## Approval-by-conversation (spec §2, verbatim intent)

> **Approval-by-conversation.** In a planning session, William saying "these issues are approved"
> IS the approval; the agent then applies the `agent-ready` labels for him. The label records the
> approval; it is not itself the approval. The only bright line: no agent labels work `agent-ready`
> absent his explicit say-so in conversation (or a standing auto-approval rule he himself defines).
> *"I don't want some agent between now and when this gets actually built deciding that that's not
> possible."*

Read that literally:

- **The word is the approval.** William's say-so in the conversation is the act that authorizes
  the work. Nothing else is.
- **The label only records it.** `agent-ready` is a durable receipt of a decision William already
  made out loud — it is not the decision, and applying it is a clerical act, not a judgment.
- **You never supply the judgment.** You do not decide an issue is "obviously fine," "clearly what
  he'd want," or "low-risk enough." Absent his explicit say-so, the label does not go on. If you
  are unsure whether he approved a given issue, he didn't — ask.

## What you do when William approves

When William approves issues in conversation (e.g. "yes, those three are approved"):

1. **Apply `agent-ready`** to each issue he named — and only those.
2. **Append an audit comment** to each, recording the human decision behind the label:

   ```
   Approved by William in conversation, 2026-07-02.
   ```

   (Use the real date.) The comment is the paper trail that ties the label back to the moment of
   approval — the morning report and any future audit read it to distinguish work William's word
   released from work a standing rule released.

That is the entire ceremony. Approval is one touch: his word, your label, your audit note.

## The one exception: a standing rule William himself defined

The bright line permits exactly one non-conversational path to a queued issue: **a standing
auto-approval rule that William himself defined in advance.** Such a rule must carry its **own
distinct label** — never `agent-ready` — so the audit trail always shows *how* an issue entered
the queue.

The worked example, and the only one that exists today:

- **`auto-approved:nightly-red`** — when the nightly QA run goes red, the runner auto-files a
  `type:diagnose-and-fix` issue scoped strictly to *restoring green*, labeled
  `auto-approved:nightly-red`. This is legitimate because **William defined the rule ahead of
  time** (spec §4.4) and the label is distinct, so the morning report shows exactly which work
  entered by standing rule versus by his word. No agent approved anything — the rule did, and
  William wrote the rule.

Any future standing rule follows the same shape: William defines it, it is scoped narrowly, and it
gets its own distinct label. An agent never invents a standing rule, and never reaches for
`agent-ready` as a shortcut for one.

## Pre-authorizing a foreseeable owner-gate stop (issue #165)

Some issues can *only ever* end in a needs-owner stop at the merge gate — and it is knowable **at
approval**, not just at the finish line. The clearest case: a diff that will touch a **referee
path** (`.superlooper/**` or `.github/workflows/**`). The gate can never auto-merge such a diff; it
parks it for William. If that park is foreseeable when he approves, making him wait until 3am to
grant the same word he could grant now is pure waste — the issue burns a lane just to reach a stop
it was always going to reach.

So: **when you draft or bring up an issue whose declared `touches:` reach a referee path (or that
otherwise, by its Goal/Boundaries, will certainly touch `.superlooper/**` or
`.github/workflows/**`), surface that foreseeable owner-gate stop to William at approval** and let
him **pre-authorize** it. Pre-authorization is *still his word* — just granted earlier.

- **The word is still the approval.** You never decide a referee-path change is fine. You surface
  the foreseeable stop ("this issue's `touches: loop_rules` resolves to `.superlooper/**`, so the
  gate will park it for you — do you want to pre-authorize the merge now?") and record only what he
  says. Absent his explicit say-so, you do **not** apply the label, exactly as with `agent-ready`.
- **Record it as a distinct label: `pre-authorized:referee`.** Never fold this into `agent-ready`
  (the same discipline as `auto-approved:nightly-red`): a distinct label keeps the audit trail
  showing *how* a referee touch was cleared. Apply it alongside `agent-ready` only when he
  pre-authorizes, and append an audit comment recording the decision:

  ```
  Referee-path pre-authorization granted by William in conversation, 2026-07-16.
  ```

- **What the label does, mechanically.** The launch gate (`scheduler.launch_ok`) refuses to launch
  a foreseeable-referee issue *unattended* unless it carries this label — so an un-pre-authorized
  one **waits for William** instead of burning a lane. And the merge gate (`gate.gate_decision`)
  consumes the label to **merge** a referee-touching diff instead of re-parking it. It consumes
  *only* the referee stop: every other gate (report, review evidence, checks, mergeability) still
  applies, so a pre-authorized PR merges only when everything else is green too.
- **The bright line is untouched for un-authorized diffs.** Without the label, *any* diff that
  reaches `.superlooper/**` or `.github/workflows/**` still parks needs-william — no label, no
  referee merge, ever. An agent never applies the label on its own judgment; only William's explicit
  say-so does. (Note: the launch-gate hold above only fires where the referee touch is *mechanically*
  foreseeable — the issue's declared `touches:` resolve to a referee glob, which needs the repo to
  declare a referee area. Where it is not declarable, the issue simply launches and parks at the
  merge gate as before; the bright line still holds there.)
- **The grant is per-issue and coarse — so read the scope before you grant it.** The label
  authorizes referee-path touches *for this issue* — both `.superlooper/**` and
  `.github/workflows/**` — and the merge gate keys on the label's *presence*, not on which referee
  file the diff lands in. So if a pre-authorized issue's worker touches a referee path beyond the one
  you pictured, that still auto-merges under the label (every referee touch is named in the merge
  journal, and a touch outside the issue's declared `touches:` is flagged as a wander in the morning
  report — but the merge itself is unattended, and a referee change is *live on merge*, with no
  publish backstop). Grant the label only to an issue whose Goal/Boundaries you have read and whose
  referee reach you accept; scope the issue tightly if you want a tighter grant.

## After approval: the Goal and DoD are frozen

Once an issue is `agent-ready`, its **Goal and Definition of done are William-approved text and are
never edited** — not by you, not by the worker that builds it, not during reconciliation. Drift is
handled by *appending comments* (launch-time reconciliation) or by bouncing the issue back to
William with a proposed amendment he approves yes/no. A genuine scope change goes back through
Gate 1 (a new conversation, a new approval).

**Amend by commenting before launch.** A comment William posts on an issue after approving it (but
before it launches) IS reached: at launch the brief embeds the live comment thread, and the runner
renders **the owner's** comments as binding amendments to the Goal/DoD (everyone else's are shown as
attributed context only — never instructions, so approval-by-William's-word can't be diluted by an
agent or bot comment). The approved Goal/DoD text is still never rewritten; the amendment rides
alongside it. A relaunch/regenerate rebuilds the brief, so a later comment is picked up fresh.

**Why:** editing an approved Goal or DoD in place launders an unapproved scope change past the very
gate that exists to catch it. The approved text is the thing William signed; keep it immutable.
