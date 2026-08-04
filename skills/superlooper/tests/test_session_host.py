"""The five-verb wrapper's contract (issue #304): spawn / send / state / exit / kill.

Every distrust rule the 2026-07-29/30 spikes paid for is enforced INSIDE the wrapper, so these
tests are where those rules are pinned. They drive the module through an injected probe — no test
resolves a real ``herdr`` (conftest neutralizes ``SL_HERDR`` for the same reason it neutralizes
cmux/gh/claude), and the fake speaks the real 0.7.5 envelope shape read out of
``herdr api schema --json``: ``{"id": ..., "result": {"type": ..., ...}}`` on success,
``{"id": ..., "error": {"code": ..., "message": ...}}`` with rc=1 on failure.

The evidence behind each rule lives in docs/SPIKES-2026-07-30-supervised.md; the tests name it
where a reader would otherwise think the wrapper is being paranoid for its own sake.
"""
import ast
import json
import os
from pathlib import Path

import pytest

import reorient
import session_host

_MODULE = Path(session_host.__file__)


# --------------------------------------------------------------------- the fake host

def _ok(payload):
    """A herdr success envelope."""
    return json.dumps({"id": "1", "result": payload})


def _err(code, message):
    """A herdr error envelope — the CLI prints these on stderr and exits 1."""
    return json.dumps({"id": "1", "error": {"code": code, "message": message}})


def _ws_created(ws="w1", tab="w1:t1", pane="w1:p1"):
    return _ok({
        "type": "workspace_created",
        "workspace": {"workspace_id": ws, "number": 1, "label": "i304", "focused": False,
                      "pane_count": 1, "tab_count": 1, "active_tab_id": tab,
                      "agent_status": "unknown"},
        "tab": {"tab_id": tab, "workspace_id": ws, "number": 1, "label": "i304",
                "focused": False, "pane_count": 1, "agent_status": "unknown"},
        "root_pane": {"pane_id": pane, "terminal_id": "t1", "workspace_id": ws, "tab_id": tab,
                      "focused": False, "agent_status": "unknown", "revision": 1},
    })


def _agent(name="i304", pane="w1:p1", status="idle", kind="agent_info", ready=True):
    return _ok({
        "type": kind,
        "agent": {"name": name, "pane_id": pane, "agent_status": status, "workspace_id": "w1",
                  "tab_id": "w1:t1", "terminal_id": "t1", "focused": False, "revision": 3,
                  "interactive_ready": ready, "agent": "claude"},
    })


def _process_info(pane="w1:p1", shell_pid=4242, procs=(("claude", 4243),)):
    return _ok({
        "type": "pane_process_info",
        "process_info": {"pane_id": pane, "shell_pid": shell_pid, "tty": "/dev/ttys004",
                         "foreground_process_group_id": shell_pid,
                         "foreground_processes": [{"pid": p, "name": n} for n, p in procs]},
    })


_NOT_FOUND = _err("agent_not_found", "no live agent named i304")


