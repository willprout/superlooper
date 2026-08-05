"""Issue #334 — the nudge decision core, now addressed at the session host instead of at cmux.

`bin/nudge-pane.sh` is the ONE write into a lane's session: the doorbell, the gate handback, the
progress probe, the frozen recover and the exit-interview wake ping all go through it, and the
runner branches on its exit code. #308 moved the SPAWN onto the five-verb wrapper, so
`state/panes/<id>` began holding the HOST's identifiers — and this script kept handing them to
cmux, which had never issued them. Every send and every screen read would have failed closed
against real binaries; the simulation could not see it because the cmux fake validated nothing.

What changed, and what deliberately did not:

* ADDRESSING. The lane id IS the agent name (the wrapper's rule: address by name, never by a
  cached pane id, because a pane that moves gets a new id and the old one stops resolving — the
  dragged-anchor incident class). Nothing is passed a recorded handle any more.
* THE DEAD CHECK. Was a screen heuristic — a shell prompt with no box glyphs. Is now the host's
  process facts: the pane shell has no live child. Same verdict, read from the OS.
* DELIVERY. Was `rc=0` from cmux. Is now the wrapper's proven send: rc is never evidence, so an
  unproven prompt is a REFUSAL, not a silent success.
* THE EXIT-CODE CONTRACT. Every one of the six survives, because each was paid for by an incident
  and collapsing any two is how the loop forgets which it is looking at. ONE is added: rc=7,
  "submitted but unproven", because the wrapper types first and consults its oracle afterwards —
  so that outcome is emphatically not rc=3's "nothing was typed", and a caller with an unbounded
  retry must not treat it as such.
"""
import nudge
import session_host


class FakeHost:
    """Stands in for the doorway. Records what it was asked and answers what the test staged."""

    def __init__(self, liveness=session_host.ALIVE, advisory=None, send=None, detail="staged"):
        self._liveness, self._advisory, self._send, self._detail = liveness, advisory, send, detail
        self.states, self.sends = [], []

    def state(self, session, hooks=None):
        self.states.append(session)
        return session_host.HostState(name=_name(session), liveness=self._liveness,
                                      advisory=self._advisory, detail=self._detail)

    def send(self, session, text, *, delivery, revived=False, timeout_ms=None):
        self.sends.append({"session": session, "text": text, "delivery": delivery})
        if isinstance(self._send, Exception):
            raise self._send
        return session_host.Sent(delivered=True, rc=0, detail="staged")


def _name(session):
    return session.name if isinstance(session, session_host.Session) else session


class FakeOracle:
    def mark(self):
        return {}

    def landed(self, mark):
        return True


# What a session that has taken at least one turn has written. Every test that expects a SEND needs
# one, because a session with no record at all is deferred (see the no_record test below).
FIRST_TURN = [{"type": "user", "message": {"role": "user", "content": "your brief"}}]


def _edges(host=None, oracle=FakeOracle(), records=FIRST_TURN):
    return nudge.Edges(host=lambda: host if host is not None else FakeHost(),
                       oracle=lambda *a, **k: oracle,
                       records=lambda *a, **k: list(records))


def _lane(tmp_path, workspace="w1", pane="w1:p1"):
    import panes
    state = str(tmp_path / "state")
    if workspace or pane:
        panes.record(state, "i7", session_host.Session(name="i7", workspace=workspace, pane=pane))
    return str(tmp_path)


def _nudge(tmp_path, edges=None, message="ring", iid="i7", agent="claude"):
    return nudge.nudge(str(tmp_path), iid, message, agent=agent, env={},
                       edges=edges if edges is not None else _edges())


# ------------------------------------------------------------------ the happy path

def test_a_live_session_is_sent_to_by_NAME(tmp_path):
    _lane(tmp_path)
    host = FakeHost()
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.SENT and out.state == "idle"
    assert host.sends and _name(host.sends[0]["session"]) == "i7"


def test_the_send_carries_a_delivery_oracle(tmp_path):
    # The wrapper refuses to have a code path for an unverifiable send. This is the caller's half
    # of that contract: it must supply the oracle, every time.
    _lane(tmp_path)
    host = FakeHost()
    _nudge(tmp_path, _edges(host))
    assert host.sends[0]["delivery"] is not None


def test_a_delivered_nudge_says_nothing(tmp_path):
    # Evidence is the account of a REFUSAL. A delivered nudge has nothing to explain.
    _lane(tmp_path)
    out = _nudge(tmp_path)
    assert out.code == nudge.SENT and not out.detail


