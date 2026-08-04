"""The runner's own process home (issue #306).

Ruled 2026-07-31 (docs/HERDR-ADOPTION-PLAN.md §8.1): the runner lives OUTSIDE the session host —
a plain ``gui/$UID`` login-item process talking to the host's server. The supervisor never lives
inside what it supervises.

These tests pin the two homes and the machinery each one needs, because the whole hazard here is a
rule that was TRUE under the old host being carried forward silently:

* ``pane`` — today's home. The runner lives in a visible multiplexer tab whose pane is the launch
  anchor. A detached/launchd runner loses that pane and every worker launch dies (finding D7,
  issue #33) — so under this home the launchd machinery below must stay refused.
* ``login-item`` — the new home. There is no anchor to lose, so the prohibition dissolves; what
  replaces it is a different set of things that can silently be wrong (a Background-domain job that
  cannot read the keychain, launchd's four-entry PATH, a host server that is not answering).

Everything that decides between the two is pure and lives in ``runner_home``; the tests here are
the record of WHY each rule exists, so a later reader can tell doctrine from paranoia.
"""
import plistlib

import pytest

import runner_home


# --------------------------------------------------------------- which home is this?

def test_the_default_home_is_the_pane_home():
    # A repo that says nothing keeps TODAY's behaviour. The cross-machine parallel run (plan §6)
    # depends on this: production keeps running in a visible tab while the new home is proven
    # elsewhere, and a config that predates this issue must not silently change hosts.
    assert runner_home.kind({}) == runner_home.PANE
    assert runner_home.kind({"runner_home": "pane"}) == runner_home.PANE


def test_the_login_item_home_is_opt_in_by_config():
    assert runner_home.kind({"runner_home": "login-item"}) == runner_home.LOGIN_ITEM


@pytest.mark.parametrize("cfg", [None, "pane", 7, {"runner_home": None},
                                 {"runner_home": "daemon"}, {"runner_home": 3}])
def test_an_unreadable_home_falls_back_to_the_pane_home(cfg):
    # Fail-CLOSED in the direction that cannot lose a launch: the pane home's preflight refuses to
    # start without a resolvable pane, so a garbled config gets a loud refusal rather than a runner
    # that quietly believes it needs no anchor and then births every worker into nothing.
    assert runner_home.kind(cfg) == runner_home.PANE


# --------------------------------------------------------------- the #116 restart, disposed

def test_the_pane_home_keeps_the_in_place_re_exec():
    # Under the pane home ``os.execv`` is load-bearing and stays: it PRESERVES the pid, so the
    # runner remains the foreground process of its own tab. A new pid would orphan from that tab
    # and the shell would fall back to its prompt.
    assert runner_home.restart_mechanism(runner_home.PANE) == runner_home.REEXEC


def test_the_login_item_home_retires_the_re_exec_and_exits_to_its_supervisor():
    # The whole reason for the in-place exec was the tab. With no tab, the honest restart is to
    # exit cleanly and let the supervisor start a fresh process — which reloads the installed
    # engine and clears in-memory episode state exactly as the exec did, without pretending a
    # process image swap is free.
    assert runner_home.restart_mechanism(runner_home.LOGIN_ITEM) == runner_home.EXIT_TO_SUPERVISOR


def test_every_home_has_a_restart_mechanism():
    for home in runner_home.KINDS:
        assert runner_home.restart_mechanism(home) in (runner_home.REEXEC,
                                                       runner_home.EXIT_TO_SUPERVISOR)


# --------------------------------------------------------------- addressing the job

def test_the_label_follows_the_shipped_launchd_naming():
    # Same shape as the nightly/watchdog labels already shipped (com.superlooper.<job>.<owner>__<name>)
    # so one glance at `launchctl list` groups a repo's jobs together.
    assert runner_home.label("willprout/superlooper") == "com.superlooper.runner.willprout__superlooper"


@pytest.mark.parametrize("slug", ["", None, "nosuchslug", "a/b/c"])
def test_a_label_cannot_be_derived_from_a_bad_slug(slug):
    with pytest.raises(ValueError):
        runner_home.label(slug)


