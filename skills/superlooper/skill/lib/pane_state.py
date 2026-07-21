"""Pure classification of a cmux pane's screen into a send-safety state. No I/O.

classify_screen(text, exited_marker=False, orchestrator=False, agent='claude')
  Claude -> 'dead' | 'logged_out' | 'menu' | 'at_dialog' | 'busy' | 'idle'
  Codex  -> 'dead' | 'busy' | 'idle' | 'trust_blocked' | 'permission_blocked' |
            'quota_blocked' | 'unknown'
auth_death_variant(text) -> which auth-death banner is showing, or None (issue #174). Not a
  send-safety verdict — classify_screen already refuses on all of them — but the only thing the
  OWNER can act on, so it is what the alert body is written from.

The single decision behind every write into a pane (the doorbell AND the orchestrator's
resume/answer/nudge), via bin/nudge-pane.sh:
  - 'dead'  -> the Claude process is gone; the pane is a bash shell. NEVER type (a nudge would
              run as a shell command, permission-bypassed). Caller restarts instead.
  - 'logged_out' -> auth died IN-PROCESS; the TUI is alive but every turn is refused. NEVER type:
              it cannot act, and a nudge just accrues silent failures. Caller alerts the owner.
              Covers the WHOLE auth-death family, not just the /login banner (#174): a bad external
              API key, a revoked OAuth token, an org policy that forbids the auth method in use, a
              failing apiKeyHelper, a gateway that cannot authenticate upstream. One state, because
              the send-safety answer is identical for all of them; auth_death_variant() below says
              WHICH, so the caller's alert can name the owner's actual remedy.
  - 'menu'  -> an interactive selection/confirm/trust prompt is showing; pressing Enter would
              SELECT an item and corrupt state. Defer (retry later).
  - 'at_dialog' -> the session raised its OWN question dialog (AskUserQuestion) and is waiting on
              an answer. Also never typed into (a stray Enter would SELECT an option) — the
              distinction from 'menu' is for the CALLER: this is a live, working session asking
              something in-window, not a stuck one to escalate.
  - 'busy'  -> Claude is mid-generation. Safe to send: Claude QUEUES the input and takes it after
              the current turn.
  - 'idle'  -> a normal Claude input prompt. Safe to send.

Why a pure function: screen-scraping is the only signal an external process has for "is it safe
to type here", and it is render-version-sensitive — so it must be unit-tested in isolation, and
the file markers (state/exited/<id>) are the deterministic backstop for the dangerous DEAD case.

Order matters for Claude: DEAD, then BUSY (so a generation footer's 'esc to interrupt' is never
mis-read as a menu), then MENU, else IDLE. For the ORCHESTRATOR surface we fail CLOSED — any
ambiguous or unrecognized footer, or an unreadable/empty screen, is treated as 'menu' (defer) —
because a stray Enter into the orchestrator corrupts the brain of the whole run, while a deferred
ring is simply retried (review A5).

Codex has its own adapter below. It returns distinct blocked states for status surfaces while
nudge-pane.sh maps those states to the same safe DEFER behavior.
"""
import re

# Claude's generation footer. Seeing this means "generating" — input is safely queued.
_BUSY = re.compile(r"esc(ape)? to interrupt|\binterrupt\b.*\besc", re.I)

# A bash shell prompt on the last non-empty line (the pane after Claude exited), or start-pr.sh's
# explicit "session ended" line. The state/exited/<id> marker is the primary DEAD signal; this is
# the screen-scrape backstop for a hard kill that skipped the marker.
_SESSION_ENDED = re.compile(r"session ended", re.I)
_SHELL_PROMPT = re.compile(r"(^|\n)\s*[^\n]*[%$#]\s*$|➜\s+\S")  # trailing $ / % / # or zsh arrow

# Interactive selection / confirm / trust prompts. Matched on NEWLINE-FLATTENED text so a footer
# split across lines ("Enter to confirm\n  Esc to cancel") still matches (fixes the v1 single-line
# grep fail-open). ❯ before a number = the numbered selection cursor.
_MENU_PATTERNS = [
    re.compile(r"(enter|return) to (confirm|select|continue|submit)[^.]*?(esc|escape) to (cancel|exit|go back)", re.I),
    re.compile(r"(esc|escape) to (cancel|exit|go back)[^.]*?(enter|return) to (confirm|select|continue|submit)", re.I),
    re.compile(r"❯\s*\d+[.\)]"),                 # numbered selection cursor
    re.compile(r"\bdo you want to\b", re.I),     # trust / permission prompt
    re.compile(r"\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|\(yes/no\)", re.I),
    re.compile(r"press enter to continue", re.I),
]

