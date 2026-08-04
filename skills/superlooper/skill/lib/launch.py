"""The ONE spawn path (issue #308) — pre-flight, then the five-verb wrapper's ``spawn``.

There were THREE spawners, and the plan's §9 named the landmine plainly: rewire only the runner
and the watchdog + dashboard Fixer keep calling a launcher that no longer exists — i.e. no
unattended repair at exactly the moment repair is needed. So every caller that creates a session
comes through here, and this module is the only thing left that does:

===================  ==========================  ===========================================
caller               spawns                      trigger
===================  ==========================  ===========================================
runner               worker ``i<N>``             an approved issue (launch/recover/regenerate)
watchdog             sl-debugger ``d<N>``        unattended fault, no owner present
dashboard Fixer      sl-debugger ``d<N>``        the owner taps Debug (via ``superlooper debug``)
``superlooper resume``  either, revived          an operator re-enters an interrupted flight
===================  ==========================  ===========================================

The revive path is the fourth, added AFTER the plan's audit — which is exactly the class the
landmine named, so it moves in this wave with the other three.

**What this module is, and what it deliberately is not.** It owns the PRE-FLIGHT half of the old
cmux launcher: identity, the base-ref check, worktree creation, pretrust, the session id, the
brief, run-state hygiene, and the environment a pane is handed. It owns none of the host: that is
``session_host`` (issue #304), the single doorway, and this file talks to it through the five
verbs and nothing else. It owns none of the agent either: the claude/codex command line lives in
``start-session.sh`` alone (the agent-boundary rule), and the handoff below is precisely how it
stays there.

**How a session actually starts, and why it looks indirect.** The host's own start verb TYPES the
bare agent word — ``claude``, ``codex`` — into the pane's own login shell, and then judges
readiness from the screen (measured, not assumed — reports/i308.md). The pane's shell sources
``~/.zshrc``, which sources superlooper's launch shim, which — seeing this launch's ``SL_*`` in the
pane environment — arms a one-shot shell function named after the agent. So the typed word runs
``start-session.sh``, and the ENTIRE in-pane floor survives the port unchanged: the worker
singleton, the #301 env scrub, the #299 gh-auth assert, the #303 binary pin, the #40 stderr tail
and the RC-DEADPANE exited marker.

That indirection is load-bearing rather than clever. The floor MUST run inside the pane: the
pane's shell sources the operator's rc files AFTER this launcher has finished, which is exactly
where the realized ``ANTHROPIC_API_KEY`` lived, so a launcher that scrubbed its own environment
would prove nothing about the one a worker gets. And the agent's flags must be built where the
agent-specific knowledge already lives. Hence: this module names ``SL_*`` and passes NO native
agent args at all.

**Exit codes are the contract** — ``evidence.py`` reads them, and each was paid for:

===  ==================================================================================
0    delivered, and PROVEN delivered (the floor's own per-launch sentinel)
1    refused before any pane could host a worker; nothing about the session is at fault
2    a pane was created and no worker started in it
3    the worktree base ref is missing — a per-repo config fault, never a hollow launch
4    THIS session's own environment cannot authenticate to GitHub
5    the RUNNER's environment cannot authenticate — a channel fault, the queue holds
6    THIS session's own environment is poisoned and the floor could not clean it
64   the configured agent is not one this stack can launch (repo-wide, not one issue's)
===  ==================================================================================

The stderr text is contract too: ``evidence.py`` classifies on phrases before it falls back to
rc, so the refusals below keep the exact wording its needle table matches.
"""
import fcntl
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field

import loopstate
import sanitize
import session_host

# The two mode guards, spelled here exactly as the cmux launcher spelled them. They are what stop
# a debugger id from being launched as a worker and vice versa — fail closed on WRONG-TYPED input,
# not merely on unsafe input. Without the worker guard a caller bug routes a d<N> through worktree
# creation and the issue counter; without the debugger guard an i<N> silently skips worktree
# creation and runs in whatever directory the caller happened to pass.
WORKER_RE = re.compile(r"^i[0-9]+$")
DEBUGGER_RE = re.compile(r"^d[0-9]+$")

OK = 0
ABORTED = 1
NOT_DELIVERED = 2
BASE_MISSING = 3
AUTH_DEAD = 4
AUTH_DEAD_RUNNER = 5
ENV_POISONED = 6
UNSUPPORTED_AGENT = 64

