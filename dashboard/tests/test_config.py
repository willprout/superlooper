"""The dashboard config contract (Task 1 / decisions B.4, B.7).

`config.load(path)` reads the dashboard's own ``config.json`` (which repos to watch, ports and
poll cadences, notify settings, the fun-toggle map), fills defaults, and — loud and specific,
mirroring the skill's ``lib/config.py`` — rejects any unknown key or wrong-typed value by NAME.
For each configured repo it reads that repo's OWN ``.superlooper/config.json`` for its slug and
idle/freeze thresholds (defaulting 480/2700 when absent). No William-specific paths anywhere
(shareability, decision A.3).
"""
import json
from pathlib import Path

import pytest

import config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _REPO_ROOT / "config.example.json"


# --- fixtures / helpers ---------------------------------------------------------------------

_OMIT = object()   # sentinel: distinguish "omit the key" from writing a literal null/false value


def _write_repo(base, name, slug, session=None, lanes=_OMIT):
    """A repo checkout dir carrying a skill-shaped ``.superlooper/config.json`` (only the fields
    the dashboard reads: ``repo`` slug + optional ``session`` thresholds + optional ``lanes``)."""
    repo_dir = base / name
    (repo_dir / ".superlooper").mkdir(parents=True)
    body = {"repo": slug}
    if session is not None:
        body["session"] = session
    if lanes is not _OMIT:
        body["lanes"] = lanes
    (repo_dir / ".superlooper" / "config.json").write_text(json.dumps(body))
    return repo_dir


def _write_config(base, obj):
    p = base / "config.json"
    p.write_text(json.dumps(obj))
    return p


@pytest.fixture
def one_repo(tmp_path):
    """A ready-made adopted repo and a minimal dashboard config pointing at it."""
    repo = _write_repo(tmp_path, "checkout", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    return tmp_path, repo, cfg_path


# --- defaults -------------------------------------------------------------------------------

def test_minimal_config_fills_all_defaults(one_repo):
    _, _, cfg_path = one_repo
    cfg = config.load(cfg_path)
    assert cfg["version"] == 1
    assert cfg["port"] == 8611
    assert cfg["poll_seconds"] == 2
    assert cfg["gh_poll_seconds"] == 30
    # The dashboard's own direct GitHub reads happen only in FALLBACK (issue #146) and ride a
    # deliberately slower clock — a runner-less surface must not spend the shared budget fast.
    assert cfg["fallback_gh_poll_seconds"] == 120
    assert cfg["heartbeat_down_seconds"] == 300
    # How long the runner may go quiet before the dashboard stops calling its view live truth.
    # Well under heartbeat_down_seconds: stop trusting a stale view long before declaring it dead.
    assert cfg["runner_silent_seconds"] == 90
    assert cfg["notify"] == {"imessage_to": None, "cmd": None}


def test_default_fun_map_is_master_plus_every_mvp_mechanic_all_on(one_repo):
    _, _, cfg_path = one_repo
    fun = config.load(cfg_path)["fun"]
    # §7 MVP fun set + the master toggle; joy is on by default (design record §0.1).
    assert fun == {
        "master": True,
        "solari": True,
        "solari_clack": True,
        "airlines": True,
        "living_clock": True,
        "corner_counter": True,
        "incident_sign": True,
    }


def test_explicit_scalars_are_honored(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}],
        "port": 9000, "poll_seconds": 5, "gh_poll_seconds": 60,
        "heartbeat_down_seconds": 120,
    })
    cfg = config.load(cfg_path)
    assert (cfg["port"], cfg["poll_seconds"], cfg["gh_poll_seconds"],
            cfg["heartbeat_down_seconds"]) == (9000, 5, 60, 120)


# --- unknown keys / wrong types (the loud-rejection DoD) ------------------------------------

def test_unknown_top_level_key_names_the_offender(one_repo):
    _, repo, _ = one_repo
    base = repo.parent
    cfg_path = _write_config(base, {"repos": [{"path": str(repo)}], "prot": 8611})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "prot" in str(e.value)


