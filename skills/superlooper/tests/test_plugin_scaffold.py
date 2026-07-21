"""Structural guards for issue #83: the marketplace + plugin scaffold and the ops-skill move.

Issue #83 (child 2 of the #65 plugin restructure) turns the repo into its own Claude Code
plugin marketplace with one pure-content plugin, and MOVES the superlooper ops skill's
SKILL.md + two references out of the gated engine payload into that plugin. These tests pin
the DoD facts so they cannot silently regress:

  * the two manifests exist, parse as JSON, and carry exactly the design's keys — a relative
    source ``./plugin`` (design D1) and NO ``version`` (SHA versioning, design D6), and no
    executable-component keys at all (design D2);
  * SKILL.md + approval-protocol.md + runner-ops.md live at their new plugin home and NO
    copy is left behind under ``skills/superlooper/skill/`` (design D3 — moved, never forked);
  * the rewritten router (design §6.1) points the issue-writing job at the sibling
    ``write-issue`` skill and the adoption job at the sibling ``adopt`` skill, while approval
    and ops stay this skill's own references;
  * the plugin payload is inert BY CONSTRUCTION — no ``hooks/``, ``bin/``, ``.mcp.json``,
    ``monitors/``, ``agents/``, ``settings.json`` and no executable file bits anywhere under
    ``plugin/`` (design D2, the issue's Boundaries). The permanent CI fence is a later child
    (§10.7); this is the scaffold child pinning its own deliverable;
  * no engine file still names the moved doc by its old skill-relative path
    ``references/runner-ops.md`` — every pointer was repointed to the plugin home.
"""
import json
import os
import stat
from pathlib import Path

# tests/test_plugin_scaffold.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_MARKETPLACE = _REPO / ".claude-plugin" / "marketplace.json"
_PLUGIN_JSON = _REPO / "plugin" / ".claude-plugin" / "plugin.json"
_PLUGIN_SKILL = _REPO / "plugin" / "skills" / "superlooper"
_ENGINE_SKILL = _REPO / "skills" / "superlooper" / "skill"

# The moved doc's new repo-relative home; engine prose must name it this way now.
_RUNNER_OPS_NEW = "plugin/skills/superlooper/references/runner-ops.md"
# The stale skill-relative form no engine file may keep after the move.
_RUNNER_OPS_OLD = "references/runner-ops.md"
# Legitimate longer paths that END in the stale form and must be stripped before looking for it.
# The mirror path (issue #199): the gated installer publishes the ops docs into the installed engine
# home keeping the plugin's directory shape, so the playbook's `../superlooper/references/…` sibling
# link still resolves there. That target is a DESTINATION inside the published tree, not a pointer
# back at a pre-move source, so it is not the drift this guard is about.
_RUNNER_OPS_MIRROR = "superlooper/references/runner-ops.md"
_RUNNER_OPS_LEGIT = (_RUNNER_OPS_NEW, _RUNNER_OPS_MIRROR)


# ---- manifests ---------------------------------------------------------------------------

def test_marketplace_manifest_parses_and_matches_design():
    assert _MARKETPLACE.exists(), f"marketplace manifest must exist at {_MARKETPLACE}"
    data = json.loads(_MARKETPLACE.read_text(encoding="utf-8"))
    assert data["name"] == "superlooper", "the marketplace is named superlooper"
    # owner.name is a required marketplace field.
    assert isinstance(data.get("owner"), dict) and data["owner"].get("name"), (
        "marketplace requires an owner with a name"
    )
    plugins = data["plugins"]
    assert isinstance(plugins, list) and len(plugins) == 1, "exactly one plugin entry"
    entry = plugins[0]
    assert entry["name"] == "superlooper"
    assert entry["source"] == "./plugin", "relative source must be ./plugin (design D1)"
    # SHA versioning (design D6): no version pin in the marketplace entry either — a version
    # set here pins the plugin exactly as one in plugin.json would.
    assert "version" not in entry, "no version field — SHA versioning (design D6)"


def test_plugin_manifest_parses_and_omits_versioned_and_executable_keys():
    assert _PLUGIN_JSON.exists(), f"plugin manifest must exist at {_PLUGIN_JSON}"
    data = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "superlooper"
    # DoD: deliberately NO version (D6), and none of the keys that would configure an
    # executable component (D2 — the payload is inert by construction).
    for banned in (
        "version",
        "hooks",
        "mcpServers",
        "lspServers",
        "agents",
        "commands",
        "experimental",
    ):
        assert banned not in data, f"plugin.json must not carry `{banned}` (design D2/D6)"


# ---- the move (design D3: moved, never forked) -------------------------------------------

