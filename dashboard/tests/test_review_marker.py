"""The dashboard's review-marker parse (issue #176) — a DELIBERATE MIRROR of the engine's
``skills/superlooper/skill/lib/gate.py``. Two duties are tested here:

  * the mirror still AGREES with the engine — its three regexes are pinned byte-for-byte to the
    source they copy, so a future marker-format change (the exact drift that caused #176, where the
    dashboard's private literal stopped matching #154's pinned verdict) turns a test red rather than
    silently making the board lie; and
  * ``review_state`` reads a PINNED verdict as reviewed, a SUPERSEDED/legacy one as "stale", and no
    marker at all as "absent" — the three-way split #176 asks the board to draw (never-reviewed vs
    reviewed-then-rebuilt vs reviewed-for-this-diff), all fail-closed.
"""
from pathlib import Path

import pytest

import review_marker as rm

# The monorepo root: this dashboard is a subdirectory of it, and the engine it mirrors lives at
# skills/superlooper/skill/lib/gate.py. Reached by PATH (read as text, never imported) so the pin
# cannot pull the engine's module graph onto the dashboard's sys.path — the same discipline
# test_engine.py uses to pin lib/engine.PAYLOAD_REL to bin/install.sh. The dashboard bills itself as
# separable (its own repo), so if the engine source is absent — an adopter who took command-center
# alone — the pin SKIPS rather than erroring at collection; the runtime review_marker.py never reads
# gate.py, so its behaviour is unaffected. In THIS monorepo the file is present and the pin runs.
_GATE_PATH = Path(__file__).resolve().parent.parent.parent / "skills" / "superlooper" / "skill" \
    / "lib" / "gate.py"


def _gate_source():
    if not _GATE_PATH.is_file():
        pytest.skip("engine gate.py not present (dashboard checked out without the monorepo)")
    return _GATE_PATH.read_text(encoding="utf-8")

# Three distinct, well-formed head oids, mirroring test_gate.py's own constants. HEAD is "the PR's
# current head"; OTHER stands for a superseded (gen-1) head.
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER = "b9c8d7e6f5a41302f1e0d9c8b7a6958473625140"


def _pinned(sha):
    """The pinned marker a worker posts (#154). Spelled out here rather than imported from the
    engine so the parse under test is exercised against a literal, not against the same code that
    produced it; the regexes are pinned to the engine separately below."""
    return "<!-- superlooper-review sha=%s -->" % sha


# --------------------------- the mirror stays honest ---------------------------

def test_regexes_are_pinned_byte_for_byte_to_the_engine_gate():
    """The whole point of #176: the dashboard must not keep a private copy of the marker contract
    that can drift out of step with the engine that parses it. These three regexes are copies of
    gate.py's; assert each source pattern still appears VERBATIM in gate.py, so a change there fails
    this test until the mirror is updated to match."""
    gate_src = _gate_source()
    for label, pat in (("_REVIEW_MARKER_RE", rm._REVIEW_MARKER_RE),
                        ("_REVIEW_PIN_RE", rm._REVIEW_PIN_RE),
                        ("_OID_RE", rm._OID_RE)):
        literal = 'r"%s"' % pat.pattern
        assert literal in gate_src, (
            "%s drifted from skills/superlooper/skill/lib/gate.py: this dashboard mirror renders\n"
            "  %s\n"
            "which no longer appears in gate.py. Re-copy the engine's regex (see review_marker.py "
            "for why this is a mirror, not an import)." % (label, literal))


# --------------------------- the three-way split #176 asks for ---------------------------

def test_pinned_verdict_for_the_current_head_reads_reviewed():
    """The headline #176 bug: a #154 pinned verdict must read as reviewed, not 'no review'."""
    c = [{"body": _pinned(HEAD) + "\nReviewed the full diff. No P0/P1."}]
    assert rm.review_state(c, HEAD) == rm.REVIEWED


def test_abbreviated_pin_matches_by_prefix():
    """A worker who pastes ``git rev-parse --short HEAD`` pins an ABBREVIATION; git's 7+-hex short
    oid identifies the commit unambiguously, so it must still read reviewed (engine parity)."""
    c = [{"body": _pinned(HEAD[:7]) + " reviewed"}]
    assert rm.review_state(c, HEAD) == rm.REVIEWED


