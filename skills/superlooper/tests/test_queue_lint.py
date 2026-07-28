"""The mechanical issue contract, as one testable table (issue #225).

The 2026-07-16 queue audit found 25 of 35 open issues unschedulable or wedge-on-approval — 16 of
them `agent-ready` — because they lacked a `type:` label and/or a parseable `## Loop metadata`
section. The queue LOOKED full while most of it could never launch. This module is the one place
that says what "mechanically valid" means; every surface that must be loud about an invalid issue
(the PreToolUse deny, the runner's refusal journal, `doctor --repo`, the janitor's remediation, and
the dashboard's mirrored departures board) reads its verdict rather than re-deriving one.

Two disciplines the tests below pin, because both were paid for elsewhere in this repo:

* **`blocks_launch` is the RUNNER's verdict, not an opinion.** A defect is `blocks_launch` only
  when the runner would genuinely refuse or park over it. An unknown AREA name does not stop a
  launch (the gate's wander check catches it later), so it is reported and NOT marked blocking —
  the departures board mirrors the runner, and a board that cried INVALID over something the
  runner launches happily would be the #138 drift all over again, pointed the other way.
* **Unknown input fails OPEN, per dimension.** `areas=None` (no config in reach) means the area
  names cannot be judged, so they are not judged — the other dimensions still are.
"""
import pytest

import queue_lint


METADATA = "## Loop metadata\ntouches: engine\n"
AREAS = {"engine": ["skills/**"], "dashboard": ["dashboard/**"]}


def codes(*args, **kwargs):
    return [d["code"] for d in queue_lint.lint(*args, **kwargs)]


def one(*args, **kwargs):
    found = queue_lint.lint(*args, **kwargs)
    assert len(found) == 1, found
    return found[0]


# =============================== a valid issue passes untouched ===============================

def test_a_valid_issue_has_no_defects():
    assert queue_lint.lint(["type:build"], METADATA, areas=AREAS) == []


def test_every_valid_type_kind_passes():
    for kind in queue_lint.VALID_TYPES:
        assert queue_lint.lint(["type:" + kind], METADATA, areas=AREAS) == [], kind


def test_the_wildcard_touches_is_a_declaration_not_a_defect():
    # `touches: *` is an explicit unknown-scope declaration the engine already accepts (the
    # auto-restore-green issue files exactly this). It is never an unknown AREA.
    assert queue_lint.lint(["type:build"], "## Loop metadata\ntouches: *\n", areas=AREAS) == []


def test_an_investigation_needs_no_touches():
    # Investigations produce no PR and no merge, so touches are meaningless for them — the engine
    # exempts them at the launch park (actions._MERGE_PRODUCING_TYPES) and so must the lint.
    assert queue_lint.lint(["type:investigate"], "## Goal\nfind out why\n", areas=AREAS) == []


# =============================== the type: label ===============================

def test_no_type_label_is_a_blocking_defect_naming_the_three_kinds():
    d = one([], METADATA, areas=AREAS)
    assert d["code"] == "type_missing"
    assert d["blocks_launch"] is True
    for kind in queue_lint.VALID_TYPES:
        assert "type:" + kind in d["fix"]


def test_two_type_labels_name_both_offenders():
    d = one(["type:build", "type:investigate"], METADATA, areas=AREAS)
    assert d["code"] == "type_duplicate"
    assert d["blocks_launch"] is True
    assert "type:build" in d["what"] and "type:investigate" in d["what"]


def test_an_unknown_type_kind_names_the_offending_value_in_its_real_casing():
    d = one(["type:Build"], METADATA, areas=AREAS)
    assert d["code"] == "type_unknown"
    assert "type:Build" in d["what"]


def test_a_missing_type_label_offers_the_three_kinds_as_mechanical_choices():
    # The janitor's remediation can only ever propose a value it did not invent. The kind is the
    # owner's judgment, so the lint hands back the closed vocabulary and never picks one.
    assert one([], METADATA, areas=AREAS)["choices"] == list(queue_lint.VALID_TYPES)


# =============================== the control knobs (model:/effort:) ===============================