# Broader net used ONLY for the orchestrator surface (fail-closed). Catches selection-ish UI we
# would otherwise default to 'idle'. Deliberately liberal: a false defer is cheap; a false send
# into the orchestrator is not.
_MENU_PATTERNS_STRICT = _MENU_PATTERNS + [
    re.compile(r"(esc|escape) to (cancel|exit|go back)", re.I),
    re.compile(r"use arrow keys|↑/↓|↑ ↓|to select|to navigate", re.I),
    # WS1 (2026-06-29): the modern Claude Code idle composer renders its prompt as a bare "❯" + a
    # NON-BREAKING space (U+00A0). The old strict pattern `(^|\n)\s*❯\s` matched it — `\s` matches
    # NBSP — so EVERY orchestrator ring was mis-read as a "menu" and deferred (run-20260626-1656:
    # 119/119 rings deferred, zero delivered; the whole wake channel silently died). Narrowed to
    # ONLY a "❯" that opens a slash/@ autocomplete dropdown (`❯ /compact`, `❯ @file`), where a stray
    # Enter would SELECT the highlighted entry. A bare "❯ " idle composer now falls through to idle.
    # Genuine numbered menus stay caught by `❯\s*\d+[.\)]` in _MENU_PATTERNS above.
    re.compile(r"❯\s*[/@]"),                     # a slash/@ autocomplete dropdown (NOT idle "❯ ")
    re.compile(r"▶\s|»\s"),
]

# These two states are matched PER LINE and must be the WHOLE line, not a substring of the screen
# (fresh-review P1). A worker session renders its own conversation — the files it reads, the diff it
# writes, the issue it was briefed on — so a bare substring search means the worker assigned this
# very issue reads its own screen as a broken session and disables its own lane. Verified before the
# fix: a 40-line window of THIS file, of actions.py, and of test_pane_state.py each classified as
# 'logged_out'. A banner is a line the TUI draws; a sentence containing the words is someone talking
# ABOUT it.
#
# What may sit AROUND a banner: box rules, and the leading status glyph Claude renders these behind.
# The glyphs are here on evidence, not superstition — our own captured dialog carries
# "⚠ 3 MCP servers need authentication · run /mcp", i.e. an auth-adjacent warning behind a ⚠ with a
# '·'-separated tail, and the bundle builds a ⏺/● status dot on the default render path (our exact
# string escapes it only via an early return). Missing a glyph-prefixed banner would be SILENT and
# would simply restore i336, so the leading glyph is allowed.
#
# "❯"/"⎿" are in the set for the same reason, and they are only safe because the match is a WHOLE-
# LINE fullmatch: an agent line whose entire content is exactly and only this banner is vanishingly
# rare, and since the recover keeps re-sensing, a false positive costs one self-clearing 10-minute
# cycle while a miss costs 94 minutes of typing into a dead pane.
#
# "|" is deliberately NOT here: it is a markdown table delimiter far more often than TUI chrome
# (Claude draws boxes with "│"), and a worker's screen is full of markdown.
_BANNER_DECOR = " \t│┃╭╮╰╯┌┐└┘─━═⏺●○◆◈⚠✗✘✳✻✽⎿❯▪•"


def _banner_lines(raw):
    for ln in raw.splitlines():
        s = ln.strip().strip(_BANNER_DECOR).strip()
        if s:
            yield s


