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
import gate
import gh
import gitops
import journal
import loopstate
import published_view
import tidy
import usage as usage_mod

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
DELIVERY_RETRY_SECONDS = 120   # min spacing between answer-delivery attempts to one pane
_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"   # SL_CMUX overrides (tests)

# The Restart request marker (issue #116). A Restart request asks the LIVE runner to restart ITSELF
# in its own cmux tab: `superlooper request-restart` drops this file in the STATE HOME (never
# .superlooper/**), and the runner honors it at the safe point between ticks by re-exec'ing in place.
# It is a small JSON audit record (operator + when + source); its mere EXISTENCE is the signal — a
# present-but-corrupt body still restarts, like state/ALERT. (A local ops UI over the loop shells the
# `request-restart` command, exactly as it shells `superlooper tidy`; the engine names no such UI.)
RESTART_MARKER = "runner.restart"

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
   verdict as a PR comment BEGINNING `<!-- superlooper-review -->`.
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


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


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
                 run_script=None, fetch_usage=None, workspace="", window=""):
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
        self.stop = False
        self._owns_lock = False
        # True once this process ADOPTED the singleton across a Restart re-exec (issue #116) — the
        # reborn half of a self-restart, set in acquire_singleton via the SL_RESTART_ADOPT token.
        self._reexec_adopted = False
        self._consecutive_tick_errors = 0    # reset on the first clean tick (incident 2026-07-07)
        self._tick_alert_on_disk = False     # the wedge ALERT is confirmed written (retry until so)
        self._tick_alert_notified = False    # the wedge notify+journal fired once this episode
        # DISTINCT issues in the current unbroken run of launch-delivery failures (issue #24). Any
        # verified delivery clears it; >= actions.SYSTEMIC_LAUNCH_FAILURE_CAP distinct ids is a
        # SYSTEMIC launch fault (dead anchor), not N per-issue parks. In-memory on purpose (like
        # _consecutive_tick_errors): it is live runtime health, and a restart — the documented
        # recovery for a wedged anchor — is exactly when it should reset to a clean slate.
        self._launch_fail_ids = set()
        # The #115 canary retry clock: the wall-clock of the most recent launch-delivery FAILURE.
        # decide gates the systemic-hold canary on `now - this >= CANARY_RETRY_SECONDS`, so the first
        # probe waits a full interval after the trip and each failed canary re-spaces the next. Reset
        # to 0 on any verified delivery. In-memory like the streak — a restart re-arms from scratch.
        self._launch_fail_at = 0

        self.state = os.path.join(self.home, "state")
        self.issues_path = os.path.join(self.state, "issues.json")
        for sub in ("state/activity", "state/blocked", "state/exited", "state/awaiting",
                    "state/panes", "state/started", "state/launch_stderr", "state/events/processed",
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
                # skip above drops an issue the moment it parks.
                if (cached.get("state") != "OPEN"
                        or gate.review_evidence_ok(self.config, cached.get("comments"))):
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
        lt = time.localtime(now)
        return {
            "issues_state": st,
            "blocked": self._scan_dir("state", "blocked"),
            "reports": reports,
            "answers": self._scan_dir("answers"),
            "exited": self._scan_dir("state", "exited"),
            "launch_stderr": self._scan_dir("state", "launch_stderr"),   # {id: tail} for #40 memos
            "frozen": frozen,
            "alert": alert,
            "live_lock_ids": self._live_lock_ids(),
            "filed_fingerprints": _read_json(os.path.join(self.state, "fix_issues.json")) or {},
            "local_date": time.strftime("%Y-%m-%d", lt),
            "local_hhmm": time.strftime("%H:%M", lt),
            "last_report_date": (_read(os.path.join(self.state, "last_morning_report")) or "").strip() or None,
        }

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
        work survives on the branch ref; worktree_remove drops only the checkout). Config-gated so an
        operator can keep parked worktrees for inspection. Best-effort: a worktree that can't be fully
        removed (worktree_remove -> False) is simply retried on a later tick — never raised."""
        if not self.config.get("cleanup_parked_worktrees", True):
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
            # exit_timeout=0: this loop is unbounded in N and runs every tick, so it must never pay
            # a per-lane stall waiting for a pid. Disk hygiene has no deadline — a lane whose CLI is
            # still unwinding is simply reclaimed on the next sweep, by which time the pane close
            # issued here has landed.
            self._teardown_session(iid, remove_worktree=True, exit_timeout=0)

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
        if not self.gh_view.get("stale"):
            self._refresh_finishing_prs(ist_map)
            self._refresh_finishing_investigation_comments(ist_map)

        # Launch-anchor liveness (issue #24): hand decide the runner-level launch-health signals it
        # can't sense itself (it is pure). The DISTINCT-failure streak always; the per-tick pane probe
        # only when there is demand to launch (so an idle runner never shells out to cmux or alerts).
        disk["launch_fail_ids"] = sorted(self._launch_fail_ids)
        disk["launch_fail_at"] = self._launch_fail_at    # the #115 canary retry clock (decide reads it)
        if self._wants_launch():
            disk["launch_anchor"] = self._anchor_status()

        lane_state = actions.lane_state_from(st)
        acts = actions.decide(now, self.config, self.usage_view(),
                              list(self._parsed_by_id.values()), lane_state, evs, disk,
                              self.gh_view, wake_grace_until=self._wake_grace_until)
        for a in acts:
            try:
                outcome = self._execute(a, now)
            except Exception as e:
                outcome = f"executor error: {_short_repr(e)}"   # bound the repr (incident 2026-07-07)
            journal.append(self.home, dict(a, outcome=outcome), now)

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
        self._reclaim_terminal_worktrees(st)           # git-remove park-family worktrees (safe: rebuilt on reapprove)
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
            return r.returncode
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127

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

    def _lock_pid(self, iid):
        """The pid recorded in worker.<id>.lock, or None when there is no lock / it names no
        process. start-session.sh writes the lock atomically WITH its pid (`ln` of a fully-written
        temp) and its EXIT trap frees it, so a readable pid here means a worker process that was
        alive when it took the lock. An empty/garbage lock names nobody: None, never a veto."""
        txt = (_read(os.path.join(self.state, f"worker.{iid}.lock")) or "").strip()
        try:
            pid = int(txt)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _pid_alive(self, pid):
        """True when `pid` names a live process. Signal 0 is the probe start-session.sh's own
        acquire_worker uses (`kill -0`), so the runner and the shell agree on liveness. A pid we
        may not signal (EPERM) EXISTS — that is alive, not dead: guessing dead there is what would
        prune under a live CLI."""
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _await_worker_exit(self, iid, pid, timeout=None):
        """Observe the worker CLI go, bounded. Returns True when it is gone (or was never there),
        False when it outlived the wait.

        The lock pid is authoritative, not state/exited/<id>: that marker can be STALE (a previous
        generation of the same id wrote one) and a stale marker read as proof is exactly how a live
        CLI gets pruned out from under. The lock, by contrast, is held for the whole process life
        and freed by start-session.sh's EXIT trap, so a dead pid means the CLI has truly unwound."""
        if not pid:
            return True                                    # no lock -> no live worker to wait for
        deadline = time.monotonic() + (WORKER_EXIT_TIMEOUT if timeout is None else timeout)
        while True:
            if not self._pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(WORKER_EXIT_POLL)

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

    def _teardown_session(self, iid, remove_worktree=False, exit_timeout=None):
        """THE one ordered teardown for every session end (issue #149). Every path that ends a
        lane's session comes through here, so the ordering below is stated once and cannot drift.

            1. close the pane          — ask the CLI to go; its EXIT trap frees worker.<id>.lock
            2. observe it actually go  — bounded (only when we intend to prune; see below)
            3. clear pane markers + the lock, together (D9: no marker outlives its session)
            4. only THEN prune the worktree

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

        Returns True when everything asked for was done."""
        pid = self._lock_pid(iid)                          # BEFORE step 3 clears the lock
        self._close_pane(iid)
        if remove_worktree and not self._await_worker_exit(iid, pid, timeout=exit_timeout):
            return False
        for p in (os.path.join(self.state, "panes", iid),
                  os.path.join(self.state, "panes", f"{iid}.ws"),
                  os.path.join(self.state, f"worker.{iid}.lock")):
            _rm(p)
        if remove_worktree:
            return gitops.worktree_remove(self.repo, self._worktree(iid))
        return True

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

    def _execute(self, a, now):
        fn = getattr(self, "_exec_" + str(a.get("act")), None)
        if fn is None:
            return f"no executor for {a.get('act')!r}"
        return fn(a, now)

    # --- launches ---

    def _delivery_cleared(self):
        """A verified delivery proves the launch anchor is live (issue #24): clear the distinct-failure
        streak AND reset the #115 canary retry clock. Together they re-arm normal launching and let
        decide journal the systemic-hold recovery on the next tick. Called from every verified-delivery
        path (fresh launch, recover-exited relaunch, resolve-conflict relaunch) so they never drift."""
        self._launch_fail_ids.clear()
        self._launch_fail_at = 0

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
                                 "wildcard_hold_journaled": False})   # launch ends the hold episode (#36)
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
        try:
            text = brief.build(pb, self.config, comments=comments)
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
            # clear any stale base-missing cause: a verified delivery proves the base now exists
            self._update_issue(iid, {"status": "running", "launch_error": None})
            self._delivery_cleared()                   # a verified delivery proves the anchor is live
            return "ok"                                # (a verified canary IS a real launch: the issue
                                                       #  runs and the systemic hold lifts — issue #115)
        if rc == LAUNCH_BASE_MISSING_RC:
            # The worktree base branch is missing (issue #28): a per-repo CONFIG fault, not a dead
            # launch anchor. Record the cause so decide's park memo names the branch, and DELIBERATELY
            # keep it OUT of the systemic-anchor streak (which would HOLD the queue and blame the cmux
            # anchor). Still counts toward the per-issue launch cap, so it parks (with the right memo)
            # — UNLESS this was a #115 canary probe, which is never charged to the issue (the clock is
            # already re-spaced above; the hold persists on the existing streak).
            self._update_issue(iid, {"status": "ready", "launch_error": "base_missing"},
                               fn=None if canary else (lambda st, i: self._bump(i, "launch_failures")))
            verb = "canary launch" if canary else "launch"
            return f"{verb} rc={rc} (worktree base branch missing)"
        # Delivery NOT verified. Stamp the #115 canary retry clock so the next probe waits a full
        # interval, and record this id in the runner-level anchor streak (issue #24) — decide reads
        # the streak to tell a dead anchor (many distinct ids) from a genuinely bad issue (one).
        self._launch_fail_at = now
        self._launch_fail_ids.add(iid)
        if canary:
            # A SYSTEMIC probe (issue #115), NEVER charged to the issue: no per-issue launch-cap bump
            # and no park. The streak above already stands at/over the systemic cap, so the hold
            # persists; the issue stays queued (ready, agent-ready never moved) for the next probe or
            # the eventual resume.
            self._update_issue(iid, {"status": "ready", "launch_error": None})
            return f"canary launch rc={rc} (delivery not verified — systemic hold persists)"
        self._update_issue(iid, {"status": "ready", "launch_error": None},
                           fn=lambda st, i: self._bump(i, "launch_failures"))
        return f"launch rc={rc} (delivery not verified)"

    def _operator(self):
        """The operator display name (issue #58) — config.operator over this repo's config, so
        every stranger-visible line the runner emits (answerer brief, close-investigate memo) signs
        the owner's own name and never a hardcoded person."""
        import config as config_lib
        return config_lib.operator(self.config)

    def _exec_hire_answerer(self, a, now):
        iid, aid, question = a["id"], a["answerer_id"], a["question"]
        _, answerer_model = self._models()
        answers_dir = os.path.join(self.home, "answers")
        _rm(os.path.join(answers_dir, f"{iid}.md"))    # a stale answer must never answer a NEW question
        template = _read(os.path.join(_TEMPLATES, "answerer-brief.md")) or ""
        body = (self._raw_by_id.get(iid) or {}).get("body", "")
        text = _sub(template, {"issue_num": str(a.get("num")), "issue_body": body,
                               "question": question, "worktree": self._worktree(iid),
                               "answer_path": os.path.join(answers_dir, f"{iid}.md"),
                               "operator": self._operator()})
        with open(os.path.join(self.home, "briefs", f"{aid}.md"), "w") as f:
            f.write(text)
        answerer_effort = ""
        if self.agent == "codex":
            answerer_model = os.environ.get("SL_MODEL", "")
            answerer_effort = os.environ.get("SL_EFFORT", "")
        rc = self._run_script([self._script("launch-session.sh"), "--cwd", answers_dir, aid],
                              env=self._script_env(answerer_model, answerer_effort), timeout=LAUNCH_TIMEOUT)
        if rc == 0:
            def m(st):
                recs = st.setdefault("answerers", {})
                recs[aid] = {"for": iid, "launched_at": now}
                n = int(aid[1:]) if aid[1:].isdigit() else 0
                st["next_answerer"] = max(st.get("next_answerer", 1)
                                          if type(st.get("next_answerer")) is int else 1, n + 1)
                st["issues"].setdefault(iid, loopstate.new_issue())["status"] = "blocked"
            loopstate.update(self.issues_path, m)
            return "ok"
        self._update_issue(iid, {"status": "blocked"},
                           fn=lambda st, i: self._bump(i, "answerer_failures"))
        return f"answerer launch rc={rc}"

    def _exec_deliver_answer(self, a, now):
        iid, aid = a["id"], a["answerer_id"]
        last = self._issue_field(iid, "last_delivery_attempt", 0)
        if isinstance(last, (int, float)) and now - last < DELIVERY_RETRY_SECONDS:
            return "deferred (delivery rate limit)"
        surface = self._surface(iid)
        if not surface:
            self._mark_exited(iid, "no pane recorded", now)
            return "no pane recorded — marked exited for relaunch"
        msg = (f"[superlooper] Answer from a fresh answerer to your blocked question:\n"
               f"{a.get('text', '')}\n"
               f"Your blocked marker has been cleared — continue with the issue.")
        rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                              env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        if rc == 0:
            _rm(os.path.join(self.state, "blocked", iid))
            def m(st):
                recs = st.get("answerers")
                if isinstance(recs, dict):
                    recs.pop(aid, None)
                i = st["issues"].setdefault(iid, loopstate.new_issue())
                i["status"] = "running"
                i["answer_delivery_failures"] = 0
            loopstate.update(self.issues_path, m)
            return "ok"
        if rc == 4:
            self._mark_exited(iid, "dead pane on answer delivery", now)
        self._update_issue(iid, {"last_delivery_attempt": now},
                           fn=lambda st, i: self._bump(i, "answer_delivery_failures"))
        return f"nudge rc={rc}"

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
                      "park_comment_posted": False})
        self._update_issue(iid, fn=settle)
        # Reclaim the worktree, exactly as _exec_absorb_merged does for its 'merged' settle — a
        # bounce/park absorbed this way would otherwise leave its worktree behind to accumulate.
        if self.config.get("cleanup_merged_worktrees", True):
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
                           "answerer_failures", "answer_delivery_failures", "merge_refusals")

    def _exec_reapprove(self, a, now):
        """D7-sibling operator fix: William re-approving a parked/needs-william/bounced issue (a
        fresh `agent-ready`) is a FRESH cap AND a clean slate. The next tick must launch the issue
        from scratch — so, exactly like `_exec_regenerate`, clear every stale finished/in-flight
        artifact FIRST (a leftover report would re-gate, an `exited` marker would `recover` and
        double-launch, a `blocked` marker would re-enter the answerer flow, a `recheck_failed`
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
        #    teardown closes that session and sees it go first, and declines to prune while its pid
        #    lives (the rebuild simply waits a tick — the launch below re-adds the worktree).
        self._teardown_session(iid, remove_worktree=True)
        _rm(os.path.join(self.home, "reports", f"{iid}.md"))
        for sub in ("blocked", "exited", "awaiting", "started"):
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
                      "update_result": None, "update_head_oid": None, "nudged": [], "pr": None,
                      "read_waited": False, "checks_pending_since": None,
                      "wildcard_hold_journaled": False,   # a fresh approval re-journals its own hold (#36)
                      "merge_refusal_reason": None,    # paired with merge_refusals=0 above (#27)
                      "pr_read_pending_since": None,   # a re-run's refused-read hold times fresh (#61)
                      "comments_read_pending_since": None,   # ...and its comments-read hold too (#78)
                      "park_notify_cause": None, "park_notify_at": None,
                      "park_comment_posted": False})   # ...and its own park (if any) texts again (#61)
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
        return f"reapproved (reset {old or 'nothing'})"

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
            rc = self._run_script([self._script("launch-session.sh"), iid],
                                  env=self._worker_env(iid),
                                  timeout=LAUNCH_TIMEOUT)
            if rc == 0:
                self._update_issue(iid, {"status": "running"})
                self._delivery_cleared()               # a verified delivery proves the anchor is live (#24)
                return "ok"
            self._update_issue(iid, fn=lambda st, i: self._bump(i, "launch_failures"))
            return f"relaunch rc={rc}"
        if tier == "frozen":
            self._update_issue(iid, {"status": "frozen", "last_recover_at": now})
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
                return "dead pane — marked exited for relaunch"
            return "ok" if rc == 0 else f"nudge rc={rc}"
        # idle: the safe peek — a gentle status ask, never a blind action
        surface = self._surface(iid)
        if not surface:
            return "no pane recorded"
        msg = ("[superlooper] Status check: are you progressing? If you are waiting on long "
               "background work, touch your awaiting marker (see your brief).")
        rc = self._run_script([self._script("nudge-pane.sh"), surface, iid, msg],
                              env=self._script_env("", ""), timeout=NUDGE_TIMEOUT)
        return "ok" if rc == 0 else f"nudge rc={rc}"

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
        if rc in (0, 4):
            # sent, or unsendable-forever (dead pane): either way the one nudge is spent —
            # gate.nudge_or_park parks on the next pass (never an unbounded nudge loop)
            def m(st):
                i = st["issues"].setdefault(iid, loopstate.new_issue())
                nudged = i.get("nudged")
                if not isinstance(nudged, list):
                    nudged = []
                if key not in nudged:
                    nudged = nudged + [key]
                i["nudged"] = nudged
                i["status"] = "gating"
            loopstate.update(self.issues_path, m)
            return "ok" if rc == 0 else "dead pane — nudge spent, gate parks next pass"
        return f"nudge rc={rc} (retrying next tick)"

    def _exec_merge(self, a, now):
        iid, num, pr = a["id"], a.get("num"), a.get("pr")
        ok, reason = gh.merge_pr(pr, a.get("method", "squash"))
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
        gh.comment(num, f"Merged as PR #{pr} by superlooper (gate green: report + review "
                        "evidence + required checks + mergeable).")
        gh.set_labels(num, remove=["in-progress"])
        self._update_issue(iid, {"status": "merged"})
        if self.config.get("cleanup_merged_worktrees", True):
            # (#149) The D14 hot path: the lane that just merged still has its worker idling at the
            # prompt in this very worktree, so the old bare prune unlinked a live CLI's cwd at the
            # exact moment the lane finished. Ordered teardown: close, see it go, then reclaim.
            self._teardown_session(iid, remove_worktree=True)
        return "ok"

    def _exec_update(self, a, now):
        iid, head = a["id"], a.get("head_oid")
        wt = self._worktree(iid)
        res = gitops.merge_update(wt, self.config.get("dev_branch", "main"))
        if res == "clean":
            recheck = self.config.get("ship_recheck_cmd")
            if isinstance(recheck, str) and recheck.strip():
                if self._run_cmd(recheck, cwd=wt) != 0:
                    self._update_issue(iid, {"recheck_failed": True})
                    return "recheck failed after merge-update — parking via decide"
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
        #    (D4), which is the D14 stamp-killer. A live pid defers the prune to a later tick.
        self._teardown_session(iid, remove_worktree=True)
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
                                 "nudged": [], "pr": None, "recheck_failed": False,
                                 "checks_pending_since": None, "merge_refusals": 0,
                                 "merge_refusal_reason": None,
                                 "pr_read_pending_since": None,   # fresh branch, fresh episodes (#61)
                                 "comments_read_pending_since": None,   # ...comments-read hold too (#78)
                                 "park_notify_cause": None, "park_notify_at": None,
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
            "blocked_path": os.path.join(self.state, "blocked", iid)})
        with open(os.path.join(self.home, "briefs", f"{iid}.md"), "w") as f:
            f.write(text)
        # D4 (same as _exec_launch): the preserve-path resolver relaunches into the id's OWN
        # worktree while the finished-but-alive prior session still holds worker.<id>.lock — free it
        # first, else this relaunch can't take the singleton and (status stays 'gating') retries
        # forever. Only reached with a report present, so the recorded pane is a finished session.
        self._close_stale_session(iid)
        rc = self._run_script([self._script("launch-session.sh"), iid],
                              env=self._worker_env(iid), timeout=LAUNCH_TIMEOUT)
        if rc == 0:
            self._update_issue(iid, {"status": "running", "update_result": None,
                                     "update_head_oid": None, "nudged": []})
            self._delivery_cleared()                   # a verified delivery proves the anchor is live (#24)
            return "ok"
        self._update_issue(iid, fn=lambda st, i: self._bump(i, "launch_failures"))
        return f"conflict-session launch rc={rc}"

    def _exec_close_investigate(self, a, now):
        iid, num = a["id"], a.get("num")
        if not gh.close_issue(num, comment="Investigation complete — the root-cause report is "
                                           "the marker comment above; child issues (if any) "
                                           f"carry `parent: #{num}` and await {self._operator()}'s "
                                           "approval."):
            return "close failed (will retry next tick)"
        gh.set_labels(num, remove=["in-progress"])
        self._update_issue(iid, {"status": "merged"})  # terminal-good (loopstate has no 'closed')
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

    def _exec_absorb_merged(self, a, now):
        """The PR is already MERGED on GitHub (a crash landed between merge and bookkeeping,
        or William merged by hand): settle labels + local state to match the truth.
        Idempotent — decide re-emits until this succeeds."""
        iid, num = a["id"], a.get("num")
        if not gh.set_labels(num, remove=["in-progress"]):
            return "label cleanup failed (will retry next tick)"
        self._update_issue(iid, {"status": "merged"})
        if self.config.get("cleanup_merged_worktrees", True):
            self._teardown_session(iid, remove_worktree=True)      # ordered (#149)
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
        outcome = notify.send(self.config, f"superlooper morning report — {date}", summary)
        self._log(f"morning report {date}: notify [{outcome}]")

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