@pytest.mark.parametrize("labels,code", [
    (["type:build", "model:a", "model:b"], "model_duplicate"),
    (["type:build", "model:"], "model_blank"),
    (["type:build", "effort:a", "effort:b"], "effort_duplicate"),
    (["type:build", "effort:  "], "effort_blank"),
])
def test_a_control_label_conflict_is_a_blocking_defect(labels, code):
    d = one(labels, METADATA, areas=AREAS)
    assert d["code"] == code
    assert d["blocks_launch"] is True


# =============================== the ## Loop metadata section ===============================

def test_no_loop_metadata_section_at_all_is_a_blocking_defect():
    d = one(["type:build"], "## Goal\nship it\n", areas=AREAS)
    assert d["code"] == "touches_missing"
    assert d["blocks_launch"] is True
    assert "## Loop metadata" in d["fix"]
    assert "no `## Loop metadata` section at all" in d["what"]


def test_a_loop_metadata_section_with_no_touches_line_says_so_instead():
    # Same code (the repair is the same section, the same line), but the sentence must not claim a
    # missing heading the author can see is right there.
    d = one(["type:build"], "## Loop metadata\nparent: #7\n", areas=AREAS)
    assert d["code"] == "touches_missing"
    assert d["blocks_launch"] is True
    assert "declares no `touches:` line" in d["what"]


def test_a_bare_touches_line_OUTSIDE_the_section_is_its_own_defect():
    # The audit's commonest shape: the author wrote the declaration but not the heading, so
    # parse_loop_metadata reads nothing and the runner parks it. The VALUE is already there, which
    # is what makes this the one metadata defect a machine can repair without inventing anything.
    body = "## Goal\nship it\n\ntouches: engine, dashboard\n"
    d = one(["type:build"], body, areas=AREAS)
    assert d["code"] == "touches_outside_section"
    assert d["blocks_launch"] is True
    assert d["choices"] == ["engine, dashboard"]      # the author's own words, verbatim


def test_a_touches_line_under_a_DIFFERENT_h2_still_reads_as_outside_the_section():
    body = "## Loop metadata\nparent: #7\n\n## Notes\ntouches: engine\n"
    assert one(["type:build"], body, areas=AREAS)["code"] == "touches_outside_section"


def test_a_missing_touches_offers_the_repos_declared_areas_as_choices():
    d = one(["type:build"], "## Goal\nship it\n", areas=AREAS)
    assert d["choices"] == ["engine", "dashboard", "*"]


def test_touches_defects_stand_down_when_the_repo_does_not_require_them():
    assert queue_lint.lint(["type:build"], "## Goal\nship it\n",
                           areas=AREAS, touches_required=False) == []


def test_a_touches_defect_does_not_block_launch_when_the_type_is_already_broken():
    # The engine's `_needs_touches` gates on issues.eligible(), so an issue the runner refuses over
    # its TYPE never reaches the touches park. Both defects are reported; only the type one blocks.
    found = queue_lint.lint([], "## Goal\nship it\n", areas=AREAS)
    assert [d["code"] for d in found] == ["type_missing", "touches_missing"]
    assert [d["blocks_launch"] for d in found] == [True, False]


# =============================== the area names ===============================

def test_an_area_the_repo_never_declared_is_reported_but_does_not_block_launch():
    d = one(["type:build"], "## Loop metadata\ntouches: plugin\n", areas=AREAS)
    assert d["code"] == "touches_unknown"
    assert d["blocks_launch"] is False           # the runner launches it; the gate catches it later
    assert "plugin" in d["what"]
    assert d["choices"] == ["engine", "dashboard", "*"]


def test_every_undeclared_area_is_named_at_once():
    d = one(["type:build"], "## Loop metadata\ntouches: plugin, docs\n", areas=AREAS)
    assert "plugin" in d["what"] and "docs" in d["what"]


def test_one_good_area_beside_a_bad_one_still_reports_the_bad_one():
    d = one(["type:build"], "## Loop metadata\ntouches: engine, plugin\n", areas=AREAS)
    assert d["code"] == "touches_unknown"
    assert "plugin" in d["what"] and "engine" not in d["what"].split("—")[0].split(":")[-1]


def test_unknown_areas_are_NOT_judged_when_the_config_is_out_of_reach():
    # areas=None is "we could not read the repo's config" — the fail-OPEN posture the whole hook
    # rests on. The area dimension simply stands down; the others still answer.
    assert queue_lint.lint(["type:build"], "## Loop metadata\ntouches: plugin\n",
                           areas=None) == []
    assert codes([], "## Loop metadata\ntouches: plugin\n", areas=None) == ["type_missing"]


