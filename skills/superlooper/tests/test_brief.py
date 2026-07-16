"""Tests for lib/brief.py — the worker's entire world (§C.4 / plan Task 7).

The brief = the William-approved issue body VERBATIM + a rendered mechanical footer. The body is
never rewritten (Goal/DoD/Boundaries are approved text); the footer is type-specific (build,
investigate, diagnose-and-fix) and injects the repo's ship instructions, bright lines, and the
absolute marker paths the runner reads.

These pin: byte-identical body passthrough, every placeholder resolved, the ship_cmd/no-ship_cmd
rendering, the three type variants, bright-line injection, absolute state paths, and the two defect
classes Session 1 kept catching — shared mutable defaults / input mutation, and fail-OPEN on a
wrong-typed (not just missing) field.
"""
import pytest

import brief
import config as configlib
import gate


def _cfg(tmp_home, **over):
    """A minimal validated config for a repo, with state under a tmp SL_HOME."""
    raw = {"repo": "acme/widget"}
    raw.update(over)
    cfg = configlib._validate_and_fill(raw)
    return cfg


def _issue(**over):
    p = {
        "num": 123,
        "id": "i123",
        "title": "Fix the login redirect",
        "type": "build",
        "body": "## Goal\nMake login redirect to /home.\n\n## Definition of done\n- [ ] redirects\n\n"
                "## Boundaries\nDo not touch billing.\n\n## Loop metadata\ntouches: frontend\n",
        "branch": "sl/i123-fix-login-redirect",
        "labels": ["agent-ready", "type:build"],
        "touches": ["frontend"],
        "blocked_by": [],
        "parent": None,
        "created_at": "2026-07-01T00:00:00Z",
        "priority": 2,
        "expedite": False,
    }
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _sl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    return tmp_path


# --------------------------------------------------------------------------- body passthrough
def test_body_passes_through_byte_identical(_sl_home):
    p = _issue()
    out = brief.build(p, _cfg(_sl_home))
    assert p["body"] in out, "the William-approved body must appear verbatim, unrewritten"


def test_no_unresolved_placeholders_in_footer(_sl_home):
    # every {placeholder} in the FOOTER template must be substituted — a leftover brace means the
    # worker gets literal "{report_path}" instead of a path. Scoped to the footer (the William body
    # may legitimately contain braces that must pass through verbatim — see next test).
    import re
    out = brief.build(_issue(), _cfg(_sl_home))
    footer = out.split("# Loop contract", 1)[1]
    leftovers = re.findall(r"\{[a-z_]+\}", footer)
    assert leftovers == [], f"unresolved footer placeholders: {leftovers}"


def test_braces_in_body_pass_through_literally(_sl_home):
    # the William body is concatenated verbatim (never format()'d), so a {issue_num}/{branch} token
    # in a code sample in the body stays LITERAL — it must not be substituted.
    body = "## Goal\nEmit the literal tokens {issue_num} and {branch} in a code sample.\n"
    out = brief.build(_issue(body=body), _cfg(_sl_home))
    assert body in out, "body braces must pass through unsubstituted"


# --------------------------------------------------------------------------- ship instructions
def test_ship_instructions_with_ship_cmd(_sl_home):
    out = brief.build(_issue(), _cfg(_sl_home, ship_cmd="scripts/ship.sh --human-approved"))
    assert "scripts/ship.sh --human-approved" in out
    assert "EXCLUSIVELY" in out
    assert "gh pr create" not in out, "with a ship_cmd, do NOT tell the worker to open a PR directly"


def test_ship_instructions_without_ship_cmd(_sl_home):
    out = brief.build(_issue(), _cfg(_sl_home, ship_cmd=None))
    assert "gh pr create" in out
    # the marker the brief teaches must be the PINNED form the gate actually parses (#154) — an
    # unpinned verdict cannot prove which diff it reviewed and never satisfies the gate, so a
    # brief teaching the legacy form would walk every worker into a nudge->park.
    assert gate.pinned_review_marker() in out, \
        "no-pipeline repos must require a review verdict pinned to the reviewed head"
    assert "fresh-agent review" in out or "wrote none of it" in out
    # ...and it must NOT teach a shell substitution: `gh pr comment --body '<!-- ... -->'` needs
    # single quotes for a body full of `<!--`/`-->`, and single quotes never expand `$(...)`. A
    # worker following that literally posts unexpanded text that pins nothing (fresh-review P1-3).
    assert "$(git rev-parse HEAD)" not in out, \
        "the brief must teach paste-the-oid, not a substitution gh will not expand"


