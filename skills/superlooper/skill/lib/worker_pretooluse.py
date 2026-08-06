"""The Claude PreToolUse hook's core (issues #156 + #185 + #225 + #226): the costliest
session-instruction-drift incidents, and the three paths to a bad merge, made mechanically
impossible rather than instructed-against.

pretooluse-hook.sh fences the session (loop-only, Claude-only, cwd-safe) and hands the hook
payload here. This decides ONE thing: does this tool call cross one of the named hard lines, and if
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

  4. A FORGED COMMIT STATUS, 5. AN APPROVAL LABEL (`agent-ready` / `pre-authorized:*`), and
     6. OUT-OF-BAND SHIPPING (`gh pr merge`, a direct push to the dev branch, any force-push) —
     issue #226, the three paths to a bad merge. Their full rationale, their accepted misses and the
     one posture difference from duty 3 sit above the matchers themselves, below. Two facts belong
     up here because they shape the whole module:

       * WHY THIS IS THE ONLY LAYER. The owner ruled on 2026-08-05 (Q11) that there will be NO bot
         account: every loop action runs on his own identity, so no vendor-side token scoping is
         coming, and because every session posts as the same login no after-the-fact provenance
         check is even possible. The moment of the tool call is the only place identity exists.
       * WHAT IS OUT OF SURFACE, measured on #232 (2026-08-05). The forgery surface writable by an
         ACCOUNT credential is exactly one endpoint: POST /repos/{o}/{r}/statuses/{sha}. Check-run
         creation is App-only and the status-creating GraphQL mutations are unreachable the same
         way, so deny branches for those would test an impossible call. They are recorded, not
         coded — and if GitHub ever opens either, this note is where the gap is already named.

DENY ONLY THE NAMED HAZARDS. Everything else is allowed — no broad allowlist that could break
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
session into the unattended contract costs more than the dialog. NO OTHER DUTY is carved out — a
pattern can match the owner's live processes whether or not anyone is watching, an unlaunchable
issue is unlaunchable either way, and a person at the pane makes a forged status or a self-merge no
less of a bad merge. No brief ever promises any of them (the sl-debugger contract forbids
pattern-kills, merging and force-pushing at every authority tier, `full` included). The flag is honored for `d<N>` ALONE, because the owner tap is the only attended launch
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
This is an honest limit, not a footnote: a seat with no PreToolUse layer is simply not reached, and
one such seat exists TODAY inside a worker's own session — `/superlooper:cross-review` runs a
non-Claude agent as a subprocess in the worker's pane, inheriting the worker's credential with no
hook of its own (#232's finding). Nothing here can cover it; the brief's own words are what does.

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


# ================= duties 4-6: the three bad-merge paths (issue #226) =================
#
# Three routes to a bad merge were guarded by prose alone. The owner ruled on 2026-08-05 (Q11) that
# there will be NO bot account: every loop action stays on his own identity, so no vendor-side token
# scoping is coming, and because every session posts as the same login no after-the-fact provenance
# check is even possible. The moment of the tool call is the only place identity exists — which makes
# these denies THE protection for all three paths, not one layer of several.
#
#   4. A FORGED COMMIT STATUS. gate.py's required-checks rollup folds gh's two shapes — CheckRun
#      (name/conclusion) and StatusContext (context/state) — into one verdict, and GitHub's own
#      branch protection accepts a hand-posted context just as readily, so a failing diff can merge
#      green.
#   5. AN APPROVAL LABEL. `agent-ready` IS the launch gate (actions.py) and `pre-authorized:*` is
#      what the merge gate consumes (#165), so a session that can apply either self-approves work the
#      owner never saw. Workers are INSTRUCTED to file child issues (brief.py), so they hold the very
#      tool that does it.
#   6. OUT-OF-BAND SHIPPING. `gh pr merge`, a direct push to the dev branch, and any force-push.
#      Precedent i328 (2026-07-15): a worker merged its own PR, defeating all three completion
#      signals at once and stalling the queue two hours. #155's per-tick branch->PR reconcile is
#      post-hoc — it settles the fact, it prevents nothing.
#
# OUT OF SURFACE, DELIBERATELY UNGUARDED (measured on #232, 2026-08-05). The forgery surface
# writable by an ACCOUNT credential is exactly one endpoint: POST /repos/{o}/{r}/statuses/{sha}.
# Check runs cannot be created by any account credential at all (the Checks API is App-only), and the
# status-creating GraphQL mutations are unreachable the same way. Shipping deny branches for those
# would be testing an impossible call, so they are recorded here instead of coded.
#
# ACCEPTED MISSES, stated so they stay conscious choices (the same posture as the pattern-kill
# matcher's, and pinned in test_pretooluse_hook.py):
#   * a call behind a quote — `sh -c '…'`, `bash -c "…"`, `eval '…'` — where the name never sits at
#     a command position this parser can see;
#   * `git push --all` (it is not a force, and every ref it would move still has to fast-forward);
#   * a hand-rolled `curl` to the statuses endpoint (it needs a token the worker has no handy route
#     to; `gh` is what a session actually reaches for);
#   * `git push` with no refspec while the checkout happens to sit ON the dev branch — the command
#     line does not say which branch that is, and reading the checkout is a new I/O this hook does
#     not do;
#   * exotic refspecs (`HEAD:refs/for/main`) and a remote spelled as a URL;
#   * a seat with no PreToolUse layer at all — today that is `/superlooper:cross-review`'s non-Claude
#     subprocess running inside the worker's own pane on the worker's credential (#232). See the
#     module docstring's CLAUDE ONLY note.
# The brief still instructs against all three hazards — brief.py's `_SHIP_NO_CMD` now says so, and
# the brief-footer line about never hand-posting a commit status is the documented backstop for
# exactly these misses, not redundant teaching.
#
# ...and one ACCEPTED FALSE DENY, the same safe-direction trade duty 2 already makes: an unquoted
# newline reads as the separator it is, so a line of a HEREDOC BODY reads as a command position.
# `cat <<EOF` … `gh pr merge is the gate's job` … `EOF` is refused. Erring toward denying costs the
# session a rephrase; erring the other way costs a rewritten branch. Pinned in tests either way.
#
# ONE POSTURE DIFFERENCE FROM DUTY 3, on purpose. There, a deny was the DANGEROUS direction (refusing
# a legitimate `gh issue create`), so two calls on one line stand the duty down. Here a deny is the
# CHEAP direction — it costs the session a rephrase, never a lost merge — so EVERY invocation on the
# line is judged and any one match denies. What stays identical is the rule that we never judge what
# we could not read: an unreadable command line, an argument held in a shell variable, or a dev
# branch no config in reach declares each stands its own check down.

# `git`'s global options that consume the NEXT token, so `git -C /path push --force` and
# `git -c user.name=x push …` are still read as pushes rather than as some other subcommand.
_GIT_GLOBAL_WITH_VALUE = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                    "--exec-path", "--super-prefix"))

# Leading words that wrap a real command without changing what it is — duty 2's list, plus the
# CONDITION keywords (fresh-agent review round 2, 2026-08-06). Duty 2 documents `if pkill …; then`
# as an accepted miss because a deny there is the expensive direction; here it is the cheap one, so
# `if git push origin main; then …` and `until gh pr merge 42; do …` are closed rather than accepted.
_WRAPPERS = frozenset(("sudo", "env", "nohup", "time", "command", "builtin", "exec", "xargs",
                       "then", "do", "else", "if", "elif", "while", "until", "!"))

# `FOO=bar gh pr merge 42` — an env-assignment PREFIX is the ordinary way to run one command with
# one variable set, and it left the binary looking like an argument to something else (same review).
# Matched as a whole token so a `--body 'a=b'` value, which shlex hands over as one token including
# its spaces, cannot pass for one.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _newline_separated(command):
    """`command` with every newline OUTSIDE a quoted string replaced by an explicit `;`.

    shlex treats a newline as ordinary whitespace, so `cd /x\\ngit push --force` splits to
    [cd, /x, git, push, --force] and the `git` no longer follows a separator — the ORDINARY
    multi-line Bash call would walk straight past every command-position check below. A newline IS a
    separator in a shell, so we say so before splitting. Newlines INSIDE quotes are left alone: a PR
    body or a commit message is text, not structure, and turning its line breaks into separators is
    how `git commit -m 'a\\ngit push --force'` would become a false deny.

    A deliberately small scanner, not a shell: it tracks single and double quotes and a backslash
    escape, and nothing else. When it guesses wrong the result usually fails to shlex-split, which
    is the fail-open answer anyway."""
    out, quote, i, n = [], None, 0, len(command)
    while i < n:
        ch = command[i]
        if quote is None and ch == "\\":
            out.append(command[i:i + 2])          # an escaped char is never structure
            i += 2
            continue
        if quote == "'":
            quote = None if ch == "'" else quote
        elif quote == '"':
            if ch == "\\":
                out.append(command[i:i + 2])
                i += 2
                continue
            quote = None if ch == '"' else quote
        elif ch in "\"'":
            quote = ch
        out.append(" ; " if (ch == "\n" and quote is None) else ch)
        i += 1
    return "".join(out)


def _split_shell(command):
    """The command's tokens, with unquoted newlines as `;` separators and shell punctuation split
    off its neighbours, or None when the line cannot be read.

    `punctuation_chars=True` is the load-bearing half (fresh-agent review, 2026-08-06): plain
    `shlex.split` leaves UNSPACED punctuation glued to the word before it, so `cd /tmp; gh pr merge`
    tokenizes as [cd, /tmp;, gh, …] and the `gh` no longer follows a separator. The most ordinary
    compound lines a worker writes — a leading `cd x;` and a trailing `; echo ok` — walked straight
    past every command-position check. With it, `;`, `&&`, `||`, `|`, `&`, `(`, `)` and the redirect
    operators each become their own token, spaced or not.

    Kept separate from `_split_command` (duty 3's) so neither of these changes can move duty 3's
    verdicts: its fail-open direction makes its own misses harmless, and re-tokenizing it would be a
    behavior change this issue does not need."""
    if not isinstance(command, str) or ("gh" not in command and "git" not in command):
        return None
    try:
        lexer = shlex.shlex(_newline_separated(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


# A redirect ends an argument RUN without starting a new command: what follows `>` is a filename,
# not a refspec. (`_SEPARATORS`, which duty 3 shares, is left alone.)
_REDIRECTS = frozenset(("<", ">", ">>", "<<", "<<<", ">&", "<&", "&>", "&>>", ">|"))
_RUN_END = _SEPARATORS | _REDIRECTS

# `gh`'s OWN global options, which sit BEFORE the subcommand — so `gh --repo x pr merge 42` is still
# `gh pr merge`. Only these three take a value. NOTE the deliberate difference from duty 3, where
# `--repo` stands the whole duty DOWN: there the question is what CONTRACT an issue is filed under
# (another repo has its own areas and its own `touches_required`), and holding this repo's rules
# over someone else's issue would be a verdict on evidence we do not have. Here the question is
# this SESSION's own conduct — a loop worker forges no status, approves no issue and merges no PR,
# in this repo or any other — so a `--repo` never buys permission.
_GH_GLOBAL_WITH_VALUE = frozenset(("--repo", "-R", "--hostname"))


def _at_command_position(tokens, i):
    """Is `tokens[i]` a command being RUN, rather than an argument to something else? True at the
    line start, right after a separator, or behind a chain of benign wrappers and env-assignment
    prefixes. The walk-back must REACH a separator or the line start, so `echo if git push` — where
    it lands on `echo` — is still text, not a call."""
    j = i - 1
    while j >= 0 and (tokens[j] in _WRAPPERS or _ASSIGNMENT_RE.match(tokens[j])):
        j -= 1
    return j < 0 or tokens[j] in _SEPARATORS


def _invocations(tokens, binary):
    """[(start, end)] — the ARGUMENT run of every `binary` invoked at a command position. `start` is
    the index of its first argument, `end` the index of the next separator or redirect (or the
    line's end). An absolute or relative path to the binary is seen through."""
    out = []
    for i, tok in enumerate(tokens):
        if not (tok == binary or tok.endswith("/" + binary)):
            continue
        if not _at_command_position(tokens, i):
            continue
        end = len(tokens)
        for j in range(i + 1, len(tokens)):
            if tokens[j] in _RUN_END:
                end = j
                break
        out.append((i + 1, end))
    return out


# `--help` prints text and calls nothing. Denying it is a deny on no evidence at all (review r2).
_HELP_FLAGS = frozenset(("--help", "-h"))
# Methods that cannot write. `gh` sends exactly the method it is told to, so an EXPLICIT one is
# evidence about this call, not a guess about it — which is why honoring it opens no bypass.
_READ_METHODS = frozenset(("GET", "HEAD"))


def _gh_verb_start(tokens, start, end):
    """The index of a `gh` call's SUBCOMMAND, stepping over gh's own global options. `gh --repo x pr
    merge` and `gh --hostname h api …` are the same calls as `gh pr merge` and `gh api …`."""
    i = start
    while i < end:
        tok = tokens[i]
        if tok in _GH_GLOBAL_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1                           # `--repo=x`, `-Rowner/repo`, valueless flags
            continue
        return i
    return end


def _is_help(tokens, start, end):
    """Is this `gh` call asking for HELP rather than doing anything? `gh pr merge --help` prints a
    page and merges nothing."""
    return any(t in _HELP_FLAGS for t in tokens[start:end])


# --------------------------- matcher A: a forged commit status ---------------------------

# The ONE endpoint an account credential can forge a required check through (#232). Anchored on
# `repos/<owner>/<repo>/statuses/<sha>` so the READ shapes — `commits/<sha>/status` and
# `commits/<sha>/statuses`, which is how a worker legitimately checks its own CI — cannot match: the
# write path ends in the sha, the read paths do not contain `statuses/` at all.
_STATUS_WRITE_RE = re.compile(r"(?:^|/)repos/[^/\s]+/[^/\s]+/statuses/[^/\s?#&]+")

_STATUS_REASON = (
    "Hand-posting a commit status is forbidden in a superlooper loop session. The merge gate's "
    "required-checks rollup folds BOTH of GitHub's shapes — check runs and commit statuses — into "
    "one verdict, and GitHub's own branch protection accepts a hand-posted context just as readily, "
    "so a status you write yourself can merge a failing diff green. CI goes green because the tests "
    "ran, not because anyone said so, and the gate is entitled to read a check as evidence. Nothing "
    "was posted. READING status is fine and is what you actually want here "
    "(`gh api repos/{owner}/{repo}/commits/<sha>/status`, or `gh pr checks`). If a required check is "
    "red, fix the diff; if you cannot, say so plainly in your report and let the gate park the issue "
    "for the owner. Writing a check is never a worker's job."
)


def _forged_status_deny(tokens):
    for start, end in _invocations(tokens, "gh"):
        verb = _gh_verb_start(tokens, start, end)
        if verb >= end or tokens[verb] != "api" or _is_help(tokens, start, end):
            continue
        if _api_method(tokens, verb, end) in _READ_METHODS:
            continue                         # an explicitly declared read writes nothing
        if any(_STATUS_WRITE_RE.search(t) for t in tokens[verb + 1:end]):
            return _STATUS_REASON
    return None


# --------------------------- matcher B: approval labels ---------------------------

# `agent-ready` is the launch gate; the `pre-authorized:` FAMILY (gate.PREAUTHORIZED_REFEREE_LABEL
# is today's only member) is what the merge gate consumes. Held as literals rather than imported:
# this module runs before EVERY tool call, and pulling gate.py in would trade that for a constant
# whose value has never moved. The prefix is matched, not the one label, so a future member is
# covered the day it is minted.
_APPROVAL_LABEL = "agent-ready"
_PREAUTH_PREFIX = "pre-authorized:"

# Long forms FIRST: `_flag_value`'s attached-short branch would read `--label` as `-l` with the
# value `-label`, so the order here is load-bearing (the same ordering duty 3 relies on).
_LABEL_ADD_FLAGS = ("--add-label", "--label", "-l")

# `repos/<o>/<r>/issues/<n>/labels[…]` — GitHub routes PR labels through the ISSUES endpoint too, so
# this one shape covers both. The DELETE form carries the label name in the path (`…/labels/<name>`)
# and is excluded twice over: the path token is never scanned for a label name, and an explicit
# DELETE method stands the check down outright.
_LABELS_ENDPOINT_RE = re.compile(r"(?:^|/)issues/[^/\s]+/labels(?:$|[/?#])")
_METHOD_FLAGS = ("--method", "-X")

_APPROVAL_REASON = (
    "Applying `agent-ready` or a `pre-authorized:*` label is forbidden in a superlooper loop "
    "session: approval and pre-authorization are the OWNER's verbs alone (a bright line of this "
    "system). `agent-ready` IS the launch gate — it is what puts an issue into a lane — and "
    "`pre-authorized:referee` is what the merge gate consumes, so applying either would approve work "
    "the owner never saw, using the owner's own identity. Nothing was labeled. File it "
    "`needs-owner` instead: that is the label that puts the issue in front of the owner, and they "
    "approve it in their own words. (Removing one of these labels is NOT denied — removal is a "
    "safety act, not an approval.)"
)


def _is_approval_label(value):
    """Does this `--label`-style argument name an approval label? A comma list is split, each part
    compared whole (so `agent-ready-followup` is an ordinary label) and the `pre-authorized:` family
    matched by prefix. An argument holding a shell variable is a recipe, not a value — see
    `_unexpanded` — so it is never judged."""
    if not isinstance(value, str) or _unexpanded(value):
        return False
    for part in value.split(","):
        p = part.strip().lower()
        if p == _APPROVAL_LABEL or p.startswith(_PREAUTH_PREFIX):
            return True
    return False


def _api_method(tokens, start, end):
    """The HTTP method a `gh api` call declares (`-X POST`, `--method=DELETE`, `-XDELETE`), upper
    cased, or None when it declares none.

    Walks one token at a time and tries BOTH spellings at each — not duty 3's advance-by-flag-width
    loop, which would consume the token on the first spelling and never reach the second (`-X` would
    have been unreachable behind `--method`)."""
    for i in range(start, end):
        for name in _METHOD_FLAGS:
            val, _ = _flag_value(tokens, i, name)
            if val is not None:
                return val.strip().upper()
    return None


def _approval_label_deny(tokens):
    for start, end in _invocations(tokens, "gh"):
        verb = _gh_verb_start(tokens, start, end)
        if _is_help(tokens, start, end):
            continue                         # a help page applies no label
        args = tokens[verb:end]
        if len(args) >= 2 and args[0] in ("issue", "pr") and args[1] in ("create", "edit"):
            i = verb + 2
            while i < end:
                for name in _LABEL_ADD_FLAGS:
                    val, nxt = _flag_value(tokens, i, name)
                    if val is not None:
                        if _is_approval_label(val):
                            return _APPROVAL_REASON
                        i = nxt
                        break
                else:
                    i += 1
        elif args and args[0] == "api":
            if _api_method(tokens, verb, end) == "DELETE":
                continue                     # a removal, whatever it names
            on_labels, names_one = False, False
            for tok in tokens[verb + 1:end]:
                if _LABELS_ENDPOINT_RE.search(tok):
                    on_labels = True         # the PATH — never scanned for a label name
                    continue
                # `-f labels[]=agent-ready` reaches shlex as one token; a bare value is one too.
                if _is_approval_label(tok.split("=", 1)[1] if "=" in tok else tok):
                    names_one = True
            if on_labels and names_one:
                return _APPROVAL_REASON
    return None


# --------------------------- matcher C: out-of-band shipping ---------------------------

# `--mirror` carries no `--force` and no refspec, yet it force-updates EVERY ref and deletes the
# ones this checkout no longer has — a force-push spelled as a mode (fresh-agent review, 2026-08-06).
_PUSH_FORCE_FLAGS = frozenset(("-f", "--force", "--force-with-lease", "--force-if-includes",
                               "--mirror"))
_PUSH_FORCE_PREFIXES = ("--force-with-lease=", "--force-if-includes=")
# `git push` options that consume the NEXT token, so their values are never read as refspecs.
_PUSH_VALUE_FLAGS = frozenset(("--repo", "-o", "--push-option", "--receive-pack", "--exec"))
# A dry run SHIPS NOTHING: it is a read-shaped call, and read-shaped calls pass. Denying one would
# take away the only safe way a worker has to check what a refspec would do.
_PUSH_DRY_RUN_FLAGS = frozenset(("--dry-run", "-n"))

_SANCTIONED_PATH = (
    " The sanctioned path is the whole point of the loop and it is short: push your work to THIS "
    "issue's branch (`git push -u origin HEAD`), open the PR (`gh pr create --fill`), post the "
    "pinned review verdict as a PR comment, write your report — then the GATE merges it once CI is "
    "green and the verdict pins your head commit. Ending the session with the PR open is finishing, "
    "not stopping short."
)

_MERGE_REASON = (
    "`gh pr merge` is forbidden in a superlooper loop session: the GATE merges, never the worker. A "
    "self-merged PR defeats all three completion signals at once — incident i328 (2026-07-15) "
    "stalled the queue two hours that way — and nothing else in the system re-checks the diff after "
    "you take that step. Nothing was merged." + _SANCTIONED_PATH
)

_FORCE_REASON = (
    "Force-pushing is forbidden in a superlooper loop session — `No force-push, ever` is a bright "
    "line, and it holds for your OWN `sl/*` branch too, not just the mainline. On a feature branch "
    "the cost is a stranded PR: the merge gate reads a review verdict PINNED to the commit that was "
    "reviewed, and a force-push rewrites that commit out of existence, so the PR sits on a stale "
    "review pin until someone unsticks it by hand. Nothing was pushed. If your branch needs to catch "
    "up with the mainline, MERGE it in (`git fetch origin && git merge origin/<dev branch>`) — "
    "history you have already pushed is never rewritten here." + _SANCTIONED_PATH
)

_DEV_PUSH_REASON = (
    "A direct `git push` to `%s` — this repo's dev mainline — is forbidden in a superlooper loop "
    "session: the GATE merges to the mainline, never a worker, and a direct push lands work that no "
    "PR and no required check ever judged. Nothing was pushed. Worker pushes go to THIS issue's "
    "branch only." + _SANCTIONED_PATH
)


def _push_argv(tokens, start, end):
    """The arguments of a `git push` whose argument run is [start, end), or None when this `git` call
    is some other subcommand. Global options are stepped over first, so `git -C /path push …` and
    `git -c user.name=x push …` are read for what they are."""
    i = start
    while i < end:
        tok = tokens[i]
        if tok in _GIT_GLOBAL_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    return tokens[i + 1:end] if i < end and tokens[i] == "push" else None


def _push_refspecs(argv):
    """The refspec arguments of a `git push`. The FIRST positional is the remote, so it is dropped —
    which is also why `git push` and `git push origin` yield nothing to judge (an accepted miss: the
    branch they would push is in the checkout, not on the command line)."""
    positional, i = [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        positional.append(arg)
        i += 1
    return positional[1:]


def _refspec_dest(refspec):
    """The branch a refspec WRITES: the right of the colon when there is one, the whole thing when
    there is not, with a force `+` and a `refs/heads/` prefix stripped. `HEAD:main`, `main`,
    `main:main`, `:main` (a delete) and `HEAD:refs/heads/main` all resolve to `main`."""
    spec = refspec[1:] if refspec.startswith("+") else refspec
    dest = spec.split(":", 1)[1] if ":" in spec else spec
    return dest[len("refs/heads/"):] if dest.startswith("refs/heads/") else dest


def _out_of_band_deny(tokens, contract):
    for start, end in _invocations(tokens, "gh"):
        verb = _gh_verb_start(tokens, start, end)
        if tokens[verb:verb + 2] == ["pr", "merge"] and not _is_help(tokens, start, end):
            return _MERGE_REASON
    for start, end in _invocations(tokens, "git"):
        argv = _push_argv(tokens, start, end)
        if argv is None:
            continue
        if any(a in _PUSH_DRY_RUN_FLAGS for a in argv):
            continue                         # ships nothing — a read, not a push
        if any(a in _PUSH_FORCE_FLAGS or a.startswith(_PUSH_FORCE_PREFIXES) for a in argv):
            return _FORCE_REASON
        refspecs = _push_refspecs(argv)
        if any(r.startswith("+") for r in refspecs):
            return _FORCE_REASON             # `+refspec` is the same force, spelled the other way
        if refspecs:
            # Read the config only once a push with a real refspec is in hand — this runs before
            # every tool call, and a dev branch nobody declared stands this check down (never a
            # guessed mainline: denying a push over a name we invented is the one wrong direction).
            dev = _contract(contract).get("dev_branch")
            if isinstance(dev, str) and dev and any(_refspec_dest(r) == dev for r in refspecs):
                return _DEV_PUSH_REASON % dev
    return None


def _bad_merge_deny(command, contract):
    """The duty-4/5/6 verdict for one Bash call: a deny reason, or None to allow."""
    tokens = _split_shell(command)
    if not tokens:
        return None
    return (_forged_status_deny(tokens)
            or _approval_label_deny(tokens)
            or _out_of_band_deny(tokens, contract))


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
    """This repo's half of the mechanical contract: {"areas", "touches_required", "dev_branch"}.

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
    it is still judged.

    `dev_branch` (issue #226) rides the SAME file and the same posture: no config in reach means no
    mainline we can name, and matcher C's dev-branch check stands down rather than guessing one. A
    config that simply omits the key does declare a mainline — config.py's own default is `main`, so
    that is what the runner would use and what we hold the session to."""
    cfg = _walk_up_for_config(cwd)
    if cfg is None and isinstance(state_home, str) and isinstance(issue_id, str):
        cfg = _read_config(os.path.join(state_home, "worktrees", issue_id,
                                        ".superlooper", "config.json"))
    if cfg is None:
        return {"areas": None, "touches_required": False, "dev_branch": None}
    tr = cfg.get("touches_required")
    dev = cfg.get("dev_branch")
    return {"areas": cfg.get("areas"),
            "touches_required": tr if isinstance(tr, bool) else True,
            "dev_branch": dev.strip() if isinstance(dev, str) and dev.strip() else "main"}


# Nothing known: no areas, no demand for `touches:`, no mainline. Every dimension reading this
# stands itself down, so a caller that omits the contract can only ever deny LESS, never more.
_NO_CONTRACT = {"areas": None, "touches_required": False, "dev_branch": None}


def _contract(contract):
    """Resolve the contract thunk, degrading to "nothing known" for a missing or broken one."""
    return contract() if callable(contract) else dict(_NO_CONTRACT)


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
    got = _contract(contract)
    areas = got.get("areas")
    body = parsed["body"]
    defects = queue_lint.lint(parsed["labels"], body if body is not None else "",
                              areas=areas,
                              # An unreadable body must never be reported as a missing section.
                              touches_required=bool(got.get("touches_required")) and body is not None,
                              # This is the ONE surface that asks for the opt-in notices (issue
                              # #400): an issue's provenance can only be recorded while it is being
                              # written, so a create-time deny is the only moment saying so helps.
                              advisories=True)
    # ...but a notice is never a REFUSAL. `refusals()` is what decides whether to deny, so an
    # otherwise-valid issue with no `source:` label is created rather than bounced — nothing may
    # ever block on that family (owner ruling 2026-08-06). When the command has ALREADY earned a
    # deny, the notice still rides along in the text: the session is about to retype the command,
    # so it is the cheapest possible moment to say the whole contract at once.
    return _issue_create_reason(defects, areas) if queue_lint.refusals(defects) else None


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
    """Return a deny-reason string, or None to let the call proceed. Deny ONLY the named
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
        # The bad-merge matchers run BEFORE the issue-create format check, deliberately:
        # `gh issue create --label agent-ready` trips both, and the session must be told it may not
        # self-approve — not handed a format lecture implying the command is fine once the body is.
        return _bad_merge_deny(command, contract) or _issue_create_deny(tool_input, contract)
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
