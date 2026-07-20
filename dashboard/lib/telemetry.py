"""Bounded, local GitHub API-burn telemetry (issue #15).

Superlooper's runner and the dashboard are the two GitHub API clients on William's machine, and they
share ONE `gh` auth token — hence one rate-limit budget. When issue #8 went
looking for who burned the GraphQL quota during a quota incident, it found NO persisted telemetry:
attribution came from source cadence plus live process-watch sampling, enough for a census but too
thin for the next incident. This module is the durable, bounded record that closes that gap. Every
`gh` subprocess the adapter runs lands one line here; a periodic FREE rate-limit snapshot rides
alongside — so a later incident can attribute burn by client/op/api without a live sampler.

Owner ruling (William, 2026-07-16): there will be NO second GitHub token — when quota runs low the
answer is to poll GitHub LESS. This telemetry is the first step (find where the burn is); the
follow-up optimization issues cite its data.

Design constraints:
  * LOCAL + BOUNDED. Rows land under the repo's state home
    (`<state_home>/gh-telemetry-<client>.jsonl`), outside the repo — never tracked runtime data. The
    file is a byte-bounded ring: once it crosses `MAX_BYTES` the oldest lines are dropped, keeping the
    newest `TRIM_TO_BYTES`.
  * FAIL-SAFE, ALWAYS. Telemetry is observability, never a dependency: every `record_*` call swallows
    all errors (a full disk, a permission fault, a serialization bug) and returns — a telemetry
    failure must NEVER break or fail a `gh` read/write. (Contrast the journal, whose WRITE path fails
    LOUD: a lost journal line is a lost decision; a lost telemetry line is only a lost sample.)
  * NO EXTRA BURN. The rate-limit snapshot reads `gh api rate_limit`, which GitHub documents as exempt
    from rate limiting — snapshots cost no quota. The runner already probes that endpoint once per
    poll, so the snapshot rides that same read.

Two row kinds, both carrying `ts`/`client`/`repo`:
  * kind="call"       — one per gh subprocess: `op` (the gh.py function it ran for), `family` (the gh
                        subcommand), `api` ("graphql"/"rest"/"unknown" — the EXPECTED quota bucket),
                        and `status` (success/failure/refused/rate_limited).
  * kind="rate_limit" — a periodic snapshot: per-resource {limit, used, remaining, reset} for core,
                        graphql (and search), plus `ok` (did the snapshot read succeed). Deltas of
                        `used` across snapshots are what estimate hourly GraphQL/core burn.

Reading note for a burn report: the snapshot's `used` deltas are the AUTHORITATIVE burn magnitude;
the `kind="call"` rows attribute it by client/op/api. The `api rate_limit` family is quota-EXEMPT
(it IS the free snapshot probe, recorded because it is still a gh subprocess) — exclude that family
when tallying "REST calls that spent quota" from call rows, or it inflates the REST total.
"""
import json
import os
import re
import time

# The byte ceiling for one telemetry file, and the size it is trimmed back to once the ceiling is
# crossed. Hysteresis (trim target < ceiling) makes the O(n) rewrite amortized-rare: many appends
# pass between trims, instead of a rewrite firing on every append once the file sits near the cap.
MAX_BYTES = 1_000_000
TRIM_TO_BYTES = 700_000

# The four statuses a gh call is classified into (DoD): a clean answer, a rate-limit refusal (the
# incident signal), a non-rate-limit GitHub refusal, or a local failure that never reached GitHub.
STATUS_SUCCESS = "success"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_REFUSED = "refused"
STATUS_FAILURE = "failure"

# gh's rate-limit refusals, primary and secondary. GitHub's wording is stable here — it is the API's
# own message, echoed by gh to stderr ("API rate limit exceeded", "you have exceeded a secondary rate
# limit"). We match the load-bearing phrases case-insensitively; anything we don't recognize is NOT
# called a rate limit, because a mislabel here would hide the very signal a quota incident needs.
_RATE_LIMIT_RE = re.compile(r"rate limit|secondary rate limit", re.I)


def classify_status(rc, stderr):
    """Map a gh ``(rc, stderr)`` into one of the four statuses:

      * rc 0                        -> success (GitHub answered; the body may be empty — that is
                                       "answered empty", which a reader tells from a refusal BY THIS
                                       status, since a refusal is never ``success``).
      * nonzero + rate-limit stderr -> rate_limited (the quota-exhaustion signal — checked before the
                                       generic buckets so a 403 rate-limit is never miscounted).
      * rc 127                      -> failure (gh missing / bad invocation — the call never left the
                                       machine, so it burned no quota; kept distinct so burn accounting
                                       does not count it as an attempt that hit GitHub).
      * any other nonzero           -> refused (a timeout, or gh reached GitHub and it refused: auth /
                                       403 / 404 / 5xx). GitHub was contacted-or-attempted.
    """
    if rc == 0:
        return STATUS_SUCCESS
    if stderr and _RATE_LIMIT_RE.search(stderr):
        return STATUS_RATE_LIMITED
    if rc == 127:
        return STATUS_FAILURE
    return STATUS_REFUSED


