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
    per-issue reports and drop the ``morning-<date>.md`` digest)."""
    if isinstance(name, str) and name.startswith("i") and name[1:].isdigit():
        return int(name[1:])
    return None


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
      ``reports``        sorted issue ids with a per-issue report (morning digest excluded)
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
        "reports": _report_ids(os.path.join(home, "reports")),
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
