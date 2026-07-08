"""The GitHub adapter: one thin subprocess wrapper around the `gh` CLI, and typed JSON parsers
above it. GitHub is superlooper's work-queue state store, so every read a tick makes goes through
here.

Two hard rules, both bought by the autocode runs:
  1. `_run` NEVER raises into a tick. A `gh` timeout, a missing binary, a killed process — all
     become a nonzero rc + empty stdout, so the caller acts on nothing.
  2. Every parser FAILS CLOSED: a nonzero rc, a timeout, or unparseable/ wrong-typed JSON yields
     the EMPTY-but-typed result ([] / {} / False / None). Acting on nothing is always safe; acting
     on a half-read GitHub state is not (a parked blocker once held two issues all night, and a
     fail-OPEN coercion once launched work over quota).

The gh binary is overridable via `SL_GH` (tests point it at tests/fakes/fake-gh).
"""
import copy
import json
import os
import re
import subprocess
from urllib.parse import quote

import issues as _issues  # pure sibling module; used only to filter child_issues by parent

_ISSUE_FIELDS = "number,title,labels,body,createdAt"
# labels rides along for the gate's §C.4 step-6c `preserve` check (gate._pr_labels) — the one
# PR label that changes a gate decision (conflict-resolution session instead of regenerate).
# headRefOid rides along for the runner's update_result bookkeeping: the gate's view contract
# says update_result is "for the CURRENT head; the runner clears it whenever the PR head
# changes", and the head is only detectable by its oid (Task 10).
_PR_FIELDS = "number,state,mergeable,statusCheckRollup,files,headRefName,headRefOid,labels"


def _binary():
    return os.environ.get("SL_GH", "gh")


# The repo every gh call targets (owner/name), set once from config.repo (set_repo below).
# None = unpinned: the ambient environment passes through untouched.
_repo = None


def set_repo(slug):
    """Pin every gh subprocess to ONE repo (D1, live dry-run 2026-07-03). gh resolves its target
    from the process cwd's git remotes, so a runner started outside the adopted repo silently
    talked to the wrong repo — or none. The CLI and Runner call this with config.repo at startup;
    _run then injects GH_REPO — gh's own override, honored by the issue/pr/label commands and the
    `gh api` {owner}/{repo} placeholders — into every subprocess, beating cwd inference AND any
    ambient GH_REPO the operator exported as a workaround (a stale export from operating repo A
    must never redirect repo B's runner). None/blank clears the pin."""
    global _repo
    _repo = slug.strip() if isinstance(slug, str) and slug.strip() else None


