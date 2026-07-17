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

    # Dark tower first: it outranks "second opinion", because a source that can't reach GitHub isn't
    # showing a second opinion — it is showing nothing at all, and a calm "data 30s ago" over a
    # picture that is going nowhere is the confident blank this issue exists to kill.
    #
    # Whether the age survives depends on WHOSE read went dark, and the two are genuinely different:
    #
    #   * FALLBACK — the age is when the DASHBOARD's own fetch last RAN, and with the link down that
    #     read came back empty. "data 12s ago" beside "the tower is blind" would date the screen by a
    #     failure and hand the owner a freshness number for a layer holding nothing. Dropped.
    #     (Caught by driving a browser, not by a test — the two clauses only contradict out loud.)
    #   * LIVE — `unreachable` is the RUNNER's own stale flag, and the age is when the RUNNER last
    #     reached GitHub. That is a true and useful number ("the runner last got through 20m ago") —
    #     exactly what the owner needs while the link is down. Kept. (Raised in review: failing safe
    #     by blanking it would throw away honest information at the moment it matters most.)
    if isinstance(github, dict) and github.get("unreachable"):
        shown = age if src.get("mode") == flights.SOURCE_LIVE else _UNKNOWN_AGE
        return {"state": "dark",
                "text": "data %s · can't reach GitHub — the tower is blind" % shown}

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


# =============================== the whole field's truth (issue #180) ===============================
# Everything above answers "how much of THIS repo's field do I believe?" — and it only ever reached
# the shell view, because the strip lives in the field overlays and boring mode has no field.
#
# Boring mode carried the RUNNER DOWN banner, but that fires at `heartbeat_down_seconds` (300s) while
# the strip fires at `runner_silent_seconds` (90s). So for three and a half minutes boring mode
# rendered a confident table of flights with nothing on screen saying the data may be a picture of
# the past — the same silent lie #166 exists to close, just on the other view. (Pre-existing, not a
# regression: #146's freshness stamp was field-scoped in exactly the same way and #166 inherited the
# scope.) Engine drift never reached boring mode at all, so a merged-but-not-live engine fix was
# invisible there entirely.

_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_NOTICE: 1, LEVEL_DOWN: 2}


def _level(lvl):
    """A level from this module's own vocabulary, or ``down``.

    Load-bearing, and subtler than it looks (raised in review). The CSS styles ONE class per known
    level and the base ``.btruth`` IS the healthy ground, so an unrecognised level reaches the
    browser as ``lvl-<junk>``, matches no rule, and paints the CALM strip — a false all-clear
    arriving through the one door this module swears is bolted. Ranking junk as down is not enough:
    ``max`` returns the ELEMENT it ranked, so the junk string itself would survive to the class
    attribute. It has to be replaced here, at the boundary, not merely sorted correctly.

    Unreachable while ``banner`` is the only source (it emits exactly the three), which is precisely
    why it is worth pinning: the next level someone adds server-side must not render as "all fine"
    on the way to getting its colour. (Pinned by ``test_an_unknown_level_can_never_paint_the_calm_strip``
    and, at the seam, by ``test_every_level_the_server_can_emit_has_a_boring_mode_colour``.)

    The ``isinstance`` is not redundant: ``in`` on a dict HASHES, so an unhashable level (``[]``,
    ``{}``) would raise ``TypeError`` out of the one function whose entire job is junk defense —
    on the 2-second poll (raised in review).
    """
    return lvl if isinstance(lvl, str) and lvl in _LEVEL_RANK else LEVEL_DOWN


def _spoken(line):
    """Is this a line that actually SAYS something — a dict carrying non-empty ``text``?"""
    return isinstance(line, dict) and isinstance(line.get("text"), str) and bool(line["text"])