class FakeHerdr:
    """Stands in for the herdr CLI *and* for the OS process facts around it.

    ``script`` maps the first two argv words after the binary — ``("agent", "start")`` — to a
    ``(rc, stdout, stderr)`` triple, or to a LIST of them consumed in order (so a test can make
    the same read answer differently before and after a teardown).
    """

    def __init__(self, script=None, alive=(), children=None):
        self.script = {k: list(v) if isinstance(v, list) else v for k, v in (script or {}).items()}
        self.alive = set(alive)
        self.children = dict(children or {})
        self.calls = []                 # every argv, binary included
        self.signals = []               # ("term"|"kill", pid) — kill is BY PID, never by pattern
        self.slept = 0.0

    # -- the herdr edge ------------------------------------------------------------------
    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        key = tuple(argv[1:3])
        spec = self.script.get(key)
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else (spec[0] if spec else None)
        if spec is None:
            return _Completed(1, "", _err("unknown_command", "no script for %s" % (key,)))
        rc, out, err = spec
        return _Completed(rc, out, err)

    # -- the OS edge ---------------------------------------------------------------------
    def pid_alive(self, pid):
        return pid in self.alive

    def child_pids(self, pid):
        if pid not in self.alive:
            return []
        return list(self.children.get(pid, ()))

    def terminate(self, pid):
        self.signals.append(("term", pid))
        return True

    def kill(self, pid):
        self.signals.append(("kill", pid))
        return True

    def sleep(self, seconds):
        self.slept += seconds

    # -- helpers for the tests -----------------------------------------------------------
    def verbs(self):
        return [tuple(c[1:3]) for c in self.calls]

    def die(self, pid):
        self.alive.discard(pid)
        self.children.pop(pid, None)


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeDelivery:
    """The transcript-side oracle. ``answers`` are consumed in order; the last one repeats.

    True/False/None on purpose: the spikes' methodological finding is that a delivery oracle
    checks that a reply EXISTS, never what it says — and an oracle that cannot read the
    transcript answers None, which the wrapper must treat as "not proven", never as "fine".
    """

    def __init__(self, *answers):
        self.answers = list(answers) or [True]
        self.marks = 0
        self.checks = 0

    def mark(self):
        self.marks += 1
        return "mark-%d" % self.marks

    def landed(self, mark):
        self.checks += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _healthy(over=None):
    """A host where everything works: spawn confirms, the agent resolves, the pane has a child."""
    script = {
        ("workspace", "create"): (0, _ws_created(), ""),
        ("agent", "start"): (0, _agent(kind="agent_started"), ""),
        ("agent", "get"): (0, _agent(), ""),
        ("pane", "process-info"): (0, _process_info(), ""),
        ("agent", "prompt"): (0, _agent(kind="agent_prompted", status="done"), ""),
        ("agent", "send-keys"): (0, _ok({"type": "ok"}), ""),
        ("workspace", "close"): (0, _ok({"type": "ok"}), ""),
        # The teardown's verification read. Default: the host no longer has it — i.e. a close that
        # really took. A test that wants a close which did NOT take overrides this with a result.
        ("workspace", "get"): (1, "", _err("workspace_not_found", "no workspace w1")),
    }
    script.update(over or {})
    return FakeHerdr(script=script, alive={4242, 4243}, children={4242: [4243]})


def _host(fake):
    return session_host.SessionHost(probe=fake, binary="/nonexistent/superlooper-test-herdr")


def _session(**over):
    kwargs = dict(name="i304", workspace="w1", tab="w1:t1", pane="w1:p1", shell_pid=4242)
    kwargs.update(over)
    return session_host.Session(**kwargs)


def _preamble(note=""):
    return reorient.render({"id": "i304", "session_id": "abc", "branch": "sl/i304", "note": note})


# --------------------------------------------------------------------- the doorway itself

def test_the_module_exposes_exactly_the_five_verbs():
    # The whole point of a single doorway is that it has a KNOWN width. A sixth public verb is how
    # the interface stops being swappable — the next host would have to implement it too.
    public = {n for n in vars(session_host.SessionHost)
              if not n.startswith("_") and callable(getattr(session_host.SessionHost, n))}
    assert public == {"spawn", "send", "state", "exit", "kill"}


def test_the_wrapper_resolves_its_binary_from_the_env_override(monkeypatch):
    # Same convention as SL_CMUX / SL_GH / SL_CLAUDE: the override is what makes the ratchet in
    # conftest possible at all.
    monkeypatch.setenv("SL_HERDR", "/tmp/stub-herdr")
    assert session_host.SessionHost(probe=_healthy()).binary == "/tmp/stub-herdr"
    monkeypatch.delenv("SL_HERDR")
    assert session_host.SessionHost(probe=_healthy()).binary == "herdr"


def test_conftest_points_the_herdr_override_at_a_binary_that_cannot_exist():
    # The ratchet itself (CLAUDE.md, 2026-07-03 toast-spam incident): a test that forgets to inject
    # a probe must fail loudly rather than drive the owner's real fleet.
    assert not os.path.exists(os.environ["SL_HERDR"])


# --------------------------------------------------------------------- the process facts

class _PsProbe(session_host.Probe):
    """The REAL probe with only its command edge replaced — so the pgrep/ps parsing is the code
    that ships, while no test reaches a real pgrep. `os.kill(<our own pid>, 0)` is not an external
    binary and has no side effect, so the existence half runs for real."""

    def __init__(self, answers):
        self.answers = answers          # ("pgrep"|"ps") -> (rc, stdout)
        self.asked = []

    def run(self, argv, timeout=None):
        self.asked.append(list(argv))
        rc, out = self.answers.get(argv[0], (1, ""))
        return _Completed(rc, out, "")


def test_a_defunct_process_is_not_a_live_one():
    # Found by driving the module end to end: after SIGTERM *and* SIGKILL landed, `kill(pid, 0)`
    # still succeeded — the process was defunct, waiting to be reaped — and the teardown declared
    # itself unverified over a process that was already gone.
    mine = os.getpid()
    assert not _PsProbe({"ps": (0, "%d Z+\n" % mine)}).pid_alive(mine)
    assert _PsProbe({"ps": (0, "%d S+\n" % mine)}).pid_alive(mine)


