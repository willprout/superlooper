"""The triage flight's BRAIN — what a verdict MEANS, and what a run writes down (issue #449).

``plugin/skills/superlooper/references/triage-standing-rule.md`` is the authority: William defined
that delegation himself, and per ``approval-protocol.md`` a standing rule he defines is the one
sanctioned non-conversational path to autonomous action. This module implements it. It must never
weaken, widen or restate it — drift between the code and the rule is a defect, and
``tests/test_triage_run.py`` pins the rubric against the document itself so drift goes red.

Three modules, three jobs, deliberately not one:

  * ``triage.py`` (#448) is the STATE CONTRACT and the trigger — where the flight's memory lives
    and when a flight is due. It decides nothing about any issue.
  * this module is the DECISION: what each verdict authorises, which acts the delegation's edges
    refuse outright, and the exact prose every close, ledger entry and sitting-sheet line carries.
  * the ``triage-act`` CLI verb is the only thing that SPENDS a decision. Everything the flight
    writes goes through it.

**Why the acts are verbs rather than instructions in the brief.** A brief can only ask; a verb can
refuse. The standing rule's limits — never touch an approved issue, never re-close a reopened one,
never close a nit without a ledger entry, never apply an approval label — are the kind of rule this
codebase does not teach in prose when it can enforce at the moment of the mistake (the #215/#225
precedents). So the brief points at the rule for JUDGEMENT and hands every WRITE to a verb that
holds the edges. A flight that tries to cross one is told exactly what it crossed, at the call.

Pure: no gh, no git, no disk, no clock. The impure halves — reading the issue, verifying a cited
commit against ``origin/main``, posting the comment — belong to the caller.
"""
import collections
import re

import triage

# --------------------------------------------------------------------------- the machine marker

# Every comment this delegation posts BEGINS with this. Two jobs, both load-bearing:
#
#   * `brief.py` skips any comment whose body starts with `<!-- superlooper-` when it renders a
#     worker's post-approval amendments. On these repos every comment shares one gh identity, so
#     without the marker a triage close comment on an issue the owner later approves would render
#     to that worker as "William's binding amendment" — the flight's own words promoted to the
#     owner's. The marker is what keeps a machine comment machine-readable.
#   * a human reading the issue can see at a glance which comments a flight wrote.
MARKER = "<!-- superlooper-triage -->"

# The ledger entry's own marker, per closed issue. It is what makes the nit close IDEMPOTENT: the
# entry is written first (so the close comment can link something that exists), and if the close
# then fails, the next flight must be able to see that this issue's entry is already filed rather
# than file a second one. Deliberately carries the SOURCE issue number, not the ledger's.
_LEDGER_MARKER = "<!-- superlooper-limitation issue=%d -->"


def ledger_marker(num):
    """The per-source-issue marker a ledger entry carries, so a re-run recognises its own entry."""
    return _LEDGER_MARKER % int(num)


# --------------------------------------------------------------------------- the nit rubric

RubricLine = collections.namedtuple("RubricLine", ["id", "title", "test"])

# The DEFAULT rubric, verbatim from the standing rule's own four lines. A per-repo config override
# (`triage.rubric`) REPLACES this set entirely — a repo whose "not worth a lane" is a different
# question gets to say so — but the default is the owner's, and it lives here rather than in the
# config so a repo that says nothing inherits the rule as written.
#
# `test_the_default_rubric_matches_the_standing_rule_document` reads the rule doc and pins every
# line against it, so an edit to one that is not an edit to the other goes red.
DEFAULT_RUBRIC = (
    RubricLine("N1", "Unreachable input",
               "the defect requires input no live code path can produce."),
    RubricLine("N2", "Cosmetic-only",
               "wording/spacing in a surface no decision or alert reads."),
    RubricLine("N3", "Cost exceeds consequence",
               "the fix's full lane cost exceeds the worst realized consequence, AND that "
               "consequence is loud when it happens (self-evidencing, not silent)."),
    RubricLine("N4", "Duplicate hardening",
               "a second guard for a class a merged class-killer already covers, adding no new "
               "detection."),
)

# The rule's hard floor UNDER the rubric: three shapes that are never a nit however the lines read,
# and which a per-repo override therefore cannot buy its way past. Rendered into the brief verbatim
# and pinned against the document, same as the lines above.
NEVER_A_NIT = (
    "anything silent-failure-shaped",
    "anything touching approvals, merging, or publishing",
    "anything the owner has personally flagged",
)