def _row(repo):
    """One repo's line in the whole-field strip, carrying that repo's OWN ``banner`` verdict.

    Nothing here is re-derived. The tick/data objects are passed through BY REFERENCE from the
    strip the shell already binds, so boring mode and the field cannot tell two different stories
    about the same runner — which would be the original bug wearing a third hat. (Pinned by
    ``test_boring_modes_strip_reuses_each_repos_own_verdict_never_a_second_opinion``.)

    A repo whose verdict is missing or junk gets a DOWN row, never a dropped one: a repo silently
    absent from the strip reads exactly like a repo that is fine.
    """
    repo = repo if isinstance(repo, dict) else {}
    # §7: the literal slug stays visible everywhere in boring mode, so it is the honest fallback for
    # an unnamed repo. An unattributable alarm is barely better than no alarm.
    name = repo.get("name") or repo.get("slug") or "?"
    t = repo.get("truth")
    # A line without WORDS is as useless as no line: `{"tick": {}}` would satisfy a bare isinstance
    # check and then render the binder's own fallback text under a level claiming all is well — the
    # level and the words contradicting each other on screen (raised in review).
    if not isinstance(t, dict) or not _spoken(t.get("tick")) or not _spoken(t.get("data")):
        t = banner(None)                  # no verdict is not an all-clear — degrade to the alarm
    return {"name": name, "level": _level(t.get("level")),
            "tick": t["tick"], "data": t["data"]}


def whole_field(repos):
    """The standing truth strip for boring mode (screen 8c) — every repo at once.

    ``repos``  the snapshot's repo slices, each read only for ``name``/``slug`` and its ``truth``
               block (this repo's ``banner`` above, already composed by the server).

    Returns ``{level, repos: [{name, level, tick, data}, ...], engine}``.

    **How the multi-repo case aggregates** (decided, not implied — DoD): *worst-of on the LEVEL,
    exact per-repo on the WORDS.*

      * The **level** is the worst repo's, exactly as ``banner`` takes the worst of its own lines —
        so one glance at the strip's colour is a true summary of everything under it, and a field
        where any loop may be dead is never a green field.
      * The **words** are NOT aggregated. A single worst-of sentence would say "loop may be down"
        without saying WHOSE loop — sending the owner to check the wrong runner while hiding that
        the other is fine. Boring mode's whole rule is every visual channel paired with an exact
        numeral (§4), so each repo keeps its own named row with its own number.
      * A per-row column on the flights table was rejected: truth is per-REPO, not per-FLIGHT. A
        column would repeat one sentence down every row of a repo and imply a per-flight freshness
        that does not exist.

    The **engine line is stated ONCE**, not once per repo: there is one installed engine behind
    every watched repo (the server hands the same drift to every ``banner``), so repeating it would
    be noise — and worse, a lie of shape, implying the drift were per-repo. Taken from the first
    repo that has one so a junk leading entry cannot silence a real drift.

    Never raises: it rides the 2-second poll, and a strip that could 500 the snapshot would take
    down the very table the owner came for. Nothing to report is not an all-clear — junk in, alarm
    out, like everything else here.
    """
    entries = list(repos) if isinstance(repos, (list, tuple)) else []
    rows = [_row(r) for r in entries]

    eng = None
    for r in entries:
        t = r.get("truth") if isinstance(r, dict) else None
        if isinstance(t, dict) and isinstance(t.get("engine"), dict):
            eng = t["engine"]
            break

    # No repos at all means no verdict, and an absent verdict has never been an all-clear here.
    # (Unreachable through real config — `repos` is required non-empty — but the asymmetry is the
    # one direction this module may never fail in, so it holds at the boundary too.)
    # Every row's level is already this module's own vocabulary (``_row`` → ``_level``), so ranking
    # here can never be handed a string it cannot place.
    level = LEVEL_DOWN
    if rows:
        level = max((r["level"] for r in rows), key=lambda l: _LEVEL_RANK[l])
        # Boundary defense, and dead against the server's own input: `banner` already promotes any
        # repo carrying an engine line to `notice`, so every row is ≥ notice by the time we get here
        # (proved by mutation in review — replacing this with `if False` broke no test). It stays for
        # a hand-built block that states drift under an `ok` level: a stated drift must colour the
        # strip, or the line and the ground contradict each other.
        if _LEVEL_RANK[level] < _LEVEL_RANK[LEVEL_NOTICE] and eng is not None:
            level = LEVEL_NOTICE          # drift alone is a notice, exactly as it is per-repo
    return {"level": level, "repos": rows, "engine": eng}
