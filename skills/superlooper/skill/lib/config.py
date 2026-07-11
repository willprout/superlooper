"""Per-repo config contract (§C.1): load + validate `.superlooper/config.json`, fill defaults,
and two pure helpers the scheduler/gate/runner build against.

Validation is hand-rolled (stdlib only — the runtime carries no pip deps, so no jsonschema).
It fails LOUD and SPECIFIC: an unknown key, a bad enum, or a wrong type names the offender, so a
typo in a repo's config is a clear adopt-time error rather than a silent misconfiguration that
only shows up as odd loop behaviour at 3am.
"""
import copy
import fnmatch
import json
import os
from pathlib import Path

# Top-level scalar/structural fields and their defaults (§C.1). `repo` is the ONLY required
# field (no sensible default — it names the GitHub repo and the state home). `areas` and
# `required_checks` default EMPTY: they are per-repo declarations the example fills in, not
# universal values (doctor/adopt separately require at least one required_check before a repo
# may run — that is a Task-10 gate, not a load-time one, so a freshly-adopted stub still loads).
_TOP_DEFAULTS = {
    "version": 1,
    "dev_branch": "main",
    "prod_branch": None,
    "agent": "claude",
    "lanes": 2,
    "affinity": "hard",
    "areas": {},
    "touches_required": True,
    "required_checks": [],
    "merge_method": "squash",
    "ship_cmd": None,
    "ship_recheck_cmd": None,
    "report_required_sections": ["Tests", "Browser evidence", "Regression tests", "Review"],
    "bright_lines": [],
    "cleanup_merged_worktrees": True,
    "report_time": "08:45",
}

# Nested dict fields — deep-merged one level so a partial override (e.g. session.retry_cap) keeps
# the sibling defaults instead of wiping them. Unknown sub-keys inside these are rejected too.
_NESTED_DEFAULTS = {
    # opus[1m] for BOTH (owner ruling 2026-07-06): this loader default WINS over
    # runner._models()'s fallback (a filled-in value is truthy), so it must carry the ruling
    # itself — a stale "fable" here silently starved answerers on repos that omit `models`
    # (the eApp session's 2026-07-06 catch). Repos override either key explicitly (the eApp
    # pins answerer: fable by William's project-specific choice).
    # worker_effort defaults to None (owner ruling 2026-07-07): a repo-wide reasoning-effort
    # default for WORKER launches. None means exactly today's behaviour — NEVER send --effort. It
    # MUST stay a genuine null: unlike worker/answerer, a filled-in truthy default would WIN over
    # the runner's no-flag fallback and force an effort on every repo that omits the field (the
    # stale-fable trap). A per-issue effort:* label overrides it; the answerer never reads it.
    "models": {"worker": "opus[1m]", "answerer": "opus[1m]", "worker_effort": None},
    # checks_pending_cap (issue #26): seconds a FINISHED PR may sit with its required checks
    # PENDING before the runner escalates ONCE to needs-william (naming the unreported checks).
    # The merge decision stays fail-closed — pending never merges — this only bounds the wait so a
    # required check that never reports can't hold a green PR gating forever, silently. Default
    # 10800 (3h) clears any real CI run; a huge value effectively disables the bound.
    "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2,
                "checks_pending_cap": 10800},
    "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
           "quarantine": [], "nightly_time": "02:00"},
    "notify": {"imessage_to": None, "cmd": None},
    # janitor.aged_park_days (issue #62): how long a parked / needs-william issue may sit with
    # NO activity (GitHub updatedAt) before `superlooper janitor` proposes closing it. A
    # proposal only — nothing closes without the owner's explicit approval in the janitor's
    # own confirm step. 0 proposes every parked issue immediately.
    "janitor": {"aged_park_days": 14},
    "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
}

_ALLOWED_TOP = set(_TOP_DEFAULTS) | set(_NESTED_DEFAULTS) | {"repo"}
_AGENTS = {"claude", "codex"}
_AFFINITIES = {"hard", "soft"}
_MERGE_METHODS = {"squash", "merge", "rebase"}   # gh's own set; the runner defaults to squash (§B.4)
# `lanes` reserved-pool keys (issue #63): "build" hosts ALL merge-producing work (build AND
# diagnose-and-fix); "investigate" is the reserved investigation pool. A merge-producing issue
# never occupies an investigation lane — that reservation is the whole point.
_LANE_POOLS = ("build", "investigate")


def _err(msg):
    raise ValueError(f"invalid .superlooper/config.json: {msg}")


