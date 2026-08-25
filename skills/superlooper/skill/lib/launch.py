"""The ONE spawn path (issue #308) — pre-flight, then the five-verb wrapper's ``spawn``.

There were THREE spawners, and the plan's §9 named the landmine plainly: rewire only the runner
and the watchdog + dashboard Fixer keep calling a launcher that no longer exists — i.e. no
unattended repair at exactly the moment repair is needed. So every caller that creates a session
comes through here, and this module is the only thing left that does:

===================  ==========================  ===========================================
caller               spawns                      trigger
===================  ==========================  ===========================================
runner               worker ``i<N>``             an approved issue (launch/recover/regenerate)
runner               triage flight ``t<N>``      at most once a day, and only when some open
                                                 issue's body changed since the last verdicts
                                                 (``lib/triage.py``, issue #448)
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
7    THIS session holds the wrong Anthropic account (or none, or an API key)
8    the RUNNER's environment holds the wrong Anthropic account — a channel fault
9    this machine's fleet is fenced and the host's control socket is not — a channel fault
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

import identity
import journal
import loopstate
import panes
import sanitize
import session_host
import triage

# The THREE mode guards, the first two spelled exactly as the cmux launcher spelled them. They are
# what stop one session class from being launched as another — fail closed on WRONG-TYPED input,
# not merely on unsafe input. Without the worker guard a caller bug routes a d<N> through worktree
# creation and the issue counter; without the debugger guard an i<N> silently skips worktree
# creation and runs in whatever directory the caller happened to pass; and since #448 a third id
# shape exists, so both of those crossings now have a second way to go wrong.
WORKER_RE = re.compile(r"^i[0-9]+$")
DEBUGGER_RE = re.compile(r"^d[0-9]+$")
TRIAGE_RE = re.compile(r"^t[0-9]+$")

# The session classes, as a caller DECLARES one (``Spec.mode``). Spelled out rather than inferred,
# because a third class is exactly where "``cwd`` selects the mode and nothing else does" stops
# being enough: a triage flight in its default home takes no ``cwd`` and creates no worktree, so
# on the old rule it is indistinguishable from a worker launch — and would be run as one.
WORKER = "worker"
DEBUGGER = "debugger"
TRIAGE = "triage"
MODES = (WORKER, DEBUGGER, TRIAGE)

_MODE_GUARDS = {WORKER: WORKER_RE, DEBUGGER: DEBUGGER_RE, TRIAGE: TRIAGE_RE}
# One refusal sentence per mode. The first two are VERBATIM — ``evidence.py`` classifies launcher
# stderr on phrases, and tests/test_launch_delivery drives them through the real CLI.
_MODE_REFUSALS = {
    WORKER: "[%s] worker mode expects an issue id (i<N>) — refusing",
    DEBUGGER: "[%s] --cwd mode is for debugger (d<N>) ids only — refusing",
    TRIAGE: "[%s] triage mode expects a triage id (t<N>) — refusing",
}

OK = 0
ABORTED = 1
NOT_DELIVERED = 2
BASE_MISSING = 3
AUTH_DEAD = 4
AUTH_DEAD_RUNNER = 5
ENV_POISONED = 6
# The Anthropic half of the same pair (#314). Split from 4/5 for exactly the reason those two are
# split from each other: the remedies are different sentences to different people (`gh auth login`
# versus "this session is on the wrong subscription"), and a memo that names the wrong one sends
# the owner to repair something that is not broken.
CLAUDE_IDENTITY = 7
CLAUDE_IDENTITY_RUNNER = 8
# The fence pre-flight (#326). ONE code for both fatal verdicts (OPEN and UNREACHABLE) rather than
# the split rc 4/5 and 7/8 use, because those splits exist where the runner BRANCHES — session-side
# faults park one issue, runner-side ones hold the queue. Both fence verdicts are machine-level and
# both hold, so a second code would divide the evidence table on something nothing reads. What
# differs between them is one sentence of remedy, and the stderr carries that verbatim into the memo.
FENCE_DOWN = 9
UNSUPPORTED_AGENT = 64

AGENTS = ("claude", "codex")

# The launch-floor denylist (#301), named HERE only to keep the launcher from being the one that
# hands the poison over. This is NOT the floor — the floor is the post-scrub assert that runs in
# the session's own environment, because only code running there can prove anything about it.
# Whole families, never a fixed list: a new CLAUDE_CODE_* or ANTHROPIC_* shipped by a future
# release is poison the day it appears, and a list would learn about it from a bill.
_POISON_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_POISON_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "XDG_CONFIG_HOME")

# The identity contract's two agent-owned variables (#314). NOT poison — the fleet ASSIGNS the
# config dir, deliberately — but never forwarded either: an inherited one is a credential namespace
# nobody chose, and the whole contract is that identity is assigned rather than picked up. The
# session's own floor is what refuses them; this only keeps the launcher from being the source.
_IDENTITY_VARS = (identity.CONFIG_DIR_VAR, identity.REDIRECT_VAR)

# Defense in depth beside the fence (#305). The wrapper strips these too, and the host injects its
# own pane-identity vars whatever anyone passes — which is precisely why the token, and not this,
# is the fence. What this rules out is the launcher becoming a second, quieter grant path.
_HOST_ENV_PREFIX = "HERDR"

_GH_PROBE_SECONDS = 10          # a black-holed network must not eat the caller's whole timeout
# The fence pre-flight's own bound (#326), named for the same reason the gh one is: a wedged or
# hostile peer on the control socket must not be able to grow a launch into a hang. `fence_probe`
# turns this into a bound on the whole exchange, not merely on one recv.
_FENCE_SECONDS = 5
_START_TIMEOUT_MS = 45000       # the pane's floor (a bounded gh read) runs before the agent does

_VERIFY_SECONDS = 30            # the floor stamps its sentinel BEFORE the agent starts, so this
_VERIFY_PAUSE = 0.25            # window is nearly always already satisfied when spawn returns
_MARKER_STALE_MINUTES = 5       # only genuinely abandoned self-refusal markers are swept
_REFUSAL_GRACE = 3.0            # a pane's own refusal can land just after the host gives up

# A GitHub login is [A-Za-z0-9-] and nothing else. `gh` merges its diagnostics into the capture and
# an error path prints MULTI-LINE text, so the test is a whole-string match: a line-anchored one
# would happily take a bare word from line 2 of an error message as "the expected login".
_LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")


class _Refused(Exception):
    """A refusal raised from below the top-level flow, carrying its own operator-facing sentence.

    Only for the pre-flight steps that must abort a launch from inside a helper: ``launch``'s
    catch-all renders these verbatim rather than as "the launcher failed unexpectedly", so a
    deliberate refusal never reads to the owner as a crash.
    """


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

    ``mode`` selects the session class, and the id must then MATCH it — deriving the class from
    the id instead would make a caller's mistake silent, and the guards exist to make it loud:

    * ``worker`` (``i<N>``)   — a worktree off ``origin/<dev_branch>``, on the issue's branch.
    * ``debugger`` (``d<N>``) — in place, in ``cwd``; no worktree and no branch. THE class that
      receives the control-socket token, which is why nothing else may reach it by accident.
    * ``triage`` (``t<N>``)   — the flight (#448). ``triage_home`` says where: ``checkout`` (the
      ruled default) runs it in ``repo`` itself, creating nothing; ``worktree`` gives it a
      DETACHED checkout of the base, for a repo whose gitignored overlay is sensitive. ``cwd`` is
      refused here — a triage flight's home comes from the repo's config, and a caller offering a
      directory has confused this class with the one that holds the token.

    An EMPTY ``mode`` keeps the original rule for the two original classes — ``cwd`` present means
    debugger, absent means worker — so every pre-#448 call site is unchanged. A triage flight must
    say so explicitly: its default home takes no ``cwd``, so silence would read as "worker".
    """
    id: str
    run_root: str
    repo: str = ""
    dev_branch: str = "main"
    cwd: str = None
    mode: str = ""                       # "" -> derive from `cwd` (worker / debugger), as before
    triage_home: str = triage.CHECKOUT   # triage only: `checkout` (default) or `worktree`
    engine_bin: str = ""
    agent: str = "claude"
    model: str = ""
    effort: str = ""
    attended: bool = False
    resume_session_id: str = ""
    expect_gh_login: str = None          # None -> resolve it here, in the runner's own environment
    gh_bin: str = ""
    claude_bin: str = ""                 # #303: named ONLY when this launcher actually has a pin
    # #314. None -> read the machine's own assignment (SL_FLEET_CLAUDE_CONFIG_DIR); "" -> none.
    # A canonical absolute string is derived from it ONCE, here, and every spawn path is handed
    # that same string byte for byte — the credential namespace is a hash of it as written.
    claude_config_dir: str = None
    expect_claude_account: str = ""      # an operator PIN (an orgId); else read from the dir above
    session_name: str = ""
    label: str = ""
    verify_seconds: int = _VERIFY_SECONDS
    start_timeout_ms: int = _START_TIMEOUT_MS
    codex: dict = field(default_factory=dict)
    forwarded_env: dict = None           # what the caller's own environment offers (scrubbed here)
    probe_env: dict = None               # the environment the identity read runs in (None = ours)
    # There is deliberately NO field here for the fence pre-flight (#326) — not for the switch and
    # not for the socket. It reads `os.environ` and nothing else, because `os.environ` is exactly
    # what the `herdr` child inherits: the doorway's `_call` runs the CLI without an explicit env,
    # so a caller-supplied environment could make the pre-flight probe one socket while the spawn
    # drove another, and a FENCED verdict about a socket nothing launches onto is worse than no
    # verdict at all. An earlier draft of this had such a field and the cross-review caught it. The
    # same reasoning as `session_host.receives_token`, which takes no grant parameter: a flag that
    # can be passed is a flag that will one day be passed by the wrong call site.


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

    def fence(self, socket_path, timeout=_FENCE_SECONDS):
        """What a TOKENLESS caller finds at the control socket (#326) — FENCED / OPEN / UNREACHABLE.

        An edge like the others: it opens a socket, so it is injected rather than called inline, and
        no test reaches a real one. The question itself belongs to the doorway and is asked there —
        this only decides when it gets asked.
        """
        return session_host.fence_probe(socket_path, timeout=timeout)

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


