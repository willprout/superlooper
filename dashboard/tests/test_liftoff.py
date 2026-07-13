"""Issue #45 — the pure decision core behind the ONE command (``bin/liftoff``).

``liftoff`` starts (or verifies already-running) BOTH the dashboard and one watched repo's runner.
These tests pin the decisions that make that idempotent and boundary-clean:

  * the runner start rides the config contract (``superlooper_cli`` + ``superlooper run``), NEVER a
    hardcoded engine path — the engine stays dashboard-agnostic;
  * a second invocation double-starts NEITHER — an up dashboard and a live runner each resolve to
    "leave it";
  * the target runner is resolved explicitly (never guessed when several repos are watched).

All pure: injected probe results in, argvs / plan out. The real socket/kill/Popen/execv live in the
bin and are exercised by test_liftoff_bin.py.
"""
import os

import liftoff


def _config(*repos, cli="/opt/skills/superlooper/bin/superlooper"):
    return {"port": 8611, "superlooper_cli": cli, "repos": list(repos)}


def _repo(slug, path, name=None):
    owner, nm = slug.split("/", 1)
    return {"slug": slug, "owner": owner, "name": name or nm, "path": path}


# --------------------------- resolve_repo ---------------------------

def test_single_watched_repo_needs_no_repo_arg():
    r = _repo("will-titan/sandbox", "/checkouts/sandbox")
    assert liftoff.resolve_repo(_config(r), None) is r


def test_several_watched_repos_require_an_explicit_choice():
    a = _repo("o/a", "/co/a"); b = _repo("o/b", "/co/b")
    try:
        liftoff.resolve_repo(_config(a, b), None)
        assert False, "ambiguous target must raise, never guess a runner to start"
    except ValueError as e:
        assert "o/a" in str(e) and "o/b" in str(e)   # the error names the choices


def test_repo_arg_matches_by_slug_name_or_path():
    a = _repo("will-titan/sandbox", "/checkouts/sandbox", name="sandbox")
    b = _repo("o/other", "/checkouts/other")
    cfg = _config(a, b)
    assert liftoff.resolve_repo(cfg, "will-titan/sandbox") is a   # slug
    assert liftoff.resolve_repo(cfg, "sandbox") is a              # bare name
    assert liftoff.resolve_repo(cfg, "/checkouts/sandbox") is a   # checkout path


def test_repo_arg_matching_nothing_raises_naming_the_watched():
    a = _repo("o/a", "/co/a")
    try:
        liftoff.resolve_repo(_config(a), "o/nope")
        assert False
    except ValueError as e:
        assert "o/nope" in str(e) and "o/a" in str(e)


# --------------------------- the config-contract coupling ---------------------------

def test_runner_argv_shells_the_configured_cli_never_a_hardcoded_path():
    # The whole engine-agnostic boundary in one assertion: liftoff shells the CONFIGURED superlooper
    # CLI with the engine's own documented `run --repo`, so the engine stays a black box.
    argv = liftoff.runner_argv("/opt/skills/superlooper/bin/superlooper", "/checkouts/sandbox")
    assert argv == ["/opt/skills/superlooper/bin/superlooper", "run", "--repo", "/checkouts/sandbox"]


def test_dashboard_argv_uses_the_same_interpreter():
    argv = liftoff.dashboard_argv("/usr/bin/python3", "/app/bin/command-center", "cfg.json")
    assert argv == ["/usr/bin/python3", "/app/bin/command-center", "cfg.json"]


# --------------------------- runner_lock_pid (read-only) ---------------------------