def test_top_level_must_be_object(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError) as e:
        config.load(p)
    assert "object" in str(e.value)


@pytest.mark.parametrize("key,bad", [
    ("port", "8611"),        # string, not int
    ("port", True),          # bool is an int subclass — must still be rejected
    ("port", 0),             # out of range
    ("port", 70000),         # out of range
    ("poll_seconds", 0),     # must be >= 1
    ("poll_seconds", 2.5),   # float, not int
    ("gh_poll_seconds", -1),
    ("heartbeat_down_seconds", "300"),
    ("version", 2),          # unsupported version
])
def test_wrong_typed_or_out_of_range_scalar_is_rejected_by_name(tmp_path, key, bad):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}], key: bad})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert key in str(e.value)


def test_malformed_json_names_the_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    with pytest.raises(ValueError) as e:
        config.load(p)
    assert "config.json" in str(e.value)


def test_missing_config_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / "nope.json")


# --- repos list -----------------------------------------------------------------------------

def test_repos_key_is_required(tmp_path):
    cfg_path = _write_config(tmp_path, {"port": 8611})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "repos" in str(e.value)


def test_repos_must_be_non_empty(tmp_path):
    cfg_path = _write_config(tmp_path, {"repos": []})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "repos" in str(e.value)


def test_repo_entry_must_be_object_with_path(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [str(repo)]})  # bare string, not {path}
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "path" in str(e.value)


def test_unknown_key_in_repo_entry_is_rejected(tmp_path):
    # (`airline` became a real key in Task 7, so the unknown-key example is a plain typo of it.)
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo), "airlnie": "TITAN"}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "airlnie" in str(e.value)


def test_repo_path_missing_is_rejected(tmp_path):
    cfg_path = _write_config(tmp_path, {"repos": [{"path": ""}]})
    with pytest.raises(ValueError):
        config.load(cfg_path)


def test_home_tilde_in_repo_path_is_expanded(tmp_path, monkeypatch):
    # A ~ path must expand to the user's home (no literal '~' leaks into a filesystem path).
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _write_repo(tmp_path, "co", "acme/widget")   # tmp_path is HOME, so repo == ~/co
    cfg_path = _write_config(tmp_path, {"repos": [{"path": "~/co"}]})
    cfg = config.load(cfg_path)
    assert cfg["repos"][0]["path"] == str(repo)
    assert "~" not in cfg["repos"][0]["path"]


# --- per-repo facts: slug + idle/freeze thresholds ------------------------------------------

def test_reads_repo_slug_and_default_thresholds(one_repo):
    _, _, cfg_path = one_repo
    entry = config.load(cfg_path)["repos"][0]
    assert entry["slug"] == "acme/widget"
    assert entry["owner"] == "acme"
    assert entry["name"] == "widget"
    assert entry["idle_seconds"] == 480     # default when the repo omits session
    assert entry["freeze_seconds"] == 2700


def test_reads_repo_overridden_thresholds(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget",
                       session={"idle_seconds": 90, "freeze_seconds": 600})
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    entry = config.load(cfg_path)["repos"][0]
    assert entry["idle_seconds"] == 90
    assert entry["freeze_seconds"] == 600


# --- the repo's configured lane count (issue #35: the empty-queue caption must reflect it) ---

