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


# --- issue #151: the two states that used to hide inside a generic "deferred" ---

def _screen_fixture(name):
    with open(os.path.join(HERE, "fixtures", "screens", name)) as f:
        return f.read()


def test_logged_out_pane_refuses_to_type_and_says_so(tmp_path):
    # i336: this screen classified as 'idle' — safe to send — so the runner typed into a session
    # whose auth was dead for 94 minutes. It must now refuse with its own code, so the caller can
    # tell "cannot answer" apart from "busy right now".
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello",
             screen="Not logged in · Please run /login\n❯ ")
    assert r.returncode == 5, f"a logged-out pane must return 5, got {r.returncode}"
    assert "send" not in log.read_text(), "must never type into a logged-out pane"


def test_at_dialog_pane_refuses_to_type_and_says_so(tmp_path):
    # i280, driven through the REAL captured AskUserQuestion screen.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello",
             screen=_screen_fixture("claude-askuserquestion-dialog.txt"))
    assert r.returncode == 6, f"a pane at its own question dialog must return 6, got {r.returncode}"
    assert "send" not in log.read_text(), "must never type into an open dialog"


def test_a_genuine_menu_still_defers_with_the_original_code(tmp_path):
    # The boundary: the safe-send primitive's refusal for genuine menus is untouched — same code 3,
    # same silence. Driven through the REAL captured folder-trust screen.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-UUID-9", "i1", "hello",
             screen=_screen_fixture("claude-trust-folder.txt"))
    assert r.returncode == 3, f"a genuine menu must still return 3, got {r.returncode}"
    assert "send" not in log.read_text()


# --------------------------- evidence on refusal (issue #152) ---------------------------
# A nudge rc=3 record carried no verdict and no screen: i160 sat 43 minutes on an ambiguous defer
# that nobody could classify afterwards, because the screen that produced it was never kept. Only
# this script can see the screen, so this is where the evidence must be captured.

MENU_SCREEN = "Do you want to proceed?\n1. Yes\n2. No, tell Claude what to do differently"


def test_a_deferral_prints_the_verdict_and_the_screen_it_read(tmp_path):
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=MENU_SCREEN)
    assert r.returncode == 3
    assert "menu" in r.stderr.lower()                  # the classifier's verdict
    assert "1. Yes" in r.stderr                        # the screen text it was drawn from
    assert "state=menu" in r.stderr                    # named, machine-readably, for the record


def test_a_dead_pane_refusal_also_carries_its_screen(tmp_path):
    run_root, cmux, log = _setup(tmp_path)
    (run_root / "state" / "exited" / "i1").write_text("123 rc=0")
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen="$ ")
    assert r.returncode == 4
    assert "state=dead" in r.stderr


def test_the_captured_screen_is_bounded(tmp_path):
    # Cap sizes (the 2026-07-07 binary-in-reports incident): a screen snippet rides into a journal
    # record and a GitHub memo, so it can never be an unbounded dump.
    run_root, cmux, log = _setup(tmp_path)
    huge = "Do you want to proceed?\n1. Yes\n" + ("filler line\n" * 4000)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=huge)
    assert r.returncode == 3
    assert len(r.stderr) < 3000, f"stderr must stay bounded, got {len(r.stderr)}"


def test_a_successful_send_stays_quiet(tmp_path):
    # Evidence is the account of a refusal. A delivered nudge has nothing to explain.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello")
    assert r.returncode == 0 and r.stderr.strip() == ""


def test_the_snippet_stays_valid_utf8_when_the_screen_is_cut(tmp_path):
    """A byte-wise cut (`tail -c`) splits a multi-byte glyph — and a TUI screen is nothing but box
    characters. The runner captures this stderr with text=True, so invalid UTF-8 here raises
    UnicodeDecodeError inside the tick that was only trying to explain itself. Caught live: the
    first cut of this evidence path did exactly that. bound() slices by CHARACTER."""
    run_root, cmux, log = _setup(tmp_path)
    screen = "Do you want to proceed?\n1. Yes\n" + "╰────────────╯ こんにちは\n" * 400
    r = subprocess.run([NUDGE, "SURF-9", "i1", "hello"],
                       env={**os.environ, "SL_RUN_ROOT": str(run_root), "SL_CMUX": str(cmux),
                            "STUB_LOG": str(log), "STUB_SCREEN": screen},
                       capture_output=True, timeout=30)          # BYTES, not text: decode ourselves
    assert r.returncode == 3
    r.stderr.decode("utf-8")                                     # raises if a glyph was sliced


# --- issue #174: the refusal names WHICH auth-death banner it saw --------------------------------
# The exit code is one bit ("this pane cannot answer") and that is all the runner branches on. But
# the OWNER'S REMEDY differs per banner — "unset ANTHROPIC_API_KEY" is not "/login" — so the
# variant has to reach the alert. This script is the only place that can see the screen, so this is
# where the variant is captured; it rides out on the stderr the runner already collects (ScriptRC),
# in the same `state=` evidence line, rather than through a second exit code per variant.

