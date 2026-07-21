#!/usr/bin/env python3
"""The deterministic loop runner (plan Task 10): a ~15s tick that SENSES (usage, GitHub poll,
disk markers, events.py), DECIDES (actions.decide — one pure function, the only brain), and
ACTS (this file's executors, all thin I/O over the Task-6/9 machinery). No model call exists
anywhere in this process — LLM judgment is hired per-event as VISIBLE interactive sessions
through launch-session.sh, and the runner never waits on judgment to act safely.

Failure posture (the nobody-responds-for-8-hours standard, EVENT-MODEL.md):
  * a tick NEVER raises: every helper fails closed to an empty/error shape, every executor is
    guarded, and an executor error is a journaled outcome, not a crash;
  * the runner is fail-STOPPED: SIGTERM/crash leaves in-flight sessions untouched and merges
    nothing while it is down; a restart rebuilds the world from GitHub + disk (decide is
    state-driven; the event dedup set is rebuilt from events/processed + reconciled to disk);
  * a failed gh WRITE never advances local state past the truth — the action re-emits next
    tick and retries.

Executor <-> action contract: see actions.py's module docstring. Every action is journaled
with its outcome (journal.jsonl — the morning report and the ratchet read it).
"""
import json
import os
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "lib"),):
    _p = os.path.abspath(_p)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import actions
import brief
import events as events_mod
import evidence
import gate
import gh
import gitops
import journal
import loopstate
import published_view
import tidy
import usage as usage_mod
import worker_hook

TICK_SECONDS = 15
TICK_ERROR_ALERT = 4           # consecutive tick crashes (~1 min at 15 s) -> ALERT + notify. A
                               # wedged tick never reaches actions.decide, so this alarm is raised
                               # from run()'s own guard, not the decide brain (incident 2026-07-07).
# Post-wake grace (issue #42). Closing the laptop overnight suspends the runner mid-tick; on wake the
# next tick lands hours later than the ~15s cadence predicts, so every in-flight worker's activity
# and the usage meter's last-success look ancient purely from the wall-clock jump — which used to
# fire a cascade of false frozen-recovery nudges + a self-clearing usage_stale ALERT on a stranger's
# first sleeping night. A tick whose gap since the previous tick reaches WAKE_GAP_SECONDS is read as
# a wake (well above any routine tick — even a merge-update recheck maxes near RECHECK_TIMEOUT — and
# well below the smallest false-alarm window: the usage fail-open grace at 30 min, frozen at 45 min).
WAKE_GAP_SECONDS = 1200
# WAKE_GRACE_SECONDS: how long after a detected wake gap the liveness (idle/frozen) and usage_stale
# alarms stay disarmed. It PROTECTS AGAINST the resume artifact — it is the window a suspended-then-
# resumed healthy worker needs to re-stamp its activity, and the usage poller (60s cadence) needs to
# land a fresh fetch, before the alarms re-arm. A genuinely dead session or dark meter still alarms
# once it expires; short enough that a real death is delayed only minutes atop the 45-min freeze tier.
WAKE_GRACE_SECONDS = 300
# State-home format version (issue #45). The dashboard reads this state home field-by-field and
# every reader fails CLOSED to empty, so a future change to the on-disk SHAPE would silently BLANK
# the dashboard rather than error. The runner stamps this number into state/state_format.json at
# startup; a reader that doesn't recognize the version NAMES the mismatch instead of blanking. This
# is the ENGINE's declared state-home format — distinct from loopstate's issues.json schema
# `version` (a narrower, per-file number). BUMP THIS whenever a change to the state-home layout the
# dashboard reads (journal record shape, marker semantics, a state file's meaning) is not
# backward-compatible — never for an additive change an old reader tolerates.
#
# v2 (issue #146) — a DELIBERATE EXCEPTION to the rule above, made by the owner, not by this code.
# The home now carries state/gh_view.json (the runner's own GitHub view, which the dashboard renders
# as its primary truth). That addition is backward-compatible ON DISK — every v1 file is unchanged,
# so a pre-#146 dashboard misreads nothing and renders exactly as it did before. By the letter of the
# rule above, that is an additive change an old reader tolerates, and it would NOT earn a bump.
#
# Issue #146's approved DoD directs the bump anyway ("the state-format contract is updated ... so a
# stale dashboard reading a new layout fails loudly, not silently"), because the thing at stake is
# not a misread field but a false BELIEF: after #146 the owner expects this dashboard to be showing
# the runner's view, and an un-updated one silently isn't — it is still double-polling GitHub, the
# exact divergence the issue exists to end. The stamp is the only channel that reaches an old
# dashboard, so v2 is what makes that visible.
#
# The cost is real and worth naming: the card an old dashboard shows says "some readings may be
# blank", and none will be. See PR #146 — flagged for William's ruling. If he prefers the rule as
# written, revert to 1 here and to {1} in the dashboard's KNOWN_STATE_FORMATS; everything else in
# #146 works unchanged (a v1 home is already handled — the dashboard names it as `no-published-view`
# and falls back loudly).
STATE_FORMAT_VERSION = 2
USAGE_REFRESH_SECONDS = 60
# Account-auth probe cadence (issue #159 / forensics U3). The probe (`claude auth status` + the
# credential keychain mtime) bounds `claude` spawns to at most one per this many seconds while a
# spend is pending; the ~30-min capture is the durable forensic flight recorder for the auth-death
# class (i336), the ONLY on-disk record of auth state over time. The history file is bounded so it
# can never grow without limit (the #41 growth discipline).
AUTH_REFRESH_SECONDS = 60
AUTH_CAPTURE_SECONDS = 1800
AUTH_HISTORY_MAX_LINES = 2000
AUTH_HISTORY_FILENAME = "auth_history.jsonl"
GH_POLL_SECONDS = 90
JOURNAL_ROTATE_SECONDS = 6 * 3600   # how often to archive the journal's stale tail (issue #41): the
                                    # first tick rotates (migrates a pre-existing large journal),
                                    # then at most every 6h — cheap, and read()/status stay bounded
MAX_POLL_CALLS = 30            # budget cap per poll cycle (poll_ship discipline): the tail of
                               # an oversized fetch list simply waits for the next cycle
LAUNCH_TIMEOUT = 120           # launch-session.sh verifies delivery within ~30s; be generous
# launch-session.sh's DISTINCT exit code for "the worktree base branch origin/<dev_branch> does not
# exist" (issue #28) — a per-repo config fault, kept out of the systemic-anchor streak so the park
# memo can name the branch instead of the launch shim. Must match launch-session.sh's `exit 3`.
LAUNCH_BASE_MISSING_RC = 3
NUDGE_TIMEOUT = 60
# The exit interview's wake ping (issue #215). On Claude the interview PAYLOAD rides the mailbox
# (verified, zero-keystroke — #148), but a finished worker is RESTING and the Stop hook fires only
# at a turn end, so armed mail would sit unread forever. This ping is the sanctioned idle-wake
# keystroke (mailbox spike, 2026-07-15): one short line that starts a turn, at whose end the hook
# consumes the mail and blocks the stop with the interview as the continuation reason. It carries
# NO payload on purpose — rc=0 on a send was never proof of arrival (i280); the mail and its
# consumption receipt are the real channel.
EXIT_WAKE_PING = ("[superlooper] end-of-run message waiting — this line only wakes your session; "
                  "the runner's instruction arrives when this turn ends.")


class ScriptRC(int):
    """An exit code that still carries what the script SAID on its way down (issue #152).

    An int subclass on purpose, and the whole trick: `rc == 0`, `rc == LAUNCH_BASE_MISSING_RC`,
    `f"rc={rc}"` and json all keep working byte-for-byte, so every existing caller and every test
    that injects a plain int rc is untouched — while the callers that need the reason can read
    `.stderr_tail`. Readers MUST use `getattr(rc, "stderr_tail", "")`: an injected/stubbed plain int
    genuinely captured nothing, and that path must fail CLOSED to "captured: none" rather than
    inherit some other call's text (a memo naming the wrong component is the exact bug this fixes).

    Bounded at construction — captured text is caller-controlled and a raw binary in a report once
    wedged the runner outright (incident 2026-07-07).
    """
    def __new__(cls, rc, stderr=""):
        o = super().__new__(cls, rc)
        o.stderr_tail = evidence.bound(stderr)
        return o


class Outcome(str):
    """An executor's human outcome line, carrying the structured evidence behind it (issue #152).

    A str subclass for the same reason ScriptRC is an int: the journal writes `outcome` as a plain
    string and `status` renders it, so nothing downstream changes shape. The evidence rides beside
    it and is journaled as its own field. Built ONLY via Runner._failed() so a failure record can
    never be assembled without going through the schema.
    """
    def __new__(cls, text, evidence_rec=None):
        o = super().__new__(cls, text)
        o.evidence = evidence_rec
        return o
RECHECK_TIMEOUT = 600
CLOSE_TIMEOUT = 15             # bound the best-effort close of a stale session's pane (D4)
# (#149) After the pane close, how long teardown waits to OBSERVE the worker CLI actually go before
# it will prune the worktree. A closed surface does not mean a dead process: the CLI unwinds on its
# own clock, and its start-session.sh holds worker.<id>.lock until it truly exits. Bounded because a
# tick must never wedge — on timeout the prune is simply skipped and retried on a later tick, which
# is always safe (an unreclaimed worktree costs disk; a worktree pruned under a live CLI unlinks its
# cwd and kills the next hook spawn, which costs the lane its liveness/exit stamp — the D14 family).
WORKER_EXIT_TIMEOUT = 10
WORKER_EXIT_POLL = 0.25
_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"   # SL_CMUX overrides (tests)

# The Restart request marker (issue #116). A Restart request asks the LIVE runner to restart ITSELF
# in its own cmux tab: `superlooper request-restart` drops this file in the STATE HOME (never
# .superlooper/**), and the runner honors it at the safe point between ticks by re-exec'ing in place.
# It is a small JSON audit record (operator + when + source); its mere EXISTENCE is the signal — a
# present-but-corrupt body still restarts, like state/ALERT. (A local ops UI over the loop shells the
# `request-restart` command, exactly as it shells `superlooper tidy`; the engine names no such UI.)
RESTART_MARKER = "runner.restart"

# Durable owner-question protocol markers (#163). Both begin with brief._MARKER_PREFIX
# ("<!-- superlooper-"), so brief.build's amendments logic SKIPS them: the runner's own question
# comment is never mistaken for a binding owner amendment, and a marked answer is embedded once via
# the Q&A block, not twice. A plain (un-marked) owner reply carries no marker and rides the
# amendments block instead — either path reaches the relaunched worker.
QUESTION_MARKER = "<!-- superlooper-question -->"
ANSWER_MARKER = "<!-- superlooper-answer -->"


def _latest_answer(comments, owner, after_iso):
    """The owner's typed answer to the CURRENT question: the body (marker line stripped) of the
    LATEST ANSWER_MARKER comment that is BOTH (1) authored by the repo `owner` and (2) posted AFTER
    the question was (`after_iso`, the question's post time). Returns "" when there is none — a plain
    owner reply carries no marker and rides the brief's amendments block instead, and the fresh
    session still gets the question from qa_log.

    The two scopes close the trust holes a bare "last marker comment" left (fresh-agent review):
      - OWNER-only, so a stranger on this PUBLIC repo cannot post `<!-- superlooper-answer -->` and
        have it embedded as the binding answer — the same owner-only rule brief._amendments enforces.
      - AFTER the question, so a PRIOR question's still-present answer marker is never mistaken for
        the answer to THIS question (the owner may answer a second question via a plain reply).

    Fail-closed throughout: a wrong-typed comment/author/body/timestamp is skipped, and an unknown
    owner (owner=None) or absent scope trusts NO marker comment — never a raise. ISO-8601 UTC (Z)
    timestamps sort lexically == chronologically, so the `> after_iso` compare needs no parsing."""
    if not isinstance(comments, list) or not (isinstance(owner, str) and owner):
        return ""
    after = after_iso if isinstance(after_iso, str) else ""
    for c in reversed(comments):
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not (isinstance(body, str) and body.lstrip().startswith(ANSWER_MARKER)):
            continue
        author = c.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if login != owner:                     # a non-owner marker is never the answer
            continue
        created = c.get("createdAt")
        if not (isinstance(created, str) and created > after):   # must post-date THIS question
            continue
        return body.split(ANSWER_MARKER, 1)[1].strip()
    return ""


_TEMPLATES = os.path.abspath(os.path.join(_HERE, "..", "templates"))

# The conflict-resolution session's brief (§C.4 6c — the `preserve` escape). Inline: it is
# runner machinery, not repo-configurable prose. The session works IN the PR's own branch;
# every gate re-runs afterwards, so it must refresh the report AND the review evidence.
_CONFLICT_BRIEF = """\
# Resolve the merge conflict on PR #{pr} (issue #{issue_num})

This PR carries the `preserve` label, so instead of regenerating it you are hired to resolve
its conflict IN PLACE, in this worktree on branch `{branch}`.

1. `git fetch origin && git merge origin/{dev_branch}` — resolve every conflict faithfully,
   preserving BOTH the mainline's intent and this branch's intent. Never force-push; never
   rewrite history; a plain `git push` is the only push you may make.
2. Run the tests; fix what the merge broke.
3. Get a fresh-agent review of the RESOLVED diff (an agent that wrote none of it) and post its
   verdict as a PR comment BEGINNING `{review_marker}` — post it AFTER your final push, and
   replace {pin_placeholder} with the oid `git rev-parse HEAD` then prints — run it and paste the
   oid in, because a shell substitution is NOT expanded inside a single-quoted
   `gh pr comment --body` and the unexpanded text pins nothing. The gate ignores the
   pre-conflict verdict already on this PR: it reviewed a different diff.
4. Rewrite your report at {report_path} with the required sections ({report_sections}) — the
   full ship gate re-runs on this PR from scratch.

If the conflict is not mechanically resolvable without a product decision, STOP: write your
single specific question to {blocked_path} and end your turn.
"""


