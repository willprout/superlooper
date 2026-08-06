"""lib/labels.py — the §C.2 label set and the pure per-repo label MIGRATION plan (issue #160).

These pin the pure core the runner's boot migration and the CLI's adopt/doctor both build on:
the runner-managed subset is derived from the '(runner-managed)' description tag (LABELS stays the
single source of truth), and label_migration_plan turns "what labels does the repo have now" into
the ordered, idempotent steps that close the merged+installed -> applied gap. Impure application
(gh writes, the systemic hold) is tested in test_runner.py; here nothing touches GitHub.
"""
import gate
import labels


def test_referee_preauthorization_label_exists_but_is_never_runner_managed():
    """Issue #165: the owner's pre-authorization is only reachable if the label EXISTS — gh refuses
    to apply a label a repo doesn't have, so a label absent from LABELS is a feature the owner
    cannot use at all (adopt creates the set; doctor reports what's missing). It must be here, and
    its name must be the ONE the gate keys on.

    But it must NEVER be runner-managed: '(runner-managed)' is what boot-migration self-healing keys
    off, and this label is the owner's WORD — the runner creating or healing it is exactly the
    machine granting itself a bright-line pass. adopt (the whole set) creates it; the runner (the
    tagged subset) never touches it.
    """
    spec = labels.label_spec(gate.PREAUTHORIZED_REFEREE_LABEL)
    assert spec is not None, "the label the gate keys on must be in the §C.2 set, or adopt/doctor " \
                             "never create it and the owner cannot apply it"
    color, desc = spec
    assert color and desc
    assert "(runner-managed)" not in desc
    assert gate.PREAUTHORIZED_REFEREE_LABEL not in labels.runner_managed_labels()
    assert gate.PREAUTHORIZED_REFEREE_LABEL not in labels.missing_runner_labels(set())
    # and it is never planned for creation by the runner's boot migration, on any repo state
    assert all(step.get("name") != gate.PREAUTHORIZED_REFEREE_LABEL
               for step in labels.label_migration_plan(set()))


def test_rebuild_label_exists_but_is_never_runner_managed():
    """Issue #161: the explicit rebuild-from-scratch verb is signalled by the `rebuild` label — the
    owner's choice to DISCARD a finished PR/report instead of the default resume-at-the-gate. gh
    refuses to apply a label a repo doesn't have, so it must be in the §C.2 set (adopt creates it;
    the dashboard's rebuild verb also create-or-forces it on tap). It is NOT runner-managed: like
    every owner-applied control label it is the OWNER's word, never a label the runner creates or
    heals as its own machinery."""
    spec = labels.label_spec("rebuild")
    assert spec is not None, "the rebuild label must be in the §C.2 set, or adopt never creates it"
    color, desc = spec
    assert color and desc
    assert "(runner-managed)" not in desc
    assert "rebuild" not in labels.runner_managed_labels()
    assert "rebuild" not in labels.missing_runner_labels(set())
    assert all(step.get("name") != "rebuild" for step in labels.label_migration_plan(set()))


def test_awaiting_answer_is_registered_and_runner_managed():
    """Issue #337, the #165 defect class's second occurrence. The #163 question hand-back writes
    `awaiting-answer` in the runner; nothing ever put it in LABELS, so adopt never created it, the
    #160 boot migration never healed it, and gh refused the label move on every repo. Because that
    move retries silently, #310 froze for ~9 hours (2026-08-04) with the owner's answer
    un-ingestable — the settle that leaves awaiting_answer runs only after the label lands.

    It must be registered AND '(runner-managed)': unlike the owner's own verbs (expedite, rebuild,
    pre-authorized:referee) the RUNNER applies this one as machinery, so the boot migration has to
    self-heal it on every repo adopted before this shipped — otherwise the next question hand-back
    on such a repo freezes exactly the same way.
    """
    spec = labels.label_spec("awaiting-answer")
    assert spec is not None, "the label the question hand-back writes must be in the §C.2 set, or " \
                             "adopt never creates it and gh refuses every hand-back"
    color, desc = spec
    assert color and desc
    assert "(runner-managed)" in desc
    assert "awaiting-answer" in labels.runner_managed_labels()
    # a repo adopted before #337 lacks it -> boot plans the create, so the heal is automatic
    assert "awaiting-answer" in labels.missing_runner_labels(
        {n for n, _c, _d in labels.LABELS} - {"awaiting-answer"})
    assert {"kind": "create", "name": "awaiting-answer"} in labels.label_migration_plan(
        [n for n, _c, _d in labels.LABELS if n != "awaiting-answer"])


def test_the_source_family_starter_set_is_registered():
    """Issue #400: every new issue names WHERE it came from, so the owner can filter the pile by
    provenance — what he shaped in a planning session versus what the agents added.

    gh refuses a label the repo does not have, so the registry is what makes the family usable at
    all: adopt creates the whole set from LABELS, and doctor reports what a repo is missing. The
    six values here are the STARTER set, not a closed one (see the open-family test below)."""
    for name in ("source:orchestration", "source:build", "source:investigation",
                 "source:debugger", "source:qa", "source:dashboard-flag"):
        spec = labels.label_spec(name)
        assert spec is not None, ("%s must be in the §C.2 set, or adopt never creates it and every "
                                  "session told to apply it is refused by gh" % name)
        color, desc = spec
        assert color and desc.strip()


def test_the_source_family_is_open_like_model_and_effort():
    # Owner ruling (#400): adopters add their own values — a future `source:slackbot` — without an
    # engine change. That is only true if the doc-lint knows the family's values are OPEN; otherwise
    # a doc naming an adopter's value reddens CI over an instruction that genuinely works.
    assert "source" in labels.OPEN_LABEL_FAMILIES


