"""bin/pretrust.sh — WHICH file the trust record lands in (issue #345).

Trust is keyed per config dir. #311's acceptance run on the mini measured this step writing its
record into the operator's DEFAULT Claude config file while the session it was trusting for would
be launched under a per-worker ``CLAUDE_CONFIG_DIR`` (#314's seam, merged 2026-08-05). A pre-trust
that lands in one file while the session reads another is INERT — and because every issue gets a
fresh worktree, that is every launch on a fleet machine, not an edge case.

Measured on this machine (2026-08-06), which is what these tests encode:

  * ``CLAUDE_CONFIG_DIR`` set   -> ``$CLAUDE_CONFIG_DIR/.claude.json``   (the file lives INSIDE it)
  * ``CLAUDE_CONFIG_DIR`` unset -> ``$HOME/.claude.json``                (a SIBLING of ``~/.claude``)

The asymmetry is real and is the whole trap: ``~/.claude-fleet/.claude.json`` exists and carries
``projects[...].hasTrustDialogAccepted``, while ``~/.claude/.claude.json`` does not exist at all.

These tests drive the REAL script — the resolution being tested is shell, so a Python paraphrase of
it would prove nothing. No claude, no gh, no host: `jq` and `bash` are the only binaries reached,
and both are pure local text tools with no owner state behind them.
"""
import json
import os
import shutil
import subprocess

import pytest

import identity

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
PRETRUST = os.path.join(REPO_ROOT, "skill", "bin", "pretrust.sh")

# The one key the vendor itself persists for the folder gate (spike A3). #311's boundary: nothing
# else may be written — accepting the bypass warning persists NOTHING, so any second key would be
# guessing at another program's private state.
TRUST_KEY = "hasTrustDialogAccepted"

needs_flock = pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="flock(1) is not shipped on macOS, where the script's own guard falls through to "
           "best-effort; the serialization it provides is only observable where flock exists")


def _run(folder, *args, **kw):
    """Drive pretrust.sh with an EXPLICIT environment — never the ambient one.

    A suite run inside a worker pane carries the loop's own CLAUDE_CONFIG_DIR, and inheriting it is
    precisely the fault under test.
    """
    home = kw.pop("home")
    env = {"PATH": os.environ.get("PATH", "")}
    env["HOME"] = str(home)
    env.update(kw.pop("env", None) or {})
    assert not kw, kw
    return subprocess.run(["bash", PRETRUST, str(folder)] + [str(a) for a in args],
                          env=env, capture_output=True, text=True)


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _assigned(tmp_path, name=".claude-fleet"):
    """A provisioned per-worker config dir, as the fleet's identity plan gives each worker."""
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


