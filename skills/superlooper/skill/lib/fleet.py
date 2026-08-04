"""The fleet machine's build-up — what it is, how it is made, and how it is judged (issue #309).

The 2026-08-03 machine ruling (docs/HERDR-ADOPTION-PLAN.md §6, §8.1) stands the session-host fleet
up FRESH on the always-on Mac mini while the old production loop keeps running untouched on the
work laptop. This module is that build-up expressed as code rather than as a morning of careful
typing, because the DoD is a list of properties **that are all silently wrong in a way that still
looks alive**:

* an **unfenced** control socket does not look broken from the runner's seat — it answers;
* a host **one release off the pin** starts, runs, and lacks the two upstream fixes the whole
  adoption rests on (herdrdev/herdr#2064 clientless restore, #1878 prompt submission);
* a **fleet config dir spelled with a trailing slash** is a different credential namespace, so a
  perfectly provisioned login reports logged-out and the worker presents as auth-death (#300);
* an inherited, EMPTY credential-redirect variable silently bills the **owner's** subscription;
* a job loaded in the wrong launchd domain has **no login keychain**, so `gh` dies intermittently;
* and the host's own screen classifier answers **idle** for any screen it does not recognise
  (`default_known_agent_idle_fallback`, read out of the pinned release's `src/detect/manifest.rs`)
  — the exact reading the owner personally watched it give a pane that was blocked on a dialog.

So there are two halves here and they are deliberately separate. ``render_*``/``*_path`` are the
build-up: pure functions producing the artefacts an install writes. ``check_*`` is the judge: one
named block per DoD property, in the same shape as ``doctor --stack``, so "the mini is built" is an
exit code rather than an opinion.

**What this module is not.** It does not talk to the host. Every path, name and environment
variable belonging to the host comes from ``session_host`` — the five-verb doorway — and is spelled
nowhere here, so swapping the host stays the bounded rewrite ``tests/test_one_session_host_door.py``
protects. It also performs no cutover: standing the fleet server up on its own named session leaves
the machine's existing sessions exactly where they were, which is what "fresh build-up beside
untouched production" means mechanically.
"""
import json
import os
import re
import time
import tomllib
import plistlib
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import herdr_hook
import runner_home
import session_host
import stack_doctor
from stack_doctor import CheckResult

# The pin has ONE machine-readable home and this is not it — a second copy is how a bump leaves two
# disagreeing versions on disk (the failure being a build one release behind the checksum it is
# verified against, which looks perfectly healthy).
PINNED_VERSION = herdr_hook.PINNED_VERSION

# The dedicated named session (plan §2). It is what lets the owner's own window and the fleet's
# server share one machine without sharing one server — and, unlike an XDG override, it costs a
# worker pane nothing (an inherited config home was measured de-authenticating `gh` while it kept
# answering, docs/SPIKES-2026-07-29-results.md).
SESSION_NAME = "fleet"

# macOS `sockaddr_un.sun_path` — 104 bytes INCLUDING the terminating NUL, so 103 usable characters.
# Named here because the plan names it: a long username plus a long session name is enough to make
# a server that starts and then cannot bind, and the error it prints is about a socket, not a path.
SUN_PATH_MAX = 104

# The build-up's own prefix under the state base. NOT per-repo: the host is one server for the whole
# machine, and a per-repo prefix would give two repos two hosts, two fences and two tokens.
PREFIX_DIR = "fleet"

# The launchd job. Machine-wide, so unlike the runner/watchdog/nightly labels it carries no repo
# slug — there is exactly one of these per machine and a second would fight the first for the
# session name.
SERVER_LABEL = "com.superlooper.session-host"

# The agent whose screen manifest the build-up overrides. The loop launches Claude Code; a machine
# that later flies another agent overrides that one too, which is why this is a parameter
# everywhere below and a default only here.
DEFAULT_AGENT = "claude"

# The carried fence patch's own refusal message (vendor/herdr/, `UNAUTHORIZED_MESSAGE`), used as the
# patch's signature AT REST. A stock build and a patched one report the same version and answer the
# same verbs, so the version alone can never tell them apart — and the difference is the whole
# security posture. Measured to discriminate: present once in the built binary, absent from the
# stock release. It is a string, not a checksum, on purpose: the binary's hash changes with every
# toolchain and every build, and a check nobody can keep green is a check that gets deleted.
FENCE_SIGNATURE = "unauthorized: this socket requires a token"

# The strings that identify a file as one THIS build-up wrote. `--install` rewrites a file carrying
# its marker (that is what makes it idempotent) and REFUSES anything else — the host's config
# directory is shared with whatever sessions the machine already runs, and our own launchd label is
# not proof that the plist under it is ours.
CONFIG_MARKER = "superlooper fleet"
PLIST_MARKER = "superlooper FLEET SESSION HOST"
VIEWER_MARKER = "superlooper fleet viewer"