# --------------------------------------------------------------------------- the mode

def _mode_of(spec):
    """Which session class this Spec declares, or None for one this launcher cannot read.

    The EXACT empty string (or no ``mode`` attribute at all, for a Spec built before #448) derives
    the ORIGINAL rule — ``cwd`` present means debugger, absent means worker — so every pre-#448
    call site keeps working unchanged.

    Everything else is None, INCLUDING a wrong-typed one: ``None``, ``False``, ``0``, ``[]`` and a
    blank-but-not-empty ``" "`` are all a caller that meant SOMETHING, and coercing them to
    "said nothing" would route them to the worker path — the silent fail-open on wrong-typed
    input that these guards exist to make loud (fresh-agent review, P1). Fail closed on
    wrong-typed input, not merely on unsafe input.
    """
    declared = getattr(spec, "mode", "")
    # TYPE first, and `type(...) is str` rather than isinstance: `==` and `in` compare by VALUE, so
    # an object with a co-operative ``__eq__``/``__hash__`` would answer yes to both and walk into
    # the guard table, while one whose ``__eq__`` raises would take the launcher down instead of
    # refusing (fresh-agent review round 2). Same coercion trap `issues.dep_met` documents for
    # `blocked_by=[True]`: not-raising is only half of "counts as unreadable".
    if type(declared) is not str:
        return None
    if declared == "":
        return DEBUGGER if spec.cwd is not None else WORKER
    return declared if declared in MODES else None


