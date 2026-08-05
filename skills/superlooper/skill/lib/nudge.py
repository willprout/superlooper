"""The ONE write into a lane's live session — through the doorway, never at a multiplexer (#334).

Every message the loop ever puts in front of a worker comes through here: the gate's handback, the
progress probe, the frozen recover's wake, the idle status peek and the exit-interview ping. The
runner branches on the exit code, so the six codes below are a contract, not a convention.

WHAT #334 CHANGED. Issue #308 moved every session SPAWN onto the five-verb wrapper, so
`state/panes/<id>` and its `.ws` began holding the SESSION HOST's pane and workspace instead of
cmux surface UUIDs. `_close_pane` moved with the spawn; this path did not, and kept handing those
identifiers to `cmux read-screen` / `cmux send`. Against real binaries every ring would have failed
closed — the exact shape of run-20260625-1857, where 156/156 doorbell rings deferred because the
send could not resolve its target and nobody could see why.

Four things are different now, and one deliberately is not:

1. **Addressed by NAME.** The lane id IS the agent name. The wrapper's own rule (adoption plan §7):
   a pane moved to another workspace gets a new id and the old one stops resolving for outside
   callers, so a cached handle breaking when the owner rearranges his window IS the cmux
   dragged-anchor class. Nothing here reads a handle to address anything; `state/panes/<id>` is
   consulted for ONE thing — whether a session was ever recorded for this lane at all.
2. **DEAD is a process fact.** It was a screen heuristic (a shell prompt with no box glyphs); it is
   now the host's own read of the pane shell's live children, through the wrapper's `state`. Same
   verdict, from the OS instead of from a render — and the render was the weaker of the two.
3. **Delivery is PROVEN or the nudge did not happen.** cmux's rc=0 was taken as delivery and lied
   6/6 times in the spikes. The wrapper refuses to have a code path for an unverifiable send, so
   this supplies the transcript oracle (`lib/delivery.py`) and an unproven prompt is a REFUSAL.
4. **The two hardest-won refusals moved surfaces, not away.** Auth-death (#151/#174) and the
   session's own open question (#151/i280) came off the screen and onto the session's own record —
   see `pane_state.classify_transcript`. The host exposes no screen read and must not grow one
   (plan §7.3), and the record is better evidence anyway: it cannot scroll out of a 40-line window
   and a worker cannot talk its way into a verdict.

What did NOT change is the exit-code contract. Every code below was paid for by a measured
incident, and collapsing any two of them is how the loop forgets which one it is looking at.

EXIT CODES (load-bearing — the runner branches on these):
  0 = SENT, and PROVEN delivered. Note "delivered" is still not "a turn was taken": mid-generation
      input is queued, and the runner confirms real progress through activity/report, as before.
  1 = FAILED. The host refused the send, the id cannot address an agent, or this agent has no
      delivery oracle at all — a channel fault, not the session's.
  3 = DEFERRED. Nothing was typed and the session may be perfectly healthy: the host could not
      vouch for the pane, its advisory says the agent is waiting on a person, or the send could not
      be PROVEN. Caller retries later.
  4 = DEAD. The agent process is gone. Caller RESTARTS — it must NEVER type, or the message would
      run as a permission-bypassed shell command (RC-DEADPANE).
  5 = AUTH DEAD IN-SESSION (#151, widened by #174). Every turn is refused. Nothing was typed.
      Caller ALERTS THE OWNER — it must not retry (a nudge cannot be answered) and must not restart
      (the relaunch re-enters dead auth). ONE code for the whole family because the caller's
      handling is identical; WHICH banner it was rides out on stderr as `auth=<variant>` so the
      alert can name the owner's actual remedy.
  6 = AT DIALOG (#151). The session raised its OWN question and is waiting on an answer. Nothing
      was typed. This is a LIVE, working session — surface it, never escalate it.

5 and 6 are refusals exactly like 3; they differ only in telling the caller WHY, which is the whole
point. The old single "deferred" made a dead-auth session, a session asking a question and a
genuine menu indistinguishable, and the runner treated all three as "stuck".
"""
import os
import sys

import delivery as delivery_mod
import evidence
import pane_state
import panes
import session_host

SENT, FAILED, DEFERRED, DEAD, LOGGED_OUT, AT_DIALOG = 0, 1, 3, 4, 5, 6

# How long ONE control call to the host may hang, and how long the prompt itself may take.
#
# Sized to fit inside the runner's own NUDGE_TIMEOUT with room to spare. A nudge now makes several
# calls where it used to make three cmux invocations, and the failure mode of getting this wrong is
# not a slow nudge — it is the runner killing the script mid-send and recording rc=124 ("it hung")
# for a host that was merely slow, which reads as a channel fault and holds the queue.
#
# 30s for the prompt is generous for a doorbell: the host's own documented behaviour is to refuse
# with `agent_prompt_stalled` if a non-working pane shows no lifecycle change within five seconds.
CALL_SECONDS = 10
PROMPT_TIMEOUT_MS = 30000

