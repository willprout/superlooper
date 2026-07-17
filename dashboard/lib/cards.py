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

# The review line has THREE readings, not two (issue #176). A verdict pinned to a superseded diff is
# "stale" — a review DID happen, just not for the current code — and must not read like "never
# reviewed" (a bare cross), because the two demand different owner responses: re-review the new diff
# vs get a first review. The gate carries the state on ``review_state`` (see lib/review_marker); when
# it is "stale" the review row swaps to this distinct label + gloss and keeps its own state so the
# pixel layer can paint it amber (reviewed, then rebuilt), never the same as absent.
_REVIEW_STALE_GLOSS = ("reviewed, then rebuilt",
                       "a review was posted, but for an earlier version of this diff — the current "
                       "code needs a fresh review before it can land")


def _review_state_of(gate):
    """The review line's state for display: the gate's own ``review_state`` when present (issue
    #176), else derived from the ``review`` bool for a legacy gate dict (True -> reviewed, False ->
    absent). Anything unrecognised fails closed to 'absent' — never a hopeful 'reviewed'."""
    state = gate.get("review_state")
    if state in ("reviewed", "stale", "absent", "unread"):
        return state
    return "reviewed" if gate.get("review") else "absent"

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
    """Which decision kind a waiting flight is. A durable owner-decision QUESTION (#163) is its own
    kind and takes precedence — it is answered, not approved/dropped-only, and its story is the
    question itself, not a go-around count. Otherwise: a flight that went around (``attempt`` >= 2 —
    a conflict regeneration happened) and STILL landed on William's desk is the ``conflict-cap`` case,
    whatever its underlying stage (§3); then ``parked`` (the machine gave up), or an amber decision
    that is a ``bounced`` push-back or a plain ``needs-owner``."""
    if flight.get("awaiting_reason") == "question":
        return "question"
    if _attempt(flight) >= 2:
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
    "question": {
        "headline": "A worker needs your decision before it can continue.",
        "plain": "The worker exited cleanly and posted its question here — nothing is frozen. Type an "
                 "answer and a fresh session resumes with it, reusing the work so far if it still fits.",
        "term": "question", "badge": "QUESTION",
    },
}


def _collision_sentence(flight):
    """The one plain sentence naming the collision on a conflict-cap card (§3: "names the collision
    in one plain sentence and offers reasoned choices, never a bare badge"). Built from real facts —
    the go-around count — so it never overclaims what happened."""
    attempt = _attempt(flight)
    go_arounds = attempt - 1
    times = "once" if go_arounds == 1 else "%d times" % go_arounds
    return ("%s kept colliding with work that landed first — rebuilt %s, %d attempts used, and it "
            "still couldn't merge cleanly." % (flight.get("label") or ("SL-%s" % flight.get("num")),
                                               times, attempt))


# =============================== the dossier — the evidence behind the decision (issue #162) ===============================
# William must be able to judge a hand-back without opening a terminal, so the card carries what the
# MACHINE actually saw, not just the sentence it wrote. Every item below is a real recorded fact —
# nothing is inferred, and an absent capture is SAID rather than papered over (the honest-empty
# discipline, §5: calm carries a caption; so does ignorance).

_NO_EVIDENCE = ("The runner recorded no structured evidence for this decision — what you see is "
                "everything the journal carries.")


def _last_handback_record(journal_slice, memo):
    """The journal record for the hand-back whose words are ON THE CARD — or ``None``.

    THE INVARIANT (both halves found by Codex cross-review, issue #162): the dossier describes the
    same decision as the memo above it, or it describes nothing. Two ways that broke:

    * Selecting only ``park`` records made a SETTLED bounce show an older park's evidence beside the
      bounce's own memo. So the scan mirrors ``flights._flight_memo`` exactly — the last ``park`` or
      ``bounce`` **that has a memo** (a memo-less record is not a hand-back either reader will show).
    * The card's text does not always come from the journal at all: in the window between the worker
      writing ``state/blocked/<id>`` and the runner's next tick, ``_flight_memo`` shows the MARKER's
      text and no ``bounce`` record exists yet — so the newest journalled hand-back is a DIFFERENT,
      older decision. Borrowing its evidence pairs one question with another's answer.

    Hence the match: the record must carry the exact text being shown. Anything else fails closed to
    "no structured evidence", which the card then says out loud.
    """
    found = None
    for r in journal_slice or []:
        if isinstance(r, dict) and r.get("act") in flights.HANDBACK_ACTS and r.get("memo"):
            found = r
    if found is None:
        return None
    if (found.get("memo") or "").strip() != (memo or "").strip():
        return None
    return found


