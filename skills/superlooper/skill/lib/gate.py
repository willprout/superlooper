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
INVESTIGATION_MARKER = "<!-- superlooper-investigation -->"

# A review verdict must name the diff it reviewed (issue #154). The marker rides in two forms:
#
#   pinned  <!-- superlooper-review sha=<7-40 hex> -->   names the head oid it reviewed
#   legacy  <!-- superlooper-review -->                  unpinned: cannot prove WHAT it reviewed
#
# Only a PIN that matches the PR's current head is a verdict for the code being merged. Without
# this, `review_evidence_ok` accepted any marker comment on the PR regardless of diff — and at the
# time reapprove PRESERVED the branch (retired by #177, which rotates it), so a rebuilt gen-2 PR
# still carried its gen-1 review comment and the gate mechanically vouched for code no reviewer had
# seen: the README bright line ("no verdict, no merge") silently void for every post-reapprove
# generation. Caught before it fired a bad merge. The pin is no less load-bearing now — ANY push
# after a posted verdict moves the head out from under it, on the same branch or a rebuilt one.
# The legacy form is accepted as a SHAPE (so the gate can say "repin it" instead of "no review at
# all") but never as EVIDENCE — back-compat here is fail-closed, like every other unreadable input.
#
# The marker match is deliberately LOOSE about what rides between `superlooper-review` and `-->`,
# and the pin is validated separately. An all-or-nothing regex made a MALFORMED pin read as
# "absent" — no review evidence at all — and the nudge for "absent" prints the very marker the
# worker just posted, so it reposts it and parks: a false-park loop with no way out. Recognising
# the marker and rejecting only the PIN lets the gate say "repin this", which is the truth and is
# actionable. (Fresh-review finding, P1-3.)
# The payload must be separated by WHITESPACE (or absent entirely). `\b` would match before a
# hyphen too, so `<!-- superlooper-review-notes sha=<head> -->` — a sibling in the `<!-- superlooper-`
# marker family — parsed as a full verdict: fail-OPEN on the one property this module exists to
# protect. (Second fresh review, P2-a.)
_REVIEW_MARKER_RE = re.compile(r"<!--\s*superlooper-review(\s[^\n]*?)?-->", re.IGNORECASE)
_REVIEW_PIN_RE = re.compile(r"\bsha\s*=\s*(\S+)", re.IGNORECASE)
# A readable git oid. Abbreviations are honored (a worker reaches for `git rev-parse --short
# HEAD`); 7 hex is git's own default abbreviation and identifies a commit unambiguously on a
# single PR. Shorter than 7 is not an oid — it fails closed rather than prefix-matching loosely.
_OID_RE = re.compile(r"[0-9a-fA-F]{7,40}")

# What the briefs and nudges render inside the marker where the oid belongs. Deliberately NOT a
# shell substitution: the natural way to hand `gh pr comment` a body full of `<!--` and `-->` is
# single quotes, which do NOT expand `$(...)`. A worker taught `sha=$(git rev-parse HEAD)` posts
# that text verbatim, pinning nothing — so teach paste-the-oid instead. A worker who pastes THIS
# placeholder literally still lands somewhere honest: it parses as a marker with an unreadable
# pin, and the nudge says "repin it with the real oid". (Fresh-review finding, P1-3.)
REVIEW_PIN_PLACEHOLDER = "REVIEWED_HEAD_OID"


def pinned_review_marker(sha=REVIEW_PIN_PLACEHOLDER):
    """The pinned review marker a worker posts: the verdict names the diff it reviewed. THE one
    source of truth for the string — every place that teaches it (the brief, the review nudge, the
    conflict brief) renders it from here, so the form the worker is taught cannot drift from the
    form this module parses."""
    return f"<!-- superlooper-review sha={sha} -->"

# Paths that define the loop's live referee. Unlike ordinary wander areas, a merged change here
# immediately changes the rules that judge worker PRs, so these paths are owner-only stops.
_REFEREE_PREFIXES = (".superlooper/", ".github/workflows/")

# The owner's referee pre-authorization (issue #165). A referee-path touch can ONLY ever end in a
# needs-owner stop; when that stop is FORESEEABLE at approval, William can grant his word up front
# and it is recorded as THIS label (a distinct label, never `agent-ready` — the same discipline as
# `auto-approved:nightly-red`, so the audit trail always shows HOW the referee touch was cleared).
# The gate consumes it to merge a referee-touching diff instead of re-parking, and the launch gate
# consumes it to start a foreseeable-referee issue unattended. Its ABSENCE is the bright line: an
# un-authorized referee diff still parks needs-william, never auto-merges.
PREAUTHORIZED_REFEREE_LABEL = "pre-authorized:referee"

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

# An HTML comment is not prose (issue #189). i153's harvested report carried a literal
# "<!-- filled in after the fresh-agent review verdict lands -->" as its whole Review section and
# cleared the 40-char floor on the comment's own text — a section that renders BLANK counted as
# evidence. A comment is the absence of the thing the section exists to carry, so it is stripped
# before the count rather than merely discounted. `.*?` + DOTALL: a multi-line placeholder is one
# comment, and the strip is not line-oriented.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# An UNCLOSED "<!--" swallows the rest of the section when rendered, so it must swallow the rest
# here too — anything after it is invisible prose, and reading it as evidence is the same fail-open
# in a subtler costume. Applied after the closed-comment strip, so it only ever sees a real dangle.
# ANCHORED TO LINE START (fresh review P1), which is both what CommonMark actually does — an HTML
# block only OPENS at the start of a line — and what keeps a report that merely TALKS about "<!--"
# mid-sentence (in inline code, say) from having its real prose swallowed and its finished build
# false-parked. This very issue's report is that shape, which is how the case was found.
_OPEN_COMMENT = re.compile(r"(?m)^[ \t]*<!--.*\Z", re.S)