# Our one appended rule. The id is namespaced on purpose: the file is a copy of the vendor's
# manifest, and a reader has to be able to tell at a glance which rule is not theirs.
CATCHALL_RULE_ID = "superlooper_unknown_screen_is_unknown"
# The marker that records WHICH vendor manifest version was snapshotted. A local override SHADOWS
# the auto-updating remote manifest, so a snapshot silently ages into a stale detection ruleset;
# `check_screen_fallback` compares this against what the host currently has and says so.
_SOURCE_MARKER = "# superlooper: snapshot of the vendor manifest version "
_SOURCE_RE = re.compile(r"^%s(\S+)" % re.escape(_SOURCE_MARKER), re.M)

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "launchd.session-host.plist"
_template_cache = None


def _template():
    global _template_cache
    if _template_cache is None:
        try:
            _template_cache = _TEMPLATE_PATH.read_text()
        except OSError as e:
            raise FileNotFoundError(
                "the session-host LaunchAgent template is missing or unreadable at %s (%s) — "
                "republish the engine through bin/install.sh, which ships templates/ with the "
                "payload" % (_TEMPLATE_PATH, e))
    return _template_cache


def _xml(value):
    return _xml_escape(str(value))


# --------------------------------------------------------------------------------- the layout

def prefix(state_base):
    """The fleet's own directory under the state base — binary, token, logs, build tree."""
    return os.path.join(state_base, PREFIX_DIR)


def host_binary(fleet_prefix):
    return os.path.join(fleet_prefix, "bin", session_host.BINARY_NAME)


def token_file(fleet_prefix):
    """Where the server reads its fence token from.

    **Why a file and not a plist value**, measured on the mini while building this: `launchctl
    print gui/$UID/<label>` echoes a job's `EnvironmentVariables` verbatim, so a token spelled in
    the plist is one command away from any same-uid process — which is precisely the worker the
    fence exists to keep out. The file is `0600`; the vendor README is right that this does not
    exclude the same uid either, and that residual exposure is stated in docs/FLEET.md rather than
    papered over. What the file DOES buy is that reading it requires knowing the path, while
    `launchctl print` requires knowing nothing.
    """
    return os.path.join(fleet_prefix, "token")


def log_dir(fleet_prefix):
    return os.path.join(fleet_prefix, "logs")


def viewer_command(fleet_prefix):
    """The double-clickable attach script the owner registers as a Login Item.

    A viewer is a TUI: it needs a terminal, so it cannot be a LaunchAgent the way the server is.
    It is also no longer load-bearing — #302 measured clientless restore working on the pinned
    build, which retired plan §5.4's "keep a viewer attached" workaround — so this ships as an
    artefact the owner may register, never as something an install silently adds to their login.
    """
    return os.path.join(fleet_prefix, "viewer.command")


def server_plist_path(home):
    return os.path.join(home, "Library", "LaunchAgents", SERVER_LABEL + ".plist")


# --------------------------------------------------------------------------------- the socket

def socket_path(host_config_dir, session=SESSION_NAME):
    return session_host.session_socket_path(host_config_dir, session)


def socket_path_problem(path):
    """None, or the sentence explaining why this path cannot be bound.

    Measured in BYTES, not characters: the limit is on `sun_path`, and a non-ASCII home directory
    spends more bytes than it looks like it does.
    """
    used = len(os.fsencode(path)) + 1          # +1 for the NUL the kernel stores
    if used > SUN_PATH_MAX:
        return ("the control socket path is %d bytes and the kernel's limit is %d (sockaddr_un."
                "sun_path, NUL included) — the server starts and then cannot bind, and the error "
                "it prints names a socket rather than a path: %s" % (used, SUN_PATH_MAX, path))
    return None


# --------------------------------------------------------------------------------- host config

def host_config_path(host_config_dir):
    return os.path.join(host_config_dir, "config.toml")


def host_config_settings():
    """Just the two settings, with no ownership marker — what a REFUSAL tells an operator to merge.

    The full rendered file carries `CONFIG_MARKER`, which is how `--install` recognises a file as
    its own. Printing that in a refusal would invite the operator to paste it into a file they
    maintain by hand, and the NEXT install would then read the marker as ownership and replace the
    whole thing (fresh-agent review round 2). So the merge instructions carry settings only.
    """
    return "version_check = false\n\n[session]\nresume_agents_on_restore = true\n"


def render_host_config():
    """The host's `config.toml` as the fleet needs it (plan §2).

    Two settings, and the second is not in the plan because it was not known when the plan was
    written: a background version check on an unattended server is a standing invitation to leave
    the pinned, PATCHED build — and the patch is the fence. Both keys are written explicitly rather
    than left to a default, because "it defaults to what we want" is a fact about one release.
    """
    return (
        "# superlooper fleet — the session host's own configuration (issue #309).\n"
        "# Written by `superlooper fleet --install`; re-running it rewrites this file.\n"
        "\n"
        "# The pin is a DELIBERATE event that re-runs acceptance and re-applies the fence patch\n"
        "# (vendor/herdr/README.md). A background check that nags an unattended server toward a\n"
        "# newer build is how a fleet quietly stops being the build you fenced.\n"
        "version_check = false\n"
        "\n"
        "[session]\n"
        "# Bring hosted agents back after a restore rather than leaving bare shells. Already the\n"
        "# default at the pinned release; spelled out because a default is a fact about one\n"
        "# version, and this one is load-bearing for every overnight restart.\n"
        "resume_agents_on_restore = true\n"
    )


