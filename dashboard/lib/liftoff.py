"""Liftoff (issue #45) — the pure decision core behind the ONE command that brings up the pair.

Today the operator starts the runner and the dashboard separately. ``bin/liftoff`` is the single
documented command that starts — or verifies already-running — BOTH: this dashboard and one watched
repo's runner. It always starts the dashboard in the background (a localhost server needs no tab);
what it does about the RUNNER depends on where that repo's runner lives (``runner_home``, issue
#306, wired here by #310):

* ``pane`` (the default) — run liftoff BY HAND in a session tab, exactly like ``superlooper run``
  itself: it FOREGROUNDS the runner in the current tab, so the runner lands in a visible tab — the
  one proven restart procedure (see the engine's runner-ops.md). Automated tab placement stays out;
  a human's real tab is the anchor.
* ``login-item`` — launchd owns the runner's process, so there is no tab to claim: liftoff runs the
  engine's own bootstrap step, reports both halves up, and hands the terminal back.

Two boundaries shape this file:

* **The engine stays dashboard-agnostic.** liftoff lives entirely on the dashboard side and shells
  the engine's OWN documented ``superlooper run``, reached through the dashboard config's
  ``superlooper_cli`` (the same generic contract the Tidy button uses, issue #41) — never a
  hardcoded engine path and never an engine that knows the dashboard exists.
* **Idempotent — a second invocation double-starts neither.** liftoff probes first: the dashboard's
  port (already bound ⇒ leave it) and the runner's pidfile (a live pid ⇒ leave it). The real
  backstops are the ones each side already owns — the dashboard's bind-failed guard (issue #34) and
  the runner's pidfile singleton — so even a racing probe is safe; the probe just makes the common
  case clean and quiet.

Everything here is pure: it takes already-read facts (config, probe results) and returns argvs,
resolved repos, and a plan. All real I/O (the socket probe, ``os.kill`` liveness, ``Popen``,
``execv``) lives in ``bin/liftoff``, which is the composition root.

**Missing-config errors ANNOUNCE, they never auto-switch (issue #104).** liftoff resolves
``./config.json`` against the directory you RUN it from, not the script's own location. The first
real run tripped on exactly that: run from the repo root, liftoff reported ``no dashboard config at
config.json`` (a bare relative path that named nowhere) and advised copying the example — while the
operator's config sat, already written, in the dashboard dir one directory over. ``missing_config_message``
is the honest replacement: it names the ABSOLUTE path liftoff checked and every way to point it right.
When a config already exists beside the script, the message NAMES it and how to select it but does
NOT silently adopt it — silently switching which config a label-writing dashboard watches is the
"quietly watch the wrong thing" failure ``lib/config.py`` is built to reject; naming the path and the
three ways out (run from its directory, pass it as an argument, or set ``$CC_CONFIG``) instead teaches
the operator liftoff's cwd-relative resolution, so the next run is right by understanding, not luck.
"""
import os
import re

# The literal "this is a command-center" claim a responder must carry before its pid is
# trusted as a kill target. Imported, never re-spelled: the process that WRITES the claim
# (lib/version) and the command that READS it must not be able to disagree about the word.
from version import PRODUCT

# ---------------------------------------------------------------- the runner's home (issue #306)
# The runner no longer always lives in the tab you run liftoff from. ``runner_home`` is a per-repo
# ENGINE config key, and liftoff must never read that file itself (the engine stays a black box we
# only shell) — it asks ``superlooper runner-home --repo <path>``, which reports the answer
# read-only, and matches the spelling here. Same two words the engine uses; a third would be a
# translation layer waiting to drift.
PANE = "pane"
LOGIN_ITEM = "login-item"
_HOMES = (PANE, LOGIN_ITEM)

# The report's first line: ``runner_home: <kind>`` (the pane form continues with an em-dash and a
# sentence, the login-item form with an indented job/domain/plist/live block — both start here).
_HOME_RE = re.compile(r"^runner_home:\s*(\S+)", re.M)


def parse_runner_home(stdout):
    """The runner home named by ``superlooper runner-home --repo <path>`` output, or ``None``.

    ``None`` covers three real cases and they all want the same answer: an engine too OLD to have
    the subcommand (argparse's usage error, no such line), an engine that could not run at all
    (empty output), and an engine reporting a home this build has never heard of. The caller then
    keeps the PANE behaviour, and that direction is chosen deliberately: guessing ``login-item``
    would make liftoff refuse to start the runner at all on a machine where the tab is the whole
    mechanism, whereas guessing ``pane`` at worst starts a runner in the tab — working, if not
    where a login-item owner wanted it. Pure: the subprocess lives in the composition root.
    """
    m = _HOME_RE.search(stdout or "")
    if not m:
        return None
    return m.group(1) if m.group(1) in _HOMES else None


