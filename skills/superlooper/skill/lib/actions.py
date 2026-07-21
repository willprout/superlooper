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
  * NOTIFY IS A STANDING RULE (owner directive), NIGHT-BATCHED (issue #164): every new systemic
    ALERT (runner/auth dead, whole queue stalled) and every freeze emits {"act": "notify"} at any
    hour — that is the safety layer, never quieted. A routine owner-DECISION hand-back (park /
    bounce / durable question) pages immediately during the DAY, but during quiet hours (config
    `notify.quiet_hours`, default 21:00–08:00) it is BATCHED to the morning report instead: the
    ACTION still fires (state settles, the journal + morning report list it), only the page waits.
    The scenario table asserts both directions. ONCE per (issue, park-cause) episode (issue #61):
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
                  "exited": {id: marker-text}, "frozen": dict|None,
                  "alert": dict|None, "live_lock_ids": iterable of ids with a LIVE worker lock,
                  "filed_fingerprints": {fingerprint: issue_num},
                  "local_date": "YYYY-MM-DD", "local_hhmm": "HH:MM",
                  "last_report_date": str|None}
  gh_view        {"stale": bool (fresh ONLY when exactly False), "consecutive_failures": int,
                  "closed_nums": set,
                  "closed_read_ok": bool — did the read that produced closed_nums actually LAND
                  (issue #172)? An empty closed_nums is ambiguous without it: GitHub answered
                  "nothing is closed", or it REFUSED the read and the fail-closed parser produced
                  the same empty — and `probe` (rate_limit) is exempt from throttling, so `stale`
                  stays False through a throttle either way. Trusted only in the direction it
                  explicitly asserts: an EXPLICIT False means refused; anything else (including a
                  key-absent older view) reads as a landed read, so no refusal is ever manufactured
                  out of missing data,
                  "prs": {id: pr_view (+comments attached ONLY on a clean
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
  launch, post_question, answer_relaunch, bounce, recover(tier=idle|frozen|exited), gate,
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
import evidence
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
RECOVER_RETRY_SECONDS = 600        # frozen-session recovery ladder re-fires at most every 10 min
# The bounded probe ladder (issue #157), keyed on the PROGRESS clock, not activity staleness. A lane
# that takes turns but makes no commit/marker/HEAD change for PROGRESS_STALL_SECONDS is probed for a
# machine-readable ack, at most PROBE_CAP times spaced PROBE_RETRY_SECONDS apart, then escalated to a
# classified park. Total stall->park is bounded (~STALL + (CAP-1)*RETRY), never the i328 infinite
# nudge loop; a lane that makes ANY real progress resets the episode and is never parked. Config
# overrides live under session.* (progress_stall_seconds / probe_retry_seconds / probe_cap).
PROGRESS_STALL_SECONDS = 900       # no commit/marker/HEAD change (turns still taken) this long -> probe
PROBE_RETRY_SECONDS = 300          # min spacing between probes within one progress-stall episode
PROBE_CAP = 3                      # probes per episode before a classified progress-stall park
# The exit interview's reply window (issue #215): how long a finishing investigation gets to file
# its findings as child issues and post the one-line reply before the ladder re-asks (once) and
# then parks. Generous next to the probe window — answering means CREATING issues, not writing
# one ack line. The window runs from the LATER of the ask and its consumption receipt (a mail
# consumed late, e.g. a worker mid-long-turn, gets its full window from delivery). Config
# override: session.exit_reply_window_seconds.
EXIT_REPLY_WINDOW_SECONDS = 600
# The gate nudge's COMPLIANCE WINDOW (issue #222): once the gate nudges a cause (missing report
# sections, absent/stale review evidence, a missing investigation marker), the worker gets this long
# to comply before the gate parks the lane. The pre-#222 grace was ONE tick (~18-25s): decide stamped
# the nudge and the very next tick found the key present and parked, so no worker could post a review
# comment or fix report sections in time — the "nudge once" design was structurally a park with extra
# steps. The window runs from the nudge's actual delivery (the `nudged_at` stamp _exec_nudge writes
# when the key is spent). One nudge per cause is UNCHANGED — this bounds the WAIT between the nudge and
# the park, it does not unbound the nudge (the i280 lesson). Default 480s ("on the order of the session
# idle threshold", session.idle_seconds) — generous enough for a real paperwork fix, far past a tick.
# Config override: session.nudge_grace_window_seconds. An unreadable/absent stamp reads as EXPIRED
# (fail closed to the park; the ladder stays bounded, never an unbounded wait).
NUDGE_GRACE_WINDOW_SECONDS = 480
# How long a session may sit at its OWN in-window question before the owner is told (issue #151).
# A lane at a dialog is never parked — it is alive — but it must not be SILENT either: the loop's
# channel for "worker needs input" is state/blocked/<id> -> hire_answerer, and an in-window
# AskUserQuestion is off that channel, so nobody will ever answer it and the lane's slot (frozen is
# an INFLIGHT status) would leak forever. 30 min is far past any dialog a watching owner answers,
# and far inside the 94-minute class of silence this issue exists to end.
AT_DIALOG_ALERT_SECONDS = 1800
LAUNCH_FAILURE_CAP = 2             # launch never delivered twice -> park (RC-LAUNCHVERIFY x2)
# A dead DELIVERY CHANNEL — the cmux launch anchor (the pane every worker tab is born in), the
# launch shim, or the launch machinery — is a RUNNER-level fault, never N per-issue parks (incident
# 2026-07-09: a dead anchor walked 10 approved issues into 10 parks in ~8 min). The runner records
# ONLY channel-attributable launch failures in this streak (evidence.is_channel_fault gates it; a
# per-issue fault like base_missing parks its own issue and never enters here — issue #153), so a
# single entry already means the channel is down. The FIRST such failure is therefore systemic:
# hold launches, one alert, the queue left intact — no issue absorbs the blame, not even the first.
# (The earlier design waited for a SECOND distinct issue to infer "channel" by counting; reading the
# evidence reason tells us on the first failure, so the count threshold is 1.) Kept beside the
# pane-probe path (`launch_anchor`), which catches a dead anchor before a launch is even attempted.
SYSTEMIC_LAUNCH_FAILURE_CAP = 1    # >= this many channel-attributable failures -> systemic (#24/#153)
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
# At most TWO owner-decision questions per issue (#163). A worker that would ask a THIRD is no longer
# stuck on a decision — it is stuck on SCOPE, which only the owner can untangle; so the third hands
# the issue back (needs-owner) with the question quoted, rather than posting a third round-trip.
QUESTION_CAP = 2
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
              f"post its verdict as a PR comment BEGINNING `{gate.pinned_review_marker()}` "
              f"(what was reviewed + P0/P1 outcome), with {gate.REVIEW_PIN_PLACEHOLDER} replaced "
              "by the output of `git rev-parse HEAD`. The runner will not merge without it.",
    # issue #154: the verdict exists but does not provably cover the head being merged — either
    # it carries no readable pin, or it pins a superseded diff (the post-reapprove rebuild case).
    # The remedy is the same for both: review what is on the PR NOW and pin the verdict to it.
    "review_stale": "Your PR carries a review verdict that does not name the code now on it — it "
                    "either has no readable `sha=` pin or pins an older commit, and the head has "
                    "moved since (a rebuild or a new push). Re-review the CURRENT diff with a "
                    "fresh agent and post a verdict pinned to the current head: a PR comment "
                    f"BEGINNING `{gate.pinned_review_marker()}`, where "
                    f"{gate.REVIEW_PIN_PLACEHOLDER} is the literal oid `git rev-parse HEAD` "
                    "prints after your last push — run it, then paste the oid in. A shell "
                    "substitution is NOT expanded inside a single-quoted `gh pr comment --body`, "
                    "and the unexpanded text pins nothing. The runner will not merge code that "
                    "nothing vouches for.",
    "checks": "A required check failed on your PR. Investigate the failure, fix it, and push — "
              "the gate re-runs automatically.",
    "investigation": "Post your root-cause report as an issue comment BEGINNING "
                     "`<!-- superlooper-investigation -->` — without that marker comment the "
                     "runner cannot even begin closing the parent (the close itself runs "
                     "through an exit interview that arrives once the marker exists).",
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
    "launch_systemic_failure": "a launch is failing DELIVERY to the channel — the cmux anchor or the "
                               "launch shim — not to any one issue: a systemic launch fault. The "
                               "queue is held intact (nothing parked, no issue charged). The usual "
                               "cause when this trips after you walk "
                               "away is macOS App Nap suspending an idle/occluded cmux: it still "
                               "answers new-surface but defers spawning the tab's shell past the "
                               "verify window, so no worker starts. Fix: run "
                               "`defaults write com.cmuxterm.app NSAppSleepDisabled -bool true` "
                               "(or re-run bin/install-launch-shim.sh), then FULLY QUIT and "
                               "relaunch cmux in a visible tab and restart the runner — the flag is "
                               "read only at app launch. If it persists, check the cmux anchor.",
    "auth_dead": "the account AUTH probe reads DEAD — `claude auth status` reports not-logged-in "
                 "(or the Claude Code credential keychain item is gone), so a fresh launch or a "
                 "recovery relaunch would start LOGGED OUT and burn the spend (the i336 class). "
                 "Launches and relaunches are HELD (the queue is intact, nothing parked) and "
                 "resume automatically once auth reads healthy again. Re-login: run `claude` (or "
                 "`claude auth login`) in a terminal signed into the loop's subscription account. "
                 "This is the PRE-LAUNCH sibling of the in-window 'Not logged in' state "
                 "(session_logged_out): it catches dead auth BEFORE a session is spent, not after.",
}


def _alert_message(reason):
    if isinstance(reason, str) and reason.startswith("session_at_dialog:"):
        iid = reason.split(":", 1)[1]
        return (f"{iid}'s session has been sitting at its OWN question dialog in-window for "
                f"{AT_DIALOG_ALERT_SECONDS // 60}+ min — it asked something and is waiting for an "
                "answer nobody is going to give: an in-window question bypasses the loop's "
                "blocked-file/answerer channel entirely. The lane is ALIVE and is not parked, but "
                "it is holding its slot and cannot progress. Open its tab and answer the dialog "
                "(or press Esc so it writes a blocked file instead). Clears by itself once the "
                "dialog is gone.")
    if isinstance(reason, str) and reason.startswith("session_logged_out:"):
        iid = reason.split(":", 1)[1]
        return (f"{iid}'s session is logged OUT in-window ('Not logged in · Please run /login') — "
                "its auth died mid-run, so the CLI is still up but every turn is refused. The loop "
                "will NOT nudge it (a nudge cannot be answered) and will NOT relaunch it (the "
                "relaunch would re-enter dead auth); the lane is held as-is for you. Known from the "
                "2026-07-14 night: running /login INSIDE the wedged window did not stick — closing "
                "the window and starting a fresh session did. The alert clears by itself once the "
                "lane reads healthy again.")
    if isinstance(reason, str) and reason.startswith("park_label_stuck:"):
        iid = reason.split(":", 1)[1]
        return (f"{iid} handed back to the owner but its label move has been failing for "
                f"{PARK_LABEL_STUCK_ALERT_SECONDS // 60}+ min — GitHub writes are not landing. "
                "This alert IS the escalation (the hand-back's own page went out during the day, or "
                "was batched to the morning report if it happened in quiet hours — #164); the label "
                "retries continue silently. Check GitHub availability / rate limits "
                "(`gh api rate_limit`).")
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


def _progress_stall_memo(iid, clock, stall_secs, attempts, ack_state):
    """The dossier for a progress-stall park (issue #157). Names the evidence William reads instead
    of an unbounded nudge loop: how long the progress clock has been frozen, the frozen HEAD, how
    many probes went unanswered-or-lied-to, and the worker's OWN last self-report. Fail-open on
    wrong-typed inputs (never raise into a park)."""
    head = clock.get("head") if isinstance(clock, dict) else None
    head_s = head[:12] if isinstance(head, str) and head else "unknown"
    mins = int(stall_secs // 60) if isinstance(stall_secs, (int, float)) else 0
    n = attempts if type(attempts) is int and attempts >= 0 else 0
    said = {
        "DONE": "the worker acked DONE, but produced no report/PR the loop can see",
        "WORKING": "the worker acked WORKING, but the progress clock disagrees (the i328 shape)",
        "WAITING": "the worker acked WAITING on background work — verify it is real, not a stall",
        "STUCK": "the worker acked STUCK and asked for help",
    }.get(ack_state, "the worker never answered a probe with a valid ack")
    return (f"{iid}: progress-stall park (issue #157). No new commit/marker/HEAD change for "
            f"~{mins} min (HEAD {head_s}) across {n} probe(s) — {said}. The lane took turns without "
            f"progressing, so it was escalated instead of nudged forever (the i328 infinite loop).")


def _captured_addendum(ev):
    """The captured-stderr addendum for a memo that already names its own cause (issue #152).

    Used where the memo's wording is ALREADY right — the #28 base-missing memo — to show the
    launcher's own words underneath it, so the operator can check the runner's reading instead of
    trusting it. Fail-open on wrong-typed/absent evidence (never raise into a park): no record, no
    addendum. Where the memo must be DERIVED from the evidence, use evidence.park_memo instead.
    """
    if not isinstance(ev, dict):
        return ""
    captured = ev.get("captured")
    if not isinstance(captured, str) or not captured.strip() or captured == evidence.CAPTURED_NONE:
        return ""
    return ("\n\ncaptured at the point of failure (stderr tail — the launcher's own account):\n"
            + evidence.bound(captured, limit=evidence.PARK_MEMO_CAPTURED_MAX))


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


# Night-batching (issue #164). Routine owner-DECISION hand-backs (a park, a bounce, a durable
# question) are held to the morning report during quiet hours instead of paged; the SYSTEMIC-STOP
# alerts and the merge-freeze notice always push. The fallback for an OLD config.json (pre-#164, no
# `notify.quiet_hours` key) is config's OWN default — one source of truth, imported so it can't drift.
_DEFAULT_QUIET_HOURS = _config.DEFAULT_QUIET_HOURS
_ASCII_DIGITS = frozenset("0123456789")


def _valid_hhmm(v):
    """A zero-padded 24h "HH:MM" in range — what the runner's `time.strftime('%H:%M')` local clock
    always stamps, so the lexical compare below is a true time-of-day order. ASCII digits only:
    `str.isdigit()` is True for Unicode numerics (superscripts, other-script digits) that would then
    RAISE in int() — violating decide's never-raise contract — so membership against 0-9 is used."""
    return (isinstance(v, str) and len(v) == 5 and v[2] == ":"
            and set(v[:2]) <= _ASCII_DIGITS and set(v[3:]) <= _ASCII_DIGITS
            and 0 <= int(v[:2]) <= 23 and 0 <= int(v[3:]) <= 59)


def _in_quiet_hours(hhmm, quiet_hours):
    """True iff local time `hhmm` falls inside the configured quiet-hours window — when a routine
    owner-decision hand-back is BATCHED to the morning report instead of pushed (issue #164).
    `quiet_hours` is {"start","end"} in "HH:MM"; None (or malformed) DISABLES quieting. Fails toward
    PUSHING (returns False) on ANY uncertainty — a missing clock, a garbled window — because a
    notification is a convenience layer the morning report + journal always backstop, so a spurious
    page is safer than a wrongly-swallowed one. `start > end` wraps past midnight (the night window);
    `start == end` is a degenerate empty window (never quiet); `end` is EXCLUSIVE."""
    if not isinstance(quiet_hours, dict):
        return False
    start, end = quiet_hours.get("start"), quiet_hours.get("end")
    if not (_valid_hhmm(start) and _valid_hhmm(end) and _valid_hhmm(hhmm)) or start == end:
        return False
    if start < end:
        return start <= hhmm < end           # same-day window
    return hhmm >= start or hhmm < end        # wraps midnight (e.g. 21:00 -> 08:00)


def _since_ok(since, now):
    """A USABLE pending-checks clock: a real number in [0, now] (issue #26, Codex R1). The runner
    only ever writes `now`, so a FUTURE value (would make now-since negative and defeat the cap —
    an unbounded wait again) or a NEGATIVE one (would make now-since huge and escalate spuriously)
    is corrupt. Treat it as unstamped so it re-stamps, never trusted to defeat OR trip the bound."""
    return _real(since) and 0 <= since <= now


def _in_owner_handback_episode(ist, blocked_text):
    """True iff the issue is in an owner-handback episode — a park, a bounce, OR a durable question
    (#163) that is handing the issue back to the owner (issue #108). Either it has already SETTLED
    into an owner-decision status (parked / needs_william / bounced / awaiting_answer), or it is
    mid-handback with the durable marker / BOUNCED memo present (the storm states, where a label move
    keeps failing and status has not settled). awaiting_answer counts so the owner CLOSING a waiting
    question on GitHub (his Drop) is absorbed like any other hand-back close, freeing the worktree. Scopes external-close absorption
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
    if status in ("bounced", "parked", "needs_william", "awaiting_answer"):
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


# A corrupt state/issues.json can carry a WRONG-TYPED status. An UNHASHABLE one ([]/{}) makes any
# `status in <SET>` membership test raise `unhashable type`, and because the tick stamps its heartbeat
# LAST (runner.tick), that raise wedges the whole tick before the heartbeat — so the dashboard's
# dead-man's switch reads a LIVE runner as dead (issue #95, the same fail-open-on-wrong-typed defect
# class events.detect_events / snapshot / tidy.closable / retry_runaway already guard). The existing
# `isinstance(ist, dict)` guards on these sites do NOT catch it: a dict ist can still hold a wrong-typed
# status VALUE. Fold every such value to a sentinel that is a member of NO status set, so each membership
# test stays hash-safe and fails CLOSED in every direction — a corrupt issue occupies no lane, makes no
# territory claim, and is never launched. A genuinely status-less None must NOT collapse to the sentinel
# (None is a legitimate member of RELAUNCHABLE_STATUSES — cold/ready), so it is preserved as-is.
_CORRUPT_STATUS = object()


def _status_of(ist):
    """`ist['status']` when it is None or a well-typed str; else a sentinel that belongs to no status
    set (a wrong-typed/unhashable status fails closed, never raising into a `status in <SET>` test)."""
    s = ist.get("status") if isinstance(ist, dict) else None
    return s if (s is None or isinstance(s, str)) else _CORRUPT_STATUS


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


def _refused_closed_read_deps(p, closed_nums, closed_read_ok):
    """The `blocked-by` numbers this issue is held on WHILE the closed-list read stands REFUSED
    (issue #172) — empty when the read landed, when the issue declares no dependency, or when the
    dependency is satisfied anyway. This is the ONE condition under which "blocked" is a statement
    about GitHub's refusal rather than about the dependency: the fail-closed empty set cannot tell
    the two apart, so the caller names the refusal instead of the (unobserved) closure state."""
    if closed_read_ok or not isinstance(p, dict):
        return []
    deps = p.get("blocked_by") if isinstance(p.get("blocked_by"), list) else []
    return [d for d in deps if d not in closed_nums]


def _refused_closed_read_reason(open_deps):
    return ("the closed-issue list could not be read this poll — GitHub REFUSED it (a throttle "
            "refuses the list read while `gh api rate_limit`, which is exempt, still answers), so "
            "its `blocked-by` ("
            + ", ".join("#%s" % d for d in open_deps)
            + ") cannot be verified as closed. A refused read is never taken as 'nothing is "
              "closed': held until a clean closed-list read lands, which is the next poll.")


def _launch_gate_reason(p, closed_nums, usage, config=None, closed_read_ok=True):
    """WHY the one launch gate (scheduler.launch_ok) refused to start or restart this session —
    the SPECIFIC failing condition, named. This is what makes a refusal a legible hold instead of
    the silent launch D8 caught: the board and the journal say which dependency is open, or which
    label went ambiguous, not merely "held". Conditions are tested in launch_ok's own order so the
    prose always names the one that actually decided."""
    if not scheduler.usage_ok(usage):
        return ("no usage headroom (the meter is unreadable/unhealthy, or at-or-over a ceiling) — "
                "the restart waits for quota, exactly as a fresh launch does")
    if not isinstance(p, dict):
        return ("the issue is not in the current GitHub view, so its approval, type and "
                "`blocked-by` cannot be read — a session is never restarted on eligibility that "
                "cannot be verified")
    labels = p.get("labels") if isinstance(p.get("labels"), list) else []
    if "agent-ready" not in labels and "in-progress" not in labels:
        return ("the approval is gone — the issue carries neither `agent-ready` nor the loop's own "
                "`in-progress` stamp, so restarting it is nobody's word")
    if p.get("type") not in issues_mod.VALID_TYPES:
        return ("its `type:` labels are missing, unknown or doubled, so what to build is ambiguous "
                "— a restart waits for the labels to be fixed, exactly as a fresh launch does")
    if p.get("label_conflict"):
        return ("its control labels conflict (2+ `model:*` or 2+ `effort:*`), so which model/effort "
                "to restart under is ambiguous — waiting for the labels to be fixed")
    deps = p.get("blocked_by") if isinstance(p.get("blocked_by"), list) else []
    open_deps = [d for d in deps if d not in closed_nums]
    if open_deps:
        # The view now VOUCHES for its closed read (issue #172), so when the read was REFUSED we say
        # THAT — the honest cause — instead of describing the dependency at all. #150 could only be
        # non-committal here ("not confirmed closed") because the poll handed decide a bare set with
        # no way to tell a refusal from an answer; carrying the read health closes that gap.
        if not closed_read_ok:
            return _refused_closed_read_reason(open_deps)
        # Says "not confirmed closed", never "still open" (fresh-agent review P2-1). gh's closed-list
        # read fails CLOSED to an empty set, and `probe` (rate_limit) is exempt from throttling — so
        # a THROTTLED poll still stamps the view fresh while every dependency reads as unmet. The
        # refusal to launch is right either way (a fresh launch does the same, and it self-heals on
        # the next clean read), but the loop must not durably stamp the board and the journal with a
        # closure state it did not actually observe — the refused≠empty trap of #21/#61/#78/#92/#108.
        # (Kept as the wording for a LANDED read: a view with no vouch is never narrated as refused.)
        return ("its `blocked-by` is not satisfied in the latest GitHub read: "
                + ", ".join("#%s" % d for d in open_deps)
                + " not confirmed closed. The restart waits for the dependency to close — a recovery "
                  "never carries a session past a blocker a fresh launch would respect.")
    if config is not None and gate.foreseeable_referee_stop(p.get("touches"), config) \
            and not gate.preauthorized_referee(p.get("labels")):
        # issue #165: its declared `touches:` resolve to a referee subtree, so building it can ONLY
        # end in a needs-owner park — and the owner has not pre-authorized it. Waiting for his word
        # (a `pre-authorized:referee` label at approval) beats burning a lane to reach a certain stop.
        return (f"it declares `touches:` that reach a referee path (.superlooper/** or "
                ".github/workflows/**), so building it can only end in a needs-owner park — and the "
                f"owner has not pre-authorized it (`{gate.PREAUTHORIZED_REFEREE_LABEL}`). Held for his "
                "word up front rather than launched unattended to a foreseeable stop; pre-authorize "
                "at approval or re-scope the issue off the referee paths.")
    return "the launch gate refused this start (no single condition named)"


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
        if isinstance(ist, dict) and _status_of(ist) in INFLIGHT_STATUSES:
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
        if not isinstance(ist, dict) or _status_of(ist) not in TERRITORY_CLAIM_STATUSES:
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
    progress_stall_secs = _count(session.get("progress_stall_seconds"), PROGRESS_STALL_SECONDS)
    probe_retry_secs = _count(session.get("probe_retry_seconds"), PROBE_RETRY_SECONDS)
    probe_cap = _count(session.get("probe_cap"), PROBE_CAP)
    exit_window = _count(session.get("exit_reply_window_seconds"), EXIT_REPLY_WINDOW_SECONDS)
    nudge_window = _count(session.get("nudge_grace_window_seconds"), NUDGE_GRACE_WINDOW_SECONDS)
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
    exited = _dget(dsk, "exited", dict)
    launch_stderr = _dget(dsk, "launch_stderr", dict)   # {id: bounded stderr tail} (issue #40)
    status_clocks = _dget(dsk, "status_clocks", dict)   # {id: parsed status.json} — the #157 progress clock
    acks = _dget(dsk, "acks", dict)                      # {id: raw ack text} — the worker's probe reply
    awaiting = _dget(dsk, "awaiting", dict)              # {id: marker} — worker flagged long background work
    exit_receipts = _dget(dsk, "exit_receipts", dict)    # {id: newest mail-consumption ts} (#148/#215)
    frozen = dsk.get("frozen") if isinstance(dsk.get("frozen"), dict) else None
    alert_on_disk = dsk.get("alert") if isinstance(dsk.get("alert"), dict) else None
    raw_locks = dsk.get("live_lock_ids")
    live_locks = set(raw_locks) if isinstance(raw_locks, (set, frozenset, list, tuple)) else set()

    # Night-batching (issue #164): during quiet hours a routine owner-DECISION hand-back (park /
    # bounce / durable question) is BATCHED to the morning report + dashboard instead of pushed —
    # only systemic-stop ALERTs (runner/auth dead, whole queue stalled) and the merge-freeze notice
    # keep paging (the safety layer). Config-absent -> the default night window (batching is ON by
    # default, the point of this issue); an explicit null -> disabled (every hand-back pages, the
    # pre-#164 behaviour). The clock is the runner's local HH:MM; _in_quiet_hours fails toward
    # PUSHING on any uncertainty, and the morning report lists every hand-back regardless, so a
    # night-suppressed decision is never lost — only unsent until morning.
    notify_cfg = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    quiet_hours = notify_cfg["quiet_hours"] if "quiet_hours" in notify_cfg else _DEFAULT_QUIET_HOURS
    notify_quiet = _in_quiet_hours(dsk.get("local_hhmm"), quiet_hours)

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
    # (#150) The usage rule is no longer asked on its own here: it is one half of scheduler.launch_ok,
    # the single gate start_ok() puts in front of EVERY start and restart. Asking it separately is
    # what let the recovery tier obey usage while skipping eligibility — the D8 drift itself.
    filed = _dget(dsk, "filed_fingerprints", dict)

    gv = gh_view if isinstance(gh_view, dict) else {}
    gh_stale = gv.get("stale") is not False            # fresh ONLY when explicitly False
    prs = _dget(gv, "prs", dict)
    issue_comments = _dget(gv, "issue_comments", dict)
    raw_closed = gv.get("closed_nums")
    closed_nums = set(raw_closed) if isinstance(raw_closed, (set, frozenset, list, tuple)) else set()
    # Does the poll VOUCH for that closed set (issue #172)? Only an EXPLICIT False is a refusal — a
    # view that never carried the key (an older document) keeps today's non-committal prose rather
    # than having a refusal invented for it. Eligibility is UNCHANGED either way: a refused read
    # still holds every `blocked-by` issue, which is the safe direction (waiting beats launching
    # past a blocker). What changes is that the hold is now SAID, and named for its real cause.
    closed_read_ok = gv.get("closed_read_ok") is not False

    out = []
    parked_now = set()
    reapproved_now = set()
    resumed_now = set()          # re-approved FINISHED builds resuming at the gate (issue #161):
                                 # held out of THIS tick's launch phase, like reapproved_now, so the
                                 # gate re-claims the lane rather than a fresh session rebuilding it

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
        # Night-batching (#164): a park is a routine owner DECISION and a park is a SAFE state, so
        # during quiet hours it is held to the morning report instead of paged. The park ACTION
        # still fires — state settles, the label moves, the journal (and the morning report) list
        # it — so nothing is lost, only unsent until morning. A genuinely stuck park (its label move
        # failing past the bound) still escalates via the park_label_stuck ALERT, which pages: a
        # failing GitHub write IS a systemic problem. The notify-once marker is stamped by the
        # executor regardless of whether we paged, so this park never re-pages once day breaks.
        if not notify_quiet:
            notify(f"superlooper: {iid} {who}", memo)
        out.append(act)

    def start_ok(p, resume=True):
        """THE gate, asked identically by every path that starts or restarts a session (issue #150 /
        D8). Fresh phase-E launches reach the same predicate through scheduler.launchable, which
        filters candidates on launch_ok. `config=cfg` arms the foreseeable-referee gate (#165) on
        the recovery paths too, so a certain, un-authorized referee park never burns a lane on a
        restart any more than on a fresh launch."""
        return scheduler.launch_ok(p, closed_nums, bool(frozen), usage_sched, resume=resume,
                                   config=cfg)

    def launch_hold(iid, num, p, reason=None):
        """start_ok (or the #159 auth gate) said no: HOLD, legibly — never a silent launch (D8), and
        never a park. This is a WAIT, not a verdict: the retry cap and park semantics are untouched (a
        boundary of #150), the marker/labels stay exactly as they are, and the restart fires on the
        tick the gate passes. Journal ONCE per CAUSE — dedup on the reason the executor stamps
        durably, so a standing hold can't spam a 15s tick, while a CHANGED cause (the blocker closed
        but the labels went ambiguous) still speaks rather than leaving stale prose on the board. An
        explicit `reason` overrides the usage/eligibility reason (the auth gate names auth)."""
        reason = reason if isinstance(reason, str) and reason \
            else _launch_gate_reason(p, closed_nums, usage_sched, config=cfg,
                                     closed_read_ok=closed_read_ok)
        if ist_of(iid).get("launch_hold_reason") == reason:
            return
        out.append({"act": "launch_hold", "id": iid, "num": num, "reason": reason})

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
    #   * launch_fail_ids: the DISTINCT issues whose launch failed for a CHANNEL reason (the anchor,
    #     the shim, the launch machinery — the runner classifies via evidence.is_channel_fault before
    #     recording; a per-issue fault never enters here and still parks its own issue). Any verified
    #     delivery clears it. Because the streak is channel-only, its FIRST entry already means the
    #     channel is down: >= SYSTEMIC_LAUNCH_FAILURE_CAP (1) is systemic (issue #153).
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
                and _status_of(ist_of(iid)) in RELAUNCHABLE_STATUSES)
    has_pending_launch = any(_held_queue_member(iid, p) for iid, p in parsed_by_id.items())
    # One degraded mode for both detectors: hold every fresh launch and suppress the per-issue
    # launch-cap park (phases D+E), so the queue is left intact for when the anchor resolves.
    launch_degraded = anchor_down or systemic_launch
    # Account-level AUTH gate (issue #159 / forensics U3). The runner hands us a `claude auth status`
    # + credential-keychain snapshot when a spend is pending; a DEFINITIVE dead reading (valid is
    # literally False — the CLI is not-logged-in, or the keychain item is gone) means a fresh launch
    # or a recovery relaunch would start LOGGED OUT and burn the spend (the i336 class the in-window
    # 'logged_out' state catches only AFTER a session is up). Fail OPEN on anything unreadable (valid
    # None/absent/wrong-typed): a probe we merely could not run must never freeze the whole loop — the
    # #46/#76 dark-meter asymmetry, applied to auth. auth_invalid holds fresh launches AND recovery
    # relaunches (below) and, when there is real spend demand, raises the auth_dead ALERT.
    auth_probe = dsk.get("auth_probe")
    auth_invalid = isinstance(auth_probe, dict) and auth_probe.get("valid") is False
    # Display-sleep launch hold (issue #124). macOS will not boot a fresh cmux tab's shell while the
    # DISPLAY sleeps, so a launch attempted then is created and closed as an orphan (exit 2) — a burned
    # attempt that feeds #24's systemic streak and churns an alert every sleeping episode. The runner
    # hands us a per-tick display-power read (only when there is launch demand); we HOLD every fresh
    # launch — and the #115 canary — QUIETLY while it reads CONFIRMED asleep, and resume automatically
    # on wake (the next tick reads awake). FAIL OPEN on anything but an explicit True (unreadable /
    # absent / awake all launch normally, EXACTLY today's behavior): a false hold would wedge the whole
    # queue, whereas a missed hold merely costs one already-self-recovering canary cycle. Unlike the
    # anchor / auth detectors this raises NO alert and enters NO streak — a sleeping display is normal,
    # expected behavior (the owner's Mac overnight), not a fault to page on.
    display_asleep = dsk.get("display_asleep") is True
    # A recovery relaunch is spend demand too — a dead-auth reading with no fresh queue but an
    # in-flight lane (an ORPHAN RESUME after a restart, a crash relaunch, a conflict resolve) must
    # still surface and never hold SILENTLY (fresh-review P1 sub-note; i336 was a recovery scenario).
    # `in-progress`-labelled == an in-flight lane that may relaunch this tick; the exited marker is a
    # backstop for a lane whose label move hasn't landed. Loose by design: over-surfacing dead auth is
    # fail-safe, it auto-clears on the next healthy probe, and a terminal lane's marker is cleaned.
    has_relaunch_demand = (
        any(isinstance(p, dict) and isinstance(p.get("labels"), list)
            and "in-progress" in p["labels"]
            for p in parsed_by_id.values())
        or any(_iid_num(k) is not None for k in exited))
    # launches_held folds auth AND a sleeping display (#124) into the same fresh-launch suppression the
    # anchor/systemic detectors use, so the queue is held intact (never parked) while either stands,
    # exactly as under a dead anchor. The recovery-relaunch and orphan-resume holds are applied
    # per-issue below. NB: display_asleep is deliberately NOT part of launch_degraded — it must NOT
    # feed the alert/streak surface the way anchor_down/systemic_launch do (a sleeping display is not a
    # fault) — only this fresh-launch/cap-park suppression.
    launches_held = launch_degraded or auth_invalid or display_asleep
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
        # A session whose auth died IN-PROCESS (issue #151 / i336). The runner senses this from the
        # pane and records it; only the owner can fix it, so it is alert-worthy the moment it is
        # seen. It rides the same durable-marker discipline as every reason here: it stands while
        # the state is on disk and auto-clears when the lane reads healthy again.
        #
        # TERMINAL statuses are excluded for the same reason park_label_stuck excludes them
        # (fresh-review P1): a merged/parked lane's last reading is history, not a live problem —
        # nothing will ever re-sense it to clear the field — and an un-clearable reason would pin
        # the ALERT open forever AND poison the dedup for every other reason (the `existing !=
        # reasons` compare below is what decides whether anything at all gets said).
        if (ist_of(iid).get("sensed_state") == "logged_out"
                and _status_of(ist_of(iid)) not in TERMINAL_STATUSES):
            reasons.append(f"session_logged_out:{iid}")
        # A session sitting at its OWN in-window question, past the bound. It is never parked (it
        # is alive), but refusing to park it is exactly why it must speak: nothing else in the loop
        # will ever notice it, and its lane slot is held the whole time. Needs a REAL stamp — an
        # absent/corrupt/future one must not fire on a dialog that just opened (_since_ok is the
        # same usable-clock rule the checks-pending bound uses).
        if (ist_of(iid).get("sensed_state") == "at_dialog"
                and _status_of(ist_of(iid)) not in TERMINAL_STATUSES
                and _since_ok(ist_of(iid).get("sensed_since"), now)
                and now - ist_of(iid)["sensed_since"] >= AT_DIALOG_ALERT_SECONDS):
            reasons.append(f"session_at_dialog:{iid}")
        stamped = ist_of(iid).get("park_notify_at")
        being_absorbed = not gh_stale and _iid_num(iid) in closed_nums
        if (not being_absorbed
                and _status_of(ist_of(iid)) not in TERMINAL_STATUSES
                and isinstance(ist_of(iid).get("park_notify_cause"), str)
                and _real(stamped) and now - stamped >= PARK_LABEL_STUCK_ALERT_SECONDS):
            reasons.append(f"park_label_stuck:{iid}")
    if anchor_down and has_pending_launch:             # a dead anchor only matters with work to launch
        reasons.append("launch_anchor_down")
    if systemic_launch:
        reasons.append("launch_systemic_failure")
    if auth_invalid and (has_pending_launch or has_relaunch_demand):   # dead auth only matters with a
        reasons.append("auth_dead")                    # spend pending (idle -> quiet, like the anchor)
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
        status = _status_of(ist)     # hash-safe: a wrong-typed status folds to a no-set sentinel (#95)
        if status is _CORRUPT_STATUS:
            # A wrong-typed/unhashable status is UNREADABLE lifecycle bookkeeping: hash-safety alone is
            # not enough, because the sentinel would fall through every non-membership branch below as
            # if this were cold state and could still gate -> MERGE a finished PR, or ORPHAN-launch an
            # in-progress one — acting on corrupted state (Codex cross-review). Fail CLOSED by taking NO
            # consequential action on it: the membership sites already keep it out of lanes/claims and
            # fresh launches, and detect_events has surfaced a bounded corrupt_status record naming it, so
            # leaving it fully inert here is the skip the issue asks for. Identity check (`is`) so a raw
            # None cold-start issue — a legitimate member of RELAUNCHABLE_STATUSES — is never caught. (#95)
            continue
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
                    # D11 (issue #161): re-approving a FINISHED build must stop destroying finished
                    # work by default. A report on disk means the worker finished and opened its PR
                    # (its last act is the report, filed only after `gh pr create`), so the default is
                    # RESUME AT THE GATE — re-enter the merge gate on the existing PR and its #154
                    # durable review evidence, building nothing new (`resume_at_gate`). Only the
                    # explicit `rebuild` label — the dashboard's separately-named destructive verb —
                    # or a lane with no finished work to keep takes the rebuild-from-scratch path
                    # (`reapprove`, which prunes the worktree and wipes the report). An investigation
                    # opens no PR (its completion is a marker comment, not a merge) and a bounce
                    # rejects the issue's premise, so both always rebuild.
                    itype = (p.get("type") if isinstance(p, dict) else None) or ist.get("type")
                    if (iid in reports and itype != "investigate" and status != "bounced"
                            and "rebuild" not in labels):
                        out.append({"act": "resume_at_gate", "id": iid, "num": _iid_num(iid)})
                        resumed_now.add(iid)
                    else:
                        # `had_rebuild` tells the executor whether the one-shot `rebuild` label is
                        # actually on the issue, so it clears ONLY a label it knows exists — a
                        # repo-absent `rebuild` (a not-yet-re-adopted repo) is never fed to the
                        # engine's all-or-nothing batched remove (issue #161).
                        out.append({"act": "reapprove", "id": iid, "num": _iid_num(iid),
                                    "had_rebuild": "rebuild" in labels})
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
                # #215: even the reconciled close runs through the exit interview — the marker
                # alone closing green is the motivating incident. A parked lane has no session
                # to interview, so an absent/malformed/unaccounted reply simply leaves it parked
                # (the owner's call); a truthful reply still reconciles: verify (the one typed
                # read), then close on a clean verdict. A STRANDED ack relays first (fresh
                # review P2-4): a lane parked out-of-band (usage/systemic, not the gate's own
                # suppressed paths) can hold a valid nonce-fenced ack the live branch never got
                # to post — the worker DID answer, so the answer must reach the durable thread
                # rather than die with the park. Relay-pending defers the verdict a tick, same
                # as the live branch.
                nonce = ist.get("exit_nonce")
                if isinstance(nonce, str) and nonce and ist.get("exit_ack_relayed") != nonce \
                        and gate.exit_ack_line(acks.get(iid), nonce):
                    out.append({"act": "relay_exit_reply", "id": iid, "num": _iid_num(iid),
                                "line": gate.exit_ack_line(acks.get(iid), nonce),
                                "nonce": nonce})
                    continue
                reply = gate.exit_interview_reply(issue_comments.get(iid))
                state, detail = gate.exit_interview_verdict(reply, ist.get("exit_verify"))
                if state == "close":
                    out.append({"act": "close_investigate", "id": iid, "num": _iid_num(iid),
                                "exit": detail})
                elif state == "verify":
                    out.append({"act": "verify_exit_refs", "id": iid, "num": _iid_num(iid),
                                "refs": detail, "reply_key": reply.get("key")})
            continue                                   # else re-release happens via labels (phase E)
        num = _iid_num(iid)
        p = parsed_by_id.get(iid)
        blocked_text = blocked.get(iid) if isinstance(blocked.get(iid), str) else None
        has_report = iid in reports
        has_exited = iid in exited
        retries = ist.get("retries", 0)

        # ---- awaiting a durable question's answer (#163): the worker exited cleanly, the question
        #      is a durable GitHub comment, the window is closed and the lane is free. Nothing here
        #      relaunches, recovers, or nudges — an owner-question issue holds NO live window. The
        #      ONE thing that moves it is the owner's answer: the approval verb (a fresh `agent-ready`
        #      on this awaiting_answer issue) -> a fresh session that embeds the Q&A in its brief and
        #      reuses the pushed WIP. `continue` here keeps it exempt from every liveness/recovery/
        #      launch path below (including a stray turn-end `exited` stamp left by the closed pane). ----
        if status == "awaiting_answer":
            labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
            if "agent-ready" in labels:
                out.append({"act": "answer_relaunch", "id": iid, "num": num})
            continue

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
                # ---- the exit interview (issue #215): the marker opens an interview, never the
                # close. All facts the gate's ladder needs are assembled HERE (the gate stays a
                # clockless pure function): the newest reply parsed from the SAME comments read
                # (zero added per-tick reads), the degraded-path ack relay, and the clock-derived
                # window/delivery booleans. ----
                exit_reply = gate.exit_interview_reply(view_comments)
                exit_relay_pending = False
                nonce = ist.get("exit_nonce")
                if isinstance(nonce, str) and nonce and ist.get("exit_ack_relayed") != nonce:
                    # Codex Stop can't block a stop, so its reply arrives as an ack FILE (the
                    # #157 channel, nonce-fenced); the runner posts it as the durable comment.
                    # While that relay is in flight the gate WAITS — the answer is already in
                    # the runner's hands, so re-asking or parking would contradict it.
                    line = gate.exit_ack_line(acks.get(iid), nonce)
                    if line:
                        out.append({"act": "relay_exit_reply", "id": iid, "num": num,
                                    "line": line, "nonce": nonce})
                        exit_relay_pending = True
                # The reply window runs from the LATER of the ask and its consumption receipt:
                # delivery is judged by the receipt (never a send rc), and a mail consumed late —
                # a worker mid-long-turn — gets its full window from actual delivery. An
                # unreadable ask clock reads as expired: the ladder is bounded (asks cap), so
                # corruption walks to the park, never to an unbounded wait.
                asked_at = ist.get("exit_asked_at")
                receipt = exit_receipts.get(iid)
                exit_delivered = (_since_ok(asked_at, now) and _since_ok(receipt, now)
                                  and receipt >= asked_at)
                if _since_ok(asked_at, now):
                    base = receipt if (exit_delivered and receipt > asked_at) else asked_at
                    exit_expired = now - base >= exit_window
                else:
                    exit_expired = True
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
                exit_reply = None                      # the interview is investigate-only (#215)
                exit_relay_pending = exit_delivered = exit_expired = False
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
            # issue #222: the compliance window. The gate is clockless, so decide computes which
            # already-nudged keys have EXPIRED (their window ran out) and passes the subset in — the
            # same split as the exit interview's `exit_ask_expired`. The window runs from `nudged_at`
            # (the delivery stamp _exec_nudge writes). An unreadable/absent/future stamp reads as
            # EXPIRED via the _since_ok discipline (fail closed to the park; the ladder stays bounded,
            # never an unbounded wait). Only keys actually in `nudged` are considered.
            nudged_at = ist.get("nudged_at")
            nudge_expired = []
            if isinstance(nudged, list):
                for k in nudged:
                    # `k` is a str in every path the engine writes; guard it anyway so a hand-corrupt
                    # non-str (worse, non-hashable) element can't raise in dict.get and break decide's
                    # never-raise contract — a bad key simply reads as expired (park), fail closed.
                    stamp = nudged_at.get(k) if isinstance(nudged_at, dict) and isinstance(k, str) \
                        else None
                    if not _since_ok(stamp, now) or now - stamp >= nudge_window:
                        nudge_expired.append(k)
            conflicts = ist.get("conflicts", 0)
            inflight = {}
            for other in ist_map:
                oist = ist_of(other)
                if other != iid and _status_of(oist) in INFLIGHT_STATUSES:
                    ot = oist.get("declared_touches")
                    inflight[other] = [t for t in ot if isinstance(t, str)] \
                        if isinstance(ot, list) else []

            # issue #165: the owner's referee pre-authorization is read from the issue's LIVE labels
            # (his word, applied at approval), precomputed here the same way investigation_done is —
            # the gate consumes the bool, never the label set. Absent/wrong-typed labels -> False, so
            # the bright line holds by default.
            pre_auth_referee = gate.preauthorized_referee(p.get("labels")) if isinstance(p, dict) \
                else False
            g = gate.gate_decision(
                {"type": itype, "conflicts": conflicts, "nudged": nudged,
                 "nudge_expired": nudge_expired,
                 "declared_touches": declared, "update_result": update_result,
                 "review_carry": ist.get("review_carry"),
                 "pre_authorized_referee": pre_auth_referee,
                 "investigation_done": inv_done,
                 "exit_reply": exit_reply, "exit_asks": ist.get("exit_asks"),
                 "exit_asked_key": ist.get("exit_asked_key"),
                 "exit_verify": ist.get("exit_verify"),
                 "exit_ask_expired": exit_expired, "exit_delivered": exit_delivered,
                 "exit_relay_pending": exit_relay_pending},
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
                # head_oid pins the merge to the commit this decision actually judged (#154): the
                # review verdict was matched against THIS head, so the merge must land on it or be
                # refused — never on whatever the branch grew since the last poll.
                merge_act = {"act": "merge", "id": iid, "num": num, "pr": pv.get("number"),
                             "method": method, "head_oid": head, "wander": wander}
                if g.get("referee_preauthorized") is True:
                    # (#165) Carry the owner's pre-authorization onto the ACT. The gate already
                    # decided it; the executor must never re-derive it. Without this the journal
                    # record (_journal_outcome writes the act verbatim) and the merge comment would
                    # both recite the ORDINARY green rationale for a diff that crossed a bright line
                    # on his word — and the journal naming the referee paths is the one compensating
                    # control approval-protocol.md offers for the coarse per-issue grant.
                    merge_act["referee_preauthorized"] = True
                    merge_act["referee_paths"] = g.get("referee_paths")
                out.append(merge_act)
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
                # (#150 / D8) The gate's verdict is "hire a session to resolve this conflict" — a
                # session START, so it owes the same eligibility every other start owes. It asked
                # nothing before. Held (never parked) so the issue keeps its preserve-labeled PR and
                # the resolver hires the tick the gate passes. Dead account auth (#159) holds it too:
                # a conflict resolver spawned into logged-out auth would burn the spend like any start.
                if auth_invalid:
                    launch_hold(iid, num, p,
                                reason="account auth is not valid — conflict resolve held (see the auth_dead alert)")
                elif display_asleep:
                    # Display is asleep (issue #124): the conflict resolver is a session START — a fresh
                    # tab macOS will not boot while the display sleeps. HOLD like the auth sibling (keeps
                    # its preserve-labeled PR); it hires the tick the display wakes.
                    launch_hold(iid, num, p,
                                reason="the display is asleep — conflict resolve held; macOS will not "
                                       "boot the new tab's shell until wake, when it resumes automatically")
                elif start_ok(p):
                    out.append({"act": "resolve_conflict", "id": iid, "num": num,
                                "pr": pv.get("number"), "wander": wander})
                else:
                    launch_hold(iid, num, p)
            elif act == "close_investigate":
                out.append({"act": "close_investigate", "id": iid, "num": num,
                            "exit": g.get("exit")})
            elif act == "exit_interview":
                # deliver (or re-deliver) the interview through the worker channel — the
                # executor stamps the ask BEFORE the send, so failure walks toward the cap.
                out.append({"act": "exit_interview", "id": iid, "num": num,
                            "reply_key": g.get("reply_key"), "defect": g.get("defect")})
            elif act == "verify_exit_refs":
                # the ONE added GitHub read per finishing investigation (#215): a typed
                # child-set search whose verdict is stamped against this reply's key, so it
                # never re-fires for the same reply (and a refused read stamps nothing — the
                # gate re-emits next tick, which IS the wait).
                out.append({"act": "verify_exit_refs", "id": iid, "num": num,
                            "refs": g.get("refs"), "reply_key": g.get("reply_key")})
            # "wait" -> no action this tick

            # The issue LEFT the failing state without the park label ever landing (issue #61):
            # the gate reached a non-park verdict while the notify-once marker is still stamped.
            # Clear it so a LATER genuine park on this issue texts again — the guard is
            # per-cause-EPISODE, never forever. One-shot: the cleared field stops re-emission.
            if isinstance(ist.get("park_notify_cause"), str) and iid not in parked_now:
                out.append({"act": "clear_park_marker", "id": iid})
            continue

        # ---- in flight: absorb a PR that concluded OUT OF BAND (issue #155) ----
        # The lane is still building, so nothing below here would ever look at its PR — which is how
        # i328 stalled the afternoon queue for two hours: its PR was merged out-of-band while the
        # runner, which associated branch->PR only when an issue FINISHED, carried pr: null and never
        # learned the PR existed. _refresh_inflight_prs now reconciles every in-flight lane each
        # tick, so the fact is in the view; this acts on it. Checked BEFORE the blocked/answerer and
        # frozen/idle lifecycles below, so a concluded lane stops spinning them (that spinning WAS
        # the stall) — and only on a POSITIVE answer about this lane's ACTIVE branch:
        #   * a FRESH view, and the read PRESENT (`iid in prs`). The refused!=empty discipline (#61):
        #     the poll and the refresh OMIT a refused lookup, so presence == "GitHub answered".
        #   * a real PR number. Answered-empty ({}) is the NORMAL mid-build state — the worker has
        #     not pushed yet — so it means KEEP BUILDING. It is never "no PR exists", never a park.
        #   * not `superseded` — a belt-and-braces guard mirroring the orphan sweep's own rule. In
        #     practice a superseded PR cannot BE the active branch's answer (regenerate stamps the
        #     new branch before it ever labels the old PR), so this should never fire; it costs one
        #     condition to stay correct if that ordering is ever relaxed.
        if status in INFLIGHT_STATUSES and not gh_stale and iid in prs:
            pv = prs.get(iid) if isinstance(prs.get(iid), dict) else {}
            pr_num = pv.get("number")
            if pr_num and "superseded" not in gate._pr_labels(pv):
                state = pv.get("state")
                if state == "MERGED":
                    # The work LANDED, whatever this lane still thinks it is doing. absorb_merged is
                    # idempotent and tears the session down in order (#149), which frees the slot.
                    # Deliberately NOT episode-scoped the way CLOSED is below: a merge is a landed
                    # fact about the BRANCH, true whoever opened the PR, and the runner may have
                    # slept through the whole open->merge (a wake gap) and so never recorded it.
                    # Refusing to settle without a recorded number would re-open the i328 stall.
                    out.append({"act": "absorb_merged", "id": iid, "num": num})
                    continue
                if state == "CLOSED" and pr_num == ist.get("pr"):
                    # THIS episode's PR was closed under it. Out-of-band by construction: the runner
                    # never closes its own PR (regenerate supersedes it and leaves it OPEN on a
                    # preserved branch; the janitor's close path vetoes every claimed lane), so this
                    # was a deliberate human call. Why is unknowable here, and both guesses are
                    # wrong — rebuilding loops against the call, merging is not ours to make — so
                    # hand it back ONCE and stand the lane down. No LLM, no retry ladder. This is
                    # the gate's own long-standing verdict for a closed PR, moved earlier so the
                    # lane stops now instead of after a build nobody will merge.
                    #
                    # The `pr_num == ist["pr"]` scope is load-bearing, not caution (fresh-agent
                    # review P0): re-approving clears `pr` but KEEPS the branch stamp, so a
                    # relaunched lane rebuilds on the same branch — where the old CLOSED PR still
                    # answers the lookup. A closed PR does not stop a NEW PR on that head (GitHub
                    # refuses only a second OPEN one), which is exactly how this recovered before
                    # #155: the worker opened a fresh PR and the newest-first lookup returned it.
                    # Unscoped, this park would fire a tick after launch — before the worker can
                    # push — and re-park forever, with the owner's only remedy being the very
                    # re-approval that re-triggers it. `pr: None` == this episode owns no PR yet.
                    park(iid, num,
                         f"PR #{pr_num} for branch '{ist.get('branch') or '?'}' was CLOSED without "
                         "merging while this issue was still building — external intervention, so "
                         f"{operator} decides. The lane is stood down rather than rebuilt over that "
                         "call; the branch and its commits are untouched on the remote.",
                         needs_william=True, cause="pr_closed")
                    continue
                if state == "OPEN" and pr_num != ist.get("pr"):
                    # Record the number the moment a PR exists (the issue's first DoD line). Durable,
                    # so it survives the restart that re-derives everything else — and it is what
                    # tells a later tick whether a CLOSED PR is this episode's or a previous one's
                    # ghost. OPEN only: a MERGED/CLOSED PR is handled above or ignored as history,
                    # and recording one would let a ghost masquerade as this episode's own.
                    out.append({"act": "record_pr", "id": iid, "pr": pr_num})

        # ---- blocked marker present: bounce, or a durable owner-decision question ----
        # A question or a bounce is handled EVEN IF an `exited` marker is also present: in the #163
        # model the worker EXITS right after writing its question (start-session.sh then stamps
        # `exited`), so blocked+exited is the NORMAL hand-off, not a crash. The blocked branch owns
        # it and `continue`s, so the exited-recovery ladder below never mistakes a clean question-exit
        # for a crash to relaunch. (post_question / bounce consume the marker, so this cannot loop.)
        if blocked_text is not None:
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
                # Night-batching (#164): a bounce is an owner-decision hand-back — held to the
                # morning report during quiet hours. The bounce ACTION still fires (the label move,
                # the journal, the report); only the 3am page waits. A stuck bounce-label still
                # escalates via park_label_stuck (a failing GitHub write is systemic and pages).
                if not notify_quiet:
                    notify(f"superlooper: {iid} bounced (needs-owner)", blocked_text)
                out.append(act)
                continue
            # A real owner-decision question (#163). The worker has posted a structured question to
            # its blocked file, pushed its WIP, and ended its turn. Instead of hiring an answerer to
            # nudge a live-frozen pane (the model that died with i336's auth death and i280's zombie
            # window), the runner posts the question DURABLY as a GitHub comment, closes the window,
            # and releases the lane (post_question). The owner's answer — the approval verb — later
            # relaunches a fresh session with the Q&A embedded. At most QUESTION_CAP questions per
            # issue; a corrupt/at-cap counter means a THIRD, which is a scoping problem only the owner
            # can untangle, so it hands back to needs-owner with the question quoted (never a third
            # round-trip). The notify fires once per question (gated on the post-once stamp), so a
            # re-derived tick — the label move retrying — never re-texts.
            asked, corrupt = _counter(ist, "questions_asked")
            if corrupt or asked >= QUESTION_CAP:
                park(iid, num, f"third owner question on issue #{num} (cap {QUESTION_CAP}) — a "
                               f"scoping problem only {operator} can untangle, not another "
                               f"round-trip. latest question: {blocked_text!r}",
                     needs_william=True, cause="question_cap")
            else:
                # Night-batching (#164): a durable owner question is an owner DECISION — held to the
                # morning report during quiet hours. post_question still fires, so the question is
                # posted DURABLY as a GitHub comment (the owner sees it on the dashboard / in the
                # report); only the 3am page waits. Gated on the post-once stamp too, so a re-derived
                # tick never re-pages.
                if not ist.get("question_posted") and not notify_quiet:
                    notify(f"superlooper: {iid} needs an answer",
                           f"a worker exited on a question and is waiting for {operator}:\n\n"
                           f"{blocked_text}")
                out.append({"act": "post_question", "id": iid, "num": num, "question": blocked_text})
            continue

        # ---- liveness recovery: exited beats frozen beats idle ----
        if has_exited:
            # A launch that died at startup left its real reason in the captured stderr tail (#40);
            # name it in whichever exited-park memo fires, so the operator sees the actual error and
            # not just the relaunch count.
            stderr_memo = _launch_stderr_memo(launch_stderr.get(iid))
            if auth_invalid:
                # Account auth is dead (issue #159): a relaunch would start LOGGED OUT and burn the
                # spend. HOLD (the auth_dead ALERT above names it) — never park (auth is the owner's
                # to fix, and the lane resumes the tick it reads healthy). Checked FIRST so a lane
                # already at its retry cap holds too, rather than parking under a fault that is not
                # its own; the relaunch charges no attempt because it never fires.
                launch_hold(iid, num, p,
                            reason="account auth is not valid — relaunch held (see the auth_dead alert)")
            elif display_asleep:
                # Display is asleep (issue #124): the recovery relaunch boots a FRESH tab, whose shell
                # macOS will not schedule while the display sleeps — the exact orphan-and-burn #124
                # prevents. HOLD like the auth sibling (no attempt, no streak entry, no alert, no park
                # even at cap); it resumes the tick the display wakes. Quiet — a sleeping display is
                # normal overnight behavior, not a fault.
                launch_hold(iid, num, p,
                            reason="the display is asleep — relaunch held; macOS will not boot the "
                                   "new tab's shell until wake, when it resumes automatically")
            elif type(retries) is not int:             # corrupt counter -> to William, not a loop
                park(iid, num, "exited, and the retry counter is unreadable — parking" + stderr_memo,
                     cause="exited_cap")
            elif retries >= retry_cap:
                park(iid, num, f"exited and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking" + stderr_memo, cause="exited_cap")
            elif start_ok(p):
                out.append({"act": "recover", "id": iid, "tier": "exited"})
            else:
                # (#150 / D8) This tier used to ask usage ALONE, so a crash recovery relaunched a
                # worker straight past a `blocked-by` that had since reopened — the only thing that
                # stopped it was the worker's own step-0 reconcile bouncing itself. It now asks the
                # WHOLE gate. Usage is unchanged (start_ok's usage half IS usage_launchable's rule):
                # no headroom still means the marker persists and the relaunch resumes with the
                # quota — only now the wait says why, instead of passing silently.
                launch_hold(iid, num, p)
            continue
        if iid in frozen_ids or status == "frozen":
            if in_wake_grace:
                continue                               # wake grace: hold the recovery ladder (issue #42)
            # ---- un-latch a LATCHED `frozen` status when the progress clock advances (issue #231) ----
            # The stored status latched to `frozen`, but the frozen tier `continue`s before the #157
            # probe ladder below, so nothing ever notices when the session resumes real work: it keeps
            # the stale paint and draws the 10-minute nudge until its report lands (observed live, 360
            # eApp 2026-07-16 — a session that had already opened its PR). Key the un-latch on the
            # PROGRESS clock (HEAD / report / blocked marker), NEVER on activity: a nudge refreshes the
            # pane's activity — an activity-keyed un-latch would let the ladder answer itself (i328) —
            # but it can never move HEAD/report/blocked. Only an already-LATCHED status is un-latched;
            # a lane crossing INTO frozen this tick (`frozen_ids`, status still 'running') takes the
            # normal recover below, which is what stamps the baseline the comparison reads.
            if status == "frozen":
                cur_sig = events_mod.progress_signature(status_clocks.get(iid))
                if cur_sig is not None:
                    baseline = ist.get("progress_sig")
                    if events_mod.progress_advanced(baseline, cur_sig):
                        # The session demonstrably resumed: a report/blocked marker, or a HEAD movement
                        # between two real commits, since the freeze baseline. Flip back to `running`,
                        # end the frozen ladder, journal the transition (evidence class named), and
                        # start a fresh #157 episode. `progress_advanced` is the fail-CLOSED gate: a
                        # corrupt baseline or a head that merely became git-UNREADABLE ('None') is NOT
                        # an advance, so a nudge waking the worker can never un-latch its own ladder
                        # (i328). The action is `unlatch_frozen`, deliberately NOT `unfreeze` — that
                        # verb is the unrelated MERGES-frozen mechanism (state/merges_frozen.json), a
                        # different machine that happens to share the word (this issue's boundary).
                        out.append({"act": "unlatch_frozen", "id": iid, "num": num, "sig": cur_sig,
                                    "evidence_class": events_mod.progress_evidence(baseline, cur_sig)})
                        continue
                    if not events_mod.usable_baseline(baseline) and events_mod.usable_baseline(cur_sig):
                        # No baseline fit to measure a future advance against — it froze before the
                        # probe ladder ever anchored it (an `awaiting` lane, whose ladder is suppressed),
                        # the freeze stamp found no readable clock, or a stored baseline is
                        # corrupt/unreadable-head. ANCHOR the current (readable) signature so a later
                        # PROVEN advance is measurable, and never read a first/repaired baseline as
                        # progress itself (mirrors the ladder's own first-sight-anchors-without-acting,
                        # #157). Anchor only to a readable signature — never poison the baseline with an
                        # unreadable 'None' head. A good baseline that simply did not advance falls
                        # through to the nudge ladder, its last-known-good head preserved.
                        out.append({"act": "progress_advance", "id": iid, "sig": cur_sig})
                        continue
            # The lane looks frozen by the clock, but the runner has SENSED why (issue #151), and
            # in both cases PARKING is the wrong answer:
            #   logged_out (i336) — auth died in-process. Alerted above and held for the owner; a
            #     park would just bury it behind a memo that names the wrong cause.
            #   at_dialog (i280) — the session is ALIVE and asking something in-window; going quiet
            #     to wait on an answer is not a fault, and parking a working lane on that evidence
            #     is the exact bug this issue exists to end. But "waiting on a human" would be too
            #     generous a reading: nobody is watching that pane, and an in-window question
            #     bypasses the blocked-file/answerer channel, so the wait can be endless — which is
            #     why refusing to park it obliges the bounded session_at_dialog ALERT above. Alive
            #     is not the same as fine.
            # The recover still fires (below). That is deliberate and load-bearing: `recover` is
            # what RE-READS the screen, and _record_sensed — the only writer of sensed_state — runs
            # nowhere else. Suppressing it too would make the field impossible to clear, and the
            # lane would go silent forever on a reading that had long since stopped being true
            # (fresh-review P0). It delivers nothing — nudge-pane refuses to type at both states
            # (rc 5/6) — so this is a re-sense, not a nudge, and the state self-clears the moment
            # the screen says something else.
            sensed = ist.get("sensed_state")
            parking = False
            if sensed in ("logged_out", "at_dialog"):
                parking = False                        # sensed alive/held: re-sense, never park
            elif type(retries) is not int:
                park(iid, num, "frozen, and the retry counter is unreadable — parking",
                     cause="frozen_cap")
                parking = True
            elif retries >= retry_cap:
                park(iid, num, f"frozen and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking", cause="frozen_cap")
                parking = True
            if not parking:
                last_rec = ist.get("last_recover_at")
                last_rec = last_rec if _real(last_rec) else 0
                if now - last_rec >= RECOVER_RETRY_SECONDS:
                    out.append({"act": "recover", "id": iid, "tier": "frozen"})
            continue

        # ---- progress-stall probe ladder (issue #157): keyed on the PROGRESS clock, not activity ----
        # THE i328 fix. The idle peek below keys on activity_mtime, which the nudge itself refreshes —
        # so it could never escalate (8 nudges ~497s apart, forever). This tier keys on
        # state/status/<id>.json (HEAD + the report/blocked markers), which move ONLY on real
        # progress and are immune to a probe or its ack. A lane making progress re-anchors and is
        # NEVER parked; a lane taking turns without progressing is probed a BOUNDED number of times
        # (each demanding a machine-readable ack file), then escalated with a dossier. The frozen
        # tier above already claimed dead/frozen lanes, so a lane reaching here is ALIVE (taking
        # turns) — "turns taken but no progress" is exactly what this measures.
        clock = status_clocks.get(iid)
        cur_sig = events_mod.progress_signature(clock)
        have_clock = cur_sig is not None
        if have_clock and status in INFLIGHT_STATUSES and not in_wake_grace and iid not in awaiting:
            since = ist.get("progress_since")
            if ist.get("progress_sig") != cur_sig or not _since_ok(since, now):
                # first sight / real progress / a corrupt clock: re-anchor and (on a genuine change)
                # reset the episode. The executor clears the probe counters ONLY when the signature
                # actually changed, so a mere since-repair never drops an in-flight escalation.
                out.append({"act": "progress_advance", "id": iid, "sig": cur_sig})
                continue
            if now - since >= progress_stall_secs:
                # STALLED: turns taken, no commit/marker/HEAD change for the whole window.
                nonce = ist.get("probe_nonce")
                ack_state = events_mod.parse_ack(acks.get(iid), nonce) if isinstance(nonce, str) else None
                attempts, attempts_corrupt = _counter(ist, "probe_attempts")
                if attempts_corrupt:
                    # A wrong-typed probe counter means the cap can't be trusted — fail CLOSED to a
                    # park (the same discipline retries/merge_refusals/answerer_failures hold), never
                    # re-read it as 0 and re-probe (that is the fail-OPEN-on-wrong-typed defect class).
                    park(iid, num, f"{iid}: progress-stall park (issue #157) — the probe-attempt "
                                   f"counter is unreadable (corrupt state), so the bounded ladder "
                                   f"cannot be trusted; parking for review.", cause="progress_stall")
                    continue
                if (ack_state == "DONE" and iid not in reports
                        and not ist.get("harvest_tried")):
                    # THE REPORT HARVEST'S TRIGGER (issue #189). Look for the report before
                    # concluding there is none: "acked DONE, but produced no report" is the exact
                    # i280/i328 stall (a finished worker that wrote its report one directory off),
                    # and it used to end in a park. A DONE ack is the ONLY mechanical "I am
                    # finished" the loop ever gets — nonce-fenced, and only ever asked of a lane
                    # this ladder found progress-stalled and nudge-pane found IDLE. That is the
                    # discriminator the Stop hook could never have, which is why the harvest fired
                    # on every rest and promoted two live drafts on 07-16 (i153/i163).
                    # ONCE per episode (`harvest_tried`, cleared by progress_advance): a report
                    # that genuinely does not exist must not re-harvest every tick — that is the
                    # i328 loop in a new costume. A fruitless attempt falls through to the cap
                    # below and parks exactly as it always did.
                    # What stops a LANDED harvest from re-firing is the has_report branch far
                    # above, which owns a lane the moment a report is visible and never reaches
                    # here. Note it is NOT the progress signature: the clock's `report` field is
                    # stamped by the worker's own Stop hook, and a finished worker takes no
                    # further turn, so a harvest the RUNNER performs never moves that signature
                    # (fresh review P2 — this comment used to claim it did).
                    out.append({"act": "harvest_report", "id": iid, "num": num})
                    continue
                if ack_state == "STUCK" or (type(probe_cap) is int and attempts >= probe_cap):
                    # cap exhausted, OR the worker explicitly asked for help: escalate to a
                    # classified park with a dossier — never an infinite loop, never a false park of
                    # a lane still making progress (a progressing lane re-anchored above). STUCK /
                    # WAITING reach the OWNER (a live worker needing a human); a silent or
                    # WORKING-lying lane goes to the parked queue for review.
                    memo = _progress_stall_memo(iid, clock, now - since, attempts, ack_state)
                    park(iid, num, memo, needs_william=ack_state in ("STUCK", "WAITING"),
                         cause="progress_stall")
                else:
                    last_probe = ist.get("probe_sent_at")
                    if not _real(last_probe) or now - last_probe >= probe_retry_secs:
                        out.append({"act": "probe", "id": iid, "num": num, "attempt": attempts + 1})
                continue                                    # a stalled lane is fully handled here
            # progress fresh: fall through to label reconciliation; the idle fallback below is gated
            # on `not have_clock`, so a clock-bearing lane is never nudged on activity staleness.
        if iid in idle_ids and not have_clock:
            # fallback ONLY when there is no progress clock (an install/session that never stamped
            # state/status/<id>.json — the pre-#148 shape, or a hook that failed): the old activity
            # peek, degraded-but-safe. A clock-bearing lane is handled by the tier above instead.
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
            if launches_held:
                continue                               # SYSTEMIC launch fault (#24) / dead auth (#159)
                                                       # / a sleeping display (#124): hold, never park
                                                       # per-issue — the queue stays intact for when it
                                                       # resolves (the anchor/auth alert stands; a
                                                       # sleeping display carries none — the park just
                                                       # defers to wake)
            # The evidence the runner captured at the MOMENT the launch failed (issue #152) — the
            # launcher's own stderr, stamped into loopstate by _exec_launch. This is what lets the
            # memo below name the component actually at fault instead of guessing one.
            launch_ev = ist.get("launch_evidence")
            if ist.get("launch_error") == "base_missing":
                # issue #28: launch-session.sh could not create the worktree because its base ref
                # origin/<dev_branch> does not exist. Name the REAL cause — the missing base branch
                # — instead of sending the newcomer to debug the launch shim (the wrong component).
                # The captured stderr rides along (#152) so the operator can check this reading
                # against the launcher's own words rather than take the runner's word for it.
                park(iid, num, f"launch never delivered: the worktree base branch "
                               f"'origin/{dev_branch}' does not exist, so every worktree creation "
                               f"fails before Claude starts — a repo/config fault, not a "
                               f"launch-delivery problem. Set `dev_branch` in "
                               f".superlooper/config.json to the repo's real default branch "
                               f"(`superlooper adopt` detects it; `superlooper doctor` validates "
                               f"it), then re-approve." + _captured_addendum(launch_ev),
                     cause="launch_base_missing")
            else:
                # (#152) This memo used to ask "is the launch shim installed?" for EVERY non-base
                # cause. On 2026-07-09 it said that to ten issues in a row while the real fault —
                # a launch anchor pointing at a deleted cmux workspace — sat in the stderr the
                # runner had already read and dropped. The shim was installed, and innocent; the
                # launch never reached it. Now the memo speaks from the evidence: it names the
                # anchor when the anchor is dead, still names the shim when rc=2 says the shim
                # genuinely never fired, and admits "reason unknown" when nothing was captured —
                # which is the one thing the old memo could never bring itself to do.
                park(iid, num, evidence.park_memo(launch_ev, attempts=LAUNCH_FAILURE_CAP),
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
            if resumable and auth_invalid:
                # Account auth is dead (issue #159): resuming the orphan on its PR branch would start
                # LOGGED OUT and burn the spend. HOLD in place (never reclaim — reclaim would orphan
                # the pushed work, same reasoning as the gate-hold below); the auth_dead ALERT names
                # it and the resume fires the tick auth reads healthy again.
                launch_hold(iid, num, p,
                            reason="account auth is not valid — resume held (see the auth_dead alert)")
            elif resumable and display_asleep:
                # Display is asleep (issue #124): the orphan resume boots a FRESH tab (orphan: True),
                # whose shell macOS will not schedule while the display sleeps. HOLD in place like the
                # auth sibling (never reclaim — reclaim would orphan the pushed work); it resumes the
                # tick the display wakes.
                launch_hold(iid, num, p,
                            reason="the display is asleep — resume held; macOS will not boot the new "
                                   "tab's shell until wake, when it resumes automatically")
            elif resumable and not start_ok(p):
                # (#150 / D8) The restart rebuild resumes an in-progress issue's session on its open
                # PR's branch — and asked NO gate at all: not eligibility, not even usage. HOLD in
                # place rather than reclaim: this orphan resume is the ONLY path that re-attaches the
                # existing PR branch to a worktree (`orphan: True`), so requeueing to the fresh-launch
                # path would later try to build the branch afresh and orphan the pushed work. Held,
                # the issue keeps its PR and resumes properly the tick the gate passes.
                launch_hold(iid, num, p)
            elif resumable:
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
                    or iid in resumed_now      # just re-claimed for the gate (#161): never rebuild it
                    or _status_of(ist) not in RELAUNCHABLE_STATUSES   # wrong-typed status: never launch (#95)
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

    if not gh_stale and not issue_state_corrupt_for_launches and not launches_held:
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
        # Foreseeable-referee launch hold (issue #165): an approved issue whose declared touches
        # resolve to a referee path is withheld by launch_ok unless pre-authorized — so it never
        # reaches launchable/launch_holds above. Journal WHY here (via the same #150 launch_hold
        # ledger, deduped once per episode) rather than withholding it silently: "why isn't my
        # approved issue running?" must be answerable. Journal-only — no park, no notify, no label
        # move — the queue is untouched and the issue launches the tick a pre-authorization lands.
        for c in candidates:
            cid = c.get("id")
            if cid in selected_ids:
                continue
            if gate.foreseeable_referee_stop(c.get("touches"), cfg) \
                    and not gate.preauthorized_referee(c.get("labels")):
                launch_hold(cid, c.get("num"), c)
                continue
            # Refused closed-list read (issue #172). launch_ok withheld this candidate because its
            # `blocked-by` is unsatisfied in a closed set the poll could not actually READ — so on
            # the fresh path it was dropped SILENTLY, and a throttle made every blocked-by issue
            # quietly un-launchable for as long as it lasted. Journal WHY, through the same #150
            # ledger (deduped once per cause), naming the refusal rather than the dependency.
            # Deliberately NOT emitted for a LANDED read: a genuinely open dependency is the loop
            # working as designed and must not walk the journal every tick.
            refused_deps = _refused_closed_read_deps(c, closed_nums, closed_read_ok)
            if refused_deps:
                launch_hold(cid, c.get("num"), c, reason=_refused_closed_read_reason(refused_deps))
    elif (systemic_launch and not anchor_down and not auth_invalid and not display_asleep
            and not gh_stale and not issue_state_corrupt_for_launches):
        # ...and NOT while the display sleeps (#124): a canary into a sleeping display would just
        # create+close an orphan, the very burned attempt this hold exists to prevent. The systemic
        # streak stays intact (the alert still stands), and the canary fires the tick the display wakes.
        # Canary re-arm of the SYSTEMIC hold (issue #115). The streak-based hold cannot clear itself,
        # so once per CANARY_RETRY_SECONDS since the last delivery failure, probe with ONE canary
        # launch of the front-of-queue issue. Suppressed while auth reads DEAD (#159): a canary into
        # dead account auth would just start logged-out and re-fail — hold until auth recovers.
        # A verified delivery clears the streak (runner) and normal
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
