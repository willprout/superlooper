"""The launch-time identity env contract (issue #314) — which Anthropic account a session runs as.

#301 removed the poison that flips a session onto API billing. This is the other half, and #300
found it is sharper than a scrub, because the isolation mechanism is a STRING:

* ``claude`` derives its macOS keychain service name as ``Claude Code-credentials-<8 hex>`` where
  the suffix is ``sha256`` of the ``CLAUDE_CONFIG_DIR`` value **as written** — no canonicalisation
  of any kind. Five spellings of one directory produced five credential namespaces, and the wrong
  one presents as a **logged-out session**, never as an error (#300 landmine 1). So isolation holds
  only as long as every spawn emits the SAME string, which is why the launch path derives it once
  and this module is the one place that says what "the same string" means.
* ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` overrides that derivation, and the binary's own branch reads
  a **set-but-empty** value as "no config dir" — collapsing the namespace back to the owner's
  unsuffixed default. An inherited empty value is the classic shell accident
  (``export X=$SOMETHING_UNSET``) and its consequence is the c1 silent-billing-flip in a new
  costume (#300 landmine 2).
* And ``claude auth status`` answers ``loggedIn: true`` for a session running on an **API key**,
  with ``email``/``orgId``/``subscriptionType`` all null — measured against the real binary
  (2.1.222) on 2026-08-04. "No error" is therefore not "on the intended subscription". Only a
  positive read of the account tells them apart, which is the same doctrine #299 applies to ``gh``.

The owner's ruling on this issue (2026-08-04) is STRICT and is what the verdicts below encode: a
worker must provably be logged in, on the intended account, never on an API key, else the launch is
refused. The stated rationale is capacity, not secrecy — the two Max accounts are separate
rate-limit pools and the runner's usage machinery reasons about one pool per lane, so a session on
the wrong pool makes assignment non-deterministic even though both accounts are the owner's.

**Two callers, one contract.** ``lib/launch.py`` derives the canonical string ONCE per launch and
names it in the pane environment; ``bin/start-session.sh`` runs ``python3 identity.py --assert``
inside the session's own environment, which is the only place that can prove anything about it
(the pane's shell sources the operator's rc files after the launcher is gone). ``lib/fleet.py``'s
build-up judge reads the same functions, so the machine's report and the launch seam can never
disagree about what a canonical dir or a healthy account is.
"""
import hashlib
import json
import os
import subprocess
import sys
import unicodedata

# Claude Code's own variables. Named as strings because they are the AGENT's spelling, not ours —
# everything this loop passes down is `SL_*`, and these two are the exceptions the agent itself
# reads (which is exactly why they can be inherited from anywhere).
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
REDIRECT_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"

# Ours. `SL_CLAUDE_CONFIG_DIR` is what the LAUNCHER names in the pane (the assignment), and
# `SL_FLEET_CLAUDE_CONFIG_DIR` is what the MACHINE sets (the operator's choice, read once by the
# launcher). Two names rather than one because they are two different claims: "this machine runs
# its fleet under that dir" and "this session was assigned that dir by the launch that started it".
ASSIGN_VAR = "SL_CLAUDE_CONFIG_DIR"
FLEET_DIR_VAR = "SL_FLEET_CLAUDE_CONFIG_DIR"
# The expected account, named in the pane by the launcher — and readable as an operator PIN in the
# runner's own environment, the way `SL_EXPECT_GH_LOGIN` is (#299). One name for both halves,
# pinned empty in the runner's launch env so a runner started from inside a worker pane can never
# inherit that session's answer and skip resolving the identity itself.
EXPECT_VAR = "SL_EXPECT_CLAUDE_ACCOUNT"

# Suggested only, and named in fix text rather than applied as a default: a default config dir
# applied by the launch path would move every worker on every machine onto a dir nobody provisioned,
# and an unprovisioned dir parks a worker at the first-run theme picker (#300).
SUGGESTED_FLEET_DIR = "~/.claude-fleet"

# The status read is LOCAL (it reads the keychain item), so unlike the gh probe there is no retry:
# a network blip is not this read's failure mode, and every second spent here comes out of the
# launcher's 30s delivery-verify window, which the gh probe is already spending from.
PROBE_SECONDS_VAR = "SL_CLAUDE_PROBE_SECONDS"
DEFAULT_PROBE_SECONDS = 8

