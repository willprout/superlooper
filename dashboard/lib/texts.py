"""The owner-text channel's age (issue #495) — when did a text last reach the phone?

**Why this exists.** The engine's morning report texted every day, and that daily text was the one
proof the notify channel still worked. The owner ruled (2026-09-16) that the report texts only when it
has news, and declined a heartbeat text in its place: a dead channel only matters when there is work,
and he looks at this dashboard when he approves work. So the proof moved from the phone to here — an
AGE, stated on the standing truth strip.

**Where the fact comes from.** The engine's notify doorway journals every text it attempts as a
``notify_canary`` act: ``ok`` (delivered), ``channel`` (whichever channel the engine chose, or
``log-only`` when none is configured), ``rc``, ``tier`` and ``caller``. Older records (the pre-#495
morning canary) carry the same first three fields, so they read the same way. This module reads those
records out of the journal the snapshot already loads — no new read of any kind.

**The verdict** mirrors the engine's own (``report.notify_canary`` + ``report.CHANNEL_STALE_SECONDS``),
because the morning report file and this strip must not tell the owner two different things about one
channel. It is mirrored rather than imported: the dashboard never imports the engine.

  * ``delivered``    — the latest attempt worked, and a text reached the phone within the week
  * ``stale``        — nothing delivered in more than a week (with the last delivery's age when the
                       journal still holds one, without it when the journal is a week old and holds none)
  * ``unproven``     — no delivery on record, and the journal is too young to say "a week"; also a
                       delivery whose time cannot be trusted (stamped in the future by a clock jump)
  * ``dead``         — the latest attempt failed
  * ``unconfigured`` — the latest attempt was log-only: no channel is configured at all

**Fail closed.** A record that cannot be read proves nothing: ``ok`` must be exactly ``True``, the ts
must be a finite number, and the channel a real one. Junk degrades toward ``unproven``, never toward
``delivered``. Pure, and never raises — it rides the 2-second poll.
"""
import math

# Mirrors the engine's report.CHANNEL_STALE_SECONDS (a week). Past this with no delivered text, the
# channel is unproven, and the strip says so.
STALE_SECONDS = 7 * 24 * 3600

DELIVERED = "delivered"
STALE = "stale"
UNPROVEN = "unproven"
DEAD = "dead"
UNCONFIGURED = "unconfigured"
STATES = (DELIVERED, STALE, UNPROVEN, DEAD, UNCONFIGURED)

CANARY_ACT = "notify_canary"
_LOG_ONLY = "log-only"
# "Channels" on which nothing reached anyone: no channel configured, or a refusal that never got to one.
_NOT_A_DELIVERY = frozenset({_LOG_ONLY, "refused"})


def _finite(v):
    """A real, finite number. ``OverflowError`` is caught because ``json.loads`` parses an arbitrarily
    large integer and ``math.isfinite`` RAISES on one (the #458 shape)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _channel(rec):
    c = rec.get("channel")
    return c if isinstance(c, str) and c else None


def last_text(journal, now, fmt=None):
    """The channel verdict for one repo's journal.

    ``journal`` the journal records (``readers.read_journal``), oldest first.
    ``now``     the snapshot clock.
    ``fmt``     an injectable seconds → duration formatter (the server passes ``format_duration``).

    Returns ``{state, channel, rc, delivered_age, delivered_age_text, delivered_channel}``:
    ``channel``/``rc`` describe the LATEST attempt (what a dead channel names); the ``delivered_*``
    fields describe the newest DELIVERED text, whatever came after it."""
    records = [r for r in journal if isinstance(r, dict)] if isinstance(journal, list) else []
    clock = now if _finite(now) else None

    latest, latest_ts = None, None
    delivered_ts, delivered_channel = None, None
    oldest = None
    for rec in records:
        ts = rec.get("ts")
        timed = _finite(ts)
        if timed and (oldest is None or ts < oldest):
            oldest = ts
        if rec.get("act") != CANARY_ACT:
            continue
        # the latest attempt: newest readable ts; an untimed record never outranks a timed one
        if latest is None or (timed and (latest_ts is None or ts >= latest_ts)):
            latest, latest_ts = rec, (ts if timed else latest_ts)
        channel = _channel(rec)
        if (timed and rec.get("ok") is True and channel is not None
                and channel not in _NOT_A_DELIVERY
                and (delivered_ts is None or ts >= delivered_ts)):
            delivered_ts, delivered_channel = ts, channel

    age = None
    if delivered_ts is not None and clock is not None and clock - delivered_ts >= 0:
        age = clock - delivered_ts
    rc = latest.get("rc") if isinstance(latest, dict) else None
    out = {"channel": _channel(latest) if isinstance(latest, dict) else None,
           "rc": rc if isinstance(rc, int) and not isinstance(rc, bool) else None,
           "delivered_age": age,
           "delivered_age_text": ("%s ago" % fmt(age)) if (fmt is not None and age is not None)
           else None,
           "delivered_channel": delivered_channel if age is not None else None}

    if isinstance(latest, dict) and _channel(latest) == _LOG_ONLY:
        out["state"] = UNCONFIGURED
    elif isinstance(latest, dict) and latest.get("ok") is not True:
        out["state"] = DEAD
    elif age is not None:
        out["state"] = DELIVERED if age <= STALE_SECONDS else STALE
    elif delivered_ts is not None:
        out["state"] = UNPROVEN          # a delivery we cannot age (future-stamped, or no clock)
    elif clock is not None and oldest is not None and clock - oldest > STALE_SECONDS:
        out["state"] = STALE             # a week of journal and not one delivered text in it
    else:
        out["state"] = UNPROVEN
    return out
