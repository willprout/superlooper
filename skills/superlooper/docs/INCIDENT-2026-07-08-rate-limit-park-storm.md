# SUPERSEDED — see INCIDENT-2026-07-08-park-notify-storm.md

This doc was the command-center orchestrator's independent write-up of the same night
(2026-07-08 morning), produced without noticing the superlooper orchestrator's fuller
analysis already in `docs/INCIDENT-2026-07-08-park-notify-storm.md`. That doc is
authoritative — it adds what this one lacked: the storms ride the **GraphQL** hourly window
(resets ~:11:55 past each hour; both storms ended at :11), there were TWO storms (i32 then
i36) with clean mid-hour merges between them, and burn-share attribution is explicitly
unproven pending observability.

Everything this doc found is covered there, with one delta now relocated to its proper home:
the **dashboard's own burn** is command-center's follow-up (their doc correctly scopes it
out). Code-verified 2026-07-08 by the command-center orchestrator: the dashboard's snapshot
assembly calls `pr_for_branch` (GraphQL) for EVERY issue in issues.json — including merged
ones, whose branch field persists — each on its own 30s cache, forever. At ~21 landed
flights that is ~2,500+ GraphQL calls/hr for data that can never change again, growing with
every landing. Fix tracked as a command-center issue (fetch-once-and-remember for concluded
flights, subsuming held issue #47's merged-PR-size caching clause).
