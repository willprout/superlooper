"""The runner's launch rules, MIRRORED — the departures board's bridge to the engine's order.

The departures board promises "what's approved and waiting, in real launch order". That promise is
only kept if the board's order is the RUNNER's order. The runner decides it in
``skills/superlooper/skill/lib/issues.py`` (``eligible`` / ``sort_key``, spent by
``scheduler.launchable``); every rule in this module is a deliberate mirror of a NAMED rule there —
never the dashboard's own policy. If the two ever disagree, this file is wrong, not the engine.

Why mirror instead of import? The dashboard is the loop's face and must never touch the engine: it
ships standalone (``bin/install.sh``), and the engine stays dashboard-agnostic. So the copy is kept
honest by TESTS rather than by coupling — ``tests/test_launch_rules.py`` reads the engine's own
``eligible()``/``sort_key()`` off disk whenever they're there (the monorepo, where CI runs both
suites) and fails the moment the mirror drifts. That test is the bridge the board lacked (issue
#138): before it, the two copies of the rules had drifted far enough that the board could advertise
an issue as NEXT OFF THE STAND that the runner would never launch.

What is mirrored (engine source → what's here):

  ``issues.VALID_TYPES``            → :data:`TYPE_KINDS`
  ``issues.parse_issue``            → :func:`priority_rank`, :func:`has_expedite`, the ``type:`` /
                                      ``model:`` / ``effort:`` label reading inside :func:`refusal`
  ``issues.eligible``               → :func:`refusal` — a bad ``type:`` or a control-label conflict
                                      never launches, so the board must never show it launchable
  ``queue_lint.lint``               → :func:`refusal`'s TERRITORY half (issue #225) — a
                                      merge-producing issue that declares no parseable ``touches:``
                                      under ``## Loop metadata`` is PARKED by the runner, so it is
                                      never a flight waiting its turn
  ``issues.sort_key``               → :func:`sort_key`
  ``actions.RELAUNCHABLE_STATUSES`` → :data:`RELAUNCHABLE_STATUSES`

What is deliberately NOT mirrored: lane capacity, anti-affinity, usage caps, the launch-failure cap,
and the ``in-progress`` label check. These gate whether the runner launches a candidate AT ALL on a
given tick; none of them REORDERS the queue. The board shows the launch ORDER; it does not predict
the tick.

One rule is mirrored only HALFWAY, on purpose. ``queue_lint`` also reports a ``touches:`` naming an
area the repo never declared — and that is NOT a board refusal, because the runner does not validate
area names and launches such an issue happily (the gate's wander check is what eventually catches
its diff). Rendering INVALID over something the runner is about to launch would be the very drift
#138 was filed for, pointed the other way. That defect is surfaced by ``superlooper doctor --repo``,
by the runner's own refusal journal, and by the janitor's remediation — never here.

The territory half also needs two facts the label half does not: the repo's declared ``areas`` and
its ``touches_required`` knob. Both arrive from the repo entry (``config._enrich_repo``) and both
default to "unknown", which stands the check DOWN — a board that guessed ``touches_required: True``
for a repo that never said so would paint every issue in it PAPERWORK at once.

Two of those are worth naming honestly rather than waving at, because they are not strictly
transient. ``launch_failures`` is a PERSISTENT per-issue counter, and the runner's ``base_missing``
path bumps it while leaving ``agent-ready`` in place — so an at-cap issue can read as launchable
here for up to one tick, until the runner's own park lands and strips the label (which takes it off
this board by itself). The window is a tick and it closes on its own, so mirroring the cap would buy
noise, not truth. If that ever stops being true, it belongs here with a parity test — not in a
comment.

``blocked-by`` is the one eligibility rule the board renders RICHER than the engine: the runner
simply drops a dependency-blocked issue from its candidates, while the board keeps showing it as
"awaiting connection SL-N", never launchable (design record §3). That is a rendering of the same
verdict, not a different verdict.

Everything here is a pure function of a label list, an issue body and a loopstate dict — no I/O,
no ``gh``.
"""
import re

# The three issue kinds. Mirror of issues.VALID_TYPES — an issue whose `type:` label is missing,
# unknown, or doubled parses to "invalid", and eligible() refuses to launch it.
TYPE_KINDS = ("build", "investigate", "diagnose-and-fix")

