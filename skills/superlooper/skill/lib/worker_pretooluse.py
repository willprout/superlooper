"""The Claude PreToolUse hook's core (issues #156 + #185): two of the costliest
session-instruction-drift incidents made mechanically impossible rather than instructed-against.

pretooluse-hook.sh fences the session (loop-only, Claude-only, cwd-safe) and hands the hook
payload here. This decides ONE thing: does this tool call cross one of two named hard lines, and if
so, what reason does the session receive? The spike (docs/SPIKE-2026-07-15-hook-capabilities.md)
proved a PreToolUse hook returning permissionDecision:"deny" blocks the call even under
--dangerously-skip-permissions, with the reason delivered to the model verbatim.

  1. ASKUSERQUESTION. An interactive dialog in an unattended lane has no human to answer it; it
     stalls the lane until someone clears it by hand (incident i280 — all night). The deny does not
     just forbid; it hands back the DURABLE protocol that session was supposed to use — and WHICH
     protocol that is depends on the session's role, because handing a session someone else's
     escalation channel is itself the drift this hook exists to stop.
  2. PATTERN-KILLS. `pkill -f` / `killall` match by name/pattern, so a session's kill can also match
     the OWNER's own live processes — a worker once killed the owner's live dashboard this way. The
     deny restates the standing CLAUDE.md rule: record the PID ($!) and kill only that exact PID.
  3. MECHANICALLY-INVALID `gh issue create` (issue #225). The 2026-07-16 queue audit found 25 of 35
     open issues unschedulable — 16 of them `agent-ready` — for want of a `type:` label and/or a
     parseable `## Loop metadata` section. Workers file follow-ups all day and were never handed
     the mechanical format. The owner's redesign (2026-07-16) is this module's own principle
     applied to a third hazard: stop TEACHING the format in the first prompt ("extra instructions
     in the very first prompt get ignored half the time" — his words) and BOUNCE the session at the
     moment it files wrong. The issue is validated BEFORE it exists; the deny names exactly what is
     missing and the required shape; the session retries correctly on the freshest possible turn.

DENY ONLY THE THREE NAMED HAZARDS. Everything else is allowed — no broad allowlist that could break
legitimate tool use (issue Boundaries).

EVERY UNATTENDED SESSION THE LOOP LAUNCHES (owner ruling on #185, 2026-07-16). #156 shipped this
worker-scoped and left the question open; the ruling closed it — the watchdog's sl-debugger is
unattended too, so both hazards apply to it as well. Each id the loop's launchers can produce gets
its OWN AskUserQuestion fallback (_ROLES):

  * `i<N>` WORKER   -> write state/blocked/<id> in QUESTION/OPTIONS/RECOMMENDATION form, COMMIT AND
                       PUSH the WIP, END the session. The runner acts on that file — and only for
                       `i<N>` — by posting the question durably and closing the window; a FRESH
                       session resumes the issue later with the owner's answer in its brief (#163),
                       reusing the PRESERVED worktree. No session is nudged awake to answer it, and
                       none is answered in place.
  * `d<N>` DEBUGGER -> the memo under <state home>/reports/ plus the notify that EVERY unattended
                       sl-debugger run ends with (plugin/skills/sl-debugger/references/
                       unattended-contract.md). Never the worker's blocked file: nothing reads one
                       for a `d<N>`, so that fallback would be a dead drop.

The ruling named a third seat — the answerer `a<N>` — conditionally: "while any remain pre-#194".
None remain. #194 merged on 2026-07-21 and retired the answerer scaffolding outright, narrowing
the launcher's `--cwd` mode to `^d[0-9]+$`, so NO launcher can produce an `a<N>` any more.
Carrying a role for a session that cannot exist would re-add the very scaffolding #194 removed, so
it is out: the ruling's condition, not the ruling, is what lapsed.

An id of any other shape is a session whose protocol we do not know, so we hand it nothing and deny
nothing — the same fail-open posture as everything else here.

ATTENDANCE — NOT ROLE — IS THE ONE CARVE-OUT. `superlooper debug` (issue #144) launches a `d<N>`
session through the SAME shim as the watchdog, but with a person at the keyboard; its brief says so
and invites them to ask. That launch sets SL_ATTENDED=1 and duty 1 stands down for it: the deny's
whole premise ("no human is at this pane") would be a falsehood, and a falsehood that pushes the
session into the unattended contract costs more than the dialog. Duty 2 is NOT carved out — a
pattern can match the owner's live processes whether or not anyone is watching, and no brief ever
promises pattern-kills (the sl-debugger contract forbids them at every authority tier, `full`
included). The flag is honored for `d<N>` ALONE, because the owner tap is the only attended launch
that exists: a worker's env descends from the runner's shell, and an ambient `export SL_ATTENDED=1`
there must never quietly disarm the deny i280 paid for. (Belt AND suspenders: runner._script_env
and _debugger_shim_run both PIN the flag empty on every unattended launch, so the leak is closed at
the launcher too — neither half relies on the other.)

Two accepted limits of that flag, stated so they stay conscious choices (same posture as the kill
matcher's documented misses — this is a safety net for ACCIDENTAL drift, not an adversarial jail):
a `d<N>` session's cwd IS the repo, so an in-repo `.claude/settings.json` env block could assert
attendance for the watchdog's debugger (nothing in this repo does, and writing one is a deliberate
act, not a slip); and `superlooper debug` asserts attendance for every --source, the command
center's button included, which is the same claim debugger-brief-owner.md already makes to the
session in words — the person who clicked is at the machine, and nothing re-arms the duty if they
then walk away.

CLAUDE ONLY. Codex has no PreToolUse event (spike verdict); its backstop is the classifier's
at_dialog/logged_out states. run() no-ops for SL_AGENT=codex so a global registration is inert there.

FAIL OPEN, ALWAYS. This fires before EVERY tool call. A broken duty must degrade to "allow" (today's
behavior — the brief still instructs against them), never to blocking every tool and wedging the
session. The hook speaks to Claude ONLY by printing deny JSON on stdout; printing nothing lets the
call proceed untouched.

Duty 3 sharpens that to FAIL OPEN PER DIMENSION, because it is the first duty that must READ an
argument rather than recognize a shape. Each dimension answers only from evidence it actually has:
a body built by command substitution stands the BODY dimension down while the labels are still
judged; a `--repo` pointing who-knows-where stands the whole duty down (another repo has another
contract); a command line shlex cannot split is never judged at all. A deny of a legitimate
`gh issue create` the hook merely could not READ would cost more than the defect it prevents.
"""
import json
import os
import re
import shlex
import sys

