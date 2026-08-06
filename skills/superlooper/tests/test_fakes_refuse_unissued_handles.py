"""Issue #334 — the acceptance harness must be able to SEE a handle-format mismatch.

This is the more dangerous half of #334 and the reason it insisted on a test of its own. When #308
moved every session spawn onto the five-verb wrapper, `state/panes/<id>` stopped holding cmux
surface UUIDs and started holding the session HOST's pane and workspace. The nudge path went on
handing them to cmux — and `test_simulation.py` stayed green, because the fakes validated nothing:
`fake-cmux read-screen` fell back to a default screen for any surface at all, and `send`/`send-key`
appended to a log without looking. A configuration that could not have worked against real binaries
passed the whole acceptance suite.

A fake that answers anything cannot tell you that you addressed the wrong thing. So both fakes now
refuse an identifier they never issued, and these tests are the ratchet: each one fails if its
refusal is taken out, which is the property the DoD asks for. They drive the fake binaries
DIRECTLY — no engine code in the loop — because what is under test here is the harness's ability to
fail, not the engine's behaviour.
"""
import json
import os
import subprocess

import pytest

FAKES = os.path.join(os.path.dirname(__file__), "fakes")
CMUX = os.path.join(FAKES, "fake-cmux")
HOST = os.path.join(FAKES, "fake-sessionhost")

# A herdr-shaped handle: what `state/panes/<id>` holds after #308, and exactly what the nudge path
# was still handing to cmux. Nothing in a cmux vocabulary ever looks like this.
HOST_SHAPED = "w1:p1"


def _cmux(tmp_path, *args, mode="deliver"):
    env = {**os.environ, "FAKE_CMUX_DIR": str(tmp_path), "CMUX_MODE": mode,
           "SL_LAUNCH_DIR": str(tmp_path / "launch")}
    return subprocess.run([CMUX, *args], env=env, capture_output=True, text=True, timeout=60)


def _host(tmp_path, *args, mode="hollow"):
    env = {**os.environ, "FAKE_HOST_DIR": str(tmp_path), "HOST_MODE": mode}
    return subprocess.run([HOST, *args], env=env, capture_output=True, text=True, timeout=60)


def _issue_surface(tmp_path):
    """Mint one real surface, the way the launcher used to, and return it."""
    r = _cmux(tmp_path, "new-surface", "--type", "terminal", "--pane", "anchor", mode="drop")
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[1]


def _issue_workspace(tmp_path):
    r = _host(tmp_path, "workspace", "create", "--cwd", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)["result"]["workspace"]["workspace_id"]


# ------------------------------------------------------------------ fake-cmux

@pytest.mark.parametrize("verb", ["read-screen", "send", "send-key", "close-surface"])
def test_cmux_refuses_a_surface_it_never_issued(tmp_path, verb):
    """The exact blindness. Each of these used to succeed for ANY surface — which is why a nudge
    aimed at a host pane id looked, to the whole acceptance suite, like a nudge that worked."""
    _issue_surface(tmp_path)                    # so the fake has a registry to refuse against
    r = _cmux(tmp_path, verb, "--surface", HOST_SHAPED, "hello")
    assert r.returncode != 0, f"{verb} accepted a surface it never issued: {r.stdout!r}"
    assert "not_found" in (r.stdout + r.stderr)


def test_cmux_still_serves_a_surface_it_did_issue(tmp_path):
    # The refusal must not be a blanket one — a fake that refuses everything is as blind as a fake
    # that accepts everything, just louder.
    surf = _issue_surface(tmp_path)
    assert _cmux(tmp_path, "read-screen", "--surface", surf, "--lines", "40").returncode == 0
    assert _cmux(tmp_path, "send", "--surface", surf, "hello").returncode == 0


def test_cmux_refuses_a_workspace_it_never_issued(tmp_path):
    # Cross-workspace addressing was the OTHER half of the old send argv, and a wrong workspace is
    # exactly what the 2026-07-09 launch storm was: cmux resolved nothing and said so.
    surf = _issue_surface(tmp_path)
    r = _cmux(tmp_path, "send", "--surface", surf, "--workspace", "w1", "hello")
    assert r.returncode != 0 and "not_found" in (r.stdout + r.stderr)