def test_reads_repo_configured_lane_count(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget", lanes=3)
    entry = config.load(_write_config(tmp_path, {"repos": [{"path": str(repo)}]}))["repos"][0]
    assert entry["lanes"] == 3


def test_absent_lane_count_reads_as_unknown_not_an_invented_default(one_repo):
    # The repo omits `lanes` (e.g. an older adopted config) → the dashboard can't know the count, so
    # it records None (unknown) rather than inventing a number. Downstream the empty-queue caption
    # then drops the runway clause entirely — the honest fallback (issue #35).
    _, _, cfg_path = one_repo
    entry = config.load(cfg_path)["repos"][0]
    assert entry["lanes"] is None


@pytest.mark.parametrize("bad", ["two", 0, -1, True, 2.5])
def test_unreadable_lane_count_falls_back_to_unknown_not_a_number(tmp_path, bad):
    # Unlike the liveness thresholds this loader CONSUMES (wrong ones silently mis-tier a session, so
    # they fail loud), `lanes` drives only a cosmetic truth-caption and has no numeric default to
    # coerce to. An unreadable value therefore records None (unknown) so the caption shows no number —
    # the honest fallback the owner mandated (issue #35). This is self-revealing, not hiding: the
    # missing count on screen IS the signal, where a wrong number would be the thing that misleads.
    repo = _write_repo(tmp_path, "co", "acme/widget", lanes=bad)
    entry = config.load(_write_config(tmp_path, {"repos": [{"path": str(repo)}]}))["repos"][0]
    assert entry["lanes"] is None


def test_repo_without_superlooper_config_is_rejected_by_path(tmp_path):
    unadopted = tmp_path / "plain"
    unadopted.mkdir()
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(unadopted)}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert str(unadopted) in str(e.value)


def test_zero_threshold_is_accepted(tmp_path):
    # 0 is a valid (if odd) threshold — mirror the skill's `>= 0` contract exactly.
    repo = _write_repo(tmp_path, "co", "acme/widget", session={"idle_seconds": 0})
    entry = config.load(_write_config(tmp_path, {"repos": [{"path": str(repo)}]}))["repos"][0]
    assert entry["idle_seconds"] == 0
    assert entry["freeze_seconds"] == 2700   # partial session: the absent key still defaults


@pytest.mark.parametrize("session,offender", [
    ({"idle_seconds": "90"}, "idle_seconds"),      # string, not int
    ({"idle_seconds": True}, "idle_seconds"),      # bool-as-int
    ({"freeze_seconds": -5}, "freeze_seconds"),    # negative
    ({"freeze_seconds": 2.5}, "freeze_seconds"),   # float
])
def test_present_but_malformed_repo_threshold_is_rejected_loud(tmp_path, session, offender):
    # A threshold this loader CONSUMES must fail loud when present-and-wrong — not silently
    # coerce to the default (that would hide a misconfiguration behind wrong liveness tiers).
    repo = _write_repo(tmp_path, "co", "acme/widget", session=session)
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert offender in str(e.value)
    assert str(repo) in str(e.value)   # and it names the offending repo


def test_wrong_shaped_repo_session_is_rejected(tmp_path):
    repo = tmp_path / "co"
    (repo / ".superlooper").mkdir(parents=True)
    (repo / ".superlooper" / "config.json").write_text(
        json.dumps({"repo": "acme/widget", "session": "nope"}))
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "session" in str(e.value)


def test_typoed_repos_key_names_the_typo_not_missing_repos(tmp_path):
    # A misspelled top-level key must name the offender (`reops`), not merely report `repos`
    # missing — otherwise a typo sends the user hunting for the wrong problem.
    cfg_path = _write_config(tmp_path, {"reops": [{"path": "x"}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "reops" in str(e.value)


def test_repo_config_without_slug_is_rejected(tmp_path):
    repo = tmp_path / "co"
    (repo / ".superlooper").mkdir(parents=True)
    (repo / ".superlooper" / "config.json").write_text(json.dumps({"lanes": 2}))  # no 'repo'
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert str(repo) in str(e.value) or "repo" in str(e.value)


def test_two_repos_each_get_their_own_facts(tmp_path):
    a = _write_repo(tmp_path, "a", "acme/widget", session={"idle_seconds": 100})
    b = _write_repo(tmp_path, "b", "acme/gadget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(a)}, {"path": str(b)}]})
    repos = config.load(cfg_path)["repos"]
    assert [r["slug"] for r in repos] == ["acme/widget", "acme/gadget"]
    assert repos[0]["idle_seconds"] == 100
    assert repos[1]["idle_seconds"] == 480


# --- notify block ---------------------------------------------------------------------------

def test_notify_values_are_honored(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}],
        "notify": {"imessage_to": "+15550001111", "cmd": "notify.sh {title} {body}"},
    })
    cfg = config.load(cfg_path)
    assert cfg["notify"]["imessage_to"] == "+15550001111"
    assert cfg["notify"]["cmd"] == "notify.sh {title} {body}"


def test_unknown_notify_key_is_rejected(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}], "notify": {"sms_to": "x"},
    })
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "sms_to" in str(e.value)