try:
    import queue_lint
except Exception:          # a half-published engine must not take duties 1 and 2 down with it
    queue_lint = None


# --------------------------- duty 1: AskUserQuestion ---------------------------

def _blocked_path(state_home, issue_id):
    """The blocked-question file, the durable fallback the deny points at — the SAME path the
    brief's "Blocked?" clause names (state/blocked/<id> under the run root)."""
    return os.path.join(state_home, "state", "blocked", issue_id)


# The shared opening: WHY the dialog cannot stand. Identical for every role — the fact that nobody
# is at the pane is the same fact — so only the fallback below differs.
_ASK_PREFIX = ("AskUserQuestion is forbidden in an unattended superlooper %s session: no human is "
               "at this pane to answer it, so the dialog would stall this session until someone "
               "clears it by hand (incident i280). Do NOT ask interactively. ")


def _worker_ask_reason(state_home, issue_id):
    # HELD TO THE LIVE CONTRACT (issue #230). This text reaches the model VERBATIM at the instant it
    # errs, so a stale sentence here is not a doc bug — the worker ACTS on it. Three drifts the
    # 2026-07-16 first-prompt audit found in the original #156 wording, each fixed here:
    #   * it promised "a fresh answerer replies into this session" — nobody does. #163 replaced that
    #     shape (the runner posts the question durably and CLOSES the window; a FRESH session resumes
    #     with the answer in its brief) and #194 retired the answerer seat outright. A worker holding
    #     the old model waits in a closing window for a reply that never comes — the i280 stall the
    #     deny exists to prevent, re-entered through the deny's own words.
    #   * it said "write ... and end your turn", omitting the COMMIT AND PUSH the footer requires, so
    #     obeying it verbatim ended the session with this checkout holding the only copy of the work
    #     (the i153/i163 loss #190 fences). State the push's reason CAREFULLY: the resume does NOT
    #     depend on it — _exec_post_question tears down with remove_worktree=False and
    #     the launcher only creates a worktree when none exists, so the relaunch reuses the
    #     PRESERVED WIP, uncommitted files included. Telling a worker its unpushed work is
    #     unrecoverable would be a NEW falsehood in the same class this issue removes, and a costly
    #     one: a worker whose push fails would then refuse to end the session (the i280 stall) or
    #     reach for something the bright lines forbid. The push is a SECOND copy, which is what the
    #     runner's own docstring claims ("no live window is the only copy") and what #190's guard
    #     reads to decide a checkout is safe to reclaim. Name what the push buys with the same care:
    #     it is a copy OFF THIS MACHINE, not off this checkout. A worktree shares the repo's object
    #     database, so once the work is COMMITTED the branch ref already carries it (gitops: "The
    #     branch itself is untouched ... only the checkout dies") and the launcher's
    #     `worktree add "$WT" "$BRANCH"` fallback re-attaches it. Saying the push is the last chance
    #     to escape "this one checkout" would be the same over-claim one step smaller.
    #   * its assumption hint named the PR body ALONE, which an `investigate` worker cannot use —
    #     it opens zero PRs, and brief.py (_ASSUME_INVESTIGATE) special-cases the hint for exactly
    #     that. The hook cannot see the issue type (no launcher exports one, and inventing that
    #     plumbing is a behavior change this text fix does not need), so the hint is type-NEUTRAL:
    #     it offers both deliverables and lets the worker pick the one its brief gave it.
    return _ASK_PREFIX % "worker" + (
        "Use the durable protocol instead: write your single, specific question to %s as three "
        "parts — `QUESTION:` the one decision, `OPTIONS:` the choices you see, `RECOMMENDATION:` "
        "the one you would take if forced — then COMMIT AND PUSH your work-in-progress on this "
        "issue's branch (`git push -u origin HEAD`) and END the session. The runner then posts your "
        "question as a durable comment on the issue, closes this window and releases the lane, and "
        "a FRESH session resumes the issue with the owner's answer in its brief, reusing this "
        "worktree's work-in-progress. Push before you end anyway: the runner is what closes this "
        "window, so the push is your last chance to put the work anywhere but this machine. "
        "You get at most TWO questions on one issue — a third hands the issue to the "
        "owner as a scoping problem instead of resuming you — so if you can safely proceed on one "
        "reasonable assumption, prefer stating it in your deliverable (the PR body, or your "
        "root-cause report if this issue opens no PR) over blocking."
        % _blocked_path(state_home, issue_id))


