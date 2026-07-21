"""The weekly once-over, as census arithmetic and one page of text (issue #200).

`superlooper upkeep` is one batched owner touch replacing a handful of remembered chores: glance at
the machine stack, at how far the installed engine has drifted from main, at what GitHub debris the
janitor would propose, at whether the ops docs still name live things, at the branch/worktree
sprawl, at whether the notify channel actually delivered anything this week, and at the loop's own
question and park rates.

THIS module is the pure half — every function takes already-fetched data and returns data or lines,
with no disk, no gh, no clock and no subprocess. The CLI (`skill/bin/superlooper upkeep`) does the
gathering; that split is what lets the whole report be unit-tested against hand-built views,
including the wrong-typed shapes a corrupt state file produces.

Two properties are load-bearing, not stylistic:

  * **Read-only, by construction rather than by care.** Nothing here writes, and the CLI half calls
    only readers: ``janitor.propose`` (the same pure selector ``superlooper janitor --dry-run``
    uses), ``stack_doctor.check_stack`` with the ``SKIP_SEND`` sentinel so the notify block does not
    push a message, ``gitops.worktree_reclaim_block`` (documented read-only and network-free), and
    the journal. The issue's Boundaries put an ``--execute`` permanently out of scope; the report's
    job is to name the EXISTING one-tap remedy for each finding and stop there.
  * **A finding always carries its remedy.** A weekly report that says "12 commits behind" and
    leaves the reader to remember how to publish is another chore, not fewer. Every row that has
    something to say ends in a `->` line naming the exact command, and each of those commands is one
    that already existed and already has its own approval gate.

The layout is deliberately narrow (a label column plus wrapped detail) so the whole thing fits one
terminal screen — the report is meant to be GLANCED at, and anything that scrolls gets skimmed.
"""

WEEK_SECONDS = 7 * 24 * 3600

# The section labels, in report order. Public because the tests assert the page is complete by
# iterating this rather than by re-typing the list — a section quietly dropped would otherwise pass.
SECTIONS = ("stack", "engine", "janitor", "docs", "branches", "worktrees", "notify", "week")

_LABEL_WIDTH = 10
_INDENT = " " * (_LABEL_WIDTH + 2)

# Stack blocks that already have a dedicated row further down the page. Their WARNs are folded out
# of the stack summary — an engine 12 commits behind is one line about publishing, not two — but a
# FAIL from any of them is ALWAYS shown, because a FAIL means something the dedicated row cannot
# say: `notify channel` FAILs when no channel is configured at all or the config would not load,
# and that must never be swallowed by a canary row that has no canary to report.
# `tests/test_upkeep.py` pins every name here against the doctor's live block list, so a renamed
# block fails loudly instead of silently un-folding.
STACK_BLOCKS_WITH_OWN_ROW = ("installed engine current", "notify channel")

# How many janitor proposals / worktree findings the page lists before it says "and N more". Same
# reasoning as doc_lint.MAX_FINDINGS: truncate to stay one page, but never silently.
MAX_ITEMS = 6


# --------------------------- coercion helpers (fail closed) ---------------------------

def _records(v):
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _ts(rec):
    t = rec.get("ts")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _num(v):
    return v if type(v) is int else None


def _s(v, default=""):
    return v if isinstance(v, str) else default


def _plural(n, word, suffix="s"):
    return "%d %s%s" % (n, word, "" if n == 1 else suffix)


# --------------------------- the censuses ---------------------------

