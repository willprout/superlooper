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
        ("runner anchor (live)", True),      # no repo in this config -> cleanly skipped, passes
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
