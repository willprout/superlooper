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
