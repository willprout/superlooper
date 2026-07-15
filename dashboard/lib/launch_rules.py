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
  ``issues.sort_key``               → :func:`sort_key`
  ``actions.RELAUNCHABLE_STATUSES`` → :data:`RELAUNCHABLE_STATUSES`

What is deliberately NOT mirrored: lane capacity, anti-affinity, usage caps, the launch-failure cap
and the touches-required park. Those are TICK-LOCAL — they decide whether the runner launches a
candidate on THIS tick, not where the candidate sits in the queue. The board shows the launch
ORDER; it does not predict the tick. (``blocked-by`` is the one eligibility rule the board renders
richer than the engine: the runner simply drops a dependency-blocked issue from its candidates,
while the board keeps showing it as "awaiting connection SL-N", never launchable — design record
§3. That is a rendering of the same verdict, not a different verdict.)

Everything here is a pure function of a label list and a loopstate dict — no I/O, no ``gh``.
"""

# The three issue kinds. Mirror of issues.VALID_TYPES — an issue whose `type:` label is missing,
# unknown, or doubled parses to "invalid", and eligible() refuses to launch it.
TYPE_KINDS = ("build", "investigate", "diagnose-and-fix")

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


def refusal(labels):
    """Why the RUNNER would refuse to launch this issue, or ``None`` if it would launch it.

    Mirror of ``issues.eligible``'s label rules — a valid ``type:`` and no ``model:``/``effort:``
    conflict — checked in the ENGINE'S OWN ORDER (type first, then the control knobs), so the reason
    the board names is the rule the runner actually hits first, never a second-order one.

    Returns ``None``, or ``{"code", "flap", "text"}``: ``code`` is the discrete reason (the pixels
    never parse prose), ``flap`` the split-flap phrase, ``text`` the plain sentence naming the bad
    label and how to fix it.

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
    return None


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
    none)? Mirror of ``actions._eligible_launch_ids``'s status gate.

    This is the rule the board needs to see a REQUEUED flight at all. Launching strips
    ``agent-ready`` (so an in-flight issue leaves the board by itself), but the conflict-rebuild
    path puts ``agent-ready`` BACK with status ``ready`` + ``requeue_front`` — that issue is queued
    again, and the runner will launch it next, front of its band. A wrong-typed status is not one
    the runner launches from, so it fails closed here too (engine issue #95)."""
    if issue_state is None:
        return True
    if not isinstance(issue_state, dict):
        return False
    return issue_state.get("status") in RELAUNCHABLE_STATUSES


def requeue_front(issue_state):
    """Whether loopstate says this issue was re-fronted after a conflict rebuild. Coerced to a real
    bool exactly as the runner does (``bool(ist.get("requeue_front"))``)."""
    return bool(issue_state.get("requeue_front")) if isinstance(issue_state, dict) else False