def _debugger_ask_reason(state_home, issue_id):
    # The unattended sl-debugger has no blocked file to write; its contract ends EVERY run the same
    # way — memo + notify — so an unanswerable question is a finding, not a dialog.
    return _ASK_PREFIX % "sl-debugger" + (
        "The watchdog launched you, not a person, so behave as unattended (the stricter mode is "
        "always safe): decide from the state home's own truth within your authority tier, and turn "
        "anything you cannot decide into a named finding in the memo you write under %s — plus the "
        "notify — then end the session. The memo is the owner's whole picture of tonight."
        % os.path.join(state_home, "reports"))


# The ONE place session id -> role -> fallback is decided. `i<N>` and `d<N>` are exactly the shapes
# lib/launch.py's own mode guards enforce (`^i[0-9]+$` for a worker, `^d[0-9]+$` for --cwd since
# #194), so this cannot recognize a session the loop cannot launch — and it stays in step with the
# launcher: a seat retired there falls out of here, which is why `a<N>` is gone. ASCII digits only,
# deliberately: `str.isdigit()` would also accept unicode digits that no launcher can produce.
_ROLES = {"i": ("worker", _worker_ask_reason),
          "d": ("debugger", _debugger_ask_reason)}
_ID_RE = re.compile(r"^([id])([0-9]+)$")


