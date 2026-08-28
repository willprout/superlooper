"""The triage flight as the board sees it (issue #451) — one pure read of the journal.

The ``t<N>`` triage flight (#448) is the third session class the launcher spawns, beside the ``i<N>``
issue worker and the ``d<N>`` debugger seat. It is the queue-hygiene delegation the owner made by
standing rule: once a day, when something in the unapproved pile has changed, one unattended session
judges that pile and may close, merge, fix or escalate on its own word.

**Where its truth lives, and where it does not.** #463 settled that a flight is structurally absent
from everything that tracks a lane — no loopstate record, no branch, no PR — and therefore absent
from the runner's published view too, in that document's own words: *"a flight has no loopstate
record by design, so it is never tracked, never carried and never given a phase … #451 owns how a
flight renders."* So there is exactly one place a flight's work is written down for a reader, and
#449 built it deliberately: the journal. ``report.py``'s comment beside the morning report's Triage
section is the contract this module holds up the other end of —

    Derived from the journal like every other section, and from nothing else. The flight writes a
    markdown run log in its state home too, but that log is prose for the NEXT flight; the journal
    is what the report and the dashboard read, so they can never drift from each other.

The assembler already reads the whole journal every tick (for the tower log, the shipped counter and
the incident sign), so this card costs **no new server read** — the same property #458's fixer block
was built on, and for the same reason: one set of records, glossed twice, can never contradict
itself about one night's work.

**The tally is the FLIGHT's whenever the flight wrote one.** ``superlooper triage-finish`` counts its
own journal and writes both the ``counts`` dict and the summary sentence that goes into the run log
and the morning report. The card binds those verbatim. Only when no finish is on record — a flight
that died halfway, or one still working — are the acts counted here, which is exactly the fallback
``report._triage_summary`` takes and for the same stated reason: *"a flight that died before
finishing still closed whatever it closed, and the owner must still be told."*

**Silence on a day with no flight.** ``present`` is False and nothing renders. That is the honest
absence (``report._triage`` returns ``[]`` and the whole section vanishes), and it is what keeps the
board of every repo that has not opted in — which, since the flight ships disabled, is all of them —
byte-for-byte what it was before this issue.

Pure and total: no clock of its own, no I/O, and no record shape can make it raise. It runs inside
the 2-second snapshot poll, where a raise blanks the whole board for every repo.
"""
import math
import re

import flights


# --------------------------------------------------------------------------- what a flight IS

# The flight id's shape, mirrored from the engine's ``triage.FLIGHT_ID_RE`` rather than imported:
# the dashboard imports no engine module, by construction (it is its own repo's face over a state
# home the engine happens to write). #463 made this shape the engine's to spell, and this is the
# board reading the same answer from the other side of the state home.
FLIGHT_ID_RE = re.compile(r"^t[0-9]+$")

# The acts #449's flight journals. ``triage_launch`` and ``triage_finish`` bracket the run and name
# no issue; the five between them are one verdict on one issue each. Spelled as bare literals, as
# every other reader in this repo spells the engine's vocabulary.
LAUNCH_ACT = "triage_launch"
FINISH_ACT = "triage_finish"
ACT_PREFIX = "triage_"

# The ONE outcome word that means a session exists. Fail closed on everything else — absent, blank,
# or a word a newer engine invented — for the reason ``fixer.launch_outcome`` does on the same
# question one session class over: a plane on the field is a CLAIM that a session is running, and a
# record that never made that claim must not be read as having made it.
LAUNCHED = "launched"

# The run's tally, in the engine's own key order (``bin/superlooper`` ▸ ``_triage_counts``).
COUNT_KEYS = ("judged", "merged", "closed", "ledger", "fixed", "escalated")

# Which act increments which count — the engine's ``_TRIAGE_ACT_COUNTS``, mirrored. ``judged`` is
# every one of them (an act is a judgement); ``ledger`` is not here because it is a property of a
# close (a nit files a limitation), not an act of its own.
ACT_COUNTS = {"triage_keep": None, "triage_fix": "fixed", "triage_merge": "merged",
              "triage_close": "closed", "triage_escalate": "escalated"}