# AUTH DEATH (issue #151 / incident i336). Claude Code renders these INSIDE a live TUI frame, so
# the pane looks like a perfectly normal idle composer and classified as 'idle' = safe-to-send —
# the runner then typed into a pane that could not act for 94 minutes.
#
# The DoD named ONE "exact stable string": "Not logged in · Please run /login" (U+00B7 separator),
# and it is the first pattern below. But grepping the installed Claude Code binary (2.1.211) turned
# up FOUR auth-death messages, not one — the bundle carries "Not logged in · Please run /login",
# "Not logged in · Run /login", "Session expired. Please run /login to sign in again." and
# "Not logged in. Run claude auth login to authenticate.". An exact-only match would leave the
# other three reading as 'idle', which is precisely the bug. So the exact string is the anchor and
# its siblings are covered too; the separator is matched loosely (\W) because only the WORDS are
# stable — a render that swaps "·" for "-" must not silently reopen the hole. That looseness is
# safe ONLY because these are fullmatched against a single line.
#
# ISSUE #174 widened this to the REST of the family. Claude Code 2.1.216 renders TEN distinct
# unanswerable auth-death messages through this same bare-line shape, and before #174 every one of
# them except the four above still classified as 'idle' — safe to send — which is the i336
# mechanism exactly: the runner keeps nudging a pane that can never answer, and the freeze ladder
# reads the silence as a liveness problem rather than an auth one.
#
# ONE STATE, NOT SEVERAL. #174's DoD asked which shape this should take. They are all `logged_out`,
# carrying a VARIANT tag. The send-safety verdict — the only question this module answers — is
# identical for every member ("never type here"), and every downstream guard that acts on it
# (nudge-pane's rc 5, the runner's rc->state map, decide's never-park set, the TERMINAL_STATUSES
# fence) treats them identically. A second state would buy no behaviour while multiplying the
# chance that one of those sites forgets a member — and a forgotten member is SILENT, which is how
# i336 happened. What genuinely differs is the OWNER'S REMEDY ("unset ANTHROPIC_API_KEY" is not
# "/login"), and the remedy belongs in the alert body; the variant is how it gets there.
#
# HOW THE NEW ONES ARE ANCHORED, and why it differs from the four above. The new members are LONG —
# the org-policy banner is 138 characters — so an 80-column pane WRAPS them and #151's
# whole-banner fullmatch would miss every one. So each is anchored on its head clause plus the
# separator, with the rest of the line free (it may be cut mid-word by the wrap). That is still a
# whole-LINE fullmatch, not a substring search: the line must BEGIN with the banner's own opening
# clause, which is what keeps a worker rendering its own conversation from reading its screen as a
# broken session (#151's fresh-review P1 fence, re-run over these files).
#
# THE SEPARATOR is PER PATTERN, and holds only the character that pattern's banner actually uses.
# A single shared class was wrong twice over, both times found by review:
#
#   - #151's loose `\W` matches a plain SPACE, and the bundle ships "Invalid API key format. API key
#     must contain only alphanumeric characters, …" — a NON-fatal validation message sharing a head
#     clause with a genuine banner and separated from its tail by nothing but a space.
#   - a shared `[·•:.]` still let ordinary tool output through, because ":" and "." are how ENGLISH
#     separates a clause from its explanation: "OAuth token revoked: see the runbook",
#     "Authentication error: try again", "AWS authentication failed: check ~/.aws/credentials".
#     A worker's screen is full of tool results and "⎿" is stripped as chrome, so such a line
#     reduces to exactly this shape. Only three banners genuinely use a sentence separator
#     ("Not logged in.", "Session expired.", "Failed to authenticate:"); everything else is "·".
#
# The cost of a false positive here is not "one self-clearing cycle", which is what the #151-era
# comment assumed. For an IDLE pane it is unbounded: decide suppresses the park, the only follow-up
# is a recover that nudge-pane refuses to type into, so nothing ever writes to the pane, the
# offending line never scrolls out of the 40-line window, and the alert stands until a human looks.
# That asymmetry is why every pattern below is anchored on a VERIFIED head clause, why the one
# pattern that had a free head is gone, and why a bare head is allowed ONLY where the head alone
# could not plausibly be anything else.
_SEP_DOT = r"\s*[·•]\s*"          # the "·"-separated family — the overwhelming majority
_SEP_SENTENCE = r"\s*[.:]\s*"     # the three that separate with ordinary sentence punctuation


def _banner(head, tail, sep=_SEP_DOT):
    """`<head clause> <sep> <tail>`, fullmatched against ONE line. The tail is REQUIRED: these heads
    are ordinary English ("Authentication error", "Invalid API key", "Not logged in") and it is the
    tail that makes the line Claude Code's rather than some other tool's."""
    return re.compile(head + sep + tail, re.I)


