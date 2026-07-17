"""The unattended-debugger watchdog's decision core (issue #66) — pure functions only.

This is the MECHANICAL fallback for a loop that breaks while the owner is away: it watches
the engine's own health signals and, when one trips and nobody intervenes within a grace
window, asks for ONE fresh sl-debugger session (launched by the CLI through the same
interactive launch shim worker sessions use — never a headless `claude -p`). No LLM call
exists anywhere in this module and it makes no repair decisions: it detects, notifies,
waits, launches, journals. All judgment lives in the launched session (the sl-debugger
skill's unattended contract).

This module ALSO owns RUNNER RESURRECTION (issue #208): when a runner is PROVABLY GONE —
heartbeat stale AND its recorded pid dead — the check RESTARTS `superlooper run` (a
deterministic, zero-token process, never an LLM) instead of hiring a debugger for a wedge. A
corpse needs restarting, not diagnosing. That path (`_resurrection`, `after_resurrect`) reroutes
heartbeat_stale away from the debugger episode, caps restarts per rolling hour (then escalates
loudly), and settles the reborn-but-booting runner. It is still pure: the CLI resolves the
recorded anchor pane and executes the restart through resurrect-runner.sh.

Three signals, per the heartbeat/ALERT contract in plugin/skills/superlooper/references/runner-ops.md:
  heartbeat_stale  state/runner.heartbeat older than the configured bound. The heartbeat
                   marks tick PROGRESS (stamped at the END of a successful tick), so a
                   runner that is alive-but-wedged reads stale, exactly as intended. An
                   ABSENT heartbeat is NOT stale — the loop never ran in this state home.
  alert            state/ALERT present (the runner's own persistent-fault alarm). Presence
                   is the whole signal; an unreadable file still counts (fail closed).
  no_progress      work the SCHEDULER would launch NOW exists, every lane is empty, and nothing
                   has launched for the whole bound — with a FRESH heartbeat (a dead loop is
                   the heartbeat's finding) and a usage meter that does NOT read exhausted. The
                   eligibility view is `scheduler.launchable` (launchable_nums), NOT bare
                   issues.eligible, so it respects EXACTLY the holds the scheduler respects.

Designed-safe waits must NEVER trip (the DoD's bright line). They are excluded structurally:
  * gate-waiting on CI / building work is `in-progress`, not `agent-ready` -> not eligible;
  * blocked-by holds fail issues.eligible until the dependency is CLOSED;
  * parked / needs-william issues are not `agent-ready` (and are excluded by label too);
  * frozen-but-building occupies a lane -> lanes_busy resets the no-progress clock (a freeze
    stops merges, never builds — the constitution — so an EMPTY-lane freeze with waiting
    work is a genuine anomaly and does trip);
  * a TERRITORY CLAIM from a gating/holding issue occupies NO lane but holds every overlapping
    eligible candidate behind it under hard affinity -> scheduler.launchable excludes those held
    candidates, so a finished PR gate-waiting on CI (with a wildcard/overlapping claim) plus one
    eligible issue is a designed-safe wait, not a trip (issue #92 — the binding fix);
  * a usage meter that successfully READS exhausted is the runner fail-closed holding on
    purpose -> clock resets. A DARK (unreadable) meter never suppresses — the #46/#76
    asymmetry: fail open on unreadable, fail closed only on reads-exhausted — so a launchd
    context with no Keychain access cannot silently neuter the detector.

When the no-progress view is UNOBSERVABLE this check (gh unreachable — a probe blip OR a refused
list read, distinguished from a genuine empty answer by gh's read-health `ok`), the clocks FREEZE
and an open no_progress episode is HELD, never stood down on the blip: a gh outage cannot drop the
episode and re-trip it (a duplicate owner text + a restarted grace) on recovery. A genuinely
OBSERVED clear (gh up and reporting nothing launchable, or a lane gone busy) still stands down.

Episode discipline (the anti-storm rails): one notify when the episode opens; a clear during
the grace stands down SILENTLY (journal only); at most one VERIFIED launch per episode, a
failed launch retries up to LAUNCH_ATTEMPT_CAP with ONE failure text; a live debugger
session (any worker.d*.lock with a live pid) blocks a new launch — never two. A kill-switch
file (state/WATCHDOG_OFF) makes every check observe + journal and change nothing.
"""
import math