def _role(issue_id):
    """('worker'|'debugger', reason_fn) for a loop session id, else None. None means a session whose
    escalation protocol we do not know — we deny it nothing (fail open)."""
    m = _ID_RE.match(issue_id) if isinstance(issue_id, str) else None
    return _ROLES[m.group(1)] if m else None


# --------------------------- duty 2: pattern-kills ---------------------------

_KILL_REASON = (
    "Killing processes by name or pattern (pkill / killall) is forbidden in a superlooper loop "
    "session: the pattern can also match the OWNER's own live processes — a worker once killed the "
    "owner's live dashboard this way. Record the PID of anything you background ($!) and kill only "
    "that exact PID (`kill <pid>` / `kill -9 <pid>`). If this was a SEARCH over text and not a kill "
    "at all — a grep over docs or logs — you hit the documented false positive: a shell separator "
    "inside your quoted pattern (`|`, `;`, `&`, `(`, a backtick, a newline) reads as a command "
    "position. Rephrase the SEARCH (`grep -e pkill -e killall …`) and it goes through."
)

# `pkill` / `killall` invoked as a COMMAND, not as an incidental substring. It matches only at a
# command position — the string start, or right after a shell separator/subshell opener
# (;  &  |  newline  (  `  {) — so `grep pkill log`, `echo "pkill"`, and a filename like
# notes-about-killall.txt are NOT denied, while `a && pkill x`, `x; killall y`, `$(pkill z)` and a
# newline-joined command ARE. A leading benign wrapper (sudo/env/xargs/…) and an absolute/relative
# path prefix (/usr/bin/pkill, ./pkill) are seen through. Trailing (?![\w.-]) keeps it a whole word
# so `pkilld`/`pkill.sh` don't trip it.
#
# DELIBERATELY NARROW (issue Boundaries: deny only the named patterns, no broad allowlist). It is a
# safety net for ACCIDENTAL drift, not an adversarial jail, and the brief still instructs against
# both hazards — so it accepts, by design, both a rare false DENY (a `; pkill` sitting inside a
# quoted commit message or a heredoc body reads as a command position — it errs toward denying,
# which merely costs the worker a rephrase, never a killed owner process) and a rare MISS of an
# unusual invocation form: `sh -c 'pkill x'` / `bash -c "…"` / `eval "…"` (the name sits behind a
# quote, past any anchor), `xargs -r pkill` (a flag breaks the xargs wrapper), and `if pkill …; then`
# (condition position). test_pretooluse_hook.py pins these accepted edges so the behavior is visible
# and any future tightening is a conscious change, not an accident.
_KILL_RE = re.compile(
    r"""
    (?:^|[\n;&|(`{])                                              # command position
    \s*
    (?:(?:sudo|env|nohup|time|command|builtin|exec|xargs|then|do|else)\s+)*   # benign leading words
    (?:[\w./-]*/)?                                                # optional path prefix
    (?:pkill|killall)
    (?![\w.-])                                                    # whole word
    """,
    re.VERBOSE,
)


def _is_pattern_kill(command):
    return isinstance(command, str) and bool(_KILL_RE.search(command))


# --------------------------- duty 3: `gh issue create` (issue #225) ---------------------------

# Shell separators shlex.split leaves as their own tokens. A `gh` right after one of these is at a
# COMMAND position (`cd x && gh issue create …`), which is how the ordinary compound line is read
# without also reading `echo gh issue create` as a call.
_SEPARATORS = frozenset(("&&", "||", ";", "|", "&", "(", ")", "{", "}"))

