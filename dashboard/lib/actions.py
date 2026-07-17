"""The mechanical verbs — the ONLY writes in the whole product (design record §2, Task 6).

The set was six through issue #162; issue #161 splits re-approval into resume-at-the-gate (the
existing ``approve``) and an explicit ``rebuild``, so ``rebuild`` is the seventh — still a plain
label+comment write in William's name, no new KIND of write, no AI.

Every button the dashboard shows is one of exactly SIX existing mechanical verbs — approve/
re-approve, drop, expedite, bounce-yes, flag, discuss — each a label/comment/issue write made in
WILLIAM'S NAME with an audit trail. New loops must not add verbs (design record §2/§10). This
module holds their SEMANTICS as pure logic over an injected ``gh`` adapter, so the whole verb
contract is unit-tested against the fake-gh harness with mutation assertions and the server stays a
thin dispatcher (design record B.1: semantics in tested Python, never in the JS or the socket).

Hard constraints this module encodes (bright lines, not conveniences):

* **No AI, ever.** Flag files the raw text verbatim as an issue; Discuss assembles a briefing by
  string concatenation from the flight's own facts. There is no model call anywhere in here.
* **``agent-ready`` is William's word.** Approve and bounce-yes apply it ONLY as the direct effect
  of a William tap arriving at the endpoint — never autonomously, never on a schedule. The audit
  comment records that the tap happened.
* **A WATCHED repo only.** Every write is gated on an allow-list of the configured repo slugs, so a
  stray or forged request can never steer the label-writer at an arbitrary repo.
* **Fail closed.** Every underlying gh write already fails closed to ``False``/``None``; each verb
  reports that outcome honestly as ``ok`` rather than pretending a write landed.

Only READS ride the slow gh cache (decision B.2); WRITES here always take the raw ``gh`` adapter so
a label change is never served stale (the composition root wires it that way).
"""
import time

# The labels the verbs move. `agent-ready` is applied only in direct response to an operator tap
# (Approve / bounce-yes); the others are ordinary mechanical labels. The owner-decision label was
# renamed `needs-william` -> `needs-owner` (issue #58); the legacy id is still REMOVED alongside the
# new one so a repo adopted before the rename (or one mid-migration) clears cleanly on re-approve.
AGENT_READY = "agent-ready"
PARKED = "parked"
NEEDS_OWNER = "needs-owner"
NEEDS_OWNER_LEGACY = "needs-william"
EXPEDITE = "expedite"
FLAG = "flag"
# The durable-question labels/marker (#163). `awaiting-answer` is the control label the engine puts
# on an issue whose worker exited on an owner-decision question; the Answer verb clears it and
# re-applies agent-ready. ANSWER_MARKER mirrors the engine's runner.ANSWER_MARKER: the engine reads
# the LATEST comment carrying it as the owner's answer and embeds the Q&A in the relaunched brief.
AWAITING_ANSWER = "awaiting-answer"
ANSWER_MARKER = "<!-- superlooper-answer -->"
# The explicit rebuild-from-scratch flag (issue #161). Re-approving a finished lane RESUMES AT THE
# GATE by default (the engine keeps the PR/report); this label is the owner's separately-named choice
# to instead DISCARD that work and build anew. The engine reads it, then clears it once consumed. It
# is created-or-forced on first use (like FLAG) so the button works on a repo not yet re-adopted.
REBUILD = "rebuild"
_REBUILD_LABEL_COLOR = "d73a4a"              # the destructive red — this verb throws finished work away

# The flag label, created-or-updated on first use so a fresh repo's first flag just works (the gh
# adapter's create_label uses --force, so this is idempotent — never an error on a repo that already
# has the label).
_FLAG_LABEL_COLOR = "d73a4a"                 # GitHub's default red — a flag is a call for attention


def flag_label_desc(operator):
    return "Flagged by %s from the command center — a planning session sweeps these later" % operator


_FLAG_TITLE_MAX = 72                          # a flag title is the first line, trimmed — the body carries all


# =============================== audit-comment wording (pure, pinned by tests) ===============================
# One shape for every verb — "<Verb-past> by <operator> via command-center, <date>." — so the trail
# is uniform and greppable, and every write is attributable to the operator's tap (design record §0
# / CLAUDE.md). `operator` is the configured operator display name (config.operator), issue #58.

def approve_comment(operator, date):
    return "Approved by %s via command-center, %s." % (operator, date)


def drop_comment(operator, date):
    return "Dropped by %s via command-center, %s." % (operator, date)


def expedite_comment(operator, date):
    return "Expedited by %s via command-center, %s." % (operator, date)


def bounce_comment(operator, date):
    return ("Bounce accepted by %s via command-center, %s. "
            "Proceeding with the amended goal." % (operator, date))


