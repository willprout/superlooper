"""The limitations ledger and its two upstream consumers (issue #450).

The ledger is the durable home for findings that are TRUE but not worth a lane, so a nit close is
a FILING rather than a loss (``plugin/skills/superlooper/references/triage-standing-rule.md`` is
the ruling of record). It lives as a GitHub ISSUE on purpose: changing it never needs a PR, a
review, or a merge — which is exactly what makes it usable by a triage flight that may only touch
GitHub.

What these pin:

  * the marker obeys the label contract — ``limitations-ledger`` is registered in
    ``lib/labels.py`` (so ``adopt`` creates it and ``gh`` will accept it) and is NOT
    runner-managed (the runner never applies it; ``adopt`` does, once, at scaffold time);
  * ``find_ledger`` confirms the marker in the PAYLOAD rather than trusting the ``--label``
    filter, picks the lowest-numbered candidate, and fails closed on garbage — because the
    alternative to "no ledger found" is adopt CREATING one, and a wrong answer here means a
    second ledger or a write into somebody else's issue;
  * the ledger body documents its own entry format: rubric line, the limitation's content, and a
    link to the closed source issue;
  * the two upstream consumers carry the consult-the-ledger line — the worker brief (before
    FILING a follow-up) and the cross-review instructions (before FLAGGING a finding). That
    upstream check is where the volume reduction happens: known nits stop being born.

Run from skills/superlooper:  python -m pytest tests/test_limitations_ledger.py
"""
import re
from pathlib import Path

import pytest

import brief
import config as configlib
import labels as labels_mod
import limitations

# tests/ -> superlooper -> skills -> <repo root>
_REPO = Path(__file__).resolve().parents[3]
_CROSS_REVIEW_SKILL = _REPO / "plugin" / "skills" / "cross-review" / "SKILL.md"
_STANDING_RULE = (_REPO / "plugin" / "skills" / "superlooper" / "references"
                  / "triage-standing-rule.md")


# --------------------------------------------------------------- the marker obeys the contract

def test_the_ledger_label_is_registered_in_the_vocabulary():
    """gh REFUSES to apply a label the repo does not have, and `issue create` is all-or-nothing —
    so an unregistered marker would mean adopt's ledger create fails ENTIRELY and the repo simply
    has no ledger (the #165/#337 defect class, reached from the filing end)."""
    registered = {name for name, _color, _desc in labels_mod.LABELS}
    assert limitations.LEDGER_LABEL in registered, (
        "limitations.LEDGER_LABEL (%r) must be registered in labels.LABELS — adopt creates the "
        "label set from that list, and gh refuses `issue create --label` for a label the repo "
        "lacks" % (limitations.LEDGER_LABEL,))


def test_the_ledger_label_is_never_runner_managed():
    """The RUNNER never applies this one — `adopt` does, exactly once, when it scaffolds the
    ledger. So it must not carry the '(runner-managed)' tag that drives the #160 boot migration:
    that migration exists for labels a running runner would otherwise fail to write every tick."""
    assert limitations.LEDGER_LABEL not in labels_mod.runner_managed_labels()


def test_the_ledger_label_has_a_colour_and_a_description():
    spec = labels_mod.label_spec(limitations.LEDGER_LABEL)
    assert spec is not None
    color, desc = spec
    assert re.fullmatch(r"[0-9a-f]{6}", color), "a label colour is a bare 6-digit hex"
    assert desc.strip(), "humans learn the vocabulary from label descriptions"


# --------------------------------------------------------------- find_ledger (the pure half)

def _candidate(num, label=None, pinned=True):
    return {"number": num,
            "labels": [{"name": label if label is not None else limitations.LEDGER_LABEL}],
            "isPinned": pinned}


def test_find_ledger_returns_the_marked_issue():
    found = limitations.find_ledger([_candidate(7)])
    assert found is not None and found["number"] == 7


def test_find_ledger_returns_none_when_nothing_carries_the_marker():
    """The `--label` filter is an ARGUMENT, not a guarantee: confirm the marker in the payload.
    A read that answered with unrelated issues must scaffold a ledger, never write into #101."""
    assert limitations.find_ledger([_candidate(101, label="type:build")]) is None
    assert limitations.find_ledger([]) is None