def week_counts(records, now):
    """The loop's own rates over the last 7 days, from the journal.

    ``{"questions", "question_issues", "parks", "needs_owner", "bounces", "merges"}`` — all ints.

    Only SUCCESSFUL records count: a `post_question` that failed never reached the owner, so it is
    not a question asked, and counting it would inflate the very rate the owner reads to decide
    whether work is arriving under-specified. `merges` rides along as the denominator that makes
    "3 questions" mean something — three questions across thirty merges is a different week from
    three across four.

    Window boundary matches ``report._in_window``: a record with no parseable ts is KEPT (journal
    always stamps one, so an unstamped record is a corrupt line, and honest over-reporting beats a
    silently shrinking count). Every wrong-typed input degrades to zero rather than raising."""
    cutoff = (now - WEEK_SECONDS) if isinstance(now, (int, float)) and not isinstance(now, bool) \
        else float("-inf")
    counts = {"questions": 0, "question_issues": 0, "parks": 0, "needs_owner": 0,
              "bounces": 0, "merges": 0}
    question_nums = set()
    for rec in _records(records):
        if rec.get("outcome") != "ok":
            continue
        ts = _ts(rec)
        if ts is not None and ts < cutoff:
            continue
        act = rec.get("act")
        if act == "post_question":
            counts["questions"] += 1
            num = _num(rec.get("num"))
            if num is not None:
                question_nums.add(num)
        elif act == "park":
            counts["parks"] += 1
            # `needs_william` is the journal's own field name for the needs-OWNER flag (the label
            # was renamed in #58; the record key was not, and rewriting history is not this
            # module's business). Read it, present it under today's name.
            if rec.get("needs_william") is True:
                counts["needs_owner"] += 1
        elif act == "bounce":
            counts["bounces"] += 1
        elif act == "merge":
            counts["merges"] += 1
    counts["question_issues"] = len(question_nums)
    return counts


def branch_census(branches, proposals):
    """How many `sl/*` branches the remote carries, and how many the janitor can prove are debris.

    ``{"total", "sl", "proposed", "kept"}``. `kept` — the sl/* branches the janitor will NOT
    propose deleting — is the number worth reading: the janitor only proposes a branch whose PR
    provably merged or was provably superseded AND whose tip has not moved since. Everything else
    accumulates silently forever, and nothing mechanical will ever clear it. A climbing `kept` is
    the signal that a human has to look.

    `branches` is ``gh.remote_branches()``'s {name: tip}; `proposals` is ``janitor.propose``'s
    list. Both fail closed to empty."""
    names = [b for b in branches if isinstance(b, str)] if isinstance(branches, dict) else []
    sl = [b for b in names if b.startswith("sl/")]
    proposed = {p.get("target") for p in _records(proposals) if p.get("kind") == "branch"}
    hit = [b for b in sl if b in proposed]
    return {"total": len(names), "sl": len(sl), "proposed": len(hit),
            "kept": len(sl) - len(hit)}


def worktree_census(worktree_ids, issues, blocks):
    """What lane checkouts are on disk and which of them hold work that exists nowhere else.

    ``{"total", "reclaimable": [iid], "held": [{"id","status","block"}]}``.

    `reclaimable` reuses ``tidy.reclaimable_worktrees``' exact rule via that module, so this can
    never drift from what the runner's opt-in reaper would actually take.

    `held` is the finding a weekly glance exists for: a checkout that is FINISHED WITH — park-family
    terminal, or carrying no lane record at all — whose ``gitops.worktree_reclaim_block`` says
    dirty / unpushed / unreadable. That combination means the only copy of a worker's output lives
    in a checkout nothing will ever reclaim (issue #190's refusal, working exactly as intended), so
    it sits on disk until a person decides — and a person only decides if something tells them.

    A LIVE lane is deliberately excluded, and this is the whole subtlety: an in-flight or mid-gate
    worktree is dirty because a worker is writing in it right now. Reporting that as unsaved work
    would put a line on the weekly page every single week for the healthiest possible reason, which
    is how a report teaches its reader to skim past it. (Found by running upkeep against this repo:
    the lane building this very feature was the first thing it flagged.) `merged` is excluded too —
    its removal rides the merge-time path and its own ``cleanup_merged_worktrees`` gate.

    `blocks` is {iid: reason-or-None} from ``gitops.worktree_reclaim_block``; a missing entry reads
    as "not checked", which is neither reclaimable nor held. Every wrong-typed input -> empty."""
    import tidy

    ids = sorted({w for w in worktree_ids if isinstance(w, str)}
                 if isinstance(worktree_ids, (set, frozenset, list, tuple)) else set())
    issues = issues if isinstance(issues, dict) else {}
    blocks = blocks if isinstance(blocks, dict) else {}
    safe = [i for i in tidy.reclaimable_worktrees(issues, ids)
            if not _s(blocks.get(i))]
    held = []
    for iid in ids:
        ist = issues.get(iid)
        known = isinstance(ist, dict) and isinstance(ist.get("status"), str)
        status = ist["status"] if known else "(no lane record)"
        block = _s(blocks.get(iid))
        # Only a checkout nobody is still writing in, and that nothing will come back for.
        if block and (not known or status in tidy.REAPPROVABLE):
            held.append({"id": iid, "status": status, "block": block})
    return {"total": len(ids), "reclaimable": safe, "held": held}


