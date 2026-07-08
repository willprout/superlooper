"""Issue metadata parser + queue ordering (§C.2). Pure functions over gh's issue JSON — no I/O,
so the whole queue policy is unit-testable without GitHub.

The three moving parts:
  parse_issue  — a gh issue dict -> a normalized parsed issue (type, touches, blocked_by, ...)
  eligible     — is a parsed issue launchable right now (approved, valid, deps closed)?
  sort_key     — William's priority order as a tuple: expedite > priority band > requeue-front
                 (a conflict-regenerated issue) > oldest-first.
"""
import re

# The three issue kinds (exactly one type:* label per issue). Anything else -> "invalid",
# which eligible() refuses to launch (a mislabeled issue never runs; it waits for William).
VALID_TYPES = ("build", "investigate", "diagnose-and-fix")

_H2 = re.compile(r"^##\s+(.*\S)\s*$")


def parse_sections(body):
    """Split an issue/PR body into {H2-heading: section-text}. A section runs from its `## X`
    line to the next `## ` (or EOF). `###`+ subheadings are NOT section boundaries. A non-string
    body (missing, or a wrong-typed gh field) is treated as empty — never allowed to raise."""
    if not isinstance(body, str):
        body = ""
    sections = {}
    current = None
    buf = []
    for line in body.splitlines():
        m = _H2.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def parse_loop_metadata(body):
    """Extract the `## Loop metadata` block's fields, tolerant of a missing block or missing
    fields. Returns {"touches": [...], "blocked_by": [ints], "parent": int|None}. Reads only
    inside the Loop metadata section so a `touches:`-like phrase in Goal prose can't leak in."""
    meta_text = ""
    for heading, text in parse_sections(body).items():
        if heading.strip().lower() == "loop metadata":
            meta_text = text
            break
    touches, blocked_by, parent = [], [], None
    for line in meta_text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("touches:"):
            vals = s.split(":", 1)[1]
            touches = [t.strip() for t in vals.split(",") if t.strip()]
        elif low.startswith("blocked-by:"):
            blocked_by = [int(n) for n in re.findall(r"#?(\d+)", s.split(":", 1)[1])]
        elif low.startswith("parent:"):
            mm = re.search(r"#?(\d+)", s.split(":", 1)[1])
            parent = int(mm.group(1)) if mm else None
    return {"touches": touches, "blocked_by": blocked_by, "parent": parent}


def _label_names(gh_issue):
    """gh --json labels returns [{"name": ...}]; some queries return bare strings. Handle both,
    and defend against every wrong-typed shape (non-list labels, non-string names, None) — a
    malformed label set must yield [] (an unapproved, invalid issue), never raise into a tick."""
    labels = gh_issue.get("labels")
    if not isinstance(labels, list):
        return []
    out = []
    for lab in labels:
        if isinstance(lab, dict):
            name = lab.get("name")
        elif isinstance(lab, str):
            name = lab
        else:
            name = None
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _single_control_label(labels, prefix):
    """Read an EXACTLY-ONE `<prefix><value>` control knob (William-applied, e.g. `model:` /
    `effort:`) out of a label set. Returns (value_or_None, conflict):
      0 labels                 -> (None,  False)  — no override (the default path)
      exactly 1, non-empty val -> (value, False)  — the override, PASS-THROUGH (no allowlist; an
                                                    unknown value fails the launch loudly + parks)
      exactly 1, empty/blank   -> (None,  True)   — a bare `model:` is malformed; fail CLOSED
                                                    (eligible() refuses) rather than silently using
                                                    the default (the fail-open-on-wrong-typed-input
                                                    defect class — Codex review 2026-07-07 #2).
      2+                       -> (None,  True)   — ambiguous; conflict flag makes eligible() refuse,
                                                    mirroring the exactly-one type:* rule (a
                                                    mislabeled issue waits for William).
    """
    vals = [name[len(prefix):] for name in labels if name.startswith(prefix)]
    if len(vals) == 1 and vals[0].strip():
        return vals[0], False
    return None, len(vals) > 0


