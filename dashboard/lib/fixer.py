"""The Deploy Fixer button (issue #141) — a LOCAL COMMAND execution, a sibling of Tidy
(``lib/tidy``), Restart (``lib/restart``) and Janitor (``lib/janitor``) in the dashboard's SECOND
button class (design record §2's verb amendment: local command execution, not a GitHub write).

When the board shows something wrong — a frozen repo, a wedged session, a park pile-up, an ALERT —
the owner's recourse used to be: open a terminal, find the repo, start a debug session by hand.
This is that, as one tap: compose a readout from what the UI is *currently showing* as unhealthy,
add whatever the owner types in his own words, and hand both to ``superlooper debug``, the engine's
owner-tap launch verb, which opens ONE fresh interactive Claude session running the ``sl-debugger``
skill.

**The no-AI-in-the-dashboard bright line is NOT crossed here** (owner ruling, 2026-07-15). This
module makes no model call and holds no standing seat. It assembles a string — exactly as
``actions.compose_briefing`` already does for Discuss — and executes a local CLI. The AI runs in the
launched session, in its own process, because a human tapped a button; the tap plus his note are his
word, the same way the Approve button records it. ``tests/test_fixer.py`` pins the absence of any
model/network call in this file, so a future edit that reaches for one fails CI.

**Why this is a THIN SHELL (issue #144).** It did not start that way. #141 shipped this module
driving the engine's launch shim DIRECTLY, hard-coding five engine internals that had no stability
contract toward the dashboard: the ``--cwd <dir> d<N>`` invocation form, the
``<state_home>/briefs/<id>.md`` path convention, the ``SL_RUN_ROOT``/``SL_PANE``/``SL_MODEL``
handshake, ``state/runner.anchor.json``'s shape, and ``worker.d<N>.lock`` + pid liveness as "a
debugger is already running". The engine was free to change any of them, and because no test may
reach a real shim, this suite would have stayed green while production silently broke.

Two of those five could not merely be COPIED correctly — they were unreachable from outside the
engine at all:

1. *A reused id.* The ``d<N>`` namespace is the watchdog's, allocated from ``state/watchdog.json`` ▸
   ``next_debugger``. A dashboard launch could read that counter but must never write it (the
   watchdog's anti-storm rails live in the same document, behind the engine's atomic lock — §6
   machinery the center must not add to the runner), so it could only step AROUND the watchdog's
   next id, never claim it. A later watchdog launch would then reuse the id and overwrite the brief.
2. *A simultaneous launch.* Both sides checked ``worker.d*.lock``, but ``start-session.sh`` creates
   that lock a beat AFTER the launch begins — so two checks landing in the same few seconds both saw
   "nobody home" and both launched. Its correlation is worst exactly when it matters: a wedged
   runner is both what trips the watchdog and what makes the owner tap this button.

``superlooper debug`` closes both by allocating and launching while holding the watchdog's OWN lock
— which the watchdog holds across its entire check, launch subprocess included. So this module now
keeps only what is genuinely the dashboard's (it is the thing that knows what the board is showing)
and shells the CLI for everything else, exactly as ``lib/restart`` shells ``request-restart``.

What remains here:

* **The trouble readout.** :func:`trouble_context` derives what the UI is currently rendering as
  unhealthy from the same snapshot the pixels came from (design record B.1: semantics server-side),
  and :func:`compose_context` turns it into the markdown the engine frames into the brief.
* **The dashboard's OWN launch log.** Beside ``desk.json``, never inside a loop state home
  (decision B.4). The engine journals the tap in the tower log; this is the center's own record of
  what its own button did, including the failures.
* **What became of the last tap** (:func:`last_launch`, issue #458). A pure read of the ENGINE's
  ``debug_launch`` acts — the records the snapshot assembler already holds — turned into the one
  sentence the trouble banner binds beside the button. On 2026-08-26 a tap during a Claude-auth
  outage was consumed, attempted, failed and journaled, and the board said nothing: the only surface
  that had ever named a result was the note box, and a close or a reload threw it away. Deriving the
  answer from the journal rather than from this module's own log is what keeps the banner and the
  tower log from being able to disagree about one launch — and what keeps the fix free of any new
  read.

Bright lines this module encodes (not conveniences):

* **Fail closed, always.** An unwatched repo, a repo not on the field — refused BEFORE any
  subprocess. Everything downstream is the engine's own honest refusal, surfaced in its words.
* **This adapter never raises into a caller.** A missing binary, a timeout, a killed process — all
  become an honest ``{"ok": false, "error": …}`` (mirrors ``restart._run`` / ``tidy._run``).
* **The launch never touches labels.** It starts a session; that is the whole verb.
* **The launched session's authority is the sl-debugger skill's own** (its human-present contract).
  This module reports what the board shows and gets out of the way — it never dictates a repair.
"""
import json
import math
import os
import subprocess
import time
from pathlib import Path

