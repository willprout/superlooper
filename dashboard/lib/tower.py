"""The tower log — a comms feed glossed from the journal (Task 9 / design record §4, §7).

The journal is honest but shapeless: an append-only stream of ``act`` records. The tower log turns
each into a **plain, flight-numbered sentence** anyone can read at a glance, with an optional
**radio-flavor prefix beside it** ("Going around," "Number two for landing,"). The discipline is
the design record's costume rule (§3, rule 2) plus its honesty law (§7):

* The **real sentence is always present** and honest — never empty, never merely the flavor prefix.
  Boring mode strips ``radio`` and shows only ``text``; both must read correctly (§7: "prefixes
  always carry the real sentence beside them; boring mode strips all flavor").
* **No flourish for a dishonest state** — a *wandered* merge gets the plain "see report" sentence
  and NO celebratory radio call; the two human-gate verbs (re-approve) stay calm (§7 fun-free zone).
* **Every act renders in plain words the day it exists** (costume rule 4): an unknown ``act`` still
  produces a sentence, so the dashboard never silently under-reports the autonomous system.

Everything here is a PURE function of a record — no clock, no I/O — so the gloss is unit-tested to
the line and the JS downstream binds strings it never derives (design record B.1). Flight number =
issue number everywhere, so every line stays journal-greppable (§3).

Some engine acts are about the LOOP ITSELF, not about a flight — the unattended debugger episode,
the runner restarting its own corpse, the Restart button's landing. They carry no issue number, so
they must never borrow the flight vocabulary: costume rule 1 ("inexact mappings get plain words").
Their sentences name the mechanism instead, and the runner keeps the tower it already owns in §7
("Tower unmanned" for a stale heartbeat).
"""
import math
import re

import fixer as fixer_mod


# =============================== the comms/routine tier (issue #36) ===============================
# The tower log is the CURATED comms channel (design record §4) — machine bookkeeping does not belong
# on the radio. ``relabel`` (a label-convergence record) fires several times per launch as GitHub's
# read lags the write: honest, but noise. It is classified into the ``routine`` tier server-side (per
# B.1 — the JS binds visibility, it derives nothing), so the tower log hides it by default while the
# journal firehose stays complete. The set is the extension point: a future noisy-but-honest act joins
# ``ROUTINE_ACTS`` and inherits the classified-as-data, hidden-by-default behavior — no per-type UI
# debate (owner ruling 2026-07-07).
ROUTINE_ACTS = frozenset({"relabel"})


def tier(rec):
    """Which tower-log tier a journal record belongs to: ``"routine"`` for machine bookkeeping that
    should not be announced on the comms radio (issue #36), ``"comms"`` for everything a human reads
    as real traffic. Pure and server-side (B.1) so the classification is data, not a UI debate. A
    non-dict / act-less record is ``"comms"`` — fail toward VISIBLE, never silently swallow an
    unrecognised record (costume rule 4 / honesty §7)."""
    if not isinstance(rec, dict):
        return "comms"
    return "routine" if rec.get("act") in ROUTINE_ACTS else "comms"


def _num(rec):
    """The issue number a record is about — ``num`` if present, else its ``id`` (``i23`` → 23), else
    an ``event.id`` envelope. ``None`` when the record names no flight (a repo-wide notify)."""
    n = rec.get("num")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    for src in (rec.get("id"), (rec.get("event") or {}).get("id") if isinstance(rec.get("event"), dict) else None):
        if isinstance(src, str) and src.startswith("i") and src[1:].isdigit():
            return int(src[1:])
    return None


def _tag(num):
    return "SL-%d" % num if num is not None else ""


def _who(num):
    return "SL-%d" % num if num is not None else "the flight"