# Anything that puts the body somewhere this parser cannot see. Each stands the BODY dimension down
# (the labels are still judged) rather than the whole duty: they are honest "we don't know", not
# evidence of a defect.
_BODY_ELSEWHERE = frozenset(("--body-file", "-F", "--editor", "--template", "--fill",
                             "--fill-first", "--fill-verbose"))
# ...and anything that puts the LABELS somewhere this parser cannot see either. These stand the
# WHOLE duty down, like `--repo` (fresh-agent review, 2026-07-30): `--web` hands the whole form to a
# browser for the person to fill in, and `--recover` restores a saved draft's fields — including its
# labels — from a file. In both, an empty `--label` set is UNREAD labels, not the confident evidence
# of a missing `type:` label that an ordinary command line's absence would be. Denying there is a
# verdict on evidence we do not have, which is the one direction this duty may never be wrong in.
_DECLARATION_ELSEWHERE = frozenset(("--web", "--recover"))
# Shell constructs that mean the quoted text is NOT what the command will actually send: a command
# substitution, a process substitution, or a plain variable expansion. shlex keeps every one of
# them literal, so seeing one is exactly the signal that what we hold is a RECIPE rather than the
# value. A bare `$var` counts (fresh-agent review, 2026-07-28): `--body "$body"` is an ordinary
# shape, and judging the seven characters `$body` AS the issue body denies a perfectly good command
# over content this hook never read.
#
# `$` and a backtick are matched anywhere in the value, deliberately coarser than a real shell
# parser. A body that legitimately contains one ("costs 5$ per run") therefore also stands its check
# down — a false ALLOW, which is the only direction this duty is permitted to be wrong in.
_UNEXPANDED = ("$", "`")


def _unexpanded(value):
    """Is this argument a recipe for a value rather than the value? See _UNEXPANDED."""
    return isinstance(value, str) and any(m in value for m in _UNEXPANDED)


def _split_command(command):
    """The command's tokens, or None when it cannot be read (unbalanced quotes, a heredoc, a
    non-string). Never judged is always safer than judged wrong."""
    if not isinstance(command, str) or "gh" not in command:
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _issue_create_span(tokens):
    """(start, end) of the ONE `gh issue create` argument run in these tokens, else None.

    `gh` is recognized at a command position only — the line start or right after a separator — so
    `echo gh issue create` and a quoted mention inside a commit message are not calls. An absolute
    or relative path to the binary (`/opt/homebrew/bin/gh`) is seen through. TWO calls on one line
    stand the duty down: which one a defect belongs to stops being answerable, and the compound is
    rare enough that a rephrase costs nothing."""
    spans = []
    for i, tok in enumerate(tokens):
        if not (tok == "gh" or tok.endswith("/gh")):
            continue
        if i and tokens[i - 1] not in _SEPARATORS:
            continue
        if tokens[i + 1:i + 3] != ["issue", "create"]:
            continue
        end = len(tokens)
        for j in range(i + 3, len(tokens)):
            if tokens[j] in _SEPARATORS:
                end = j
                break
        spans.append((i + 3, end))
    return spans[0] if len(spans) == 1 else None


def _flag_value(tokens, idx, name):
    """The value of a `--flag value` / `--flag=value` / `-f value` / `-fvalue` argument at `idx`,
    as (value, next_index), or (None, idx+1) when this token is not that flag.

    The ATTACHED short form is not a nicety: `gh` is cobra/pflag-based, so `-ltype:build` and
    `-b'…'` are as valid as the separated form and workers write them (fresh-agent review round 2,
    2026-07-28). Missing it read `-ltype:build` as an unrecognized token — the labels came back
    EMPTY and a valid command was denied for a `type:` label that was right there."""
    tok = tokens[idx]
    if tok == name:
        return (tokens[idx + 1], idx + 2) if idx + 1 < len(tokens) else (None, idx + 1)
    if name.startswith("--") and tok.startswith(name + "="):
        return tok[len(name) + 1:], idx + 1
    if not name.startswith("--") and len(tok) > len(name) and tok.startswith(name):
        return tok[len(name):], idx + 1            # -lvalue
    return None, idx + 1


