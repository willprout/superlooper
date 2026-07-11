"""The mechanical ship gate (§C.4) as pure functions — failing tests first, one per numbered
step of the state machine. Two standing hunts from Session 1's reviews are negative-tested
throughout: shared mutable defaults, and fail-OPEN on wrong-TYPED (not just missing) input —
a corrupt view must always land on the safe action (wait/nudge/park), never on "merge" and
never on a raised exception into the tick.
"""
import gate


def _cfg(**over):
    """A §C.1-shaped config (the fields gate.py reads), defaults matching config.py."""
    base = {
        "dev_branch": "main",
        "ship_cmd": None,
        "required_checks": ["quality-gate"],
        "report_required_sections": ["Tests", "Review"],
        "session": {"idle_seconds": 480, "freeze_seconds": 2700,
                    "retry_cap": 2, "conflict_cap": 2},
        "areas": {"frontend": ["src/components/**"], "api": ["src/api/**"]},
        "touches_required": True,
    }
    base.update(over)
    return base


def _issue(**over):
    """The issue view the runner assembles for the gate: the loopstate entry merged with
    parsed-issue facts (type) and precomputed marker facts (investigation_done)."""
    base = {"type": "build", "status": "gating", "conflicts": 0, "nudged": [],
            "update_result": None, "declared_touches": ["frontend"],
            "investigation_done": False}
    base.update(over)
    return base


def _pr(**over):
    """A pr_view: gh.pr_for_branch(...) merged with comments (the runner attaches
    gh.pr_comments(num) under 'comments')."""
    base = {"number": 555, "state": "OPEN", "mergeable": "MERGEABLE", "labels": [],
            "statusCheckRollup": [{"context": "quality-gate", "state": "SUCCESS"}],
            "files": [{"path": "src/components/Widget.tsx"}],
            "comments": [{"body": "<!-- superlooper-review --> fresh-agent review: "
                                  "diff reviewed, no P0/P1 findings"}]}
    base.update(over)
    return base


GOOD_REPORT = (
    "## Tests\n171 passed in 2.1s — full suite output attached in the PR description.\n"
    "## Review\nFresh reviewer (wrote none of the diff) approved; two P2 nits deferred, "
    "zero P0/P1 findings.\n"
)


_DEFAULT = object()   # sentinel: `pr=None` must reach gate_decision as a real None


def _decide(issue=None, pr=_DEFAULT, report=GOOD_REPORT, cfg=None, frozen=False, inflight=None):
    d = gate.gate_decision(issue or _issue(), _pr() if pr is _DEFAULT else pr, report,
                           cfg or _cfg(), frozen, inflight or {})
    # every decision must explain itself — a bare action with no reason is unjournalable
    assert isinstance(d.get("reason"), str) and d["reason"].strip()
    return d


# --------------------------- report_sections_ok (step 2, cross-review C3) ---------------------------

def test_report_sections_ok_happy():
    assert gate.report_sections_ok(GOOD_REPORT, ["Tests", "Review"]) is True


def test_report_sections_missing_heading():
    assert gate.report_sections_ok("## Tests\n" + "x" * 50, ["Tests", "Review"]) is False


def test_report_sections_empty_body_never_merges():
    # THE C3 negative: headings present but bodies empty must fail — an empty-section report
    # once looked "complete" to a headings-only check.
    txt = "## Tests\n\n## Review\n\n"
    assert gate.report_sections_ok(txt, ["Tests", "Review"]) is False


def test_report_sections_short_body_fails_and_boundary_passes():
    # < 40 non-whitespace chars of prose is not evidence; exactly 40 is the documented floor.
    short = "## Tests\nok\n## Review\nfine\n"
    assert gate.report_sections_ok(short, ["Tests", "Review"]) is False
    body40 = "x" * 40
    txt = f"## Tests\n{body40}\n## Review\n{body40}\n"
    assert gate.report_sections_ok(txt, ["Tests", "Review"]) is True


