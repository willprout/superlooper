"""Content lint for the sl-debugger skill, now living in the plugin (issue #87).

Issue #64 authored the skill under ``skills/sl-debugger/skill/`` with these content-lint tests
in a sibling ``tests/`` dir that the CI ``tests`` check never ran (CI only invokes pytest from
``skills/superlooper`` and ``dashboard`` — see .github/workflows). Issue #87 (child of the #65
plugin restructure, design §6.5) ``git mv``s the skill payload into
``plugin/skills/sl-debugger/`` and relocates this suite INTO the engine suite so the ``tests``
check actually runs it. Nothing executable lands under ``plugin/`` — this is content lint only,
and it lives here, in ``skills/superlooper/tests/``, exactly like ``test_cross_review_skill.py``.

These tests pin the properties #64's DoD makes load-bearing (router structure, every routed
reference resolves, safety rails greppable, the unattended-invocation contract's authority
tiers, documented-incident coverage, the read-only health readout) PLUS the #87 DoD facts (the
payload moved, the runner-ops cross-reference resolves inside the plugin tree, the engine-home
reference stays accurate, the README is a supersession tombstone, and the plugin skill dir
carries nothing executable).

Run from skills/superlooper:  python -m pytest tests/test_sl_debugger_skill.py
"""

import os
import re
import stat
from pathlib import Path

# tests/ -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_SKILL_DIR = _REPO / "plugin" / "skills" / "sl-debugger"
SKILL_MD = _SKILL_DIR / "SKILL.md"
REFERENCES = _SKILL_DIR / "references"
README = _REPO / "skills" / "sl-debugger" / "README.md"
OLD_HOME = _REPO / "skills" / "sl-debugger" / "skill"
# runner-ops lives in the SIBLING superlooper skill inside the same plugin (moved there by #83).
RUNNER_OPS = _REPO / "plugin" / "skills" / "superlooper" / "references" / "runner-ops.md"
# The reliability ledger — a dated record (doc-lint excludes it), pinned here only for the D13
# fix pointer this issue (#209) owes it.
LEDGER = _REPO / "skills" / "superlooper" / "docs" / "RELIABILITY-LEDGER.md"


def read(path):
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- structure

def test_skill_md_exists_with_frontmatter():
    text = read(SKILL_MD)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with YAML frontmatter"
    front = m.group(1)
    assert re.search(r"^name:\s*sl-debugger\s*$", front, re.MULTILINE)
    assert re.search(r"^description:\s*\S", front, re.MULTILINE)


def test_every_routed_reference_exists_and_no_orphans():
    text = read(SKILL_MD)
    # Match the skill's OWN references (`references/foo.md`), NOT the cross-skill pointer into
    # the sibling superlooper skill (`../superlooper/...`) — the negative lookbehind for `/`
    # excludes any `references/` that is itself preceded by a path segment (the sibling's dir).
    routed = set(re.findall(r"(?<!/)references/([a-z0-9-]+\.md)", text))
    assert routed, "SKILL.md router must point at reference files"
    on_disk = {p.name for p in REFERENCES.glob("*.md")}
    assert routed == on_disk, (
        f"router/reference drift: routed-but-missing={routed - on_disk}, "
        f"orphaned-on-disk={on_disk - routed}"
    )


def test_router_declares_on_demand_loading():
    text = read(SKILL_MD).lower()
    assert "on demand" in text or "on-demand" in text, (
        "SKILL.md must state the open-only-what-you-need discipline"
    )


# ---------------------------------------------------------------- safety rails

def test_safety_rails_stated_in_skill_md():
    text = read(SKILL_MD)
    lowered = text.lower()
    for phrase in (
        "agent-ready",          # never applied by this skill
        "force-push",           # never
        ".superlooper/",        # never edited
        "frozen issue text",    # never edited
        ".github/workflows/",   # never touched
    ):
        assert phrase.lower() in lowered, f"SKILL.md must name the rail: {phrase}"
    # never kill by name/pattern — accept either wording, must sit near a "never"
    assert re.search(r"never[^.\n]*(pkill|by name|by pattern|name/pattern)",
                     lowered), "SKILL.md must forbid killing processes by name/pattern"
    # state surgery gated on the human's explicit go, and journaled
    assert re.search(r"explicit(ly)?\s+(go|approv|confirm)", lowered), (
        "SKILL.md must gate state surgery on the human's explicit go"
    )
    assert "journal" in lowered, "SKILL.md must require journaling state surgery"