def test_notify_wrong_typed_value_is_rejected(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}], "notify": {"cmd": 123},
    })
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "cmd" in str(e.value)


# --- fun toggle map -------------------------------------------------------------------------

def test_fun_toggles_are_honored(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}],
        "fun": {"solari_clack": False, "corner_counter": False},
    })
    fun = config.load(cfg_path)["fun"]
    assert fun["solari_clack"] is False
    assert fun["corner_counter"] is False
    assert fun["master"] is True          # untouched keys keep their default


def test_unknown_fun_key_is_rejected(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}], "fun": {"salari": False},  # typo
    })
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "salari" in str(e.value)


def test_non_boolean_fun_toggle_is_rejected(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}], "fun": {"master": "yes"},
    })
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "master" in str(e.value)


def test_fun_enabled_gates_every_mechanic_on_master(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}],
        "fun": {"master": False, "solari": True},
    })
    cfg = config.load(cfg_path)
    # master off ⇒ nothing is enabled, even a mechanic whose own toggle is True.
    assert config.fun_enabled(cfg, "solari") is False
    assert config.fun_enabled(cfg, "airlines") is False


def test_fun_enabled_true_only_when_master_and_mechanic_on(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {
        "repos": [{"path": str(repo)}],
        "fun": {"solari_clack": False},
    })
    cfg = config.load(cfg_path)
    assert config.fun_enabled(cfg, "solari") is True
    assert config.fun_enabled(cfg, "solari_clack") is False


def test_fun_enabled_rejects_unknown_mechanic(one_repo):
    _, _, cfg_path = one_repo
    cfg = config.load(cfg_path)
    with pytest.raises(ValueError):
        config.fun_enabled(cfg, "nope")


# --- superlooper CLI path (issue #41: the Tidy button's local command) ----------------------

def test_superlooper_cli_defaults_to_the_installed_skill_path(tmp_path, monkeypatch):
    # Shareable default (decision A.3): the skill's own bin, ~-relative so it works for any user —
    # expanded to an absolute path so no literal '~' leaks into a command invocation.
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg = config.load(_write_config(tmp_path, {"repos": [{"path": str(repo)}]}))
    assert cfg["superlooper_cli"] == str(tmp_path / ".claude/skills/superlooper/bin/superlooper")
    assert "~" not in cfg["superlooper_cli"]


def test_superlooper_cli_override_is_honored_and_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg = config.load(_write_config(tmp_path, {
        "repos": [{"path": str(repo)}], "superlooper_cli": "~/bin/sl"}))
    assert cfg["superlooper_cli"] == str(tmp_path / "bin/sl")


@pytest.mark.parametrize("bad", [123, "", "   ", None, True])
def test_superlooper_cli_wrong_typed_or_empty_is_rejected_by_name(tmp_path, bad):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}], "superlooper_cli": bad})
    with pytest.raises(ValueError) as e:
        config.load(cfg_path)
    assert "superlooper_cli" in str(e.value)


# --- state-home derivation ------------------------------------------------------------------

def test_state_home_default_base(monkeypatch):
    monkeypatch.delenv("SL_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/pat")
    assert str(config.state_home("acme/widget")) == "/home/pat/.superlooper/acme__widget"


def test_state_home_honors_sl_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SL_HOME", str(tmp_path))
    assert config.state_home("acme/widget") == tmp_path / "acme__widget"