def test_an_unreadable_process_table_never_turns_a_live_pid_into_a_dead_one():
    # The fail direction matters more than the check: "cannot tell, call it dead" would let exit
    # tear down a live session. ps may only ever DEMOTE what signal 0 already vouched for.
    assert _PsProbe({"ps": (127, "")}).pid_alive(os.getpid())


def test_the_pane_child_read_drops_defunct_children():
    # A pane whose only child is a zombie is a bare shell. Counting it as occupied is exactly how a
    # hollow pane reads as a working session.
    probe = _PsProbe({"pgrep": (0, "501\n502\n"), "ps": (0, "501 Z+\n502 S+\n")})
    assert probe.child_pids(4242) == [502]
    assert probe.asked[0][:2] == ["pgrep", "-P"], "children come from pgrep -P, an OS fact"
    assert _PsProbe({"pgrep": (1, "")}).child_pids(4242) == [], "no match is a real answer"


def test_a_child_read_that_could_not_be_taken_is_not_an_empty_pane():
    # The two answers must never collide: "this pane has no children" ENDS a session, while "the
    # probe could not run" says nothing. Conflating them lets a timed-out pgrep authorise exit to
    # close a working session (fresh-agent review, P0).
    assert _PsProbe({"pgrep": (127, "")}).child_pids(4242) is None, "missing pgrep: no answer"
    assert _PsProbe({"pgrep": (124, "")}).child_pids(4242) is None, "timed-out pgrep: no answer"


# --------------------------------------------------------------------- names, not ids

@pytest.mark.parametrize("bad", ["I304", "304i", "i304!", "i" * 33, "", "i 304", "i304.1", None])
def test_a_name_herdr_would_refuse_is_refused_before_any_rpc(bad):
    # herdr's constraint is `[a-z][a-z0-9_-]{0,31}`. Validating it HERE means a bad name costs a
    # local raise instead of a half-created workspace with no agent in it.
    fake = _healthy()
    with pytest.raises(session_host.NameRefused):
        _host(fake).spawn(name=bad, cwd="/tmp/wt")
    assert fake.calls == [], "nothing may be created for a name the host cannot address"


def test_an_issue_id_becomes_a_legal_name_and_an_impossible_one_is_refused():
    assert session_host.name_for("i304") == "i304"
    assert session_host.name_for("D12") == "d12", "ids are lowercased, not rejected, for case"
    with pytest.raises(session_host.NameRefused):
        session_host.name_for("issue-number-three-hundred-and-four-x")   # 38 chars


def test_every_agent_verb_addresses_the_agent_by_name():
    # The cmux dragged-anchor incident class: a cached pane id stops resolving the moment the owner
    # rearranges his window, because a moved pane gets a NEW workspace-qualified id. Names follow
    # the occupant, so every agent-surface call must carry the name.
    fake = _healthy()
    host = _host(fake)
    session = host.spawn(name="i304", cwd="/tmp/wt")
    host.send(session, "hello", delivery=FakeDelivery(True))
    host.state(session)
    for call in fake.calls:
        if call[1] == "agent" and call[2] != "start":
            assert call[3] == "i304", "agent verbs address by name: %s" % (call,)


# --------------------------------------------------------------------- spawn

def test_spawn_creates_the_workspace_starts_the_agent_and_hands_back_a_handle():
    fake = _healthy()
    session = _host(fake).spawn(name="i304", cwd="/tmp/wt", env={"SL_ISSUE_ID": "i304"},
                                label="superlooper i304")
    create = fake.calls[0]
    assert create[1:3] == ["workspace", "create"]
    assert "--cwd" in create and create[create.index("--cwd") + 1] == "/tmp/wt"
    assert "--env" in create and create[create.index("--env") + 1] == "SL_ISSUE_ID=i304"
    assert "--no-focus" in create, "focus never moves for an unattended launch"
    start = fake.calls[1]
    assert start[1:4] == ["agent", "start", "i304"]
    assert start[start.index("--pane") + 1] == "w1:p1", "the pane comes from the create response"
    assert start[start.index("--kind") + 1] == "claude"
    assert session == session_host.Session(name="i304", workspace="w1", tab="w1:t1",
                                           pane="w1:p1", shell_pid=4242, owned=True)


