"""Permanent mechanical fence: the engine never APPLIES a label it never REGISTERED (issue #337).

`gh` refuses to put a label on an issue unless the repo already has that label. So a label spelled
in engine code but absent from ``lib/labels.py``'s LABELS is not a cosmetic gap — it is a step the
runner can never complete. And because every label move in the runner is written to RETRY (a gh blip
must never lose a hand-back), an unregistered label does not fail loudly: it fails **silently, every
tick, forever**.

That is exactly what happened on 2026-08-04. The #163 question hand-back writes ``awaiting-answer``
in ``_exec_post_question``; nothing ever put it in LABELS, so ``adopt`` never created it and the
#160 boot migration never healed it. Issue #310 sat frozen for ~9 hours emitting
``label move failed (will retry silently next tick)`` — the owner's answer un-ingestable the whole
time, because the settle that leaves ``awaiting_answer`` runs only AFTER the label move lands. A
supervised ``gh label create`` was the only thing that unfroze it.

This is the second occurrence of the defect class issue #165 named: **the suite cannot see it.**
Every test sets labels in Python — the fakes hold a list of strings and append to it — so a label
gh would refuse looks identical to one it would accept. A green suite proved nothing, twice. The
fix for the class cannot be another behavioural test; it has to be a structural one that reads the
SOURCE and asks "is every label this code applies in the vocabulary?".

Two layers, because a label literal can hide two ways:

1. **The vocabulary check.** Every label name the engine passes to a gh call that APPLIES labels
   (``set_labels(add=…)``, ``pr_add_labels``, ``create_issue(labels=…)``) — and every label in an
   action/payload dict that feeds one (``{"act": "relabel", "add": […]}``, ``{"labels": […]}``) —
   must be a name in ``labels.LABELS``. Names are resolved through module-scope constants, in this
   module and across modules (``gate.RESTORE_GREEN_LABEL``, ``FIX_ISSUE_LABELS``), because the
   cheapest way past a literal scan is to put the string in a constant one file over.

   REMOVALS are checked against a wider set: ``LABELS`` plus ``labels.RETIRED_LABELS``. Removing
   the dead ``needs-william`` alongside the live ``needs-owner`` is deliberate (a repo mid-#58
   migration must clear cleanly), so the retired vocabulary is legitimate on the remove side and
   only on the remove side.

2. **The unreadable-argument ratchet.** A label the fence cannot resolve is the fence's blind spot,
   so an unresolvable argument is not waved through — it must appear in ``_UNREADABLE`` below with a
   written reason. Today there are exactly two, both genuinely dynamic dispatchers whose literals
   are checked at the sites that BUILD their payload (the relabel action dicts, the fix-issue label
   lists). A new dynamic label write has to add itself here first, which puts a human in the loop at
   review time — the same discipline ``test_one_publish_door.py`` uses for the publish door.

**Scanned surface: the engine only** (``skills/superlooper/skill/**``, by extension and by shebang,
because the CLI ``skill/bin/superlooper`` carries none). This is deliberate and worth stating,
because the alternative was tried and hurt: the one-doorway fence scans the whole repo, and an
engine-area allow-list on a whole-repo scan is what blocked issue #310's dashboard work. A fence
filed under ``touches: engine`` guards the engine.

So the dashboard is out of reach here, and it defends the same class differently — its verbs
create-or-force a label (``--force``) immediately before applying it, which is why its own ``flag``
label works on a repo that never adopted. That is a real defense, not a gap this fence should paper
over by growing an exemption for another area's file.

The ``test_fence_flags_*`` meta-tests construct each evasion from synthetic source and assert the
fence catches it, so this guard cannot rot into a vacuously-green test — the failure mode that makes
structural guards worthless. ``test_the_fence_would_have_caught_the_2026_08_04_freeze`` runs it
against the REAL engine tree with ``awaiting-answer`` withheld from the vocabulary, which is the
only proof that matters: on the day of the incident, this would have been red.
"""
import ast
import os
import subprocess
from pathlib import Path

import labels as labels_mod

# tests/test_label_vocabulary.py -> tests -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_ENGINE = "skills/superlooper/skill"

