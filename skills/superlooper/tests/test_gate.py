"""The mechanical ship gate (§C.4) as pure functions — failing tests first, one per numbered
step of the state machine. Two standing hunts from Session 1's reviews are negative-tested
throughout: shared mutable defaults, and fail-OPEN on wrong-TYPED (not just missing) input —
a corrupt view must always land on the safe action (wait/nudge/park), never on "merge" and
never on a raised exception into the tick.
"""
import gate

# Three distinct, well-formed head oids. HEAD is "the PR's current head" everywhere below;
# OTHER stands for a superseded (gen-1) head, THIRD for a later worker push.
HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
OTHER = "b9c8d7e6f5a41302f1e0d9c8b7a6958473625140"
THIRD = "c0ffee11deadbeef2222333344445555666677fe"


def _marker(sha):
    """The pinned review marker a worker posts: the verdict names the diff it reviewed."""
    return gate.pinned_review_marker(sha)


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
            "headRefOid": HEAD,
            "statusCheckRollup": [{"context": "quality-gate", "state": "SUCCESS"}],
            "files": [{"path": "src/components/Widget.tsx"}],
            "comments": [{"body": f"{_marker(HEAD)} fresh-agent review: "
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


def test_new_default_sections_flow_through_the_unchanged_check(tmp_path):
    # issue #57 DoD item 4: the DEFAULT list changed but the mechanical CHECK did not. Load the
    # shipped default via config (not a hardcoded literal) and prove report_sections_ok still enforces
    # it exactly: every required H2 present with real prose passes; dropping any one still fails.
    import json
    import config
    d = tmp_path / ".superlooper"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"repo": "me/tool"}))
    default = config.load(tmp_path)["report_required_sections"]
    good = "".join(f"## {s}\n{'x' * 50}\n" for s in default)
    assert gate.report_sections_ok(good, default) is True
    missing_last = "".join(f"## {s}\n{'x' * 50}\n" for s in default[:-1])
    assert gate.report_sections_ok(missing_last, default) is False
    # and an H2 that exists but is empty still fails (the >= SECTION_MIN_CHARS prose rule is intact)
    empty_bodies = "".join(f"## {s}\n\n" for s in default)
    assert gate.report_sections_ok(empty_bodies, default) is False


# --------------------------- review_evidence_ok (step 2b) ---------------------------

def test_review_evidence_ship_cmd_repo_owns_review():
    # ship_cmd set -> the repo pipeline owns review (eApp: ship.sh's diff-pinned review/local-gate)
    assert gate.review_evidence_ok(_cfg(ship_cmd="scripts/ship.sh"), [], HEAD) is True


def test_review_evidence_marker_comment_pinned_to_head():
    ok = [{"body": f"{_marker(HEAD)} reviewed; P0/P1: none"}]
    assert gate.review_evidence_ok(_cfg(), ok, HEAD) is True
    assert gate.review_evidence_ok(_cfg(), [], HEAD) is False
    # the marker must BEGIN the comment — quoting it mid-text (e.g. discussing the contract) is
    # not a verdict
    mid = [{"body": f"the gate wants {_marker(HEAD)} at the start"}]
    assert gate.review_evidence_ok(_cfg(), mid, HEAD) is False


def test_review_evidence_pin_may_be_abbreviated_and_is_case_insensitive():
    """`git rev-parse --short HEAD` is what a worker reaches for — a 7+ hex prefix of the head
    identifies the reviewed commit as unambiguously as the full oid on a single PR."""
    for pin in (HEAD, HEAD[:7], HEAD[:12], HEAD.upper()):
        c = [{"body": f"{_marker(pin)} reviewed"}]
        assert gate.review_evidence_ok(_cfg(), c, HEAD) is True, pin
    # a prefix of the WRONG sha never matches
    assert gate.review_evidence_ok(_cfg(), [{"body": _marker(OTHER[:7])}], HEAD) is False


def test_review_evidence_stale_pin_does_not_satisfy_the_gate():
    """The defect this issue exists for: a verdict for a superseded diff must stop counting."""
    stale = [{"body": f"{_marker(OTHER)} reviewed gen-1"}]
    assert gate.review_evidence_ok(_cfg(), stale, HEAD) is False
    assert gate.review_evidence_state(_cfg(), stale, HEAD) == "stale"


