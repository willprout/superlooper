"""Task 10 — the push channel (``lib/notify.py``), ported from the skill's notifier.

``send()`` has ONE job: deliver a single notification by a fixed precedence and return a short
outcome STRING the caller journals — it must NEVER raise into the poll loop, and every subprocess
it spawns is time-bounded so a hung Messages/notifier can't wedge a tick. The precedence the issue
pins is three tiers (a desktop toast is useless to the absent owner a dead-man's switch exists
for):

    notify.imessage_to → an osascript one-liner to Messages.app (osascript resolved via the
                         injected ``SL_OSASCRIPT`` — the conftest contract, never a PATH stub)
    notify.cmd         → a shell template with {title}/{body} (an ntfy/Pushover curl, say)
    log-only           → nothing configured; the content is already journaled, only unsent

The chosen channel does NOT cascade on failure (a failing primary must surface, not hide behind a
fallback). These tests drive the injected osascript stub the conftest's ``SL_OSASCRIPT`` contract
exists for — no test ever reaches a real ``osascript``.
"""
import os
import stat

import pytest

import notify


def _write_stub(path, script):
    path.write_text("#!/bin/bash\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# --------------------------- log-only (nothing configured) ---------------------------

def test_send_is_log_only_when_nothing_is_configured():
    assert notify.send({}, "RUNNER DOWN", "no heartbeat") == "log-only"


def test_send_is_log_only_when_notify_block_is_empty_or_null():
    assert notify.send({"notify": {}}, "t", "b") == "log-only"
    assert notify.send({"notify": {"imessage_to": None, "cmd": None}}, "t", "b") == "log-only"


# --------------------------- imessage precedence (osascript, injected) ---------------------------

def test_send_imessage_invokes_the_injected_osascript_with_recipient_and_message(tmp_path, monkeypatch):
    args_rec = tmp_path / "argv.txt"
    stdin_rec = tmp_path / "stdin.txt"
    stub = _write_stub(tmp_path / "fake_osascript",
                       'cat > "%s"\nprintf "%%s\\n%%s\\n%%s\\n" "$1" "$2" "$3" > "%s"\nexit 0\n'
                       % (stdin_rec, args_rec))
    monkeypatch.setenv("SL_OSASCRIPT", stub)

    out = notify.send({"notify": {"imessage_to": "+15551234567"}}, "RUNNER DOWN", "no heartbeat")

    assert out == "sent via imessage"
    lines = args_rec.read_text().splitlines()
    assert lines[0] == "-"                        # `osascript -` reads the script from stdin
    assert lines[1] == "+15551234567"             # recipient is argv, never string-interpolated
    assert lines[2] == "RUNNER DOWN\nno heartbeat".splitlines()[0]  # title on the first line
    assert "no heartbeat" in args_rec.read_text()                   # body follows on its own line
    assert stdin_rec.read_text().strip(), "the AppleScript is fed on stdin (osascript -)"


def test_send_imessage_title_only_when_no_body(tmp_path, monkeypatch):
    args_rec = tmp_path / "argv.txt"
    stub = _write_stub(tmp_path / "fake_osascript",
                       'cat > /dev/null\nprintf "%%s" "$3" > "%s"\nexit 0\n' % args_rec)
    monkeypatch.setenv("SL_OSASCRIPT", stub)
    assert notify.send({"notify": {"imessage_to": "will@x"}}, "just a title", "") == "sent via imessage"
    assert args_rec.read_text() == "just a title"   # no trailing newline / empty body line


def test_send_imessage_nonzero_exit_is_reported_never_raised(tmp_path, monkeypatch):
    stub = _write_stub(tmp_path / "fake_osascript", "exit 3\n")
    monkeypatch.setenv("SL_OSASCRIPT", stub)
    assert notify.send({"notify": {"imessage_to": "x"}}, "t", "b") == "imessage send failed (rc=3)"


def test_send_imessage_missing_binary_fails_closed_never_raises():
    # conftest points SL_OSASCRIPT at a guaranteed-absent path by default: an OSError, coerced to
    # rc 127, becomes a journaled outcome — never an exception into the tick.
    out = notify.send({"notify": {"imessage_to": "x"}}, "t", "b")
    assert out == "imessage send failed (rc=127)"


def test_imessage_precedence_beats_cmd(tmp_path, monkeypatch):
    stub = _write_stub(tmp_path / "fake_osascript", "exit 0\n")
    monkeypatch.setenv("SL_OSASCRIPT", stub)
    cmd_marker = tmp_path / "cmd_ran"
    cfg = {"notify": {"imessage_to": "x", "cmd": 'touch "%s"' % cmd_marker}}
    assert notify.send(cfg, "t", "b") == "sent via imessage"
    assert not cmd_marker.exists(), "precedence selects ONE channel; cmd must not also fire"


# --------------------------- cmd precedence (shell template) ---------------------------

def test_send_cmd_runs_the_template_with_title_and_body(tmp_path):
    out = tmp_path / "cmd_out.txt"
    cfg = {"notify": {"cmd": "printf '%s|%s' {title} {body} > \"" + str(out) + "\""}}
    assert notify.send(cfg, "RUNNER DOWN", "no heartbeat") == "sent via cmd"
    assert out.read_text() == "RUNNER DOWN|no heartbeat"


def test_send_cmd_payload_cannot_inject_command_substitution(tmp_path):
    # The Codex R2 C1 port: the untrusted VALUE rides in the environment ($SL_BODY), never the
    # shell string — so `$(...)`/backticks in a memo are delivered VERBATIM, never executed.
    out = tmp_path / "cmd_out.txt"
    pwned = tmp_path / "pwned"
    cfg = {"notify": {"cmd": "printf '%s' {body} > \"" + str(out) + "\""}}
    payload = "$(touch %s) `touch %s`" % (pwned, pwned)
    assert notify.send(cfg, "t", payload) == "sent via cmd"
    assert not pwned.exists(), "command substitution in the payload must NOT execute"
    assert out.read_text() == payload, "the payload is delivered verbatim"


def test_send_cmd_nonzero_exit_is_reported(tmp_path):
    assert notify.send({"notify": {"cmd": "exit 7"}}, "t", "b") == "cmd notify failed (rc=7)"


def test_send_cmd_is_bounded_by_a_timeout(monkeypatch):
    # A hung notifier can never wedge the tick: a command that outlives the (here tiny) bound is
    # killed and reported as a timeout, not awaited forever.
    monkeypatch.setattr(notify, "SEND_TIMEOUT", 0.3)
    assert notify.send({"notify": {"cmd": "sleep 5"}}, "t", "b") == "cmd notify failed (rc=124)"


# --------------------------- never raises on a weird payload ---------------------------

@pytest.mark.parametrize("title,body", [(None, None), (123, {"x": 1}), ("t", None)])
def test_send_coerces_nonstring_payload_and_never_raises(title, body):
    # A stray non-string title/body can never break the send — coerced to str, delivered log-only
    # here since nothing is configured.
    assert notify.send({}, title, body) == "log-only"


def test_send_imessage_never_raises_on_a_nul_byte_in_the_payload():
    # subprocess.run raises ValueError (not OSError/TimeoutExpired) for an embedded NUL in argv,
    # BEFORE the binary runs — so the never-raises contract must swallow it into a reported outcome.
    out = notify.send({"notify": {"imessage_to": "x"}}, "t", "bad\x00body")
    assert out.startswith("imessage send failed")


def test_send_cmd_never_raises_on_a_nul_byte_in_the_payload():
    # For the cmd channel the NUL rides in the SL_BODY env value; subprocess.run rejects a NUL in
    # env the same way. Reported, never raised.
    out = notify.send({"notify": {"cmd": "true {body}"}}, "t", "bad\x00body")
    assert out.startswith("cmd notify failed")
