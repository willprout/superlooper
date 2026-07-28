"""Which GitHub-side debris may `superlooper janitor` PROPOSE? PURE selection from pre-fetched
data — no gh, no subprocess, no clock — so the safety contract is a unit-test table
(tests/test_janitor.py). The CLI (skill/bin/superlooper `janitor`) fetches, lists, takes the
owner's y/N, and executes what he approved.

The janitor is the founding spec's §8 V2 roadmap item (issue #62): as the loop runs, debris
accumulates that no existing mechanism owns — stale `sl/*` remote branches whose PRs merged or
were superseded, PRs labeled `superseded` left open by design (§C.4 6b), and parked /
needs-william issues gathering dust. This module only ever PROPOSES; acting on a proposal is
William's word, like `agent-ready` (the same propose/approve split as tidy). Nothing is ever
auto-closed or auto-deleted.

A fourth kind joined them on the owner's amendment of 2026-07-17 (issue #225): **metadata repair**
for a mechanically-invalid issue — one the runner can never launch for want of a `type:` label or a
parseable `## Loop metadata`. The 2026-07-16 audit found 25 of 35 open issues in that state and
they were repaired by hand, once; his ruling was that the janitor's existing propose/approve
contract fits the fix exactly, "so the manual batch-repair run becomes a janitor tap, never a hand
job again."

That kind carries one rule the other three did not need: **the janitor never invents a value.**
Which KIND an issue is, and which TERRITORY it touches, are judgment calls — and there is no
standing LLM seat to make them (the constitution's first bright line), nor should there be. So the
sweep never guesses. It offers the closed set of values the repo ITSELF declares — the three type
kinds, the repo's own `areas` — as mutually-exclusive ALTERNATIVES sharing a `choose_group`, and
the owner's tap picks one. The CLI's bulk `y/N` path must never execute a grouped alternative (it
would apply all three type labels and manufacture the very `type_duplicate` it was fixing); only an
explicit per-key tap does. The ONE ungrouped metadata fix is the case where nothing is being chosen
at all: the author already wrote the `touches:` value and only the heading above it is missing.

Safety, stated as code below and pinned by tests:
  * A branch is proposed ONLY when its work provably landed or was provably replaced: its PR
    MERGED, or its PR CLOSED and labeled `superseded`. A branch with no PR, a refused PR lookup,
    an OPEN PR (even a superseded one — closing that PR is its own proposal, and the branch
    follows a LATER sweep once the PR is closed; deleting a branch under an open PR would
    force-close the PR server-side), or a closed-unmerged PR without `superseded` is NEVER
    proposed. Never propose deleting an unmerged branch's work.
  * ...and only when the branch's CURRENT tip is the PR's last-known head (headRefOid): commits
    pushed after the PR merged/closed would be lost with the branch, so a moved or unprovable
    tip is never proposed (cross-review round 1, M3).
  * In-flight and mid-gate work (actions.TERRITORY_CLAIM_STATUSES — imported, never re-invented,
    same as tidy) is mechanically excluded by TWO independent paths: the issue number parsed from
    the branch name AND the loopstate-recorded branch. A wrong-typed loopstate record for an
    issue EXCLUDES that issue (can't prove it idle -> don't touch it).
  * Age is proven, never guessed: a parked issue with a missing/unparseable updatedAt is skipped.
  * Every wrong-typed input fails CLOSED to "propose nothing" — the fail-open-on-wrong-typed
    defect class pointing the safe way: when in doubt, do NOT propose.
"""
import calendar
import re
import time

import actions
import queue_lint

BRANCH_PREFIX = "sl/"
# The label the runner leaves on a PR replaced by a rebuild (§C.4 6b) and the park-family labels
# the owner's attention queue lives under. Names, not statuses: these are GitHub-side. Both the
# current `needs-owner` and the legacy `needs-william` are recognized so a repo adopted before the
# operator-name rename (issue #58) — or one mid-migration — keeps being read correctly.
SUPERSEDED_LABEL = "superseded"
PARK_LABELS = ("parked", "needs-owner", "needs-william")

_BRANCH_NUM_RE = re.compile(r"^sl/i(\d+)(?:-|$)")
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def branch_issue_num(branch):
    """sl/i<N>-<slug>[...] -> N (brief.branch_for's convention), else None. Generations
    (sl/i5-x-r2) parse to the same issue — a live rebuild shields every generation."""
    if not isinstance(branch, str):
        return None
    m = _BRANCH_NUM_RE.match(branch)
    return int(m.group(1)) if m else None


