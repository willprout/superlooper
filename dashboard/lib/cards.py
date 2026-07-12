"""Needs You cards + the flight-card drawer (Task 9 / design record §2–§4).

These are the two decision surfaces. Both obey costume rule 2 (§3): **the plain-language gloss
leads; the literal term is secondary, for the vet on hover.** A Needs You card is a decision waiting
on William — a plain headline, a gloss, the raw memo, and (for the conflict-cap case) one plain
sentence naming the collision, with **Discuss highlighted as the default** (§8 open risk: guard
against blind Approve presses there). The drawer is ground truth one click from anywhere: title,
circuit rail, the clearance checklist under its REAL check names (§3), issue/PR/branch links, memo
history, the size-not-risk cargo chip (§3), that flight's glossed journal slice, and the go-around
counter.

All of it is pure over an already-built ``flights.build_flight`` object and its journal slice — the
gloss/mapping is here and tested, the JS binds strings it never derives (design record B.1). The
journal slice is glossed through the shared, already-tested :mod:`tower` vocabulary so the drawer
and the tower log can never drift.
"""
import json

import flights
import tower

_GH = "https://github.com"


# =============================== the shared plain-language vocabulary (costume rule 2) ===============================
# The clearance checklist's four REAL, fixed check names (§3), each with the plain gloss the card
# LEADS with — "mergeable = fits cleanly onto today's code" (the design record's own example).
GATE_GLOSS = {
    "report": ("report filed", "a written report for this issue exists"),
    "review": ("independently reviewed", "an agent that didn't write the code checked it"),
    "ci": ("checks green", "the required automated checks all passed"),
    "mergeable": ("fits cleanly", "fits cleanly onto today's code, with no conflict"),
}
_GATE_ORDER = ("report", "review", "ci", "mergeable")

# The circuit stages, developer-term FIRST (costume rule 2 / joy-pass owner ruling 2026-07-07): the
# ground-truth drawer's rail leads with the real state name (``dev``); the airport metaphor
# (``flavor``) is the secondary skin; ``desc`` is the fuller plain-language detail for the hover. The
# gate step (``final``) names the four real checks so its detail matches the true mechanics.
STAGE_GLOSS = {
    flights.AT_STAND: ("queued", "at the stand", "approved, waiting for a free runway"),
    flights.TAXI_OUT: ("launching", "taxiing out", "launching — delivery-verification in progress"),
    flights.TAKEOFF: ("session started", "takeoff", "the build session has started"),
    flights.DOWNWIND: ("building", "downwind", "building — the long working leg"),
    flights.BASE_TURN: ("report filed", "base turn", "report filed, heading for the gate"),
    flights.FINAL: ("gate checks", "final",
                    "the gate — report, review, CI, mergeable; cleared to merge when all green"),
    flights.TOUCHDOWN: ("merged", "touchdown", "merged"),
    flights.TAXI_IN: ("closed", "taxi in", "closed and cleaned up"),
}

# The off-path states in plain words (§5 — each demands an opposite response, so each reads distinct).
OFF_PATH_PLAIN = {
    flights.PARKED: "parked — the machine gave up; nothing was lost, it needs your call",
    flights.AWAITING: "awaiting your decision — amber, a person is needed before it moves",
    flights.HOLDING: "holding — number 2 for landing, sequenced behind an overlapping lane",
    flights.SESSION_FROZEN: "session frozen — a stalled session, no contrail",
    flights.STRANDED: ("stranded at the gate — the session finished and filed its report, but the "
                       "gate never landed it; the problem is at the runner/gate, not the session"),
    flights.MERGES_FREEZE: "landings paused — a repair flight is out; builds keep flying",
}


# =============================== the card kind — the four decisions ===============================

def card_kind(flight):
    """Which of the four decision kinds a waiting flight is. A flight that went around (``attempt``
    >= 2 — a conflict regeneration happened) and STILL landed on William's desk is the ``conflict-cap``
    case, whatever its underlying stage — the go-around cap is the story that needs telling (§3).
    Otherwise: ``parked`` (the machine gave up), or an amber decision that is a ``bounced`` push-back
    or a plain ``needs-owner``."""
    if flight.get("attempt", 1) >= 2:
        return "conflict-cap"
    if flight.get("stage") == flights.PARKED:
        return "parked"
    if flight.get("awaiting_reason") == "bounced":
        return "bounced"
    return "needs-owner"