def _clipped(head, tail=r".*", sep=_SEP_DOT):
    """Same, but the separator AND the tail are OPTIONAL — for heads unmistakable on their own.

    FRESH-REVIEW P1-1: requiring the tail meant a pane narrow enough to wrap or CLIP the banner at
    its separator read as plain 'idle' = safe to send. Measured at a plain 80 columns with the
    message list's normal 2-char indent, "Your organization has disabled Claude subscription access
    for Claude Code ·" was the entire first line and classified idle — i336 restored, silently, on
    the exact members this issue exists to catch. Several of these render inside a fixed-height box
    in the bundle, so they CLIP: the tail is not on a second line, it is gone.

    Reserved for the five heads that name a Claude-specific condition in full ("Your organization
    has disabled …", "Your apiKeyHelper script is failing", "Your account does not have access to
    Claude"). Round 2 of the review pulled `not logged in`, `session expired`, `oauth token revoked`,
    `invalid api key` and the four cloud-credential strings BACK out of this set: each is short
    enough that a realistic pane never clips it, and each is a sentence some other tool on the
    machine emits. The tail here is left OPEN rather than pinned, because a pinned tail leaves a
    dead band — a clip landing inside the tail matches neither arm.

    The tail is optional INSIDE the separator group, not just alongside it: a clip can land one
    character past the separator ("… Claude Code ·") as easily as before it."""
    return re.compile(head + r"(?:" + sep + r"(?:" + tail + r")?)?", re.I)


# (variant, pattern) in match order — the FIRST match wins, so more specific tails precede looser
# ones on a shared head. Every literal is verified against the installed binary
# (~/.local/share/claude/versions/2.1.216), read out of its interned string table; none is invented.
_LOGGED_OUT_PATTERNS = [
    # --- the /login family: #151's four, byte-for-byte. They keep the original loose `\W` because
    # they are the proven anchors i336 was closed on and nothing has been reported against them.
    ("login", re.compile(r"not logged in\s*\W\s*please run /login", re.I)),  # #151's exact string
    ("login", re.compile(r"not logged in\s*\W\s*run /login", re.I)),
    ("login", re.compile(r"not logged in\W+run claude auth login to authenticate\W*", re.I)),
    ("login", re.compile(r"session expired\W+please run /login to sign in again\W*", re.I)),
    # The same two, tolerant of a CLIPPED tail (P1-1) but with the tail still PINNED (round-2 P1-2):
    # a bare "Not logged in" or "Session expired" line is something `gh`, `docker` and half the CLIs
    # on the machine print, and it says nothing about THIS session's auth.
    ("login", _banner(r"not logged in", r"run claude auth login.*", sep=_SEP_SENTENCE)),
    ("login", _banner(r"session expired", r"please run /login.*", sep=_SEP_SENTENCE)),
    ("login", _banner(r"your session has expired", r"please run /login.*", sep=_SEP_SENTENCE)),
    ("login", _banner(r"login expired", r"please run /login.*")),
    # The bundle's own generic first-party render: `Please run /login \xB7 ${API Error}: ${detail}`.
    # Tail pinned to "API error", because "Please run /login" is a sentence a human writes.
    ("login", _banner(r"please run /login", r"api error\b.*")),
    # The CLAUDE_CODE_REMOTE render of the SAME banner. The bundle's own ternary is
    #   Wt(process.env.CLAUDE_CODE_REMOTE) ? "Authentication error \xB7 Try again"
    #                                      : "Not logged in \xB7 Run /login"
    # so #174's last two DoD bullets ("Authentication error · Try again" and "the CLAUDE_CODE_REMOTE
    # variant of the login banner") are the same string at the same code site. It is tagged
    # separately anyway: an owner who sees it is in a remote environment, and the remedy differs.
    ("login_remote", _banner(r"authentication error", r"try again\W*")),
    # --- the token was killed or expired server-side: /login again, and if it RECURS the token was
    # revoked deliberately, which no amount of re-login fixes.
    ("oauth_revoked", _banner(r"oauth token revoked", r"(?:please )?run /login.*")),
    # Tail carries "expired" (round-2 P1-2): a bare "oauth session" prefix also matches sentences
    # like "Failed to authenticate: OAuth session token was not returned by the provider", which is
    # somebody else's OAuth, not Claude's. Clip floor is 45 columns — well inside any real pane.
    ("oauth_revoked",
     _banner(r"failed to authenticate", r"oauth session expired.*", sep=_SEP_SENTENCE)),
    # `Failed to authenticate. ${API Error}: ${detail}` — the remote sibling of the /login render
    # above. Pinned to "API error" for the same reason: a bare "Failed to authenticate: …" is a
    # sentence half the tools on the machine emit, and it is not about Claude's auth.
    ("auth_error", _banner(r"failed to authenticate", r"api error\b.*", sep=_SEP_SENTENCE)),
    # --- an EXTERNAL credential is in force and is bad. The remedy is the opposite of "/login":
    # the key must be FIXED or UNSET, and unsetting is what falls back to the subscription.
    ("invalid_api_key", _banner(r"invalid api key", r"fix external api key.*")),
    # --- org POLICY, or a disabled org, forbids the credential in use. Nothing the loop can do.
    # These four heads are long, Claude-specific and clip at realistic widths, so they are the
    # `_clipped` set.
    ("api_key_org_disabled",
     _clipped(r"your anthropic_api_key belongs to a disabled organization")),
    ("org_api_key_disabled",
     _clipped(r"your organization has disabled api key authentication")),
    ("subscription_disabled",
     _clipped(r"your organization has disabled claude subscription access for claude code")),
    ("no_account_access",
     _clipped(r"your account does not have access to claude", sep=_SEP_SENTENCE)),
    # --- the credential SOURCE is broken rather than the credential itself.
    ("apikey_helper_failing", _clipped(r"your apikeyhelper script is failing")),
    # Tails deliberately short: at a narrow pane the long ones clip, and "the gateway"/"this may be"
    # already separate these two from each other and from the CLAUDE_CODE_REMOTE render above.
    ("gateway_auth", _banner(r"authentication error", r"the gateway.*")),
    ("auth_error", _banner(r"authentication error", r"this may be a temporary.*")),
    # --- the Bedrock / Vertex credential paths. Same block in the bundle, same consequence (the
    # session cannot take a turn). The "·" tail is REQUIRED (round-2 P1-2): the bundle always
    # renders these as `${msg} \xB7 run \`${cmd}\` and retry \xB7 ${API Error}: ${detail}`, while a
    # BARE "AWS authentication failed" is exactly what the owner's own aws CLI prints in a tool
    # result — and that says nothing about this session's auth.
    ("cloud_credentials", _banner(r"aws credentials expired or invalid", r".*")),
    ("cloud_credentials", _banner(r"aws authentication failed", r".*")),
    ("cloud_credentials", _banner(r"google cloud credentials expired or invalid", r".*")),
    ("cloud_credentials", _banner(r"google cloud authentication failed", r".*")),
]


