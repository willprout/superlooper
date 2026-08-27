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
    the launcher exits 1 for five different faults, so rc alone can never name one. The
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
        "the launch aborted before any pane could host a worker — no worker was ever started, so "
        "nothing about the session itself is at fault; the captured stderr names the step that "
        "failed"),
    2: ("shim_not_fired",
        "no worker started for this launch: either the session host would not give it a pane, or "
        "a pane was created and the launch shim never handed the agent verb to start-session.sh "
        "— is the shim installed? (bin/install-launch-shim.sh). The captured stderr says which"),
    3: ("base_missing",
        "the worktree base branch does not exist on origin, so every worktree creation fails "
        "before the agent starts — a repo/config fault (dev_branch), not a launch-delivery problem"),
    4: ("gh_auth_dead",
        "the positive gh-auth assert refused the flight: `gh` did not answer as the login this "
        "loop runs as from inside the SESSION's own environment, so the session could not have "
        "read its own issue or posted any evidence — GitHub auth for that environment is dead or "
        "belongs to another account, not a launch-delivery problem. Re-login: `gh auth login "
        "--hostname github.com` with the account that owns the loop repo, then re-approve"),
    5: ("gh_auth_dead_runner",
        "the RUNNER's own `gh` could not say who it is, so there was no identity to launch any "
        "session against and no pane was ever opened — a machine-level GitHub auth fault that no "
        "queued issue caused and none can fix. Every launch will fail until it is repaired: "
        "`gh auth login --hostname github.com` with the account that owns the loop repo"),
    6: ("env_poisoned",
        "the launch-floor env scrub could not clean this session's own environment: variables that "
        "silently change how a session runs survived into it, so the flight was refused before it "
        "started. Left alone they are invisible — ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL move the "
        "session off Max-subscription billing onto API billing with no error and no signal, and an "
        "inherited CLAUDE_CODE_* turns transcript saving off, which silently breaks `--resume`. "
        "The captured stderr names the exact variables: find where they are exported (a shell rc "
        "file, a LaunchAgent, a wrapper) and remove them, then re-approve"),
    7: ("claude_identity_wrong",
        "the positive Anthropic-account assert refused the flight (#314): from inside the SESSION's "
        "own environment, `claude auth status` did not report the account this launch assigned — "
        "not logged in, on an API key, or on a different org. The two subscriptions are separate "
        "rate-limit pools, so a session on the wrong one is a lane whose capacity nobody can "
        "predict; and an API-key session bills per token while still answering `loggedIn: true`. "
        "The captured stderr names which of those it was: repair it in a supervised `claude` "
        "window under that config dir, then re-approve"),
    8: ("claude_identity_wrong_runner",
        "the RUNNER's own environment could not produce the Anthropic account every session would "
        "be launched against, so no pane was ever opened — a machine-level identity fault that no "
        "queued issue caused and none can fix by re-approving. Every launch will fail until it is "
        "repaired: log the fleet's config dir into its own account in a supervised window, or "
        "correct SL_FLEET_CLAUDE_CONFIG_DIR"),
    9: ("fence_down",
        "the pre-flight fence check refused the flight (#326): this machine declares its fleet "
        "fenced, and a TOKENLESS connection to the session host's control socket was either SERVED "
        "or unanswerable. A served one means there is no fence at all — every worker pane already "
        "carries the socket path, so any session launched onto it could drive the whole fleet with "
        "ten lines of python and no host binary. An unanswerable one proves nothing either way, "
        "and silence is never read as a fence here. NO issue caused this and re-approving fixes "
        "nothing: rebuild the patched host (vendor/herdr/build.sh) and re-run `superlooper fleet "
        "--install`, or write SL_FLEET_FENCE=off into the fleet prefix's `environment` file on a "
        "machine that is deliberately unfenced — since #355 that file is the machine's declaration "
        "and an exported variable no longer overrides it. The captured stderr says which of the "
        "two verdicts it was"),
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
        "the message could not be sent at all: the session host refused it, the lane id cannot "
        "address an agent, or this agent has no delivery oracle — a channel fault, not the "
        "session's, so nothing was delivered and the session never saw the message"),
    3: ("pane_deferred",
        "NOTHING WAS TYPED and the session may be perfectly healthy: the host could not vouch for "
        "the pane, it reports the agent waiting on a person, or the session has written no record "
        "to judge it by (so it may be sitting at a first-run dialog) — the runner refused to type "
        "and will retry"),
    4: ("pane_dead",
        "the agent process is gone and the pane is a bare shell — typing here would run the "
        "message as a permission-bypassed shell command, so the caller must relaunch instead"),
    5: ("pane_logged_out",
        "the session's auth died in-process: the TUI is alive but every turn is refused, so a "
        "nudge cannot be answered and a relaunch would re-enter dead auth — this needs the owner"),
    6: ("pane_at_dialog",
        "the session is ALIVE and asking its own question in-window, waiting on an answer — going "
        "quiet to wait is not a fault, and parking it would kill a working lane"),
    7: ("send_unproven",
        "the prompt WAS submitted to the session host and nothing could confirm it arrived — rc is "
        "never delivery evidence, so the runner refuses to claim one. Distinct from a deferral "
        "because something really was typed: a caller with a one-shot key must spend it rather "
        "than re-submit into a live worker every tick"),
    124: ("nudge_timeout", "the nudge script never returned within the runner's timeout"),
    127: ("nudge_script_unrunnable",
          "the nudge script could not be executed at all (missing, or not executable)"),
}

