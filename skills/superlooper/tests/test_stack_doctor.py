import json
import os
from types import SimpleNamespace

import config as config_lib
import notify
import stack_doctor


class FakeProbe:
    def __init__(self, commands=None, files=None, env=None, home="/home/will", alive_pids=None):
        self.commands = commands or {}
        self.files = files or {}
        self.env = env or {}
        self.home = home
        self.alive_pids = set(alive_pids or [])
        self.calls = []

    def pid_alive(self, pid):
        return pid in self.alive_pids

    def command(self, name, envvar=None, default=None):
        if envvar and self.env.get(envvar):
            return self.env[envvar]
        if name in self.commands:
            return self.commands[name].get("path", name)
        return default if default and default in self.commands else None

    def run(self, argv, timeout=10):
        self.calls.append(list(argv))
        spec = self.commands.get(argv[0])
        if spec is None:
            spec = next((v for v in self.commands.values() if v.get("path") == argv[0]), None)
        if spec is None:
            return SimpleNamespace(returncode=127, stdout="", stderr="")
        key = tuple(argv[1:])
        rc, out, err = spec.get(key, (127, "", "unexpected command"))
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def exists(self, path):
        return path in self.files or path in {v.get("path") for v in self.commands.values()}

    def read_text(self, path):
        if path not in self.files:
            return None
        return self.files[path]

    def expanduser(self, path):
        return path.replace("~", self.home, 1) if path.startswith("~") else path


def _plugin_row(plugin_id="superlooper@superlooper", enabled=True, scope="user"):
    """One row of `claude plugin list --json`, shaped exactly as the real CLI emits it (verified
    against Claude Code's own output on 2026-07-15): the marketplace-qualified id, the enable flag,
    and the scope the plugin was installed at."""
    return {"id": plugin_id, "version": "1.0.0", "scope": scope, "enabled": enabled,
            "installPath": "/home/will/.claude/plugins/cache/superlooper/superlooper/1.0.0",
            "installedAt": "2026-07-14T00:00:00.000Z", "lastUpdated": "2026-07-14T00:00:00.000Z"}


def _healthy_probe():
    return FakeProbe(
        commands={
            "codex": {
                "path": "/bin/codex",
                ("login", "status"): (0, "Logged in using ChatGPT\n", ""),
            },
            "claude": {
                "path": "/bin/claude",
                ("auth", "status", "--json"): (
                    0,
                    json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
                    "",
                ),
                ("plugin", "list", "--json"): (
                    0,
                    json.dumps([_plugin_row()]),
                    "",
                ),
            },
            "gh": {
                "path": "/bin/gh",
                ("auth", "status", "--active", "--hostname", "github.com"): (
                    0, "Logged in\n", ""
                ),
                ("api", "rate_limit"): (
                    0,
                    json.dumps({"resources": {"core": {"limit": 5000, "remaining": 4999}}}),
                    "",
                ),
            },
            "/Applications/cmux.app/Contents/Resources/bin/cmux": {
                "path": "/Applications/cmux.app/Contents/Resources/bin/cmux",
            },
            "defaults": {
                "path": "defaults",
                ("read", "com.cmuxterm.app", "NSAppSleepDisabled"): (0, "1\n", ""),
            },
        },
        files={
            "/home/will/.superlooper/launch-shim.zsh": "# shim\n",
            "/home/will/.zshrc": 'source "$HOME/.superlooper/launch-shim.zsh"\n',
        },
    )


def _ok_sender(channel="cmd"):
    """A fake notify.send_test that reports a delivered send without touching a subprocess."""
    def _send(config, title, body):
        return notify.SendResult(channel, True, 0, "")
    return _send


def test_stack_doctor_all_checks_pass_with_injected_probe():
    config = {"notify": {"cmd": "printf '%s\n' \"$SL_TITLE\"", "imessage_to": None}}
    results = stack_doctor.check_stack(
        config, probe=_healthy_probe(), sender=_ok_sender(), announce=lambda *a: None,
    )

    assert [(r.name, r.ok) for r in results] == [
        ("codex CLI", True),
        ("cmux present", True),
        ("claude login", True),
        ("gh auth", True),
        ("gh API headroom", True),
        ("notify channel", True),
        ("launch shim sourced", True),
        ("cmux App Nap disabled", True),
        ("runner anchor (live)", True),      # no repo in this config -> cleanly skipped, passes
        ("installed engine current", True),  # no VERSION stamp injected -> cleanly skipped, passes
        ("superlooper plugin", True),        # installed + enabled in the healthy probe
    ]