def _folder(tmp_path, name="worktree"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return d


def _trusted(conf):
    """The folders `conf` records as trusted — [] when the file does not exist."""
    if not os.path.exists(str(conf)):
        return []
    data = json.loads(open(str(conf)).read())
    return sorted(k for k, v in (data.get("projects") or {}).items() if v.get(TRUST_KEY) is True)


# ------------------------------------------------------- the assignment decides the file (#345)

def test_an_assigned_config_dir_is_where_the_trust_record_lands(tmp_path):
    """The DoD's first half: the record lands in the dir the session will be launched with."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    r = _run(work, str(cfg), home=home)

    assert r.returncode == 0, r.stderr
    assert _trusted(cfg / ".claude.json") == [str(work)]


def test_the_operators_default_file_is_never_touched_when_a_dir_is_assigned(tmp_path):
    """The measured fault, stated as a test: the entry went to the owner's file and not the
    fleet's, so the pre-trust was inert for exactly the sessions it exists to protect."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    _run(work, str(cfg), home=home)

    assert not os.path.exists(str(home / ".claude.json"))


def test_the_default_path_writes_the_operators_own_file_exactly_as_before(tmp_path):
    """The DoD's second half. An EMPTY assignment is the launcher saying 'this machine assigns no
    config dir', which is every machine but the fleet — and it must behave as it always has."""
    home, work = _home(tmp_path), _folder(tmp_path)

    r = _run(work, "", home=home)

    assert r.returncode == 0, r.stderr
    assert _trusted(home / ".claude.json") == [str(work)]


def test_an_empty_assignment_beats_an_inherited_config_dir(tmp_path):
    """Identity is ASSIGNED, never inherited (claim c3). The launcher's `edges.run` hands pretrust
    the launcher's own environment, so a stray CLAUDE_CONFIG_DIR there would otherwise re-open this
    exact bug one directory over: the record in a dir the launch is NOT using."""
    home, stray, work = _home(tmp_path), _assigned(tmp_path, "stray"), _folder(tmp_path)

    r = _run(work, "", home=home, env={"CLAUDE_CONFIG_DIR": str(stray)})

    assert r.returncode == 0, r.stderr
    assert _trusted(home / ".claude.json") == [str(work)]
    assert not os.path.exists(str(stray / ".claude.json"))


def test_a_hand_run_with_no_assignment_follows_the_agents_own_variable(tmp_path):
    """No second argument at all is a HAND run (an operator, a drill), and there the honest answer
    is the file a `claude` started in that same shell would read. The launcher never takes this
    branch — it always names the assignment, empty or not — which is what keeps the branch above
    from being reachable by inheritance."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    r = _run(work, home=home, env={"CLAUDE_CONFIG_DIR": str(cfg)})

    assert r.returncode == 0, r.stderr
    assert _trusted(cfg / ".claude.json") == [str(work)]
    assert not os.path.exists(str(home / ".claude.json"))


def test_a_hand_run_with_no_variable_at_all_still_writes_the_default_file(tmp_path):
    home, work = _home(tmp_path), _folder(tmp_path)

    r = _run(work, home=home)

    assert r.returncode == 0, r.stderr
    assert _trusted(home / ".claude.json") == [str(work)]


def test_the_shell_and_the_python_twin_agree_about_where_the_file_is(tmp_path):
    """Two readers of one vendor fact: this script decides where to WRITE and `doctor --stack`
    decides where to READ. They must name the same file or the doctor certifies a file nobody
    writes — the same shape as the defect this issue fixes, one layer up."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    _run(work, str(cfg), home=home)
    _run(_folder(tmp_path, "other"), "", home=home)

    assert _trusted(identity.config_file(str(cfg))) == [str(work)]
    assert _trusted(identity.config_file(None, home=str(home))) == [str(tmp_path / "other")]


# ----------------------------------------------------------------- what it writes, and no more

def test_the_key_written_is_the_one_the_vendor_persists_and_nothing_more(tmp_path):
    """#311's boundary: accepting the bypass-permissions warning persists NOTHING, so inventing a
    key for it would be guessing at another program's private state. The folder key alone closes
    BOTH gates (measured), which is why nothing new needs writing."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)
    (cfg / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "numStartups": 4}))

    _run(work, str(cfg), home=home)

    data = json.loads((cfg / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True and data["numStartups"] == 4
    assert list(data["projects"][str(work)].keys()) == [TRUST_KEY]


def test_the_physical_path_is_the_key(tmp_path):
    """Spike A3's catch, kept under test through the rewrite: `claude` keys trust on the resolved
    path, so a folder reached through a symlink must be recorded under its physical name or the
    dialog still fires."""
    home, cfg = _home(tmp_path), _assigned(tmp_path)
    real = _folder(tmp_path, "real")
    link = tmp_path / "link"
    os.symlink(str(real), str(link))

    _run(link, str(cfg), home=home)

    assert _trusted(cfg / ".claude.json") == [str(real)]


def test_a_second_run_under_the_assigned_dir_is_idempotent(tmp_path):
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)
    _run(work, str(cfg), home=home)
    before = (cfg / ".claude.json").read_text()

    r = _run(work, str(cfg), home=home)

    assert r.returncode == 0 and "already trusted" in r.stdout
    assert (cfg / ".claude.json").read_text() == before


# -------------------------------------------------------------------- the guard, and the refusal

def test_the_lock_sits_beside_the_file_it_writes(tmp_path):
    """DoD: the concurrency guard is preserved FOR WHATEVER FILE IT NOW WRITES. A lock still
    pinned to the default file would serialize two launches against a file neither is editing."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    _run(work, str(cfg), home=home)

    assert os.path.exists(str(cfg / ".claude.json.lock"))
    assert not os.path.exists(str(home / ".claude.json.lock"))


@needs_flock
def test_two_concurrent_launches_never_lose_each_others_records(tmp_path):
    """RC-DEADFEATURES, now against the per-worker file: two launches reading-modifying-writing one
    config at once would lose an entry, and the launch that lost it flies into the dialog."""
    home, cfg = _home(tmp_path), _assigned(tmp_path)
    folders = [_folder(tmp_path, "wt%d" % i) for i in range(8)]

    procs = [subprocess.Popen(["bash", PRETRUST, str(f), str(cfg)],
                              env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for f in folders]
    for p in procs:
        assert p.wait(timeout=60) == 0, p.stderr.read()

    assert _trusted(cfg / ".claude.json") == sorted(str(f) for f in folders)


def test_an_assigned_dir_that_does_not_exist_refuses_rather_than_inventing_a_namespace(tmp_path):
    """FAIL CLOSED. Creating it would provision a credential namespace nobody chose, and the
    launcher's rc gate turns this refusal into 'not launching' rather than into a pane that opens
    on a dialog. (The launch path cannot reach here in practice — #314's identity read refuses an
    unusable dir several steps earlier — so this is the hand-run's belt.)"""
    home, work = _home(tmp_path), _folder(tmp_path)
    missing = tmp_path / "never-provisioned"

    r = _run(work, str(missing), home=home)

    assert r.returncode != 0
    assert str(missing) in r.stderr
    assert not os.path.exists(str(missing))
    assert not os.path.exists(str(home / ".claude.json")), "and it did not silently fall back"


# ------------------------------------------------------------------------- the codex path (#345)

def test_the_codex_path_is_deliberately_unaffected(tmp_path):
    """OWNER RULING 2026-08-05: Claude Code only. Codex keys trust in `$CODEX_HOME/config.toml` and
    reads no CLAUDE_CONFIG_DIR at all, so an assignment is meaningless there — it is dropped at the
    hand-off rather than forwarded into a file format that has nowhere to put it. This test IS the
    DoD's record of that disposition."""
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)
    codex_home = tmp_path / "codex"

    r = _run(work, str(cfg), home=home,
             env={"SL_AGENT": "codex", "CODEX_HOME": str(codex_home)})

    assert r.returncode == 0, r.stderr
    assert 'trust_level = "trusted"' in (codex_home / "config.toml").read_text()
    assert str(work) in (codex_home / "config.toml").read_text()
    # ...and not one byte of the Claude side, under either file.
    assert not os.path.exists(str(cfg / ".claude.json"))
    assert not os.path.exists(str(home / ".claude.json"))


def test_an_unsupported_agent_still_refuses_before_writing_anything(tmp_path):
    home, cfg, work = _home(tmp_path), _assigned(tmp_path), _folder(tmp_path)

    r = _run(work, str(cfg), home=home, env={"SL_AGENT": "aider"})

    assert r.returncode == 64 and "aider" in r.stderr
    assert not os.path.exists(str(cfg / ".claude.json"))
