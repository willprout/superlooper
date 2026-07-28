"""Which CLOSED issues are only PRETENDING to be done? PURE selection from pre-fetched data — no
gh, no subprocess, no clock — so the safety contract is a unit-test table (tests/test_closures.py).
`superlooper doctor` renders the findings; `superlooper janitor` turns each one into a proposed
REOPEN the owner approves (lib/janitor.py). Nothing here reopens or edits anything.

The trusted-signal failure this closes (issue #229, ledger class 1). On 2026-07-16 a ledger
DOCUMENTATION commit whose message read "…fixes #189/#190…" auto-closed #189 — an approved,
priority:high fix for the previous night's draft-promotion regression that was never built. The
tracker said COMPLETED; the two files the fix names were untouched; the regression vector stayed
open for a day while recorded as fixed. Nothing noticed, because CLOSED-as-COMPLETED is believed
without asking what closed it — and the loop's own habit of writing issue references into ledger
and memo commits makes a recurrence likely, not exotic.

What GitHub actually gives us to judge on: every close records a `ClosedEvent`, and its `closer` is
one of exactly three things.
  * a **PullRequest** — the healthy loop path (`Closes #N` in a merged PR body). The gate ran, CI
    was green, the work is on the mainline. Never flagged.
  * **nothing at all** — the owner closed it by hand from the UI or the CLI (the janitor's own
    approved closes land here too). His word is the highest authority there is. Never flagged.
  * a **Commit** — a commit message keyword closed it. That is the incident class, with ONE
    exemption: if a merged PR carries that commit, the work still went through the gate (a squash
    or merge commit whose message happened to carry the keyword), so it is not a bare keyword close.

Fail direction, stated once and obeyed everywhere below: a finding becomes a proposal to REOPEN
the owner's own closed work, so every wrong-typed, missing or unprovable input flags NOTHING. We
flag only what we can prove: reason COMPLETED, closer provably a commit, and a provably empty list
of merged PRs carrying it. "Could not read it" is never "it is broken".

Scope note, deliberate: this judges EVERY closed issue in the repo, not just the loop's own. In a
repo whose humans legitimately close issues from commit messages, that reads as noise rather than
as debris — which is exactly why the doctor renders it as a WARN and the janitor only ever
proposes. `evidence()` is the half that lets the owner tell the two apart at a glance: an approved
loop fix that was never built leaves NO `sl/i<N>` branch and NO `sl/i<N>` PR anywhere.
"""
import re

COMPLETED = "COMPLETED"

# sl/i<N>[-slug][...] -> N. Deliberately the SAME convention as janitor.branch_issue_num (and
# brief.branch_for): generations (sl/i5-x-r2) belong to issue 5, and a bare `sl/i5` counts, but
# `sl/i1890-…` must never read as issue 189's branch. Mirrored rather than imported so this module
# stays a leaf with no sibling dependency (janitor imports closures, never the other way).
_BRANCH_NUM_RE = re.compile(r"^sl/i(\d+)(?:-|$)")


def _pos_int(v):
    """A real positive int (bool excluded — `True` is not PR #1), else None."""
    return v if type(v) is int and v > 0 else None


def _str(v):
    """A str as itself, anything else as "" — a missing title or headline renders as empty, never
    as the word "None" in an audit comment the owner will read."""
    return v if isinstance(v, str) else ""


def branch_issue_num(branch):
    """sl/i<N>-<slug>[...] -> N, else None (janitor.branch_issue_num's convention)."""
    if not isinstance(branch, str):
        return None
    m = _BRANCH_NUM_RE.match(branch)
    return int(m.group(1)) if m else None


def _bare_commit_close(rec):
    """The one judgment: is `rec` provably an issue closed as COMPLETED by a bare commit-message
    keyword? Returns the closer dict when it is, else None. Every branch that cannot PROVE it
    returns None — see the module docstring's fail direction."""
    if not isinstance(rec, dict):
        return None
    if rec.get("stateReason") != COMPLETED:
        return None                          # NOT_PLANNED never claimed the work was done
    closer = rec.get("closer")
    if not isinstance(closer, dict):
        return None                          # None = the owner's own hand; anything else unreadable
    if closer.get("type") != "commit":
        return None                          # a merged PR closed it: the gate ran
    if not _str(closer.get("oid")):
        return None                          # can't name the commit -> can't evidence the finding
    carriers = closer.get("merged_prs")
    if not isinstance(carriers, list):
        return None                          # unprovable -> fail closed
    if any(_pos_int(p) is not None for p in carriers):
        return None                          # the commit rode a merged PR: gated, not a bare close
    return closer