# gh functions that APPLY labels, and where each one's label argument sits: {name: (kwarg, index)}.
# The index is the positional fallback — `set_labels(num, ["x"])` is the same write as
# `set_labels(num, add=["x"])`, and a fence that only understood the keyword form would miss it.
_APPLY_CALLS = {
    "set_labels": ("add", 1),
    "pr_add_labels": ("labels", 1),
    "create_issue": ("labels", 2),
}
# ...and the one that REMOVES them. Checked against the wider vocabulary (see the module docstring).
_REMOVE_CALLS = {
    "set_labels": ("remove", 2),
}

# Dict-literal payloads that reach one of the calls above. The runner's executors are fed by pure
# planners, so the label literal often lives in a dict in `lib/actions.py` and never appears at the
# gh call at all — `_exec_relabel` just forwards `a.get("add")`.
_ACT_APPLY_KEYS = ("add", "labels")            # in an action/issue-payload dict: applies labels
_ACT_REMOVE_KEYS = ("remove",)                 # in an action dict: removes labels

# Every label argument the fence CANNOT resolve, and why it is safe. An entry is
# (repo-relative path, the unparsed expression) -> reason. Keying on the expression rather than a
# line number keeps the entry stable when code moves but invalidates it the moment the code changes.
_UNREADABLE = {
    (_ENGINE + "/bin/runner.py", "a.get('add') or []"):
        "The relabel executor is a pure forwarder: every label it can ever write is a literal in "
        "the `{'act': 'relabel', 'add': [...]}` dicts lib/actions.py builds, and those ARE scanned "
        "by layer 1. Resolving it here would mean interpreting the action bus.",
    (_ENGINE + "/bin/runner.py", "a.get('remove') or []"):
        "The remove half of the same forwarder, covered the same way — the literals are in the "
        "relabel action dicts lib/actions.py builds, which layer 1 scans.",
    (_ENGINE + "/bin/runner.py", "a.get('labels')"):
        "The file-an-issue executor forwards whatever the planner put in the payload; the label "
        "lists themselves are literals in lib/actions.py (FIX_ISSUE_LABELS) and lib/nightly.py "
        "(NIGHTLY_FIX_LABELS), both resolved and checked by layer 1.",
    (_ENGINE + "/bin/superlooper", "issue['labels']"):
        "The nightly auto-file forwards the payload lib/nightly.py's fix_issue() built; that "
        "payload is a dict literal carrying NIGHTLY_FIX_LABELS, which layer 1 resolves and checks "
        "at its own site.",
    (_ENGINE + "/bin/superlooper", "[p['label']]"):
        "The janitor's add-label repair applies a label its PLAN computed, so no literal exists "
        "here at all. The plan can only ever compute `type:<kind>` for kind in issues.VALID_TYPES "
        "(lib/janitor.py builds it from queue_lint's type choices) — pinned by "
        "test_every_type_label_the_janitor_can_compute_is_registered below.",
}

_SCRIPT_SUFFIXES = (".py",)
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}


# --------------------------------------------------------------- the scanned surface

