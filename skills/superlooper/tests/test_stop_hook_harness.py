"""Issue #148 — the Claude worker Stop hook is the runner's in-process embassy, not just a clock.

Three duties, each proven here by driving the REAL bash hook against a REAL git worktree and a
real temp state home (the rig test_hooks.py established):

  1. REPORT HARVEST  — i280 and i328 both wrote their report to a worktree-relative path on the
     same day and the queue stalled two hours on i328. If the canonical reports/<id>.md is absent
     and a report sits under the worker's cwd, the hook moves it.
  2. PROGRESS CLOCK  — state/status/<id>.json each turn end (HEAD, dirty, report/blocked markers),
     so the runner can tell "made progress" from "took a turn" (what lets a probe ladder escape
     i328's infinite loop).
  3. MAILBOX         — state/mail/<id> is consumed, blocks the stop, comes back as the
     continuation reason, and leaves a .consumed.<ts> receipt as delivery proof. Zero keystrokes.

Codex is NOT in scope (its Stop is notify-only, spike verdict) — test_hooks.py pins that its
behavior is unchanged. The Claude-only fence is asserted here too.
"""
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
STOP_HOOK = os.path.join(REPO_ROOT, "skill", "bin", "stop-hook.sh")

ISSUE = "i7"
REPORT_TEXT = "## Tests\nthe suite is green\n\n## Screenshot evidence\nn/a\n\n## Review\nclean\n"


# --------------------------- the rig ---------------------------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=30, check=True).stdout.strip()


