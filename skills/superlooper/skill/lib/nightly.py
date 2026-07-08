"""The nightly QA decision core (plan Task 12, spec §4.4). PURE: JUnit parsing + the
flake/persistent/accepted/quarantined classification + the auto-filed fix-issue shape. The
orchestration around it (fresh worktree of origin/<dev>, run qa.nightly_cmd, freeze merges, file
issues, journal, notify) is thin glue in `skill/bin/superlooper` — this module never touches
git/subprocess/GitHub, so the whole decision table is a unit test.

Design commitments (audit-backed, mirrored from the rest of the machinery):
  * FAIL CLOSED on unparseable results. parse_junit reports ok=False when it found no real test
    evidence (no files, all malformed, or zero testcases); the caller then renders an honest
    "nightly could not parse results" + raises ALERT, NEVER a silent green. An empty/broken
    results file is the exact place a fail-OPEN would masquerade as "all passed".
  * ONE identity scheme. A failure's fingerprint is gate.fix_issue_fingerprint (content, not a
    commit — L7), so a nightly failure, its ledger acceptance, and a runner-filed dev-red fix
    issue for the same breakage all share one key; the fix-issue body carries the runner's exact
    "Failure fingerprint: <fp>" dedup marker.
  * Flake = failed once only. With a retry, persistent = tests that failed in BOTH runs; a test
    that failed once and passed once is a flake -> gate-health stats, never an issue. Quarantine
    and accepted-ledger failures never freeze merges or file issues.
"""
import fnmatch
import re
from xml.etree import ElementTree as ET

import gate

# Must equal actions.FIX_ISSUE_LABELS — a nightly-filed and a runner-filed fix issue are the same
# standing-rule auto-approval and must be indistinguishable in the audit trail (§4.4). Pinned by a
# regression test (test_nightly) rather than an import, to keep this core free of the heavy
# actions->brief/gate/scheduler import chain.
NIGHTLY_FIX_LABELS = ["type:diagnose-and-fix", "agent-ready", "auto-approved:nightly-red", "expedite"]

_BODY_EXCERPT = 1200      # cap the observed-failure block so a giant traceback can't bloat the issue


# --------------------------- JUnit parsing ---------------------------

def _test_id(case):
    """`classname::name` (pytest's identity); name alone when classname is absent."""
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    return f"{classname}::{name}" if classname else name


def _failure_text(case):
    """The failure/error child's message + text — what the fingerprint normalizes."""
    bits = []
    for child in case:
        if child.tag in ("failure", "error"):
            msg = child.get("message") or ""
            txt = (child.text or "").strip()
            bits.append((msg + " " + txt).strip())
    return "\n".join(b for b in bits if b)


def parse_junit(xml_texts):
    """Parse a list of JUnit-XML document strings. Returns
        {"ok": bool, "tests": int, "failures": [{"test_id", "text"}]}
    `ok` is True only when at least one real <testcase> was seen across the inputs — no evidence
    of a run (no files / all malformed / zero testcases) is ok=False, so the caller never reports
    a silent green. A single malformed document is skipped, not fatal (tolerant like journal.read).
    """
    texts = xml_texts if isinstance(xml_texts, list) else []
    tests = 0
    failures = []
    for text in texts:
        if not isinstance(text, str):
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue                    # one broken file never sinks the whole parse
        for case in root.iter("testcase"):
            tests += 1
            if any(child.tag in ("failure", "error") for child in case):
                failures.append({"test_id": _test_id(case), "text": _failure_text(case)})
    return {"ok": tests > 0, "tests": tests, "failures": failures}


# --------------------------- classification ---------------------------

def fingerprint(failure):
    """Content fingerprint of one failure ({test_id, text}) via the gate scheme."""
    f = failure if isinstance(failure, dict) else {}
    return gate.fix_issue_fingerprint(f.get("test_id"), f.get("text"))


def _is_quarantined(test_id, quarantine):
    tid = test_id if isinstance(test_id, str) else ""
    for pat in quarantine:
        if not isinstance(pat, str):
            continue
        if tid == pat:                    # exact match FIRST — never depends on glob compilation
            return True
        try:
            if fnmatch.fnmatch(tid, pat):
                return True
        except re.error:
            # a real pytest id used as a pattern (`test[gpu-0]`) is an invalid glob range; the
            # exact-match check above already covered it, so a compile failure just means "no glob
            # match" — never a raise that would sink the whole nightly (the never-raise contract).
            continue
    return False


