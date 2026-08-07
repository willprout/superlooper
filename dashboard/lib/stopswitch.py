"""The Stop/Start verb (issue #365) — a LOCAL COMMAND execution over the engine's deliberate off
switch (issue #239). The dashboard's FOURTH button in that class, after Tidy, Restart and the
Janitor, and a direct sibling of ``lib/restart.py``.

Every GitHub-write button lives in ``lib/actions.py``. This one, like its siblings, shells the local
``superlooper`` CLI instead — here ``superlooper stop`` and ``superlooper start`` — and it owns none
of the mechanism: the marker, the supervisor hold, the pid corroboration, the guardians standing
down all live in the engine, which is the only place that knows what a runner home even is. This
adapter runs the verb, relays what it said, and refuses to embellish it.

**Why the off switch needs a button at all.** The 2026-07-13 owner ruling gives the command center
full parity with every owner-facing verb; the terminal is for the initial launch and nothing else.
Stop is the verb that ruling most obviously reaches, because the state it produces is the one the
owner is most likely to want from a screen rather than a shell — off for the night.

**What makes it different from Restart.** Restart asks a runner that is UP to come back; this one
takes it DOWN, so its failure modes are asymmetric and every single one has to survive to the
screen intact:

* **The engine has no preflight for this.** ``request-restart --check`` exists and ``stop`` has no
  equivalent, deliberately — a stop is recorded BEFORE anything can die, so there is no honest
  "would this work?" to ask that does not already change the answer. The dialog is therefore a
  single confirm-gated step, and the confirm is the gate the DoD names. (The board already tells
  the owner whether a runner is live; the dialog does not need to re-ask.)
* **A stop that did not take is a well-formed body at a NONZERO exit** — launchd still holds the
  job, or something alive holds a pid the runner's own record does not claim. So this parses the
  CLI's JSON FIRST (``lib/restart``'s posture) and only falls back to an rc-based error when stdout
  carries no object at all. A refusal is a truthful result, never a generic crash.
* **A stop that HALF took exits ZERO.** The job is gone and the runner is finishing its tick: the
  engine calls that the designed clean stop and it is right to. But "the runner is stopped" would
  be a lie about a process that is still merging, so :func:`summarize` reports what is true — the
  stop is recorded, and nothing will start it again.
* **``start`` cannot always start.** In the pane home a runner must live in a session window a
  human opened (automated placement is owner-ruled out, 2026-07-09), so ``start`` there hands back
  the one line to type and DELIBERATELY leaves the recorded stop standing. ``started`` — not ``ok``
  — is the field a UI branches on, and a summary that conflated them would strand the owner.

**Semantics here, pixels downstream (design record B.1).** :func:`summarize` turns the CLI's JSON
into the sentences the dialog shows, in tested Python, so the browser composes no claim about
whether the loop is running. It is also TOTAL: an engine republished with a shape this build has
not met degrades to an honest, non-committal summary rather than raising into a request thread.

**A WATCHED repo only.** Every invocation is gated on the allow-list mapping each configured repo
slug to its checkout path; a stray or forged request for an unwatched repo is refused BEFORE any
subprocess runs — the same bright line ``Actions``, ``Tidy`` and ``Restart`` draw.

The CLI to run is the CONFIGURED path (config's ``superlooper_cli``), with ``SL_SUPERLOOPER``
overriding it exactly so ``tests/conftest.py`` can point every test at an absent binary and a stop
test can inject the fake in-body. Same precedence as Tidy/Restart/Janitor/Fixer.
"""
import json
import os
import subprocess

# Per-call hard timeout (seconds). Deliberately far longer than Restart's 30: `stop` budgets a whole
# take-down (~30s for the process to leave its tick) on TOP of a launchctl boot-out that blocks
# through the teardown and carries its own 30s window, and `start` waits for a bootstrapped job to
# report a pid. A timeout shorter than the verb's own budget would report "timed out" over a stop
# that was working — the one failure message that would send an owner to kill things by hand.
_DEFAULT_TIMEOUT = 90

# The engine's own words for what launchd is doing with the job, keyed by the CLI's `job_state`.
# Copied from `superlooper`'s `_emit_stop` rather than paraphrased: the terminal and the dialog
# describe the same event, and two wordings for one fact is how a surface starts lying slowly.
_JOB_STATE = {
    "gone": "%s is booted out of %s — launchd has nothing to restart until your next login",
    "tearing_down": "launchd is still tearing %s out of %s — it is on its way out, and a job being "
                    "removed is not a job that gets restarted",
    "loaded": "%s is STILL loaded in %s — this stop did not hold it",
    "unconfirmed": "the boot-out of %s from %s was accepted, but launchctl could not be read back "
                   "to confirm the job is gone",
}


