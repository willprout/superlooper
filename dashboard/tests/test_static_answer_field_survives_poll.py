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


def test_every_free_text_field_on_a_poll_rebuild_path_is_covered():
    """The sweep (issue #475 DoD). Every ``<textarea>``/``<input>`` in the bundle is either on a
    rebuild path and covered, or off it — and this test says which, so a new field on the poll path
    has to be classified rather than silently inheriting the bug.

    * ``needsyou.js`` / ``drawer.js`` answer fields — ON the poll path (``#root`` and the drawer
      node are rebuilt every ~2s); covered by KeepInput.
    * ``shell.js`` ``#cc-flag-text`` — the flag composer, an overlay built ONCE outside ``#root``.
    * ``shell.js`` ``#fire-filter`` — inside the boring-mode skeleton, built once per view switch
      (``state.builtView``) and re-seeded from ``state.fireFilter``; the poll only rewrites the
      table and firehose bodies.
    * ``fixer.js`` ``#cc-fixer-note`` — an overlay outside ``#root``; its body is rewritten only by
      an operator action (open / retry / deploy), never by the poll.
    * ``replay.js`` scrub — a ``range`` in a once-built overlay, and not free text.
    """
    known = {
        "needsyou.js": 1,   # the Answer field on the card
        "drawer.js": 1,     # the Answer field in the drawer
        "shell.js": 2,      # flag composer + firehose filter (both off the poll rebuild path)
        "fixer.js": 1,      # the fixer note (off the poll rebuild path)
        "replay.js": 1,     # the replay scrub (a range, not free text)
    }
    found = {}
    for js in sorted(_STATIC.glob("*.js")):
        src = _strip_js_comments(js.read_text(encoding="utf-8"))
        n = len(re.findall(r"<(?:textarea|input)\b", src))
        if n:
            found[js.name] = n
    assert found == known, (
        "a free-text field was added or moved: classify it — on the poll rebuild path it must be "
        "inside a KeepInput.preserve rebuild; off it, record why here.\nfound: %r" % (found,))
