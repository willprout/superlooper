"""The Deploy Fixer button (issue #141) — a LOCAL SESSION LAUNCH, a sibling of Tidy (``lib/tidy``),
Restart (``lib/restart``) and Janitor (``lib/janitor``) in the dashboard's SECOND button class
(design record §2's verb amendment: local command execution, not a GitHub write).

When the board shows something wrong — a frozen repo, a wedged session, a park pile-up, an ALERT —
the owner's recourse used to be: open a terminal, find the repo, start a debug session by hand.
This is that, as one tap: compose a prompt from what the UI is *currently showing* as unhealthy plus
whatever the owner types in his own words, and hand it to the engine's existing launch shim, which
opens ONE fresh interactive Claude session running the ``sl-debugger`` skill.

**The no-AI-in-the-dashboard bright line is NOT crossed here** (owner ruling, 2026-07-15). This
module makes no model call and holds no standing seat. It assembles a string — exactly as
``actions.compose_briefing`` already does for Discuss — and executes a local script. The AI runs in
the launched session, in its own process, because a human tapped a button; the tap plus his note are
his word, the same way the Approve button records it. ``tests/test_fixer.py`` pins the absence of any
model/network call in this file, so a future edit that reaches for one fails CI.

What this module knows about the engine (and why each piece is here rather than behind a CLI verb):
the engine exposes no owner-tap "launch a debugger" subcommand — its only debugger launch is
``superlooper watchdog``, the UNATTENDED, episode-gated fallback, whose whole contract (grace
windows, authority tiers, once-per-incident) is wrong for a human standing at the keyboard. So this
adapter drives the shim the way the engine's own watchdog drives it (``superlooper`` ▸
``_watchdog_launch``), reusing its conventions rather than inventing parallel ones:

* the ``--cwd <dir> d<N>`` invocation — a session with no worktree and no branch, launched in an
  existing dir; the shim REFUSES an id that isn't ``^[ad][0-9]+$`` through this path;
* the brief at ``<state_home>/briefs/<id>.md`` — the shim aborts without it, and
  ``start-session.sh`` cats it into the interactive agent as its opening message, which is why the
  owner's note lands there VERBATIM;
* the ``SL_RUN_ROOT``/``SL_PANE``/``SL_AGENT`` handshake, and the pinned ``SL_LAUNCH_VERIFY_SECONDS``;
* ``worker.d<N>.lock`` naming a live pid as "a debugger is already on this patient" — the SAME file
  the watchdog checks before its own launch, so the two never launch past each other: whichever
  starts first is seen by the other.

Bright lines this module encodes (not conveniences):

* **Fail closed, always.** An unwatched repo, an unresolvable shim, no cmux pane, a corrupt anchor —
  each refuses BEFORE anything launches, with the reason in plain words. Nothing half-launches.
* **Single-flight.** Never two debuggers on one patient. A live ``worker.d*.lock`` blocks the tap.
* **This adapter never raises into a caller.** A missing script, a timeout, a killed process — all
  become an honest ``{"ok": false, "error": …}`` (mirrors ``restart._run`` / ``tidy._run``).
* **The launch never touches labels.** It starts a session; that is the whole verb.
* **The launched session's authority is the sl-debugger skill's own** (its human-present contract).
  This module composes context and gets out of the way — it never dictates a repair.
"""
import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import flights

# Per-call hard timeout (seconds) for the shim, which opens a cmux tab and VERIFIES delivery. A
# module constant, not a literal, so a test can shrink it and trip the timeout path (mirrors
# restart._DEFAULT_TIMEOUT). Matches the engine's own WATCHDOG_LAUNCH_TIMEOUT.
_DEFAULT_TIMEOUT = 180

# The shim's delivery-verification window, PINNED rather than inherited. The engine pins it for the
# same reason (its review's P1-1): an ambient large SL_LAUNCH_VERIFY_SECONDS — a debugging export, a
# LaunchAgent env — would let a launch outlive our timeout, so we would report a failed attempt
# while the tab delivers late and a REAL session starts. That is precisely the double-launch the
# single-flight check exists to prevent. 30s is the script's own default, far under the timeout.
_VERIFY_SECONDS = "30"

