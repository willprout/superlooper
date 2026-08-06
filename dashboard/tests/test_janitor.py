"""Issue #121 — the Janitor verb: the command center's GitHub-side debris sweep, the THIRD button
in the local-command class the Tidy verb (issue #41) opened.

Like Tidy, the dashboard does not re-implement any GitHub logic: it drives the local
``superlooper janitor`` CLI, which owns the entire safety contract (the engine's ``lib/janitor.py``
pure selector). So this adapter (``lib/janitor.py``) mirrors ``lib/tidy.py``'s discipline — a
subprocess wrapper that NEVER raises, a hard timeout, fail-closed on any nonzero/garbage — while the
SEMANTICS it adds are pure and unit-tested: parsing the CLI's JSON envelope and grouping proposals
by kind for the front-end (design B.1, so the JS stays logic-free).

Two properties are load-bearing:

* **No real binary in tests.** The CLI writes GitHub (deletes branches, closes PRs/issues); a stray
  real call would touch a live repo. The conftest points ``SL_SUPERLOOPER`` at an absent path by
  default; these tests override it in-body to ``tests/fakes/fake-superlooper``.
* **Only the tapped subset is ever executed.** ``execute`` passes EXACTLY the selected keys via
  ``--execute-keys``; the fake records them, so a test proves nothing beyond the owner's taps runs.
"""
import json
from pathlib import Path

import pytest

import janitor as janitor_mod

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake-superlooper")
SLUG = "will-titan/command-center"
PATH = "/home/pat/code/command-center"          # a synthetic (non-William) checkout path


# =============================== the pure parsers / grouping (against the REAL CLI envelope) ===============================
# `superlooper janitor --json` prints ONE JSON object (bin/superlooper cmd_janitor):
#   {"ok": true, "proposals": [{kind,key,action,target,why(,head|title)}...],
#    "held": [...], "aged_park_days": 14}
# `--execute-keys` prints: {"ok": true, "results": [{key,outcome,reason?}...],
#    "executed", "failed", "skipped", "held"}

_REAL_PROPOSE = json.dumps({
    "ok": True,
    "proposals": [
        {"kind": "branch", "key": "branch:sl/i23-old", "action": "delete-branch",
         "target": "sl/i23-old", "why": "PR #40 merged — the work is on the mainline"},
        {"kind": "pr", "key": "pr:41", "action": "close-pr", "target": 41, "head": "sl/i18-x",
         "why": "open but superseded — replaced by a rebuild; the branch stays"},
        {"kind": "issue", "key": "issue:9", "action": "close-issue", "target": 9,
         "title": "flaky retry logic", "why": "parked and untouched for 30d (threshold 14d)"},
    ],
    "held": ["branch:sl/i7-x"],
    "aged_park_days": 14,
})

# The FOURTH debris class (issue #229, rendered here by issue #289): an issue closed as COMPLETED
# by a bare commit-message keyword, proposed for REOPEN. Copied from the engine's own emit — kind
# `issue-reopen`, key `reopen:<num>` (the two spellings deliberately differ; a close and a reopen of
# the same issue must never conflate in the refused map).
_REOPEN_PROPOSAL = {"kind": "issue-reopen", "key": "reopen:12", "action": "reopen-issue",
                    "target": 12, "title": "retry backoff never fires",
                    "commit": "abc1234",
                    "why": "closed as COMPLETED by commit abc1234 — a bare keyword, no PR"}


def test_parse_propose_reads_the_real_cli_envelope():
    doc = janitor_mod.parse_propose(_REAL_PROPOSE)
    assert doc["ok"] is True
    assert [p["key"] for p in doc["proposals"]] == ["branch:sl/i23-old", "pr:41", "issue:9"]
    assert doc["held"] == ["branch:sl/i7-x"]


def test_parse_propose_fails_closed_on_garbage():
    assert janitor_mod.parse_propose("")["ok"] is False
    assert janitor_mod.parse_propose("not json")["ok"] is False
    assert janitor_mod.parse_propose("[1, 2, 3]")["ok"] is False   # a list, not the envelope


def test_group_proposals_groups_by_kind_in_a_stable_order():
    doc = janitor_mod.parse_propose(_REAL_PROPOSE)
    groups = janitor_mod.group_proposals(doc["proposals"])
    assert [g["kind"] for g in groups] == ["branch", "pr", "issue"]
    assert all(g["label"] for g in groups)                      # every group names itself
    branch = groups[0]
    assert branch["items"][0]["key"] == "branch:sl/i23-old"
    assert branch["items"][0]["what"] == "delete branch sl/i23-old"
    assert "merged" in branch["items"][0]["why"]
    pr = groups[1]
    assert pr["items"][0]["what"] == "close PR #41 (sl/i18-x)"
    issue = groups[2]
    assert issue["items"][0]["what"].startswith("close issue #9")
    assert "flaky retry" in issue["items"][0]["what"]


