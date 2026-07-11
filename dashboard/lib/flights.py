"""The flight model (Task 3 / decision B.1) — every display semantic, derived server-side.

This is the dashboard's truth layer. The raw facts read by Task 2 (a state home's ``issues.json``,
markers, journal, heartbeat, freeze/alert files) and Task 4 (per-issue ``gh`` PR facts + a
worktree's diff-stat cargo) are honest but shapeless; here they become **flight objects** whose
every field is a settled semantic — which discrete circuit stage a flight is at, whether its
contrail is crisp or its progress is secretly flat, whether the gate is cleared, which repo is the
worst-off. The JavaScript downstream only binds these values to pixels; it computes nothing. The
squint test (design record §3, costume rule 3): delete the airplane art and what remains — this
module's output — must still be a correct state diagram.

Everything here is a PURE function of already-read facts: no ``gh``, no file I/O, no subprocess, no
network, no wall-clock read except through an injected ``now``. That is what makes the whole truth
layer unit-testable to the line.

The design record is the constitution for this file. The load-bearing disciplines, each pinned by
a test in ``tests/test_flights.py``:

* **Position is discrete, never time (§3).** The circuit stage comes from status + markers +
  journal landmarks — never from "how long has it been flying". ``circuit_stage`` takes no clock
  at all; elapsed time can only change the *liveness* axis (a separate signal), never the stage.
* **Off-path states never collapse (§5).** parked / awaiting (amber owner decision) / holding /
  session-frozen (a dead SESSION) / merges-freeze (the REPO's landings calmly paused) are five
  distinct things demanding different responses; a test proves no two share a value.
* **Progress ≠ liveness (§5).** A crisp contrail with flat progress is an explicit ``spinning``
  warning — the doom-looping worker re-running one failing test must never read as the healthiest
  plane on the field.
* **Honesty in celebration (§7).** A wandered merge (``wander: true``) earns no flourish.
* **The incident sign counts machine stumbles only (§7).** park / conflict-cap / failed auto-fix /
  runner death repaint it; William approving, re-approving, answering a bounce or grading a
  promotion never does. And the corner counter is outcome-only — no human-latency stopwatch may
  ever exist on this surface (§7 kill list #4).
"""
import re
import time

# --------------------------- vocabulary (the state diagram's alphabet) ---------------------------
# On-circuit discrete stages (design record §3), in traffic-pattern order. A flight's position is
# ALWAYS exactly one of these (or an off-path state below) — never an interpolation.
AT_STAND = "at-stand"      # approved, queued — chocks off, waiting for a runway
TAXI_OUT = "taxi-out"      # launching; delivery-verification visible (a flake never reaches takeoff)
TAKEOFF = "takeoff"        # session started, not yet visibly building
DOWNWIND = "downwind"      # building — the long working leg (liveness + progress live here)
BASE_TURN = "base-turn"    # report filed, turning toward the gate
FINAL = "final"            # the gate: report ✓ review ✓ CI ✓ mergeable ✓ → cleared to land
TOUCHDOWN = "touchdown"    # merged — the landing
TAXI_IN = "taxi-in"        # closed, cleaned up
CIRCUIT_STAGES = (AT_STAND, TAXI_OUT, TAKEOFF, DOWNWIND, BASE_TURN, FINAL, TOUCHDOWN, TAXI_IN)

# Off-path states (design record §5) — each rendered unmistakably differently because each demands
# an opposite response. These are NOT positions on the circuit.
PARKED = "parked"                  # the machine gave up — chocks, dimmed, "your call" (never restful)
AWAITING = "awaiting"              # amber: a decision waits on William (needs-william / bounced)
HOLDING = "holding"                # sequenced behind another lane — "number 2 for landing"
SESSION_FROZEN = "session-frozen"  # a dead SESSION (liveness past the frozen tier) — grey, no contrail
STRANDED = "stranded"              # a FINISHED session at the gate the GATE stopped advancing (§5 /
                                   # issue #22) — the report is filed; the runner never landed it.
                                   # NOT a dead session: the work completed, so it never greys out.
MERGES_FREEZE = "merges-freeze"    # the REPO's landings are calmly paused — repair flight dispatched
OFF_PATH_STATES = (PARKED, AWAITING, HOLDING, SESSION_FROZEN, STRANDED, MERGES_FREEZE)

# Liveness tiers (design record §5) — the contrail, tied to the runner's real activity-age tiers.
FRESH = "fresh"    # activity file younger than the repo's idle threshold — bold, bright contrail
IDLE = "idle"      # past idle, short of frozen — sputtering contrail, the tower peeks
FROZEN = "frozen"  # past the freeze threshold — no contrail; a stalled session, visible across the room
LIVENESS_TIERS = (FRESH, IDLE, FROZEN)


# =============================== liveness (§5) ===============================

def liveness_tier(mtime, now, idle_seconds, freeze_seconds):
    """The contrail tier for an activity file last touched at ``mtime`` (epoch seconds), as of
    ``now``, against a repo's OWN ``idle_seconds`` / ``freeze_seconds`` thresholds.

    ``FRESH`` while younger than ``idle_seconds``; ``IDLE`` from there to ``freeze_seconds``;
    ``FROZEN`` at or past ``freeze_seconds`` (both boundaries inclusive of the higher tier, mirroring
    the runner's ``>=`` tiering). ``mtime`` of ``None`` (no activity file) ⇒ ``None``: a flight with
    no session running has no liveness signal, and must never be painted as "frozen" for lack of one.
    Thresholds are the repo's declared numbers (decision B.4), never a module-global constant."""
    if mtime is None:
        return None
    age = now - mtime
    if age >= freeze_seconds:
        return FROZEN
    if age >= idle_seconds:
        return IDLE
    return FRESH


# =============================== contrail kind (§5 — Task 7) ===============================

