"""Structural guards for issue #86: absorb the machine-local cross-review command into the
plugin as the ``cross-review`` skill, and repoint repo-doc invocation wording.

Issue #86 (child of the #65 plugin restructure, design §6.4 / decision D5) absorbs
``~/.claude/commands/cross-review.md`` into ``plugin/skills/cross-review/SKILL.md`` as a
pure-content skill. These tests pin the DoD facts so they cannot silently regress:

  * the skill exists at the plugin home with parseable frontmatter whose ``name`` is
    ``cross-review`` (so the manual invocation is ``/superlooper:cross-review``);
  * the absorbed body preserves the load-bearing content of the machine-local command — the
    four-part prompt assembly (orientation + spec context + artifact + review brief), the
    structured output format, the cost note, the honest-value calibration step, and the
    no-silent-fallback discipline;
  * the skill states the owner ruling of 2026-07-10 explicitly: on a Claude-only machine a
    fresh same-model subagent that wrote none of the code is an equally valid review path, so
    the fallback is declared, not left ambient (design §6.4);
  * the suggest-cross-review HOOK is NOT absorbed (decision D5) — the plugin stays inert, so
    the cross-review skill dir must carry no executable component (the whole-plugin executable
    fence lives in test_plugin_scaffold.py; this pins the new skill dir specifically);
  * ``skills/superlooper/CLAUDE.md`` and ``skills/superlooper/docs/STACK.md`` update the bare
    ``/cross-review`` slash-command wording to the namespaced ``/superlooper:cross-review``,
    each with a one-line transition note that the machine-local command may coexist until the
    owner retires it (owner decision O3).
"""
import os
import stat
from pathlib import Path

# tests/test_cross_review_skill.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "plugin" / "skills" / "cross-review" / "SKILL.md"
_CLAUDE_MD = _REPO / "skills" / "superlooper" / "CLAUDE.md"
_STACK_MD = _REPO / "skills" / "superlooper" / "docs" / "STACK.md"


def _frontmatter(text):
    """Return the YAML frontmatter block (between the first two ``---`` fences) as a string."""
    assert text.startswith("---\n"), "SKILL.md must open with a YAML frontmatter fence"
    end = text.index("\n---", 4)
    return text[4:end]


# ---- the absorbed skill exists at the plugin home ----------------------------------------

def test_cross_review_skill_exists_with_frontmatter():
    assert _SKILL.is_file(), f"cross-review skill must live at {_SKILL}"
    text = _SKILL.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    # A skill needs a name (drives the /superlooper:<name> invocation) and a description
    # (the router/auto-invocation trigger).
    assert "name: cross-review" in fm, "frontmatter name must be cross-review"
    assert "description:" in fm, "frontmatter must carry a description"


def test_skill_body_is_more_than_frontmatter():
    text = _SKILL.read_text(encoding="utf-8")
    body = text[text.index("\n---", 4) + 4 :]
    assert len(body.strip()) > 400, "the absorbed body must carry the real command content"


# ---- the absorbed body preserves load-bearing content ------------------------------------

def test_preserves_four_part_prompt_assembly():
    text = _SKILL.read_text(encoding="utf-8").lower()
    # The four parts of the self-contained Codex prompt (machine-local command, step 2).
    assert "orientation" in text, "prompt assembly must include project orientation"
    assert "spec context" in text or "related spec" in text, "must include spec context"
    assert "artifact" in text, "must include the artifact itself"
    assert "review brief" in text, "must include the review brief"


def test_preserves_structured_output_format():
    text = _SKILL.read_text(encoding="utf-8")
    # The verdict enum + the calibration heading are the load-bearing shape of the review.
    assert "APPROVED" in text and "NEEDS REVISION" in text, "verdict enum must survive"
    assert "Critical issues" in text, "output format must keep the blocking-issues section"
    assert "look right" in text, (
        "output format must keep the 'things that look right' calibration section"
    )


def test_preserves_cost_note():
    text = _SKILL.read_text(encoding="utf-8").lower()
    assert "cost" in text, "the cost note must survive the absorption"
    # Pin the note's substance, not just the word "cost": the round-trip-per-invocation cost
    # against the subscription is the reason the note exists.
    assert "round-trip" in text or "subscription" in text, (
        "the cost note must keep its round-trip-vs-subscription substance"
    )


def test_preserves_honest_value_calibration_step():
    text = _SKILL.read_text(encoding="utf-8").lower()
    # The calibration loop: after applying fixes, honestly assess catch value vs review tax.
    assert "calibration" in text, "the honest-value calibration step must survive"
    assert "review tax" in text, "calibration must keep the value-vs-review-tax framing"


def test_preserves_no_silent_fallback_discipline():
    text = _SKILL.read_text(encoding="utf-8").lower()
    # The machine-local command's discipline: never silently degrade a review path.
    assert "silently" in text, "the no-silent-fallback discipline must survive"


# ---- the owner ruling of 2026-07-10 is stated explicitly ---------------------------------

def test_states_fresh_subagent_fallback_ruling():
    text = _SKILL.read_text(encoding="utf-8")
    lower = text.lower()
    # The adaptation for the loop (design §6.4): a fresh same-model subagent that wrote none of
    # the code is an equally valid review path on a Claude-only machine — stated, not ambient.
    assert "subagent" in lower, "the fresh-subagent fallback must be named"
    assert "2026-07-10" in text, "the fallback must cite the owner ruling of 2026-07-10"
    assert "claude-only" in lower or "claude only" in lower, (
        "the ruling applies to a Claude-only machine"
    )


# ---- the plugin stays inert: the new skill dir carries nothing executable (D5) -----------

_BANNED_DIRS = {"hooks", "bin", "monitors", "agents"}


def test_cross_review_skill_dir_has_no_executable_component():
    skill_dir = _SKILL.parent
    assert skill_dir.is_dir(), "the cross-review skill dir must exist"
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        for d in dirnames:
            assert d not in _BANNED_DIRS, (
                f"cross-review skill must not ship a `{d}/` component dir (design D5): {dirpath}"
            )
        for f in filenames:
            p = Path(dirpath) / f
            assert not (p.stat().st_mode & exec_bits), (
                f"cross-review skill file must not carry an executable bit (design D2/D5): {p}"
            )


# ---- repo docs repoint /cross-review -> /superlooper:cross-review with a transition note -

def test_claude_md_uses_namespaced_invocation_with_transition_note():
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    assert "/superlooper:cross-review" in text, (
        "CLAUDE.md must name the namespaced /superlooper:cross-review invocation"
    )
    # The bare slash-command form is retired (the noun 'a cross-review' may still appear;
    # '/superlooper:cross-review' does not contain the substring '/cross-review').
    assert "/cross-review" not in text, (
        "CLAUDE.md must not keep the bare /cross-review slash-command wording"
    )
    lower = text.lower()
    assert "machine-local" in lower and ("coexist" in lower or "retire" in lower), (
        "CLAUDE.md must carry the one-line transition note (owner decision O3)"
    )


def test_stack_md_uses_namespaced_invocation_with_transition_note():
    text = _STACK_MD.read_text(encoding="utf-8")
    assert "/superlooper:cross-review" in text, (
        "STACK.md must name the namespaced /superlooper:cross-review invocation"
    )
    assert "/cross-review" not in text, (
        "STACK.md must not keep the bare /cross-review slash-command wording"
    )
    lower = text.lower()
    assert "machine-local" in lower and ("coexist" in lower or "retire" in lower), (
        "STACK.md must carry the one-line transition note (owner decision O3)"
    )
