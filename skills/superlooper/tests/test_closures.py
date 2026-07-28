"""The accidental-close detector (issue #229): which CLOSED issues are only PRETENDING to be done?

The incident this pins: on 2026-07-16 a ledger DOCUMENTATION commit whose message read
"…fixes #189/#190…" auto-closed #189 — an approved, priority:high, never-built fix. The tracker
read COMPLETED; the files the fix names were untouched; the regression vector stayed open for a
day while recorded as fixed. CLOSED-as-COMPLETED was believed with no merged PR behind it.

Selection only. Nothing here reopens or edits anything — the janitor PROPOSES a reopen and the
owner's word executes it, the same propose/approve split every other debris class uses. The safety
contract is therefore the same one janitor.py states: every wrong-typed or unprovable input fails
CLOSED to "flag nothing", because a flag becomes a proposal to reopen the owner's own closed work.
"""
import closures


def _rec(num, *, reason="COMPLETED", closer=None, title="A thing", closed="2026-07-16T15:32:28Z"):
    return {"number": num, "title": title, "stateReason": reason, "closedAt": closed,
            "closer": closer}


def _commit(oid="8b79d7ac4f5cb0876e32ae839ab21237989195de", headline="ledger: 07-16 overnight",
            merged_prs=()):
    return {"type": "commit", "oid": oid, "headline": headline,
            "merged_prs": list(merged_prs)}


def _pr(num=242, merged=True):
    return {"type": "pull_request", "number": num, "merged": merged}


# --------------------------- the three DoD cases ---------------------------

def test_a_keyword_closed_unbuilt_issue_is_flagged():
    # the 2026-07-16 shape exactly: COMPLETED, closer is a bare commit, no merged PR carries it.
    found = closures.flagged([_rec(189, closer=_commit())])
    assert [f["num"] for f in found] == [189]
    f = found[0]
    assert f["commit"] == "8b79d7ac4f5cb0876e32ae839ab21237989195de"
    assert f["title"] == "A thing"
    # the `why` must NAME the closing commit — it becomes the janitor's audit comment, and an
    # audit comment that cannot be traced back to the offending commit is not evidence.
    assert "8b79d7ac" in f["why"] and "ledger: 07-16 overnight" in f["why"]


def test_an_issue_closed_by_its_own_merged_pr_is_not_flagged():
    # the healthy loop path: `Closes #N` in a merged PR body -> GitHub records the PR as closer.
    assert closures.flagged([_rec(242, closer=_pr(242, merged=True))]) == []


def test_an_owner_closed_issue_is_not_flagged():
    # closed by hand from the UI/CLI: GitHub records NO closer at all. The owner's own word is
    # the highest authority there is — never second-guess it.
    assert closures.flagged([_rec(98, closer=None)]) == []


# --------------------------- the gated-commit exemption ---------------------------

def test_a_commit_closer_carried_by_a_merged_pr_is_not_flagged():
    # a squash/merge commit whose message carried the keyword, landed THROUGH the gate: the work
    # shipped and CI judged it, so this is not the incident class even though the closer is a
    # Commit. Only a commit no merged PR carries is a bare keyword close.
    assert closures.flagged([_rec(150, closer=_commit(merged_prs=[240]))]) == []


def test_an_unmerged_associated_pr_does_not_exempt_the_commit():
    # a CLOSED-unmerged PR proves nothing landed through the gate. gh only reports merged ones,
    # but a wrong/legacy shape carrying a zero or a bool must not read as "a merged PR exists".
    assert [f["num"] for f in closures.flagged([_rec(151, closer=_commit(merged_prs=[0]))])] == [151]
    assert [f["num"] for f in closures.flagged([_rec(152, closer=_commit(merged_prs=[True]))])] == [152]


# --------------------------- not-planned / reopened ---------------------------

def test_a_not_planned_close_is_not_flagged():
    # NOT_PLANNED never claimed the work was done, so there is no false "fixed" to correct.
    assert closures.flagged([_rec(60, reason="NOT_PLANNED", closer=_commit())]) == []


def test_a_missing_or_wrong_typed_state_reason_is_not_flagged():
    for reason in (None, "", "completed", 7, ["COMPLETED"]):
        assert closures.flagged([_rec(61, reason=reason, closer=_commit())]) == []


