"""The limitations ledger (issue #450): the durable home for findings that are TRUE but not worth
a lane, so a nit close is a FILING rather than a loss.

`plugin/skills/superlooper/references/triage-standing-rule.md` is the ruling of record; this module
is its mechanical half — the marker, the title, the body adopt writes, and the pure find that makes
`adopt` idempotent.

**It lives as a GitHub ISSUE, deliberately.** Every other durable loop fact is a tracked file or a
state-home JSON; this one is not, because the thing that WRITES it is a triage flight whose only
permitted writes are GitHub and its own state folder. A ledger in the repo would need a PR, a
review and a merge for every accepted nit — which is more lane cost than the nit itself, i.e. the
exact accounting the rubric exists to respect. As a GitHub issue it is also readable by anyone with
the repo open, and pinned it is the first thing they see.

**Not to be confused with `lib/ledger.py`.** That is the known-FAILURE ledger — a state-home JSON
mapping a failure fingerprint to William's acceptance, read by the gate on every tick. This one is
prose, per-repo, on GitHub, and no gate reads it. Two ledgers, two audiences: `ledger.py` keeps a
red test from re-blocking a merge; this keeps a true-but-cheap finding from being re-filed and
re-argued forever.

Pure: nothing here touches GitHub. `adopt` owns the gh calls (create-or-find, pin), and the two
consumers — the worker brief and the cross-review instructions — carry the consult-the-ledger line
as prose.
"""

# The marker. A LABEL rather than a recorded issue number in the config, for two reasons: a number
# in `.superlooper/config.json` would put the ledger's identity behind the one file a loop worker
# may never edit (the referee's rulebook), and a label is what a human filtering the issue list
# actually reaches for. Registered in `lib/labels.py` — gh refuses `issue create --label` outright
# for a label the repo lacks, so an unregistered marker would mean adopt silently scaffolds nothing
# (the #165/#337 defect class). NOT '(runner-managed)': the runner never applies it; `adopt` does,
# once, when it scaffolds the ledger.
LEDGER_LABEL = "limitations-ledger"

LEDGER_TITLE = "Limitations ledger — accepted limitations, filed rather than lost"

# The body adopt writes on the issue it creates. A module-level template rendered through a
# FUNCTION (never handed out directly) so no caller can hold a reference that a later edit would
# ride into the next repo's ledger — the shared-mutable-default class this suite keeps catching.
#
# It documents its OWN entry format on purpose (DoD): the ledger is the one artifact a triage
# flight can reach with no checkout, so "how do I write an entry" has to be legible from the issue
# itself, not from a doc the flight would have to clone a repo to read.
_LEDGER_BODY = """\
<!-- superlooper-limitations-ledger -->

This is the repo's **limitations ledger**: the durable home for findings that are true but not
worth a lane. A nit closed to this ledger is a **filing, not a loss** — the finding stays readable,
and the next worker or reviewer can see that it has already been weighed and accepted.

`superlooper adopt` scaffolds this issue and pins it. Keep it **open** and **pinned**; the
`{label}` label is how every consumer finds it. It lives as a GitHub issue precisely so
that changing it never needs a PR, a review, or a merge.

## How to add an entry

Append one bullet to **Entries** below. Every entry carries exactly three things:

1. **The rubric line** that made it a nit — from the repo's nit rubric (a per-repo config override
   may replace the default set):
   - **N1 Unreachable input** — the defect requires input no live code path can produce.
   - **N2 Cosmetic-only** — wording or spacing in a surface no decision or alert reads.
   - **N3 Cost exceeds consequence** — the fix's full lane cost exceeds the worst realized
     consequence, and that consequence is loud when it happens.
   - **N4 Duplicate hardening** — a second guard for a class a merged class-killer already covers.
2. **The limitation itself**, in a sentence or two: what is true, and what it costs.
3. **A link to the closed source issue**. That issue's close comment links back here, so the trail
   reads in both directions.

Format — one bullet, in that order:

```
- **[N3] The morning report rounds a run's burn to whole minutes.** A 20-second run reads as
  "0m". The only surface that reads it is the report itself, and the real number is one journal
  line away. — accepted from #123 (closed)
```

**Never a nit, so never an entry here:** anything silent-failure-shaped; anything touching
approvals, merging, or publishing; anything the owner has personally flagged.

## Who reads this, and when

- **Workers**, before filing a follow-up issue: if the finding is already an entry here, it is
  accepted — do not re-file it.
- **Reviewers**, before raising a finding: an entry here is a filed, accepted limitation, not a
  new finding.
- **The triage flight**, before closing anything as a nit: the close comment and the entry are
  written together, or neither is.

An entry is **reversible**. If a limitation stops being acceptable, open a fresh issue that says so
and strike the entry — nothing here is a promise never to fix it.

## Entries

_(none yet — `superlooper adopt` scaffolded this ledger empty.)_
"""


def ledger_body():
    """The markdown body adopt writes on a freshly scaffolded ledger issue.

    Rendered per call (the label is substituted from LEDGER_LABEL, so the marker is spelled once)
    and returned as a fresh string, so a caller can never mutate the template other callers read.
    """
    return _LEDGER_BODY.format(label=LEDGER_LABEL)


def find_ledger(issues):
    """The repo's ledger among raw ``gh issue list`` dicts, or None.

    Returns the LOWEST-numbered candidate that actually carries ``LEDGER_LABEL`` in its own
    payload. Three deliberate properties, each of which is the difference between adopt finding
    the ledger and adopt doing something worse than nothing:

      * **the marker is confirmed from the payload**, never assumed from the ``--label`` filter the
        caller asked for. A filter is an argument; the payload is the answer. This is also what
        makes this a pure function a test can drive, rather than trust in an argv flag.
      * **lowest-numbered wins**, so a repo that somehow grew two ledgers converges on the first
        one instead of pinning and printing a different number on every adopt run.
      * **fails closed on garbage** — a wrong-typed input, a candidate with no usable ``number``,
        an unreadable ``labels`` — reads as "no ledger here". Safe only in this direction, and only
        because the caller's response is to scaffold a FRESH ledger: it never writes into an issue
        whose shape it could not read.

    The returned value is the caller's own dict (``number``, and ``isPinned`` when the read asked
    for it), not a copy — callers here only read it.
    """
    best = None
    for item in issues if isinstance(issues, list) else ():
        if not isinstance(item, dict):
            continue
        num = item.get("number")
        if type(num) is not int:          # `True` is an int subclass; an issue number is not a bool
            continue
        marks = item.get("labels")
        if not isinstance(marks, list):
            continue
        names = {m.get("name") for m in marks if isinstance(m, dict)}
        if LEDGER_LABEL not in names:
            continue
        if best is None or num < best["number"]:
            best = item
    return best