def _prose_chars(body):
    """A section body -> its non-whitespace PROSE characters: HTML comments removed, then all
    whitespace. This is what SECTION_MIN_CHARS measures — what a READER would actually see."""
    return re.sub(r"\s", "", _OPEN_COMMENT.sub("", _HTML_COMMENT.sub("", body)))


def report_sections_ok(report_text, required):
    """Every required H2 heading present AND carrying >= SECTION_MIN_CHARS non-whitespace chars
    of RENDERED prose — HTML comments do not count (issue #189). Wrong-typed report or required
    list -> False (fail closed); an EMPTY required list is vacuously ok (config defaults are
    non-empty; doctor owns refusing degenerate repo setups, cross-review C3)."""
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
        req in sections and len(_prose_chars(sections[req])) >= SECTION_MIN_CHARS
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


def _review_pins(comments):
    """Every review-marker comment's pin, in order, as the RAW string it claims (validated by the
    caller) — or None for a marker carrying no `sha=` at all. The marker must BEGIN the comment
    (same contract as _any_comment_begins — quoting it mid-text is not a verdict). Anything
    unreadable simply doesn't appear: a wrong-typed list/entry contributes nothing (fail closed)."""
    out = []
    for c in comments if isinstance(comments, list) else []:
        body = c.get("body") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if isinstance(body, str):
            m = _REVIEW_MARKER_RE.match(body.lstrip())
            if m:
                # group(1) is None for the payload-less `<!-- superlooper-review-->`; `or ""` keeps
                # that from raising into the tick (a corrupt input must never except — module rule)
                pin = _REVIEW_PIN_RE.search(m.group(1) or "")
                out.append(pin.group(1) if pin else None)
    return out


def _oid(v):
    """A readable git oid, lowercased for comparison — else None (fail closed). fullmatch, so a
    string that merely CONTAINS hex ('sha: abc1234!') is not an oid."""
    return v.lower() if isinstance(v, str) and _OID_RE.fullmatch(v) else None


def review_evidence_state(config, pr_comments, head_oid, review_carry=None):
    """§C.4 step 2b — the fresh-agent-review standing rule, verified MECHANICALLY *and* pinned to
    the diff it vouched for (issue #154). Returns one of:

      "ship"           — the repo's own pipeline owns review (`ship_cmd` set, e.g. the eApp's
                         diff-pinned review/local-gate); the marker contract does not apply.
      "ok"             — a verdict pinned to the PR's current head (or to a head the runner
                         mechanically carried it across — see review_carry below).
      "unread"         — the comments read was REFUSED or starved (key absent / wrong-typed).
                         NOT "no review": the caller must WAIT, never park on it (issue #78).
      "absent"         — a clean read with no review-marker comment at all.
      "head_unreadable" — a marker exists but the PR view carries no readable head oid: a corrupt
                         view, so the pin cannot be judged. Fail closed (the caller waits).
      "unpinned"       — a marker exists but carries no READABLE pin: the legacy no-`sha=` form, a
                         placeholder never substituted, or an unexpanded `$(...)`. It cannot prove
                         which diff it reviewed, so it is a shape, never evidence.
      "stale"          — a readable pin, but for a superseded diff — the head has moved since.

    `review_carry` is the runner's record of its OWN mechanical merge-update: {"from": the
    reviewed oid, "to": the head that update produced}. A merge-update merges dev into the branch
    and pushes — it moves the head WITHOUT touching the worker's authored diff, so the verdict
    must ride across it or every merge-updated PR would false-park on the review it actually has.
    The carry is bound to the head it was carried TO: the moment a WORKER pushes past it, the
    head no longer matches `to` and the pin re-stales. That binding is what keeps the carry from
    becoming a blanket re-attestation of whatever lands on the branch next.
    """
    ship_cmd = (config or {}).get("ship_cmd") if isinstance(config, dict) else None
    if isinstance(ship_cmd, str) and ship_cmd.strip():
        return "ship"
    if not isinstance(pr_comments, list):
        return "unread"
    pins = _review_pins(pr_comments)
    if not pins:
        return "absent"
    head = _oid(head_oid)
    if head is None:
        return "head_unreadable"
    attested = {head}
    carry = review_carry if isinstance(review_carry, dict) else {}
    c_from, c_to = _oid(carry.get("from")), _oid(carry.get("to"))
    if c_from and c_to and c_to == head:
        attested.add(c_from)
    # validate each claimed pin: None here means "no readable oid" (absent `sha=`, a placeholder,
    # an unexpanded substitution) — a shape the gate can name, never evidence it can act on.
    valid = [_oid(p) for p in pins]
    for p in valid:
        if p and any(a.startswith(p) for a in attested):
            return "ok"
    return "unpinned" if all(p is None for p in valid) else "stale"


def review_evidence_ok(config, pr_comments, head_oid=None, review_carry=None):
    """The boolean face of review_evidence_state: True only for a verdict that provably covers
    the PR's current head (or a ship_cmd repo). Every not-ok state — unread, absent, unpinned,
    stale, unreadable head — is False here; gate_decision distinguishes them (wait vs nudge)."""
    return review_evidence_state(config, pr_comments, head_oid, review_carry) in ("ship", "ok")


def investigation_done(issue_comments):
    """Cross-review C1: an investigation is complete iff its root-cause report exists as an
    issue comment beginning INVESTIGATION_MARKER. Zero child issues is legal — 'nothing to do'
    is a valid root cause — so the marker, not the children, is the completion signal. Since
    issue #215 the marker opens the EXIT INTERVIEW rather than the close: completion and
    findings-accounting are separate facts."""
    return _any_comment_begins(issue_comments, INVESTIGATION_MARKER)


