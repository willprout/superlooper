"""The dashboard's config contract (Task 1 / decisions B.4, B.7).

The command center is shareable from day one (decision A.3): every per-user fact — which repos to
watch, ports, poll cadences, notify settings, the fun-toggle map — enters through THIS file's
``config.json``, never a hardcoded William-specific path. So the loader has one job beyond reading
JSON: fail LOUD and SPECIFIC. An unknown key, a wrong type, or an out-of-range port names the
offender, so a typo is a clear startup error rather than a dashboard that quietly watches the
wrong thing. The validation style deliberately mirrors the superlooper skill's ``lib/config.py``
(hand-rolled, stdlib only — the runtime carries no pip deps).

Two things the dashboard's config does NOT carry, because each configured repo already declares
them in its own ``.superlooper/config.json`` (decision B.4 — explicit, no scanning magic): the
repo's slug (``owner/name`` → its state home) and its idle/freeze liveness thresholds. For every
listed repo the loader reads that file and folds those facts into the repo entry, defaulting the
thresholds to 480/2700 s when the repo omits them.
"""
import copy
import json
import os
from pathlib import Path

# Top-level scalar fields and their defaults. `repos` is the ONLY required key (no sensible
# default — a dashboard with nothing to watch is a mistake worth flagging loudly).
_TOP_DEFAULTS = {
    "version": 1,
    "port": 8611,
    "poll_seconds": 2,            # front-end + journal/state re-read cadence (decision B.2)
    "gh_poll_seconds": 30,        # the slower `gh` clock
    "heartbeat_down_seconds": 300,  # runner heartbeat age → RUNNER DOWN surface (Task 10 hook)
}

# The notify block mirrors the skill's shape (decision B.4): imessage_to → cmd → log precedence,
# resolved by Task 10. Both null by default (a fresh shareable install nags no one).
_NOTIFY_DEFAULTS = {"imessage_to": None, "cmd": None}

# The fun-toggle map: one master switch (design record §7) plus a per-mechanic key for every
# fun mechanic that ships with the MVP (§7 "Ship with the MVP" + the Solari clack, B.10). All
# default ON — joy is a first-class, terminal requirement (design record §0.1), so the honest
# default is fun-fully-on; a user dials it back explicitly. New mechanics (T7/T8) extend this map.
_FUN_DEFAULTS = {
    "master": True,          # the one switch that gates everything below
    "solari": True,          # the Solari arrivals board (the flagship delight moment)
    "solari_clack": True,    # its optional mechanical clack (sound; ships low, B.10)
    "airlines": True,        # repos rendered as airlines (name + crest + colors)
    "living_clock": True,    # wall-clock time drives field lighting
    "corner_counter": True,  # the always-visible feel-good stats corner
    "incident_sign": True,   # "N landings since the last incident"
}

# Per-repo idle/freeze liveness thresholds, defaulted when the repo omits `session` (decision B.4).
_REPO_THRESHOLD_DEFAULTS = {"idle_seconds": 480, "freeze_seconds": 2700}

# The local superlooper CLI the Tidy button drives (issue #41). A ~-relative default pointing at the
# installed skill's own bin, so it works for any user out of the box (shareability, decision A.3);
# overridable in config for a non-standard install. Tilde-expanded at load (``~`` → the user's home)
# so no literal '~' ever leaks into a command invocation; the default thereby resolves to an
# absolute path (a relative override stays relative, resolved against cwd like gh's bare ``gh``).
_DEFAULT_SUPERLOOPER_CLI = "~/.claude/skills/superlooper/bin/superlooper"

_ALLOWED_TOP = set(_TOP_DEFAULTS) | {"repos", "notify", "fun", "superlooper_cli", "operator"}
_ALLOWED_REPO_ENTRY = {"path", "airline"}

# Every fun mechanic except the master switch — the snapshot resolves each against master so the
# front-end binds plain booleans (design record B.1) instead of re-deriving the gating.
FUN_MECHANICS = tuple(sorted(k for k in _FUN_DEFAULTS if k != "master"))