REFUSED = 3                    # identity.py's own exit code for a refusal; 0 = the account is right


# ------------------------------------------------------------------ the string, and its namespace

def namespace_suffix(value):
    """The 8 hex characters ``claude`` appends to its keychain service name for this config dir.

    Read out of the shipped binary in #300 and re-derived here (``sha256`` over the NFC-normalised
    string, first 8 hex chars) for ONE reason: it turns "two spellings of one directory" from an
    abstraction into the thing that actually differs, so a refusal can name the namespace a wrong
    spelling would have looked in — and "logged out" is the only symptom the pane itself can see.
    """
    text = unicodedata.normalize("NFC", value if isinstance(value, str) else "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def canonical(value, home=None):
    """``(canonical_string, None)`` for a config-dir spelling, or ``(None, why-it-is-refused)``.

    NORMALISED, not resolved: `os.path.normpath` collapses the trailing slash, the `//` and the
    `/./` that #300 measured producing their own credential namespaces, and nothing more. It
    deliberately does NOT `realpath` — a symlink resolved here would hand a session a string the
    operator never wrote, and the credential namespace is a hash of the string, so "helpfully"
    rewriting it is the same class of surprise this function exists to remove.
    """
    raw = value if isinstance(value, str) else ""
    if not raw.strip():
        return None, "no config dir was given"
    if raw.startswith("~"):
        if raw != "~" and not raw.startswith("~/"):
            return None, ("%r names another user's home directory — the credential namespace is a "
                          "hash of this string, so a `~user` spelling is neither this account's dir "
                          "nor a portable one; give an absolute path" % raw)
        if home is None:
            home = os.path.expanduser("~")
        if not isinstance(home, str) or not home.startswith("/"):
            return None, ("%r starts with `~` and this environment has no usable HOME to expand it "
                          "against, so it would reach a session as a literal `~` — its own third "
                          "credential namespace, which reports logged-out" % raw)
        raw = home if raw == "~" else os.path.join(home, raw[2:])
    if not raw.startswith("/"):
        return None, ("%r is not an absolute path — the credential namespace is a hash of this "
                      "string as written, so a relative dir is a DIFFERENT identity that reports "
                      "logged-out rather than erroring" % raw)
    return os.path.normpath(raw), None


def config_dir_problem(value):
    """None, or why this spelling of a fleet config dir cannot be trusted AS WRITTEN.

    The judge's half of `canonical`: it does not fix the string, it says whether the string is
    already the one canonical spelling. Used by the build-up report (`fleet.check_identity`) and by
    the launch floor, so a dir that would pass the doctor is exactly the dir a launch will accept.
    """
    if not isinstance(value, str) or not value:
        return "no fleet config dir is configured"
    if not value.startswith("/"):
        return ("%r is not an absolute path — the credential namespace is a hash of this string as "
                "written, so a relative or ~-spelled dir is a DIFFERENT identity that reports "
                "logged-out" % value)
    if value != os.path.normpath(value):
        return ("%r is not the canonical spelling of %r — a trailing slash, a `//` or a `/./` each "
                "hash to their own credential namespace, and a worker launched with the wrong one "
                "presents as auth-death" % (value, os.path.normpath(value)))
    return None


def worker_config_dir(env=None, home=None):
    """``(canonical dir or None, problem or None)`` for the config dir THIS MACHINE assigns workers.

    ``(None, None)`` — nothing configured — is a normal answer and not a fault: it is what every
    machine except the fleet reports, and it means a session runs under the machine's default
    Claude login exactly as it did before this contract existed. What must never happen is a
    configured-but-unusable value silently becoming that same "no assignment".
    """
    env = os.environ if env is None else env
    raw = env.get(FLEET_DIR_VAR)
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    return canonical(raw, home=env.get("HOME") if home is None else home)


def resolve_config_dir(explicit, env=None, home=None):
    """The one derivation the whole launch path uses: an explicit Spec value, else the machine's."""
    if explicit is None:
        return worker_config_dir(env, home=home)
    if not str(explicit).strip():
        return None, None
    return canonical(explicit, home=(env or {}).get("HOME") if home is None else home)


# ------------------------------------------------------------------- the redirect variable (#300)

def redirect_problem(env):
    """None, or why this environment's credential-redirect variable must refuse a launch.

    ANY value, not just the empty one. The empty value is the dangerous accident — the binary's own
    branch reads it as "no config dir" and falls back to the owner's unsuffixed namespace, so the
    fleet quietly bills the owner — but #300's third row is a deliberate escape hatch that points
    one config dir's session at ANOTHER dir's login, and a session that INHERITED that is a session
    running as an account nobody assigned it. Both are refusals; only the memo differs.
    """
    if REDIRECT_VAR not in (env or {}):
        return None
    value = env.get(REDIRECT_VAR)
    if not isinstance(value, str) or not value.strip():
        return ("%s is set but EMPTY in this session's own environment — `claude` reads that as "
                "'no config dir' and falls back to the owner's unsuffixed credential namespace, so "
                "this session would silently spend the owner's subscription with nothing anywhere "
                "erroring (#300 landmine 2). It is the classic `export X=$SOMETHING_UNSET`: find "
                "where it is exported (a shell rc file, a LaunchAgent, a wrapper) and remove it"
                % REDIRECT_VAR)
    return ("%s=%r reached this session's own environment — it REDIRECTS the credential namespace "
            "onto another config dir's login, so this session would run as an account no launch "
            "assigned it. The fleet does not set this variable; an inherited one is never passed "
            "through. Find where it is exported and remove it" % (REDIRECT_VAR, value))


# ------------------------------------------------------------------------ the account, positively

def parse_status(text):
    """The dict `claude auth status` printed, or None.

    ``raw_decode`` takes the LEADING JSON object and ignores anything after it — the same hazard
    ``usage.probe_auth`` documents: an update banner or a token-refresh notice printed after the
    blob makes a strict ``json.loads`` raise, and a healthy read would then degrade to "unreadable",
    which under this contract means a REFUSED launch.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def describe(status):
    """A one-line human name for an account, for the memo and the success line."""
    status = status if isinstance(status, dict) else {}
    return "%s (org %s, %s)" % (status.get("email") or "?", status.get("orgId") or "?",
                                status.get("subscriptionType") or "?")


def account_problem(status, expected=None):
    """None, or why this `claude auth status` reading is not an account a worker may fly on.

    POSITIVE by construction: every branch requires an observation, and the final `return None` is
    reached only after the read said logged in, on a subscription, not on an API key, with an org
    that matches the expectation. Absence of an error is never enough — the API-key row below is
    the measured proof of why.
    """
    if not isinstance(status, dict) or not status:
        return ("`claude auth status` produced no readable JSON, so which Anthropic account this "
                "environment holds is unknown — and absence of an answer is not identity. (A "
                "logged-out dir does NOT reach this branch: it answers with a body, which is why "
                "the body and never the exit code is what this reads.)")
    if status.get("loggedIn") is not True:
        return ("this environment is not logged in to Claude (`loggedIn` is %r, authMethod %r) — a "
                "worker launched here lands on a login screen, which the session host classifies "
                "as idle rather than as blocked"
                % (status.get("loggedIn"), status.get("authMethod")))
    api_key = status.get("apiKeySource")
    if api_key:
        # MEASURED (2026-08-04, claude 2.1.222): with ANTHROPIC_API_KEY exported, this same command
        # answers `loggedIn: true` with null email/orgId/subscriptionType. A check that stopped at
        # `loggedIn` would green-light exactly the billing flip #301 exists to prevent.
        return ("this environment is running on an API key (apiKeySource %s), not on the "
                "subscription — `loggedIn` is true and the account fields are null, which is the "
                "silent API-billing flip wearing a healthy session's costume. The launch floor "
                "scrubs ANTHROPIC_*; if one is back, find where it is exported and remove it"
                % api_key)
    subscription = status.get("subscriptionType")
    if not isinstance(subscription, str) or not subscription.strip():
        return ("this environment reports no subscriptionType, so it is not provably on a "
                "subscription seat — the owner's ruling on #314 is that a worker flies on a known "
                "subscription or not at all (account: %s)" % describe(status))
    org = status.get("orgId")
    if not isinstance(org, str) or not org.strip():
        return ("this environment reports no orgId, so its billing entity cannot be established — "
                "orgId is what makes the two accounts' rate-limit pools tellable apart")
    if expected and org != expected:
        return ("this environment is on org %s (%s, %s) and the launch expected org %s — the two "
                "subscriptions are separate rate-limit pools, so a session on the wrong one makes "
                "lane assignment non-deterministic"
                % (org, status.get("email") or "?", subscription, expected))
    return None


# ------------------------------------------------------------------------------- reading the CLI

class _Subprocess:
    """The default status runner. Shaped like ``launch.Edges`` / a test's fake, and nothing else."""

    def run(self, argv, timeout=None, cwd=None, env=None):
        try:
            return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                                  timeout=timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", "no answer within %ss" % timeout)
        except (OSError, ValueError) as e:
            return subprocess.CompletedProcess(argv, 127, "", "could not run %s: %s" % (argv[0], e))


def probe_env(base=None, config_dir=None):
    """The environment a status read runs in — the ONE difference from ours is the config dir.

    The redirect variable is dropped rather than passed through, so this read measures the
    namespace the config dir names and not some other one; a caller that wants to know whether the
    redirect is present asks `redirect_problem`, which is a different question with a different
    remedy. Nothing else is touched: PATH, HOME and the keychain context the read legitimately
    needs must survive (c25's landmine — overriding HOME breaks macOS keychain OAuth outright).
    """
    env = dict(os.environ if base is None else base)
    env.pop(REDIRECT_VAR, None)
    if config_dir:
        env[CONFIG_DIR_VAR] = config_dir
    else:
        # ABSENT, never empty: an empty CLAUDE_CONFIG_DIR is its own namespace (`sha256("")`), not
        # "the default one" — the distinction the whole contract rests on.
        env.pop(CONFIG_DIR_VAR, None)
    return env


def status_from(proc):
    """The account a finished `claude auth status` reported, or None — THE BODY, NEVER THE RC.

    Load-bearing and measured (2026-08-04, 2.1.222): the command exits **1** when it is logged out
    while printing a perfectly informative `{"loggedIn": false, "authMethod": "none"}`. A reader
    that discarded the body on a non-zero rc would turn the single most likely state of a fresh
    fleet config dir into "could not be read at all", and send the operator looking for a broken
    binary instead of a login screen. `None` is reserved for a genuine non-answer.

    Shared by every caller — the launcher, the in-session floor and the build-up judge — because
    they run the same command through three different subprocess shapes and must not disagree
    about what its answer means.
    """
    return (parse_status(getattr(proc, "stdout", "") or "")
            or parse_status(getattr(proc, "stderr", "") or ""))


def read_status(runner, claude, config_dir=None, env=None, timeout=None):
    """`claude auth status` under exactly one config dir, as a dict — or None if unreadable.

    #300's measuring instrument: JSON with `email` / `orgId` / `subscriptionType`, per config dir,
    non-interactively and without spending a token (so it is inside the owner's no-headless
    billing rule — it is a status read, never a session).

    The answer comes from `status_from` — the body, never the exit code, for the reason spelled
    out there.
    """
    if not claude:
        return None
    return status_from(runner.run([claude, "auth", "status"],
                                  timeout=timeout or DEFAULT_PROBE_SECONDS,
                                  env=probe_env(env, config_dir)))


def resolve_claude(env=None):
    """``(path, None)`` for the binary this stack would launch, or ``(None, why-not)``.

    Delegates to ``stack_doctor.resolve_claude`` — the existing Python twin of start-session.sh's
    own #303 ladder, which ``tests/test_start_session.py`` drives against the shell side so the two
    cannot drift. There is deliberately no third ladder here: the identity read must ask the SAME
    binary the session is about to run, or it is a measurement of a different process.
    """
    try:
        import stack_doctor
    except ImportError as e:                                 # an engine published without lib/
        return None, ("the ladder that decides which claude this stack runs could not be loaded "
                      "(%s) — republish the engine through bin/install.sh" % e)
    found = stack_doctor.resolve_claude(stack_doctor.Probe(os.environ if env is None else env))
    if found.get("ok") and found.get("path"):
        return found["path"], None
    reason = {
        "relative": "the SL_CLAUDE pin %r is a relative path, which resolves against whatever "
                    "directory each process happens to be in" % found.get("path"),
        "unrunnable": "the SL_CLAUDE pin %r is not an executable file" % found.get("path"),
        "absent": "no claude binary could be resolved at all (SL_CLAUDE unset, no standalone "
                  "install at ~/.local/bin/claude, none on PATH)",
    }.get(found.get("reason"), "the claude binary could not be resolved")
    return None, reason


# ---------------------------------------------------------------------------- the session verdict

def expected_account(env):
    value = (env or {}).get(EXPECT_VAR)
    return value.strip() if isinstance(value, str) and value.strip() else None


def env_problem(env):
    """None, or why this session's identity ENVIRONMENT is not one a worker may fly in.

    Ordered before any account read on purpose: both branches decide WHICH credential namespace a
    read would consult, so a status taken under a bad one is a confident measurement of the wrong
    thing.
    """
    problem = redirect_problem(env)
    if problem:
        return problem
    raw_assigned = (env or {}).get(ASSIGN_VAR)
    assigned = raw_assigned if isinstance(raw_assigned, str) and raw_assigned.strip() else ""
    raw_actual = (env or {}).get(CONFIG_DIR_VAR)
    if not assigned:
        if raw_actual is None:
            return None
        return ("this session was not assigned a Claude config dir by its launch, but its own "
                "environment carries %s=%r — identity is ASSIGNED, never inherited (claim c3), and "
                "a config dir picked up from a shell rc file or a LaunchAgent is a credential "
                "namespace nobody chose. Either remove it, or set %s on this machine so the launch "
                "path assigns it (suggested: %s)"
                % (CONFIG_DIR_VAR, raw_actual, FLEET_DIR_VAR, SUGGESTED_FLEET_DIR))
    problem = config_dir_problem(assigned)
    if problem:
        return ("the launch assigned this session a config dir it cannot use: %s" % problem)
    if raw_actual != assigned:
        return ("this session's %s is %r and the launch assigned %r — they must be BYTE-identical, "
                "because the credential namespace is a hash of the string as written: %r would look "
                "in `Claude Code-credentials-%s` while the fleet's login lives in "
                "`Claude Code-credentials-%s`, and the only symptom is a session that reports "
                "logged out (#300 landmine 1)"
                % (CONFIG_DIR_VAR, raw_actual, assigned, raw_actual,
                   namespace_suffix(raw_actual if isinstance(raw_actual, str) else ""),
                   namespace_suffix(assigned)))
    return None


def session_problem(env, status):
    """The whole in-session verdict: None, or the sentence the launch floor refuses with."""
    return env_problem(env) or account_problem(status, expected_account(env))


# --------------------------------------------------------------------------------- the floor half

def _assert(env, runner=None):
    """``(exit code, message)`` for `--assert`, run inside the session's own environment."""
    problem = env_problem(env)
    if problem:
        return REFUSED, problem
    claude, why = resolve_claude(env)
    if claude is None:
        # DEFERRED, and this is the one branch that does not refuse — deliberately, and it is not
        # a hole. The very next thing start-session.sh does with a claude agent is walk that same
        # #303 ladder itself, and it REFUSES the launch outright when the ladder names nothing
        # runnable (exit 127, no agent, ever). So no session flies unverified either way; what
        # differs is only which memo the owner reads, and "SL_CLAUDE pins a file that is not
        # executable" is the accurate one. Refusing here instead would relabel every binary-pin
        # fault on the machine as an identity fault — a mis-blame of exactly the kind the rc table
        # exists to prevent. What this may never become is a general "could not ask, so proceed":
        # every OTHER unreadable state below is a refusal.
        return 0, ("identity NOT asserted: %s. The binary pin (#303) refuses this launch on the "
                   "next step, which is where that fault is named" % why)
    try:
        seconds = int(str(env.get(PROBE_SECONDS_VAR) or DEFAULT_PROBE_SECONDS).strip())
    except (TypeError, ValueError):
        seconds = DEFAULT_PROBE_SECONDS          # a typo must not mean "refuse"
    config_dir = env.get(CONFIG_DIR_VAR) or None
    status = read_status(runner or _Subprocess(), claude, config_dir=config_dir, env=env,
                         timeout=max(1, seconds))
    problem = account_problem(status, expected_account(env))
    if problem:
        return REFUSED, problem
    return 0, ("%s runs as %s%s" % (claude, describe(status),
                                    " under %s" % config_dir if config_dir else
                                    " under the machine's default Claude config dir"))


def main(argv, env=None, runner=None):
    if list(argv) != ["--assert"]:
        print("usage: identity.py --assert   (verifies THIS environment's Anthropic identity)",
              file=sys.stderr)
        return 2
    code, message = _assert(os.environ if env is None else env, runner=runner)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":                                   # pragma: no cover - process boundary
    sys.exit(main(sys.argv[1:]))