def rebuild_comment(operator, date):
    return "Rebuilt from scratch by %s via command-center, %s." % (operator, date)


def flag_title(text):
    """A mechanical (never AI) title for a flag issue: the first non-empty line, trimmed, prefixed
    ``flag:``. The full raw text is the issue BODY; this is only a scannable heading."""
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        return "flag: (see body)"
    if len(first) > _FLAG_TITLE_MAX:
        first = first[:_FLAG_TITLE_MAX - 1].rstrip() + "…"
    return "flag: " + first


def _default_today():
    return time.strftime("%Y-%m-%d")


# =============================== the verb executor (writes) ===============================

class Actions:
    """The write-side verbs, bound to a ``gh`` adapter, an allow-list of watched repo slugs, and a
    ``today`` clock (a callable returning ``YYYY-MM-DD``; injected in tests, ``time`` in prod). Each
    method returns a result dict — always ``{"ok": bool, "verb": <name>, …}`` — that the server
    serializes back to the tap. ``ok`` is the honest GitHub outcome, so a failed write never reads as
    a success."""

    def __init__(self, gh_mod, allowed_repos, today=None, operator=None):
        self._gh = gh_mod
        self._allowed = set(allowed_repos or [])
        self._today = today if today is not None else _default_today
        # The operator display name every audit comment / flag description signs with (issue #58);
        # the composition root passes config.operator. Falls back to a neutral word if unset.
        self._operator = operator if (isinstance(operator, str) and operator.strip()) else "the owner"

    def _date(self):
        return self._today() if callable(self._today) else self._today

    def _refuse(self, verb):
        # An unwatched repo is refused BEFORE any gh call — the label-writer only ever touches the
        # repos the operator configured (bright line: never steerable off-machine or off-repo).
        return {"ok": False, "verb": verb, "error": "unknown repo"}

    def _clear_rebuild_override(self, repo, num):
        """Clear a stale ``rebuild`` override, FAIL-CLOSED (issue #161). A resume/continue verb —
        approve (resume-at-the-gate), bounce-yes, answer — must never land ``agent-ready`` beside a
        surviving ``rebuild``: the engine reads ``agent-ready + rebuild`` as the owner's choice to
        DISCARD finished work (the D11 defect), so a half-applied resume that left the override
        standing would silently rebuild. Running this BEFORE the ``agent-ready`` add makes the clear a
        precondition — its failure aborts the verb, so agent-ready never lands over a live override.

        A repo-absent OR issue-absent ``rebuild`` is a benign no-op that ``set_labels`` reports as
        success (it swallows gh's ``'rebuild' not found``), so a normal re-approval — the vast
        majority, with no rebuild anywhere — is never spuriously blocked. Only a GENUINE gh failure
        (auth / network / 5xx) returns False, and that same failure would sink the ``agent-ready`` add
        one line later regardless — so this adds no new failure mode, only the guarantee."""
        return self._gh.set_labels(repo, num, remove=[REBUILD])

    def approve(self, repo, num):
        """Approve / re-approve: apply ``agent-ready`` (William's word — this tap IS his word),
        clear ``parked`` and ``needs-william``, and leave the standard audit comment. One endpoint
        serves both the fresh approval and the re-approval of a parked flight."""
        if repo not in self._allowed:
            return self._refuse("approve")
        # Resume-at-the-gate is the OPPOSITE of a rebuild, so clear any stale `rebuild` override FIRST
        # and fail-closed (issue #161) — never land agent-ready beside a live override, or the engine
        # rebuilds. Only the `rebuild` verb ever applies that label; every other re-approval clears it.
        if not self._clear_rebuild_override(repo, num):
            return {"ok": False, "verb": "approve", "labeled": False, "commented": False,
                    "error": "could not clear a stale rebuild override — not re-approving"}
        labeled = self._gh.set_labels(repo, num, add=[AGENT_READY],
                                      remove=[PARKED, NEEDS_OWNER, NEEDS_OWNER_LEGACY])
        commented = self._gh.comment(repo, num, approve_comment(self._operator, self._date()))
        # ok requires BOTH: agent-ready is William's word ONLY when its audit comment records the
        # tap — a label applied without the trail is not a success (the "journal-greppable via audit
        # comments" contract, and the agent-ready bright line).
        return {"ok": bool(labeled and commented), "verb": "approve",
                "labeled": bool(labeled), "commented": bool(commented)}

    def drop(self, repo, num):
        """Drop: close the issue with its audit comment in one atomic gh call. The ONLY destructive
        verb — its single client-side confirm lives at the call site (never here)."""
        if repo not in self._allowed:
            return self._refuse("drop")
        closed = self._gh.close_issue(repo, num, comment=drop_comment(self._operator, self._date()))
        return {"ok": bool(closed), "verb": "drop", "closed": bool(closed)}

    def expedite(self, repo, num):
        """Expedite: add the ``expedite`` label (the runner's launch-order verb — ⚡ to the top) and
        leave an audit comment. Only adds a label; nothing is removed or closed."""
        if repo not in self._allowed:
            return self._refuse("expedite")
        labeled = self._gh.set_labels(repo, num, add=[EXPEDITE])
        commented = self._gh.comment(repo, num, expedite_comment(self._operator, self._date()))
        return {"ok": bool(labeled and commented), "verb": "expedite",
                "labeled": bool(labeled), "commented": bool(commented)}

    def rebuild(self, repo, num):
        """Rebuild from scratch (issue #161): the destructive sibling of approve. Re-applies
        ``agent-ready`` AND the ``rebuild`` label — the engine reads that label as the owner's
        explicit choice to DISCARD the finished PR/report and build anew, overriding the default
        resume-at-the-gate. Clears the park labels and leaves the standard audit comment. The
        ``rebuild`` label is create-or-forced first (idempotent ``--force``, mirroring ``flag``) so
        the verb works even on a repo not yet re-adopted after #161 shipped — gh refuses to apply a
        label that does not exist. ``agent-ready`` is William's word: ``ok`` requires BOTH the label
        move and its audit comment, so a label applied without the trail is not a success."""
        if repo not in self._allowed:
            return self._refuse("rebuild")
        self._gh.create_label(repo, REBUILD, _REBUILD_LABEL_COLOR,
                              "%s's explicit rebuild: discard this issue's PR and review and build "
                              "from scratch" % self._operator)
        labeled = self._gh.set_labels(repo, num, add=[AGENT_READY, REBUILD],
                                      remove=[PARKED, NEEDS_OWNER, NEEDS_OWNER_LEGACY])
        commented = self._gh.comment(repo, num, rebuild_comment(self._operator, self._date()))
        return {"ok": bool(labeled and commented), "verb": "rebuild",
                "labeled": bool(labeled), "commented": bool(commented)}

    def bounce_yes(self, repo, num):
        """Bounce-yes: accept a bounced flight's proposed amendment — re-apply ``agent-ready`` and
        clear ``needs-william`` (and ``parked`` if it lingers) so the runner relaunches it, with an
        audit comment naming the accepted bounce. Distinct verb, distinct trail from a plain
        approve."""
        if repo not in self._allowed:
            return self._refuse("bounce-yes")
        # Clear a stale `rebuild` override first, fail-closed (issue #161): a bounce-accept rebuilds
        # from the AMENDED premise, so it must never inherit a prior tap's rebuild override.
        if not self._clear_rebuild_override(repo, num):
            return {"ok": False, "verb": "bounce-yes", "labeled": False, "commented": False,
                    "error": "could not clear a stale rebuild override — not re-approving"}
        labeled = self._gh.set_labels(repo, num, add=[AGENT_READY],
                                      remove=[NEEDS_OWNER, NEEDS_OWNER_LEGACY, PARKED])
        commented = self._gh.comment(repo, num, bounce_comment(self._operator, self._date()))
        return {"ok": bool(labeled and commented), "verb": "bounce-yes",
                "labeled": bool(labeled), "commented": bool(commented)}

    def answer(self, repo, text, num):
        """Answer a durable owner-decision question (#163): post the owner's typed answer VERBATIM as
        a marked comment and re-apply ``agent-ready`` (William's word — this tap IS his word),
        clearing ``awaiting-answer``. The runner then relaunches a fresh session with the full Q&A in
        its brief, reusing the pushed WIP branch if it still applies cleanly. Two existing mechanical
        primitives only — a comment and a label move — no AI, no new machinery, no summarizing.

        ``ok`` requires BOTH the durable answer comment AND the re-approval: ``agent-ready`` is
        William's word only when the answer that earned it is on the record (the agent-ready bright
        line + the audit-trail contract)."""
        if repo not in self._allowed:
            return self._refuse("answer")
        text = (text or "").strip()
        if not text:
            return {"ok": False, "verb": "answer", "error": "empty answer"}
        # Clear a stale `rebuild` override first, fail-closed (issue #161): answering a durable
        # question resumes with the WIP reused, so it must never inherit a rebuild override.
        if not self._clear_rebuild_override(repo, num):
            return {"ok": False, "verb": "answer", "commented": False, "labeled": False,
                    "error": "could not clear a stale rebuild override — not re-approving"}
        commented = self._gh.comment(repo, num, "%s\n%s" % (ANSWER_MARKER, text))
        labeled = self._gh.set_labels(repo, num, add=[AGENT_READY],
                                      remove=[AWAITING_ANSWER, NEEDS_OWNER, NEEDS_OWNER_LEGACY])
        return {"ok": bool(commented and labeled), "verb": "answer",
                "commented": bool(commented), "labeled": bool(labeled)}

    def flag(self, repo, text):
        """Flag: file the raw text VERBATIM as a new issue labeled ``flag`` (no AI, no summarizing),
        creating the ``flag`` label on first use. Returns the new issue number in ``num``. Empty
        text is refused with no gh call."""
        if repo not in self._allowed:
            return self._refuse("flag")
        text = (text or "").strip()
        if not text:
            return {"ok": False, "verb": "flag", "error": "empty flag"}
        # Create-or-update the label first (idempotent via --force) so the labeled create can't fail
        # for want of the label on a repo seeing its first flag.
        self._gh.create_label(repo, FLAG, _FLAG_LABEL_COLOR, flag_label_desc(self._operator))
        num = self._gh.create_issue(repo, flag_title(text), text, labels=[FLAG])
        return {"ok": num is not None, "verb": "flag", "num": num}