def _evidence_items(rec):
    """The evidence the runner captured (issue #152) on this hand-back, as ordered label/value rows,
    plus whether that capture was STRUCTURED.

    #152 is the producer and has not landed on every path, so this reads DEFENSIVELY. A dict is a
    real structured capture: one row per field (insertion order — the runner writes cause-first). A
    bare string is that issue's own fail-closed ``"captured: none, reason unknown"`` shape — it is
    SHOWN, but it is the report of an absence, not evidence, so it does not count as captured
    (Codex cross-review: counting it suppressed the honest-empty note and dressed "we saw nothing"
    up as something the machine saw). Anything else is ignored rather than rendered as a Python
    repr at the owner.
    """
    ev = rec.get("evidence") if isinstance(rec, dict) else None
    if isinstance(ev, dict):
        rows = [{"label": str(k), "value": _plain(v)} for k, v in ev.items() if _plain(v)]
        return rows, bool(rows)
    if isinstance(ev, str) and ev.strip():
        return [{"label": "captured", "value": ev.strip()}], False
    return [], False


def _attempt(flight):
    """This flight's attempt count, fail-closed to 1. ``build_flight`` normalizes it, but this layer
    is pure over an arbitrary dict and one malformed flight must never blank the owner's whole inbox
    (Codex cross-review, issue #162). A bool is not a count; NaN/inf and non-ints are not counts."""
    a = flight.get("attempt", 1)
    if isinstance(a, bool) or not isinstance(a, int):
        return 1
    return a if a >= 1 else 1


