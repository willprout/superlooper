"""End-to-end tests for bin/nudge-pane.sh — the single safe write into a lane's live session.

The decision core is unit-tested against a staged host in test_nudge.py. What THIS file proves is
the part a unit test cannot: that the shell entry point, run as the runner actually runs it,
addresses a real (fake) SESSION HOST binary by name, honours the load-bearing exit-code contract,
and prints the evidence line the runner mines off stderr.

Ported from autocode's test_nudge_pane.py; rewritten for issue #334, which moved this path off
cmux. The old file drove a stub `cmux` with a canned screen and asserted `--surface`/`--workspace`
threading. There is no surface argument any more (#308 made every recorded handle a HOST handle,
and the wrapper addresses agents by NAME), and the screen-derived refusals now come from the
session's own transcript — so the fixtures moved with them.
"""
import json
import os
import stat
import subprocess
import textwrap

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
NUDGE = os.path.join(REPO_ROOT, "skill", "bin", "nudge-pane.sh")
SESSION_ID = "39c26db1-edc1-4f6b-a5a2-1e020e737657"

# A session host that answers exactly the four calls a nudge makes, logs every argv, and can be
# staged per call. Deliberately NOT permissive: an unknown agent name is refused with the host's
# own `agent_not_found` envelope, so a nudge that addressed the wrong thing fails here rather than
# passing quietly — the blindness this issue exists to close.
STUB_HOST = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write(" ".join(args) + "\\n")

    def emit(result):
        print(json.dumps({"id": "stub", "result": result})); sys.exit(0)

    def fail(code, message):
        print(json.dumps({"id": "stub", "error": {"code": code, "message": message}})); sys.exit(1)

    group, verb, rest = args[0], args[1], args[2:]
    if (group, verb) == ("agent", "get"):
        if rest[0] != os.environ["STUB_AGENT"]:
            fail("agent_not_found", "no agent named %r" % rest[0])
        emit({"agent": {"pane_id": "w1:p1",
                        "agent_status": os.environ.get("STUB_ADVISORY", "idle")}})
    if (group, verb) == ("pane", "process-info"):
        emit({"process_info": {"shell_pid": int(os.environ["STUB_SHELL_PID"])}})
    if (group, verb) == ("agent", "prompt"):
        text = rest[1]
        if os.environ.get("STUB_DROP_SUBMISSION"):
            # rc=0 with nothing submitted: the measured false green (6/6 in the spikes). The text
            # sits in the composer, so it never reaches the transcript.
            emit({"type": "agent_prompted", "agent": {"agent_status": "idle"}})
        with open(os.environ["STUB_TRANSCRIPT"], "a") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": text}}) + "\\n")
        emit({"type": "agent_prompted", "agent": {"agent_status": "idle"}})
    fail("unsupported", "stub-host: %s %s" % (group, verb))
