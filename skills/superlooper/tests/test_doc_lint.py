"""Doc-lint against a LIVE manifest generated from the code (issue #199, defect class D12).

D12 named doc drift a root cause of lost nights: "ops docs name dead verbs, a sync orphaned the
installed docs, the debugger playbook wasn't installed on the machine having the incident". The
first two thirds of that are what this file mechanises. An operator — or one of the owner's helper
agents, which is the case that actually hurt — reaches for an ops doc mid-incident and acts on a
name the running system no longer has. Nothing errors; the recovery just goes wrong.

The checks themselves live in ``lib/doc_lint.py`` (moved there for issue #200, so
``superlooper upkeep`` reports the same verdict in its weekly once-over instead of a second
implementation drifting alongside this one). Read that module for what each reader does and why.
THIS file is the guard: it points the readers at this checkout, parametrizes them over the whole
doc set, holds STACK.md to its completeness claim, and — the part that keeps a lint from rotting
into decoration — proves each check still fires by building the violation out of synthetic source.

What this lint does NOT cover, stated so nobody reads more into it than it does:

  * **Verbs only in the two-word ``superlooper <verb>`` form.** Docs also name verbs bare in
    backticks (`` `doctor` ``, `` `tidy` ``); bare words cannot be checked without flagging
    ordinary English. Renaming a verb is caught wherever a doc writes the invocation, not
    everywhere the verb is mentioned. Flags (``--stack``) are not covered at all.
  * **Block names both ways only inside STACK.md**, which is the only doc making an "emits these
    exact block names" claim — the completeness the DoD asks for. STACK.md's Tier lists get a
    one-way phantom check; block names mentioned in the other ops docs are unchecked, because
    nothing distinguishes a doctor block name from an ordinary backticked phrase without a registry
    of retired names to compare against.
  * **Invented BARE labels** (`needs-attention`) — see ``doc_lint.documented_labels``.
  * **Repo paths under `bin/` and `docs/`** — those two prefixes are ambiguous in these docs (they
    are also payload- and installed-home-relative), so citations of `bin/install.sh` and
    `docs/ADOPTING.md` go unchecked. See ``doc_lint._REPO_TOP_DIRS``.

Three properties keep it honest:

  1. **The doc set is globbed, not listed.** Every ``.md`` under ``plugin/skills/`` is linted
     automatically, so a new playbook page is covered the day it lands. The few docs named
     individually are pinned by ``test_the_linted_doc_set_is_complete_and_real``.
  2. **Completeness runs both ways** where a doc makes a completeness claim. STACK.md's "Check
     Names And Fixes" section says "emits these exact block names"; the lint fails when the doctor
     grows a name the list omits AND when the list names a block the doctor cannot emit.
  3. **Meta-tests.** ``test_lint_flags_*`` builds each violation class out of synthetic source and
     asserts the lint catches it, so a lint that silently stopped looking cannot stay green.

Scope (issue #199 Boundaries): the OPERATIONAL docs — STACK.md, runner-ops, the skill playbooks,
ADOPTING.md, the root README. Not the design records, incident write-ups or the reliability ledger:
those are dated historical documents whose whole job is to say what was true THEN, so pinning them
to today's verb table would be wrong.
"""
import sys
from pathlib import Path

import pytest

import doc_lint
import stack_doctor
from doc_lint import (code_spans, documented_labels, documented_repo_paths,  # noqa: F401
                      documented_verbs, live_doctor_blocks, live_verbs)

_ENGINE = Path(__file__).resolve().parent.parent          # skills/superlooper
_REPO = Path(__file__).resolve().parents[3]               # the monorepo root
_CLI = _REPO / doc_lint.CLI_REL
_STACK_DOCTOR = _REPO / doc_lint.STACK_DOCTOR_REL
_STACK_MD = _REPO / doc_lint.STACK_MD_REL

_NON_OPERATIONAL_PREFIXES = doc_lint.NON_OPERATIONAL_PREFIXES
_NON_OPERATIONAL_EXACT = doc_lint.NON_OPERATIONAL_EXACT
_TIER_NON_BLOCK_BULLETS = doc_lint.TIER_NON_BLOCK_BULLETS