def test_stack_doctor_failures_carry_one_line_fix_hints():
    probe = _healthy_probe()
    del probe.commands["codex"]
    probe.commands["gh"][("api", "rate_limit")] = (
        0,
        json.dumps({"resources": {"core": {"limit": 5000, "remaining": 12}}}),
        "",
    )
    probe.files["/home/will/.zshrc"] = "# no shim\n"
    # agent: codex makes the missing Codex a real FAIL (this machine launches Codex workers), so
    # this test exercises the fix-hint text on a genuine codex failure. On a Claude-only machine
    # the same absence is a WARN — see test_codex_absent_and_not_required_warns_stack_still_passes.
    config = {"agent": "codex", "notify": {"cmd": None, "imessage_to": None}}

    results = stack_doctor.check_stack(config, probe=probe)
    failures = {r.name: r for r in results if not r.ok}

    assert "codex CLI" in failures
    assert "Install the Codex CLI" in failures["codex CLI"].fix
    assert "gh API headroom" in failures
    assert "Wait for the hourly GitHub API quota" in failures["gh API headroom"].fix
    assert "notify channel" in failures
    assert ".superlooper/config.json" in failures["notify channel"].fix
    assert "launch shim sourced" in failures
    assert "install-launch-shim.sh" in failures["launch shim sourced"].fix


# --- codex CLI: a WARN, not a FAIL, unless THIS machine actually runs Codex (issue #30) ---------
# STACK.md tiers Codex as an orchestrator (Tier 2) tool, and the repo-level doctor already treats
# a missing Codex as a WARN "needed only for --agent codex". The stack doctor used to hard-fail on
# it regardless, blocking a Claude-only newcomer from ever reaching an all-green stack. Owner ruling
# 2026-07-10: an independent same-model fresh subagent is a valid review path, so Codex absence must
# fail the stack ONLY when a repo's config selects `agent: codex`.

def test_codex_required_is_true_only_when_config_selects_the_codex_agent():
    assert stack_doctor._codex_required({"agent": "codex"}) is True
    assert stack_doctor._codex_required({"agent": "claude"}) is False
    assert stack_doctor._codex_required({}) is False          # default agent is claude
    assert stack_doctor._codex_required(None) is False         # unreadable config never forces it


def test_codex_absent_and_not_required_warns_stack_still_passes():
    probe = _healthy_probe()
    del probe.commands["codex"]                                # Codex CLI not installed
    config = {"agent": "claude", "notify": {"cmd": "true", "imessage_to": None}}

    results = stack_doctor.check_stack(
        config, probe=probe, sender=_ok_sender(), announce=lambda *a: None,
    )
    codex = next(r for r in results if r.name == "codex CLI")

    assert codex.warn is True                                  # advisory, not a failure
    assert codex.ok is True                                    # a WARN does not fail the stack
    assert [r.name for r in results if not r.ok] == []         # overall stack PASSES
    # the WARN explains WHY it is optional and names the same-model review path
    assert "not found" in codex.detail.lower()
    assert "codex" in codex.detail.lower()
    # and it renders as a WARN line, not ok/FAIL
    line = next(l for l in stack_doctor.format_results(results) if "codex CLI" in l)
    assert line.strip().startswith("WARN")


def test_codex_absent_but_required_by_codex_agent_fails_as_before():
    probe = _healthy_probe()
    del probe.commands["codex"]
    config = {"agent": "codex", "notify": {"cmd": "true", "imessage_to": None}}

    results = stack_doctor.check_stack(
        config, probe=probe, sender=_ok_sender(), announce=lambda *a: None,
    )
    codex = next(r for r in results if r.name == "codex CLI")

    assert codex.ok is False                                   # hard FAIL: this machine needs Codex
    assert codex.warn is False
    assert "Install the Codex CLI" in codex.fix
    assert "codex CLI" in [r.name for r in results if not r.ok]


def test_codex_present_but_unauthenticated_warns_when_not_required():
    probe = _healthy_probe()
    probe.commands["codex"][("login", "status")] = (1, "", "Not logged in")

    result = stack_doctor.check_codex(probe, required=False)

    assert result.ok is True                                   # not needed here -> does not fail
    assert result.warn is True
    assert "codex" in result.detail.lower()


def test_codex_present_but_unauthenticated_fails_when_required():
    probe = _healthy_probe()
    probe.commands["codex"][("login", "status")] = (1, "", "Not logged in")

    result = stack_doctor.check_codex(probe, required=True)

    assert result.ok is False
    assert result.warn is False
    assert "codex login" in result.fix


def test_format_results_renders_a_warn_label():
    warn = stack_doctor.CheckResult("codex CLI", True, "codex not found", warn=True)
    line = stack_doctor.format_results([warn])[0]
    assert line.strip().startswith("WARN")
    assert "codex CLI" in line


def test_format_results_renders_a_malformed_warn_as_fail_not_warn():
    # Defensive: a WARN must always pass (warn ⇒ ok). A malformed warn+not-ok result would land in
    # cmd_stack_doctor's failures (not r.ok) yet must not print a reassuring WARN — the label and
    # the exit code stay in agreement, so the render layer downgrades it to FAIL.
    bad = stack_doctor.CheckResult("codex CLI", False, "boom", warn=True)
    line = stack_doctor.format_results([bad])[0]
    assert line.strip().startswith("FAIL")