def test_source_qa_is_runner_managed_and_the_rest_of_the_family_is_not():
    """The one `source:` label the ENGINE itself applies — and the reason it must self-heal.

    The restore-green fix filer (actions.FIX_ISSUE_LABELS / nightly.NIGHTLY_FIX_LABELS) stamps
    `source:qa` on the issue it CREATES. `gh issue create` refuses outright when a label is missing,
    so on a repo adopted before this shipped the whole auto-file would fail — a red nightly with no
    fix issue ever filed. That is the #165/#337 defect class a third time, so the boot migration
    heals this one.

    The other five are applied by SESSIONS and by the dashboard, never by the runner, so they are
    not runner-managed: '(runner-managed)' is what the migration keys off, and tagging a label the
    runner never writes would make the vocabulary lie about who writes what.
    """
    color, desc = labels.label_spec("source:qa")
    assert color and "(runner-managed)" in desc
    assert "source:qa" in labels.runner_managed_labels()
    # a repo adopted before #400 lacks it -> boot plans the create, no re-adopt needed
    assert {"kind": "create", "name": "source:qa"} in labels.label_migration_plan(
        [n for n, _c, _d in labels.LABELS if n != "source:qa"])
    for name in ("source:orchestration", "source:build", "source:investigation",
                 "source:debugger", "source:dashboard-flag"):
        assert "(runner-managed)" not in labels.label_spec(name)[1]
        assert name not in labels.runner_managed_labels()
        assert all(step.get("name") != name for step in labels.label_migration_plan(set()))


def test_runner_managed_subset_is_the_tagged_set():
    # exactly the labels the RUNNER writes as machinery — derived from the description tag, so the
    # LABELS list is the one place the vocabulary is defined.
    assert set(labels.runner_managed_labels()) == {"in-progress", "needs-owner", "parked",
                                                   "awaiting-answer", "source:qa"}
    for name in labels.runner_managed_labels():
        color, desc = labels.label_spec(name)
        assert "(runner-managed)" in desc and color   # every runner-managed label has a real spec


def test_missing_runner_labels_fails_closed_on_garbage():
    assert labels.missing_runner_labels(set(n for n, _c, _d in labels.LABELS)) == []
    assert labels.missing_runner_labels(
        {"agent-ready", "in-progress", "parked", "awaiting-answer", "source:qa"}) == ["needs-owner"]
    # a wrong-typed / garbage read is treated as EMPTY -> every runner-managed label reads missing,
    # never as "all present" (the repo's fail-open-on-wrong-typed defect class).
    assert set(labels.missing_runner_labels("garbage")) == {"in-progress", "needs-owner", "parked",
                                                            "awaiting-answer", "source:qa"}
    assert set(labels.missing_runner_labels(None)) == {"in-progress", "needs-owner", "parked",
                                                       "awaiting-answer", "source:qa"}


def test_plan_is_empty_when_already_applied():
    have = [n for n, _c, _d in labels.LABELS]              # a fully-adopted repo
    assert labels.label_migration_plan(have) == []
    # needs-william already renamed away AND needs-owner present -> still a no-op (idempotent).
    assert labels.label_migration_plan(
        ["needs-owner", "in-progress", "parked", "awaiting-answer", "source:qa"]) == []


def test_plan_creates_a_missing_runner_managed_label():
    have = [n for n, _c, _d in labels.LABELS if n != "needs-owner"]
    plan = labels.label_migration_plan(have)
    assert plan == [{"kind": "create", "name": "needs-owner"}]


def test_plan_renames_needs_william_first_and_does_not_recreate_it():
    # the 2026-07-13 storm's exact shape: the repo still carries the OLD needs-william and lacks the
    # NEW needs-owner. The plan renames in place (preserving every issue that carries it) and must
    # NOT then also try to create needs-owner (the rename already produced it).
    have = ["needs-william", "in-progress", "parked", "awaiting-answer", "source:qa", "agent-ready"]
    plan = labels.label_migration_plan(have)
    assert plan == [{"kind": "rename", "old": "needs-william", "new": "needs-owner"}]


def test_plan_heals_a_repo_adopted_before_awaiting_answer_was_registered():
    # EVERY repo adopted before issue #337 is in this state: the whole pre-#337 set present, the
    # question hand-back's label absent. Boot creates exactly it — one step, nothing else disturbed
    # — which is what makes the fix reach other adopted repos without anyone re-running adopt.
    have = [n for n, _c, _d in labels.LABELS if n != "awaiting-answer"]
    assert labels.label_migration_plan(have) == [{"kind": "create", "name": "awaiting-answer"}]


def test_plan_renames_and_still_creates_other_missing_labels():
    have = ["needs-william", "agent-ready"]                # in-progress + parked + awaiting also missing
    plan = labels.label_migration_plan(have)
    assert plan[0] == {"kind": "rename", "old": "needs-william", "new": "needs-owner"}
    created = [s["name"] for s in plan if s["kind"] == "create"]
    # NOT needs-owner (the rename produced it); LABELS order, so awaiting-answer comes after parked
    assert created == ["in-progress", "parked", "awaiting-answer", "source:qa"]


def test_plan_does_not_rename_when_needs_owner_already_exists():
    # both old and new present (a mid-migration repo): renaming would collide, so the rename is
    # skipped and only genuinely-missing labels are created.
    have = ["needs-william", "needs-owner", "in-progress", "parked", "awaiting-answer", "source:qa"]
    assert labels.label_migration_plan(have) == []


def test_plan_creates_every_runner_managed_label_from_scratch():
    plan = labels.label_migration_plan([])
    assert plan == [{"kind": "create", "name": n}
                    for n in ("in-progress", "needs-owner", "parked", "awaiting-answer",
                              "source:qa")]
