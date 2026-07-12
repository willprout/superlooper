"""The morning report + promotion evidence — rendered from the journal (plan Task 11/12).

PURE and FAIL-CLOSED. morning()/promotion() take already-read data and return markdown; no disk,
no gh, no clock. Every input is coerced to a safe empty shape rather than trusted, and a corrupt
or wrong-typed record is skipped, never fatal — the runner-ops promise is that a broken overnight
never takes down the report, it renders honestly ("could not parse", "nothing happened") so
William is never shown a blank that reads as either broken OR falsely green.

The journal is the durable record of what the runner DID overnight; every action record is the
actions.decide() dict + an "outcome" ("ok" or a reason), each ts-stamped by journal.append. This
module reads those back. The report's CURRENT-state facts (report date, freeze marker, ready
queue, usage) arrive via `view` — the live snapshot the runner/CLI assembles for the report; its
keys are documented on morning() below.
"""

WEEK_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600


# --------------------------- coercion helpers (fail closed) ---------------------------

def _records(journal_records):
    return [r for r in journal_records if isinstance(r, dict)] if isinstance(journal_records, list) else []


def _dict(v):
    return v if isinstance(v, dict) else {}


def _num(v):
    return v if type(v) is int else None


def _ts(rec):
    t = rec.get("ts")
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else None


def _ok(rec):
    return rec.get("outcome") == "ok"


