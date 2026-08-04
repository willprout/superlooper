"""skill/lib/herdr_hook.py — the per-worker hook config that gives the session host its
session-id capture WITHOUT `integration install` ever touching a global settings file (issue #307).

Three things are under test here, and they are different in kind:

1. **The PIN.** The host's installer writes one specific hook, with one specific command spelling,
   into one specific event. We reproduce that shape from OUR side, so the constants below are a
   contract with a third-party release — asserted against the vendored asset's own version marker
   and checksum, so a version bump that changes the integration cannot land silently.
2. **The RENDERING.** A settings document Claude Code will actually load, written where a worker
   can read it and nowhere near ``~/.claude/settings.json``.
3. **The DETECTION.** Given somebody's global settings, does it carry the host's hook? That is the
   doctor's question — "was `integration install` ever run on this machine" — and it has to
   recognise the LEGACY event registrations too, because an old install left them behind.
"""
import json
import os
import stat
import subprocess

import pytest

import herdr_hook

_SKILL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skill"))


# --------------------------------------------------------------------------- the pin

def test_the_vendored_asset_is_the_pinned_release_byte_for_byte():
    """The checksum IS the pin. A dropped-in newer asset changes this and goes red — which is the
    only mechanical thing standing between "we carry herdr's hook verbatim" and "we carry some
    hook"."""
    path = herdr_hook.vendored_script(home=_SKILL)
    assert os.path.isfile(path), "the vendored hook asset is missing at %s" % path
    assert herdr_hook.script_checksum(path) == herdr_hook.HOOK_SCRIPT_SHA256


def test_the_vendored_asset_declares_the_pinned_integration_version():
    """herdr stamps its own integration version into the asset. Reading it back is a SECOND,
    independent pin: a checksum says "some file changed", this says "the integration contract
    changed", and the version-bump acceptance needs to be told which."""
    text = open(herdr_hook.vendored_script(home=_SKILL)).read()
    assert "HERDR_INTEGRATION_VERSION=%d" % herdr_hook.INTEGRATION_VERSION in text


def test_the_asset_is_carried_verbatim_and_not_forked():
    """Boundary of issue #307: carry the invocation, never fork the script. The two markers herdr
    writes into its own asset are the cheapest proof nobody edited it."""
    text = open(herdr_hook.vendored_script(home=_SKILL)).read()
    assert "# installed by herdr" in text
    assert "HERDR_INTEGRATION_ID=claude" in text


# --------------------------------------------------------------------------- the command line

def test_hook_command_matches_the_hosts_own_spelling():
    assert herdr_hook.hook_command("/opt/e/herdr-agent-state.sh") == \
        "bash '/opt/e/herdr-agent-state.sh' session"


def test_hook_command_quotes_a_path_with_a_single_quote_the_way_the_host_does():
    """herdr's shell_single_quote wraps in single quotes and rewrites an inner quote as '"'"'.
    A path with an apostrophe in it is ordinary on a Mac (``/Users/o'brien``), and a naive quote
    would turn the hook command into a syntax error that fails silently at every session start."""
    assert herdr_hook.hook_command("/Users/o'brien/h.sh") == \
        """bash '/Users/o'"'"'brien/h.sh' session"""


def test_hook_command_refuses_an_empty_path():
    with pytest.raises(ValueError):
        herdr_hook.hook_command("")


# --------------------------------------------------------------------------- the document

def test_settings_document_is_the_pinned_event_matcher_and_timeout():
    doc = herdr_hook.settings_document("/e/h.sh")
    groups = doc["hooks"][herdr_hook.HOOK_EVENT]
    assert list(doc["hooks"]) == [herdr_hook.HOOK_EVENT], \
        "exactly one event: the pinned release registers SessionStart and nothing else"
    assert len(groups) == 1 and groups[0]["matcher"] == herdr_hook.HOOK_MATCHER
    hook, = groups[0]["hooks"]
    assert hook == {"type": "command", "command": "bash '/e/h.sh' session",
                    "timeout": herdr_hook.HOOK_TIMEOUT}


def test_the_document_carries_nothing_but_hooks():
    """A per-worker settings file is MERGED over the user's own settings by Claude Code. Anything
    else in here — a model, a permission, an env block — would silently override the operator's
    choice for every worker, which is the opposite of this issue's point."""
    assert list(herdr_hook.settings_document("/e/h.sh")) == ["hooks"]


# --------------------------------------------------------------------------- writing it

def test_write_settings_produces_a_file_claude_can_load(tmp_path):
    script = tmp_path / "herdr-agent-state.sh"
    script.write_text("#!/bin/sh\n")
    out = tmp_path / "run" / "hooks" / "i307.settings.json"
    written = herdr_hook.write_settings(str(out), str(script))
    assert written == str(out)
    doc = json.loads(out.read_text())
    assert doc == herdr_hook.settings_document(str(script))
    assert out.read_text().endswith("\n")


