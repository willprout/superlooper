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
import collections
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import issues as _issues  # pure sibling module; used only to filter child_issues by parent
import telemetry  # pure sibling module; bounded local GitHub API-burn telemetry (issue #15)

_ISSUE_FIELDS = "number,title,labels,body,createdAt"
# labels rides along for the gate's §C.4 step-6c `preserve` check (gate._pr_labels) — the one
# PR label that changes a gate decision (conflict-resolution session instead of regenerate).
# headRefOid rides along for the runner's update_result bookkeeping: the gate's view contract
# says update_result is "for the CURRENT head; the runner clears it whenever the PR head
# changes", and the head is only detectable by its oid (Task 10).
# `body` rides this existing read (issue #404): the gate's step 2c must see whether the PR closes
# its issue on merge, and adding a FIELD to a call the poll already makes costs no extra GitHub
# request — the API-burn discipline that keeps the per-tick read budget where #21/#61 put it.
_PR_FIELDS = "number,state,mergeable,statusCheckRollup,files,headRefName,headRefOid,labels,body"
# statusCheckRollup needs no companion field for the gate's latest-run-per-name fold (issue #402):
# `--json statusCheckRollup` is atomic — gh's own query already selects startedAt/completedAt on
# every CheckRun and createdAt on every StatusContext, which is the recency gate._rollup_entries
# ranks by. Nothing to add here; a future gh that drops those timestamps would land the fold on
# its documented fail-closed fallback (any-failure-wins), not on a wrong answer.


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


# GitHub API-burn telemetry sink (issue #15). None = OFF: no telemetry is recorded, so ordinary
# tests and one-shot CLI commands (doctor/status/adopt) write nothing. The LONG-LIVED runner turns
# it on once at startup (superlooper `cmd_run` -> set_telemetry(state_home)); being the sole writer
# of its file makes the ring's rewrite race-free. Every gh subprocess this process then runs lands
# one bounded row under `<state_home>/gh-telemetry-runner.jsonl`, alongside the runner's other state.
_telemetry_home = None


def set_telemetry(home):
    """Enable per-call GitHub API-burn telemetry for THIS process, writing under `home` (issue #15) —
    the runner's state home. Off by default; a falsy `home` disables it again. Recording is
    best-effort and never breaks a gh call (see lib/telemetry.py)."""
    global _telemetry_home
    _telemetry_home = os.fspath(home) if home else None


def _caller_op():
    """The gh.py public function this subprocess is being run for — the OUTERMOST non-underscore
    function defined in THIS module on the call stack. Walking to the OUTERMOST (nearest the external
    caller) rather than the innermost keeps the SEMANTIC entry point (`ready_issues`) even when a
    public function delegates through others down to `_run`. Best-effort: `"?"` on any failure — the
    op is a telemetry label, never worth raising into a gh call for."""
    try:
        op = "?"
        f = sys._getframe(1)
        while f is not None:
            if f.f_globals.get("__name__") == __name__ and not f.f_code.co_name.startswith("_"):
                op = f.f_code.co_name        # overwrite as we climb -> the final value is outermost
            f = f.f_back
        return op
    except Exception:
        return "?"


def _record_call(args, rc, err):
    """Record one gh subprocess as a telemetry row when telemetry is enabled. Never raises (the
    telemetry module swallows its own errors; the None-guard and list() copy are the only work here)."""
    if _telemetry_home is None:
        return
    telemetry.record_call(_telemetry_home, "runner", _repo, _caller_op(), list(args), rc, err)


