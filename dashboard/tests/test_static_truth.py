"""Guard (issue #166): the standing truth strip reaches the screen, and never lies quietly.

The strip's SEMANTICS are unit-tested in ``test_truth`` and its drift arithmetic in ``test_engine``.
This file defends the seam between those verdicts and the pixels — the part no Python test can see.
Like the other field guards (#22/#27/#30/#32/#35/#38/#45/#146), these are STRING checks on the
shipped static bundle: the repo runs no JS engine (Python stdlib only), so a rendered assertion is
impossible and a seam check is what CI can honestly enforce. The proof that it LOOKS right — and
that a healthy field is still a joy to look at (§0.1) — is the PR's screenshot evidence.

The failures worth pinning here are all the same shape: the strip going QUIET when it should speak.
A blank strip, a strip hidden by the runner-down CSS takeover, or a strip whose "down" state renders
identically to "all good" would each rebuild the original bug — a dashboard that looks confident
while blind.
"""
import re
import sys
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def _boring_binder():
    m = re.search(r"function updateBoringTruth\(\)\s*\{(.*?)\n  \}", _SHELL, re.S)
    assert m, "shell.js must bind boring mode's truth strip"
    return m.group(1)


# --------------------------- the strip is mounted and bound ---------------------------

def test_the_field_mounts_the_truth_strip():
    assert "fld-truth" in _FIELD, "field.js must mount the standing truth strip"


def test_the_strip_binds_the_servers_verdict_and_derives_nothing():
    # design B.1: the JS binds words, never derives them. If the strip picked its own staleness
    # threshold it could contradict the board sitting above it — the original bug, wearing a hat.
    assert re.search(r"\.truth\b", _FIELD), "field.js must read repo.truth"
    assert re.search(r"bindTruth", _FIELD), "field.js must bind the strip from the server's block"


def test_the_strip_renders_all_three_facts():
    # The three questions the strip exists to answer: is the loop alive, whose truth is this, and is
    # the merged engine fix actually running?
    #
    # Matched against the RENDER, not the declarations (raised in review): the first cut asserted
    # `"tick" in body`, which the binder's own `var tick = t.tick || {}` line satisfies — it would
    # have passed against a binder that declared all three and rendered none.
    binder = re.search(r"function bindTruth\(t\)\s*\{(.*?)\n  \}", _FIELD, re.S)
    assert binder, "bindTruth must exist"
    body = binder.group(1)
    for fact, expr in (("tick", r"esc\(tick\.text"), ("data", r"esc\(data\.text"),
                       ("engine", r"esc\(eng\.text\)")):
        assert re.search(expr, body), "the strip must actually RENDER its %s line" % fact


def test_the_level_class_reaches_the_strip():
    # One glance at the strip's colour must summarise everything under it.
    assert re.search(r"lvl-", _FIELD), "field.js must bind the server's level onto the strip"


# --------------------------- silence is the failure mode ---------------------------

def test_an_absent_verdict_falls_back_to_down_never_to_blank():
    # THE guard of this file. A snapshot with no truth block must not render a calm empty strip:
    # an absent verdict is not an all-clear. The binder's defaults must name the down state.
    binder = re.search(r"function bindTruth\(t\)\s*\{(.*?)\n  \}", _FIELD, re.S)
    body = binder.group(1)
    assert "'down'" in body or '"down"' in body, (
        "bindTruth must default to the DOWN state when the server said nothing — never a blank")
    assert re.search(r"lvl-'\s*\+\s*esc\(t\.level\s*\|\|\s*'down'\)", body), (
        "the strip's level must default to down, not to ok")


def test_the_runner_down_takeover_never_hides_the_strip():
    # RUNNER DOWN grays the field and hides the overlays. The strip is the thing that SAYS the loop
    # may be down — hiding it in the down state would silence the one sentence that matters most.
    takeover = re.search(r"\.fld-root\.down \.fld-overlays > ([^\{]+)\{", _CSS)
    assert takeover, "the runner-down overlay takeover rule must exist"
    assert ":not(.fld-truth)" in takeover.group(1), (
        "the truth strip must survive the runner-down CSS takeover")


