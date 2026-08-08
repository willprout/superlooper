# Herdr adoption plan — component disposition

**Status: RULED 2026-07-30 — owner adopted herdr as the host** (direct word to the orchestrator:
"I'm good with Herdr"). This document is now the plan of record. Posture per the same ruling:
**attended-preferred, headless-capable** — the owner keeps a viewer window (nice UX only now —
the pinned v0.8.0 carries the #2064 fix), but nothing load-bearing may depend on a client
being attached, and acceptance tests run clientless. Evidence basis: docs/TOOL-DIVE-2026-07-28.md,
docs/SPIKES-2026-07-29/30-*.md, ARCHITECTURE-PROPOSALS.md §4, and HERDR-UPSTREAM-ISSUES.md (a
local working file, deliberately not committed — the filed issues live on the herdrdev/herdr
tracker: #2063–#2066).
Operating principle throughout: **herdr is muscle, never truth.**

## 1. Use as-is (stock herdr)

- **The background server** — session hosting, PTY ownership, survives client detach. The body.
- **Workspace/worktree topology** — `workspace create --cwd --env`, worktrees first-class.
- **`agent start`** — the spawn verb (wrapped with our post-spawn confirmation).
- **`agent prompt --wait`** — the ONLY prompt form we ever use, + `agent wait`.
- **Crash-restore + auto-`claude --resume`** — the revive machinery; clientless since the pinned
  v0.8.0 (§8.1), measured by #302 and #311.
- **Attach / observe** — the owner's window, SSH thin client, read-only frame streams for the
  dashboard live view. Watching costs nothing and load-bears nothing.
- **Screen manifests for hook-blind states** (auth-death, crash-at-startup) with their
  fleet auto-updates — as FALLBACK sensing only.
- **The seen/unseen bit** — read via CLI (which never marks seen) for morning-report dedup (c22).

## 2. Configure, don't code

- `resume_agents_on_restore=true` (already the default).
- Local manifest override: unknown screen → UNKNOWN, never idle (herdr supports local overrides).
- Dedicated named session + short socket path (sun_path 104-char limit).
- Server + viewer started as gui/$UID login items — never Background launchd (keychain rule);
  explicit PATH in anything launchd-started (bare-bones default PATH).

## 3. Customize (the two real modifications)

1. **The fence** — token auth on the control socket (insertion point: src/api/server.rs
   handle_connection, before dispatch). **RULED 2026-07-31: carried Apache patch only, NO
   upstream ask** — owner: "we're not using it as designed" (reverses this plan's earlier
   upstream-first stance). Cost accepted: re-apply the patch at every version bump (bump = a
   deliberate event that re-runs acceptance anyway). **Token distribution, per the same ruling:
   runner and watchdog hold it; sl-debugger/Fixer `d<N>` sessions RECEIVE it at spawn** (owner's
   explicit requirement — repair sessions must be able to drive herdr, e.g. revive/kill/read
   panes); **workers `i<N>` never see it.** The token grants the how; the D13 supervised/
   unattended rails still govern the whether. REQUIRED before unattended fleet use. Also strip
   HERDR_* from worker pane env as defense-in-depth (the socket path is guessable, so the token
   is the real fence). **The token is readable by the processes it fences, and that is ACCEPTED
   (owner ruling 2026-08-05, #342)** — on a machine where fleet and workers share one UNIX
   account no file-based scheme keeps a secret from them, but the token bounds the CONTROL
   SURFACE, not the operating system: what a worker that reached it could then do is already
   bounded by the standing rails (the kill-by-pattern deny, the D13 supervised/unattended
   split), and a second UNIX account is not worth upending the keychain, login-item and
   worktree-ownership arrangements. #342's closing comment is the ruling of record; it covers
   the `token.provenance` sidecar (#309) on the same grounds.
2. **Session-id capture without the global settings rewrite** (#2066) — carry herdr's
   state-report hook line inside superlooper's OWN per-worker hook config at launch, instead of
   `herdr integration install claude` touching global ~/.claude/settings.json.

## 4. Build on top (ours — the wrapper)

The **five-verb wrapper module**: spawn / send / state / exit / kill. One doorway; the rest of the
runner never talks to herdr directly; swapping herdr's internals for tmux later is a bounded
rewrite behind an unchanged interface.

- **spawn**: workspace+agent start, then confirm the process actually exists (post-spawn check).
- **send**: `--wait` always + transcript-side delivery verification (rc alone is never proof;
  even fixed-preview rc=0 means "queued"). **Enter-chaser — settled on the pinned v0.8.0:** a
  post-revive first prompt now submits on its own (5/5 unchased, #302), so `send-keys enter` is
  not an unconditional step; the wrapper fires it only when the delivery oracle has NOT confirmed
  a revived send, and #317 extended that fallback to a send that waited out a host restore window
  (a restored send IS a post-`--resume` first prompt — herdr did the reviving instead of us).
  Scoped to the revive path on purpose: a stray Enter into a pane showing a selection dialog
  SELECTS. Resume re-orientation preamble (a revived session remembers the conversation, not the
  world).
- **state**: superlooper's own Claude hooks are PRIMARY truth; herdr surfaces are advisory;
  liveness = process facts (pgrep pane child), never `agent list`.
- **exit/kill**: keep our ordered-teardown discipline; herdr verbs as mechanism only.

## 5. Explicitly not using / banned

1. Plain `agent prompt` without `--wait` — banned (rc=0 carries no delivery information).
2. herdr's lifecycle states as truth — they encode attended-mode "has the user seen it"
   semantics (SKILL.md: idle = ready AND seen in the focused UI).
3. **herdr plugins — the stance was worded wrong; corrected by survey (2026-07-30).** Plugins are
   **global to the user**: herdr's own docs state installed plugins are "available in every Herdr
   session," and none of the 148 config keys scope them per session/workspace/agent. So
   "banned for workers, fine for the owner" is NOT implementable — anything the owner installs is
   invokable by every worker, via an unauthenticated socket, and plugin commands never pass
   through Claude Code's PreToolUse hooks (a standing bypass of our deny rails).
   **Therefore the enforcement point is not "don't install plugins" — it is the fence: workers
   must not reach the herdr control surface at all.** Note the fence must be token auth, not
   env-stripping alone: `HERDR_SOCKET_PATH` is injected into panes AND the default path is
   deterministic, and the protocol is plain JSON — a worker needs no `herdr` binary to drive the
   fleet, so denying the verb is insufficient. Strip the env var too, as defense in depth.
   Marketplace context: unreviewed auto-scrape of the `herdr-plugin` GitHub topic (424 listed,
   368 created in the last 30 days, median 1 star, live typosquat observed) — install only by
   exact owner/repo, pinned with `--ref` (there is no update command; reinstall re-clones main).
   **Post-fence, three are worth a supervised trial in the owner's own window:**
   herdr-file-viewer (294★, read-only, explicitly no event hooks — best value/risk),
   herdr-navigator (fuzzy jump across many panes, no hooks/network), and at most one phone path
   (herdr-remote menu-bar-only mode; its relay, and Collie, are remote shell access by their own
   authors' words). **Skip:** herdr-plus (globally-invocable arbitrary script runner), herdr-lazy
   (startup hook that fetch-executes plugins at every server boot), reviewr (auto-runs on every
   `worktree.created` AND pulls the owner toward per-diff review — against the batching model),
   worktrunk (duplicates the runner's own worktree logic). Check herdr's FIRST-PARTY
   `[ui.toast]` / `[ui.sound]` before any notification plugin — ~25 of them re-implement it.
4. ~~TEMPORARY: reliance on clientless auto-revive (login-item viewer / robot client)~~ —
   **item deleted 2026-08-05 as its own text instructed**: the #2064 fix shipped in the pinned
   v0.8.0 (§8.1) and clientless restore across `kill -9` with no client ever attached is now
   measured twice — #302's lab (3 drills, 6/6) and #311's unfenced acceptance control on the
   mini. Nothing here may be given a viewer or robot client again on this reasoning.

## 6. Sequencing gates (unchanged from §4 rulings)

Floor work first (host-independent): resurrection trick (--session-id/--resume + re-orientation),
env scrub + no-API-key/base-url launch assert (c1 — two realized incidents), positive gh-auth
post-spawn assert. Then: standalone-claude PATH pin BEFORE any cmux removal; wrapper + fence;
parallel-run period — **cross-machine per the 2026-08-03 machine ruling: the herdr canary lane
stands up fresh on the Mac mini while cmux carries production untouched on the work laptop**
(owner supervising the first flights per his rule); acceptance = the S-criteria spike tests
re-run against the wrapped herdr, clientless, ON the mini; only then cmux retires and the
laptop side winds down (in-flight work finishes before cutover per the preservation rules).

## 7. Design fit: where our use matches herdr's intent and where it inverts it

**Mechanism level — fully as designed.** herdr deliberately exposes every capability twice (CLI +
newline-JSON socket) so scripts and agents drive it identically to a human. Machine-driving is a
first-class supported mode, and we use the documented forms (`--wait` always, `--no-focus`,
address by name, parse IDs from JSON).

**Workflow level — inverted, deliberately, in four ways.** herdr's daily model is: the human is
the driver and the reference point; agents are peers in one tab spawning sibling helpers; focus
management matters; plugins extend the human's workflow. Ours: the machine is the driver, the
human is an occasional spectator, workers are isolated strangers with zero fleet reach, focus
never moves. Two consequences worth stating plainly:
- **The phantom is this seam, not a stray bug.** `idle` = "ready AND seen in the focused UI" is
  coherent for an attended operator and meaningless for an unattended runner. Hence §4's rule:
  herdr's states are advisory, our hooks + process facts are truth.
- **The fence bans herdr's own headline agent capability.** The bundled agent skill exists to
  teach an in-pane agent to inspect siblings and spawn helpers; that is exactly the reach we deny
  workers. So the skill is NOT installed for workers. It IS worth installing for the sl-debugger
  (read-only diagnosis of a live herd) and for the owner's own supervised sessions.
- **Drift risk:** herdr optimizes for its attended user base, so features may evolve in
  directions that don't serve us. Mitigations already in the plan: the wrapper (one doorway), the
  tmux escape hatch, and upstream engagement (working well so far — see the response log).

### Wrapper rules the skill's own documentation dictates

1. **Address agents by NAME, never by cached pane ID.** A pane moved to another workspace gets a
   NEW workspace-qualified pane ID; the old ID stops resolving for outside callers. A cached
   handle breaking when the owner rearranges his window IS the cmux dragged-anchor incident class
   — this is how we avoid re-importing it. Names follow the pane occupant and are cleared when the
   agent exits, so "name no longer resolves" is itself a real signal. Constraint: names must match
   `[a-z][a-z0-9_-]{0,31}` — issue-keyed names must be lowercase and ≤32 chars.
2. **Closed tab/pane IDs are never reused** — safe for our handle bookkeeping (no ABA problem).
3. **Alternate-screen limit on reads:** rows that scroll off Claude's alternate screen never enter
   herdr's scrollback, so no line count recovers them. Screen reads can NEVER be the evidence
   path — the documented fallback (agent writes a file, supervisor reads the file) is exactly
   superlooper's report mechanism. Independent confirmation of our design.

### Features we are not using yet, worth evaluating

- `agent send-keys <name> esc|ctrl+c` — logical keys, validated before any bytes are written.
  Cleaner than raw keystrokes for dismissing dialogs, and the natural mechanism for the
  post-revive Enter chaser.
- `pane run` + `pane wait-output --match/--regex` — atomic command+Enter and output matching in a
  non-agent pane. Candidate for runner-side evidence commands.
- `agent read --source detection` — the exact plain-text buffer herdr's own classifier reads;
  better than us re-capturing screens for the fallback sensing path.
- `herdr notification` and `terminal session observe` (read-only frame stream) — unexplored;
  candidates for the dashboard live view and morning-report feeds.

## 8. Open items

1. ~~The host decision itself~~ — **RULED 2026-07-30: adopt herdr** (see Status header).
   Sub-decisions, ruled 2026-07-31: **runner lives OUTSIDE herdr** (plain login-item process
   talking to the server — the supervisor never lives inside what it supervises); **version
   policy = pin an exact release containing the #2063-class prompt fix + the #2064 restore fix**,
   upgrades are deliberate events that re-run the acceptance spikes. **What a bump RUNS is
   `skills/superlooper/bin/acceptance.py`** (issue #348) — the suite as an artefact rather than as
   #311's transcript. It builds a throwaway host from the carried patch as it currently stands,
   stands it up on sockets of its own, drives it only through the five-verb doorway, never attaches
   a client, and reports against the twelve criteria in `docs/ARCHITECTURE-PROPOSALS.md` §3 with
   one evidence file each; a failed criterion exits non-zero and a criterion it cannot judge
   unattended is reported NOT RUN with its reason rather than rounded up. Its paired controls are
   the part that is load-bearing: the fenced and unfenced lanes run together in the same run, which
   is the only reason #311 could say "a refused call" instead of "a broken build". It costs real
   agent sessions and says so before it spends them. **The pin is herdr `v0.8.0`**
   — stable (`prerelease=false`), published 2026-08-03, commit
   `346411fa21afd297f5ed3b3fa56f9e3fbf7654b7`, retested asset `herdr-macos-aarch64` sha256
   `d53a9f93fccfdfcc55632927bf51002f5add0aa7990bcdf508ffbd84ac658178` (#302 proved by ancestry
   that it contains `ac47b9e`, the #2064 restore fix; the release notes name the #1878
   submission fix, fix #2066, and relicense herdr AGPL-3.0 → **Apache-2.0**, which makes §3.1's
   carried fence patch a plainly permitted act). The fallback re-decision (preview-pin vs
   stable+workarounds) never triggered. The pin's machine-readable home is
   `skills/superlooper/skill/lib/herdr_hook.py`'s `PINNED_VERSION`, which
   `skills/superlooper/vendor/herdr/build.sh` reads so a bump cannot leave two disagreeing pins
   on disk; **fence = carried patch only, no upstream ask, d<N> sessions get the token**
   (ruled 2026-07-31, see §3.1);
   **fleet machine = MAC MINI as a fresh build-up (ruled 2026-08-03)** — the herdr fleet stands
   up on the always-on mini from day one while cmux production keeps running untouched on the
   work laptop; cutover only when the acceptance suite passes ON the mini. Identity plan: the
   fleet rides its own Claude subscription + the loop's GitHub login via per-worker
   `CLAUDE_CONFIG_DIR`/`GH_CONFIG_DIR` (c25), keeping the owner's personal mini sessions on his
   other subscription — includes a cheap floor spike proving two subscriptions coexist in two
   config dirs on one Mac (designed mechanism, not yet proven in our lab). Agent posture, **owner
   ruling 2026-08-05 (#352): Claude Code only for now — Codex is not a live target**, so no
   delivery-oracle or pre-trust work is owed for Codex lanes. No sub-decisions remain open on the
   host.
2. ~~#2064 fix shipping + retest~~ — **RESOLVED: shipped in the pinned v0.8.0 (§8.1) and retested
   clientless (#302, 3 drills 6/6; re-demonstrated by #311's unfenced acceptance control on the
   mini). §5.4's workaround is deleted.**
3. Fence: upstream PR accepted vs carried patch.
4. ~~Enter-chaser necessity on fixed builds~~ — **RESOLVED: retested on the pinned v0.8.0 (#302,
   5/5 unchased); §4's send rule now states the settled behaviour.**
5. Proposal 1b (pre-flight triage) — unruled, orthogonal.
6. Sleep soak (one agent-free night) — acquitted for a short nap, full night unproven.

## Evaluated and rejected (so nobody re-litigates)

- **owainlewis/youtube-tutorials `herdr-agent-workflow` `/ticket`** (evaluated 2026-07-30):
  nothing to adopt or steal. Despite the name it authors no ticket — it is a 16-line dispatch
  command (read issue → worktree → unfocused tab → start claude → send an improvised,
  never-written-down task) inside a tutorial whose bulk is a 606-line video script. No issue
  format, no artifact, no gate; its flight ends at `gh pr create` with "self-review" as the only
  quality control — the exact shape P1 exists to remove, and its one-sentence unwritten task is
  the anti-pattern of P2's on-disk plan artifact. Its wiring is also stale/incorrect vs the
  current CLI (`agent send` isn't a verb; `#<n>` violates the name rule; wrong `agent start`
  flags) and its happy path is **our filed bug #2063**: `--no-focus` tab + prompt without
  `--wait` = silent drop. Only takeaway, and it is context not code: an independent practitioner
  converged on the same one-issue → one-worktree → one-fresh-agent → one-pane shape with herdr as
  the dispatch target — mild outside support for the herdr direction, nothing more.

## 9. The rest of the system (added 2026-07-30 — the plan above covered only the worker seam)

Verified against the code, not assumed. **There are THREE spawners today, not one** — every one
of them shells out to `launch-session.sh`:

| Spawner | Spawns | Trigger |
|---|---|---|
| runner (`_exec_launch`) | worker sessions `i<N>` | an approved issue |
| watchdog | sl-debugger sessions `d<N>` (`--cwd` mode) | unattended fault, no owner present |
| dashboard Fixer | sl-debugger session `d<N>` | owner taps Debug |

**Migration landmine:** rewiring only the runner leaves the watchdog and Fixer calling a cmux
launcher that no longer exists — i.e. no unattended repair at exactly the moment repair is
needed. The five-verb wrapper must be the single spawn path for all three, and the two `d<N>`
paths must be migrated and tested in the same wave as workers, not after. (The launcher's
`^i[0-9]+$` / `^d[0-9]+$` mode guards must survive the port — they are what stop a debugger id
from being launched as a worker and vice versa.)

**Substrate-free — needs nothing from this migration** (verified): `janitor.py` (pure GitHub-side
selection, "no gh, no subprocess, no clock"; proposes, owner approves), `nightly.py` (decision
table + launchd), the gate, scheduler, issue/label logic, report/notify. Roughly the whole brain.
This is the payoff of the split: the herdr swap touches session hosting and nothing else.

**Changes with the host:**
- **The runner's own home.** Today it must live in a visible cmux tab (its pane is the launch
  anchor; a detached/launchd runner loses the socket — D7). Under herdr the anchor concept
  disappears: the runner becomes an ordinary process that talks to the herdr server. It still
  must start from the gui/$UID login context (keychain rule), and `superlooper run` / Liftoff /
  Restart / the #116 in-place `os.execv` re-exec all need revisiting — several may simply retire.
- **Dashboard cmux verbs:** Tidy (closes finished sessions' cmux windows), Liftoff, Restart, and
  the Fixer's launch path are all cmux-shaped. Owner ruling 2026-07-30: replace the live-view
  ambition with **a button on the issue card that opens that session's herdr window** (attach,
  which is proven) — no observe-stream plumbing.
- **sl-debugger sessions** get the same wrapper treatment, and per the 2026-07-31 fence ruling
  they RECEIVE the socket token at spawn (repair requires driving herdr; workers never do). The
  token grants capability only — the D13 supervised/unattended rails still bound what a `d<N>`
  session may actually do with it. Related open question: a debugger session is exactly the case
  where herdr's own agent skill IS wanted (read-only inspection of a live herd), so `d<N>`
  sessions may carry it while `i<N>` workers never do.
- **Pre-flight (the pre-tab half of `launch-session.sh`) survives mostly intact:** base-ref check
  (distinct exit 3 so a missing base never becomes a hollow launch), worktree creation (stays
  ours — c17 flock, our preservation rules), env scrub (c1), and **pretrust** — herdr does NOT
  remove the first-run trust dialog (the supervised run hit one, and herdr classified that
  blocked pane `idle`), so S9 pre-trust stays load-bearing.

**Question-answering is already fence-compatible and needs no herdr feature.** The answerer seat
is RETIRED (#163): a worker writes `state/blocked/<id>`, ends its turn, the runner posts a durable
GitHub comment, the lane releases, and a FRESH session picks the answer up later. No session ever
spawns a session to ask a question, so the fence costs nothing here.

**"Sessions spawning sessions" under the fence — the general rule:** workers cannot reach the
herdr control surface, therefore **all session creation flows through the runner or the watchdog**.
Nothing today violates this (answerer retired; exit interviews ride the hook mailbox; cross-review
runs as a CLI *inside* the worker's own session, not as a sibling pane). Consequences for the
active proposals: P1's finisher = a runner-spawned session (fine); P2's executor = either a
Claude Code subagent in-process (fine, no herdr involvement) or a runner-mediated second session
(fine). Only one future pattern is foreclosed: a worker directly spawning a sibling herdr agent —
e.g. making the Codex reviewer its own pane. If that is ever wanted, it must be runner-brokered,
never worker-initiated.
