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


def _with_origin(src, tmp_path):
    """Give ``src`` a REAL origin whose ``main`` can move without the checkout knowing — the shape of
    this project's actual deployment: the loop merges to origin, and the local checkout lags until
    someone pulls. Returns a working clone through which "merges" are pushed into that origin.

    A real remote (never a fabricated ref) so ``origin/main`` resolves exactly as it does on the
    deployment machine, and a ``fetch`` moves it exactly as a real merge would.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True,
                   capture_output=True, text=True)
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "fetch", "-q", "origin")
    work = tmp_path / "origin-work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True,
                   capture_output=True, text=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    return work


def _merge_to_origin(work, rel, text, msg):
    """Land a commit on the origin's ``main`` — a merged loop PR, from the checkout's point of view."""
    _commit(work, rel, text, msg)
    _git(work, "push", "-q", "origin", "HEAD:main")


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

@pytest.fixture
def bare_home(tmp_path, monkeypatch):
    """A HOME with no engine installed.

    `install_dir` consults the DEFAULT install (~/.claude/skills/superlooper) as a backstop, so
    without this the machine's own real install leaks into the assertions — and the suite would pass
    or fail depending on whose laptop it ran on. Returns the fake home for tests that want to
    populate it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_install_dir_derives_the_installers_dest_from_the_configured_cli(tmp_path, bare_home):
    # install.sh publishes the payload to $DEST and the CLI to $DEST/bin/superlooper, so the parent
    # of the CLI's bin/ IS $DEST. Deriving it from config's existing `superlooper_cli` means a
    # non-standard install is honored for free and no new config key exists to get out of sync.
    custom = tmp_path / "custom-install"
    assert engine.install_dir(str(custom / "bin" / "superlooper")) == str(custom)


def test_a_cli_pointed_at_the_path_shim_still_finds_the_install(bare_home):
    # Raised in review. install.sh ALSO drops a `superlooper` shim in ~/.local/bin — which is how
    # every doc invokes it. Deriving $DEST from that gives ~/.local, which holds no VERSION, and the
    # engine line would go SILENT forever: a config choice quietly disabling the honesty surface.
    real = bare_home / ".claude" / "skills" / "superlooper"
    real.mkdir(parents=True)
    (real / "VERSION").write_text("abc1234 2026-07-16\n")
    assert engine.install_dir(str(bare_home / ".local" / "bin" / "superlooper")) == str(real)


def test_a_real_custom_install_still_wins_over_the_default(tmp_path, bare_home):
    # The backstop must never override an install the operator actually has: a configured dir that
    # HOLDS a stamp is the answer, even when the default install also exists.
    default = bare_home / ".claude" / "skills" / "superlooper"
    default.mkdir(parents=True)
    (default / "VERSION").write_text("aaa 2026-07-16\n")
    custom = tmp_path / "custom-install"
    (custom / "bin").mkdir(parents=True)
    (custom / "VERSION").write_text("bbb 2026-07-16\n")
    assert engine.install_dir(str(custom / "bin" / "superlooper")) == str(custom)


def test_install_dir_of_a_nonsense_cli_is_none(bare_home):
    assert engine.install_dir("") is None
    assert engine.install_dir(None) is None


# --------------------------- the VERSION stamp ---------------------------

def test_installed_stamp_reads_the_sha_and_date_install_sh_wrote(dest):
    _stamp(dest, "abc1234", "2026-07-15")
    assert engine.installed_stamp(dest) == {"sha": "abc1234", "at": "2026-07-15"}


def test_absent_version_reads_as_no_stamp(dest):
    # Absent is an ANSWER: nothing was ever published here, so there is nothing to say.
    assert engine.installed_stamp(dest) is None


def test_a_present_but_empty_version_is_a_failure_not_an_absence(dest):
    # Raised in review. install.sh always writes content, so an empty stamp means something broke.
    # Mapping it to None would make a half-written stamp render as a silent all-clear.
    (dest / "VERSION").write_text("\n")
    assert engine.installed_stamp(dest) is engine.UNREADABLE


def test_an_unreadable_version_is_a_failure_not_an_absence(dest):
    # A directory where the stamp should be: present, unopenable. That is a failure, not "never
    # published" — the two must not collapse into the same silent answer.
    (dest / "VERSION").mkdir()
    assert engine.installed_stamp(dest) is engine.UNREADABLE


def test_an_unreadable_stamp_speaks_rather_than_going_quiet(src, dest):
    (dest / "VERSION").write_text("")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["behind"] is None
    assert st["message"] and "can't tell" in st["message"]


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


