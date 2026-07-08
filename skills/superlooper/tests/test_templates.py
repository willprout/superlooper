"""The Task-10 templates: the answerer brief (the hired judgment's entire world) and the
launchd keep-alive plist. Both are consumed by substituting {name} placeholders literally
(brief.py's _sub convention — never str.format, which chokes on prose braces)."""
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


# --------------------------- launchd template ---------------------------

def test_launchd_template_is_a_valid_keepalive_plist():
    t = (_TEMPLATES / "launchd.runner.plist").read_text()
    for ph in ("{label}", "{superlooper_bin}", "{repo_path}", "{state_home}"):
        assert ph in t, f"missing placeholder {ph}"
    rendered = _sub(t, {"label": "com.superlooper.o__r",
                        "superlooper_bin": "/Users/w/.claude/skills/superlooper/bin/superlooper",
                        "repo_path": "/Users/w/projects/r",
                        "state_home": "/Users/w/.superlooper/o__r"})
    d = plistlib.loads(rendered.encode())
    assert d["Label"] == "com.superlooper.o__r"
    assert d["KeepAlive"] is True
    args = d["ProgramArguments"]
    assert args[0].endswith("superlooper") and "run" in args
    # logs land in the state home (the external watchdog's one place to look)
    assert d["StandardOutPath"].startswith("/Users/w/.superlooper/o__r")
    assert d["StandardErrorPath"].startswith("/Users/w/.superlooper/o__r")


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