def _tracked_engine_files():
    """Repo-relative paths git tracks under the engine, or a pruned walk when there is no git."""
    try:
        out = subprocess.run(["git", "-C", str(_REPO), "ls-files", "-z", "--", _ENGINE],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            return sorted(p for p in out.stdout.split("\0") if p)
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for root, dirs, files in os.walk(_REPO / _ENGINE):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            found.append((Path(root) / name).relative_to(_REPO).as_posix())
    return sorted(found)


def _is_python(path, rel):
    if rel.endswith(_SCRIPT_SUFFIXES):
        return True
    try:                                        # the engine CLI carries no extension at all
        with open(path, "rb") as f:
            return "python" in f.readline(256).decode("utf-8", "replace")
    except OSError:
        return False


def _surface():
    """(relpath, source-text) for every Python file in the engine payload."""
    out = []
    for rel in _tracked_engine_files():
        path = _REPO / rel
        if path.is_file() and _is_python(path, rel):
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
    return out


# --------------------------------------------------------------- resolving names to labels

def _own_scope_nodes(scope):
    """Every node under ``scope`` that belongs to ``scope`` — nested defs are their own scopes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _scope_bindings(scope, inherited):
    """``{name: [value-node, …]}`` for this scope, seeded with the enclosing scopes' bindings.

    Scope-aware on purpose. A flat file-wide map would be simpler and wrong in the direction that
    hurts: ``runner.py`` binds a local called ``label`` in two unrelated functions — the park's
    ``"needs-owner" if … else "parked"`` and the boot migration's f-string — and merging them would
    make the park's perfectly readable ternary read as unresolvable, buying an ``_UNREADABLE``
    entry that excuses the wrong code.

    A LIST per name, not the last binding seen, for the same fail-closed reason a ternary yields
    both arms: a name assigned twice in one scope can hold either value at the write, so both are
    checked and one unreadable binding makes the name unreadable. A local binding SHADOWS the
    inherited one outright (that is what shadowing means); only same-scope rebindings union.
    """
    local = {}
    for node in _own_scope_nodes(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local.setdefault(target.id, []).append(node.value)
    out = dict(inherited)
    out.update(local)
    return out


def _module_bindings(text):
    """Module-scope ``NAME = …`` bindings, as value NODES (not evaluated values).

    Nodes rather than ``literal_eval``, because the constants that matter are not literals:
    ``FIX_ISSUE_LABELS = ["type:diagnose-and-fix", "agent-ready", gate.RESTORE_GREEN_LABEL,
    "expedite"]`` mixes literals with a name from another module, and ``literal_eval`` throws the
    whole list away. Keeping the node lets the resolver follow that last element across the import.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    return _scope_bindings(tree, {})


def _module_stem(rel):
    return Path(rel).stem


def _resolve(node, own, across, seen=None):
    """The label names an expression yields, or None when the fence cannot read it.

    ``[]``/``None`` resolve to the empty list (a real, readable "no labels"). Names, dotted module
    constants (``gate.RESTORE_GREEN_LABEL``), transparent ``list(...)`` wrappers, ``a or []``
    fallbacks and ``x if c else y`` ternaries all resolve through — a ternary yields BOTH arms,
    because both are labels the code can write. Anything else — a subscript, a format string, a
    comprehension, any other call — is UNREADABLE and lands on layer 2 rather than passing quietly.
    """
    seen = seen if seen is not None else set()
    if node is None:
        return []
    if id(node) in seen:
        return None                             # a binding cycle: refuse rather than recurse
    seen = seen | {id(node)}
    if isinstance(node, ast.Constant):
        if node.value is None:
            return []
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return _union(node.elts, own, across, seen)
    if isinstance(node, ast.Name):
        if node.id not in own:
            return None
        return _union(own[node.id], own, across, seen)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        other = across.get(node.value.id)
        if not other or node.attr not in other:
            return None
        # resolved in the OTHER module's scope — a constant there refers to that module's names
        return _union(other[node.attr], other, across, seen)
    if isinstance(node, ast.Call):
        # `list(FIX_ISSUE_LABELS)` copies a list; it does not change what is in it.
        if isinstance(node.func, ast.Name) and node.func.id in ("list", "tuple", "sorted"):
            return _resolve(node.args[0], own, across, seen) if len(node.args) == 1 else None
        return None
    if isinstance(node, ast.BoolOp):
        # `a.get("add") or []` — readable only if BOTH sides are; a readable fallback proves nothing
        # about the value it falls back FROM.
        return _union(node.values, own, across, seen)
    if isinstance(node, ast.IfExp):
        return _union([node.body, node.orelse], own, across, seen)
    return None


def _union(nodes, own, across, seen):
    names = []
    for node in nodes:
        got = _resolve(node, own, across, seen)
        if got is None:
            return None
        names.extend(got)
    return names


# --------------------------------------------------------------- finding the label writes

def _arg(node, kwarg, index):
    """The label argument of a call: the named keyword, else the positional slot.

    A ``**payload`` splat is returned only when no explicit keyword matched — it is unreadable by
    construction, so it lands on layer 2 rather than being mistaken for "this call passes no
    labels", which is what would happen if the splat were simply ignored.
    """
    splat = None
    for kw in node.keywords:
        if kw.arg == kwarg:
            return kw.value
        if kw.arg is None:
            splat = kw.value
    if len(node.args) > index:
        return node.args[index]
    return splat


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dict_items(node):
    """``{"add": [...]}`` -> {"add": <value node>}, string keys only."""
    out = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
    return out


def _is_label_payload(items):
    """Is this dict literal one that carries labels to a gh write?

    Two shapes, both narrow on purpose: a relabel ACTION (the runner's `_exec_relabel` forwards its
    add/remove straight to `set_labels`), and an issue-creation payload (`{"title": …, "body": …,
    "labels": […]}` — what `_exec_file_issue` forwards to `create_issue`). A bare `"labels"` key is
    NOT enough: parsed GitHub views carry one too, and reading those as writes would make the fence
    cry wolf over data it only ever reads.
    """
    act = items.get("act")
    if isinstance(act, ast.Constant) and act.value == "relabel":
        return True
    return "labels" in items and "title" in items and "body" in items


def _label_sites(rel, text, across):
    """Every label-write site in one module: (kind, expression-source, resolved-names-or-None).

    ``kind`` is "apply" (the label must EXIST for gh to attach it — the #337 defect) or "remove"
    (retired names are legitimate here). Walked scope by scope so each site is resolved against the
    bindings actually visible where it is written.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    sites = []
    _walk_scope(tree, {}, across, sites)
    return sites


def _walk_scope(scope, inherited, across, sites):
    own = _scope_bindings(scope, inherited)

    def record(kind, node):
        if node is None:
            return
        sites.append((kind, ast.unparse(node), _resolve(node, own, across)))

    for node in _own_scope_nodes(scope):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _APPLY_CALLS:
                record("apply", _arg(node, *_APPLY_CALLS[name]))
            if name in _REMOVE_CALLS:
                record("remove", _arg(node, *_REMOVE_CALLS[name]))
        elif isinstance(node, ast.Dict):
            items = _dict_items(node)
            if _is_label_payload(items):
                for key in _ACT_APPLY_KEYS:
                    record("apply", items.get(key))
                for key in _ACT_REMOVE_KEYS:
                    record("remove", items.get(key))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _walk_scope(node, own, across, sites)


def _scan(surface):
    """(unregistered, unreadable) over a whole surface of (relpath, text) pairs."""
    across = {_module_stem(rel): _module_bindings(text) for rel, text in surface}
    registered = {name for name, _color, _desc in labels_mod.LABELS}
    removable = registered | set(labels_mod.RETIRED_LABELS)
    unregistered, unreadable = [], []
    for rel, text in surface:
        for kind, src, names in _label_sites(rel, text, across):
            if names is None:
                unreadable.append((rel, src))
                continue
            allowed = registered if kind == "apply" else removable
            for name in names:
                if name not in allowed:
                    unregistered.append((rel, kind, name, src))
    return unregistered, unreadable


# --------------------------------------------------------------- layer 1: the vocabulary check

def test_every_label_the_engine_applies_is_in_the_vocabulary():
    unregistered, _unreadable = _scan(_surface())
    assert not unregistered, (
        "the engine writes these labels, but labels.LABELS does not carry them: %s.\n"
        "gh REFUSES to apply a label the repo does not have, and every label move in the runner "
        "retries silently — so this does not fail loudly, it freezes the issue (2026-08-04, #310, "
        "~9h). Register the label in skill/lib/labels.py with a colour and a description; tag the "
        "description '(runner-managed)' if the RUNNER writes it, so the #160 boot migration heals "
        "it on repos adopted before this shipped." % (unregistered,)
    )


def test_the_fence_would_have_caught_the_2026_08_04_freeze():
    # The proof that this guard is worth having: run it against the REAL engine tree with
    # `awaiting-answer` withheld from the vocabulary — the exact state of main until #337 — and it
    # must name the label and the file. If this ever passes, the fence has stopped reading the
    # source that froze #310.
    real = labels_mod.LABELS
    try:
        labels_mod.LABELS = [row for row in real if row[0] != "awaiting-answer"]
        unregistered, _unreadable = _scan(_surface())
    finally:
        labels_mod.LABELS = real
    hits = [(rel, name) for rel, kind, name, _src in unregistered
            if kind == "apply" and name == "awaiting-answer"]
    assert hits, ("with awaiting-answer withheld the fence must flag the question hand-back — "
                  "that is the bug #337 was filed for")
    assert any(rel.endswith("bin/runner.py") for rel, _name in hits)


# --------------------------------------------------------------- layer 2: the unreadable ratchet

def test_every_unreadable_label_argument_is_accounted_for():
    _unregistered, unreadable = _scan(_surface())
    strays = [site for site in unreadable if site not in _UNREADABLE]
    assert not strays, (
        "these label arguments cannot be resolved to names, so layer 1 cannot check them: %s.\n"
        "If the labels are literals somewhere layer 1 already reads (an action dict, a module "
        "constant), add the site to _UNREADABLE in this file with that reason. Otherwise spell the "
        "labels as literals or a module constant, so the vocabulary check can see them." % (strays,)
    )


def test_the_unreadable_allow_list_has_not_rotted():
    # A stale entry silently widens the ratchet: the code it excused moves or changes, the entry
    # keeps sitting here, and the next dynamic write inherits an exemption nobody granted.
    _unregistered, unreadable = _scan(_surface())
    stale = [site for site in _UNREADABLE if site not in unreadable]
    assert not stale, ("these _UNREADABLE entries no longer match any site — the code changed; "
                       "re-read it and delete or re-justify the entry: %s" % (stale,))
    reasonless = [site for site, why in _UNREADABLE.items() if len(why.strip()) < 40]
    assert not reasonless, "every _UNREADABLE entry needs a real reason: %s" % (reasonless,)


def test_every_type_label_the_janitor_can_compute_is_registered():
    """The one label the engine BUILDS rather than spells (issue #229's add-label repair).

    ``lib/janitor.py`` plans ``"type:%s" % kind`` for every kind ``queue_lint`` offers, and
    ``queue_lint.VALID_TYPES`` is ``issues.VALID_TYPES`` — so the closed set of labels that repair
    can ever apply is derivable, even though the string itself never appears in the source. Adding
    a fourth issue kind without a matching ``type:`` label would give the owner a one-tap repair
    that gh refuses: this same defect, reached from the other end. That is why the ``[p['label']]``
    site may sit on the unreadable ratchet — this test, not the ratchet, is what covers it.
    """
    import issues as issues_mod
    import janitor as janitor_mod
    import queue_lint as queue_lint_mod

    registered = {name for name, _color, _desc in labels_mod.LABELS}
    assert queue_lint_mod.VALID_TYPES is issues_mod.VALID_TYPES, (
        "the lint's kinds must stay the engine's own kinds, or this check reads the wrong list")
    assert janitor_mod.queue_lint is queue_lint_mod, (
        "the janitor must build its type: choices from this lint, or this check reads the wrong list")
    missing = ["type:%s" % kind for kind in issues_mod.VALID_TYPES
               if "type:%s" % kind not in registered]
    assert not missing, (
        "the janitor's add-label repair can compute these labels, but labels.LABELS does not carry "
        "them: %s — the owner would tap a repair gh refuses" % (missing,))


def test_the_scanned_surface_covers_the_extensionless_cli():
    # The engine's own CLI has no `.py` suffix, and it is where `adopt` creates the label set. An
    # extension-only sweep would leave the most label-dense file in the engine outside the fence.
    scanned = {rel for rel, _text in _surface()}
    for rel in (_ENGINE + "/bin/superlooper", _ENGINE + "/bin/runner.py",
                _ENGINE + "/lib/actions.py"):
        assert rel in scanned, "%s must be on the scanned surface" % rel


# --------------------------------------------------------------- meta-tests: the fence bites

# The meta-tests deliberately do NOT use `awaiting-answer` as their villain: it is registered now,
# so a fence that stopped working would go quietly green. This name is one nobody will ever add to
# the vocabulary, and `test_the_meta_tests_villain_is_really_unregistered` says so out loud.
_UNKNOWN = "sl-337-never-registered"

_LITERAL = """import gh
def hand_back(num):
    gh.set_labels(num, add=[%(x)r], remove=["in-progress"])
""" % {"x": _UNKNOWN}

_POSITIONAL = """import gh
def hand_back(num):
    gh.set_labels(num, [%(x)r])
""" % {"x": _UNKNOWN}

_VIA_CONSTANT = """import gh
AWAITING = %(x)r
def hand_back(num):
    gh.set_labels(num, add=[AWAITING], remove=["in-progress"])
""" % {"x": _UNKNOWN}

_VIA_OTHER_MODULE = """import gh, vocab
def hand_back(num):
    gh.set_labels(num, add=[vocab.AWAITING], remove=["in-progress"])
"""

_VOCAB_MODULE = """AWAITING = %(x)r
""" % {"x": _UNKNOWN}

_VIA_ACTION_DICT = """def plan(iid, num):
    return [{"act": "relabel", "id": iid, "num": num,
             "add": [%(x)r], "remove": ["in-progress"]}]
""" % {"x": _UNKNOWN}

_VIA_ISSUE_PAYLOAD = """FIX_LABELS = ["type:build", %(x)r]
def plan():
    return {"act": "file_issue", "title": "t", "body": "b", "labels": list(FIX_LABELS)}
""" % {"x": _UNKNOWN}

_VIA_PR_LABEL = """import gh
def supersede(pr):
    gh.pr_add_labels(pr, [%(x)r])
""" % {"x": _UNKNOWN}

_VIA_TERNARY = """import gh
def park(num, needs_owner):
    label = "parked" if not needs_owner else %(x)r
    gh.set_labels(num, add=[label], remove=["in-progress"])
""" % {"x": _UNKNOWN}

_SHADOWED_LOCAL = """import gh
def unrelated(step):
    label = "%%s->%%s" %% (step["old"], step["new"])
    return label
def park(num):
    label = "parked"
    gh.set_labels(num, add=[label], remove=["in-progress"])
"""

_UNREADABLE_SRC = """import gh
def forward(num, payload):
    gh.set_labels(num, add=payload["add"], remove=payload["remove"])
"""

_CLEAN = """import gh
PARK = "parked"
def park(num):
    gh.set_labels(num, add=[PARK], remove=["in-progress", "agent-ready"])
    gh.set_labels(num, add=[], remove=None)
"""

_RETIRED_ON_REMOVE = """import gh
def reapprove(num):
    gh.set_labels(num, add=["in-progress"], remove=["needs-owner", "needs-william"])
"""

_READ_ONLY_VIEW = """def parse(view):
    return {"num": view.get("number"), "labels": view.get("labels") or []}
"""


def _scan1(text, extra=None):
    surface = [("skills/superlooper/skill/lib/probe.py", text)]
    surface += list(extra or [])
    return _scan(surface)


def _flagged(text, extra=None):
    unregistered, _unreadable = _scan1(text, extra)
    return {name for _rel, _kind, name, _src in unregistered}


def test_fence_flags_a_bare_unregistered_literal():
    assert _UNKNOWN in _flagged(_LITERAL), (
        "the shape the 2026-08-04 freeze actually had must be caught")


def test_fence_reads_the_positional_form_too():
    assert _UNKNOWN in _flagged(_POSITIONAL), (
        "set_labels(num, [...]) is the same write as set_labels(num, add=[...])")


def test_fence_follows_a_label_hidden_in_a_constant():
    assert _UNKNOWN in _flagged(_VIA_CONSTANT), (
        "the cheapest way past a literal scan is a module constant — it must resolve")


def test_fence_follows_a_label_hidden_in_another_module():
    extra = [("skills/superlooper/skill/lib/vocab.py", _VOCAB_MODULE)]
    assert _UNKNOWN in _flagged(_VIA_OTHER_MODULE, extra), (
        "a constant one file over (gate.RESTORE_GREEN_LABEL is the real case) must resolve")


def test_fence_reads_the_action_dicts_that_feed_the_executor():
    # The literals that matter often never appear at the gh call: the runner's relabel executor is a
    # forwarder, and the names live in the pure planner's action dicts.
    assert _UNKNOWN in _flagged(_VIA_ACTION_DICT)


def test_fence_reads_an_issue_creation_payload():
    assert _UNKNOWN in _flagged(_VIA_ISSUE_PAYLOAD), (
        "a label list carried into create_issue must be checked, list() wrapper and all")


def test_fence_reads_a_pr_label_write():
    assert _UNKNOWN in _flagged(_VIA_PR_LABEL)


def test_fence_reads_both_arms_of_a_ternary():
    # The real shape in `_exec_park`: `label = "needs-owner" if … else "parked"`. Both arms are
    # labels the code can write, so both must be checked — reading only one is how a fence gives a
    # false all-clear on the branch nobody exercised.
    assert _UNKNOWN in _flagged(_VIA_TERNARY)


def test_fence_resolves_names_per_scope_not_per_file():
    # `runner.py` really does bind a local called `label` in two unrelated functions — one to a
    # literal, one to an f-string. A file-wide name map would make the readable one unresolvable,
    # and the fix for THAT would be an _UNREADABLE entry excusing code the fence can read fine.
    unregistered, unreadable = _scan1(_SHADOWED_LOCAL)
    assert not unregistered and not unreadable, (
        "a same-named local in an unrelated function must not blind the fence: %s %s"
        % (unregistered, unreadable))


def test_the_meta_tests_villain_is_really_unregistered():
    # If someone ever registers this name, every meta-test above starts passing vacuously and the
    # fence rots into decoration. Say it out loud so that day is a red test, not a silent one.
    assert _UNKNOWN not in {name for name, _c, _d in labels_mod.LABELS}


def test_fence_refuses_to_wave_through_an_unreadable_argument():
    _unregistered, unreadable = _scan1(_UNREADABLE_SRC)
    assert len(unreadable) == 2, (
        "a subscripted add/remove is the fence's blind spot; it must land on the ratchet rather "
        "than pass silently: %s" % (unreadable,))


def test_fence_reads_every_binding_of_a_rebound_name():
    # A name assigned twice in one scope can hold either value at the write, so both are checked.
    # Taking "the last one seen" would make the fence's verdict depend on AST traversal order —
    # green or red for reasons that have nothing to do with the code.
    src = """import gh
def hand_back(num, urgent):
    label = "parked"
    if urgent:
        label = %(x)r
    gh.set_labels(num, add=[label])
""" % {"x": _UNKNOWN}
    assert _UNKNOWN in _flagged(src)


def test_fence_does_not_read_a_kwargs_splat_as_no_labels():
    # `set_labels(num, **payload)` passes labels the fence cannot see. Ignoring the splat would
    # read as "this call applies nothing" — a silent all-clear on the one shape it cannot check.
    src = """import gh
def forward(num, payload):
    gh.set_labels(num, **payload)
"""
    _unregistered, unreadable = _scan1(src)
    assert unreadable, "a **splat must land on the ratchet, not read as an empty add"


def test_fence_stays_quiet_on_registered_labels():
    # The other half of a useful fence: it must not cry wolf, or it gets muted. `add=[]` and
    # `remove=None` are readable "no labels", not unreadable arguments.
    unregistered, unreadable = _scan1(_CLEAN)
    assert not unregistered and not unreadable


def test_fence_allows_a_retired_label_on_the_remove_side_only():
    # Removing the dead needs-william beside the live needs-owner is how a repo mid-#58 migration
    # clears cleanly. It is legitimate to REMOVE and never legitimate to APPLY.
    assert not _flagged(_RETIRED_ON_REMOVE)
    retired = next(iter(labels_mod.RETIRED_LABELS))
    applied = "import gh\ndef go(num):\n    gh.set_labels(num, add=[%r])\n" % retired
    assert retired in _flagged(applied), (
        "a retired name is dead vocabulary — applying it is the 2026-07-13 bounce storm")


def test_fence_does_not_read_a_parsed_view_as_a_label_write():
    # Parsed GitHub views carry a "labels" key too. Reading one as a write would redden the suite
    # over data the engine only ever reads — and a fence that cries wolf gets deleted, not fixed.
    unregistered, unreadable = _scan1(_READ_ONLY_VIEW)
    assert not unregistered and not unreadable
