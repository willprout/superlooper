"""The Task-10 templates: the answerer brief (the hired judgment's entire world) and the
launchd NIGHTLY plist. Both are consumed by substituting {name} placeholders literally
(brief.py's _sub convention — never str.format, which chokes on prose braces).

There is deliberately NO launchd RUNNER plist (issue #33): a launchd-started runner is a
detached daemon with no cmux tab, so it can never self-detect a pane; its startup preflight
correctly fails hard, and a KeepAlive would relaunch it into the same failure forever. The
runner is started/restarted by hand in a visible cmux tab (references/runner-ops.md → Restarting
the runner); only the nightly — which needs no pane — runs under launchd."""
import plistlib
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "skill" / "templates"


def _sub(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{" + k + "}", v)
    return text


# --------------------------- answerer brief ---------------------------

def test_answerer_brief_carries_the_full_contract():
    t = (_TEMPLATES / "answerer-brief.md").read_text()
    # every placeholder the runner substitutes
    for ph in ("{issue_num}", "{issue_body}", "{question}", "{worktree}", "{answer_path}"):
        assert ph in t, f"missing placeholder {ph}"
    # the plan's contract phrases: one question, read-only worktree, <=10 lines or PARK:,
    # the answer file is the FINAL action (its existence is the done signal)
    low = t.lower()
    assert "one question" in low
    assert "PARK: " in t          # the exact (case-sensitive) marker decide() matches on
    assert "change nothing" in low or "read-only" in low or "change no" in low
    assert "final action" in low
    assert "10 lines" in low


def test_answerer_brief_renders_clean():
    t = (_TEMPLATES / "answerer-brief.md").read_text()
    out = _sub(t, {"issue_num": "42", "issue_body": "## Goal\nDo the thing.",
                   "question": "A or B?", "worktree": "/tmp/wt/i42",
                   "answer_path": "/tmp/home/answers/i42.md"})
    assert "{" not in out.replace("{}", ""), "unsubstituted placeholder left behind"
    assert "#42" in out and "A or B?" in out and "/tmp/home/answers/i42.md" in out


# --------------------------- launchd templates ---------------------------

def test_no_launchd_runner_template_ships():
    # issue #33: the impossible mode must stay gone. NO launchd plist other than the nightly may
    # invoke the `run` subcommand — a launchd runner is a detached daemon with no cmux tab, so the
    # pane preflight fails hard whether it loops via KeepAlive or fires once via RunAtLoad. The
    # runner is (re)started by hand in a cmux tab; nothing under templates/ may re-offer one. Checked
    # on the parsed ProgramArguments (not a raw-text KeepAlive scan), so a RunAtLoad-only or
    # differently-named runner plist is caught too.
    assert not (_TEMPLATES / "launchd.runner.plist").exists()
    for p in _TEMPLATES.glob("*.plist"):
        if p.name == "launchd.nightly.plist":
            continue
        args = [str(a) for a in plistlib.loads(p.read_text().encode()).get("ProgramArguments", [])]
        assert "run" not in args, f"{p.name} re-introduces a launchd runner (invokes `run`) — issue #33"


def test_launchd_nightly_template_is_a_valid_scheduled_oneshot():
    t = (_TEMPLATES / "launchd.nightly.plist").read_text()
    for ph in ("{label}", "{superlooper_bin}", "{repo_path}", "{state_home}", "{hour}", "{minute}"):
        assert ph in t, f"missing placeholder {ph}"
    rendered = _sub(t, {"label": "com.superlooper.nightly.o__r",
                        "superlooper_bin": "/Users/w/.claude/skills/superlooper/bin/superlooper",
                        "repo_path": "/Users/w/projects/r",
                        "state_home": "/Users/w/.superlooper/o__r",
                        "hour": "2", "minute": "0"})
    d = plistlib.loads(rendered.encode())
    assert d["Label"] == "com.superlooper.nightly.o__r"
    # a scheduled ONE-SHOT (StartCalendarInterval at qa.nightly_time), never a keep-alive
    assert "KeepAlive" not in d and "RunAtLoad" not in d
    assert d["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}
    args = d["ProgramArguments"]
    assert args[0].endswith("superlooper") and "nightly" in args
    assert d["StandardOutPath"].endswith("/logs/nightly.log")
