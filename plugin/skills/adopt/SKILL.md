---
name: adopt
description: Bootstrap a fresh machine into the superlooper loop and wire up a repo. Use when setting superlooper up for the first time on a new computer, or adopting a new repository into the loop — the ordered clone → publish → machine-check → adopt → doctor → run walkthrough, plus a pointer to the full published `.superlooper/config.json` contract and label set. Deliberately self-contained: everything you need before the engine is installed is right here; the full contract it points at is a published file that lands the moment you publish.
---

# adopt — from a bare machine to a running loop

This skill is the **bootstrap**. It takes a computer that has never run superlooper and walks it,
in order, to a loop that is building approved issues. It is deliberately **self-contained**:
nothing below asks you to open a source-repo file before the engine is installed — on a fresh
machine you have not even cloned the repo yet. The one thing it routes to, the full adoption
contract, is a **published** file that exists only *after* step 2, and it is named by its stable
installed path so you can always find it.

Two different things get "adopted", and it helps to keep them straight:

- the **machine** — steps 1–2 put the `superlooper` command and its stack on this computer. Do
  this **once** per machine.
- each **repo** — steps 3–6 wire one repository's config, labels, and branch protection. Repeat
  per repository you point the loop at.

## The walkthrough — run these in order (the order is load-bearing)

Each step produces exactly what the next one checks for, so out-of-order guarantees a red report
(e.g. a `doctor` before you `publish` finds no launch shim, no hooks, and no `superlooper` command
to run at all). Do them top to bottom.

1. **Clone the source repo.** `git clone <superlooper-repo-url>` and `cd` into it. This checkout
   is the only source you need — it carries the gated installer and the engine payload. Nothing
   else here is read from the repo; everything after step 2 runs from the *installed* copy.

2. **Publish — `./bin/install.sh`, from the repo root.** This copies the engine into
   `~/.claude/skills/superlooper/`, registers the two activity hooks, installs the keystroke-free
   launch shim, and links a stable `superlooper` command onto your PATH (it prints the exact
   `export PATH="…"` line to add if the chosen bin dir isn't already on PATH). Re-run any time to
   republish.

   **Why it pauses to ask you.** `./bin/install.sh` is the loop's one **publish gate**, and it is
   not a formality. The running loop executes the *installed* copy, never this source repo — so a
   merged engine change is **inert** until someone republishes. That makes publish the trustworthy
   place to catch an unwanted change: before it copies anything, the installer shows you the exact
   **diff** of engine files changed since the last publish and refuses to proceed without an
   **explicit OK** (an interactive `y`, or `--yes` once you've reviewed the list). Read the list —
   if it is what you expect, approve it on purpose. This human checkpoint is *why* `skills/**` can
   be a trusted bright line: no engine change reaches a live loop without a person saying so here.

3. **Check the machine stack — `superlooper doctor --stack --repo <path>`.** This is the
   machine-level readout: `cmux` present, `claude` logged in through the subscription account,
   `gh` authenticated with API headroom, the launch shim sourced, and the notify channel actually
   delivering. It changes nothing except one announced test notification, prints one line per
   block, and exits nonzero only on a `FAIL`. Fix every `FAIL` before moving on. (The `notify`
   block will `FAIL` until step 4 writes a config with a notify channel set — that's expected;
   re-run this after step 4 to confirm it goes green.)

4. **Adopt the repo — `superlooper adopt --repo <path>`.** Writes `.superlooper/config.json` from
   the template, seeds the CLAUDE.md standing-rules block, creates the label set, and prints the
   branch-protection advice. It is safe to re-run: it never overwrites an existing config, and it
   creates the labels idempotently (`--force`) — an existing label is updated in place, never
   duplicated. Then **edit the config**: set `repo`, your `areas`, at least
   one `required_checks` entry, and any `bright_lines`. (Every field is documented in the
   published contract — see below.)

5. **Check the repo wiring — `superlooper doctor --repo <path>`.** Verifies the config parses, the
   labels exist, `dev_branch` exists on origin, the PR-required checks are non-empty, and every
   `required_checks` name actually matches a check the repo has reported (a case/shape typo would
   otherwise gate a green PR forever). `doctor` changes nothing — fix anything red and re-run until
   it is all-green.

6. **Run — `superlooper run --repo <path>`.** Start the runner in a cmux tab you can watch (it
   targets that tab's own pane automatically). This is the only way to run it — there is no
   launchd daemon, because a paneless daemon can't open the worker tabs the loop needs. It picks
   up approved issues on its next tick. Approval is William's word: an agent never applies
   `agent-ready`; it is added only after his explicit say-so.

## The full contract lives here (published)

Everything above is the *bootstrap*. The complete adoption contract — every
`.superlooper/config.json` field with its default and meaning, the full label set, the
branch-protection detail, and the "config is trusted executable data" warning — is one published
file:

```
~/.claude/skills/superlooper/docs/ADOPTING.md
```

Step 2 puts it there. ADOPTING.md rides the *same* gated payload as the `superlooper` command, so
on any machine where that command resolves at all, this doc sits beside it at exactly that path —
no guessing, no source checkout required. Open it when you edit the config in step 4: it is the
source of truth for every knob. It is the single canonical contract; nothing here duplicates it,
so the two can't drift.