_RC_TABLES = {"launch": _LAUNCH_RC, "nudge": _NUDGE_RC}

# ---- what the captured TEXT means --------------------------------------------------------------
# Checked BEFORE the rc-only reading, because the launcher exits 1 for five distinct faults
# and only its stderr says which. Matched case-insensitively against the real strings the tools
# emit (cmux's own error text, and the scripts' own `echo ... >&2` diagnostics). Order is
# significant: first match wins, so the most specific patterns lead.
#
# These refine an outcome that has ALREADY failed — they never create one. For a PER-ISSUE reason a
# substring that matches by accident costs a mis-worded reason on a real failure, never a false
# park. For a CHANNEL reason the cost is higher and asymmetric: an accidental match turns one
# issue's own fault into a HELD QUEUE, which `is_channel_fault` calls the bigger, quieter outage.
# So a needle that maps to a channel reason must be impossible in the loop's own launcher text —
# see the note on the gh needles below, which a bare "429" violated by matching the `[i429]` id
# prefix that every single launcher line carries.
_LAUNCH_TEXT = (
    # FIRST OF ALL (issue #326) — and note this needle is NOT the fence's classification path. rc=9
    # is authoritative (see `_RC_AUTHORITATIVE`), precisely because this refusal's text interpolates
    # a socket path and a switch value the engine did not choose. What remains here is the FALLBACK
    # for the one window where the rc carries no meaning: a newly-merged launcher emitting rc=9 read
    # by an engine published BEFORE this change, whose rc table has no entry for it. That degrades
    # to a per-issue park rather than a held queue — loud and bounded, and it clears the moment the
    # engine is republished.
    #
    # It leads the table for the same reason the rc rule exists: an interpolated socket path can
    # contain any other needle here, so any position but the first is a position some later needle
    # can be inserted above. Costs the readings below nothing — "FENCE DOWN" is the loop's OWN
    # phrase from lib/launch.py, which gh, git and cmux can never emit, and which no variable name
    # in an env-poison refusal can contain (it has a space in it).
    (("fence down",),
     ("fence_down",
      "this machine declares its fleet fenced and the pre-flight found the session host's control "
      "socket unfenced (a tokenless caller was SERVED) or unanswerable — so no session was "
      "launched onto it. A machine-level fault no queued issue caused and none can fix by "
      "re-approving: rebuild the patched host and reinstall the fleet server, or declare the "
      "machine unfenced deliberately")),
    # SECOND (issue #448), and ahead of everything below for the same reason `fence down` leads:
    # two of the triage launcher's refusals interpolate a value the engine did not choose (a
    # per-repo `triage.home` string, a checkout path), so any position a later needle could be
    # inserted above is a position where `SL_TRIAGE_HOME=not_found` reads as a dead cmux workspace.
    #
    # It is a PER-ISSUE reason, deliberately, and that is the whole point of the entry: these are
    # per-REPO CONFIGURATION faults (a typo'd home, an SL_REPO naming a checkout that moved), and
    # without a needle they fall through to the rc=1 default `launch_failed_before_delivery` —
    # which IS a channel fault. One repo's config typo would then hold the entire approved queue,
    # waiting on something that never self-heals and that no queued issue caused. `TRIAGE LAUNCH
    # REFUSED` is the loop's OWN phrase from lib/launch.py; gh, git and the session host can never
    # emit it, and it contains spaces, so no variable name or path can carry it by accident.
    (("triage launch refused",),
     ("triage_launch_refused",
      "the triage flight could not be launched because nothing told it a place to run — an "
      "unreadable `triage.home`, or an SL_REPO naming a checkout that is not there. No session was "
      "created. Fix the repo's `.superlooper/config.json`, or the checkout path; note that a "
      "MISSING CHECKOUT is a machine-level fault every worker launch is hitting too, and their own "
      "refusals are what hold the queue for it — this reading only says the flight did not go "
      "out, which is deliberately not a reason on its own to stop the approved queue")),
    # THEN (issue #301), ahead of the gh needles. A poisoned environment is causally
    # UPSTREAM of the auth death it can produce — an inherited XDG_CONFIG_HOME is exactly how `gh`
    # dies — so when the session's refusal names the environment, the environment is the honest
    # reading and "run `gh auth login`" is a confidently wrong remedy. Ordering it here costs the
    # auth readings nothing: this needle is the loop's OWN words from start-session.sh's refusal,
    # a phrase gh (and cmux) can never emit.
    (("env poisoned",),
     ("env_poisoned",
      "the launch-floor env scrub could not clean this session's own environment: variables that "
      "silently change how a session runs — API-key billing, a redirected base URL, transcript "
      "saving switched off — survived into it, so the flight was refused before it started. The "
      "captured stderr names them; find where they are exported and remove them")),
    # THEN, ahead of every cmux pattern below (issue #299). The auth refusals relay `gh`'s OWN
    # error text into the launcher's stderr, and gh's wording is not ours to control — a message
    # containing "could not connect" or "not_found" would otherwise be read as a dead cmux anchor
    # and raise a socket/workspace alert about a GitHub fault. Both refusal paths emit the literal
    # "GH AUTH DEAD", so matching it first keeps an auth failure classified as auth.
    #
    # A TRANSIENT is split out ahead of the auth reading for the same reason one level down: the
    # assert cannot tell "not authenticated" from "GitHub did not answer", and rate-limit
    # exhaustion, a GitHub outage or a dead DNS would otherwise park the issue under a memo telling
    # the owner to re-login — a confidently WRONG remedy. Named as its own reason, it is a channel
    # fault (below) and the queue holds until GitHub is reachable again instead.
    # Every needle here must be a string the LOOP'S OWN launcher text can never contain. That is a
    # sharper bar than the cmux patterns below, because these reasons are CHANNEL faults: a stray
    # match no longer costs a mis-worded reason on an already-failed launch, it converts a
    # one-issue park into a HELD QUEUE on a fault that never self-heals. A bare "429" cost exactly
    # that — every launcher line is prefixed `[$ID]`, so on issue i429 a missing brief, a failed
    # worktree and a dead cmux workspace all read as "GitHub is rate-limited" and froze the queue.
    # Hence `http 429` / `api rate limit` over `429`, and the enumerated 5xx over `http 5`.
    #
    # `connection refused` is DELIBERATELY ABSENT for the same reason one rung up: cmux's own
    # socket error carries it too, and this tuple is ordered ahead of `anchor_socket_lost`, so
    # including it turned a dead cmux socket into "wait for GitHub to come back" — a remedy for
    # a fault that never self-recovers. gh's Go error always spells the whole
    # `dial tcp <ip>:443: connect: connection refused`, which `dial tcp` already catches.
    (("api rate limit", "secondary rate limit", "http 429",
      "http 500", "http 502", "http 503", "http 504",
      "could not resolve", "dial tcp",
      "no answer within", "i/o timeout", "network is unreachable", "temporary failure"),
     ("gh_probe_unreachable",
      "the gh-auth assert could not get an answer OUT of GitHub — a rate limit, an outage, or a "
      "network fault, not a credential that has died. Re-authenticating would fix nothing; the "
      "queue is held until GitHub answers again, and resumes on its own when it does")),
    # The Anthropic half (#314), ahead of the gh needles for the same ordering reason they are
    # ahead of the cmux ones: these are the loop's OWN words, and the memo they select talks about
    # a subscription rather than about `gh auth login`. The runner-env spelling leads its own
    # session-env sibling, because "CLAUDE IDENTITY REFUSED" and "CLAUDE IDENTITY (runner env)" are
    # different faults with different blast radii and the first match wins.
    (("claude identity (runner env)",),
     ("claude_identity_wrong_runner",
      "the RUNNER's own environment could not produce the Anthropic account to launch sessions "
      "against, so no pane was opened — a machine-level identity fault no queued issue caused. "
      "Repair the fleet config dir's login in a supervised window")),
    (("claude identity refused",),
     ("claude_identity_wrong",
      "the positive Anthropic-account assert refused the flight from inside the session's own "
      "environment: not logged in, on an API key, or on an org this launch did not assign. The "
      "captured stderr names which")),
    (("gh auth dead (runner env)",),
     ("gh_auth_dead_runner",
      "the RUNNER's own `gh` could not say who it is, so no session could be launched against any "
      "identity and no tab was opened — a machine-level GitHub auth fault no queued issue caused. "
      "Repair with `gh auth login --hostname github.com`")),
    (("gh auth dead",),
     ("gh_auth_dead",
      "the positive gh-auth assert refused the flight: `gh` did not answer as the login this loop "
      "runs as from inside the session's own environment, so the session could not have read its "
      "issue or posted any evidence. Re-login with `gh auth login --hostname github.com`")),
    # THE storm (2026-07-09). cmux exited 0 while printing this to stdout, so the cmux launcher's
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
      "the worktree could not be created — a git-level fault (a leftover worktree, a locked index, "
      "a branch already checked out elsewhere), or a git that could not be asked at all. The "
      "captured stderr names the rcs; the base branch is NOT claimed either way here, because the "
      "one case that proves it missing has its own exit code (base_missing) and this one does not")),
    (("sanitize validation failed", "issues.json load"),
     ("identity_invalid",
      "the issue's identity or branch failed validation before anything reached git or the shell "
      "— the runner's own state for this issue is unusable, not the launch machinery")),
)


