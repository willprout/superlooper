"""notify.send — the iMessage-first notification adapter (plan Task 11), behind the one doorway.

Precedence: notify.imessage_to (via skill/bin/imessage-notify.sh, an osascript one-liner) →
notify.cmd (a {title}/{body} template) → `cmux notify` → log-only. It NEVER raises — a send
failure is a returned outcome string the runner journals, never an exception into a tick. Since
issue #493 send()/send_test() deliver only what notify.render produced: line 1 of the envelope rides
as the title, the rest as the body, so these channel tests hand them a rendered text.

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


def _t(config, headline, ask=None):
    """A rendered owner text for the channel tests (the doorway itself is tested further down)."""
    return notify.render(config, notify.TEST, headline, ask=ask, caller="test")


def _line1(headline, config=None):
    """What line 1 of a rendered text reads for `headline` under `config`."""
    return _t(config, headline).lines[0]


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
    out = notify.send(cfg, _t(cfg, "i7 parked", "retry cap hit on #7"), home=tmp_path)

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

    cfg = {"notify": {"imessage_to": "+1555"}}
    out = notify.send(cfg, _t(cfg, "t", "b"), home=tmp_path)            # must not raise
    assert "imessage" in out and "fail" in out.lower(), out
    # it did NOT silently fall through to another channel — imessage was the chosen channel
    assert "cmux" not in out and "log-only" not in out


def test_cmd_channel_when_no_imessage(tmp_path, monkeypatch):
    marker = tmp_path / "cmd-ran.txt"
    cfg = {"notify": {"imessage_to": None,
                      "cmd": f'printf "%s|%s" {{title}} {{body}} > {marker}'}}   # bare: adapter quotes
    monkeypatch.setenv("SL_CMUX", "/nonexistent/cmux")       # cmux must not be reached
    out = notify.send(cfg, _t(cfg, "TITLE", "BODY"), home=tmp_path)
    assert out.startswith("sent via cmd"), out
    assert marker.read_text() == _line1("TITLE", cfg) + "|BODY"   # {title}/{body} substituted


def test_cmux_fallback_when_nothing_configured(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "cmux.log"
    cmux = _capturing_bin(bindir, "cmux", cap)
    monkeypatch.setenv("SL_CMUX", str(cmux))
    cfg = {"notify": {"imessage_to": None, "cmd": None}}
    out = notify.send(cfg, _t(cfg, "hello", "world"), home=tmp_path)
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
    out = notify.send(cfg, _t(cfg, "t", f"$(touch {pwned})"), home=tmp_path)
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
    out = notify.send(cfg, _t(cfg, "t", f"$(touch {pwned})"), home=tmp_path)
    assert out.startswith("sent via cmd"), out
    assert not pwned.exists()                                     # THE regression: the $(...) did NOT run
    assert "PWNED_QUOTED" in out_file.read_text()                 # delivered as DATA, never executed


def test_cmd_channel_exposes_title_body_as_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    out_file = tmp_path / "env.txt"
    cfg = {"notify": {"cmd": f'printf "%s|%s" "$SL_TITLE" "$SL_BODY" > {out_file}'}}
    out = notify.send(cfg, _t(cfg, "the title", "the body"), home=tmp_path)
    assert out.startswith("sent via cmd")
    assert out_file.read_text() == _line1("the title", cfg) + "|the body"   # env-var alternative


def test_log_only_when_no_channel_and_no_cmux(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    cfg = {"notify": {"imessage_to": None, "cmd": None}}
    out = notify.send(cfg, _t(cfg, "t", "b"), home=tmp_path)
    assert out == "log-only", out


def test_wrong_typed_config_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    # config not a dict, notify not a dict, imessage_to wrong-typed — all coerce to log-only,
    # never an exception into the tick (fail-closed like every other view in this codebase).
    for cfg in (None, {"notify": "nope"}, {"notify": {"imessage_to": 12345}}):
        assert notify.send(cfg, _t(cfg, "t", "b"), home=tmp_path) == "log-only"


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
    r = notify.send_test(cfg, _t(cfg, "TITLE", "BODY"), home=tmp_path)
    assert r.channel == "cmd"
    assert r.ok is True
    assert r.rc == 0
    assert out_file.read_text() == _line1("TITLE", cfg)   # it really ran through the configured path


def test_send_test_carries_rc_and_stderr_from_a_failed_cmd_send(tmp_path, monkeypatch):
    # Reproduces the live incident shape: the configured command exits nonzero and writes the
    # real reason to stderr. send_test must surface BOTH so the doctor can print rc + the tail.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg = {"notify": {"imessage_to": None,
                      "cmd": 'printf "recipient file missing\\n" 1>&2; exit 2'}}
    r = notify.send_test(cfg, _t(cfg, "t", "b"), home=tmp_path)
    assert r.channel == "cmd"
    assert r.ok is False
    assert r.rc == 2
    assert "recipient file missing" in r.stderr


def test_send_test_never_raises_and_reports_log_only_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-such-cmux"))
    cfg = {"notify": {"imessage_to": None, "cmd": None}}
    r = notify.send_test(cfg, _t(cfg, "t", "b"), home=tmp_path)
    assert r.channel == "log-only"
    assert r.ok is True          # nothing to send is not a failure — the doctor gates on config


def test_send_never_raises_on_a_pathological_config_value(tmp_path, monkeypatch):
    # A config value with an embedded null byte makes subprocess.run raise ValueError (not OSError).
    # The documented contract is "never an exception into the tick" — and the read-only doctor now
    # calls this path, so it must degrade to a returned failure, never a traceback.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    bad_to = {"notify": {"imessage_to": "+1555\x00bad"}}
    out = notify.send(bad_to, _t(bad_to, "t", "b"), home=tmp_path)
    assert "imessage" in out and "fail" in out.lower(), out
    bad_cmd = {"notify": {"cmd": "printf x\x00"}}
    r = notify.send_test(bad_cmd, _t(bad_cmd, "t", "b"), home=tmp_path)
    assert r.ok is False and r.rc != 0


def test_send_still_returns_the_same_outcome_strings_after_refactor(tmp_path, monkeypatch):
    # send()'s journaled-string contract (the runner depends on it) is unchanged by send_test.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    ok_cfg, bad_cfg = {"notify": {"cmd": 'exit 0'}}, {"notify": {"cmd": 'exit 2'}}
    ok = notify.send(ok_cfg, _t(ok_cfg, "t", "b"), home=tmp_path)
    bad = notify.send(bad_cfg, _t(bad_cfg, "t", "b"), home=tmp_path)
    assert ok == "sent via cmd"
    assert bad == "cmd notify failed (rc=2)"


# --- the doorway (issue #493): every owner text is rendered by notify.render --------------------
# A text is a pager, never a runbook. ONE renderer produces the envelope
#     <tier emoji> <repo>@<machine> · <what happened, one clause>
#     <one line: what is asked of the owner, or nothing>
#     <one GitHub URL, only when an issue or PR exists>
# and send/send_test deliver ONLY what it produced. The cap (3 lines, TEXT_MAX_BYTES) is enforced
# here, and an overflow is journaled as `notify_truncated` so a verbose caller surfaces in the
# morning report instead of reaching the phone unseen.

import ast
import json

import pytest

_CFG = {"repo": "willprout/superlooper", "notify": {"machine_label": "mini"}}


@pytest.mark.parametrize("tier,emoji", [
    (notify.DOWN, "🔴"), (notify.WAITING, "🟠"), (notify.RECOVERED, "🟢"),
    (notify.MORNING, "☀️"), (notify.TEST, "🧪"),
])
def test_render_produces_the_exact_envelope_for_each_tier(tier, emoji):
    t = notify.render(_CFG, tier, "runner down, 3 approved waiting",
                      ask="auto-restart failed 5x this hour — needs you",
                      url="https://github.com/willprout/superlooper/issues/412")
    assert t.lines == (
        f"{emoji} superlooper@mini · runner down, 3 approved waiting",
        "auto-restart failed 5x this hour — needs you",
        "https://github.com/willprout/superlooper/issues/412",
    )
    assert t.text == "\n".join(t.lines)
    assert t.tier == tier and not t.truncated and t.dropped_bytes == 0


def test_render_the_ask_and_url_lines_are_optional():
    only_head = notify.render(_CFG, notify.RECOVERED, "runner back")
    assert only_head.lines == ("🟢 superlooper@mini · runner back",)
    with_url = notify.render(_CFG, notify.WAITING, "#412 waits for your answer",
                             url="https://github.com/willprout/superlooper/issues/412")
    assert with_url.lines == ("🟠 superlooper@mini · #412 waits for your answer",
                              "https://github.com/willprout/superlooper/issues/412")


def test_render_takes_a_tier_never_a_free_title():
    # The tier set is closed: a free title ("superlooper ALERT") is not a tier.
    for bad in ("superlooper ALERT", "", None, "🔴", "red"):
        with pytest.raises(ValueError):
            notify.render(_CFG, bad, "x")


def test_render_machine_falls_back_to_the_short_hostname(monkeypatch):
    monkeypatch.setattr(notify.socket, "gethostname", lambda: "Williams-Mac-mini.local")
    cfg = {"repo": "willprout/eapp", "notify": {"machine_label": None}}
    assert notify.render(cfg, notify.DOWN, "x").lines[0] == "🔴 eapp@Williams-Mac-mini · x"
    cfg_absent = {"repo": "willprout/eapp", "notify": {}}      # key absent reads the same as null
    assert notify.render(cfg_absent, notify.DOWN, "x").lines[0] == "🔴 eapp@Williams-Mac-mini · x"


def test_render_machine_label_overrides_the_hostname(monkeypatch):
    monkeypatch.setattr(notify.socket, "gethostname", lambda: "Williams-MacBook-Pro.local")
    cfg = {"repo": "willprout/eapp", "notify": {"machine_label": "laptop"}}
    assert notify.render(cfg, notify.DOWN, "x").lines[0] == "🔴 eapp@laptop · x"


def test_render_never_raises_on_a_config_missing_identity(monkeypatch):
    # A wrong-typed config must still render SOMETHING identifying (fail-closed like every view).
    monkeypatch.setattr(notify.socket, "gethostname", lambda: "")
    for cfg in (None, {}, {"repo": 7, "notify": {"machine_label": 7}}):
        line = notify.render(cfg, notify.DOWN, "x").lines[0]
        assert line.startswith("🔴 ") and "@" in line and line.endswith(" · x")


def test_render_collapses_multiline_input_so_the_envelope_stays_three_lines():
    t = notify.render(_CFG, notify.WAITING, "i7\nparked", ask="line one\n\nline two\nline three",
                      url="https://github.com/willprout/superlooper/issues/7")
    assert len(t.lines) == 3
    assert t.lines[0] == "🟠 superlooper@mini · i7 parked"
    assert t.lines[1] == "line one line two line three"
    assert not any("\n" in ln for ln in t.lines)


def test_render_caps_bytes_and_lines_and_records_the_dropped_length():
    runbook = "check gh auth and re-run adopt, then restart the runner. " * 200   # ~11 kB
    url = "https://github.com/willprout/superlooper/issues/493"
    t = notify.render(_CFG, notify.DOWN, "usage meter unreadable", ask=runbook, url=url,
                      caller="decide:alert")
    assert len(t.text.encode("utf-8")) <= notify.TEXT_MAX_BYTES
    assert len(t.lines) <= notify.TEXT_MAX_LINES
    assert t.lines[0] == "🔴 superlooper@mini · usage meter unreadable"   # identity never cut
    assert t.lines[-1] == url                                             # a cut URL is useless
    assert t.lines[1].endswith("…")
    assert t.truncated and t.dropped_bytes > 10000
    assert t.caller == "decide:alert"


def test_render_cuts_the_headline_only_after_the_ask_is_gone():
    long_head = "a headline that is far too long for a lock screen " * 20
    t = notify.render(_CFG, notify.DOWN, long_head, ask="some ask")
    assert len(t.text.encode("utf-8")) <= notify.TEXT_MAX_BYTES
    assert t.lines[0].startswith("🔴 superlooper@mini · a headline")
    assert t.lines[0].endswith("…")
    assert t.truncated


def test_render_never_exceeds_the_cap_even_with_an_absurd_identity():
    cfg = {"repo": "o/" + "r" * 600, "notify": {"machine_label": "m" * 600}}
    t = notify.render(cfg, notify.DOWN, "x" * 600, ask="y" * 600, url="https://e.x/" + "u" * 600)
    assert len(t.text.encode("utf-8")) <= notify.TEXT_MAX_BYTES
    assert 1 <= len(t.lines) <= notify.TEXT_MAX_LINES and t.truncated


def test_render_defaults_the_caller_to_the_calling_function():
    t = notify.render(_CFG, notify.TEST, "x")
    assert "test_render_defaults_the_caller_to_the_calling_function" in t.caller


def _cmd_cfg(tmp_path, **notify_over):
    out = tmp_path / "delivered.txt"
    n = {"imessage_to": None, "machine_label": "mini",
         "cmd": f'printf "%s\\n%s" "$SL_TITLE" "$SL_BODY" > {out}'}
    n.update(notify_over)
    return {"repo": "willprout/superlooper", "notify": n}, out


def _journal(home):
    p = home / "journal.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_send_delivers_exactly_the_rendered_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.WAITING, "i7 parked", ask="retry cap hit",
                      url="https://github.com/willprout/superlooper/issues/7")
    assert notify.send(cfg, t, home=tmp_path) == "sent via cmd"
    assert out.read_text() == t.text          # title = line 1, body = the rest: one message
    # nothing truncated -> no truncation record; the delivery itself is the canary act (issue #495)
    assert [r["act"] for r in _journal(tmp_path)] == ["notify_canary"]


def test_send_journals_a_truncation_and_never_delivers_more_than_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.DOWN, "ALERT: usage_stale", ask="runbook " * 2000,
                      caller="decide:alert")
    assert notify.send(cfg, t, home=tmp_path) == "sent via cmd"
    assert len(out.read_text().encode("utf-8")) <= notify.TEXT_MAX_BYTES
    recs = [r for r in _journal(tmp_path) if r.get("act") == "notify_truncated"]
    assert len(recs) == 1
    assert recs[0]["caller"] == "decide:alert"
    assert recs[0]["tier"] == notify.DOWN
    assert recs[0]["dropped_bytes"] == t.dropped_bytes > 0


def test_send_journals_the_truncation_even_when_the_channel_is_log_only(tmp_path, monkeypatch):
    # The defect is the caller's verbosity, not the channel: it surfaces whether or not a text left.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg = {"repo": "willprout/superlooper", "notify": {"machine_label": "mini"}}
    t = notify.render(cfg, notify.DOWN, "x", ask="y" * 5000, caller="cli:nightly")
    assert notify.send(cfg, t, home=tmp_path) == "log-only"
    assert [r["caller"] for r in _journal(tmp_path) if r["act"] == "notify_truncated"] == ["cli:nightly"]


def test_send_defaults_the_truncation_journal_to_the_configured_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    cfg = {"repo": "willprout/superlooper", "notify": {"machine_label": "mini"}}
    notify.send(cfg, notify.render(cfg, notify.DOWN, "x", ask="y" * 5000, caller="c"))
    assert [r["act"] for r in _journal(tmp_path / "slhome" / "willprout__superlooper")] == \
        ["notify_truncated", "notify_canary"]


def test_send_and_send_test_refuse_anything_the_renderer_did_not_produce(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    assert notify.send(cfg, "superlooper ALERT").startswith("refused")      # a raw title
    assert notify.send(cfg, ("🔴 x", "y")).startswith("refused")
    r = notify.send_test(cfg, "superlooper doctor: notify channel test")
    assert r.ok is False and r.channel == "refused"
    assert not out.exists()                                                 # nothing delivered


def test_send_refuses_a_rendered_text_tampered_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.DOWN, "x")
    for bad in (t._replace(lines=t.lines + ("a", "b", "c")),
                t._replace(lines=(t.lines[0] + "z" * 1000,)),
                t._replace(lines=(t.lines[0] + "\nsmuggled\nlines\nhere",)),
                t._replace(tier="superlooper ALERT")):
        assert notify.send(cfg, bad, home=tmp_path).startswith("refused")
        assert notify.send_test(cfg, bad, home=tmp_path).ok is False
    assert not out.exists()


def test_send_test_returns_the_delivery_result_for_a_rendered_text(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.TEST, "notify channel test")
    r = notify.send_test(cfg, t, home=tmp_path)
    assert (r.channel, r.ok, r.rc) == ("cmd", True, 0)
    assert out.read_text() == "🧪 superlooper@mini · notify channel test\n"


# --- every send is the channel canary (issue #495) ----------------------------------------------------
# The daily morning text used to be the one proof the channel worked. It now goes out only on news, so
# the proof is whatever text last went out: the doorway journals EVERY attempt as `notify_canary`, and
# the report + dashboard read "last text delivered <age>" from the newest delivered one.

def _canaries(home):
    return [r for r in _journal(home) if r.get("act") == "notify_canary"]


@pytest.mark.parametrize("tier", [notify.DOWN, notify.WAITING, notify.RECOVERED, notify.MORNING,
                                  notify.TEST])
def test_a_delivered_text_of_any_tier_is_journaled_as_the_canary(tier, tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    assert notify.send(cfg, notify.render(cfg, tier, "x", caller="decide:park"),
                       home=tmp_path) == "sent via cmd"
    (rec,) = _canaries(tmp_path)
    assert (rec["ok"], rec["channel"], rec["rc"]) == (True, "cmd", 0)
    assert rec["tier"] == tier and rec["caller"] == "decide:park"
    assert isinstance(rec["ts"], (int, float))          # the age the surfaces render hangs off this


def test_send_test_journals_its_delivery_as_the_canary_too(tmp_path, monkeypatch):
    # doctor --stack's live test and the hand-run paths use send_test: a text that reached the phone
    # proves the channel whichever function sent it.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    r = notify.send_test(cfg, notify.render(cfg, notify.TEST, "t", caller="doctor:notify_channel"),
                         home=tmp_path)
    assert r.ok is True
    (rec,) = _canaries(tmp_path)
    assert (rec["ok"], rec["channel"], rec["caller"]) == (True, "cmd", "doctor:notify_channel")


def test_a_failed_send_is_journaled_as_a_failed_canary_with_its_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path, cmd='printf "recipient file missing" 1>&2; exit 2')
    assert notify.send(cfg, notify.render(cfg, notify.DOWN, "x"), home=tmp_path).startswith(
        "cmd notify failed")
    (rec,) = _canaries(tmp_path)
    assert (rec["ok"], rec["channel"], rec["rc"]) == (False, "cmd", 2)
    assert "recipient file missing" in rec["detail"]


def test_a_log_only_send_is_journaled_as_log_only_never_as_a_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg = {"repo": "willprout/superlooper", "notify": {"machine_label": "mini"}}
    assert notify.send(cfg, notify.render(cfg, notify.WAITING, "x"), home=tmp_path) == "log-only"
    (rec,) = _canaries(tmp_path)
    assert rec["channel"] == "log-only"


def test_a_refused_text_journals_no_canary(tmp_path, monkeypatch):
    # nothing was attempted, so nothing was proven either way
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    assert notify.send(cfg, "superlooper ALERT", home=tmp_path).startswith("refused")
    assert notify.send_test(cfg, "raw", home=tmp_path).ok is False
    assert _canaries(tmp_path) == []


def test_the_canary_defaults_to_the_configured_state_home(tmp_path, monkeypatch):
    # doctor --stack hands its sender no home: its live test still lands in the repo's own journal
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    cfg, out = _cmd_cfg(tmp_path)
    notify.send_test(cfg, notify.render(cfg, notify.TEST, "t"))
    assert [r["channel"] for r in _canaries(tmp_path / "slhome" / "willprout__superlooper")] == ["cmd"]


def test_a_canary_journal_that_cannot_be_written_never_stops_the_text(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    assert notify.send(cfg, notify.render(cfg, notify.DOWN, "x"), home=blocker / "home") == \
        "sent via cmd"
    assert out.exists()


def test_the_suite_never_journals_into_the_real_state_home():
    # conftest points SL_HOME at a tmp dir for every test (issue #495): a doorway send with no home
    # would otherwise write a DELIVERED canary into the live loop's journal, and the owner's report and
    # dashboard would then show a text that never reached his phone.
    assert os.environ.get("SL_HOME")
    assert Path(os.environ["SL_HOME"]).resolve() != Path("~/.superlooper").expanduser().resolve()


def test_imessage_channel_receives_the_envelope_as_one_message(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "osascript.log"
    # capture argv one per line: `- <recipient> <message>` — the message is the whole envelope
    _stub(bindir / "osascript", f'#!/bin/sh\nshift\nprintf "%s" "$2" > "{cap}"\n')
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    cfg = {"repo": "willprout/superlooper", "notify": {"imessage_to": "+1555", "machine_label": "mini"}}
    t = notify.render(cfg, notify.WAITING, "i7 parked", ask="retry cap hit",
                      url="https://github.com/willprout/superlooper/issues/7")
    assert notify.send(cfg, t, home=tmp_path) == "sent via imessage"
    assert cap.read_text() == t.text


# The class-killer's static half: no engine sender may hand send()/send_test() anything but a
# rendered text. Every call must pass exactly (config, <rendered text>[, home]) and the text may never
# be a string literal, an f-string or a concatenation — i.e. a raw title can never reach the phone.
_ENGINE_SOURCES = sorted(
    [p for p in (Path(__file__).resolve().parent.parent / "skill" / "lib").glob("*.py")]
    + [p for p in (Path(__file__).resolve().parent.parent / "skill" / "bin").iterdir()
       if p.is_file() and (p.suffix == ".py" or p.name == "superlooper")])


def _notify_aliases(tree):
    """Every name the module binds the notify module to (`import notify`, `import notify as x`)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name == "notify"}
    return names