def test_pin_for_a_superseded_head_reads_stale_not_absent():
    """The distinction #176 exists to draw: a verdict pinned to an OLD head is 'reviewed, then
    rebuilt', NOT 'never reviewed'. It must not collapse to the same bare cross as absent."""
    c = [{"body": _pinned(OTHER) + " reviewed gen-1"}]
    assert rm.review_state(c, HEAD) == rm.STALE


def test_legacy_unpinned_marker_reads_stale():
    """The pre-#154 unpinned marker cannot prove WHICH diff it reviewed. The gate fails it closed
    (never 'ok'); the board must not show a confident green either — it reads as the same
    'review exists, but not provably for this diff' bucket as a superseded pin."""
    c = [{"body": "<!-- superlooper-review --> reviewed; P0/P1: none"}]
    assert rm.review_state(c, HEAD) == rm.STALE


def test_malformed_pin_reads_stale_never_reviewed():
    """A pin the gate cannot read (an unexpanded ``$(...)``, the placeholder pasted verbatim, a
    too-short or non-hex token) is a marker needing a repin — a review exists but does not cover
    this diff. It must never read as reviewed (that would vouch for code no oid names)."""
    for bad in ("$(git rev-parse HEAD)", "REVIEWED_HEAD_OID", "abc12", "zzzzzzzz", "<HEAD-OID>"):
        c = [{"body": _pinned(bad) + " reviewed"}]
        assert rm.review_state(c, HEAD) == rm.STALE, bad


def test_no_marker_reads_absent():
    c = [{"body": "looks good to me"}, {"body": "nit: rename foo"}]
    assert rm.review_state(c, HEAD) == rm.ABSENT


def test_a_sibling_marker_is_not_a_verdict():
    """``<!-- superlooper-`` is a marker FAMILY; a sibling (``superlooper-review-notes`` etc.) must
    never vouch for a diff. Engine parity (its P2-a): the payload has to be whitespace-separated."""
    for name in ("superlooper-review-notes", "superlooper-reviewer", "superlooper-review2"):
        c = [{"body": "<!-- %s sha=%s --> not a verdict" % (name, HEAD)}]
        assert rm.review_state(c, HEAD) == rm.ABSENT, name


def test_marker_must_begin_the_comment():
    """Quoting the marker mid-text is not a verdict — the marker must BEGIN the comment (engine
    parity with _any_comment_begins). Leading whitespace is tolerated; prose before it is not."""
    quoted = [{"body": "I posted `%s` earlier" % _pinned(HEAD)}]
    assert rm.review_state(quoted, HEAD) == rm.ABSENT
    lead_ws = [{"body": "   \n" + _pinned(HEAD) + " reviewed"}]
    assert rm.review_state(lead_ws, HEAD) == rm.REVIEWED


def test_unreadable_comments_read_unread_not_absent():
    """Fail closed like the gate (issue #78): a refused/starved comments read is NOT 'no review'.
    The board must not paint a confident 'never reviewed' cross over a read that never happened."""
    assert rm.review_state(None, HEAD) == rm.UNREAD
    assert rm.review_state("boom", HEAD) == rm.UNREAD


def test_unreadable_head_with_a_marker_present_is_unread():
    """A marker exists but the PR view carries no readable head oid — the pin cannot be judged, so
    fail closed (engine's head_unreadable). Never claim reviewed off an unjudgeable head."""
    c = [{"body": _pinned(HEAD) + " reviewed"}]
    for bad_head in (None, "", "not-an-oid", 12345):
        assert rm.review_state(c, bad_head) == rm.UNREAD, bad_head


def test_a_readable_pin_among_junk_still_reads_reviewed():
    """A legacy marker plus a current pinned verdict must read reviewed — the good pin wins, exactly
    as the gate honors any attesting pin among several comments."""
    c = [{"body": "<!-- superlooper-review --> legacy"},
         {"body": _pinned(HEAD) + " reviewed current"}]
    assert rm.review_state(c, HEAD) == rm.REVIEWED


def test_wrong_typed_comment_entries_never_raise():
    """A corrupt entry (not a dict, a dict with no str body) simply doesn't count — fail closed,
    never an exception into the poll (module rule inherited from the gate)."""
    c = [42, {"nobody": True}, {"body": None}, {"body": _pinned(HEAD)}]
    assert rm.review_state(c, HEAD) == rm.REVIEWED
    c2 = [42, {"nobody": True}, {"body": None}]
    assert rm.review_state(c2, HEAD) == rm.ABSENT