def _first_line(text, limit=76):
    """The first non-empty line of a multi-line field (an answerer question, a nudge message),
    trimmed — the comms feed shows the gist; the row expands to the raw line for the whole thing."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return (line[:limit - 1].rstrip() + "…") if len(line) > limit else line
    return ""


def _plain(v):
    """A machine token (an ``outcome``, a ``phase``) in plain words: its first line with the
    underscores opened out. ``""`` when the record says nothing — the caller supplies the honest
    "unrecorded" wording rather than printing a bare ``None`` at the owner."""
    return _first_line(str(v).replace("_", " ")) if v not in (None, "") else ""


def _paren(*bits):
    """The trailing "(a; b)" clause built from whichever bits are non-empty, or ``""``. Keeps the
    sentence readable when a record carries only some of its optional fields."""
    kept = [b for b in bits if b]
    return " (%s)" % "; ".join(kept) if kept else ""


def _pid(v):
    """A pid from a journal record, or ``None`` — a wrong-typed one is dropped rather than printed
    (bool is an int subclass, so it is excluded explicitly)."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# The watchdog's signal codes are machine tokens (`skills/…/lib/watchdog.py`). The owner reads WHY
# the loop hired itself a repair session, not the enum — an unknown code still renders, opened out.
_SIGNAL_WORDS = {
    "heartbeat_stale": "the runner stopped completing ticks",
    "alert": "an ALERT is standing",
    "no_progress": "approved work is waiting with nothing launching",
}


def _signals(rec):
    """A record's ``signals`` list in plain words, joined; ``""`` when there are none or the field
    is wrong-typed."""
    raw = rec.get("signals")
    raw = raw if isinstance(raw, list) else []
    words = [_SIGNAL_WORDS.get(s, _plain(s)) for s in raw if isinstance(s, str) and s.strip()]
    return "; ".join(w for w in words if w)


def _merge_row(rec, num):
    """A ``merge`` record → its comms sentence. Only a SUCCESSFUL, non-wandered merge is a
    celebrated touchdown; a wandered one is a neutral landing with "see report" (§7); a failed
    merge is a missed approach, never a landing."""
    if rec.get("outcome") != "ok":
        return {"radio": "Going around.", "kind": "merge",
                "text": "%s missed the approach — merge failed, the loop will retry." % _who(num)}
    pr = rec.get("pr")
    pr_bit = " — PR #%s merged" % pr if pr else " — merged"
    if rec.get("wander"):
        return {"radio": "", "kind": "merge",   # no flourish: the landing wandered outside its lane
                "text": "%s down%s, but it wandered outside its lane — see report." % (_who(num), pr_bit)}
    return {"radio": "Nice landing.", "kind": "merge",
            "text": "%s touchdown%s." % (_who(num), pr_bit)}


