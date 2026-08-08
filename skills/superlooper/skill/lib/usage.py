#!/usr/bin/env python3
"""Read Claude Code/Max subscription usage (5-hour + 7-day windows), and probe account auth.

Proven recipe (macOS only): reads the local Claude Code OAuth token from the Keychain
and calls the undocumented OAuth usage endpoint the client uses for itself. This reuses
William's own local token in the same call Claude Code already makes — it adds no new
outbound exposure. There is no public/documented API for this; this is the endpoint.

If the endpoint starts returning 401/403 for no obvious reason, bump USER_AGENT_VERSION
below to the current `claude-code/<version>` (a stale User-Agent is silently refused).

**WHICH ACCOUNT both readers ask about (issue #350).** They predate the #314 spawn seam, and were
written when there was only one Claude login on a machine. There no longer is: a machine that sets
``SL_FLEET_CLAUDE_CONFIG_DIR`` runs every worker under that dir's credential namespace, and the two
Max accounts are separate RATE-LIMIT POOLS. So both readers here derive the worker's config dir
(``identity.worker_config_dir`` — the same derivation the launch path uses) and aim at it, because
asking the machine's default login would mean the auth gate holds (or fails to hold) the queue on
an account no worker runs as, and the scheduler paces lanes against a pool the workers are not
spending.

The mechanism is not just an env var, which is why `credential_service` exists: ``claude`` derives
its macOS keychain service name as ``Claude Code-credentials-<8 hex>`` where the suffix is
``sha256`` of the ``CLAUDE_CONFIG_DIR`` string as written (#300's derivation, re-derived once in
``identity.namespace_suffix``). A reader pointed at a config dir must look in THAT dir's own item;
the unsuffixed one answers about the owner.
"""
import datetime
import json
import re
import subprocess
import urllib.request
import urllib.error

# The launch seam's own identity contract (#314): where the worker's config dir comes from, how its
# credential namespace is derived, and which `claude` this stack runs. Imported rather than
# re-derived — a second copy of any of those three is a second answer to "which account is this".
# Both modules are agent-specific and ship together in `lib/`.
import identity

USER_AGENT_VERSION = "2.1.90"  # keep current-ish or the endpoint 403s

# The macOS login-keychain item Claude Code stores its OAuth credentials in when NO config dir is
# assigned — the owner's default, unsuffixed namespace. The base string is `identity`'s, not a copy:
# that module's mismatch memo names suffixed items too, and a rename that reached only one of us
# would leave the other confidently pointing an operator at a keychain item that does not exist.
# Every read below goes through `credential_service`, so the probe and the meter can never end up
# asking about two different accounts.
CRED_KEYCHAIN_SERVICE = identity.CREDENTIAL_SERVICE


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


def credential_service(config_dir=None):
    """The keychain service name the credentials for THIS config dir live under.

    ``claude`` appends an 8-hex namespace suffix derived from the ``CLAUDE_CONFIG_DIR`` string and
    falls back to the bare name when none is set (#300), so this is the whole difference between
    reading the account a worker runs as and reading the owner's. The suffix comes from
    ``identity.namespace_suffix`` — the same derivation the launch seam refuses a mis-spelled
    config dir with — because two copies of it would be two answers to which item holds a login.

    A blank/whitespace value is "no assignment", never its own namespace: an EMPTY config dir does
    hash to its own namespace in the binary, but no launch path here ever emits one (``identity``
    treats empty as absent throughout), so treating it as the default is the reading that matches
    what a worker would actually get.

    That last rule is why this is NOT the same function as the one behind ``identity.env_problem``'s
    mismatch memo, which spells the suffixed name for a value a session INHERITED. There, a
    set-but-empty ``CLAUDE_CONFIG_DIR`` really does land in ``sha256("")``'s own namespace and the
    memo must say so; here, blank can only mean "this machine assigned nothing". Two questions, two
    answers — what they share, and all they may share, is ``namespace_suffix``.
    """
    if isinstance(config_dir, str) and config_dir.strip():
        return "%s-%s" % (CRED_KEYCHAIN_SERVICE, identity.namespace_suffix(config_dir))
    return CRED_KEYCHAIN_SERVICE


