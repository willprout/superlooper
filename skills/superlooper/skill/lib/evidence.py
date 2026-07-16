"""Structured evidence for every non-success outcome the runner records (issue #152).

The 2026-07-09 launch storm is why this module exists. Ten issues parked under a memo asking
"is the launch shim installed?" while the real cause — a launch anchor pointing at a cmux
workspace that had been deleted — sat in runner.log, read by nobody. The rc was recorded; the
reason was thrown away at `_run_script`, which returned an int and dropped the stderr that named
the fault. A memo written from a bare code can only guess, and it guessed the wrong component:
the shim was installed and innocent, and the launch never reached it.

So: the truth about a failure lives at the point it happens, and the ONLY way it reaches a reader
is if someone captures it there and carries it. The runner captures (stderr from the launch/nudge
scripts, which already diagnose themselves loudly); this module judges and formats. It is pure —
no I/O, no clock, no cmux — so every reading below is unit-testable against the real strings the
tools emit.

Three rules hold it together:

  * FAIL CLOSED, NEVER SILENT. build() ALWAYS emits a `captured` field. When nothing was captured
    it reads CAPTURED_NONE — an honest "captured: none, reason unknown". An ABSENT field would
    read as "nothing went wrong", which is the lie this issue exists to end; validate() therefore
    rejects a record missing it, so an evidence-free failure record cannot be written at all.
  * BOUNDED, ALWAYS. Captured text is caller-controlled — a worker's screen, a tool's stderr — and
    a raw binary in a report once wedged the runner outright (incident 2026-07-07). bound() caps
    the size and strips control bytes; every entry point runs through it, and the park memo bounds
    a second time (the same belt-and-suspenders `_launch_stderr_memo` uses for issue #40).
  * READ THE TEXT, NOT JUST THE CODE. An rc is a category; the captured text is the cause.
    launch-session.sh exits 1 for five different faults, so rc alone can never name one. The
    stderr patterns below refine an ALREADY-FAILED outcome — they never manufacture a failure, so
    a stray substring costs a mis-worded reason, never a false park.
"""

CAPTURED_NONE = "captured: none, reason unknown"

# Bounds. The tail is what matters (a failing command's LAST words name the cause), so both cap
# from the end. STDERR_TAIL_MAX matches actions.LAUNCH_STDERR_MEMO_MAX's intent: enough for a real
# traceback tail, nowhere near a dump.
STDERR_TAIL_MAX = 1200
SCREEN_SNIPPET_MAX = 800

_ELLIPSIS = "…"


def bound(text, limit=STDERR_TAIL_MAX):
    """Sanitize and cap caller-controlled captured text; "" for anything unusable.

    Fail-open on TYPE (never raise into the tick this is describing) but strict on CONTENT: control
    bytes are dropped so a binary or an ANSI-painted TUI screen can never ride into a journal record
    or a GitHub memo. Newlines and tabs survive — a stderr tail and a screen snippet are multi-line,
    and flattening them would cost the reader the shape of the error. Keeps the LAST `limit` chars.
    """
    if not isinstance(text, str):
        return ""
    # Drop C0/C1 control bytes except \n and \t (\r collapses into \n: a TUI screen is full of them
    # and a bare \r would overwrite the line in a terminal that renders the memo).
    cleaned = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch in "\n\t" or (ord(ch) >= 32 and not (127 <= ord(ch) <= 159)):
            cleaned.append(ch)
    out = "".join(cleaned).strip()
    if not out:
        return ""
    if not isinstance(limit, int) or limit < 1:
        limit = STDERR_TAIL_MAX
    if len(out) > limit:
        out = _ELLIPSIS + out[-limit:]
    return out


# ---- what an rc MEANS, per tool ----------------------------------------------------------------
# Keyed to the exit codes the scripts actually document. Each entry is (reason, detail) where the
# detail is the sentence a park memo speaks: it must name the component ACTUALLY at fault, because
# a newcomer reading it will go debug whatever it names.

