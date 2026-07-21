"""Issue #194 — the answerer model is RETIRED, and the retirement is enforced by ABSENCE.

#163 replaced the blocked->answerer model (hire a second session to answer a live-frozen
worker) with an exit-clean durable question: the worker writes its blocked file, pushes its WIP
and ends its turn; the runner posts the question as a GitHub comment and RELEASES the lane; the
owner's answer relaunches a fresh session. #163 removed the wiring but deliberately left the
scaffolding inert to keep its blast radius small. #194 removes the scaffolding.

This file is the ratchet, in this repo's own doctrine (CLAUDE.md: "enforced by absence — the
code for the forbidden thing must not exist"). It asserts the SYMBOLS are gone, not the WORD:
the runner and actions still carry comments explaining *why* the answerer model died, and that
history is exactly the kind of hard-won comment the port discipline says to keep. What must not
come back is a state key, a counter, a selector, a config field, a template or a launch mode
that a future edit could re-wire into a second standing session.

Deliberately NOT covered here (owner's scope note on #194): `worker_pretooluse._ask_reason`,
the #156 deny text that still names "a fresh answerer" — that is filed separately as #230.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import actions
import config as config_lib
import loopstate
import tidy

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / "skill"
_LIB, _BIN, _TEMPLATES = _SKILL / "lib", _SKILL / "bin", _SKILL / "templates"


# --------------------------- the retired symbols ---------------------------

# Identifier-level tokens that only ever existed to serve the answerer. Prose that merely says
# "answerer" is fine (it is history); a symbol is a live wire.
_RETIRED_SYMBOLS = (
    "hire_answerer",              # the decide() action + its executor
    "deliver_answer",             # ...and its delivery half
    "closable_answerers",         # the tidy selector for a<N> windows
    "next_answerer",              # the monotonic aid high-water counter
    "answerer_failures",          # the hire ladder's counter
    "answer_delivery_failures",   # ...and the delivery ladder's
    '"answerers"',                # the loopstate active-hire map (as read/written)
    "'answerers'",
    '"answerer"',                 # the config key (renamed to models.debugger — #194)
    "'answerer'",
)


def _payload_files():
    """Every file in the publishable payload — lib, bin and templates."""
    for base in (_LIB, _BIN, _TEMPLATES):
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield p


def test_no_retired_answerer_symbol_survives_in_the_payload():
    offenders = []
    for p in _payload_files():
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue                          # binary/unreadable: carries no Python symbol
        for sym in _RETIRED_SYMBOLS:
            for i, line in enumerate(text.splitlines(), 1):
                if sym in line:
                    offenders.append(f"{p.relative_to(_SKILL)}:{i} {sym} -> {line.strip()[:90]}")
    assert not offenders, (
        "the answerer scaffolding is retired (#194) — these symbols must not exist:\n"
        + "\n".join(offenders))


def test_the_answerer_brief_template_is_gone():
    # Fully orphaned since #163: no code loads it (contrast brief-footer.md, loaded at brief.py:36).
    assert not (_TEMPLATES / "answerer-brief.md").exists()


def test_tidy_has_no_answerer_selector():
    assert not hasattr(tidy, "closable_answerers")
    assert not hasattr(tidy, "_aid_num")


# --------------------------- the retired `blocked` status ---------------------------

def test_blocked_is_not_a_tracked_status():
    """Nothing has set status="blocked" since #163: a worker writes the blocked FILE while its
    status stays `running`, and the runner moves it straight to `awaiting_answer`. Leaving the
    enum member in place invites a future edit to re-introduce a status with no writer."""
    assert "blocked" not in loopstate.VALID
    assert "awaiting_answer" in loopstate.VALID, "#163's live status must stay"


def test_blocked_is_not_an_inflight_status():
    assert "blocked" not in actions.INFLIGHT_STATUSES
    # the statuses that DO hold a lane are unchanged
    assert actions.INFLIGHT_STATUSES == {"running", "frozen", "exited"}


# --------------------------- the config contract ---------------------------

def _write_cfg(repo_path, cfg):
    d = repo_path / ".superlooper"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg))
    return repo_path


def test_models_answerer_is_no_longer_a_config_field(tmp_path):
    """The key is RENAMED, not deleted: the sl-debugger seat (issue #66) is the only remaining
    reader of what used to be `models.answerer`, so it keeps a per-repo pin under its own name.
    A config still carrying the old key fails LOUD at load, naming the allowed keys — the owner
    sees exactly what to rename instead of silently losing the pin."""
    with pytest.raises(ValueError) as e:
        config_lib.load(_write_cfg(tmp_path, {"repo": "o/r", "models": {"answerer": "fable"}}))
    assert "models.answerer" in str(e.value)
    assert "debugger" in str(e.value), "the error must name the key that replaced it"


def test_models_debugger_is_the_replacement_pin(tmp_path):
    cfg = config_lib.load(_write_cfg(tmp_path, {"repo": "o/r", "models": {"debugger": "fable"}}))
    assert cfg["models"]["debugger"] == "fable"
    assert "answerer" not in cfg["models"]


def test_models_debugger_defaults_to_the_strongest_configuration(tmp_path):
    cfg = config_lib.load(_write_cfg(tmp_path, {"repo": "o/r"}))
    assert cfg["models"]["debugger"] == "opus[1m]"


def test_models_debugger_is_validated_like_worker(tmp_path, tmp_path_factory):
    for bad in ("", "   ", 7, None, True):
        d = tmp_path_factory.mktemp("bad")
        with pytest.raises(ValueError):
            config_lib.load(_write_cfg(d, {"repo": "o/r", "models": {"debugger": bad}}))


# --------------------------- the launch mode ---------------------------

_LAUNCH = str(_BIN / "launch-session.sh")


def test_cwd_mode_refuses_an_answerer_id(tmp_path):
    """`--cwd` is SHARED with the watchdog's sl-debugger (d<N>, issue #66), so it survives — but
    narrowed. An a<N> id must now be refused before anything is created, exactly as an i<N> id
    always was: the mode is the debugger's alone."""
    run_root = tmp_path / "run"
    (run_root / "state").mkdir(parents=True)
    (run_root / "answers").mkdir()
    env = {**os.environ, "SL_RUN_ROOT": str(run_root), "SL_PANE": "pane:1",
           "SL_CMUX": "/bin/false"}
    r = subprocess.run([_LAUNCH, "--cwd", str(run_root / "answers"), "a1"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, f"--cwd must reject an answerer id, got rc={r.returncode}"
    assert "d<N>" in r.stderr, f"the refusal must name the surviving mode, got: {r.stderr!r}"
    assert not (run_root / "state" / "panes" / "a1").exists(), "must refuse before creating a tab"


def test_launcher_id_guard_is_debugger_only():
    # Belt-and-braces on the regex itself: the shell guard must not admit `a<N>` any more.
    src = (_BIN / "launch-session.sh").read_text()
    assert not re.search(r"\^\[ad\]", src), "the --cwd id guard must no longer accept a<N>"
    assert re.search(r"\^d\[0-9\]\+\$", src), "the --cwd id guard must pin d<N>"
