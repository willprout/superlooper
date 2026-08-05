"""Issue #334 — the send-safety states that used to need a SCREEN, read from the session's own record.

Under cmux, `bin/nudge-pane.sh` classified a `read-screen` capture and refused to type into three
kinds of pane: a bare shell (#RC-DEADPANE), a session whose auth had died in-window (#151/#174,
i336's 94 minutes of typing at a pane that could not answer) and a session sitting at its OWN
question dialog (#151, i280's false park of a live lane).

The session host exposes no screen read, and the adoption plan is emphatic that it must not become
one: rows that scroll off Claude's alternate screen never enter the host's scrollback, so screen
reads can never be an evidence path (§7.3), and the wrapper deliberately builds no `agent read`
call at all. What the plan blesses instead is the file shape — "the agent writes a file, the
supervisor reads the file".

Claude Code already writes that file. Verified against the real transcripts on this machine
(2026-08-04): every auth-death banner the #174 table was built from is ALSO recorded as an
`isApiErrorMessage` assistant entry carrying the identical string, and an open AskUserQuestion is
an assistant `tool_use` with no `tool_result` answering it. So both states survive the move — off a
render we scraped and onto a record the agent wrote, which cannot scroll away and which the liveness
tier's own process facts already sit beside.

`classify_screen` is untouched and still tested in test_pane_state.py: the pattern table is shared,
so a banner learned for one surface is known to both.
"""
import pane_state

DIALOG = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_01", "name": "AskUserQuestion",
     "input": {"questions": [{"question": "Which way?", "header": "Q"}]}}]}}


def _api_error(text):
    """An entry in the exact shape Claude records a refused turn in (verified on this machine)."""
    return {"type": "assistant", "isApiErrorMessage": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user(text="carry on"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text="on it"):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _answer(tool_use_id="toolu_01"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "the second one"}]}}


# ------------------------------------------------------------------ auth death (#151 / #174)

def test_an_auth_death_banner_in_the_record_refuses_the_send():
    assert pane_state.classify_transcript(
        [_user(), _api_error("Not logged in · Please run /login")]) == "logged_out"


def test_every_banner_the_screen_table_knows_is_known_here_too():
    # The table is shared on purpose: a banner learned for one surface must never be unknown to the
    # other, which is precisely how i336 stayed silent for 94 minutes.
    for banner in ("Not logged in · Please run /login",
                   "Authentication error · Try again",
                   "OAuth token revoked · Please run /login",
                   "Invalid API key · Fix external API key",
                   "Failed to authenticate: OAuth session expired and could not be refreshed. "
                   "Please run /login.",
                   "Please run /login · API Error: 401 Invalid authentication credentials",
                   "Your organization has disabled Claude subscription access for Claude Code · "
                   "Contact your administrator"):
        assert pane_state.classify_transcript([_api_error(banner)]) == "logged_out", banner
        assert pane_state.auth_death_variant(banner), banner


def test_a_recovered_session_is_not_still_logged_out():
    # Self-clearing, and it must be: the owner fixes the key, the session takes a turn, and the
    # lane has to become nudgeable again without anybody restarting anything.
    assert pane_state.classify_transcript(
        [_api_error("Not logged in · Please run /login"), _user(), _assistant()]) == "idle"


def test_a_server_side_api_error_is_not_auth_death():
    # `isApiErrorMessage` covers overload and disconnect too. Those are transient and the lane is
    # perfectly nudgeable; reading them as auth death would alert the owner for a 529.
    for text in ("API Error: 529 Overloaded. This is a server-side issue, usually transient.",
                 "API Error: Connection closed mid-response. The response above may be incomplete."):
        assert pane_state.classify_transcript([_api_error(text)]) == "idle", text


def test_a_usage_limit_is_not_auth_death():
    # The runner has its own usage meter and its own fail-closed hold for this. Calling it auth
    # death would hand the owner the wrong remedy ("/login" for a limit that resets on its own).
    assert pane_state.classify_transcript(
        [_api_error("You've hit your session limit · resets 3:50am (America/Denver)")]) == "idle"


def test_the_banner_must_be_an_api_error_entry_not_merely_text_that_says_so():
    # A worker rendering THIS source file, or quoting the banner in its own reply, must never
    # disable its own lane — the #151 fresh-review P1 fence, restated for the record surface. Here
    # the fence is structural rather than textual: only the agent's own refused-turn entries carry
    # `isApiErrorMessage`, and a session cannot write one by talking.
    quoted = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Not logged in · Please run /login"}]}}
    assert pane_state.classify_transcript([quoted]) == "idle"
    assert pane_state.classify_transcript([_user("Not logged in · Please run /login")]) == "idle"


def test_a_sidechain_api_error_is_a_subagents_problem_not_the_panes():
    # A subagent hitting its own error does not stop the pane from taking a prompt.
    entry = dict(_api_error("Not logged in · Please run /login"), isSidechain=True)
    assert pane_state.classify_transcript([entry]) == "idle"


# ------------------------------------------------------------------ the session's own dialog (i280)

def test_an_unanswered_question_dialog_refuses_the_send():
    assert pane_state.classify_transcript([_user(), DIALOG]) == "at_dialog"


def test_an_answered_question_dialog_does_not():
    assert pane_state.classify_transcript([_user(), DIALOG, _answer()]) == "idle"


def test_another_tools_result_does_not_answer_the_dialog():
    # Matched on the tool_use id, not on "some tool_result came later" — a worker's dialog sits open
    # while nothing else in the session is running, but a stale pairing rule would clear it the
    # moment any unrelated result landed.
    assert pane_state.classify_transcript([DIALOG, _answer("toolu_99")]) == "at_dialog"


def test_an_ordinary_tool_use_is_not_a_dialog():
    other = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_02", "name": "Bash", "input": {"command": "ls"}}]}}
    assert pane_state.classify_transcript([other]) == "idle"


def test_auth_death_outranks_an_open_dialog():
    # Both refuse the send, so the order only decides WHICH refusal the caller is told about — and
    # the auth one is the only one with an owner remedy attached, so it must win.
    assert pane_state.classify_transcript(
        [DIALOG, _api_error("Not logged in · Please run /login")]) == "logged_out"


# ------------------------------------------------------------------ the honest unknowns

def test_no_record_at_all_is_unknown_never_a_verdict():
    # A freshly-spawned session has no transcript until its first turn. Reading that as a refusal
    # would wedge every first nudge; reading it as 'idle' would claim a safety check that never
    # ran. The caller leans on the host's process facts, which is the stronger signal anyway.
    assert pane_state.classify_transcript([]) == "unknown"
    assert pane_state.classify_transcript(None) == "unknown"


def test_the_exited_marker_still_outranks_everything():
    assert pane_state.classify_transcript([_user()], exited_marker=True) == "dead"


def test_a_garbage_record_never_raises():
    assert pane_state.classify_transcript(["nonsense", 7, None, {"type": "user"}]) == "idle"


def test_codex_has_no_transcript_vocabulary_here():
    # The agent boundary: this reads Claude Code's record shape and says so, rather than quietly
    # applying Claude's table to a file Codex never wrote.
    assert pane_state.classify_transcript([_api_error("Not logged in · Please run /login")],
                                          agent="codex") == "unknown"
