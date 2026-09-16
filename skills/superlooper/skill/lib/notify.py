"""The notification adapter (plan Task 11, spec §2 "long-running work finishing / stalling /
needing input reaches William"), and the ONE doorway every owner text passes (issue #493).

THE DOORWAY. A text is a pager, never a runbook (owner ruling 2026-09-16): the owner runs more than
one loop on more than one machine, reads it on a lock screen, and cannot reply from the phone. So
no sender composes its own text any more. render() takes a TIER (a closed set — the emoji carries
the priority), a one-clause headline, an optional ask line and an optional URL, and produces:

    <tier emoji> <repo>@<machine> · <what happened, one clause>
    <one line: what is asked of the owner, or nothing>
    <one GitHub URL, only when an issue or PR exists>

`repo` is the name half of the configured owner/name; `machine` is `notify.machine_label`, else the
host's short hostname. The cap (TEXT_MAX_LINES, TEXT_MAX_BYTES) is enforced HERE: an overflowing
input is cut to fit — the ask first, then the headline; the identity and the URL are never cut — and
send()/send_test() journal the overflow as its own `notify_truncated` act, so a verbose caller
surfaces in the morning report's gate health instead of reaching the phone unseen. send() and
send_test() deliver only a Text the renderer produced; a raw string (the old free title) is refused.

DELIVERY, by a fixed precedence (line 1 of the envelope rides as the title, the rest as the body):

    notify.imessage_to  → text via Messages.app (skill/bin/imessage-notify.sh, an osascript
                          one-liner; the first send triggers a one-time macOS permission click —
                          see plugin/skills/superlooper/references/runner-ops.md, and the
                          launchd-started nightly needs it too)
    notify.cmd          → a shell command template with {title}/{body} (an ntfy/Pushover curl, say)
    cmux notify         → the local desktop toast ($SL_CMUX, same binary doctor probes)
    log-only            → nothing configured and no cmux: the action is already journaled by the
                          runner, so the content is never lost, only unsent

Two hard rules, both bought by the autocode postmortems (desktop-only alerts that never reached
the phone, a hung notifier that wedged a tick):
  1. send()/send_test() NEVER raise. Every channel is wrapped; a failure — missing binary, nonzero
     exit, timeout — becomes a returned outcome STRING the runner journals, never an exception into
     the tick. (render() raises only on a tier outside the closed set: a programmer error the tests
     pin, never a runtime condition — and the runner's executor still catches it as a refusal.)
  2. Bounded. Every subprocess carries a hard timeout so a hung Messages/cmux cannot stall the
     loop. Notifications are a convenience layer, never a safety layer (the ALERT file + journal
     are the real signal); so a best-effort send that fails is fine, it is just recorded.

The chosen channel does NOT cascade on failure: if imessage_to is set and the send fails, the
outcome is "imessage send failed …" — we do not silently re-route to cmux (that would hide a
misconfigured primary channel behind a desktop toast William may never see). Precedence selects
the ONE channel to use; log-only is only reached when nothing higher is configured/available.
"""
import os
import socket
import subprocess
import sys
from collections import namedtuple

# The full outcome of one delivery attempt. send() flattens this to a journaled string (its
# unchanged contract); the stack doctor reads it whole (via send_test) to FAIL the notify block
# on a nonzero send and print the actual error — rc + stderr — instead of a bare "configured".
#   channel: "imessage" | "cmd" | "cmux" | "log-only"
#   ok:      the send exited 0 (log-only is ok: nothing to send is not a failure)
#   rc:      the channel command's return code (0 when ok)
#   stderr:  the command's captured stderr (why it failed), "" on success/log-only
SendResult = namedtuple("SendResult", ["channel", "ok", "rc", "stderr"])

_HERE = os.path.dirname(os.path.abspath(__file__))
# imessage-notify.sh lives beside the other entry-point scripts in skill/bin (this module is
# skill/lib). Resolved once, absolutely, so it works whether invoked from a worktree or an install.
_IMESSAGE_SCRIPT = os.path.abspath(os.path.join(_HERE, "..", "bin", "imessage-notify.sh"))

# Same default + override as the doctor check and the ported cmux machinery, so one env var
# (SL_CMUX) points every cmux caller — including this one — at a stub in tests.
_CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"

SEND_TIMEOUT = 15   # generous: Messages can be slow to hand off; still bounds a hung notifier.


# ------------------------------------------------------------------------------------------------
# The doorway (issue #493)
# ------------------------------------------------------------------------------------------------