def test_the_engine_line_is_escaped_like_every_other_bound_string():
    # The drift line carries a git-derived sha. Everything bound here goes through esc().
    binder = re.search(r"function bindTruth\(t\)\s*\{(.*?)\n  \}", _FIELD, re.S)
    body = binder.group(1)
    assert re.search(r"esc\(eng\.text\)", body), "the engine line must be escaped"


# --------------------------- the three levels must not look alike ---------------------------

def test_each_level_is_visually_distinct():
    # A "down" strip that renders identically to a healthy one is the bug this issue closes, drawn
    # in CSS instead of Python. Each level must paint its own ground or border.
    for lvl in ("lvl-notice", "lvl-down"):
        block = re.search(r"\.fld-truth\.%s\s*\{([^}]*)\}" % lvl, _CSS)
        assert block, "shell.css must style .fld-truth.%s" % lvl
        assert re.search(r"background|border", block.group(1)), (
            ".fld-truth.%s must paint its own ground/border — it cannot read as the healthy state"
            % lvl)


def test_the_healthy_strip_stays_quiet():
    # §0.1: joy is terminal, and a permanent alarm taxes every glance at a healthy field. The OK
    # state must NOT carry the down state's pulse — the escalation is what keeps the shout credible.
    down = re.search(r"\.fld-truth\.lvl-down\s*\{([^}]*)\}", _CSS)
    assert "animation" in down.group(1), "the down state earns the eye — it should pulse"
    base = re.search(r"^\.fld-truth\s*\{([^}]*)\}", _CSS, re.MULTILINE)
    assert base, "shell.css must style the base .fld-truth"
    assert "animation" not in base.group(1), (
        "the resting strip must not pulse — a permanent alarm is wallpaper by next week")


def test_the_strip_keeps_the_joy_corner_free():
    # The painted incident sign ("N landings since the last incident") owns bottom-RIGHT and is a §7
    # joy element. Covering it for plumbing would trade delight for telemetry — §0.1 forbids it.
    base = re.search(r"^\.fld-truth\s*\{([^}]*)\}", _CSS, re.MULTILINE)
    assert "left:" in base.group(1), "the strip lives bottom-LEFT; bottom-right is the joy corner"


def test_reduced_motion_is_honored():
    assert re.search(r"prefers-reduced-motion[^}]*\}[^{]*\{[^}]*\}", _CSS)
    block = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{([^}]*\})", _CSS)
    assert any("fld-truth" in b for b in block), (
        "the strip's pulse must be disabled under prefers-reduced-motion")


# ============ boring mode's strip reaches the screen too (issue #180) ============
# The strip above lives in the FIELD overlays, and boring mode (screen 8c) has no field — so for
# three and a half minutes (the 90s strip threshold vs the 300s RUNNER DOWN banner) boring mode
# rendered a confident table with nothing qualifying it. These guard the same seam on the other view.

def test_boring_mode_mounts_a_truth_strip():
    assert "btruth" in _SHELL, "boring mode must mount the whole-field truth strip"
    assert re.search(r'id="btruth"', _SHELL), "the strip must be in boring mode's skeleton"


def test_boring_modes_strip_is_bound_on_every_poll():
    # A strip built into the skeleton but never re-bound would freeze at its first value — a strip
    # that stops updating is a strip that lies, which is worse than not having one.
    render = re.search(r"if \(state\.boring\) \{(.*?)\n    \} else \{", _SHELL, re.S)
    assert render, "the boring render branch must exist"
    assert "updateBoringTruth()" in render.group(1), (
        "boring mode must re-bind its truth strip on every poll, not just at skeleton build")


