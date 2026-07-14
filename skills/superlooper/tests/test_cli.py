"""The `superlooper` CLI (plan Task 10): run / status / adopt / doctor + the Task-11/12 stubs.

Invoked as a real subprocess (argparse, exit codes, output — the William-facing contract).
Everything external is injected: fake-gh via SL_GH, the state base via SL_HOME, HOME pointed
at a tmp dir for the shim/hooks checks, a stub cmux via SL_CMUX, a stub jq on PATH.

The one plan-named hard requirement: doctor (and adopt's printout) FAIL HARD when
`required_checks` is empty — a repo with no CI check enforcing its tests has no mechanical
§4.3 gate, so adoption requires at least one (cross-review C3).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import loopstate

_ROOT = Path(__file__).resolve().parent.parent
CLI = _ROOT / "skill" / "bin" / "superlooper"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gh"
_FAKE_GH = Path(__file__).resolve().parent / "fakes" / "fake-gh"

ALL_LABELS = ["agent-ready", "in-progress", "needs-owner", "parked", "expedite",
              "preserve", "auto-approved:nightly-red", "superseded",
              "priority:high", "priority:low",
              "type:build", "type:investigate", "type:diagnose-and-fix",
              # per-issue model/effort control knobs — gh refuses to apply a label that does not
              # exist, so adopt must seed the starter set (owner ruling 2026-07-07).
              "model:opus", "model:opus[1m]", "model:fable",
              "effort:low", "effort:medium", "effort:high", "effort:xhigh", "effort:max"]

RULE_START = "<!-- loop-standing-rules:start -->"
RULE_END = "<!-- loop-standing-rules:end -->"
RULE_REQUIRED_SNIPPETS = [
    "Approval is the repo owner's word",
    "`agent-ready` is never applied by an agent",
    "Read the parked-issue memo before re-approving",
    "Reviews are performed by a fresh agent",
    "shared mutable defaults",
    "fail-open on wrong-typed input",
    "No metered or paid spend",
    "Never work in the loop's own checkout",
]


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "userhome"
    (home / ".superlooper").mkdir(parents=True)
    fixdir = tmp_path / "gh"
    shutil.copytree(_FIXTURES, fixdir)
    (fixdir / "label_list.json").write_text(json.dumps([{"name": n} for n in ALL_LABELS]))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for stub in ("jq",):
        p = bindir / stub
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / ".superlooper").mkdir(parents=True)
    # required_checks match the names the committed gh fixtures report (pr_list.json rollup +
    # check_runs.json), so the doctor's issue-#26 name cross-check passes on a healthy repo.
    (repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["review/local-gate", "quality-gate"]}))
    env = {**os.environ,
           "HOME": str(home), "SL_HOME": str(tmp_path / "slhome"),
           "SL_GH": str(_FAKE_GH), "GH_FIXTURES": str(fixdir),
           "SL_CMUX": "/bin/ls",
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    env.pop("GH_FAIL", None)
    # a healthy shim + hooks footprint (doctor checks these)
    (home / ".superlooper" / "launch-shim.zsh").write_text("# shim")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": "activity-hook.sh", "Stop": "stop-hook.sh"}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": "activity-hook.sh", "Stop": "stop-hook.sh"}}))
    return type("Rig", (), {"env": env, "repo": repo, "fixdir": fixdir,
                            "home": home, "tmp": tmp_path})


def cli(rig, *args, env_over=None, inp=None):
    env = {**rig.env, **(env_over or {})}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env, timeout=60, input=inp)


def mutations(rig):
    p = rig.fixdir / "mutations.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def standing_rules_block(text):
    start = text.index(RULE_START)
    end = text.index(RULE_END, start) + len(RULE_END)
    return text[start:end]


# --------------------------- doctor ---------------------------

def test_doctor_ok_when_everything_is_healthy(rig):
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "required_checks" in r.stdout


def test_doctor_fails_hard_on_empty_required_checks(rig):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": []}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "required_checks" in r.stdout + r.stderr
    # a repo with no CI check enforcing its tests has no mechanical gate: the message says why
    assert "check" in (r.stdout + r.stderr).lower()


def test_doctor_fails_on_invalid_config(rig):
    (rig.repo / ".superlooper" / "config.json").write_text("{not json")
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0


def test_doctor_fails_when_gh_is_unreachable(rig):
    r = cli(rig, "doctor", "--repo", str(rig.repo), env_over={"GH_FAIL": "1"})
    assert r.returncode != 0
    assert "gh" in (r.stdout + r.stderr).lower()


def test_doctor_fails_on_missing_labels(rig):
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": "agent-ready"}]))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "preserve" in r.stdout + r.stderr        # names what's missing


def test_doctor_fails_when_shim_is_missing(rig):
    (rig.home / ".superlooper" / "launch-shim.zsh").unlink()
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "shim" in (r.stdout + r.stderr).lower()


def test_doctor_fails_when_the_dev_branch_is_missing_on_origin(rig):
    # issue #28: the worktree base is origin/<dev_branch>. If that branch does not exist on the
    # remote, every launch dies at worktree creation. doctor must FAIL and NAME the branch, so the
    # cause is caught at adoption time, not chased through a "shim not installed?" park memo.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "dev_branch": "develop",
         "required_checks": ["review/local-gate", "quality-gate"]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo), env_over={"GH_MISSING_BRANCHES": "develop"})
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "develop" in out                          # the failure NAMES the missing branch
    assert "FAIL" in out


def test_doctor_passes_when_the_dev_branch_exists(rig):
    # the healthy repo's dev_branch (default "main") exists on origin -> the new check must pass.
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "main" in r.stdout and "dev_branch" in r.stdout.lower()


def test_doctor_warns_when_codex_hooks_are_missing(rig):
    (rig.home / ".codex" / "hooks.json").unlink()
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "WARN" in out
    assert "Codex activity hooks registered" in out
    assert "hooks.json" in out


# ------------- doctor: required_checks name cross-check (issue #26) -------------

def _set_checks(rig, checks):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": checks}))


def test_doctor_healthy_repo_passes_the_check_name_cross_check(rig):
    # the rig's required_checks match what the fixtures report on PRs and the dev branch
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr


def test_doctor_flags_a_typo_with_a_case_or_shape_hint(rig):
    # config says "Quality Gate" but the repo reports "quality-gate": a name it cannot find
    _set_checks(rig, ["Quality Gate"])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "Quality Gate" in out                       # names the offending config entry
    assert "quality-gate" in out                       # case/shape hint -> the real reported name


def test_doctor_fails_a_never_wired_required_check(rig):
    _set_checks(rig, ["nonexistent-check"])
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "nonexistent-check" in (r.stdout + r.stderr)


def test_doctor_flags_a_check_that_reports_on_prs_but_never_on_dev(rig):
    # the 2026-07-09 incident shape: reported on PRs (pr_list.json) but never on the dev branch.
    _set_checks(rig, ["quality-gate"])
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "review/local-gate", "status": "completed",
                         "conclusion": "success"}]}))   # dev reports OTHER checks, never quality-gate
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "quality-gate" in out and "dev" in out.lower()


def test_doctor_flags_a_check_that_reports_on_dev_but_never_on_prs(rig):
    # the mirror of pr-only: reported on the dev branch but never on recent PRs — every PR reads
    # pending forever, so the green PR never merges (Codex R1).
    _set_checks(rig, ["quality-gate"])
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 700, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "review/local-gate",
             "status": "COMPLETED", "conclusion": "SUCCESS"}]}]))   # PRs never report quality-gate
    # dev branch DOES report quality-gate
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "quality-gate", "status": "completed",
                         "conclusion": "success"}]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "quality-gate" in out and "PR" in out


def test_doctor_passes_a_pr_only_check_excluded_from_the_dev_set(rig):
    # issue #52: `ship` gates PR merges but never reports on the dev branch, and the config EXCLUDES
    # it from the dev set. That exclusion is exactly the fix — the doctor must NOT flag it (under the
    # old single-list model this was the 2026-07-09 pr_only FAIL).
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate"]}}))
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ship", "state": "SUCCESS"}]}]))
    # dev reports quality-gate (default check_runs.json) but NEVER ship
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ship" in r.stdout                      # shown in the required_checks display line


def test_doctor_flags_a_dev_required_check_that_never_reports_on_dev(rig):
    # the mis-split the doctor MUST still catch: `ship` is listed as dev-required but reports only on
    # PRs -> the dev-side poll reads pending forever, so a mainline freeze never lifts.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate", "ship"]}}))
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ship", "state": "SUCCESS"}]}]))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "ship" in out and "dev" in out.lower()


def test_doctor_flags_a_pr_required_check_that_reports_only_on_dev(rig):
    # split mirror of the dev-gap case: `ship` is PR-required but reports ONLY on the dev branch,
    # never on a PR -> every PR reads pending forever, so a green PR never merges. Must FAIL.
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r",
         "required_checks": {"pr": ["quality-gate", "ship"], "dev": ["quality-gate"]}}))
    # PRs report quality-gate only (never ship); the dev branch reports quality-gate + ship
    (rig.fixdir / "pr_list.json").write_text(json.dumps([{
        "number": 555, "state": "OPEN", "statusCheckRollup": [
            {"__typename": "StatusContext", "context": "quality-gate", "state": "SUCCESS"}]}]))
    (rig.fixdir / "check_runs.json").write_text(json.dumps(
        {"check_runs": [{"name": "quality-gate", "status": "completed", "conclusion": "success"},
                        {"name": "ship", "status": "completed", "conclusion": "success"}]}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "ship" in out and "PR" in out


def test_doctor_warns_when_no_checks_observed_yet(rig):
    # a freshly adopted repo with no CI history: cannot verify names -> WARN, never a hard FAIL.
    (rig.fixdir / "pr_list.json").write_text("[]")
    (rig.fixdir / "check_runs.json").write_text(json.dumps({"check_runs": []}))
    r = cli(rig, "doctor", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no checks observed" in (r.stdout + r.stderr).lower()


def _write_exe(path, body):
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _stack_env(rig, *, gh_remaining=4999):
    bindir = rig.tmp / "stack-bin"
    bindir.mkdir(exist_ok=True)
    codex = _write_exe(
        bindir / "codex",
        "#!/bin/sh\n"
        "if [ \"$1\" = login ] && [ \"$2\" = status ]; then\n"
        "  echo 'Logged in using ChatGPT'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    claude = _write_exe(
        bindir / "claude",
        "#!/bin/sh\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ] && [ \"$3\" = --json ]; then\n"
        "  printf '%s\\n' '{\"loggedIn\": true, \"authMethod\": \"claude.ai\"}'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    gh = _write_exe(
        bindir / "gh",
        "#!/bin/sh\n"
        "if [ \"$1\" = auth ] && [ \"$2\" = status ]; then exit 0; fi\n"
        "if [ \"$1\" = api ] && [ \"$2\" = rate_limit ]; then\n"
        f"  printf '%s\\n' '{{\"resources\": {{\"core\": {{\"limit\": 5000, \"remaining\": {gh_remaining}}}}}}}'; exit 0\n"
        "fi\n"
        "exit 64\n",
    )
    cmux = _write_exe(bindir / "cmux", "#!/bin/sh\nexit 0\n")
    # `defaults` MUST be stubbed too (issue #120): the stack doctor now runs
    # `defaults read com.cmuxterm.app NSAppSleepDisabled`, and cmd_stack_doctor builds a REAL Probe.
    # Without this stub the CLI doctor would read the host's real com.cmuxterm.app domain — reaching a
    # real external binary AND making the green-stack assertion depend on the host's actual cmux App
    # Nap setting. Report App Nap disabled (rc 0, "1") so the healthy stack is green everywhere.
    defaults = _write_exe(
        bindir / "defaults",
        "#!/bin/sh\n"
        "if [ \"$1\" = read ] && [ \"$3\" = NSAppSleepDisabled ]; then echo 1; exit 0; fi\n"
        "exit 1\n",
    )
    return {"SL_CODEX": codex, "SL_CLAUDE": claude, "SL_GH": gh, "SL_CMUX": cmux,
            "SL_DEFAULTS": defaults}


def test_doctor_stack_ok_uses_fake_commands_and_mutates_nothing(rig):
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    zshrc = rig.home / ".zshrc"
    zshrc.write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    watched = [cfg_path, zshrc, rig.home / ".superlooper" / "launch-shim.zsh"]
    before = {p: p.read_text() for p in watched}

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for name in ("codex CLI", "cmux present", "claude login", "gh auth",
                 "gh API headroom", "notify channel", "launch shim sourced",
                 "cmux App Nap disabled"):
        assert name in out
    assert "required_checks" not in out
    # the one deliberate side effect is announced before it fires
    assert "sending" in out.lower() and "test" in out.lower()
    assert {p: p.read_text() for p in watched} == before


def test_doctor_stack_flags_a_live_runner_whose_anchor_no_longer_resolves(rig):
    # End-to-end (issue #33): a LIVE runner (pidfile = a live pid) whose recorded pane no longer
    # resolves in cmux FAILs doctor --stack with the manual-restart hint. _stack_env's cmux prints
    # nothing for list-pane-surfaces, so the recorded pane reads as unresolvable — the misplacement.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    state = rig.tmp / "slhome" / "o__r" / "state"
    state.mkdir(parents=True)
    (state / "runner.lock").write_text(str(os.getpid()))          # this test process = a live pid
    (state / "runner.anchor.json").write_text(json.dumps(
        {"pane": "DEADPANE", "workspace": "WS-x", "window": "WIN-x", "pid": os.getpid()}))

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL runner anchor (live)" in out
    assert "DEADPANE" in out
    assert "superlooper run" in out and "runner-ops" in out


def test_doctor_stack_fails_with_actionable_hint(rig):
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": None, "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text("# no shim source\n")

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_stack_env(rig, gh_remaining=0))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL gh API headroom" in out
    assert "Fix: Wait for the hourly GitHub API quota" in out
    assert "FAIL notify channel" in out
    assert "Fix: Set notify.cmd or notify.imessage_to" in out
    assert "FAIL launch shim sourced" in out
    assert "Fix: Run" in out and "install-launch-shim.sh" in out


def test_doctor_stack_flags_missing_app_nap_default(rig):
    # Issue #120 end-to-end: when NSAppSleepDisabled is not set for the cmux bundle, `doctor --stack`
    # must FAIL loudly with the exact remedy — this is the machine that systemically loses launch
    # delivery ~40 min after the operator walks away. The `defaults` stub reports the key absent
    # (rc 1), exactly as the real binary does on an un-nap-proofed machine.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')
    env = _stack_env(rig)
    env["SL_DEFAULTS"] = _write_exe(rig.tmp / "stack-bin" / "defaults-absent",
                                    "#!/bin/sh\nexit 1\n")   # every read -> "does not exist"

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=env)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL cmux App Nap disabled" in out
    assert "defaults write com.cmuxterm.app NSAppSleepDisabled -bool true" in out
    assert any(w in out.lower() for w in ("relaunch", "restart", "quit"))


def _codexless_stack_env(rig, **kw):
    """A healthy stack env with Codex made unresolvable: SL_CODEX unset AND PATH narrowed so
    `shutil.which('codex')` misses too. codex/claude/gh/cmux/defaults all resolve via their SL_* env
    (SL_DEFAULTS keeps the App Nap read off the host's real com.cmuxterm.app domain), so the only
    unresolved `which` left is for codex; PATH keeps the rig's jq bin plus /usr/bin:/bin so notify's
    `bash -lc` still resolves, but no standard installer ever puts codex in those dirs — so 'Codex is
    absent' stays hermetic regardless of what the host machine has installed."""
    env = _stack_env(rig, **kw)
    del env["SL_CODEX"]
    env["PATH"] = f"{rig.tmp / 'bin'}:/usr/bin:/bin"
    return env


def test_doctor_stack_warns_but_passes_when_codex_absent_on_a_claude_machine(rig):
    # Issue #30: a Claude-only newcomer (config agent defaults to claude) with no Codex installed
    # must still reach an all-green stack. Codex absence is a WARN, not a FAIL, and the exit is 0.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))            # no "agent" key -> defaults to claude
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_codexless_stack_env(rig))

    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "WARN codex CLI" in out
    assert "FAIL" not in out
    assert "all stack checks passed" in out


