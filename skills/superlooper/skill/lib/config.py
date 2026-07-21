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

# The night window during which routine owner-DECISION pages are batched to the morning report
# (issue #164). The ONE source of truth: the notify default below embeds it, and actions.py imports
# it as the fallback for an OLD config.json that predates the key — so the two can never drift.
DEFAULT_QUIET_HOURS = {"start": "21:00", "end": "08:00"}

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
    # report_required_sections (issue #57): the H2 headings a worker's final report must carry with
    # real prose (the gate checks presence mechanically). The DEFAULT must be honestly satisfiable by
    # ANY repo — a CLI/library/service worker can never produce "Browser evidence", so a browser-only
    # default nudged-then-parked every finished issue on a fresh adopt of a non-web repo. So the
    # shipped floor is exactly the two things every worker is ALREADY required to produce: passing
    # Tests (TDD + required_checks) and a fresh-agent Review (gate step 2b). A web/UI repo opts back
    # into richer evidence by setting this list explicitly (see config.example.json / ADOPTING.md,
    # e.g. ["Tests", "Browser evidence", "Regression tests", "Review"]). Must stay NON-empty: an empty
    # required list is vacuously ok at the gate, silently disabling the section check.
    "report_required_sections": ["Tests", "Review"],
    "bright_lines": [],
    # Prune a merged lane's WORKTREE once its session has ended. Strictly about the checkout (#178):
    # false keeps the merged worktree on disk for inspection, and nothing else — the session is still
    # closed, under auto_close_merged_windows just below. It used to imply "leave the coding CLI
    # running" too, which since #155 (absorb_merged can fire on an in-flight lane) meant a worker left
    # building against a branch that had already merged.
    "cleanup_merged_worktrees": True,
    # Auto-close a lane's cmux window (and, ordered by #149, reclaim its worktree unless
    # cleanup_merged_worktrees says keep it) once it has SUCCESSFULLY MERGED and landed — owner ruling
    # 2026-07-16 (#168). Default True: a merged lane is truly done and never resurrected, so its
    # finished session need not linger. Set false to keep merged windows/worktrees for inspection —
    # the pre-#149 "nothing auto-closed" posture as an explicit opt-out; `superlooper tidy` then
    # remains the owner's explicit word to close the WINDOW (tidy closes windows only, never prunes a
    # worktree, so the merged checkout then stays on disk for manual inspection). Off, the worktree
    # persists too: a prune can never run under the live CLI this would leave open (#149). This is the
    # only #149-family auto-close of a merged lane; park-family lanes are NEVER auto-closed while
    # live (see below).
    "auto_close_merged_windows": True,
    # Reclaim the worktrees of park-family terminal issues (parked/needs-william/bounced) on every
    # tick. Owner ruling 2026-07-16 (#168): DEFAULT FALSE — the owner must be able to open the window
    # of stalled work and look at the session, so a park-family lane's window AND worktree simply
    # persist until an owner verb (re-approve / drop / tidy) resolves the lane. Set true to opt back
    # into the disk-bounding reaper (#41) on a disk-constrained machine, accepting that it closes
    # park-family windows to do so; the #190 dirty/unpushed refusal still guards every prune.
    "cleanup_parked_worktrees": False,
    "report_time": "08:45",
}

