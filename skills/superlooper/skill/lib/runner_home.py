"""The runner's own process home — where the supervisor lives, and how it is started (issue #306).

Ruled 2026-07-31 (docs/HERDR-ADOPTION-PLAN.md §8.1): **the runner lives OUTSIDE the session host.**
The supervisor never lives inside what it supervises. This module is the one place that knows what
that means mechanically, so every verb built around the OLD home can be disposed of explicitly
instead of quietly carried forward.

There are exactly two homes, and the difference between them is not cosmetic — it is which failure
each one can have:

``pane`` (today, and the default)
    The runner runs in a visible multiplexer tab, and that tab's pane is the LAUNCH ANCHOR every
    worker session is born in. A detached/launchd runner has no pane, so its launches all die with
    a broken pipe (finding D7) — which is why issue #33 removed the launchd runner template and
    wrote "there is no way to make launchd start the runner correctly". That statement was true
    *of a host whose spawn needs an anchor*, and this module is where it stops being universal.

``login-item``
    The runner is an ordinary process started from the owner's GUI login session, talking to the
    session host's server through the five-verb wrapper (``session_host``, issue #304). There is no
    anchor to lose, so #33's prohibition dissolves — but three NEW things can silently be wrong,
    and each is refused at boot rather than explained in a document:

    1. **The session.** It must be ``gui/$UID`` — measured (docs/SPIKES-2026-07-29-results.md,
       test 3): a ``gui/$UID`` LaunchAgent reports ``managername = Aqua``, the login keychain is
       its default keychain, and ``gh`` read its keyring token in one second with no prompt. A
       ``system/`` domain daemon is a DIFFERENT session and was never tested; the reports of
       "intermittent gh auth-death under launchd" live there. So the gui domain is spelled once,
       here, and no function in this file takes a domain parameter — a rule that can be passed as
       an argument is a rule that will one day be passed the wrong argument.
    2. **PATH.** The same spike found the real gotcha is mundane: launchd hands a job
       ``/usr/bin:/bin:/usr/sbin:/sbin`` and nothing else. No Homebrew, no ``~/.local/bin``. A
       launchd-hosted runner that shells ``gh`` by bare name simply does not find it — and reports
       every GitHub read as a failure while looking perfectly alive. The plist therefore carries an
       EXPLICIT PATH built from where the commands actually resolved at install time.
    3. **The host.** The pane home fails hard at boot when the multiplexer or the pane will not
       resolve (D7 — a quiet warning there once let a mis-started runner abort every launch and
       burn every issue's retry cap). The login-item home's equivalent is the session host's server
       not answering, and it is refused the same way. The answer is obtained by the CALLER through
       the wrapper and passed in here as a fact, so this module never touches the host itself.

Everything here is pure — argvs, strings, and verdicts over already-read facts. The I/O lives in
the CLI (which owns the process boundary) and in ``stack_doctor`` (which owns the probe). That is
what lets the boot preflight, the doctor block and the watchdog's restart path all be tested
without a real ``launchctl``.

**What this module deliberately does NOT do:** decide anything per-session. It is about the
supervisor's own process. Worker sessions are the wrapper's business, and the two must not blur —
the whole ruling is that they live on opposite sides of the host boundary.
"""
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape


def _xml(value):
    """One value, safe to drop into an XML text node. Stdlib only, like everything else here."""
    return _xml_escape(str(value))

# ---------------------------------------------------------------------------- the two homes
PANE = "pane"
LOGIN_ITEM = "login-item"
KINDS = (PANE, LOGIN_ITEM)

# The per-repo config key that selects one. Default PANE: a config written before this issue keeps
# today's behaviour exactly, which is what the cross-machine parallel run (plan §6) rests on.
CONFIG_KEY = "runner_home"

# ---------------------------------------------------------------------------- restart mechanisms
# The #116 Restart button's REQUEST half is home-independent (a marker file the live runner honors
# between ticks). Its EXECUTION half is not, and this is where that is decided.
REEXEC = "re-exec"                      # os.execv in place — the pane home, see restart_mechanism
EXIT_TO_SUPERVISOR = "exit-to-supervisor"   # exit 0 and let launchd start a fresh process

# ---------------------------------------------------------------------------- launchd facts
# What launchd actually hands a gui/$UID job, measured in spike 3. Written down because it is the
# whole reason the rendered plist carries an explicit PATH.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The Aqua session's own name, as `launchctl managername` reports it for BOTH an interactive shell
# and a gui/$UID LaunchAgent. Anything else is a different keychain session.
AQUA = "Aqua"

# The commands the RUNNER ITSELF shells by bare name. Deliberately short: the coding agent is
# resolved through start-session.sh's pinned ladder (#303) and the session host through the wrapper
# (#304), so neither belongs in a PATH check owned by the runner's process home.
REQUIRED_COMMANDS = ("gh", "git")

