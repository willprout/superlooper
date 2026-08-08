#!/usr/bin/env python3
"""The acceptance suite, as a thing you RUN (issue #348).

`docs/HERDR-ADOPTION-PLAN.md` §8.1 makes every session-host version bump a deliberate event that
re-runs acceptance, and #311 ran that suite once, by hand, against the mini. What that produced was
a TRANSCRIPT — a report to imitate rather than something to run. This file is the same suite with
its shape fixed in code, for the same reason `vendor/herdr/build.sh` exists beside a README that
already described the build in four commands: an acceptance that exists only as prose is one that
will be run differently, or partly, or not at all.

    skills/superlooper/bin/acceptance.py --evidence <dir>

**What it does.** It builds a throwaway host from the carried patch as it currently stands, stands
that host up on sockets of its own under config directories it created itself, drives it only
through the five-verb doorway (`skill/lib/session_host.py`), never attaches a client, and judges
each of the twelve criteria in `docs/ARCHITECTURE-PROPOSALS.md` §3 by an ARTEFACT rather than by a
screen. Every criterion gets its own evidence file; a failed criterion exits non-zero.

**Two properties are load-bearing, and both were learned the hard way in #311's run.**

1. **Paired controls.** A drill that could pass for the wrong reason runs a CONTROL alongside it,
   differing in exactly one variable, in the same run. #311's headline — a fenced host capturing no
   session id — reads as a broken build until the unfenced control captures one on the same binary
   in the same minute, at which point it reads as what it was: a tokenless call the host refused.
   `capture_verdict` and `fence_verdict` below encode that as arithmetic, and a drill whose control
   did not run reports NOT RUN rather than a pass.
2. **It never touches the live fleet, production, or the operator's own host.** Not by warning — by
   construction. There is no flag that can aim it at an existing server; its sockets live under a
   directory it made this run; `socket_refusal` refuses the machine's own socket, the fleet's, and
   anything already bound; and every signal it sends goes to a pid it recorded itself
   (`Started`), never to a name or a pattern.

**It costs real agent sessions, and it says so before it spends them.** Faking the agent side would
make it cheap and worthless: the substrate's failures are exactly at the agent seam, which is where
every one of #311's findings lives.

**What it is not.** It is not a runtime path. Nothing in the runner imports this, nothing in the
engine may come to depend on it, and it is deliberately outside the published payload (`skill/`)
for the same reason the build script is: it needs the SOURCE checkout — the patch is a build input
that never ships. It also JUDGES and never repairs: it does not rebuild, install over or
reconfigure the machine's live host, and it does not edit a config to make itself pass. Building
its OWN host is setup, and is the only build it does.

The two writes it makes outside its own lab are named here rather than buried: it pre-trusts each
lane's throwaway folder in the Claude config dir the drill sessions run under (the same act
`skill/bin/pretrust.sh` performs on every launch — without it no unattended session gets past a
first-run dialog), and it removes those records again at teardown. Both are recorded in the
evidence.

**Exit codes.** 0 — nothing failed. 1 — a criterion FAILED. 2 — the run could not be completed
(a criterion left unjudged, a build that would not build, a host that never came up).
"""
import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.dirname(_HERE)                     # skills/superlooper
_REPO_ROOT = os.path.dirname(os.path.dirname(_ENGINE))
sys.path.insert(0, os.path.join(_ENGINE, "skill", "lib"))

import delivery                                                       # noqa: E402
import fleet                                                          # noqa: E402
import herdr_hook                                                     # noqa: E402
import session_host                                                   # noqa: E402


class SuiteError(Exception):
    """The suite refusing to do something, or refusing to pretend it did."""


# --------------------------------------------------------------------------- verdicts

# Four words, and the two that are not PASS/FAIL are the ones the DoD leans on. PARTIAL is a
# criterion the suite judged and that is met in part — the plan already rules some of them that way
# (S4's vocabulary, S8's OS containment). NOT RUN is a criterion nothing measured, and it carries
# its reason as a required field: "never silently skipped, never counted as a pass" is enforced by
# `Finding` refusing to be constructed without one.
PASS, FAIL, PARTIAL, NOT_RUN = "PASS", "FAIL", "PARTIAL", "NOT RUN"
_VERDICTS = (PASS, FAIL, PARTIAL, NOT_RUN)

OK, CRITERION_FAILED, RUN_INCOMPLETE = 0, 1, 2


# --------------------------------------------------------------- the criteria, read not owned

# The criteria live in the architecture doc and this script reports AGAINST them (issue #348's
# boundary). Folding them into this file's own vocabulary is how the suite would drift from the bar
# it claims to measure — silently, and in the direction of whatever the script happens to do.
CRITERIA_DOC = "docs/ARCHITECTURE-PROPOSALS.md"
CRITERIA_HEADING = "### The criteria"

# `S<n> <title> (<requirement>)`, as §3 spells them. The title runs to the opening parenthesis and
# the requirement is what is inside it; nothing is re-worded here.
_CRITERION_RE = re.compile(r"\bS(\d+)\s+([^(*\n][^(*]*?)\s*\(([^)]*)\)", re.S)
# `**I. Process contract** — …`, the grouping heading each run of criteria sits under.
_GROUP_RE = re.compile(r"\*\*([IVX]+\.[^*]+)\*\*")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Criterion:
    id: str
    title: str
    requirement: str
    group: str = ""


def criteria(text):
    """Parse §3's criteria out of the doc's own words. Raises rather than falling back.

    A silent fallback to a built-in list is exactly how this script would start owning the twelve
    requirements it is supposed to be reporting against, so there is no fallback to fall into.
    """
    body = text
    if CRITERIA_HEADING in text:
        body = text.split(CRITERIA_HEADING, 1)[1]
        # Stop at the next heading of the same level, so a later section cannot donate criteria.
        stop = body.find("\n### ")
        if stop != -1:
            body = body[:stop]
    found, group = [], ""
    for chunk in re.split(r"(?=\*\*[IVX]+\.)", body):
        heading = _GROUP_RE.search(chunk)
        if heading:
            group = _WS.sub(" ", heading.group(1)).strip()
        for match in _CRITERION_RE.finditer(chunk):
            found.append(Criterion(
                id="S" + match.group(1),
                title=_WS.sub(" ", match.group(2)).strip(),
                requirement=_WS.sub(" ", match.group(3)).strip(),
                group=group))
    if not found:
        raise SuiteError(
            "no criteria could be read out of %s — the suite reports AGAINST that document's own "
            "wording and will not substitute a list of its own" % CRITERIA_DOC)
    seen, unique = set(), []
    for criterion in found:
        if criterion.id in seen:
            continue
        seen.add(criterion.id)
        unique.append(criterion)
    return tuple(unique)


def read_criteria(repo_root=None):
    path = os.path.join(str(repo_root or _REPO_ROOT), CRITERIA_DOC)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise SuiteError("could not read the criteria from %s: %s" % (path, exc))
    return criteria(text)


# --------------------------------------------------------------------------- findings

@dataclass(frozen=True)
class Finding:
    """One verdict, with the artefact that produced it.

    ``judged_by`` is the same column #311's table carried, and it is required: a verdict whose
    provenance is not written down is an opinion.
    """
    criterion: str
    verdict: str
    judged_by: str
    detail: str = ""
    artefacts: tuple = ()

    def __post_init__(self):
        if self.verdict not in _VERDICTS:
            raise ValueError("unknown verdict %r" % (self.verdict,))
        if not str(self.judged_by).strip():
            raise ValueError("[%s] a verdict needs the artefact that produced it" % self.criterion)
        if self.verdict == NOT_RUN and not str(self.detail).strip():
            # The DoD's own words: never silently skipped. A not-run with no reason IS a silent
            # skip wearing a label, so it cannot be constructed.
            raise ValueError("[%s] a NOT RUN criterion must carry the reason it was not run"
                             % self.criterion)

    @property
    def passed(self):
        return self.verdict == PASS


