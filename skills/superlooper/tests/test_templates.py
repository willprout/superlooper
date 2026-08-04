"""The shipped templates: the launchd NIGHTLY plist, the WATCHDOG plist, and — since issue #306 —
the RUNNER plist, which exactly ONE home may install. (The answerer brief lived here until #194
retired that seat — its absence is pinned in tests/test_answerer_retired.py alongside the rest of
the retirement.)

**Issue #33's rule, and what replaced it.** There used to be no launchd RUNNER template at all, and
the reasoning was sound for the host of the day: a launchd-started runner is a detached process with
no multiplexer tab, so it can never self-detect the pane every worker session is born in; its
startup preflight correctly fails hard, and a KeepAlive would relaunch it into that same failure
forever. That is a fact about a host whose SPAWN NEEDS AN ANCHOR — not about launchd.

The 2026-07-31 ruling (docs/HERDR-ADOPTION-PLAN.md §8.1) moved the runner outside the session host,
onto a host whose spawn needs no anchor, so the prohibition dissolves for that home and only that
home. What replaces the blanket ban is narrower and sharper, and these tests hold it:

* the runner template is the ONLY plist that may invoke `run`, and every other shipped plist still
  may not — the original protection, kept;
* it is a `gui/$UID` LaunchAgent, never a system daemon (the keychain rule, spike 3);
* it carries an EXPLICIT PATH, because launchd's own four entries do not include `gh`;
* the `pane` home does not install it — `lib/runner_home.py` is what decides, and
  tests/test_runner_home.py pins that decision.
"""
import plistlib
import re
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent.parent / "skill" / "templates"


def _sub(text, mapping):
    for k, v in mapping.items():
        text = text.replace("{" + k + "}", v)
    return text


# --------------------------- launchd templates ---------------------------

def test_only_the_runner_template_may_invoke_run():
    # Issue #33's protection, narrowed rather than dropped (see the module docstring): exactly ONE
    # shipped plist may invoke the `run` subcommand, and it is the one named for that job. A
    # scheduled one-shot — the nightly, the watchdog — that quietly grew a `run` invocation would be
    # a launchd runner nobody decided to install. Checked on the parsed ProgramArguments (not a
    # raw-text scan), so a differently-shaped re-introduction is caught too.
    for p in _TEMPLATES.glob("*.plist"):
        # render {placeholder}s with a type-neutral dummy first — a template may hold one inside
        # an <integer> element (e.g. the watchdog's {interval_seconds}), which raw plistlib rejects.
        rendered = re.sub(r"\{[a-z_]+\}", "1", p.read_text())
        args = [str(a) for a in plistlib.loads(rendered.encode()).get("ProgramArguments", [])]
        if p.name == "launchd.runner.plist":
            assert "run" in args, "the runner template stopped invoking `run`"
            continue
        assert "run" not in args, f"{p.name} re-introduces an undeclared launchd runner (invokes `run`)"


def test_no_shipped_plist_asks_for_the_system_domain():
    # The keychain rule (docs/SPIKES-2026-07-29-results.md test 3): a gui/$UID LaunchAgent is in the
    # Aqua session and reads the login keychain; a system/ daemon is a different session and was
    # never tested. No shipped template may hint otherwise — not in a key, not in its own comments,
    # which is where an operator would read the install instruction.
    for p in _TEMPLATES.glob("*.plist"):
        text = p.read_text()
        assert "system/" not in text, f"{p.name} names the system launchd domain"
        # A user agent must never ask for another user, either.
        assert "UserName" not in text, f"{p.name} sets UserName — a user LaunchAgent never needs it"


