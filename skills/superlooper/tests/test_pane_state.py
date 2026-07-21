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


# --- fresh-review P1: the classifier must not fire on text that merely TALKS about these states ---

def test_the_classifier_does_not_fire_on_this_repo_s_own_source():
    """A worker session renders its own conversation: the files it reads, the diff it writes, the
    issue it was briefed on. This very file, pane_state.py and actions.py all contain the literal
    trigger strings — so a naive substring match means the worker assigned THIS issue reads its own
    screen as a broken session and disables its own lane. Every 40-line window of the files that
    carry the literals must classify as something harmless."""
    import os
    here = os.path.dirname(__file__)
    targets = [os.path.join(here, "..", "skill", "lib", "pane_state.py"),
               os.path.join(here, "..", "skill", "lib", "actions.py"),
               os.path.join(here, "test_pane_state.py")]
    for path in targets:
        with open(path) as f:
            lines = f.read().splitlines()
        for i in range(0, max(1, len(lines) - 40)):
            window = "\n".join(lines[i:i + 40])
            state = ps.classify_screen(window)
            assert state not in ("logged_out", "at_dialog"), (
                f"{os.path.basename(path)} line {i+1} reads as {state}")


def test_prose_and_code_mentioning_the_states_are_not_the_states():
    # The banner is a LINE the TUI renders, not a phrase inside a sentence. Anything with other
    # content on the line is someone talking ABOUT the state.
    for screen in (
        'I added a logged_out state that fires on "Not logged in · Please run /login".',
        '    re.compile(r"not logged in\\s*\\W\\s*please run /login", re.I),  # the exact string',
        "the alert body says (Not logged in · Please run /login) so the owner knows",
        "| footer | `N. Chat about this` escape row | the free-text row |",
        '    re.compile(r"\\d\\s*[.)]\\s*chat about this", re.I),',
        "Ask me to type something. 3. or so.",
    ):
        assert ps.classify_screen(screen) not in ("logged_out", "at_dialog"), screen


def test_a_real_banner_is_still_caught_inside_its_box():
    # The flip side: a genuine banner is its own line, and the TUI may draw a box around it. The
    # box must not hide it.
    for screen in ("Not logged in · Please run /login",
                   "│ Not logged in · Please run /login │",
                   "╭────────────────────────────────╮\n│ Not logged in · Please run /login │\n"
                   "╰────────────────────────────────╯\n❯ "):
        assert ps.classify_screen(screen) == "logged_out", screen


def test_a_banner_behind_a_ui_glyph_is_still_caught():
    """FRESH-REVIEW P1 — a MISS here is silent, and silence means i336 all over again.

    Evidence this shape is real, from our own capture: claude-askuserquestion-dialog.txt line 11 is
    "⚠ 3 MCP servers need authentication · run /mcp" — Claude Code demonstrably renders auth-adjacent
    warnings behind a leading glyph with a '·'-separated tail. The bundle agrees: the status dot
    (⏺/●) is built on the default render path, and our exact string escapes it only by an early
    return, while the "Session expired" message is thrown from a 401 handler and would take the
    dotted path.

    Whole-line fullmatching is what makes the leading glyph safe to allow: an agent message whose
    ENTIRE content is exactly and only this banner is vanishingly rare, and — since the recover now
    keeps re-sensing — a false positive costs one self-clearing 10-minute cycle, while a miss costs
    94 minutes of typing into a pane that cannot answer. The asymmetry favours catching it."""
    for glyph in ("⏺", "●", "⚠", "✗", "⎿", "✻", "○"):
        screen = f"{glyph} Not logged in · Please run /login"
        assert ps.classify_screen(screen) == "logged_out", screen
    assert ps.classify_screen("  ⚠  Session expired. Please run /login to sign in again.") == "logged_out"


def test_a_markdown_table_row_is_not_a_banner():
    # '|' is a markdown table delimiter far more often than it is TUI chrome (Claude draws boxes
    # with '│'), and a worker's screen is full of markdown — including this PR's own body.
    assert ps.classify_screen("| Not logged in · Please run /login |") != "logged_out"
    assert ps.classify_screen("| state | Not logged in · Please run /login | the banner |") != "logged_out"


def test_a_markdown_blockquote_of_a_dialog_row_is_not_a_dialog():
    # '>' was admitted as a possible option cursor; Claude uses '❯'. Dropping it costs nothing and
    # stops a quoted row in a review comment or chat log from reading as a live dialog.
    assert ps.classify_screen("> 3. Type something.") != "at_dialog"
    assert ps.classify_screen("> 4. Chat about this") != "at_dialog"