# --------------------------------------------------------------------------- the launch

def launch(spec, host=None, edges=None):
    """Pre-flight, then spawn, then PROVE a worker started. Never raises."""
    edges = edges if edges is not None else Edges()
    try:
        return _launch(spec, host, edges)
    except _Refused as e:
        return Result(ABORTED, str(e))
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
    mode = _mode_of(spec)
    if mode is None:
        return Result(ABORTED, "[%s] unknown session mode %r (expected: %s, or \"\" to derive it "
                               "from --cwd) — refusing"
                      % (iid, getattr(spec, "mode", ""), ", ".join(MODES)))
    # ``--cwd`` belongs to the debugger and to nothing else. Checked BEFORE the id guard so a
    # triage spec carrying one is refused with the debugger's own sentence — the confusion it
    # names is real, and the class it was confused with is the one holding the fence's token.
    if mode == TRIAGE and spec.cwd is not None:
        return Result(ABORTED, _MODE_REFUSALS[DEBUGGER] % iid)
    if not _MODE_GUARDS[mode].match(iid):
        return Result(ABORTED, _MODE_REFUSALS[mode] % iid)
    debugger = mode == DEBUGGER
    flight = mode == TRIAGE
    if spec.agent not in AGENTS:
        return Result(UNSUPPORTED_AGENT,
                      "[%s] unsupported agent '%s' (expected: %s)"
                      % (iid, spec.agent, " or ".join(AGENTS)))

    # ---- the fence (issue #326) ------------------------------------------------------------
    # FIRST of the machine-level asserts, and cheapest: one unix-socket exchange, no subprocess.
    # Ordered ahead of the two identity reads because it is the refusal that says this machine may
    # not run an unattended worker AT ALL — spending a `gh api user` and a `claude auth status`
    # first would be resolving whose account a launch runs under when the launch was never going to
    # be permitted. Like them, it is ordered before the worktree and before any host RPC so a
    # refusal costs no orphan pane and no leftover checkout (the base-missing discipline, #28).
    if not debugger:                     # every TOKENLESS class: the worker and the flight
        refused = _fence_preflight(spec, iid, edges)
        if refused is not None:
            return refused

    # ---- the expected gh login (issue #299) -----------------------------------------------
    # Read ONCE here, in the runner's own environment, and handed down: the session's own assert
    # needs something to assert AGAINST, and the honest expectation is the loop's OWN identity.
    # FAIL CLOSED and fail EARLY — before the worktree and before any host RPC, so a refusal costs
    # no orphan pane and no leftover checkout (the base-missing discipline, #28).
    expect_login = spec.expect_gh_login
    if not expect_login:
        probe = edges.run([spec.gh_bin or "gh", "api", "user", "--jq", ".login"],
                          timeout=_GH_PROBE_SECONDS, env=identity_probe_env(spec.probe_env))
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

    # ---- the identity env contract (issue #314) --------------------------------------------
    # ONE derivation, here, for every spawn path — because the credential namespace `claude` uses
    # is `sha256` of the CLAUDE_CONFIG_DIR string AS WRITTEN, so two spellings of one directory are
    # two identities and the wrong one presents as a LOGGED-OUT session rather than as an error
    # (#300 landmine 1). Deriving it per caller is how that drift would arrive.
    #
    # Ordered beside the gh read and before the worktree for the same reason: a refusal here must
    # cost no orphan pane and no leftover checkout.
    probe_base = spec.probe_env if spec.probe_env is not None else os.environ
    config_dir, problem = identity.resolve_config_dir(spec.claude_config_dir, probe_base)
    if problem:
        return Result(CLAUDE_IDENTITY_RUNNER, "\n".join([
            "[%s] CLAUDE IDENTITY (runner env): this machine's fleet config dir cannot be used — "
            "%s" % (iid, problem),
            "[%s] Set %s to one canonical absolute path (suggested: %s), or unset it to run "
            "workers on the machine's default Claude login. NOT launching; no session was created."
            % (iid, identity.FLEET_DIR_VAR, identity.SUGGESTED_FLEET_DIR)]))
    # The expected ACCOUNT, resolved once here and handed down, exactly as the gh login is: the
    # session's own assert needs something to assert against, and a second-hand answer proves
    # nothing about this launch.
    #
    # ONLY when this machine has actually assigned an identity (a config dir, or an operator's
    # pinned orgId). With neither, "the intended account" is by definition whatever login the
    # machine's default config dir holds — the launcher and the session would be reading the same
    # namespace by construction, so the comparison would cost every launch a subprocess to
    # discover something it already knew. The session's own assert stays POSITIVE either way: it
    # still requires logged-in, on a subscription, never on an API key.
    expect_account = str(spec.expect_claude_account or "").strip()
    if config_dir or expect_account:
        claude_bin, why = spec.claude_bin, None
        if not claude_bin:
            claude_bin, why, _deferrable = identity.resolve_claude(probe_base)
        if not claude_bin:
            # NAMED, not folded into "the account could not be read": the fault is the binary pin
            # (#303), the remedy is an install, and a memo that said "log in again" would send the
            # owner to repair a credential that is fine. Unlike the session-side assert this one
            # cannot defer to the pin's own refusal — that refusal happens in a pane, and this
            # launch has not created one.
            return Result(CLAUDE_IDENTITY_RUNNER, "\n".join([
                "[%s] CLAUDE IDENTITY (runner env): which Anthropic account a session would hold "
                "cannot be established because %s" % (iid, why),
                "[%s] Install Claude Code's standalone native build (`claude install stable`) or "
                "point SL_CLAUDE at a real binary. NOT launching; no session was created." % iid]))
        status = identity.read_status(
            edges, claude_bin, config_dir=config_dir,
            env=identity_probe_env(spec.probe_env, config_dir=config_dir))
        problem = identity.account_problem(status, expect_account or None)
        if problem:
            # A CHANNEL fault (rc=8): every launch on this machine will read the same account, so
            # charging one issue a park for it would walk the whole approved queue into parks over
            # a single machine-level fault — the 2026-07-09 storm's shape with a new cause.
            return Result(CLAUDE_IDENTITY_RUNNER, "\n".join([
                "[%s] CLAUDE IDENTITY (runner env): %s" % (iid, problem),
                "[%s] Repair it in a supervised window (`claude` under %s), then re-approve. NOT "
                "launching; no session was created."
                % (iid, config_dir or "the machine's default config dir")]))
        expect_account = status.get("orgId")

    # ---- the brief, BEFORE anything is created ---------------------------------------------
    # Ordered here for the same reason the identity read is: a refusal must cost no leftover
    # checkout, no new branch and no trust entry. (The cmux launcher checked it here too; moving it
    # after worktree creation would leave all three behind on a brief the runner failed to write.)
    #
    # A revive opens on its OWN brief and leaves the lane's original untouched beside it: the
    # crash-recovery relaunch re-runs this without rebuilding the brief, so a preamble written over
    # briefs/<id>.md would later be delivered verbatim to a brand-new, empty session. FAIL CLOSED,
    # never fall back — substituting the original would deliver the whole issue brief as a NEW
    # instruction into a conversation that already built it.
    resume = bool(spec.resume_session_id)
    brief = os.path.join(spec.run_root, "briefs",
                         "%s.resume.md" % iid if resume else "%s.md" % iid)
    if not os.path.isfile(brief):
        return Result(ABORTED, "[%s] missing brief %s" % (iid, brief))

    # ---- where the session lives ----------------------------------------------------------
    branch = ""
    if debugger:
        if not os.path.isdir(spec.cwd):
            return Result(ABORTED, "[%s] --cwd dir does not exist: %s" % (iid, spec.cwd))
        # ABSOLUTE and PHYSICAL: the pane's shell starts in its own default dir, so a relative one
        # would name nothing there.
        worktree = os.path.realpath(spec.cwd)
    elif flight:
        # The triage flight's home (#448). The two answers see DIFFERENT repositories — only the
        # checkout shows the gitignored working files an orchestrator sees — so an unreadable
        # value is REFUSED rather than defaulted: guessing here is a wrong answer either way, and
        # the loader is where a typo already fails loudly at adopt time.
        # `getattr` for the ABSENT case and a strict check on everything else — never
        # `spec.triage_home or CHECKOUT`, which coerced None/False/0/[] into "checkout" and so
        # fail-OPENED a caller bug straight into the owner's REAL working tree (fresh-agent review
        # round 2, P0). Silence takes the ruled default; a value a caller WROTE must be readable.
        home_kind = getattr(spec, "triage_home", triage.CHECKOUT)
        if type(home_kind) is not str or home_kind not in triage.HOMES:
            return Result(ABORTED, "[%s] unknown triage home %r (expected: %s) — refusing"
                          % (iid, home_kind, " or ".join(triage.HOMES)))
        if not spec.repo:
            # Named rather than left to git, for the reason the worker refusal below spells out:
            # without it the base probe fails and the launch would blame a dev_branch that is not
            # what went wrong.
            return Result(ABORTED, "[%s] no target repo was given for a triage launch (SL_REPO) — "
                                   "not launching" % iid)
        if home_kind == triage.CHECKOUT:
            if not os.path.isdir(spec.repo):
                return Result(ABORTED, "[%s] the repo checkout does not exist: %s"
                              % (iid, spec.repo))
            # Nothing is created: the flight runs in the checkout an orchestrator would open. Its
            # read-only discipline there (fetch first, judge against origin/main, write only to
            # GitHub and the state home) is the BRIEF's to enforce — no code here writes to it.
            worktree = os.path.realpath(spec.repo)
        else:
            try:
                base = "origin/%s" % sanitize.branch(spec.dev_branch)
            except (ValueError, TypeError):
                return Result(ABORTED, "[%s] dev_branch sanitize validation failed — not launching"
                              % iid)
            worktree = os.path.join(spec.run_root, "worktrees", iid)
            failed = _make_worktree(spec, edges, iid, worktree, "", base)
            if failed is not None:
                return failed
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
    #
    # ITS RC IS THE GATE, not decoration. The cmux launcher ran under `set -e`, so a pretrust that
    # failed aborted the launch before any tab existed; ignoring it here would fail OPEN into the
    # exact stall this step prevents — the pane opens, the agent blocks on the trust dialog, no
    # sentinel is ever stamped, and the launch reads as rc=2, a CHANNEL fault that holds the whole
    # queue and blames the launch shim. pretrust.sh exits nonzero on a missing `jq`, an
    # unsupported agent and an unwritable trust store, all of which are real and none of which the
    # launch machinery caused.
    #
    # AND IT IS AIMED, not merely run (issue #345). Trust is keyed per config dir, so the record has
    # to land in the file the session this launch is about to start will actually read — the SAME
    # canonical string derived above and named in the pane below, handed over byte for byte. #311
    # measured the alternative: the entry in the operator's default config while the worker read a
    # per-worker one, which is a pre-trust that exists and does nothing, on every launch, because
    # every issue gets a fresh worktree.
    #
    # NAMED even when it is empty, rather than left off: this is a child process and it inherits the
    # runner's own shell, so an unnamed assignment would let a stray CLAUDE_CONFIG_DIR there aim the
    # write at a dir this launch is not using — the same fault one directory over. Empty is a
    # statement ("this machine assigns none"), which is why pretrust.sh distinguishes it from absent.
    trusted = edges.run([os.path.join(spec.engine_bin, "pretrust.sh"), worktree, config_dir or ""],
                        timeout=60)
    if trusted.rc != 0:
        # rc ONLY — no third-party text, for the reason the worktree refusal below spells out:
        # evidence.py classifies this line, and its CHANNEL needles ("no answer within", which is
        # exactly how a pretrust that HANGS renders here) are matched before anything else. A
        # stalled local pretrust must not be reported as "GitHub is not answering; the queue will
        # resume on its own", which is a remedy for a fault that is not happening.
        return Result(ABORTED,
                      "[%s] could not pre-trust %s (pretrust.sh rc=%s) — refusing to launch into a "
                      "folder whose first-run trust dialog would block the session with nobody "
                      "there to answer it" % (iid, worktree, trusted.rc))

    # ---- session identity (issue #298) ----------------------------------------------------
    # Identity handed down, never self-asserted. A RELAUNCH always mints anew (`--session-id` on
    # an already-used id is an error); a REVIVE passes the recorded id straight through, because
    # minting on a revive would strand the very transcript the operator asked to re-enter.
    # CLAUDE ONLY: `--session-id`/`--resume` are Claude Code's spelling, and minting for a codex
    # repo would write a UUID into lane state that names no conversation anywhere.
    session_id = ""
    if spec.agent == "claude":
        session_id = spec.resume_session_id or str(uuid.uuid4())

    # ---- run-state hygiene ----------------------------------------------------------------
    token = edges.token()
    _prepare_state(spec, iid, session_id, token)

    # ---- the doorway ----------------------------------------------------------------------
    host = host if host is not None else session_host.SessionHost()
    name = spec.session_name or ("superlooper %s%s" % (iid, " (%s)" % branch if branch else ""))
    pane_env = pane_environment(spec, iid, session_id, resume, expect_login, token,
                                session_name=name, config_dir=config_dir,
                                expect_account=expect_account)
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
        refusal = _self_refusal(spec, iid, token, edges=edges, seconds=_REFUSAL_GRACE)
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

    _record_delivery(spec, iid, session, debugger or flight, resume)
    return Result(OK, "", "[launch] %s  branch=%s pane=%s ws=%s name='%s' (delivery verified)"
                  % (iid, branch or "<none>", session.pane, session.workspace, name),
                  session=session)


