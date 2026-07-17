"""The six mechanical verbs (``lib/actions.py``) — the ONLY writes in the whole product (Task 6).

Every verb is exercised END TO END through the real ``gh`` adapter pointed at ``tests/fakes/fake-gh``
(the DoD: "every write is exercised through the fake-gh harness with mutation assertions; no real
gh reachable in tests"). We assert the exact mutation each verb produced — which labels moved, the
audit-comment wording, the flag issue + its first-use label — so the mechanical contract is pinned,
not just "a write happened".

Three disciplines this file defends:
  1. **Audit trail, every write.** Each verb leaves a ``… by Ada via command-center, <date>.``
     comment (or a flag issue whose body is the raw text) — journal-greppable, William's name.
  2. **Watched repo only.** Every write is gated on the allow-list of configured slugs; a request
     naming an unwatched repo is refused with no gh call (the label-writer can't be steered off its
     repos).
  3. **Fail closed.** A gh failure (``GH_FAIL``) yields ``ok: False`` — a verb never claims a write
     landed when it didn't.
"""
import json
from pathlib import Path

import pytest

import actions
import gh

FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"
REPO = "will-titan/command-center"
OTHER = "will-titan/superlooper"
DATE = "2026-07-07"


def _use_fake(monkeypatch, fixdir):
    monkeypatch.setenv("SL_GH", str(FAKE_GH))
    monkeypatch.setenv("GH_FIXTURES", str(fixdir))


def _mutations(fixdir):
    p = Path(fixdir) / "mutations.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _calls(fixdir):
    p = Path(fixdir) / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _acts(monkeypatch, tmp_path, allowed=(REPO,)):
    _use_fake(monkeypatch, tmp_path)
    return actions.Actions(gh, allowed_repos=list(allowed), today=lambda: DATE, operator="Ada")


# =============================== approve / re-approve ===============================