def test_runner_lock_pid_reads_the_pidfile(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    (state / "runner.lock").write_text("4321")
    assert liftoff.runner_lock_pid(tmp_path) == 4321


def test_runner_lock_pid_absent_or_garbage_is_none(tmp_path):
    assert liftoff.runner_lock_pid(tmp_path) is None            # no file
    state = tmp_path / "state"; state.mkdir()
    (state / "runner.lock").write_text("not-a-pid")
    assert liftoff.runner_lock_pid(tmp_path) is None            # unparseable


# --------------------------- make_plan: idempotency core ---------------------------

_R = _repo("o/a", "/co/a")
_DASH = ["python3", "/app/bin/command-center", "cfg.json"]
_RUN = ["/cli/superlooper", "run", "--repo", "/co/a"]
_URL = "http://127.0.0.1:8611"


def _plan(dashboard_up, runner_pid):
    return liftoff.make_plan(_R, _URL, _DASH, _RUN,
                             dashboard_up=dashboard_up, runner_pid=runner_pid)


def test_both_down_starts_both_runner_in_foreground():
    p = _plan(dashboard_up=False, runner_pid=None)
    assert p["dashboard"]["start"] is True and p["dashboard"]["argv"] == _DASH
    assert p["dashboard"]["foreground"] is False           # the dashboard is a background server
    assert p["runner"]["start"] is True and p["runner"]["argv"] == _RUN
    assert p["runner"]["foreground"] is True               # the runner takes over this cmux tab


def test_dashboard_up_is_not_restarted():
    p = _plan(dashboard_up=True, runner_pid=None)
    assert p["dashboard"]["start"] is False
    assert "leaving it" in p["dashboard"]["message"] and _URL in p["dashboard"]["message"]
    assert p["runner"]["start"] is True                    # the runner half is independent


def test_live_runner_is_not_restarted():
    p = _plan(dashboard_up=False, runner_pid=999)
    assert p["runner"]["start"] is False and p["runner"]["pid"] == 999
    assert "leaving it" in p["runner"]["message"] and "999" in p["runner"]["message"]
    assert p["dashboard"]["start"] is True                 # the dashboard half is independent


def test_both_up_starts_neither():
    p = _plan(dashboard_up=True, runner_pid=999)
    assert p["dashboard"]["start"] is False and p["runner"]["start"] is False


# --------------------------- missing_config_message (issue #104) ---------------------------
# The first real run of liftoff failed with a message that named neither WHERE it looked (a bare
# relative "config.json") nor a way out (it advised "copy config.example.json" while the config sat,
# already written, one directory over). These pin the honest replacement.

def test_missing_config_message_names_the_absolute_path_and_all_three_ways():
    msg = liftoff.missing_config_message("/home/op/proj/config.json")
    assert "/home/op/proj/config.json" in msg               # names WHERE it actually looked
    # all three ways out: run from the config's directory, pass it as an argument, set CC_CONFIG
    assert "directory" in msg
    assert "argument" in msg
    assert "CC_CONFIG" in msg
    assert msg.startswith("liftoff:") and msg.endswith("\n")  # liftoff's plain, newline-terminated voice


def test_missing_config_message_names_a_config_found_beside_the_script_and_omits_copy_advice():
    # The live #104 case: liftoff run from the repo root while the config sat in the dashboard dir.
    # The message must NAME that found config and how to select it — and, because a config already
    # EXISTS, must NOT advise copying the example (even when an example path is also supplied).
    msg = liftoff.missing_config_message(
        "/home/op/proj/config.json",
        script_dir_config="/home/op/proj/dashboard/config.json",
        example_config="/home/op/proj/dashboard/config.example.json")
    assert "/home/op/proj/config.json" in msg                       # where it looked
    assert "/home/op/proj/dashboard/config.json" in msg             # the config that DOES exist
    assert "CC_CONFIG" in msg and "argument" in msg                 # the three ways still listed
    assert "config.example.json" not in msg                         # NO copy-the-example advice
    assert "copy" not in msg.lower() and "cp " not in msg


def test_missing_config_message_advises_copying_the_example_when_none_exists():
    # No config anywhere obvious → the genuine fresh-install case: spell the exact `cp` first step.
    msg = liftoff.missing_config_message(
        "/home/op/proj/config.json",
        example_config="/home/op/proj/dashboard/config.example.json")
    assert "/home/op/proj/config.json" in msg
    assert "/home/op/proj/dashboard/config.example.json" in msg     # the example to copy
    assert "cp " in msg                                             # the concrete copy command
    assert "/home/op/proj/dashboard/config.json" in msg            # copies TO the sibling config
    assert "CC_CONFIG" in msg                                       # the three ways still listed