def host_config_problem(text):
    """None, or what is missing from — or wrong with — an installed host config.

    PARSED, not pattern-matched (fresh-agent review round 2). Two settings in two different tables
    is exactly the shape text-matching gets wrong in both directions: a key in a later table looks
    like it is in `[session]`, a duplicate table reads as fine while the host refuses the file
    outright. A parser answers the question the host will actually ask, and a file it cannot parse
    is reported as unreadable rather than judged on its spelling.
    """
    if text is None:
        return "no config file"
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return ("it is not valid TOML (%s) — the host refuses to start on a config it cannot "
                "parse, so this is not a state to report as merely incomplete" % e)
    except (TypeError, ValueError) as e:
        return "it could not be parsed (%s)" % e
    session = data.get("session")
    if not isinstance(session, dict):
        return "no [session] table"
    if session.get("resume_agents_on_restore") is not True:
        return ("session.resume_agents_on_restore is not true — a restored server brings its panes "
                "back as bare shells")
    if data.get("version_check") is not False:
        return ("a top-level version_check = false is missing — an unattended server nagged toward "
                "a newer build is a fleet that can quietly stop being the build you fenced (nested "
                "under a table it would parse and be ignored)")
    return None


# --------------------------------------------------------------- the screen-state override

def override_path(host_config_dir, agent=DEFAULT_AGENT):
    """Where the host reads a LOCAL manifest override, from its own `src/detect/manifest.rs`."""
    return os.path.join(host_config_dir, "agent-detection", "%s.toml" % agent)


def render_manifest_override(source_toml, source_version=None):
    """The vendor's manifest, carried verbatim, plus one lowest-priority catch-all rule.

    **Why a whole copy.** A local override REPLACES the manifest it shadows — the host does not
    merge them — so dropping the vendor's rules would trade a wrong `idle` for no detection at all.

    **Why the rule.** With no rule matching, the pinned release answers `Idle` for a KNOWN agent
    (`fallback_reason = "default_known_agent_idle_fallback"`). Plan §2 wants the opposite, and c2 is
    the general form: absence of signal is UNKNOWN, never a normal state. A priority-1 rule that
    matches any screen fires only where nothing else did — which is exactly the fallback case.

    Re-rendering an already-rendered file is a no-op on the rule, so `fleet --install` is idempotent
    and a snapshot can be refreshed without stacking copies of ours.
    """
    body = _strip_catchall(source_toml)
    if source_version is None:
        source_version = manifest_version(body) or "unknown"
    return (body.rstrip("\n") + "\n\n"
            + _SOURCE_MARKER + str(source_version) + "\n"
            + "# Appended by superlooper (issue #309). Everything above is the vendor's own\n"
            + "# manifest, carried verbatim. This rule is ours: with nothing else matching, the\n"
            + "# host answers `idle` for a known agent, and an unrecognised screen reported as\n"
            + "# ready-for-work is the one wrong answer an unattended loop cannot survive.\n"
            + "[[rules]]\n"
            + 'id = "%s"\n' % CATCHALL_RULE_ID
            + 'state = "unknown"\n'
            + "priority = 1\n"
            + 'region = "whole_recent"\n'
            + "regex = ['(?s).*']\n")


def _strip_catchall(text):
    """`text` with a previously appended catch-all block (and its marker) removed."""
    out, dropping = [], False
    for line in (text or "").splitlines():
        if line.startswith(_SOURCE_MARKER):
            dropping = True
            continue
        if dropping:
            continue
        out.append(line)
    body = "\n".join(out)
    # A file rendered by an older version of this function, or hand-edited: cut from the rule's own
    # [[rules]] header rather than leaving a half-block behind.
    if CATCHALL_RULE_ID in body:
        marker = body.index(CATCHALL_RULE_ID)
        head = body.rfind("[[rules]]", 0, marker)
        if head != -1:
            body = body[:head]
    return body.rstrip("\n") + "\n"


def manifest_version(text):
    """The `version = "..."` a screen-detection manifest declares, or None."""
    found = re.search(r'^\s*version\s*=\s*"([^"]+)"', text or "", re.M)
    return found.group(1) if found else None