import flights
import session_window

# Per-call hard timeout (seconds) for the CLI, which opens a session window and VERIFIES delivery. A
# module constant, not a literal, so a test can shrink it and trip the timeout path (mirrors
# restart._DEFAULT_TIMEOUT). Matches the engine's own launch timeout.
_DEFAULT_TIMEOUT = 180

# A note is the owner's words, not a manuscript; the board readout is a readout, not a corpus. Both
# are bounded HERE as well as in the engine — the engine bounds what it will frame into a brief, and
# this bounds what a browser POST can push through a subprocess argument in the first place.
NOTE_MAX = 4000
CONTEXT_MAX = 8000

_LOG_FILENAME = "fixer-log.jsonl"


# =============================== the trouble the UI is showing (pure) ===============================

# The flight stages that mean "this flight is stuck", as the UI renders them. HOLDING is
# deliberately absent: a flight sequenced behind another lane is a designed-safe wait, not trouble —
# pointing a debugger at it would be pointing it at the system working correctly.
_STUCK_STAGES = (flights.PARKED, flights.AWAITING, flights.SESSION_FROZEN, flights.STRANDED)

# Plain sentences for the repo-level conditions — the same vocabulary the trouble banner uses, so
# the words the debugger reads are the words the owner just read.
_CONDITION_TEXT = {
    "runner-down": "RUNNER DOWN — the runner's heartbeat is stale, so nothing on the board can be trusted",
    "alert": "ALERT — the runner declared a persistent fault (state/ALERT is present)",
    flights.MERGES_FREEZE: "LANDINGS PAUSED — merges are frozen (a repair flight is out)",
}

_FLIGHT_TEXT = {
    flights.PARKED: "parked — the machine gave up on it",
    flights.AWAITING: "awaiting an owner decision",
    flights.SESSION_FROZEN: "session frozen — a dead session still on the field",
    flights.STRANDED: "stranded at the gate — the report is filed but the gate never landed it",
    "spinning": "spinning — a live session making no progress",
}


def _find_repo(snapshot, repo_slug):
    for repo in (snapshot or {}).get("repos", []) or []:
        if repo.get("slug") == repo_slug:
            return repo
    return None