# Nested dict fields — deep-merged one level so a partial override (e.g. session.retry_cap) keeps
# the sibling defaults instead of wiping them. Unknown sub-keys inside these are rejected too.
_NESTED_DEFAULTS = {
    # opus[1m] for BOTH (owner ruling 2026-07-06): this loader default WINS over
    # runner._worker_model()'s fallback (a filled-in value is truthy), so it must carry the ruling
    # itself — a stale "fable" here silently starved the hired seat on repos that omit `models`
    # (the eApp session's 2026-07-06 catch). Repos override either key explicitly.
    # `debugger` is the watchdog's unattended sl-debugger seat (issue #66) — the field WAS
    # `models.answerer`, renamed when #194 retired the answerer, because the debugger became its
    # only reader (a hired-judgment seat, so it keeps the same strongest-configuration default).
    # An old config still saying `answerer` fails loud as an unknown key, naming `debugger` in the
    # allowed list — the owner renames the pin instead of silently losing it.
    # worker_effort defaults to None (owner ruling 2026-07-07): a repo-wide reasoning-effort
    # default for WORKER launches. None means exactly today's behaviour — NEVER send --effort. It
    # MUST stay a genuine null: unlike worker/debugger, a filled-in truthy default would WIN over
    # the runner's no-flag fallback and force an effort on every repo that omits the field (the
    # stale-fable trap). A per-issue effort:* label overrides it; the debugger never reads it.
    # reviewer/reviewer_effort (issue #158): the per-repo pin for the CROSS-REVIEWER's model +
    # reasoning-effort. The 2026-07-14→15 incident began when the owner changed his machine-global
    # Codex config for unrelated work and every in-flight cross-review silently ran at ultra effort,
    # timed out, and aged workers past the freeze threshold — because the plugin's cross-review ran
    # `codex exec` BARE (no -m, no -c model_reasoning_effort), inheriting ~/.codex/config.toml. So
    # both fields carry CONCRETE non-null defaults (unlike worker_effort, whose null means "no flag"):
    # a null here would re-open the bare-invocation hole — the review must ALWAYS pass an explicit
    # flag. `reviewer` is a codex model (the default cross-review path; a Claude-only machine falls
    # back to a fresh subagent that needs no model flag); `reviewer_effort` is a bounded, reliably-
    # under-timeout tier. A repo overrides either; the codex flag syntax lives in bin/cross-review.sh
    # (agent boundary), so the pin stays per-repo config, never a hardcoded Codex fact in the core.
    "models": {"worker": "opus[1m]", "debugger": "opus[1m]", "worker_effort": None,
               "reviewer": "gpt-5.5", "reviewer_effort": "medium"},
    # checks_pending_cap (issue #26): seconds a FINISHED PR may sit with its required checks
    # PENDING before the runner escalates ONCE to needs-william (naming the unreported checks).
    # The merge decision stays fail-closed — pending never merges — this only bounds the wait so a
    # required check that never reports can't hold a green PR gating forever, silently. Default
    # 10800 (3h) clears any real CI run; a huge value effectively disables the bound.
    "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2, "conflict_cap": 2,
                "checks_pending_cap": 10800},
    "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
           "quarantine": [], "nightly_time": "02:00"},
    # notify.quiet_hours (issue #164): the window during which routine owner-DECISION pages (a park,
    # a bounce, a durable question) are BATCHED to the morning report instead of pushed — nobody
    # answers a 3am page and a park is a safe state. Systemic-stop ALERTs (runner/auth dead, whole
    # queue stalled) and the merge-freeze notice always push; only the owner-decision hand-backs are
    # held. Defaults ON, 21:00–08:00 (end EXCLUSIVE, wraps midnight); an explicit null DISABLES the
    # batching (every hand-back pages immediately, the pre-#164 behaviour).
    "notify": {"imessage_to": None, "cmd": None, "quiet_hours": dict(DEFAULT_QUIET_HOURS)},
    # janitor.aged_park_days (issue #62): how long a parked / needs-william issue may sit with
    # NO activity (GitHub updatedAt) before `superlooper janitor` proposes closing it. A
    # proposal only — nothing closes without the owner's explicit approval in the janitor's
    # own confirm step. 0 proposes every parked issue immediately.
    "janitor": {"aged_park_days": 14},
    "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True},
    # watchdog (issue #66): the unattended-debugger fallback. `authority` is the standing
    # tier the launched sl-debugger session runs at — DEFAULT `full` (owner standing rule
    # 2026-07-10); even `full` excludes the constitution absolutely, enforced by the
    # sl-debugger skill's unattended contract, never relaxed here. `allowlist` names the
    # exact repair verbs permitted at the `allowlist` tier. grace_minutes is the text->launch
    # wait; the two *_minutes bounds tune the stale-heartbeat and no-progress detectors.
    # resurrection_max_per_hour (issue #208): a runner that is PROVABLY GONE (heartbeat stale AND
    # its recorded pid dead) is automatically restarted — the runner is a deterministic, zero-token
    # process, so it should restart as often as it needs to. This caps the restarts in a rolling
    # hour; hitting the cap escalates loudly and pauses (a repeatedly-dying runner is an incident,
    # not a flap). 0 disables auto-restart (escalate on the first provably-gone check instead).
    "watchdog": {"authority": "full", "allowlist": [], "grace_minutes": 30,
                 "heartbeat_stale_minutes": 20, "no_progress_minutes": 30,
                 "resurrection_max_per_hour": 5},
}