def contrail_kind(mtime, now, idle_seconds, freeze_seconds):
    """The contrail the field draws for an activity file last touched at ``mtime`` — the §5 visual
    ladder over the same real tiers as :func:`liveness_tier`: ``crisp`` while young, ``thin`` once
    the fresh tier has half-aged ("thins as it ages"), ``sputter`` through the idle tier, ``none``
    at or past frozen. No activity file ⇒ ``none`` — a flight with no session running trails
    nothing, and must never be painted frozen for lack of a signal. Thresholds are the repo's own
    (decision B.4)."""
    if mtime is None:
        return "none"
    age = now - mtime
    if age >= freeze_seconds:
        return "none"
    if age >= idle_seconds:
        return "sputter"
    if age >= idle_seconds / 2.0:
        return "thin"
    return "crisp"


# =============================== the living clock (§7) ===============================

def daypart(now):
    """``day`` / ``dusk`` / ``night`` from the LOCAL wall clock at epoch ``now`` — the living
    clock that drives field lighting (design record §7). Exactly the prototype's three tints; the
    dawn hours reuse the dusk wash. This is the ONLY thing the clock may touch — sky/visibility
    weather is banned in writing (§7 kill list #6)."""
    h = time.localtime(now).tm_hour
    if 8 <= h < 18:
        return "day"
    if 18 <= h < 21 or 6 <= h < 8:
        return "dusk"
    return "night"


# =============================== runways = the repo's real lanes (§3) ===============================

def assign_runways(lanes):
    """Map each distinct real lane to a runway index (0/1) — "2 runways = 2 concurrent builds"
    (design record §3). Deterministic across input order (sorted lane names, alternating), so the
    same lane owns the same runway every poll. ``None`` entries (no lane held) are not lanes."""
    distinct = sorted({l for l in lanes if l is not None}, key=str)
    return {lane: i % 2 for i, lane in enumerate(distinct)}


def empty_queue_caption(lanes):
    """The empty-departures caption, singular/plural-correct, reflecting the repo's REAL lane count.

    "N runways open" = N concurrent build slots ("2 runways = 2 concurrent builds", design record
    §3). This is a truth-first surface, so it must never claim a count it can't stand behind (issue
    #35): only a genuine positive int prints a number; ``None`` — or any unreadable value (0,
    negative, non-int, bool) — falls back to a bare "QUEUE EMPTY" with NO invented number. Owned
    server-side so the JS binds this finished string (design record B.1) and the singular/plural
    never drifts into a client-side branch."""
    if isinstance(lanes, bool) or not isinstance(lanes, int) or lanes < 1:
        return "QUEUE EMPTY"
    noun = "RUNWAY" if lanes == 1 else "RUNWAYS"
    return "QUEUE EMPTY · %d %s OPEN" % (lanes, noun)


# =============================== airline identity (§7) ===============================
# SNES-class tail colors (the prototype's PAL.tail blue and the dashboard-terminal teal first).
# Deterministic per slug via a tiny explicit hash — Python's builtin hash() is salted per process
# and would repaint every airline on restart.
AIRLINE_COLORS = ("#2E5EA8", "#2E8B8B", "#7A5CBF", "#C2542E",
                  "#2F8A4C", "#B0397E", "#8A6D2F", "#3E7BC4")


def airline_color(slug):
    """The airline's stable tail color for ``slug`` — identity serves legibility (§7), so the
    color must survive restarts and be the same on every surface that draws this repo."""
    h = 0
    for ch in str(slug):
        h = (h * 31 + ord(ch)) % 1000003
    return AIRLINE_COLORS[h % len(AIRLINE_COLORS)]


# =============================== attempt counter + wander (§3/§7) ===============================

def attempt_number(journal):
    """Which attempt this flight is on: 1 by default, +1 for every ``regenerate`` in ``journal``.

    A conflict-regeneration is an honest retire-and-rebuild (design record §3) — the old attempt is
    retired and a NEW flight taxis out as attempt 2. ``journal`` is this issue's records (the caller
    filters by id); counting the append-only ``regenerate`` events is absence-proof — no stored
    counter to drift."""
    return 1 + sum(1 for r in journal if isinstance(r, dict) and r.get("act") == "regenerate")


def flight_label(num, attempt):
    """The flight number shown on every surface: ``SL-<num>`` on the first attempt, ``SL-<num>·A<n>``
    once a go-around has happened (design record §3; DoD format ``SL-N·A2``). Flight number = issue
    number so every surface stays journal-greppable."""
    base = "SL-%d" % num
    return base if attempt <= 1 else "%s·A%d" % (base, attempt)


def merged_wandered(journal):
    """``True`` iff this flight's most recent ``merge`` wandered outside its declared areas
    (``wander: true``). A wandered merge is a real landing but a dishonest one to celebrate — the
    design record's honesty law (§7) gives it a neutral touchdown and a "see report" marker, never a
    flourish. Only a merge sets this; a flight still in the air is not "wandered"."""
    wander = False
    for r in journal:
        if isinstance(r, dict) and r.get("act") == "merge":
            wander = bool(r.get("wander"))   # last merge wins (a regenerated flight can merge twice)
    return wander


def landed_clean(journal):
    """``True`` only with POSITIVE proof of a clean landing: the most recent ``merge`` succeeded
    (``outcome == "ok"``) AND did not wander. No merge record at all, a failed merge, or a wandered
    merge ⇒ ``False``. This is the honesty gate on celebration (design record §7): a flourish must be
    EARNED by an observed clean landing, never granted by the mere absence of a wander flag — so a
    ``status: merged`` flight whose merge event is missing or corrupt is shown as landed but is NOT
    celebrated until the clean landing is proven."""
    last_merge = None
    for r in journal:
        if isinstance(r, dict) and r.get("act") == "merge":
            last_merge = r
    if last_merge is None or last_merge.get("outcome") != "ok":
        return False
    return not bool(last_merge.get("wander"))