# The plain headline + leading gloss + hover term for each kind (costume rule 2).
_CARD_COPY = {
    "parked": {
        "headline": "The machine tried this and gave up — it needs your call.",
        "plain": "Parked means the automatic build stopped and is waiting for you. Nothing was lost.",
        "term": "parked", "badge": "PARKED",
    },
    "needs-owner": {
        "headline": "A decision is waiting on you before this can move.",
        "plain": "The worker paused and asked for your input to continue.",
        "term": "needs-owner", "badge": "AWAITING",
    },
    "bounced": {
        "headline": "The worker thinks the plan is off and suggested a change.",
        "plain": "A bounce is the worker pushing back with a proposed amendment — accept it to relaunch, or discuss.",
        "term": "bounced", "badge": "BOUNCED",
    },
    "conflict-cap": {
        "headline": "This kept colliding with other work and couldn't land — your call.",
        "plain": "It was rebuilt from scratch after merge conflicts and still couldn't merge cleanly, so it came to you.",
        "term": "conflict cap", "badge": "CONFLICT CAP",
    },
}


def _collision_sentence(flight):
    """The one plain sentence naming the collision on a conflict-cap card (§3: "names the collision
    in one plain sentence and offers reasoned choices, never a bare badge"). Built from real facts —
    the go-around count — so it never overclaims what happened."""
    attempt = flight.get("attempt", 1)
    go_arounds = max(0, attempt - 1)
    times = "once" if go_arounds == 1 else "%d times" % go_arounds
    return ("%s kept colliding with work that landed first — rebuilt %s, %d attempts used, and it "
            "still couldn't merge cleanly." % (flight.get("label") or ("SL-%s" % flight.get("num")),
                                               times, attempt))


def needs_you_card(flight, slug):
    """A whole-field Needs You card for a waiting flight (design record §4). Leads with a plain
    headline + gloss (the literal term is on hover), carries the raw memo, and — for the conflict-cap
    case only — one plain sentence naming the collision with ``discuss_default`` set so the client
    highlights Discuss instead of Approve (§8). ``badge_base`` is the state word; the server appends
    the exact age numeral. The buttons (Task 6) are wired by the client from ``num``/``repo``."""
    kind = card_kind(flight)
    copy = _CARD_COPY[kind]
    is_conflict = kind == "conflict-cap"
    return {
        "num": flight.get("num"),
        "flight": flight.get("label"),
        "repo": slug,
        "state": flight.get("stage"),
        "reason": flight.get("awaiting_reason"),
        "kind": kind,
        "badge_base": copy["badge"],
        "headline": copy["headline"],
        "gloss": {"plain": copy["plain"], "term": copy["term"]},
        "memo": flight.get("memo"),
        "collision": _collision_sentence(flight) if is_conflict else None,
        # Discuss is the highlighted default ONLY on the conflict-cap card — everywhere else Approve
        # leads. This is the §8 guard against a blind Approve press on a collision card.
        "discuss_default": is_conflict,
    }


# =============================== the drawer — ground truth one click away (§4) ===============================

def _circuit_rail(flight):
    """The circuit rail: every discrete stage in order, with the flight's honest position marked
    ``current`` and the stages behind it ``done`` (design record §3). The position is the flight's
    ``circuit_stage`` — kept even when an off-path state (amber/grey/parked) overrides the primary
    stage, so the rail shows where the plane really is, never teleported to a magic fix (§5)."""
    cur = flight.get("circuit_stage")
    stages = list(flights.CIRCUIT_STAGES)
    cur_idx = stages.index(cur) if cur in stages else -1
    rail = []
    for i, st in enumerate(stages):
        dev, flavor, desc = STAGE_GLOSS[st]
        # ``label`` is the developer term the rail LEADS with; ``flavor`` is the airport skin the JS
        # renders small and secondary; ``desc`` is the fuller plain detail the hover carries (costume
        # rule 2). ``term`` is the literal state id, kept in the snapshot beside ``stage``.
        rail.append({"stage": st, "label": dev, "flavor": flavor, "desc": desc, "term": st,
                     "current": i == cur_idx, "done": cur_idx >= 0 and i < cur_idx})
    return rail


