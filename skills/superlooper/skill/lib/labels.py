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
    # The #163 durable-question hand-back: a worker exited on an owner decision, the question is a
    # comment, and this label is what says the issue is WAITING rather than building. Registered by
    # issue #337, after it was born in code and never in the vocabulary — the runner wrote it, gh
    # refused it (no repo had it), and because every label move retries silently, #310 froze for ~9
    # hours on 2026-08-04 with the owner's answer un-ingestable. '(runner-managed)' is load-bearing
    # here, not decoration: the runner APPLIES this one as machinery (unlike expedite/rebuild/
    # pre-authorized:referee, which are the owner's own verbs), so the #160 boot migration must
    # self-heal it on every repo adopted before this shipped — otherwise the next question hand-back
    # on such a repo freezes exactly the same way.
    ("awaiting-answer", "c5def5",
     "a posted worker question awaits {operator}'s answer (runner-managed)"),
    ("expedite", "b60205", "queue-bypass lane: the next free lane takes this first"),
    ("preserve", "5319e7", "on a PR: resolve conflicts in-branch instead of regenerating"),
    ("auto-approved:nightly-red", "e99695",
     "standing rule: auto-filed fix for a red nightly/dev (scoped to restoring green)"),
    # {operator}'s word, granted at approval, for a foreseeable referee-path owner-stop (issue
    # #165). A DISTINCT label, never folded into `agent-ready` — the same discipline as
    # `auto-approved:nightly-red` above: the audit trail must always show HOW a referee touch was
    # cleared. It carries its OWN colour (`f9d0c4`, a pale peach — not that sibling's deeper salmon
    # `e99695`), so the two owner-word grants are told apart at a glance in the issue list: the
    # discipline they share is the distinct label, not a shared colour. Deliberately NOT
    # '(runner-managed)': the runner must never create, apply, or heal this one — it is only ever
    # applied by hand, by {operator}.
    ("pre-authorized:referee", "f9d0c4",
     "{operator}'s pre-authorization: the gate may merge this issue's referee-path touches"),
    ("superseded", "cccccc", "on a PR: replaced by a rebuild; branch preserved, nothing auto-closed"),
    # {operator}'s explicit rebuild-from-scratch verb (issue #161). Re-approving a FINISHED lane now
    # RESUMES AT THE GATE by default (keeps the PR/report/worktree, re-runs the merge gate); this
    # label is the owner's separately-named choice to instead DISCARD that work and build anew. Like
    # every owner-applied control label (expedite, pre-authorized:referee) it is NEVER
    # '(runner-managed)': the owner applies it, the runner only reads it and clears it once consumed.
    ("rebuild", "d73a4a",
     "{operator}'s explicit rebuild: discard this issue's PR and review and build from scratch "
     "(instead of the default resume-at-the-gate)"),
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
    # WHO FILED THIS (issue #400). Provenance only: one label per issue naming the session kind that
    # created it, so {operator} can filter the pile at a glance — issues he worked to shape in a
    # planning session versus ones the agents added, and a quick check that the QA filer is doing its
    # job. NOTHING in the engine reads these: no scheduling, gating or approval decision may ever
    # turn on provenance (owner ruling 2026-08-06), and `test_no_launch_path_reads_the_source_family`
    # in test_label_vocabulary.py is what keeps that true. They share ONE colour on purpose — this is
    # a metadata band, not a set of distinctions the eye has to tell apart mid-queue.
    #
    # The VALUE set is OPEN, like model:/effort: (see OPEN_LABEL_FAMILIES below): an adopter adds
    # `source:slackbot` by creating and applying it, with no engine change. What is here is the
    # starter set the engine's own filers and the write-issue doctrine name.
    ("source:orchestration", "5a32a3", "provenance: shaped in a planning session with {operator}"),
    ("source:build", "5a32a3",
     "provenance: filed by an `i<N>` build session (a follow-up, a promise, or a split)"),
    ("source:investigation", "5a32a3",
     "provenance: a child of a `type:investigate` issue"),
    ("source:debugger", "5a32a3", "provenance: filed by a `d<N>` repair session"),
    # The one the ENGINE applies, and so the one that must self-heal. The restore-green filer stamps
    # it on the issue it CREATES (actions.FIX_ISSUE_LABELS / nightly.NIGHTLY_FIX_LABELS), and
    # `gh issue create` refuses OUTRIGHT for a label the repo lacks — so on a repo adopted before
    # this shipped the auto-file would fail entirely and a red nightly would sit with no fix issue.
    # That is the #165/#337 defect class a third time; '(runner-managed)' is what makes the #160 boot
    # migration create it without anyone re-running adopt. It rides ALONGSIDE
    # `auto-approved:nightly-red` and replaces nothing: that one records how the issue was APPROVED,
    # this one records who FILED it.
    ("source:qa", "5a32a3",
     "provenance: auto-filed by the restore-green QA filer (runner-managed)"),
    ("source:dashboard-flag", "5a32a3",
     "provenance: filed by the command center's Flag button"),
]