def test_group_proposals_omits_empty_kinds_and_drops_wrong_typed_items():
    only_pr = [{"kind": "pr", "key": "pr:5", "action": "close-pr", "target": 5, "why": "w"},
               "garbage", {"kind": "branch"}, {"no": "key"}]   # last two: no valid key → dropped
    groups = janitor_mod.group_proposals(only_pr)
    assert [g["kind"] for g in groups] == ["pr"]
    assert [i["key"] for i in groups[0]["items"]] == ["pr:5"]


# =============================== the reopen class + the never-drop rule (issue #289) ===============================

def test_group_proposals_gives_the_reopen_kind_its_own_tile():
    # Issue #289: the CLI proposes and executes the accidental-close REOPEN end to end, but this
    # adapter's kind ordering knew only three kinds and dropped it — while still COUNTING it, so a
    # reopen-only sweep read as "apron's clear" on the owner's primary surface. It gets a tile.
    groups = janitor_mod.group_proposals([_REOPEN_PROPOSAL])
    assert [g["kind"] for g in groups] == ["issue-reopen"]
    assert groups[0]["label"], "the reopen tile must name itself"
    assert groups[0]["tappable"] is True, "a reopen is a one-touch approve-then-execute item"
    item = groups[0]["items"][0]
    assert item["key"] == "reopen:12"                 # the identity --execute-keys sends back
    assert item["what"] == "reopen issue #12: retry backoff never fires"
    assert "abc1234" in item["why"]


def test_group_proposals_keeps_the_reopen_tile_in_the_clis_own_emission_order():
    doc = janitor_mod.parse_propose(_REAL_PROPOSE)
    groups = janitor_mod.group_proposals(doc["proposals"] + [_REOPEN_PROPOSAL])
    assert [g["kind"] for g in groups] == ["branch", "pr", "issue", "issue-reopen"]


def test_group_proposals_surfaces_an_unrenderable_kind_instead_of_dropping_it():
    # The bug one layer up from #229's own: a kind this file has no tile for was silently dropped
    # while the count still included it. Now it is SHOWN — raw key plus the action the CLI named —
    # in a trailing group the dialog marks NOT tappable (the dashboard must not offer a tap whose
    # consequence it cannot state). `metadata` (issue #225) is exactly such a kind today.
    meta = {"kind": "metadata", "key": "meta:7:type=type:bug", "action": "add-label", "target": 7,
            "label": "type:bug", "choose_group": "issue:7:type", "why": "no type: label"}
    future = {"kind": "gremlin", "key": "gremlin:3", "action": "banish-gremlin", "target": 3,
              "why": "it is a gremlin"}
    groups = janitor_mod.group_proposals([_REOPEN_PROPOSAL, meta, future])
    assert [g["kind"] for g in groups] == ["issue-reopen", "other"]
    other = groups[1]
    assert other["tappable"] is False
    assert other["label"], "the catch-all group must name itself too"
    assert [i["key"] for i in other["items"]] == ["meta:7:type=type:bug", "gremlin:3"]
    # it names the raw facts the CLI gave it rather than inventing a verb it does not know
    assert other["items"][0]["what"] == "add-label 7"
    assert other["items"][1]["what"] == "banish-gremlin 3"


def test_unreadable_count_counts_exactly_what_the_grouping_cannot_place():
    # The residue: an entry with no string `key` cannot be tapped and cannot even name itself, so it
    # is not grouped — but it must not vanish either. It is COUNTED so the dialog can say plainly
    # that the sweep found more than it can show, instead of an all-clear.
    props = [_REOPEN_PROPOSAL, "garbage", {"kind": "branch"}, {"no": "key"}, {"key": 5}]
    assert janitor_mod.unreadable_count(props) == 4
    assert janitor_mod.unreadable_count([_REOPEN_PROPOSAL]) == 0
    assert janitor_mod.unreadable_count("not a list") == 0
    assert janitor_mod.unreadable_count(None) == 0
    # the invariant the dialog's all-clear depends on: every proposal is either shown or counted
    shown = sum(len(g["items"]) for g in janitor_mod.group_proposals(props))
    assert shown + janitor_mod.unreadable_count(props) == len(props)