# ---- who is at fault: the DELIVERY CHANNEL, or the ISSUE (issue #153) --------------------------
# A launch failure is one of two kinds, and the loop charges them in opposite ways. A DELIVERY-
# CHANNEL fault — the cmux launch anchor, the launch shim, or the launch machinery itself — is a
# fault NONE of the queued issues caused: the launch never reached a worker because the channel was
# down. Charging an issue a launch-cap increment or a park for it blames an issue for something it
# did not do (the 2026-07-09 storm: one dead anchor walked ten issues into ten parks in ~8 min).
# The runner holds the queue systemically and probes with a canary (#24/#115) for these instead.
# Every OTHER launch failure names THIS issue's own state — its base branch, its worktree, its
# identity, its brief — and still parks that one issue. These are the reasons _classify() emits for
# the machinery-level faults; a reason absent here (base_missing, gh_auth_dead, env_poisoned,
# worktree_create_failed, identity_invalid, brief_missing, or any unmapped rc) is treated as per-issue.
#
# env_poisoned (rc=6, issue #301) is absent for exactly the reason gh_auth_dead is, and the two were
# decided together: it is an ENVIRONMENT fault whose memo names variables in a file only the owner
# can edit. Held as a channel fault it would surface as the systemic-launch ALERT, whose body talks
# about App Nap and the cmux anchor — so an exported ANTHROPIC_API_KEY would be reported as a cmux
# problem and the bill would keep arriving. Parked per-issue, the memo names the variables.
#
# gh_auth_dead (rc=4, issue #299) is deliberately NOT here, for the same reason base_missing is not:
# it is an ENVIRONMENT fault whose memo the owner must actually READ. Routed to the channel it would
# hold the queue behind the systemic-launch ALERT, whose body names App Nap and the cmux anchor —
# so dead GitHub auth would be reported to the owner as a cmux problem, the exact mis-blame the
# text table above exists to end. Parked per-issue, the memo names the auth and the `gh auth login`
# remedy.
#
# Both of those notes end at the same wall — "we would hold it if the hold could name its own cause"
# — and #320 is where that wall came down. Neither reason moved into this set: a single sample still
# cannot tell one broken worktree from a broken machine, so both still park their own lane on one.
# What changed is that a SECOND distinct lane refusing the same way now escalates them to a held
# queue under an alert reason of their own (SYSTEMIC_ESCALATION_REASONS below, and the layer in
# actions.py). So the reasoning above stands exactly as written, and the "or park it wrongly" horn
# of its dilemma is gone.
CHANNEL_FAULT_REASONS = frozenset({
    "anchor_workspace_missing",       # the 07-09 storm: anchor targets a deleted cmux workspace
    "anchor_socket_lost",             # the runner lost its cmux socket — it reaches no pane at all
    "shim_not_fired",                 # rc=2: a tab was created but the shim never ran the command
    "launch_failed_before_delivery",  # rc=1 generic: "nothing about the session itself is at fault"
    "launch_timeout",                 # rc=124: the launcher hung — no session ever started
    "launch_script_unrunnable",       # rc=127: the launch script itself could not execute (install)
    "agent_unsupported",              # rc=64: the configured agent is repo-wide wrong, not one issue's
    # (#299) The RUNNER's own gh cannot authenticate: no tab was ever opened, every launch will fail
    # identically, and no issue can fix it by re-approving. Charging it per-issue would walk the
    # whole approved queue into parks over one machine-level fault — the 07-09 shape, new cause.
    "gh_auth_dead_runner",
    # (#299) GitHub itself did not answer (rate limit / outage / network). Nothing is wrong with any
    # issue OR with the credential, and it repairs itself — hold, never park, never say "re-login".
    "gh_probe_unreachable",
    # (#314) The RUNNER's own environment cannot produce the Anthropic account sessions launch
    # against — its fleet config dir is logged out, on an API key, or on the wrong org. Every launch
    # reads the same answer, so this is the gh_auth_dead_runner shape one credential over: charged
    # per-issue it would walk the whole approved queue into parks over one machine-level fault.
    # Its SESSION-side sibling (`claude_identity_wrong`, rc=7) is deliberately absent for the same
    # reason gh_auth_dead is: it is an environment fault whose memo the owner must actually READ,
    # and held as a channel fault it would surface under the systemic-launch alert, whose body
    # names App Nap and the cmux anchor.
    "claude_identity_wrong_runner",
    # (#326) The session host's control socket is unfenced (or unanswerable) on a machine that
    # declares its fleet fenced. Every launch here reads the SAME socket and gets the same verdict,
    # so this is the gh_auth_dead_runner shape one layer down: charged per-issue it would walk the
    # whole approved queue into parks over one machine-level fault, and each of those parks would
    # carry a memo asking the owner to re-approve an issue that was never the problem.
    #
    # Unlike its two siblings there is no per-issue variant to hold apart from: a fence is a
    # property of the host server, and no session can be launched in a way that fixes it. Held as a
    # channel fault the queue simply waits — which is the correct posture, because the alternative
    # to waiting is flying workers onto an open socket.
    "fence_down",
})


