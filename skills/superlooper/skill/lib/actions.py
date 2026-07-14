"""The runner's brain as data: decide() maps one tick's full view of the world to an ordered
list of actions. PURE — no gh, no subprocess, no disk, no clock reads — so the entire spec-§5
failure model is a unit-test scenario table (tests/test_actions.py) and runner.py is a dumb
executor of what this returns.

Design commitments (all bought in prior runs, all tested):

  * STATE-driven, not event-driven: decisions derive from GitHub views + disk markers +
    loopstate, so a restarted runner fed a COLD state reconstructs every in-flight decision
    (spec §5 "runner death: restart rebuilds from GitHub + disk"). Events (events.py) only
    add the edge-triggered liveness tiers (idle/frozen), whose response is a safe peek/ladder.
  * FAIL CLOSED on wrong-typed input: every view field that isn't the expected shape lands on
    the safe action (do nothing / wait / park-to-William), never an exception into the tick and
    never a trusting default. The gh view is stale-unless-explicitly-fresh: gate, launch, and
    orphan decisions all require `gh_view["stale"] is False`.
  * No mutation of any input, no module-level mutable state: same inputs -> same output, twice.
  * NOTIFY IS A STANDING RULE (owner directive): every transition to parked/needs-william,
    every freeze, and every new ALERT emits {"act": "notify"} in the same action list. The
    scenario table asserts this per scenario. ONCE per (issue, park-cause) episode (issue #61):
    when a park's own label move keeps failing, the park re-emits every tick (the labels must
    converge) but as a marked SILENT retry — the 2026-07-08 storm re-texted one park 41 times.
  * Label mechanics are runner-side only (cross-review C2): bounce/park/reclaim/relabel actions
    carry the label payloads; no worker is ever asked to move a label.

The view contract (assembled by runner.py each tick):

  now            epoch seconds.
  config         the validated per-repo config (config.load()).
  usage          usage.fetch_claude_usage() result + {"last_ok_at": epoch of the last SUCCESSFUL
                 fetch (None if never), "first_attempt_at": epoch}. decide splits the two dark-meter
                 cases (issue #46): a meter that successfully READS exhausted (a fresh 'ok' at/over
                 the ceiling) fails CLOSED; a meter that is UNREADABLE past USAGE_FAIL_OPEN_GRACE_
                 SECONDS FAILS OPEN (launch, journal once, alert once). age > USAGE_STALE_SECONDS
                 (within the grace) still launches nothing (fail closed); once dark past the grace,
                 the same crossing FAILS OPEN and raises the usage_stale ALERT together.
  parsed_issues  issues.parse_issue() dicts for the union of open `agent-ready` and open
                 `in-progress` issues (deduped). Wrong-typed nums are skipped here — an issue
                 that can't be identified can't be safely acted on.
  lane_state     [{"id", "touches", "type"?}] for currently occupied lanes — lane_state_from()
                 builds it. Finished-but-unmerged territory claims are derived separately from
                 issues_state; they do not consume lane capacity.
  events         this tick's events.detect_events() output.
  disk           {"issues_state": loopstate dict, "blocked": {id: text}, "reports": {id: text},
                  "answers": {id: text}, "exited": {id: marker-text}, "frozen": dict|None,
                  "alert": dict|None, "live_lock_ids": iterable of ids with a LIVE worker lock,
                  "filed_fingerprints": {fingerprint: issue_num},
                  "local_date": "YYYY-MM-DD", "local_hhmm": "HH:MM",
                  "last_report_date": str|None}
  gh_view        {"stale": bool (fresh ONLY when exactly False), "consecutive_failures": int,
                  "closed_nums": set, "prs": {id: pr_view (+comments attached ONLY on a clean
                  CommentRead; a refused/starved comments read leaves 'comments' ABSENT, so the
                  build gate HOLDs via await_comments_read, issue #78); {} = GitHub ANSWERED "none
                  exists"; KEY ABSENT = no trustworthy lookup — not fetched yet, or REFUSED (the
                  poll omits a refused PrRead, issue #61), so the build gate HOLDs via
                  await_pr_read, bounded, never an immediate park}, "issue_comments": {id: [...]}
                  — an entry is present ONLY for a CLEAN read (issue #21); a refused/starved
                  investigate read is OMITTED, so a KEY-ABSENT investigate id means "no
                  trustworthy read this tick" -> HOLD via await_read, never park,
                  "dev_checks": the branch's full check universe — check-runs
                  {name,status,conclusion} AND commit statuses {context,state} (issue #23; key
                  absent = not fetched)}

Action vocabulary (the executor contract, one journal record each):
  launch, hire_answerer, deliver_answer, bounce, recover(tier=idle|frozen|exited), gate,
  merge, update, nudge, hold, await_read, await_pr_read, clear_pr_read, await_comments_read,
  clear_comments_read, note_checks_pending,
  clear_checks_pending, park, clear_park_marker, regenerate, resolve_conflict,
  close_investigate, absorb_close, reclaim, relabel, freeze, unfreeze, file_fix_issue, alert,
  clear_alert, morning_report, notify. Safety actions (alert/freeze/unfreeze) come first; launches come
  LAST. `note_checks_pending`/`clear_checks_pending` stamp/clear the bounded pending-checks
  clock (issue #26). `await_read` is the investigate-gate's HOLD when this tick has no
  trustworthy comment read (refused or starved): it journals the wait ONCE per episode (deduped
  on the issue's `read_waited` flag) so a finished investigation is never parked on an
  unverified read and never waits silently (#21). `await_pr_read`/`clear_pr_read` are the
  build-gate siblings for a refused PR lookup (issue #61): the stamp doubles as the bound clock
  (PR_READ_HOLD_CAP_SECONDS -> park once) and the journal-once dedup. `await_comments_read`/
  `clear_comments_read` are the same siblings for a PR that IS found but whose comments sub-read
  was refused/starved (issue #78): the gate's comments-absent WAIT, journaled once per episode on
  the `comments_read_pending_since` clock, park-once past the same bound. `clear_park_marker` ends a
  notify-once park episode whose label move never landed, so a later genuine park texts again.
  `absorb_close` (issue #108) settles a bounced/parked issue the owner CLOSED on GitHub mid-episode
  to a concluded terminal state and stands the episode down (no more label writes / texts).
"""
import math

import brief
import config as _config
import events as events_mod
import gate
import issues as issues_mod
import scheduler

# Staleness / cap constants. The caps that PARK are deliberately small (park is cheap and safe:
# one William-touch re-releases); the thresholds that ALERT sit past the caps (a doom loop must
# be loud, but only a real one).
USAGE_STALE_SECONDS = 300          # no fresh usage for 5 min -> cached reading too old to gate as
                                   # FRESH; WITHIN the fail-open grace below, launches fail CLOSED.
# The bounded grace before a DARK usage meter flips from fail-closed to fail-OPEN (issue #46). Past
# this, an UNREADABLE meter (api_error / no_keychain / auth_expired — a TLS/Keychain/auth outage,
# live incident 2026-07-10) is treated as unreadable-NOT-exhausted: launch normally so work
# continues, rather than freezing the whole loop while real usage is low. PROTECTS AGAINST two
# failure modes at once: (a) the doom-loop of stopping everything on a meter we merely cannot READ
# (the reported defect); (b) flapping on a brief blip — a dark meter still fails CLOSED for the
# first half hour, riding out transient outages before ever launching blind. It is also the alert
# threshold: the usage_stale ALERT fires at the exact moment fail-open engages, so the meter is
# never dark-and-launching silently. A meter that successfully READS exhausted (a fresh 'ok' fetch
# at/over the ceiling) is unaffected — that still fails CLOSED; only an UNREADABLE meter fails open.
USAGE_FAIL_OPEN_GRACE_SECONDS = 1800
GH_ALERT_FAILURES = 10             # consecutive failed poll cycles (~15 min at 90 s) -> ALERT
ANSWERER_TIMEOUT_SECONDS = 900     # the answerer's 15-min freeze tier = its timeout
RECOVER_RETRY_SECONDS = 600        # frozen-session recovery ladder re-fires at most every 10 min
LAUNCH_FAILURE_CAP = 2             # launch never delivered twice -> park (RC-LAUNCHVERIFY x2)
# A dead LAUNCH ANCHOR (the cmux pane every worker tab is born in) is a RUNNER-level fault, never
# N per-issue parks (incident 2026-07-09: a dead anchor walked 10 approved issues into 10 parks in
# ~8 min). When this many DISTINCT issues fail launch-delivery back-to-back — the runner's streak,
# any verified delivery clears it — it is systemic, not issue-specific: hold launches, one alert,
# the queue left intact. 2 distinct issues failing consecutively already outstrips any single bad
# issue (which the per-issue LAUNCH_FAILURE_CAP handles), so the trip is early and the queue is
# spared. Kept below/at the pane-probe path (`launch_anchor`), which catches a dead anchor directly.
SYSTEMIC_LAUNCH_FAILURE_CAP = 2    # >= this many DISTINCT issues failing delivery -> systemic (#24)
# A tripped systemic-launch breaker (#24) cannot clear itself: the streak clears ONLY on a VERIFIED
# delivery, yet the hold suppresses every delivery, so the loop sits healthy-but-frozen until a
# manual restart (live 2026-07-13 — three failures held, the owner's 20:21 re-approve launched
# nothing). Re-arm the breaker: while the hold stands, every CANARY_RETRY_SECONDS attempt ONE canary
# launch of the front-of-queue issue as a probe. A verified delivery clears the streak and normal
# launching resumes; a failed canary re-enters the hold, charging NO per-issue launch cap and parking
# nothing. Spaced generously — a probe every few minutes catches recovery without hammering a
# genuinely dead anchor — and the interval is measured from the LAST delivery failure, so the first
# canary waits a full interval after the trip rather than firing the instant the hold engages (#115).
CANARY_RETRY_SECONDS = 300
ANSWERER_FAILURE_CAP = 2           # answerer hire failed twice -> park the issue
DELIVERY_FAILURE_CAP = 3           # answer would not deliver to the pane -> park the issue
MERGE_REFUSAL_CAP = 2              # gate-green PR's merge refused this many ticks -> park (#27)
UPDATE_ERROR_ALERT = 4             # persistent merge-update infra errors -> ALERT (never regenerate)
RUNAWAY_THRESHOLD = 4              # retries far past the cap -> ALERT (events.retry_runaway)
# The bound (seconds) on a FINISHED issue's required-checks PENDING wait (issue #26). A required
# check that never reports reads as pending forever, and the wait had no timer — so an unreported
# check kept a green PR gating with no park/memo/notify. Past this, the runner escalates ONCE to
# needs-william naming the unreported checks. Mirrors config's default; used only when the config
# omits/corrupts session.checks_pending_cap (an unbounded wait is the bug, so a bad value still
# bounds — never disables).
CHECKS_PENDING_CAP_DEFAULT = 10800
# The bound (seconds) on a FINISHED build's refused-PR-lookup HOLD (issue #61). During an hourly
# GraphQL dead zone the lookup is refused, not answered — the gate HOLDs (safe idle: no park, no
# text) rather than mistaking the refusal for "no PR exists" (the 2026-07-08 storm). The account's
# GraphQL window refills at a fixed minute past each hour, so a dead zone lasts ~11 min at most;
# 15 min outlasts it with margin. Past the bound the gate parks ONCE — fail-to-owner is preserved,
# it just stops being per-tick.
PR_READ_HOLD_CAP_SECONDS = 900
# The bound (seconds) on a park whose LABEL MOVE keeps failing (issue #61 / incident §4). The park
# already texted once (notify-once marker); silent retries past this bound mean GitHub writes are
# genuinely stuck — ALERT-worthy: one more text, not zero and not twenty.
PARK_LABEL_STUCK_ALERT_SECONDS = 600

