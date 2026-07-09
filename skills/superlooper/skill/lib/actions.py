"""The runner's brain as data: decide() maps one tick's full view of the world to an ordered
list of actions. PURE — no gh, no subprocess, no disk, no clock reads — so the entire spec-§5
failure model is a unit-test scenario table (tests/test_actions.py) and runner.py is a dumb
executor of what this returns.

Design commitments (all bought in prior runs, all tested):

  * STATE-driven, not event-driven: decisions derive from GitHub views + disk markers +
    loopstate, so a restarted runner fed a COLD state reconstructs every in-flight decision
    (spec §5 "runner death: restart rebuilds from GitHub + disk"). Events (events.py) only
    add the edge-triggered liveness tiers (idle/frozen), whose response is a safe peek/ladder.
  * FAIL CLOSED on wrong-typed input: every view field that isn't the expected shape lands on
    the safe action (do nothing / wait / park-to-William), never an exception into the tick and
    never a trusting default. The gh view is stale-unless-explicitly-fresh: gate, launch, and
    orphan decisions all require `gh_view["stale"] is False`.
  * No mutation of any input, no module-level mutable state: same inputs -> same output, twice.
  * NOTIFY IS A STANDING RULE (owner directive): every transition to parked/needs-william,
    every freeze, and every new ALERT emits {"act": "notify"} in the same action list. The
    scenario table asserts this per scenario.
  * Label mechanics are runner-side only (cross-review C2): bounce/park/reclaim/relabel actions
    carry the label payloads; no worker is ever asked to move a label.

The view contract (assembled by runner.py each tick):

  now            epoch seconds.
  config         the validated per-repo config (config.load()).
  usage          usage.fetch_claude_usage() result + {"last_ok_at": epoch of the last SUCCESSFUL
                 fetch (None if never), "first_attempt_at": epoch}. decide computes staleness:
                 age > USAGE_STALE_SECONDS launches nothing (fail closed, RC-USAGEFAILOPEN);
                 age > USAGE_ALERT_SECONDS raises the ALERT.
  parsed_issues  issues.parse_issue() dicts for the union of open `agent-ready` and open
                 `in-progress` issues (deduped). Wrong-typed nums are skipped here — an issue
                 that can't be identified can't be safely acted on.
  lane_state     [{"id", "touches", "type"?}] for currently occupied lanes — lane_state_from()
                 builds it.
  events         this tick's events.detect_events() output.
  disk           {"issues_state": loopstate dict, "blocked": {id: text}, "reports": {id: text},
                  "answers": {id: text}, "exited": {id: marker-text}, "frozen": dict|None,
                  "alert": dict|None, "live_lock_ids": iterable of ids with a LIVE worker lock,
                  "filed_fingerprints": {fingerprint: issue_num},
                  "local_date": "YYYY-MM-DD", "local_hhmm": "HH:MM",
                  "last_report_date": str|None}
  gh_view        {"stale": bool (fresh ONLY when exactly False), "consecutive_failures": int,
                  "closed_nums": set, "prs": {id: pr_view+comments; {} = fetched, none exists;
                  KEY ABSENT = not fetched yet, so WAIT}, "issue_comments": {id: [...]},
                  "dev_checks": [{name,status,conclusion}] (key absent = not fetched)}

Action vocabulary (the executor contract, one journal record each):
  launch, hire_answerer, deliver_answer, bounce, recover(tier=idle|frozen|exited), gate,
  merge, update, nudge, hold, park, regenerate, resolve_conflict, close_investigate,
  reclaim, relabel, freeze, unfreeze, file_fix_issue, alert, clear_alert, morning_report,
  notify. Safety actions (alert/freeze/unfreeze) come first; launches come LAST.
"""
import math

import brief
import events as events_mod
import gate
import scheduler