import issues
import scheduler

# Signal codes — sorted alphabetically wherever a list of them is stored or journaled, so
# episode comparisons and journal greps are deterministic.
HEARTBEAT_STALE = "heartbeat_stale"
ALERT = "alert"
NO_PROGRESS = "no_progress"

AUTHORITY_TIERS = ("diagnose-only", "allowlist", "full")
AUTHORITY_DEFAULT = "full"
GRACE_MINUTES_DEFAULT = 30
HEARTBEAT_STALE_MINUTES_DEFAULT = 20   # comfortably past the longest legitimate tick (a
                                       # ship recheck may hold one ~10 min — RECHECK_TIMEOUT)
NO_PROGRESS_MINUTES_DEFAULT = 30
LAUNCH_ATTEMPT_CAP = 3                 # failed-launch retries per episode; then hold (no storm)

# Runner resurrection (issue #208). A runner that is PROVABLY GONE — heartbeat stale AND its
# recorded pid dead — is a deterministic zero-token process that should just restart, as often as
# it needs to, rather than wait for a debugger or the morning. But a runner that keeps dying is a
# real incident: cap the restarts in a rolling hour, then escalate loudly instead of flapping.
RESURRECTION_MAX_PER_HOUR_DEFAULT = 5  # restarts per rolling hour before we stop and escalate
RESURRECTION_WINDOW_SECONDS = 3600     # the rolling window the cap counts over
RESURRECTION_SETTLE_SECONDS = 300      # after a restart the reborn runner is booting/catching up;
                                       # a stale heartbeat with a LIVE pid inside this window is the
                                       # new runner not having stamped its first tick yet, NOT a
                                       # wedge — so it must not open a debugger episode. Ticks are
                                       # ~15s, so one settled tick lands far inside 5 min.

KILL_SWITCH_FILENAME = "WATCHDOG_OFF"  # state/WATCHDOG_OFF disables the whole path
STATE_FILENAME = "watchdog.json"       # state/watchdog.json — episode + no-progress clocks

# The scheduler's launch ceilings (spec): a SUCCESSFUL usage read strictly OVER either one means
# the runner is holding launches BY DESIGN (the comparison below is `>`, matching the scheduler's
# `>` gate — spec ">90%"/">96%"). Values duplicated from scheduler to keep this module importable
# without it; a test pins them equal, and a drift would only shift the designed-hold suppression.
_FIVE_HOUR_CEILING = 90
_SEVEN_DAY_CEILING = 96


def _new_resurrection():
    # issue #208: `attempts` are the restart timestamps in the rolling window (the crash-loop cap
    # counts these); `capped_notified` / `failure_notified` dedup the escalation and failed-restart
    # texts for one down-streak (cleared when the runner is observed healthy again).
    return {"attempts": [], "capped_notified": False, "failure_notified": False}


def new_state():
    # disabled_observed: the signal set the last kill-switched check journaled, so a standing
    # kill-switch journals once per DISTINCT observation instead of once per check (an
    # overnight switch at a 5-min interval must not write ~96 identical lines — the
    # 2026-07-08 unbounded-repetition class). None = not currently disabled-deduping.
    return {"episode": None, "no_progress_since": {}, "next_debugger": 1,
            "disabled_observed": None, "resurrection": _new_resurrection(),
            "next_resurrection": 1}


def coerce_state(raw):
    """A USABLE persisted state from whatever watchdog.json held: wrong-typed shapes (hand
    edits, corruption) degrade field-by-field to the fresh defaults rather than crashing the
    check or fabricating an episode (the fail-open-on-wrong-typed-input defect class)."""
    st = new_state()
    if not isinstance(raw, dict):
        return st
    ep = raw.get("episode")
    if isinstance(ep, dict) and isinstance(ep.get("opened_at"), (int, float)):
        st["episode"] = ep
    nps = raw.get("no_progress_since")
    if isinstance(nps, dict):
        st["no_progress_since"] = {
            k: v for k, v in nps.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}
    nd = raw.get("next_debugger")
    if type(nd) is int and nd >= 1:
        st["next_debugger"] = nd
    dob = raw.get("disabled_observed")
    if isinstance(dob, list) and all(isinstance(x, str) for x in dob):
        st["disabled_observed"] = dob
    res = raw.get("resurrection")
    if isinstance(res, dict):
        r = _new_resurrection()
        at = res.get("attempts")
        if isinstance(at, list):
            r["attempts"] = [t for t in at
                             if isinstance(t, (int, float)) and not isinstance(t, bool)]
        r["capped_notified"] = res.get("capped_notified") is True
        r["failure_notified"] = res.get("failure_notified") is True
        st["resurrection"] = r
    nr = raw.get("next_resurrection")
    if type(nr) is int and nr >= 1:
        st["next_resurrection"] = nr
    return st