# --------------------------------------------------------------------------- the fence pre-flight

# What the journal calls this check, and what a report reads back.
FENCE_ACT = "fence_preflight"
# The verdict when there was no socket to ask about. Deliberately NOT spelled `unreachable`: that
# word means "we asked and got nothing", and this means "we never had an address to ask". Reporting
# the second as the first would send an operator to restart a server that may be running fine.
FENCE_UNRESOLVED = "unresolved"


def _fence_preflight(spec, iid, edges):
    """Refuse a worker launch onto an unfenced control socket, or return None (#326).

    #305 shipped the fence and ``fence_probe``; nothing called the probe on a launch path, so the
    only thing establishing that a fleet was actually fenced was a human running the acceptance
    check by hand. The carried patch is deliberately INERT when no token is configured — that is
    what lets upstream's own test suite pass unmodified — so a stock, unpatched or misconfigured
    host serves any tokenless worker. And that host does not look broken from the runner's seat,
    because it answers. This is the check that turns "the fence is up" from an assumption a machine
    inherited at build time into an observation made on every launch.

    **EVERY TOKENLESS launch.** The MODE decides and nothing else does, exactly as
    ``session_host.receives_token`` lets the NAME decide who gets the token:

    * an ``i<N>`` is the thing the fence exists to contain. It is handed no token, so on a fenced
      socket it is refused and on an open one it can drive every pane on the machine.
    * a ``t<N>`` is the same exposure one session class over (#448): the standing rule's own words
      are that a triage flight "holds no fence token and drives no herdr surface", so it is gated
      exactly as a worker is. The d<N> exemption below is about the token a repair session
      RECEIVES, and a flight receives none — reading the exemption as "not a worker" would have
      handed the whole fleet to the one class whose job is to act on the queue unattended.
    * a ``d<N>`` RECEIVES the token by design, so an open socket grants a repair session nothing it
      does not already hold — and refusing repair BECAUSE the fence is down would mean no
      unattended repair at exactly the moment repair is needed, which is the landmine the whole
      spawn port was built around.

    There is deliberately no ``attended`` bypass, and that omission is load-bearing rather than an
    oversight: ``SL_ATTENDED`` is read from the environment, so an ambient ``export SL_ATTENDED=1``
    in the shell or LaunchAgent that started the runner would silently disarm the fence for every
    worker on the machine. The runner already pins it empty for that exact reason; a gate that
    honoured it would be trusting the variable that pin exists because nobody can trust.

    **UNREACHABLE refuses** — the ruled behaviour, decided rather than defaulted. Three reasons,
    and any one of them is sufficient. Silence is not a fence: ``fence_probe`` refuses to call it
    one (c2) and neither may this. The spawn is about to be driven through that same socket, so a
    socket that is genuinely down fails the launch one step later anyway — refusing costs a healthy
    machine nothing. And a pre-flight that PROCEEDED on UNREACHABLE would be silently disarmed by
    anything that breaks the probe, which is a fail-OPEN on the one check whose whole job is to
    fail closed.

    The verdict is journaled on EVERY worker launch, permitted ones included. That line is what
    keeps a default-off switch from being a silent no-op: a machine that was quietly never armed
    writes down its OPEN socket every launch, so the morning report can show the fence state over
    time instead of leaving "this fleet has been unfenced all week" indistinguishable from "this
    fleet has been fenced all week". The journal is a RECORD of the decision and never an input to
    it — an unwritable one cannot refuse a launch, and far more importantly cannot permit one.
    """
    required, unrecognised = session_host.fence_required(os.environ)
    # `os.environ`, with no override anywhere in the signature — see the note on `Spec`. The doorway
    # runs the host CLI with no explicit environment, so the child inherits exactly this; resolving
    # the socket from it through the doorway's OWN resolver is what makes "the socket probed" and
    # "the socket driven" the same address by construction, rather than by two copies of a
    # resolution rule that can drift apart or be pointed at different places by a caller.
    socket_path = session_host.control_socket_path(os.environ)
    verdict = edges.fence(socket_path) if socket_path else FENCE_UNRESOLVED

    refusal = None
    if required and verdict != session_host.FENCED:
        refusal = Result(FENCE_DOWN, "\n".join(
            [_fence_memo(iid, verdict, socket_path)]
            + ([("[%s] (%s is set to %r, which this engine does not recognise — it reads as "
                 "'required', because a switch typo that read as 'off' would be a silently "
                 "disarmed fence.)" % (iid, session_host.FENCE_REQUIRED_VAR, unrecognised))]
               if unrecognised else [])
            + ["[%s] NOT launching; no session was created." % iid]))

    record = {"act": FENCE_ACT, "id": iid, "verdict": verdict, "required": required,
              "socket": socket_path, "refused": refusal is not None}
    if unrecognised:
        record["switch"] = unrecognised
    try:
        journal.append(spec.run_root, record)
    except (OSError, ValueError):
        # Telemetry may never fail a launch — and, far more important, may never pass one. The
        # refusal above is already built; this only writes it down.
        pass
    return refusal


