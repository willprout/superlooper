"""Config contract (§C.1): load + validate `.superlooper/config.json`, fill defaults, and the
two pure helpers path_to_area / state_home. Hand-rolled validation, stdlib only (no jsonschema)."""
import json
from pathlib import Path

import pytest

import config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _REPO_ROOT / "skill" / "templates" / "config.example.json"


def _write_cfg(repo_path, obj):
    d = repo_path / ".superlooper"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(obj))
    return repo_path


# --------------------------- loading + defaults ---------------------------

def test_example_config_loads_clean(tmp_path):
    # the shipped template = §C.1 verbatim must validate and load with its stated values.
    raw = json.loads(_EXAMPLE.read_text())
    _write_cfg(tmp_path, raw)
    cfg = config.load(tmp_path)
    assert cfg["repo"] == "owner/name"
    assert cfg["lanes"] == 2
    assert cfg["affinity"] == "hard"
    assert cfg["merge_method"] == "squash"
    assert cfg["required_checks"] == ["review/local-gate", "quality-gate"]
    assert cfg["areas"]["frontend"] == ["src/components/**", "src/styles/**"]


def test_minimal_config_fills_defaults(tmp_path):
    # only `repo` supplied -> every other field defaults to the §C.1 values.
    _write_cfg(tmp_path, {"repo": "me/tool"})
    cfg = config.load(tmp_path)
    assert cfg["version"] == 1
    assert cfg["dev_branch"] == "main"
    assert cfg["prod_branch"] is None
    assert cfg["agent"] == "claude"
    assert cfg["lanes"] == 2
    assert cfg["affinity"] == "hard"
    assert cfg["areas"] == {}
    assert cfg["touches_required"] is True
    assert cfg["required_checks"] == []
    assert cfg["merge_method"] == "squash"
    assert cfg["ship_cmd"] is None and cfg["ship_recheck_cmd"] is None
    assert cfg["report_required_sections"] == ["Tests", "Review"]   # issue #57: web-agnostic default
    assert cfg["bright_lines"] == []
    assert cfg["models"] == {"worker": "opus[1m]", "answerer": "opus[1m]", "worker_effort": None}
    assert cfg["session"] == {"idle_seconds": 480, "freeze_seconds": 2700,
                              "retry_cap": 2, "conflict_cap": 2, "checks_pending_cap": 10800}
    assert cfg["qa"] == {"nightly_cmd": None, "results_glob": None, "retry_once": True,
                         "quarantine": [], "nightly_time": "02:00"}
    assert cfg["cleanup_merged_worktrees"] is True
    assert cfg["notify"] == {"imessage_to": None, "cmd": None}
    assert cfg["codex"] == {"dangerous_bypass": False, "bypass_hook_trust": True,
                            "no_alt_screen": True}
    assert cfg["report_time"] == "08:45"
    # watchdog (issue #66): authority DEFAULTS to full (owner standing rule 2026-07-10) —
    # the constitution's absolute exclusions are enforced by the sl-debugger contract, not here.
    assert cfg["watchdog"] == {"authority": "full", "allowlist": [], "grace_minutes": 30,
                               "heartbeat_stale_minutes": 20, "no_progress_minutes": 30}


# --------------------------- report sections default (issue #57) ---------------------------

def test_default_report_sections_are_web_agnostic(tmp_path):
    # issue #57: the SHIPPED default must be honestly satisfiable by ANY repo. A CLI/library/service
    # worker can never produce "Browser evidence", so demanding it in the default nudged-then-parked
    # every finished issue on a fresh adopt of a non-web repo. The universal floor is exactly the two
    # things every worker is ALREADY required to produce: passing tests (TDD + required_checks) and a
    # fresh-agent review (gate step 2b). A web repo opts back into browser evidence explicitly.
    _write_cfg(tmp_path, {"repo": "me/cli-tool"})
    secs = config.load(tmp_path)["report_required_sections"]
    assert secs == ["Tests", "Review"]
    assert "Browser evidence" not in secs
    # must stay NON-empty: report_sections_ok treats an empty required list as vacuously ok, so an
    # empty default would silently disable the section gate for every repo that omits the field.
    assert secs


