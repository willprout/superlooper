# Writing loop issues

You are an agent writing GitHub issues for the superlooper loop, in a planning conversation with
William. **William never writes issues himself — you do** (spec §2). The issue is the entire,
durable brief a fresh Claude session will build from: it is load-bearing machinery, not a note.
Every rule below exists because a specific thing went wrong when it was missing; the **Why** lines
name the incident so the rule is never quietly dropped.

You **draft** issues here. You do **not** apply `agent-ready` — that is William's word alone
(see `approval-protocol.md`). File issues carrying every label *except* `agent-ready`.

---

## The body format (parsed mechanically — H2 headings, exact)

The runner reads these four `##` sections by heading. Emit them verbatim; do not rename them.

```markdown
## Goal
<durable intent — what outcome, and WHERE the truth lives. Never assert current code facts.>

## Definition of done
- [ ] <machine-checkable wherever possible>
- [ ] <one checkbox per acceptance fact>

## Boundaries
<what this issue must NOT touch or decide — the scope fence the worker stays inside>

## Loop metadata
touches: frontend, api
blocked-by: #41, #52
parent: #40
```

- **`touches:`** — comma-separated area names (they must be `areas` keys in the repo's
  `.superlooper/config.json`). **Mandatory when the repo's config sets `touches_required: true`**
  (the eApp); optional elsewhere. Declared areas are **verified against the PR's actual diff at
  gate time** — a `touches:` that lies gets logged as a wander in the morning report, so declare
  honestly.
  *Why:* affinity scheduling (which issues may build in parallel) is only as safe as the
  declarations; unverified `touches:` is an honor system a wandering session silently breaks.
- **`blocked-by: #N, #M`** — only where an issue genuinely must not start until another's PR
  merges. The runner holds it until every referenced issue is **closed**.
- **`parent: #N`** — set **only on investigation children** (§ investigate, below). It is how the
  runner counts a parent's children and how the morning report groups them.

---

## One `type:` label, exactly one

Every issue declares its kind with exactly one type label. Zero or two is invalid and the runner
refuses it.

- **`type:build`** — a pre-scoped change. One issue → one PR. Must carry a real Definition of done.
- **`type:investigate`** — an undiagnosed problem. Output = a root-cause report **as an issue
  comment** + scoped child issues. **Zero PRs.** Children each carry `parent: #N` and are labeled
  `needs-william` (William approves every child before it runs — one label releases a series).
  Zero children is a legitimate finding: "nothing to do" is a valid root cause.
- **`type:diagnose-and-fix`** — a small bug: one session diagnoses **and** fixes, *if* the fix
  stays inside the issue's Boundaries. If the root cause is bigger than the boundaries — or (on
  the eApp) touches any bright-line area — it **splits** into approval-needing children instead of
  fixing, comments the diagnosis, and opens no PR.

---

## The rules (each cites the incident that motivates it)

### Thin-issue doctrine: point, never assert

State **durable intent** — Goal, Definition of done, Boundaries — and **point** at where truth
lives ("the shape is in `src/types/application.ts`"). Never **assert** a current code fact ("the
handler at line 220 does X"). Intent does not rot; assertions do.

**Why:** in run-20260701-1750 a repo-state assertion baked into a queued brief rotted while the
issue waited — the code moved, the assertion didn't, and the stale brief burned a night. The fix
is structural: an issue that only points can't go stale, because a fresh session re-reads current
`main` as its mandated first step (launch-time reconciliation). Assertions are the thing that
rots; don't write them.

### Definition of done: machine-checkable wherever possible

Prefer checkboxes a machine (or the worker's own tests) can verify — "endpoint returns 200 for X",
"the new column is nullable", "regression test `test_foo` passes" — over prose a human must judge.
**Why:** the per-PR gate is mechanical; a DoD the machine can't check pushes judgment back onto a
human touch the loop is built to avoid.

### `blocked-by` is a smell — justify it in the Goal, or re-scope

Dependency chains are where nights die. Prefer splitting into one issue or independently-landable
pieces over a `blocked-by` chain. If you must use it, justify the dependency in the Goal.

**Why:** in run-20260701-1750, sub-1 parked and its `blocked-by` held sub-4 and sub-5 ineligible
all night — three issues idle behind one stuck one. A chain multiplies a single failure into a
stalled queue. Re-scope so pieces land independently.

### Cross-PR promises become ISSUES, never code comments

If work in this issue implies work another PR must do — an interface the other side must honor, a
migration a later change depends on, a follow-up the fix defers — **file it as a new issue**
(labeled `needs-william`). Never leave it as a `// TODO`, a code comment, or a line in a PR body.

**Why:** this is the eApp's single costliest systemic miss across the autocode runs. A promise
written as a code comment is invisible to the queue: no one is scheduled to keep it, it merges,
and the contract silently breaks on `main`. An issue is the only durable, schedulable home for a
cross-PR promise. Make the promise a first-class queue item or it will not be kept.

### Bright-line work always splits

If an issue would touch a repo's declared bright-line area (config `bright_lines` — e.g. on the
eApp: the cascade engine, force-push, restricted-data journeys), it does not do that work inline:
it **splits** it into a child issue for William. The worker brief enforces this at build time; you
enforce it at write time by scoping the issue away from bright lines in the first place.
**Why:** bright lines are William-only decisions; an issue that quietly crosses one converts an
owner decision into an autonomous one — exactly what the whole design forbids.

### Never edit an approved Goal or DoD

Once an issue is approved (`agent-ready`), its Goal and Definition of done are frozen William-text.
Reconciliation **appends comments only**; a real scope change goes back through William (Gate 1).
**Why:** the approved text is what William signed off on. Editing it in place launders an
unapproved scope change past the intake gate.

---

## Filing the issue

Create it through `gh`, with every label the issue needs **except `agent-ready`**:

```bash
gh issue create \
  --title "<concise imperative title>" \
  --body-file <path-to-the-body-above> \
  --label "type:build" --label "priority:high"     # NEVER --label "agent-ready"
```

Then bring the drafted issues to William for approval in conversation. When he approves, the
approval step (`approval-protocol.md`) applies `agent-ready` and records the audit comment. Until
then the issue sits un-queued — which is correct: an unapproved issue must never build.