def test_module_docstring_names_the_reopen_class_not_three_kinds():
    # Issue #290, absorbed here: the prose enumerating "three classes of debris" outlived the fourth.
    # Stale prose on a trusted surface is the same failure as a dropped tile, one layer up.
    doc = (janitor_mod.__doc__ or "").lower()
    assert "reopen" in doc, "the module docstring must name the accidental-close reopen class"


def test_held_rows_name_a_held_reopen():
    # A held-back reopen is reported by the CLI as the bare key `reopen:<num>`; the retry row must
    # state the consequence of tapping it, exactly as the other three kinds do.
    assert janitor_mod.held_rows(["reopen:12"]) == \
        [{"key": "reopen:12", "what": "reopen issue #12"}]


def test_held_rows_name_what_each_held_action_would_do():
    # Issue #131: a held-back key is now a RETRY target, so the panel must state the consequence of
    # tapping it — the same plain-language verb the proposal rows carry. The CLI reports `held` as
    # bare keys, so the verb is derived from the key's own kind prefix (server-side and pure; the JS
    # stays logic-free, design B.1). No safety rule is re-derived: the CLI still decides at act time.
    rows = janitor_mod.held_rows(["branch:sl/i7-x", "pr:41", "issue:9"])
    assert [r["key"] for r in rows] == ["branch:sl/i7-x", "pr:41", "issue:9"]
    assert [r["what"] for r in rows] == ["delete branch sl/i7-x", "close PR #41", "close issue #9"]


def test_held_rows_fall_back_to_the_raw_key_for_an_unrecognized_kind():
    # An unknown prefix must still be SHOWN (never silently dropped — a held action the owner
    # cannot see is a held action he cannot clear); it just names itself rather than inventing a verb.
    rows = janitor_mod.held_rows(["gremlin:7", "nocolon"])
    assert [r["what"] for r in rows] == ["gremlin:7", "nocolon"]


def test_held_rows_drop_wrong_typed_entries():
    assert janitor_mod.held_rows([None, 5, "", {"key": "pr:1"}, "pr:2"]) == \
        [{"key": "pr:2", "what": "close PR #2"}]
    assert janitor_mod.held_rows("branch:x") == []      # not a list → nothing
    assert janitor_mod.held_rows(None) == []


def test_propose_carries_held_rows_beside_the_raw_held_keys(jan_fix):
    # `held` (raw keys) stays exactly as it was — the old contract is not broken — and `held_items`
    # is added beside it. The front-end treats the presence of `held_items` as the server's proof
    # that it speaks the retry contract at all (an older server returns only `held`).
    verb, fixtures = jan_fix
    (fixtures / "propose.json").write_text(json.dumps(
        {"ok": True, "proposals": [], "held": ["branch:sl/i7-x", "pr:41"], "aged_park_days": 14}))
    res = verb.propose(SLUG)
    assert res["ok"] is True
    assert res["held"] == ["branch:sl/i7-x", "pr:41"]
    assert res["held_items"] == [
        {"key": "branch:sl/i7-x", "what": "delete branch sl/i7-x"},
        {"key": "pr:41", "what": "close PR #41"}]


def test_parse_execute_reads_results_and_counts():
    doc = janitor_mod.parse_execute(json.dumps({
        "ok": True, "results": [{"key": "pr:41", "outcome": "ok"}],
        "executed": 1, "failed": 0, "skipped": 0, "held": 0}))
    assert doc["ok"] is True and doc["executed"] == 1
    assert doc["results"][0]["outcome"] == "ok"


# =============================== the Janitor verb (against fake-superlooper) ===============================

@pytest.fixture
def jan_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_JANITOR_FIXTURES", str(tmp_path))
    # A deliberately bogus configured binary: a passing test PROVES the SL_SUPERLOOPER override
    # (the fail-closed fixture's only lever) wins over the configured path.
    verb = janitor_mod.Janitor("/nonexistent/configured-superlooper", {SLUG: PATH})
    return verb, tmp_path


def _calls(fixtures):
    p = fixtures / "calls.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _mutations(fixtures):
    p = fixtures / "mutations.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def test_propose_groups_the_proposals_the_cli_names(jan_fix):
    verb, _ = jan_fix
    res = verb.propose(SLUG)
    assert res["ok"] is True and res["verb"] == "janitor-propose"
    assert res["count"] == 3
    assert [g["kind"] for g in res["groups"]] == ["branch", "pr", "issue"]