# --------------------------------------------------------------------------- type variants
def test_build_footer_has_ship_gate(_sl_home):
    out = brief.build(_issue(type="build"), _cfg(_sl_home))
    assert "Ship gate" in out
    assert "REAL browser" in out
    assert "SPLIT" not in out, "a plain build must not carry the diagnose-and-fix split clause"


def test_investigate_footer_replaces_ship_gate(_sl_home):
    out = brief.build(_issue(type="investigate"), _cfg(_sl_home))
    assert "<!-- superlooper-investigation -->" in out
    assert "parent: #123" in out, "child issues must carry parent metadata pointing at this issue"
    assert "needs-owner" in out
    # no ship gate / no PR for an investigation
    assert "Ship gate" not in out
    assert "REAL browser" not in out
    assert "gh pr create" not in out


def test_diagnose_and_fix_adds_split_clause(_sl_home):
    out = brief.build(_issue(type="diagnose-and-fix"), _cfg(_sl_home))
    assert "Ship gate" in out, "diagnose-and-fix still ships a fix when in scope"
    assert "SPLIT" in out
    assert "child issues" in out


def test_unknown_type_raises(_sl_home):
    # a mislabeled issue must never silently render a build brief — fail closed (the runner filters
    # invalid types before launch; reaching here is a bug worth surfacing loudly).
    with pytest.raises(ValueError):
        brief.build(_issue(type="invalid"), _cfg(_sl_home))


def test_investigate_has_no_pr_guidance(_sl_home):
    # cross-review Task 7: an investigation opens no PR, so NO PR instruction may leak anywhere in
    # its brief (the "assume + note in PR body" hint must point at the report instead).
    out = brief.build(_issue(type="investigate"), _cfg(_sl_home))
    assert "PR body" not in out
    assert "gh pr create" not in out
    assert "Open the PR" not in out
    assert "root-cause report" in out, "the assumption hint should point at the report instead"


# --------------------------------------------------------------------------- drive step (issue #101)
# The BUILD / diagnose-and-fix ship gate's "drive it" step must be honestly satisfiable by ANY repo,
# not just a web app. #57 fixed this same non-web mismatch for the report_required_sections DEFAULT;
# this fixes it in the brief PROSE. A CLI / API / library / service worker has no browser to drive,
# so a browser-ONLY step 2 left every non-web worker with an instruction it could not honestly
# follow (fudge it, or be stuck). The reworded step keeps the "drive the REAL feature end-to-end,
# not just the tests" intent AND still names a REAL browser for the web case (DoD: web repos still
# get a browser-drive instruction). diagnose-and-fix reuses the build ship gate, so both are pinned.

def _ship_gate(out):
    """The ship-gate block only (build / diagnose-and-fix): from the Ship gate header to the next
    footer section, so an unrelated footer token can't vacuously satisfy a drive-step assertion."""
    return out.split("**Ship gate", 1)[1].split("**Blocked?", 1)[0]


@pytest.mark.parametrize("itype", ["build", "diagnose-and-fix"])
def test_ship_gate_drive_step_is_honest_for_non_web(_sl_home, itype):
    gate = _ship_gate(brief.build(_issue(type=itype), _cfg(_sl_home)))
    assert "end-to-end" in gate, "the drive step must keep the drive-the-real-feature-end-to-end intent"
    assert any(tok in gate for tok in ("CLI", "API", "library", "service")), \
        "the drive step must name a non-web surface a non-web (CLI/library/service) repo can honestly drive"


@pytest.mark.parametrize("itype", ["build", "diagnose-and-fix"])
def test_ship_gate_drive_step_still_covers_web(_sl_home, itype):
    # DoD: web repos still get a browser-drive instruction — the reworded prose keeps a REAL browser
    # as the web-app case (this also keeps test_build_footer_has_ship_gate's "REAL browser" green).
    gate = _ship_gate(brief.build(_issue(type=itype), _cfg(_sl_home)))
    assert "REAL browser" in gate, "the web case must still name a real browser to drive"