def test_skill_and_references_moved_to_plugin():
    assert (_PLUGIN_SKILL / "SKILL.md").is_file(), "SKILL.md must live at the plugin home"
    assert (_PLUGIN_SKILL / "references" / "approval-protocol.md").is_file()
    assert (_PLUGIN_SKILL / "references" / "runner-ops.md").is_file()


def test_moved_skill_keeps_its_invocation_name():
    text = (_PLUGIN_SKILL / "SKILL.md").read_text(encoding="utf-8")
    # Frontmatter name stays `superlooper`, so the router is invoked as /superlooper:superlooper.
    assert "name: superlooper" in text


def test_no_copies_left_in_engine_payload():
    # Zero copies remain under the gated engine payload — one home, no double-load (design D3).
    assert not (_ENGINE_SKILL / "SKILL.md").exists(), "SKILL.md must not remain in the engine"
    assert not (_ENGINE_SKILL / "references" / "approval-protocol.md").exists()
    assert not (_ENGINE_SKILL / "references" / "runner-ops.md").exists()


# ---- router rewrite (design §6.1) --------------------------------------------------------

def test_router_routes_issue_writing_and_adoption_to_sibling_skills():
    text = (_PLUGIN_SKILL / "SKILL.md").read_text(encoding="utf-8")
    # issue-writing now routes to the write-issue sibling, NOT to a local reference file.
    assert "/superlooper:write-issue" in text, (
        "router must route issue-writing to the write-issue sibling skill"
    )
    assert "references/issue-writing.md" not in text, (
        "issue-writing.md is no longer this skill's reference — it routes to write-issue"
    )
    # adoption now routes to the adopt sibling.
    assert "/superlooper:adopt" in text, (
        "router must route adoption to the adopt sibling skill"
    )


def test_router_keeps_approval_and_ops_as_own_references():
    text = (_PLUGIN_SKILL / "SKILL.md").read_text(encoding="utf-8")
    # These two references travelled WITH the skill and remain its own (design §6.1).
    assert "references/approval-protocol.md" in text
    assert "references/runner-ops.md" in text


# ---- the inert-plugin boundary (design D2) -----------------------------------------------

_BANNED_DIRS = {"hooks", "bin", "monitors", "agents"}
_BANNED_FILES = {".mcp.json", ".lsp.json", "settings.json", "settings.local.json"}


def test_plugin_payload_has_no_executable_components():
    plugin_root = _REPO / "plugin"
    assert plugin_root.is_dir(), "the plugin payload must exist"
    for dirpath, dirnames, filenames in os.walk(plugin_root):
        for d in dirnames:
            assert d not in _BANNED_DIRS, (
                f"plugin must not contain a `{d}/` component dir (design D2): {dirpath}"
            )
        for f in filenames:
            assert f not in _BANNED_FILES, (
                f"plugin must not contain `{f}` (design D2): {dirpath}"
            )


def test_plugin_payload_has_no_executable_file_bits():
    plugin_root = _REPO / "plugin"
    assert plugin_root.is_dir(), "the plugin payload must exist"
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    for dirpath, _dirnames, filenames in os.walk(plugin_root):
        for f in filenames:
            p = Path(dirpath) / f
            assert not (p.stat().st_mode & exec_bits), (
                f"plugin file must not carry an executable bit (design D2): {p}"
            )


# ---- engine prose pointers repointed to the plugin home ----------------------------------

def test_no_engine_file_names_the_moved_doc_by_its_old_relative_path():
    """After the move, ``runner-ops.md`` lives in the plugin. Every engine file that used to
    name it as the skill-relative ``references/runner-ops.md`` must now name the plugin home.
    The new full path CONTAINS the old string as a tail, so strip the legitimate full paths first
    (the plugin home, and the issue-#199 publish mirror that keeps the plugin's shape so the
    playbook's sibling link resolves on an installed machine), then any surviving bare
    ``references/runner-ops.md`` is a stale pointer."""
    roots = [_ENGINE_SKILL, _REPO / "skills" / "superlooper" / "tests"]
    offenders = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for f in filenames:
                p = Path(dirpath) / f
                # This guard file necessarily spells out both path forms; skip itself.
                if p.name == Path(__file__).name:
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                stripped = text
                for legit in _RUNNER_OPS_LEGIT:
                    stripped = stripped.replace(legit, "")
                if _RUNNER_OPS_OLD in stripped:
                    offenders.append(str(p.relative_to(_REPO)))
    assert not offenders, (
        "these engine files still name the moved doc by its old skill-relative path "
        f"(repoint to {_RUNNER_OPS_NEW}): {sorted(offenders)}"
    )
