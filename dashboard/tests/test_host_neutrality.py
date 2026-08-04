"""Issue #310 — the dashboard names no session host, anywhere in the product.

Its DoD box: *no dashboard code references cmux once the mini lane is the target*. The owner's
2026-08-04 ruling made that stricter and simpler. The dashboard does not swap one host name for
another — it names **no host at all**:

    The Open-session-window button does NOT land from this branch, and the dashboard never names
    the host. […] The one-doorway fence test stays exactly as strict as it is; no dashboard paths
    enter ``_ALLOWED``. […] the dashboard is NOT a token holder; the engine verb is the doorway.

So this is the dashboard-side mirror of the engine's fence
(``skills/superlooper/tests/test_one_session_host_door.py``), and it is deliberately blunter,
because the dashboard's position is blunter: the engine has exactly ONE door to the host, and the
dashboard has NONE. Every host-shaped verb it offers — Tidy, Restart, Liftoff, the Fixer — reaches
the host by shelling the ``superlooper`` CLI, which is the only thing here that knows what a session
window even is.

Two things rest on that:

* **The host swap costs the dashboard nothing.** #308 moved the engine's spawners onto the wrapper;
  no line in this repo's dashboard had to change with them, and this test is why that stays true.
* **The copy cannot go stale into a lie.** A dialog that says "restarts itself in its own cmux tab"
  was true when it was written and is false under the ``login-item`` home (#306) — and would be
  false again under a new host. Host-neutral words ("its own session window") survive both.

**Two exclusions, both deliberate.** ``tests/`` may name a host: the conftest neutralizes a binary
literally called ``cmux`` (the "no test reaches a real external binary" ratchet has to spell what it
is neutralizing), the fakes model a CLI's real output, and this file has to quote what it forbids.
``docs/`` and ``design/`` may too: they are the settled design record and its source uploads —
history, not a claim the product makes today.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Every host this repo has ever driven or plans to. The point of the list is that it does not matter
# WHICH one is current — the dashboard is not allowed to have an opinion about any of them.
#
# Only names that are PROPER NOUNS. GNU ``screen`` is a terminal multiplexer too, and it is
# deliberately absent: "screen" is also an ordinary English word this product says constantly (the
# owner's screen, off-screen, screen reader), so listing it would make the ratchet fire on 21 files
# that name no host at all — and a guard that cries wolf gets weakened until it means nothing.
_HOSTS = ("cmux", "herdr", "tmux", "iterm", "wezterm", "kitty")


def _product_surface():
    """Every shipped file of the dashboard: the pixels, the pure cores, the entry points, the page
    templates, and the config the owner copies. Not ``tests/``, not ``docs/``, not ``design/``."""
    return (sorted(_ROOT.glob("static/*"))
            + sorted(_ROOT.glob("lib/*.py"))
            + sorted(p for p in _ROOT.glob("bin/*") if p.is_file())
            + sorted(_ROOT.glob("templates/*"))
            + [_ROOT / "config.example.json"])


def test_the_ratchet_actually_sweeps_the_product():
    # A structural guard that silently stops finding files is worse than no guard: it reports green
    # forever. Pin the floor so a moved directory fails here rather than passing vacuously.
    surfaces = _product_surface()
    assert len(surfaces) > 30, (
        "the host-neutrality ratchet is sweeping %d files — it must cover the whole product"
        % len(surfaces))
    names = {p.name for p in surfaces}
    for expected in ("shell.js", "server.py", "flights.py", "command-center", "liftoff"):
        assert expected in names, "%s must be inside the swept surface" % expected


def test_no_dashboard_product_file_names_a_session_host():
    offenders = {}
    for path in _product_surface():
        text = path.read_text(encoding="utf-8").lower()
        hit = [h for h in _HOSTS if re.search(r"\b%s\b" % h, text)]
        if hit:
            offenders[path.name] = hit
    assert not offenders, (
        "these dashboard files name a session host: %s\n"
        "The dashboard names no host (owner ruling 2026-08-04, issue #310): every host-shaped verb "
        "here shells the `superlooper` CLI, which owns the one doorway. Say 'session window', not "
        "the host's name — that sentence stays true through a host swap, and this one will not."
        % offenders)


def test_the_ratchet_would_catch_a_reintroduced_host_name():
    # The meta-test: prove the sweep can actually fail, so it cannot rot into a green that means
    # nothing (the failure mode structural guards die of). Same discipline as the engine fence's
    # `test_fence_flags_*` meta-tests.
    for host in _HOSTS:
        sample = "// launches one session in its own %s tab\n" % host
        assert [h for h in _HOSTS if re.search(r"\b%s\b" % h, sample.lower())] == [host]
    # …and that the neutral phrasing the product actually uses is NOT an offence.
    neutral = "// launches one interactive sl-debugger session in its own session window\n"
    assert not [h for h in _HOSTS if re.search(r"\b%s\b" % h, neutral.lower())]