def _auth_death(lines):
    """The variant key of the auth-death banner on one of `lines`, or None. First match wins."""
    for variant, pat in _LOGGED_OUT_PATTERNS:
        if any(pat.fullmatch(ln) for ln in lines):
            return variant
    return None


def auth_death_variant(text):
    """Which auth-death banner a screen is showing — the key the ALERT uses to name the owner's
    actual remedy — or None if there is none.

    Split out from classify_screen because the two answer different questions and only one of them
    is a send-safety decision. classify_screen says "never type here" (identical for every member);
    this says WHY, which is the only part the owner can act on. Pure and separately unit-tested for
    the same reason the rest of this module is: it is render-version-sensitive.

    A `logged_out` screen ALWAYS yields a variant — classify_screen consults this same table, so
    the two can never disagree. Callers must still treat None as "no usable variant" and fall
    back to generic wording, because the value travels through a text channel that can be lost.
    """
    return _auth_death(list(_banner_lines(text or "")))

# THE SESSION'S OWN QUESTION DIALOG (issue #151 / incident i280). A worker blocked on its own
# AskUserQuestion went stale, tripped the frozen tier, and the nudge ladder walked a LIVE, working
# lane into a false park.
#
# These two patterns are deliberately POSITIVE anchors, not "a menu without permission wording".
# Real captures of both screens (tests/fixtures/screens/, taken from Claude Code 2.1.211 driven in
# a live cmux pane) show why: the folder-trust prompt renders "❯ 1. Yes, I trust this folder" under
# an "Enter to confirm · Esc to cancel" footer and contains NO "do you want to"/"do you trust"
# wording at all — so any exclusion rule would have mis-read a genuine trust menu as a question and
# quietly stopped escalating it. What the question dialog has and no permission menu ever does is
# its two tail rows: the free-text "Other" row (rendered with the placeholder "Type something." —
# in the bundle, `multiSelect ? "Type something" : "Type something."`) and the "Chat about this"
# escape row. Both are stable literals in the bundle.
#
# Fullmatched against a WHOLE line (with the option cursor allowed to lead it), so a row is a row:
# the sentence "ask me to type something", a doc table naming the row, and this file's own source
# all fail to match where a bare substring search would have fired (fresh-review P1).
_AT_DIALOG_ROWS = [
    re.compile(r"[❯\s]*\d+\s*[.)]\s*type something\.?\s*", re.I),
    re.compile(r"[❯\s]*\d+\s*[.)]\s*chat about this\s*", re.I),
]