def test_every_service_target_is_the_gui_domain():
    # THE keychain rule (spike 3, docs/SPIKES-2026-07-29-results.md): a `gui/$UID` LaunchAgent is
    # in the Aqua session and reads the login keychain; a `system/` daemon is a different session
    # and was never tested. The domain is not a parameter anywhere here — it is spelled once.
    assert runner_home.domain(501) == "gui/501"
    assert runner_home.service_target(501, "com.x") == "gui/501/com.x"


def test_the_launchctl_argvs_name_the_gui_service_and_nothing_else():
    lc = "/bin/launchctl"
    assert runner_home.bootstrap_argv(lc, 501, "/p/x.plist") == [lc, "bootstrap", "gui/501",
                                                                 "/p/x.plist"]
    assert runner_home.bootout_argv(lc, 501, "com.x") == [lc, "bootout", "gui/501/com.x"]
    assert runner_home.print_argv(lc, 501, "com.x") == [lc, "print", "gui/501/com.x"]
    # -k restarts a job that IS running; without it a wedged runner would be left in place.
    assert runner_home.kickstart_argv(lc, 501, "com.x") == [lc, "kickstart", "-k", "gui/501/com.x"]


def test_no_launchctl_argv_can_be_pointed_at_the_system_domain():
    # A regression fence, not a style rule: the ONE way this machinery could break the keychain
    # rule is a caller talking it into another domain, so no function here takes a domain.
    import inspect
    for name in ("bootstrap_argv", "bootout_argv", "kickstart_argv", "print_argv", "domain",
                 "service_target"):
        params = inspect.signature(getattr(runner_home, name)).parameters
        assert "domain" not in params, "%s takes a domain — the gui/$UID rule became optional" % name


# --------------------------------------------------------------- reading the job back

def test_the_service_pid_is_read_out_of_launchctl_print():
    out = """com.superlooper.runner.o__r = {
\tactive count = 1
\tpath = /Users/w/Library/LaunchAgents/com.superlooper.runner.o__r.plist
\tstate = running
\tpid = 41231
\tprogram = /Users/w/.claude/skills/superlooper/bin/superlooper
}"""
    assert runner_home.service_pid(out) == 41231


@pytest.mark.parametrize("out", [
    "",
    None,
    "Could not find service \"com.superlooper.runner.o__r\" in domain for uid: 501",
    "com.x = {\n\tstate = not running\n}",       # loaded but not up: no pid to read
    "com.x = {\n\tpid = notanumber\n}",
])
def test_an_unreadable_service_pid_is_none_never_a_guess(out):
    # None means UNKNOWN and every caller fails closed on it. A guessed pid here would be a pid
    # something later signals.
    assert runner_home.service_pid(out) is None


# --------------------------------------------------------------- launchd's PATH (the real gotcha)

def test_the_launchd_base_path_is_the_four_entries_launchd_actually_hands_a_job():
    # Measured, not assumed (spike 3): launchd hands a gui/$UID job exactly this PATH. Homebrew
    # and ~/.local/bin are absent, which is why `gh` — and the standalone `claude` — vanish.
    assert runner_home.LAUNCHD_PATH == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_the_rendered_path_prepends_the_dirs_the_required_commands_were_found_in():
    path = runner_home.launch_path(["/opt/homebrew/bin", "/Users/w/.local/bin"])
    assert path.split(":")[:2] == ["/opt/homebrew/bin", "/Users/w/.local/bin"]
    assert path.endswith(runner_home.LAUNCHD_PATH)


def test_the_rendered_path_never_repeats_a_directory():
    path = runner_home.launch_path(["/usr/bin", "/opt/homebrew/bin", "/opt/homebrew/bin"])
    parts = path.split(":")
    assert len(parts) == len(set(parts)), path
    assert "/opt/homebrew/bin" in parts and "/usr/bin" in parts


def test_resolve_commands_reports_what_it_found_and_what_is_missing():
    found, missing = runner_home.resolve_commands(
        {"gh": "/opt/homebrew/bin/gh", "git": "/usr/bin/git"}.get)
    assert found == {"gh": "/opt/homebrew/bin/gh", "git": "/usr/bin/git"}
    assert missing == []


def test_resolve_commands_names_the_missing_ones_in_order():
    found, missing = runner_home.resolve_commands(lambda name: None)
    assert found == {}
    assert missing == list(runner_home.REQUIRED_COMMANDS)


