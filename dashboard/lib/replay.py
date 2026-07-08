"""Night replay (Task 11 / design record §4) — a journal time-window becomes ordered field frames.

The replay is a beloved **treat, never load-bearing** (§0.1 owner ruling, §4): it plays back any
window of the append-only ``journal.jsonl`` as a scrubbable, steppable time-lapse over the SAME
airfield engine the live field uses, every frame clickable through to the raw journal record that
produced it. It answers "watch what the field did" — never "what do I need to do now" (that is the
mechanical digest's job, and the live Needs You panel's).

Everything here is a PURE function of a journal (a list of records) plus an injected ``hhmm``
formatter — no wall clock, no I/O, no ``gh`` — so the whole reconstruction is unit-tested to the
line. The load-bearing disciplines:

* **Frames run forward in time (§4).** The window is sorted by ``ts``; a record with no usable ts
  (a corrupt ``NaN``, a half-written line) can't be placed on a timeline and is dropped — the movie
  never jumps.
* **Position is DISCRETE and derived through the truth layer (§3, B.1).** Each flight's stage at a
  frame comes from :func:`flights.flight_stage` fed by a tiny per-flight fact accumulator walked
  from journal landmarks — the SAME function the live field derives its stage from. The replay is
  the state diagram in motion, not a second model that could drift (the squint test). A plane only
  moves when a real journal event changed its stage, and that transit IS the event you are watching.
* **The living clock is honest per frame (§7).** Each frame's lighting (``daypart``) is the wall
  clock at THAT event's real ts — a 2 a.m. park replays under night lighting — never "now".
* **Every frame is a click target (§4).** It carries the offending flight ``num``, the glossed
  comms sentence (via the tested :mod:`tower` vocabulary), and the exact ``raw`` journal line, so a
  tap on any frame opens that flight's drawer or expands ground truth.
"""
import json
import math
import time

import flights
import tower


# Air working stages whose planes trail a contrail in the replay (a treat has no liveness axis, so
# the contrail is a plain "is this plane flying a working leg" flag — the engine only draws trails
# for air anchors anyway). A grey session-frozen / amber awaiting hull never trails (§5).
_TRAILING = {flights.TAKEOFF, flights.DOWNWIND, flights.BASE_TURN, flights.FINAL, flights.HOLDING}

# The most frames a single replay ships by default — a localhost treat, but an unbounded journal
# should never balloon the payload. When the window is bigger, the MOST RECENT frames are kept (the
# recent past is what you replay) and the window is flagged truncated so the client can say so.
_DEFAULT_MAX_FRAMES = 600


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _local_hhmm(ts):
    """Local wall-clock ``HH:MM`` for a ts, or ``""`` when unusable — the same guarded formatting
    as the server's, so a lib-only caller still gets sensible times. Injectable (the server passes
    its own; tests pass a deterministic stub) so timezone never leaks into an assertion."""
    if not _finite(ts):
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return ""


def _daypart(ts):
    """The frame's lighting for a ts — :func:`flights.daypart`, but guarded. A corrupt-but-FINITE
    ts (a huge or negative epoch that slips past the ``NaN``/``Infinity`` screen) overflows
    ``time.localtime``; the replay must never 500 on one bad journal line (the derivations-never-raise
    rule), so unusable ts fall back to plain ``day`` lighting."""
    try:
        return flights.daypart(ts)
    except (ValueError, OSError, OverflowError):
        return "day"


# =============================== per-flight fact accumulator ===============================
# The replay reconstructs each flight's DISCRETE facts by walking its journal landmarks in time
# order, then hands them to the tested flights.flight_stage — never inventing a parallel stage map.

def _new_facts():
    """A flight's blank slate before its first event."""
    return {"status": None, "launched": False, "session_started": False,
            "report": False, "cleared": False, "closed": False, "attempt": 1}


def _apply(facts, rec):
    """Fold one journal record into a flight's facts. Only records that move the DISCRETE position
    change anything; flavor acts (nudge, notify, relabel, answers) leave the stage exactly as it
    was — the plane holds its anchor, honest to the journal."""
    act = rec.get("act")
    if act == "launch":
        facts["launched"] = True
        facts["session_started"] = False       # just off the stand — a brief takeoff before the leg
        facts["status"] = "running"
    elif act == "regenerate":
        # A go-around is an honest retire-and-rebuild (§3): a NEW attempt taxis out from the start.
        facts["attempt"] += 1
        facts["launched"] = True
        facts["session_started"] = False
        facts["report"] = False
        facts["cleared"] = False
        facts["status"] = "running"
    elif act == "gate":
        if rec.get("outcome") == "ok":
            facts["cleared"] = True
            facts["status"] = "gating"
        # a held/failed gate is NOT a pass — the plane stays at its report/base position
    elif act == "merge":
        if rec.get("outcome") == "ok":
            facts["status"] = "merged"          # the landing; a missed approach (failed merge) holds
    elif act == "park":
        # park carries the whole "machine gave up" family; needs_william is the amber owner-decision.
        facts["status"] = "needs_william" if rec.get("needs_william") else "parked"
    elif act == "bounce":
        facts["status"] = "bounced"
    elif act == "hold":
        facts["status"] = "holding"
    elif act == "drop":
        facts["closed"] = True
    elif act == "event":
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        et = ev.get("type")
        if et == "session_finished":
            facts["report"] = True
            facts["session_started"] = True
            facts["status"] = "running"
        elif et == "frozen":
            facts["status"] = "frozen"
        elif et == "exited":
            facts["status"] = "exited"
        elif et == "session_blocked":
            facts["session_started"] = True
            facts["status"] = "blocked"
    # Any post-launch record for a live flight proves the session is doing work → on the leg, not
    # merely just-airborne. (Applied after the specific handlers so a session_finished still wins
    # its BASE_TURN via report=True.)
    if act not in ("launch", "regenerate") and facts["launched"]:
        facts["session_started"] = True


