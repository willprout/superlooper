---
# Loop contract (mechanical — the runner reads files, not your words)

The runner acts on FILES you write, not on prose. Do exactly what these steps say.

**Step 0 — reconcile (MANDATORY FIRST STEP).** Read the issue above{post_approval_note} against CURRENT `origin/{dev_branch}`. Small drift (a stale pointer, a renamed file): proceed and append a
one-line note to issue #{issue_num}. Premise-level drift (the problem is already gone, or the
approach is invalidated by what actually shipped): STOP — write {blocked_path} beginning
`BOUNCED:` with a one-line explanation PLUS a ready-to-approve proposed amendment to the
Goal/DoD, then end your session. The RUNNER (not you) posts that memo to the issue and moves
the labels — you touch no labels and you never edit the issue's Goal/DoD yourself.

**Scope.** Work in this worktree on branch `{branch}`. Stay strictly inside the issue's
Boundaries. Work this issue should NOT do — and any promise that spills into another PR —
becomes a NEW issue labeled `needs-owner`, never a code comment.

{work_block}

{bright_lines}**Blocked?** Write your single, specific question to {blocked_path} and end your turn. A
fresh answerer will reply into this session. If you can safely proceed on one reasonable
assumption, {assumption_hint}

**Long background wait?** touch {awaiting_path} first, and remove it when you resume.

**House rules (every session).** Image and binary evidence (screenshots, PNGs, PDFs) goes in a `reports/screenshots/` subdirectory beside your report — only `.md` files belong at the top level of `reports/`, where the runner reads every file as text (a loose binary there once wedged the runner). Never kill a process by name or pattern (`pkill -f`, `killall`) — the pattern can also match the owner's own live processes; record the PID (`$!`) of anything you background and kill only that PID.

**Finish.** {finish_deliverable}Then write {report_path} with EXACTLY these H2 sections:
{report_sections} — the runner mechanically checks they exist and carry real prose. The
report is your LAST action. Never force-push. Never hand-post a commit status. Never label
anything `agent-ready` (that word is {operator}'s alone).