def test_a_repo_that_declares_no_areas_judges_no_area_name():
    # `areas: {}` is the loader's own default and a perfectly legitimate config. With nothing
    # declared, every name would be "unknown" — so the check stands down rather than flagging
    # every issue in the repo.
    assert queue_lint.lint(["type:build"], "## Loop metadata\ntouches: whatever\n",
                           areas={}) == []


# =============================== wrong-typed input never raises ===============================

@pytest.mark.parametrize("labels", [None, "type:build", 42, [None, 7, {"no": "name"}]])
def test_wrong_typed_labels_degrade_to_no_labels_and_never_raise(labels):
    assert "type_missing" in codes(labels, METADATA, areas=AREAS)


@pytest.mark.parametrize("body", [None, 42, {"body": "x"}, []])
def test_a_wrong_typed_body_degrades_to_empty_and_never_raises(body):
    assert "touches_missing" in codes(["type:build"], body, areas=AREAS)


@pytest.mark.parametrize("areas", ["engine", 42, ["engine"]])
def test_wrong_typed_areas_stand_the_area_check_down(areas):
    assert queue_lint.lint(["type:build"], "## Loop metadata\ntouches: plugin\n",
                           areas=areas) == []


def test_a_wrong_typed_touches_required_enforces():
    # Same fail-SAFE direction as actions._touches_required: a garbled config enforces rather than
    # silently launching a no-touches issue.
    assert codes(["type:build"], "## Goal\nx\n", areas=AREAS,
                 touches_required="yes") == ["touches_missing"]


# =============================== gh-issue shaped entry point ===============================

def test_lint_issue_reads_a_raw_gh_issue_dict():
    gh_issue = {"number": 284, "title": "t", "labels": [{"name": "needs-owner"}],
                "body": "## Goal\nx\n"}
    assert queue_lint.lint_issue(gh_issue, areas=AREAS) == \
        queue_lint.lint(["needs-owner"], "## Goal\nx\n", areas=AREAS)


def test_lint_issue_tolerates_a_non_dict():
    assert [d["code"] for d in queue_lint.lint_issue(None, areas=AREAS)] == \
        ["type_missing", "touches_missing"]


# =============================== the one-line rendering every surface shares ===============================

def test_describe_names_the_defect_and_its_fix_on_one_line():
    line = queue_lint.describe(one([], METADATA, areas=AREAS))
    assert "\n" not in line
    assert "type:" in line


def test_blocking_says_whether_the_runner_would_refuse():
    assert queue_lint.blocking(queue_lint.lint([], METADATA, areas=AREAS)) is True
    assert queue_lint.blocking(
        queue_lint.lint(["type:build"], "## Loop metadata\ntouches: plugin\n",
                        areas=AREAS)) is False
    assert queue_lint.blocking([]) is False


def test_signature_is_a_stable_defect_set_identity():
    # The runner journals a refusal ONCE per defect SET: the signature is what makes "the same
    # complaint" recognizable across ticks, and a CHANGED complaint speak up again.
    a = queue_lint.lint([], METADATA, areas=AREAS)
    b = queue_lint.lint([], METADATA, areas=AREAS)
    c = queue_lint.lint(["type:build"], "## Goal\nx\n", areas=AREAS)
    assert queue_lint.signature(a) == queue_lint.signature(b)
    assert queue_lint.signature(a) != queue_lint.signature(c)
    assert queue_lint.signature([]) == ""


# =============================== the runner's shape: an ALREADY-parsed issue ===============================
# On a tick the runner holds issues.parse_issue's output, not a body. lint_parsed answers from
# exactly that — so the journal's complaint and the doctor's complaint are the same complaint.

def parsed(labels=("agent-ready", "type:build"), touches=("engine",)):
    return {"num": 5, "id": "i5", "labels": list(labels), "touches": list(touches),
            "type": "build", "blocked_by": [], "parent": None}


def test_lint_parsed_passes_a_valid_issue():
    assert queue_lint.lint_parsed(parsed(), areas=AREAS) == []