# ---- faults that are PER-ISSUE on one sample and MACHINE-WIDE across several (issue #320) -------
#
# The set above answers "did any queued issue cause this?" from a SINGLE launch. For three reasons
# that question genuinely cannot be answered from one sample, and each of them carries a note above
# saying so: gh_auth_dead, claude_identity_wrong and env_poisoned are all environment faults, and
# the environment they describe might be ONE worktree's (this lane's problem, park it) or EVERY
# worker's (nobody's problem, hold the queue). Read as per-issue they cost the 2026-07-29 shape: an
# inherited XDG_CONFIG_HOME de-authenticates `gh` in every worker's fresh env while the RUNNER's own
# stays healthy, so the poll keeps working, every approved issue launches, refuses, and parks in
# turn, and the owner pays N re-approvals for a 30-second `gh auth login`. Read as channel faults
# they cost the opposite mistake — a one-off broken worktree freezing the whole loop, and (worse) a
# hold whose alert body names the cmux anchor and macOS App Nap, which is why each of those notes
# concluded "parked per-issue, the memo names the real cause".
#
# The discriminator is the owner's own (2026-08-03), and it is the same inference the systemic-launch
# streak already makes for the delivery channel one rung up: N consecutive refusals across DISTINCT
# issues means it is not the issues, it is the environment. So the reasons below stay per-issue on
# one sample — a genuinely one-off session fault still parks just its own lane, with its own memo —
# and the layer in actions.py escalates them to a HELD queue once a second distinct lane refuses the
# same way. Each escalated class carries its OWN alert reason and remedy there; a class held under
# another class's banner is the mis-blame this whole family of notes exists to prevent.
#
# Adding a class is this frozenset plus one row in actions.LAUNCH_ALERT_REASONS (and its message).
# Nothing about the detector, the hold, the recovery probe or the resume edge changes.
SYSTEMIC_ESCALATION_REASONS = frozenset({
    "gh_auth_dead",           # rc=4 — the SESSION's own gh could not say who it is
    "claude_identity_wrong",  # rc=7 — the SESSION's own Anthropic account is absent/wrong
    "env_poisoned",           # rc=6 — the launch-floor scrub could not clean the session's env
})


