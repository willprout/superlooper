import pane_state as ps


def test_exited_marker_is_dead():
    assert ps.classify_screen("anything at all", exited_marker=True) == "dead"
    assert ps.classify_screen("", exited_marker=True) == "dead"


def test_session_ended_line_is_dead():
    screen = "[pr-01] session ended 16:12 — scroll up to inspect, or: claude --resume\nuser@host ~ %"
    assert ps.classify_screen(screen) == "dead"


def test_bare_shell_prompt_is_dead():
    assert ps.classify_screen("$ ") == "dead"
    assert ps.classify_screen("some output\nwilliam@mac autocode % ") == "dead"
    assert ps.classify_screen("➜  autocode git:(main) ") == "dead"


def test_busy_generation_footer():
    assert ps.classify_screen("✻ Thinking…\n  (esc to interrupt)") == "busy"
    assert ps.classify_screen("Running tool…  Esc to interrupt") == "busy"


def test_busy_beats_menu_match():
    # A generation footer contains 'esc to ...' — must NOT be mis-read as a menu (would defer
    # forever while it generates). Busy is checked first and is safe to send (Claude queues).
    assert ps.classify_screen("Working\nesc to interrupt") == "busy"


def test_menu_single_line():
    assert ps.classify_screen("❯ 1. Yes  2. No   (Enter to confirm · Esc to cancel)") == "menu"


def test_menu_multiline_footer_fail_open_fixed():
    # The v1 single-line grep MISSED this (footer split across lines) -> fail-open send. Now caught.
    screen = "Do you trust this folder?\n\n  Enter to confirm\n  Esc to cancel\n"
    assert ps.classify_screen(screen) == "menu"


def test_menu_trust_prompt():
    assert ps.classify_screen("Do you want to proceed?\n 1. Yes  2. No") == "menu"


def test_menu_yes_no():
    assert ps.classify_screen("Overwrite file? (y/n)") == "menu"


def test_idle_normal_prompt():
    screen = "│ > \n╰────────────────╯\n  ? for shortcuts"
    assert ps.classify_screen(screen) == "idle"


def test_idle_benign_numbered_output_not_a_menu():
    # A numbered LIST in normal output (no selection cursor, no confirm/cancel footer) is idle.
    screen = "Here are the steps:\n1. first\n2. second\n3. third\n│ > "
    assert ps.classify_screen(screen) == "idle"


def test_empty_screen_defers_on_both_surfaces():
    # A5 + BASH-3: an unreadable/empty screen DEFERS for both — a live idle Claude always renders
    # its input box, so empty means a read glitch or a dead/garbage pane. Deferring is cheaper than
    # a stray Enter into the orchestrator (corrupts the brain) or a command into a dead bash pane.
    assert ps.classify_screen("", orchestrator=True) == "menu"
    assert ps.classify_screen("", orchestrator=False) == "menu"


def test_orchestrator_fails_closed_on_arrow_menu():
    # An unrecognized selection footer that the standard set would call 'idle' is deferred for the
    # orchestrator surface (broadened menu net).
    screen = "Pick a model\n  claude-opus\n  claude-sonnet\n  use arrow keys to select"
    assert ps.classify_screen(screen, orchestrator=False) == "idle"   # exec: lenient
    assert ps.classify_screen(screen, orchestrator=True) == "menu"    # orch: fail closed


def test_orchestrator_still_rings_a_clean_idle_prompt():
    # Fail-closed must NOT mean "never ring" — a clearly-idle prompt is still idle.
    screen = "│ > \n╰────────────╯\n  ? for shortcuts"
    assert ps.classify_screen(screen, orchestrator=True) == "idle"


# --------------------------- WS1: the modern idle composer is NOT a menu ---------------------------
# run-20260626-1656 rang the orchestrator 119 times; every ring `deferred` because the modern Claude
# Code idle composer renders "❯" + a NON-BREAKING space (U+00A0), and the old strict pattern
# `(^|\n)\s*❯\s` matched it (\s matches NBSP). The whole wake channel silently died. These pin the
# repair AND the autocomplete fail-open it could otherwise open.

# The load-bearing byte: a real NON-BREAKING space. Hand-typing a plain " " would NOT reproduce the
# bug (a plain space never tripped \s differently) — the NBSP is exactly why the old suite was green
# while prod deferred 119/119, so this fixture MUST carry  .
NBSP = " "
# A faithful modern idle composer: the "❯" prompt at line start + NBSP, then the real live footer.
MODERN_IDLE_COMPOSER = (
    "Some earlier assistant output ended here.\n"
    "\n"
    "❯" + NBSP + "\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    "  ? for shortcuts"
)


def test_modern_nbsp_composer_is_idle_for_orchestrator():
    # THE regression fix (RC1): the real NBSP composer must classify idle for the orchestrator, so
    # the doorbell ring sends instead of deferring forever.
    assert NBSP == "\xa0"                                   # guard: the fixture really is U+00A0
    assert "❯ " in MODERN_IDLE_COMPOSER                # guard: ❯ is immediately followed by NBSP
    assert ps.classify_screen(MODERN_IDLE_COMPOSER, orchestrator=True) == "idle"
    assert ps.classify_screen(MODERN_IDLE_COMPOSER, orchestrator=False) == "idle"