def test_claude_api_key_auth_does_not_satisfy_subscription_login():
    probe = _healthy_probe()
    probe.commands["claude"][("auth", "status", "--json")] = (
        0,
        json.dumps({"loggedIn": True, "authMethod": "apiKey"}),
        "",
    )

    # inject a fake sender: this test is about the claude block, and check_notify now performs a
    # REAL send for a configured cmd — an un-stubbed "ntfy" would reach a live push binary on any
    # machine that has it (the 2026-07-03 fail-closed ratchet).
    results = stack_doctor.check_stack(
        {"notify": {"cmd": "ntfy", "imessage_to": None}}, probe=probe,
        sender=_ok_sender(), announce=lambda *a: None,
    )

    claude = next(r for r in results if r.name == "claude login")
    assert claude.ok is False
    assert "claude auth login" in claude.fix


def test_cmux_path_lookup_matches_runner_not_path_search():
    probe = _healthy_probe()
    del probe.commands["/Applications/cmux.app/Contents/Resources/bin/cmux"]
    probe.commands["cmux"] = {"path": "/usr/local/bin/cmux"}

    result = stack_doctor.check_cmux(probe)

    assert result.ok is False
    assert "/Applications/cmux.app" in result.detail
    assert "SL_CMUX" in result.fix


def test_launch_shim_filename_comment_does_not_count_as_sourced():
    probe = _healthy_probe()
    probe.files["/home/will/.zshrc"] = (
        "# source \"$HOME/.superlooper/launch-shim.zsh\"\n"
        "# launch-shim.zsh lives elsewhere\n"
    )

    result = stack_doctor.check_launch_shim(probe)

    assert result.ok is False
    assert "install-launch-shim.sh" in result.fix


# --- cmux App Nap: the launch-delivery-dies-40-min-after-you-walk-away killer (issue #120) ----
# With display/system sleep disabled, macOS App Nap is the one mechanism that suspends an idle,
# occluded cmux while the system stays awake — a napped cmux answers new-surface (returns a UUID)
# but defers spawning the tab's shell past the 30s verify window, so no worker starts (rc=2) and
# the systemic breaker trips ~40 min after the operator walks away. The cure is the persistent
# `NSAppSleepDisabled` default on the cmux bundle; the doctor must FAIL loudly when it is absent.

def test_app_nap_passes_when_nsappsleepdisabled_is_true():
    probe = _healthy_probe()

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is True and not getattr(result, "warn", False)
    assert ["defaults", "read", "com.cmuxterm.app", "NSAppSleepDisabled"] in probe.calls


def test_app_nap_fails_when_the_default_is_absent():
    probe = _healthy_probe()
    # Key/domain absent: `defaults read` exits 1 with a "does not exist" message on stderr.
    probe.commands["defaults"][("read", "com.cmuxterm.app", "NSAppSleepDisabled")] = (
        1, "", "The domain/default pair of (com.cmuxterm.app, NSAppSleepDisabled) does not exist\n")

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is False
    assert "NSAppSleepDisabled" in result.fix and "-bool true" in result.fix
    assert "cmux" in result.fix.lower() and ("restart" in result.fix.lower()
                                             or "relaunch" in result.fix.lower()
                                             or "quit" in result.fix.lower())


def test_app_nap_fails_when_the_default_is_explicitly_false():
    probe = _healthy_probe()
    probe.commands["defaults"][("read", "com.cmuxterm.app", "NSAppSleepDisabled")] = (0, "0\n", "")

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is False
    assert "-bool true" in result.fix


def test_app_nap_honors_a_bundle_id_override():
    probe = _healthy_probe()
    probe.env["SL_CMUX_BUNDLE_ID"] = "com.example.other"
    probe.commands["defaults"][("read", "com.example.other", "NSAppSleepDisabled")] = (0, "1\n", "")

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is True
    assert ["defaults", "read", "com.example.other", "NSAppSleepDisabled"] in probe.calls


def test_app_nap_warns_but_does_not_fail_when_defaults_is_unavailable():
    # No `defaults` binary resolvable (an unusual, non-macOS-ish env): the hazard cannot be read,
    # so this must WARN (pass) rather than FAIL — we never fail the stack on an undeterminable state.
    probe = _healthy_probe()
    del probe.commands["defaults"]

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is True
    assert getattr(result, "warn", False) is True


def test_app_nap_warns_when_the_read_errors_rather_than_reporting_absent():
    # `defaults` resolves but the read fails to execute (rc 127 from Probe on an OSError) — that is
    # NOT the documented "does not exist" (rc 1), so it must WARN (can't determine), never FAIL.
    probe = _healthy_probe()
    probe.commands["defaults"][("read", "com.cmuxterm.app", "NSAppSleepDisabled")] = (
        127, "", "could not exec")

    result = stack_doctor.check_cmux_app_nap(probe)

    assert result.ok is True
    assert getattr(result, "warn", False) is True


# --- notify channel: verified by a real test send, not just "is it set" (issue #25) ---------
# The live 2026-07-10 failure: notify.cmd was SET but every send exited 2 (recipient file gone),
# yet the doctor passed the block because it only checked configuration. The check now sends one
# real message through the configured path and FAILs the block on a nonzero send, carrying rc +
# the stderr tail so the reason is on the FAIL line.