_LAUNCH_RC = {
    1: ("launch_failed_before_delivery",
        "the launch aborted before any tab could host a worker — no worker was ever started, so "
        "nothing about the session itself is at fault; the captured stderr names the step that "
        "failed"),
    2: ("shim_not_fired",
        "a tab WAS created but no worker ever started in it within the verify window: the launch "
        "shim did not run the dropped command — is it installed? (bin/install-launch-shim.sh)"),
    3: ("base_missing",
        "the worktree base branch does not exist on origin, so every worktree creation fails "
        "before the agent starts — a repo/config fault (dev_branch), not a launch-delivery problem"),
    64: ("agent_unsupported",
         "the configured agent is not one this launcher can start (expected: claude or codex)"),
    124: ("launch_timeout",
          "the launch script never returned within the runner's timeout — it hung rather than "
          "failing, so no exit reason exists to read"),
    127: ("launch_script_unrunnable",
          "the launch script could not be executed at all (missing, or not executable) — the "
          "install may be incomplete"),
}

_NUDGE_RC = {
    1: ("send_failed",
        "cmux refused the write into the pane: the send itself failed, so nothing was delivered "
        "and the session never saw the message"),
    3: ("pane_deferred",
        "the pane could not be safely typed into (a menu, an ambiguous or unreadable screen) so "
        "the runner refused to type and will retry — the session may be perfectly healthy"),
    4: ("pane_dead",
        "the agent process is gone and the pane is a bare shell — typing here would run the "
        "message as a permission-bypassed shell command, so the caller must relaunch instead"),
    5: ("pane_logged_out",
        "the session's auth died in-process: the TUI is alive but every turn is refused, so a "
        "nudge cannot be answered and a relaunch would re-enter dead auth — this needs the owner"),
    6: ("pane_at_dialog",
        "the session is ALIVE and asking its own question in-window, waiting on an answer — going "
        "quiet to wait is not a fault, and parking it would kill a working lane"),
    124: ("nudge_timeout", "the nudge script never returned within the runner's timeout"),
    127: ("nudge_script_unrunnable",
          "the nudge script could not be executed at all (missing, or not executable)"),
}

_RC_TABLES = {"launch": _LAUNCH_RC, "nudge": _NUDGE_RC}

# ---- what the captured TEXT means --------------------------------------------------------------
# Checked BEFORE the rc-only reading, because launch-session.sh exits 1 for five distinct faults
# and only its stderr says which. Matched case-insensitively against the real strings the tools
# emit (cmux's own error text, and the scripts' own `echo ... >&2` diagnostics). Order is
# significant: first match wins, so the most specific patterns lead.
#
# These refine an outcome that has ALREADY failed — they never create one. A substring that
# matches by accident costs a mis-worded reason on a real failure, never a false park.
_LAUNCH_TEXT = (
    # THE storm (2026-07-09). cmux exits 0 while printing this to stdout, so launch-session.sh's
    # surface-parse guard echoes the whole output to stderr — which is how the cause reaches us.
    (("not_found", "pane or workspace not found", "workspace not found", "pane not found"),
     ("anchor_workspace_missing",
      "the launch anchor targets a cmux pane/workspace that no longer exists — cmux resolved no "
      "surface, so no tab was created and the launch never reached the shim. Restart superlooper "
      "in a visible cmux tab in the target pane's own workspace")),
    (("broken pipe", "could not connect"),
     ("anchor_socket_lost",
      "the runner lost its cmux socket (a detached/nohup start, or cmux went away), so it can "
      "reach no pane at all — every launch will fail until it runs inside a visible cmux tab")),
    (("missing brief",),
     ("brief_missing",
      "the launch found no brief file to hand the agent — the runner failed to write it, so the "
      "session had nothing to work from")),
    (("could not create the worktree",),
     ("worktree_create_failed",
      "the worktree could not be created even though its base branch exists — a git-level fault "
      "(a leftover worktree, a locked index, or a branch already checked out elsewhere)")),
    (("sanitize validation failed", "issues.json load"),
     ("identity_invalid",
      "the issue's identity or branch failed validation before anything reached git or the shell "
      "— the runner's own state for this issue is unusable, not the launch machinery")),
)


def _classify(kind, rc, captured):
    """(reason, detail) for a non-success outcome. Text first, then the rc table, then an honest
    fallback that names the rc rather than inventing a cause."""
    if kind == "launch" and captured:
        low = captured.lower()
        for needles, verdict in _LAUNCH_TEXT:
            if any(n in low for n in needles):
                return verdict
    table = _RC_TABLES.get(kind, {})
    if rc in table:
        return table[rc]
    # An rc nobody has mapped. Say exactly that — a guess here is how the storm memo happened.
    return (f"{kind}_rc_{rc}",
            f"the {kind} failed with an exit code this runner has no reading for (rc={rc}) — the "
            "captured text is the only account of what happened")