def _plain(v):
    """A journal value as one plain string. Bools render as words (``rc: False`` would read as a
    Python literal at a human), everything else through ``str`` — the JS escapes it downstream."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def decision_dossier(flight, journal_slice):
    """The evidence behind an owner hand-back (design record §4 / issue #162) — pure over the flight
    and its journal slice.

    Returns ``{captured, note, items: [{label, value}]}``. ``captured`` says whether the runner
    recorded STRUCTURED evidence (#152) for this park; when it did not, ``note`` names that absence
    in plain words instead of implying the memo is all the machine saw. ``items`` always carries the
    real facts the dashboard can already read honestly — the recorded cause, the gate's own reading
    at the hand-back, and the go-around count — so the card is useful today and richer the moment
    #152 lands.
    """
    rec = _last_handback_record(journal_slice, flight.get("memo")) or {}
    items, captured = _evidence_items(rec)

    # The runner's own episode key — a terse machine classification (`answerer_escalated`,
    # `checks_pending`, `launch_delivery`). It is usually the memo verbatim (``park()`` defaults
    # cause=memo), and on a bounce it is the literal "bounce" — which the BOUNCED badge already says.
    # Echoing either back under a heading is noise, so a key that only repeats the memo or names its
    # own act is dropped; a key that classifies something the memo does not is real evidence.
    cause = rec.get("cause")
    cause = cause.strip() if isinstance(cause, str) else ""
    if cause and cause != (flight.get("memo") or "").strip() and cause != rec.get("act"):
        items.append({"label": "recorded cause", "value": cause})

    # What the gate READ at the hand-back, under the four real check names (§3). Only the checks that
    # were RED are evidence — a green check explains nothing about why this stopped. Skipped for a
    # #163 question: the worker paused MID-build to ask, so every gate check is naturally not-yet and
    # says nothing about the decision — the question itself is the whole evidence (shown as the memo).
    gate = flight.get("gate") or {}
    # A stale review is named distinctly here too (issue #176): "reviewed, then rebuilt" is a
    # different hand-back reason than "never independently reviewed", and the dossier is where the
    # owner reads why it stopped.
    red = []
    for k in _GATE_ORDER:
        if gate.get(k):
            continue
        if k == "review" and _review_state_of(gate) == "stale":
            red.append(_REVIEW_STALE_GLOSS[0])
        else:
            red.append(GATE_GLOSS[k][0])
    if red and flight.get("awaiting_reason") != "question":
        items.append({"label": "gate at hand-back", "value": "not yet: " + ", ".join(red)})

    go_arounds = _attempt(flight) - 1
    if go_arounds:
        items.append({"label": "rebuilt after conflicts",
                      "value": "%d time%s" % (go_arounds, "" if go_arounds == 1 else "s")})

    return {"captured": captured, "note": None if captured else _NO_EVIDENCE, "items": items}


# =============================== the verbs, named by consequence (issue #162) ===============================
# No button may hide what it does. These are exactly the mechanical verbs ``lib/actions`` already
# exposes — approve / bounce-yes / drop / discuss — with NO new verb invented here (issue #162
# boundary); only their NAMES change, and the name now states the effect.
#
# The load-bearing honesty: a re-approval is NOT "carry on from here". A fresh `agent-ready` on any
# park-family status routes through the engine's `_exec_reapprove`, which prunes the worktree,
# DELETES the filed report, zeroes the attempt counters and relaunches from scratch. "Re-approve"
# hid that — the owner could not tell the button threw his finished work away. Issue #161 splits the
# verb (resume-at-the-gate vs rebuild-from-scratch); until that lands, these labels name what the
# engine REALLY does today, because a label that flatters is worse than no label.

# Conditional, because a hand-back can happen BEFORE any work exists: a `needs_william` park can be
# raised at approval time (a missing `touches:` declaration, say), where there is no worktree and no
# report to throw away. `_exec_reapprove` removes them IF PRESENT — so the sentence says "any",
# never asserting work exists (Codex cross-review, issue #162). What it must never soften is the
# warning itself: nothing is resumed, and anything already built is not kept.
_DISCARDS = ("Any worktree and filed report this issue already has are discarded — nothing is "
             "resumed — its attempt counters are zeroed, and a fresh session starts from the issue.")


def decision_actions(flight, slug=None):
    """The ordered buttons for a waiting flight, each naming its consequence (issue #162).

    Each item is ``{act, label, consequence, tone, destructive}`` — plus ``armed_label`` and
    ``armed_caption`` on a destructive verb, whose second tap the client arms. ``act`` is the wire
    verb the server's executor already knows; the client binds these strings and derives none of
    them (design record B.1), so the card and the drawer can never drift apart or drift from the
    engine. ``slug`` names the destructive verb's unique target in its caption; without one the
    caption is omitted rather than naming an ambiguous target (issue #44).
    """
    kind = card_kind(flight)
    bounced = flight.get("awaiting_reason") == "bounced" or kind == "bounced"
    num = flight.get("num")

    if bounced:
        yes = {"act": "bounce-yes", "label": "Accept the amendment & rebuild",
               "consequence": "Records that you accepted the worker's proposed amendment. " + _DISCARDS}
    else:
        yes = {"act": "approve", "label": "Re-approve & rebuild from scratch",
               "consequence": "Re-applies agent-ready in your name — your word, on the record. "
                              + _DISCARDS}
    yes.update({"tone": "ghost" if kind == "conflict-cap" else "primary", "destructive": False})

    # The armed caption is a SEMANTIC — it names a destructive consequence — so it lives here beside
    # the label it warns about, never hard-coded in the JS where the two could drift (B.1 / Codex
    # cross-review). It names the UNIQUE target, repo AND number: Needs You is whole-field, so two
    # repos can each carry a #7 and the number alone would not say which one closes (issue #44).
    drop = {"act": "drop", "label": "Drop — closes the issue for good",
            "armed_label": "Tap again to close #%s for good" % num,
            "armed_caption": ("✕ Closes %s #%s for good — never-mind, not release." % (slug, num)
                              if slug else None),
            "consequence": "Closes the issue on GitHub with your audit comment. This is never-mind, "
                           "not release — nothing gets built.",
            "tone": "ghost", "destructive": True}

    discuss = {"act": "discuss", "label": "Discuss — draft a briefing",
               "consequence": "Assembles a briefing you can read and edit. Changes nothing on "
                              "GitHub and builds nothing.",
               "tone": "primary" if kind == "conflict-cap" else "link", "destructive": False}

    # A durable question (#163) is ANSWERED, not approved: the primary verb takes the owner's typed
    # text (``input: "answer"`` tells the client to render the answer field). Posting it re-applies
    # agent-ready in William's name — his word, on the record — and a fresh session resumes with the
    # Q&A in its brief. Drop (never-mind) and Discuss stay available.
    if kind == "question":
        answer = {"act": "answer", "label": "Answer & relaunch", "input": "answer",
                  "consequence": "Posts your answer on the issue and re-applies agent-ready in your "
                                 "name — a fresh session resumes with your answer in its brief, "
                                 "reusing the work so far if it still applies cleanly.",
                  "tone": "primary", "destructive": False}
        return [answer, discuss, drop]

    # On a collision, Discuss LEADS (§8 — the guard against a blind Approve press there).
    return [discuss, yes, drop] if kind == "conflict-cap" else [yes, drop, discuss]


def needs_you_card(flight, slug, journal_slice=None):
    """A whole-field Needs You card for a waiting flight (design record §4). Leads with a plain
    headline + gloss (the literal term is on hover), carries the WHOLE memo (never trimmed — issue
    #162), a link to the issue, the dossier of evidence behind the decision, consequence-named verbs,
    and — for the conflict-cap case only — one plain sentence naming the collision with
    ``discuss_default`` set so the client highlights Discuss instead of Approve (§8). ``badge_base``
    is the state word; the server appends the exact age numeral. ``journal_slice`` is this flight's
    records, for the dossier; omitting it yields an honest empty dossier, never a crash."""
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
        # The whole question, exactly as the worker wrote it — the card grows to fit it (#162).
        "memo": flight.get("memo"),
        "issue_url": "%s/%s/issues/%s" % (_GH, slug, flight.get("num")),
        "dossier": decision_dossier(flight, journal_slice or []),
        "actions": decision_actions(flight, slug=slug),
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
    gloss. ``ok`` is the honest gate reading (fail-closed upstream in ``flights.gate_checklist``).
    Every row also carries a ``state`` so the pixel layer maps glyphs uniformly: the binary checks
    are ``ok``/``no``; the review line is the three-way #176 state (reviewed / stale / absent /
    unread), and a STALE review swaps to a distinct label + gloss so 'reviewed, then rebuilt' never
    renders identical to 'never reviewed'."""
    gate = flight.get("gate") or {}
    out = []
    for key in _GATE_ORDER:
        label, gloss = GATE_GLOSS[key]
        if key == "review":
            state = _review_state_of(gate)
            if state == "stale":
                label, gloss = _REVIEW_STALE_GLOSS
            out.append({"key": key, "label": label, "gloss": gloss,
                        "ok": state == "reviewed", "state": state})
        else:
            ok = bool(gate.get(key))
            out.append({"key": key, "label": label, "gloss": gloss, "ok": ok,
                        "state": "ok" if ok else "no"})
    return out


def _memo_history(flight, journal_slice):
    """Every distinct memo this flight accrued, in journal order (design record §4 — "memo
    history"): each HAND-BACK memo — ``park`` or ``bounce`` — plus the flight's current memo (a live
    bounce marker's text) when it isn't already the last of them. Order preserved, duplicates
    collapsed.

    Bounces belong here (Codex cross-review, issue #162): reading only parks made a
    ``bounce -> park`` history list just the park, hiding that the worker ever pushed back — and the
    drawer is meant to be the flight's whole story.
    """
    memos = []
    for r in journal_slice:
        if isinstance(r, dict) and r.get("act") in flights.HANDBACK_ACTS and r.get("memo"):
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
        # ONE source for the verbs (issue #162): the drawer renders the same consequence-named
        # actions as the card, so the two surfaces cannot drift. ``approve_act``/``approve_label``
        # are kept as the drawer's existing yes-verb contract — now read out of that single list.
        acts = decision_actions(flight)
        yes = [a for a in acts if a["act"] in ("approve", "bounce-yes")][0]
        decision = {
            "kind": kind,
            "actions": acts,
            "approve_act": yes["act"],
            "approve_label": yes["label"],
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
        "dossier": decision_dossier(flight, journal_slice),
        "cargo": _cargo(flight),
        "journal": journal,
        "decision": decision,
        "attempt": _attempt(flight),
        "go_arounds": _attempt(flight) - 1,
    }