# The same set for the WATCHDOG's job (issue #328). An ALIAS, not a copy, and shared on purpose:
# `gh` is the one whose absence is silent — the watchdog's file-backed detectors keep reporting
# while every GitHub read refuses, which it reads as UNOBSERVABLE and so freezes its clocks — and
# `git` rides along because the shipped watchdog template instructs the operator to record both, and
# because the watchdog hands its OWN environment to the launch shim it spawns a debugger session
# through. A separate name rather than a bare re-use of REQUIRED_COMMANDS so the day the two jobs
# genuinely need different sets, that is one edit here instead of a silent widening of both.
WATCHDOG_COMMANDS = REQUIRED_COMMANDS

# `launchctl print` renders a service as an indented block; the pid line is the one fact this
# module reads out of it. Anchored to the line so a `pid` appearing inside some other value cannot
# be mistaken for the service's own.
_PID_LINE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.M)

# The `state = <words>` line from the same block. A CLOSED vocabulary on both sides on purpose: an
# unrecognised word is UNKNOWN, never quietly folded into whichever answer is convenient.
_STATE_LINE = re.compile(r"^\s*state\s*=\s*(.+?)\s*$", re.M)
_IDLE_STATES = frozenset({"not running", "waiting"})
_RUNNING_STATES = frozenset({"running", "spawn scheduled"})

# The shipped launchd label shape, matching the nightly and watchdog jobs already installed
# (com.superlooper.<job>.<owner>__<name>) so one glance at `launchctl list` groups a repo's jobs.
_LABEL_FMT = "com.superlooper.runner.%s__%s"

# The WATCHDOG's job, in the same shape (issue #328). It lives here rather than beside the watchdog
# because this module is where the launchd vocabulary is spelled — the domain, the print/bootstrap
# argvs and the PATH rule are already shared, and a second file minting launchd labels is a second
# file that can drift from the domain rule they all have to obey.
_WATCHDOG_LABEL_FMT = "com.superlooper.watchdog.%s__%s"

# The plist itself is a SHIPPED TEMPLATE, not a string in here — same home as the nightly and
# watchdog plists, so an operator can read the job before installing it and `tests/test_templates.py`
# validates it as one of the shipped set. The rules that go WITH it — absolute paths, the explicit
# PATH — stay in render_plist below, so there is one door and no half-substituted plist can be
# produced elsewhere.
#
# Read LAZILY, unlike brief.py's eager footer read, because of where this module sits: `config.py`
# imports it, and config is imported by essentially everything. An eager read would turn a missing
# or unreadable template into an ImportError that takes down the runner, the gate, the doctor and
# the CLI — for a file only the installer needs. Failing inside render_plist confines the blast
# radius to the one verb that actually wants it, and says what is wrong.
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "launchd.runner.plist"
_template_cache = None


def _template():
    global _template_cache
    if _template_cache is None:
        try:
            _template_cache = _TEMPLATE_PATH.read_text()
        except OSError as e:
            raise FileNotFoundError(
                "the runner LaunchAgent template is missing or unreadable at %s (%s) — republish "
                "the engine through bin/install.sh, which ships templates/ with the payload"
                % (_TEMPLATE_PATH, e))
    return _template_cache


# ---------------------------------------------------------------------------- which home is this?

def kind(config):
    """The home this repo's runner lives in — ``PANE`` or ``LOGIN_ITEM``.

    Fail-closed to ``PANE`` for anything unreadable, and the direction matters: the pane home's
    preflight REFUSES to start without a resolvable anchor, so a garbled config produces a loud
    refusal. Defaulting the other way would produce a runner that quietly believes it needs no
    anchor and then births every worker into nothing.
    """
    value = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    return value if value in KINDS else PANE


def restart_mechanism(home):
    """How a Restart request is EXECUTED in this home (issue #116's disposition).

    ``PANE`` → ``REEXEC``. ``os.execv`` is load-bearing there because it PRESERVES the pid, so the
    runner stays the foreground process of its own tab; a new pid would orphan from the tab and the
    shell would fall back to its prompt.

    ``LOGIN_ITEM`` → ``EXIT_TO_SUPERVISOR``. The tab was the entire reason for an in-place image
    swap. With no tab, the honest restart is a clean exit: the supervisor starts a fresh process
    that reloads the installed engine and clears in-memory episode state — the same two things the
    exec bought — without the singleton-adoption dance the exec needed to survive its own pid.
    """
    return EXIT_TO_SUPERVISOR if home == LOGIN_ITEM else REEXEC


# ---------------------------------------------------------------------------- addressing the job

