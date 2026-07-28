"""The mechanical issue contract, in ONE place (issue #225).

The 2026-07-16 queue audit found 25 of 35 open issues unschedulable or wedge-on-approval — 16 of
them `agent-ready` — because they lacked a `type:` label and/or a parseable `## Loop metadata`
section. The queue LOOKED full while most of it could never launch, and the owner found out by
noticing an expedited issue sitting last on the departures board. The root cause was structural:
nothing anywhere surfaced an invalid issue AS invalid, so the failure mode was silence.

This module says what "mechanically valid" means, once, as a list of discrete defects. Five
surfaces read its verdict instead of each re-deriving one:

  * the PreToolUse deny (worker_pretooluse duty 3) — validates an issue BEFORE it exists, so the
    correction lands at the freshest possible turn and the session simply retries correctly;
  * the runner's refusal journal (actions) — an approved issue it will not launch is SAID, once;
  * `superlooper doctor --repo` — the audit, runnable any time;
  * the janitor's remediation — it proposes fixes only for defects whose value it can DERIVE;
  * the dashboard's departures board, through its own mirrored copy in `lib/launch_rules.py`
    (the dashboard never imports the engine — see that module's docstring).

Everything here is PURE: label lists, an issue body, and the repo's declared `areas` in, defect
dicts out. No gh, no I/O, no clock.

TWO DISCIPLINES, both paid for elsewhere in this repo:

**`blocks_launch` is the RUNNER's verdict, not an opinion.** A defect carries `blocks_launch: True`
only where the runner would genuinely refuse or park. An unknown AREA name does not stop a launch —
the runner never validates area names; the gate's wander check is what eventually catches the diff
that lands outside the declared territory. So it is REPORTED and not marked blocking. The
departures board mirrors the runner (issue #138), and a board crying INVALID over something the
runner launches happily is that same drift pointed the other way.

**Unknown input fails OPEN, per DIMENSION.** `areas=None` means "the repo's config was not in
reach" — the hook's degraded mode when it cannot find a config to read. The area names then are not
judged at all, while the type and metadata dimensions still answer. Never a blanket stand-down, and
never a confident verdict on evidence we do not have.

A defect is a dict:
  code           discrete reason (the pixels and the journal never parse prose)
  blocks_launch  would the RUNNER refuse/park over this, right now?
  what           one sentence naming what is wrong, quoting the offending value in its REAL casing
  fix            one sentence naming the exact repair
  choices        the closed set of values a MECHANICAL fix could set, or [] when there is none.
                 The janitor may only ever propose a value from here — never one it invented.
"""
import re

import issues as issues_mod

# The three issue kinds, from the engine's own single source. Imported, never re-listed: a fourth
# kind must not need editing in two places.
VALID_TYPES = issues_mod.VALID_TYPES

# Investigations produce no PR and no merge, so a territory declaration is meaningless for them —
# the engine exempts them at the launch park (actions._MERGE_PRODUCING_TYPES) and so does this.
MERGE_PRODUCING_TYPES = ("build", "diagnose-and-fix")

_TYPE_PREFIX = "type:"
_WILDCARD = "*"

# A `touches:` declaration anywhere in the body, INSIDE the Loop metadata section or not. Used only
# to tell the audit's commonest shape (the author wrote the line but not the heading) apart from a
# genuinely absent declaration — the two have different fixes, and only the first has a value a
# machine may repair without inventing anything.
_TOUCHES_LINE = re.compile(r"^[ \t]*touches[ \t]*:(.*)$", re.IGNORECASE | re.MULTILINE)
# An H2 heading line, for locating (rather than parsing) the Loop metadata section during a repair.
_H2_HEADING = re.compile(r"^##\s+\S")

_KINDS_PHRASE = ", ".join(_TYPE_PREFIX + k for k in VALID_TYPES[:-1]) + \
                " or " + _TYPE_PREFIX + VALID_TYPES[-1]

# The canonical shape the fix messages quote. One string so the hook's deny, the doctor's fix line
# and the brief's pointer cannot drift into three subtly different formats.
METADATA_SHAPE = "## Loop metadata\ntouches: <area>[, <area>...]"