def _run_full(args, timeout=30):
    """Run `gh <args>` with a HARD timeout. Returns (rc, stdout, stderr). Never raises: a timeout,
    a missing binary, or any OSError is caught and returned as a nonzero rc with empty streams so
    the caller fails closed. stderr is returned for the few callers that must surface WHY a write
    was refused (merge_pr, issue #27); the reads ignore it and act on rc alone."""
    env = {**os.environ, "GH_REPO": _repo} if _repo else None   # None = inherit untouched
    try:
        proc = subprocess.run([_binary(), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "gh timed out"              # conventional timeout rc
    except (OSError, ValueError):
        rc, out, err = 127, "", "gh not found / bad invocation"   # command not found / bad invocation
    _record_call(args, rc, err)     # issue #15: one bounded burn-telemetry row per subprocess (no-op when off)
    return (rc, out, err)


def _run(args, timeout=30):
    """Run `gh <args>`; returns (rc, stdout) — the stderr-swallowing form nearly every caller
    wants (failures surface via rc). Thin wrapper so the subprocess/env/timeout machinery lives in
    ONE place."""
    rc, out, _ = _run_full(args, timeout=timeout)
    return (rc, out)


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


# The read-health contract, generalized (issue #92) — the #21/#61 refused-vs-answered-empty
# discipline for the two reads the unattended watchdog's no-progress detector depends on. A
# fail-closed empty read is safe for the runner (act on nothing), but the watchdog must tell a
# REFUSED read apart from a genuinely empty one: a refused list read that looked like "nothing
# eligible" reset the no-progress clocks and stood an episode down (the trap gh.probe's docstring
# names). ReadHealth carries `ok` — True ONLY on a clean, well-typed answer (rc 0 + a JSON list);
# `value` is ALWAYS the empty-but-typed fallback on any refusal, so a caller that ignores `ok`
# still fails closed exactly as before.
ReadHealth = collections.namedtuple("ReadHealth", ["value", "ok"])


def _json_list_health(args, timeout=30):
    """(list, ok): _json_list's read-health twin. ok=False on a nonzero rc / timeout / missing
    binary, unparseable body, or a wrong-typed (non-list) body — every refusal the fail-closed
    parsers collapse to []. value is [] on any of those."""
    rc, out = _run(args, timeout=timeout)
    if rc != 0:
        return ReadHealth([], False)
    try:
        v = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return ReadHealth([], False)
    if not isinstance(v, list):
        return ReadHealth([], False)
    return ReadHealth(v, True)


# --------------------------- reads (fail closed to empty-but-typed) ---------------------------

def ready_issues(limit=200):
    """Open issues labeled `agent-ready` (the launch queue). Raw gh dicts; the caller runs
    issues.parse_issue on each."""
    return _json_list(["issue", "list", "--state", "open", "--label", "agent-ready",
                       "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def ready_issues_health(limit=200):
    """ready_issues() as a ReadHealth(issues, ok) — the watchdog's read-health variant (issue
    #92). ok distinguishes a refused list read from a genuinely empty agent-ready queue, so a
    refused read FREEZES the no-progress clocks instead of masquerading as 'no work exists'."""
    return _json_list_health(["issue", "list", "--state", "open", "--label", "agent-ready",
                              "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def closed_issue_nums_health(limit=200):
    """closed_issue_nums() as a ReadHealth(nums_set, ok) — the read-health variant (issue #92). A
    refused closed-list read (ok=False) must not reset the detector's clocks: an empty closed set
    makes every blocked-by dependency read as unmet, which could wrongly shrink the eligible set and
    stand an episode down.

    THIS is the variant the runner's poll uses too (issue #172). It read the bare set for five
    releases, and a throttled closed list therefore reached decide as a fresh, confident "nothing is
    closed" — `probe` (`gh api rate_limit`) is EXEMPT from rate limiting, so the poll completed and
    stamped the view `stale: False` right through the throttle, and every blocked-by issue went
    silently un-launchable. `ok` rides into gh_view as `closed_read_ok` so the hold is SAID."""
    rh = _json_list_health(["issue", "list", "--state", "closed", "--json", "number",
                            "--limit", str(limit)])
    nums = {i["number"] for i in rh.value
            if isinstance(i, dict) and type(i.get("number")) is int}
    return ReadHealth(nums, rh.ok)


def open_issues(label, limit=200):
    """Open issues carrying `label`, raw gh dicts (the caller parses). The runner's poll uses
    this for the `in-progress` sweep (orphan reclaim: an in-progress issue with no live session
    belongs back in the queue or in a relaunch)."""
    return _json_list(["issue", "list", "--state", "open", "--label", label,
                       "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def open_issues_all(limit=200):
    """EVERY open issue, raw gh dicts — the queue lint's read (issue #225).

    Deliberately unlabelled: the 2026-07-16 audit's whole point was that the defect lives in issues
    nobody has approved YET, so filtering by `agent-ready` would find only the wedges already
    burning and none of the ones about to. Fails closed to [] like every other list read, and the
    caller must treat an empty answer as "no verdict" rather than "the queue is clean" — a refused
    read and a spotless repo look identical from here."""
    return _json_list(["issue", "list", "--state", "open",
                       "--json", _ISSUE_FIELDS, "--limit", str(limit)])


def set_issue_body(num, body):
    """Replace an issue's body — the janitor's approved metadata repair (issue #225), only ever on
    the owner's explicit word, never from any automatic path. True on success.

    Written through a FILE (`--body-file`), not `--body`: an issue body is multi-KB markdown with
    newlines and backticks, and passing it as an argv string is how a repair truncates or a shell
    quirk mangles the very metadata it was meant to fix."""
    fd, path = tempfile.mkstemp(prefix="sl-issue-body-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body if isinstance(body, str) else "")
        rc, _ = _run(["issue", "edit", str(num), "--body-file", path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return rc == 0


def closed_issue_nums(limit=200):
    """Numbers of closed issues, as a set (blocked-by eligibility: a dependency counts as done
    only when its issue is CLOSED). Fails closed to an empty set — with no readable closed list,
    every blocked-by dependency reads as unmet and the dependent issue simply waits.

    Waiting is the safe direction, but the empty set alone cannot say WHY it is empty. Any caller
    that will narrate or act on the emptiness wants closed_issue_nums_health() instead (#172)."""
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


def labels_health(limit=200):
    """labels() as a ReadHealth(names_set, ok) — the boot preflight's read-health variant (issue
    #108, the #92 refused-vs-answered-empty discipline). `gh api rate_limit` (the probe) is EXEMPT
    from rate limiting, so it reads OK even when core quota is exhausted and the label LIST read is
    throttled to a fail-closed empty set; without `ok` the preflight would read that empty as 'every
    runner-managed label missing' and refuse to boot during the very throttle window it must survive.
    A refused label read (ok=False) SKIPS the preflight (like an unreachable gh); only a CLEAN read
    (ok=True) that genuinely lacks a runner-managed label fails loud."""
    rh = _json_list_health(["label", "list", "--json", "name", "--limit", str(limit)])
    names = {l["name"] for l in rh.value
             if isinstance(l, dict) and isinstance(l.get("name"), str)}
    return ReadHealth(names, rh.ok)


def create_label(name, color, description):
    """Create-or-update one label (`--force` updates an existing one, so adopt is idempotent).
    True on success."""
    rc, _ = _run(["label", "create", name, "--color", color,
                  "--description", description, "--force"])
    return rc == 0


def rename_label(old, new):
    """Rename a label in place (`gh label edit <old> --name <new>`), PRESERVING it on every issue
    that carries it — the adopt-side migration for issue #58's `needs-william` -> `needs-owner`
    rename. True on success (rc 0); False on any gh failure, so the caller can report the mixed
    state honestly rather than pretend the migration landed."""
    rc, _ = _run(["label", "edit", old, "--name", new])
    return rc == 0


def probe():
    """Is gh reachable + authenticated RIGHT NOW? (`gh api rate_limit` — free, does not count
    against limits.) The runner probes once per poll cycle: a False keeps the previous GitHub
    view (marked stale, so gate/launch decisions wait) and feeds the persistent-failure ALERT
    counter, instead of letting every fail-closed empty read masquerade as 'no work exists'.

    Rides a FREE rate-limit SNAPSHOT (issue #15) when telemetry is enabled: `gh api rate_limit` is
    exempt from rate limiting, and the runner already makes exactly this call here once per poll — so
    the snapshot costs no extra quota. The per-resource used/remaining/reset it records is what lets a
    later incident estimate hourly GraphQL/core burn without a live sampler. A refused probe records
    `ok=False` with no resources (distinguishing 'GitHub refused' from a real answer)."""
    rc, out = _run(["api", "rate_limit"])
    if _telemetry_home is not None:
        telemetry.record_rate_limit(_telemetry_home, "runner", _repo,
                                    telemetry.parse_rate_limit(out) if rc == 0 else {}, rc == 0)
    return rc == 0


def issue(num):
    return _json_dict(["issue", "view", str(num), "--json", _ISSUE_FIELDS])


def issue_is_open(num):
    """Is issue `num` open RIGHT NOW? True (open) / False (closed) / None (unreadable).

    The post-merge closure verify (issue #404). A merged PR closes its issue only through GitHub's
    closing keyword, and GitHub honors that keyword only for merges into the repository's DEFAULT
    branch — so a repo whose `dev_branch` is not the default gets NO server-side closure however
    perfect the keyword. This is the read that catches it, and it rides the MERGE path only: one
    call per merge, nothing on the runner's per-tick poll.

    None is the whole point of the tri-state. Collapsing a refused read into False would let the
    verify declare the issue closed on an answer GitHub never gave — the #21/#61 refused-vs-
    answered-empty discipline on the one read whose failure re-opens the incident it exists to
    close. The caller journals the unverified merge and leaves the pair to the janitor sweep.
    Anything but a recognized OPEN/CLOSED (either case — the state renders uppercase in GraphQL and
    lowercase in some paths) is unreadable, never a guess."""
    state = _json_dict(["issue", "view", str(num), "--json", "state"]).get("state")
    if not isinstance(state, str):
        return None
    return {"OPEN": True, "CLOSED": False}.get(state.upper())


# The comment-read contract (issue #21). A comment read has THREE outcomes, and the caller must
# tell the last two apart: (1) GitHub answered with comments, (2) GitHub answered "no comments",
# (3) GitHub REFUSED — rate-limit / 403 / 5xx / timeout / missing binary, or a wrong-typed /
# unparseable body. The old contract collapsed (2) and (3) both to [], so a single stale or
# refused read looked identical to an authoritative empty thread — and the investigate gate parked
# a finished investigation off that unverified read (this repo, #8, 2026-07-10). CommentRead keeps
# them distinct: `comments` is ALWAYS a list (still fail-closed to [] — acting on nothing is safe),
# and `ok` is True ONLY on a clean answer ({"comments": <list>} over rc 0). refused -> ok=False, so
# the gate HOLDS instead of parking; answered-empty -> ok=True, so the gate still nudges->parks.
CommentRead = collections.namedtuple("CommentRead", ["comments", "ok"])


def _comment_read(view_args, timeout=30):
    rc, out = _run(view_args, timeout=timeout)
    if rc != 0:
        return CommentRead([], False)          # GitHub refused / timed out / no binary
    try:
        d = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return CommentRead([], False)          # unparseable body: cannot trust it -> refused
    if isinstance(d, dict) and isinstance(d.get("comments"), list):
        return CommentRead(list(d["comments"]), True)   # a clean answer (possibly empty)
    return CommentRead([], False)              # missing/wrong-typed field: not a clean answer


def issue_comments(num):
    """The issue's comment thread as a CommentRead(comments, ok). `ok` distinguishes a genuine
    empty thread from a refused read, so a finished investigation is never parked on an unverified
    read (issue #21)."""
    return _comment_read(["issue", "view", str(num), "--json", "comments"])


def pr_comments(num):
    """The PR's comment thread as a CommentRead(comments, ok) — same refused-vs-answered-empty
    contract as issue_comments()."""
    return _comment_read(["pr", "view", str(num), "--json", "comments"])


# The PR-lookup contract (issue #61) — the build-gate half of the #21 read discipline. A
# PR-for-branch lookup has THREE outcomes: (1) GitHub answered with a PR, (2) GitHub answered
# "no PR on this head", (3) GitHub REFUSED — rate-limit / 403 / 5xx / timeout / missing binary,
# or an unparseable / wrong-typed body. The old contract collapsed (2) and (3) both to {}, so
# during the hourly GraphQL dead zones a refused lookup read as "no PR exists" and finished
# builds were parked as PR-less, re-notifying every tick (the 2026-07-08 storm: 41 texts).
# PrRead keeps them distinct: `pr` is ALWAYS a dict (still fail-closed to {} — acting on nothing
# is safe), and `ok` is True ONLY on a clean answer. refused -> ok=False, so the build gate
# HOLDs; answered-empty -> ok=True, so a genuinely PR-less finish still parks (once).
PrRead = collections.namedtuple("PrRead", ["pr", "ok"])


def pr_for_branch(branch):
    """The PR whose head is `branch`, whatever its state (so the caller sees open/merged/closed),
    as a PrRead(pr, ok). pr={} with ok=True means GitHub genuinely answered "no PR"; ok=False
    means the lookup was refused and the caller must NOT treat the emptiness as an answer."""
    rc, out = _run(["pr", "list", "--head", branch, "--state", "all",
                    "--json", _PR_FIELDS, "--limit", "1"])
    if rc != 0:
        return PrRead({}, False)                # GitHub refused / timed out / no binary
    try:
        lst = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return PrRead({}, False)                # unparseable body: cannot trust it -> refused
    if not isinstance(lst, list):
        return PrRead({}, False)                # wrong-typed body: not a clean answer
    if lst:
        # A clean FOUND answer must carry a real positive-int PR number (bool excluded) — every
        # consumer identifies the PR by it, and a numberless entry would read as trustworthy and
        # then park at the gate as "no PR exists" (the fail-OPEN-on-wrong-typed defect class,
        # Codex review C2). Anything else is a wrong-shaped body -> refused.
        #
        # headRefOid is deliberately NOT validated here, though #154 made it load-bearing (the gate
        # pins the review verdict to it). Validating it in this shared read would refuse the whole
        # PR for every consumer — the janitor's branch sweep reads the same function and does not
        # need a well-formed oid to decide a PR is MERGED — and GraphQL types headRefOid non-null,
        # so it buys nothing reachable. gate.review_evidence_state owns that judgement for its own
        # pure inputs instead ("head_unreadable" -> wait, never merge), which is the same
        # unreachable-but-fail-closed shape step 3 already uses for an unreadable `files`.
        first = lst[0]
        if isinstance(first, dict) and type(first.get("number")) is int and first["number"] > 0:
            return PrRead(first, True)
        return PrRead({}, False)
    return PrRead({}, True)                     # a clean answer: no PR on this head


def branch_checks(branch):
    """The dev branch HEAD's FULL required-check universe — check-runs AND commit statuses —
    used to poll dev checks post-merge, where no PR exists (the poll behind freeze/unfreeze).
    `branch` is any ref GitHub's commit endpoints accept, so a SHA works identically — which is
    how recent_branch_check_entries walks the doctor's window one commit at a time (issue #406).

    GitHub splits these across TWO REST endpoints: /check-runs (CheckRun) and /status (the
    combined commit-status, latest per context). The GraphQL statusCheckRollup the PR view reads
    unifies both, so a dev poll that read ONLY /check-runs was BLIND to any required check that
    reports on the branch as a commit status — its dev view read pending forever, so a mainline
    freeze could never auto-lift (issue #23). Reading both here restores parity with the PR view.

    Normalized to the SAME two shapes the PR rollup carries, so gate.required_checks_state folds
    them with no special-casing: check-runs -> {name, status, conclusion}; statuses ->
    {context, state}. gh substitutes {owner}/{repo}; the ref is URL-encoded so a slashed branch
    (sl/i1-x) doesn't split into extra path segments.

    UNCHANGED by issue #402, deliberately, and here is exactly how the two surfaces line up now
    that the PR rollup judges each required name by its LATEST run (gate._rollup_entries).

    This poll's entries carry NO timestamps — the normalization above drops them — so they always
    take that fold's fail-closed any-failure-wins branch. Behavior here is bit-for-bit what it was.
    That is the right answer for the shape this poll actually sees: a re-run of a workflow run
    supersedes its own earlier attempt WITHIN one check suite, and both endpoints already collapse
    that (/check-runs' default filter=latest, /status being the COMBINED latest-per-context), so a
    dev-branch commit carrying ONE suite reports each name exactly once and there is no superseded
    run to outvote anything.

    What `filter=latest` does NOT do is dedupe ACROSS check suites — measured, not assumed: on a
    commit carrying two suites the default call returned BOTH runs of the name (willprout/
    superlooper b259992, 2026-08-07). A dev-branch commit only grows a second suite if it was
    ALREADY built as a PR head and then reached the branch unchanged, which the default squash
    merge never produces (it writes a fresh commit whose only suite is the push one). Under
    `merge_method: "rebase"` it could, and then a superseded red here would freeze the mainline
    with no way to rank it away — filed for the owner rather than fixed inside #402's boundary,
    which holds this poll unchanged. Fail-closed either way: a spurious freeze, never a false
    unfreeze.

    The two reads fail closed INDEPENDENTLY to their empty contribution: a required check that
    never reports still reads pending (never a false green -> never a spurious unfreeze), and a
    red on EITHER endpoint still freezes. For a required check that reports via a SINGLE endpoint
    (the norm — GitHub identifies a required check by one context/name), a blip on the other
    endpoint can only shrink that check's view toward pending, never toward green. The lone
    exception is a name double-reported across BOTH endpoints with conflicting verdicts where the
    red side blips — a misconfiguration corner, not a real required-check shape."""
    ref = quote(branch, safe="")
    out = []
    runs = _json_dict(["api", "repos/{owner}/{repo}/commits/%s/check-runs" % ref]).get("check_runs")
    if isinstance(runs, list):
        out += [{"name": r.get("name"), "status": r.get("status"),
                 "conclusion": r.get("conclusion")}
                for r in runs if isinstance(r, dict)]
    statuses = _json_dict(["api", "repos/{owner}/{repo}/commits/%s/status" % ref]).get("statuses")
    if isinstance(statuses, list):
        out += [{"context": s.get("context"), "state": s.get("state")}
                for s in statuses if isinstance(s, dict)]
    return out


# How many recent dev-branch commits the DOCTOR's check-name audit looks back over (issue #406).
# SMALL AND FIXED, deliberately: every commit in the window costs TWO REST calls (check-runs +
# status), so this bound IS the API-burn budget of that read. Five is enough to see past a
# just-merged HEAD whose check-run object does not exist yet — the whole failure this window
# exists to remove — without turning an on-demand command into a page-walk.
DEV_CHECK_WINDOW = 5


def recent_branch_commits(branch, limit=DEV_CHECK_WINDOW):
    """The SHAs of `branch`'s most recent commits, newest first, capped at `limit` (issue #406).
    Fails closed to [] — the caller then reads the branch ref itself, which is exactly the
    single-commit read this window replaces, so a refusal is never worse than the old behavior.
    The ref is URL-encoded so a slashed branch does not break the query."""
    if not (isinstance(limit, int) and not isinstance(limit, bool) and limit > 0):
        limit = DEV_CHECK_WINDOW
    lst = _json_list(["api", "repos/{owner}/{repo}/commits?sha=%s&per_page=%d"
                      % (quote(branch, safe=""), limit)])
    out = []
    for c in lst[:limit]:
        if isinstance(c, dict) and isinstance(c.get("sha"), str) and c["sha"]:
            out.append(c["sha"])
    return out


def recent_branch_check_entries(branch, limit=DEV_CHECK_WINDOW, stop_when_seen=None):
    """Every check entry a bounded window of `branch`'s most recent commits reported, unioned —
    the DOCTOR's dev-surface evidence for the required_checks name cross-check (issue #406).

    Why a window. The doctor reads the PR surface across ~30 recent PRs but used to read the dev
    surface at the branch's single HEAD commit. Minutes after a merge that HEAD carries no
    check-run object yet (and an API blip folds the same way), so the read came back empty and the
    doctor announced "the branch has no check history yet to confirm it runs there" — a FAIL
    downgraded to a WARN — while the commits immediately behind it carried green runs of that exact
    check. The failure mode is false reassurance, never a false alarm, and recent commits are the
    evidence that removes it.

    The window is SMALL AND FIXED (DEV_CHECK_WINDOW = 5 commits, two REST calls each), which is
    the entire API-burn budget of this read. `stop_when_seen` shrinks it further at no cost to the
    answer: pass the names being audited and the walk stops as soon as every one has been observed
    here, because a name already seen on this surface cannot change its surface-membership by being
    seen again. The healthy case — a HEAD that reports everything — therefore costs the commit list
    plus one commit's reads, barely more than the HEAD-only read it replaces.

    Fails closed at every step, and always toward LESS evidence: an unreadable commit list falls
    back to the branch ref itself, and an unreadable commit contributes nothing. Less evidence can
    only turn a FAIL into "cannot verify yet"; it can never manufacture the observation that would
    let a misconfigured name read as fine.

    Entries carry branch_checks' normalized shapes, so gate.check_names folds them unchanged. This
    is a doctor-only read: the runner's freeze/unfreeze poll still reads branch_checks at the
    branch HEAD, where the question is what the mainline says RIGHT NOW, not whether a name has
    ever run there."""
    _seq = (set, list, tuple, frozenset)
    want = ({n for n in stop_when_seen if isinstance(n, str) and n}
            if isinstance(stop_when_seen, _seq) else set())
    refs = recent_branch_commits(branch, limit=limit) or [branch]
    out, seen = [], set()
    for ref in refs:
        entries = branch_checks(ref)
        out += entries
        if want:
            seen |= {key for key in (e.get("name") or e.get("context") for e in entries)
                     if isinstance(key, str) and key}
            if want <= seen:
                break
    return out


def recent_pr_check_entries(limit=30):
    """Every statusCheckRollup entry across the repo's most recent PRs (any state), flattened into
    one list, for the doctor's required_checks cross-check (issue #26). Raw rollup dicts (CheckRun
    / StatusContext) — the caller runs gate.check_names over them. Fails closed to [] — an
    unreadable PR list yields 'no evidence', which the doctor renders as 'cannot verify names yet',
    never as a false 'name not found'."""
    lst = _json_list(["pr", "list", "--state", "all", "--json", "statusCheckRollup",
                      "--limit", str(limit)])
    out = []
    for pr in lst:
        rollup = pr.get("statusCheckRollup") if isinstance(pr, dict) else None
        if isinstance(rollup, list):
            out += [c for c in rollup if isinstance(c, dict)]
    return out


def remote_branches(limit=100):
    """{branch name: tip sha} for the repo's remote branches (ONE page, up to `limit` — the
    janitor's sweep converges across approved runs, so pagination isn't worth its parse
    fragility; a repo holding >100 live branches has bigger debris problems than a truncated
    sweep). The tip riding along is the janitor's moved-since-the-PR guard: a delete is
    proposed only when the tip equals the PR's headRefOid. Fails closed to {}; an entry whose
    sha is unreadable is kept with tip None (the janitor then never proposes it)."""
    lst = _json_list(["api", "repos/{owner}/{repo}/branches?per_page=%d" % limit])
    out = {}
    for b in lst:
        if isinstance(b, dict) and isinstance(b.get("name"), str):
            commit = b.get("commit")
            sha = commit.get("sha") if isinstance(commit, dict) else None
            out[b["name"]] = sha if isinstance(sha, str) and sha else None
    return out


def open_prs_labeled(label, limit=100):
    """Open PRs carrying `label`, raw gh dicts (the janitor's `superseded` sweep — §C.4 6b
    leaves those PRs open by design, so only the owner's word ever closes one). Fails closed
    to []."""
    return _json_list(["pr", "list", "--state", "open", "--label", label,
                       "--json", "number,title,state,headRefName,labels",
                       "--limit", str(limit)])


def open_issues_activity(label, limit=200):
    """Open issues carrying `label`, WITH updatedAt — the janitor's dust clock (createdAt says
    when an issue was born; updatedAt says when it last saw ANY activity, which is what
    "gathering dust" means). Raw dicts; fails closed to []."""
    return _json_list(["issue", "list", "--state", "open", "--label", label,
                       "--json", "number,title,labels,updatedAt", "--limit", str(limit)])


# --------------------------- accidental-close audit (issue #229) ---------------------------
# What CLOSED an issue is not in any `gh issue list --json` field: `stateReason` says COMPLETED
# either way, whether a merged PR shipped the fix or a stray "fixes #189" in a ledger commit's
# message tripped GitHub's keyword close. Only the issue's ClosedEvent carries the closer, and only
# GraphQL exposes it — so this is the one GraphQL query in the adapter.
#
# It is a GraphQL query rather than N REST timeline reads on purpose (the owner's API-burn ruling,
# 2026-07-16 — one read proving the whole set, never per-issue reads): ONE call answers 100 issues
# for ~3 rate-limit points, where the REST /issues/{n}/timeline endpoint would cost one call each.
# Nothing on the runner's per-tick path calls this; `superlooper doctor` and the janitor's sweep do,
# both of which are on-demand.
_CLOSERS_QUERY = """
query ClosedIssueClosers($owner: String!, $name: String!, $page: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: CLOSED, first: $page, after: $cursor,
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        stateReason
        closedAt
        timelineItems(last: 1, itemTypes: [CLOSED_EVENT]) {
          nodes {
            ... on ClosedEvent {
              closer {
                __typename
                ... on PullRequest { number merged }
                ... on Commit {
                  oid
                  messageHeadline
                  associatedPullRequests(first: 5) { nodes { number merged } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
_CLOSERS_PAGE = 100          # GitHub's max page size for a connection

# closed_issue_closers' read-health contract, one field wider than ReadHealth. `truncated` is the
# difference between "no issue in this repo was accidentally closed" and "no issue in the WINDOW I
# looked at was" — and the doctor prints an unqualified "every" off this call, so the window has to
# be part of the answer rather than a docstring's promise. Ordering is UPDATED_AT DESC, not
# CREATED_AT: a close bumps updatedAt, so the issues this audit is about sort to the front whatever
# their age, and truncation drops the least-recently-touched instead of the oldest-numbered.
ClosedRead = collections.namedtuple("ClosedRead", ["value", "ok", "truncated"])


def _closer_of(node):
    """The normalized `closer` for one issue node, or None when the issue was closed BY HAND (no
    closer at all — the owner's own word) or when the timeline is unreadable. Shapes:
        {"type": "pull_request", "number": int|None, "merged": bool|None}
        {"type": "commit", "oid": str, "headline": str, "merged_prs": [int, ...]}
    `merged_prs` is the exemption lib/closures.py needs: a commit that a MERGED PR carries went
    through the gate, so its keyword close is not the accidental class. An unreadable
    associatedPullRequests block yields NO merged_prs key at all, which closures fails closed on
    (it cannot prove the commit was bare) — never an empty list, which would read as proof."""
    events = (node.get("timelineItems") or {}).get("nodes") if isinstance(node, dict) else None
    ev = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else {}
    closer = ev.get("closer")
    if not isinstance(closer, dict):
        return None                              # closed by hand, or nothing readable
    kind = closer.get("__typename")
    if kind == "PullRequest":
        return {"type": "pull_request", "number": closer.get("number"),
                "merged": closer.get("merged")}
    if kind != "Commit":
        return None                              # an unknown closer type is not a bare commit
    out = {"type": "commit", "oid": closer.get("oid"),
           "headline": closer.get("messageHeadline")}
    assoc = closer.get("associatedPullRequests")
    nodes = assoc.get("nodes") if isinstance(assoc, dict) else None
    if isinstance(nodes, list):
        out["merged_prs"] = [p["number"] for p in nodes
                             if isinstance(p, dict) and p.get("merged") is True
                             and type(p.get("number")) is int]
    return out


def closed_issue_closers(limit=400):
    """Every CLOSED issue with WHAT closed it, as a ClosedRead(list, ok, truncated) — the
    accidental-close audit's one read (issue #229). Each record:
        {"number": int, "title": str, "stateReason": str, "closedAt": str, "closer": <see
         _closer_of> | None}
    consumed by lib/closures.flagged.

    `ok` follows the #21/#61 refused-vs-answered-empty discipline, and it MATTERS here in the
    direction the other reads do not: a refused read yields [] which flags nothing, so the janitor
    stays safe by ignoring it — but the doctor would print a confident "no accidental closes found"
    off a GitHub outage. With `ok` it says "could not read" instead. ok=False on any refused page,
    an unparseable/wrong-typed body, or a GraphQL `errors` payload; `value` is then [].

    `truncated` is True when the repo has MORE closed issues than `limit` — the audit looked at a
    window, not at everything, and the caller must not render that as "every issue". Bounded at
    `limit` (4 pages of 100 by default) rather than walking a decade of history on every doctor run.

    The page loop is bounded three ways, because an unbounded `while` around a 30s subprocess is a
    hang: the running total against `limit`, a hard page counter, and a repeated-cursor guard. A
    degraded endpoint that answers `{"nodes": [], "hasNextPage": true}` with a FRESH cursor every
    time defeats the first two on their own."""
    limit = max(0, int(limit))
    max_pages = -(-limit // _CLOSERS_PAGE) + 1        # ceil, +1 slack for short pages
    out, cursor, seen_cursors, pages = [], None, set(), 0
    while len(out) < limit and pages < max_pages:
        args = ["api", "graphql", "-F", "owner=:owner", "-F", "name=:repo",
                "-F", "page=%d" % min(_CLOSERS_PAGE, max(1, limit - len(out))),
                "-f", "query=" + _CLOSERS_QUERY]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        rc, body = _run(args)
        pages += 1
        if rc != 0:
            return ClosedRead([], False, False)  # refused: never a partial list dressed as whole
        try:
            doc = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return ClosedRead([], False, False)
        if not isinstance(doc, dict) or doc.get("errors"):
            return ClosedRead([], False, False)  # a GraphQL error payload is rc 0 but not an answer
        repo = (doc.get("data") or {}).get("repository") if isinstance(doc.get("data"), dict) else None
        conn = repo.get("issues") if isinstance(repo, dict) else None
        if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
            return ClosedRead([], False, False)  # wrong-typed body: not a clean answer
        for node in conn["nodes"]:
            if not isinstance(node, dict):
                continue
            out.append({"number": node.get("number"), "title": node.get("title"),
                        "stateReason": node.get("stateReason"), "closedAt": node.get("closedAt"),
                        "closer": _closer_of(node)})
        page = conn.get("pageInfo") if isinstance(conn.get("pageInfo"), dict) else {}
        if page.get("hasNextPage") is not True:
            return ClosedRead(out, True, False)  # genuinely the end: the audit saw everything
        # More pages EXIST. Whether we stopped by our own bound or because the server stopped
        # handing out usable cursors, the answer is a window either way — a missing or repeated
        # cursor under hasNextPage=true is a server we cannot follow, not a repo we finished.
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            return ClosedRead(out, True, True)
        seen_cursors.add(cursor)
    # Left the loop with pages still waiting: the audit saw a window, and must say so.
    return ClosedRead(out, True, True)


def sl_head_prs(limit=1000):
    """Every PR the repo has ever had on an `sl/*` head — {"number", "state", "headRefName"} — as a
    ReadHealth(list, ok), for the doctor's "did an sl/i<N> PR ever exist?" evidence column (issue
    #229). ONE list read however many issues are flagged, and it is issued only when at least one
    IS flagged.

    `ok` is what stops the evidence line from FABRICATING a negative. Its strongest sentence is
    "nothing was ever built for it", and an unhealthy read produces the same [] as a genuinely
    PR-less issue — so without `ok` a rate-limit blip (or a repo that outgrew the bound) prints a
    proven-sounding never-built claim about work that shipped months ago. ok=False on any refusal
    AND on a full page, because a list capped at `limit` cannot prove a PR's absence: an old
    `sl/i5` PR falls off the end long before anything is wrong with GitHub."""
    rh = _json_list_health(["pr", "list", "--state", "all", "--json", "number,state,headRefName",
                            "--limit", str(limit)])
    prs = [{"number": p.get("number"), "state": p.get("state"),
            "headRefName": p["headRefName"]}
           for p in rh.value
           if isinstance(p, dict) and isinstance(p.get("headRefName"), str)
           and p["headRefName"].startswith("sl/")]
    return ReadHealth(prs, rh.ok and len(rh.value) < limit)


# GitHub's REST hard maximum for `per_page`. It CLAMPS silently — ask for 200 and you get 100 rows
# with no error and no signal — so a completeness guard written against any larger number can never
# fire, and would report a truncated list as complete. (Verified live: a repo with hundreds of
# branches answers `branches?per_page=200` with exactly 100.) The #165 inert-guard class: a check
# written against a bound the server does not honor is not a check.
REST_PAGE_MAX = 100


def remote_branches_health(limit=REST_PAGE_MAX):
    """remote_branches() as a ReadHealth({name: tip}, ok) — the evidence column's branch half
    (issue #229). Same reasoning as sl_head_prs: absence is only evidence when the read was clean
    AND complete, so a FULL page (the repo has more branches than one page holds) reads ok=False
    rather than letting a truncated list prove a branch never existed — the doctor would otherwise
    print "nothing was ever built for it" about work sitting on a live `sl/i<N>` branch, and `sl/*`
    sorts late alphabetically, so loop branches are exactly the ones a cut-off list loses. The
    janitor keeps using the bare remote_branches(): there, an unreadable branch simply is not
    proposed, which is already the safe direction.

    `limit` is clamped to REST_PAGE_MAX so the guard is compared against what GitHub will ACTUALLY
    return, never against a number it silently ignores."""
    page = max(1, min(int(limit), REST_PAGE_MAX))
    lst = _json_list_health(["api", "repos/{owner}/{repo}/branches?per_page=%d" % page])
    out = {}
    for b in lst.value:
        if isinstance(b, dict) and isinstance(b.get("name"), str):
            commit = b.get("commit")
            sha = commit.get("sha") if isinstance(commit, dict) else None
            out[b["name"]] = sha if isinstance(sha, str) and sha else None
    return ReadHealth(out, lst.ok and len(lst.value) < page)


def default_branch():
    """The repo's default branch name (e.g. 'main'/'master'/'develop'), or None if gh can't
    answer (unreachable, unauthenticated, or a wrong-typed ref). adopt writes this as `dev_branch`
    so a repo whose default is not 'main' doesn't fail every worktree creation off origin/main
    (issue #28). None is the honest fallback: adopt keeps the template default and prints a hint."""
    ref = _json_dict(["repo", "view", "--json", "defaultBranchRef"]).get("defaultBranchRef")
    name = ref.get("name") if isinstance(ref, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def branch_exists(branch):
    """True iff `gh api .../branches/<branch>` returns 0 (the branch is present on the remote).
    ANY nonzero exit -> False. This is DELIBERATELY conservative: a genuine 404 and a transient
    blip (5xx/timeout/rate-limit) both read as False, so a rare gh hiccup can produce a false
    'missing' — but it can NEVER produce a false 'present'. That direction is the safe one for
    doctor's use (issue #28): a false FAIL is a re-runnable annoyance on a human-run check, whereas
    masking a genuinely-missing base branch would let every launch die at worktree creation
    undetected. (Not worth distinguishing 404 from 5xx by parsing gh's stderr — that substring
    match is brittle across gh versions and could misclassify a real 404 as transient, the worse
    error.) The ref is URL-encoded so a slashed branch doesn't split into extra path segments."""
    rc, _ = _run(["api", "repos/{owner}/{repo}/branches/%s" % quote(branch, safe="")])
    return rc == 0


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


# The exit-interview verification read (#215) needs each child's open/closed state on top of the
# standard issue fields: a CLOSED child still accounts for a finding (the owner already acted).
_CHILD_FIELDS = _ISSUE_FIELDS + ",state"


def child_issues_health(parent_num):
    """child_issues() as a ReadHealth(children, ok) — the issue #215 exit-interview verification
    read, and the ONE GitHub read a finishing investigation adds (owner API-burn ruling,
    2026-07-16: one search proving every FINDINGS-FILED ref at once — parent linkage included —
    never per-ref issue reads). Each child rides with `labels` and `state` so
    gate.accounted_child_nums can judge needs-owner / released / closed without further reads.
    refused != empty (the #21/#61 discipline): ok=False on any refusal, and the gate WAITS on it
    rather than reading the fail-closed [] as 'no children exist' — which would block a truthful
    reply forever."""
    rh = _json_list_health(["issue", "list", "--state", "all",
                            "--search", '"parent: #%d" in:body' % parent_num,
                            "--json", _CHILD_FIELDS, "--limit", "200"])
    kids = [c for c in rh.value
            if isinstance(c, dict)
            and _issues.parse_loop_metadata(c.get("body", "")).get("parent") == parent_num]
    return ReadHealth(kids, rh.ok)


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


def reopen_issue(num, comment=None):
    """Reopen an issue — the janitor's approved accidental-close action (issue #229), only ever
    invoked on the owner's explicit word, never from any automatic path. The comment is the audit
    trail: it names the commit whose message keyword closed the issue, so the reopen explains
    itself on the issue where the next reader will look. True on success."""
    args = ["issue", "reopen", str(num)]
    if comment:
        args += ["--comment", comment]
    rc, _ = _run(args)
    return rc == 0


def delete_branch(branch):
    """Delete a remote branch ref — the janitor's approved stale-branch action, only ever
    invoked on the owner's explicit word, never from any automatic path. This deletes a ref
    outright; it never rewrites one (there is still no force machinery anywhere). Slashes in
    the branch stay raw ref segments; anything stranger is percent-encoded. True on success."""
    rc, _ = _run(["api", "repos/{owner}/{repo}/git/refs/heads/%s" % quote(branch, safe="/"),
                  "-X", "DELETE"])
    return rc == 0


def close_pr(num, comment=None):
    """Close a PR without merging — the janitor's approved superseded-PR action (the branch
    stays; deleting it is a SEPARATE proposal on a later sweep). True on success."""
    args = ["pr", "close", str(num)]
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


# A merge refusal reason (gh stderr) rides into a park memo / notify / issue comment, so it is
# bounded — a chatty or pathological gh error can't blow the memo up (issue #27).
MERGE_REFUSAL_REASON_CHARS = 500


def _merge_refusal_reason(stderr):
    """A single-line, bounded tail of gh's stderr — the honest 'why' behind a refused merge, safe
    to drop into a memo. Empty/None -> "". Whitespace (incl. newlines) is collapsed so multi-line
    gh output reads as one line; then the tail is kept within the char bound."""
    s = " ".join((stderr or "").split())
    return s[-MERGE_REFUSAL_REASON_CHARS:] if s else ""


def merge_pr(num, method="squash", head_oid=None):
    """Merge a PR with the configured method (squash default, §B.4). Returns (ok, reason): (True,
    "") on success, (False, <bounded gh stderr tail>) when GitHub REFUSES the merge — ordinary
    branch protection (required approvals / strict up-to-date) or a token without merge rights
    (issue #27). The caller counts refusals and, at the cap, parks the issue to William with the
    reason. There is no force path anywhere — the runner never force-pushes, and never bypasses
    branch protection; it surfaces the refusal so the owner can act on it.

    `head_oid` pins the merge to the exact commit the gate judged (#154). The gate matches the
    review verdict against a POLLED snapshot of the head (up to GH_POLL_SECONDS old), but the
    merge lands on whatever the head is NOW: without this the verdict is verified against one
    commit and enforced against none, so a worker pushing inside the poll window lands unreviewed
    code under a perfectly matching pin. `--match-head-commit` makes GitHub refuse when the head
    moved, which is the honest answer — the refusal ladder above counts it, and the next poll sees
    the new head and asks for a re-review (review_stale) rather than merging. A caller with no
    readable oid sends no constraint (gh rejects an empty value; an unconstrained merge is exactly
    the pre-#154 behaviour, never worse)."""
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    argv = ["pr", "merge", str(num), flag]
    if isinstance(head_oid, str) and head_oid.strip():
        argv += ["--match-head-commit", head_oid.strip()]
    rc, _, err = _run_full(argv)
    return (True, "") if rc == 0 else (False, _merge_refusal_reason(err))