def _wcfg(config):
    """The watchdog config block with defaults filled, total on garbage (config.load already
    validates; this keeps the pure core safe when handed a partial/hand-built dict)."""
    cfg = config if isinstance(config, dict) else {}
    w = cfg.get("watchdog") if isinstance(cfg.get("watchdog"), dict) else {}

    def _minutes(key, default):
        v = w.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        return v if ok else default

    authority = w.get("authority")
    if authority not in AUTHORITY_TIERS:
        authority = AUTHORITY_DEFAULT
    allowlist = w.get("allowlist")
    allowlist = [x for x in allowlist if isinstance(x, str)] if isinstance(allowlist, list) else []
    rmax = w.get("resurrection_max_per_hour")
    # >= 0 (0 disables resurrection -> escalate immediately); anything else falls to the default.
    if not (isinstance(rmax, int) and not isinstance(rmax, bool) and rmax >= 0):
        rmax = RESURRECTION_MAX_PER_HOUR_DEFAULT
    return {"authority": authority, "allowlist": allowlist,
            "grace_seconds": _minutes("grace_minutes", GRACE_MINUTES_DEFAULT) * 60,
            "heartbeat_stale_seconds":
                _minutes("heartbeat_stale_minutes", HEARTBEAT_STALE_MINUTES_DEFAULT) * 60,
            "no_progress_seconds":
                _minutes("no_progress_minutes", NO_PROGRESS_MINUTES_DEFAULT) * 60,
            "grace_minutes": _minutes("grace_minutes", GRACE_MINUTES_DEFAULT),
            "resurrection_max_per_hour": rmax}


def usage_reads_exhausted(usage):
    """True ONLY for a successful usage read strictly OVER a launch ceiling (the `>` below matches
    the scheduler's `>` gate — a read exactly AT the ceiling still launches, so it is not
    exhausted) — the runner's designed fail-closed hold. Anything unreadable/partial/malformed is
    False: a dark meter is unreadable, NOT exhausted (the #46/#76 asymmetry), so it never
    suppresses the detector."""
    if not isinstance(usage, dict) or usage.get("auth_status") != "ok":
        return False
    fh, sd = usage.get("five_hour_pct"), usage.get("seven_day_pct")

    def _finite(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)

    if not _finite(fh) or not _finite(sd):
        return False
    return fh > _FIVE_HOUR_CEILING or sd > _SEVEN_DAY_CEILING