def _worktree(path):
    """A real git repo standing in for the worker's worktree. Returns its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30,
                   capture_output=True)
    _git(path, "config", "user.email", "worker@example.invalid")
    _git(path, "config", "user.name", "Worker")
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-qm", "seed")
    return _git(path, "rev-parse", "HEAD")


def _state_home(tmp_path):
    root = tmp_path / "run"
    (root / "state").mkdir(parents=True)
    return root


def _payload(cwd, *, stop_hook_active=False):
    # The documented Claude Stop payload (code.claude.com/docs/en/hooks).
    return {
        "session_id": "sess-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": str(cwd),
        "permission_mode": "bypassPermissions",
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
        "last_assistant_message": "Done.",
    }


def _stop(run_root, cwd, *, stop_hook_active=False, agent="claude", spawn_cwd=None,
          payload=None):
    blob = json.dumps(_payload(cwd, stop_hook_active=stop_hook_active)
                      if payload is None else payload)
    env = {**os.environ, "SL_ISSUE_ID": ISSUE, "SL_RUN_ROOT": str(run_root), "SL_AGENT": agent}
    return subprocess.run(["bash", STOP_HOOK], input=blob, env=env, capture_output=True,
                          text=True, timeout=60, cwd=spawn_cwd)


def _status(run_root):
    return json.loads((run_root / "state" / "status" / f"{ISSUE}.json").read_text())


def _decision(result):
    """The hook speaks to Claude ONLY through JSON on stdout. No JSON == let the stop proceed."""
    out = result.stdout.strip()
    return json.loads(out) if out else None


def _receipts(run_root):
    mail_dir = run_root / "state" / "mail"
    return sorted(p for p in mail_dir.iterdir() if ".consumed." in p.name) if mail_dir.exists() else []


def _put_mail(run_root, text):
    box = run_root / "state" / "mail"
    box.mkdir(parents=True, exist_ok=True)
    (box / ISSUE).write_text(text)
    return box / ISSUE


# --------------------------- duty 1: report harvest ---------------------------

def test_worktree_relative_report_is_harvested_to_the_canonical_path(tmp_path):
    # The i280/i328 mistake exactly: the report written relative to the worktree, so the runner
    # (which reads state_home/reports/<id>.md) never sees it and the queue stalls.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    canonical = run_root / "reports" / f"{ISSUE}.md"
    assert canonical.exists(), "an absent canonical report must be rescued from the worker cwd"
    assert canonical.read_text() == REPORT_TEXT
    assert not stray.exists(), "the harvest MOVES — a leftover would re-harvest forever"


def test_bare_report_at_the_cwd_root_is_harvested(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    (wt / f"{ISSUE}.md").write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert (run_root / "reports" / f"{ISSUE}.md").read_text() == REPORT_TEXT


def test_an_existing_canonical_report_is_never_clobbered(tmp_path):
    # The canonical report is the worker's real deliverable; a stale worktree copy must not win.
    run_root = _state_home(tmp_path)
    (run_root / "reports").mkdir()
    canonical = run_root / "reports" / f"{ISSUE}.md"
    canonical.write_text("## Tests\nthe real report\n")
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("## Tests\na stale draft\n")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert canonical.read_text() == "## Tests\nthe real report\n"
    assert stray.exists(), "with the canonical report present the hook must not touch the worktree"


def test_an_empty_report_is_not_harvested(tmp_path):
    # A touched/half-written file is not a report; harvesting it would fire session_finished on
    # nothing and the gate would read an empty deliverable.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("   \n")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert not (run_root / "reports" / f"{ISSUE}.md").exists()


def test_a_git_tracked_file_is_never_harvested(tmp_path):
    # Safety fence: harvesting MOVES the file. A report the worker committed is repo content —
    # ripping it out of the worktree would leave a deletion in the branch under review.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)
    _git(wt, "add", "reports")
    _git(wt, "commit", "-qm", "tracked report")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert stray.exists(), "a tracked file must stay in the worktree"
    assert not (run_root / "reports" / f"{ISSUE}.md").exists()


# --------------------------- duty 2: the progress clock ---------------------------

def test_status_clock_stamps_head_and_a_clean_tree(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    head = _worktree(wt)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    st = _status(run_root)
    assert st["id"] == ISSUE
    assert st["head"] == head, "HEAD is the progress signal a ladder reads"
    assert st["dirty"] is False
    assert st["report"] is False
    assert st["blocked"] is False
    assert isinstance(st["ts"], int)


def test_status_clock_sees_a_dirty_tree_and_the_markers(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    (wt / "seed.txt").write_text("edited\n")
    (run_root / "reports").mkdir()
    (run_root / "reports" / f"{ISSUE}.md").write_text(REPORT_TEXT)
    (run_root / "state" / "blocked").mkdir()
    (run_root / "state" / "blocked" / ISSUE).write_text("a question")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    st = _status(run_root)
    assert st["dirty"] is True
    assert st["report"] is True
    assert st["blocked"] is True


def test_status_clock_reflects_the_harvest_it_just_did(tmp_path):
    # Ordering pin: harvest runs BEFORE the stamp, so the clock never says report=false about a
    # report the same turn just rescued.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert _status(run_root)["report"] is True


def test_status_clock_still_stamps_when_the_cwd_is_not_a_git_repo(tmp_path):
    # Fail-open: no git facts is not a reason to withhold the clock.
    run_root = _state_home(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    r = _stop(run_root, plain)

    assert r.returncode == 0, r.stderr
    st = _status(run_root)
    assert st["head"] is None
    assert st["dirty"] is None


# --------------------------- duty 3: the mailbox ---------------------------

def test_mail_blocks_the_stop_and_comes_back_as_the_continuation_reason(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    mail = _put_mail(run_root, "CI went red on your PR — look at the failing job.")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    d = _decision(r)
    assert d is not None, "mail must block the stop"
    assert d["decision"] == "block"
    assert d["reason"] == "CI went red on your PR — look at the failing job."
    assert not mail.exists(), "delivered mail must leave the inbox"


def test_delivery_leaves_a_consumption_receipt_carrying_what_was_delivered(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    _put_mail(run_root, "look at the failing job")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    got = _receipts(run_root)
    assert len(got) == 1, "the runner reads the receipt as delivery proof"
    assert got[0].name.startswith(f"{ISSUE}.consumed.")
    assert got[0].read_text() == "look at the failing job"


def test_a_delivered_mailbox_does_not_redeliver_on_the_next_rest(tmp_path):
    # The i328 shape: an unread mailbox that re-injects forever. Consumption is the first guard.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    _put_mail(run_root, "ping")

    first = _stop(run_root, wt)
    second = _stop(run_root, wt)

    assert _decision(first)["decision"] == "block"
    assert _decision(second) is None, "a consumed mailbox must not block a second time"


def test_stop_hook_active_refuses_to_deliver_so_a_block_cannot_spin(tmp_path):
    # The bounded re-injection guard the DoD names. stop_hook_active means we are ALREADY inside a
    # continuation this hook forced; blocking again is how a Stop hook spins forever.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    mail = _put_mail(run_root, "ping")

    r = _stop(run_root, wt, stop_hook_active=True)

    assert r.returncode == 0, r.stderr
    assert _decision(r) is None, "must not block while a stop-hook continuation is already active"
    assert mail.exists(), "undelivered mail stays queued — a receipt here would be a false proof"
    assert _receipts(run_root) == []


def test_stop_hook_active_still_stamps_the_clock_and_harvests(tmp_path):
    # The guard is about BLOCKING only; the file-side duties are idempotent and must still run.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt, stop_hook_active=True)

    assert r.returncode == 0, r.stderr
    assert (run_root / "reports" / f"{ISSUE}.md").exists()
    assert _status(run_root)["report"] is True


def test_an_empty_mailbox_is_silent(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", "no mail == no stdout at all; the stop proceeds untouched"


def test_a_blank_mail_is_consumed_without_blocking(tmp_path):
    # A blank mail carries no instruction. Blocking on it would burn a turn saying nothing — but it
    # must still leave the inbox or it would be retried on every rest.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    mail = _put_mail(run_root, "   \n")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert _decision(r) is None
    assert not mail.exists()


def test_mail_is_bounded_so_a_huge_file_cannot_be_stuffed_into_the_turn(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    _put_mail(run_root, "x" * 200_000)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    d = _decision(r)
    assert d["decision"] == "block"
    assert len(d["reason"]) < 100_000, "an unbounded reason would blow the turn's context"


# --------------------------- fences, liveness, and cwd safety ---------------------------

def test_the_claude_stop_still_stamps_liveness(tmp_path):
    # The hook's original duty. A harness that forgets it would make every healthy worker look frozen.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert (run_root / "state" / "activity" / ISSUE).exists()


def test_codex_gets_no_mailbox(tmp_path):
    # Boundary: Codex's Stop is notify-only (it cannot block), so a "delivery" there would be a lie.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    mail = _put_mail(run_root, "ping")

    r = _stop(run_root, wt, agent="codex")

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""
    assert mail.exists(), "Codex mail must stay queued for the typed-probe path"
    assert _receipts(run_root) == []


def test_non_worker_sessions_are_untouched(tmp_path):
    # The hook is registered globally; the answerer and every ad-hoc session must be a strict no-op.
    wt = tmp_path / "wt"
    _worktree(wt)
    env = {k: v for k, v in os.environ.items() if k not in ("SL_ISSUE_ID", "SL_RUN_ROOT")}
    r = subprocess.run(["bash", STOP_HOOK], input=json.dumps(_payload(wt)), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


@pytest.mark.parametrize("blob", ["", "{", "not json at all", json.dumps([1, 2])])
def test_malformed_hook_input_never_breaks_the_session(tmp_path, blob):
    # Fail closed and SILENT: a non-zero exit or stray stdout here would surface as a hook error in
    # a live worker's TUI on every rest.
    run_root = _state_home(tmp_path)
    env = {**os.environ, "SL_ISSUE_ID": ISSUE, "SL_RUN_ROOT": str(run_root), "SL_AGENT": "claude"}
    r = subprocess.run(["bash", STOP_HOOK], input=blob, env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_the_hook_survives_a_worker_cwd_deleted_underneath_it(tmp_path):
    # Teardown removes worktrees. A live process whose cwd is unlinked is the pruned-cwd shape from
    # the 07-15 forensics — the hook must spawn from a safe cwd and still do its file-side duties.
    run_root = _state_home(tmp_path)
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    env = {**os.environ, "SL_ISSUE_ID": ISSUE, "SL_RUN_ROOT": str(run_root), "SL_AGENT": "claude"}
    script = f'cd "{doomed}" && rmdir "{doomed}" && exec bash "{STOP_HOOK}"'
    r = subprocess.run(["bash", "-c", script], input=json.dumps(_payload(doomed)), env=env,
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    st = _status(run_root)
    assert st["head"] is None, "a vanished worktree has no HEAD — say so rather than crash"
    assert isinstance(st["ts"], int)


def test_mail_still_delivers_when_the_worker_cwd_is_gone(tmp_path):
    # The mailbox lives in the state home, so it must not depend on the worktree existing at all.
    run_root = _state_home(tmp_path)
    doomed = tmp_path / "doomed2"
    doomed.mkdir()
    _put_mail(run_root, "your worktree is gone — stop and report")
    env = {**os.environ, "SL_ISSUE_ID": ISSUE, "SL_RUN_ROOT": str(run_root), "SL_AGENT": "claude"}
    script = f'cd "{doomed}" && rmdir "{doomed}" && exec bash "{STOP_HOOK}"'
    r = subprocess.run(["bash", "-c", script], input=json.dumps(_payload(doomed)), env=env,
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    assert _decision(r)["reason"] == "your worktree is gone — stop and report"
