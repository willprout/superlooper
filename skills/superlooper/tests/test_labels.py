"""lib/labels.py — the §C.2 label set and the pure per-repo label MIGRATION plan (issue #160).

These pin the pure core the runner's boot migration and the CLI's adopt/doctor both build on:
the runner-managed subset is derived from the '(runner-managed)' description tag (LABELS stays the
single source of truth), and label_migration_plan turns "what labels does the repo have now" into
the ordered, idempotent steps that close the merged+installed -> applied gap. Impure application
(gh writes, the systemic hold) is tested in test_runner.py; here nothing touches GitHub.
"""
import labels


def test_runner_managed_subset_is_the_tagged_set():
    # exactly the labels the RUNNER writes as machinery — derived from the description tag, so the
    # LABELS list is the one place the vocabulary is defined.
    assert set(labels.runner_managed_labels()) == {"in-progress", "needs-owner", "parked"}
    for name in labels.runner_managed_labels():
        color, desc = labels.label_spec(name)
        assert "(runner-managed)" in desc and color   # every runner-managed label has a real spec


def test_missing_runner_labels_fails_closed_on_garbage():
    assert labels.missing_runner_labels(set(n for n, _c, _d in labels.LABELS)) == []
    assert labels.missing_runner_labels({"agent-ready", "in-progress", "parked"}) == ["needs-owner"]
    # a wrong-typed / garbage read is treated as EMPTY -> every runner-managed label reads missing,
    # never as "all present" (the repo's fail-open-on-wrong-typed defect class).
    assert set(labels.missing_runner_labels("garbage")) == {"in-progress", "needs-owner", "parked"}
    assert set(labels.missing_runner_labels(None)) == {"in-progress", "needs-owner", "parked"}


def test_plan_is_empty_when_already_applied():
    have = [n for n, _c, _d in labels.LABELS]              # a fully-adopted repo
    assert labels.label_migration_plan(have) == []
    # needs-william already renamed away AND needs-owner present -> still a no-op (idempotent).
    assert labels.label_migration_plan(["needs-owner", "in-progress", "parked"]) == []


def test_plan_creates_a_missing_runner_managed_label():
    have = [n for n, _c, _d in labels.LABELS if n != "needs-owner"]
    plan = labels.label_migration_plan(have)
    assert plan == [{"kind": "create", "name": "needs-owner"}]


def test_plan_renames_needs_william_first_and_does_not_recreate_it():
    # the 2026-07-13 storm's exact shape: the repo still carries the OLD needs-william and lacks the
    # NEW needs-owner. The plan renames in place (preserving every issue that carries it) and must
    # NOT then also try to create needs-owner (the rename already produced it).
    have = ["needs-william", "in-progress", "parked", "agent-ready"]
    plan = labels.label_migration_plan(have)
    assert plan == [{"kind": "rename", "old": "needs-william", "new": "needs-owner"}]


def test_plan_renames_and_still_creates_other_missing_labels():
    have = ["needs-william", "agent-ready"]                # in-progress + parked also missing
    plan = labels.label_migration_plan(have)
    assert plan[0] == {"kind": "rename", "old": "needs-william", "new": "needs-owner"}
    created = [s["name"] for s in plan if s["kind"] == "create"]
    assert created == ["in-progress", "parked"]           # NOT needs-owner (rename produced it)


def test_plan_does_not_rename_when_needs_owner_already_exists():
    # both old and new present (a mid-migration repo): renaming would collide, so the rename is
    # skipped and only genuinely-missing labels are created.
    have = ["needs-william", "needs-owner", "in-progress", "parked"]
    assert labels.label_migration_plan(have) == []


def test_plan_creates_every_runner_managed_label_from_scratch():
    plan = labels.label_migration_plan([])
    assert plan == [{"kind": "create", "name": n} for n in ("in-progress", "needs-owner", "parked")]