def test_report_sections_whitespace_padding_does_not_count():
    # 40 chars must be NON-whitespace: spaces/newlines/tabs can't pad a section past the floor.
    txt = "## Tests\n" + ("x " * 19) + "\n## Review\n" + "y" * 40 + "\n"   # 19 non-ws chars
    assert gate.report_sections_ok(txt, ["Tests", "Review"]) is False


def test_report_sections_h3_is_not_h2():
    # the contract is H2 headings, parsed mechanically — an H3 "### Tests" is not the section.
    txt = "### Tests\n" + "x" * 50 + "\n## Review\n" + "y" * 50
    assert gate.report_sections_ok(txt, ["Tests", "Review"]) is False


def test_report_sections_wrong_typed_report_fails_closed():
    for bad in (None, 42, ["## Tests"], {"Tests": "x"}):
        assert gate.report_sections_ok(bad, ["Tests"]) is False
    # wrong-typed required list -> closed too (never "vacuously ok" off corrupt config)
    assert gate.report_sections_ok(GOOD_REPORT, None) is False


def test_report_sections_empty_required_is_vacuously_ok():
    # an empty required list loads legally (config default is non-empty; doctor C3 owns refusing
    # degenerate repo configs) — nothing required means nothing missing.
    assert gate.report_sections_ok("anything", []) is True


# --------------------------- review_evidence_ok (step 2b) ---------------------------

def test_review_evidence_ship_cmd_repo_owns_review():
    # ship_cmd set -> the repo pipeline owns review (eApp: ship.sh's diff-pinned review/local-gate)
    assert gate.review_evidence_ok(_cfg(ship_cmd="scripts/ship.sh"), []) is True


def test_review_evidence_marker_comment():
    ok = [{"body": "<!-- superlooper-review --> reviewed; P0/P1: none"}]
    assert gate.review_evidence_ok(_cfg(), ok) is True
    assert gate.review_evidence_ok(_cfg(), []) is False
    # the marker must BEGIN the comment — quoting it mid-text (e.g. discussing the contract) is
    # not a verdict
    mid = [{"body": "the gate wants <!-- superlooper-review --> at the start"}]
    assert gate.review_evidence_ok(_cfg(), mid) is False


def test_review_evidence_tolerates_wrong_typed_comments():
    junk = [None, 42, {"nobody": "x"}, {"body": None}]
    assert gate.review_evidence_ok(_cfg(), junk) is False
    assert gate.review_evidence_ok(_cfg(), None) is False


# --------------------------- investigation_done (cross-review C1) ---------------------------

def test_investigation_marker_comment():
    done = [{"body": "<!-- superlooper-investigation -->\nRoot cause: ..."}]
    assert gate.investigation_done(done) is True
    assert gate.investigation_done([{"body": "Root cause: ..."}]) is False
    assert gate.investigation_done([]) is False
    assert gate.investigation_done(None) is False


# --------------------------- touch_verdict (step 3) ---------------------------

def test_touch_verdict_clean():
    v = gate.touch_verdict(["frontend"], ["frontend"], {})
    assert v == {"wander": False, "overlap_lane": None}


def test_touch_verdict_wander():
    v = gate.touch_verdict(["frontend"], ["frontend", "db"], {})
    assert v["wander"] is True and v["overlap_lane"] is None


def test_touch_verdict_nothing_declared_is_not_wander():
    # a repo without touches_required lets issues declare nothing; no promise -> no wander spam
    assert gate.touch_verdict([], ["api"], {})["wander"] is False


def test_touch_verdict_overlap_holds_merge():
    v = gate.touch_verdict(["frontend"], ["frontend"], {"i7": ["frontend", "db"]})
    assert v["overlap_lane"] == "i7"
    # disjoint in-flight lane -> no hold
    v2 = gate.touch_verdict(["frontend"], ["frontend"], {"i7": ["db"]})
    assert v2["overlap_lane"] is None


def test_touch_verdict_wildcard_overlaps_everything():
    # '*' is the no-declared-area bucket (config.path_to_area): it conflicts with any lane,
    # in EITHER direction (the kickoff's fixed wildcard-overlap contract).
    assert gate.touch_verdict([], ["*"], {"i7": ["db"]})["overlap_lane"] == "i7"
    assert gate.touch_verdict([], ["api"], {"i7": ["*"]})["overlap_lane"] == "i7"