def parse_issue_create(command):
    """What a `gh issue create` command line declares, or None when it is not confidently one.

    Returns {"labels": [...] or None, "body": str or None}, where None on a FIELD means that
    dimension could not be read and must not be judged. `labels: []` is different from
    `labels: None`: the line parsed and simply carries no `--label`, which is confident evidence of
    a missing `type:` label (two live issues in this repo, #284 and #286, were filed exactly so).

    `--repo`/`-R` stands the WHOLE duty down: another repo has another contract (its own `areas`,
    its own `touches_required`), and this repo's rules are not held over an issue filed there.
    `--web` and `--recover` stand it down for the other reason: the labels and body are chosen in a
    browser form or restored from a draft file, so this parser reads neither."""
    # (an unreadable LABEL argument also stands the whole duty down — see _issue_create_deny)
    tokens = _split_command(command)
    if not tokens:
        return None
    span = _issue_create_span(tokens)
    if span is None:
        return None
    start, end = span
    labels, body, body_readable, labels_readable = [], None, True, True
    i = start
    while i < end:
        tok = tokens[i]
        # `--repo x`, `--repo=x`, `-R x` AND the attached `-Rowner/repo` — missing the last one
        # held THIS repo's contract over an issue being filed somewhere else (review round 2).
        if tok in ("--repo", "-R") or tok.startswith("--repo=") or tok.startswith("-R"):
            return None
        # `-w` is gh's own documented shorthand for `--web`, and both flags also take the attached
        # `--flag=value` form. Whole-token matching alone missed all three, which is the same
        # attached-form gap `-Rowner/repo` was caught on (review rounds 2 and 3).
        if (tok in _DECLARATION_ELSEWHERE or tok == "-w"
                or tok.startswith("--web=") or tok.startswith("--recover=")):
            return None                      # labels AND body live where this parser cannot look
        if tok in _BODY_ELSEWHERE:
            body_readable = False
            i += 1
            continue
        for name in ("--label", "-l"):
            val, nxt = _flag_value(tokens, i, name)
            if val is not None:
                if _unexpanded(val):
                    labels_readable = False  # `--label "$labels"` — a recipe, not the label set
                elif labels is not None:
                    labels += [p.strip() for p in val.split(",") if p.strip()]
                i = nxt
                break
        else:
            for name in ("--body", "-b"):
                val, nxt = _flag_value(tokens, i, name)
                if val is not None:
                    body = val
                    i = nxt
                    break
            else:
                i += 1
    if _unexpanded(body):
        body, body_readable = None, False    # a recipe for the text, not the text
    return {"labels": labels if labels_readable else None,
            # No `--body` at all is not a missing body — gh then prompts or errors, so there is no
            # text to judge either way. Only a body we READ may be judged.
            "body": body if (body_readable and body is not None) else None}