def test_the_required_commands_are_the_ones_the_runner_itself_shells_by_bare_name():
    # Deliberately short. The coding agent has its own pinned ladder (#303) and the session host
    # is resolved behind the wrapper (#304) — neither is named here, so this list stays about the
    # runner's OWN shell-outs.
    assert set(runner_home.REQUIRED_COMMANDS) == {"gh", "git"}


# --------------------------------------------------------------- the boot preflight

def _ok_facts(**over):
    facts = {"manager": "Aqua", "missing": [], "gh_ok": True, "host_answered": True}
    facts.update(over)
    return facts


def test_a_healthy_login_item_context_passes_preflight():
    ok, why = runner_home.preflight(**_ok_facts())
    assert ok is True
    assert "Aqua" in why


@pytest.mark.parametrize("manager", ["Background", "System", "StandardIO", "", None])
def test_preflight_refuses_any_session_that_is_not_aqua(manager):
    # The keychain rule enforced at the moment of the mistake rather than taught in a doc: a
    # Background/system-domain job reads a different keychain session, which is where the
    # "intermittent gh auth-death" reports come from. Unreadable is refused too — this home is
    # launchd-hosted by definition, so an unanswerable `launchctl managername` is a broken home.
    ok, why = runner_home.preflight(**_ok_facts(manager=manager))
    assert ok is False
    assert "Aqua" in why and "gui/" in why


def test_preflight_refuses_a_path_that_lost_the_commands_the_runner_shells():
    ok, why = runner_home.preflight(**_ok_facts(missing=["gh"]))
    assert ok is False
    assert "gh" in why and "PATH" in why


def test_preflight_refuses_when_the_keychain_backed_gh_token_cannot_be_read():
    ok, why = runner_home.preflight(**_ok_facts(gh_ok=False))
    assert ok is False
    assert "gh" in why.lower()


def test_preflight_refuses_when_gh_could_not_be_asked_at_all():
    # None is UNKNOWN, and UNKNOWN is not a pass: a runner whose GitHub identity cannot be proven
    # burns every issue's retry cap confidently.
    ok, why = runner_home.preflight(**_ok_facts(gh_ok=None))
    assert ok is False


@pytest.mark.parametrize("answered", [False, None])
def test_preflight_refuses_when_the_session_host_did_not_answer(answered):
    # The port of the pane home's D7 rule: fail HARD before the loop when the launch channel is
    # unreachable. A quiet warning here once let a mis-started runner abort every launch and burn
    # every issue's retry cap.
    ok, why = runner_home.preflight(**_ok_facts(host_answered=answered))
    assert ok is False
    assert "host" in why.lower()


def test_preflight_reports_every_problem_at_once():
    # A boot refusal the operator reads ONCE should name everything wrong, not make them fix one
    # thing per restart.
    ok, why = runner_home.preflight(manager="Background", missing=["gh"], gh_ok=False,
                                    host_answered=False)
    assert ok is False
    for expected in ("Aqua", "gh", "PATH", "host"):
        assert expected in why or expected in why.lower()


# --------------------------------------------------------------- the plist itself

def _render(**over):
    fields = {"label": "com.superlooper.runner.o__r",
              "superlooper_bin": "/Users/w/.claude/skills/superlooper/bin/superlooper",
              "repo_path": "/Users/w/projects/r",
              "state_home": "/Users/w/.superlooper/o__r",
              "path": "/opt/homebrew/bin:" + runner_home.LAUNCHD_PATH}
    fields.update(over)
    return plistlib.loads(runner_home.render_plist(**fields).encode())


def test_the_rendered_job_starts_at_login_and_is_kept_alive():
    d = _render()
    # RunAtLoad is the "login item" half — the job starts when the owner's GUI session comes up.
    # KeepAlive is what makes an exiting runner restart, which is the login-item home's whole
    # answer to the #116 Restart button.
    assert d["RunAtLoad"] is True
    assert d["KeepAlive"] is True
    assert d["ThrottleInterval"] >= 10, "a crash-looping runner must be throttled, not spun"


def test_the_rendered_job_runs_the_runner_against_the_named_repo():
    d = _render()
    args = [str(a) for a in d["ProgramArguments"]]
    assert args[0].endswith("superlooper")
    assert args[1] == "run"
    assert "/Users/w/projects/r" in args