# --------------------------- the exit interview (issue #215) ---------------------------
# ~45 actionable items once died inside investigation report bodies while the gate closed green:
# investigation_done checks only that a marker comment EXISTS, never that its findings went
# anywhere. The fix is an end-of-run exit interview, not a report format — a format taught at
# brief time asks a model to remember protocol across a long, context-heavy run (the
# instruction-drift failure mode this system keeps re-learning), so the runner ASKS at the moment
# of finish, the freshest turn, and the gate parses ONE strict line. The reply is a durable issue
# comment, so the claim is timestamped and owner-auditable; the honest limit stands — this
# guarantees findings are ACCOUNTED FOR, not correctly triaged.
EXIT_NO_FINDINGS = "NO-FINDINGS"
EXIT_FINDINGS_PREFIX = "FINDINGS-FILED:"
# The interview plus ONE re-ask, then park — the same one-remedy-then-hand-back discipline as the
# nudge ledger. Re-approval resets it (the reapprove executor zeroes exit_asks).
EXIT_ASK_CAP = 2

_EXIT_REF_RE = re.compile(r"#(\d+)$")


def _exit_parse_line(line):
    """One stripped reply line -> a (kind, refs) pair, or None when it is not reply-shaped at
    all. Prefix-shaped but unparseable text is 'malformed' — a named, re-askable defect — never
    silently ignored (that would route a worker's honest-but-typo'd answer to the no-reply park,
    whose memo would then lie about what happened)."""
    if line == EXIT_NO_FINDINGS:
        return ("no_findings", [])
    if line.startswith(EXIT_FINDINGS_PREFIX):
        refs = []
        for tok in line[len(EXIT_FINDINGS_PREFIX):].split():
            m = _EXIT_REF_RE.fullmatch(tok)
            if not m:
                return ("malformed", [])
            refs.append(int(m.group(1)))
        return ("findings", refs) if refs else ("malformed", [])
    if line.startswith(EXIT_NO_FINDINGS):
        return ("malformed", [])            # "NO-FINDINGS but…": reply intent, broken grammar
    return None


def exit_interview_reply(comments):
    """The NEWEST exit-interview reply in an issue's comment thread, parsed — or None when no
    comment is a reply at all. A reply is a comment whose body, stripped, is exactly one line
    beginning `FINDINGS-FILED:` or `NO-FINDINGS` (mid-text quotes are not replies, same contract
    as every marker). Returns {"kind": "no_findings"|"findings"|"malformed", "refs": [ints],
    "key": str} — `key` identifies WHICH comment answered, so a verification verdict is pinned
    to the reply it judged and can never vouch for a newer one. The comment's `id` is preferred
    (globally unique — two replies posted within one second share a createdAt, and a verdict for
    the first must never vouch for the second; fresh review P2-1), then createdAt, then the
    thread position. Newest wins: comments arrive in thread (chronological) order, so the LAST
    reply-shaped comment is the answer — a worker that corrects itself is believed.
    Wrong-typed comment lists/entries contribute nothing (fail closed, never a raise)."""
    out = None
    for i, c in enumerate(comments if isinstance(comments, list) else []):
        body = c.get("body") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if not isinstance(body, str):
            continue
        stripped = body.strip()
        line = stripped if "\n" not in stripped else None
        parsed = _exit_parse_line(line) if line is not None else None
        if parsed is None and line is None:
            # a multi-line body that BEGINS with a reply prefix is a malformed reply ("exactly
            # one line" is the grammar), not chatter — same intent rule as _exit_parse_line.
            first = stripped.splitlines()[0].strip() if stripped else ""
            if first.startswith(EXIT_FINDINGS_PREFIX) or first.startswith(EXIT_NO_FINDINGS):
                parsed = ("malformed", [])
        if parsed is None:
            continue
        key = None
        if isinstance(c, dict):
            for field in ("id", "createdAt"):
                v = c.get(field)
                if isinstance(v, str) and v:
                    key = v
                    break
        out = {"kind": parsed[0], "refs": parsed[1], "key": key or "idx-%d" % i}
    return out


def exit_ack_line(text, nonce):
    """The degraded (Codex) path's reply: extract the one-line reply from an ack FILE that
    carries `nonce`, nonce stripped, ready to post verbatim as the durable reply comment. The
    nonce fence is load-bearing (same rule as events.parse_ack): a stale ack answering a
    previous ask — or the #157 progress-probe ladder, which shares the ack file — must never be
    read as answering this one. The LAST matching line wins, mirroring the reply comments'
    newest-wins rule — a worker that appends a correction to its own ack is believed. Returns
    None when the nonce is absent, no line both carries the nonce and begins with a reply
    prefix, or any input is wrong-typed (fail closed: no relay, and the bounded ask ladder owns
    what happens next)."""
    if not isinstance(text, str) or not isinstance(nonce, str) or not nonce or nonce not in text:
        return None
    out = None
    for raw in text.splitlines():
        line = " ".join(t for t in raw.split() if t != nonce)
        if nonce in raw and (line.startswith(EXIT_FINDINGS_PREFIX)
                             or line.startswith(EXIT_NO_FINDINGS)):
            out = line
    return out


def accounted_child_nums(children):
    """The child-issue numbers that ACCOUNT for a finding (issue #215), from a
    gh.child_issues_health read (already filtered to genuine `parent: #N` children). A finding is
    accounted when the owner can see it — `needs-owner` — or has already acted on it under an
    existing convention: released to the queue (`agent-ready`, the approval word), claimed
    (`in-progress`), or CLOSED. An open child with none of those is invisible to the owner: it
    does NOT account, so a ref to it blocks the close. Wrong-typed entries contribute nothing."""
    out = set()
    for c in children if isinstance(children, list) else []:
        if not isinstance(c, dict) or type(c.get("number")) is not int:
            continue
        labels = _pr_labels(c)      # same dict-or-string labels fold gh uses everywhere
        state = c.get("state")
        if (labels & {"needs-owner", "agent-ready", "in-progress"}) \
                or (isinstance(state, str) and state.upper() == "CLOSED"):
            out.add(c["number"])
    return out


