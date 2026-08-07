"""The fleet machine's build-up (issue #309) — its layout, its rendered artefacts, and its judge.

The build-up is a machine-state act whose DoD is a list of properties nobody can eyeball, and every
one of them is silently wrong in a way that still looks alive: an unfenced socket answers, a stale
manifest override still parses, a fleet config dir with a trailing slash simply reports logged-out,
and a host one release off the pin starts perfectly. So the build-up ships with a judge, and this
file is the judge's own tests.

Everything here is pure or probe-injected. No test reaches a real launchctl, a real control socket,
a real `claude`, or the operator's own host config — the suite-wide ratchet.
"""
import inspect
import json
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
        # Mirrors the real Probe: None for anything that is not a readable REGULAR file, which is
        # how a symlinked token reaches the caller.
        if path in self.modes:
            return self.modes[path]
        return 0o600 if self.exists(path) else None

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
    # A file the HOST would refuse is not a file this may call merely incomplete: the config is
    # PARSED, so invalid TOML is reported as invalid rather than judged on its spelling.
    dup = "version_check = false\n[session]\nresume_agents_on_restore = true\n[session]\n"
    assert "not valid TOML" in fleet.host_config_problem(dup)


def test_both_config_readers_give_the_same_verdicts(monkeypatch):
    # `tomllib` is 3.11+. The fleet machine has it; the CI floor does not, so there is a fallback
    # line walker — and a check whose strictness depends on the interpreter is exactly the kind of
    # thing that passes here and misses there. Both readers answer every case identically or this
    # goes red.
    cases = [
        fleet.render_host_config(),
        fleet.host_config_settings(),
        "version_check = false\n[session]\nresume_agents_on_restore = true\n[session]\n",
        "version_check = false\n[session]\n[ui]\nresume_agents_on_restore = true\n",
        "[session]\nresume_agents_on_restore = true\nversion_check = false\n",
        "version_check = false\n",
        "version_check = false\n[session]\nresume_agents_on_restore = false\n",
        "# version_check = false\n[session]\nresume_agents_on_restore = true\n",
    ]
    cases += [
        # Anything the walker cannot classify must fail CLOSED: the host refuses to start on a
        # config it cannot parse, and a file that already carried both settings must not sail past
        # on the strength of them.
        "version_check = false\n[session]\nresume_agents_on_restore = true\n[[broken\n",
        "version_check = false\n[session]\nresume_agents_on_restore = true\nthis is = = junk\n",
    ]
    real = [fleet.host_config_problem(c) for c in cases]
    monkeypatch.setattr(fleet, "tomllib", None)
    fallback = [fleet.host_config_problem(c) for c in cases]
    for case, a, b in zip(cases, real, fallback):
        assert (a is None) == (b is None), (case, a, b)
    assert (real[0], fallback[0]) == (None, None)
    # Both must catch the duplicate table — the case a flat text sweep calls healthy and the host
    # refuses to start on — and both must SAY it the same way, or one machine's report is
    # unrecognisable beside another's.
    assert "not valid TOML" in real[2] and "not valid TOML" in fallback[2]


def test_the_merge_instructions_carry_no_ownership_marker():
    # A refusal tells an operator to paste these into a file they maintain. If they carried the
    # marker, the NEXT install would read it as ownership and replace the whole file.
    settings = fleet.host_config_settings()
    assert fleet.CONFIG_MARKER not in settings
    assert fleet.host_config_problem(settings) is None       # ...and they are still sufficient
    assert fleet.CONFIG_MARKER in fleet.render_host_config()  # the managed file still claims it


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
        # A BUILT machine arms its own runner (issue #355). Before this file existed the fleet
        # judge could go entirely green on a machine whose launcher checked nothing.
        fleet.env_file(_PREFIX): fleet.render_env_file(),
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