def has_catchall_rule(text):
    """Is our catch-all actually an ACTIVE rule in this file — not a comment, not a leftover id?

    Substring presence is not the question (fresh-agent review). A file whose only mention of the
    id is inside a comment, or whose rule body has been commented out, would satisfy a naive `in`
    while the host still answers `idle` for every screen it does not recognise — the exact state
    this block exists to detect, sailing past the block that exists to detect it.
    """
    if not text:
        return False
    body = re.sub(r"^\s*#.*$", "", text, flags=re.M)
    found = re.search(r"""^\s*id\s*=\s*['"]%s['"]\s*$""" % re.escape(CATCHALL_RULE_ID),
                      body, re.M)
    if not found:
        return False
    head = body.rfind("[[rules]]", 0, found.start())
    if head == -1:
        return False
    nxt = body.find("[[rules]]", found.end())
    block = body[head:nxt if nxt != -1 else len(body)]
    return bool(re.search(r"""^\s*state\s*=\s*['"]unknown['"]\s*$""", block, re.M))


def override_source_version(text):
    """Which vendor manifest version this override was snapshotted from, or None."""
    found = _SOURCE_RE.search(text or "")
    return found.group(1) if found else None


# --------------------------------------------------------------------------------- the login item

def render_server_plist(*, binary, session, token_file, log_dir, path):
    """The server's gui/$UID LaunchAgent, as text.

    Absolute paths are REQUIRED for the same reason the runner's are: launchd runs a job from `/`,
    so a relative program resolves to nothing and the failure surfaces as a job that starts and
    dies instantly into a log nobody is watching.

    The token does NOT appear here — only the PATH of the file holding it. See `token_file`.
    """
    for name, value in (("binary", binary), ("token_file", token_file), ("log_dir", log_dir)):
        if not str(value).startswith("/"):
            raise ValueError("%s must be absolute — launchd runs the job from '/', so %r would "
                             "resolve to nothing" % (name, value))
    if not session_host.NAME_RE.match(str(session or "")):
        raise ValueError("the session name %r is not addressable by the host" % (session,))
    return (_template()
            .replace("{label}", _xml(SERVER_LABEL))
            .replace("{binary}", _xml(binary))
            .replace("{session}", _xml(session))
            .replace("{token_file_var}", _xml(session_host.API_TOKEN_FILE_ENV_VAR))
            .replace("{token_file}", _xml(token_file))
            .replace("{log_dir}", _xml(log_dir))
            .replace("{path}", _xml(path)))


def render_viewer_command(*, binary, session):
    """The owner's attach script — a `.command` file, double-clickable and Login-Item-registerable."""
    return ("#!/bin/sh\n"
            "# superlooper fleet viewer (issue #309). Attaches to the fleet's session in a real\n"
            "# terminal. Nothing load-bearing depends on this being open: #302 measured the pinned\n"
            "# build reviving hosted agents with no client ever attached, which retired plan\n"
            "# §5.4's workaround. Register it as a Login Item only if you want the window.\n"
            "exec %s --session %s\n" % (_sh_quote(binary), _sh_quote(session)))


def _sh_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------------- identity (c25)

# #300's landmine 2. Present-but-EMPTY collapses the credential namespace back to the unsuffixed
# default — the owner's — and nothing anywhere errors. Named as a string because it is Claude
# Code's variable, not the host's.
_REDIRECT_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"


def config_dir_problem(value):
    """None, or why this spelling of the fleet config dir cannot be trusted.

    #300's landmine 1: the credential namespace is `sha256` of the string **as written**, with no
    canonicalisation of any kind. Five spellings of one directory produced five namespaces, and the
    wrong one presents as auth-death rather than as an error — the refused-read-as-empty family,
    arriving down a path nobody is watching.
    """
    if not isinstance(value, str) or not value:
        return "no fleet config dir is configured"
    if not value.startswith("/"):
        return ("%r is not an absolute path — the credential namespace is a hash of this string as "
                "written, so a relative or ~-spelled dir is a DIFFERENT identity that reports "
                "logged-out" % value)
    if value != os.path.normpath(value):
        return ("%r is not the canonical spelling of %r — a trailing slash, a `//` or a `/./` each "
                "hash to their own credential namespace, and a worker launched with the wrong one "
                "presents as auth-death" % (value, os.path.normpath(value)))
    return None


def identity_problem(fleet_status, owner_status):
    """None, or why the fleet is not riding its own subscription.

    `owner_status` may be None — the owner's default config dir being unreadable is not the fleet's
    fault and must not be reported as the fleet's failure. Everything else fails closed.
    """
    if not isinstance(fleet_status, dict):
        return "the fleet config dir's login could not be read at all"
    if fleet_status.get("loggedIn") is not True:
        return ("the fleet config dir is not logged in — a worker launched here lands on a login "
                "screen, and the identity plan's OAuth is an owner action (#313)")
    if not fleet_status.get("orgId"):
        return "the fleet login reports no orgId, so its billing entity cannot be established"
    if not isinstance(owner_status, dict) or not owner_status.get("orgId"):
        return None
    if fleet_status.get("orgId") == owner_status.get("orgId"):
        return ("the fleet and the owner report the SAME orgId (%s) — orgId is the billing entity, "
                "so the fleet is spending the owner's subscription and nothing anywhere errors"
                % fleet_status.get("orgId"))
    if fleet_status.get("email") and fleet_status.get("email") == owner_status.get("email"):
        return ("the fleet and the owner report the same account (%s) — different orgIds with one "
                "account is not the separation the ruling asked for"
                % fleet_status.get("email"))
    return None


