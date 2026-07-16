"""Engine publish drift (issue #166) — is the loop RUNNING the fixes that were merged?

The runner executes the INSTALLED engine copy, never the checkout. A merged engine change is inert
until someone republishes it through the gated ``bin/install.sh``, so a fix can sit merged for days
while the loop keeps running the old code — and nothing said so. This module counts that gap.

Exercised against REAL local git repos, exactly as ``test_pollers`` exercises ``diff_stat``: git is
local, touches no network, and is not one of the neutralized external binaries (``gh`` / ``cmux`` /
``osascript`` / the ``superlooper`` CLI). The fixtures below build a throwaway checkout shaped like
the superlooper monorepo — a payload under ``skills/superlooper/skill`` and files outside it — so
the path-scoping is proven against git's real answer rather than a stub's.

The load-bearing assertion in this file is :func:`test_unknown_is_never_reported_as_zero` and its
siblings: **no failure may ever render as "you are up to date"**. A false all-clear is the exact
failure class the issue exists to close.
"""
import subprocess
from pathlib import Path

import pytest

import engine

# The monorepo root: this dashboard is a subdirectory of it, and the gated installer lives at its
# bin/install.sh. Reached by path because the installer is shell, not an importable module.
_MONOREPO = Path(__file__).resolve().parent.parent.parent


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _commit(path, rel, text, msg):
    p = path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg)


def _head(path):
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def src(tmp_path):
    """A real checkout shaped like the superlooper monorepo, on ``main``."""
    path = tmp_path / "src"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _commit(path, engine.PAYLOAD_REL + "/runner.py", "v1\n", "base")
    _git(path, "branch", "-M", "main")
    return path


@pytest.fixture
def dest(tmp_path):
    """An installed-engine dir, as ``bin/install.sh`` leaves it."""
    d = tmp_path / "installed"
    d.mkdir()
    return d


def _stamp(dest, sha, date="2026-07-16"):
    (dest / "VERSION").write_text("%s %s\n" % (sha, date))


# --------------------------- the banner and the installer must agree ---------------------------

def test_the_banner_scopes_the_same_payload_the_installer_publishes():
    # The whole honesty of the drift line rests on this: the number counts commits under the SAME
    # tree the named remedy would publish. If install.sh's PAYLOAD_REL is ever moved and this
    # constant isn't, the banner keeps counting confidently against a path nobody publishes — a
    # wrong number, stated calmly, which is worse than no number at all.
    sh = (_MONOREPO / "bin" / "install.sh").read_text(encoding="utf-8")
    assert 'PAYLOAD_REL="%s"' % engine.PAYLOAD_REL in sh, (
        "lib/engine.PAYLOAD_REL must match bin/install.sh's PAYLOAD_REL")


def test_the_named_remedy_is_the_gated_installer_that_actually_exists():
    # The remedy is a real, runnable path from the monorepo root — and specifically the GATED one,
    # not the engine's ungated nested copy (which would walk the owner around his own fence).
    assert (_MONOREPO / engine.REMEDY).is_file()
    assert engine.REMEDY == "bin/install.sh"


# --------------------------- the install dir, derived from existing config ---------------------------

def test_install_dir_derives_the_installers_dest_from_the_configured_cli():
    # install.sh publishes the payload to $DEST and the CLI to $DEST/bin/superlooper, so the parent
    # of the CLI's bin/ IS $DEST. Deriving it from config's existing `superlooper_cli` means a
    # non-standard install is honored for free and no new config key exists to get out of sync.
    assert engine.install_dir("~/.claude/skills/superlooper/bin/superlooper".replace("~", "/home/x")) \
        == "/home/x/.claude/skills/superlooper"


def test_install_dir_of_a_nonsense_cli_is_none():
    assert engine.install_dir("") is None
    assert engine.install_dir(None) is None


# --------------------------- the VERSION stamp ---------------------------

def test_installed_stamp_reads_the_sha_and_date_install_sh_wrote(dest):
    _stamp(dest, "abc1234", "2026-07-15")
    assert engine.installed_stamp(dest) == {"sha": "abc1234", "at": "2026-07-15"}


def test_absent_version_reads_as_no_stamp(dest):
    assert engine.installed_stamp(dest) is None


def test_unreadable_or_empty_version_reads_as_no_stamp(dest):
    (dest / "VERSION").write_text("\n")
    assert engine.installed_stamp(dest) is None


# --------------------------- finding the engine source ---------------------------

def test_source_repo_is_the_watched_checkout_carrying_the_payload(src, tmp_path):
    other = tmp_path / "other"
    (other / "lib").mkdir(parents=True)
    assert engine.source_repo([str(other), str(src)]) == str(src)


