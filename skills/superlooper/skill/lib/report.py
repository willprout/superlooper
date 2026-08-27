"""The morning report + promotion evidence — rendered from the journal (plan Task 11/12).

PURE and FAIL-CLOSED. morning()/promotion() take already-read data and return markdown; no disk,
no gh, no clock. Every input is coerced to a safe empty shape rather than trusted, and a corrupt
or wrong-typed record is skipped, never fatal — the runner-ops promise is that a broken overnight
never takes down the report, it renders honestly ("could not parse", "nothing happened") so
William is never shown a blank that reads as either broken OR falsely green.

The journal is the durable record of what the runner DID overnight; every action record is the
actions.decide() dict + an "outcome" ("ok" or a reason), each ts-stamped by journal.append. This
module reads those back. The report's CURRENT-state facts (report date, freeze marker, ready
queue, usage) arrive via `view` — the live snapshot the runner/CLI assembles for the report; its
keys are documented on morning() below.
"""
import math

WEEK_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600

# How long a standing hold — or the merge freeze — may stand before the report stops merely LISTING
# it and raises an ALERT-tier line above the sections (issue #405). A hold is a WAIT by design: the
# cause is re-derived every tick and clears itself the moment the world changes, so hours of holding
# are normal and must not cry wolf. A FULL DAY of it is not a wait any more, it is a stall — nothing
# in the loop will move that issue until the cause clears or the owner does something, and the eApp
# loop proved a stall can sit silent for three days. The freeze shares the threshold for the same
# reason: a dev-check freeze clears within a tick of dev going green and a nightly freeze within one
# night, so a freeze still standing a day later has outlived both of its own recovery paths.
HOLD_ALERT_SECONDS = DAY_SECONDS
FREEZE_ALERT_SECONDS = DAY_SECONDS

# The per-issue statuses that END a hold episode: the issue is done with the loop, so a stamp left
# standing on it is history, not a hold. MIRRORS actions.TERMINAL_STATUSES — deliberately duplicated
# rather than imported, because this module is the pure renderer and importing the runner's brain to
# read one frozenset would drag the whole decide graph into every report. tests/test_report.py pins
# the two together, so they cannot drift apart silently.
_TERMINAL_STATUSES = frozenset({"merged", "parked", "needs_william", "bounced"})

# actions.ALERT_UNREADABLE — the sentinel a state/ALERT read yields when the marker EXISTS but could
# not be parsed (#320). Mirrored rather than imported for the same reason the set above is: this is
# the pure renderer, and it must not drag the decide graph into every report. tests pin the pair.
_ALERT_UNREADABLE = "alert_unreadable"

# A `launch_hold_reason` that begins with this is the ONE stamp whose content says the issue is NOT
# held: issue #172's retirement prose, written when a previously-unlanded closed-list read has since
# landed and the gate now PASSES. The engine has no clear-the-stamp verb, so it retires a stale stamp
# by overwriting it with an honest all-clear — which means a truthy `launch_hold_reason` is not, by
# itself, evidence of a hold. Listing one would print a self-refuting line ("has been held 1d 6h —
# this issue is no longer held by the eligibility gate at all"), and an issue merely waiting for lane
# capacity would raise a STALL alert at 24h. MIRRORS actions._LANE_BOUND_PREFIX, matched on the
# PREFIX for the reason that constant documents (the tail prose may differ across releases, and a
# stamp that cannot be recognized cannot be corrected). tests/test_report.py pins the two together.
_LANE_BOUND_PREFIX = "the closed-issue list read has since LANDED"

# The three durable stamps a standing hold dedups its JOURNALING on (issue #405). Each entry:
# (stamp, the age clock its executor stamps beside it, the journal act that carries its prose, the
# short TAG the report line uses, prose to fall back on when no reason can be recovered, and whether
# the stamp ITSELF is the reason).
#
# The stamps are the loop's own record of who is held: every one is cleared at the exact points an
# issue stops being held (launch, recover, resolve, park, re-approve), so a standing stamp means
# "this issue has not moved since the loop refused it".
#
# Only `launch_hold_reason` (#150) carries prose. `wildcard_hold_journaled` (#36) is a bare bool, and
# `queue_invalid_signature` (#225) is a string but an opaque defect FINGERPRINT — rendering it would
# put "sig-a1b2" where the owner needs "missing `## Loop metadata`". Both read their reason back from
# the latest journal record instead. The engine-generation re-announce puts one such record in the
# journal per generation, which covers every restart; a runner whose UPTIME exceeds the journal's own
# hot-retention window (journal.HOT_RETAIN_SECONDS) can still outlive its record, and that is what
# the per-family fallback prose below is for — the line then names the family without claiming a
# specific cause it can no longer read.
_HOLD_KINDS = (
    ("launch_hold_reason", "launch_hold_since", "launch_hold", "launch",
     "the launch gate refused this start", True),
    # Deliberately says WHICH SIDE is unknown: scheduler.launch_holds emits when the candidate is the
    # no-touches wildcard AND when the in-flight lane blocking it is, and with no record to read back
    # this line cannot tell them apart. Naming one would assert a mechanism that may be the wrong one.
    ("wildcard_hold_journaled", "wildcard_hold_since", "wildcard_hold", "wildcard",
     "a no-touches wildcard (this issue, or the lane blocking it) overlaps every lane under hard "
     "affinity — the queue serializes", False),
    ("queue_invalid_signature", "queue_invalid_since", "queue_invalid", "queue contract",
     "the issue fails the mechanical queue contract", False),
)
_HOLD_ACTS = frozenset(k[2] for k in _HOLD_KINDS)


# --------------------------- coercion helpers (fail closed) ---------------------------

def _records(journal_records):
    return [r for r in journal_records if isinstance(r, dict)] if isinstance(journal_records, list) else []


def _dict(v):
    return v if isinstance(v, dict) else {}


def _num(v):
    return v if type(v) is int else None


def _ts(rec):
    t = rec.get("ts")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _ok(rec):
    return rec.get("outcome") == "ok"