_ALLOWED_TOP = set(_TOP_DEFAULTS) | set(_NESTED_DEFAULTS) | {"repo", "operator"}
_AGENTS = {"claude", "codex"}
_AFFINITIES = {"hard", "soft"}
_MERGE_METHODS = {"squash", "merge", "rebase"}   # gh's own set; the runner defaults to squash (§B.4)
# `lanes` reserved-pool keys (issue #63): "build" hosts ALL merge-producing work (build AND
# diagnose-and-fix); "investigate" is the reserved investigation pool. A merge-producing issue
# never occupies an investigation lane — that reservation is the whole point.
_LANE_POOLS = ("build", "investigate")
# `required_checks` surfaces (issue #52): "pr" gates PR merges (§C.4 step 5), "dev" gates the dev
# freeze/unfreeze poll. The object form declares them separately; a flat list gates BOTH.
_CHECK_SURFACES = ("pr", "dev")
# watchdog.authority tiers (issue #66): what the unattended sl-debugger session may do.
_WATCHDOG_AUTHORITIES = {"diagnose-only", "allowlist", "full"}


def _err(msg):
    raise ValueError(f"invalid .superlooper/config.json: {msg}")


_ASCII_DIGITS = frozenset("0123456789")


def _valid_hhmm(v):
    """A zero-padded 24h clock time "HH:MM" in range — the shape quiet_hours (issue #164) needs, and
    what the runner's `time.strftime('%H:%M')` local clock always produces, so the actions-side
    lexical compare is a true time-of-day order. ASCII digits only: `str.isdigit()` is True for
    superscripts/other Unicode numerics that would then RAISE in int() (and compare wrongly against
    the ASCII clock), so membership against 0-9 is used instead of isdigit()."""
    return (isinstance(v, str) and len(v) == 5 and v[2] == ":"
            and set(v[:2]) <= _ASCII_DIGITS and set(v[3:]) <= _ASCII_DIGITS
            and 0 <= int(v[:2]) <= 23 and 0 <= int(v[3:]) <= 59)


def _validate_quiet_hours(qh):
    """`notify.quiet_hours` is EITHER null (batching disabled — every hand-back pages immediately) OR
    an object {"start": "HH:MM", "end": "HH:MM"}. Fails loud + specific: a typo'd time or a missing
    side is a clear adopt-time error, never a window that silently never matches (issue #164)."""
    if qh is None:
        return
    if not isinstance(qh, dict):
        _err("'notify.quiet_hours' must be null or an object "
             '{"start": "HH:MM", "end": "HH:MM"}, got %r' % (qh,))
    for k in qh:
        if k not in ("start", "end"):
            _err(f"unknown key 'notify.quiet_hours.{k}' (allowed: start, end)")
    for k in ("start", "end"):
        if k not in qh:
            _err(f"'notify.quiet_hours' must set both 'start' and 'end' (missing: {k})")
        if not _valid_hhmm(qh[k]):
            _err(f"'notify.quiet_hours.{k}' must be a zero-padded 24h time \"HH:MM\", got {qh[k]!r}")


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


