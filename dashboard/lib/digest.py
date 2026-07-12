"""Mechanical morning digest (Task 11 / design record §4) — the load-bearing "what happened".

Where the night replay is a treat (see :mod:`replay`), the digest is the honest, mechanical answer
to "what did my field do while I was away." It is a **plain account, no AI, no composed prose**
(§9 kill list): mechanical counts, then ONE honest sentence per exception (a park, a go-around, a
freeze arc, a missed approach), over a **timestamped, clickable event table** — every row and every
exception carries the flight number and the raw journal line, so a tap opens the drawer or expands
ground truth.

Everything here is a PURE function of a journal (a list of records) plus an injected ``hhmm``
formatter — no wall clock, no I/O, no ``gh``. The disciplines:

* **The window is the honest scope.** Counts, exceptions and the table are all computed over the
  same ``[start, end]`` slice; a record with no usable ts can't be placed on the timeline and is
  dropped (mirrors the tolerant journal reader).
* **One sentence per exception, real words (§7 costume rule 2, §9 no composed prose).** Each
  sentence is a template over the record's own fields (memo, conflict count, freeze duration) — a
  fact rendered plainly, never a generated narrative.
* **Clickable ground truth (§4).** Both the event table and the exception list carry ``num`` (→ the
  flight drawer) and ``raw`` (→ the exact journal line), so the mechanical account never asks you to
  take its word.
"""
import json
import math
import time

import tower


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _local_hhmm(ts):
    """Local wall-clock ``HH:MM`` for a ts, or ``""`` when unusable — the default when no formatter
    is injected. Guarded against a corrupt ``NaN``/``Infinity`` ts (``time.localtime`` overflows on
    an infinite ts), so one bad journal line never crashes the digest."""
    if not _finite(ts):
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return ""


def _dur(seconds):
    """A compact floored duration (``"41m"``, ``"1h"``), or ``""`` when unusable. Floored, never
    rounded up — a freeze arc never reads longer than it was (§5/§7 honesty)."""
    if not _finite(seconds):
        return ""
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh" % (s // 3600)


def _rec_num(rec):
    """The flight a record is about — ``num``, else ``id`` (``i23`` → 23), else an ``event.id``
    envelope; ``None`` for a repo-wide record."""
    n = rec.get("num")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    for src in (rec.get("id"), (rec.get("event") or {}).get("id")
                if isinstance(rec.get("event"), dict) else None):
        if isinstance(src, str) and src.startswith("i") and src[1:].isdigit():
            return int(src[1:])
    return None