def test_a_refused_cmux_send_records_nothing(tmp_path):
    """The refusal has to be silent as well as loud: if a rejected send still landed in sends.jsonl,
    every assertion built on that log would keep passing on a send that never happened."""
    _issue_surface(tmp_path)
    _cmux(tmp_path, "send", "--surface", HOST_SHAPED, "hello")
    assert not (tmp_path / "sends.jsonl").exists()


# ------------------------------------------------------------------ fake-sessionhost

def test_the_host_refuses_to_prompt_an_agent_it_never_started(tmp_path):
    r = _host(tmp_path, "agent", "prompt", "i404", "hello", "--wait", "--timeout", "1000")
    assert r.returncode != 0
    assert json.loads(r.stdout)["error"]["code"] == "agent_not_found"
    assert not (tmp_path / "prompts.jsonl").exists()


def test_the_host_refuses_a_prompt_without_wait(tmp_path):
    """Plain `agent prompt` is banned outright (adoption plan §5.1): 6/6 measured prompts typed
    their text, dropped the submission and returned rc=0. The harness refuses it so a regression
    that reintroduced the plain verb dies here rather than in production."""
    ws = _issue_workspace(tmp_path)
    _host(tmp_path, "agent", "start", "i7", "--kind", "claude", "--pane", "%s:p1" % ws)
    r = _host(tmp_path, "agent", "prompt", "i7", "hello")
    assert r.returncode != 0 and json.loads(r.stdout)["error"]["code"] == "bad_request"


def test_the_host_refuses_to_start_an_agent_in_a_pane_it_never_created(tmp_path):
    r = _host(tmp_path, "agent", "start", "i7", "--kind", "claude", "--pane", "nowhere:p1")
    assert r.returncode != 0
    assert json.loads(r.stdout)["error"]["code"] == "pane_not_found"


def test_the_host_refuses_process_info_for_a_pane_it_never_created(tmp_path):
    r = _host(tmp_path, "pane", "process-info", "--pane", "not-a-pane")
    assert r.returncode != 0
    assert json.loads(r.stdout)["error"]["code"] == "pane_not_found"


def test_the_host_refuses_to_close_a_workspace_it_never_created(tmp_path):
    """The one that would let a teardown VERIFY against a foreign id. The wrapper reads
    `workspace_not_found` as positive proof the window went, so a fake that shrugged at an unknown
    workspace would confirm a close that never touched anything."""
    r = _host(tmp_path, "workspace", "close", "w404")
    assert r.returncode != 0
    assert json.loads(r.stdout)["error"]["code"] == "workspace_not_found"
    assert not (tmp_path / "closed.jsonl").exists()


def test_the_host_refuses_to_focus_a_workspace_it_never_created(tmp_path):
    """Issue #339's cross-repo guarantee, checked at the harness end. `focus-session` proves a lane
    id from repo A cannot reach repo B's window; that proof is worth nothing if the stand-in host
    focuses whatever it is handed."""
    r = _host(tmp_path, "workspace", "focus", "w404")
    assert r.returncode != 0
    assert json.loads(r.stdout)["error"]["code"] == "workspace_not_found"
    assert not (tmp_path / "focused.jsonl").exists()


def test_the_host_still_serves_the_ids_it_did_issue(tmp_path):
    ws = _issue_workspace(tmp_path)
    assert _host(tmp_path, "agent", "start", "i7", "--kind", "claude",
                 "--pane", "%s:p1" % ws).returncode == 0
    assert _host(tmp_path, "agent", "prompt", "i7", "hi", "--wait",
                 "--timeout", "1000").returncode == 0
    assert _host(tmp_path, "workspace", "close", ws).returncode == 0


# ------------------------------------------------------------------ the mismatch itself

def test_a_host_handle_handed_to_cmux_fails_the_harness(tmp_path):
    """The end of the blindness, stated as one assertion.

    A recorded handle from the session host, given to cmux, must now FAIL — loudly, with cmux's own
    not-found text, which is what a real binary would have done all along. Before this the same call
    exited 0 and returned a default screen, and the whole simulation went green on a loop that could
    not ring a single worker.
    """
    _issue_surface(tmp_path / "cmux")
    ws = _issue_workspace(tmp_path / "host")
    r = _cmux(tmp_path / "cmux", "send", "--surface", "%s:p1" % ws, "--workspace", ws, "ring")
    assert r.returncode != 0
    assert "not_found" in (r.stdout + r.stderr)
