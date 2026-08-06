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
import hashlib
import os
import re
import time
try:                                    # 3.11+; the fleet machine has it, the CI floor does not
    import tomllib
except ImportError:                     # pragma: no cover - exercised by the CI interpreter
    tomllib = None
import plistlib
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import herdr_hook
import identity
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


def token_provenance_file(fleet_prefix):
    """Sidecar recording the digest of the token this build-up minted beside it.

    The token file has no room for a marker — its whole content is the secret, and the server reads
    it verbatim — so the record lives next to it. Without it, `--install` adopted any regular file
    it found at the token path, which meant a file left there by accident, by an aborted install, or
    by an older prefix became the fence's secret without anyone deciding it should.

    **What it is NOT, stated because the honest limit is easy to overclaim** (fresh-agent review,
    final pass): this is an INTEGRITY record, not authentication. A same-uid process that can write
    `<prefix>/token` can equally write this file with the matching digest, so it does not defend
    against one. Nothing file-based can, on a machine where the fleet and its workers share a UNIX
    account — which is the same reason the token file itself is readable, and it is exactly the
    decision filed as **#342**. What this buys is that an UNVOUCHED or CHANGED token is refused
    rather than silently adopted, and that the refusal is loud enough to be investigated.
    """
    return os.path.join(fleet_prefix, "token.provenance")


def token_provenance(token_text):
    """What the sidecar holds for a given token: a digest, never the secret itself."""
    return hashlib.sha256((token_text or "").encode("utf-8", "surrogateescape")).hexdigest()


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
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return ("it is not valid TOML (%s) — the host refuses to start on a config it cannot "
                    "parse, so this is not a state to report as merely incomplete" % e)
        except (TypeError, ValueError) as e:
            return "it could not be parsed (%s)" % e
    else:
        data, problem = _read_toml_ish(text)
        if problem:
            return problem
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


# The two literals this check ever needs to recognise. Deliberately not a value parser: the
# questions are "is it exactly true" and "is it exactly false", and anything else — an int, a
# string, a nested array — is neither, which is the answer the caller wants anyway.
_BOOLS = {"true": True, "false": False}


def _read_toml_ish(text):
    """`(mapping, problem)` for the two settings this check asks about, without a TOML parser.

    Only reached on an interpreter older than 3.11 (`tomllib` is the reader everywhere else, and it
    is the one the fleet machine uses). It is a LINE WALKER, not a parser, and it is written to be
    honest about that: it tracks which table each key belongs to — the thing a flat regex sweep gets
    wrong in both directions — and refuses a duplicate table header, which real TOML rejects and a
    naive reader would call healthy. Anything it cannot classify is ignored rather than guessed at,
    so its answer is only ever "these two keys are set like this", which is all the caller uses.
    """
    data, table, seen = {}, None, set()
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).strip()
        line = "" if line.startswith("#") else line
        if not line:
            continue
        header = re.match(r"^\[([^\[\]]+)\]$", line)
        if header:
            table = header.group(1).strip()
            if table in seen:
                # Worded to MATCH tomllib's branch above, not merely to be correct. Both readers
                # answer the same operator, and a message that changed with the interpreter would
                # make one machine's report unrecognisable beside another's — the same reason the
                # dual-reader test asserts the shared phrase and not each reader's own.
                return None, ("it is not valid TOML: the table [%s] is declared twice — the host "
                              "refuses to start on a config it cannot parse, so this is not a "
                              "state to report as merely incomplete" % table)
            seen.add(table)
            data.setdefault(table, {})
            continue
        if line.startswith("[["):
            # An array of tables — not a table this reads, but its SHAPE still has to be valid, or
            # `[[broken` sails past as "some array header" in a file that already carried both
            # settings (fresh-agent review round 4).
            if not re.match(r"^\[\[[^\[\]]+\]\]$", line):
                return None, ("it is not valid TOML: this interpreter has no TOML parser, and the "
                              "fallback reader cannot classify %r — upgrade to Python 3.11+ for an "
                              "exact answer, or simplify the file" % line[:60])
            table = None
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(\S+)\s*$", line)
        if not pair:
            # FAIL CLOSED on anything this walker cannot classify (fresh-agent review round 4).
            # Skipping it would let `[[broken` sit in a file that already carried both settings and
            # report healthy — while the host refuses to start on it. This reader is narrow and
            # says so; a line it cannot read is a config it cannot vouch for.
            return None, ("it is not valid TOML: this interpreter has no TOML parser, and the "
                          "fallback reader cannot classify %r — upgrade to Python 3.11+ for an "
                          "exact answer, or simplify the file" % line[:60])
        value = _BOOLS.get(pair.group(2).lower(), pair.group(2))
        if table is None:
            data[pair.group(1)] = value
        elif isinstance(data.get(table), dict):
            data[table][pair.group(1)] = value
    return data, None


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
    if tomllib is not None:
        # PARSED where we can (fresh-agent review): the host reads this file with a real parser and
        # REFUSES it if it does not parse, so a malformed override is not an installed one. A rule
        # also has to be able to FIRE — an id and a state with no region or match expression is a
        # rule the host will never apply, which is the same wrong `idle` with a green line over it.
        try:
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, TypeError, ValueError):
            return False
        rules = data.get("rules")
        if not isinstance(rules, list):
            return False
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("id") != CATCHALL_RULE_ID:
                continue
            if rule.get("state") != "unknown" or not rule.get("region"):
                return False
            return bool(rule.get("regex") or rule.get("line_regex") or rule.get("contains"))
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
    return bool(re.search(r"""^\s*state\s*=\s*['"]unknown['"]\s*$""", block, re.M)
                and re.search(r"^\s*region\s*=", block, re.M)
                and re.search(r"^\s*(regex|line_regex|contains)\s*=", block, re.M))


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