# --------------------------- a dirty payload cannot be compared ---------------------------
#
# Raised in review, and verified against the installer: bin/install.sh does NOT publish HEAD — it
# rsyncs the WORKING TREE (§2) and then stamps HEAD's sha (§3). So the stamp only identifies the
# published CONTENT while the payload is clean. Dirty, it breaks BOTH ways, which is why neither
# number may be stated.

def test_uncommitted_engine_changes_are_unknown_not_a_confident_zero(src, dest):
    # The false all-clear: publish a dirty tree and the stamp says HEAD, so VERSION..HEAD counts 0
    # and the strip goes silent — "engine is live" — while live code sits in no commit at all.
    _stamp(dest, _head(src))
    (src / engine.PAYLOAD_REL / "runner.py").write_text("edited, not committed\n")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["behind"] is None
    assert "uncommitted engine changes" in st["message"]


def test_an_untracked_payload_file_is_also_dirty(src, dest):
    # rsync copies untracked files too, so a brand-new engine file is live-on-publish while being
    # invisible to any commit-based count.
    _stamp(dest, _head(src))
    (src / engine.PAYLOAD_REL / "brand_new.py").write_text("new\n")
    assert engine.drift(str(src), str(dest))["known"] is False


def test_dirt_outside_the_payload_does_not_muddy_the_engine_verdict(src, dest):
    # A dashboard edit or a scratch file is not an engine change. If any dirt anywhere blanked the
    # count, the line would be permanently unknown on a working machine — the nag §0.2 forbids.
    _stamp(dest, _head(src))
    (src / "README.md").write_text("editing the readme\n")
    (src / "scratch.txt").write_text("junk\n")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True and st["behind"] == 0


def test_a_dirty_payload_is_unknown_even_when_commits_are_also_waiting(src, dest):
    # Don't state "1 fix waiting" when the installer would in fact publish that PLUS uncommitted
    # work: an understated count is still a wrong one.
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "a real fix")
    (src / engine.PAYLOAD_REL / "runner.py").write_text("and more, uncommitted\n")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False and st["behind"] is None


# --------------------------- "merged" means origin/main, not this checkout ---------------------------
#
# THE finding of this module's review, verified against the real checkout on the deployment machine.
# `~/Projects/superlooper` lags its own `origin/main` BY DESIGN — the loop merges its PRs to origin,
# so a freshly merged engine fix is on origin/main and NOT in the checkout until someone pulls.
# Counting against HEAD made that fix read `behind: 0` -> no message -> an empty engine line, which
# is pixel-identical to a live engine. The banner built to end confident-while-blind went silent
# about exactly the fix it exists to name.

def test_a_fix_merged_on_origin_main_but_not_pulled_is_still_named(src, dest, tmp_path):
    origin = _with_origin(src, tmp_path)
    _stamp(dest, _head(src))
    # A fix merges to origin/main. The local checkout has NOT pulled — the everyday state here.
    _merge_to_origin(origin, engine.PAYLOAD_REL + "/runner.py", "v2\n", "a merged engine fix")
    _git(src, "fetch", "-q", "origin")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True
    assert st["behind"] == 1, "a fix merged on origin/main is merged, pulled or not"
    assert "merged but not yet live" in st["message"]


def test_the_remedy_names_the_pull_when_the_checkout_is_the_thing_in_the_way(src, dest, tmp_path):
    # install.sh publishes the CHECKOUT. Telling the owner to "re-run the installer" over work he
    # has not pulled would publish the old code and leave the banner refusing to clear — a remedy
    # that cannot work is worse than none.
    origin = _with_origin(src, tmp_path)
    _stamp(dest, _head(src))
    _merge_to_origin(origin, engine.PAYLOAD_REL + "/runner.py", "v2\n", "merged, unpulled")
    _git(src, "fetch", "-q", "origin")
    assert engine.drift(str(src), str(dest))["message"] == (
        "1 engine fix merged but not yet live; pull, then re-run the installer to switch it on")


def test_once_pulled_the_remedy_is_just_the_installer(src, dest, tmp_path):
    origin = _with_origin(src, tmp_path)
    _stamp(dest, _head(src))
    _merge_to_origin(origin, engine.PAYLOAD_REL + "/runner.py", "v2\n", "merged")
    _git(src, "fetch", "-q", "origin")
    _git(src, "merge", "-q", "origin/main")          # the owner pulls
    msg = engine.drift(str(src), str(dest))["message"]
    assert "pull" not in msg and "re-run the installer" in msg