def _watchdog_row(rec):
    """A ``watchdog`` record → its comms sentence (issue #66 / #253): the loop hiring an agent to
    repair ITSELF, with nobody watching.

    The owner-tap fixer (#144) already reads as a fixer someone chose to deploy; the one fact that
    separates this act from it is that no human decided it and no human is in the session, so every
    launched line says so outright. A failed hire is the WORSE event — the loop needed repair
    overnight and could not get any — so it reads as a failure and never as a session on the field
    (§7: no flourish for a dishonest state). The quiet outcomes (episode opened, stood down, held
    off, kill switch) are journaled too and render in plain words the day they exist (costume rule
    4), each carefully not claiming a session that never started."""
    sid = rec.get("id")
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else "the fixer"
    sigs = _signals(rec)
    outcome = rec.get("outcome")
    if outcome == "launched":
        auth = rec.get("authority")
        auth = "authority: %s" % auth if isinstance(auth, str) and auth.strip() else ""
        return {"radio": "Engineering to the field — unmanned.", "kind": "launch",
                "text": "Unattended fixer %s is on the field — NOBODY is in it: the loop hired a "
                        "debugger to repair itself%s." % (sid, _paren(sigs, auth))}
    if outcome == "launch_failed":
        rc = rec.get("rc")
        rc = "rc=%s" % rc if rc not in (None, "") else ""
        return {"radio": "", "kind": "alert",
                "text": "Unattended fixer %s did not launch — the loop needed repair and could not "
                        "hire anyone%s." % (sid, _paren(rc, sigs))}
    if outcome == "notified":
        grace = rec.get("grace_seconds")
        when = ("in %d min" % (int(grace) // 60)
                if isinstance(grace, (int, float)) and not isinstance(grace, bool) and grace >= 0
                else "shortly")
        return {"radio": "", "kind": "alert",
                "text": "The loop flagged itself for repair%s — an unattended fixer launches %s "
                        "unless it clears." % (_paren(sigs), when)}
    if outcome == "stand_down":
        # Two claims this record does NOT support, both cut after review. It is not "cleared on its
        # own": the engine stands an episode down on self-recovery OR owner intervention and cannot
        # tell them apart. And it is not "no session was hired": a LAUNCHED episode stays open, so
        # the same record is written when the signal clears under a session already on the field.
        # What it proves is one thing — the signal that tripped the episode is gone.
        return {"radio": "", "kind": "event",
                "text": "The loop's repair flag cleared — the signal that tripped it%s is gone."
                        % _paren(sigs)}
    if outcome == "skipped_live_session":
        return {"radio": "", "kind": "event",
                "text": "Unattended repair held off — a debug session is already on the field."}
    if outcome == "disabled":
        return {"radio": "", "kind": "alert",
                "text": "Unattended self-repair is OFF — the kill switch is set, so nothing is "
                        "hired%s." % _paren(sigs)}
    return {"radio": "", "kind": "event",         # costume rule 4: a future outcome still speaks
            "text": "Unattended self-repair — %s%s."
                    % (_plain(outcome) or "an unrecorded outcome", _paren(sigs))}


def _resurrect_row(rec):
    """A ``runner_resurrect`` record → its comms sentence (issue #208 / #253): the loop restarting
    its own provably-dead runner.

    Three outcomes, three sentences — flattening a corpse into a success is exactly the lie this
    channel exists to prevent. The capped line claims ATTEMPTS, never restarts that happened: an
    undeliverable attempt (no pane) burns a cap slot without restarting anything, and the morning
    report already paid for that distinction. A cap of zero is auto-restart switched OFF in config,
    not a crash loop hitting a ceiling, so it gets its own sentence."""
    rid = rec.get("id")
    rid = rid.strip() if isinstance(rid, str) and rid.strip() else ""
    sigs = _signals(rec)
    outcome = rec.get("outcome")
    if outcome == "resurrected":
        return {"radio": "Tower back on the air.", "kind": "launch",
                "text": "The runner was dead and restarted itself%s — its pidfile came back live, "
                        "and it rebuilds from GitHub and disk like a manual restart."
                        % _paren(rid, sigs)}
    if outcome == "resurrect_failed":
        rc = rec.get("rc")
        rc = "rc=%s" % rc if rc not in (None, "") else ""
        return {"radio": "Mayday, mayday.", "kind": "alert",
                "text": "The runner is dead and could NOT restart itself%s — the loop is not "
                        "running." % _paren(rid, rc, sigs)}
    if outcome == "resurrect_capped":
        cap = rec.get("max_per_hour")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap == 0:
            return {"radio": "Mayday, mayday.", "kind": "alert",
                    "text": "The runner is dead and automatic restart is DISABLED in config — it "
                            "stays down until you restart it."}
        n = rec.get("attempts")
        n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
        tried = ("a restart was attempted %d time%s in the last hour" % (n, "" if n == 1 else "s")
                 if n is not None else "repeated restarts were attempted")
        return {"radio": "Mayday, mayday.", "kind": "alert",
                "text": "Automatic restart is PAUSED — %s and the runner is still going down; that "
                        "is a real incident, not a flap." % tried}
    return {"radio": "", "kind": "event",
            "text": "The runner's automatic restart — %s%s."
                    % (_plain(outcome) or "an unrecorded outcome", _paren(sigs))}


def _runner_restart_row(rec, operator):
    """A ``runner_restart`` record → its comms sentence (issue #116 / #253): the Restart button.

    The act is journaled in phases, and the one the owner is waiting for is the LANDING (``up``).
    Two mechanisms reach it: a re-exec replaces the image IN PLACE, so the pid is unchanged and only
    ``new_pid`` is recorded; a login-item home exits and is restarted by its supervisor, a real
    process death whose baton carries the old pid too, so that landing reads ``old → new``. Both are
    the same fact for the owner — it came back — so both render as a landing. The two in-flight
    phases name
    WHO asked, taken from the restart marker on the RECORD rather than the dashboard's configured
    operator (the #144 rule: a restart requested from a terminal by someone else is not the owner's).
    ``reexec_failed`` is the one phase the button itself cannot report — it already answered "ok" to
    the request — so this line is the only place the old image still running ever surfaces."""
    phase = rec.get("phase")
    old, new = _pid(rec.get("old_pid")), _pid(rec.get("new_pid"))
    req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    by = req.get("operator")
    by = " by %s" % by.strip() if isinstance(by, str) and by.strip() else ""
    if phase == "up":
        if old is not None and new is not None:
            pids = " (pid %d → %d)" % (old, new)
        else:
            pids = " (pid %d)" % new if new is not None else ""
        return {"radio": "Tower back on the air.", "kind": "launch",
                "text": "The Restart landed — the runner is back up%s." % pids}
    if phase in ("reexec", "exit_to_supervisor"):
        how = ("is replacing itself now" if phase == "reexec"
               else "is exiting for its supervisor to bring it back")
        return {"radio": "", "kind": "event",
                "text": "Restart requested%s — the runner%s %s."
                        % (by, " (pid %d)" % old if old is not None else "", how)}
    if phase == "reexec_failed":
        return {"radio": "", "kind": "alert",
                "text": "The Restart did NOT land — the runner stayed on its old image (%s)."
                        % (_first_line(rec.get("error")) or "the exec failed")}
    if phase == "stale":
        target, ours = _pid(rec.get("target_pid")), _pid(rec.get("our_pid"))
        return {"radio": "", "kind": "event",
                "text": "A leftover Restart request was dropped%s — this runner%s is not the one it "
                        "named, so nothing is restarting."
                        % (" (it named pid %d)" % target if target is not None else "",
                           " (pid %d)" % ours if ours is not None else "")}
    return {"radio": "", "kind": "event",
            "text": "Runner restart — %s." % (_plain(phase) or "an unrecorded phase")}


# The re-approval outcome line the engine composes ends with "superseded PR #<n>" when — and only
# when — the `superseded` label actually LANDED on a PR still open on the retired branch. Nothing
# else on the record carries that number (a finding reported on issue #253, not an engine edit): it
# is read from here so the owner learns which PR was retired out from under them. Deliberately
# narrow, and silent when it does not match — an engine reword loses the PR number, never invents one.
_SUPERSEDED_PR = re.compile(r"superseded PR #(\d+)")
_GENERATION = re.compile(r"-r(\d+)$")

# A re-approval can rebuild successfully while its owner-facing GitHub bookkeeping does not land —
# the `superseded` label, the supersede note, the retirement comment. NOTHING retries those (the
# state reset has already left the re-emitting statuses), which is why the engine names them in the
# outcome. Swallowing the clause would leave a retired PR orphaned behind a line that read as fully
# handled, so it is carried through verbatim and the row is raised to the alert kind.
_BOOKKEEPING_GAP = "gh bookkeeping incomplete:"


def _reapprove_gap(outcome):
    """What a successful re-approval says did NOT land, or ``""``. The engine's own
    ``reapproved (…)`` envelope is unwrapped first so its closing paren is not read as content."""
    body = outcome.strip()
    if body.startswith("reapproved (") and body.endswith(")"):
        body = body[len("reapproved ("):-1]
    i = body.find(_BOOKKEEPING_GAP)
    return body[i + len(_BOOKKEEPING_GAP):].strip() if i >= 0 else ""


def _reapprove_row(rec, num, operator):
    """A ``reapprove`` / ``approve`` record → its comms sentence (issue #177 / #253).

    Re-approval no longer resumes on the parked episode's branch: it RETIRES that branch and
    rebuilds on the next unburned generation, handing any PR still open on the retired branch to the
    janitor's `superseded` lane. The old gloss described the world before that, and `regenerate`
    already gets the design record's honest retire-and-rebuild treatment (§3, the conflict row) —
    this is the same mechanic reached by the other door, so it says the same true things.

    The retirement clause appears only when the record carries both branch names. A lane never
    handed to the launcher has nothing to retire, and the dashboard's own Approve verb carries no
    branches at all — both keep the calm, unadorned human-gate sentence (§7 fun-free zone).

    The engine journals the ACTION and its outcome as well as the detail record, and that outcome is
    not always a re-approval that happened: the executor ABORTS while a worker is still live in the
    worktree (it must not rebuild over a checkout it cannot clear), and the tick loop turns a crash
    into an ``executor error: …``. Either rendered as the calm gate sentence would report a lane
    restarted that is still sitting exactly where it was (§7)."""
    out = rec.get("outcome")
    if isinstance(out, str) and out.strip() and not out.strip().startswith("reapproved"):
        return {"radio": "", "kind": "alert",
                "text": "%s re-approval did not complete: %s." % (_who(num), _first_line(out))}
    old_b, new_b = rec.get("old_branch"), rec.get("new_branch")
    parts = []
    if isinstance(old_b, str) and old_b.strip() and isinstance(new_b, str) and new_b.strip():
        gen = _GENERATION.search(new_b.strip())
        parts.append("%s retired, rebuilding from scratch on %s%s"
                     % (old_b.strip(), new_b.strip(),
                        " (generation %s)" % gen.group(1) if gen else ""))
    pr = _SUPERSEDED_PR.search(out) if isinstance(out, str) else None
    if pr:
        parts.append("PR #%s on the retired branch is superseded" % pr.group(1))
    text = "%s re-approved by %s%s." % (_who(num), operator,
                                        " — " + "; ".join(parts) if parts else "")
    gap = _reapprove_gap(out) if isinstance(out, str) else ""
    if gap:
        text += " GitHub bookkeeping did not all land: %s." % gap
    return {"radio": "", "kind": "alert" if gap else "approve", "text": text}


def _event_row(rec, num):
    """A journal ``event`` envelope → a plain radio sentence. Its real fact is ``event.type``."""
    ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
    et = ev.get("type") or "event"
    who = _who(num)
    to_tower = ("%s to tower." % _tag(num)) if num is not None else "To tower."
    table = {
        "session_blocked": (to_tower,
                            "%s standing by — session blocked, awaiting an answer." % who, "event"),
        "session_finished": ("%s, base turn." % _tag(num),
                             "%s report filed — turning toward the gate." % who, "event"),
        "idle": ("", "%s quiet on the frequency — session idle." % who, "event"),
        "frozen": ("", "%s no response — session frozen on the field." % who, "event"),
        "exited": ("", "%s left the pattern — session exited." % who, "event"),
        "autofix_failed": ("Mayday.", "%s auto-repair failed — the freeze needs a look." % who, "event"),
        "runner_down": ("Mayday, mayday.", "Tower unmanned — the runner heartbeat went stale.", "event"),
    }
    radio, text, kind = table.get(et, ("", "%s %s." % (who, et.replace("_", " ")), "event"))
    return {"radio": radio, "kind": kind, "text": text}


# The one-line gloss for the acts with no special sub-cases. Each is (radio, kind, sentence-tail);
# the tail is prefixed with the flight tag by comms_row. A ``None`` radio means "no flavor".
def comms_row(rec, operator="the owner"):
    """One journal record → ``{radio, text, kind, num, tier}``. ``text`` is the real, plain, flight-
    numbered sentence (always non-empty); ``radio`` is optional flavor shown beside it; ``kind`` is
    a style class; ``tier`` is the comms/routine classification (issue #36 — see :func:`tier`). A
    non-dict record degrades to a bare "unreadable line" row (comms tier), never a crash.
    ``operator`` is the configured operator display name (issue #58) — a re-approval is the owner's
    own gate, so its line signs their name."""
    if not isinstance(rec, dict):
        return {"radio": "", "text": "an unreadable journal line", "kind": "unknown",
                "num": None, "tier": "comms"}
    act = rec.get("act")
    num = _num(rec)
    who = _who(num)

    if act == "merge":
        row = _merge_row(rec, num)
    elif act == "event":
        row = _event_row(rec, num)
    elif act == "launch":
        row = {"radio": "Cleared for takeoff.", "kind": "launch",
               "text": "%s departed — build session started." % who}
    elif act == "park":
        row = {"radio": "Mayday.", "kind": "park",
               "text": "%s parked — the machine gave up; your call." % who}
    elif act == "hold":
        row = {"radio": "Number two for landing.", "kind": "hold",
               "text": "%s holding — number 2 for landing, behind an overlapping lane." % who}
    elif act == "regenerate":
        n = rec.get("conflicts")
        tail = " (conflict #%s)" % n if isinstance(n, int) and not isinstance(n, bool) else ""
        row = {"radio": "Going around.", "kind": "regen",
               "text": "%s go-around — rebuilding from scratch%s." % (who, tail)}
    elif act == "nudge":
        msg = _first_line(rec.get("message")) or (rec.get("nudge_key") or "a reminder")
        row = {"radio": "Tower to %s." % (_tag(num) or "the flight"), "kind": "nudge",
               "text": "%s nudge — %s" % (who, msg)}
    elif act == "hire_answerer":
        q = _first_line(rec.get("question")) or "a blocking question"
        row = {"radio": "%s to tower." % (_tag(num) or "Aircraft"), "kind": "radio",
               "text": "%s radio — worker blocked: %s (auto-tower answering)." % (who, q)}
    elif act == "deliver_answer":
        a = _first_line(rec.get("text")) or "answer delivered"
        row = {"radio": "Tower to %s." % (_tag(num) or "aircraft"), "kind": "answer",
               "text": "%s answer — auto-tower: %s" % (who, a)}
    elif act == "gate":
        if rec.get("outcome") == "ok":
            row = {"radio": "", "kind": "gate", "text": "%s cleared the gate." % who}
        else:                                # a failed/held gate is NOT a pass — never read as cleared
            reason = _first_line(rec.get("outcome")) or "a check is not green"
            row = {"radio": "", "kind": "gate", "text": "%s held at the gate — %s." % (who, reason)}
    elif act == "notify":
        row = {"radio": "", "kind": "notify", "text": "note — %s" % (rec.get("title") or "(memo)")}
    elif act in ("reapprove", "approve"):
        row = _reapprove_row(rec, num, operator)
    elif act == "relabel":
        row = {"radio": "", "kind": "relabel", "text": "%s relabelled." % who}
    elif act == "update":
        row = {"radio": "", "kind": "update",
               "text": "%s update — %s." % (who, _first_line(rec.get("outcome")) or "in progress")}
    elif act == "debug_launch":
        # An owner-tap debugger launch (`superlooper debug`, issue #144). Repo-wide, not a flight:
        # its id is d<N>, so the generic fallback rendered the nonsense "the flight debug_launch."
        # The operator comes from the RECORD (who actually tapped), never the dashboard's configured
        # name — a launch made from a terminal by someone else must not be signed with the owner's.
        sid = rec.get("id") or "a fixer"
        by = rec.get("operator") or operator
        # The three-way read is ``lib/fixer.launch_outcome``'s, not a second copy of it: the trouble
        # banner glosses this same record beside the button that made it (issue #458), and two
        # classifications of one launch is how two surfaces come to disagree in the same frame.
        outcome = fixer_mod.launch_outcome(rec)
        if outcome == fixer_mod.LAUNCHED:
            row = {"radio": "Engineering to the field.", "kind": "launch",
                   "text": "Fixer %s deployed by %s — a debug session is on the field." % (sid, by)}
        elif outcome == fixer_mod.FAILED:   # no flourish for a dishonest state (§7): it did NOT start
            why = _first_line(rec.get("error")) or "no session was confirmed"
            row = {"radio": "", "kind": "alert",
                   "text": "Fixer %s did not launch — %s." % (sid, why)}
        else:
            # Neither of the engine's two words: no outcome beside the record, or one a newer engine
            # invented. "Did not launch" was a claim this record does not support — and reporting a
            # launch nobody has resolved as a failure is the same over-claim in the other direction.
            said = _first_line(rec.get("outcome"))
            row = {"radio": "", "kind": "unknown",
                   "text": "Fixer %s — no launch outcome is recorded%s; nothing has confirmed a "
                           "session." % (sid, (" (the engine said “%s”)" % said) if said else "")}
    # The three engine-level acts that name no flight (issue #253). Each fell through to the generic
    # fallback below and reached the owner as a sentence about a flight that does not exist — the
    # same defect #144 fixed for the owner-tap fixer, three more times over.
    elif act == "watchdog":
        row = _watchdog_row(rec)
    elif act == "runner_resurrect":
        row = _resurrect_row(rec)
    elif act == "runner_restart":
        row = _runner_restart_row(rec, operator)
    elif act == "alert":
        row = {"radio": "Mayday, mayday.", "kind": "alert", "text": "ALERT raised — a factory-stop."}
    elif act == "freeze":
        row = {"radio": "", "kind": "freeze", "text": "Landings paused — a repair flight is out."}
    elif act == "unfreeze":
        row = {"radio": "", "kind": "freeze", "text": "Landings resumed — the field is clear."}
    else:
        row = {"radio": "", "kind": "unknown", "text": "%s %s." % (who, str(act or "event"))}

    row["num"] = num
    row["tier"] = tier(rec)      # comms vs routine bookkeeping (issue #36) — the client binds visibility
    return row


# =============================== the "since you last looked" divider (§4) ===============================

def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def apply_divider(rows, last_seen):
    """Mark the "since you last looked" boundary on a chronological ``rows`` list (design record §4).

    Only ``comms`` rows can be fresh or carry the divider: "since you last looked" is a real-traffic
    signal, and routine bookkeeping is hidden by default (issue #36), so a fresh routine row would
    both fake "new radio traffic" and anchor the divider to a row the reader cannot see. A row with
    no ``tier`` is treated as comms (backward-compatible with pre-#36 callers).

    Each comms row gains ``fresh`` (its ``ts`` is newer than the persisted ``last_seen`` watermark);
    the FIRST fresh comms row also gains ``divider: True`` — the single line the client draws to
    separate what arrived while William was away from what he had already seen. ``last_seen`` of
    ``None`` (a first-ever look) marks nothing fresh and draws no line. A non-finite ``ts`` (a corrupt
    NaN) is never fresh — that comparison is meaningless — and never raises. Returns the count of
    fresh comms rows (the client's "N new since you last looked" badge). Mutates ``rows`` in place."""
    count = 0
    drawn = False
    for r in rows:
        ts = r.get("ts")
        fresh = (last_seen is not None and _finite(ts) and ts > last_seen
                 and r.get("tier", "comms") == "comms")
        r["fresh"] = bool(fresh)
        if fresh:
            count += 1
            if not drawn:
                r["divider"] = True
                drawn = True
    return count