def _fence_memo(iid, verdict, socket_path):
    """The operator-facing first line, per verdict. Each names a DIFFERENT repair.

    ``evidence.py`` classifies on this text before it reads the rc, so two rules bind it. It must
    lead with the loop's own "FENCE DOWN" phrase, which no third-party tool can emit. And it must
    contain none of the needles that map to an earlier reason — in particular none of the phrases a
    socket failure invites (``could not connect``, ``connection refused``, ``no answer within``),
    every one of which belongs to the gh-transient or cmux-anchor needle further down the table. An
    unfenced fleet reported as "wait for GitHub to come back" would be a remedy for a fault that
    never self-recovers, offered while the socket stays wide open.
    """
    if verdict == session_host.OPEN:
        return "\n".join([
            "[%s] FENCE DOWN: a tokenless connection to %s was SERVED — the session host's control "
            "socket has no fence at all. Its path is injected into every pane whatever a launcher "
            "passes, and the protocol is plain newline-JSON, so a worker started here could drive "
            "the whole fleet with ten lines of python and no host binary." % (iid, socket_path),
            "[%s] This machine declares its fleet fenced (%s). Rebuild the patched host with "
            "vendor/herdr/build.sh and re-run `superlooper fleet --install`, or write %s=off into "
            "the fleet prefix's `environment` file if this machine is deliberately unfenced — on a "
            "machine the build-up armed (#355) that FILE is the declaration and an exported "
            "variable no longer wins."
            % (iid, session_host.FENCE_REQUIRED_VAR, session_host.FENCE_REQUIRED_VAR)])
    if verdict == FENCE_UNRESOLVED:
        return "\n".join([
            "[%s] FENCE DOWN: this machine declares its fleet fenced, but where the host keeps its "
            "control socket could not be resolved from this launcher's own environment — so the "
            "fence was never measured. A guessed path would be a verdict about a machine nothing "
            "looked at." % iid,
            "[%s] Give the launcher's process a HOME (or the explicit socket override the fleet's "
            "own deployment sets), then re-run `superlooper doctor`." % iid])
    return "\n".join([
        "[%s] FENCE DOWN: %s did not answer, so nothing proved this fleet is fenced — and silence "
        "is never read as one here (absence of signal is UNKNOWN, never health). The spawn would "
        "have been driven through that same socket." % (iid, socket_path),
        "[%s] The host's server is not running, or it is bound somewhere else: run `superlooper "
        "fleet --install` and read the server log under the fleet prefix." % iid])


