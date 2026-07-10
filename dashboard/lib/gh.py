"""The GitHub adapter: one thin subprocess wrapper around the ``gh`` CLI, and typed JSON parsers
above it. GitHub is the loop's work-queue state store, so this is the dashboard's only egress to it
(alongside the notifier, Task 10) — every issue/PR/label read the snapshot makes, and every
mechanical-verb write a button makes, goes through here.

Two hard rules, ported from superlooper's ``skill/lib/gh.py`` (both bought by real autocode runs):

  1. ``_run`` NEVER raises into a poll. A ``gh`` timeout, a missing binary, a killed process — all
     become a nonzero rc + empty stdout, so the caller acts on nothing.
  2. Every parser FAILS CLOSED: a nonzero rc, a timeout, or unparseable / wrong-typed JSON yields
     the EMPTY-but-typed result (``[]`` / ``{}`` / ``False`` / ``None``). Acting on nothing is
     always safe; acting on a half-read GitHub state is not (the dashboard writes labels — it must
     never, say, "helpfully" clear ``parked`` off an issue it only half-read).

**One deliberate divergence from the skill.** superlooper runs one runner per adopted repo, so it
pins the target once with a module-global ``set_repo``. The command center watches MANY repos in
one process, so a hidden global would be a footgun (which repo is "current"? a concurrent poll
races it). Here the target repo is an EXPLICIT first argument on every call, injected per-subprocess
as ``GH_REPO`` (gh's own override — beats cwd inference and any ambient ``GH_REPO`` the operator
exported). Stateless: no call can silently talk to the wrong repo.

The gh binary is overridable via ``SL_GH`` (tests point it at ``tests/fakes/fake-gh``); the
per-call timeout via the module-level ``_DEFAULT_TIMEOUT`` (tests shrink it to exercise the
timeout path without waiting).
"""
import copy
import json
import os
import re
import subprocess

_ISSUE_FIELDS = "number,title,labels,body,createdAt,state"    # `state` (OPEN/CLOSED) lets the
# departures board resolve a blocked-by connection fail-closed — a blocker is only "arrived" with
# positive proof it is CLOSED (Task 8; open/unknown ⇒ still awaiting).
# state/mergeable/statusCheckRollup are what the gate checklist (Task 3) reads; headRefName rides
# along so the caller can confirm the PR really is this branch's. additions/deletions/changedFiles
# are the PR's own diff size — the cargo that must survive after a landed flight's worktree is
# cleaned up (issue #48, absorbing #47), carried on the SAME single read, never a second call. Raw
# dicts out — the SEMANTICS (is the gate green? which stage? how big?) live in lib/flights.py.
_PR_FIELDS = "number,state,mergeable,statusCheckRollup,headRefName,additions,deletions,changedFiles"

# Per-call hard timeout (seconds). A module constant, not a literal, so a test can shrink it and
# trip the timeout path in a fraction of a second instead of waiting the real 30.
_DEFAULT_TIMEOUT = 30


def _binary():
    return os.environ.get("SL_GH", "gh")


