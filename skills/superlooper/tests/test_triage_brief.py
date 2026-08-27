"""The brief a `t<N>` triage flight receives (issue #449).

The flight is an unattended session with no conversation and no person in it; this text is its
entire world, exactly as ``debugger-brief.md`` is a watchdog-launched debugger's. What it must
carry is settled by the standing rule
(``plugin/skills/superlooper/references/triage-standing-rule.md``) and by one rule of thumb this
repo pays for over and over: never TEACH in a brief what a gate can ENFORCE at the moment of the
mistake (#215, #225). So the brief POINTS at the rule for judgement, states the three musts that
have no gate behind them, and hands every write to a verb that holds the delegation's edges.

Two properties this suite exists for:

  * the pointer must resolve on a machine that has no plugin — the flight may be running on an
    adopted repo where the only copy of the rule is the one the gated installer published. That
    path is DERIVED from ``ops_docs``, never spelled here (the #197/D12 discipline).
  * the brief must render CLEAN: an unsubstituted ``{placeholder}`` reaching a session is an
    instruction it cannot follow.
"""
import re
from pathlib import Path

import ops_docs
import triage_run

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "skill" / "templates"
_BRIEF = _TEMPLATES / "triage-brief.md"

# Every placeholder the launcher substitutes. Listed here rather than derived, so ADDING one to the
# template without teaching the launcher to fill it fails this suite instead of a live flight.
_PLACEHOLDERS = ("{flight_id}", "{date}", "{repo_slug}", "{repo_path}", "{dev_branch}",
                 "{operator}", "{state_home}", "{run_log}", "{rubric}", "{recent_runs}",
                 "{verdicts}", "{pile}", "{ledger}", "{cli}", "{home_note}")

_FILL = {
    "flight_id": "t7", "date": "2026-08-27", "repo_slug": "o/r", "repo_path": "/Users/w/r",
    "dev_branch": "main", "operator": "willprout", "state_home": "/Users/w/.superlooper/o__r",
    "run_log": "/Users/w/.superlooper/o__r/triage/runs/2026-08-27.md",
    "rubric": triage_run.render_rubric({}),
    "home_note": "You are running in the repo's REAL checkout (`/Users/w/r`)... "
                 "It is **read-only to you** and it is **not evidence**.",
    "recent_runs": "### 2026-08-26\n\n- `triage_close` **#131** — overtaken\n",
    "verdicts": "- #131 -> overtaken (2026-08-26)\n",
    "pile": "- #140 *A nit* — no verdict recorded\n",
    "ledger": "#12", "cli": "superlooper",
}


def _render():
    return triage_run.render_brief(_BRIEF.read_text(encoding="utf-8"), _FILL)


def test_the_brief_template_exists_and_declares_every_placeholder():
    t = _BRIEF.read_text(encoding="utf-8")
    for ph in _PLACEHOLDERS:
        assert ph in t, "missing placeholder %s" % ph


def test_the_brief_renders_clean():
    out = _render()
    assert "{" not in out.replace("{}", ""), "unsubstituted placeholder left behind"
    assert "t7" in out and "/Users/w/r" in out and "willprout" in out
    assert "N3 Cost exceeds consequence" in out       # the rubric really landed inline


def test_the_brief_carries_the_powers_and_limits_by_pointer():
    """The RULE is the authority. The brief must route to it, and the route must resolve on a
    machine with no plugin — derived from the code that publishes it, never spelled here."""
    out = _render()
    assert "triage-standing-rule.md" in out
    # the source path in this repo's own checkout...
    assert "plugin/skills/superlooper/references/triage-standing-rule.md" in out
    # ...and the published mirror, where an adopted repo's machine actually has it
    mirror = dict(ops_docs.OPS_DOCS)[
        "plugin/skills/superlooper/references/triage-standing-rule.md"]
    assert "%s/%s" % ("/".join(ops_docs.MIRROR_REL), mirror) in out
    # it points AT the rule for the powers and limits rather than restating them
    low = out.lower()
    assert "powers" in low and "limits" in low


def test_the_brief_states_the_three_musts_that_have_no_gate_behind_them():
    out = _render()
    low = out.lower()
    # 1. fetch first, and judge staleness against origin/<dev> — never the working tree
    assert "fetch" in low
    assert "origin/main" in out
    assert "working tree" in low and "read-only" in low
    # 2. read the last three run logs and the verdicts file before acting
    assert "verdicts" in low
    assert re.search(r"last three run logs|three most recent run logs", low)
    # 3. a reopened issue is owner protest — never re-closed unless the body changed
    assert "reopen" in low and "protest" in low
    assert "unless" in low and "body" in low


def test_the_brief_names_the_session_class_and_that_nobody_is_watching():
    out = _render()
    low = out.lower()
    assert "unattended" in low
    assert "t7" in out
    # a flight is not a worker: no branch, no PR, no code
    assert "no pull request" in low or "open no pull request" in low
    assert "end the session" in low


def test_the_brief_routes_every_write_through_the_mechanical_verbs():
    """The brief may ASK; only a verb can REFUSE. Every write the delegation permits has one, and
    the brief must name them — a flight that reaches for `gh issue close` itself is outside every
    guard this issue built."""
    out = _render()
    for verb in ("triage-act", "triage-finish"):
        assert verb in out, "the brief must name the `%s` verb" % verb
    assert "--verdict" in out
    # every verdict in the rule's vocabulary is spelled for the flight
    for v in ("buildable", "underspecified", "contains-owner-decision", "overtaken",
              "duplicate-of-#", "nit("):
        assert v in out, "the brief must spell the %r verdict" % v


def test_the_brief_never_hands_the_flight_an_approval_word():
    """`agent-ready` is the owner's word (memory: it is his alone). The brief may name it only as
    a thing the flight must not touch — never inside a command it is being told to run."""
    out = _render()
    assert re.search(r"[Nn]ever apply or remove `agent-ready`", out), (
        "the brief must state the approval-label prohibition outright")
    assert "pre-authorized" in out
    # and it must never appear inside a verb invocation the flight is being handed
    for block in re.findall(r"```(.*?)```", out, re.S):
        assert "agent-ready" not in block and "pre-authorized" not in block, block