def _stage(facts):
    launched = facts["launched"]
    return flights.flight_stage(
        facts["status"], report_present=facts["report"],
        session_started=facts["session_started"] and launched, launched=launched,
        cleared=facts["cleared"], closed=facts["closed"])


def _circuit_stage(facts):
    launched = facts["launched"]
    return flights.circuit_stage(
        facts["status"], report_present=facts["report"],
        session_started=facts["session_started"] and launched, launched=launched,
        cleared=facts["cleared"], closed=facts["closed"])


_OFF_PATH = (flights.PARKED, flights.AWAITING, flights.SESSION_FROZEN, flights.HOLDING)


def _flight_view(num, facts, tail):
    """One flight's frame object — the engine's model shape (design B.1: the pixels compute
    nothing), keyed exactly like the live field's so the replay binder maps them the same way."""
    stage = _stage(facts)
    contrail = "crisp" if stage in _TRAILING else "none"
    return {
        "num": num,
        "label": flights.flight_label(num, facts["attempt"]),
        "stage": stage,
        "circuit_stage": _circuit_stage(facts),
        "runway": num % 2,                     # no lane info in the journal — a stable parity runway
        "contrail": contrail,
        "spinning": False,                     # replay has no liveness axis — a treat, not a monitor
        "trouble": False,                      # no alarm dimming in the time-lapse; it stays clean
        "tail": tail,
    }


def _rec_num(rec):
    """The flight a record is about — its ``num``, else its ``id`` (``i23`` → 23), else an
    ``event.id`` envelope; ``None`` for a repo-wide record (a notify, an alert)."""
    n = rec.get("num")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    for src in (rec.get("id"), (rec.get("event") or {}).get("id")
                if isinstance(rec.get("event"), dict) else None):
        if isinstance(src, str) and src.startswith("i") and src[1:].isdigit():
            return int(src[1:])
    return None


def _valid_upto(journal, end):
    """Every valid record with a usable ts up to ``end`` (inclusive; ``None`` = unbounded), sorted
    ascending. A corrupt/absent ts can't sit on a timeline and is dropped, so the movie is always
    monotonic; non-dict lines are skipped (same rule as the tolerant journal reader). Records BEFORE
    the window's start are kept here on purpose — they carry the state the field already had when the
    window opened (see :func:`build_replay`)."""
    out = []
    for rec in journal:
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if not _finite(ts):
            continue
        if end is not None and ts > end:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("ts"))
    return out


def build_replay(journal, *, slug="", name="", start=None, end=None,
                 hhmm=None, max_frames=_DEFAULT_MAX_FRAMES):
    """A journal (list of records) → the replay document the front-end scrubs.

    ``start``/``end`` (epoch seconds, inclusive, either ``None`` for unbounded) bound the window;
    ``hhmm`` formats a ts to the viewer's local ``HH:MM`` (injected so timezone never leaks into the
    lib); ``max_frames`` caps the payload, keeping the MOST RECENT frames and flagging truncation.

    The reconstruction walks EVERY valid record up to ``end`` so a windowed replay inherits the state
    the field already had when the window opened — a flight that launched an hour before a "last 6h"
    window is still in the air, not magically back at the stand. A FRAME is emitted only for records
    inside ``[start, end]``; earlier records are pre-roll that build state silently.

    Returns ``{slug, name, empty, window, frames}``. Each frame is one journal record applied to the
    cumulative reconstruction: ``{i, ts, hhmm, daypart, status, num, kind, radio, text, raw,
    flights: [...]}`` — the flights are every flight seen through this record, each at its discrete
    reconstructed stage, in the engine's model shape. ``num``/``raw`` make the frame a click target
    to its event."""
    hhmm = _local_hhmm if hhmm is None else hhmm
    tail = flights.airline_color(slug) if slug else None

    facts_by_num = {}
    order = []                                  # first-seen order, kept stable for the field list
    frames = []
    for rec in _valid_upto(journal, end):
        num = _rec_num(rec)
        if num is not None:
            if num not in facts_by_num:
                facts_by_num[num] = _new_facts()
                order.append(num)
            _apply(facts_by_num[num], rec)

        ts = rec.get("ts")
        if start is not None and ts < start:
            continue                            # pre-roll: accumulate state, emit no frame yet

        flight_views = [_flight_view(n, facts_by_num[n], tail) for n in order]
        status = "attention" if any(f["stage"] in _OFF_PATH for f in flight_views) else "ok"

        gloss = tower.comms_row(rec)
        frames.append({
            "ts": ts,
            "hhmm": hhmm(ts),
            "daypart": _daypart(ts),            # the living clock at the event's OWN time (§7), guarded
            "status": status,
            "num": num,
            "kind": gloss["kind"],
            "radio": gloss["radio"],
            "text": gloss["text"],
            "raw": json.dumps(rec, separators=(",", ":")),
            "flights": flight_views,
        })

    truncated = len(frames) > max_frames
    if truncated:
        frames = frames[-max_frames:]           # the recent past is what you replay
    for i, fr in enumerate(frames):
        fr["i"] = i

    return {
        "slug": slug,
        "name": name or slug,
        "empty": len(frames) == 0,
        "window": {
            "start": frames[0]["ts"] if frames else start,
            "end": frames[-1]["ts"] if frames else end,
            "frames": len(frames),
            "truncated": truncated,
        },
        "frames": frames,
    }
