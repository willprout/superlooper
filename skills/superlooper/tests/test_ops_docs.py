"""The ops docs ride the gated publish, and the doctor says so (issue #199, defect class D12).

The third of D12 the doc-lint cannot reach: "the debugger playbook wasn't installed on the machine
having the incident". A lint keeps the docs TRUE; it cannot put them on the machine. After the
plugin restructure the playbook and runner-ops travel as plugin CONTENT, which means a machine that
runs the loop but never installed the plugin — or whose plugin is disabled, which `doctor --stack`
already only WARNs about — has no playbook at 3am. The unattended debugger brief tells the session
to follow `references/unattended-contract.md`; on that machine there is nothing to follow.

So the gated `bin/install.sh` now MIRRORS the operational docs into the installed engine home
alongside the payload it already publishes, and `doctor --stack` verifies they are there and carry
the current publish stamp.

Two design points worth stating, because both are load-bearing:

  * **One home in the repo, a mirror on the machine.** The plugin restructure moved skill content
    out of the engine payload deliberately — "moved, never copied — one home, no drift, no
    double-load". That still holds: ``plugin/`` remains the only place these files are EDITED. The
    installer copies them out at publish time, the same way it copies the payload, so the mirror is
    a build product that cannot drift from its source by more than one publish.
  * **The playbook lands as PLAYBOOK.md, not SKILL.md.** A file named SKILL.md under
    ``~/.claude/skills/`` risks being discovered as a second, stale copy of the sl-debugger skill —
    exactly the double-load the restructure closed. Renaming it in the mirror makes the reference
    copy unmistakably a reference copy. Its ``references/`` links are relative and survive intact.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import ops_docs
import stack_doctor

_ENGINE = Path(__file__).resolve().parent.parent          # skills/superlooper
_REPO = Path(__file__).resolve().parents[3]               # the monorepo root
_INSTALLER = _REPO / "bin" / "install.sh"


# --------------------------------------------------------------------------------------------
# The doc set
# --------------------------------------------------------------------------------------------

def test_every_mirrored_source_exists_in_the_repo():
    missing = [src for src, _dst in ops_docs.OPS_DOCS if not (_REPO / src).is_file()]
    assert not missing, "ops_docs names sources that are not in the repo: %s" % missing


def test_the_whole_debugger_playbook_ships_not_just_its_entry_page():
    """Glob-vs-list: a reference page added to the playbook must be mirrored too.

    The playbook is only useful whole — SKILL.md routes to the four references, and the unattended
    contract (the one that constrains what a 3am session may touch) is one of them. A hand-written
    list that silently falls behind the directory is the same rot the doc-lint exists to stop, so
    it is checked here rather than trusted.
    """
    on_disk = {p.relative_to(_REPO).as_posix()
               for p in (_REPO / "plugin" / "skills" / "sl-debugger").rglob("*.md")}
    mirrored = {src for src, _dst in ops_docs.OPS_DOCS}
    assert on_disk, "the sl-debugger playbook has moved — this guard is looking at nothing"
    assert on_disk <= mirrored, (
        "playbook pages that would NOT reach an installed machine: %s"
        % sorted(on_disk - mirrored))


def test_the_whole_ops_reference_set_ships_not_just_runner_ops():
    """Same glob-vs-list guard for the superlooper skill's references.

    Without it, an operational reference added beside runner-ops.md would silently never reach a
    machine — the identical rot the sl-debugger guard above prevents, one directory over.
    """
    on_disk = {p.relative_to(_REPO).as_posix()
               for p in (_REPO / "plugin" / "skills" / "superlooper" / "references").glob("*.md")}
    mirrored = {src for src, _dst in ops_docs.OPS_DOCS}
    assert on_disk, "the superlooper skill's references have moved — this guard sees nothing"
    assert on_disk <= mirrored, (
        "ops references that would NOT reach an installed machine: %s"
        % sorted(on_disk - mirrored))


def test_the_operator_facing_ops_docs_ship():
    mirrored = {src for src, _dst in ops_docs.OPS_DOCS}
    for rel in ("skills/superlooper/docs/STACK.md",
                "plugin/skills/superlooper/references/runner-ops.md",
                "plugin/skills/sl-debugger/SKILL.md",
                "plugin/skills/sl-debugger/references/unattended-contract.md"):
        assert rel in mirrored, "%s must ship with the engine publish" % rel


def _md_references(text):
    """Every string in the doc that could be a relative reference to another `.md` page.

    Deliberately over-inclusive — markdown link targets AND backticked paths, including bare
    `foo.md` filenames. Which of these are real links is decided by resolution against the SOURCE
    tree, below, not by guessing here from the shape of the string.
    """
    found = set(re.findall(r"\]\(([^)\s]+\.md)[^)]*\)", text))     # [text](path.md)
    found |= set(re.findall(r"`([\w./-]+\.md)`", text))            # `path.md`, `../path.md`
    return {ref for ref in found if not ref.startswith(("http://", "https://", "/", "~"))}


def test_the_mirror_preserves_every_link_that_resolves_in_the_source_tree(tmp_path):
    """The invariant: a reference that works where the doc LIVES must work where it is PUBLISHED.

    That is the whole correctness condition for the mirror layout, and it is self-calibrating —
    resolution against the source tree decides what counts as a link, so there is no hand-written
    list of prefixes to fall behind. A bare `runner-ops.md` mentioned by name in STACK.md does not
    resolve beside STACK.md, so it was never a link and is not held to one; the playbook's
    `references/unattended-contract.md` and its `../superlooper/references/runner-ops.md` sibling
    both do resolve, so the mirror has to keep them resolving.

    An earlier version of this test extracted only `./`- and `../`-prefixed backticks and therefore
    walked exactly ONE link, leaving the playbook's whole routing table — five bare
    `references/<page>.md` pointers, including the contract the 3am brief sends the session to —
    unchecked. A mirror that flattened `sl-debugger/references/` would have broken every one of
    them with this test green.
    """
    dest = _published_dest(tmp_path)
    ops_docs.publish(_REPO, dest, "abc1234 2026-07-21")
    root = Path(ops_docs.mirror_dir(dest))

    broken, walked = [], 0
    for src, dst in ops_docs.OPS_DOCS:
        source_page = _REPO / src
        mirror_page = root / dst
        for ref in sorted(_md_references(source_page.read_text(encoding="utf-8"))):
            if not (source_page.parent / ref).exists():
                continue                      # not a relative link where it lives — a mention
            walked += 1
            if not (mirror_page.parent / ref).exists():
                broken.append("%s -> %s (resolves at %s, not in the mirror)" % (dst, ref, src))
    assert not broken, "the published ops-doc mirror breaks links that work in the repo: %s" % broken
    # A link-walk that walks nothing is the failure mode this test is most likely to rot into:
    # tighten the extractor by accident and it goes quietly, permanently green. The playbook's
    # entry page alone carries its four references plus the runner-ops sibling.
    assert walked >= 5, "the link extractor found only %d links — it has stopped looking" % walked


def test_the_playbooks_sibling_link_is_the_one_this_layout_exists_for():
    """A pin on the specific link that made flattening wrong, so the reason survives a refactor."""
    text = (_REPO / "plugin" / "skills" / "sl-debugger" / "SKILL.md").read_text(encoding="utf-8")
    assert "../superlooper/references/runner-ops.md" in text, (
        "the playbook no longer links sideways to runner-ops — if that is deliberate, the mirror "
        "may flatten again; if not, the link was lost")
    assert ("plugin/skills/superlooper/references/runner-ops.md",
            "superlooper/references/runner-ops.md") in ops_docs.OPS_DOCS


def test_no_mirror_target_is_named_skill_md():
    """A SKILL.md under the installed skills home could be discovered as a second sl-debugger."""
    bad = [dst for _src, dst in ops_docs.OPS_DOCS if Path(dst).name == "SKILL.md"]
    assert not bad, ("mirror targets named SKILL.md risk a double-loaded skill: %s" % bad)
    assert ("plugin/skills/sl-debugger/SKILL.md", "sl-debugger/PLAYBOOK.md") in ops_docs.OPS_DOCS


def test_mirror_targets_are_unique_and_relative():
    targets = [dst for _src, dst in ops_docs.OPS_DOCS]
    assert len(targets) == len(set(targets)), "two sources mirror onto the same target"
    for dst in targets:
        assert not os.path.isabs(dst) and ".." not in Path(dst).parts, dst


# --------------------------------------------------------------------------------------------
# publish()
# --------------------------------------------------------------------------------------------

def _published_dest(tmp_path, version="abc1234 2026-07-21"):
    """An install destination in the state the installer leaves it in before step 6.

    publish() refuses anything else — a directory with no VERSION stamp is not a published engine
    home, it is a mistyped path, and refusing is what makes the rmtree inside it safe. So the
    tests build the real precondition rather than a bare mkdir.
    """
    dest = tmp_path / "installed"
    dest.mkdir()
    (dest / "VERSION").write_text(version + "\n", encoding="utf-8")
    return dest


def test_publish_writes_every_doc_and_the_stamp(tmp_path):
    dest = _published_dest(tmp_path)
    written = ops_docs.publish(_REPO, dest, "abc1234 2026-07-21")

    assert sorted(written) == sorted(dst for _src, dst in ops_docs.OPS_DOCS)
    for src, dst in ops_docs.OPS_DOCS:
        landed = Path(ops_docs.mirror_dir(dest)) / dst
        assert landed.is_file(), "%s did not land at %s" % (src, dst)
        assert landed.read_text(encoding="utf-8") == (_REPO / src).read_text(encoding="utf-8")
    assert Path(ops_docs.stamp_path(dest)).read_text(encoding="utf-8").strip() == "abc1234 2026-07-21"


def test_publish_clears_a_stale_mirror_and_is_idempotent(tmp_path):
    dest = _published_dest(tmp_path)
    ops_docs.publish(_REPO, dest, "old 2026-01-01")
    orphan = Path(ops_docs.mirror_dir(dest)) / "retired-page.md"
    orphan.write_text("a doc that no longer exists upstream\n", encoding="utf-8")

    first = ops_docs.publish(_REPO, dest, "new 2026-07-21")
    second = ops_docs.publish(_REPO, dest, "new 2026-07-21")

    assert first == second
    assert not orphan.exists(), "a stale mirrored page survived a republish"
    assert Path(ops_docs.stamp_path(dest)).read_text(encoding="utf-8").strip() == "new 2026-07-21"


def test_publish_refuses_a_missing_source(tmp_path, monkeypatch):
    dest = _published_dest(tmp_path)
    monkeypatch.setattr(ops_docs, "OPS_DOCS",
                        (("plugin/skills/sl-debugger/NOPE.md", "sl-debugger/NOPE.md"),))
    with pytest.raises(ops_docs.MissingOpsDoc):
        ops_docs.publish(_REPO, dest, "abc1234 2026-07-21")


def test_publish_refuses_a_destination_that_is_not_a_published_engine_home(tmp_path):
    """The rmtree's real guard: `--dest ~` must not clear `~/docs/ops`.

    `publish` deletes and rebuilds `<dest>/docs/ops`, so the only thing standing between a
    mistyped `--dest` and someone's directory is this precondition. Requiring the VERSION stamp
    the installer writes at step 3 costs nothing (it is always there by step 6) and is not a
    property any ordinary directory has.
    """
    plain = tmp_path / "somebody-elses-home"
    (plain / "docs" / "ops").mkdir(parents=True)
    keep = plain / "docs" / "ops" / "notes.md"
    keep.write_text("not ours\n", encoding="utf-8")

    with pytest.raises(ops_docs.MissingOpsDoc):
        ops_docs.publish(_REPO, plain, "abc1234 2026-07-21")
    assert keep.exists(), "publish cleared a directory that was not a published engine home"

    with pytest.raises(ops_docs.MissingOpsDoc):
        ops_docs.publish(_REPO, tmp_path / "does-not-exist", "abc1234 2026-07-21")


def test_publish_never_escapes_the_mirror_directory(tmp_path):
    """Everything publish() touches lives under <dest>/docs/ops — it is not a general copier."""
    dest = _published_dest(tmp_path)
    (dest / "lib").mkdir()
    (dest / "lib" / "gate.py").write_text("payload\n", encoding="utf-8")

    ops_docs.publish(_REPO, dest, "abc1234 2026-07-21")

    assert (dest / "lib" / "gate.py").read_text(encoding="utf-8") == "payload\n"
    assert (dest / "VERSION").read_text(encoding="utf-8") == "abc1234 2026-07-21\n"
    assert sorted(p.name for p in dest.iterdir()) == ["VERSION", "docs", "lib"]


def test_expected_paths_covers_every_doc_plus_the_stamp(tmp_path):
    dest = tmp_path / "installed"
    expected = set(ops_docs.expected_paths(dest))
    assert ops_docs.stamp_path(dest) in expected
    assert len(expected) == len(ops_docs.OPS_DOCS) + 1
    for _src, dst in ops_docs.OPS_DOCS:
        assert os.path.join(ops_docs.mirror_dir(dest), *dst.split("/")) in expected


def test_list_cli_prints_the_sources_for_the_installer(tmp_path):
    proc = subprocess.run([sys.executable, str(_ENGINE / "skill" / "lib" / "ops_docs.py"), "--list"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    printed = [l for l in proc.stdout.splitlines() if l.strip()]
    assert printed == [src for src, _dst in ops_docs.OPS_DOCS]


def test_publish_cli_mirrors_the_docs(tmp_path):
    dest = _published_dest(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(_ENGINE / "skill" / "lib" / "ops_docs.py"), "--publish",
         "--repo-root", str(_REPO), "--dest", str(dest), "--version", "abc1234 2026-07-21"],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (Path(ops_docs.mirror_dir(dest)) / "sl-debugger" / "PLAYBOOK.md").is_file()
    assert Path(ops_docs.stamp_path(dest)).is_file()


# --------------------------------------------------------------------------------------------
# The gated installer actually does it
# --------------------------------------------------------------------------------------------

def _installer_text():
    return _INSTALLER.read_text(encoding="utf-8")


def test_the_gated_installer_publishes_the_ops_docs():
    text = _installer_text()
    assert "ops_docs.py" in text, "bin/install.sh must invoke the ops-docs mirror"
    assert "--publish" in text and "--repo-root" in text, text[-2000:]


def test_the_engine_diff_gate_covers_the_ops_doc_sources():
    """A doc change must be shown at the publish gate, like every other published file.

    The gate's whole promise is "here is what you are about to make live". Mirroring docs the gate
    never diffed would put content on the machine that the human OK'd without seeing.
    """
    text = _installer_text()
    assert "OPS_DOC_PATHS" in text, "the gate's diff scope must include the ops-doc sources"
    # The scope is used by BOTH gate branches: the no-baseline ls-files listing and the diff.
    for anchor in ("ls-files", "diff --name-status"):
        line = next((l for l in text.splitlines() if anchor in l and "git -C" in l), None)
        assert line is not None, "lost the %r line in the gate" % anchor
        assert "$OPS_DOC_PATHS" in line or "${OPS_DOC_PATHS" in line, (
            "%r scopes the gate to the payload only, so an ops-doc change would publish "
            "unreviewed: %s" % (anchor, line))


def test_the_installer_dry_run_announces_the_ops_docs():
    text = _installer_text()
    assert re.search(r"would mirror.*ops docs|ops docs.*would", text, re.IGNORECASE), \
        "--dry-run must say the ops docs would be mirrored"


# --------------------------------------------------------------------------------------------
# doctor --stack's block
# --------------------------------------------------------------------------------------------

class FakeProbe:
    """A probe whose filesystem is a dict — no real ~/.claude, no real binaries."""

    def __init__(self, home="/fake-home", files=None):
        self.home = home
        self.env = {"HOME": home}
        self.files = dict(files or {})

    def command(self, name, envvar=None, default=None):
        return None

    def run(self, argv, timeout=10):
        raise AssertionError("check_ops_docs must not run an external binary: %r" % (argv,))

    def exists(self, path):
        return path in self.files or any(k.startswith(path.rstrip("/") + "/") for k in self.files)

    def read_text(self, path):
        return self.files.get(path)

    def expanduser(self, path):
        return path

    def pid_alive(self, pid):
        return False


def _install_home(home="/fake-home"):
    return os.path.join(home, ".claude", "skills", "superlooper")


def _healthy_files(home="/fake-home", version="abc1234 2026-07-21"):
    dest = _install_home(home)
    files = {os.path.join(dest, "VERSION"): version + "\n",
             ops_docs.stamp_path(dest): version + "\n"}
    for _src, dst in ops_docs.OPS_DOCS:
        files[os.path.join(ops_docs.mirror_dir(dest), *dst.split("/"))] = "# doc\n"
    return files


def test_ops_docs_block_passes_on_a_freshly_published_machine():
    result = stack_doctor.check_ops_docs(FakeProbe(files=_healthy_files()))
    assert result.name == "installed ops docs"
    assert result.ok and not result.warn, result.detail
    assert "abc1234" in result.detail


def test_ops_docs_block_fails_when_the_playbook_is_absent():
    files = _healthy_files()
    playbook = os.path.join(ops_docs.mirror_dir(_install_home()), "sl-debugger", "PLAYBOOK.md")
    assert playbook in files
    del files[playbook]

    result = stack_doctor.check_ops_docs(FakeProbe(files=files))

    assert not result.ok, result.detail
    assert "PLAYBOOK.md" in result.detail
    assert "install.sh" in result.fix


def test_ops_docs_block_fails_when_the_mirror_is_from_an_older_publish():
    files = _healthy_files(version="new5678 2026-07-21")
    files[ops_docs.stamp_path(_install_home())] = "old1234 2026-07-01\n"

    result = stack_doctor.check_ops_docs(FakeProbe(files=files))

    assert not result.ok, result.detail
    assert "old1234" in result.detail and "new5678" in result.detail
    assert "install.sh" in result.fix


def test_ops_docs_block_fails_when_nothing_was_mirrored_at_all():
    """A published engine with no mirror beside it: the mirror step failed, or the CLI is being
    run from a source checkout against an engine published before this change."""
    dest = _install_home()
    result = stack_doctor.check_ops_docs(
        FakeProbe(files={os.path.join(dest, "VERSION"): "abc1234 2026-07-21\n"}))

    assert not result.ok, result.detail
    assert "install.sh" in result.fix


def test_ops_docs_block_skips_cleanly_when_no_engine_is_installed():
    """No installed engine at all is another block's problem — never this one's false alarm."""
    result = stack_doctor.check_ops_docs(FakeProbe(files={}))
    assert result.ok and not result.warn, result.detail
    assert "no installed engine" in result.detail.lower()


def test_ops_docs_block_warns_when_the_engine_carries_no_stamp_to_compare():
    files = _healthy_files()
    dest = _install_home()
    del files[os.path.join(dest, "VERSION")]

    result = stack_doctor.check_ops_docs(FakeProbe(files=files))

    assert result.ok and result.warn, result.detail
    assert "stamp" in result.detail.lower()


def test_check_stack_emits_the_ops_docs_block():
    names = [r.name for r in stack_doctor.check_stack({}, probe=FakeProbe(files=_healthy_files()),
                                                      sender=lambda *a, **k: None,
                                                      announce=lambda *a, **k: None)]
    assert "installed ops docs" in names
    assert len(names) == len(set(names)), "duplicate block name in check_stack"


# --------------------------------------------------------------------------------------------
# The brief the 3am session actually reads
# --------------------------------------------------------------------------------------------

def _mirror_rel():
    """The mirror's location as the CODE defines it — never as a test spells it."""
    return "/".join(ops_docs.MIRROR_REL)