def test_spawn_passes_native_agent_arguments_after_the_separator():
    fake = _healthy()
    _host(fake).spawn(name="i304", cwd="/tmp/wt", agent_args=["--resume", "abc"])
    start = fake.calls[1]
    assert start[-3:] == ["--", "--resume", "abc"]


def test_spawn_tears_the_workspace_down_when_the_agent_never_starts():
    # c15: verify-or-teardown with transactional rollback. A failed `agent start` that left the
    # workspace behind would leak a pane per attempt and make the next launch's name collide.
    fake = _healthy({("agent", "start"): (1, "", _err("pane_busy", "pane already has an agent"))})
    with pytest.raises(session_host.SpawnRefused) as exc:
        _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert "pane already has an agent" in str(exc.value)
    assert ("workspace", "close") in fake.verbs(), "the workspace we created must be closed"


def test_spawn_refuses_a_hollow_launch_the_owner_watched_herdr_report_as_ready():
    # The §10 phantom, witnessed: `agent list` reported six agents idle/interactive_ready while
    # `pgrep` found ZERO claude processes. herdr saying "started" is not a process existing.
    fake = _healthy()
    fake.children[4242] = []                    # pane shell with nothing running in it
    with pytest.raises(session_host.SpawnRefused) as exc:
        _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert "no live child" in str(exc.value).lower()
    assert ("workspace", "close") in fake.verbs()


def test_spawn_refuses_when_the_process_facts_cannot_be_read_at_all():
    # Absence of signal maps to UNKNOWN, never to idle/done (c2) — and an unconfirmed spawn is a
    # refused spawn, because the alternative is a lane the runner believes is working.
    fake = _healthy({("pane", "process-info"): (0, _process_info(shell_pid=None), "")})
    with pytest.raises(session_host.SpawnRefused):
        _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert ("workspace", "close") in fake.verbs()


def test_spawn_refuses_when_the_name_does_not_resolve_after_start():
    fake = _healthy({("agent", "get"): (1, "", _NOT_FOUND)})
    with pytest.raises(session_host.SpawnRefused):
        _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert ("workspace", "close") in fake.verbs()


def test_spawn_waits_a_moment_before_calling_a_pane_hollow():
    # The confirmation polls rather than reading once: `agent start` returns when herdr detects the
    # agent, which can be a beat before the pane's own child process is visible to pgrep.
    fake = _healthy()
    fake.children[4242] = []
    calls = {"n": 0}

    def child_pids(pid):
        calls["n"] += 1
        return [4243] if calls["n"] > 2 else []

    fake.child_pids = child_pids
    session = _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert session.shell_pid == 4242
    assert fake.slept > 0, "it must actually wait between reads, not spin"
    assert ("workspace", "close") not in fake.verbs()


def test_spawn_never_reads_a_screen_for_its_evidence():
    # The alternate-screen limit: rows that scroll off Claude's alternate screen never enter herdr's
    # scrollback, so a screen read can never be the evidence path. File-based reports stay.
    fake = _healthy()
    _host(fake).spawn(name="i304", cwd="/tmp/wt")
    assert ("agent", "read") not in fake.verbs()
    assert ("pane", "read") not in fake.verbs()


# --------------------------------------------------------------------- send

def test_send_always_carries_wait():
    # docs/SPIKES-2026-07-30-supervised.md §8: WITHOUT --wait, 6/6 prompts typed their text and
    # dropped the submission while returning rc=0. `--wait` is herdr's own prescription too.
    fake = _healthy()
    _host(fake).send(_session(), "do the thing", delivery=FakeDelivery(True))
    prompt = [c for c in fake.calls if c[1:3] == ["agent", "prompt"]][0]
    assert "--wait" in prompt
    assert prompt[prompt.index("--timeout") + 1].isdigit(), "a bounded wait, never an open one"
    assert prompt[3:5] == ["i304", "do the thing"]


def test_a_send_without_a_delivery_oracle_is_impossible():
    fake = _healthy()
    with pytest.raises(TypeError):
        _host(fake).send(_session(), "hi")                       # no oracle at all
    with pytest.raises(ValueError):
        _host(fake).send(_session(), "hi", delivery=None)        # or an empty one
    assert fake.calls == [], "nothing is typed into a pane we cannot verify delivery into"


def test_a_zero_rc_is_not_delivery():
    # The exact 2026-07-30 lie: `agent_prompted`, rc=0, returned the same second, text still sitting
    # in the composer 65s later. rc is not evidence; the transcript is.
    fake = _healthy()
    oracle = FakeDelivery(False)
    with pytest.raises(session_host.DeliveryUnproven) as exc:
        _host(fake).send(_session(), "do the thing", delivery=oracle)
    assert oracle.checks >= 1
    assert "rc is not evidence" in str(exc.value), "the memo must say WHY rc=0 proved nothing"