# Investigations produce no PR and no merge, so a territory declaration is meaningless for them.
# Mirror of actions._MERGE_PRODUCING_TYPES (via queue_lint.MERGE_PRODUCING_TYPES).
MERGE_PRODUCING_KINDS = ("build", "diagnose-and-fix")

# The `## Loop metadata` section, and the `touches:` line inside it. Mirror of
# issues.parse_sections + issues.parse_loop_metadata: a section runs from its `## X` line to the
# next `## `, and only a plain `touches:`-prefixed line INSIDE it is a declaration — so a
# "this work touches: the engine" phrase in Goal prose is never mistaken for one.
_H2 = re.compile(r"^##\s+(.*\S)\s*$")
_TOUCHES_ANYWHERE = re.compile(r"^[ \t]*touches[ \t]*:(.*)$", re.IGNORECASE | re.MULTILINE)
# The canonical shape every surface quotes. Mirror of queue_lint.METADATA_SHAPE.
METADATA_SHAPE = "## Loop metadata / touches: <area>[, <area>...]"

# The loopstate statuses from which the runner will still launch an issue (mirror of
# actions.RELAUNCHABLE_STATUSES). `None` is the never-launched issue that has no loopstate entry at
# all. "ready" covers BOTH the fresh issue and — the case the board used to miss entirely — the
# conflict-rebuilt one the regenerate path re-releases with `agent-ready` and `requeue_front`.
RELAUNCHABLE_STATUSES = frozenset([None, "ready", "parked", "needs_william", "bounced"])

# Priority bands. The runner reads EXACTLY two labels — `if "priority:high" in labels: 1 elif
# "priority:low" in labels: 3 else: 2` — so the vocabulary is closed and the match is EXACT.
# Anything else (absent, `priority:normal`, a bare number, a mis-cased `Priority:High`) is the plain
# middle band to the runner, and so it must be here. The board once accepted a `medium` alias and
# bare numeric `priority:<n>` labels the runner never learned, which is how it could rank a
# `priority:0` issue ahead of a real `priority:high` one and then watch the runner do the opposite.
_PRIORITY_LABEL_RANK = {"priority:high": 1, "priority:low": 3}
_DEFAULT_PRIORITY_RANK = 2
_BAND_NAME = {1: "high", 2: "normal", 3: "low"}

_EXPEDITE_LABEL = "expedite"
_TYPE_PREFIX = "type:"

# The refusal each broken label set earns. `flap` is the split-flap phrase (the board's own signage
# voice); `text` is the plain sentence — it names the offending label in its REAL casing and says
# how to fix it, so the owner can act from where they read it (design record §0.3, tap-where-you-
# read). Keyed by the code :func:`refusal` returns.
_TYPE_KINDS_PHRASE = ", ".join(_TYPE_PREFIX + k for k in TYPE_KINDS[:-1]) + " or " + _TYPE_PREFIX + TYPE_KINDS[-1]


def label_names(labels):
    """A gh label list — ``[{"name": "x"}, …]`` or bare ``["x", …]`` — as a LIST of name strings.

    A list, not a set: the runner COUNTS occurrences (two ``type:`` labels is a refusal), so
    collapsing duplicates would quietly forgive a conflict the runner refuses. Mirror of
    ``issues._label_names``, including its tolerance: any junk entry (a non-dict/str, a non-string
    name, ``None``) is skipped rather than raised on, so a half-read issue never breaks a poll."""
    if not isinstance(labels, list):
        return []
    out = []
    for lb in labels:
        if isinstance(lb, dict):
            name = lb.get("name")
        elif isinstance(lb, str):
            name = lb
        else:
            name = None
        if isinstance(name, str) and name:
            out.append(name)
    return out


def priority_rank(labels):
    """The runner's priority band as its own number: ``1`` high, ``2`` normal, ``3`` low (SMALLER
    leaves sooner). Mirror of ``issues.parse_issue``'s band read — exact label match, closed
    vocabulary, everything unrecognized is the honest middle. With both band labels present the
    runner's if-chain checks high first, so high wins; ``min`` gives the same answer for any label
    order (the board must never flap on gh's list order)."""
    ranks = [_PRIORITY_LABEL_RANK[n] for n in label_names(labels) if n in _PRIORITY_LABEL_RANK]
    return min(ranks) if ranks else _DEFAULT_PRIORITY_RANK


def band_name(rank):
    """The band's display name for a rank — ``high`` / ``normal`` / ``low``. The board shows the
    runner's own bands and no others (there is no ``medium``)."""
    return _BAND_NAME.get(rank, _BAND_NAME[_DEFAULT_PRIORITY_RANK])