def test_the_rendered_job_carries_an_explicit_path():
    # The spike-proven gotcha. Without this the job gets launchd's four entries, `gh` is not among
    # them, and every GitHub read fails on a runner that looks perfectly alive.
    d = _render()
    assert d["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")


def test_the_rendered_job_logs_where_the_other_jobs_log():
    d = _render()
    assert d["StandardOutPath"] == "/Users/w/.superlooper/o__r/logs/runner.log"
    assert d["StandardErrorPath"] == d["StandardOutPath"]


def test_the_template_leaves_no_placeholder_behind():
    assert "{" not in runner_home.render_plist(
        label="l", superlooper_bin="/b", repo_path="/r", state_home="/s", path="/usr/bin")


def test_rendering_refuses_a_relative_path_launchd_could_not_resolve():
    # launchd runs the job from `/`, so a relative program or repo path resolves to nothing —
    # and the failure surfaces as a runner that starts and immediately dies in a log nobody reads.
    with pytest.raises(ValueError):
        runner_home.render_plist(label="l", superlooper_bin="bin/superlooper", repo_path="/r",
                                 state_home="/s", path="/usr/bin")
    with pytest.raises(ValueError):
        runner_home.render_plist(label="l", superlooper_bin="/b", repo_path="../r",
                                 state_home="/s", path="/usr/bin")


# --------------------------------------------------------------- the config contract

def test_the_config_loader_defaults_the_home_to_pane(tmp_path):
    import config as config_lib
    (tmp_path / ".superlooper").mkdir()
    (tmp_path / ".superlooper" / "config.json").write_text('{"repo": "o/r"}')
    cfg = config_lib.load(tmp_path)
    assert cfg["runner_home"] == runner_home.PANE
    assert runner_home.kind(cfg) == runner_home.PANE


def test_the_config_loader_accepts_the_login_item_home(tmp_path):
    import config as config_lib
    (tmp_path / ".superlooper").mkdir()
    (tmp_path / ".superlooper" / "config.json").write_text(
        '{"repo": "o/r", "runner_home": "login-item"}')
    assert runner_home.kind(config_lib.load(tmp_path)) == runner_home.LOGIN_ITEM


def test_a_typo_in_the_home_is_a_loud_config_error_not_a_silent_fallback(tmp_path):
    # kind() is fail-closed for RUNTIME readers handed a half-read config; the LOADER is not, because
    # a config the owner wrote with `runner_home: "launchd"` in it should name the typo at adopt
    # time. Silently running the other home is the misconfiguration this loader exists to refuse.
    import config as config_lib
    (tmp_path / ".superlooper").mkdir()
    (tmp_path / ".superlooper" / "config.json").write_text(
        '{"repo": "o/r", "runner_home": "launchd"}')
    with pytest.raises(ValueError) as e:
        config_lib.load(tmp_path)
    assert "runner_home" in str(e.value) and "login-item" in str(e.value)


# --------------------------------------------------------------- the live runner in each home
#
# These use the same rig shape as tests/test_runner.py (tmp state home, injected run_script, no
# real host binaries anywhere). What they pin is narrow and load-bearing: the pane home must be
# byte-for-byte what it was — production runs on it during the whole parallel-run period — and the
# login-item home must not do any of the three anchor-shaped things that only make sense in a tab.

import json
import os

import runner as runner_mod


def _config(**over):
    cfg = {"repo": "o/r", "dev_branch": "main", "prod_branch": None, "lanes": 2,
           "affinity": "hard", "areas": {}, "touches_required": False,
           "required_checks": ["ci"], "merge_method": "squash", "ship_cmd": None,
           "ship_recheck_cmd": None, "report_required_sections": ["Tests"], "bright_lines": [],
           "cleanup_merged_worktrees": True, "report_time": "08:45",
           "models": {"worker": "opus", "debugger": "fable"},
           "session": {"idle_seconds": 480, "freeze_seconds": 2700, "retry_cap": 2,
                       "conflict_cap": 2},
           "qa": {"nightly_cmd": None, "results_glob": None, "retry_once": True,
                  "quarantine": [], "nightly_time": "02:00"},
           "notify": {"imessage_to": None, "cmd": None, "quiet_hours": None},
           "codex": {"dangerous_bypass": False, "bypass_hook_trust": True, "no_alt_screen": True}}
    cfg.update(over)
    return cfg