# Staleness / cap constants. The caps that PARK are deliberately small (park is cheap and safe:
# one William-touch re-releases); the thresholds that ALERT sit past the caps (a doom loop must
# be loud, but only a real one).
USAGE_STALE_SECONDS = 300          # no fresh usage for 5 min -> launch nothing (fail closed)
USAGE_ALERT_SECONDS = 3600         # no fresh usage for 1 h  -> ALERT (plan Task 10)
GH_ALERT_FAILURES = 10             # consecutive failed poll cycles (~15 min at 90 s) -> ALERT
ANSWERER_TIMEOUT_SECONDS = 900     # the answerer's 15-min freeze tier = its timeout
RECOVER_RETRY_SECONDS = 600        # frozen-session recovery ladder re-fires at most every 10 min
LAUNCH_FAILURE_CAP = 2             # launch never delivered twice -> park (RC-LAUNCHVERIFY x2)
ANSWERER_FAILURE_CAP = 2           # answerer hire failed twice -> park the issue
DELIVERY_FAILURE_CAP = 3           # answer would not deliver to the pane -> park the issue
UPDATE_ERROR_ALERT = 4             # persistent merge-update infra errors -> ALERT (never regenerate)
RUNAWAY_THRESHOLD = 4              # retries far past the cap -> ALERT (events.retry_runaway)

# The red-nightly standing rule's EXACT label set (spec §4.4, owner-defined 2026-07-02). The
# distinct `auto-approved:nightly-red` label is what makes this auto-approval auditable as
# standing-rule work, not an agent applying William's word. Do not add, drop, or reorder.
FIX_ISSUE_LABELS = ["type:diagnose-and-fix", "agent-ready", "auto-approved:nightly-red", "expedite"]

# Statuses that occupy a lane (a blocked/frozen/exited session still owns its worktree+branch)
# vs statuses from which a (re)launch is legitimate. gating/holding hold NO lane: the build is
# done and only merge mechanics remain, so a lane frees the moment the report lands.
INFLIGHT_STATUSES = {"running", "blocked", "frozen", "exited"}
TERMINAL_STATUSES = {"merged", "parked", "needs_william", "bounced"}
RELAUNCHABLE_STATUSES = {None, "ready", "parked", "needs_william", "bounced"}
# The park-family terminal statuses a FRESH `agent-ready` re-releases. Re-approval is William's
# word again (spec §2: the label records his word, it is never the decision), so it is a fresh
# cap: the runner zeroes the per-issue attempt counters and re-releases to `ready` (see the
# `reapprove` action). `merged` is deliberately excluded — merged work is truly done; a stray
# label on it must never resurrect and rebuild it.
REAPPROVAL_STATUSES = {"parked", "needs_william", "bounced"}

NUDGE_MESSAGES = {
    "sections": "Your report is missing required sections (or they carry no real prose). "
                "Rewrite the report with substantive text under every required H2, then finish "
                "again — the runner checks mechanically.",
    "review": "The gate found no review evidence. Get a fresh-agent review of your diff and "
              "post its verdict as a PR comment BEGINNING `<!-- superlooper-review -->` "
              "(what was reviewed + P0/P1 outcome). The runner will not merge without it.",
    "checks": "A required check failed on your PR. Investigate the failure, fix it, and push — "
              "the gate re-runs automatically.",
    "investigation": "Post your root-cause report as an issue comment BEGINNING "
                     "`<!-- superlooper-investigation -->` — the runner closes the parent only "
                     "when that marker comment exists.",
}