# --------------------------------------------------------------------------- bright lines
def test_bright_lines_injected_verbatim(_sl_home):
    lines = ["Force-push is forbidden.", "Ship EXCLUSIVELY via scripts/ship.sh."]
    out = brief.build(_issue(), _cfg(_sl_home, bright_lines=lines))
    for ln in lines:
        assert ln in out, f"bright line not injected verbatim: {ln!r}"


def test_no_bright_lines_no_header(_sl_home):
    out = brief.build(_issue(), _cfg(_sl_home, bright_lines=[]))
    assert "Bright lines" not in out, "empty bright_lines must not emit an empty header"


def test_bright_line_with_brace_stays_literal(_sl_home):
    # cross-review Task 7: config prose is injected LAST, so a bright line containing a {placeholder}
    # token is passed through verbatim — never over-substituted with a real value.
    out = brief.build(_issue(), _cfg(_sl_home, bright_lines=["Never rebase onto {branch} by hand."]))
    assert "Never rebase onto {branch} by hand." in out


# --------------------------------------------------------------------------- house rules (universal footer)
# Two hard-won worker rules (incident 2026-07-07) live in the UNIVERSAL footer, so every adopted
# repo inherits them — not just the one repo whose CLAUDE.md first carried them:
#   1. Image/binary evidence goes under `reports/screenshots/`; only `.md` at the top level of
#      `reports/` (one loose binary there wedges the runner's every tick — 40+ min silent stall).
#   2. Never kill by name/pattern (`pkill -f`, `killall`) — that once collateral-killed the owner's
#      live dashboard; background processes are killed by recorded PID only.
# They live in the template body (not {work_block}), so they render for EVERY issue type.

def _footer(out):
    """The mechanical footer region only — the William body may legitimately contain these words.
    rsplit (not split) isolates the LAST occurrence, so a body/amendment that quotes the footer
    header can't smuggle body text into what we assert on."""
    return out.rsplit("# Loop contract", 1)[1]


@pytest.mark.parametrize("itype", ["build", "investigate", "diagnose-and-fix"])
def test_footer_screenshots_evidence_subdirectory_rule(_sl_home, itype):
    footer = _footer(brief.build(_issue(type=itype), _cfg(_sl_home)))
    assert "reports/screenshots/" in footer, "image/binary evidence must be routed to reports/screenshots/"
    assert "`.md`" in footer and "top level of `reports/`" in footer, \
        "the footer must state that only .md files belong at the top level of reports/"


@pytest.mark.parametrize("itype", ["build", "investigate", "diagnose-and-fix"])
def test_footer_never_kill_by_pattern_rule(_sl_home, itype):
    footer = _footer(brief.build(_issue(type=itype), _cfg(_sl_home)))
    assert "`pkill -f`" in footer and "`killall`" in footer, \
        "the footer must name the forbidden by-pattern kills (pkill -f, killall)"
    assert "kill only that PID" in footer, "the footer must require killing by recorded PID only"


@pytest.mark.parametrize("itype", ["build", "investigate", "diagnose-and-fix"])
def test_footer_house_rules_stay_agent_agnostic(_sl_home, itype):
    # DoD: the footer stays agent-agnostic — no Claude/Codex specifics, and no leak of the one
    # repo (command-center) whose CLAUDE.md first carried these rules. Parametrized over all three
    # types so the per-type {work_block} substitutions are also guarded against a future leak.
    footer = _footer(brief.build(_issue(type=itype), _cfg(_sl_home)))
    for tok in ("Claude", "Codex", "command-center", "Gemini", "Copilot"):
        assert tok not in footer, f"footer must stay agent/repo-agnostic — leaked {tok!r}"


# --------------------------------------------------------------------------- absolute paths
def test_marker_paths_are_absolute_under_state_home(_sl_home):
    cfg = _cfg(_sl_home)
    out = brief.build(_issue(), cfg)
    home = str(configlib.state_home(cfg))
    assert f"{home}/reports/i123.md" in out
    assert f"{home}/state/blocked/i123" in out
    assert f"{home}/state/awaiting/i123" in out


def test_report_sections_from_config(_sl_home):
    cfg = _cfg(_sl_home, report_required_sections=["Tests", "Restricted-data browser", "Review"])
    out = brief.build(_issue(), cfg)
    assert "Tests" in out and "Restricted-data browser" in out and "Review" in out


