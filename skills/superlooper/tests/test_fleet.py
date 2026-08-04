"""The fleet machine's build-up (issue #309) — its layout, its rendered artefacts, and its judge.

The build-up is a machine-state act whose DoD is a list of properties nobody can eyeball, and every
one of them is silently wrong in a way that still looks alive: an unfenced socket answers, a stale
manifest override still parses, a fleet config dir with a trailing slash simply reports logged-out,
and a host one release off the pin starts perfectly. So the build-up ships with a judge, and this
file is the judge's own tests.

Everything here is pure or probe-injected. No test reaches a real launchctl, a real control socket,
a real `claude`, or the operator's own host config — the suite-wide ratchet.
"""
import plistlib
import re
from pathlib import Path

import fleet
import runner_home
import session_host

from test_stack_doctor import FakeProbe

_TEMPLATES = Path(__file__).resolve().parent.parent / "skill" / "templates"

_HOME = "/home/will"
_STATE_BASE = _HOME + "/.superlooper"
_PREFIX = _STATE_BASE + "/fleet"
_BIN = fleet.host_binary(_PREFIX)
_HOST_CONFIG_DIR = _HOME + "/.config/host"
_FLEET_CLAUDE_DIR = _HOME + "/.claude-fleet"
_CLAUDE = _HOME + "/.local/bin/claude"
_OWNER = {"email": "owner@y.com", "org": "org-owner"}
_FLEET = {"email": "fleet@x.com", "org": "org-fleet"}


def _auth_json(account, sub="max", logged_in=True):
    return ('{"loggedIn": %s, "authMethod": "claude.ai", "email": "%s", "orgId": "%s",'
            ' "subscriptionType": "%s"}'
            % ("true" if logged_in else "false", account["email"], account["org"], sub))


