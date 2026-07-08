"""Guard (issue #30): both split-flap boards must PAGE through their backlog.

The Solari arrivals board shows a fixed window of ``MAX_ROWS`` rows; with 12+ landings the older
history was unreachable. Departures likewise rendered its whole queue in one ever-growing column.
Issue #30 adds pagination to both, in keeping with their split-flap character:

  * arrivals: the server hands the board a backlog capped to the smaller of 5 pages or 3 days
    (``flights.cap_arrivals`` — unit-tested in ``test_flights.py``); the Solari controller paginates
    that list ``MAX_ROWS`` at a time, page transitions use the existing flap flutter (settle < 1s,
    reduced-motion honored), a split-flap page indicator sits in the corner, and after 5 minutes of
    inactivity the board flaps back to page 1 (owner amendments 2026-07-07);
  * departures: the full queue paginates ``DEP_PAGE_SIZE`` at a time with a visible page control.

Like the tower-scroll guards (issue #27), these are STRING guards on the shipped static bundle, not
behavioural tests — the repo runs no JS engine (Python stdlib only). They fail CI if a future edit
drops the paging seam or lets the two boards' page sizes drift out of the server's cap. The rendered
proof that paging LOOKS and FEELS right (joy included) lives in the PR's screenshot evidence.
"""
import inspect
import re
from pathlib import Path