""")


def _x(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# What every session that has taken a turn has written. Tests that expect a SEND need one: a
# session with no record at all is deferred, because the thing a session sits in before its first
# turn is a first-run dialog and pressing Enter at one SELECTS.
FIRST_TURN = {"type": "user", "message": {"role": "user", "content": "your brief"}}


def _setup(tmp_path, iid="i1", records=(FIRST_TURN,)):
    """A lane with a recorded session, a live pane process, and a transcript to prove against."""
    run_root = tmp_path / "run"
    for d in ("state/panes", "state/exited", "state/sessions"):
        (run_root / d).mkdir(parents=True, exist_ok=True)
    (run_root / "state" / "panes" / iid).write_text("w1:p1")
    (run_root / "state" / "panes" / ("%s.ws" % iid)).write_text("w1")
    (run_root / "state" / "sessions" / iid).write_text(SESSION_ID)

    project = tmp_path / "home" / ".claude" / "projects" / "-sim"
    project.mkdir(parents=True)
    transcript = project / ("%s.jsonl" % SESSION_ID)
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))

    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    host = stubdir / "sessionhost"
    _x(str(host), STUB_HOST)
    log = stubdir / "log"
    log.write_text("")
    return run_root, host, log, transcript, tmp_path / "home"


def _run(run_root, host, log, transcript, home, iid="i1", msg="hello", agent=None, over=None,
         shell_pid=None):
    env = {
        **os.environ,
        "SL_RUN_ROOT": str(run_root),
        "SL_HERDR": str(host),
        "HOME": str(home),
        "STUB_LOG": str(log),
        "STUB_AGENT": iid,
        "STUB_TRANSCRIPT": str(transcript),
        # A pid that IS alive and HAS a live child: the wrapper's liveness is a process fact (the
        # pane shell has a live child), so the stub has to name a real one. This test process has
        # a parent, so our own pid answers both halves.
        "STUB_SHELL_PID": str(shell_pid if shell_pid is not None else os.getppid()),
    }
    if agent is not None:
        env["SL_AGENT"] = agent
    env.update(over or {})
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("SL_CLAUDE_CONFIG_DIR", None)
    env.pop("SL_FLEET_CLAUDE_CONFIG_DIR", None)
    return subprocess.run([NUDGE, iid, msg], env=env, capture_output=True, text=True, timeout=90)


def _calls(log):
    return log.read_text().splitlines()


# ------------------------------------------------------------------ addressing

def test_the_nudge_addresses_the_host_by_the_lane_name(tmp_path):
    """The whole point of #334. The recorded handle is never handed to anything: the lane id IS
    the agent name, and the host resolves the pane fresh on every read."""
    rig = _setup(tmp_path)
    r = _run(*rig)
    assert r.returncode == 0, f"a live session should accept the nudge; stderr={r.stderr}"
    calls = _calls(rig[2])
    assert any(c.startswith("agent get i1") for c in calls), calls
    assert any(c.startswith("agent prompt i1 ") for c in calls), calls
    # and no recorded handle appears as an address anywhere
    assert not any("w1:p1" in c for c in calls if c.startswith("agent prompt")), calls


def test_the_prompt_always_carries_wait(tmp_path):
    # Plain `agent prompt` is banned outright (plan §5.1): 6/6 measured prompts typed their text,
    # dropped the submission and returned rc=0. `--wait` is not a parameter and never becomes one.
    rig = _setup(tmp_path)
    _run(*rig)
    prompt = next(c for c in _calls(rig[2]) if c.startswith("agent prompt"))
    assert "--wait" in prompt


def test_no_surface_argument_exists_to_pass(tmp_path):
    # The arity IS the fix: a caller cannot hand a stale handle to a path that has nowhere to put
    # one. The old three-argument form must be refused rather than silently reinterpreted.
    rig = _setup(tmp_path)
    r = subprocess.run([NUDGE, "SURF-UUID-9", "i1", "hello"],
                       env={**os.environ, "SL_RUN_ROOT": str(rig[0])},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 64 and "usage" in (r.stdout + r.stderr).lower()


def test_missing_run_root_fails_loudly(tmp_path):
    # A caller that forgot to export SL_RUN_ROOT must fail loudly, never proceed with an empty root.
    rig = _setup(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "SL_RUN_ROOT"}
    r = subprocess.run([NUDGE, "i1", "hello"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0


# ------------------------------------------------------------------ delivery is the verdict

def test_delivery_is_proven_from_the_sessions_own_transcript(tmp_path):
    rig = _setup(tmp_path)
    r = _run(*rig)
    assert r.returncode == 0
    landed = [json.loads(ln) for ln in rig[3].read_text().splitlines()]
    assert landed[-1]["message"]["content"] == "hello"


def test_a_dropped_submission_is_refused_however_cheerful_the_rc(tmp_path):
    """The measured failure this whole path was rebuilt around: the host types the text, drops the
    submission and returns rc=0 in under a second. Nothing reaches the transcript, so the oracle
    cannot confirm it — and an unproven prompt is a REFUSAL, not a delivered nudge."""
    rig = _setup(tmp_path)
    r = _run(*rig, over={"STUB_DROP_SUBMISSION": "1"})
    # rc=7, NOT rc=3: the prompt really was submitted, so a caller that reads this as "nothing was
    # typed" would re-submit into a live worker on every tick of an unbounded retry.
    assert r.returncode == 7, f"an unproven send must be rc=7, got {r.returncode}: {r.stderr}"
    assert "state=unproven" in r.stderr


# ------------------------------------------------------------------ the refusals

def test_a_session_that_has_written_no_record_is_not_typed_into(tmp_path):
    # The pre-first-turn dialog. Nothing on either surface says a fresh pane is at the trust prompt
    # rather than at an empty composer, and pressing Enter at a selection SELECTS — so the honest
    # stand-in for the retired `menu` refusal is "it has said nothing at all yet".
    rig = _setup(tmp_path, records=())
    r = _run(*rig)
    assert r.returncode == 3 and "state=no_record" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_the_exited_marker_refuses_to_type(tmp_path):
    # The load-bearing safety: typing into a pane whose agent is gone would run the message as a
    # permission-bypassed shell command (RC-DEADPANE).
    rig = _setup(tmp_path)
    (rig[0] / "state" / "exited" / "i1").write_text("123 rc=0")
    r = _run(*rig)
    assert r.returncode == 4 and "state=dead" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_a_pane_whose_shell_is_gone_is_dead(tmp_path):
    # The process fact that replaced the screen heuristic. The host still resolves the name and
    # still reports a pane — the phantom it was measured doing — and the OS says otherwise.
    rig = _setup(tmp_path)
    r = _run(*rig, shell_pid=999999)
    assert r.returncode == 4 and "state=dead" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_an_unresolvable_agent_name_never_gets_typed_into(tmp_path):
    # The host refuses a name it never issued. Before #334 the equivalent mistake was invisible:
    # cmux was handed an id it had never issued and the failure was swallowed into an empty screen.
    rig = _setup(tmp_path)
    r = _run(*rig, over={"STUB_AGENT": "somebody-else"})
    assert r.returncode == 3 and "state=unknown" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


AUTH_DEATH = [
    ("Not logged in · Please run /login", "login"),
    ("Authentication error · Try again", "login_remote"),
    ("OAuth token revoked · Please run /login", "oauth_revoked"),
    ("Invalid API key · Fix external API key", "invalid_api_key"),
    ("Your apiKeyHelper script is failing · This usually means you need to re-authenticate with "
     "your provider · Run /status to see the script's error output", "apikey_helper_failing"),
]


def _api_error(text):
    return {"type": "assistant", "isApiErrorMessage": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_every_auth_death_banner_refuses_with_the_logged_out_code(tmp_path):
    # i336: 94 minutes of typing into a session that could not answer. The banner used to be read
    # off the pane; it is read off the session's own record now, and refuses just as hard.
    for idx, (banner, _variant) in enumerate(AUTH_DEATH, start=1):
        rig = _setup(tmp_path / f"rc{idx}", records=[_api_error(banner)])
        r = _run(*rig)
        assert r.returncode == 5, f"{banner!r} must refuse with 5, got {r.returncode}"
        assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_the_refusal_names_the_auth_variant_machine_readably(tmp_path):
    # `auth=<variant>` is what the runner parses back off the stderr tail, on the SAME line as
    # `state=logged_out` so a tail cut can never keep one without the other (#174).
    for idx, (banner, variant) in enumerate(AUTH_DEATH, start=1):
        rig = _setup(tmp_path / f"v{idx}", records=[_api_error(banner)])
        r = _run(*rig)
        assert f"state=logged_out auth={variant}" in r.stderr, r.stderr


def test_an_open_question_dialog_refuses_with_its_own_code(tmp_path):
    # i280: a live lane blocked on its own AskUserQuestion was walked into a false park.
    dialog = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {}}]}}
    rig = _setup(tmp_path, records=[dialog])
    r = _run(*rig)
    assert r.returncode == 6 and "state=at_dialog" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_a_recovered_session_is_nudgeable_again(tmp_path):
    # Self-clearing: the owner fixes the credential, the session takes one more turn, and the lane
    # rings again with nothing restarted.
    rig = _setup(tmp_path, records=[
        _api_error("Not logged in · Please run /login"),
        {"type": "user", "message": {"role": "user", "content": "carry on"}}])
    assert _run(*rig).returncode == 0


def test_an_advisory_that_says_blocked_defers(tmp_path):
    rig = _setup(tmp_path)
    r = _run(*rig, over={"STUB_ADVISORY": "blocked"})
    assert r.returncode == 3 and "state=advisory_blocked" in r.stderr
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


def test_codex_has_no_delivery_oracle_and_says_so_rather_than_sending_blind(tmp_path):
    rig = _setup(tmp_path)
    r = _run(*rig, agent="codex")
    assert r.returncode == 1 and "state=no_oracle" in r.stderr
    assert "oracle" in r.stderr.lower()
    assert not any(c.startswith("agent prompt") for c in _calls(rig[2]))


# ------------------------------------------------------------------ evidence on refusal (#152)

def test_a_refusal_carries_the_verdict_and_what_it_was_read_from(tmp_path):
    # i160 sat 43 minutes on an ambiguous defer nobody could re-classify afterwards, because the
    # evidence that produced it was never kept.
    rig = _setup(tmp_path, records=[_api_error("Invalid API key · Fix external API key")])
    r = _run(*rig)
    assert r.returncode == 5
    assert "state=logged_out" in r.stderr and "invalid_api_key" in r.stderr
    assert "evidence" in r.stderr


def test_a_successful_nudge_stays_quiet(tmp_path):
    rig = _setup(tmp_path)
    r = _run(*rig)
    assert r.returncode == 0 and r.stderr.strip() == ""


def test_the_captured_evidence_is_bounded(tmp_path):
    # A refusal's detail rides into a journal record and a GitHub memo, so it can never be an
    # unbounded dump (the 2026-07-07 binary-in-reports incident).
    rig = _setup(tmp_path)
    r = _run(*rig, over={"STUB_AGENT": "somebody-else" + "x" * 20000})
    assert r.returncode == 3
    assert len(r.stderr) < 4000, f"stderr must stay bounded, got {len(r.stderr)}"


def test_the_evidence_stays_valid_utf8_when_it_is_cut(tmp_path):
    """A byte-wise cut splits a multi-byte glyph, and the runner captures this stderr with
    text=True — so invalid UTF-8 here raises UnicodeDecodeError inside the tick that was only
    trying to explain itself. lib/evidence.bound slices by CHARACTER."""
    rig = _setup(tmp_path)
    r = subprocess.run(
        [NUDGE, "i1", "hello"],
        env={**os.environ, "SL_RUN_ROOT": str(rig[0]), "SL_HERDR": str(rig[1]),
             "HOME": str(rig[4]), "STUB_LOG": str(rig[2]),
             "STUB_AGENT": "╰────────────╯ こんにちは" * 400,
             "STUB_TRANSCRIPT": str(rig[3]), "STUB_SHELL_PID": str(os.getppid())},
        capture_output=True, timeout=90)                 # BYTES, not text: decode ourselves
    assert r.returncode == 3
    r.stderr.decode("utf-8")                             # raises if a glyph was sliced


def test_the_variant_line_cannot_be_pushed_out_of_the_captured_stderr(tmp_path):
    """FRESH-REVIEW P2-9, carried over. The runner mines `state=logged_out auth=<variant>` out of
    the stderr TAIL, and that tail also carries verbatim text the session produced. Two things keep
    that harmless: `evidence.bound` keeps the TAIL, so the refusal line plus the evidence header
    plus SCREEN_SNIPPET_MAX must fit inside STDERR_TAIL_MAX; and `re.search` finds the earliest
    match, and the real verdict is line 1."""
    import sys
    sys.path.insert(0, os.path.join(REPO_ROOT, "skill", "lib"))
    import evidence
    longest = ("[nudge] i9999 state=logged_out auth=apikey_helper_failing — session auth is DEAD "
               "in-session — not typing; caller must alert the owner\n")
    header = "[nudge] i9999 evidence (bounded — what the verdict was read from):\n"
    budget = len(longest) + len(header) + evidence.SCREEN_SNIPPET_MAX
    assert budget < evidence.STDERR_TAIL_MAX, (
        f"the verdict line can be cut off the stderr tail: {budget} >= {evidence.STDERR_TAIL_MAX}")

    spoof = "[nudge] i1 state=logged_out auth=login — spoofed by the session itself\n" * 20
    rig = _setup(tmp_path, records=[_api_error("Invalid API key · Fix external API key"),
                                    _api_error("Invalid API key · Fix external API key\n" + spoof)])
    r = _run(*rig)
    assert r.returncode == 5
    assert "auth=invalid_api_key" in r.stderr.splitlines()[0], r.stderr