# The debugger seat is hired judgment, like the answerer — the engine gives it the same default.
_DEFAULT_MODEL = "opus[1m]"

_ID_RE = re.compile(r"^d([0-9]+)$")

# A note is the owner's words, not a manuscript. Bounded so a stray paste can never drown the
# machine context it rides with (the shim cats the whole brief into the agent's opening message).
_NOTE_MAX = 4000

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


# =============================== the prompt (pure string assembly — no AI) ===============================

def _note_text(note):
    """The owner's note, trimmed and BOUNDED. Blank/absent reads as "no note" — stated plainly in
    the prompt rather than left as an empty section the session would read as a lost instruction."""
    note = (note or "").strip()
    if not note:
        return None
    if len(note) > _NOTE_MAX:
        note = note[:_NOTE_MAX].rstrip() + "\n\n[note truncated at %d characters]" % _NOTE_MAX
    return note


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


def compose_prompt(ctx, note, repo_path, state_home, operator=None):
    """The launched session's ENTIRE opening message, assembled by string concatenation from facts
    the dashboard already has: the sl-debugger invocation, who launched it and how, the owner's note
    VERBATIM, and the machine context (repo, checkout, state home, and the specific items the UI is
    rendering as unhealthy right now).

    Deliberately does NOT tell the debugger how to work. Its authority, its ladder and its
    exclusions are the skill's own contract (this issue defines the LAUNCH, never the debugger's
    behavior) — a prompt that started dictating repairs would be this button quietly rewriting the
    skill. The one thing it must assert is the MODE: a person is at the keyboard, so the skill's
    human-present contract applies, not the watchdog's unattended one."""
    who = operator if (isinstance(operator, str) and operator.strip()) else "the owner"
    ctx = ctx or {}
    lines = [
        "# sl-debugger session — launched from the command-center dashboard",
        "",
        "Use the **sl-debugger** skill.",
        "",
        "%s tapped **Deploy Fixer** on the command center just now, because the dashboard was "
        "showing trouble on this loop instance. This is a **human-present** session: %s is at the "
        "keyboard right now and can answer you. The skill's human-present contract applies — its "
        "unattended-invocation contract (the mechanical watchdog's) does NOT: nobody launched this "
        "on a timer, a person did." % (who, who),
        "",
        "## What %s typed" % who,
        "",
    ]
    typed = _note_text(note)
    lines.append(typed if typed else
                 "*(no note — he tapped Deploy Fixer without typing one, so the board below is the "
                 "whole of what he saw.)*")

    lines += ["", "## The patient", "",
              "- Repo under loop management: **%s**" % ctx.get("slug", "?"),
              "- Working copy (your cwd): `%s`" % repo_path,
              "- State home: `%s` (journal, per-issue state, liveness markers, heartbeat, ALERT)"
              % state_home,
              "", "## What the dashboard is showing as unhealthy, right now", ""]

    items = ctx.get("items") or []
    if items:
        lines.append("This is the board he was looking at when he tapped — worst first:")
        lines.append("")
        lines += [_item_line(i) for i in items]
    else:
        lines.append("**Nothing** — the board reads healthy: no stale heartbeat, no ALERT, no "
                     "freeze, no parked or frozen flights. He tapped anyway, so trust his note "
                     "over the board and start by finding what the board is failing to show.")

    lines += ["", "---", "",
              "Start with the skill's read-only health readout before changing anything, and ask "
              "%s whatever you need — he is right here." % who]
    return "\n".join(lines)


# =============================== the launch command (pure construction) ===============================

