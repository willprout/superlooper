"""Content lint for the sl-debugger skill.

The skill is prose, not code — these tests pin the properties issue #64's DoD
makes load-bearing: the router structure exists, every routed reference resolves,
the safety rails are stated verbatim-enough to grep, the unattended-invocation
contract carries the authority tiers #66 builds against, the manual install line
exists, and every documented incident class is covered by a walkthrough.

Run from skills/sl-debugger:  python -m pytest tests/
(These tests are deliberately OUTSIDE the engine suite — issue #64's boundary is
a new directory only; the engine suite proves no collateral by staying untouched.)
"""

import re
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_ROOT / "skill" / "SKILL.md"
REFERENCES = SKILL_ROOT / "skill" / "references"
README = SKILL_ROOT / "README.md"


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
    routed = set(re.findall(r"references/([a-z0-9-]+\.md)", text))
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


# ---------------------------------------------------------------- install

def test_manual_install_line_documented():
    text = read(README)
    assert re.search(
        r"(cp -R|rsync -a?)[^\n]*skills/sl-debugger/skill[^\n]*~/.claude/skills/sl-debugger",
        text,
    ), "README must carry the one-line manual install (copy, never symlink)"
    assert "symlink" in text.lower(), "README must state the never-symlink rule"


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