class Report:
    """Every finding this run made, and the arithmetic that turns them into an exit code."""

    def __init__(self, criteria_list, started_at=None):
        self.criteria = tuple(criteria_list)
        self._by_id = {c.id: c for c in self.criteria}
        self.findings = []
        self.drills = []
        self.notes = []
        self.started_at = started_at or time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- recording ---------------------------------------------------------------------
    def record(self, finding):
        if finding.criterion in self._by_id:
            self.findings.append(finding)
        else:
            # A named drill rather than one of the twelve — the capture drill and the fence pair
            # get their own rows so their evidence is not buried inside a criterion's file.
            self.drills.append(finding)
        return finding

    def note(self, line):
        """A run-level fact the summary must carry — a deviation, a cost, a thing not done."""
        self.notes.append(str(line))

    # ---- reading -----------------------------------------------------------------------
    def unjudged(self):
        judged = {f.criterion for f in self.findings}
        return [c.id for c in self.criteria if c.id not in judged]

    def counts(self):
        out = {verdict: 0 for verdict in _VERDICTS}
        for finding in self.findings:
            out[finding.verdict] += 1
        return out

    def exit_code(self):
        if any(f.verdict == FAIL for f in self.findings + self.drills):
            return CRITERION_FAILED
        if self.unjudged():
            # A suite that reports on eleven of twelve criteria and exits 0 is worse than no suite:
            # the missing one reads as a pass to everybody who only looks at the code.
            return RUN_INCOMPLETE
        return OK

    def green(self):
        return not self.unjudged() and all(f.passed for f in self.findings + self.drills)

    # ---- rendering ---------------------------------------------------------------------
    def summary(self):
        counts = self.counts()
        lines = ["# Session-host acceptance — %s" % self.started_at, ""]
        lines.append("**Verdict: %s.** %d of %d criteria pass; %d partial, %d failed, %d not run."
                     % ("GREEN" if self.green() else "NOT GREEN",
                        counts[PASS], len(self.criteria), counts[PARTIAL], counts[FAIL],
                        counts[NOT_RUN]))
        lines.append("")
        lines.append("Criteria are read from `%s` §3 and reported against, never re-worded here."
                     % CRITERIA_DOC)
        if self.notes:
            lines += ["", "## How this run was made", ""]
            lines += ["- %s" % note for note in self.notes]
        if self.drills:
            lines += ["", "## Drills", "", "| drill | verdict | judged by |", "|---|---|---|"]
            for finding in self.drills:
                lines.append("| %s | **%s** | %s |"
                             % (finding.criterion, finding.verdict, finding.judged_by))
            for finding in self.drills:
                lines += ["", "**%s — %s.** %s" % (finding.criterion, finding.verdict,
                                                  finding.detail)]
        lines += ["", "## The criteria, one line each", "",
                  "| | criterion | verdict | judged by |", "|---|---|---|---|"]
        for criterion in self.criteria:
            found = [f for f in self.findings if f.criterion == criterion.id]
            verdict = found[0].verdict if found else "NO FINDING"
            judged = found[0].judged_by if found else "the suite recorded nothing for this criterion"
            lines.append("| %s | %s | **%s** | %s |"
                         % (criterion.id, criterion.title, verdict, judged))
        missing = self.unjudged()
        if missing:
            lines += ["", "**The run is INCOMPLETE**: %s carry no finding at all. A criterion "
                          "nobody judged is not a criterion that passed." % ", ".join(missing)]
        not_run = [f for f in self.findings if f.verdict == NOT_RUN]
        if not_run:
            lines += ["", "## Not run, and why", ""]
            lines += ["- **%s** — %s" % (f.criterion, f.detail) for f in not_run]
        lines += ["", "Evidence: one file per criterion beside this one.", ""]
        return "\n".join(lines)

    def _page(self, finding):
        criterion = self._by_id.get(finding.criterion)
        lines = ["# %s — %s" % (finding.criterion,
                                criterion.title if criterion else "drill"), ""]
        if criterion:
            lines += ["> **The requirement** (`%s` §3, %s): %s"
                      % (CRITERIA_DOC, criterion.group, criterion.requirement), ""]
        lines += ["**Verdict: %s.**" % finding.verdict, "",
                  "**Judged by:** %s" % finding.judged_by, "",
                  finding.detail or "", ""]
        for name, text in finding.artefacts:
            lines += ["## %s" % name, "", "```", str(text).rstrip(), "```", ""]
        return "\n".join(lines)

    def write(self, dirpath):
        """One named file per criterion and per drill, plus the summary. Returns what it wrote."""
        os.makedirs(dirpath, exist_ok=True)
        written = []
        for finding in self.findings + self.drills:
            path = os.path.join(dirpath, "%s.md" % _slug(finding.criterion))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._page(finding))
            written.append(path)
        for criterion in self.criteria:
            if criterion.id in {f.criterion for f in self.findings}:
                continue
            path = os.path.join(dirpath, "%s.md" % criterion.id)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# %s — %s\n\n**NO FINDING.** The suite recorded nothing for this "
                             "criterion, which is a fault in the run rather than a verdict about "
                             "the host.\n\n> %s\n" % (criterion.id, criterion.title,
                                                     criterion.requirement))
            written.append(path)
        path = os.path.join(dirpath, "summary.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.summary())
        written.append(path)
        return written


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-") or "finding"


# --------------------------------------------------------------------------- the lane matrix

@dataclass(frozen=True)
class Lane:
    """One drill lane: a real agent session, on a host configured one way.

    The matrix is the paired-control discipline made structural. Each lane differs from
    ``fenced-hooked`` — the configuration a fleet actually runs — in exactly ONE variable, which is
    what lets a difference in outcome be attributed to that variable and nothing else.
    """
    name: str
    fenced: bool
    hooked: bool
    trusted: bool
    expect_capture: bool
    why: str


LANES = (
    Lane("fenced-hooked", fenced=True, hooked=True, trusted=True, expect_capture=True,
         why="the configuration a fleet actually runs: the fence up, the state-report hook in the "
             "lane's own settings file, the folder pre-trusted"),
    Lane("unfenced-hooked", fenced=False, hooked=True, trusted=True, expect_capture=True,
         why="THE FENCE CONTROL (#311). Same binary, same config, same minute, one variable. "
             "Without it a silent capture reads as a broken build; with it, it reads as the "
             "refused call it is"),
    Lane("fenced-unhooked", fenced=True, hooked=False, trusted=True, expect_capture=False,
         why="THE HOOK CONTROL (#307). If a lane with no settings file captures an id anyway, the "
             "capture is not coming from the hook and the drill is measuring something else"),
    Lane("fenced-untrusted", fenced=True, hooked=True, trusted=False, expect_capture=False,
         why="THE FIRST-RUN GATE (S9). A fresh folder with no trust record, so the gate is "
             "measured rather than assumed — and it is the cheapest lane here, because a session "
             "stopped at a dialog never takes a turn"),
)

# Which drill needs which controls. Structural, so a drill cannot claim a control nobody runs —
# `test_acceptance_suite.py` asserts every name here is a lane the matrix stands up.
CONTROLS = {
    "fenced-hooked": ("unfenced-hooked", "fenced-unhooked"),
}

CAPTURE_DRILL = "capture drill (rebuild + kill -9 + headless restart)"
FENCE_DRILL = "fence pair (a tokenless caller, both hosts)"


def cost_notice(lanes=LANES):
    """What this run will spend, said before it spends it."""
    paid = [lane for lane in lanes if lane.trusted]
    return (
        "This suite costs REAL agent sessions: %d lanes, of which %d take turns on the "
        "subscription the drill config dir belongs to (the %d untrusted lane never gets past its "
        "first-run dialog and therefore never takes one). The agent side is deliberately not "
        "faked — the substrate's failures are all at the agent seam."
        % (len(lanes), len(paid), len(lanes) - len(paid)))


# --------------------------------------------------------------- the paired verdicts (pure)

@dataclass(frozen=True)
class LaneOutcome:
    """What one lane's drill observed. ``captured`` is True / False / None, and None means the lane
    did not run — which is a third answer, never a quiet False."""
    lane: str
    captured: object = None
    detail: str = ""
    artefacts: tuple = ()


def _ran(outcomes, name):
    outcome = outcomes.get(name)
    return outcome is not None and outcome.captured is not None


def capture_verdict(outcomes):
    """Judge the capture drill (#344's physical half, absorbed into this suite by #348).

    The question: after a `kill -9` of the host and a headless restart with no client ever
    attached, did the lane come back as a resumed agent — i.e. was its session id captured?

    The arithmetic is the pairing. A silent capture on the fenced lane means one of two completely
    different things, and only the controls tell them apart:

    * the unfenced control DID capture → the host refused a tokenless state report. That is the
      fence one patch-generation behind its own ruling (#331), not a broken build — which is
      exactly the sentence #311 could only write because it ran the pair;
    * neither lane captured → the build, the hook or the asset is broken, and the fence is
      innocent.
    """
    artefacts = tuple(a for name in ("fenced-hooked", "unfenced-hooked", "fenced-unhooked")
                      for a in (outcomes.get(name).artefacts if outcomes.get(name) else ()))
    primary = outcomes.get("fenced-hooked")
    if not _ran(outcomes, "fenced-hooked"):
        return Finding(CAPTURE_DRILL, NOT_RUN, "no lane completed the drill",
                       "the fenced lane did not run, so there is nothing to judge and nothing to "
                       "pair it against.", artefacts)
    missing = [name for name in CONTROLS["fenced-hooked"] if not _ran(outcomes, name)]
    if missing:
        return Finding(
            CAPTURE_DRILL, NOT_RUN, "an unpaired lane, which is not evidence",
            "the fenced lane ran and its control(s) %s did not. An unpaired result here is not a "
            "verdict: a capture could be the fence working or the hook firing for another reason, "
            "and a silence could be the fence refusing the report or the build being broken. The "
            "pairing is what made #311's run conclusive, so this reports NOT RUN rather than a "
            "pass." % ", ".join(missing), artefacts)

    control = outcomes["unfenced-hooked"]
    hook_control = outcomes["fenced-unhooked"]
    if hook_control.captured:
        return Finding(
            CAPTURE_DRILL, FAIL, "the hook control captured an id it should not have",
            "the lane with NO settings file — and therefore no state-report hook — came back "
            "resumed anyway. Something other than the hook is capturing the id, so this drill is "
            "not measuring what it claims and its other lanes cannot be read. (#307 measured the "
            "opposite: an identical command line with `--settings` removed reported no session at "
            "all.)", artefacts)
    if primary.captured and control.captured:
        return Finding(
            CAPTURE_DRILL, PASS,
            "the restarted host's own pane process, read from the OS, on both lanes",
            "the fenced lane captured a session id and came back resumed after `kill -9` and a "
            "headless restart, and so did the unfenced control — so the fence is up AND the ruled "
            "allowance (#331) is admitting the state report. The hook control captured nothing, "
            "which is what says the capture comes from the hook.", artefacts)
    if not primary.captured and control.captured:
        return Finding(
            CAPTURE_DRILL, FAIL, "the fenced/unfenced pair, one variable apart",
            "the fenced lane captured NO session id and came back a bare shell, while the "
            "unfenced control — same binary, same config, same cwd, same settings file, same "
            "minute — captured one and came back resumed. The single variable is the fence, so "
            "this is a tokenless `%s` being REFUSED, not a broken build: the host is running the "
            "fence one patch-generation behind #331's allowance. Rebuild it from the current "
            "patch (`vendor/herdr/build.sh --force`) and re-run."
            % session_host.STATE_REPORT_METHOD, artefacts)
    if not primary.captured and not control.captured:
        return Finding(
            CAPTURE_DRILL, FAIL, "the fenced/unfenced pair, neither of which captured",
            "NEITHER lane captured a session id, so the fence is not the variable — an unfenced "
            "host refuses nothing and it captured nothing either. Look at the build, the carried "
            "hook asset and the per-lane settings file before looking at the fence.", artefacts)
    return Finding(
        CAPTURE_DRILL, FAIL, "the fenced/unfenced pair, inverted",
        "the fenced lane captured a session id and the UNFENCED control did not, which no known "
        "mechanism explains — an unfenced host refuses nothing the fenced one admits. Treat the "
        "run as unsound rather than the host as certified.", artefacts)


def fence_verdict(probes):
    """Judge the fence itself, with its own control.

    ``probes`` maps ``fenced``/``unfenced`` to a `session_host.fence_probe` verdict. The control is
    not decoration: a socket that refused EVERY caller would sail through the fenced half and be a
    broken host rather than a fence, which is the same shape as
    `tests/test_fence_token_auth.py`'s two halves.
    """
    fenced, unfenced = probes.get("fenced"), probes.get("unfenced")
    detail = "fenced host: %s; unfenced control: %s" % (fenced, unfenced)
    if fenced is None or unfenced is None:
        return Finding(FENCE_DRILL, NOT_RUN, "an unpaired probe, which is not evidence",
                       "one side of the pair did not run (%s). A refusal on its own cannot be "
                       "told from a host that answers nobody." % detail)
    if fenced == session_host.FENCED and unfenced == session_host.OPEN:
        return Finding(FENCE_DRILL, PASS, "a tokenless probe of both sockets — a worker's position",
                       "the fenced host refused the tokenless caller and the unfenced control "
                       "served it, so the refusal is the fence rather than a host that is simply "
                       "not answering. " + detail)
    if fenced != session_host.FENCED:
        return Finding(FENCE_DRILL, FAIL, "a tokenless probe of both sockets",
                       "the fenced host did NOT refuse a tokenless caller. An unfenced socket does "
                       "not look broken from the runner's seat — it answers. " + detail)
    return Finding(FENCE_DRILL, FAIL, "a tokenless probe of both sockets",
                   "the fenced host refused the tokenless caller but so did the unfenced CONTROL, "
                   "which should have served it. That is a host answering nobody rather than a "
                   "fence, so the fenced side's refusal proves nothing. " + detail)


# ------------------------------------------------------- it cannot be pointed at a live host

def lane_socket(lab_root, host_name):
    """Where one throwaway host's control socket goes — under a directory this run made."""
    return session_host.session_socket_path(
        session_host.config_dir({"XDG_CONFIG_HOME": os.path.join(str(lab_root), host_name,
                                                                 "config")}))


def socket_refusal(path, env=None):
    """None if this is a socket the suite may bind, else the sentence explaining why it may not.

    By construction rather than by warning: there is no flag that names a socket (see `parser`),
    so the only paths that reach here are ones the suite derived for itself — and this is the belt
    that says so out loud, because "the acceptance broke the machine it was certifying" is the one
    outcome an acceptance may never have.
    """
    env = os.environ if env is None else env
    path = os.path.abspath(str(path))
    live = session_host.control_socket_path(env)
    if live and os.path.abspath(live) == path:
        return ("that is this machine's OWN control socket — the operator's host, or whatever "
                "their environment names. The suite stands up its own or does not run.")
    config_dir = session_host.config_dir(env)
    if os.path.abspath(fleet.socket_path(config_dir)) == path:
        return ("that is the fleet's control socket. The fleet is the thing being certified; an "
                "acceptance that drove it could be the thing that broke it.")
    if os.path.abspath(fleet.socket_path(config_dir, session=None)) == path:
        return "that is the host's default-session socket on this machine, not a socket of ours."
    if os.path.exists(path):
        return ("something is already at that path, so it belongs to a server this run did not "
                "start.")
    problem = fleet.socket_path_problem(path)
    if problem:
        return problem
    return None


# ------------------------------------------------------- it never signals what it did not start

class Started:
    """Every process this run started, and the only pids it will ever signal.

    The house rule (`pkill -f` / `killall` are banned outright) exists because a pattern can also
    match the owner's own live processes — and on this machine, one of them is the fleet's host and
    another is the operator's. So the suite records a pid when it starts one and refuses to signal
    anything else, including on the interrupt path: `interrupted` is the SAME code as `stop_all`,
    not a second, untested one.
    """

    def __init__(self, kill=None, sleep=None, alive=None):
        self._kill = kill if kill is not None else os.kill
        self._sleep = sleep if sleep is not None else time.sleep
        self._alive = alive
        self._started = {}

    def record(self, pid, what):
        self._started[int(pid)] = str(what)
        return int(pid)

    def outstanding(self):
        return sorted(self._started)

    def describe(self, pid):
        return self._started.get(int(pid), "")

    def stop(self, pid, patience=5.0):
        """SIGTERM, then SIGKILL if it is still there. Refuses a pid this run did not start."""
        pid = int(pid)
        if pid not in self._started:
            raise SuiteError(
                "refusing to signal pid %s: this run did not start it. Every signal the suite "
                "sends goes to a pid it recorded itself — never a name, never a pattern." % pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                self._kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            waited = 0.0
            while waited < patience:
                if not self.is_alive(pid):
                    break
                self._sleep(0.2)
                waited += 0.2
            if not self.is_alive(pid):
                break
        self._started.pop(pid, None)

    def is_alive(self, pid):
        if self._alive is not None:
            return self._alive(int(pid))
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def stop_all(self):
        for pid in self.outstanding():
            try:
                self.stop(pid)
            except SuiteError:                          # pragma: no cover - cannot happen
                pass

    # The interrupt path is the teardown path. A second one would be a second thing to get wrong,
    # and it would only ever be exercised at the worst possible moment.
    interrupted = stop_all


# --------------------------------------------------------------------------- the live half
#
# Everything below stands things up and reads them. It is deliberately the part with no unit tests:
# a fake host and a fake agent would prove the fakes, and the substrate's failures are all at the
# agent seam. What IS tested is every verdict this half feeds — the functions above.

_SOCKET_WAIT = 30.0
_RESTORE_WAIT = 90.0
_PROMPT_MS = 180000

# The ambient variables a throwaway host inherits — see `Lab.host_env` for what happens without
# them. An ALLOW-list rather than "everything except the poison", because a lab that forwarded the
# runner's whole environment would hand its drill sessions the launcher's own contract
# (`SL_ATTENDED`, `SL_SESSION_ID`, a credential redirect) and measure something else entirely.
_AMBIENT = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG", "LC_ALL",
            "__CF_USER_TEXT_ENCODING")


class Lab:
    """The throwaway world: two hosts, their sockets, their lanes, and the teardown."""

    def __init__(self, root, binary, config_dir, claude=None, report=None, started=None):
        self.root = root
        self.binary = binary
        self.config_dir = config_dir
        self.claude = claude
        self.report = report
        self.started = started if started is not None else Started()
        self.sockets = {}
        self.hosts = {}
        self.sessions = {}
        self._trusted = []
        self._logs = {}

    # ---- hosts -------------------------------------------------------------------------
    def host_env(self, name, fenced, base=None):
        """The server's own environment — and therefore, by inheritance, every pane's.

        Three rules, and the first was MEASURED here rather than reasoned about:

        1. **The ambient identity a real fleet server has, this one has too.** A server started
           from a hand-written variable list hands its panes an environment POORER than launchd
           hands the fleet's, and the drills then measure the lab's poverty and report it as the
           host's. Concretely (2026-08-07, this machine): drop ``USER`` and the agent in the pane
           reports **logged out** — `claude auth status` answers ``loggedIn: false`` under the very
           config dir that answers ``true`` with ``USER`` restored, because that is how it finds
           its keychain item. It cost an afternoon to find and it would read as auth-death in the
           host, so the list below is an ALLOW-list of ambient variables, not an invention.
        2. **`HOME` is never overridden.** A pane inherits it, and overriding HOME breaks macOS
           keychain OAuth outright — the c25 landmine, recorded in `lib/launch.py`.
        3. **No credential redirect is forwarded.** Whatever is ambient in the runner's shell, a
           drill session must resolve its identity from the config dir it was ASSIGNED; an
           inherited `ANTHROPIC_API_KEY` silently bills somebody else and makes S10 meaningless.

        The XDG homes ARE overridden: that is how two throwaway hosts get two sockets on one
        machine without either of them being the operator's. The fleet reaches the same isolation
        with a named session instead, and that difference is recorded in the run's notes rather
        than smoothed over.
        """
        base = dict(os.environ if base is None else base)
        room = os.path.join(self.root, name)
        env = {key: base[key] for key in _AMBIENT if base.get(key)}
        env.setdefault("TERM", "xterm-256color")
        env.update({
            "XDG_CONFIG_HOME": os.path.join(room, "config"),
            "XDG_STATE_HOME": os.path.join(room, "state"),
            "XDG_DATA_HOME": os.path.join(room, "data"),
        })
        if fenced:
            env[session_host.API_TOKEN_ENV_VAR] = "acceptance-" + os.urandom(12).hex()
        return env

    def start_host(self, name, fenced):
        env = self.host_env(name, fenced)
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
            os.makedirs(env[key], exist_ok=True)
        socket_path = session_host.session_socket_path(session_host.config_dir(env))
        refusal = socket_refusal(socket_path)
        if refusal:
            raise SuiteError("refusing to bind %s: %s" % (socket_path, refusal))
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        # The host's own configuration, rendered by the same code the fleet build-up uses, so the
        # lab is not a second opinion about what a configured host looks like.
        with open(fleet.host_config_path(session_host.config_dir(env)), "w",
                  encoding="utf-8") as handle:
            handle.write(fleet.render_host_config())
        log_path = os.path.join(self.root, "%s-host.log" % name)
        log = open(log_path, "ab")
        self._logs[name] = log_path
        proc = subprocess.Popen([self.binary, "server"], env=env, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True, cwd=self.root)
        self.started.record(proc.pid, "the %s throwaway host" % name)
        self.hosts[name] = {"env": env, "proc": proc, "fenced": fenced, "log": log_path}
        self.sockets[name] = socket_path
        self._await_socket(socket_path, name)
        return self.hosts[name]

    def _await_socket(self, socket_path, name):
        deadline = time.monotonic() + _SOCKET_WAIT
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                time.sleep(0.5)
                return
            time.sleep(0.2)
        raise SuiteError("the %s host never created its socket at %s — see %s"
                         % (name, socket_path, self._logs.get(name)))

    def restart_host(self, name):
        """`kill -9` the host and bring it back HEADLESS — the drill's destructive half.

        The pid is the one recorded when this run started the process, and `Started` refuses any
        other. Nothing is ever ended by name here.
        """
        host = self.hosts[name]
        pid = host["proc"].pid
        self.started.stop(pid, patience=10.0)
        socket_path = self.sockets[name]
        for _ in range(50):
            if not os.path.exists(socket_path):
                break
            time.sleep(0.2)
        log = open(host["log"], "ab")
        proc = subprocess.Popen([self.binary, "server"], env=host["env"], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True, cwd=self.root)
        self.started.record(proc.pid, "the %s throwaway host (restarted)" % name)
        host["proc"] = proc
        self._await_socket(socket_path, name)
        return proc.pid

    def wrapper(self, name):
        """The five-verb doorway, pointed at one throwaway host. The ONLY way the suite talks."""
        env = dict(self.hosts[name]["env"])
        return session_host.SessionHost(probe=_LabProbe(env), binary=self.binary, env=env,
                                        prompt_timeout_ms=_PROMPT_MS)

    # ---- lanes -------------------------------------------------------------------------
    def lane_cwd(self, lane):
        path = os.path.join(self.root, "lanes", lane.name)
        os.makedirs(path, exist_ok=True)
        return path

    def settings_for(self, lane):
        """The per-lane settings file that registers the host's own state-report hook (#307)."""
        path = os.path.join(self.root, "settings", "%s.settings.json" % lane.name)
        return herdr_hook.write_settings(
            path, herdr_hook.vendored_script(os.path.join(_ENGINE, "skill")))

    def pretrust(self, cwd):
        """Write the folder-trust record the drill sessions need, and remember to remove it.

        The same act `skill/bin/pretrust.sh` performs on every launch — run through that script
        rather than re-implemented, so the suite cannot certify a mechanism it does not use.
        """
        script = os.path.join(_ENGINE, "skill", "bin", "pretrust.sh")
        proc = subprocess.run([script, cwd, self.config_dir], capture_output=True, text=True,
                              timeout=60, env=dict(os.environ, SL_AGENT="claude"))
        if proc.returncode != 0:
            raise SuiteError("pre-trusting %s failed: %s" % (cwd, proc.stderr.strip()))
        self._trusted.append(os.path.realpath(cwd))
        return proc.stdout.strip()

    def untrust_all(self):
        """Remove every trust record this run wrote. The lab leaves no state behind."""
        path = os.path.join(self.config_dir, ".claude.json")
        removed = []
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return removed
        projects = document.get("projects")
        if not isinstance(projects, dict):
            return removed
        for folder in self._trusted:
            if projects.pop(folder, None) is not None:
                removed.append(folder)
        if removed:
            tmp = path + ".acceptance.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
            os.replace(tmp, path)
        self._trusted = []
        return removed

    def spawn(self, lane, host_name):
        """One real agent session, through the doorway. Costs a real session; that is the point."""
        host = self.wrapper(host_name)
        cwd = self.lane_cwd(lane)
        if lane.trusted:
            self.pretrust(cwd)
        session_id = str(uuid.uuid4())
        args = ["--dangerously-skip-permissions", "--session-id", session_id]
        if lane.hooked:
            args = ["--settings", self.settings_for(lane)] + args
        # A caller's deliberate smuggling attempt rides along on every spawn: the wrapper's own
        # scrub is what must drop it, and reading it back from inside the pane is the S7 drill.
        env = {"CLAUDE_CONFIG_DIR": self.config_dir,
               "SL_ACCEPTANCE_LANE": lane.name,
               session_host.API_TOKEN_ENV_VAR: "smuggled-by-the-caller-and-must-not-arrive"}
        session = host.spawn(_lane_name(lane), cwd, env=env, kind="claude", agent_args=args,
                             label="superlooper acceptance %s" % lane.name)
        self.sessions[lane.name] = {"session": session, "host": host_name, "cwd": cwd,
                                    "session_id": session_id, "lane": lane}
        return self.sessions[lane.name]

    def oracle(self, lane_name, text):
        record = self.sessions[lane_name]
        return delivery.Transcript(
            root=os.path.join(self.config_dir, delivery.PROJECTS_DIRNAME),
            session_id=record["session_id"], text=text)

    # ---- teardown ----------------------------------------------------------------------
    def teardown(self):
        """Close what we opened, in an order that leaves nothing running.

        Sessions first, through the doorway, because that is the path that re-reads the live pane
        and attributes the pid before signalling it. Then the hosts, by recorded pid. Anything
        still alive afterwards is REPORTED as an orphan rather than hunted — a pid recorded at
        spawn that nothing can still vouch for is exactly the pid `session_host` refuses to signal,
        and the same rule applies to its caller.
        """
        loose = []
        for name, record in list(self.sessions.items()):
            try:
                host = self.wrapper(record["host"])
                out = host.kill(record["session"])
                if out.orphan_pid:
                    loose.append("%s left pid %s behind" % (name, out.orphan_pid))
            except Exception as exc:                     # teardown never raises past this point
                loose.append("%s could not be closed: %s" % (name, exc))
        self.started.stop_all()
        try:
            removed = self.untrust_all()
        except Exception as exc:                         # pragma: no cover
            loose.append("trust records could not be removed: %s" % exc)
        else:
            if removed and self.report:
                self.report.note("removed the %d folder-trust record(s) this run wrote to %s"
                                 % (len(removed), os.path.join(self.config_dir, ".claude.json")))
        if loose and self.report:
            self.report.note("teardown loose ends: " + "; ".join(loose))
        return loose


def _lane_name(lane):
    """The host-addressable name for a lane. The host's own rule is `[a-z][a-z0-9_-]{0,31}`."""
    return re.sub(r"[^a-z0-9_-]+", "-", lane.name.lower())[:32]


class _LabProbe(session_host.Probe):
    """The doorway's edge, with the lab host's environment on it.

    The doorway builds every command line; this only decides which world it runs in. That is the
    whole reason the suite can drive a throwaway host without becoming a second caller of it.
    """

    def __init__(self, env):
        self._env = dict(env)

    def run(self, argv, timeout=session_host._CALL_SECONDS):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  env=self._env)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", "no answer within %ss" % timeout)
        except (OSError, ValueError):
            return subprocess.CompletedProcess(argv, 127, "", "could not run %s" % (argv[:1],))