def parse_epoch(iso):
    """GitHub's UTC timestamp ('2026-07-01T12:00:00Z') -> epoch float, else None. Exactly the
    one format the API emits — anything else fails closed (age must be proven, never guessed)."""
    if not isinstance(iso, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(iso, _ISO_Z)))
    except ValueError:
        return None


def _label_names(raw):
    """Label names from gh's [{'name': ...}] shape (a bare-string list also tolerated).
    Wrong-typed -> empty (fail closed: an unprovable label is absent)."""
    if not isinstance(raw, list):
        return frozenset()
    out = set()
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out.add(entry["name"])
        elif isinstance(entry, str):
            out.add(entry)
    return frozenset(out)


def _iid_num(iid):
    """i<N> -> N, else None (mirrors tidy._iid_num — a self-contained pure parser)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _exclusions(ls_issues):
    """(issue numbers, branch names) the janitor must never touch: every loopstate lane whose
    status is in-flight or mid-gate — PLUS every lane whose record is wrong-typed (can't prove
    it idle -> excluded). ls_issues must already be a dict — propose() fails the WHOLE sweep
    closed when the exclusion source itself is wrong-typed."""
    nums, branches = set(), set()
    for iid, ist in ls_issues.items():
        num = _iid_num(iid)
        if num is None:
            continue
        if not isinstance(ist, dict):
            nums.add(num)                       # unreadable record: fail closed, exclude
            continue
        status = ist.get("status")
        # isinstance FIRST: an unhashable wrong-typed status must be skipped, never raise.
        if isinstance(status, str) and status in actions.TERRITORY_CLAIM_STATUSES:
            nums.add(num)
            branch = ist.get("branch")
            if isinstance(branch, str) and branch:
                branches.add(branch)
    return nums, branches


def _pr_int(v):
    """A real positive-int PR/issue number (bool excluded), else None."""
    return v if type(v) is int and v > 0 else None


def _metadata_proposals(lint_issues, areas, touches_required, ex_nums):
    """The metadata-repair proposals for one sweep (issue #225), sorted by issue then key.

    A proposal is emitted ONLY for a defect whose repair is a value the janitor did not invent:

      * a missing `type:` label            -> one `add-label` alternative per KIND (a menu)
      * a missing/undeclared `touches:`    -> one `set-body` alternative per DECLARED AREA, plus
                                              the explicit-unknown-scope `*` (a menu)
      * a `touches:` line the parser misses -> ONE determined `set-body` fix, ungrouped, because
                                              the author already wrote the value

    Everything else is named by `doctor --repo` and left alone. Two `type:` labels: which one to
    remove is the owner's call, and the janitor has no verb that could be right. An UNDECLARED area
    name (`touches: plugin` in a repo with no `plugin` area) likewise — and there the reason is
    sharper than judgment. The correct repair may well be to add that area to
    `.superlooper/config.json`, which is a bright-line file no automatic path may write. Offering
    to overwrite the author's declaration instead would be the janitor picking the OTHER answer,
    silently, by deleting words it did not write.

    Each `set-body` alternative carries the EXACT body it would write, computed here — nothing is
    re-derived at execute time, so what the owner approves is what lands (and reconcile's fresh
    re-derivation recomputes it against the CURRENT body, so a mid-wait edit is never clobbered
    with a stale one)."""
    out = []
    for iss in lint_issues if isinstance(lint_issues, list) else []:
        num = _pr_int(iss.get("number")) if isinstance(iss, dict) else None
        if num is None or num in ex_nums:
            continue
        body = iss.get("body") if isinstance(iss.get("body"), str) else ""
        title = iss.get("title") if isinstance(iss.get("title"), str) else ""
        for d in queue_lint.lint_issue(iss, areas=areas, touches_required=touches_required):
            code, why = d["code"], queue_lint.describe(d)
            if code == "type_missing":
                for kind in d["choices"]:
                    label = "type:%s" % kind
                    out.append({"kind": "metadata", "key": "meta:%d:type=%s" % (num, label),
                                "action": "add-label", "target": num, "title": title,
                                "label": label, "choose_group": "issue:%d:type" % num,
                                "why": why})
            elif code == "touches_outside_section":
                new_body = queue_lint.with_touches(body, d["choices"][0] if d["choices"] else "")
                if new_body is None or new_body == body:
                    continue
                out.append({"kind": "metadata", "key": "meta:%d:metadata-section" % num,
                            "action": "set-body", "target": num, "title": title,
                            "value": d["choices"][0], "choose_group": None,
                            "body": new_body, "why": why})
            elif code == "touches_missing":
                for area in d["choices"]:
                    new_body = queue_lint.with_touches(body, area)
                    if new_body is None or new_body == body:
                        continue
                    out.append({"kind": "metadata", "key": "meta:%d:touches=%s" % (num, area),
                                "action": "set-body", "target": num, "title": title,
                                "value": area, "choose_group": "issue:%d:touches" % num,
                                "body": new_body, "why": why})
    return sorted(out, key=lambda p: (p["target"], p["key"]))


def propose(*, branches, branch_prs, superseded_prs, parked_issues, ls_issues,
            now, aged_park_days, refused=frozenset(), dev_branch="main",
            lint_issues=None, areas=None, touches_required=False):
    """The full proposal list for one sweep, grouped branches -> PRs -> issues, each sorted
    (deterministic; no input mutated). Returns {"proposals": [...], "refused": [...]} where
    `refused` holds the keys that WOULD have been proposed but sit in the caller's refused set
    (a previously failed action is surfaced once and never silently retried — the CLI holds
    these back until --retry-refused).

    branches        {remote branch name: current tip sha} (gh.remote_branches). The tip is the
                    moved-since-the-PR guard: a delete is proposed only when it equals the
                    PR's headRefOid; a missing/wrong-typed tip is never proposed.
    branch_prs      {branch: (pr_dict, ok)} — gh.pr_for_branch's PrRead per branch. ok=False
                    (a REFUSED lookup) fails closed: the branch is not proposed.
    superseded_prs  raw gh dicts for OPEN PRs labeled `superseded`.
    parked_issues   raw gh dicts (number/title/labels/updatedAt) for open parked/needs-william
                    issues.
    ls_issues       loopstate['issues'] — the in-flight/mid-gate exclusion source.
    now             epoch (injected — this module reads no clock).
    aged_park_days  the configurable dust threshold (config janitor.aged_park_days).
    refused         action keys previously refused/failed (held back, reported separately).
    dev_branch      never proposed, whatever it is named (belt + braces).
    lint_issues     raw gh dicts for EVERY open issue (gh.open_issues_all) — the metadata-repair
                    input (#225). Omitted or wrong-typed -> no metadata proposals at all, which is
                    exactly the pre-#225 sweep: a menu built on an unread issue list is a menu
                    built on nothing.
    areas           the repo's declared `areas` (name -> globs), or None when unknown.
    touches_required the repo's own knob. Defaults FALSE here, unlike the ENGINE's enforce-on-
                    garbage posture: the runner knows it is looking at its own adopted repo, while
                    a caller that forgets to pass this must propose FEWER edits, never more.

    Each proposal: {"kind", "key", "action", "target", "why"} (+ "head" for PRs, "title" for
    issues and metadata, "label"/"value"/"body"/"choose_group" for metadata) — `key` is the stable
    identity ("branch:<name>" / "pr:<num>" / "issue:<num>" / "meta:<num>:<what>") the refused map
    and reconcile() work in. A non-null `choose_group` marks mutually-exclusive ALTERNATIVES: at
    most one member of a group may ever execute, and never from a bulk approval."""
    if not isinstance(ls_issues, dict):
        # the exclusion source is unreadable: nothing is provably idle, so the whole sweep
        # fails closed — no proposals at all, whatever the candidates' own evidence says.
        return {"proposals": [], "refused": []}
    ex_nums, ex_branches = _exclusions(ls_issues)
    refused = refused if isinstance(refused, (set, frozenset)) else frozenset()
    # A wrong-typed threshold must NOT coerce to the most aggressive setting (0d — propose
    # every park immediately); None disables the issue class entirely (cross-review r1, M1).
    threshold_days = aged_park_days if (type(aged_park_days) is int
                                        and aged_park_days >= 0) else None
    proposals, held = [], []

    def emit(p):
        (held if p["key"] in refused else proposals).append(p)

    # --- stale sl/* branches: work provably landed (merged) or provably replaced (superseded) ---
    branch_prs = branch_prs if isinstance(branch_prs, dict) else {}
    branches = branches if isinstance(branches, dict) else {}
    for b in sorted(b for b in branches if isinstance(b, str)):
        if not b.startswith(BRANCH_PREFIX) or b == dev_branch:
            continue
        if branch_issue_num(b) in ex_nums or b in ex_branches:
            continue
        entry = branch_prs.get(b)
        if not (isinstance(entry, tuple) and len(entry) == 2):
            continue
        pr, ok = entry
        if ok is not True or not isinstance(pr, dict) or not pr:
            continue                             # refused lookup / no PR ever: never delete
        num = _pr_int(pr.get("number"))
        state = pr.get("state")
        if num is None:
            continue
        # The moved-since-the-PR guard: the branch's CURRENT tip must be the PR's last-known
        # head. Commits pushed after the merge/close would be lost with the branch, so a moved
        # or unprovable tip (missing sha, missing headRefOid) is never proposed.
        tip, oid = branches.get(b), pr.get("headRefOid")
        if not (isinstance(tip, str) and tip and isinstance(oid, str) and oid and tip == oid):
            continue
        if state == "MERGED":
            why = f"PR #{num} merged — the work is on the mainline"
        elif state == "CLOSED" and SUPERSEDED_LABEL in _label_names(pr.get("labels")):
            why = f"PR #{num} (superseded) is closed — replaced by a rebuild"
        else:
            continue                             # open, or closed-unmerged: work stays
        emit({"kind": "branch", "key": f"branch:{b}", "action": "delete-branch",
              "target": b, "why": why})

    # --- open PRs labeled superseded: left open by design, closable only on the owner's word ---
    seen_prs = set()
    prs = superseded_prs if isinstance(superseded_prs, list) else []
    for p in sorted((p for p in prs if isinstance(p, dict)),
                    key=lambda p: (_pr_int(p.get("number")) is None, _pr_int(p.get("number")) or 0)):
        num = _pr_int(p.get("number"))
        if num is None or num in seen_prs:
            continue
        if p.get("state") != "OPEN":
            continue                             # a raced/stale answer must not close a closed PR
        if SUPERSEDED_LABEL not in _label_names(p.get("labels")):
            continue                             # the entry itself must prove the label
        head = p.get("headRefName")
        head = head if isinstance(head, str) else ""
        if branch_issue_num(head) in ex_nums or (head and head in ex_branches):
            continue
        seen_prs.add(num)
        emit({"kind": "pr", "key": f"pr:{num}", "action": "close-pr", "target": num,
              "head": head,
              "why": "open but superseded — replaced by a rebuild; the branch stays"})

    # --- parked / needs-william issues gathering dust past the threshold ---
    seen_issues = set()
    parked = parked_issues if isinstance(parked_issues, list) and threshold_days is not None \
        else []
    for i in sorted((i for i in parked if isinstance(i, dict)),
                    key=lambda i: (_pr_int(i.get("number")) is None, _pr_int(i.get("number")) or 0)):
        num = _pr_int(i.get("number"))
        if num is None or num in seen_issues or num in ex_nums:
            continue
        labels = _label_names(i.get("labels"))
        # `in-progress` = claimed by a lane; `agent-ready` = the owner's approval word is ON
        # the issue (a re-approval whose label cleanup blipped can leave it beside a stale
        # park label) — either one mechanically excludes: never propose closing approved or
        # claimed work (cross-review round 1, M2).
        if "in-progress" in labels or "agent-ready" in labels:
            continue
        park = next((l for l in PARK_LABELS if l in labels), None)
        if park is None:
            continue
        updated = parse_epoch(i.get("updatedAt"))
        if updated is None:
            continue                             # age unprovable -> fail closed
        age = (now - updated) if isinstance(now, (int, float)) else -1
        if age < threshold_days * 86400:
            continue
        seen_issues.add(num)
        title = i.get("title") if isinstance(i.get("title"), str) else ""
        emit({"kind": "issue", "key": f"issue:{num}", "action": "close-issue", "target": num,
              "title": title,
              "why": f"{park} and untouched for {int(age // 86400)}d "
                     f"(threshold {threshold_days}d)"})

    # --- mechanically-invalid issues: the metadata the runner cannot launch without (issue #225) ---
    for p in _metadata_proposals(lint_issues, areas, touches_required, ex_nums):
        emit(p)

    return {"proposals": proposals, "refused": sorted(p["key"] for p in held)}


def reconcile(approved, fresh_proposals):
    """(to_execute, skipped): an approved item executes ONLY if a fresh re-derivation still
    proposes it — the y/N wait can be minutes, and the world may have moved (a re-approval
    mid-wait must never get its branch deleted). The FRESH item is what executes (current why,
    current fields); a fresh item nobody approved never runs. Wrong-typed inputs -> nothing."""
    fresh_by_key = {p["key"]: p for p in fresh_proposals
                    if isinstance(p, dict) and isinstance(p.get("key"), str)} \
        if isinstance(fresh_proposals, list) else {}
    to_run, skipped = [], []
    for p in approved if isinstance(approved, list) else []:
        if not (isinstance(p, dict) and isinstance(p.get("key"), str)):
            continue
        f = fresh_by_key.get(p["key"])
        (to_run.append(f) if f is not None else skipped.append(p))
    return to_run, skipped