# --------------------------------------------------------------------------------- the judge

def _auth_status(probe, claude, config_dir):
    """`claude auth status` under ONE config dir, as a dict — or None if it could not be read.

    The measurement instrument #300 identified: it prints `email` / `orgId` / `subscriptionType`
    as JSON, per config dir, non-interactively and without spending a token.
    """
    if not claude:
        return None
    proc = probe.run([claude, "auth", "status"], timeout=20,
                     env={"CLAUDE_CONFIG_DIR": config_dir})
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(proc, "stdout", "") or "")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_host_binary(probe, fleet_prefix):
    """`host binary` — the pinned build is installed in the fleet's own prefix."""
    name = "host binary"
    path = host_binary(fleet_prefix)
    fix = ("build it from the source checkout with `vendor/herdr/build.sh` — it clones the pinned "
           "tag, applies the fence patch beside it, and installs the result here")
    if not probe.exists(path):
        return CheckResult(name, False, "nothing is installed at %s" % path, fix)
    if not probe.executable(path):
        return CheckResult(name, False, "%s is there but not executable" % path, fix)
    proc = probe.run([path, "--version"], timeout=10)
    out = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).strip()
    got = out.split()[-1] if out else ""
    if getattr(proc, "returncode", 1) != 0 or not got:
        return CheckResult(name, False,
                           "%s would not report a version (%s)" % (path, out or "no output"), fix)
    if got != PINNED_VERSION:
        return CheckResult(
            name, False,
            "%s reports %s and the pin is %s — the pinned release is the one carrying the "
            "clientless-restore and prompt-submission fixes the adoption rests on"
            % (path, got, PINNED_VERSION), fix)
    # The version says nothing about the patch: a stock build and a fenced one report the same
    # string and answer the same verbs. This is the fence AT REST — the carried patch's own refusal
    # message, compiled into the binary. Verified to discriminate: 1 occurrence in the built
    # binary, 0 in the stock release. `host fence` proves the running server; this proves the file,
    # which is what a `--force`-less rebuild or a hand-copied binary would otherwise slip past.
    fenced = probe.contains(path, FENCE_SIGNATURE)
    if fenced is None:
        return CheckResult(name, False,
                           "%s reports the pinned %s but could not be read to check whether the "
                           "fence patch is in it — not being able to look is not proof"
                           % (path, PINNED_VERSION), fix)
    if not fenced:
        return CheckResult(
            name, False,
            "%s reports the pinned %s but carries NO fence patch — it is a stock build, and a "
            "stock build's control socket serves every worker on this machine"
            % (path, PINNED_VERSION),
            "rebuild it: `vendor/herdr/build.sh --force`")
    return CheckResult(name, True,
                       "%s reports the pinned %s and carries the fence patch" % (path,
                                                                                PINNED_VERSION))


def _plist_program(text):
    """The job's ProgramArguments, or None. Parsed, because this is what it will really run."""
    try:
        job = plistlib.loads((text or "").encode())
    except Exception:                                  # plistlib raises a family, not one class
        return None
    args = job.get("ProgramArguments") if isinstance(job, dict) else None
    return [str(a) for a in args] if isinstance(args, list) and args else None


def plist_runs_problem(text, *, binary, session, token_file_path):
    """None, or why the INSTALLED job would not run the fleet this build-up configured.

    Every other block reads the artefacts on disk: `host binary` inspects `<prefix>/bin/`,
    `fleet isolation` stats `<prefix>/token`. If the job runs something else, all of them are
    describing files nothing uses — the whole report goes green about a fleet that is not the one
    running (fresh-agent review round 2). So the job's own argv and environment are the join.
    """
    argv = _plist_program(text)
    if not argv:
        return "its ProgramArguments could not be read"
    if argv[0] != binary:
        return ("it runs %s, not the fleet's own %s — every other block here inspects the latter, "
                "so they would all be describing a binary nothing runs" % (argv[0], binary))
    if session not in argv:
        return "it does not name the %r session, so it would bind a different socket" % session
    try:
        job = plistlib.loads(text.encode())
        env = job.get("EnvironmentVariables") or {}
    except Exception:
        return "its EnvironmentVariables could not be read"
    named = env.get(session_host.API_TOKEN_FILE_ENV_VAR)
    if named != token_file_path:
        return ("it reads its fence token from %r, not %s — the token this build-up minted and "
                "checks the permissions of is not the one the server uses"
                % (named, token_file_path))
    return None