# name -> (color, description-template). Built once; label_spec resolves a create step's color/desc.
_SPEC = {name: (color, desc) for name, color, desc in LABELS}

# The one label migration the runner knows how to apply beyond creating a missing label: the issue
# #58 rename of `needs-william` -> `needs-owner`. It merged INTO adopt, but a repo adopted before it
# — and never re-adopted after the republish — still carries the OLD label, so every runner
# hand-back that writes the NEW label fails and re-notifies forever (the 2026-07-13 bounce storm,
# ~15 texts). Renaming in place PRESERVES it on every issue that carries it, unlike creating a fresh
# new-name label and orphaning the old one.
# Retired label name -> the name that replaced it. PUBLIC because it is the only record of the
# loop's dead vocabulary, and the doc-lint (issue #199, defect class D12) needs it: an ops doc that
# describes today's behaviour in a retired name sends the reader hunting a label the runner never
# writes. Keeping the map here — beside LABELS — means the lint reads the same source of truth the
# migration does, and a future rename is one edit, not two.
RETIRED_LABELS = {
    "needs-william": "needs-owner",
}

# Label families whose VALUE set is deliberately open (owner ruling 2026-07-07, see the model:/effort:
# block above): the runner has no allowlist, so any `model:<x>` / `effort:<x>` a repo creates and
# applies works, and LABELS carries only a starter set. PUBLIC for the same reason RETIRED_LABELS is
# — the doc-lint has to know that `model:haiku` in an ops doc is a legitimate example and not a typo
# for a label that does not exist. Without this the lint would redden CI over an instruction that
# genuinely works, which is how a guard gets deleted rather than fixed.
#
# `source` joins them by the same ruling, for a stronger reason (issue #400): the family exists so an
# ADOPTER can name their own filers, and a `source:slackbot` written into that adopter's own docs must
# not be read here as a typo for a label that does not exist.
OPEN_LABEL_FAMILIES = ("model", "effort", "source")

# The provenance family's prefix, spelled ONCE. Every surface that has to RECOGNIZE the family —
# the lint's advisory, the structural guards — derives its spelling from here rather than retyping
# it, so a rename is one edit and no guard is left scanning for the old spelling while passing.
SOURCE_PREFIX = "source:"

# The one value the ENGINE itself applies (the restore-green filer). Named here rather than spelled
# at the two filers so the label-vocabulary fence resolves it across modules and the two filers
# cannot drift into stamping different provenance for the same auto-file.
SOURCE_QA = "source:qa"


def source_labels():
    """The registered `source:` values, in LABELS order. The engine's own starter set — NOT a closed
    allowlist (nothing validates a `source:` value; see OPEN_LABEL_FAMILIES), so this is what adopt
    seeds and what the write-issue doctrine is checked against, never a gate on what a repo may
    apply."""
    return [name for name, _color, _desc in LABELS if name.startswith(SOURCE_PREFIX)]


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

    Empty when the repo is already migrated (a no-op boot). RENAMES come first and, when they apply,
    are accounted for before the create scan — so a label a rename produces (needs-owner) is not
    then also queued for creation. Wrong-typed `existing` fails closed to empty (via
    missing_runner_labels), so a garbage read plans to (re)create the runner-managed set rather than
    silently plan nothing.

    EVERY entry in RETIRED_LABELS is planned, not just the first: that map is public (the doc-lint
    reads it), so a future rename will be added there and would otherwise be lint-visible but never
    actually migrated — the exact shape of the 2026-07-13 bounce storm, where a merged rename had
    not been applied to the repo. Sorted so the plan is deterministic."""
    have = set(existing) if isinstance(existing, (set, list, tuple, frozenset)) else set()
    steps = []
    for old, new in sorted(RETIRED_LABELS.items()):
        if old in have and new not in have:
            steps.append({"kind": "rename", "old": old, "new": new})
            have = set(have)
            have.discard(old)
            have.add(new)                      # the rename produced it — don't also create it
    for name in missing_runner_labels(have):
        steps.append({"kind": "create", "name": name})
    return steps