def _prep(run, quarantine):
    """Index one run's failures by test_id (last failure of a test wins), splitting out the
    quarantined ones (which never freeze/file/flake)."""
    kept, quar = {}, {}
    for f in (run or []):
        if not isinstance(f, dict):
            continue
        tid = f.get("test_id")
        rec = {"test_id": tid, "text": f.get("text"), "fp": fingerprint(f)}
        (quar if _is_quarantined(tid, quarantine) else kept)[tid] = rec
    return kept, quar


def classify(run1, run2, quarantine, accepted_fps):
    """Split failures into to_file / accepted / flakes / quarantined.

      to_file      persistent (failed both runs, or the only run when run2 is None) AND not in the
                   accepted ledger -> these FREEZE merges + file fix issues.
      accepted     persistent but content-fingerprinted in the ledger -> folded away (§4.6).
      flakes       failed once only -> gate-health stats, never an issue.
      quarantined  matched a qa.quarantine pattern -> never freezes/files/flakes.

    Flake/persistent is keyed by TEST ID (a test flaked); the fingerprint keys ledger + dedup +
    filing. All inputs are coerced to safe empties — wrong-typed args never raise."""
    quarantine = [q for q in quarantine if isinstance(q, str)] if isinstance(quarantine, list) else []
    accepted = set(accepted_fps) if isinstance(accepted_fps, (set, frozenset, list, tuple)) else set()

    k1, q1 = _prep(run1, quarantine)
    quarantined = dict(q1)
    if run2 is None:
        persistent_ids = set(k1)
        flake_ids = set()
        latest = dict(k1)
    else:
        k2, q2 = _prep(run2, quarantine)
        quarantined.update(q2)
        persistent_ids = set(k1) & set(k2)
        flake_ids = (set(k1) | set(k2)) - persistent_ids
        latest = {**k1, **k2}           # run2's record wins for a persistent test

    accepted_hit = [latest[t] for t in sorted(persistent_ids, key=lambda x: str(x))
                    if latest[t]["fp"] in accepted]
    to_file = [latest[t] for t in sorted(persistent_ids, key=lambda x: str(x))
               if latest[t]["fp"] not in accepted]
    flakes = [latest[t] for t in sorted(flake_ids, key=lambda x: str(x))]
    return {"to_file": to_file, "accepted": accepted_hit, "flakes": flakes,
            "quarantined": list(quarantined.values())}


# --------------------------- the auto-filed fix issue ---------------------------

def _fence_for(text):
    """A code fence longer than the longest backtick run in `text`, so worker-controlled failure
    output containing ``` (or longer) cannot close the fence early and inject issue-body content
    (Codex R2 M2). At least three backticks."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def fix_issue(failure, dev_branch="main"):
    """The standing-rule fix issue for one persistent nightly failure — scoped STRICTLY to
    restoring green, carrying the runner's fingerprint dedup marker and the exact standing-rule
    labels. Returns {title, body, labels, fingerprint}."""
    f = failure if isinstance(failure, dict) else {}
    tid = f.get("test_id")
    tid = tid if isinstance(tid, str) and tid.strip() else "(unknown test)"
    # newline-normalize the id: it flows into the title and an inline `code` span, where a raw
    # newline would break the heading / DoD line (and could inject Markdown structure).
    tid = tid.replace("\r", " ").replace("\n", " ")
    text = f.get("text") if isinstance(f.get("text"), str) else ""
    excerpt = text[:_BODY_EXCERPT]
    fence = _fence_for(excerpt)
    dev = dev_branch if isinstance(dev_branch, str) and dev_branch.strip() else "main"
    fp = fingerprint(f)
    title = f"Restore green: nightly failure in {tid}"
    body = (
        f"## Goal\n"
        f"The nightly QA suite failed on `{dev}` in `{tid}` — a persistent failure (it reproduced "
        f"on retry, so it is not a flake). Diagnose and fix whatever broke it. This issue is scoped "
        f"STRICTLY to restoring green — no opportunistic improvements (spec §4.4 red-nightly "
        f"standing rule).\n"
        f"Failure fingerprint: `{fp}` (auto-filed once per distinct breakage).\n\n"
        f"Observed failure:\n{fence}\n{excerpt}\n{fence}\n\n"
        f"## Definition of done\n"
        f"- [ ] `{tid}` passes in the nightly suite on `{dev}`\n\n"
        f"## Boundaries\n"
        f"Only the minimal change that restores green. Anything larger becomes a new issue for "
        f"William to approve. Merges are frozen until the nightly is green again.\n\n"
        f"## Loop metadata\n"
        f"touches:\n"
    )
    return {"title": title, "body": body, "labels": list(NIGHTLY_FIX_LABELS), "fingerprint": fp}