def test_branch_falls_back_to_deterministic_slug(_sl_home):
    # when the runner has not stamped a branch, build derives sl/<id>-<slug> so the brief is always
    # complete and the slug convention has ONE source of truth (brief.branch_for).
    p = _issue()
    del p["branch"]
    out = brief.build(p, _cfg(_sl_home))
    assert brief.branch_for(p) in out
    assert "sl/i123-" in out


# --------------------------------------------------- defect-class hunts (Session-1 recurring bugs)
def test_build_does_not_mutate_inputs(_sl_home):
    # no in-place mutation of the caller's parsed_issue / config (a shared-state bleed class).
    import copy
    p = _issue()
    cfg = _cfg(_sl_home, bright_lines=["one"])
    p_before, cfg_before = copy.deepcopy(p), copy.deepcopy(cfg)
    brief.build(p, cfg)
    assert p == p_before, "build must not mutate the parsed issue"
    assert cfg == cfg_before, "build must not mutate the config"


def test_wrong_typed_body_does_not_crash(_sl_home):
    # fail CLOSED, not open: a non-string body (broken gh field) renders an empty body region, never
    # raises. The footer must still render fully.
    for bad in (None, 123, ["not", "a", "string"], {"x": 1}):
        out = brief.build(_issue(body=bad), _cfg(_sl_home))
        assert isinstance(out, str) and "Loop contract" in out


def test_invalid_num_fails_closed(_sl_home):
    # cross-review Task 7: num is load-bearing (#N, parent: #N, Closes #N). A missing/wrong-typed num
    # would render "#None" and aim the worker at the wrong issue — raise, never fail open. bool is an
    # int subclass so True/False must be rejected too.
    for bad in (None, "123", True, 0, -5, 1.5):
        with pytest.raises(ValueError):
            brief.build(_issue(num=bad), _cfg(_sl_home))


def test_wrong_typed_report_sections_raises(_sl_home):
    # cross-review Task 7: report_required_sections is contractual (the gate checks these); a
    # wrong-typed value must fail closed, not silently render an empty "required H2s" contract.
    cfg = _cfg(_sl_home)
    cfg["report_required_sections"] = "oops not a list"
    with pytest.raises(ValueError):
        brief.build(_issue(), cfg)


def test_wrong_typed_bright_lines_does_not_crash(_sl_home):
    # a wrong-typed bright_lines (not a list) must not crash or leak a placeholder; treat as none.
    out = brief.build(_issue(), _cfg(_sl_home, bright_lines=[]))
    # force a wrong type post-validation (simulating a caller passing a raw dict)
    cfg = _cfg(_sl_home)
    cfg["bright_lines"] = "oops not a list"
    out2 = brief.build(_issue(), cfg)
    assert isinstance(out2, str) and "{bright_lines}" not in out2


# --------------------------- branch_for generations (Task 10, conflict regenerate) ---------------------------
# A conflict-regenerated rebuild can NOT reuse its branch name: the superseded PR stays open on
# that branch (nothing auto-closed), GitHub refuses a second PR with the same head, and a plain
# push to the preserved remote branch is refused (no force path exists anywhere). So each rebuild
# gets a generation suffix — still minted HERE, the single source of truth for branch names.

def test_branch_for_generation_zero_is_unchanged():
    p = {"num": 5, "id": "i5", "title": "Fix the widget"}
    assert brief.branch_for(p) == "sl/i5-fix-the-widget"
    assert brief.branch_for(p, generation=0) == "sl/i5-fix-the-widget"


def test_branch_for_generation_suffixes_rebuilds():
    p = {"num": 5, "id": "i5", "title": "Fix the widget"}
    assert brief.branch_for(p, generation=1) == "sl/i5-fix-the-widget-r1"
    assert brief.branch_for(p, generation=2) == "sl/i5-fix-the-widget-r2"


def test_branch_for_wrong_typed_generation_fails_closed_to_base():
    # bool is an int subclass (True would mint -r1 for a flag); wrong-typed/negative -> base name,
    # never an exception into the tick.
    p = {"num": 5, "id": "i5", "title": "Fix the widget"}
    for bad in (True, "1", None, -1, 1.5):
        assert brief.branch_for(p, generation=bad) == "sl/i5-fix-the-widget"