# --------------------------------------------------------------------------- pre-flight pieces

def _make_worktree(spec, edges, iid, worktree, branch, base):
    """Create the lane worktree, or return the Result that refuses this launch.

    PRESERVATION: an existing checkout is REUSED, never removed. A relaunch or regenerate lands in
    the id's own worktree with its unpushed work intact (#190/#168) — the launcher has no path
    that destroys one, which is why c17's remove-force half was rejected.

    An EMPTY ``branch`` is the triage flight's worktree home (#448): a DETACHED checkout of the
    base. A flight never commits, never pushes and must never create a ref, so there is no branch
    to make and no existing one to attach — it goes straight to the base-ref verdict on failure.
    """
    if os.path.isdir(worktree):
        return None
    with WorktreeLock(os.path.join(spec.run_root, "state", "worktree.lock")):
        if os.path.isdir(worktree):          # another launch won the race and made it for us
            return None
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        attach_rc = "n/a"
        if branch:
            fresh = edges.run(["git", "-C", spec.repo, "worktree", "add", "-b", branch, worktree,
                               base], timeout=120)
            if fresh.rc == 0:
                return None
            # The fallback attaches an EXISTING branch — a relaunch or regenerate reuses the name.
            attach = edges.run(["git", "-C", spec.repo, "worktree", "add", worktree, branch],
                               timeout=120)
            attach_rc = attach.rc
            if attach.rc == 0:
                return None
        else:
            fresh = edges.run(["git", "-C", spec.repo, "worktree", "add", "--detach", worktree,
                               base], timeout=120)
            if fresh.rc == 0:
                return None
        exists = edges.run(["git", "-C", spec.repo, "rev-parse", "--verify", "--quiet",
                            "%s^{commit}" % base], timeout=30)
        # rc=1 is git's own "that ref is not here" under `--verify --quiet`. Anything else — a
        # timeout (124), an unrunnable git (127), a broken repo — is a fault this probe could not
        # answer, and reading it as a missing base would tell the owner to fix a dev_branch that
        # was never the problem. Fall through to the generic refusal, which names the rcs.
        if exists.rc == 1:
            # Issue #28's DISTINCT code, so the park memo blames the branch rather than the launch
            # machinery — and a missing base never becomes a hollow launch.
            return Result(BASE_MISSING,
                          "[%s] worktree base '%s' does not exist on '%s' — the configured "
                          "dev_branch is not on origin, so no worktree can be created. Run "
                          "'superlooper doctor' and set dev_branch to the repo's default, then "
                          "re-approve." % (iid, base, spec.repo))
        # Git's OWN words are deliberately NOT appended. evidence.py classifies this line, and its
        # channel needles ("no answer within", "dial tcp", "could not resolve") are matched BEFORE
        # the worktree needle — so third-party text here could turn one issue's local git fault
        # into a held queue. Its own file states the rule: a needle that maps to a channel reason
        # must be impossible in the loop's own launcher text.
        # The base is NOT claimed to exist: on the fall-through path above git did not answer that
        # question at all, and asserting it would be a second confidently-wrong statement in the
        # memo the first one was just removed from.
        return Result(ABORTED,
                      "[%s] could not create the worktree at '%s' for branch '%s' off base '%s' "
                      "(git rc: create=%s attach=%s verify=%s)"
                      % (iid, worktree, branch or "<detached>", base, fresh.rc, attach_rc,
                         exists.rc))