# --------------------------- the page ---------------------------

def _row(label, text, out):
    out.append("%-*s%s" % (_LABEL_WIDTH + 2, label, text))


def _sub(text, out):
    out.append(_INDENT + text)


def _remedy(text, out):
    out.append(_INDENT + "-> " + text)


def _more(items, out, render):
    """List up to MAX_ITEMS, then SAY what was dropped. A truncated list with no remainder reads as
    the whole story, which is how a weekly report starts lying."""
    for item in items[:MAX_ITEMS]:
        _sub(render(item), out)
    extra = len(items) - MAX_ITEMS
    if extra > 0:
        _sub("... and %d more" % extra, out)


def fold_stack(results):
    """``stack_doctor`` CheckResults -> the plain dicts ``render`` reads, minus the duplicates.

    Takes anything with ``.name/.ok/.warn/.detail`` (the real dataclass, or a stand-in) and returns
    ``[{"name","ok","warn","detail"}]``. WARNs from ``STACK_BLOCKS_WITH_OWN_ROW`` are dropped — see
    that constant for why — and everything else, pass or fail, is kept so the counts stay honest.
    Wrong-typed input -> ``[]``."""
    out = []
    for r in results if isinstance(results, list) else []:
        name = _s(getattr(r, "name", None), "?")
        ok = bool(getattr(r, "ok", False))
        warn = bool(getattr(r, "warn", False)) and ok
        if warn and name in STACK_BLOCKS_WITH_OWN_ROW:
            continue
        out.append({"name": name, "ok": ok, "warn": warn,
                    "detail": _s(getattr(r, "detail", None))})
    return out


def _stack_row(stack, repo_path, out):
    if not isinstance(stack, list):
        _row("stack", "not read — the machine-level checks could not run.", out)
        _remedy("superlooper doctor --stack --repo %s" % repo_path, out)
        return
    fails = [r for r in stack if isinstance(r, dict) and not r.get("ok")]
    warns = [r for r in stack if isinstance(r, dict) and r.get("ok") and r.get("warn")]
    passes = len(stack) - len(fails) - len(warns)
    _row("stack", "%d FAIL, %d WARN, %d ok" % (len(fails), len(warns), passes), out)
    # Only what needs looking at. A one-page report that re-prints twelve passing blocks buries
    # the one line that matters — `doctor --stack` is where the full readout lives.
    for r in fails + warns:
        _sub("%s %s%s" % ("FAIL" if not r.get("ok") else "WARN", _s(r.get("name"), "?"),
                          (" — " + _s(r.get("detail"))) if _s(r.get("detail")) else ""), out)
    if fails or warns:
        _remedy("superlooper doctor --stack --repo %s   (also sends the live notify test)"
                % repo_path, out)


