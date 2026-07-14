"""Content lint for the adopt skill — the plugin's fresh-machine bootstrap (issue #85).

Issue #85 (child of the #65 plugin restructure, design §6.3 / D9) authors the plugin's
``adopt`` skill: a self-contained bootstrap that takes a fresh machine from a bare clone to a
running loop (clone -> gated ``./bin/install.sh`` -> ``superlooper doctor --stack`` ->
``adopt``/``doctor``/``run``), and routes to the PUBLISHED full adoption contract at the stable
path ``~/.claude/skills/superlooper/docs/ADOPTING.md``. That path is stable because #85 also
moves ADOPTING.md into the gated payload (``skill/docs/ADOPTING.md``), so it lands there on any
machine where the CLI this skill wraps exists at all.

These tests pin the DoD facts so they cannot silently regress:

  * the skill exists with ``name: adopt`` frontmatter and a real description;
  * the bootstrap is SELF-CONTAINED — it needs no file read outside the plugin before the
    engine is installed: the full contract it routes to is the POST-INSTALL published path,
    never a source-repo path a bare clone would have to open first;
  * the bootstrap steps read clone -> ./bin/install.sh -> doctor --stack -> adopt -> doctor ->
    run, in that order, so following them verbatim reaches a running loop;
  * it explains WHY the gated installer pauses for an OK — the engine-diff publish gate (a
    merged engine change is inert until a human approves the diff at republish);
  * the plugin skill dir ships nothing executable (design D2/D5 — pure content).

Run from skills/superlooper:  python -m pytest tests/test_adopt_skill.py
"""
import os
import re
import stat
from pathlib import Path

# tests/ -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_SKILL_DIR = _REPO / "plugin" / "skills" / "adopt"
SKILL_MD = _SKILL_DIR / "SKILL.md"

# The published contract's stable path — where #85's payload move lands ADOPTING.md on any
# machine where the CLI this skill wraps exists at all.
_PUBLISHED_DOC = "~/.claude/skills/superlooper/docs/ADOPTING.md"
# The source-repo payload path ADOPTING.md now lives at. A bootstrap that names THIS is NOT
# self-contained — a bare clone would have to open a repo file before the engine is installed.
_REPO_PAYLOAD_DOC = "skill/docs/ADOPTING.md"
# The old repo path's tail. The published path CONTAINS it, so a bare match is not enough — a
# legitimate occurrence must be prefixed by the installed-home ``~/.claude/`` (see the guard).
_OLD_DOC_TAIL = "skills/superlooper/docs/ADOPTING.md"


def read(path):
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- structure

def test_skill_md_exists_with_frontmatter():
    text = read(SKILL_MD)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with YAML frontmatter"
    front = m.group(1)
    assert re.search(r"^name:\s*adopt\s*$", front, re.MULTILINE), "frontmatter name must be `adopt`"
    assert re.search(r"^description:\s*\S", front, re.MULTILINE), "frontmatter needs a description"


# ---------------------------------------------------------------- the bootstrap order

def test_bootstrap_orders_clone_install_stackdoctor_then_the_trio():
    text = read(SKILL_MD)
    steps = [
        # anchor on `git clone` (body step 1 only), NOT a bare "clone" — the frontmatter
        # description also says "clone", and matching that would leave the body clone step's
        # position effectively unpinned.
        ("git clone", text.find("git clone")),
        ("install.sh", text.find("install.sh")),
        ("doctor --stack", text.find("doctor --stack")),
        ("superlooper adopt", text.find("superlooper adopt")),
        # the repo doctor step; the stack step is `superlooper doctor --stack --repo`, which does
        # NOT contain this contiguous substring, so this isolates the second doctor invocation.
        ("superlooper doctor --repo", text.find("superlooper doctor --repo")),
        ("superlooper run", text.find("superlooper run")),
    ]
    for name, idx in steps:
        assert idx != -1, f"bootstrap must include {name!r}"
    order = [idx for _, idx in steps]
    assert order == sorted(order), (
        "bootstrap must read clone -> ./bin/install.sh -> doctor --stack -> adopt -> "
        f"doctor --repo -> run; got { {n: i for n, i in steps} }"
    )


# ---------------------------------------------------------------- self-containment

def test_routes_to_the_published_contract_path():
    text = read(SKILL_MD)
    assert _PUBLISHED_DOC in text, (
        f"the skill must route to the PUBLISHED full contract at {_PUBLISHED_DOC}"
    )


def test_bootstrap_is_self_contained_no_prerepo_doc_read():
    # DoD: the bootstrap needs no file read outside the plugin before the engine is installed.
    # So the full contract it routes to must be the POST-INSTALL published path — never the
    # source-repo payload path, and never a bare source-repo pointer a fresh clone would open.
    text = read(SKILL_MD)
    assert _REPO_PAYLOAD_DOC not in text, (
        f"the skill must not send a fresh machine to the source-repo doc {_REPO_PAYLOAD_DOC!r} "
        "before install — route to the published path instead (self-containment)"
    )
    # Every `skills/superlooper/docs/ADOPTING.md` occurrence must be the published `~/.claude/...`
    # path, not a bare source-repo pointer.
    idx = 0
    while True:
        i = text.find(_OLD_DOC_TAIL, idx)
        if i == -1:
            break
        assert text[:i].endswith("~/.claude/"), (
            "an ADOPTING.md pointer must be the published `~/.claude/...` path, not a source-repo "
            f"path (context: ...{text[max(0, i - 15):i + len(_OLD_DOC_TAIL)]}...)"
        )
        idx = i + len(_OLD_DOC_TAIL)


# ---------------------------------------------------------------- the install gate rationale

def test_explains_the_engine_diff_install_gate():
    # DoD/Goal: the bootstrap must explain WHY `./bin/install.sh` pauses for an OK — the
    # engine-diff publish gate: a merged engine change is INERT until a human approves the DIFF
    # the installer shows at republish. Pin the load-bearing terms so the rationale can't be
    # quietly reduced to a bare "run install.sh".
    text = read(SKILL_MD)
    lowered = text.lower()
    assert "install.sh" in text
    assert "inert" in lowered, "must say a merged engine change stays inert until republished"
    assert "diff" in lowered, "must say the installer shows the engine diff"
    assert "explicit ok" in lowered, "must say the installer asks for an explicit OK before publishing"


# ---------------------------------------------------------------- the plugin stays inert

_BANNED_DIRS = {"hooks", "bin", "monitors", "agents", "commands"}


def test_plugin_skill_dir_ships_nothing_executable():
    # design D2/D5: pure-content payload — no component dir, no executable bit. Mirrors the
    # per-skill guards in test_sl_debugger_skill.py / test_cross_review_skill.py.
    assert _SKILL_DIR.is_dir(), "the adopt skill dir must exist in the plugin"
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    for dirpath, dirnames, filenames in os.walk(_SKILL_DIR):
        for d in dirnames:
            assert d not in _BANNED_DIRS, (
                f"adopt skill must not ship a `{d}/` component dir (design D2/D5): {dirpath}"
            )
        for f in filenames:
            p = Path(dirpath) / f
            assert not (p.stat().st_mode & exec_bits), (
                f"adopt skill file must not carry an executable bit (design D2/D5): {p}"
            )
