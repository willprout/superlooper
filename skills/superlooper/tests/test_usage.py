import json
import urllib.error
from unittest import mock

import pytest

import identity
import usage


@pytest.fixture(autouse=True)
def pinned_claude(tmp_path, monkeypatch):
    """A resolvable `claude` for every test in this file (issue #350/#323).

    The probe now resolves its binary through the launch stack's own ladder instead of a bare-name
    PATH lookup, and conftest's ratchet pins SL_CLAUDE at a guaranteed-ABSENT path — so without
    this, every probe test below would exercise only the "no runnable claude" branch and never
    reach the status read it is about. This is a real, executable FILE and never a real claude:
    `usage.subprocess.run` is mocked in every test here, so the ladder resolves this path and
    nothing ever runs it.

    The stub EXITS 97 rather than 0, which is the fail-closed half of that promise. A test added
    here later that forgets to patch `usage.subprocess.run` would otherwise run this file, get a
    clean rc, AND shell a real `security find-generic-password` against the owner's login keychain
    — while looking exactly like a test that exercised the CLI half. 97 makes such a test read
    `cli: unknown` instead, which is a failing assertion rather than a silent one.
    """
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nexit 97\n")
    stub.chmod(0o755)
    monkeypatch.setenv("SL_CLAUDE", str(stub))
    return str(stub)


def _keychain_ok(token="tok"):
    payload = json.dumps({"claudeAiOauth": {"accessToken": token}})
    return mock.Mock(returncode=0, stdout=payload)


def _http_ok():
    body = json.dumps({
        "five_hour": {"utilization": 73, "resets_at": "2026-06-24T20:00:00Z"},
        "seven_day": {"utilization": 41, "resets_at": "2026-06-30T00:00:00Z"},
    }).encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = body
    return cm


def test_ok_path():
    with mock.patch("usage.subprocess.run", return_value=_keychain_ok()), \
         mock.patch("usage.urllib.request.urlopen", return_value=_http_ok()):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "ok"
    assert r["five_hour_pct"] == 73
    assert r["seven_day_pct"] == 41
    assert r["five_hour_resets"] == "2026-06-24T20:00:00Z"
    # R4: epoch fields derived from the ISO timestamps (no ISO/epoch mismatch downstream)
    assert r["five_hour_resets_epoch"] == usage.iso_to_epoch("2026-06-24T20:00:00Z")
    assert isinstance(r["five_hour_resets_epoch"], int)


def test_required_headers_present():
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _http_ok()

    with mock.patch("usage.subprocess.run", return_value=_keychain_ok()), \
         mock.patch("usage.urllib.request.urlopen", side_effect=fake_urlopen):
        usage.fetch_claude_usage()
    h = captured["headers"]
    assert h["authorization"].startswith("Bearer ")
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    assert h["user-agent"].startswith("claude-code/")


def test_iso_to_epoch_roundtrip():
    assert usage.iso_to_epoch("1970-01-01T00:00:00Z") == 0
    assert usage.iso_to_epoch(None) is None
    assert usage.iso_to_epoch("garbage") is None