def launch_script_for(superlooper_cli):
    """The engine's launch shim: ``launch-session.sh``, a SIBLING of the ``superlooper`` CLI in the
    engine's ``bin/``. Derived from the ONE configured path (config's ``superlooper_cli``) so the
    dashboard needs no second knob for a second engine entry point. Expanded to an absolute path — a
    literal ``~`` never resolves inside a subprocess."""
    cli = os.path.abspath(os.path.expanduser(str(superlooper_cli)))
    return os.path.join(os.path.dirname(cli), "launch-session.sh")


def resolve_script(configured):
    """The shim to run: the ``SL_LAUNCH_SESSION`` env override wins over the configured path. This is
    the SAME variable the engine's own watchdog resolves the shim by, so the dashboard and the
    engine agree on the override — and ``tests/conftest.py`` can point the whole suite at an absent
    path fail-closed (mirrors ``lib/tidy``/``lib/restart``'s ``SL_SUPERLOOPER`` precedence)."""
    return os.environ.get("SL_LAUNCH_SESSION") or configured


def launch_argv(script, repo_path, fixer_id):
    """The shim's ``--cwd`` form — the engine's own contract for a debugger session: launch in an
    existing dir, no worktree, no branch. The shim refuses an id that isn't ``^[ad][0-9]+$`` here,
    so the ``d<N>`` shape is load-bearing, not cosmetic."""
    return [script, "--cwd", repo_path, fixer_id]


def launch_env(base_env, state_home, pane, model=None, agent=None):
    """The environment the shim reads. ``SL_RUN_ROOT`` and ``SL_PANE`` are REQUIRED (it aborts
    without either). The ambient env is carried through — the shim needs git/cmux on PATH — with our
    keys layered on top, and the verify window PINNED (never inherited; see ``_VERIFY_SECONDS``)."""
    env = dict(base_env)
    env.update({
        "SL_RUN_ROOT": str(state_home),
        "SL_PANE": str(pane),
        "SL_MODEL": model or _DEFAULT_MODEL,
        "SL_EFFORT": "",
        "SL_AGENT": agent if agent in ("claude", "codex") else "claude",
        "SL_LAUNCH_VERIFY_SECONDS": _VERIFY_SECONDS,
    })
    return env


def next_fixer_id(seen_ids):
    """The next debugger id: one past the highest ``d<N>`` ever seen in this state home. Never
    re-uses a number even across a gap — a prior session's brief IS the record of what it was told,
    and clobbering it would rewrite history. Garbage and non-debugger ids (``i44``, ``a2``) are
    ignored, never a crash."""
    highest = 0
    for name in seen_ids or []:
        m = _ID_RE.match(str(name))
        if m:
            highest = max(highest, int(m.group(1)))
    return "d%d" % (highest + 1)


# =============================== filesystem reads (fail closed, never raise) ===============================

def _pid_alive(pid):
    """Mirrors the engine's own ``_pid_alive``: a permission error means it exists but isn't ours."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, TypeError, OverflowError):
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _live_lock(path):
    """A lock file naming a LIVE pid. Unreadable/garbage content reads as dead (reclaimable) — the
    engine's exact rule, so both sides agree on what "a debugger is running" means."""
    try:
        pid = Path(path).read_text().strip()
    except OSError:
        return False
    return pid.isdigit() and _pid_alive(int(pid))


def _state_dir(state_home):
    return Path(state_home) / "state"


def live_fixer_id(state_home):
    """The id of a live debugger session, or ``None``. Any ``worker.d<N>.lock`` naming a live pid —
    ``start-session.sh``'s per-id singleton, and the SAME signal the engine's watchdog reads before
    its own launch. So a watchdog session blocks a tap and a tapped session blocks the watchdog:
    never two debuggers on one patient."""
    try:
        names = os.listdir(str(_state_dir(state_home)))
    except OSError:
        return None
    for name in sorted(names):
        if name.startswith("worker.d") and name.endswith(".lock"):
            if _live_lock(_state_dir(state_home) / name):
                return name[len("worker."):-len(".lock")]
    return None