def manifest():
    """The whole live-verb manifest for THIS checkout."""
    return doc_lint.manifest(_REPO)


def ops_docs():
    """Every operational doc in this checkout, repo-relative, sorted."""
    return doc_lint.ops_docs(_REPO)


def _documented_block_names():
    return doc_lint.documented_block_names(_REPO)


def _tier_bullet_names():
    return doc_lint.tier_bullet_names(_REPO)


def _read(rel):
    return (_REPO / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# The lint.
# --------------------------------------------------------------------------------------------

def test_the_manifest_is_generated_and_non_empty():
    m = manifest()
    assert m["verbs"], "no CLI verbs read from the source — the extractor has stopped working"
    assert m["labels"], "no labels read from labels.LABELS"
    assert m["doctor_blocks"], "no doctor block names read from stack_doctor"
    # Spot-anchors: if these three ever leave the manifest the extractor is broken, not the code.
    assert {"run", "doctor", "adopt"} <= m["verbs"]
    assert {"agent-ready", "needs-owner"} <= m["labels"]
    assert "notify channel" in m["doctor_blocks"]


def test_the_linted_doc_set_is_complete_and_real():
    docs = ops_docs()
    for rel in docs:
        assert (_REPO / rel).is_file(), "linted doc %s does not exist" % rel
    # Every playbook page is in, by glob.
    assert "plugin/skills/sl-debugger/SKILL.md" in docs
    assert "plugin/skills/sl-debugger/references/unattended-contract.md" in docs
    assert "plugin/skills/superlooper/references/runner-ops.md" in docs
    assert "skills/superlooper/docs/STACK.md" in docs
    # And nothing operational hides in skills/superlooper/docs/ without being linted or explicitly
    # classified as a dated record.
    linted = set(docs)
    stray = []
    for path in sorted((_ENGINE / "docs").glob("*.md")):
        rel = path.relative_to(_REPO).as_posix()
        if rel in linted:
            continue
        if path.name in _NON_OPERATIONAL_EXACT or path.name.startswith(_NON_OPERATIONAL_PREFIXES):
            continue
        stray.append(rel)
    assert not stray, (
        "these docs are neither linted nor classified as dated records: %s. Add them to "
        "doc_lint.NAMED if they are operational, or to the non-operational classification if they "
        "are history." % stray)


@pytest.mark.parametrize("rel", ops_docs())
def test_ops_doc_names_only_live_verbs(rel):
    m = manifest()
    documented = documented_verbs(_read(rel), m["doctor_blocks"])
    unknown = documented - m["verbs"]
    assert not unknown, (
        "%s invokes `superlooper <verb>` for verbs the CLI does not register: %s (live verbs: %s). "
        "A dead verb in an ops doc is defect class D12 — the operator or helper agent runs it "
        "mid-incident and gets nothing." % (rel, sorted(unknown), sorted(m["verbs"])))


@pytest.mark.parametrize("rel", ops_docs())
def test_ops_doc_names_only_live_labels(rel):
    m = manifest()
    unknown, stale = documented_labels(_read(rel), m["labels"], m["retired_labels"])
    assert not unknown, (
        "%s names labels that are not in labels.LABELS: %s. gh refuses to apply a label that does "
        "not exist, so a doc naming one hands the reader an instruction that cannot work."
        % (rel, sorted(unknown)))
    assert not stale, (
        "%s describes current behaviour using retired label name(s) %s in a paragraph that never "
        "names the replacement. Say %s, or keep the old name only in a passage that explains the "
        "rename (as runner-ops.md and ADOPTING.md do)."
        % (rel, sorted(stale), sorted(m["retired_labels"][n] for n in sorted(stale))))


@pytest.mark.parametrize("rel", ops_docs())
def test_ops_doc_cites_only_paths_that_exist(rel):
    missing = sorted(p for p in documented_repo_paths(_read(rel)) if not (_REPO / p).exists())
    assert not missing, (
        "%s cites repo paths that do not exist: %s. This is the 'a sync orphaned the installed "
        "docs' third of D12 — the reference outlived the move." % (rel, missing))


def test_stack_md_tier_lists_name_no_block_the_code_cannot_emit():
    """The fixes list is not the only place STACK.md spells block names.

    A Tier bullet naming a block that no longer exists is the same lie in a different section, and
    it is the section an operator reads FIRST — the tiers are the "what does this machine need"
    walkthrough; the fixes list is what they turn to after a red line.
    """
    live = live_doctor_blocks(_STACK_DOCTOR)
    named = _tier_bullet_names()
    assert named, "STACK.md's Tier lists must bullet the block names in backticks"
    phantom = sorted({n for n in named if n not in live and n not in _TIER_NON_BLOCK_BULLETS})
    assert not phantom, (
        "STACK.md's Tier lists name blocks doctor --stack cannot emit: %s — the tiers are the "
        "first thing an operator reads. If one of these is deliberately not a block name, add it "
        "to doc_lint.TIER_NON_BLOCK_BULLETS with a reason." % phantom)


def test_stack_md_documents_every_block_name_the_code_can_emit():
    """Completeness, direction one: the doctor grew a block, the doc did not.

    This is issue #142's defect (three blocks emitted, none listed) made un-repeatable from the
    static side. ``test_stack_doctor.py`` pins the same claim dynamically; the two disagree only
    when a name exists in code but no FakeProbe path reaches it, which is precisely the drift this
    static read is here to catch.
    """
    live = live_doctor_blocks(_STACK_DOCTOR)
    documented = set(_documented_block_names())
    assert documented, ("STACK.md's %r section must bullet the block names"
                        % doc_lint.CHECK_NAMES_HEADING)
    undocumented = sorted(live - documented)
    assert not undocumented, (
        "doctor --stack can emit block names STACK.md's 'Check Names And Fixes' list omits, so its "
        '"emits these exact block names" claim is false: %s' % undocumented)
    # Issue #142's three, pinned by name. They were the original breach; naming them here means a
    # regression reads as "the #142 blocks fell out again" rather than a nameless diff.
    for name in ("cmux App Nap disabled", "runner anchor (live)", "installed engine current"):
        assert name in live, "%r is no longer a doctor block — update this pin deliberately" % name
        assert name in documented, "STACK.md dropped issue #142's block %r" % name


def test_stack_md_names_no_block_the_code_cannot_emit():
    """Completeness, direction two: the doc names a block that no longer exists."""
    live = live_doctor_blocks(_STACK_DOCTOR)
    phantom = sorted(set(_documented_block_names()) - live)
    assert not phantom, (
        "STACK.md's 'Check Names And Fixes' list names blocks doctor --stack cannot emit: %s — an "
        "operator looking one up finds an entry for a line they will never see." % phantom)


def test_the_static_and_dynamic_block_readings_agree_on_what_ships():
    """The AST manifest must be a superset of what a real ``check_stack`` call emits.

    If it ever is not, the static reader has stopped seeing a live block and every lint above it
    has a hole. Uses the same green-machine shape ``test_stack_doctor.py`` builds, minimally: we
    only need the NAMES, and ``check_stack`` returns one result per block regardless of verdict.
    """
    class _DeadProbe:
        env = {"HOME": "/nonexistent-sl-doc-lint"}
        home = "/nonexistent-sl-doc-lint"

        def command(self, name, envvar=None, default=None):
            return None

        def run(self, argv, timeout=10):
            raise AssertionError("doc-lint must not run an external binary: %r" % (argv,))

        def exists(self, path):
            return False

        def read_text(self, path):
            return None

        def expanduser(self, path):
            return path

        def pid_alive(self, pid):
            return False

    emitted = {r.name for r in stack_doctor.check_stack({}, probe=_DeadProbe(),
                                                        sender=lambda *a, **k: None,
                                                        announce=lambda *a, **k: None)}
    missed = sorted(emitted - live_doctor_blocks(_STACK_DOCTOR))
    assert not missed, (
        "check_stack emits block names the static manifest does not see: %s — the AST reader is "
        "blind to them and the doc lint silently stopped covering them." % missed)


# --------------------------------------------------------------------------------------------
# lint(): the same checks as ONE verdict, which is what `superlooper upkeep` reports weekly.
# --------------------------------------------------------------------------------------------

def test_lint_agrees_with_the_per_doc_tests_on_this_checkout():
    """The aggregate must see this repo exactly as the parametrized tests above do.

    Two implementations of "is the doc set clean" that could disagree would be worse than one:
    upkeep would print a green line on a Sunday for a repo CI has been failing since Friday.
    """
    result = doc_lint.lint(_REPO)
    assert result["status"] == "clean", result["findings"]
    assert result["docs"] == len(ops_docs())


def test_lint_skips_cleanly_outside_a_superlooper_checkout(tmp_path):
    """Every adopted repo but this one has no ops docs at all. That is a SKIP, not a finding."""
    result = doc_lint.lint(tmp_path)
    assert result["status"] == "skipped" and result["findings"] == []
    assert "source checkout" in result["detail"]


def test_lint_reports_a_dead_verb_as_a_finding_rather_than_raising(tmp_path):
    """The aggregate must FIND what the per-doc tests FAIL on — built from a synthetic checkout so
    the assertion cannot be satisfied by this repo happening to be clean."""
    root = tmp_path / "repo"
    (root / "plugin" / "skills").mkdir(parents=True)
    (root / doc_lint.CLI_REL).parent.mkdir(parents=True)
    (root / doc_lint.CLI_REL).write_text('def main():\n    add("doctor", cmd_doctor)\n',
                                         encoding="utf-8")
    (root / doc_lint.STACK_DOCTOR_REL).parent.mkdir(parents=True)
    (root / doc_lint.STACK_DOCTOR_REL).write_text(
        'def check_thing(probe):\n    return CheckResult("a block", True)\n'
        'def check_stack(config):\n    return [check_thing(None)]\n', encoding="utf-8")
    (root / doc_lint.STACK_MD_REL).parent.mkdir(parents=True)
    (root / doc_lint.STACK_MD_REL).write_text(
        "# Stack\n\n## Tier 1\n\n- `a block` - it does a thing\n\n"
        "## Check Names And Fixes\n\n- `a block`: fix it\n", encoding="utf-8")
    (root / "plugin" / "skills" / "PAGE.md").write_text(
        "Run `superlooper resurrect --repo .` when the lane wedges.\n"
        "See `skills/superlooper/docs/NOPE.md`.\n", encoding="utf-8")
    for rel in doc_lint.NAMED:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# nothing to see\n", encoding="utf-8")

    result = doc_lint.lint(root)

    assert result["status"] == "findings"
    joined = "\n".join(result["findings"])
    assert "superlooper resurrect" in joined
    assert "NOPE.md" in joined


def test_lint_bounds_its_findings_and_says_how_many_it_dropped(tmp_path):
    """A repo-wide rename can produce hundreds. A one-page report must truncate — and SAY so."""
    root = tmp_path / "repo"
    (root / "plugin" / "skills").mkdir(parents=True)
    (root / doc_lint.CLI_REL).parent.mkdir(parents=True)
    (root / doc_lint.CLI_REL).write_text('def main():\n    add("doctor", cmd_doctor)\n',
                                         encoding="utf-8")
    (root / doc_lint.STACK_DOCTOR_REL).parent.mkdir(parents=True)
    (root / doc_lint.STACK_DOCTOR_REL).write_text(
        'def check_thing(probe):\n    return CheckResult("a block", True)\n'
        'def check_stack(config):\n    return [check_thing(None)]\n', encoding="utf-8")
    (root / doc_lint.STACK_MD_REL).parent.mkdir(parents=True)
    (root / doc_lint.STACK_MD_REL).write_text(
        "# Stack\n\n## Tier 1\n\n- `a block` - it does a thing\n\n"
        "## Check Names And Fixes\n\n- `a block`: fix it\n", encoding="utf-8")
    for rel in doc_lint.NAMED:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# nothing to see\n", encoding="utf-8")
    # Distinct ALPHABETIC verb names: the verb pattern is `[a-z][a-z-]*`, so `phantom1`/`phantom2`
    # would both tokenise to the one verb `phantom` and produce a single finding.
    (root / "plugin" / "skills" / "PAGE.md").write_text(
        "".join("Run `superlooper phantom%s`.\n" % (chr(ord("a") + i))
                for i in range(doc_lint.MAX_FINDINGS + 5)), encoding="utf-8")

    result = doc_lint.lint(root)

    assert len(result["findings"]) == doc_lint.MAX_FINDINGS
    assert "5 more finding(s)" in result["detail"]


def test_lint_never_raises_on_an_unreadable_manifest(tmp_path):
    root = tmp_path / "repo"
    (root / "plugin" / "skills").mkdir(parents=True)
    (root / doc_lint.CLI_REL).parent.mkdir(parents=True)
    (root / doc_lint.CLI_REL).write_text("def main(:\n", encoding="utf-8")   # a syntax error
    result = doc_lint.lint(root)
    assert result["status"] == "findings"
    assert "manifest" in result["findings"][0]


# --------------------------------------------------------------------------------------------
# Meta-tests: prove each check actually fires. A guard that cannot go red is decoration.
# --------------------------------------------------------------------------------------------

def test_lint_flags_a_dead_verb():
    m = manifest()
    doc = "Run `superlooper resurrect --repo .` when the lane wedges.\n"
    assert documented_verbs(doc, m["doctor_blocks"]) - m["verbs"] == {"resurrect"}


def test_lint_ignores_a_verb_shaped_phrase_outside_code_formatting():
    m = manifest()
    doc = "This bolts the superlooper resurrect idea onto the runner.\n"
    assert not documented_verbs(doc, m["doctor_blocks"]) - m["verbs"]


def test_lint_does_not_mistake_the_superlooper_plugin_block_for_a_verb():
    m = manifest()
    assert "superlooper plugin" in m["doctor_blocks"]
    doc = "The `superlooper plugin` block WARNs when it is missing.\n"
    assert not documented_verbs(doc, m["doctor_blocks"]) - m["verbs"]


def test_lint_flags_an_unknown_label():
    m = manifest()
    unknown, stale = documented_labels("Drop `type:refactor` on it.\n",
                                       m["labels"], m["retired_labels"])
    assert unknown == {"type:refactor"} and not stale


def test_lint_flags_a_retired_label_used_as_current_behaviour():
    m = manifest()
    unknown, stale = documented_labels("A sensitive-area diff still parks `needs-william`.\n",
                                       m["labels"], m["retired_labels"])
    assert stale == {"needs-william"} and not unknown


def test_lint_flags_a_retired_label_that_ends_a_sentence():
    """The most common shape in prose, and the one the tokeniser swallowed.

    `.` is inside the token class so a dotted string is read as one token rather than split into a
    label-shaped fragment, which meant `needs-william.` used to match nothing at all — and the
    approval-protocol.md sentence this lint was written to catch is one edit away from that form.
    """
    m = manifest()
    for doc in ("A sensitive-area diff still parks needs-william.\n",
                "A sensitive-area diff still parks needs-william,\n",
                "A sensitive-area diff still parks **needs-william**.\n"):
        _unknown, stale = documented_labels(doc, m["labels"], m["retired_labels"])
        assert stale == {"needs-william"}, "missed the retired label in %r" % doc


def test_lint_flags_a_bad_label_that_ends_a_sentence():
    m = manifest()
    unknown, _stale = documented_labels("Drop `priority:urgent`.\n",
                                        m["labels"], m["retired_labels"])
    assert unknown == {"priority:urgent"}


def test_lint_allows_a_retired_label_in_a_sentence_that_names_its_replacement():
    m = manifest()
    doc = "`needs-owner` — an owner decision is required (renamed from `needs-william`).\n"
    unknown, stale = documented_labels(doc, m["labels"], m["retired_labels"])
    assert not unknown and not stale


def test_lint_accepts_a_concrete_value_in_an_open_label_family():
    """`model:`/`effort:` values are open by owner ruling — LABELS is a starter set, not a gate.

    A doc writing `model:haiku` is documenting something that genuinely works. A lint that
    reddened CI over it would be wrong AND would teach the next author to delete the lint.
    """
    m = manifest()
    assert "model:haiku" not in m["labels"] and "effort:ultra" not in m["labels"]
    doc = "Drop `model:haiku` on a cheap issue, or `effort:ultra` on the hardest one.\n"
    unknown, stale = documented_labels(doc, m["labels"], m["retired_labels"])
    assert not unknown and not stale


def test_lint_still_flags_a_bad_value_in_a_closed_label_family():
    m = manifest()
    unknown, _stale = documented_labels("Use `priority:urgent`.\n", m["labels"], m["retired_labels"])
    assert unknown == {"priority:urgent"}, "priority: values are closed and must stay checked"


def test_lint_does_not_let_one_good_table_row_excuse_a_stale_one():
    """A table is many records, not one paragraph — runner-ops.md's label table is 15 rows."""
    m = manifest()
    doc = ("| `needs-owner` | an owner decision is required (renamed from `needs-william`) |\n"
           "| something else | a capped conflict parks `needs-william` |\n")
    _unknown, stale = documented_labels(doc, m["labels"], m["retired_labels"])
    assert stale == {"needs-william"}


def test_lint_ignores_a_label_family_wildcard():
    m = manifest()
    doc = "Duplicate `model:*`/`effort:*` labels make the runner wait.\n"
    unknown, stale = documented_labels(doc, m["labels"], m["retired_labels"])
    assert not unknown and not stale


def test_lint_flags_a_repo_path_that_does_not_exist():
    doc = "See `skills/superlooper/docs/NOPE.md` for the ladder.\n"
    found = documented_repo_paths(doc)
    assert found == {"skills/superlooper/docs/NOPE.md"}
    assert not (_REPO / "skills/superlooper/docs/NOPE.md").exists()


def test_lint_ignores_an_installed_path_that_only_looks_repo_relative():
    doc = "Read `~/.claude/skills/superlooper/docs/ADOPTING.md` on the machine.\n"
    assert not documented_repo_paths(doc)


def test_lint_ignores_a_repo_path_glob():
    doc = "The corpus lives in `skills/superlooper/docs/INCIDENT-*.md`.\n"
    assert not documented_repo_paths(doc)


def test_verb_extractor_ignores_add_calls_in_prose(tmp_path):
    fake = tmp_path / "superlooper"
    fake.write_text('"""help table\n\nadd("phantom", nope)\n"""\n'
                    '# add("commented-out", cmd_x)\n'
                    'def main():\n'
                    '    add("real", cmd_real)\n', encoding="utf-8")
    assert live_verbs(fake) == {"real"}


def test_block_extractor_refuses_an_unresolvable_name(tmp_path):
    fake = tmp_path / "stack_doctor.py"
    fake.write_text(
        "def check_thing(probe):\n"
        "    return CheckResult(NAMES[0], True)\n"
        "def check_stack(config):\n"
        "    return [check_thing(None)]\n", encoding="utf-8")
    with pytest.raises(doc_lint.UnreadableManifest, match="could not statically resolve"):
        live_doctor_blocks(fake)


def test_block_extractor_resolves_a_locally_bound_name(tmp_path):
    fake = tmp_path / "stack_doctor.py"
    fake.write_text(
        "def check_thing(probe):\n"
        '    name = "a block"\n'
        "    if probe:\n"
        "        return CheckResult(name, True)\n"
        '    return CheckResult("another block", False)\n'
        "def check_ignored(probe):\n"
        '    return CheckResult("not on the stack", True)\n'
        "def check_stack(config):\n"
        "    return [check_thing(None)]\n", encoding="utf-8")
    assert live_doctor_blocks(fake) == {"a block", "another block"}


def test_doc_lint_reaches_the_monorepo_root():
    """The engine suite runs from skills/superlooper; the ops docs it lints live above it."""
    assert (_REPO / "plugin" / "skills").is_dir()
    assert (_REPO / ".git").exists()
    assert sys.version_info[0] == 3


def test_the_lint_core_ships_with_the_engine():
    """``lib/doc_lint.py`` is publishable payload, not a test fixture.

    ``superlooper upkeep`` imports it on a machine that may have no checkout of this repo at all,
    so it has to live under ``skill/lib`` and import cleanly with nothing but the stdlib and its
    sibling ``labels``.
    """
    assert Path(doc_lint.__file__).parent == _ENGINE / "skill" / "lib"