def exit_interview_verdict(reply, verify):
    """The close verdict over a parsed reply + the runner's verification record, shared by the
    live gate and the parked-investigation reconcile (#21) so neither can drift into closing
    unaccounted. Returns (state, detail):

      ("close",  claim)   — an accounted reply: the one-line claim for the close comment.
      ("verify", refs)    — a findings reply not yet verified: the refs for the ONE typed
                            child_issues_health read (verify is absent or pinned to an OLDER
                            reply — a verdict never vouches for a reply it did not judge).
      ("park",   reason)  — the verification record itself is unreadable: fail closed to the
                            owner, never close on a corrupt verdict.
      ("ask",    defect)  — the interview (or a re-ask) is owed: no reply yet (defect None), a
                            malformed reply, or refs the child set does not account for (defect
                            names them). The caller owns the bounded ask ladder.
    """
    r = reply if isinstance(reply, dict) else None
    kind = r.get("kind") if r else None
    if kind == "no_findings":
        return ("close", EXIT_NO_FINDINGS)
    if kind == "findings":
        refs = r.get("refs")
        # dedup + sort: `FINDINGS-FILED: #12 #12` is one claim, and the close comment should
        # read canonically whatever order the worker typed
        refs = sorted({x for x in refs if type(x) is int}) if isinstance(refs, list) else []
        if not refs:
            return ("ask", "the FINDINGS-FILED reply carries no readable `#N` refs — reply "
                           "with exactly one line: `FINDINGS-FILED: #N [#N …]` or "
                           "`NO-FINDINGS`")
        v = verify if isinstance(verify, dict) else None
        if v is None or v.get("key") != r.get("key"):
            return ("verify", refs)
        missing = v.get("missing")
        if not isinstance(missing, list) or any(type(m) is not int for m in missing):
            return ("park", "the exit-interview verification record is unreadable — parking, "
                            "never closing unaccounted")
        if not missing:
            return ("close", EXIT_FINDINGS_PREFIX + " "
                    + " ".join("#%d" % x for x in refs))
        return ("ask", "the reply lists " + ", ".join("#%d" % m for m in missing)
                + " which the parent's child set does not account for (a genuine child must "
                  "carry `parent: #N` in its Loop metadata and the `needs-owner` label, unless "
                  "already released) — file them properly, then reply again")
    if kind == "malformed":
        return ("ask", "the reply does not parse — it must be exactly one line: "
                       "`FINDINGS-FILED: #N [#N …]` or `NO-FINDINGS`")
    return ("ask", None)


