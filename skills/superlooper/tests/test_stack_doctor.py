import json
from types import SimpleNamespace

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


def test_stack_doctor_all_checks_pass_with_injected_probe():
    config = {"notify": {"cmd": "printf '%s\n' \"$SL_TITLE\"", "imessage_to": None}}
    results = stack_doctor.check_stack(config, probe=_healthy_probe())

    assert [(r.name, r.ok) for r in results] == [
        ("codex CLI", True),
        ("cmux present", True),
        ("claude login", True),
        ("gh auth", True),
        ("gh API headroom", True),
        ("notify command configured", True),
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
    assert "notify command configured" in failures
    assert ".superlooper/config.json" in failures["notify command configured"].fix
    assert "launch shim sourced" in failures
    assert "install-launch-shim.sh" in failures["launch shim sourced"].fix


def test_claude_api_key_auth_does_not_satisfy_subscription_login():
    probe = _healthy_probe()
    probe.commands["claude"][("auth", "status", "--json")] = (
        0,
        json.dumps({"loggedIn": True, "authMethod": "apiKey"}),
        "",
    )

    results = stack_doctor.check_stack(
        {"notify": {"cmd": "ntfy", "imessage_to": None}}, probe=probe
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