# ---------------------------------------------------------------------------
# Issue #174: the REST of the auth-death family.
#
# #151 closed i336's exact shape — the "/login" banner. But Claude Code renders a WIDER family of
# unanswerable auth-death messages through the same bare-line shape, and every one of them still
# classified as 'idle' (= safe to send), which is the i336 mechanism exactly.
#
# Every string below is VERIFIED against the installed binary
# (~/.local/share/claude/versions/2.1.216), read out of the interned string table at file offsets
# 0x414_1xxx and 0x804_2xxx — not invented. The full block is reproduced in the PR body.
#
# The DoD asked which shape these should take: ONE refusing state or several. They are one state —
# `logged_out` — carrying a VARIANT tag. The send-safety verdict is identical for all of them
# ("never type here"), and every downstream guard that acts on it (nudge-pane's rc 5, the runner's
# rc->state map, decide's never-park set, the TERMINAL_STATUSES fence) treats them identically —
# so a second state buys no behaviour and multiplies the chance one site forgets a member, which
# would silently reopen the hole. What genuinely differs is the OWNER'S REMEDY, and that lives in
# the alert body; the variant is how it gets there.
# ---------------------------------------------------------------------------

# The banner -> variant table, exactly as captured. Each tuple is (screen line, expected variant).
AUTH_DEATH_BANNERS = [
    # the /login family (#151's four, plus the fifth the binary carries beside them)
    ("Not logged in · Please run /login", "login"),
    ("Not logged in · Run /login", "login"),
    ("Not logged in. Run claude auth login to authenticate.", "login"),
    ("Session expired. Please run /login to sign in again.", "login"),
    ("Login expired · Please run /login", "login"),
    # the CLAUDE_CODE_REMOTE render of that SAME banner — the bundle's own ternary is
    #   Wt(process.env.CLAUDE_CODE_REMOTE) ? "Authentication error · Try again"
    #                                      : "Not logged in · Run /login"
    # so the DoD's last two bullets ("Authentication error · Try again" and "the CLAUDE_CODE_REMOTE
    # variant of the login banner") turn out to be the same string at the same code site.
    ("Authentication error · Try again", "login_remote"),
    # the token was killed server-side
    ("OAuth token revoked · Please run /login", "oauth_revoked"),
    ("Failed to authenticate: OAuth session expired and could not be refreshed", "oauth_revoked"),
    # an EXTERNAL key is in force and is bad
    ("Invalid API key · Fix external API key", "invalid_api_key"),
    ("Your ANTHROPIC_API_KEY belongs to a disabled organization · Unset the environment variable "
     "to use your subscription instead", "api_key_org_disabled"),
    ("Your ANTHROPIC_API_KEY belongs to a disabled organization · Update or unset the environment "
     "variable", "api_key_org_disabled"),
    # org policy forbids the auth method in use
    ("Your organization has disabled API key authentication · Run /login to sign in with your "
     "claude.ai account", "org_api_key_disabled"),
    ("Your organization has disabled API key authentication · Unset ANTHROPIC_API_KEY to use your "
     "claude.ai account instead", "org_api_key_disabled"),
    ("Your organization has disabled API key authentication · Unset ANTHROPIC_API_KEY and run "
     "/login to sign in with your claude.ai account", "org_api_key_disabled"),
    ("Your organization has disabled API key authentication · Unset the apiKeyHelper setting and "
     "run /login to sign in with your claude.ai account", "org_api_key_disabled"),
    ("Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic "
     "API key instead, or ask your admin to enable access", "subscription_disabled"),
    # the credential SOURCE is broken rather than the credential
    ("Your apiKeyHelper script is failing · This usually means you need to re-authenticate with "
     "your provider · Run /status to see the script's error output", "apikey_helper_failing"),
    ("Authentication error · The gateway could not authenticate with its upstream provider — "
     "contact your gateway administrator", "gateway_auth"),
    ("Authentication error · This may be a temporary network issue, please try again", "auth_error"),
    ("Your session has expired. Please run /login to sign in again.", "login"),
    ("Your account does not have access to Claude. Please login again or contact your "
     "administrator.", "no_account_access"),
    # The Bedrock / Vertex credential paths. The bundle always renders these with the "·" tail
    # (`${msg} · run `${cmd}` and retry · API Error: ${detail}`), and the tail is REQUIRED — a bare
    # "AWS authentication failed" is what the owner's own aws CLI prints in a tool result.
    ("AWS credentials expired or invalid · run `aws sso login` and retry", "cloud_credentials"),
    ("AWS authentication failed · run `aws sso login` and retry", "cloud_credentials"),
    ("Google Cloud credentials expired or invalid · run `gcloud auth application-default login` "
     "and retry", "cloud_credentials"),
    ("Google Cloud authentication failed · if credentials are current, check GCP IAM permissions",
     "cloud_credentials"),
]