def _runner(tmp_path, home_kind):
    r = runner_mod.Runner(repo=str(tmp_path / "repo"), config=_config(runner_home=home_kind),
                          state_home=str(tmp_path / "home"), pane="pane-1",
                          run_script=lambda *a, **k: 0,
                          fetch_usage=lambda: {"auth_status": "ok", "five_hour_pct": 1.0,
                                               "seven_day_pct": 1.0})
    r._anchor_status = lambda: {"ok": True, "reason": ""}
    r.tick = lambda now=None: None
    return r


def test_the_runner_reads_its_home_from_the_repo_config(tmp_path):
    (tmp_path / "repo").mkdir()
    assert _runner(tmp_path, "pane").runner_home == runner_home.PANE
    assert _runner(tmp_path, "login-item").runner_home == runner_home.LOGIN_ITEM


def test_a_pane_runner_still_re_execs_in_place_on_a_restart_request(tmp_path):
    # Production regression guard. The whole parallel-run plan rests on this home being untouched.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "pane")
    seen = []
    r._reexec = lambda argv: seen.append(argv)
    runner_mod.write_restart_request(r.state, {"target_pid": os.getpid()})
    r.run(max_ticks=1, sleep=lambda s: None)
    assert seen, "the pane home stopped re-exec'ing — issue #116's mechanism was lost"


def test_a_login_item_runner_exits_for_its_supervisor_instead_of_re_execing(tmp_path):
    # The #116 disposition in code. os.execv existed to PRESERVE the pid so the runner stayed its
    # tab's foreground process. With no tab there is nothing to stay in front of, and launchd's
    # KeepAlive brings back a fresh process image — the same engine reload and the same cleared
    # episode state, without the singleton-adoption dance the exec needed to survive its own pid.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    r._reexec = lambda argv: pytest.fail("a login-item runner must never re-exec in place")
    runner_mod.write_restart_request(r.state, {"target_pid": os.getpid()})
    assert r.run(max_ticks=5, sleep=lambda s: None) == 0
    assert r.stop is True, "the loop must actually stop so the supervisor can restart it"
    # And it must leave nothing behind that would refuse the replacement.
    assert not (tmp_path / "home" / "state" / "runner.lock").exists()


def test_the_login_item_exit_is_journaled_so_the_restart_is_traceable(tmp_path):
    # A process that simply vanishes and reappears is indistinguishable from a crash loop. The
    # journal is what tells the morning report which it was.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    runner_mod.write_restart_request(r.state, {"target_pid": os.getpid()})
    r.run(max_ticks=5, sleep=lambda s: None)
    rows = [json.loads(x) for x in
            (tmp_path / "home" / "journal.jsonl").read_text().splitlines() if x.strip()]
    restarts = [x for x in rows if x.get("act") == "runner_restart"]
    assert restarts and restarts[-1]["phase"] == "exit_to_supervisor"


def test_a_login_item_runner_records_no_launch_anchor(tmp_path):
    # There is no anchor to record. Writing one would leave the doctor (and the watchdog's restart
    # path) chasing a pane that never existed.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    r.acquire_singleton()
    r._write_anchor()
    assert not (tmp_path / "home" / "state" / "runner.anchor.json").exists()


def test_a_pane_runner_still_records_its_anchor(tmp_path):
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "pane")
    r.acquire_singleton()
    r._write_anchor()
    rec = json.loads((tmp_path / "home" / "state" / "runner.anchor.json").read_text())
    assert rec["pane"] == "pane-1" and rec["pid"] == os.getpid()


def test_every_live_runner_declares_which_home_it_is_in(tmp_path):
    # The one file the doctor and the watchdog read to answer "where does this runner live?".
    # Written in BOTH homes, so a reader never has to infer the home from the absence of an anchor.
    (tmp_path / "repo").mkdir()
    for home_kind, expect_label in ((runner_home.PANE, None),
                                    (runner_home.LOGIN_ITEM,
                                     "com.superlooper.runner.o__r")):
        r = _runner(tmp_path, home_kind)
        r.acquire_singleton()
        r._write_anchor()
        rec = json.loads((tmp_path / "home" / "state" / "runner.home.json").read_text())
        assert rec["kind"] == home_kind and rec["pid"] == os.getpid()
        assert rec.get("label") == expect_label
        r.release_singleton()


