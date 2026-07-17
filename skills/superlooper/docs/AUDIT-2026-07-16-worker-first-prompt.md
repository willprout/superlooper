# Audit — everything injected into a worker's first prompt (2026-07-16)

**Question asked.** Of every rule the loop puts in a worker's first prompt: (a) which ones does
the loop *crucially rely on* — rules whose violation breaks superlooper — each a mechanization
candidate per the point-of-error principle (hook deny / exit interview / gate check, never more
prompt text); and (b) which content is stale — no longer true or no longer needed.

**How.** Three independent audit passes over `lib/brief.py`, `templates/brief-footer.md`,
`.superlooper/config.json` bright_lines, the enforcement layer (`worker_pretooluse.py`,
`worker_hook.py`, `gate.py`, runner/janitor), and the reliability ledger, at
main == origin/main 5a58da7. Full rule register: 41 distinct rules; the brief is assembled as
header → issue body verbatim → Q&A trail → amendments → footer (work block by `type:` label,
bright lines from config, house rules, finish contract).

## Headline: one accounting error, two unguarded invariants

- **#189 was closed but never built.** The ledger documentation commit 8b79d7a ("…fixes
  #189/#190…") tripped GitHub's close-keyword at 15:32Z. No `sl/i189` branch or code commit
  exists; the Stop-hook harvest still fires on every rest with no genuinely-ended guard
  (`worker_hook.py`), and `gate.py`'s `SECTION_MIN_CHARS = 40` still passes placeholders — the
  exact pair behind the 07-16 i153/i163 draft-promotion regression. Reopened same evening,
  approval labels intact. Detector for the class: **#229**.
- **Only two rules, if ignored, could break the never-broken invariant (no bad merge, ever) —
  and both had zero enforcement:**
  - *"Never hand-post a commit status"* — a worker on the shared owner login can forge a green
    `tests` status; the gate's rollup and GitHub branch protection both believe it. Filed:
    **#226** (PreToolUse deny).
  - *"Never label anything `agent-ready`"* — workers are instructed to file child issues;
    nothing stops self-approval, and the shared login makes after-the-fact provenance
    impossible. Filed: **#227** (PreToolUse deny, also covers `pre-authorized:*`).

## List (a) — crucially-relied-on rules → mechanization state

| Rule (brief) | If ignored | Enforcement today | Action |
|---|---|---|---|
| Never hand-post a commit status | silent bad merge | NONE | **#226** deny |
| Never self-apply `agent-ready` / `pre-authorized:*` | unapproved work launches | NONE | **#227** deny |
| Report is your LAST action | draft promoted → false finish → park (was: work destroyed) | #190 built (reclaim fence); #189 **not built** | **#189 reopened** |
| Ship only via the sanctioned path (no `gh pr merge`, no direct/force push) | i328: 2h stall, all completion signals defeated | post-hoc absorb only (#155); rule not even stated in the no-ship-cmd brief flavor | **#228** deny |
| Report at canonical path with required sections | i280/i328 stalls | strong: harvest rescue + gate section check + probe ladder | adequate (residual = #189's placeholder floor) |
| Blocked protocol (question file, push WIP, end session; 2-question cap) | all-night stalled lane (i280) | strong: AskUserQuestion deny + runner cap + #190 fence | adequate; deny *text* stale → **#230** |
| Pinned review marker, re-review each push | park (fail-closed), never bad merge | fully mechanized (#154) | none |
| Bright lines: `.superlooper/**`, `.github/workflows/**` | wasted build, owner park | gate referee-park (since #17) — fires only at merge time | keep prose (it saves the build) |
| pkill/pattern-kill ban; binaries out of reports/ | owner-process kill; invisible report | both mechanized (#156 deny; fail-closed reads) | keep prose (documented backstop for deny's accepted misses) |

**Judgment calls that stay prose (correctly):** Step 0 reconcile/bounce (it *saved* the system —
D8, the i154 catch), TDD/drive-it-for-real, scope discipline, treating owner amendments as
binding. The system's right posture is what it does: bound the blast radius mechanically.

**Standing structural note:** every session posts as the owner's login, so any non-marker comment
a *worker* leaves on an issue renders as a BINDING owner amendment in a later relaunch's brief
(`brief.py` skips only `<!-- superlooper-` machine markers). No incident yet; inherent to the
single-identity setup; weighs on #226/#227.

## List (b) — stale content

1. **WRONG, actively harmful** — the #156 AskUserQuestion deny reason still teaches the retired
   pre-#163 answerer flow ("a fresh answerer replies into this session"), omits the push-WIP
   step (a verbatim-obedient worker exits with the worktree as the only copy of its work), and
   leaks a PR instruction to investigate workers. Filed: **#230**.
2. **WRONG rationale** — footer's reports/ house rule claims "the runner reads every file as
   text (a loose binary once wedged the runner)"; the runner has failed closed on undecodable
   files since the monorepo initial commit. The true, unstated consequence: a binary/misnamed
   report is *invisible*. Folded into **#230**.
3. **DEAD** — `templates/answerer-brief.md` is orphaned (nothing loads it), plus stale
   `hire_answerer` references in `actions.py:127` and `tidy.py:60,104-112`. Scope-noted onto
   the already-open **#194**.
4. **REDUNDANT but KEEP** — the pkill house rule (deliberate fail-open backstop for the deny's
   documented accepted misses) and bright lines 2/3 (gate enforces them, but only after a full
   build is spent — the prose is what prevents the waste).
5. Verified NOT stale: blocked/question protocol (matches #163 exactly), pinned-review teaching
   (single-sourced from `gate.pinned_review_marker()`), report contract, investigation marker,
   labels/metadata taught fragments.

## Filed set (all `needs-owner`, William approves by label)

- **#226** deny: hand-posted commit status / check run (bad-merge path 1)
- **#227** deny: `agent-ready` / `pre-authorized:*` self-application (bad-merge path 2)
- **#228** deny: `gh pr merge`, direct push to dev, force-push (i328 class)
- **#229** doctor/janitor: closed-by-commit-keyword with no merged PR → propose reopen
- **#230** enforcement-text drift: #156 deny reason → #163 contract; footer reports/ rationale
- **#189** reopened (accidental close by 8b79d7a); **#194** scope comment (dead answerer files)

Priority order per the audit: #226, #227, #189, #228, then #225-build (already approved), then
#230/#229 cleanup tier.