def test_notify_check_sends_one_test_message_and_passes_on_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    marker = tmp_path / "delivered.txt"
    config = {"notify": {"imessage_to": None,
                         "cmd": f'printf "%s" "$SL_BODY" > {marker}'}}   # real send, exits 0
    announced = []

    result = stack_doctor.check_notify(config, announce=announced.append)

    assert result.ok is True
    assert "cmd" in result.detail
    assert marker.exists()                              # a message really went through the path
    # it announced the side effect BEFORE sending, naming the channel and the message text
    joined = "\n".join(announced)
    assert "cmd" in joined and "test" in joined.lower()


def test_notify_check_fails_carrying_rc_and_stderr_of_a_failed_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    config = {"notify": {"imessage_to": None,
                         "cmd": 'printf "recipient file missing\\n" 1>&2; exit 2'}}

    result = stack_doctor.check_notify(config, announce=lambda *a: None)

    assert result.ok is False
    assert "rc=2" in result.detail
    assert "recipient file missing" in result.detail    # the actual reason, on the FAIL line
    assert result.fix                                   # an actionable hint is still present


def test_notify_check_unconfigured_still_fails_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_CMUX", str(tmp_path / "no-cmux"))
    calls = []

    def _recording_sender(config, title, body):
        calls.append((title, body))
        return notify.SendResult("cmd", True, 0, "")

    announced = []
    result = stack_doctor.check_notify(
        {"notify": {"cmd": None, "imessage_to": None}},
        sender=_recording_sender, announce=announced.append,
    )

    assert result.ok is False
    assert "notify.cmd and notify.imessage_to are empty" in result.detail
    assert ".superlooper/config.json" in result.fix
    assert calls == []                                  # no test message sent when unconfigured
    assert announced == []                              # and nothing announced


def test_notify_check_announces_before_it_sends(tmp_path):
    events = []

    def _recording_sender(config, title, body):
        events.append("send")
        return notify.SendResult("cmd", True, 0, "")

    def _recording_announce(msg):
        events.append("announce")

    stack_doctor.check_notify(
        {"notify": {"cmd": "true", "imessage_to": None}},
        sender=_recording_sender, announce=_recording_announce,
    )

    assert events == ["announce", "send"]               # announced, THEN sent — never the reverse


# --- runner anchor (live): a LIVE runner's recorded launch anchor must still resolve (issue #33) ---
# The 2026-07-09 misplacement: a runner's cmux tab was dragged to another window; its pane stopped
# resolving and every worker launch parked. This is the cheap, read-only after-the-fact catch — it
# fires ONLY when a runner is actually live (pidfile pid alive), and re-runs the same probe the
# startup preflight uses. Not-live / no-anchor are skips, never a FAIL.

_ANCHOR_CMUX = "/bin/cmux-anchor"


def _anchor_state_dir(tmp_path, monkeypatch, repo="o/r"):
    monkeypatch.setenv("SL_HOME", str(tmp_path))
    return os.path.join(str(config_lib.state_home({"repo": repo})), "state")


def _anchor_probe(state, *, pid, alive, anchor=None, resolves=True):
    files = {os.path.join(state, "runner.lock"): str(pid)}
    if anchor is not None:
        files[os.path.join(state, "runner.anchor.json")] = json.dumps(anchor)
    row = "  surface:1  superlooper tab\n" if resolves else "Error: not_found\n"
    pane = (anchor or {}).get("pane", "PANE-UUID")
    ws = (anchor or {}).get("workspace")
    # The probe is workspace-scoped: the command only "resolves" when queried with the recorded
    # --pane AND --workspace, exactly as the check must call it (cmux scopes pane resolution to a
    # workspace). A call missing the recorded workspace would miss this key and read as not_found.
    key = ("list-pane-surfaces", "--pane", pane) + (("--workspace", ws) if ws else ())
    return FakeProbe(
        commands={_ANCHOR_CMUX: {"path": _ANCHOR_CMUX, key: (0, row, "")}},
        files=files, env={"SL_CMUX": _ANCHOR_CMUX},
        alive_pids=[pid] if alive else [],
    )


def test_runner_anchor_skips_when_no_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path))
    probe = FakeProbe(env={"SL_CMUX": _ANCHOR_CMUX})     # no runner.lock file at all
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True and r.warn is False and "no live runner" in r.detail.lower()


def test_runner_anchor_skips_when_pidfile_is_stale(tmp_path, monkeypatch):
    state = _anchor_state_dir(tmp_path, monkeypatch)
    probe = _anchor_probe(state, pid=4242, alive=False, anchor={"pane": "PANE-UUID"})
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True and r.warn is False and "no live runner" in r.detail.lower()
    assert probe.calls == []                              # never probed cmux — nothing is live


