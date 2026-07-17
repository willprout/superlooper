"""Reading the fresh-agent review verdict off a PR (issue #176) — a DELIBERATE MIRROR of the
engine's ``skills/superlooper/skill/lib/gate.py`` (which owns ``_REVIEW_MARKER_RE`` /
``_REVIEW_PIN_RE`` / ``_OID_RE`` and ``review_evidence_state``).

**Why this is here at all.** The board's gate checklist has to answer the same question the engine's
gate does — "does this PR carry a review verdict for the code being merged?" — so the "why isn't
this landing" surface agrees with the mechanism. #176 is what happens when it does NOT: the
dashboard kept its own literal ``"<!-- superlooper-review -->"`` and matched it as a SUBSTRING, so
once #154 pinned the verdict to its diff (``<!-- superlooper-review sha=<oid> -->``) every correctly
reviewed PR read as "review: missing". A private copy of a contract silently drifted out of step
with the contract — the class-D12 doc/telemetry drift the reliability ledger warns about.

**Why a MIRROR and not an import.** The obvious fix is to call ``gate.review_evidence_state``
directly. The dashboard cannot: it is a separate, stdlib-only deployable that renders over each
ADOPTED repo's GitHub state, and an adopted checkout need not contain the engine at all (a friend
who took superlooper for their own repo has no ``skills/superlooper`` tree — see lib/engine.py's
"source_repo" for the same fact). There is no import path to the engine from the dashboard's
runtime. So this is a mirror, and ``tests/test_review_marker.py`` PINS its three regexes byte-for-
byte to gate.py's source: a future marker-format change turns that test red instead of silently
re-breaking the board. This is the same discipline ``lib/engine.PAYLOAD_REL`` uses to stay in step
with ``bin/install.sh`` — a documented mirror plus a test that reads the other side.

**What is deliberately NOT mirrored.** ``gate.review_evidence_state`` also folds in two facts the
dashboard does not have and cannot honestly reproduce:

  * ``ship_cmd`` — a repo whose OWN pipeline owns review, where the marker contract doesn't apply.
    The dashboard doesn't read per-repo engine config, so a ``ship_cmd`` repo may show a review line
    the gate would waive. This is unchanged by #176: the old substring check showed the same line.
  * ``review_carry`` — the runner's private record that IT merge-updated the branch (moved the head
    without touching the authored diff), so the verdict rides across. The dashboard reads only public
    GitHub state, so a PR the runner just merge-updated reads ``STALE`` here until a fresh pinned
    verdict lands. That errs toward "not proven for this diff", never toward a false green — the same
    fail-closed direction as the gate, and strictly better than the old code, which showed a bare
    "missing" for it.

So this module answers the reduced question the dashboard CAN answer honestly from a PR's comments
and head oid, and collapses the engine's ``stale`` + ``unpinned`` into one board-facing ``STALE``
("a review exists, but not provably for this diff"). Every path fails closed.
"""
import re

# MIRROR — byte-identical to gate.py's. These are copies, not an independent design: do not "improve"
# one without the other. tests/test_review_marker.py pins each pattern to gate.py's source so the two
# cannot drift apart unnoticed (that drift is the whole of issue #176).
#
# The marker match is deliberately LOOSE about what rides between ``superlooper-review`` and ``-->``;
# the pin is validated separately, so a MALFORMED pin reads as "marker needing a repin", never as
# "no marker at all". The payload must be WHITESPACE-separated (or absent), so a sibling in the
# ``<!-- superlooper-`` family (``superlooper-review-notes`` …) is not mistaken for a verdict.
_REVIEW_MARKER_RE = re.compile(r"<!--\s*superlooper-review(\s[^\n]*?)?-->", re.IGNORECASE)
_REVIEW_PIN_RE = re.compile(r"\bsha\s*=\s*(\S+)", re.IGNORECASE)
# A readable git oid. 7 hex is git's own default abbreviation and identifies a commit unambiguously
# on a single PR; shorter fails closed rather than prefix-matching loosely.
_OID_RE = re.compile(r"[0-9a-fA-F]{7,40}")

# The board-facing states. Fewer than the engine's (no ``ship``; ``stale`` + ``unpinned`` +
# ``head_unreadable`` fold into STALE/UNREAD per the module docstring), so the checklist can draw the
# three distinctions #176 asks for without claiming knowledge the dashboard doesn't have.
REVIEWED = "reviewed"   # a verdict pinned to the PR's current head — provably this diff (gate "ok")
STALE = "stale"         # a marker exists but not provably for this diff (superseded/legacy/malformed)
ABSENT = "absent"       # a clean comments read with no review marker at all
UNREAD = "unread"       # the comments (or the head) could not be read — fail closed, NOT "no review"


def _oid(v):
    """A readable git oid, lowercased for comparison — else None (fail closed). fullmatch, so a
    string that merely CONTAINS hex ('sha: abc1234!') is not an oid."""
    return v.lower() if isinstance(v, str) and _OID_RE.fullmatch(v) else None


def _review_pins(comments):
    """Every review-marker comment's claimed pin, in order, as the RAW string (validated by the
    caller) — or None for a marker carrying no ``sha=`` at all. The marker must BEGIN the comment
    (leading whitespace ignored): quoting it mid-text is not a verdict. A wrong-typed list or entry
    contributes nothing (fail closed, never raises)."""
    out = []
    for c in comments if isinstance(comments, list) else []:
        body = c.get("body") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if isinstance(body, str):
            m = _REVIEW_MARKER_RE.match(body.lstrip())
            if m:
                # group(1) is None for the payload-less ``<!-- superlooper-review-->``; ``or ""``
                # keeps that from raising (a corrupt input must never except into the poll).
                pin = _REVIEW_PIN_RE.search(m.group(1) or "")
                out.append(pin.group(1) if pin else None)
    return out


def review_state(pr_comments, head_oid):
    """The board's reading of a PR's review evidence, mirroring ``gate.review_evidence_state`` over
    the inputs the dashboard has (a PR's comments + its current head oid). Returns one of REVIEWED /
    STALE / ABSENT / UNREAD:

      REVIEWED — a review-marker comment carries a pin that matches (as a prefix — abbreviations are
                 honored) the PR's current head: a verdict provably for THIS diff.
      STALE    — a marker exists, but no readable pin matches the head: a superseded pin, the legacy
                 unpinned form, or a malformed pin. A review HAPPENED; the board just cannot prove it
                 covers the current code. Distinct from ABSENT so "reviewed, then rebuilt" never
                 reads as "never reviewed".
      ABSENT   — a clean read with no review marker at all.
      UNREAD   — the comments read was refused/starved (wrong-typed), or a marker exists but the head
                 oid is unreadable. Fail closed — the caller must not paint a confident cross.

    Every branch fails closed: only REVIEWED lets the gate line go green, matching the engine, which
    merges only on a pin that provably covers the head."""
    if not isinstance(pr_comments, list):
        return UNREAD
    pins = _review_pins(pr_comments)
    if not pins:
        return ABSENT
    head = _oid(head_oid)
    if head is None:
        return UNREAD          # a marker exists but the head can't be judged — gate's head_unreadable
    for p in pins:
        po = _oid(p)
        if po and head.startswith(po):
            return REVIEWED
    return STALE               # gate's stale OR unpinned — a review exists, not provably for this diff