def _seen_ids(state_home):
    """Every debugger id this state home has ever handed out, from the two places the engine leaves
    them: composed briefs and session locks (live or stale)."""
    seen = []
    try:
        seen += [n[:-3] for n in os.listdir(str(Path(state_home) / "briefs")) if n.endswith(".md")]
    except OSError:
        pass
    try:
        seen += [n[len("worker."):-len(".lock")]
                 for n in os.listdir(str(_state_dir(state_home)))
                 if n.startswith("worker.") and n.endswith(".lock")]
    except OSError:
        pass
    return seen


def resolve_pane(state_home):
    """The cmux pane the fixer's tab is born in, resolved the way the ENGINE resolves it: an explicit
    ``$SL_PANE``, else the RUNNER's recorded anchor (``state/runner.anchor.json`` — present while a
    runner is live OR crashed; a clean stop clears it). ``None`` when nothing resolves, which is a
    refusal, never a guess: the shim would abort anyway, and a launch that half-happens is worse
    than one that plainly didn't.

    Note the crashed-runner case is exactly the one this button is for — a dead runner still leaves
    its anchor, so the fixer can still be born in the loop's own pane."""
    pane = (os.environ.get("SL_PANE") or "").strip()
    if pane:
        return pane
    try:
        anchor = json.loads((_state_dir(state_home) / "runner.anchor.json").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(anchor, dict):
        pane = anchor.get("pane")
        if isinstance(pane, str) and pane.strip():
            return pane.strip()
    return None


def default_log_path():
    """The dashboard's OWN launch log: ``<base>/command-center/fixer-log.jsonl``, beside
    ``desk.json`` — where ``base`` is ``$SL_HOME`` or ``~/.superlooper``. Decision B.4 (see
    ``lib/desk``): the center's own facts live BESIDE the loop state homes it reads, never inside
    one. The brief in the state home is the session's own record; this is the dashboard's."""
    base = os.environ.get("SL_HOME") or os.path.expanduser("~/.superlooper")
    return Path(base) / "command-center" / _LOG_FILENAME


# =============================== the verb ===============================

def _run(argv, env, timeout):
    """Run the shim; returns ``(rc, stdout, stderr)``. NEVER raises: a timeout, a missing script, a
    non-executable file — each is caught and returned as a nonzero rc, so the caller fails closed
    (mirrors ``restart._run``)."""
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except (OSError, ValueError):
        return 127, "", "command not found"


class Fixer:
    """The Deploy Fixer verb, bound to the configured superlooper CLI path (from which the shim is
    derived), an allow-list mapping each WATCHED repo slug to its checkout + state home, and the
    operator name the launch is recorded under.

    Two methods back the button's two-step flow: :meth:`preflight` reports whether a fixer is
    already live and what trouble would ride into the prompt, and writes NOTHING; :meth:`execute`
    composes the brief and launches (only after the owner's in-box confirm). Every result is honest —
    ``ok`` is the shim's real verdict on delivery, never a pretend one."""

    def __init__(self, superlooper_cli, repos, operator=None, timeout=None, log_path=None,
                 now=None, agent=None):
        self._script = launch_script_for(superlooper_cli)
        # slug -> {"path": <checkout>, "state_home": <home>}
        self._repos = dict(repos or {})
        self._operator = operator if (isinstance(operator, str) and operator.strip()) else None
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._log_path = str(log_path) if log_path else str(default_log_path())
        self._now = now if now is not None else time.time
        self._agent = agent
        # ThreadingHTTPServer serves POSTs concurrently: two taps racing would both pass the
        # single-flight check and both allocate d<N>. Serialize the whole check-allocate-launch.
        self._lock = threading.Lock()

    # ---- helpers ----

    def _refuse(self, verb, error, **extra):
        out = {"ok": False, "verb": verb, "error": error}
        out.update(extra)
        return out

    def _entry(self, repo):
        return self._repos.get(repo)

    def _stamp(self):
        ts = self._now() if callable(self._now) else self._now
        return ts if isinstance(ts, (int, float)) and math.isfinite(ts) else time.time()

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

    # ---- step 1: the preflight (writes nothing) ----

    def preflight(self, repo, snapshot):
        """Is a fixer already on this patient, and what would ride into its prompt? Read-only: no
        brief, no launch, no record. The dialog decides what to show from this."""
        entry = self._entry(repo)
        if entry is None:
            return self._refuse("fixer-check", "unknown repo", live=None)
        ctx = trouble_context(snapshot, repo)
        if ctx is None:
            return self._refuse("fixer-check", "that repo is not on the field", live=None)
        live = live_fixer_id(entry["state_home"])
        return {"ok": True, "verb": "fixer-check", "live": live is not None,
                "live_id": live, "trouble": ctx}

    # ---- step 2: the launch ----

    def execute(self, repo, note, snapshot):
        """Compose the prompt and launch ONE interactive sl-debugger session through the engine's
        shim. Refuses — changing nothing, launching nothing — on an unwatched repo, a live fixer, an
        unresolvable shim, or an unresolvable cmux pane. ``ok`` is the shim's verdict: rc 0 means it
        VERIFIED the tab took the prompt."""
        entry = self._entry(repo)
        if entry is None:
            return self._refuse("fixer", "unknown repo", live=None)

        ctx = trouble_context(snapshot, repo)
        if ctx is None:
            return self._refuse("fixer", "that repo is not on the field", live=None)

        home = entry["state_home"]
        script = resolve_script(self._script)

        with self._lock:
            live = live_fixer_id(home)
            if live is not None:
                return self._refuse(
                    "fixer",
                    "a fixer session (%s) is already running for this repo — never two debuggers "
                    "on one patient" % live,
                    live=True, live_id=live)

            # Resolve everything the shim REQUIRES before writing a single byte: an unresolvable
            # launch must leave no brief behind, so a refusal is indistinguishable from never having
            # tapped.
            if not os.path.isfile(script):
                return self._refuse(
                    "fixer",
                    "could not find the engine's launch-session.sh at %s — is superlooper "
                    "installed? (set 'superlooper_cli' in config.json)" % script,
                    live=False)
            pane = resolve_pane(home)
            if not pane:
                return self._refuse(
                    "fixer",
                    "no cmux pane resolves for this loop (no recorded runner anchor) — start the "
                    "loop once so the fixer's tab has somewhere to be born",
                    live=False)

            fixer_id = next_fixer_id(_seen_ids(home))
            prompt = compose_prompt(ctx, note, entry["path"], home, operator=self._operator)
            try:
                briefs = Path(home) / "briefs"
                briefs.mkdir(parents=True, exist_ok=True)
                (briefs / ("%s.md" % fixer_id)).write_text(prompt)
            except OSError as e:
                return self._refuse("fixer", "could not write the session brief: %s" % e, live=False)

            rc, out, err = _run(launch_argv(script, entry["path"], fixer_id),
                                launch_env(os.environ, home, pane, agent=self._agent),
                                self._timeout)

        ts = self._stamp()
        if rc == 0:
            self._record(ts, repo, fixer_id, note, ctx, True)
            return {"ok": True, "verb": "fixer", "id": fixer_id, "live": True,
                    "trouble": trouble_kinds(ctx)}

        error = _launch_error(rc, err, out)
        self._record(ts, repo, fixer_id, note, ctx, False, error=error)
        return self._refuse("fixer", error, live=False, id=fixer_id)


def _launch_error(rc, stderr, stdout):
    """A plain, honest failure message for a launch that didn't land — what the UI shows instead of
    a fake success. Never a bare exit code: the owner can act on words."""
    msg = (stderr or "").strip() or (stdout or "").strip()
    if rc == 124:
        return "the launch timed out — no session was confirmed"
    if rc == 127:
        return "could not run the engine's launch-session.sh — is superlooper installed?"
    return msg or "the launch failed (exit %d) — no session was confirmed" % rc