# The closed tier set. A sender names one of these; it never writes its own title word. The emoji is
# the priority, so a lock screen sorts itself.
DOWN = "down"            # the loop is down with work to do and cannot fix itself — needs the owner
WAITING = "waiting"      # a decision or answer is waiting on the owner; the loop keeps running
RECOVERED = "recovered"  # the loop came back from a DOWN condition
MORNING = "morning"      # the morning report
TEST = "test"            # a test send (doctor)
TIER_EMOJI = {DOWN: "🔴", WAITING: "🟠", RECOVERED: "🟢", MORNING: "☀️", TEST: "🧪"}

# The hard cap. Three lines (identity + what, the ask, the URL) and a byte budget on the order of a
# short SMS: a text that needs more than this is a runbook, and runbooks belong where a human at a
# keyboard reads them (the journal, the morning-report file, the dashboard, the issue). Bytes, not
# characters, because the channel limits are bytes and the emoji and the separator are multi-byte.
TEXT_MAX_LINES = 3
TEXT_MAX_BYTES = 280

_SEP = " · "            # between the identity and the headline
_ELLIPSIS = "…"         # marks a cut line, so a truncated text never reads as whole
_REFUSED = "refused: not a rendered owner text (every text is built by notify.render)"


class Text(namedtuple("Text", ["tier", "lines", "caller", "full_bytes", "dropped_bytes"])):
    """One rendered owner text: what render() produced and the ONLY thing send()/send_test() deliver.
      tier:          one of the closed tier names
      lines:         the envelope, 1..TEXT_MAX_LINES single lines, within TEXT_MAX_BYTES together
      caller:        who asked for it ("decide:park", "cli:nightly", ...) — named in the truncation act
      full_bytes:    the size the envelope WOULD have had uncut
      dropped_bytes: how much the cap removed (0 when the input fit)"""
    __slots__ = ()

    @property
    def text(self):
        return "\n".join(self.lines)

    @property
    def truncated(self):
        return self.dropped_bytes > 0


def _notify_block(config):
    cfg = config if isinstance(config, dict) else {}
    return cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}


def _one_line(v):
    """Any input as ONE line: every run of whitespace — newlines included — becomes a single space.
    Collapsing is not truncation (nothing is lost); it is what keeps a multi-paragraph memo from
    breaking the three-line envelope."""
    return " ".join(str(v).split()) if v is not None else ""


def _nbytes(s):
    return len(s.encode("utf-8"))


def _cut(s, budget):
    """`s` within `budget` UTF-8 bytes, ending in an ellipsis when anything was cut; "" when nothing
    but the ellipsis would fit. Cuts on a character boundary (a partial multi-byte tail is dropped)."""
    if _nbytes(s) <= budget:
        return s
    room = budget - _nbytes(_ELLIPSIS)
    if room <= 0:
        return ""
    kept = s.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
    return kept + _ELLIPSIS if kept else ""


def machine(config):
    """The machine half of the identity: `notify.machine_label` when set, else the short hostname
    (the first label of gethostname — "Williams-Mac-mini.local" reads "Williams-Mac-mini"). Never
    raises; a host that will not say reads "unknown-host"."""
    label = _notify_block(config).get("machine_label")
    label = _one_line(label) if isinstance(label, str) else ""
    if label:
        return label
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    host = _one_line(host).split(".")[0] if isinstance(host, str) else ""
    return host or "unknown-host"


def _repo_name(config):
    """The name half of the configured owner/name — the repo half of the identity."""
    repo = config.get("repo") if isinstance(config, dict) else None
    name = repo.split("/", 1)[-1] if isinstance(repo, str) else ""
    return _one_line(name) or "unknown-repo"


def _fit(prefix, head, ask, url):
    """The envelope's lines within the cap. The order things give way is the design: the ask line is
    cut first (it is the part that grows into a runbook), then the headline; the identity prefix and
    the URL are never cut (a cut URL is useless). Only an identity + URL that alone overflow — an
    absurd label or repo — lose the URL, and past that line 1 is hard-cut, so the cap always holds."""
    def lines(h, a, u):
        return [prefix + h] + ([a] if a else []) + ([u] if u else [])

    def size(h, a, u):
        return _nbytes("\n".join(lines(h, a, u)))

    cap = TEXT_MAX_BYTES
    if size(head, ask, url) <= cap:
        return lines(head, ask, url)
    if ask:
        cut_ask = _cut(ask, cap - size(head, "", url) - 1)       # -1: the newline the ask line needs
        if cut_ask and size(head, cut_ask, url) <= cap:
            return lines(head, cut_ask, url)
    for u in ((url, "") if url else ("",)):
        cut_head = _cut(head, cap - size("", "", u))
        if size(cut_head, "", u) <= cap:
            return lines(cut_head, "", u)
    return [_cut(prefix + head, cap)]