# Codex-specific screen clues. Keep these out of the Claude path: a bare "›" is Codex's idle
# composer, while Claude's modern composer uses "❯".
_CODEX_BUSY_PATTERNS = [
    re.compile(r"\bworking\b.*\besc(?:ape)? to interrupt\b", re.I),
    re.compile(r"\brunning\s+posttooluse\s+hook\b", re.I),
    re.compile(r"\brunning\s+\w+\s+hook\b", re.I),
]
_CODEX_IDLE = re.compile(r"(^|\n)\s*›(?:\s|\xa0|$)")
_CODEX_TRUST = re.compile(r"\bdo you trust the contents of this directory\?", re.I)
_CODEX_QUOTA = re.compile(
    r"\busage limit resets\b|\busage limits? (?:resets?|reached)\b|\bquota\b.*\b(resets?|reached|exceeded)\b",
    re.I,
)
_CODEX_PERMISSION_PATTERNS = [
    re.compile(r"\bpermission\b.*\b(approve|approval|allow|deny)\b", re.I),
    re.compile(r"\b(approve|allow|deny)\b.*\b(tool|command|permission)\b", re.I),
    re.compile(r"\bdo you want to (?:allow|approve)\b", re.I),
    re.compile(r"\bapproval required\b", re.I),
]


def _flatten(text):
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def _looks_dead(raw, flat):
    if _SESSION_ENDED.search(flat):
        return True
    return bool(not _BUSY.search(raw) and _SHELL_PROMPT.search(raw) and "│" not in raw and "╰" not in raw)


def _classify_codex(raw, flat, exited_marker=False):
    if exited_marker:
        return "dead"
    if _looks_dead(raw, flat):
        return "dead"
    if not flat:
        return "unknown"
    if _CODEX_QUOTA.search(flat):
        return "quota_blocked"
    if _CODEX_TRUST.search(flat):
        return "trust_blocked"
    for pat in _CODEX_PERMISSION_PATTERNS:
        if pat.search(flat) or pat.search(raw):
            return "permission_blocked"
    for pat in _CODEX_BUSY_PATTERNS:
        if pat.search(flat) or pat.search(raw):
            return "busy"
    if _CODEX_IDLE.search(raw):
        return "idle"
    return "unknown"


def classify_screen(text, exited_marker=False, orchestrator=False, agent="claude"):
    raw = text or ""
    flat = _flatten(raw)
    if agent == "codex":
        return _classify_codex(raw, flat, exited_marker=exited_marker)

    if exited_marker:
        return "dead"

    # DEAD (screen-scrape backstop to the exited marker): explicit end line, or a bare shell
    # prompt with no sign of a live Claude TUI.
    if _looks_dead(raw, flat):
        return "dead"
    if not flat:
        # Unreadable/empty screen -> DEFER for BOTH surfaces (review BASH-3). A live idle Claude
        # always renders its input box, so an empty read means a read glitch OR a dead/garbage pane
        # (e.g. a hard-killed session whose exited marker didn't get written). Deferring a doorbell/
        # nudge one cycle is far cheaper than a stray command into a permission-bypassed bash shell.
        return "menu"
    # LOGGED_OUT before BUSY (issue #151): 'busy' is a SAFE-TO-SEND state (Claude queues input), so
    # a stale generation footer lingering under an auth-death banner would otherwise hand back a
    # green light on a pane that cannot act. Refusing a genuinely-busy pane costs one retry; typing
    # into dead auth is the 94-minute failure this state exists to end.
    lines = list(_banner_lines(raw))
    if _auth_death(lines):
        return "logged_out"
    if _BUSY.search(flat):
        return "busy"

    # AT_DIALOG before the menu table (issue #151): the question dialog's own footer reads
    # "Enter to select · ↑/↓ to navigate · Esc to cancel", which the generic menu patterns match —
    # so checking it after would mean this state could never fire. Safe to put first because the
    # anchors are AskUserQuestion-only rows (see _AT_DIALOG_PATTERNS): no permission/trust menu
    # renders them, which the real trust-screen fixture pins as a regression test.
    #
    # The ORCHESTRATOR surface is excluded and keeps failing closed to 'menu' (review A5): a stray
    # Enter there corrupts the brain of the whole run, and no caller acts on at_dialog for it.
    if not orchestrator:
        for pat in _AT_DIALOG_ROWS:
            if any(pat.fullmatch(ln) for ln in lines):
                return "at_dialog"

    patterns = _MENU_PATTERNS_STRICT if orchestrator else _MENU_PATTERNS
    for pat in patterns:
        if pat.search(flat) or pat.search(raw):
            return "menu"
    return "idle"
