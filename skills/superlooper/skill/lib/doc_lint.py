"""The operational-doc lint, as a library (issue #199's checks; lifted here for issue #200).

Issue #199 built this as ``tests/test_doc_lint.py`` — a CI guard against defect class D12, where an
ops doc names a verb, label, doctor block or file the running system no longer has, and an operator
(or one of the owner's helper agents, which is the case that actually hurt) acts on it mid-incident.
The checks themselves are pure functions over a repo checkout, and CI is not the only place they are
worth running: ``superlooper upkeep`` reports the lint's verdict as one line of the weekly
once-over, so drift is caught on a Sunday morning rather than by whoever pushes next.

So the checks live HERE and the test module imports them. The tests keep their whole job — the
per-doc parametrization, the completeness assertions and, most importantly, the meta-tests that
build each violation out of synthetic source and prove the lint still fires. Nothing about #199's
guarantees moved; only the address did.

The manifest is GENERATED, never written down:

  * **verbs** — the subcommands the CLI's argparse really registers, read out of the CLI script's
    ``add("<verb>", ...)`` calls by AST (not regex: an ``add(`` in a comment or docstring must not
    mint a phantom verb).
  * **labels** — ``labels.LABELS``, imported. Retired names come from ``labels.RETIRED_LABELS``.
  * **doctor block names** — every ``CheckResult`` name in the functions ``check_stack`` calls
    DIRECTLY, read by AST so BRANCH-ONLY names count too.
  * **repo paths** — a doc citing a path under one of the repo's unambiguous top-level component
    directories (see ``_REPO_TOP_DIRS``) names a file in THIS repo, so the file has to be there.

Every reader takes the repo root as a PARAMETER. That is what lets the same code serve a test that
knows where its checkout is and a CLI that was handed ``--repo``; it is also why nothing here
guesses a path from ``__file__``.

One honest asymmetry, stated rather than hidden: the label vocabulary is IMPORTED from the running
engine's ``labels`` module, while verbs and doctor blocks are read out of the checkout's source. Run
from a source checkout of the same tree (CI, and the dogfood loop) those are the same thing. Run
from an installed engine against a checkout that has moved ahead, the label half describes the
INSTALLED vocabulary — which is the publish drift ``upkeep`` reports on the line above, not a
separate lie. Reading LABELS out of the checkout by AST would trade a legible one-import fact for a
second, weaker parser; the drift line covers it instead.
"""
import ast
import re
from pathlib import Path

import labels as labels_mod

# Where each generated half is read from, repo-relative. Named once so a move is one edit.
CLI_REL = "skills/superlooper/skill/bin/superlooper"
STACK_DOCTOR_REL = "skills/superlooper/skill/lib/stack_doctor.py"
STACK_MD_REL = "skills/superlooper/docs/STACK.md"

CHECK_NAMES_HEADING = "## Check Names And Fixes"

# Globbed, so a playbook page added tomorrow is linted tomorrow. The second pattern picks up the
# operating instructions each top-level COMPONENT keeps beside itself — a stranger's first contact
# with that component's verbs. Discovered rather than listed on purpose: the engine may not name the
# repo's other components (``tests/test_dashboard_agnostic.py`` enforces that one-way dependency),
# and a component added next year is linted the day its README lands.
GLOBBED = ("plugin/skills/**/*.md", "*/README.md")

# Named individually because they do not share a directory with anything globbable.
NAMED = (
    "skills/superlooper/docs/STACK.md",           # the machine-stack ops doc
    "skills/superlooper/skill/docs/ADOPTING.md",  # the adoption walkthrough, published with the engine
    "README.md",                                  # a stranger's first contact with the verbs
)

# Everything under skills/superlooper/docs/ that is NOT operational. These are dated records —
# design decisions, incident forensics, audits, the reliability ledger — whose job is to say what
# was true when they were written. Linting them against today's verb table would demand rewriting
# history.
NON_OPERATIONAL_PREFIXES = ("DESIGN-", "INCIDENT-", "AUDIT-", "RESEARCH-", "SPIKE-", "TODO-",
                            "PLAN-", "DRYRUN-", "MIGRATION-")