def _defect(code, blocks_launch, what, fix, choices=()):
    return {"code": code, "blocks_launch": bool(blocks_launch), "what": what, "fix": fix,
            "choices": list(choices)}


def _area_names(areas):
    """The repo's declared area names, or None when they cannot be judged.

    None means the area dimension STANDS DOWN (fail open): either no config was in reach, or the
    value is wrong-typed. An EMPTY declaration stands it down too, deliberately — `areas: {}` is
    the loader's own default and a perfectly legitimate config, and judging names against nothing
    would flag every issue in such a repo as wrong."""
    if not isinstance(areas, dict) or not areas:
        return None
    names = [a for a in areas if isinstance(a, str) and a.strip()]
    return names or None


def _touches_choices(areas):
    """What a mechanical fix may set a `touches:` line to: the repo's own declared areas, plus the
    explicit unknown-scope wildcard. Empty when the areas are out of reach — the janitor then has
    nothing it may propose, which is the honest answer."""
    names = _area_names(areas)
    return (names + [_WILDCARD]) if names else []


def _bare_touches_value(body):
    """The author's own `touches:` words from a line the runner could NOT parse, else None.

    Only ever consulted once `parse_loop_metadata` has already come back empty, so any match here
    is by definition a declaration that did not land: written under the wrong heading, under no
    heading at all, or in a shape the parser's plain `touches:`-prefix read does not accept. Returns
    the raw right-hand side, verbatim — this is the one metadata defect a machine can repair without
    inventing anything, because the VALUE is already written; only the framing that makes it
    parseable is missing."""
    for m in _TOUCHES_LINE.finditer(body):
        val = m.group(1).strip()
        if val:
            return val
    return None


def _lint_labels(labels):
    """(defects, itype) for the LABEL half of the contract — the half that needs no body and no
    repo config, so it answers identically for a raw gh issue, a parsed issue, and a half-typed
    `gh issue create` command line. `itype` is the single valid kind, or None."""
    names = issues_mod._label_names({"labels": labels})
    found = []

    # --- the type: label. Exactly one, of a known kind (issues.parse_issue's own rule). ---
    type_vals = [n[len(_TYPE_PREFIX):] for n in names if n.startswith(_TYPE_PREFIX)]
    itype = type_vals[0] if (len(type_vals) == 1 and type_vals[0] in VALID_TYPES) else None
    if not type_vals:
        found.append(_defect(
            "type_missing", True,
            "no `type:` label — the runner parses this issue's kind as invalid and never launches it",
            "Add exactly one of %s." % _KINDS_PHRASE,
            choices=VALID_TYPES))
    elif len(type_vals) > 1:
        found.append(_defect(
            "type_duplicate", True,
            "two or more `type:` labels (%s) — the runner needs exactly one"
            % ", ".join(_TYPE_PREFIX + v for v in type_vals),
            "Remove all but one.",
            choices=[_TYPE_PREFIX + v for v in type_vals]))
    elif type_vals[0] not in VALID_TYPES:
        found.append(_defect(
            "type_unknown", True,
            "`%s%s` is not a kind the runner knows" % (_TYPE_PREFIX, type_vals[0]),
            "Use %s." % _KINDS_PHRASE,
            choices=VALID_TYPES))

    # --- the control knobs. Mirror of issues._single_control_label, which fails CLOSED both ways:
    #     2+ labels is ambiguous, and a bare `model:` is malformed — neither silently falls back. ---
    for prefix, kind in (("model:", "model"), ("effort:", "effort")):
        vals = [n[len(prefix):] for n in names if n.startswith(prefix)]
        if len(vals) > 1:
            found.append(_defect(
                kind + "_duplicate", True,
                "two or more `%s` labels — the runner needs at most one" % prefix,
                "Remove all but one."))
        elif len(vals) == 1 and not vals[0].strip():
            found.append(_defect(
                kind + "_blank", True,
                "the `%s` label carries no value — the runner will not guess one" % prefix,
                "Give it a value or remove the label."))
    return found, itype


