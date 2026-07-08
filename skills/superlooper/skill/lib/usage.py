#!/usr/bin/env python3
"""Read Claude Code/Max subscription usage (5-hour + 7-day windows).

Proven recipe (macOS only): reads the local Claude Code OAuth token from the Keychain
and calls the undocumented OAuth usage endpoint the client uses for itself. This reuses
William's own local token in the same call Claude Code already makes — it adds no new
outbound exposure. There is no public/documented API for this; this is the endpoint.

If the endpoint starts returning 401/403 for no obvious reason, bump USER_AGENT_VERSION
below to the current `claude-code/<version>` (a stale User-Agent is silently refused).
"""
import datetime
import json
import subprocess
import urllib.request
import urllib.error

USER_AGENT_VERSION = "2.1.90"  # keep current-ish or the endpoint 403s


def iso_to_epoch(iso):
    """'2026-06-24T20:00:00Z' -> int epoch seconds. None on None/garbage (never raises)."""
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return int(datetime.datetime.fromisoformat(s).timestamp())
    except (ValueError, AttributeError):
        return None


def fetch_claude_usage() -> dict:
    """Fetch Claude Code/Max subscription usage (5-hour and weekly windows).

    Returns a dict with:
      five_hour_pct, seven_day_pct  -> utilization percentages (0-100)
      five_hour_resets, seven_day_resets -> ISO reset timestamps
      five_hour_resets_epoch, seven_day_resets_epoch -> int epoch seconds (R4)
      auth_status -> ok | no_keychain | no_token | rate_limited | auth_expired | api_error
    All numeric values are None on failure. macOS only (reads the Keychain).
    """
    result = {
        "five_hour_pct": None, "seven_day_pct": None,
        "five_hour_resets": None, "seven_day_resets": None,
        "five_hour_resets_epoch": None, "seven_day_resets_epoch": None,
        "auth_status": "unknown",
    }
    try:
        # 1. Pull the Claude Code OAuth token from the macOS Keychain.
        token_raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if token_raw.returncode != 0:
            result["auth_status"] = "no_keychain"
            return result
        creds = json.loads(token_raw.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            result["auth_status"] = "no_token"
            return result

        # 2. Call the undocumented OAuth usage endpoint. All three headers required.
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": f"claude-code/{USER_AGENT_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        # 3. Parse the two usage windows.
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}
        result["five_hour_pct"] = fh.get("utilization")
        result["seven_day_pct"] = sd.get("utilization")
        result["five_hour_resets"] = fh.get("resets_at")
        result["seven_day_resets"] = sd.get("resets_at")
        result["five_hour_resets_epoch"] = iso_to_epoch(result["five_hour_resets"])
        result["seven_day_resets_epoch"] = iso_to_epoch(result["seven_day_resets"])
        # Schema-drift defense (RC-USAGEFAILOPEN, producer side): a 200 whose body renamed/omitted
        # the windows would leave the pcts None. Do NOT report that as healthy 'ok' (the scheduler
        # would then fail closed anyway, but mark it here so the cause is visible and last-good
        # staleness logic is correct). Fail closed: api_error.
        if result["five_hour_pct"] is None or result["seven_day_pct"] is None:
            result["auth_status"] = "api_error"
        else:
            result["auth_status"] = "ok"
    except urllib.error.HTTPError as e:
        if e.code == 429:
            result["auth_status"] = "rate_limited"       # back off and retry later
        elif e.code in (401, 403):
            result["auth_status"] = "auth_expired"         # token expired; user must re-login
        else:
            result["auth_status"] = "api_error"
    except Exception:
        result["auth_status"] = "api_error"
    return result


if __name__ == "__main__":
    print(json.dumps(fetch_claude_usage(), indent=2))