def test_doctor_stack_fails_when_codex_absent_but_config_selects_codex_agent(rig):
    # Issue #30: the mirror case. A machine whose config runs `agent: codex` genuinely needs Codex,
    # so its absence is a hard FAIL with the install hint, exactly as before.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["agent"] = "codex"
    cfg["notify"] = {"cmd": "printf '%s\\n' \"$SL_TITLE\"", "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo),
            env_over=_codexless_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL codex CLI" in out
    assert "Install the Codex CLI" in out
    assert "STACK DOCTOR FAILED" in out


def test_doctor_stack_fails_when_the_notify_test_send_fails(rig):
    # The live 2026-07-10 incident, end to end: notify.cmd is SET but every send exits nonzero
    # (recipient file gone). The doctor must FAIL the block and print rc + the stderr reason,
    # instead of passing because a value was merely configured.
    cfg_path = rig.repo / ".superlooper" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["notify"] = {"cmd": 'printf "recipient file missing\\n" 1>&2; exit 2', "imessage_to": None}
    cfg_path.write_text(json.dumps(cfg))
    (rig.home / ".zshrc").write_text('source "$HOME/.superlooper/launch-shim.zsh"\n')

    r = cli(rig, "doctor", "--stack", "--repo", str(rig.repo), env_over=_stack_env(rig))

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "FAIL notify channel" in out
    assert "rc=2" in out
    assert "recipient file missing" in out          # the actual error rode onto the FAIL line


# --------------------------- adopt ---------------------------

def test_adopt_writes_config_creates_labels_and_prints_requirements(rig):
    fresh = rig.tmp / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["repo"] == "will/proj"               # detected from the origin remote
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert created == set(ALL_LABELS)
    out = r.stdout
    assert "branch protection" in out.lower()
    assert "required_checks" in out                  # the same at-least-one-check requirement


def test_adopt_yields_web_agnostic_report_sections(rig):
    # issue #57 DoD: a fresh adopt on a fixture repo must yield a section list a NON-WEB worker can
    # honestly satisfy — never the old "Browser evidence" demand that nudged-then-parked every
    # finished issue on a CLI/library/service repo. adopt copies the shipped template, so this is the
    # end-to-end proof that the honest default reaches a freshly adopted repo's config.
    fresh = rig.tmp / "fresh-sections"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["report_required_sections"] == ["Tests", "Review"]
    assert "Browser evidence" not in cfg["report_required_sections"]


def test_adopt_detects_and_writes_the_repo_default_branch(rig):
    # issue #28: on a master/develop repo, dev_branch left at the template's "main" makes every
    # worktree creation fail off origin/main. adopt must detect the repo's real default (via gh)
    # and write it as dev_branch.
    fresh = rig.tmp / "fresh-branch"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    (rig.fixdir / "repo_view.json").write_text(json.dumps({"defaultBranchRef": {"name": "trunk"}}))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["dev_branch"] == "trunk"              # detected default, not the template's "main"
    # the branch-protection printout names the DETECTED branch, not the template default
    assert "`trunk`" in r.stdout


def test_adopt_keeps_the_template_default_branch_when_gh_cannot_detect(rig):
    # gh unreachable: adopt must not crash — it keeps the template's dev_branch ("main") and writes
    # the config. Its GitHub half (label creation) does fail here, so adopt now exits nonzero
    # (issue #29) — but cleanly (a handled exit 1, not a traceback), with the config on disk. doctor
    # is the backstop that later FAILs if that guessed branch is wrong.
    fresh = rig.tmp / "fresh-nogh"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh), env_over={"GH_FAIL": "1"})
    assert r.returncode == 1, r.stdout + r.stderr     # labels failed -> nonzero, but no crash
    cfg = json.loads((fresh / ".superlooper" / "config.json").read_text())
    assert cfg["dev_branch"] == "main"               # template fallback, config still written