# --------------------------------- the ruled state-report allowance (issues #331, #344)
# `host fence` asks whether a tokenless caller is refused at all. This asks the second half — is
# the ONE method the owner's 2026-08-04 ruling opened admitted — and it is the half nothing on the
# machine's filesystem can answer. Two generations of the host binary exist (the fence as first
# built, and the fence carrying the allowance); they report the same version and carry the same
# compiled-in refusal message, so `host binary` is green for both, and from the runner's seat both
# machines launch, run and work. Only the live socket tells them apart.
#
# Every case below drives the REAL probes in session_host through a scripted socket, never a
# stand-in for the probes themselves: a second implementation of that reading is exactly what this
# issue exists to avoid.

def _socket_saying(**by_method):
    """A fake control socket: one canned reply line per method asked for, silence for the rest."""
    def connect(socket_path, payload, timeout):
        reply = by_method.get(json.loads(payload)["method"])
        return "" if reply is None else json.dumps(reply)
    return connect


def _unauthorized():
    return {"id": "x", "error": {"code": "unauthorized", "message": fleet.FENCE_SIGNATURE}}


def _probes_for(**by_method):
    """(fence, capture) — the real probes bound to one scripted socket, in check_* seam shape."""
    connect = _socket_saying(**by_method)
    return (lambda s: session_host.fence_probe(s, connect=connect),
            lambda s: session_host.state_report_probe(s, connect=connect))


def test_the_state_capture_check_fails_a_fenced_host_that_refuses_the_state_report():
    """The failure this issue closes: the machine looks entirely healthy, and captures nothing."""
    fence, capture = _probes_for(ping=_unauthorized(),
                                 **{session_host.STATE_REPORT_METHOD: _unauthorized()})
    r = fleet.check_state_capture("/s.sock", capture=capture, fence=fence)
    assert not r.ok
    assert session_host.STATE_REPORT_METHOD in r.detail and "bare shell" in r.detail
    # The rebuild, named — and named with the flag that makes it happen, because an installed
    # binary already reporting the pin is what build.sh otherwise leaves alone.
    assert "build.sh" in (r.fix or "") and "--force" in (r.fix or "")


def test_the_state_capture_check_passes_only_when_a_FENCED_host_admits_the_report():
    """The ruled state: refused everywhere, admitted for that one method (owner, 2026-08-04)."""
    fence, capture = _probes_for(
        ping=_unauthorized(),
        **{session_host.STATE_REPORT_METHOD: {"id": "x", "error": {"code": "pane_not_found",
                                                                   "message": "no such pane"}}})
    r = fleet.check_state_capture("/s.sock", capture=capture, fence=fence)
    assert r.ok and not r.warn, r.detail
    assert session_host.STATE_REPORT_METHOD in r.detail


def test_a_socket_that_admits_everything_is_not_the_ruled_allowance():
    """An unfenced host admits the report BECAUSE it admits everything, and a build-up judge that
    rendered that as the allowance would be certifying a property it never measured."""
    fence, capture = _probes_for(
        ping={"id": "x", "result": {"type": "pong", "version": fleet.PINNED_VERSION}},
        **{session_host.STATE_REPORT_METHOD: {"id": "x", "result": {"type": "ok"}}})
    r = fleet.check_state_capture("/s.sock", capture=capture, fence=fence)
    assert not r.ok and "no fence" in r.detail


def test_an_unreachable_socket_is_read_as_neither_admitted_nor_refused():
    """Absence of signal is unknown, never health (c2) — and never the REBUILD either: sending
    somebody to rebuild a host on the strength of a question that went unanswered is the same
    unmeasured certification in the other direction."""
    for answers in ({}, {"ping": _unauthorized()}):
        fence, capture = _probes_for(**answers)
        r = fleet.check_state_capture("/s.sock", capture=capture, fence=fence)
        assert not r.ok, answers
        assert "--force" not in (r.fix or ""), answers
        assert "REFUSES" not in r.detail and "admits the state report" not in r.detail, answers
    # ...and the third shape of silence: the report gets through, the fence question does not.
    fence, capture = _probes_for(
        **{session_host.STATE_REPORT_METHOD: {"id": "x", "error": {"code": "pane_not_found",
                                                                   "message": "no such pane"}}})
    r = fleet.check_state_capture("/s.sock", capture=capture, fence=fence)
    assert not r.ok and "--force" not in (r.fix or "")