def test_repair_ladder_orders_safest_first():
    ladder = read(REFERENCES / "repair-ladder.md").lower()
    ro = ladder.find("read-only")
    rev = ladder.find("reversible")
    surgery = ladder.find("surgery")
    assert 0 <= ro < rev < surgery, (
        "repair ladder must present read-only forensics, then reversible steps, "
        "then owner-confirmed state surgery, in that order"
    )


# ------------------------------------------------------- unattended contract

def test_unattended_contract_authority_tiers():
    text = read(REFERENCES / "unattended-contract.md")
    lowered = text.lower()
    for tier in ("diagnose-only", "allowlist", "full"):
        assert tier in lowered, f"missing authority tier: {tier}"
    assert re.search(r"default[^.\n]*full", lowered), "default tier must be full"
    # even `full` excludes the constitution absolutely
    for phrase in ("agent-ready", "force-push", "frozen issue text",
                   ".superlooper/", ".github/workflows/"):
        assert phrase.lower() in lowered, f"unattended exclusions must name: {phrase}"
    assert re.search(r"never[^.\n]*(pkill|by name|by pattern|name/pattern)", lowered)


def test_unattended_contract_episode_discipline():
    lowered = read(REFERENCES / "unattended-contract.md").lower()
    assert re.search(r"once[- ]per[- ]incident", lowered), "must act once per incident"
    assert "journal" in lowered, "every action journaled"
    assert "memo" in lowered, "must end with a plain-language memo"
    assert "notify" in lowered, "must end with a notify"


# ---------------------------------------------------------------- #87: the move

def test_no_skill_payload_remains_under_old_home():
    # #87 DoD: `git mv` — the payload moves, it does not get copied. Nothing that was the
    # publishable skill (SKILL.md or any reference) may remain under the pre-move home.
    assert not (OLD_HOME / "SKILL.md").exists(), (
        f"SKILL.md must not remain under the old home: {OLD_HOME}"
    )
    if OLD_HOME.exists():
        stragglers = [p for p in OLD_HOME.rglob("*.md")]
        assert not stragglers, f"no skill markdown may remain under {OLD_HOME}: {stragglers}"


def test_runner_ops_crossref_resolves_within_the_plugin_tree():
    # #87 DoD: the runner-ops cross-reference is repointed from the engine's installed
    # references dir to the plugin-internal SIBLING skill. Both skills ship as siblings under
    # the plugin's skills/ dir, so a relative `../superlooper/...` pointer resolves inside the
    # installed plugin cache wherever its root lands. We EXTRACT the pointer from SKILL.md and
    # resolve it (rather than hard-coding the literal): that pins the WRITTEN path lands on the
    # real doc, and keeps this file free of the bare skill-relative form that
    # test_plugin_scaffold.py's stale-pointer scan flags.
    text = read(SKILL_MD)
    m = re.search(r"\.\./superlooper/[\w./-]*runner-ops\.md", text)
    assert m, "SKILL.md must point runner-ops at the plugin sibling (a ../superlooper/... path)"
    resolved = (SKILL_MD.parent / m.group(0)).resolve()
    assert resolved == RUNNER_OPS.resolve(), (
        "the sibling pointer must resolve to the plugin's runner-ops doc"
    )
    assert RUNNER_OPS.is_file(), f"the plugin runner-ops reference must exist at {RUNNER_OPS}"
    # The stale engine-installed-references pointer must be gone. Note this is the `.../references/`
    # form; the bare engine-home `~/.claude/skills/superlooper/` (no trailing references dir) stays.
    assert "~/.claude/skills/superlooper/references/" not in text, (
        "SKILL.md must not still point runner-ops at the engine's installed references dir "
        "(it moved into the plugin, and that dir goes away at the next gated republish)"
    )