# =============================== circuit stage — DISCRETE (§3) ===============================
# Stages a running session can pass through in the air (used to decide when liveness may downgrade
# a stage to a dead session). at-stand has no session; merged/taxi-in have finished.
_IN_AIR = (TAXI_OUT, TAKEOFF, DOWNWIND, BASE_TURN, FINAL)


def circuit_stage(status, report_present=False, session_started=False,
                  launched=False, cleared=False, closed=False):
    """The DISCRETE on-circuit position from a runner ``status`` plus a few discrete facts —
    deliberately taking NO clock, so position can never be time-derived (design record §3, costume
    rule 3). Progression: an approved-but-unlaunched flight is ``AT_STAND``; a launch dispatched but
    the session not yet up is ``TAXI_OUT``; a started-but-not-yet-building session is ``TAKEOFF``;
    a session emitting activity is ``DOWNWIND``; a filed report turns it to ``BASE_TURN``; the gate
    (status ``gating`` or all four checks green) is ``FINAL``; a merge is ``TOUCHDOWN``, and a
    closed+cleaned flight ``TAXI_IN``. Off-path states (parked/awaiting/holding/frozen) are NOT
    handled here — ``flight_stage`` layers them on top."""
    if status == "merged":
        return TAXI_IN if closed else TOUCHDOWN
    if cleared or status == "gating":
        return FINAL
    if status == "ready":
        return TAXI_OUT if launched else AT_STAND
    # running, blocked (a live radio call is still flying), exited, or an unknown status all read
    # their position from the same discrete landmarks — report filed > building > launched > queued.
    if report_present:
        return BASE_TURN
    if session_started:
        return DOWNWIND
    if launched:
        return TAKEOFF
    return TAXI_OUT if status == "running" else AT_STAND


def flight_stage(status, liveness=None, bounced=False, long_wait=False,
                 report_present=False, session_started=False, launched=False,
                 cleared=False, closed=False):
    """The flight's PRIMARY state: an off-path state when one applies, otherwise the on-circuit
    stage. Off-path states take precedence in the order that reflects what William must DO, and
    never collapse into one another (design record §5):

      1. ``PARKED`` — the machine gave up; dominates everything (chocks, "your call").
      2. ``AWAITING`` — an owner decision waits: status ``needs_william``/``bounced``, or a blocked
         session whose marker is a ``BOUNCED:`` memo. Amber, never confusable with a dead session.
      3. ``HOLDING`` — sequenced behind an overlapping lane ("number 2 for landing").
      4. ``STRANDED`` — a FINISHED session at the gate (``FINAL`` + a filed report) whose activity
         has aged into the frozen tier. The session COMPLETED its work — a finished session's
         activity file naturally goes stale — so this is the GATE failing to advance, NOT a dead
         session (issue #22). Distinct look, distinct response: point the owner at the runner/gate.
      5. ``SESSION_FROZEN`` — a genuinely dead session: status ``frozen``, OR an in-air stage with
         NO filed report whose contrail has aged into the frozen tier. Time DEGRADED its liveness
         mid-work; it did not advance the plane up the circuit.

    ``long_wait`` (the worker's own ``state/awaiting/<id>`` background-wait touch) is deliberately
    NOT an owner decision, so it never turns a flight amber — it rides along as an annotation the
    caller adds elsewhere."""
    if status == "parked":
        return PARKED
    if status in ("needs_william", "bounced") or bounced:
        return AWAITING
    if status == "holding":
        return HOLDING
    if status == "frozen":
        return SESSION_FROZEN
    stage = circuit_stage(status, report_present=report_present, session_started=session_started,
                          launched=launched, cleared=cleared, closed=closed)
    if liveness == FROZEN and stage in _IN_AIR:
        # A stale contrail at an in-air stage is a dead session — EXCEPT at the gate with a filed
        # report: there the session already finished, so its naturally-stale activity means the GATE
        # stopped advancing, not that the session died (issue #22). Only the frozen tier trips this;
        # a fresh/absent gate is still cleared-to-land (FINAL), never fabricated into "stranded".
        if stage == FINAL and report_present:
            return STRANDED
        return SESSION_FROZEN
    return stage


# =============================== progress ≠ liveness → spinning (§5) ===============================
# The doom-loop detector's two tunable knobs. A flat window is one with real REPETITION (not merely
# a quiet session) and no diff growth. Design record §8 flags these as needing calibration against
# real incidents — hence named constants, not magic literals.
_SPIN_MIN_EVENTS = 3    # fewer events than this is "quiet", never "flat" — don't false-alarm the calm
_SPIN_MAX_VARIETY = 1   # one repeating kind of event = the worker is stuck on a single operation


def _event_kind(rec):
    """The record's KIND for variety counting. A journal ``event`` envelope carries its real fact in
    ``event.type`` (``session_blocked`` / ``idle`` / ``frozen`` / ``session_finished`` …), so it is
    keyed as ``event:<type>`` — otherwise every distinct event would collapse to one kind and a
    lively mixed window could masquerade as flat (a false ``spinning`` alarm). Any other record is
    keyed by its bare ``act``."""
    act = rec.get("act")
    if act == "event":
        ev = rec.get("event")
        if isinstance(ev, dict) and ev.get("type"):
            return "event:%s" % ev.get("type")
    return act


