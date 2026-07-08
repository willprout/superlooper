# MIGRATION KICKOFF — collapse the two projects into one shareable monorepo (new GitHub account)

**GOAL**
One private GitHub repo on THIS machine's GitHub account holding both halves — the superlooper
engine and the command-center dashboard — that installs the skill from itself and re-adopts the
dashboard into the loop, so the whole thing ships as a single clone → install → run package.
Done = a stranger could clone it, run `install.sh`, and run the dashboard, with **both original
projects' git histories preserved**.

**DECISIONS ALREADY MADE (settled with William — do not relitigate)**
- Monorepo layout: `skill/` (today's `superlooper/` contents — the engine), `dashboard/`
  (today's `command-center/`), `docs/` (constitution, design record, incident docs),
  `bin/install.sh`, one top-level `README.md` with the single clone→install→run story.
- This CONSCIOUSLY amends the original two-repo "physical wall" ruling: the wall (engine files
  simply absent from the loop-writable repo) becomes a FENCE. William accepted this trade to get
  one shareable repo.
- The fence: `skill/**`, `.superlooper/**`, `.github/workflows/**` are bright lines no loop
  worker may touch; turn ON the loop's area declaration (`touches_required: true` with `areas`
  naming at least engine vs dashboard) so a PR drifting toward the engine is flagged at the gate.
- The safeguard that makes the fence trustworthy: `install.sh` must show — and gate on William's
  OK — exactly which `skill/**` files changed since the last publish. (Rationale under
  Discoveries: publish is the human checkpoint.)
- The engine is NEVER loop-built — supervised sessions only. Only the dashboard is loop-maintained.
- `install.sh` copies ONLY `skill/` into `~/.claude/skills/superlooper` — a COPY, never a
  symlink; the dashboard never enters `~/.claude`.

**DISCOVERIES PAID FOR (cost real time; don't rediscover)**
- The running loop executes the INSTALLED copy at `~/.claude/skills/superlooper`, not the repo.
  Repo and running engine are decoupled: a bad engine merge to `main` is inert until someone
  republishes — which is exactly why the safeguard belongs at publish time, not as a live guard.
- Source NEVER lives in `~/.claude`; publishing is always a deliberate copy, never a symlink
  (a symlink would leak half-finished edits into a live loop).
- New account = new repo slug → the loop's state home becomes
  `~/.superlooper/<newowner>__<newname>/` and issue numbers restart at 1.
- DECIDE WITH WILLIAM: whether to carry the old airport's flight history (the journal). Default =
  start fresh (old issue numbers would mismatch the new repo and confuse the arrivals board /
  replay); optional keepsake = archive the old journal. It lives on the SOURCE machine at
  `~/.superlooper/will-titan__command-center/journal.jsonl` (NOT in this package).
- Preserve BOTH git histories in the merge (git subtree / `read-tree`, not a flat copy) — both
  folders in the package include their `.git`.
- OPEN engine fixes are still owed and documented in `superlooper/docs/INCIDENT-*` (the rate-limit
  park/notify storm; owner comments invisible to workers). These docs MUST survive into `docs/` —
  they are the next engine work, not history to discard.
- Shared-checkout discipline: commit by explicit path; never `git add -A` while another session is
  live in the checkout.

**DEFINITION OF DONE (evidence, not assertion)**
- Monorepo exists on this account (private), structured `skill/ dashboard/ docs/ bin/install.sh
  README.md`; `git log` shows commits from BOTH original projects.
- `bin/install.sh` runs, copies only `skill/` to `~/.claude/skills/superlooper`, and demonstrably
  shows/gates the `skill/**` diff since last publish — paste the output.
- `superlooper doctor` is green against the monorepo's `.superlooper/config.json` (new slug,
  engine+dashboard `areas`, `required_checks: ["tests"]`, the bright lines) — paste it.
- The dashboard launches from `dashboard/bin/command-center` and serves `/api/snapshot` on
  127.0.0.1 — curl or screenshot.
- README lets a stranger clone → install → run; a fresh reader could follow it.
- Suite green; CI `tests` passes on the new repo.
- Ends with a fresh-agent cross-review of the migration (structure, fences, the install.sh
  safeguard) and a short plain-language report for William.

**BOUNDARIES**
- Everything needed is in the package on this machine; you likely CANNOT reach the old `will-titan`
  account's private repos over the network — don't depend on it.
- Don't relitigate the monorepo decision or the fence-vs-wall trade.
- Supervised session (engine surgery + a move): no metered/paid spend without William's explicit
  confirmation; GitHub Actions free tier only. Preserve both histories; never force-push.

**READ FIRST (inside the unpacked package)**
1. `superlooper/CLAUDE.md` — the engine's constitution and the bright-line philosophy
2. `superlooper/PLAN-2026-07-07-command-center-mvp.md` — how the two projects relate + every
   settled decision (§A/§B)
3. `superlooper/docs/DESIGN-2026-07-06-command-center-ux.md` — the dashboard constitution (§0 rulings)
4. `superlooper/docs/INCIDENT-2026-07-08-park-notify-storm.md` (+ the other `INCIDENT-*`) — the
   engine fixes still owed
5. `superlooper/bin/install.sh` — the current publish mechanism you'll extend with the engine-diff gate
6. `command-center/CLAUDE.md` and `command-center/README.md` — the dashboard's worker rules + current
   install sketch

---

Read the listed files and the relevant code before planning anything. If there is a genuine choice
of approach, present the options with tradeoffs before building; otherwise just proceed. Follow this
project's CLAUDE.md gates and conventions where they exist. Verify with evidence before claiming
done. End with a short plain-language report: what we set out to do, what is verifiably true now
(with the evidence), open items, surprises.
