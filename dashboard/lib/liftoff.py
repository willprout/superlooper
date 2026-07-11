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
"""
import os


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