# The narrowest pane, in AVAILABLE columns, at which each banner must still refuse. This is not
# decoration: it is the residual clip floor, and it is inherent to anchoring on a head clause —
# below it, the head itself is cut and nothing can recognise the line short of re-joining wrapped
# rows. Writing it down turns "we think 80 is fine" into an assertion that breaks if a pattern's
# required tail ever grows. Anything at or under 80 is safe on any real cmux pane.
CLIP_FLOOR = 73          # the worst case: subscription_disabled's head is exactly 73 characters


def test_every_auth_death_banner_refuses_instead_of_reading_as_idle():
    """The whole issue in one assertion: before this, every banner below classified as 'idle' —
    safe to send — so the runner would keep nudging a pane that can never answer and the freeze
    ladder would read the silence as a liveness problem rather than an auth one."""
    for banner, _variant in AUTH_DEATH_BANNERS:
        assert ps.classify_screen(banner) == "logged_out", banner


def test_each_banner_reports_its_own_variant():
    """The variant is what lets the alert name the RIGHT remedy: 'unset ANTHROPIC_API_KEY' is not
    the same instruction as '/login', and telling an owner the wrong one costs them the night."""
    for banner, variant in AUTH_DEATH_BANNERS:
        assert ps.auth_death_variant(banner) == variant, banner


def test_a_screen_with_no_auth_banner_has_no_variant():
    for screen in ("❯ ", "✻ Thinking… (esc to interrupt)", "", "❯ 1. Yes  2. No"):
        assert ps.auth_death_variant(screen) is None, screen


def test_auth_death_banners_survive_inside_a_live_tui_frame():
    """i336's actual shape: the banner rendered INSIDE a live composer frame, which is exactly why
    it read as a normal idle prompt and got typed into for 94 minutes."""
    for banner, variant in AUTH_DEATH_BANNERS:
        screen = ("╭──────────────────────────────╮\n"
                  f"│ {banner} │\n"
                  "╰──────────────────────────────╯\n"
                  "❯ \n")
        assert ps.classify_screen(screen) == "logged_out", banner
        assert ps.auth_death_variant(screen) == variant, banner


def test_auth_death_banners_are_caught_behind_a_status_glyph():
    """Same evidence as #151's glyph fence: Claude Code renders auth-adjacent warnings behind a
    leading status glyph, and a MISS here is silent."""
    for glyph in ("⏺", "●", "⚠", "✗", "⎿"):
        for banner, variant in AUTH_DEATH_BANNERS:
            screen = f"{glyph} {banner}"
            assert ps.classify_screen(screen) == "logged_out", screen
            assert ps.auth_death_variant(screen) == variant, screen


def test_a_wrapped_banner_is_still_caught():
    """The new members are LONG — the org-policy one is 138 characters — so an 80-column pane wraps
    them and #151's whole-banner fullmatch would miss every one. The head clause plus its separator
    is what is anchored on, so the FIRST wrapped line still fires."""
    screen = ("Your organization has disabled API key authentication · Unset the apiKeyHelper\n"
              "setting and run /login to sign in with your claude.ai account\n"
              "❯ ")
    assert ps.classify_screen(screen) == "logged_out"
    assert ps.auth_death_variant(screen) == "org_api_key_disabled"


def test_auth_death_beats_busy_and_is_never_typed_into():
    """'busy' is a SAFE-TO-SEND state (Claude queues input), so a stale generation footer under an
    auth-death banner must not hand back a green light on a pane that cannot act."""
    for banner, _variant in AUTH_DEATH_BANNERS:
        assert ps.classify_screen(f"{banner}\n✻ Thinking… (esc to interrupt)") == "logged_out"


def test_dead_still_beats_every_auth_death_banner():
    """A banner scrolled into a dead bash pane stays DEAD: typing there would run the nudge as a
    permission-bypassed shell command (RC-DEADPANE), the worst outcome on the table."""
    for banner, _variant in AUTH_DEATH_BANNERS:
        assert ps.classify_screen(f"{banner}\nwilliam@mac probe % ") == "dead", banner
        assert ps.classify_screen(banner, exited_marker=True) == "dead", banner


