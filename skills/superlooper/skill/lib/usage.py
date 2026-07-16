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
import re
import subprocess
import urllib.request
import urllib.error

USER_AGENT_VERSION = "2.1.90"  # keep current-ish or the endpoint 403s

# The macOS login-keychain item Claude Code stores its OAuth credentials in. Agent-specific by
# construction (this whole file is inside the agent boundary), and shared with fetch_claude_usage
# below so the probe and the meter read the SAME credential.
CRED_KEYCHAIN_SERVICE = "Claude Code-credentials"


def iso_to_epoch(iso):
    """'2026-06-24T20:00:00Z' -> int epoch seconds. None on None/garbage (never raises)."""
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return int(datetime.datetime.fromisoformat(s).timestamp())
    except (ValueError, AttributeError):
        return None


# The `security` <timedate> attribute renders as e.g. "20260716063543Z" (UTC). Its epoch is the
# credential keychain item's modification time — the piece the i336 forensics (U3) needed and could
# not get from disk: a token that silently rotated/expired mid-run would move this mtime.
_KEYCHAIN_MDAT = re.compile(r'"mdat"<timedate>=\S*\s+"(\d{14})Z', re.I)
# The stable auth-death phrases Claude Code prints when it is NOT in a JSON-status build. Kept in
# sync (in spirit) with pane_state._LOGGED_OUT_PATTERNS — the in-window siblings of this account
# probe — so a prose render still reads as logged-out and never silently reopens i336.
_CLI_LOGGED_OUT = re.compile(r"not logged in|please run /login|run claude auth login|logged out",
                             re.I)
# `security`'s exit code for errSecItemNotFound — the DEFINITIVE "the credential item is gone" case.
# Any OTHER nonzero rc (a keychain DB error, the login keychain not in the search list) is a read we
# could not TRUST, not proof of absence, so it fails OPEN (unknown) rather than blocking every launch.
_SEC_ITEM_NOT_FOUND = 44


def _keychain_ts_to_epoch(s):
    """'20260716063543' (a `security` <timedate>, UTC) -> int epoch seconds. None on None/garbage
    (never raises)."""
    try:
        dt = datetime.datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _credential_keychain_state(timeout=5):
    """(present, mtime_epoch) for the Claude Code credential keychain item, reading its ATTRIBUTES
    only — never `-w`, so the OAuth secret is never dumped. present is False on a definitive absence
    (nonzero rc), None when `security` could not be run at all (fail-open ambiguity), True otherwise.
    mtime_epoch is the item's `mdat` in epoch seconds, or None."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", CRED_KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None, None
    if r.returncode == _SEC_ITEM_NOT_FOUND:
        return False, None                     # item is GONE: a fresh launch would have no creds
    if r.returncode != 0:
        return None, None                      # some other security error -> UNKNOWN (fail open)
    m = _KEYCHAIN_MDAT.search((r.stdout or "") + (r.stderr or ""))
    return True, (_keychain_ts_to_epoch(m.group(1)) if m else None)


def probe_auth(timeout=5) -> dict:
    """The cheap, agent-specific, NEVER-metered auth probe (issue #159 / forensics U3).

    Runs `claude auth status` (a STATUS read — not a headless `claude -p` session, so it is inside
    the owner's no-headless-metering rule) and reads the credential keychain item's mtime. Returns:
        cli              -> "logged_in" | "logged_out" | "unknown"
        keychain_present -> True | False | None
        keychain_mtime   -> int epoch seconds | None
        status_raw       -> the (bounded) `claude auth status` text, for the forensic capture
        valid            -> True | False | None
    `valid` is the launch-gating verdict: False ONLY on a DEFINITIVE dead reading (the CLI reports
    not-logged-in, or the credential keychain item is gone) — those are exactly the states in which a
    fresh launch or a recovery relaunch would start LOGGED OUT and burn the spend (the i336 class).
    Anything unreadable (binary missing, hang, unrecognized output, keychain unreadable) -> None ->
    the caller FAILS OPEN: a probe we merely could not run must never freeze the whole loop (the
    #46/#76 dark-meter asymmetry, applied to auth)."""
    cli = "unknown"
    status_raw = ""
    try:
        r = subprocess.run(["claude", "auth", "status"],
                           capture_output=True, text=True, timeout=timeout)
        status_raw = ((r.stdout or "") + (r.stderr or "")).strip()
        parsed = None
        try:
            # raw_decode reads a LEADING JSON object and IGNORES any trailing text (an update
            # banner / token-refresh notice printed after the status blob). A plain json.loads would
            # raise 'Extra data' on that trailing text and drop a healthy logged-IN read to the prose
            # fallback below — which could then match an auth-login phrase inside that very banner and
            # FALSELY read logged-out, freezing launches on a live account (fresh-review P1). So JSON
            # wins whenever the output STARTS with it; the prose fallback is only for non-JSON renders.
            parsed, _ = json.JSONDecoder().raw_decode((r.stdout or "").lstrip())
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("loggedIn"), bool):
            cli = "logged_in" if parsed["loggedIn"] else "logged_out"
        elif _CLI_LOGGED_OUT.search(status_raw):
            cli = "logged_out"                 # non-JSON render: the stable auth-death phrase
    except Exception:
        cli = "unknown"                        # binary missing / hang -> unknown, never invalid

    keychain_present, keychain_mtime = _credential_keychain_state(timeout=timeout)

    if cli == "logged_out" or keychain_present is False:
        valid = False                          # definitive: block the spend, alert the owner
    elif cli == "logged_in":
        valid = True
    else:
        valid = None                           # unknown -> fail open (never block on a dark probe)

    return {"cli": cli, "keychain_present": keychain_present,
            "keychain_mtime": keychain_mtime, "valid": valid,
            "status_raw": status_raw[:1000]}


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
            ["security", "find-generic-password", "-s", CRED_KEYCHAIN_SERVICE, "-w"],
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
