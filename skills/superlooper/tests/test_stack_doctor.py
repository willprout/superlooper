import json
from types import SimpleNamespace

import notify
import stack_doctor


class FakeProbe:
    def __init__(self, commands=None, files=None, env=None, home="/home/will"):
        self.commands = commands or {}
        self.files = files or {}
        self.env = env or {}
        self.home = home
        self.calls = []

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
    config = {"notify": {"cmd": None, "imessage_to": None}}

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