def test_the_state_capture_check_cannot_reach_the_binarys_contents_at_all():
    """The DoD's own words, kept structurally. A block that consulted the file on disk would be
    green on both generations — that is the defect — so this one takes no filesystem probe: it is
    handed a socket path and two callables, and there is nothing else it could read."""
    assert "probe" not in inspect.signature(fleet.check_state_capture).parameters
    fence, capture = _probes_for(ping=_unauthorized(),
                                 **{session_host.STATE_REPORT_METHOD: _unauthorized()})
    assert not fleet.check_state_capture("/s.sock", capture=capture, fence=fence).ok


# ------------------------------------------- the machine's own runner environment (issue #355)
# The fence pre-flight (#326) is armed by SL_FLEET_FENCE on the RUNNER's own process, and until
# this issue nothing in the engine set it — so the gate shipped correct and inert on the one
# machine it was built for. These are the tests for the file the build-up writes, for the rule
# that a machine's own declaration beats an ambient variable, and for the block that reports it.


class _Unreadable(FakeProbe):
    """A probe for which a file EXISTS and cannot be read — the one state `read_text` alone
    cannot express (it answers None for both absent and unreadable)."""

    def read_text(self, path):
        return None


def test_the_env_file_lives_in_the_fleet_prefix_and_not_in_a_repo():
    # Same reason the prefix itself does: the fence is a property of the MACHINE's host server, and
    # a per-repo file would be two repos disagreeing about one socket.
    assert fleet.env_file(_PREFIX) == _PREFIX + "/environment"


def test_the_rendered_env_file_arms_the_switch_carries_the_marker_and_is_readable():
    text = fleet.render_env_file()
    assert fleet.ENV_MARKER in text, "install must be able to recognise its own file"
    assert "%s=%s" % (session_host.FENCE_REQUIRED_VAR, session_host.FENCE_REQUIRED) in text
    # DoD: an operator can tell an armed machine from an unarmed one by reading it, so the value
    # is on a line of its own, in plain sight, and the file says what it is for.
    assert any(line.strip() == "%s=%s" % (session_host.FENCE_REQUIRED_VAR,
                                          session_host.FENCE_REQUIRED)
               for line in text.splitlines())
    assignments, problem, ignored = fleet.machine_env(text)
    assert problem is None and ignored == []
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}


def test_a_machine_the_build_up_never_touched_assigns_nothing():
    # The dev-workstation bullet, and the whole reason this is a FILE the build-up writes rather
    # than a default in the code: no build-up, no switch, no gate, no new refusal.
    assert fleet.machine_env(None, present=False) == ({}, None, [])
    probe = FakeProbe(home=_HOME)
    assert fleet.load_machine_env(probe, _PREFIX) == ({}, None, [])


def test_a_machine_may_be_taken_out_of_the_fenced_set_in_writing():
    # `off` exists for exactly this (session_host.fence_required): deleting the file disarms too,
    # but leaves nothing on disk that SAYS so — which is indistinguishable from never having built.
    assignments, problem, ignored = fleet.machine_env(
        "%s\n%s=off\n" % ("# " + fleet.ENV_MARKER, session_host.FENCE_REQUIRED_VAR))
    assert assignments == {session_host.FENCE_REQUIRED_VAR: "off"} and problem is None
    assert ignored == []


def test_a_file_that_names_no_switch_fails_closed_to_required():
    # The file exists only because the build-up ran here, so its PRESENCE is the machine's
    # declaration. A hand-edit that deleted the line must not read as "unfenced" — that is the
    # silently-disarmed-fence failure this issue exists to end, one layer up.
    assignments, problem, ignored = fleet.machine_env("# " + fleet.ENV_MARKER + "\n")
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem and "no" in problem.lower()