def test_launchd_runner_template_is_a_gui_login_item_with_an_explicit_path():
    t = (_TEMPLATES / "launchd.runner.plist").read_text()
    for ph in ("{label}", "{superlooper_bin}", "{repo_path}", "{state_home}", "{path}"):
        assert ph in t, f"missing placeholder {ph}"
    rendered = _sub(t, {"label": "com.superlooper.runner.o__r",
                        "superlooper_bin": "/Users/w/.claude/skills/superlooper/bin/superlooper",
                        "repo_path": "/Users/w/projects/r",
                        "state_home": "/Users/w/.superlooper/o__r",
                        "path": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    d = plistlib.loads(rendered.encode())
    assert d["Label"] == "com.superlooper.runner.o__r"
    # The login-item half: up with the owner's GUI session, and restarted when it exits — which is
    # how the #116 Restart button works in this home (the runner exits, launchd brings it back).
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True
    assert d["ThrottleInterval"] >= 10, "a broken runner must be throttled, not spun"
    # The spike-proven gotcha: launchd's own PATH has no Homebrew, so `gh` would simply not exist.
    assert d["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert d["StandardOutPath"].endswith("/logs/runner.log")


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


# --------------------------- watchdog templates (issue #66) ---------------------------

def test_debugger_brief_carries_the_unattended_invocation_context():
    t = (_TEMPLATES / "debugger-brief.md").read_text()
    # every placeholder the watchdog substitutes
    for ph in ("{signals}", "{detail}", "{authority}", "{allowlist}", "{state_home}",
               "{repo_path}", "{repo_slug}", "{grace_minutes}"):
        assert ph in t, f"missing placeholder {ph}"
    low = t.lower()
    # the invocation contract: the session knows it is UNATTENDED, invokes the sl-debugger
    # skill, follows its unattended contract, ends with the memo + notify, and never loops.
    assert "sl-debugger" in low
    assert "unattended" in low
    assert "unattended-contract" in low
    assert "memo" in low
    assert "end the session" in low
    # never a second unattended trigger from inside the session
    assert "never" in low and ("relaunch" in low or "re-trigger" in low or "schedule" in low)


def test_debugger_brief_renders_clean():
    t = (_TEMPLATES / "debugger-brief.md").read_text()
    out = _sub(t, {"signals": "heartbeat_stale", "detail": "runner heartbeat stale 42 min",
                   "authority": "full", "allowlist": "(none)",
                   "state_home": "/Users/w/.superlooper/o__r",
                   "repo_path": "/Users/w/projects/r", "repo_slug": "o/r",
                   "grace_minutes": "30"})
    assert "{" not in out.replace("{}", ""), "unsubstituted placeholder left behind"
    assert "heartbeat_stale" in out and "full" in out and "/Users/w/.superlooper/o__r" in out


def test_launchd_watchdog_template_is_a_valid_scheduled_oneshot():
    import plistlib
    t = (_TEMPLATES / "launchd.watchdog.plist").read_text()
    for ph in ("{label}", "{superlooper_bin}", "{repo_path}", "{state_home}",
               "{interval_seconds}", "{path}"):
        assert ph in t, f"missing placeholder {ph}"
    rendered = _sub(t, {"label": "com.superlooper.watchdog.o__r",
                        "superlooper_bin": "/Users/w/.claude/skills/superlooper/bin/superlooper",
                        "repo_path": "/Users/w/projects/r",
                        "state_home": "/Users/w/.superlooper/o__r",
                        "interval_seconds": "300",
                        "path": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"})
    d = plistlib.loads(rendered.encode())
    assert d["Label"] == "com.superlooper.watchdog.o__r"
    # The same measured gotcha as the runner job (issue #306): without an explicit PATH the
    # watchdog's every GitHub read refuses, which it reads as UNOBSERVABLE — so its no-progress
    # detector silently never fires while the job looks perfectly healthy.
    assert d["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    # a scheduled ONE-SHOT every StartInterval seconds — never a keep-alive (issue #33's
    # lesson stands: nothing under templates/ may keep-alive or re-offer a launchd runner)
    assert "KeepAlive" not in d and "RunAtLoad" not in d
    assert d["StartInterval"] == 300
    args = [str(a) for a in d["ProgramArguments"]]
    assert args[0].endswith("superlooper") and "watchdog" in args and "run" not in args