# The red-nightly standing rule's EXACT label set (spec §4.4, owner-defined 2026-07-02). The
# distinct `auto-approved:nightly-red` label is what makes this auto-approval auditable as
# standing-rule work, not an agent applying William's word. Do not add, drop, or reorder.
FIX_ISSUE_LABELS = ["type:diagnose-and-fix", "agent-ready", "auto-approved:nightly-red", "expedite"]

# Statuses that occupy a lane (a blocked/frozen/exited session still owns its worktree+branch)
# vs statuses from which a (re)launch is legitimate. gating/holding hold NO lane: the build is
# done and only merge mechanics remain, so a lane frees the moment the report lands.
INFLIGHT_STATUSES = {"running", "blocked", "frozen", "exited"}
TERRITORY_CLAIM_STATUSES = INFLIGHT_STATUSES | {"gating", "holding"}
TERMINAL_STATUSES = {"merged", "parked", "needs_william", "bounced"}
RELAUNCHABLE_STATUSES = {None, "ready", "parked", "needs_william", "bounced"}
# The park-family terminal statuses a FRESH `agent-ready` re-releases. Re-approval is William's
# word again (spec §2: the label records his word, it is never the decision), so it is a fresh
# cap: the runner zeroes the per-issue attempt counters and re-releases to `ready` (see the
# `reapprove` action). `merged` is deliberately excluded — merged work is truly done; a stray
# label on it must never resurrect and rebuild it.
REAPPROVAL_STATUSES = {"parked", "needs_william", "bounced"}

NUDGE_MESSAGES = {
    "sections": "Your report is missing required sections (or they carry no real prose). "
                "Rewrite the report with substantive text under every required H2, then finish "
                "again — the runner checks mechanically.",
    "review": "The gate found no review evidence. Get a fresh-agent review of your diff and "
              "post its verdict as a PR comment BEGINNING `<!-- superlooper-review -->` "
              "(what was reviewed + P0/P1 outcome). The runner will not merge without it.",
    "checks": "A required check failed on your PR. Investigate the failure, fix it, and push — "
              "the gate re-runs automatically.",
    "investigation": "Post your root-cause report as an issue comment BEGINNING "
                     "`<!-- superlooper-investigation -->` — the runner closes the parent only "
                     "when that marker comment exists.",
}

# Human-readable ALERT notify bodies. The reason CODES (stable, sorted) are what the ALERT file
# stores and what decide dedups on; these strings are only the push text. A reason not listed here
# (gh_unreachable, launch_runaway:<id>, update_errors:<id>) falls back to its own code.
ALERT_MESSAGES = {
    "usage_stale": "usage meter unreadable past the grace — FAILING OPEN: launching normally so "
                   "work continues; real usage may be low, and sessions hit the wall themselves if "
                   "quota is genuinely gone. Three known causes, most→least common: (1) EXPIRED "
                   "CLAUDE AUTH — re-login (the OAuth token in the macOS Keychain has expired); "
                   "(2) a STALE PINNED CLIENT VERSION — bump USER_AGENT_VERSION in skill/lib/usage.py "
                   "to the current claude-code/<version> (a stale User-Agent is silently 403'd); "
                   "(3) BROKEN TLS TRUST in the invoking Python — a python.org framework install "
                   "fails every HTTPS with CERTIFICATE_VERIFY_FAILED until you run its "
                   "'Install Certificates.command'. Diagnose with `superlooper doctor`; gating "
                   "resumes automatically once the meter reads again.",
    "launch_anchor_down": "launch anchor gone — restart superlooper in a visible cmux tab. The "
                          "launch queue is held intact; every approved issue keeps agent-ready and "
                          "launches resume automatically once the tab's pane resolves again.",
    "launch_systemic_failure": "launches are failing delivery across multiple issues — a systemic "
                               "launch fault, not an issue-specific one. The queue is held intact "
                               "(nothing parked). The usual cause when this trips after you walk "
                               "away is macOS App Nap suspending an idle/occluded cmux: it still "
                               "answers new-surface but defers spawning the tab's shell past the "
                               "verify window, so no worker starts. Fix: run "
                               "`defaults write com.cmuxterm.app NSAppSleepDisabled -bool true` "
                               "(or re-run bin/install-launch-shim.sh), then FULLY QUIT and "
                               "relaunch cmux in a visible tab and restart the runner — the flag is "
                               "read only at app launch. If it persists, check the cmux anchor.",
}


def _alert_message(reason):
    if isinstance(reason, str) and reason.startswith("park_label_stuck:"):
        iid = reason.split(":", 1)[1]
        return (f"{iid} handed back to the owner but its label move has been failing for "
                f"{PARK_LABEL_STUCK_ALERT_SECONDS // 60}+ min — GitHub writes are not landing. "
                "The hand-back (park or bounce) already texted once; retries continue silently. "
                "Check GitHub availability / rate limits (`gh api rate_limit`).")
    return ALERT_MESSAGES.get(reason, reason)


LAUNCH_STDERR_MEMO_MAX = 1200      # chars of a failed launch's stderr tail carried into a park memo


def _launch_stderr_memo(tail):
    """Format the captured launch-stderr tail for a relaunch-cap park memo, or "" when there is
    nothing usable (issue #40). A launch that dies immediately — bad --model, a renamed/dropped CLI
    flag — writes its real reason to stderr and vanishes with the doomed tab; start-session.sh
    captures a bounded tail so this memo can NAME the error instead of only "relaunched N times".
    Fail-open on wrong-typed input (never raise into the tick): a non-string / blank tail yields no
    addendum. Bounded to the LAST chars — the tail carries the actual error, and a park memo must
    never become an unbounded stderr dump (start-session.sh already byte-bounds the file; this is
    the second, memo-side bound)."""
    if not isinstance(tail, str):
        return ""
    t = tail.strip()
    if not t:
        return ""
    if len(t) > LAUNCH_STDERR_MEMO_MAX:
        t = "…" + t[-LAUNCH_STDERR_MEMO_MAX:]
    return "\n\nlaunch stderr (tail — the agent's own error just before it exited):\n" + t