def test_no_watched_checkout_carries_the_payload_means_no_engine_source(tmp_path):
    # A friend who adopted superlooper for their own repo has no monorepo checked out. There is
    # nothing to compare, so there is nothing to say — silence, never a scary unknown.
    other = tmp_path / "other"
    other.mkdir()
    assert engine.source_repo([str(other)]) is None


# --------------------------- the count ---------------------------

def test_behind_counts_only_commits_that_touch_the_payload(src, dest):
    base = _head(src)
    _stamp(dest, base)
    _commit(src, "README.md", "docs\n", "docs only")          # not the engine
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "engine fix one")
    _commit(src, engine.PAYLOAD_REL + "/lib/x.py", "x\n", "engine fix two")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True
    assert st["behind"] == 2, "the README commit must not count as an engine fix"


def test_a_freshly_published_engine_is_not_behind(src, dest):
    _stamp(dest, _head(src))
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True and st["behind"] == 0
    assert st["message"] is None, "an up-to-date engine says nothing (§0.2 — no nagging)"


def test_the_drift_message_is_the_dod_sentence_and_names_the_remedy(src, dest):
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "fix a")
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v3\n", "fix b")
    st = engine.drift(str(src), str(dest))
    assert st["behind"] == 2
    assert st["message"] == ("2 engine fixes merged but not yet live; re-run the installer "
                             "to switch them on")
    assert st["remedy"] == "bin/install.sh"


def test_one_fix_is_named_in_the_singular(src, dest):
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "fix a")
    st = engine.drift(str(src), str(dest))
    assert st["message"] == ("1 engine fix merged but not yet live; re-run the installer "
                             "to switch it on")


def test_the_installed_build_rides_along_for_inspection(src, dest):
    sha = _head(src)
    _stamp(dest, sha, "2026-07-11")
    st = engine.drift(str(src), str(dest))
    assert st["installed_sha"] == sha and st["installed_at"] == "2026-07-11"


# --------------------------- unknown is never zero ---------------------------

def test_unknown_is_never_reported_as_zero(src, dest, monkeypatch):
    # THE assertion of this module. A git that errors must not wave through a confident
    # "up to date" — that is a false all-clear, the failure class this issue closes.
    _stamp(dest, _head(src))
    monkeypatch.setattr(engine, "_git", lambda *a, **k: (128, ""))
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False
    assert st["behind"] is None, "a failed count must never collapse to 0"
    assert st["message"] and "can't tell" in st["message"]


def test_a_baseline_outside_this_history_is_unknown_not_zero(src, dest):
    # A VERSION from an unrelated history (a re-clone, a rewritten branch). install.sh fails SAFE
    # here by treating the whole payload as new; a BANNER cannot honestly claim a number, so it
    # says it cannot tell.
    _stamp(dest, "deadbee")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["behind"] is None


def test_a_nogit_tarball_stamp_is_unknown_not_zero(src, dest):
    # install.sh stamps `nogit` when it publishes from a tree with no git. There is no baseline to
    # diff, so there is no honest number.
    _stamp(dest, "nogit")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["behind"] is None


def test_an_engine_never_published_here_says_nothing(src, dest):
    # No VERSION at all: nothing was ever published through install.sh to this dest, so there is no
    # baseline AND no evidence of a live engine to warn about. Silence, not a permanent unknown —
    # a line that can never clear is the nag §0.2 forbids.
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["message"] is None


def test_no_source_repo_says_nothing(dest):
    _stamp(dest, "abc1234")
    st = engine.drift(None, str(dest))
    assert st["known"] is False and st["message"] is None


# --------------------------- the slow clock ---------------------------

def test_state_is_measured_on_a_slow_clock_not_every_two_second_poll(src, dest):
    # Drift changes only when someone merges or republishes — both minutes-scale events. Shelling
    # git twice a second for an answer that moves twice a day is waste the poll loop can't afford.
    _stamp(dest, _head(src))
    calls = []
    clock = [1000.0]

    def counting(*a, **k):
        calls.append(1)
        return engine.drift(*a, **k)

    d = engine.EngineDrift(str(src), str(dest), interval=30, clock=lambda: clock[0],
                           measure=counting)
    d.state(); d.state(); d.state()
    assert len(calls) == 1, "three polls inside the interval must cost one measurement"
    clock[0] += 30
    d.state()
    assert len(calls) == 2, "past the interval it re-measures"


def test_state_never_raises_into_the_poll_loop(dest):
    d = engine.EngineDrift("/nonexistent/checkout", str(dest))
    assert d.state()["known"] is False
