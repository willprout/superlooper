"""What a DELIVERED prompt looks like — the engine's delivery oracle (issue #334).

`lib/session_host.SessionHost.send` will not send without one. That is not caution, it is the
measured finding: 6/6 prompts typed their text into the composer, dropped the submission and
returned `agent_prompted` rc=0 in under a second (docs/SPIKES-2026-07-30-supervised.md §8), and
even a settled `--wait` rc=0 means "queued". So the wrapper takes an oracle from its caller and
raises `DeliveryUnproven` unless that oracle positively says the prompt landed. Until this module
existed, the engine had no caller that could supply one — the wrapper's `send` had zero production
call sites, which is why #308's move left the nudge path stranded on cmux instead of migrating it.

**Why this lives outside the wrapper.** "What a delivered prompt looks like" is knowledge about the
AGENT, not about the host — a tmux-hosted Claude proves delivery exactly the way a herdr-hosted one
does, and a Codex under either proves it differently. The wrapper's own docstring makes that split
explicit ("the wrapper never reads a transcript itself — it takes a delivery ORACLE"), and the
project's agent-boundary rule (CLAUDE.md) requires the agent-specific half to live in a named file.
This is that file.

**Why the TRANSCRIPT and not the screen.** The adoption plan prescribes transcript-side
verification (§4) and forbids screen reads as an evidence path (§7.3): rows that scroll off the
agent's alternate screen never enter the host's scrollback, so no line count recovers them. A
transcript is a FILE the agent writes — the plan's own blessed fallback shape ("agent writes a
file, supervisor reads the file") — and it is what every spike used as its ground truth.

**What counts as delivery, exactly.** A NEW `user` entry, appearing after the mark, carrying the
text we sent. Three deliberate exclusions:

* NOT an assistant reply. A busy session queues the prompt and answers minutes later, and `send`
  must not block for a whole turn; requiring a reply would make every nudge into a working lane
  read as a non-delivery. (The spikes' criterion WAS reply-exists, because a lab probe asks a
  trivial question and waits for it. A doorbell is not that.)
* NEVER the content of any reply. The spikes' methodological finding: two trivial-arithmetic
  answers were wrong that night, so an oracle that graded the answer would have reported a delivery
  failure for a prompt that was delivered perfectly.
* NOT a `type: user` entry that is a tool RESULT, a SIDECHAIN, a META entry or a COMPACT SUMMARY.
  All four are recorded under the same role and none of them is the pane accepting a prompt: a
  worker whose session `cat`s the runner log, a subagent handed the same text, or a compaction
  summarising an earlier turn back into the file would each otherwise manufacture proof that a
  prompt nobody submitted had arrived.

**What it must never do is guess.** Three answers, and the third is load-bearing: True (proven),
False (we could read the transcript throughout and our prompt is not in it — the composer-drop
failure, positively), None (we could not read one at all: no session id, no config dir, transcript
saving off). The wrapper treats False and None identically — both raise — but the caller's memo
should not claim a non-delivery it did not observe.
"""
import json
import os
import re
import time

import identity

# How long `landed` will wait for a transcript line that is still being flushed. `--wait` returning
# "settled" is the host observing a lifecycle change, not the agent's JSONL hitting disk, and the
# two are not the same moment. Cheap insurance: on the success path this costs one stat, and on the
# failure path a nudge was going to be refused anyway.
PATIENCE_SECONDS = 6.0
POLL_SECONDS = 0.2

# How much of a transcript `records` will read for the send-safety sense. A worker's transcript
# runs to megabytes; every verdict `pane_state.classify_transcript` reaches is "the most recent
# entry decides", so a bounded tail loses nothing and keeps a per-nudge read cheap.
TAIL_BYTES = 256 * 1024

# The ceiling for the widened re-read below. A single transcript record can be larger than the
# ordinary window — a 300KB tool result is unremarkable for these agents — and a window that lands
# entirely inside one record yields NO complete line, which reads as "this session has written
# nothing" and now REFUSES the nudge. So an empty bounded read widens once rather than answering a
# question it did not actually look at.
MAX_TAIL_BYTES = 8 * 1024 * 1024

