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


# ---------------------------------------------------------------------------
# Issue #151: honest session-state sensing — logged_out + at_dialog.
# The screens below are REAL captures from Claude Code 2.1.211 driven in a cmux
# pane (tests/fixtures/screens/), not hand-written guesses: the trust dialog and
# the AskUserQuestion dialog render nearly identical selection chrome, and only a
# real capture settles which markers actually separate them.
# ---------------------------------------------------------------------------

import pathlib

_SCREENS = pathlib.Path(__file__).parent / "fixtures" / "screens"


def _screen(name):
    return (_SCREENS / name).read_text()


# --- logged_out (i336: a dead-auth pane classified 'idle', so the runner typed into it 94 min) ---

def test_logged_out_exact_string_from_the_dod():
    # The exact stable string named in issue #151, byte-for-byte (U+00B7 MIDDLE DOT separator).
    assert ps.classify_screen("Not logged in · Please run /login") == "logged_out"


def test_logged_out_sibling_variants_are_not_idle():
    # Claude Code 2.1.211 ships FOUR auth-death messages, not one (verified by grepping the
    # installed binary). Matching only the DoD's exact string would leave these three classifying
    # as 'idle' — i.e. safe-to-send — which IS the i336 bug. None of them may read as idle.
    for screen in ("Not logged in · Run /login",
                   "Session expired. Please run /login to sign in again.",
                   "Not logged in. Run claude auth login to authenticate."):
        assert ps.classify_screen(screen) == "logged_out", screen


def test_logged_out_inside_a_live_tui_frame_is_still_logged_out():
    # The real failure was a logged-out message rendered INSIDE a live composer frame — which is
    # exactly why it read as a normal idle prompt and got typed into.
    screen = ("╭───────────────────────────────╮\n"
              "│ Not logged in · Please run /login │\n"
              "╰───────────────────────────────╯\n"
              "❯ \n")
    assert ps.classify_screen(screen) == "logged_out"


def test_logged_out_beats_busy_and_is_never_typed_into():
    # If a stale generation footer is still on screen under a logged-out banner, refuse. 'busy' is
    # a SAFE-TO-SEND state (Claude queues input); sending into dead auth is the thing to prevent.
    screen = "Not logged in · Please run /login\n✻ Thinking… (esc to interrupt)"
    assert ps.classify_screen(screen) == "logged_out"


def test_dead_still_beats_logged_out():
    # A logged-out line scrolled into a dead bash pane must stay DEAD: typing there would run the
    # nudge as a permission-bypassed shell command (RC-DEADPANE), the worst outcome on the table.
    assert ps.classify_screen("Not logged in · Please run /login\nwilliam@mac probe % ") == "dead"
    assert ps.classify_screen("Not logged in · Please run /login", exited_marker=True) == "dead"


# --- at_dialog (i280: a worker at its own AskUserQuestion dialog read as 'frozen' -> false park) ---

def test_real_askuserquestion_dialog_is_at_dialog():
    # REAL capture: Claude Code driven to call AskUserQuestion in a live cmux pane.
    assert ps.classify_screen(_screen("claude-askuserquestion-dialog.txt")) == "at_dialog"


def test_real_trust_dialog_stays_menu_not_at_dialog():
    # REAL capture of the folder-trust prompt. This is the boundary the issue draws: a genuine
    # menu keeps its exact current classification. Note it carries NO 'do you want to' and NO
    # 'do you trust' — it is caught only by the numbered-cursor pattern, and its footer
    # ('Enter to confirm · Esc to cancel') is near-identical to the question dialog's. Any
    # at_dialog rule defined by EXCLUDING known permission wording would mis-read this as a
    # question. That is why at_dialog is anchored on positive, AskUserQuestion-only markers.
    assert ps.classify_screen(_screen("claude-trust-folder.txt")) == "menu"


def test_at_dialog_markers_are_the_askuserquestion_only_rows():
    # The two rows the real dialog always renders and no permission menu ever does: the free-text
    # 'Other' row (placeholder "Type something.") and the 'Chat about this' escape row. Both are
    # stable literals in the 2.1.211 bundle.
    assert ps.classify_screen("Which colour?\n❯ 1. Red\n  2. Blue\n  3. Type something.\n"
                              "Enter to select · ↑/↓ to navigate · Esc to cancel") == "at_dialog"
    assert ps.classify_screen("Pick one\n❯ 1. A\n  2. B\n  4. Chat about this\n"
                              "Enter to select · Esc to cancel") == "at_dialog"


def test_at_dialog_is_distinct_from_frozen_and_from_menu():
    # The DoD's core ask: at_dialog is its own state. A live session waiting on an in-window
    # question is neither dead, nor frozen, nor a permission menu.
    state = ps.classify_screen(_screen("claude-askuserquestion-dialog.txt"))
    assert state not in ("frozen", "menu", "dead", "idle", "busy")


def test_ordinary_menus_and_prompts_are_unchanged():
    # Regression fence around the boundary "do not change refusal behavior for genuine menus".
    assert ps.classify_screen("❯ 1. Yes  2. No   (Enter to confirm · Esc to cancel)") == "menu"
    assert ps.classify_screen("Do you want to proceed?\n❯ 1. Yes\n  2. No") == "menu"
    assert ps.classify_screen("Continue? (y/n)") == "menu"


def test_orchestrator_surface_stays_fail_closed_on_a_dialog():
    # The orchestrator surface fails CLOSED by design (review A5): every ambiguous selection screen
    # defers as 'menu'. at_dialog is a worker-surface signal and must not weaken that.
    assert ps.classify_screen(_screen("claude-askuserquestion-dialog.txt"),
                              orchestrator=True) == "menu"


def test_new_states_require_the_claude_agent_path():
    # Mirrors the existing Codex-leak fence: Claude-specific labels must not appear for agent=codex.
    for screen in ("Not logged in · Please run /login",
                   _screen("claude-askuserquestion-dialog.txt")):
        assert ps.classify_screen(screen, agent="codex") not in ("logged_out", "at_dialog")