def test_review_evidence_unpinned_legacy_marker_fails_closed():
    """Back-compat is FAIL-CLOSED: an unpinned marker cannot prove which diff it reviewed, so it
    never satisfies the gate — the safe branch (nudge->park), never a silent merge."""
    legacy = [{"body": "<!-- superlooper-review --> reviewed; P0/P1: none"}]
    assert gate.review_evidence_ok(_cfg(), legacy, HEAD) is False
    assert gate.review_evidence_state(_cfg(), legacy, HEAD) == "unpinned"


def test_review_evidence_malformed_pin_is_unpinned_never_absent():
    """Fresh-review P1-3. A marker whose pin is unreadable must be diagnosed as a marker needing a
    REPIN — not as "no review evidence at all". The difference is a false-park loop: the "absent"
    nudge prints the marker to post, so a worker that posted an unexpanded `$(...)` (the natural
    result of `gh pr comment --body '<!-- ... -->'`, since single quotes do not substitute) would
    be told to post the exact text it just posted, repost it, and park."""
    for bad in ("$(git rev-parse HEAD)",            # the substitution gh never expanded
                gate.REVIEW_PIN_PLACEHOLDER,        # the placeholder pasted verbatim
                "abc12",                            # too short to be an oid
                "zzzzzzzz",                         # not hex
                "<HEAD-OID>"):                      # an angle-bracketed placeholder
        c = [{"body": f"<!-- superlooper-review sha={bad} --> reviewed"}]
        assert gate.review_evidence_state(_cfg(), c, HEAD) == "unpinned", bad
        assert gate.review_evidence_ok(_cfg(), c, HEAD) is False, bad


def test_review_evidence_a_sibling_marker_is_not_a_verdict():
    """Second fresh review, P2-a. Loosening the marker match to diagnose bad pins must not loosen
    WHICH marker counts: `<!-- superlooper-` is a family prefix, and a sibling must never vouch for
    a diff. The payload has to be whitespace-separated, or `superlooper-review-notes` reads as a
    full verdict — fail-OPEN on the one property this module protects."""
    for name in ("superlooper-review-notes", "superlooper-reviewer", "superlooper-review2"):
        c = [{"body": f"<!-- {name} sha={HEAD} --> not a verdict"}]
        assert gate.review_evidence_state(_cfg(), c, HEAD) == "absent", name
    # ...while the payload-less real marker still parses (and never raises on a None payload)
    assert gate.review_evidence_state(_cfg(), [{"body": "<!-- superlooper-review-->"}],
                                      HEAD) == "unpinned"


def test_review_evidence_a_marker_among_junk_pins_still_reads_stale_not_unpinned():
    """A readable pin that simply doesn't match is STALE (re-review the current diff); only the
    total absence of a readable pin is UNPINNED. Mixing the two must not mask a real stale pin."""
    c = [{"body": "<!-- superlooper-review --> legacy"},
         {"body": f"{_marker(OTHER)} reviewed gen-1"}]
    assert gate.review_evidence_state(_cfg(), c, HEAD) == "stale"


