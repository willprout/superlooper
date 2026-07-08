"""The slow-clock pollers — the dashboard's only outward I/O besides ``gh`` and serving pages.

Three pieces, all fail-tolerant so a 2-second poll loop can never be taken down by a slow disk, a
foreign worktree, or an unreachable usage endpoint:

  * ``Cached`` — the cadence primitive. ``gh`` is expensive and rate-limited, so its reads run on a
    SLOW clock: a fetch fires at most once per ``interval`` (the config's ``gh_poll_seconds``) and
    the last value is served in between. The clock is injectable so cadence is unit-tested with a
    fake, never real sleeping.

  * ``diff_stat`` — the flight's cargo size (+N/−N/files) from ``git diff`` in the lane worktree,
    ``<state-home>/worktrees/<id>``. Read-only; every git failure (absent worktree, a directory
    that isn't a repo, an unknown base) fails closed to an empty-but-typed ``present: False`` — the
    diff chip then simply shows nothing rather than a lie. The diff is taken against the branch's
    MERGE-BASE with the mainline (two-dot), so it counts committed AND uncommitted work as one
    honest cargo total and a mainline that has moved on doesn't masquerade as deletions.

  * ``read_usage`` — the usage pill's feed, ported fail-closed from the skill's ``lib/usage.py``
    (decision B.7). On ANY failure it returns an explicit UNKNOWN sentinel (``known: False`` ⇒ the
    pill renders "usage ?"); it holds NO state, so it can never serve a stale number after a fetch
    starts failing. The Keychain read and the HTTPS call are dependency-injected, so the fail-closed
    logic is fully testable without touching the Keychain or the network.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

# ============================ Cached: the gh slow clock ============================


class Cached:
    """Memoize ``fetch()`` for ``interval`` seconds on an injectable ``clock``. ``get()`` returns
    the cached value, refreshing only once ``interval`` seconds have elapsed since the last fetch
    (boundary inclusive: a get exactly ``interval`` seconds later refetches). This is the ``gh``
    slow clock — wrap one per (repo, query) with ``interval = config['gh_poll_seconds']``."""

    _MISSING = object()

    def __init__(self, fetch, interval, clock=None):
        self._fetch = fetch
        self._interval = interval
        self._clock = clock if clock is not None else time.time
        self._value = self._MISSING
        self._last = None

    def get(self):
        now = self._clock()
        if self._value is self._MISSING or (now - self._last) >= self._interval:
            self._value = self._fetch()
            self._last = now
        return self._value


# ============================ diff_stat: the worktree cargo poller ============================

_GIT_TIMEOUT = 30   # seconds per git call — a hung git must never wedge the poll loop

_EMPTY_DIFF = {"present": False, "files": 0, "added": 0, "removed": 0}


def _git(cwd, *args, timeout=_GIT_TIMEOUT):
    """Run ``git -C <cwd> <args>``. Returns ``(rc, stdout)``. Never raises: a timeout, a missing
    binary, or any OSError becomes a nonzero rc with empty stdout (fail closed)."""
    try:
        r = subprocess.run(["git", "-C", os.fspath(cwd), *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.returncode, r.stdout)
    except subprocess.TimeoutExpired:
        return (124, "")
    except OSError:
        return (127, "")


def diff_stat(worktree, base_branch="main"):
    """The lane worktree's cargo vs ``base_branch``, as ``{present, files, added, removed}``.

    ``present`` is ``False`` (with zero counts) whenever the diff can't be read honestly — the
    worktree directory is absent (issue not launched yet, or finished and cleaned up), the path is
    not a git repo, or ``base_branch`` can't be resolved there. Otherwise ``present`` is ``True``
    and the counts are the two-dot line delta from ``merge-base(base_branch, HEAD)`` to the working
    tree, PLUS untracked new files. That is the full cargo: committed edits, uncommitted tracked
    edits, AND brand-new files a worker created but hasn't ``git add``-ed yet (``git diff`` alone
    ignores those — counting only tracked changes would report empty cargo for a fresh flight). A
    binary file counts toward ``files`` but not the line counts (git reports ``-`` for its
    added/removed; an untracked binary is detected by a NUL byte and likewise counts as a file
    only). ``.gitignore``-d paths are excluded — they are not cargo."""
    wt = os.fspath(worktree)
    if not os.path.isdir(wt):
        return dict(_EMPTY_DIFF)
    rc, mb = _git(wt, "merge-base", base_branch, "HEAD")
    if rc != 0 or not mb.strip():
        return dict(_EMPTY_DIFF)
    rc, out = _git(wt, "diff", "--numstat", mb.strip())
    if rc != 0:
        return dict(_EMPTY_DIFF)
    files = added = removed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        a, r = parts[0], parts[1]
        if a.isdigit():
            added += int(a)
        if r.isdigit():
            removed += int(r)
    # Untracked-but-not-ignored files: git diff never sees these, so fold them in by hand. -z gives
    # raw NUL-separated paths (no shell-quoting of odd filenames). A best-effort pass — if it fails
    # we still return the tracked cargo rather than nothing.
    f, a = _untracked_stat(wt)
    files += f
    added += a
    return {"present": True, "files": files, "added": added, "removed": removed}


def _untracked_stat(wt):
    """``(file_count, added_lines)`` for untracked, non-ignored files in ``wt``. Added lines are
    counted git's way — a new text file's line count, a NUL-carrying (binary) file contributes a
    file but no line count, and a symlink contributes exactly one line (git stores it as a blob
    holding the target path, so it is NOT followed). Read-only; an unreadable file counts as a file
    with no lines. Files are read in bounded chunks so a large untracked artifact can't balloon the
    poller's memory."""
    rc, out = _git(wt, "ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0:
        return 0, 0
    files = added = 0
    for rel in out.split("\x00"):
        if not rel:
            continue
        files += 1
        added += _added_lines(os.path.join(wt, rel))
    return files, added


def _added_lines(path):
    """git-equivalent added-line count for a new file at ``path``: 1 for a symlink (its blob is the
    target path), 0 for a binary (NUL in the first block) or unreadable file, else the text line
    count (a final line without a trailing newline still counts). Streamed in bounded chunks."""
    if os.path.islink(path):
        return 1
    try:
        with open(path, "rb") as fh:
            first = fh.read(8192)
            if b"\x00" in first:                 # git's own binary heuristic -> no line count
                return 0
            n = first.count(b"\n")
            last = first[-1:]
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return 0
    return n + (1 if last and last != b"\n" else 0)   # trailing partial line counts as +1


# ============================ read_usage: fail-closed usage pill ============================

# Bump to the current `claude-code/<version>` if the endpoint starts refusing a stale User-Agent.
USER_AGENT_VERSION = "2.1.90"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# The dashboard-facing unknown sentinel: known:False ⇒ the pill renders "usage ?".
_UNKNOWN_USAGE = {
    "known": False,
    "five_hour_pct": None, "seven_day_pct": None,
    "five_hour_resets_epoch": None, "seven_day_resets_epoch": None,
    "status": "unknown",
}


def iso_to_epoch(iso):
    """``'2026-07-07T20:00:00Z'`` -> int epoch seconds. ``None`` on ``None``/garbage (never
    raises)."""
    if not iso:
        return None
    try:
        return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError, TypeError):
        return None