def test_example_template_report_sections_are_web_agnostic():
    # `adopt` copies config.example.json VERBATIM, so its value is exactly what a fresh adopt writes.
    # It must carry the honest universal default, not the old browser-heavy list (issue #57).
    raw = json.loads(_EXAMPLE.read_text())
    assert raw["report_required_sections"] == ["Tests", "Review"]
    assert "Browser evidence" not in raw["report_required_sections"]


def test_explicit_report_sections_survive_load(tmp_path):
    # issue #57 DoD: a repo that sets report_required_sections explicitly is UNTOUCHED by the new
    # default — a web repo keeps its "Browser evidence" opt-in exactly as written, in order.
    web = ["Tests", "Browser evidence", "Regression tests", "Review"]
    _write_cfg(tmp_path, {"repo": "acme/webapp", "report_required_sections": web})
    assert config.load(tmp_path)["report_required_sections"] == web


# --------------------------- watchdog block (issue #66) ---------------------------

def test_watchdog_authority_parses_all_three_tiers(tmp_path):
    for tier in ("diagnose-only", "allowlist", "full"):
        _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {"authority": tier}})
        assert config.load(tmp_path)["watchdog"]["authority"] == tier


def test_watchdog_authority_rejects_unknown_tiers(tmp_path):
    for bad in ("FULL", "", "yolo", None, 1, ["full"]):
        _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {"authority": bad}})
        with pytest.raises(ValueError, match="watchdog.authority"):
            config.load(tmp_path)


def test_watchdog_allowlist_must_be_a_list_of_strings(tmp_path):
    _write_cfg(tmp_path, {"repo": "o/r",
                          "watchdog": {"allowlist": ["superlooper doctor", "relabel"]}})
    assert config.load(tmp_path)["watchdog"]["allowlist"] == ["superlooper doctor", "relabel"]
    for bad in ("relabel", {"a": 1}, [1], [None], 0):
        _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {"allowlist": bad}})
        with pytest.raises(ValueError, match="watchdog.allowlist"):
            config.load(tmp_path)


def test_watchdog_minutes_validation(tmp_path):
    # grace may be 0 (launch on the tripping check); the two detection bounds must be >= 1
    # (a zero bound would trip on any instantaneous glimpse).
    _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {"grace_minutes": 0}})
    assert config.load(tmp_path)["watchdog"]["grace_minutes"] == 0
    for key, bad in (("grace_minutes", -1), ("grace_minutes", True), ("grace_minutes", "30"),
                     ("heartbeat_stale_minutes", 0), ("heartbeat_stale_minutes", 1.5),
                     ("no_progress_minutes", 0), ("no_progress_minutes", False)):
        _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {key: bad}})
        with pytest.raises(ValueError, match=f"watchdog.{key}"):
            config.load(tmp_path)


def test_watchdog_unknown_subkey_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "o/r", "watchdog": {"graice_minutes": 30}})
    with pytest.raises(ValueError, match="watchdog.graice_minutes"):
        config.load(tmp_path)


def test_checks_pending_cap_default_and_validation(tmp_path):
    # issue #26: the bound on how long a finished PR may sit with required checks pending before
    # the runner escalates ONCE to needs-william. Defaults, overrides, and rejects bad types.
    _write_cfg(tmp_path, {"repo": "me/tool"})
    assert config.load(tmp_path)["session"]["checks_pending_cap"] == 10800
    _write_cfg(tmp_path, {"repo": "me/tool", "session": {"checks_pending_cap": 600}})
    assert config.load(tmp_path)["session"]["checks_pending_cap"] == 600
    for bad in (-1, "soon", True, 1.5):
        _write_cfg(tmp_path, {"repo": "me/tool", "session": {"checks_pending_cap": bad}})
        with pytest.raises(ValueError):
            config.load(tmp_path)