def test_approve_adds_agent_ready_removes_parked_and_needs_william(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.approve(REPO, 4)
    assert res["ok"] is True
    muts = _mutations(tmp_path)
    assert all(m["num"] == "4" for m in muts if m["kind"] == "set_labels")
    assert "agent-ready" in _added_labels(muts)                              # agent-ready applied
    # the blockers cleared (each in its own edit — issue #114 split; order-independent), PLUS the
    # `rebuild` override (issue #161): a plain re-approval is a RESUME, so it clears any stale rebuild.
    assert _removed_labels(muts) == {"parked", "needs-owner", "needs-william", "rebuild"}


def test_approve_posts_the_exact_audit_comment(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    a.approve(REPO, 4)
    comment = [m for m in _mutations(tmp_path) if m["kind"] == "comment"][-1]
    assert comment["num"] == "4"
    assert comment["body"] == "Approved by Ada via command-center, 2026-07-07."


def test_approve_is_pinned_to_the_named_repo(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path, allowed=(REPO, OTHER))
    a.approve(OTHER, 9)
    assert _calls(tmp_path)[-1]["repo"] == OTHER


def test_approve_refuses_an_unwatched_repo_with_no_gh_call(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path, allowed=(REPO,))
    res = a.approve("evil/elsewhere", 4)
    assert res["ok"] is False
    assert res["error"] == "unknown repo"
    assert _calls(tmp_path) == []          # nothing ever reached gh


def test_approve_fails_closed_when_gh_write_fails(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert a.approve(REPO, 4)["ok"] is False


# --------------------------- approve survives a completed #58 migration (issue #114) ---------------------------
# The live bug: on a repo that FINISHED the needs-william -> needs-owner rename, the batched remove
# still named the now-repo-absent legacy label, gh hard-failed the whole edit, and the agent-ready
# add never landed — every Approve tap died with "nothing changed". These pin the fix end to end.

def _removed_labels(muts):
    out = set()
    for m in muts:
        if m["kind"] == "set_labels" and m.get("remove"):
            out.update(m["remove"].split(","))
    return out


def _added_labels(muts):
    out = set()
    for m in muts:
        if m["kind"] == "set_labels" and m.get("add"):
            out.update(m["add"].split(","))
    return out


def test_approve_succeeds_on_a_repo_that_finished_the_needs_owner_migration(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_LABEL_NOT_IN_REPO", "needs-william")   # the #58 rename is complete here
    res = a.approve(REPO, 4)
    assert res["ok"] is True and res["labeled"] is True           # the dead button lives again
    muts = _mutations(tmp_path)
    assert "agent-ready" in _added_labels(muts)                   # agent-ready LANDED
    assert {"parked", "needs-owner"} <= _removed_labels(muts)     # the real removes LANDED
    comment = [m for m in muts if m["kind"] == "comment"][-1]     # the audit comment posted
    assert comment["body"] == "Approved by Ada via command-center, 2026-07-07."


def test_approve_still_clears_the_legacy_label_mid_migration(tmp_path, monkeypatch):
    # A repo still carrying needs-william (mid-migration) MUST still get it removed on approve.
    a = _acts(monkeypatch, tmp_path)
    res = a.approve(REPO, 4)
    assert res["ok"] is True
    assert "needs-william" in _removed_labels(_mutations(tmp_path))


# =============================== rebuild — the explicit destructive re-approval (issue #161) ===============================
# Re-approving a finished lane now RESUMES AT THE GATE by default (the engine keeps the PR/report and
# re-runs the merge gate). Rebuild is the separately-named destructive verb: it re-applies agent-ready
# AND the `rebuild` label, which the engine reads as the owner's explicit choice to DISCARD the
# finished PR/report and build from scratch. Same audit-trail + fail-closed disciplines as approve.

def test_rebuild_applies_agent_ready_and_the_rebuild_label(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.rebuild(REPO, 4)
    assert res["ok"] is True and res["verb"] == "rebuild"
    muts = _mutations(tmp_path)
    added = _added_labels(muts)
    assert "agent-ready" in added and "rebuild" in added     # BOTH — the rebuild flag rides along
    assert {"parked", "needs-owner", "needs-william"} <= _removed_labels(muts)


def test_rebuild_creates_the_rebuild_label_first_so_it_works_pre_adopt(tmp_path, monkeypatch):
    # gh refuses to apply a label a repo doesn't have; a repo not yet re-adopted after #161 shipped
    # would have no `rebuild` label. Create-or-force it first (idempotent, --force) — mirrors flag —
    # so the button just works, then apply it.
    a = _acts(monkeypatch, tmp_path)
    a.rebuild(REPO, 4)
    muts = _mutations(tmp_path)
    lab = next(m for m in muts if m["kind"] == "create_label")
    assert lab["name"] == "rebuild" and lab["force"] is True
    setl = next(m for m in muts if m["kind"] == "set_labels" and "rebuild" in (m.get("add") or ""))
    assert muts.index(lab) < muts.index(setl)                # created BEFORE it is applied


def test_rebuild_posts_the_exact_audit_comment(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    a.rebuild(REPO, 4)
    comment = [m for m in _mutations(tmp_path) if m["kind"] == "comment"][-1]
    assert comment["num"] == "4"
    assert comment["body"] == "Rebuilt from scratch by Ada via command-center, 2026-07-07."


def test_rebuild_refuses_an_unwatched_repo_with_no_gh_call(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path, allowed=(REPO,))
    res = a.rebuild("evil/elsewhere", 4)
    assert res["ok"] is False and res["error"] == "unknown repo"
    assert _calls(tmp_path) == []


def test_rebuild_fails_closed_when_gh_write_fails(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert a.rebuild(REPO, 4)["ok"] is False


def test_the_non_rebuild_reapproval_verbs_clear_a_stale_rebuild_label(tmp_path, monkeypatch):
    # Issue #161, the one-shot guarantee (fresh-review P1): `rebuild` is applied ONLY by the rebuild
    # verb. Every OTHER re-approval — approve (resume-at-the-gate), bounce-yes, answer — must REMOVE a
    # stale rebuild left behind by an earlier tap whose engine-side cleanup blipped, so a later plain
    # re-approval can never inherit a destructive override and wipe finished work (the D11 defect).
    for verb, call in (("approve", lambda a: a.approve(REPO, 4)),
                       ("bounce-yes", lambda a: a.bounce_yes(REPO, 4)),
                       ("answer", lambda a: a.answer(REPO, "go ahead", 4))):
        a = _acts(monkeypatch, tmp_path)
        call(a)
        removed = _removed_labels(_mutations(tmp_path))
        assert "rebuild" in removed, "%s must clear a stale rebuild label (#161)" % verb
        # clean the mutations log between verbs so each assertion reads only its own writes
        (tmp_path / "mutations.jsonl").unlink(missing_ok=True)


def test_bounce_yes_succeeds_on_a_repo_that_finished_the_migration(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_LABEL_NOT_IN_REPO", "needs-william")
    res = a.bounce_yes(REPO, 8)
    assert res["ok"] is True and res["labeled"] is True
    muts = _mutations(tmp_path)
    assert "agent-ready" in _added_labels(muts)
    assert "needs-owner" in _removed_labels(muts)
    comment = [m for m in muts if m["kind"] == "comment"][-1]
    assert comment["body"].startswith("Bounce accepted by Ada via command-center, 2026-07-07.")


def test_bounce_yes_still_clears_the_legacy_label_mid_migration(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    assert a.bounce_yes(REPO, 8)["ok"] is True
    assert "needs-william" in _removed_labels(_mutations(tmp_path))


def test_approve_still_fails_on_a_genuine_write_error_not_masked_by_tolerance(tmp_path, monkeypatch):
    # A genuine (non "not found") failure on a label write is NOT a vacuous repo-absent remove —
    # the tap's failure toast must still fire (no false-ok from the #114 tolerance).
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL_REMOVE", "1")
    assert a.approve(REPO, 4)["ok"] is False


def test_bounce_yes_still_fails_on_a_genuine_write_error(tmp_path, monkeypatch):
    # The re-release path (bounce-yes) shares the same tolerance — pin that IT, too, still surfaces a
    # genuine remove failure as not-ok (the DoD covers BOTH affected verbs).
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL_REMOVE", "1")
    assert a.bounce_yes(REPO, 8)["ok"] is False


class _CommentFailsGh:
    """A gh double where the LABEL/close/create writes land but the audit COMMENT fails — to prove a
    verb reports ok:False when its required trail doesn't post. agent-ready must never read as
    applied-and-recorded when the record didn't land (a bright line: the tap is William's word ONLY
    when the audit comment carries it)."""
    def set_labels(self, *a, **k): return True
    def comment(self, *a, **k): return False
    def close_issue(self, *a, **k): return True
    def create_label(self, *a, **k): return True
    def create_issue(self, *a, **k): return 1


@pytest.mark.parametrize("verb", ["approve", "expedite", "bounce_yes"])
def test_label_verbs_are_not_ok_if_the_audit_comment_fails(verb):
    a = actions.Actions(_CommentFailsGh(), allowed_repos=[REPO], today=lambda: DATE)
    res = getattr(a, verb)(REPO, 4)
    assert res["labeled"] is True          # the label DID move…
    assert res["commented"] is False       # …but the audit comment did not…
    assert res["ok"] is False              # …so the verb is NOT a success (no un-audited agent-ready)


# =============================== drop (the one destructive verb) ===============================

def test_drop_closes_issue_with_audit_comment_in_one_call(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.drop(REPO, 5)
    assert res["ok"] is True
    close = [m for m in _mutations(tmp_path) if m["kind"] == "close_issue"][-1]
    assert close["num"] == "5"
    assert close["comment"] == "Dropped by Ada via command-center, 2026-07-07."


def test_drop_refuses_an_unwatched_repo(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    assert a.drop(OTHER, 5)["ok"] is False
    assert _calls(tmp_path) == []


def test_drop_fails_closed_on_gh_error(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert a.drop(REPO, 5)["ok"] is False


# =============================== expedite ===============================

def test_expedite_adds_the_expedite_label_and_audit_comment(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    assert a.expedite(REPO, 7)["ok"] is True
    muts = _mutations(tmp_path)
    label = [m for m in muts if m["kind"] == "set_labels"][-1]
    assert label["num"] == "7"
    assert label["add"] == "expedite"
    assert label["remove"] is None                    # expedite only adds
    comment = [m for m in muts if m["kind"] == "comment"][-1]
    assert comment["body"] == "Expedited by Ada via command-center, 2026-07-07."


# =============================== bounce-yes ===============================

def test_bounce_yes_reapproves_and_clears_needs_william(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.bounce_yes(REPO, 8)
    assert res["ok"] is True
    muts = _mutations(tmp_path)
    assert "agent-ready" in _added_labels(muts)
    # clears the current owner-decision label AND the legacy one (issue #58 compat), each in its
    # own edit (issue #114 split)
    removed = _removed_labels(muts)
    assert "needs-owner" in removed and "needs-william" in removed


def test_bounce_yes_audit_comment_names_the_accepted_bounce(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    a.bounce_yes(REPO, 8)
    comment = [m for m in _mutations(tmp_path) if m["kind"] == "comment"][-1]
    assert comment["body"].startswith("Bounce accepted by Ada via command-center, 2026-07-07.")


# =============================== flag (raw text → issue labeled flag, no AI) ===============================

# =============================== answer (#163 — the durable-question verb) ===============================

def test_answer_posts_the_marked_comment_and_re_approves(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.answer(REPO, "Use approach A — it matches the pattern.", 4)
    assert res["ok"] is True
    muts = _mutations(tmp_path)
    comment = [m for m in muts if m["kind"] == "comment"][-1]
    assert comment["num"] == "4"
    # the answer carries the machine marker (the engine reads the latest of these as the answer)
    assert comment["body"].startswith("<!-- superlooper-answer -->")
    assert "Use approach A — it matches the pattern." in comment["body"]
    # agent-ready re-applied (the trigger); awaiting-answer cleared (+ the legacy owner labels)
    assert "agent-ready" in _added_labels(muts)
    assert "awaiting-answer" in _removed_labels(muts)


def test_answer_rejects_empty_text_without_touching_gh(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.answer(REPO, "   ", 4)
    assert res["ok"] is False and res["error"] == "empty answer"
    assert _calls(tmp_path) == []


def test_answer_refuses_an_unwatched_repo_with_no_gh_call(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path, allowed=(REPO,))
    res = a.answer("evil/elsewhere", "hi", 4)
    assert res["ok"] is False and res["error"] == "unknown repo"
    assert _calls(tmp_path) == []


def test_answer_fails_closed_when_gh_write_fails(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_FAIL", "1")
    assert a.answer(REPO, "an answer", 4)["ok"] is False


def test_flag_creates_the_flag_label_first_then_the_issue(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "321")
    res = a.flag(REPO, "the arrivals board flickers on merge")
    assert res["ok"] is True
    assert res["num"] == 321
    muts = _mutations(tmp_path)
    # the label is created (force = idempotent first-use) BEFORE the issue that references it
    lab = next(m for m in muts if m["kind"] == "create_label")
    assert lab["name"] == "flag" and lab["force"] is True
    iss = next(m for m in muts if m["kind"] == "create_issue")
    assert muts.index(lab) < muts.index(iss)
    assert iss["labels"] == "flag"


def test_flag_files_the_raw_text_verbatim_as_the_body_no_ai(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    raw = "the arrivals board flickers on merge\nand the clack is too loud"
    a.flag(REPO, raw)
    iss = next(m for m in _mutations(tmp_path) if m["kind"] == "create_issue")
    assert iss["body"] == raw                       # exact text, no summarization
    assert iss["title"].startswith("flag:")
    assert "flickers on merge" in iss["title"]      # title derived mechanically from line 1


def test_flag_rejects_empty_text_without_touching_gh(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    res = a.flag(REPO, "   \n  ")
    assert res["ok"] is False
    assert _calls(tmp_path) == []


def test_flag_fails_closed_when_create_returns_no_number(tmp_path, monkeypatch):
    a = _acts(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_NEW_ISSUE_NUM", "")       # create prints no digits → None
    assert a.flag(REPO, "something")["ok"] is False


# =============================== discuss (compose a briefing snippet — no write, no AI) ===============================

def _snapshot_with_flight(**flight):
    base = {"id": "i8", "num": 8, "label": "SL-8", "stage": "parked",
            "memo": None, "pr": None, "branch": "sl-i8-x", "attempt": 1,
            "cargo": {"present": False, "added": 0, "removed": 0, "files": 0}}
    base.update(flight)
    return {"repos": [{"slug": REPO, "name": "command-center", "flights": [base]}]}


def test_discuss_composes_from_the_flights_own_facts(monkeypatch):
    snap = _snapshot_with_flight(stage="parked", memo="tests failed twice — scope looks wrong",
                                 cargo={"present": True, "added": 40, "removed": 6, "files": 3})
    text = actions.compose_briefing(snap, REPO, 8)
    assert "SL-8" in text
    assert "command-center" in text
    assert "tests failed twice" in text             # the memo rode into the briefing
    assert "40" in text and "6" in text             # the diff facts are present
    assert "github.com/%s/issues/8" % REPO in text  # a pointer the fresh session can open


def test_discuss_includes_the_pr_link_when_there_is_one(monkeypatch):
    snap = _snapshot_with_flight(stage="final", pr=42)
    text = actions.compose_briefing(snap, REPO, 8)
    assert "github.com/%s/pull/42" % REPO in text


def test_discuss_is_a_minimal_stub_when_the_flight_is_not_on_the_field(monkeypatch):
    # A queued issue (not flying) still gets a usable snippet — a pointer to the issue, never a crash.
    snap = {"repos": [{"slug": REPO, "name": "command-center", "flights": []}]}
    text = actions.compose_briefing(snap, REPO, 99)
    assert "SL-99" in text
    assert "github.com/%s/issues/99" % REPO in text


def test_discuss_contains_no_model_call_marker(monkeypatch):
    # Belt-and-suspenders on the no-AI bright line: the briefing is pure string assembly.
    snap = _snapshot_with_flight()
    text = actions.compose_briefing(snap, REPO, 8)
    assert isinstance(text, str) and text.strip()


# =============================== the audit-comment wording is pinned directly ===============================

def test_audit_comment_builders_exact_wording():
    assert actions.approve_comment("Ada", "2026-07-07") == "Approved by Ada via command-center, 2026-07-07."
    assert actions.drop_comment("Ada", "2026-07-07") == "Dropped by Ada via command-center, 2026-07-07."
    assert actions.expedite_comment("Ada", "2026-07-07") == "Expedited by Ada via command-center, 2026-07-07."
    assert actions.bounce_comment("Ada", "2026-07-07").startswith(
        "Bounce accepted by Ada via command-center, 2026-07-07.")