def launchable_nums(parsed_issues, lane_state, config, closed_nums, territory_claims):
    """The issue numbers the SCHEDULER would launch RIGHT NOW — every scheduler hold already
    applied. This is the no-progress detector's eligibility view, and it must respect EXACTLY the
    holds the scheduler respects, or a designed-safe wait reads as waiting work and trips:
      * eligibility (agent-ready, valid type, every blocked-by dependency CLOSED) — via the same
        issues.eligible rule scheduler._plan uses;
      * lane capacity + anti-affinity among running lanes and same-tick selections;
      * TERRITORY CLAIMS — the #92 binding fix: a gating/holding issue holds a claim that occupies
        NO lane but blocks every overlapping candidate under hard affinity, so a finished PR
        gate-waiting on CI (with a wildcard/overlapping claim) plus one eligible issue is a
        designed-safe wait, not a no-progress trip.
    Usage is passed as a synthetic PASS (fail_open) DELIBERATELY: the watchdog keeps its OWN
    reads-exhausted gate (usage_reads_exhausted) with the #46/#76 dark-meter asymmetry, so
    re-running scheduler's fail-closed usage rule here would double-count the meter and let a dark
    meter wrongly read as 'nothing launchable' — neutering the detector under launchd. `frozen` is
    False: eligibility ignores freeze (builds continue).
    The belt-and-braces label exclusions (in-progress / parked / needs-owner / needs-william)
    mirror the runner's own candidate filter — a park removes agent-ready mechanically, but a
    half-moved label set must read as a designed-safe wait, never a trip.

    CONTRACT: this is exactly `scheduler.launchable` — the SCHEDULER's holds — NOT the runner's
    full launch decision. The runner (actions.decide) applies two further gates the scheduler does
    not: it PARKS an approved no-touches merge-producing issue (touches_required), and it FREEZES
    launches on a structurally-corrupt issues.json. Such an issue is treated here as launchable, so
    the view can transiently OVER-count relative to what the runner will actually launch. That
    cannot produce a spurious 30-min trip in practice: the runner parks the no-touches issue within
    a tick or two (dropping agent-ready → gone from the next read), and a corrupt issues.json raises
    state/ALERT (the watchdog's `alert` signal fires anyway). The only surviving window is GitHub
    WRITES stuck while READS keep succeeding for the whole bound — and a write dead zone refuses
    reads in lockstep (gh_ok False → clocks freeze). Deliberately NOT duplicating the runner's park
    logic here keeps this a thin scheduler wrapper and this module free of the actions/gate subtree."""
    candidates = []
    for p in parsed_issues:
        if not isinstance(p, dict):
            continue
        labels = {l for l in (p.get("labels") or []) if isinstance(l, str)}
        if {"in-progress", "parked", "needs-owner", "needs-william"} & labels:
            continue
        candidates.append(p)
    selected = scheduler.launchable(candidates, lane_state, config, {"fail_open": True},
                                    closed_nums, False, territory_claims=territory_claims)
    return sorted({sel["num"] for sel in selected
                   if type(sel.get("num")) is int and sel["num"] > 0})


def _hb_fresh(now, heartbeat, stale_seconds):
    ok = isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool)
    return ok and now - heartbeat <= stale_seconds


def _no_progress_observable(now, view, w):
    """Can we TRUST a no_progress CLEAR this check? A definite designed-hold — a busy lane or a
    successful reads-exhausted meter (both DISK/meter truth, independent of GitHub) — IS an
    observation and clears the clock. Otherwise we need a trustworthy eligibility view: gh
    reachable AND a fresh heartbeat. When neither holds, the condition is UNOBSERVABLE (a gh blip
    / a wedged loop), not cleared — so an open no_progress episode is HELD rather than stood down
    on the blip (which would re-trip on recovery: a duplicate owner text + a restarted grace)."""
    if view.get("lanes_busy") or view.get("usage_exhausted"):
        return True
    return bool(view.get("gh_ok")) and _hb_fresh(now, view.get("heartbeat"),
                                                 w["heartbeat_stale_seconds"])


def _update_no_progress(now, view, state, w):
    """Advance the per-issue no-progress clocks and return the (possibly empty) list of issue
    nums that have waited the FULL bound. The clock only runs while the condition it measures
    holds; a designed-safe interruption RESETS it, an unobservable interval FREEZES it:
      * lanes busy or usage reads exhausted -> the wait so far was designed -> clear all;
      * gh unreachable or heartbeat not fresh -> no trustworthy view -> keep clocks untouched
        (the condition held at both observed endpoints; the middle is unknown);
      * otherwise -> keep/first-stamp a clock per currently-eligible issue, drop the rest."""
    since = dict(state.get("no_progress_since") or {})
    # Lane occupancy / a designed usage hold are DISK/meter truth, independent of GitHub:
    # they reset the clocks FIRST, even when the eligibility view is unavailable (the CLI
    # deliberately skips the gh query while lanes are busy).
    if view.get("lanes_busy") or view.get("usage_exhausted"):
        return {}, []
    if not view.get("gh_ok") or not _hb_fresh(now, view.get("heartbeat"),
                                              w["heartbeat_stale_seconds"]):
        return since, []
    nums = view.get("eligible_nums") or []
    since = {str(n): since.get(str(n), now) for n in nums}
    ripe = [int(k) for k, ts in since.items() if now - ts >= w["no_progress_seconds"]]
    return since, sorted(ripe)


