"""Issue #334 — the delivery ORACLE the wrapper's `send` requires.

`lib/session_host.SessionHost.send` refuses to have a code path for an unproven send: `rc` carries
no delivery information (6/6 measured prompts typed their text, dropped the submission and returned
rc=0 — docs/SPIKES-2026-07-30-supervised.md §8), so it takes an oracle from its caller and raises
`DeliveryUnproven` unless that oracle says the prompt landed. Nothing in the engine had one; this
is it.

The oracle is TRANSCRIPT-side, which is what the adoption plan §4 prescribes and what every spike
used. It is also the one signal that survives the plan's other rule — screen reads can never be the
evidence path (§7.3) — because a transcript is a FILE the agent writes, not a render we scrape.

What it checks and what it deliberately does NOT: a NEW user entry carrying our text. Not an
assistant reply (a busy session legitimately queues the prompt and answers minutes later, and the
wrapper's send must not block for a whole turn), and never the CONTENT of any reply — the spikes'
methodological finding is that a delivery oracle checks that an entry exists, never what it says,
because a model that flubs the answer would otherwise read as a delivery failure.
"""
import json
import os

import pytest

import delivery
import identity

SESSION = "39c26db1-edc1-4f6b-a5a2-1e020e737657"


def _root(tmp_path, slug="-tmp-work"):
    d = tmp_path / ".claude" / "projects" / slug
    d.mkdir(parents=True)
    return str(tmp_path / ".claude" / "projects"), d / ("%s.jsonl" % SESSION)


def _entry(role, text, **over):
    rec = {"type": role, "message": {"role": role, "content": text}}
    rec.update(over)
    return json.dumps(rec) + "\n"


def _oracle(root, text, **over):
    kw = dict(root=root, session_id=SESSION, text=text, patience=0.0, sleep=lambda s: None)
    kw.update(over)
    return delivery.Transcript(**kw)


# --------------------------------------------------------------- the happy path

def test_a_user_entry_carrying_our_text_after_the_mark_is_delivery(tmp_path):
    root, path = _root(tmp_path)
    path.write_text(_entry("user", "an older prompt"))
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "ring the doorbell"))
    assert oracle.landed(mark) is True


def test_an_entry_that_was_already_there_is_not_delivery(tmp_path):
    """The mark is the whole defence against a re-nudge reading its own predecessor as proof: the
    text is identical every time the frozen tier fires, so only what appears AFTER counts."""
    root, path = _root(tmp_path)
    path.write_text(_entry("user", "ring the doorbell"))
    oracle = _oracle(root, "ring the doorbell")
    assert oracle.landed(oracle.mark()) is False


def test_nothing_new_at_all_is_not_delivery(tmp_path):
    root, path = _root(tmp_path)
    path.write_text(_entry("user", "an older prompt"))
    oracle = _oracle(root, "ring the doorbell")
    assert oracle.landed(oracle.mark()) is False


def test_a_different_prompt_landing_is_not_our_delivery(tmp_path):
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "some other prompt entirely"))
    assert oracle.landed(mark) is False


def test_delivery_is_proven_without_any_assistant_reply(tmp_path):
    # A busy session queues the prompt and answers later. Requiring a reply would make every nudge
    # into a working lane read as a non-delivery — the failure mode this oracle must not have.
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "ring the doorbell"))
    assert oracle.landed(mark) is True


def test_the_transcript_file_can_appear_after_the_mark(tmp_path):
    # A freshly-spawned session has no transcript until its first turn. The mark must survive that
    # rather than reading "no file" as a permanent blindness.
    root = str(tmp_path / ".claude" / "projects")
    os.makedirs(os.path.join(root, "-tmp-work"))
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(os.path.join(root, "-tmp-work", "%s.jsonl" % SESSION), "w") as f:
        f.write(_entry("user", "ring the doorbell"))
    assert oracle.landed(mark) is True


# --------------------------------------------------------------- the honest unknowns

def test_no_session_id_is_unknown_not_a_verdict(tmp_path):
    root, _ = _root(tmp_path)
    oracle = _oracle(root, "hello", session_id="")
    assert oracle.landed(oracle.mark()) is None


def test_no_transcript_root_is_unknown_not_a_verdict(tmp_path):
    # "Transcript saving is off" and a config dir nobody can read look the same from here, and
    # neither is evidence of anything. None fails closed at the wrapper (DeliveryUnproven).
    oracle = _oracle(str(tmp_path / "nowhere"), "hello")
    assert oracle.landed(oracle.mark()) is None


def test_an_unparseable_line_does_not_take_a_real_delivery_down(tmp_path):
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write("{ not json at all\n")
        f.write(_entry("user", "ring the doorbell"))
    assert oracle.landed(mark) is True


def test_a_sidechain_entry_is_not_this_session_being_prompted(tmp_path):
    # Sidechain entries are a SUBAGENT's conversation inside the same file. A subagent quoting the
    # nudge back (they read the same files we write) must never read as the pane having taken it.
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "ring the doorbell", isSidechain=True))
    assert oracle.landed(mark) is False