def _send_calls(tree):
    aliases = _notify_aliases(tree) | {"notify"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("send", "send_test")
                and isinstance(node.func.value, ast.Name) and node.func.value.id in aliases):
            yield node


def test_no_raw_title_string_reaches_send_anywhere_in_the_engine():
    offenders, seen = [], 0
    for path in _ENGINE_SOURCES:
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in _send_calls(tree):
            seen += 1
            where = f"{path.name}:{call.lineno}"
            if (len(call.args) != 2 or any(isinstance(a, ast.Starred) for a in call.args)
                    or any(k.arg != "home" for k in call.keywords)):
                offenders.append(f"{where}: send takes (config, rendered_text[, home=])")
                continue
            text = call.args[1]
            if isinstance(text, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                offenders.append(f"{where}: a raw string reaches send")
    assert seen >= 8, f"the scan found only {seen} send calls — did the senders move?"
    assert not offenders, "\n".join(offenders)


def test_the_doctor_sender_seam_is_handed_a_rendered_text():
    # stack_doctor calls its injectable `sender`, not notify.send_test by name, so the AST scan above
    # cannot see it. Pin it by behaviour: the fake sender receives a rendered TEST-tier text.
    import stack_doctor
    got = []

    def _sender(config, text):
        got.append(text)
        return notify.SendResult("cmd", True, 0, "")
    stack_doctor.check_notify({"repo": "willprout/superlooper",
                               "notify": {"cmd": "true", "imessage_to": None, "machine_label": "mini"}},
                              sender=_sender, announce=lambda *a: None)
    assert len(got) == 1 and isinstance(got[0], notify.Text) and got[0].tier == notify.TEST
    assert got[0].lines[0].startswith("🧪 superlooper@mini · ")


def test_the_pre_doorway_call_shape_is_refused_never_delivered(tmp_path, monkeypatch):
    # A runner started on the old engine never reaches this module (superlooper run imports notify at
    # start-up, so it keeps the old one until restarted); any (config, title, body) call is a bug.
    # The refusal comes before anything is journaled, so the body is never used as a state home.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    monkeypatch.chdir(tmp_path)                           # a relative "home" would land here
    cfg, out = _cmd_cfg(tmp_path)
    long_title = "superlooper ALERT " * 100               # would truncate -> journal, if accepted
    assert notify.send(cfg, long_title, "old-body").startswith("refused")
    assert notify.send_test(cfg, long_title, "old-body").ok is False
    assert not out.exists()
    assert not (tmp_path / "old-body").exists()           # the body never became a journal home


# --- inputs UTF-8 cannot carry (fresh review P2) ---------------------------------------------------
# send()/send_test() never raise, whatever they are handed; render() never raises but for a tier.

def test_a_hand_built_text_carrying_a_lone_surrogate_is_refused_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.DOWN, "x")
    bad = t._replace(lines=(t.lines[0] + "\ud800",))
    assert notify.send(cfg, bad, home=tmp_path).startswith("refused")
    assert notify.send_test(cfg, bad, home=tmp_path).ok is False
    assert not out.exists()


def test_render_never_raises_on_a_lone_surrogate_in_a_memo(tmp_path, monkeypatch):
    # A memo decoded from a JSON "\\ud800" escape carries a lone surrogate, which UTF-8 cannot encode.
    # Before the doorway it failed quietly at send time; the doorway must not turn it into a raise.
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    cfg, out = _cmd_cfg(tmp_path)
    t = notify.render(cfg, notify.WAITING, "i7 parked", ask="memo \ud800 tail " + "\ud800" * 400)
    assert len(t.text.encode("utf-8")) <= notify.TEXT_MAX_BYTES
    assert notify.send(cfg, t, home=tmp_path) == "sent via cmd"
    assert out.read_text().startswith("🟠 superlooper@mini · i7 parked\nmemo ? tail")