def test_write_settings_refuses_a_script_that_is_not_there(tmp_path):
    """Fail CLOSED and LOUD. A settings file naming a hook script that does not exist produces a
    session that starts fine and never reports its id — the silent half-working state this whole
    issue exists to avoid."""
    with pytest.raises(herdr_hook.HookConfigError):
        herdr_hook.write_settings(str(tmp_path / "s.json"), str(tmp_path / "absent.sh"))


def test_write_settings_replaces_a_stale_file_atomically(tmp_path):
    script = tmp_path / "h.sh"
    script.write_text("#!/bin/sh\n")
    out = tmp_path / "i307.settings.json"
    out.write_text('{"hooks": {"SessionStart": [{"matcher": "*", "hooks": []}]}}')
    herdr_hook.write_settings(str(out), str(script))
    assert json.loads(out.read_text()) == herdr_hook.settings_document(str(script))
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".")], "left a temp file behind"


def test_write_settings_is_owner_only(tmp_path):
    """The file names a path a hook will execute at every session start. World-writable would let
    anything on the machine choose that command."""
    script = tmp_path / "h.sh"
    script.write_text("#!/bin/sh\n")
    out = tmp_path / "i307.settings.json"
    herdr_hook.write_settings(str(out), str(script))
    assert stat.S_IMODE(os.stat(out).st_mode) & 0o077 == 0


# --------------------------------------------------------------------------- detection

def _installed_global(hook_path="/Users/x/.claude/hooks/herdr-agent-state.sh"):
    """What `herdr integration install claude` leaves in a global settings.json."""
    return {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "bash '%s' session" % hook_path, "timeout": 10}]}]}}


def test_detects_an_installed_hook_in_a_settings_document():
    assert herdr_hook.carried_hook_commands(_installed_global()) == [
        "bash '/Users/x/.claude/hooks/herdr-agent-state.sh' session"]


def test_detects_the_legacy_event_registrations_an_old_install_left_behind():
    """The pinned release REMOVES these on install, so a machine carrying them was installed by an
    older herdr and never re-run. The doctor must still call that "the global file carries the
    host's hook"."""
    doc = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "bash '/h/herdr-agent-state.sh' idle"}]}]}}
    assert herdr_hook.carried_hook_commands(doc)


def test_a_clean_settings_document_carries_nothing():
    doc = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "$HOME/.claude/skills/superlooper/bin/stop-hook.sh"}]}]}}
    assert herdr_hook.carried_hook_commands(doc) == []


def test_our_own_per_worker_document_is_recognised_as_carrying_the_hook():
    """The same predicate has to answer YES for our file — otherwise the doctor could not tell a
    worker's config was wired, only that the global one was clean."""
    assert herdr_hook.carried_hook_commands(herdr_hook.settings_document("/e/herdr-agent-state.sh"))


@pytest.mark.parametrize("doc", [
    None, "", [], {"hooks": None}, {"hooks": {"SessionStart": "nope"}},
    {"hooks": {"SessionStart": [None]}}, {"hooks": {"SessionStart": [{"hooks": "x"}]}},
    {"hooks": {"SessionStart": [{"hooks": [{"command": 7}]}]}},
])
def test_detection_survives_a_malformed_settings_document(doc):
    """Somebody's settings file is not ours to validate. A wrong-typed node must read as "no hook
    here", never as an exception that takes the doctor down."""
    assert herdr_hook.carried_hook_commands(doc) == []


def test_detection_reads_a_settings_file_from_disk(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_installed_global()))
    assert herdr_hook.carried_hook_commands_in_file(str(path))


def test_an_unreadable_or_absent_settings_file_reads_as_no_hook(tmp_path):
    assert herdr_hook.carried_hook_commands_in_file(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert herdr_hook.carried_hook_commands_in_file(str(bad)) == []


# --------------------------------------------------------------------------- where it looks

def test_global_settings_path_follows_the_hosts_own_resolution(monkeypatch, tmp_path):
    """herdr's claude_dir() is `$CLAUDE_CONFIG_DIR` else `$HOME/.claude`. The doctor has to ask
    about the SAME file the installer would have written, or it clears a machine it never checked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert herdr_hook.global_settings_path({"HOME": str(tmp_path)}) == \
        os.path.join(str(tmp_path), ".claude", "settings.json")
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "/fleet/cfg"}
    assert herdr_hook.global_settings_path(env) == "/fleet/cfg/settings.json"


def test_an_empty_config_dir_override_falls_back_to_home_like_the_host_does():
    env = {"HOME": "/h", "CLAUDE_CONFIG_DIR": ""}
    assert herdr_hook.global_settings_path(env) == "/h/.claude/settings.json"


# --------------------------------------------------------------------------- the CLI edge

def test_the_module_renders_a_file_when_run_as_a_script(tmp_path):
    """start-session.sh shells out to this module — the launcher stays free of JSON assembly, and
    the rendering under test here is the same code the launcher runs."""
    out = tmp_path / "i307.settings.json"
    proc = subprocess.run(
        ["python3", os.path.join(_SKILL, "lib", "herdr_hook.py"), str(out)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(out), "it prints the path it wrote, for the launcher to use"
    doc = json.loads(out.read_text())
    assert herdr_hook.carried_hook_commands(doc)
