"""Guard (issue #31): the two split-flap boards must read as SIBLINGS, one airport's signage.

The boards visual pass (owner joy pass, 2026-07-07) settles a shared split-flap language across the
departures queue and the Solari arrivals board. Before this pass the two read as strangers:

  * departures wasted width on a 100px FLIGHT cell (for a 6-char "SL-441"), carried a redundant Nº
    queue-position column (row order ALREADY is the launch order), clipped its STATUS phrase, and set
    its ``.flap`` type so small the flaps read as faint lines, not flaps;
  * arrivals nailed the flap look but set its glyph so large that titles clipped to ~13 characters.

This pass brings them to ONE scale and ONE tile face: the arrivals tiles shrink (type reads right,
titles fit), the departures ``.flap`` grows and adopts the arrivals tiles' face colours + mid-seam,
the STATUS becomes a short colour-coded label mirroring the arrivals remark chip, and the ⚡ expedite
marker is explained in place by a legend. Row order carries the launch order (Nº column removed).

Like the tower-scroll (issue #27) and paging (issue #30) guards, these are STRING guards on the
shipped static bundle, not behavioural tests — the repo runs no JS engine (Python stdlib only). They
fail CI if a future edit drops a sibling-making seam or lets the two boards' scales drift apart. The
rendered proof that the boards LOOK and FEEL like one signage system (joy included, nothing clipped)
lives in the PR's screenshot evidence.
"""
import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"
_SOLARI = (_STATIC / "solari.js").read_text(encoding="utf-8")
_BOARDS = (_STATIC / "boards.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "boards.css").read_text(encoding="utf-8")


def _num(pattern, text, what):
    m = re.search(pattern, text)
    assert m, "could not find %s" % what
    return float(m.group(1))


# =============================== the shared split-flap SCALE ===============================

def test_arrivals_tiles_shrunk_from_the_oversized_original():
    # The flagship's type was "too big and clips text" (#31). The tiles must come DOWN from the original
    # 21x32 / 17px so a full row of glyphs reads as flaps AND fits without clipping short titles.
    tile_w = _num(r"TILE_W\s*=\s*(\d+)", _SOLARI, "TILE_W")
    tile_h = _num(r"TILE_H\s*=\s*(\d+)", _SOLARI, "TILE_H")
    glyph = _num(r"GLYPH_FONT\s*=\s*\"\d+\s+(\d+)px", _SOLARI, "GLYPH_FONT size")
    assert tile_w < 21, "TILE_W (%s) must shrink below the oversized original 21px" % tile_w
    assert tile_h < 32, "TILE_H (%s) must shrink below the oversized original 32px" % tile_h
    assert glyph < 17, "the Solari glyph (%spx) must shrink below the oversized original 17px" % glyph
    # ...but not so small the flaps stop reading as flaps — keep them within a sane split-flap range.
    assert tile_w >= 14 and glyph >= 12, "the tiles must stay large enough to still READ as flaps"


def test_departures_flap_type_reads_as_flaps():
    # The departures ``.flap`` type was 13px — too small for the flaps to read as flaps (#31). boards.css
    # must override it UP (>= 14px) so the departures flaps carry the same visual weight as the arrivals.
    # Find the boards.css .flap override (base lives in shell.css; boards.css loads after and wins).
    m = re.search(r"\.flap\s*\{[^}]*?font:\s*\d+\s+(\d+)px", _CSS, re.S)
    assert m, "boards.css must override .flap with a larger font so the flaps read as flaps"
    assert int(m.group(1)) >= 14, "the departures .flap type (%spx) must be >= 14px to read as flaps" % m.group(1)


def test_both_boards_share_one_tile_face():
    # Sibling glue: the departures .flap face must use the SAME split-flap tile colours as the Solari
    # arrivals tiles (#2A313B light half / #1B2129 dark half). If they drift, the boards look like two
    # different signs again. Scoped to the actual `.flap { ... }` block so a stray comment can't satisfy
    # it (Codex review): the bare `.flap {` rule, not `.flap.exped {` etc.
    flap_block = re.search(r"\.flap\s*\{.*?\}", _CSS, re.S)
    assert flap_block, "boards.css must override the base .flap with a tile face"
    for hexc in ("#2A313B", "#1B2129"):
        assert hexc in _SOLARI, "the Solari arrivals tiles must keep the shared face colour %s" % hexc
        assert hexc in flap_block.group(0), (
            "the departures .flap face must adopt the arrivals tile colour %s (one signage)" % hexc)


# =============================== the flutter budget stays under a second ===============================

def test_arrivals_flutter_still_settles_under_one_second():
    # Rescaling the tiles must NOT change the motion (DoD: flutter behaviour unchanged, settle < 1s).
    # Compute the worst-case whole-board settle from the timing constants + the column cap and assert it
    # stays under 1000ms — a mechanical guard on the DoD, independent of the tile pixel size.
    stagger = _num(r"STAGGER\s*=\s*(\d+)", _SOLARI, "STAGGER")
    step = _num(r"STEP\s*=\s*(\d+)", _SOLARI, "STEP")
    max_flips = _num(r"MAX_FLIPS\s*=\s*(\d+)", _SOLARI, "MAX_FLIPS")
    row_lead = _num(r"ROW_LEAD\s*=\s*(\d+)", _SOLARI, "ROW_LEAD")
    max_rows = _num(r"MAX_ROWS\s*=\s*(\d+)", _SOLARI, "MAX_ROWS")
    cap = _num(r"cols\s*>\s*(\d+)\s*\)\s*cols\s*=\s*\1", _SOLARI, "the cols cap (if cols > N) cols = N")
    # Worst tile: last column, MAX_FLIPS flaps, bottom row. Each flip step lasts STEP+8; the runway has
    # MAX_FLIPS+1 steps, the last one STEP+8 long (matches land()/flipStep). Plus the row-shuffle lead.
    worst = (max_rows - 1) * row_lead + (cap - 1) * stagger + (max_flips + 1) * (step + 8)
    assert worst < 1000, "worst-case flutter settle is %sms — must stay under 1000ms (DoD)" % worst


# =============================== departures: the audited columns ===============================

def test_departures_drops_the_redundant_queue_position_column():
    # Row order IS the launch order (#31): the standalone Nº position column is removed. The Nº glyph
    # must be gone from the departures markup (header label AND the per-row position flap).
    assert "Nº" not in _BOARDS, (
        "boards.js must no longer render the redundant Nº queue-position column (row order is the order)")


def test_departures_flight_column_is_tightened():
    # The FLIGHT cell was 100px for a ~6-char "SL-441" — far too wide (#31). boards.css must override the
    # departures grid so the first (FLIGHT) track is tighter than the original 100px.
    m = re.search(r"\.dep-cols\s*,\s*\.dep-row\s*\{\s*grid-template-columns:\s*(\d+)px", _CSS)
    assert m, "boards.css must override .dep-cols/.dep-row grid-template-columns (tighten the FLIGHT cell)"
    assert int(m.group(1)) < 100, "the FLIGHT column (%spx) must be tighter than the wasteful 100px" % m.group(1)


def test_departures_status_is_a_short_colour_coded_label():
    # The STATUS column read unclear (a clipped phrase). It becomes a short, colour-coded label that
    # mirrors the arrivals remark chip — one signage system. Assert the dep-status element + its three
    # colour states exist in the css, and the full phrase is preserved (title/aria) for the veteran.
    assert "dep-status" in _BOARDS, "boards.js must render a short STATUS label (dep-status)"
    assert ".dep-status" in _CSS, "boards.css must colour the departures STATUS label"
    for state in (".dep-status.next", ".dep-status.queued", ".dep-status.await"):
        assert state in _CSS, "boards.css must colour the '%s' STATUS state" % state
    # The arrivals remark and the departures status must share the SAME type treatment (font/size), the
    # thing that makes them read as one board's chips. Both are 11px mono with letter-spacing.
    assert re.search(r"\.solari-remark\s*\{[^}]*font:\s*600\s+11px", _CSS), "arrivals remark is 600 11px mono"
    assert re.search(r"\.dep-status\s*\{[^}]*font:\s*600\s+11px", _CSS), (
        "the departures STATUS must share the arrivals remark's 600 11px mono treatment (siblings)")


def test_departures_status_keeps_the_full_phrase_for_the_screen_reader():
    # Costume rule 2 (design record §3): the short chip never destroys the real words. The full server
    # phrase must reach a SCREEN READER as real DOM text — not a mouse-only title alone (Codex review):
    # a visually-hidden (.cc-sr-only) span carries the full phrase while the short visible chip is
    # aria-hidden, so an SR reads the whole phrase and not both. The title stays too (belt-and-braces).
    #
    # `statusDetail` IS that full phrase: `statusFull` on every ordinary row, and on a PAPERWORK row
    # (issue #138) the server's fuller plain-words reason — which label is bad and how to fix it. A
    # refused flight is exactly where the short chip would destroy the most, so it carries the most.
    assert "cc-sr-only" in _BOARDS and "esc(statusDetail)" in _BOARDS, (
        "the full status phrase (statusDetail) must ride in a .cc-sr-only span so a screen reader reads it")
    assert "var statusDetail = d.refusal_text || statusFull;" in _BOARDS, (
        "statusDetail must fall back to the full server phrase whenever there is no refusal reason")
    assert re.search(r'aria-hidden="true"', _BOARDS) and "esc(statusLabel)" in _BOARDS, (
        "the short visible chip (statusLabel) must be aria-hidden so the SR reads the full phrase only")
    assert ".cc-sr-only" in _CSS, "boards.css must ship the visually-hidden utility the SR text uses"
    assert re.search(r'title="[^"]*\+\s*esc\(statusDetail\)', _BOARDS), (
        "the full phrase should also stay on the hover title for a mouse user")


def test_departures_explains_the_expedite_marker_in_place():
    # The ⚡ marker must be self-explanatory without hover (#31 DoD: legend or hover — we ship a legend).
    assert "dep-legend" in _BOARDS, "boards.js must render an in-place ⚡ legend (dep-legend)"
    assert ".dep-legend" in _CSS, "boards.css must style the ⚡ legend"
    seg = _BOARDS[_BOARDS.index("dep-legend"):] if "dep-legend" in _BOARDS else ""
    assert "⚡" in seg and re.search(r"EXPEDITE", seg, re.I), (
        "the legend must show the ⚡ glyph and the word EXPEDITE so the marker explains itself")


# =============================== nothing the paging pass established regressed ===============================

def test_departures_still_paginates_and_keeps_its_page_control():
    # This visual pass must not disturb the #30 paging contract that the sibling redesign renders around.
    assert re.search(r"deps\.slice\(\s*start\s*,\s*start\s*\+\s*DEP_PAGE_SIZE\s*\)", _BOARDS)
    assert "dep-pager" in _BOARDS and "data-deppage" in _BOARDS
