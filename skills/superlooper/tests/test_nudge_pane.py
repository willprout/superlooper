"""Tests for bin/nudge-pane.sh — the single safe pane-write primitive (the resume/answer path that
lost 156/156 rings in run-20260625-1857). Exercises the surface+workspace addressing and the
load-bearing exit-code contract (DEAD=4 refuses to type into a bash shell). A stub cmux logs every
call so we can assert the workspace threading, and returns a canned screen so lib/pane_state
classifies it.

Ported from autocode's test_nudge_pane.py. Superlooper adaptations:
  - env prefix SL_ (SL_RUN_ROOT, SL_CMUX); callers export SL_RUN_ROOT (port fix 2);
  - the orchestrator special case is GONE (the deterministic runner is not a cmux pane), so the
    orchestrator-specific tests are dropped and the classifier end-to-end checks run on exec panes;
  - PORT FIX 1: read-screen must carry NO --workspace (cmux rejects it there) while send/send-key
    still do — asserted directly below.
"""
import os
import stat
import subprocess
import textwrap

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
NUDGE = os.path.join(REPO_ROOT, "skill", "bin", "nudge-pane.sh")

STUB_CMUX = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -u
    printf '%s\\n' "$*" >> "$STUB_LOG"      # record the full argv of every call
    case "${1:-}" in
      read-screen) printf '%s' "${STUB_SCREEN:-}" ;;   # canned screen -> lib/pane_state classifies
      *) : ;;
    esac
    exit 0