def _count(v):
    """A journaled flake/persistent field is a count. Tolerate a list (len) or a wrong-typed
    value (0) so a malformed nightly record can never crash the health line."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple, set)):
        return len(v)
    return 0


def _reference_now(view, records):
    """The clock the 'last 7 days' windows hang off. Prefer the runner-supplied `now`; else the
    latest journaled ts (≈ report time); else 0 (an empty journal has no window)."""
    n = view.get("now")
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        return float(n)
    stamps = [t for t in (_ts(r) for r in records) if t is not None]
    return float(max(stamps)) if stamps else 0.0


def _overnight_start(records, now):
    """The lower bound for the OVERNIGHT sections (merged/parked/bounces/wanders): the last
    morning report's ts, i.e. 'everything since the previous report'. The journal is append-only
    and never rotated, so without this bound these sections would accumulate every event ever and
    the daily report — and its 'nothing happened' honesty — would degrade permanently after the
    first merge. Falls back to a 24h lookback when no prior report is recorded (first-ever run)."""
    stamps = [t for t in (_ts(r) for r in records if r.get("act") == "morning_report")
              if t is not None and t < now]
    return max(stamps) if stamps else now - DAY_SECONDS


# --------------------------- links ---------------------------

def _repo(config):
    r = config.get("repo")
    return r if isinstance(r, str) and r.strip() else None


def _issue_link(repo, num):
    return f"https://github.com/{repo}/issues/{num}" if repo else f"#{num}"


def _pr_link(repo, pr):
    return f"https://github.com/{repo}/pull/{pr}" if repo else f"PR #{pr}"


# --------------------------- section builders ---------------------------

def _in_window(rec, window_start):
    """A ts-stamped record is in-window when ts >= window_start; a record without a ts (journal
    always stamps one, so this is only a corrupt line) is kept — honest over-reporting beats
    silently dropping it."""
    ts = _ts(rec)
    return ts is None or ts >= window_start


_MERGE_ACTS = ("merge", "absorb_merged")


def _reconciled_parks(records, window_start):
    """Issue numbers whose in-window park was SUPERSEDED by a later landing (merge/absorb_merged) —
    i.e. the issue parked, was re-approved, and merged (#37). Such a park is no longer an OPEN ask:
    its final outcome is 'merged', so it must not be reported as needing William.

    Reconciliation is by final outcome, not mere co-occurrence — a park counts as resolved only when
    a landing came strictly at-or-after it (latest merge ts >= latest park ts). A merge that came
    BEFORE the park (an issue that landed, re-opened, then parked again) leaves the park as the
    latest word and a genuine open ask. A corrupt park with no comparable ts, paired with any
    in-window landing, is treated as resolved (a same-window landing is strong evidence the ask was
    answered); a landing with no comparable ts never resolves a real-ts park (honest over-report)."""
    park_ts, merge_ts = {}, {}

    def note(store, num, ts):
        # track the latest KNOWN ts per issue; a missing/corrupt ts records presence at -inf so it
        # never wins a max() against a real stamp.
        store[num] = max(store.get(num, float("-inf")), ts if ts is not None else float("-inf"))

    for r in records:
        if not (_ok(r) and _in_window(r, window_start)):
            continue
        num = _num(r.get("num"))
        if num is None:
            continue
        act = r.get("act")
        if act == "park":
            note(park_ts, num, _ts(r))
        elif act in _MERGE_ACTS:
            note(merge_ts, num, _ts(r))
    return {num for num, pts in park_ts.items() if num in merge_ts and merge_ts[num] >= pts}


def _owner_closed(records, window_start):
    """Issue numbers whose in-window park/bounce was STOOD DOWN by the owner CLOSING the issue on
    GitHub — an `absorb_close` (issue #108). The owner already answered by closing, so the hand-back
    is no longer an OPEN ask and must not be reported as one (it leaves Parked AND Bounces). NOT a
    landing: an absorbed close is a drop, never listed under Merged. Mirrors _reconciled_parks'
    at/after-ts discipline — a close resolves only a park/bounce that came at-or-before it — so an
    issue the owner closed, then re-opened and re-approved and that parked AGAIN this window still
    reads as a genuine open ask (its latest word is the new park, not the old close)."""
    ask_ts, close_ts = {}, {}

    def note(store, num, ts):
        store[num] = max(store.get(num, float("-inf")), ts if ts is not None else float("-inf"))

    for r in records:
        if not (_ok(r) and _in_window(r, window_start)):
            continue
        num = _num(r.get("num"))
        if num is None:
            continue
        act = r.get("act")
        if act in ("park", "bounce"):
            note(ask_ts, num, _ts(r))
        elif act == "absorb_close":
            note(close_ts, num, _ts(r))
    return {num for num, ats in ask_ts.items() if num in close_ts and close_ts[num] >= ats}


def _merged(records, repo, window_start, parked_earlier=frozenset()):
    """Clean merges AND absorbed out-of-band merges (a PR that landed on GitHub between merge and
    bookkeeping) — both are issues that landed. Windowed to the overnight bound, deduped by issue
    number, latest record wins. Issues in `parked_earlier` (parked then later merged this window)
    carry an inline note so the resolved park episode reads as history here, not a lost open ask."""
    seen = {}
    for r in records:
        if r.get("act") in _MERGE_ACTS and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None:
                seen[num] = (r.get("id"), _num(r.get("pr")))
    lines = []
    for num in sorted(seen):
        iid, pr = seen[num]
        tag = f" ({iid})" if iid else ""
        pr_bit = f" · PR {_pr_link(repo, pr)}" if pr is not None else ""
        note = " · parked earlier, later merged" if num in parked_earlier else ""
        lines.append(f"- #{num}{tag} — {_issue_link(repo, num)}{pr_bit}{note}")
    return lines


def _parked(records, window_start, resolved=frozenset()):
    """Open asks only. A parked issue whose final outcome was a later merge (`resolved`, from
    _reconciled_parks) is dropped here — it renders once under Merged, never as a second open ask."""
    seen = {}
    for r in records:
        if r.get("act") == "park" and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None and num not in resolved:
                seen[num] = (r.get("id"), bool(r.get("needs_william")), r.get("memo"))
    lines = []
    for num in sorted(seen):
        iid, needs, memo = seen[num]
        tag = f" ({iid})" if iid else ""
        who = "**needs-owner**" if needs else "parked"
        lines.append(f"- #{num}{tag} {who} — {memo if isinstance(memo, str) else '(no memo)'}")
    return lines


def _bounces(records, window_start, resolved=frozenset()):
    """Open push-backs only. A bounce the owner CLOSED on GitHub (`resolved`, from _owner_closed)
    is dropped — the owner already answered by closing, so it is history, not an open ask."""
    seen = {}
    for r in records:
        if r.get("act") == "bounce" and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None and num not in resolved:
                seen[num] = (r.get("id"), r.get("memo"))
    lines = []
    for num in sorted(seen):
        iid, memo = seen[num]
        tag = f" ({iid})" if iid else ""
        lines.append(f"- #{num}{tag} — {memo if isinstance(memo, str) else '(no memo)'}")
    return lines


def _regenerations(records, window_start):
    """Conflict regenerations within the last 7 days — the §4.2 tuning metric (climbing ⇒ tighten
    affinity / drop lanes; always zero ⇒ loosen)."""
    lines = []
    for r in sorted(records, key=lambda x: _ts(x) or 0):
        if r.get("act") == "regenerate" and _ok(r):
            ts = _ts(r)
            if ts is None or ts < window_start:
                continue
            num = _num(r.get("num"))
            branch = r.get("new_branch")
            conflicts = _count(r.get("conflicts"))
            tag = f" ({r.get('id')})" if r.get("id") else ""
            where = f" → rebuilt on `{branch}`" if isinstance(branch, str) and branch else ""
            lines.append(f"- #{num}{tag}{where} (conflict #{conflicts})")
    return lines


def _wanders(records, window_start):
    """PRs whose actual diff touched areas the issue didn't declare in `touches:` — deduped by
    issue number. gate-derived actions (merge/hold) carry the `wander` flag."""
    nums = {}
    for r in records:
        if r.get("wander"):
            ts = _ts(r)
            if ts is not None and ts < window_start:
                continue
            num = _num(r.get("num"))
            if num is not None:
                nums[num] = r.get("id")
    lines = []
    for num in sorted(nums):
        tag = f" ({nums[num]})" if nums[num] else ""
        lines.append(f"- #{num}{tag} — actual diff wandered beyond its declared `touches:`")
    return lines


def _questions(records, window_start):
    """Owner-decision questions asked overnight (#163) — the question-rate the DoD tracks from day
    one. Each successful `post_question` is one durable question a worker handed to the owner before
    exiting cleanly; a climbing rate means work is reaching the loop under-specified. Counted per
    issue (most recent id label wins). Returns (lines, total)."""
    counts = {}
    total = 0
    for r in sorted(records, key=lambda x: _ts(x) or 0):
        if r.get("act") == "post_question" and _ok(r):
            ts = _ts(r)
            if ts is not None and ts < window_start:
                continue
            num = _num(r.get("num"))
            if num is None:
                continue
            entry = counts.setdefault(num, [0, r.get("id")])
            entry[0] += 1
            if r.get("id"):
                entry[1] = r.get("id")
            total += 1
    lines = []
    for num in sorted(counts):
        n, iid = counts[num]
        tag = f" ({iid})" if iid else ""
        lines.append(f"- #{num}{tag} — asked {n} owner question(s)")
    return lines, total


def notify_canary(records, now=None, max_age_seconds=None):
    """What the LATEST notify-channel canary says, as a verdict dict (issue #164).

    The daily morning push doubles as the channel heartbeat: the runner journals its delivery result
    as `notify_canary`, and this reads the newest one back — so a SILENTLY dead channel (once dead
    for days, found only by a human reading the journal) is visible on the surfaces a dead channel
    could never itself reach. Returns
    ``{"status": "unverified"|"unconfigured"|"healthy"|"dead", "channel": str, "rc": int|None,
    "detail": str}``.

    Fail closed: a wrong-typed/absent record reads as `unverified`, never a false green; a
    `log-only` result is `unconfigured`, never 'healthy'; and `ok` must be EXACTLY True (a truthy
    string in a hand-edited journal must not read as delivered).

    Freshness (issue #200): the morning report calls this with a canary minutes old, so by default
    there is no age bound. A WEEKLY reader (`superlooper upkeep`) passes `now` and
    `max_age_seconds`: a delivered canary older than the window is downgraded to `unverified` —
    a channel nothing has exercised in a week is not "healthy", it is unproven. Only a DELIVERED
    canary is aged out; a `dead`/`unconfigured` one stays as-is (its warning does not go stale).

    Structured rather than pre-rendered because there are now two readers with different shapes —
    the morning report's markdown bullet (``_notify_channel`` below) and ``superlooper upkeep``'s
    one-line weekly census. One verdict, two renderings; the verdict lives here."""
    canaries = [r for r in _records(records) if r.get("act") == "notify_canary"]
    if not canaries:
        return {"status": "unverified", "channel": "?", "rc": None, "detail": ""}
    latest = max(canaries, key=lambda r: _ts(r) if _ts(r) is not None else float("-inf"))
    channel = latest.get("channel")
    channel = channel if isinstance(channel, str) and channel else "?"
    rc = latest.get("rc")
    rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
    detail = latest.get("detail")
    detail = detail.strip() if isinstance(detail, str) and detail.strip() else ""
    if channel == "log-only":
        return {"status": "unconfigured", "channel": channel, "rc": rc, "detail": detail}
    if latest.get("ok") is True:               # `is True`: a truthy string must never read as green
        # Age-out a stale delivery for a windowed (weekly) reader. A missing/corrupt ts fails
        # CLOSED — it cannot prove freshness, so it cannot read as healthy.
        if isinstance(now, (int, float)) and not isinstance(now, bool) \
                and isinstance(max_age_seconds, (int, float)) and not isinstance(max_age_seconds, bool):
            ts = _ts(latest)
            if ts is None or ts < now - max_age_seconds:
                why = ("last delivery was %dd ago — older than the report window"
                       % int((now - ts) // 86400)) if ts is not None else \
                      "last delivery has no readable timestamp — cannot confirm it is recent"
                return {"status": "unverified", "channel": channel, "rc": rc, "detail": why}
        return {"status": "healthy", "channel": channel, "rc": rc, "detail": detail}
    return {"status": "dead", "channel": channel, "rc": rc,
            "detail": detail or "(no error captured)"}


def _notify_channel(records):
    """The notify-channel canary line (issue #164) — the morning report's rendering of
    `notify_canary`'s verdict."""
    v = notify_canary(records)
    channel = v["channel"]
    if v["status"] == "unverified":
        return "- Notify channel: not verified this cycle (no canary recorded)."
    if v["status"] == "unconfigured":
        return ("- Notify channel: **no channel configured** — pushes go to the journal only; set "
                "`notify.imessage_to` or `notify.cmd` so alerts can reach your phone.")
    if v["status"] == "healthy":
        return f"- Notify channel: healthy (last push delivered via {channel})."
    # dead -> the last push did NOT deliver. Say so loudly, naming the channel + reason: this line
    # is the whole point — the owner reads it here even when the channel can't reach them.
    rc_s = f", rc={v['rc']}" if v["rc"] is not None else ""
    return (f"- Notify channel: **DEAD** — the last push did not deliver via {channel}{rc_s}: "
            f"{v['detail']}. Pushes are **not reaching you**; fix the channel and re-run "
            "`superlooper doctor --stack`.")


def _gate_health(records, window_start, ledger, config):
    nightlies = [r for r in records if r.get("act") == "nightly"
                 and (_ts(r) is None or _ts(r) >= window_start)]
    quarantine = _dict(config).get("qa", {})
    quarantine = quarantine.get("quarantine") if isinstance(quarantine, dict) else None
    q_size = len(quarantine) if isinstance(quarantine, list) else 0
    accepted = len(ledger) if isinstance(ledger, dict) else 0

    lines = []
    if nightlies:
        latest = max(nightlies, key=lambda x: _ts(x) or 0)
        total = len(nightlies)
        # `is True` throughout: a corrupt journal line ("green": "false", a truthy string) must
        # never read as green, and a wrong-typed parse_error must not be trusted (Codex R2 M1).
        green = sum(1 for r in nightlies if r.get("green") is True and r.get("parse_error") is not True)
        date = latest.get("date") or "?"
        if latest.get("parse_error") is True:
            # a broken results file is NEVER a silent green — it is an honest failure line here
            # (+ the push the nightly sent); merges were NOT auto-verified this run.
            lines.append(f"- Nightly ({date}): could not parse results — dev not auto-verified; "
                         "see the nightly log.")
        elif latest.get("green") is True:
            lines.append(f"- Nightly ({date}): green.")
        elif latest.get("green") is False:
            persistent = _count(latest.get("persistent"))
            filed = latest.get("filed")
            fk = len(filed) if isinstance(filed, list) else _count(filed)
            lines.append(f"- Nightly ({date}): {persistent} persistent failure(s), "
                         f"filed {fk} fix issue(s).")
        else:
            # green is neither True nor False (missing/wrong-typed) and it's not a clean parse
            # error: a corrupt record — say so, never imply green.
            lines.append(f"- Nightly ({date}): result unclear (corrupt record) — dev not "
                         "auto-verified; see the nightly log.")
        lines.append(f"- {green}/{total} green over the last 7 nights; "
                     f"flakes last run: {_count(latest.get('flakes'))}.")
    else:
        lines.append("- Nightly: no runs recorded in the last 7 days.")
    lines.append(f"- Quarantine: {q_size} test(s). Accepted known failures: {accepted}.")
    lines.append(_notify_channel(records))     # issue #164: the channel canary rides the health block
    return lines


def _watchdog(records, window_start):
    """Unattended-debugger activity (issue #66): every watchdog LAUNCH — verified or failed —
    must reach the owner's morning surface. Notified/stood-down episodes stay in the journal
    only: nothing ultimately happened, and the summary's quiet claim must stay honest."""
    lines = []
    for r in records:
        if r.get("act") != "watchdog" or not _in_window(r, window_start):
            continue
        sigs = ", ".join(s for s in (r.get("signals") or []) if isinstance(s, str)) \
            or "(signal unrecorded)"
        if r.get("outcome") == "launched":
            lines.append(f"- Launched unattended sl-debugger session {r.get('id')} — signals: "
                         f"{sigs}; authority: {r.get('authority')}. Its memo is in this "
                         "reports/ folder.")
        elif r.get("outcome") == "launch_failed":
            lines.append(f"- Launch of unattended sl-debugger session {r.get('id')} FAILED "
                         f"(rc={r.get('rc')}) — signals: {sigs}. The loop needed attention "
                         "overnight and the fallback could not start.")
    return lines


def _resurrection(records, window_start):
    """Runner resurrection activity (issue #208): every automatic RESTART of a provably-gone runner —
    succeeded, failed, or cap-paused — reaches the owner's morning surface. The runner going down and
    restarting itself overnight is exactly what the owner must never coffee past."""
    lines = []
    for r in records:
        if r.get("act") != "runner_resurrect" or not _in_window(r, window_start):
            continue
        sigs = ", ".join(s for s in (r.get("signals") or []) if isinstance(s, str)) \
            or "(signal unrecorded)"
        outcome = r.get("outcome")
        if outcome == "resurrected":
            # What was actually VERIFIED is the pidfile flipping to a live pid; the reconcile is a
            # property of `superlooper run` itself, not something this path observed. State the
            # mechanism, don't assert the outcome as witnessed history (fresh-review P2-4).
            lines.append(f"- Runner was down and AUTOMATICALLY RESTARTED ({r.get('id')}) — signals: "
                         f"{sigs}. It came up as a normal `superlooper run` (verified live via its "
                         "pidfile), so it rebuilds from GitHub + disk like a manual restart.")
        elif outcome == "resurrect_failed":
            lines.append(f"- Automatic runner restart {r.get('id')} FAILED (rc={r.get('rc')}) — the "
                         "loop was down and could not restart itself (its cmux tab is likely gone).")
        elif outcome == "resurrect_capped":
            if r.get("max_per_hour") == 0:
                lines.append("- Runner is DOWN and auto-restart is DISABLED "
                             "(watchdog.resurrection_max_per_hour = 0) — it will stay down until you "
                             "restart it.")
            else:
                # "attempted", never "was restarted": an undeliverable attempt (no_pane) counts
                # toward the cap without ever restarting anything, so asserting a restart here would
                # fabricate history the journal does not support (fresh-review P1-2).
                lines.append(f"- Runner auto-restart PAUSED — restart was attempted "
                             f"{r.get('attempts')} time(s) in an hour and the runner is still going "
                             "down. A repeatedly-dying runner is a real incident, not a flap; the "
                             "loop needs you.")
    return lines


# --------------------------- the triage flight (issue #449) ---------------------------
# The queue-hygiene flight the owner delegated by standing rule closes and merges issues on its own
# word. That authority is only safe if its exercise is VISIBLE, once, on the surface the owner
# already reads — so every act it takes, and every act its own edges refused, lands here.
#
# Derived from the journal like every other section, and from nothing else. The flight writes a
# markdown run log in its state home too, but that log is prose for the NEXT flight; the journal is
# what the report and the dashboard read, so they can never drift from each other.

# The acts, in the order they render, with the verb the line leads with. `triage_keep` is
# deliberately absent: silence means kept — an issue a flight looked at and left alone is not news,
# and listing every one would bury the four that are.
_TRIAGE_ACTS = ("triage_merge", "triage_close", "triage_fix", "triage_escalate", "triage_refused")

# A refusal's reason is written for the FLIGHT, at the moment of its mistake, and it can legitimately
# name a whole label vocabulary. The owner's morning surface gets the gist; the journal and the run
# log keep the whole thing. Bounded here rather than at the source for exactly that reason — the
# audience differs, so the length should.
_TRIAGE_REASON_MAX = 180
# And how many refusals may render before the rest become a counted line. A flight whose every act
# is refused is a flight with a broken brief, not a report to scroll — but the overflow is STATED,
# never silently dropped.
_TRIAGE_REFUSALS_MAX = 5


def _triage_records(records, window_start):
    return [r for r in records
            if isinstance(r.get("act"), str) and r["act"].startswith("triage_")
            and _in_window(r, window_start)]


def _triage_line(rec, repo):
    """One act as one line, leading with what happened and ending with the issue's link."""
    act = rec.get("act")
    num = _num(rec.get("num"))
    link = _issue_link(repo, num) if num is not None else None
    where = " — %s" % link if link else ""
    if act == "triage_merge":
        other = _num(rec.get("absorber"))
        return "- Merged #%s into #%s (its content was absorbed there)%s" % (
            num, other if other is not None else "?", where)
    if act == "triage_close":
        # A nit is told from an overtaken close by its VERDICT, not by the presence of a ledger
        # number: a nit whose ledger id went unrecorded is still a nit, and rendering it as
        # "overtaken by (commit unrecorded)" would state a reason the flight never gave.
        verdict = rec.get("verdict")
        ledger = _num(rec.get("ledger"))
        if isinstance(verdict, str) and verdict.startswith("nit(") or ledger is not None:
            return "- Closed #%s as a nit under rubric line %s — filed in the ledger%s%s" % (
                num, rec.get("rubric") or "?",
                " #%s" % ledger if ledger is not None else "", where)
        commit = rec.get("commit")
        return "- Closed #%s as overtaken by `%s`%s" % (
            num, commit if isinstance(commit, str) and commit.strip() else "(commit unrecorded)",
            where)
    if act == "triage_fix":
        fixed = rec.get("fixed")
        what = ", ".join(x for x in fixed if isinstance(x, str)) if isinstance(fixed, list) else ""
        return "- Fixed #%s (%s)%s" % (num, what or "metadata", where)
    if act == "triage_escalate":
        head = "- **#%s escalated**%s" % (num, " (approved — flagged only)" if rec.get("held")
                                         else "")
        finding = rec.get("finding") or rec.get("detail")
        rec_line = rec.get("recommend")
        parts = [head]
        if isinstance(finding, str) and finding.strip():
            parts.append(" — %s" % finding.strip().replace("\n", " "))
        if link:
            parts.append(" — %s" % link)
        out = "".join(parts)
        if isinstance(rec_line, str) and rec_line.strip():
            out += "\n  - **Recommend:** %s" % rec_line.strip().replace("\n", " ")
        return out
    # triage_refused — the delegation's own edges doing their job, which the owner sees once.
    detail = rec.get("detail")
    reason = (detail if isinstance(detail, str) and detail.strip()
              else "(reason unrecorded)").replace("\n", " ")
    if len(reason) > _TRIAGE_REASON_MAX:
        reason = reason[:_TRIAGE_REASON_MAX].rstrip() + "… (full reason in the run log)"
    return "- REFUSED on #%s: %s%s" % (num, reason, where)


def _triage_summary(records, window_start):
    """The clause the summary tally gains on a night a flight flew — short, because the summary
    line is the push body. Built from the run's own `triage_finish` counts when it wrote them, and
    counted from the acts themselves when it did not: a flight that died before finishing still
    closed whatever it closed, and the owner must still be told."""
    mine = _triage_records(records, window_start)
    counts = None
    for rec in mine:
        if rec.get("act") == "triage_finish" and isinstance(rec.get("counts"), dict):
            counts = rec["counts"]
    if counts is None:
        counts = {"merged": sum(1 for r in mine if r.get("act") == "triage_merge"),
                  "closed": sum(1 for r in mine if r.get("act") == "triage_close"),
                  "escalated": sum(1 for r in mine if r.get("act") == "triage_escalate")}
    return " Triage: %d merged · %d closed · %d escalated." % (
        _count(counts.get("merged")), _count(counts.get("closed")),
        _count(counts.get("escalated")))


def _triage(records, repo, window_start):
    """The flight's overnight lines, or [] on a day with no run.

    Empty is the whole point on a quiet day: the caller renders NO SECTION at all rather than a
    "None." under a heading. A delegation that did not fly is not a standing item on the owner's
    morning, and a heading that appears every day stops being read on the day it matters."""
    mine = _triage_records(records, window_start)
    if not mine:
        return []
    # The run's own tally leads, then any launch that FAILED (a night with no triage at all is the
    # thing to notice first), then the acts in the order they happened.
    head, acts, refused = [], [], 0
    for rec in mine:
        act = rec.get("act")
        if act == "triage_finish":
            detail = rec.get("detail")
            counts = rec.get("counts")
            if isinstance(detail, str) and detail.strip():
                head.append("- The flight: %s." % detail.strip())
            elif isinstance(counts, dict):
                head.append("- The flight: judged %s." % _count(counts.get("judged")))
        elif act == "triage_launch":
            outcome = rec.get("outcome")
            if isinstance(outcome, str) and outcome != "launched":
                head.append("- Flight %s FAILED to launch — %s. Nothing was triaged today."
                            % (rec.get("id") or "t?", outcome))
        elif act in _TRIAGE_ACTS:
            if act == "triage_refused":
                refused += 1
                if refused > _TRIAGE_REFUSALS_MAX:
                    continue
            acts.append(_triage_line(rec, repo))
    if refused > _TRIAGE_REFUSALS_MAX:
        # NO SILENT CAP: say how many were left out, and where the rest of them are.
        acts.append("- …and %d more refusal(s) this run — the full list is in the run log."
                    % (refused - _TRIAGE_REFUSALS_MAX))
    return head + acts


# --------------------------- standing holds + ages (issue #405) ---------------------------

def _age(seconds):
    """A span as the coarse, readable duration the report speaks in: "3d 4h", "5h 12m", "40m".

    Returns None for anything that cannot be trusted as a span — a wrong-typed value, a NEGATIVE one
    (a hold stamped in the future by a clock jump), or a NON-FINITE one. An age the loop cannot prove
    renders as no age at all; it is never invented, and it never feeds the alert threshold.

    The non-finite guard is load-bearing, not defensive decoration: Python's json module round-trips
    the bare literals `NaN` and `Infinity` without complaint, so a corrupt or hand-edited issues.json
    can hand this module a stamp that `int()` refuses (ValueError / OverflowError). This module's
    whole contract is that a broken overnight never takes the report down — a raise here would blank
    the one surface the owner reads, which is precisely the silence #405 exists to end."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
            or not math.isfinite(seconds) or seconds < 0:
        return None
    s = int(seconds)
    d, rem = divmod(s, DAY_SECONDS)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _since(v):
    """An epoch stamp, or None if it is missing, wrong-typed (bool is an int — exclude it) or
    non-finite (json round-trips `NaN`/`Infinity`). Dropped at the door so no downstream arithmetic —
    the age render OR the alert threshold comparison — ever sees one."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
        else None


def _iid_num(iid):
    """``i405`` -> ``405``; anything else -> None (sorts last, still rendered by its id)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _hold_reasons(records):
    """The LATEST journaled reason per (issue id, hold act) — the prose for the two hold families
    whose durable stamp carries none. A record with no usable ts still counts (it sorts below every
    real stamp), so a corrupt line degrades to "an older reason", never to no reason at all."""
    latest = {}
    for r in records:
        act, iid = r.get("act"), r.get("id")
        # `isinstance` first: a wrong-typed `act` could be an unhashable list, and `x in frozenset`
        # raises on one — a corrupt journal line must be skipped, never fatal.
        if not isinstance(act, str) or act not in _HOLD_ACTS or not isinstance(iid, str):
            continue
        ts = _ts(r)
        ts = ts if ts is not None else float("-inf")
        key = (iid, act)
        if key not in latest or ts >= latest[key][0]:
            latest[key] = (ts, r.get("reason"))
    return {k: v[1] for k, v in latest.items()}


def standing_holds(issues_state, journal_records=None):
    """Every issue the loop is currently HOLDING, as records — the engine-side truth behind the
    report's Standing holds section (issue #405), and the shape a later surface can render.

    ``[{"id", "num", "kind", "tag", "reason", "since", "relaunch"}]``, sorted by issue number.
    PURE: the runner's loopstate dict in (``view['issues_state']``), a list out. Every field is
    coerced; a wrong-typed state, entry or stamp yields fewer holds, never an exception.

    What counts as held is the DURABLE STAMP, not a re-derivation: decide re-derives hold conditions
    every tick but only ever writes them down on change, and the stamps are cleared at every point an
    issue stops being held. A terminal issue (parked/merged/bounced/needs_william) is excluded — its
    episode ended, so a leftover stamp there is history.

    Which means the LISTING is exact and the REASON can lag. A standing stamp always means the issue
    has not launched, recovered, parked or been re-approved since the loop refused it — that is what
    is being reported, and it is what an age is worth alerting on. But nothing clears a stamp when
    only its CAUSE resolves (an issue whose queue-contract defect was fixed, then sat lane-bound,
    still wears the old complaint until it launches); there is deliberately no clear-the-stamp verb
    in the engine, and inventing one is a policy change this issue's boundaries exclude. The one
    exception the engine DOES write is #172's lane-bound retirement, whose prose says the issue is
    not held at all — skipped here, or it would print a self-refuting alert (see _LANE_BOUND_PREFIX).

    ``relaunch`` is the fact that makes an exited lane legible: True means the worker DIED and its
    relaunch is what is held, so a surface reading a lane whose status still says `running` can name
    the real cause instead of painting a live flight."""
    issues = _dict(_dict(issues_state).get("issues"))
    reasons = _hold_reasons(_records(journal_records))
    out = []
    for iid, ist in issues.items():
        if not isinstance(iid, str) or not isinstance(ist, dict):
            continue
        # `isinstance` before the membership test: a wrong-typed status could be an unhashable list,
        # and `x in frozenset` RAISES on one — the coercion contract has to hold at the lookup too.
        status = ist.get("status")
        if isinstance(status, str) and status in _TERMINAL_STATUSES:
            continue
        for stamp, since_key, act, tag, fallback, stamp_is_reason in _HOLD_KINDS:
            v = ist.get(stamp)
            if not v:
                continue
            # `launch_hold_reason` IS the prose; the other two stamps carry none, so their reason
            # comes from the journal. A wrong-typed stamp falls through to the same lookup rather
            # than being rendered raw, and an unrecoverable reason falls back to the family's own
            # prose — the line always says SOMETHING about why, never a bare "held".
            reason = v if stamp_is_reason and isinstance(v, str) and v.strip() \
                else reasons.get((iid, act))
            # The all-clear check sits HERE, on the RESOLVED reason, not on the raw stamp: a
            # wrong-typed `launch_hold_reason` (a truthy list) falls through to the journal
            # fallback, which would re-supply the very retirement prose the skip exists to catch —
            # and the line would then read "has been held 3d — this issue is no longer held".
            if isinstance(reason, str) and reason.startswith(_LANE_BOUND_PREFIX):
                continue                       # an all-clear, not a hold (see the constant)
            out.append({"id": iid, "num": _iid_num(iid), "kind": act, "tag": tag,
                        "reason": reason.strip() if isinstance(reason, str) and reason.strip()
                        else fallback,
                        "since": _since(ist.get(since_key)),
                        "relaunch": act == "launch_hold" and bool(ist.get("relaunch_held"))})
    out.sort(key=lambda h: (h["num"] if h["num"] is not None else 1 << 30, h["kind"]))
    return out


def _hold_age(hold, now):
    return None if hold.get("since") is None else now - hold["since"]


def _hold_head(hold):
    num = hold.get("num")
    return f"#{num} ({hold['id']})" if num is not None else hold["id"]


def _standing_holds(holds, now):
    """One line per standing hold: who, for how long, and why."""
    lines = []
    for h in holds:
        age = _age(_hold_age(h, now))
        when = f"held {age}" if age else "held — start time not recorded"
        # The tag is a short noun, and the reason follows a colon: the reasons themselves carry em
        # dashes, and three dashes in one line is unreadable exactly where the owner is reading for
        # the cause.
        tag = "relaunch (the worker EXITED)" if h.get("relaunch") else h["tag"]
        lines.append(f"- {_hold_head(h)} — **{when}** · {tag}: {h['reason']}")
    return lines


def _hold_alerts(holds, view, now):
    """The ALERT-tier lines a listing is not enough for (issue #405): a hold, or the freeze, that
    has stood past its threshold. Rendered above the sections, where the owner cannot coffee past
    them. No new notification is earned — these ride the one daily push the report already sends,
    via the summary line's own count."""
    # The threshold is tested against the age _age() would RENDER, never a raw span: an age this
    # module cannot render honestly (wrong-typed, negative, non-finite) is one it cannot alert on
    # either, and the two must agree or an alert could print "held None".
    lines = []
    for h in holds:
        age, rendered = _hold_age(h, now), _age(_hold_age(h, now))
        if rendered is None or age < HOLD_ALERT_SECONDS:
            continue
        lines.append(f"**{_hold_head(h)} has been held {rendered}** — {h['reason']} (a hold that "
                     "old is a STALL, not a wait: nothing in the loop moves it until the cause "
                     "clears).")
    frozen = view.get("frozen")
    if isinstance(frozen, dict) and frozen:
        since = _since(frozen.get("since"))
        age = None if since is None else now - since
        rendered = _age(age)
        if rendered is not None and age >= FREEZE_ALERT_SECONDS:
            lines.append(f"**Merges have been FROZEN for {rendered}** — "
                         f"{frozen.get('reason') or '(reason unrecorded)'}. A freeze this old has "
                         "outlived both of its own recovery paths (a green dev check, a green "
                         "nightly); it is a stall, not the safe idle state.")
    return lines


def _queue_hold(view, now):
    """The ALERT-tier line for a PAUSED queue (issue #320), or None.

    A systemic hold is the quietest state the loop has: one alert went out when it tripped, and then
    nothing — no park, no notify, no label anywhere changes. On the morning after, a full queue with
    nothing moving renders exactly like a queue with nothing to do, and "Nothing happened overnight"
    is a true sentence about the worst possible night. So this line sits with the aged-hold alerts,
    above every section, and breaks `quiet` unconditionally.

    Unlike a standing hold it is NOT age-gated: a per-issue hold is a wait by design and hours of it
    are normal, whereas a held queue means the machine is broken and no tick will fix it. One minute
    of that is already the thing the owner must see.

    The caller pre-computes `view['queue_hold']` (actions.queue_hold_reasons over state/ALERT) — the
    same discipline `engine_drift` follows, so this module never imports the runner's brain to read
    one frozenset."""
    hold = view.get("queue_hold")
    if not isinstance(hold, dict):
        return None
    reasons = [r for r in hold.get("reasons") if isinstance(r, str) and r.strip()] \
        if isinstance(hold.get("reasons"), list) else []
    if not reasons:
        return None
    since = _since(hold.get("since"))
    rendered = _age(None if since is None else now - since)
    for_bit = f" for {rendered}" if rendered else ""
    if reasons == [_ALERT_UNREADABLE]:
        # The fail-closed half: the marker exists and nothing could be read out of it, so the report
        # may not claim a cause OR claim the queue is fine. Say exactly what is known.
        return (f"**The loop's ALERT marker is present but UNREADABLE{for_bit}** — its hold state "
                "cannot be read, so the launch queue may be paused with nothing running. Read "
                "`state/ALERT` and run `superlooper status` / `superlooper doctor` before assuming "
                "the night was merely quiet.")
    # The claim is scoped to THE HOLD, not to the world: the hold takes no action, which is true of
    # every held class. "Nothing is parked" flatly is not — a lane that reached its own launch cap
    # before a second distinct refusal proved the outage parked on its own account, and telling the
    # owner otherwise walks them past a real re-approval (#320, fresh review).
    return (f"**The launch queue is HELD{for_bit} — nothing is launching** ({', '.join(reasons)}). "
            "This is a PAUSE, not an idle queue: the hold itself parks nothing and moves no label, "
            "so every approved issue keeps its place and launching resumes by itself once the "
            "cause clears. `superlooper status` prints the same line with the remedy.")


def _freeze(view, now):
    frozen = view.get("frozen")
    if isinstance(frozen, dict) and frozen:
        reason = frozen.get("reason") or "(reason unrecorded)"
        since = _since(frozen.get("since"))
        age = None if since is None else _age(now - since)
        for_bit = f" (frozen {age})" if age else ""
        return [f"Merges are **FROZEN** — {reason}{for_bit}. Building continues; this is the safe "
                "idle state."]
    return ["Merges flowing."]


def _usage_queue(view):
    lines = []
    usage = view.get("usage")
    if isinstance(usage, dict) and usage:
        pct = usage.get("pct")
        lines.append(f"- Usage: {pct}% of the window used." if isinstance(pct, (int, float))
                     and not isinstance(pct, bool) else "- Usage: captured.")
    else:
        lines.append("- Usage: (not captured this cycle).")
    queue = view.get("queue")
    queue = [q for q in queue if isinstance(q, dict)] if isinstance(queue, list) else []
    if queue:
        nxt = queue[0]
        nxt_num = _num(nxt.get("num"))
        nxt_title = nxt.get("title") if isinstance(nxt.get("title"), str) else ""
        head = f"#{nxt_num} {nxt_title}".strip() if nxt_num is not None else nxt_title or "(next)"
        lines.append(f"- Queue depth: {len(queue)} waiting; next up: {head}.")
    else:
        lines.append("- Queue empty.")
    return lines


def _engine_drift(view):
    """A one-line installed-engine publish-drift nudge (issue #39), or None. The runner/CLI
    pre-computes the drift (git lives in the impure assembler; this module stays pure) and hands it
    in via view['engine_drift'] — a stack_doctor.engine_drift() dict. Rendered ONLY when the
    installed engine is BEHIND; every other state (in sync, skipped, an unmeasurable anomaly) stays
    silent here — the doctor is where those surface. Reinforces, never undercuts, the publish gate:
    the fix is a manual republish, and the line says so."""
    d = view.get("engine_drift")
    if not isinstance(d, dict) or d.get("status") != "behind":
        return None
    n = d.get("behind")
    if not (isinstance(n, int) and not isinstance(n, bool) and n > 0):
        return None
    ref = d.get("ref")
    ref = ref if isinstance(ref, str) and ref.strip() else "main"
    unit = "commit" if n == 1 else "commits"
    return (f"**Installed engine {n} {unit} behind {ref}** — merged engine fixes are live only "
            "after you republish through the gated `bin/install.sh` (publishing stays manual; "
            "republish when convenient).")


def _section(title, lines, empty="None."):
    body = "\n".join(lines) if lines else empty
    return f"## {title}\n{body}\n"


# --------------------------- the report ---------------------------

def morning(journal_records, gh_view, ledger, config):
    """Render the morning report markdown. Args:
      journal_records  list of journal.read() dicts (the overnight action log).
      gh_view          the live snapshot the runner/CLI assembles for the report:
                         {"date": "YYYY-MM-DD", "now": epoch (window reference; default: latest
                          journaled ts), "frozen": merges_frozen.json dict|None,
                          "queue": [{"num","title"}, …] ready issues, "usage": usage dict|None,
                          "issues_state": the runner's loopstate dict (issues.json) — the durable
                          hold stamps the Standing holds section is derived from (#405); absent or
                          wrong-typed simply yields no holds,
                          "queue_hold": {"reasons": [...], "since": epoch}|None — the PAUSED-queue
                          state (#320), pre-computed by the caller from state/ALERT via
                          actions.queue_hold_reasons; absent/empty renders no hold}
      ledger           the known-failure ledger dict {fingerprint: {...}} (accepted-failure count).
      config           the per-repo config (repo for links, qa.quarantine size).
    Never raises; every arg is coerced to a safe empty shape."""
    records = _records(journal_records)
    view = _dict(gh_view)
    cfg = _dict(config)
    repo = _repo(cfg)
    now = _reference_now(view, records)
    week_start = now - WEEK_SECONDS         # the 7-day trend window (regenerations, gate health)
    overnight_start = _overnight_start(records, now)   # since the last report (overnight sections)
    date = view.get("date")
    date = date if isinstance(date, str) and date.strip() else "(date unknown)"

    # Reconcile park records against final outcomes (#37): a park that later merged this window is
    # resolved, so it leaves the open-ask Parked section and is annotated on its Merged line. A
    # park/bounce the owner CLOSED on GitHub (#108, an absorb_close) is likewise no longer an open
    # ask — it drops from Parked/Bounces but is NOT a landing, so it never renders under Merged.
    resolved_parks = _reconciled_parks(records, overnight_start)
    owner_closed = _owner_closed(records, overnight_start)
    merged = _merged(records, repo, overnight_start, resolved_parks)
    parked = _parked(records, overnight_start, resolved_parks | owner_closed)
    bounces = _bounces(records, overnight_start, owner_closed)
    regens = _regenerations(records, week_start)
    wanders = _wanders(records, overnight_start)
    watchdog = _watchdog(records, overnight_start)
    resurrections = _resurrection(records, overnight_start)     # runner auto-restarts (#208)
    questions, q_total = _questions(records, overnight_start)   # owner-question rate (#163)
    triage_lines = _triage(records, repo, overnight_start)      # the triage flight (#449)
    holds = standing_holds(view.get("issues_state"), records)   # standing holds + ages (#405)
    hold_alerts = _hold_alerts(holds, view, now)
    queue_hold = _queue_hold(view, now)                         # the PAUSED queue (#320)
    frozen = isinstance(view.get("frozen"), dict) and bool(view.get("frozen"))
    queue = [q for q in view.get("queue") if isinstance(q, dict)] if isinstance(view.get("queue"), list) else []

    # A routine (green) nightly is the system working, not activity that needs William — and one
    # runs EVERY night, so counting it here would mean no night is ever quiet. A RED nightly shows
    # up as `frozen` instead, which does break quiet. An unattended debugger LAUNCH (or a launch
    # that failed) always breaks quiet — the owner must never coffee past one (issue #66).
    # A hold or freeze that has stood past its threshold (#405) breaks quiet too: an issue nothing
    # will move for a day is not "nothing happened overnight" — it is the one night that reads
    # quiet precisely BECAUSE nothing moved. A hold under the threshold does NOT break quiet: it is
    # a standing condition working as designed, and the section below lists it either way.
    # A HELD QUEUE breaks quiet unconditionally (#320) and does not wait on an age threshold like a
    # per-lane hold: a paused loop is precisely the night that reads "nothing happened" BECAUSE
    # nothing could happen, and the queue may well be empty on top of it (the outage started before
    # anything was approved), so no other term here would catch it.
    # A triage flight breaks quiet unconditionally: it is an autonomous session that CLOSED and
    # MERGED the owner's issues on a standing delegation, and "nothing happened overnight" over a
    # night when six issues were shut is the one sentence that would retire the delegation.
    quiet = not any((merged, parked, bounces, regens, wanders, watchdog, resurrections,
                     questions, queue, frozen, hold_alerts, queue_hold, triage_lines))
    summary = ("Nothing happened overnight — queue empty." if quiet else
               f"{len(merged)} merged · {len(parked)} parked/needs-owner · "
               f"{len(bounces)} bounce(s) · {len(regens)} regen(s) · "
               f"{q_total} question(s) · queue: {len(queue)}.")
    if triage_lines:
        # The summary line IS the push body, and it is the ONE place an autonomous delegation could
        # become invisible: a night on which a flight closed three issues would otherwise reach the
        # owner's phone as "0 merged · 0 parked · queue: 1". Appended as its own clause rather than
        # folded into the tally above, because those terms are the LOOP's — a merge there is a PR
        # landing on the mainline, and a flight never merges anything of the kind.
        summary += _triage_summary(records, overnight_start)
    if queue_hold:
        # The summary line IS the push body, so this rides the report's one notification. It leads
        # the other alert-tier suffixes because it is the only one that says the whole loop stopped:
        # a tally reading "0 merged · queue: 6" is otherwise a normal-looking slow night.
        summary += " **THE LAUNCH QUEUE IS HELD — nothing is launching; see below.**"
    if hold_alerts:
        # The summary line IS the push body, so the count rides the one notification the report
        # already sends — an aged hold reaches the phone without earning a push of its own. It names
        # no threshold: the count can mix hold and freeze alerts, which are governed by two constants
        # that only happen to be equal today, and a single hardcoded number would become a lie the
        # day they diverge. The alert lines below each state their own age.
        summary += f" **{len(hold_alerts)} standing hold/freeze past its age threshold — see below.**"

    parts = [
        f"# superlooper morning report — {date}\n",
        f"{summary}\n",
    ]
    # Alert-tier lines sit directly under the summary tally — above every section, and after the
    # first non-title line so they never hijack the push body (the drift nudge's own discipline).
    # The queue hold leads them: an aged lane hold is one issue, a held queue is every issue.
    parts += [f"{line}\n" for line in ([queue_hold] if queue_hold else []) + hold_alerts]
    # The publish-drift nudge sits AFTER the summary tally so it never hijacks the push body (the
    # first non-title, non-blank line). Drift is a standing condition, not overnight activity, so it
    # is rendered independently of `quiet` — a quiet night with drift still reads "nothing happened".
    drift = _engine_drift(view)
    if drift:
        parts.append(f"{drift}\n")
    parts += [
        _section("Merged", merged, "Nothing merged."),
        _section("Parked / needs-owner", parked),
        _section("Bounces", bounces),
        _section("Owner questions", questions, "None — no worker needed an owner decision."),
        _section("Conflict regenerations (last 7 days)", regens),
        _section("Wanders", wanders),
        _section("Unattended debugger", watchdog, "None — the watchdog launched nothing."),
        _section("Runner resurrection", resurrections, "None — the runner did not go down."),
        # SILENT on a day with no flight (the list is empty, so no heading at all) — unlike every
        # section above, which renders its own "None." line. A delegation that did not fly is not a
        # standing item on the owner's morning.
        *([_section("Triage", triage_lines)] if triage_lines else []),
        _section("Gate health", _gate_health(records, week_start, ledger, cfg)),
        _section("Standing holds", _standing_holds(holds, now), "None — nothing is held."),
        "## Freeze state\n" + "\n".join(_freeze(view, now)) + "\n",
        _section("Usage / queue", _usage_queue(view)),
    ]
    return "\n".join(parts)


# --------------------------- promotion evidence (Gate 2) ---------------------------

def _fp(failure):
    """A failure's content fingerprint (the gate/ledger scheme) — imported lazily to keep this
    module free of the gate import unless a promotion report actually needs it."""
    import gate
    f = failure if isinstance(failure, dict) else {}
    return gate.fix_issue_fingerprint(f.get("test_id"), f.get("text"))


def _suite_section(suite, ledger):
    suite = _dict(suite)
    ledger = ledger if isinstance(ledger, dict) else {}
    if not suite.get("ok"):
        # never a silent "all clear": if we could not parse the suite, say so plainly.
        return ["Could not parse the suite results — this report cannot show failures; "
                "re-run the suite before deciding."]
    failures = [f for f in suite.get("failures") if isinstance(f, dict)] \
        if isinstance(suite.get("failures"), list) else []
    new, accepted = [], 0
    for f in failures:
        fp = _fp(f)
        if fp in ledger:
            accepted += 1                       # already accepted -> folded away (one approval, ever)
        else:
            new.append((f, fp))
    lines = []
    if new:
        lines.append(f"**{len(new)} NEW failure(s)** — not in the known-failure ledger:")
        for f, fp in new:
            tid = f.get("test_id") or "(unknown test)"
            first = (f.get("text") or "").strip().splitlines()
            detail = f" — {first[0]}" if first else ""
            # the fingerprint rides on the line so William can copy it straight into accept-failure
            lines.append(f"- {tid}{detail}  (fingerprint: `{fp}`)")
    else:
        lines.append("No new failures (nothing outside the known-failure ledger).")
    lines.append(f"\n{accepted} known failure(s) folded away (accepted in the ledger — "
                 "each approved once, by content).")
    return lines


def _merges_section(compare):
    c = _dict(compare)
    prod = c.get("prod_branch")
    dev = c.get("dev_branch") if isinstance(c.get("dev_branch"), str) else "dev"
    if not (isinstance(prod, str) and prod.strip()):
        return ["No prod branch configured — this repo promotes by its own checklist "
                "(`prod_branch` is null in .superlooper/config.json). Nothing to diff here."]
    result = c.get("result")
    if not isinstance(result, dict) or not result:
        return [f"Could not read the `{prod}...{dev}` comparison (GitHub unreadable) — "
                "check by hand before promoting."]
    ahead = result.get("ahead_by")
    total = result.get("total_commits")
    n = ahead if isinstance(ahead, int) and not isinstance(ahead, bool) else total
    n = n if isinstance(n, int) and not isinstance(n, bool) else "?"
    return [f"`{dev}` is **{n} commit(s)** ahead of `{prod}` since the last promotion."]


def _open_issues_section(open_issues, repo):
    items = [i for i in open_issues if isinstance(i, dict)] if isinstance(open_issues, list) else []
    if not items:
        return ["No open issues."]
    lines = []
    for i in sorted(items, key=lambda x: x.get("num") if isinstance(x.get("num"), int) else 1 << 30):
        num = _num(i.get("num"))
        title = i.get("title") if isinstance(i.get("title"), str) else ""
        labels = i.get("labels")
        lbl = f"  [{', '.join(labels)}]" if isinstance(labels, list) and labels else ""
        head = f"- #{num} {title}".rstrip() if num is not None else f"- {title}"
        lines.append(f"{head}{lbl}")
    return lines


def promotion(date, suite, ledger, compare, open_issues, config):
    """Render the dev->prod promotion EVIDENCE report (spec §4.6 Gate 2). Args:
      date         "YYYY-MM-DD".
      suite        {"ok": bool, "failures": [{test_id, text}], "source": str} — a fresh suite run
                   or the latest nightly's parsed results.
      ledger       accepted-failure map (folds already-accepted failures away).
      compare      {"prod_branch": str|None, "dev_branch": str, "result": gh.compare dict|None}.
      open_issues  [{num, title, labels}] open-issue summary.
      config       per-repo config (repo for links).

    EVIDENCE ONLY — there is deliberately NO pass/fail verdict and NO must-pass-to-promote logic
    anywhere; William decides. Never raises (every arg coerced)."""
    cfg = _dict(config)
    repo = _repo(cfg)
    date = date if isinstance(date, str) and date.strip() else "(date unknown)"
    src = _dict(suite).get("source")
    src = src if isinstance(src, str) and src.strip() else "the suite"
    parts = [
        f"# superlooper promotion evidence — {date}\n",
        "**Evidence only — no pass/fail verdict.** Promotion of dev→prod is your judgment "
        "(Gate 2); this report gathers the evidence, it does not decide.\n",
        _section(f"Suite results ({src})", _suite_section(suite, ledger)),
        _section("Merges since last promotion", _merges_section(compare)),
        _section("Open issues", _open_issues_section(open_issues, repo)),
        "## Accepting a failure\n"
        "A failure you judge non-blocking is accepted ONCE, by content, and never re-blocks:\n"
        "`superlooper accept-failure <fingerprint> --note \"…\"` "
        "(the fingerprint prints beside each new failure in future runs).\n",
    ]
    return "\n".join(parts)