def progress(journal_window, diff_delta=None):
    """A progress reading independent of liveness (design record §5), from a rolling ``journal_window``
    (this issue's recent records, sliced by the caller) plus an optional ``diff_delta`` — the change
    in cargo line-count since the last sample, which the server tracks across polls (``None`` when
    the worktree diff isn't readable).

    Returns ``{variety, events, diff_delta, growing, flat}``. ``variety`` is the number of DISTINCT
    ``act`` types in the window; ``growing`` is ``True`` when the diff advanced. Progress is ``flat``
    only when code is NOT being written (``diff_delta`` is 0 or unknown) AND the window shows genuine
    repetition — at least ``_SPIN_MIN_EVENTS`` records of ``_SPIN_MAX_VARIETY`` or fewer kinds. A
    growing diff always rescues a flight (real work is landing on disk); a sparse window never
    flat-flags a calm-but-healthy session."""
    events = [r for r in journal_window if isinstance(r, dict)]
    variety = len({_event_kind(r) for r in events})
    growing = isinstance(diff_delta, (int, float)) and not isinstance(diff_delta, bool) and diff_delta > 0
    flat = (not growing) and len(events) >= _SPIN_MIN_EVENTS and variety <= _SPIN_MAX_VARIETY
    return {"variety": variety, "events": len(events),
            "diff_delta": diff_delta, "growing": growing, "flat": flat}


def is_spinning(liveness, prog):
    """The explicit ``spinning`` warning (design record §5): a CRISP contrail (``FRESH`` liveness)
    over FLAT progress. This is the one honesty fix that stops a doom-looping worker — alive-looking
    but getting nowhere — from rendering as the healthiest plane on the field. An idle or frozen
    flight with flat progress is not "spinning" (it's idle/frozen, a separate signal)."""
    return liveness == FRESH and bool(prog.get("flat"))


# =============================== gate checklist (§3 — real check names) ===============================

def _ci_passed(rollup, required_checks):
    """Every configured required check present AND concluded ``SUCCESS``. Fail closed: an absent,
    pending (no conclusion), or failing required check — or no required checks configured at all —
    means the gate's CI line is NOT green. Non-required checks are ignored; the gate reads only the
    real required checks (decision B.11)."""
    if not required_checks:
        return False
    by_name = {c.get("name"): c for c in rollup if isinstance(c, dict)}
    for name in required_checks:
        c = by_name.get(name)
        if not c or c.get("conclusion") != "SUCCESS":
            return False
    return True


def gate_checklist(pr_facts, report_present, review_present, required_checks):
    """The clearance checklist rendered at ``FINAL`` — the four REAL, fixed check names (design
    record §3): ``report`` (a per-issue report exists), ``review`` (a fresh-agent review verdict is
    posted), ``ci`` (all required checks green), ``mergeable`` (the PR fits cleanly onto today's
    code). ``cleared`` is all four — "cleared to land". Every line fails closed: an unreadable PR
    (``{}``) or a missing signal reads as not-passed, never as a hopeful yes the runner would then
    refuse to act on."""
    pr = pr_facts if isinstance(pr_facts, dict) else {}
    rollup = pr.get("statusCheckRollup")
    report = bool(report_present)
    review = bool(review_present)
    ci = _ci_passed(rollup if isinstance(rollup, list) else [], required_checks)
    mergeable = pr.get("mergeable") == "MERGEABLE"
    return {"report": report, "review": review, "ci": ci, "mergeable": mergeable,
            "cleared": report and review and ci and mergeable}


# =============================== incident sign — machine stumbles ONLY (§7) ===============================
# The ONLY events that repaint "N landings since the last incident": machine-side failures. Every
# one is the MACHINE stumbling — never William acting.
#   • park           — the machine gave up on a flight. This is ALSO how a conflict-cap hit and a
#                      launch-flake surface in the journal (verified: sample-data.txt), so the whole
#                      "gave up" family collapses to this one act.
#   • autofix_failed — a freeze whose automatic repair itself failed/stalled (design record §3, the
#                      one crash state); recognized as a top-level act OR an `event` envelope type.
#   • runner_down    — the dead-man's switch tripped (design record §6); likewise recognized either
#                      way, so if the runner ever journals it, it counts without a code change here.
# William's gates (approve/reapprove, bounce answers, promotion) and the machine's HELP actions
# (hire_answerer/deliver_answer, gate, relabel, hold, nudge) are deliberately ABSENT — acting on the
# loop, or the machine recovering, must never feel like breaking a streak.
_INCIDENT_ACTS = {"park", "autofix_failed", "runner_down"}
_INCIDENT_EVENT_TYPES = {"autofix_failed", "runner_down"}


def _is_landing(rec):
    return rec.get("act") == "merge" and rec.get("outcome") == "ok"


def _is_incident(rec):
    act = rec.get("act")
    if act in _INCIDENT_ACTS:
        return True
    if act == "event":
        ev = rec.get("event")
        if isinstance(ev, dict) and ev.get("type") in _INCIDENT_EVENT_TYPES:
            return True
    return False


def incident_stats(journal):
    """``{total_landings, landings_since_incident, last_incident_ts}`` for the incident sign
    (design record §7). ``total_landings`` counts every successful ``merge``;
    ``landings_since_incident`` counts only those AFTER the most recent machine-side failure (so the
    sign resets on a stumble, even one William later resolved — resolution is an owner gate and never
    repaints it). No incident ever ⇒ the two counts are equal and ``last_incident_ts`` is ``None``.
    The journal is walked in file order (append-only ⇒ time order)."""
    total = since = 0
    last_incident_ts = None
    for r in journal:
        if not isinstance(r, dict):
            continue
        if _is_incident(r):
            since = 0                       # the sign resets — start counting landings afresh
            last_incident_ts = r.get("ts")
        elif _is_landing(r):
            total += 1
            since += 1
    return {"total_landings": total, "landings_since_incident": since,
            "last_incident_ts": last_incident_ts}


# =============================== corner counter — outcome-only (§7 / kill list #4) ===============================