# Where Claude keeps its per-project transcripts under whichever config dir is in force.
PROJECTS_DIRNAME = "projects"
CONFIG_DIR_DEFAULT = ".claude"

# The agents this engine can prove a delivery for. Claude's proof is its transcript; Codex has no
# equivalent this module can read, and its proven reply channel (the nonce-fenced ack file) is
# ASYNCHRONOUS — it lands minutes later, not at the moment `send` returns — so it cannot answer the
# question the wrapper asks. `for_lane` returns None there rather than a permissive stand-in: a
# stand-in would be the rc=0-is-delivery lie again, wearing an oracle's costume.
#
# The consequence is real and is FILED, not buried here: on the session host a Codex lane can be
# launched and then never rung — no gate handback, no progress probe, no exit interview. Issue #352
# carries that decision. Nothing in this file may quietly close the gap by widening this tuple; an
# agent belongs here when something in this module can positively prove its prompts land.
AGENTS = ("claude",)

_WS = re.compile(r"\s+")


def transcript_root(env=None):
    """Where THIS machine's worker transcripts live, or None when nothing resolves.

    Resolved through ``identity.worker_config_dir`` — the SAME derivation every spawn path uses,
    not a second one. That is #314's own rule ("one canonical config-dir string per worker") and it
    is load-bearing twice over:

    * It CANONICALISES. The engine's own suggested fleet value is ``~/.claude-fleet``; a resolver
      that joined the raw string would hand ``os.listdir`` a literal ``~`` and find nothing, while
      every worker ran happily under the expanded path. The oracle would then answer "cannot tell"
      about a directory it never looked at — and "cannot tell" is a refusal, so the whole machine's
      nudges would defer forever after really delivering their prompts. Silent, and it reads as a
      healthy-session defer.
    * It reads the MACHINE's assignment (``SL_FLEET_CLAUDE_CONFIG_DIR``) and nothing else. An
      inherited ``CLAUDE_CONFIG_DIR`` / ``SL_CLAUDE_CONFIG_DIR` is exactly what the rest of the
      engine refuses to trust in the runner's environment — the launch floor will not forward one,
      `identity_probe_env` scrubs it, and `_script_env` pins its sibling empty — because a runner
      started from inside a worker or debugger pane carries THAT session's namespace. Ranking them
      first here would point the oracle at one credential namespace while every worker ran in
      another.

    Falls back to ``$HOME/.claude``, which is the namespace of every machine that is not the fleet.
    None (rather than a guess) when there is no HOME and no assignment, for the same reason the
    canonicalisation matters: a guessed root costs real nudges.
    """
    env = os.environ if env is None else env
    assigned, problem = identity.worker_config_dir(env)
    if assigned and not problem:
        return os.path.join(assigned, PROJECTS_DIRNAME)
    # A configured-but-unusable assignment is NOT quietly downgraded to "the default namespace":
    # that is the one thing #314 says must never happen, and reading the operator's own transcripts
    # instead of the fleet's would make the oracle answer about the wrong sessions entirely.
    if problem:
        return None
    home = env.get("HOME")
    if not isinstance(home, str) or not home.strip():
        return None
    return os.path.join(home, CONFIG_DIR_DEFAULT, PROJECTS_DIRNAME)


def session_id(run_root, iid):
    """The conversation id recorded for this lane at launch (`state/sessions/<id>`, issue #298), or
    "" — which every caller here reads as "nothing to look for", never as an error."""
    try:
        with open(os.path.join(run_root, "state", "sessions", iid)) as f:
            return f.read().strip()
    except OSError:
        return ""


def for_lane(run_root, iid, text, agent="claude", env=None, patience=PATIENCE_SECONDS,
             sleep=None):
    """The oracle for one lane's session, or None when this agent has no delivery proof.

    None is a real answer and the caller must act on it — refuse the send and say why. It is NOT a
    licence to send unproven: see AGENTS above.
    """
    if agent not in AGENTS:
        return None
    return Transcript(root=transcript_root(env), session_id=session_id(run_root, iid), text=text,
                      patience=patience, sleep=sleep)