def test_find_ledger_picks_the_lowest_numbered_candidate():
    """A repo that somehow grew two ledgers must converge on the FIRST one, not flip-flop between
    adopt runs (each run would otherwise pin and print a different number)."""
    found = limitations.find_ledger([_candidate(90), _candidate(12), _candidate(45)])
    assert found["number"] == 12


def test_find_ledger_fails_closed_on_garbage():
    """Every wrong shape reads as 'no ledger here'. That is the SAFE direction only because adopt
    then scaffolds a fresh one — it never writes into an issue whose shape it could not read."""
    assert limitations.find_ledger(None) is None
    assert limitations.find_ledger("not a list") is None
    assert limitations.find_ledger([None, 5, "x"]) is None
    assert limitations.find_ledger([{"labels": [{"name": limitations.LEDGER_LABEL}]}]) is None, \
        "a candidate with no number is unusable — adopt could not link or pin it"
    assert limitations.find_ledger([{"number": "7",
                                     "labels": [{"name": limitations.LEDGER_LABEL}]}]) is None, \
        "a wrong-typed number is unusable too"
    assert limitations.find_ledger([{"number": 7, "labels": "nope"}]) is None


def test_find_ledger_reads_the_pin_state_it_was_given():
    assert limitations.find_ledger([_candidate(7, pinned=False)])["isPinned"] is False


# --------------------------------------------------------------- the body documents its format

def test_the_ledger_body_documents_the_entry_format():
    """DoD: the entry format lives on the ledger issue's OWN body — rubric line, the limitation's
    content, and a link to the closed source issue. Nowhere else: the ledger is the one artifact a
    triage flight can reach with no checkout."""
    body = limitations.ledger_body()
    lower = body.lower()
    assert "rubric" in lower, "an entry names the rubric line that made it a nit"
    assert "limitation" in lower, "an entry carries the limitation's own content"
    assert "closed" in lower and "issue" in lower, (
        "an entry links the CLOSED source issue, so the trail reads in both directions")
    assert re.search(r"#\d+", body), "the format must be SHOWN, with a worked example entry"


def test_the_ledger_body_names_the_default_rubric_lines():
    """The rubric is the standing rule's (N1–N4). Naming them on the ledger is what lets a filer
    write a correct entry without opening a checkout."""
    body = limitations.ledger_body()
    for line in ("N1", "N2", "N3", "N4"):
        assert line in body, "the default nit rubric line %s must be named on the ledger" % line


def test_the_ledger_rubric_lines_match_the_standing_rule_of_record():
    """One fact, two homes: the ruling of record and the body adopt writes. A rubric line that
    exists in one and not the other is an entry format nobody can satisfy."""
    rule = _STANDING_RULE.read_text(encoding="utf-8")
    ruled = set(re.findall(r"\*\*(N[1-9])\b", rule))
    assert ruled, "the standing rule must still carry a named nit rubric"
    on_ledger = set(re.findall(r"\b(N[1-9])\b", limitations.ledger_body()))
    assert ruled == on_ledger, (
        "the ledger body and %s disagree about the nit rubric: only-in-rule=%s only-on-ledger=%s "
        "— a filer reading the ledger would write an entry citing a line that does not exist (or "
        "miss one that does)" % (_STANDING_RULE.name, sorted(ruled - on_ledger),
                                 sorted(on_ledger - ruled)))


def test_the_ledger_body_is_stable_across_calls():
    """adopt writes this once, but a shared mutable default here would let one caller's edit ride
    into the next repo's ledger — the defect class this suite keeps catching."""
    first = limitations.ledger_body()
    assert limitations.ledger_body() == first
    assert isinstance(first, str) and first.strip()


def test_the_ledger_title_is_a_non_empty_single_line():
    assert limitations.LEDGER_TITLE.strip()
    assert "\n" not in limitations.LEDGER_TITLE


# --------------------------------------------------------------- consumer 1: the worker brief

def _cfg(tmp_home, **over):
    raw = {"repo": "acme/widget"}
    raw.update(over)
    return configlib._validate_and_fill(raw)


def _issue(**over):
    p = {"num": 123, "id": "i123", "title": "Fix the login redirect", "type": "build",
         "body": "## Goal\nx\n\n## Definition of done\n- [ ] y\n\n## Boundaries\nz\n\n"
                 "## Loop metadata\ntouches: frontend\n",
         "branch": "sl/i123-fix", "labels": ["agent-ready", "type:build"],
         "touches": ["frontend"], "blocked_by": [], "parent": None,
         "created_at": "2026-07-01T00:00:00Z", "priority": 2, "expedite": False}
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _sl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    return tmp_path