def test_auth_death_variants_require_the_claude_agent_path():
    """Mirrors the existing Codex-leak fence: Claude-specific labels must not appear for codex."""
    for banner, _variant in AUTH_DEATH_BANNERS:
        assert ps.classify_screen(banner, agent="codex") != "logged_out", banner


def test_the_bundles_own_generic_render_shapes_are_anchored_too():
    """Claude Code carries its own normalizer for these banners, and its regexes name the stable
    generic shapes: `^Please run /login \xB7 `, `^Failed to authenticate\\. ` and `^Not logged in$`.
    Those shapes are anchored so a renamed variant is caught on arrival.

    Their tails are pinned to "API Error" rather than left open. That is the fresh review's P1-2/P1-3
    lesson: an open tail on an ordinary English head ("Failed to authenticate: ...") fires on half
    the tool output a worker's own screen carries, and the false positive is NOT self-clearing."""
    for screen in ("Please run /login · API Error: 401 unauthorized",
                   "Failed to authenticate. API Error: 403 forbidden"):
        assert ps.classify_screen(screen) == "logged_out", screen
    # `^Not logged in$` is NOT anchored, though the bundle's normalizer names it. Round 2 of the
    # review read that normalizer properly: it is a REPLACE CHAIN, and `^Not logged in$` only ever
    # sees the residue left after ` · Please run /login$` has already been stripped — so it is not
    # evidence of a standalone render, and a bare "Not logged in" line is something `gh` and
    # `docker` print. Both real siblings are matched literally above.
    assert ps.classify_screen("Not logged in") != "logged_out"


def test_the_open_headed_suffix_net_is_gone():
    """It was the only pattern here with a free HEAD, and the fresh review showed what that costs:
    Claude Code's markdown renderer strips inline-code backticks, so a reviewer's own sentence about
    this feature renders as a bare `<something> · Please run /login` line and disables the lane of
    the worker discussing it. Dropping it costs a hypothetical future variant; keeping it cost a
    demonstrated false positive on the very screen this code runs under."""
    assert ps.classify_screen("Credential handoff went sideways · Please run /login") != "logged_out"


# --- the fences: these must NOT fire ------------------------------------------------------------

def test_the_new_patterns_do_not_fire_on_neighbouring_non_auth_messages():
    """Every string here is a REAL Claude Code message from the same region of the bundle that is
    NOT auth death — the session can still take a turn. Refusing on these would hold a healthy lane
    and page the owner for nothing.

    'Invalid API key format. …' is the sharp one: it shares a head clause with a genuine banner and
    is separated only by an ordinary space, which is precisely why the separator is an explicit
    punctuation class rather than #151's loose `\\W`."""
    for screen in (
        "Invalid API key format. API key must contain only alphanumeric characters, dashes, and "
        "underscores.",
        "Credit balance is too low",
        "Prompt is too long",
        "Repeated 529 Overloaded errors",
        "Server is temporarily limiting requests (not your usage limit)",
        "Request timed out",
        "Opus is experiencing high load, please use /model to switch to Sonnet",
        "⚠ 3 MCP servers need authentication · run /mcp",
    ):
        assert ps.classify_screen(screen) != "logged_out", screen
        assert ps.auth_death_variant(screen) is None, screen


def test_prose_and_code_mentioning_the_new_banners_are_not_the_banners():
    """The #151 discipline, extended: a banner is a LINE the TUI draws; a sentence containing the
    words is someone talking ABOUT it. A worker session renders its own conversation — the diff it
    writes, the issue it was briefed on — so a bare substring search means the worker assigned THIS
    issue reads its own screen as a broken session and disables its own lane."""
    for screen in (
        'I added a variant for "Invalid API key · Fix external API key" this morning.',
        '    ("invalid_api_key", _banner(r"invalid api key", r"fix external api key")),',
        "| variant | Invalid API key · Fix external API key | unset the key |",
        "- `OAuth token revoked · …`",
        "the alert body says (Authentication error · Try again) so the owner knows",
        "> Your organization has disabled API key authentication · Run /login to sign in",
        "see Authentication error · Try again in the table above for the remote render",
    ):
        assert ps.classify_screen(screen) != "logged_out", screen


