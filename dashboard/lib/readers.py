"""Loop-state readers (Task 2 / decisions B.1, B.4) — pure, fail-tolerant.

The dashboard's whole model of a repo starts here: these functions turn a superlooper *state
home* (``~/.superlooper/<owner>__<name>/``, laid out in docs/BUILD-PLAN.md §D) into a plain facts
dict the flight model and server consume. They only ever READ files — no ``gh``, no subprocess, no
network, and deliberately NO semantics: stage mapping, liveness *tiers*, the progress heuristic
and every other derivation belong to the flight-model task. What lives here is the raw truth on
disk (issue state, marker text, mtimes, epochs) plus the two arithmetic facts §D names outright
(heartbeat age).

Fail-tolerance is the contract, not a nicety: the runner writes these files continuously and can
crash mid-write, so a half-written journal line, a truncated ``issues.json`` or a missing marker
dir must degrade to an empty/None default — never an exception that would take down a 2-second
poll loop. Two failure *directions* are encoded deliberately, mirroring the runner's own reads:

* **Fail OPEN** where a file's *content* is the whole signal — a corrupt ``issues.json`` reads as
  ``{}`` (no issues known) rather than pretending to state we can't trust.
* **Fail CLOSED** where a file's *existence* is the signal — a present-but-corrupt
  ``merges_frozen.json`` / ``ALERT`` reads as ``{}`` (a dict, so "frozen"/"alerting" still counts),
  exactly as ``bin/runner.py``'s ``_read_json`` does. Absent ⇒ ``None`` (not frozen / no alert).

The tolerant journal reader mirrors the skill's ``lib/journal.py`` ``read()`` (skip corrupt, blank
and non-dict lines; missing file ⇒ ``[]``) so both agree on what a "record" is.
"""
import json
import os
import time
from collections import deque

JOURNAL = "journal.jsonl"

# Every way a half-written or adversarial JSON body can blow up json.loads. JSONDecodeError is a
# ValueError subclass; RecursionError (deeply nested arrays/objects) is NOT, and would otherwise
# escape a reader — so it is caught explicitly to keep the "no reader ever raises" contract.
_JSON_ERRORS = (json.JSONDecodeError, ValueError, RecursionError)


# --------------------------- low-level tolerant reads ---------------------------