class EnvProbe(FakeProbe):
    """A FakeProbe whose `claude auth status` answer depends on CLAUDE_CONFIG_DIR — because that is
    the entire mechanism under test (#300: the credential namespace is keyed by the config dir).

    A probe that returned one answer regardless of environment would let the identity check pass
    while asking the same question twice, which is the exact bug the check exists to catch.
    """

    def __init__(self, *args, accounts=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = accounts or {}
        self.modes = {}
        self.run_env = []

    def mode(self, path):
        return self.modes.get(path, 0o600) if self.exists(path) else None

    def contains(self, path, needle):
        if not self.exists(path):
            return None
        return needle in (self.files.get(path) or "")

    def run(self, argv, timeout=10, env=None):
        self.run_env.append((list(argv), dict(env or {})))
        if list(argv[1:3]) == ["auth", "status"]:
            answer = self.accounts.get((env or {}).get("CLAUDE_CONFIG_DIR"))
            if answer is None:
                return super().run(["__missing__"], timeout)
            return super().run(["__auth__"], timeout) if answer == "error" else _ok(answer)
        return super().run(argv, timeout)


class _Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _ok(text):
    return _Proc(0, text)


# --------------------------------------------------------------- layout

def test_the_prefix_hangs_off_the_state_base_and_never_off_a_repo():
    # The session host is ONE server for the whole machine; a per-repo prefix would give two repos
    # two hosts, two fences and two tokens, which is not a fleet.
    assert fleet.prefix(_STATE_BASE) == _PREFIX
    assert _BIN.startswith(_PREFIX + "/bin/")
    assert fleet.token_file(_PREFIX) == _PREFIX + "/token"
    assert fleet.server_plist_path(_HOME).endswith("/Library/LaunchAgents/"
                                                   + fleet.SERVER_LABEL + ".plist")


def test_the_socket_path_is_the_hosts_own_derivation_for_a_named_session():
    # Read out of the pinned release's session.rs: <config dir>/sessions/<name>/<socket>. Spelled
    # in the doorway, never here — this module names no host machinery.
    got = fleet.socket_path(_HOST_CONFIG_DIR)
    assert got == session_host.session_socket_path(_HOST_CONFIG_DIR, fleet.SESSION_NAME)
    assert "/sessions/" + fleet.SESSION_NAME + "/" in got
    assert got != session_host.session_socket_path(_HOST_CONFIG_DIR, None)


def test_a_socket_path_at_or_past_the_sun_path_limit_is_refused_with_its_length():
    assert fleet.socket_path_problem("/short/path.sock") is None
    long_path = "/" + ("x" * fleet.SUN_PATH_MAX) + ".sock"
    problem = fleet.socket_path_problem(long_path)
    assert problem and str(fleet.SUN_PATH_MAX) in problem and long_path in problem
    # Counted in BYTES: a multibyte home directory spends more of the budget than it looks like.
    wide = "/" + ("é" * (fleet.SUN_PATH_MAX // 2))
    assert fleet.socket_path_problem(wide), "the limit must be measured in bytes, not characters"


# --------------------------------------------------------------- the host config (plan §2)

def test_the_rendered_host_config_carries_the_plan_s_session_settings():
    text = fleet.render_host_config()
    assert re.search(r"^resume_agents_on_restore\s*=\s*true$", text, re.M), text
    # It must sit under [session] — the host reads it there, and a bare top-level key parses as
    # valid TOML and is silently ignored.
    assert text.index("[session]") < text.index("resume_agents_on_restore")
    assert fleet.host_config_problem(text) is None


def test_the_host_config_check_names_each_missing_setting():
    assert "no config file" in fleet.host_config_problem(None)
    assert "[session]" in fleet.host_config_problem("version_check = false\n")
    only_session = "[session]\nresume_agents_on_restore = true\n"
    assert "version_check" in fleet.host_config_problem(only_session)
    off = "version_check = false\n[session]\nresume_agents_on_restore = false\n"
    assert "resume_agents_on_restore" in fleet.host_config_problem(off)
    # A commented-out setting is not a setting.
    assert fleet.host_config_problem("# resume_agents_on_restore = true\n[session]\n")


def test_the_host_config_is_written_where_the_host_reads_it():
    assert fleet.host_config_path(_HOST_CONFIG_DIR) == _HOST_CONFIG_DIR + "/config.toml"


# --------------------------------------------------------------- the screen-state override

def test_the_override_appends_a_catch_all_that_makes_an_unknown_screen_unknown():
    source = 'id = "claude"\nversion = "2026.08.04.1"\n\n[[rules]]\nid = "x"\nstate = "idle"\n'
    text = fleet.render_manifest_override(source, source_version="2026.08.04.1")
    # The vendor's own rules are carried verbatim — a local override REPLACES the manifest it
    # shadows, so dropping a rule trades a wrong `idle` for no detection at all.
    assert 'id = "x"' in text and 'version = "2026.08.04.1"' in text
    # ...plus exactly one rule of ours, at the lowest priority, so it fires only where nothing
    # else did — which is precisely the host's `default_known_agent_idle_fallback` case.
    assert text.count(fleet.CATCHALL_RULE_ID) == 1
    tail = text[text.index(fleet.CATCHALL_RULE_ID):]
    assert 'state = "unknown"' in tail
    assert re.search(r"priority\s*=\s*([0-9]+)", tail).group(1) == "1"
    assert fleet.override_source_version(text) == "2026.08.04.1"


def test_rendering_an_override_twice_does_not_stack_catch_alls():
    source = 'id = "claude"\nversion = "1"\n\n[[rules]]\nid = "x"\nstate = "idle"\n'
    once = fleet.render_manifest_override(source, source_version="1")
    twice = fleet.render_manifest_override(once, source_version="2")
    assert twice.count(fleet.CATCHALL_RULE_ID) == 1
    assert twice.count("[[rules]]") == once.count("[[rules]]")
    assert fleet.override_source_version(twice) == "2"
    assert 'id = "x"' in twice


def test_the_override_records_the_snapshotted_version_even_when_not_told_one():
    source = 'id = "claude"\nversion = "2026.01.01.1"\n'
    assert fleet.override_source_version(fleet.render_manifest_override(source)) == "2026.01.01.1"
    assert fleet.override_source_version("id = \"claude\"\n") is None


def test_the_override_lands_where_the_host_looks_for_a_local_override():
    assert (fleet.override_path(_HOST_CONFIG_DIR, "claude")
            == _HOST_CONFIG_DIR + "/agent-detection/claude.toml")


# --------------------------------------------------------------- the login item

def test_the_server_plist_is_a_gui_login_item_with_an_explicit_path_and_no_secret():
    text = fleet.render_server_plist(binary=_BIN, session=fleet.SESSION_NAME,
                                     token_file=_PREFIX + "/token", log_dir=_PREFIX + "/logs",
                                     path="/opt/homebrew/bin:/usr/bin:/bin")
    job = plistlib.loads(text.encode())
    assert job["Label"] == fleet.SERVER_LABEL
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True
    assert job["ProgramArguments"][0] == _BIN
    assert fleet.SESSION_NAME in job["ProgramArguments"]
    env = job["EnvironmentVariables"]
    # PATH is load-bearing: launchd hands a job four directories and none of them is Homebrew, and
    # every pane the server spawns inherits what the server got.
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    # The token must NEVER be a plist value. Measured on the fleet machine: `launchctl print
    # gui/$UID/<label>` echoes EnvironmentVariables verbatim, so a token there is one command away
    # from any same-uid process — which is exactly the worker the fence exists to keep out.
    assert env[session_host.API_TOKEN_FILE_ENV_VAR] == _PREFIX + "/token"
    assert session_host.API_TOKEN_ENV_VAR not in env


def test_the_server_plist_refuses_a_relative_path_and_an_unaddressable_session():
    for bad in ("bin/host", "./host"):
        try:
            fleet.render_server_plist(binary=bad, session="fleet", token_file="/t",
                                      log_dir="/l", path="/usr/bin")
        except ValueError as e:
            assert "absolute" in str(e)
        else:
            raise AssertionError("a relative program path was accepted: %r" % bad)
    try:
        fleet.render_server_plist(binary="/b", session="Fleet Host", token_file="/t",
                                  log_dir="/l", path="/usr/bin")
    except ValueError as e:
        assert "addressable" in str(e)
    else:
        raise AssertionError("an unaddressable session name was accepted")


def test_a_substituted_value_lands_escaped_so_a_stray_ampersand_cannot_break_the_plist():
    text = fleet.render_server_plist(binary="/R&D/host", session="fleet", token_file="/t&t",
                                     log_dir="/l", path="/usr/bin")
    job = plistlib.loads(text.encode())          # the assertion: it still parses
    assert job["ProgramArguments"][0] == "/R&D/host"


def test_the_shipped_server_template_is_a_real_plist_and_names_no_system_domain():
    raw = (_TEMPLATES / "launchd.session-host.plist").read_text()
    rendered = re.sub(r"\{[a-z_]+\}", "x", raw)
    plistlib.loads(rendered.encode())
    assert "system/" not in raw, "the keychain rule: gui/$UID only"


def test_the_viewer_ships_as_an_artefact_and_never_as_something_an_install_registers():
    text = fleet.render_viewer_command(binary=_BIN, session=fleet.SESSION_NAME)
    assert text.startswith("#!/bin/sh")
    assert _BIN in text and fleet.SESSION_NAME in text
    assert fleet.viewer_command(_PREFIX).endswith(".command")


# --------------------------------------------------------------- identity (c25, #300, #313)

def test_a_config_dir_that_is_not_the_canonical_spelling_is_refused():
    # #300's landmine 1: the credential namespace is sha256 over the string AS WRITTEN. Five
    # spellings of one directory are five identities, and the wrong one presents as auth-death.
    assert fleet.config_dir_problem(_FLEET_CLAUDE_DIR) is None
    for bad in (_FLEET_CLAUDE_DIR + "/", "~/.claude-fleet", ".claude-fleet",
                _HOME + "/./.claude-fleet", _HOME + "//.claude-fleet", "", None):
        assert fleet.config_dir_problem(bad), bad


def test_identity_is_separate_only_when_both_the_account_and_the_billing_org_differ():
    fleet_side = {"loggedIn": True, "email": "fleet@x.com", "orgId": "org-fleet",
                  "subscriptionType": "max"}
    owner_side = {"loggedIn": True, "email": "owner@y.com", "orgId": "org-owner"}
    assert fleet.identity_problem(fleet_side, owner_side) is None
    # Same org = the same billing entity, whatever the address says. That is the failure this
    # check exists for: the fleet quietly spending the owner's subscription, with no error.
    assert "orgId" in fleet.identity_problem(dict(fleet_side, orgId="org-owner"), owner_side)
    assert fleet.identity_problem(dict(fleet_side, loggedIn=False), owner_side)
    assert fleet.identity_problem(dict(fleet_side, orgId=""), owner_side)
    assert fleet.identity_problem(None, owner_side)
    # The owner's dir being unreadable is not the FLEET's failure, and reporting it as one would
    # send someone to fix the wrong machine.
    assert fleet.identity_problem(fleet_side, None) is None


# --------------------------------------------------------------- the checks

def _green_plist():
    return fleet.render_server_plist(binary=_BIN, session=fleet.SESSION_NAME,
                                     token_file=fleet.token_file(_PREFIX),
                                     log_dir=fleet.log_dir(_PREFIX),
                                     path="/opt/homebrew/bin:" + runner_home.LAUNCHD_PATH)


def _green_probe(files=None, commands=None, env=None, accounts=None):
    all_files = {
        _BIN: "ELF-ish bytes " + fleet.FENCE_SIGNATURE + " more bytes",
        fleet.token_file(_PREFIX): "s3cret\n",
        fleet.server_plist_path(_HOME): _green_plist(),
        fleet.host_config_path(_HOST_CONFIG_DIR): fleet.render_host_config(),
        fleet.override_path(_HOST_CONFIG_DIR, "claude"):
            fleet.render_manifest_override('id = "claude"\nversion = "9"\n', source_version="9"),
        _FLEET_CLAUDE_DIR + "/.claude.json": '{"hasCompletedOnboarding": true}',
    }
    all_files.update(files or {})
    all_commands = {
        _BIN: {"path": _BIN, ("--version",): (0, "host %s\n" % fleet.PINNED_VERSION, "")},
        "claude": {"path": _CLAUDE},
        "launchctl": {"path": "/bin/launchctl",
                      ("print", runner_home.service_target(501, fleet.SERVER_LABEL)):
                          (0, "\tstate = running\n\tpid = 4242\n", "")},
    }
    all_commands.update(commands or {})
    return EnvProbe(files=all_files, commands=all_commands, home=_HOME,
                    env=env if env is not None else {"HOME": _HOME},
                    accounts=accounts if accounts is not None
                    else {_FLEET_CLAUDE_DIR: _auth_json(_FLEET), None: _auth_json(_OWNER)})


def test_the_binary_check_fails_when_the_installed_host_is_not_the_pinned_version():
    probe = _green_probe(commands={_BIN: {"path": _BIN, ("--version",): (0, "host 0.7.5\n", "")}})
    r = fleet.check_host_binary(probe, _PREFIX)
    assert not r.ok and "0.7.5" in r.detail and fleet.PINNED_VERSION in r.detail


def test_the_binary_check_names_the_build_script_when_nothing_is_installed():
    r = fleet.check_host_binary(FakeProbe(home=_HOME), _PREFIX)
    assert not r.ok and "build.sh" in (r.fix or "")


def test_the_binary_check_passes_only_on_the_pin():
    assert fleet.check_host_binary(_green_probe(), _PREFIX).ok


def test_a_stock_binary_at_the_pinned_version_is_not_a_fenced_one():
    # The version cannot tell them apart — a stock v0.8.0 hand-copied into the fleet prefix reports
    # exactly the pinned string. The patch's own refusal message, compiled in, is what can.
    stock = _green_probe(files={_BIN: "ELF-ish bytes with no patch in them"})
    r = fleet.check_host_binary(stock, _PREFIX)
    assert not r.ok and "stock build" in r.detail
    assert "--force" in (r.fix or "")


def test_an_unreadable_binary_is_not_a_fenced_one_either():
    class Blind(EnvProbe):
        def contains(self, path, needle):
            return None
    probe = _green_probe()
    probe.__class__ = Blind
    r = fleet.check_host_binary(probe, _PREFIX)
    assert not r.ok and "not being able to look is not proof" in r.detail


def test_the_fence_check_is_red_on_an_open_socket_and_on_silence():
    assert fleet.check_fence("/s.sock", lambda p: session_host.FENCED).ok
    for bad in (session_host.OPEN, session_host.UNREACHABLE):
        assert not fleet.check_fence("/s.sock", lambda p, b=bad: b).ok, bad
    # An OPEN socket is the dangerous answer, not the milder one: it is the state that looks
    # healthy from the runner's seat, so the wording has to carry the consequence.
    assert "unattended" in fleet.check_fence("/s.sock", lambda p: session_host.OPEN).detail


def test_the_fence_wait_stops_at_the_first_answer_and_never_waits_out_a_bad_one():
    # Driving the real install found this: `--load` bootstraps the job and judges it a moment
    # later, and a server still binding is UNREACHABLE — a red line the command created itself.
    answers = [session_host.UNREACHABLE, session_host.UNREACHABLE, session_host.FENCED]
    naps = []
    assert fleet.wait_for_fence("/s.sock", fence=lambda p: answers.pop(0),
                                sleep=naps.append) == session_host.FENCED
    assert len(naps) == 2
    # An OPEN socket is an answer. Waiting it out would delay the one verdict that must be loud.
    naps.clear()
    assert fleet.wait_for_fence("/s.sock", fence=lambda p: session_host.OPEN,
                                sleep=naps.append) == session_host.OPEN
    assert naps == []
    # Silence stays silence — bounded, and never upgraded to a guess.
    naps.clear()
    assert fleet.wait_for_fence("/s.sock", fence=lambda p: session_host.UNREACHABLE,
                                sleep=naps.append, attempts=3) == session_host.UNREACHABLE
    assert len(naps) == 2


def test_the_login_item_check_refuses_a_job_that_is_not_loaded_in_the_gui_domain():
    r = fleet.check_login_item(_green_probe(), uid=501, home=_HOME)
    assert r.ok and "4242" in r.detail
    dead = _green_probe(commands={"launchctl": {
        "path": "/bin/launchctl",
        ("print", runner_home.service_target(501, fleet.SERVER_LABEL)):
            (113, "", "Could not find service")}})
    r = fleet.check_login_item(dead, uid=501, home=_HOME)
    assert not r.ok and runner_home.domain(501) in r.detail


def test_the_login_item_check_refuses_a_plist_carrying_only_launchds_own_path():
    bare = fleet.render_server_plist(binary=_BIN, session=fleet.SESSION_NAME,
                                     token_file=fleet.token_file(_PREFIX),
                                     log_dir=fleet.log_dir(_PREFIX),
                                     path=runner_home.LAUNCHD_PATH)
    r = fleet.check_login_item(_green_probe(files={fleet.server_plist_path(_HOME): bare}),
                               uid=501, home=_HOME)
    assert not r.ok and "PATH" in r.detail


def test_an_unresolvable_launchctl_is_a_failure_not_a_passing_warn():
    # Absence of signal is never health (c2): a plist on disk starts nothing, and a WARN here would
    # let the whole build-up exit ready on a job nobody proved was loaded.
    blind = _green_probe(commands={"launchctl": {}})
    blind.commands.pop("launchctl")
    r = fleet.check_login_item(blind, uid=501, home=_HOME)
    assert not r.ok and not r.warn and "could not be read" in r.detail


def test_a_plist_that_spells_the_token_itself_is_refused():
    # The renderer never writes this; the judge reads what is INSTALLED. `launchctl print` echoes
    # EnvironmentVariables verbatim, so such a plist publishes the fence token to every same-uid
    # process — the worker the fence exists to keep out.
    leaky = _green_plist().replace(
        "<key>%s</key>" % session_host.API_TOKEN_FILE_ENV_VAR,
        "<key>%s</key>" % session_host.API_TOKEN_ENV_VAR)
    r = fleet.check_login_item(_green_probe(files={fleet.server_plist_path(_HOME): leaky}),
                               uid=501, home=_HOME)
    assert not r.ok and "rotate" in (r.fix or "")


def test_a_plist_with_no_path_at_all_is_the_worse_case_not_the_neutral_one():
    naked = _green_plist().replace(
        "<key>PATH</key>\n        <string>/opt/homebrew/bin:%s</string>\n        "
        % runner_home.LAUNCHD_PATH, "")
    r = fleet.check_login_item(_green_probe(files={fleet.server_plist_path(_HOME): naked}),
                               uid=501, home=_HOME)
    assert not r.ok and "no PATH at all" in r.detail


def test_a_loaded_but_idle_job_is_a_failure_not_a_pass():
    idle = _green_probe(commands={"launchctl": {
        "path": "/bin/launchctl",
        ("print", runner_home.service_target(501, fleet.SERVER_LABEL)):
            (0, "\tstate = not running\n", "")}})
    r = fleet.check_login_item(idle, uid=501, home=_HOME)
    assert not r.ok and "NOT running" in r.detail


def test_the_config_check_reports_the_socket_it_will_bind():
    r = fleet.check_host_config(_green_probe(), _HOST_CONFIG_DIR)
    assert r.ok and fleet.socket_path(_HOST_CONFIG_DIR) in r.detail
    missing = _green_probe(files={fleet.host_config_path(_HOST_CONFIG_DIR): "[session]\n"})
    assert not fleet.check_host_config(missing, _HOST_CONFIG_DIR).ok


def test_the_screen_fallback_check_fails_on_absence_and_only_warns_on_staleness():
    probe = _green_probe()
    assert fleet.check_screen_fallback(probe, _HOST_CONFIG_DIR).ok
    stale = fleet.check_screen_fallback(probe, _HOST_CONFIG_DIR, live_version="10")
    # Advisory by our own doctrine (plan §5.2) — a stale snapshot costs detection quality, not
    # correctness, so it must never turn the build-up red.
    assert stale.ok and stale.warn and "9" in stale.detail and "10" in stale.detail
    gone = _green_probe(files={fleet.override_path(_HOST_CONFIG_DIR, "claude"): "id = \"claude\"\n"})
    r = fleet.check_screen_fallback(gone, _HOST_CONFIG_DIR)
    assert not r.ok and "idle" in r.detail


def test_a_key_in_a_later_table_does_not_satisfy_the_session_setting():
    # TOML keys belong to the table they follow. This parses, and the host reads
    # `ui.resume_agents_on_restore` — which it ignores.
    assert fleet.host_config_problem(
        "version_check = false\n[session]\n[ui]\nresume_agents_on_restore = true\n")
    # ...and the top-level setting must not be satisfied from inside [session] either.
    assert fleet.host_config_problem(
        "[session]\nresume_agents_on_restore = true\nversion_check = false\n")


def test_a_commented_out_or_absent_rule_is_not_an_installed_override():
    good = fleet.render_manifest_override('id = "claude"\nversion = "9"\n', source_version="9")
    assert fleet.has_catchall_rule(good)
    assert not fleet.has_catchall_rule('# id = "%s"' % fleet.CATCHALL_RULE_ID)
    assert not fleet.has_catchall_rule('id = "%s"' % fleet.CATCHALL_RULE_ID)   # no [[rules]] header
    # The rule is there but its state has been edited to the very answer we are refusing.
    assert not fleet.has_catchall_rule(good.replace('state = "unknown"', 'state = "idle"'))
    assert not fleet.has_catchall_rule(None)


def test_the_isolation_check_refuses_a_readable_fence_token():
    loose = _green_probe()
    loose.modes[fleet.token_file(_PREFIX)] = 0o644
    r = fleet.check_isolation(loose, _PREFIX, _HOST_CONFIG_DIR)
    assert not r.ok and "644" in r.detail
    # A token that cannot be stat-ed at all is not a token whose permissions are fine.
    r = fleet.check_isolation(EnvProbe(home=_HOME), _PREFIX, _HOST_CONFIG_DIR)
    assert not r.ok and "unknown" in r.detail


def test_the_identity_check_fails_when_the_fleet_dir_reads_the_owners_login():
    same = _green_probe(accounts={_FLEET_CLAUDE_DIR: _auth_json(_OWNER),
                                  None: _auth_json(_OWNER)})
    r = fleet.check_identity(same, _FLEET_CLAUDE_DIR, claude=_CLAUDE)
    assert not r.ok and "orgId" in r.detail
    # The measurement IS the pair of reads: the same command, run twice, differing only in the
    # config dir. A check that asked once could never see this failure.
    dirs = [env.get("CLAUDE_CONFIG_DIR") for argv, env in same.run_env
            if list(argv[1:3]) == ["auth", "status"]]
    assert dirs == [_FLEET_CLAUDE_DIR, None]


def test_the_identity_check_passes_on_two_different_billing_orgs():
    r = fleet.check_identity(_green_probe(), _FLEET_CLAUDE_DIR, claude=_CLAUDE)
    assert r.ok and not r.warn and _FLEET["org"] in r.detail


def test_an_unprovisioned_fleet_dir_fails_the_build_up():
    # A FAIL, not a warn: an unprovisioned config dir parks the first worker at the theme picker,
    # a screen no auth manifest covers and the host reports as idle. #313's DoD lists provisioning
    # past onboarding for exactly this reason.
    for onboarding in ("{}", '{"hasCompletedOnboarding": false}', "not json at all"):
        probe = _green_probe(files={_FLEET_CLAUDE_DIR + "/.claude.json": onboarding})
        r = fleet.check_identity(probe, _FLEET_CLAUDE_DIR, claude=_CLAUDE)
        assert not r.ok and "onboarding" in r.detail, onboarding
    # Both spellings of the flag are the same fact — a check that knew only one would false-red a
    # perfectly provisioned dir.
    for spelling in ('{"hasCompletedOnboarding":true}', '{"hasCompletedOnboarding": true}'):
        probe = _green_probe(files={_FLEET_CLAUDE_DIR + "/.claude.json": spelling})
        assert fleet.check_identity(probe, _FLEET_CLAUDE_DIR, claude=_CLAUDE).ok, spelling


def test_the_identity_check_refuses_a_credential_redirect_it_inherited():
    # #300's landmine 2: CLAUDE_SECURESTORAGE_CONFIG_DIR present-but-EMPTY collapses the namespace
    # back to the owner's. Silent, and it bills the wrong subscription.
    probe = _green_probe(env={"HOME": _HOME, "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""})
    r = fleet.check_identity(probe, _FLEET_CLAUDE_DIR, claude=_CLAUDE)
    assert not r.ok and "CLAUDE_SECURESTORAGE_CONFIG_DIR" in r.detail


def test_the_identity_check_refuses_a_non_canonical_dir_before_it_asks_anything():
    probe = _green_probe()
    r = fleet.check_identity(probe, _FLEET_CLAUDE_DIR + "/", claude=_CLAUDE)
    assert not r.ok
    assert not probe.run_env, "a dir that cannot be trusted must not be measured"


def test_the_isolation_check_says_what_it_cannot_see():
    r = fleet.check_isolation(_green_probe(), _PREFIX, _HOST_CONFIG_DIR)
    assert r.ok
    # It is a one-machine check by construction: the other machine's production is not observable
    # from here, and a block that implied otherwise would be a confident liar.
    assert "not observable" in r.detail


# --------------------------------------------------------------- the report

def test_check_fleet_returns_one_named_block_per_dod_property():
    results = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                                host_config_dir=_HOST_CONFIG_DIR,
                                fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                fence=lambda p: session_host.FENCED)
    names = [r.name for r in results]
    assert names == list(dict.fromkeys(names)), "duplicate block names: %s" % names
    for expected in ("host binary", "host login item", "host fence", "host config",
                     "screen fallback", "claude binary", "fleet identity", "fleet isolation"):
        assert expected in names, "%s missing from %s" % (expected, names)
    # The binary pin comes BEFORE the identity read for the same reason it does in doctor --stack:
    # which claude is in use is upstream of which account it is logged into.
    assert names.index("claude binary") < names.index("fleet identity")


def test_check_fleet_is_green_on_a_built_machine_and_red_on_an_open_socket():
    green = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                              host_config_dir=_HOST_CONFIG_DIR,
                              fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                              fence=lambda p: session_host.FENCED)
    assert [r.name for r in green if not r.ok] == []
    open_socket = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                                    host_config_dir=_HOST_CONFIG_DIR,
                                    fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                    fence=lambda p: session_host.OPEN)
    assert [r.name for r in open_socket if not r.ok] == ["host fence"]