def _count(v):
    """A journaled flake/persistent field is a count. Tolerate a list (len) or a wrong-typed
    value (0) so a malformed nightly record can never crash the health line."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple, set)):
        return len(v)
    return 0


def _reference_now(view, records):
    """The clock the 'last 7 days' windows hang off. Prefer the runner-supplied `now`; else the
    latest journaled ts (≈ report time); else 0 (an empty journal has no window)."""
    n = view.get("now")
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        return float(n)
    stamps = [t for t in (_ts(r) for r in records) if t is not None]
    return float(max(stamps)) if stamps else 0.0


def _overnight_start(records, now):
    """The lower bound for the OVERNIGHT sections (merged/parked/bounces/wanders): the last
    morning report's ts, i.e. 'everything since the previous report'. The journal is append-only
    and never rotated, so without this bound these sections would accumulate every event ever and
    the daily report — and its 'nothing happened' honesty — would degrade permanently after the
    first merge. Falls back to a 24h lookback when no prior report is recorded (first-ever run)."""
    stamps = [t for t in (_ts(r) for r in records if r.get("act") == "morning_report")
              if t is not None and t < now]
    return max(stamps) if stamps else now - DAY_SECONDS


# --------------------------- links ---------------------------

def _repo(config):
    r = config.get("repo")
    return r if isinstance(r, str) and r.strip() else None


def _issue_link(repo, num):
    return f"https://github.com/{repo}/issues/{num}" if repo else f"#{num}"


def _pr_link(repo, pr):
    return f"https://github.com/{repo}/pull/{pr}" if repo else f"PR #{pr}"


# --------------------------- section builders ---------------------------

def _in_window(rec, window_start):
    """A ts-stamped record is in-window when ts >= window_start; a record without a ts (journal
    always stamps one, so this is only a corrupt line) is kept — honest over-reporting beats
    silently dropping it."""
    ts = _ts(rec)
    return ts is None or ts >= window_start


_MERGE_ACTS = ("merge", "absorb_merged")


def _reconciled_parks(records, window_start):
    """Issue numbers whose in-window park was SUPERSEDED by a later landing (merge/absorb_merged) —
    i.e. the issue parked, was re-approved, and merged (#37). Such a park is no longer an OPEN ask:
    its final outcome is 'merged', so it must not be reported as needing William.

    Reconciliation is by final outcome, not mere co-occurrence — a park counts as resolved only when
    a landing came strictly at-or-after it (latest merge ts >= latest park ts). A merge that came
    BEFORE the park (an issue that landed, re-opened, then parked again) leaves the park as the
    latest word and a genuine open ask. A corrupt park with no comparable ts, paired with any
    in-window landing, is treated as resolved (a same-window landing is strong evidence the ask was
    answered); a landing with no comparable ts never resolves a real-ts park (honest over-report)."""
    park_ts, merge_ts = {}, {}

    def note(store, num, ts):
        # track the latest KNOWN ts per issue; a missing/corrupt ts records presence at -inf so it
        # never wins a max() against a real stamp.
        store[num] = max(store.get(num, float("-inf")), ts if ts is not None else float("-inf"))

    for r in records:
        if not (_ok(r) and _in_window(r, window_start)):
            continue
        num = _num(r.get("num"))
        if num is None:
            continue
        act = r.get("act")
        if act == "park":
            note(park_ts, num, _ts(r))
        elif act in _MERGE_ACTS:
            note(merge_ts, num, _ts(r))
    return {num for num, pts in park_ts.items() if num in merge_ts and merge_ts[num] >= pts}


def _merged(records, repo, window_start, parked_earlier=frozenset()):
    """Clean merges AND absorbed out-of-band merges (a PR that landed on GitHub between merge and
    bookkeeping) — both are issues that landed. Windowed to the overnight bound, deduped by issue
    number, latest record wins. Issues in `parked_earlier` (parked then later merged this window)
    carry an inline note so the resolved park episode reads as history here, not a lost open ask."""
    seen = {}
    for r in records:
        if r.get("act") in _MERGE_ACTS and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None:
                seen[num] = (r.get("id"), _num(r.get("pr")))
    lines = []
    for num in sorted(seen):
        iid, pr = seen[num]
        tag = f" ({iid})" if iid else ""
        pr_bit = f" · PR {_pr_link(repo, pr)}" if pr is not None else ""
        note = " · parked earlier, later merged" if num in parked_earlier else ""
        lines.append(f"- #{num}{tag} — {_issue_link(repo, num)}{pr_bit}{note}")
    return lines


def _parked(records, window_start, resolved=frozenset()):
    """Open asks only. A parked issue whose final outcome was a later merge (`resolved`, from
    _reconciled_parks) is dropped here — it renders once under Merged, never as a second open ask."""
    seen = {}
    for r in records:
        if r.get("act") == "park" and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None and num not in resolved:
                seen[num] = (r.get("id"), bool(r.get("needs_william")), r.get("memo"))
    lines = []
    for num in sorted(seen):
        iid, needs, memo = seen[num]
        tag = f" ({iid})" if iid else ""
        who = "**needs-owner**" if needs else "parked"
        lines.append(f"- #{num}{tag} {who} — {memo if isinstance(memo, str) else '(no memo)'}")
    return lines


def _bounces(records, window_start):
    seen = {}
    for r in records:
        if r.get("act") == "bounce" and _ok(r) and _in_window(r, window_start):
            num = _num(r.get("num"))
            if num is not None:
                seen[num] = (r.get("id"), r.get("memo"))
    lines = []
    for num in sorted(seen):
        iid, memo = seen[num]
        tag = f" ({iid})" if iid else ""
        lines.append(f"- #{num}{tag} — {memo if isinstance(memo, str) else '(no memo)'}")
    return lines


def _regenerations(records, window_start):
    """Conflict regenerations within the last 7 days — the §4.2 tuning metric (climbing ⇒ tighten
    affinity / drop lanes; always zero ⇒ loosen)."""
    lines = []
    for r in sorted(records, key=lambda x: _ts(x) or 0):
        if r.get("act") == "regenerate" and _ok(r):
            ts = _ts(r)
            if ts is None or ts < window_start:
                continue
            num = _num(r.get("num"))
            branch = r.get("new_branch")
            conflicts = _count(r.get("conflicts"))
            tag = f" ({r.get('id')})" if r.get("id") else ""
            where = f" → rebuilt on `{branch}`" if isinstance(branch, str) and branch else ""
            lines.append(f"- #{num}{tag}{where} (conflict #{conflicts})")
    return lines


def _wanders(records, window_start):
    """PRs whose actual diff touched areas the issue didn't declare in `touches:` — deduped by
    issue number. gate-derived actions (merge/hold) carry the `wander` flag."""
    nums = {}
    for r in records:
        if r.get("wander"):
            ts = _ts(r)
            if ts is not None and ts < window_start:
                continue
            num = _num(r.get("num"))
            if num is not None:
                nums[num] = r.get("id")
    lines = []
    for num in sorted(nums):
        tag = f" ({nums[num]})" if nums[num] else ""
        lines.append(f"- #{num}{tag} — actual diff wandered beyond its declared `touches:`")
    return lines


def _gate_health(records, window_start, ledger, config):
    nightlies = [r for r in records if r.get("act") == "nightly"
                 and (_ts(r) is None or _ts(r) >= window_start)]
    quarantine = _dict(config).get("qa", {})
    quarantine = quarantine.get("quarantine") if isinstance(quarantine, dict) else None
    q_size = len(quarantine) if isinstance(quarantine, list) else 0
    accepted = len(ledger) if isinstance(ledger, dict) else 0

    lines = []
    if nightlies:
        latest = max(nightlies, key=lambda x: _ts(x) or 0)
        total = len(nightlies)
        # `is True` throughout: a corrupt journal line ("green": "false", a truthy string) must
        # never read as green, and a wrong-typed parse_error must not be trusted (Codex R2 M1).
        green = sum(1 for r in nightlies if r.get("green") is True and r.get("parse_error") is not True)
        date = latest.get("date") or "?"
        if latest.get("parse_error") is True:
            # a broken results file is NEVER a silent green — it is an honest failure line here
            # (+ the push the nightly sent); merges were NOT auto-verified this run.
            lines.append(f"- Nightly ({date}): could not parse results — dev not auto-verified; "
                         "see the nightly log.")
        elif latest.get("green") is True:
            lines.append(f"- Nightly ({date}): green.")
        elif latest.get("green") is False:
            persistent = _count(latest.get("persistent"))
            filed = latest.get("filed")
            fk = len(filed) if isinstance(filed, list) else _count(filed)
            lines.append(f"- Nightly ({date}): {persistent} persistent failure(s), "
                         f"filed {fk} fix issue(s).")
        else:
            # green is neither True nor False (missing/wrong-typed) and it's not a clean parse
            # error: a corrupt record — say so, never imply green.
            lines.append(f"- Nightly ({date}): result unclear (corrupt record) — dev not "
                         "auto-verified; see the nightly log.")
        lines.append(f"- {green}/{total} green over the last 7 nights; "
                     f"flakes last run: {_count(latest.get('flakes'))}.")
    else:
        lines.append("- Nightly: no runs recorded in the last 7 days.")
    lines.append(f"- Quarantine: {q_size} test(s). Accepted known failures: {accepted}.")
    return lines


def _watchdog(records, window_start):
    """Unattended-debugger activity (issue #66): every watchdog LAUNCH — verified or failed —
    must reach the owner's morning surface. Notified/stood-down episodes stay in the journal
    only: nothing ultimately happened, and the summary's quiet claim must stay honest."""
    lines = []
    for r in records:
        if r.get("act") != "watchdog" or not _in_window(r, window_start):
            continue
        sigs = ", ".join(s for s in (r.get("signals") or []) if isinstance(s, str)) \
            or "(signal unrecorded)"
        if r.get("outcome") == "launched":
            lines.append(f"- Launched unattended sl-debugger session {r.get('id')} — signals: "
                         f"{sigs}; authority: {r.get('authority')}. Its memo is in this "
                         "reports/ folder.")
        elif r.get("outcome") == "launch_failed":
            lines.append(f"- Launch of unattended sl-debugger session {r.get('id')} FAILED "
                         f"(rc={r.get('rc')}) — signals: {sigs}. The loop needed attention "
                         "overnight and the fallback could not start.")
    return lines


def _freeze(view):
    frozen = view.get("frozen")
    if isinstance(frozen, dict) and frozen:
        reason = frozen.get("reason") or "(reason unrecorded)"
        return [f"Merges are **FROZEN** — {reason}. Building continues; this is the safe idle state."]
    return ["Merges flowing."]


def _usage_queue(view):
    lines = []
    usage = view.get("usage")
    if isinstance(usage, dict) and usage:
        pct = usage.get("pct")
        lines.append(f"- Usage: {pct}% of the window used." if isinstance(pct, (int, float))
                     and not isinstance(pct, bool) else "- Usage: captured.")
    else:
        lines.append("- Usage: (not captured this cycle).")
    queue = view.get("queue")
    queue = [q for q in queue if isinstance(q, dict)] if isinstance(queue, list) else []
    if queue:
        nxt = queue[0]
        nxt_num = _num(nxt.get("num"))
        nxt_title = nxt.get("title") if isinstance(nxt.get("title"), str) else ""
        head = f"#{nxt_num} {nxt_title}".strip() if nxt_num is not None else nxt_title or "(next)"
        lines.append(f"- Queue depth: {len(queue)} waiting; next up: {head}.")
    else:
        lines.append("- Queue empty.")
    return lines


def _engine_drift(view):
    """A one-line installed-engine publish-drift nudge (issue #39), or None. The runner/CLI
    pre-computes the drift (git lives in the impure assembler; this module stays pure) and hands it
    in via view['engine_drift'] — a stack_doctor.engine_drift() dict. Rendered ONLY when the
    installed engine is BEHIND; every other state (in sync, skipped, an unmeasurable anomaly) stays
    silent here — the doctor is where those surface. Reinforces, never undercuts, the publish gate:
    the fix is a manual republish, and the line says so."""
    d = view.get("engine_drift")
    if not isinstance(d, dict) or d.get("status") != "behind":
        return None
    n = d.get("behind")
    if not (isinstance(n, int) and not isinstance(n, bool) and n > 0):
        return None
    ref = d.get("ref")
    ref = ref if isinstance(ref, str) and ref.strip() else "main"
    unit = "commit" if n == 1 else "commits"
    return (f"**Installed engine {n} {unit} behind {ref}** — merged engine fixes are live only "
            "after you republish through the gated `bin/install.sh` (publishing stays manual; "
            "republish when convenient).")


def _section(title, lines, empty="None."):
    body = "\n".join(lines) if lines else empty
    return f"## {title}\n{body}\n"


# --------------------------- the report ---------------------------

def morning(journal_records, gh_view, ledger, config):
    """Render the morning report markdown. Args:
      journal_records  list of journal.read() dicts (the overnight action log).
      gh_view          the live snapshot the runner/CLI assembles for the report:
                         {"date": "YYYY-MM-DD", "now": epoch (window reference; default: latest
                          journaled ts), "frozen": merges_frozen.json dict|None,
                          "queue": [{"num","title"}, …] ready issues, "usage": usage dict|None}
      ledger           the known-failure ledger dict {fingerprint: {...}} (accepted-failure count).
      config           the per-repo config (repo for links, qa.quarantine size).
    Never raises; every arg is coerced to a safe empty shape."""
    records = _records(journal_records)
    view = _dict(gh_view)
    cfg = _dict(config)
    repo = _repo(cfg)
    now = _reference_now(view, records)
    week_start = now - WEEK_SECONDS         # the 7-day trend window (regenerations, gate health)
    overnight_start = _overnight_start(records, now)   # since the last report (overnight sections)
    date = view.get("date")
    date = date if isinstance(date, str) and date.strip() else "(date unknown)"

    # Reconcile park records against final outcomes (#37): a park that later merged this window is
    # resolved, so it leaves the open-ask Parked section and is annotated on its Merged line.
    resolved_parks = _reconciled_parks(records, overnight_start)
    merged = _merged(records, repo, overnight_start, resolved_parks)
    parked = _parked(records, overnight_start, resolved_parks)
    bounces = _bounces(records, overnight_start)
    regens = _regenerations(records, week_start)
    wanders = _wanders(records, overnight_start)
    watchdog = _watchdog(records, overnight_start)
    frozen = isinstance(view.get("frozen"), dict) and bool(view.get("frozen"))
    queue = [q for q in view.get("queue") if isinstance(q, dict)] if isinstance(view.get("queue"), list) else []

    # A routine (green) nightly is the system working, not activity that needs William — and one
    # runs EVERY night, so counting it here would mean no night is ever quiet. A RED nightly shows
    # up as `frozen` instead, which does break quiet. An unattended debugger LAUNCH (or a launch
    # that failed) always breaks quiet — the owner must never coffee past one (issue #66).
    quiet = not any((merged, parked, bounces, regens, wanders, watchdog, queue, frozen))
    summary = ("Nothing happened overnight — queue empty." if quiet else
               f"{len(merged)} merged · {len(parked)} parked/needs-owner · "
               f"{len(bounces)} bounce(s) · {len(regens)} regen(s) · queue: {len(queue)}.")

    parts = [
        f"# superlooper morning report — {date}\n",
        f"{summary}\n",
    ]
    # The publish-drift nudge sits AFTER the summary tally so it never hijacks the push body (the
    # first non-title, non-blank line). Drift is a standing condition, not overnight activity, so it
    # is rendered independently of `quiet` — a quiet night with drift still reads "nothing happened".
    drift = _engine_drift(view)
    if drift:
        parts.append(f"{drift}\n")
    parts += [
        _section("Merged", merged, "Nothing merged."),
        _section("Parked / needs-owner", parked),
        _section("Bounces", bounces),
        _section("Conflict regenerations (last 7 days)", regens),
        _section("Wanders", wanders),
        _section("Unattended debugger", watchdog, "None — the watchdog launched nothing."),
        _section("Gate health", _gate_health(records, week_start, ledger, cfg)),
        "## Freeze state\n" + "\n".join(_freeze(view)) + "\n",
        _section("Usage / queue", _usage_queue(view)),
    ]
    return "\n".join(parts)


# --------------------------- promotion evidence (Gate 2) ---------------------------

def _fp(failure):
    """A failure's content fingerprint (the gate/ledger scheme) — imported lazily to keep this
    module free of the gate import unless a promotion report actually needs it."""
    import gate
    f = failure if isinstance(failure, dict) else {}
    return gate.fix_issue_fingerprint(f.get("test_id"), f.get("text"))


def _suite_section(suite, ledger):
    suite = _dict(suite)
    ledger = ledger if isinstance(ledger, dict) else {}
    if not suite.get("ok"):
        # never a silent "all clear": if we could not parse the suite, say so plainly.
        return ["Could not parse the suite results — this report cannot show failures; "
                "re-run the suite before deciding."]
    failures = [f for f in suite.get("failures") if isinstance(f, dict)] \
        if isinstance(suite.get("failures"), list) else []
    new, accepted = [], 0
    for f in failures:
        fp = _fp(f)
        if fp in ledger:
            accepted += 1                       # already accepted -> folded away (one approval, ever)
        else:
            new.append((f, fp))
    lines = []
    if new:
        lines.append(f"**{len(new)} NEW failure(s)** — not in the known-failure ledger:")
        for f, fp in new:
            tid = f.get("test_id") or "(unknown test)"
            first = (f.get("text") or "").strip().splitlines()
            detail = f" — {first[0]}" if first else ""
            # the fingerprint rides on the line so William can copy it straight into accept-failure
            lines.append(f"- {tid}{detail}  (fingerprint: `{fp}`)")
    else:
        lines.append("No new failures (nothing outside the known-failure ledger).")
    lines.append(f"\n{accepted} known failure(s) folded away (accepted in the ledger — "
                 "each approved once, by content).")
    return lines


def _merges_section(compare):
    c = _dict(compare)
    prod = c.get("prod_branch")
    dev = c.get("dev_branch") if isinstance(c.get("dev_branch"), str) else "dev"
    if not (isinstance(prod, str) and prod.strip()):
        return ["No prod branch configured — this repo promotes by its own checklist "
                "(`prod_branch` is null in .superlooper/config.json). Nothing to diff here."]
    result = c.get("result")
    if not isinstance(result, dict) or not result:
        return [f"Could not read the `{prod}...{dev}` comparison (GitHub unreadable) — "
                "check by hand before promoting."]
    ahead = result.get("ahead_by")
    total = result.get("total_commits")
    n = ahead if isinstance(ahead, int) and not isinstance(ahead, bool) else total
    n = n if isinstance(n, int) and not isinstance(n, bool) else "?"
    return [f"`{dev}` is **{n} commit(s)** ahead of `{prod}` since the last promotion."]


def _open_issues_section(open_issues, repo):
    items = [i for i in open_issues if isinstance(i, dict)] if isinstance(open_issues, list) else []
    if not items:
        return ["No open issues."]
    lines = []
    for i in sorted(items, key=lambda x: x.get("num") if isinstance(x.get("num"), int) else 1 << 30):
        num = _num(i.get("num"))
        title = i.get("title") if isinstance(i.get("title"), str) else ""
        labels = i.get("labels")
        lbl = f"  [{', '.join(labels)}]" if isinstance(labels, list) and labels else ""
        head = f"- #{num} {title}".rstrip() if num is not None else f"- {title}"
        lines.append(f"{head}{lbl}")
    return lines


def promotion(date, suite, ledger, compare, open_issues, config):
    """Render the dev->prod promotion EVIDENCE report (spec §4.6 Gate 2). Args:
      date         "YYYY-MM-DD".
      suite        {"ok": bool, "failures": [{test_id, text}], "source": str} — a fresh suite run
                   or the latest nightly's parsed results.
      ledger       accepted-failure map (folds already-accepted failures away).
      compare      {"prod_branch": str|None, "dev_branch": str, "result": gh.compare dict|None}.
      open_issues  [{num, title, labels}] open-issue summary.
      config       per-repo config (repo for links).

    EVIDENCE ONLY — there is deliberately NO pass/fail verdict and NO must-pass-to-promote logic
    anywhere; William decides. Never raises (every arg coerced)."""
    cfg = _dict(config)
    repo = _repo(cfg)
    date = date if isinstance(date, str) and date.strip() else "(date unknown)"
    src = _dict(suite).get("source")
    src = src if isinstance(src, str) and src.strip() else "the suite"
    parts = [
        f"# superlooper promotion evidence — {date}\n",
        "**Evidence only — no pass/fail verdict.** Promotion of dev→prod is your judgment "
        "(Gate 2); this report gathers the evidence, it does not decide.\n",
        _section(f"Suite results ({src})", _suite_section(suite, ledger)),
        _section("Merges since last promotion", _merges_section(compare)),
        _section("Open issues", _open_issues_section(open_issues, repo)),
        "## Accepting a failure\n"
        "A failure you judge non-blocking is accepted ONCE, by content, and never re-blocks:\n"
        "`superlooper accept-failure <fingerprint> --note \"…\"` "
        "(the fingerprint prints beside each new failure in future runs).\n",
    ]
    return "\n".join(parts)