def test_touch_verdict_first_overlap_deterministic_and_tolerant():
    # sorted lane order -> deterministic pick; a wrong-typed lane entry is skipped, not fatal
    v = gate.touch_verdict([], ["api"], {"i9": ["api"], "i2": ["api"], "i5": "junk"})
    assert v["overlap_lane"] == "i2"


def test_touch_verdict_tolerates_wrong_typed_inflight():
    # Codex cross-review (Task 9): a corrupt inflight view ("junk" has no .get; mixed-type
    # dict keys break sorted()) must degrade to no-overlap, never raise into the tick.
    assert gate.touch_verdict([], ["api"], "junk")["overlap_lane"] is None
    assert gate.touch_verdict([], ["api"], None)["overlap_lane"] is None
    v = gate.touch_verdict([], ["api"], {3: ["api"], "i2": ["api"]})
    assert v["overlap_lane"] == "i2"   # non-string lane ids skipped, no TypeError


# --------------------------- required checks (step 5) ---------------------------

def test_checks_green():
    rollup = [{"context": "quality-gate", "state": "SUCCESS"},
              {"name": "extra-optional", "conclusion": "FAILURE"}]   # not required -> ignored
    assert gate.required_checks_state(rollup, ["quality-gate"]) == "green"


def test_checks_pending_when_missing_or_running():
    assert gate.required_checks_state([], ["quality-gate"]) == "pending"
    rollup = [{"context": "quality-gate", "state": "PENDING"}]
    assert gate.required_checks_state(rollup, ["quality-gate"]) == "pending"
    rollup2 = [{"name": "quality-gate", "status": "IN_PROGRESS", "conclusion": None}]
    assert gate.required_checks_state(rollup2, ["quality-gate"]) == "pending"


def test_checks_fail_beats_pending():
    rollup = [{"context": "quality-gate", "state": "FAILURE"},
              {"name": "review/local-gate", "conclusion": None}]
    assert gate.required_checks_state(rollup, ["quality-gate", "review/local-gate"]) == "fail"


def test_checks_wrong_typed_rollup_is_pending_never_green():
    # fail closed: a corrupt rollup must WAIT, not merge
    for bad in (None, "junk", [None, "x", 42]):
        assert gate.required_checks_state(bad, ["quality-gate"]) == "pending"


def test_checks_unrecognized_state_is_pending_never_green():
    # Codex cross-review (Task 9): success is an EXPLICIT set — a state gh grows tomorrow
    # ("BOGUS") or a wrong-typed value must bucket to pending (wait), never fall through to
    # green. The old else-green fold was fail-open.
    for weird in ("BOGUS", 123, [], {}):
        rollup = [{"context": "quality-gate", "state": weird}]
        assert gate.required_checks_state(rollup, ["quality-gate"]) == "pending"


def test_checks_rest_lowercase_conclusions_fold_like_graphql_uppercase():
    # Task-15 simulation catch: gh's REST check-runs API (gh.branch_checks — the dev-branch
    # poll behind freeze/unfreeze) reports conclusions in LOWERCASE ("failure", "success"),
    # while the GraphQL PR rollup is uppercase. Without case normalization every dev state
    # read as "pending", so a red dev NEVER froze merges and a green dev NEVER unfroze them —
    # the whole fix-forward loop was dead on a real repo.
    red = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
    assert gate.required_checks_state(red, ["ci"]) == "fail"
    green = [{"name": "ci", "status": "completed", "conclusion": "success"}]
    assert gate.required_checks_state(green, ["ci"]) == "green"
    skipped = [{"name": "ci", "status": "completed", "conclusion": "skipped"}]
    assert gate.required_checks_state(skipped, ["ci"]) == "green"


# ---------------- check_names / pending breakdown / audit (issue #26) ----------------

