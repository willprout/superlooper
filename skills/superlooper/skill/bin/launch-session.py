#!/usr/bin/env python3
"""Mechanical launch of ONE session — the single spawn path (issue #308).

Two modes, unchanged from the cmux launcher this replaces:

    launch-session.py <i-id>                worker:   worktree off origin/<dev>, then launch
    launch-session.py --cwd <dir> <d-id>    debugger: launch in an existing dir (no worktree)

The decision core is ``lib/launch.py``; this is the process boundary around it, so the runner,
the watchdog, the dashboard Fixer (through ``superlooper debug``) and ``superlooper resume`` all
drive ONE launcher with one exit-code contract that ``lib/evidence.py`` reads. Every knob arrives
as an ``SL_*`` variable exactly as before, because the callers are subprocesses that share nothing
else — and because keeping the contract identical is what let the three spawners move in one wave
instead of three.

What changed underneath: the tab is now created through the five-verb wrapper (``session_host``,
issue #304) rather than by driving cmux directly. Nothing else moved — the pre-flight half (the
base-ref check with its distinct exit 3, worktree creation and its preservation rule, pretrust,
the session id, the brief, run-state hygiene) and the in-pane floor (``start-session.sh``) are the
same code doing the same work.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))

import launch                                                            # noqa: E402


def _truthy(value):
    return str(value or "").strip() in ("1", "true", "TRUE", "yes", "YES", "on", "ON")


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _spec(argv):
    """Parse the two modes into a Spec, or return None (usage went to stderr)."""
    cwd = None
    if argv[:1] == ["--cwd"]:
        if len(argv) < 3:
            print("usage: launch-session.py --cwd <dir> <d-id>", file=sys.stderr)
            return None
        cwd, iid = argv[1], argv[2]
    elif len(argv) >= 1 and argv[0]:
        iid = argv[0]
    else:
        print("usage: launch-session.py <i-id>  (or --cwd <dir> <d-id>)", file=sys.stderr)
        return None

    env = os.environ
    run_root = env.get("SL_RUN_ROOT")
    if not run_root:
        print("SL_RUN_ROOT required", file=sys.stderr)
        return None
    # SL_REPO / SL_DEV_BRANCH are the WORKER path's inputs; the --cwd path resolves neither, so
    # they stay optional here and lib/launch refuses a worker launch that lacks them.
    return launch.Spec(
        id=iid,
        run_root=run_root,
        repo=env.get("SL_REPO", ""),
        dev_branch=env.get("SL_DEV_BRANCH", "main"),
        cwd=cwd,
        engine_bin=_HERE,
        agent=env.get("SL_AGENT") or "claude",
        model=env.get("SL_MODEL", ""),
        effort=env.get("SL_EFFORT", ""),
        attended=_truthy(env.get("SL_ATTENDED")),
        resume_session_id=env.get("SL_RESUME_SESSION_ID", ""),
        # Empty means "resolve it here, in the runner's own environment" — which is what every
        # caller pins it to, precisely so a stale second-hand answer can never be trusted (#299).
        expect_gh_login=env.get("SL_EXPECT_GH_LOGIN") or None,
        gh_bin=env.get("SL_GH", ""),
        claude_bin=env.get("SL_CLAUDE", ""),
        verify_seconds=_int(env.get("SL_LAUNCH_VERIFY_SECONDS"), launch._VERIFY_SECONDS),
        codex={"dangerous_bypass": env.get("SL_CODEX_DANGEROUS_BYPASS", ""),
               "bypass_hook_trust": env.get("SL_CODEX_BYPASS_HOOK_TRUST", ""),
               "no_alt_screen": env.get("SL_CODEX_NO_ALT_SCREEN", "")},
        # The pane's shell inherits the HOST's environment, not ours, so this is not how a variable
        # reaches a session — it is what the launcher OFFERS, already scrubbed by pane_environment.
        # PATH rides so a pinned engine layout survives; nothing else is assumed.
        forwarded_env={k: v for k, v in env.items() if k in ("PATH", "TMPDIR", "LANG")},
    )


def main(argv):
    spec = _spec(argv)
    if spec is None:
        return 1
    result = launch.launch(spec)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