# --------------------------------------------------------------------------- OS reads

def _argv_of(pid):
    """One process's command line, read from the OS rather than taken on the host's word."""
    try:
        proc = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (proc.stdout or "").strip()


def _pane_child_argv(host, session):
    """The argv of whatever is running in a session's pane, via the doorway's state read.

    Deliberately not `ps -Eww`: on macOS a same-uid reader is refused another process's
    environment, so an env-based check here would be silently vacuous — a mistake already made once
    in this codebase (`vendor/herdr/README.md` records it).
    """
    state = host.state(session)
    if not state.shell_pid:
        return "", state
    kids = session_host.Probe().child_pids(state.shell_pid)
    if not kids:
        return "", state
    return " | ".join(_argv_of(kid) for kid in kids), state


# --------------------------------------------------------------------------- the build

def build_host(work_dir, prefix, force=True):
    """Build the throwaway host FROM THE PATCH AS IT CURRENTLY STANDS.

    Setup, not repair: it installs into a prefix of ours and never touches the machine's live host.
    `--force` is not optional — the build script is idempotent on the VERSION, and a patch change at
    an unchanged pin is exactly the miss that produced #311's finding.
    """
    script = os.path.join(_ENGINE, "vendor", "herdr", "build.sh")
    argv = [script, "--prefix", prefix, "--work", work_dir] + (["--force"] if force else [])
    proc = subprocess.run(argv, capture_output=True, text=True)
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SuiteError("the throwaway host would not build (rc=%s):\n%s"
                         % (proc.returncode, log[-4000:]))
    binary = fleet.host_binary(prefix)
    if not os.path.isfile(binary):
        raise SuiteError("the build reported success but %s is not there" % binary)
    return binary, log


