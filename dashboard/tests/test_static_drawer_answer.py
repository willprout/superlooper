"""Issue #471 — the drawer's go-ahead verb, on the shipped static bundle.

The crash this issue fixes was a server-side one (``lib/cards`` indexed a verb list that a #163
QUESTION flight never fills). But the fix hands the client something new: a go-ahead verb that
takes the operator's typed words. Answer is refused empty by ``lib/actions``, so a bare button
would be a dead end — the field has to be beside it, in the container ``shell.js``'s ``doAnswer``
reads from.

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the bundle, the same
discipline as ``test_static_session_window`` / ``test_static_decision_card``. They exist so a future
edit that drops the field, drops the fail-soft, or re-derives the verb in the client fails CI. The
rendered proof lives in the PR's screenshot evidence, driven in a real browser.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_DRAWER_JS = (_STATIC / "drawer.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


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


_ACTIONS = _handler_body(_strip_js_comments(_DRAWER_JS), "actionsHTML")


def test_the_drawer_renders_the_typed_field_for_an_input_taking_verb():
    # The server says whether the verb takes input (`approve_input`); the client renders the field.
    assert "approve_input" in _ACTIONS
    assert "answer-field" in _ACTIONS, (
        "a go-ahead verb that is refused empty must render its field, not a dead-end button")


def test_the_field_sits_in_the_same_container_as_its_button():
    # shell.js's doAnswer reads `btn.parentNode.querySelector("textarea.answer-field")`. If the
    # field and the button are not siblings, the button silently posts nothing.
    assert 'class="act"' in _ACTIONS, "field and button share one `.act` container, as on the card"
    assert 'querySelector("textarea.answer-field")' in _SHELL_JS


def test_the_drawer_derives_no_verb_of_its_own():
    # Design record B.1: the act and its label are the SERVER's. The client may not hard-code
    # "answer" (or any other verb) as the drawer's go-ahead — that is the drift this issue punished.
    assert "dec.approve_act" in _ACTIONS and "dec.approve_label" in _ACTIONS
    assert '"answer"' not in _ACTIONS and "'answer'" not in _ACTIONS


def test_a_decision_with_no_go_ahead_verb_degrades_to_discuss_not_to_undefined():
    # The fail-soft half of #471: lib/cards pins one go-ahead verb per kind, so this branch should
    # never be reached — but if it ever is, the cost must be one missing button, never an
    # "undefined" on a button the owner would tap.
    assert "!dec.approve_act" in _ACTIONS


def test_the_drawers_answer_field_is_styled_where_it_now_lives():
    # `.card .actions .answer-field` does not reach the drawer's row — an unstyled textarea in a
    # flex row renders as a squeezed sliver.
    assert ".drawer-actions .answer-field" in _CSS
    assert ".drawer-actions .act" in _CSS
