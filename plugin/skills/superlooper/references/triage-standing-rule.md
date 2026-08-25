# The triage flight — standing rule and rubric

**Status: RULED — owner delegation, William in conversation 2026-08-20 → 08-25.** Per
`approval-protocol.md`, a standing rule William himself defines is the one sanctioned
non-conversational path to autonomous action, and it must carry its own distinct audit
trail. This document IS such a rule: it records the delegation; the triage-wave issues
build the mechanics against it. Precedent: `auto-approved:nightly-red`.

## The delegation

A triage flight (`t<N>`, launched by the runner at most once per day, and only when some
open issue changed since the last run's verdicts) MAY, autonomously:

1. **Fix format, labels, and metadata of UNAPPROVED issues** — the four-section body,
   exactly one `type:` label, honest `touches:`, any queue-lint defect.
2. **Merge duplicates among UNAPPROVED issues** — the absorber's body gains the content,
   the absorbed closes with a cross-reference comment.
3. **Close an UNAPPROVED issue as overtaken or dead**, citing commit-level evidence on
   `origin/main` in the close comment.
4. **Close an UNAPPROVED issue as a nit under the rubric below** — ONLY with a
   simultaneous entry in the repo's limitations ledger (the pinned ledger issue) naming
   the rubric line; the close comment links the ledger entry and vice versa.

It must NEVER:

- Touch an approved (`agent-ready`) issue's Goal, DoD, labels, or state — lint may FLAG;
  every action on an approved issue escalates to the owner. (The frozen-text rule; also
  mechanically backed: the PreToolUse deny wave rides every loop-launched session.)
- Close or merge anything whose content contains an owner decision — the
  `contains-owner-decision` verdict always escalates with a recommendation, never acts.
- Apply `agent-ready` or any `pre-authorized:*` label (denied at the call).
- Touch the session host: a `t<N>` session holds no fence token and drives no herdr
  surface.

## Per-issue verdicts (the 1b classifier, absorbed here)

Every unapproved issue gets exactly one verdict: `buildable` / `underspecified` /
`contains-owner-decision` / `duplicate-of-#N` / `overtaken` / `nit(<rubric-line>)`.
`underspecified` is fixed in place when the gap is mechanical, escalated when it is not.

## The default nit rubric (a per-repo config override may replace it)

An issue is a nit — closed to the ledger — when ANY line applies:

- **N1 Unreachable input:** the defect requires input no live code path can produce.
- **N2 Cosmetic-only:** wording/spacing in a surface no decision or alert reads.
- **N3 Cost exceeds consequence:** the fix's full lane cost exceeds the worst realized
  consequence, AND that consequence is loud when it happens (self-evidencing, not silent).
- **N4 Duplicate hardening:** a second guard for a class a merged class-killer already
  covers, adding no new detection.

**Never a nit:** anything silent-failure-shaped; anything touching approvals, merging, or
publishing; anything the owner has personally flagged.

## Accountability

- Every close carries a GitHub comment: the verdict, the evidence, and (for nits) the
  rubric line plus the ledger link. Closes are reversible; **a reopened issue is owner
  protest — the flight never re-closes it unless the body has since changed.**
- Kept issues get NO comment (silence = kept). Verdicts persist in the state home
  (issue → body-hash → verdict → date); an unchanged body is never re-litigated.
- Each run writes a markdown log in the state home's triage folder; the flight reads the
  last three run logs plus the verdicts file before acting. The morning report carries a
  triage section: merged / closed (with ledger count) / fixed / escalated, with links.
- Escalations are composed as the owner's sitting sheet: one line and a recommendation
  per item. Prioritization and what-to-build-next remain the owner's, always.

## Home

The flight runs in the repo's real checkout (not a worktree) so it sees what an
orchestrator sees, including gitignored working files. Discipline that home requires:
fetch first, and every staleness judgment is made against `origin/main`, never the
working tree; the working tree is read-only to the flight — its only writes are GitHub
and its own state-home folder. A repo whose gitignored overlay is sensitive may set the
per-repo config to worktree mode, accepting that overlay-aware triage is lost there.