def _slug_parts(repo_slug, job):
    """``(owner, name)`` from an ``owner/name`` slug, or ValueError naming which job wanted it.

    Raises rather than sanitizing: a label is how every later verb (bootout, kickstart, print)
    addresses a job, and a quietly-repaired one would address a different job — or none.
    """
    if not isinstance(repo_slug, str) or repo_slug.count("/") != 1:
        raise ValueError("a %s label needs an owner/name slug, got %r" % (job, repo_slug))
    owner, name = repo_slug.split("/", 1)
    if not owner or not name:
        raise ValueError("a %s label needs an owner/name slug, got %r" % (job, repo_slug))
    return owner, name


def label(repo_slug):
    """The LaunchAgent label for a repo's runner, or ValueError."""
    return _LABEL_FMT % _slug_parts(repo_slug, "runner")


def watchdog_label(repo_slug):
    """The LaunchAgent label for a repo's WATCHDOG job, or ValueError (issue #328).

    A separate function rather than a ``job=`` parameter on ``label``: every caller of ``label``
    means the runner, and a defaulted parameter is how one of them one day addresses the wrong job
    while reading as though it addressed the right one.
    """
    return _WATCHDOG_LABEL_FMT % _slug_parts(repo_slug, "watchdog")


def domain(uid):
    """The ONE launchd domain this home may live in: ``gui/<uid>``.

    Spelled once, and taken by nothing as a parameter (see the module docstring). The keychain rule
    is only worth anything if it cannot be argued with at a call site.
    """
    return "gui/%d" % int(uid)


def service_target(uid, job_label):
    return "%s/%s" % (domain(uid), job_label)


def bootstrap_argv(launchctl, uid, plist_path):
    """Load the job into the gui domain. `bootstrap` (not the deprecated `load`) so a failure is
    reported against a named domain rather than swallowed."""
    return [launchctl, "bootstrap", domain(uid), os.fspath(plist_path)]


def bootout_argv(launchctl, uid, job_label):
    return [launchctl, "bootout", service_target(uid, job_label)]


def kickstart_argv(launchctl, uid, job_label):
    """Start the job, restarting it if it is already running (``-k``).

    ``-k`` is the load-bearing flag: the case this exists for is a runner that is wedged or gone
    while its job is still loaded, and a plain kickstart would leave a wedged one exactly where it
    was.
    """
    return [launchctl, "kickstart", "-k", service_target(uid, job_label)]


def print_argv(launchctl, uid, job_label):
    return [launchctl, "print", service_target(uid, job_label)]


def service_is_idle(text):
    """Did the service EXPLICITLY report a not-running state? True / False / None (unreadable).

    Split out from ``service_pid`` after a fresh-agent review: "no pid" and "the service says it is
    not running" are different facts, and treating the first as the second turns a changed output
    shape, a truncated read, or any other unreadable answer into a benign-looking "loaded but idle".
    A caller that must fail closed needs to tell them apart, so it gets a positive read or nothing.
    """
    match = _STATE_LINE.search(text or "")
    if not match:
        return None
    state = " ".join(match.group(1).split()).lower()
    if state in _IDLE_STATES:
        return True
    if state in _RUNNING_STATES:
        return False
    return None