def test_a_reopen_only_sweep_renders_a_tile_and_is_tappable(jan_fix):
    # The exact live shape issue #289 was filed on: a keyword-closed issue as the ONLY debris. The
    # CLI says it will reopen an issue; before this, the command center said there was nothing to
    # sweep. Now the tile is there, and tapping it drives the same --execute-keys path.
    verb, fixtures = jan_fix
    (fixtures / "propose.json").write_text(json.dumps(
        {"ok": True, "proposals": [_REOPEN_PROPOSAL], "held": [], "aged_park_days": 14}))
    res = verb.propose(SLUG)
    assert res["ok"] is True and res["count"] == 1
    assert res["unreadable"] == 0
    assert [g["kind"] for g in res["groups"]] == ["issue-reopen"]
    assert res["groups"][0]["items"][0]["key"] == "reopen:12"

    ex = verb.execute(SLUG, ["reopen:12"])
    assert ex["ok"] is True and ex["executed"] == 1
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[argv.index("--execute-keys") + 1] == "reopen:12"
    mut = [m for m in _mutations(fixtures) if m["kind"] == "janitor_execute"][-1]
    assert mut["keys"] == ["reopen:12"]


def test_propose_reports_the_proposals_it_could_not_read(jan_fix):
    # count vs unreadable is what lets the dialog refuse to say "apron's clear" while the sweep
    # found debris it cannot draw. A keyless entry is counted, never silently swallowed.
    verb, fixtures = jan_fix
    (fixtures / "propose.json").write_text(json.dumps(
        {"ok": True, "proposals": [{"kind": "branch"}], "held": [], "aged_park_days": 14}))
    res = verb.propose(SLUG)
    assert res["ok"] is True and res["count"] == 1
    assert res["groups"] == [] and res["unreadable"] == 1


def test_a_failed_propose_still_carries_the_unreadable_field(jan_fix, monkeypatch):
    # The front-end reads `unreadable` on every propose reply; the fail-closed envelope must carry
    # it too, so an error body can never be read as "1 proposal we couldn't draw".
    verb, _ = jan_fix
    monkeypatch.setenv("SL_JANITOR_FAIL", "1")
    assert verb.propose(SLUG)["unreadable"] == 0


def test_propose_invokes_json_against_the_right_repo_and_changes_nothing(jan_fix):
    verb, fixtures = jan_fix
    verb.propose(SLUG)
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["janitor", "--repo", PATH]
    assert "--json" in argv
    assert "--execute-keys" not in argv       # propose executes nothing
    assert _mutations(fixtures) == []          # a read: no janitor_execute recorded


def test_execute_passes_exactly_the_tapped_subset(jan_fix):
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["pr:41", "issue:9"])
    assert res["ok"] is True and res["executed"] == 2
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[:3] == ["janitor", "--repo", PATH]
    assert "--json" in argv
    i = argv.index("--execute-keys")
    assert argv[i + 1] == "pr:41,issue:9"      # exactly the tapped subset, comma-joined
    # the fake records the keys it was asked to execute — nothing beyond the taps
    mut = [m for m in _mutations(fixtures) if m["kind"] == "janitor_execute"][-1]
    assert mut["keys"] == ["pr:41", "issue:9"]


def test_execute_never_retries_a_held_action_unless_asked(jan_fix):
    # The holdback contract, unchanged (issue #121): an ordinary sweep must NOT carry
    # --retry-refused, so a key the CLI is holding back stays held. This is the guard that keeps
    # #131's retry from leaking into the normal path.
    verb, fixtures = jan_fix
    verb.execute(SLUG, ["pr:41"])
    assert "--retry-refused" not in _calls(fixtures)[-1]["argv"]


def test_execute_with_retry_passes_retry_refused(jan_fix):
    # Issue #131: the owner's explicit per-held-row Retry tap → the CLI's --retry-refused, alongside
    # the single tapped key. Nothing else changes: same --execute-keys subset, same fresh re-derive.
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["branch:sl/i7-x"], retry=True)
    assert res["ok"] is True
    argv = _calls(fixtures)[-1]["argv"]
    assert "--retry-refused" in argv
    assert argv[argv.index("--execute-keys") + 1] == "branch:sl/i7-x"


def test_retry_is_refused_for_anything_but_a_single_action(jan_fix):
    # Cross-review round 1 (medium): strict-boolean is not enough — the retry path is documented as
    # ONE held row's own deliberate tap, so the verb must enforce that shape too. A caller (or a
    # forged body) asking to retry a BATCH would widen the narrowest consent path in the dialog into
    # a bulk re-run of known-failing writes, so it is refused before any subprocess.
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["branch:sl/i7-x", "pr:41"], retry=True)
    assert res["ok"] is False and res["executed"] == 0
    assert "one" in res["error"]                   # names the actual rule, not a generic refusal
    assert _calls(fixtures) == []                  # nothing ran