def _is_num(v):
    # a utilization percentage; bool is an int subclass, so exclude it explicitly
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _keychain_token():
    """The real macOS Keychain read (impure). Returns the OAuth access token, or ``""`` if none.
    Raises on Keychain failure so ``fetch_claude_usage`` maps it to ``no_keychain``. The ``security``
    binary resolves through ``SL_SECURITY`` so the conftest guard can neutralize this egress
    fail-closed by default — the same ratchet that covers ``gh``/``osascript``, extended to the
    usage reader's Keychain (and, transitively, its network: no token ⇒ no request)."""
    raw = subprocess.run(
        [os.environ.get("SL_SECURITY", "security"),
         "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, timeout=5)
    if raw.returncode != 0:
        raise OSError("no Claude Code Keychain entry")
    creds = json.loads(raw.stdout.strip())
    return creds.get("claudeAiOauth", {}).get("accessToken", "")


def _https_get_json(url, headers, timeout):
    """The real HTTPS GET (impure; NEVER invoked by a test — usage tests inject a fake
    ``http_get``). Returns the parsed JSON body; raises ``urllib`` errors upward."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_claude_usage(token_source=_keychain_token, http_get=_https_get_json):
    """Fetch Claude Code/Max subscription usage (5-hour + 7-day windows), ported from the skill's
    ``lib/usage.py``. Returns a dict with ``five_hour_pct``/``seven_day_pct`` (None on failure),
    their reset epochs, and ``auth_status`` (``ok`` | ``no_keychain`` | ``no_token`` |
    ``rate_limited`` | ``auth_expired`` | ``api_error``). ``token_source`` and ``http_get`` are
    injected so the whole path is testable without the Keychain or the network."""
    result = {
        "five_hour_pct": None, "seven_day_pct": None,
        "five_hour_resets_epoch": None, "seven_day_resets_epoch": None,
        "auth_status": "unknown",
    }
    try:
        token = token_source()
    except Exception:
        result["auth_status"] = "no_keychain"
        return result
    if not token:
        result["auth_status"] = "no_token"
        return result

    headers = {
        "Authorization": "Bearer %s" % token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/%s" % USER_AGENT_VERSION,
    }
    try:
        data = http_get(_USAGE_URL, headers, 5)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            result["auth_status"] = "rate_limited"      # back off, retry later
        elif e.code in (401, 403):
            result["auth_status"] = "auth_expired"       # token expired; re-login needed
        else:
            result["auth_status"] = "api_error"
        return result
    except Exception:
        result["auth_status"] = "api_error"
        return result

    if not isinstance(data, dict):
        result["auth_status"] = "api_error"
        return result
    fh = data.get("five_hour") if isinstance(data.get("five_hour"), dict) else {}
    sd = data.get("seven_day") if isinstance(data.get("seven_day"), dict) else {}
    result["five_hour_pct"] = fh.get("utilization")
    result["seven_day_pct"] = sd.get("utilization")
    result["five_hour_resets_epoch"] = iso_to_epoch(fh.get("resets_at"))
    result["seven_day_resets_epoch"] = iso_to_epoch(sd.get("resets_at"))
    # Schema-drift defense: a 200 whose body renamed/omitted a window (pct None) OR gave a
    # wrong-typed utilization (a string "42", a bool) is NOT healthy. Only real numbers for BOTH
    # windows earn 'ok'; anything else fails closed to api_error so the pill shows "?" honestly and
    # the status never contradicts an unusable reading.
    if _is_num(result["five_hour_pct"]) and _is_num(result["seven_day_pct"]):
        result["auth_status"] = "ok"
    else:
        result["auth_status"] = "api_error"
    return result


def read_usage(fetcher=fetch_claude_usage):
    """The usage pill's fail-closed feed. Calls ``fetcher()`` FRESH every time (no cached state) and
    returns ``{known, five_hour_pct, seven_day_pct, five_hour_resets_epoch, seven_day_resets_epoch,
    status}``. ``known`` is ``True`` only when the fetch reported ``ok`` AND both percentages are
    real numbers; on anything else — an exception, a non-dict, a non-``ok`` status, or a missing
    percentage — it returns the UNKNOWN sentinel (``known: False``, all numbers ``None``), never a
    stale or partial number. ``status`` surfaces the underlying reason (``auth_expired`` /
    ``rate_limited`` / …) so the pill can explain the "?" rather than just show it."""
    try:
        raw = fetcher()
    except Exception:
        return dict(_UNKNOWN_USAGE)
    if not isinstance(raw, dict):
        return dict(_UNKNOWN_USAGE)
    status = raw.get("auth_status")
    fh, sd = raw.get("five_hour_pct"), raw.get("seven_day_pct")
    if status == "ok" and _is_num(fh) and _is_num(sd):
        return {
            "known": True,
            "five_hour_pct": fh, "seven_day_pct": sd,
            "five_hour_resets_epoch": raw.get("five_hour_resets_epoch"),
            "seven_day_resets_epoch": raw.get("seven_day_resets_epoch"),
            "status": "ok",
        }
    out = dict(_UNKNOWN_USAGE)
    if isinstance(status, str) and status and status != "ok":
        out["status"] = status         # surface WHY it's unknown, not just that it is
    elif status == "ok":
        out["status"] = "api_error"    # 'ok' but unusable pcts is self-contradictory schema drift
    return out