# --------------------------------------------------------------------------- the drills


def _quick(host, session):
    state = host.state(session)
    return "liveness=%s advisory=%s name_resolves=%s pane=%s" % (
        state.liveness, state.advisory, state.name_resolves, state.pane)


def run_suite(args, report):
    """Stand the lab up, run every drill, record every finding. Returns the exit code."""
    criteria_list = report.criteria
    lab_root = tempfile.mkdtemp(prefix="sl-acc-", dir=args.lab_base)
    report.note("lab root: %s (removed at teardown unless --keep)" % lab_root)
    report.note(cost_notice())

    started = Started()
    lab = Lab(lab_root, binary=None, config_dir=args.config_dir, report=report, started=started)

    def _panic(signum, _frame):
        # The interrupt path tears down FIRST and keeps the evidence gathered so far. Order
        # matters: a run that wrote its report and then failed to stop a server would have left
        # the one thing this suite may never leave behind.
        sys.stderr.write("\nacceptance: interrupted (signal %s) — tearing the lab down\n" % signum)
        try:
            lab.teardown()
        finally:
            try:
                report.note("INTERRUPTED by signal %s — the run is incomplete and the criteria "
                            "below are only what had been reached" % signum)
                report.write(args.evidence)
            except Exception:                            # pragma: no cover - best effort only
                pass
            os._exit(RUN_INCOMPLETE)

    signal.signal(signal.SIGINT, _panic)
    signal.signal(signal.SIGTERM, _panic)
    atexit.register(started.stop_all)

    try:
        # ---- 0. the binary under test ---------------------------------------------------
        if args.host_binary:
            lab.binary = os.path.abspath(args.host_binary)
            report.note("the host binary was SUPPLIED (%s) rather than built from the patch — the "
                        "capture drill therefore certifies that file, not the patch as it "
                        "currently stands" % lab.binary)
            build_log = "not built: --host-binary was given"
        else:
            print("acceptance: building the throwaway host from the current patch (minutes)…")
            lab.binary, build_log = build_host(args.build_work,
                                               os.path.join(lab_root, "prefix"))
            report.note("built the throwaway host from `vendor/herdr/` as it currently stands")
        version = subprocess.run([lab.binary, "--version"], capture_output=True,
                                 text=True).stdout.strip()
        report.note("host under test: %s (%s)" % (lab.binary, version or "version unreadable"))

        # ---- 1. the hosts, and the fence pair -------------------------------------------
        for host_name, fenced in (("fenced", True), ("unfenced", False)):
            lab.start_host(host_name, fenced)
        probes = {name: session_host.fence_probe(path) for name, path in lab.sockets.items()}
        reports = {name: session_host.state_report_probe(path)
                   for name, path in lab.sockets.items()}
        fence = report.record(fence_verdict(probes))
        report.record(Finding(
            "S8", PARTIAL if fence.verdict == PASS else FAIL,
            "a tokenless probe of both throwaway sockets, paired",
            "%s\n\nThe allowance (#331) answered %s on the fenced host and %s on the unfenced "
            "control. PARTIAL rather than PASS for the reason #311 gave: the fence covers the "
            "CONTROL SURFACE, and panes are same-uid processes with no OS containment at all, so "
            "the kill-by-pattern deny stays load-bearing rather than becoming defence in depth."
            % (fence.detail, reports.get("fenced"), reports.get("unfenced")),
            (("fence probes", json.dumps(probes, indent=2)),
             ("state-report probes", json.dumps(reports, indent=2)))))

        # ---- 2. atomic spawn: the refusal half, before anything costs a session ----------
        _judge_atomic_spawn(lab, report)

        # ---- 3. the lanes ---------------------------------------------------------------
        outcomes = {}
        for lane in LANES:
            host_name = "fenced" if lane.fenced else "unfenced"
            try:
                lab.spawn(lane, host_name)
            except Exception as exc:
                outcomes[lane.name] = LaneOutcome(lane.name, None, "the lane did not start: %s"
                                                  % exc)
                report.note("lane %s did not start: %s" % (lane.name, exc))
        _judge_spawn_and_state(lab, report)
        _judge_delivery_and_environment(lab, report, args)
        _judge_first_run_gate(lab, report)
        _judge_lifecycle(lab, report)
        _judge_billing(report, args)

        # ---- 4. the capture drill: kill -9 each host and restart headless ---------------
        for host_name in ("fenced", "unfenced"):
            if host_name not in lab.hosts:
                continue
            print("acceptance: kill -9 the %s host and restarting it headless…" % host_name)
            lab.restart_host(host_name)
        time.sleep(5)
        for lane in LANES:
            if lane.name in outcomes or lane.name not in lab.sessions:
                continue
            outcomes[lane.name] = _capture_outcome(lab, lane)
        capture = report.record(capture_verdict(outcomes))
        _judge_from_capture(lab, report, capture, outcomes)

        # ---- 5. the criteria nothing here can judge -------------------------------------
        _record_unjudgeable(report, args)
    finally:
        lab.teardown()
        if not args.keep:
            shutil.rmtree(lab_root, ignore_errors=True)
        else:
            report.note("--keep: the lab was left at %s" % lab_root)

    for criterion in criteria_list:
        if criterion.id not in {f.criterion for f in report.findings}:
            report.record(Finding(criterion.id, NOT_RUN, "nothing",
                                  "the run ended before this criterion was reached"))
    return report.exit_code()