def _engine_row(drift, out):
    drift = drift if isinstance(drift, dict) else {}
    status = _s(drift.get("status"), "unknown")
    if status == "in_sync":
        _row("engine", "installed engine is current with %s" % _s(drift.get("ref"), "the mainline"),
             out)
        return
    if status == "skipped":
        _row("engine", "drift not measured — %s" % _s(drift.get("detail")), out)
        return
    if status == "behind":
        behind = drift.get("behind")
        behind = behind if isinstance(behind, int) and not isinstance(behind, bool) else 0
        _row("engine", "%s behind %s (stamp %s)"
             % (_plural(behind, "engine commit"), _s(drift.get("ref"), "the mainline"),
                _s(drift.get("installed_sha"), "?")), out)
        # The engine's real backstop is publishing: merged engine changes are INERT until someone
        # republishes through the one gated door, which shows the diff and asks for an explicit OK.
        _remedy("bin/install.sh from the monorepo root — the one gated publish door "
                "(shows the diff, requires an explicit OK)", out)
        return
    _row("engine", "drift unknown — %s" % _s(drift.get("detail")), out)
    _remedy("bin/install.sh from the monorepo root re-stamps the installed engine", out)


def _janitor_row(janitor, repo_path, out):
    janitor = janitor if isinstance(janitor, dict) else {}
    error = _s(janitor.get("error"))
    if error:
        _row("janitor", "could not propose — %s" % error, out)
        return
    props = _records(janitor.get("proposals"))
    held = [h for h in (janitor.get("held") or []) if isinstance(h, str)] \
        if isinstance(janitor.get("held"), list) else []
    if not props:
        _row("janitor", "nothing to propose"
             + (" (%s held back from a prior failure)" % _plural(len(held), "action")
                if held else ""), out)
        return
    kinds = {}
    for p in props:
        kinds[_s(p.get("kind"), "?")] = kinds.get(_s(p.get("kind"), "?"), 0) + 1
    summary = ", ".join("%d %s" % (n, k) for k, n in sorted(kinds.items()))
    _row("janitor", "%s: %s" % (_plural(len(props), "proposal"), summary), out)
    _more(props, out, lambda p: "%s %s — %s" % (_s(p.get("action"), "?"), p.get("target"),
                                                _s(p.get("why"))))
    if held:
        _sub("(%s held back from a prior failure — --retry-refused re-proposes)"
             % _plural(len(held), "action"), out)
    # Nothing here executes without the owner's word — that is the janitor's whole contract, and
    # naming the command rather than doing it is why upkeep can stay read-only.
    _remedy("superlooper janitor --repo %s   (y/N per sweep; the command center taps the same "
            "proposals by key)" % repo_path, out)


def _docs_row(lint, out):
    lint = lint if isinstance(lint, dict) else {}
    status = _s(lint.get("status"), "unknown")
    docs = lint.get("docs")
    docs = docs if isinstance(docs, int) and not isinstance(docs, bool) else 0
    if status == "clean":
        _row("docs", "doc-lint clean — %s checked" % _plural(docs, "operational doc"), out)
        return
    if status == "skipped":
        _row("docs", "doc-lint skipped — %s" % _s(lint.get("detail")), out)
        return
    findings = [f for f in (lint.get("findings") or []) if isinstance(f, str)] \
        if isinstance(lint.get("findings"), list) else []
    _row("docs", "doc-lint: %s across %s"
         % (_plural(len(findings), "finding"), _plural(docs, "operational doc")), out)
    _more(findings, out, lambda f: f)
    if _s(lint.get("detail")):
        _sub(_s(lint.get("detail")), out)
    # A dead name in an ops doc is defect class D12: the operator (or a helper agent) acts on it
    # mid-incident and the recovery goes wrong. The fix is always an edit to the doc named above.
    _remedy("edit the doc named above — the lint runs in CI as tests/test_doc_lint.py", out)


def _branches_row(census, repo_path, out):
    census = census if isinstance(census, dict) else {}
    sl = census.get("sl", 0)
    kept = census.get("kept", 0)
    proposed = census.get("proposed", 0)
    _row("branches", "%s on the remote; %d proposed for deletion, %d not provably landed"
         % (_plural(sl, "`sl/*` branch", "es"), proposed, kept), out)
    if proposed:
        _remedy("superlooper janitor --repo %s deletes exactly the provable ones" % repo_path, out)