def test_runner_anchor_passes_when_live_anchor_resolves(tmp_path, monkeypatch):
    state = _anchor_state_dir(tmp_path, monkeypatch)
    anchor = {"pane": "PANE-UUID", "workspace": "WS-7", "window": "WIN-7", "pid": 4242}
    probe = _anchor_probe(state, pid=4242, alive=True, anchor=anchor, resolves=True)
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True and r.warn is False
    assert "resolves" in r.detail and "WS-7" in r.detail and "WIN-7" in r.detail


def test_runner_anchor_fails_when_live_anchor_no_longer_resolves(tmp_path, monkeypatch):
    state = _anchor_state_dir(tmp_path, monkeypatch)
    anchor = {"pane": "PANE-UUID", "workspace": "WS-7", "window": "WIN-7", "pid": 4242}
    probe = _anchor_probe(state, pid=4242, alive=True, anchor=anchor, resolves=False)
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is False
    assert "PANE-UUID" in r.detail
    assert "superlooper run" in r.fix and "runner-ops" in r.fix


def test_runner_anchor_warns_when_live_runner_recorded_no_anchor(tmp_path, monkeypatch):
    state = _anchor_state_dir(tmp_path, monkeypatch)
    probe = _anchor_probe(state, pid=4242, alive=True, anchor=None)   # live pid, no anchor file
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True and r.warn is True
    assert "no" in r.detail.lower() and "anchor" in r.detail.lower()
    assert probe.calls == []                              # nothing to probe


def test_runner_anchor_skipped_without_repo_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SL_HOME", str(tmp_path))
    probe = FakeProbe(env={"SL_CMUX": _ANCHOR_CMUX})
    for cfg in (None, {}, {"notify": {}}):
        r = stack_doctor.check_runner_anchor(probe, cfg)
        assert r.ok is True and r.warn is False and "skip" in r.detail.lower()


def test_runner_anchor_probe_is_scoped_to_the_recorded_workspace(tmp_path, monkeypatch):
    # P0 regression: cmux resolves --pane within the CALLER's workspace by default, and doctor runs
    # from a different tab than the foreground runner — so the probe MUST pass the runner's recorded
    # --workspace, or it false-FAILs a healthy runner. Assert the workspace rides on the cmux call.
    state = _anchor_state_dir(tmp_path, monkeypatch)
    anchor = {"pane": "PANE-UUID", "workspace": "WS-7", "window": "WIN-7", "pid": 4242}
    probe = _anchor_probe(state, pid=4242, alive=True, anchor=anchor, resolves=True)
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True
    probe_call = next(c for c in probe.calls if "list-pane-surfaces" in c)
    assert "--workspace" in probe_call and "WS-7" in probe_call
    assert "--pane" in probe_call and "PANE-UUID" in probe_call


def test_runner_anchor_warns_when_recorded_pid_does_not_match_the_live_pid(tmp_path, monkeypatch):
    # A hard-crashed runner leaves a stale anchor; if the OS recycles its pid, the pidfile reads
    # "alive" for an unrelated process. The recorded pid must match the live pid, or we'd FAIL on a
    # dead runner's record. Mismatch -> WARN (no matching anchor), never a cmux probe.
    state = _anchor_state_dir(tmp_path, monkeypatch)
    anchor = {"pane": "PANE-UUID", "workspace": "WS-7", "window": "WIN-7", "pid": 111}  # != 4242
    probe = _anchor_probe(state, pid=4242, alive=True, anchor=anchor, resolves=True)
    r = stack_doctor.check_runner_anchor(probe, {"repo": "o/r"})
    assert r.ok is True and r.warn is True and "no matching anchor" in r.detail.lower()
    assert probe.calls == []


# --- installed-engine publish drift (issue #39) ------------------------------------------------
# The running loop executes the INSTALLED engine (~/.claude/skills/superlooper), not this repo, so a
# merged engine change is inert until someone republishes through the gated bin/install.sh — BY
# DESIGN. What was missing (observed 2026-07-10) is VISIBILITY: the installed copy sat six merged
# engine fixes behind main and no surface said so. This is a MACHINE-level fact (one installed copy
# per machine, shared by every adopted repo), so it lives in the machine-level --stack doctor. It is
# NEVER a FAIL — being behind is the design; it is a WARN at most. The git edge is behind Probe, so
# tests inject canned git output and never reach a real repo.

_SRC = "/src"                                          # a stand-in superlooper source checkout
_VERSION_PATH = "/home/will/.claude/skills/superlooper/VERSION"
_GIT = "/bin/git"