def check_login_item(probe, uid, home, fleet_prefix=None):
    """`host login item` — the server is a loaded gui/$UID LaunchAgent with an explicit PATH.

    The domain is the keychain rule and is never a parameter (see `runner_home.domain`). The PATH
    is read off the plist on disk rather than off the running job, because the plist is what the
    NEXT login will use — a job hand-started with a good environment proves nothing about tomorrow.
    """
    name = "host login item"
    plist = server_plist_path(home)
    target = runner_home.service_target(uid, SERVER_LABEL)
    fix = "run `superlooper fleet --install` to write and load it"
    text = probe.read_text(plist)
    if text is None:
        return CheckResult(name, False, "no LaunchAgent at %s" % plist, fix)
    job_path = _plist_path_value(text)
    if job_path is None or job_path == runner_home.LAUNCHD_PATH:
        # No PATH at all is the WORSE case, not the neutral one — launchd hands the job its own
        # four directories either way, so both spellings produce a server that finds no Homebrew
        # and no ~/.local/bin and passes that on to every pane it spawns. Reading a missing key as
        # "fine" would be a check that passes hardest on the plist nobody wrote properly.
        return CheckResult(
            name, False,
            "%s %s — launchd hands a job only %s, so the server finds no Homebrew and no "
            "~/.local/bin, and every pane it spawns inherits that"
            % (plist, "sets no PATH at all" if job_path is None else "carries only launchd's own "
               "PATH", runner_home.LAUNCHD_PATH), fix)
    if re.search(r"<key>\s*%s\s*</key>" % re.escape(session_host.API_TOKEN_ENV_VAR), text):
        # The RENDERER never writes this, but the judge reads what is INSTALLED, not what we would
        # have written — an older or hand-edited plist is exactly the case a judge exists for.
        # `launchctl print` echoes EnvironmentVariables verbatim, so this is the fence token
        # published to every same-uid process, i.e. to the worker the fence exists to keep out.
        return CheckResult(
            name, False,
            "%s sets %s directly — `launchctl print %s` then prints the fence token to any "
            "same-uid process. The job must name the 0600 FILE (%s) instead"
            % (plist, session_host.API_TOKEN_ENV_VAR, target, session_host.API_TOKEN_FILE_ENV_VAR),
            "re-run `superlooper fleet --install`, then rotate the token — it has been published")
    if fleet_prefix:
        problem = plist_runs_problem(text, binary=host_binary(fleet_prefix),
                                     session=SESSION_NAME,
                                     token_file_path=token_file(fleet_prefix))
        if problem:
            return CheckResult(name, False, "%s is installed but %s" % (plist, problem), fix)
    launchctl = probe.command("launchctl", envvar="SL_LAUNCHCTL", default="/bin/launchctl")
    if not launchctl:
        # NOT a warn. A plist on disk starts nothing, and "I could not ask whether the job is
        # loaded" is absence of signal — which this stack never reads as health (c2). A WARN here
        # would let the whole build-up exit ready on a LaunchAgent nobody proved was running.
        return CheckResult(name, False,
                           "%s is installed, but launchctl is not resolvable from here, so whether "
                           "the job is LOADED could not be read at all" % plist,
                           "put launchctl on PATH (or set SL_LAUNCHCTL) and re-run")
    proc = probe.run(runner_home.print_argv(launchctl, uid, SERVER_LABEL), timeout=10)
    if getattr(proc, "returncode", 1) != 0:
        return CheckResult(
            name, False,
            "%s exists but no service %s is loaded in %s — the plist on disk starts nothing until "
            "it is bootstrapped into the gui domain (never system/: only the gui domain reads the "
            "login keychain)" % (plist, SERVER_LABEL, runner_home.domain(uid)), fix)
    out = getattr(proc, "stdout", "") or ""
    pid = runner_home.service_pid(out)
    if pid:
        return CheckResult(name, True, "%s loaded in %s, pid %d" % (SERVER_LABEL, target, pid))
    if runner_home.service_is_idle(out) is True:
        where = (" read %s" % os.path.join(log_dir(fleet_prefix), "server.log")
                 if fleet_prefix else "")
        return CheckResult(name, False,
                           "%s is loaded in %s but NOT running — a KeepAlive job that is idle has "
                           "exited and been throttled;%s" % (SERVER_LABEL, target, where), fix)
    return CheckResult(name, False,
                       "%s is loaded in %s but its state could not be read — absence of signal is "
                       "not health" % (SERVER_LABEL, target), fix)


def _plist_path_value(text):
    found = re.search(r"<key>PATH</key>\s*<string>([^<]*)</string>", text or "")
    return found.group(1) if found else None


def check_fence(socket, fence=None):
    """`host fence` — a TOKENLESS caller is refused (issue #305's whole point).

    OPEN is the dangerous answer and is worded as such: an unfenced socket does not look broken
    from the runner's seat, and every worker pane already carries the path to it.
    """
    name = "host fence"
    verdict = (fence or session_host.fence_probe)(socket)
    if verdict == session_host.FENCED:
        return CheckResult(name, True,
                           "a tokenless connection to %s was refused — the fence is up" % socket)
    if verdict == session_host.OPEN:
        return CheckResult(
            name, False,
            "a tokenless connection to %s was SERVED — there is no fence, so any worker pane can "
            "drive the whole fleet with ten lines of python. REQUIRED before unattended fleet use "
            "(plan §3.1)" % socket,
            "the running server is not the patched build, or it has no token configured: rebuild "
            "with vendor/herdr/build.sh and re-run `superlooper fleet --install`")
    return CheckResult(
        name, False,
        "%s did not answer — silence is UNREACHABLE, never proof of a fence (c2)" % socket,
        "start the server: `superlooper fleet --install`, then check the log under the fleet prefix")