@pytest.mark.parametrize("itype", ["build", "investigate", "diagnose-and-fix"])
def test_the_worker_brief_carries_the_consult_the_ledger_line(_sl_home, itype):
    """Every session kind files follow-up issues, so every session kind must be told to check the
    ledger FIRST. This is where the volume reduction actually happens: a known-accepted limitation
    that is never filed costs no triage flight anything."""
    out = brief.build(_issue(type=itype), _cfg(_sl_home))
    footer = out.split("# Loop contract", 1)[1]
    assert limitations.LEDGER_LABEL in footer, (
        "the brief must name the ledger's marker label, or a worker cannot find the ledger")
    assert "ledger" in footer.lower()
    assert "before" in footer.lower()


def test_the_briefs_ledger_line_sits_with_the_filing_instruction(_sl_home):
    """Placement is the point: the line has to be read at the moment a worker is about to FILE,
    not buried in a house-rules paragraph three screens away (the point-of-error principle)."""
    footer = brief.build(_issue(), _cfg(_sl_home)).split("# Loop contract", 1)[1]
    scope = footer.split("**Scope.**", 1)[1].split("**Build.**", 1)[0]
    assert limitations.LEDGER_LABEL in scope, (
        "the consult-the-ledger line belongs in the Scope paragraph, beside 'becomes a NEW issue'")


def test_the_briefs_ledger_label_is_registered_vocabulary(_sl_home):
    """brief.py renders PROSE and deliberately dereferences no label constant, so the spelling in
    the footer is checked HERE — a typo would send every worker hunting a label no repo has."""
    footer = brief.build(_issue(), _cfg(_sl_home)).split("# Loop contract", 1)[1]
    named = set(re.findall(r"`([a-z][a-z0-9:-]*-ledger)`", footer))
    registered = {name for name, _color, _desc in labels_mod.LABELS}
    assert named, "the footer must name the ledger label in backticks"
    assert named <= registered, "the brief names a label labels.LABELS does not carry: %s" % (
        sorted(named - registered),)


# --------------------------------------------------------------- consumer 2: the cross-review

def test_the_cross_review_instructions_carry_the_consult_the_ledger_line():
    """Scoped to NOT-FLAGGING: the reviewer's job is unchanged except that an already-accepted
    limitation is not a new finding. It must not become an instruction to skip real review."""
    text = _CROSS_REVIEW_SKILL.read_text(encoding="utf-8")
    lower = text.lower()
    assert limitations.LEDGER_LABEL in text, (
        "the cross-review instructions must name the ledger's marker label so the reviewing agent "
        "can actually read the ledger")
    assert "limitations ledger" in lower
    assert "accepted" in lower, "the scoping word: an entry is an ACCEPTED limitation"
    assert re.search(r"not?\w*\s+(a\s+)?(new\s+)?finding|do not flag|don't flag|never flag", lower), (
        "the line must say what NOT to do with an accepted limitation: don't flag it as a finding")


def test_the_cross_review_ledger_label_is_registered_vocabulary():
    text = _CROSS_REVIEW_SKILL.read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z][a-z0-9:-]*-ledger)`", text))
    registered = {name for name, _color, _desc in labels_mod.LABELS}
    assert named, "the cross-review instructions must name the ledger label in backticks"
    assert named <= registered, (
        "the cross-review instructions name a label labels.LABELS does not carry: %s"
        % (sorted(named - registered),))


def test_the_cross_review_ledger_read_is_a_prompt_input_not_a_review_step():
    """The REVIEWER is a separate process with no conversation and (on the codex path) no reason to
    run gh. So the ledger is read by the agent ASSEMBLING the prompt and pasted IN — otherwise the
    line is an instruction to a process that cannot follow it."""
    text = _CROSS_REVIEW_SKILL.read_text(encoding="utf-8")
    assemble = text.split("### 2. Build the review prompt", 1)
    assert len(assemble) == 2, "the prompt-assembly step must still be findable"
    assemble = assemble[1].split("### 3.", 1)[0]
    assert limitations.LEDGER_LABEL in assemble, (
        "the ledger read belongs in the prompt-assembly step, so its entries reach the reviewer")