def classify_api(argv):
    """The GitHub API surface a gh invocation is EXPECTED to hit — i.e. which quota bucket it spends.
    Best-effort static attribution (gh's internals vary by version), enough to split GraphQL burn
    from core/REST burn, which is exactly the #8 question:

      * ``gh api graphql …``          -> graphql
      * ``gh api <rest-path>``        -> rest  (repos/…, rate_limit, git/refs/…)
      * ``gh issue|pr|repo|search …`` -> graphql (gh drives these through the GraphQL API)
      * ``gh label …``                -> rest  (gh's label commands use the REST labels endpoint)
      * anything else                 -> unknown
    """
    if not argv:
        return "unknown"
    head = argv[0]
    if head == "api":
        return "graphql" if len(argv) > 1 and argv[1] == "graphql" else "rest"
    if head in ("issue", "pr", "repo", "search"):
        return "graphql"
    if head == "label":
        return "rest"
    return "unknown"


def classify_family(argv):
    """A bounded command-family label for a gh invocation — the subcommand, NOT its arguments, so the
    field has small cardinality (good for grouping a burn report). ``["issue","list",…]`` ->
    "issue list"; ``["pr","view","20",…]`` -> "pr view". For ``gh api`` only the first path segment
    survives, so a per-branch path (``repos/{owner}/{repo}/branches/sl-i1-x``) does not explode the
    cardinality -> "api repos"."""
    if not argv:
        return ""
    head = argv[0]
    if head == "api":
        path = argv[1] if len(argv) > 1 else ""
        seg = re.split(r"[/?]", path, maxsplit=1)[0] if path else ""
        return ("api " + seg).strip()
    sub = next((a for a in argv[1:] if not a.startswith("-")), "")
    return (head + " " + sub).strip()


def parse_rate_limit(stdout):
    """Parse a ``gh api rate_limit`` body into ``{resource: {limit, used, remaining, reset}}`` for the
    resources that matter to burn estimation (core, graphql, search). Fail-closed to ``{}`` on any
    unparseable / wrong-shaped body — a snapshot we cannot read records ``ok=False`` with no
    resources, never a fabricated zero. Only integer fields are kept (a wrong-typed value is dropped,
    never coerced)."""
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    resources = obj.get("resources") if isinstance(obj, dict) else None
    if not isinstance(resources, dict):
        return {}
    out = {}
    for name in ("core", "graphql", "search"):
        r = resources.get(name)
        if not isinstance(r, dict):
            continue
        picked = {}
        for k in ("limit", "used", "remaining", "reset"):
            v = r.get(k)
            if isinstance(v, int) and not isinstance(v, bool):   # bool is an int subclass — exclude
                picked[k] = v
        if picked:
            out[name] = picked
    return out


def path(home, client):
    """The telemetry file for one client under a repo's state home. Client-suffixed so the runner and
    the dashboard never share (hence never race) a file, and an incident tool globs
    ``gh-telemetry-*.jsonl`` to read both clients at once."""
    return os.path.join(os.fspath(home), "gh-telemetry-%s.jsonl" % client)


def record_call(home, client, repo, op, argv, rc, stderr, now=None):
    """Record one gh subprocess as a ``kind="call"`` row. Derives ``family``/``api`` from ``argv`` and
    ``status`` from ``(rc, stderr)``. NEVER raises — the whole body (classification included) is
    guarded, so a bug or an odd input can never break the gh call this is observing."""
    try:
        _write(home, client, {
            "kind": "call", "client": client, "repo": repo, "op": op,
            "family": classify_family(argv), "api": classify_api(argv),
            "status": classify_status(rc, stderr),
        }, now)
    except Exception:
        return


def record_rate_limit(home, client, repo, resources, ok, now=None):
    """Record one rate-limit snapshot as a ``kind="rate_limit"`` row. Never raises (same guard as
    :func:`record_call`)."""
    try:
        _write(home, client, {
            "kind": "rate_limit", "client": client, "repo": repo,
            "resources": resources, "ok": bool(ok),
        }, now)
    except Exception:
        return


def _write(home, client, row, now):
    """Stamp ``ts`` and append one JSON line, then bound the file. May raise on an I/O fault — its
    only callers (``record_*``) swallow EVERYTHING, so the telemetry path as a whole is fail-safe.
    One ``write`` per record on an O_APPEND handle, so concurrent writers interleave line-wise rather
    than corrupting a record."""
    stamped = {"ts": now if now is not None else time.time()}
    stamped.update(row)
    line = json.dumps(stamped) + "\n"
    os.makedirs(os.fspath(home), exist_ok=True)
    p = path(home, client)
    with open(p, "a") as f:
        f.write(line)
    _trim(p)


def _trim(p):
    """Bound the file to ``MAX_BYTES`` by keeping only the newest lines that fit in ``TRIM_TO_BYTES``.
    Cheap in the common case: a single ``stat`` (getsize) under the cap returns immediately; the O(n)
    read-and-rewrite fires only when the cap is crossed. Single-writer per file (the runner process
    for its file, the dashboard process for its own), so the read -> atomic ``os.replace`` is safe
    against itself; a stray concurrent CLI invocation is the only other writer, an accepted narrow
    audit-only window (a line appended during the rare rewrite could be lost — a lost sample, never a
    lost decision). Never raises."""
    try:
        if os.path.getsize(p) <= MAX_BYTES:
            return
        with open(p, "rb") as f:
            lines = f.read().splitlines(keepends=True)
        kept, total = [], 0
        for ln in reversed(lines):
            total += len(ln)
            if total > TRIM_TO_BYTES and kept:
                break                       # older than the newest TRIM_TO_BYTES — drop it
            kept.append(ln)
        kept.reverse()
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.writelines(kept)
        os.replace(tmp, p)
    except OSError:
        return


def read(home, client):
    """Every row in one client's telemetry file, tolerant per line (corrupt / blank lines skipped; a
    missing file reads as ``[]``). For tests and future incident tooling — not on any hot path."""
    try:
        with open(path(home, client)) as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