def _run(args, repo=None, timeout=None):
    """Run ``gh <args>`` pinned to ``repo`` with a HARD timeout. Returns ``(rc, stdout)``. Never
    raises: a timeout, a missing binary, or any OSError is caught and returned as a nonzero rc with
    empty stdout so the caller fails closed. stderr is captured and swallowed (the rc is the
    signal). ``repo`` (``owner/name``) is injected as ``GH_REPO`` so the call targets exactly that
    repo; a falsy ``repo`` inherits the ambient environment untouched."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    env = {**os.environ, "GH_REPO": repo} if repo else None
    try:
        proc = subprocess.run([_binary(), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
        return (proc.returncode, proc.stdout)
    except subprocess.TimeoutExpired:
        return (124, "")          # conventional timeout rc
    except (OSError, ValueError):
        return (127, "")          # command not found / bad invocation


def _json(args, default, repo=None, timeout=None):
    rc, out = _run(args, repo=repo, timeout=timeout)
    if rc != 0:
        return copy.deepcopy(default)
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return copy.deepcopy(default)


def _json_list(args, repo=None, timeout=None):
    v = _json(args, [], repo=repo, timeout=timeout)
    return v if isinstance(v, list) else []       # wrong-typed JSON also fails closed


def _json_dict(args, repo=None, timeout=None):
    v = _json(args, {}, repo=repo, timeout=timeout)
    return v if isinstance(v, dict) else {}


# --------------------------- reads (fail closed to empty-but-typed) ---------------------------

def ready_issues(repo, limit=200):
    """Open issues labeled ``agent-ready`` (the launch queue / Solari departures order). Raw gh
    dicts; the flight model parses each. ``[]`` on any failure."""
    return open_issues(repo, label="agent-ready", limit=limit)


def open_issues_probe(repo, label=None, limit=200):
    """Open issues in ``repo`` PLUS whether gh gave us a USABLE answer — the ONE honest signal that
    separates "GitHub answered: no open issues" from "GitHub is unreachable / unreadable". Returns
    ``(issues, reachable)``:

      * ``reachable`` is ``True`` ONLY when gh exited 0 AND its output parsed to a JSON LIST (empty or
        not) — a real open-issue answer, so an empty list is a genuine all-clear;
      * ``reachable`` is ``False`` whenever the read is not trustworthy: the subprocess failed (a
        missing binary, an unauthenticated / erroring gh, a timeout, any nonzero rc), OR gh exited 0
        but handed back unparseable / wrong-shaped output. A parse failure is no more a trustworthy
        all-clear than a nonzero rc is (Codex review, issue #38) — either way we could not read the
        queue, so the field must show the honest dark-tower state, never a false calm.

    ``issues`` is the same fail-closed list :func:`open_issues` returns (``[]`` on any failure or
    wrong-typed body), so a caller that only wants the list ignores the second value. This is the
    open-issue read the snapshot already makes every poll — the reachability rides that ONE call, so
    the unreachable state costs no extra gh call (quota discipline, issue #38)."""
    args = ["issue", "list", "--state", "open", "--json", _ISSUE_FIELDS, "--limit", str(limit)]
    if label:
        args += ["--label", label]
    rc, out = _run(args, repo=repo)
    if rc != 0:
        return [], False              # unreachable / refused — never a false all-clear
    try:
        v = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return [], False              # gh ran but its output was junk — not a usable queue read
    if not isinstance(v, list):
        return [], False              # valid JSON of the wrong shape — also not a usable list answer
    return v, True


def open_issues(repo, label=None, limit=200):
    """Open issues in ``repo`` — all of them, or only those carrying ``label`` when given. Raw gh
    dicts (number/title/labels/body/createdAt). ``[]`` on any failure — with no readable list the
    dashboard simply shows no flights, which is the honest answer when GitHub is unreachable. The
    list-only surface over :func:`open_issues_probe`; the two send gh the identical query, so the
    reachability signal can never drift from the list it accompanies."""
    return open_issues_probe(repo, label=label, limit=limit)[0]


def issue(repo, num):
    """One issue's detail (number/title/labels/body/createdAt). ``{}`` on any failure."""
    return _json_dict(["issue", "view", str(num), "--json", _ISSUE_FIELDS], repo=repo)


def issue_comments(repo, num):
    """Comments on an issue, newest gh order. ``[]`` on any failure or wrong-typed body."""
    d = _json_dict(["issue", "view", str(num), "--json", "comments"], repo=repo)
    c = d.get("comments")
    return c if isinstance(c, list) else []


def pr_comments(repo, num):
    """Comments on a PR (e.g. to find the ``<!-- superlooper-review -->`` verdict). ``[]`` on any
    failure or wrong-typed body."""
    d = _json_dict(["pr", "view", str(num), "--json", "comments"], repo=repo)
    c = d.get("comments")
    return c if isinstance(c, list) else []


def pr_for_branch(repo, branch):
    """The PR whose head is ``branch``, in ANY state (so the caller sees open/merged/closed and its
    mergeable + check rollup). ``{}`` if there is none or on any failure."""
    lst = _json_list(["pr", "list", "--head", branch, "--state", "all",
                      "--json", _PR_FIELDS, "--limit", "1"], repo=repo)
    return lst[0] if lst and isinstance(lst[0], dict) else {}


# --------------------------- writes (fail closed to False / None) ---------------------------

def set_labels(repo, num, add=None, remove=None):
    """Add/remove labels on an issue. ``True`` on success, ``False`` on failure (act as if it never
    happened). Nothing to change ⇒ ``True`` with no gh call. NOTE: applying ``agent-ready`` is
    William's word alone — the call SITES enforce that, never this mechanical verb."""
    if not add and not remove:
        return True                # nothing to do
    args = ["issue", "edit", str(num)]
    if add:
        args += ["--add-label", ",".join(add)]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    rc, _ = _run(args, repo=repo)
    return rc == 0


def comment(repo, num, body):
    """Post a comment on an issue (the audit trail every button leaves). ``True`` on success."""
    rc, _ = _run(["issue", "comment", str(num), "--body", body], repo=repo)
    return rc == 0


def close_issue(repo, num, comment=None):
    """Close an issue, optionally leaving an audit comment in the SAME gh call (``--comment`` posts
    then closes atomically — so a dropped flight is never closed without its trail). ``True`` on
    success, ``False`` on failure (act as if it never happened). This is the only DESTRUCTIVE verb;
    its single client-side confirm lives at the call site, never here."""
    args = ["issue", "close", str(num)]
    if comment:
        args += ["--comment", comment]
    rc, _ = _run(args, repo=repo)
    return rc == 0


def create_issue(repo, title, body, labels=None):
    """Create an issue (e.g. the flag box: raw text ⇒ a ``flag``-labeled issue). Returns the new
    issue number, or ``None`` on failure. Label-agnostic: approval discipline lives at the call
    sites — the dashboard NEVER creates an issue carrying ``agent-ready`` (William's word)."""
    args = ["issue", "create", "--title", title, "--body", body]
    if labels:
        args += ["--label", ",".join(labels)]
    rc, out = _run(args, repo=repo)
    if rc != 0:
        return None
    m = re.search(r"/issues/(\d+)", out)          # gh prints the new issue URL
    if not m:
        m = re.search(r"(\d+)\s*$", out.strip())  # fallback: a bare trailing number
    return int(m.group(1)) if m else None


def create_label(repo, name, color, description):
    """Create-or-update one label (``--force`` updates an existing one, so the flag label's
    first-use creation is idempotent). ``True`` on success."""
    rc, _ = _run(["label", "create", name, "--color", color,
                  "--description", description, "--force"], repo=repo)
    return rc == 0