def _rubric_override(config):
    """The per-repo rubric as READ, or None when the repo says nothing usable.

    Tolerant on purpose, and only in this direction: the LOADER (`config.py`) is where a malformed
    override fails loudly, at adopt time, which is the one moment an owner can still fix it. Every
    runtime reader may be handed a half-read config, and the safe fallback is the owner's own
    default rubric — never an empty one, which would leave a flight unable to close any nit at all,
    and never a coerced one, which would let a garbage entry become a rubric line a close cites.
    """
    block = config.get(triage.CONFIG_KEY) if isinstance(config, dict) else None
    raw = block.get("rubric") if isinstance(block, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        vals = [item.get(k) for k in ("id", "title", "test")]
        if any(not (isinstance(v, str) and v.strip()) for v in vals):
            return None
        if vals[0].strip() in seen:
            return None                       # two lines with one id: a close could not name one
        seen.add(vals[0].strip())
        out.append(RubricLine(*(v.strip() for v in vals)))
    return tuple(out)


def rubric(config):
    """The nit rubric in force for this repo — the per-repo override, else the rule's own four."""
    return _rubric_override(config) or DEFAULT_RUBRIC


def rubric_line(config, line_id):
    """One rubric line by id, or None. None is a REFUSAL at the call site: a nit close names its
    line in the close comment AND in the ledger entry, so a line nobody can look up is a close
    whose reason cannot be checked."""
    if not isinstance(line_id, str):
        return None
    wanted = line_id.strip()
    return next((l for l in rubric(config) if l.id == wanted), None)


def render_rubric(config):
    """The rubric as the brief prints it — one bullet per line, plus the never-a-nit floor."""
    lines = ["- **%s %s** — %s" % (l.id, l.title, l.test) for l in rubric(config)]
    lines.append("")
    lines.append("**Never a nit, whatever the lines above say:** "
                 + "; ".join(NEVER_A_NIT) + ".")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the brief

def render_brief(template, mapping):
    """Literal ``{name}`` substitution — ``brief.py``'s ``_sub`` convention, and for its reason:
    never ``str.format``, which chokes on the prose braces, backticks and code fences a brief is
    made of, and which would happily substitute a brace that appeared inside an issue title
    somebody pasted into the pile.

    The mapping's VALUES are placed verbatim and are never re-scanned, so a ``{placeholder}``
    inside a quoted run log or an issue title stays literal — the same ordering discipline the
    worker brief keeps for the William-approved body.
    """
    out = template
    for k, v in (mapping or {}).items():
        out = out.replace("{" + k + "}", "" if v is None else str(v))
    return out


# --------------------------------------------------------------------------- verdicts as ACTS

# What a verdict AUTHORISES. The vocabulary is the rule's (`triage.py` owns its spellings); this is
# the mapping from a word to the one thing a flight may then do with it.
KEEP = "keep"            # record the verdict, say nothing on the issue (silence = kept)
ESCALATE = "escalate"    # never act — one line and a recommendation on the owner's sitting sheet
CLOSE = "close"          # close with a comment carrying the evidence (overtaken / nit)
ABSORB = "absorb"        # merge into another unapproved issue, then close the absorbed one

Verdict = collections.namedtuple("Verdict", ["name", "act", "param"])

_DUPLICATE_RE = re.compile(r"^duplicate-of-#([1-9][0-9]*)$")
# The rubric id is opaque to the parser — a repo may name its lines anything — but it may not be
# empty and it may not contain the closing paren that terminates it.
_NIT_RE = re.compile(r"^nit\(([^()]+)\)$")


def parse_verdict(text):
    """One of the rule's six verdicts as a Verdict(name, act, param), or None.

    None is a REFUSAL, never a default. A verdict this cannot read is a verdict no flight can be
    shown to have reached, and guessing an act for it is how an unrecognised word becomes a close.
    """
    if not isinstance(text, str):
        return None
    name = text.strip()
    if name == triage.BUILDABLE or name == triage.UNDERSPECIFIED:
        return Verdict(name, KEEP, None)
    if name == triage.CONTAINS_OWNER_DECISION:
        return Verdict(name, ESCALATE, None)
    if name == triage.OVERTAKEN:
        return Verdict(name, CLOSE, None)
    m = _DUPLICATE_RE.match(name)
    if m:
        return Verdict(name, ABSORB, int(m.group(1)))
    m = _NIT_RE.match(name)
    if m and m.group(1).strip():
        return Verdict(name, CLOSE, m.group(1).strip())
    return None


def is_close_verdict(text):
    """Did this verdict CLOSE the issue it was reached on? The reopen guard's question."""
    v = parse_verdict(text)
    return v is not None and v.act in (CLOSE, ABSORB)


# --------------------------------------------------------------------------- the guards

def held(issue):
    """Is this issue one the LOOP holds — approved, in flight, or awaiting the owner's answer?

    The rule: an approved issue's Goal, DoD, labels and state are never touched; lint may FLAG, and
    every action on one escalates instead. So this is the acting guard, and it is STRICT in the
    opposite direction from ``triage.changed()``: there, an unreadable label set counts as unheld,
    because the cost is one extra look. Here the cost is a write onto frozen owner text, so
    anything this cannot read counts as HELD. Never act into a fog.
    """
    if not isinstance(issue, dict):
        return True
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return True
    names = set()
    for entry in labels:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.add(name)
        elif isinstance(entry, str):
            names.add(entry)                 # the flat shape `gh issue list` returns in some views
        else:
            return True                      # an entry this cannot read -> treat the issue as held
    return any(h in names for h in triage.HELD_LABELS)


def reopen_protest(record, body):
    """Is this OPEN issue one the flight already closed, on the very body standing now?

    "A reopened issue is owner protest — the flight never re-closes it unless the body has since
    changed." The store is what makes that answerable with no GitHub archaeology: a CLOSING verdict
    recorded against this exact body, on an issue that is open again, can only mean somebody
    reopened it. The owner is the only one who could have.

    False on anything unreadable: a record we cannot interpret is not evidence that WE closed
    anything, and refusing every act on the strength of a corrupt entry would be a different bug.
    """
    if not isinstance(record, dict):
        return False
    if not is_close_verdict(record.get("verdict")):
        return False
    return record.get("body_hash") == triage.body_hash(body)


# The labels the rule denies the flight outright. `agent-ready` is William's word and nothing else
# may write it; `pre-authorized:*` is the same authority one seat over (#165). Matched as a family
# rather than by name so a future `pre-authorized:<something>` is denied the day it is registered,
# not the day somebody remembers to add it here.
_PRE_AUTHORIZED_PREFIX = "pre-authorized:"


def forbidden_label(name):
    """The reason this label may not be applied by a flight, or None when it may.

    The sentence is the point (deny-with-reason): a flight told "refused" learns nothing, and a
    flight told WHY stops trying. Applies to removal too — stripping `agent-ready` un-approves an
    issue, which is as much the owner's word as applying it.
    """
    if not isinstance(name, str):
        return "that is not a label name"
    n = name.strip()
    if n == "agent-ready":
        return ("`agent-ready` is the owner's word alone — a flight may never apply or remove it. "
                "If an issue should be approved, escalate it with a recommendation.")
    if n.startswith(_PRE_AUTHORIZED_PREFIX):
        return ("`%s` is an owner pre-authorization (#165) — a flight may never apply or remove "
                "one. Escalate with a recommendation instead." % n)
    return None


# --------------------------------------------------------------------------- the prose

def _lead(text):
    """Every comment a flight posts opens with the marker on its own line, then the prose."""
    return "%s\n%s" % (MARKER, text)


def duplicate_close_comment(absorber, why):
    """The comment the ABSORBED issue closes with: a cross-reference, the evidence, and where its
    content went. `why` is the flight's own sentence about what makes the two one issue — the half
    the owner reads if he disagrees, so it is carried rather than summarised away."""
    return _lead(
        "Closed as a **duplicate of #%d** by the triage flight, under the owner's standing rule "
        "(`triage-standing-rule.md`). This issue's content has been absorbed into #%d — nothing "
        "is lost; the work is tracked there.\n\n"
        "%s\n\n"
        "If this is not a duplicate, reopen it: a reopened issue is owner protest, and the flight "
        "will not close it again unless its body changes." % (absorber, absorber, why))


def absorber_comment(absorbed, title, why):
    """The comment the ABSORBER gains, so the merge reads from both ends."""
    return _lead(
        "Absorbed **#%d** (%s) into this issue as a duplicate — its content is appended to the "
        "body above.\n\n%s" % (absorbed, title or "no title", why))


def overtaken_close_comment(commit, why, dev_branch):
    """The comment an OVERTAKEN close carries. The commit is not decoration: the rule requires
    commit-level evidence on ``origin/<dev>``, and the verb verifies the sha is an ancestor of it
    before this text is ever written."""
    return _lead(
        "Closed as **overtaken** by the triage flight, under the owner's standing rule "
        "(`triage-standing-rule.md`).\n\n"
        "Evidence on `origin/%s`: commit `%s`.\n\n"
        "%s\n\n"
        "If this is still live, reopen it: a reopened issue is owner protest, and the flight will "
        "not close it again unless its body changes." % (dev_branch, commit, why))


def nit_close_comment(line, why, ledger_num, ledger_link):
    """The comment a NIT close carries: the rubric line, the reasoning, and the ledger entry.

    The link is the half that makes a nit close a FILING rather than a loss. It is written after
    the ledger entry exists, so it always points at something real.
    """
    return _lead(
        "Closed as a **nit** by the triage flight, under the owner's standing rule "
        "(`triage-standing-rule.md`) — true, but not worth a lane.\n\n"
        "Rubric line **%s %s**: %s\n\n"
        "%s\n\n"
        "Filed in the limitations ledger (#%d) rather than dropped: %s\n\n"
        "A ledger entry is reversible. If this limitation stops being acceptable, reopen this "
        "issue or open a fresh one — a reopened issue is owner protest, and the flight will not "
        "close it again unless its body changes."
        % (line.id, line.title, line.test, why, ledger_num, ledger_link))


def ledger_entry(line, limitation, source_num):
    """One ledger entry, in the format the ledger issue documents for itself (#450): the rubric
    line, the limitation, and a link back to the closed source issue."""
    return "%s\n- **[%s] %s** — %s — accepted from #%d (closed by the triage flight)" % (
        ledger_marker(source_num), line.id, line.title, limitation, source_num)


_ABSORB_HEADING = "## Absorbed: #%d"


def absorbed_body(absorber_body, absorbed_num, absorbed_title, absorbed_body_text):
    """The absorber's body with the absorbed issue's content appended under its own heading.

    IDEMPOTENT on the heading: a re-run (a close that failed after the body edit landed) must not
    append the same content twice. Appended rather than merged — a flight rewriting somebody's
    prose into somebody else's sections is exactly the judgement the delegation does not extend to.
    """
    base = absorber_body if isinstance(absorber_body, str) else ""
    heading = _ABSORB_HEADING % absorbed_num
    if heading in base:
        return base
    tail = "%s (%s)\n\n_Absorbed by the triage flight as a duplicate; #%d is closed._\n\n%s\n" % (
        heading, absorbed_title or "no title", absorbed_num,
        absorbed_body_text if isinstance(absorbed_body_text, str) else "")
    return base.rstrip("\n") + "\n\n" + tail


# --------------------------------------------------------------------------- the sitting sheet

SITTING_HEADING = "## The owner's sitting sheet"


def sitting_line(num, title, finding, recommendation):
    """ONE escalation, as the rule asks for it: one line and a recommendation.

    Kept to one line plus its recommendation deliberately. The sitting sheet is read in a sitting;
    an escalation that needs three paragraphs is one the flight has not finished thinking about,
    and the owner is the wrong person to finish it for free.
    """
    finding = (finding or "").strip().replace("\n", " ")
    rec = (recommendation or "").strip().replace("\n", " ")
    return "- **#%d** %s — %s\n  - **Recommend:** %s" % (
        num, (title or "").strip(), finding or "escalated with no finding recorded",
        rec or "no recommendation recorded — the flight owed one")


def sitting_sheet(lines):
    """The escalations composed into the run log's sitting sheet, or "" with nothing to escalate.

    Silent on empty by contract: a heading over nothing reads as "the owner has something to do"
    on a morning he does not.
    """
    kept = [l for l in (lines or []) if isinstance(l, str) and l.strip()]
    if not kept:
        return ""
    return ("%s\n\nEvery item below is a finding the flight was NOT authorised to act on. "
            "Prioritisation and what-to-build-next remain the owner's, always.\n\n%s\n"
            % (SITTING_HEADING, "\n".join(kept)))


# --------------------------------------------------------------------------- the run log

def run_log_line(act, num, detail):
    """One chronological line in the run's markdown log — appended as each act lands, so a flight
    that dies mid-run still leaves an honest record of what it had already done."""
    return "- `%s` **#%s** — %s" % (act, num, (detail or "").strip().replace("\n", " "))


def run_summary(counts):
    """The run's own tally, as the log closes with it and the report repeats it."""
    return ("judged %d · %d merged · %d closed (%d to the ledger) · %d fixed · %d escalated"
            % (counts.get("judged", 0), counts.get("merged", 0), counts.get("closed", 0),
               counts.get("ledger", 0), counts.get("fixed", 0), counts.get("escalated", 0)))


__all__ = ["MARKER", "ledger_marker", "RubricLine", "DEFAULT_RUBRIC", "NEVER_A_NIT",
           "rubric", "rubric_line", "render_rubric", "render_brief",
           "KEEP", "ESCALATE", "CLOSE", "ABSORB", "Verdict", "parse_verdict", "is_close_verdict",
           "held", "reopen_protest", "forbidden_label",
           "duplicate_close_comment", "absorber_comment", "overtaken_close_comment",
           "nit_close_comment", "ledger_entry", "absorbed_body",
           "SITTING_HEADING", "sitting_line", "sitting_sheet", "run_log_line", "run_summary"]