def test_check_names_extracts_both_rollup_shapes():
    entries = [{"name": "review/local-gate", "conclusion": "SUCCESS"},
               {"context": "quality-gate", "state": "SUCCESS"}]
    assert gate.check_names(entries) == {"review/local-gate", "quality-gate"}


def test_check_names_wrong_typed_is_empty_set():
    for bad in (None, "x", 5, [None, 3, {"nope": 1}, {"name": 42}, {"name": ""}]):
        assert gate.check_names(bad) == set()


def test_pending_breakdown_splits_unreported_from_running():
    rollup = [{"name": "quality-gate", "status": "IN_PROGRESS", "conclusion": None}]
    b = gate.pending_required_breakdown(rollup, ["quality-gate", "never-reports"])
    assert b == {"unreported": ["never-reports"], "running": ["quality-gate"]}


def test_pending_breakdown_all_absent_are_unreported():
    assert gate.pending_required_breakdown([], ["b", "a"]) == {
        "unreported": ["a", "b"], "running": []}          # sorted, deterministic


def test_pending_breakdown_ignores_satisfied_and_failing():
    rollup = [{"context": "green", "state": "SUCCESS"},
              {"context": "red", "state": "FAILURE"},
              {"context": "run", "state": "PENDING"}]
    b = gate.pending_required_breakdown(rollup, ["green", "red", "run", "absent"])
    assert b == {"unreported": ["absent"], "running": ["run"]}


def test_audit_all_reported_on_dev_is_clean():
    a = gate.audit_required_checks(["ci"], {"ci"}, {"ci"})
    assert a["observed"] is True and a["dev_observed"] is True
    assert a["results"] == [{"name": "ci", "status": "reported", "hint": None}]


def test_audit_typo_is_unreported_with_case_and_shape_hint():
    a = gate.audit_required_checks(["quality-gate"], {"Quality Gate"}, {"Quality Gate"})
    assert a["results"][0]["status"] == "unreported"
    assert a["results"][0]["hint"] == "Quality Gate"       # normalized match -> the real name


def test_audit_never_wired_check_has_no_hint():
    a = gate.audit_required_checks(["nonexistent"], {"ci"}, {"ci"})
    assert a["results"][0] == {"name": "nonexistent", "status": "unreported", "hint": None}


def test_audit_pr_only_check_is_distinguished_from_unreported():
    # reports on PRs, never on the dev branch — the 2026-07-09 incident shape
    a = gate.audit_required_checks(["quality-gate"], {"quality-gate"}, {"ci"})
    assert a["results"][0]["status"] == "pr_only"
    assert a["dev_observed"] is True


def test_audit_dev_only_check_is_distinguished():
    # reports on the dev branch but never on recent PRs -> the PR gate reads pending forever, so a
    # green PR never merges (Codex R1: the mirror of pr_only, and the headline failure mode)
    a = gate.audit_required_checks(["quality-gate"], {"ci"}, {"quality-gate"})
    assert a["results"][0]["status"] == "dev_only"
    assert a["pr_observed"] is True and a["dev_observed"] is True


def test_audit_no_observations_anywhere_flags_no_evidence():
    a = gate.audit_required_checks(["ci"], set(), set())
    assert a["observed"] is False and a["dev_observed"] is False
    assert a["results"][0]["status"] == "unreported"       # trivially, but observed=False guards it


def test_audit_wrong_typed_names_fail_closed_to_no_evidence():
    a = gate.audit_required_checks(["ci"], None, "junk")
    assert a["observed"] is False and a["dev_observed"] is False


# --------------------------- gate_decision: the §C.4 table ---------------------------

def test_gate_happy_path_merges():
    d = _decide()
    assert d["action"] == "merge" and d.get("wander") is False


def test_gate_no_pr_parks():                                     # step 1
    for pv in ({}, None):
        d = _decide(pr=pv)
        assert d["action"] == "park"


def test_gate_bad_sections_nudge_once_then_park():               # step 2
    d = _decide(report="## Tests\nshort\n")
    assert d["action"] == "nudge" and d["nudge_key"] == "sections"
    d2 = _decide(issue=_issue(nudged=["sections"]), report="## Tests\nshort\n")
    assert d2["action"] == "park"