def test_adopt_creates_the_model_and_effort_starter_labels(rig):
    # gh refuses to apply a label that doesn't exist in the repo, so adopt must seed every value
    # William can drop on an issue as a per-issue control knob (starter set, not an allowlist).
    fresh = rig.tmp / "fresh-knobs"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert {"model:opus", "model:opus[1m]", "model:fable"} <= created
    assert {"effort:low", "effort:medium", "effort:high", "effort:xhigh", "effort:max"} <= created


def test_adopt_never_overwrites_an_existing_config(rig):
    before = (rig.repo / ".superlooper" / "config.json").read_text()
    r = cli(rig, "adopt", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert (rig.repo / ".superlooper" / "config.json").read_text() == before
    assert "already" in r.stdout.lower()


def test_adopt_creates_claude_md_with_loop_standing_rules(rig):
    fresh = rig.tmp / "fresh-claude"
    fresh.mkdir()

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 0, r.stdout + r.stderr
    text = (fresh / "CLAUDE.md").read_text()
    assert RULE_START in text
    assert RULE_END in text
    block = standing_rules_block(text)
    for snippet in RULE_REQUIRED_SNIPPETS:
        assert snippet in block


def test_adopt_appends_standing_rules_without_touching_existing_claude_md(rig):
    prior = "# Existing CLAUDE.md\n\nKeep this byte-for-byte.\nNo final newline"
    claude = rig.repo / "CLAUDE.md"
    claude.write_text(prior)

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    text = claude.read_text()
    assert text.startswith(prior)
    assert text[len(prior):].startswith("\n\n")
    assert text.count(RULE_START) == 1
    assert text.count(RULE_END) == 1


def test_adopt_preserves_existing_claude_md_bytes_when_appending(rig):
    prior = b"# Existing CLAUDE.md\r\n\r\nKeep this byte-for-byte.\r\nInvalid byte: \xff"
    claude = rig.repo / "CLAUDE.md"
    claude.write_bytes(prior)

    r = cli(rig, "adopt", "--repo", str(rig.repo))

    assert r.returncode == 0, r.stdout + r.stderr
    data = claude.read_bytes()
    assert data.startswith(prior)
    assert data.count(RULE_START.encode()) == 1
    assert data.count(RULE_END.encode()) == 1


def test_adopt_rerun_replaces_the_standing_rules_block_instead_of_duplicating_it(rig):
    fresh = rig.tmp / "fresh-rerun"
    fresh.mkdir()

    first = cli(rig, "adopt", "--repo", str(fresh))
    second = cli(rig, "adopt", "--repo", str(fresh))

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    text = (fresh / "CLAUDE.md").read_text()
    assert text.count(RULE_START) == 1
    assert text.count(RULE_END) == 1


def test_adopted_standing_rules_are_portable_text(rig):
    fresh = rig.tmp / "fresh-portable-rules"
    fresh.mkdir()

    r = cli(rig, "adopt", "--repo", str(fresh))

    assert r.returncode == 0, r.stdout + r.stderr
    block = standing_rules_block((fresh / "CLAUDE.md").read_text())
    for forbidden in ("William", "willprout", "owner/name", fresh.name, str(fresh), "superlooper"):
        assert forbidden not in block
    assert not re.search(r"(^|\s)(~?/[^/\s`]+/[^\s`]+)", block)


def test_adopt_exits_nonzero_when_every_label_create_fails(rig):
    # Issue #29: the reported scenario is `adopt` run BEFORE `gh auth login`. Every gh write
    # fails, so not one label exists — but the config IS written. adopt must NOT report success:
    # it exits nonzero, names `gh auth login` as the likely fix, states the mixed state plainly
    # (config kept, labels pending), and says the command is safe to re-run.
    fresh = rig.tmp / "fresh-nogh-labels"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    r = cli(rig, "adopt", "--repo", str(fresh), env_over={"GH_FAIL": "1"})
    assert r.returncode != 0, r.stdout + r.stderr
    # the config is written and KEPT despite the gh failure (the mixed state the memo must name)
    assert (fresh / ".superlooper" / "config.json").exists()
    out = r.stdout + r.stderr
    assert "gh auth login" in out                     # the likely fix, named
    assert "re-run" in out.lower()                    # safe to re-run (idempotent)
    # the named re-run command must be RUNNABLE: adopt takes --repo, not a positional (a bare
    # `superlooper adopt <path>` argparse-errors), so the memo must spell the flag out.
    assert "adopt --repo" in out
    assert "config" in out.lower() and "pending" in out.lower()   # mixed state, explicit
    assert out.count("FAIL") >= len(ALL_LABELS)       # every label reported as failed


def test_adopt_exits_nonzero_when_some_labels_fail(rig):
    # A partial GitHub blip: two label creates fail, the rest succeed. Even a single failure must
    # flip the exit code — a half-created label set is still a runner that silently can't apply
    # the labels it's missing. The closing summary names the count that failed.
    fresh = rig.tmp / "fresh-partial-labels"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    (rig.fixdir / "fail_rules.json").write_text(json.dumps([
        {"match": "label create agent-ready", "times": 1},
        {"match": "label create type:build", "times": 1}]))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "gh auth login" in out
    assert "2 of %d" % len(ALL_LABELS) in out          # the failed count is reported
    # the two failed creates were NOT recorded (the fake dies before recording); the rest were
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert "in-progress" in created and "effort:max" in created
    assert "agent-ready" not in created and "type:build" not in created


def test_adopt_succeeds_when_all_labels_already_exist(rig):
    # Re-running adopt on a repo whose labels all exist: create-or-update (--force) succeeds for
    # every one, so adopt reports success and exits 0 — no failure guidance on the clean re-run.
    fresh = rig.tmp / "fresh-relabel"
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    first = cli(rig, "adopt", "--repo", str(fresh))
    assert first.returncode == 0, first.stdout + first.stderr
    second = cli(rig, "adopt", "--repo", str(fresh))
    assert second.returncode == 0, second.stdout + second.stderr
    both = second.stdout + second.stderr
    assert "gh auth login" not in both                 # no failure guidance on a clean run
    assert "FAIL" not in second.stdout
    assert "already" in second.stdout.lower()          # config already adopted, left untouched


def _fresh_repo(rig, name):
    fresh = rig.tmp / name
    fresh.mkdir()
    subprocess.run(["git", "init", "-q", str(fresh)], check=True)
    subprocess.run(["git", "-C", str(fresh), "remote", "add", "origin",
                    "https://github.com/will/proj.git"], check=True)
    return fresh


def test_adopt_migrates_the_legacy_needs_william_label(rig):
    # issue #58: a repo adopted before the operator-name change carries `needs-william`. Re-adopt
    # RENAMES it in place to the neutral `needs-owner` (gh label edit preserves it on every issue
    # that carries it) so a stranger's own audit trail stops reading another person's name.
    fresh = _fresh_repo(rig, "legacy")
    legacy = [n for n in ALL_LABELS if n != "needs-owner"] + ["needs-william"]
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": n} for n in legacy]))
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    renames = [m for m in mutations(rig) if m["kind"] == "rename_label"]
    assert any(m["old"] == "needs-william" and m["new"] == "needs-owner" for m in renames)
    assert "needs-william -> needs-owner" in r.stdout


def test_adopt_does_not_rename_when_there_is_no_legacy_label(rig):
    # A fresh repo (or one already migrated) never renames — it just creates `needs-owner`.
    fresh = _fresh_repo(rig, "cleanlabels")
    (rig.fixdir / "label_list.json").write_text("[]")
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [m for m in mutations(rig) if m["kind"] == "rename_label"]
    created = {m["name"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert "needs-owner" in created and "needs-william" not in created


def test_adopt_label_descriptions_render_the_operator_name(rig):
    # issue #58: the seeded label descriptions sign the operator's name (defaulting to the repo
    # owner login "will"), never a hardcoded "William" and never a raw {operator} placeholder.
    fresh = _fresh_repo(rig, "opnamed")
    r = cli(rig, "adopt", "--repo", str(fresh))
    assert r.returncode == 0, r.stdout + r.stderr
    descs = {m["name"]: m["description"] for m in mutations(rig) if m["kind"] == "create_label"}
    assert descs["agent-ready"] == "will's approval: the runner may launch this issue"
    assert descs["parked"] == "handed back to will with a memo (runner-managed)"
    assert not any("William" in d for d in descs.values())
    assert not any("{operator}" in d for d in descs.values())


def test_run_uses_config_agent_and_cli_override(rig):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["ci"], "agent": "codex"}))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=codex" in r.stdout

    r2 = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1",
             "--agent", "claude", "--ticks", "0",
             env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "agent=claude" in r2.stdout