def corner_stats(journal, now=None, window_days=7):
    """The feel-good quantity read (design record §7): OUTCOME facts only, aggregated fresh from the
    append-only journal (nothing stored ⇒ absence-proof). ``landings_total`` / ``landings_window``
    (successful merges — i.e. issues closed by landing — all-time and within the last ``window_days``),
    ``go_arounds`` (conflict regenerations), ``parks``. Only the weekly window reads the clock (as
    "this week" must); the totals are a pure function of the journal — ``now`` defaults to the wall
    clock the same way ``readers.read_state_home`` does, and is injected in tests.

    Known MVP data gap (design record §9): lines added/removed per landing are NOT summed here —
    the diff-stat cargo lives in a lane worktree that is cleaned up once the flight lands, so a
    post-merge line total isn't honestly available yet. The server may fold LIVE cargo for in-flight
    totals if it wants a running figure; it is not invented here. The standing audit is permanent:
    NO human-latency stat — time-to-approve, needs-you dwell, decisions/day — may EVER be added
    (kill list #4)."""
    now = time.time() if now is None else now
    cutoff = now - window_days * 86400
    landings_total = landings_window = go_arounds = parks = 0
    for r in journal:
        if not isinstance(r, dict):
            continue
        act = r.get("act")
        if _is_landing(r):
            landings_total += 1
            ts = r.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= cutoff:
                landings_window += 1
        elif act == "regenerate":
            go_arounds += 1
        elif act == "park":
            parks += 1
    return {"landings_total": landings_total, "landings_window": landings_window,
            "go_arounds": go_arounds, "parks": parks}


# =============================== global pill + tower status (§4) ===============================
# Each condition a repo can be in, ranked by how loudly it should pull the eye. The pill and the
# airfield's tower FX read the same scale, so the global dot and the field agree. Rank ≥ _ALERT_RANK
# is a factory-stop (red/"alert"); any lower non-zero rank is amber ("attention"); zero is "ok".
_ALERT_RANK = 90
_CONDITION_RANK = {
    "runner-down": 100,    # the dead-man's switch: no runner ⇒ the surface can't be trusted (§6)
    "alert": 90,           # an ALERT file is present (a factory-stop the runner declared)
    AWAITING: 50,          # an owner decision blocks work — the most actionable attention state
    PARKED: 45,            # the machine gave up on a flight
    SESSION_FROZEN: 30,    # a dead session stalled on the field
    STRANDED: 28,          # finished work the gate never landed — a nudge fixes it (issue #22); real
                           #   attention, but the work is safe, so it sits just under a dead session
    "spinning": 25,        # alive-looking but getting nowhere
    MERGES_FREEZE: 20,     # landings calmly paused — the designed safe idle state (lowest attention)
}


# State-home format versions this build of the dashboard knows how to read (issue #45). It reads a
# state home field-by-field and every reader fails CLOSED to empty, so an engine that changes the
# on-disk SHAPE would silently BLANK the field. The engine stamps the version it wrote; a stamp
# outside this set means the shape may not be the one these readers expect. ADD a version here only
# once the readers actually handle that engine's shape.
KNOWN_STATE_FORMATS = frozenset({1})


def _fmt_versions(versions):
    return "/".join("v%d" % v for v in sorted(versions))


def state_format_status(state_format):
    """Turn the raw state-format stamp fact (``readers`` → ``facts["state_format"]``) into the honest
    verdict the field binds (issue #45). ``state_format`` is ``None`` (no stamp — a pre-handshake
    home), the parsed dict (e.g. ``{"version": 1}``), or ``{}`` (present but corrupt).

    Returns ``{present, compatible, version, supported, message}``. ``message`` (the one honest line
    NAMING the mismatch) is built HERE — server-side, design record B.1 — because it names the
    versions; it is ``None`` when compatible. The three cases:

      * ``None`` ⇒ GRANDFATHERED. An old runner never stamped a version, and its shape is the one
        these readers were built for, so it renders normally — a missing stamp must never itself
        blank the field.
      * a version in ``KNOWN_STATE_FORMATS`` ⇒ compatible, silent.
      * any other version, or a present-but-unreadable stamp ⇒ INCOMPATIBLE. The field shows the
        named mismatch instead of a silently blank surface — the whole point of the handshake.
    """
    supported = sorted(KNOWN_STATE_FORMATS)
    if state_format is None:
        return {"present": False, "compatible": True, "version": None,
                "supported": supported, "message": None}
    version = state_format.get("version")
    # bool is an int subclass, so a bare isinstance(version, int) would ACCEPT True/False — reject it
    # (and any non-int) as an unreadable version we can't compare.
    if isinstance(version, bool) or not isinstance(version, int):
        version = None
    if version is not None and version in KNOWN_STATE_FORMATS:
        return {"present": True, "compatible": True, "version": version,
                "supported": supported, "message": None}
    wrote = ("state format v%d" % version) if version is not None else "an unreadable state format"
    message = ("the runner wrote %s — this command-center reads %s"
               % (wrote, _fmt_versions(supported)))
    return {"present": True, "compatible": False, "version": version,
            "supported": supported, "message": message}


def repo_state(slug, states, spinning=False, merges_frozen=None, alert=None,
               heartbeat_age=None, heartbeat_down_seconds=300):
    """One repo's worst condition, for the pill. ``states`` is the list of that repo's flights'
    primary states (from ``flight_stage``); ``spinning`` is whether any flight is spinning;
    ``merges_frozen`` / ``alert`` are the repo's freeze/ALERT facts (``None`` ⇒ absent);
    ``heartbeat_age`` is the runner-heartbeat age (``None`` ⇒ never written). Returns ``{slug, level,
    state, rank}`` naming the single worst condition — ``ok`` when nothing is wrong. A stale OR
    absent heartbeat is ``runner-down`` (a dead-man's switch can't fail open)."""
    conditions = []
    if heartbeat_age is None or heartbeat_age > heartbeat_down_seconds:
        conditions.append("runner-down")
    if alert is not None:
        conditions.append("alert")
    seen = set(states)
    for st in (AWAITING, PARKED, SESSION_FROZEN, STRANDED):
        if st in seen:
            conditions.append(st)
    if spinning:
        conditions.append("spinning")
    if merges_frozen is not None:
        conditions.append(MERGES_FREEZE)

    if not conditions:
        return {"slug": slug, "level": "ok", "state": "ok", "rank": 0}
    worst = max(conditions, key=lambda c: _CONDITION_RANK[c])
    rank = _CONDITION_RANK[worst]
    level = "alert" if rank >= _ALERT_RANK else "attention"
    return {"slug": slug, "level": level, "state": worst, "rank": rank}