def parse_issue(gh_issue):
    """Normalize a gh issue dict into the parsed shape the scheduler/gate/report build against.
    Defensive by design: a partial/odd/wrong-typed issue must NEVER raise into a tick — it parses
    to something eligible() will simply refuse (e.g. type "invalid"). One top-level guard coerces
    any non-dict input (None / list / str from a broken gh call) to {}, closing every top-level
    raise path at once; every field is then read with isinstance coercion."""
    if not isinstance(gh_issue, dict):
        gh_issue = {}
    num = gh_issue.get("number")
    labels = _label_names(gh_issue)

    # type: exactly one type:* label whose value is a known kind, else "invalid".
    type_vals = [name[len("type:"):] for name in labels if name.startswith("type:")]
    itype = type_vals[0] if (len(type_vals) == 1 and type_vals[0] in VALID_TYPES) else "invalid"

    if "priority:high" in labels:
        priority = 1
    elif "priority:low" in labels:
        priority = 3
    else:
        priority = 2

    # Per-issue control knobs (owner ruling 2026-07-07): William-applied `model:`/`effort:` labels
    # override the config/loader default for THIS issue's worker sessions only. The value flows to
    # SL_MODEL/SL_EFFORT and on to `claude --model/--effort` (start-session.sh). Exactly-one each;
    # 2+ is a conflict eligible() refuses, mirroring the exactly-one type:* rule.
    model, model_conflict = _single_control_label(labels, "model:")
    effort, effort_conflict = _single_control_label(labels, "effort:")

    meta = parse_loop_metadata(gh_issue.get("body", ""))
    title = gh_issue.get("title")
    created = gh_issue.get("createdAt")
    return {
        "num": num,
        "id": f"i{num}",
        "title": title if isinstance(title, str) else "",
        "type": itype,
        "labels": labels,
        "model": model,
        "effort": effort,
        "label_conflict": model_conflict or effort_conflict,
        "touches": meta["touches"],
        "blocked_by": meta["blocked_by"],
        "parent": meta["parent"],
        # isinstance coercion (not `or ""`): a TRUTHY non-string createdAt (42, {}) would slip
        # past `or ""` and then raise TypeError when sorted against string timestamps in py3.9.
        "created_at": created if isinstance(created, str) else "",
        "priority": priority,
        "expedite": "expedite" in labels,
    }


def eligible(parsed, closed_issue_nums, frozen):
    """Is this issue launchable NOW? Approved (`agent-ready`) AND a valid type AND every
    `blocked-by` issue is closed.

    `frozen` is accepted for interface symmetry with the scheduler and to make the constitutional
    rule EXPLICIT and testable: a frozen mainline stops MERGES, not builds (frozen-but-building is
    the safe idle state, §C.4), so freeze deliberately does NOT gate eligibility."""
    if "agent-ready" not in parsed["labels"]:
        return False
    if parsed["type"] not in VALID_TYPES:
        return False
    # A control-label conflict (2+ `model:*` or 2+ `effort:*`) is ambiguous — refuse to launch until
    # William fixes the labels, exactly as an invalid type is refused. `.get` keeps this backward-safe
    # if an older parsed dict (no `label_conflict` key) is ever passed in.
    if parsed.get("label_conflict"):
        return False
    return all(dep in closed_issue_nums for dep in parsed["blocked_by"])


def sort_key(parsed, requeue_front):
    """William's priority order as a sort tuple (ascending): expedite lane first, then the
    priority band (1 high .. 3 low), then a conflict-requeued issue ahead of its band, then
    oldest-first. `requeue_front` is the per-issue flag from loopstate (issues.json)."""
    return (not parsed["expedite"], parsed["priority"], not requeue_front, parsed["created_at"])
