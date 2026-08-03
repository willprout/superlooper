"""The re-orientation preamble a REVIVED session reads before anything else (issue #298).

`claude --resume <id>` restores the conversation exactly — and tells the session NOTHING about
what happened to the world while it was dead. That asymmetry is the whole hazard, and the rule
that answers it is fixed (docs/HERDR-ADOPTION-PLAN.md §4): *a revived session remembers the
conversation, not the world.*

Three specific beliefs a revived session holds with full confidence and no evidence:

  * **"I just pushed / just opened the PR."** The transcript ends with the tool CALL. A `kill -9`
    lands wherever it lands, so the call may have half-completed, fully completed, or never run.
  * **"the branch is where I left it."** It may have been rebased, merged, force-updated by
    nobody, or reclaimed — hours or days may have passed.
  * **"CI was green."** It was, once. Nothing about that survives the interruption.

So every fact below is RE-READ at revive time and labelled as freshly read, and the preamble
states outright that memory of the world is stale. An unreadable fact degrades to a named unknown
rather than to silence: a session told nothing assumes its memory still holds, which is the exact
failure this module exists to prevent (the same fail-closed discipline as gh.PrRead's ``ok``).

Pure and side-effect free — no git, no gh, no cmux. The CALLER reads the facts and passes them in
(``superlooper resume``), so this text is testable without a repo or a network.
"""

_UNKNOWN = "unknown (could not be read at revive time — check it yourself before relying on it)"


def _pr_line(facts):
    """One honest sentence about the PR. A REFUSED lookup must never read as "there is no PR":
    gh answers ok=False when it timed out / had no binary / returned an unparseable body, and a
    revived session told "no PR exists" opens a second one on the same branch."""
    if not facts.get("pr_ok"):
        return ("**PR:** unknown — GitHub did not answer the lookup. Do NOT read this as "
                "\"no PR exists\"; re-check before opening one.")
    pr = facts.get("pr") or {}
    number = pr.get("number")
    if not number:
        return "**PR:** no PR exists on this branch (a clean answer from GitHub, not a failed read)."
    state = pr.get("state") or "unknown state"
    # Deliberately NOT reporting check status: the caller does not read it, and a line that named
    # CI would be the one fact here that was remembered rather than re-read.
    return "**PR:** #%s (%s) — its checks and comments are NOT reported here; go and look." % (
        number, state)


def _tree_line(facts):
    dirty = facts.get("dirty")
    if dirty is None:
        return "**Working tree:** %s" % _UNKNOWN
    if dirty == 0:
        return "**Working tree:** clean — nothing uncommitted."
    return ("**Working tree:** %d uncommitted change(s). Look before you commit: some may be "
            "yours from before the interruption, mid-edit." % dirty)


def _or_unknown(value):
    return value if value else _UNKNOWN


def render(facts):
    """The complete opening message for a revived session.

    ``facts`` keys: id, session_id, branch, worktree, head, dirty, pr, pr_ok, note, lane_status.
    Everything except ``id``/``session_id`` may be missing or None — it degrades to a named
    unknown. ``note`` is the operator's new instruction and is placed LAST, after the
    re-orientation, so the session can never act on it while still believing a stale world.
    """
    head = facts.get("head")
    head_txt = ("`%s`" % head[:12]) if head else _UNKNOWN
    lines = [
        "# Re-orientation — you were interrupted and have just been resumed",
        "",
        # Agent-neutral by construction: naming a specific CLI's flag would put an agent-specific
        # fact in a lib module, which the agent-boundary rule reserves for the launcher.
        "This is session `%s` for lane **%s**, re-entered after an interruption." % (
            facts.get("session_id") or _UNKNOWN, facts.get("id") or _UNKNOWN),
        "",
        "**Read this before you do anything else.** Your conversation above survived intact; the "
        "world it describes did not. Time passed between your last message and this one — possibly "
        "a lot of it. Everything you remember about the repo, the branch, the PR and CI is from "
        "BEFORE the interruption and may be stale.",
        "",
        "In particular: your last action may have been cut off mid-flight. The transcript shows "
        "that you CALLED a tool, never that the effect landed — a commit, a push, a `gh pr create` "
        "or a file write may have half-completed or never run at all. Verify, do not assume, and "
        "do not blindly redo: check whether the work is already there first.",
        "",
        "## The world as re-read just now",
        "",
        "**Branch:** %s" % _or_unknown(facts.get("branch")),
        "**Worktree:** %s" % _or_unknown(facts.get("worktree")),
        "**HEAD:** %s" % head_txt,
        _tree_line(facts),
        _pr_line(facts),
    ]
    if facts.get("lane_status"):
        lines.append("**Lane status:** %s" % facts["lane_status"])
    lines += [
        "",
        "These were read at revive time, not remembered. Anything not listed above — CI detail, "
        "review comments, what landed on the mainline while you were gone — you have not been "
        "told, so go and look.",
        "",
        "Then pick up where you left off, under your original brief and its ship gate. Re-read the "
        "issue before you rely on your memory of it: nothing here re-read it for you, and it may "
        "have been amended, relabelled or closed while you were gone.",
    ]
    note = (facts.get("note") or "").strip()
    if note:
        lines += [
            "",
            "## New instruction from the operator",
            "",
            "(Deliberately last: re-orient on the facts above BEFORE acting on this.)",
            "",
            note,
        ]
    return "\n".join(lines) + "\n"