def _worktrees_row(census, out):
    census = census if isinstance(census, dict) else {}
    total = census.get("total", 0)
    reclaimable = census.get("reclaimable") or []
    held = census.get("held") or []
    _row("worktrees", "%d on disk; %d park-family reclaimable, %d holding unsaved work"
         % (total, len(reclaimable), len(held)), out)
    if held:
        _more(held, out, lambda h: "%s (%s) holds %s — nothing will reclaim it"
              % (_s(h.get("id"), "?"), _s(h.get("status"), "?"), _s(h.get("block"), "?")))
        # There is deliberately NO one-command remedy here: reclaiming a checkout that holds the
        # only copy of a worker's output is exactly what issue #190 made impossible. Say what to
        # look at; the decision is the owner's, by hand, per checkout.
        _remedy("open each checkout above and save or discard its work by hand — the reclaim "
                "sweep refuses these on purpose (issue #190)", out)


def _notify_row(canary, out):
    canary = canary if isinstance(canary, dict) else {}
    status = _s(canary.get("status"), "unverified")
    channel = _s(canary.get("channel"), "?")
    if status == "healthy":
        _row("notify", "healthy — the last push delivered via %s" % channel, out)
        return
    if status == "unverified":
        _row("notify", "not verified — no canary recorded in the journal window", out)
        _remedy("superlooper doctor --stack sends a live test message and proves the channel", out)
        return
    if status == "unconfigured":
        _row("notify", "NO CHANNEL CONFIGURED — pushes go to the journal only", out)
        _remedy("set notify.imessage_to or notify.cmd in .superlooper/config.json, then "
                "superlooper doctor --stack", out)
        return
    rc = canary.get("rc")
    rc_s = ", rc=%s" % rc if isinstance(rc, int) and not isinstance(rc, bool) else ""
    _row("notify", "DEAD — the last push did not deliver via %s%s: %s"
         % (channel, rc_s, _s(canary.get("detail"), "(no error captured)")), out)
    _remedy("fix the channel, then superlooper doctor --stack   (alerts are NOT reaching you)", out)


def _week_row(counts, out):
    counts = counts if isinstance(counts, dict) else {}

    def n(key):
        v = counts.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    _row("week", "%s from %s; %s (%d needs-owner), %s; %s landed"
         % (_plural(n("questions"), "owner question"), _plural(n("question_issues"), "issue"),
            _plural(n("parks"), "park"), n("needs_owner"),
            _plural(n("bounces"), "bounce"), _plural(n("merges"), "merge")), out)


def render(view):
    """The whole one-page report as a list of lines. PURE — no disk, no clock, no gh.

    `view` keys (every one fails closed to an honest "not read" row rather than raising):
      repo, repo_path, state_home, date   the header
      stack          [{"name","ok","warn","detail"}] — stack_doctor results, flattened
      engine_drift   stack_doctor.engine_drift()'s dict
      janitor        {"error": str|None, "proposals": [...], "held": [key]}
      doc_lint       doc_lint.lint()'s dict
      branches       branch_census()'s dict
      worktrees      worktree_census()'s dict
      notify         report.notify_canary()'s dict
      week           week_counts()'s dict
    """
    view = view if isinstance(view, dict) else {}
    out = []
    out.append("superlooper upkeep — %s   %s"
               % (_s(view.get("repo"), "(no repo slug)"), _s(view.get("date"))))
    out.append("  repo:  %s" % _s(view.get("repo_path")))
    out.append("  state: %s" % _s(view.get("state_home")))
    out.append("")
    _stack_row(view.get("stack"), _s(view.get("repo_path"), "."), out)
    _engine_row(view.get("engine_drift"), out)
    _janitor_row(view.get("janitor"), _s(view.get("repo_path"), "."), out)
    _docs_row(view.get("doc_lint"), out)
    _branches_row(view.get("branches"), _s(view.get("repo_path"), "."), out)
    _worktrees_row(view.get("worktrees"), out)
    _notify_row(view.get("notify"), out)
    _week_row(view.get("week"), out)
    out.append("")
    # The closing line is not decoration. `upkeep` reads a lot of the same surfaces the acting
    # verbs do, and the one thing a reader must never wonder is whether looking at the report
    # already did something.
    out.append("upkeep changed nothing — every action above is one owner-approved command away.")
    return out