def _validate_lanes(v):
    """`lanes` is EITHER a plain integer >= 1 (today's single shared pool — full back-compat) OR an
    object {"build": N, "investigate": M} splitting capacity into two STRICT pools: N lanes for
    merge-producing work plus M reserved for investigations. Fails loud and specific either way."""
    _obj_hint = ("or an object with 'build' and 'investigate' pool sizes "
                 "(e.g. {\"build\": 1, \"investigate\": 1})")
    if isinstance(v, dict):
        for k in v:
            if k not in _LANE_POOLS:
                _err(f"unknown key 'lanes.{k}' (allowed: {', '.join(_LANE_POOLS)})")
        # Opting into the object form is a conscious split, so BOTH pools must be stated — a lone
        # {"build": 2} silently zeroing investigations is exactly the surprise this rejects.
        missing = [k for k in _LANE_POOLS if k not in v]
        if missing:
            _err(f"'lanes' object must set both {' and '.join(repr(k) for k in _LANE_POOLS)} "
                 f"(missing: {', '.join(missing)})")
        total = 0
        for k in _LANE_POOLS:
            n = v[k]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                _err(f"'lanes.{k}' must be an integer >= 0, got {n!r}")
            total += n
        if total < 1:
            _err("'lanes' pools must sum to at least 1 (both 'build' and 'investigate' are 0 — "
                 "nothing would ever launch)")
        return
    # int form: bool is an int subclass (True == 1) so it must be rejected explicitly.
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        _err(f"'lanes' must be an integer >= 1 {_obj_hint}, got {v!r}")


def _validate_and_fill(raw):
    if not isinstance(raw, dict):
        _err(f"top level must be a JSON object, got {type(raw).__name__}")

    # repo — required, "owner/name" shape (also what state_home splits on).
    repo = raw.get("repo")
    if repo is None:
        _err("missing required key 'repo' (expected \"owner/name\")")
    if not isinstance(repo, str) or repo.count("/") != 1 or any(not p.strip() for p in repo.split("/")):
        _err(f"'repo' must be \"owner/name\", got {repo!r}")

    # unknown top-level keys -> loud (typo protection).
    for k in raw:
        if k not in _ALLOWED_TOP:
            _err(f"unknown key {k!r} (allowed: {', '.join(sorted(_ALLOWED_TOP))})")

    # Fill defaults. DEEP-COPY the default when a key is omitted: the list/dict defaults
    # (areas {}, required_checks [], qa.quarantine [], ...) are mutable, so handing out the
    # shared module-level object would let a caller mutating cfg[...] pollute every future
    # load (the same aliasing class as loopstate's DEFAULT_ISSUE). Present keys come straight
    # from the fresh json.loads result, which is never shared.
    out = {"repo": repo}
    for k, default in _TOP_DEFAULTS.items():
        out[k] = raw[k] if k in raw else copy.deepcopy(default)

    # --- typed checks on the top-level fields ---
    # bool is an int subclass and True == 1, so a bare `!= 1` would ACCEPT version: true.
    if isinstance(out["version"], bool) or not isinstance(out["version"], int) or out["version"] != 1:
        _err(f"unsupported config 'version' {out['version']!r} (this build understands version 1)")
    if not isinstance(out["dev_branch"], str) or not out["dev_branch"].strip():
        _err("'dev_branch' must be a non-empty string")
    if out["prod_branch"] is not None and (not isinstance(out["prod_branch"], str) or not out["prod_branch"].strip()):
        _err("'prod_branch' must be null or a non-empty string")
    if not isinstance(out["agent"], str) or out["agent"] not in _AGENTS:
        _err(f"'agent' must be one of {sorted(_AGENTS)}, got {out['agent']!r}")
    _validate_lanes(out["lanes"])
    # guard isinstance(str) BEFORE the set membership: an unhashable value (list/dict) would
    # raise a raw TypeError from `x in set`, breaking the "schema violation -> ValueError" contract.
    if not isinstance(out["affinity"], str) or out["affinity"] not in _AFFINITIES:
        _err(f"'affinity' must be one of {sorted(_AFFINITIES)}, got {out['affinity']!r}")
    if not isinstance(out["merge_method"], str) or out["merge_method"] not in _MERGE_METHODS:
        _err(f"'merge_method' must be one of {sorted(_MERGE_METHODS)}, got {out['merge_method']!r}")
    for flag in ("touches_required", "cleanup_merged_worktrees"):
        if not isinstance(out[flag], bool):
            _err(f"'{flag}' must be true or false, got {out[flag]!r}")
    for listkey in ("required_checks", "bright_lines", "report_required_sections"):
        v = out[listkey]
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            _err(f"'{listkey}' must be a list of strings")
    for strornull in ("ship_cmd", "ship_recheck_cmd"):
        v = out[strornull]
        if v is not None and (not isinstance(v, str) or not v.strip()):
            _err(f"'{strornull}' must be null or a non-empty string")
    for timekey in ("report_time",):
        if not isinstance(out[timekey], str):
            _err(f"'{timekey}' must be a string like \"08:45\"")

    # areas: dict of area-name -> list of glob strings.
    areas = out["areas"]
    if not isinstance(areas, dict):
        _err(f"'areas' must be an object of area -> [globs], got {type(areas).__name__}")
    for area, globs in areas.items():
        if not isinstance(globs, list) or any(not isinstance(g, str) for g in globs):
            _err(f"'areas.{area}' must be a list of glob strings, got {globs!r}")

    # nested dicts: deep-merge + reject unknown sub-keys.
    for field, sub_defaults in _NESTED_DEFAULTS.items():
        given = raw.get(field, {})
        if not isinstance(given, dict):
            _err(f"'{field}' must be an object, got {type(given).__name__}")
        for sk in given:
            if sk not in sub_defaults:
                _err(f"unknown key '{field}.{sk}' (allowed: {', '.join(sorted(sub_defaults))})")
        merged = copy.deepcopy(sub_defaults)   # deepcopy so qa.quarantine's default list isn't shared
        merged.update(given)                   # given values are fresh from json.loads
        out[field] = merged

    # --- typed checks on the nested sub-fields (unknown-key rejection above is not enough: a
    # wrong-TYPED value would load clean and only blow up later in the runner/notify/QA code) ---
    for mk in ("worker", "answerer"):
        v = out["models"][mk]
        if not isinstance(v, str) or not v.strip():
            _err(f"'models.{mk}' must be a non-empty string, got {v!r}")
    # worker_effort: null (no default effort) or a non-empty string (pass-through — a bad value
    # fails the launch loudly and the retry cap parks, no allowlist).
    we = out["models"]["worker_effort"]
    if we is not None and (not isinstance(we, str) or not we.strip()):
        _err(f"'models.worker_effort' must be null or a non-empty string, got {we!r}")
    for sk in ("idle_seconds", "freeze_seconds", "retry_cap", "conflict_cap", "checks_pending_cap"):
        v = out["session"][sk]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            _err(f"'session.{sk}' must be an integer >= 0, got {v!r}")
    for cmdkey in ("nightly_cmd", "results_glob"):
        v = out["qa"][cmdkey]
        if v is not None and (not isinstance(v, str) or not v.strip()):
            _err(f"'qa.{cmdkey}' must be null or a non-empty string, got {v!r}")
    if not isinstance(out["qa"]["retry_once"], bool):
        _err(f"'qa.retry_once' must be true or false, got {out['qa']['retry_once']!r}")
    q = out["qa"]["quarantine"]
    if not isinstance(q, list) or any(not isinstance(x, str) for x in q):
        _err(f"'qa.quarantine' must be a list of strings, got {q!r}")
    if not isinstance(out["qa"]["nightly_time"], str):
        _err(f"'qa.nightly_time' must be a string like \"02:00\", got {out['qa']['nightly_time']!r}")
    v = out["janitor"]["aged_park_days"]
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        _err(f"'janitor.aged_park_days' must be an integer >= 0, got {v!r}")
    for nk in ("imessage_to", "cmd"):
        v = out["notify"][nk]
        if v is not None and (not isinstance(v, str) or not v.strip()):
            _err(f"'notify.{nk}' must be null or a non-empty string, got {v!r}")
    for ck in ("dangerous_bypass", "bypass_hook_trust", "no_alt_screen"):
        if not isinstance(out["codex"][ck], bool):
            _err(f"'codex.{ck}' must be true or false, got {out['codex'][ck]!r}")

    return out


