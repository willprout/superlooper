"""Issue #136 — the stale-tower NOTAM and the Retry that must not be offered (the shipped bundle).

The repo runs no JS engine (Python stdlib only), so these are STRING guards on the shipped static
bundle — the same discipline as ``test_static_tidy.py`` / ``test_static_restart.py``. They exist so a
future edit that turns the notice into a nag, computes the skew decision in the pixels, or restores a
Retry that cannot succeed fails CI instead of shipping quietly. The rendered proof that it LOOKS
right — and is still delightful (§0.1) — lives in the PR's screenshot evidence.

What is being defended:

  * **One calm notice, then out of the way** (§0.2, no nagging). A NOTAM is the airport's own word
    for a posted advisory — it is the right register precisely because it is not an alarm: the field
    is fine, the tower is just behind. It is dismissible, and it is NOT a modal.
  * **The decision is the server's** (design B.1). The JS binds ``version.skew`` / ``.message`` /
    ``.remedy`` to pixels and computes nothing — the squint test: delete the art and the JSON still
    states the whole situation.
  * **No Retry that cannot succeed.** The live 2026-07-14 failure put a Retry beside ``no such
    action``; every tap of it re-asked the same old server the same question it had no route for.
"""
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_NOTAM_JS = (_STATIC / "notam.js").read_text(encoding="utf-8")
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_JANITOR_JS = (_STATIC / "janitor.js").read_text(encoding="utf-8")
_TIDY_JS = (_STATIC / "tidy.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


# =============================== the bundle ===============================

def test_index_loads_the_notam_bundle_before_shell():
    assert "/notam.js" in _INDEX, "index.html must load the NOTAM surface"
    assert _INDEX.index("/notam.js") < _INDEX.index("/shell.js"), (
        "notam.js must load before shell.js so window.CCNotam exists when the chrome binds it")


def test_the_notam_has_a_stylesheet_rule():
    assert ".cc-notam" in _CSS, "the notice must be styled, not raw text on the field"


def test_the_stylesheet_has_no_dangling_comment_terminator():
    """A stray ``*/`` makes the browser discard the rule that follows it, silently.

    Not hypothetical — it happened while building this notice: an edit left a second ``*/`` after an
    already-closed comment, the ``.cc-notam`` rule became inert, and the NOTAM rendered as unstyled
    text nobody would read as a notice. The string guard above passed the whole time, because
    ``.cc-notam`` was still *present* in the file. Only the browser caught it.

    A string guard can prove a rule was written; only this can prove it survives to the cascade. It
    is a whole-stylesheet check because one stray terminator harms every rule after it, not just the
    one this issue added.
    """
    i, line, in_comment, opened_at = 0, 1, False, None
    while i < len(_CSS):
        if _CSS[i] == "\n":
            line += 1
        elif not in_comment and _CSS.startswith("/*", i):
            in_comment, opened_at = True, line
            i += 2
            continue
        elif in_comment and _CSS.startswith("*/", i):
            in_comment = False
            i += 2
            continue
        elif not in_comment and _CSS.startswith("*/", i):
            raise AssertionError(
                "shell.css line %d: a `*/` that closes nothing — everything after it is discarded "
                "by the browser. Balance the comment markers." % line)
        i += 1
    assert not in_comment, ("shell.css line %d: a `/*` that is never closed — the rest of the "
                           "stylesheet is swallowed." % opened_at)


# =============================== the decision stays server-side (B.1) ===============================

def test_the_notam_binds_the_servers_decision_and_computes_none_of_it():
    for key in ("skew", "message", "remedy"):
        assert key in _NOTAM_JS, "notam.js must bind version.%s from the snapshot" % key


def test_the_notam_never_compares_stamps_itself():
    """The comparison is pure Python in lib/version.py, unit-tested there. A second implementation in
    the pixels is a second thing to get wrong — and the one in the pixels is the one nobody tests."""
    for stamp in ("server_on_disk", "assets_at_boot"):
        assert stamp not in _NOTAM_JS, (
            "notam.js must not re-derive the decision from raw stamps (%s) — bind version.skew" % stamp)


def test_the_remedy_shown_is_the_one_the_server_names():
    """A remedy hardcoded in the pixels drifts the day the command is renamed, and drifts silently."""
    assert ".remedy" in _NOTAM_JS, "the remedy must be read from the snapshot, not hardcoded"


# =============================== a notice, not a nag (§0.2) ===============================

def test_the_notice_is_dismissible():
    assert "data-notam-dismiss" in _NOTAM_JS, (
        "the notice must be dismissible — §0.2 is no nagging, and an undismissable strip nags")


def test_the_notice_is_not_a_modal():
    """§0.2 — a notice, not a modal storm. A stale tower still flies the field fine; blocking the
    surface over it would be the nag the ruling forbids."""
    for modal in ("cc-jan-card", "cc-tidy-card", "showModal", "confirm("):
        assert modal not in _NOTAM_JS, "the NOTAM must not borrow the dialog/modal machinery"


def test_the_notice_stays_dismissed_across_the_two_second_poll():
    """#root is rebuilt every ~2s. A dismissal that lived in #root would come back every poll — which
    is precisely the nag §0.2 forbids, delivered 30 times a minute."""
    assert "dismissed" in _NOTAM_JS
    assert "document.body" in _NOTAM_JS, (
        "the NOTAM must live OUTSIDE #root (appended to body, like the toast/dialogs) so the poll "
        "re-render never clobbers it or its dismissal")


# =============================== the chrome binds it, in BOTH views ===============================

def test_the_shell_updates_the_notam_from_the_snapshot():
    assert "CCNotam" in _SHELL_JS, "shell.js must feed the NOTAM the fresh snapshot"
    assert "updateChrome" in _SHELL_JS
    chrome = _SHELL_JS[_SHELL_JS.index("function updateChrome"):]
    chrome = chrome[:chrome.index("\n  }")]
    assert "CCNotam" in chrome, (
        "the NOTAM must be updated from updateChrome — the one path BOTH the shell and boring mode "
        "run, so a boring-mode reader is told the truth too")


# =============================== no Retry that cannot succeed ===============================

def test_janitor_drops_the_retry_when_the_server_is_stale():
    """The live failure: RAMP SWEEP → `no such action` → a Retry that re-asked the same old server."""
    assert "skew" in _JANITOR_JS, "janitor.js must read the server's skew flag off the error body"
    err = _JANITOR_JS[_JANITOR_JS.index("function renderError"):]
    err = err[:err.index("\n  }")]
    assert "skew" in err, "renderError must suppress the Retry when the failure was skew"


def test_tidy_drops_the_retry_when_the_server_is_stale():
    """Tidy carries the identical Retry shape, so it inherits the identical trap."""
    err = _TIDY_JS[_TIDY_JS.index("function renderError"):]
    err = err[:err.index("\n  }")]
    assert "skew" in err, "renderError must suppress the Retry when the failure was skew"


def test_the_dialogs_pass_the_skew_flag_through_from_the_body():
    """The flag has to survive the trip from the 409 body to the render, or the guard above is inert."""
    for name, js in (("janitor.js", _JANITOR_JS), ("tidy.js", _TIDY_JS)):
        assert "b.skew" in js, "%s must pass the response body's skew flag into renderError" % name
