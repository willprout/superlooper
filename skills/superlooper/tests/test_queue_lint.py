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

import issues
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


# =============================== the provenance advisory (issue #400) ===============================
# `source:` names WHO FILED an issue. It is display-and-filter only: no scheduling, gating or
# approval decision may ever read it, and NOTHING may ever block on it (owner ruling 2026-08-06).
# So it is not part of the mechanical contract at all — it is an OPT-IN advisory the create-time
# surface asks for, and every existing issue is grandfathered by the opt-in being off everywhere
# else.

def test_the_contract_is_unchanged_when_advisories_are_not_asked_for():
    # The default posture, and the one that grandfathers the standing pile: an issue with no
    # `source:` label is exactly as valid as it was before #400, on every surface that does not
    # ask. Nothing retro-complains about the issues that were filed before the family existed.
    assert queue_lint.lint(["type:build"], METADATA, areas=AREAS) == []
    assert queue_lint.lint_parsed(parsed(), areas=AREAS) == []


def test_a_missing_source_label_is_reported_when_advisories_are_asked_for():
    found = queue_lint.lint(["type:build"], METADATA, areas=AREAS, advisories=True)
    assert [d["code"] for d in found] == ["source_missing"]
    d = found[0]
    assert d["blocks_launch"] is False        # never the runner's verdict...
    assert d["advisory"] is True              # ...and never ANY gate's verdict
    assert d["choices"] == []                 # nothing may guess which session kind filed it
    assert "source:" in queue_lint.describe(d)


def test_any_source_value_satisfies_the_advisory_because_the_family_is_open():
    # Adopters add their own values (a future `source:slackbot`) with no engine change, so the
    # advisory asks whether the family is present — never whether the VALUE is one the engine knows.
    for value in ("source:build", "source:qa", "source:slackbot"):
        assert queue_lint.lint(["type:build", value], METADATA, areas=AREAS,
                               advisories=True) == [], value


def test_the_advisory_never_blocks_and_never_reads_as_a_refusal():
    found = queue_lint.lint(["type:build"], METADATA, areas=AREAS, advisories=True)
    assert queue_lint.blocking(found) is False
    assert queue_lint.refusals(found) == []


def test_refusals_keeps_every_defect_that_is_not_an_advisory():
    # The two halves of the same list: a real contract defect stays a refusal even when an advisory
    # rides beside it, so a surface that filters advisories out never filters a real defect out too.
    found = queue_lint.lint([], METADATA, areas=AREAS, advisories=True)
    assert sorted(d["code"] for d in found) == ["source_missing", "type_missing"]
    assert [d["code"] for d in queue_lint.refusals(found)] == ["type_missing"]
    assert queue_lint.blocking(found) is True


def test_the_advisory_is_reported_last_so_real_defects_lead():
    found = queue_lint.lint([], "## Goal\nx\n", areas=AREAS, advisories=True)
    assert found[-1]["code"] == "source_missing"
    assert [d["code"] for d in found[:-1]] == ["type_missing", "touches_missing"]


def test_every_contract_defect_is_a_refusal_not_an_advisory():
    # The other direction, so a future defect cannot be quietly born advisory: everything the
    # mechanical contract reports is refusable, and only the #400 provenance notice is not.
    for labels_in, body in ([], METADATA), (["type:build"], "## Goal\nx\n"), \
                           (["type:build"], "## Loop metadata\ntouches: plugin\n"), \
                           (["type:build", "model:a", "model:b"], METADATA):
        for d in queue_lint.lint(labels_in, body, areas=AREAS):
            assert d["advisory"] is False, d
        assert queue_lint.refusals(queue_lint.lint(labels_in, body, areas=AREAS))


@pytest.mark.parametrize("bad", [None, "x", 5, [1, 2], {"a": 1}, [{"no": "name"}]])
def test_the_advisory_never_raises_on_wrong_typed_labels(bad):
    # Same fail-open-per-dimension discipline the rest of the module has: a garbage gh read reports
    # the advisory (it genuinely names no source), and NEVER raises out of a hook that would then
    # deny nothing at all.
    found = queue_lint.lint(bad, METADATA, areas=AREAS, advisories=True)
    assert "source_missing" in [d["code"] for d in found]
    assert queue_lint.lint_parsed(bad, areas=AREAS, advisories=True)


def test_lint_parsed_and_lint_issue_carry_the_advisory_through_too():
    assert [d["code"] for d in queue_lint.lint_parsed(parsed(), areas=AREAS, advisories=True)] \
        == ["source_missing"]
    assert queue_lint.lint_issue({"labels": [{"name": "type:build"}], "body": METADATA},
                                 areas=AREAS, advisories=True)[0]["code"] == "source_missing"
    assert queue_lint.lint_parsed(parsed(labels=("type:build", "source:build")),
                                 areas=AREAS, advisories=True) == []


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


def test_with_touches_repairs_the_section_the_PARSER_reads_not_the_first_one():
    # Same defect class the fresh-agent review found in the dashboard mirror (Codex, 2026-07-28),
    # here on the repair side. parse_sections is a DICT, so with two `## Loop metadata` headings
    # only the LAST is ever read. Writing into the first would report a fix that changes nothing —
    # the worst possible outcome for a one-tap repair on someone else's issue.
    body = "## Loop metadata\nparent: #7\n\n## Goal\nx\n\n## Loop metadata\nblocked-by: #9\n"
    out = touched(body)
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []
    assert out.count("touches:") == 1
    assert "parent: #7" in out and "blocked-by: #9" in out       # both sections survive intact