def _drift_probe(*, version="abc123 2026-07-01", behind=None, in_history=True,
                 refs=("origin/main",), toplevel=_SRC, has_payload=True, count_out=None,
                 count_rc=0, count_err="", candidate_is_git=True, env=None):
    """A FakeProbe with git stubbed for the drift walk: rev-parse --show-toplevel on the candidate,
    cat-file -e on the stamp, rev-parse --verify on each ref, rev-list --count for the chosen ref."""
    sha = version.split()[0] if version and version.split() else ""
    spec = {"path": _GIT}
    spec[("-C", _SRC, "rev-parse", "--show-toplevel")] = (
        (0, toplevel + "\n", "") if candidate_is_git else (128, "", "not a git repository"))
    spec[("-C", toplevel, "cat-file", "-e", sha + "^{commit}")] = (
        (0, "", "") if in_history else (128, "", "Not a valid object name"))
    # Only the refs in `refs` resolve (rc 0); any other ref the walk probes is left unstubbed, which
    # FakeProbe returns as rc 127 — i.e. "does not resolve" — so ref preference/fallback is exercised.
    for r in refs:
        spec[("-C", toplevel, "rev-parse", "--verify", "--quiet", r + "^{commit}")] = (
            0, r + "-sha\n", "")
        out = count_out if count_out is not None else ("" if behind is None else str(behind))
        spec[("-C", toplevel, "rev-list", "--count", sha + ".." + r, "--",
              "skills/superlooper/skill")] = (count_rc, out + "\n", count_err)
    files = {}
    if version is not None:
        files[_VERSION_PATH] = version
    if has_payload:
        files[toplevel + "/skills/superlooper/skill"] = ""
    return FakeProbe(commands={"git": spec}, files=files, env=env or {})


def test_engine_drift_reports_commit_distance_when_behind():
    probe = _drift_probe(version="abc123 2026-07-01", behind=6)
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "behind"
    assert d["behind"] == 6
    assert d["installed_sha"] == "abc123"
    assert d["ref"] == "origin/main"
    assert "6" in d["detail"] and "behind" in d["detail"].lower()
    assert "install.sh" in d["detail"]                       # names the gated publish step


def test_engine_drift_in_sync_when_zero_commits_behind():
    probe = _drift_probe(behind=0)
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "in_sync"
    assert d["behind"] == 0


def test_engine_drift_skips_when_no_version_stamp():
    probe = _drift_probe(version=None)                       # installed copy carries no VERSION
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "skipped"
    assert d["behind"] is None
    assert "stamp" in d["detail"].lower()


def test_engine_drift_skips_on_a_nogit_stamp():
    probe = _drift_probe(version="nogit 2026-07-01")         # published from a non-git payload
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "skipped"
    assert "nogit" in d["detail"].lower()


def test_engine_drift_skips_when_no_source_checkout_present():
    # A generic adopted repo (an eApp) is NOT a superlooper source tree — there is nothing to
    # compare against, so the check honestly skips rather than inventing a comparison.
    probe = _drift_probe(behind=6, has_payload=False)
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "skipped"
    assert "source checkout" in d["detail"].lower()


def test_engine_drift_unknown_when_stamp_not_in_history():
    # A rebased or unrelated history: the stamped commit is not reachable, so a rev-list distance is
    # meaningless. Surface it as an anomaly (WARN-worthy), never a silent green or a fabricated count.
    probe = _drift_probe(behind=6, in_history=False)
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "unknown"
    assert d["behind"] is None
    assert "history" in d["detail"].lower()


def test_engine_drift_unknown_when_git_count_errors():
    probe = _drift_probe(behind=6, count_rc=128, count_out="fatal")
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "unknown"
    assert d["behind"] is None


def test_engine_drift_ignores_stderr_advisories_when_the_count_is_valid():
    # git can print an advisory to stderr (e.g. an ambiguous refname) while still emitting the
    # count to stdout. The count parse must read stdout only — merging stderr in would fail
    # isdigit() and misreport a healthy behind/in-sync repo as an 'unknown' anomaly.
    probe = _drift_probe(behind=6, count_err="warning: refname 'main' is ambiguous.\n")
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["status"] == "behind"
    assert d["behind"] == 6


def test_engine_drift_prefers_origin_dev_ref_over_local_and_head():
    # The loop merges engine fixes to origin/<dev_branch>; a stale local branch would undercount.
    # origin/main must win the ref preference when it resolves.
    probe = _drift_probe(behind=6, refs=("origin/main", "main", "HEAD"))
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["ref"] == "origin/main"
    assert d["behind"] == 6


def test_engine_drift_falls_back_to_local_dev_ref_when_no_origin():
    probe = _drift_probe(behind=3, refs=("main", "HEAD"))
    d = stack_doctor.engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert d["ref"] == "main"
    assert d["behind"] == 3


def test_engine_drift_honors_sl_source_repo_override():
    # An operator whose source checkout is elsewhere (not the adopted repo_path) points at it via
    # SL_SOURCE_REPO. Here repo_path is a non-source directory; the override supplies the real tree.
    probe = _drift_probe(behind=2, env={"SL_SOURCE_REPO": _SRC})
    d = stack_doctor.engine_drift(probe, repo_path="/some/eapp", dev_branch="main")
    assert d["status"] == "behind"
    assert d["behind"] == 2


def test_engine_drift_never_raises_on_garbage_inputs():
    # Fail-closed like the rest of the doctor: a wrong-typed probe/args yield a structured skip,
    # never an exception that could take down doctor or the morning-report assembler.
    d = stack_doctor.engine_drift(FakeProbe(), repo_path=None, dev_branch="main")
    assert d["status"] in ("skipped", "unknown")
    assert isinstance(d["detail"], str) and d["detail"]


