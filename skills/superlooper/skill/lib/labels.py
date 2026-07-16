"""The §C.2 label set and the pure per-repo label migration plan (issues #58/#108/#160).

ONE source of truth for the loop's label vocabulary, shared by the CLI's `adopt`/`doctor` (which
CREATE + verify the whole set) and the runner's boot migration (which self-heals the
runner-managed subset). Keeping it here — a pure lib module — is what lets the runner apply the
label migrations at boot without importing the extensionless CLI script.

`label_migration_plan` is the pure heart of issue #160: given the labels a repo has RIGHT NOW, it
returns the ordered, idempotent steps that close the gap between a migration that has merged +
installed (it lives in this list) and one that has actually been APPLIED to the repo. Re-running
it on an already-migrated repo returns [] — a true no-op. Impure application (the gh writes, the
systemic hold on failure) lives in runner.py; nothing here touches GitHub.
"""

# The §C.2 label set, colors included so a fresh repo reads well at a glance. adopt creates all of
# them idempotently (--force); the runner creates the runner-managed subset at boot (issue #160).
# Runner-managed labels say so in their description — humans learn the loop's vocabulary from the
# label descriptions, AND the '(runner-managed)' tag is what runner_managed_labels() keys off, so
# this list stays the single source of truth. `{operator}` in a description is substituted with the
# configured operator name at write time (issue #58), so a stranger's own labels sign their name.
LABELS = [
    ("agent-ready", "0e8a16", "{operator}'s approval: the runner may launch this issue"),
    ("in-progress", "fbca04", "claimed by a loop session (runner-managed)"),
    ("needs-owner", "d93f0b", "parked for an owner decision (runner-managed)"),
    ("parked", "c2e0c6", "handed back to {operator} with a memo (runner-managed)"),
    ("expedite", "b60205", "queue-bypass lane: the next free lane takes this first"),
    ("preserve", "5319e7", "on a PR: resolve conflicts in-branch instead of regenerating"),
    ("auto-approved:nightly-red", "e99695",
     "standing rule: auto-filed fix for a red nightly/dev (scoped to restoring green)"),
    ("superseded", "cccccc", "on a PR: replaced by a rebuild; branch preserved, nothing auto-closed"),
    ("priority:high", "ff9500", "front of the normal queue"),
    ("priority:low", "0075ca", "back of the queue"),
    ("type:build", "1d76db", "build issue"),
    ("type:investigate", "6f42c1", "investigation issue (report + children, no PR)"),
    ("type:diagnose-and-fix", "0052cc", "diagnose-and-fix issue (scope check first)"),
    # Per-issue control knobs (owner ruling 2026-07-07): William drops one on an issue to run its
    # worker sessions on a specific model/effort. gh refuses to apply a label that doesn't exist, so
    # these are a STARTER set — the runner has no allowlist, so any `model:<x>`/`effort:<x>` label
    # you create and apply works (an unknown value fails the launch loudly and parks the issue).
    ("model:opus", "0366d6", "per-issue worker model: latest Opus (~200K context)"),
    ("model:opus[1m]", "0366d6", "per-issue worker model: latest Opus + 1M context"),
    ("model:fable", "0366d6", "per-issue worker model: Fable"),
    ("model:sonnet", "0366d6", "per-issue worker model: latest Sonnet"),
    ("effort:low", "8256d0", "per-issue worker effort: low"),
    ("effort:medium", "8256d0", "per-issue worker effort: medium"),
    ("effort:high", "8256d0", "per-issue worker effort: high"),
    ("effort:xhigh", "8256d0", "per-issue worker effort: xhigh"),
    ("effort:max", "8256d0", "per-issue worker effort: max"),
]

# name -> (color, description-template). Built once; label_spec resolves a create step's color/desc.
_SPEC = {name: (color, desc) for name, color, desc in LABELS}

# The one label migration the runner knows how to apply beyond creating a missing label: the issue
# #58 rename of `needs-william` -> `needs-owner`. It merged INTO adopt, but a repo adopted before it
# — and never re-adopted after the republish — still carries the OLD label, so every runner
# hand-back that writes the NEW label fails and re-notifies forever (the 2026-07-13 bounce storm,
# ~15 texts). Renaming in place PRESERVES it on every issue that carries it, unlike creating a fresh
# new-name label and orphaning the old one.
_RENAME_OLD = "needs-william"
_RENAME_NEW = "needs-owner"


def runner_managed_labels():
    """The runner-managed subset of LABELS — the labels the RUNNER writes as machinery, tagged
    '(runner-managed)' in their descriptions (in-progress, needs-owner, parked). These are the ones
    the boot migration self-heals: if one is missing, a hand-back / claim label-move fails, which is
    exactly the storm class issue #160 closes."""
    return [name for name, _color, desc in LABELS if "(runner-managed)" in desc]


def missing_runner_labels(existing):
    """The runner-managed labels ABSENT from `existing` (a set/list of label names), in LABELS
    order. Fails closed on a wrong-typed `existing` (treats it as empty -> every one missing), so a
    garbage gh read never masquerades as 'all present'."""
    have = set(existing) if isinstance(existing, (set, list, tuple, frozenset)) else set()
    return [n for n in runner_managed_labels() if n not in have]


def label_spec(name):
    """(color, description-template) for a label name, or None if it is not a known §C.2 label. The
    template still carries the raw `{operator}` placeholder — the caller substitutes the configured
    operator at write time."""
    return _SPEC.get(name)


def label_migration_plan(existing):
    """The ordered, idempotent label migrations for a repo whose current labels are `existing` (a
    set/list of names). Each step is a dict the runner executor consumes:

        {"kind": "rename", "old": "needs-william", "new": "needs-owner"}
        {"kind": "create", "name": <a runner-managed label still missing>}

    Empty when the repo is already migrated (a no-op boot). The RENAME comes first and, when it
    applies, is accounted for before the create scan — so the label it produces (needs-owner) is not
    then also queued for creation. Wrong-typed `existing` fails closed to empty (via
    missing_runner_labels), so a garbage read plans to (re)create the runner-managed set rather than
    silently plan nothing."""
    have = set(existing) if isinstance(existing, (set, list, tuple, frozenset)) else set()
    steps = []
    if _RENAME_OLD in have and _RENAME_NEW not in have:
        steps.append({"kind": "rename", "old": _RENAME_OLD, "new": _RENAME_NEW})
        have = set(have)
        have.discard(_RENAME_OLD)
        have.add(_RENAME_NEW)                  # the rename produced it — don't also create it
    for name in missing_runner_labels(have):
        steps.append({"kind": "create", "name": name})
    return steps