AGENTS = ("claude", "codex")

# The launch-floor denylist (#301), named HERE only to keep the launcher from being the one that
# hands the poison over. This is NOT the floor — the floor is the post-scrub assert that runs in
# the session's own environment, because only code running there can prove anything about it.
# Whole families, never a fixed list: a new CLAUDE_CODE_* or ANTHROPIC_* shipped by a future
# release is poison the day it appears, and a list would learn about it from a bill.
_POISON_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_POISON_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "XDG_CONFIG_HOME")

# Defense in depth beside the fence (#305). The wrapper strips these too, and the host injects its
# own pane-identity vars whatever anyone passes — which is precisely why the token, and not this,
# is the fence. What this rules out is the launcher becoming a second, quieter grant path.
_HOST_ENV_PREFIX = "HERDR"

_GH_PROBE_SECONDS = 10          # a black-holed network must not eat the caller's whole timeout
_START_TIMEOUT_MS = 45000       # the pane's floor (a bounded gh read) runs before the agent does

_VERIFY_SECONDS = 30            # the floor stamps its sentinel BEFORE the agent starts, so this
_VERIFY_PAUSE = 0.25            # window is nearly always already satisfied when spawn returns
_MARKER_STALE_MINUTES = 5       # only genuinely abandoned self-refusal markers are swept

# A GitHub login is [A-Za-z0-9-] and nothing else. `gh` merges its diagnostics into the capture and
# an error path prints MULTI-LINE text, so the test is a whole-string match: a line-anchored one
# would happily take a bare word from line 2 of an error message as "the expected login".
_LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")


@dataclass(frozen=True)
class Ran:
    """One finished subprocess, as this module needs it."""
    rc: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Result:
    """The launcher's whole answer. ``stderr`` is diagnosis, and ``evidence.py`` reads it."""
    rc: int
    stderr: str = ""
    stdout: str = ""
    session: object = None


@dataclass
class Spec:
    """One launch request, fully resolved by its caller.

    ``cwd`` selects the mode and nothing else does: present means the in-place debugger path
    (``d<N>``, no worktree and no branch), absent means the worker path (``i<N>``, a worktree off
    ``origin/<dev_branch>``). Deriving the mode from the id instead would make a caller's mistake
    silent — the guards below exist to make it loud.
    """
    id: str
    run_root: str
    repo: str = ""
    dev_branch: str = "main"
    cwd: str = None
    engine_bin: str = ""
    agent: str = "claude"
    model: str = ""
    effort: str = ""
    attended: bool = False
    resume_session_id: str = ""
    expect_gh_login: str = None          # None -> resolve it here, in the runner's own environment
    gh_bin: str = ""
    claude_bin: str = ""                 # #303: named ONLY when this launcher actually has a pin
    session_name: str = ""
    label: str = ""
    verify_seconds: int = _VERIFY_SECONDS
    start_timeout_ms: int = _START_TIMEOUT_MS
    codex: dict = field(default_factory=dict)
    forwarded_env: dict = None           # what the caller's own environment offers (scrubbed here)