def test_retry_counts_the_keys_that_survive_filtering(jan_fix):
    # The rule is measured on the FILTERED subset (the keys that would really be sent): garbage
    # alongside one real key still leaves exactly one action, so the retry proceeds.
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["", None, "branch:sl/i7-x"], retry=True)
    assert res["ok"] is True
    argv = _calls(fixtures)[-1]["argv"]
    assert "--retry-refused" in argv
    assert argv[argv.index("--execute-keys") + 1] == "branch:sl/i7-x"


def test_execute_retry_must_be_a_real_boolean_true(jan_fix):
    # Fail closed: a merely-truthy value (a forged/garbage body that survived the route) must not
    # arm a retry of a known-failing GitHub write. Only literal True does.
    verb, fixtures = jan_fix
    for truthy in ("yes", 1, ["x"]):
        verb.execute(SLUG, ["pr:41"], retry=truthy)
        assert "--retry-refused" not in _calls(fixtures)[-1]["argv"], truthy


def test_execute_with_no_selection_is_refused_before_any_subprocess(jan_fix):
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, [])
    assert res["ok"] is False and res["executed"] == 0
    assert res["error"]                        # a plain "nothing selected" message
    assert _calls(fixtures) == []              # nothing ran


def test_execute_ignores_wrong_typed_keys(jan_fix):
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, [None, 5, "", "pr:41"])   # only the one real key survives
    assert res["ok"] is True
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[argv.index("--execute-keys") + 1] == "pr:41"


def test_execute_drops_a_key_containing_a_comma(jan_fix):
    # keys are transported comma-joined; a comma-bearing key would round-trip wrong, so it fails
    # closed (dropped) — never split into two keys that could touch something the owner didn't tap.
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["branch:sl/foo,bar", "pr:41"])
    assert res["ok"] is True
    argv = _calls(fixtures)[-1]["argv"]
    assert argv[argv.index("--execute-keys") + 1] == "pr:41"   # only the clean key survives


def test_execute_with_only_comma_keys_is_refused(jan_fix):
    verb, fixtures = jan_fix
    res = verb.execute(SLUG, ["branch:a,b"])
    assert res["ok"] is False and _calls(fixtures) == []       # nothing left → refused, no subprocess


def test_unknown_repo_is_refused_before_any_subprocess(jan_fix):
    verb, fixtures = jan_fix
    assert verb.propose("someone/else")["error"] == "unknown repo"
    assert verb.execute("someone/else", ["pr:1"])["error"] == "unknown repo"
    assert _calls(fixtures) == []


def test_command_failure_surfaces_plainly_never_a_silent_success(jan_fix, monkeypatch):
    verb, _ = jan_fix
    monkeypatch.setenv("SL_JANITOR_FAIL", "1")
    prop = verb.propose(SLUG)
    assert prop["ok"] is False and prop["groups"] == [] and prop["error"]
    ex = verb.execute(SLUG, ["pr:41"])
    assert ex["ok"] is False and ex["error"]


def test_fail_closed_propose_envelope_surfaces_its_error(jan_fix):
    # the CLI's own fail-closed refusal (unreadable state) → {"ok": false, "error": ...} nonzero.
    verb, fixtures = jan_fix
    (fixtures / "propose.json").write_text(json.dumps(
        {"ok": False, "error": "state/issues.json exists but is unreadable"}))
    res = verb.propose(SLUG)
    assert res["ok"] is False and "unreadable" in res["error"]


def test_missing_binary_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_JANITOR_FIXTURES", str(tmp_path))
    verb = janitor_mod.Janitor("/nonexistent/configured", {SLUG: PATH})
    res = verb.propose(SLUG)
    assert res["ok"] is False
    assert "superlooper" in res["error"].lower() or "CLI" in res["error"]


def test_timeout_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_SUPERLOOPER", FAKE)
    monkeypatch.setenv("SL_JANITOR_FIXTURES", str(tmp_path))
    monkeypatch.setenv("SL_JANITOR_SLEEP", "2")
    verb = janitor_mod.Janitor("/nonexistent", {SLUG: PATH}, timeout=0.2)
    assert verb.propose(SLUG)["ok"] is False


def test_env_override_wins_over_configured_binary(jan_fix):
    verb, _ = jan_fix
    assert verb.propose(SLUG)["ok"] is True     # configured binary is bogus; still works