# ============================ post-approval owner comments (incident 2026-07-07 §8) ============================
# Comments William writes AFTER approving an issue (but before it launches) must reach the worker.
# The brief embeds the launch-time comment thread, with a trust rule that keeps HIS word binding:
# only the repo OWNER's comments are amendments; everyone else is attributed context, never
# instructions. Comment text is placed AFTER substitution (same protection the body gets) so a
# {placeholder} inside a comment stays literal. Fail CLOSED, never open: an ambiguous author is
# never promoted to a binding owner amendment.
#
# Owner = the login before the "/" in config's `repo` (the _cfg fixture uses "acme/widget").
#
# These key on the unique RENDERED section headings, not the bare phrase — the footer's Step 0
# also names the "Amendments posted after approval" block by its display name, so a bare-phrase
# match would be true for every brief. The `## ... (BINDING` / `### ... (context only` headings
# appear ONLY when the block is actually rendered.

_AMEND_HEADER = "## Amendments posted after approval (BINDING"
_CONTEXT_HEADER = "### Other comments (context only"


def _comment(login="acme", body="Cap the deck at 40 pages.",
             created="2026-07-07T10:00:00Z", assoc="OWNER"):
    """A gh `--json comments` entry shape: {author:{login}, authorAssociation, body, createdAt}."""
    return {"author": {"login": login}, "authorAssociation": assoc,
            "body": body, "createdAt": created}


_QA_HEADER = "## Owner's answers to your predecessor's questions (BINDING"


def test_qa_log_renders_the_full_question_and_answer(_sl_home):
    # #163: a relaunch after an owner's answer embeds the full Q&A so the fresh session inherits the
    # decision. Both the question AND the answer appear, in order, above the loop contract.
    qa = [{"question": "QUESTION: use approach A or B?", "answer": "Use A; B breaks migrations."}]
    out = brief.build(_issue(), _cfg(_sl_home), qa=qa)
    assert _QA_HEADER in out
    assert "QUESTION: use approach A or B?" in out
    assert "Use A; B breaks migrations." in out
    assert out.index("QUESTION: use approach A or B?") < out.index("Use A; B breaks migrations.")
    assert out.index(_QA_HEADER) < out.index("# Loop contract"), "Q&A is part of 'the issue above'"


def test_qa_log_multiple_pairs_are_numbered_in_order(_sl_home):
    qa = [{"question": "first?", "answer": "do X"}, {"question": "second?", "answer": "do Y"}]
    out = brief.build(_issue(), _cfg(_sl_home), qa=qa)
    assert out.index("first?") < out.index("do X") < out.index("second?") < out.index("do Y")


def test_qa_log_empty_answer_points_at_the_amendments(_sl_home):
    # A plain GitHub-client reply carried no marker, so the runner captured no answer text — the
    # worker is pointed at the amendments block (where the owner's reply is embedded as binding).
    out = brief.build(_issue(), _cfg(_sl_home), qa=[{"question": "q?", "answer": ""}])
    assert "q?" in out and _QA_HEADER in out
    assert "Amendments" in out.split(_QA_HEADER)[1].split("# Loop contract")[0]


def test_no_qa_log_leaves_the_brief_unchanged(_sl_home):
    base = brief.build(_issue(), _cfg(_sl_home))
    assert brief.build(_issue(), _cfg(_sl_home), qa=None) == base
    assert brief.build(_issue(), _cfg(_sl_home), qa=[]) == base
    assert _QA_HEADER not in base


def test_qa_log_fails_closed_on_garbage(_sl_home):
    # wrong-typed qa (not a list) and garbage entries are skipped, never raised, never rendered.
    base = brief.build(_issue(), _cfg(_sl_home))
    assert brief.build(_issue(), _cfg(_sl_home), qa="nonsense") == base
    out = brief.build(_issue(), _cfg(_sl_home),
                      qa=[{"question": "real?", "answer": "yes"}, "junk", {"no": "keys"}])
    assert "real?" in out and "yes" in out          # the good entry renders
    assert _QA_HEADER in out


def test_qa_braces_pass_through_literally(_sl_home):
    # like the body/comments, a {branch}/{issue_num} token inside a Q&A stays LITERAL.
    out = brief.build(_issue(), _cfg(_sl_home),
                      qa=[{"question": "touch {branch}?", "answer": "yes, {issue_num} only"}])
    assert "touch {branch}?" in out and "yes, {issue_num} only" in out