@pytest.mark.parametrize("flag", ["isMeta", "isCompactSummary"])
def test_a_meta_or_compaction_entry_is_not_a_prompt(tmp_path, flag):
    """Both are real `type: user`, non-sidechain, STRING-content records — 2 of them in this
    machine's own transcripts. A compact summary is prose ABOUT the earlier conversation and can
    quote a previous nudge verbatim; landing inside the mark→landed window it would prove a
    delivery that never happened, which is the one direction this must never fail."""
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "ring the doorbell", **{flag: True}))
    assert oracle.landed(mark) is False


def test_a_tool_result_entry_is_not_a_prompt(tmp_path):
    # Claude records tool results as `type: user` with a LIST content. A tool result whose text
    # happens to contain the nudge (a worker `cat`ing the runner log would produce exactly that)
    # must not read as the prompt having been submitted.
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring the doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "ring the doorbell"}]}}) + "\n")
    assert oracle.landed(mark) is False


def test_a_non_ascii_prompt_is_still_recognised(tmp_path):
    """Caught by the simulation, and it would have broken EVERY real nudge: the transcript is
    written with `ensure_ascii`, so an em-dash lands on disk as `\\u2014` — and every message this
    engine sends a worker contains one. A matcher that looked at the raw line saw no needle and
    reported a delivery that plainly happened as unproven, forever."""
    root, path = _root(tmp_path)
    path.write_text("")
    text = "[superlooper gate] Rewrite the report — the runner checks mechanically."
    oracle = _oracle(root, text)
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}},
                           ensure_ascii=True) + "\n")
    assert "\\u2014" in path.read_text(), "the fixture must reproduce the escaping"
    assert oracle.landed(mark) is True


def test_whitespace_differences_do_not_defeat_the_match(tmp_path):
    root, path = _root(tmp_path)
    path.write_text("")
    oracle = _oracle(root, "ring   the\n doorbell")
    mark = oracle.mark()
    with open(path, "a") as f:
        f.write(_entry("user", "ring the doorbell"))
    assert oracle.landed(mark) is True


def test_landed_waits_for_a_transcript_that_is_still_being_flushed(tmp_path):
    """`--wait` returning settled does not mean the JSONL has hit disk. The oracle owns its own
    patience so a send is never called unproven over a few hundred milliseconds of lag."""
    root, path = _root(tmp_path)
    path.write_text("")
    slept = []

    def _write_on_second_look(seconds):
        slept.append(seconds)
        with open(path, "a") as f:
            f.write(_entry("user", "ring the doorbell"))

    oracle = _oracle(root, "ring the doorbell", patience=5.0, sleep=_write_on_second_look)
    assert oracle.landed(oracle.mark()) is True
    assert slept, "the oracle must be willing to wait at least once"


def test_patience_is_bounded(tmp_path):
    root, path = _root(tmp_path)
    path.write_text("")
    slept = []
    oracle = _oracle(root, "never arrives", patience=1.0, sleep=slept.append)
    assert oracle.landed(oracle.mark()) is False
    assert sum(slept) <= 1.0 + delivery.POLL_SECONDS


# --------------------------------------------------------------- resolution

def test_the_root_follows_the_machines_worker_config_dir_assignment(tmp_path):
    # #314 gives each worker its own CLAUDE_CONFIG_DIR, and Claude keeps its transcripts under it.
    # A resolver that hardcoded ~/.claude would look in the operator's own namespace and find
    # nothing — an oracle that answers "cannot tell" about every fleet worker.
    assigned = str(tmp_path / "fleet")
    assert delivery.transcript_root({"SL_FLEET_CLAUDE_CONFIG_DIR": assigned,
                                     "HOME": str(tmp_path)}) == os.path.join(assigned, "projects")


def test_the_root_is_canonicalised_the_way_the_launcher_canonicalises_it(tmp_path):
    """The engine's OWN suggested fleet value is `~/.claude-fleet`, and every spawn path expands it
    through `identity.canonical`. A second derivation that joined the raw string would hand
    os.listdir a literal `~`, find nothing, and answer "cannot tell" about every nudge on the
    machine — while the workers themselves ran perfectly under the expanded path. Silent, and it
    reads as a healthy-session defer."""
    env = {"SL_FLEET_CLAUDE_CONFIG_DIR": identity.SUGGESTED_FLEET_DIR, "HOME": str(tmp_path)}
    assert delivery.transcript_root(env) == os.path.join(
        str(tmp_path), ".claude-fleet", "projects")


def test_an_inherited_session_config_dir_does_not_steer_the_oracle(tmp_path):
    """A runner started from inside a worker or debugger pane carries THAT session's
    CLAUDE_CONFIG_DIR. The launch floor refuses to forward one, `identity_probe_env` scrubs it and
    `_script_env` pins its sibling empty, all for the same reason: it is a second-hand answer about
    somebody else's credential namespace. Ranking it here would point the oracle at one namespace
    while every worker ran in another."""
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "somebody-elses"),
           "SL_CLAUDE_CONFIG_DIR": str(tmp_path / "also-not-ours"),
           "SL_FLEET_CLAUDE_CONFIG_DIR": str(tmp_path / "fleet"), "HOME": str(tmp_path)}
    assert delivery.transcript_root(env) == os.path.join(str(tmp_path / "fleet"), "projects")


