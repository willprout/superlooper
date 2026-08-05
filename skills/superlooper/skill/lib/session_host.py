"""The ONE doorway to the session host: spawn / send / state / exit / kill (issue #304).

Nothing else in this engine talks to the host. That is what makes a later host swap a bounded
rewrite behind an unchanged interface — and it is the only way the distrust rules below get
enforced exactly once instead of being re-remembered at every call site.
``tests/test_one_session_host_door.py`` is the fence that keeps the doorway single; this module is
the only file on the scanned surface allowed to name the ``herdr`` binary, its verbs or its socket.

**Operating principle: the host is muscle, never truth.** Today the muscle is herdr (adopted
2026-07-30, docs/HERDR-ADOPTION-PLAN.md). Every rule here was paid for by a measured failure, and
each is named where it is enforced so a later reader can tell doctrine from paranoia:

* **spawn** — create the workspace, start the agent, then CONFIRM a process actually exists before
  calling the launch a launch (c15 verify-or-teardown). The owner personally watched
  ``agent list`` report six agents ``idle``/``interactive_ready`` while ``pgrep`` found zero claude
  processes (SPIKES-2026-07-30-supervised §10). A hollow launch raises and rolls back; it never
  no-ops, because a lane the runner believes is working is worse than a lane that failed.
* **send** — ``--wait`` ALWAYS. Plain ``agent prompt`` is banned outright (plan §5.1): 6/6 measured
  prompts typed their text, dropped the submission, and returned ``agent_prompted`` rc=0 in under a
  second. And rc is not evidence in either direction — even a settled ``--wait`` rc=0 means
  "queued" — so delivery is proven transcript-side, through an injected oracle, or the send raises.
  WHICH failure it raises matters as much as that it raises: a host that REFUSED the call is a
  channel fault (``HostError``, retry it), while a call the host accepted and the oracle could not
  confirm is ``DeliveryUnproven`` (something was typed). Conflating them makes a caller spend a
  one-shot nudge on a message the host never carried.
  A post-revive first prompt stalls even with ``--wait`` (5/5), so a REVIVED send chases
  ``send-keys enter``; that chaser stays until the pinned-build retest (#302) says otherwise.
  A revived send must also carry the re-orientation preamble (#298) — a revived session remembers
  the conversation, not the world.
  And ``agent_not_found`` is NEVER read as "this session died" until the host has been asked
  whether it is mid-restore (#317, herdrdev/herdr#2065): after a server restart the record comes
  back long before the pane's runtime, and the two states produce the identical error. A send that
  meets it waits the window out, bounded, and says so loudly if the bound expires.
* **state** — superlooper's own hooks are PRIMARY truth and are carried verbatim; the host's
  lifecycle word is ADVISORY and gates nothing (its ``idle`` means "ready AND seen in the focused
  UI" — an attended-mode notion that is meaningless for an unattended runner, plan §5.2). Liveness
  is a process fact: the pane's shell pid has a live child (c8), read from the OS, never from
  ``agent list``. Absence of signal is UNKNOWN, never idle/done (c2), and a host that names an
  agent's pane and then has no runtime behind it is RESTORING (#317) — a positive reading of a
  transient, not a shade of "we could not tell".
* **exit/kill** — our ordered teardown; the host's verbs are mechanism only. ``exit`` closes a
  FINISHED session's window and refuses anything else (positive allowlist, like ``tidy.closable``).
  ``kill`` is the forceful path and escalates only against a pid THIS read attributed to the
  session's own pane — never a name or pattern (house rule: a pattern can match the owner's own
  processes), and never a pid recorded at spawn that nothing can still vouch for. Both verbs report
  success only on positive evidence that the session went; a host that has stopped answering is
  silence, not confirmation.

Two addressing rules the host's own documentation dictates (plan §7):

1. **Address agents by NAME, never by a cached pane id.** A pane moved to another workspace gets a
   NEW workspace-qualified id and the old one stops resolving for outside callers — a cached handle
   breaking when the owner rearranges his window IS the cmux dragged-anchor incident class. Names
   follow the pane occupant and are CLEARED when the agent exits, so "the name no longer resolves"
   is itself a signal. The constraint is ``[a-z][a-z0-9_-]{0,31}``, validated here before any RPC.
   Where an id is unavoidable (process facts want a pane, close wants a workspace) it is resolved
   FRESH from the name at the moment of use — never read back out of the handle.
2. **Screen reads are never the evidence path.** Rows that scroll off the agent's alternate screen
   never enter the host's scrollback, so no line count recovers them. The file-based report
   mechanism stays; this module builds no ``agent read`` / ``pane read`` call at all.

Agent-boundary discipline (project CLAUDE.md): nothing agent-specific lives here. The wrapper never
reads a transcript itself — it takes a delivery ORACLE and a hooks value from its caller, so
"what a delivered prompt looks like" stays where the agent-specific knowledge already lives.

**Verb spellings** are pinned in one table below, read from the host CLI's own group help and its
``api schema --json`` (envelope: ``{"id", "result": {"type", ...}}`` on success,
``{"id", "error": {"code", "message"}}`` with rc=1 on failure). Responses are parsed defensively —
an explicit path first, then a keyed search — so a field that MOVES in a later release degrades to
UNKNOWN (which fails closed everywhere here) instead of raising somewhere unrelated.
"""
import json
import math
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field

import reorient

# The host's own name constraint. Issue-keyed names (i304, d12) satisfy it trivially; validating it
# here is what turns "unaddressable name" into a local raise instead of a half-built workspace.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The closed liveness vocabulary. There is deliberately no "probably" — an unreadable process fact
# is UNKNOWN, and every caller-visible decision here treats UNKNOWN as "not alive AND not finished".
#
# RESTORING is the fourth word and the one that costs the most to leave out (issue #317). It is not
# a shade of UNKNOWN: UNKNOWN means "nothing could be read", while RESTORING is a POSITIVE reading
# — the host has this agent's record back and no runtime behind its pane, which is what a server
# restart looks like for as long as `persist.restore` is still respawning. Both are refused by
# every teardown here, but only one of them tells a caller to come back in a minute.
ALIVE, DEAD, UNKNOWN, RESTORING = "alive", "dead", "unknown", "restoring"

# The host's lifecycle words, listed only so a reader can see what the ADVISORY field may carry.
# Nothing in this module branches on them — that is the point (plan §5.2).
ADVISORY_STATES = ("idle", "working", "blocked", "done", "unknown")

# The host error codes that mean "I do not have that" — the ONLY answer a teardown may read as
# proof the workspace went (see _workspace_gone). Its errors are spelled `<thing>_not_found`; the
# published schema types the envelope but does not enumerate the codes, so this is the assertion.
_GONE_CODES = re.compile(r"not[_ ]?found|no[_ ]?such|does[_ ]?not[_ ]?exist")

# --------------------------------------------------------------------------- the fence (#305)
# Token auth on the host's control socket, carried as a patch against the pinned release
# (vendor/herdr/). The patch is what ENFORCES it — inside the host process, where a worker cannot
# argue with it. What lives here is the other half: who is handed the token.
#
# Why a token rather than scrubbing the environment: the socket path is deterministic AND injected
# into every pane, the protocol is plain newline-JSON (so a worker needs no `herdr` binary to drive
# the fleet), and the socket's 0600 bit excludes other UNIX USERS while a worker runs as the same
# uid as the server. The env scrub below is the second layer, never the fence.

API_TOKEN_ENV_VAR = "HERDR_API_TOKEN"
# The host's other accepted source, named here only so the scrub below covers it. It is NOT the
# recommended one: an ordinary file is readable by the same uid, and the same uid is the worker.
# (The env var is safer precisely because macOS refuses a worker another process's environment.)
API_TOKEN_FILE_ENV_VAR = "HERDR_API_TOKEN_FILE"

# What a d<N> spawn passes INSTEAD of the token, for the patched host to substitute.
#
# This is the whole reason the wrapper never touches the secret. `workspace create --env K=V`
# becomes process argv, and on macOS a same-uid reader is refused another process's ENVIRONMENT but
# is served its ARGV — both measured while building this (reports/i305.md). Putting the real token
# on that command line would publish it, for the duration of the call, to exactly the workers the
# fence exists to keep out. So the request travels in argv and the secret never does.
GRANT_SENTINEL = "@superlooper-fence-grant"

# The launcher's own mode guard, spelled the same way here on purpose (lib/launch.py refuses
# `--cwd` for anything but `^d[0-9]+$`). Repair sessions must be able to drive herdr — revive, kill
# and read panes — so they RECEIVE the token; workers never do.
_DEBUGGER_RE = re.compile(r"^d[0-9]+$")

# Every variable the host uses to tell a pane where its control surface is. Stripped from what we
# forward for a worker spawn. Note what this does NOT do: the host injects its own pane-identity
# vars (HERDR_ENV, HERDR_PANE_ID, HERDR_SOCKET_PATH) whatever we pass — verified end-to-end — which
# is precisely why the token, and not this, is the fence.
_HOST_ENV_PREFIX = "HERDR"

# What a tokenless probe of the socket found. FENCED is the only good answer; OPEN is the dangerous
# one, because an unfenced socket does not look broken from the runner's seat — it answers.
FENCED, OPEN, UNREACHABLE = "fenced", "open", "unreachable"

# The MACHINE's own declaration that its fleet is fenced (#326) — see `fence_required` below for
# why this is an environment variable and not a key in the repo's config file.
FENCE_REQUIRED_VAR = "SL_FLEET_FENCE"
FENCE_REQUIRED, FENCE_OFF = "required", "off"

