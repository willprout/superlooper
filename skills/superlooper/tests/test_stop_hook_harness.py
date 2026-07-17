"""Issue #148 — the Claude worker Stop hook is the runner's in-process embassy, not just a clock.

Three duties, each proven here by driving the REAL bash hook against a REAL git worktree and a
real temp state home (the rig test_hooks.py established):

  1. REPORT HARVEST  — RETIRED from the hook by issue #189; the trigger now lives in the runner
     (lib/actions.py + the runner's executor, proven in test_report_harvest.py). What is pinned
     here is the negative: a Stop must never move a report. See the duty-1 block below.
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


def _mailbox(run_root):
    d = run_root / "state" / "mail"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _receipts(run_root, kind="consumed"):
    d = run_root / "state" / "mail"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if (".%s." % kind) in p.name)


def _put_mail(run_root, text):
    box = run_root / "state" / "mail"
    box.mkdir(parents=True, exist_ok=True)
    (box / ISSUE).write_text(text)
    return box / ISSUE


# ------------- duty 1 RETIRED: the Stop hook never harvests (issue #189) -------------
# The harvest's fences and its rescue live in test_report_harvest.py now; what belongs HERE is the
# proof the hook no longer moves anything. On 2026-07-16 it did, twice: i153 and i163 drafted a
# report at the conventional in-worktree spot MID-session, the very next Stop harvested the draft
# to the canonical path, the runner read that as session_finished, and the gate parked both lanes
# on "finished but no PR exists". A Stop fires at EVERY turn end, and at that instant a draft and a
# finished session's misplaced report are indistinguishable — same shape, same age (the 07-16
# report mtimes matched the harvest moments exactly). The discriminator only arrives LATER, as the
# absence of further turns, which a turn-end hook cannot observe. So the hook stopped guessing.

def test_the_stop_hook_never_promotes_an_in_progress_draft(tmp_path):
    # THE 07-16 REPRO (i153/i163). A worker drafts its report mid-session and keeps building. The
    # turn ends. Nothing may move: the draft stays put and, crucially, the clock still says
    # report=false — that field is what the runner turns into session_finished.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert stray.read_text() == REPORT_TEXT, "the worker's draft is still its own to edit"
    assert not (run_root / "reports" / f"{ISSUE}.md").exists(), \
        "a turn end is not proof of a finished run — the hook must not promote a draft"
    assert _status(run_root)["report"] is False, \
        "report=false is what keeps the runner from firing session_finished on a live lane"


def test_a_bare_draft_at_the_cwd_root_is_left_alone_too(tmp_path):
    # The hook's other old candidate spot. Same rule: a turn end proves nothing about either.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    (wt / f"{ISSUE}.md").write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert (wt / f"{ISSUE}.md").exists()
    assert not (run_root / "reports" / f"{ISSUE}.md").exists()


def test_the_hook_still_stamps_the_clock_beside_an_untouched_draft(tmp_path):
    # Not-harvesting must not become not-running: duties 2 and 3 are unchanged, and the clock is
    # the very signal the runner's harvest trigger reads. A silent hook would be a worse bug.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    head = _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    st = _status(run_root)
    assert st["head"] == head, "the clock still runs — the hook only stopped MOVING things"
    assert st["cwd"] == str(wt), "cwd is how the runner finds the stray report to harvest later"


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


def test_status_clock_reports_the_canonical_report_only(tmp_path):
    # The clock's `report` field means "the runner can SEE a report", so it keys on the canonical
    # path alone. Issue #189 retired the ordering pin that used to live here (harvest-then-stamp):
    # with the hook no longer harvesting, a stray draft must leave this field false — that is what
    # stops session_finished from firing on a live lane.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    (run_root / "reports").mkdir()
    (run_root / "reports" / f"{ISSUE}.md").write_text(REPORT_TEXT)

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


def test_stop_hook_active_still_stamps_the_clock(tmp_path):
    # The guard is about BLOCKING only; the file-side duties are idempotent and must still run.
    # A continuation is even LESS of an ending than a normal rest, so the draft stays put too.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    head = _worktree(wt)
    stray = wt / "reports" / f"{ISSUE}.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(REPORT_TEXT)

    r = _stop(run_root, wt, stop_hook_active=True)

    assert r.returncode == 0, r.stderr
    assert _status(run_root)["head"] == head
    assert stray.exists() and not (run_root / "reports" / f"{ISSUE}.md").exists()


def test_an_empty_mailbox_is_silent(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", "no mail == no stdout at all; the stop proceeds untouched"


def test_a_blank_mail_is_discarded_never_recorded_as_delivered(tmp_path):
    # A blank mail carries no instruction. Blocking on it would burn a turn saying nothing — but it
    # must still leave the inbox or it would be retried on every rest. The receipt has to say what
    # actually happened: a ".consumed" here would be a delivery proof for a delivery that never
    # occurred, and the runner is told to trust that word.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    mail = _put_mail(run_root, "   \n")

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    assert _decision(r) is None
    assert not mail.exists(), "it must leave the inbox or it retries forever"
    assert _receipts(run_root, "consumed") == [], "nothing was delivered — nothing may say it was"
    assert len(_receipts(run_root, "discarded")) == 1


def test_a_consumed_receipt_is_only_written_after_the_block_reaches_claude(tmp_path):
    # THE contract of the receipt: .consumed means "Claude was handed this". If the block JSON
    # cannot be written, the mail must NOT be recorded as delivered — the leftover .claimed is the
    # honest "in flight, never proven" state a runner may retry.
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    _put_mail(run_root, "ping")
    env = {**os.environ, "SL_ISSUE_ID": ISSUE, "SL_RUN_ROOT": str(run_root), "SL_AGENT": "claude"}
    # stdout closed: the hook physically cannot deliver.
    r = subprocess.run(["bash", "-c", 'exec bash "$1" >&-', "_", STOP_HOOK],
                       input=json.dumps(_payload(wt)), env=env, capture_output=True,
                       text=True, timeout=60)

    assert r.returncode == 0, "an undeliverable block must still not break the session"
    assert _receipts(run_root, "consumed") == [], "no delivery happened — no delivery proof"
    assert len(_receipts(run_root, "claimed")) == 1, "the claim stands as in-flight and unproven"


def test_mail_is_bounded_so_a_huge_file_cannot_be_stuffed_into_the_turn(tmp_path):
    run_root = _state_home(tmp_path)
    wt = tmp_path / "worktrees" / ISSUE
    _worktree(wt)
    _put_mail(run_root, "x" * 200_000)

    r = _stop(run_root, wt)

    assert r.returncode == 0, r.stderr
    d = _decision(r)
    assert d["decision"] == "block"
    assert d["reason"].startswith("x" * 1000)
    assert "truncated" in d["reason"], "a truncated instruction must SAY it was truncated"
    # Pinned to the actual bound, not a loose ceiling: 16 KiB of mail + the truncation notice.
    assert len(d["reason"]) < 16384 + 200, "an unbounded reason would blow the turn's context"


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
