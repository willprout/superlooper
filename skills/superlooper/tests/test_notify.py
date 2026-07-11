"""notify.send — the iMessage-first notification adapter (plan Task 11).

Precedence: notify.imessage_to (via skill/bin/imessage-notify.sh, an osascript one-liner) →
notify.cmd (a {title}/{body} template) → `cmux notify` → log-only. It NEVER raises — a send
failure is a returned outcome string the runner journals, never an exception into a tick.

Everything external is a stub on PATH / SL_CMUX (the project's shell-via-injected-stub pattern):
a fake `osascript` captures the message the real imessage-notify.sh hands it; SL_CMUX points at a
recording stub; a failing channel is a stub that exits nonzero. No mocks of notify itself.
"""
import os
import stat
from pathlib import Path

import notify


def _stub(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _capturing_bin(dirpath, name, capture_file, rc=0):
    """A stub executable that appends its argv to `capture_file` then exits with `rc`."""
    p = dirpath / name
    _stub(p, f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{capture_file}"\nexit {rc}\n')
    return p


def test_imessage_takes_precedence_and_reaches_osascript(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "osascript.log"
    _capturing_bin(bindir, "osascript", cap)                 # stub osascript on PATH
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    cfg = {"notify": {"imessage_to": "+15551234567", "cmd": "echo SHOULD-NOT-RUN"}}
    out = notify.send(cfg, "superlooper: i7 parked", "retry cap hit on #7")

    assert out.startswith("sent via imessage"), out
    captured = cap.read_text()
    assert "+15551234567" in captured                        # recipient flowed to osascript
    assert "i7 parked" in captured                           # the title/body did too
    assert "retry cap hit on #7" in captured


def test_imessage_send_failure_is_a_returned_outcome_never_raises(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "osascript.log"
    _capturing_bin(bindir, "osascript", cap, rc=1)           # osascript fails
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")

    out = notify.send({"notify": {"imessage_to": "+1555"}}, "t", "b")   # must not raise
    assert "imessage" in out and "fail" in out.lower(), out
    # it did NOT silently fall through to another channel — imessage was the chosen channel
    assert "cmux" not in out and "log-only" not in out


def test_cmd_channel_when_no_imessage(tmp_path, monkeypatch):
    marker = tmp_path / "cmd-ran.txt"
    cfg = {"notify": {"imessage_to": None,
                      "cmd": f'printf "%s|%s" "{{title}}" "{{body}}" > {marker}'}}
    monkeypatch.setenv("SL_CMUX", "/nonexistent/cmux")       # cmux must not be reached
    out = notify.send(cfg, "TITLE", "BODY")
    assert out.startswith("sent via cmd"), out
    assert marker.read_text() == "TITLE|BODY"                # {title}/{body} substituted


def test_cmux_fallback_when_nothing_configured(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "cmux.log"
    cmux = _capturing_bin(bindir, "cmux", cap)
    monkeypatch.setenv("SL_CMUX", str(cmux))
    out = notify.send({"notify": {"imessage_to": None, "cmd": None}}, "hello", "world")
    assert out.startswith("sent via cmux"), out
    captured = cap.read_text()
    assert "notify" in captured and "hello" in captured      # `cmux notify --title hello ...`


def test_cmd_channel_does_not_execute_body_content(tmp_path, monkeypatch):
    # the body is worker-authored (park/bounce memos) and routinely contains backticks/$()/(); it
    # must NEVER be executed by the shell — only delivered as text.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    pwned = tmp_path / "PWNED"
    out_file = tmp_path / "out.txt"
    cfg = {"notify": {"cmd": f'printf "%s" {{body}} > {out_file}'}}   # {body} bare -> notify quotes it
    out = notify.send(cfg, "t", f"$(touch {pwned})")
    assert out.startswith("sent via cmd"), out
    assert not pwned.exists()                                     # the $(...) did NOT run
    assert out_file.read_text() == f"$(touch {pwned})"            # delivered verbatim


def test_cmd_channel_quoted_placeholder_does_not_execute_body(tmp_path, monkeypatch):
    # Codex R2 C1: a config author who wraps {body} in DOUBLE QUOTES must not re-open injection —
    # shlex.quote() only protects a bare token; inside "..." a $() in the value would still run.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    pwned = tmp_path / "PWNED_QUOTED"
    out_file = tmp_path / "outq.txt"
    cfg = {"notify": {"cmd": f'printf "%s" "{{body}}" > {out_file}'}}   # {body} INSIDE double quotes
    out = notify.send(cfg, "t", f"$(touch {pwned})")
    assert out.startswith("sent via cmd"), out
    assert not pwned.exists()                                     # THE regression: the $(...) did NOT run
    assert "PWNED_QUOTED" in out_file.read_text()                 # delivered as DATA, never executed


def test_cmd_channel_exposes_title_body_as_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    out_file = tmp_path / "env.txt"
    cfg = {"notify": {"cmd": f'printf "%s|%s" "$SL_TITLE" "$SL_BODY" > {out_file}'}}
    out = notify.send(cfg, "the title", "the body")
    assert out.startswith("sent via cmd")
    assert out_file.read_text() == "the title|the body"          # safe env-var alternative works


def test_log_only_when_no_channel_and_no_cmux(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    out = notify.send({"notify": {"imessage_to": None, "cmd": None}}, "t", "b")
    assert out == "log-only", out


def test_wrong_typed_config_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    # config not a dict, notify not a dict, imessage_to wrong-typed — all coerce to log-only,
    # never an exception into the tick (fail-closed like every other view in this codebase).
    assert notify.send(None, "t", "b") == "log-only"
    assert notify.send({"notify": "nope"}, "t", "b") == "log-only"
    assert notify.send({"notify": {"imessage_to": 12345}}, "t", "b") == "log-only"


def test_default_cmux_is_neutralized_in_the_test_suite():
    # Guard for the conftest autouse fixture (2026-07-03 toast-spam ratchet): if the
    # neutralization is ever removed, _cmux_binary() falls back to the real /Applications
    # bundle and this fails on EVERY machine — not just ones with cmux installed.
    resolved = notify._cmux_binary()
    assert "/Applications/" not in resolved, resolved


# --- send_test: the stack doctor's rich-result entry point (issue #25) --------------------
# send() flattens delivery to a journaled outcome STRING; send_test() runs the SAME precedence
# but returns the full SendResult (channel, ok, rc, stderr) the doctor needs to FAIL a block on
# a nonzero send and print the actual error — a string like "cmd notify failed (rc=2)" hides the
# stderr that says WHY (the live 2026-07-10 incident: recipient file missing → exit 2).

def test_send_test_returns_ok_result_on_successful_cmd_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    out_file = tmp_path / "ran.txt"
    cfg = {"notify": {"imessage_to": None,
                      "cmd": f'printf "%s" "$SL_TITLE" > {out_file}'}}
    r = notify.send_test(cfg, "TITLE", "BODY")
    assert r.channel == "cmd"
    assert r.ok is True
    assert r.rc == 0
    assert out_file.read_text() == "TITLE"          # it really ran through the configured path


def test_send_test_carries_rc_and_stderr_from_a_failed_cmd_send(tmp_path, monkeypatch):
    # Reproduces the live incident shape: the configured command exits nonzero and writes the
    # real reason to stderr. send_test must surface BOTH so the doctor can print rc + the tail.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg = {"notify": {"imessage_to": None,
                      "cmd": 'printf "recipient file missing\\n" 1>&2; exit 2'}}
    r = notify.send_test(cfg, "t", "b")
    assert r.channel == "cmd"
    assert r.ok is False
    assert r.rc == 2
    assert "recipient file missing" in r.stderr


def test_send_test_never_raises_and_reports_log_only_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    r = notify.send_test({"notify": {"imessage_to": None, "cmd": None}}, "t", "b")
    assert r.channel == "log-only"
    assert r.ok is True          # nothing to send is not a failure — the doctor gates on config


def test_send_never_raises_on_a_pathological_config_value(tmp_path, monkeypatch):
    # A config value with an embedded null byte makes subprocess.run raise ValueError (not OSError).
    # The documented contract is "never an exception into the tick" — and the read-only doctor now
    # calls this path, so it must degrade to a returned failure, never a traceback.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    out = notify.send({"notify": {"imessage_to": "+1555\x00bad"}}, "t", "b")
    assert "imessage" in out and "fail" in out.lower(), out
    r = notify.send_test({"notify": {"cmd": "printf x\x00"}}, "t", "b")
    assert r.ok is False and r.rc != 0


def test_send_still_returns_the_same_outcome_strings_after_refactor(tmp_path, monkeypatch):
    # send()'s journaled-string contract (the runner depends on it) is unchanged by send_test.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    ok = notify.send({"notify": {"cmd": 'exit 0'}}, "t", "b")
    bad = notify.send({"notify": {"cmd": 'exit 2'}}, "t", "b")
    assert ok == "sent via cmd"
    assert bad == "cmd notify failed (rc=2)"