def has_expedite(labels):
    """Whether the ⚡ lane applies. Mirror of ``issues.parse_issue``: an exact ``expedite`` label."""
    return _EXPEDITE_LABEL in label_names(labels)


def _control_conflict(names, prefix):
    """The refusal code a ``model:``/``effort:`` control knob earns, or ``None``. Mirror of
    ``issues._single_control_label``, which fails CLOSED both ways: 2+ labels is ambiguous, and a
    bare ``model:`` is malformed — neither silently falls back to the default, both make eligible()
    refuse until the owner fixes the labels."""
    vals = [n[len(prefix):] for n in names if n.startswith(prefix)]
    if not vals:
        return None
    if len(vals) > 1:
        return "duplicate"
    return None if vals[0].strip() else "blank"


def declared_touches(body):
    """The `touches:` areas this issue declares, read EXACTLY where the engine reads them: inside
    the `## Loop metadata` section only. ``[]`` when there is none (or the body is missing or
    wrong-typed). Mirror of ``issues.parse_loop_metadata``'s touches read."""
    if not isinstance(body, str):
        return []
    meta, current = [], None
    for line in body.splitlines():
        m = _H2.match(line)
        if m:
            current = m.group(1).strip().lower()
        elif current == "loop metadata":
            meta.append(line)
    for line in meta:
        s = line.strip()
        if s.lower().startswith("touches:"):
            return [t.strip() for t in s.split(":", 1)[1].split(",") if t.strip()]
    return []


def _touches_refusal(names, body, areas, touches_required):
    """The TERRITORY half of :func:`refusal` (issue #225), or ``None``.

    Mirror of the blocking half of ``queue_lint.lint``: for a merge-producing issue in a repo that
    requires a declaration, a body the engine parses no ``touches:`` from means the runner PARKS it
    rather than launching it. An area name the repo never declared is NOT mirrored — see the module
    docstring."""
    if not touches_required:
        return None
    type_vals = [n[len(_TYPE_PREFIX):] for n in names if n.startswith(_TYPE_PREFIX)]
    if len(type_vals) != 1 or type_vals[0] not in MERGE_PRODUCING_KINDS:
        return None                      # an investigation, or a type the label half already refused
    if declared_touches(body):
        return None
    where = ("Add" if not isinstance(body, str) or not _TOUCHES_ANYWHERE.search(body)
             else "There is a `touches:` line, but not one the runner can parse. Put it under a "
                  "`## Loop metadata` heading as")
    code = "touches_missing" if where == "Add" else "touches_outside_section"
    known = sorted(a for a in areas if isinstance(a, str)) if isinstance(areas, dict) else []
    where_areas = (" This repo's areas: %s (or `*` for unknown scope)." % ", ".join(known)
                   if known else "")
    return {"code": code,
            "flap": "NO TOUCHES DECLARED" if code == "touches_missing" else "TOUCHES NOT IN METADATA",
            "text": "The runner parks this flight: it declares no `touches:` the loop can read, and "
                    "that declaration is what anti-affinity and the gate's wander check verify "
                    "against. %s `%s`.%s" % (where, METADATA_SHAPE, where_areas)}


