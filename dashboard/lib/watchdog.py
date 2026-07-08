"""The dead-man's switch (Task 10 / design record §6) — a pure per-repo edge detector.

The dashboard backend watches the runner's heartbeat; past ``heartbeat_down_seconds`` the surface
greys (RUNNER DOWN — a state stale data cannot fake) AND William gets *exactly one* push. The
greying is the flight model's job (``flights.repo_state`` → the snapshot's ``runner.repos[].down``);
THIS module owns the "exactly one" — it remembers which repos are in a down episode it has already
pushed for, so a runner that stays dead for half an hour still costs one push, never one every poll.

It is deliberately clock-FREE: it consumes the ``down`` boolean the snapshot already carries (a
single source of truth means the grey banner and the push can never disagree about whether the
runner is up). The clock lives upstream, in the heartbeat age that flips ``down`` — which is why
the switch is proven by fake-clock tests that advance ``now`` and count fires.

Thread-safe: the server evaluates the switch from request threads (``ThreadingHTTPServer``), so a
lock guards the fired-set against two concurrent down-edges double-firing. State is in-memory only:
a server restart re-arms every repo, so the WORST case after a restart is one extra push for a
still-down runner — never a MISSED one. A dead-man's switch fails toward notifying, never silent.
"""
import threading


class Watchdog:
    """Tracks, per repo slug, whether the current RUNNER DOWN episode has already been pushed."""

    def __init__(self):
        self._fired = set()
        self._lock = threading.Lock()

    def newly_down(self, runner_repos):
        """Return the repo entries that transitioned UP→DOWN since the last call — the ones needing
        a push now. A repo still down from a prior call returns nothing (no repeat nagging); a repo
        that has recovered re-arms (its slug leaves the fired set) so its next down fires again.

        ``runner_repos`` is the snapshot's ``runner.repos`` list; each entry needs ``slug`` and the
        already-computed ``down`` boolean (``heartbeat_age`` rides along for the push message)."""
        fired_now = []
        with self._lock:
            for r in runner_repos:
                slug = r.get("slug")
                if r.get("down"):
                    if slug not in self._fired:
                        self._fired.add(slug)
                        fired_now.append(r)
                    # already pushed this episode → stay silent
                else:
                    self._fired.discard(slug)   # recovered → re-arm for the next episode
        return fired_now