def _first_line(text, limit=90):
    """The first non-empty line of a (possibly multi-line) memo/question, trimmed — the digest
    sentence shows the gist; the raw line under it carries the whole thing."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return (line[:limit - 1].rstrip() + "…") if len(line) > limit else line
    return ""


def _who(num):
    return "SL-%d" % num if num is not None else "A flight"


def _window(journal, start, end):
    """The records inside ``[start, end]`` (inclusive; ``None`` = unbounded) with a usable ts,
    sorted ascending. Non-dict lines and non-finite ts are dropped."""
    out = []
    for rec in journal:
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if not _finite(ts):
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("ts"))
    return out


# =============================== counts ===============================

def _is_landing(rec):
    return rec.get("act") == "merge" and rec.get("outcome") == "ok"


def _counts(records):
    """Mechanical tallies over the window — the plain top-line the digest leads with."""
    c = {"departures": 0, "landings": 0, "wandered": 0, "go_arounds": 0, "parks": 0,
         "holds": 0, "missed_approaches": 0, "freezes": 0, "alerts": 0}
    for r in records:
        act = r.get("act")
        if act == "launch":
            c["departures"] += 1
        elif act == "merge":
            if r.get("outcome") == "ok":
                c["landings"] += 1
                if r.get("wander"):
                    c["wandered"] += 1
            else:
                c["missed_approaches"] += 1
        elif act == "regenerate":
            c["go_arounds"] += 1
        elif act == "park":
            c["parks"] += 1
        elif act == "hold":
            c["holds"] += 1
        elif act == "freeze":
            c["freezes"] += 1
        elif act == "alert":
            c["alerts"] += 1
    return c


# =============================== exceptions — one sentence each ===============================

def _exception(rec, num, hhmm, operator="the owner"):
    """A single non-freeze exception record → its ``{ts, hhmm, num, kind, sentence, raw}``, or
    ``None`` if the record is not, on its own, an exception. Freeze arcs are handled separately (they
    pair two records). Each sentence is a plain template over the record's own fields. ``operator``
    is the configured operator display name (issue #58) — a needs-decision line names the owner."""
    act = rec.get("act")
    ts = rec.get("ts")
    at = hhmm(ts)
    who = _who(num)
    kind = sentence = None

    if act == "park":
        if rec.get("needs_william"):
            kind = "awaiting"
            memo = _first_line(rec.get("memo")) or "a decision waits on you"
            sentence = "%s needs %s at %s — %s." % (who, operator, at, memo)
        else:
            kind = "park"
            memo = _first_line(rec.get("memo")) or "the machine gave up"
            sentence = "%s parked at %s — %s." % (who, at, memo)
    elif act == "bounce":
        kind = "awaiting"
        memo = _first_line(rec.get("memo") or rec.get("reason")) or "a bounce awaits your call"
        sentence = "%s bounced at %s — %s." % (who, at, memo)
    elif act == "regenerate":
        kind = "go_around"
        n = rec.get("conflicts")
        tail = " (conflict #%s)" % n if isinstance(n, int) and not isinstance(n, bool) else ""
        sentence = "%s went around at %s — rebuilding from scratch%s." % (who, at, tail)
    elif act == "merge" and rec.get("outcome") != "ok":
        kind = "missed_approach"
        sentence = "%s missed the approach at %s — merge failed; the loop will retry." % (who, at)
    elif act == "merge" and rec.get("outcome") == "ok" and rec.get("wander"):
        kind = "wander"
        pr = rec.get("pr")
        pr_bit = " (PR #%s)" % pr if pr else ""
        sentence = "%s landed at %s%s but wandered outside its lane — see report." % (who, at, pr_bit)
    elif act == "alert":
        kind = "alert"
        sentence = "ALERT raised at %s — a factory-stop the runner declared." % at
    elif act == "event":
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        et = ev.get("type")
        if et == "autofix_failed":
            kind = "autofix_failed"
            sentence = "%s auto-repair failed at %s — the freeze needs a look." % (who, at)
        elif et == "runner_down":
            kind = "runner_down"
            sentence = "The runner heartbeat went stale at %s — the tower was unmanned." % at

    if kind is None:
        return None
    return {"ts": ts, "hhmm": at, "num": num, "kind": kind, "sentence": sentence,
            "raw": json.dumps(rec, separators=(",", ":"))}


def _freeze_arcs(records, hhmm):
    """Pair each ``freeze`` with the next ``unfreeze`` into one arc exception, sorted by the freeze's
    ts. An unresolved freeze (no unfreeze in the window) reads honestly as still paused. Each arc
    carries the freeze record's ``raw`` for click-through."""
    arcs = []
    open_freeze = None
    for rec in records:
        act = rec.get("act")
        if act == "freeze":
            if open_freeze is not None:
                arcs.append(_arc(open_freeze, None, hhmm))   # back-to-back freezes: close the first
            open_freeze = rec
        elif act == "unfreeze" and open_freeze is not None:
            arcs.append(_arc(open_freeze, rec, hhmm))
            open_freeze = None
    if open_freeze is not None:
        arcs.append(_arc(open_freeze, None, hhmm))
    return arcs


def _arc(freeze, unfreeze, hhmm):
    start_ts = freeze.get("ts")
    at = hhmm(start_ts)
    if unfreeze is not None:
        end_ts = unfreeze.get("ts")
        dur = _dur(end_ts - start_ts) if (_finite(start_ts) and _finite(end_ts)) else ""
        dur_bit = " (%s)" % dur if dur else ""
        sentence = "Landings paused at %s → resumed %s%s." % (at, hhmm(end_ts), dur_bit)
    else:
        sentence = "Landings paused at %s — still paused at the window's end." % at
    return {"ts": start_ts, "hhmm": at, "num": None, "kind": "freeze_arc",
            "sentence": sentence, "raw": json.dumps(freeze, separators=(",", ":"))}


def _ts_key(e):
    ts = e.get("ts")
    return ts if _finite(ts) else 0


# =============================== the event table ===============================

def _events(records, hhmm):
    """Every windowed record as a timestamped, glossed, clickable row — the same comms vocabulary
    the tower log uses (lib.tower), so the digest and the live feed read the same."""
    rows = []
    for rec in records:
        ts = rec.get("ts")
        g = tower.comms_row(rec)
        rows.append({"ts": ts, "hhmm": hhmm(ts), "num": g["num"], "kind": g["kind"],
                     "text": g["text"], "radio": g["radio"],
                     "raw": json.dumps(rec, separators=(",", ":"))})
    return rows


def build_digest(journal, *, slug="", name="", start=None, end=None, hhmm=None, operator="the owner"):
    """A journal (list of records) → the mechanical digest the front-end renders.

    ``start``/``end`` (epoch seconds, inclusive; ``None`` = unbounded) bound the window; ``hhmm``
    formats a ts to local ``HH:MM`` (injected so timezone never leaks into the lib). Returns
    ``{slug, name, empty, clean, window, counts, exceptions, events}``: ``counts`` the mechanical
    tallies, ``exceptions`` one sentence each (time-ordered), ``events`` the full timestamped table.
    ``clean`` is "no exceptions"; ``empty`` is "no events in the window"."""
    hhmm = _local_hhmm if hhmm is None else hhmm
    records = _window(journal, start, end)

    exceptions = _freeze_arcs(records, hhmm)
    for rec in records:
        exc = _exception(rec, _rec_num(rec), hhmm, operator)
        if exc is not None:
            exceptions.append(exc)
    exceptions.sort(key=_ts_key)

    events = _events(records, hhmm)
    return {
        "slug": slug,
        "name": name or slug,
        "empty": len(events) == 0,
        "clean": len(exceptions) == 0,
        "window": {
            "start": records[0]["ts"] if records else start,
            "end": records[-1]["ts"] if records else end,
            "count": len(events),
        },
        "counts": _counts(records),
        "exceptions": exceptions,
        "events": events,
    }
