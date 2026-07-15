"""The two honest modes (issue #146) — which source the dashboard is rendering, and saying so.

The owner has been reading this dashboard as a live mirror of the runner. It never was: it asked
GitHub its own questions on its own clock, so its board could (and did) disagree with the runner —
an externally-closed issue rendered open, a dead session rendered "launching". The fix is one
source of truth with an honest stamp, and a fallback that is impossible to mistake for the real
thing.

`flights.source_mode` is the pure decision: given the runner's published view and its heartbeat,
which mode are we in, since when has the runner been quiet, and what does the banner say. It is
pure so the whole mode contract — including the fresh -> stale -> fresh round trip — is tested
without a server, a clock, or gh.

The rule: LIVE needs BOTH a usable published view AND a fresh heartbeat. Either one missing means
we are not rendering the runner's truth, and the UI must say which.
"""
import pytest

import flights

SILENT_AFTER = 90


def _view(published_at=1000, **kw):
    v = {"published_at": published_at, "polled_at": published_at, "stale": False,
         "issues": {}, "titles": {}, "closed_nums": [], "prs": {}}
    v.update(kw)
    return v


_DEFAULT = object()   # a sentinel, so `view=None` in a test means a REAL absent view


def _mode(view=_DEFAULT, heartbeat_age=5.0, now=1000, heartbeat_epoch=995, **kw):
    return flights.source_mode(_view() if view is _DEFAULT else view,
                               heartbeat_age=heartbeat_age, heartbeat_epoch=heartbeat_epoch,
                               now=now, silent_after=SILENT_AFTER, **kw)


# --------------------------- LIVE ---------------------------

def test_a_fresh_heartbeat_and_a_published_view_is_live():
    m = _mode()
    assert m["mode"] == "live"
    assert m["reason"] is None
    assert m["banner"] is None, "LIVE is the normal state — it must not shout a banner"


def test_live_reports_the_age_of_the_data_it_renders():
    # The whole point of the stamp: the owner can always see how old this picture is. Data age is
    # measured from the runner's last GitHub READ (polled_at), not the tick that copied it out.
    m = _mode(view=_view(published_at=1000, polled_at=940), now=1000)
    assert m["data_age"] == 60


def test_live_reports_the_tick_timer():
    m = _mode(heartbeat_age=12.0)
    assert m["tick_age"] == 12.0


def test_data_age_is_unknown_when_github_was_never_read():
    # A runner that has never reached GitHub still publishes its document (marked stale) — but there
    # is no GitHub data in it to be "5s old". Ageing it by the PUBLISH stamp would claim freshness
    # for data that does not exist; the honest answer is unknown, which the field renders "data ?".
    m = _mode(view=_view(published_at=970, polled_at=None, stale=True), now=1000)
    assert m["data_age"] is None


# --------------------------- FALLBACK: the runner went quiet ---------------------------

def test_a_stale_heartbeat_switches_to_fallback():
    m = _mode(heartbeat_age=SILENT_AFTER + 1)
    assert m["mode"] == "fallback"
    assert m["reason"] == "runner-silent"


def test_the_threshold_is_a_boundary_not_a_cliff():
    # Exactly at the threshold is still LIVE; one second past it is not. Pinned so a future edit
    # can't quietly widen the window in which a silent runner still renders as live truth.
    assert _mode(heartbeat_age=SILENT_AFTER)["mode"] == "live"
    assert _mode(heartbeat_age=SILENT_AFTER + 0.001)["mode"] == "fallback"


def test_the_fallback_banner_names_both_facts():
    # The DoD's two facts: since WHEN the runner has been quiet, and that this data is now coming
    # from GitHub directly rather than from the runner.
    m = _mode(heartbeat_age=600, heartbeat_epoch=1_752_000_000, now=1_752_000_600,
              hhmm=lambda ts: "14:32")
    b = m["banner"]
    assert b is not None
    text = " ".join(b["lines"]).lower()
    assert "silent since 14:32" in text
    assert "github" in text and "directly" in text


def test_the_banner_says_since_when_using_the_runners_own_last_tick():
    # "Silent since" is the last tick we SAW, not the moment we noticed — the owner needs the real
    # start of the silence to judge it.
    seen = {}

    def hhmm(ts):
        seen["ts"] = ts
        return "09:05"

    _mode(heartbeat_age=600, heartbeat_epoch=1_752_000_000, now=1_752_000_600, hhmm=hhmm)
    assert seen["ts"] == 1_752_000_000


