"""The standing truth strip (issue #166) — the line that stops the dashboard from silently lying.

**What went wrong.** The owner read this surface for weeks as a live, perfect mirror of the runner.
It never was one. A dead session rendered as "launching"; a day-stale server sat serving under fresh
UI; an externally-closed issue was never absorbed. Not one of those was a crash — the dashboard was
CONFIDENT while blind, which is the only failure mode a monitoring surface has that costs more than
being down. A dashboard that is obviously broken gets fixed in a minute. One that is quietly wrong
gets believed for weeks.

So this strip states, always and unasked, the three facts that decide how much of the screen to
believe:

  * **is the loop alive?** — the runner's last tick, and the word "loop may be down" when it's stale
  * **whose truth is this?** — the runner's own published view, or a blind/second-hand one
  * **is the merged fix actually running?** — the engine's publish drift (``lib/engine``)

**It DERIVES nothing it could get wrong.** Whether the runner is silent is decided ONCE, in
``flights.source_mode``; whether the engine is behind is decided once, in ``engine.drift``. This
module reads those verdicts and composes words. If it second-guessed either, the strip and the board
could tell two different stories — which is the original bug wearing a new hat. (Pinned by
``test_the_strip_never_derives_the_mode_itself``.)

**Unknown is never an all-clear.** Every degraded input — no source verdict, a junk dict, an age
nobody can compute — resolves toward the honest alarm, never toward silence or a confident zero.
That asymmetry is the whole design: a false "all good" is what this issue exists to end, and it is
the one direction this module may never fail in.

**Silence is still allowed where it is honest** (§0.2 — no nagging). A live engine says nothing; a
healthy tick states its age calmly and raises no alarm. A strip that shouted constantly would be
wallpaper inside a week, and then the one time it mattered the owner wouldn't see it — which is
precisely how the fallback banner would become invisible again.

Design record B.1: pure semantics here, pixels in ``static/field.js``. The squint test — delete the
art and this dict still states the whole situation.
"""
import flights

# Levels, worst-last. `down` is the loop itself; `notice` is something the owner should know but
# nothing is broken (drift, a blind data source while the loop still ticks).
LEVEL_OK = "ok"
LEVEL_NOTICE = "notice"
LEVEL_DOWN = "down"

# The conclusion, not just the number. "last tick 15m ago" makes the owner know the threshold to
# read it; naming what it MEANS is the difference between data and truth. "may be" is deliberate and
# honest — the dashboard watches the runner, it does not command it, and a stale heartbeat is strong
# evidence of a dead loop, never proof of one.
_MAY_BE_DOWN = "loop may be down"

# What an age reads as when there is no honest number. Never "0s ago", which would claim the
# freshest possible data at the exact moment we have none.
_UNKNOWN_AGE = "?"


def _age(src, key):
    """The server's own rendered age phrase for ``key``, or a plain "?" — never a fabricated
    number. ``flights.source_mode`` composes these (design B.1); a snapshot built without its
    duration formatter carries ``None``, which reads as unknown exactly like its own "?" does."""
    txt = src.get(key + "_age_text")
    return txt if isinstance(txt, str) and txt else _UNKNOWN_AGE


def _tick_line(src):
    """Is the loop alive? The runner's last tick, and the named conclusion when it is stale.

    The staleness DECISION is the server's, taken from ``source_mode``'s reason — never re-derived
    here against a second threshold that could disagree with the board's.
    """
    tick_age = src.get("tick_age")
    numeric = isinstance(tick_age, (int, float)) and not isinstance(tick_age, bool)

    # No heartbeat at all. Deliberately not "last tick ? ago" — a shrug where the alarm belongs.
    # This also catches every degraded input (no source verdict, a junk dict): unknown resolves to
    # the honest alarm, never to a calm silence.
    if not numeric:
        return {"state": "down", "text": "no tick seen — %s" % _MAY_BE_DOWN}

    # A runner that is TICKING but publishes no view (an engine older than #146) is not down, and
    # must never be called down: that would send the owner to debug a runner that is fine.
    if src.get("reason") == flights.FALLBACK_RUNNER_SILENT:
        return {"state": "down", "text": "last tick %s — %s" % (_age(src, "tick"), _MAY_BE_DOWN)}
    return {"state": "ok", "text": "last tick %s" % _age(src, "tick")}


def _data_line(src, github):
    """Whose truth is on screen, and how old. Three honest states, no fourth:

      * ``ok``    — the runner's own published view (what the owner always assumed he was reading)
      * ``dark``  — the tower is blind: no data link to GitHub, so nothing on screen is arriving
      * ``blind`` — showing GitHub directly: real data, but a SECOND opinion on a stale premise,
                    which is the exact thing that must never pass for the runner's own view
    """
    age = _age(src, "data")

    # Dark tower first: it outranks "second opinion", because a fallback that can't reach GitHub
    # either isn't showing a second opinion — it is showing nothing at all, and a calm "data 30s
    # ago" over a picture that is going nowhere is the confident blank this issue exists to kill.
    #
    # The age is deliberately DROPPED here rather than passed through. source_mode dates fallback by
    # when the dashboard's own fetch last RAN — which, with the link down, is a read that came back
    # empty. Rendering that as "data 12s ago" beside "the tower is blind" would date the screen by a
    # failure and hand the owner a freshness number for a layer holding nothing at all. Caught by
    # driving it in a browser, not by a test: the two clauses only contradict each other out loud.
    if isinstance(github, dict) and github.get("unreachable"):
        return {"state": "dark",
                "text": "data %s · can't reach GitHub — the tower is blind" % _UNKNOWN_AGE}

    if src.get("mode") == flights.SOURCE_LIVE:
        return {"state": "ok", "text": "data %s" % age}
    return {"state": "blind", "text": "data %s · GitHub direct — not the runner's view" % age}


def _engine_line(eng):
    """Is the engine the loop is RUNNING the one that was merged? ``None`` when there is honestly
    nothing to say — a live engine, or no engine source to compare against.

    Both non-silent cases carry the SERVER's own sentence (``lib/engine`` composed it, next to the
    arithmetic that justifies it), so the words and the count can never drift apart.
    """
    if not isinstance(eng, dict) or not eng.get("message"):
        return None                       # up to date, or nothing published here — silence is honest
    behind = eng.get("behind")
    known = eng.get("known") is True and isinstance(behind, int) and not isinstance(behind, bool)
    return {"state": "drift" if known else "unknown", "text": eng["message"],
            "behind": behind if known else None,
            "remedy": eng.get("remedy")}


def banner(source, engine=None, github=None):
    """The standing truth strip for one repo's field.

    ``source``  that repo's ``flights.source_mode`` verdict (the snapshot's ``repo.source``).
    ``engine``  the global ``lib.engine.EngineDrift`` state (the snapshot's ``engine``), or ``None``.
    ``github``  that repo's ``repo.github`` facts — read only for its ``unreachable`` flag.

    Returns ``{level, tick, data, engine}``. ``level`` is the worst of the lines, so one glance at
    the strip's colour is a true summary of everything under it. Never raises: it is built on the
    2-second poll, and a strip that could 500 the snapshot would take down the field the owner
    actually came for.
    """
    src = source if isinstance(source, dict) else {}
    tick = _tick_line(src)
    data = _data_line(src, github)
    eng = _engine_line(engine)

    level = LEVEL_OK
    if tick["state"] == "down":
        level = LEVEL_DOWN                # a dead loop is the headline; everything else can wait
    elif data["state"] in ("blind", "dark") or eng is not None:
        level = LEVEL_NOTICE
    return {"level": level, "tick": tick, "data": data, "engine": eng}