def test_loaded_repo_entry_carries_derived_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path / "state"))
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}]})
    entry = config.load(cfg_path)["repos"][0]
    assert entry["state_home"] == tmp_path / "state" / "acme__widget"


# --- the committed example template ---------------------------------------------------------

def test_config_example_exists_and_is_the_readme_pointer():
    # README ▸ Install & run does `cp config.example.json config.json`; that pointer must resolve.
    assert _EXAMPLE.exists(), "config.example.json (the README's install pointer) is missing"


def test_config_example_loads_through_the_real_validator(tmp_path):
    # The example's repo paths are placeholders (a user edits them), so swap in one real fixture
    # repo and run the ACTUAL loader — proving every OTHER field in the committed example (keys,
    # types, port, cadences, notify, fun map) passes validation. A typo in the example fails here.
    example = json.loads(_EXAMPLE.read_text())
    repo = _write_repo(tmp_path, "co", "acme/widget")
    example["repos"] = [{"path": str(repo)}]
    cfg_path = _write_config(tmp_path, example)
    cfg = config.load(cfg_path)              # raises if the example is malformed
    # And the example shows the documented defaults, so it round-trips to them.
    assert cfg["port"] == 8611
    assert cfg["fun"]["master"] is True
    assert cfg["notify"] == {"imessage_to": None, "cmd": None}


# --- airline identity (Task 7 / design record §7: auto-generated default, renameable) --------

def test_repo_airline_defaults_to_prettified_name(one_repo):
    _, _, cfg_path = one_repo
    assert config.load(cfg_path)["repos"][0]["airline"] == "Widget"


def test_default_airline_prettifies_hyphens_and_underscores():
    assert config.default_airline("command-center") == "Command Center"
    assert config.default_airline("my_repo") == "My Repo"


def test_repo_airline_override_is_honored(tmp_path):
    # §7: renameable — the airline name is per-user taste, entered through THIS config.
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo), "airline": "Titan Air"}]})
    assert config.load(cfg_path)["repos"][0]["airline"] == "Titan Air"


def test_repo_airline_must_be_a_non_empty_string(tmp_path):
    repo = _write_repo(tmp_path, "co", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo), "airline": "  "}]})
    with pytest.raises(ValueError, match="airline.*non-empty"):
        config.load(cfg_path)


# --------------------------- operator display name (issue #58) ---------------------------

def test_operator_defaults_to_first_repo_owner(one_repo):
    # No operator field -> the owner of the first watched repo (acme/widget -> "acme"), so the
    # command-center audit trail signs the operator's own name, never a hardcoded "William".
    _, _, cfg_path = one_repo
    cfg = config.load(cfg_path)
    assert cfg["operator"] == "acme"
    assert config.operator(cfg) == "acme"


def test_operator_explicit_value_wins(tmp_path):
    repo = _write_repo(tmp_path, "checkout", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}], "operator": "Dana"})
    cfg = config.load(cfg_path)
    assert cfg["operator"] == "Dana"
    assert config.operator(cfg) == "Dana"


def test_operator_null_falls_back_to_first_repo_owner(tmp_path):
    repo = _write_repo(tmp_path, "checkout", "acme/widget")
    cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}], "operator": None})
    assert config.load(cfg_path)["operator"] == "acme"


def test_operator_empty_or_wrong_type_rejected(tmp_path):
    repo = _write_repo(tmp_path, "checkout", "acme/widget")
    for bad in ("", "  ", 7, ["x"]):
        cfg_path = _write_config(tmp_path, {"repos": [{"path": str(repo)}], "operator": bad})
        with pytest.raises(ValueError) as e:
            config.load(cfg_path)
        assert "operator" in str(e.value)


def test_operator_resolver_is_defensive():
    assert config.operator({"operator": "Dana"}) == "Dana"
    assert config.operator({}) == "the owner"
    assert config.operator({"operator": "  "}) == "the owner"
    assert config.operator(None) == "the owner"