NON_OPERATIONAL_EXACT = ("RELIABILITY-LEDGER.md", "V2-IDEAS.md")

# Backticked Tier bullets in STACK.md that are deliberately NOT doctor block names. Empty today:
# every one of them is a live block. An explicit list is the point — adding a non-block bullet is
# then a conscious one-line edit with a reason, rather than something a shape heuristic waves
# through. (An earlier version guessed with `name.islower()`, which was deaf to `gh API headroom`,
# `codex CLI` and `cmux App Nap disabled` — one of issue #142's own three blocks — and would have
# false-flagged a future `` `conflict_cap` `` bullet.)
TIER_NON_BLOCK_BULLETS = ()

# How many findings ``lint`` returns before it says "and N more". A one-page report that scrolls off
# the screen is not a one-page report; a report that truncates without saying so is a lie. Both
# problems are solved by a small number and an explicit remainder count.
MAX_FINDINGS = 8


class UnreadableManifest(Exception):
    """A generated half of the manifest could not be read from source.

    Raised rather than guessed at: an unresolvable name means the lint would quietly stop covering
    something, which is the exact failure mode being closed. The test suite lets it surface as a red
    test; ``lint()`` catches it and reports it as a finding, because a report that crashes is a
    report nobody runs.
    """


# --------------------------------------------------------------------------------------------
# The manifest: generated from the code, never written down.
# --------------------------------------------------------------------------------------------

def live_verbs(cli_path):
    """Every subcommand the CLI registers, from its ``add("<verb>", fn, ...)`` calls.

    AST, not a regex over the source: the CLI's module docstring is itself a help table full of verb
    names, and the file is thick with prose comments. A textual scan would mint phantom verbs out of
    either. Walking the tree means only a real call expression counts.

    Scoped to BARE ``add(...)`` calls — the local helper ``main()`` defines to register a subparser
    — and never to ``<something>.add(...)``. The CLI is full of set-building (``seen.add(fp)``,
    ``out.add(name)``); those take non-literals today, but one future ``some_set.add("foo")`` would
    otherwise silently mint a verb ``foo`` that the lint would then accept in any ops doc. A
    manifest that can be widened by an unrelated line is not a manifest.

    The converse matters too: a verb registered by some other form — ``sub.add_parser("x")``
    directly, or an alias table — drops OUT of the manifest, and the lint then rejects a doc naming
    a perfectly real verb. That failure is loud and points at the doc rather than at the extractor,
    so if a ``superlooper <verb>`` assertion ever fails on a verb that plainly exists, look here.
    """
    src = Path(cli_path).read_text(encoding="utf-8")
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