def test_an_oracle_that_cannot_answer_is_not_a_delivery():
    fake = _healthy()
    with pytest.raises(session_host.DeliveryUnproven):
        _host(fake).send(_session(), "do the thing", delivery=FakeDelivery(None))


def test_a_failed_rc_with_a_landed_transcript_is_a_delivery():
    # The other direction of the same rule. A post-revive `--wait` returns agent_prompt_stalled
    # while the text may still have landed; the transcript decides, in both directions.
    fake = _healthy({("agent", "prompt"):
                       (1, "", _err("agent_prompt_stalled", "no observed state change"))})
    result = _host(fake).send(_session(), "do the thing", delivery=FakeDelivery(True))
    assert result.delivered is True
    assert result.chased is False


def test_an_unproven_ordinary_send_never_types_enter_into_the_pane():
    # The chaser is ruled for post-revive first prompts only. A stray Enter into a pane showing a
    # selection dialog SELECTS an item — the hazard pane_state.py exists for.
    fake = _healthy()
    with pytest.raises(session_host.DeliveryUnproven):
        _host(fake).send(_session(), "do the thing", delivery=FakeDelivery(False))
    assert ("agent", "send-keys") not in fake.verbs()


def test_a_revived_send_chases_enter_when_the_transcript_stays_empty():
    # §9/§10: the first prompt into a REVIVED pane stalled even with --wait, and the
    # `agent send-keys <name> enter` chaser submitted it 5/5 times.
    fake = _healthy()
    oracle = FakeDelivery(False, True)
    result = _host(fake).send(_session(), _preamble(), delivery=oracle, revived=True)
    keys = [c for c in fake.calls if c[1:3] == ["agent", "send-keys"]]
    assert keys and keys[0][3:5] == ["i304", "enter"]
    assert result.delivered is True and result.chased is True


def test_a_revived_send_that_still_does_not_land_after_the_chaser_raises():
    fake = _healthy()
    with pytest.raises(session_host.DeliveryUnproven):
        _host(fake).send(_session(), _preamble(), delivery=FakeDelivery(False), revived=True)


def test_a_revived_send_that_lands_first_time_is_not_chased():
    fake = _healthy()
    result = _host(fake).send(_session(), _preamble(), delivery=FakeDelivery(True), revived=True)
    assert result.chased is False
    assert ("agent", "send-keys") not in fake.verbs()


def test_a_revived_send_must_carry_the_reorientation_preamble():
    # A revived session remembers the conversation, not the world (#298). Handing it a bare
    # instruction lets it act on a stale memory of the branch, the PR and CI.
    fake = _healthy()
    with pytest.raises(session_host.ReorientationMissing):
        _host(fake).send(_session(), "carry on where you left off",
                         delivery=FakeDelivery(True), revived=True)
    assert fake.calls == []


def test_an_instruction_placed_before_the_preamble_is_refused():
    # Containment is not ordering. reorient.render deliberately puts the operator's note LAST, so
    # that the session re-orients BEFORE it acts; a payload that leads with the instruction defeats
    # that whether or not the preamble is somewhere below it (fresh-agent review, P1).
    fake = _healthy()
    with pytest.raises(session_host.ReorientationMissing):
        _host(fake).send(_session(), "rebase onto main first\n\n" + _preamble(),
                         delivery=FakeDelivery(True), revived=True)
    assert fake.calls == []


def test_the_preamble_check_reads_the_real_preamble_not_a_copy_of_its_words():
    # Pinned against reorient.render's own output so the two can never drift apart.
    assert reorient.HEADING in _preamble()
    fake = _healthy()
    result = _host(fake).send(_session(), _preamble(note="rebase first"),
                              delivery=FakeDelivery(True), revived=True)
    assert result.delivered is True


def test_no_code_path_in_the_module_builds_a_prompt_without_wait():
    # Bright-line-by-absence, checked structurally rather than by behaviour: the code for the
    # forbidden thing must not exist, so a future edit cannot add a "just this once" plain prompt.
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    prompts = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if ["agent", "prompt"] == words[:2]:
            prompts += 1
            assert "--wait" in words, "an agent-prompt argv without --wait: %s" % (words,)
    assert prompts == 1, "the prompt argv must be built in exactly ONE place (found %d)" % prompts


