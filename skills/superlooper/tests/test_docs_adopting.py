"""ADOPTING.md accuracy guards (issue #32).

ADOPTING.md is a newcomer's first contact with the loop, and two accuracy failures had
shipped in it:

  1. A stale parenthetical claiming the `adopt`/`doctor` commands "are built in a later
     task" — they are built and working, so a reader reasonably concludes the workflow is
     unavailable.
  2. A walkthrough whose step order (adopt -> doctor -> install the skill) guarantees a red
     `doctor`: `doctor` checks artifacts (the launch shim, the activity hooks) that only
     installation creates, and installation also provides the very `superlooper` binary the
     earlier steps invoke.

These tests pin the doc to the real CLI so it can't drift back: the documented commands must
exist, and the walkthrough must read publish/install -> adopt -> doctor -> run so that
following it verbatim reaches a green doctor before `run`.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# ADOPTING.md now rides the gated payload under `skill/` so it publishes to the stable path
# `~/.claude/skills/superlooper/docs/ADOPTING.md` (issue #85, design §6.3 / D9). It used to sit
# at `docs/ADOPTING.md` outside the payload; the relocation is what gives the `adopt` skill a
# published contract to route to on any machine where the CLI exists.
_DOC = _ROOT / "skill" / "docs" / "ADOPTING.md"
_CLI = _ROOT / "skill" / "bin" / "superlooper"


def _doc_text():
    return _DOC.read_text(encoding="utf-8")


def _registered_subcommands():
    """The subcommand names the CLI actually registers (its argparse ``add("name", ...)`` calls)."""
    src = _CLI.read_text(encoding="utf-8")
    return set(re.findall(r'add\("([a-z][a-z-]*)"', src))


def _code_spans(text):
    """Every fenced-code line and inline-code span in the doc.

    Real command invocations live in code formatting; prose does not. Collecting only code
    spans keeps a prose phrase like the H1 title "...into the superlooper loop" from being
    mistaken for a `superlooper loop` command invocation.
    """
    spans = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append(line)
        else:
            spans.extend(re.findall(r"`([^`]+)`", line))
    return spans


def _documented_subcommands(text):
    """Tokens invoked as ``superlooper <token>`` inside code formatting (fences / inline code)."""
    found = set()
    for span in _code_spans(text):
        for tok in re.findall(r"\bsuperlooper\s+([a-z][a-z-]*)", span):
            found.add(tok)
    return found


def _walkthrough(text):
    """The walkthrough section body: from its ``## ... walkthrough`` header to the next H2."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and "walkthrough" in line.lower():
            start = i
            break
    assert start is not None, "ADOPTING.md must have a '## ... walkthrough' section"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_no_stale_built_later_claim():
    # The commands are live today; any "built in a later task" framing is false and misleading.
    text = _doc_text().lower()
    assert "built in a later" not in text
    assert "built in a later task" not in text


def test_every_documented_command_is_a_real_subcommand():
    documented = _documented_subcommands(_doc_text())
    registered = _registered_subcommands()
    assert documented, "expected the doc to invoke `superlooper <cmd>` somewhere in code formatting"
    unknown = documented - registered
    assert not unknown, (
        "ADOPTING.md documents superlooper commands that the CLI does not register: "
        f"{sorted(unknown)} (registered: {sorted(registered)})"
    )
    # The walkthrough's core trio must each be a real subcommand AND be shown to the reader.
    for cmd in ("adopt", "doctor", "run"):
        assert cmd in registered, f"CLI unexpectedly lacks a `{cmd}` subcommand"
        assert cmd in documented, f"walkthrough must invoke `superlooper {cmd}`"


def test_walkthrough_orders_publish_before_adopt_before_doctor_before_run():
    wt = _walkthrough(_doc_text())
    # Publish/install must come first: it creates the launch shim + activity hooks that `doctor`
    # checks for, and links the `superlooper` command onto PATH that adopt/doctor/run all need.
    i_install = wt.find("install.sh")
    i_adopt = wt.find("superlooper adopt")
    i_doctor = wt.find("superlooper doctor")
    i_run = wt.find("superlooper run")
    assert i_install != -1, "walkthrough must include the publish/install step (./bin/install.sh)"
    assert i_adopt != -1, "walkthrough must invoke `superlooper adopt`"
    assert i_doctor != -1, "walkthrough must invoke `superlooper doctor`"
    assert i_run != -1, "walkthrough must invoke `superlooper run`"
    assert i_install < i_adopt < i_doctor < i_run, (
        "walkthrough steps must read publish/install -> adopt -> doctor -> run so that following "
        "them verbatim reaches a green doctor before run; got "
        f"install@{i_install} adopt@{i_adopt} doctor@{i_doctor} run@{i_run}"
    )


def test_report_required_sections_default_is_web_agnostic_with_browser_opt_in():
    # issue #57: the field table must document the new honest default — the two sections EVERY worker
    # can produce (Tests, Review) — never the old "Browser evidence" list that a non-web worker can't
    # satisfy. Browser evidence must still be shown, but as the documented OPT-IN for web repos.
    text = _doc_text()
    row = next((ln for ln in text.splitlines()
                if ln.strip().startswith("| `report_required_sections`")), None)
    assert row is not None, "ADOPTING.md must document report_required_sections in the field table"
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    assert len(cells) >= 2, f"malformed field-table row: {row!r}"
    default_cell = cells[1]     # | Field | Default | Meaning |
    assert default_cell == '`["Tests", "Review"]`', f"unexpected Default cell: {default_cell!r}"
    assert "Browser evidence" not in default_cell   # the web-only assumption is no longer the default
    # ...and browser evidence appears elsewhere in the doc as the explicit web-repo opt-in.
    lowered = text.lower()
    assert "browser evidence" in lowered
    assert "opt-in" in lowered or "web repo" in lowered or "web app" in lowered


def test_reserved_investigation_lanes_and_borrow_policy_are_documented():
    # issue #63 DoD: the object `lanes` shape AND the chosen borrow policy (may an investigation use
    # an idle build lane?) must be documented so an adopter can find them. Pin the doc so it can't
    # drift away from the scheduler behaviour the tests enforce.
    text = _doc_text()
    lowered = text.lower()
    # the object shape and both pool names appear
    assert '"build"' in text and '"investigate"' in text
    assert "reserved investigation lane" in lowered
    # the borrow policy is stated explicitly (no borrowing, both directions)
    assert "no borrowing" in lowered
    assert "borrows an idle build lane" in lowered
    # and the back-compat promise for the plain integer form is stated
    assert "integer form is unchanged" in lowered or "existing configs keep working" in lowered