# =============================== discuss (a composer — no write, no AI) ===============================

_STAGE_PHRASE = {
    "at-stand": "at the stand — approved, waiting for a runway",
    "taxi-out": "taxiing out — launching",
    "takeoff": "on takeoff — session just started",
    "downwind": "downwind — building (the long working leg)",
    "base-turn": "base turn — report filed, turning toward the gate",
    "final": "on final — report ✓ review ✓ CI ✓ mergeable ✓, cleared to land",
    "touchdown": "touchdown — merged",
    "taxi-in": "taxiing in — closed and cleaned up",
    "parked": "parked — the machine gave up; your call",
    "awaiting": "awaiting your decision",
    "holding": "holding — number two for landing",
    "session-frozen": "session frozen — a stalled session, no contrail",
    "stranded": "stranded at the gate — report filed, but the runner hasn't landed it",
    "merges-freeze": "landings paused — a repair flight is out",
}


def _find_flight(snapshot, repo_slug, num):
    for repo in (snapshot or {}).get("repos", []):
        if repo.get("slug") != repo_slug:
            continue
        for f in repo.get("flights", []):
            if f.get("num") == num:
                return repo, f
        return repo, None            # right repo, flight not on the field
    return None, None


def _diff_line(flight):
    cargo = flight.get("cargo") or {}
    if cargo.get("present") and (cargo.get("added") or cargo.get("removed")):
        files = cargo.get("files")
        tail = " across %d file%s" % (files, "" if files == 1 else "s") if files else ""
        return "Changes so far: +%d/−%d%s." % (int(cargo.get("added", 0)),
                                               int(cargo.get("removed", 0)), tail)
    return "Changes so far: none yet."