def is_escalatable_fault(rec):
    """True when this launch-failure evidence names a fault that is honestly PER-ISSUE on one
    sample and MACHINE-WIDE across several (issue #320) — the set above.

    Fails SAFE exactly as `is_channel_fault` does: a corrupt or unmapped record returns False, so a
    novel reason can never join a streak that holds the queue. The caller still charges the lane's
    own launch cap either way — escalation ADDS a sample to the environment question, it never
    spends the per-issue accounting that makes a one-off fault park."""
    return isinstance(rec, dict) and rec.get("reason") in SYSTEMIC_ESCALATION_REASONS


def is_channel_fault(rec):
    """True when this launch-failure evidence record names a DELIVERY-CHANNEL fault (issue #153):
    the anchor, the shim, or the launch machinery — a fault no single issue caused.

    An UNMAPPED rc (reason `<kind>_rc_<n>`) and a corrupt/non-record fail SAFE: they return False (a
    per-issue fault), so a genuinely novel exit code can never silently freeze the whole loop. This is
    NOT a blanket 'only these reasons ever hold' guarantee, though: `launch_failed_before_delivery` is
    the reason `_classify` returns for ANY rc=1 whose stderr matches no per-issue pattern, and it IS a
    channel reason — because the launcher defines rc=1 as 'aborted before any pane could host a
    worker; nothing about the session itself is at fault', and its per-issue rc=1 causes
    (worktree_create_failed, identity_invalid, brief_missing) each echo a distinguishing stderr line.
    So the contract the classifier relies on is the launcher's: a per-issue fault must carry either its
    own rc (base_missing=3, gh_auth_dead=4) or a matching stderr line — a future per-issue rc=1 added WITHOUT one would
    be read as channel. A wrongly-held queue is a bigger, quieter outage than one wrongly-parked issue
    the owner can see and re-approve, so the default leans to holding on the machinery-level reasons."""
    return isinstance(rec, dict) and rec.get("reason") in CHANNEL_FAULT_REASONS