def test_unmerged_local_work_is_not_counted_as_merged(src, dest, tmp_path):
    # The mirror of the headline case: a commit sitting in the checkout that origin/main has never
    # seen is not "merged", and must not be counted as one.
    origin = _with_origin(src, tmp_path)
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "local, unmerged")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True and st["behind"] == 0
    assert st["message"] is None


def test_a_checkout_with_no_main_at_all_narrows_the_claim(src, dest):
    # An exotic repo with neither origin/main nor main: the commits are real and not live, but they
    # cannot be PROVEN merged, so the sentence may not use the word.
    _stamp(dest, _head(src))
    _git(src, "checkout", "-q", "-b", "only-branch")
    _git(src, "branch", "-q", "-D", "main")
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "work")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True and st["behind"] == 1
    assert "merged" not in st["message"], "unprovable work must not be called merged"
    assert "not yet live" in st["message"] and "re-run the installer" in st["message"]


def test_on_main_the_dod_sentence_is_used_verbatim(src, dest):
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "a merged fix")
    assert engine.drift(str(src), str(dest))["message"] == (
        "1 engine fix merged but not yet live; re-run the installer to switch it on")


# --------------------------- ask the installer's question, not a lookalike ---------------------------
#
# Raised in review: install.sh's gate diffs CONTENT (`git diff --name-status`), while a bare commit
# count is a different question that disagrees with it in both directions.

def test_a_commit_and_its_revert_leave_nothing_to_publish(src, dest):
    # The trust-corroding case: two payload commits, identical content. A commit count would nag
    # "2 engine fixes merged but not yet live"; the owner runs the named remedy and install.sh
    # answers "no engine changes since last publish — payload is unchanged". A banner that sends him
    # to a remedy that does nothing is a §0.2 nag AND a small lie.
    _stamp(dest, _head(src))
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2\n", "a change")
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v1\n", "revert it")
    st = engine.drift(str(src), str(dest))
    assert st["known"] is True and st["behind"] == 0
    assert st["message"] is None, "identical payloads have nothing to switch on"


def test_an_installed_engine_ahead_of_the_checkout_is_unknown_not_zero(src, dest):
    # The live engine was published from a build that `main` never got (a branch publish, a
    # rolled-back checkout). Its payload DIFFERS from main's, yet no merged commit explains the
    # difference — `main..` from that sha counts nothing, because main is behind it. install.sh's
    # gate would report real changes here. A bare commit count reads 0, and 0 renders as silence:
    # a confident all-clear over an engine nobody can account for.
    _git(src, "checkout", "-q", "-b", "published-from-here")
    _commit(src, engine.PAYLOAD_REL + "/runner.py", "v2-only-here\n", "published, never merged")
    _stamp(dest, _head(src))               # the LIVE engine is THIS build...
    _git(src, "checkout", "-q", "main")    # ...and the checkout sits on main, which lacks it
    st = engine.drift(str(src), str(dest))
    assert st["known"] is False, "an engine nobody can account for must not read as up to date"
    assert st["behind"] is None
    assert "differs" in st["message"] and "divergent" in st["message"]


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


def test_a_measurement_that_blows_up_speaks_rather_than_going_quiet(src, dest):
    # Raised in review. A raising measurement used to return the SILENT unknown, which renders as no
    # engine line — indistinguishable from a live engine. "I broke" and "nothing to report" must
    # never look alike, or the error path quietly issues an all-clear.
    def boom(*a, **k):
        raise RuntimeError("git fell over")

    d = engine.EngineDrift(str(src), str(dest), measure=boom)
    st = d.state()
    assert st["known"] is False and st["behind"] is None
    assert st["message"] and "can't tell" in st["message"], (
        "a failed measurement must carry a message, or it renders as silence")


def test_unmeasurable_always_carries_a_message():
    st = engine.unmeasurable()
    assert st["known"] is False and st["behind"] is None and st["message"]


def test_concurrent_polls_measure_once(src, dest):
    # The server is a ThreadingHTTPServer, so overlapping polls land in state() together. Without a
    # lock each thread shells out to git for the same answer.
    import threading as _t
    _stamp(dest, _head(src))
    calls = []
    barrier = _t.Barrier(4)

    def slow(*a, **k):
        calls.append(1)
        return engine.drift(*a, **k)

    d = engine.EngineDrift(str(src), str(dest), interval=30, measure=slow)

    def poll():
        barrier.wait()
        d.state()

    threads = [_t.Thread(target=poll) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, "four concurrent polls must cost exactly one git measurement"