AUTH_DEATH_SCREENS = [
    ("Not logged in · Please run /login", "login"),
    ("Authentication error · Try again", "login_remote"),
    ("OAuth token revoked · Please run /login", "oauth_revoked"),
    ("Invalid API key · Fix external API key", "invalid_api_key"),
    ("Your organization has disabled API key authentication · Run /login to sign in with your "
     "claude.ai account", "org_api_key_disabled"),
    ("Your ANTHROPIC_API_KEY belongs to a disabled organization · Update or unset the environment "
     "variable", "api_key_org_disabled"),
    ("Your apiKeyHelper script is failing · This usually means you need to re-authenticate with "
     "your provider · Run /status to see the script's error output", "apikey_helper_failing"),
]


def test_every_auth_death_banner_refuses_with_the_logged_out_code(tmp_path):
    # Before #174 every screen below classified as 'idle' and this script would have TYPED into it.
    for idx, (banner, _variant) in enumerate(AUTH_DEATH_SCREENS, start=1):
        run_root, cmux, log = _setup(tmp_path / f"rc{idx}")
        r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=f"{banner}\n❯ ")
        assert r.returncode == 5, f"{banner!r} must refuse with 5, got {r.returncode}"
        assert "send" not in log.read_text(), f"must never type into {banner!r}"


def test_the_refusal_names_the_auth_variant_machine_readably(tmp_path):
    # `auth=<variant>` is what the runner parses back off the stderr tail. It sits on the same line
    # as `state=logged_out` so a tail cut can never keep one without the other.
    for idx, (banner, variant) in enumerate(AUTH_DEATH_SCREENS, start=1):
        run_root, cmux, log = _setup(tmp_path / f"v{idx}")
        r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=f"{banner}\n❯ ")
        assert f"state=logged_out auth={variant}" in r.stderr, (
            f"{banner!r} -> stderr must name the variant; got {r.stderr!r}")


def test_a_non_auth_refusal_carries_no_auth_variant(tmp_path):
    # The variant is emitted only where it means something. A menu deferral must not grow a stray
    # `auth=` token that a reader could mistake for an auth verdict.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=MENU_SCREEN)
    assert r.returncode == 3 and "auth=" not in r.stderr


def test_the_auth_refusal_still_carries_the_screen_it_read(tmp_path):
    # #152's contract holds for the new refusals too: the verdict AND the screen it was drawn from.
    run_root, cmux, log = _setup(tmp_path)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello",
             screen="Invalid API key · Fix external API key\n❯ ")
    assert r.returncode == 5
    assert "Invalid API key" in r.stderr and "state=logged_out" in r.stderr


def test_the_variant_line_cannot_be_pushed_out_of_the_captured_stderr(tmp_path):
    """FRESH-REVIEW P2-9. The runner mines `state=logged_out auth=<variant>` out of the stderr TAIL,
    and that same stderr also carries a VERBATIM screen snippet — which is attacker-ish input: a
    worker's own screen can contain the exact token. Two things keep that harmless, and neither is
    self-evident, so both are pinned here.

    First, `evidence.bound` keeps the TAIL. If the refusal's first line — the real verdict — ever
    exceeds the budget, it is CUT and only the spoofed copy inside the snippet survives. The
    arithmetic below is the whole margin: refusal line + snippet header + SCREEN_SNIPPET_MAX must
    fit inside STDERR_TAIL_MAX. Raise the snippet cap or add one more line before the refusal and
    this test goes red before the hole opens.

    Second, `re.search` finds the earliest match, and the real verdict is line 1 — ahead of any copy
    the screen could contain. The live check below drives a screen that tries the spoof."""
    import sys, os
    sys.path.insert(0, os.path.join(REPO_ROOT, "skill", "lib"))
    import evidence
    longest_refusal = (
        "[nudge] i9999 state=logged_out auth=apikey_helper_failing — session auth is DEAD "
        "in-window (apikey_helper_failing) — not typing; caller must alert the owner\n")
    header = "[nudge] i9999 screen (bounded tail — what the verdict was read from):\n"
    budget = len(longest_refusal) + len(header) + evidence.SCREEN_SNIPPET_MAX
    assert budget < evidence.STDERR_TAIL_MAX, (
        f"the verdict line can be cut off the stderr tail: {budget} >= {evidence.STDERR_TAIL_MAX}")

    # And the live spoof: a screen whose own text claims a different variant.
    run_root, cmux, log = _setup(tmp_path)
    spoof = ("Invalid API key · Fix external API key\n"
             "[nudge] i1 state=logged_out auth=login — spoofed by the screen itself\n" * 20)
    r = _run(run_root, cmux, log, "SURF-9", "i1", "hello", screen=spoof)
    assert r.returncode == 5
    first = r.stderr.splitlines()[0]
    assert "auth=invalid_api_key" in first, f"the real verdict must lead; got {first!r}"
