#!/usr/bin/env python3
"""Mechanical launch of ONE session — the single spawn path (issue #308).

Three modes — the first two unchanged from the cmux launcher this replaces:

    launch-session.py <i-id>                worker:   worktree off origin/<dev>, then launch
    launch-session.py --cwd <dir> <d-id>    debugger: launch in an existing dir (no worktree)
    launch-session.py --triage <t-id>       triage:   the flight (#448) — the repo's own checkout,
                                                      or SL_TRIAGE_HOME=worktree for a detached one

The triage mode takes a FLAG rather than being inferred from the id, because its default home
creates nothing and takes no directory — so on the old "``--cwd`` selects the mode" rule it would
be indistinguishable from a worker launch, and would be run as one. Where it runs is an SL_*
variable like every other knob, resolved from the repo's `triage.home` by whoever calls this.

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


_USAGE = ("usage: launch-session.py <i-id>  (or --cwd <dir> <d-id>, or --triage <t-id>)")


def _spec(argv):
    """Parse the three modes into a Spec, or return None (usage went to stderr)."""
    cwd = None
    mode = ""
    if argv[:1] == ["--cwd"]:
        if len(argv) < 3:
            print("usage: launch-session.py --cwd <dir> <d-id>", file=sys.stderr)
            return None
        cwd, iid = argv[1], argv[2]
    elif argv[:1] == ["--triage"]:
        if len(argv) < 2 or not argv[1]:
            print("usage: launch-session.py --triage <t-id>", file=sys.stderr)
            return None
        mode, iid = launch.TRIAGE, argv[1]
    elif len(argv) >= 1 and argv[0]:
        iid = argv[0]
    else:
        print(_USAGE, file=sys.stderr)
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
        mode=mode,
        # PASSED THROUGH, never defaulted here: an unreadable value must reach lib/launch, which
        # REFUSES rather than choosing a home — the two homes see different repositories, so a
        # quiet fallback in the process boundary would launch the flight against the wrong one.
        # Empty means "say nothing", and the Spec default (checkout) then stands.
        triage_home=env.get("SL_TRIAGE_HOME") or launch.Spec.triage_home,
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
        # #314. None means "read this machine's own assignment (SL_FLEET_CLAUDE_CONFIG_DIR) in
        # lib/launch, where the canonical string is derived ONCE for every spawn path" — the same
        # shape as expect_gh_login above, and for the same reason: one derivation, or two spellings
        # of one directory become two credential namespaces.
        claude_config_dir=None,
        expect_claude_account=env.get("SL_EXPECT_CLAUDE_ACCOUNT", ""),
        verify_seconds=_int(env.get("SL_LAUNCH_VERIFY_SECONDS"), launch._VERIFY_SECONDS),
        codex={"dangerous_bypass": env.get("SL_CODEX_DANGEROUS_BYPASS", ""),
               "bypass_hook_trust": env.get("SL_CODEX_BYPASS_HOOK_TRUST", ""),
               "no_alt_screen": env.get("SL_CODEX_NO_ALT_SCREEN", "")},
        # NOTHING is forwarded, which is what the cmux launcher did too. A pane's shell is spawned
        # by the host and sources the operator's own rc files, so this was never how a variable
        # reaches a session — and PATH in particular is the one variable most able to change which
        # binaries a session resolves, including the third rung of the #303 claude ladder. The
        # parameter stays because `pane_environment` is what scrubs it, and a caller that has a
        # reason to offer something should have to say so.
    )


def main(argv):
    # ---- ONE variable of the launch-floor scrub belongs on THIS side too (issue #301) ----------
    # The floor itself lives in start-session.sh — it is the only code that runs in the session's
    # own environment, and the pane's shell sources the operator's rc files AFTER this process has
    # finished, so a scrub here would prove nothing about the worker's env.
    #
    # XDG_CONFIG_HOME is the exception, for two mechanical reasons rather than tidiness:
    #
    #   * `gh` resolves its config dir from it, and the worker's floor removes it. If this side
    #     kept an inherited value while the worker dropped it, the two `gh api user` reads would
    #     consult DIFFERENT config dirs — and #299 COMPARES their answers, so every launch would
    #     refuse with a mismatch that names no real fault (a held queue, or a park per issue).
    #   * the session host's own CLI resolves its socket from it too — observed while driving this
    #     launcher against a live server (reports/i308.md): with it set, `workspace create` fails
    #     with a bare NotFound and the launch reads as a delivery-channel fault.
    #
    # Process-wide, exactly as the cmux launcher had it, so every child this launcher spawns — the
    # gh probe, git, pretrust, the host — agrees about where config lives. `identity_probe_env` is
    # the belt to this braces: it keeps the guarantee for an in-process caller too.
    os.environ.pop("XDG_CONFIG_HOME", None)
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