def test_a_present_but_empty_switch_is_not_a_disarm():
    # The #300 landmine shape: present-but-empty reads to a parser as nothing at all while reading
    # to a human as "set". `off` is the word that disarms; an empty value is a broken file.
    assignments, problem, _ = fleet.machine_env(
        "%s=\n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem


def test_a_file_that_cannot_be_read_fails_closed_to_required():
    probe = _Unreadable(files={fleet.env_file(_PREFIX): "unused"}, home=_HOME)
    assignments, problem, _ = fleet.load_machine_env(probe, _PREFIX)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem and "read" in problem


def test_a_reader_that_RAISES_is_the_same_fact_as_one_that_cannot_read():
    # The caller is a RUNNER'S BOOT (fresh-agent review, P0). `Probe.read_text` catches OSError
    # only, so an undecodable byte arrives here as a UnicodeDecodeError — and the file this very
    # build-up writes is full of em dashes, one editor round-trip away from exactly that. A
    # traceback out of this function is a runner that does not start, on every resurrect.
    class _Raises(FakeProbe):
        def read_text(self, path):
            raise UnicodeDecodeError("utf-8", b"\x92", 0, 1, "invalid start byte")
    probe = _Raises(files={fleet.env_file(_PREFIX): "unused"}, home=_HOME)
    assignments, problem, _ = fleet.load_machine_env(probe, _PREFIX)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem


def test_a_value_carrying_a_nul_is_refused_rather_than_handed_to_the_environment():
    # `os.environ` rejects an embedded NUL with a ValueError, and a crash-truncated or NUL-padded
    # file is exactly the shape that produces one. Fail closed, never raise.
    assignments, problem, _ = fleet.machine_env(
        "%s=requ\0ired\n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem
    env = {}
    fleet.apply_machine_env(env, assignments)          # the real environ would raise on a NUL
    assert "\0" not in env[session_host.FENCE_REQUIRED_VAR]


def test_presence_is_read_before_the_content_so_a_race_fails_closed():
    # Reading presence SECOND would let a file unlinked mid-call come back as "this machine
    # declares nothing" — a silent disarm out of a race (fresh-agent review).
    class _Vanishing(FakeProbe):
        def exists(self, path):
            return True
        def read_text(self, path):
            return None
    assignments, problem, _ = fleet.load_machine_env(_Vanishing(home=_HOME), _PREFIX)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem


def test_a_trailing_comment_on_the_value_line_is_a_comment():
    # The docs invite exactly this edit, and the file's own body is full of `#` lines — so
    # `SL_FLEET_FENCE=off  # taken out of the fleet` must disarm rather than parse as an
    # unrecognised value that fails closed to armed and ignores the operator (fresh-agent review).
    assignments, problem, _ = fleet.machine_env(
        "%s=off   # taken out of the fleet 2026-08-06\n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: "off"} and problem is None


def test_a_reinstall_keeps_a_value_the_machine_already_declared():
    # `--install` is a build-up step after a version bump, not a decision about this machine's
    # posture — and the file's own text offers `off` as THE supported edit. Stamping `required`
    # back over it would make that instruction false the first time anybody followed it.
    disarmed = fleet.render_env_file(session_host.FENCE_OFF)
    assert fleet.keep_declared_fence(disarmed) == session_host.FENCE_OFF
    assert session_host.FENCE_REQUIRED_VAR + "=off" in disarmed
    # ...but only a value this engine RECOGNISES. A typo is not preserved forever: the fail-closed
    # reading of one belongs to the refusal that names it, not to a file that carries it silently.
    assert fleet.keep_declared_fence("%s=requried\n" % session_host.FENCE_REQUIRED_VAR) \
        == session_host.FENCE_REQUIRED
    assert fleet.keep_declared_fence(None) == session_host.FENCE_REQUIRED
    assert fleet.keep_declared_fence("# nothing here\n") == session_host.FENCE_REQUIRED


def test_an_unrecognised_spelling_is_carried_verbatim_rather_than_repaired():
    # The switch's parsing belongs to session_host.fence_required (#326) and is not re-implemented
    # here: a typo must reach it intact so the launch refusal can NAME the value it refused on.
    assignments, _, _ = fleet.machine_env("%s=requried\n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: "requried"}
    assert session_host.fence_required(assignments) == (True, "requried")


def test_only_the_variables_this_build_up_declares_are_ever_applied():
    # An ALLOW-LIST, not a general env file. A machine-level file that could set anything would put
    # SL_ATTENDED, SL_RESUME_SESSION_ID and the rest of the launcher's contract on disk, where the
    # runner pins them empty precisely because an ambient value must never ride into a worker.
    assignments, _, ignored = fleet.machine_env(
        "SL_ATTENDED=1\n%s=required\nPATH=/evil\n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert sorted(ignored) == ["PATH", "SL_ATTENDED"]


def test_comments_blank_lines_and_shell_habits_are_read():
    assignments, problem, _ = fleet.machine_env(
        "# a comment\n\n   export %s = 'required'  \n" % session_host.FENCE_REQUIRED_VAR)
    assert assignments == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert problem is None


def test_the_machines_own_declaration_beats_an_ambient_variable():
    # The rule, and the reason for it: an `export SL_FLEET_FENCE=off` left in a shell rc file or a
    # LaunchAgent would otherwise silently disarm the fleet machine — the same inheritance hazard
    # the runner pins SL_ATTENDED empty for, and it would make the file unreadable as evidence.
    env = {session_host.FENCE_REQUIRED_VAR: "off", "HOME": _HOME}
    applied, replaced = fleet.apply_machine_env(
        env, {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED})
    assert env[session_host.FENCE_REQUIRED_VAR] == session_host.FENCE_REQUIRED
    assert applied == {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    assert replaced == {session_host.FENCE_REQUIRED_VAR: "off"}


def test_applying_nothing_leaves_an_unbuilt_machines_environment_exactly_as_it_was():
    env = {"HOME": _HOME}
    assert fleet.apply_machine_env(env, {}) == ({}, {})
    assert env == {"HOME": _HOME}


def _unarmed_probe(**kw):
    """A fully built machine whose runner nothing arms — the exact state #326 shipped into."""
    probe = _green_probe(**kw)
    probe.files.pop(fleet.env_file(_PREFIX), None)
    return probe


def test_the_launch_gate_block_is_red_on_a_machine_the_build_up_never_armed():
    r = fleet.check_launch_gate(_unarmed_probe(), _PREFIX)
    assert not r.ok
    assert fleet.env_file(_PREFIX) in r.detail
    assert "fleet --install" in (r.fix or "")


def test_the_launch_gate_block_is_green_when_the_machine_arms_it():
    r = fleet.check_launch_gate(_green_probe(), _PREFIX)
    assert r.ok and not r.warn
    assert session_host.FENCE_REQUIRED_VAR in r.detail and fleet.env_file(_PREFIX) in r.detail


def test_the_launch_gate_block_is_red_when_the_machine_disarms_itself():
    probe = _green_probe(files={fleet.env_file(_PREFIX): "%s=off\n"
                                % session_host.FENCE_REQUIRED_VAR})
    r = fleet.check_launch_gate(probe, _PREFIX)
    assert not r.ok and "off" in r.detail


def test_the_launch_gate_block_is_red_when_the_file_cannot_be_read():
    probe = _Unreadable(files={fleet.env_file(_PREFIX): "unused"}, home=_HOME)
    r = fleet.check_launch_gate(probe, _PREFIX)
    assert not r.ok
    # It still says which way a runner would fail, because the runner arms anyway — a red line that
    # left the operator guessing about the live posture would be worse than the file.
    assert session_host.FENCE_REQUIRED in r.detail


def test_the_launch_gate_block_says_an_ambient_variable_is_overridden_and_stays_green():
    probe = _green_probe(env={"HOME": _HOME, session_host.FENCE_REQUIRED_VAR: "off"})
    r = fleet.check_launch_gate(probe, _PREFIX)
    assert r.ok and r.warn, "the machine's file wins, so the gate IS armed — but say it out loud"
    assert "off" in r.detail


def test_the_launch_gate_block_warns_about_variables_it_will_not_apply():
    probe = _green_probe(files={fleet.env_file(_PREFIX):
                                fleet.render_env_file() + "SL_ATTENDED=1\n"})
    r = fleet.check_launch_gate(probe, _PREFIX)
    assert r.ok and r.warn and "SL_ATTENDED" in r.detail


def test_the_launch_gate_and_the_host_fence_are_two_different_facts():
    # The DoD's own words: a machine can have one without the other. A fenced socket with nothing
    # arming the launcher is the exact state #326 shipped into, and the report must not blur them.
    results = fleet.check_fleet(_unarmed_probe(), state_base=_STATE_BASE,
                                host_config_dir=_HOST_CONFIG_DIR,
                                fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                fence=lambda p: session_host.FENCED)
    by_name = {r.name: r for r in results}
    assert by_name["host fence"].ok and not by_name["launch gate"].ok
    # ...and the mirror image: armed launcher, open socket.
    results = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                                host_config_dir=_HOST_CONFIG_DIR,
                                fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                fence=lambda p: session_host.OPEN)
    by_name = {r.name: r for r in results}
    assert not by_name["host fence"].ok and by_name["launch gate"].ok


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


def test_a_job_that_runs_something_other_than_the_fleet_is_refused():
    # Every other block inspects the artefacts under the prefix. If the JOB runs a different
    # binary, names a different session, or reads a different token file, all of them are
    # describing files nothing uses — and the whole report goes green about a fleet that is not
    # the one running.
    good = _green_plist()
    for broken, why in (
            (good.replace(_BIN, "/opt/homebrew/bin/other"), "binary"),
            (good.replace("<string>%s</string>" % fleet.SESSION_NAME, "<string>other</string>"),
             "session"),
            # The VALUE of --session, not the word appearing somewhere in argv: this one binds the
            # DEFAULT session — the owner's own — which is the one thing the build-up must not share.
            (good.replace("<string>--session</string>\n        <string>%s</string>"
                          % fleet.SESSION_NAME,
                          "<string>--session</string>\n        <string>default</string>\n"
                          "        <string>%s</string>" % fleet.SESSION_NAME),
             "session value"),
            (good.replace(fleet.token_file(_PREFIX), "/somewhere/else/token"), "token file")):
        probe = _green_probe(files={fleet.server_plist_path(_HOME): broken})
        r = fleet.check_login_item(probe, uid=501, home=_HOME, fleet_prefix=_PREFIX)
        assert not r.ok, why
    assert fleet.check_login_item(_green_probe(), uid=501, home=_HOME,
                                  fleet_prefix=_PREFIX).ok
    # An unparseable plist is not a passing one.
    junk = _green_probe(files={fleet.server_plist_path(_HOME): "not a plist at all"})
    assert not fleet.check_login_item(junk, uid=501, home=_HOME, fleet_prefix=_PREFIX).ok


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


def test_an_override_the_host_would_refuse_or_never_apply_is_not_installed(monkeypatch):
    good = fleet.render_manifest_override('id = "claude"\nversion = "9"\n', source_version="9")
    # The host reads this with a real parser and refuses what will not parse, so where a parser is
    # available neither may this. Asserted FIRST, before the fallback is forced on below: the line
    # walker cannot see malformed TOML and does not claim to.
    if fleet.tomllib is not None:
        assert not fleet.has_catchall_rule(good + "\n[[[broken")
    for reader in ("parser", "line walker"):
        if reader == "line walker":
            monkeypatch.setattr(fleet, "tomllib", None)
        assert fleet.has_catchall_rule(good), reader
        # A rule with no way to match is a rule the host never applies — the same wrong `idle`
        # with a green line over it.
        assert not fleet.has_catchall_rule(good.replace("regex = ['(?s).*']", "")), reader
        assert not fleet.has_catchall_rule(good.replace('region = "whole_recent"', "")), reader
        assert not fleet.has_catchall_rule('# id = "%s"' % fleet.CATCHALL_RULE_ID), reader


def test_the_identity_block_names_the_seam_that_makes_it_load_bearing():
    # This line used to have to say what it did NOT prove: that a launch actually points a session
    # at this dir. #314 closed that gap — the launch path reads the same variable, derives the same
    # canonical string, and each session refuses itself unless the account under it is the expected
    # one — so the line now names the seam instead of apologising for its absence.
    r = fleet.check_identity(_green_probe(), _FLEET_CLAUDE_DIR, claude=_CLAUDE)
    assert r.ok and "#314" in r.detail
    assert fleet.identity.FLEET_DIR_VAR in r.detail
    assert "not that a launch uses it" not in r.detail


def test_the_build_up_judge_and_the_launch_seam_share_one_account_verdict():
    # The judge used to accept any `loggedIn: true` — and the real binary answers exactly that for
    # a session running on an API KEY, with null org and subscription. A build-up that green-lit
    # such a dir would be reporting a healthy identity for one that bills per token.
    on_key = {"loggedIn": True, "authMethod": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY",
              "email": None, "orgId": None, "subscriptionType": None}
    assert "API key" in fleet.identity_problem(on_key, {"orgId": "org-owner"})
    assert fleet.identity_problem(on_key, None)
    # One definition, not two copies: the seam's own canonicalisation rule IS this module's.
    assert fleet.config_dir_problem is fleet.identity.config_dir_problem


def test_a_token_this_build_up_cannot_vouch_for_is_not_adopted():
    # A fence whose secret somebody else chose is not a fence: a same-uid process that dropped its
    # own token at the path before the first install would have the server configured to accept a
    # secret it already knows, with every block green.
    minted = "s3cret\n"
    assert fleet.token_provenance(minted) == fleet.token_provenance("s3cret\n")
    assert fleet.token_provenance(minted) != fleet.token_provenance("someone-elses\n")
    # The sidecar holds a DIGEST, never a second copy of the thing the fence protects.
    assert minted.strip() not in fleet.token_provenance(minted)
    assert fleet.token_provenance_file(_PREFIX) != fleet.token_file(_PREFIX)
    # And the honest limit, asserted so a later reader cannot overclaim it: the record is a plain
    # digest of the token, so anything that can write one half can write the other. It refuses an
    # unvouched or CHANGED token; it does not authenticate one against a same-uid process. That
    # question is #342, and nothing file-based answers it on a shared UNIX account.
    assert fleet.token_provenance("anything") == fleet.token_provenance("anything")
    assert "not authentication" in fleet.token_provenance_file.__doc__
    assert "#342" in fleet.token_provenance_file.__doc__


def test_the_isolation_check_refuses_a_readable_fence_token():
    loose = _green_probe()
    loose.modes[fleet.token_file(_PREFIX)] = 0o644
    r = fleet.check_isolation(loose, _PREFIX, _HOST_CONFIG_DIR)
    assert not r.ok and "644" in r.detail
    # A token that cannot be stat-ed at all is not a token whose permissions are fine.
    r = fleet.check_isolation(EnvProbe(home=_HOME), _PREFIX, _HOST_CONFIG_DIR)
    assert not r.ok and "unknown" in r.detail
    # ...and a symlink there is not a permissions question with a reassuring answer: `Probe.mode`
    # lstats and answers only for regular files, so the judge cannot report the mode of a file the
    # fleet never minted.
    linked = _green_probe()
    linked.modes[fleet.token_file(_PREFIX)] = None
    monkey = fleet.check_isolation(linked, _PREFIX, _HOST_CONFIG_DIR)
    assert not monkey.ok and "not a regular file" in monkey.detail


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
                                fence=lambda p: session_host.FENCED,
                                capture=lambda p: session_host.ADMITTED)
    names = [r.name for r in results]
    assert names == list(dict.fromkeys(names)), "duplicate block names: %s" % names
    for expected in ("host binary", "host login item", "host fence", "host state capture",
                     "launch gate", "host config", "screen fallback", "claude binary",
                     "fleet identity", "fleet isolation"):
        assert expected in names, "%s missing from %s" % (expected, names)
    # The gate is downstream of the socket it guards: a reader who fixes an open socket first is
    # not then told to go and arm a launcher against a fence that was not there.
    assert names.index("host fence") < names.index("launch gate")
    # The allowance sits with the fence it is a hole in, and after it: "is anything refused" is
    # upstream of "is the one ruled method admitted", and an operator reading down the report
    # should meet them in that order.
    assert names.index("host fence") < names.index("host state capture") < names.index("launch gate")
    # The binary pin comes BEFORE the identity read for the same reason it does in doctor --stack:
    # which claude is in use is upstream of which account it is logged into.
    assert names.index("claude binary") < names.index("fleet identity")


def test_check_fleet_is_green_on_a_built_machine_and_red_on_an_open_socket():
    green = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                              host_config_dir=_HOST_CONFIG_DIR,
                              fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                              fence=lambda p: session_host.FENCED,
                              capture=lambda p: session_host.ADMITTED)
    assert [r.name for r in green if not r.ok] == []
    open_socket = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                                    host_config_dir=_HOST_CONFIG_DIR,
                                    fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                    fence=lambda p: session_host.OPEN,
                                    capture=lambda p: session_host.ADMITTED)
    # Both lines go red, and they are not one line reported twice: the fence is down AND the
    # allowance is unproven, because a socket that serves everyone proves nothing about a hole.
    assert [r.name for r in open_socket if not r.ok] == ["host fence", "host state capture"]


def test_the_judge_fails_a_perfectly_built_machine_whose_socket_refuses_the_state_report():
    """This issue in one test: `host binary` is green — the installed file reports the pin and
    carries the fence's compiled-in refusal string, which is true of BOTH generations — while the
    live socket says this host captures no session ids. Before this block, that machine printed
    `fleet build-up complete`."""
    results = fleet.check_fleet(_green_probe(), state_base=_STATE_BASE,
                                host_config_dir=_HOST_CONFIG_DIR,
                                fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                                fence=lambda p: session_host.FENCED,
                                capture=lambda p: session_host.REFUSED)
    by_name = {r.name: r for r in results}
    assert by_name["host binary"].ok and by_name["host fence"].ok
    assert not by_name["host state capture"].ok
    assert [r.name for r in results if not r.ok] == ["host state capture"]


def test_the_judge_asks_the_capture_question_of_the_socket_the_fence_block_judged():
    """One socket, both questions. A capture block that resolved its own path (from the
    environment, say) could report on the owner's default session while `host fence` reported on
    the fleet's — two machines in one report, and neither reader would know."""
    asked = []
    fleet.check_fleet(_green_probe(), state_base=_STATE_BASE, host_config_dir=_HOST_CONFIG_DIR,
                      fleet_config_dir=_FLEET_CLAUDE_DIR, uid=501, home=_HOME,
                      fence=lambda p: (asked.append(("fence", p)), session_host.FENCED)[1],
                      capture=lambda p: (asked.append(("capture", p)), session_host.ADMITTED)[1])
    paths = {p for _who, p in asked}
    assert paths == {fleet.socket_path(_HOST_CONFIG_DIR)}, asked
    assert ("capture", fleet.socket_path(_HOST_CONFIG_DIR)) in asked