def _fail_open_reason(dark_age):
    """The bounded journal record for entering a fail-open episode (issue #46): a fixed sentence
    plus the darkness duration in whole minutes. Bounded so a long outage journals ONE record, not
    a growing one."""
    mins = int(dark_age // 60) if math.isfinite(dark_age) else None
    span = f"{mins} min" if mins is not None else "an unbounded span"
    return (f"usage meter unreadable for {span} (past the {USAGE_FAIL_OPEN_GRACE_SECONDS // 60}-min "
            "grace) — FAILING OPEN: launching normally so work continues. A dark meter is treated as "
            "unreadable, not exhausted; if quota is genuinely gone the sessions hit the wall "
            "themselves and #24's systemic breaker trips. A meter that successfully reads exhausted "
            "still fails closed.")


def _real(x):
    """A usable number: int/float, not bool, finite."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _count(x, default=0):
    """A usable counter: a real int (bool excluded), else the default. Wrong-typed counters are
    the fail-OPEN defect class — callers decide whether default or park is the safe landing."""
    return x if type(x) is int else default


def _since_ok(since, now):
    """A USABLE pending-checks clock: a real number in [0, now] (issue #26, Codex R1). The runner
    only ever writes `now`, so a FUTURE value (would make now-since negative and defeat the cap —
    an unbounded wait again) or a NEGATIVE one (would make now-since huge and escalate spuriously)
    is corrupt. Treat it as unstamped so it re-stamps, never trusted to defeat OR trip the bound."""
    return _real(since) and 0 <= since <= now


def _in_owner_handback_episode(ist, blocked_text):
    """True iff the issue is in an owner-handback episode — a park or a bounce that is handing the
    issue back to the owner (issue #108). Either it has already SETTLED into an owner-decision
    status, or it is mid-handback with the durable marker / BOUNCED memo present (the storm states,
    where a label move keeps failing and status has not settled). Scopes external-close absorption
    to exactly these episodes, so a normal merge-close (status 'merged') or a plain running build is
    never mistaken for the owner's Drop. A 'merged' status short-circuits to False FIRST: it is the
    settled-DONE bucket (a real landing, an absorbed out-of-band merge, OR this issue's own
    absorb_close), so it must never re-absorb — even if a crash left a stale park_notify_cause behind
    (which clear_park_marker would otherwise mop up). This also makes absorb_close idempotent: once it
    settles an issue to 'merged', this returns False and the absorb never re-fires."""
    ist = ist if isinstance(ist, dict) else {}
    status = ist.get("status")
    if status == "merged":
        return False
    if status in ("bounced", "parked", "needs_william"):
        return True
    if isinstance(blocked_text, str) and blocked_text.lstrip().startswith("BOUNCED:"):
        return True
    return isinstance(ist.get("park_notify_cause"), str)


def _checks_pending_cap(config):
    """session.checks_pending_cap seconds — the bound on a finished issue's pending-checks wait
    (issue #26). Corrupt/missing -> the module default, never disabled: an unbounded wait is the
    bug this closes, so a bad value must still bound."""
    ses = config.get("session") if isinstance(config, dict) and isinstance(config.get("session"), dict) else {}
    v = ses.get("checks_pending_cap")
    return v if type(v) is int and v >= 0 else CHECKS_PENDING_CAP_DEFAULT


def _counter(ist, key):
    """(value, corrupt) for a persisted cap counter. MISSING legitimately means 0; a PRESENT
    non-int value — including an explicit null, which nothing in this system ever writes —
    is corruption, and the caller must land on the safe action (park/alert), never re-allow
    the capped action by reading it as 0 (Codex cross-review rounds 1+2 — the
    fail-OPEN-on-wrong-TYPED defect class)."""
    if not isinstance(ist, dict) or key not in ist:
        return 0, False
    v = ist[key]
    if type(v) is int:
        return v, False
    return 0, True


def _iid_num(iid):
    """i<N> -> N, else None (a loopstate key that isn't an issue id is corruption, skipped)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _sorted_ids(ids):
    return sorted(ids, key=_iid_num)


def _dget(d, key, want):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, want) else want()


# --- touches_required (issue #36): the knob now ACTS at launch/intake ---
_MERGE_PRODUCING_TYPES = {"build", "diagnose-and-fix"}   # investigations produce no PR/merge


def _declares_touches(p):
    """True iff the parsed issue declares a non-empty `touches:` (a list with >=1 non-blank area).
    A literal '*' counts as declared — it is an explicit unknown-scope declaration, and its
    serialization cost is journaled separately (wildcard_hold), not refused here."""
    t = p.get("touches") if isinstance(p, dict) else None
    return isinstance(t, list) and any(isinstance(x, str) and x.strip() for x in t)


def _touches_required(cfg):
    """Fail SAFE to enforcement: a config missing the key or carrying a non-bool (corruption the
    loader would reject, but decide is defensive of every input) enforces, matching the loader
    default of True — never silently launch a no-touches issue on a garbled config."""
    tr = cfg.get("touches_required") if isinstance(cfg, dict) else None
    return tr if isinstance(tr, bool) else True


def _touches_required_memo(num):
    return (f"issue #{num} is approved (agent-ready) but its `## Loop metadata` declares no "
            "`touches:` line, and this repo sets `touches_required: true`. superlooper will not "
            "launch it until the issue declares which area(s) it touches (e.g. `touches: engine`): "
            "the declaration is what anti-affinity and the wander check verify against. Add a "
            "`touches:` line to the Loop metadata and re-approve.")


def _wildcard_hold_reason(h):
    """Prose for a wildcard launch-suppression (issue #36): why this approved issue could not
    co-schedule and the lane serialized. Names which side is the no-touches wildcard."""
    blocker = h.get("blocker_id")
    if h.get("self_wildcard") and h.get("blocker_wildcard"):
        why = (f"it and in-flight lane {blocker} both declare no `touches:` (wildcard '*'), which "
               "overlaps every lane under hard affinity")
    elif h.get("self_wildcard"):
        why = ("it declares no `touches:` (wildcard '*'), which overlaps every lane under hard "
               "affinity")
    else:                                       # blocker_wildcard
        why = (f"in-flight lane {blocker} declares no `touches:` (wildcard '*'), which overlaps "
               "every lane under hard affinity")
    return (f"launch held: {why} — so it cannot co-schedule and the lane serializes. This is why "
            "only one lane is busy; declare a narrower `touches:` (or add matching `areas`) to let "
            "lanes run in parallel.")


def lane_state_from(issues_state):
    """[{"id", "touches", "type"?}] for every issue whose status occupies a lane, sorted by issue
    number (deterministic). Pure; wrong-typed state or entries degrade to no lanes / no touches."""
    issues = _dget(issues_state, "issues", dict)
    out = []
    for iid in _sorted_ids(k for k in issues if _iid_num(k) is not None):
        ist = issues.get(iid)
        if isinstance(ist, dict) and ist.get("status") in INFLIGHT_STATUSES:
            touches = ist.get("declared_touches")
            touches = [t for t in touches if isinstance(t, str)] if isinstance(touches, list) else []
            lane = {"id": iid, "touches": touches}
            itype = ist.get("type")
            if isinstance(itype, str) and itype:
                lane["type"] = itype
            out.append(lane)
    return out


def territory_claims_from(issues_state):
    """[{"id", "touches", "type"?}] for merge-producing issues whose declared territory is still
    protected, sorted by issue number. This deliberately outlives the lane for gating/holding
    issues, but releases on merge, regenerate/requeue (`ready`), and park-family terminal states.
    Terminal parks release even wildcard/no-touches territory so a no-touches repo cannot freeze."""
    issues = _dget(issues_state, "issues", dict)
    out = []
    for iid in _sorted_ids(k for k in issues if _iid_num(k) is not None):
        ist = issues.get(iid)
        if not isinstance(ist, dict) or ist.get("status") not in TERRITORY_CLAIM_STATUSES:
            continue
        if ist.get("type") == "investigate":
            continue
        touches = ist.get("declared_touches")
        touches = [t for t in touches if isinstance(t, str)] if isinstance(touches, list) else []
        claim = {"id": iid, "touches": touches}
        itype = ist.get("type")
        if isinstance(itype, str) and itype:
            claim["type"] = itype
        out.append(claim)
    return out


def _issues_state_corrupt_for_launches(issues_state):
    """True when persisted issue state is structurally unreadable enough that fresh launches must
    stop. Missing state is a cold start and is allowed; present-but-wrong-typed state could hide a
    held territory claim, so launches fail closed for that tick."""
    if issues_state is None:
        return False
    if not isinstance(issues_state, dict):
        return True
    if "issues" not in issues_state:
        return bool(issues_state)
    issues = issues_state.get("issues")
    if not isinstance(issues, dict):
        return True
    return any(_iid_num(iid) is not None and not isinstance(ist, dict)
               for iid, ist in issues.items())


def _failing_required(dev_checks, required):
    """(name, conclusion) of the first REQUIRED check reporting a failing state, in the config's
    declared order (deterministic), else None."""
    if not isinstance(dev_checks, list) or not isinstance(required, list):
        return None
    by_name = {}
    for c in dev_checks:
        if isinstance(c, dict) and isinstance(c.get("name") or c.get("context"), str):
            by_name.setdefault(c.get("name") or c.get("context"), []).append(c)
    for req in required:
        for c in by_name.get(req, []):
            state = c.get("conclusion") or c.get("state")
            if isinstance(state, str) and state.upper() in gate._CHECK_FAIL:
                return (req, state)
    return None


def dev_fingerprint(dev_checks, required):
    """The durable identity of a red-dev breakage (file the fix issue ONCE per distinct failure,
    L7: fingerprint content, never a commit). Built from the first failing required check's
    name + conclusion — the richest content branch_checks exposes; the nightly (Task 12) adds
    failure text through the same gate.fix_issue_fingerprint."""
    failing = _failing_required(dev_checks, required)
    name, concl = failing if failing else ("dev", "")
    return gate.fix_issue_fingerprint(name, concl)


def _fix_issue(dev_branch, name, conclusion, fingerprint, operator="the owner"):
    title = f"Restore green: required check '{name}' is red on {dev_branch}"
    body = (
        f"## Goal\n"
        f"The dev mainline `{dev_branch}` has a red required check: `{name}` ({conclusion}).\n"
        f"Diagnose and fix whatever broke it. This issue is scoped STRICTLY to restoring green —\n"
        f"no opportunistic improvements (spec §4.4 red-nightly standing rule).\n"
        f"Failure fingerprint: `{fingerprint}` (auto-filed once per distinct breakage).\n\n"
        f"## Definition of done\n"
        f"- [ ] required check `{name}` is green on `{dev_branch}`\n\n"
        f"## Boundaries\n"
        f"Only the minimal change that restores green. Anything larger becomes a new issue for\n"
        f"{operator} to approve. Merges are frozen until dev is green again.\n\n"
        f"## Loop metadata\n"
        # `touches: *` — a restore-green fix has genuinely unknown scope (whatever broke the check),
        # so the wildcard is the honest declaration. It also satisfies touches_required (issue #36:
        # an EMPTY touches would be refused at launch, deadlocking auto-restore-green since this
        # issue is auto-approved and the mainline is frozen until it lands). '*' serializes under
        # hard affinity — correct for an expedited fix while merges are frozen anyway.
        f"touches: *\n"
    )
    return {"act": "file_fix_issue", "fingerprint": fingerprint, "title": title, "body": body,
            "labels": list(FIX_ISSUE_LABELS)}


def decide(now, config, usage, parsed_issues, lane_state, events, disk, gh_view,
           wake_grace_until=None):
    """One tick's view of the world -> the ordered action list. See the module docstring for
    the full view and action contracts.

    wake_grace_until (issue #42): while now < this deadline the runner just detected a wake gap (a
    tick that landed far later than the ~15s cadence predicts — the laptop slept). During the grace
    the FRESH dark-meter crossing is held (no usage_stale alert / no fail-open) and the frozen
    recovery ladder is held, so the wall-clock jump alone never opens a false-alarm cascade; a
    genuinely dark meter or dead session still alarms once the grace expires. It gates ONLY the
    fresh crossing — an already-established dark episode (prev_dark) is never disturbed, so the grace
    can neither re-open nor falsely close a real outage. None = no grace (the normal case)."""
    if not _real(now):
        return []                      # a tick without a clock decides nothing (fail closed)
    in_wake_grace = _real(wake_grace_until) and now < wake_grace_until

    # ---- defensive coercion of every input (wrong-typed -> safe empty, never a raise) ----
    cfg = config if isinstance(config, dict) else {}
    operator = _config.operator(cfg)          # the owner name every hand-back memo/notify uses (#58)
    session = _dget(cfg, "session", dict)
    retry_cap = _count(session.get("retry_cap"), 2)
    dev_branch = cfg.get("dev_branch") if isinstance(cfg.get("dev_branch"), str) else "main"

    plist = parsed_issues if isinstance(parsed_issues, list) else []
    parsed_by_id = {}
    for p in plist:
        num = p.get("num") if isinstance(p, dict) else None
        if type(num) is int and num > 0:               # bool/str/None num: unidentifiable, skip
            parsed_by_id[f"i{num}"] = p

    lanes_in = [l for l in lane_state if isinstance(l, dict)] if isinstance(lane_state, list) else []
    evs = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    idle_ids = {e.get("id") for e in evs if e.get("type") == "session_idle"}
    frozen_ids = {e.get("id") for e in evs if e.get("type") == "frozen"}

    dsk = disk if isinstance(disk, dict) else {}
    raw_issues_state = dsk.get("issues_state")
    issues_state = raw_issues_state if isinstance(raw_issues_state, dict) else {}
    issue_state_corrupt_for_launches = _issues_state_corrupt_for_launches(raw_issues_state)
    ist_map = _dget(issues_state, "issues", dict)
    blocked = _dget(dsk, "blocked", dict)
    reports = _dget(dsk, "reports", dict)
    answers = _dget(dsk, "answers", dict)
    exited = _dget(dsk, "exited", dict)
    launch_stderr = _dget(dsk, "launch_stderr", dict)   # {id: bounded stderr tail} (issue #40)
    frozen = dsk.get("frozen") if isinstance(dsk.get("frozen"), dict) else None
    alert_on_disk = dsk.get("alert") if isinstance(dsk.get("alert"), dict) else None
    raw_locks = dsk.get("live_lock_ids")
    live_locks = set(raw_locks) if isinstance(raw_locks, (set, frozenset, list, tuple)) else set()

    # ---- usage gating (issue #46): fail CLOSED on a fresh over-ceiling read; fail OPEN on a DARK
    # (unreadable-past-grace) meter. Computed here, after alert_on_disk, because the dark-meter
    # EPISODE is marked by the DURABLE usage_stale ALERT (prev_dark) — the piece that survives a
    # runner restart. ----
    usage_view = usage if isinstance(usage, dict) else {}
    last_ok = usage_view.get("last_ok_at")
    first_attempt = usage_view.get("first_attempt_at")
    usage_age = (now - last_ok) if _real(last_ok) else math.inf
    # A real attempt/success timeline. A malformed/absent usage view (NEITHER timestamp) has none, so
    # the LAUNCH decision below fails CLOSED — fail-open is never triggered by wrong-typed input.
    have_timeline = _real(last_ok) or _real(first_attempt)
    # The dark-meter clock, anchored at the last GOOD read — or, on a cold start that never succeeded,
    # at the first attempt — so a never-read meter still gets the full grace before we launch blind.
    dark_anchor = last_ok if _real(last_ok) else first_attempt
    dark_age = (now - dark_anchor) if _real(dark_anchor) else math.inf
    # meter_fresh: POSITIVE evidence of a CURRENT good read (last good read exists AND is recent).
    # Recovery keys on THIS, never on "not failing open" — which is also true on a cold start, within
    # a fresh grace, and (the trap) on a runner restart mid-outage whose in-memory clock has reset.
    meter_fresh = _real(last_ok) and usage_age <= USAGE_STALE_SECONDS
    # `not in_wake_grace` gates ONLY this fresh crossing (issue #42): a wake gap makes last_ok look
    # ancient purely from the wall-clock jump, so hold the NEW dark episode until the poller lands a
    # fresh fetch (within the grace). The prev_dark continuation below is deliberately NOT gated —
    # the grace must never re-open or falsely close a genuine, already-alerted outage.
    dark_past_grace = have_timeline and dark_age > USAGE_FAIL_OPEN_GRACE_SECONDS and not in_wake_grace
    # prev_dark: is a dark-meter episode ALREADY established? The usage_stale ALERT is DURABLE (it
    # survives a runner restart); the grace clock above is in-memory (resets on restart). Keying the
    # episode's CONTINUATION on the durable marker makes the grace serve ONCE per episode: a restart
    # mid-outage neither re-freezes for a second grace nor reads its reset clock as recovery.
    prev_dark = bool(alert_on_disk) and "usage_stale" in _dget(alert_on_disk, "reasons", list)
    # The dark-meter episode is ACTIVE when the meter first crosses the grace OR an already-established
    # episode still has no fresh read. It CLOSES only on a genuinely fresh read (meter_fresh). The
    # usage_stale ALERT tracks exactly this, so prev_dark next tick stays in lockstep.
    episode_active = dark_past_grace or (prev_dark and not meter_fresh)
    # FAIL OPEN (the LAUNCH policy) = the episode is active AND we have a real timeline. The
    # have_timeline guard keeps a malformed usage view failing CLOSED even mid-episode (never launch on
    # wrong-typed input) while the alert still stands. A meter that successfully READS exhausted has a
    # fresh last_ok_at, so meter_fresh is True, the episode is not active, and failing_open is False:
    # the exhausted-read gate keeps failing closed. The cases are mutually exclusive by construction.
    failing_open = episode_active and have_timeline
    usage_sched = dict(usage_view)
    usage_sched["stale"] = bool(usage_view.get("stale")) or usage_age > USAGE_STALE_SECONDS
    usage_sched["fail_open"] = failing_open
    usage_launchable = scheduler.usage_ok(usage_sched)
    filed = _dget(dsk, "filed_fingerprints", dict)

    gv = gh_view if isinstance(gh_view, dict) else {}
    gh_stale = gv.get("stale") is not False            # fresh ONLY when explicitly False
    prs = _dget(gv, "prs", dict)
    issue_comments = _dget(gv, "issue_comments", dict)
    raw_closed = gv.get("closed_nums")
    closed_nums = set(raw_closed) if isinstance(raw_closed, (set, frozenset, list, tuple)) else set()

    # answerer bookkeeping: an active record per issue; the next id must never collide with an
    # existing one even if the counter is corrupt (scan wins over a wrong-typed counter).
    answerers = _dget(issues_state, "answerers", dict)
    active_answerer = {}
    max_aid = 0
    for aid, rec in answerers.items():
        if isinstance(aid, str) and aid.startswith("a") and aid[1:].isdigit():
            max_aid = max(max_aid, int(aid[1:]))
        if isinstance(rec, dict) and isinstance(rec.get("for"), str):
            active_answerer[rec["for"]] = (aid, rec)
    next_aid = max(_count(issues_state.get("next_answerer"), 1), max_aid + 1)

    out = []
    parked_now = set()
    reapproved_now = set()

    def notify(title, body):
        out.append({"act": "notify", "title": title, "body": body})

    def ist_of(iid):
        v = ist_map.get(iid)
        return v if isinstance(v, dict) else {}

    def park(iid, num, memo, needs_william=False, cause=None):
        # Notify-once per (issue, park-cause) — issue #61. When the park's own label move fails
        # (the 2026-07-08 dead zone failed reads AND writes in lockstep), local status never
        # settles to parked, so decide re-derives this same verdict every tick — and used to
        # re-text every tick (41 texts). The executor stamps `park_notify_cause` durably BEFORE
        # attempting the label move; a re-derived park for the SAME cause is therefore a SILENT
        # retry (the labels still converge — the retry is marked so the journal reads as one park
        # episode, not N parks). A DIFFERENT cause is a new episode and texts again; the marker
        # clears when the issue leaves the failing state (clear_park_marker / reapprove).
        # ORDER IS LOAD-BEARING (Codex review C1): the notify is emitted BEFORE the park action,
        # so the suppression marker (stamped by _exec_park) can only land after the text already
        # went out — a runner crash between the two executors DUPLICATES a text on the next tick,
        # never silently loses it. Fail toward the owner.
        parked_now.add(iid)
        cause = cause if isinstance(cause, str) and cause else memo
        act = {"act": "park", "id": iid, "num": num,
               "needs_william": needs_william, "memo": memo, "cause": cause}
        if ist_of(iid).get("park_notify_cause") == cause:
            act["retry"] = True
            out.append(act)
            return
        who = "needs-owner" if needs_william else "parked"
        notify(f"superlooper: {iid} {who}", memo)
        out.append(act)

    # ---- launch-anchor liveness (issue #24): a dead launch anchor must never walk the queue ----
    # The runner launches every worker as a cmux tab in ONE pane (the anchor). When that pane stops
    # resolving mid-run (the runner's tab dragged to another cmux window), EVERY launch fails
    # delivery and the per-issue cap (2 -> park) walked the whole approved queue into parks + notifies
    # (10 issues in ~8 min, 2026-07-09). That is a SYSTEMIC, runner-level fault — one alert, launches
    # HELD, the queue intact — not N issue-specific parks. Two independent detectors feed one degraded
    # mode; the runner senses both and passes them in the view (decide stays pure):
    #   * launch_anchor: the per-tick pane probe. ONLY an EXPLICIT ok is False degrades — a missing or
    #     wrong-typed probe is treated as ok (fail SAFE for launches: never wedge the whole queue on
    #     absent probe data; the streak below still backstops a truly dead anchor).
    #   * launch_fail_ids: the DISTINCT issues in the current unbroken run of launch-delivery failures
    #     (runner-maintained; any verified delivery clears it). >= SYSTEMIC_LAUNCH_FAILURE_CAP distinct
    #     issues failing back-to-back is systemic; one issue at its own cap is not (and still parks).
    anchor = dsk.get("launch_anchor")
    anchor_down = isinstance(anchor, dict) and anchor.get("ok") is False
    raw_fail_ids = dsk.get("launch_fail_ids")
    fail_ids = {x for x in raw_fail_ids if _iid_num(x) is not None} \
        if isinstance(raw_fail_ids, (list, set, tuple, frozenset)) else set()
    systemic_launch = len(fail_ids) >= SYSTEMIC_LAUNCH_FAILURE_CAP
    # A dead anchor only matters when approved work is held behind it: an agent-ready, not-in-flight
    # issue. Deliberately does NOT exclude an at-cap / corrupt-counter issue — while degraded ITS
    # launch-cap park is SUPPRESSED too (below), so it is part of the held queue the alert must
    # surface, never sat on silently. (A RELAUNCHABLE status still excludes a running-but-stale-
    # labelled issue, whose relabel reconciliation is a separate concern.)
    def _held_queue_member(iid, p):
        labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
        return ("agent-ready" in labels and "in-progress" not in labels
                and ist_of(iid).get("status") in RELAUNCHABLE_STATUSES)
    has_pending_launch = any(_held_queue_member(iid, p) for iid, p in parsed_by_id.items())
    # One degraded mode for both detectors: hold every fresh launch and suppress the per-issue
    # launch-cap park (phases D+E), so the queue is left intact for when the anchor resolves.
    launch_degraded = anchor_down or systemic_launch
    # prev_systemic: was a systemic-launch hold ALREADY established? The launch_systemic_failure
    # ALERT is DURABLE (survives a runner restart), so its presence on disk is the episode marker —
    # the same durable-marker discipline usage's prev_dark uses. Its falling edge (prev_systemic and
    # not systemic_launch) is the recovery the canary probe (or a restart) produces: the streak
    # cleared on a verified delivery while the alert still names it. Journaled once below (#115).
    prev_systemic = bool(alert_on_disk) and "launch_systemic_failure" in _dget(alert_on_disk, "reasons", list)

    # ================= A. alerts (safety first, before any work) =================
    reasons = []
    if _count(gv.get("consecutive_failures")) >= GH_ALERT_FAILURES:
        reasons.append("gh_unreachable")
    reasons += [f"launch_runaway:{iid}"
                for iid in sorted(events_mod.retry_runaway(issues_state, RUNAWAY_THRESHOLD))]
    if episode_active:                                 # dark past the grace -> alert AND (with a
        reasons.append("usage_stale")                  # timeline) fail open, so a dark meter is never
                                                       # silent; the alert stands until a fresh read
    for iid in _sorted_ids(k for k in ist_map if _iid_num(k) is not None):
        errs, corrupt = _counter(ist_of(iid), "update_errors")
        if corrupt or errs >= UPDATE_ERROR_ALERT:
            reasons.append(f"update_errors:{iid}")     # a corrupt counter is alert-worthy too
        # A hand-back (park #61, OR bounce #108 — both reuse this marker) whose label move keeps
        # failing past the bound: the marker is stamped but status never settled terminal, so the
        # silent retries have run long enough to be ALERT-worthy (one text via the standard dedup —
        # never twenty). A terminal status means the move landed (episode over); the marker clearing
        # (recovery / reapprove / absorb_close) drops the reason, which auto-clears the ALERT. Skip an
        # issue the owner has CLOSED on GitHub (fresh proof): its stuck label is moot — absorb_close
        # settles it this same tick — so a "label stuck" text as the owner drops it is pure noise (#108
        # review P2). gh_stale keeps this fail-SAFE: an unproven close never suppresses a real alert.
        stamped = ist_of(iid).get("park_notify_at")
        being_absorbed = not gh_stale and _iid_num(iid) in closed_nums
        if (not being_absorbed
                and ist_of(iid).get("status") not in TERMINAL_STATUSES
                and isinstance(ist_of(iid).get("park_notify_cause"), str)
                and _real(stamped) and now - stamped >= PARK_LABEL_STUCK_ALERT_SECONDS):
            reasons.append(f"park_label_stuck:{iid}")
    if anchor_down and has_pending_launch:             # a dead anchor only matters with work to launch
        reasons.append("launch_anchor_down")
    if systemic_launch:
        reasons.append("launch_systemic_failure")
    reasons.sort()
    if reasons:
        existing = alert_on_disk.get("reasons") if alert_on_disk else None
        if existing != reasons:
            out.append({"act": "alert", "reasons": reasons})
            notify("superlooper ALERT", "; ".join(_alert_message(r) for r in reasons))
    elif alert_on_disk:
        out.append({"act": "clear_alert"})

    # ---- fail-open episode journaling (issue #46), bounded to ONE record per episode ----
    # The dark-meter episode IS the usage_stale-alert episode, so the ALERT-on-disk's usage_stale
    # presence (prev_dark) is the durable episode marker — no new persistent state. Emit a `fail_open`
    # record on the episode's ENTRY edge (now active, not yet recorded) and a `usage_recovered` record
    # on its EXIT edge (was recorded, now closed by a fresh read). Both are journal-only (no label
    # move, no park); the owner notify rides the usage_stale ALERT above. Deduped on prev_dark, so a
    # continuous outage — INCLUDING one spanning a runner restart — journals exactly one open + one
    # close. Recovery closes ONLY on `not episode_active`, which (given prev_dark) means a genuinely
    # fresh read arrived — never merely because a restart reset the in-memory grace clock.
    if episode_active and not prev_dark:
        out.append({"act": "fail_open", "reason": _fail_open_reason(dark_age)})
    elif prev_dark and not episode_active:
        out.append({"act": "usage_recovered",
                    "reason": "usage meter readable again — normal usage gating resumed; the "
                              "fail-open episode is closed."})

    # ---- systemic-launch recovery journaling (issue #115), the exit edge of the #24 hold ----
    # The systemic hold cannot clear itself (the streak clears only on a verified delivery, which the
    # hold suppresses), so the canary probe below re-arms it: a verified canary delivery clears the
    # runner's streak, and THIS tick sees systemic_launch fall while the durable ALERT still names it
    # — the exit edge. Emit ONE journal record; the ALERT itself is retracted by the reasons diff in
    # section A above (it no longer lists launch_systemic_failure). A restart — the documented #24
    # fallback — clears the streak the same way, so it too journals exactly one recovery. Deduped on
    # the durable marker (prev_systemic): once section A clears the alert, the next tick sees no
    # marker and emits nothing.
    if prev_systemic and not systemic_launch:
        out.append({"act": "launch_recovered",
                    "reason": "launch delivery verified again (a canary probe or a restart) — the "
                              "systemic launch streak is cleared and normal launching resumes in "
                              "priority order (unless a separate hold, e.g. a dead launch anchor, "
                              "still stands)."})

    # ================= B. dev mainline: freeze / fix-forward / unfreeze =================
    # Requires a FRESH, PRESENT dev-check view: no data never unfreezes and never freezes —
    # the current freeze state simply persists (frozen-but-building is the safe idle state).
    dev_checks = gv.get("dev_checks")
    if not gh_stale and isinstance(dev_checks, list):
        # The freeze/unfreeze rule reads the DEV-required set only (issue #52): a check that gates
        # PR merges but never reports on the dev branch (a ship status on PR heads only) is excluded
        # via the split config, so it can no longer strand a mainline freeze forever. A flat list
        # gates both surfaces (back-compat via the accessor).
        dev_required = _config.dev_required_checks(cfg)
        dev_state = gate.required_checks_state(dev_checks, dev_required)
        if dev_state == "fail":
            failing = _failing_required(dev_checks, dev_required)
            name, concl = failing if failing else ("dev", "")
            fp = gate.fix_issue_fingerprint(name, concl)
            if not frozen:
                out.append({"act": "freeze", "reason": f"dev checks red: {name} ({concl})",
                            "fingerprint": fp})
                notify("superlooper: merges frozen",
                       f"required check '{name}' is red on {dev_branch}; fix-forward filed, "
                       "building continues")
            if fp not in filed:
                out.append(_fix_issue(dev_branch, name, concl, fp, operator))
        elif dev_state == "green" and frozen:
            out.append({"act": "unfreeze"})

    # ================= C. morning report (fires once per local day) =================
    local_date = dsk.get("local_date")
    local_hhmm = dsk.get("local_hhmm")
    report_time = cfg.get("report_time") if isinstance(cfg.get("report_time"), str) else "08:45"
    if (isinstance(local_date, str) and isinstance(local_hhmm, str)
            and local_hhmm >= report_time and local_date != dsk.get("last_report_date")):
        out.append({"act": "morning_report", "date": local_date})

    # ================= D. per-issue flows, in issue-number order =================
    all_ids = _sorted_ids({k for k in ist_map if _iid_num(k) is not None} | set(parsed_by_id))
    for iid in all_ids:
        ist = ist_of(iid)
        status = ist.get("status")
        # External-close absorption (issue #108): William closing the issue on GitHub — the
        # dashboard's Drop, or a close by hand — WHILE the loop is bouncing/parking it is his
        # answer. Absorb it: settle terminal, stand the episode down (no more label writes, no more
        # texts, no dashboard presence). POSITIVE proof only — a FRESH view whose closed set names
        # this issue (#48: never act on a stale/refused read, which fails closed to still-open);
        # SCOPED to owner-handback episodes so a normal merge-close (status 'merged', its own
        # terminal) or a plain running build is never mistaken for the owner's Drop. Checked before
        # the terminal-status block AND the bounce/park re-derivations below, so it wins over every
        # retry the stuck label would otherwise keep firing.
        if (not gh_stale and _iid_num(iid) in closed_nums
                and _in_owner_handback_episode(ist, blocked.get(iid))):
            out.append({"act": "absorb_close", "id": iid, "num": _iid_num(iid)})
            continue
        if status in TERMINAL_STATUSES:
            # Re-approval (dry-run finding, 2026-07-04): a parked-on-cap issue stays filtered
            # from launches FOREVER — its at-cap counter persists across a re-added `agent-ready`
            # label. But re-approval IS a fresh cap. When a park-family issue carries a fresh
            # `agent-ready`, emit `reapprove`: the executor zeroes the attempt counters (journaling
            # the old ones) and re-releases to `ready`. It is the ONLY action for this issue this
            # tick — the phase-E launch waits one tick so it fires against the reset counters, never
            # the stale at-cap ones (see the `reapproved_now` guard below).
            reapproved = False
            if status in REAPPROVAL_STATUSES:
                p = parsed_by_id.get(iid)
                labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
                if "agent-ready" in labels:
                    out.append({"act": "reapprove", "id": iid, "num": _iid_num(iid)})
                    reapproved_now.add(iid)
                    reapproved = True
            # Reconciliation (issue #21): a PARKED investigation whose marker comment appears on a
            # later SUCCESSFUL read must never be left parked forever — close it. Only a fresh,
            # trustworthy read acts: the view must be fresh AND the read PRESENT (a refused read is
            # OMITTED from issue_comments, so `iid in issue_comments` == "a clean answer this poll",
            # and answered-empty carries no marker so it stays parked). William re-approving (the
            # reapprove branch above) is his explicit word to re-run, so it wins over reconciliation.
            if (not reapproved and status == "parked" and not gh_stale
                    and ist.get("type") == "investigate"
                    and iid in issue_comments
                    and gate.investigation_done(issue_comments.get(iid))):
                out.append({"act": "close_investigate", "id": iid, "num": _iid_num(iid)})
            continue                                   # else re-release happens via labels (phase E)
        num = _iid_num(iid)
        p = parsed_by_id.get(iid)
        blocked_text = blocked.get(iid) if isinstance(blocked.get(iid), str) else None
        has_report = iid in reports
        has_exited = iid in exited
        retries = ist.get("retries", 0)

        # ---- recheck failure: an owner decision, checked before any gate re-run ----
        if ist.get("recheck_failed"):
            park(iid, num, "ship_recheck_cmd failed after the mechanical merge-update — "
                           f"never coached around a fail-closed gate; {operator} decides",
                 needs_william=True, cause="recheck")
            continue

        # ---- finished: the ship gate owns this issue ----
        if has_report:
            if status not in ("gating", "holding"):
                out.append({"act": "gate", "id": iid})
            if gh_stale:
                continue
            itype = (p.get("type") if isinstance(p, dict) else None) or ist.get("type")
            if itype == "investigate":
                if iid not in issue_comments:
                    # No trustworthy comment read this tick: the read was REFUSED (omitted from the
                    # view by the poll) or STARVED (poll budget/throttle). HOLD — never nudge, never
                    # park a finished investigation on an unverified read (issue #21: #8's false-park
                    # off one stale read). Journal ONCE per episode (dedup on read_waited) so the
                    # wait is never silent and the record stays bounded across a long outage.
                    if not ist.get("read_waited"):
                        out.append({"act": "await_read", "id": iid, "num": num,
                                    "reason": "finished investigation: no trustworthy comment read "
                                              "yet (GitHub refused, or the read has not landed) — "
                                              "holding, never parking on an unverified read"})
                    continue
                view_comments = issue_comments.get(iid)
                inv_done = gate.investigation_done(view_comments)
                pv = {}
            else:
                if iid not in prs:
                    # No trustworthy PR read this tick: the lookup was REFUSED (the poll OMITS a
                    # refused read from the view) or has not landed yet. HOLD — never park a
                    # finished build on an unverified lookup: the 2026-07-08 storm parked finished
                    # work as PR-less inside an hourly GraphQL dead zone (issue #61). Stamp the
                    # wait clock ONCE (bounded refusal journaling — one record per episode, not
                    # one per tick); past the bound, park ONCE (fail-to-owner preserved). The
                    # _since_ok discipline re-stamps a corrupt/future clock rather than letting it
                    # defeat or spuriously trip the bound (issue #26).
                    since = ist.get("pr_read_pending_since")
                    if not _since_ok(since, now):
                        out.append({"act": "await_pr_read", "id": iid, "num": num,
                                    "reason": "finished build: no trustworthy PR lookup yet for "
                                              f"branch {ist.get('branch') or '?'} (GitHub refused "
                                              "the read, or it has not landed) — holding, never "
                                              "parking on an unverified lookup"})
                    elif now - since >= PR_READ_HOLD_CAP_SECONDS:
                        park(iid, num,
                             "finished, but GitHub refused every PR lookup for "
                             f"{PR_READ_HOLD_CAP_SECONDS // 60}+ min (rate limit / 403 / 5xx) — "
                             "cannot verify whether a PR exists for branch "
                             f"'{ist.get('branch') or '?'}'. Parked once; if the PR exists it "
                             "stays intact and re-approving after reads recover will pick it up.",
                             cause="pr_read_refused")
                    continue
                pv = prs.get(iid) if isinstance(prs.get(iid), dict) else {}
                if _real(ist.get("pr_read_pending_since")):
                    out.append({"act": "clear_pr_read", "id": iid})   # a trustworthy read landed
                inv_done = False
                if pv.get("state") == "MERGED":
                    # crash window (Codex round-1 C2): the merge landed but the runner died
                    # before settling local state/labels — ABSORB the merged fact
                    # (idempotent), never wedge in gate-wait with a stuck in-progress label.
                    out.append({"act": "absorb_merged", "id": iid, "num": num})
                    continue

            update_result = ist.get("update_result")
            head = pv.get("headRefOid")
            if update_result is not None and isinstance(head, str) \
                    and ist.get("update_head_oid") != head:
                update_result = None                   # stale verdict for a previous head
            declared = (p.get("touches") if isinstance(p, dict) else None) \
                or ist.get("declared_touches") or []
            nudged = ist.get("nudged", [])
            conflicts = ist.get("conflicts", 0)
            inflight = {}
            for other in ist_map:
                oist = ist_of(other)
                if other != iid and oist.get("status") in INFLIGHT_STATUSES:
                    ot = oist.get("declared_touches")
                    inflight[other] = [t for t in ot if isinstance(t, str)] \
                        if isinstance(ot, list) else []

            g = gate.gate_decision(
                {"type": itype, "conflicts": conflicts, "nudged": nudged,
                 "declared_touches": declared, "update_result": update_result,
                 "investigation_done": inv_done},
                pv, reports.get(iid), cfg, bool(frozen), inflight)

            act, wander = g.get("action"), g.get("wander", False)

            # Bounded pending-checks escalation (issue #26): the ONE time-based backstop over the
            # gate's fail-closed 'pending' wait. A required check that never reports reads as
            # pending forever, so an unbounded wait left a finished issue in `gating` with no park,
            # no memo, no notify. Stamp the clock on the first pending tick; escalate ONCE past the
            # cap (park -> needs_william is terminal, so it can't re-fire); clear it the moment the
            # wait is no longer on the checks, so a later pending episode times from scratch. The
            # MERGE decision is untouched — pending never merges — this only makes the wait bounded.
            since = ist.get("checks_pending_since")
            if g.get("checks_pending"):
                if not _since_ok(since, now):
                    out.append({"act": "note_checks_pending", "id": iid})   # unset/corrupt: (re)stamp
                elif now - since >= _checks_pending_cap(cfg):
                    pend = g.get("pending") if isinstance(g.get("pending"), dict) else {}
                    unrep = [x for x in (pend.get("unreported") or []) if isinstance(x, str)]
                    running = [x for x in (pend.get("running") or []) if isinstance(x, str)]
                    detail = (("never reported: " + ", ".join(unrep)) if unrep else "") \
                        + (("; still running: " + ", ".join(running)) if running else "")
                    park(iid, num,
                         "required checks stayed pending past the bound — "
                         + (detail or "no required check ever reported")
                         + ". An unreported required check keeps a green PR gating forever; verify "
                         "the config names against what the repo actually reports (run "
                         "`superlooper doctor`) and the check's workflow triggers.",
                         needs_william=True, cause="checks_pending")
                    continue
            elif _real(since):
                out.append({"act": "clear_checks_pending", "id": iid})       # left the pending episode

            # Bounded comments-read wait (issue #78): the PR LOOKUP landed but its comments sub-read
            # was REFUSED or starved this tick, so the runner OMITTED the 'comments' key and the gate
            # WAITs (step 2b: comments_unread) rather than reading the fail-closed empty as "no review
            # marker" and marching the review nudge ladder to park a finished, reviewed build — the
            # build-gate sibling of await_pr_read (#61)/await_read (#21). Journal the wait ONCE per
            # episode via a clock; past the same refused-read bound, park ONCE so a permanent partial
            # dead zone (PR lookup up, comments endpoint down) still hands to the owner instead of
            # holding silent-forever; clear the clock when a trustworthy comments read lands. The
            # _since_ok discipline re-stamps a corrupt/future clock, never trusting it to defeat or
            # spuriously trip the bound. Structurally identical to the pending-checks backstop above.
            csince = ist.get("comments_read_pending_since")
            if g.get("comments_unread"):
                if not _since_ok(csince, now):
                    out.append({"act": "await_comments_read", "id": iid, "num": num,
                                "reason": "finished build: PR is visible but its comments read was "
                                          "refused or starved this tick — holding, never parking "
                                          "review evidence on an unverified empty read"})
                elif now - csince >= PR_READ_HOLD_CAP_SECONDS:
                    park(iid, num,
                         "finished and the PR is visible, but GitHub refused every comments read for "
                         f"{PR_READ_HOLD_CAP_SECONDS // 60}+ min (rate limit / 403 / 5xx) — cannot "
                         f"verify review evidence for PR #{pv.get('number') or '?'}. Parked once; the "
                         "PR stays intact and re-approving after reads recover will pick it up.",
                         cause="comments_read_refused")
                    continue
            elif _real(csince):
                out.append({"act": "clear_comments_read", "id": iid})   # a trustworthy read landed

            if act == "merge":
                # Bounded merge refusals (issue #27): the gate is green but GitHub can still REFUSE
                # the merge — ordinary branch protection (required approvals / strict up-to-date) or
                # a token without merge rights. The executor counts each refusal (`merge_refusals`)
                # and records the gh stderr (`merge_refusal_reason`); we retry UNDER the cap, then
                # park needs-william ONCE with the reason. A corrupt counter fails closed to the
                # park (never re-merge forever on a wrong-typed value). We NEVER bypass protection —
                # the refusal is surfaced to the owner, whose re-approval resets the guard (the
                # counter is episode-scoped; _exec_reapprove zeroes it).
                refusals, corrupt = _counter(ist, "merge_refusals")
                if corrupt or refusals >= MERGE_REFUSAL_CAP:
                    why = ist.get("merge_refusal_reason")
                    why = why if isinstance(why, str) and why.strip() else "(no gh reason captured)"
                    how_many = ("the merge-refusal counter is unreadable" if corrupt
                                else f"GitHub refused the merge {MERGE_REFUSAL_CAP} consecutive times")
                    park(iid, num,
                         f"the gate is green but {how_many} — ordinary branch protection (required "
                         "approvals / strict up-to-date) or a token without merge rights. "
                         f"superlooper never bypasses branch protection. gh said: {why}. Grant what "
                         "the protection requires (approve the PR / update the token's merge "
                         "rights), then re-approve the issue to retry.",
                         needs_william=True, cause="merge_refused")
                    continue
                method = cfg.get("merge_method")
                method = method if method in ("squash", "merge", "rebase") else "squash"
                out.append({"act": "merge", "id": iid, "num": num, "pr": pv.get("number"),
                            "method": method, "wander": wander})
            elif act == "update":
                out.append({"act": "update", "id": iid, "num": num, "pr": pv.get("number"),
                            "head_oid": head, "wander": wander})
            elif act == "hold":
                if status != "holding":                # journal-once: the hold is an edge
                    h = {"act": "hold", "id": iid, "reason": g.get("reason"), "wander": wander}
                    if g.get("overlap_lane") is not None:
                        h["overlap_lane"] = g["overlap_lane"]
                    if g.get("overlap_wildcard"):      # issue #36: the no-match-areas merge-hold cause
                        h["overlap_wildcard"] = True
                    out.append(h)
            elif act == "nudge":
                key = g.get("nudge_key")
                out.append({"act": "nudge", "id": iid, "nudge_key": key,
                            "message": NUDGE_MESSAGES.get(key, g.get("reason", ""))})
            elif act == "park":
                park(iid, num, g.get("reason", "gate parked this issue"),
                     needs_william=bool(g.get("needs_william")))   # cause defaults to the memo
            elif act == "regenerate":
                new_conflicts = _count(conflicts) + 1
                src = p if isinstance(p, dict) else {"num": num, "id": iid,
                                                     "title": ist.get("title", "")}
                out.append({"act": "regenerate", "id": iid, "num": num, "pr": pv.get("number"),
                            "new_branch": brief.branch_for(src, generation=new_conflicts),
                            "conflicts": new_conflicts, "wander": wander})
            elif act == "resolve_conflict":
                out.append({"act": "resolve_conflict", "id": iid, "num": num,
                            "pr": pv.get("number"), "wander": wander})
            elif act == "close_investigate":
                out.append({"act": "close_investigate", "id": iid, "num": num})
            # "wait" -> no action this tick

            # The issue LEFT the failing state without the park label ever landing (issue #61):
            # the gate reached a non-park verdict while the notify-once marker is still stamped.
            # Clear it so a LATER genuine park on this issue texts again — the guard is
            # per-cause-EPISODE, never forever. One-shot: the cleared field stops re-emission.
            if isinstance(ist.get("park_notify_cause"), str) and iid not in parked_now:
                out.append({"act": "clear_park_marker", "id": iid})
            continue

        # ---- blocked marker present (and no report): bounce or the answerer lifecycle ----
        if blocked_text is not None and not has_exited:
            if blocked_text.lstrip().startswith("BOUNCED:"):
                # Notify-once per bounce (issue #108, mirroring #61's park guard). A bounce hands the
                # issue back to the owner via a needs-owner label move; when that move keeps failing
                # (the 2026-07-13 missing-`needs-owner`-label storm), local status never settles, so
                # decide re-derives THIS bounce every tick — and used to re-text every tick (a text
                # every ~18s). The executor stamps the durable handback marker (`park_notify_cause`,
                # REUSED: a bounce and a park never overlap on one issue, and reuse gets the
                # park_label_stuck alert + reapprove reset for free) BEFORE the label move, so a
                # re-derived bounce for the same cause is a SILENT retry — the labels still converge.
                # ORDER IS LOAD-BEARING (mirrors park, Codex C1): notify BEFORE the bounce action, so
                # a crash between the two executors can only DUPLICATE a text, never lose it.
                cause = "bounce"
                act = {"act": "bounce", "id": iid, "num": num, "memo": blocked_text, "cause": cause}
                if ist.get("park_notify_cause") == cause:
                    act["retry"] = True
                    out.append(act)
                    continue
                notify(f"superlooper: {iid} bounced (needs-owner)", blocked_text)
                out.append(act)
                continue
            aid_rec = active_answerer.get(iid)
            answer = answers.get(iid) if isinstance(answers.get(iid), str) else None
            if aid_rec and answer is not None:
                deliveries, corrupt = _counter(ist, "answer_delivery_failures")
                if corrupt or deliveries >= DELIVERY_FAILURE_CAP:
                    park(iid, num, f"the answer would not deliver to the session pane "
                                   f"({DELIVERY_FAILURE_CAP} attempts, or the attempt counter "
                                   f"is unreadable). question was: {blocked_text!r}",
                         cause="answer_delivery")
                elif answer.lstrip().startswith("PARK:"):
                    park(iid, num, f"answerer escalated to {operator}. question: "
                                   f"{blocked_text!r} — answer: {answer!r}",
                         needs_william=True, cause="answerer_escalated")
                else:
                    out.append({"act": "deliver_answer", "id": iid,
                                "answerer_id": aid_rec[0], "text": answer})
            elif aid_rec:
                launched_at = aid_rec[1].get("launched_at")
                age = (now - launched_at) if _real(launched_at) else math.inf
                if age >= ANSWERER_TIMEOUT_SECONDS:
                    park(iid, num, f"answerer {aid_rec[0]} timed out after "
                                   f"{ANSWERER_TIMEOUT_SECONDS // 60} min. question was: "
                                   f"{blocked_text!r}", cause="answerer_timeout")
            else:
                hires, corrupt = _counter(ist, "answerer_failures")
                if corrupt or hires >= ANSWERER_FAILURE_CAP:
                    park(iid, num, f"could not launch an answerer ({ANSWERER_FAILURE_CAP} "
                                   f"attempts, or the attempt counter is unreadable). "
                                   f"question was: {blocked_text!r}", cause="answerer_hire")
                else:
                    out.append({"act": "hire_answerer", "id": iid, "num": num,
                                "answerer_id": f"a{next_aid}", "question": blocked_text})
                    next_aid += 1
            continue

        # ---- liveness recovery: exited beats frozen beats idle ----
        if has_exited:
            # A launch that died at startup left its real reason in the captured stderr tail (#40);
            # name it in whichever exited-park memo fires, so the operator sees the actual error and
            # not just the relaunch count.
            stderr_memo = _launch_stderr_memo(launch_stderr.get(iid))
            if type(retries) is not int:               # corrupt counter -> to William, not a loop
                park(iid, num, "exited, and the retry counter is unreadable — parking" + stderr_memo,
                     cause="exited_cap")
            elif retries >= retry_cap:
                park(iid, num, f"exited and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking" + stderr_memo, cause="exited_cap")
            elif usage_launchable:
                out.append({"act": "recover", "id": iid, "tier": "exited"})
            # no usage headroom -> the marker persists; relaunch resumes with the quota
            continue
        if iid in frozen_ids or status == "frozen":
            if in_wake_grace:
                continue                               # wake grace: hold the recovery ladder (issue #42)
            if type(retries) is not int:
                park(iid, num, "frozen, and the retry counter is unreadable — parking",
                     cause="frozen_cap")
            elif retries >= retry_cap:
                park(iid, num, f"frozen and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking", cause="frozen_cap")
            else:
                last_rec = ist.get("last_recover_at")
                last_rec = last_rec if _real(last_rec) else 0
                if now - last_rec >= RECOVER_RETRY_SECONDS:
                    out.append({"act": "recover", "id": iid, "tier": "frozen"})
            continue
        if iid in idle_ids:
            out.append({"act": "recover", "id": iid, "tier": "idle"})
            continue

        # ---- label reconciliation + orphaned in-progress (restart rebuild) ----
        labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
        if status in INFLIGHT_STATUSES or status in ("gating", "holding"):
            if "agent-ready" in labels:
                # the launch/park label move didn't land (gh blip): converge GitHub to reality
                out.append({"act": "relabel", "id": iid, "num": num,
                            "add": ["in-progress"], "remove": ["agent-ready"]})
            continue
        # status is None/"ready" from here on
        launch_fails, corrupt = _counter(ist, "launch_failures")
        if "agent-ready" in labels and (corrupt or launch_fails >= LAUNCH_FAILURE_CAP):
            if launch_degraded:
                continue                               # SYSTEMIC launch fault (#24): hold, never
                                                       # park per-issue — the queue stays intact for
                                                       # when the anchor resolves (the alert stands)
            if ist.get("launch_error") == "base_missing":
                # issue #28: launch-session.sh could not create the worktree because its base ref
                # origin/<dev_branch> does not exist. Name the REAL cause — the missing base branch
                # — instead of sending the newcomer to debug the launch shim (the wrong component).
                park(iid, num, f"launch never delivered: the worktree base branch "
                               f"'origin/{dev_branch}' does not exist, so every worktree creation "
                               f"fails before Claude starts — a repo/config fault, not a "
                               f"launch-delivery problem. Set `dev_branch` in "
                               f".superlooper/config.json to the repo's real default branch "
                               f"(`superlooper adopt` detects it; `superlooper doctor` validates "
                               f"it), then re-approve.", cause="launch_base_missing")
            else:
                park(iid, num, f"launch was never delivered ({LAUNCH_FAILURE_CAP} verified "
                               "attempts, or the attempt counter is unreadable) — is the launch "
                               "shim installed? (bin/install-launch-shim.sh)",
                     cause="launch_delivery")
            continue
        if "in-progress" in labels and iid not in live_locks and not gh_stale and iid in prs:
            pv = prs.get(iid) if isinstance(prs.get(iid), dict) else {}
            branch = pv.get("headRefName")
            stamped = ist.get("branch")
            # An open PR resumes the orphan ONLY if it is this issue's ACTIVE branch: a
            # `superseded` label or a loopstate branch stamp that differs (a partially-executed
            # regenerate) means the PR is dead history — requeue, never resurrect it.
            resumable = (pv.get("number") and pv.get("state") == "OPEN"
                         and "superseded" not in gate._pr_labels(pv)
                         and (not isinstance(stamped, str) or not stamped.strip()
                              or stamped == branch))
            if resumable:
                touches = p.get("touches") if isinstance(p.get("touches"), list) else []
                out.append({"act": "launch", "id": iid, "num": num, "branch": branch,
                            "touches": touches, "soft_overlap": False, "orphan": True})
            elif pv.get("number") and pv.get("state") == "OPEN":
                out.append({"act": "reclaim", "id": iid, "num": num})
            elif pv.get("state") in ("MERGED", "CLOSED"):
                # stuck labels (spec §5): the work concluded outside the runner; absorb it
                out.append({"act": "relabel", "id": iid, "num": num,
                            "add": [], "remove": ["in-progress"]})
            elif not pv.get("number"):
                out.append({"act": "reclaim", "id": iid, "num": num})

    # ================= E. fresh launches (LAST: freed lanes wait one tick) =================
    # launch_degraded holds EVERY fresh launch (issue #24): a dead/failing launch anchor makes every
    # delivery fail, so attempting more only walks the queue. The queue is preserved (agent-ready
    # intact) and resumes the tick the anchor resolves — no William relabeling. The #115 canary
    # (below) is the ONE exception: a single probe re-arms a systemic hold that cannot clear itself.
    def _eligible_launch_ids():
        """Approved, not-in-flight, launchable issues in priority order — the shared candidate set
        for both normal fresh launches AND the #115 canary probe. A PURE filter with NO side effects
        (it never parks): the touches-required park belongs to the normal path only, and a canary must
        never park while the systemic hold stands."""
        for iid in _sorted_ids(parsed_by_id):
            p = parsed_by_id[iid]
            labels = p.get("labels") if isinstance(p.get("labels"), list) else []
            ist = ist_of(iid)
            launch_fails, corrupt = _counter(ist, "launch_failures")
            if ("agent-ready" not in labels or "in-progress" in labels
                    or iid in parked_now
                    or iid in reapproved_now   # just re-released: launch next tick, reset counters
                    or ist.get("status") not in RELAUNCHABLE_STATUSES
                    or corrupt or launch_fails >= LAUNCH_FAILURE_CAP):
                continue
            yield iid, p, ist

    def _needs_touches(p):
        # touches_required (issue #36): an approved merge-producing issue that declares no `touches:`
        # is REFUSED at launch (never silently launched into an un-verifiable affinity). Investigations
        # produce no PR/merge, so touches are meaningless for them: exempt. Gate on ELIGIBILITY
        # (blocked-by closed, no control-label conflict) so we refuse ONLY at the true launch point:
        # an issue waiting on an open dependency keeps waiting, and a label-conflict issue is left for
        # its own handling — not mislabeled with a "missing touches" memo (fresh-agent review P2-1).
        return (_touches_required(cfg) and p.get("type") in _MERGE_PRODUCING_TYPES
                and not _declares_touches(p)
                and issues_mod.eligible(p, closed_nums, bool(frozen)))

    def _launch_branch(iid):
        branch = ist_of(iid).get("branch")
        return branch if isinstance(branch, str) and branch.strip() else brief.branch_for(parsed_by_id[iid])

    if not gh_stale and not issue_state_corrupt_for_launches and not launch_degraded:
        candidates = []
        for iid, p, ist in _eligible_launch_ids():
            if _needs_touches(p):
                park(iid, p.get("num"), _touches_required_memo(p.get("num")),
                     needs_william=True, cause="touches_missing")
                continue
            candidates.append(dict(p, requeue_front=bool(ist.get("requeue_front"))))
        claims = territory_claims_from(issues_state)
        selected_ids = set()
        for sel in scheduler.launchable(candidates, lanes_in, cfg, usage_sched,
                                        closed_nums, bool(frozen), territory_claims=claims):
            iid = sel["id"]
            selected_ids.add(iid)
            out.append({"act": "launch", "id": iid, "num": sel["num"], "branch": _launch_branch(iid),
                        "touches": sel["touches"], "soft_overlap": sel["soft_overlap"],
                        "orphan": False})
        # Wildcard launch-suppression journaling (issue #36): a no-touches wildcard — the candidate
        # itself, or the lane blocking it — serializes the queue silently under hard affinity. Record
        # WHY, ONCE per episode (dedup on the issue's `wildcard_hold_journaled` flag, reset on launch/
        # reapprove), so "why is only one lane busy" is answerable from the journal. Bounded and
        # journal-only: no notify, no park, the queue is untouched.
        for h in scheduler.launch_holds(candidates, lanes_in, cfg, usage_sched,
                                        closed_nums, bool(frozen), territory_claims=claims):
            hid = h.get("id")
            if hid in selected_ids or ist_of(hid).get("wildcard_hold_journaled"):
                continue
            out.append({"act": "wildcard_hold", "id": hid, "num": h.get("num"),
                        "blocker": h.get("blocker_id"), "reason": _wildcard_hold_reason(h)})
    elif (systemic_launch and not anchor_down
            and not gh_stale and not issue_state_corrupt_for_launches):
        # Canary re-arm of the SYSTEMIC hold (issue #115). The streak-based hold cannot clear itself,
        # so once per CANARY_RETRY_SECONDS since the last delivery failure, probe with ONE canary
        # launch of the front-of-queue issue. A verified delivery clears the streak (runner) and normal
        # launching resumes next tick; a failed canary re-enters the hold — the runner charges NO
        # per-issue cap and this decide emits no park. Skipped while the pane probe itself reports the
        # anchor DEAD (anchor_down): that detector self-re-arms via its per-tick probe, and a canary
        # into a probe-dead pane is wasted; once the probe recovers but the streak persists, THIS path
        # fires and clears it. The interval gate fails CLOSED on a garbage/absent clock (no probe).
        fail_at = dsk.get("launch_fail_at")
        if _real(fail_at) and now - fail_at >= CANARY_RETRY_SECONDS:
            candidates = [dict(p, requeue_front=bool(ist.get("requeue_front")))
                          for iid, p, ist in _eligible_launch_ids()
                          if not _needs_touches(p)]   # a touches-missing issue is never the probe
            claims = territory_claims_from(issues_state)
            probe = scheduler.launchable(candidates, lanes_in, cfg, usage_sched,
                                         closed_nums, bool(frozen), territory_claims=claims)
            if probe:
                sel = probe[0]                         # the single front-of-queue issue, priority order
                out.append({"act": "launch", "id": sel["id"], "num": sel["num"],
                            "branch": _launch_branch(sel["id"]), "touches": sel["touches"],
                            "soft_overlap": sel["soft_overlap"], "orphan": False, "canary": True})
    return out