def _signals(now, view, state, w):
    """(sorted signal codes, detail strings for the notify body, new no_progress_since)."""
    sigs, details = [], []
    hb = view.get("heartbeat")
    if isinstance(hb, (int, float)) and not isinstance(hb, bool) \
            and now - hb > w["heartbeat_stale_seconds"]:
        sigs.append(HEARTBEAT_STALE)
        details.append(f"runner heartbeat stale {int((now - hb) // 60)} min "
                       "(the loop is not completing ticks)")
    alert = view.get("alert")
    if alert is not None:
        reasons = alert.get("reasons") if isinstance(alert, dict) else None
        reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
        sigs.append(ALERT)
        details.append("ALERT present (" + (", ".join(reasons) or "unreadable") + ")")
    since, ripe = _update_no_progress(now, view, state, w)
    if ripe:
        sigs.append(NO_PROGRESS)
        waited = min(since[str(n)] for n in ripe)
        details.append("approved work waiting " + str(int((now - waited) // 60)) + "+ min with "
                       "every lane free and nothing launching ("
                       + ", ".join(f"#{n}" for n in ripe) + ")")
    pairs = sorted(zip(sigs, details))          # alphabetical by code: deterministic everywhere
    return [s for s, _ in pairs], [d for _, d in pairs], since


def _rec(outcome, signals, **extra):
    return {"act": "watchdog", "outcome": outcome, "signals": list(signals), **extra}


def _rrec(outcome, signals, **extra):
    """A resurrection journal record — a DISTINCT act (issue #208) so the morning report and
    journal greps separate runner restarts from debugger episodes (`watchdog`) and from the
    live runner's own self-restart (`runner_restart`, issue #116)."""
    return {"act": "runner_resurrect", "outcome": outcome, "signals": list(signals), **extra}


def _without(sigs, details, drop):
    """(sigs, details) with `drop` removed — the two lists are index-aligned (built together in
    _signals), so a signal's detail string is dropped with it."""
    kept = [(s, d) for s, d in zip(sigs, details) if s != drop]
    return [s for s, _ in kept], [d for _, d in kept]


def _resurrection(now, view, w, sigs, details, new_state):
    """The provably-gone-runner restart decision (issue #208), folded into the mechanical check.

    Returns (sigs, details, resurrect, journal, notify, new_state, runner_down). `runner_down` is
    the RAW fact — provably gone THIS check — reported on every check regardless of the cap or the
    journal's escalation dedup, so a caller's summary can stay honest while the escalation stays
    quiet (fresh-review P1-1: a deduped journal made the 3rd capped check read "healthy").
    `runner_dead` in the view is
    the CLI's "state/runner.lock names a DEAD pid" read — a CRASH leaves the pidfile behind, a
    clean stop removes it, so it distinguishes 'provably gone' from 'deliberately stopped'.

      * heartbeat stale AND runner_dead -> the loop is a corpse: restarting it (a deterministic,
        zero-token process) is the fix, NOT hiring a debugger. heartbeat_stale is REMOVED from the
        signal set the debugger episode sees, and — under the rolling-hour cap — a resurrect request
        is emitted; past the cap a repeatedly-dying runner escalates LOUDLY, once, and stops.
      * heartbeat stale AND the pid is ALIVE, shortly after a restart -> the reborn runner is still
        booting and has not stamped its first tick, so the OLD heartbeat still reads stale. Inside
        the settle window that is catching-up, NOT a wedge: suppress it (no debugger episode). After
        the settle window a still-stale heartbeat is a genuine wedge and the debugger path engages.

    Both required (the DoD): a fresh heartbeat with a dead pid is a runner that crashed seconds ago,
    and we wait the heartbeat-stale bound (giving a human first crack) before stepping in."""
    r = dict(new_state.get("resurrection") or _new_resurrection())
    r.setdefault("attempts", [])
    r.setdefault("capped_notified", False)
    r.setdefault("failure_notified", False)
    resurrect, journal, notify = None, [], []
    runner_dead = bool(view.get("runner_dead"))
    hb_stale = HEARTBEAT_STALE in sigs

    # A healthy runner (heartbeat fresh, pid alive) closes the down-streak: re-arm the dedup flags
    # so a genuinely NEW incident texts again. (Booting — stale hb, live pid — is NOT healthy, so
    # its heartbeat_stale keeps this from firing mid-recovery.)
    if not hb_stale and not runner_dead:
        r["capped_notified"] = False
        r["failure_notified"] = False

    attempts = [t for t in r["attempts"]
                if isinstance(t, (int, float)) and not isinstance(t, bool)]

    if hb_stale and runner_dead:
        present = sorted(sigs)                              # capture BEFORE filtering, for the memo
        sigs, details = _without(sigs, details, HEARTBEAT_STALE)
        recent = [t for t in attempts if now - t < RESURRECTION_WINDOW_SECONDS]
        cap = w["resurrection_max_per_hour"]
        if len(recent) >= cap:
            r["attempts"] = recent
            if not r["capped_notified"]:                   # escalate once per capped streak (no storm)
                r["capped_notified"] = True
                journal.append(_rrec("resurrect_capped", present,
                                     attempts=len(recent), max_per_hour=cap))
                if cap == 0:                               # auto-restart disabled by config
                    notify.append((
                        "superlooper runner is DOWN — auto-restart is DISABLED",
                        "the runner is provably gone (heartbeat stale, pid dead) but automatic "
                        "restart is disabled (watchdog.resurrection_max_per_hour = 0). The loop is "
                        "down and will stay down until you restart it."))
                else:                                      # genuine crash-loop cap hit
                    # ATTEMPTED, never "was restarted": an attempt is recorded before delivery, so an
                    # undeliverable one (no_pane — no tab made, nothing launched) burns a slot too.
                    # Counting it is deliberate; asserting a restart that never happened is not
                    # (fresh-review P1-2 — fabricated history is this codebase's cardinal sin).
                    notify.append((
                        "superlooper runner keeps dying — auto-restart PAUSED",
                        f"automatic restart has been attempted {len(recent)} time(s) in the last "
                        "hour and the runner is still down. That is a real incident, not a flap, so "
                        "automatic resurrection is paused — the loop needs you."))
        else:
            n = new_state.get("next_resurrection", 1)
            resurrect = {"id": f"r{n}", "signals": present}
            new_state["next_resurrection"] = n + 1
            r["attempts"] = recent + [now]
            r["capped_notified"] = False
    elif hb_stale and not runner_dead:
        last = max(attempts, default=None)
        if last is not None and now - last < RESURRECTION_SETTLE_SECONDS:
            sigs, details = _without(sigs, details, HEARTBEAT_STALE)   # reborn runner still booting
        r["attempts"] = attempts
    else:
        r["attempts"] = attempts

    new_state = dict(new_state, resurrection=r)
    return sigs, details, resurrect, journal, notify, new_state, (hb_stale and runner_dead)


def evaluate(now, config, view, state):
    """One mechanical check. Returns {"state", "journal", "notify", "launch", "resurrect",
    "runner_down"}:
      state    the new state to persist (episode + no-progress clocks + id counter);
      journal  act:"watchdog" records for TRANSITIONS only (open/stand-down/launch outcomes
               live in after_launch; quiet waiting checks journal nothing);
      notify   [(title, body)] — at most one entry (the episode-opening text);
      launch   None, or the launch request {"id","signals","authority","allowlist"} the
               caller executes through the launch shim, then feeds to after_launch.
      resurrect  None, or the restart request {"id","signals"} the caller executes through
               resurrect-runner.sh, then feeds to after_resurrect (issue #208).
      runner_down  the runner is PROVABLY GONE this check (heartbeat stale AND its recorded pid
               dead). Reported EVERY check, unlike the escalation journal/notify, which dedup to
               once per capped streak — so a caller can stay honest ("the runner is DOWN") on
               checks that deliberately say nothing. False under the kill switch (path suppressed).
    The caller supplies `view` (every I/O fact, already read) so this stays a pure function.
    """
    w = _wcfg(config)
    state = coerce_state(state)
    sigs, details, since = _signals(now, view, state, w)

    if view.get("kill_switch"):
        # Observe + journal + change nothing else: no episode opens, no clock advances, no
        # launch, NO resurrection (WATCHDOG_OFF is the whole-path kill switch). The journal record
        # dedups on the OBSERVED signal set (review P1-2): a standing switch writes one line per
        # distinct observation, never one per check.
        # runner_down stays False here: the kill switch suppresses the whole resurrection path, so
        # the caller's summary reports DISABLED (what it observed), never a cap it never evaluated.
        if sigs == state.get("disabled_observed"):
            return {"state": state, "notify": [], "launch": None, "journal": [], "resurrect": None,
                    "runner_down": False}
        return {"state": dict(state, disabled_observed=sigs), "notify": [], "launch": None,
                "journal": [_rec("disabled", sigs)], "resurrect": None, "runner_down": False}

    new_state = dict(state, no_progress_since=since, disabled_observed=None)
    journal, notify, launch = [], [], None

    # Runner resurrection (issue #208) runs BEFORE the debugger-episode logic: it may reroute a
    # provably-gone runner's heartbeat_stale away from the episode (a corpse needs restarting, not
    # diagnosing) and emit a resurrect request or a loud escalation.
    sigs, details, resurrect, res_journal, res_notify, new_state, runner_down = _resurrection(
        now, view, w, sigs, details, new_state)
    journal.extend(res_journal)
    notify.extend(res_notify)
    ep = state.get("episode")

    if not sigs:
        if ep is not None:
            ep_signals = ep.get("signals") or []
            if NO_PROGRESS in ep_signals and not _no_progress_observable(now, view, w):
                # The no_progress condition is UNOBSERVABLE this check (gh unreachable / heartbeat
                # not fresh), NOT cleared. Standing down here would drop the episode on a gh blip
                # and re-trip it on recovery — a duplicate owner text and a restarted grace that
                # can defer the launch indefinitely across repeated blips. HOLD the SAME episode
                # (opened_at, grace clock, frozen no-progress clock all intact); a genuinely
                # OBSERVED clear stands it down below. Quiet: no notify, no launch, no journal line
                # (a long outage at a 5-min interval must not write a record per check).
                new_state["episode"] = ep
                return {"state": new_state, "journal": journal, "notify": notify,
                        "launch": None, "resurrect": resurrect, "runner_down": runner_down}
            # Self-recovery or owner intervention during (or after) the grace: stand down
            # SILENTLY — the journal keeps the record, the phone stays quiet.
            journal.append(_rec("stand_down", ep_signals))
        new_state["episode"] = None
        return {"state": new_state, "journal": journal, "notify": notify, "launch": launch,
                "resurrect": resurrect, "runner_down": runner_down}

    if ep is None:
        ep = {"signals": sigs, "opened_at": now, "detail": "; ".join(details),
              "launched_at": None, "launch_id": None, "launch_attempts": 0,
              "launch_failure_notified": False}
        notify.append((
            "superlooper watchdog",
            "; ".join(details) + f". If this still stands in {int(w['grace_minutes'])} min, "
            f"an unattended sl-debugger session launches (authority: {w['authority']}). It "
            "stands down automatically if the signal clears; touch state/"
            f"{KILL_SWITCH_FILENAME} to disable."))
        journal.append(_rec("notified", sigs, grace_seconds=w["grace_seconds"],
                            authority=w["authority"]))
    else:
        merged = sorted(set(ep.get("signals") or []) | set(sigs))
        if merged != ep.get("signals"):
            ep = dict(ep, signals=merged, detail="; ".join(details))
    new_state["episode"] = ep

    grace_elapsed = now - ep["opened_at"] >= w["grace_seconds"]
    if grace_elapsed and ep.get("launched_at") is None:
        if view.get("debugger_live"):
            # Never two debugger sessions: a prior session (this episode's failed attempt, or
            # an earlier episode's still-open one) holds a live worker.d*.lock — wait it out.
            journal.append(_rec("skipped_live_session", ep["signals"]))
        elif (ep.get("launch_attempts") or 0) < LAUNCH_ATTEMPT_CAP:
            n = new_state.get("next_debugger", 1)
            launch = {"id": f"d{n}", "signals": list(ep["signals"]),
                      "authority": w["authority"], "allowlist": list(w["allowlist"])}
            new_state["next_debugger"] = n + 1

    return {"state": new_state, "journal": journal, "notify": notify, "launch": launch,
            "resurrect": resurrect, "runner_down": runner_down}


def after_launch(now, config, state, launch, rc):
    """Record the outcome of an executed launch request. rc==0 (delivery VERIFIED by the
    launch shim) marks the episode launched — once per incident, no relaunch on the same
    episode. A nonzero rc counts an attempt (retried by later checks up to LAUNCH_ATTEMPT_CAP)
    and texts the owner ONCE per episode about the failure — the loop still needs attention
    and now the fallback could not start either. `config` is accepted for call-site symmetry with
    evaluate(); after_launch reads no config knob (authority/allowlist already rode into the launch
    request), so it deliberately does NOT resolve _wcfg(config)."""
    state = coerce_state(state)
    ep = state.get("episode")
    if ep is None:                       # stand-down raced the launch; keep the honest record
        ep = {"signals": list(launch.get("signals") or []), "opened_at": now,
              "launched_at": None, "launch_id": None, "launch_attempts": 0,
              "launch_failure_notified": False}
    journal, notify = [], []
    sigs = launch.get("signals") or []
    if rc == 0:
        ep = dict(ep, launched_at=now, launch_id=launch.get("id"))
        journal.append(_rec("launched", sigs, id=launch.get("id"),
                            authority=launch.get("authority")))
        notify.append(("superlooper watchdog launched sl-debugger",
                       f"unattended session {launch.get('id')} launched — signals: "
                       + ", ".join(sigs) + f" (authority: {launch.get('authority')}). Its "
                       "memo will land in the state home's reports/."))
    else:
        ep = dict(ep, launch_attempts=(ep.get("launch_attempts") or 0) + 1)
        journal.append(_rec("launch_failed", sigs, id=launch.get("id"), rc=rc))
        if not ep.get("launch_failure_notified"):
            ep["launch_failure_notified"] = True
            notify.append(("superlooper watchdog could NOT launch sl-debugger",
                           f"launch of session {launch.get('id')} failed (rc={rc}) — most "
                           "likely no resolvable cmux pane (loop stopped and its tab gone?). "
                           "The tripped signal still stands: " + ", ".join(sigs)
                           + ". The loop needs you."))
    return {"state": dict(state, episode=ep), "journal": journal, "notify": notify}


def after_resurrect(now, config, state, resurrect, rc):
    """Record the outcome of an executed resurrect request (issue #208). rc==0 (the runner came up —
    the launcher VERIFIED a live pidfile) TEXTS THE OWNER: a dead runner restarting itself is a loud
    event, not a silent one (the DoD). A nonzero rc journals the failure and texts ONCE per down-
    streak (the reborn tab could not be placed — its pane is gone, or the shim did not run), then
    later checks retry up to the rolling-hour cap. `config` is accepted for call-site symmetry with
    after_launch; this reads no config knob (the cap already gated the request in evaluate).

    The attempt itself was recorded in evaluate (so a launch that fails to VERIFY still counts toward
    the cap — a runner whose tab is gone must not retry forever); after_resurrect only journals the
    outcome and manages the failure-text dedup."""
    state = coerce_state(state)
    r = dict(state.get("resurrection") or _new_resurrection())
    journal, notify = [], []
    sigs = list(resurrect.get("signals") or [])
    rid = resurrect.get("id")
    if rc == 0:
        r["failure_notified"] = False
        journal.append(_rrec("resurrected", sigs, id=rid))
        notify.append((
            "superlooper runner was down — restarted it",
            f"the runner was provably gone (signals: {', '.join(sigs) or 'heartbeat_stale'}) and "
            f"has been automatically restarted ({rid}) in its cmux tab. It reconciles from GitHub + "
            "disk exactly like a manual restart — no work lost, no counters reset."))
    else:
        journal.append(_rrec("resurrect_failed", sigs, id=rid, rc=rc))
        if not r.get("failure_notified"):
            r["failure_notified"] = True
            notify.append((
                "superlooper could NOT restart the runner",
                f"the runner is down and the automatic restart ({rid}) failed (rc={rc}) — most "
                "likely its cmux tab/pane is gone, so a new one cannot be placed without you. The "
                "loop is not running."))
    return {"state": dict(state, resurrection=r), "journal": journal, "notify": notify}


def render_brief(template, mapping):
    """Literal {name} substitution — brief.py's _sub convention (never str.format, which
    chokes on prose braces)."""
    for k, v in mapping.items():
        template = template.replace("{" + k + "}", str(v))
    return template