# --------------------------------------------------------------------- the allowance (#331)
# The fence refuses every unauthenticated connection before dispatch, with exactly ONE exception,
# ruled by the owner on 2026-08-04: the agent's own state report. That call is what tells the host
# which session id to `--resume` after a crash, it is made by the host's own hook script carried
# byte-for-byte (#307), and that script presents no token because upstream herdr has no token
# concept — nor do `i<N>` worker panes, by the same 2026-07-31 distribution ruling. Without the
# allowance a fenced host captures no id at all and `persist.restore` brings a crashed pane back as
# a bare shell, SILENTLY: the session launches, runs and works.
#
# The allowance lives in the carried patch, host-side and method-scoped (`auth::admit`). Nothing
# here grants it and nothing here can: what this module owns is the ability to ASK a live socket
# which of the two states a machine is in, because "the fence is up and the capture works" and "the
# fence is up and the capture is silent" look identical from the runner's seat.
STATE_REPORT_METHOD = "pane.report_agent_session"

# What a tokenless state report was answered with. ADMITTED is the ruled behaviour; REFUSED is a
# host built before the allowance (or from a patch that lost it at a version bump).
ADMITTED, REFUSED = "admitted", "refused"

# The pane the capture probe names, and the agent label it reports — BOTH deliberately unusable.
#
# The probe has to reach the method body to learn that the fence let it in, and a doctor command
# must not write anything while it does. The host's own handler checks the pane id first and the
# agent label second, so an unresolvable pane refuses it before any state is touched, and an empty
# label refuses it again in the impossible case that the id resolved anyway. Two independent stops,
# because "the doctor corrupted a live pane's resume record" is not a bug anybody would enjoy.
_PROBE_PANE = "superlooper-capture-probe-not-a-pane"
_PROBE_AGENT = ""

# The host's own session-name rule, transcribed from the pinned release (`session::validate_name`:
# ASCII alphanumerics plus `.`, `_` and `-`, at most 64 bytes, and never `.` or `..`). A name that
# fails it is IGNORED by the host, which then serves on the plain config-dir socket — so a resolver
# that did not check would point at a directory nothing ever creates. No trimming: the host does
# none, so a name with a leading space is invalid rather than tidied into a valid one.
#
# `\Z`, not `$`: Python's `$` also matches just before a TRAILING NEWLINE, so `"loop\n"` — a name
# the host rejects outright — would pass a `$`-anchored version of this and send the probe to a
# directory that cannot exist. The test parametrises that exact value.
_SESSION_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

# The most a single control-socket reply may be before `_speak` gives up on it. A real one is a
# JSON line; nothing legitimate is near this, and the number exists so a hostile or wedged peer
# cannot grow a probe's buffer without limit.
_MAX_REPLY_BYTES = 4 * 1024 * 1024

_DEFAULT_BINARY = "herdr"
_CALL_SECONDS = 20                 # a control call that hangs is a dead host, not a slow one
_PROMPT_TIMEOUT_MS = 120000        # the host's own documented prompt wait
_START_TIMEOUT_MS = 30000          # its documented agent-start default, named rather than implied
_START_RETRIES = 6                 # a pane's login shell has to reach its prompt before an agent
_START_RETRY_PAUSE = 1.0           # can be started in it — bounded, ~6s, and only for that refusal
# The host's way of saying "that pane is not at a shell prompt yet". Asserted here like _GONE_CODES
# is: if a later release renames it, spawns fail LOUDLY on a cold pane rather than silently.
_PANE_BUSY = re.compile(r"pane[_ ]busy|not an available shell|no[_ ]available[_ ]shell")
_CONFIRM_READS = 5                 # post-spawn confirmation attempts...
_CONFIRM_PAUSE = 1.0               # ...one second apart: `agent start` can return a beat before
                                   # the pane's own child is visible to the OS
_TEARDOWN_READS = 3
_TEARDOWN_PAUSE = 1.0

# ----------------------------------------------------------------- the restore window (#317)
# herdrdev/herdr#2065, closed upstream as `duplicate` and NOT fixed on the pinned release. After
# the host server restarts, `persist.restore` puts the agent RECORDS back long before the panes'
# runtimes exist, so for the length of that window the two surfaces flatly disagree:
#
#     agent list / agent get      ->  idle, interactive_ready=true          <- lies
#     pane process-info --pane …  ->  pane_not_found                        <- honest
#     agent prompt … --wait       ->  agent_not_found                       <- honest
#     the pid recorded at spawn   ->  gone, with the whole server tree
#
# `agent_not_found` is also exactly what a genuinely-dead agent produces, and THAT is the hazard: a
# caller reading it as "the session is gone" tears down a flight that was about to come back on its
# own, which turns a survivable host restart into lost work.
#
# TWO KNOWN FALSE POSITIVES, recorded rather than implied (fresh-agent review). Neither is unsafe —
# both cost a bounded wait and then the ordinary refusal — but neither is "only a restore produces
# this", which is what the conjunction below is tempting to claim:
#   * a pane the OWNER closed under a live agent record (`pane close`) presents the same "name
#     resolves, pane refused" pair, and pays the full bound on every send until the record clears;
#   * a host that resolves a name and reports NO pane for it is read as unreadable rather than as
#     restoring, so a restore that rebuilds a record before assigning a pane would be missed. That
#     one fails toward the retryable channel fault, never toward "gone".
#
# The window was RE-MEASURED on the pinned v0.8.0 and came back an order of magnitude wider than
# the numbers #302 recorded on the same build one day earlier (2026-08-05, reports/i317.md):
# 3.9–8.2s across six runs of the shipped drill, and 19.6s / 28.9s from the host's own
# `persist.restore`→`pane.spawned` log lines while a probe grid was running — load moves it, which
# is exactly why it is re-measured rather than trusted. Against #302's 0.78–4.06s.
#
# 45 seconds is that worst reading with half again on top, and it is capped from ABOVE by the
# caller rather than chosen freely: the only production caller is `lib/nudge.py`, whose whole
# budget the runner kills at `NUDGE_TIMEOUT` (120s). A bound that does not fit inside it does not
# produce a loud expiry at all — it produces rc=124, which that module's own comment says "reads as
# a channel fault and holds the queue". An operator whose machine is slower raises both, through
# the variable below. (Fresh-agent review, P0: the first draft's 120s default could not have
# reached a caller.)
#
# WHAT THE WAIT ACTUALLY COVERS IN PRODUCTION, measured by driving `nudge-pane.sh` through a real
# restore window (reports/i317.md): almost never the whole window. `nudge` asks `state` FIRST, and
# a RESTORING verdict is not ALIVE, so it defers in 0.1s with nothing typed and the runner simply
# tries again next tick — which is the cheaper shape of "wait, not dead" and the one that carries
# most of this issue's weight. The send-path wait is the SECOND net: it covers the residual race
# where the window opens in the few hundred milliseconds between that state read and the prompt.
# Sizing it against the whole window is still right, because the race can open at the window's very
# start.
RESTORE_WAIT_SECONDS = 45.0
RESTORE_POLL_SECONDS = 1.0
# The operator's override, same convention as SL_HERDR one screen up. It exists because the right
# bound is a property of the MACHINE (how long its disk takes to respawn a fleet of panes), not of
# this code, and an operator who has measured their own should not have to patch the engine.
RESTORE_WAIT_ENV_VAR = "SL_HERDR_RESTORE_WAIT"

# How long `exit` waits before believing a DEAD reading (#317). Comfortably past the ~0.7s the
# restore's shell-respawned-agent-not-yet-attached gap measured at, and paid only on teardown —
# which is rare, and which is the one verb here that destroys something.
_EXIT_CONFIRM_PAUSE = 2.0


# --------------------------------------------------------------------------- failures
# Every one of these is raised rather than returned. A teardown that could not be verified, a
# delivery that could not be proven and a hollow spawn all have the same failure mode if they are
# reported as values: a caller that forgets to look at the value carries on believing it worked.

class HostError(Exception):
    """Base for every refusal this doorway makes."""


class NameRefused(HostError):
    """The name cannot address an agent on this host (or could not be derived from an id)."""


class SpawnRefused(HostError):
    """The session was not created, or could not be confirmed — and has been rolled back."""


class DeliveryUnproven(HostError):
    """The prompt may or may not have reached the agent; nothing proved that it did."""


class ReorientationMissing(HostError):
    """A revived session was about to be handed an instruction without re-orientation first."""


class RestoreWindowExpired(HostError):
    """The host was still mid-restore when the bounded wait ran out (issue #317).

    Deliberately NOT ``DeliveryUnproven``: the host REFUSED every attempt, so nothing was typed and
    a caller holding a one-shot nudge must not spend it blaming a worker for ignoring a message
    that was never carried. Deliberately not a silent return either — a window that outlives the
    bound is either a machine in trouble or a build whose window has moved, and both are things a
    person should see rather than a loop should absorb.
    """


class TeardownRefused(HostError):
    """This session may not be torn down by this verb (still live, unknown, or not ours)."""


class TeardownUnverified(HostError):
    """The close was issued and the session is still there."""


# --------------------------------------------------------------------------- records

@dataclass(frozen=True)
class Session:
    """A handle on one hosted session.

    ``pane`` is a CONVENIENCE, never an address: every read here re-resolves the pane from the name
    (see the module docstring). ``shell_pid`` is the pid we recorded at spawn — the only pid this
    module will ever signal. ``owned`` is False for a pane we did not create, which both teardown
    verbs refuse to close (the host's own rule, and ours).
    """
    name: str
    workspace: str
    pane: str = None
    tab: str = None
    shell_pid: int = None
    owned: bool = True


