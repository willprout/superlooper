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
import usage as usage_mod

TICK_SECONDS = 15
TICK_ERROR_ALERT = 4           # consecutive tick crashes (~1 min at 15 s) -> ALERT + notify. A
                               # wedged tick never reaches actions.decide, so this alarm is raised
                               # from run()'s own guard, not the decide brain (incident 2026-07-07).
USAGE_REFRESH_SECONDS = 60
GH_POLL_SECONDS = 90
MAX_POLL_CALLS = 30            # budget cap per poll cycle (poll_ship discipline): the tail of
                               # an oversized fetch list simply waits for the next cycle
LAUNCH_TIMEOUT = 120           # launch-session.sh verifies delivery within ~30s; be generous
NUDGE_TIMEOUT = 60
RECHECK_TIMEOUT = 600
CLOSE_TIMEOUT = 15             # bound the best-effort close of a stale session's pane (D4)
DELIVERY_RETRY_SECONDS = 120   # min spacing between answer-delivery attempts to one pane
_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"   # SL_CMUX overrides (tests)

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


def detect_self_pane(cmux=None, run=None):
    """The cmux pane THIS process is running in — so `superlooper run` started inside a cmux tab
    targets that tab's OWN pane with zero configuration, and survives a machine restart that
    reassigns pane UUIDs (owner request 2026-07-06: never hardcode a pane id). Worker tabs then
    open as siblings in the runner's own pane (`new-surface --pane`), grouped and watchable — the
    same design the D7 fix requires (runner and workers share one workspace by construction).

    cmux does NOT export a pane id into a tab's shell (only CMUX_SURFACE_ID / CMUX_WORKSPACE_ID),
    so ask cmux directly: `identify` returns a `caller` object naming the INVOKING tab's `pane_id`.
    `--id-format uuids` is required — without it `pane_id` comes back null. Returns "" when cmux is
    unreachable or we're not inside a cmux surface (a detached/launchd start): the caller then
    falls back to an explicit SL_PANE, and preflight_pane fails hard if neither resolves."""
    cmux = cmux or os.environ.get("SL_CMUX", _CMUX_DEFAULT)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        r = run([cmux, "--id-format", "uuids", "identify"])
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if getattr(r, "returncode", 1) != 0:
        return ""
    try:
        data = json.loads(getattr(r, "stdout", "") or "")
    except (ValueError, TypeError):
        return ""
    caller = data.get("caller") if isinstance(data, dict) else None
    pane = caller.get("pane_id") if isinstance(caller, dict) else None
    return pane if isinstance(pane, str) and pane.strip() else ""