def test_engine_home_reference_remains_accurate():
    # #87 DoD: the "where truth lives" text keeps `~/.claude/skills/superlooper/` as the engine's
    # installed home — still true after the restructure (D4: the engine home path never moves; it
    # just sheds SKILL.md + references at the next republish, becoming a pure engine home).
    text = read(SKILL_MD)
    # Collapse runs of whitespace so a line-wrapped phrase ("is what actually\nruns") still matches.
    flat = " ".join(text.lower().split())
    assert "~/.claude/skills/superlooper/" in text, (
        "SKILL.md must still name the engine's installed home ~/.claude/skills/superlooper/"
    )
    assert "actually runs" in flat, (
        "the engine-home reference must still say the installed copy is what actually runs"
    )


# ---------------------------------------------------------------- #87: supersession README

def test_readme_is_supersession_tombstone():
    # #87 DoD: the manual `cp -R` install is superseded by the plugin. The README no longer
    # documents a manual copy; it states the plugin supersedes it and preserves provenance.
    text = read(README)
    lowered = text.lower()
    # The manual copy-into-~/.claude line is gone (it is superseded, not documented).
    assert not re.search(
        r"(cp -R|rsync -a?)[^\n]*skills/sl-debugger/skill[^\n]*~/.claude/skills/sl-debugger",
        text,
    ), "README must no longer carry the manual cp -R install line (superseded by the plugin)"
    assert "plugin/skills/sl-debugger" in text, "README must point at the plugin home"
    assert "supersed" in lowered, "README must state the plugin supersedes the manual install"
    assert "#64" in text, "README must keep provenance (authored under issue #64)"


# ---------------------------------------------------------------- #87: plugin stays inert

_BANNED_DIRS = {"hooks", "bin", "monitors", "agents"}


def test_plugin_skill_dir_has_no_executable_component():
    # The plugin is a pure-content payload (design D2/D5): the sl-debugger skill dir must ship no
    # component dir that would execute and no file carrying an executable bit. Mirrors the guard
    # in test_cross_review_skill.py, scoped to this skill.
    assert _SKILL_DIR.is_dir(), "the sl-debugger skill dir must exist in the plugin"
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    for dirpath, dirnames, filenames in os.walk(_SKILL_DIR):
        for d in dirnames:
            assert d not in _BANNED_DIRS, (
                f"sl-debugger skill must not ship a `{d}/` component dir (design D5): {dirpath}"
            )
        for f in filenames:
            p = Path(dirpath) / f
            assert not (p.stat().st_mode & exec_bits), (
                f"sl-debugger skill file must not carry an executable bit (design D2/D5): {p}"
            )


# ------------------------------------------------------------ incident cover

def test_every_documented_incident_class_has_a_walkthrough():
    text = read(REFERENCES / "failure-classes.md")
    lowered = text.lower()
    # the four documented classes, by date and by signature vocabulary
    for date, markers in {
        "2026-07-07": ("tick_error", "heartbeat"),
        "2026-07-08": ("park", "notify", "rate"),
        "2026-07-09": ("territory", "regenerate"),
        "2026-07-10": ("investigation", "marker"),
    }.items():
        assert date in text, f"missing incident class dated {date}"
        for marker in markers:
            assert marker in lowered, f"incident {date}: missing signature term {marker!r}"


def test_walkthroughs_carry_signature_diagnosis_repair():
    text = read(REFERENCES / "failure-classes.md").lower()
    for section in ("signature", "diagnos", "repair"):
        assert section in text, f"failure classes must structure {section!r} per class"


# ------------------------------------------------------------ health readout

def test_health_readout_is_read_only_and_names_the_probes():
    text = read(REFERENCES / "health-readout.md")
    lowered = text.lower()
    assert "read-only" in lowered
    for probe in ("runner.heartbeat", "state/alert", "journal.jsonl",
                  "superlooper status", "superlooper doctor", "issues.json"):
        assert probe in lowered, f"health readout must cover: {probe}"


# --------------------------------------------------- #209: the supervised/unattended split (D13)