# Where the numbers on the card came from. The distinction is not decoration: the flight's own tally
# is authoritative (it counted its own journal at finish, including acts this window may not hold),
# and the derived one is an honest best effort over what is on record here.
FLIGHT = "flight"
DERIVED = "derived"

# The three states a run can be in on the board.
FLYING = "flying"          # launched, and has not closed its run — the plane is up
FINISHED = "finished"      # the flight wrote its sitting sheet and its tally; the survey is over
NO_FLIGHT = "no-flight"    # a launch was attempted and did not land a session — nothing was triaged

# How far back a run stays the board's news. The lease is ONE flight per local day (``triage.due``
# ▸ the day stamp), so a day and a half covers the most recent flight under every cadence the
# trigger can produce — including a flight that went out at 02:00 and is being read the following
# evening — while still guaranteeing that a flight which launched and never finished leaves the
# field rather than orbiting it forever. It is a display window, not a claim about the run.
WINDOW_SECONDS = 36 * 3600

# What a flight is called when its own launch record does not name it. A plane needs a stable
# identity to be drawn by and keyed on, and a record that supplies none cannot be flown — but the
# run still happened, and saying so with a stand-in beats saying nothing.
UNNAMED_LABEL = "the triage flight"


def is_flight_id(value):
    """Is ``value`` a ``t<N>`` triage-flight id? Total: a lane id, a debugger seat, a bare number
    and any wrong-typed value are all simply not flights."""
    return isinstance(value, str) and FLIGHT_ID_RE.match(value) is not None


def flight_num(value):
    """``t7`` -> ``7``; anything that is not a flight id -> ``None``. The number is what keys the
    plane's sprite and orders it on the field, so it must never be guessed."""
    return int(value[1:]) if is_flight_id(value) else None


# --------------------------------------------------------------------------- reading the journal

def _finite(v):
    """``True`` only for a real, finite number. ``OverflowError`` is caught because ``json.loads``
    parses an arbitrarily large INTEGER and ``math.isfinite(10**309)`` RAISES on it rather than
    answering False — the shape ``tower._finite`` and ``flights._stop_epoch`` are both hardened
    against, reached here through the same door (a corrupt journal line)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _ts(rec):
    ts = rec.get("ts")
    return float(ts) if _finite(ts) else None


def _is_triage(rec):
    act = rec.get("act")
    return isinstance(act, str) and act.startswith(ACT_PREFIX)


def _rec_flight(rec):
    """The ``t<N>`` a record belongs to, or ``None``.

    Two keys, because the engine stamps it under two: an act carries ``flight`` (``_triage_record``
    reads ``SL_ISSUE_ID``, which the LAUNCHER assigns and no session can self-assert) and the launch
    itself carries ``id``. Both are the same claim, and a reader that knows only one of them
    attributes either nothing or everything.

    ``None`` is the load-bearing answer, and it is the engine's own (``triage.finished_flights``): a
    hand-run verb from the owner's shell journals an EMPTY flight id — the CLI reads it from the
    session's environment and a terminal has none — and that record belongs to NO flight rather than
    to whichever one ran last.
    """
    for key in ("flight", "id"):
        v = rec.get(key)
        if is_flight_id(v):
            return v
    return None


def _one_line(text, limit=140):
    """The first non-empty line of a field the flight wrote, trimmed. A run summary is one line by
    construction; a detail written by an agent is not necessarily."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return (line[:limit - 1].rstrip() + "…") if len(line) > limit else line
    return ""


def counts(records):
    """The run's tally counted from its own acts — the fallback for a flight that wrote none.

    Mirrors the engine's ``_triage_counts`` exactly, including ``ledger`` (a close that filed a
    limitation carries the ledger issue's number) and ``judged`` (every act is a judgement). Pure,
    total, and every value is an ``int``: a wrong-typed record contributes nothing rather than
    poisoning a number the card prints.
    """
    out = {k: 0 for k in COUNT_KEYS}
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        key = ACT_COUNTS.get(rec.get("act"), False)
        if key is False:
            continue                                   # not a counted act (launch/finish/refused)
        out["judged"] += 1
        if key:
            out[key] += 1
        if rec.get("act") == "triage_close" and type(rec.get("ledger")) is int:
            out["ledger"] += 1
    return out


