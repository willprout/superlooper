"""Liftoff (issue #45) — the pure decision core behind the ONE command that brings up the pair.

Today the operator starts the runner and the dashboard separately. ``bin/liftoff`` is the single
documented command that starts — or verifies already-running — BOTH: this dashboard and one watched
repo's runner. It is run BY HAND inside a cmux tab, exactly like ``superlooper run`` itself: it
starts the dashboard in the background (a localhost server needs no tab) and then FOREGROUNDS the
runner in the current tab, so the runner lands in a visible cmux tab — the one proven restart
procedure (see the engine's runner-ops.md). Automated tab placement stays out; a human's real tab
is the anchor.

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


def dashboard_restart_decision(url, snapshot):
    """The pure decision behind ``liftoff --restart-dashboard`` (issue #136): what to do about a
    dashboard that is running an older build than the checkout on disk.

    This flag exists because liftoff's normal path is idempotent BY CONTRACT — it verifies an
    already-serving dashboard and leaves it alone — which is right for starting and useless for
    healing: a routine liftoff never clears the skew. And the remedy has to live here, in a command
    read fresh from disk, rather than in a dashboard button: a stale server is stale precisely
    because it lacks the newly merged routes, so a restart ENDPOINT would 404 on exactly the servers
    that need it.

    ``snapshot`` is the live dashboard's ``/api/snapshot`` (already probed and shape-verified by the
    composition root) or ``None`` if nothing of ours is serving. Returns ``{action, pid, message}``:

    * ``start`` — nothing is serving; just bring one up.
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
      (a server predating this issue, or a stranger on the port). Guessing a kill target is exactly
      the failure above, so liftoff stops and tells the owner how to finish by hand.

    A dashboard that is already current still restarts: the flag is the owner's explicit act, not a
    repair the machine talks itself into. The message just says so.
    """
    if snapshot is None:
        return {"action": "start", "pid": None,
                "message": "nothing is serving at %s — starting a fresh dashboard" % url}
    version = (snapshot.get("version") or {}) if isinstance(snapshot, dict) else {}
    pid = version.get("pid")
    # bool is an int in Python — screen it out explicitly, or `True` reads as pid 1.
    identified = (version.get("product") == "command-center"
                  and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0)
    if not identified:
        return {"action": "refuse", "pid": None,
                "message": ("something is serving at %s but does not identify itself as a "
                            "command-center with a pid — either it predates --restart-dashboard or "
                            "it is not ours. liftoff will not guess which process to signal.\n"
                            "  Stop it by hand (Ctrl-C in the tab running it, or close that tab), "
                            "then run: liftoff --restart-dashboard" % url)}
    was = "stale" if version.get("skew") else "already current"
    return {"action": "stop-then-start", "pid": pid,
            "message": "restarting the dashboard at %s (pid %d — its build is %s)" % (url, pid, was)}


def make_plan(repo, url, dashboard_argv_, runner_argv_, *, dashboard_up, runner_pid):
    """The idempotent plan: what to start, what to leave, and the plain line to print for each.

    ``dashboard_up`` is the port probe (already serving?); ``runner_pid`` is the LIVE runner pid or
    ``None`` (the pidfile read + liveness check). Neither half is ever double-started: an up
    dashboard and a live runner each resolve to ``start: False`` with a "leaving it" line. Only the
    runner half is ever run in the FOREGROUND (``foreground: True``) — the dashboard is always a
    background server.
    """
    if dashboard_up:
        dashboard = {"start": False, "foreground": False,
                     "message": "dashboard already serving at %s — leaving it" % url}
    else:
        dashboard = {"start": True, "foreground": False, "argv": list(dashboard_argv_),
                     "message": "starting the dashboard → %s" % url}
    if runner_pid is not None:
        runner = {"start": False, "foreground": True, "pid": runner_pid,
                  "message": "runner already running for %s (pid %d) — leaving it"
                             % (repo["slug"], runner_pid)}
    else:
        runner = {"start": True, "foreground": True, "argv": list(runner_argv_), "pid": None,
                  "message": "starting the runner for %s in this cmux tab" % repo["slug"]}
    return {"dashboard": dashboard, "runner": runner}