def wait_for_fence(socket, fence=None, sleep=None, attempts=15, pause=1.0):
    """Poll the control socket until it answers, and return the last verdict.

    `--load` bootstraps the job and then judges it, and a server that has not finished binding yet
    is UNREACHABLE — which the fence block correctly refuses to read as anything good. Without this
    wait, the install prints a red line it created itself, one second before the machine is fine.
    Found by driving the real install.

    Bounded and honest: it stops at the FIRST answer of any kind (a fence that is genuinely down
    answers immediately and must be reported immediately, not waited out), and a socket that never
    answers still returns UNREACHABLE rather than a guess.
    """
    probe_fn = fence or session_host.fence_probe
    sleep = sleep or time.sleep
    verdict = session_host.UNREACHABLE
    for attempt in range(max(1, attempts)):
        verdict = probe_fn(socket)
        if verdict != session_host.UNREACHABLE:
            return verdict
        if attempt + 1 < attempts:
            sleep(pause)
    return verdict


def check_host_config(probe, host_config_dir):
    """`host config` — the settings plan §2 names, plus the socket the session will actually bind."""
    name = "host config"
    path = host_config_path(host_config_dir)
    fix = "run `superlooper fleet --install` to write it"
    problem = host_config_problem(probe.read_text(path))
    if problem:
        return CheckResult(name, False, "%s: %s" % (path, problem), fix)
    sock = socket_path(host_config_dir)
    sock_problem = socket_path_problem(sock)
    if sock_problem:
        return CheckResult(name, False, sock_problem,
                           "move the state base somewhere shorter, or shorten the session name")
    return CheckResult(
        name, True,
        "%s sets resume_agents_on_restore and pins the version check; session %r binds %s (%d of "
        "%d bytes)" % (path, SESSION_NAME, sock, len(os.fsencode(sock)) + 1, SUN_PATH_MAX))


def check_screen_fallback(probe, host_config_dir, agent=DEFAULT_AGENT, live_version=None):
    """`screen fallback` — an unrecognised screen reports UNKNOWN rather than idle.

    A WARN, never a FAIL, when the override is merely STALE: the host's word is advisory by our own
    doctrine (plan §5.2) and gates nothing, so a snapshot a few manifest versions behind costs
    detection quality and not correctness. Its ABSENCE is a FAIL, because the default answer is the
    one wrong answer an unattended loop cannot survive.
    """
    name = "screen fallback"
    path = override_path(host_config_dir, agent)
    text = probe.read_text(path)
    if not has_catchall_rule(text):
        return CheckResult(
            name, False,
            "no local manifest override at %s — with no rule matching, the host answers `idle` for "
            "a known agent, so a screen it does not recognise (a trust dialog, a login prompt) "
            "reads as ready for work" % path,
            "run `superlooper fleet --install`, which snapshots the host's own manifest and appends "
            "the catch-all")
    snapshot = override_source_version(text)
    if live_version and snapshot and live_version != snapshot:
        return CheckResult(
            name, True,
            "%s is installed but snapshotted from manifest %s while the host now has %s — a local "
            "override SHADOWS the auto-updating one, so its detection rules are frozen at the "
            "snapshot. Re-run `superlooper fleet --install` to refresh it (advisory only: the "
            "host's state gates nothing here)" % (path, snapshot, live_version),
            warn=True)
    return CheckResult(name, True,
                       "%s makes an unrecognised screen UNKNOWN (snapshot %s)"
                       % (path, snapshot or "unrecorded"))


