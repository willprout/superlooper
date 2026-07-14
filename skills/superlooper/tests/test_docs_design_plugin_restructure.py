"""Durable-record guards for the plugin-restructure design (issue #82).

Issue #65's approved plugin-restructure design lived only in a GitHub comment (the one
beginning ``<!-- superlooper-investigation -->``). Issue #82 — the first child of the
restructure — commits it as a durable file so the design survives independent of GitHub.

These tests pin two facts the DoD requires and that must not silently regress:

  1. ``docs/DESIGN-2026-07-11-plugin-restructure.md`` exists and carries the FULL report
     (not a stub) — every section 0-10, every resolved decision D1-D10, every owner
     decision O1-O4, the load-bearing layout/skill anchors, and the report's closing line
     — introduced by a short provenance header pointing back at the #65 comment (which the
     DoD explicitly permits in place of the raw marker line).
  2. The V2-IDEAS plugin-restructure bullet points at that record with a status of
     "designed / record committed", so a reader of the ledger is led to the durable file.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DESIGN = _ROOT / "docs" / "DESIGN-2026-07-11-plugin-restructure.md"
_V2 = _ROOT / "docs" / "V2-IDEAS.md"


def _design_text():
    return _DESIGN.read_text(encoding="utf-8")


def _v2_text():
    return _V2.read_text(encoding="utf-8")


def _plugin_bullet(text):
    """The V2-IDEAS 'Plugin restructure' top-level bullet: from its '- **Plugin restructure'
    line up to (but not including) the next top-level '- **' bullet or section header."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("- **Plugin restructure"):
            start = i
            break
    assert start is not None, "V2-IDEAS.md must have a '- **Plugin restructure' bullet"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s.startswith("- **") or s.startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_design_record_exists_and_is_titled():
    assert _DESIGN.exists(), f"design record must exist at {_DESIGN}"
    text = _design_text()
    first = next(ln for ln in text.splitlines() if ln.strip())
    # The report's own H1 leads the file — not a bare HTML marker comment.
    assert first == "# Design: the superlooper plugin restructure (issue #65)", (
        f"first content line must be the report H1, got {first!r}"
    )
    assert not first.startswith("<!--"), "the raw investigation marker must not lead the file"


def test_provenance_header_points_back_at_the_65_comment():
    text = _design_text()
    # The DoD allows a short provenance header in place of the marker line; it must name its
    # source so a reader can trace the record to the #65 report comment.
    assert "issue #65" in text
    assert "superlooper-investigation" in text, (
        "provenance must name the #65 report comment's marker so the source is traceable"
    )
    assert "#82" in text, "provenance should record that #82 committed the record"


def test_design_record_carries_every_section():
    text = _design_text()
    for n in range(0, 11):
        assert f"## {n}." in text, f"design record is missing section '## {n}.'"


def test_design_record_carries_every_decision():
    text = _design_text()
    for i in range(1, 11):
        assert f"**D{i} —" in text, f"design record is missing resolved decision D{i}"
    for i in range(1, 5):
        assert f"**O{i} —" in text, f"design record is missing owner decision O{i}"


def test_design_record_carries_load_bearing_anchors():
    text = _design_text()
    # The five-skill roster (the frozen owner ruling the whole design rests on).
    assert (
        "five skills (`superlooper`, `write-issue`, `adopt`, `cross-review`, `sl-debugger`)"
        in text
    ), "the five-skill roster must survive verbatim"
    # The repo-as-marketplace layout anchors.
    assert ".claude-plugin/marketplace.json" in text
    assert "plugin/" in text
    # The report's closing line — proves the full body landed, not a truncation.
    assert "Zero PRs from this investigation; no code changed." in text
    # The §10 child roster the report filed (by description — the report body lists the nine
    # children as prose, not by issue number; #83-#90 live only in the separate child-index
    # comment, which is deliberately NOT reproduced here).
    for child in (
        "Scaffold the marketplace + plugin",
        "Mechanical inert-plugin fence in CI",
        "Root README: the paste-URL install path",
    ):
        assert child in text, f"design record should list §10 child {child!r}"


def test_v2_ideas_bullet_points_at_the_record():
    bullet = _plugin_bullet(_v2_text())
    assert "DESIGN-2026-07-11-plugin-restructure.md" in bullet, (
        "the plugin-restructure bullet must point at the committed design record"
    )
    lowered = bullet.lower()
    assert "record committed" in lowered, (
        "the bullet must report the design's status: record committed"
    )