def _clearance(flight):
    """The clearance checklist under the four REAL check names (§3), each leading with its plain
    gloss. ``ok`` is the honest gate reading (fail-closed upstream in ``flights.gate_checklist``)."""
    gate = flight.get("gate") or {}
    out = []
    for key in _GATE_ORDER:
        label, gloss = GATE_GLOSS[key]
        out.append({"key": key, "label": label, "gloss": gloss, "ok": bool(gate.get(key))})
    return out


def _memo_history(flight, journal_slice):
    """Every distinct memo this flight accrued, in journal order (design record §4 — "memo
    history"): each ``park`` memo, plus the flight's current memo (a bounce marker's text) when it
    isn't already the last park memo. Order preserved, duplicates collapsed."""
    memos = []
    for r in journal_slice:
        if isinstance(r, dict) and r.get("act") == "park" and r.get("memo"):
            m = r["memo"]
            if m not in memos:
                memos.append(m)
    current = flight.get("memo")
    if current and current not in memos:
        memos.append(current)
    return memos


def _cargo(flight):
    """The size-not-risk cargo chip (§3 — weight is a neutral fact, never risk): +N/−N and files,
    or an honest empty when the worktree diff isn't readable."""
    c = flight.get("cargo") or {}
    present = bool(c.get("present"))
    added, removed = int(c.get("added", 0)), int(c.get("removed", 0))
    files = c.get("files") if present else None
    chip = ("+%d/−%d" % (added, removed)) if present else "—"
    return {"present": present, "added": added, "removed": removed, "files": files, "chip": chip}


def flight_drawer(flight, journal_slice, slug, name, title=None, hhmm=None, operator="the owner"):
    """The whole flight-card drawer (design record §4) — pure over the flight + its journal slice.

    ``hhmm`` is an injected ``ts -> "HH:MM"`` formatter (the server passes its locale-aware one; the
    default yields ``""`` so the core stays clock-free and testable). ``operator`` is the configured
    operator display name (issue #58), threaded to the glossed journal so a re-approval line signs
    the owner's own name. Returns the title, circuit rail, off-path note, clearance checklist,
    issue/PR/branch links, memo history, cargo chip, the glossed journal slice (each row expandable
    to its raw line), and the go-around counter."""
    hhmm = hhmm or (lambda ts: "")
    num = flight.get("num")
    pr = flight.get("pr")
    stage = flight.get("stage")

    journal = []
    for rec in journal_slice:
        c = tower.comms_row(rec, operator)
        ts = rec.get("ts") if isinstance(rec, dict) else None
        journal.append({"ts": ts, "hhmm": hhmm(ts), "text": c["text"], "radio": c["radio"],
                        "kind": c["kind"],
                        "raw": json.dumps(rec, separators=(",", ":"))})

    off = None
    if stage in OFF_PATH_PLAIN:
        off = {"state": stage, "plain": OFF_PATH_PLAIN[stage]}

    # The drawer's action verbs are the SERVER's, not the JS's (design record B.1): a bounced flight
    # must fire bounce-yes (its distinct audit trail), and the conflict-cap Discuss-default (§8) must
    # hold in the drawer exactly as it does on the card. ``None`` for a flight that isn't a decision.
    decision = None
    if stage in (flights.PARKED, flights.AWAITING):
        kind = card_kind(flight)
        bounced = flight.get("awaiting_reason") == "bounced" or kind == "bounced"
        decision = {
            "kind": kind,
            "approve_act": "bounce-yes" if bounced else "approve",
            "approve_label": ("Accept & relaunch" if bounced
                              else ("Re-approve & relaunch" if stage == flights.PARKED else "Re-approve")),
            "discuss_default": kind == "conflict-cap",
        }

    return {
        "num": num,
        "flight": flight.get("label"),
        "repo": slug,
        "airline": name,
        "title": title or (flight.get("label") or ("SL-%s" % num)),
        "stage": stage,
        "circuit": _circuit_rail(flight),
        "off_path": off,
        "clearance": _clearance(flight),
        "links": {
            "issue": "%s/%s/issues/%s" % (_GH, slug, num),
            "pr": ("%s/%s/pull/%s" % (_GH, slug, pr)) if pr else None,
            "branch": flight.get("branch"),
        },
        "memos": _memo_history(flight, journal_slice),
        "cargo": _cargo(flight),
        "journal": journal,
        "decision": decision,
        "attempt": flight.get("attempt", 1),
        "go_arounds": max(0, flight.get("attempt", 1) - 1),
    }