def test_the_module_never_builds_a_screen_read():
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        assert words[:2] not in (["agent", "read"], ["pane", "read"]), (
            "screen reads are never the evidence path (alternate-screen limit): %s" % (words,))


# --------------------------------------------------------------------- state

def test_liveness_is_a_process_fact_even_when_herdr_says_the_agent_is_ready():
    # The witnessed phantom, as a unit test: herdr reports idle + interactive_ready, the pane shell
    # has no child at all. The wrapper answers DEAD and files herdr's word under `advisory`.
    fake = _healthy()
    fake.children[4242] = []
    st = _host(fake).state(_session())
    assert st.liveness == session_host.DEAD
    assert st.advisory == "idle"
    assert st.name_resolves is True


def test_herdrs_lifecycle_word_changes_nothing_but_the_advisory_field():
    # "herdr's lifecycle states never gate an action", stated as a property: vary its answer across
    # its whole vocabulary and every other field must be identical.
    seen = set()
    for word in ("idle", "working", "blocked", "done", "unknown"):
        fake = _healthy({("agent", "get"): (0, _agent(status=word), "")})
        st = _host(fake).state(_session(), hooks={"phase": "building"})
        seen.add((st.name, st.liveness, st.name_resolves, json.dumps(st.hooks, sort_keys=True)))
        assert st.advisory == word
    assert len(seen) == 1, "a herdr lifecycle word leaked into a field other than `advisory`"


def test_state_never_asks_agent_list():
    # `agent list` is the surface that lied for four minutes while nothing was running.
    fake = _healthy()
    _host(fake).state(_session())
    assert ("agent", "list") not in fake.verbs()


def test_state_resolves_the_pane_fresh_and_ignores_the_cached_one():
    # The pane in the handle is from spawn time. If the owner moved the pane, that id now names
    # something else — reading process facts from it would report a stranger's process.
    fake = _healthy({("agent", "get"): (0, _agent(pane="w9:p3"), ""),
                       ("pane", "process-info"): (0, _process_info(pane="w9:p3", shell_pid=777), "")})
    fake.alive.add(777)
    fake.children[777] = [778]
    st = _host(fake).state(_session(pane="w1:p1"))
    info = [c for c in fake.calls if c[1:3] == ["pane", "process-info"]][0]
    assert info[info.index("--pane") + 1] == "w9:p3"
    assert st.liveness == session_host.ALIVE
    assert st.pane == "w9:p3"


def test_hooks_are_carried_verbatim_and_their_absence_is_never_filled_by_herdr():
    fake = _healthy()
    hooks = {"phase": "building", "stamped_at": 123}
    assert _host(fake).state(_session(), hooks=hooks).hooks == hooks
    blind = _host(fake).state(_session())
    assert blind.hooks is None, "no hooks read means no primary truth — not herdr's word instead"


def test_a_name_that_stops_resolving_is_reported_and_a_dead_recorded_pid_is_conclusive():
    # Names clear when the agent exits, so a name that no longer resolves is a real signal. It is
    # not by itself proof of death — but a recorded pid that is GONE is.
    fake = _healthy({("agent", "get"): (1, "", _NOT_FOUND)})
    fake.die(4242)
    st = _host(fake).state(_session())
    assert st.name_resolves is False
    assert st.liveness == session_host.DEAD
    assert st.advisory is None


def test_an_unresolvable_name_over_a_live_recorded_pid_is_unknown_not_alive():
    # Pids are recycled by the OS; a pid we recorded minutes ago being alive does not prove it is
    # still OUR process. Fail closed: unknown, and say why.
    fake = _healthy({("agent", "get"): (1, "", _NOT_FOUND)})
    st = _host(fake).state(_session())
    assert st.liveness == session_host.UNKNOWN
    assert "recorded pid" in st.detail.lower()


def test_a_pane_whose_children_cannot_be_read_is_unknown_not_dead():
    # Same P0 as above, seen from the verb that acts on it: DEAD is what authorises a teardown, so
    # an unreadable probe must never produce it.
    fake = _healthy()
    fake.child_pids = lambda pid: None
    st = _host(fake).state(_session())
    assert st.liveness == session_host.UNKNOWN
    with pytest.raises(session_host.TeardownRefused):
        _host(fake).exit(_session())


def test_state_is_unknown_when_the_host_itself_cannot_be_reached():
    # herdr's server being down is not evidence about the worker. rc=124 is the probe's timeout.
    fake = FakeHerdr(script={}, alive={4242}, children={4242: [4243]})
    st = _host(fake).state(_session())
    assert st.liveness == session_host.UNKNOWN
    assert st.advisory is None