def check_identity(probe, fleet_config_dir, claude=None):
    """`fleet identity` — the fleet rides its own subscription, and the owner's is untouched."""
    name = "fleet identity"
    problem = config_dir_problem(fleet_config_dir)
    if problem:
        return CheckResult(name, False, problem,
                           "set the canonical absolute spelling once and use it everywhere; #300 "
                           "measured five spellings of one directory producing five identities")
    if _REDIRECT_VAR in (getattr(probe, "env", {}) or {}):
        return CheckResult(
            name, False,
            "%s is set in this environment — present-but-empty collapses the credential namespace "
            "back to the owner's unsuffixed one, so the fleet silently bills the owner's "
            "subscription (#300 landmine 2)" % _REDIRECT_VAR,
            "unset %s; never let a launch inherit it" % _REDIRECT_VAR)
    if not claude:
        return CheckResult(name, True,
                           "no `claude` to ask, so the fleet login could not be read — the `claude "
                           "binary` block names that problem", warn=True)
    fleet_status = _auth_status(probe, claude, fleet_config_dir)
    owner_status = _auth_status(probe, claude, None)
    problem = identity_problem(fleet_status, owner_status)
    if problem:
        return CheckResult(name, False, problem,
                           "log the fleet dir into its own account in a supervised window (#313); "
                           "a session cannot drive a browser sign-in")
    if _onboarded(probe.read_text(os.path.join(fleet_config_dir, ".claude.json"))) is not True:
        # A FAIL, not a warn (fresh-agent review). This block is a BUILD-UP gate, and an
        # unprovisioned config dir parks the first worker at the theme picker — a screen no auth
        # manifest covers and the host classifies `idle`. #313's own DoD lists provisioning past
        # onboarding for exactly this reason, so "logged in, provisioning unconfirmed" is not a
        # state the build-up may be declared complete in.
        return CheckResult(
            name, False,
            "%s is logged in as %s (org %s) but its onboarding flag is not a confirmed true — an "
            "unprovisioned config dir parks a worker at the theme picker, a screen no auth "
            "manifest covers and the host reports as idle"
            % (fleet_config_dir, fleet_status.get("email"), fleet_status.get("orgId")),
            "open one interactive `claude` under that config dir and finish first-run (#313)")
    other = (owner_status or {}).get("orgId")
    return CheckResult(
        name, True,
        "%s rides its own subscription (%s, org %s%s)"
        % (fleet_config_dir, fleet_status.get("subscriptionType") or "?",
           fleet_status.get("orgId"), "; the owner's default dir is org %s" % other if other else ""))


def _onboarded(text):
    """True / False / None(unreadable) for a config dir's first-run state.

    PARSED, never substring-matched: `"hasCompletedOnboarding":true` and the spaced spelling are
    the same fact, and a check that recognised only one would warn about a perfectly provisioned
    dir — the kind of false red that teaches an operator to skim the block.
    """
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "hasCompletedOnboarding" not in data:
        return None
    return data.get("hasCompletedOnboarding") is True


def check_isolation(probe, fleet_prefix, host_config_dir):
    """`fleet isolation` — nothing the fleet owns is shared with what it must not disturb.

    Honest about its reach: production on the OTHER machine cannot be observed from here, and a
    block that implied it could would be exactly the confident liar this stack refuses to be. What
    it does assert is the local half — the fleet's session, socket and prefix are its own.
    """
    name = "fleet isolation"
    fleet_socket = socket_path(host_config_dir)
    default_socket = session_host.session_socket_path(host_config_dir, None)
    token = token_file(fleet_prefix)
    mode = probe.mode(token)
    if mode is None:
        return CheckResult(name, False, "the fence token at %s could not be stat-ed, so whether it "
                                        "is readable by anything on this machine is unknown" % token,
                           "run `superlooper fleet --install` to mint it")
    if mode & 0o077:
        return CheckResult(
            name, False,
            "the fence token at %s is mode %o — group- or world-readable. It is the whole secret "
            "the control socket's auth rests on" % (token, mode),
            "chmod 600 %s and restart the server (the token is read once at start)" % token)
    if fleet_socket == default_socket:
        return CheckResult(
            name, False,
            "the fleet would bind the host's DEFAULT socket (%s) — the owner's own window and the "
            "fleet would be one server, which is the shared fate the fresh build-up exists to "
            "avoid" % default_socket,
            "give the fleet its own named session")
    return CheckResult(
        name, True,
        "on this machine the fleet is its own: session %r on %s, prefix %s, separate from the "
        "host's default session at %s. Production on the other machine is not observable from "
        "here — this block asserts the local half only"
        % (SESSION_NAME, fleet_socket, fleet_prefix, default_socket))


def check_fleet(probe, state_base, host_config_dir=None, fleet_config_dir=None, uid=None,
                home=None, fence=None, live_manifest_version=None, agent=DEFAULT_AGENT):
    """Every block, in the order a build-up goes wrong.

    Binary before login item before fence before config: each one is upstream of the next, and a
    reader who fixes the first red line usually clears several.
    """
    home = home or getattr(probe, "home", None) or os.path.expanduser("~")
    host_config_dir = host_config_dir or session_host.config_dir(getattr(probe, "env", None))
    uid = os.getuid() if uid is None else uid
    fleet_prefix = prefix(state_base)
    claude = stack_doctor.resolve_claude(probe)
    return [
        check_host_binary(probe, fleet_prefix),
        check_login_item(probe, uid, home, fleet_prefix=fleet_prefix),
        check_fence(socket_path(host_config_dir), fence=fence),
        check_host_config(probe, host_config_dir),
        check_screen_fallback(probe, host_config_dir, agent=agent,
                              live_version=live_manifest_version),
        # The launch stack's own binary pin (#303): a fleet machine where the first `claude` on the
        # ladder is cmux's wrapper is one uninstall away from having no launcher at all, and this
        # issue's amendment makes a green line here the cmux-independence assertion.
        stack_doctor.check_claude_binary(probe),
        check_identity(probe, fleet_config_dir, claude=claude["path"] if claude["ok"] else None),
        check_isolation(probe, fleet_prefix, host_config_dir),
    ]
