"""Issue #475 — the operator's typed answer must survive the 2s poll redraw.

The bug the owner hit live: the Answer textarea (#163) is rendered inside a container the poll
rebuilds with an ``innerHTML`` assignment — ``#root`` for the Needs You card, the drawer node for
the flight card — so a rebuilt textarea came back empty about a second after he started typing.
Nothing could ever be submitted, which made the dashboard's own Answer verb unusable and left a
worker parked on ``awaiting-answer`` with no way through.

Two layers of guard, on purpose:

* the BEHAVIOUR is driven for real in ``tests/js/answer_field_survives.test.js`` (run from
  ``test_static_answer_field_dom.py``), which types into the real rendered field and drives real
  redraw cycles;
* these are STRING guards on the bundle — the discipline the rest of the ``test_static_*`` suite
  uses — so that the WIRING cannot be quietly removed. ``shell.js`` is too big to run headless, so
  its rebuild is guarded here and proven in the PR's real-browser screenshot evidence.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _ROOT / "static"
_KEEP_JS = (_STATIC / "keepinput.js").read_text(encoding="utf-8")
_DRAWER_JS = (_STATIC / "drawer.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")


def _strip_js_comments(js):
    """Drop block comments and whole-line ``//`` comments so a guard binds the CODE, not a comment
    that happens to mention the same word (the convention from issue #28's guards)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


def _handler_body(js, name):
    """The source of ``function <name>(...) { ... }``, brace-matched — a guard anchored on the
    function's real extent rather than a character window after a keyword."""
    i = js.index("function %s(" % name)
    depth, start = 0, js.index("{", i)
    for j in range(start, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError("unbalanced braces in %s" % name)


_KEEP_CODE = _strip_js_comments(_KEEP_JS)
_DRAWER_CODE = _strip_js_comments(_DRAWER_JS)
_SHELL_CODE = _strip_js_comments(_SHELL_JS)


def test_the_drawer_rebuild_carries_typed_text_across_it():
    # Every drawer rebuild goes through render() — the poll's update() and the journal caret's
    # in-place re-render — so the preservation belongs there, not at one call site.
    assert "KeepInput.preserve" in _DRAWER_CODE, (
        "the drawer's innerHTML rebuild must carry the operator's typed answer across it")
    assert "node.innerHTML" not in _DRAWER_CODE.split("function paint")[0], (
        "no drawer rebuild may bypass the preservation")


def test_the_shell_rebuild_carries_typed_text_across_it():
    # The Needs You card renders its own Answer field inside the #root rebuild.
    assert "keepTypedText(root, function () { root.innerHTML = shellHTML(); });" in _SHELL_CODE, (
        "the 2s #root rebuild must carry the operator's typed answer across it")
    assert "KeepInput.preserve" in _SHELL_CODE


def test_both_rebuilds_fail_soft_when_the_module_is_absent():
    # A missing bundle must cost a lost draft, never a blank dashboard.
    assert "window.KeepInput" in _DRAWER_CODE and "window.KeepInput" in _SHELL_CODE


def test_the_module_is_loaded_before_the_renderers_that_need_it():
    order = [_INDEX.index("/%s.js" % name) for name in ("keepinput", "needsyou", "drawer", "shell")]
    assert order == sorted(order), (
        "keepinput.js must load before every renderer whose rebuild it wraps")


def test_preservation_is_keyed_to_the_flight_not_to_dom_position():
    # A card can appear or leave between two polls. Matching restored text by POSITION would pour
    # one flight's half-written answer into another flight's field — and that field POSTs to a
    # different issue. The key is the identifying attributes the renderers already emit.
    assert 'getAttribute("data-repo")' in _KEEP_CODE and 'getAttribute("data-num")' in _KEEP_CODE
    assert 'getAttribute("data-input")' in _KEEP_CODE


def test_only_what_the_operator_typed_survives_so_live_data_still_refreshes():
    # The browser's own dirty model: value !== defaultValue. An untouched field keeps refreshing
    # from the server like every other pixel — the fix must not freeze the drawer's live data.
    assert "el.value !== el.defaultValue" in _KEEP_CODE


def test_the_preserved_set_is_an_exclude_list_so_future_fields_inherit_the_survival():
    # An include-list would silently drop a text-ish input type added later — exactly the shape of
    # bug this issue is fixing. Non-text controls are the ones that have to be named.
    assert "NOT_TEXT" in _KEEP_CODE
    for non_text in ("checkbox", "radio", "range", "file", "submit"):
        assert '%s: 1' % non_text in _KEEP_CODE or '"%s": 1' % non_text in _KEEP_CODE


def test_every_field_the_operator_types_or_picks_into_is_classified():
    """The sweep (issue #475 DoD item 4). Every control in the bundle that holds operator state —
    markup ``<textarea>``/``<input>``/``<select>`` AND one built imperatively with
    ``createElement`` — is either on a poll rebuild path and covered, or off it. This test names
    which, so a new field has to be classified rather than silently inheriting the bug.

    ON the poll rebuild path, covered by KeepInput:

    * ``needsyou.js`` — the Needs You card's Answer field, inside the ``#root`` rebuild.
    * ``drawer.js`` — the drawer's Answer field, inside the drawer node's rebuild.

    OFF it (each verified by reading the file, not by assumption):

    * ``shell.js`` ``#cc-flag-text`` — the flag composer, an overlay appended to ``<body>`` once.
    * ``shell.js`` ``#fire-filter`` + ``#fire-range`` — inside the boring-mode skeleton, built once
      per view switch (``state.builtView``) and re-seeded from ``state``; the poll only rewrites
      the truth line, the table bodies and the firehose lines.
    * ``shell.js`` ``fallbackCopy``'s textarea — created, used and removed synchronously inside one
      clipboard call; it never survives to see a poll.
    * ``fixer.js`` ``#cc-fixer-note`` — an overlay outside ``#root`` whose body is rewritten only by
      an operator action (open / retry / deploy); the file starts no timer.
    * ``replay.js`` scrub + ``#cc-replay-range`` — a once-built body overlay; the scrub is a
      ``range``, not free text.
    * ``digest.js`` ``#cc-digest-range`` — the same shape: a once-built body overlay, opened by a
      button, its body rewritten only on open.
    """
    known = {
        "needsyou.js": 1,   # the Answer field on the card            — ON the poll path, covered
        "drawer.js": 1,     # the Answer field in the drawer          — ON the poll path, covered
        "shell.js": 4,      # flag composer, firehose filter + range, fallbackCopy's textarea
        "fixer.js": 1,      # the fixer note
        "replay.js": 2,     # the replay scrub + its range select
        "digest.js": 1,     # the digest window select
    }
    field = re.compile(r"""<(?:textarea|input|select)\b"""
                       r"""|createElement\(\s*["'](?:textarea|input|select)["']""")
    found = {}
    for js in sorted(_STATIC.glob("*.js")):
        src = _strip_js_comments(js.read_text(encoding="utf-8"))
        n = len(field.findall(src))
        if n:
            found[js.name] = n
    assert found == known, (
        "a control holding operator state was added or moved: classify it — on the poll rebuild "
        "path it must be inside a KeepInput.preserve rebuild; off it, record why in this "
        "docstring.\nfound: %r" % (found,))


def test_a_posted_answer_is_cleared_so_the_preservation_cannot_re_offer_it():
    """The regression the preservation itself creates, if nothing else changes.

    Before #475 a submitted answer was wiped by the very next poll, which read — accidentally — as
    "sent". Now the field is faithfully preserved, so the operator's already-posted words would sit
    in the box beside a live Answer button until the card leaves, for as long as the GitHub read
    lags (the label ride is on the slow ``gh`` clock). A second tap posts a SECOND answer comment
    and re-applies the label. So a successful post must clear the field — which also makes it
    non-dirty again, so the preservation correctly stops carrying it. This is the flag composer's
    own discipline (``openFlagBox`` clears, ``submitFlag`` closes on success).
    """
    body = _handler_body(_SHELL_CODE, "doAnswer")
    ok_branch = body.split("res.body.ok")[1].split("} else {")[0]
    assert "clearAnswerFields(repo, num)" in ok_branch, (
        "a successful answer must clear the field, or the preservation re-offers posted words")
    # …and it must clear BEFORE the re-poll, or that poll's capture carries the posted text over.
    assert ok_branch.index("clearAnswerFields") < ok_branch.index("refresh()")
    # It must NOT clear through the reference read before the POST. A GitHub write is a comment
    # plus a label and routinely outlasts a 2s poll, so by the time it returns the surface has been
    # rebuilt and that reference is a DETACHED node — clearing it clears nothing the owner can see
    # (measured in Chrome with a 5s write: the posted words sat in the live field indefinitely).
    assert 'field.value = ""' not in ok_branch, (
        "clear the LIVE field by (repo, num); the captured reference may be detached by then")
    clear = _handler_body(_SHELL_CODE, "clearAnswerFields")
    assert 'querySelectorAll("textarea.answer-field")' in clear
    assert 'getAttribute("data-repo")' in clear and 'getAttribute("data-num")' in clear, (
        "clearing must be scoped to the flight that was answered, never every field on the surface")


def test_the_preservation_cannot_take_the_joy_surfaces_down_with_it():
    """§0.1: the airfield canvas and the Solari board are re-parented into the fresh mount AFTER
    the rebuild returns. A throw out of capture/restore would skip that re-parenting on every poll
    and leave both surfaces blank — a lost draft may never cost the dashboard its joy."""
    keep = _handler_body(_KEEP_CODE, "preserve")
    assert keep.count("try {") >= 2, (
        "capture and restore must each fail soft; only the caller's own rebuild may throw")


def test_the_drawer_has_no_rebuild_that_bypasses_the_preservation():
    """Position-independent: whatever order the file is in, ``render`` is the single funnel and the
    only raw rebuilds are ``paint``'s and ``close``'s wipe."""
    assert "KeepInput.preserve" in _handler_body(_DRAWER_CODE, "render")
    assert "node.innerHTML" not in _handler_body(_DRAWER_CODE, "render")
    assert _DRAWER_CODE.count("node.innerHTML =") == 2, (
        "exactly two: paint's wrapped rebuild and close's wipe — a third is an unguarded rebuild")
    assert 'node.innerHTML = ""' in _handler_body(_DRAWER_CODE, "close")