def refusal(labels, body=None, areas=None, touches_required=False):
    """Why the RUNNER would refuse to launch this issue, or ``None`` if it would launch it.

    Mirror of ``issues.eligible``'s label rules — a valid ``type:`` and no ``model:``/``effort:``
    conflict — plus (issue #225) the runner's touches-required park, checked in the ENGINE'S OWN
    ORDER (type, then the control knobs, then the territory declaration), so the reason the board
    names is the rule the runner actually hits first, never a second-order one.

    Returns ``None``, or ``{"code", "flap", "text"}``: ``code`` is the discrete reason (the pixels
    never parse prose), ``flap`` the split-flap phrase, ``text`` the plain sentence naming what is
    wrong and how to fix it.

    ``body``/``areas``/``touches_required`` default to the pre-#225 behaviour — labels alone — so a
    caller with no repo config in reach (a standalone dashboard) judges exactly what it can prove.

    The two rules eligible() also applies are handled by the caller, not here: ``agent-ready`` is
    how the candidates were queried in the first place, and ``blocked-by`` is the board's richer
    "awaiting connection" state (see the module docstring)."""
    names = label_names(labels)

    type_vals = [n[len(_TYPE_PREFIX):] for n in names if n.startswith(_TYPE_PREFIX)]
    if not type_vals:
        return {"code": "type_missing", "flap": "NO TYPE LABEL",
                "text": "No type: label — the runner won't launch this flight. Add %s."
                        % _TYPE_KINDS_PHRASE}
    if len(type_vals) > 1:
        return {"code": "type_duplicate", "flap": "TWO TYPE LABELS",
                "text": "Two type: labels (%s) — the runner needs exactly one to launch this "
                        "flight. Remove all but one."
                        % ", ".join(_TYPE_PREFIX + v for v in type_vals)}
    if type_vals[0] not in TYPE_KINDS:
        return {"code": "type_unknown", "flap": "UNKNOWN TYPE LABEL",
                "text": "%s%s isn't a kind the runner knows, so it won't launch this flight. "
                        "Use %s." % (_TYPE_PREFIX, type_vals[0], _TYPE_KINDS_PHRASE)}

    for prefix, kind in (("model:", "model"), ("effort:", "effort")):
        conflict = _control_conflict(names, prefix)
        if conflict == "duplicate":
            return {"code": kind + "_duplicate", "flap": "TWO %s LABELS" % kind.upper(),
                    "text": "Two %s labels — the runner needs at most one to launch this flight. "
                            "Remove all but one." % prefix}
        if conflict == "blank":
            return {"code": kind + "_blank", "flap": "%s LABEL BLANK" % kind.upper(),
                    "text": "The %s label has no value — the runner won't guess, so it won't launch "
                            "this flight. Give it a value or remove the label." % prefix}
    return _touches_refusal(names, body, areas, touches_required)


def sort_key(num, expedite, rank, requeue_front, created_at):
    """One flight's place in the launch order, as the runner would compute it.

    Mirror of ``issues.sort_key`` — ``(not expedite, priority, not requeue_front, created_at)`` —
    with the issue number appended. The number is NOT an invention: the runner's candidate list is
    pre-sorted by issue number (``actions._sorted_ids``) and Python's sort is stable, so number IS
    the runner's own final tiebreak for two flights that tie on everything else. Appending it makes
    the board's order fully determined by the facts (never a set-order flake).

    ``created_at`` is gh's raw ``createdAt`` — an ISO-8601 Z string, so lexical order IS
    chronological order. A missing/wrong-typed value coerces to ``""`` and sorts oldest-first,
    exactly as ``issues.parse_issue`` does (never ``or ""``: a truthy non-string would slip through
    and then raise when compared against real timestamps on Python 3.9)."""
    return (not expedite, rank, not requeue_front,
            created_at if isinstance(created_at, str) else "",
            num if isinstance(num, int) else 0)


def is_launch_candidate(issue_state):
    """Would the runner still launch this issue, given its loopstate entry (``None`` when it has
    none)? Mirror of ``actions._eligible_launch_ids``'s status gate — including, precisely, its
    ``_status_of`` fold.

    This is the rule the board needs to see a REQUEUED flight at all. Launching strips
    ``agent-ready`` (so an in-flight issue leaves the board by itself), but the conflict-rebuild
    path puts ``agent-ready`` BACK with status ``ready`` + ``requeue_front`` — that issue is queued
    again, and the runner will launch it next, front of its band.

    A wrong-typed status fails CLOSED — never launched, and never RAISED on. That second half is
    load-bearing and is why this reads the status through the same fold the engine uses
    (``_status_of``/``_CORRUPT_STATUS``, engine issue #95): an UNHASHABLE status (a list, a dict)
    would make a bare ``status in <frozenset>`` throw TypeError, and this runs inside the snapshot
    poll — so one corrupt entry would take down the whole board, every repo and every flight, not
    merely its own row. ``None`` must survive the fold intact: it is a legitimate member of
    RELAUNCHABLE_STATUSES (the never-launched issue), not a corrupt value."""
    if issue_state is None:
        return True
    if not isinstance(issue_state, dict):
        return False
    status = issue_state.get("status")
    if not (status is None or isinstance(status, str)):
        return False
    return status in RELAUNCHABLE_STATUSES


def requeue_front(issue_state):
    """Whether loopstate says this issue was re-fronted after a conflict rebuild. Coerced to a real
    bool exactly as the runner does (``bool(ist.get("requeue_front"))``)."""
    return bool(issue_state.get("requeue_front")) if isinstance(issue_state, dict) else False