def global_pill(repo_states):
    """The one global dot (design record §4): the WORST condition across every repo, naming the
    offending repo. All clear ⇒ ``{level: ok, state: ok, offender: None}``. ``repo_states`` are
    ``repo_state`` results; ties resolve to the first (deterministic given input order)."""
    worst = None
    for r in repo_states:
        if worst is None or r["rank"] > worst["rank"]:
            worst = r
    if worst is None or worst["rank"] == 0:
        return {"level": "ok", "state": "ok", "offender": None}
    return {"level": worst["level"], "state": worst["state"], "offender": worst["slug"]}


def tower_status(pill):
    """The airfield's tower FX tier (``ok`` / ``attention`` / ``alert``) — the pill's own level, so
    the field and the global dot never disagree."""
    return pill["level"]


# =============================== queue order — the departures board (§3) ===============================
# Which flight leaves the stand next is a state diagram, not a UI concern: it is a PURE function of
# eligibility, the ⚡ expedite label, the priority band, and the blocked-by connections an issue
# declares in its body. Computed here (design record B.1) so the split-flap board only PAINTS the
# order — delete the flaps and this list is still the correct launch sequence (the squint test).
#
# Priority bands. The runner's own launch order is FIFO within a band; the dashboard reflects it,
# it does not invent it. The vocabulary is a small fixed set — a `priority:<band>` label maps to a
# rank where SMALLER is more urgent; a bare `priority:<n>` numeric label rides the same axis
# (``priority:0`` most urgent); absent or unrecognized ⇒ the middle band (``normal``). If the loop
# ever adopts a different priority vocabulary, extend this one map — nothing else changes.
_PRIORITY_RANK = {"high": 0, "normal": 1, "medium": 1, "low": 2}
_DEFAULT_PRIORITY_BAND = "normal"
_EXPEDITE_LABEL = "expedite"
_PRIORITY_PREFIX = "priority:"