def records(run_root, iid, agent="claude", env=None, tail_bytes=TAIL_BYTES):
    """The tail of this lane's transcript, parsed — what `pane_state.classify_transcript` reads.

    THE TAIL, bounded, because a long-running worker's transcript is megabytes and this runs on
    every nudge. Bounded reads are also why the classifier's rules are all "the most recent entry
    decides": a window that starts mid-history must never change a verdict.

    An empty list is the honest answer for every failure here — no session id, no config dir,
    transcript saving off, an unreadable file. The classifier reads that as UNKNOWN, and the NUDGE
    path treats UNKNOWN as "this session has taken no turn, so it may be sitting at a first-run
    dialog" and refuses. That is why the window widens rather than reporting an emptiness it never
    looked hard enough to establish.
    """
    if agent not in AGENTS:
        return []
    oracle = Transcript(root=transcript_root(env), session_id=session_id(run_root, iid), text="")
    path = oracle._path()
    if not path:
        return []
    # At least 1, never 0. A zero window reads nothing, finds no complete line, and multiplies
    # back to zero — an infinite loop. Unreachable from any caller today (nothing passes
    # `tail_bytes`), which is exactly why it is worth closing here rather than trusting that to
    # stay true.
    window = max(int(tail_bytes), 1)
    while True:
        out, size = _tail(path, window)
        # An EMPTY answer from a NON-empty file means the window landed inside a single oversized
        # record and every line in it was a fragment. Widening is not optional politeness: the
        # caller reads an empty list as "this session has written nothing", and that now refuses the
        # nudge — so answering it off a window we never saw a whole record in would defer a healthy
        # busy worker, unboundedly, for having read a large file.
        if out or size <= window or window >= MAX_TAIL_BYTES:
            return out
        window = min(window * 8, MAX_TAIL_BYTES)


