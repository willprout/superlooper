# command-center — project instructions (loop worker standing orders)

command-center is the animated 16-bit airport dashboard over the superlooper issue-loop: a
**renderer with buttons** over each adopted repo's GitHub state, journal, and state dir. It is
its own repo so the loop can maintain its own face while never touching the engine. If you are
reading this, you are almost certainly a loop worker building one approved issue in a fresh
session and worktree — these are your standing orders.

## The design record is the constitution

`docs/DESIGN-RECORD.md` is the settled design direction. Its **§0 owner rulings are fixed
points** — do not relitigate them, do not optimize them away:

- **Joy is a first-class, terminal requirement (§0.1).** The animated airfield and the Solari
  arrivals board exist to make William's work fun — not as decoration, not as a means to better
  monitoring. You may make the fun *more honest* (truth-fixes, salience fixes); you may NEVER
  trade fun away for efficiency. **Every review of this surface must include joy in its goal
  function** — "is it still delightful?" is a gate, not a nicety.
- No schedule, no nagging (§0.2). Tap-where-you-read (§0.3). One overview + per-repo terminals
  in a **square tile grid**, never a ring (§0.5). The **16-bit look** — pixel-art sprites,
  SNES-class palettes, deliberately NOT a slick SaaS chart (§0.8); the Solari arrivals board is
  the flagship delight moment and gets the animation-quality investment first.

## Constitutional inheritance (never violated)

- **No AI/LLM anywhere inside the dashboard or its server.** Every button is an existing
  mechanical verb — a label change, a comment, an issue create. No model calls, no standing seat.
- **`agent-ready` is William's word.** The Approve button is the purest form of it: a tap *he*
  makes, recorded with the standard audit comment (`Approved by William via command-center,
  <date>.`). No code ever applies that label on its own.
- **Localhost only.** The server binds `127.0.0.1` exclusively — it can write labels, so it must
  never be reachable off the machine. Never bind `0.0.0.0` or a public interface.

## How you work here (the gate enforces these mechanically)

- **TDD with pytest.** Pure logic lives in `lib/` and is unit-tested there; the JS binds values
  to visuals and stays logic-free. Test first: red → green → refactor. `pytest` is the only dev
  dependency and lives in a git-ignored `.venv` you create per checkout — see README ▸ Develop
  (`python3 -m venv .venv && .venv/bin/python -m pip install pytest`); the runtime needs no venv.
- **Python 3 stdlib only** at runtime — no pip dependencies. The deployment target is William's
  Mac (currently **Python 3.9**), and CI pins 3.9; keep every runtime construct 3.9-compatible.
- **Semantics server-side, pixels client-side (B.1).** Stage mapping, liveness tiers, the
  progress heuristic, the gate checklist, pill aggregation — all derived in pure Python `lib/`
  with tests. The squint test: delete the art and the JSON is still a correct state diagram.
- **No test may reach a real external binary** (`gh`, `osascript`, `cmux`) or the network.
  `tests/conftest.py` neutralizes the resolution env vars fail-closed by default (autouse) and a
  guard test fails if that is ever removed — keep it that way (2026-07-03 toast-spam ratchet).
- **Fresh-agent review, always.** Every change is reviewed by an agent that did NOT write the
  code — `/cross-review` (Codex second opinion) by default, a fresh subagent reviewer if Codex is
  unavailable — with the verdict posted on the PR. P0/P1 findings are fixed before the final
  commit. Non-regulated repo: at most **2 review/fix rounds**, then stop and present William a
  consolidated decision.
- **Your final report must contain these H2 sections** (the gate checks their presence in your
  report mechanically):
  - `## Tests` — the suite is green; show the output.
  - `## Screenshot evidence` — on a **visual task** (airfield, boards, panels, drawer), a
    rendered-dashboard screenshot proving it looks right, joy included. On a **pure-logic task**,
    write the heading and state plainly that there is no rendered surface (correctness is shown
    by the unit tests). **Image files go in `reports/screenshots/` inside the state home —
    NEVER loose in `reports/` itself.** Only `.md` belongs at that level: the runner reads every
    top-level file in `reports/` as text, and one binary file there wedges its every tick
    (incident 2026-07-07 — three PNGs stalled the whole loop for 40+ minutes, silently).
  - `## Review` — the fresh-agent review verdict and how P0/P1 findings were resolved.
- **Never claim done without evidence:** run the tests and show the output; render the dashboard
  and show the screenshot.
- **You share this machine with a live runner and William's own running dashboard.** Never kill
  processes by name or pattern (`pkill -f`, `killall`) — the pattern `bin/command-center` also
  matches William's live dashboard, which was collateral-killed exactly this way (incident
  2026-07-07). When you background a server for evidence, record its PID (`$!`) and kill only
  that PID.

## Bright lines (never edit these paths)

- **`.superlooper/**`** — the loop's executable config (the referee's own rulebook: gates,
  required checks, notify commands). A PR that edits it reprograms the loop; such changes come
  only through a supervised session, never a loop worker.
- **`.github/workflows/**`** — CI is the mechanical gate that judges your PR, and a worker must
  not edit its own referee. Workflow changes come only through a supervised session.

## Money

No metered/paid spend without William's explicit confirmation. The one pre-approved exception is
the GitHub Actions `tests` workflow (free tier). Codex `/cross-review` is free.