@dataclass(frozen=True)
class HostState:
    """What is knowable about a session right now, with each fact labelled by its provenance.

    ``hooks`` is PRIMARY (the caller's own hook-derived truth, carried verbatim and never
    interpreted here). ``advisory`` is the host's lifecycle word and gates nothing. ``liveness`` is
    an OS fact. Nothing fills a missing ``hooks`` with ``advisory`` — that substitution is exactly
    the phantom this split exists to keep out.
    """
    name: str
    liveness: str
    pane: str = None
    shell_pid: int = None
    name_resolves: bool = None
    advisory: str = None
    hooks: object = None
    detail: str = ""


@dataclass(frozen=True)
class Sent:
    """The outcome of a proven delivery. There is no 'maybe' — an unproven send raises.

    ``restored`` is True when this delivery only landed after the wrapper waited out a host restore
    window (#317). It is reported rather than smoothed over because the session on the other end is
    now a REVIVED one — herdr brought it back with ``--resume``, so it remembers the conversation
    and nothing about what happened to the world (#298).

    **This is a KNOWN HOLE in the #298 rule, not a delegation of it** (fresh-agent review). A send
    that opens with ``revived=True`` is refused without the re-orientation preamble; a send that
    BECOMES revived under the wrapper's feet, mid-call, is not — the text was already chosen by a
    caller who believed the session was live, and refusing it here would throw away the flight this
    whole path exists to save. So the prompt goes and the flag says what happened. Closing it means
    deciding what a caller should do with an instruction it can no longer re-word, which is that
    caller's policy and a separate issue.
    """
    delivered: bool
    chased: bool = False
    rc: int = None
    advisory: str = None
    restored: bool = False
    waited_seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class Teardown:
    """The outcome of a verified teardown. ``signalled`` names how far the escalation had to go.

    ``orphan_pid`` is the one honest loose end this module can be left holding: a pid recorded at
    spawn that is still alive after the host has positively let go of the workspace. It may not be
    signalled (nothing can still attribute it to this session, and the OS reuses pid numbers) and it
    may not veto the teardown either (that would make a legitimately-ended session unverifiable
    forever). So it is REPORTED — a named doubt the caller can log or park on, never a silence.
    """
    closed: bool
    signalled: list = field(default_factory=list)
    orphan_pid: int = None
    detail: str = ""


# --------------------------------------------------------------------------- the external edges

class Probe:
    """Every edge this module touches: the host CLI, and the OS process facts that judge it.

    Injected so no test resolves a real host binary (the conftest ratchet) — and so the process
    facts, which are the whole basis of liveness here, can be staged deterministically.
    """

    def run(self, argv, timeout=_CALL_SECONDS):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # 124 like the shell's timeout(1), and like stack_doctor's probe: a host that does not
            # answer is a host we know nothing through, which every caller here reads as UNKNOWN.
            return subprocess.CompletedProcess(argv, 124, "", "no answer within %ss" % timeout)
        except (OSError, ValueError):
            return subprocess.CompletedProcess(argv, 127, "", "could not run %s" % (argv[:1],))

    def pid_alive(self, pid):
        """Is this pid a RUNNING process?

        Signal 0 probes without delivering, and a pid we may not signal (EPERM) still exists. But
        signal 0 alone is not the question asked here: a DEFUNCT process — killed, not yet reaped
        by its parent — answers it happily. The end-to-end drive of this module caught exactly
        that: after SIGTERM and SIGKILL both landed, the pid still "existed" and the teardown
        reported itself unverified. So an existing pid is demoted when the OS says it is a zombie.

        The order is deliberate. Signal 0 is the primary read because it cannot fail for want of a
        binary; ``ps`` only ever DEMOTES its answer, and an unreadable ``ps`` leaves it standing.
        The alternative fail direction — "cannot tell, call it dead" — would let ``exit`` tear down
        a live session, which is the one mistake this module must never make.
        """
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True
        return int(pid) not in self._defunct([pid])

    def child_pids(self, pid):
        """The pane shell's live children (c8), or None when the OS could not be asked.

        `pgrep -P` is a READ; nothing here ends a process by pattern. Two answers that must never
        collide: rc=1 means "this pane has no children", which ENDS a session, while a timeout or a
        missing binary means nothing at all — and returning [] for both would let a failed probe
        authorise ``exit`` to close a working session. Defunct children are filtered out for the
        same reason as ``pid_alive``: a pane whose only child is a zombie is a bare shell.
        """
        proc = self.run(["pgrep", "-P", str(pid)], timeout=5)
        if proc.returncode not in (0, 1):        # 1 = no match, which IS a real answer
            return None
        kids = [int(line) for line in (proc.stdout or "").split() if line.isdigit()]
        defunct = self._defunct(kids)
        return [kid for kid in kids if kid not in defunct]

    def _defunct(self, pids):
        """The subset of ``pids`` the OS reports as zombies — one `ps` read, never per-pid.

        An unreadable answer returns the EMPTY set, i.e. "nothing is demoted": this read may only
        ever take liveness away from a pid signal 0 already vouched for, never grant it.
        """
        pids = [int(p) for p in pids if isinstance(p, int) or (isinstance(p, str) and p.isdigit())]
        if not pids:
            return set()
        proc = self.run(["ps", "-o", "pid=,state=", "-p", ",".join(str(p) for p in pids)],
                        timeout=5)
        if proc.returncode != 0:
            return set()
        out = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].startswith("Z"):
                out.add(int(parts[0]))
        return out

    def terminate(self, pid):
        return self._signal(pid, signal.SIGTERM)

    def kill(self, pid):
        return self._signal(pid, signal.SIGKILL)

    def _signal(self, pid, sig):
        # BY PID ONLY — the pid this module recorded at spawn or re-read from the live pane. The
        # house rule against `pkill -f`/`killall` exists because a pattern can also match the
        # owner's own live processes.
        try:
            os.kill(int(pid), sig)
            return True
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return False

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        """A monotonic clock, injected for the same reason `sleep` is.

        The restore wait (#317) is the one loop here whose bound has to be WALL CLOCK rather than a
        count of pauses. Counting pauses was the first draft and it was not a bound at all: the
        delivery oracle polls for six seconds of its own on every check, so a "120 second" wait ran
        about fourteen minutes — past the caller's own kill timeout, which turned the loud expiry
        this issue asked for into an rc=124 that reads as a channel fault (fresh-agent review, P0).
        Monotonic, so a clock step cannot extend or collapse the wait.
        """
        return time.monotonic()


class _Reply:
    """A parsed host answer. ``ok`` means the host returned a result — never that it told truth."""

    def __init__(self, ok, payload, error, rc, spoke):
        self.ok, self.payload, self.error, self.rc, self.spoke = ok, payload, error, rc, spoke


def _parse(proc):
    """Turn a CompletedProcess into a _Reply, fail-closed on anything unparseable.

    ``spoke`` records whether the HOST answered (a structured error envelope) as opposed to the
    call never getting through (timeout, missing binary, garbage). The difference matters exactly
    once — "the host says this name is gone" is a signal, "we could not ask" is not.
    """
    rc = getattr(proc, "returncode", 1)
    out = (getattr(proc, "stdout", "") or "").strip()
    err = (getattr(proc, "stderr", "") or "").strip()
    body = None
    for text in (out, err):
        if not text:
            continue
        try:
            candidate = json.loads(text)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            body = candidate
            break
    if body is not None and isinstance(body.get("result"), dict) and rc == 0:
        return _Reply(True, body["result"], "", rc, True)
    if body is not None and isinstance(body.get("error"), dict):
        detail = "%s: %s" % (body["error"].get("code", "error"), body["error"].get("message", ""))
        return _Reply(False, None, detail.strip(), rc, True)
    return _Reply(False, None, (err or out or "no answer")[:400], rc, False)


def _is_gone(reply):
    """Did the host answer "I do not have that"? — the ONE place its not-found spelling is read.

    Both halves are load-bearing. ``spoke`` first, because a timeout or a missing binary is not the
    host saying anything (c2), and a caller that read silence as not-found would treat a dead host
    as a tidy answer. Then the CODE, not the message: a message can carry the words "not found" for
    an unrelated reason, and the code is the field the host means as a verdict.
    """
    if reply.ok or not reply.spoke:
        return False
    code = (reply.error or "").split(":", 1)[0].strip().lower()
    return bool(_GONE_CODES.search(code))