# ------------------------------------------------------------------ the refusals

def test_the_exited_marker_refuses_before_anything_is_asked(tmp_path):
    # The deterministic DEAD signal, and it must short-circuit: asking a host about a session we
    # already know ended is latency spent to learn nothing.
    _lane(tmp_path)
    (tmp_path / "state" / "exited").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "exited" / "i7").write_text("123 rc=0")
    host = FakeHost()
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.DEAD and out.state == "dead"
    assert not host.states and not host.sends


def test_a_pane_with_no_live_child_is_dead_and_is_never_typed_into(tmp_path):
    # RC-DEADPANE: the message would run as a permission-bypassed shell command. The screen
    # heuristic inferred this from a prompt glyph; the host reads it off the process table.
    _lane(tmp_path)
    host = FakeHost(liveness=session_host.DEAD)
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.DEAD and not host.sends


def test_an_unknown_liveness_defers_rather_than_typing(tmp_path):
    # Absence of signal is UNKNOWN, never idle (c2). An unreadable screen deferred before and an
    # unanswerable host defers now — same direction, because the alternative is typing blind.
    _lane(tmp_path)
    host = FakeHost(liveness=session_host.UNKNOWN)
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.DEFERRED and not host.sends


def test_a_lane_with_no_recorded_session_is_nothing_to_ring(tmp_path):
    _lane(tmp_path, workspace="", pane="")
    host = FakeHost()
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.DEAD and not host.sends


def test_an_unaddressable_id_fails_loudly(tmp_path):
    _lane(tmp_path)
    out = _nudge(tmp_path, _edges(FakeHost()), iid="NOT A NAME")
    assert out.code == nudge.FAILED


def test_auth_death_in_the_record_refuses_with_its_own_code_and_variant(tmp_path):
    # i336: the runner typed into a session whose auth was dead for 94 minutes because the pane
    # rendered a perfectly normal composer. The refusal and the owner's remedy both survive the
    # move to the host — read now from the session's own record.
    _lane(tmp_path)
    host = FakeHost()
    records = [{"type": "assistant", "isApiErrorMessage": True,
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Invalid API key · Fix external API key"}]}}]
    out = _nudge(tmp_path, _edges(host, records=records))
    assert out.code == nudge.LOGGED_OUT and out.state == "logged_out"
    assert out.auth == "invalid_api_key" and not host.sends


def test_an_open_question_dialog_refuses_with_its_own_code(tmp_path):
    # i280: a worker blocked on its own AskUserQuestion was walked into a false park. This is a
    # LIVE lane — the caller must surface it, not escalate it — and a stray Enter would SELECT.
    _lane(tmp_path)
    host = FakeHost()
    records = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {}}]}}]
    out = _nudge(tmp_path, _edges(host, records=records))
    assert out.code == nudge.AT_DIALOG and not host.sends


def test_an_advisory_that_says_blocked_takes_the_send_away(tmp_path):
    """The host's lifecycle word GATES NOTHING (plan §5.2) — it may not authorise anything, because
    its `idle` means "ready AND seen in the focused UI", an attended notion that is meaningless for
    an unattended runner. This is the one direction it is still worth reading in: a word that only
    ever REFUSES grants nothing, and costs one retry when it is wrong. It is a supplement to the
    record-side dialog check, never a substitute — see the module."""
    _lane(tmp_path)
    host = FakeHost(advisory="blocked")
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.DEFERRED and not host.sends


def test_an_advisory_never_grants_a_send_on_its_own(tmp_path):
    # The other half of the same rule: `idle` from the host does not make a dead pane sendable.
    _lane(tmp_path)
    host = FakeHost(liveness=session_host.DEAD, advisory="idle")
    assert _nudge(tmp_path, _edges(host)).code == nudge.DEAD


# ------------------------------------------------------------------ delivery is the verdict

def test_an_unproven_delivery_is_a_refusal_not_a_send(tmp_path):
    # The heart of it: cmux's rc=0 was accepted as delivery and lied 6/6 times. Now the wrapper
    # raises unless the oracle confirms, and a nudge that cannot be proven is never called a send.
    _lane(tmp_path)
    host = FakeHost(send=session_host.DeliveryUnproven("nothing proved this prompt was delivered"))
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.UNPROVEN and out.state == "unproven"
    assert "deliver" in out.detail.lower()


