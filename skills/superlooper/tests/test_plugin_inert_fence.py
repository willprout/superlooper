"""Permanent mechanical fence: the plugin payload stays inert forever (issue #88).

Design record: ``docs/DESIGN-2026-07-11-plugin-restructure.md``.

  * **Design D2 — nothing executable in the plugin, enforced mechanically.**
    Verbatim: "Plugin hooks/bin/MCP/monitors execute ungated and ride updates;
    ruling 3 therefore demands their absence, and a CI test makes the absence a
    property of main rather than a convention."

  * **The update-gate ruling (owner ruling 3 of 2026-07-10 — "updates keep the
    human gate").** Once a plugin is enabled, its hooks, MCP servers, ``bin/``
    executables, and monitors run automatically with no further human gate AND
    ride marketplace auto-updates. Only skill *content* (markdown) may travel on
    those update semantics; anything that executes must reach a machine solely
    through the diff-showing, OK-requiring gated ``bin/install.sh`` republish.
    The decisive consequence, stated in the design's §1.3: *nothing executable
    may ship in the plugin payload at all* — the gate must hold by construction,
    not by convention.

This is design §10.7, the fence "that keeps ``plugin/`` executable-free forever."
It is deliberately SELF-CONTAINED: it re-derives the whole inert invariant from
the repo tree, so it stands as the permanent ratchet independent of the #83
scaffold guard (``test_plugin_scaffold.py``), which pinned only that scaffold
child's own deliverable at scaffold time. Landing order relative to #83 does not
matter (issue #88): it is green on an already-inert main (the guards walk the real
tree and find nothing banned) and goes red the moment a banned component dir/file,
an inline executable-component key, an
executable file bit, or a non-content file type appears anywhere on the plugin
distribution surface (``plugin/`` plus the repo-root ``.claude-plugin/`` that
carries the marketplace manifest).

The ``test_fence_flags_*`` meta-tests construct each violation class in a temp
tree and assert the fence catches it, so this guard can never silently rot into a
vacuously-green test — the failure mode that makes structural guards worthless.
"""
import json
import os
import stat
from pathlib import Path

# tests/test_plugin_inert_fence.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MARKETPLACE_DIR = _REPO / ".claude-plugin"
_PLUGIN_JSON = _PLUGIN / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON = _MARKETPLACE_DIR / "marketplace.json"

# The two roots that make up the shipped plugin distribution surface: the payload
# itself and the repo-root marketplace manifest dir. The executable-bit and
# file-type guards sweep both (issue #88: "under `plugin/` or `.claude-plugin/`").
_SURFACE_ROOTS = (_PLUGIN, _MARKETPLACE_DIR)

# Component dirs whose mere presence would make the plugin execute ungated. Claude
# Code auto-loads each once the plugin is enabled — hooks/ (event handlers), bin/
# (executables), monitors/ (background watchers), agents/ (subagent definitions),
# commands/ (slash-command scripts). All banned by design D2.
_BANNED_DIRS = {"hooks", "bin", "monitors", "agents", "commands"}

# Component config files that would wire up execution inline: an MCP server table,
# an LSP server table, or a settings file that can register hooks/permissions.
_BANNED_FILES = {".mcp.json", ".lsp.json", "settings.json", "settings.local.json"}

# Keys that, present inline in plugin.json, configure an executable component
# without a component dir. Issue #88 names hooks/mcpServers/lsp explicitly; the
# near-cousins (lspServers, agents, commands) belong to the same executable class.
_BANNED_INLINE_KEYS = {"hooks", "mcpServers", "lsp", "lspServers", "agents", "commands"}

# Non-content files permitted on the surface: exactly the two JSON manifests. Every
# other shipped file must be markdown (issue #88: "markdown plus the two JSON
# manifests"). Compared by resolved path so a rogue plugin.json planted elsewhere
# is NOT waved through on basename alone.
_ALLOWED_NON_MD = {_PLUGIN_JSON.resolve(), _MARKETPLACE_JSON.resolve()}

# VCS/tooling dirs that never ship in the payload — pruned so a local .git or a
# stray __pycache__ can't register a false violation. (Neither exists under the
# surface on a clean checkout; pruning just makes the fence robust to local cruft.)
_PRUNE_DIRS = {".git", "__pycache__", ".pytest_cache"}

_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


# ---- pure fence helpers (reused by the real guards and the meta-tests) --------------------

def _iter_files(root):
    """Yield every real file under ``root``, pruning VCS/tooling dirs that never ship."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _banned_component_hits(plugin_root):
    """Banned component dirs/files found anywhere under ``plugin_root`` (sorted paths)."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(plugin_root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for d in dirnames:
            if d in _BANNED_DIRS:
                hits.append(str(Path(dirpath) / d) + "/")
        for f in filenames:
            if f in _BANNED_FILES:
                hits.append(str(Path(dirpath) / f))
    return sorted(hits)


def _inline_key_hits(manifest_data):
    """Banned executable-component keys present at the top level of a plugin manifest."""
    return sorted(k for k in _BANNED_INLINE_KEYS if k in manifest_data)


def _exec_bit_hits(roots):
    """Files carrying any executable bit anywhere under the given roots (sorted paths)."""
    hits = []
    for root in roots:
        for p in _iter_files(root):
            if p.stat().st_mode & _EXEC_BITS:
                hits.append(str(p))
    return sorted(hits)