def _dig(payload, *path):
    """Follow an explicit path through a response, or None. Never raises on a shape change."""
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _find(payload, key, depth=6):
    """The first value under ``key`` anywhere in a response.

    The explicit path is tried first everywhere this is used; this is the degradation path for a
    field that MOVES in a later release. A miss returns None, which fails closed.
    """
    if depth < 0:
        return None
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find(value, key, depth - 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find(value, key, depth - 1)
            if found is not None:
                return found
    return None


def _seconds(value, fallback):
    """A positive FINITE number of seconds, or ``fallback``. Never raises, never returns 0.

    Used for the restore bound, whose override arrives from an operator's environment. A junk
    value must not take a runner down at construction, and must not degrade to 0 either: 0 means
    "never wait out a restore", which is the exact behaviour issue #317 exists to remove.

    ``isfinite`` is not decoration (fresh-agent review, P1). ``float()`` happily accepts ``inf``,
    ``"Infinity"`` and ``"1e400"``, and an infinite bound escaped this guard to raise
    ``OverflowError`` from the wait's arithmetic — out through `nudge.nudge`, which catches
    ``HostError`` and documents itself as never raising. A guard that admits the one value able to
    break its own caller is not a guard.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) and number > 0 else fallback


def _pid(value):
    """A usable pid, or None. 0 and negatives are not pids; a string of digits is."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value) or None
    return None


# --------------------------------------------------------------------------- names

def _validate(name):
    """The name as given, or NameRefused. Deliberately does NOT normalise: a caller that hands the
    doorway an unaddressable name has a bug, and silently repairing it hides the bug until the day
    two lanes collide on one repaired name."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise NameRefused(
            "%r cannot address an agent on this host: names must match %s (lowercase, <=32 chars)"
            % (name, NAME_RE.pattern))
    return name


def name_for(session_id):
    """A lane id (``i304``, ``D12``) as a host-legal agent name, or NameRefused.

    Case is repaired because ids are ours and their case carries no meaning; length and character
    class are NOT, because a truncated name would silently address a different agent.
    """
    if not isinstance(session_id, str):
        raise NameRefused("%r is not an id" % (session_id,))
    return _validate(session_id.strip().lower())


def _name_of(session):
    return session.name if isinstance(session, Session) else session


# --------------------------------------------------------------------------- the fence

def receives_token(name):
    """Is this session one that is GIVEN the control-socket token? (issue #305)

    The NAME decides, and nothing else does. There is deliberately no parameter anywhere in this
    module that grants the token, because "the ``i<N>`` spawn path provably never gets it" has to
    be a property of the code's shape rather than of every caller's care — a flag that can be
    passed is a flag that will one day be passed by the wrong call site.

    Deny by default: only a real ``d<N>`` id is a repair session. Anything unrecognised is treated
    as a worker, which is the direction a mistake here should fail in.

    Holding the token is CAPABILITY, never PERMISSION — the D13 supervised/unattended rails still
    govern what a ``d<N>`` session may actually do with it.
    """
    return bool(isinstance(name, str) and _DEBUGGER_RE.match(name))


def fence_required(env=None):
    """Does THIS MACHINE declare its fleet fenced? ``(required, unrecognised value or None)``.

    The switch the launch pre-flight reads (#326). A verdict on its own decides nothing — an OPEN
    socket is the normal, correct state of a dev workstation running a stock host, and a pre-flight
    that refused there would break every dev spawn on the day it shipped. This is what says which
    machines an OPEN answer is fatal on.

    **An ENVIRONMENT variable rather than a key in the repo's own config**, which is the one design
    decision here worth defending. ``.superlooper/config.json`` travels with the repo through git,
    so the fleet mini and the owner's laptop read the SAME file — and the whole point of the switch
    is that those two machines must answer differently. The fence is a property of the host server
    running on a machine, not of a checkout. ``SL_FLEET_*`` is already the spelling for exactly that
    claim (``identity.FLEET_DIR_VAR``: "what the MACHINE sets").

    Three answers, and the third is the one that matters:

    * unset, empty, or ``off`` — no fence is claimed. ``off`` exists so a machine can be taken out
      of the fenced set DELIBERATELY, leaving a value on disk that says so; deleting the variable is
      indistinguishable from never having set it.
    * ``required`` — the fence is claimed, and the pre-flight enforces it.
    * **anything else — the fence is claimed anyway, and the value comes back so the refusal can
      name it.** Fails CLOSED because the alternative is the failure this whole issue exists to
      end: a typo on the fleet machine reading as "off" is a silently disarmed fence, and an
      unfenced socket does not look broken from the runner's seat. Truthy spellings (``1``,
      ``true``, ``yes``) are deliberately NOT accepted as synonyms rather than being quietly
      welcomed — ``SL_FLEET_FENCE=0`` reads to a human as "disarmed" and to a truthy parser as
      nothing at all, and a switch whose two readers disagree about a security posture is worse
      than one that is simply strict about its two words.
    """
    env = os.environ if env is None else env
    raw = env.get(FENCE_REQUIRED_VAR)
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or value.lower() == FENCE_OFF:
        return False, None
    if value.lower() == FENCE_REQUIRED:
        return True, None
    return True, value


def fence_probe(socket_path, env=None, timeout=5.0, connect=None):
    """Ask the host's socket the question a WORKER would ask, and report what came back.

    This is the check that turns "the fence is up" from an assumption into an observation, and the
    one a version bump re-runs (vendor/herdr/README.md). It presents NO token on purpose: a probe
    that authenticated itself would report the fence up on a socket that is wide open to everyone
    else.

    Returns FENCED (refused — the good answer), OPEN (a tokenless caller was SERVED, i.e. there is
    no fence), or UNREACHABLE. Silence is UNREACHABLE, never FENCED: absence of signal is UNKNOWN
    (c2), and a socket that accepts and then says nothing has proven nothing.

    ``env`` is accepted for signature symmetry with the rest of this module and is DELIBERATELY
    NOT READ. Both probes here present no credential by construction, so passing one an environment
    holding the token does not authenticate it — and must not be read as doing so. (Not a raise:
    the runner's own environment legitimately holds the token, and the launch pre-flight #326 will
    call these from exactly there.)
    """
    line = (connect or _speak)(socket_path, json.dumps(
        {"id": "superlooper-fence-probe", "method": "ping", "params": {}}) + "\n", timeout)
    if not line:
        return UNREACHABLE
    try:
        reply = json.loads(line)
    except (TypeError, ValueError):
        return UNREACHABLE
    error = reply.get("error") if isinstance(reply, dict) else None
    if isinstance(error, dict) and error.get("code") == "unauthorized":
        return FENCED
    if isinstance(reply, dict) and reply.get("result"):
        return OPEN
    # Some other error: the host spoke but not about auth. That is not a fence, and it is not a
    # served request either — refuse to call it either one.
    return UNREACHABLE


def state_report_probe(socket_path, env=None, timeout=5.0, connect=None):
    """Does this socket admit the vendor's state report from a TOKENLESS caller? (issue #331)

    The second half of the fence's question, and the one nothing else can answer by looking at a
    file: `fence_probe` says whether the fence is up, this says whether the ruled hole is in it.
    A machine can be fenced with the capture working and fenced with the capture silent, and from
    the runner's seat those two are the same machine — sessions launch, run and work in both.

    Presents NO token, for the same reason `fence_probe` does not: it must ask exactly what the
    hook in a worker pane asks, and a probe that authenticated itself would report an allowance
    that no worker actually enjoys. ``env`` is accepted for signature symmetry and deliberately
    unread — see `fence_probe`.

    Returns ADMITTED (the call reached the method body), REFUSED (`unauthorized` — the fence turned
    it away, so this host captures no session ids), or UNREACHABLE.

    ADMITTED means "past the fence", NOT "the report was accepted" — the probe's own arguments are
    deliberately unusable (see `_PROBE_PANE`), so the host's own refusal of them is the evidence.
    Any error code OTHER than `unauthorized` is therefore a pass: the fence's vocabulary is the
    discriminator, and a code that MOVES in a later release degrades to ADMITTED rather than
    silently reading a live allowance as absent.

    On an UNFENCED host every method is admitted, so this returns ADMITTED there too. That is not a
    flaw to fix here — it is why the doctor asks BOTH probes and reads them together.
    """
    line = (connect or _speak)(socket_path, json.dumps({
        "id": "superlooper-capture-probe",
        "method": STATE_REPORT_METHOD,
        "params": {"pane_id": _PROBE_PANE, "source": "herdr:claude", "agent": _PROBE_AGENT},
    }) + "\n", timeout)
    if not line:
        return UNREACHABLE
    try:
        reply = json.loads(line)
    except (TypeError, ValueError):
        return UNREACHABLE
    if not isinstance(reply, dict):
        return UNREACHABLE
    error = reply.get("error")
    if isinstance(error, dict):
        return REFUSED if error.get("code") == "unauthorized" else ADMITTED
    if reply.get("result"):
        return ADMITTED
    # The host spoke, but said neither. Refuse to call that either answer (c2).
    return UNREACHABLE


def control_socket_path(env=None):
    """Where this machine's host keeps its control socket, resolved the way the host resolves it.

    Read from the environment rather than by asking the binary, so a caller that only wants to
    PROBE never has to run anything. The order is the host's own (`session::active_api_socket_path`
    at the pinned release), minus the `--session` CLI flag, which is a thing only its CLI has:

    1. ``HERDR_SOCKET_PATH`` — the explicit override, and what the adoption plan's short-socket-path
       deployment (§2, the 104-char ``sun_path`` limit) actually sets;
    2. otherwise a named session's own directory, if ``HERDR_SESSION`` names one the host would
       actually honour — not the default, and passing the host's own name validation;
    3. otherwise the config directory itself.

    Step 2's validation is not decoration. The host IGNORES a name it considers invalid and serves
    on the plain config-dir socket; a resolver that accepted any string would send the probe to a
    ``sessions/<junk>/`` path that cannot exist, and the doctor would then report "nothing is
    listening" about a machine with a live host. Deferring to what the host does is the only way
    that answer stays true.

    Returns None when there is nothing to resolve against (no HOME and no XDG override), because a
    guessed path would make a probe answer UNREACHABLE about a machine it never looked at.
    """
    env = os.environ if env is None else env
    explicit = env.get("HERDR_SOCKET_PATH")
    if explicit:
        return explicit
    config_home = env.get("XDG_CONFIG_HOME")
    if config_home:
        # `herdr-dev` is the DEBUG build's directory name; a release build is plain `herdr`, and a
        # release build is the only thing #302's pin ships.
        base = os.path.join(config_home, "herdr")
    else:
        home = env.get("HOME")
        if not home:
            return None
        base = os.path.join(home, ".config", "herdr")
    session = env.get("HERDR_SESSION") or ""
    if session != "default" and _SESSION_NAME_RE.match(session) and session not in (".", ".."):
        return os.path.join(base, "sessions", session, "herdr.sock")
    return os.path.join(base, "herdr.sock")


def _speak(socket_path, payload, timeout):
    """One newline-JSON exchange over the control socket. The only place this module opens it.

    No `herdr` binary is involved — which is the whole point of the fence: this is exactly what a
    worker could do with ten lines of python and a path it already has.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    # `timeout` is per-recv, and the two bounds below are what turn that into a bound on the CALL.
    # Without them a peer that emits one byte every `timeout - 1` seconds is never timed out and
    # never ends its line, so this loop runs forever and `buf` grows forever — and the callers are
    # a doctor block and a launch pre-flight, i.e. exactly the places where hanging quietly is the
    # worst available outcome. Whatever is at the other end of this socket is precisely the thing
    # the fence exists because we do not trust.
    deadline = time.monotonic() + max(float(timeout), 0.0) * 2
    try:
        client.connect(socket_path)
        client.sendall(payload.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            if time.monotonic() > deadline or len(buf) > _MAX_REPLY_BYTES:
                # A truncated read is not a reply. Say nothing rather than hand a caller half a
                # line to make a verdict out of — every probe here reads "" as UNREACHABLE.
                return ""
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace").strip()
    except (OSError, ValueError):
        return ""
    finally:
        client.close()


# --------------------------------------------------------------------------- where the host lives
# The build-up (issue #309) has to CREATE the things this module elsewhere only talks to: install
# the binary, write the config the server reads, put the socket somewhere short enough to bind. It
# needs the host's own path derivations to do that — and every one of them is host machinery, so
# they live here, behind the same door as the verbs. `lib/fleet.py` reads them from this module and
# spells none of them itself, which is what keeps the swap bounded: a tmux host replaces these
# three functions along with the five verbs, and the build-up's logic is untouched.
#
# Read out of the pinned release's own source (`src/session.rs`: `data_dir_for` /
# `api_socket_path_for`, `src/config.rs`: `config_dir`), not guessed — a wrong path here produces a
# server nobody can reach while everything reports success.

BINARY_NAME = _DEFAULT_BINARY
# The host's config directory NAME under the XDG config home. Not a full path: the home itself is
# the caller's environment, and baking one in would freeze a location the operator may have moved.
CONFIG_DIR_NAME = "herdr"
SOCKET_FILE = "herdr.sock"


def config_dir(env=None):
    """The host's config directory — ``$XDG_CONFIG_HOME/<name>``, else ``~/.config/<name>``.

    The build-up deliberately does NOT set ``XDG_CONFIG_HOME`` for the server: a pane inherits the
    server's environment, and an inherited XDG config home was measured de-authenticating ``gh``
    while it kept answering (docs/SPIKES-2026-07-29-results.md). Isolation comes from the named
    session below, which costs a pane nothing.
    """
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".config")
    return os.path.join(base, CONFIG_DIR_NAME)


def state_dir(env=None):
    """The host's state directory — ``$XDG_STATE_HOME/<name>``, else ``~/.local/state/<name>``.

    Separate from the config dir because the host keeps them separate: config is what an operator
    writes, state is what the host caches for itself — including the agent-detection manifests it
    fetches in the background, which the build-up snapshots.
    """
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, CONFIG_DIR_NAME)