def test_boring_modes_strip_renders_the_tick_the_data_and_the_engine():
    # DoD items 1 and 3. Matched against the RENDER, not the declarations — the #166 review caught a
    # first cut that asserted on `var tick = ...` lines a binder could satisfy while rendering none.
    body = _boring_binder()
    for fact, expr in (("tick", r"esc\(tick\.text"), ("data", r"esc\(data\.text"),
                       ("engine", r"esc\(t\.engine\.text\)")):
        assert re.search(expr, body), "boring mode's strip must actually RENDER its %s line" % fact


def test_boring_modes_strip_names_each_repo():
    # DoD item 2, at the seam. Worst-of on the level, exact per-repo on the words — an alarm with no
    # repo name on it sends the owner to check the wrong runner and hides that the other one is fine.
    body = _boring_binder()
    assert re.search(r"esc\(r\.name\)", body), (
        "each repo's row must carry its name — an unattributable alarm is barely an alarm")


def test_boring_modes_strip_binds_the_servers_verdict_and_derives_nothing():
    # design B.1, and the reason this can't drift from the shell's strip: it reads snapshot.truth,
    # which lib/truth.whole_field builds from each repo's OWN banner verdict by reference.
    body = _boring_binder()
    assert re.search(r"state\.snapshot\.truth", body), "boring mode must read the server's block"
    assert not re.search(r"\d{2,}\s*\*\s*1000|Date\.now|/\s*60", body), (
        "the strip must pick no threshold and format no age of its own")


def test_boring_modes_absent_verdict_falls_back_to_down_never_to_blank():
    # THE guard, ported. A snapshot with no truth block must not render a calm empty strip over a
    # confident table: an absent verdict is not an all-clear.
    body = _boring_binder()
    assert re.search(r'lvl-"\s*\+\s*esc\(t\.level\s*\|\|\s*"down"\)', body), (
        "the strip's level must default to down, not to ok")
    assert "loop may be down" in body, (
        "the no-verdict fallback must name the down state rather than render nothing")


def test_every_level_the_server_can_emit_has_a_boring_mode_colour():
    # The vocabulary ratchet (raised in review). Both strips style ONE class per level over a base
    # that IS the healthy ground, so a level with no rule renders CALM — the false all-clear, arriving
    # through a CSS gap instead of a Python one. lib/truth clamps unknown levels to `down`, which
    # protects today; this protects TOMORROW, when someone adds LEVEL_WARN server-side and the strip
    # silently paints it green on the way to picking its colour.
    #
    # LEVEL_OK is deliberately exempt: the base rule IS the healthy state, so it needs no override.
    # Covers .fld-truth too — the field strip has the identical latent gap, and this is the one place
    # that can see both vocabularies at once.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import truth

    non_ok = [v for k, v in vars(truth).items()
              if k.startswith("LEVEL_") and v != truth.LEVEL_OK]
    assert non_ok, "the level vocabulary must be discoverable, or this ratchet is a no-op"
    for strip in (".btruth", ".fld-truth"):
        for lvl in non_ok:
            assert re.search(r"%s\.lvl-%s\s*\{" % (re.escape(strip), re.escape(lvl)), _CSS), (
                "%s has no rule for level %r — it would render as the healthy state" % (strip, lvl))


def test_boring_modes_levels_are_visually_distinct_without_animation():
    # The field strip pulses to earn the eye. Boring mode may not — "boring mode is fully static, no
    # exceptions" is an owner ruling and `.boring *` kills every animation. So each level must paint
    # its own ground/border, or "down" renders identically to healthy: the bug this issue closes.
    for lvl in ("lvl-notice", "lvl-down"):
        block = re.search(r"\.btruth\.%s\s*\{([^}]*)\}" % lvl, _CSS)
        assert block, "shell.css must style .btruth.%s" % lvl
        assert re.search(r"background|border", block.group(1)), (
            ".btruth.%s must paint its own ground/border — it cannot read as the healthy state" % lvl)
        assert "animation" not in block.group(1), (
            "boring mode is fully static (owner ruling) — the strip earns the eye without motion")