# blocked-by connections live in the issue body's Loop metadata (NOT a label): a `blocked-by:` line
# carrying one or more `#<n>` issue refs. Matched case-insensitively; only refs ON that line count,
# so a stray "#9" elsewhere in the body is never mistaken for a connection.
_BLOCKED_BY_LINE = re.compile(r"^\s*blocked-by\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
_ISSUE_REF = re.compile(r"#(\d+)")


def label_names(labels):
    """Normalize a gh label list — either ``[{"name": "x"}, …]`` or bare ``["x", …]`` — to a set of
    name strings. Tolerant of junk entries (a non-dict/str is skipped), so a half-read issue never
    raises here."""
    out = set()
    for lb in labels or []:
        if isinstance(lb, dict):
            n = lb.get("name")
            if isinstance(n, str):
                out.add(n)
        elif isinstance(lb, str):
            out.add(lb)
    return out


def has_label(labels, name):
    """Whether ``name`` is present in a gh label list (dict or string shaped)."""
    return name in label_names(labels)


def _priority_pairs(labels):
    """Every ``(rank, band)`` a flight's ``priority:*`` labels imply. Scanning ALL of them (not the
    first the set happens to yield) makes the result deterministic across processes — Python's set
    iteration order is hash-seeded, so "return the first priority label" would flap run to run."""
    pairs = []
    for n in label_names(labels):
        if n.lower().startswith(_PRIORITY_PREFIX):
            band = n.split(":", 1)[1].strip().lower()
            if band in _PRIORITY_RANK:
                pairs.append((_PRIORITY_RANK[band], band))
            elif band.isdigit():
                pairs.append((int(band), band))
    return pairs


def priority_band(labels):
    """The named priority band from a ``priority:<band>`` label — ``high`` / ``normal`` / ``low`` —
    or ``normal`` when absent or unrecognized (the honest middle: an unlabeled flight is neither
    rushed nor deferred). With several priority labels, the MOST URGENT (lowest rank) wins,
    deterministically."""
    pairs = _priority_pairs(labels)
    if not pairs:
        return _DEFAULT_PRIORITY_BAND
    return min(pairs, key=lambda rb: (rb[0], rb[1]))[1]


def priority_rank(labels):
    """The numeric launch-urgency rank (SMALLER = leaves sooner) for a flight's ``priority:*`` label.
    Named bands map through ``_PRIORITY_RANK``; a bare numeric ``priority:<n>`` rides the same axis
    (``priority:0`` most urgent); absent/unknown ⇒ the middle band. Several priority labels resolve
    to the most urgent (minimum) rank, deterministically. Expedite is a SEPARATE, stronger signal
    handled by :func:`queue_rows`, never folded in here."""
    pairs = _priority_pairs(labels)
    return min(r for r, _ in pairs) if pairs else _PRIORITY_RANK[_DEFAULT_PRIORITY_BAND]


def parse_blocked_by(body):
    """The issue numbers this flight is blocked-by, read from the ``blocked-by:`` line of the issue
    body's Loop metadata (design record §3 — a connection, NOT a label). Returns the distinct refs
    sorted ascending; ``[]`` when the line is absent or the body is empty/``None``. Only ``#<n>``
    refs on the ``blocked-by`` line itself are counted."""
    if not isinstance(body, str) or not body:
        return []
    nums = set()
    for m in _BLOCKED_BY_LINE.finditer(body):
        for ref in _ISSUE_REF.findall(m.group(1)):
            nums.add(int(ref))
    return sorted(nums)


def _queue_status_text(launchable, blocked_by, pos):
    """The plain split-flap STATUS phrase for a queue row (real words, costume rule 2): an unmet
    connection reads ``AWAITING CONNECTION SL-N`` (never in the air); the flight at the front of the
    launch order is ``NEXT OFF THE STAND``; everything else is plainly ``QUEUED``."""
    if not launchable:
        return "AWAITING CONNECTION SL-%d" % blocked_by
    if pos == 1:
        return "NEXT OFF THE STAND"
    return "QUEUED"


def queue_rows(candidates, satisfied):
    """The ordered departures/launch queue (design record §3): ⚡ expedite on top, then the priority
    band, then issue number ascending (FIFO within a band). A candidate whose ``blocked-by``
    connection has NOT arrived is not launchable — it is shown ``awaiting connection SL-N`` and sinks
    below every launchable flight, NEVER in the air; ⚡ cannot override an unmet connection.

    ``candidates`` are the eligible issues (open, ``agent-ready``, not already flying) in the gh
    shape ``{num, title, labels, body}``. ``satisfied(n)`` is a predicate the caller supplies —
    ``True`` only with POSITIVE proof that blocker issue ``n`` has landed/closed (its connection has
    arrived). It MUST fail closed: an unknown/unreadable blocker stays a blocker, so a flight is
    never shown in the air on a hopeful guess. Returns a list of row dicts; the JS binds them to
    flaps and computes nothing."""
    rows = []
    for c in candidates:
        num = c.get("num")
        labels = c.get("labels")
        blockers = parse_blocked_by(c.get("body"))
        unmet = [n for n in blockers if not satisfied(n)]
        blocked_by = unmet[0] if unmet else None          # the first connection still to arrive
        launchable = blocked_by is None
        expedited = has_label(labels, _EXPEDITE_LABEL) and launchable  # ⚡ never lifts a blocked flight
        rows.append({
            "num": num,
            "flight": "SL-%d" % num,
            "destination": c.get("title") or "",
            "expedited": expedited,
            "priority": priority_band(labels),
            "priority_rank": priority_rank(labels),
            "blocked_by": blocked_by,
            "launchable": launchable,
        })

    # Launchable flights first (⚡, then band, then FIFO by number); blocked flights after, in the
    # same band/number order. A stable key means the order is fully determined by the facts.
    rows.sort(key=lambda r: (not r["launchable"], not r["expedited"],
                             r["priority_rank"], r["num"]))

    pos = 0
    for r in rows:
        if r["launchable"]:
            pos += 1
            r["pos"] = pos
            r["status"] = "expedited" if r["expedited"] else "queued"
        else:
            r["pos"] = None
            r["status"] = "awaiting"
        r["status_text"] = _queue_status_text(r["launchable"], r["blocked_by"], r["pos"])
    return rows


# =============================== arrivals board — bounded backlog (§3 / §7) ===============================
# The split-flap arrivals board pages through history, but never unboundedly: the backlog is the
# smaller of a few pages or a few days of landings, and older entries drop off (owner amendment
# 2026-07-07). This is pure semantics (design record B.1) — the JS only paginates + flutters what
# this returns. ``page_size`` is the semantic rows-per-page and MUST equal the Solari board's
# ``MAX_ROWS`` (a cross-file guard pins the two together), so "``max_pages`` pages" server-side and
# the client's page count agree.

def cap_arrivals(rows, now, page_size=5, max_pages=5, max_age_days=3):
    """The bounded, newest-first arrivals backlog. Two caps compose: landings older than
    ``max_age_days`` drop off the board entirely, and of what remains only the newest
    ``page_size * max_pages`` survive — whichever cap bites first wins ("the smaller of N pages or M
    days"). ``rows`` are arrival dicts each carrying a ``ts`` (merge epoch seconds); order need not be
    sorted — this returns them newest-first.

    Honesty on unprovable recency (§7): a landing whose ``ts`` is missing/non-finite (a status-merged
    flight with no journal merge proof) is NOT dropped by the age filter — the board still carries a
    real landing it merely can't prove fresh — it only sorts to the bottom (oldest), so it falls off
    only when genuinely newer landings crowd it past the page cap. The 3-day edge is inclusive: a
    landing exactly ``max_age_days`` old is still within the window."""
    cutoff = now - max_age_days * 86400
    kept = [r for r in rows if not (_finite_ts(r.get("ts")) and r["ts"] < cutoff)]
    kept.sort(key=lambda r: r["ts"] if _finite_ts(r.get("ts")) else float("-inf"), reverse=True)
    return kept[: page_size * max_pages]


def _finite_ts(v):
    """A real, finite numeric ts (never a bool, never NaN/Infinity — a corrupt journal ts can be a
    non-finite float that ``json.loads`` accepts). Non-finite ⇒ treated as unknown recency."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


# =============================== build_flight — facts → an honest flight ===============================
_EMPTY_CARGO = {"present": False, "files": 0, "added": 0, "removed": 0}


def _is_count(v):
    """A real, non-negative integer count (never a bool, never a float/None) — the shape gh reports
    for a PR's additions/deletions/changedFiles."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def cargo_from_pr(pr_facts):
    """The cargo (size, never risk) a PR carries in its OWN diff-stat — ``{present, files, added,
    removed}`` — or ``None`` when the PR offers no honest size. This is the TOP of the cargo
    precedence (issue #48, absorbing #47): a landed flight's lane worktree is cleaned up, but its PR
    remembers ``additions``/``deletions``/``changedFiles`` forever, so cargo survives landing —
    retroactively, for every flight that ever flew.

    ``None`` (never a fabricated ``+0/−0``) whenever the size can't be read honestly: no PR / a
    fail-closed empty read (``{}``), the size fields absent (an older/partial read), or a
    ``changedFiles`` that isn't a real count of at least one file. A PR that touched at least one
    file but moved zero lines (a pure rename, a binary) is a real — if quiet — size and IS reported.
    ``None`` hands the decision down to the live worktree diff, then to honest absence."""
    if not isinstance(pr_facts, dict) or not pr_facts:
        return None
    added, removed, files = (pr_facts.get("additions"), pr_facts.get("deletions"),
                             pr_facts.get("changedFiles"))
    if not (_is_count(added) and _is_count(removed) and _is_count(files) and files >= 1):
        return None
    return {"present": True, "files": files, "added": added, "removed": removed}
_DEFAULT_PROGRESS_WINDOW = 900   # seconds of journal history the progress heuristic looks back over
# Statuses that mean the session already left the stand (so a flight with no launch event journaled
# is still known to have launched).
_LAUNCHED_STATUSES = {"running", "blocked", "frozen", "exited", "gating", "holding", "merged"}


def _windowed(journal, now, window_s):
    """This issue's records within the last ``window_s`` seconds of ``now`` — the rolling window the
    progress heuristic reads. A record with no ``ts`` is kept (we can't prove it's stale). With no
    ``now``, the whole journal is the window."""
    if now is None:
        return list(journal)
    cutoff = now - window_s
    out = []
    for r in journal:
        if not isinstance(r, dict):
            continue
        ts = r.get("ts")
        if ts is None or (isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= cutoff):
            out.append(r)
    return out


def _flight_memo(journal, blocked_txt, bounced):
    """The plain memo a Needs You card leads with: a ``BOUNCED:`` marker's text for a bounce, else
    the most recent ``park`` event's memo (which also carries the needs-william case). Radio-call
    questions are NOT memos — those are tower-log material, handled elsewhere."""
    if bounced and blocked_txt:
        return blocked_txt
    memo = None
    for r in journal:
        if isinstance(r, dict) and r.get("act") == "park" and r.get("memo"):
            memo = r.get("memo")   # last park wins
    return memo


def build_flight(issue, repo):
    """Compose one issue's facts into an honest flight object — the whole truth layer, assembled.

    ``issue`` is the per-flight facts the server gathers (id/num/status/branch/pr from ``issues.json``
    + ``gh``; ``activity_mtime``; the ``blocked``/``awaiting_marker`` markers; ``report_present`` /
    ``review_present``; this flight's ``journal`` slice; ``pr_facts``; ``cargo`` + ``diff_delta``).
    ``repo`` carries the repo's thresholds, ``required_checks``, ``now``, ``merges_frozen`` and the
    optional ``progress_window_seconds``. Every derived field routes through the tested functions
    above, so the object is exactly the state diagram — nothing computed in the pixels downstream."""
    num = issue.get("num")
    status = issue.get("status")
    journal = issue.get("journal") or []
    now = repo.get("now")
    activity_mtime = issue.get("activity_mtime")

    attempt = attempt_number(journal)
    live = liveness_tier(activity_mtime, now, repo.get("idle_seconds", 480),
                         repo.get("freeze_seconds", 2700))

    session_started = activity_mtime is not None
    launched = (session_started or status in _LAUNCHED_STATUSES
                or any(isinstance(r, dict) and r.get("act") == "launch" for r in journal))

    blocked_txt = issue.get("blocked")
    bounced = isinstance(blocked_txt, str) and blocked_txt.lstrip().startswith("BOUNCED:")
    long_wait = bool(issue.get("awaiting_marker"))

    gate = gate_checklist(issue.get("pr_facts") or {}, issue.get("report_present"),
                          issue.get("review_present"), repo.get("required_checks") or [])

    stage = flight_stage(status, liveness=live, bounced=bounced, long_wait=long_wait,
                         report_present=bool(issue.get("report_present")),
                         session_started=session_started, launched=launched,
                         cleared=gate["cleared"], closed=bool(issue.get("closed")))
    # The underlying on-circuit position, kept even when an off-path state overrides the primary
    # stage — the field renders the amber ring / grey frozen plane AT its honest position (§5),
    # never teleported to a magic fix.
    stage_on_circuit = circuit_stage(status, report_present=bool(issue.get("report_present")),
                                     session_started=session_started, launched=launched,
                                     cleared=gate["cleared"], closed=bool(issue.get("closed")))
    # Grey/no-contrail is reserved for liveness failure (§5): a frozen SESSION never trails, even
    # if its activity file is paradoxically fresh.
    contrail = "none" if stage == SESSION_FROZEN else contrail_kind(
        activity_mtime, now, repo.get("idle_seconds", 480), repo.get("freeze_seconds", 2700))

    window = _windowed(journal, now, repo.get("progress_window_seconds", _DEFAULT_PROGRESS_WINDOW))
    prog = progress(window, issue.get("diff_delta"))
    spinning = is_spinning(live, prog)

    wander = merged_wandered(journal)
    merged = status == "merged" or stage in (TOUCHDOWN, TAXI_IN)
    celebrate = landed_clean(journal)      # a flourish is EARNED by a proven clean landing (§7),
                                           # never granted by the mere absence of a wander flag

    awaiting_reason = None
    if stage == AWAITING:
        awaiting_reason = "bounced" if (status == "bounced" or bounced) else "needs-william"

    return {
        "id": issue.get("id"),
        "num": num,
        "label": flight_label(num, attempt),
        "attempt": attempt,
        "stage": stage,
        "on_circuit": stage in CIRCUIT_STAGES,
        "circuit_stage": stage_on_circuit,
        "contrail": contrail,
        "liveness": live,
        "progress": prog,
        "spinning": spinning,
        "wander": wander,
        "merged": merged,
        "celebrate": celebrate,
        "gate": gate,
        # Cargo precedence (issue #48): the PR's own diff size first — so a landed flight's cargo
        # survives its cleaned-up worktree — then the live worktree diff, then honest absence.
        "cargo": cargo_from_pr(issue.get("pr_facts")) or issue.get("cargo") or dict(_EMPTY_CARGO),
        "branch": issue.get("branch"),
        "pr": issue.get("pr"),
        "awaiting_reason": awaiting_reason,
        "long_wait": long_wait,
        "memo": _flight_memo(journal, blocked_txt, bounced),
        "landings_paused": repo.get("merges_frozen") is not None,
    }