def trouble_context(snapshot, repo_slug):
    """What the dashboard is CURRENTLY rendering as unhealthy for ``repo_slug`` — the machine-readable
    half of the fixer's prompt, assembled from the same snapshot the pixels came from (design record
    B.1: semantics server-side). ``None`` when the repo isn't on the field — the caller refuses; it
    never invents a context for a repo it cannot see.

    Items are ranked worst-first by the UI's own condition table. A healthy field yields an EMPTY
    item list and ``healthy: True`` — an honest answer the prompt states plainly, never a fabricated
    symptom (the owner may tap because he saw something the board didn't)."""
    repo = _find_repo(snapshot, repo_slug)
    if repo is None:
        return None

    items = []
    if repo.get("runner_down"):
        items.append({"kind": "runner-down", "text": _CONDITION_TEXT["runner-down"],
                      "heartbeat_age": repo.get("heartbeat_age")})
    if repo.get("alert") is not None:
        items.append({"kind": "alert", "text": _CONDITION_TEXT["alert"]})
    if repo.get("merges_frozen") is not None:
        items.append({"kind": flights.MERGES_FREEZE, "text": _CONDITION_TEXT[flights.MERGES_FREEZE]})

    for f in repo.get("flights", []) or []:
        stage = f.get("stage")
        kind = stage if stage in _STUCK_STAGES else ("spinning" if f.get("spinning") else None)
        if kind is None:
            continue
        items.append({"kind": kind, "num": f.get("num"), "title": f.get("title"),
                      "text": _FLIGHT_TEXT.get(kind, kind), "stage": stage,
                      "liveness": f.get("liveness"), "memo": f.get("memo"), "pr": f.get("pr"),
                      "attempt": f.get("attempt")})

    # Worst-first by the UI's OWN ranking, so the debugger reads the board in the same order the
    # owner does. Python's sort is stable, so same-rank items keep snapshot order (the field's).
    items.sort(key=lambda i: -flights.condition_rank(i["kind"]))
    state = repo.get("state") or {}
    return {"slug": repo_slug, "name": repo.get("name") or repo_slug,
            "state": state.get("state"), "level": state.get("level"),
            "healthy": not items, "items": items}


def trouble_kinds(ctx):
    """The bare condition kinds, for the launch record — what the UI was showing at tap time."""
    return [i["kind"] for i in (ctx or {}).get("items", [])]


# =============================== the context (pure string assembly — no AI) ===============================