def test_an_unproven_delivery_is_not_reported_as_nothing_typed(tmp_path):
    """UNPROVEN must not collapse into DEFERRED, and the reason is concrete rather than pedantic.
    `send` submits the prompt and consults the oracle AFTERWARDS, so this branch means "it was
    typed and we cannot prove it arrived" — while rc=3 promises the opposite. A caller whose retry
    is unbounded (the gate's one nudge per cause) reads rc=3 as "nothing happened, try again", and
    would re-submit a real handback into a live worker every tick, forever."""
    _lane(tmp_path)
    host = FakeHost(send=session_host.DeliveryUnproven("nope"))
    out = _nudge(tmp_path, _edges(host))
    assert out.code != nudge.DEFERRED
    assert host.sends, "the prompt really was handed to the host before the oracle refused it"


def test_a_host_refusal_of_the_send_is_a_failure(tmp_path):
    _lane(tmp_path)
    host = FakeHost(send=session_host.HostError("the host would not"))
    out = _nudge(tmp_path, _edges(host))
    assert out.code == nudge.FAILED and out.state == "send_failed"


def test_an_agent_with_no_delivery_oracle_refuses_rather_than_sending_blind(tmp_path):
    """Codex has no transcript this engine can read and its proven channel (the nonce-fenced ack
    file) answers minutes later, not when `send` returns. Sending anyway would be the rc=0-is-
    delivery lie again, so the send does not happen and the refusal names the gap."""
    _lane(tmp_path)
    host = FakeHost()
    edges = nudge.Edges(host=lambda: host, oracle=lambda *a, **k: None, records=lambda *a, **k: [])
    out = _nudge(tmp_path, edges, agent="codex")
    assert out.code == nudge.FAILED and out.state == "no_oracle"
    assert not host.sends and "oracle" in out.detail.lower()


def test_a_session_that_has_written_no_record_is_not_typed_into(tmp_path):
    """The screen classifier refused on a MENU because Enter at a selection SELECTS. The host
    exposes no screen, so the one case that still has a signal is this one: a session that has
    written nothing has taken no turn, and what a session sits in before its first turn is the
    first-run trust dialog. Costs a healthy worker nothing — a real one records its brief as its
    first turn."""
    _lane(tmp_path)
    host = FakeHost()
    out = _nudge(tmp_path, _edges(host, records=[]))
    assert out.code == nudge.DEFERRED and out.state == "no_record"
    assert not host.sends


def test_an_agent_that_keeps_no_readable_record_is_not_deferred_for_it(tmp_path):
    # "This session has written nothing yet" and "this agent keeps nothing we can read" are two
    # different silences. Only the first is a reason to wait; the second is the oracle's refusal.
    _lane(tmp_path)
    host = FakeHost()
    edges = nudge.Edges(host=lambda: host, oracle=lambda *a, **k: None, records=lambda *a, **k: [])
    assert _nudge(tmp_path, edges, agent="codex").state == "no_oracle"


# ------------------------------------------------------------------ the evidence line (#152/#174)

def test_a_refusal_names_its_verdict_machine_readably(tmp_path):
    _lane(tmp_path)
    line = nudge.evidence_line("i7", nudge.Outcome(nudge.DEFERRED, "unproven", detail="because"))
    assert line.startswith("[nudge] i7 state=unproven ")


def test_an_auth_refusal_pins_the_variant_to_the_state_token(tmp_path):
    # The runner mines `state=logged_out auth=<variant>` off the stderr tail, anchored on the
    # PAIRING — a bare `auth=` anywhere in the tail would let a worker whose own screen shows this
    # source file hand the runner an auth verdict quoted out of a comment.
    line = nudge.evidence_line("i7", nudge.Outcome(nudge.LOGGED_OUT, "logged_out", auth="login",
                                                  detail="d"))
    assert "state=logged_out auth=login" in line


def test_a_non_auth_refusal_carries_no_auth_token(tmp_path):
    line = nudge.evidence_line("i7", nudge.Outcome(nudge.DEFERRED, "unproven", detail="d"))
    assert "auth=" not in line


def test_the_captured_detail_is_bounded(tmp_path):
    # A refusal's detail rides into a journal record and a GitHub memo (the 2026-07-07
    # binary-in-reports incident), so it can never be an unbounded dump.
    _lane(tmp_path)
    host = FakeHost(send=session_host.DeliveryUnproven("x" * 40000))
    out = _nudge(tmp_path, _edges(host))
    assert 0 < len(out.detail) < 3000