def _run(args, timeout=30):
    """Run `gh <args>` with a HARD timeout. Returns (rc, stdout). Never raises: a timeout, a
    missing binary, or any OSError is caught and returned as a nonzero rc with empty stdout so the
    caller fails closed. stderr is captured (swallowed here — the runner logs failures via rc)."""
    env = {**os.environ, "GH_REPO": _repo} if _repo else None   # None = inherit untouched
    try:
        proc = subprocess.run([_binary(), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
        return (proc.returncode, proc.stdout)
    except subprocess.TimeoutExpired:
        return (124, "")          # conventional timeout rc
    except (OSError, ValueError):
        return (127, "")          # command not found / bad invocation


def _json(args, default, timeout=30):
    rc, out = _run(args, timeout=timeout)
    if rc != 0:
        return copy.deepcopy(default)
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return copy.deepcopy(default)


def _json_list(args, timeout=30):
    v = _json(args, [], timeout=timeout)
    return v if isinstance(v, list) else []       # wrong-typed JSON also fails closed


def _json_dict(args, timeout=30):
    v = _json(args, {}, timeout=timeout)
    return v if isinstance(v, dict) else {}


# --------------------------- reads (fail closed to empty-but-typed) ---------------------------

def ready_issues(limit=200):
    """Open issues labeled `agent-ready` (the launch queue). Raw gh dicts; the caller runs
    issues.parse_issue on each."""
    return _json_list(["issue", "list", "--state", "open", "--label", "agent-ready",
                       "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def open_issues(label, limit=200):
    """Open issues carrying `label`, raw gh dicts (the caller parses). The runner's poll uses
    this for the `in-progress` sweep (orphan reclaim: an in-progress issue with no live session
    belongs back in the queue or in a relaunch)."""
    return _json_list(["issue", "list", "--state", "open", "--label", label,
                       "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def closed_issue_nums(limit=200):
    """Numbers of closed issues, as a set (blocked-by eligibility: a dependency counts as done
    only when its issue is CLOSED). Fails closed to an empty set — with no readable closed list,
    every blocked-by dependency reads as unmet and the dependent issue simply waits."""
    lst = _json_list(["issue", "list", "--state", "closed", "--json", "number",
                      "--limit", str(limit)])
    return {i["number"] for i in lst
            if isinstance(i, dict) and type(i.get("number")) is int}


def labels(limit=200):
    """Existing label names in the repo, as a set (doctor's §C.2 label check). Fails closed to
    an empty set — doctor then reports every label missing, which is the honest answer when
    GitHub is unreadable."""
    lst = _json_list(["label", "list", "--json", "name", "--limit", str(limit)])
    return {l["name"] for l in lst if isinstance(l, dict) and isinstance(l.get("name"), str)}


def create_label(name, color, description):
    """Create-or-update one label (`--force` updates an existing one, so adopt is idempotent).
    True on success."""
    rc, _ = _run(["label", "create", name, "--color", color,
                  "--description", description, "--force"])
    return rc == 0


def probe():
    """Is gh reachable + authenticated RIGHT NOW? (`gh api rate_limit` — free, does not count
    against limits.) The runner probes once per poll cycle: a False keeps the previous GitHub
    view (marked stale, so gate/launch decisions wait) and feeds the persistent-failure ALERT
    counter, instead of letting every fail-closed empty read masquerade as 'no work exists'."""
    rc, _ = _run(["api", "rate_limit"])
    return rc == 0


def issue(num):
    return _json_dict(["issue", "view", str(num), "--json", _ISSUE_FIELDS])


def issue_comments(num):
    d = _json_dict(["issue", "view", str(num), "--json", "comments"])
    c = d.get("comments")
    return c if isinstance(c, list) else []


def pr_comments(num):
    d = _json_dict(["pr", "view", str(num), "--json", "comments"])
    c = d.get("comments")
    return c if isinstance(c, list) else []


def pr_for_branch(branch):
    """The PR whose head is `branch`, whatever its state (so the caller sees open/merged/closed).
    {} if none / on failure."""
    lst = _json_list(["pr", "list", "--head", branch, "--state", "all",
                      "--json", _PR_FIELDS, "--limit", "1"])
    return lst[0] if lst and isinstance(lst[0], dict) else {}


def branch_checks(branch):
    """Check-run rollup for a branch's HEAD commit (used to poll dev checks post-merge, where no
    PR exists). Normalized to [{name, status, conclusion}]. gh substitutes {owner}/{repo}; the ref
    is URL-encoded so a slashed branch (sl/i1-x) doesn't split into extra path segments."""
    d = _json_dict(["api", "repos/{owner}/{repo}/commits/%s/check-runs" % quote(branch, safe="")])
    runs = d.get("check_runs")
    if not isinstance(runs, list):
        return []
    return [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
            for r in runs if isinstance(r, dict)]


def compare(base, head):
    """`base...head` merge-base comparison (status/ahead_by/behind_by/files). {} on failure.
    Used for the dev->prod promotion diff (`prod...dev`). Refs are URL-encoded (slashed branches)."""
    return _json_dict(["api", "repos/{owner}/{repo}/compare/%s...%s"
                       % (quote(base, safe=""), quote(head, safe=""))])


def child_issues(parent_num):
    """Issues whose Loop metadata declares `parent: #<parent_num>`. A body search narrows the
    candidate set, then each is filtered PRECISELY via issues.parse_loop_metadata — GitHub search
    is substring-fuzzy ("parent: #4" would also match "#40"), so the parse is the source of truth."""
    candidates = _json_list(["issue", "list", "--state", "all",
                             "--search", '"parent: #%d" in:body' % parent_num,
                             "--json", _ISSUE_FIELDS, "--limit", "200"])
    return [c for c in candidates
            if isinstance(c, dict)
            and _issues.parse_loop_metadata(c.get("body", "")).get("parent") == parent_num]


# --------------------------- writes (fail closed to False/None) ---------------------------

def set_labels(num, add=None, remove=None):
    """Add/remove labels on an issue. True on success, False on failure (act as if it didn't
    happen). Label mechanics are always runner-side, never a worker duty."""
    args = ["issue", "edit", str(num)]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    if not add and not remove:
        return True                # nothing to do
    rc, _ = _run(args)
    return rc == 0


def comment(num, body):
    """Post a comment on an issue. True on success."""
    rc, _ = _run(["issue", "comment", str(num), "--body", body])
    return rc == 0


def pr_comment(num, body):
    """Post a comment on a PR (e.g. the runner's merge cross-link)."""
    rc, _ = _run(["pr", "comment", str(num), "--body", body])
    return rc == 0


def pr_add_labels(num, labels):
    """Add labels to a PR (§C.4 6b: mark a conflicted PR `superseded` — the branch and the PR
    stay; only the label records that a rebuild replaced it). True on success."""
    if not labels:
        return True
    rc, _ = _run(["pr", "edit", str(num), "--add-label", ",".join(labels)])
    return rc == 0


def close_issue(num, comment=None):
    """Close an issue (the investigate-type gate: marker comment present -> close the parent).
    True on success."""
    args = ["issue", "close", str(num)]
    if comment:
        args += ["--comment", comment]
    rc, _ = _run(args)
    return rc == 0


def create_issue(title, body, labels=None):
    """Create an issue (e.g. an auto-filed nightly-red fix). Returns the new issue number, or None
    on failure. Label-agnostic: the approval discipline lives at the CALL SITES — worker/skill-
    created issues never carry `agent-ready`, and the ONLY exception that does (the nightly-red
    fix) is William's own standing rule, carrying its distinct `auto-approved:nightly-red` label."""
    args = ["issue", "create", "--title", title, "--body", body]
    if labels:
        args += ["--label", ",".join(labels)]
    rc, out = _run(args)
    if rc != 0:
        return None
    m = re.search(r"/issues/(\d+)", out)          # gh prints the new issue URL
    if not m:
        m = re.search(r"(\d+)\s*$", out.strip())  # fallback: a bare trailing number
    return int(m.group(1)) if m else None


def merge_pr(num, method="squash"):
    """Merge a PR with the configured method (squash default, §B.4). True on success. There is no
    force path anywhere — the runner never force-pushes."""
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    rc, _ = _run(["pr", "merge", str(num), flag])
    return rc == 0
