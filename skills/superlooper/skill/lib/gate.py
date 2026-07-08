"""The mechanical ship gate (§C.4): pure decisions over views the runner assembles.

Every function here is a pure function of its inputs — no gh, no subprocess, no disk — so the
whole state machine is unit-testable and the runner (Task 10) is a thin executor of the actions
this module returns. Two failure disciplines run through everything, both bought in prior runs:

  * FAIL CLOSED on wrong-typed input: a corrupt report/rollup/counter must land on the safe
    action (wait / nudge / park — all of which merely defer to a human or a later tick), never
    on "merge" and never on an exception into the tick.
  * The runner never resolves conflicts, never force-pushes, never posts a status by hand, and
    never converts an owner-only decision into an autonomous one (constitution bright lines).
    Frozen-but-building is the safe idle state.
"""
import hashlib
import re

import config as _config   # pure sibling; used only for path_to_area in gate_decision

# The two marker-comment contracts (cross-review C1 + plan approval fix (a)). These strings are
# load-bearing: the brief (Task 7) instructs workers to post them, and THIS module is the only
# consumer — the fresh-agent-review standing rule verified mechanically, never LLM-remembered.
REVIEW_MARKER = "<!-- superlooper-review -->"
INVESTIGATION_MARKER = "<!-- superlooper-investigation -->"

# A required H2 section must carry at least this many NON-WHITESPACE characters of prose.
# Cross-review C3: a report whose headings exist but whose bodies are empty once looked
# "complete" to a headings-only check — empty headings must never merge.
SECTION_MIN_CHARS = 40

# Check-rollup folding (conclusion // state), departing from autocode's display-oriented fold
# in TWO fail-closed ways (the second from the Task-9 cross-review): CANCELLED/ACTION_REQUIRED
# count as FAIL for a REQUIRED check, and success is an EXPLICIT set — any state outside both
# sets (a value gh grows tomorrow, a wrong-typed entry) buckets to PENDING, never to green.
# NEUTRAL/SKIPPED count as satisfied, matching GitHub's own required-check semantics.
_CHECK_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
_CHECK_SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def report_sections_ok(report_text, required):
    """Every required H2 heading present AND carrying >= SECTION_MIN_CHARS non-whitespace chars
    of prose (cross-review C3). Wrong-typed report or required list -> False (fail closed);
    an EMPTY required list is vacuously ok (config defaults are non-empty; doctor owns refusing
    degenerate repo setups, cross-review C3)."""
    if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
        return False
    if not required:
        return True
    if not isinstance(report_text, str):
        return False
    sections = {}
    current = None
    for line in report_text.splitlines():
        s = line.strip()
        if s.startswith("## "):            # exactly H2: '### x' does not startswith '## '
            current = s[3:].strip()
            sections.setdefault(current, "")
        elif current is not None:
            sections[current] += line + "\n"
    return all(
        req in sections and len(re.sub(r"\s", "", sections[req])) >= SECTION_MIN_CHARS
        for req in required)


def _any_comment_begins(comments, marker):
    """True iff any comment's body BEGINS with `marker` (leading whitespace ignored — 'begins'
    is the contract; quoting the marker mid-text is not a verdict). Tolerates wrong-typed
    comment lists/entries: anything unreadable simply doesn't count (fail closed)."""
    if not isinstance(comments, list):
        return False
    for c in comments:
        body = c.get("body") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if isinstance(body, str) and body.lstrip().startswith(marker):
            return True
    return False


def review_evidence_ok(config, pr_comments):
    """§C.4 step 2b — the fresh-agent-review standing rule, verified MECHANICALLY: either the
    repo's own pipeline owns review (`ship_cmd` set — e.g. the eApp's diff-pinned
    review/local-gate) OR a fresh-agent verdict exists as a PR comment beginning REVIEW_MARKER."""
    ship_cmd = (config or {}).get("ship_cmd") if isinstance(config, dict) else None
    if isinstance(ship_cmd, str) and ship_cmd.strip():
        return True
    return _any_comment_begins(pr_comments, REVIEW_MARKER)


