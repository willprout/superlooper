"""The slow-clock pollers (``lib/pollers.py``): the ``gh``-cadence cache, the worktree diff-stat
poller, and the fail-closed usage reader.

What each defends:
  * **Cached** — the ``gh`` slow clock: a fetch runs at most once per ``gh_poll_seconds``; between
    ticks the cached value is served. Driven by an injected fake clock, never real sleeping.
  * **diff_stat** — the flight's cargo size (+N/−N/files) from ``git diff`` in the lane worktree.
    Exercised against REAL local git repos (git is local, touches no network, and is not a
    neutralized egress binary); absent/foreign worktrees fail closed.
  * **read_usage** — the usage pill's feed. On ANY failure it returns an explicit *unknown*
    sentinel (``known: False`` ⇒ the pill renders "usage ?"), NEVER a stale number. The network /
    Keychain calls are dependency-injected, so no test here reaches either.
"""
import os
import subprocess
import urllib.error

import pollers


# ============================ Cached: the gh slow clock ============================

class _Clock:
    """A hand-cranked clock so cadence is tested deterministically (no real sleeping)."""
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_cached_fetches_once_then_serves_cache_within_interval():
    clock = _Clock()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    c = pollers.Cached(fetch, interval=30, clock=clock)
    assert c.get() == 1          # first get -> fetch
    assert c.get() == 1          # cached, no refetch
    clock.advance(29)
    assert c.get() == 1          # still inside the 30s window
    assert calls["n"] == 1


def test_cached_refetches_after_interval_elapses():
    clock = _Clock()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    c = pollers.Cached(fetch, interval=30, clock=clock)
    assert c.get() == 1
    clock.advance(30)            # exactly at the boundary -> refetch
    assert c.get() == 2
    clock.advance(31)
    assert c.get() == 3
    assert calls["n"] == 3


def test_cached_respects_a_custom_gh_poll_interval():
    clock = _Clock()
    calls = {"n": 0}
    c = pollers.Cached(lambda: calls.__setitem__("n", calls["n"] + 1) or calls["n"],
                       interval=5, clock=clock)
    c.get()
    clock.advance(4)
    c.get()
    assert calls["n"] == 1        # 4s < 5s: still cached
    clock.advance(1)
    c.get()
    assert calls["n"] == 2        # 5s reached: refetched


# ============================ diff_stat: the worktree cargo poller ============================

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)