def runner_home_argv(superlooper_cli, repo_path):
    """The engine's READ-ONLY home report: ``<superlooper_cli> runner-home --repo <path>``. Changes
    nothing — it is the probe liftoff runs before deciding what starting the runner even means."""
    return [superlooper_cli, "runner-home", "--repo", repo_path]


def runner_job_argv(superlooper_cli, repo_path):
    """The engine's own setup step for the login-item home: ``runner-home --repo <path> --install
    --load`` — render the LaunchAgent, place it, and bootstrap it (issue #306). Idempotent by
    construction (it boots the job out before bootstrapping it back in), which is what lets liftoff
    keep its never-double-start contract in this home without a second probe."""
    return [superlooper_cli, "runner-home", "--repo", repo_path, "--install", "--load"]


def missing_config_message(looked_at, *, script_dir_config=None, example_config=None):
    """The friendly, actionable error when liftoff's chosen config file does not exist (issue #104).

    All three inputs are already-resolved facts — the composition root does the disk checks; this
    stays pure — and every branch names the ABSOLUTE path liftoff looked at plus all three ways to
    point it right (run liftoff from the config's directory, pass the path as the first argument, or
    set ``$CC_CONFIG``), mirroring the plain, newline-terminated voice of the sibling command-center's
    friendly failures (issue #34).

    * ``looked_at`` — the absolute path liftoff resolved and found nothing at. Named first, so the
      reader learns *where* liftoff actually looked (it resolves a relative path against the directory
      you run it from, not the script's location — the exact thing that misled the first run).
    * ``script_dir_config`` — the absolute path of a config that sits beside the script
      (``<liftoff dir>/../config.json``) IF that file exists, else ``None``. When given (the live #104
      case), the message NAMES that found config and how to select it and, because a config already
      exists, OMITS the copy-the-example advice — but never silently adopts it (see the module
      docstring's rationale).
    * ``example_config`` — the absolute path of the shipped ``config.example.json`` IF it exists, else
      ``None``. Used only when no config exists anywhere obvious, to spell the exact ``cp`` first step.
    """
    lines = ["liftoff: no config at %s" % looked_at]
    if script_dir_config is not None:
        lines += [
            "  A config already exists beside liftoff, at %s — but liftoff looks for" % script_dir_config,
            "  ./config.json in the directory you run it FROM, not where the script lives. Use that",
            "  config any of three ways:",
            "    - cd to the dashboard directory, then run: bin/liftoff",
            "    - pass it as the first argument: liftoff %s" % script_dir_config,
            "    - point CC_CONFIG at it: export CC_CONFIG=%s" % script_dir_config,
        ]
    else:
        lines += [
            "  liftoff looks for ./config.json in the directory you run it FROM. Point it at your",
            "  config any of three ways:",
            "    - run liftoff from the directory that holds config.json",
            "    - pass it as the first argument: liftoff /path/to/config.json",
            "    - point CC_CONFIG at it: export CC_CONFIG=/path/to/config.json",
        ]
        if example_config is not None:
            target = os.path.join(os.path.dirname(example_config), "config.json")
            lines += [
                "  No config yet? Create one from the example:",
                "    cp %s %s" % (example_config, target),
            ]
    return "\n".join(lines) + "\n"


def resolve_repo(config, repo_arg):
    """The single watched repo whose runner ``liftoff`` should start. ``repo_arg`` (the ``--repo``
    value, or ``None``) may be a slug (``owner/name``), a bare repo name, or a checkout path.

    With no ``--repo`` and exactly one watched repo, that repo is the obvious target. With no
    ``--repo`` and several, or a ``--repo`` that matches none, raise ``ValueError`` naming the
    watched repos — liftoff steers exactly one runner, so the choice must be explicit, never guessed.
    """
    repos = config["repos"]
    slugs = ", ".join(r["slug"] for r in repos)
    if repo_arg is None:
        if len(repos) == 1:
            return repos[0]
        raise ValueError(
            "this config watches %d repos — name which runner to start with "
            "--repo <slug|name|path> (watched: %s)" % (len(repos), slugs))
    want = repo_arg.strip()
    want_path = os.path.abspath(os.path.expanduser(want))
    for r in repos:
        if want in (r["slug"], r.get("name")) or want_path == os.path.abspath(r["path"]):
            return r
    raise ValueError("--repo %r matches no watched repo (watched: %s)" % (repo_arg, slugs))


def runner_argv(superlooper_cli, repo_path):
    """The engine's OWN documented start, reached through the config contract: ``<superlooper_cli>
    run --repo <path>``. ``superlooper_cli`` is the dashboard config's ``superlooper_cli`` (issue
    #41's generic pointer at the installed engine) — NEVER a hardcoded engine path, so the engine
    stays a black box liftoff only shells."""
    return [superlooper_cli, "run", "--repo", repo_path]


