"""The tower log — a comms feed glossed from the journal (Task 9 / design record §4, §7).

The journal is honest but shapeless: an append-only stream of ``act`` records. The tower log turns
each into a **plain, flight-numbered sentence** anyone can read at a glance, with an optional
**radio-flavor prefix beside it** ("Going around," "Number two for landing,"). The discipline is
the design record's costume rule (§3, rule 2) plus its honesty law (§7):

* The **real sentence is always present** and honest — never empty, never merely the flavor prefix.
  Boring mode strips ``radio`` and shows only ``text``; both must read correctly (§7: "prefixes
  always carry the real sentence beside them; boring mode strips all flavor").
* **No flourish for a dishonest state** — a *wandered* merge gets the plain "see report" sentence
  and NO celebratory radio call; the two human-gate verbs (re-approve) stay calm (§7 fun-free zone).
* **Every act renders in plain words the day it exists** (costume rule 4): an unknown ``act`` still
  produces a sentence, so the dashboard never silently under-reports the autonomous system.

Everything here is a PURE function of a record — no clock, no I/O — so the gloss is unit-tested to
the line and the JS downstream binds strings it never derives (design record B.1). Flight number =
issue number everywhere, so every line stays journal-greppable (§3).
"""
import math


# =============================== the comms/routine tier (issue #36) ===============================
# The tower log is the CURATED comms channel (design record §4) — machine bookkeeping does not belong
# on the radio. ``relabel`` (a label-convergence record) fires several times per launch as GitHub's
# read lags the write: honest, but noise. It is classified into the ``routine`` tier server-side (per
# B.1 — the JS binds visibility, it derives nothing), so the tower log hides it by default while the
# journal firehose stays complete. The set is the extension point: a future noisy-but-honest act joins
# ``ROUTINE_ACTS`` and inherits the classified-as-data, hidden-by-default behavior — no per-type UI
# debate (owner ruling 2026-07-07).
ROUTINE_ACTS = frozenset({"relabel"})


def tier(rec):
    """Which tower-log tier a journal record belongs to: ``"routine"`` for machine bookkeeping that
    should not be announced on the comms radio (issue #36), ``"comms"`` for everything a human reads
    as real traffic. Pure and server-side (B.1) so the classification is data, not a UI debate. A
    non-dict / act-less record is ``"comms"`` — fail toward VISIBLE, never silently swallow an
    unrecognised record (costume rule 4 / honesty §7)."""
    if not isinstance(rec, dict):
        return "comms"
    return "routine" if rec.get("act") in ROUTINE_ACTS else "comms"


def _num(rec):
    """The issue number a record is about — ``num`` if present, else its ``id`` (``i23`` → 23), else
    an ``event.id`` envelope. ``None`` when the record names no flight (a repo-wide notify)."""
    n = rec.get("num")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    for src in (rec.get("id"), (rec.get("event") or {}).get("id") if isinstance(rec.get("event"), dict) else None):
        if isinstance(src, str) and src.startswith("i") and src[1:].isdigit():
            return int(src[1:])
    return None


def _tag(num):
    return "SL-%d" % num if num is not None else ""


def _who(num):
    return "SL-%d" % num if num is not None else "the flight"