def load(repo_path):
    """Read + validate `<repo_path>/.superlooper/config.json`, returning a dict with all defaults
    filled. Missing file -> FileNotFoundError naming the exact path; malformed JSON or a schema
    violation -> ValueError naming the offender."""
    cfg_path = Path(repo_path) / ".superlooper" / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"no superlooper config at {cfg_path} — run `superlooper adopt` in {repo_path} first")
    try:
        raw = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{cfg_path} is not valid JSON: {e}") from e
    return _validate_and_fill(raw)


def path_to_area(config, path):
    """Map a repo-relative file path to its declared area (fnmatch against `areas`, FIRST match
    wins in declared order), else the wildcard area '*' (which overlaps everything under hard
    affinity — a file in no declared area conflicts with any lane, the safe default)."""
    for area, globs in config.get("areas", {}).items():
        for g in globs:
            if fnmatch.fnmatch(path, g):
                return area
    return "*"


def state_home(config):
    """The per-repo state directory: `~/.superlooper/<owner>__<repo>/`. `SL_HOME` overrides the
    `~/.superlooper` base (tests point it at a tmp dir; it also lets a friend relocate state)."""
    owner, name = config["repo"].split("/", 1)
    base = os.environ.get("SL_HOME") or os.path.expanduser("~/.superlooper")
    return Path(base) / f"{owner}__{name}"