def _summary(count_map):
    """The dashboard's own spelling of the tally, used ONLY when the flight wrote none. Deliberately
    the engine's wording (``triage_run.run_summary``) so a derived line and a flight's own line read
    as the same kind of sentence — the card says which it is, it does not say it twice."""
    return ("judged %d · %d merged · %d closed (%d to the ledger) · %d fixed · "
            "%d escalated" % tuple(count_map[k] for k in COUNT_KEYS))


def _mtime(activity, fid):
    """The flight's own ``state/activity/<id>`` stamp, or ``None`` — a wrong-typed scan, a missing
    entry and a non-numeric mtime all read the same: no liveness signal."""
    if not isinstance(activity, dict) or fid is None:
        return None
    v = activity.get(fid)
    return v if _finite(v) else None


def _blank():
    return {"present": False, "on_field": False, "id": None, "num": None,
            "label": UNNAMED_LABEL, "state": None, "contrail": "none",
            "counts": {k: 0 for k in COUNT_KEYS}, "counts_source": DERIVED, "counts_note": "",
            "escalated": 0, "tally": "", "headline": "", "text": "", "ts": None}


def run(records, now, activity=None, window_seconds=WINDOW_SECONDS,
        idle_seconds=480, freeze_seconds=2700):
    """The most recent triage run inside the display window, as the board renders it.

    ``records``        one repo's journal, exactly as the assembler already read it.
    ``now``            this tick's clock (the caller's, never one of ours).
    ``activity``       ``{id: mtime}`` from ``state/activity`` — the scan the assembler already made
                       for every lane. The flight's own stamp is looked up HERE rather than passed
                       in, because only this function knows which flight the run belongs to; a
                       flight with no stamp yet (the gap between the launch and the delivery-verify
                       that writes it) has no signal, which is not the same as frozen.
    ``idle``/``freeze`` the repo's OWN thresholds (decision B.4), never a module constant.

    ``present`` False is the honest render for a day with no flight, and it is the whole of what a
    repo that has not opted in ever sees.
    """
    if not isinstance(records, (list, tuple)):
        return _blank()

    window_start = now - window_seconds if _finite(now) and _finite(window_seconds) else None
    mine = []
    for rec in records:
        if not isinstance(rec, dict) or not _is_triage(rec):
            continue
        ts = _ts(rec)
        if ts is None or (window_start is not None and ts < window_start):
            continue                      # a record with no usable clock joins no run
        mine.append((ts, rec))

    # BY THE CLOCK, not by file order (fresh-agent review round 2). The journal is append-ordered in
    # practice, but every display reader here sorts by ts rather than trusting that — `_tower_window`
    # says so in as many words — and a launch line that lands out of order must not make an older
    # launch the day's run. File position breaks the tie, so two records sharing a ts stay stable.
    mine.sort(key=lambda pair: pair[0])

    launch = None
    for ts, rec in mine:
        if rec.get("act") == LAUNCH_ACT:
            launch = (ts, rec)            # the LATEST launch in the window is this run's
    if launch is None:
        # Acts with no launch behind them are a hand-run verb from the owner's own shell (the CLI
        # records an EMPTY flight id for one, deliberately). They are real, and the tower log shows
        # every one — but there was no flight, so there is no run and no card claiming one.
        return _blank()

    launch_ts, launch_rec = launch
    fid = _rec_flight(launch_rec)
    # THIS FLIGHT's own acts, and nothing else's (fresh-agent review, P0). The window alone is not
    # the boundary — two things slip through it, and they fail in opposite directions:
    #
    #   * an operator's hand-run `triage-finish` carries an EMPTY flight id, and read as this run's
    #     close it grounds a still-working survey and prints a tally the flight never wrote;
    #   * another flight's acts (a second `t<N>` in the same window) get counted into this one.
    #
    # So attribution is by the id the LAUNCHER assigned, exactly as `triage.finished_flights` does
    # it one repo over. A launch that names no flight can attribute nothing: no identity, no claim.
    # The `ts >= launch_ts` bound stays, so yesterday's t7 cannot lend today's t7 its numbers.
    during = [rec for ts, rec in mine
              if ts >= launch_ts and rec is not launch_rec and fid is not None
              and _rec_flight(rec) == fid]

    finish = None
    for rec in during:
        if rec.get("act") == FINISH_ACT:
            finish = rec

    flown = launch_rec.get("outcome") == LAUNCHED and fid is not None
    if launch_rec.get("outcome") != LAUNCHED:
        state = NO_FLIGHT
    elif finish is not None:
        state = FINISHED
    else:
        state = FLYING

    said = finish.get("counts") if isinstance(finish, dict) else None
    if isinstance(said, dict) and all(type(said.get(k)) is int for k in COUNT_KEYS):
        count_map, source = {k: said[k] for k in COUNT_KEYS}, FLIGHT
    else:
        count_map, source = counts(during), DERIVED

    tally = _one_line(finish.get("detail")) if source == FLIGHT and isinstance(finish, dict) else ""
    if not tally:
        tally = _summary(count_map)

    label = fid or UNNAMED_LABEL
    return {
        "present": True,
        # A plane is drawn only for a launch the engine CONFIRMED, on a flight it NAMED. Everything
        # else renders as a card and no aircraft — the same fail-closed line `fixer.last_launch`
        # draws before offering a window onto a session that may not exist.
        "on_field": bool(flown and state == FLYING),
        "id": fid, "num": flight_num(fid) if fid else None, "label": label,
        "state": state,
        "contrail": flights.contrail_kind(_mtime(activity, fid), now, idle_seconds, freeze_seconds)
                    if state == FLYING else "none",
        "counts": count_map, "counts_source": source,
        # Said out loud only where it is NEWS: a run that CLOSED and still wrote no readable tally
        # means the flight's own last move did not land, and the numbers beside it are ours rather
        # than its. On a run still in the air, derived is simply what "so far" means, and stamping
        # every one of those with a caveat would teach the owner to ignore the caveat.
        "counts_note": ("the flight recorded no tally of its own — counted from its acts"
                        if state == FINISHED and source == DERIVED else ""),
        "escalated": count_map["escalated"],
        "tally": tally,
        "headline": _HEADLINES[state],
        "text": _sentence(state, label, launch_rec, tally),
        "ts": launch_ts,
    }