def _validate_required_checks(v):
    """`required_checks` is EITHER a plain list of check-name strings (today — the SAME set gates
    both PR merges and the dev freeze/unfreeze, full back-compat) OR an object
    {"pr": [...], "dev": [...]} declaring the two surfaces SEPARATELY (issue #52). The split lets a
    repo EXCLUDE a check that gates PRs but never reports on the dev branch (a ship status stamped
    on PR head commits only, which the post-squash-merge dev HEAD never receives) from the dev set —
    otherwise the widened dev poll reads it `pending` forever and a mainline freeze never lifts.
    Fails loud + specific either way. An empty list on either surface is allowed here (doctor gates
    the PR set non-empty at adopt time, exactly like a bare `required_checks: []` still loads)."""
    if isinstance(v, dict):
        for k in v:
            if k not in _CHECK_SURFACES:
                _err(f"unknown key 'required_checks.{k}' (allowed: {', '.join(_CHECK_SURFACES)})")
        # opting into the object form is a conscious split, so BOTH surfaces must be stated — a lone
        # {"pr": [...]} silently defaulting dev back to pr would recreate the stranded-freeze bug.
        missing = [k for k in _CHECK_SURFACES if k not in v]
        if missing:
            _err(f"'required_checks' object must set both "
                 f"{' and '.join(repr(k) for k in _CHECK_SURFACES)} (missing: {', '.join(missing)})")
        for k in _CHECK_SURFACES:
            names = v[k]
            if not isinstance(names, list) or any(not isinstance(x, str) for x in names):
                _err(f"'required_checks.{k}' must be a list of strings, got {names!r}")
        return
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        _err("'required_checks' must be a list of strings, or an object "
             '{"pr": [...], "dev": [...]} splitting PR-required from dev-required checks, '
             f"got {v!r}")


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

    # operator display name (issue #58): the name every stranger-visible runtime string signs with —
    # briefs, park memos, label descriptions, dashboard audit trail. Defaults to the repo owner's
    # GitHub login (the part before "/"), so a fresh adopt attributes the loop's work to the actual
    # owner and never a hardcoded person. null/absent -> that default; a present value must be a
    # non-empty string (a blank or a typo fails loud, like every other field). Resolved into `out`
    # below, after the defaults fill (so it rides the same out dict the loader returns).
    raw_operator = raw.get("operator")
    if not (raw_operator is None or (isinstance(raw_operator, str) and raw_operator.strip())):
        _err(f"'operator' must be null or a non-empty string, got {raw_operator!r}")

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
    for flag in ("touches_required", "cleanup_merged_worktrees", "cleanup_parked_worktrees",
                 "auto_close_merged_windows"):
        if not isinstance(out[flag], bool):
            _err(f"'{flag}' must be true or false, got {out[flag]!r}")
    for listkey in ("bright_lines", "report_required_sections"):
        v = out[listkey]
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            _err(f"'{listkey}' must be a list of strings")
    # required_checks: list (both surfaces) OR {"pr":[...], "dev":[...]} (issue #52) — validated
    # by its own helper so the object form is accepted and its sub-keys rejected loudly by name.
    _validate_required_checks(out["required_checks"])
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
    for mk in ("worker", "debugger"):
        v = out["models"][mk]
        if not isinstance(v, str) or not v.strip():
            _err(f"'models.{mk}' must be a non-empty string, got {v!r}")
    # worker_effort: null (no default effort) or a non-empty string (pass-through — a bad value
    # fails the launch loudly and the retry cap parks, no allowlist).
    we = out["models"]["worker_effort"]
    if we is not None and (not isinstance(we, str) or not we.strip()):
        _err(f"'models.worker_effort' must be null or a non-empty string, got {we!r}")
    # reviewer / reviewer_effort (issue #158): NO valid null. A null model would omit `-m` and a null
    # effort would omit `-c model_reasoning_effort=`, and either omission lets codex read the
    # machine-global config — the exact ambient-poison the pin exists to end. So both are required
    # non-empty strings, validated like worker/debugger (a blank or wrong type fails the adopt loudly).
    for rk in ("reviewer", "reviewer_effort"):
        v = out["models"][rk]
        if not isinstance(v, str) or not v.strip():
            _err(f"'models.{rk}' must be a non-empty string (the cross-reviewer pin has no valid "
                 f"null — a review must never inherit the machine-global Codex config), got {v!r}")
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
    _validate_quiet_hours(out["notify"]["quiet_hours"])
    for ck in ("dangerous_bypass", "bypass_hook_trust", "no_alt_screen"):
        if not isinstance(out["codex"][ck], bool):
            _err(f"'codex.{ck}' must be true or false, got {out['codex'][ck]!r}")
    wd = out["watchdog"]
    if not isinstance(wd["authority"], str) or wd["authority"] not in _WATCHDOG_AUTHORITIES:
        _err(f"'watchdog.authority' must be one of {sorted(_WATCHDOG_AUTHORITIES)}, "
             f"got {wd['authority']!r}")
    if not isinstance(wd["allowlist"], list) or any(not isinstance(x, str) for x in wd["allowlist"]):
        _err(f"'watchdog.allowlist' must be a list of strings, got {wd['allowlist']!r}")
    # grace may be 0 (launch on the tripping check); the detection bounds must be >= 1 —
    # a zero bound would trip on any instantaneous glimpse of the condition. resurrection_max_per_hour
    # may be 0 (disables auto-restart -> escalate immediately on a provably-gone runner).
    for wk, lo in (("grace_minutes", 0), ("heartbeat_stale_minutes", 1),
                   ("no_progress_minutes", 1), ("resurrection_max_per_hour", 0)):
        v = wd[wk]
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            _err(f"'watchdog.{wk}' must be an integer >= {lo}, got {v!r}")

    # Fill the operator (validated above): explicit non-blank value wins, else the repo owner login.
    out["operator"] = raw_operator.strip() if isinstance(raw_operator, str) else repo.split("/", 1)[0].strip()

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