class Runner:
    def __init__(self, repo, config, state_home=None, pane=None, agent="claude",
                 run_script=None, fetch_usage=None):
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
        if run_script is not None:
            self._run_script = run_script
        if fetch_usage is not None:
            self._fetch_usage = fetch_usage
        self.stop = False
        self._owns_lock = False
        self._consecutive_tick_errors = 0    # reset on the first clean tick (incident 2026-07-07)
        self._tick_alert_on_disk = False     # the wedge ALERT is confirmed written (retry until so)
        self._tick_alert_notified = False    # the wedge notify+journal fired once this episode
        # DISTINCT issues in the current unbroken run of launch-delivery failures (issue #24). Any
        # verified delivery clears it; >= actions.SYSTEMIC_LAUNCH_FAILURE_CAP distinct ids is a
        # SYSTEMIC launch fault (dead anchor), not N per-issue parks. In-memory on purpose (like
        # _consecutive_tick_errors): it is live runtime health, and a restart — the documented
        # recovery for a wedged anchor — is exactly when it should reset to a clean slate.
        self._launch_fail_ids = set()

        self.state = os.path.join(self.home, "state")
        self.issues_path = os.path.join(self.state, "issues.json")
        for sub in ("state/activity", "state/blocked", "state/exited", "state/awaiting",
                    "state/panes", "state/started", "state/events/processed",
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
        self._usage = {"last_ok": {}, "last_ok_at": None, "first_attempt_at": None,
                       "checked_at": 0}
        self.emitted = self._rebuild_emitted()

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

    def _handle_signal(self, signum, frame):
        # Fail-stopped by design: in-flight sessions untouched, nothing merges while down.
        self.stop = True

    def run(self, max_ticks=None, sleep=time.sleep):
        if not self.acquire_singleton():
            print("another runner is live for this state home — exiting", file=sys.stderr)
            return 1
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
                ticks += 1
                if not self.stop and (max_ticks is None or ticks < max_ticks):
                    sleep(TICK_SECONDS)
        finally:
            self.release_singleton()
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
            status = ist.get("status")
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
            pv = gh.pr_for_branch(branch)
            if pv.get("number") and budget():
                pv = dict(pv)
                pv["comments"] = gh.pr_comments(pv["number"]).comments
            prs[iid] = pv

        self._parsed_by_id = parsed_by_id
        self._raw_by_id = raw_by_id
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
          - NEVER downgrade: gh.pr_for_branch fails CLOSED to {} on a transient blip, so only a
            POSITIVE find (a PR with a number) updates the view — a {} never overwrites a known PR
            (that overwrite, run every tick, would re-park completed work — the exact bug this fixes).
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
            if ist.get("status") in actions.TERMINAL_STATUSES:
                continue
            finished = (os.path.exists(os.path.join(self.home, "reports", f"{iid}.md"))
                        or ist.get("status") in ("gating", "holding"))
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
            pv = gh.pr_for_branch(branch)
            if pv.get("number"):             # POSITIVE find only — a transient {} never erases a cache entry
                pv = dict(pv)
                pv["comments"] = gh.pr_comments(pv["number"]).comments
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
            if ist.get("type") != "investigate" or ist.get("status") in actions.TERMINAL_STATUSES:
                continue
            finishing = (os.path.exists(os.path.join(self.home, "reports", f"{iid}.md"))
                         or ist.get("status") in ("gating", "holding"))
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

    # ------------------------- the tick -------------------------

    def tick(self, now=None):
        now = time.time() if now is None else now
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
            freeze_secs=session.get("freeze_seconds", events_mod.FREEZE_SECONDS))
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
        if self._wants_launch():
            disk["launch_anchor"] = self._anchor_status()

        lane_state = actions.lane_state_from(st)
        acts = actions.decide(now, self.config, self.usage_view(),
                              list(self._parsed_by_id.values()), lane_state, evs, disk,
                              self.gh_view)
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

        # Heartbeat = "a full tick completed", stamped LAST (incident 2026-07-07). It used to be
        # stamped at the TOP of the tick, so a tick that crashed part-way still read as freshly
        # alive and the dashboard's dead-man's switch never fired through a 42-min wedge. Now a
        # wedged tick lets the heartbeat go stale. Note the split: runner.lock (the pidfile) says
        # the PROCESS is up; runner.heartbeat says the loop is making PROGRESS — different signals,
        # on purpose. The external-watchdog contract in references/runner-ops.md matches this.
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
        recorded pane (a first launch) or the surface is already gone (a crashed session)."""
        surface = self._surface(iid)
        if surface:
            ws = (_read(os.path.join(self.state, "panes", f"{iid}.ws")) or "").strip()
            args = [os.environ.get("SL_CMUX", _CMUX_DEFAULT), "close-surface", "--surface", surface]
            if ws:
                args += ["--workspace", ws]
            self._run_script(args, timeout=CLOSE_TIMEOUT)      # best-effort; rc ignored (dead surface = no-op)
        for p in (os.path.join(self.state, "panes", iid),
                  os.path.join(self.state, "panes", f"{iid}.ws"),
                  os.path.join(self.state, f"worker.{iid}.lock")):
            _rm(p)

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

    def _exec_launch(self, a, now):
        iid, num, branch = a["id"], a.get("num"), a.get("branch")
        p = self._parsed_by_id.get(iid)
        if p is None:
            return "skipped: issue not in the current GitHub view"
        self._update_issue(iid, {"branch": branch, "num": num, "type": p.get("type"),
                                 "declared_touches": list(p.get("touches") or [])})
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
            self._update_issue(iid, {"status": "running"})
            self._launch_fail_ids.clear()              # a verified delivery proves the anchor is live
            return "ok"
        # Delivery NOT verified: bump the per-issue cap AND record this id in the runner-level
        # anchor streak (issue #24) — decide reads the streak to tell a dead anchor (many distinct
        # ids) from a genuinely bad issue (one), and hold the queue instead of walking it into parks.
        self._launch_fail_ids.add(iid)
        self._update_issue(iid, {"status": "ready"},
                           fn=lambda st, i: self._bump(i, "launch_failures"))
        return f"launch rc={rc} (delivery not verified)"

    def _exec_hire_answerer(self, a, now):
        iid, aid, question = a["id"], a["answerer_id"], a["question"]
        _, answerer_model = self._models()
        answers_dir = os.path.join(self.home, "answers")
        _rm(os.path.join(answers_dir, f"{iid}.md"))    # a stale answer must never answer a NEW question
        template = _read(os.path.join(_TEMPLATES, "answerer-brief.md")) or ""
        body = (self._raw_by_id.get(iid) or {}).get("body", "")
        text = _sub(template, {"issue_num": str(a.get("num")), "issue_body": body,
                               "question": question, "worktree": self._worktree(iid),
                               "answer_path": os.path.join(answers_dir, f"{iid}.md")})
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
        body = ("**Bounced by the worker session** (premise-level drift found at launch-time "
                "reconciliation). The worker's memo, verbatim:\n\n"
                f"{a.get('memo', '')}\n\n"
                "_Labels moved by the runner. The proposed amendment above is ready to approve "
                "or reject — one touch._")
        if not gh.comment(num, body):
            return "memo comment failed (will retry next tick)"
        if not gh.set_labels(num, add=["needs-william"], remove=["in-progress"]):
            return "label move failed (will retry next tick)"
        _rm(os.path.join(self.state, "blocked", iid))
        self._update_issue(iid, {"status": "bounced"})
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
        label = "needs-william" if a.get("needs_william") else "parked"
        gh.comment(num, f"**superlooper parked this issue** — {a.get('memo', '')}")
        if not gh.set_labels(num, add=[label], remove=["in-progress", "agent-ready"]):
            return "label move failed (will retry next tick)"
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
        #    launch-session.sh recreates the worktree, _close_stale_session frees the pane/lock.
        gitops.worktree_remove(self.repo, self._worktree(iid))
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
                      "merge_refusal_reason": None})   # paired with merge_refusals=0 above (#27)
            recs = st.get("answerers")
            if isinstance(recs, dict):
                for aid in [k for k, v in recs.items()
                            if isinstance(v, dict) and v.get("for") == iid]:
                    recs.pop(aid, None)
        self._update_issue(iid, fn=reset)
        journal.append(self.home, {"act": "reapprove", "id": iid, "old_counters": old}, now)
        # Clear the park-family labels William re-approved past; the next tick's launch moves
        # agent-ready -> in-progress. Best-effort: a gh blip only leaves a cosmetic stale label,
        # never blocks the relaunch (phase E keys off agent-ready, not the parked label).
        gh.set_labels(num, remove=["parked", "needs-william"])
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
                self._launch_fail_ids.clear()          # a verified delivery proves the anchor is live (#24)
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
            gitops.worktree_remove(self.repo, self._worktree(iid))
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
        gitops.worktree_remove(self.repo, self._worktree(iid))
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
                                 "merge_refusal_reason": None})
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
            self._launch_fail_ids.clear()              # a verified delivery proves the anchor is live (#24)
            return "ok"
        self._update_issue(iid, fn=lambda st, i: self._bump(i, "launch_failures"))
        return f"conflict-session launch rc={rc}"

    def _exec_close_investigate(self, a, now):
        iid, num = a["id"], a.get("num")
        if not gh.close_issue(num, comment="Investigation complete — the root-cause report is "
                                           "the marker comment above; child issues (if any) "
                                           f"carry `parent: #{num}` and await William's "
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
            gitops.worktree_remove(self.repo, self._worktree(iid))
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
        view = {"date": date, "now": now, "frozen": frozen, "queue": queue,
                "usage": self.usage_view()}
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
