"""Tests for skill/bin/install-cli-link.sh — the publish step that puts a stable `superlooper`
command on PATH (issue #31).

Every doc invokes the CLI bare (`superlooper adopt`, `superlooper doctor`, `superlooper run`),
but the real binary lives inside the published skill at
``~/.claude/skills/superlooper/bin/superlooper`` — a location no install step ever put on PATH.
This linker writes a THIN SHIM (not a symlink) into a standard user bin dir; the shim ``exec``s
the PUBLISHED copy, never a source checkout.

Every case runs against a FAKE $HOME with a fully CONTROLLED $PATH, so neither the real home nor
any real bin dir (``~/.local/bin``, ``/usr/local/bin``) is ever touched — a candidate is only ever
chosen if it appears in the $PATH this test hands the installer.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))            # skills/superlooper
LINK = os.path.join(REPO_ROOT, "skill", "bin", "install-cli-link.sh")
# The gated repo-root publish door (one level above skills/): its final step runs the linker above.
ROOT_INSTALL = os.path.abspath(os.path.join(REPO_ROOT, "..", "..", "bin", "install.sh"))

# The system dirs the script's own tools (mkdir/grep/chmod/dirname/rm/cat + bash) resolve from.
# Deliberately EXCLUDES every candidate bin dir, so "on PATH" is decided only by what a test adds.
SYS = "/usr/bin:/bin"
INSTALLED = "$HOME/.claude/skills/superlooper/bin/superlooper"    # the shim's exec target, literal
MARKER = "superlooper-cli-shim"


def _run(home, *, path):
    env = {"HOME": str(home), "PATH": path}
    return subprocess.run([LINK], env=env, capture_output=True, text=True, timeout=30)


def _out(proc):
    return proc.stdout + proc.stderr


def _is_exec(p):
    return bool(p.stat().st_mode & stat.S_IXUSR)


def test_creates_a_thin_shim_in_an_on_path_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"                          # NOT pre-created: creatable path
    r = _run(home, path=f"{local_bin}:{SYS}")

    assert r.returncode == 0, _out(r)
    shim = local_bin / "superlooper"
    assert shim.exists(), _out(r)
    assert not shim.is_symlink(), "must be a real shim file, never a symlink (breaks lib import)"
    assert _is_exec(shim), "shim must be executable"
    body = shim.read_text()
    assert MARKER in body
    assert INSTALLED in body                                     # execs the installed copy
    assert body.strip().splitlines()[-1] == f'exec "{INSTALLED}" "$@"'
    out = _out(r)
    assert str(shim) in out                                      # prints WHERE it linked
    assert "resolves now" in out and "not on your PATH" not in out


def test_shim_points_at_the_installed_copy_never_the_source_repo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    r = _run(home, path=f"{local_bin}:{SYS}")
    assert r.returncode == 0, _out(r)
    body = (local_bin / "superlooper").read_text()
    # the installed layout is .../superlooper/bin/superlooper (skill/ contents rsynced UP one level);
    # the source layout is .../skills/superlooper/skill/bin/superlooper. Neither a source path nor
    # the source-only "/skill/bin/" segment may appear.
    assert "/skill/bin/superlooper" not in body
    assert REPO_ROOT not in body
    assert str(home) not in body                                 # $HOME stays LITERAL, unexpanded


def test_is_idempotent_byte_for_byte(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    first = _run(home, path=f"{local_bin}:{SYS}")
    body1 = (local_bin / "superlooper").read_text()
    second = _run(home, path=f"{local_bin}:{SYS}")
    body2 = (local_bin / "superlooper").read_text()
    assert first.returncode == 0 and second.returncode == 0, _out(first) + _out(second)
    assert body1 == body2                                        # re-run rewrites identical bytes
    assert list(local_bin.glob("superlooper")) == [local_bin / "superlooper"]


def test_prints_the_exact_manual_step_when_nothing_is_on_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # No candidate dir is on PATH (SYS excludes them all): the linker must NOT silently skip — it
    # writes the shim into the preferred dir and prints the exact line to add it to PATH.
    r = _run(home, path=SYS)
    assert r.returncode == 0, _out(r)
    local_bin = home / ".local" / "bin"
    assert (local_bin / "superlooper").exists(), "shim is still written, never skipped"
    out = _out(r)
    assert "not on your PATH" in out
    assert f'export PATH="{local_bin}:$PATH"' in out             # the EXACT, runnable manual step


def test_prefers_an_on_path_candidate_over_the_fallback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # ~/.local/bin is NOT on PATH but ~/bin is: pick the one that actually works today.
    home_bin = home / "bin"
    r = _run(home, path=f"{home_bin}:{SYS}")
    assert r.returncode == 0, _out(r)
    assert (home_bin / "superlooper").exists()
    assert not (home / ".local" / "bin" / "superlooper").exists()
    assert "resolves now" in _out(r)


def test_sweeps_a_stale_shim_it_left_in_another_candidate_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    home_bin = home / "bin"
    # Run 1 lands the shim in ~/.local/bin.
    _run(home, path=f"{local_bin}:{SYS}")
    assert (local_bin / "superlooper").exists()
    # Run 2 (PATH now favours ~/bin) must land there AND remove the stale ~/.local/bin shim, so
    # exactly one superlooper shim is ever on PATH.
    r = _run(home, path=f"{home_bin}:{SYS}")
    assert r.returncode == 0, _out(r)
    assert (home_bin / "superlooper").exists()
    assert not (local_bin / "superlooper").exists()             # stale one swept
    assert "removed stale shim" in _out(r)


def test_replaces_a_foreign_superlooper_with_a_loud_note(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    foreign = local_bin / "superlooper"
    foreign.write_text("#!/bin/sh\necho not-ours\n")             # a pre-existing, non-shim file
    foreign.chmod(0o755)
    r = _run(home, path=f"{local_bin}:{SYS}")
    assert r.returncode == 0, _out(r)
    body = foreign.read_text()
    assert MARKER in body                                        # our shim now owns the name
    assert "not-ours" not in body
    assert "replaced" in _out(r).lower()                         # never a silent clobber


def test_the_sweep_leaves_a_foreign_superlooper_in_another_dir_untouched(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    home_bin = home / "bin"
    home_bin.mkdir(parents=True)
    foreign = home_bin / "superlooper"                          # a user's own binary, no marker
    foreign.write_text("#!/bin/sh\necho theirs\n")
    foreign.chmod(0o755)
    r = _run(home, path=f"{local_bin}:{SYS}")                    # chooses ~/.local/bin; sweep visits ~/bin
    assert r.returncode == 0, _out(r)
    assert (local_bin / "superlooper").exists()
    assert foreign.exists() and "theirs" in foreign.read_text()  # marker-guarded: NOT swept
    assert "removed stale shim" not in _out(r)


def test_warns_when_a_foreign_superlooper_earlier_on_path_shadows_the_shim(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    other = home / "otherbin"                                   # NOT one of the candidate dirs
    other.mkdir()
    foreign = other / "superlooper"
    foreign.write_text("#!/bin/sh\necho theirs\n")
    foreign.chmod(0o755)
    local_bin = home / ".local" / "bin"
    # otherbin is FIRST on PATH (it wins resolution); ~/.local/bin is a candidate, so the shim lands
    # there but does NOT actually resolve. The report must say so instead of falsely claiming success.
    r = _run(home, path=f"{other}:{local_bin}:{SYS}")
    assert r.returncode == 0, _out(r)
    assert (local_bin / "superlooper").exists()                 # shim still written to the candidate
    out = _out(r)
    assert "shadows" in out
    assert str(foreign) in out                                  # names WHAT shadows it


def test_refuses_and_survives_a_directory_sitting_at_the_target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    collide = local_bin / "superlooper"
    collide.mkdir()                                             # pathological: a DIR occupies the target
    (collide / "keep.txt").write_text("precious")
    r = _run(home, path=f"{local_bin}:{SYS}")
    assert r.returncode == 0, _out(r)                           # never fails an otherwise-good publish
    assert collide.is_dir() and (collide / "keep.txt").read_text() == "precious"   # left untouched
    assert "a directory occupies" in _out(r)


# --- end-to-end: the real publish door links the CLI, and a bare `superlooper` then resolves ------

@pytest.mark.skipif(shutil.which("rsync") is None or shutil.which("git") is None,
                    reason="the root installer needs rsync + git")
def test_publish_then_a_bare_superlooper_command_resolves(tmp_path):
    """DoD, end to end: after a publish on a fresh home, the docs' bare `superlooper` invocation
    resolves through PATH and runs the INSTALLED CLI. Running `--help` proves the CLI's lib/ imports
    resolved — i.e. the shim exec'd the real path (a symlink would have imported from the wrong dir
    and crashed here)."""
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    # Put the fake ~/.local/bin FIRST on PATH so the linker picks a dir UNDER the fake home (never a
    # real /usr/local/bin); keep the real system dirs so install.sh finds rsync/git/python3/bash.
    path = f"{local_bin}:{os.environ.get('PATH', '')}"
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"), "PATH": path}
    env.pop("ZDOTDIR", None)                                     # so the launch shim edits fake .zshrc
    # --yes accepts the engine-diff gate non-interactively (a fresh home has no baseline, so the whole
    # payload counts as new and still needs an explicit OK — supplied here).
    # Insurance against a future selection regression: record the REAL /usr/local/bin/superlooper
    # state and assert the publish never changed it (the fake ~/.local/bin must win selection).
    real_usr_local = Path("/usr/local/bin/superlooper")
    existed_before = real_usr_local.exists()

    r = subprocess.run([ROOT_INSTALL, "--yes"], env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, _out(r)

    published = home / ".claude" / "skills" / "superlooper" / "bin" / "superlooper"
    assert published.exists(), "install.sh must publish the CLI payload"
    shim = local_bin / "superlooper"
    assert shim.exists(), "install.sh must link the superlooper command onto PATH"
    assert not (home / "bin" / "superlooper").exists()           # only the chosen candidate got it
    assert real_usr_local.exists() == existed_before, "must never touch the real /usr/local/bin"
    assert "install-cli-link" in _out(r)                         # the linker ran and reported

    got = subprocess.run(["superlooper", "--help"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert got.returncode == 0, got.stdout + got.stderr
    out = got.stdout + got.stderr
    assert "usage" in out.lower()
    assert "adopt" in out and "doctor" in out                    # the documented subcommands resolve


@pytest.mark.skipif(shutil.which("rsync") is None or shutil.which("git") is None,
                    reason="the root installer needs rsync + git")
def test_dry_run_links_nothing_and_publishes_nothing(tmp_path):
    """The engine-diff publish gate + the whole publish must stay side-effect-free under --dry-run:
    the linker runs only in the real path (step 5), never in the dry-run branch."""
    home = tmp_path / "home"
    home.mkdir()
    local_bin = home / ".local" / "bin"
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
           "PATH": f"{local_bin}:{os.environ.get('PATH', '')}"}
    env.pop("ZDOTDIR", None)
    r = subprocess.run([ROOT_INSTALL, "--dry-run"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, _out(r)
    assert not (local_bin / "superlooper").exists(), "dry-run must link nothing"
    assert not (home / ".claude" / "skills" / "superlooper").exists(), "dry-run must publish nothing"
    out = _out(r)
    assert "install-cli-link.sh" in out and "would run" in out   # but it announces the step