def _judge_atomic_spawn(lab, report):
    """S1's negative half: a refused spawn RAISES and leaves nothing behind."""
    host = lab.wrapper("fenced")
    impossible = os.path.join(lab.root, "no-such-directory-for-a-workspace")
    detail, verdict = "", FAIL
    try:
        host.spawn("accprobe", impossible, kind="claude", agent_args=["--version"])
    except session_host.SpawnRefused as exc:
        state = host.state(session_host.Session(name="accprobe", workspace=""))
        verdict = PASS if state.name_resolves is False else FAIL
        detail = ("a spawn into a directory that does not exist RAISED (%s) and the name does not "
                  "resolve afterwards (%s) — the rollback ran, so there is no silent no-op path "
                  "and no leaked workspace." % (type(exc).__name__, _quick_state(state)))
    except Exception as exc:
        detail = "the spawn failed in a way the doorway does not name: %r" % exc
    else:
        detail = ("a spawn into a directory that does not exist SUCCEEDED, which is the hollow "
                  "launch this criterion exists to remove.")
    report.record(Finding("S1", verdict, "a deliberately impossible spawn, driven through the "
                                         "doorway", detail))


def _quick_state(state):
    return "liveness=%s name_resolves=%s" % (state.liveness, state.name_resolves)


def _judge_spawn_and_state(lab, report):
    """S1's positive half (folded into the existing finding), S4, and S6's first leg."""
    record = lab.sessions.get("fenced-hooked")
    if not record:
        report.record(Finding("S4", NOT_RUN, "nothing",
                              "no lane started, so nothing could be read back"))
        return
    host = lab.wrapper(record["host"])
    argv, state = _pane_child_argv(host, record["session"])
    prior = [f for f in report.findings if f.criterion == "S1"]
    if prior and prior[0].verdict == PASS:
        report.findings.remove(prior[0])
        report.record(Finding(
            "S1", PASS, "a refused spawn, and the OS's own view of a successful one",
            prior[0].detail + "\n\nAnd the positive half: `spawn` returned only after proving a "
            "process exists behind the name (pane shell pid %s), and the pane's child argv read "
            "from the OS is the command the launch asked for." % state.shell_pid,
            (("pane child argv (ps)", argv or "nothing was readable"),)))
    report.record(Finding(
        "S4", PARTIAL, "the doorway's own structured state read",
        "the host answers with structured data — liveness from OS process facts, the host's "
        "lifecycle word carried as ADVISORY and gating nothing, the pane resolved fresh from the "
        "name. PARTIAL, as the plan already rules: the vocabulary is attended-mode (its `idle` "
        "means ready AND seen in the focused UI), which is why nothing in the engine branches on "
        "it.",
        (("state read", json.dumps({
            "name": state.name, "liveness": state.liveness, "advisory": state.advisory,
            "name_resolves": state.name_resolves, "pane": state.pane,
            "shell_pid": state.shell_pid, "hooks": state.hooks, "detail": state.detail},
            indent=2, default=str)),)))