# --------------------------------------------------- the machine's own runner environment (#355)
# #326 shipped the launch pre-flight: before an `i<N>` worker spawns, the launcher probes the
# control socket and refuses (exit 9) when a tokenless caller is SERVED or the socket cannot be
# reached. It is armed by `SL_FLEET_FENCE=required`, read from the RUNNER's own process
# environment — an environment variable rather than a config key because `.superlooper/config.json`
# travels with the repo through git and the fleet machine and a dev workstation must answer
# differently (`session_host.fence_required` owns that argument).
#
# Nothing SET it. The build-up installed a patched host and its judge measured the socket, but the
# machine it built did not tell its own runner to enforce that at launch time — so the gate shipped
# correct and INERT on the one machine it was built for, and arming it was a step someone had to
# remember. Which is the same shape as the hole #326 closed one layer up: the fence existed, and
# nothing called it.
#
# **Why a file the runner reads, rather than a value baked into the runner's LaunchAgent.** There
# are two runner homes (`runner_home`: a visible pane today, a login item optionally), the choice
# is per-repo, and it is explicitly not this issue's to make. A plist would arm exactly one of
# them — leaving today's default silently unarmed, which is this issue's own failure mode wearing
# a different hat. The fence is a property of the MACHINE's host server, so the machine states it
# once, in one place, and any runner that boots here picks it up whichever home it has.
#
# **It is READ, never sourced**, and only the variables named below are ever applied. A
# machine-level file that could set anything would put the launcher's whole contract on disk —
# `SL_ATTENDED`, `SL_RESUME_SESSION_ID`, `SL_EXPECT_GH_LOGIN` — every one of which the runner pins
# EMPTY precisely because an ambient value must never ride into a worker session.

ENV_FILE = "environment"

# The marker that makes the file `--install`'s own to rewrite, exactly like CONFIG_MARKER. Unlike
# the host's config this file lives in the fleet's own prefix, so a collision is unlikely — but the
# install has ONE door for "is this mine?" and a second rule would be a second answer.
ENV_MARKER = "superlooper fleet environment"

# The allow-list. One variable today, and adding a second is a decision rather than an edit: what
# this file can set is what a machine can silently change about every launch made on it.
ENV_VARS = (session_host.FENCE_REQUIRED_VAR,)


def env_file(fleet_prefix):
    """Where the machine states what its runners enforce — beside the token, never in a repo."""
    return os.path.join(fleet_prefix, ENV_FILE)