def compose_briefing(snapshot, repo_slug, num):
    """Assemble a plain-text briefing snippet for a fresh Claude session from the flight's OWN facts
    — pure string assembly, no AI (design record §2: Discuss copies a ready snippet the client puts
    on the clipboard). Finds the flight by (slug, num) in the snapshot; when it isn't on the field
    (e.g. a still-queued issue) it degrades to a minimal but usable stub — a pointer to the issue,
    never a crash."""
    repo, flight = _find_flight(snapshot, repo_slug, num)
    name = (repo.get("name") if repo else None) or repo_slug
    issue_url = "https://github.com/%s/issues/%s" % (repo_slug, num)

    if flight is None:
        return ("Let's dig into SL-%s in %s.\n"
                "Issue: %s\n\n"
                "Help me decide what to do with it "
                "(re-approve, drop, expedite, or amend the goal)." % (num, name, issue_url))

    lines = ["Let's dig into SL-%s — %s (%s).\n" % (num, name, repo_slug)]

    stage = flight.get("stage")
    status_bits = [_STAGE_PHRASE.get(stage, stage or "unknown")]
    attempt = flight.get("attempt", 1)
    if attempt and attempt > 1:
        status_bits.append("attempt %d" % attempt)
    if flight.get("wander"):
        status_bits.append("wandered — see report")
    lines.append("Status: " + " · ".join(status_bits) + ".")

    memo = (flight.get("memo") or "").strip()
    if memo:
        lines.append("Note: " + memo)

    lines.append(_diff_line(flight))

    pr = flight.get("pr")
    if pr:
        lines.append("PR: https://github.com/%s/pull/%s" % (repo_slug, pr))
    lines.append("Issue: " + issue_url)

    lines.append("\nHelp me decide what to do with it "
                 "(re-approve, drop, expedite, or amend the goal).")
    return "\n".join(lines)