def pane_environment(spec, iid, session_id, resume, expect_login, token,
                     session_name=None, config_dir=None, expect_account=None):
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
        if key.startswith(_HOST_ENV_PREFIX) or is_poison(key) or key in _IDENTITY_VARS:
            continue
        env[key] = str(value)
    env.update({
        "SL_ISSUE_ID": iid,
        "SL_RUN_ROOT": spec.run_root,
        # ONE string for the workspace label, the terminal title and the remote-control label —
        # as the cmux launcher had it. Split, the phone's Remote Control list silently loses the
        # branch, which is the one thing that tells two lanes apart at a glance.
        "SL_SESSION_NAME": session_name or spec.session_name or "superlooper %s" % iid,
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
        # The identity contract (#314), NAMED on every launch INCLUDING the empty case — the same
        # discipline SL_EXPECT_GH_LOGIN follows. "This machine assigns no config dir" is then
        # something the launcher SAID, sitting in the pane environment a test or an operator can
        # read, rather than something the floor inferred from a variable that was not there.
        # `CLAUDE_CONFIG_DIR` itself is deliberately NOT set here: it is the AGENT's own variable,
        # so the pane's floor is what exports it (the agent-boundary rule), one step later and
        # inside the session's own shell.
        identity.ASSIGN_VAR: str(config_dir or ""),
        identity.EXPECT_VAR: str(expect_account or ""),
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


def identity_probe_env(base=None, config_dir=None):
    """The environment the launcher's own identity reads run in — the differences from ours ARE the
    point (issues #299 and #314).

    `XDG_CONFIG_HOME` is where `gh` resolves its config dir from, and the session's floor REMOVES
    it (start-session.sh, issue #301). If this side kept an inherited value while the worker
    dropped it, the two `gh api user` reads would consult DIFFERENT config dirs — and #299 compares
    their answers. Every launch would then refuse with a mismatch that names no real fault: either
    the launcher's probe finds no `hosts.yml` and the whole queue is HELD as a runner-env auth
    death, or it answers as a SECOND account and every issue parks on an assert against an identity
    the worker was never going to have. Dropping it here makes both sides read the same config dir
    by construction, which is exactly what the cmux launcher did and why.

    The `claude` half is the same argument one variable over (#314). The session's floor SCRUBS the
    poison and REFUSES an inherited credential redirect, so a probe that kept either would measure
    a namespace the worker will never use: an inherited ANTHROPIC_API_KEY makes this read answer
    `loggedIn: true` on an API key (measured), and the launch would be refused for a fault that
    exists only on this side of the spawn. The config dir is then set — or removed outright, never
    emptied, because an empty CLAUDE_CONFIG_DIR is its own credential namespace rather than "the
    default one".

    Nothing else is touched: PATH, HOME and the keychain context these reads legitimately need must
    survive (the c25 landmine — overriding HOME breaks macOS keychain OAuth outright).
    """
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if is_poison(name):                 # includes XDG_CONFIG_HOME, the #299 gh half
            env.pop(name, None)
    return identity.probe_env(env, config_dir)


def is_poison(name):
    """Is this variable one the launch floor exists to keep out of a session? (issue #301)"""
    name = str(name)
    return name in _POISON_NAMES or any(name.startswith(p) for p in _POISON_PREFIXES)


def _prepare_state(spec, iid, session_id, token):
    """Everything that must exist (or must NOT survive) before a session starts."""
    state = os.path.join(spec.run_root, "state")
    for sub in ("activity", "panes", "started", "blocked", "exited", "awaiting", "sessions",
                "authfail", "envfail", "identityfail", "mail", "status", "launch_stderr",
                "phase"):                          # (#443) the cross-review script's breadcrumb
        os.makedirs(os.path.join(state, sub), exist_ok=True)
    os.makedirs(os.path.join(spec.run_root, "reports"), exist_ok=True)

    # Record the id NOW, before the session exists. Deliberately UNLIKE the activity stamp below,
    # which is withheld until delivery is verified: an id is an IDENTITY claim, not a liveness one
    # — nothing reads this file as proof anything is running — and a host that dies inside the
    # verify window must still leave behind the handle to resume by.
    if session_id and not _write_atomic(os.path.join(state, "sessions", iid), session_id):
        # The old launcher exited 1 here. Flying anyway produces a session nobody can `resume`,
        # with nothing anywhere saying why — the handle is the whole point of minting the id.
        # Raised as this module's own refusal so it renders as a decision, not as a crash.
        raise _Refused("[%s] could not record the session id under %s — refusing, because this "
                       "session would not be resumable" % (iid, os.path.join(state, "sessions")))

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
                 os.path.join(state, "status", "%s.json" % iid),
                 # (#443) The phase breadcrumb is a claim about what a session is doing RIGHT NOW,
                 # so it must never outlive the session that wrote it. A worker killed between the
                 # cross-review script's start stamp and its end trap leaves an OPEN one, and a
                 # relaunch inside its staleness window would publish the fresh session as
                 # "cross-reviewing" while it is only building. This is the common pre-session
                 # hygiene point (the reapprove/rebuild executors clear it too, on paths that can
                 # abort before ever reaching a launch).
                 os.path.join(state, "phase", iid)):
        _rm_quiet(path)

    # Sweep this id's ABANDONED self-refusal markers. Nothing else prunes them: the launcher only
    # removes the marker it actually OBSERVED. AGE-BOUNDED on purpose — an id-wide delete would
    # take an OVERLAPPING launch's live marker with it, and that launch would then report a
    # delivery fault for an auth one.
    cutoff = time.time() - _MARKER_STALE_MINUTES * 60
    for sub in ("authfail", "envfail", "identityfail", "started"):
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
    confidently wrong remedy for it. Identity is read LAST for the same reason from the other end:
    the floor runs it last, after the environment it depends on has been proven clean, so a marker
    here is a real account fault rather than an environment one wearing its costume.
    """
    for kind in ("envfail", "authfail", "identityfail"):
        path = os.path.join(spec.run_root, "state", kind, "%s.%s" % (iid, token))
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return kind, f.read().strip()
            except OSError:
                return kind, ""
    return None


def _self_refusal(spec, iid, token, edges=None, seconds=0.0):
    """The Result for a session that refused itself, or None. Speaks the session's OWN diagnosis
    so the memo names the fault instead of the launch machinery.

    ``seconds`` is a short GRACE for the path where the host gave up first: the pane writes its
    marker a beat after `agent start` has already failed, and reading once would report rc=2 (a
    channel fault that holds the queue, blaming the launch machinery) for what is really a poisoned
    environment or dead auth (per-issue, with a memo that names it). The shell launcher watched all
    three markers in ONE window and never had this race.
    """
    marker = _refusal_marker(spec, iid, token)
    waited = 0.0
    while marker is None and edges is not None and waited < seconds:
        edges.sleep(_VERIFY_PAUSE)
        waited += _VERIFY_PAUSE
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
    if kind == "identityfail":
        return Result(CLAUDE_IDENTITY, "\n".join([
            "[%s] CLAUDE IDENTITY REFUSED in the session's own environment — the flight was "
            "refused before it started." % iid,
            "[%s] %s" % (iid, why or "the session reported no detail"),
            "[%s] Nothing is running; this session would have worked on somebody else's "
            "subscription, or on none." % iid]))
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


def _record_delivery(spec, iid, session, untracked, resume):
    """Only NOW is it honest to record liveness. Writing the activity stamp before delivery was
    confirmed is exactly what fabricated 'launched & alive' for 45 minutes while no worker had
    started.

    ``untracked`` is every session class that is not a queued ISSUE — the debugger and, since
    #448, the triage flight. Neither has a lane counter to bump."""
    state = os.path.join(spec.run_root, "state")
    _write_atomic(os.path.join(state, "activity", iid), str(int(time.time())))
    # THE one writer of the recorded handle, and it writes through `lib/panes` so that what a
    # spawn records and what the nudge/teardown paths read back is one vocabulary rather than four
    # hand-rolled path joins (issue #334 — the #308 format change was invisible precisely because
    # there was no such place).
    panes.record(state, iid, session)
    if untracked or resume:
        # A debugger and a triage flight are not tracked issues, so neither has a counter; and a
        # REVIVE is deliberately not counted (#298) — `retries` is mechanical telemetry about how
        # many times a lane had to be STARTED OVER, and re-entering the same conversation is the
        # opposite of that.
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
    half-written value. True when it landed — the caller decides whether a miss is fatal."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        _rm_quiet(tmp)
        return False


def _rm_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = ["Spec", "Result", "Ran", "Edges", "WorktreeLock", "launch", "pane_environment",
           "is_poison", "WORKER_RE", "DEBUGGER_RE", "TRIAGE_RE", "WORKER", "DEBUGGER", "TRIAGE",
           "MODES", "OK", "ABORTED", "NOT_DELIVERED",
           "BASE_MISSING", "AUTH_DEAD", "AUTH_DEAD_RUNNER", "ENV_POISONED", "FENCE_DOWN",
           "UNSUPPORTED_AGENT"]