_MIN_PORT, _MAX_PORT = 1, 65535


def _err(msg):
    raise ValueError(f"invalid config.json: {msg}")


def _is_int(v):
    # bool is an int subclass and True == 1, so a bare isinstance(v, int) would ACCEPT a boolean.
    return isinstance(v, int) and not isinstance(v, bool)


def load(config_path):
    """Read + validate the dashboard's ``config.json`` at ``config_path``, returning a dict with
    every default filled and each repo entry enriched with its slug/thresholds/state-home.

    Missing file → ``FileNotFoundError``. Malformed JSON or any schema violation → ``ValueError``
    naming the offender.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"no dashboard config at {path} — copy config.example.json first")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    return _validate_and_fill(raw)


def _validate_and_fill(raw):
    if not isinstance(raw, dict):
        _err(f"top level must be a JSON object, got {type(raw).__name__}")

    # unknown top-level keys → loud (typo protection). This runs BEFORE the required-key check so
    # a misspelled key (`reops`) names the actual offender rather than reporting `repos` missing.
    for k in raw:
        if k not in _ALLOWED_TOP:
            _err(f"unknown key {k!r} (allowed: {', '.join(sorted(_ALLOWED_TOP))})")

    # repos — required, non-empty (no sensible default; an empty watch-list is a misconfiguration).
    if "repos" not in raw:
        _err("missing required key 'repos' (a list of {\"path\": \"...\"} checkout entries)")

    # Fill scalar defaults. Present keys come straight from the fresh json.loads result.
    out = {}
    for k, default in _TOP_DEFAULTS.items():
        out[k] = raw[k] if k in raw else copy.deepcopy(default)

    # --- typed checks on the scalars ---
    if isinstance(out["version"], bool) or out["version"] != 1:
        _err(f"unsupported 'version' {out['version']!r} (this build understands version 1)")
    if not _is_int(out["port"]) or not (_MIN_PORT <= out["port"] <= _MAX_PORT):
        _err(f"'port' must be an integer in {_MIN_PORT}..{_MAX_PORT}, got {out['port']!r}")
    for sk in ("poll_seconds", "gh_poll_seconds", "heartbeat_down_seconds"):
        v = out[sk]
        if not _is_int(v) or v < 1:
            _err(f"'{sk}' must be an integer >= 1, got {v!r}")

    # The Tidy button's local CLI path (issue #41): a non-empty string, expanded — present-but-wrong
    # fails loud by name (a mistyped path here means the Tidy button can't run), never a silent
    # coerce to the default that would hide the misconfiguration.
    raw_cli = raw.get("superlooper_cli", _DEFAULT_SUPERLOOPER_CLI)
    if not isinstance(raw_cli, str) or not raw_cli.strip():
        _err(f"'superlooper_cli' must be a non-empty string path, got {raw_cli!r}")
    out["superlooper_cli"] = os.path.expanduser(raw_cli.strip())

    out["notify"] = _fill_and_check_map("notify", raw.get("notify", {}), _NOTIFY_DEFAULTS,
                                        _check_str_or_null)
    out["fun"] = _fill_and_check_map("fun", raw.get("fun", {}), _FUN_DEFAULTS, _check_bool)
    out["repos"] = _load_repos(raw["repos"])

    # operator display name (issue #58): the name the command center signs its audit trail with —
    # "Approved by <operator> via command-center", the needs-you cards, the digest. One person runs
    # this localhost dashboard, so it is a single top-level field. Defaults to the owner of the FIRST
    # watched repo (a shareable install watches the operator's own repos), so a fresh config signs
    # the operator's own name and never a hardcoded "William". null/absent -> that default; a present
    # value must be a non-empty string (a blank or typo fails loud, like every other field).
    raw_operator = raw.get("operator")
    if raw_operator is None:
        out["operator"] = out["repos"][0]["owner"]
    elif isinstance(raw_operator, str) and raw_operator.strip():
        out["operator"] = raw_operator.strip()
    else:
        _err(f"'operator' must be null or a non-empty string, got {raw_operator!r}")
    return out


def _fill_and_check_map(field, given, defaults, check_value):
    """Deep-merge one level over ``defaults``, rejecting unknown sub-keys, then type-check each
    resulting value with ``check_value(field, key, value)``."""
    if not isinstance(given, dict):
        _err(f"'{field}' must be an object, got {type(given).__name__}")
    for sk in given:
        if sk not in defaults:
            _err(f"unknown key '{field}.{sk}' (allowed: {', '.join(sorted(defaults))})")
    merged = copy.deepcopy(defaults)
    merged.update(given)
    for k, v in merged.items():
        check_value(field, k, v)
    return merged


def _check_str_or_null(field, key, v):
    if v is not None and (not isinstance(v, str) or not v.strip()):
        _err(f"'{field}.{key}' must be null or a non-empty string, got {v!r}")


def _check_bool(field, key, v):
    if not isinstance(v, bool):
        _err(f"'{field}.{key}' must be true or false, got {v!r}")


def _load_repos(raw_repos):
    if not isinstance(raw_repos, list) or not raw_repos:
        _err(f"'repos' must be a non-empty list of {{\"path\": \"...\"}} entries, got {raw_repos!r}")
    repos = []
    for i, entry in enumerate(raw_repos):
        if not isinstance(entry, dict):
            _err(f"'repos[{i}]' must be an object with a 'path', got {type(entry).__name__}")
        for k in entry:
            if k not in _ALLOWED_REPO_ENTRY:
                _err(f"unknown key 'repos[{i}].{k}' (allowed: {', '.join(sorted(_ALLOWED_REPO_ENTRY))})")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            _err(f"'repos[{i}].path' must be a non-empty string, got {raw_path!r}")
        airline = entry.get("airline")
        if airline is not None and (not isinstance(airline, str) or not airline.strip()):
            _err(f"'repos[{i}].airline' must be a non-empty string when given, got {airline!r}")
        repos.append(_enrich_repo(os.path.expanduser(raw_path), airline))
    return repos


def default_airline(name):
    """The auto-generated airline name for a repo (design record §7 — identity serves legibility;
    renameable via the repo entry's ``airline`` key): the repo name with separators as spaces,
    title-cased. ``command-center`` → ``Command Center``."""
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    return " ".join(w.capitalize() for w in words)


def _enrich_repo(path, airline=None):
    """Fold in the facts the repo declares for itself (decision B.4): its slug and idle/freeze
    thresholds, read from the repo's own ``.superlooper/config.json``. That file MUST exist and
    carry a valid ``owner/name`` slug — without it the dashboard can't find the repo's state
    home, so a missing/slug-less config is a loud error naming the offending path. ``airline``
    is the user's override for the repo's airline name (§7 — renameable), defaulted from the
    repo name when absent."""
    repo_cfg = Path(path) / ".superlooper" / "config.json"
    if not repo_cfg.exists():
        _err(f"repo path {path!r}: no superlooper config at {repo_cfg} "
             f"— is that repo adopted into the loop?")
    try:
        body = json.loads(repo_cfg.read_text())
    except json.JSONDecodeError as e:
        _err(f"repo path {path!r}: {repo_cfg} is not valid JSON: {e}")
    if not isinstance(body, dict):
        _err(f"repo path {path!r}: {repo_cfg} top level must be a JSON object")

    slug = body.get("repo")
    if not isinstance(slug, str) or slug.count("/") != 1 or any(not p.strip() for p in slug.split("/")):
        _err(f"repo path {path!r}: its {repo_cfg} 'repo' must be \"owner/name\", got {slug!r}")
    owner, name = slug.split("/", 1)

    # The repo's session block only needs its two threshold keys read here — the dashboard does
    # NOT re-validate the skill's whole schema (out of scope; the repo carries keys like retry_cap
    # the dashboard never reads). But the two keys it DOES consume follow this repo's loud contract:
    # absent → default; present-but-malformed → a loud error naming the offender, never a silent
    # coerce to the default (which would hide a misconfiguration behind wrong liveness tiers).
    session = body.get("session", {})
    if not isinstance(session, dict):
        _err(f"repo path {path!r}: its {repo_cfg} 'session' must be an object, "
             f"got {type(session).__name__}")
    entry = {"path": path, "slug": slug, "owner": owner, "name": name,
             "airline": airline.strip() if airline else default_airline(name)}
    for tk, default in _REPO_THRESHOLD_DEFAULTS.items():
        if tk not in session:
            entry[tk] = default
            continue
        v = session[tk]
        if not _is_int(v) or v < 0:   # non-negative int (0 valid), mirroring the skill's contract
            _err(f"repo path {path!r}: its {repo_cfg} 'session.{tk}' must be "
                 f"an integer >= 0, got {v!r}")
        entry[tk] = v

    # The repo's configured lane count (issue #35): how many concurrent builds it runs, so the
    # empty-queue caption can state the truth ("N RUNWAYS OPEN") instead of a hardcoded "2". Unlike
    # the thresholds above — which this loader CONSUMES for liveness tiers, so a wrong one fails loud
    # — `lanes` drives ONLY a cosmetic caption and has no numeric default to fall back to. So an
    # absent OR unreadable value records None (unknown): downstream the caption then shows no number
    # rather than inventing one (the owner's honest-fallback ruling, 2026-07-10). Leniency hides no
    # misconfiguration here — a missing runway count on screen is itself the tell, where a wrong
    # number would be the thing that misleads.
    lanes = body.get("lanes")
    entry["lanes"] = lanes if (_is_int(lanes) and lanes >= 1) else None

    entry["state_home"] = state_home(slug)
    return entry


def state_home(slug):
    """The per-repo loop state directory: ``<base>/<owner>__<name>``. ``$SL_HOME`` overrides the
    ``~/.superlooper`` base (tests point it at a tmp dir; it also lets a friend relocate state) —
    same derivation as the skill's ``config.state_home`` so both agree on where state lives."""
    owner, name = slug.split("/", 1)
    base = os.environ.get("SL_HOME") or os.path.expanduser("~/.superlooper")
    return Path(base) / f"{owner}__{name}"


def operator(config):
    """The operator display name — what the command center signs its audit trail with (issue #58).
    Prefers an explicit non-blank ``operator``, else the neutral 'the owner'. Never raises: pure
    display functions (digest/tower/cards) call it and must stay fail-closed on a partial config.

    Unlike the skill's twin resolver (which can derive the owner from the single ``repo`` slug),
    this has no repo-owner fallback: a dashboard config carries a LIST of repos with possibly
    different owners, so there is no single owner to derive. That is why ``load`` fills ``operator``
    from the first watched repo's owner up front — every production caller passes a loaded config
    where the field is present, and this resolver's 'the owner' branch is the defensive floor only."""
    if isinstance(config, dict):
        op = config.get("operator")
        if isinstance(op, str) and op.strip():
            return op.strip()
    return "the owner"


def fun_enabled(config, mechanic):
    """Whether a fun mechanic should render: its own toggle AND the master switch (design record
    §7 — master gates everything). ``mechanic`` must be a known fun key (a typo is a bug, not a
    silent False)."""
    fun = config["fun"]
    if mechanic not in _FUN_DEFAULTS or mechanic == "master":
        raise ValueError(f"unknown fun mechanic {mechanic!r} (known: "
                         f"{', '.join(k for k in sorted(_FUN_DEFAULTS) if k != 'master')})")
    return bool(fun["master"]) and bool(fun[mechanic])