def test_no_engine_source_retypes_the_review_marker_by_hand():
    """Fresh-review P1-4: the "one source of truth" comment on pinned_review_marker() was false —
    all four teaching sites hardcoded the literal, so the drift it declared impossible was exactly
    as possible as before. Enforce the claim by ABSENCE (this repo's standing discipline): no
    engine source may spell the marker out; it renders from gate.pinned_review_marker() or it is a
    second copy waiting to drift out of step with the regex that parses it."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "skill"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "gate.py":
            continue                     # gate.py IS the source of truth (regex + renderer)
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "superlooper-review" in line:
                offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()[:70]}")
    assert not offenders, (
        "these render the review marker by hand instead of gate.pinned_review_marker():\n  "
        + "\n  ".join(offenders))


def test_no_worker_facing_text_tells_a_worker_to_post_a_substitution():
    """Second fresh review, P2-b: ADOPTING.md went on teaching `sha=$(git rev-parse HEAD)` after
    the fix round declared that form broken, because the *.py guard above could not see it.

    A file-wide grep is the wrong instrument — it cannot tell teaching the form from WARNING
    against it, and prose must stay free to say "do not write `sha=$(...)`". So guard the
    ARTIFACTS a worker actually receives instead: a worker who obeys a `$(...)` posts it
    unexpanded, pins nothing, and earns a nudge it cannot satisfy by obeying (the P1-3 loop).
    The brief's own half of this lives in test_brief.py, over brief.build()'s real output."""
    import actions
    for key, text in actions.NUDGE_MESSAGES.items():
        assert "$(" not in text, f"nudge {key!r} tells the worker to post a substitution"
    # ...and the two review nudges still teach the real marker, from the one source of truth
    for key in ("review", "review_stale"):
        assert gate.pinned_review_marker() in actions.NUDGE_MESSAGES[key]


def test_review_marker_taught_everywhere_is_the_marker_the_gate_parses():
    """Fresh-review P1-4. The helper claims to be the one source of truth for the string; prove the
    round trip so the claim is enforced, not asserted — the rendered marker must parse back."""
    rendered = gate.pinned_review_marker(HEAD)
    assert gate.review_evidence_ok(_cfg(), [{"body": rendered + " reviewed"}], HEAD) is True
    # ...and the placeholder form the briefs render is recognised as a marker needing a repin,
    # never as "no review evidence at all"
    placeholder = gate.pinned_review_marker()
    assert gate.REVIEW_PIN_PLACEHOLDER in placeholder
    assert gate.review_evidence_state(_cfg(), [{"body": placeholder}], HEAD) == "unpinned"


def test_review_evidence_unreadable_head_never_merges():
    """A head oid the runner could not read is a corrupt view, not a verdict — fail closed."""
    ok = [{"body": _marker(HEAD)}]
    for bad in (None, "", 42, "nothex", HEAD[:4]):
        assert gate.review_evidence_ok(_cfg(), ok, bad) is False, bad


def test_review_evidence_tolerates_wrong_typed_comments():
    junk = [None, 42, {"nobody": "x"}, {"body": None}]
    assert gate.review_evidence_ok(_cfg(), junk, HEAD) is False
    assert gate.review_evidence_ok(_cfg(), None, HEAD) is False


def test_review_evidence_carry_honors_a_runner_merge_update():
    """The runner's OWN mechanical merge-update moves the head without touching the authored
    diff. It records the carry {from: reviewed oid, to: new head}; the verdict rides across it,
    or every merge-updated PR would false-park on the review it actually has."""
    reviewed = [{"body": f"{_marker(OTHER)} reviewed"}]
    carry = {"from": OTHER, "to": HEAD}
    assert gate.review_evidence_ok(_cfg(), reviewed, HEAD, carry) is True
    # ...but the carry is bound to the head it was carried TO: a worker push past it re-stales.
    assert gate.review_evidence_ok(_cfg(), reviewed, THIRD, carry) is False
    # ...and a wrong-typed / half-written carry never rescues a stale pin (fail closed)
    for bad in (None, {}, {"from": OTHER}, {"to": HEAD}, {"from": 1, "to": HEAD}, "x"):
        assert gate.review_evidence_ok(_cfg(), reviewed, HEAD, bad) is False, bad


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
    assert v == {"wander": False, "overlap_lane": None, "overlap_wildcard": False}


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


def test_touch_verdict_overlap_wildcard_flag_names_the_no_match_cause():
    # issue #36: when the diff mapped to '*' (files in no declared `areas`), the hold is
    # wildcard-caused — the flag lets the runner journal WHY the merge is held.
    v = gate.touch_verdict([], ["*"], {"i7": ["db"]})
    assert v["overlap_lane"] == "i7" and v["overlap_wildcard"] is True
    # the lane side being the wildcard also counts (a no-touches in-flight lane holds every merge).
    v2 = gate.touch_verdict([], ["api"], {"i7": ["*"]})
    assert v2["overlap_lane"] == "i7" and v2["overlap_wildcard"] is True


def test_touch_verdict_named_overlap_is_not_wildcard():
    # a genuine named-area overlap holds the merge, but it is NOT the wildcard trap — the operator
    # declared these areas overlapping, so overlap_wildcard stays False.
    v = gate.touch_verdict(["frontend"], ["frontend"], {"i7": ["frontend", "db"]})
    assert v["overlap_lane"] == "i7" and v["overlap_wildcard"] is False


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
    # A CLEAN answered-empty comments read ([]) genuinely says "no review marker" — the nudge
    # ladder is intact, and it is NEVER an unread-hold (issue #78: only an ABSENT read waits).
    pr = _pr(comments=[])
    d = _decide(pr=pr)
    assert d["action"] == "nudge" and d["nudge_key"] == "review"
    assert d.get("comments_unread") is None
    d2 = _decide(issue=_issue(nudged=["review"]), pr=pr)
    assert d2["action"] == "park"


def test_gate_stale_verdict_never_merges_rebuilt_code():         # step 2b (issue #154)
    """THE reproduction (#154 DoD). The real 07-15 sequence, end to end at the gate:

      review at gen-1 head -> park -> reapprove -> a fresh worker rebuilds on the SAME branch
      and pushes new commits -> the PR head moves, the gen-1 review comment survives on the PR.

    Before diff-pinning, `review_evidence_ok` accepted any marker comment on the PR regardless of
    which diff it reviewed, so this decided `merge`: a gen-1 verdict mechanically vouching for
    gen-2 code no reviewer ever saw — the README bright line ("no verdict, no merge") silently
    void for every post-reapprove generation. It must now take the safe branch, never merge.
    """
    rebuilt = _pr(headRefOid=HEAD,                       # gen-2 head after the rebuild's push
                  comments=[{"body": f"{_marker(OTHER)} gen-1: reviewed, no P0/P1"}])
    d = _decide(pr=rebuilt)
    assert d["action"] != "merge"
    assert d["action"] == "nudge" and d["nudge_key"] == "review_stale"
    # and it still marches to park rather than nudging forever
    d2 = _decide(issue=_issue(nudged=["review_stale"]), pr=rebuilt)
    assert d2["action"] == "park"


def test_gate_matching_verdict_still_merges():                   # step 2b (issue #154)
    """The other half of the DoD: pinning must not block the unchanged-diff case — that reuse is
    exactly what Wave 2's 'resume at gate' will read."""
    d = _decide(pr=_pr(headRefOid=HEAD, comments=[{"body": f"{_marker(HEAD)} reviewed"}]))
    assert d["action"] == "merge"


def test_gate_merge_update_carry_keeps_a_reviewed_pr_merging():  # step 2b (issue #154)
    """The runner's own merge-update moves the head; the review it already has must ride across,
    or the gate would park PRs the runner itself updated."""
    pr = _pr(headRefOid=HEAD, comments=[{"body": f"{_marker(OTHER)} reviewed"}])
    d = _decide(issue=_issue(review_carry={"from": OTHER, "to": HEAD}), pr=pr)
    assert d["action"] == "merge"


def test_gate_comments_absent_waits_never_nudges():              # step 2b (issue #78)
    # A REFUSED or starved comments read leaves the 'comments' key ABSENT from the PR view (the
    # runner attaches it ONLY on a clean CommentRead). The gate must WAIT for a trustworthy read,
    # never read the absence as "no review marker" and march the nudge ladder to park a finished,
    # reviewed build — the #21/#61 refused≠empty discipline, now closing the build gate's
    # comments-attachment surface. Mirrors step-3's unreadable-files WAIT.
    pr = _pr()
    del pr["comments"]                                           # comments never attached (refused)
    d = _decide(pr=pr)
    assert d["action"] == "wait" and d.get("comments_unread") is True
    # ...and even after the review nudge key was already spent, absence still WAITs (never parks):
    # a partial dead zone that opens AFTER the one nudge must not park a reviewed build.
    d2 = _decide(issue=_issue(nudged=["review"]), pr=pr)
    assert d2["action"] == "wait" and d2.get("comments_unread") is True


def test_gate_wrong_typed_comments_waits_never_nudges():         # step 2b, corrupt view (issue #78)
    # A wrong-typed comments field is a CORRUPT view, not an authoritative "no marker" — WAIT for
    # the runner's next-tick refetch, exactly like step-3's corrupt-files field.
    for junk in ("not-a-list", 7, {"body": "x"}):
        d = _decide(pr=_pr(comments=junk))
        assert d["action"] == "wait" and d.get("comments_unread") is True, junk


def test_gate_ship_cmd_repo_needs_no_marker_comment():           # step 2b, eApp path
    d = _decide(pr=_pr(comments=[]), cfg=_cfg(ship_cmd="scripts/ship.sh"))
    assert d["action"] == "merge"


def test_gate_ship_cmd_repo_ignores_absent_comments():           # step 2b, eApp path (issue #78)
    # a ship_cmd repo owns review itself, so an unread comments thread is moot — still merges,
    # never a spurious comments-unread WAIT.
    pr = _pr()
    del pr["comments"]
    d = _decide(pr=pr, cfg=_cfg(ship_cmd="scripts/ship.sh"))
    assert d["action"] == "merge" and d.get("comments_unread") is None


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


def test_gate_wildcard_hold_reason_names_the_no_match_cause():   # issue #36 (merge-hold journaling)
    # A diff whose files match no declared `areas` maps to '*' and holds the merge against EVERY
    # lane. The reason must SAY that (no-declared-area / wildcard) so "why is only one lane busy"
    # is answerable from the journal, and carry the structured overlap_wildcard flag.
    pr = _pr(files=[{"path": "random/loose-file.txt"}])
    d = _decide(pr=pr, inflight={"i7": ["db"]})
    assert d["action"] == "hold" and d["overlap_wildcard"] is True
    assert "areas" in d["reason"] and ("wildcard" in d["reason"] or "*" in d["reason"])


def test_gate_wildcard_hold_reason_when_the_inflight_lane_is_the_wildcard():
    # The mirror: our diff is well-declared, but an in-flight lane of unknown scope ('*') holds
    # every merge. The reason names the LANE as the no-touches wildcard.
    pr = _pr(files=[{"path": "src/components/Widget.tsx"}])      # maps to 'frontend'
    d = _decide(pr=pr, inflight={"i7": ["*"]})
    assert d["action"] == "hold" and d["overlap_wildcard"] is True
    assert "i7" in d["reason"]
    # P2-2 (fresh review): this branch is reached only when the lane declares the LITERAL '*', so
    # the reason must name that (unknown scope), not the inaccurate "declares no touches:".
    assert "touches: *" in d["reason"] and "no `touches:`" not in d["reason"]


def test_gate_named_overlap_hold_is_not_flagged_wildcard():
    # A genuine named-area overlap holds too, but it is the operator's declared affinity, not the
    # wildcard trap: overlap_wildcard stays False and the reason is the plain one.
    d = _decide(inflight={"i7": ["frontend"]})                  # our diff maps to 'frontend'
    assert d["action"] == "hold" and d["overlap_lane"] == "i7"
    assert d.get("overlap_wildcard", False) is False


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


def test_gate_reads_pr_required_set_from_split_config():         # step 5 (issue #52)
    # When required_checks is split {"pr":[...], "dev":[...]}, the merge gate evaluates the PR set,
    # NEVER the dev set. A check that is PR-required but excluded from the dev set still gates the PR.
    split = _cfg(required_checks={"pr": ["quality-gate", "ship"], "dev": ["quality-gate"]})
    # PR rollup: quality-gate green, `ship` MISSING -> the PR set is pending -> wait (never merge).
    # (If the gate wrongly read the dev set ["quality-gate"], this would merge — the regression.)
    d = _decide(cfg=split,
                pr=_pr(statusCheckRollup=[{"context": "quality-gate", "state": "SUCCESS"}]))
    assert d["action"] == "wait" and d.get("checks_pending") is True
    assert d["pending"] == {"unreported": ["ship"], "running": []}
    # both PR-required checks green -> the gate proceeds to merge (nothing else blocks in _pr()).
    d2 = _decide(cfg=split,
                 pr=_pr(statusCheckRollup=[{"context": "quality-gate", "state": "SUCCESS"},
                                           {"context": "ship", "state": "SUCCESS"}]))
    assert d2["action"] == "merge"


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


# --------------------------- referee pre-authorization (issue #165) ---------------------------
# A referee-path touch (.superlooper/**, .github/workflows/**) can only ever END in a needs-owner
# stop. When it is FORESEEABLE at approval (the issue declares a touch area that resolves to a
# referee glob), William can pre-authorize it up front — his word, recorded as the
# `pre-authorized:referee` label. The gate then CONSUMES that authorization and merges instead of
# re-parking at the finish line. Without the label, the bright line is untouched: an un-authorized
# referee diff still parks needs-william.

def test_preauthorized_referee_label_detected():
    assert gate.preauthorized_referee([gate.PREAUTHORIZED_REFEREE_LABEL]) is True
    assert gate.preauthorized_referee(["type:build", gate.PREAUTHORIZED_REFEREE_LABEL]) is True
    assert gate.preauthorized_referee(["type:build", "agent-ready"]) is False
    assert gate.preauthorized_referee([]) is False
    # fail closed on every wrong-typed label set — an unreadable set is NOT pre-authorized
    for bad in (None, "pre-authorized:referee", 42, [123, None], [{"name": "x"}]):
        assert gate.preauthorized_referee(bad) is False, bad


def test_foreseeable_referee_stop_from_declared_area():
    # a repo that schedules referee work declares a referee area; an issue that will touch referee
    # declares it in touches: — so the stop is computable from the DECLARATION alone, at approval.
    cfg = _cfg(areas={"frontend": ["src/components/**"], "loop_rules": [".superlooper/**"],
                      "ci": [".github/workflows/*.yml"]})
    assert gate.foreseeable_referee_stop(["loop_rules"], cfg) is True
    assert gate.foreseeable_referee_stop(["ci"], cfg) is True
    assert gate.foreseeable_referee_stop(["frontend"], cfg) is False
    assert gate.foreseeable_referee_stop([], cfg) is False
    # an area that merely COULD reach referee (a broad `.github/**`) is not CERTAIN -> not flagged
    assert gate.foreseeable_referee_stop(["broad"], _cfg(areas={"broad": [".github/**"]})) is False
    # fail closed / degrade to not-foreseeable on wrong-typed inputs (the gate's own diff-time
    # referee park stays the bright line for anything this misses)
    assert gate.foreseeable_referee_stop(["loop_rules"], {"areas": "junk"}) is False
    assert gate.foreseeable_referee_stop("junk", cfg) is False
    assert gate.foreseeable_referee_stop(["loop_rules"], None) is False


def test_gate_referee_path_preauthorized_merges():
    # THE issue #165 acceptance: a protected-path issue with a recorded pre-authorization merges
    # without a new finish-line park.
    pr = _pr(files=[{"path": ".superlooper/config.json"}])
    d = _decide(issue=_issue(pre_authorized_referee=True), pr=pr)
    assert d["action"] == "merge"
    assert d.get("referee_preauthorized") is True
    assert ".superlooper/config.json" in d.get("referee_paths", [])


def test_gate_referee_workflow_path_preauthorized_merges():
    pr = _pr(files=[{"path": ".github/workflows/quality.yml"}])
    d = _decide(issue=_issue(pre_authorized_referee=True), pr=pr)
    assert d["action"] == "merge" and d.get("referee_preauthorized") is True


def test_gate_referee_path_without_preauthorization_still_parks():
    # the bright line is untouched for un-authorized diffs: no label -> park needs-william, exactly
    # as before. (Boundaries: never auto-merge referee without his explicit pre-authorization.)
    pr = _pr(files=[{"path": ".superlooper/config.json"}])
    d = _decide(issue=_issue(pre_authorized_referee=False), pr=pr)
    assert d["action"] == "park" and d["needs_william"] is True
    assert d.get("referee_preauthorized") is not True


def test_gate_preauthorization_does_not_bypass_other_gates():
    # pre-authorization consumes ONLY the referee owner-stop; every other gate still applies, so a
    # failing check on a pre-authorized referee PR does NOT merge — it takes the checks ladder.
    pr = _pr(files=[{"path": ".superlooper/config.json"}],
             statusCheckRollup=[{"context": "quality-gate", "state": "FAILURE"}])
    d = _decide(issue=_issue(pre_authorized_referee=True), pr=pr)
    assert d["action"] == "nudge" and d["nudge_key"] == "checks"
    # and a frozen mainline still holds it
    d2 = _decide(issue=_issue(pre_authorized_referee=True), pr=pr, frozen=True)
    assert d2["action"] in ("hold", "nudge")   # never merge under a foreseeable non-referee stop


def test_gate_preauthorization_ignored_when_diff_touches_no_referee_path():
    # a pre-authorized issue whose diff DOESN'T touch referee just merges normally — the flag is
    # inert (no referee_preauthorized journal noise on an ordinary merge).
    d = _decide(issue=_issue(pre_authorized_referee=True))
    assert d["action"] == "merge" and d.get("referee_preauthorized") is not True