def _bounded(text, limit, what):
    """A string, trimmed and BOUNDED. Truncation is STATED in the text so the session reads a
    visible cut rather than silently losing the end of an instruction."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + ("\n\n[%s truncated at %d characters]" % (what, limit))
    return text


def _item_line(item):
    num = item.get("num")
    head = ("SL-%s — " % num) if num else ""
    line = "- " + head + item.get("text", item.get("kind", "?"))
    bits = []
    if item.get("heartbeat_age") is not None:
        bits.append("last heartbeat %ds ago" % int(item["heartbeat_age"]))
    if item.get("attempt") and item["attempt"] > 1:
        bits.append("attempt %d" % item["attempt"])
    if item.get("pr"):
        bits.append("PR #%s" % item["pr"])
    if bits:
        line += " (" + "; ".join(bits) + ")"
    memo = (item.get("memo") or "").strip()
    if memo:
        line += "\n    its own memo: " + memo
    return line


def compose_context(ctx):
    """The dashboard's half of the launched session's opening message: the specific things the UI is
    rendering as unhealthy RIGHT NOW, worst first, in the same words the owner just read.

    This is the piece that is genuinely the dashboard's — nobody else knows what board the owner was
    looking at when he tapped. The engine (``superlooper debug``) frames it, together with the note,
    into the brief, and owns everything about the session itself: the invocation, the mode, the
    patient's paths, the skill's contract.

    Deliberately a READOUT, never a work order. It does not tell the debugger how to work — its
    authority, its ladder and its exclusions are the sl-debugger skill's own contract, and a context
    that started dictating repairs would be this button quietly rewriting the skill."""
    ctx = ctx or {}
    lines = ["## What the dashboard is showing as unhealthy, right now", ""]
    items = ctx.get("items") or []
    if items:
        lines.append("This is the board he was looking at when he tapped — worst first:")
        lines.append("")
        lines += [_item_line(i) for i in items]
    else:
        lines.append("**Nothing** — the board reads healthy: no stale heartbeat, no ALERT, no "
                     "freeze, no parked or frozen flights. He tapped anyway, so trust his note "
                     "over the board and start by finding what the board is failing to show.")
    return _bounded("\n".join(lines), CONTEXT_MAX, "board readout")


# =============================== the launch record ===============================

def default_log_path():
    """The dashboard's OWN launch log: ``<base>/command-center/fixer-log.jsonl``, beside
    ``desk.json`` — where ``base`` is ``$SL_HOME`` or ``~/.superlooper``. Decision B.4 (see
    ``lib/desk``): the center's own facts live BESIDE the loop state homes it reads, never inside
    one. The engine journals the tap in the tower log; this is the center's record of its own
    button, failures included."""
    base = os.environ.get("SL_HOME") or os.path.expanduser("~/.superlooper")
    return Path(base) / "command-center" / _LOG_FILENAME


# =============================== the verb ===============================

def _binary(configured):
    """The superlooper CLI to run: the ``SL_SUPERLOOPER`` env override wins over the configured path
    (config's ``superlooper_cli``), mirroring ``lib/restart``/``lib/tidy``'s precedence so every
    local-command button and the tests agree on binary resolution."""
    return os.environ.get("SL_SUPERLOOPER") or configured


def _run(binary, args, stdin_text=None, timeout=None):
    """Run ``<binary> <args>``; returns ``(rc, stdout, stderr)``. NEVER raises: a timeout, a missing
    binary, or any OSError is caught and returned as a nonzero rc with empty stdout so the caller
    fails closed (mirrors ``restart._run``)."""
    try:
        proc = subprocess.run([binary, *args], input=stdin_text, capture_output=True, text=True,
                              timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"            # conventional timeout rc
    except (OSError, ValueError):
        return 127, "", "command not found"    # missing binary / bad invocation


def parse_result(stdout):
    """The single JSON object ``superlooper debug --json`` prints, or ``None`` when stdout carries no
    parseable object (a missing/crashed CLI). Pure and unit-tested, so the coupling to the CLI's
    ``--json`` contract is pinned by a test rather than discovered in production."""
    txt = (stdout or "").strip()
    if not txt:
        return None
    try:
        val = json.loads(txt)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _error(rc, stderr, binary):
    """A plain, honest failure message for a CLI that didn't answer — what the UI shows instead of a
    fake success. Names the CLI on a missing binary so the operator knows exactly what to fix."""
    stderr = (stderr or "").strip()
    if rc == 127:
        return ("could not run the superlooper CLI at %s — is it installed? "
                "(set 'superlooper_cli' in config.json)" % binary)
    if rc == 124:
        return "the launch timed out — no session was confirmed"
    return stderr or ("superlooper debug failed (exit %d) — no session was confirmed" % rc)


class Fixer:
    """The Deploy Fixer verb, bound to the configured superlooper CLI path, an allow-list mapping
    each WATCHED repo slug to its checkout path, and the operator name the launch is recorded under.

    Two methods back the button's two-step flow: :meth:`preflight` reports whether a debugger is
    already live and what trouble would ride into the prompt, and writes NOTHING; :meth:`execute`
    hands the note and the board readout to the engine (only after the owner's in-box confirm).
    Every result is honest — ``ok`` is the engine's real verdict on delivery, never a pretend one."""

    def __init__(self, superlooper_cli, repo_paths, operator=None, timeout=None, log_path=None,
                 now=None):
        self._binary = superlooper_cli
        self._paths = dict(repo_paths or {})
        self._operator = operator if (isinstance(operator, str) and operator.strip()) else None
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._log_path = str(log_path) if log_path else str(default_log_path())
        self._now = now if now is not None else time.time

    # ---- helpers ----

    def _refuse(self, verb, error, **extra):
        out = {"ok": False, "verb": verb, "error": error, "live": None, "live_id": None}
        out.update(extra)
        return out

    def _stamp(self):
        ts = _usable_ts(self._now() if callable(self._now) else self._now)
        return ts if ts is not None else time.time()

    def _record(self, ts, repo, fixer_id, note, ctx, ok, error=None):
        """Append one launch record to the dashboard's own log — timestamped, with the note and the
        trouble that was on screen, so a later reader can see a fixer ran and WHY. A failed launch is
        recorded too: the honest history is the point, and an attempt that failed is exactly what a
        later reader needs. A write failure never breaks the owner's tap — this is bookkeeping."""
        rec = {"ts": ts, "repo": repo, "id": fixer_id, "operator": self._operator or "the owner",
               "note": note or "", "trouble": trouble_kinds(ctx), "ok": bool(ok)}
        if error:
            rec["error"] = error
        try:
            p = Path(self._log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(str(p), "a") as f:
                f.write(json.dumps(rec) + "\n")
        except (OSError, ValueError, TypeError):
            pass

    def _invoke(self, path, extra, stdin_text=None):
        """Shell ``superlooper debug`` and return its parsed body, or an honest failure dict when the
        CLI could not answer at all. The CLI's own refusals (a live debugger, no pane, a launch that
        did not land) are WELL-FORMED bodies at rc 1 — surfaced as-is, in the engine's words, never
        flattened into a generic error (``lib/restart``'s dead-runner discipline)."""
        binary = _binary(self._binary)
        rc, out, err = _run(binary, ["debug", "--repo", path, "--json", *extra],
                            stdin_text=stdin_text, timeout=self._timeout)
        parsed = parse_result(out)
        if parsed is not None:
            return parsed
        return {"ok": False, "live": None, "live_id": None, "error": _error(rc, err, binary)}

    # ---- step 1: the preflight (writes nothing) ----

    def preflight(self, repo, snapshot):
        """Is a debugger already on this patient, and what would ride into its prompt? Read-only: no
        brief, no launch, no record. The dialog decides what to show from this.

        Liveness is the ENGINE's answer (``superlooper debug --check``), not a lock file this module
        interprets — that convention is the engine's to change."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("fixer-check", "unknown repo")
        ctx = trouble_context(snapshot, repo)
        if ctx is None:
            return self._refuse("fixer-check", "that repo is not on the field")

        res = self._invoke(path, ["--check"])
        res["verb"] = "fixer-check"
        res["trouble"] = ctx
        if not res.get("ok"):
            return res
        res["live"] = bool(res.get("live"))
        return res

    # ---- step 2: the launch ----

    def execute(self, repo, note, snapshot):
        """Hand the owner's note and the board readout to ``superlooper debug``, which launches ONE
        interactive sl-debugger session. Refuses locally — changing nothing, running nothing — on an
        unwatched repo or a repo that isn't on the field; every other refusal is the engine's own,
        surfaced in its words. ``ok`` is the engine's verdict: true only when the launch shim
        VERIFIED the tab took the prompt."""
        path = self._paths.get(repo)
        if path is None:
            return self._refuse("fixer", "unknown repo")

        ctx = trouble_context(snapshot, repo)
        if ctx is None:
            return self._refuse("fixer", "that repo is not on the field")

        extra = ["--context-file", "-", "--source", "command-center"]
        # A blank note is sent as NO note, never as whitespace: the engine says "no note" in its own
        # words, and an argument of spaces would land where the owner's words go.
        sent_note = _bounded(note, NOTE_MAX, "note")
        if sent_note:
            extra += ["--note", sent_note]
        if self._operator:
            extra += ["--operator", self._operator]

        res = self._invoke(path, extra, stdin_text=compose_context(ctx))
        res["verb"] = "fixer"
        ts = self._stamp()
        if res.get("ok"):
            self._record(ts, repo, res.get("id"), sent_note, ctx, True)
            res["trouble"] = trouble_kinds(ctx)
            return res
        self._record(ts, repo, res.get("id"), sent_note, ctx, False, error=res.get("error"))
        return res


# =============================== what became of the tap (issue #458) ===============================
#
# The tap has to RENDER ITS OUTCOME. On 2026-08-26 the owner tapped Deploy Fixer during a
# Claude-auth outage: the request was consumed, the engine attempted the launch, the attempt failed,
# the failure was journaled — and the dashboard showed nothing. The only surface that had ever named
# a result was the note box itself, and a close, a page reload or a superseding open threw it away;
# "seems to have done nothing" about a button that did something is exactly the dishonest surface
# this project keeps killing.
#
# So the outcome is derived from the ENGINE's own ``debug_launch`` acts, which the snapshot
# assembler already reads for the tower log — no new read anywhere, and no second source of truth
# that could disagree with the tower's own sentence about the same launch. Pure and clock-free
# (design record B.1: semantics server-side, so the JS binds a finished sentence and derives
# nothing); the caller stamps the display time from the record's ``ts``.

LAUNCH_ACT = "debug_launch"

# The three states the button's surface can be in. PENDING is the one that must exist: the engine
# journals an outcome only AFTER the launch has resolved, and a later engine may journal a word this
# build has never met. Reading either as "failed" claims something we cannot support, and rendering
# neither is the silence this issue exists to kill.
LAUNCHED = "launched"
FAILED = "failed"
PENDING = "pending"

# The engine's own words for the two outcomes it currently journals (skill/bin/superlooper ▸
# cmd_debug). Anything else — a missing outcome, a blank one, a word from a newer engine — is
# PENDING, in that engine's own word.
_ENGINE_LAUNCHED = "launched"
_ENGINE_FAILED = "launch_failed"

_HEADLINES = {LAUNCHED: "FIXER DEPLOYED", FAILED: "FIXER DID NOT LAUNCH",
              PENDING: "FIXER OUTCOME UNKNOWN"}

# A journaled reason is a message, not a manuscript: a shim that hands back a traceback must not be
# able to take over the banner it is being reported in.
REASON_MAX = 200


def _one_line(text, limit=REASON_MAX):
    """The first non-empty line of a journaled field, trimmed and bounded — ``""`` when the field
    says nothing. The banner is one line; a multi-line stderr would otherwise break it apart."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return (line[:limit - 1].rstrip() + "…") if len(line) > limit else line
    return ""


def launch_outcome(rec):
    """Which of the three states a ``debug_launch`` record is in — :data:`LAUNCHED`,
    :data:`FAILED` or :data:`PENDING`. PURE.

    Exported because TWO surfaces gloss the same record: the trouble banner (through
    :func:`last_launch`) and the tower log (``lib/tower``). Making the call twice is how they end up
    disagreeing about one launch in the same 2-second frame — which is exactly what happened before
    issue #458, when the tower read every word that was not the literal ``launched`` as "did not
    launch" and the banner had learned to say "not yet known"."""
    recorded = _one_line((rec or {}).get("outcome"), 60)
    if recorded == _ENGINE_LAUNCHED:
        return LAUNCHED
    return FAILED if recorded == _ENGINE_FAILED else PENDING


def _usable_ts(ts):
    """A journal ``ts`` that arithmetic can safely touch, else ``None``. The journal is append-only
    text a crashing runner writes, so every shape it can carry has to answer rather than raise:
    ``json.loads`` accepts ``NaN``/``Infinity``, AND parses an arbitrarily large integer for which
    ``math.isfinite`` itself raises ``OverflowError`` (fresh-agent review, issue #458). One corrupt
    line must never take down the 2-second poll — least of all on the record this surface exists to
    render."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    try:
        return ts if math.isfinite(ts) else None
    except OverflowError:
        return None


def _blank_launch():
    """The absent block — every key present, so a caller never branches on a missing one (the shape
    ``lib/session_window`` holds for the same reason)."""
    return {"present": False, "outcome": None, "id": None, "lane": None, "operator": None,
            "ts": None, "reason": "", "recorded": "", "headline": "", "text": "", "session": False}


def _launch_sentence(outcome, name, operator, reason, recorded, lane):
    """The one plain sentence the surface binds, composed HERE so the wording is pinned by a test and
    the pixels carry no opinion about what a launch outcome means."""
    if outcome == LAUNCHED:
        by = (" by %s" % operator) if operator else ""
        if lane:
            return "%s launched%s — it has its own session window." % (name, by)
        # The engine's word stands — a session launched — but the record names no seat this board
        # can point at, so there is no way in to offer. Saying so is the honest half of that: a
        # sentence that promised a window while the button was missing would be the same over-claim
        # this issue exists to kill (fresh-agent review, P1).
        return ("%s launched%s — it has its own session window, but the journal records no seat "
                "this board can open." % (name, by))
    if outcome == FAILED:
        return "%s did not launch — %s." % (name, reason)
    if recorded:
        # An outcome a newer engine invented. Quote it rather than translate it: this build cannot
        # honestly call it a launch or a failure, and pretending either way is the worse answer.
        return ("%s — outcome not yet known (the engine recorded “%s”); nothing has "
                "confirmed a session." % (name, recorded))
    return ("%s — the launch is recorded with no outcome beside it; nothing has confirmed a "
            "session." % name)


def last_launch(records):
    """The NEWEST fixer launch in ``records`` and what became of it — the block the tap surface
    binds so a tap is never followed by silence.

    ``records`` is one repo's journal slice, exactly as the assembler already read it. The journal is
    append-only, so the last ``debug_launch`` line in file order is the newest one (the same rule
    ``server._last_ts`` uses); a rewritten ``ts`` cannot reorder it.

    Three outcomes, and none of them is nothing:

    ``launched``   the engine confirmed a session and named it. ``session`` is True — and ONLY here
                   — because that is the one state in which offering to open a window is honest
    ``failed``     the engine said the launch did not land, in the words IT journaled. A record with
                   no reason still says it failed, with an honest stand-in rather than a blank
    ``pending``    the outcome is not one this build can read: absent, blank, or a word a newer
                   engine invented. Rendering it as a failure would claim what we cannot support

    ``present`` is False when this repo has never had a tap — the honest absence of a launch, which
    is not the same as saying nothing about one that happened.
    """
    rec = None
    for r in records or []:
        if isinstance(r, dict) and r.get("act") == LAUNCH_ACT:
            rec = r                                   # file order: the last match is the newest
    if rec is None:
        return _blank_launch()

    recorded = _one_line(rec.get("outcome"), 60)
    outcome = launch_outcome(rec)

    sid = rec.get("id")
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else None
    operator = rec.get("operator")
    operator = operator.strip() if isinstance(operator, str) and operator.strip() else None
    # A launch that failed with nothing said is still a launch that failed — the engine's own
    # fallback wording, so the banner and the tower log read the same on the same record.
    reason = _one_line(rec.get("error")) if outcome == FAILED else ""
    if outcome == FAILED and not reason:
        reason = "no session was confirmed"
    ts = _usable_ts(rec.get("ts"))

    name = ("Fixer %s" % sid) if sid else "The fixer"
    # The seat the open-session affordance may target, canonicalised by the SAME fence the route
    # validates against (``lib/session_window.debugger_lane_id``) — one definition of what a fixer
    # seat is, so the surface can never offer a button the endpoint behind it refuses. ``id`` stays
    # the journal's own string (that is what the record says); ``lane`` is what may go on a wire.
    lane = session_window.debugger_lane_id(sid) if outcome == LAUNCHED else None
    return {"present": True, "outcome": outcome, "id": sid, "lane": lane, "operator": operator,
            "ts": ts, "reason": reason, "recorded": recorded, "headline": _HEADLINES[outcome],
            "text": _launch_sentence(outcome, name, operator, reason, recorded, lane),
            # The open-session affordance the flight card already has, offered for the fixer's own
            # d<N> lane — but only on a launch the engine CONFIRMED and named with a seat this board
            # can actually open. On a failure it would point at a window that was never opened; on a
            # corrupt id it would be a dead control that 400s when tapped.
            "session": lane is not None}