# --------------------------- status ---------------------------

def test_status_renders_lanes_gate_and_frozen(rig):
    state_home = rig.tmp / "slhome" / "o__r"
    (state_home / "state").mkdir(parents=True)
    st = loopstate.new_state()
    st["issues"]["i5"] = dict(loopstate.new_issue(), status="running", branch="sl/i5-x",
                              retries=1)
    st["issues"]["i7"] = dict(loopstate.new_issue(), status="gating")
    st["issues"]["i9"] = dict(loopstate.new_issue(), status="parked")
    loopstate.save(str(state_home / "state" / "issues.json"), st)
    loopstate.save(str(state_home / "state" / "merges_frozen.json"),
                   {"reason": "dev red: ci", "fingerprint": "fp", "since": 1})
    import journal as journal_mod
    journal_mod.append(state_home, {"act": "merge", "id": "i3", "outcome": "ok"}, now=100)
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "i5" in out and "running" in out and "sl/i5-x" in out
    assert "i7" in out and "gating" in out
    assert "FROZEN" in out and "dev red: ci" in out
    assert "merge" in out                            # journal tail is rendered


def test_status_on_a_never_run_repo_is_calm(rig):
    r = cli(rig, "status", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "never" in r.stdout.lower() or "no runner" in r.stdout.lower()


# --------------------------- run + stubs ---------------------------

def _cmux_stub(rig, *, resolve=True, self_pane=None, workspace="WS-9", window="WIN-9"):
    """A minimal cmux for the run command's pane resolution + D7 preflight. `identify` returns a
    caller.{pane_id,workspace_id,window_id} (self-pane + anchor auto-detection); `list-pane-surfaces`
    prints a surface line (or an rc-0 'Error: not_found', the exit-code trap) per `resolve`; every
    other subcommand fails so a tick's launch attempts stay inert (no real tabs)."""
    # `--id-format uuids` precedes the subcommand, so scan all args for the verb.
    # self_pane="" -> identify yields no pane_id (simulates running outside a cmux surface).
    pane = "SELFPANE" if self_pane is None else self_pane
    caller = (f'{{"pane_id": "{pane}", "workspace_id": "{workspace}", "window_id": "{window}"}}'
              if pane else "{}")
    body = ("#!/bin/sh\n"
            'for a in "$@"; do case "$a" in\n'
            f'  identify) echo \'{{"caller": {caller}}}\'; exit 0 ;;\n'
            '  list-pane-surfaces) '
            + ("echo '  surface:1  tab'; exit 0 ;;\n" if resolve
               else "echo 'Error: not_found'; exit 0 ;;\n")
            + 'esac; done\nexit 1\n')
    p = rig.tmp / ("cmux_ok" if resolve else "cmux_bad")
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def test_run_ticks_once_and_writes_the_heartbeat(rig):
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=claude" in r.stdout
    hb = rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat"
    assert hb.exists()


def test_run_accepts_explicit_codex_agent_selection(rig):
    # CLI plumbing only: --agent codex is accepted and reaches the runner. No launch happens in
    # this one-tick empty queue, so no codex/claude binary can be invoked.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--agent", "codex",
            "--ticks", "1", env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "agent=codex" in r.stdout
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_auto_detects_its_own_pane_without_any_pane_flag(rig):
    # owner request 2026-07-06: no --pane, no $SL_PANE — the runner targets the cmux tab it runs
    # in (cmux identify -> caller.pane_id) and starts cleanly. No hardcoded pane anywhere.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="SELFPANE"),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFPANE" in r.stdout and "this cmux tab" in r.stdout
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_boot_line_carries_the_workspace_and_window_anchor(rig):
    # issue #33: the boot line must name WHERE the runner landed (workspace + window), so a runner
    # started in the wrong cmux window (the 2026-07-09 misplacement) is visible immediately — not an
    # opaque pane UUID that could be any window.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="SELFPANE",
                                            workspace="WS-42", window="WIN-42"),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "workspace=WS-42" in r.stdout and "window=WIN-42" in r.stdout


def test_run_boot_line_omits_anchor_fields_that_do_not_resolve(rig):
    # An explicit --pane in an environment cmux-identify can't answer: the pane still resolves
    # (preflight passes), but workspace/window are simply omitted — never printed as empty noise.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "0",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane="")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "workspace=" not in r.stdout and "window=" not in r.stdout