def test_the_classifier_still_does_not_fire_on_this_repo_s_own_source():
    """#151's fence, re-run against the files this issue ADDS the literals to. Verified before the
    #151 fix that a 40-line window of these files each classified as 'logged_out'; the same must
    stay false now that the family is ten times larger and the alert bodies quote the banners."""
    import os
    here = os.path.dirname(__file__)
    # Every file that now carries the banner literals — the fresh review (P2-5) caught the first
    # cut omitting the three test files and the fixture README, which is where most of the literals
    # actually live.
    targets = [os.path.join(here, "..", "skill", "lib", "pane_state.py"),
               os.path.join(here, "..", "skill", "lib", "actions.py"),
               os.path.join(here, "..", "skill", "bin", "runner.py"),
               os.path.join(here, "..", "skill", "bin", "nudge-pane.sh"),
               os.path.join(here, "test_pane_state.py"),
               os.path.join(here, "test_actions.py"),
               os.path.join(here, "test_nudge_pane.py"),
               os.path.join(here, "test_runner.py"),
               os.path.join(here, "fixtures", "screens", "README.md")]
    for path in targets:
        with open(path) as f:
            lines = f.read().splitlines()
        # range end is len-39, not len-40: the last window must REACH the final line (P2-5). With
        # len-40 the sweep stopped one line short and never looked at the end of any file.
        for i in range(0, max(1, len(lines) - 39)):
            window = "\n".join(lines[i:i + 40])
            state = ps.classify_screen(window)
            assert state not in ("logged_out", "at_dialog"), (
                f"{os.path.basename(path)} line {i+1} reads as {state}")


def test_a_real_healthy_pane_carrying_auth_warnings_is_still_idle():
    """REAL capture, Claude Code 2.1.216, driven live in a pty at 100x40 and rendered through a
    terminal emulator (tests/fixtures/screens/claude-idle-with-auth-warnings.txt).

    This is the fixture that matters most for #174, and it was found by accident while trying to
    induce a real auth-death screen: a perfectly HEALTHY session renders

        ⚠ Your login expires in 4 days · run /login to renew

    which is a `·`-separated line, behind a ⚠ glyph, that names /login — the exact shape every
    pattern in this family matches on. The session is fine; the warning is a courtesy. Any looser
    net over the /login shape reads this as auth death, refuses to nudge a working lane, and pages
    the owner at 3am about a session that is doing its job. That is the false-positive direction
    #174 has to stay on the right side of, and a hand-written screen would never have shown it."""
    screen = _screen("claude-idle-with-auth-warnings.txt")
    assert ps.classify_screen(screen) == "idle"
    assert ps.auth_death_variant(screen) is None
    # and each warning line on its own, so a future re-capture cannot hide a regression in chrome
    for line in ("⚠ Your login expires in 4 days · run /login to renew",
                 "⚠ 4 MCP servers need authentication · run /mcp"):
        assert ps.classify_screen(line) == "idle", line
        assert ps.auth_death_variant(line) is None, line


# --- fresh-review P1s (issue #174) --------------------------------------------------------------

def test_a_clipped_banner_still_refuses_at_a_narrow_pane_width():
    """FRESH-REVIEW P1-1. The first cut required whitespace AFTER the separator, so any wrap or
    clip that landed the separator at end-of-line killed the match outright and the screen went
    back to reading 'idle' — i336 restored, silently, on the exact members this issue exists for.

    It is not hypothetical: the reviewer measured it at a plain 80 columns with the message list's
    normal 2-character indent, where 'Your organization has disabled Claude subscription access for
    Claude Code ·' is the whole first line. And several of these render inside a fixed-height box in
    the bundle, so they CLIP — the tail is gone entirely, not pushed to a second line.

    The long heads are unmistakable on their own, so they now match head-only too."""
    # The `_clipped` set, and only it. Round 2 pulled `OAuth token revoked` and `Invalid API key`
    # back out: both banners are under 40 characters, so no realistic pane ever clips them, and both
    # heads are sentences other tools print. Every head below names a Claude-specific condition in
    # full and could not plausibly be anything else.
    heads = [
        ("Your organization has disabled Claude subscription access for Claude Code",
         "subscription_disabled"),
        ("Your organization has disabled API key authentication", "org_api_key_disabled"),
        ("Your ANTHROPIC_API_KEY belongs to a disabled organization", "api_key_org_disabled"),
        ("Your apiKeyHelper script is failing", "apikey_helper_failing"),
        ("Your account does not have access to Claude", "no_account_access"),
    ]
    for head, variant in heads:
        seps = [".", " ."] if variant == "no_account_access" else ["·", " ·"]
        for line in [head] + [head + " " + sp for sp in seps] + [f"  {head} {seps[0]}"]:
            assert ps.classify_screen(line) == "logged_out", repr(line)
            assert ps.auth_death_variant(line) == variant, repr(line)