def flagged(closed_issues):
    """Every issue in `closed_issues` that is CLOSED-as-COMPLETED behind a bare commit keyword,
    deduped and sorted by issue number (deterministic; no input mutated).

    `closed_issues` is gh.closed_issue_closers()'s normalized list — each entry
    {"number", "title", "stateReason", "closedAt", "closer"}, where `closer` is
    {"type": "pull_request", "number", "merged"} / {"type": "commit", "oid", "headline",
    "merged_prs": [merged PR numbers carrying that commit]} / None (an owner-closed issue).

    Each finding: {"num", "title", "commit", "headline", "closedAt", "why"} — `why` NAMES the
    closing commit, because it becomes the janitor's audit comment on the reopened issue and an
    audit trail that cannot be traced back to the offending commit is not evidence.
    """
    out, seen = [], set()
    for rec in closed_issues if isinstance(closed_issues, list) else []:
        closer = _bare_commit_close(rec)
        if closer is None:
            continue
        num = _pos_int(rec.get("number"))
        if num is None or num in seen:
            continue
        seen.add(num)
        oid = _str(closer.get("oid"))
        headline = _str(closer.get("headline"))
        out.append({
            "num": num,
            "title": _str(rec.get("title")),
            "commit": oid,
            "headline": headline,
            "closedAt": _str(rec.get("closedAt")),
            "why": "closed as COMPLETED by commit %s (%s) — a commit-message keyword closed it "
                   "and no merged PR carries that commit, so nothing shipped through the gate"
                   % (oid[:8], headline or "no commit subject"),
        })
    out.sort(key=lambda f: f["num"])
    return out


def evidence(num, prs=(), branches=()):
    """One plain sentence: did an `sl/i<num>` branch or PR ever exist? — the third column of the
    doctor's listing (issue #229's DoD), and the fact that separates the two ways an issue can be
    keyword-closed. An approved loop fix that was never BUILT leaves no trace anywhere: no PR, no
    branch. A human's ordinary "fixes #N" commit in a repo that does not use the loop leaves none
    either, but there the closing commit itself is the work — which is why this is a sentence for
    the owner to read, never a verdict this module draws.

    prs       gh.sl_head_prs()'s list — {"number", "state", "headRefName"} for every PR the repo
              has had on an sl/* head, any state.
    branches  gh.remote_branches()'s {name: tip} — branches on the remote RIGHT NOW. A deleted
              branch leaves nothing to find, so its absence is reported as "none on the remote
              now", never as "none ever existed".

    Wrong-typed inputs render as "nothing found" and never raise — this runs inside a doctor line."""
    pr_bits = []
    for p in prs if isinstance(prs, list) else []:
        if not isinstance(p, dict):
            continue
        if branch_issue_num(p.get("headRefName")) != num:
            continue
        pnum = _pos_int(p.get("number"))
        if pnum is None:
            continue
        state = _str(p.get("state")) or "?"
        pr_bits.append("#%d (%s, %s)" % (pnum, state, _str(p.get("headRefName"))))
    live = sorted(b for b in (branches if isinstance(branches, dict) else {})
                  if branch_issue_num(b) == num)

    if not pr_bits and not live:
        return ("no sl/i%d PR was ever opened and no sl/i%d branch is on the remote now — "
                "nothing was ever built for it" % (num, num))
    parts = []
    if pr_bits:
        parts.append("PR%s %s" % ("s" if len(pr_bits) > 1 else "", ", ".join(sorted(pr_bits))))
    else:
        parts.append("no sl/i%d PR was ever opened" % num)
    if live:
        parts.append("branch%s %s still on the remote" % ("es" if len(live) > 1 else "",
                                                          ", ".join(live)))
    else:
        parts.append("no sl/i%d branch on the remote now" % num)
    return "; ".join(parts)