# --------------------------------------------------------------------- exit

def test_exit_refuses_to_close_a_session_that_is_still_running():
    # Ordered teardown: `exit` closes a FINISHED session's window. A live one is the owner's to
    # look at (#168), and closing it destroys work in flight.
    fake = _healthy()
    with pytest.raises(session_host.TeardownRefused):
        _host(fake).exit(_session())
    assert ("workspace", "close") not in fake.verbs()


def test_exit_refuses_when_liveness_is_unknown():
    # Positive allowlist, exactly like tidy.closable: only a state we can NAME as finished is
    # closable. Unknown is not finished.
    fake = _healthy({("agent", "get"): (1, "", _NOT_FOUND)})
    with pytest.raises(session_host.TeardownRefused):
        _host(fake).exit(_session())
    assert ("workspace", "close") not in fake.verbs()


def test_exit_closes_the_workspace_of_a_finished_session_and_verifies_it_went():
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)]})
    fake.children[4242] = []                       # the agent has exited; the pane shell remains
    result = _host(fake).exit(_session())
    close = [c for c in fake.calls if c[1:3] == ["workspace", "close"]][0]
    assert close[3] == "w1"
    assert result.closed is True and result.signalled == []


def test_exit_raises_when_the_close_did_not_take():
    # A close that returns rc=0 and changes nothing is a no-op wearing a success code. The rc is
    # not what is read — the workspace still being there is.
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (0, _agent(), "")],
                     ("workspace", "close"): (0, _ok({"type": "ok"}), ""),
                     ("workspace", "get"): (0, _ok({"type": "workspace_info",
                                                    "workspace": {"workspace_id": "w1"}}), "")})
    fake.children[4242] = []
    with pytest.raises(session_host.TeardownUnverified):
        _host(fake).exit(_session())


def test_exit_will_not_call_a_close_verified_while_the_host_is_silent():
    # Silence is not evidence. Reporting `closed` when the host stopped answering is how a
    # workspace leaks while the runner believes it was reaped (fresh-agent review, P0).
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", "connection refused")],
                     ("workspace", "get"): (1, "", "connection refused")})
    fake.children[4242] = []
    with pytest.raises(session_host.TeardownUnverified):
        _host(fake).exit(_session())


def test_the_agent_being_gone_is_not_proof_the_workspace_went():
    # The sharper form of the same P0 (review round 2). exit only ever runs on a session whose agent
    # has ALREADY exited, and the host clears names on exit — so "the name no longer resolves" was
    # true before the close and confirms nothing about it. A close that quietly did nothing would
    # leak one empty workspace per lane, forever, while every teardown reported success.
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)],
                     ("workspace", "close"): (1, "", _err("close_failed", "workspace is busy")),
                     ("workspace", "get"): (0, _ok({"type": "workspace_info",
                                                    "workspace": {"workspace_id": "w1"}}), "")})
    fake.children[4242] = []
    with pytest.raises(session_host.TeardownUnverified) as exc:
        _host(fake).exit(_session())
    assert "workspace is busy" in str(exc.value), "the host's own words belong in the memo"


def test_only_a_not_found_answer_means_the_workspace_went():
    # A "busy" or "refused" reply is the host saying it could not tell us — reading any structured
    # error as gone would be the same false success in a new costume (review verification pass).
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)],
                     ("workspace", "get"): (1, "", _err("workspace_busy", "try again"))})
    fake.children[4242] = []
    with pytest.raises(session_host.TeardownUnverified):
        _host(fake).exit(_session())


def test_a_close_that_errored_but_left_nothing_behind_is_still_a_teardown():
    # The other direction, so the fix above does not trade a false success for a false failure: a
    # `workspace_not_found` refusal means the window was ALREADY gone. Refusing THAT would park a
    # lane over a window the owner had simply closed himself.
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)],
                     ("workspace", "close"): (1, "", _err("workspace_not_found", "no such w1"))})
    fake.children[4242] = []
    assert _host(fake).exit(_session()).closed is True


def test_neither_teardown_verb_touches_a_workspace_we_did_not_create():
    # herdr's own rule, and ours: close only what you created.
    fake = _healthy()
    fake.children[4242] = []
    borrowed = _session(owned=False)
    for verb in ("exit", "kill"):
        with pytest.raises(session_host.TeardownRefused):
            getattr(_host(fake), verb)(borrowed)
    assert ("workspace", "close") not in fake.verbs()