def test_the_full_family_survives_a_hard_clip_at_every_realistic_width():
    """The P1-1 defect, swept rather than spot-checked — and swept for real this time.

    ROUND 2 of the review caught the first version of this test being a tautology: it tried only
    widths 80/100/120 at indent 0, and at none of those does any banner's clip land on its
    separator, so it was green against the buggy code it claimed to guard. It was written from the
    patch, not from the risk.

    This version sweeps every width from the declared CLIP_FLOOR up, at every indent a real render
    puts in front of a message, and clips the way a fixed-height box actually does."""
    for banner, _variant in AUTH_DEATH_BANNERS:
        for indent in range(0, 8):
            for width in range(CLIP_FLOOR + indent, 141):
                clipped = (" " * indent + banner)[:width].rstrip()
                assert ps.classify_screen(clipped) == "logged_out", (
                    f"width={width} indent={indent}: {clipped!r}")


def test_unrelated_tool_output_mentioning_authentication_is_not_claude_auth_death():
    """FRESH-REVIEW P1-2, the false-positive direction, and the expensive one. A worker's own screen
    is full of tool results — a curl against a registry, a failing deploy — and '⎿' is stripped as
    TUI chrome, so a bash result line reduces to a bare English sentence. None of these say anything
    about CLAUDE's auth, and refusing on one holds a working lane.

    The cost is worse than 'one self-clearing cycle': decide suppresses the park and the only
    follow-up is a recover that nudge-pane refuses to type into — so nothing ever writes to the
    pane, the offending line never scrolls out of the 40-line window, and the alert stands until a
    human intervenes. That is why the separator class is now only what Claude Code actually renders
    ('·' and the ':'/'.' the two 'Failed to authenticate' forms use) and why the open-tailed
    'Failed to authenticate' net was replaced by its two real shapes."""
    for screen in (
        "⎿  Failed to authenticate: bad credentials",
        "⎿  Failed to authenticate. Retrying in 5s",
        "Failed to authenticate: 401 from the artifact registry",
        "OAuth token revoked - see the runbook",
        "Authentication error - try again",
        "⎿  Invalid API key - the deploy token expired",
        "Login expired - re-run the fixture generator",
        # ROUND 2 of the review: the first fence only tried the "-" forms — i.e. exactly the
        # characters that cut had just deleted from the separator class — so it was written from the
        # patch rather than from the risk and proved nothing. These are the ":" and "." forms, and
        # they are the shapes ordinary English actually uses to introduce an explanation.
        "OAuth token revoked: see the runbook",
        "OAuth token revoked. See the runbook.",
        "Authentication error: try again",
        "Failed to authenticate: OAuth session token was not returned by the provider",
        "AWS authentication failed: check ~/.aws/credentials",
        "Google Cloud authentication failed: no ADC found",
        "AWS authentication failed",
        "Not logged in",
        "Not logged in: run `gh auth login` first",
        "Not logged in. Run `docker login` to continue.",
        "Session expired",
        "Session expired.",
        "Invalid API key",
        "Invalid API key.",
        "Invalid API key:",
    ):
        assert ps.classify_screen(screen) != "logged_out", screen
        assert ps.auth_death_variant(screen) is None, screen


def test_rendered_markdown_prose_about_this_feature_is_not_a_banner():
    """FRESH-REVIEW P1-3, and the sharpest one: the source fence tests RAW files, where quotes and
    backticks are still present — but Claude Code's markdown renderer strips inline-code backticks
    before drawing the line, and '⏺' (the assistant-message prefix) is stripped as chrome. So a
    sentence a reviewer of this very PR would plausibly write renders as a bare banner-shaped line.

    The generic '<anything> · Please run /login' suffix net was the only pattern here with an open
    HEAD, and this is what it cost: the worker assigned this issue disabling its own lane while
    talking about the fix. It is gone. Every remaining pattern is anchored on a verified head
    clause, which is #151's discipline and the reason that fence held for two issues."""
    for screen in (
        "⏺ I anchored the generic suffix net on · Please run /login",
        "⏺ The banner reads Not logged in · Please run /login when auth dies",
        "the shape is <detail> · Please run /login and it is now gone",
        "⏺ see the table: variant login · Please run /login",
    ):
        assert ps.classify_screen(screen) != "logged_out", screen