def test_agent_default_is_claude_and_codex_is_settable(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b"})
    assert config.load(tmp_path)["agent"] == "claude"
    _write_cfg(tmp_path, {"repo": "a/b", "agent": "codex"})
    assert config.load(tmp_path)["agent"] == "codex"


def test_worker_effort_default_is_null_and_settable(tmp_path):
    # repo-wide worker effort default: null (absent) means NEVER send --effort. The loader default
    # MUST be a genuine null, not an invented value — a filled-in truthy default would win over the
    # runner's no-flag fallback for every repo that omits the field (the stale-fable trap, fa64efb).
    _write_cfg(tmp_path, {"repo": "a/b"})
    assert config.load(tmp_path)["models"]["worker_effort"] is None
    # a repo may set it; the deep-merge keeps the sibling model defaults.
    _write_cfg(tmp_path, {"repo": "a/b", "models": {"worker_effort": "high"}})
    cfg = config.load(tmp_path)
    assert cfg["models"]["worker_effort"] == "high"
    assert cfg["models"]["worker"] == "opus[1m]" and cfg["models"]["answerer"] == "opus[1m]"


def test_deep_merge_keeps_sibling_nested_defaults(tmp_path):
    # a partial nested dict must fill the OTHER sub-keys from the default, not wipe them.
    _write_cfg(tmp_path, {"repo": "me/tool", "session": {"retry_cap": 5}})
    cfg = config.load(tmp_path)
    assert cfg["session"]["retry_cap"] == 5           # overridden
    assert cfg["session"]["idle_seconds"] == 480      # sibling default preserved
    assert cfg["session"]["conflict_cap"] == 2


# --------------------------- validation ---------------------------

def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        config.load(tmp_path)
    assert str(tmp_path / ".superlooper" / "config.json") in str(e.value)


def test_missing_repo_rejected(tmp_path):
    _write_cfg(tmp_path, {"lanes": 2})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "repo" in str(e.value)


def test_bad_repo_format_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "noslash"})
    with pytest.raises(ValueError):
        config.load(tmp_path)


def test_unknown_top_level_key_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "bogus_key": 1})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "bogus_key" in str(e.value)


def test_bad_agent_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "agent": "gptbot"})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "agent" in str(e.value)


def test_unknown_nested_key_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "session": {"idl_seconds": 1}})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "idl_seconds" in str(e.value)


def test_affinity_must_be_hard_or_soft(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "affinity": "medium"})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "affinity" in str(e.value)
    for good in ("hard", "soft"):
        _write_cfg(tmp_path, {"repo": "a/b", "affinity": good})
        assert config.load(tmp_path)["affinity"] == good


def test_unhashable_enum_value_raises_valueerror_not_typeerror(tmp_path):
    # an unhashable value (list/dict) for an enum field must yield the contract's ValueError,
    # never a raw TypeError from `x in set` (cross-review round 2, Task 2).
    for bad in ({"affinity": ["hard"]}, {"merge_method": {"squash": True}}):
        _write_cfg(tmp_path, {"repo": "a/b", **bad})
        with pytest.raises(ValueError):
            config.load(tmp_path)


def test_lanes_must_be_positive_int(tmp_path):
    for bad in (0, -1, "2", 1.5):
        _write_cfg(tmp_path, {"repo": "a/b", "lanes": bad})
        with pytest.raises(ValueError):
            config.load(tmp_path)
    _write_cfg(tmp_path, {"repo": "a/b", "lanes": 1})
    assert config.load(tmp_path)["lanes"] == 1


# --------------------------- reserved investigation lanes (issue #63) ---------------------------
# `lanes` may ALSO be an object splitting capacity into two strict pools:
#   {"build": N, "investigate": M}  — N lanes for merge-producing work, M reserved for investigations.
# A plain integer keeps today's single-shared-pool behaviour exactly (the test above is untouched).

