"""Guard (issue #444): the painted landmarks under the downwind leg are the SERVER's verdict.

The airfield draws four landmarks below the working leg — ``▸ RECONCILE PT``, ``▸ BUILD ISLAND``,
``▸ REVIEW RIDGE``, ``▸ CI SHOALS`` — and the design record named them as that leg's real
sub-phases (§3). Three of the four stood dark for the whole life of this surface, and
``airfield_live.js`` said exactly why in its own comment: *"the runner journals no per-phase fact
that could honestly place a plane over them (known MVP data gap, §9)"*. Issue #443 closed that gap
engine-side, so this is the renderer's half.

The seam these guard: **which landmark is lit is a SEMANTIC, so it is derived in Python and the JS
only binds it** (design record B.1 — the squint test: delete the art and the JSON is still a correct
state diagram). ``lib/flights.field_landmarks`` is the rule, unit-tested per phase value in
tests/test_flights.py; ``lib/server`` publishes it as ``repo.field_landmarks``; and the two files
here must actually carry it to the pixels rather than re-deriving a second opinion in the browser —
CI runs no JS, so a derivation that moved back into the client would be untested forever.

Like the other field guards (issues #22/#27/#30/#32/#203/#204), these are STRING guards on the
shipped static bundle: this repo runs no JS engine in CI (Python stdlib only). The RENDERED proof
that the landmarks light for the right phase lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_LIVE = (_STATIC / "airfield_live.js").read_text(encoding="utf-8")
_FIELD = (_STATIC / "field.js").read_text(encoding="utf-8")
_SHELL_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def _strip_js_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


def _fn_body(code, name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", code)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i + 1:j]
    return ""


_LIVE_CODE = _strip_js_comments(_LIVE)
_FIELD_CODE = _strip_js_comments(_FIELD)


def test_the_binder_carries_the_server_s_lit_landmarks_into_the_engine():
    assert "field_landmarks" in _FIELD_CODE, (
        "field.js must bind repo.field_landmarks — the server's verdict on which landmark is TRUE")
    assert re.search(r"landmarks\s*:", _FIELD_CODE), (
        "field.js must pass the lit list into the engine model as `landmarks`")


def test_the_engine_binds_the_lit_list_and_derives_nothing():
    body = _fn_body(_LIVE_CODE, "landmarkFlags")
    assert body, "airfield_live.js must still define landmarkFlags() — the layout's lit list"
    assert "placementOf" not in body, (
        "landmarkFlags must not re-derive which landmark is true from the sprites — that semantic "
        "moved to lib/flights.field_landmarks, where it has a test per phase value (design B.1)")
    for phase in ("cross-reviewing", "pr-open", "report-posted"):
        assert phase not in _LIVE_CODE, (
            "the phase vocabulary must not appear in the pixels (%r) — the JS binds words and lit "
            "flags the server chose, it never maps a phase itself (design B.1)" % phase)


def test_the_engine_fails_soft_to_an_unlit_field():
    # A snapshot with no lit list (an embedder, a half-built model) must render no landmark rather
    # than throw in the rAF loop or invent one. The four painted signs are scenery when nothing is
    # honestly over them — the state this surface shipped with for its whole life.
    body = _fn_body(_LIVE_CODE, "landmarkFlags")
    assert re.search(r"\[\s*false\s*,\s*false\s*,\s*false\s*,\s*false\s*\]", body), (
        "landmarkFlags must fail soft to an all-dark list when the model carries none")


def test_the_four_landmarks_are_still_the_four_the_binder_draws():
    # The lit list is positional — the server hands back one flag per painted sign, in the order
    # field.js lays them out. If a sign is added or reordered here without moving
    # flights.FIELD_LANDMARKS with it, every landmark would light for the wrong phase.
    m = re.search(r"LM_LABELS\s*=\s*\[([^\]]*)\]", _FIELD_CODE)
    assert m, "field.js must declare LM_LABELS — the painted landmark row"
    labels = [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()]
    assert len(labels) == 4, "the leg has four painted landmarks; the lit list is one flag each"
    assert "RECONCILE" in labels[0] and "BUILD ISLAND" in labels[1]
    assert "REVIEW RIDGE" in labels[2] and "CI SHOALS" in labels[3], (
        "west→east order is load-bearing: it is the order a plane flies the leg, and the order "
        "lib/flights._PHASE_LANDMARK maps phases into")


def test_the_python_rule_and_the_painted_row_agree_on_the_order():
    import flights
    assert flights.FIELD_LANDMARKS == ("reconcile-pt", "build-island", "review-ridge", "ci-shoals")


def test_the_towed_cloth_keeps_its_text_on_one_line():
    # The cloth is a fixed 74 logical px and #204 proves its stagger occlusion-free at that width.
    # Without `nowrap` a line longer than the cloth BREAKS instead of clipping, and the second row
    # falls straight onto the landmark label painted below the leg — observed in the browser the
    # moment a phase word made the line longer than "BUILDING" (issue #444). Clipping keeps the
    # cloth's promise, and the flight number leads the line so clipping can never eat it.
    m = re.search(r"\.fld-banner\s*\{(.*?)\}", _SHELL_CSS, flags=re.S)
    assert m, "shell.css must style .fld-banner — the towed name cloth"
    assert "nowrap" in m.group(1), (
        "the cloth must never wrap: a broken second row spills past the 14px cloth onto the "
        "landmark label below the leg (issue #444)")
    assert "overflow: hidden" in m.group(1), (
        "the cloth must still clip what does not fit — nowrap without it would overflow sideways")