def operator(config):
    """The operator display name — what every stranger-visible runtime string signs with (issue
    #58): briefs, park memos, notifications, the morning report. Prefers an explicit non-blank
    ``operator``, else the repo owner's GitHub login (the part before "/"), else the neutral
    'the owner'. Never raises: the pure decision cores (gate/brief/report) call it while staying
    fail-closed on wrong-typed config."""
    if isinstance(config, dict):
        op = config.get("operator")
        if isinstance(op, str) and op.strip():
            return op.strip()
        repo = config.get("repo")
        if isinstance(repo, str) and repo.count("/") == 1:
            owner = repo.split("/", 1)[0].strip()
            if owner:
                return owner
    return "the owner"


def pr_required_checks(config):
    """The required checks that gate PR merges — §C.4 step 5 folds the PR statusCheckRollup down to
    THIS set (issue #52). `required_checks` is EITHER a flat list (the same set gates both surfaces)
    OR {"pr":[...], "dev":[...]}; this returns the PR set from whichever form is present. Extracts
    the PR surface INDEPENDENTLY of dev: a cleanly-typed list survives even if `dev` is malformed.
    A wrong-typed/absent value degrades to []; note an EMPTY set is vacuously GREEN at the gate (not
    pending — see gate.required_checks_state, cross-review C3), so an empty PR set does NOT fail
    closed. The backstop against that is `doctor` (adopt-time), which FAILs hard on an empty PR set;
    the loader also rejects wrong-typed config before it can ever reach the gate."""
    rc = config.get("required_checks") if isinstance(config, dict) else None
    if isinstance(rc, dict):
        pr = rc.get("pr")
        return pr if isinstance(pr, list) else []
    return rc if isinstance(rc, list) else []


def dev_required_checks(config):
    """The required checks expected to report on (and gate the freeze/unfreeze of) the DEV branch
    (issue #52). A flat `required_checks` list applies to both surfaces (back-compat); the object
    form's `dev` set lets a repo EXCLUDE a PR-only check that never reports on dev — which would
    otherwise strand a mainline freeze forever. Extracts the dev surface INDEPENDENTLY of pr; a
    wrong-typed/absent value degrades to []. An EMPTY dev set is a legitimate choice (a repo whose
    CI runs on PRs only): the freeze mechanism then idles — it never freezes on dev, and it lifts an
    existing freeze (an empty required set is vacuously green). The loader rejects wrong-typed config,
    so the []-on-garbage path is defensive only."""
    rc = config.get("required_checks") if isinstance(config, dict) else None
    if isinstance(rc, dict):
        dev = rc.get("dev")
        return dev if isinstance(dev, list) else []
    return rc if isinstance(rc, list) else []


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