def test_check_engine_drift_warns_but_never_fails_when_behind():
    # Being behind is BY DESIGN (inert-until-republished). It must be a WARN — visible but never a
    # FAIL that would break an otherwise-healthy stack or pressure toward auto-republish.
    probe = _drift_probe(behind=6)
    r = stack_doctor.check_engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert r.ok is True and r.warn is True                   # WARN, not FAIL
    assert "6" in r.detail and "install.sh" in r.detail
    line = stack_doctor.format_results([r])[0]
    assert line.strip().startswith("WARN")


def test_check_engine_drift_plain_ok_when_in_sync():
    probe = _drift_probe(behind=0)
    r = stack_doctor.check_engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert r.ok is True and r.warn is False                  # a clean ok line, no WARN
    line = stack_doctor.format_results([r])[0]
    assert line.strip().startswith("ok")


def test_check_engine_drift_plain_ok_when_skipped():
    probe = _drift_probe(behind=6, has_payload=False)        # no source checkout -> skip
    r = stack_doctor.check_engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert r.ok is True and r.warn is False


def test_check_engine_drift_warns_on_measurement_anomaly():
    probe = _drift_probe(behind=6, in_history=False)         # stamp not in history -> unknown
    r = stack_doctor.check_engine_drift(probe, repo_path=_SRC, dev_branch="main")
    assert r.ok is True and r.warn is True


def test_check_stack_threads_repo_path_and_dev_branch_into_the_drift_row():
    # The stack list must carry the drift row, fed the caller's repo_path and the config's
    # dev_branch (so origin/<dev_branch> is the compared ref, not a hardcoded 'main').
    probe = _drift_probe(behind=4, refs=("origin/release",))
    results = stack_doctor.check_stack(
        {"dev_branch": "release", "notify": {"cmd": "true", "imessage_to": None}},
        probe=probe, sender=_ok_sender(), announce=lambda *a: None, repo_path=_SRC,
    )
    drift = next(r for r in results if r.name == "installed engine current")
    assert drift.warn is True and "4" in drift.detail and "origin/release" in drift.detail


# --- superlooper plugin presence (issue #90) ---------------------------------------------------
# After the plugin restructure (design D10), loop machines get their SKILL CONTENT from the
# superlooper plugin, not from the gated engine payload. A machine without it silently loses the
# ops / write-issue / debugger skills in planning and worker sessions — nothing errors, the sessions
# are just dumber. This block makes that absence visible. It is a WARN, NEVER a FAIL: the runner
# itself does not depend on the skills being installed (briefs are self-contained), so a missing
# plugin must not block an otherwise-healthy stack from passing.
#
# Truth surface: `claude plugin list --json`, the DOCUMENTED CLI (plugins reference → `plugin list`),
# which reports install AND enable state in one call. The registry file ~/.claude/plugins/
# installed_plugins.json is deliberately NOT read: it appears nowhere in the official plugin docs,
# so it is an internal implementation detail this check must not couple to.

_PLUGIN_LIST = ("plugin", "list", "--json")


def _plugin_probe(rows=None, *, rc=0, out=None, err="", has_claude=True, env=None):
    """A FakeProbe whose `claude plugin list --json` returns `rows` (or a raw `out`/`rc` for the
    malformed-output and error paths). `has_claude=False` removes the CLI entirely."""
    probe = _healthy_probe()
    probe.env.update(env or {})
    if not has_claude:
        del probe.commands["claude"]
        return probe
    payload = out if out is not None else json.dumps(rows or [])
    probe.commands["claude"][_PLUGIN_LIST] = (rc, payload, err)
    return probe


def test_superlooper_plugin_passes_when_installed_and_enabled():
    probe = _plugin_probe([_plugin_row()])

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is False              # a clean ok line, no advisory
    assert "superlooper@superlooper" in r.detail
    assert "user" in r.detail                            # names the scope it is installed at
    line = stack_doctor.format_results([r])[0]
    assert line.strip().startswith("ok")


def test_superlooper_plugin_warns_when_not_installed():
    # Another marketplace's plugin is installed, but not ours.
    probe = _plugin_probe([_plugin_row("superpowers@superpowers-marketplace")])

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True               # advisory — never fails the stack
    assert "not installed" in r.detail.lower()
    # the WARN carries the whole story inline (format_results prints `fix` only for a FAIL), so the
    # exact install path must be in `detail` or the operator is told a problem with no cure
    assert "plugin marketplace add willprout/superlooper" in r.detail
    assert "plugin install superlooper@superlooper" in r.detail
    line = stack_doctor.format_results([r])[0]
    assert line.strip().startswith("WARN")


def test_superlooper_plugin_warns_when_installed_but_disabled():
    probe = _plugin_probe([_plugin_row(enabled=False)])

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True
    assert "disabled" in r.detail.lower()
    assert "plugin enable superlooper@superlooper" in r.detail   # the cure for THIS state, not reinstall