def test_run_fails_hard_when_the_pane_will_not_resolve(rig):
    # D7: an unresolvable pane must FAIL HARD before the loop (never a quiet warning that then
    # burns every issue's retry cap). cmux exits 0 with an 'Error: not_found' line; we still refuse.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "ghost", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=False)})
    assert r.returncode != 0
    assert "FATAL" in (r.stdout + r.stderr)
    assert "resolve pane" in (r.stdout + r.stderr).lower()
    # and it never started: no heartbeat written
    assert not (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_fails_hard_when_no_pane_and_not_in_cmux(rig):
    # No --pane, no $SL_PANE, and identify yields nothing (started outside a cmux surface):
    # fail hard, and tell the operator to run inside a cmux tab.
    r = cli(rig, "run", "--repo", str(rig.repo), "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True, self_pane=""),
                      "SL_PANE": "", "CMUX_PANE_ID": ""})
    assert r.returncode != 0
    assert "no cmux pane" in (r.stdout + r.stderr).lower()
    assert "cmux tab" in (r.stdout + r.stderr).lower()


# ------------- run: runner-managed label boot preflight (issue #108) -------------

def _cli_module():
    """Import the extensionless CLI as a module so its pure helpers can be unit-tested. Safe: the
    file guards its entrypoint behind `if __name__ == '__main__'`, and conftest already put
    skill/lib + skill/bin on sys.path so its imports resolve."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("superlooper_cli", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_runner_managed_labels_is_the_tagged_subset():
    # the runner-managed subset is derived from the '(runner-managed)' tag in the LABELS
    # descriptions, so the LABELS list stays the single source of truth.
    sl = _cli_module()
    assert set(sl.runner_managed_labels()) == {"in-progress", "needs-owner", "parked"}


def test_missing_runner_labels_pure():
    sl = _cli_module()
    assert sl.missing_runner_labels(set(ALL_LABELS)) == []
    assert sl.missing_runner_labels({"agent-ready", "in-progress", "parked"}) == ["needs-owner"]
    assert set(sl.missing_runner_labels(set())) == {"in-progress", "needs-owner", "parked"}
    assert set(sl.missing_runner_labels([])) == {"in-progress", "needs-owner", "parked"}  # list ok
    assert set(sl.missing_runner_labels("garbage")) == {"in-progress", "needs-owner", "parked"}


def test_run_fails_loud_when_a_runner_managed_label_is_missing(rig):
    # the 2026-07-13 incident: `needs-owner` did not exist, so every bounce/park label move retried
    # forever. The runner defends itself at boot — FAIL LOUD, naming the label AND the exact adopt
    # remediation, rather than boot into a repo where it cannot hand issues back.
    (rig.fixdir / "label_list.json").write_text(json.dumps(
        [{"name": n} for n in ALL_LABELS if n != "needs-owner"]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "needs-owner" in out                            # names the missing label
    assert "adopt" in out and str(rig.repo) in out         # names the exact remediation
    # it never started: no heartbeat written
    assert not (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_boots_when_all_runner_managed_labels_present(rig):
    # the all-present case: the healthy rig has ALL_LABELS -> the preflight passes and the runner
    # ticks normally.
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_skips_the_label_preflight_when_gh_is_unreachable(rig):
    # a transient gh blip at boot must NOT block a restart: a refused label read fails closed
    # (ok=False) and SKIPS the check, and fix-1's bounded-storm guards cover a genuinely-missing
    # label until the next doctor/adopt. gh unreachable -> boot proceeds; the tick's own poll marks
    # the view stale and simply waits.
    (rig.fixdir / "label_list.json").write_text(json.dumps([{"name": "agent-ready"}]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True), "GH_FAIL": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


def test_run_boots_when_the_label_read_is_refused_but_gh_probes_ok(rig):
    # P1 (issue #108 review, the #92 refused-vs-answered-empty class): `gh api rate_limit` (probe) is
    # EXEMPT from rate limiting, so during a core-throttle window it reads OK while the label LIST
    # read is throttled to a fail-closed empty set. The preflight must read that as a REFUSED read
    # (ok=False) and SKIP — never as "every runner-managed label missing" — or it would wedge the
    # boot during the very rate-limit window this issue hardens against. label_list.json is intact
    # (all labels present), but the ONE `label list` call is forced to fail, mimicking the throttle.
    (rig.fixdir / "fail_rules.json").write_text(json.dumps([{"match": "label list", "times": 1}]))
    r = cli(rig, "run", "--repo", str(rig.repo), "--pane", "p1", "--ticks", "1",
            env_over={"SL_CMUX": _cmux_stub(rig, resolve=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (rig.tmp / "slhome" / "o__r" / "state" / "runner.heartbeat").exists()


# --------------------------- promotion + accept-failure (Task 12) ---------------------------

def test_accept_failure_persists_into_the_ledger(rig):
    fp = "abc123def456abcd"
    r = cli(rig, "accept-failure", fp, "--note", "known flaky widget", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    home = rig.tmp / "slhome" / "o__r"
    led = json.loads((home / "ledger.json").read_text())
    assert fp in led and led[fp]["note"] == "known flaky widget"


def test_promote_report_use_latest_nightly_is_evidence_only(rig):
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "last_nightly.json").write_text(json.dumps(
        {"date": "2026-07-01", "ok": True,
         "failures": [{"test_id": "t::regression", "text": "new boom after PR #40"}]}))
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrote" in r.stdout
    reports = sorted((home / "reports").glob("promotion-*.md"))
    assert reports
    text = reports[0].read_text()
    assert "evidence only" in text.lower() and "must pass" not in text.lower()   # §4.6 bright line
    assert "t::regression" in text                                               # new failure shown


def test_promote_report_wrong_typed_cached_ok_is_not_treated_as_parsed(rig):
    # Codex R2 C3: a corrupt/hand-edited last_nightly.json with a truthy-but-wrong-typed `ok`
    # ("false", {}, 1, …) must NOT render as parsed evidence ("No new failures") — that would be
    # a silent all-clear. Require ok is True; anything else is the could-not-parse path.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "last_nightly.json").write_text(json.dumps(
        {"date": "2026-07-01", "ok": "false", "failures": []}))     # ok is a STRING, not True
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("promotion-*.md"))[0].read_text()
    assert "could not parse" in text.lower()
    assert "no new failures" not in text.lower()


def test_promote_report_missing_nightly_is_a_clean_error(rig):
    r = cli(rig, "promote-report", "--use-latest-nightly", "--repo", str(rig.repo))
    assert r.returncode == 1
    assert "no stored nightly" in (r.stdout + r.stderr).lower()


def test_promote_report_fresh_suite_runs_and_writes(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "promote-report", "--repo", str(rig.repo),
            env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    home = rig.tmp / "slhome" / "o__r"
    assert sorted((home / "reports").glob("promotion-*.md"))


# --------------------------- nightly QA (Task 12) ---------------------------

def _write_qa(rig, qa):
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["ci"], "qa": qa}))


def _write_junit(path, failing):
    if failing:
        path.write_text('<testsuites><testsuite tests="1" failures="1">'
                        '<testcase classname="pkg" name="test_x">'
                        '<failure message="boom">at line 5</failure></testcase>'
                        '</testsuite></testsuites>')
    else:
        path.write_text('<testsuites><testsuite tests="1" failures="0">'
                        '<testcase classname="pkg" name="test_x"/></testsuite></testsuites>')


def _nightly_records(rig):
    home = rig.tmp / "slhome" / "o__r"
    jp = home / "journal.jsonl"
    recs = [json.loads(x) for x in jp.read_text().splitlines()] if jp.exists() else []
    return home, [r for r in recs if r.get("act") == "nightly"]


def test_nightly_null_cmd_is_a_clean_noop(rig):
    r = cli(rig, "nightly", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "null" in r.stdout.lower() and "nothing to run" in r.stdout.lower()


def test_nightly_persistent_failure_freezes_and_files_a_standing_rule_issue(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=True)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml", "retry_once": True})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    created = [m for m in mutations(rig) if m["kind"] == "create_issue"]
    assert len(created) == 1
    # the exact standing-rule label set (§4.4 audit trail) + the runner's fingerprint dedup marker
    assert created[0]["labels"] == \
        "type:diagnose-and-fix,agent-ready,auto-approved:nightly-red,expedite"
    assert "Failure fingerprint:" in created[0]["body"]
    home, recs = _nightly_records(rig)
    fm = json.loads((home / "state" / "merges_frozen.json").read_text())
    assert fm["source"] == "nightly"                                 # nightly claims freeze ownership
    assert recs and recs[-1]["persistent"] == 1 and recs[-1]["green"] is False


def test_green_nightly_clears_its_own_freeze(rig, tmp_path):
    # Codex R2 C2 (review test 2): the next green nightly unfreezes a nightly-owned freeze.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text(json.dumps(
        {"reason": "nightly red", "source": "nightly", "since": 1}))
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (home / "state" / "merges_frozen.json").exists()      # nightly cleared its own freeze


def test_green_nightly_leaves_a_dev_check_freeze_alone(rig, tmp_path):
    # a green nightly must NOT clear a runner dev-check freeze — that one is the runner's to clear.
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text(json.dumps(
        {"reason": "dev red: ci", "source": "dev-check", "since": 1}))
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "state" / "merges_frozen.json").exists()          # dev-check freeze untouched


def test_nightly_green_freezes_nothing_and_files_nothing(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    fx = tmp_path / "junit.xml"
    _write_junit(fx, failing=False)
    _write_qa(rig, {"nightly_cmd": f"mkdir -p results && cp {fx} results/junit.xml",
                    "results_glob": "results/*.xml"})
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert [m for m in mutations(rig) if m["kind"] == "create_issue"] == []
    home, recs = _nightly_records(rig)
    assert not (home / "state" / "merges_frozen.json").exists()
    assert recs[-1]["green"] is True


def test_nightly_unparseable_results_are_honest_never_a_silent_green(rig, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_qa(rig, {"nightly_cmd": "true", "results_glob": "results/*.xml"})   # produces no results
    r = cli(rig, "nightly", "--repo", str(rig.repo), env_over={"SL_NIGHTLY_WORKTREE": str(wt)})
    assert r.returncode == 1                                          # nonzero: could not confirm
    home, recs = _nightly_records(rig)
    assert not (home / "state" / "merges_frozen.json").exists()       # no freeze on could-not-parse
    assert [m for m in mutations(rig) if m["kind"] == "create_issue"] == []
    assert recs[-1]["parse_error"] is True and recs[-1]["green"] is False


def test_morning_report_treats_a_corrupt_freeze_marker_as_frozen(rig):
    # a present-but-non-dict freeze marker must read as FROZEN (existence = frozen), never flowing
    home = rig.tmp / "slhome" / "o__r"
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "merges_frozen.json").write_text('["nightly red"]')   # valid JSON, wrong type
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    text = sorted((home / "reports").glob("morning-*.md"))[0].read_text()
    assert "FROZEN" in text


def test_morning_report_writes_and_reflects_the_journal(rig):
    import journal
    home = rig.tmp / "slhome" / "o__r"
    journal.append(str(home), {"act": "merge", "id": "i5", "num": 5, "pr": 9, "outcome": "ok"})
    r = cli(rig, "morning-report", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrote" in r.stdout
    reports = sorted((home / "reports").glob("morning-*.md"))
    assert reports, "no morning report written"
    text = reports[0].read_text()
    assert "superlooper morning report" in text
    # the seeded merge is reflected, cross-linked against the repo (o/r)
    assert "#5" in text and "https://github.com/o/r/pull/9" in text


# --------------------------- D1: gh pinned to config.repo, never cwd ---------------------------

def _recording_gh(rig):
    """A wrapper SL_GH that records $GH_REPO per invocation, then behaves exactly like fake-gh —
    the assertion surface for 'every gh call this CLI makes carries the config repo'."""
    record = rig.tmp / "gh-env.log"
    wrapper = rig.tmp / "gh-wrapper"
    wrapper.write_text("#!/bin/sh\n"
                       'printf "%s\\n" "${GH_REPO:-}" >> "' + str(record) + '"\n'
                       'exec "' + str(_FAKE_GH) + '" "$@"\n')
    wrapper.chmod(0o755)
    return record, wrapper


def test_doctor_pins_gh_to_config_repo_not_cwd(rig):
    # D1 (live dry-run 2026-07-03): run the CLI from an UNRELATED cwd — every gh call must
    # still carry config.repo via GH_REPO, not fall back to gh's cwd-remote inference.
    record, wrapper = _recording_gh(rig)
    elsewhere = rig.tmp / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "doctor", "--repo", str(rig.repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = record.read_text().splitlines()
    assert lines and all(l == "o/r" for l in lines), (lines, r.stdout)


def test_adopt_pins_gh_to_the_adopted_repo(rig):
    # already-adopted path: label creation must target the config's repo from any cwd
    # (the fresh-adopt/origin-detection path is covered separately below)
    record, wrapper = _recording_gh(rig)
    elsewhere = rig.tmp / "elsewhere-adopt"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "adopt", "--repo", str(rig.repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = record.read_text().splitlines()
    assert lines and all(l == "o/r" for l in lines), (lines, r.stdout)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_fresh_adopt_pins_gh_to_the_detected_origin(rig):
    # fresh adopt: no config yet — the pin must come from the origin-detected slug that adopt
    # just wrote into the config, so the label set lands in the DETECTED repo from any cwd
    record, wrapper = _recording_gh(rig)
    repo = rig.tmp / "fresh-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=rig.env, timeout=30)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/det/ected.git"], check=True, env=rig.env, timeout=30)
    elsewhere = rig.tmp / "elsewhere-fresh"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "adopt", "--repo", str(repo)],
                       capture_output=True, text=True, cwd=str(elsewhere),
                       env={**rig.env, "SL_GH": str(wrapper)}, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((repo / ".superlooper" / "config.json").read_text())
    assert cfg["repo"] == "det/ected"
    lines = record.read_text().splitlines()
    assert lines and all(l == "det/ected" for l in lines), (lines, r.stdout)


# --------------------------- tidy (close finished session windows) ---------------------------
# `superlooper tidy` is William's explicit word — never automatic (V1 'nothing auto-closed').
# It closes the cmux windows of FINISHED sessions (default: merged; --all: every terminal
# status) and NEVER an in-flight lane. The close mirrors the runner's _close_stale_session:
# `cmux close-surface --surface <uuid> [--workspace <ws>]`, rc ignored, then the pane markers +
# singleton lock are cleared.

def _tidy_home(rig):
    return rig.tmp / "slhome" / "o__r"


def _seed_tidy_state(rig, issues, panes):
    """issues: {iid: status}. panes: {iid: (surface, ws_or_None)} — also drops a worker lock per
    pane so a test can assert tidy frees the singleton lock like _close_stale_session does."""
    home = _tidy_home(rig)
    (home / "state" / "panes").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    for iid, status in issues.items():
        st["issues"][iid] = dict(loopstate.new_issue(), status=status, branch=f"sl/{iid}")
    loopstate.save(str(home / "state" / "issues.json"), st)
    for iid, (surf, ws) in panes.items():
        (home / "state" / "panes" / iid).write_text(surf)
        if ws:
            (home / "state" / "panes" / f"{iid}.ws").write_text(ws)
        (home / "state" / f"worker.{iid}.lock").write_text("held")
    return home


def _recording_cmux(rig, *, rc=0):
    """A cmux stub that records every invocation's argv to a log and exits `rc` — the surface to
    assert both the exact close argv and that a nonzero rc is ignored (best-effort close)."""
    log = rig.tmp / "cmux-close.log"
    stub = rig.tmp / "cmux-rec"
    stub.write_text("#!/bin/sh\n"
                    'printf "%s\\n" "$*" >> "' + str(log) + '"\n'
                    f"exit {rc}\n")
    stub.chmod(0o755)
    return log, str(stub)


def test_tidy_dry_run_lists_merged_windows_and_closes_nothing(rig):
    _seed_tidy_state(rig, {"i1": "merged", "i2": "merged", "i5": "running"},
                     {"i1": ("surf1", None), "i2": ("surf2", None), "i5": ("surf5", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--dry-run", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "i1" in r.stdout and "i2" in r.stdout
    assert "i5" not in r.stdout                       # the in-flight lane is never listed
    assert not log.exists()                           # dry-run closed nothing


def test_tidy_yes_closes_merged_windows_with_the_close_stale_session_argv(rig):
    home = _seed_tidy_state(rig, {"i1": "merged", "i5": "running"},
                            {"i1": ("surf1", None), "i5": ("surf5", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    assert calls == ["close-surface --surface surf1"]     # exactly the merged window, that argv
    # markers + lock cleared for the closed session; the in-flight lane untouched
    assert not (home / "state" / "panes" / "i1").exists()
    assert not (home / "state" / "worker.i1.lock").exists()
    assert (home / "state" / "panes" / "i5").exists()
    assert (home / "state" / "worker.i5.lock").exists()


def test_tidy_passes_the_workspace_when_one_is_recorded(rig):
    _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("surf1", "ws7")})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf1 --workspace ws7"]


def test_tidy_ignores_a_nonzero_close_rc(rig):
    # best-effort: a dead surface makes cmux exit nonzero; tidy still succeeds and still clears.
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("surf1", None)})
    log, cmux = _recording_cmux(rig, rc=3)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf1"]
    assert not (home / "state" / "panes" / "i1").exists()


def test_tidy_default_scope_leaves_parked_windows_alone(rig):
    home = _seed_tidy_state(rig, {"i1": "merged", "i2": "parked"},
                            {"i1": ("surf1", None), "i2": ("surf2", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf1"]
    assert (home / "state" / "panes" / "i2").exists()     # parked left for possible re-approval


def test_tidy_all_scope_closes_every_terminal_status_but_never_inflight(rig):
    home = _seed_tidy_state(
        rig, {"i1": "merged", "i2": "parked", "i3": "needs_william", "i4": "bounced",
              "i5": "running", "i6": "gating"},
        {f"i{n}": (f"surf{n}", None) for n in range(1, 7)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    closed = set(log.read_text().splitlines())
    assert closed == {f"close-surface --surface surf{n}" for n in (1, 2, 3, 4)}
    assert (home / "state" / "panes" / "i5").exists()     # running: never closed
    assert (home / "state" / "panes" / "i6").exists()     # gating: never closed
    # merged is fully cleaned (never relaunches -> race-free); re-approvable sessions keep their
    # markers + lock (runner reconciles them) so tidy can never free a live worker's lock.
    assert not (home / "state" / "panes" / "i1").exists()
    assert not (home / "state" / "worker.i1.lock").exists()
    for n in (2, 3, 4):
        assert (home / "state" / "panes" / f"i{n}").exists()
        assert (home / "state" / f"worker.i{n}.lock").exists()


def test_tidy_confirm_yes_closes(rig):
    _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("surf1", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux}, inp="y\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf1"]


def test_tidy_confirm_default_no_aborts_and_closes_nothing(rig):
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("surf1", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux}, inp="\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()                               # empty answer = No = nothing closed
    assert (home / "state" / "panes" / "i1").exists()
    assert (home / "state" / "worker.i1.lock").exists()


def test_tidy_with_only_inflight_sessions_closes_nothing(rig):
    home = _seed_tidy_state(rig, {"i5": "running", "i6": "blocked", "i7": "exited"},
                            {"i5": ("surf5", None), "i6": ("surf6", None), "i7": ("surf7", None)})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()
    for n in (5, 6, 7):
        assert (home / "state" / "panes" / f"i{n}").exists()


def test_tidy_on_a_never_run_repo_is_calm(rig):
    r = cli(rig, "tidy", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "no" in r.stdout.lower()                       # "no finished ... to close"


def test_tidy_survives_a_corrupt_issues_json(rig):
    # a wrong-typed issues.json (parses as a JSON list, not a state dict) must degrade to
    # "nothing to close", never crash — fail closed on wrong-typed input.
    home = _tidy_home(rig)
    (home / "state" / "panes").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text('["not", "a", "state", "dict"]')
    (home / "state" / "panes" / "i1").write_text("surf1")
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not log.exists()
    assert "no" in r.stdout.lower()


def test_tidy_never_touches_a_reapprovable_sessions_markers_or_lock(rig):
    # (Codex cross-review rounds 1-2, critical) a parked/needs-william/bounced session can be
    # re-approved + relaunched by a LIVE runner at any time — and its pane markers/lock aren't
    # under any lock tidy can take, so a read-then-remove can never be made atomic. The airtight
    # fix is STRUCTURAL: tidy closes such a window but NEVER removes its markers/lock (that stays
    # the runner's _close_stale_session lifecycle), so tidy can never free a live worker's lock.
    home = _seed_tidy_state(rig, {"i2": "parked"}, {"i2": ("surf2", "ws2")})
    log, cmux = _recording_cmux(rig)
    r = cli(rig, "tidy", "--all", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": cmux})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf2 --workspace ws2"]
    # the window is closed, but the session's markers + singleton lock are LEFT for the runner
    assert (home / "state" / "panes" / "i2").exists()
    assert (home / "state" / "panes" / "i2.ws").exists()
    assert (home / "state" / "worker.i2.lock").exists()
    assert "reconcile" in r.stdout.lower()


def test_tidy_snapshots_the_surface_so_a_relaunch_cannot_redirect_the_close(rig):
    # tidy closes the SNAPSHOTTED surface (captured at list time), never a fresh re-read. Even if
    # the pane marker is rewritten to a new (live) surface during the run, the close targets the
    # OLD, already-dead surface — so a concurrent relaunch can't get its live window closed.
    home = _seed_tidy_state(rig, {"i1": "merged"}, {"i1": ("surf-old", None)})
    log = rig.tmp / "cmux-close.log"
    marker = home / "state" / "panes" / "i1"
    stub = rig.tmp / "cmux-relaunch"
    stub.write_text("#!/bin/sh\n"
                    'printf "%s\\n" "$*" >> "' + str(log) + '"\n'
                    'printf "surf-new" > "' + str(marker) + '"\n'      # simulate a relaunch mid-close
                    "exit 0\n")
    stub.chmod(0o755)
    r = cli(rig, "tidy", "--yes", "--repo", str(rig.repo), env_over={"SL_CMUX": str(stub)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["close-surface --surface surf-old"]   # snapshot, not re-read


# --------------------------- janitor (propose-and-approve GitHub debris sweep) ---------------------------
# `superlooper janitor` (issue #62, spec §8 V2) PROPOSES GitHub-side cleanup — stale sl/*
# branches whose PRs merged or were superseded, PRs labeled `superseded` left open by design,
# and parked / needs-william issues gathering dust — and executes ONLY what William approves
# (y/N or --yes, the same word-discipline as tidy). The list / --dry-run changes nothing
# anywhere; nothing is ever auto-closed or auto-deleted; in-flight work can never be proposed.

def _janitor_home(rig):
    return rig.tmp / "slhome" / "o__r"


def _seed_janitor_fixtures(rig):
    """The committed fixtures already carry one of each debris class:
      - branches.json: main, sl/i5-fix-thing, sl/i7-old-thing
      - pr_list_superseded.json: OPEN PR #14 labeled superseded on sl/i7-old-thing
      - issue_list_parked.json: issue #9 labeled parked, updatedAt 2026-06-01 (long aged)
    Add the per-head PR lookups: sl/i5-fix-thing's PR #12 MERGED with headRefOid matching the
    branch's current tip in branches.json (branch proposable); sl/i7-old-thing's PR #14 still
    OPEN (branch NOT proposable — the PR close comes first). And an explicit empty
    needs-william queue."""
    (rig.fixdir / "pr_list_head_sl__i5-fix-thing.json").write_text(json.dumps(
        [{"number": 12, "state": "MERGED", "headRefName": "sl/i5-fix-thing",
          "headRefOid": "bbb222", "labels": []}]))
    (rig.fixdir / "pr_list_head_sl__i7-old-thing.json").write_text(json.dumps(
        [{"number": 14, "state": "OPEN", "headRefName": "sl/i7-old-thing",
          "headRefOid": "ccc333", "labels": [{"name": "superseded"}]}]))
    # The janitor sweeps EVERY park-family label (issue #58: needs-owner + legacy needs-william).
    (rig.fixdir / "issue_list_needs-owner.json").write_text("[]")
    (rig.fixdir / "issue_list_needs-william.json").write_text("[]")


def _janitor_journal(rig):
    p = _janitor_home(rig) / "journal.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _janitor_refused(rig):
    p = _janitor_home(rig) / "state" / "janitor_refused.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_janitor_dry_run_lists_all_three_classes_and_changes_nothing(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    # one proposal per debris class, each with its one-line why
    assert "sl/i5-fix-thing" in out and "#12" in out and "merged" in out.lower()
    assert "#14" in out and "superseded" in out
    assert "#9" in out and "parked" in out and "threshold 14d" in out
    # the branch under the OPEN superseded PR is NOT proposed for deletion
    assert "delete branch sl/i7-old-thing" not in out
    # dry-run changes NOTHING anywhere: no gh writes, no journal, no refused file, no state dir
    assert mutations(rig) == []
    assert _janitor_journal(rig) == []
    assert _janitor_refused(rig) is None


def test_janitor_prompt_default_no_aborts_and_executes_nothing(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--repo", str(rig.repo), inp="\n")
    assert r.returncode == 0
    assert "aborted" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []


def test_janitor_yes_executes_all_three_actions_and_journals_them(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    muts = mutations(rig)
    kinds = {m["kind"] for m in muts}
    assert kinds == {"delete_ref", "close_pr", "close_issue"}
    assert next(m for m in muts if m["kind"] == "delete_ref")["ref"] == "heads/sl/i5-fix-thing"
    pr_close = next(m for m in muts if m["kind"] == "close_pr")
    assert pr_close["num"] == "14" and "janitor" in pr_close["comment"]
    issue_close = next(m for m in muts if m["kind"] == "close_issue")
    assert issue_close["num"] == "9" and "janitor" in issue_close["comment"]
    recs = [x for x in _janitor_journal(rig) if x.get("act") == "janitor"]
    assert len(recs) == 3 and all(x["outcome"] == "ok" for x in recs)
    assert all(x.get("why") for x in recs)          # the one-line why rides into the journal
    assert "3 executed" in r.stdout


def test_janitor_prompt_y_executes(rig):
    _seed_janitor_fixtures(rig)
    r = cli(rig, "janitor", "--repo", str(rig.repo), inp="y\n")
    assert r.returncode == 0
    assert len(mutations(rig)) == 3


def test_janitor_never_proposes_inflight_or_midgate_work(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    st = loopstate.new_state()
    st["issues"]["i5"] = dict(loopstate.new_issue(), status="running",
                              branch="sl/i5-fix-thing")
    st["issues"]["i7"] = dict(loopstate.new_issue(), status="gating",
                              branch="sl/i7-old-thing")
    st["issues"]["i9"] = dict(loopstate.new_issue(), status="holding")
    loopstate.save(str(home / "state" / "issues.json"), st)
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to propose" in r.stdout
    assert mutations(rig) == []


def test_janitor_unreadable_loopstate_refuses_to_propose(rig):
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "issues.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "unreadable" in (r.stdout + r.stderr)
    assert mutations(rig) == []


def test_janitor_respects_the_configured_age_threshold(rig):
    _seed_janitor_fixtures(rig)
    (rig.repo / ".superlooper" / "config.json").write_text(json.dumps(
        {"version": 1, "repo": "o/r", "required_checks": ["quality-gate"],
         "janitor": {"aged_park_days": 100000}}))
    r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "close issue" not in r.stdout        # issue #9 is no longer past the threshold
    assert "#12" in r.stdout                    # the branch/PR proposals are unaffected


def test_janitor_failed_action_surfaces_once_and_is_held_back(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "fail_rules.json").write_text(json.dumps(
        [{"match": "git/refs/heads/sl/i5-fix-thing", "times": 1,
          "stderr": "HTTP 403: refs are protected"}]))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    # the failure is LOUD: nonzero exit, a FAIL line, a fail journal record, a refused entry
    assert r.returncode != 0
    assert "FAIL" in r.stdout and "branch:sl/i5-fix-thing" in r.stdout
    recs = {x["target"]: x["outcome"] for x in _janitor_journal(rig)
            if x.get("act") == "janitor"}
    assert recs == {"sl/i5-fix-thing": "fail", 14: "ok", 9: "ok"}
    assert "branch:sl/i5-fix-thing" in _janitor_refused(rig)

    # sweep 2, with the world reflecting sweep 1 (PR closed, issue closed): the refused branch
    # is HELD BACK — surfaced as a held-back count, never silently retried.
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")
    r2 = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "nothing to propose" in r2.stdout and "held back" in r2.stdout
    deletes = [m for m in mutations(rig) if m["kind"] == "delete_ref"]
    assert deletes == []                         # sweep 1's delete FAILED, sweep 2 never retried

    # sweep 3 with --retry-refused (no fail rule left): the delete executes and the refusal
    # record clears.
    r3 = cli(rig, "janitor", "--yes", "--retry-refused", "--repo", str(rig.repo))
    assert r3.returncode == 0, r3.stdout + r3.stderr
    deletes = [m for m in mutations(rig) if m["kind"] == "delete_ref"]
    assert [d["ref"] for d in deletes] == ["heads/sl/i5-fix-thing"]
    assert _janitor_refused(rig) == {}


def test_janitor_never_prunes_a_refusal_on_absence(rig):
    # a key's ABSENCE from a sweep is not proof its debris is gone — a transient gh blip also
    # produces absence (every read fails closed to empty). Pruning on it would silently drop
    # the hold-back and let a later sweep retry a refused action without --retry-refused
    # (cross-review round 1, C1). A refusal clears ONLY via a later successful execution.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text(json.dumps(
        {"branch:sl/i99-vanished": {"reason": "gone", "ts": 1}}))
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode == 0
    assert "branch:sl/i99-vanished" in _janitor_refused(rig)


def test_janitor_unreadable_refused_file_refuses_to_run(rig):
    # corrupt hold-back state read as {} would re-propose every held-back action — the same
    # fail-open class as unreadable issues.json (cross-review round 1, C2). Refuse instead.
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "janitor_refused.json").write_text("{corrupt")
    r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    assert r.returncode != 0
    assert "janitor_refused.json" in (r.stdout + r.stderr)
    assert mutations(rig) == [] and _janitor_journal(rig) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_janitor_permission_denied_refused_file_refuses_to_run(rig):
    # a PRESENT hold-back file that cannot be OPENED (EPERM, not ENOENT) must read as
    # unreadable, never as missing/{} — only a genuinely absent file means "nothing ever
    # refused" (cross-review round 2: _read() collapsed every OSError to None).
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    p = home / "state" / "janitor_refused.json"
    p.write_text(json.dumps({"branch:sl/i99-x": {"reason": "r", "ts": 1}}))
    p.chmod(0o000)
    try:
        r = cli(rig, "janitor", "--yes", "--repo", str(rig.repo))
    finally:
        p.chmod(0o644)
    assert r.returncode != 0
    assert "janitor_refused.json" in (r.stdout + r.stderr)
    assert mutations(rig) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_janitor_permission_denied_loopstate_refuses_to_propose(rig):
    # the same distinction for issues.json: an unreadable-by-permissions exclusion source must
    # refuse the sweep (nothing is provably idle), never read as "no lanes exist".
    _seed_janitor_fixtures(rig)
    home = _janitor_home(rig)
    (home / "state").mkdir(parents=True, exist_ok=True)
    p = home / "state" / "issues.json"
    p.write_text(json.dumps({"issues": {"i5": {"status": "running"}}}))
    p.chmod(0o000)
    try:
        r = cli(rig, "janitor", "--dry-run", "--repo", str(rig.repo))
    finally:
        p.chmod(0o644)
    assert r.returncode != 0
    assert "unreadable" in (r.stdout + r.stderr)
    assert mutations(rig) == []


def test_janitor_nothing_to_propose_is_a_clean_exit(rig):
    _seed_janitor_fixtures(rig)
    (rig.fixdir / "branches.json").write_text("[]")
    (rig.fixdir / "pr_list_superseded.json").write_text("[]")
    (rig.fixdir / "issue_list_parked.json").write_text("[]")
    r = cli(rig, "janitor", "--repo", str(rig.repo))   # no --yes: must not hang on a prompt
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to propose" in r.stdout
    assert mutations(rig) == [] and _janitor_journal(rig) == []