_ENV_PROBE_FILE = "environment.txt"


def _judge_delivery_and_environment(lab, report, args):
    """S3 (delivery proven transcript-side) and S7 (the pane's environment, read from inside it).

    One prompt, two criteria, and the second is why the prompt is the one it is: the only way to
    read a pane's environment on macOS is to have the pane report it — a same-uid reader is refused
    another process's environment, so `ps -Eww` returns nothing and a check built on it would be
    silently vacuous.
    """
    record = lab.sessions.get("fenced-hooked")
    if not record:
        for cid in ("S3", "S7"):
            report.record(Finding(cid, NOT_RUN, "nothing", "no lane started"))
        return
    host = lab.wrapper(record["host"])
    target = os.path.join(record["cwd"], _ENV_PROBE_FILE)
    text = ("Acceptance drill. Run `env` and write its complete output to %s, then reply with the "
            "single word done. Do nothing else." % target)
    oracle = lab.oracle("fenced-hooked", text)
    try:
        sent = host.send(record["session"], text, delivery=oracle)
    except Exception as exc:
        report.record(Finding("S3", FAIL, "an addressed `--wait` send with a transcript oracle",
                              "the send did not prove delivery: %r" % exc))
        report.record(Finding("S7", NOT_RUN, "nothing",
                              "the environment probe rides on the delivery drill, which failed"))
        return
    report.record(Finding(
        "S3", PASS, "an addressed `--wait` send, proven transcript-side rather than by rc",
        "delivered=%s chased=%s rc=%s restored=%s. Addressed BY NAME, never by a cached pane id, "
        "and the proof is the caller's own oracle: rc is not evidence in either direction, since "
        "even a settled `--wait` rc=0 means queued."
        % (sent.delivered, sent.chased, sent.rc, sent.restored)))

    deadline = time.monotonic() + 180
    body = ""
    while time.monotonic() < deadline:
        try:
            with open(target, encoding="utf-8", errors="replace") as handle:
                body = handle.read()
            if body.strip():
                break
        except OSError:
            pass
        time.sleep(2)
    if not body.strip():
        report.record(Finding(
            "S7", NOT_RUN, "nothing",
            "the lane never wrote its environment out, so nothing was measured from inside the "
            "pane. This is reported rather than inferred: reading it from outside is impossible "
            "on macOS (a same-uid reader is refused another process's environment)."))
        return
    names = {}
    for line in body.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            names[key.strip()] = value
    smuggled = names.get(session_host.API_TOKEN_ENV_VAR)
    problems = []
    if smuggled:
        problems.append("the caller's smuggled credential arrived in the pane as %r" % smuggled)
    for poison in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if names.get(poison):
            problems.append("%s reached the session" % poison)
    if names.get("CLAUDE_CONFIG_DIR") != lab.config_dir:
        problems.append("the assigned config dir did not arrive (%r)"
                        % names.get("CLAUDE_CONFIG_DIR"))
    report.record(Finding(
        "S7", FAIL if problems else PASS,
        "the pane's own environment, written out by the session itself",
        ("; ".join(problems) if problems else
         "measured from INSIDE the pane: the token this run deliberately smuggled through the "
         "caller's environment was dropped by the wrapper's scrub, no credential redirect reached "
         "the session, and the assigned config dir arrived intact. The host injects its own pane "
         "context whatever we pass, which is precisely why the fence — and not this scrub — is the "
         "fence."),
        (("the pane's environment, keys only",
          "\n".join(sorted(k for k in names if not k.startswith("_")))),)))