def test_lint_parsed_catches_a_missing_type_label():
    assert [x["code"] for x in queue_lint.lint_parsed(parsed(labels=("agent-ready",)),
                                                      areas=AREAS)] == ["type_missing"]
    # …and both halves at once when both are wrong (the audit's commonest pair).
    assert [x["code"] for x in queue_lint.lint_parsed(parsed(labels=("agent-ready",), touches=()),
                                                      areas=AREAS)] == ["type_missing",
                                                                        "touches_missing"]


def test_lint_parsed_catches_a_missing_touches_declaration():
    d = queue_lint.lint_parsed(parsed(touches=()), areas=AREAS)
    assert [x["code"] for x in d] == ["touches_missing"]
    assert d[0]["blocks_launch"] is True


def test_lint_parsed_never_claims_a_missing_section_it_cannot_see():
    # It has no body, so it must not assert WHERE the declaration is missing from — only that the
    # runner reads none. (lint(), which does have the body, is free to be specific.)
    d = queue_lint.lint_parsed(parsed(touches=()), areas=AREAS)[0]
    assert "at all" not in d["what"]


def test_lint_parsed_catches_an_undeclared_area():
    d = queue_lint.lint_parsed(parsed(touches=("plugin",)), areas=AREAS)
    assert [x["code"] for x in d] == ["touches_unknown"]
    assert d[0]["blocks_launch"] is False


def test_lint_parsed_exempts_an_investigation():
    assert queue_lint.lint_parsed(parsed(labels=("type:investigate",), touches=()),
                                  areas=AREAS) == []


@pytest.mark.parametrize("bad", [None, [], "i5", {"labels": None, "touches": "engine"}])
def test_lint_parsed_never_raises_on_a_wrong_typed_parsed_issue(bad):
    assert "type_missing" in [d["code"] for d in queue_lint.lint_parsed(bad, areas=AREAS)]


def test_lint_parsed_and_lint_agree_on_the_same_issue():
    body = "## Loop metadata\ntouches: plugin\n"
    assert [d["code"] for d in queue_lint.lint(["type:build"], body, areas=AREAS)] == \
           [d["code"] for d in queue_lint.lint_parsed(parsed(touches=("plugin",)), areas=AREAS)]


# =============================== the repair the janitor executes ===============================
# `with_touches` builds the body a metadata fix would write. It ADDS ONLY — it never deletes or
# rewrites an author's words — because this runs against someone else's issue on the owner's
# one-tap approval, and a repair that eats prose is worse than the defect it fixes.

def touched(body, value="engine"):
    return queue_lint.with_touches(body, value)


def test_with_touches_appends_a_whole_section_when_there_is_none():
    out = touched("## Goal\nship it\n")
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []
    assert "## Goal\nship it" in out                      # the author's words, untouched


def test_with_touches_fills_an_existing_loop_metadata_section():
    out = touched("## Goal\nx\n\n## Loop metadata\nparent: #7\n")
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []
    assert out.count("## Loop metadata") == 1             # never a second section
    assert "parent: #7" in out                            # the other fields survive


def test_with_touches_promotes_a_trailing_bare_line_in_place():
    # The audit's commonest shape, and the one repair that is purely a one-line INSERT: the author
    # wrote `touches:` last (where the template puts it) but not the heading above it.
    out = queue_lint.with_touches("## Goal\nship it\n\ntouches: engine, dashboard\n",
                                  "engine, dashboard")
    assert out == "## Goal\nship it\n\n## Loop metadata\ntouches: engine, dashboard\n"
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []


def test_with_touches_never_deletes_a_stray_line_it_cannot_promote():
    body = "## Goal\ntouches: engine\n\nmore prose\n"
    out = touched(body)
    assert "touches: engine" in out and "more prose" in out
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []


def test_with_touches_is_idempotent_on_an_already_valid_body():
    assert touched(METADATA) == METADATA


@pytest.mark.parametrize("body", [None, 42, "", []])
def test_with_touches_survives_a_wrong_typed_body(body):
    out = queue_lint.with_touches(body, "engine")
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []


def test_with_touches_refuses_an_empty_value_rather_than_writing_a_bare_line():
    # A blank declaration parses to nothing, so writing one would "repair" the issue into the same
    # defect while telling the owner it was fixed.
    assert queue_lint.with_touches("## Goal\nx\n", "") is None
    assert queue_lint.with_touches("## Goal\nx\n", None) is None
