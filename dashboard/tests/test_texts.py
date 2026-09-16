"""The owner-text channel as a visible AGE (issue #495) — when did a text last reach the phone?

The morning report used to text every day, and that daily text was the only proof the notify channel
still worked. It now texts only when there is news (owner ruling 2026-09-16), and there is deliberately
no heartbeat text in its place: a dead channel only matters when there is work, and the owner looks at
this dashboard when he approves work. So the proof becomes an age he can see here.

The engine journals EVERY text it attempts as a ``notify_canary`` act — delivered, failed, or log-only
(no channel configured), whatever its tier and sender. ``lib/texts`` reads that back into a verdict;
``lib/truth`` words it on the standing strip. Pure: a journal and a clock in, a dict out.
"""
import math

import texts

NOW = 2_000_000
HOUR = 3600
DAY = 24 * HOUR


def _canary(ts, ok=True, channel="cmd", rc=0, detail="", tier="waiting"):
    return {"ts": ts, "act": "notify_canary", "ok": ok, "channel": channel, "rc": rc,
            "detail": detail, "tier": tier, "caller": "decide:park", "outcome": "ok"}


def _fmt(secs):
    return "%dh" % (secs // 3600)


def test_a_recent_delivery_is_delivered_with_its_age():
    v = texts.last_text([_canary(NOW - 3 * HOUR)], NOW, fmt=_fmt)
    assert v["state"] == texts.DELIVERED
    assert v["delivered_age"] == 3 * HOUR and v["delivered_age_text"] == "3h ago"
    assert v["delivered_channel"] == "cmd"


def test_a_text_of_any_tier_is_the_proof():
    j = [_canary(NOW - 5 * DAY, tier="morning"), _canary(NOW - HOUR, tier="down")]
    assert texts.last_text(j, NOW)["delivered_age"] == HOUR


def test_nothing_delivered_in_more_than_a_week_is_stale_and_keeps_its_age():
    v = texts.last_text([_canary(NOW - 9 * DAY)], NOW, fmt=_fmt)
    assert v["state"] == texts.STALE and v["delivered_age"] == 9 * DAY


def test_exactly_a_week_is_still_delivered():
    assert texts.last_text([_canary(NOW - texts.STALE_SECONDS)], NOW)["state"] == texts.DELIVERED


def test_a_journal_older_than_a_week_with_no_delivery_is_stale_without_an_age():
    j = [{"ts": NOW - 10 * DAY, "act": "merge", "num": 7, "outcome": "ok"}]
    v = texts.last_text(j, NOW)
    assert v["state"] == texts.STALE and v["delivered_age"] is None


def test_a_young_journal_with_no_delivery_is_unproven_never_stale():
    j = [{"ts": NOW - HOUR, "act": "merge", "num": 7, "outcome": "ok"}]
    assert texts.last_text(j, NOW)["state"] == texts.UNPROVEN
    assert texts.last_text([], NOW)["state"] == texts.UNPROVEN


def test_a_failed_latest_attempt_is_dead_and_still_ages_the_last_delivery():
    j = [_canary(NOW - 2 * DAY), _canary(NOW - 60, ok=False, rc=2, detail="recipient missing")]
    v = texts.last_text(j, NOW, fmt=_fmt)
    assert v["state"] == texts.DEAD and v["channel"] == "cmd" and v["rc"] == 2
    assert v["delivered_age"] == 2 * DAY and v["delivered_age_text"] == "48h ago"


def test_a_delivery_after_a_failure_is_healthy_again():
    j = [_canary(NOW - 2 * HOUR, ok=False, rc=2), _canary(NOW - HOUR)]
    assert texts.last_text(j, NOW)["state"] == texts.DELIVERED


def test_log_only_is_unconfigured_and_never_a_delivery():
    j = [_canary(NOW - 2 * DAY), _canary(NOW - 60, channel="log-only")]
    v = texts.last_text(j, NOW)
    assert v["state"] == texts.UNCONFIGURED
    only_log = texts.last_text([_canary(NOW - 60, channel="log-only")], NOW)
    assert only_log["delivered_age"] is None


def test_ok_must_be_exactly_true():
    # a hand-edited "ok": "true" must never read as a delivery
    v = texts.last_text([_canary(NOW - HOUR, ok="true")], NOW)
    assert v["state"] != texts.DELIVERED and v["delivered_age"] is None


def test_a_delivery_with_an_unusable_time_proves_nothing():
    for bad in (None, "yesterday", True, math.nan, math.inf, 10 ** 400):
        v = texts.last_text([_canary(bad)], NOW)
        assert v["delivered_age"] is None, bad
        assert v["state"] != texts.DELIVERED, bad


def test_a_delivery_stamped_far_in_the_future_is_unproven_not_fresh():
    v = texts.last_text([_canary(NOW + DAY)], NOW)
    assert v["state"] == texts.UNPROVEN and v["delivered_age"] is None
    assert v["delivered_untimed"] is True        # a delivery exists; only its age cannot be read


def test_a_desktop_toast_is_not_a_text_that_reached_the_phone():
    # cmux is the doorway's local fallback when no owner channel is configured (doctor --stack refuses
    # it as a channel): its delivery must never read as "last text delivered"
    v = texts.last_text([_canary(NOW - HOUR, channel="cmux")], NOW)
    assert v["delivered_age"] is None and v["state"] != texts.DELIVERED


def test_two_attempts_stamped_the_same_instant_read_the_later_journal_line():
    j = [_canary(NOW - HOUR), _canary(NOW - HOUR, ok=False, rc=2)]
    assert texts.last_text(j, NOW)["state"] == texts.DEAD


def test_junk_never_raises_into_the_two_second_poll():
    for junk in (None, "x", 7, [None, 3, "line", {"act": []}, {"act": "notify_canary", "ts": []}],
                 [{"act": "notify_canary", "ts": NOW, "ok": True, "channel": ["cmd"]}]):
        v = texts.last_text(junk, NOW)
        assert v["state"] in texts.STATES
    assert texts.last_text([_canary(NOW)], None)["state"] in texts.STATES


def test_every_state_is_named_in_the_vocabulary():
    assert set(texts.STATES) == {texts.DELIVERED, texts.STALE, texts.UNPROVEN, texts.DEAD,
                                 texts.UNCONFIGURED}


def test_a_text_journaled_moments_after_the_snapshot_clock_is_just_delivered():
    # the snapshot takes `now` before it reads the journal; a send landing in between is the same
    # moment, not a clock jump, and must not flicker the strip to a notice for a poll
    v = texts.last_text([_canary(NOW + 2)], NOW, fmt=_fmt)
    assert v["state"] == texts.DELIVERED and v["delivered_age"] == 0