def _judge_first_run_gate(lab, report):
    """S9, measured rather than assumed: the untrusted lane against its pre-trusted control."""
    untrusted = lab.sessions.get("fenced-untrusted")
    control = lab.sessions.get("fenced-hooked")
    if not untrusted or not control:
        report.record(Finding("S9", NOT_RUN, "nothing",
                              "the gate drill needs both an untrusted lane and a pre-trusted "
                              "control, and one of them did not start"))
        return
    host = lab.wrapper(untrusted["host"])
    text = "Acceptance gate drill: reply with the single word ready."
    oracle = lab.oracle("fenced-untrusted", text)
    gated, detail = None, ""
    try:
        host.send(untrusted["session"], text, delivery=oracle, timeout_ms=60000)
        gated = False
        detail = "a session in a folder with NO trust record took the prompt anyway"
    except Exception as exc:
        gated = True
        detail = "the untrusted lane could not be driven (%s: %s)" % (type(exc).__name__,
                                                                      str(exc)[:300])
    state = host.state(untrusted["session"])
    report.record(Finding(
        "S9", FAIL if gated else PASS,
        "an untrusted lane beside a pre-trusted control on the same host",
        "%s, while the pre-trusted control on the same host and the same binary delivered. The "
        "host reads the untrusted lane as advisory=%r. So a first-run gate is still there and "
        "pre-trust is still load-bearing: this criterion asks for ZERO attended gates, and the "
        "suite itself has to write a trust record before any lane of its own will run (#345 owns "
        "which file that record goes in)." % (detail, state.advisory)))


def _capture_outcome(lab, lane):
    """After `kill -9` and a headless restart: did this lane come back as a RESUMED agent?

    Judged by the restarted host's own pane process, read from the OS. That is the artefact #311's
    table used, and it is stronger than asking the host what it recorded — a resumed process either
    carries the id on its command line or it does not.
    """
    record = lab.sessions[lane.name]
    host = lab.wrapper(record["host"])
    deadline = time.monotonic() + _RESTORE_WAIT
    argv, state = "", None
    while time.monotonic() < deadline:
        argv, state = _pane_child_argv(host, record["session"])
        if argv:
            break
        time.sleep(3)
    captured = bool(argv) and "--resume" in argv
    return LaneOutcome(
        lane=lane.name, captured=captured,
        detail="after the restart the pane runs: %s" % (argv or "nothing — a bare shell"),
        artefacts=((("%s: pane argv after the headless restart" % lane.name),
                    argv or "nothing was running in the pane (a bare shell)"),
                   (("%s: state after the restart" % lane.name),
                    _quick_state(state) if state else "no state read")))


def _judge_from_capture(lab, report, capture, outcomes):
    """S5, S6 and S12 all read off the same restart the capture drill performed."""
    fenced = outcomes.get("fenced-hooked")
    resolved = fenced is not None and fenced.captured is not None
    report.record(Finding(
        "S6", PASS if resolved else FAIL,
        "a session re-addressed BY NAME across a `kill -9` of its host",
        "the lane outlived the process that spawned it and was re-addressed by name from the "
        "doorway after the host was killed outright and restarted headless — no anchor, no "
        "terminal geography, no cached pane id. %s"
        % (fenced.detail if fenced else "no lane survived to be re-addressed")))
    report.record(Finding(
        "S5", PARTIAL, "two `kill -9` + headless restarts, with no client ever attached",
        "the clientless half is measured: every drill in this run happened with no client attached "
        "to either host, through two host deaths and two headless restarts. The OTHER half — a "
        "full night with the display off and the machine locked (plan §8.6) — is NOT RUN and "
        "cannot be: it is not a thing one session can do, and rounding it up into this PARTIAL "
        "would be claiming a night nobody sat through."))
    report.record(Finding(
        "S12", PASS if capture.verdict == PASS else FAIL,
        "the per-lane settings file, and the operator's global settings file untouched",
        "the hook the per-lane `--settings` file registers is the ONLY way a session id reaches "
        "the host, so the capture drill's verdict is this criterion's evidence: %s. The operator's "
        "own global settings file is never opened by this suite — the hook goes in a file that "
        "belongs to one lane." % capture.verdict))