def render_env_file(fence=session_host.FENCE_REQUIRED):
    """The file `--install` writes. Its whole job is to be READABLE after the fact.

    The DoD's second bullet: arming must not be silent in the other direction either. A machine
    whose gate is armed says so in a file an operator can `cat`, and a machine deliberately taken
    out of the fenced set says THAT in the same file — which is why `off` is spelled out here as
    the supported edit rather than left to "delete the file". Deleting it disarms too, and leaves
    nothing on disk that says so, which is indistinguishable from never having built (the same
    argument `session_host.fence_required` makes for the value existing at all).
    """
    return (
        "# %s — read by `superlooper run` at boot (issue #355).\n"
        "#\n"
        "# This file is what ARMS the launch pre-flight on this machine: with it, every `i<N>`\n"
        "# worker launch probes the session host's control socket first and REFUSES (exit 9) when\n"
        "# a tokenless caller is served or the socket cannot be reached. Without it the launcher\n"
        "# checks nothing, which is the correct behaviour on a dev workstation running a stock\n"
        "# host — an unfenced socket is that machine's normal state.\n"
        "#\n"
        "# It is READ, never sourced, and only %s is applied. Written by\n"
        "# `superlooper fleet --install`; re-running it rewrites this file.\n"
        "#\n"
        "# To take this machine OUT of the fenced set, change the value below to `off` rather than\n"
        "# deleting the file: a deleted file disarms too, and leaves nothing that says so.\n"
        "%s=%s\n" % (ENV_MARKER, ", ".join(ENV_VARS), session_host.FENCE_REQUIRED_VAR, fence))


def machine_env(text, present=None):
    """What a runner booting on this machine adds to its own environment: ``(assignments,
    problem, ignored)``.

    ``present`` says whether the FILE is there, which ``text`` alone cannot express — a reader
    answers None both for "no such file" and for "could not be read", and those are opposite
    facts here. It defaults to ``text is not None``.

    Three rules, and the middle one is the whole security posture:

    * **absent — nothing.** A dev workstation that never ran the build-up is untouched: no switch,
      no gate, no new refusal. This is why the arming is a file the build-up writes and not a
      default in the code.
    * **present but not readable as an arming — ``required``, with the problem named.** The file
      exists only because the build-up ran HERE, so its presence is the machine's declaration. A
      hand-edit that lost the line, a value that is empty, or a file that cannot be read must not
      read as "this fleet is unfenced" — that is the silently-disarmed-fence failure this issue
      exists to end, and the same fail-closed direction `fence_required` takes on a typo.
    * **present and readable — carried VERBATIM.** Including a misspelling: the switch's parsing
      belongs to `session_host.fence_required` (#326 ruled it, and this issue may not change it),
      and a value repaired here would never reach the refusal that exists to name it.

    ``ignored`` names variables the file sets that this build-up does not — never applied, always
    reported, because a file that silently dropped half its content would be worse than one that
    refused it.
    """
    present = (text is not None) if present is None else present
    if not present:
        return {}, None, []
    fallback = {session_host.FENCE_REQUIRED_VAR: session_host.FENCE_REQUIRED}
    if text is None:
        return dict(fallback), (
            "it exists but could not be read, so what this machine declares is unknown — a runner "
            "booting here fails closed and arms the gate anyway (%s=%s)"
            % (session_host.FENCE_REQUIRED_VAR, session_host.FENCE_REQUIRED)), []
    assignments, ignored = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # `export K=V` and quoted values are read even though nothing ever sources this file: an
        # operator editing it will reach for shell habits, and a line that LOOKS like it sets the
        # switch while being silently ignored is the failure mode this whole issue is about.
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in ENV_VARS:
            ignored.append(key)
            continue
        assignments[key] = value
    problem = None
    if not (assignments.get(session_host.FENCE_REQUIRED_VAR) or "").strip():
        # Present-but-empty is the #300 landmine shape: it reads to a human as "set" and to a
        # parser as nothing at all. `off` is the word that disarms; an empty value is a broken file.
        assignments.update(fallback)
        problem = ("it names no %s, so what this machine declares is unknown — a runner booting "
                   "here fails closed and arms the gate anyway (%s=%s)"
                   % (session_host.FENCE_REQUIRED_VAR, session_host.FENCE_REQUIRED_VAR,
                      session_host.FENCE_REQUIRED))
    return assignments, problem, ignored


def load_machine_env(probe, fleet_prefix):
    """`machine_env` for the file on disk — the ONE reader the runner's boot and the judge share.

    Two callers that read one file with two readers is two answers, and the pair that matters here
    is "the report says armed" / "the runner is not". `exists` before `read_text` for the reason
    `machine_env` takes ``present``: the reader cannot tell absence from an unreadable file, and
    those are the opposite verdicts.
    """
    path = env_file(fleet_prefix)
    return machine_env(probe.read_text(path), present=probe.exists(path))