def render(config, tier, headline, ask=None, url=None, caller=None):
    """THE rendering entry point: the one place an owner text is composed. Returns a Text whose lines
    are exactly the envelope (see the module docstring) within the cap. `tier` must be one of the
    closed set (DOWN / WAITING / RECOVERED / MORNING / TEST) — a free title raises ValueError, because
    the tier set being closed is the contract. `caller` names the sender in a truncation record; it
    defaults to the calling module:function. Pure apart from the hostname read."""
    if not isinstance(tier, str) or tier not in TIER_EMOJI:
        raise ValueError("notify.render needs a tier from %s, got %r" % (sorted(TIER_EMOJI), tier))
    if caller is None:
        frame = sys._getframe(1)
        caller = "%s:%s" % (frame.f_globals.get("__name__", "?"), frame.f_code.co_name)
    prefix = "%s %s@%s%s" % (TIER_EMOJI[tier], _repo_name(config), machine(config), _SEP)
    head = _one_line(headline) or "(no headline)"     # line 1 always says SOMETHING after the ·
    ask, url = _one_line(ask), _one_line(url)
    full_bytes = _nbytes("\n".join([prefix + head] + ([ask] if ask else []) + ([url] if url else [])))
    lines = tuple(_fit(prefix, head, ask, url))
    return Text(tier, lines, str(caller), full_bytes, full_bytes - _nbytes("\n".join(lines)))


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _rendered(text):
    """True only for a Text render() could have produced: a closed-set tier whose emoji leads line 1,
    1..TEXT_MAX_LINES single lines within TEXT_MAX_BYTES. A raw string, a tuple, or a Text tampered
    past the cap (namedtuple._replace) is refused exactly like the old free title — the cap is a
    property of what is delivered, not merely of what render() happened to return."""
    if not isinstance(text, Text):
        return False
    if not (isinstance(text.tier, str) and text.tier in TIER_EMOJI):
        return False
    lines = text.lines
    if not (isinstance(lines, tuple) and 1 <= len(lines) <= TEXT_MAX_LINES
            and all(isinstance(ln, str) and "\n" not in ln and "\r" not in ln for ln in lines)):
        return False
    return (lines[0].startswith(TIER_EMOJI[text.tier] + " ")
            and _nbytes("\n".join(lines)) <= TEXT_MAX_BYTES
            and isinstance(text.caller, str) and _int(text.full_bytes)
            and _int(text.dropped_bytes) and text.dropped_bytes >= 0)


def _journal_truncation(config, text, home):
    """Journal a truncated text as its own `notify_truncated` act — the class-killer: a verbose
    caller can never reach the phone unseen, and the morning report's gate health lists it where the
    defect can be fixed. Written whether or not the channel then delivers (the defect is the caller's
    verbosity, not the channel). Never raises: a journal hiccup must not stop the text itself."""
    if not text.truncated:
        return
    try:
        import journal
        if home is None:
            import config as config_lib
            home = config_lib.state_home(config)
        journal.append(home, {"act": "notify_truncated", "caller": text.caller, "tier": text.tier,
                              "headline": text.lines[0], "full_bytes": text.full_bytes,
                              "dropped_bytes": text.dropped_bytes, "cap_bytes": TEXT_MAX_BYTES,
                              "outcome": "ok"})
    except Exception:
        pass


# ------------------------------------------------------------------------------------------------
# Delivery
# ------------------------------------------------------------------------------------------------