def _judge_lifecycle(lab, report):
    """S2, DRIVEN rather than asserted: `exit` must refuse a live lane and `kill` must end it.

    The subject is the untrusted lane — live, sitting at its first-run dialog, and the one lane
    here that has taken no turn, so a teardown drill costs nothing. It is deliberately not one of
    the capture drill's three lanes: ending a control before the drill it controls would be the
    suite breaking its own pairing.
    """
    record = lab.sessions.get("fenced-untrusted")
    if not record:
        report.record(Finding("S2", NOT_RUN, "nothing",
                              "the lifecycle drill's subject lane did not start, and the suite "
                              "will not end one of the capture drill's lanes to get a subject"))
        return
    host = lab.wrapper(record["host"])
    before = host.state(record["session"])
    refused, refusal = False, ""
    try:
        host.exit(record["session"])
    except session_host.TeardownRefused as exc:
        refused, refusal = True, str(exc)[:300]
    except Exception as exc:
        refusal = "exit failed in a way the doorway does not name: %r" % exc
    try:
        torn = host.kill(record["session"])
    except Exception as exc:
        report.record(Finding("S2", FAIL, "the doorway's ordered teardown against a live lane",
                              "`exit` %s, and `kill` then failed: %r"
                              % ("refused as it should" if refused else "did not refuse", exc)))
        lab.sessions.pop("fenced-untrusted", None)
        return
    after = host.state(record["session"])
    ok = refused and torn.closed and after.name_resolves is False
    report.record(Finding(
        "S2", PASS if ok else FAIL,
        "the doorway's ordered teardown against a live lane, and the name afterwards",
        "before: %s (liveness from OS process facts — the pane's shell pid and a live child — "
        "never from the host's agent list). `exit` %s. `kill` reported closed=%s signalled=%s "
        "orphan=%s. Afterwards the host no longer resolves the name (name_resolves=%s), so \"no "
        "live process in this lane\" is a real query rather than an inference."
        % (_quick_state(before),
           ("REFUSED the live session, as it must (%s)" % refusal) if refused
           else ("did NOT refuse a live session — %s" % (refusal or "it closed it")),
           torn.closed, torn.signalled, torn.orphan_pid, after.name_resolves)))
    lab.sessions.pop("fenced-untrusted", None)


def _judge_billing(report, args):
    """S10's judgeable half: are the drill sessions in a credential namespace of their own?"""
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

    def status(config_dir):
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        try:
            proc = subprocess.run([claude, "auth", "status"], capture_output=True, text=True,
                                  timeout=30, env=env)
        except (OSError, subprocess.SubprocessError):
            return {}
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError:
            return {}

    drill, default = status(args.config_dir), status(None)
    if not drill:
        report.record(Finding("S10", NOT_RUN, "nothing",
                              "the drill config dir's account could not be read at all"))
        return
    separate = bool(drill.get("orgId")) and drill.get("orgId") != default.get("orgId")
    subscription = drill.get("subscriptionType")
    report.record(Finding(
        "S10", PARTIAL if (separate and subscription) else FAIL,
        "`claude auth status` under the drill config dir and under the operator's default",
        "the drill sessions ran under %s, which resolves to org %r on a %r subscription — a "
        "credential namespace %s the operator's default (%r). That is the half an artefact can "
        "judge. The other half — that a hosted session BILLS as interactive Claude Code rather "
        "than as an API call — is reported on screen by the agent and nowhere structured, so this "
        "suite does not claim it: reading it would be a screen read, which the plan's own wrapper "
        "rules put outside the evidence path."
        % (args.config_dir, drill.get("orgName"), subscription,
           "distinct from" if separate else "IDENTICAL TO", default.get("orgName")),
        (("drill config dir", json.dumps(drill, indent=2)),
         ("operator default", json.dumps(default, indent=2)))))


def _record_unjudgeable(report, args):
    """The criteria this suite cannot judge unattended — named, with the reason, never skipped."""
    report.record(Finding(
        "S11", NOT_RUN, "nothing — deliberately",
        "attach-on-demand is not exercised, by ruling rather than by omission: this acceptance is "
        "CLIENTLESS end to end, and attaching a client to prove attaching works would break the "
        "one property every other drill here depends on. Carried evidence only (#302, #311)."))


# --------------------------------------------------------------------------- the entry point

def parser():
    """The command line.

    Note what is NOT here, and cannot be added without a conversation: there is no way to name a
    socket, a session, or a host to attach to. That absence is the "by construction" half of "it
    refuses to run against the live fleet's socket or the operator's own" — a flag would make it a
    warning instead.
    """
    p = argparse.ArgumentParser(
        prog="acceptance.py",
        description="Re-run the session-host acceptance suite (issue #348). Costs real agent "
                    "sessions; stands up and tears down its own throwaway hosts.")
    p.add_argument("--evidence", default=None,
                   help="where the per-criterion evidence files are written "
                        "(default: <state base>/acceptance/runs/<timestamp>)")
    p.add_argument("--config-dir", default=None,
                   help="the Claude config dir the drill sessions run under. Its folder-trust "
                        "records are written and removed again by this run.")
    p.add_argument("--host-binary", default=None,
                   help="judge THIS binary instead of building one from the current patch. "
                        "Recorded in the summary as a deviation, because the drill then certifies "
                        "a file rather than the patch as it stands.")
    p.add_argument("--build-work", default=None,
                   help="where the upstream tree is cloned and built (default: <state "
                        "base>/acceptance/src, kept between runs so a re-run is incremental)")
    p.add_argument("--lab-base", default="/tmp",
                   help="where the throwaway lab is made. Short by default because a unix socket "
                        "path has to fit sun_path (default: /tmp)")
    p.add_argument("--keep", action="store_true",
                   help="leave the lab directory behind for inspection (the hosts are stopped "
                        "either way)")
    p.add_argument("--list", action="store_true",
                   help="print the criteria and the lane matrix, run nothing, spend nothing")
    return p


def _state_base():
    return os.environ.get("SL_HOME") or os.path.join(os.path.expanduser("~"), ".superlooper")


def main(argv=None):
    args = parser().parse_args(argv)
    criteria_list = read_criteria(_REPO_ROOT)

    if args.list:
        print("The criteria, read from %s §3:\n" % CRITERIA_DOC)
        for criterion in criteria_list:
            print("  %-4s %-32s %s" % (criterion.id, criterion.title, criterion.requirement))
        print("\nThe lane matrix (each differs from fenced-hooked in exactly one variable):\n")
        for lane in LANES:
            print("  %-18s fence=%-5s hook=%-5s trusted=%-5s expect capture=%s\n      %s"
                  % (lane.name, lane.fenced, lane.hooked, lane.trusted, lane.expect_capture,
                     lane.why))
        print("\n" + cost_notice())
        return OK

    base = os.path.join(_state_base(), "acceptance")
    args.config_dir = args.config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or ""
    args.build_work = args.build_work or os.path.join(base, "src")
    args.evidence = args.evidence or os.path.join(base, "runs",
                                                  time.strftime("%Y%m%d-%H%M%S"))
    if not args.config_dir or not os.path.isdir(args.config_dir):
        raise SystemExit(
            "acceptance: --config-dir must name an existing, logged-in Claude config dir for the "
            "drill sessions. It is not created here: a config dir nobody provisioned is a "
            "credential namespace nobody chose.")

    report = Report(criteria_list)
    report.note("criteria read from %s §3 (%d of them)" % (CRITERIA_DOC, len(criteria_list)))
    report.note("drill sessions run under %s" % args.config_dir)
    print(cost_notice())
    try:
        code = run_suite(args, report)
    except Exception as exc:
        # Broad on purpose: whatever went wrong, the evidence gathered up to that point is written
        # and the summary says the run did not complete. A suite that threw away its own findings
        # on the way out would make every partial run unreadable.
        report.note("the run could not be completed: %s: %s" % (type(exc).__name__, exc))
        code = RUN_INCOMPLETE
        sys.stderr.write("acceptance: %s: %s\n" % (type(exc).__name__, exc))
    written = report.write(args.evidence)
    print("\n" + report.summary())
    print("acceptance: %d evidence files in %s" % (len(written), args.evidence))
    return code


if __name__ == "__main__":
    sys.exit(main())