def _make_repo(path):
    """A real git repo whose ``main`` holds ``a.txt`` = 'keep\\ndrop\\n'."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "a.txt").write_text("keep\ndrop\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "branch", "-m", "main")     # name it main regardless of git's default
    return path


def test_diff_stat_counts_added_and_removed_including_uncommitted(tmp_path):
    wt = _make_repo(tmp_path / "wt")
    _git(wt, "checkout", "-q", "-b", "feature")
    # committed cargo: a.txt loses "drop", gains add1/add2 (+2 -1); b.txt is new (+1)
    (wt / "a.txt").write_text("keep\nadd1\nadd2\n")
    (wt / "b.txt").write_text("new\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "work")
    # uncommitted cargo: another line in b.txt (proves the two-dot diff includes the working tree)
    (wt / "b.txt").write_text("new\nwip\n")

    out = pollers.diff_stat(wt, base_branch="main")
    assert out == {"present": True, "files": 2, "added": 4, "removed": 1}


def test_diff_stat_counts_untracked_new_files(tmp_path):
    # A worker mid-flight often has brand-new files not yet `git add`ed. `git diff` ignores
    # untracked files, so counting only tracked changes would report empty cargo — a lie. The
    # poller must fold untracked files into the cargo (they ARE uncommitted work).
    wt = _make_repo(tmp_path / "wt")
    _git(wt, "checkout", "-q", "-b", "feature")
    (wt / "new.txt").write_text("one\ntwo\n")     # untracked (never staged)
    (wt / "also.txt").write_text("solo")          # untracked, no trailing newline -> 1 line
    out = pollers.diff_stat(wt, base_branch="main")
    assert out == {"present": True, "files": 2, "added": 3, "removed": 0}


def test_diff_stat_untracked_ignores_gitignored_files(tmp_path):
    # .gitignored paths (e.g. .venv) are NOT cargo — --exclude-standard must drop them.
    wt = _make_repo(tmp_path / "wt")
    (wt / ".gitignore").write_text("ignored.txt\n")
    _git(wt, "add", ".gitignore")
    _git(wt, "commit", "-q", "-m", "ignore")
    (wt / "ignored.txt").write_text("noise\nnoise\n")   # untracked but ignored
    (wt / "real.txt").write_text("cargo\n")             # untracked, real
    out = pollers.diff_stat(wt, base_branch="main")
    assert out == {"present": True, "files": 1, "added": 1, "removed": 0}


def test_diff_stat_counts_tracked_and_untracked_together(tmp_path):
    # The full cargo picture: committed edits + uncommitted tracked edits + untracked new files.
    wt = _make_repo(tmp_path / "wt")
    _git(wt, "checkout", "-q", "-b", "feature")
    (wt / "a.txt").write_text("keep\nadd1\nadd2\n")     # tracked: +2 -1
    _git(wt, "add", "-A"); _git(wt, "commit", "-q", "-m", "work")
    (wt / "fresh.txt").write_text("x\ny\nz\n")          # untracked: +3
    out = pollers.diff_stat(wt, base_branch="main")
    assert out == {"present": True, "files": 2, "added": 5, "removed": 1}


def test_diff_stat_untracked_symlink_counts_as_one_line(tmp_path):
    # git stores a symlink as a 1-line blob (the target path) — the poller must NOT follow it and
    # count the linked file's contents.
    wt = _make_repo(tmp_path / "wt")
    (wt / "target.txt").write_text("one\ntwo\nthree\n")   # untracked text: +3
    os.symlink("target.txt", wt / "link.txt")             # untracked symlink: +1 (its blob)
    out = pollers.diff_stat(wt, base_branch="main")
    assert out == {"present": True, "files": 2, "added": 4, "removed": 0}


def test_diff_stat_untracked_binary_counts_file_not_lines(tmp_path):
    wt = _make_repo(tmp_path / "wt")
    (wt / "blob.bin").write_bytes(b"\x00\x01\x02" * 10000)   # NUL -> binary
    assert pollers.diff_stat(wt, base_branch="main") == {
        "present": True, "files": 1, "added": 0, "removed": 0}


def test_diff_stat_untracked_large_text_counts_all_lines_across_chunks(tmp_path):
    wt = _make_repo(tmp_path / "wt")
    (wt / "big.txt").write_text("line\n" * 20000)           # ~100KB: spans multiple read chunks
    assert pollers.diff_stat(wt, base_branch="main") == {
        "present": True, "files": 1, "added": 20000, "removed": 0}


def test_diff_stat_clean_branch_is_present_but_zero(tmp_path):
    wt = _make_repo(tmp_path / "wt")   # still on main, no changes vs merge-base
    assert pollers.diff_stat(wt, base_branch="main") == {
        "present": True, "files": 0, "added": 0, "removed": 0}


def test_diff_stat_absent_worktree_fails_closed(tmp_path):
    missing = tmp_path / "worktrees" / "i9"   # never created (issue not launched / cleaned up)
    assert pollers.diff_stat(missing, base_branch="main") == {
        "present": False, "files": 0, "added": 0, "removed": 0}


def test_diff_stat_non_git_dir_fails_closed(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "x.txt").write_text("hi")
    assert pollers.diff_stat(d, base_branch="main") == {
        "present": False, "files": 0, "added": 0, "removed": 0}


def test_diff_stat_unknown_base_branch_fails_closed(tmp_path):
    wt = _make_repo(tmp_path / "wt")
    # base branch doesn't exist in this worktree -> merge-base fails -> fail closed (not a crash)
    assert pollers.diff_stat(wt, base_branch="does-not-exist") == {
        "present": False, "files": 0, "added": 0, "removed": 0}


# ============================ read_usage: fail-closed usage pill ============================

def _good_raw():
    return {
        "five_hour_pct": 42, "seven_day_pct": 71,
        "five_hour_resets_epoch": 1000, "seven_day_resets_epoch": 2000,
        "auth_status": "ok",
    }


def test_read_usage_known_on_good_data():
    u = pollers.read_usage(fetcher=_good_raw)
    assert u["known"] is True
    assert u["five_hour_pct"] == 42
    assert u["seven_day_pct"] == 71
    assert u["five_hour_resets_epoch"] == 1000
    assert u["status"] == "ok"


def test_read_usage_unknown_when_fetcher_raises():
    def boom():
        raise RuntimeError("keychain exploded")
    u = pollers.read_usage(fetcher=boom)
    assert u["known"] is False
    assert u["five_hour_pct"] is None
    assert u["seven_day_pct"] is None
    assert u["status"] == "unknown"


def test_read_usage_unknown_surfaces_auth_status_reason():
    def expired():
        return {"five_hour_pct": None, "seven_day_pct": None, "auth_status": "auth_expired"}
    u = pollers.read_usage(fetcher=expired)
    assert u["known"] is False
    assert u["five_hour_pct"] is None
    assert u["status"] == "auth_expired"   # the pill can explain WHY, not just "?"


def test_read_usage_unknown_when_pct_missing_despite_ok_status():
    # schema drift: a 200 that renamed a window leaves a pct None — never report a partial number
    def half():
        return {"five_hour_pct": 42, "seven_day_pct": None, "auth_status": "ok"}
    u = pollers.read_usage(fetcher=half)
    assert u["known"] is False
    assert u["five_hour_pct"] is None      # not the stray 42


def test_read_usage_wrong_typed_pct_is_not_reported_ok():
    # A stringly-typed "42" is non-None but non-numeric: known:False is right, but the status must
    # NOT stay "ok" — an unusable-but-"ok" result is self-contradictory and misleads the pill.
    def stringy():
        return {"five_hour_pct": "42", "seven_day_pct": 71, "auth_status": "ok"}
    u = pollers.read_usage(fetcher=stringy)
    assert u["known"] is False
    assert u["status"] != "ok"


def test_read_usage_is_stateless_never_serves_a_stale_number():
    good = pollers.read_usage(fetcher=_good_raw)
    assert good["five_hour_pct"] == 42

    def boom():
        raise RuntimeError("now failing")
    after = pollers.read_usage(fetcher=boom)
    assert after["known"] is False
    assert after["five_hour_pct"] is None  # the earlier 42 is NOT remembered


# ---- fetch_claude_usage: the impure fetch, exercised with injected transport (no network) ----

def test_fetch_usage_ok_parses_both_windows():
    def token():
        return "tok"

    def http_get(url, headers, timeout):
        assert "oauth/usage" in url
        assert headers["Authorization"] == "Bearer tok"
        return {"five_hour": {"utilization": 12, "resets_at": "2026-07-07T20:00:00Z"},
                "seven_day": {"utilization": 55, "resets_at": "2026-07-10T00:00:00Z"}}

    r = pollers.fetch_claude_usage(token_source=token, http_get=http_get)
    assert r["auth_status"] == "ok"
    assert r["five_hour_pct"] == 12
    assert r["seven_day_pct"] == 55
    assert r["five_hour_resets_epoch"] == pollers.iso_to_epoch("2026-07-07T20:00:00Z")


def test_fetch_usage_no_keychain_when_token_source_raises():
    def token():
        raise OSError("no keychain")
    r = pollers.fetch_claude_usage(token_source=token, http_get=lambda *a, **k: {})
    assert r["auth_status"] == "no_keychain"
    assert r["five_hour_pct"] is None


def test_fetch_usage_no_token_when_empty():
    r = pollers.fetch_claude_usage(token_source=lambda: "", http_get=lambda *a, **k: {})
    assert r["auth_status"] == "no_token"


def test_fetch_usage_rate_limited_on_429():
    def http_get(url, headers, timeout):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    r = pollers.fetch_claude_usage(token_source=lambda: "tok", http_get=http_get)
    assert r["auth_status"] == "rate_limited"


def test_fetch_usage_auth_expired_on_401():
    def http_get(url, headers, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    r = pollers.fetch_claude_usage(token_source=lambda: "tok", http_get=http_get)
    assert r["auth_status"] == "auth_expired"


def test_fetch_usage_schema_drift_fails_closed_not_ok():
    def http_get(url, headers, timeout):
        return {"unexpected": "shape"}     # 200 but no windows
    r = pollers.fetch_claude_usage(token_source=lambda: "tok", http_get=http_get)
    assert r["auth_status"] == "api_error"
    assert r["five_hour_pct"] is None


def test_fetch_usage_wrong_typed_utilization_fails_closed_not_ok():
    # A 200 whose utilization is a string / bool is NOT healthy — never call it "ok".
    def stringy(url, headers, timeout):
        return {"five_hour": {"utilization": "12"}, "seven_day": {"utilization": True}}
    r = pollers.fetch_claude_usage(token_source=lambda: "tok", http_get=stringy)
    assert r["auth_status"] == "api_error"


def test_fetch_usage_default_token_source_is_neutralized_in_tests():
    # No injection: exercises the REAL _keychain_token, whose `security` binary the conftest
    # autouse fixture points at an absent path (SL_SECURITY). It must fail closed to no_keychain —
    # proving the default usage path can reach neither the Keychain nor the network in the suite.
    r = pollers.fetch_claude_usage()
    assert r["auth_status"] == "no_keychain"


def test_read_usage_default_path_fails_closed_under_neutralized_security():
    # The public entry with no injection is likewise fail-closed in the suite (unknown sentinel).
    u = pollers.read_usage()
    assert u["known"] is False


def test_iso_to_epoch_parses_and_tolerates_garbage():
    import datetime
    expected = int(datetime.datetime(2026, 7, 7, 20, 0, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
    assert pollers.iso_to_epoch("2026-07-07T20:00:00Z") == expected
    assert pollers.iso_to_epoch(None) is None
    assert pollers.iso_to_epoch("not-a-date") is None