def _read(path):
    """File text, or ``None`` if it can't be read (missing, a directory, permission). The runner
    and workers write UTF-8, so we decode as UTF-8 explicitly (never the CI machine's locale) and
    replace any undecodable bytes rather than raise — a half-written marker must degrade to
    best-effort text, not crash the poll loop."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_json_existence(path):
    """For files whose *existence* is the signal (ALERT, merges_frozen): ``None`` when absent,
    ``{}`` when present-but-unreadable (fail closed — the state still counts), else the parsed
    dict. A non-dict JSON body also collapses to ``{}``. Mirrors ``bin/runner.py:_read_json``."""
    txt = _read(path)
    if txt is None:
        return None
    try:
        v = json.loads(txt)
    except _JSON_ERRORS:
        return {}
    return v if isinstance(v, dict) else {}


def _read_stop_marker(path):
    """The deliberate-stop marker (issue #239/#365), read the way the ENGINE reads it: ``None`` only
    when the file is genuinely ABSENT, ``{}`` for present-but-unreadable in any way — corrupt JSON, a
    non-dict body, a directory in its place, a permission-denied read.

    Why this can't be ``_read_json_existence``: that reader maps a MISSING file and an UNOPENABLE one
    to the same ``None``, which is right for ALERT and merges_frozen (absent and unreadable both mean
    "not frozen") and exactly wrong here. For this file the two are opposite answers, and the wrong
    one is the dangerous one: every reader of the marker treats absent as permission to restart the
    loop, so an unreadable marker read as absent puts the board back to RUNNER DOWN and fires the
    push over a stop the owner deliberately made — this issue's own defect, arriving through a
    permission bit. The engine's ``runner.read_stop_marker`` draws the line with the same
    ``os.path.exists`` check, and this mirrors it. (Found by a fresh reviewer.)
    """
    txt = _read(path)
    if txt is None:
        return {} if os.path.exists(path) else None
    try:
        v = json.loads(txt)
    except _JSON_ERRORS:
        return {}
    return v if isinstance(v, dict) else {}


def _read_state_format(path):
    """The engine's state-home format stamp (issue #45), with the fail direction the HANDSHAKE needs:
    ``None`` ONLY when truly ABSENT (a pre-handshake home ⇒ the flight model grandfathers it), and
    ``{}`` for present-but-untrustworthy — corrupt JSON, a non-dict body, OR a file that exists but
    can't be opened (a directory in its place, a permission-denied read). A present stamp we can't
    parse is a shape we can't confirm, so it must surface as a NAMED mismatch, never masquerade as
    "no stamp". This is deliberately stricter than ``_read_json_existence`` (which reads an
    unopenable file as absent) — for merges_frozen/ALERT absent-vs-unreadable both mean "not
    frozen/not alerting", but here absent (grandfather) and unreadable (mismatch) must stay distinct."""
    txt = _read(path)
    if txt is None:
        return {} if os.path.exists(path) else None   # exists-but-unopenable ⇒ mismatch, else absent
    try:
        v = json.loads(txt)
    except _JSON_ERRORS:
        return {}
    return v if isinstance(v, dict) else {}


def _iter_records(lines):
    """Yield the well-formed JSON *objects* from ``lines``, in order; skip blank lines, corrupt
    JSON, and non-dict JSON (arrays/scalars). Same rule as skill ``journal.read``. A generator so
    ``tail_journal`` can keep only a bounded window without materializing the whole history."""
    for line in lines:
        try:
            rec = json.loads(line)
        except _JSON_ERRORS:
            continue
        if isinstance(rec, dict):
            yield rec


def _journal_lines(home):
    txt = _read(os.path.join(os.fspath(home), JOURNAL))
    return txt.splitlines() if txt is not None else []


# --------------------------- journal ---------------------------

def read_journal(home):
    """Every valid record in ``<home>/journal.jsonl``, in file order. Missing file ⇒ ``[]``;
    corrupt/blank/non-dict lines are skipped (fail closed per line)."""
    return list(_iter_records(_journal_lines(home)))


def tail_journal(home, limit):
    """The most recent ``limit`` valid records (in file order) — the bounded window a log/firehose
    view needs. ``limit <= 0`` ⇒ ``[]``; a ``limit`` past the history returns all records; corrupt
    lines never consume a slot (the window is filled from valid records only). A ``deque`` keeps
    only the last ``limit`` records so the window's memory is bounded no matter how long the
    append-only journal grows."""
    if limit <= 0:
        return []
    return list(deque(_iter_records(_journal_lines(home)), maxlen=limit))


# --------------------------- directory scans ---------------------------

def _scan_text(dir_path):
    """``{filename: text}`` for every readable file in ``dir_path`` (markers are named by bare
    issue id, e.g. ``blocked/i8``). Missing dir ⇒ ``{}``; an unreadable entry (e.g. a subdir) is
    skipped. Text is returned verbatim — the ``BOUNCED:`` prefix and any newlines are preserved,
    because classifying a marker is the flight model's job, not the reader's."""
    out = {}
    try:
        names = os.listdir(dir_path)
    except OSError:
        return out
    for n in names:
        txt = _read(os.path.join(dir_path, n))
        if txt is not None:
            out[n] = txt
    return out


def _scan_mtimes(dir_path):
    """``{filename: mtime}`` (raw float epoch mtimes) for every regular file in ``dir_path``.
    Missing dir ⇒ ``{}``. Ages are NOT computed here — the flight model turns an mtime into a
    liveness tier against each repo's own thresholds."""
    out = {}
    try:
        names = os.listdir(dir_path)
    except OSError:
        return out
    for n in names:
        p = os.path.join(dir_path, n)
        try:
            if os.path.isfile(p):
                out[n] = os.path.getmtime(p)
        except OSError:
            continue
    return out


def _iid_num(name):
    """``i<N>`` -> ``N``, else ``None`` — the skill's rule for "is this an issue id" (used to keep
    per-issue reports and drop the ``morning-<date>.md`` digest).

    ``isdecimal``, not ``isdigit``: ``"²".isdigit()`` is True but ``int("²")`` RAISES, so a file
    called ``i²`` in a scanned directory would throw out of a reader — inside the 2-second snapshot
    poll, blanking the whole board for every repo rather than being ignored as the junk filename it
    is. Same fail-closed-per-entry direction as every other reader here (the #139 defect class)."""
    if isinstance(name, str) and name.startswith("i") and name[1:].isdecimal():
        return int(name[1:])
    return None


def _session_window_ids(panes_dir):
    """The lane ids that have a RECORDED session window — the engine writes ``state/panes/<id>``
    when it launches a session and removes it when that window is closed (issue #340).

    The engine's own selection rule, deliberately: an entry named ``i<N>`` counts, the ``<id>.ws``
    workspace sidecar does not, and anything else in the directory is ignored (engine
    ``lib/panes.recorded_ids``, narrowed to the issue lanes this board renders). Matching it is what
    keeps the flight card's Open-session-window button and ``superlooper tidy`` agreeing about which
    lanes have a window — the alternative, deriving it from a status, gets both ends wrong: a parked
    lane's window is kept alive on purpose and is exactly the one the owner most needs to open, and
    a merged-and-tidied lane looks launched forever with its window long gone.

    The marker is not a *guarantee* the window is still open, and it is not meant to be: whether
    that lane still HAS a window is a question only the session host can answer, and the engine's
    ``focus-session`` verb asks it at tap time and reports the answer honestly (``no_window`` is an
    ordinary outcome there, not a failure). This decides only whether there is anything worth
    asking about.

    Missing/unreadable dir ⇒ an empty set. Fail closed: no marker ⇒ no lane claims a window ⇒ no
    button offered, which is the safe direction for a reader that cannot see the markers at all.
    """
    try:
        names = os.listdir(panes_dir)
    except OSError:
        return set()
    return {n for n in names if _iid_num(n) is not None}


def _report_ids(reports_dir):
    """Sorted issue ids that have a per-issue report *file* (``reports/i<N>.md``); the morning
    digest, any other non-issue file, and directories that merely look like an id are all excluded
    (the documented shape is ``reports/<id>.md``). Missing dir ⇒ ``[]``."""
    try:
        names = os.listdir(reports_dir)
    except OSError:
        return []
    ids = set()
    for n in names:
        if not n.endswith(".md"):
            continue
        stem = n[:-len(".md")]
        if _iid_num(stem) is None:
            continue
        try:
            if os.path.isfile(os.path.join(reports_dir, n)):
                ids.add(stem)
        except OSError:
            continue
    return sorted(ids, key=_iid_num)


# --------------------------- heartbeat ---------------------------

def _pid_alive(pid):
    """Does ``pid`` name a live process? A PROBE, never a raise — it runs on the 2-second poll.

    ``os.kill(pid, 0)`` sends no signal; signal 0 is the existence check, and it is the same one the
    engine's ``runner._pid_alive`` uses. ``PermissionError`` means the process EXISTS and belongs to
    someone else, which is still alive. Non-positive pids are refused outright: ``kill(0, …)`` and
    ``kill(-n, …)`` address process GROUPS, so a junk pidfile must never be turned into a signal at
    a group — even a signal that does nothing.

    The catch list mirrors the engine's ``runner._probe_pid`` deliberately, and ``OverflowError`` is
    the reason it has to: a pid too large for a C int raises it, and it is NOT an ``OSError``, so
    catching only ``OSError`` lets a ``runner.lock`` holding an absurd number escape this reader and
    500 the 2-second snapshot poll — blanking the whole board, every repo, over one junk file. Only
    a definite "alive" counts as alive; everything a probe cannot answer reads False, exactly as the
    engine's own bool face does. (Found by a fresh reviewer, who ran the probe rather than assuming.)
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    return True


def _runner_live(state_dir):
    """Is a runner actually RUNNING for this state home? The mirror of the engine's
    ``runner.live_runner_pid``: its ``runner.lock`` pid, confirmed alive.

    This exists because the heartbeat cannot answer it. The heartbeat says when the runner last
    FINISHED A TICK, which is a freshness signal on a five-minute threshold — useful for "has the
    loop gone quiet?", useless for "is the process there right now?". The difference is the whole
    of issue #365's honesty: `superlooper stop` lets the runner finish its tick, so a SUCCESSFUL
    stop routinely stamps a heartbeat AFTER the stop marker and then exits, and a heartbeat-only
    read would call that a stop that did not take — for the five minutes it takes the heartbeat to
    go stale, after every single stop.

    A missing pidfile, an unparseable one, and a dead pid all read ``False``: that is the engine's
    own reading, and the direction is load-bearing — this ``False`` is the positive "nothing is
    running" observation that lets a stop marker mean the loop is off.
    """
    txt = _read(os.path.join(state_dir, "runner.lock"))
    if txt is None:
        return False
    try:
        pid = int(txt.strip())
    except (TypeError, ValueError):
        return False
    return _pid_alive(pid)


def _heartbeat(state_dir, now):
    """``(epoch, age)`` from ``state/runner.heartbeat`` (the runner writes ``str(int(now))`` each
    tick). Missing or unparseable ⇒ ``(None, None)`` — the flight model reads a ``None`` age as
    RUNNER DOWN. ``age`` is left un-clamped (a small negative from clock skew is truthful, not a
    reader's to normalize)."""
    txt = _read(os.path.join(state_dir, "runner.heartbeat"))
    if txt is None:
        return None, None
    try:
        epoch = int(txt.strip())
    except (ValueError, TypeError):
        return None, None
    return epoch, float(now - epoch)


# --------------------------- the facts dict ---------------------------

def read_state_home(home, now=None):
    """Read a superlooper state home into the flight model's facts dict. Never raises on a missing
    or corrupt file. ``now`` (epoch seconds) is injectable for tests and defaults to the wall
    clock; it is used only to age the heartbeat.

    Keys:
      ``issues_state``   raw ``state/issues.json`` content (``{}`` if missing/corrupt)
      ``activity``       ``{id: mtime}`` from ``state/activity/`` (raw float mtimes)
      ``blocked``        ``{id: text}`` from ``state/blocked/`` (``BOUNCED:`` prefix preserved)
      ``exited``         ``{id: text}`` from ``state/exited/``
      ``awaiting``       ``{id: text}`` from ``state/awaiting/`` (touch markers ⇒ ``""``)
      ``heartbeat_epoch``/``heartbeat_age``  runner tick epoch and its age (``None`` if absent)
      ``merges_frozen``  ``state/merges_frozen.json`` (``None`` absent; ``{}`` corrupt ⇒ frozen)
      ``alert``          ``state/ALERT`` (``None`` absent; ``{}`` corrupt ⇒ alerting)
      ``stopped``        ``state/runner.stopped`` — the deliberate-stop marker (issue #239/#365).
                         ``None`` absent; ``{}`` present-but-unparseable ⇒ STILL a stop; else the
                         record (``stopped_at``, ``operator``, ``source``, ``home``, ``pid``)
      ``runner_live``    is a runner PROCESS alive for this home (``state/runner.lock``'s pid,
                         confirmed) — the engine's own liveness rule, not the heartbeat's freshness
      ``reports``        sorted issue ids with a per-issue report (morning digest excluded)
      ``session_windows`` the set of lane ids with a recorded session window (``state/panes/<id>``
                         — the engine's own marker, written at launch and removed when the window
                         is closed; the same one ``superlooper tidy`` selects on)
      ``state_format``   ``state/state_format.json`` — the engine's state-home format stamp (issue
                         #45). ``None`` when ABSENT (a pre-handshake home ⇒ grandfathered by the
                         flight model); the parsed dict (e.g. ``{"version": 1}``) when present;
                         ``{}`` when present-but-corrupt (fail closed — a stamp we can't trust is
                         "present, version unknown", never mistaken for "no stamp"). Whether a
                         version is COMPATIBLE is the flight model's call — the reader stays raw.
    """
    now = time.time() if now is None else now
    home = os.fspath(home)
    state = os.path.join(home, "state")

    issues_state = _read_json_existence(os.path.join(state, "issues.json"))
    if issues_state is None:
        issues_state = {}   # for issue state, absent and corrupt both mean "nothing known" (open)

    epoch, age = _heartbeat(state, now)
    return {
        "issues_state": issues_state,
        "activity": _scan_mtimes(os.path.join(state, "activity")),
        "blocked": _scan_text(os.path.join(state, "blocked")),
        "exited": _scan_text(os.path.join(state, "exited")),
        "awaiting": _scan_text(os.path.join(state, "awaiting")),
        "heartbeat_epoch": epoch,
        "heartbeat_age": age,
        "merges_frozen": _read_json_existence(os.path.join(state, "merges_frozen.json")),
        "alert": _read_json_existence(os.path.join(state, "ALERT")),
        # The deliberate-stop marker `superlooper stop` writes (issues #239/#365) — the fact that
        # tells a stop apart from a crash. Existence is the signal, in the engine's own strict sense
        # (see _read_stop_marker): every reader of this file treats absent as permission to restart
        # the loop, so a marker lost to a truncated write — or to a permission bit — would hand the
        # runner back to the guardians the owner just overruled. The flight model decides whether
        # the stop actually TOOK (lib/flights.stop_state); this stays raw.
        "stopped": _read_stop_marker(os.path.join(state, "runner.stopped")),
        # Is a runner PROCESS live right now (state/runner.lock, pid confirmed alive)? The engine's
        # own liveness rule, mirrored — and the fact that decides whether a recorded stop actually
        # TOOK. The heartbeat answers a different question on a five-minute clock; see _runner_live.
        "runner_live": _runner_live(state),
        "reports": _report_ids(os.path.join(home, "reports")),
        # The lane ids with a recorded session window (state/panes/<id>) — the Open-session-window
        # button's gate, and the same marker `superlooper tidy` selects on (issue #340).
        "session_windows": _session_window_ids(os.path.join(home, "state", "panes")),
        # The engine's state-home format stamp (issue #45): absent ⇒ None (grandfathered), any
        # present-but-untrustworthy read ⇒ {} which the flight model names as an INCOMPATIBLE stamp
        # — never a silent blank. Uses its own reader (not _read_json_existence) so a present-but-
        # UNREADABLE stamp is a mismatch, not mistaken for absent.
        "state_format": _read_state_format(os.path.join(state, "state_format.json")),
        # The runner's own published GitHub view (issue #146) — the dashboard's PRIMARY source. The
        # runner rewrites it every tick (atomically), so a read can still land on an old or, with a
        # crash mid-rename, an unreadable file: absent ⇒ None (a pre-#146 engine that publishes
        # nothing — the flight model falls back and NAMES it), present-but-corrupt ⇒ {} (which
        # carries no publish stamp, so source_mode refuses to render it as truth). Whether the
        # document is FRESH enough to be truth is the flight model's call; this stays raw.
        "published_view": _read_json_existence(os.path.join(state, "gh_view.json")),
    }