def test_a_runner_that_never_ticked_is_still_an_honest_fallback():
    # No heartbeat at all (never started, or the file is gone): we cannot name a silent-since time,
    # and must not invent one — but the mode is still fallback and still says where data comes from.
    m = _mode(heartbeat_age=None, heartbeat_epoch=None)
    assert m["mode"] == "fallback"
    assert m["reason"] == "runner-silent"
    assert m["silent_since"] is None
    assert "github" in " ".join(m["banner"]["lines"]).lower()


# --------------------------- FALLBACK: no view to render ---------------------------

def test_no_published_view_is_fallback_even_with_a_fresh_heartbeat():
    # A pre-#146 engine ticks happily but publishes nothing. The heartbeat is fresh, yet we are NOT
    # rendering the runner's truth — so this is fallback, and it is a DIFFERENT reason than silence.
    m = _mode(view=None, heartbeat_age=5.0)
    assert m["mode"] == "fallback"
    assert m["reason"] == "no-published-view"


def test_the_no_view_banner_does_not_claim_the_runner_is_silent():
    # It isn't — it's ticking. Claiming silence would send the owner to debug a healthy runner.
    m = _mode(view=None, heartbeat_age=5.0)
    text = " ".join(m["banner"]["lines"]).lower()
    assert "silent" not in text
    assert "github" in text and "directly" in text


def test_a_corrupt_view_is_fallback_never_rendered_as_truth():
    # Present but unreadable ({} from the reader). Fail closed: a view we can't parse is not a view.
    m = _mode(view={}, heartbeat_age=5.0)
    assert m["mode"] == "fallback"
    assert m["reason"] == "no-published-view"


@pytest.mark.parametrize("bad", ["not a dict", [], 7, True])
def test_a_wrong_typed_view_never_raises_and_never_reads_as_live(bad):
    m = _mode(view=bad, heartbeat_age=5.0)
    assert m["mode"] == "fallback"


def test_a_view_without_a_publish_stamp_is_not_a_usable_view():
    # published_at is what dates the data. Without it we cannot honestly age anything, so the
    # document is not a view we may render as live truth.
    m = _mode(view=_view(published_at=None), heartbeat_age=5.0)
    assert m["mode"] == "fallback"
    assert m["reason"] == "no-published-view"


# --------------------------- the round trip ---------------------------

def test_fresh_then_stale_then_fresh_returns_to_live_and_clears_the_banner():
    # The DoD's transition: the mode is a pure function of the CURRENT facts — nothing latches — so
    # a recovered runner returns to LIVE on the next poll, with no restart and no sticky banner.
    live = _mode(heartbeat_age=5.0)
    stale = _mode(heartbeat_age=SILENT_AFTER + 60)
    recovered = _mode(heartbeat_age=5.0)

    assert (live["mode"], stale["mode"], recovered["mode"]) == ("live", "fallback", "live")
    assert live["banner"] is None
    assert stale["banner"] is not None
    assert recovered["banner"] is None, "the banner must clear itself when the runner comes back"


def test_both_modes_always_carry_the_two_clocks():
    # "The UI always displays the age of the data it renders and a tick timer, in BOTH modes."
    for m in (_mode(heartbeat_age=5.0), _mode(heartbeat_age=SILENT_AFTER + 60)):
        assert "data_age" in m and "tick_age" in m


def test_fallback_data_age_reports_the_dashboards_own_poll_not_the_stale_view():
    # In fallback the data on screen came from the dashboard's OWN GitHub read. Ageing it by the
    # runner's abandoned document would claim the picture is far older than it is.
    m = _mode(view=_view(published_at=100), heartbeat_age=SILENT_AFTER + 60, now=1000,
              fetched_at=985)
    assert m["data_age"] == 15


def test_fallback_data_age_is_unknown_before_the_first_direct_poll_lands():
    # Never guess: no read yet means no honest age to show.
    m = _mode(view=None, heartbeat_age=SILENT_AFTER + 60, now=1000, fetched_at=None)
    assert m["data_age"] is None