def _disallowed_type_hits(roots, allowed_non_md):
    """Files that are neither markdown nor one of the permitted manifests (sorted paths)."""
    hits = []
    for root in roots:
        for p in _iter_files(root):
            if p.suffix == ".md":
                continue
            if p.resolve() in allowed_non_md:
                continue
            hits.append(str(p))
    return sorted(hits)


# ---- the fence, applied to the real repo (green on an inert main) -------------------------

def test_plugin_ships_no_banned_component_dirs_or_files():
    """No hooks/, bin/, monitors/, agents/, commands/, and no .mcp.json/.lsp.json/
    settings.json — each would execute ungated and ride updates (design D2)."""
    assert _PLUGIN.is_dir(), "the plugin payload must exist"
    hits = _banned_component_hits(_PLUGIN)
    assert hits == [], (
        "plugin/ must contain no executable component dir or config file "
        f"(design D2, update-gate ruling): {hits}"
    )


def test_plugin_manifest_has_no_inline_executable_component_keys():
    """plugin.json must not wire up an executable component inline — no
    hooks/mcpServers/lsp (nor the lspServers/agents/commands cousins) (design D2)."""
    assert _PLUGIN_JSON.is_file(), f"plugin manifest must exist at {_PLUGIN_JSON}"
    data = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
    hits = _inline_key_hits(data)
    assert hits == [], (
        "plugin.json must carry no inline executable-component key "
        f"(design D2, update-gate ruling): {hits}"
    )


def test_no_file_on_the_plugin_surface_is_executable():
    """No file under plugin/ or the repo-root .claude-plugin/ carries an executable
    bit — an executable file rides updates and runs ungated (design D2)."""
    assert _PLUGIN.is_dir(), "the plugin payload must exist"
    hits = _exec_bit_hits(_SURFACE_ROOTS)
    assert hits == [], (
        "no plugin-surface file may carry an executable bit "
        f"(design D2, update-gate ruling): {hits}"
    )


def test_plugin_surface_ships_only_markdown_and_the_two_json_manifests():
    """The whole surface is pure content: every file is markdown, except exactly the
    plugin.json and marketplace.json manifests. A new file type (a .py/.sh/.json that
    is not a manifest) is the earliest signal an executable component is creeping in."""
    assert _PLUGIN.is_dir(), "the plugin payload must exist"
    assert _PLUGIN_JSON.is_file() and _MARKETPLACE_JSON.is_file(), "both manifests must exist"
    hits = _disallowed_type_hits(_SURFACE_ROOTS, _ALLOWED_NON_MD)
    assert hits == [], (
        "the plugin surface may ship only markdown plus the two JSON manifests "
        f"(design D2, update-gate ruling): {hits}"
    )


# ---- meta-tests: prove the fence actually bites, so it can never rot green -----------------

def _write(path, text="x", executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _clean_synthetic_plugin(tmp_path):
    """A minimal inert payload: one skill's markdown plus an inert plugin.json."""
    plugin = tmp_path / "plugin"
    _write(plugin / "skills" / "superlooper" / "SKILL.md", "# skill\n")
    manifest = _write(plugin / ".claude-plugin" / "plugin.json", '{"name": "superlooper"}\n')
    return plugin, manifest


def test_fence_passes_a_clean_synthetic_payload(tmp_path):
    """Guard against the helpers being trivially always-failing: a clean tree is clean."""
    plugin, manifest = _clean_synthetic_plugin(tmp_path)
    assert _banned_component_hits(plugin) == []
    assert _exec_bit_hits((plugin,)) == []
    assert _disallowed_type_hits((plugin,), {manifest.resolve()}) == []
    assert _inline_key_hits(json.loads(manifest.read_text())) == []


def test_fence_flags_a_banned_component_dir(tmp_path):
    plugin, _ = _clean_synthetic_plugin(tmp_path)
    _write(plugin / "hooks" / "on-load.sh", "#!/bin/sh\n")
    hits = _banned_component_hits(plugin)
    assert any(h.endswith("/hooks/") for h in hits), hits


def test_fence_flags_a_banned_component_file(tmp_path):
    plugin, _ = _clean_synthetic_plugin(tmp_path)
    _write(plugin / ".mcp.json", '{"mcpServers": {}}\n')
    hits = _banned_component_hits(plugin)
    assert any(h.endswith("/.mcp.json") for h in hits), hits


def test_fence_flags_an_inline_executable_component_key():
    data = {"name": "superlooper", "hooks": {"PreToolUse": []}}
    assert "hooks" in _inline_key_hits(data)
    assert "mcpServers" in _inline_key_hits({"name": "x", "mcpServers": {}})
    assert "lsp" in _inline_key_hits({"name": "x", "lsp": {}})


def test_fence_flags_an_executable_file_bit(tmp_path):
    plugin, _ = _clean_synthetic_plugin(tmp_path)
    _write(plugin / "skills" / "superlooper" / "run.md", "# doc\n", executable=True)
    hits = _exec_bit_hits((plugin,))
    assert any(h.endswith("/run.md") for h in hits), hits


def test_fence_flags_a_non_content_file_type(tmp_path):
    plugin, manifest = _clean_synthetic_plugin(tmp_path)
    _write(plugin / "skills" / "superlooper" / "helper.py", "print('nope')\n")
    hits = _disallowed_type_hits((plugin,), {manifest.resolve()})
    assert any(h.endswith("/helper.py") for h in hits), hits