def test_with_touches_leaves_a_body_whose_LAST_section_already_declares_alone():
    body = "## Loop metadata\nparent: #7\n\n## Loop metadata\ntouches: engine\n"
    assert touched(body) == body


def test_with_touches_repairs_the_section_the_parser_reads_under_MIXED_CASE_headings():
    # Review round 2 (Codex, 2026-07-28): parse_sections keys by the RAW heading text, so
    # `## loop metadata` and `## Loop metadata` are two DISTINCT keys and parse_loop_metadata reads
    # the FIRST of them. Repairing the LAST would let the janitor execute, journal and comment a
    # successful body fix whose output still lints as broken — the worst outcome a one-tap repair
    # on someone else's issue can have.
    body = "## loop metadata\nparent: #1\n\n## Loop metadata\nparent: #2\n"
    out = touched(body)
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []
    assert "parent: #1" in out and "parent: #2" in out


def test_with_touches_clears_the_BLOCKING_defect_across_every_heading_shape():
    # The janitor's whole safety story rests on this: what it proposes is what lands, and what
    # lands is launchable. One table, every duplicate/casing/EOF shape the parser distinguishes.
    # A DECLARED area is used throughout, because that is the only kind of value the janitor may
    # ever pass — see the next test for what an author-written one leaves behind.
    for body in ["## Goal\nx\n",
                 "",
                 "## Loop metadata\nparent: #7\n",
                 "## loop metadata\nparent: #1\n\n## Loop metadata\nparent: #2\n",
                 "## Loop metadata\nparent: #1\n\n## Goal\nx\n\n## Loop metadata\nparent: #2\n",
                 "## LOOP METADATA\nparent: #1\n",
                 "## Goal\nx\n## Loop metadata\nparent: #7",
                 # a HALF-FILLED template: the heading and the key are there, the value is not.
                 # The audit was full of these, and the engine reads the LAST `touches:` line in the
                 # section — so a repair inserted above this one is silently overruled by it.
                 "## Loop metadata\ntouches:\n",
                 "## Loop metadata\ntouches:   \n",
                 "## Loop metadata\ntouches:\nparent: #7\n",
                 "## Goal\nship it\n\ntouches: engine\n"]:
        out = queue_lint.with_touches(body, "engine")
        assert queue_lint.lint(["type:build"], out, areas=AREAS) == [], repr(body)


def test_with_touches_refuses_a_value_that_is_nothing_but_separators():
    # `value.strip()` is truthy for "," so the empty-value guard never fired, and the verifier
    # then compared [] against [] and called it a successful repair — reopening the false-repair
    # loop one notch narrower (review round 2). A value that declares nothing is not a repair.
    for value in (",", ",,", " , , "):
        assert queue_lint.with_touches("## Goal\nx\n", value) is None, value
        assert queue_lint.with_touches("## Loop metadata\nparent: #7\n", value) is None, value


def test_a_blank_trailing_touches_line_still_earns_a_working_repair():
    # The audit's commonest shape. Promoting it in place would head a section whose only
    # declaration is still blank, so the verifier rejects it — and the issue would be left with no
    # repair on offer at all. It falls through to a freshly appended section instead.
    out = queue_lint.with_touches("## Goal\nShip it.\n\ntouches:\n", "engine")
    assert out is not None
    assert issues.parse_loop_metadata(out)["touches"] == ["engine"]
    assert queue_lint.lint(["type:build"], out, areas=AREAS) == []
    assert "## Goal\nShip it.\n" in out          # still additive — the author's words are intact


def test_with_touches_never_returns_a_body_its_OWN_parser_cannot_read():
    # The invariant behind every metadata proposal: what the janitor writes to someone else's issue
    # must be launchable, or it must not be written at all. A repair that lands, journals `ok` and
    # posts an audit comment saying `declared touches: engine` — while the issue stays exactly as
    # unlaunchable as before — is the worst outcome a one-tap repair can have, and it is worse than
    # offering no repair, because the owner believes the queue is fixed.
    shapes = ["## Loop metadata\ntouches:\n", "## Loop metadata\ntouches:\nparent: #7\n",
              "## Goal\nx\n", "", "## Loop metadata\nparent: #7\n",
              "## Goal\nship it\n\ntouches: engine\n", "## LOOP METADATA\ntouches:\n"]
    for body in shapes:
        for value in ("engine", "*", "engine, dashboard"):
            out = queue_lint.with_touches(body, value)
            if out is None:
                continue                       # refusing to repair is always allowed
            parsed = issues.parse_loop_metadata(out)["touches"]
            expected = [t.strip() for t in value.split(",") if t.strip()]
            assert parsed == expected, (repr(body), value, repr(out), parsed)


def test_with_touches_promoting_an_authors_own_UNDECLARED_area_leaves_the_advisory_standing():
    # Named so it stays a conscious choice (fresh-agent verification pass, 2026-07-28). For
    # `touches_outside_section` the value is not chosen from the repo's areas — it is the AUTHOR's
    # own words, which is exactly what makes that repair mechanical. If those words name an area the
    # repo never declared, the repair clears the defect that BLOCKS the launch and leaves the
    # non-blocking area advisory standing. That is the honest outcome, not a half-done fix: the
    # issue goes from unlaunchable to launchable, and the area question is one the janitor may not
    # answer (the right fix may be to declare `plugin` in .superlooper/config.json — a bright-line
    # file no automatic path may write).
    out = queue_lint.with_touches("## Goal\nx\n\ntouches: plugin\n", "plugin")
    found = queue_lint.lint(["type:build"], out, areas=AREAS)
    assert [d["code"] for d in found] == ["touches_unknown"]
    assert queue_lint.blocking(found) is False