def test_owner_comments_embedded_as_binding_in_order(_sl_home):
    comments = [_comment(body="First amendment: cap the deck at 40 pages."),
                _comment(body="Second amendment: put the page number in the corner.")]
    out = brief.build(_issue(), _cfg(_sl_home), comments=comments)
    assert _AMEND_HEADER in out, "owner comments must render under a binding-amendments header"
    assert "First amendment: cap the deck at 40 pages." in out
    assert "Second amendment: put the page number in the corner." in out
    assert out.index("First amendment") < out.index("Second amendment"), "order must be preserved"
    assert "@acme" in out, "owner comments carry attribution (the author login)"
    assert "2026-07-07T10:00:00Z" in out, "owner comments carry attribution (the timestamp)"


def test_owner_amendments_land_above_the_loop_contract(_sl_home):
    # Step 0 says "Read the issue above" — amendments must sit in that region (before the footer),
    # so an owner amendment is part of "the issue above" the worker is told to read.
    out = brief.build(_issue(), _cfg(_sl_home),
                      comments=[_comment(body="An owner amendment.")])
    assert out.index("An owner amendment.") < out.index("# Loop contract")


def test_non_owner_comments_are_context_never_binding(_sl_home):
    comments = [_comment(login="randobot", assoc="NONE",
                         body="Please also add server-side telemetry.")]
    out = brief.build(_issue(), _cfg(_sl_home), comments=comments)
    assert "Please also add server-side telemetry." in out, "non-owner comments are still shown"
    assert _CONTEXT_HEADER in out, "a non-owner comment renders under a context-only header"
    assert _AMEND_HEADER not in out, "a non-owner comment must NOT create a binding-amendments block"
    assert "@randobot" in out


def test_owner_and_non_owner_land_in_their_own_sections(_sl_home):
    comments = [_comment(body="Owner's binding word."),
                _comment(login="somebot", assoc="NONE", body="A bot's suggestion.")]
    out = brief.build(_issue(), _cfg(_sl_home), comments=comments)
    # owner section first (binding), context section after — and each body under its own header
    assert out.index(_AMEND_HEADER) < out.index("Owner's binding word.") \
        < out.index(_CONTEXT_HEADER) < out.index("A bot's suggestion.")


def test_zero_comments_leaves_the_brief_unchanged(_sl_home):
    base = brief.build(_issue(), _cfg(_sl_home))               # comments=None (default)
    assert brief.build(_issue(), _cfg(_sl_home), comments=None) == base
    assert brief.build(_issue(), _cfg(_sl_home), comments=[]) == base, \
        "no comments must render byte-identically to the pre-fix brief"
    assert _AMEND_HEADER not in base and _CONTEXT_HEADER not in base
    # and the bare phrase appears NOWHERE (Step 0's amendments pointer is conditional on a block
    # actually rendering — a no-comment brief is byte-identical to the pre-comments footer).
    assert "Amendments posted after approval" not in base


def test_comment_braces_pass_through_literally(_sl_home):
    # SANITIZATION: comment text is embedded like the body (after all substitution), so a
    # {issue_num}/{branch} token inside a comment stays LITERAL — never over-substituted.
    out = brief.build(_issue(), _cfg(_sl_home),
                      comments=[_comment(body="Emit the literal tokens {issue_num} and {branch}.")])
    assert "{issue_num}" in out and "{branch}" in out, "comment braces must not be substituted"


def test_superlooper_marker_comments_are_skipped(_sl_home):
    # On these single-gh-identity repos the runner's OWN mechanical marker comments are owner-
    # authored; embedding `<!-- superlooper-review -->` as "William's binding amendment" would be
    # wrong. A comment whose body begins with a superlooper marker is never an amendment.
    comments = [_comment(body="<!-- superlooper-review -->\nReviewed the diff, LGTM."),
                _comment(body="A genuine owner amendment.")]
    out = brief.build(_issue(), _cfg(_sl_home), comments=comments)
    # the marker comment's payload is never embedded (the marker literal itself DOES appear in the
    # footer's ship instructions by design, so we key on the comment body, not the marker string).
    assert "Reviewed the diff, LGTM." not in out
    assert "A genuine owner amendment." in out