def test_a_configured_but_unusable_assignment_resolves_to_nothing(tmp_path):
    # NOT quietly downgraded to the default namespace — that substitution is the one thing #314
    # exists to prevent, and here it would make the oracle answer about the operator's own sessions.
    assert delivery.transcript_root({"SL_FLEET_CLAUDE_CONFIG_DIR": "relative/dir",
                                     "HOME": str(tmp_path)}) is None


def test_the_root_falls_back_to_the_default_claude_home(tmp_path):
    assert delivery.transcript_root({"HOME": str(tmp_path)}) == str(
        tmp_path / ".claude" / "projects")


def test_no_home_and_no_assignment_resolves_to_nothing(tmp_path):
    assert delivery.transcript_root({}) is None


def test_for_lane_reads_the_recorded_session_id(tmp_path):
    os.makedirs(str(tmp_path / "state" / "sessions"))
    (tmp_path / "state" / "sessions" / "i7").write_text(SESSION + "\n")
    oracle = delivery.for_lane(str(tmp_path), "i7", "hello", agent="claude",
                               env={"HOME": str(tmp_path)})
    assert oracle is not None and oracle.session_id == SESSION


# --------------------------------------------------------------- the record the sense reads

def _lane(tmp_path, lines):
    os.makedirs(str(tmp_path / "state" / "sessions"))
    (tmp_path / "state" / "sessions" / "i7").write_text(SESSION)
    _, path = _root(tmp_path)
    path.write_text("".join(lines))
    return {"HOME": str(tmp_path)}


def test_records_parses_the_lanes_transcript(tmp_path):
    env = _lane(tmp_path, [_entry("user", "one"), _entry("assistant", "two")])
    got = delivery.records(str(tmp_path), "i7", env=env)
    assert [r["message"]["content"] for r in got] == ["one", "two"]


def test_records_reads_only_the_tail(tmp_path):
    # A worker's transcript is megabytes and this runs on every nudge. The bound is also why every
    # verdict downstream is "the most recent entry decides".
    env = _lane(tmp_path, [_entry("user", "x" * 500) for _ in range(50)] + [_entry("user", "last")])
    got = delivery.records(str(tmp_path), "i7", env=env, tail_bytes=2000)
    assert got and got[-1]["message"]["content"] == "last"
    assert len(got) < 50


def test_a_tail_that_lands_mid_line_drops_the_fragment(tmp_path):
    # The seek is byte-wise, so the first line in the window is almost certainly half a record.
    # Keeping it would feed the classifier garbage; parsing would fail anyway, but the drop makes
    # the contract explicit rather than accidental.
    env = _lane(tmp_path, [_entry("user", "y" * 4000), _entry("user", "the whole one")])
    got = delivery.records(str(tmp_path), "i7", env=env, tail_bytes=200)
    assert [r["message"]["content"] for r in got] == ["the whole one"]


def test_records_widens_its_window_rather_than_reporting_a_false_emptiness(tmp_path):
    """A single record can be larger than the ordinary window — a 300KB tool result is unremarkable
    — and a window landing entirely inside one yields NO complete line. Reported as empty that reads
    as "this session has written nothing", which the nudge path now REFUSES on: a busy, healthy
    worker would be deferred, unboundedly, for having read a large file."""
    env = _lane(tmp_path, [_entry("user", "first"),
                           _entry("user", "z" * 40000),          # bigger than the window below
                           _entry("assistant", "last")])
    got = delivery.records(str(tmp_path), "i7", env=env, tail_bytes=1000)
    assert got, "an oversized record must not be reported as no record at all"
    assert got[-1]["message"]["content"] == "last"


def test_records_is_empty_when_there_is_no_transcript(tmp_path):
    # Not an error. The classifier reads it as UNKNOWN, which lib/nudge.py turns into a deferral —
    # so this emptiness has to be genuine, which is what the widening read below is about.
    assert delivery.records(str(tmp_path), "i7", env={"HOME": str(tmp_path)}) == []


def test_records_is_empty_for_an_agent_with_no_transcript_vocabulary(tmp_path):
    env = _lane(tmp_path, [_entry("user", "one")])
    assert delivery.records(str(tmp_path), "i7", agent="codex", env=env) == []


def test_an_agent_with_no_oracle_gets_none_rather_than_a_pretend_one(tmp_path):
    """The boundary this issue had to establish. Claude's delivery proof is its transcript; Codex
    has no equivalent this engine can read, and its proven channel (the nonce-fenced ack file) is
    asynchronous — minutes, not the moment `send` returns. Returning a permissive stand-in would
    reintroduce exactly the rc=0-is-delivery lie the wrapper exists to refuse, so the honest answer
    is None and the caller refuses the send."""
    assert delivery.for_lane(str(tmp_path), "i7", "hello", agent="codex",
                             env={"HOME": str(tmp_path)}) is None