def _read_config(path):
    """A repo's `.superlooper/config.json` as a dict, or None. Every failure — absent, a directory,
    unreadable, unparseable, wrong-typed — is the same answer: we do not know this repo's contract."""
    try:
        with open(path) as f:
            body = json.load(f)
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _walk_up_for_config(cwd):
    """The nearest `.superlooper/config.json` at or above `cwd`, or None."""
    if not isinstance(cwd, str) or not cwd:
        return None
    here = os.path.abspath(cwd)
    while True:
        cfg = _read_config(os.path.join(here, ".superlooper", "config.json"))
        if cfg is not None:
            return cfg
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def repo_contract(state_home, issue_id, cwd=None):
    """This repo's half of the mechanical contract: {"areas", "touches_required"}.

    Two routes to the same file, because the two session shapes sit in different places. A worker's
    cwd IS its worktree — `<state home>/worktrees/<id>` is exactly what the launcher creates —
    while the `d<N>` debugger runs `--cwd` against a checkout with no worktree at all. The payload's
    own cwd is tried first (walked UP, so a session working inside a subdirectory still finds it)
    and the derived worktree path second, so neither shape depends on the other's luck.

    With NO config in reach the touches dimension STANDS DOWN entirely (`touches_required: False`)
    rather than defaulting to the engine's enforce-on-garbage posture. That default is right for the
    runner, which knows it is looking at its own adopted repo; here, not finding a config means we
    do not know that we are — and a confident demand on evidence we do not have is the one thing
    this duty must never make. The `type:` vocabulary is superlooper's own and repo-independent, so
    it is still judged."""
    cfg = _walk_up_for_config(cwd)
    if cfg is None and isinstance(state_home, str) and isinstance(issue_id, str):
        cfg = _read_config(os.path.join(state_home, "worktrees", issue_id,
                                        ".superlooper", "config.json"))
    if cfg is None:
        return {"areas": None, "touches_required": False}
    tr = cfg.get("touches_required")
    return {"areas": cfg.get("areas"),
            "touches_required": tr if isinstance(tr, bool) else True}


_CREATE_PREAMBLE = (
    "`gh issue create` refused: this issue would be MECHANICALLY INVALID, so superlooper could "
    "never launch it — it would sit in the queue looking like work that is waiting its turn. (The "
    "2026-07-16 audit found 25 of 35 open issues in exactly that state.) Nothing was created. Fix "
    "these and re-run the SAME command:\n")

_CREATE_SHAPE = (
    "\nThe body needs a section in EXACTLY this shape (it is what anti-affinity and the gate's "
    "wander check verify against):\n\n%s\n" % queue_lint.METADATA_SHAPE if queue_lint else "")


def _issue_create_reason(defects, areas):
    """The deny text: every defect at once, each with its own fix, then — only when a metadata
    defect is among them — the required shape and this repo's real area names.

    Every defect at once, not one per retry: a session bounced three times for three halves of one
    format has been taught nothing. And the shape block is CONDITIONAL for the same reason the body
    dimension can stand down: a deny over labels alone must not also lecture about a body this hook
    never read."""
    lines = [_CREATE_PREAMBLE]
    for d in defects:
        lines.append("  • " + queue_lint.describe(d))
    if any(d["code"].startswith("touches") for d in defects):
        lines.append(_CREATE_SHAPE)
        # `a.strip()`, like queue_lint._area_names: a blank key would otherwise render as
        # "areas: ,    , engine" in the one sentence that tells the worker what to write.
        names = [a for a in areas if isinstance(a, str) and a.strip()] \
            if isinstance(areas, dict) else []
        if names:
            lines.append("This repo declares these areas: %s (or `*` for genuinely unknown scope)."
                         % ", ".join(names))
    return "\n".join(lines)


def _issue_create_deny(tool_input, contract):
    """The duty-3 verdict for one Bash call: a deny reason, or None to allow."""
    if queue_lint is None:
        return None                      # no contract module in reach — allow, as always
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    parsed = parse_issue_create(command)
    if parsed is None:
        return None                      # not confidently a `gh issue create`
    if parsed["labels"] is None:
        # An unreadable label ARGUMENT (`--label "$labels"`) stands the WHOLE duty down, not just
        # the label check (fresh-agent review, 2026-07-28). Without the labels we cannot tell an
        # investigation — which needs no `touches:` at all — from a build, so demanding a
        # declaration would be a guess dressed as a rule.
        return None
    got = contract() if callable(contract) else {"areas": None, "touches_required": False}
    areas = got.get("areas")
    body = parsed["body"]
    defects = queue_lint.lint(parsed["labels"], body if body is not None else "",
                              areas=areas,
                              # An unreadable body must never be reported as a missing section.
                              touches_required=bool(got.get("touches_required")) and body is not None)
    return _issue_create_reason(defects, areas) if defects else None