def _credential_keychain_state(service, timeout=5):
    """(present, mtime_epoch) for the Claude Code credential keychain item, reading its ATTRIBUTES
    only — never `-w`, so the OAuth secret is never dumped. present is False ONLY on a DEFINITIVE
    absence (rc == errSecItemNotFound); None (fail-open ambiguity) when `security` could not be run
    at all OR returned some other error we cannot trust as proof of absence; True otherwise.
    mtime_epoch is the item's `mdat` in epoch seconds, or None.

    `service` is the namespace to ask about (see `credential_service`), and it is REQUIRED rather
    than defaulted, because "absent" is a DEFINITIVE verdict here: read against the wrong service
    name it would report the fleet's login as gone, or the owner's as alive, with equal
    confidence. A default would make that the quiet outcome of forgetting an argument."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None, None
    if r.returncode == _SEC_ITEM_NOT_FOUND:
        return False, None                     # item is GONE: a fresh launch would have no creds
    if r.returncode != 0:
        return None, None                      # some other security error -> UNKNOWN (fail open)
    m = _KEYCHAIN_MDAT.search((r.stdout or "") + (r.stderr or ""))
    return True, (_keychain_ts_to_epoch(m.group(1)) if m else None)


def _worker_namespace(env):
    """``(config dir or None, problem or None)`` — the namespace a WORKER on this machine runs in.

    A thin guard around `identity.worker_config_dir` for ONE reason: both readers below used to be
    total functions (every path returned a dict; nothing propagated), and their callers rely on
    that — the runner's usage refresh keeps last-good on an exception, and the watchdog's read
    swallows one. Introducing a call that can raise would move a decision the callers make
    deliberately into an accident of where the exception happened to land. A derivation we could
    not even run is treated as an unusable assignment, which is exactly what it is.
    """
    try:
        return identity.worker_config_dir(env)
    except Exception as e:                                   # pragma: no cover - defensive
        return None, "the machine's worker config dir could not be derived (%s)" % e


def probe_auth(timeout=5, env=None) -> dict:
    """The cheap, agent-specific, NEVER-metered auth probe (issue #159 / forensics U3).

    Runs `claude auth status` (a STATUS read — not a headless `claude -p` session, so it is inside
    the owner's no-headless-metering rule) and reads the credential keychain item's mtime. Returns:
        cli              -> "logged_in" | "logged_out" | "unknown"
        keychain_present -> True | False | None
        keychain_mtime   -> int epoch seconds | None
        status_raw       -> the (bounded) `claude auth status` text, for the forensic capture
        valid            -> True | False | None
        config_dir       -> the config dir this reading is ABOUT; None means the machine's DEFAULT
                            login, EXCEPT on the one branch where there was no namespace to read
                            at all (an assignment this machine names but cannot use). Read the
                            `note` TEXT to tell those apart — it opens "the config dir this
                            machine assigns workers cannot be used" for the second and "the
                            `claude` this stack would launch is not available" for the other reason
                            a note exists. The shape of the rest of the snapshot does NOT
                            distinguish them: a machine with no assignment, a broken binary pin and
                            an unreadable keychain produces an identical all-empty reading, because
                            the keychain half fails OPEN to None on an untrusted `security` error.
        note             -> why a half could not be asked, when one could not (else "")
    `valid` is the launch-gating verdict: False ONLY on a DEFINITIVE dead reading (the CLI reports
    not-logged-in, or the credential keychain item is gone) — those are exactly the states in which a
    fresh launch or a recovery relaunch would start LOGGED OUT and burn the spend (the i336 class).
    Anything unreadable (binary missing, hang, unrecognized output, keychain unreadable) -> None ->
    the caller FAILS OPEN: a probe we merely could not run must never freeze the whole loop (the
    #46/#76 dark-meter asymmetry, applied to auth).

    ISSUE #350 changed WHICH ACCOUNT is read and WHICH BINARY is asked; the verdict rules above are
    untouched. Both halves now aim at the config dir this machine assigns workers, and the binary
    comes from the launch stack's own ladder (`identity.resolve_claude`) rather than from bare PATH
    order — the one reader whose answer gates spend used to resolve by luck while every worker
    resolved by configuration (#323).
    """
    # ONE derivation for both halves — a probe whose CLI read and keychain read could disagree
    # about the namespace would be two measurements presented as one.
    config_dir, dir_problem = _worker_namespace(env)
    cli = "unknown"
    status_raw = ""
    note = ""

    if dir_problem:
        # This machine NAMES a config dir that cannot be turned into a credential namespace, so
        # there is no account this probe could honestly ask about. Falling back to the unsuffixed
        # item would answer about the OWNER — a confident answer to a different question, which is
        # the one outcome worse than "unknown" for a gate that holds the whole queue. Nothing is
        # asked; every field stays at its fail-open value and `note` says why. (The same fault
        # refuses every launch at the #314 floor with a memo naming the remedy, so this is not the
        # only place it surfaces — it is the place it must not be papered over.)
        note = ("the config dir this machine assigns workers cannot be used, so which account they "
                "run as is unknown: %s" % dir_problem)
        return {"cli": cli, "keychain_present": None, "keychain_mtime": None, "valid": None,
                "status_raw": "", "config_dir": None, "note": note[:1000]}

    try:
        claude, why, _deferrable = identity.resolve_claude(env)
    except Exception as e:                                   # pragma: no cover - defensive
        claude, why = None, "the claude ladder could not be walked (%s)" % e
    if claude is None:
        # No runnable claude: report the existing fail-open unknown for the CLI half rather than a
        # confident answer. The keychain half below is independent of the binary and still answers,
        # exactly as it did before when spawning `claude` raised.
        #
        # WORDED to avoid the substring `could not resolve`, for the same reason
        # `identity.resolve_claude`'s own reason table is: lib/evidence.py matches that phrase as a
        # CHANNEL fault (a network needle), and a stray match there converts one issue's park into
        # a HELD QUEUE. This note reaches only the auth snapshot today, not launcher stderr — but
        # the distance between "inert" and "live" here is one copy-paste.
        note = ("the `claude` this stack would launch is not available, so no account status was "
                "read: %s" % why)
    else:
        try:
            r = subprocess.run([claude, "auth", "status"], capture_output=True, text=True,
                               timeout=timeout,
                               # The ONE difference from our own environment is the config dir (and
                               # the credential-redirect variable, which `probe_env` drops so this
                               # read measures the namespace the dir names and not some other one).
                               # HOME is deliberately left alone — overriding it breaks macOS
                               # keychain OAuth outright (c25's landmine).
                               env=identity.probe_env(env, config_dir))
            status_raw = ((r.stdout or "") + (r.stderr or "")).strip()
            parsed = None
            try:
                # raw_decode reads a LEADING JSON object and IGNORES any trailing text (an update
                # banner / token-refresh notice printed after the status blob). A plain json.loads
                # would raise 'Extra data' on that trailing text and drop a healthy logged-IN read
                # to the prose fallback below — which could then match an auth-login phrase inside
                # that very banner and FALSELY read logged-out, freezing launches on a live account
                # (fresh-review P1). So JSON wins whenever the output STARTS with it; the prose
                # fallback is only for non-JSON renders.
                parsed, _ = json.JSONDecoder().raw_decode((r.stdout or "").lstrip())
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("loggedIn"), bool):
                cli = "logged_in" if parsed["loggedIn"] else "logged_out"
            elif _CLI_LOGGED_OUT.search(status_raw):
                cli = "logged_out"             # non-JSON render: the stable auth-death phrase
        except Exception:
            cli = "unknown"                    # hang / spawn failure -> unknown, never invalid

    keychain_present, keychain_mtime = _credential_keychain_state(
        credential_service(config_dir), timeout=timeout)

    if cli == "logged_out" or keychain_present is False:
        valid = False                          # definitive: block the spend, alert the owner
    elif cli == "logged_in":
        valid = True
    else:
        valid = None                           # unknown -> fail open (never block on a dark probe)

    return {"cli": cli, "keychain_present": keychain_present,
            "keychain_mtime": keychain_mtime, "valid": valid,
            "status_raw": status_raw[:1000],
            # Which account this reading is ABOUT, on the record: the snapshot is written to
            # `auth_probe.json` and appended to the ~30-min flight recorder, and a recorder that
            # did not say which credential namespace it measured would be unreadable on a machine
            # that has two.
            # Bounded like `status_raw` two lines up, and for the same reason: this whole dict is
            # appended verbatim to the auth flight recorder, whose growth discipline bounds the
            # number of LINES and not the length of one. The note interpolates operator-supplied
            # values (a config dir, an SL_CLAUDE pin), so its length is not ours to assume.
            "config_dir": config_dir, "note": note[:1000]}


def fetch_claude_usage(env=None) -> dict:
    """Fetch Claude Code/Max subscription usage (5-hour and weekly windows).

    Returns a dict with:
      five_hour_pct, seven_day_pct  -> utilization percentages (0-100)
      five_hour_resets, seven_day_resets -> ISO reset timestamps
      five_hour_resets_epoch, seven_day_resets_epoch -> int epoch seconds (R4)
      auth_status -> ok | no_keychain | no_token | rate_limited | auth_expired | api_error
                     | namespace_unknown
      config_dir  -> the config dir whose pool these numbers describe (None = the machine's default)
      note        -> why there was no pool to meter, on `namespace_unknown` only (else "")
    All numeric values are None on failure. macOS only (reads the Keychain).

    ISSUE #350: the pool metered is the one this machine's WORKERS spend, not the owner's default
    login. The scheduler paces lanes on this read and the two Max accounts are separate rate-limit
    pools, so a number from the wrong account is worse than no number — the scheduler's fail-closed
    rule plus decide's bounded grace already handle "dark" safely (#46/#76), and there is no rule
    anywhere that handles "confidently wrong".
    """
    result = {
        "five_hour_pct": None, "seven_day_pct": None,
        "five_hour_resets": None, "seven_day_resets": None,
        "five_hour_resets_epoch": None, "seven_day_resets_epoch": None,
        "auth_status": "unknown", "config_dir": None, "note": "",
    }
    # The same derivation `probe_auth` and the launch seam use. A configured-but-unusable value is
    # NOT "no assignment": there is no namespace to meter, and reading the unsuffixed item would
    # pace the fleet's lanes against the owner's pool. `namespace_unknown` is simply not `ok`, so
    # every consumer (scheduler._usage_ok, watchdog.usage_reads_exhausted, the runner's last-good
    # keeper) already treats it as the dark meter it is.
    config_dir, dir_problem = _worker_namespace(env)
    if dir_problem:
        result["auth_status"] = "namespace_unknown"
        # CARRIED, not dropped. Nothing consumes this — but this is the branch an operator's typo
        # in SL_FLEET_CLAUDE_CONFIG_DIR lands in, and it leaves the meter permanently dark. A
        # status enum with no reason beside it makes that a mystery to whoever reads the state
        # file; the reason costs nothing and names the fix.
        result["note"] = dir_problem[:1000]
        return result
    result["config_dir"] = config_dir
    try:
        # 1. Pull the Claude Code OAuth token from THIS namespace's macOS Keychain item.
        token_raw = subprocess.run(
            ["security", "find-generic-password", "-s", credential_service(config_dir), "-w"],
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