def test_gate_wrong_typed_report_is_the_sections_path():
    # fail-closed hunt: a None/corrupt report routes to nudge-then-park, never a crash or merge
    d = _decide(report=None)
    assert d["action"] == "nudge" and d["nudge_key"] == "sections"


def test_gate_review_evidence_nudge_once_then_park():            # step 2b
    pr = _pr(comments=[])
    d = _decide(pr=pr)
    assert d["action"] == "nudge" and d["nudge_key"] == "review"
    d2 = _decide(issue=_issue(nudged=["review"]), pr=pr)
    assert d2["action"] == "park"


def test_gate_ship_cmd_repo_needs_no_marker_comment():           # step 2b, eApp path
    d = _decide(pr=_pr(comments=[]), cfg=_cfg(ship_cmd="scripts/ship.sh"))
    assert d["action"] == "merge"


def test_gate_wander_is_journaled_not_blocking():                # step 3 (wander)
    pr = _pr(files=[{"path": "src/components/W.tsx"}, {"path": "migrations/001.sql"}])
    d = _decide(pr=pr, cfg=_cfg(areas={"frontend": ["src/components/**"],
                                       "db": ["migrations/**"]}))
    assert d["action"] == "merge" and d["wander"] is True


def test_gate_referee_superlooper_path_parks_needs_william():
    pr = _pr(files=[{"path": ".superlooper/config.json"}])
    d = _decide(pr=pr)
    assert d["action"] == "park" and d["needs_william"] is True
    assert ".superlooper/config.json" in d["reason"]


def test_gate_referee_workflow_path_parks_needs_william():
    pr = _pr(files=[{"path": ".github/workflows/quality.yml"}])
    d = _decide(pr=pr)
    assert d["action"] == "park" and d["needs_william"] is True
    assert ".github/workflows/quality.yml" in d["reason"]


def test_gate_referee_path_mixed_with_allowed_path_never_partially_merges():
    pr = _pr(files=[{"path": "src/components/W.tsx"},
                    {"path": ".github/workflows/quality.yml"}])
    d = _decide(pr=pr)
    assert d["action"] == "park" and d["needs_william"] is True
    assert ".github/workflows/quality.yml" in d["reason"]


def test_gate_declaring_referee_area_does_not_whitelist_referee_path():
    cfg = _cfg(areas={"frontend": ["src/components/**"],
                      "loop_rules": [".superlooper/**"]})
    pr = _pr(files=[{"path": ".superlooper/config.json"}])
    d = _decide(issue=_issue(declared_touches=["loop_rules"]), pr=pr, cfg=cfg)
    assert d["action"] == "park" and d["needs_william"] is True
    assert ".superlooper/config.json" in d["reason"]


def test_gate_overlap_with_inflight_lane_holds():                # step 3 (overlap)
    d = _decide(inflight={"i7": ["frontend"]})
    assert d["action"] == "hold" and d["overlap_lane"] == "i7"


def test_gate_undeclared_area_file_overlaps_via_wildcard():      # step 3 (the '*' contract)
    pr = _pr(files=[{"path": "random/loose-file.txt"}])          # matches no configured area
    d = _decide(pr=pr, inflight={"i7": ["db"]})
    assert d["action"] == "hold" and d["overlap_lane"] == "i7"


def test_gate_frozen_holds_merges():                             # step 4
    d = _decide(frozen=True)
    assert d["action"] == "hold"


def test_gate_checks_pending_waits():                            # step 5
    d = _decide(pr=_pr(statusCheckRollup=[{"context": "quality-gate", "state": "PENDING"}]))
    assert d["action"] == "wait"


def test_gate_checks_pending_surfaces_running_breakdown():       # step 5 (issue #26)
    d = _decide(pr=_pr(statusCheckRollup=[{"context": "quality-gate", "state": "PENDING"}]))
    assert d["action"] == "wait" and d["checks_pending"] is True
    assert d["pending"] == {"unreported": [], "running": ["quality-gate"]}