def remote_manifest_path(host_state_dir, agent):
    """The host's cached copy of an agent's REMOTE screen-detection manifest.

    This is the file the build-up snapshots into a local override: it is what the host is actually
    using today, so a snapshot of it freezes nothing the machine did not already have.
    """
    return os.path.join(host_state_dir, "agent-detection", "remote", "%s.toml" % agent)


def session_socket_path(host_config_dir, session=None):
    """The control socket for a named session — ``<config dir>/sessions/<name>/<socket>``.

    ``session=None`` is the host's DEFAULT session, whose socket sits directly in the config dir.
    The fleet always names its session: a dedicated name is what lets the owner's own window and
    the fleet's server share one machine without sharing one server (plan §2).
    """
    if session is None:
        return os.path.join(host_config_dir, SOCKET_FILE)
    return os.path.join(host_config_dir, "sessions", session, SOCKET_FILE)


# --------------------------------------------------------------------------- the doorway

class SessionHost:
    """The five verbs. Everything else in this class is private and stays that way — a sixth public
    verb is how the interface stops being swappable."""

    def __init__(self, probe=None, binary=None, env=None, prompt_timeout_ms=_PROMPT_TIMEOUT_MS,
                 confirm_reads=_CONFIRM_READS, confirm_pause=_CONFIRM_PAUSE,
                 teardown_reads=_TEARDOWN_READS, teardown_pause=_TEARDOWN_PAUSE,
                 call_seconds=_CALL_SECONDS, restore_wait_seconds=None,
                 restore_poll_seconds=RESTORE_POLL_SECONDS):
        self._probe = probe if probe is not None else Probe()
        self.env = env if env is not None else os.environ
        # Same override convention as SL_CMUX / SL_GH / SL_CLAUDE, and for the same reason: it is
        # what lets conftest point every test at a binary that cannot exist.
        self.binary = binary or self.env.get("SL_HERDR") or _DEFAULT_BINARY
        self._prompt_timeout_ms = int(prompt_timeout_ms)
        self._confirm_reads = int(confirm_reads)
        self._confirm_pause = confirm_pause
        self._teardown_reads = int(teardown_reads)
        self._teardown_pause = teardown_pause
        # How long ONE control call may hang. A verb makes several, so a caller whose own deadline
        # is short — a sweep that runs every tick over every lane — has to be able to say so, or a
        # single wedged host turns a best-effort teardown into minutes of stalled tick.
        self._call_seconds = float(call_seconds)
        # How long a send may wait out a host restore window (#317), and how often it looks. The
        # caller's argument wins, then the operator's environment, then the measured default —
        # and a junk override falls back to the default rather than raising here or, far worse,
        # becoming 0.0, which is precisely the never-wait behaviour this issue exists to remove.
        self.restore_wait_seconds = _seconds(
            restore_wait_seconds,
            _seconds(self.env.get(RESTORE_WAIT_ENV_VAR), RESTORE_WAIT_SECONDS))
        self.restore_poll_seconds = _seconds(restore_poll_seconds, RESTORE_POLL_SECONDS)

    # ---- verb 1: spawn -----------------------------------------------------------------
    def spawn(self, name, cwd, env=None, kind="claude", agent_args=(), label=None,
              start_timeout_ms=_START_TIMEOUT_MS):
        """Create the session and PROVE it exists, or roll back and raise (c15).

        The rollback is the part that matters. A refused spawn that left its workspace behind leaks
        a pane per attempt, and the next launch of the same lane collides with a name that is still
        taken — so every failure path below closes what it created before it raises.
        """
        name = _validate(name)
        create = ["workspace", "create", "--cwd", str(cwd), "--no-focus"]
        if label:
            create += ["--label", str(label)]
        for key, value in sorted(self._pane_env(name, env).items()):
            create += ["--env", "%s=%s" % (key, value)]
        reply = self._call(create)
        if not reply.ok:
            raise SpawnRefused("[%s] the host would not create a workspace: %s"
                               % (name, reply.error))

        workspace = _dig(reply.payload, "workspace", "workspace_id") or _find(reply.payload,
                                                                             "workspace_id")
        tab = _dig(reply.payload, "tab", "tab_id") or _find(reply.payload, "tab_id")
        pane = _dig(reply.payload, "root_pane", "pane_id") or _find(reply.payload, "pane_id")
        if not workspace:
            # We may have created something we cannot address. Say so plainly rather than pretend
            # the launch simply failed — a leak the operator can see beats a leak they cannot.
            raise SpawnRefused("[%s] the host created a workspace but reported no id, so it could "
                               "not be closed again: %s" % (name, json.dumps(reply.payload)[:300]))
        if not pane:
            self._rollback(workspace)
            raise SpawnRefused("[%s] the host reported no root pane to start the agent in" % name)

        start = ["agent", "start", name, "--kind", str(kind), "--pane", pane,
                 "--timeout", str(int(start_timeout_ms))]
        if agent_args:
            start += ["--"] + [str(a) for a in agent_args]     # native agent args, after the sep
        # A pane that is still BOOTING is a wait, not a failure — measured against a live host
        # while wiring the spawners onto this doorway (#308, reports/i308.md). `workspace create`
        # returns as soon as the workspace exists, but the host will only start an agent in "an
        # available shell", and the pane's login shell (which sources the operator's rc files) is
        # still coming up. It refuses with `agent_pane_busy`, and without this the launch dies on a
        # perfectly healthy machine — a flaky launch, which is the whole class this module exists
        # to remove. Retried against THE SAME pane, because that is what has to settle; re-creating
        # the workspace would just produce another shell at exactly the same stage.
        #
        # ONLY that refusal waits. Every other one is a real failure to report at once, because a
        # retried diagnosis is a slow diagnosis.
        for attempt in range(max(1, _START_RETRIES)):
            if attempt:
                self._probe.sleep(_START_RETRY_PAUSE)
            reply = self._call(start, timeout=int(start_timeout_ms) / 1000.0 + _CALL_SECONDS)
            if reply.ok or not _PANE_BUSY.search((reply.error or "").lower()):
                break
        if not reply.ok:
            self._rollback(workspace)
            raise SpawnRefused("[%s] the agent did not start: %s" % (name, reply.error))

        # ---- the post-spawn confirmation. The host reporting "started" is not a process existing.
        detail = "no confirmation was attempted"
        for attempt in range(max(1, self._confirm_reads)):
            if attempt:
                self._probe.sleep(self._confirm_pause)
            got = self._call(["agent", "get", name])
            if not got.ok:
                detail = "the host no longer resolves the name: %s" % got.error
                continue
            live_pane = _dig(got.payload, "agent", "pane_id") or pane
            shell_pid, liveness, detail = self._process_facts(live_pane)
            if liveness == ALIVE:
                return Session(name=name, workspace=workspace, tab=tab, pane=live_pane,
                               shell_pid=shell_pid, owned=True)
        self._rollback(workspace)
        raise SpawnRefused("[%s] the session was started but no process is behind it — %s"
                           % (name, detail))

    # ---- verb 2: send ------------------------------------------------------------------
    def send(self, session, text, *, delivery, revived=False, timeout_ms=None):
        """Deliver one prompt, with ``--wait``, and prove it landed — or raise.

        ``delivery`` is required and has no default: a send whose delivery nobody can check is the
        exact operation that lied 6/6 times, so the code path for it does not exist.

        A prompt the host refuses with its not-found code is NOT reported until the wrapper has
        asked whether the host is mid-restore (#317) — see ``_wait_out_restore``. The three ways
        out of that wait are the three honest answers: the window closes and the prompt goes
        (``Sent.restored``), the signature breaks and the ordinary channel-fault raise follows, or
        the bound expires and ``RestoreWindowExpired`` says so in as many words.
        """
        name = _validate(_name_of(session))
        if delivery is None:
            raise ValueError("a delivery oracle is required: rc carries no delivery information, "
                             "so an unverifiable send is not a send")
        if revived and not (text or "").lstrip().startswith(reorient.HEADING):
            # #298: `--resume` restores the conversation and tells the session NOTHING about what
            # happened to the world. An instruction delivered before the re-orientation is acted on
            # under a remembered branch, PR and CI — so the test is LEADS WITH, not contains:
            # reorient.render puts the operator's note last for exactly this reason, and a payload
            # that opens with the instruction defeats the ordering however much preamble follows.
            raise ReorientationMissing(
                "[%s] a revived session must be handed the re-orientation preamble BEFORE any "
                "instruction — the payload has to lead with reorient.render's output" % name)

        ms = int(timeout_ms or self._prompt_timeout_ms)
        mark = delivery.mark()
        # THE one prompt argv in this codebase. `--wait` is not a parameter and never becomes one.
        argv = ["agent", "prompt", name, text, "--wait", "--timeout", str(ms)]
        reply = self._call(argv, timeout=ms / 1000.0 + _CALL_SECONDS)
        landed = delivery.landed(mark)
        restored, waited = False, 0.0
        if landed is not True and _is_gone(reply):
            # THE RESTORE WINDOW (#317). `agent_not_found` is ambiguous by itself, so before it can
            # be reported as anything the wrapper asks whether the host is mid-restore — and if it
            # is, waits it out instead of handing the caller a verdict that reads "gone".
            reply, landed, restored, waited = self._wait_out_restore(
                name, argv, ms, delivery, mark, reply, landed,
                recorded=session.shell_pid if isinstance(session, Session) else None)
        if landed is not True and not reply.ok:
            # THE HOST REFUSED THE CALL — an unresolvable name, a timeout, a missing binary, or its
            # own `agent_prompt_stalled`. That is a CHANNEL fault and it must not wear the delivery
            # oracle's costume: `DeliveryUnproven` says "we typed and could not confirm it", and a
            # caller with a one-shot key (the gate's single handback) spends that key and then parks
            # the lane with a memo blaming a worker for ignoring a message the host never carried.
            # A refused call is exactly what a caller should RETRY, so it raises the base error.
            #
            # Ordered before the revive chaser deliberately: chasing Enter into a pane whose prompt
            # call was refused presses Enter for no reason, and a stray Enter SELECTS.
            raise HostError("[%s] the host refused the prompt (rc=%s%s) — nothing was carried to "
                            "the session, so this is a channel fault rather than an unproven "
                            "delivery" % (name, reply.rc,
                                          "; " + reply.error if reply.error else ""))
        chased = False
        if landed is not True and (revived or restored):
            # 5/5 in the spikes: a post-revive first prompt lands in the composer unsubmitted even
            # with `--wait`, and `send-keys enter` submits it. Scoped to the revive path on
            # purpose — a stray Enter into a pane showing a selection dialog SELECTS an item.
            #
            # `restored` counts as revived, and missing that was the review's first P0. A prompt
            # that waited out a restore window IS a post-`--resume` first prompt — herdr did the
            # reviving instead of us, which changes who typed `--resume` and nothing about the pane
            # it produced. Without this the feature's own success path lands in the composer
            # unsubmitted, raises DeliveryUnproven, and spends the caller's one-shot nudge on a
            # session that was working: the exact harm RestoreWindowExpired was written to avoid.
            self._call(["agent", "send-keys", name, "enter"])
            chased = True
            landed = delivery.landed(mark)
        if landed is not True:
            raise DeliveryUnproven(
                "[%s] nothing proved this prompt was delivered (host rc=%s%s%s). rc is not "
                "evidence: even a settled rc=0 means queued." % (
                    name, reply.rc, "; " + reply.error if reply.error else "",
                    "; the enter chaser did not help" if chased else ""))
        return Sent(delivered=True, chased=chased, rc=reply.rc,
                    advisory=_dig(reply.payload or {}, "agent", "agent_status"),
                    restored=restored, waited_seconds=waited,
                    detail="delivery confirmed by the caller's oracle, not by rc" + (
                        "" if not restored else
                        "; the host was mid-restore and this prompt waited %.1fs for its window to "
                        "close, so the session on the other end is a REVIVED one" % waited))

    # ---- verb 3: state -----------------------------------------------------------------
    def state(self, session, hooks=None):
        """What is true about this session: hooks primary, process facts for liveness, host
        advisory. Never raises — a state read that raised would take the runner down with the
        host it was asking about."""
        name = _name_of(session)
        recorded = session.shell_pid if isinstance(session, Session) else None
        cached_pane = session.pane if isinstance(session, Session) else None

        got = self._call(["agent", "get", name])
        if got.ok:
            advisory = _dig(got.payload, "agent", "agent_status")
            # FRESH pane, every read. The cached one is where the dragged-anchor class comes from.
            pane = _dig(got.payload, "agent", "pane_id") or cached_pane
            shell_pid, liveness, detail = self._process_facts(pane)
            return HostState(name=name, liveness=liveness, pane=pane,
                             shell_pid=shell_pid or recorded, name_resolves=True,
                             advisory=advisory if isinstance(advisory, str) else None,
                             hooks=hooks, detail=detail)

        # The name did not resolve. That is a real signal when the host SPOKE (names clear when the
        # agent exits) and no signal at all when we could not reach it.
        resolves = False if got.spoke else None
        if recorded is None:
            return HostState(name=name, liveness=UNKNOWN, pane=cached_pane, name_resolves=resolves,
                             hooks=hooks,
                             detail="the host does not resolve the name (%s) and no pid was "
                                    "recorded for this session" % got.error)
        if not self._probe.pid_alive(recorded):
            return HostState(name=name, liveness=DEAD, pane=cached_pane, shell_pid=recorded,
                             name_resolves=resolves, hooks=hooks,
                             detail="the host does not resolve the name and the recorded pid %s "
                                    "is gone" % recorded)
        # A live recorded pid is NOT proof this is still our process: pids get reused. Unknown.
        return HostState(name=name, liveness=UNKNOWN, pane=cached_pane, shell_pid=recorded,
                         name_resolves=resolves, hooks=hooks,
                         detail="the host does not resolve the name (%s) but the recorded pid %s "
                                "is still alive — a pid can be reused, so this is unknown, not "
                                "alive" % (got.error, recorded))

    # ---- verb 4: exit ------------------------------------------------------------------
    def exit(self, session):
        """Close a FINISHED session's window. Ordered: prove it is finished, close, prove it went.

        Positive allowlist, exactly like ``tidy.closable``: only a state we can NAME as finished is
        closable. A live session is the owner's to inspect (#168) and an unknown one might be one.
        """
        self._require_ours(session)
        before = self.state(session)
        if before.liveness != DEAD:
            # RESTORING lands here too, and that is the whole point of giving it a word (#317): the
            # window's process facts are unreadable, and an allowlist that closed on "not readable"
            # would close the window of a session the host was seconds from bringing back.
            raise TeardownRefused(
                "[%s] exit closes a finished session; this one reads %s (%s). %s" % (
                    before.name, before.liveness, before.detail,
                    "Wait for the host's restore to finish — it is bringing this session back."
                    if before.liveness == RESTORING
                    else "Use kill to end a session that is still running."))

        # ONE dead reading is not enough to destroy a window (#317, second fresh-agent review).
        # The restore's second half — pane shell respawned, agent not yet attached — presents as a
        # bare shell, which is DEAD by every rule in this module, and it lasted ~0.7s in the log
        # readings. A close issued inside it takes down a session the host is actively bringing
        # back. So the verdict has to HOLD across a pause longer than that gap. A genuinely
        # finished session reads DEAD both times and pays two seconds; a restoring one does not
        # read DEAD twice, and keeps its window.
        #
        # WHAT THIS DOES NOT DO, measured rather than assumed (third verification pass): it does
        # not save the session from its CALLERS. Both production call sites answer a
        # `TeardownRefused` with `kill`, which closes the workspace unconditionally and by design —
        # so the refusal below is a signal, not a save. Teaching a caller to tell a restore-shaped
        # refusal from a still-alive one is caller policy and issue #356; the margin here is also a
        # stability heuristic rather than a guarantee, since only the SIZE of that gap was measured
        # and not its frequency.
        self._probe.sleep(_EXIT_CONFIRM_PAUSE)
        again = self.state(session)
        if again.liveness != DEAD:
            raise TeardownRefused(
                "[%s] the dead reading did not hold: a re-read %.1fs later says %s (%s). A bare "
                "shell is also what a restore looks like part-way through, so exit refuses "
                "anything that cannot say DEAD twice."
                % (again.name, _EXIT_CONFIRM_PAUSE, again.liveness, again.detail))
        close = self._call(["workspace", "close", session.workspace])
        for attempt in range(max(1, self._teardown_reads)):
            if attempt:
                self._probe.sleep(self._teardown_pause)
            after = self.state(session)
            # Ask about the WORKSPACE, not just the agent. "The name no longer resolves" is nearly
            # free here — exit only ever runs on a session whose agent has already exited, and the
            # host clears names on exit — so verifying with it would confirm something that was
            # true before the close and leak an empty workspace per lane. `close.ok` is not the
            # test either: a `workspace_not_found` refusal means the window was ALREADY gone, which
            # is a teardown, while a cheerful rc=0 that changed nothing is not.
            if self._workspace_gone(session.workspace) is True and after.liveness != ALIVE:
                # Same loose end kill can be left holding: the pane shell a finished session leaves
                # behind may outlive the close. It is reported, never silently dropped.
                recorded = session.shell_pid
                orphan = (recorded if recorded is not None and self._probe.pid_alive(recorded)
                          else None)
                return Teardown(closed=True, signalled=[], orphan_pid=orphan,
                                detail="workspace %s is gone (the host no longer has it)%s"
                                       % (session.workspace,
                                          "" if orphan is None else
                                          "; the pid recorded at spawn (%s) is still alive and "
                                          "could not be attributed to this session" % orphan))
        raise TeardownUnverified(
            "[%s] the close was issued but nothing confirmed the workspace went (%s)%s"
            % (before.name, session.workspace,
               "; the host answered: " + close.error if not close.ok else ""))

    # ---- verb 5: kill ------------------------------------------------------------------
    def kill(self, session):
        """End a session that is still running, and PROVE the process is gone.

        The escalation walks ONE pid, and only one it can attribute: the live pane's shell, re-read
        while the pane still exists (after the close there is nothing left to ask). A pid recorded
        at spawn is deliberately NOT good enough on its own — if the host no longer resolves the
        name, the OS may have handed that number to anybody, and signalling it would be the
        pattern-kill hazard with extra steps. Never a name, never a pattern: the house rule exists
        because such a search can also match the owner's own live processes.
        """
        self._require_ours(session)
        before = self.state(session)
        recorded = session.shell_pid if isinstance(session, Session) else None
        # ALIVE is the only verdict state() reaches through FRESH pane process facts, so it is the
        # only one that attributes a pid to this session.
        pid = before.shell_pid if before.liveness == ALIVE else None
        close = self._call(["workspace", "close", session.workspace])

        signalled = []
        # (label, escalation) pairs rather than bare callables: a bound method is a fresh object on
        # every attribute access, so identity could not tell the two rungs apart afterwards — and
        # `signalled` is the record of how violent the teardown had to be.
        ladder = [(None, None), ("term", self._probe.terminate), ("kill", self._probe.kill)]
        for label, step in ladder:
            if step is not None:
                if pid is None:
                    break
                step(pid)
                signalled.append(label)
            for attempt in range(max(1, self._teardown_reads)):
                if attempt:
                    self._probe.sleep(self._teardown_pause)
                if self._ended(session, pid):
                    orphan = (recorded if pid is None and recorded is not None
                              and self._probe.pid_alive(recorded) else None)
                    return Teardown(closed=True, signalled=signalled, orphan_pid=orphan,
                                    detail="workspace %s is gone; %s%s" % (
                                        session.workspace,
                                        "no signal needed" if not signalled
                                        else "escalated to " + "+".join(signalled),
                                        "" if orphan is None else
                                        "; the pid recorded at spawn (%s) is still alive and could "
                                        "not be attributed to this session, so it was neither "
                                        "signalled nor counted as evidence" % orphan))
        unattributed = (pid is None and recorded is not None
                        and self._probe.pid_alive(recorded))
        raise TeardownUnverified(
            "[%s] nothing confirmed this session ended%s%s%s"
            % (before.name,
               " (escalated to %s against pid %s)" % ("+".join(signalled), pid) if signalled
               else "",
               "; the host answered: " + close.error if not close.ok else "",
               "; the pid recorded at spawn (%s) is also still alive and this read could not "
               "attribute it to this session, so it was deliberately NOT signalled — %s"
               % (recorded, before.detail) if unattributed else ""))

    # ---- private -----------------------------------------------------------------------
    def _pane_env(self, name, env):
        """The environment this spawn asks the host to set on the new pane — the fence's own half
        of the launch (#305).

        Two rules, in this order, and the order is the whole design:

        1. **Drop every ``HERDR_*`` the caller handed us.** Whatever a call site believes it is
           doing, a worker's pane does not get host-control variables from us. This runs FIRST so
           that a caller cannot smuggle anything in under its own name.
        2. **Then, for a ``d<N>`` only, ask for the token** — with the sentinel, never the secret.

        **This wrapper never handles the token at all**, and that is deliberate. These arguments
        become the argv of a `herdr` process, and a same-uid reader is served another process's
        argv while being refused its environment (both measured — reports/i305.md). So the real
        token would be readable by every worker on the machine for the duration of a debugger
        spawn. The patched host substitutes its own secret when it sees the sentinel, and ignores a
        literal value outright, so nothing a caller supplies can choose a pane's credential.

        The scrub is defense in depth, not the fence: the host injects its own ``HERDR_ENV`` /
        ``HERDR_PANE_ID`` / ``HERDR_SOCKET_PATH`` into every pane no matter what we pass here
        (observed, not assumed). A worker therefore still LEARNS where the control socket is; what
        the carried patch takes away is its ability to be served by it.
        """
        out = {k: v for k, v in (env or {}).items() if not str(k).startswith(_HOST_ENV_PREFIX)}
        if receives_token(name):
            out[API_TOKEN_ENV_VAR] = GRANT_SENTINEL
        return out

    def _call(self, args, timeout=None):
        argv = [self.binary] + [str(a) for a in args]
        return _parse(self._probe.run(argv, timeout=timeout or self._call_seconds))

    def _restoring(self, name, recorded=None, pane_was_gone=False):
        """Is the host mid-restore for this agent? ``(bool, detail, liveness)`` — issue #317.

        Two reads, in this order, and neither is ``agent list``'s status field. The host said
        ``idle``/``interactive_ready=true`` about a name with zero processes behind it for the
        whole 29-second window this was measured in; that field is the surface proven to lie here
        and nothing in this module may branch on it.

        1. **Does the host still RESOLVE the name?** This is its restore bookkeeping, not its
           opinion: names are cleared when an agent exits, so a name that still answers after the
           action surface refused it is the record `persist.restore` put back.
        2. **Is there a runtime behind the pane that record names?** Asked through the same process
           facts every liveness read here uses, so RESTORING can only be reached the one way.

        A restore has TWO halves and the second one is not pane-absent (fresh-agent review, P0).
        The host respawns the pane's shell first and attaches the agent to it after — that gap is
        the same one `_PANE_BUSY` exists for (~0.7s measured), and while it is open
        `pane process-info` ANSWERS with a bare shell, which reads DEAD. Waiting only through the
        first half would abandon the wait at the moment the restore was furthest along.

        Two independent signals separate that bare shell from a session that simply finished, and
        the FIRST is the one that works in production (second fresh-agent review, P0 — the first
        draft had only the second, and it was dead code):

        * ``pane_was_gone`` — this caller already watched the host refuse this very pane. A
          finished session cannot have produced that: its pane never went away. So a pane that was
          absent and is now a bare shell is a pane being rebuilt, and nothing else. It needs no
          persisted state, which matters because ``Session.shell_pid`` is never written to disk —
          `panes.Handle.as_session` does not carry it, so every production send arrives with
          ``recorded=None``.
        * ``recorded`` — a genuine PROCESS fact for the callers that do hold a live handle: a
          finished session still sits in the shell we recorded at spawn, while a restored pane's
          shell is a NEW pid because the whole tree died with the server.
        """
        got = self._call(["agent", "get", name])
        if not got.ok:
            if got.spoke:
                why = ("the host does not resolve the name either (%s), which is what a "
                       "genuinely-ended agent looks like — its name is cleared on exit" % got.error)
            else:
                why = ("the host did not answer at all (%s), and silence is not evidence of a "
                       "restore" % got.error)
            return False, why, UNKNOWN
        pane = _dig(got.payload, "agent", "pane_id") or _find(got.payload, "pane_id")
        shell_pid, liveness, detail = self._process_facts(pane)
        if liveness == RESTORING:
            return True, detail, liveness
        # `_pid` rather than a bare int(): `recorded` comes off a frozen dataclass nobody validates,
        # and a ValueError here would escape `send` past a caller that documents itself as never
        # raising (fresh-agent review).
        recorded = _pid(recorded)
        new_pid = recorded is not None and shell_pid is not None and int(shell_pid) != recorded
        if liveness == DEAD and (pane_was_gone or new_pid):
            because = (" and this wait already watched the host refuse that pane" if pane_was_gone
                       else " under a NEW pid (%s, not the %s recorded at spawn)"
                            % (shell_pid, recorded))
            return True, ("pane %s is a bare shell%s while the host still resolves the name — the "
                          "restore's second half, where the pane is back and its agent has not "
                          "attached yet" % (pane, because)), liveness
        return False, detail, liveness

    def _wait_out_restore(self, name, argv, ms, delivery, mark, reply, landed, recorded=None):
        """Wait out a host restore window, then re-offer the prompt. ``(reply, landed, restored,
        waited)``, or ``RestoreWindowExpired`` — issue #317.

        **Why re-issuing the prompt is safe here, and only here.** The loop runs on exactly one
        precondition: the host REFUSED the last call with its not-found code, so nothing was
        carried to the pane. The moment the host accepts a call — or refuses it in any other words
        — the loop stops, because a second `agent prompt` after an accepted one is two prompts in
        somebody's composer. That is the property ``test_a_prompt_the_host_accepted_is_never_
        re_issued_by_the_wait`` exists to keep.

        **The bound is WALL CLOCK, and the oracle is not asked while the host is refusing.** Both
        of those are the same lesson, learned at review (P0). Counting pauses is not a bound when
        something inside the loop can block: `delivery.landed` polls for six seconds of its own, so
        a pause-counted "120 second" wait ran about fourteen minutes of real time — past the
        caller's kill timeout, which replaced this verb's loud expiry with an rc=124 that reads as
        a channel fault. Asking the oracle at all was the waste: while the host is REFUSING,
        nothing was carried, so there is nothing for a transcript to have recorded. It is asked
        once, on the way out, about the answer that actually matters.
        """
        restoring, detail, liveness = self._restoring(name, recorded)
        if not restoring:
            # Not a restore. The caller's refusal stands exactly as the host gave it, and `send`
            # reports it as the channel fault it already knew how to report.
            return reply, landed, False, 0.0

        pane_was_gone = liveness == RESTORING
        started = self._probe.now()
        deadline = started + self.restore_wait_seconds
        # A poll COUNT as well as the deadline. The wall clock is primary and stays primary — but
        # `now()` is injected, and a fake that forgets to advance it turns this into an unbounded
        # spin (measured at ~340k host calls in three seconds). The count cannot be lied to.
        for _ in range(int(self.restore_wait_seconds / max(self.restore_poll_seconds, 0.001)) + 2):
            # Never sleep past the deadline: a caller-supplied poll longer than the bound would
            # otherwise overshoot it wholesale (600s of sleep for a 1s bound, measured).
            self._probe.sleep(max(0.0, min(self.restore_poll_seconds,
                                           deadline - self._probe.now())))
            # `self._call_seconds`, not the module default: a caller that shortened its own call
            # budget (nudge halves it) meant that for every call this verb makes, and the wait's
            # arithmetic has to be the caller's arithmetic or the bound is a fiction.
            reply = self._call(argv, timeout=ms / 1000.0 + self._call_seconds)
            if not _is_gone(reply):
                # The host stopped refusing. NOW the oracle is worth asking: this is the first
                # answer that could have carried anything. Whatever the host said stands, including
                # a refusal in different words, which the caller's own error path reports.
                return reply, delivery.landed(mark), True, self._probe.now() - started
            restoring, detail, liveness = self._restoring(name, recorded, pane_was_gone)
            pane_was_gone = pane_was_gone or liveness == RESTORING
            if not restoring:
                # The window closed the other way: the agent is genuinely gone. Hand back the
                # refusal so `send` raises its ordinary channel fault rather than inventing one.
                return reply, landed, False, self._probe.now() - started
            if self._probe.now() >= deadline:
                break
        raise RestoreWindowExpired(
            "[%s] the host was STILL mid-restore after %.1fs (bound %.1fs, raise it with %s): its "
            "record for this agent is back and no agent is running behind its pane. Nothing was "
            "typed — this is a park, not a dead session, and not a delivery to doubt. %s"
            % (name, self._probe.now() - started, self.restore_wait_seconds,
               RESTORE_WAIT_ENV_VAR, detail))

    def _process_facts(self, pane):
        """``(shell_pid, liveness, detail)`` for a pane — the OS's answer, not the host's.

        The host is asked only for the HANDLE (which pid hosts this pane); whether that pid is a
        living process with something running in it is read from the OS. That split is the whole
        defence against the phantom: the host's persisted state survived a restart that its
        processes did not.

        RESTORING is reached from here and nowhere else (#317). Every caller arrives having just
        had the host RESOLVE the agent's name — that is where ``pane`` came from — so a host that
        then refuses the very pane it named with its own not-found code is telling us it has the
        record and not the runtime. That conjunction is what a restore looks like, and it is what
        neither of the two "gone" cases can produce: a killed agent CLEARS its name, so the read
        that produced ``pane`` would already have failed; and a pane that outlives its agent
        ANSWERS this call with a bare shell, which is DEAD below. Both measured (reports/i317.md).
        """
        if not pane:
            return None, UNKNOWN, "no pane to read process facts from"
        reply = self._call(["pane", "process-info", "--pane", pane])
        if not reply.ok:
            if _is_gone(reply):
                return None, RESTORING, (
                    "the host names this agent's pane (%s) and then has no runtime behind it (%s) "
                    "— the signature of a restore window, not of a session that ended" % (
                        pane, reply.error))
            return None, UNKNOWN, "the host could not report %s's processes: %s" % (pane,
                                                                                    reply.error)
        shell_pid = _pid(_dig(reply.payload, "process_info", "shell_pid"))
        if shell_pid is None:
            shell_pid = _pid(_find(reply.payload, "shell_pid"))
        if shell_pid is None:
            return None, UNKNOWN, "the host reported no shell pid for %s" % pane
        if not self._probe.pid_alive(shell_pid):
            return shell_pid, DEAD, "the pane shell pid %s is gone" % shell_pid
        children = self._probe.child_pids(shell_pid)
        if children is None:
            # The probe could not answer. That is not "the pane is empty" — DEAD is what authorises
            # a teardown, so an unreadable read must never produce it.
            return shell_pid, UNKNOWN, ("could not read the children of pane shell pid %s — the "
                                        "process probe did not answer" % shell_pid)
        if not children:
            return shell_pid, DEAD, ("the pane shell pid %s has no live child — the pane is a bare "
                                     "shell, whatever the host reports" % shell_pid)
        return shell_pid, ALIVE, "pane shell pid %s has live child %s" % (shell_pid, children[0])

    def _ended(self, session, pid):
        """Is this session verifiably over? Every branch needs POSITIVE evidence.

        * An attributed pid that is gone is the strongest answer there is — but only the workspace
          being gone says the WINDOW went with it, so both are required.
        * With no pid to watch, the host positively letting go of the workspace is the evidence. A
          host that stopped answering has told us nothing, and `_workspace_gone` returns None there.

        A recorded pid that is still alive is deliberately NOT part of this predicate. It cannot be
        signalled (nothing attributes it to this session, and pid numbers are reused), and letting
        it veto would leave a legitimately-ended session unverifiable forever. It is reported as
        ``Teardown.orphan_pid`` instead — a named doubt rather than a silent one.
        """
        if pid is not None:
            # Cheapest evidence first: while the pid is alive nothing else can make this true, and
            # asking the host on every poll of the escalation ladder is pure chatter.
            if self._probe.pid_alive(pid):
                return False
            return self._workspace_gone(session.workspace) is True
        if self._workspace_gone(session.workspace) is not True:
            return False
        return self.state(session).name_resolves is False

    def _workspace_gone(self, workspace):
        """True / False / None — does the host still have this workspace?

        Only a NOT-FOUND answer means gone. Reading any structured error that way would let a
        "busy" or "refused" reply — the host saying it could not tell us — pass as a completed
        teardown, which is the same false success in a new costume. Everything else is None, and
        every caller reads None as "not verified", so the failure mode is a park, not a leak.

        ``_GONE_CODES`` is the one place the host's not-found spelling is asserted (its errors are
        ``<thing>_not_found``; the schema does not enumerate codes). If a later release renames it,
        this module refuses to verify teardowns until this line is updated — loudly, and in the
        safe direction.
        """
        if not workspace:
            return None
        reply = self._call(["workspace", "get", workspace])
        if reply.ok:
            return False
        return True if _is_gone(reply) else None

    def _rollback(self, workspace):
        """Undo a spawn that could not be confirmed. Best effort by design: the raise that follows
        is the report, and a failed rollback must not replace the diagnosis with its own error."""
        if workspace:
            self._call(["workspace", "close", workspace])

    @staticmethod
    def _require_ours(session):
        if not isinstance(session, Session) or not session.workspace:
            raise TeardownRefused("teardown needs the handle spawn returned (workspace id and all)")
        if not session.owned:
            # The host's own coordination rule, and ours: close only what you created. A borrowed
            # pane belongs to the owner's window.
            raise TeardownRefused("[%s] this workspace was not created by the loop — refusing to "
                                  "close someone else's window" % session.name)