import flights

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SOLARI = (_STATIC / "solari.js").read_text(encoding="utf-8")
_BOARDS = (_STATIC / "boards.js").read_text(encoding="utf-8")
_SHELL = (_STATIC / "shell.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "boards.css").read_text(encoding="utf-8")


# =============================== the server↔client page-size contract ===============================

def test_solari_max_rows_matches_the_server_arrivals_page_size():
    # "5 pages" server-side (flights.cap_arrivals page_size) and the client's page count only agree if
    # the Solari's rows-per-page == the server's page_size. Pin them together so neither drifts silently.
    m = re.search(r"MAX_ROWS\s*=\s*(\d+)", _SOLARI)
    assert m, "solari.js must define MAX_ROWS (the rows-per-page)"
    page_size = inspect.signature(flights.cap_arrivals).parameters["page_size"].default
    assert int(m.group(1)) == page_size, (
        "solari.js MAX_ROWS (%s) must equal the server's cap_arrivals page_size (%s), or the boards "
        "and the backlog cap disagree on a page" % (m.group(1), page_size))


# =============================== arrivals (Solari) paging ===============================

def test_solari_controller_tracks_a_page():
    # The persistent controller owns the current page across the 2s poll (it must not reset to page 1
    # every poll, or a reader browsing history is yanked back constantly).
    assert "_page" in _SOLARI, "the Solari controller must track a current _page across polls"


def test_solari_paginates_the_full_backlog_by_page():
    # update() must render a PAGE-sized slice keyed off _page, not just the first MAX_ROWS. The full
    # list arrives from the server; the board shows one page of it.
    assert re.search(r"\.slice\(\s*[^,]*_page[^,]*,", _SOLARI) or re.search(r"_page\s*\*\s*MAX_ROWS", _SOLARI), (
        "solari.js must slice the arrivals by _page * MAX_ROWS to show one page at a time")


def test_solari_has_a_page_control_and_a_split_flap_page_indicator():
    for cls in ("solari-pager", "solari-page-num"):
        assert cls in _SOLARI, "solari.js must build the '%s' element (page control + corner indicator)" % cls
    assert "solari-page-prev" in _SOLARI and "solari-page-next" in _SOLARI, (
        "solari.js must offer prev/next page buttons")


def test_solari_page_turn_uses_the_flap_and_honors_reduced_motion():
    # A page turn is a real board change → it must flutter (the DoD: page transitions use the existing
    # flap animation) and, under prefers-reduced-motion, land instantly (the same reduced path).
    # Tightened (Codex review): pin the concrete behavior, not merely that the word "reduced" appears.
    assert "_paint(true)" in _SOLARI, (
        "goToPage must flutter the page turn via _paint(true) — the same flap machinery an arrival uses")
    assert re.search(r"var\s+reduced\s*=\s*reducedMotion\(\)", _SOLARI), (
        "_paint must read prefers-reduced-motion (reducedMotion()) before deciding to flutter")
    assert re.search(r"doFlutter\s*=\s*flutter\s*&&\s*!reduced", _SOLARI), (
        "reduced motion must gate OUT the flutter (doFlutter = flutter && !reduced) — land instantly")


def test_solari_flaps_back_to_page_one_after_five_minutes_idle():
    # Owner amendment 2026-07-07: after 5 minutes of inactivity the board returns to page 1 (newest).
    # Tightened (Codex review): the 300000ms delay and the goToPage(0) target must be the SAME timer.
    assert re.search(r"IDLE_RESET_MS\s*=\s*300000", _SOLARI), (
        "solari.js must define IDLE_RESET_MS = 300000 (the 5-minute inactivity window)")
    assert re.search(r"setTimeout\(\s*function\s*\(\)\s*\{\s*self\.goToPage\(0\);\s*\}\s*,\s*IDLE_RESET_MS\)",
                     _SOLARI), (
        "the inactivity setTimeout must flap the board back to page 1 (goToPage(0)) after IDLE_RESET_MS")


def test_solari_resets_page_when_the_camera_switches_repos():
    # The Solari controller is a persistent singleton; the boards follow the camera (§4). Switching
    # repos must reset it to page 1 (its newest) — else repo B shows on repo A's page (Codex review).
    attach_params = re.search(r"function attach\(([^)]*)\)", _BOARDS).group(1)
    assert "slug" in attach_params, (
        "Boards.attach must take the camera repo's slug so the Solari can detect a repo change")
    assert re.search(r"repo:\s*slug", _BOARDS), "attach must forward the repo identity to Solari.update"
    assert "_repo" in _SOLARI and re.search(r"repo\s*!==\s*this\._repo", _SOLARI), (
        "solari.js update must compare the incoming repo to the one it is showing")
    seg = _SOLARI[_SOLARI.index("this._repo = repo"):]
    assert re.search(r"this\._page\s*=\s*0", seg), (
        "on a repo change the controller must reset this._page = 0 (show the new repo's newest page)")
    assert "Boards.attach(" in _SHELL and re.search(r"rb\s*\?\s*rb\.slug", _SHELL), (
        "shell.js must pass the camera repo's slug (rb ? rb.slug : \"\") into Boards.attach")


def test_solari_page_size_slice_is_not_hardcoded_to_first_rows_only():
    # The old code sliced (0, MAX_ROWS) unconditionally — the exact bug issue #30 fixes. That literal
    # "always the first page" slice must be gone so history is reachable.
    assert not re.search(r"slice\(\s*0\s*,\s*MAX_ROWS\s*\)", _SOLARI), (
        "solari.js must no longer clip arrivals to the FIRST MAX_ROWS only (issue #30) — paginate instead")


# =============================== departures paging ===============================

def test_departures_paginates_the_queue():
    assert re.search(r"DEP_PAGE_SIZE\s*=\s*\d+", _BOARDS), (
        "boards.js must define DEP_PAGE_SIZE (departures rows per page)")
    # Tightened (Codex review): the exact page slice, not just "a .slice() exists somewhere".
    assert re.search(r"deps\.slice\(\s*start\s*,\s*start\s*\+\s*DEP_PAGE_SIZE\s*\)", _BOARDS), (
        "departuresInner must slice deps to the current page: deps.slice(start, start + DEP_PAGE_SIZE)")


def test_departures_has_a_visible_page_control():
    assert "dep-pager" in _BOARDS, "boards.js must render a departures page control (dep-pager)"
    assert "data-deppage" in _BOARDS, (
        "the departures page buttons must carry data-deppage so the shell handles the page turn")


def test_departures_inner_takes_a_page_argument():
    # departuresInner must accept the current page so the shell can re-render the right page each poll.
    m = re.search(r"function departuresInner\(([^)]*)\)", _BOARDS)
    assert m and "page" in m.group(1), "departuresInner(deps, slug, page) must take the current page"


# =============================== shell wiring (page state survives the poll) ===============================

def test_shell_holds_departures_page_state_across_polls():
    assert "depPage" in _SHELL, (
        "shell.js must keep departures page state in `state` so the 2s poll re-render preserves it")


def test_shell_resets_departures_page_on_repo_switch():
    # A repo switch swaps in a different queue; keeping the old page could land on an out-of-range page.
    seg = _SHELL[_SHELL.index("data-repoidx"):]
    assert re.search(r"depPage\s*=\s*0", seg), (
        "the repo selector must reset state.depPage = 0 so the new repo's queue starts on page 1")


def test_shell_passes_the_page_to_departures_and_handles_the_page_turn():
    assert re.search(r"departuresInner\([^)]*depPage", _SHELL), (
        "boardsHTML must pass state.depPage into departuresInner")
    assert "data-deppage" in _SHELL, "shell.js must handle the departures page-turn click (data-deppage)"


# =============================== styling ===============================

def test_pager_styling_exists_and_reduced_motion_is_covered():
    assert ".solari-pager" in _CSS, "boards.css must style the Solari pager"
    assert ".dep-pager" in _CSS, "boards.css must style the departures pager"
    # The split-flap page indicator reads like a flap tile (dark tile, not a plain number).
    assert ".solari-page-num" in _CSS, "boards.css must style the split-flap page indicator"
    # The existing reduced-motion block kills animation on .solari and its descendants; the pager lives
    # inside .solari, so it is already covered — assert the block is still present (belt-and-braces).
    assert "prefers-reduced-motion" in _CSS, "boards.css must keep the reduced-motion guard"