# --------------------------------------------------------------------------- the words

_HEADLINES = {FLYING: "TRIAGE SURVEY UP", FINISHED: "TRIAGE RUN CLOSED", NO_FLIGHT: "NO TRIAGE TODAY"}


def _sentence(state, label, launch_rec, tally):
    """One honest sentence for the card. Never the act name, never a blank — the #253 lesson is that
    a surface which cannot say what happened must say THAT, in words, rather than fall through."""
    if state == NO_FLIGHT:
        # The engine's own wording for this, so the card, the tower log and the morning report all
        # say the same thing about the same failed launch (``report._triage``).
        said = _one_line(launch_rec.get("outcome")) or "the launcher confirmed no session"
        return "%s did not launch — %s. Nothing was triaged today." % (label, said)
    if state == FINISHED:
        return "%s closed its run — %s." % (label, tally)
    why = _one_line(launch_rec.get("detail"))
    if label == UNNAMED_LABEL:
        # The launch is on record and its own id is not readable, so nothing it did could be
        # attributed to it. Say exactly that rather than printing a tally of zeroes as if the run
        # had judged nothing.
        return ("%s is out — %s. Its launch record did not name the flight, so no act on this "
                "board could be attributed to it." % (label, why or "a triage survey was launched"))
    return ("%s is over the unapproved queue — %s. So far: %s."
            % (label, why or "a triage survey is out", tally))


__all__ = ["FLIGHT_ID_RE", "LAUNCH_ACT", "FINISH_ACT", "ACT_PREFIX", "LAUNCHED", "COUNT_KEYS",
           "ACT_COUNTS", "FLIGHT", "DERIVED", "FLYING", "FINISHED", "NO_FLIGHT", "WINDOW_SECONDS",
           "UNNAMED_LABEL", "is_flight_id", "flight_num", "counts", "run"]