def _lint_touches(declared, blocks, areas, section_present=False, bare=None, no_section_hint=True):
    """Defects for the TERRITORY half — a `touches:` declaration that is absent, unparseable, or
    names an area this repo never declared.

    `declared` is the parsed declaration (what the RUNNER actually reads). `bare` is the author's
    own `touches:` words when a line WAS written but did not parse — the only metadata defect a
    machine can repair without inventing anything, because the value is already there. A caller
    working from a parsed issue rather than a body has no `bare` to offer and says so by omission."""
    if declared:
        # The area NAMES. Reported, never blocking: the runner does not validate them — the gate's
        # wander check is what eventually reads such a diff as out-of-lane.
        known = _area_names(areas)
        unknown = [t for t in declared if t != _WILDCARD and t not in known] if known else []
        if not unknown:
            return []
        return [_defect(
            "touches_unknown", False,
            "`touches:` names %s, which this repo's config does not declare as an area — the "
            "runner launches it, then the gate reads its diff as an out-of-lane wander"
            % ", ".join("`%s`" % u for u in unknown),
            "Use one of the repo's declared areas (%s), or `*` for genuinely unknown scope."
            % ", ".join(known),
            choices=_touches_choices(areas))]

    if bare is not None:
        return [_defect(
            "touches_outside_section", blocks,
            "a `touches:` line is written but the runner parses no declaration from it — it is not "
            "a plain `touches: ...` line inside a `## Loop metadata` section",
            "Put it under a `## Loop metadata` heading, in this exact shape:\n%s" % METADATA_SHAPE,
            choices=[bare])]

    where = ("no `## Loop metadata` section at all" if no_section_hint and not section_present
             else "the `## Loop metadata` section declares no `touches:` line")
    return [_defect(
        "touches_missing", blocks,
        "%s — so the runner reads no `touches:` declaration, and that declaration is what "
        "anti-affinity and the gate's wander check verify against" % where,
        "Add:\n%s" % METADATA_SHAPE,
        choices=_touches_choices(areas))]


def lint(labels, body, areas=None, touches_required=True):
    """Every way this issue fails the mechanical contract, in the order the RUNNER hits them.

    `areas` is the repo's `areas` config block (name -> globs), or None when it is out of reach.
    `touches_required` is the repo's own knob; a wrong-typed value ENFORCES, the same fail-safe
    direction as actions._touches_required (never silently launch a no-touches issue on a garbled
    config). Returns [] for a valid issue."""
    body = body if isinstance(body, str) else ""
    found, itype = _lint_labels(labels)
    if not _territory_applies(itype, touches_required):
        return found
    sections = issues_mod.parse_sections(body)
    declared = issues_mod.parse_loop_metadata(body)["touches"]
    return found + _lint_touches(
        declared, _territory_blocks(found, itype), areas,
        section_present=any(h.strip().lower() == "loop metadata" for h in sections),
        bare=None if declared else _bare_touches_value(body))


def lint_parsed(parsed, areas=None, touches_required=True):
    """lint() from an ALREADY-parsed issue (issues.parse_issue's shape) — what the runner holds on
    a tick. It has the labels and the parsed `touches`, but no body, so the one thing it cannot
    tell apart is WHY a declaration is absent (never written, or written where the parser does not
    look). Both repair the same way, so the verdict is the same `touches_missing`, worded without
    a claim about the section it cannot see."""
    if not isinstance(parsed, dict):
        parsed = {}
    found, itype = _lint_labels(parsed.get("labels"))
    if not _territory_applies(itype, touches_required):
        return found
    declared = parsed.get("touches")
    declared = [t for t in declared if isinstance(t, str) and t.strip()] \
        if isinstance(declared, list) else []
    return found + _lint_touches(declared, _territory_blocks(found, itype), areas,
                                 no_section_hint=False)