def _real(x):
    """A usable number: int/float, not bool, finite."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _count(x, default=0):
    """A usable counter: a real int (bool excluded), else the default. Wrong-typed counters are
    the fail-OPEN defect class — callers decide whether default or park is the safe landing."""
    return x if type(x) is int else default


def _counter(ist, key):
    """(value, corrupt) for a persisted cap counter. MISSING legitimately means 0; a PRESENT
    non-int value — including an explicit null, which nothing in this system ever writes —
    is corruption, and the caller must land on the safe action (park/alert), never re-allow
    the capped action by reading it as 0 (Codex cross-review rounds 1+2 — the
    fail-OPEN-on-wrong-TYPED defect class)."""
    if not isinstance(ist, dict) or key not in ist:
        return 0, False
    v = ist[key]
    if type(v) is int:
        return v, False
    return 0, True


def _iid_num(iid):
    """i<N> -> N, else None (a loopstate key that isn't an issue id is corruption, skipped)."""
    if isinstance(iid, str) and iid.startswith("i") and iid[1:].isdigit():
        return int(iid[1:])
    return None


def _sorted_ids(ids):
    return sorted(ids, key=_iid_num)


def _dget(d, key, want):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, want) else want()


def lane_state_from(issues_state):
    """[{"id", "touches", "type"?}] for every issue whose status occupies a lane, sorted by issue
    number (deterministic). Pure; wrong-typed state or entries degrade to no lanes / no touches."""
    issues = _dget(issues_state, "issues", dict)
    out = []
    for iid in _sorted_ids(k for k in issues if _iid_num(k) is not None):
        ist = issues.get(iid)
        if isinstance(ist, dict) and ist.get("status") in INFLIGHT_STATUSES:
            touches = ist.get("declared_touches")
            touches = [t for t in touches if isinstance(t, str)] if isinstance(touches, list) else []
            lane = {"id": iid, "touches": touches}
            itype = ist.get("type")
            if isinstance(itype, str) and itype:
                lane["type"] = itype
            out.append(lane)
    return out


def _failing_required(dev_checks, required):
    """(name, conclusion) of the first REQUIRED check reporting a failing state, in the config's
    declared order (deterministic), else None."""
    if not isinstance(dev_checks, list) or not isinstance(required, list):
        return None
    by_name = {}
    for c in dev_checks:
        if isinstance(c, dict) and isinstance(c.get("name") or c.get("context"), str):
            by_name.setdefault(c.get("name") or c.get("context"), []).append(c)
    for req in required:
        for c in by_name.get(req, []):
            state = c.get("conclusion") or c.get("state")
            if isinstance(state, str) and state.upper() in gate._CHECK_FAIL:
                return (req, state)
    return None


def dev_fingerprint(dev_checks, required):
    """The durable identity of a red-dev breakage (file the fix issue ONCE per distinct failure,
    L7: fingerprint content, never a commit). Built from the first failing required check's
    name + conclusion — the richest content branch_checks exposes; the nightly (Task 12) adds
    failure text through the same gate.fix_issue_fingerprint."""
    failing = _failing_required(dev_checks, required)
    name, concl = failing if failing else ("dev", "")
    return gate.fix_issue_fingerprint(name, concl)


def _fix_issue(dev_branch, name, conclusion, fingerprint):
    title = f"Restore green: required check '{name}' is red on {dev_branch}"
    body = (
        f"## Goal\n"
        f"The dev mainline `{dev_branch}` has a red required check: `{name}` ({conclusion}).\n"
        f"Diagnose and fix whatever broke it. This issue is scoped STRICTLY to restoring green —\n"
        f"no opportunistic improvements (spec §4.4 red-nightly standing rule).\n"
        f"Failure fingerprint: `{fingerprint}` (auto-filed once per distinct breakage).\n\n"
        f"## Definition of done\n"
        f"- [ ] required check `{name}` is green on `{dev_branch}`\n\n"
        f"## Boundaries\n"
        f"Only the minimal change that restores green. Anything larger becomes a new issue for\n"
        f"William to approve. Merges are frozen until dev is green again.\n\n"
        f"## Loop metadata\n"
        f"touches:\n"
    )
    return {"act": "file_fix_issue", "fingerprint": fingerprint, "title": title, "body": body,
            "labels": list(FIX_ISSUE_LABELS)}


