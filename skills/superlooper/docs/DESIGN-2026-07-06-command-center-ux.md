# DESIGN 2026-07-06 — The Command Center (superlooper UX)

**Status:** settled design direction from the "fable plan ux" session (William + Fable,
2026-07-06). Adversarially reviewed the same day by a 7-agent panel (§8); amendments adopted,
one recommendation overruled by owner ruling. This is a **design record, not an implementation
plan** — it is what a future build session gets briefed from. §0 rulings are fixed points in
the same sense as the constitution's §2: do not relitigate them, do not optimize them away.

Constitutional inheritance (unchanged, restated so no reader forgets): the command center is a
**renderer with buttons** over GitHub + the journal + the state dir. No standing LLM seat
anywhere in it; no AI inside the dashboard; every button is an existing mechanical verb (label
changes, comments, issue creation); `agent-ready` remains William's word — a button *he* taps
is the purest form of it, recorded with the standard audit comment.

---

## §0 — Owner rulings (fixed points)

1. **The joy ruling (2026-07-06, overrules review recommendation #1).** Joy and gamification
   are **first-class, terminal requirements** of this product — not decoration, not a means to
   better monitoring. William: *"I'm trying to gamify my work a little bit. I want to bring
   some joy into my life… work can also be fun."* The animated airfield ships in V1. Future
   sessions and reviews may make the fun **more honest** (truth-fixes, salience fixes) but may
   not trade fun away for monitoring efficiency. Reviews of this surface must include joy in
   their goal function.
2. **No schedule, no nagging.** No daily-edition ritual; event pushes + a living surface
   checked at will. Queue runway is **visible only** — the machine never nags William to feed
   it.
3. **Tap-where-you-read.** Wherever a decision is shown, its action buttons are right there.
4. **Presence modes are V2** (away-mode autonomy is V1 baseline behavior), along with
   per-tool activity verbs and AI-drafted flags.
5. **One overview, per-repo terminals**, navigated by panning one continuous airfield (plus
   click-to-fly); bigger/more-active repos get bigger terminals. **Arrangement ruling
   (2026-07-07):** terminals sit next to each other in a plain **square tile grid**. The
   exported design's "terminals on a ring" concept (camera swinging around a ring) is
   REJECTED by owner taste — do not rebuild it.
6. **Planning stays in sit-down sessions** (the real Gate-1 intake). The center gets a
   lightweight **flag box** for reacting to what you see.
7. **Promotion is a per-repo dial**: session-grade where heavy (eApp), one-tap where light
   (public repo → own instance), absent where main is live. Universally rendered as one
   number per repo: "what has landed that users haven't gotten yet."
8. **The 16-bit look (2026-07-06).** The airfield view is styled as a 16-bit-era video game —
   pixel-art sprites, SNES-class palettes — deliberately NOT a slick SaaS visualization.
   (This also keeps the animation layer cheap: sprites, not vector physics.) The **Solari
   arrivals board is the flagship delight moment** — it gets the animation-quality
   investment first, and it should be genuinely satisfying.

---

## §1 — What this is, and who it serves

One surface where William reads the state of every loop-adopted repo **and acts on it**. Two
personas, both first-class:

- **The vibe coder** (William): solo, non-professional, ADHD. Needs plain language, glanceable
  truth, decisions shaped yes/no, and genuine delight — "not a SaaS tool."
- **The 20-year veteran**: needs full visibility into everything in flight and fast access to
  ground truth (branches, PRs, checks, raw journal). Must never pay for the fun.

The mechanism serving both is **two altitudes everywhere**: a plain-English surface generated
from the same facts, and an expandable technical layer (never a separately-maintained one, so
they cannot drift). Plus **boring mode** (§4) as the vet's full escape hatch.

## §2 — The interaction model

### Information tiers

| Tier | Contents | Behavior |
|---|---|---|
| **Push** (interrupts) | Factory-stopped: runner down (dead-man's switch, §6), ALERT, freeze that is aging past threshold or whose auto-fix itself failed/stalled, usage cap. Decisions blocking work: `needs-william`, bounces. **Machine-gave-up parks as a threshold digest** ("4 parked, oldest 26h — tap to triage"), event-driven so the no-ritual rule holds. | Reaches William wherever he is (iMessage today). Rare by design. |
| **Inbox** ("Needs You") | Every decision waiting on him, each card: plain-language headline + gloss, memo, and buttons. | Sits until he comes; badge-counted; never re-pings. |
| **Ambient** | The airfield, the boards, tower log, health strip, per-repo shipped-delta, "since you last looked" markers. | There when he looks. Raw depths (journal, cmux tabs, GitHub) exist underneath; he should never *need* them. |

### The verbs (complete list — new loops must not add verbs)

**Plan** (sit-down session, outside the center) · **Flag** (quick-capture box → files raw text
as a GitHub issue labeled `flag`, no AI; a planning session sweeps flags later) ·
**Approve / Decline** (tap = William's word + audit comment) · **Steer** (`expedite`,
priority, `preserve`, drop) · **Discuss** (copies a ready briefing snippet for a fresh Claude
session) · **Promote** (per-repo grade) · rare ops verbs stay CLI (`run`, `adopt`, `doctor`).

**Verb amendment (owner, 2026-07-07):** `tidy` — close finished session windows — is the
first rare-ops verb promoted from CLI to a dashboard button: William-tapped, an in-UI
confirm listing exactly what will close (replacing the CLI y/N), executing the local
`superlooper tidy` command. This deliberately creates a second button class (local command
execution, not a GitHub write); localhost-only remains absolute, and the CLI mechanically
cannot close a live session. Other ops verbs (`run`, `adopt`, `doctor`) stay CLI.

### Modes

**Away is the baseline** and is exactly the proven V1 machine: park-and-continue, answerer
resolves worker questions, quiet except factory-stopped pushes. **Present mode (V2)**:
auto-detected presence (+ one-tap override; unknown = away) routes worker questions to William
first with a bounded ~30-min wait and safe fallback to the answerer — an optimization layer,
never a dependency; the loop must never stall on him.

## §3 — The airport world

Every issue is a **flight** flying one closed **circuit** (the traffic pattern) around its
repo's terminal — superlooper flies loops. **Flight number = issue number** (SL-441 ⇔ #441)
everywhere, so every surface is journal-greppable.

**Circuit stages (discrete — position never encodes time or fake progress):**
at the stand (approved, queued) → taxi out (launching; delivery-verification made visible — a
launch flake is a plane that never reaches the runway) → takeoff (session started) → downwind
(building; elapsed timer + liveness, §5; landmarks below mark real phases: Reconcile Point =
the mandatory step-0 issue-vs-reality check, Build Island, Review Ridge = fresh-agent review,
CI Shoals) → base turn (report filed) → final (the gate: clearance checklist with **real check
names** — report ✓ review ✓ CI ✓ mergeable ✓ → "cleared to land") → touchdown (merged) → taxi
in (closed, cleaned up) → arrivals board.

**Mappings (post-review, truth-fixed):**

| Real mechanic | Rendering | Notes |
|---|---|---|
| Lanes | Runways ("2 runways = 2 concurrent builds"); a lane's flight owns its runway for takeoff and landing | freeze does NOT close runways (see below) |
| Merge | Landing | dev slang alignment ("land the PR") |
| Queue | Departures board (split-flap), real launch order, ⚡ expedite on top | |
| Shipped | Arrivals board, newest first, plain sentences (MVP: issue titles) | |
| `blocked-by` | "Awaiting connection SL-41" on the departures board (never in the air) | |
| Gate hold (overlapping lane) | "Number 2 for landing" — sequenced behind the other flight | |
| Conflict → regenerate | **Honest retire-and-rebuild:** the old attempt is visibly retired (its branch/PR preserved and linked, marked superseded); a NEW flight taxis out as SL-441·a2 with an empty hold that refills. Memo in plain words: "previous work discarded — rebuilding from scratch against the updated code; attempt 2 of 2 before this comes to you." Per-issue go-around counter surfaced; repeats flagged as a scoping smell. | Never "equipment swap" / "nothing was lost" framing — that was a lie (review, adopted) |
| Freeze (merges paused, builds continue) | **Calm**: "landings paused — repair flight dispatched," landing clearance suspended on the arrivals path; planes keep taking off and flying. Crash/incident imagery is **reserved** for freeze-AND-auto-fix-failed. A freeze that ages past threshold escalates to a push. Incident tied to the failing check + candidate merge SHA(s); rendered "culprit among these N" when ambiguous. | Freeze is the designed safe idle state; drawing it as a crash trains panic then crash-blindness (review, adopted) |
| Parked | Looks **stalled** — chocks, dimmed, "gave up" — never restful; persistent "N parked, your call" ribbon + the threshold digest push | |
| Worker blocked | Radio call: plain question in the tower log with who's answering ("auto-tower responding…"); V2 presence routes it to William first | |
| Diff size | Plane weight/cargo = **size, a neutral fact** ("+340/−12" chip alongside) — never "risk" | a long flight with zero cargo is also visible |
| Repos | Terminals on one pannable field, sized by activity | |
| Bounce / needs-william | Distinct **amber "awaiting your decision"** state — never visually confusable with a dead session or a cleanly finished one (§5) | |

**Costume rules (the discipline):**
1. The metaphor may only encode **true mechanics**; inexact mappings get plain words.
2. Costume never touches words you act on — buttons, memos, check names, error text are real.
   **Amendment (review, adopted):** decision cards *lead* with a plain-language gloss
   ("mergeable = fits cleanly onto today's code"); the literal term is secondary/on-hover for
   the vet. The conflict-cap card names the collision in one plain sentence and offers
   reasoned choices, never a bare badge.
3. Squint test: delete the airplane art and what remains must be a correct state diagram.
4. **(New, review, adopted):** any journaled event type renders **in plain words the day it
   exists**; metaphor skin is an optional per-type treatment added later. A dashboard that
   silently under-reports an autonomous system is worse than none. (This also defines the
   no-PR terminal for V2/V3 issue types: a "findings" pad whose checklist is report ✓ only.)

## §4 — The screen (desktop; mobile deferred)

Four-panel layout:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ● all systems ok   ✈ tower: auto   usage ▓▓▓░ 62%          [+ Flag] │  ← global pill aggregates
├────────────┬───────────────────────────────────────────┬─────────────┤    the WORST state across
│ NEEDS YOU  │              THE AIRFIELD                 │ TOWER LOG   │    ALL repos, names the
│ cards w/   │   animated, pannable; terminals,          │ comms feed: │    offender; trouble
│ buttons +  │   circuits, holds, landings; [⏮ replay]   │ questions,  │    raises a persistent
│ glosses;   │                                           │ answers,    │    banner independent of
│ never pans │                                           │ nudges,     │    camera/scroll
│ or filters;│                                           │ memos; each │
│ collapses  │                                           │ expands to  │
│ when empty │                                           │ raw journal │
├────────────┴──────────────┬────────────────────────────┴─────────────┤
│ DEPARTURES (queue)        │ ARRIVALS (landed, plain sentences)       │
└───────────────────────────┴──────────────────────────────────────────┘
```

- **Boards + tower log follow the camera** (zoomed into a repo → filtered); **Needs You never
  filters, never moves.** Empty Needs You collapses to an "all clear" ribbon.
- **The flight-card drawer** opens from any plane or row anywhere, always the same: issue
  title, circuit position, clearance checklist, links (issue/PR/branch), memo history, that
  flight's journal slice.
- **Boring mode (review, adopted — additive, not a replacement):** one keystroke flips the
  center to a dense flat table of every flight across repos, sortable by stage / staleness /
  elapsed / repo; the tower log gains a toggle to the full `journal.jsonl` firehose with
  time-range + free-text filters and clickable flight numbers. Every visual channel is paired
  with an exact numeral ("idle 12m", "+340/−12") — sort by the number, the art is flavor.
- **"Since you last looked"** markers + Needs You badge replace any scheduled report.
- **The morning artifact (review, adopted):** a mechanically generated plain digest — counts
  plus one sentence per exception (parks, go-arounds, freeze arc) — over a timestamped,
  clickable event table. **The night replay** (the airfield playing back any period as a
  time-lapse, scrubbable/steppable, every frame a clickable event) is a beloved *treat* behind
  a button, never the load-bearing answer to "what happened."

## §5 — Honest signals (liveness, progress, ambiguity)

- **Contrail = liveness**, tied to the runner's real tiers: crisp while the activity file is
  fresh; thins as it ages; sputters at the 8-min idle tier (and you see the tower peek);
  gone at the 45-min frozen tier. A stalled session is visible from across the room.
- **Progress ≠ liveness (review, adopted):** a separate progress signal derived from existing
  data (diff-stat delta, distinct files touched, journal event variety over a rolling window).
  **Crisp contrail + flat progress = an explicit "spinning?" warning state** — the doom-looping
  worker that re-runs the same failing test forever must never render as the healthiest plane
  on the field. Card/hover shows last journal line inline.
- **Off-path states are visually distinct (review, adopted):** grey/no-contrail is reserved
  strictly for liveness failure; **amber** = awaiting an owner decision (bounce,
  needs-william); **base-turn position** = cleanly finished, report filed. Three states
  demanding opposite responses never share a rendering.
- **Alarm salience inside an animated world (joy ruling + review, reconciled):** trouble gets
  treatments motion cannot bury — the field dims and the problem is lit; the global pill and a
  camera-independent banner name any off-screen trouble. The quiet state carries an explicit
  caption ("last landing 3h ago — all clear") so calm is never ambiguous.

## §6 — Monitoring armor

- **Dead-man's switch on the runner (review, adopted):** the dashboard backend — structurally
  separate from the runner — watches `state/runner.heartbeat`; past threshold the entire
  surface grays with "RUNNER DOWN — last heartbeat Xm ago" (a state stale data cannot fake)
  and fires a push. Nobody-watches-the-watcher, closed. No runner changes needed.
- **Push taxonomy is the safety net; glanceability is never load-bearing for an absent
  owner.** Final push list: runner down · ALERT · freeze aging past threshold or auto-fix
  itself failed/stalled · usage cap · needs-william/bounce · parks via threshold digest.
- The center **adds no machinery to the runner** in V1. It is a read-only poller (journal,
  state dir, `gh`, read-only `git diff --stat` against existing worktrees) plus label/comment
  writes when William taps.

## §7 — The fun layer (gamification — fun pass 2026-07-06, vetted and adopted)

**Design law** (every mechanic must satisfy all six; violations are dead on arrival):

- **Never gamify judgment** — no points, speed, streaks, or celebration attached to approvals
  or promotion. The two human gates are fun-free zones, permanently: nothing animates, sounds,
  or counts when William applies `agent-ready` or grades a promotion. The calm there IS the
  design.
- **Never punish absence** — no login streaks, no decay, no FOMO, no daily quests. All
  progression is computed by aggregation over the append-only `journal.jsonl` — no stored
  progression state exists, so nothing CAN decay: absence-proof by construction, not policy.
- **Celebrate only real outcomes** — landings, green nights, rescued parks, adopted ratchet
  rules, milestone counts. Never engagement metrics.
- **Zero added friction** — no mechanic may add steps, obscure information, or tax the vet.
- **Honesty** — no celebration may misrepresent state (a wandered merge gets NO flourish).
- **Taste-dependent elements (sound especially) are individually toggleable**, plus one
  master fun toggle covering everything in this section.

**The governing insight (adopted as the layer's north star):** the best moments are
deliverable BY absence. The emotional signature of this product is "it worked while I was
gone," so the peak loop is *walk away → come back → discover what your field did without
you* — which structurally inverts every dark pattern in the genre.

**The invariant (reverse squint test):** delete the entire fun layer and the dashboard loses
no information and gains none. Fun is load-bearing for *wanting to look*, and for nothing
else.

### Mechanics (vetted; grouped by build order)

**Owner curation (2026-07-06):** the MVP core below is owner-reviewed. **Cut by owner taste
(do not re-add without his ask):** the water-cannon salute, the field dog, and the
boring-mode single flap — **boring mode is fully static, no exceptions.** The greaser/firm
landing honesty is owner-approved. Mechanics in "fast follow" and "when the field earns it"
that are not explicitly marked approved are *proposed, pending owner taste* — bring them to
William before building.

**Ship with the MVP (cheap, immediate warmth):**

- **Solari arrivals board** *(owner's favorite — the flagship moment)* — merges flutter in
  split-flap style and settle into the real issue title (optional mechanical clack). This is
  where animation-quality investment goes first: the flutter must be genuinely satisfying.
  Settle < 1s, readable mid-flutter, honors `prefers-reduced-motion`.
- **Repos as airlines** — each adopted repo gets a name, crest, colors ("TITAN AIR — est.
  2026-07 — 214 landings"); auto-generated defaults, renameable. Identity serves legibility
  ("Titan Air heavy on final" parses faster than a repo slug); the literal slug stays on
  every flight card and everywhere in boring mode.
- **The living clock** — wall-clock time drives field lighting (runway lights at dusk,
  landing-light trails at night, dawn wash for the replay); calendar drives ground-level-only
  seasonal dressing. **Ambient sky/visibility weather is banned in writing** — no fog, no
  storms (see reconciliation below). No palette may reduce state-channel contrast; contrail
  legibility at night is sacred.
- **The corner counter** *(owner-requested, replaces the record board)* — a small
  always-visible stats corner for the feel-good quantity read: PRs landed this week, lines
  added/removed, issues closed. Journal + `gh` computed, nothing stored. **Standing audit
  rule unchanged: outcome stats only — no human-latency stat (time-to-approve, needs-you
  dwell) may ever appear.**
- **The incident sign** *(owner-requested, replaces the service-record calendar)* — the
  classic factory sign, machine-humor edition, painted on a hangar wall: **"N landings since
  the last incident."** An *incident* is strictly a machine-side failure event — a park, a
  conflict-cap hit, a freeze whose auto-fix failed, a runner death. **William's own gates
  (approvals, bounce answers, promotion) are normal operations and never touch the counter**
  — acting on the loop must never feel like breaking a streak; only the machine's stumbles
  repaint the sign.

**Fast follow:**

- **The pilot's logbook** — the durable external memory: total landings, circuits, go-arounds
  survived, heaviest cargo, night landings, per-airline subtotals, firsts. 100% journal
  aggregation. Same standing audit as the record board: outcome stats only, zero presence or
  judgment-latency stats, forever.
- **Greaser/firm landing honesty** *(owner-approved)* — landing quality from journal facts only: zero go-arounds
  + first-pass gate + no hold = a "greaser" (butter-smooth, tower log: "SL-441, nice
  landing"); any other clean merge = a firm landing (still positive — correct technique, not
  failure); a merge that wandered outside declared areas gets no flourish at all, neutral
  touchdown, "see report" marker.
- **"Back in service"** — a merge whose issue history contains a prior park gets a wrench
  badge and a tower-log line; closes the open failure loop audibly. If a replacement issue
  landed the fix, the line says so honestly ("SL-441 superseded by SL-457 — landed").
- **Safety placards** *(lands with the V2 ratchet loop)* — each adopted ratchet rule becomes
  a framed placard on the tower wall: verbatim text, date, link to its incident. Aviation's
  deepest real tradition (every regulation traces to an incident) — failure-processing as
  visible institution-building. Auto-detected, never a ceremony; flavor only on resolved
  material, never on live decision cards.

**When the field earns it:**

- **Aircraft types by issue class** — silhouettes from real labels: owner-planned features =
  mainline jets; Dependabot = small cargo props; sweeper = utility turboprop; the auto-fix
  issue = a repair aircraft in an **exclusive emergency livery** that visibly taxis past the
  whole queue (true — it really does). Unknown label = default airframe, never a guess.
  "Last night was mostly little Dependabot props" becomes a story you see from across the
  room.
- **Ground-crew vignettes** — every vignette keys to a real journal/state event via a
  **published mapping table** (fuel truck while liveness is fresh; marshaller during gate
  checks; sweeper cart after a cleanup event; chocks + "MX REQ" tag on parked planes). Any
  vignette without a real event gets cut. (The field dog was cut by owner taste.)
- **Night signatures & the Year in Flight** — each night's replay data rendered as a
  long-exposure image: circuits as discrete stage-to-stage light segments (angular by design
  — honestly reflecting discrete positions, never interpolated), go-arounds visible as loops.
  Auto-saved as that night's logbook page; any date range compiles on demand into a "Year in
  Flight" reel. On-demand only — never a scheduled ritual. Telemetry as generative art that
  IS the data.
- **Milestone liveries** — per-airline landing counts (25/100/250/500/1000) permanently
  unlock paint schemes. Cosmetic only; the state channels (contrail, stage, weight) remain
  the only state visuals; **no unlockable livery may resemble the repair aircraft's
  emergency livery** (that one is semantic and reserved).
- **The museum hangar** — notable airframes preserved automatically (first landing per
  airline, heaviest diff, milestone landings, placard-producing flights); clicking one opens
  its real flight card. Off the main sightline, zero runtime information — and it gives the
  disposable aircraft a dignified afterlife that reinforces the correct mental model:
  flights are durable, airframes are moments.
- **Sister airports** *(when friends adopt)* — strictly opt-in both sides: a friend's install
  exports a **postcard** (static snapshot: skyline render, landing count, founding date,
  latest museum piece) you pin on your tower wall; liveries/crests exportable as gifts. No
  live feeds, no rankings — the UI is **structurally incapable** of juxtaposing two airports'
  stats. Animal Crossing letters, not Strava.

**Sound (the honest soundscape):** master + per-layer toggles: Solari clack, touchdown thump,
radio-squelch click per tower-log entry, optional low ATC murmur whose density derives from
the **real** event rate (a quiet night is silent — liveliness is never faked), rare optional
"cleared to land" voice on gate pass. Ships with clicks low and murmur OFF. Tower-log radio
prefixes ("roger," "going around") always carry the real sentence beside them; boring mode
strips all flavor.

### The kill list (rejected by design — recorded so no future session re-invents them)

1. **Approval XP / inbox-zero celebration on Needs You** — the most natural gamification on
   the whole surface, and it is a speed bonus on the two human gates. Trains hurried
   judgment. DOA.
2. **Login streaks / daily quests / "your field missed you"** — every variant monetizes guilt
   about absence, including soft forms (a dimmed "welcome back" implying neglect). DOA.
3. **Friend leaderboards** — competitive throughput pressure flows backward into the approval
   gate; corrupts judgment indirectly, which is worse because it's deniable. DOA.
4. **Owner latency metrics** (time-to-approve, needs-william dwell, decisions/day) — looks
   like ops telemetry, is a stopwatch on William's judgment. DOA (and the standing audit on
   logbook/records enforces it forever).
5. **Random / loot-box rewards** — variable-ratio reinforcement is the literal addiction
   machinery of dark-pattern gaming; it makes you crave the slot machine, not the outcome.
   Every unlock here is deterministic and milestone-tied. DOA.
6. **Weather-as-loop-health** (storms/fog when things break) — all loop trouble is causal and
   the freeze already reads calm-by-design; fog also literally obscures the field. DOA.

**Reconciliation with §10's "weather over Production":** the ban above is on weather as
decoration or as LOOP state on the dev field. V3's prod telemetry may render as weather at
the Production destination because it is genuinely exogenous — user behavior arrives from
outside the system's control, exactly like weather; loop failures never do.

## §8 — Review record (2026-07-06, 7-agent adversarial panel)

Panel: six attack lenses (veteran-dev workflows, novice mental-model, SRE/monitoring rigor,
metaphor integrity, scale + build realism, and a devil's-advocate null hypothesis) + one
judge. 47 raw findings → deduped, adjudicated; 6 killed as overblown.

**Adopted (folded into §§3–6 above):** boring mode + full-journal search; dead-man's switch;
progress-vs-liveness ("spinning?") signal; freeze redesign (calm routine / escalating aging /
crash imagery only for auto-fix-failed; lane vs merge-gate split); honest retire-and-rebuild
regeneration framing; parks look stalled + threshold digest push; distinct
amber/grey/finished states; camera-independent alert rail + aggregated global pill; stage-
discrete positions (no time-as-position); plain-language glosses on decision cards; weight =
size never risk; costume rule 4 (plain-words fallback for unmapped events + no-PR terminal);
morning digest mechanical + replay demoted to a treat.

**Overruled (owner ruling §0.1):** the recommendation to demote the animated airfield to
static flight cards. The panel's goal function omitted joy as a terminal requirement (its
brief framed fun as instrumental). Two of its supporting arguments also fail on inspection:
"the field trains babysitting" treats optional delight as toil, but the runner is safe
unattended by design — watching is riskless entertainment; and "hardest-to-maintain component
for a solo non-professional" prices maintenance as human hands, but the dashboard will be a
repo inside the loop itself — the machine maintains the machine's face. The genuine costs
knowingly accepted: the field is the MVP's largest build item, and novelty-decay risk exists
(hedged by boring mode).

**Open risks to watch in real use:** doom-loop detection heuristics will need tuning against
real incidents; push thresholds (freeze age, park count/age) need calibration rounds against
real nights; the phase-2 skin-drift risk if V2 event types ship plain-text-only (costume rule
4 mitigates); launch-flake clustering may eventually warrant a rate/trend push; watch for
blind Approve presses on conflict-cap cards (consider making Discuss the highlighted default
there); with the field kept, verify the fun ages well — boring mode is the hedge.

## §9 — MVP scope and data feasibility

**In (all feedable by existing machinery — journal.jsonl, state markers, `gh`, worktrees):**
the airfield (animated, per §0.1) · boards · tower log · Needs You cards with working buttons
(approve/re-approve = label + audit comment; drop; expedite; bounce-yes; Discuss = copy
briefing) · flight-card drawer · boring-mode table + journal firehose · dead-man's switch ·
replay + mechanical digest · diff-size chip (read-only `git diff --stat` poll) · flag box
(one `gh issue create`) · since-you-last-looked · global pill/alert rail.

**Deferred to V2:** presence modes + William-first question routing; per-tool activity verbs
("running tests…"); AI-drafted flags; composed arrival prose (MVP uses issue titles); the
V2 dashboard additions below.

**Known data gaps (accepted for MVP):** activity files carry timestamps but not verbs;
progress signal is heuristic until per-tool verbs exist.

**Build note:** the dashboard should live as its own repo **adopted into the superlooper loop**
— built and maintained via issues through the very machinery it renders. (Also the honest
answer to the panel's maintenance concern.)

## §10 — How V2/V3 land (the growth rule)

> **A new loop never adds a surface or a verb.** It adds inbox items (same yes/no shapes),
> feed entries, and health rows. The unit of everything stays "an issue with a label."
> Costume rule 4 guarantees nothing is ever invisible for lack of metaphor art.

Mapped: triage/Slack drafts → Needs You approval cards (booking desk) · sweeper → one weekly
**batch card** (approve all / cherry-pick / skip) · Dependabot → **inbound flights from
another airport**: PRs not born from issues, still needing full landing clearance · janitor →
batched ground-crew proposals (and the apron clutter is visible before it even asks) ·
ratchet → proposed-rule cards, approved like issues; adopted rules live in **the Rulebook**
(each rule linking its incident; the known-failure ledger alongside as the "known squawks"
list) · V3 investigations → **recon flights** (fly the circuit, land no PR; cargo = a findings
report + proposed child flights, grouped under the parent) · V3 prod telemetry → **weather
over Production airport** (the one genuinely new data source; a storm forming visibly precedes
the recon flight it triggers).

**Dashboard additions earned by V2, in order:** origin badges on every flight (whose word or
which standing rule/bot admitted this work — the audit trail at a glance) · the
**scheduled-services panel** (each recurring service: last ran, next due, produced what —
because V2's new failure class is a generator silently dying, and V1's surfaces only watch
flights) · batch cards · the Rulebook · weather (V3).