def test_gate_checks_pending_names_the_unreported_check():       # step 5 (issue #26)
    # the target failure: a required check that never reports reads as pending forever
    d = _decide(pr=_pr(statusCheckRollup=[]))
    assert d["action"] == "wait" and d["checks_pending"] is True
    assert d["pending"] == {"unreported": ["quality-gate"], "running": []}


def test_gate_non_pending_decisions_carry_no_checks_pending_flag():
    assert "checks_pending" not in _decide()                     # merge path
    assert _decide().get("checks_pending") is None


def test_gate_checks_fail_hand_back_once_then_park():            # step 5
    pr = _pr(statusCheckRollup=[{"context": "quality-gate", "state": "FAILURE"}])
    d = _decide(pr=pr)
    assert d["action"] == "nudge" and d["nudge_key"] == "checks"
    d2 = _decide(issue=_issue(nudged=["checks"]), pr=pr)
    assert d2["action"] == "park"


def test_gate_mergeable_unknown_waits_never_conflicts():         # step 6 (cross-review M2)
    # GitHub computes mergeability ASYNC: UNKNOWN/null/'' must WAIT — treating it as conflict
    # once superseded a perfectly clean PR; treating it as mergeable would merge unverified.
    for m in ("UNKNOWN", None, ""):
        d = _decide(pr=_pr(mergeable=m))
        assert d["action"] == "wait", f"mergeable={m!r} must wait, got {d['action']}"


def test_gate_conflicting_tries_mechanical_update_first():       # step 6a
    d = _decide(pr=_pr(mergeable="CONFLICTING"))
    assert d["action"] == "update"


def test_gate_clean_update_waits_for_github_recompute():         # step 6a (post-update tick)
    d = _decide(issue=_issue(update_result="clean"), pr=_pr(mergeable="CONFLICTING"))
    assert d["action"] == "wait"


def test_gate_real_conflict_regenerates():                       # step 6b
    d = _decide(issue=_issue(update_result="conflict"), pr=_pr(mergeable="CONFLICTING"))
    assert d["action"] == "regenerate"


def test_gate_conflict_cap_goes_to_william():                    # step 6b (cap)
    # conflict_cap=2: the FIRST real conflict regenerates (conflicts 0 -> 1); the SECOND parks
    # needs-william (1+1 >= cap). Matches the Task-15 scenario "second conflict -> parked at cap".
    d = _decide(issue=_issue(update_result="conflict", conflicts=1),
                pr=_pr(mergeable="CONFLICTING"))
    assert d["action"] == "park" and d["needs_william"] is True


def test_gate_preserve_label_resolves_in_branch():               # step 6c
    pr = _pr(mergeable="CONFLICTING", labels=[{"name": "preserve"}])
    d = _decide(issue=_issue(update_result="conflict"), pr=pr)
    assert d["action"] == "resolve_conflict"
    # preserve wins even at the conflict cap: William explicitly said keep THIS branch alive
    d2 = _decide(issue=_issue(update_result="conflict", conflicts=5), pr=pr)
    assert d2["action"] == "resolve_conflict"


def test_gate_investigate_marker_closes_parent():                # investigate branch (C1)
    d = _decide(issue=_issue(type="investigate", investigation_done=True), pr={})
    assert d["action"] == "close_investigate"


def test_gate_investigate_close_ignores_freeze_and_pr():
    # freeze only stops MERGES; closing an investigation parent is not a merge. Zero children
    # and zero PRs are legal ("nothing to do" is a valid root cause).
    d = _decide(issue=_issue(type="investigate", investigation_done=True), pr=None, frozen=True)
    assert d["action"] == "close_investigate"


def test_gate_investigate_missing_marker_nudge_once_then_park():
    d = _decide(issue=_issue(type="investigate"), pr={})
    assert d["action"] == "nudge" and d["nudge_key"] == "investigation"
    d2 = _decide(issue=_issue(type="investigate", nudged=["investigation"]), pr={})
    assert d2["action"] == "park"


def test_gate_already_merged_pr_is_a_noop_wait():
    # defensive: the runner shouldn't gate a merged PR, but if it does the safe answer is a
    # no-op wait (post-merge dev-checks polling owns this phase), never a second merge.
    d = _decide(pr=_pr(state="MERGED"))
    assert d["action"] == "wait"