def test_skill_md_states_the_supervised_unattended_split():
    """#209 DoD: the rails must state the split, not the old absolute prohibition.

    William's 2026-07-16 D13 ruling: anything done with him present and directing is allowed,
    always; prohibitions like "never hand-merge" bind UNATTENDED contexts only. The rails must
    name both halves and route hand-merging by them, so a helper agent mid-incident does not
    guess.
    """
    lowered = read(SKILL_MD).lower()
    assert "supervised" in lowered, "the rails must name the supervised mode"
    assert "unattended" in lowered, "the rails must name the unattended mode"
    # the D13 ruling is cited so the split is anchored to its authority, not a passing phrase
    assert "d13" in lowered or "2026-07-16" in lowered, (
        "the rails must anchor the split to William's D13 ruling"
    )
    # unattended never hand-merges / self-merges — the strict half
    assert re.search(r"never\s+hand-merge", lowered), (
        "the rails must state that an unattended session never hand-merges"
    )
    # supervised follows his DIRECT INSTRUCTION, full stop — the released half
    assert re.search(r"direct[^.\n]*instruction", lowered), (
        "the rails must state that a supervised session follows William's direct instruction"
    )


def test_skill_md_states_installed_engine_is_read_only_for_every_session():
    """#209 binding amendment (William, 2026-07-16): the installed engine tree is read-only for
    EVERY session, including a supervised debugger. Engine bugs go to a GitHub issue on the
    source repo; an emergency fix reaches the machine only through the gated bin/install.sh
    republish — never an in-place edit (the cb161ef loss). Phrased repo-agnostically."""
    text = read(SKILL_MD)
    lowered = text.lower()
    assert "~/.claude/skills/superlooper" in text, (
        "the amendment must name the installed engine home"
    )
    assert re.search(r"read-only for every session", lowered), (
        "the installed engine must be declared read-only for every session"
    )
    assert "github issue" in lowered, (
        "an engine bug must be routed to a GitHub issue on the source repo"
    )
    assert "bin/install.sh" in text, (
        "an emergency fix must reach the machine through the gated bin/install.sh republish"
    )
    assert "cb161ef" in lowered, "the rationale (the cb161ef silent-loss incident) must be cited"
    # Repo-agnostic: no hardcoded repo slug or URL, so a fork points at its own fork.
    assert "willprout/superlooper" not in lowered, "must not hardcode the source repo slug"
    assert "github.com/" not in lowered, "must not hardcode a GitHub repo URL"


def test_repair_ladder_states_the_split_for_hand_merging():
    """#209 DoD: the playbook that repeated the old absolute prohibition ("never merge or close
    PRs by hand ... with the human present or not") is amended to the same split."""
    ladder = read(REFERENCES / "repair-ladder.md").lower()
    assert "supervised" in ladder and "unattended" in ladder, (
        "the ladder's 'what no rung touches' section must name both halves of the split"
    )
    assert "split" in ladder, "the ladder must route hand-merging by the split"
    assert re.search(r"never\s+hand-merge", ladder), (
        "unattended, no rung hand-merges — the strict half must still be stated"
    )
    # The old absolute wording that contradicted D13 — "never ... merge or close PRs by hand"
    # in the "present or not" breath — must be gone. Collapse whitespace so the line-wrapped
    # original ("never\nmerge or close PRs by hand") is caught, not missed on the newline.
    flat = " ".join(ladder.split())
    assert "merge or close prs by hand" not in flat, (
        "the old absolute hand-merge prohibition must not survive in the ladder — "
        "hand-merging now follows the split"
    )


def test_unattended_contract_names_itself_as_the_split_half():
    """#209: the unattended contract is correctly the STRICT half; it should say so, so an agent
    reading only this file knows a supervised session is governed elsewhere."""
    lowered = read(REFERENCES / "unattended-contract.md").lower()
    assert "split" in lowered, "the unattended contract must name the supervised/unattended split"
    assert "supervised" in lowered, (
        "it must point at the supervised half so the reader knows this is only one half"
    )


def test_d13_ledger_line_carries_a_fix_pointer_to_this_issue():
    """#209 DoD: the D13 ledger line gets a fix pointer to this issue's PR."""
    text = read(LEDGER)
    # isolate the D13 clause (bounded by the next defect marker) to pin the pointer to D13 itself
    m = re.search(r"\*\*D13\*\*(.+?)\*\*D14\*\*", text, re.DOTALL)
    assert m, "the ledger must still carry the D13 entry"
    d13 = m.group(1)
    assert "#209" in d13, "the D13 line must point at issue #209 as its fix"
    assert "split" in d13.lower() or "supervised" in d13.lower(), (
        "the fix pointer must name what #209 did — write the supervised/unattended split"
    )