def live_doctor_blocks(module_path):
    """Every block name ``doctor --stack`` can emit, by AST, scoped to ``check_stack``'s callees.

    Scoping matters: ``stack_doctor`` is free to grow ``CheckResult``s that the machine-level
    ``--stack`` run never returns, and STACK.md's list is explicitly about the ``--stack`` blocks.
    So we read the functions ``check_stack`` calls DIRECTLY — one level, not a transitive walk —
    then take every ``CheckResult(...)`` first argument inside them, string literal or a
    locally-bound string name. A block whose name moved into a deeper helper would silently leave
    the manifest; the test suite's static-vs-dynamic cross-check is the backstop that notices.

    Raises ``UnreadableManifest`` rather than guessing if a name cannot be resolved statically: an
    unresolvable name means the lint would quietly stop covering a block, which is the exact failure
    mode being closed.
    """
    path = Path(module_path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "check_stack" not in funcs:
        raise UnreadableManifest("%s no longer defines check_stack" % path)

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
        raise UnreadableManifest(
            "doc-lint could not statically resolve a doctor block name at %s — the lint would "
            "silently stop covering that block. Bind the name to a plain string local (the "
            "`name = \"...\"` shape check_superlooper_plugin uses) so it stays readable from "
            "source." % ", ".join(unresolved))
    return names


def manifest(repo_root):
    """The whole live-verb manifest in one object — what the ops docs are linted against."""
    root = Path(repo_root)
    return {
        "verbs": live_verbs(root / CLI_REL),
        "labels": live_labels(),
        "retired_labels": retired_labels(),
        "doctor_blocks": live_doctor_blocks(root / STACK_DOCTOR_REL),
    }


# --------------------------------------------------------------------------------------------
# The linted surface: what counts as an operational doc.
# --------------------------------------------------------------------------------------------

def ops_docs(repo_root):
    """Every operational doc, repo-relative, sorted."""
    root = Path(repo_root)
    found = set()
    for pattern in GLOBBED:
        for path in root.glob(pattern):
            found.add(path.relative_to(root).as_posix())
    for rel in NAMED:
        found.add(rel)
    return sorted(found)


def is_source_checkout(repo_root):
    """Does `repo_root` look like a superlooper MONOREPO checkout — the only tree these docs exist
    in? Read-only, path arithmetic only.

    ``upkeep`` runs against whatever repo the owner adopted, and for every repo but this one the
    ops docs are simply not there. That is a clean SKIP, not a finding — the same posture
    ``stack_doctor.engine_drift`` takes when there is no source checkout to compare against.
    """
    root = Path(repo_root)
    return (root / CLI_REL).is_file() and (root / "plugin" / "skills").is_dir()


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
# out: the ops docs use both as payload- and installed-home-relative prefixes (a `bin/<script>`
# invocation, `docs/ADOPTING.md`), so including them would flag correct references — at the cost
# that a citation of `bin/install.sh` or `docs/ADOPTING.md` is never existence-checked. A lookbehind
# keeps an INSTALLED path (`~/.claude/skills/superlooper/bin/superlooper`) or a state-home path from
# being read as a repo path; those legitimately do not exist in the tree.
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


# --------------------------------------------------------------------------------------------
# STACK.md's two block-name lists (the only doc making a completeness claim).
# --------------------------------------------------------------------------------------------

def documented_block_names(repo_root):
    """The block names bulleted under STACK.md's 'Check Names And Fixes' heading, in doc order."""
    lines = (Path(repo_root) / STACK_MD_REL).read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(CHECK_NAMES_HEADING)
    except ValueError:
        raise UnreadableManifest("STACK.md must keep the %r heading" % CHECK_NAMES_HEADING)
    names = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        match = re.match(r"- `([^`]+)`:", line)
        if match:
            names.append(match.group(1))
    return names


def tier_bullet_names(repo_root):
    """Backticked names bulleted in STACK.md's Tier sections, written ``- `name` - description``.

    A SECOND place the same names are spelled. Renaming a block and updating only the fixes list
    leaves these stale, and the fixes-list check cannot see it — the two lists use different bullet
    punctuation, which is exactly why that drift would survive. Prose-heading bullets (`Publish
    discipline`, `Repo-level doctor green`) carry no backticks, so the regex never returns them.

    Scoped to the Tier sections proper (first ``## Tier`` heading onward), not the whole preamble:
    the intro prose is about the doctor's behaviour rather than its block list, and a backticked
    bullet there would be flagged as a phantom block it never claimed to be.
    """
    lines = (Path(repo_root) / STACK_MD_REL).read_text(encoding="utf-8").splitlines()
    try:
        end = lines.index(CHECK_NAMES_HEADING)
    except ValueError:
        raise UnreadableManifest("STACK.md must keep the %r heading" % CHECK_NAMES_HEADING)
    start = next((i for i, line in enumerate(lines) if line.startswith("## Tier")), None)
    if start is None:
        raise UnreadableManifest("STACK.md must keep its '## Tier ...' sections")
    return [m.group(1) for m in
            (re.match(r"- `([^`]+)` - ", line) for line in lines[start:end]) if m]


# --------------------------------------------------------------------------------------------
# The whole lint, in one call — what `superlooper upkeep` reports.
# --------------------------------------------------------------------------------------------

def lint(repo_root):
    """Run every check over `repo_root` and return one verdict dict. NEVER raises.

    ``{"status": "skipped"|"clean"|"findings", "docs": int, "findings": [str], "detail": str}``

    Findings are one human-readable line each, doc-first, so the weekly report can print them
    verbatim. Never raising is the contract that lets ``upkeep`` call this blind: an unreadable doc
    or an unresolvable manifest becomes a FINDING (something to look at), never a traceback that
    takes down the rest of the report — a report that crashes on a bad day is a report nobody runs
    on a good one.

    The findings list is deliberately BOUNDED at ``MAX_FINDINGS``: this is a one-page weekly
    once-over, and a repo-wide rename can produce hundreds. What is dropped is SAID, never silently
    truncated (`detail` carries the remainder count) — an unqualified list reads as "that was all
    of it".
    """
    root = Path(repo_root)
    if not is_source_checkout(root):
        return {"status": "skipped", "docs": 0, "findings": [],
                "detail": "%s is not a superlooper source checkout — the operational docs the lint "
                          "reads live only in the engine's own monorepo." % root}
    findings = []
    try:
        man = manifest(root)
    except (UnreadableManifest, OSError, SyntaxError, ValueError) as exc:
        return {"status": "findings", "docs": 0,
                "findings": ["manifest: %s" % exc],
                "detail": "the lint could not read what the code actually registers, so no doc "
                          "could be checked against it."}

    docs = ops_docs(root)
    for rel in docs:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError as exc:
            findings.append("%s: cannot be read (%s)" % (rel, exc))
            continue
        for verb in sorted(documented_verbs(text, man["doctor_blocks"]) - man["verbs"]):
            findings.append("%s: names `superlooper %s`, which the CLI does not register" %
                            (rel, verb))
        unknown, stale = documented_labels(text, man["labels"], man["retired_labels"])
        for name in sorted(unknown):
            findings.append("%s: names label `%s`, which is not in labels.LABELS" % (rel, name))
        for name in sorted(stale):
            findings.append("%s: describes current behaviour with the retired label `%s` (say `%s`)"
                            % (rel, name, man["retired_labels"][name]))
        for path in sorted(p for p in documented_repo_paths(text) if not (root / p).exists()):
            findings.append("%s: cites `%s`, which does not exist" % (rel, path))

    # STACK.md's completeness claim, both directions — the only doc that makes one.
    try:
        listed = set(documented_block_names(root))
        tiers = set(tier_bullet_names(root))
        for name in sorted(man["doctor_blocks"] - listed):
            findings.append("%s: 'Check Names And Fixes' omits the doctor block `%s`"
                            % (STACK_MD_REL, name))
        for name in sorted(listed - man["doctor_blocks"]):
            findings.append("%s: 'Check Names And Fixes' names `%s`, which doctor --stack cannot "
                            "emit" % (STACK_MD_REL, name))
        for name in sorted(tiers - man["doctor_blocks"] - set(TIER_NON_BLOCK_BULLETS)):
            findings.append("%s: a Tier bullet names `%s`, which doctor --stack cannot emit"
                            % (STACK_MD_REL, name))
    except (UnreadableManifest, OSError) as exc:
        findings.append("%s: %s" % (STACK_MD_REL, exc))

    if not findings:
        return {"status": "clean", "docs": len(docs), "findings": [],
                "detail": "every verb, label, doctor block and repo path the operational docs name "
                          "is live."}
    shown, extra = findings[:MAX_FINDINGS], max(0, len(findings) - MAX_FINDINGS)
    return {"status": "findings", "docs": len(docs), "findings": shown,
            "detail": ("%d more finding(s) not shown — run the doc lint for the full list."
                       % extra) if extra else ""}