def decide(now, config, usage, parsed_issues, lane_state, events, disk, gh_view):
    """One tick's view of the world -> the ordered action list. See the module docstring for
    the full view and action contracts."""
    if not _real(now):
        return []                      # a tick without a clock decides nothing (fail closed)

    # ---- defensive coercion of every input (wrong-typed -> safe empty, never a raise) ----
    cfg = config if isinstance(config, dict) else {}
    session = _dget(cfg, "session", dict)
    retry_cap = _count(session.get("retry_cap"), 2)
    dev_branch = cfg.get("dev_branch") if isinstance(cfg.get("dev_branch"), str) else "main"

    usage_view = usage if isinstance(usage, dict) else {}
    last_ok = usage_view.get("last_ok_at")
    usage_age = (now - last_ok) if _real(last_ok) else math.inf
    usage_sched = dict(usage_view)
    usage_sched["stale"] = bool(usage_view.get("stale")) or usage_age > USAGE_STALE_SECONDS
    usage_launchable = scheduler.usage_ok(usage_sched)

    plist = parsed_issues if isinstance(parsed_issues, list) else []
    parsed_by_id = {}
    for p in plist:
        num = p.get("num") if isinstance(p, dict) else None
        if type(num) is int and num > 0:               # bool/str/None num: unidentifiable, skip
            parsed_by_id[f"i{num}"] = p

    lanes_in = [l for l in lane_state if isinstance(l, dict)] if isinstance(lane_state, list) else []
    evs = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    idle_ids = {e.get("id") for e in evs if e.get("type") == "session_idle"}
    frozen_ids = {e.get("id") for e in evs if e.get("type") == "frozen"}

    dsk = disk if isinstance(disk, dict) else {}
    issues_state = _dget(dsk, "issues_state", dict)
    ist_map = _dget(issues_state, "issues", dict)
    blocked = _dget(dsk, "blocked", dict)
    reports = _dget(dsk, "reports", dict)
    answers = _dget(dsk, "answers", dict)
    exited = _dget(dsk, "exited", dict)
    frozen = dsk.get("frozen") if isinstance(dsk.get("frozen"), dict) else None
    alert_on_disk = dsk.get("alert") if isinstance(dsk.get("alert"), dict) else None
    raw_locks = dsk.get("live_lock_ids")
    live_locks = set(raw_locks) if isinstance(raw_locks, (set, frozenset, list, tuple)) else set()
    filed = _dget(dsk, "filed_fingerprints", dict)

    gv = gh_view if isinstance(gh_view, dict) else {}
    gh_stale = gv.get("stale") is not False            # fresh ONLY when explicitly False
    prs = _dget(gv, "prs", dict)
    issue_comments = _dget(gv, "issue_comments", dict)
    raw_closed = gv.get("closed_nums")
    closed_nums = set(raw_closed) if isinstance(raw_closed, (set, frozenset, list, tuple)) else set()

    # answerer bookkeeping: an active record per issue; the next id must never collide with an
    # existing one even if the counter is corrupt (scan wins over a wrong-typed counter).
    answerers = _dget(issues_state, "answerers", dict)
    active_answerer = {}
    max_aid = 0
    for aid, rec in answerers.items():
        if isinstance(aid, str) and aid.startswith("a") and aid[1:].isdigit():
            max_aid = max(max_aid, int(aid[1:]))
        if isinstance(rec, dict) and isinstance(rec.get("for"), str):
            active_answerer[rec["for"]] = (aid, rec)
    next_aid = max(_count(issues_state.get("next_answerer"), 1), max_aid + 1)

    out = []
    parked_now = set()
    reapproved_now = set()

    def notify(title, body):
        out.append({"act": "notify", "title": title, "body": body})

    def park(iid, num, memo, needs_william=False):
        parked_now.add(iid)
        out.append({"act": "park", "id": iid, "num": num,
                    "needs_william": needs_william, "memo": memo})
        who = "needs-william" if needs_william else "parked"
        notify(f"superlooper: {iid} {who}", memo)

    def ist_of(iid):
        v = ist_map.get(iid)
        return v if isinstance(v, dict) else {}

    # ================= A. alerts (safety first, before any work) =================
    reasons = []
    if _count(gv.get("consecutive_failures")) >= GH_ALERT_FAILURES:
        reasons.append("gh_unreachable")
    reasons += [f"launch_runaway:{iid}"
                for iid in sorted(events_mod.retry_runaway(issues_state, RUNAWAY_THRESHOLD))]
    if usage_age > USAGE_ALERT_SECONDS:
        reasons.append("usage_stale")
    for iid in _sorted_ids(k for k in ist_map if _iid_num(k) is not None):
        errs, corrupt = _counter(ist_of(iid), "update_errors")
        if corrupt or errs >= UPDATE_ERROR_ALERT:
            reasons.append(f"update_errors:{iid}")     # a corrupt counter is alert-worthy too
    reasons.sort()
    if reasons:
        existing = alert_on_disk.get("reasons") if alert_on_disk else None
        if existing != reasons:
            out.append({"act": "alert", "reasons": reasons})
            notify("superlooper ALERT", "; ".join(reasons))
    elif alert_on_disk:
        out.append({"act": "clear_alert"})

    # ================= B. dev mainline: freeze / fix-forward / unfreeze =================
    # Requires a FRESH, PRESENT dev-check view: no data never unfreezes and never freezes —
    # the current freeze state simply persists (frozen-but-building is the safe idle state).
    dev_checks = gv.get("dev_checks")
    if not gh_stale and isinstance(dev_checks, list):
        dev_state = gate.required_checks_state(dev_checks, cfg.get("required_checks"))
        if dev_state == "fail":
            failing = _failing_required(dev_checks, cfg.get("required_checks"))
            name, concl = failing if failing else ("dev", "")
            fp = gate.fix_issue_fingerprint(name, concl)
            if not frozen:
                out.append({"act": "freeze", "reason": f"dev checks red: {name} ({concl})",
                            "fingerprint": fp})
                notify("superlooper: merges frozen",
                       f"required check '{name}' is red on {dev_branch}; fix-forward filed, "
                       "building continues")
            if fp not in filed:
                out.append(_fix_issue(dev_branch, name, concl, fp))
        elif dev_state == "green" and frozen:
            out.append({"act": "unfreeze"})

    # ================= C. morning report (fires once per local day) =================
    local_date = dsk.get("local_date")
    local_hhmm = dsk.get("local_hhmm")
    report_time = cfg.get("report_time") if isinstance(cfg.get("report_time"), str) else "08:45"
    if (isinstance(local_date, str) and isinstance(local_hhmm, str)
            and local_hhmm >= report_time and local_date != dsk.get("last_report_date")):
        out.append({"act": "morning_report", "date": local_date})

    # ================= D. per-issue flows, in issue-number order =================
    all_ids = _sorted_ids({k for k in ist_map if _iid_num(k) is not None} | set(parsed_by_id))
    for iid in all_ids:
        ist = ist_of(iid)
        status = ist.get("status")
        if status in TERMINAL_STATUSES:
            # Re-approval (dry-run finding, 2026-07-04): a parked-on-cap issue stays filtered
            # from launches FOREVER — its at-cap counter persists across a re-added `agent-ready`
            # label. But re-approval IS a fresh cap. When a park-family issue carries a fresh
            # `agent-ready`, emit `reapprove`: the executor zeroes the attempt counters (journaling
            # the old ones) and re-releases to `ready`. It is the ONLY action for this issue this
            # tick — the phase-E launch waits one tick so it fires against the reset counters, never
            # the stale at-cap ones (see the `reapproved_now` guard below).
            if status in REAPPROVAL_STATUSES:
                p = parsed_by_id.get(iid)
                labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
                if "agent-ready" in labels:
                    out.append({"act": "reapprove", "id": iid, "num": _iid_num(iid)})
                    reapproved_now.add(iid)
            continue                                   # else re-release happens via labels (phase E)
        num = _iid_num(iid)
        p = parsed_by_id.get(iid)
        blocked_text = blocked.get(iid) if isinstance(blocked.get(iid), str) else None
        has_report = iid in reports
        has_exited = iid in exited
        retries = ist.get("retries", 0)

        # ---- recheck failure: an owner decision, checked before any gate re-run ----
        if ist.get("recheck_failed"):
            park(iid, num, "ship_recheck_cmd failed after the mechanical merge-update — "
                           "never coached around a fail-closed gate; William decides",
                 needs_william=True)
            continue

        # ---- finished: the ship gate owns this issue ----
        if has_report:
            if status not in ("gating", "holding"):
                out.append({"act": "gate", "id": iid})
            if gh_stale:
                continue
            itype = (p.get("type") if isinstance(p, dict) else None) or ist.get("type")
            if itype == "investigate":
                if iid not in issue_comments:
                    continue                           # comments not fetched yet -> wait
                view_comments = issue_comments.get(iid)
                inv_done = gate.investigation_done(view_comments)
                pv = {}
            else:
                if iid not in prs:
                    continue                           # PR not fetched yet -> wait
                pv = prs.get(iid) if isinstance(prs.get(iid), dict) else {}
                inv_done = False
                if pv.get("state") == "MERGED":
                    # crash window (Codex round-1 C2): the merge landed but the runner died
                    # before settling local state/labels — ABSORB the merged fact
                    # (idempotent), never wedge in gate-wait with a stuck in-progress label.
                    out.append({"act": "absorb_merged", "id": iid, "num": num})
                    continue

            update_result = ist.get("update_result")
            head = pv.get("headRefOid")
            if update_result is not None and isinstance(head, str) \
                    and ist.get("update_head_oid") != head:
                update_result = None                   # stale verdict for a previous head
            declared = (p.get("touches") if isinstance(p, dict) else None) \
                or ist.get("declared_touches") or []
            nudged = ist.get("nudged", [])
            conflicts = ist.get("conflicts", 0)
            inflight = {}
            for other in ist_map:
                oist = ist_of(other)
                if other != iid and oist.get("status") in INFLIGHT_STATUSES:
                    ot = oist.get("declared_touches")
                    inflight[other] = [t for t in ot if isinstance(t, str)] \
                        if isinstance(ot, list) else []

            g = gate.gate_decision(
                {"type": itype, "conflicts": conflicts, "nudged": nudged,
                 "declared_touches": declared, "update_result": update_result,
                 "investigation_done": inv_done},
                pv, reports.get(iid), cfg, bool(frozen), inflight)

            act, wander = g.get("action"), g.get("wander", False)
            if act == "merge":
                method = cfg.get("merge_method")
                method = method if method in ("squash", "merge", "rebase") else "squash"
                out.append({"act": "merge", "id": iid, "num": num, "pr": pv.get("number"),
                            "method": method, "wander": wander})
            elif act == "update":
                out.append({"act": "update", "id": iid, "num": num, "pr": pv.get("number"),
                            "head_oid": head, "wander": wander})
            elif act == "hold":
                if status != "holding":                # journal-once: the hold is an edge
                    h = {"act": "hold", "id": iid, "reason": g.get("reason"), "wander": wander}
                    if g.get("overlap_lane") is not None:
                        h["overlap_lane"] = g["overlap_lane"]
                    out.append(h)
            elif act == "nudge":
                key = g.get("nudge_key")
                out.append({"act": "nudge", "id": iid, "nudge_key": key,
                            "message": NUDGE_MESSAGES.get(key, g.get("reason", ""))})
            elif act == "park":
                park(iid, num, g.get("reason", "gate parked this issue"),
                     needs_william=bool(g.get("needs_william")))
            elif act == "regenerate":
                new_conflicts = _count(conflicts) + 1
                src = p if isinstance(p, dict) else {"num": num, "id": iid,
                                                     "title": ist.get("title", "")}
                out.append({"act": "regenerate", "id": iid, "num": num, "pr": pv.get("number"),
                            "new_branch": brief.branch_for(src, generation=new_conflicts),
                            "conflicts": new_conflicts, "wander": wander})
            elif act == "resolve_conflict":
                out.append({"act": "resolve_conflict", "id": iid, "num": num,
                            "pr": pv.get("number"), "wander": wander})
            elif act == "close_investigate":
                out.append({"act": "close_investigate", "id": iid, "num": num})
            # "wait" -> no action this tick
            continue

        # ---- blocked marker present (and no report): bounce or the answerer lifecycle ----
        if blocked_text is not None and not has_exited:
            if blocked_text.lstrip().startswith("BOUNCED:"):
                out.append({"act": "bounce", "id": iid, "num": num, "memo": blocked_text})
                notify(f"superlooper: {iid} bounced (needs-william)", blocked_text)
                continue
            aid_rec = active_answerer.get(iid)
            answer = answers.get(iid) if isinstance(answers.get(iid), str) else None
            if aid_rec and answer is not None:
                deliveries, corrupt = _counter(ist, "answer_delivery_failures")
                if corrupt or deliveries >= DELIVERY_FAILURE_CAP:
                    park(iid, num, f"the answer would not deliver to the session pane "
                                   f"({DELIVERY_FAILURE_CAP} attempts, or the attempt counter "
                                   f"is unreadable). question was: {blocked_text!r}")
                elif answer.lstrip().startswith("PARK:"):
                    park(iid, num, f"answerer escalated to William. question: "
                                   f"{blocked_text!r} — answer: {answer!r}",
                         needs_william=True)
                else:
                    out.append({"act": "deliver_answer", "id": iid,
                                "answerer_id": aid_rec[0], "text": answer})
            elif aid_rec:
                launched_at = aid_rec[1].get("launched_at")
                age = (now - launched_at) if _real(launched_at) else math.inf
                if age >= ANSWERER_TIMEOUT_SECONDS:
                    park(iid, num, f"answerer {aid_rec[0]} timed out after "
                                   f"{ANSWERER_TIMEOUT_SECONDS // 60} min. question was: "
                                   f"{blocked_text!r}")
            else:
                hires, corrupt = _counter(ist, "answerer_failures")
                if corrupt or hires >= ANSWERER_FAILURE_CAP:
                    park(iid, num, f"could not launch an answerer ({ANSWERER_FAILURE_CAP} "
                                   f"attempts, or the attempt counter is unreadable). "
                                   f"question was: {blocked_text!r}")
                else:
                    out.append({"act": "hire_answerer", "id": iid, "num": num,
                                "answerer_id": f"a{next_aid}", "question": blocked_text})
                    next_aid += 1
            continue

        # ---- liveness recovery: exited beats frozen beats idle ----
        if has_exited:
            if type(retries) is not int:               # corrupt counter -> to William, not a loop
                park(iid, num, "exited, and the retry counter is unreadable — parking")
            elif retries >= retry_cap:
                park(iid, num, f"exited and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking")
            elif usage_launchable:
                out.append({"act": "recover", "id": iid, "tier": "exited"})
            # no usage headroom -> the marker persists; relaunch resumes with the quota
            continue
        if iid in frozen_ids or status == "frozen":
            if type(retries) is not int:
                park(iid, num, "frozen, and the retry counter is unreadable — parking")
            elif retries >= retry_cap:
                park(iid, num, f"frozen and already relaunched {retries} times (cap "
                               f"{retry_cap}) — parking")
            else:
                last_rec = ist.get("last_recover_at")
                last_rec = last_rec if _real(last_rec) else 0
                if now - last_rec >= RECOVER_RETRY_SECONDS:
                    out.append({"act": "recover", "id": iid, "tier": "frozen"})
            continue
        if iid in idle_ids:
            out.append({"act": "recover", "id": iid, "tier": "idle"})
            continue

        # ---- label reconciliation + orphaned in-progress (restart rebuild) ----
        labels = p.get("labels") if isinstance(p, dict) and isinstance(p.get("labels"), list) else []
        if status in INFLIGHT_STATUSES or status in ("gating", "holding"):
            if "agent-ready" in labels:
                # the launch/park label move didn't land (gh blip): converge GitHub to reality
                out.append({"act": "relabel", "id": iid, "num": num,
                            "add": ["in-progress"], "remove": ["agent-ready"]})
            continue
        # status is None/"ready" from here on
        launch_fails, corrupt = _counter(ist, "launch_failures")
        if "agent-ready" in labels and (corrupt or launch_fails >= LAUNCH_FAILURE_CAP):
            park(iid, num, f"launch was never delivered ({LAUNCH_FAILURE_CAP} verified "
                           "attempts, or the attempt counter is unreadable) — is the launch "
                           "shim installed? (bin/install-launch-shim.sh)")
            continue
        if "in-progress" in labels and iid not in live_locks and not gh_stale and iid in prs:
            pv = prs.get(iid) if isinstance(prs.get(iid), dict) else {}
            branch = pv.get("headRefName")
            stamped = ist.get("branch")
            # An open PR resumes the orphan ONLY if it is this issue's ACTIVE branch: a
            # `superseded` label or a loopstate branch stamp that differs (a partially-executed
            # regenerate) means the PR is dead history — requeue, never resurrect it.
            resumable = (pv.get("number") and pv.get("state") == "OPEN"
                         and "superseded" not in gate._pr_labels(pv)
                         and (not isinstance(stamped, str) or not stamped.strip()
                              or stamped == branch))
            if resumable:
                touches = p.get("touches") if isinstance(p.get("touches"), list) else []
                out.append({"act": "launch", "id": iid, "num": num, "branch": branch,
                            "touches": touches, "soft_overlap": False, "orphan": True})
            elif pv.get("number") and pv.get("state") == "OPEN":
                out.append({"act": "reclaim", "id": iid, "num": num})
            elif pv.get("state") in ("MERGED", "CLOSED"):
                # stuck labels (spec §5): the work concluded outside the runner; absorb it
                out.append({"act": "relabel", "id": iid, "num": num,
                            "add": [], "remove": ["in-progress"]})
            elif not pv.get("number"):
                out.append({"act": "reclaim", "id": iid, "num": num})

    # ================= E. fresh launches (LAST: freed lanes wait one tick) =================
    if not gh_stale:
        candidates = []
        for iid in _sorted_ids(parsed_by_id):
            p = parsed_by_id[iid]
            labels = p.get("labels") if isinstance(p.get("labels"), list) else []
            ist = ist_of(iid)
            launch_fails, corrupt = _counter(ist, "launch_failures")
            if ("agent-ready" not in labels or "in-progress" in labels
                    or iid in parked_now
                    or iid in reapproved_now   # just re-released: launch next tick, reset counters
                    or ist.get("status") not in RELAUNCHABLE_STATUSES
                    or corrupt or launch_fails >= LAUNCH_FAILURE_CAP):
                continue
            candidates.append(dict(p, requeue_front=bool(ist.get("requeue_front"))))
        for sel in scheduler.launchable(candidates, lanes_in, cfg, usage_sched,
                                        closed_nums, bool(frozen)):
            iid = sel["id"]
            ist = ist_of(iid)
            branch = ist.get("branch")
            if not (isinstance(branch, str) and branch.strip()):
                branch = brief.branch_for(parsed_by_id[iid])
            out.append({"act": "launch", "id": iid, "num": sel["num"], "branch": branch,
                        "touches": sel["touches"], "soft_overlap": sel["soft_overlap"],
                        "orphan": False})
    return out
