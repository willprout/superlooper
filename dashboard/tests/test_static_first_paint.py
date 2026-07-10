"""Guard (issue #34): a FIRST-poll failure shows an honest "can't reach the tower" surface — before
any successful snapshot — that self-heals when the server comes back.

The reconnect surface (the ``conn-warn`` banner) is built only INSIDE the shell/boring skeleton, which
``render()`` paints only AFTER the first successful snapshot — and ``render()`` early-returns while
``state.snapshot`` is null. So before the first snapshot a failed poll had NOWHERE to show trouble:
the page sat on the seeded "connecting to the field…" text forever. The fix paints an honest error
straight into ``#root`` the moment the first poll fails, and leans on the existing self-heal — the
next successful poll's ``render()`` rebuilds ``#root`` from the fresh snapshot and wipes it.

Like the other ``test_static_*`` guards, these are STRING guards on the shipped bundle (the repo runs
no JS engine — Python stdlib only); the rendered proof that the surface looks right AND self-heals
lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SHELL_JS = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "shell.css").read_text(encoding="utf-8")


def _strip_js_comments(js):
    """Drop block + whole-line comments so a guard binds the CODE, not prose that mentions the same
    word (the pattern the other static guards use)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    return js


_SHELL_CODE = _strip_js_comments(_SHELL_JS)


def _fn_body(code, name):
    """The body of ``function <name>(...) { ... }`` by brace-matching from its opening brace. Returns
    "" when absent."""
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


def test_shell_defines_a_first_paint_disconnected_renderer():
    body = _fn_body(_SHELL_CODE, "renderDisconnected")
    assert body, "shell.js must define renderDisconnected() — the first-paint error surface (issue #34)"
    # It PAINTS the surface into the seeded root, so a failed first poll leaves the "connecting…" seed
    # behind instead of sitting on it forever.
    assert "root.innerHTML" in body, "renderDisconnected must paint into #root (issue #34)"


def test_disconnected_copy_is_honest_and_actionable():
    body = _fn_body(_SHELL_CODE, "renderDisconnected")
    low = body.lower()
    # Honest airport voice ("tower") + the literal it stands for (the command center), so the message
    # is legible whether or not you speak the metaphor — and it says it keeps trying (self-heals).
    assert "tower" in low, "the surface must speak the honest can't-reach-the-tower line (issue #34)"
    assert "command center" in low or "command-center" in low, (
        "the surface must name the literal command center so the message is actionable (issue #34)")
    assert "retry" in low or "retrying" in low, (
        "the surface must say it keeps trying — it self-heals on recovery (issue #34)")


def test_failed_first_poll_paints_the_surface_only_before_the_first_snapshot():
    # The catch path branches on !state.snapshot: NEVER-painted → the full first-paint surface;
    # already-painted → the existing conn-warn banner over the last-good snapshot (not a full-screen
    # error). Bind the guard to the call on one expression so a future edit can't call it
    # unconditionally or drop the gate (the pattern test_static_needs_collapse uses).
    assert re.search(r"!state\.snapshot\)\s*renderDisconnected\s*\(", _SHELL_CODE), (
        "the failed-poll path must call renderDisconnected() ONLY when there is no snapshot yet "
        "(issue #34)")


def test_render_still_rebuilds_root_from_the_snapshot_so_the_surface_self_heals():
    # The self-heal is the existing wholesale rebuild: a successful poll → render() → root.innerHTML =
    # the shell (or boring skeleton). Pin it so a future edit can't leave the disconnected surface
    # stuck on screen after the server returns (issue #34).
    render_body = _fn_body(_SHELL_CODE, "render")
    assert "root.innerHTML" in render_body, (
        "render() must rebuild #root from the fresh snapshot so the disconnected surface is wiped on "
        "recovery (issue #34)")


def test_disconnected_surface_is_styled_as_honest_trouble():
    # A styled surface, not a bare unstyled div — and it reads as trouble, reusing the connection
    # palette (design record §5: honest, never a frozen stale surface).
    assert re.search(r"\.cc-disconnected\b", _CSS), (
        "shell.css must style the .cc-disconnected first-paint surface (issue #34)")