def apply_machine_env(env, assignments):
    """Put the machine's declaration into a process environment: ``(applied, replaced)``.

    **The machine's own file WINS over whatever the process already carried**, and that direction
    is deliberate. An `export SL_FLEET_FENCE=off` left in a shell rc file, a wrapper or a
    LaunchAgent would otherwise silently disarm the fleet machine — the exact inheritance hazard
    the runner pins `SL_ATTENDED` empty for, and the one `_fence_preflight` refuses to honour an
    attended bypass over. It would also make the file useless as evidence: an operator could read
    an armed machine's `environment` and still be wrong about the machine.

    ``replaced`` reports the value that lost, so a boot line can say the machine disagreed with its
    operator rather than quietly winning.
    """
    applied, replaced = {}, {}
    for key, value in assignments.items():
        held = env.get(key)
        if isinstance(held, str) and held.strip() and held != value:
            replaced[key] = held
        env[key] = value
        applied[key] = value
    return applied, replaced


# --------------------------------------------------------------------------------- identity (c25)

# ONE definition of the contract, and it is not here (#314). `lib/identity.py` owns the canonical
# spelling, the credential-redirect rule and the account verdict, because the LAUNCH SEAM enforces
# exactly those three and a build-up report that judged them by its own second copy would tell an
# operator the machine is ready for launches its launcher will refuse.
_REDIRECT_VAR = identity.REDIRECT_VAR
config_dir_problem = identity.config_dir_problem