# The host lifecycle words that TAKE A SEND AWAY. Read the module's rule carefully: the host's
# advisory GATES NOTHING (plan §5.2) — its `idle` encodes "ready AND seen in the focused UI", an
# attended-operator notion that means nothing to an unattended runner, so it may never authorise
# anything. A word that can only ever REFUSE is a different proposition: it grants nothing, it
# costs one retry when it is wrong, and it is a second net under the record-side dialog check for
# the one case where typing does real damage (a stray Enter SELECTS an option).
#
# It is a supplement, never the mechanism: `blocked` is the host's word for "waiting on a person"
# and is not AskUserQuestion-specific, so it can neither name the variant nor be relied on to fire.
REFUSING_ADVISORY = ("blocked",)


class Outcome:
    """One nudge's verdict. `state` is the machine-readable token the runner reads back off
    stderr; `auth` rides beside it only where it means something (#174)."""

    __slots__ = ("code", "state", "auth", "detail")

    def __init__(self, code, state, auth=None, detail=""):
        self.code, self.state, self.auth, self.detail = code, state, auth, detail

    def __repr__(self):
        return "Outcome(code=%r, state=%r, auth=%r)" % (self.code, self.state, self.auth)


class Edges:
    """Every external edge: the doorway, the delivery oracle and the session's own record.

    Injected for the same reason `session_host.Probe` is — so no test resolves a real host binary
    (the conftest ratchet), and so a staged host/oracle can drive every branch below deterministically.
    """

    def __init__(self, host=None, oracle=None, records=None):
        self._host = host or _default_host
        self._oracle = oracle or delivery_mod.for_lane
        self._records = records or delivery_mod.records

    def host(self):
        return self._host()

    def oracle(self, run_root, iid, text, agent, env):
        return self._oracle(run_root, iid, text, agent=agent, env=env)

    def records(self, run_root, iid, agent, env):
        return self._records(run_root, iid, agent=agent, env=env)


def _default_host():
    return session_host.SessionHost(call_seconds=CALL_SECONDS,
                                    prompt_timeout_ms=PROMPT_TIMEOUT_MS)


def nudge(run_root, iid, message, agent="claude", env=None, edges=None):
    """Put one message in front of a lane's session, or refuse and say why. Never raises."""
    env = os.environ if env is None else env
    edges = edges or Edges()
    state_dir = os.path.join(run_root, "state")

    # 0. Can this id address an agent at all? Asked FIRST, before any disk or host read, because a
    #    caller that hands this an unaddressable id has a bug and every later answer would be about
    #    the wrong question. The doorway validates it again; this is where the caller hears it.
    try:
        session_host.name_for(iid)
    except session_host.NameRefused as e:
        return Outcome(FAILED, "unaddressable", detail=_bound(str(e)))

    # 1. The deterministic DEAD signal, first and cheapest. start-session.sh's EXIT trap writes it,
    #    so it is the one answer that needs nobody's cooperation — and asking a host about a
    #    session we already know ended is latency spent to learn nothing.
    if os.path.exists(os.path.join(state_dir, "exited", iid)):
        return Outcome(DEAD, "dead", detail="the session's exited marker is present")

    # 2. Is there a recorded session at all? This is the ONLY thing the recorded handle is read for
    #    here — never to address anything (see the module's rule 1).
    handle = panes.read(state_dir, iid)
    session = handle.as_session(iid)
    if session is None:
        return Outcome(DEAD, "no_session",
                       detail="no session window is recorded for this lane (state/panes/%s)" % iid)

    host = edges.host()

    # 3. Liveness, from the OS by way of the doorway. This is the RC-DEADPANE guard and it is the
    #    one refusal that must never be skipped: a message typed into a bare shell RUNS.
    live = host.state(session)
    if live.liveness == session_host.DEAD:
        return Outcome(DEAD, "dead", detail=_bound(live.detail))
    if live.liveness != session_host.ALIVE:
        return Outcome(DEFERRED, "unknown",
                       detail=_bound("the host could not vouch for this session: %s" % live.detail))
    if isinstance(live.advisory, str) and live.advisory.lower() in REFUSING_ADVISORY:
        return Outcome(DEFERRED, "advisory_blocked",
                       detail="the host reports this agent %r — waiting on a person, so nothing "
                              "was typed" % live.advisory)

    # 4. The two refusals that came off the screen and onto the session's own record.
    sensed = pane_state.classify_transcript(edges.records(run_root, iid, agent, env), agent=agent)
    if sensed == "logged_out":
        variant = pane_state.transcript_auth_death(edges.records(run_root, iid, agent, env))
        return Outcome(LOGGED_OUT, "logged_out", auth=variant,
                       detail="this session's own record shows its auth is dead in-session (%s): "
                              "every turn is refused, so a nudge cannot be answered"
                              % (variant or "variant unrecognised"))
    if sensed == "at_dialog":
        return Outcome(AT_DIALOG, "at_dialog",
                       detail="this session raised its own question and is waiting on an answer — "
                              "live, not stuck; nothing was typed")

    # 5. The send. `delivery` has no default at the doorway on purpose: a send nobody can check is
    #    the exact operation that returned rc=0 on 6/6 non-deliveries.
    oracle = edges.oracle(run_root, iid, message, agent, env)
    if oracle is None:
        return Outcome(FAILED, "no_oracle",
                       detail="no delivery oracle exists for agent %r, and rc carries no delivery "
                              "information — refusing to send a prompt whose arrival nothing could "
                              "confirm" % agent)
    try:
        host.send(session, message, delivery=oracle)
    except session_host.DeliveryUnproven as e:
        return Outcome(DEFERRED, "unproven", detail=_bound(str(e)))
    except session_host.HostError as e:
        return Outcome(FAILED, "send_failed", detail=_bound(str(e)))
    return Outcome(SENT, "idle")