class Edges:
    """Every external edge: subprocesses, the clock, and the per-launch token.

    Injected for the same reason ``session_host.Probe`` is — so no test resolves a real git, gh or
    pretrust, and so the launch token can be staged deterministically.
    """

    def run(self, argv, timeout=None, cwd=None, env=None):
        try:
            r = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=env)
            return Ran(r.returncode, r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired:
            return Ran(124, "", "no answer within %ss" % timeout)
        except (OSError, ValueError) as e:
            return Ran(127, "", "could not run %s: %s" % (argv[0] if argv else "?", e))

    def sleep(self, seconds):
        time.sleep(seconds)

    def token(self):
        """This launch's own key. Under cmux it was the new tab's surface UUID; the host's pane id
        is not known until the wrapper has already created the workspace, so the launcher mints its
        own — which is strictly better: a stale or overlapping launch stamps a different token and
        cannot false-verify this one, whatever the host does with its ids."""
        return uuid.uuid4().hex


class WorktreeLock:
    """The critical section around ``git worktree add`` — c17's salvaged half.

    Only the flock half of c17 was adopted: its remove-force idempotency would destroy the
    preserved worktree the #190/#168 unsaved-work rulings protect. ``fcntl.flock`` rather than
    shell ``flock(1)``, which macOS does not ship.

    Best effort by design: a filesystem that cannot lock must not refuse a launch. The lock
    narrows a race between two launches on one repo; it is not a correctness guarantee, and git's
    own index lock is the backstop underneath it.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._fh = open(self.path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            self._close()
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._close()
        return False

    def _close(self):
        try:
            if self._fh is not None:
                self._fh.close()
        except OSError:
            pass
        self._fh = None


# --------------------------------------------------------------------------- the launch

def launch(spec, host=None, edges=None):
    """Pre-flight, then spawn, then PROVE a worker started. Never raises."""
    edges = edges if edges is not None else Edges()
    try:
        return _launch(spec, host, edges)
    except Exception as e:                                   # noqa: BLE001 - a launcher may not
        # A launcher that raised would take its caller's tick with it, and the runner's whole
        # design is that no single lane can stop the loop. Report, never propagate.
        return Result(ABORTED, "[%s] the launcher failed unexpectedly: %s" % (spec.id, e))


def _launch(spec, host, edges):
    # ---- identity + mode ------------------------------------------------------------------
    try:
        iid = sanitize.worktree_id(spec.id)
    except (ValueError, TypeError):
        return Result(ABORTED, "[%s] id sanitize validation failed — not launching" % (spec.id,))
    debugger = spec.cwd is not None
    if debugger:
        if not DEBUGGER_RE.match(iid):
            return Result(ABORTED, "[%s] --cwd mode is for debugger (d<N>) ids only — refusing"
                          % iid)
    elif not WORKER_RE.match(iid):
        return Result(ABORTED, "[%s] worker mode expects an issue id (i<N>) — refusing" % iid)
    if spec.agent not in AGENTS:
        return Result(UNSUPPORTED_AGENT,
                      "[%s] unsupported agent '%s' (expected: %s)"
                      % (iid, spec.agent, " or ".join(AGENTS)))

    # ---- the expected gh login (issue #299) -----------------------------------------------
    # Read ONCE here, in the runner's own environment, and handed down: the session's own assert
    # needs something to assert AGAINST, and the honest expectation is the loop's OWN identity.
    # FAIL CLOSED and fail EARLY — before the worktree and before any host RPC, so a refusal costs
    # no orphan pane and no leftover checkout (the base-missing discipline, #28).
    expect_login = spec.expect_gh_login
    if not expect_login:
        probe = edges.run([spec.gh_bin or "gh", "api", "user", "--jq", ".login"],
                          timeout=_GH_PROBE_SECONDS)
        # gh's OWN WORDS, whatever the rc: an error path prints the only account of WHY, and
        # discarding it would flatten not-logged-in, rate-limited, network-down and
        # gh-not-installed into one indistinguishable memo. The SHAPE CHECK below, never the rc,
        # decides whether it is a usable login.
        expect_login = (probe.stdout or probe.stderr or "").strip()
    if not _LOGIN_RE.match(expect_login or ""):
        return Result(AUTH_DEAD_RUNNER, "\n".join([
            "[%s] GH AUTH DEAD (runner env): `gh api user` did not return a usable login, so "
            "there is no identity to launch any session against — got: %s"
            % (iid, expect_login or "<no answer>"),
            "[%s] Run `gh auth login --hostname github.com` as the account that owns the loop "
            "repo. NOT launching; no session was created." % iid]))

    # ---- where the session lives ----------------------------------------------------------
    branch = ""
    if debugger:
        if not os.path.isdir(spec.cwd):
            return Result(ABORTED, "[%s] --cwd dir does not exist: %s" % (iid, spec.cwd))
        # ABSOLUTE and PHYSICAL: the pane's shell starts in its own default dir, so a relative one
        # would name nothing there.
        worktree = os.path.realpath(spec.cwd)
    else:
        if not spec.repo:
            # Named rather than left to git. Without it `git -C ''` fails, the base-ref probe
            # fails with it, and the launch would exit 3 telling the owner to fix a dev_branch
            # that is not what went wrong — the mis-blame the whole evidence table exists to end.
            return Result(ABORTED, "[%s] no target repo was given for a worker launch (SL_REPO) — "
                                   "not launching" % iid)
        state_path = os.path.join(spec.run_root, "state", "issues.json")
        try:
            issue = loopstate.load(state_path)["issues"][iid]
            branch = sanitize.branch(issue["branch"])
            base_branch = sanitize.branch(spec.dev_branch)
        except (KeyError, TypeError, ValueError, OSError):
            return Result(ABORTED,
                          "[%s] issues.json load / sanitize validation failed — not launching"
                          % iid)
        worktree = os.path.join(spec.run_root, "worktrees", iid)
        base = "origin/%s" % base_branch
        failed = _make_worktree(spec, edges, iid, worktree, branch, base)
        if failed is not None:
            return failed

    # herdr does NOT remove the first-run trust dialog — the supervised run hit one and the host
    # classified that blocked pane `idle`. So S9 pre-trust stays load-bearing, and runs for the
    # debugger's in-place directory too (an owner-tap repair on an unvisited repo is exactly the
    # case that would hang).
    #
    # `pretrust.sh` on BOTH agent paths, which is a FAITHFUL port and not an oversight: the cmux
    # launcher called exactly this one whatever the configured agent was, and `pretrust-codex.sh`
    # has no caller anywhere. Wiring it here would be a real fix and an unrequested behaviour
    # change inside a migration, so it is filed rather than smuggled in (see the follow-up issue).
    edges.run([os.path.join(spec.engine_bin, "pretrust.sh"), worktree], timeout=60)

    # ---- session identity (issue #298) ----------------------------------------------------
    # Identity handed down, never self-asserted. A RELAUNCH always mints anew (`--session-id` on
    # an already-used id is an error); a REVIVE passes the recorded id straight through, because
    # minting on a revive would strand the very transcript the operator asked to re-enter.
    # CLAUDE ONLY: `--session-id`/`--resume` are Claude Code's spelling, and minting for a codex
    # repo would write a UUID into lane state that names no conversation anywhere.
    resume = bool(spec.resume_session_id)
    session_id = ""
    if spec.agent == "claude":
        session_id = spec.resume_session_id or str(uuid.uuid4())

    # A revive opens on its OWN brief and leaves the lane's original untouched beside it: the
    # crash-recovery relaunch re-runs this without rebuilding the brief, so a preamble written
    # over briefs/<id>.md would later be delivered verbatim to a brand-new, empty session. FAIL
    # CLOSED, never fall back — substituting the original would deliver the whole issue brief as a
    # NEW instruction into a conversation that already built it.
    brief = os.path.join(spec.run_root, "briefs",
                         "%s.resume.md" % iid if resume else "%s.md" % iid)
    if not os.path.isfile(brief):
        return Result(ABORTED, "[%s] missing brief %s" % (iid, brief))

    # ---- run-state hygiene ----------------------------------------------------------------
    token = edges.token()
    _prepare_state(spec, iid, session_id, token)

    # ---- the doorway ----------------------------------------------------------------------
    host = host if host is not None else session_host.SessionHost()
    name = spec.session_name or ("superlooper %s%s" % (iid, " (%s)" % branch if branch else ""))
    pane_env = pane_environment(spec, iid, session_id, resume, expect_login, token)
    try:
        session = host.spawn(name=iid, cwd=worktree, env=pane_env, kind=spec.agent,
                             label=spec.label or name,
                             start_timeout_ms=int(spec.start_timeout_ms))
    except session_host.HostError as e:
        # The session may have refused ITSELF from inside its own environment, in which case the
        # agent never started and the host only ever saw a pane that stayed a shell. Read the
        # floor's own markers BEFORE blaming the machinery: without them a poisoned environment
        # and dead GitHub auth both read as "the launch never delivered", and the park memo would
        # send the owner to debug the launch stack.
        refusal = _self_refusal(spec, iid, token)
        if refusal is not None:
            return refusal
        return Result(NOT_DELIVERED, "\n".join([
            "[%s] LAUNCH NOT DELIVERED: no session was created — %s" % (iid, e),
            "[%s] Nothing was left running; this is a delivery-channel fault, not the issue's."
            % iid]))

    # ---- prove a WORKER started, not merely that a process exists -------------------------
    # The host reporting `interactive_ready` is not our session: it classified a pane whose only
    # occupant was a sleeping stub as idle and ready. start-session.sh stamps its per-launch
    # sentinel BEFORE the agent starts, so by here it is normally already there.
    if not _await_start(spec, iid, token, edges):
        refusal = _self_refusal(spec, iid, token)
        if refusal is not None:
            _teardown(host, session)
            return refusal
        _teardown(host, session)
        return Result(NOT_DELIVERED, "\n".join([
            "[%s] LAUNCH NOT DELIVERED: a pane was created but no worker started in it within "
            "%ss." % (iid, spec.verify_seconds),
            "[%s] the launch shim did not hand the agent verb to start-session.sh — is it "
            "installed? (bin/install-launch-shim.sh)" % iid,
            "[%s] Tore the pane down; NOT marking active." % iid]))

    _record_delivery(spec, iid, session, debugger, resume)
    return Result(OK, "", "[launch] %s  branch=%s pane=%s ws=%s name='%s' (delivery verified)"
                  % (iid, branch or "<none>", session.pane, session.workspace, name),
                  session=session)


# --------------------------------------------------------------------------- pre-flight pieces

def _make_worktree(spec, edges, iid, worktree, branch, base):
    """Create the lane worktree, or return the Result that refuses this launch.

    PRESERVATION: an existing checkout is REUSED, never removed. A relaunch or regenerate lands in
    the id's own worktree with its unpushed work intact (#190/#168) — the launcher has no path
    that destroys one, which is why c17's remove-force half was rejected.
    """
    if os.path.isdir(worktree):
        return None
    with WorktreeLock(os.path.join(spec.run_root, "state", "worktree.lock")):
        if os.path.isdir(worktree):          # another launch won the race and made it for us
            return None
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        fresh = edges.run(["git", "-C", spec.repo, "worktree", "add", "-b", branch, worktree,
                           base], timeout=120)
        if fresh.rc == 0:
            return None
        # The fallback attaches an EXISTING branch — a relaunch or regenerate reuses the name.
        attach = edges.run(["git", "-C", spec.repo, "worktree", "add", worktree, branch],
                           timeout=120)
        if attach.rc == 0:
            return None
        exists = edges.run(["git", "-C", spec.repo, "rev-parse", "--verify", "--quiet",
                            "%s^{commit}" % base], timeout=30)
        if exists.rc != 0:
            # Issue #28's DISTINCT code, so the park memo blames the branch rather than the launch
            # machinery — and a missing base never becomes a hollow launch.
            return Result(BASE_MISSING,
                          "[%s] worktree base '%s' does not exist on '%s' — the configured "
                          "dev_branch is not on origin, so no worktree can be created. Run "
                          "'superlooper doctor' and set dev_branch to the repo's default, then "
                          "re-approve." % (iid, base, spec.repo))
        return Result(ABORTED,
                      "[%s] could not create the worktree at '%s' for branch '%s' (base '%s' "
                      "exists): %s" % (iid, worktree, branch, base,
                                       (attach.stderr or fresh.stderr or "").strip()))


def pane_environment(spec, iid, session_id, resume, expect_login, token):
    """Everything the in-pane floor is handed, and nothing else.

    A pane inherits NOTHING useful from this launcher — its shell is spawned by the host and
    sources the operator's own rc files — so every ``SL_*`` the floor reads must be NAMED here.
    Three rules, each paid for:

    1. **No poison is forwarded.** The floor itself (in the pane, in the session's own
       environment) is what proves the scrub; this only makes sure the launcher is not the one
       handing it over. Note what it CANNOT do: the pane's shell sources ``~/.zshrc`` after we are
       gone, which is exactly where the realized API key lived — hence the floor, not this.
    2. **No ``HERDR_*`` at all.** The wrapper strips them too. This is the near side of the same
       rule, so the launcher can never become a second, quieter grant path for the fence's token.
    3. **Nothing agent-specific.** No claude/codex flag is composed here; the pane's shim hands
       the typed agent verb to ``start-session.sh``, which owns every one of them.
    """
    env = {}
    for key, value in (spec.forwarded_env or {}).items():
        key = str(key)
        if key.startswith(_HOST_ENV_PREFIX) or is_poison(key):
            continue
        env[key] = str(value)
    env.update({
        "SL_ISSUE_ID": iid,
        "SL_RUN_ROOT": spec.run_root,
        "SL_SESSION_NAME": spec.session_name or "superlooper %s" % iid,
        "SL_MODEL": str(spec.model or ""),
        "SL_EFFORT": str(spec.effort or ""),
        "SL_AGENT": spec.agent,
        # Is a PERSON at the keyboard? `1` only for an owner tap. PINNED on every path rather than
        # inherited, so an ambient `export SL_ATTENDED=1` in the shell or LaunchAgent a watchdog
        # check runs from cannot hand an unattended session a dialog nobody can answer.
        "SL_ATTENDED": "1" if spec.attended else "",
        "SL_SESSION_ID": session_id,
        "SL_RESUME": "1" if resume else "",
        "SL_EXPECT_GH_LOGIN": expect_login,
        "SL_GH": spec.gh_bin or "gh",
        "SL_START_TOKEN": token,
        # The pane knows no engine paths, and its shim must hand the typed agent verb to a real
        # file. Absolute, resolved by whoever built the Spec.
        "SL_START_SESSION": os.path.join(spec.engine_bin, "start-session.sh"),
    })
    for key in ("dangerous_bypass", "bypass_hook_trust", "no_alt_screen"):
        env["SL_CODEX_%s" % key.upper()] = str((spec.codex or {}).get(key, ""))
    # #303, and the asymmetry is deliberate: NAMED when this launcher actually has a pin (else a
    # pin the operator verified with `doctor --stack` would simply not reach the worker), ABSENT
    # when it does not (an unconditional empty value would BLANK the pin the pane's own rc file
    # sets, silently converting a configured launch back into PATH luck).
    if spec.claude_bin:
        env["SL_CLAUDE"] = spec.claude_bin
    return env


def is_poison(name):
    """Is this variable one the launch floor exists to keep out of a session? (issue #301)"""
    name = str(name)
    return name in _POISON_NAMES or any(name.startswith(p) for p in _POISON_PREFIXES)


def _prepare_state(spec, iid, session_id, token):
    """Everything that must exist (or must NOT survive) before a session starts."""
    state = os.path.join(spec.run_root, "state")
    for sub in ("activity", "panes", "started", "blocked", "exited", "awaiting", "sessions",
                "authfail", "envfail", "mail", "status", "launch_stderr"):
        os.makedirs(os.path.join(state, sub), exist_ok=True)
    os.makedirs(os.path.join(spec.run_root, "reports"), exist_ok=True)

    # Record the id NOW, before the session exists. Deliberately UNLIKE the activity stamp below,
    # which is withheld until delivery is verified: an id is an IDENTITY claim, not a liveness one
    # — nothing reads this file as proof anything is running — and a host that dies inside the
    # verify window must still leave behind the handle to resume by.
    if session_id:
        _write_atomic(os.path.join(state, "sessions", iid), session_id)

    # Restart hygiene: clear ONLY this id's run-state markers, so a prior session's
    # report/exited/blocked cannot mis-fire for the fresh one. Intentionally explicit rather than a
    # glob — a wrong one would discard a real report. Delivery RECEIPTS (mail/<id>.consumed.* etc.)
    # are deliberately NOT cleared: they are the record of what was actually handed over, and
    # history survives a restart. The worktree and any committed work are never touched here.
    for path in (os.path.join(spec.run_root, "reports", "%s.md" % iid),
                 os.path.join(state, "blocked", iid),
                 os.path.join(state, "exited", iid),
                 os.path.join(state, "awaiting", iid),
                 os.path.join(state, "mail", iid),
                 os.path.join(state, "status", "%s.json" % iid)):
        _rm_quiet(path)

    # Sweep this id's ABANDONED self-refusal markers. Nothing else prunes them: the launcher only
    # removes the marker it actually OBSERVED. AGE-BOUNDED on purpose — an id-wide delete would
    # take an OVERLAPPING launch's live marker with it, and that launch would then report a
    # delivery fault for an auth one.
    cutoff = time.time() - _MARKER_STALE_MINUTES * 60
    for sub in ("authfail", "envfail", "started"):
        directory = os.path.join(state, sub)
        for entry in os.listdir(directory) if os.path.isdir(directory) else []:
            if not entry.startswith("%s." % iid):
                continue
            path = os.path.join(directory, entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    _rm_quiet(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- delivery

def _await_start(spec, iid, token, edges):
    """Did start-session.sh run in this pane, for THIS launch? The sentinel's name carries the
    launch token, so a delayed or orphaned command from a prior attempt cannot false-verify."""
    sentinel = os.path.join(spec.run_root, "state", "started", "%s.%s" % (iid, token))
    deadline = max(1, int(spec.verify_seconds))
    waited = 0.0
    while True:
        if os.path.exists(sentinel):
            _rm_quiet(sentinel)          # the per-launch proof has served its purpose
            return True
        if _refusal_marker(spec, iid, token) is not None:
            return False                 # the session refused itself; no sentinel is coming
        if waited >= deadline:
            return False
        edges.sleep(_VERIFY_PAUSE)
        waited += _VERIFY_PAUSE


def _refusal_marker(spec, iid, token):
    """(kind, why) for a session that refused ITSELF, or None.

    The environment is read BEFORE auth on purpose: a poisoned environment is causally UPSTREAM of
    the auth death it can produce (an inherited XDG_CONFIG_HOME is exactly how `gh` dies), so if
    both were ever stamped the environment is the honest reading — and "re-login" would be a
    confidently wrong remedy for it.
    """
    for kind in ("envfail", "authfail"):
        path = os.path.join(spec.run_root, "state", kind, "%s.%s" % (iid, token))
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return kind, f.read().strip()
            except OSError:
                return kind, ""
    return None


def _self_refusal(spec, iid, token):
    """The Result for a session that refused itself, or None. Speaks the session's OWN diagnosis
    so the memo names the fault instead of the launch machinery."""
    marker = _refusal_marker(spec, iid, token)
    if marker is None:
        return None
    kind, why = marker
    _rm_quiet(os.path.join(spec.run_root, "state", kind, "%s.%s" % (iid, token)))
    if kind == "envfail":
        return Result(ENV_POISONED, "\n".join([
            "[%s] ENV POISONED in the session's own environment — the flight was refused before "
            "it started." % iid,
            "[%s] %s" % (iid, why or "the session reported no detail"),
            "[%s] Nothing is running; this is an environment fault, not a launch-delivery one."
            % iid]))
    return Result(AUTH_DEAD, "\n".join([
        "[%s] GH AUTH DEAD in the session's own environment — the flight was refused before it "
        "started." % iid,
        "[%s] %s" % (iid, why or "the session reported no detail"),
        "[%s] Nothing is running; this is an auth fault, not a launch-delivery one." % iid]))


def _teardown(host, session):
    """End a pane that never became a worker. Only ever reached with NO start sentinel — so
    nothing of value is in it — and it goes through the doorway's own verified kill rather than
    any signal of ours."""
    try:
        host.kill(session)
    except session_host.HostError:
        pass                             # the raise above is the report; a failed teardown must
                                         # not replace the diagnosis with its own error


def _record_delivery(spec, iid, session, debugger, resume):
    """Only NOW is it honest to record liveness. Writing the activity stamp before delivery was
    confirmed is exactly what fabricated 'launched & alive' for 45 minutes while no worker had
    started."""
    state = os.path.join(spec.run_root, "state")
    _write_atomic(os.path.join(state, "activity", iid), str(int(time.time())))
    _write_atomic(os.path.join(state, "panes", iid), session.pane or "")
    _write_atomic(os.path.join(state, "panes", "%s.ws" % iid), session.workspace or "")
    if debugger or resume:
        # A debugger is not a tracked issue, so it has no counter; and a REVIVE is deliberately
        # not counted (#298) — `retries` is mechanical telemetry about how many times a lane had
        # to be STARTED OVER, and re-entering the same conversation is the opposite of that.
        return
    def bump(st):
        issue = st["issues"].setdefault(iid, loopstate.new_issue())
        issue["launches"] = issue.get("launches", 0) + 1
        issue["retries"] = max(issue["launches"] - 1, 0)
    try:
        loopstate.update(os.path.join(state, "issues.json"), bump)
    except (OSError, ValueError, KeyError):
        pass                             # telemetry must never fail a delivered launch


def _write_atomic(path, text):
    """tmp + mv, like every other durable state write in this stack, so a reader can never see a
    half-written value."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        _rm_quiet(tmp)


def _rm_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = ["Spec", "Result", "Ran", "Edges", "WorktreeLock", "launch", "pane_environment",
           "is_poison", "WORKER_RE", "DEBUGGER_RE", "OK", "ABORTED", "NOT_DELIVERED",
           "BASE_MISSING", "AUTH_DEAD", "AUTH_DEAD_RUNNER", "ENV_POISONED", "UNSUPPORTED_AGENT"]
