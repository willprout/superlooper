import json
import urllib.error
from unittest import mock
import usage


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
        return mock.Mock(returncode=1, stdout="", stderr="password not found")
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
        if args[0] == "claude":
            return _claude_status(True)
        return _keychain_attrs(True)

    with mock.patch("usage.subprocess.run", side_effect=_spy):
        usage.probe_auth()
    security_calls = [c for c in calls if c and c[0] == "security"]
    assert security_calls, "probe_auth must read the credential keychain item"
    for c in security_calls:
        assert "-w" not in c, "probe_auth must never dump the keychain secret"


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