def test_the_home_record_is_cleared_on_a_clean_exit_only_by_its_owner(tmp_path):
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    r.acquire_singleton()
    r._write_anchor()
    path = tmp_path / "home" / "state" / "runner.home.json"
    # A runner that lost the singleton must never delete the live holder's record.
    path.write_text(json.dumps({"kind": "login-item", "pid": 999999, "label": "x"}))
    r._clear_anchor()
    assert path.exists()
    path.write_text(json.dumps({"kind": "login-item", "pid": os.getpid(), "label": "x"}))
    r._clear_anchor()
    assert not path.exists()


def test_a_login_item_runner_never_probes_a_pane_anchor(tmp_path):
    # The per-tick anchor probe (issue #24) shells out to the multiplexer for the pane workers are
    # born in. Under this home there is no such pane, so an unguarded probe would report the launch
    # channel DOWN on a perfectly healthy runner and hold the whole queue.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    r._anchor_status = lambda: pytest.fail("the pane probe ran in a home with no pane")
    r._parsed_by_id = {"i1": {"labels": ["agent-ready"]}}       # real launch demand
    assert r._wants_launch() is True
    assert r._probe_launch_anchor() is None


def test_a_pane_runner_still_probes_its_anchor_when_there_is_launch_demand(tmp_path):
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "pane")
    r._parsed_by_id = {"i1": {"labels": ["agent-ready"]}}
    assert r._probe_launch_anchor() == {"ok": True, "reason": ""}


def test_no_home_probes_the_anchor_without_launch_demand(tmp_path):
    # Unchanged from #24: an idle runner never shells out and never alerts.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "pane")
    r._parsed_by_id = {}
    assert r._probe_launch_anchor() is None


def test_the_test_ratchet_keeps_the_real_service_manager_out_of_reach():
    # The other half of the login-item home: launchd's verbs START AND STOP jobs — including the
    # owner's live runner and watchdog — so no test may reach the real `launchctl`. conftest points
    # SL_LAUNCHCTL at a path that cannot exist, and this fails if that neutralization is removed.
    assert not os.path.exists(os.environ["SL_LAUNCHCTL"])


def test_the_launchd_install_target_is_not_the_real_launchagents_dir():
    # SL_LAUNCHD_DIR steers where a plist is PLACED. Inherited from a dogfooding shell it would let
    # a test write a job into the owner's real ~/Library/LaunchAgents; scrubbed, the CLI's default
    # is only ever reached by a test that opts in with its own sandbox dir.
    assert os.environ.get("SL_LAUNCHD_DIR") is None


# --------------------------------------------------------------- what installing it for real found

def test_the_rendered_job_is_valid_xml_when_paths_contain_a_double_hyphen():
    # Found by installing this for real. Substitution is a plain textual replace over the whole
    # file, so a placeholder named IN THE DOCUMENTATION COMMENT is substituted there too — and a
    # value containing `--` (any path under a worktree named with one, which this repo's own
    # worktrees are) makes that comment, and the whole plist, invalid XML. macOS's own parser
    # tolerated it; python's did not, so the DOCTOR reported a healthy running job as unparseable.
    ugly = "/private/tmp/claude-501/-Users-w--superlooper-worktrees-i306/drive"
    d = plistlib.loads(runner_home.render_plist(
        label="com.superlooper.runner.o__r", superlooper_bin=ugly + "/bin/superlooper",
        repo_path=ugly + "/repo", state_home=ugly + "/home",
        path="/opt/homebrew/bin:" + runner_home.LAUNCHD_PATH,
        state_base=ugly + "/slhome").encode())
    assert d["EnvironmentVariables"]["SL_HOME"] == ugly + "/slhome"


def test_a_relocated_state_base_rides_into_the_job():
    d = plistlib.loads(runner_home.render_plist(
        label="l", superlooper_bin="/b", repo_path="/r", state_home="/s", path="/usr/bin",
        state_base="/elsewhere").encode())
    assert d["EnvironmentVariables"]["SL_HOME"] == "/elsewhere"