def _str_or_none(v):
    """A configured channel value is a non-empty string; anything else (None, wrong-typed) reads
    as 'not configured' — the same fail-closed coercion every view in this codebase uses."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def _run(args, timeout=SEND_TIMEOUT, env=None):
    """Run a channel's command. Returns (return_code, stderr); a missing binary / OSError / timeout
    is a nonzero rc with an explanatory stderr, never a raise (mirrors gh._run and the runner's
    _run_script discipline). The stderr rides back so the stack doctor can print WHY a send failed,
    not just that it did."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **env} if env else None)
        return r.returncode, (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except (OSError, ValueError) as e:
        # OSError: missing binary. ValueError: a pathological arg (an embedded null byte, or
        # non-UTF-8 output — UnicodeDecodeError subclasses ValueError). Both become a nonzero
        # outcome, never a raise: the module contract is "never an exception into the tick", and
        # the read-only stack doctor now calls this path too.
        return 127, str(e)


def _cmux_binary():
    return os.environ.get("SL_CMUX", _CMUX_DEFAULT)


# One outcome-string pair per channel: (delivered, failed-template). send() renders these so its
# journaled contract stays byte-for-byte what the runner and its tests expect after the refactor.
_OUTCOME = {
    "imessage": ("sent via imessage", "imessage send failed (rc={rc})"),
    "cmd": ("sent via cmd", "cmd notify failed (rc={rc})"),
    "cmux": ("sent via cmux", "cmux notify failed (rc={rc})"),
}


def _deliver(config, text):
    """Select the ONE channel by precedence, run it, and return the full SendResult. Never raises.
    This is the single home of precedence + the never-raise guarantee: both send() (which flattens
    it to a journaled string) and send_test() (which the doctor reads whole) call it, so the two
    can never drift apart. `text` is an already-validated rendered Text: its first line is the
    title every channel shows, the remaining (at most two) lines the body."""
    title = text.lines[0]
    body = "\n".join(text.lines[1:])
    n = _notify_block(config)
    imessage_to = _str_or_none(n.get("imessage_to"))
    cmd = _str_or_none(n.get("cmd"))

    if imessage_to is not None:
        rc, err = _run([_IMESSAGE_SCRIPT, imessage_to, title, body])
        return SendResult("imessage", rc == 0, rc, err)

    if cmd is not None:
        # The untrusted VALUES never enter the shell string. {title}/{body} are replaced with
        # VARIABLE REFERENCES ("$SL_TITLE"/"$SL_BODY"), and the values ride in the environment.
        # A bash variable's value is not re-parsed for command substitution, so a memo containing
        # `$(...)`/backticks/quotes is delivered verbatim NO MATTER how the config author quotes
        # the placeholder — shlex.quote alone only protected a BARE token and re-opened injection
        # inside `"{body}"` (Codex R2 C1). $SL_TITLE/$SL_BODY are equivalently available for
        # authors who prefer to reference them directly. Put {title}/{body} as BARE tokens for
        # verbatim delivery — the adapter supplies the quoting; a placeholder the author ALSO
        # wraps in quotes stays SAFE (never executes) but its value may word-split.
        rendered = cmd.replace("{title}", '"$SL_TITLE"').replace("{body}", '"$SL_BODY"')
        rc, err = _run(["bash", "-lc", rendered], env={"SL_TITLE": title, "SL_BODY": body})
        return SendResult("cmd", rc == 0, rc, err)

    cmux = _cmux_binary()
    if os.path.exists(cmux):
        # `--title` is the form that actually sets visible text (autocode CMUX-NOTES spike); a bare
        # positional is accepted but ignored. --body carries the detail line.
        rc, err = _run([cmux, "notify", "--title", title, "--body", body])
        return SendResult("cmux", rc == 0, rc, err)

    return SendResult("log-only", True, 0, "")


def send(config, text, home=None):
    """Deliver one rendered owner text by the configured precedence; return a short outcome string
    the caller journals. Never raises. `text` must come from render() — anything else (a raw title
    string, a tampered copy past the cap) is REFUSED, never delivered. A truncated text journals its
    `notify_truncated` act into `home` (default: the configured state home) before it goes out."""
    if not _rendered(text):
        return _REFUSED
    _journal_truncation(config, text, home)
    r = _deliver(config, text)
    if r.channel == "log-only":
        return "log-only"
    ok_msg, fail_msg = _OUTCOME[r.channel]
    return ok_msg if r.ok else fail_msg.format(rc=r.rc)


def send_test(config, text, home=None):
    """Deliver ONE rendered text through the configured precedence and return the full SendResult
    (channel, ok, rc, stderr) — the stack doctor's hook for PROVING the channel works, and the
    morning report's canary. Same precedence, same refusal, same truncation journal, same never-raise
    guarantee as send(); the only difference is the caller gets rc + stderr instead of a flattened
    string, so a failed send can be reported with its actual reason. A real message really goes out:
    callers announce the side effect first."""
    if not _rendered(text):
        return SendResult("refused", False, 2, _REFUSED)
    _journal_truncation(config, text, home)
    return _deliver(config, text)