def _tail(path, window):
    """``(parsed records, file size)`` for the last ``window`` bytes, or ``([], 0)``."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(size - window, 0))
            raw = f.read()
    except OSError:
        return [], 0
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > window and lines:
        lines = lines[1:]                 # the seek almost certainly landed mid-line; drop it
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue                      # a half-flushed tail line is not a record
        out.append(parsed)
    return out, size


class Transcript:
    """The Claude oracle. Stateless between calls except for what `mark` hands back, so one
    instance per send and no shared cursor to get out of step."""

    def __init__(self, root, session_id, text, patience=PATIENCE_SECONDS, sleep=None):
        self.root = root or ""
        self.session_id = (session_id or "").strip()
        self.text = text or ""
        self._needle = _normalise(self.text)
        self.patience = max(float(patience), 0.0)
        self._sleep = sleep if sleep is not None else time.sleep

    # ---- the contract the wrapper calls -------------------------------------------------
    def mark(self):
        """Everything about the transcript that is true BEFORE the send.

        The size, not a timestamp: it is what lets `landed` read only the bytes that arrived
        afterwards, which is the whole defence against a re-nudge matching its own predecessor —
        the frozen tier sends byte-identical text every ten minutes, so "our text is in the file"
        is never on its own an answer.

        A path that does not resolve yet is not a failure. A freshly-spawned session has no
        transcript until its first turn, and re-resolving in `landed` is what lets the file appear
        between the two calls.
        """
        path = self._path()
        return {"path": path, "size": _size(path) if path else 0}

    def landed(self, mark):
        """True / False / None — see the module docstring. Polls up to `patience`."""
        if not self.session_id or not self.root or not self._needle:
            return None
        mark = mark if isinstance(mark, dict) else {}
        start = mark.get("size") or 0
        looked = False
        # Bounded by ATTEMPTS, not by a wall-clock deadline. The patience is a budget, and counting
        # it in polls keeps this decision path off the machine clock — the same discipline #217 had
        # to retrofit onto the freeze tier after a UTC-vs-local drift turned nine sim tests red
        # nightly. It also means a test can inject a sleep that does not actually sleep.
        for attempt in range(max(1, int(self.patience / POLL_SECONDS) + 1)):
            if attempt:
                self._sleep(POLL_SECONDS)
            path = mark.get("path") or self._path()
            if path:
                looked = True
                if self._carries_our_prompt(path, start):
                    return True
        # `looked` is the difference between the two refusals. Having READ the transcript and not
        # found the prompt is a positive non-delivery (the composer drop). Never having found one
        # at all says nothing about the send — transcript saving may simply be off.
        return False if looked else None

    # ---- private ------------------------------------------------------------------------
    def _path(self):
        """The transcript for this conversation, found by GLOB rather than by rebuilding Claude's
        project-slug encoding.

        The slug is the session's cwd with separators rewritten, and Claude resolves symlinks
        before encoding it (measured in the spikes: a `/private/tmp` scratchpad came back with the
        resolved path). Reconstructing that would be a second implementation of somebody else's
        private detail, wrong the first time a worktree sits behind a symlink. The session id is a
        uuid and names the file directly, so the directory it happens to sit in is not our problem.
        """
        if not self.root or not self.session_id or not _UUID_ISH.match(self.session_id):
            return ""
        try:
            entries = os.listdir(self.root)
        except OSError:
            return ""
        name = "%s.jsonl" % self.session_id
        for project in entries:
            candidate = os.path.join(self.root, project, name)
            if os.path.isfile(candidate):
                return candidate
        return ""

    def _carries_our_prompt(self, path, start):
        try:
            with open(path, "rb") as f:
                f.seek(max(int(start), 0))
                raw = f.read()
        except OSError:
            return False
        for line in raw.decode("utf-8", "replace").splitlines():
            if _is_our_prompt(line, self._needle):
                return True
        return False


# A pre-assigned Claude session id (`--session-id <uuid>`). Checked before the glob so a junk value
# — a truncated read, an id from a state home that was hand-edited — cannot become a directory
# traversal in the join below.
_UUID_ISH = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")


def _is_our_prompt(line, needle):
    """Is this transcript line a submitted user prompt carrying `needle`?

    Every guard here is a way a `type: user` entry can exist without the pane having accepted a
    prompt — see the module docstring's three exclusions.
    """
    line = line.strip()
    # The cheap reject is on the ROLE, never on the text. Matching the needle against the raw line
    # would be faster and is WRONG: the file is written with `ensure_ascii`, so a `—` in the prompt
    # is `\\u2014` on disk and every superlooper nudge contains one. That mismatch is silent — it
    # reads as "the prompt never arrived", which refuses a delivery that plainly happened — and the
    # simulation is what caught it. Compare only DECODED text from here on.
    if not line or '"user"' not in line:
        return False
    try:
        rec = json.loads(line)
    except ValueError:
        return False                      # a half-flushed line is not evidence; the poll retries
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return False
    # Every flag here marks a `type: user` entry that is NOT the pane accepting a typed prompt.
    # `isSidechain` is a subagent's conversation inside the same file; `isMeta` is the harness
    # talking to itself; `isCompactSummary` is a PROSE SUMMARY of the earlier conversation, which
    # can quote a previous prompt of ours verbatim — and landing inside the mark→landed window it
    # would prove a delivery that never happened, which is the one direction this must never fail.
    if any(rec.get(flag) for flag in ("isSidechain", "isMeta", "isCompactSummary")):
        return False
    message = rec.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    # A submitted prompt is a plain STRING. A list is a tool result or an attachment block, which
    # is the agent talking to itself about work it did, not the pane taking an instruction.
    return isinstance(content, str) and needle in _normalise(content)


def _normalise(text):
    """Whitespace-collapsed, for a containment test that survives the wrapping a long nudge gets."""
    return _WS.sub(" ", text or "").strip()


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