def test_lanes_object_form_parses_and_preserves_shape(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "lanes": {"build": 1, "investigate": 1}})
    assert config.load(tmp_path)["lanes"] == {"build": 1, "investigate": 1}
    # a zero pool is allowed (one side deliberately paused) as long as the total is >= 1
    _write_cfg(tmp_path, {"repo": "a/b", "lanes": {"build": 2, "investigate": 0}})
    assert config.load(tmp_path)["lanes"] == {"build": 2, "investigate": 0}
    _write_cfg(tmp_path, {"repo": "a/b", "lanes": {"build": 0, "investigate": 3}})
    assert config.load(tmp_path)["lanes"] == {"build": 0, "investigate": 3}


def test_lanes_object_requires_both_pools(tmp_path):
    # opting into the object form is a conscious split, so BOTH pool sizes must be stated — a lone
    # {"build": 2} silently zeroing investigations is exactly the surprise this rejects.
    for bad in ({"build": 1}, {"investigate": 1}):
        _write_cfg(tmp_path, {"repo": "a/b", "lanes": bad})
        with pytest.raises(ValueError) as e:
            config.load(tmp_path)
        assert "lanes" in str(e.value)


def test_lanes_object_rejects_unknown_pool_key(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "lanes": {"build": 1, "investigate": 1, "review": 1}})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "review" in str(e.value)


def test_lanes_object_pool_sizes_must_be_nonneg_ints(tmp_path):
    for bad in ({"build": "1", "investigate": 1}, {"build": -1, "investigate": 1},
                {"build": 1.5, "investigate": 1}, {"build": True, "investigate": 1}):
        _write_cfg(tmp_path, {"repo": "a/b", "lanes": bad})
        with pytest.raises(ValueError) as e:
            config.load(tmp_path)
        assert "build" in str(e.value) or "lanes" in str(e.value)


def test_lanes_object_total_must_be_at_least_one(tmp_path):
    # both pools zero == nothing would ever launch: reject loudly rather than deadlock silently.
    for bad in ({"build": 0, "investigate": 0}, {}):
        _write_cfg(tmp_path, {"repo": "a/b", "lanes": bad})
        with pytest.raises(ValueError) as e:
            config.load(tmp_path)
        assert "lanes" in str(e.value)


def test_areas_must_be_dict_of_glob_lists(tmp_path):
    # a value that is a bare string instead of a list of globs is a common mistake -> reject.
    _write_cfg(tmp_path, {"repo": "a/b", "areas": {"frontend": "src/**"}})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "frontend" in str(e.value) or "areas" in str(e.value)
    # a well-formed areas map loads and its globs are usable (compile via fnmatch)
    _write_cfg(tmp_path, {"repo": "a/b", "areas": {"frontend": ["src/**"]}})
    cfg = config.load(tmp_path)
    assert cfg["areas"]["frontend"] == ["src/**"]


def test_version_true_not_accepted_as_1(tmp_path):
    # bool is an int subclass and True == 1 in Python — `version: true` must NOT sneak through.
    _write_cfg(tmp_path, {"repo": "a/b", "version": True})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "version" in str(e.value)


def test_bad_nested_field_types_rejected(tmp_path):
    # unknown-key rejection is not enough; a wrong-TYPED nested value must also be rejected at
    # load, not blow up later in the runner/notify/QA code (cross-review, Task 2).
    bad_cases = [
        {"models": {"worker": False}},          # model name must be a non-empty string
        {"models": {"answerer": ""}},           # empty string not allowed
        {"models": {"worker_effort": ""}},      # null or non-empty string
        {"models": {"worker_effort": 5}},       # null or non-empty string
        {"qa": {"retry_once": "false"}},        # must be a real bool
        {"qa": {"quarantine": [1]}},            # list of strings
        {"qa": {"nightly_cmd": []}},            # null or non-empty string
        {"notify": {"cmd": []}},                # null or non-empty string
        {"notify": {"imessage_to": 5551234}},   # null or non-empty string
        {"codex": {"dangerous_bypass": "yes"}}, # must be a real bool
        {"codex": {"bypass_hook_trust": 1}},     # bool only, not int
        {"codex": {"no_alt_screen": None}},      # bool only
    ]
    for extra in bad_cases:
        _write_cfg(tmp_path, {"repo": "a/b", **extra})
        with pytest.raises(ValueError):
            config.load(tmp_path)


