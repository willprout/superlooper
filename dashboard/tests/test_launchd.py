"""Task 12 — the launchd keep-alive renderer (pure ``lib/launchd.py``).

The dashboard's optional always-on install (DoD): one ``launchctl`` LaunchAgent that keeps
``bin/command-center`` alive. Following the skill's ``templates/launchd.runner.plist`` pattern, a
plist template carries ``{placeholders}`` a thin installer substitutes. But command-center is ONE
localhost process watching many repos — not one runner per repo — so there is a single label and a
single job, simpler than the skill's per-repo runner.

Everything with a right/wrong answer lives here in tested pure Python (semantics server-side, decision
B.1): the substitution, the leftover-placeholder guard, and the shape of the plist the template
renders to. ``bin/install-launchd.sh`` is then a thin shell that resolves absolute paths and writes
what this module renders — its own end-to-end is pinned in ``test_install_launchd.py``.
"""
import plistlib
import subprocess
from pathlib import Path

import launchd

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "templates" / "launchd.command-center.plist"

_BIN = "/opt/command-center/bin/command-center"
_CONFIG = "/opt/command-center/config.json"
_LOG = "/Users/pat/Library/Logs/command-center.log"


# =============================== pure substitution ===============================

def test_render_substitutes_every_placeholder():
    tmpl = "L={label} B={command_center_bin} C={config_path} O={log_path}"
    out = launchd.render_plist(tmpl, label="com.example", command_center_bin=_BIN,
                               config_path=_CONFIG, log_path=_LOG)
    assert out == "L=com.example B=%s C=%s O=%s" % (_BIN, _CONFIG, _LOG)


def test_render_allows_a_brace_substring_in_a_path_value():
    # The leftover-placeholder guard must judge the TEMPLATE, not the substituted values — a real
    # path that happens to contain a ``{lowercase}`` run (rare, but legal on disk) is not a template
    # typo and must render, not be refused (fresh-review nit, 2026-07-07).
    tmpl = "C={config_path}"
    out = launchd.render_plist(tmpl, label="com.example", command_center_bin=_BIN,
                               config_path="/opt/{beta}/config.json", log_path=_LOG)
    assert out == "C=/opt/{beta}/config.json"


def test_render_rejects_a_leftover_placeholder():
    # A template typo (``{bogus}``) must fail LOUD at install time, never silently ship a plist with
    # a literal ``{bogus}`` that launchd would choke on — the same fail-loud ethos as config.load.
    tmpl = "{label} {command_center_bin} {config_path} {log_path} {bogus}"
    try:
        launchd.render_plist(tmpl, label="com.example", command_center_bin=_BIN,
                             config_path=_CONFIG, log_path=_LOG)
    except ValueError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("render_plist must reject an unfilled placeholder")


def test_default_label_is_one_dashboard_job_not_per_repo():
    # One localhost process watches every repo, so there is exactly one keep-alive job / one label —
    # no owner/name slug baked in (that is the skill's per-repo runner, a different shape).
    assert launchd.DEFAULT_LABEL == "com.command-center"
    assert "/" not in launchd.DEFAULT_LABEL


# =============================== the committed template renders to a valid plist ===============================

def test_committed_template_parses_as_a_keepalive_plist():
    rendered = launchd.render_plist(_TEMPLATE.read_text(), label=launchd.DEFAULT_LABEL,
                                    command_center_bin=_BIN, config_path=_CONFIG, log_path=_LOG)
    doc = plistlib.loads(rendered.encode("utf-8"))     # a malformed plist raises here
    assert doc["Label"] == launchd.DEFAULT_LABEL
    # ProgramArguments launches the dashboard binary with the config path — exactly bin then config.
    assert doc["ProgramArguments"] == [_BIN, _CONFIG]
    assert doc["KeepAlive"] is True                    # relaunch a crashed dashboard
    assert doc["RunAtLoad"] is True                    # start at login
    assert doc["StandardOutPath"] == _LOG
    assert doc["StandardErrorPath"] == _LOG


def test_committed_template_carries_no_leftover_placeholder():
    rendered = launchd.render_plist(_TEMPLATE.read_text(), label=launchd.DEFAULT_LABEL,
                                    command_center_bin=_BIN, config_path=_CONFIG, log_path=_LOG)
    assert "{" not in rendered and "}" not in rendered


def test_committed_template_throttles_keepalive_relaunches():
    # Issue #34: KeepAlive relaunches a crashed dashboard, but a bind failure (the port is already in
    # use) exits on EVERY launch — so without a throttle KeepAlive would hot crash-loop, hammering the
    # port and filling the log many times a second. A ThrottleInterval spaces the relaunches so the
    # loop is COOL (at most once per interval), leaving a human time to read the friendly line and free
    # the port. Documented in the README's launchd section (DoD).
    rendered = launchd.render_plist(_TEMPLATE.read_text(), label=launchd.DEFAULT_LABEL,
                                    command_center_bin=_BIN, config_path=_CONFIG, log_path=_LOG)
    doc = plistlib.loads(rendered.encode("utf-8"))
    ti = doc.get("ThrottleInterval")
    assert isinstance(ti, int) and not isinstance(ti, bool), (
        "the keep-alive must set an integer ThrottleInterval so a bind-failure loop is cool, not hot "
        "(issue #34)")
    assert ti >= 10, (
        "a throttle under launchd's own 10s default gives no real protection against a hot crash-loop")


# =============================== the CLI the install shell calls ===============================

def test_cli_prints_the_rendered_plist(tmp_path):
    # bin/install-launchd.sh shells out to `python3 lib/launchd.py --bin … --config … --log …`; that
    # contract is pinned here so a change to the CLI flags can't silently break the installer.
    proc = subprocess.run(
        ["python3", str(_ROOT / "lib" / "launchd.py"),
         "--bin", _BIN, "--config", _CONFIG, "--log", _LOG],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = plistlib.loads(proc.stdout.encode("utf-8"))
    assert doc["ProgramArguments"] == [_BIN, _CONFIG]
    assert doc["Label"] == launchd.DEFAULT_LABEL