# --------------------------- attendance ---------------------------

# The same words start-session.sh's truthy() accepts, so one boolean is read one way across the
# launch stack. Anything else — including the empty string every unattended launch pins — is False.
_TRUE = {"1", "true", "yes", "on"}


def _attended(env, role):
    """True only for the OWNER TAP: a `d<N>` session that `superlooper debug` marked SL_ATTENDED=1,
    meaning a person is at this pane right now. Honored for the debugger role ALONE — a worker is
    never attended, and reading the flag for one would let an ambient export in the runner's shell
    disarm duty 1 (see the module docstring)."""
    return role == "debugger" and (env.get("SL_ATTENDED") or "").strip().lower() in _TRUE


# --------------------------- the decision ---------------------------

def decide(tool_name, tool_input, state_home, issue_id, ask_reason, attended=False,
           contract=None):
    """Return a deny-reason string, or None to let the call proceed. Deny ONLY the three named
    hazards — no broad allowlist. `ask_reason` is the caller's role-specific fallback text builder;
    `attended` stands duty 1 down when a human is genuinely present.

    `ask_reason` is REQUIRED, deliberately (fresh-agent review): a default would mean a caller that
    forgets it silently hands some other role the WORKER's protocol — the exact drift this module
    says is worse than no deny at all. Better a TypeError, which main() turns into a fail-open
    allow, than a confident wrong instruction.

    `contract` is a ZERO-ARG CALLABLE returning this repo's {"areas", "touches_required"} — a
    thunk, not a value, because resolving it reads a file and this runs before EVERY tool call. It
    is invoked only once a command has already parsed as a `gh issue create`, which is rare. Its
    default is the fail-open contract (nothing known), so a caller that omits it can only ever deny
    LESS, never more."""
    if tool_name == "AskUserQuestion":
        if attended:
            return None                  # a person IS at this pane — the dialog will be answered
        return ask_reason(state_home, issue_id)
    if tool_name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if _is_pattern_kill(command):
            return _KILL_REASON          # never carved out, attended or not
        return _issue_create_deny(tool_input, contract)
    return None


def run(payload, env):
    """Decide the PreToolUse outcome for one loop session. Returns the deny-reason string, or None
    to ALLOW. No-ops (returns None) outside the session ids the loop's own launchers produce —
    `i<N>`/`d<N>`, so an ad-hoc or owner's-own session is untouched — and for Codex, and for
    any non-PreToolUse payload, so the hook is safe to register globally."""
    if (env.get("SL_AGENT") or "claude").strip() == "codex":
        return None                      # Codex has no PreToolUse event; Claude-only (spike verdict)
    issue_id = (env.get("SL_ISSUE_ID") or "").strip()
    state_home = (env.get("SL_RUN_ROOT") or "").strip()
    role = _role(issue_id)
    if not state_home or role is None:
        return None                      # not a loop session (ad-hoc / anything else)
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    return decide(payload.get("tool_name"), payload.get("tool_input"), state_home, issue_id,
                  ask_reason=role[1], attended=_attended(env, role[0]),
                  contract=lambda: repo_contract(state_home, issue_id, payload.get("cwd")))


# --------------------------- the turn ---------------------------

def _deny(reason):
    """The EXACT JSON Claude Code requires to block a tool call, reason delivered to the model
    verbatim. Blocks even under --dangerously-skip-permissions (PreToolUse fires before the
    permission-mode check), so a bypass-mode worker cannot escape the denial (spike verdict)."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                         # unreadable input fails OPEN (allow) and SILENT
    try:
        reason = run(payload, os.environ)
    except Exception:
        return 0                         # a broken deny must never block every tool — fail OPEN
    if not reason:
        return 0                         # nothing printed -> the call proceeds untouched
    try:
        sys.stdout.write(json.dumps(_deny(reason)))
        sys.stdout.flush()               # a buffered decision that never lands is not a denial
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
