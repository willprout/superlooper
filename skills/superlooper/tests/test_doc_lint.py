"""Doc-lint against a LIVE manifest generated from the code (issue #199, defect class D12).

D12 named doc drift a root cause of lost nights: "ops docs name dead verbs, a sync orphaned the
installed docs, the debugger playbook wasn't installed on the machine having the incident". The
first two thirds of that are what this file mechanises. An operator — or one of the owner's helper
agents, which is the case that actually hurt — reaches for an ops doc mid-incident and acts on a
name the running system no longer has. Nothing errors; the recovery just goes wrong.

The cure is a manifest that is GENERATED, never written:

  * **verbs** — the subcommands the CLI's argparse really registers, read out of
    ``skill/bin/superlooper``'s ``add("<verb>", ...)`` calls by AST (not regex: an ``add(`` in a
    comment or a docstring must not create a phantom verb).
  * **labels** — ``labels.LABELS``, imported. The one source of truth for the loop's §C.2 label
    vocabulary, so importing it is definitionally current. Retired names (``needs-william`` ->
    ``needs-owner``) come from ``labels.RETIRED_LABELS``, the same module's own migration record.
  * **doctor block names** — every ``CheckResult`` name in the functions ``check_stack`` calls
    DIRECTLY, read by AST so BRANCH-ONLY names count too. One call level, not transitively: a name
    moved down into a helper would drop out of the manifest, which is why
    ``test_the_static_and_dynamic_block_readings_agree_on_what_ships`` cross-checks it against a
    real ``check_stack`` call. This static read is otherwise stronger than the dynamic
    ``_emitted_block_names`` check in ``test_stack_doctor.py`` (issue #142), which sees only the
    names one FakeProbe machine-state happens to emit. Both stay: the dynamic one proves the names
    a real call produces, this one proves the names the code can produce at all.
  * **repo paths** — a doc that cites `skills/…`, `plugin/…`, `dashboard/…`, `.superlooper/…` or
    `.github/…` is naming a file in THIS repo, so the file has to be there. This is the "a sync
    orphaned the installed docs" third of D12: the reference outlives the move.

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
  * **Invented BARE labels** (`needs-attention`) — see ``documented_labels``.
  * **Repo paths under `bin/` and `docs/`** — those two prefixes are ambiguous in these docs (they
    are also payload- and installed-home-relative), so citations of `bin/install.sh` and
    `docs/ADOPTING.md` go unchecked. See ``_REPO_TOP_DIRS``.

Three properties keep the lint from rotting into decoration:

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
import ast
import re
import sys
from pathlib import Path

import pytest

import labels as labels_mod
import stack_doctor

_ENGINE = Path(__file__).resolve().parent.parent          # skills/superlooper
_REPO = Path(__file__).resolve().parents[3]               # the monorepo root
_CLI = _ENGINE / "skill" / "bin" / "superlooper"
_STACK_DOCTOR = _ENGINE / "skill" / "lib" / "stack_doctor.py"
_STACK_MD = _ENGINE / "docs" / "STACK.md"

_CHECK_NAMES_HEADING = "## Check Names And Fixes"


# --------------------------------------------------------------------------------------------
# The manifest: generated from the code, never written down.
# --------------------------------------------------------------------------------------------

def live_verbs(cli_path=None):
    """Every subcommand the CLI registers, from its ``add("<verb>", fn, ...)`` calls.

    AST, not a regex over the source: ``superlooper``'s module docstring is itself a help table
    full of verb names, and the file is thick with prose comments. A textual scan would mint
    phantom verbs out of either. Walking the tree means only a real call expression counts.

    Scoped to BARE ``add(...)`` calls — the local helper ``main()`` defines to register a
    subparser — and never to ``<something>.add(...)``. The CLI is full of set-building
    (``seen.add(fp)``, ``out.add(name)``); those take non-literals today, but one future
    ``some_set.add("foo")`` would otherwise silently mint a verb ``foo`` that the lint would then
    accept in any ops doc. A manifest that can be widened by an unrelated line is not a manifest.

    The converse matters too: a verb registered by some other form — ``sub.add_parser("x")``
    directly, or an alias table — drops OUT of the manifest, and the lint then rejects a doc naming
    a perfectly real verb. That failure is loud and points at the doc rather than at the extractor,
    so if a ``superlooper <verb>`` assertion ever fails on a verb that plainly exists, look here.
    """
    src = Path(cli_path or _CLI).read_text(encoding="utf-8")
    found = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "add"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
    return found


def live_labels():
    """The §C.2 label vocabulary, imported from its single source of truth."""
    return {name for name, _color, _desc in labels_mod.LABELS}


def retired_labels():
    """Old label name -> the name that replaced it (``labels.RETIRED_LABELS``).

    A retired name is not simply banned. The runtime still RECOGNISES ``needs-william`` so a repo
    mid-migration keeps working, and runner-ops.md legitimately explains the rename — a doc that
    tells the reader "X was renamed to Y" is doing its job. What must never happen is an ops doc
    describing today's behaviour in the dead name, which is how a helper agent ends up hunting a
    label the runner no longer writes. See ``documented_labels`` for the rule that separates them.
    """
    return dict(labels_mod.RETIRED_LABELS)


def _string_constants_in(func_node):
    """``name = "literal"`` bindings inside one function body — enough to resolve the one
    indirection ``stack_doctor`` actually uses (``name = "superlooper plugin"`` reused across a
    function's several return paths)."""
    consts = {}
    for node in ast.walk(func_node):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    return consts


def live_doctor_blocks(module_path=None):
    """Every block name ``doctor --stack`` can emit, by AST, scoped to ``check_stack``'s callees.

    Scoping matters: ``stack_doctor`` is free to grow ``CheckResult``s that the machine-level
    ``--stack`` run never returns, and STACK.md's list is explicitly about the ``--stack`` blocks.
    So we read the functions ``check_stack`` calls DIRECTLY — one level, not a transitive walk —
    then take every ``CheckResult(...)`` first argument inside them, string literal or a
    locally-bound string name. A block whose name moved into a deeper helper would silently leave
    the manifest; ``test_the_static_and_dynamic_block_readings_agree_on_what_ships`` is the
    backstop that notices.

    Raises rather than guesses if a name cannot be resolved statically: an unresolvable name means
    the lint would quietly stop covering a block, which is the exact failure mode being closed.
    """
    path = Path(module_path or _STACK_DOCTOR)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "check_stack" not in funcs:
        raise AssertionError("%s no longer defines check_stack" % path)

    callees = set()
    for node in ast.walk(funcs["check_stack"]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            callees.add(node.func.id)

    names, unresolved = set(), []
    for fname in sorted(callees & set(funcs)):
        func = funcs[fname]
        consts = _string_constants_in(func)
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CheckResult" and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            elif isinstance(first, ast.Name) and first.id in consts:
                names.add(consts[first.id])
            else:
                unresolved.append("%s:%d" % (fname, getattr(node, "lineno", -1)))
    if unresolved:
        raise AssertionError(
            "doc-lint could not statically resolve a doctor block name at %s — the lint would "
            "silently stop covering that block. Bind the name to a plain string local (the "
            "`name = \"...\"` shape check_superlooper_plugin uses) so it stays readable from "
            "source." % ", ".join(unresolved))
    return names


def manifest():
    """The whole live-verb manifest in one object — what the ops docs are linted against."""
    return {
        "verbs": live_verbs(),
        "labels": live_labels(),
        "retired_labels": retired_labels(),
        "doctor_blocks": live_doctor_blocks(),
    }


# --------------------------------------------------------------------------------------------
# The linted surface: what counts as an operational doc.
# --------------------------------------------------------------------------------------------

# Globbed, so a playbook page added tomorrow is linted tomorrow.
_GLOBBED = ("plugin/skills/**/*.md",)

# Named individually because they do not share a directory with anything globbable.
_NAMED = (
    "skills/superlooper/docs/STACK.md",           # the machine-stack ops doc
    "skills/superlooper/skill/docs/ADOPTING.md",  # the adoption walkthrough, published with the engine
    "README.md",                                  # a stranger's first contact with the verbs
    "dashboard/README.md",                        # the command centre's own operating instructions
)

# Everything under skills/superlooper/docs/ that is NOT operational. These are dated records —
# design decisions, incident forensics, audits, the reliability ledger — whose job is to say what
# was true when they were written. Linting them against today's verb table would demand rewriting
# history. `test_the_linted_doc_set_is_complete_and_real` pins this split so a NEW ops doc dropped
# into that directory cannot slip past the lint unnoticed.
_NON_OPERATIONAL_PREFIXES = ("DESIGN-", "INCIDENT-", "AUDIT-", "RESEARCH-", "SPIKE-", "TODO-",
                             "PLAN-", "DRYRUN-", "MIGRATION-")
_NON_OPERATIONAL_EXACT = ("RELIABILITY-LEDGER.md", "V2-IDEAS.md")


def ops_docs():
    """Every operational doc, repo-relative, sorted."""
    found = set()
    for pattern in _GLOBBED:
        for path in _REPO.glob(pattern):
            found.add(path.relative_to(_REPO).as_posix())
    for rel in _NAMED:
        found.add(rel)
    return sorted(found)


# --------------------------------------------------------------------------------------------
# Reading a doc: only code formatting counts.
# --------------------------------------------------------------------------------------------

def code_spans(text):
    """Every fenced-code line and inline-code span. Real names live in code formatting; prose does
    not — the same discipline ``test_docs_adopting.py`` already uses, so a title like "…into the
    superlooper loop" is not read as a `superlooper loop` command."""
    spans, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append(line)
        else:
            spans.extend(re.findall(r"`([^`]+)`", line))
    return spans


_VERB_RE = re.compile(r"\bsuperlooper\s+([a-z][a-z-]*)")


def documented_verbs(text, doctor_blocks=()):
    """Tokens invoked as ``superlooper <token>`` inside code formatting.

    ``doctor_blocks`` is subtracted, not ignored: ``superlooper plugin`` is a real doctor BLOCK
    NAME, printed by ``doctor --stack``, and it appears in backticks in STACK.md exactly as it is
    printed. Without this the lint would demand a `superlooper plugin` subcommand that must never
    exist.
    """
    blocks = set(doctor_blocks)
    found = set()
    for span in code_spans(text):
        for tok in _VERB_RE.findall(span):
            if ("superlooper " + tok) in blocks:
                continue
            found.add(tok)
    return found


# What the lint recognises as a label claim: a RETIRED name (the whole point of the retired check),
# or a `<family>:<value>` pair whose family is one the live set actually uses. The value must be
# non-empty and concrete — `model:*` and `effort:` are a doc talking about the FAMILY, not claiming
# a specific label exists, and the tokeniser stops at the colon for both.
#
# Bare live names (`parked`, `preserve`, `agent-ready`…) are deliberately NOT enumerated. A
# hand-written list of them would be the one un-generated table in a manifest whose whole claim is
# "generated, never written", and it could not catch anything: a bare name is either live (passes)
# or an ordinary English word the lint has no business ruling on. An invented bare label
# (`needs-attention`) is therefore out of this lint's reach — stated here rather than implied.
_LABEL_TOKEN_RE = re.compile(r"[A-Za-z0-9:\[\]_.-]+")
_LABEL_PAIR_RE = re.compile(r"^[a-z][a-z-]*:[A-Za-z0-9\[\]-]+$")


def label_families(live):
    """The `<family>:` prefixes the live label set actually uses (type, priority, model, effort,
    auto-approved, pre-authorized).

    Derived, not listed — and load-bearing: `/superlooper:write-issue` is a namespaced PLUGIN SKILL
    invocation with the exact shape of a label pair. Restricting to real families means the lint
    reads those as what they are instead of demanding a `superlooper:write-issue` label.
    """
    return {name.split(":", 1)[0] for name in live if ":" in name}


def _label_candidates(span, retired, families):
    for tok in _LABEL_TOKEN_RE.findall(span):
        # Strip sentence punctuation. `.` is in the token class so a dotted string is read as ONE
        # token rather than silently split into a label-shaped fragment — but that means a name
        # ending a sentence arrives as `needs-william.` and would otherwise match nothing. That is
        # the single most common shape in prose, and one edit away from the approval-protocol.md
        # sentence this lint was written to catch. (No live label contains a `.`, and the pair
        # regex's value class excludes it, so nothing legitimate is lost by stripping.)
        tok = tok.strip(".,;:")
        if not tok:
            continue
        if tok in retired:
            yield tok
        elif _LABEL_PAIR_RE.match(tok) and tok.split(":", 1)[0] in families:
            yield tok


def _paragraphs(text):
    """Each block of consecutive non-blank lines, with markdown table ROWS split out singly.

    This is the unit the retired-label carve-out is judged in. A line is too tight — ADOPTING.md's
    honest rename note puts `needs-owner` on the bullet and `needs-william` on its continuation —
    and the whole document is far too loose, since one correct mention anywhere would excuse every
    stale one.

    A table is the case where "block of non-blank lines" is also too loose: runner-ops.md's label
    table is fifteen rows in one block, and one correct `needs-owner` row would excuse a stale name
    in any other row. A table row is a self-contained record and a reader reads it as one, so each
    `|`-led line stands alone.
    """
    blocks, current = [], []

    def flush():
        if current:
            blocks.append("\n".join(current))
            del current[:]

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("|"):
            flush()
            blocks.append(line)
        else:
            current.append(line)
    flush()
    return blocks


def documented_labels(text, live, retired):
    """(unknown, stale_retired) label names a doc claims.

    Read from the WHOLE text, not only code formatting. Verbs need the code-span discipline
    because English is full of verb-shaped phrases; label names are distinctive enough that the
    risk runs the other way — approval-protocol.md's "still parks needs-william" is unbackticked
    prose, and it is exactly the sentence that would send a helper agent hunting a dead label.
    Nothing live can fail this check, so scanning prose costs nothing and catches more.

    ``unknown`` — a `<family>:<value>` pair whose value is not in LABELS, in a family whose values
    are CLOSED. gh refuses to apply a label that does not exist, so a doc naming one hands the
    reader an instruction that cannot work. ``labels.OPEN_LABEL_FAMILIES`` (model, effort) are
    exempt by owner ruling: the runner has no allowlist there, LABELS carries only a starter set,
    and `model:haiku` in a doc is a legitimate example rather than a typo. Failing those would
    redden CI over an instruction that genuinely works — which is how a guard gets deleted instead
    of fixed.

    ``stale_retired`` — a retired name in a paragraph that never names its replacement. The
    carve-out keeps runner-ops.md's honest "(renamed from `needs-william`; `adopt` migrates the old
    label in place)" while failing a bare description of today's behaviour in the dead name.
    """
    live = set(live)
    families = label_families(live) - set(labels_mod.OPEN_LABEL_FAMILIES)
    unknown, stale = set(), set()
    for block in _paragraphs(text):
        for tok in _label_candidates(block, retired, families):
            if tok in live:
                continue
            if tok in retired:
                if retired[tok] not in block:
                    stale.add(tok)
                continue
            unknown.add(tok)
    return unknown, stale


# Repo-relative paths a doc cites. Anchored on the repo top-level directories that are UNAMBIGUOUS
# in these docs, which is five of the eight that exist. `bin/` and `docs/` are deliberately left
# out: the ops docs use both as payload- and installed-home-relative prefixes (`bin/liftoff`,
# `docs/ADOPTING.md`), so including them would flag correct references — at the cost that a
# citation of `bin/install.sh` or `docs/ADOPTING.md` is never existence-checked. A lookbehind keeps
# an INSTALLED path (`~/.claude/skills/superlooper/bin/superlooper`) or a state-home path from being
# read as a repo path; those legitimately do not exist in the tree.
_REPO_TOP_DIRS = ("plugin", "skills", "dashboard", ".superlooper", ".github")
_PATH_RE = re.compile(
    r"(?<![/~\w.-])((?:%s)/[A-Za-z0-9_./-]*[A-Za-z0-9_-])"
    % "|".join(re.escape(d) for d in _REPO_TOP_DIRS))


def documented_repo_paths(text):
    """Repo-relative paths cited in code formatting OR in a markdown link target.

    A trailing `*`/`?` in the source span means the doc is naming a FAMILY of files
    (`skills/superlooper/docs/INCIDENT-*.md`); the character class stops before the wildcard, so we
    look at what follows the match and drop it rather than checking a truncated stem.

    Link targets are included because `[the ladder](skills/superlooper/docs/repair.md)` is a
    citation a reader will actually click, and it carries no backticks — code formatting is the
    right discipline for COMMAND names, not for hyperlinks.
    """
    found = set()
    for span in code_spans(text) + re.findall(r"\]\(([^)\s]+)[^)]*\)", text):
        for match in _PATH_RE.finditer(span):
            rel = match.group(1)
            # The character class stops before a wildcard, so the trailing character is where a
            # glob shows up: `skills/superlooper/docs/INCIDENT-*.md` arrives here as the stem
            # `…/INCIDENT-` with `*` next. That is a doc naming a FAMILY, not a file.
            tail = span[match.end():match.end() + 1]
            if tail in ("*", "?"):
                continue
            found.add(rel)
    return found


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
        "these docs are neither linted nor classified as dated records: %s. Add them to _NAMED if "
        "they are operational, or to the non-operational classification if they are history."
        % stray)


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


def _documented_block_names():
    """The block names bulleted under STACK.md's 'Check Names And Fixes' heading, in doc order."""
    lines = _STACK_MD.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(_CHECK_NAMES_HEADING)
    except ValueError:
        raise AssertionError("STACK.md must keep the %r heading" % _CHECK_NAMES_HEADING)
    names = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        match = re.match(r"- `([^`]+)`:", line)
        if match:
            names.append(match.group(1))
    return names


# Backticked Tier bullets in STACK.md that are deliberately NOT doctor block names. Empty today:
# every one of them is a live block. An explicit list is the point — adding a non-block bullet is
# then a conscious one-line edit with a reason, rather than something a shape heuristic waves
# through. (An earlier version guessed with `name.islower()`, which was deaf to `gh API headroom`,
# `codex CLI` and `cmux App Nap disabled` — one of issue #142's own three blocks — and would have
# false-flagged a future `` `conflict_cap` `` bullet.)
_TIER_NON_BLOCK_BULLETS = ()


def _tier_bullet_names():
    """Backticked names bulleted in STACK.md's Tier sections, written ``- `name` - description``.

    A SECOND place the same names are spelled. Renaming a block and updating only the fixes list
    leaves these stale, and the fixes-list check cannot see it — the two lists use different bullet
    punctuation, which is exactly why that drift would survive. Prose-heading bullets (`Publish
    discipline`, `Repo-level doctor green`) carry no backticks, so the regex never returns them.

    Scoped to the Tier sections proper (first ``## Tier`` heading onward), not the whole preamble:
    the intro prose is about the doctor's behaviour rather than its block list, and a backticked
    bullet there would be flagged as a phantom block it never claimed to be.
    """
    lines = _STACK_MD.read_text(encoding="utf-8").splitlines()
    try:
        end = lines.index(_CHECK_NAMES_HEADING)
    except ValueError:
        raise AssertionError("STACK.md must keep the %r heading" % _CHECK_NAMES_HEADING)
    start = next((i for i, line in enumerate(lines) if line.startswith("## Tier")), None)
    assert start is not None, "STACK.md must keep its '## Tier ...' sections"
    return [m.group(1) for m in
            (re.match(r"- `([^`]+)` - ", line) for line in lines[start:end]) if m]


def test_stack_md_tier_lists_name_no_block_the_code_cannot_emit():
    """The fixes list is not the only place STACK.md spells block names.

    A Tier bullet naming a block that no longer exists is the same lie in a different section, and
    it is the section an operator reads FIRST — the tiers are the "what does this machine need"
    walkthrough; the fixes list is what they turn to after a red line.
    """
    live = live_doctor_blocks()
    named = _tier_bullet_names()
    assert named, "STACK.md's Tier lists must bullet the block names in backticks"
    phantom = sorted({n for n in named if n not in live and n not in _TIER_NON_BLOCK_BULLETS})
    assert not phantom, (
        "STACK.md's Tier lists name blocks doctor --stack cannot emit: %s — the tiers are the "
        "first thing an operator reads. If one of these is deliberately not a block name, add it "
        "to _TIER_NON_BLOCK_BULLETS with a reason." % phantom)


def test_stack_md_documents_every_block_name_the_code_can_emit():
    """Completeness, direction one: the doctor grew a block, the doc did not.

    This is issue #142's defect (three blocks emitted, none listed) made un-repeatable from the
    static side. ``test_stack_doctor.py`` pins the same claim dynamically; the two disagree only
    when a name exists in code but no FakeProbe path reaches it, which is precisely the drift this
    static read is here to catch.
    """
    live = live_doctor_blocks()
    documented = set(_documented_block_names())
    assert documented, "STACK.md's %r section must bullet the block names" % _CHECK_NAMES_HEADING
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
    live = live_doctor_blocks()
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
    missed = sorted(emitted - live_doctor_blocks())
    assert not missed, (
        "check_stack emits block names the static manifest does not see: %s — the AST reader is "
        "blind to them and the doc lint silently stopped covering them." % missed)


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

    `.` has to be inside the token class (a label value may carry one), so `needs-william.` used to
    match nothing at all — and the approval-protocol.md sentence this lint was written to catch was
    one edit away from that exact form.
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
    with pytest.raises(AssertionError, match="could not statically resolve"):
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
