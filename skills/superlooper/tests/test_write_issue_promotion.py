"""Structural guards for issue #84: promote issue-writing into the plugin as write-issue.

Issue #84 (child 3 of the #65 plugin restructure) PROMOTES the issue-writing reference out of
the gated engine payload into the plugin as a first-class skill (design §6.2):

    skills/superlooper/skill/references/issue-writing.md  ->  plugin/skills/write-issue/SKILL.md

The move is content promotion only — add frontmatter, keep every rule and its incident-citing
**Why** line intact (the citations are the mechanism that stops rules being quietly dropped),
and repoint the two cross-references to approval-protocol.md at the sibling skill's home. These
tests pin the DoD facts so they cannot silently regress:

  * the promoted skill exists at its plugin home with valid frontmatter (``name: write-issue``
    and a description that triggers on drafting/filing loop issues);
  * NO copy is left behind at the old engine path (design D3 — moved, never forked);
  * the body preserves the mechanically-parsed body-format spec, the one-``type:`` contract, all
    six rules, and every incident-citing Why line — nothing quietly dropped;
  * the two references to the approval protocol (now a sibling skill's reference, not a local
    file) are repointed to the superlooper skill's home, leaving no broken bare pointer.
"""
import re
from pathlib import Path

import yaml

# tests/test_write_issue_promotion.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_WRITE_ISSUE = _REPO / "plugin" / "skills" / "write-issue" / "SKILL.md"
_OLD_REFERENCE = _REPO / "skills" / "superlooper" / "skill" / "references" / "issue-writing.md"

# The corrected cross-reference form: approval-protocol.md is now the sibling superlooper skill's
# reference, so write-issue names it by that skill rather than as a bare local filename.
_SIBLING_APPROVAL_REF = "the superlooper skill's `references/approval-protocol.md`"


def _read():
    assert _WRITE_ISSUE.is_file(), f"the promoted skill must exist at {_WRITE_ISSUE}"
    return _WRITE_ISSUE.read_text(encoding="utf-8")


def _frontmatter(text):
    """Return the parsed YAML frontmatter block (the fenced ``---`` header)."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with a `---` YAML frontmatter block"
    return yaml.safe_load(m.group(1))


# ---- the move (design D3: moved, never forked) -------------------------------------------

def test_promoted_skill_exists_at_plugin_home():
    assert _WRITE_ISSUE.is_file(), f"write-issue SKILL.md must live at {_WRITE_ISSUE}"


def test_no_copy_left_at_old_engine_path():
    assert not _OLD_REFERENCE.exists(), (
        "issue-writing.md must not remain under the gated engine payload — moved, never forked "
        f"(design D3): {_OLD_REFERENCE}"
    )


# ---- frontmatter (design §6.2) -----------------------------------------------------------

def test_frontmatter_names_the_skill_write_issue():
    fm = _frontmatter(_read())
    assert fm.get("name") == "write-issue", "frontmatter name must be write-issue"


def test_frontmatter_description_triggers_on_drafting_or_filing_issues():
    fm = _frontmatter(_read())
    desc = fm.get("description")
    assert isinstance(desc, str) and desc.strip(), "frontmatter needs a non-empty description"
    low = desc.lower()
    assert "issue" in low, "description must mention issues (its trigger surface)"
    assert any(w in low for w in ("draft", "fil", "writ")), (
        "description must trigger on drafting/filing/writing loop issues (design §6.2)"
    )


# ---- body-format spec preserved ----------------------------------------------------------

def test_body_format_spec_preserved():
    text = _read()
    # The four mechanically-parsed H2 section names the runner reads, verbatim.
    for heading in ("## Goal", "## Definition of done", "## Boundaries", "## Loop metadata"):
        assert heading in text, f"body-format spec must keep the `{heading}` section name"
    # The metadata keys and the one-`type:`-label contract.
    for token in ("touches:", "blocked-by", "parent:",
                  "type:build", "type:investigate", "type:diagnose-and-fix"):
        assert token in text, f"body-format/type spec must keep `{token}`"


# ---- all six rules preserved -------------------------------------------------------------

_RULE_HEADINGS = (
    "### Thin-issue doctrine: point, never assert",
    "### Definition of done: machine-checkable wherever possible",
    "### `blocked-by` is a smell — justify it in the Goal, or re-scope",
    "### Cross-PR promises become ISSUES, never code comments",
    "### Bright-line work always splits",
    "### Never edit an approved Goal or DoD",
)


def test_all_six_rules_preserved():
    text = _read()
    for heading in _RULE_HEADINGS:
        assert heading in text, f"a rule was quietly dropped: {heading!r}"


# ---- every incident-citing Why line preserved (the mechanism) ----------------------------

def test_every_why_citation_preserved():
    text = _read()
    # Six bold rule-Why lines + one italic format-Why line = seven citations. The Why lines are
    # what stop a rule being dropped without its motivating incident, so none may vanish.
    assert text.count("Why:") >= 7, "an incident-citing Why line was dropped"


# Each rule must keep ITS OWN incident citation — a floor count alone can't catch one rule's Why
# being silently reworded while the total stays >= 7. Bind every rule heading to a distinctive
# phrase from its motivating incident, and require both to sit in the same rule section.
_RULE_WHY_ANCHORS = {
    "### Thin-issue doctrine: point, never assert": "a repo-state assertion baked into a queued brief rotted",
    "### Definition of done: machine-checkable wherever possible": "the per-PR gate is mechanical",
    "### `blocked-by` is a smell — justify it in the Goal, or re-scope": "sub-1 parked",
    "### Cross-PR promises become ISSUES, never code comments": "single costliest systemic miss",
    "### Bright-line work always splits": "William-only decisions",
    "### Never edit an approved Goal or DoD": "launders an",
}


def test_each_rule_keeps_its_own_why_citation():
    text = _read()
    # Isolate the "## The rules" region so a `### ` split yields exactly the rule sections.
    start = text.index("## The rules")
    end = text.index("## Filing the issue", start)
    blocks = ("\n" + text[start:end]).split("\n### ")[1:]
    by_heading = {"### " + b.split("\n", 1)[0]: b for b in blocks}
    for heading, anchor in _RULE_WHY_ANCHORS.items():
        assert heading in by_heading, f"rule section missing: {heading!r}"
        section = by_heading[heading]
        assert "Why:" in section, f"rule lost its Why line: {heading!r}"
        assert anchor in section, (
            f"rule {heading!r} lost its own incident citation {anchor!r} "
            "(a Why line was reworded, not just kept in count)"
        )


def test_distinctive_incident_tokens_preserved():
    text = _read()
    # Load-bearing incident references the Why lines cite by name.
    assert text.count("run-20260701-1750") >= 2, "the run-20260701-1750 incident citations"
    assert "single costliest systemic miss" in text, "the cross-PR-promise incident citation"
    assert "launders an" in text, "the never-edit-approved-text incident citation"


# ---- cross-references repointed to the sibling skill -------------------------------------

def test_approval_protocol_references_repointed_to_sibling():
    text = _read()
    # approval-protocol.md is the sibling superlooper skill's reference now, not a local file.
    assert text.count("references/approval-protocol.md") == 2, (
        "both approval-protocol references must survive the move"
    )
    assert text.count(_SIBLING_APPROVAL_REF) == 2, (
        "both approval references must name the sibling superlooper skill's reference"
    )
    # No broken bare pointer to a local approval-protocol.md may remain.
    assert "(see `approval-protocol.md`)" not in text, "stale bare approval pointer"
    assert "(`approval-protocol.md`)" not in text, "stale bare approval pointer"