def build(kind, rc, captured, **extra):
    """The ONE constructor for a non-success record. Always returns a dict carrying `captured`.

    `captured` is the text the runner actually collected at the point of failure (a stderr tail, a
    screen snippet). When it is empty/None/wrong-typed the field falls back to CAPTURED_NONE —
    fail-closed to an honest admission rather than an absent field. The output always survives
    validate(); that pairing is what makes an evidence-free failure record unwritable.
    """
    limit = SCREEN_SNIPPET_MAX if kind == "nudge" else STDERR_TAIL_MAX
    text = bound(captured, limit=limit)
    reason, detail = _classify(kind, rc, text)
    rec = {"kind": str(kind), "rc": rc, "reason": reason, "detail": detail,
           "captured": text or CAPTURED_NONE}
    rec.update({k: v for k, v in extra.items() if k not in rec})
    return rec


def validate(rec):
    """Return `rec` if it is a well-formed evidence record; raise ValueError otherwise.

    This is the schema gate the DoD asks for: a failure record without evidence cannot be written.
    It is deliberately strict and deliberately RAISES — a caller that reaches for a bare code is a
    programmer error to be fixed at the source, not degraded silently at the reader (the journal's
    own write path fails loud for exactly this reason). CAPTURED_NONE passes: "we looked and found
    nothing" is evidence; a missing field is not.
    """
    if not isinstance(rec, dict):
        raise ValueError(f"evidence must be a dict record, got {type(rec).__name__}")
    for field in ("kind", "reason", "detail", "captured"):
        val = rec.get(field)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"evidence record is missing a usable {field!r}: {rec!r}")
    return rec


def summary(rec):
    """A one-line human outcome for the journal/status readers: the reason and rc, never a bare
    code. Degrades to a plain string on a corrupt record — a summary must not crash a tick."""
    if not isinstance(rec, dict):
        return "outcome unknown (no evidence record)"
    return f"{rec.get('kind', '?')} rc={rec.get('rc', '?')} ({rec.get('reason', 'unclassified')})"


# The park memo's own second bound: the memo is a GitHub comment a human reads, so the captured
# tail rides in shorter than the journal's copy.
PARK_MEMO_CAPTURED_MAX = 900


def park_memo(rec, attempts=None):
    """The park memo for a launch that never delivered — the sentence the 07-09 storm should have
    written. Names the component actually at fault from the evidence, then shows the captured
    diagnostic verbatim so the reader can check the runner's reading rather than trust it.

    Degrades, never raises: a park happens on the worst tick of a run, and a corrupt evidence
    record must cost wording, not the hand-back. With no usable record it still says what it does
    know (the attempts) and admits the rest — an honest "reason unknown" beats a confident lie.
    """
    count = attempts if isinstance(attempts, int) and attempts >= 0 else None
    tried = (f"launch was never delivered ({count} verified attempts, or the attempt counter is "
             f"unreadable)") if count is not None else "launch was never delivered"
    if not isinstance(rec, dict):
        return (f"{tried} — and no evidence was recorded for the failure, so the cause cannot be "
                f"named from this runner's records ({CAPTURED_NONE}).")
    detail = rec.get("detail")
    reason = rec.get("reason")
    captured = rec.get("captured")
    if not (isinstance(detail, str) and detail.strip()):
        detail = "the cause was not classified"
    if not (isinstance(reason, str) and reason.strip()):
        reason = "unclassified"
    captured = captured if isinstance(captured, str) and captured.strip() else CAPTURED_NONE
    if captured != CAPTURED_NONE:
        captured = bound(captured, limit=PARK_MEMO_CAPTURED_MAX)
        tail = ("\n\ncaptured at the point of failure (stderr tail — the launcher's own account):\n"
                + captured)
    else:
        tail = f"\n\n{CAPTURED_NONE}"
    return (f"{tried} — {detail} "
            f"(launch rc={rec.get('rc', '?')}, reason `{reason}`).{tail}")