def test_no_keychain():
    with mock.patch("usage.subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "no_keychain"
    assert r["five_hour_pct"] is None


def test_no_token():
    empty = mock.Mock(returncode=0, stdout=json.dumps({"claudeAiOauth": {}}))
    with mock.patch("usage.subprocess.run", return_value=empty):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "no_token"


def test_rate_limited():
    err = urllib.error.HTTPError("u", 429, "rl", {}, None)
    with mock.patch("usage.subprocess.run", return_value=_keychain_ok()), \
         mock.patch("usage.urllib.request.urlopen", side_effect=err):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "rate_limited"


def test_auth_expired():
    err = urllib.error.HTTPError("u", 403, "no", {}, None)
    with mock.patch("usage.subprocess.run", return_value=_keychain_ok()), \
         mock.patch("usage.urllib.request.urlopen", side_effect=err):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "auth_expired"


def test_schema_drift_is_not_ok():
    # RC-USAGEFAILOPEN (producer side): a 200 whose body renamed/omitted the windows must NOT
    # read as healthy 0% — it is api_error so the scheduler fails closed.
    body = json.dumps({"five_hour": {}, "weekly": {"utilization": 41}}).encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = body
    with mock.patch("usage.subprocess.run", return_value=_keychain_ok()), \
         mock.patch("usage.urllib.request.urlopen", return_value=cm):
        r = usage.fetch_claude_usage()
    assert r["auth_status"] == "api_error"
    assert r["five_hour_pct"] is None and r["seven_day_pct"] is None


# ---------------------------------------------------------------------------
# probe_auth (issue #159): the cheap, agent-specific, NEVER-metered auth probe the runner uses to
# gate a launch/recovery spend and to feed the 30-min forensic capture. It runs `claude auth
# status` (a status read) + reads the credential keychain item's mtime (never -w, so the secret is
# never dumped). `valid` is the launch-gating verdict: False ONLY on a DEFINITIVE dead reading
# (CLI not-logged-in, or the keychain item gone); None (unreadable) FAILS OPEN.
# ---------------------------------------------------------------------------
def _claude_status(logged_in=True, extra="", rc=0):
    body = {"loggedIn": logged_in, "authMethod": "claude.ai", "apiProvider": "firstParty"}
    return mock.Mock(returncode=rc, stdout=json.dumps(body) + extra, stderr="")


def _keychain_attrs(present=True, ts="20260716063543"):
    if not present:
        # errSecItemNotFound — the code `security` returns for a missing item (rc 44).
        return mock.Mock(returncode=44, stdout="", stderr="password not found")
    attrs = (
        '    "acct"<blob>="willprout"\n'
        f'    "cdat"<timedate>=0x3230 "20260326231749Z\\000"\n'
        f'    "mdat"<timedate>=0x3230 "{ts}Z\\000"\n'
        '    "svce"<blob>="Claude Code-credentials"\n'
    )
    return mock.Mock(returncode=0, stdout=attrs, stderr="")


def test_probe_auth_logged_in_and_keychain_present():
    with mock.patch("usage.subprocess.run",
                    side_effect=[_claude_status(True), _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "logged_in"
    assert r["keychain_present"] is True
    assert r["keychain_mtime"] == usage._keychain_ts_to_epoch("20260716063543")
    assert isinstance(r["keychain_mtime"], int)
    assert r["valid"] is True


def test_probe_auth_logged_out_is_definitively_invalid():
    with mock.patch("usage.subprocess.run",
                    side_effect=[_claude_status(False), _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "logged_out"
    assert r["valid"] is False          # the launch gate blocks on this


def test_probe_auth_missing_keychain_is_definitively_invalid():
    # The credential item is gone: a fresh launch has no creds to read -> dead auth, even if the
    # CLI status read is somehow unreadable.
    with mock.patch("usage.subprocess.run",
                    side_effect=[OSError("no claude"), _keychain_attrs(present=False)]):
        r = usage.probe_auth()
    assert r["keychain_present"] is False
    assert r["valid"] is False


def test_probe_auth_unreadable_cli_with_creds_fails_open():
    # `claude` won't run (binary missing / hang) but the keychain item is present: we CANNOT prove
    # auth is dead, so valid is None -> the caller fails OPEN (never freeze the whole loop on a
    # probe we merely couldn't run — the #46/#76 dark-meter asymmetry, applied to auth).
    with mock.patch("usage.subprocess.run",
                    side_effect=[OSError("no claude"), _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "unknown"
    assert r["valid"] is None


def test_probe_auth_non_json_logged_out_phrase():
    # An older/newer CLI that prints prose instead of JSON must still be read as logged-out on the
    # stable auth-death phrase — a render change must not silently reopen the i336 hole.
    prose = mock.Mock(returncode=1,
                      stdout="Not logged in. Run claude auth login to authenticate.", stderr="")
    with mock.patch("usage.subprocess.run",
                    side_effect=[prose, _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "logged_out"
    assert r["valid"] is False


def test_probe_auth_never_dumps_the_secret():
    # The keychain read must query ATTRIBUTES only — never `-w` (which prints the OAuth token).
    calls = []

    def _spy(args, **kw):
        calls.append(list(args))
        # NOT `args[0] == "claude"`: since #350 the probe runs the ladder-RESOLVED path, so a
        # bare-name test would answer the status read with keychain attributes and quietly stop
        # exercising the branch it reads as exercising.
        if args[0] != "security":
            return _claude_status(True)
        return _keychain_attrs(True)

    with mock.patch("usage.subprocess.run", side_effect=_spy):
        usage.probe_auth()
    security_calls = [c for c in calls if c and c[0] == "security"]
    assert security_calls, "probe_auth must read the credential keychain item"
    for c in security_calls:
        assert "-w" not in c, "probe_auth must never dump the keychain secret"


def test_probe_auth_json_with_a_trailing_banner_stays_logged_in():
    # A healthy JSON status followed by a trailing stdout banner (an update / token-refresh notice)
    # must NOT drop to the prose fallback and match its "run claude auth login" phrase -> a false
    # logged-out block on a live account (fresh-review P1). raw_decode reads the LEADING JSON.
    trailing = _claude_status(True, extra="\nRun `claude auth login` to refresh your expiring token.")
    with mock.patch("usage.subprocess.run",
                    side_effect=[trailing, _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "logged_in" and r["valid"] is True


def test_probe_auth_json_false_with_trailing_text_stays_logged_out():
    trailing = _claude_status(False, extra="\nsome trailing note")
    with mock.patch("usage.subprocess.run",
                    side_effect=[trailing, _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert r["cli"] == "logged_out" and r["valid"] is False


def test_probe_auth_other_security_error_is_unknown_not_absent():
    # errSecItemNotFound (rc 44) is definitive-absent -> block; ANY OTHER nonzero rc is a read we
    # cannot trust (keychain DB error, keychain not in the search list) -> UNKNOWN -> fail open.
    other = mock.Mock(returncode=1, stdout="", stderr="SecKeychainSearchCopyNext error")
    with mock.patch("usage.subprocess.run",
                    side_effect=[_claude_status(True), other]):
        r = usage.probe_auth()
    assert r["keychain_present"] is None
    assert r["valid"] is True                # cli says logged_in, keychain unknown -> not blocked
    # ...and when the CLI is ALSO unreadable, an untrusted keychain read stays fail-open (None).
    with mock.patch("usage.subprocess.run",
                    side_effect=[OSError("no claude"), other]):
        r = usage.probe_auth()
    assert r["keychain_present"] is None and r["valid"] is None


def test_probe_auth_status_raw_is_bounded():
    big = _claude_status(True, extra=" " + "x" * 5000)
    with mock.patch("usage.subprocess.run",
                    side_effect=[big, _keychain_attrs(True)]):
        r = usage.probe_auth()
    assert len(r["status_raw"]) <= 1000


def test_keychain_ts_to_epoch_roundtrip():
    assert usage._keychain_ts_to_epoch("19700101000000") == 0
    assert usage._keychain_ts_to_epoch("garbage") is None
    assert usage._keychain_ts_to_epoch(None) is None


# ---------------------------------------------------------------------------
# #350: BOTH readers ask about the account a WORKER runs as, not the machine's default login.
#
# #314 put a per-worker Claude config dir on the spawn seam: when a machine sets
# SL_FLEET_CLAUDE_CONFIG_DIR, every session runs under THAT dir's credential namespace. These two
# readers predate that seam. The mechanism is not just an env var — `claude` derives its keychain
# service name as `Claude Code-credentials-<8 hex of sha256(CLAUDE_CONFIG_DIR)>` (#300), so a
# reader pointed at a config dir must look in that dir's OWN item; asking the unsuffixed one
# answers about the owner. On a fleet machine that is a different rate-limit pool, which is exactly
# the capacity determinism the owner's #314 ruling asked for.
# ---------------------------------------------------------------------------
_FLEET_DIR = "/Users/tester/.claude-fleet"
_OWNER_SERVICE = "Claude Code-credentials"


def _fleet_service():
    return "%s-%s" % (_OWNER_SERVICE, identity.namespace_suffix(_FLEET_DIR))


def _service_of(argv):
    """The keychain service name a `security` argv named, or None for any other command."""
    if not argv or argv[0] != "security" or "-s" not in argv:
        return None
    return argv[argv.index("-s") + 1]


class _Spy:
    """Records every argv (and the env it was handed) the reader shells out with."""

    def __init__(self, answer):
        self.calls, self.envs, self._answer = [], [], answer

    def __call__(self, args, **kw):
        argv = list(args)
        self.calls.append(argv)
        self.envs.append(kw.get("env"))
        return self._answer(argv)

    def env_for(self, command):
        for argv, env in zip(self.calls, self.envs):
            if argv and argv[0] == command:
                return env
        return None

    def services(self):
        return [s for s in (_service_of(c) for c in self.calls) if s]


def test_the_keychain_suffix_is_the_launch_seams_own_derivation():
    # ONE derivation of the namespace suffix for the whole engine: `identity.namespace_suffix` is
    # what the launch seam refuses a mis-spelled config dir with, and a second copy here would be a
    # second answer to "which keychain item holds this dir's login".
    assert usage.credential_service(_FLEET_DIR) == _fleet_service()
    # No assignment -> the bare, unsuffixed item, which is exactly what `claude` itself falls back
    # to. A whitespace-only value is "no assignment" too, never its own namespace.
    assert usage.credential_service(None) == _OWNER_SERVICE
    assert usage.credential_service("   ") == _OWNER_SERVICE


def test_probe_auth_asks_the_assigned_namespace_on_both_halves(monkeypatch):
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", _FLEET_DIR)
    spy = _Spy(lambda argv: _keychain_attrs(True) if argv[0] == "security" else _claude_status(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    # half one: the CLI status read runs under the assigned dir, so it reports THAT login...
    assert spy.env_for(pinned_claude_path(spy))["CLAUDE_CONFIG_DIR"] == _FLEET_DIR
    # ...half two: the credential item it reads is that dir's own, never the owner's unsuffixed one.
    assert spy.services() == [_fleet_service()]
    assert r["config_dir"] == _FLEET_DIR       # the snapshot says which account it is about
    assert r["valid"] is True


def pinned_claude_path(spy):
    """The claude argv[0] the spy actually saw — the ladder-resolved path, not the bare name."""
    for argv in spy.calls:
        if argv and argv[0] != "security":
            return argv[0]
    raise AssertionError("the probe never ran a `claude auth status` read at all")


def test_probe_auth_with_no_assignment_reads_the_default_namespace_as_today():
    # The off-the-fleet path must be untouched: the unsuffixed item, and a status read with no
    # config dir named at all (ABSENT, never empty — an empty CLAUDE_CONFIG_DIR is its own
    # namespace, `sha256("")`, not "the default one").
    spy = _Spy(lambda argv: _keychain_attrs(True) if argv[0] == "security" else _claude_status(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    assert spy.services() == [_OWNER_SERVICE]
    assert "CLAUDE_CONFIG_DIR" not in spy.env_for(pinned_claude_path(spy))
    assert r["config_dir"] is None
    assert r["cli"] == "logged_in" and r["valid"] is True


def test_probe_auth_never_reads_the_owners_item_for_the_assigned_namespace(monkeypatch):
    # The defect, stated as a test: the OWNER's credential item is healthy and the ASSIGNED
    # namespace's is gone. Today's reader asked the unsuffixed item and reported a live account —
    # the owner's answer read as this one's. It must read the namespace the workers actually use.
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", _FLEET_DIR)

    def _answer(argv):
        if argv[0] == "security":
            return _keychain_attrs(_service_of(argv) == _OWNER_SERVICE)
        return _claude_status(False)

    spy = _Spy(_answer)
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    assert spy.services() == [_fleet_service()]
    assert r["keychain_present"] is False
    assert r["valid"] is False              # definitive for THAT namespace; the gate's rule is unchanged


def test_probe_auth_unusable_assignment_is_unknown_rather_than_the_owners_answer(monkeypatch):
    # A machine that NAMES a config dir this reader cannot turn into a namespace has no account it
    # could honestly ask about. Falling back to the unsuffixed item would be a confident answer to
    # a different question — the one outcome worse than "unknown" for a gate that holds the queue.
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", "relative/dir")
    spy = _Spy(lambda argv: _keychain_attrs(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    assert spy.calls == []                  # nothing was asked of anything
    assert r["cli"] == "unknown"
    assert r["keychain_present"] is None and r["keychain_mtime"] is None
    assert r["valid"] is None               # fail open — a dark probe never freezes the loop
    assert "relative" in (r["note"] or "")


def test_probe_auth_runs_the_binary_the_launch_stack_would(pinned_claude):
    # #323, absorbed into #350: the one reader whose answer gates spend used to resolve its
    # `claude` by bare PATH order while every worker resolves by configuration (SL_CLAUDE -> the
    # standalone install -> PATH). Two machines could disagree about which claude is authoritative.
    spy = _Spy(lambda argv: _keychain_attrs(True) if argv[0] == "security" else _claude_status(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        usage.probe_auth()
    assert [c for c in spy.calls if c[0] != "security"] == [[pinned_claude, "auth", "status"]]
    assert not any(c[0] == "claude" for c in spy.calls), "never a bare-name PATH lookup"


def test_probe_auth_with_no_runnable_claude_stays_fail_open_unknown(monkeypatch):
    # The ladder's fail-closed pin refusal reaches this reader too: a pin naming nothing runnable
    # is never quietly downgraded to PATH. There is then no account read to make — but the keychain
    # half is independent of the binary and still answers, exactly as it does today when `claude`
    # cannot be spawned.
    monkeypatch.setenv("SL_CLAUDE", "/nonexistent/superlooper-test-claude")
    spy = _Spy(lambda argv: _keychain_attrs(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    assert [c[0] for c in spy.calls] == ["security"]
    assert r["cli"] == "unknown" and r["valid"] is None
    assert "SL_CLAUDE" in (r["note"] or "")


def test_an_unresolvable_claude_does_not_reset_the_keychain_half_to_the_owner(monkeypatch):
    # The two halves are independent, and this is the branch where that could go wrong quietly:
    # the binary is gone, so the CLI half cannot answer — but the credential item still must be the
    # ASSIGNED namespace's. A regression that let the keychain read fall back to the unsuffixed
    # item here would report the owner's live login as this fleet's, on exactly the machines that
    # are half-broken already.
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", _FLEET_DIR)
    monkeypatch.setenv("SL_CLAUDE", "/nonexistent/superlooper-test-claude")
    spy = _Spy(lambda argv: _keychain_attrs(True))
    with mock.patch("usage.subprocess.run", side_effect=spy):
        r = usage.probe_auth()
    assert spy.services() == [_fleet_service()]
    assert r["cli"] == "unknown" and r["valid"] is None
    assert r["config_dir"] == _FLEET_DIR
    # ...and the note must not carry the phrase lib/evidence.py reads as a CHANNEL fault, which
    # would turn one issue's park into a held queue if this text ever reached launcher stderr.
    assert "could not resolve" not in (r["note"] or "")


def test_fetch_usage_meters_the_assigned_namespaces_pool(monkeypatch):
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", _FLEET_DIR)
    spy = _Spy(lambda argv: _keychain_ok())
    with mock.patch("usage.subprocess.run", side_effect=spy), \
         mock.patch("usage.urllib.request.urlopen", return_value=_http_ok()):
        r = usage.fetch_claude_usage()
    assert spy.services() == [_fleet_service()]
    assert r["auth_status"] == "ok" and r["five_hour_pct"] == 73
    assert r["config_dir"] == _FLEET_DIR      # which pool these numbers describe, on the record


def test_fetch_usage_with_no_assignment_meters_the_default_pool_as_today():
    spy = _Spy(lambda argv: _keychain_ok())
    with mock.patch("usage.subprocess.run", side_effect=spy), \
         mock.patch("usage.urllib.request.urlopen", return_value=_http_ok()):
        r = usage.fetch_claude_usage()
    assert spy.services() == [_OWNER_SERVICE]
    assert r["auth_status"] == "ok" and r["config_dir"] is None


def test_fetch_usage_reports_unknown_rather_than_another_accounts_numbers(monkeypatch):
    # The scheduler paces lanes on this read. With the assigned namespace's credential item absent
    # and the owner's present, the old reader returned the OWNER's utilisation — pacing the fleet's
    # lanes against a pool they are not spending. Report the dark meter instead; `no_keychain` is
    # not `ok`, so the scheduler's existing fail-closed-then-grace path takes it from here.
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", _FLEET_DIR)

    def _answer(argv):
        if _service_of(argv) == _OWNER_SERVICE:
            return _keychain_ok("the-owners-token")
        return mock.Mock(returncode=44, stdout="")

    spy = _Spy(_answer)
    with mock.patch("usage.subprocess.run", side_effect=spy), \
         mock.patch("usage.urllib.request.urlopen", return_value=_http_ok()) as opened:
        r = usage.fetch_claude_usage()
    assert spy.services() == [_fleet_service()]
    assert r["auth_status"] == "no_keychain"
    assert r["five_hour_pct"] is None and r["seven_day_pct"] is None
    assert not opened.called, "no token, so no call — never the owner's numbers under this label"


def test_fetch_usage_unusable_assignment_never_reads_the_owners_item(monkeypatch):
    monkeypatch.setenv("SL_FLEET_CLAUDE_CONFIG_DIR", "relative/dir")
    spy = _Spy(lambda argv: _keychain_ok("the-owners-token"))
    with mock.patch("usage.subprocess.run", side_effect=spy), \
         mock.patch("usage.urllib.request.urlopen", return_value=_http_ok()) as opened:
        r = usage.fetch_claude_usage()
    assert spy.calls == [] and not opened.called
    assert r["auth_status"] == "namespace_unknown"
    assert r["five_hour_pct"] is None and r["seven_day_pct"] is None
    # The reason travels with the verdict: this is where an operator's typo lands, and it leaves
    # the meter dark until someone reads the state file and understands why.
    assert "not an absolute path" in (r["note"] or "")