def identity_problem(fleet_status, owner_status):
    """None, or why the fleet is not riding its own subscription.

    `owner_status` may be None — the owner's default config dir being unreadable is not the fleet's
    fault and must not be reported as the fleet's failure. Everything else fails closed.

    The single-account half is `identity.account_problem`, which is the verdict the LAUNCH SEAM
    applies (#314). Sharing it is not tidiness: this block used to accept any `loggedIn: true`,
    and the real binary answers exactly that for a session running on an API KEY — so the build-up
    could report a healthy fleet identity for a dir that bills per token. What remains here is the
    part only a build-up can ask: whether the fleet's account is a DIFFERENT one from the owner's.
    """
    problem = identity.account_problem(fleet_status)
    if problem:
        return ("the fleet config dir's own login is not one a worker may fly on: %s (the identity "
                "plan's OAuth is an owner action — #313)" % problem)
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

    The OVERLAY (rather than `identity.probe_env`'s whole-environment build) is what this reader
    needs and the launch seam's does not: `config_dir=None` must REMOVE the variable so the same
    command can be asked about the owner's default dir, which is the second half of the
    two-different-orgIds measurement. What it does share is `identity.status_from` — the body,
    never the exit code, because `claude auth status` EXITS 1 when logged out while printing a
    perfectly readable answer, and this block used to report that as unreadable (found by driving
    the real CLI against a real unprovisioned dir).
    """
    if not claude:
        return None
    return identity.status_from(probe.run([claude, "auth", "status"], timeout=20,
                                          env={identity.CONFIG_DIR_VAR: config_dir}))


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
    try:
        named = argv[argv.index("--session") + 1]
    except (ValueError, IndexError):
        named = None
    if named != session:
        # The VALUE of the flag, not merely the word appearing somewhere in argv (fresh-agent
        # review round 4): `--session default server fleet` contains "fleet" and binds the default
        # session, which is the owner's own — the one thing this build-up exists not to share.
        return ("its --session is %r, not %r, so it would bind a different socket"
                % (named, session))
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


def check_launch_gate(probe, fleet_prefix, env=None):
    """`launch gate` — a runner booting on this machine ARMS the fence pre-flight (issue #355).

    A DIFFERENT fact from `host fence`, and kept a separate block because a machine can have either
    without the other. `host fence` asks the socket whether a tokenless caller is refused;
    this asks whether the LAUNCHER on this machine will check that before it flies a worker. #326
    shipped exactly the state where the first was green and the second was not, and one merged line
    would have reported that machine as ready.

    What it judges is the FILE, not this command's environment, because the file is what a runner
    reads — and a doctor that answered out of the shell it happened to be typed in would report the
    posture of a terminal rather than of a machine.
    """
    name = "launch gate"
    path = env_file(fleet_prefix)
    fix = "run `superlooper fleet --install`, which writes it"
    if env is None:
        env = getattr(probe, "env", None) or {}
    ambient = (env.get(session_host.FENCE_REQUIRED_VAR) or "").strip()
    assignments, problem, ignored = load_machine_env(probe, fleet_prefix)
    if not assignments:
        return CheckResult(
            name, False,
            "the launch gate is NOT armed on this machine: there is no %s, so a runner booting here "
            "carries no %s and every `i<N>` launch flies without probing the control socket first "
            "— which is the state a fenced-looking machine is silently in%s"
            % (path, session_host.FENCE_REQUIRED_VAR,
               (". (This command's own environment sets %s=%r, so a runner started from THIS shell "
                "would be armed — but a login item, or any other shell, would not.)"
                % (session_host.FENCE_REQUIRED_VAR, ambient)) if ambient else ""),
            fix)
    declared = assignments[session_host.FENCE_REQUIRED_VAR]
    required, unrecognised = session_host.fence_required(assignments)
    if problem:
        return CheckResult(
            name, False, "%s: %s" % (path, problem),
            "re-run `superlooper fleet --install` to rewrite it (or repair the line by hand)")
    if not required:
        return CheckResult(
            name, False,
            "%s sets %s=%r — this machine is DELIBERATELY out of the fenced set, so its runners "
            "launch workers without probing the control socket. That is a legitimate state for a "
            "dev workstation and not one a fleet build-up is complete in (plan §3.1)"
            % (path, session_host.FENCE_REQUIRED_VAR, declared),
            "set it back to %r, or accept that this machine is not a fleet machine"
            % session_host.FENCE_REQUIRED)
    notes = []
    if unrecognised:
        notes.append("the value %r is not one this engine recognises, so the pre-flight reads it as "
                     "'required' and names it in the refusal" % unrecognised)
    if ambient and ambient != declared:
        notes.append("this command's own environment sets %s=%r; the machine's file wins at boot, "
                     "so a runner is armed anyway — but that variable is doing nothing"
                     % (session_host.FENCE_REQUIRED_VAR, ambient))
    if ignored:
        notes.append("it also names %s, which a runner never applies: this file may set only %s"
                     % (", ".join(sorted(set(ignored))), ", ".join(ENV_VARS)))
    return CheckResult(
        name, True,
        "%s sets %s=%s, so a runner booting here refuses an `i<N>` launch onto a control socket "
        "that is not fenced (exit 9)%s"
        % (path, session_host.FENCE_REQUIRED_VAR, declared,
           " — " + "; ".join(notes) if notes else ""),
        fix if notes else "", warn=bool(notes))


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
        return CheckResult(
            name, False, problem,
            "set %s to one canonical absolute path (suggested: %s) — the launch path reads that "
            "variable and assigns the SAME string to every session, and #300 measured five "
            "spellings of one directory producing five identities"
            % (identity.FLEET_DIR_VAR, identity.SUGGESTED_FLEET_DIR))
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
    if _onboarded(probe.read_text(identity.config_file(fleet_config_dir))) is not True:
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
    # What this block proves, stated exactly. The gap it used to name — "this proves the config
    # DIR's identity, not that a launch uses it" — is closed by #314: the launch path derives this
    # same string once and every spawn is handed it byte for byte, and the session refuses itself
    # if the account under it is not the expected one. The remaining honest limit is narrower and
    # is stated instead: a launch only assigns this dir when the MACHINE says so, so a green line
    # here plus an unset SL_FLEET_CLAUDE_CONFIG_DIR still means workers ride the default login.
    # That case never reaches this line — `config_dir_problem(None)` fails the block above.
    return CheckResult(
        name, True,
        "%s rides its own subscription (%s, org %s%s), and %s assigns it to every launch — the "
        "spawn seam derives this one canonical string and each session refuses itself unless "
        "`claude auth status` reports that account (#314)"
        % (fleet_config_dir, fleet_status.get("subscriptionType") or "?",
           fleet_status.get("orgId"),
           "; the owner's default dir is org %s" % other if other else "",
           identity.FLEET_DIR_VAR))


def _onboarded(text):
    """True / False / None(unreadable) for a config dir's first-run state.

    Delegated to `identity.onboarded` (issue #345), which is now also what `doctor --stack` reads:
    the build-up judge and the machine doctor must not be able to disagree about whether a config
    dir has finished first-run, and two copies of one parse is exactly how they would.
    """
    return identity.onboarded(text)


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
        return CheckResult(
            name, False,
            "the fence token at %s is missing, unreadable, or not a regular file (a symlink there "
            "would have this block report the permissions of a file the fleet never minted) — so "
            "whether the secret is readable on this machine is unknown" % token,
            "run `superlooper fleet --install` to mint it; move any non-regular object aside first")
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
        # Downstream of the socket it guards (issue #355), and never merged into it: `host fence`
        # is a fact about the server, this is a fact about every launch made on this machine, and
        # #326 shipped exactly the machine where the first was green and the second was inert.
        check_launch_gate(probe, fleet_prefix),
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