def test_no_state_base_leaves_the_variable_out_entirely():
    # Baking in the default would freeze a path the operator never chose.
    d = plistlib.loads(runner_home.render_plist(
        label="l", superlooper_bin="/b", repo_path="/r", state_home="/s",
        path="/usr/bin").encode())
    assert "SL_HOME" not in d["EnvironmentVariables"]


def test_a_relative_state_base_is_refused_like_every_other_path():
    with pytest.raises(ValueError):
        runner_home.render_plist(label="l", superlooper_bin="/b", repo_path="/r", state_home="/s",
                                 path="/usr/bin", state_base="slhome")


def test_the_rendered_job_does_not_block_buffer_its_own_log():
    # Also found by driving it: with no terminal on the other end the runner's stdout is
    # BLOCK-buffered, so its boot line and preflight verdict sit in a buffer while the log file the
    # job declares stays zero bytes — on a perfectly healthy runner. The first thing an operator
    # does with a job that misbehaves is read that log.
    d = plistlib.loads(runner_home.render_plist(
        label="l", superlooper_bin="/b", repo_path="/r", state_home="/s",
        path="/usr/bin").encode())
    assert d["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_a_login_item_restart_journals_its_landing_not_just_its_departure(tmp_path):
    # Driving the real job exposed the gap: the exit was journaled and nothing recorded that the
    # supervisor brought it back. An exit followed by a FAILED restart then reads exactly like a
    # successful one in the morning report — which is the single question that report is asked
    # about a restart. The re-exec path solves this with an env token; a process that actually dies
    # cannot carry one, so the departing runner leaves the note on disk and its successor spends it.
    (tmp_path / "repo").mkdir()
    departing = _runner(tmp_path, "login-item")
    runner_mod.write_restart_request(departing.state, {"target_pid": os.getpid()})
    departing.run(max_ticks=5, sleep=lambda s: None)

    reborn = _runner(tmp_path, "login-item")
    reborn.run(max_ticks=1, sleep=lambda s: None)
    rows = [json.loads(x) for x in
            (tmp_path / "home" / "journal.jsonl").read_text().splitlines() if x.strip()]
    phases = [x["phase"] for x in rows if x.get("act") == "runner_restart"]
    assert phases == ["exit_to_supervisor", "up"], phases
    landing = [x for x in rows if x.get("phase") == "up"][0]
    assert landing["old_pid"] == os.getpid() and landing["new_pid"] == os.getpid()

    # Spent exactly once: an ordinary later boot is not a restart landing.
    third = _runner(tmp_path, "login-item")
    third.run(max_ticks=1, sleep=lambda s: None)
    rows = [json.loads(x) for x in
            (tmp_path / "home" / "journal.jsonl").read_text().splitlines() if x.strip()]
    assert [x["phase"] for x in rows if x.get("act") == "runner_restart"] == \
        ["exit_to_supervisor", "up"]


def test_an_ordinary_login_item_boot_journals_no_restart_landing(tmp_path):
    # A first boot, or a KeepAlive restart after a crash, is not a restart the owner asked for.
    (tmp_path / "repo").mkdir()
    r = _runner(tmp_path, "login-item")
    r.run(max_ticks=1, sleep=lambda s: None)
    text = (tmp_path / "home" / "journal.jsonl")
    rows = [json.loads(x) for x in text.read_text().splitlines() if x.strip()] \
        if text.exists() else []
    assert not [x for x in rows if x.get("act") == "runner_restart"]


def test_a_missing_template_breaks_only_the_installer_not_every_import(monkeypatch, tmp_path):
    # This module sits in config.py's import chain, and config is imported by essentially
    # everything. An eager template read would turn a missing file into an ImportError that takes
    # down the runner, the gate, the doctor and the CLI — for a file only the installer needs.
    monkeypatch.setattr(runner_home, "_template_cache", None)
    monkeypatch.setattr(runner_home, "_TEMPLATE_PATH", tmp_path / "gone.plist")
    # Everything else still works...
    assert runner_home.kind({"runner_home": "login-item"}) == runner_home.LOGIN_ITEM
    assert runner_home.label("o/r") == "com.superlooper.runner.o__r"
    # ...and the one verb that needs it says exactly what is wrong.
    with pytest.raises(FileNotFoundError) as e:
        runner_home.render_plist(label="l", superlooper_bin="/b", repo_path="/r", state_home="/s",
                                 path="/usr/bin")
    assert "install.sh" in str(e.value)