def service_pid(text):
    """The pid ``launchctl print`` reports for a service, or None.

    None means UNKNOWN — the service is absent, loaded-but-not-running, or the output changed
    shape — and every caller here fails closed on it. There is deliberately no fallback guess: a
    guessed pid is a pid something later compares, reports, or (elsewhere in this engine) signals.
    """
    match = _PID_LINE.search(text or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------- PATH

def launch_path(dirs, base=LAUNCHD_PATH):
    """The explicit PATH the rendered plist carries: the dirs the required commands resolved in,
    prepended to what launchd would have given anyway.

    Order-preserving and de-duplicated. Prepending (rather than replacing) keeps the system
    binaries the runner also uses — ``pgrep``, ``ps``, ``pmset``, ``osascript`` — reachable without
    naming every one of them here.
    """
    out = []
    for entry in list(dirs) + str(base).split(":"):
        entry = (entry or "").strip()
        if entry and entry not in out:
            out.append(entry)
    return ":".join(out)


def resolve_commands(which, commands=REQUIRED_COMMANDS):
    """``(found, missing)`` for the commands the runner shells by bare name.

    ``which`` is injected (``shutil.which``, or a probe's resolver) so this stays pure and so a
    test never resolves against the host's real PATH.
    """
    found, missing = {}, []
    for name in commands:
        path = which(name)
        if path:
            found[name] = path
        else:
            missing.append(name)
    return found, missing


# ---------------------------------------------------------------------------- the boot preflight

def preflight(*, manager, missing, gh_ok, host_answered):
    """``(ok, why)`` for a login-item runner about to start — the port of the pane home's D7 rule.

    Every problem is reported at once rather than one per restart: this is a message an operator
    reads in a log after the fact, and a refusal that names one of three faults teaches them to fix
    it and be refused again.

    ``manager``      what ``launchctl managername`` said (``Aqua`` is the only pass).
    ``missing``      required commands that did not resolve on PATH.
    ``gh_ok``        True / False / None — whether the keychain-backed GitHub token read. None is
                     UNKNOWN and is NOT a pass: a runner whose identity cannot be proven burns
                     every issue's retry cap confidently.
    ``host_answered``True / False / None — whether the session host's server answered THROUGH THE
                     WRAPPER. Same rule: only True passes.
    """
    problems = []
    if manager != AQUA:
        problems.append(
            "this process is not in the Aqua login session (`launchctl managername` said %r, "
            "expected %r) — load the runner as a user LaunchAgent in the gui/$UID domain, never "
            "system/: only the gui domain reads the login keychain, which gh and the notify "
            "channel both depend on" % (manager or "(unreadable)", AQUA))
    if missing:
        problems.append(
            "PATH does not resolve: %s. launchd hands a job only %s — no Homebrew, no ~/.local/bin "
            "— so the plist must set PATH explicitly (see `superlooper runner-home --install`, "
            "which records where these actually resolved)"
            % (", ".join(missing), LAUNCHD_PATH))
    # Skipped entirely when gh is one of the MISSING commands: the PATH problem above already names
    # it, and it is the actionable one. Saying the same fact twice in a message an operator reads
    # once is how a refusal starts getting skimmed (fresh-agent review).
    if gh_ok is not True and "gh" not in (missing or []):
        problems.append(
            "the keychain-backed gh login did not read from here (%s) — a runner that cannot prove "
            "its GitHub identity fails every read while looking perfectly alive"
            % ("gh reported no active login" if gh_ok is False else "gh could not be asked at all"))
    if host_answered is not True:
        problems.append(
            "the session host did not answer through the wrapper (%s) — every worker launch would "
            "fail and the whole queue would burn its retry caps, so this refuses to start instead"
            % ("no answer" if host_answered is None else "reachable but not answering"))
    if problems:
        return False, "the runner's login-item home is not usable: " + "; ".join(problems)
    return True, ("login-item home ready: Aqua session, PATH resolves %s, gh login reads, the "
                  "session host answers" % ", ".join(REQUIRED_COMMANDS))


# ---------------------------------------------------------------------------- the plist

def render_plist(*, label, superlooper_bin, repo_path, state_home, path, state_base=None):
    """The LaunchAgent plist for this repo's runner, as text.

    Absolute paths are REQUIRED, not normalised: launchd runs a job from ``/``, so a relative
    program or repo path resolves to nothing — and the failure surfaces as a job that starts and
    dies instantly into a log nobody is watching. Refusing at render time puts the error in front
    of the person running the installer.

    ``state_base`` is the operator's ``SL_HOME`` when — and only when — they have relocated the
    state base. Found by driving the real installer: without it, a job installed from a shell with a
    relocated base computes a DIFFERENT state home once launchd runs it, giving a second empty state
    home beside the real one while the job's own log still points at the first. The installer bakes
    the environment the job needs, and this is part of that environment exactly as much as PATH is.
    Left out when unset, because baking in the default would freeze a path the operator never chose.
    """
    for name, value in (("superlooper_bin", superlooper_bin), ("repo_path", repo_path),
                        ("state_home", state_home)):
        if not str(value).startswith("/"):
            raise ValueError("%s must be absolute — launchd runs the job from '/', so %r would "
                             "resolve to nothing" % (name, value))
    extra = ""
    if state_base:
        if not str(state_base).startswith("/"):
            raise ValueError("state_base must be absolute — launchd runs the job from '/', so %r "
                             "would resolve to nothing" % (state_base,))
        extra = "\n        <key>SL_HOME</key>\n        <string>%s</string>" % _xml(state_base)
    # Every substituted value lands in an XML TEXT NODE, so it is escaped (fresh-agent review). A
    # directory named `R&D` is enough to produce a plist launchd cannot parse, and that failure
    # surfaces as a job that silently never starts — the same defect family as the `--` in a comment
    # that installing this for real turned up, one layer down. `extra` is assembled above from
    # already-escaped parts and so is inserted as markup, not text.
    return (_template()
            .replace("{label}", _xml(label))
            .replace("{superlooper_bin}", _xml(superlooper_bin))
            .replace("{repo_path}", _xml(repo_path))
            .replace("{state_home}", _xml(state_home))
            .replace("{path}", _xml(path))
            .replace("{extra_env}", extra))
