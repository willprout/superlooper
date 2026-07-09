"""Pure classification of a cmux pane's screen into a send-safety state. No I/O.

classify_screen(text, exited_marker=False, orchestrator=False, agent='claude')
  Claude -> 'dead' | 'menu' | 'busy' | 'idle'
  Codex  -> 'dead' | 'busy' | 'idle' | 'trust_blocked' | 'permission_blocked' |
            'quota_blocked' | 'unknown'

The single decision behind every write into a pane (the doorbell AND the orchestrator's
resume/answer/nudge), via bin/nudge-pane.sh:
  - 'dead'  -> the Claude process is gone; the pane is a bash shell. NEVER type (a nudge would
              run as a shell command, permission-bypassed). Caller restarts instead.
  - 'menu'  -> an interactive selection/confirm/trust prompt is showing; pressing Enter would
              SELECT an item and corrupt state. Defer (retry later).
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
    if _BUSY.search(flat):
        return "busy"

    patterns = _MENU_PATTERNS_STRICT if orchestrator else _MENU_PATTERNS
    for pat in patterns:
        if pat.search(flat) or pat.search(raw):
            return "menu"
    return "idle"
