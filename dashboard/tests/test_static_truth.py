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
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


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
    binder = re.search(r"function bindTruth\(t\)\s*\{(.*?)\n  \}", _FIELD, re.S)
    assert binder, "bindTruth must exist"
    body = binder.group(1)
    for fact in ("tick", "data", "eng"):
        assert fact in body, "the strip must render its %s line" % fact


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