def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured path
    (config's ``superlooper_cli``), mirroring ``lib/restart``/``lib/tidy``'s precedence so the entry
    point and the tests agree on binary resolution."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, timeout=None):
    """Run ``<binary> <args>``; returns ``(rc, stdout, stderr)``. NEVER raises: a timeout, a missing
    binary, or any OSError is caught and returned as a nonzero rc with empty stdout so the caller
    fails closed (mirrors ``restart._run``)."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


def parse_result(stdout):
    """The single JSON object ``superlooper stop|start --json`` prints, or ``None`` when stdout
    carries no parseable object (a missing/crashed CLI). Pure and unit-tested, so the coupling to
    the CLI's ``--json`` contract is pinned by a test rather than discovered in production."""
    txt = (stdout or "").strip()
    if not txt:
        return None
    try:
        val = json.loads(txt)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _error(verb, rc, stderr, binary):
    """A plain, honest failure message for a CLI that didn't answer — what the UI shows instead of a
    fake success. Names the CLI on a missing binary so the operator knows exactly what to fix."""
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "superlooper %s timed out" % verb
    return stderr or ("superlooper %s failed (exit %d)" % (verb, rc))


# =============================== the summary (design record B.1) ===============================

def _summary(level, headline, lines=None, remedy=None, stopped=False, started=False):
    """One shape for every outcome, so the dialog has exactly ONE render path.

    ``stopped`` and ``started`` are the two claims the surface is not allowed to get wrong, so they
    are booleans derived here rather than inferred downstream from ``ok`` — which is a different
    question in both directions: a stop can succeed without the runner being gone yet, and a start
    can succeed having started nothing at all. Both keys ride on every summary (never only the
    relevant one) so no consumer ever reads an absent key as false by accident."""
    return {"level": level, "headline": headline, "lines": list(lines or []), "remedy": remedy,
            "stopped": bool(stopped), "started": bool(started)}


def _job_line(result):
    """launchd's side of a stop, in the engine's words — or ``None`` when there is no job to speak
    of (the pane home has no supervisor, so there is nothing to hold and nothing to report)."""
    job, domain = result.get("job"), result.get("domain")
    if not job:
        return None
    template = _JOB_STATE.get(result.get("job_state"))
    return (template % (job, domain)) if template else None


def _stop_process_line(result):
    """What became of the runner PROCESS — the sentence that decides whether the owner should
    expect the loop to be gone now or shortly."""
    if not result.get("was_running"):
        return ("no runner was live for this repo — the stop is recorded anyway, which is the half "
                "that keeps it down")
    pid = result.get("pid")
    if result.get("process_gone"):
        return "pid %s is gone" % pid
    if result.get("signal_refused"):
        return "pid %s was NOT signalled — %s" % (pid, result["signal_refused"])
    return ("pid %s is still finishing its current tick — it stops at the end of it, and nothing "
            "will start it again" % pid)


def summarize_stop(result):
    """The Stop dialog's whole result, from the CLI's body.

    The honesty rule it exists to enforce: ``stopped`` is True only when the runner is provably
    GONE. ``ok`` is not that claim — the engine exits zero for a stop that is recorded and holding
    while the runner finishes a tick, which is the right verdict on the VERB and the wrong headline
    for the owner. Two facts, two fields."""
    if not result.get("ok"):
        lines = []
        if result.get("marker"):
            # The record OUTLIVES the failed stop, and saying so is the difference between a useful
            # failure and an alarming one — but ONLY when a marker actually landed. On the path
            # where the marker itself could not be written, nothing was recorded and nothing was
            # taken down, and claiming otherwise would be its own fabrication.
            lines.append("the stop is still RECORDED (%s): a runner that is still running is still "
                         "watched, but once this one does go down — for any reason — the record "
                         "stands and the watchdog will not restart it. `superlooper start` "
                         "withdraws it." % result["marker"])
        if result.get("job_state") == "gone":
            # The half that DID work: a stop that booted the job out and then declined to signal a
            # pid is not the same failure as one that changed nothing.
            lines.append("the job %s IS booted out of %s — launchd is not the problem here."
                         % (result.get("job"), result.get("domain")))
        return _summary("err", "STOP INCOMPLETE — %s" % (result.get("error")
                                                         or "the engine did not say why"),
                        lines, remedy=result.get("remedy"))

    gone = bool(result.get("process_gone"))
    lines = []
    if result.get("marker"):
        lines.append("recorded at %s — deliberate, so the watchdog stands down instead of "
                     "resurrecting the loop" % result["marker"])
    job = _job_line(result)
    if job:
        lines.append(job)
    lines.append(_stop_process_line(result))
    if result.get("start"):
        lines.append("start it again with: %s" % result["start"])
    return _summary("ok",
                    "the runner is stopped." if gone
                    else "stop recorded — the runner has not gone yet.",
                    lines, stopped=gone)


def summarize_start(result):
    """The Start dialog's whole result, from the CLI's body.

    ``started`` mirrors the engine's own field and is the only thing the surface may call a start:
    True when something is PROVABLY running. A pane-home answer is ``ok`` and starts nothing, so it
    is reported as the warning it is, with the engine's manual line and the standing stop both
    carried through — a "started" over a loop that is still off is how an owner ends up staring at
    a dead board waiting for work that will never launch."""
    if not result.get("ok"):
        return _summary("err", "START FAILED — %s" % (result.get("error")
                                                      or "the engine did not say why"),
                        remedy=result.get("remedy"))

    lines = []
    if result.get("cleared"):
        lines.append("the deliberate stop is cleared — the watchdog watches this runner again.")
    if not result.get("started"):
        # Nothing came up. The manual line is the ENGINE's (it is the only side that knows this
        # repo's home), relayed verbatim rather than authored here — which is also what keeps this
        # module free of any opinion about what a session window is.
        if result.get("manual"):
            lines.append(result["manual"])
        if not result.get("cleared"):
            lines.append("the recorded stop still stands — it is still true until a runner boots, "
                         "and the runner clears it itself the moment one does.")
        home_note = ("a runner in this home must live in a session window you open yourself"
                     if result.get("home") == "pane" else "the loop is still off")
        return _summary("warn", "nothing was started — %s." % home_note, lines)

    if result.get("already_running") and result.get("job_loaded"):
        lines.append("a runner was already live for this repo (pid %s) and its job is loaded in "
                     "%s — nothing to start." % (result.get("pid"), result.get("domain")))
    else:
        if result.get("job"):
            lines.append("%s bootstrapped into %s%s"
                         % (result["job"], result.get("domain"),
                            " (its runner was already live — launchd had lost the job)"
                            if result.get("already_running") else ""))
        lines.append("pid %s" % result["pid"] if result.get("pid") else
                     "the job is loaded; it had not reported a pid yet — check `superlooper "
                     "status` in a moment")
    if result.get("manual"):
        lines.append(result["manual"])
    return _summary("ok", "the runner is running again.", lines, started=True)


def summarize(result):
    """Dispatch on the result's ``verb``. TOTAL by construction: a body from an engine this build
    has never met — or no body at all — becomes an honest non-answer, because the alternative is a
    KeyError inside the request thread and a dialog that says nothing while the loop's state is
    exactly what the owner came to find out."""
    if not isinstance(result, dict):
        return _summary("err", "the engine's answer could not be understood.")
    verb = result.get("verb")
    if verb == "stop":
        return summarize_stop(result)
    if verb == "start":
        return summarize_start(result)
    return _summary("err", "the engine answered with no verb this dashboard recognises — "
                           "the loop's state is unchanged as far as this button can tell.",
                    remedy=result.get("remedy"))


class StopSwitch:
    """The Stop/Start verb, bound to the configured superlooper CLI path, an allow-list mapping each
    WATCHED repo slug to its checkout path, and the operator display name it signs with (recorded in
    the engine's marker and journal, so the audit trail says who turned the loop off and that a tap
    — not a shell — asked). Every result is the real command outcome plus a ``summary``; never a
    pretend one."""

    def __init__(self, binary, repo_paths, operator=None, timeout=None):
        self._binary = binary
        self._paths = dict(repo_paths or {})
        self._operator = operator if (isinstance(operator, str) and operator.strip()) else None
        self._timeout = timeout

    def _refuse(self, verb):
        # An unwatched repo is refused BEFORE any subprocess — the command runner only ever targets
        # the checkouts the operator configured (bright line: never steerable off-machine/off-repo).
        out = {"ok": False, "verb": verb, "error": "unknown repo"}
        out["summary"] = _summary("err", "unknown repo — nothing was asked of the loop.")
        return out

    def stop(self, repo):
        """Stop this repo's runner on purpose — runs ``superlooper stop`` (the in-UI confirm already
        happened). Records the stop as deliberate, holds the supervisor, takes the process down; a
        stop that did not take comes back as the engine's own honest refusal, never a false
        all-clear."""
        return self._invoke("stop", repo)

    def start(self, repo):
        """Withdraw a deliberate stop and start this repo's runner again — runs ``superlooper
        start``. The way back on, so an owner who stops is never stranded."""
        return self._invoke("start", repo)

    def _invoke(self, verb, repo):
        path = self._paths.get(repo)
        if path is None:
            return self._refuse(verb)
        extra = ["--source", "command-center"]
        if self._operator:
            extra += ["--operator", self._operator]
        binary = _binary(self._binary)
        rc, out, err = _run(binary, [verb, "--repo", path, "--json", *extra],
                            timeout=self._timeout)
        parsed = parse_result(out)
        if parsed is not None:
            # The CLI answered (a stop that did not take is a well-formed body at rc 1) — surface
            # its honest outcome, normalizing the verb name for the UI trail.
            parsed["verb"] = verb
            parsed["summary"] = summarize(parsed)
            return parsed
        # No parseable JSON ⇒ the CLI is missing or crashed: a plain, honest failure. Never a
        # "stopped" the owner would believe, and never a silent success.
        message = _error(verb, rc, err, binary)
        return {"ok": False, "verb": verb, "error": message,
                "summary": _summary("err", message)}