# --------------------------- fail closed on every unprovable input ---------------------------

def test_wrong_typed_inputs_flag_nothing():
    assert closures.flagged(None) == []
    assert closures.flagged("nope") == []
    assert closures.flagged({}) == []
    assert closures.flagged([None, 7, "x", [], {}]) == []


def test_an_unreadable_closer_is_never_flagged():
    # a closer we could not parse is a state we could not READ; asserting an accidental close off
    # it would propose reopening the owner's work on no evidence at all.
    for closer in ("commit", 7, [], {"type": "commit"}, {"type": "commit", "oid": ""},
                   {"type": "commit", "oid": None, "merged_prs": []},
                   {"type": "unknown", "oid": "abc", "merged_prs": []}):
        assert closures.flagged([_rec(70, closer=closer)]) == []


def test_a_commit_with_an_unreadable_merged_pr_list_is_not_flagged():
    # can't prove no merged PR carries it -> don't propose. (Absent key and wrong type alike.)
    assert closures.flagged([_rec(71, closer={"type": "commit", "oid": "abc"})]) == []
    assert closures.flagged([_rec(72, closer={"type": "commit", "oid": "abc",
                                              "merged_prs": "240"})]) == []


def test_a_bad_issue_number_is_never_flagged():
    for num in (None, "189", 0, -3, True, 1.5):
        assert closures.flagged([_rec(num, closer=_commit())]) == []


def test_findings_are_deduped_and_sorted_by_issue_number():
    recs = [_rec(9, closer=_commit()), _rec(4, closer=_commit()), _rec(9, closer=_commit())]
    assert [f["num"] for f in closures.flagged(recs)] == [4, 9]


def test_flagged_never_mutates_its_input():
    recs = [_rec(189, closer=_commit())]
    before = repr(recs)
    closures.flagged(recs)
    assert repr(recs) == before


def test_a_missing_title_renders_as_empty_never_none():
    f = closures.flagged([{"number": 5, "stateReason": "COMPLETED", "closer": _commit()}])[0]
    assert f["title"] == "" and isinstance(f["headline"], str)


# --------------------------- the branch/PR evidence line (doctor's third column) ---------------

def test_evidence_says_so_when_no_sl_branch_or_pr_ever_existed():
    # the #189 shape: an approved fix that was never built leaves NO trace on the remote. This is
    # the sentence that tells the owner "this really was never built", not just "closed oddly".
    line = closures.evidence(189, prs=[], branches={})
    assert "no sl/i189" in line and ("PR" in line and "branch" in line)


def test_evidence_names_the_prs_that_did_exist():
    prs = [{"number": 240, "state": "CLOSED", "headRefName": "sl/i189-fix-the-thing"},
           {"number": 241, "state": "MERGED", "headRefName": "sl/i190-other"}]
    line = closures.evidence(189, prs=prs, branches={})
    assert "#240" in line and "CLOSED" in line
    assert "#241" not in line          # a DIFFERENT issue's PR is never evidence for this one


def test_evidence_names_a_branch_still_on_the_remote():
    line = closures.evidence(189, prs=[], branches={"sl/i189-fix": "abc123", "main": "d00d"})
    assert "sl/i189-fix" in line


def test_evidence_matches_generations_but_never_a_number_prefix():
    # sl/i5-x-r2 is issue 5's rebuild (janitor.branch_issue_num's convention); sl/i1890-... is a
    # DIFFERENT issue whose number merely starts with 189.
    prs = [{"number": 7, "state": "MERGED", "headRefName": "sl/i5-x-r2"},
           {"number": 8, "state": "OPEN", "headRefName": "sl/i1890-unrelated"}]
    assert "#7" in closures.evidence(5, prs=prs, branches={})
    assert "#8" not in closures.evidence(189, prs=prs, branches={})
    assert "sl/i1890-unrelated" not in closures.evidence(189, prs=[],
                                                         branches={"sl/i1890-unrelated": "a"})


def test_evidence_fails_closed_on_wrong_typed_inputs():
    # an unreadable evidence read must render as "none found", never raise into the doctor.
    for prs, branches in ((None, None), ("x", "y"), ([None, 7], {7: "x"}), ([{}], {"": ""})):
        assert isinstance(closures.evidence(189, prs=prs, branches=branches), str)