def _first_line(text, limit=76):
    """The first non-empty line of a multi-line field (an answerer question, a nudge message),
    trimmed — the comms feed shows the gist; the row expands to the raw line for the whole thing."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return (line[:limit - 1].rstrip() + "…") if len(line) > limit else line
    return ""


def _merge_row(rec, num):
    """A ``merge`` record → its comms sentence. Only a SUCCESSFUL, non-wandered merge is a
    celebrated touchdown; a wandered one is a neutral landing with "see report" (§7); a failed
    merge is a missed approach, never a landing."""
    if rec.get("outcome") != "ok":
        return {"radio": "Going around.", "kind": "merge",
                "text": "%s missed the approach — merge failed, the loop will retry." % _who(num)}
    pr = rec.get("pr")
    pr_bit = " — PR #%s merged" % pr if pr else " — merged"
    if rec.get("wander"):
        return {"radio": "", "kind": "merge",   # no flourish: the landing wandered outside its lane
                "text": "%s down%s, but it wandered outside its lane — see report." % (_who(num), pr_bit)}
    return {"radio": "Nice landing.", "kind": "merge",
            "text": "%s touchdown%s." % (_who(num), pr_bit)}


def _event_row(rec, num):
    """A journal ``event`` envelope → a plain radio sentence. Its real fact is ``event.type``."""
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    et = ev.get("type") or "event"
    who = _who(num)
    to_tower = ("%s to tower." % _tag(num)) if num is not None else "To tower."
    table = {
        "session_blocked": (to_tower,
                            "%s standing by — session blocked, awaiting an answer." % who, "event"),
        "session_finished": ("%s, base turn." % _tag(num),
                             "%s report filed — turning toward the gate." % who, "event"),
        "idle": ("", "%s quiet on the frequency — session idle." % who, "event"),
        "frozen": ("", "%s no response — session frozen on the field." % who, "event"),
        "exited": ("", "%s left the pattern — session exited." % who, "event"),
        "autofix_failed": ("Mayday.", "%s auto-repair failed — the freeze needs a look." % who, "event"),
        "runner_down": ("Mayday, mayday.", "Tower unmanned — the runner heartbeat went stale.", "event"),
    }
    radio, text, kind = table.get(et, ("", "%s %s." % (who, et.replace("_", " ")), "event"))
    return {"radio": radio, "kind": kind, "text": text}


# The one-line gloss for the acts with no special sub-cases. Each is (radio, kind, sentence-tail);
# the tail is prefixed with the flight tag by comms_row. A ``None`` radio means "no flavor".
def comms_row(rec, operator="the owner"):
    """One journal record → ``{radio, text, kind, num, tier}``. ``text`` is the real, plain, flight-
    numbered sentence (always non-empty); ``radio`` is optional flavor shown beside it; ``kind`` is
    a style class; ``tier`` is the comms/routine classification (issue #36 — see :func:`tier`). A
    non-dict record degrades to a bare "unreadable line" row (comms tier), never a crash.
    ``operator`` is the configured operator display name (issue #58) — a re-approval is the owner's
    own gate, so its line signs their name."""
    if not isinstance(rec, dict):
        return {"radio": "", "text": "an unreadable journal line", "kind": "unknown",
                "num": None, "tier": "comms"}
    act = rec.get("act")
    num = _num(rec)
    who = _who(num)

    if act == "merge":
        row = _merge_row(rec, num)
    elif act == "event":
        row = _event_row(rec, num)
    elif act == "launch":
        row = {"radio": "Cleared for takeoff.", "kind": "launch",
               "text": "%s departed — build session started." % who}
    elif act == "park":
        row = {"radio": "Mayday.", "kind": "park",
               "text": "%s parked — the machine gave up; your call." % who}
    elif act == "hold":
        row = {"radio": "Number two for landing.", "kind": "hold",
               "text": "%s holding — number 2 for landing, behind an overlapping lane." % who}
    elif act == "regenerate":
        n = rec.get("conflicts")
        tail = " (conflict #%s)" % n if isinstance(n, int) and not isinstance(n, bool) else ""
        row = {"radio": "Going around.", "kind": "regen",
               "text": "%s go-around — rebuilding from scratch%s." % (who, tail)}
    elif act == "nudge":
        msg = _first_line(rec.get("message")) or (rec.get("nudge_key") or "a reminder")
        row = {"radio": "Tower to %s." % (_tag(num) or "the flight"), "kind": "nudge",
               "text": "%s nudge — %s" % (who, msg)}
    elif act == "hire_answerer":
        q = _first_line(rec.get("question")) or "a blocking question"
        row = {"radio": "%s to tower." % (_tag(num) or "Aircraft"), "kind": "radio",
               "text": "%s radio — worker blocked: %s (auto-tower answering)." % (who, q)}
    elif act == "deliver_answer":
        a = _first_line(rec.get("text")) or "answer delivered"
        row = {"radio": "Tower to %s." % (_tag(num) or "aircraft"), "kind": "answer",
               "text": "%s answer — auto-tower: %s" % (who, a)}
    elif act == "gate":
        if rec.get("outcome") == "ok":
            row = {"radio": "", "kind": "gate", "text": "%s cleared the gate." % who}
        else:                                # a failed/held gate is NOT a pass — never read as cleared
            reason = _first_line(rec.get("outcome")) or "a check is not green"
            row = {"radio": "", "kind": "gate", "text": "%s held at the gate — %s." % (who, reason)}
    elif act == "notify":
        row = {"radio": "", "kind": "notify", "text": "note — %s" % (rec.get("title") or "(memo)")}
    elif act in ("reapprove", "approve"):
        row = {"radio": "", "kind": "approve",   # a human gate is a fun-free zone (§7) — stays calm
               "text": "%s re-approved by %s." % (who, operator)}
    elif act == "relabel":
        row = {"radio": "", "kind": "relabel", "text": "%s relabelled." % who}
    elif act == "update":
        row = {"radio": "", "kind": "update",
               "text": "%s update — %s." % (who, _first_line(rec.get("outcome")) or "in progress")}
    elif act == "alert":
        row = {"radio": "Mayday, mayday.", "kind": "alert", "text": "ALERT raised — a factory-stop."}
    elif act == "freeze":
        row = {"radio": "", "kind": "freeze", "text": "Landings paused — a repair flight is out."}
    elif act == "unfreeze":
        row = {"radio": "", "kind": "freeze", "text": "Landings resumed — the field is clear."}
    else:
        row = {"radio": "", "kind": "unknown", "text": "%s %s." % (who, str(act or "event"))}

    row["num"] = num
    row["tier"] = tier(rec)      # comms vs routine bookkeeping (issue #36) — the client binds visibility
    return row


# =============================== the "since you last looked" divider (§4) ===============================

def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def apply_divider(rows, last_seen):
    """Mark the "since you last looked" boundary on a chronological ``rows`` list (design record §4).

    Only ``comms`` rows can be fresh or carry the divider: "since you last looked" is a real-traffic
    signal, and routine bookkeeping is hidden by default (issue #36), so a fresh routine row would
    both fake "new radio traffic" and anchor the divider to a row the reader cannot see. A row with
    no ``tier`` is treated as comms (backward-compatible with pre-#36 callers).

    Each comms row gains ``fresh`` (its ``ts`` is newer than the persisted ``last_seen`` watermark);
    the FIRST fresh comms row also gains ``divider: True`` — the single line the client draws to
    separate what arrived while William was away from what he had already seen. ``last_seen`` of
    ``None`` (a first-ever look) marks nothing fresh and draws no line. A non-finite ``ts`` (a corrupt
    NaN) is never fresh — that comparison is meaningless — and never raises. Returns the count of
    fresh comms rows (the client's "N new since you last looked" badge). Mutates ``rows`` in place."""
    count = 0
    drawn = False
    for r in rows:
        ts = r.get("ts")
        fresh = (last_seen is not None and _finite(ts) and ts > last_seen
                 and r.get("tier", "comms") == "comms")
        r["fresh"] = bool(fresh)
        if fresh:
            count += 1
            if not drawn:
                r["divider"] = True
                drawn = True
    return count