def _clean_areas(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _areas_overlap(a, b):
    """The kickoff's fixed wildcard contract: '*' (a path matching no declared area glob)
    overlaps everything, in either direction."""
    return bool(a) and bool(b) and bool(set(a) & set(b) or "*" in a or "*" in b)


def _referee_paths(paths):
    """Return repo-relative paths that hit the live referee rulebook/check families."""
    if not isinstance(paths, list):
        return []
    return sorted({p for p in paths if isinstance(p, str)
                   and (p == ".superlooper" or p == ".github/workflows"
                        or any(p.startswith(prefix) for prefix in _REFEREE_PREFIXES))})


def preauthorized_referee(labels):
    """True iff an issue's label set carries the owner's explicit referee pre-authorization
    (issue #165). This is William's WORD, recorded at approval — the ONE thing that lets the gate
    merge a referee-touching diff (instead of parking it needs-william) and the ONE thing that lets
    the launch gate start a foreseeable-referee issue unattended. Fail closed on any wrong-typed
    label set (None, a bare string, non-string entries): an unreadable set is never pre-authorized,
    so the bright line holds by default."""
    if not isinstance(labels, list):
        return False
    return PREAUTHORIZED_REFEREE_LABEL in [x for x in labels if isinstance(x, str)]


def _glob_targets_referee(glob):
    """Does a config `areas` glob live INSIDE a referee subtree — so a file matching it is CERTAIN
    to be a referee path? True for `.superlooper/**`, `.github/workflows/*.yml`, and the bare dir
    names; False for `src/**` and for a merely-broad glob like `.github/**` (which COULD reach the
    workflows dir but is not certain to, so it is not a foreseeable stop — the gate's own diff-time
    park catches it if the worker actually lands there)."""
    if not isinstance(glob, str):
        return False
    prefix = re.split(r"[*?\[]", glob, maxsplit=1)[0]   # the literal head, up to the first wildcard
    return any(prefix.startswith(ref) for ref in _REFEREE_PREFIXES) \
        or glob in (".superlooper", ".github/workflows")


def foreseeable_referee_stop(declared_touches, config):
    """§C.4 / issue #165: is a referee owner-stop FORESEEABLE from this issue's DECLARATION alone,
    at approval time? True when any declared touch AREA resolves (via config.areas globs) to a
    referee subtree — i.e. building the issue will, by its own `touches:`, reach .superlooper/** or
    .github/workflows/**, a stop that can only ever end by handing to the owner. This is what makes
    the stop pre-authorizable up front (and what lets the launch gate refuse to burn a lane
    reaching a certain, un-authorized park). Fail closed to False on any wrong-typed input: an
    unreadable declaration/config is simply 'not foreseeable' — the gate's referee park over the
    ACTUAL diff remains the bright line for everything this cannot see in advance.

    Certainty is per-GLOB, not per-AREA: an area is flagged when ANY of its globs targets referee,
    so a MIXED area (`{"ci": [".github/workflows/**", "scripts/**"]}`) marks every `touches: ci`
    issue foreseeable — including one that would only ever edit `scripts/`. That over-claims, and
    deliberately in the safe direction: the cost is an approved non-referee issue WAITING for a
    pre-authorization it doesn't need (visible — it is journaled as a launch_hold naming the label,
    and clears the moment the owner grants or re-scopes), never a referee diff merging unattended.
    A repo that trips this should split the referee glob into its own area."""
    areas = config.get("areas") if isinstance(config, dict) else None
    if not isinstance(areas, dict):
        return False
    for area in declared_touches if isinstance(declared_touches, list) else []:
        globs = areas.get(area) if isinstance(area, str) else None
        if isinstance(globs, list) and any(_glob_targets_referee(g) for g in globs):
            return True
    return False


def touch_verdict(declared, actual_areas, inflight):
    """§C.4 step 3. wander = the diff left the declared touches (actual ⊄ declared) — journaled
    and morning-reported, never blocking. Nothing declared -> no promise to break -> no wander
    (a touches_required:false repo lets issues skip the declaration). overlap_lane = the first
    (sorted — deterministic) in-flight lane whose declared touches overlap the ACTUAL areas;
    the merge HOLDS until that lane resolves, because merging under a live overlapping lane
    invalidates that lane's base. overlap_wildcard = that overlap was caused by a wildcard '*' on
    either side (issue #36): the diff mapped to '*' (files in no declared `areas`), or the blocking
    lane declares '*'/nothing. It lets the runner journal WHY a merge is held — the no-match-areas
    trap, not a named-area overlap the operator declared on purpose. A wrong-typed inflight view,
    non-string lane ids (mixed-type keys would break sorted()), and wrong-typed lane entries all
    degrade to skipped — lane ids are runner-constructed strings, so anything else is corruption,
    never a real lane."""
    decl = _clean_areas(declared)
    actual = _clean_areas(actual_areas)
    wander = bool(decl) and "*" not in decl and not set(actual) <= set(decl)
    overlap_lane = None
    overlap_wildcard = False
    lanes = inflight if isinstance(inflight, dict) else {}
    for lane in sorted(k for k in lanes if isinstance(k, str)):
        touches = _clean_areas(lanes.get(lane))
        if _areas_overlap(actual, touches):
            overlap_lane = lane
            overlap_wildcard = ("*" in actual) or ("*" in touches)
            break
    return {"wander": wander, "overlap_lane": overlap_lane, "overlap_wildcard": overlap_wildcard}


def _rollup_entries(status_rollup):
    """Fold a check rollup into {check_name: [UPPERCASED state str | None]}. Rollup entries carry
    gh's two shapes: CheckRun (name/conclusion) and StatusContext (context/state). Non-string
    states (wrong-typed, unhashable) normalize to None. Strings are UPPERCASED: the GraphQL PR
    rollup reports "FAILURE" but the REST check-runs API (gh.branch_checks — the dev poll behind
    freeze/unfreeze) reports "failure"; without this fold the dev branch always read "pending", so
    red never froze and green never unfroze (Task-15 simulation catch)."""
    entries = {}
    for c in status_rollup if isinstance(status_rollup, list) else []:
        if isinstance(c, dict):
            key = c.get("name") or c.get("context")
            if isinstance(key, str):
                v = c.get("conclusion") or c.get("state")
                entries.setdefault(key, []).append(v.upper() if isinstance(v, str) else None)
    return entries


def required_checks_state(status_rollup, required) -> str:
    """§C.4 step 5: fold the PR's check rollup down to the REQUIRED checks' joint state:
    'fail' (any required check failed — beats pending), 'pending' (any required check missing
    or still running, or the rollup/required list is unreadable — fail closed: WAIT, never
    merge on a half-read rollup), else 'green'. An EMPTY required list is vacuously green here;
    doctor fails hard on it at adopt time (cross-review C3)."""
    if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
        return "pending"
    if not required:
        return "green"
    entries = _rollup_entries(status_rollup)
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


def pending_required_breakdown(status_rollup, required):
    """For a 'pending' required_checks_state, split the required names into those ABSENT from this
    rollup ('unreported' — a required check that reports nowhere keeps a green PR gating forever,
    issue #26; a check that merely reports late is absent only transiently) and those present-but-
    not-yet-terminal ('running'). Names already satisfied or failing are omitted — they are not
    what the wait is on. Returns {"unreported": [sorted], "running": [sorted]}. Wrong-typed
    required -> both empty (fail closed)."""
    if not isinstance(required, list):
        return {"unreported": [], "running": []}
    entries = _rollup_entries(status_rollup)
    unreported, running = set(), set()
    for req in required:
        if not isinstance(req, str):
            continue
        vals = entries.get(req)
        if not vals:
            unreported.add(req)
        elif any(s in _CHECK_FAIL for s in vals) or all(s in _CHECK_SUCCESS for s in vals):
            continue                      # failing or satisfied: not a pending wait
        else:
            running.add(req)
    return {"unreported": sorted(unreported), "running": sorted(running)}


def check_names(entries):
    """The set of distinct check NAMES a rollup reports (issue #26 doctor cross-check). Accepts a
    PR statusCheckRollup OR gh.branch_checks output — both CheckRun (name) and StatusContext
    (context) shapes. Empty/wrong-typed keys and a wrong-typed rollup fold to nothing (fail
    closed: no evidence, never a phantom name)."""
    out = set()
    for c in entries if isinstance(entries, list) else []:
        if isinstance(c, dict):
            key = c.get("name") or c.get("context")
            if isinstance(key, str) and key:
                out.add(key)
    return out


def _normalize_check(name):
    return re.sub(r"[^a-z0-9]", "", name.lower()) if isinstance(name, str) else ""


def _closest_check_name(req, observed):
    """The observed check name most likely MEANT by `req` when nothing matches exactly: a
    case-insensitive exact match first ('quality-gate' vs 'Quality-Gate'), then a normalized match
    ignoring case AND separators ('quality-gate' vs 'Quality Gate'). None when nothing is close — a
    genuinely unknown name, not a near-miss. Deterministic (observed is scanned sorted)."""
    lo = req.lower()
    for o in sorted(observed):
        if o.lower() == lo:
            return o
    target = _normalize_check(req)
    if target:
        for o in sorted(observed):
            if _normalize_check(o) == target:
                return o
    return None


def audit_required_checks(required, pr_names, dev_names):
    """Adoption-time cross-check (issue #26): every required_checks name must match a check the
    repo has ACTUALLY reported — on BOTH surfaces the loop reads. `pr_names` are check names seen
    on recent PRs (the merge gate reads the PR statusCheckRollup); `dev_names` those seen on the
    dev branch HEAD (the freeze/unfreeze poll reads gh.branch_checks). Names match CASE-SENSITIVELY
    — GitHub identifies a required check by exact name, so 'quality-gate' does NOT satisfy a repo
    that reports 'Quality Gate'. A check missing from EITHER surface reads as pending forever on
    that surface: a green PR that never merges (PR-side gap) or a mainline freeze that never lifts
    (dev-side gap). Returns:
      {"observed": bool,       # any check seen at all (either surface); False = no evidence yet
       "pr_observed": bool,    # any check seen on recent PRs
       "dev_observed": bool,   # any check seen on the dev branch
       "results": [{"name": req, "status": ..., "hint": name|None}]}
    status per required check (the doctor decides FAIL vs WARN from the *_observed flags):
      reported   — seen on BOTH surfaces (healthy).
      pr_only    — seen on recent PRs but NEVER on the dev branch (the 2026-07-09 incident: the
                   dev poll reads pending forever).
      dev_only   — seen on the dev branch but NEVER on recent PRs (every PR reads pending forever,
                   so a green PR never merges).
      unreported — seen on NEITHER surface. A typo/never-wired name; hint carries the closest
                   observed name (case- and separator-insensitive) when one exists.
    Wrong-typed inputs degrade to empty sets (fail closed): with no evidence `observed` is False,
    and the doctor renders 'cannot verify yet', never a false 'name not found'."""
    _seq = (set, list, tuple, frozenset)
    reqs = [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
    prs = {n for n in pr_names if isinstance(n, str) and n} if isinstance(pr_names, _seq) else set()
    devs = {n for n in dev_names if isinstance(n, str) and n} if isinstance(dev_names, _seq) else set()
    observed = prs | devs
    results = []
    for req in reqs:
        on_pr, on_dev = req in prs, req in devs
        if on_pr and on_dev:
            status, hint = "reported", None
        elif on_pr:
            status, hint = "pr_only", None
        elif on_dev:
            status, hint = "dev_only", None
        else:
            status, hint = "unreported", _closest_check_name(req, observed)
        results.append({"name": req, "status": status, "hint": hint})
    return {"observed": bool(observed), "pr_observed": bool(prs),
            "dev_observed": bool(devs), "results": results}


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
        nudge_keys already delivered), nudge_expired (issue #222: the subset of `nudged` keys
        whose compliance window has elapsed — decide computes it from the `nudged_at` stamps and
        the clock; a nudged-but-not-expired key WAITs, an expired one parks), declared_touches
        (list), update_result
        (None|'clean'|'conflict' — the outcome of gitops.merge_update for the CURRENT head;
        the runner clears it whenever the PR head changes), investigation_done (bool,
        precomputed via investigation_done() on the issue comments).
      pr_view — gh.pr_for_branch(branch) ({} when none) with the PR's comments attached
        under 'comments' (gh.pr_comments) ONLY on a clean read; a REFUSED/starved comments read
        leaves the key ABSENT, and step 2b WAITs on it (comments_unread) rather than reading the
        fail-closed empty as "no review marker" and parking a reviewed build (issue #78).
      frozen — merges_frozen.json exists (freeze stops MERGES only, never builds/closes).
      inflight — {lane_issue_id: declared_touches} for the OTHER currently-running lanes.
    """
    ist = issue_state if isinstance(issue_state, dict) else {}
    pv = pr_view if isinstance(pr_view, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    op = _config.operator(cfg)                # the owner name a hand-back memo addresses (issue #58)
    nudged = ist.get("nudged")
    # issue #222: a nudge opens a COMPLIANCE WINDOW, it does not park on the next tick. decide stamps
    # `nudged_at` on delivery and passes `nudge_expired` — the keys whose window has run out (the gate
    # stays clockless, exactly like the exit interview's `exit_ask_expired`). A key already nudged but
    # whose window is still OPEN WAITs; only an EXPIRED key parks. Fail closed on an unreadable window:
    # decide always passes a real list, so an absent/wrong-typed value is corruption — read every
    # already-nudged key as expired (park; handing to William is safe, an unbounded WAIT is not).
    nudge_expired = ist.get("nudge_expired")
    _window_readable = isinstance(nudge_expired, (list, set, tuple))

    def nudge_or_park(key, defect):
        # one nudge per cause; then WAIT out the compliance window; then park. The nudge stays a
        # single nudge (i280) — #222 bounds only the WAIT between the nudge and the park. A wrong-typed
        # nudge ledger parks immediately: handing to William is safe; an unbounded nudge loop is not.
        if not isinstance(nudged, list):
            return {"action": "park",
                    "reason": f"{defect} — and the nudge ledger is unreadable, parking"}
        if key in nudged:
            if (not _window_readable) or key in nudge_expired:
                return {"action": "park",
                        "reason": f"{defect} — nudged once and the compliance window has elapsed, "
                                  "parking"}
            return {"action": "wait",
                    "reason": f"{defect} — nudged once; waiting out the compliance window before "
                              "parking (the worker still has time to comply)"}
        return {"action": "nudge", "nudge_key": key, "reason": f"{defect} — nudging once"}

    # ---- investigate-type: the marker-comment contract, no PR, no merge (C1). Checked before
    # every merge-mechanics step — freeze never blocks closing a finished investigation. Since
    # issue #215 the marker alone no longer closes: the close runs through the EXIT INTERVIEW —
    # findings filed as needs-owner children and verified, or an explicit NO-FINDINGS. The extra
    # view fields (exit_reply parsed from the same comments read, exit_verify the runner's one
    # child-set verdict, the clock-derived exit_ask_expired / exit_delivered booleans) are
    # assembled by decide; this stays a clockless pure function. ----
    if ist.get("type") == "investigate":
        if ist.get("investigation_done") is not True:
            return nudge_or_park(
                "investigation",
                "report exists but no issue comment begins the investigation marker")
        reply = ist.get("exit_reply") if isinstance(ist.get("exit_reply"), dict) else None
        state, detail = exit_interview_verdict(reply, ist.get("exit_verify"))
        if state == "close":
            return {"action": "close_investigate", "exit": detail,
                    "reason": "investigation marker present and the exit interview is "
                              f"accounted ({detail}) — close the parent"}
        if state == "verify":
            return {"action": "verify_exit_refs", "refs": detail,
                    "reply_key": reply.get("key"),
                    "reason": "exit reply claims filed findings — verifying each ref against "
                              "the parent's real child set (one typed read; refused waits)"}
        if state == "park":
            return {"action": "park", "reason": detail}
        # state == "ask": the interview (or a re-ask) is owed. Bounded exactly like every other
        # remedy ladder: the interview plus ONE re-ask, then park — never close unaccounted.
        if ist.get("exit_relay_pending") is True:
            # the runner itself holds the worker's ack and is still posting the durable reply
            # comment — re-asking or parking now would contradict an answer already in hand.
            return {"action": "wait",
                    "reason": "worker's exit ack is being posted as the durable reply "
                              "comment — waiting for it to land"}
        asks = ist.get("exit_asks")
        asks = 0 if asks is None else asks
        if type(asks) is not int or asks < 0:
            return {"action": "park",
                    "reason": "exit-interview ask counter unreadable — parking, never "
                              "closing unaccounted"}
        key = reply.get("key") if reply else None
        if asks == 0:
            return {"action": "exit_interview", "reply_key": key, "defect": detail,
                    "reason": "investigation finished — delivering the exit interview "
                              "(findings must be accounted before the close)"}
        if detail is not None and key != ist.get("exit_asked_key"):
            # a defective reply the last ask has not yet addressed: re-ask about IT (charged
            # per reply — "malformed after one re-ask parks"), don't wait out the window.
            if asks < EXIT_ASK_CAP:
                return {"action": "exit_interview", "reply_key": key, "defect": detail,
                        "reason": f"{detail} — re-asking once"}
            return {"action": "park",
                    "reason": f"{detail} — and the exit-interview re-ask is spent; parking, "
                              "never closing unaccounted"}
        if ist.get("exit_ask_expired") is True:
            if asks < EXIT_ASK_CAP:
                return {"action": "exit_interview", "reply_key": key, "defect": detail,
                        "reason": "no usable exit-interview reply within the window — "
                                  "re-asking once"}
            if detail is not None:
                # a reply EXISTS but never became usable — the memo must say that, not claim
                # nobody answered (fresh review P2-3: honesty of the hand-back)
                return {"action": "park",
                        "reason": f"exit interview unresolved after {EXIT_ASK_CAP} asks — the "
                                  f"newest reply is unusable ({detail}) and no correction "
                                  "arrived within the window; parking, never closing "
                                  "unaccounted"}
            how = ("the interview was delivered (consumption receipt/ack recorded) but no "
                   "usable reply was posted"
                   if ist.get("exit_delivered") is True else
                   "the interview was never verifiably delivered (no consumption receipt/"
                   "ack) — the session is likely dead")
            return {"action": "park",
                    "reason": f"exit interview unanswered after {EXIT_ASK_CAP} asks — {how}; "
                              "parking, never closing unaccounted"}
        return {"action": "wait",
                "reason": "exit interview delivered — awaiting the one-line reply comment"}

    # ---- build / diagnose-and-fix ----
    # step 1: a PR must exist for the issue branch (identity = the branch lookup itself).
    if not pv.get("number"):
        return {"action": "park",
                "reason": f"finished but no PR exists for the issue branch (memo to {op})"}
    if pv.get("state") == "MERGED":
        # defensive no-op: post-merge dev-check polling owns this phase; never merge twice.
        return {"action": "wait", "reason": "PR already merged — nothing left to gate"}
    if pv.get("state") == "CLOSED":
        return {"action": "park",
                "reason": f"PR was closed without merging (external intervention) — {op} decides"}

    # step 2: the report must carry real prose under every required H2 (C3).
    if not report_sections_ok(report_text, cfg.get("report_required_sections")):
        return nudge_or_park("sections",
                             "report is missing required sections or they are empty")

    # step 2b: mechanical review evidence (the standing fresh-agent-review rule). A comments read
    # the runner could not verify leaves the 'comments' key ABSENT (or wrong-typed): the poll and
    # the finishing refresh attach comments ONLY on a clean CommentRead, so absence means REFUSED
    # or starved — never an authoritative "no review marker". WAIT for a trustworthy read rather
    # than reading the fail-closed empty as "no review evidence" and marching the nudge ladder to
    # park a finished, reviewed build (issue #78; the #21/#61 refused≠empty discipline, closing the
    # build gate's comments-attachment surface). Reaching here already means the repo has no
    # ship_cmd (else review_evidence_ok would be True), so the comments read is genuinely load-
    # bearing. A CLEAN read — a real list, even empty — keeps the nudge->park ladder intact; only
    # an unreadable/absent read waits, mirroring step-3's unreadable-files WAIT.
    # The verdict must also PIN the diff it reviewed (issue #154): any push after the verdict is
    # posted — a re-push to the same PR, or (before #177 rotated the branch) a reapproved rebuild
    # landing on the preserved one — moves the head out from under it, so an unpinned verdict
    # outlives the code it vouched for and would merge work no reviewer saw.
    # A pin for a superseded head is not evidence — it takes the same
    # nudge->park ladder as no evidence at all, under its own key so each cause gets its one nudge.
    rstate = review_evidence_state(cfg, pv.get("comments"), pv.get("headRefOid"),
                                   ist.get("review_carry"))
    if rstate not in ("ship", "ok"):
        if rstate == "unread":
            return {"action": "wait", "comments_unread": True,
                    "reason": "PR comments unread (refused or starved) — waiting for a "
                              "trustworthy read before judging review evidence"}
        if rstate == "head_unreadable":
            # a marker exists but the view has no readable head to pin it against: corrupt view,
            # same discipline as step 3's unreadable-files WAIT — refetch, never guess.
            return {"action": "wait",
                    "reason": "PR head oid unreadable — refetching before judging the review "
                              "verdict against the diff it reviewed"}
        if rstate == "absent":
            return nudge_or_park(
                "review", "no review evidence (no ship pipeline and no review-marker comment)")
        if rstate == "unpinned":
            return nudge_or_park(
                "review_stale",
                "the review verdict carries no readable `sha=` pin (a legacy marker, or a "
                "placeholder/`$(...)` that was never substituted), so it cannot prove which diff "
                "it reviewed")
        return nudge_or_park(
            "review_stale",
            "the review verdict is pinned to a superseded diff — the PR's head has moved since "
            "it was reviewed, so nothing vouches for the code being merged")

    # step 3: touch verification from the PR's ACTUAL files (declared touches are a promise;
    # the diff is the truth). Wander only journals; an overlap with a live lane holds the merge.
    # A wrong-typed files field is a CORRUPT VIEW: wait for the runner's next-tick refetch —
    # degrading it to "no files" once sailed past touch verification to merge (cross-review).
    files = pv.get("files")
    if not isinstance(files, list):
        return {"action": "wait",
                "reason": "PR files list unreadable — refetching before touch verification"}
    paths = [f.get("path") for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)]
    actual_areas = sorted({_config.path_to_area(cfg, p) for p in paths})
    verdict = touch_verdict(ist.get("declared_touches"), actual_areas, inflight)
    wander = verdict["wander"]
    referee = _referee_paths(paths)
    referee_preauthorized = bool(referee) and ist.get("pre_authorized_referee") is True
    if referee and not referee_preauthorized:
        joined = ", ".join(referee)
        return {"action": "park", "needs_william": True, "wander": wander,
                "referee_paths": referee,
                "reason": "diff reaches live referee path(s): "
                          f"{joined} — needs-owner; never auto-merging changes to "
                          ".superlooper/** or .github/workflows/** without the owner's explicit "
                          f"`{PREAUTHORIZED_REFEREE_LABEL}` pre-authorization"}
    # referee_preauthorized: the owner pre-authorized this foreseeable stop at approval (issue
    # #165) — CONSUME his word and fall through to the ordinary merge mechanics instead of
    # re-parking. It consumes ONLY the referee stop: every remaining gate (overlap, freeze, checks,
    # mergeability) still runs, so a pre-authorized PR merges only when everything ELSE is green
    # too. The paths ride onto the final decision (below) so the merge journal records that a
    # referee-touching diff merged under pre-authorization — never a silent auto-merge.
    if verdict["overlap_lane"] is not None:
        lane = verdict["overlap_lane"]
        if verdict.get("overlap_wildcard"):
            # issue #36: name the wildcard cause so "why is only one lane busy" is answerable from
            # the journal. Two shapes: OUR diff mapped to '*' (files in no declared `areas`), or the
            # blocking lane itself is a no-touches wildcard.
            if "*" in actual_areas:
                reason = (f"diff touches files in no declared `areas` (wildcard '*'), which overlaps "
                          f"every in-flight lane — holding behind lane {lane}. Add an `areas` glob "
                          "covering these files so the merge can co-schedule with other lanes.")
            else:
                # This branch is reachable only when the lane declares the LITERAL '*' (an empty lane
                # declaration never overlaps at the gate — _areas_overlap requires both sides truthy),
                # e.g. an in-flight restore-green fix (touches: *). So name that, not "no touches:".
                reason = (f"in-flight lane {lane} declares `touches: *` (unknown scope), which overlaps "
                          "every diff — holding until it resolves.")
        else:
            reason = f"diff overlaps in-flight lane {lane} — hold until that lane resolves"
        return {"action": "hold", "overlap_lane": lane,
                "overlap_wildcard": bool(verdict.get("overlap_wildcard")),
                "wander": wander, "reason": reason}

    # step 4: frozen mainline holds every merge (frozen-but-building is the safe idle state).
    if frozen:
        return {"action": "hold", "wander": wander,
                "reason": "merges frozen (fix-forward in progress) — holding"}

    # step 5: required checks. PR gating reads the PR-required set (issue #52): when required_checks
    # is split {"pr":[...], "dev":[...]}, a check that is PR-required but excluded from the dev set
    # still gates the PR here; a flat list gates both surfaces (back-compat via the accessor).
    pr_required = _config.pr_required_checks(cfg)
    checks = required_checks_state(pv.get("statusCheckRollup"), pr_required)
    if checks == "pending":
        # Surface WHICH required checks are keeping this pending (issue #26): the runner bounds the
        # wait and escalates once past the cap, naming the unreported checks in the memo. The merge
        # decision itself stays fail-closed — pending never merges — this only makes the wait
        # bounded and legible instead of silent-forever.
        return {"action": "wait", "wander": wander, "checks_pending": True,
                "pending": pending_required_breakdown(pv.get("statusCheckRollup"), pr_required),
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
                                  "needs-owner + memo"}
            return {"action": "regenerate", "wander": wander,
                    "reason": "real conflict — supersede the PR (branch preserved on the "
                              "remote) and rebuild from the issue on current dev"}
        return {"action": "update", "wander": wander,
                "reason": "PR conflicts with dev — attempt the mechanical merge-update"}
    if mergeable == "MERGEABLE":
        # step 7: everything green — squash-merge (close-by-Closes, labels, journal are the
        # runner's executor mechanics).
        out = {"action": "merge", "wander": wander,
               "reason": "gate green: PR + report + review evidence + checks + mergeable"}
        if referee_preauthorized:
            # record that a referee-touching diff merged under the owner's pre-authorization, so the
            # merge journal names the paths — a pre-authorized merge is never a silent auto-merge.
            out["referee_preauthorized"] = True
            out["referee_paths"] = referee
            out["reason"] += (f" (referee path(s) {', '.join(referee)} merged under the owner's "
                              f"`{PREAUTHORIZED_REFEREE_LABEL}` pre-authorization)")
        return out
    return {"action": "wait", "wander": wander,
            "reason": f"mergeability {mergeable!r} not computed yet — waiting on GitHub"}