def test_orchestrator_typed_draft_is_idle():
    # A normal in-progress draft (no slash/@ autocomplete, no digit/arrow/confirm cue) is idle —
    # safe to send (Claude appends/queues). Fail-closed must not mean "never ring a working brain".
    assert ps.classify_screen("❯ draft text the orchestrator typed", orchestrator=True) == "idle"
    assert ps.classify_screen("❯" + NBSP + "rebuild the plan", orchestrator=True) == "idle"


def test_orchestrator_numbered_and_arrow_menus_still_deferred():
    # Safety no-regression: removing the bare-❯ pattern must NOT let a genuine selection menu send.
    assert ps.classify_screen("❯ 1. Yes  2. No", orchestrator=True) == "menu"
    assert ps.classify_screen("❯ 2. Skip this", orchestrator=True) == "menu"
    arrow = "Pick a model\n  claude-opus\n❯ claude-sonnet\n  use arrow keys to select"
    assert ps.classify_screen(arrow, orchestrator=True) == "menu"


def test_slash_autocomplete_is_deferred_fail_open_pin():
    # DECIDED behavior (red-team attack #6): a "❯ /compact" (or "❯ @file") autocomplete dropdown is
    # an OPEN selection — a stray Enter would run/select it. So it DEFERS (menu), not idle. This is
    # the explicit pin so removing the bare-❯ pattern never silently reopens this fail-open.
    assert ps.classify_screen("❯ /compact", orchestrator=True) == "menu"
    assert ps.classify_screen("❯ /clear", orchestrator=True) == "menu"
    assert ps.classify_screen("❯" + NBSP + "@src/main.py", orchestrator=True) == "menu"
    # ...but the exec (lenient) surface is unaffected and still treats a plain composer as idle.
    assert ps.classify_screen(MODERN_IDLE_COMPOSER, orchestrator=False) == "idle"


def test_permission_dialog_classifies_as_menu_defer():
    # fact-3 regression pin: the Claude Code permission/hook-ask dialog (the exact surface Run 1's
    # spend-guard sat on) must classify `menu` -> nudge-pane DEFER(3), so the orchestrator can NEVER
    # answer it through the safe primitive. _MENU_PATTERNS already carries `\bdo you want to\b`
    # (pane_state.py:43) and the numbered-cursor `❯\s*\d+[.\)]`, so this PASSES today — it stays as a
    # permanent pin: the WS1 episode proves these patterns get narrowed under pressure, and this stops
    # a future narrowing from silently re-opening the dialog surface.
    dialogs = [
        "Bash command\n\nscripts/ship.sh request-ci 42\n\nMetered CI spend (about 2-4 USD per run). "
        "CLAUDE.md Money rule: requires explicit owner spend confirmation in this conversation first.\n\n"
        "Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently",
        "Do you want to allow this tool use?\n❯ 1. Allow\n  2. Deny",
    ]
    for screen in dialogs:
        assert ps.classify_screen(screen, exited_marker=False) == "menu", screen[:40]


# --------------------------- Codex adapter fixtures ----------------------------------------------

CODEX_IDLE_COMPOSER = (
    "Welcome to Codex\n\n"
    "› \n"
    "  ? for shortcuts"
)


def test_codex_busy_fixtures():
    assert ps.classify_screen("Working (12s • esc to interrupt)", agent="codex") == "busy"
    assert ps.classify_screen("Running PostToolUse hook", agent="codex") == "busy"


def test_codex_idle_composer_fixture():
    assert ps.classify_screen(CODEX_IDLE_COMPOSER, agent="codex") == "idle"


def test_codex_trust_prompt_fixture():
    screen = "Do you trust the contents of this directory?\n\n  y yes\n  n no"
    assert ps.classify_screen(screen, agent="codex") == "trust_blocked"


def test_codex_permission_approval_prompt_fixtures():
    screens = [
        "Approval required\nAllow Codex to run command `pytest`?\n  Approve\n  Deny",
        "Permission requested: approve or deny this tool use",
        "Do you want to allow this command?",
    ]
    for screen in screens:
        assert ps.classify_screen(screen, agent="codex") == "permission_blocked"


def test_codex_usage_limit_fixture_is_quota_blocked():
    screen = "You've hit your usage limit. Your usage limit resets at 5:00 PM."
    assert ps.classify_screen(screen, agent="codex") == "quota_blocked"


def test_codex_dead_fixtures():
    assert ps.classify_screen("anything", exited_marker=True, agent="codex") == "dead"
    assert ps.classify_screen("[i1] session ended 16:12 — scroll up, or: codex resume\n$ ",
                              agent="codex") == "dead"
    assert ps.classify_screen("william@mac superlooper % ", agent="codex") == "dead"


def test_codex_unknown_fixtures_fail_closed():
    assert ps.classify_screen("", agent="codex") == "unknown"
    assert ps.classify_screen("Some unrecognized Codex screen", agent="codex") == "unknown"


def test_codex_only_states_require_codex_agent_selection():
    # The default Claude path remains the historical four-state classifier. Codex-specific labels
    # must not leak into Claude classification unless the caller selected agent="codex".
    codex_screens = [
        "Do you trust the contents of this directory?",
        "Approval required\nAllow Codex to run command `pytest`?",
        "usage limit resets at 5:00 PM",
        "Some unrecognized Codex screen",
    ]
    codex_only = {"trust_blocked", "permission_blocked", "quota_blocked", "unknown"}
    for screen in codex_screens:
        assert ps.classify_screen(screen) not in codex_only