def test_gate_externally_closed_pr_parks():
    # a CLOSED-not-merged PR under an open issue means a human intervened -> William decides
    d = _decide(pr=_pr(state="CLOSED"))
    assert d["action"] == "park"


def test_gate_step_order_sections_before_checks():
    # the §C.4 numbering is load-bearing: a PR failing BOTH sections and checks gets the
    # sections nudge (step 2) — the worker can fix the report while CI churns
    pr = _pr(statusCheckRollup=[{"context": "quality-gate", "state": "FAILURE"}])
    d = _decide(pr=pr, report="## Tests\nshort\n")
    assert d["action"] == "nudge" and d["nudge_key"] == "sections"


def test_gate_hold_before_freeze_before_checks():
    # step 3 (overlap hold) outranks step 4 (freeze), which outranks step 5 (checks)
    d = _decide(frozen=True, inflight={"i7": ["frontend"]})
    assert d["action"] == "hold" and d["overlap_lane"] == "i7"
    d2 = _decide(frozen=True,
                 pr=_pr(statusCheckRollup=[{"context": "quality-gate", "state": "PENDING"}]))
    assert d2["action"] == "hold"


def test_gate_corrupt_files_field_waits_never_merges():
    # Codex cross-review (Task 9): a wrong-typed files list once degraded to "no files" and
    # sailed past touch verification to merge. A corrupt view must WAIT (the runner refetches
    # the PR next tick), never merge unverified.
    d = _decide(pr=_pr(files="junk"))
    assert d["action"] == "wait"
    d2 = _decide(pr=_pr(files=None))
    assert d2["action"] == "wait"


def test_gate_corrupt_nudged_field_parks_not_loops():
    # fail-closed hunt: a wrong-typed `nudged` (corrupt issues.json) must land on park —
    # handing to William is safe; an endless nudge loop is not.
    d = _decide(issue=_issue(nudged="junk"), report="## Tests\nshort\n")
    assert d["action"] == "park"


def test_gate_never_mutates_its_inputs():
    # shared-mutable-default hunt: gate_decision is called in a loop over issues — it must not
    # mutate the views it is handed (a mutated inflight dict once leaked one issue's touches
    # into the next issue's decision in autocode's scheduler; keep the property pinned here).
    issue = _issue()
    pr = _pr(mergeable="CONFLICTING")
    cfg = _cfg()
    inflight = {"i7": ["db"]}
    import copy
    snap = copy.deepcopy((issue, pr, cfg, inflight))
    gate.gate_decision(issue, pr, GOOD_REPORT, cfg, False, inflight)
    assert (issue, pr, cfg, inflight) == snap


# --------------------------- fix_issue_fingerprint (post-merge red, L7-style) ---------------------------

def test_fingerprint_stable_across_noise():
    a = gate.fix_issue_fingerprint(
        "quality-gate", "FAIL tests/test_x.py::test_y at 2026-07-02T14:03:22Z after 3 retries "
                        "in /home/runner/work/repo/src/mod.py line 142")
    b = gate.fix_issue_fingerprint(
        "quality-gate", "FAIL tests/test_x.py::test_y at 2026-07-03T09:11:07Z after 5 retries "
                        "in /Users/ci/builds/repo/src/mod.py line 981")
    assert a == b
    assert isinstance(a, str) and len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_fingerprint_distinguishes_checks_and_content():
    same_summary = "ImportError: cannot import name gate"
    assert gate.fix_issue_fingerprint("quality-gate", same_summary) != \
        gate.fix_issue_fingerprint("review/local-gate", same_summary)
    assert gate.fix_issue_fingerprint("quality-gate", "ImportError: x") != \
        gate.fix_issue_fingerprint("quality-gate", "SyntaxError: y")


def test_fingerprint_tolerates_wrong_typed_input():
    fp = gate.fix_issue_fingerprint(None, None)
    assert isinstance(fp, str) and len(fp) == 16