""")

IDLE_SCREEN = "│ > \n╰────────────╯\n  ? for shortcuts"


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path):
    run_root = tmp_path / "run"
    for d in ("state/panes", "state/exited"):
        (run_root / d).mkdir(parents=True, exist_ok=True)
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    cmux = stubdir / "cmux"
    _x(str(cmux), STUB_CMUX)
    log = stubdir / "log"
    log.write_text("")
    return run_root, cmux, log


def _run(run_root, cmux, log, surf, iid, msg, screen=IDLE_SCREEN, agent=None):
    env = {
        **os.environ,
        "SL_RUN_ROOT": str(run_root),
        "SL_CMUX": str(cmux),
        "STUB_LOG": str(log),
        "STUB_SCREEN": screen,
    }
    if agent is not None:
        env["SL_AGENT"] = agent
    return subprocess.run([NUDGE, surf, iid, msg], env=env, capture_output=True, text=True, timeout=30)


def test_read_screen_omits_workspace_but_send_carries_it(tmp_path):
    # PORT FIX 1 regression: read-screen must NOT carry --workspace (cmux rejects it there → the
    # swallowed error left an empty screen → permanent fail-closed defer). send/send-key MUST carry
    # --workspace when known (cross-workspace addressing). This is the exact split the launch machinery
    # needs, and the one a future "restore symmetry" edit would silently break.
    run_root, cmux, log = _setup(tmp_path)
    (run_root / "state" / "panes" / "i1.ws").write_text("WS-UUID-123")
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello")
    assert r.returncode == 0, f"idle pane should accept the send; stderr={r.stderr}"
    calls = log.read_text().splitlines()
    read_line = next((ln for ln in calls if ln.startswith("read-screen")), None)
    assert read_line is not None, f"read-screen not called; calls=\n{calls}"
    assert "--surface SURF-UUID-9" in read_line, f"read-screen missing --surface: {read_line}"
    assert "--workspace" not in read_line, f"read-screen must NOT carry --workspace: {read_line}"
    # send and send-key MUST carry both --surface and --workspace
    for verb in ("send ", "send-key"):
        line = next((ln for ln in calls if ln.startswith(verb.strip())), None)
        assert line is not None, f"{verb} was not called; calls=\n{calls}"
        assert "--surface SURF-UUID-9" in line, f"{verb} missing --surface: {line}"
        assert "--workspace WS-UUID-123" in line, f"{verb} missing --workspace: {line}"


def test_omits_workspace_gracefully_when_ws_unknown(tmp_path):
    # no .ws file -> still works (surface UUID alone), just without the belt-and-suspenders flag.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello")
    assert r.returncode == 0
    assert "--workspace" not in log.read_text()


def test_dead_pane_refuses_to_type(tmp_path):
    # the load-bearing safety: an exited marker => DEAD(4); NEVER send (would run as a shell command
    # in the now-bash pane, permissions-bypassed).
    run_root, cmux, log = _setup(tmp_path)
    (run_root / "state" / "exited" / "i1").write_text("123 rc=0")
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello")
    assert r.returncode == 4, f"a dead pane must return 4, got {r.returncode}"
    assert "send" not in log.read_text(), "must not send into a dead pane"


def test_missing_run_root_fails_loudly(tmp_path):
    # Port fix 2: a caller that forgot to export SL_RUN_ROOT must fail loudly, not silently misbehave.
    run_root, cmux, log = _setup(tmp_path)
    env = {**os.environ, "SL_CMUX": str(cmux), "STUB_LOG": str(log), "STUB_SCREEN": IDLE_SCREEN}
    env.pop("SL_RUN_ROOT", None)
    r = subprocess.run([NUDGE, "SURF-UUID-9", "i1", "hello"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "missing SL_RUN_ROOT must fail (not proceed with an empty root)"


# --------------------------- the classifier consumed end-to-end -----------------------------------
# The whole nudge-pane.sh -> lib/pane_state chain, on the exact bytes that mattered. Unit tests prove
# the pure classifier; these prove the shell pipeline that consumes it SENDS on a real idle composer
# and DEFERS on a real menu — now on an ordinary exec pane (there is no orchestrator surface).

NBSP = "\xa0"
MODERN_IDLE_COMPOSER = (
    "❯" + NBSP + "\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    "  ? for shortcuts"
)
REAL_MENU = "❯ 1. Yes  2. No   (Enter to confirm · Esc to cancel)"


def test_sends_on_modern_nbsp_composer(tmp_path):
    # An idle session showing the modern "❯"+NBSP composer must SEND (exit 0), not be mis-read as a
    # menu and deferred (the WS1 class of bug, now on an exec pane).
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "resume please", screen=MODERN_IDLE_COMPOSER)
    assert r.returncode == 0, f"modern idle composer must send, got rc={r.returncode}; {r.stderr}"
    assert any(ln.startswith("send ") for ln in log.read_text().splitlines()), "must actually send"


def test_defers_on_real_menu(tmp_path):
    # Safety no-regression: a genuine selection menu still DEFERS (3), never a stray Enter into a menu.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "resume please", screen=REAL_MENU)
    assert r.returncode == 3, f"a real menu must defer, got rc={r.returncode}"
    assert not any(ln.startswith("send ") for ln in log.read_text().splitlines())


def test_codex_idle_composer_sends_when_agent_selected(tmp_path):
    run_root, cmux, log = _setup(tmp_path)
    screen = "Earlier output\n\n› \n  ? for shortcuts"
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "resume please",
             screen=screen, agent="codex")
    assert r.returncode == 0, f"Codex idle composer must send, got rc={r.returncode}; {r.stderr}"
    assert any(ln.startswith("send ") for ln in log.read_text().splitlines()), "must actually send"


def test_codex_attention_prompts_defer_when_agent_selected(tmp_path):
    prompts = [
        "Do you trust the contents of this directory?",
        "Approval required\nAllow Codex to run command `pytest`?\nApprove / Deny",
        "You've hit your usage limit. Your usage limit resets later today.",
        "Unrecognized Codex screen",
    ]
    for idx, screen in enumerate(prompts, start=1):
        run_root, cmux, log = _setup(tmp_path / str(idx))
        r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "resume please",
                 screen=screen, agent="codex")
        assert r.returncode == 3, f"Codex attention/unknown screen must defer: {screen!r}"
        assert not any(ln.startswith("send ") for ln in log.read_text().splitlines())