def test_defaults_not_shared_between_loads(tmp_path, tmp_path_factory):
    # the mutable defaults (lists/dicts) must be deep-copied per load, so a caller mutating one
    # loaded config never pollutes the module-level template for the next load (same aliasing
    # class as loopstate.DEFAULT_ISSUE).
    _write_cfg(tmp_path, {"repo": "a/b"})
    cfg1 = config.load(tmp_path)
    cfg1["required_checks"].append("polluted")
    cfg1["areas"]["x"] = ["y/**"]
    cfg1["qa"]["quarantine"].append("polluted")
    other = tmp_path_factory.mktemp("repo2")
    _write_cfg(other, {"repo": "c/d"})
    cfg2 = config.load(other)
    assert cfg2["required_checks"] == [], "required_checks default leaked across loads"
    assert cfg2["areas"] == {}, "areas default leaked across loads"
    assert cfg2["qa"]["quarantine"] == [], "qa.quarantine default leaked across loads"


def test_not_json_rejected_with_path(tmp_path):
    d = tmp_path / ".superlooper"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{not valid json,,,}")
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "config.json" in str(e.value)


# --------------------------- path_to_area ---------------------------

def _cfg(areas):
    return {"repo": "a/b", "areas": areas}


def test_path_to_area_first_match_wins():
    areas = {
        "frontend": ["src/components/**", "src/styles/**"],
        "api": ["src/api/**", "src/server/**"],
        "db": ["migrations/**", "src/db/**"],
    }
    c = _cfg(areas)
    assert config.path_to_area(c, "src/components/Button.tsx") == "frontend"
    assert config.path_to_area(c, "src/api/routes.py") == "api"
    assert config.path_to_area(c, "migrations/0007_add.sql") == "db"


def test_path_to_area_unmatched_is_wildcard():
    c = _cfg({"api": ["src/api/**"]})
    assert config.path_to_area(c, "README.md") == "*"
    assert config.path_to_area(c, "docs/whatever.md") == "*"


def test_path_to_area_no_areas_is_wildcard():
    assert config.path_to_area(_cfg({}), "anything/at/all.py") == "*"


# --------------------------- state_home ---------------------------

def test_state_home_default(monkeypatch):
    monkeypatch.delenv("SL_HOME", raising=False)
    home = config.state_home({"repo": "octocat/Hello-World"})
    assert home == Path("~/.superlooper").expanduser() / "octocat__Hello-World"


def test_state_home_respects_sl_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "slhome"))
    home = config.state_home({"repo": "octocat/Hello-World"})
    assert home == tmp_path / "slhome" / "octocat__Hello-World"


# --------------------------- janitor knobs (issue #62) ---------------------------

def test_janitor_defaults_and_override(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b"})
    assert config.load(tmp_path)["janitor"]["aged_park_days"] == 14
    _write_cfg(tmp_path, {"repo": "a/b", "janitor": {"aged_park_days": 30}})
    assert config.load(tmp_path)["janitor"]["aged_park_days"] == 30


def test_janitor_bad_values_rejected(tmp_path):
    for bad in (True, -1, "14", None, 1.5):
        _write_cfg(tmp_path, {"repo": "a/b", "janitor": {"aged_park_days": bad}})
        with pytest.raises(ValueError) as e:
            config.load(tmp_path)
        assert "aged_park_days" in str(e.value)


def test_janitor_unknown_subkey_rejected(tmp_path):
    _write_cfg(tmp_path, {"repo": "a/b", "janitor": {"aged_prak_days": 30}})
    with pytest.raises(ValueError) as e:
        config.load(tmp_path)
    assert "janitor.aged_prak_days" in str(e.value)