# --------------------------------------------------------------------- kill

def test_kill_closes_the_workspace_and_confirms_the_process_is_gone():
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)]})
    host = _host(fake)
    original_close = fake.run

    def run(argv, timeout=None):
        out = original_close(argv, timeout=timeout)
        if argv[1:3] == ["workspace", "close"]:
            fake.die(4242)                          # a close that really reaps its pane
        return out

    fake.run = run
    result = host.kill(_session())
    assert result.closed is True and result.signalled == []
    assert fake.signals == [], "a close that worked needs no signal"


def test_kill_escalates_to_our_own_recorded_pid_and_never_to_a_pattern():
    # House rule: never kill by name or pattern — the pattern can match the owner's own processes.
    # The escalation ladder walks OUR recorded pid, and only ours.
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)]})
    terminated = {"done": False}

    def terminate(pid):
        fake.signals.append(("term", pid))
        terminated["done"] = True
        fake.die(pid)
        return True

    fake.terminate = terminate
    result = _host(fake).kill(_session())
    assert fake.signals == [("term", 4242)]
    assert result.signalled == ["term"]
    for call in fake.calls:
        assert not any(word in ("pkill", "killall") for word in call)


def test_kill_escalates_all_the_way_to_sigkill_before_giving_up():
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)]})

    def kill(pid):
        fake.signals.append(("kill", pid))
        fake.die(pid)
        return True

    fake.kill = kill
    result = _host(fake).kill(_session())
    assert [s[0] for s in fake.signals] == ["term", "kill"]
    assert result.signalled == ["term", "kill"]


def test_kill_raises_rather_than_report_a_teardown_it_could_not_verify():
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (0, _agent(), "")]})
    with pytest.raises(session_host.TeardownUnverified) as exc:
        _host(fake).kill(_session())
    assert "4242" in str(exc.value), "the memo must name the pid that survived"


def test_kill_never_signals_a_pid_it_could_not_attribute_to_this_session():
    # The module's own pid-reuse warning, obeyed: when the host no longer resolves the name, a
    # recorded pid that is still alive might be ANY process the OS handed that number to. Signalling
    # it would be the pattern-kill hazard with extra steps (fresh-agent review, P0).
    fake = _healthy({("agent", "get"): (1, "", _NOT_FOUND)})
    result = _host(fake).kill(_session())
    assert fake.signals == [], "an unattributable pid is never signalled"
    # ...and it does not veto the teardown either, or a session that legitimately ended could never
    # be verified once the OS reused its pid number. It is REPORTED (review round 2, P1).
    assert result.closed is True and result.orphan_pid == 4242
    assert "not be attributed" in result.detail


def test_kill_is_unverified_when_the_workspace_is_still_there():
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)],
                     ("workspace", "get"): (0, _ok({"type": "workspace_info",
                                                    "workspace": {"workspace_id": "w1"}}), "")})
    fake.terminate = lambda pid: (fake.signals.append(("term", pid)), fake.die(pid))[0]
    with pytest.raises(session_host.TeardownUnverified):
        _host(fake).kill(_session())


def test_kill_verifies_by_name_when_there_is_no_pid_to_watch():
    # The other half of that rule: with no pid in play, the host's own "this name is gone" IS the
    # evidence — so a session whose process facts were never readable can still be ended.
    fake = _healthy({("agent", "get"): [(0, _agent(), ""), (1, "", _NOT_FOUND)],
                     ("pane", "process-info"): (0, _process_info(shell_pid=None), "")})
    result = _host(fake).kill(_session(shell_pid=None))
    assert result.closed is True and result.signalled == []
    assert fake.signals == []


def test_kill_reads_the_live_pane_before_closing_it():
    # After the close there is nothing left to ask, so the pid to verify against must be read while
    # the pane still exists — and read FRESH, because the recorded one may be stale.
    fake = _healthy({("agent", "get"): [(0, _agent(pane="w9:p3"), ""), (1, "", _NOT_FOUND)],
                       ("pane", "process-info"): (0, _process_info(pane="w9:p3", shell_pid=999), "")})
    fake.alive.add(999)
    fake.children[999] = [1000]

    def kill(pid):
        fake.signals.append(("kill", pid))
        fake.die(pid)
        return True

    fake.terminate = lambda pid: (fake.signals.append(("term", pid)), True)[1]
    fake.kill = kill
    _host(fake).kill(_session(shell_pid=4242))
    assert ("term", 999) in fake.signals, "the FRESH pid is the one we signal"