def _territory_applies(itype, touches_required):
    """Is the territory half of the contract in force at all? Investigations produce no PR and no
    merge, so a declaration is meaningless for them; a repo may also switch the requirement off.
    A wrong-typed knob ENFORCES (actions._touches_required's own fail-safe direction)."""
    required = touches_required if isinstance(touches_required, bool) else True
    return required and itype != "investigate"


def _territory_blocks(label_defects, itype):
    """Would a missing declaration be the thing STOPPING this launch?

    The runner's touches park (actions._needs_touches) gates on issues.eligible(), so it fires only
    for an issue that is otherwise launchable. An issue already refused over its type or a control
    label never reaches the park — its metadata defect is still REPORTED (the owner wants the whole
    picture in one pass), just not claimed as the cause."""
    return itype in MERGE_PRODUCING_TYPES and not blocking(label_defects)


def lint_issue(gh_issue, areas=None, touches_required=True):
    """lint() over a raw gh issue dict. A non-dict (None, a list, a string from a broken gh call)
    degrades to an empty issue — which lints as invalid, never as valid, and never raises."""
    if not isinstance(gh_issue, dict):
        gh_issue = {}
    return lint(gh_issue.get("labels"), gh_issue.get("body"),
                areas=areas, touches_required=touches_required)


_METADATA_HEADING = "## Loop metadata"


def with_touches(body, value):
    """`body` rewritten so the runner CAN parse a `touches: <value>` declaration, or None when
    `value` is empty (a blank declaration parses to nothing, so writing one would "repair" an issue
    into the same defect while reporting success).

    This is what the janitor EXECUTES on the owner's one-tap approval, against someone else's issue,
    so it ADDS ONLY. It never deletes a line and never rewrites an author's prose — a repair that
    eats words is worse than the defect it fixes. Three cases, all additive:

      1. a `## Loop metadata` section already exists -> the `touches:` line is inserted into it,
         keeping its other fields (`parent:`, `blocked-by:`) exactly where they are;
      2. no section, and the body ENDS with an unparseable `touches:` line — the audit's commonest
         shape, because the template puts the declaration last — -> the heading is inserted on the
         line above it. A pure one-line insert; nothing moves and nothing is duplicated;
      3. anything else -> a fresh section is appended. A stray `touches:` line elsewhere stays
         where the author put it (now as prose), because deleting it is not this function's call."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    body = body if isinstance(body, str) else ""

    lines = body.splitlines()
    heading_at = next((i for i, ln in enumerate(lines)
                       if _H2_HEADING.match(ln) and ln.split("##", 1)[1].strip().lower()
                       == "loop metadata"), None)
    if heading_at is not None:
        if issues_mod.parse_loop_metadata(body)["touches"]:
            return body                                  # already parseable: nothing to add
        lines.insert(heading_at + 1, "touches: %s" % value)
        return "\n".join(lines) + ("\n" if body.endswith("\n") else "")

    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if last is not None and _TOUCHES_LINE.match(lines[last]):
        lines.insert(last, _METADATA_HEADING)
        return "\n".join(lines) + ("\n" if body.endswith("\n") else "")

    tail = "%s\ntouches: %s\n" % (_METADATA_HEADING, value)
    if not body:
        return tail
    return body + ("\n" if body.endswith("\n") else "\n\n") + tail


def describe(defect):
    """One defect as ONE line — what is wrong, then the fix. Every surface that prints a defect
    prints THIS, so the doctor, the journal and the deny cannot drift into three phrasings of the
    same complaint. Newlines inside a fix (the metadata shape) collapse to ' '."""
    text = "%s — %s" % (defect.get("what", ""), defect.get("fix", ""))
    return " ".join(text.split())


def blocking(defects):
    """Would the RUNNER refuse to launch over any of these? (An empty list is not blocking.)"""
    return any(d.get("blocks_launch") for d in defects)


def signature(defects):
    """A stable identity for a defect SET — the codes, sorted, joined.

    The runner journals a refusal ONCE per issue: this is what makes "the same complaint"
    recognizable across ticks, and a CHANGED complaint (a fix that traded one defect for another)
    speak up again instead of going quiet on a still-broken issue."""
    return ",".join(sorted(d.get("code", "") for d in defects))