# Exit codes whose rc DECIDES, with the text pass skipped entirely (issue #326).
#
# The text pass exists to disambiguate — the launcher exits 1 for five distinct faults and only its
# stderr says which. It costs nothing where an rc is already unambiguous, and it costs everything
# where that rc's memo INTERPOLATES a value this engine did not choose. `fence_down` does exactly
# that: it names the control socket it probed (a path the operator picked) and, when the switch is
# unreadable, the switch's value (which may be any string at all). Run through the needle table,
# a socket at `/tmp/env poisoned.sock` classifies a machine-level fence failure as `env_poisoned` —
# a PER-ISSUE reason. That is the 2026-07-09 shape exactly, and the same mistake `[i429]` already
# taught once: the whole approved queue walks into parks over one machine fault, each memo naming
# something the issue did not do.
#
# The rule this generalizes: an rc whose stderr carries text from outside this engine may not be
# classified BY that text. Adding an entry here is how a future such reason opts out — and it is
# strictly safer than ordering its needle first, which the next needle added above it would undo.
_RC_AUTHORITATIVE = {"launch": frozenset({9})}


def _classify(kind, rc, captured):
    """(reason, detail) for a non-success outcome. Text first, then the rc table, then an honest
    fallback that names the rc rather than inventing a cause — except for the rcs above, which
    skip the text pass because their own text is partly not ours."""
    if rc in _RC_AUTHORITATIVE.get(kind, ()) and rc in _RC_TABLES.get(kind, {}):
        return _RC_TABLES[kind][rc]
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

    Per-surface SIZING happens at the point of capture — nudge-pane.sh bounds the screen to
    SCREEN_SNIPPET_MAX, ScriptRC bounds a launch stderr to STDERR_TAIL_MAX — and build applies only
    the uniform final SAFETY cap. It must NOT re-bound tighter: bound() keeps the TAIL, and a nudge
    refusal puts its `state=<verdict>` line FIRST (the one carrier of which of rc=3's five screen
    verdicts fired — menu/trust/permission/quota/unknown, all one `reason`), so a tighter tail-cut
    on a large screen would silently drop the verdict, re-creating the i160 "defer nobody could
    re-classify" it exists to end (fresh-review P1). STDERR_TAIL_MAX clears the whole composite.
    """
    text = bound(captured, limit=STDERR_TAIL_MAX)
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