def test_relaunch_rebuild_picks_up_comments_fresh(_sl_home):
    # A regenerate/relaunch routes back through the launch path, which rebuilds the brief with the
    # CURRENT thread. build() must be stateless wrt comments: a later comment shows up on rebuild.
    out1 = brief.build(_issue(), _cfg(_sl_home), comments=[_comment(body="First requirement.")])
    out2 = brief.build(_issue(), _cfg(_sl_home),
                       comments=[_comment(body="First requirement."),
                                 _comment(body="Added after the first launch.")])
    assert "Added after the first launch." not in out1
    assert "Added after the first launch." in out2


# --------------------------------------------- fail CLOSED, never OPEN (the recurring defect class)
def test_wrong_typed_comments_arg_is_ignored(_sl_home):
    # a wrong-typed comments arg (not a list) must not crash or emit a section — treat as none,
    # identical to no comments (like a wrong-typed bright_lines).
    base = brief.build(_issue(), _cfg(_sl_home))
    for bad in ("not a list", 123, {"comments": []}):
        out = brief.build(_issue(), _cfg(_sl_home), comments=bad)
        assert out == base, f"wrong-typed comments {bad!r} must render like no comments"


def test_malformed_comment_entries_are_skipped_not_crashed(_sl_home):
    # a non-dict entry, or a comment with a non-string body, is skipped — never rendered, never a
    # crash (fail closed like a broken gh body region).
    comments = ["not a dict", 42, {"author": {"login": "acme"}, "body": 999},
                _comment(body="The one valid amendment.")]
    out = brief.build(_issue(), _cfg(_sl_home), comments=comments)
    assert isinstance(out, str) and "Loop contract" in out
    assert "The one valid amendment." in out


def test_ambiguous_author_is_never_promoted_to_binding(_sl_home):
    # THE fail-OPEN defect class: a comment whose author can't be read as the owner must NOT become
    # a binding amendment. author=None / missing login -> context at most, never binding.
    for author in (None, {}, {"login": None}, {"login": 123}, "acme"):
        out = brief.build(_issue(), _cfg(_sl_home),
                          comments=[{"author": author, "body": "Might look like William."}])
        assert "Might look like William." in out, "the text is still shown (as context)"
        assert _AMEND_HEADER not in out, \
            f"author={author!r} must not be trusted as the binding owner"


def test_amendments_bind_nothing_when_owner_underivable():
    # defense in depth on the amendment helper: with no clean "owner/name" repo there is no trusted
    # login, so every comment is context, nothing binding — never guess an owner. (build() itself
    # rejects a malformed repo even earlier, via state_home; this pins _amendments' own rule.)
    # The malformed-slug shapes (owner/, /repo, whitespace parts) are Codex cross-review 2026-07-07:
    # a lone slash-count check would have minted a trusted "owner" from "owner/" — fail OPEN.
    for repo in (None, "", "not-a-slug", "too/many/slashes", 123,
                 "owner/", "/repo", " owner/repo", "owner/ repo", " /repo"):
        block = brief._amendments([_comment(login="acme", body="Owner-shaped text.")],
                                  {"repo": repo})
        assert "Owner-shaped text." in block, f"repo={repo!r}: the text is still shown as context"
        assert _AMEND_HEADER not in block, f"repo={repo!r}: no derivable owner -> no binding block"
        assert _CONTEXT_HEADER in block


def test_owner_login_requires_a_clean_slug():
    # Codex cross-review 2026-07-07: the trust anchor must not be derivable from a malformed repo.
    assert brief._owner_login({"repo": "acme/widget"}) == "acme"
    for bad in (None, "", "acme", "a/b/c", "acme/", "/widget", " /widget", "acme/ ", 123, ["a/b"]):
        assert brief._owner_login({"repo": bad}) is None, f"repo={bad!r} must yield no owner"
    # a repo carrying stray whitespace yields an owner no real GitHub login can equal -> fail closed
    assert brief._owner_login({"repo": " acme/widget"}) == " acme"


def test_build_does_not_mutate_comments(_sl_home):
    import copy
    comments = [_comment(body="do not mutate me")]
    before = copy.deepcopy(comments)
    brief.build(_issue(), _cfg(_sl_home), comments=comments)
    assert comments == before, "build must not mutate the caller's comments list/entries"