def dashboard_argv(python_exe, command_center_path, config_path):
    """The dashboard server's own entry point, launched on the same interpreter that ran liftoff:
    ``<python> bin/command-center <config>``. Backgrounded by the composition root; here we only
    name the argv."""
    return [python_exe, command_center_path, config_path]


def runner_lock_pid(state_home):
    """The pid recorded in ``<state_home>/state/runner.lock`` (the runner's pidfile singleton), or
    ``None`` if the file is absent or unparseable. READ-ONLY — the same file the runner writes with
    ``str(os.getpid())`` and the same tolerance the runner uses reading it back. Liveness (is that
    pid actually alive?) is the composition root's ``os.kill`` call; this only reads the number."""
    path = os.path.join(os.fspath(state_home), "state", "runner.lock")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _identified_pid(version):
    """The pid a version block hands over as a kill target, or ``None``.

    Trusted ONLY when the responder also carries the ``product`` marker naming itself a
    command-center. A pid is just a number anything could print; paired with the explicit product
    claim it is the process asserting what it is. That pairing is what makes a SIGTERM safe here,
    and it has bitten this project before: a pattern kill (``pkill -f``) collateral-killed
    William's live dashboard (2026-07-07), and the port-holder is no safer.
    """
    if not isinstance(version, dict):
        return None
    pid = version.get("pid")
    # bool is an int in Python — screen it out explicitly, or `True` reads as pid 1.
    if (version.get("product") == PRODUCT
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        return pid
    return None


def dashboard_restart_decision(url, snapshot, port_busy=False, identity=None):
    """The pure decision behind ``liftoff --restart-dashboard`` (issue #136): what to do about a
    dashboard that is running an older build than the checkout on disk.

    This flag exists because liftoff's normal path is idempotent BY CONTRACT — it verifies an
    already-serving dashboard and leaves it alone — which is right for starting and useless for
    healing: a routine liftoff never clears the skew. And the remedy has to live here, in a command
    read fresh from disk, rather than in a dashboard button: a stale server is stale precisely
    because it lacks the newly merged routes, so a restart ENDPOINT would 404 on exactly the servers
    that need it.

    ``snapshot`` is the live dashboard's ``/api/snapshot`` (already probed and shape-verified by the
    composition root) or ``None`` if nothing of ours ANSWERED. ``port_busy`` is the kernel's separate,
    dumber answer to "is anything accepting TCP on that port": the two together are what let a silent
    port be told apart from an empty one. ``identity`` is the second, snapshot-free probe
    (``/api/version``, issue #471) — the version block a dashboard can still publish while its
    snapshot builder is crashing; consulted ONLY when the snapshot said nothing. Returns
    ``{action, pid, message}``:

    * ``start`` — the port is free and nothing is serving; just bring one up.
    * ``stop-then-start`` — stop ``pid``, wait for it to actually go, then start fresh. The pid is
      trusted ONLY when the responder also carries the ``product`` marker naming itself a
      command-center. A pid is just a number anything could print, and the snapshot's general shape
      (``generated_at`` + ``repos``) is a resemblance, not a proof — without the explicit claim, any
      localhost responder could aim a SIGTERM at a process of its choosing. A signal is the one
      irreversible thing done to another process here, and it has bitten this project before: a
      pattern kill (``pkill -f``) collateral-killed William's live dashboard (2026-07-07), and the
      port-holder is no safer — ``_dashboard_up``'s own contract admits an unrelated app can squat
      the port.
    * ``refuse`` — something is serving but will not identify itself as a command-center with a pid
      (a server predating this issue, or a stranger on the port) — and, for a silent port, did not
      identify itself on ``/api/version`` either. Guessing a kill target is exactly the failure
      above, so liftoff stops and tells the owner how to finish by hand.

    A dashboard that is already current still restarts: the flag is the owner's explicit act, not a
    repair the machine talks itself into. The message just says so.
    """
    if snapshot is None:
        if port_busy:
            # A wedged dashboard that can still SAY WHO IT IS (issue #471). The snapshot is built
            # from the watched repos' state homes and the identity from the checkout, so one can
            # crash while the other answers — which is exactly what happened on 2026-09-02: every
            # /api/snapshot 500'd on one repo's state, the port stayed held, and the refusal below
            # sent the owner to Ctrl-C a background process with no tab. The bar for WHOM to signal
            # is not lowered by a millimetre: still the process's own explicit product claim beside
            # its own pid. All that changed is that we now ask on a route the failure does not
            # silence, instead of concluding "unidentifiable" from one broken endpoint.
            pid = _identified_pid(identity)
            if pid is not None:
                return {"action": "stop-then-start", "pid": pid,
                        "message": ("the dashboard at %s is not answering /api/snapshot but still "
                                    "identifies itself (pid %d) — stopping it and starting a fresh "
                                    "one on the current build" % (url, pid))}
            # Silent is not empty. The snapshot probe answers None for a timeout or a truncated body
            # — exactly how a WEDGED BUT ALIVE dashboard looks, still holding the socket. Spawning
            # over that gives the owner a replacement that dies at bind while the stale server keeps
            # answering, and a liftoff that cheerfully reported success. We cannot identify it (it
            # never told us its pid), so we cannot stop it, so we start nothing.
            return {"action": "refuse", "pid": None,
                    "message": ("something is holding %s but will not answer as a command-center — "
                                "it may be a wedged dashboard, or another app on that port. liftoff "
                                "cannot identify it, so it will not signal it and will not start a "
                                "second dashboard beside it.\n"
                                "  Stop it by hand (Ctrl-C in the tab running it, or close that "
                                "tab), then run: liftoff --restart-dashboard" % url)}
        return {"action": "start", "pid": None,
                "message": "nothing is serving at %s — starting a fresh dashboard" % url}
    # A live snapshot decides on its OWN version block — one probe, one answer. The identity route
    # is a fallback for the case above (nothing answered here), never a second opinion that could
    # hand over a different pid than the process actually talking to us.
    version = (snapshot.get("version") or {}) if isinstance(snapshot, dict) else {}
    pid = _identified_pid(version)
    if pid is None:
        return {"action": "refuse", "pid": None,
                "message": ("something is serving at %s but does not identify itself as a "
                            "command-center with a pid — either it predates --restart-dashboard or "
                            "it is not ours. liftoff will not guess which process to signal.\n"
                            "  Stop it by hand (Ctrl-C in the tab running it, or close that tab), "
                            "then run: liftoff --restart-dashboard" % url)}
    was = "stale" if version.get("skew") else "already current"
    return {"action": "stop-then-start", "pid": pid,
            "message": "restarting the dashboard at %s (pid %d — its build is %s)" % (url, pid, was)}


def make_plan(repo, url, dashboard_argv_, runner_argv_, *, dashboard_up, runner_pid,
              runner_home=PANE):
    """The idempotent plan: what to start, what to leave, and the plain line to print for each.

    ``dashboard_up`` is the port probe (already serving?); ``runner_pid`` is the LIVE runner pid or
    ``None`` (the pidfile read + liveness check). Neither half is ever double-started: an up
    dashboard and a live runner each resolve to ``start: False`` with a "leaving it" line. The
    dashboard is always a background server.

    ``runner_home`` (issue #306, wired here by issue #310) decides what STARTING the runner means,
    and it changes exactly one thing: whether liftoff claims this tab.

    * ``PANE`` — the runner's pane IS the launch anchor, so liftoff foregrounds it here and the
      process it exec's becomes the runner. Unchanged, and still the default: a repo that never set
      the key behaves exactly as it did before this issue.
    * ``LOGIN_ITEM`` — launchd owns the runner's process, so there is nothing to foreground. The
      "start" is the engine's own bootstrap step, run to completion like any other command, and
      liftoff returns to the shell afterwards with both halves up. Claiming the tab here would be
      worse than pointless: it would start a SECOND runner outside the home its owner chose.

    ``runner_argv_`` is the argv for the home already selected by the composition root (it knows the
    home; this stays pure), so this function decides start/leave, foreground, and the plain line.
    """
    if dashboard_up:
        dashboard = {"start": False, "foreground": False,
                     "message": "dashboard already serving at %s — leaving it" % url}
    else:
        dashboard = {"start": True, "foreground": False, "argv": list(dashboard_argv_),
                     "message": "starting the dashboard → %s" % url}
    # Liveness is home-independent — the runner writes the same pidfile wherever it lives — so the
    # never-double-start half of the contract needed no change at all for issue #306.
    foreground = runner_home != LOGIN_ITEM
    if runner_pid is not None:
        runner = {"start": False, "foreground": foreground, "pid": runner_pid,
                  "message": "runner already running for %s (pid %d) — leaving it"
                             % (repo["slug"], runner_pid)}
    elif foreground:
        runner = {"start": True, "foreground": True, "argv": list(runner_argv_), "pid": None,
                  "message": "starting the runner for %s in this tab" % repo["slug"]}
    else:
        # Says what is being ATTEMPTED, not what will be true afterwards: the bootstrap can refuse
        # (a PATH it cannot honestly record, a launchctl that would not bootstrap), and a line that
        # promised "launchd keeps it running" followed by its own retraction is the 3am-readability
        # failure this migration exists to end. The composition root prints the outcome after.
        runner = {"start": True, "foreground": False, "argv": list(runner_argv_), "pid": None,
                  "message": "setting up the runner for %s as its login item — launchd will own "
                             "the process, so this terminal stays yours" % repo["slug"]}
    return {"dashboard": dashboard, "runner": runner}