def _sub(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, NOT an OSError — a binary file (a PNG dropped in
        # reports/, a Finder .DS_Store) once escaped this guard and wedged every tick forever
        # (incident 2026-07-07). Fail closed to "absent", exactly as a missing file does, so the
        # scan skips it. macOS re-drops .DS_Store on any Finder browse — this must hold forever.
        return None


def _short_repr(exc, limit=500):
    """A size-bounded repr for the journal. A UnicodeDecodeError embeds the ENTIRE offending
    byte string in its repr; journaling one PNG's error grew a live journal ~47 MB -> 74 MB in
    ~40 minutes (incident 2026-07-07). Keep the head, note how much was dropped, never raise."""
    try:
        r = repr(exc)
    except Exception:
        return f"<unrepresentable {type(exc).__name__}>"
    return r if len(r) <= limit else r[:limit] + f"...<+{len(r) - limit} chars truncated>"


def _read_json(path):
    txt = _read(path)
    if txt is None:
        # _read maps BOTH "absent" and "present-but-unreadable" (binary/permission) to None. For
        # JSON safety state those must NOT collapse: an ABSENT file is None (e.g. not frozen), but
        # a PRESENT-but-unreadable one must fail CLOSED to {} so existence still counts — a binary
        # merges_frozen.json has to keep merges frozen, not silently un-freeze them (Codex review
        # 2026-07-07, the repo's fail-OPEN-on-wrong-typed-input defect class). Distinguish by
        # existence; the tiny stat race resolves either way to the safe outcome.
        return {} if os.path.exists(path) else None
    try:
        v = json.loads(txt)
    except (json.JSONDecodeError, ValueError):
        return {}                  # exists-but-unreadable: a dict so existence still counts
    return v if isinstance(v, dict) else {}


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _probe_pid(pid):
    """The pid pulse: 'alive' | 'dead' | 'unknown'. Signal 0 is the same probe start-session.sh's
    own acquire_worker uses (`kill -0`), so the runner and the shell agree on liveness.

    Three states, not two, because the two answers are not equally safe to guess (issue #151).
    'dead' AUTHORISES action — a relaunch, a prune — so it is returned ONLY on a definitive
    ProcessLookupError: this pid was probed and nothing is there. Everything else that stops the
    probe from answering — a huge int (OverflowError: os.kill's C int conversion, NOT an OSError,
    so it must be caught explicitly or it escapes into the tick), a non-int, an empty/garbage lock,
    an unexpected errno — is 'unknown': the check could not be run, which is never evidence of
    death. A corrupt lock must cost a probe, not a session.

    PermissionError means the process EXISTS and is someone else's — that is ALIVE, not dead
    (#149: reading it dead is what would prune a worktree under a running CLI).

    pid <= 0 is refused WITHOUT probing: os.kill(0, 0) signals the caller's whole process group and
    os.kill(-1, 0) every process the user owns, so both would "succeed" and read back as a live
    worker that does not exist. No worker ever has such a pid; a lock holding one is corrupt.

    This runs inside the tick, which must never raise (it would wedge the loop before the heartbeat
    stamp), so it returns a state for every input and lets nothing escape."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"                  # probed, and nothing is there: the only definitive answer
    except PermissionError:
        return "alive"                 # exists, owned by someone else
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"               # the probe could not be run — never read as death


def _pid_alive(pid):
    """True when `pid` names a live process. The bool face of _probe_pid, kept because it gates
    every worktree prune (#149) and its contract is load-bearing there: ONLY a definite 'alive'
    holds a prune off, so anything that names nobody (garbage, a huge int, None) stays False —
    a probe result, never a raise."""
    return _probe_pid(pid) == "alive"


# ------------------------- restart request marker (issue #116) -------------------------
# Shared by the Runner (which HONORS the marker) and `superlooper request-restart` (which WRITES it),
# so the two agree on the path/format in one place.

def restart_marker_path(state_dir):
    return os.path.join(os.fspath(state_dir), RESTART_MARKER)


def read_restart_request(state_dir):
    """The restart request dict, or ``None`` when the marker is ABSENT. A present-but-unparseable
    body reads as ``{}`` — existence is the signal (like state/ALERT), so a malformed marker never
    loses the button's intent. ``_read`` maps BOTH a missing file and an UNDECODABLE one to ``None``,
    so an ``os.path.exists`` check distinguishes them: a present-but-unreadable marker is still a
    request (``{}``), only a genuinely absent one is ``None``."""
    path = restart_marker_path(state_dir)
    txt = _read(path)
    if txt is None:
        return {} if os.path.exists(path) else None
    try:
        val = json.loads(txt)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def write_restart_request(state_dir, payload):
    """Atomically drop the restart marker (tmp + ``os.replace``) so the honoring runner never reads a
    half write. The tmp name is per-pid so two concurrent ``request-restart`` calls can't corrupt
    each other's tmp before the rename (fresh-agent review). Returns the path written."""
    path = restart_marker_path(state_dir)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return path


def clear_restart_request(state_dir):
    _rm(restart_marker_path(state_dir))


def live_runner_pid(state_dir):
    """The pid of the LIVE runner for this state home (its ``runner.lock`` pid, confirmed alive), or
    ``None`` — a stale pidfile (dead pid), an unparseable one, or no pidfile all read as ``None``.
    This is what the Restart button's dead-runner check rides on: no live runner ⇒ the button makes
    no attempt to launch anything, it just reports that no loop is running."""
    txt = _read(os.path.join(os.fspath(state_dir), "runner.lock"))
    if txt is None:
        return None
    try:
        pid = int(txt.strip())
    except ValueError:
        return None
    return pid if _pid_alive(pid) else None


def _has_surface_row(out):
    """True if the probe output contains a real surface row. `list-pane-surfaces` prints one row
    per surface as `[* ]surface:<n>  <title>` (we don't pass --id-format, so refs, not UUIDs). A
    resolvable pane always lists at least its own surface. Judging on this POSITIVE signal — not a
    broad 'error:' substring scan — is deliberate: surface rows carry user-controlled TAB TITLES, so
    a valid pane with a tab literally named 'Error: build log' must not false-fail the preflight."""
    for ln in out.splitlines():
        if ln.lstrip().lstrip("*").strip().startswith("surface:"):
            return True
    return False


def preflight_pane(pane, cmux=None, run=None):
    """(ok, message) for whether the runner can actually reach cmux and resolve its target pane
    from ITS OWN workspace — the hard precondition every launch depends on.

    Why this must fail HARD, not warn (finding D7): the runner launches every worker as a cmux tab
    via `new-surface --pane <SL_PANE>`, which resolves ONLY within the caller's cmux workspace. A
    detached/nohup runner loses the socket (every launch → "Broken pipe"); a runner in a DIFFERENT
    workspace than the pane can't resolve it (every launch → "Pane not found"). In the live dry-run
    a runner started without a reachable pane printed one easy-to-miss warning, then silently
    aborted every launch — each issue burned its retry cap and parked, un-recoverable by
    re-approval alone. So we probe read-only BEFORE the loop and refuse to start otherwise.

    The probe is `list-pane-surfaces --pane` — read-only, and it resolves the pane the exact same
    (caller-workspace-scoped) way `new-surface` does, so it reproduces both failure modes without
    creating a tab. cmux exits 0 EVEN for a missing pane (printing 'Error: not_found'), so success
    is judged on the OUTPUT — the presence of a real surface row (`_has_surface_row`) — never the
    unreliable exit code, and never a broad substring scan that a tab title could trip."""
    if not (isinstance(pane, str) and pane.strip()):
        return False, ("no cmux pane resolved (--pane / $SL_PANE / the current cmux tab). The "
                       "runner launches every worker as a tab in a specific cmux pane and cannot "
                       "run without one — start it inside a visible cmux tab (it targets that "
                       "tab's own pane automatically).")
    cmux = cmux or os.environ.get("SL_CMUX", _CMUX_DEFAULT)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        r = run([cmux, "list-pane-surfaces", "--pane", pane])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, (f"could not run cmux ({cmux!r}): {e}. The runner must run inside cmux, in "
                       "a visible tab in the target pane's own workspace.")
    out = ((getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")).strip()
    if getattr(r, "returncode", 1) != 0 or not _has_surface_row(out):
        first = out.splitlines()[0] if out else f"rc={getattr(r, 'returncode', '?')}, no output"
        return False, (f"cmux cannot resolve pane {pane!r} from this workspace ({first}). The "
                       "runner must start in a VISIBLE cmux tab IN the target pane's own "
                       "workspace: a detached/nohup start loses the cmux socket ('Broken pipe') "
                       "and a different workspace can't resolve the pane ('Pane not found') — "
                       "either way every launch aborts and every issue parks.")
    return True, ""


def display_asleep(run=None):
    """Is the machine's DISPLAY confirmed asleep? True = confirmed asleep, False = confirmed awake,
    None = unreadable (issue #124). Read-only, bounded, injectable for tests. Fail-open lives in the
    CALLER: decide holds launches only on an explicit True, so None/False both launch normally.

    WHY this exists: macOS does NOT schedule a fresh cmux tab's shell to boot while the display
    sleeps (the live 2026-07-13 launch killer). launch-session.sh drops the worker command in a file
    the tab's ~/.zshrc shim runs (keystroke-free, RC6), but that shim never runs because the shell
    itself is not scheduled — so the 30s delivery sentinel expires and the tab is closed as an orphan
    (exit 2): a burned launch attempt, a systemic-streak entry (#24), and an alert, every sleeping
    episode. #115's canary makes THAT self-recover, but the cleaner fix is to not ATTEMPT delivery
    into a sleeping display at all. The runner calls this once per tick (only when there is launch
    demand) and holds every fresh launch while it reads asleep, resuming automatically on wake.

    WHERE it lives: display power is an OS fact, not an agent one, so this belongs on the launcher
    side next to the cmux ANCHOR probe (preflight_pane), NOT in any agent-specific file — the agent
    boundary (CLAUDE.md) stays intact and the loop stays swappable to another coding agent.

    SIGNAL: `pmset -g systemstate` prints the IOPMSystemCapabilities bitfield as
        Current System Capabilities are: CPU Graphics Audio Network
    The `Graphics` capability is set while the display framebuffer is powered and CLEARS on display
    sleep — portable across Intel/Apple-Silicon and internal/external displays (IODisplayWrangler is
    absent on Apple Silicon, so the classic `ioreg`/`pmset -g powerstate IODisplayWrangler` probe
    returns 'Internal failure' there and cannot be used). We conclude ASLEEP only on a POSITIVE,
    unambiguous read: the capabilities line resolved AND lacks Graphics. EVERYTHING else — pmset
    missing (non-macOS), timeout, non-zero exit, no capabilities line, empty output — returns None
    (unknown). Fail-open is the safety property that matters: a false ASLEEP would wedge the WHOLE
    queue, whereas a false AWAKE merely costs one already-self-recovering #115 canary cycle."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=5))
    try:
        r = run(["pmset", "-g", "systemstate"])
    except (OSError, subprocess.TimeoutExpired):
        return None                                    # pmset absent (non-macOS) / hung -> unknown
    if getattr(r, "returncode", 1) != 0:
        return None                                    # pmset errored -> do not trust its output
    out = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    for line in out.splitlines():
        low = line.lower()
        if "system capabilities" in low:              # the positive read: caps line resolved
            return "graphics" not in low               # Graphics present -> awake; absent -> asleep
    return None                                        # no capabilities line -> unknown -> fail open


def _caller_field(caller, key):
    """One field of cmux `identify`'s caller object, fail-CLOSED to "" — a null/int/blank field
    reads as absent, never leaking a non-string anchor into a boot line or a `new-surface` target
    (guards against the project's fail-OPEN-on-wrong-TYPED defect class)."""
    val = caller.get(key) if isinstance(caller, dict) else None
    return val if isinstance(val, str) and val.strip() else ""


def detect_self_anchor(cmux=None, run=None):
    """The cmux ANCHOR identity THIS process runs in — {"pane", "workspace", "window"}, each fail-
    closed to "". `pane` is the tab's own pane (the `new-surface --pane` target every worker is born
    in); `workspace`/`window` name WHERE that pane lives, so a runner started in the WRONG cmux
    window — the 2026-07-09 focused-window misplacement — is visible from the boot line, not just an
    opaque pane UUID (issue #33). All three come from ONE `identify` call.

    cmux does NOT export a pane id into a tab's shell (only CMUX_SURFACE_ID / CMUX_WORKSPACE_ID),
    so ask cmux directly: `identify` returns a `caller` object naming the INVOKING tab's `pane_id`,
    `workspace_id`, and `window_id` (NOT `focused`, which is whatever tab is focused right now — the
    very fallback that misplaces a runner). `--id-format uuids` is required — without it `pane_id`
    comes back null. Every field is "" when cmux is unreachable, we're not inside a cmux surface (a
    detached/launchd start), or the field is absent/wrong-typed. An older cmux that omits window_id
    still yields pane + workspace; the boot line shows whichever resolve."""
    empty = {"pane": "", "workspace": "", "window": ""}
    cmux = cmux or os.environ.get("SL_CMUX", _CMUX_DEFAULT)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        r = run([cmux, "--id-format", "uuids", "identify"])
    except (OSError, subprocess.TimeoutExpired):
        return dict(empty)
    if getattr(r, "returncode", 1) != 0:
        return dict(empty)
    try:
        data = json.loads(getattr(r, "stdout", "") or "")
    except (ValueError, TypeError):
        return dict(empty)
    caller = data.get("caller") if isinstance(data, dict) else None
    return {"pane": _caller_field(caller, "pane_id"),
            "workspace": _caller_field(caller, "workspace_id"),
            "window": _caller_field(caller, "window_id")}


def detect_self_pane(cmux=None, run=None):
    """The cmux pane THIS process is running in — so `superlooper run` started inside a cmux tab
    targets that tab's OWN pane with zero configuration, and survives a machine restart that
    reassigns pane UUIDs (owner request 2026-07-06: never hardcode a pane id). Worker tabs then
    open as siblings in the runner's own pane (`new-surface --pane`), grouped and watchable — the
    same design the D7 fix requires (runner and workers share one workspace by construction). Thin
    wrapper over detect_self_anchor so the identify call + fail-closed rules live in one place."""
    return detect_self_anchor(cmux=cmux, run=run)["pane"]


class Runner:
    def __init__(self, repo, config, state_home=None, pane=None, agent="claude",
                 run_script=None, fetch_usage=None, workspace="", window="", local_clock=None):
        import config as config_lib          # sibling module; only for the state-home default
        self.repo = os.fspath(repo)
        self.config = config
        self.agent = agent if agent in ("claude", "codex") else "claude"
        # D1 (live dry-run 2026-07-03): pin every gh call to config.repo — gh otherwise infers
        # the repo from the process cwd, and the runner may be started from anywhere.
        gh.set_repo(config.get("repo") if isinstance(config, dict) else None)
        self.home = os.fspath(state_home) if state_home else str(config_lib.state_home(config))
        # Explicit override only; the CLI resolves the self-pane default (detect_self_pane) before
        # constructing the Runner. CMUX_PANE_ID is deliberately NOT read — cmux never exports it
        # (only CMUX_SURFACE_ID / CMUX_WORKSPACE_ID), so that old fallback silently never fired.
        self.pane = pane or os.environ.get("SL_PANE") or ""
        # Anchor identity of the runner's own tab (issue #33): the workspace/window the pane lives
        # in. Display + doctor use them to make a misplaced runner visible; they never gate launches
        # (the pane is the only thing new-surface needs). "" when cmux couldn't resolve them.
        self.workspace = workspace if isinstance(workspace, str) else ""
        self.window = window if isinstance(window, str) else ""
        if run_script is not None:
            self._run_script = run_script
        if fetch_usage is not None:
            self._fetch_usage = fetch_usage
        # Injectable local clock (issue #217): disk_view stamps local_date/local_hhmm — the time-of-
        # day signals decide's night-batching (#164) reads — from this. The default is the machine's
        # real wall clock; a test injects a pinned clock so a time-of-day scenario is deterministic
        # instead of inheriting the CI timezone. The production entrypoint never passes it (default
        # path is byte-identical to `time.localtime(now)`), so this seam is inert off the test bench.
        if local_clock is not None:
            self._local_clock = local_clock
        self.stop = False
        self._owns_lock = False
        # True once this process ADOPTED the singleton across a Restart re-exec (issue #116) — the
        # reborn half of a self-restart, set in acquire_singleton via the SL_RESTART_ADOPT token.
        self._reexec_adopted = False
        self._consecutive_tick_errors = 0    # reset on the first clean tick (incident 2026-07-07)
        self._tick_alert_on_disk = False     # the wedge ALERT is confirmed written (retry until so)
        self._tick_alert_notified = False    # the wedge notify+journal fired once this episode
        # DISTINCT issues whose launch failed for a DELIVERY-CHANNEL reason — the anchor, the shim, or
        # the launch machinery, never the issue's own state (issue #24, refined by #153). A per-issue
        # fault (base_missing, worktree_create_failed, ...) NEVER enters this streak: _exec_launch
        # classifies via evidence.is_channel_fault before recording. Any verified delivery clears it;
        # >= actions.SYSTEMIC_LAUNCH_FAILURE_CAP entries is a SYSTEMIC launch fault (a dead channel),
        # held once for the whole queue, never N per-issue parks. Because the streak is channel-only,
        # its FIRST entry already means the channel is down. In-memory on purpose (like
        # _consecutive_tick_errors): it is live runtime health, and a restart — the documented
        # recovery for a wedged anchor — is exactly when it should reset to a clean slate.
        self._launch_fail_ids = set()
        # The #115 canary retry clock: the wall-clock of the most recent launch-delivery FAILURE.
        # decide gates the systemic-hold canary on `now - this >= CANARY_RETRY_SECONDS`, so the first
        # probe waits a full interval after the trip and each failed canary re-spaces the next. Reset
        # to 0 on any verified delivery. In-memory like the streak — a restart re-arms from scratch.
        self._launch_fail_at = 0
        # Reclaim-hold dedup (issue #190): {iid: reason} for park-family lanes whose worktree the
        # reclaim guard REFUSED to prune because it held unsaved work. The reaper sweeps every lane
        # every tick, so a lane stuck with unsaved work would journal its refusal ~4x/min without
        # this — we journal only on a NEW (iid, reason) and drop the entry once the prune finally
        # lands (a later re-park then re-journals). In-memory like the launch streak: the durable
        # record is the journal line + the preserved worktree itself, not this dedup cache.
        self._reclaim_held = {}

        self.state = os.path.join(self.home, "state")
        self.issues_path = os.path.join(self.state, "issues.json")
        for sub in ("state/activity", "state/blocked", "state/exited", "state/awaiting",
                    "state/panes", "state/started", "state/launch_stderr", "state/events/processed",
                    "state/pending_teardown",           # (#149) prunes declined under a live CLI
                    "briefs", "reports", "answers", "worktrees", "logs"):
            os.makedirs(os.path.join(self.home, sub), exist_ok=True)
        if not os.path.exists(self.issues_path):
            loopstate.save(self.issues_path, loopstate.new_state())

        # last-known GitHub view: stale until the first successful poll proves otherwise.
        self.gh_view = {"stale": True, "consecutive_failures": 0, "closed_nums": set(),
                        "prs": {}, "issue_comments": {}}
        self._parsed_by_id = {}
        self._raw_by_id = {}
        self._last_poll = 0
        # The last SUCCESSFUL GitHub read (issue #146). Distinct from _last_poll, which is the last
        # poll ATTEMPT and advances even when the probe finds GitHub down — publishing that as the
        # data's age would date stale data by a failed read. None until the first poll lands.
        self._last_poll_ok = None
        # The previously published titles and SETTLED PRs, kept so a MERGED flight's title and its
        # PR's +N/−N/files survive its issue leaving the poll set (published_view's carry
        # discipline — the want-set stops polling a terminal issue outright). Seeded lazily from the
        # document on disk so a restarted runner doesn't blank the arrivals board it published a
        # tick ago.
        self._published_titles = None
        self._published_prs = None
        self._last_journal_rotate = 0.0     # 0 => the first tick rotates (journal bound, issue #41)
        # Wake-gap detection (issue #42): _last_tick_now is the previous tick's wall-clock (used to
        # spot a resume that landed far past the cadence); _wake_grace_until is the deadline until
        # which the liveness/usage alarms stay disarmed. Seed _last_tick_now from the durable
        # heartbeat so a runner RESTARTED after a long downtime (its workers/meter all stale) also
        # reads the gap and grants grace — not only a process suspended-then-resumed in place. The
        # in-place suspend case updates _last_tick_now each tick, so it needs no seed. 0.0 = no grace.
        self._last_tick_now = self._read_heartbeat()
        self._wake_grace_until = 0.0
        self._usage = {"last_ok": {}, "last_ok_at": None, "first_attempt_at": None,
                       "checked_at": 0}
        # Cached account-auth snapshot (issue #159): the last `claude auth status` + keychain-mtime
        # read, refreshed on a cadence and handed to decide when a spend is pending. captured_at is
        # the flight-recorder clock (0 => the first tick captures a boot-time sample).
        self._auth = {"snapshot": None, "checked_at": 0.0, "captured_at": 0.0}
        self.emitted = self._rebuild_emitted()

    def _read_heartbeat(self):
        """The last completed tick's wall-clock from the durable heartbeat file (int seconds), or
        None if it is absent/unreadable — the wake-gap baseline for a fresh process (issue #42)."""
        try:
            return float(int(_read(os.path.join(self.state, "runner.heartbeat")).strip()))
        except (AttributeError, ValueError):
            return None

    # ------------------------- singleton / signals -------------------------

    def acquire_singleton(self):
        """Pidfile singleton (state/runner.lock). True if THIS instance now owns the loop;
        False if a live runner already does. A dead holder's lock is stolen. Ownership is
        per-instance (not per-pid): a second Runner in the same process must still lose."""
        lock = os.path.join(self.state, "runner.lock")
        cur = _read(lock)
        if cur is not None:
            try:
                pid = int(cur.strip())
            except ValueError:
                pid = None
            held_by_us = pid == os.getpid() and self._owns_lock
            # Re-exec adoption (issue #116): os.execv PRESERVES the pid, so after the Restart button's
            # self-restart the lock still holds our OWN live pid — the check just below would read
            # that as "another live runner" and refuse. A one-shot env token, set by _honor_restart in
            # the PRE-exec image and equal to our post-exec pid, proves the lock is ours-by-re-exec, so
            # the reborn image adopts it IN PLACE — returning WITHOUT reopening the pidfile. Not
            # rewriting it is the point: `open(lock, "w")` truncates before it writes, and a
            # concurrent `run` reading that momentary empty file would see "no holder" and double-start
            # (fresh-agent review). Since the lock is never even briefly emptied across the exec, a
            # concurrent `run` always reads our live pid and refuses — genuinely zero window. The token
            # is consumed here so a later, unrelated acquire can never mistake a stale value for a
            # fresh re-exec.
            if pid == os.getpid() and os.environ.get("SL_RESTART_ADOPT") == str(os.getpid()):
                os.environ.pop("SL_RESTART_ADOPT", None)
                self._reexec_adopted = True
                self._owns_lock = True
                return True
            if pid is not None and _pid_alive(pid) and not held_by_us:
                return False
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        self._owns_lock = True
        return True

    def release_singleton(self):
        lock = os.path.join(self.state, "runner.lock")
        if self._owns_lock and (_read(lock) or "").strip() == str(os.getpid()):
            _rm(lock)
        self._owns_lock = False

    def _anchor_path(self):
        return os.path.join(self.state, "runner.anchor.json")

    def _write_anchor(self):
        """Record THIS live runner's launch anchor (issue #33): the pane every worker tab is born
        in, the workspace/window it lives in, and our pid. `doctor` reads this to verify a LIVE
        runner's anchor still resolves — a runner whose tab was dragged to another cmux window (the
        2026-07-09 misplacement) leaves a recorded anchor that no longer resolves. Written only while
        the singleton is held and cleared on clean exit, so a present anchor means "a runner claims
        this pane"; the pid lets a reader pair it with the pidfile and ignore a stale one. Never
        raises — the anchor is a diagnostic, never a safety gate."""
        try:
            loopstate.save(self._anchor_path(), {"pane": self.pane, "workspace": self.workspace,
                                                 "window": self.window, "pid": os.getpid()})
        except OSError:
            pass

    def _clear_anchor(self):
        """Remove the anchor on clean exit — but only if it is OURS (pid match), so a runner that
        lost the singleton and is exiting can never delete the live holder's record."""
        rec = _read_json(self._anchor_path())
        if isinstance(rec, dict) and rec.get("pid") == os.getpid():
            _rm(self._anchor_path())

    def _handle_signal(self, signum, frame):
        # Fail-stopped by design: in-flight sessions untouched, nothing merges while down.
        self.stop = True

    # ------------------------- restart button (issue #116) -------------------------

    def _restart_requested(self):
        """True when a Restart request marker has been dropped in the state home by `superlooper
        request-restart` (existence is the signal — see read_restart_request)."""
        return read_restart_request(self.state) is not None

    def _honor_restart(self):
        """Honor a Restart request: consume the marker, journal the intent (old pid), then re-exec
        THIS invocation in place so a fresh process image reloads the currently-installed engine in
        the SAME cmux tab with cleared in-memory episode state (the systemic-launch streak, the
        tick-error counter, the wake grace — all reset by construction in a new __init__). The
        singleton lock is NOT released: the reborn image (same pid, via the SL_RESTART_ADOPT token)
        adopts it, so there is no window a second runner could double-start. Returns ONLY if the exec
        itself fails (or under a test's injected _reexec): the marker is already consumed, so the loop
        simply continues on the old image rather than re-looping the restart."""
        req = read_restart_request(self.state) or {}
        # The marker binds to the pid `request-restart` saw live (fresh-agent review). If that pid is
        # NOT us, the request targeted a DIFFERENT runner that died before honoring it, and we are a
        # freshly-started replacement — it was never a request for US. Clear it and do NOT restart, so
        # a manual restart after a crash-with-pending-marker doesn't spuriously self-restart. (A
        # marker with no target_pid — a hand-written one — is honored by whoever is running.)
        target = req.get("target_pid")
        if target is not None and target != os.getpid():
            clear_restart_request(self.state)
            try:
                journal.append(self.home, {"act": "runner_restart", "phase": "stale",
                                           "target_pid": target, "our_pid": os.getpid()})
            except Exception:
                pass
            return
        clear_restart_request(self.state)                  # consume BEFORE the exec — never re-loop
        try:
            journal.append(self.home, {"act": "runner_restart", "phase": "reexec",
                                       "old_pid": os.getpid(), "request": req})
        except Exception:
            pass
        os.environ["SL_RESTART_ADOPT"] = str(os.getpid())  # the reborn image adopts the held lock
        try:
            self._reexec([sys.executable] + list(sys.argv))
        except OSError as e:
            # The exec itself failed (e.g. the interpreter path vanished mid-run): stay up on the old
            # image, drop the now-useless adopt token, and journal it. The button already reported
            # success on the REQUEST landing; a rare exec failure surfaces here + in the morning report.
            os.environ.pop("SL_RESTART_ADOPT", None)
            try:
                journal.append(self.home, {"act": "runner_restart", "phase": "reexec_failed",
                                           "error": _short_repr(e)})
            except Exception:
                pass
            return
        # os.execv does not return on success; a returning INJECTED _reexec (tests only) lands here —
        # drop the token so the test process's environment is left clean.
        os.environ.pop("SL_RESTART_ADOPT", None)

    def _reexec(self, argv):                               # injectable (tests); default replaces us
        """Replace THIS process image with a fresh one running the same invocation. ``os.execv``
        PRESERVES the pid — so the runner stays the foreground process of its own cmux tab (a NEW
        pid would orphan from the tab and the shell would fall back to its prompt) — and reloads
        every engine module from disk, which is exactly how a republished engine is picked up and the
        in-memory episode state is wiped. ``argv[0]`` is the interpreter; ``argv[1:]`` re-runs the
        exact CLI (`superlooper run --repo …`) the operator launched, so the new process re-detects
        the SAME pane from the SAME tab."""
        os.execv(argv[0], argv)

    def _stamp_state_format(self):
        """Stamp the state-home format version (issue #45) so the dashboard can HANDSHAKE on the
        shape it's about to read field-by-field: a reader that doesn't recognize the version names
        the mismatch instead of silently blanking. Written by the LIVE runner only — from run(),
        AFTER the singleton is won — so a duplicate or preflight-failing start (which constructs a
        Runner but never owns the loop) can't overwrite the running engine's stamp with its own
        version and forge a false (or hide a real) mismatch. Atomic (loopstate.save = tmp +
        os.replace) because the dashboard polls this file continuously: it must only ever observe a
        complete version dict, never a half write. Guarded so a stamp failure can never abort run()."""
        try:
            loopstate.save(os.path.join(self.state, "state_format.json"),
                           {"version": STATE_FORMAT_VERSION})
        except OSError as e:
            self._log(f"state_format stamp skipped: {_short_repr(e)}")

    # ------------------------- boot migrations (issue #160) -------------------------

    def _apply_boot_migrations(self, now=None):
        """Apply pending per-repo migrations idempotently at boot, so a migration that has merged +
        installed never sits UNAPPLIED until a human remembers to re-run `adopt`. Returns True if
        the boot may proceed to the tick loop; False if a migration could NOT be applied and the
        boot is HELD (see _hold_boot_migration) — which is what stops the loop running against an
        un-migrated repo and STORMING a failing write every tick.

        The 2026-07-13 bounce storm (~15 texts) is the exact gap this closes: the #58
        needs-william -> needs-owner label rename had merged INTO adopt, but the repo was never
        re-adopted after the republish, so every bounce label-move wrote a needs-owner that did not
        exist, failed, and re-notified every ~18s. A merged, owner-approved, already-installed
        migration step is not new code execution — the runner self-heals the merged -> applied gap.

        Two boot migrations, both idempotent:
          * state-format stamp — _stamp_state_format(), run just above in run(); always re-applied.
          * runner-managed LABEL migrations — this method: the #58 rename + creating any missing
            runner-managed label (in-progress / needs-owner / parked). This EXTENDS #108's boot
            preflight from a fail-loud refusal to a self-heal (owner ruling, issue #160), keeping
            #108's read-health discipline: a REFUSED label read SKIPS every migration and proceeds,
            so a transient boot-time gh blip can never wedge a restart (the #92 refused-vs-answered-
            empty class); the loop's own poll then marks the view stale and simply waits, and
            doctor/adopt stay the backstop until a boot whose label read lands.

        Reached ONLY by the LIVE runner (run() calls it after the singleton is won), so a duplicate
        or preflight-failing start never mutates labels — the same discipline _stamp_state_format
        documents. Never raises: a migration STEP that raises is caught and held exactly like one
        that returns failure, and an unexpected read error fails OPEN to a skip (a framework bug
        must never brick the runner out of ever starting)."""
        import labels as labels_lib
        import config as config_lib
        now = time.time() if now is None else now
        # Read the label set inside the guard AND extract ok/value DEFENSIVELY (getattr, not
        # attribute access): a wrong-TYPED read — a non-ReadHealth stub, a future adapter regression
        # returning None/a dict — must fail CLOSED to a SKIP, never raise past here. This method's
        # "never raises" contract is what protects the boot, and a read anomaly is indistinguishable
        # from a refused read: skip (the #92 refused-vs-answered-empty discipline — never wedge a
        # restart on a blip, and never read garbage as "every label missing" and mutate off it).
        try:
            health = gh.labels_health()
            ok = bool(getattr(health, "ok", False))
            value = getattr(health, "value", None)
        except Exception as e:                 # labels_health never raises by contract; belt + braces
            self._log(f"boot migration: label read errored, skipped: {_short_repr(e)}")
            return True
        if not ok:
            return True                        # refused/transient/wrong-typed read -> skip
        plan = labels_lib.label_migration_plan(value)
        if not plan:
            return True                        # already applied -> a true no-op boot
        op = config_lib.operator(self.config)
        failures = []
        for step in plan:
            label = (f"{step.get('old')}->{step.get('new')}" if step.get("kind") == "rename"
                     else step.get("name"))
            try:
                if step["kind"] == "rename":
                    ok = gh.rename_label(step["old"], step["new"])
                else:
                    spec = labels_lib.label_spec(step["name"])
                    ok = bool(spec) and gh.create_label(
                        step["name"], spec[0], spec[1].replace("{operator}", op))
            except Exception as e:             # a migration that RAISES holds, exactly like one
                ok = False                     # that returns failure — never let it escape into run()
                self._log(f"boot migration {step.get('kind')} {label} raised: {_short_repr(e)}")
            try:
                journal.append(self.home, {"act": "migration", "kind": step.get("kind"),
                                           "label": label, "ok": bool(ok)}, now)
            except Exception:
                pass
            if not ok:
                failures.append((step.get("kind"), label))
        if failures:
            self._hold_boot_migration(failures, now)
            return False
        return True

    def _hold_boot_migration(self, failures, now):
        """Hold the boot on a migration that could not be applied (issue #160): write the LEGIBLE
        systemic hold (state/ALERT — the surface `status` and the dashboard already render) and
        notify ONCE, after which run() refuses to enter the tick loop. Holding the BOOT (not
        ticking at all) is what makes 'not a per-tick storm' structural: the loop never runs, so the
        failing label write is never retried — and re-notified — every tick.

        Reuses state/ALERT on purpose: it is THE systemic-hold surface, and the reuse self-heals on
        recovery. A later restart re-applies the migration; if it lands, the loop runs and the first
        clean tick's decide reclaims ALERT from its OWN reasons — which never include a
        migration_hold code — so a recovered migration clears this hold with no extra bookkeeping.
        Every side effect is guarded: a hold must REPORT the fault, never raise."""
        reasons = sorted("migration_hold:%s:%s" % (kind or "?", label) for kind, label in failures)
        named = ", ".join(label for _kind, label in failures)
        try:
            loopstate.save(os.path.join(self.state, "ALERT"), {"reasons": reasons, "since": now})
        except Exception as e:
            self._log(f"migration hold ALERT write failed: {_short_repr(e)}")
        try:
            journal.append(self.home, {"act": "migration_hold", "reasons": reasons}, now)
        except Exception:
            pass
        try:
            import notify
            notify.send(self.config, "superlooper HELD — a repo migration could not be applied",
                        f"a pending per-repo migration failed to apply at boot ({named}); the loop "
                        "is HELD rather than running against an un-migrated repo and storming a "
                        "failing write every tick. Check gh auth / re-run `superlooper adopt` "
                        "(idempotent), then restart the runner.")
        except Exception:
            pass
        self._log(f"BOOT HELD: migration could not be applied: {named}")

    def _view_path(self):
        return os.path.join(self.state, "gh_view.json")

    def _publish_view(self, now, ist_map):
        """Write this tick's GitHub view to ``state/gh_view.json`` (issue #146) — the file the
        dashboard renders as its PRIMARY truth.

        The runner has always held this view and thrown it away each tick, which left the dashboard
        no choice but to ask GitHub the same questions on its own clock: a second poller on one
        rate-limit budget (a contributor to the 2026-07-08 storm, §1b) whose answers drifted from the
        runner's (an externally-closed issue rendered as open; a dead session rendered as launching).
        Publishing it is what collapses the two views into one.

        Shaping is the pure ``published_view.build`` (tested standalone); this only supplies the
        runner's facts and does the write. Atomic (``loopstate.save`` = tmp + ``os.replace``) because
        the dashboard polls this file every ~2s and must only ever see a whole document. Fully
        guarded — publishing is a REPORT, never a duty the loop owes itself, so any failure costs the
        document and not the tick (it runs ahead of the heartbeat stamp; a raise here would present a
        healthy loop as dead, the 2026-07-07 class). Catches Exception, not just OSError: the
        document is built from live GitHub answers, and a wrong-typed one must not wedge the loop."""
        try:
            if self._published_titles is None:      # seed both carries from disk once, post-restart
                prior = _read_json(self._view_path()) or {}
                self._published_titles = prior.get("titles") if isinstance(prior.get("titles"), dict) else {}
                self._published_prs = prior.get("prs") if isinstance(prior.get("prs"), dict) else {}
            tracked = set(ist_map) if isinstance(ist_map, dict) else set()
            # The landings this runner performed itself. `_exec_merge` records them here and never
            # back into gh_view, so loopstate is the ONLY place the merge is written down — and the
            # cached PR it merged still reads OPEN (the gate can't merge one that doesn't). Without
            # this the settled carry refuses every real landing and the cargo chip blanks a poll
            # window later. `_status_of` is hash-safe: a wrong-typed status can't raise here (#95).
            merged_ids = {iid for iid in tracked
                          if actions._status_of(ist_map.get(iid) if isinstance(ist_map.get(iid), dict)
                                                else {}) == "merged"}
            doc = published_view.build(
                self.gh_view, self._raw_by_id, tracked_ids=tracked,
                now=now, polled_at=self._last_poll_ok, carry_titles=self._published_titles,
                carry_prs=self._published_prs, merged_ids=merged_ids)
            loopstate.save(self._view_path(), doc)
            self._published_titles = doc["titles"]
            self._published_prs = doc["prs"]
        except Exception as e:
            self._log(f"view publish skipped: {_short_repr(e)}")

    def run(self, max_ticks=None, sleep=time.sleep):
        if not self.acquire_singleton():
            print("another runner is live for this state home — exiting", file=sys.stderr)
            return 1
        # Hygiene (fresh-agent review): consume any lingering re-exec adopt token so it can never be
        # inherited by a worker subprocess. acquire_singleton already pops it on the adoption path;
        # this also covers the edge where the lock was externally deleted mid-exec and the reborn
        # image took the normal-acquire path, leaving the token set.
        os.environ.pop("SL_RESTART_ADOPT", None)
        self._stamp_state_format()                     # the live runner declares its state format (#45)
        self._write_anchor()                           # record the live launch anchor for doctor (#33)
        if self._reexec_adopted:
            # The reborn half of a Restart re-exec (issue #116): record that the restart LANDED (the
            # "up" side of old pid → new), so the journal + morning report show it completed — not
            # merely that it was requested. Guarded: a diagnostic must never abort run().
            try:
                journal.append(self.home, {"act": "runner_restart", "phase": "up",
                                           "new_pid": os.getpid()})
            except Exception:
                pass
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        ticks = 0
        try:
            # Apply pending per-repo migrations before the first tick (issue #160). Inside the try
            # so a HELD boot still releases the singleton + clears the anchor via the finally below;
            # a hold refuses the tick loop rather than let it storm a failing write against an
            # un-migrated repo. run() returns 2 (a boot fault, like the pane preflight), not 0.
            if not self._apply_boot_migrations():
                print("BOOT HELD: a pending per-repo migration could not be applied — see the "
                      "ALERT and the notification. Fix it (check gh auth / re-run `superlooper "
                      "adopt`, both idempotent) and restart the runner.", file=sys.stderr)
                return 2
            while not self.stop and (max_ticks is None or ticks < max_ticks):
                try:
                    self.tick()
                except Exception as e:                 # a tick must never kill the loop
                    self._consecutive_tick_errors += 1
                    try:
                        journal.append(self.home, {"act": "tick_error", "error": _short_repr(e)})
                    except Exception:
                        pass
                    # A crashing tick is otherwise invisible: it only journals + retries the same
                    # rock forever (incident 2026-07-07: 130+ silent tick_errors, 42-min stall, no
                    # ping). Once the loop has been stuck ~1 min, raise the standard ALERT + notify.
                    # Re-enter every crashing tick from the threshold on (>=, not ==) so a transient
                    # ALERT-write failure at the threshold tick is retried, not lost (Codex review
                    # 2026-07-07); the on-disk/notified flags keep it idempotent — notify fires once.
                    # Guarded: this runs inside the "a tick never kills the loop" region.
                    if self._consecutive_tick_errors >= TICK_ERROR_ALERT:
                        try:
                            self._raise_tick_error_alert(self._consecutive_tick_errors)
                        except Exception:
                            pass
                else:
                    # A clean tick disarms the alarm and re-arms it for the next episode.
                    self._consecutive_tick_errors = 0
                    self._tick_alert_on_disk = False
                    self._tick_alert_notified = False
                # Honor a Restart request (issue #116) at the SAFE POINT between ticks — a tick has
                # just fully returned (even a crashing one is caught above), so no executor is
                # mid-flight and no worker session is touched. _honor_restart re-execs in place and
                # does not return; if the exec itself fails it returns and the loop simply continues
                # on the old image (the marker is already consumed, so it never re-loops).
                if not self.stop and self._restart_requested():
                    self._honor_restart()
                ticks += 1
                if not self.stop and (max_ticks is None or ticks < max_ticks):
                    sleep(TICK_SECONDS)
        finally:
            self.release_singleton()
            self._clear_anchor()
        return 0

    def _raise_tick_error_alert(self, count):
        """Raise the standard ALERT + notify for a wedged tick loop. Called from run()'s guard on
        EVERY crashing tick at/after the threshold, because a crashing tick never reaches
        actions.decide (the usual alert author). Idempotent per wedge episode via two flags that a
        clean tick resets: the ALERT file write is retried until it lands (a transient disk failure
        at the threshold tick must not lose the alarm — Codex review 2026-07-07); the notify +
        journal fire exactly once. It writes the SAME ALERT file decide uses, so the first clean
        tick's decide auto-clears it. Every side effect is guarded — a raise here must not kill run()."""
        now = time.time()
        reasons = [f"runner_tick_errors:{count}"]
        if not self._tick_alert_on_disk:
            try:
                loopstate.save(os.path.join(self.state, "ALERT"), {"reasons": reasons, "since": now})
                self._tick_alert_on_disk = True
            except Exception:
                pass                       # left False -> retried on the next crashing tick
        if not self._tick_alert_notified:
            try:
                journal.append(self.home, {"act": "alert", "reasons": reasons}, now)
            except Exception:
                pass
            try:
                import notify
                notify.send(self.config, "superlooper ALERT",
                            f"runner tick has failed {count}x in a row — the loop is wedged")
            except Exception:
                pass
            self._tick_alert_notified = True   # notify.send never raises; dedupe regardless

    # ------------------------- sensing -------------------------

    def _fetch_usage(self):                            # default; injectable for tests
        return usage_mod.fetch_claude_usage()

    def _local_clock(self, now):                       # default; injectable for tests (issue #217)
        """The runner's local wall clock as a struct_time, the single source disk_view stamps
        local_date/local_hhmm from. A test injects a pinned clock here so a time-of-day policy
        (decide's #164 night-batching) can be driven deterministically instead of depending on the
        machine timezone the sim happens to run under."""
        return time.localtime(now)

    def _refresh_usage(self, now):
        if self.agent == "codex":
            # Codex quota accounting is intentionally deferred in the v1 adapter. Do not let
            # Claude's usage endpoint fail-closed gate block opt-in Codex launches.
            self._usage["checked_at"] = now
            if self._usage["first_attempt_at"] is None:
                self._usage["first_attempt_at"] = now
            self._usage["last_ok"] = {
                "auth_status": "ok",
                "five_hour_pct": 0.0,
                "seven_day_pct": 0.0,
                "usage_deferred": True,
                "agent": "codex",
            }
            self._usage["last_ok_at"] = now
            return
        if now - self._usage["checked_at"] < USAGE_REFRESH_SECONDS:
            return
        self._usage["checked_at"] = now
        if self._usage["first_attempt_at"] is None:
            self._usage["first_attempt_at"] = now
        try:
            result = self._fetch_usage()
        except Exception:
            return                                     # last-good stands; staleness does the rest
        if isinstance(result, dict) and result.get("auth_status") == "ok":
            self._usage["last_ok"] = result
            self._usage["last_ok_at"] = now

    def usage_view(self):
        return {**self._usage["last_ok"],
                "last_ok_at": self._usage["last_ok_at"],
                "first_attempt_at": self._usage["first_attempt_at"]}

    # ------------------------- account auth (issue #159) -------------------------
    # A fresh launch or a recovery relaunch inherits the SAME credential keychain the runner reads;
    # if account auth is dead, the new session starts LOGGED OUT and the spend is burned (the i336
    # class the in-window `logged_out` state catches only AFTER a session is up). So the runner probes
    # `claude auth status` + the credential keychain mtime — a status read, never a metered session —
    # caches the snapshot for decide's pre-spend gate, and lays down a durable ~30-min flight recorder
    # so the auth-death class is knowable from disk next time. Both are agent-specific (the agent
    # boundary), so the whole path is Claude-only; a Codex lane's auth is out of scope.

    def _probe_auth(self):                             # default; overridden per-instance in tests
        return usage_mod.probe_auth()

    def _refresh_auth(self, now, force=False):
        """Refresh the cached auth snapshot on a cadence (bounds `claude` spawns). Self-guarded: a
        probe failure leaves the last-good snapshot and fails OPEN downstream (a dark probe never
        blocks the loop — the #46/#76 asymmetry). Claude-only."""
        if self.agent != "claude":
            return
        if (not force and self._auth["checked_at"]
                and now - self._auth["checked_at"] < AUTH_REFRESH_SECONDS):
            return
        self._auth["checked_at"] = now
        try:
            snap = self._probe_auth()
        except Exception:
            return
        if isinstance(snap, dict):
            self._auth["snapshot"] = {**snap, "checked_at": int(now)}
            try:
                loopstate.save(os.path.join(self.state, "auth_probe.json"), self._auth["snapshot"])
            except Exception:
                pass

    def auth_view(self):
        return self._auth["snapshot"]

    def _capture_auth(self, now):
        """The ~30-min auth FLIGHT RECORDER (forensics U3): append `claude auth status` + the
        credential keychain mtime to a durable append-only file, so the next in-process auth death
        (i336) is knowable from disk instead of unknowable. Forces a genuinely-timed fresh sample at
        each capture boundary; bounded (the #41 growth discipline); self-guarded (a capture failure
        never costs the tick). Claude-only."""
        if self.agent != "claude":
            return
        if self._auth["captured_at"] and now - self._auth["captured_at"] < AUTH_CAPTURE_SECONDS:
            return
        self._auth["captured_at"] = now                # mark the attempt: the next sample is ~30 min
                                                       # out even if this probe fails (never hammer)
        self._refresh_auth(now, force=True)            # a fresh, genuinely-30-min-spaced sample
        snap = self._auth["snapshot"]
        if not isinstance(snap, dict) or snap.get("checked_at") != int(now):
            return                                     # the forced probe did not land a FRESH sample
                                                       # this tick (it raised) — record NOTHING rather
                                                       # than stamp a stale reading with a fresh time
                                                       # (a flight recorder must not lie; fresh-review)
        line = json.dumps({"at": int(now), **snap}, sort_keys=True)
        try:
            path = os.path.join(self.state, AUTH_HISTORY_FILENAME)
            lines = []
            if os.path.exists(path):
                with open(path) as f:
                    lines = f.read().splitlines()
            lines.append(line)
            if len(lines) > AUTH_HISTORY_MAX_LINES:    # bound the recorder: keep the recent tail
                lines = lines[-(AUTH_HISTORY_MAX_LINES // 2):]
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
        except Exception:
            pass

    def _wants_session_start(self):
        """True when a launch OR a recovery relaunch may happen this tick — the condition under which
        decide's auth gate/alert (issue #159) can act, so the probe is fed to the view only then (an
        idle runner never shells out to `claude` and is never told its auth is dead). Demand is:
          * fresh-queue demand (_wants_launch: agent-ready, not in-progress); OR
          * ANY in-flight lane — an `in-progress`-labelled issue. This is the load-bearing breadth
            (fresh-review P1): an ORPHAN RESUME after a restart is an in-progress lane with an open PR
            and NO exited marker, and a crash relaunch and a conflict resolve are in-progress too. An
            exited marker alone (a lane whose label move hasn't landed yet) is caught as a backstop.
        Over-feeding is harmless: decide only HOLDS/ALERTS when it is actually about to spend."""
        if self._wants_launch():
            return True
        for p in self._parsed_by_id.values():
            labels = [l for l in (p.get("labels") or []) if isinstance(l, str)]
            if "in-progress" in labels:
                return True
        try:
            return any(actions._iid_num(n) is not None
                       for n in os.listdir(os.path.join(self.state, "exited")))
        except OSError:
            return False

    def _poll_github(self, now):
        if now - self._last_poll < GH_POLL_SECONDS and self._last_poll:
            return
        self._last_poll = now
        if not gh.probe():
            self.gh_view = {**self.gh_view, "stale": True,
                            "consecutive_failures": self.gh_view.get("consecutive_failures", 0) + 1}
            return
        calls = [6]                                    # probe+lists below, +2 for the dev view
        # (gh.branch_checks now reads BOTH /check-runs and /status — the full dev universe, #23)

        def budget():
            calls[0] += 1
            return calls[0] <= MAX_POLL_CALLS

        gitops.fetch(self.repo)                        # keep origin/<dev> fresh for worktree bases
        import issues as issues_lib
        raw = list(gh.ready_issues()) + list(gh.open_issues("in-progress"))
        parsed_by_id, raw_by_id = {}, {}
        for r in raw:
            p = issues_lib.parse_issue(r)
            num = p.get("num")
            if type(num) is int and num > 0 and p["id"] not in parsed_by_id:
                parsed_by_id[p["id"]] = p
                raw_by_id[p["id"]] = r if isinstance(r, dict) else {}
        closed = gh.closed_issue_nums()
        dev_checks = gh.branch_checks(self.config.get("dev_branch", "main"))

        st = self._load_state()
        ist_map = st.get("issues") if isinstance(st.get("issues"), dict) else {}
        prs, issue_comments = {}, {}
        # The fetch want-set. Two disciplines are load-bearing (issue #21 (b) — the silent-forever
        # strand): a terminal issue (merged/bounced/parked-build) is DONE being gated — decide skips
        # it — yet its report stays on disk forever, so keying "want" off report-on-disk alone grew
        # the set with every merge and starved the tail under MAX_POLL_CALLS. So:
        #   * INDEPENDENT OF MERGED HISTORY: terminal issues are excluded outright; only a PARKED
        #     INVESTIGATION is kept, at LOW priority, so a marker that landed after a park can
        #     reconcile it (never parked forever) without letting parked accumulation crowd the poll.
        #   * FINISHING FIRST: finishing work (report on disk / gating / holding / in-progress
        #     orphan) is fetched before any parked-reconcile read, so parked-issue accumulation can
        #     never starve a freshly-finished issue of its gating read.
        want = {}                                      # iid -> priority (0 finishing, 1 reconcile)
        for iid in set(ist_map) | set(parsed_by_id):
            ist = ist_map.get(iid) if isinstance(ist_map.get(iid), dict) else {}
            p = parsed_by_id.get(iid, {})
            status = actions._status_of(ist)   # hash-safe: a wrong-typed status won't raise here (#95)
            itype = (p or {}).get("type") or ist.get("type")
            if status in actions.TERMINAL_STATUSES:
                if itype == "investigate" and status == "parked":
                    want[iid] = 1                      # reconcile a late-marker park (low priority)
                continue                               # merged / bounced / parked-build: never poll
            gating = (os.path.exists(os.path.join(self.home, "reports", f"{iid}.md"))
                      or status in ("gating", "holding"))
            orphanish = "in-progress" in (p.get("labels") or [])
            if gating or orphanish:
                want[iid] = 0
        for iid in sorted(want, key=lambda x: (want[x], len(x), x)):
            ist = ist_map.get(iid) if isinstance(ist_map.get(iid), dict) else {}
            p = parsed_by_id.get(iid)
            itype = (p or {}).get("type") or ist.get("type")
            if itype == "investigate":
                if budget():
                    num = (p or {}).get("num") or actions._iid_num(iid)
                    cr = gh.issue_comments(num)
                    if cr.ok:                          # store ONLY a clean answer; a refused read
                        issue_comments[iid] = cr.comments   # is OMITTED, so decide HOLDS (never
                continue                               # parks on an unverified read) — issue #21
            branch = ist.get("branch")
            if not (isinstance(branch, str) and branch.strip()):
                if p is None:
                    continue
                branch = brief.branch_for(p)
            if not budget():
                continue
            pr_read = gh.pr_for_branch(branch)
            if not pr_read.ok:
                continue     # REFUSED lookup: OMITTED from the view, so decide HOLDs — never
                             # "no PR exists" off a rate-limited read (issue #61; the 2026-07-08
                             # storm parked finished builds inside hourly GraphQL dead zones)
            pv = pr_read.pr
            if pv.get("number") and budget():
                cr = gh.pr_comments(pv["number"])
                if cr.ok:                        # attach ONLY a clean read; a REFUSED comments read
                    pv = dict(pv)                # is OMITTED (key ABSENT), so the gate WAITs instead
                    pv["comments"] = cr.comments # of reading the fail-closed [] as "no review marker"
                                                 # and parking a reviewed build (issue #78; the
                                                 # #21/#61 refused≠empty discipline on the build path)
            prs[iid] = pv    # {} here is TRUSTWORTHY: GitHub answered "no PR on this head"

        self._parsed_by_id = parsed_by_id
        self._raw_by_id = raw_by_id
        self._last_poll_ok = now              # this read LANDED — the age the published view reports
        self.gh_view = {"stale": False, "consecutive_failures": 0, "closed_nums": closed,
                        "prs": prs, "issue_comments": issue_comments, "dev_checks": dev_checks}

    def _refresh_finishing_prs(self, ist_map):
        """Freshen the PR (+ comments) for a FINISHED issue whose cached PR view is MISSING,
        bypassing the 90s poll throttle (§ tick D3 fix). A worker that finished and opened its PR
        between two polls would otherwise be gated against a stale, pre-PR snapshot and false-parked
        on "no PR exists". Two invariants keep this from re-introducing that very false-park:
          - look up finished issues (report on disk, or gating/holding). Re-fetch is bounded to
            two cases: (a) the PR is NOT yet cached (catch a freshly-opened PR — the D3 fix), or
            (b) the PR IS cached, still OPEN, but shows NO review evidence yet — a review-marker
            comment posted AFTER the poll first cached the PR is otherwise invisible until the
            next 90s poll, and the gate can nudge THEN park within that stale window, false-parking
            completed, properly-reviewed work (D6, live 2026-07-04). Case (b) self-terminates the
            moment the marker lands or the PR leaves OPEN, so the re-fetched set stays a small
            transient (finished-but-not-yet-reviewed) even through a long merges-freeze. Any other
            cached PR is skipped — no double-fetch of what the 90s poll just fetched;
          - NEVER downgrade: only a POSITIVE find (a PR with a number) updates the view. A
            refused lookup (PrRead ok=False, issue #61) carries nothing, and even a CLEAN
            answered-empty never overwrites here — a known PR does not vanish from a --state all
            lookup, so an empty answer over a cached PR is a search-index blip, and writing it
            (every tick) would re-park completed work — the exact bug the D3 fix bought off.
            Answered-empty enters the view via the poll's from-scratch snapshot instead.
        Leaves every other view key exactly as _poll_github built it."""
        if not isinstance(ist_map, dict):
            return
        prs = dict(self.gh_view.get("prs") or {})
        changed = False
        for iid, ist in ist_map.items():
            if actions._iid_num(iid) is None:
                continue
            ist = ist if isinstance(ist, dict) else {}
            # Investigate issues never open a PR — their completion signal is the marker COMMENT,
            # freshened by _refresh_finishing_investigation_comments. Skip them here so this path
            # never spends a pointless pr_for_branch lookup on one (issue #21).
            if ist.get("type") == "investigate":
                continue
            # A terminal issue is DONE being gated (decide skips it): never refresh it, or the D6
            # re-fetch below would poll a parked-but-unreviewed OPEN PR every tick forever, as its
            # report stays on disk (cross-review C1). This keeps the whole refresh set bounded.
            status = actions._status_of(ist)   # hash-safe: a wrong-typed status won't raise here (#95)
            if status in actions.TERMINAL_STATUSES:
                continue
            finished = (os.path.exists(os.path.join(self.home, "reports", f"{iid}.md"))
                        or status in ("gating", "holding"))
            if not finished:
                continue
            cached = prs.get(iid)
            if isinstance(cached, dict) and cached.get("number"):
                # D6: keep re-fetching comments for a finished, still-OPEN PR that has no review
                # evidence yet (a late review marker must reach the gate before it nudges+parks);
                # skip once reviewed or no longer OPEN. Bounded on two sides: this OPEN-no-review
                # set self-clears when the marker lands or the PR closes, and the terminal-status
                # skip above drops an issue the moment it parks. The evidence question is the
                # DIFF-PINNED one (#154): a PR carrying only a stale gen-1 verdict still has no
                # evidence for its current head, so it stays in the re-fetch set until the worker
                # posts a verdict pinned to the code it actually rebuilt.
                if (cached.get("state") != "OPEN"
                        or gate.review_evidence_ok(self.config, cached.get("comments"),
                                                   cached.get("headRefOid"),
                                                   ist.get("review_carry"))):
                    continue
            branch = ist.get("branch")
            if not (isinstance(branch, str) and branch.strip()):
                p = self._parsed_by_id.get(iid)
                if not p:
                    continue
                branch = brief.branch_for(p)
            pv = gh.pr_for_branch(branch).pr   # a refused read has no pr by construction
            if pv.get("number"):             # POSITIVE find only — refused/empty never erases a cache entry
                pv = dict(pv)
                cr = gh.pr_comments(pv["number"])
                if cr.ok:                    # attach ONLY a clean read; a REFUSED comments read
                    pv["comments"] = cr.comments   # leaves the key ABSENT so the gate WAITs, never
                                                   # parks a reviewed build on a fail-closed empty
                                                   # (issue #78 — same discipline as the poll site)
                prs[iid] = pv
                changed = True
        if changed:
            self.gh_view = {**self.gh_view, "prs": prs}

    def _refresh_inflight_prs(self, ist_map):
        """Reconcile branch -> PR for every IN-FLIGHT lane, every tick (issue #155).

        Before this, the runner associated a branch with its PR only once an issue FINISHED, so a PR
        that CONCLUDED while its lane was still building was invisible. i328 cost the afternoon queue
        two hours on exactly that: its PR was merged out-of-band, which closed the issue ("Closes
        #328"), which dropped it from BOTH open-issue lists the poll reads — and the poll's want-set
        reaches a building lane only through its `in-progress` LABEL, which is read off those very
        lists. So the lookup stopped happening at the moment it started to matter, `pr` stayed null,
        and the lane held its slot until a human noticed.

        Keying off LOCAL STATE rather than GitHub's open lists is what closes that hole: loopstate
        still says `running` and still carries the branch stamp long after GitHub has forgotten the
        issue ever existed.

        The disciplines are _refresh_finishing_prs's, for its reasons:
          - POSITIVE-FIND only. Neither a REFUSED lookup (PrRead ok=False, issue #61) nor a clean
            answered-empty writes anything — so this path structurally CANNOT manufacture the "no PR
            exists" answer that false-parks live work. Answered-empty still enters the view through
            the poll's from-scratch snapshot, exactly as before.
          - FINISHING lanes are SKIPPED — they are _refresh_finishing_prs's, and it also buys the
            comments the gate needs. Disjoint from that sibling, so those two never double-fetch.
        NOT disjoint from the 90s poll, though: the poll's want-set reaches an in-flight lane whose
        issue is still OPEN and `in-progress`-labelled (its `orphanish` tier), and it attaches
        COMMENTS on a clean read. So on a poll tick such a lane is looked up twice — an accepted
        cost, since the poll's tier cannot cover the case this exists for (an out-of-band merge
        CLOSES the issue and drops it from the lists that tier is built from). What is NOT accepted
        is dropping the poll's paid-for comments: an absent `comments` key reads as REFUSED to the
        gate and makes it WAIT (#78), so a same-numbered cached read is carried forward rather than
        clobbered (fresh-agent cross-review).
        No comments sub-read of its own: an in-flight lane is not being gated, so the only fact
        worth buying is the PR's state — ONE lookup per building lane per tick, the issue's cost
        bound. Called only on a FRESH view (tick() gates on not-stale): an outage degrades to the
        existing wait."""
        if not isinstance(ist_map, dict):
            return
        prs = dict(self.gh_view.get("prs") or {})
        changed = False
        for iid, ist in ist_map.items():
            if actions._iid_num(iid) is None:
                continue
            ist = ist if isinstance(ist, dict) else {}
            # An investigate issue never opens a PR — its completion signal is the marker COMMENT,
            # so a pr_for_branch lookup on one is pure waste (issue #21).
            if ist.get("type") == "investigate":
                continue
            status = actions._status_of(ist)   # hash-safe: a wrong-typed status won't raise here (#95)
            if status not in actions.INFLIGHT_STATUSES:
                continue                       # terminal / ready / gating / holding: not this path's
            if os.path.exists(os.path.join(self.home, "reports", f"{iid}.md")):
                continue                       # the report landed but status has not moved yet:
                                               # finishing, so _refresh_finishing_prs owns it
            branch = ist.get("branch")
            if not (isinstance(branch, str) and branch.strip()):
                continue                       # no branch stamp yet — nothing to reconcile against
            pv = gh.pr_for_branch(branch).pr   # a refused read has no pr by construction
            if pv.get("number"):               # POSITIVE find only: refused/empty never erase a
                cached = prs.get(iid)          # cache entry, and never fabricate a PR-less answer
                if (isinstance(cached, dict) and cached.get("number") == pv["number"]
                        and "comments" in cached and "comments" not in pv):
                    # Same PR => the poll's clean comments read still belongs to it. Carry it rather
                    # than strip it (#78: an absent key means REFUSED, and the gate WAITs). Number-
                    # matched, so another PR's review evidence can never be grafted onto this one.
                    pv = {**pv, "comments": cached["comments"]}
                prs[iid] = pv
                changed = True
        if changed:
            self.gh_view = {**self.gh_view, "prs": prs}

    def _refresh_finishing_investigation_comments(self, ist_map):
        """Budget-exempt sibling of _refresh_finishing_prs, for INVESTIGATE issues (issue #21 (b)).
        The 90s poll walks its want-set under a fixed call budget in sorted order, so a finished
        investigation can be STARVED of its comment read tick after tick — and the gate then holds
        forever with no marker in view (and, before #21, false-parked on that unverified read). This
        rescue refreshes the comment thread for any FINISHING investigation (report on disk, or
        gating/holding) whose comments are MISSING from the view — starved this poll, or REFUSED
        (gh.issue_comments omits a refused read). Two invariants keep it from re-introducing the very
        false-park it defends against:
          - POSITIVE reads only: a refused/timed-out read fails closed (ok=False) and is SKIPPED, so
            it never fabricates an empty 'no marker' answer that would false-park a live investigation;
          - a cached (already-fetched) read is never re-fetched — no double work inside a poll window.
        Terminal issues are skipped (decide is done with them); parked-investigation reconciliation
        rides the 90s poll's low-priority want tier, not this every-tick path, so this set stays
        bounded to the transient finishing-but-unread investigations. Called only on a FRESH view
        (tick() gates on not-stale): a probe-DOWN outage skips it entirely, but a probe-UP partial
        refusal (a data-read rate-limit — probe uses the free `gh api rate_limit`, so it stays green)
        DOES re-issue the read each tick until it lands. That retry is bounded to K = concurrently
        finishing investigations (a small transient set) and is exactly symmetric with
        _refresh_finishing_prs's own every-tick re-fetch — a deliberate cost so a late marker or a
        recovered read reaches the gate within one tick, not one poll window."""
        if not isinstance(ist_map, dict):
            return
        comments = dict(self.gh_view.get("issue_comments") or {})
        changed = False
        for iid, ist in ist_map.items():
            if actions._iid_num(iid) is None:
                continue
            ist = ist if isinstance(ist, dict) else {}
            status = actions._status_of(ist)   # hash-safe: a wrong-typed status won't raise here (#95)
            if ist.get("type") != "investigate" or status in actions.TERMINAL_STATUSES:
                continue
            finishing = (os.path.exists(os.path.join(self.home, "reports", f"{iid}.md"))
                         or status in ("gating", "holding"))
            if not finishing or iid in comments:       # not finishing, or already a clean read
                continue
            cr = gh.issue_comments(actions._iid_num(iid))
            if cr.ok:                                  # POSITIVE read only — a refusal never overwrites
                comments[iid] = cr.comments
                changed = True
        if changed:
            self.gh_view = {**self.gh_view, "issue_comments": comments}

    def _load_state(self):
        try:
            st = loopstate.load(self.issues_path)
            return st if isinstance(st, dict) else loopstate.new_state()
        except (OSError, ValueError):
            return loopstate.new_state()

    def _scan_dir(self, *parts):
        d = os.path.join(self.home, *parts)
        out = {}
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for n in names:
            if n.startswith("."):
                # macOS metadata (.DS_Store, ._* AppleDouble) — Finder drops these on any browse
                # and never a runner marker. .DS_Store's binary B-tree once wedged every tick
                # (incident 2026-07-07); skip dotfiles BY NAME so the tolerance holds regardless
                # of a given file's byte content (a small .DS_Store can even decode as valid text).
                continue
            txt = _read(os.path.join(d, n))
            if txt is not None:              # a binary file reads as None (see _read) -> skipped too
                out[n.split(".")[0] if "." in n else n] = txt
        return out

    def _status_clocks(self):
        """{id: parsed state/status/<id>.json} — the #157 progress clock worker_hook.stamp_status
        writes on every rest. A present-but-unreadable file (empty dict from _read_json) or a
        non-.json name is skipped: an absent/garbage clock reads as "no signal", and the probe
        ladder falls back to the activity tiers there rather than inventing a signature."""
        out = {}
        d = os.path.join(self.state, "status")
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for n in names:
            if n.startswith(".") or not n.endswith(".json"):
                continue
            v = _read_json(os.path.join(d, n))
            if isinstance(v, dict) and v:
                out[n[:-len(".json")]] = v
        return out

    def _live_lock_ids(self):
        out = set()
        try:
            names = os.listdir(self.state)
        except OSError:
            return out
        for n in names:
            if n.startswith("worker.") and n.endswith(".lock"):
                pid = (_read(os.path.join(self.state, n)) or "").strip()
                if pid.isdigit() and _pid_alive(int(pid)):
                    out.add(n[len("worker."):-len(".lock")])
        return out

    def disk_view(self, now):
        st = self._load_state()
        reports = {iid: txt for iid, txt in self._scan_dir("reports").items()
                   if actions._iid_num(iid) is not None}
        frozen = _read_json(os.path.join(self.state, "merges_frozen.json"))
        if frozen == {}:
            frozen = {"reason": "merges_frozen.json unreadable"}   # existence = frozen (fail closed)
        alert = _read_json(os.path.join(self.state, "ALERT"))
        lt = self._local_clock(now)         # injectable (#217): default is time.localtime(now)
        return {
            "issues_state": st,
            "blocked": self._scan_dir("state", "blocked"),
            "reports": reports,
            "exited": self._scan_dir("state", "exited"),
            "launch_stderr": self._scan_dir("state", "launch_stderr"),   # {id: tail} for #40 memos
            "status_clocks": self._status_clocks(),      # {id: parsed status.json} — the #157 progress clock
            "acks": self._scan_dir("state", "ack"),      # {id: raw ack text} — the worker's probe reply
            "awaiting": self._scan_dir("state", "awaiting"),   # {id: marker} — long background work flagged
            "exit_receipts": self._exit_receipts(),      # {id: newest mail-consumption ts} (#148/#215)
            "frozen": frozen,
            "alert": alert,
            "live_lock_ids": self._live_lock_ids(),
            "filed_fingerprints": _read_json(os.path.join(self.state, "fix_issues.json")) or {},
            "local_date": time.strftime("%Y-%m-%d", lt),
            "local_hhmm": time.strftime("%H:%M", lt),
            "last_report_date": (_read(os.path.join(self.state, "last_morning_report")) or "").strip() or None,
        }

    def _exit_receipts(self):
        """{issue id: newest mail-consumption ts} from state/mail/<id>.consumed.<ts>[.n] — the
        worker hook's two-phase delivery receipt (#148), and THE proof the exit interview (#215)
        reads as 'the worker was handed this'. Pending mail, .claimed markers (in flight, never
        proven) and .discarded ones (blank mail) are not receipts; an unparseable name simply
        contributes nothing. INVARIANT (fresh review P2-2): the exit interview is TODAY the only
        mailbox writer, so 'newest receipt' == 'the interview was delivered'. A second mail type
        must fence receipts per-purpose (e.g. a purpose tag in the mail name) before reusing
        this scan, or its consumption would silently extend the interview's reply window."""
        d = os.path.join(self.state, "mail")
        out = {}
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for n in names:
            if n.startswith("."):
                continue                       # macOS metadata, same rule as _scan_dir
            iid, sep, tail = n.partition(".consumed.")
            ts = tail.split(".")[0] if sep else ""   # tolerate _free_name's .1/.2 suffixes
            if iid and ts.isdigit():
                out[iid] = max(out.get(iid, 0), int(ts))
        return out

    def _wants_launch(self):
        """True if the last poll left any APPROVED, not-yet-in-flight issue in the queue — the only
        condition under which the launch anchor matters. Gates the per-tick anchor re-probe (below)
        so an idle runner never shells out to cmux, and never alerts, with nothing to launch (#24).
        Mirrors the morning report's waiting-queue rule (agent-ready, not in-progress)."""
        for p in self._parsed_by_id.values():
            labels = [l for l in (p.get("labels") or []) if isinstance(l, str)]
            if "agent-ready" in labels and "in-progress" not in labels:
                return True
        return False

    def _anchor_status(self):
        """Re-validate the launch anchor — the cmux pane every worker tab is born in — as a
        {"ok", "reason"} dict for the view. The runner resolves this pane once at boot and preflight
        fails hard there; but nothing re-checked it AFTER the loop started, so a pane that died
        mid-run (the runner's tab dragged to another cmux window, ordinary human tidying) went
        undetected while every launch failed delivery and the per-issue cap walked the whole queue
        into parks (incident 2026-07-09). This runs the SAME read-only probe each tick there is
        launch demand, so a dead anchor is caught as a RUNNER-level fault (one alert, launches held)
        rather than mistaken for N per-issue launch failures. Injected/overridden in tests."""
        ok, why = preflight_pane(self.pane)
        return {"ok": ok, "reason": why}

    def _display_asleep(self):
        """Display-sleep tri-state for decide (issue #124): True = confirmed asleep, False = confirmed
        awake, None = unreadable. A thin wrapper over the module-level probe so tests can override the
        method (and conftest can neutralize the real `pmset` shell-out), exactly like _anchor_status /
        _probe_auth. Called per tick only when there is launch demand; fail-open lives in decide."""
        return display_asleep()

    # ------------------------- events -------------------------

    def _events_dir(self):
        return os.path.join(self.state, "events")

    def _rebuild_emitted(self):
        """Restart rebuild (events.py contract): token events re-latch from events/+processed/,
        then reconcile against CURRENT markers so a re-created marker still re-fires (D1).
        idle/frozen deliberately do NOT rebuild — a still-stuck session should re-alert."""
        ed = self._events_dir()
        dicts = []
        for sub in ("", "processed"):
            try:
                names = os.listdir(os.path.join(ed, sub))
            except OSError:
                continue
            for n in names:
                ev = _read_json(os.path.join(ed, sub, n))
                if ev:
                    dicts.append(ev)
        emitted = events_mod.emitted_from_events(dicts)
        ids = {k[0] for k in emitted}
        st = self._load_state()
        snaps = events_mod.snapshot(self.home, sorted(ids), st, time.time())
        marker_hashes = {}
        for s in snaps:
            marker_hashes[(s["id"], "finished")] = s["report_hash"]
            marker_hashes[(s["id"], "blocked")] = s["blocked_hash"]
        return events_mod.reconcile_emitted(emitted, marker_hashes)

    def _persist_events(self, evs, new_emitted, now):
        """Write each event durably BEFORE acting; a failed write un-latches its dedup key so
        the event re-fires next tick rather than being silently lost."""
        ed = self._events_dir()
        names = []
        for sub in ("", "processed"):
            try:
                names += os.listdir(os.path.join(ed, sub))
            except OSError:
                pass
        written = []
        for ev in evs:
            seq = events_mod.next_seq(names)
            name = f"{seq}.json"
            names.append(name)
            path = os.path.join(ed, name)
            try:
                loopstate.save(path, dict(ev, ts=now))
                written.append(path)
            except OSError:
                key = events_mod._event_key(ev)
                new_emitted.discard(key)
        return written

    def _prune_processed_events(self):
        """Bound the processed/ dir (issue #41): _persist_events + _rebuild_emitted both scan it, so
        its unbounded growth made next_seq and the restart rebuild slow with total history. Archive
        the oldest beyond events_mod.PROCESSED_CAP into processed_archive/ (never deleted). The newest
        file — the global-max seq — always stays, so next_seq keeps returning max+1 (monotonic, no
        collision with an archived seq). Best-effort: an unmovable file is left, retried next tick."""
        pdir = os.path.join(self._events_dir(), "processed")
        try:
            names = os.listdir(pdir)
        except OSError:
            return
        overflow = events_mod.processed_overflow(names)
        if not overflow:
            return
        adir = os.path.join(self._events_dir(), "processed_archive")
        try:
            os.makedirs(adir, exist_ok=True)
        except OSError:
            return
        for n in overflow:
            try:
                os.replace(os.path.join(pdir, n), os.path.join(adir, n))
            except OSError:
                pass

    def _reclaim_terminal_worktrees(self, st):
        """Bound long-run disk growth (issue #41): git worktree remove the worktrees of park-family
        terminal issues (parked / needs-william / bounced), which otherwise linger forever (only
        MERGED worktrees are auto-removed today). tidy.reclaimable_worktrees is the pure, fail-closed
        selector — it never returns an in-flight/gating/holding/ready lane, so a LIVE build is never
        touched. Safe because re-approval rebuilds from the issue on a fresh branch (the committed
        work survives on the branch ref; worktree_remove drops only the checkout).

        OFF BY DEFAULT since owner ruling 2026-07-16 (#168): `cleanup_parked_worktrees` now defaults
        FALSE, so this sweep is a no-op unless a repo explicitly opts in. The owner must be able to
        open the window of stalled work and look at the session — and this reaper closes that window
        (the #149 ordered teardown must close the pane before it can prune under the live CLI), which
        was the behavior annoying him on the work machine. So a park-family lane's window AND worktree
        now simply persist until an owner verb resolves the lane. An opt-in operator on a disk-
        constrained machine accepts the window close; the #190 dirty/unpushed guard still protects
        every prune. Best-effort: a worktree that can't be fully removed (worktree_remove -> False) is
        simply retried on a later tick — never raised."""
        if not self.config.get("cleanup_parked_worktrees", False):
            return
        issues = st.get("issues") if isinstance(st, dict) and isinstance(st.get("issues"), dict) else {}
        # `isinstance(status, str)` BEFORE the set membership: an unhashable wrong-typed status
        # ([], {}) from a corrupt issues.json must be skipped, never raise `unhashable type` on the
        # `in REAPPROVAL_STATUSES` test — this early-out must match the pure selector's own discipline
        # (a raise here is unguarded and would wedge the tick before the heartbeat stamp).
        if not any(isinstance(d, dict) and isinstance(d.get("status"), str)
                   and d.get("status") in actions.REAPPROVAL_STATUSES
                   for d in issues.values()):
            return                                     # nothing parked -> skip the listing + git entirely
        wdir = os.path.join(self.home, "worktrees")
        try:
            on_disk = [n for n in os.listdir(wdir) if os.path.isdir(os.path.join(wdir, n))]
        except OSError:
            return
        for iid in tidy.reclaimable_worktrees(issues, on_disk):
            # (#149) through the ONE ordered teardown: a park-family lane can still have a live CLI
            # idling in its worktree, and this reaper runs every tick — unlinking that cwd is the
            # D14 prune-under-a-live-CLI shape. It also clears the lane's stale pane markers, which
            # a bare worktree_remove left behind to outlive their session (D9).
            # exit_timeout=0: this loop is unbounded in N and runs every tick, so it does not add a
            # per-lane WAIT on top of the close it already pays (that close is bounded by
            # CLOSE_TIMEOUT and is a fast no-op on an already-dead surface). Disk hygiene has no
            # deadline — a lane whose CLI is still unwinding is reclaimed on the next sweep, by
            # which time the pane close issued here has landed.
            # guard_worktree=True (#190): these park-family lanes are NOT merged, so their worktree
            # may hold the worker's only copy of its output (the i153/i163 loss). Refuse to prune a
            # dirty/unpushed checkout; reclaim it once the work is committed AND pushed.
            self._teardown_session(iid, remove_worktree=True, exit_timeout=0, guard_worktree=True)

    # ------------------------- the tick -------------------------

    def tick(self, now=None):
        now = time.time() if now is None else now
        # Wake-gap grace (issue #42): a tick that lands >= WAKE_GAP_SECONDS past the previous one means
        # the machine was suspended (a healthy loop ticks every ~15s), so every worker's activity and
        # the usage meter's last-success now look ancient purely from the wall-clock jump. Open a short
        # grace so the liveness/usage alarms don't fire on the resume artifact; they re-arm (and a
        # genuinely dead session/dark meter still alarms) once it expires. Stamped BEFORE detection so
        # the same tick that sees the gap already runs under the grace.
        if self._last_tick_now is not None and now - self._last_tick_now >= WAKE_GAP_SECONDS:
            self._wake_grace_until = now + WAKE_GRACE_SECONDS
            journal.append(self.home, {"act": "wake_gap", "gap": int(now - self._last_tick_now),
                                       "grace_until": int(self._wake_grace_until)}, now)
        self._last_tick_now = now
        self._refresh_usage(now)
        self._capture_auth(now)                        # #159 auth flight recorder: a ~30-min sample, always
        try:
            self._poll_github(now)
        except Exception as e:
            self.gh_view = {**self.gh_view, "stale": True,
                            "consecutive_failures": self.gh_view.get("consecutive_failures", 0) + 1}
            journal.append(self.home, {"act": "poll_error", "error": _short_repr(e)}, now)

        disk = self.disk_view(now)
        st = disk["issues_state"]
        ist_map = st.get("issues") if isinstance(st.get("issues"), dict) else {}
        tracked = sorted((i for i in ist_map if actions._iid_num(i) is not None),
                         key=actions._iid_num)
        session = self.config.get("session") if isinstance(self.config.get("session"), dict) else {}
        snaps = events_mod.snapshot(self.home, tracked, st, now)
        evs, new_emitted = events_mod.detect_events(
            snaps, self.emitted, now,
            idle_secs=session.get("idle_seconds", events_mod.IDLE_SECONDS),
            freeze_secs=session.get("freeze_seconds", events_mod.FREEZE_SECONDS),
            wake_grace_until=self._wake_grace_until)
        written = self._persist_events(evs, new_emitted, now)
        self.emitted = new_emitted
        for ev in evs:
            journal.append(self.home, {"act": "event", "event": ev}, now)

        # A worker that finished AND opened its PR between two 90s GitHub polls would otherwise be
        # gated against a stale, pre-PR snapshot and false-parked on "no PR exists" (the offline sim
        # never hit this — fake-gh creates the PR synchronously; live async worker timing does, near-
        # deterministically for fast issues). Before deciding, refresh the PR + comments for any
        # FINISHED issue (report on disk, or gating/holding) so the gate's terminal calls never rest
        # on a >poll-window-old cache. Cheap: one gh lookup per finishing issue, only when GitHub is
        # reachable (a stale/unreachable view is left untouched — the gate then waits, never parks).
        # ...and every IN-FLIGHT lane reconciles its branch to its PR each tick (#155), so a PR that
        # CONCLUDES mid-build — an out-of-band merge or close — is absorbed instead of stalling the
        # lane: the poll cannot see one, because an out-of-band merge closes the issue and drops it
        # from the open-issue lists the want-set is built from (i328: two hours of queue).
        if not self.gh_view.get("stale"):
            self._refresh_finishing_prs(ist_map)
            self._refresh_inflight_prs(ist_map)
            self._refresh_finishing_investigation_comments(ist_map)

        # Launch-anchor liveness (issue #24): hand decide the runner-level launch-health signals it
        # can't sense itself (it is pure). The DISTINCT-failure streak always; the per-tick pane probe
        # only when there is demand to launch (so an idle runner never shells out to cmux or alerts).
        disk["launch_fail_ids"] = sorted(self._launch_fail_ids)
        disk["launch_fail_at"] = self._launch_fail_at    # the #115 canary retry clock (decide reads it)
        if self._wants_launch():
            disk["launch_anchor"] = self._anchor_status()
        # Account-auth gate (issue #159): hand decide a fresh-ish `claude auth status` + keychain
        # snapshot ONLY when a spend is pending (a launch OR a relaunch), so it holds a launch/relaunch
        # into dead auth and alerts — but an idle runner never probes or alarms. Fail-open lives in
        # decide: only a definitive `valid is False` blocks; an absent/unknown snapshot launches.
        if self._wants_session_start():
            self._refresh_auth(now)
            auth_snap = self.auth_view()
            if isinstance(auth_snap, dict):
                disk["auth_probe"] = auth_snap
            # Display-sleep launch hold (issue #124): macOS will not boot a fresh tab's shell while the
            # display sleeps, so decide holds every fresh launch AND every recovery relaunch (exited /
            # orphan-resume / conflict-resolve — all boot a fresh tab) while this reads asleep, resuming
            # on wake. Gated on _wants_session_start (launch OR relaunch), the SAME breadth as the auth
            # probe — a recovery-only tick must still see the signal. Tri-state; fail-open (only an
            # explicit True holds) lives in decide.
            disk["display_asleep"] = self._display_asleep()

        lane_state = actions.lane_state_from(st)
        acts = actions.decide(now, self.config, self.usage_view(),
                              list(self._parsed_by_id.values()), lane_state, evs, disk,
                              self.gh_view, wake_grace_until=self._wake_grace_until)
        for a in acts:
            try:
                outcome = self._execute(a, now)
            except Exception as e:
                outcome = f"executor error: {_short_repr(e)}"   # bound the repr (incident 2026-07-07)
            self._journal_outcome(a, outcome, now)

        for path in written:                           # acted on -> archive for restart rebuilds
            dest = os.path.join(self._events_dir(), "processed", os.path.basename(path))
            try:
                os.replace(path, dest)
            except OSError:
                pass

        # Long-run growth bounds (issue #41). All three archive (never delete) and are hygiene, not
        # safety — each is self-guarded so it can never crash the tick or block the heartbeat.
        if written:                                    # processed/ only grew if events were archived
            self._prune_processed_events()             # keep next_seq + rebuild scan history-independent
        self._reclaim_terminal_worktrees(st)           # opt-in only (#168): OFF by default, park-family persists
        self._drain_pending_teardowns(st)              # (#149) retry prunes declined under a live CLI
        if now - self._last_journal_rotate >= JOURNAL_ROTATE_SECONDS:
            try:
                journal.rotate(self.home, now)         # archive the journal's stale tail -> read() stays bounded
            except Exception as e:
                self._log(f"journal rotate skipped: {_short_repr(e)}")
            self._last_journal_rotate = now

        # Publish the GitHub view this tick decided against (issue #146) — the dashboard's primary
        # truth. Stamped after decide/execute so the document reflects the SAME view the tick acted
        # on: the board the owner reads and the decisions the runner made can never be two different
        # stories. Before the heartbeat, so a fresh heartbeat always has a view of its own vintage
        # behind it (the dashboard trusts the view exactly as far as the heartbeat is fresh); and
        # self-guarded, so a publish failure costs the document, never the tick.
        self._publish_view(now, ist_map)

        # Heartbeat = "a full tick completed", stamped LAST (incident 2026-07-07). It used to be
        # stamped at the TOP of the tick, so a tick that crashed part-way still read as freshly
        # alive and the dashboard's dead-man's switch never fired through a 42-min wedge. Now a
        # wedged tick lets the heartbeat go stale. Note the split: runner.lock (the pidfile) says
        # the PROCESS is up; runner.heartbeat says the loop is making PROGRESS — different signals,
        # on purpose. The external-watchdog contract in
        # plugin/skills/superlooper/references/runner-ops.md matches this.
        try:
            with open(os.path.join(self.state, "runner.heartbeat"), "w") as f:
                f.write(str(int(now)))
        except OSError:
            pass

    # ------------------------- executors -------------------------

    def _run_script(self, args, env=None, timeout=LAUNCH_TIMEOUT):   # injectable
        try:
            r = subprocess.run([str(a) for a in args], env={**os.environ, **(env or {})},
                               capture_output=True, text=True, timeout=timeout)
            self._log((r.stdout or "") + (r.stderr or ""))
            # Carry the stderr — the ONLY account of WHY (issue #152). It used to stop here, at
            # `return r.returncode`: runner.log kept the reason and every caller got a bare int, so
            # the 07-09 storm's "Pane or workspace not found" was written to a file nobody read
            # while the park memo guessed (wrongly) at the shim. The scripts diagnose themselves
            # loudly on stderr; this just stops throwing that away.
            return ScriptRC(r.returncode, r.stderr)
        except subprocess.TimeoutExpired:
            return ScriptRC(124, "")     # a hang leaves no exit reason to read — evidence says so
        except OSError as e:
            return ScriptRC(127, f"could not execute {args[0] if args else '?'}: {e}")

    def _run_cmd(self, cmd, cwd, timeout=RECHECK_TIMEOUT):           # injectable (recheck)
        try:
            r = subprocess.run(["bash", "-lc", cmd], cwd=cwd, capture_output=True,
                               text=True, timeout=timeout)
            self._log((r.stdout or "") + (r.stderr or ""))
            return r.returncode
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127

    def _log(self, text):
        if not text:
            return
        try:
            with open(os.path.join(self.home, "logs", "runner.log"), "a") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def _script_env(self, model, effort=""):
        # SL_EFFORT is empty by default; a value comes from a per-issue effort:* label or the
        # repo-wide config.models.worker_effort default (resolved in _worker_env; owner rulings
        # 2026-07-07). start-session.sh forwards it to `claude --effort` only when non-empty, so the
        # default path (no label, worker_effort null) sends no --effort at all.
        codex = self.config.get("codex") if isinstance(self.config.get("codex"), dict) else {}
        def env_bool(name, default):
            v = os.environ.get(name)
            if v is not None:
                return v
            return "1" if default else "0"
        return {"SL_RUN_ROOT": self.home, "SL_REPO": self.repo, "SL_PANE": self.pane,
                "SL_DEV_BRANCH": str(self.config.get("dev_branch", "main")),
                "SL_MODEL": str(model or ""), "SL_EFFORT": str(effort or ""),
                "SL_AGENT": self.agent,
                "SL_CODEX_DANGEROUS_BYPASS": env_bool(
                    "SL_CODEX_DANGEROUS_BYPASS", bool(codex.get("dangerous_bypass", False))),
                "SL_CODEX_BYPASS_HOOK_TRUST": env_bool(
                    "SL_CODEX_BYPASS_HOOK_TRUST", bool(codex.get("bypass_hook_trust", True))),
                "SL_CODEX_NO_ALT_SCREEN": env_bool(
                    "SL_CODEX_NO_ALT_SCREEN", bool(codex.get("no_alt_screen", True)))}

    @staticmethod
    def _clean_control(v):
        """A per-issue model/effort value read back from (possibly corrupt) issue state: a non-empty
        string, else None. A wrong-typed stamp (`["fable"]`, `{...}`, 0) must degrade to the config
        fallback, never stringify garbage into `claude --model/--effort` (Codex review 2026-07-07,
        medium) — the same fail-safe the rest of the runner applies to wrong-typed disk state."""
        return v if isinstance(v, str) and v.strip() else None

    def _worker_env(self, iid):
        """Launch env for a WORKER of `iid`, applying its per-issue model/effort override.
        Precedence: model = issue `model:*` label > config.models.worker > loader default; effort =
        issue `effort:*` label > config.models.worker_effort (repo-wide default; None -> nothing) >
        no flag. worker_effort defaults to null in the config contract, so a repo that omits it gets
        exactly today's behaviour.

        The fresh parsed view is authoritative WHEN PRESENT — it reflects William's CURRENT labels,
        so a label he removed mid-flight reverts to the default. It also REFRESHES the durable stamp
        (below) to those current labels, so the stamp can never resurrect a removed label or drop an
        added one (Codex review 2026-07-07 #1 + round-2). When the parsed cache is UNAVAILABLE (gh
        unreachable, or a cold restart before the first successful poll), a state-driven relaunch —
        recover-exited fires straight off the on-disk marker; resolve-conflict — falls back to that
        stamped value so the override survives instead of silently reverting to the default. It NEVER
        blocks the relaunch — a crashed worker must always be able to restart (bright line); the
        config default applies only when neither source has a value. The ANSWERER never comes through
        here (config-only, launched with _script_env(answerer_model))."""
        p = self._parsed_by_id.get(iid)
        if p is not None:
            model, effort = p.get("model"), p.get("effort")
            # Keep the durable fallback in lock-step with the live labels (idempotent: only writes on
            # an actual change), so an outage relaunch below never reads a stale first-launch value.
            if (self._issue_field(iid, "model"), self._issue_field(iid, "effort")) != (model, effort):
                self._update_issue(iid, {"model": model, "effort": effort})
        else:
            model = self._clean_control(self._issue_field(iid, "model"))
            effort = self._clean_control(self._issue_field(iid, "effort"))
        models = self.config.get("models") if isinstance(self.config.get("models"), dict) else {}
        if self.agent == "codex":
            return self._script_env(model or os.environ.get("SL_MODEL", ""),
                                    effort or os.environ.get("SL_EFFORT", "")
                                    or models.get("worker_effort") or "")
        return self._script_env(model or self._models()[0],
                                effort or models.get("worker_effort") or "")

    def _script(self, name):
        return os.path.join(_HERE, name)

    def _models(self):
        m = self.config.get("models") if isinstance(self.config.get("models"), dict) else {}
        # Default worker AND answerer to `opus[1m]` (owner ruling 2026-07-06): the latest Opus
        # (the `opus` alias auto-tracks it) WITH the 1M-token context window (the `[1m]` suffix
        # opts in — bare `opus` is the standard ~200K context). The answerer is the loop's
        # highest-judgment hire (resolve-vs-escalate on a blocked worker), and long worker builds
        # benefit from the large window, so both run on the strongest configuration by default.
        # Kept modular: a repo overrides either in config.models. The value flows to
        # `claude --model "$SL_MODEL"` — always double-quoted through the launch stack (%q +
        # quoted expansion), so the brackets never hit zsh glob expansion.
        return m.get("worker") or "opus[1m]", m.get("answerer") or "opus[1m]"

    @staticmethod
    def _bump(issue, key):
        """Increment a persisted counter, surviving a corrupt (wrong-typed) stored value —
        a corrupt counter resets to an honest restart instead of raising inside the executor
        and wedging the bad state in place (Codex round-1 M1)."""
        v = issue.get(key)
        issue[key] = (v if type(v) is int else 0) + 1

    def _update_issue(self, iid, fields=None, fn=None):
        def m(st):
            issue = st["issues"].setdefault(iid, loopstate.new_issue())
            if fn:
                fn(st, issue)
            if fields:
                issue.update(fields)
        return loopstate.update(self.issues_path, m)

    def _issue_field(self, iid, key, default=None):
        st = self._load_state()
        ist = st.get("issues", {}).get(iid)
        return ist.get(key, default) if isinstance(ist, dict) else default

    def _surface(self, iid):
        return (_read(os.path.join(self.state, "panes", iid)) or "").strip()

    def _worktree(self, iid):
        return os.path.join(self.home, "worktrees", iid)

    def _lock_path(self, iid):
        """The lane's worker singleton, named in ONE place — start-session.sh's `acquire_worker`
        writes it, teardown clears it, and the #169 hand-back quotes this exact path at the owner
        because deleting it is the one thing that frees a lane held by a stale lock."""
        return os.path.join(self.state, f"worker.{iid}.lock")

    def _lock_pid(self, iid):
        """The pid recorded in worker.<id>.lock, or None when there is no lock / it names no
        process. start-session.sh writes the lock atomically WITH its pid (`ln` of a fully-written
        temp) and its EXIT trap frees it, so a readable pid here means a worker process that was
        alive when it took the lock. An empty/garbage lock names nobody: None, never a veto."""
        txt = (_read(self._lock_path(iid)) or "").strip()
        try:
            pid = int(txt)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _worker_liveness(self, iid):
        """Is this lane's worker process actually alive? 'alive' | 'dead' | 'unknown' (issue #151).

        The lane's ground truth, and the reason it outranks the screen: start-session.sh holds
        worker.<id>.lock for the whole life of the CLI and frees it from its EXIT trap, so the pid
        in there is the process itself — not an inference drawn from what the pane happens to be
        rendering. i160 is the cost of the inference: an interrupted-but-open CLI rendered a screen
        nobody could classify, so the runner sat on an ambiguous rc=3 defer for 43 minutes while
        the answer ('the process is right there, alive') was one kill -0 away the whole time.

        NO lock is 'unknown', not 'dead'. The lock is written moments AFTER the process starts, so
        a launch that has not reached it yet has no lock and is very much alive — reading that as
        death would relaunch a worker on top of itself. Absence of evidence is not evidence here;
        state/exited/<id> is the deterministic signal for a session that genuinely ended."""
        return _probe_pid(self._lock_pid(iid))

    def _await_worker_exit(self, iid, pid, timeout=None):
        """Observe the worker CLI go, bounded. Returns True when it is gone (or was never there),
        False when it outlived the wait.

        The lock pid is authoritative, not state/exited/<id>: that marker can be STALE (a previous
        generation of the same id wrote one) and a stale marker read as proof is exactly how a live
        CLI gets pruned out from under. The lock, by contrast, is held for the whole process life
        and freed by start-session.sh's EXIT trap, so a dead pid means the CLI has truly unwound.

        The converse is weaker, and deliberately so: a LIVE pid does not prove OUR worker lives. A
        SIGKILLed start-session.sh never runs its trap, so its lock outlives it, and pids recycle
        (~99999 on macOS) — an unrelated process can inherit that number and hold this lane's prune
        off for its whole lifetime. We accept that asymmetry: we err toward not pruning, because
        the other error kills a lane's stamp (D14).

        Do not read that as costless. A stale lock naming a reused pid does NOT just cost disk: it
        makes _exec_reapprove/_exec_regenerate defer forever (they must not rebuild over a worktree
        they cannot clear), and decide re-emits them every tick while `reapproved_now` holds back
        the very launch whose _close_stale_session would drop the stale lock. The lane livelocks —
        and until #169 that was SILENT: journaled each tick via the executor's outcome, but uncapped
        and un-alerted, so only removing the lock by hand freed it.

        That asymmetry is now bounded rather than merely accepted (#169). The refusal here is
        unchanged — we still err toward not pruning — but each refused REBUILD is charged to
        `teardown_deferrals` (_charge_teardown_deferral), and at TEARDOWN_DEFERRAL_CAP decide hands
        the lane to the owner naming this pid and this lock path. That makes it symmetric with
        start-session.sh's acquire_worker, whose identical pid-reuse refusal was already counted
        (`launch_failures`) and already parked with a memo. Do not remove the cap on the strength of
        'it only costs disk' — it never only cost disk."""
        if not pid:
            return True                                    # no lock -> no live worker to wait for
        deadline = time.monotonic() + (WORKER_EXIT_TIMEOUT if timeout is None else timeout)
        while True:
            if not _pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(WORKER_EXIT_POLL)

    def _auto_close_merged(self):
        """May a lane that just MERGED and landed have its cmux window auto-closed and its worktree
        reclaimed? Owner ruling 2026-07-16 (#168): auto-closing a window is allowed ONLY for a
        merged-and-landed lane, and even that is gated by `auto_close_merged_windows` (default True).
        Composed (AND) with the pre-existing `cleanup_merged_worktrees` so a repo that set EITHER knob
        to keep its finished checkouts is honored; #178 tracks unifying the overlapping pair. Off, the
        merged window — and, since a prune can never run under the live CLI it would leave open (#149),
        its worktree — persists; `superlooper tidy` is the owner's explicit word to close the WINDOW
        (tidy never prunes a worktree, so the checkout then stays on disk for manual inspection)."""
        return (self.config.get("auto_close_merged_windows", True)
                and self.config.get("cleanup_merged_worktrees", True))

    def _close_pane(self, iid):
        """Close the id's recorded surface. Best-effort; rc ignored (a dead surface = a no-op)."""
        surface = self._surface(iid)
        if not surface:
            return
        ws = (_read(os.path.join(self.state, "panes", f"{iid}.ws")) or "").strip()
        args = [os.environ.get("SL_CMUX", _CMUX_DEFAULT), "close-surface", "--surface", surface]
        if ws:
            args += ["--workspace", ws]
        self._run_script(args, timeout=CLOSE_TIMEOUT)

    def _teardown_session(self, iid, remove_worktree=False, exit_timeout=None, guard_worktree=False):
        """THE one ordered teardown for every session end (issue #149). Every path that ends a
        lane's session comes through here, so the ordering below is stated once and cannot drift.

            1. close the pane          — ask the CLI to go; its EXIT trap frees worker.<id>.lock
            2. observe it actually go  — bounded (only when we intend to prune; see below)
            3. clear pane markers + the lock, together (D9: no marker outlives its session)
            4. (guarded callers only) refuse if the worktree holds unsaved work — see below
            5. only THEN prune the worktree

        `guard_worktree=True` (the disk-hygiene reclaim: the parked reaper and its declined-prune
        drain, for park-family lanes) refuses to prune a worktree that still holds the ONLY copy of
        the worker's output — uncommitted changes, or commits on no remote ref (issue #190). The
        overnight i153/i163 regression pruned exactly this: parked lanes sitting at origin/main with
        the worker's report-described harness uncommitted, deleted with the checkout. The refusal is
        journaled once per state (_hold_reclaim) and the worktree preserved; the every-tick reaper
        is the retry, so the moment the work is committed AND pushed the next sweep reclaims it. The
        session markers ARE still cleared here (the worker is verified gone by step 2 — only the
        checkout is kept, so William can recover the work). The deliberate-throwaway paths
        (regenerate/reapprove — a fresh rebuild from the issue) and the merge paths (work already on
        the mainline) pass guard_worktree=False and prune unconditionally, exactly as before: a
        conflict-regenerated worktree is usually dirty by design, and guarding it would livelock the
        rebuild it exists to enable.

        WHY THE ORDER IS THE WHOLE POINT (the D14 family, 07-15 forensics r2/U4). The agent CLI
        spawns its hooks with an EXPLICIT cwd — the worker's worktree. Prune that worktree while
        the CLI still stands in it and the next hook spawn dies in posix_spawn with ENOENT before
        the hook can run a single line: the liveness/exit stamp never lands, at the exact moment
        the lane finishes. That silence is what opened the zombie windows and the blind recovery
        cascades (four on 07-15 alone). No in-hook `cd` can defend against this — the hook is
        never spawned at all — so refusing to prune under a live CLI is the only real fix.

        The pid, not the pane, is the gate: `close-surface` returning says the surface is gone, not
        that the process is. A worktree is NEVER removed while worker.<id>.lock names a live pid.

        On timeout we return False having cleared NOTHING — deliberately. The lock is the only
        record of that live pid, so clearing it would let the very next tick read "no lock, no
        worker" and prune under the CLI anyway, reintroducing the bug one tick later. Leaving the
        lane fully intact costs a retry on a later tick and nothing else.

        `remove_worktree=False` is the D4 relaunch close: it prunes nothing, so it has nothing to
        protect and does not wait (a relaunch must not pay a stall for a pid it is about to
        supersede). That path's behavior is byte-for-byte what it was before this function existed.

        `exit_timeout=0` probes once and never sleeps — for callers with no deadline (the parked
        reaper, which sweeps EVERY parked lane on EVERY tick and so must not pay a per-lane stall;
        a live pid there simply defers its prune to the next sweep).

        A declined prune is RECORDED (state/pending_teardown/<id>) and retried by
        _drain_pending_teardowns on later ticks. That marker is load-bearing, not bookkeeping: most
        callers here settle their issue to a terminal status in the same breath, so decide never
        looks at the lane again and there is no other retry — without the marker a declined prune
        would leak its worktree, pane markers and lock FOREVER, and the D9 stale-marker bug this
        function exists to close would reopen on the timeout path.

        RETURNS False for EXACTLY ONE reason: a live worker still holds the worktree, so the lane
        is not clear. Callers read that as "not mine yet — try again later"; a rebuild that acted
        on it anyway would inherit a live session's checkout. A removal that merely FAILED (git
        prune rc, a stubborn dir) is NOT that: nothing is in the way, the lane is clear, and the
        deferral marker carries the retry — worktree_remove is best-effort by contract, and
        conflating its rc with "a worker is alive" would abort rebuilds over a git hiccup."""
        pid = self._lock_pid(iid)                          # BEFORE step 3 clears the lock
        self._close_pane(iid)
        if remove_worktree and not self._await_worker_exit(iid, pid, timeout=exit_timeout):
            self._defer_teardown(iid, pid)
            return False                                   # the ONE meaning: still held
        for p in (os.path.join(self.state, "panes", iid),
                  os.path.join(self.state, "panes", f"{iid}.ws"),
                  self._lock_path(iid)):
            _rm(p)
        # (#169) The lock is gone, so whatever refused this lane's rebuilds is provably gone with it
        # — every rung charged against it is history. Cleared HERE, at the one place that proves the
        # cause is over, and not in each rebuild's success block: a rebuild whose DEMAND merely
        # disappeared (its PR merged out of band, its conflict resolved by the merge-update) would
        # otherwise strand a partial ladder, and the next episode's very first legitimate deferral —
        # a worker taking one tick to unwind — would park a healthy lane at the cap, over a memo
        # claiming N consecutive refusals that never happened. Clearing here makes "consecutive"
        # true by construction, on every path that clears a lock.
        self._clear_teardown_deferrals(iid)
        if remove_worktree:
            # (#190) The unsaved-work guard, for the disk-hygiene reclaim only. The worker is gone
            # (step 2), so the checkout is nobody's cwd — but if it still holds the sole copy of the
            # work (dirty tree, or commits on no remote ref), pruning it here is the silent data loss
            # this guard exists to stop. Refuse: journal once, PRESERVE the worktree (its markers are
            # already cleared above), and let the every-tick reaper retry once the work is saved.
            if guard_worktree:
                block = gitops.worktree_reclaim_block(self._worktree(iid))
                if block:
                    self._hold_reclaim(iid, block)
                    return True                            # lane clear of a live worker; work kept
            # Clear the deferral only once the worktree is truly gone; a failed remove keeps the
            # marker so the drain retries the removal on a later tick.
            if gitops.worktree_remove(self.repo, self._worktree(iid)):
                _rm(os.path.join(self.state, "pending_teardown", iid))
                self._reclaim_held.pop(iid, None)          # saved & gone: a later re-park re-journals
            else:
                self._defer_teardown(iid, pid)
        return True

    def _hold_reclaim(self, iid, reason):
        """Journal a reclaim refusal — bounded to ONE line per (iid, reason) so the every-tick
        reaper never storms the log (issue #190). The record + the preserved worktree are the
        durable surface; this dedup is in-memory (a restart re-journals once, still bounded)."""
        if self._reclaim_held.get(iid) == reason:
            return
        self._reclaim_held[iid] = reason
        journal.append(self.home, {"act": "reclaim_held", "id": iid, "reason": reason})

    def _defer_teardown(self, iid, pid):
        """Record a declined prune so a later tick retries it. Fail-silent: a marker we cannot
        write costs a leaked worktree (disk), never a raise into the tick."""
        try:
            os.makedirs(os.path.join(self.state, "pending_teardown"), exist_ok=True)
            with open(os.path.join(self.state, "pending_teardown", iid), "w") as f:
                f.write(f"pid={pid} still alive at teardown\n")
        except OSError:
            pass

    def _clear_teardown_deferrals(self, iid):
        """Retire this lane's declined-prune ladder and the evidence explaining it (#169). Guarded
        so the common case costs no write: _teardown_session runs for every deferred lane on every
        tick, and an unconditional locked read-modify-write there would be a per-lane, per-tick cost
        for nothing. The stamps go with the count — evidence for a count of zero is a lie, and the
        memo that would quote it names a pid from an episode that is over.

        The guard is a TYPE test, not a truth test, and that is the whole of it. decide's `_counter`
        calls any present non-int corrupt — `null` by name — and fails closed to the park, so a
        falsy-corrupt value (None, "", False, []) would park the lane on its first tick with zero
        deferrals actually attempted. Under a `if not ...` guard THIS repair would then skip exactly
        those values, and no other path rewrites the field: the owner would clear the lock, the
        rebuild would run, and the next one would park instantly again, forever. Only an honest int
        0 (or an absent key) may be left alone."""
        v = self._issue_field(iid, "teardown_deferrals", 0)
        if type(v) is int and v == 0:
            return
        self._update_issue(iid, {"teardown_deferrals": 0, "teardown_deferral_pid": None,
                                 "teardown_deferral_lock": None})

    def _charge_teardown_deferral(self, iid):
        """Charge ONE refused rebuild to this lane's teardown-deferral ladder (issue #169), the
        bound decide caps on. The sibling of _charge_launch_failure, and for the same reason: the
        refusal itself is correct — a rebuild must not run over a worktree it could not clear — but
        an UNCOUNTED refusal cannot end. start-session.sh's own acquire_worker has the identical
        pid-reuse exposure and its refusal is counted (`launch_failures`) and eventually parks with
        a memo; this one was counted nowhere, so a stale lock naming a recycled pid parked a lane
        silently and forever.

        Only the two REBUILD paths charge it (_exec_reapprove / _exec_regenerate) — the paths whose
        whole action aborts on the decline. The disk-hygiene deferrals (the merge auto-close, the
        parked reaper, the drain just below) defer for the same reason every tick BY DESIGN — a
        merged lane's worker idles at the prompt — and their retry is _drain_pending_teardowns, not
        a rebuild; charging them would park healthy lanes for finishing normally.

        The pid and the lock path are stamped alongside the count because the park memo is useless
        without them: removing that lock is the one thing that frees the lane. Re-read here rather
        than threaded out of _teardown_session — a declined teardown clears NOTHING, so the lock is
        still on disk saying exactly what refused. Returns (count, pid) for the outcome string, so
        the journal shows the ladder climbing instead of N identical lines."""
        pid = self._lock_pid(iid)
        seen = {}
        def charge(st, i):
            self._bump(i, "teardown_deferrals")
            seen["n"] = i["teardown_deferrals"]
        self._update_issue(iid, {"teardown_deferral_pid": pid,
                                 "teardown_deferral_lock": self._lock_path(iid)}, fn=charge)
        return seen.get("n", 0), pid

    def _drain_pending_teardowns(self, st):
        """Retry the teardowns that declined because their CLI was still alive (#149). THIS is the
        retry the ordering's safety argument rests on: teardown may refuse to prune under a live
        CLI precisely because something else will try again, and for a lane that has already
        settled to 'merged' nothing else would.

        Only TERMINAL lanes are drained, and that veto is the whole safety of this sweep: an id can
        be re-approved or regenerated between the decline and the retry, and by then the worktree
        at that path belongs to a NEW, live worker. Tearing that down would be the very bug this
        code exists to prevent, self-inflicted. Terminal here means settled and not coming back
        without a fresh launch — which itself rebuilds the worktree.

        exit_timeout=0: a sweep over every deferred lane on every tick must not pay a per-lane
        stall. Self-guarded — hygiene must never crash the tick or block the heartbeat."""
        d = os.path.join(self.state, "pending_teardown")
        try:
            pending = os.listdir(d)
        except OSError:
            return                                         # no deferrals -> nothing to do
        issues = st.get("issues") if isinstance(st, dict) and isinstance(st.get("issues"), dict) else {}
        for iid in pending:
            rec = issues.get(iid)
            status = rec.get("status") if isinstance(rec, dict) else None
            if not isinstance(status, str):
                continue                                   # unknown lane: fail closed, keep waiting
            if status in actions.TERMINAL_STATUSES:
                # (#190) Inherit the reaper's unsaved-work guard for park-family lanes: a declined
                # prune deferred here can be a parked lane whose worktree still holds the only copy
                # of its work. A MERGED lane never guards — its work is on the mainline by
                # definition — so the merge-path cleanup this drain exists to retry is untouched.
                guard = status in actions.REAPPROVAL_STATUSES
                self._teardown_session(iid, remove_worktree=True, exit_timeout=0, guard_worktree=guard)
            else:
                # Back in flight (relaunched/regenerated since the decline): the worktree at this
                # path is a LIVE lane's now, rebuilt by its own launch. Drop the stale marker —
                # draining here would tear down a running worker, the exact bug we prevent.
                _rm(os.path.join(d, iid))
                # ...and drop any reclaim-hold dedup for it (#190): a lane that left park-family
                # without its worktree being pruned here must, if it later re-parks with the same
                # cause, journal that refusal afresh — the entry is popped on a real prune, so this
                # covers the marker-dropped-without-a-prune path.
                self._reclaim_held.pop(iid, None)

    def _close_stale_session(self, iid):
        """D4: a relaunch (conflict-regenerate, or a retry) targets an id whose PRIOR session may
        still be ALIVE at the interactive prompt — a real claude worker does NOT exit after writing
        its report, it idles at the prompt — and its start-session.sh therefore holds
        worker.<id>.lock for the whole process life. Close that recorded pane FIRST so the old
        start-session's EXIT trap frees the lock, then clear the pane markers + any stale lock so the
        fresh start-session can acquire it; otherwise the new worker can't take the singleton, the
        launch never delivers, and the issue false-parks (surfaced in the live dry-run).

        Safe because a launch is ONLY ever issued for a NON-in-flight id (the scheduler filters
        running/blocked, and reclaim after a runner restart never launches a live worker — the held
        lock is what keeps that reclaim from double-driving) — so the recorded pane here belongs to a
        finished/superseded/dead session, never an actively-building one. No-op when there is no
        recorded pane (a first launch) or the surface is already gone (a crashed session).

        The relaunch face of _teardown_session (#149): same close + same marker hygiene, no prune."""
        self._teardown_session(iid, remove_worktree=False)

    def _mark_exited(self, iid, why, now):
        try:
            with open(os.path.join(self.state, "exited", iid), "w") as f:
                f.write(f"{int(now)} rc=? ({why})\n")
        except OSError:
            pass

    def _evidence(self, kind, rc, **extra):
        """The structured account of one failed script call: its rc-distinct reason plus whatever
        the script said on its way down. `getattr(..., "stderr_tail", "")` is the fail-closed half:
        a plain int rc (an injected stub, or a caller that never captured) genuinely has no
        evidence, and build() turns that into an honest CAPTURED_NONE rather than borrowing text
        from some other call. validate() raises on a malformed record — loudly, at the programmer,
        exactly as journal's own write path does."""
        return evidence.validate(
            evidence.build(kind, int(rc), getattr(rc, "stderr_tail", ""), **extra))

    def _failed(self, kind, rc, text, ev=None, **extra):
        """The ONE way a non-success script outcome becomes a record (issue #152).

        Every failing launch/nudge path returns through here, so an evidence-free failure record
        cannot be written by construction. Pass `ev` when the caller already built the record to
        stamp into loopstate, so the journal and the state carry the SAME account rather than two
        independently-derived ones. The human line names the reason too, so a reader skimming
        `superlooper status` sees "anchor_workspace_missing", never just "rc=1".
        """
        ev = ev if ev is not None else self._evidence(kind, rc, **extra)
        return Outcome(f"{text} ({ev['reason']})", ev)

    def _execute(self, a, now):
        fn = getattr(self, "_exec_" + str(a.get("act")), None)
        if fn is None:
            return f"no executor for {a.get('act')!r}"
        return fn(a, now)

    def _journal_outcome(self, a, outcome, now):
        """Write the action's record: what was decided, what happened, and — when it failed — WHY.

        An outcome that failed carries its evidence (issue #152); it lands as its own field so the
        record answers why, not just what. A SUCCESSFUL outcome stays evidence-free on purpose: it
        has nothing to explain, and a stray empty record on every `ok` would train the reader to
        skip the field on the one tick it matters.
        """
        rec = dict(a, outcome=str(outcome))
        ev = getattr(outcome, "evidence", None)
        if ev is not None:
            rec["evidence"] = ev
        journal.append(self.home, rec, now)

    # --- launches ---

    def _delivery_cleared(self):
        """A verified delivery proves the launch anchor is live (issue #24): clear the distinct-failure
        streak AND reset the #115 canary retry clock. Together they re-arm normal launching and let
        decide journal the systemic-hold recovery on the next tick. Called from every verified-delivery
        path (fresh launch, recover-exited relaunch, resolve-conflict relaunch) so they never drift."""
        self._launch_fail_ids.clear()
        self._launch_fail_at = 0

    def _charge_launch_failure(self, iid, ev, now, canary=False, fields=None):
        """Charge ONE non-verified launch delivery to whoever is actually at fault (issue #153), the
        single decision every launch-delivery path shares (fresh launch, recover-exited relaunch,
        resolve-conflict relaunch) so the invariant is mechanical and can't drift between them.

        A DELIVERY-CHANNEL fault — the cmux anchor, the launch shim, or the launch machinery itself
        (evidence.is_channel_fault) — or a #115 canary probe is charged to the CHANNEL: it feeds the
        systemic streak and the retry clock and NEVER bumps the per-issue launch cap, so no issue
        absorbs a fault none of them caused and a dead channel is detected even when only in-flight
        work is being relaunched. Every OTHER (per-issue) fault bumps the cap so the issue parks.
        `fields` are the caller's path-specific loopstate fields; launch_evidence is always stamped.
        Returns True when charged to the channel (held), False when charged to the issue."""
        merged = dict(fields or {}, launch_evidence=ev)
        if canary or evidence.is_channel_fault(ev):
            self._launch_fail_at = now
            self._launch_fail_ids.add(iid)
            self._update_issue(iid, merged)
            return True
        self._update_issue(iid, merged, fn=lambda st, i: self._bump(i, "launch_failures"))
        return False

    def _exec_launch(self, a, now):
        iid, num, branch = a["id"], a.get("num"), a.get("branch")
        canary = bool(a.get("canary"))                 # a systemic re-arm probe (#115), not charged
                                                       # to the issue: no per-issue cap on failure
        p = self._parsed_by_id.get(iid)
        if p is None:
            return "skipped: issue not in the current GitHub view"
        if canary:
            # (#115) A canary is a systemic PROBE: re-space the retry clock up front so EVERY
            # non-verified outcome below (a brief error, a base-missing worktree, or an unverified
            # delivery) makes the next probe wait a full CANARY_RETRY_SECONDS — never a per-tick
            # re-fire. A verified delivery resets the clock to 0 via _delivery_cleared() below.
            self._launch_fail_at = now
        self._update_issue(iid, {"branch": branch, "num": num, "type": p.get("type"),
                                 "declared_touches": list(p.get("touches") or []),
                                 "wildcard_hold_journaled": False,    # launch ends the hold episode (#36)
                                 "launch_hold_reason": None})         # ...and the eligibility hold (#150)
        # NB: the per-issue model/effort override is stamped into durable state by _worker_env below
        # (it refreshes on EVERY worker launch so the stamp tracks William's current labels) — see
        # its docstring. Kept in one place so recover/resolve_conflict relaunches stamp identically.
        pb = dict(p)
        pb["body"] = (self._raw_by_id.get(iid) or {}).get("body", "")
        pb["branch"] = branch
        # Post-approval owner comments (incident 2026-07-07 §8): fold the issue's LAUNCH-TIME comment
        # thread into the brief so amendments William posts AFTER approving (but before launch) reach
        # the worker. gh.issue_comments fails CLOSED to [] on any gh error, so a fetch failure degrades
        # to "no amendments" — byte-identical to the pre-fix brief — rather than parking a fully-
        # approved issue over a supplementary channel (proceed-and-journal: the fetched count below is
        # the visible record; a genuine gh outage still surfaces via the poll's consecutive_failures
        # ALERT). A regenerate/relaunch routes back through here, so the rebuild picks up the thread
        # fresh. brief.build applies the owner-only trust rule and skips the runner's own markers.
        # A refused read (ok=False) carries comments=[] — the same fail-closed "no amendments" the
        # old contract gave, so brief-building never blocks on a supplementary channel (issue #21).
        comments = gh.issue_comments(num).comments
        # The answered-question trail (#163): a relaunch after an owner's answer embeds the full Q&A
        # so the fresh session inherits every settled decision. Empty/absent qa_log -> no Q&A block,
        # a brief byte-identical to a first launch. Reused across recover/regenerate/answer_relaunch
        # (all route through here), so a WIP that had to be conflict-rebuilt still carries the Q&A.
        qa = self._issue_field(iid, "qa_log")
        qa = qa if isinstance(qa, list) else None
        try:
            text = brief.build(pb, self.config, comments=comments, qa=qa)
        except ValueError as e:
            return f"brief failed: {e}"
        with open(os.path.join(self.home, "briefs", f"{iid}.md"), "w") as f:
            f.write(text)
        journal.append(self.home, {"act": "brief_comments", "id": iid, "num": num,
                                   "fetched": len(comments)}, now)
        if a.get("orphan") and not os.path.isdir(self._worktree(iid)):
            # resume the PR's EXISTING branch: attach (never -b off dev, which would orphan
            # the remote work behind an unpushable non-fast-forward)
            gitops.worktree_add(self.repo, self._worktree(iid), branch)
        # D4: free any prior (finished-but-alive) session for this id before relaunching, so its
        # still-held worker singleton lock can't block this launch's delivery. No-op on a first launch.
        self._close_stale_session(iid)
        rc = self._run_script([self._script("launch-session.sh"), iid],
                              env=self._worker_env(iid), timeout=LAUNCH_TIMEOUT)
        if rc == 0:
            gh.set_labels(num, add=["in-progress"], remove=["agent-ready"])
            # clear any stale base-missing cause: a verified delivery proves the base now exists.
            # launch_evidence clears with it (#152) for the same reason and the #40 staleness lesson
            # (review P1-1): a fixed anchor must not leave last week's cause behind to name the wrong
            # component in a later, unrelated park.
            self._update_issue(iid, {"status": "running", "launch_error": None,
                                     "launch_evidence": None},
                               fn=lambda st, i: self._reset_progress_clock(i))   # fresh #157 episode
            self._delivery_cleared()                   # a verified delivery proves the anchor is live
            return "ok"                                # (a verified canary IS a real launch: the issue
                                                       #  runs and the systemic hold lifts — issue #115)
        ev = self._evidence("launch", rc)
        if rc == LAUNCH_BASE_MISSING_RC:
            # The worktree base branch is missing (issue #28): a per-repo CONFIG fault, not a dead
            # launch anchor. Record the cause so decide's park memo names the branch, and DELIBERATELY
            # keep it OUT of the systemic-anchor streak (which would HOLD the queue and blame the cmux
            # anchor). Still counts toward the per-issue launch cap, so it parks (with the right memo)
            # — UNLESS this was a #115 canary probe, which is never charged to the issue (the clock is
            # already re-spaced above; the hold persists on the existing streak).
            self._update_issue(iid, {"status": "ready", "launch_error": "base_missing",
                                     "launch_evidence": ev},
                               fn=None if canary else (lambda st, i: self._bump(i, "launch_failures")))
            verb = "canary launch" if canary else "launch"
            return self._failed("launch", rc, f"{verb} rc={rc} (worktree base branch missing)", ev=ev)
        # Delivery NOT verified. WHO is at fault decides how it is charged (issue #153): a
        # DELIVERY-CHANNEL fault (the cmux anchor, the launch shim, the launch machinery — or a #115
        # canary probe) feeds the systemic streak and charges NO per-issue cap, so the queue holds
        # systemically on the FIRST such failure and no issue absorbs the blame; a PER-ISSUE fault (a
        # git-level worktree failure, unusable issue state, a missing brief) bumps the cap so it parks
        # with the evidence-named memo (#152). The shared helper is the one place that decision lives.
        if self._charge_launch_failure(iid, ev, now, canary=canary,
                                       fields={"status": "ready", "launch_error": None}):
            note = ("systemic hold persists" if canary
                    else "channel fault — the queue is held systemically, no issue charged")
            verb = "canary launch" if canary else "launch"
            return self._failed("launch", rc, ev=ev,
                                text=f"{verb} rc={rc} (delivery not verified — {note})")
        return self._failed("launch", rc, f"launch rc={rc} (delivery not verified)", ev=ev)

    def _operator(self):
        """The operator display name (issue #58) — config.operator over this repo's config, so
        every stranger-visible line the runner emits (answerer brief, close-investigate memo) signs
        the owner's own name and never a hardcoded person."""
        import config as config_lib
        return config_lib.operator(self.config)

    def _render_question(self, question):
        """The durable question comment (#163): the machine marker FIRST (so brief.build skips it as
        the runner's own marker, never a binding owner amendment), then the worker's verbatim
        question, then how to answer. Signs the owner's own name (config.operator, #58)."""
        op = self._operator()
        return (f"{QUESTION_MARKER}\n"
                f"**A superlooper worker paused this issue to ask {op} a decision, then exited "
                "cleanly** — its work-in-progress branch is pushed and its lane is released, so "
                "nothing sits frozen waiting on an answer.\n\n"
                f"{question}\n\n"
                f"_{op}: answer from the dashboard's Questions list, or reply here and re-apply "
                "`agent-ready` — a fresh session then resumes this issue with your answer in its "
                "brief, reusing the pushed branch if it still applies cleanly._")

    def _exec_post_question(self, a, now):
        """#163: turn a worker's blocked-file question into a DURABLE GitHub comment, close the live
        window, and RELEASE the lane (awaiting_answer). This replaces the live-frozen-session-plus-
        answerer model — the one that died with i336's in-process auth death and i280's zombie window
        — with an exit-clean hand-back the owner answers on his own clock.

        Idempotent and self-healing. The blocked marker is the durable "still needs processing"
        signal, removed ONLY in the final settle, so a crash at any earlier step re-derives this same
        action next tick and converges: the comment posts ONCE (question_posted stamp), the label
        move retries (set_labels is idempotent), the window close is a no-op on an already-closed
        pane. The worktree is PRESERVED (remove_worktree=False, the no-stall D4 close) so the relaunch
        reuses the WIP; the worker has also pushed it to origin, so no live window is the only copy."""
        iid, num, question = a["id"], a.get("num"), a.get("question", "")
        # 1. post the durable question ONCE. Stamp AFTER it lands so a failed post retries next tick;
        #    a re-derived tick (the label move retrying) skips the post and never double-comments.
        if not self._issue_field(iid, "question_posted"):
            if not gh.comment(num, self._render_question(question)):
                return "question comment failed (will retry next tick)"
            self._update_issue(iid, {"question_posted": True})
        # 2. the issue is now waiting on the owner, not building.
        if not gh.set_labels(num, add=["awaiting-answer"], remove=["in-progress"]):
            return "label move failed (will retry silently next tick)"
        self._forget_cached_label(iid, "in-progress")
        # 3. close the live window, PRESERVING the WIP worktree for the relaunch.
        self._teardown_session(iid, remove_worktree=False)
        # 4. settle terminal-for-now: awaiting_answer, bump the 2-capped question counter, reset the
        #    post-once stamp for the NEXT question, and consume the blocked/exited markers LAST.
        def settle(st, i):
            i["status"] = "awaiting_answer"
            i["pending_question"] = question
            # The question's post time (ISO-8601 UTC), so answer ingestion only trusts an answer
            # marker that post-DATES it — a prior question's answer is never reused (#163 review).
            i["question_posted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            prev = i.get("questions_asked")
            i["questions_asked"] = (prev if type(prev) is int else 0) + 1
            i["question_posted"] = False
        self._update_issue(iid, fn=settle)
        _rm(os.path.join(self.state, "blocked", iid))
        _rm(os.path.join(self.state, "exited", iid))   # a stray turn-end stamp must not recover-launch
        return "ok"

    def _exec_answer_relaunch(self, a, now):
        """#163: the owner answered a durable question (the approval verb re-applied). Record the Q&A
        into the durable qa_log the relaunch brief embeds, then re-release the issue to ready+requeue
        so phase-E relaunches a FRESH session — reusing the preserved WIP worktree, or (if the WIP no
        longer applies cleanly) letting the mechanical gate conflict-ladder rebuild it with the Q&A
        carried forward. questions_asked is deliberately NOT reset: the 2-cap spans the issue's life,
        so a re-approval-to-answer must never silently reopen an unbounded round-trip."""
        iid, num = a["id"], a.get("num")
        # A REFUSED/starved comments read fails closed to (comments=[], ok=False) — it must NEVER be
        # read as "no answer" (fresh-agent review P1): that would embed an empty answer as binding and
        # fire ONCE (status leaves awaiting_answer), silently losing the owner's decision. HOLD
        # instead — leave the issue awaiting_answer with agent-ready set, so decide re-emits this next
        # tick until a trustworthy read lands (the refused!=empty discipline await_comments_read uses).
        cr = gh.issue_comments(num)
        if not cr.ok:
            return "comments read refused — holding for a trustworthy read (retries next tick)"
        after = self._issue_field(iid, "question_posted_at")
        answer = _latest_answer(cr.comments, brief.owner_login(self.config), after)
        q = self._issue_field(iid, "pending_question") or ""
        def settle(st, i):
            log = i.get("qa_log")
            log = log if isinstance(log, list) else []
            log.append({"question": q, "answer": answer})
            i["qa_log"] = log
            i["pending_question"] = None
            i["status"] = "ready"
            i["requeue_front"] = True
        self._update_issue(iid, fn=settle)
        # Clear BOTH transient markers, mirroring reapprove: a `blocked` marker that survived a failed
        # _rm (or a worker that wrote one without exiting) would otherwise re-fire post_question on the
        # stale question after the relaunch; the `exited` stamp must not recover-launch (review P2).
        _rm(os.path.join(self.state, "blocked", iid))
        _rm(os.path.join(self.state, "exited", iid))
        ok = gh.set_labels(num, add=["agent-ready"], remove=["awaiting-answer"])
        self._forget_cached_label(iid, "awaiting-answer")
        return "ok" if ok else "answer recorded; label move will retry (orphan sweep reconciles)"

    # --- bounce / park / labels ---

    def _exec_bounce(self, a, now):
        iid, num = a["id"], a.get("num")
        cause = a.get("cause") or "bounce"
        # Notify-once marker (issue #108, mirroring #61's _exec_park): stamp the durable hand-back
        # marker BEFORE the label move, so when the label write fails in the dead zone that NEEDS the
        # bounce (the 2026-07-13 missing `needs-owner` label — #58 renamed it in adopt, nobody re-ran
        # adopt post-republish), decide recognizes next tick's re-derived bounce as the SAME episode
        # and suppresses its notify — the label keeps retrying silently; the text happened once. The
        # notify EXECUTES before this stamp (decide orders it first), so a crash mid-tick can only
        # DUPLICATE the text, never lose it. `park_notify_at` anchors the stuck-label ALERT bound, so
        # a same-cause retry must never re-stamp it (the bound would never elapse); it only repairs an
        # unusable value. `park_comment_posted` makes the verbatim memo comment once-per-episode,
        # retried while the bounce is still re-deriving; once the label lands (terminal) an unposted
        # memo stays best-effort — the notify text and the journal already carried it to the owner.
        # The marker fields are REUSED from the park path (a bounce and a park never overlap on one
        # issue), which also gets this path the `park_label_stuck` alert and the reapprove reset free.
        prev = self._issue_field(iid, "park_notify_cause")
        if prev != cause:
            self._update_issue(iid, {"park_notify_cause": cause, "park_notify_at": now,
                                     "park_comment_posted": False})
        elif not actions._since_ok(self._issue_field(iid, "park_notify_at"), now):
            self._update_issue(iid, {"park_notify_at": now})
        body = ("**Bounced by the worker session** (premise-level drift found at launch-time "
                "reconciliation). The worker's memo, verbatim:\n\n"
                f"{a.get('memo', '')}\n\n"
                "_Labels moved by the runner. The proposed amendment above is ready to approve "
                "or reject — one touch._")
        if not self._issue_field(iid, "park_comment_posted"):
            if gh.comment(num, body):
                self._update_issue(iid, {"park_comment_posted": True})
        if not gh.set_labels(num, add=["needs-owner"], remove=["in-progress"]):
            return "label move failed (will retry silently next tick)"
        _rm(os.path.join(self.state, "blocked", iid))
        def settle(st, i):
            # Stand down any answerer still filed for this issue (mirrors _exec_park /
            # _exec_absorb_close): a bounce is terminal, so it must not leave a `for: <iid>` record
            # behind. This keeps the "answerers holds exactly the ACTIVE answerers" invariant airtight
            # across EVERY terminal transition — the property tidy.closable_answerers rests on to close
            # a bounced issue's finished answerer window (issue #132 review).
            recs = st.get("answerers")
            if isinstance(recs, dict):
                for aid in [k for k, v in recs.items()
                            if isinstance(v, dict) and v.get("for") == iid]:
                    recs.pop(aid, None)
            i["status"] = "bounced"
        self._update_issue(iid, fn=settle)
        return "ok"

    def _exec_absorb_close(self, a, now):
        """Issue #108: the issue was CLOSED on GitHub (William pressed the dashboard's Drop, or
        closed it by hand) WHILE the loop was bouncing/parking it. His close IS his answer — so
        settle local state terminal and stand the episode down: NO further label writes (the issue
        is closed; its labels are moot), NO notify, and clear the hand-back markers + blocked/awaiting
        files so nothing re-derives the bounce/park next tick. Settles to 'merged' — loopstate has no
        'closed' status and 'merged' is the engine's terminal-good bucket for a closed-not-PR-merged
        issue (mirrors _exec_close_investigate); the dashboard, seeing the issue CLOSED on GitHub,
        concludes the flight off the field. Idempotent — decide re-emits only while the issue is still
        in a non-terminal hand-back episode or lingering as a closed owner-decision status; once this
        settles it to 'merged', _in_owner_handback_episode is False and the absorb never re-fires."""
        iid = a["id"]
        _rm(os.path.join(self.state, "blocked", iid))
        _rm(os.path.join(self.state, "awaiting", iid))
        def settle(st, i):
            # drop any lingering answerer record for this issue (mirrors _exec_park): an
            # answerer-caused park that was still storming when the owner closed it would otherwise
            # leave its `for: <iid>` record behind.
            recs = st.get("answerers")
            if isinstance(recs, dict):
                for aid in [k for k, v in recs.items()
                            if isinstance(v, dict) and v.get("for") == iid]:
                    recs.pop(aid, None)
            i.update({"status": "merged", "park_notify_cause": None, "park_notify_at": None,
                      "park_landed_cause": None,       # the pair (#169)
                      "park_comment_posted": False})
        self._update_issue(iid, fn=settle)
        # Reclaim the worktree, exactly as _exec_absorb_merged does for its 'merged' settle — a
        # bounce/park absorbed this way would otherwise leave its worktree behind to accumulate. The
        # owner's close IS the owner verb that resolved this lane (#168), so auto-closing its window
        # here is not the "auto-close stalled work" the ruling forbids; it is gated by the same
        # merged knob for an operator who keeps finished windows for inspection.
        if self._auto_close_merged():
            self._teardown_session(iid, remove_worktree=True)      # ordered (#149)
        return "ok"

    def _forget_cached_label(self, iid, label):
        """Drop `label` from the runner's cached GitHub view for `iid`. The cache
        (`self._parsed_by_id`) only refreshes on the 90s poll, so after the runner itself changes a
        label it must update the cache to match — otherwise a tick before the next poll decides on a
        stale label the runner already removed."""
        p = self._parsed_by_id.get(iid)
        if isinstance(p, dict) and isinstance(p.get("labels"), list) and label in p["labels"]:
            p["labels"] = [l for l in p["labels"] if l != label]

    def _exec_park(self, a, now):
        iid, num = a["id"], a.get("num")
        label = "needs-owner" if a.get("needs_william") else "parked"
        cause = a.get("cause")
        cause = cause if isinstance(cause, str) and cause else a.get("memo", "")
        # Notify-once marker (issue #61): stamped durably BEFORE the label move is attempted, so
        # when the label write fails in the same GitHub dead zone that caused the park (the
        # 2026-07-08 storm failed reads and writes in lockstep), decide recognizes next tick's
        # re-derived park as the SAME episode and suppresses its notify — the labels keep
        # retrying silently; the texting happened once. The notify action EXECUTES before this
        # stamp (decide orders it first — Codex review C1), so a crash mid-tick can only
        # duplicate the text, never lose it. park_notify_at anchors the stuck-label ALERT bound,
        # so a same-cause retry must never re-stamp it (the bound would never elapse); it only
        # repairs an unusable value. park_comment_posted makes the memo comment once-per-episode
        # too (21 duplicate memos in the storm), retried while the park is still re-deriving;
        # once the label lands (terminal) an unposted memo stays best-effort — the notify text
        # and the journal already carried it to the owner.
        prev = self._issue_field(iid, "park_notify_cause")
        if prev != cause:
            self._update_issue(iid, {"park_notify_cause": cause, "park_notify_at": now,
                                     "park_comment_posted": False})
        elif not actions._since_ok(self._issue_field(iid, "park_notify_at"), now):
            self._update_issue(iid, {"park_notify_at": now})
        if not self._issue_field(iid, "park_comment_posted"):
            if gh.comment(num, f"**superlooper parked this issue** — {a.get('memo', '')}"):
                self._update_issue(iid, {"park_comment_posted": True})
        if not gh.set_labels(num, add=[label], remove=["in-progress", "agent-ready"]):
            return "label move failed (will retry silently next tick)"
        # Sync the cache to the label move we JUST made (found live 2026-07-06): a park removes
        # `agent-ready` on GitHub, but the cached view keeps it until the next poll. Without this,
        # the very next tick sees "parked + agent-ready" and `reapprove`s the issue back — reset,
        # relaunch, fail, park, repeat: a park->reapprove churn loop that defeats the launch cap.
        # A genuine re-approval by William re-adds `agent-ready` and the next poll fires reapprove.
        self._forget_cached_label(iid, "agent-ready")
        _rm(os.path.join(self.state, "blocked", iid))
        _rm(os.path.join(self.state, "awaiting", iid))
        def m(st):
            recs = st.get("answerers")
            if isinstance(recs, dict):
                for aid in [k for k, v in recs.items()
                            if isinstance(v, dict) and v.get("for") == iid]:
                    recs.pop(aid, None)
            i = st["issues"].setdefault(iid, loopstate.new_issue())
            i["status"] = "needs_william" if a.get("needs_william") else "parked"
            # (#169) The one durable record that a park's LABELS actually moved — written here and
            # nowhere else, because this block runs only past the set_labels above. `status` cannot
            # carry that fact: a lane that was ALREADY needs_william (parked before, for some other
            # cause) looks identical whether this park landed or died at the gh call, and decide
            # needs the difference. It reads this to tell "the owner put `agent-ready` back" (only
            # possible after a park that really stripped it) from "my label move failed and the old
            # label is simply still standing" — the #61 dead-zone state, which must stay a silent
            # retry. Cleared wherever park_notify_cause is, so it never outlives its episode.
            i["park_landed_cause"] = cause
        loopstate.update(self.issues_path, m)
        return "ok"

    # Per-issue attempt counters a fresh approval zeroes. `launches` MUST be reset alongside
    # `retries`: launch-session.sh recomputes `retries = launches - 1` on every verified delivery,
    # so leaving `launches` at its old value would silently restore `retries` on the next launch
    # and re-park the issue at the retry cap. `conflicts` and the answerer counters are reset too
    # so re-approval is a clean slate on every ladder, not just the launch one. `merge_refusals`
    # is here too so the merge-refusal guard is EPISODE-scoped (issue #27): a re-approval rebuilds
    # from scratch, so the rebuilt PR's merge must be retried from zero, never a forever-latched
    # park. (The paired `merge_refusal_reason` string is cleared in the reset block below, not
    # here — this tuple is int counters only, zeroed with `i[k] = 0`.)
    _REAPPROVE_COUNTERS = ("launches", "retries", "conflicts", "launch_failures",
                           "answerer_failures", "answer_delivery_failures", "merge_refusals",
                           "questions_asked",   # a fresh approval resets the 2-question cap too (#163)
                           "exit_asks")         # ...and the exit-interview ask ladder (#215)
    # (`teardown_deferrals` is deliberately NOT here: the #169 ladder is retired by the teardown
    # that clears the lock — _teardown_session — which this executor has already run by the time it
    # resets anything. Listing it too would zero a counter that is provably 0 and imply this reset
    # is what makes it consecutive, which would be a lie about where the invariant lives.)

    def _exec_reapprove(self, a, now):
        """D7-sibling operator fix: William re-approving a parked/needs-william/bounced issue (a
        fresh `agent-ready`) is a FRESH cap AND a clean slate. The next tick must launch the issue
        from scratch — so, exactly like `_exec_regenerate`, clear every stale finished/in-flight
        artifact FIRST (a leftover report would re-gate, an `exited` marker would `recover` and
        double-launch, a `blocked` marker would re-enter the question flow, a `recheck_failed`
        field would re-park immediately). Then zero the attempt counters (`launches` MUST reset —
        launch-session.sh derives `retries = launches - 1`, so a non-zero launches would restore
        the retry count and re-park at cap) and re-release to `ready`. The old counters are
        JOURNALED (never lost — the honest record of what the issue already cost). actions.decide
        holds the launch back one tick so it fires against the reset state."""
        iid, num = a["id"], a.get("num")
        old = {}
        # 1. local hygiene FIRST (mirrors _exec_regenerate): stale artifacts must be unable to
        #    drive decide() before the fresh launch. Best-effort — no-ops when nothing is there;
        #    launch-session.sh recreates the worktree.
        #    (#149) This USED to prune the worktree outright and leave the pane/lock for the launch
        #    to clean up later — i.e. it unlinked the cwd of a session that was, by this function's
        #    own D4 reasoning, quite possibly still alive: the D14 sequence verbatim. The ordered
        #    teardown closes that session and sees it go first.
        #    A declined prune ABORTS the re-approval, touching no state (same reason as
        #    _exec_regenerate): launch-session.sh reuses a surviving worktree rather than failing,
        #    so a fresh start would silently inherit the parked run's stale checkout — the opposite
        #    of the clean slate this executor promises. decide re-emits while `agent-ready` stands.
        #    (#169) The deferral is COUNTED. It is right to abort, but a stale lock naming a reused
        #    pid never goes dead, and decide's re-emission holds back the very launch whose
        #    _close_stale_session would drop that lock — so an uncounted abort is a lane parked
        #    silently and forever. At the cap decide parks it needs-owner naming the pid + lock.
        if not self._teardown_session(iid, remove_worktree=True):
            n, pid = self._charge_teardown_deferral(iid)
            return (f"worker still live in the worktree (pid {pid}) — deferring the fresh start "
                    f"(deferral {n} of {actions.TEARDOWN_DEFERRAL_CAP}; retries next tick)")
        _rm(os.path.join(self.home, "reports", f"{iid}.md"))
        # `mail` and `ack` join this list for the #215 exit interview (fresh review P1): a park
        # can leave the interview MAIL armed, and mail carries no episode fence — a reapproved
        # episode's fresh session would consume the stale ask at its first rest and could post
        # NO-FINDINGS before re-investigating anything, closing the re-run without its own
        # interview. Pending mail only: the .consumed/.claimed/.discarded receipts are the
        # history of what was actually delivered and stay (launch-session.sh's own rule).
        for sub in ("blocked", "exited", "awaiting", "started", "mail", "ack"):
            _rm(os.path.join(self.state, sub, iid))
        # 2. durable state: zero the attempt counters and clear the stale run/gate fields that
        #    would otherwise re-park, plus any active answerer record (a fresh approval is a fresh
        #    question too — mirrors _exec_park's answerer cleanup).
        def reset(st, i):
            for k in self._REAPPROVE_COUNTERS:
                if i.get(k):                           # record only non-zero prior cost
                    old[k] = i.get(k)
                i[k] = 0
            i.update({"status": "ready", "requeue_front": False, "recheck_failed": False,
                      "update_result": None, "update_head_oid": None, "nudged": [],
                      "nudged_at": {},                 # ...and its per-cause window stamps (#222):
                      "pr": None,                      # a fresh run earns a fresh nudge+grace, never
                                                       # its predecessor's spent, already-expired keys
                      "review_carry": None,            # a rebuild is reviewed on its own diff (#154)
                      "read_waited": False, "checks_pending_since": None,
                      "wildcard_hold_journaled": False,   # a fresh approval re-journals its own hold (#36)
                      "launch_hold_reason": None,      # ...and re-journals an eligibility hold (#150)
                      "merge_refusal_reason": None,    # paired with merge_refusals=0 above (#27)
                      "pr_read_pending_since": None,   # a re-run's refused-read hold times fresh (#61)
                      "comments_read_pending_since": None,   # ...and its comments-read hold too (#78)
                      "park_notify_cause": None, "park_notify_at": None,
                      "park_landed_cause": None,       # ...and the landed-park record (#169)
                      "park_comment_posted": False,    # ...and its own park (if any) texts again (#61)
                      "pending_question": None, "qa_log": [],   # a clean-slate rebuild drops the
                      "question_posted": False,        # prior Q&A trail, its post time, and the
                      "question_posted_at": None,      # post-once stamp (#163)
                      "exit_asked_at": None, "exit_asked_key": None,   # a fresh episode gets a
                      "exit_nonce": None, "exit_verify": None,         # fresh exit interview,
                      "exit_ack_relayed": None})       # paired with exit_asks=0 above (#215)
            recs = st.get("answerers")
            if isinstance(recs, dict):
                for aid in [k for k, v in recs.items()
                            if isinstance(v, dict) and v.get("for") == iid]:
                    recs.pop(aid, None)
        self._update_issue(iid, fn=reset)
        journal.append(self.home, {"act": "reapprove", "id": iid, "old_counters": old}, now)
        # Clear the park-family labels the owner re-approved past; the next tick's launch moves
        # agent-ready -> in-progress. Best-effort: a gh blip only leaves a cosmetic stale label,
        # never blocks the relaunch (phase E keys off agent-ready, not the parked label). Remove BOTH
        # the current `needs-owner` and the legacy `needs-william` so a repo mid-migration clears too.
        gh.set_labels(num, remove=["parked", "needs-owner", "needs-william"])
        # The one-shot `rebuild` label (issue #161) is cleared in its OWN call, and ONLY when it
        # actually triggered this rebuild (`had_rebuild`, set by decide). Both are load-bearing:
        # engine `set_labels` is one batched, all-or-nothing `gh issue edit`, so folding `rebuild`
        # into the batch above would let a repo-absent `rebuild` (a repo that republished the engine
        # but has not re-run `adopt`) HARD-FAIL the whole remove, stranding the park labels — and
        # unconditionally removing `rebuild` on the non-rebuild reapproves (an unfinished lane, an
        # investigation) would hit that same repo-absent hard-fail. When `had_rebuild` holds the label
        # WAS on the issue, so it exists in the repo and this isolated remove is safe. Best-effort:
        # the dashboard's resume verbs also clear a stale `rebuild`, so a blip here is not the only
        # backstop.
        if a.get("had_rebuild"):
            gh.set_labels(num, remove=["rebuild"])
        return f"reapproved (reset {old or 'nothing'})"

    def _exec_resume_at_gate(self, a, now):
        """D11 fix (issue #161): re-approving a FINISHED build RESUMES AT THE GATE — it re-enters the
        mechanical merge gate on the PR the issue already opened, building NOTHING new. Unlike
        _exec_reapprove (the explicit `rebuild` path), this KEEPS the worktree, the filed report, the
        recorded PR, and the #154 durable review carry. It clears ONLY the transient fields that would
        make the gate re-park the instant it re-runs — the episode-scoped merge-refusal guard (#27),
        the recheck-failed hand-back (checked before the gate), the pending-checks / PR-read /
        comments-read hold clocks (#26/#61/#78), the park-notify episode markers (#61), and the
        nudge ledger (#222) — and re-claims the lane for gating: status -> gating, and the labels swap
        agent-ready -> in-progress (the finished/gate path keys off the report on disk, and the launch
        phase skips an in-progress lane, so no fresh session ever rebuilds over the preserved work).
        The attempt counters are DELIBERATELY not zeroed: a resume is a continuation of the same
        episode, not a fresh cap, so the honest prior cost stays. The ONE exception is the #169
        declined-prune ladder below — not an attempt counter but a record of one specific action's
        refusals, which this resume is not making. No _teardown_session: a park-family
        lane has no live session (the park stood it down), and tearing one down is exactly what this
        fix exists to avoid."""
        iid, num = a["id"], a.get("num")
        def resume(st, i):
            i.update({"status": "gating", "requeue_front": False, "recheck_failed": False,
                      "merge_refusals": 0, "merge_refusal_reason": None,
                      "checks_pending_since": None, "comments_read_pending_since": None,
                      "pr_read_pending_since": None, "read_waited": False,
                      "park_notify_cause": None, "park_notify_at": None,
                      "park_landed_cause": None,       # its pair (#169)
                      "park_comment_posted": False,
                      # (#169) The declined-prune ladder is one of those transient fields too, and
                      # it belongs to the ACTION that charged it (a reapprove/regenerate rebuild),
                      # never to the lane at large. A resume tears nothing down, so it charges
                      # nothing — but a resumed lane's worker is idling at the prompt HOLDING its
                      # lock (that is what a finished session does), so a stranded at-cap counter
                      # would park it needs-owner at the FIRST declined prune of its next
                      # regenerate, ~10s in, quoting the previous episode's pid at the owner. Clear
                      # the stamps with the counter: evidence for a count of zero is a lie.
                      "teardown_deferrals": 0, "teardown_deferral_pid": None,
                      "teardown_deferral_lock": None,
                      # issue #222: the nudge ledger is the archetypal "transient field that re-parks
                      # the gate the instant it re-runs" — a leftover key + its stale (long-expired)
                      # `nudged_at` stamp would make the gate park INSTANTLY at the first gate hiccup,
                      # zero grace (defect b). An owner re-approval is a fresh chance, so clear both;
                      # the gate nudges anew and the worker gets its full compliance window.
                      "nudged": [], "nudged_at": {}})
        self._update_issue(iid, fn=resume)
        journal.append(self.home, {"act": "resume_at_gate", "id": iid}, now)
        # Re-claim the lane: in-progress (the loop is driving it to merge) replaces the owner's
        # agent-ready, and the park-family labels the re-approval passed are cleared. Best-effort like
        # _exec_reapprove's own tail: the durable status=gating is the real guard (it keeps the lane
        # out of both the reapprove branch and the launch phase), so a gh blip only leaves a cosmetic
        # stale label, never a rebuild. Remove BOTH needs-owner and the legacy needs-william so a repo
        # mid-migration clears too.
        gh.set_labels(num, add=["in-progress"],
                      remove=["agent-ready", "parked", "needs-owner", "needs-william"])
        self._forget_cached_label(iid, "agent-ready")
        return "resumed at the gate (PR, report and worktree preserved)"

    def _exec_reclaim(self, a, now):
        if not gh.set_labels(a.get("num"), add=["agent-ready"], remove=["in-progress"]):
            return "label move failed (will retry next tick)"
        self._update_issue(a["id"], {"status": "ready"})
        return "ok"

    def _exec_relabel(self, a, now):
        ok = gh.set_labels(a.get("num"), add=a.get("add") or [], remove=a.get("remove") or [])
        return "ok" if ok else "label move failed (will retry next tick)"

    # --- liveness ---

    def _exec_recover(self, a, now):
        iid, tier = a["id"], a.get("tier")
        if tier == "exited":
            # The eligibility-hold episode ends when the GATE PASSES, not when the launch lands —
            # decide only emits recover after start_ok, so by here the hold is already over. Clear
            # the stamp BEFORE the attempt, exactly as _exec_launch does (fresh-agent review P2-2):
            # clearing it only on a verified delivery left a failed relaunch wearing a stale "waiting
            # on #101" against blockers that had since closed, AND silenced the next episode's
            # journal — decide dedups on this stamp, so a genuinely new hold whose reason still
            # matched it never spoke.
            self._update_issue(iid, {"launch_hold_reason": None})
            rc = self._run_script([self._script("launch-session.sh"), iid],
                                  env=self._worker_env(iid),
                                  timeout=LAUNCH_TIMEOUT)
            if rc == 0:
                # Clear BOTH stale launch fields together (#152): they are set together on failure
                # and name the same event, so a verified delivery must not leave one behind to
                # disagree with the other in a later, unrelated park memo.
                self._update_issue(iid, {"status": "running", "launch_error": None,
                                         "launch_evidence": None},
                                   fn=lambda st, i: self._reset_progress_clock(i))  # fresh #157 episode
                self._delivery_cleared()               # a verified delivery proves the anchor is live (#24)
                return "ok"
            ev = self._evidence("launch", rc, tier=tier)
            # Same #153 charge rule as the fresh launch: a channel fault is held systemically and
            # charges no per-issue cap; a per-issue fault bumps the cap. A dead channel that surfaces
            # ONLY here (all work in-flight, nothing fresh to launch) still trips the systemic hold.
            held = self._charge_launch_failure(iid, ev, now)
            tag = "channel fault — held systemically" if held else "issue charged"
            return self._failed("launch", rc, f"relaunch rc={rc} ({tag})", ev=ev)
        # ---- the pid pulse, BEFORE any screen tier (issue #151) ----
        # Ground truth first: if the worker process is gone, nothing the pane renders can change
        # that, and asking the screen is how i160 lost 43 minutes to an unclassifiable one. Only a
        # DEFINITE 'dead' short-circuits — 'unknown' falls through to the screen tiers exactly as
        # before, so a corrupt lock costs a probe and never manufactures a relaunch.
        if self._worker_liveness(iid) == "dead":
            self._mark_exited(iid, f"{tier} with a dead worker pid", now)
            return "dead worker pid — marked exited for relaunch"

        if tier == "frozen":
            # Latch `frozen` AND anchor the progress-clock baseline the #231 un-latch reads against.
            # Capture the signature as of THIS moment ONLY when there is no baseline yet: an `awaiting`
            # lane's probe ladder never ran, so it reaches the freeze with progress_sig=None, and
            # without a baseline the un-latch could not tell a resumed HEAD from the pre-freeze one.
            # Only-if-None is deliberate — never clobber an existing baseline (that would re-anchor to a
            # post-progress signature every 10 minutes and mask the very advance we watch for). A clock
            # that is absent this tick leaves the baseline None; the decide-side first-sight anchor
            # picks it up when a clock finally appears.
            def _anchor_frozen_baseline(st, i):
                i["status"] = "frozen"
                i["last_recover_at"] = now
                if i.get("progress_sig") is None:
                    sig = events_mod.progress_signature(self._status_clocks().get(iid))
                    if events_mod.usable_baseline(sig):   # only a readable-head sig; never poison None
                        i["progress_sig"] = sig
            self._update_issue(iid, fn=_anchor_frozen_baseline)
            surface = self._surface(iid)
            if not surface:
                self._mark_exited(iid, "frozen with no pane recorded", now)
                return "no pane — marked exited for relaunch"
            msg = ("[superlooper] You have been inactive for a long time. Continue with your "
                   "issue; if you are blocked, write your blocked-question file; if you are "
                   "waiting on long background work, touch your awaiting marker.")
            rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                                  env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
            if rc == 4:
                self._mark_exited(iid, "dead pane found by frozen recovery", now)
                return self._failed("nudge", rc, "dead pane — marked exited for relaunch", tier=tier)
            return self._record_sensed(iid, rc, now, tier=tier)
        # idle: the safe peek — a gentle status ask, never a blind action
        surface = self._surface(iid)
        if not surface:
            return "no pane recorded"
        msg = ("[superlooper] Status check: are you progressing? If you are waiting on long "
               "background work, touch your awaiting marker (see your brief).")
        rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                              env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        return self._record_sensed(iid, rc, now, tier="idle")

    def _record_sensed(self, iid, rc, now, tier=None):
        """Turn a nudge-pane rc into the lane's honest sensed state (issue #151). Shared by BOTH
        liveness tiers so their reading of the same rc can never drift apart.

        nudge-pane refuses to TYPE into these panes — that enforcement lives in pane_state, at the
        only place that can actually see the screen. What lands here is the caller's half: naming
        what was seen so decide can act on it (alert the owner; never park a lane that is alive)
        instead of re-firing a nudge every 10 minutes at a pane that cannot answer (i336) or
        marching a live one to a park (i280).

        sensed_state is a LIVE READING, not a label: any other rc clears it, and the recover that
        produced this rc keeps firing (decide suppresses the park, not the re-sense), so the reading
        cannot outlive what it describes. A sticky one would mute the next genuine freeze.

        `sensed_since` stamps when the CURRENT reading began and is preserved while it holds, so the
        alert bound measures the episode rather than resetting every re-sense. It is what stops a
        lane sitting silently at an unanswerable in-window question forever."""
        sensed = {5: "logged_out", 6: "at_dialog"}.get(rc)

        def m(st, i):
            if i.get("sensed_state") != sensed:
                i["sensed_state"] = sensed
                i["sensed_since"] = now if sensed else None
        self._update_issue(iid, fn=m)
        if rc == 0:
            return "ok"
        # Every refusal is journaled WITH the classifier's verdict and the screen it read (#152).
        # A nudge rc=3 record used to carry neither: i160 sat 43 minutes on an ambiguous defer that
        # nobody could classify afterwards, because the screen that produced it was never kept.
        if sensed == "logged_out":
            return self._failed("nudge", rc, "logged out in-window — alerting the owner, not nudging",
                                tier=tier)
        if sensed == "at_dialog":
            return self._failed("nudge", rc, "session is asking something in-window — leaving it alone",
                                tier=tier)
        return self._failed("nudge", rc, f"nudge rc={rc}", tier=tier)

    # --- the progress-stall probe ladder's executors (issue #157) ---

    @staticmethod
    def _reset_progress_clock(i):
        """Clear the #157 progress bookkeeping for a fresh episode. Called on every (re)launch: the
        old session's frozen signature and probe counters must not carry over, or the first tick
        after relaunch would immediately look stalled and re-probe a healthy new lane."""
        i["progress_sig"] = None
        i["progress_since"] = None
        i["probe_attempts"] = 0                 # a counter resets to 0, not to an unset None
        i["probe_nonce"] = None
        i["probe_sent_at"] = None
        i["harvest_tried"] = False              # (#189) a new episode re-arms the one report rescue

    def _exec_progress_advance(self, a, now):
        """Anchor the progress clock. Re-stamps progress_since to now; when the signature ACTUALLY
        changed (real progress / first sight), it also resets the probe episode and clears any stale
        ack — a lane that made progress starts its stall clock and its probe cap over. A mere
        since-repair (same signature, a corrupt clock) re-stamps the clock only, so an in-flight
        escalation is never silently dropped."""
        iid, sig = a["id"], a.get("sig")
        changed = {"v": False}

        def m(st, i):
            if i.get("progress_sig") != sig:
                self._reset_progress_clock(i)              # new episode: zero the probe counters
                changed["v"] = True
            i["progress_sig"] = sig
            i["progress_since"] = now
        self._update_issue(iid, fn=m)
        if changed["v"]:
            _rm(os.path.join(self.state, "ack", iid))      # a new episode: last episode's ack is moot
        return "ok"

    def _exec_unlatch_frozen(self, a, now):
        """Un-latch a lane whose stored status latched to `frozen` but whose #157 progress clock has
        since advanced (issue #231): the session demonstrably resumed real work — a new HEAD, or a
        report/blocked marker, past the freeze baseline — so write the status back to `running`, end
        the frozen recovery ladder, and open a FRESH progress episode anchored at the signature we
        just measured. Keys on the progress clock, never activity: a nudge refreshes activity but
        never moves the signature, so this transition can never answer its own ladder (the i328 trap).

        NB the verb: `unlatch_frozen`, NOT `_exec_unfreeze` (which already exists, below, for the
        unrelated MERGES-frozen mechanism — same word, different machine; this issue's boundary).

        The frozen-era transients are cleared so nothing outlives the freeze: `last_recover_at` (a
        later re-freeze must nudge promptly, not sit out a stale 10-minute window) and any
        `sensed_state` reading (a lane making real progress is neither logged-out nor stuck at a
        dialog — a stale reading would keep firing the #151 alerts at a working lane). The journal
        record — action + id + the evidence class decide named — is written by the tick's
        _journal_outcome, so the un-latch is auditable as one bounded line."""
        iid, sig = a["id"], a.get("sig")

        def m(st, i):
            self._reset_progress_clock(i)          # a fresh #157 episode (zero probe counters)...
            i["progress_sig"] = sig                # ...anchored at the advance we just measured
            i["progress_since"] = now
            i["status"] = "running"
            i["last_recover_at"] = None            # a later re-freeze nudges fresh, not on a stale clock
            i["sensed_state"] = None               # a working lane: drop any frozen-era screen reading
            i["sensed_since"] = None
        self._update_issue(iid, fn=m)
        _rm(os.path.join(self.state, "ack", iid))  # the fresh episode: any old probe ack is moot
        return f"un-latched frozen -> running ({a.get('evidence_class') or 'progress clock advanced'})"

    def _exec_harvest_report(self, a, now):
        """Rescue a report a DONE-acked worker wrote one directory off (issues #148/#189).

        The mover — fences and all — is still worker_hook.harvest_report; only its TRIGGER lives
        here. It ran on every Stop until 07-16, when it promoted two live drafts (i153/i163) to
        "finished" and the gate parked both. A rest is not an ending, so the hook could never tell
        a draft from a finished session's misplaced report; decide only emits this action once the
        worker itself has acked DONE.

        The cwd comes from the worker's OWN progress clock (worker_hook.stamp_status records the
        cwd Claude was actually in), so the harvest looks exactly where the hook used to look. No
        clock, no cwd, no harvest — never a guessed directory for a destructive move.

        DELIBERATELY NOT pinned to self._worktree(iid) (fresh review P2, declined with reasons):
        that path is CONSTRUCTED, not resolved, and an equality check against a resolved cwd
        silently never matches wherever the state home sits behind a symlink (/tmp -> /private/tmp
        on this very platform) — turning the rescue off for good, which is the exact failure this
        duty exists to prevent. The containment fence inside harvest_report already requires the
        resolved source to sit under the resolved cwd, and no reachable exploit was found: the cwd
        is one the worker's own hook stamped, i.e. a directory Claude really ran in.

        `harvest_tried` is stamped WHATEVER happens, including on a refusal: it is the bound that
        keeps a report which simply does not exist from re-harvesting every tick (the i328 loop in
        a new costume). progress_advance clears it, so a genuinely-later finish is still rescued.
        """
        iid = a["id"]
        self._update_issue(iid, fn=lambda st, i: i.update({"harvest_tried": True}))
        clock = self._status_clocks().get(iid)
        cwd = clock.get("cwd") if isinstance(clock, dict) else None
        if not isinstance(cwd, str) or not cwd:
            return "no progress clock cwd — nothing to harvest from (attempt spent)"
        try:
            moved = worker_hook.harvest_report(self.home, iid, cwd)
        except Exception as e:
            # A duty that raises must never wedge the tick. The ladder still escalates.
            journal.append(self.home, {"act": "harvest_error", "id": iid,
                                       "error": _short_repr(e)}, now)
            return f"harvest failed ({e.__class__.__name__}) — the ladder still escalates"
        if not moved:
            return ("no stray report found under the worker cwd — the ladder escalates as before "
                    "(attempt spent)")
        return f"harvested {moved} -> reports/{iid}.md (the worker acked DONE)"

    def _exec_probe(self, a, now):
        """One bounded progress-stall probe (issue #157). Delivers a MACHINE-READABLE ask through the
        safe-send primitive — its refusals (dead/logged-out/at-dialog) are kept intact via
        _record_sensed, exactly like the frozen recover — demanding the worker WRITE an ack file
        (DONE/WORKING/WAITING/STUCK + the nonce), never a prose-only question the runner can't hear.
        The attempt is counted BEFORE the send, so a probe that fails to deliver still walks the cap
        toward escalation rather than looping."""
        iid, num = a["id"], a.get("num")
        surface = self._surface(iid)
        if not surface:
            # No pane to probe. The idle peek could return inertly here (a peek that no-ops costs
            # nothing), but the PROBE tier is a bounded escalation ladder: returning without
            # advancing probe_attempts / probe_sent_at would leave decide re-emitting a probe every
            # tick forever (never escalating — the exact i328 pathology this issue kills). Mirror the
            # frozen tier: a running lane with no pane is a relaunch case, not a nudge case.
            self._mark_exited(iid, "progress-stall probe found no pane recorded", now)
            return "no pane — marked exited for relaunch"
        # Ground truth first (issue #151): a gone worker process can't answer a probe — mark it
        # exited for relaunch instead of typing at a dead pane. Only a DEFINITE 'dead' short-circuits.
        if self._worker_liveness(iid) == "dead":
            self._mark_exited(iid, "progress-stall probe found a dead worker pid", now)
            return "dead worker pid — marked exited for relaunch"
        nonce = "%d-%d" % (int(now), int(a.get("attempt") or 0))
        ack_path = os.path.join(self.state, "ack", iid)
        os.makedirs(os.path.join(self.state, "ack"), exist_ok=True)
        msg = (f"[superlooper] PROGRESS PROBE (nonce {nonce}). Your lane has taken turns but made no "
               f"commit / marker / HEAD change for a while, so the runner cannot tell whether you are "
               f"still making progress. THIS MESSAGE IS READ BY A MACHINE — a prose reply typed here "
               f"is NOT read. Reply by WRITING the file {ack_path} with a single line: "
               f"`<STATE> {nonce}` where <STATE> is exactly one of DONE, WORKING, WAITING, STUCK "
               f"(DONE = finished; WORKING = actively progressing; WAITING = on long background work "
               f"— also touch your awaiting marker; STUCK = need help). Write nothing else in that file. "
               f"Writing this ack does not reset the progress clock, so keep making real progress.")
        # Count the attempt + rotate the nonce BEFORE the send (fail toward the cap, never a loop).
        self._update_issue(iid, fn=lambda st, i: self._probe_bump(i, nonce, now))
        rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                              env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        if rc == 4:
            self._mark_exited(iid, "dead pane found by progress-stall probe", now)
            return self._failed("nudge", rc, "dead pane — marked exited for relaunch", tier="progress")
        return self._record_sensed(iid, rc, now, tier="progress")

    def _probe_bump(self, i, nonce, now):
        self._bump(i, "probe_attempts")
        i["probe_nonce"] = nonce
        i["probe_sent_at"] = now

    # --- the gate's executors ---

    def _exec_gate(self, a, now):
        self._update_issue(a["id"], {"status": "gating"})
        return "ok"

    def _exec_note_checks_pending(self, a, now):
        """Issue #26: stamp WHEN a finished issue's required-checks PENDING wait began, so decide
        can bound it. Idempotent — stamps only if the current clock is not a usable one in [0, now]
        (unset, or a corrupt/future/negative value): the episode's clock must not reset each tick,
        or the bound never elapses. decide re-emits this every pending tick until a usable stamp
        lands, and _exec_clear_checks_pending / reapprove / regenerate clear it."""
        def m(st, i):
            if not actions._since_ok(i.get("checks_pending_since"), now):
                i["checks_pending_since"] = now
        self._update_issue(a["id"], fn=m)
        return "ok"

    def _exec_clear_checks_pending(self, a, now):
        """Issue #26: clear the pending-checks clock the moment the wait is no longer on the checks
        (green/fail/mergeability), so a later pending episode times from scratch instead of
        inheriting a stale start (which would escalate immediately on re-entry)."""
        self._update_issue(a["id"], {"checks_pending_since": None})
        return "ok"

    def _exec_await_read(self, a, now):
        """Issue #21: a finished investigation has NO trustworthy comment read this tick — GitHub
        refused it (omitted from the view), or the poll budget/throttle starved it. decide emitted
        this exactly ONCE per episode (deduped on `read_waited`) so the hold is journaled, never
        silent, and never a park. Stamp the flag; the gate keeps holding at status=gating until a
        clean read lands (then it closes on the marker, or nudges->parks on a genuinely-absent one).
        Reset by _exec_reapprove so a re-run's own wait re-journals."""
        self._update_issue(a["id"], {"read_waited": True})
        return a.get("reason", "holding: finished investigation awaiting a trustworthy comment read")

    def _exec_await_pr_read(self, a, now):
        """Issue #61: a finished BUILD has no trustworthy PR lookup this tick — GitHub refused it
        (omitted from the view) or the poll budget/throttle starved it. decide emitted this only
        while the wait clock is unstamped, so the hold is journaled once per episode (this
        outcome IS the bounded refusal record), never silent, and never an immediate park. Stamp
        idempotently — the PR_READ_HOLD_CAP bound must run from episode start, and a corrupt/
        future clock is repaired, never trusted (the _since_ok discipline). decide parks ONCE if
        the bound expires; _exec_clear_pr_read ends the episode when a trustworthy read lands."""
        def m(st, i):
            if not actions._since_ok(i.get("pr_read_pending_since"), now):
                i["pr_read_pending_since"] = now
        self._update_issue(a["id"], fn=m)
        return a.get("reason", "holding: finished build awaiting a trustworthy PR lookup")

    def _exec_clear_pr_read(self, a, now):
        """Issue #61: a trustworthy PR lookup landed (a real PR, or a clean answered-empty) —
        the refused-read episode is over; a later one times from scratch."""
        self._update_issue(a["id"], {"pr_read_pending_since": None})
        return "ok"

    def _exec_await_comments_read(self, a, now):
        """Issue #78: a finished BUILD's PR IS visible but its comments sub-read was REFUSED
        (omitted from the view) or starved this tick — so the gate can't verify review evidence.
        HOLD — never nudge, never park a reviewed build on a fail-closed empty comments read (the
        build-gate sibling of _exec_await_pr_read). decide emitted this only while the wait clock
        is unstamped, so the hold is journaled once per episode (this outcome IS the bounded
        refusal record), never silent, never an immediate park. Stamp idempotently — the
        PR_READ_HOLD_CAP bound must run from episode start, and a corrupt/future clock is repaired,
        never trusted (the _since_ok discipline). decide parks ONCE if the bound expires;
        _exec_clear_comments_read ends the episode when a trustworthy comments read lands."""
        def m(st, i):
            if not actions._since_ok(i.get("comments_read_pending_since"), now):
                i["comments_read_pending_since"] = now
        self._update_issue(a["id"], fn=m)
        return a.get("reason", "holding: finished build awaiting a trustworthy comments read")

    def _exec_clear_comments_read(self, a, now):
        """Issue #78: a trustworthy comments read landed (marker present -> merges, or a clean
        answered-empty -> the review nudge ladder resumes) — the refused-read episode is over; a
        later one times from scratch."""
        self._update_issue(a["id"], {"comments_read_pending_since": None})
        return "ok"

    def _exec_clear_park_marker(self, a, now):
        """Issue #61: the issue left its failing state without the park label ever landing (e.g.
        the PR became visible and the gate flipped to merge), so the notify-once episode is over.
        Clearing the marker lets a LATER genuine park on this issue text again — the guard is
        per-cause-episode, never forever."""
        self._update_issue(a["id"], {"park_notify_cause": None, "park_notify_at": None,
                                     "park_landed_cause": None,   # its pair (#169)
                                     "park_comment_posted": False})
        return "ok"

    def _exec_wildcard_hold(self, a, now):
        """Issue #36: a no-touches wildcard serialized the queue (this issue could not co-schedule).
        decide emitted this ONCE per episode (deduped on `wildcard_hold_journaled`); stamp the flag
        so the same continuous hold does not re-journal every tick, and return the reason so the
        journal outcome carries the WHY. The flag is reset on launch (_exec_launch) and on reapprove,
        so a later, fresh episode re-journals. Journal-only: no label move, no notify."""
        self._update_issue(a["id"], {"wildcard_hold_journaled": True})
        return a.get("reason", "launch held by a no-touches wildcard — the lane serializes")

    def _exec_launch_hold(self, a, now):
        """Issue #150 (D8): the one launch gate refused to start/restart this session. decide emitted
        this ONCE per CAUSE (deduped on the stamped reason); stamp it so the same standing hold does
        not re-journal every tick, and return the reason so the journal outcome carries the WHY. The
        stamp clears on launch (_exec_launch) and on reapprove, so a later episode speaks again; a
        CHANGED cause re-journals immediately because the reason no longer matches. Journal-only: no
        label move, no notify, no status change — a hold is a WAIT, not a park."""
        self._update_issue(a["id"], {"launch_hold_reason": a.get("reason")})
        return a.get("reason", "launch held by the eligibility gate")

    def _exec_hold(self, a, now):
        self._update_issue(a["id"], {"status": "holding"})
        return "ok"

    def _exec_nudge(self, a, now):
        iid, key = a["id"], a.get("nudge_key")
        surface = self._surface(iid)
        if not surface:
            rc = 4                                     # no pane = nowhere to nudge: spend the key
        else:
            rc = self._run_script([self._script("nudge-pane.sh"), surface, iid,
                                   f"[superlooper gate] {a.get('message', '')}"],
                                  env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        if rc in (0, 4, 5):
            # sent, or unsendable-FOREVER (4 = dead pane; 5 = logged out in-window, issue #151):
            # either way the one nudge is spent — gate.nudge_or_park parks on the next pass (never
            # an unbounded nudge loop). rc=5 belongs here and not with the defers: a session whose
            # auth is dead can never answer, and before #151 taught the classifier to see it, this
            # screen read as 'idle' and was typed into for rc=0 — which spent the key and reached
            # the owner. Leaving 5 out would have made a logged-out lane re-nudge every tick and
            # never park: strictly worse than the bug being fixed.
            # rc=6 (at_dialog) is NOT here on purpose: the pane is LIVE, and a dialog that gets
            # answered leaves the next pass free to deliver, so spending the lane's one nudge on a
            # refusal would burn it for nothing. It defers exactly like rc=3 — and inherits rc=3's
            # unbounded-retry shape, which is pre-existing and should be bounded for both together
            # if the gate's defer is ever capped. (Not "the session answers it" — nobody may:
            # the liveness tier's session_at_dialog alert is what catches a dialog nobody answers.)
            def m(st):
                i = st["issues"].setdefault(iid, loopstate.new_issue())
                nudged = i.get("nudged")
                if not isinstance(nudged, list):
                    nudged = []
                if key not in nudged:
                    nudged = nudged + [key]
                i["nudged"] = nudged
                # issue #222: stamp WHEN this cause was nudged (the moment the key is spent — this
                # branch is reached only on a delivered/dead/logged-out send, never a defer). decide
                # times the compliance window from this stamp before parking, so the worker gets a
                # real grace to comply instead of the pre-#222 one-tick death. A wrong-typed ledger
                # is rebuilt fresh here, matching the `nudged` guard just above.
                nudged_at = i.get("nudged_at")
                if not isinstance(nudged_at, dict):
                    nudged_at = {}
                nudged_at[key] = now
                i["nudged_at"] = nudged_at
                i["status"] = "gating"
            loopstate.update(self.issues_path, m)
            if rc == 0:
                return "ok"
            why = "logged out in-window" if rc == 5 else "dead pane"
            return f"{why} — nudge spent, gate parks next pass"
        return f"nudge rc={rc} (retrying next tick)"

    def _exec_merge(self, a, now):
        iid, num, pr = a["id"], a.get("num"), a.get("pr")
        ok, reason = gh.merge_pr(pr, a.get("method", "squash"), head_oid=a.get("head_oid"))
        if not ok:
            # GitHub REFUSED the merge of a gate-green PR — ordinary branch protection (required
            # approvals / strict up-to-date) or a token without merge rights (issue #27). Count the
            # refusal and record WHY (the bounded gh stderr): decide keeps retrying under the bound,
            # then parks needs-william ONCE with this reason. The status never advances past the
            # truth — the issue stays gating until it merges or the cap parks it. No force path, no
            # protection bypass: the refusal is surfaced to the owner, never coached around.
            self._update_issue(iid, {"merge_refusal_reason": reason},
                               fn=lambda st, i: self._bump(i, "merge_refusals"))
            return f"merge refused (will retry to the bound): {reason or '(no gh reason)'}"
        # (#165) A pre-authorized referee merge NAMES what rode through it and under whose word.
        # Reciting the ordinary green rationale for a diff that crossed a bright line would make the
        # one unattended referee merge the loop can perform its least legible one — and a referee
        # change is LIVE on merge, with no publish backstop to catch it later.
        if a.get("referee_preauthorized") is True:
            paths = a.get("referee_paths")
            named = ", ".join(p for p in paths if isinstance(p, str)) \
                if isinstance(paths, list) else ""
            gh.comment(num, f"Merged as PR #{pr} by superlooper (gate green: report + review "
                            "evidence + required checks + mergeable) — including referee path(s) "
                            f"{named or '(unnamed)'}, merged under {self._operator()}'s "
                            "`pre-authorized:referee` pre-authorization rather than parked.")
        else:
            gh.comment(num, f"Merged as PR #{pr} by superlooper (gate green: report + review "
                            "evidence + required checks + mergeable).")
        gh.set_labels(num, remove=["in-progress"])
        self._update_issue(iid, {"status": "merged"})
        if self._auto_close_merged():
            # (#149) The D14 hot path: the lane that just merged still has its worker idling at the
            # prompt in this very worktree, so the old bare prune unlinked a live CLI's cwd at the
            # exact moment the lane finished. Ordered teardown: close, see it go, then reclaim.
            # (#168) A merged-and-landed lane is the ONE case the owner allows to auto-close.
            self._teardown_session(iid, remove_worktree=True)
        return "ok"

    def _review_carry(self, iid, head, pre, wt):
        """Carry the PR's review verdict across the runner's OWN merge-update (issue #154).

        A merge-update merges dev into the branch and plain-pushes: the head moves, but the
        worker's AUTHORED diff is untouched — so the fresh-agent verdict pinned to the pre-merge
        head still vouches for exactly the code being merged. Without this record the gate's
        diff-pin would read the new head as "reviewed at a superseded diff" and nudge->park every
        PR the runner itself updated.

        That claim — "the new head is the REVIEWED head plus dev, and nothing else" — is only true
        if the worktree was ACTUALLY sitting on the reviewed head when we merged, so `pre` (HEAD
        read before the merge) must equal `head` (the oid the gate judged) or we carry nothing.
        Without that check the carry asserts a fact it never verified: a worker that committed
        locally without pushing (or pushed inside the ~90s poll window) leaves the worktree ahead
        of the head the gate saw, `plain_push` fast-forwards those commits onto the remote, and the
        carry would attest code no reviewer read. `ship_recheck_cmd` and CI do NOT close that hole
        — they prove the merged tree's TESTS pass, never that a fresh agent read the diff, which is
        the whole distinction #154 exists to draw. (Fresh-review finding, P0-1.)

        Reaching here means the gate returned `update`, which sits BELOW step 2b — so review
        evidence was valid for `head` at decision time, either pinned to it directly or carried
        onto it by a previous update. `from` therefore keeps the ORIGINALLY reviewed oid across a
        chain of updates; only `to` advances. Fails closed everywhere: an unreadable head on either
        side records no carry at all (the gate then asks for a re-review rather than trust a guess).
        """
        new_head = gitops.head_oid(wt)
        if not (isinstance(head, str) and head and new_head and pre):
            return None
        if pre.lower() != head.lower():
            return None          # the worktree was NOT at the reviewed head — vouch for nothing
        prev = self._issue_field(iid, "review_carry")
        reviewed = head
        if isinstance(prev, dict) and isinstance(prev.get("from"), str) \
                and isinstance(prev.get("to"), str) and prev["to"].lower() == head.lower():
            reviewed = prev["from"]        # a chain of updates keeps naming the oid actually reviewed
        return {"from": reviewed, "to": new_head}

    def _exec_update(self, a, now):
        iid, head = a["id"], a.get("head_oid")
        wt = self._worktree(iid)
        # HEAD *before* the merge: the carry's premise is that we merged dev into the head the gate
        # judged, and that is only checkable from here (afterwards the merge has already moved it).
        pre = gitops.head_oid(wt)
        res = gitops.merge_update(wt, self.config.get("dev_branch", "main"))
        if res == "clean":
            recheck = self.config.get("ship_recheck_cmd")
            if isinstance(recheck, str) and recheck.strip():
                if self._run_cmd(recheck, cwd=wt) != 0:
                    self._update_issue(iid, {"recheck_failed": True})
                    return "recheck failed after merge-update — parking via decide"
            # Record the carry BEFORE the push, and independent of its outcome. The carry states a
            # LINEAGE fact ("this new head is the reviewed head plus dev"), not "the push landed",
            # and it is inert unless the PR's head actually becomes `to`. Recording it only on a
            # push that REPORTS success would park correctly-reviewed work when a push lands but
            # reports nonzero (a network drop after the ref update): the head moves on GitHub, no
            # carry exists, and step 2b sits ABOVE the update retry — so the gate parks on
            # `review_stale` before the retry could heal it. (Fresh-review finding, P0-1 related.)
            #
            # ...and only ever WRITE a carry we actually computed. An unconditional write lets a
            # `None` overwrite a CORRECT carry on the retry after a failed push: the worktree is
            # already merged by then, so `pre` is the merged oid rather than the head the gate
            # judged, `_review_carry` declines, and the wipe re-opens the same false-park through
            # a different door. Never wiping is safe — a stale {from: A, to: C} can only fire if
            # the head becomes exactly C, and only that merge can produce C, so the claim holds.
            # (Second fresh review, P1.)
            carry = self._review_carry(iid, head, pre, wt)
            if carry:
                self._update_issue(iid, {"review_carry": carry})
            if gitops.plain_push(wt):
                self._update_issue(iid, {"update_result": "clean", "update_head_oid": head,
                                         "update_errors": 0})
                return "ok"
            res = "error"                              # push refused/failed: infra, retry
        if res == "conflict":
            self._update_issue(iid, {"update_result": "conflict", "update_head_oid": head})
            return "real conflict — gate decides regenerate/preserve next pass"
        self._update_issue(iid, {"update_result": "error", "update_head_oid": head},
                           fn=lambda st, i: self._bump(i, "update_errors"))
        return "merge-update infra error (will retry; never treated as a conflict)"

    def _exec_regenerate(self, a, now):
        iid, num, pr = a["id"], a.get("num"), a.get("pr")
        # 1. local hygiene FIRST (M1): the stale worktree and markers must be unable to
        #    false-gate the rebuild even if the gh steps below fail and retry later.
        #    (#149) Ordered: the superseded session is closed and observed gone before its worktree
        #    is pruned — this path used to unlink the cwd of a session it knew might still be live
        #    (D4), which is the D14 stamp-killer.
        #    A declined prune must ABORT the regenerate, touching no state: launch-session.sh only
        #    creates the worktree `if [ ! -d "$WT" ]`, so relaunching over a surviving stale
        #    worktree does not fail — it SILENTLY reuses it, and the rebuild would run on the OLD
        #    conflicted branch while its brief names the new one, pushing commits onto a superseded
        #    PR. Fail closed instead: leave every field untouched so decide re-emits this same
        #    regenerate next tick, by which time the old CLI has almost certainly gone.
        #    (#169) Counted, for the same reason as _exec_reapprove's: the gate re-derives this same
        #    regenerate from the same unchanged conflict every tick, so an unclearable worktree
        #    means a rebuild emitted forever and landed never. At the cap decide parks it.
        if not self._teardown_session(iid, remove_worktree=True):
            n, pid = self._charge_teardown_deferral(iid)
            return (f"worker still live in the worktree (pid {pid}) — deferring the rebuild "
                    f"(deferral {n} of {actions.TEARDOWN_DEFERRAL_CAP}; retries next tick)")
        _rm(os.path.join(self.home, "reports", f"{iid}.md"))
        for sub in ("blocked", "exited", "awaiting"):
            _rm(os.path.join(self.state, sub, iid))
        # 2. durable state: the new branch is stamped BEFORE any relabel, so a partial failure
        #    can never resurrect the old branch (the orphan sweep checks the stamp). The
        #    merge-refusal guard is per-PR and episode-scoped (issue #27): a regenerate supersedes
        #    the old PR and rebuilds on a fresh branch, so the new PR's merge starts from zero —
        #    reset the counter and its captured reason, exactly like update_result/pr/nudged above.
        #    (`conflicts` is deliberately NOT reset here — unlike merge_refusals it counts toward
        #    the conflict cap across generations.)
        self._update_issue(iid, {"status": "ready", "branch": a.get("new_branch"),
                                 "conflicts": a.get("conflicts"), "requeue_front": True,
                                 "update_result": None, "update_head_oid": None,
                                 "review_carry": None,   # fresh branch, fresh review (#154)
                                 "nudged": [], "nudged_at": {},   # fresh nudge+grace window (#222)
                                 "pr": None, "recheck_failed": False,
                                 # (the #169 declined-prune ladder is already retired by the
                                 # _teardown_session above — it clears the lock, which IS the cause)
                                 "checks_pending_since": None, "merge_refusals": 0,
                                 "merge_refusal_reason": None,
                                 "pr_read_pending_since": None,   # fresh branch, fresh episodes (#61)
                                 "comments_read_pending_since": None,   # ...comments-read hold too (#78)
                                 "park_notify_cause": None, "park_notify_at": None,
                                 "park_landed_cause": None,       # its pair (#169)
                                 "park_comment_posted": False})
        # 3. GitHub: supersede the PR (branch preserved on the remote, PR left open — nothing
        #    auto-closed), tell the issue, requeue it front-of-band.
        dev = self.config.get("dev_branch", "main")
        ok = gh.pr_add_labels(pr, ["superseded"])
        ok = gh.pr_comment(pr, "Superseded by superlooper: this PR conflicted with current "
                               f"`{dev}` and the issue is being rebuilt on a fresh branch "
                               f"(`{a.get('new_branch')}`). Branch preserved; nothing "
                               "auto-closed.") and ok
        ok = gh.comment(num, f"Conflicted with `{dev}` — superseding PR #{pr} (branch "
                             f"preserved on the remote) and rebuilding from this issue on "
                             f"current `{dev}` (conflict {a.get('conflicts')} of the cap).") and ok
        ok = gh.set_labels(num, add=["agent-ready"], remove=["in-progress"]) and ok
        return "ok" if ok else "gh bookkeeping incomplete (orphan sweep will reconcile)"

    def _exec_resolve_conflict(self, a, now):
        iid, num, pr = a["id"], a.get("num"), a.get("pr")
        branch = self._issue_field(iid, "branch") or ""
        secs = self.config.get("report_required_sections")
        secs = ", ".join(f"`## {s}`" for s in secs) if isinstance(secs, list) else ""
        text = _sub(_CONFLICT_BRIEF, {
            "pr": pr, "issue_num": num, "branch": branch,
            "dev_branch": self.config.get("dev_branch", "main"),
            "report_path": os.path.join(self.home, "reports", f"{iid}.md"),
            "report_sections": secs,
            # rendered from gate, never retyped: the form taught here cannot drift from the form
            # the gate parses (#154)
            "review_marker": gate.pinned_review_marker(),
            "pin_placeholder": gate.REVIEW_PIN_PLACEHOLDER,
            "blocked_path": os.path.join(self.state, "blocked", iid)})
        with open(os.path.join(self.home, "briefs", f"{iid}.md"), "w") as f:
            f.write(text)
        # D4 (same as _exec_launch): the preserve-path resolver relaunches into the id's OWN
        # worktree while the finished-but-alive prior session still holds worker.<id>.lock — free it
        # first, else this relaunch can't take the singleton and (status stays 'gating') retries
        # forever. Only reached with a report present, so the recorded pane is a finished session.
        self._close_stale_session(iid)
        # The eligibility-hold episode ended when start_ok passed, not when this launch lands — clear
        # the stamp before the attempt, as _exec_launch/_exec_recover do (review P2-2, #150).
        self._update_issue(iid, {"launch_hold_reason": None})
        rc = self._run_script([self._script("launch-session.sh"), iid],
                              env=self._worker_env(iid), timeout=LAUNCH_TIMEOUT)
        if rc == 0:
            self._update_issue(iid, {"status": "running", "update_result": None,
                                     "update_head_oid": None, "review_carry": None,
                                     "nudged": [], "nudged_at": {},   # fresh nudge+grace (#222)
                                     "launch_error": None, "launch_evidence": None})
            self._delivery_cleared()                   # a verified delivery proves the anchor is live (#24)
            return "ok"
        # This launch failure is journaled too, so it carries evidence like every other (#152), and
        # follows the same #153 charge rule as _exec_launch/_exec_recover: a channel fault is held
        # systemically (no per-issue cap), a per-issue fault bumps the cap so it parks.
        ev = self._evidence("launch", rc)
        held = self._charge_launch_failure(iid, ev, now)
        tag = "channel fault — held systemically" if held else "issue charged"
        return self._failed("launch", rc, f"conflict-session launch rc={rc} ({tag})", ev=ev)

    def _exec_close_investigate(self, a, now):
        iid, num = a["id"], a.get("num")
        claim = a.get("exit")
        claim = claim.strip() if isinstance(claim, str) and claim.strip() else None
        text = ("Investigation complete — the root-cause report is the marker comment above; "
                f"child issues (if any) carry `parent: #{num}` and await {self._operator()}'s "
                "approval.")
        if claim:
            # the exit interview's accounted claim (#215), restated at the close so the audit
            # trail is one comment, not a thread-dig
            text += f" Exit interview: `{claim}`."
        if not gh.close_issue(num, comment=text):
            return "close failed (will retry next tick)"
        gh.set_labels(num, remove=["in-progress"])
        self._update_issue(iid, {"status": "merged"})  # terminal-good (loopstate has no 'closed')
        return "ok"

    # --------------- the exit interview (issue #215) ---------------

    def _exit_interview_text(self, num, defect=None, ack_path=None, nonce=None):
        """The exit-interview ask, rendered for either channel. The grammar strings are gate.py's
        own constants — the form the worker is taught cannot drift from the form the gate parses
        (the #154 rule). ack_path+nonce -> the degraded (Codex) rendering, where the reply comes
        back through the nonce-fenced ack file; otherwise the Claude mailbox rendering, where the
        worker posts the reply comment itself."""
        why = f"Your previous answer did not settle it: {defect}.\n\n" if defect else ""
        file_kids = ("file EACH ONE as its own child issue NOW — labeled `needs-owner`, "
                     f"carrying `parent: #{num}` in its `## Loop metadata` section")
        if ack_path:
            return (f"[superlooper] EXIT INTERVIEW (nonce {nonce}). Issue #{num} (an "
                    f"investigation) closes only after its findings are accounted for. {why}"
                    f"If your report surfaced findings that need follow-up work, {file_kids}. "
                    f"Then reply by WRITING the file {ack_path} containing a single line: "
                    f"`{gate.EXIT_FINDINGS_PREFIX} #a #b {nonce}` (the child issue numbers you "
                    f"filed) or `{gate.EXIT_NO_FINDINGS} {nonce}` (nothing needs follow-up). "
                    "THIS MESSAGE IS READ BY A MACHINE — a prose reply typed here is NOT read.")
        return (f"[superlooper exit interview] Issue #{num} (an investigation) is about to "
                f"close.\n\n{why}"
                "Before it closes: if your report surfaced findings that need follow-up work, "
                f"{file_kids}. Then reply by posting ONE comment on issue #{num} whose body is "
                "exactly one line —\n\n"
                f"    {gate.EXIT_FINDINGS_PREFIX} #a #b\n"
                "        (the child issue numbers you filed), or\n"
                f"    {gate.EXIT_NO_FINDINGS}\n"
                "        (nothing needs follow-up — an explicit, auditable claim)\n\n"
                f"e.g. `gh issue comment {num} --body '{gate.EXIT_NO_FINDINGS}'`. The runner "
                f"verifies each ref against #{num}'s real child set and closes only after the "
                "reply verifies. The reply is read by a MACHINE: the comment body must be that "
                "one line and nothing else.")

    def _exec_exit_interview(self, a, now):
        """Deliver (or re-deliver) the #215 exit interview through the worker channel. The ask is
        stamped BEFORE the send — the probe ladder's rule: a send that never lands still walks the
        bounded ladder toward the park, never a loop. The outcome text never claims delivery: on
        Claude the mailbox consumption receipt is the only proof (#148), on Codex the nonce-fenced
        ack is — an rc=0 'sent' was never 'arrived' (i280)."""
        iid, num = a["id"], a.get("num")
        nonce = "exit-%d" % int(now)
        defect = a.get("defect") if isinstance(a.get("defect"), str) else None

        def stamp(st, i):
            self._bump(i, "exit_asks")
            i.update({"exit_asked_at": now, "exit_asked_key": a.get("reply_key"),
                      "exit_nonce": nonce})
        self._update_issue(iid, fn=stamp)
        surface = self._surface(iid)
        if self.agent == "codex":
            # degraded path: Codex's Stop hook cannot block a stop, so no mailbox — the ask is
            # TYPED and the reply comes back through the ack file (stop-hook.sh's agent split).
            ack_path = os.path.join(self.state, "ack", iid)
            os.makedirs(os.path.join(self.state, "ack"), exist_ok=True)
            msg = self._exit_interview_text(num, defect=defect, ack_path=ack_path, nonce=nonce)
            if not surface:
                return ("no pane recorded — ask spent; the bounded ladder parks if no reply "
                        "ever lands")
            rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                                  env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
            return (f"exit interview typed (rc={rc}) — the reply is judged by the ack file, "
                    "never this rc")
        # Claude: arm the mailbox (atomically — the Stop hook may claim it at any rest), then
        # wake the resting session with the payload-free ping. A finished worker is RESTING and
        # the hook fires only at a turn end, so without the ping the mail would sit unread; the
        # ping is the sanctioned idle-wake keystroke (mailbox spike 2026-07-15) and carries no
        # payload on purpose — the mail + receipt are the real channel.
        mail_dir = os.path.join(self.state, "mail")
        mail = os.path.join(mail_dir, iid)
        try:
            os.makedirs(mail_dir, exist_ok=True)
            tmp = mail + ".tmp"
            with open(tmp, "w") as f:
                f.write(self._exit_interview_text(num, defect=defect))
            os.replace(tmp, mail)
        except OSError as e:
            return (f"mail write failed ({e.__class__.__name__}) — ask spent; the bounded "
                    "ladder re-asks once, then parks")
        if not surface:
            return ("interview mailed; no pane recorded for a wake ping — a live session "
                    "consumes it at its next rest; delivery pends the consumption receipt")
        rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, EXIT_WAKE_PING],
                              env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        return (f"interview mailed + wake ping (rc={rc}) — delivery is judged by the "
                "consumption receipt, never this rc")

    def _exec_verify_exit_refs(self, a, now):
        """The ONE added GitHub read per finishing investigation (#215, owner API-burn ruling
        2026-07-16): a typed child-set search proving each FINDINGS-FILED ref is a genuine
        `parent: #N` child that accounts for its finding (needs-owner, or already released/
        closed). The verdict is stamped against the reply's key so it never re-fires for the same
        reply — and a REFUSED read stamps nothing: decide re-emits next tick, which IS the wait
        (refused != empty, the #21/#61 discipline)."""
        iid, num = a["id"], a.get("num")
        refs = [r for r in a.get("refs") or [] if type(r) is int]
        rh = gh.child_issues_health(num)
        if not rh.ok:
            return ("child-set read refused — no verdict stamped; waiting for a trustworthy "
                    "read (retries next tick)")
        accounted = gate.accounted_child_nums(rh.value)
        missing = sorted(r for r in refs if r not in accounted)
        self._update_issue(iid, {"exit_verify": {"key": a.get("reply_key"), "missing": missing}})
        if missing:
            return "refs not accounted by the child set: " + ", ".join("#%d" % m for m in missing)
        return "ok"

    def _exec_relay_exit_reply(self, a, now):
        """Post the degraded path's ack as the durable one-line reply comment (#215): the claim
        must be timestamped and owner-auditable on the issue whichever channel carried it. The
        relayed-nonce stamp lands only AFTER gh confirms the write, so a failed post retries
        next tick. Delivery is therefore AT-LEAST-ONCE: a crash between the confirmed post and
        the stamp re-posts the identical line next tick — harmless, because the gate's
        newest-wins parse reads duplicates as one answer."""
        iid, num = a["id"], a.get("num")
        if not gh.comment(num, a.get("line") or ""):
            return "reply comment failed (will retry next tick)"
        self._update_issue(iid, {"exit_ack_relayed": a.get("nonce")})
        return "ok"

    # --- system state ---

    def _exec_freeze(self, a, now):
        # Tag the freeze with its OWNER (Codex R2 C2): the runner freezes only on a red dev
        # required-check, so its marker is source="dev-check". The nightly writes source="nightly".
        loopstate.save(os.path.join(self.state, "merges_frozen.json"),
                       {"reason": a.get("reason"), "fingerprint": a.get("fingerprint"),
                        "since": now, "source": "dev-check"})
        return "ok"

    def _exec_unfreeze(self, a, now):
        # The runner unfreezes on dev-CHECK green, so it may clear only dev-check (or untagged/
        # legacy) freezes. A nightly/browser-suite freeze (source="nightly") is the nightly's to
        # clear (a green nightly does it) — removing it here would let merges flow while the
        # nightly is still red. Codex R2 C2.
        path = os.path.join(self.state, "merges_frozen.json")
        marker = _read_json(path)
        if isinstance(marker, dict) and marker.get("source") == "nightly":
            return "held: nightly-owned freeze (only a green nightly clears it)"
        _rm(path)
        return "ok"

    def _exec_record_pr(self, a, now):
        """Stamp the PR number the reconcile found for an in-flight lane (issue #155). Durable, so
        it outlives the restart that re-derives the rest of the view — and it is what scopes the
        out-of-band-close hand-back to the PR THIS episode opened (reapprove/regenerate clear it,
        so a relaunched lane never inherits the previous episode's closed PR)."""
        self._update_issue(a["id"], {"pr": a.get("pr")})
        return "ok"

    def _exec_absorb_merged(self, a, now):
        """The PR is already MERGED on GitHub (a crash landed between merge and bookkeeping,
        or William merged by hand): settle labels + local state to match the truth.
        Idempotent — decide re-emits until this succeeds."""
        iid, num = a["id"], a.get("num")
        if not gh.set_labels(num, remove=["in-progress"]):
            return "label cleanup failed (will retry next tick)"
        self._update_issue(iid, {"status": "merged"})
        if self._auto_close_merged():
            self._teardown_session(iid, remove_worktree=True)      # ordered (#149); merged-only (#168)
        return "ok"

    def _exec_file_fix_issue(self, a, now):
        fp = a.get("fingerprint")
        # Crash window (Codex round-1 C3): create_issue may have succeeded on a prior tick
        # whose fingerprint save never landed. GitHub is the truth — reconcile before filing,
        # so the standing-rule issue is never duplicated.
        marker = f"Failure fingerprint: `{fp}`"        # the canonical field _fix_issue emits —
        for it in gh.open_issues("auto-approved:nightly-red"):     # never a bare substring
            body = it.get("body") if isinstance(it, dict) else None
            if isinstance(body, str) and fp and marker in body and type(it.get("number")) is int:
                path = os.path.join(self.state, "fix_issues.json")
                filed = _read_json(path) or {}
                filed[fp] = it["number"]
                loopstate.save(path, filed)
                return f"already filed as #{it['number']} (reconciled from GitHub)"
        num = gh.create_issue(a.get("title"), a.get("body"), labels=a.get("labels"))
        if num is None:
            return "issue create failed (will retry next tick)"
        path = os.path.join(self.state, "fix_issues.json")
        filed = _read_json(path) or {}
        filed[fp] = num
        loopstate.save(path, filed)
        return "ok"

    def _exec_alert(self, a, now):
        loopstate.save(os.path.join(self.state, "ALERT"),
                       {"reasons": a.get("reasons"), "since": now})
        return "ok"

    def _exec_clear_alert(self, a, now):
        _rm(os.path.join(self.state, "ALERT"))
        return "ok"

    def _exec_fail_open(self, a, now):
        """Issue #46: the usage meter has been UNREADABLE past the grace, so decide chose to launch
        normally rather than freeze the loop on a meter we merely cannot read. Journal-only — the
        launch policy is applied by usage_ok's fail_open flag and the owner is notified via the
        usage_stale ALERT that rides the same crossing. decide emits this ONCE per dark episode
        (deduped on the ALERT-on-disk marker), so the outcome text IS the durable record."""
        return a.get("reason", "usage meter dark past the grace — failing open (launching normally)")

    def _exec_usage_recovered(self, a, now):
        """Issue #46: the usage meter reads again; the fail-open episode is closed. Journal-only
        (normal gating resumes via usage_ok on the fresh reading); the outcome text records the
        episode close. Emitted ONCE per episode by decide."""
        return a.get("reason", "usage meter readable again — normal usage gating resumed")

    def _exec_launch_recovered(self, a, now):
        """Issue #115: a verified delivery (a canary probe or a restart) cleared the systemic-launch
        streak, so the #24 hold is lifting. Journal-only — normal launching resumes via the empty
        streak and the systemic ALERT is retracted by decide's reasons diff; the outcome text records
        the recovery. Emitted ONCE per episode by decide (deduped on the durable ALERT marker)."""
        return a.get("reason", "launch delivery verified again — the systemic launch hold is cleared")

    def _exec_morning_report(self, a, now):
        try:
            with open(os.path.join(self.state, "last_morning_report"), "w") as f:
                f.write(str(a.get("date")))
        except OSError:
            return "stamp failed"
        self._morning_report_hook(a.get("date"), now)
        return "ok"

    def _morning_report_hook(self, date, now):
        """Task 11 seam (filled): report.morning() renders reports/morning-<date>.md from the
        journal + the live view assembled here, then notify.send() pushes a one-line summary. A
        render/write/notify failure is contained — the action record + the journal it reads are
        already durable, so the report can always be re-rendered by `superlooper morning-report`."""
        import report                                # lazy: keep the agent-agnostic runner light
        import notify
        import stack_doctor
        records = journal.read(self.home)
        # ledger.json is the flat accepted-failure map (Task 12); the report only needs its size.
        # Read fail-closed here, exactly as _read_json does everywhere else in the runner.
        ledger = _read_json(os.path.join(self.home, "ledger.json")) or {}
        frozen = _read_json(os.path.join(self.state, "merges_frozen.json"))
        if frozen == {}:
            frozen = {"reason": "merges_frozen.json unreadable"}   # existence = frozen (fail closed)
        # The waiting queue = last poll's agent-ready issues not yet claimed (in-progress removed).
        queue = []
        for p in self._parsed_by_id.values():
            labels = p.get("labels") or []
            if "agent-ready" in labels and "in-progress" not in labels:
                queue.append({"num": p.get("num"), "title": p.get("title")})
        queue.sort(key=lambda q: q["num"] if isinstance(q.get("num"), int) else 1 << 30)
        # Installed-engine publish drift (issue #39): the loop runs the INSTALLED engine, so merged
        # engine fixes are inert until republished through the gated bin/install.sh. engine_drift
        # measures how far behind the installed copy is (git is impure, so it happens here, not in
        # the pure report.py). It never raises and skips cleanly when self.repo is not a superlooper
        # source checkout, so a plain adopted repo carries no notice.
        drift = stack_doctor.engine_drift(
            repo_path=self.repo, dev_branch=self.config.get("dev_branch"))
        view = {"date": date, "now": now, "frozen": frozen, "queue": queue,
                "usage": self.usage_view(), "engine_drift": drift}
        text = report.morning(records, view, ledger, self.config)
        try:
            with open(os.path.join(self.home, "reports", f"morning-{date}.md"), "w") as f:
                f.write(text)
        except OSError:
            pass
        # First non-title, non-blank line is the summary tally / "nothing happened" — the push body.
        summary = next((ln for ln in text.splitlines()
                        if ln.strip() and not ln.startswith("#")), "morning report ready")
        # The morning push doubles as the notify-channel CANARY (issue #164): send_test sends the
        # SAME push via the SAME precedence as send(), but returns the full delivery result. Journal
        # it as `notify_canary` so the NEXT report's "Notify channel" line surfaces a SILENTLY dead
        # channel (once dead for days, found only by a human reading the journal) on the owner-read
        # report + dashboard — the one surface a dead channel can't itself reach. This is the morning
        # heartbeat, at a reasonable hour: it adds NO 3am ping, unlike a synthetic nightly probe.
        r = notify.send_test(self.config, f"superlooper morning report — {date}", summary)
        self._log(f"morning report {date}: notify [{r.channel} ok={r.ok} rc={r.rc}]")
        try:
            journal.append(self.home, {"act": "notify_canary", "date": date, "ok": bool(r.ok),
                                       "channel": r.channel, "rc": r.rc,
                                       "detail": (r.stderr or "")[:200], "outcome": "ok"}, now)
        except (OSError, ValueError):
            pass                                # the report already rendered; a canary write hiccup
                                                # never breaks the morning report (contained failure)

    def _exec_notify(self, a, now):
        """Task 11 seam (filled): notify.send() delivers by the configured precedence
        (imessage_to → cmd → cmux → log-only) and never raises — its outcome string is what the
        tick loop journals for this action, so the content is never lost, only (at worst) unsent."""
        import notify
        outcome = notify.send(self.config, a.get("title"), a.get("body"))
        self._log(f"NOTIFY [{outcome}] {a.get('title')}: {a.get('body')}")
        return outcome


if __name__ == "__main__":
    print("runner.py is a module; use `superlooper run` (the CLI wires config + repo).",
          file=sys.stderr)
    sys.exit(2)