def test_the_unattended_brief_names_the_installed_playbook_fallback():
    """D12's actual failure: the brief said "follow the skill" on a machine with no skill.

    The brief must name a path that exists on a machine the gated installer has published to, so a
    session whose plugin is missing or disabled can still read the contract it is being held to.

    Derived from ``MIRROR_REL`` + ``OPS_DOCS``, never spelled: a hand-written `"docs/ops/…"` here
    would survive a rename of the mirror directory and leave this test confidently green while the
    brief pointed a 3am session at a path that no longer exists — which is precisely the defect
    class the whole issue is about, reintroduced inside its own fix.
    """
    brief = (_ENGINE / "skill" / "templates" / "debugger-brief.md").read_text(encoding="utf-8")
    playbook = dict(ops_docs.OPS_DOCS)["plugin/skills/sl-debugger/SKILL.md"]
    assert "%s/%s" % (_mirror_rel(), playbook) in brief, (
        "the unattended brief must route to the installed ops-docs mirror when the sl-debugger "
        "skill is not on the machine, and must name it where the code actually publishes it")
    assert "unattended-contract.md" in brief


def test_the_stack_doc_names_the_mirror_where_the_code_publishes_it():
    """STACK.md tells the operator where the ops docs land; same derive-don't-spell discipline."""
    stack = (_ENGINE / "docs" / "STACK.md").read_text(encoding="utf-8")
    assert "%s/" % _mirror_rel() in stack, (
        "STACK.md's `installed ops docs` entry must name the directory ops_docs.MIRROR_REL "
        "actually publishes into")


def test_the_doctor_reports_the_mirror_where_the_code_publishes_it():
    """And the block's own detail/fix text, so all three operator-facing spellings move together."""
    result = stack_doctor.check_ops_docs(FakeProbe(files=_healthy_files()))
    assert _mirror_rel() in result.detail