def test_superlooper_plugin_does_not_claim_disabled_on_an_unreadable_enabled_flag():
    # P1 regression: DISABLED is claimed ONLY on a literal `enabled: false`. The CLI's --json schema
    # is undocumented, so a row that lacks the key (or carries an unexpected value) is a state we
    # could not read — asserting DISABLED there hands the operator a confident wrong diagnosis whose
    # cure (`plugin enable`) changes nothing. _plugin_rows already applies this discipline to the
    # list shape; it must not be abandoned one level down at the row.
    row_no_key = _plugin_row()
    del row_no_key["enabled"]
    for row in (row_no_key, _plugin_row(enabled="true"), _plugin_row(enabled=None)):
        r = stack_doctor.check_superlooper_plugin(_plugin_probe([row]))
        assert r.ok is True and r.warn is True, row
        assert "disabled" not in r.detail.lower(), row      # never a false DISABLED verdict
        assert "cannot tell" in r.detail.lower(), row       # an honest could-not-determine instead


def test_superlooper_plugin_passes_when_any_matching_row_is_enabled():
    # The same id can appear at more than one scope (user/project/local). Any enabled row means the
    # skills load, so a disabled row sorting FIRST must not be read as "disabled".
    probe = _plugin_probe([_plugin_row(enabled=False, scope="project"),
                           _plugin_row(enabled=True, scope="user")])

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is False
    assert "user" in r.detail                              # reports the row that actually loads


def test_superlooper_plugin_warns_when_empty_plugin_list():
    probe = _plugin_probe([])

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True
    assert "not installed" in r.detail.lower()


def test_superlooper_plugin_warns_when_claude_cli_is_absent():
    # No `claude` to ask: the state is undeterminable, so WARN — never a confident "not installed".
    probe = _plugin_probe(has_claude=False)

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True
    assert "not installed" not in r.detail.lower()       # must NOT assert absence it cannot know
    assert probe.calls == []                             # nothing to probe


def test_superlooper_plugin_warns_when_the_list_command_errors():
    # `claude plugin list --json` exits nonzero (an older CLI without the subcommand, a broken
    # install): undeterminable -> WARN, never a false "not installed".
    probe = _plugin_probe(rc=1, out="", err="unknown command 'plugin'")

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True
    assert "not installed" not in r.detail.lower()
    assert "could not" in r.detail.lower() or "unknown" in r.detail.lower()


def test_superlooper_plugin_warns_when_the_output_is_not_parseable_json():
    probe = _plugin_probe(out="not json at all")

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is True
    assert "not installed" not in r.detail.lower()


def test_superlooper_plugin_warns_when_the_output_shape_is_unexpected():
    # The CLI's --json schema is NOT documented (only the flag is), so a future shape change must
    # degrade to an honest WARN rather than crash the doctor or fabricate a verdict.
    for payload in (json.dumps({"plugins": []}), json.dumps(["a string"]), json.dumps(None)):
        probe = _plugin_probe(out=payload)
        r = stack_doctor.check_superlooper_plugin(probe)
        assert r.ok is True and r.warn is True, payload
        assert isinstance(r.detail, str) and r.detail


def test_superlooper_plugin_reads_the_documented_cli_not_the_internal_registry_file():
    # installed_plugins.json is an internal file the official plugin docs never mention. Couple to
    # the documented `plugin list --json` CLI instead, so a registry-format change cannot silently
    # turn this block into a liar. Assert BOTH: the CLI is called, the file is never read.
    probe = _plugin_probe([_plugin_row()])
    probe.files["/home/will/.claude/plugins/installed_plugins.json"] = json.dumps(
        {"version": 2, "plugins": {}})            # present and saying "nothing installed"
    reads = []
    inner = probe.read_text
    probe.read_text = lambda p: (reads.append(p), inner(p))[1]

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is False       # the CLI's truth wins; the file is irrelevant
    assert ["/bin/claude", "plugin", "list", "--json"] in probe.calls
    assert not any("installed_plugins" in p for p in reads)


def test_superlooper_plugin_honors_a_plugin_id_override():
    probe = _plugin_probe([_plugin_row("other@elsewhere")], env={"SL_PLUGIN_ID": "other@elsewhere"})

    r = stack_doctor.check_superlooper_plugin(probe)

    assert r.ok is True and r.warn is False
    assert "other@elsewhere" in r.detail


def test_superlooper_plugin_missing_never_fails_the_whole_stack():
    # The load-bearing guarantee: the runner never depends on the skills being installed (briefs are
    # self-contained), so a missing plugin must leave an otherwise-healthy stack green and exit 0.
    probe = _plugin_probe([])                     # plugin absent
    config = {"agent": "claude", "notify": {"cmd": "true", "imessage_to": None}}

    results = stack_doctor.check_stack(
        config, probe=probe, sender=_ok_sender(), announce=lambda *a: None,
    )
    plugin = next(r for r in results if r.name == "superlooper plugin")

    assert plugin.warn is True and plugin.ok is True
    assert [r.name for r in results if not r.ok] == []          # overall stack still PASSES