def evidence_line(iid, outcome):
    """The machine-readable first line of a refusal.

    `state=<verdict>` is what the runner's evidence record reads back, and `auth=<variant>` rides
    on the SAME line for the reason #174 chose: a tail cut can then never keep one without the
    other, and the runner's regex is anchored on the pairing rather than on a bare `auth=`
    anywhere in the tail — because that tail also carries verbatim text a worker's own session
    could have produced.
    """
    auth = " auth=%s" % outcome.auth if outcome.state == "logged_out" and outcome.auth else ""
    return "[nudge] %s state=%s%s — %s" % (iid, outcome.state or "unclassified", auth,
                                           _WHY.get(outcome.code, "refused"))


_WHY = {
    FAILED: "the message could not be sent",
    DEFERRED: "deferring — nothing was typed",
    DEAD: "the session is DEAD (the agent process is gone) — not typing; caller must restart",
    LOGGED_OUT: "session auth is DEAD in-session — not typing; caller must alert the owner",
    AT_DIALOG: "session is asking a question in-window — not typing; live, not stuck",
}


def _bound(text):
    """Bounded by the SAME function the runner's own records use (`lib/evidence.bound`), not a
    second implementation: it slices by CHARACTER and strips control bytes, so a detail carrying
    box-drawing glyphs cannot emit invalid UTF-8 into the subprocess capture of the tick that was
    only trying to explain itself."""
    return evidence.bound(text or "", limit=evidence.SCREEN_SNIPPET_MAX)


def main(argv, env=None, out=None, edges=None):
    """`nudge-pane.sh <id> <message>` — the shell entry point's whole body.

    NOTE the arity change: the old form took a recorded SURFACE as its first argument and handed it
    to cmux. There is no such argument any more, and that absence is the point — a caller cannot
    pass a handle to a path that no longer has anywhere to put one.
    """
    env = os.environ if env is None else env
    out = sys.stderr if out is None else out
    if len(argv) != 2:
        out.write("usage: nudge-pane.sh <id> <message>\n")
        return 64
    iid, message = argv
    run_root = env.get("SL_RUN_ROOT") or ""
    if not run_root:
        # Every caller MUST export SL_RUN_ROOT (an unset root was an observed failure). Loud, never
        # a silent proceed against an empty path.
        out.write("[nudge] SL_RUN_ROOT required\n")
        return 64
    outcome = nudge(run_root, iid, message, agent=env.get("SL_AGENT") or "claude", env=env,
                    edges=edges)
    if outcome.code == SENT:
        return SENT                        # a delivered nudge has nothing to explain
    out.write(evidence_line(iid, outcome) + "\n")
    detail = (outcome.detail or "").strip()
    # #152: a refusal must carry BOTH halves of the judgement — the verdict AND what it was read
    # from. i160 sat 43 minutes on an ambiguous defer nobody could re-classify afterwards, because
    # the evidence that produced it was never kept.
    out.write("[nudge] %s evidence (bounded — what the verdict was read from):\n%s\n"
              % (iid, detail or "captured: none, reason unknown"))
    return outcome.code


if __name__ == "__main__":
    # Run directly by bin/nudge-pane.sh. Python puts THIS file's directory at the front of
    # sys.path, so the sibling `lib/` modules imported above resolve off the engine that owns this
    # script — never off the caller's cwd, which is a worker's worktree.
    sys.exit(main(sys.argv[1:]))