def investigation_done(issue_comments):
    """Cross-review C1: an investigation is complete iff its root-cause report exists as an
    issue comment beginning INVESTIGATION_MARKER. Zero child issues is legal — 'nothing to do'
    is a valid root cause — so the marker, not the children, is the completion signal."""
    return _any_comment_begins(issue_comments, INVESTIGATION_MARKER)


def _clean_areas(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _areas_overlap(a, b):
    """The kickoff's fixed wildcard contract: '*' (a path matching no declared area glob)
    overlaps everything, in either direction."""
    return bool(a) and bool(b) and bool(set(a) & set(b) or "*" in a or "*" in b)


def touch_verdict(declared, actual_areas, inflight):
    """§C.4 step 3. wander = the diff left the declared touches (actual ⊄ declared) — journaled
    and morning-reported, never blocking. Nothing declared -> no promise to break -> no wander
    (repos without touches_required let issues skip the declaration). overlap_lane = the first
    (sorted — deterministic) in-flight lane whose declared touches overlap the ACTUAL areas;
    the merge HOLDS until that lane resolves, because merging under a live overlapping lane
    invalidates that lane's base. A wrong-typed inflight view, non-string lane ids (mixed-type
    keys would break sorted()), and wrong-typed lane entries all degrade to skipped — lane ids
    are runner-constructed strings, so anything else is corruption, never a real lane."""
    decl = _clean_areas(declared)
    actual = _clean_areas(actual_areas)
    wander = bool(decl) and "*" not in decl and not set(actual) <= set(decl)
    overlap_lane = None
    lanes = inflight if isinstance(inflight, dict) else {}
    for lane in sorted(k for k in lanes if isinstance(k, str)):
        touches = _clean_areas(lanes.get(lane))
        if _areas_overlap(actual, touches):
            overlap_lane = lane
            break
    return {"wander": wander, "overlap_lane": overlap_lane}


def required_checks_state(status_rollup, required) -> str:
    """§C.4 step 5: fold the PR's check rollup down to the REQUIRED checks' joint state:
    'fail' (any required check failed — beats pending), 'pending' (any required check missing
    or still running, or the rollup/required list is unreadable — fail closed: WAIT, never
    merge on a half-read rollup), else 'green'. Rollup entries carry gh's two shapes: CheckRun
    (name/conclusion) and StatusContext (context/state). An EMPTY required list is vacuously
    green here; doctor fails hard on it at adopt time (cross-review C3)."""
    if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
        return "pending"
    if not required:
        return "green"
    rollup = status_rollup if isinstance(status_rollup, list) else []
    entries = {}
    for c in rollup:
        if isinstance(c, dict):
            key = c.get("name") or c.get("context")
            if isinstance(key, str):
                v = c.get("conclusion") or c.get("state")
                # non-string states (wrong-typed, unhashable) normalize to None -> pending.
                # Strings are UPPERCASED: the GraphQL PR rollup reports "FAILURE" but the REST
                # check-runs API (gh.branch_checks — the dev poll behind freeze/unfreeze)
                # reports "failure"; without this fold the dev branch always read "pending",
                # so red never froze and green never unfroze (Task-15 simulation catch).
                entries.setdefault(key, []).append(v.upper() if isinstance(v, str) else None)
    states = []
    for req in required:
        vals = entries.get(req)
        if not vals:
            states.append(None)          # required check not reported yet -> pending
        else:
            states.extend(vals)
    if any(s in _CHECK_FAIL for s in states):
        return "fail"
    if all(s in _CHECK_SUCCESS for s in states):
        return "green"
    return "pending"


def _pr_labels(pr_view):
    out = set()
    for lb in pr_view.get("labels") or [] if isinstance(pr_view, dict) else []:
        name = lb.get("name") if isinstance(lb, dict) else (lb if isinstance(lb, str) else None)
        if isinstance(name, str):
            out.add(name)
    return out


def fix_issue_fingerprint(check_name, summary):
    """A durable identity for a red-dev failure so the auto-filed fix issue fires ONCE per
    distinct breakage (L7 generalized: fingerprint CONTENT, never a commit). Normalization
    strips what varies between identical failures — path prefixes (basename survives), digits
    (timestamps, line numbers, retry counts), whitespace runs, case — then first 200 chars,
    sha256[:16]. Wrong-typed input still fingerprints (as empty text): the caller must always
    get a usable dedup key, never an exception."""
    name = check_name if isinstance(check_name, str) else ""
    text = summary if isinstance(summary, str) else ""
    text = re.sub(r"\S*/(\S+)", r"\1", text)     # basename any path-looking token
    text = re.sub(r"\d+", "", text)              # digits: timestamps, line numbers, counts
    text = re.sub(r"\s+", " ", text).strip().lower()[:200]
    name = re.sub(r"\s+", " ", name).strip().lower()
    return hashlib.sha256(f"{name}|{text}".encode()).hexdigest()[:16]


def gate_decision(issue_state, pr_view, report_text, config, frozen, inflight):
    """The §C.4 state machine as one table-driven pure function. Returns
    {"action": "merge"|"update"|"wait"|"hold"|"nudge"|"park"|"regenerate"|"resolve_conflict"
               |"close_investigate", "reason": str}
    plus, where computed: "wander" (journal-only flag), "overlap_lane" (with hold),
    "nudge_key" (with nudge — the runner appends it to issue_state['nudged'] after delivering,
    which is what makes each cause one-nudge-then-park), "needs_william" (with park when the
    handback needs an owner decision, e.g. the conflict cap).

    "resolve_conflict" is the one action beyond the plan's §C.4 comment vocabulary: ladder
    step (c) — a `preserve`-labeled PR replaces regenerate with a conflict-resolution SESSION
    in the PR's own branch, and that launch decision must be made HERE (the gate is the only
    place that sees the label + the update outcome together), not re-derived by the runner.

    The view contract (assembled by the runner):
      issue_state — the loopstate entry merged with parsed-issue facts:
        type ('build'|'investigate'|'diagnose-and-fix'), conflicts (int), nudged (list of
        nudge_keys already delivered), declared_touches (list), update_result
        (None|'clean'|'conflict' — the outcome of gitops.merge_update for the CURRENT head;
        the runner clears it whenever the PR head changes), investigation_done (bool,
        precomputed via investigation_done() on the issue comments).
      pr_view — gh.pr_for_branch(branch) ({} when none) with the PR's comments attached
        under 'comments' (gh.pr_comments).
      frozen — merges_frozen.json exists (freeze stops MERGES only, never builds/closes).
      inflight — {lane_issue_id: declared_touches} for the OTHER currently-running lanes.
    """
    ist = issue_state if isinstance(issue_state, dict) else {}
    pv = pr_view if isinstance(pr_view, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    nudged = ist.get("nudged")

    def nudge_or_park(key, defect):
        # one nudge per cause, then park. A wrong-typed nudge ledger parks immediately:
        # handing to William is safe; an unbounded nudge loop is not (fail closed).
        if not isinstance(nudged, list):
            return {"action": "park",
                    "reason": f"{defect} — and the nudge ledger is unreadable, parking"}
        if key in nudged:
            return {"action": "park", "reason": f"{defect} — already nudged once, parking"}
        return {"action": "nudge", "nudge_key": key, "reason": f"{defect} — nudging once"}

    # ---- investigate-type: the marker-comment contract, no PR, no merge (C1). Checked before
    # every merge-mechanics step — freeze never blocks closing a finished investigation. ----
    if ist.get("type") == "investigate":
        if ist.get("investigation_done") is True:
            return {"action": "close_investigate",
                    "reason": "investigation marker comment present — close the parent"}
        return nudge_or_park(
            "investigation",
            "report exists but no issue comment begins the investigation marker")

    # ---- build / diagnose-and-fix ----
    # step 1: a PR must exist for the issue branch (identity = the branch lookup itself).
    if not pv.get("number"):
        return {"action": "park",
                "reason": "finished but no PR exists for the issue branch (memo to William)"}
    if pv.get("state") == "MERGED":
        # defensive no-op: post-merge dev-check polling owns this phase; never merge twice.
        return {"action": "wait", "reason": "PR already merged — nothing left to gate"}
    if pv.get("state") == "CLOSED":
        return {"action": "park",
                "reason": "PR was closed without merging (external intervention) — William decides"}

    # step 2: the report must carry real prose under every required H2 (C3).
    if not report_sections_ok(report_text, cfg.get("report_required_sections")):
        return nudge_or_park("sections",
                             "report is missing required sections or they are empty")

    # step 2b: mechanical review evidence (the standing fresh-agent-review rule).
    if not review_evidence_ok(cfg, pv.get("comments")):
        return nudge_or_park("review",
                             "no review evidence (no ship pipeline and no review-marker comment)")

    # step 3: touch verification from the PR's ACTUAL files (declared touches are a promise;
    # the diff is the truth). Wander only journals; an overlap with a live lane holds the merge.
    # A wrong-typed files field is a CORRUPT VIEW: wait for the runner's next-tick refetch —
    # degrading it to "no files" once sailed past touch verification to merge (cross-review).
    files = pv.get("files")
    if not isinstance(files, list):
        return {"action": "wait",
                "reason": "PR files list unreadable — refetching before touch verification"}
    actual_areas = sorted({_config.path_to_area(cfg, f["path"]) for f in files
                           if isinstance(f, dict) and isinstance(f.get("path"), str)})
    verdict = touch_verdict(ist.get("declared_touches"), actual_areas, inflight)
    wander = verdict["wander"]
    if verdict["overlap_lane"] is not None:
        return {"action": "hold", "overlap_lane": verdict["overlap_lane"], "wander": wander,
                "reason": f"diff overlaps in-flight lane {verdict['overlap_lane']} — "
                          "hold until that lane resolves"}

    # step 4: frozen mainline holds every merge (frozen-but-building is the safe idle state).
    if frozen:
        return {"action": "hold", "wander": wander,
                "reason": "merges frozen (fix-forward in progress) — holding"}

    # step 5: required checks.
    checks = required_checks_state(pv.get("statusCheckRollup"), cfg.get("required_checks"))
    if checks == "pending":
        return {"action": "wait", "wander": wander,
                "reason": "required checks still pending — polling"}
    if checks == "fail":
        out = nudge_or_park("checks", "a required check failed on the PR")
        out["wander"] = wander
        return out

    # step 6: mergeability. GitHub computes this ASYNC — UNKNOWN/null/'' (or any unrecognized
    # value) means "still computing": WAIT, never conflict, never merge (cross-review M2).
    mergeable = pv.get("mergeable")
    if mergeable == "CONFLICTING":
        update_result = ist.get("update_result")
        if update_result == "clean":
            return {"action": "wait", "wander": wander,
                    "reason": "merge-update pushed — waiting for GitHub to recompute mergeability"}
        if update_result == "conflict":
            if "preserve" in _pr_labels(pv):
                return {"action": "resolve_conflict", "wander": wander,
                        "reason": "real conflict on a preserve-labeled PR — hire a "
                                  "conflict-resolution session in the PR's own branch (§C.4 c)"}
            ses = cfg.get("session") if isinstance(cfg.get("session"), dict) else {}
            cap = ses.get("conflict_cap")
            cap = cap if type(cap) is int else 2
            conflicts = ist.get("conflicts")
            if type(conflicts) is not int or conflicts + 1 >= cap:
                # post-increment >= cap (§C.4 6b) — and a corrupt counter goes to William too
                return {"action": "park", "needs_william": True, "wander": wander,
                        "reason": "conflict cap reached (or counter unreadable) — "
                                  "needs-william + memo"}
            return {"action": "regenerate", "wander": wander,
                    "reason": "real conflict — supersede the PR (branch preserved on the "
                              "remote) and rebuild from the issue on current dev"}
        return {"action": "update", "wander": wander,
                "reason": "PR conflicts with dev — attempt the mechanical merge-update"}
    if mergeable == "MERGEABLE":
        # step 7: everything green — squash-merge (close-by-Closes, labels, journal are the
        # runner's executor mechanics).
        return {"action": "merge", "wander": wander,
                "reason": "gate green: PR + report + review evidence + checks + mergeable"}
    return {"action": "wait", "wander": wander,
            "reason": f"mergeability {mergeable!r} not computed yet — waiting on GitHub"}
